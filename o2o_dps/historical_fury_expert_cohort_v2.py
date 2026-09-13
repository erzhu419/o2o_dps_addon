"""Freeze exact post-fix Fury player candidates without inventing a policy.

This is the first, identity/performance-only step toward multiple historical
expert behavior prototypes.  It consumes the exact GUID-keyed Chronicle DPS
index, applies the project's existing 36-yard-bug boundary, and gives every
player one candidate whose raid summaries receive equal weight.  The DPS index
contains neither requested actions nor queue intent, so the output explicitly
refuses both a pooled-behavior-policy claim and a complete-expert claim.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence, TextIO

from . import chronicle_external_api_ingest_v1 as ingest_v1
from . import chronicle_external_character_dps_index_v1 as dps_index_v1
from . import chronicle_external_character_history_v1 as history_v1


SCHEMA = "historical_fury_expert_cohort/v2"
IMPLEMENTATION_REVISION = (
    "historical_fury_expert_cohort_v2.1_exact_guid_postfix_player_raid_stratified"
)
KIND = "historical_fury_expert_identity_and_performance_candidate_cohort"
OUTPUT_DIRECTORY = "derived/historical_fury_expert_cohort/v2"
OUTPUT_FILENAME = "cohort.json"

FROZEN_INSTANCE_NAME = "Upper Tower of Karazhan"
FROZEN_SPEC = "Fury"
FROZEN_ROLE = "dps"
QUEUE_UNKNOWN = "UNKNOWN_NOT_OBSERVED_IN_EXACT_DPS_INDEX"
ACTION_TRACE_MISSING = "NOT_JOINED_IDENTITY_AND_PERFORMANCE_ONLY"
COMPLETE_STRATEGY_REFUSED = "NOT_A_COMPLETE_HISTORICAL_EXPERT_STRATEGY"


class HistoricalFuryExpertCohortError(RuntimeError):
    """The source index or v2 cohort violates the frozen contract."""


@dataclass(frozen=True)
class FrozenCohortSelection:
    """The exact normalized source rows admitted by the frozen v2 contract."""

    source_binding: dict[str, Any]
    selected_rows: tuple[dict[str, Any], ...]
    source_row_count: int
    filtered_out_row_counts: dict[str, int]
    same_evidence_duplicate_count_collapsed: int


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise HistoricalFuryExpertCohortError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFuryExpertCohortError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HistoricalFuryExpertCohortError(f"{label} must be non-empty text")
    return value.strip()


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFuryExpertCohortError(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _number(value: Any, *, label: str, strictly_positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise HistoricalFuryExpertCohortError(f"{label} must be finite")
    result = float(value)
    if result < 0 or (strictly_positive and result <= 0):
        comparator = "> 0" if strictly_positive else ">= 0"
        raise HistoricalFuryExpertCohortError(f"{label} must be {comparator}")
    return result


def _timestamp(value: Any, *, label: str) -> tuple[str, datetime]:
    text = _text(value, label=label)
    assert text is not None
    try:
        parsed = ingest_v1._parse_rfc3339(text, field=label)
    except ingest_v1.ChronicleIngestError as error:
        raise HistoricalFuryExpertCohortError(str(error)) from error
    if parsed.year <= 1:
        raise HistoricalFuryExpertCohortError(f"{label} is a missing timestamp")
    return text, parsed


def _median(values: Iterable[float]) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _ratio(value: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return value / denominator


def _data_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.lower() for part in resolved.parts}:
        raise HistoricalFuryExpertCohortError(
            f"cohort data_root must be inside an offline_data tree: {resolved}"
        )
    return resolved


def _relative_to_data_root(path: Path, data_root: Path, *, label: str) -> str:
    try:
        return path.resolve().relative_to(data_root.resolve()).as_posix()
    except ValueError as error:
        raise HistoricalFuryExpertCohortError(
            f"{label} must stay inside data_root"
        ) from error


def _strict_json(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value!r}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise HistoricalFuryExpertCohortError(
            f"cannot decode {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HistoricalFuryExpertCohortError(f"{label} must contain an object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFuryExpertCohortError(
            f"cohort is not canonical JSON: {error}"
        ) from error


def _zstd_executable(requested: str | Path | None) -> str:
    if requested is not None:
        candidate = Path(requested).expanduser().resolve()
        if not candidate.is_file():
            raise HistoricalFuryExpertCohortError(
                f"zstd executable does not exist: {candidate}"
            )
        return str(candidate)
    found = shutil.which("zstd")
    if found is None:
        raise HistoricalFuryExpertCohortError("zstd executable is required")
    return found


def _iter_index_partition_rows(
    manifest: Mapping[str, Any],
    *,
    data_root: Path,
    zstd_executable: str | Path | None,
) -> Iterator[dict[str, Any]]:
    partition = _mapping(manifest.get("partition"), label="source partition")
    relative = _text(partition.get("path"), label="source partition.path")
    assert relative is not None
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HistoricalFuryExpertCohortError(
            "source partition.path must be a safe relative path"
        )
    path = (data_root / relative_path).resolve()
    try:
        path.relative_to(data_root.resolve())
    except ValueError as error:
        raise HistoricalFuryExpertCohortError(
            "source partition escapes data_root"
        ) from error
    executable = _zstd_executable(zstd_executable)
    process = subprocess.Popen(
        [executable, "-q", "-d", "-c", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        for line_number, raw in enumerate(process.stdout, start=1):
            if not raw.strip():
                continue
            yield _strict_json(raw, label=f"DPS index row {line_number}")
    finally:
        process.stdout.close()
    stderr = b"" if process.stderr is None else process.stderr.read()
    return_code = process.wait()
    if process.stderr is not None:
        process.stderr.close()
    if return_code != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise HistoricalFuryExpertCohortError(
            f"zstd failed while reading DPS index: {message or return_code}"
        )


def _source_index_binding(
    manifest: Mapping[str, Any], *, manifest_path: str
) -> dict[str, Any]:
    if manifest.get("schema") != dps_index_v1.SCHEMA:
        raise HistoricalFuryExpertCohortError("source DPS index schema is unsupported")
    if manifest.get("implementation_revision") != dps_index_v1.IMPLEMENTATION_REVISION:
        raise HistoricalFuryExpertCohortError(
            "source DPS index must use the current boundary-aware revision"
        )
    content_address = _mapping(
        manifest.get("content_address"), label="source content_address"
    )
    digest = _text(content_address.get("sha256"), label="source content sha256")
    partition = _mapping(manifest.get("partition"), label="source partition")
    summary = _mapping(manifest.get("summary"), label="source summary")
    exact_count = _integer(
        summary.get("exact_ranking_record_count"),
        label="source exact_ranking_record_count",
    )
    partition_count = _integer(
        partition.get("record_count"), label="source partition.record_count"
    )
    if exact_count != partition_count:
        raise HistoricalFuryExpertCohortError(
            "source exact-ranking count disagrees with partition record count"
        )
    return {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "content_sha256": digest,
        "path": manifest_path,
        "exact_ranking_record_count": exact_count,
        "training_eligible_exact_membership_count": _integer(
            summary.get("training_eligible_exact_membership_count"),
            label="source training_eligible_exact_membership_count",
        ),
        "partition_record_count": partition_count,
        "source_censored_membership_count": _integer(
            summary.get("censored_membership_count"),
            label="source censored_membership_count",
        ),
    }


def _guild(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = row.get("guild")
    if raw is None:
        return None
    guild = _mapping(raw, label="row.guild")
    name = _text(guild.get("name"), label="row.guild.name")
    guild_id = _text(guild.get("id"), label="row.guild.id", allow_none=True)
    return {"id": guild_id, "name": name}


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    guid = _text(row.get("character_guid"), label="row.character_guid")
    instance_id = _text(row.get("instance_id"), label="row.instance_id")
    ranking_id = _text(row.get("ranking_record_id"), label="row.ranking_record_id")
    assert guid is not None and instance_id is not None and ranking_id is not None
    return guid.lower(), instance_id, ranking_id


def _comparison_stratum(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a within-raid descriptive peer-comparison stratum.

    Chronicle's aggregate Trash rankings legitimately have a null encounter ID,
    and one player can have several such rows in the same raid.  Their shared
    ``killed_at`` timestamp identifies the matching aggregate window without
    merging all Trash rows or inventing an encounter UUID.
    """

    instance_id = str(row["instance_id"])
    encounter_id = row.get("encounter_id")
    if encounter_id is not None:
        return (instance_id, "ENCOUNTER_ID", str(encounter_id))
    return (
        instance_id,
        "NULL_ENCOUNTER_ID_TRASH_WINDOW",
        str(row["encounter_name"]),
        str(row["killed_at"]),
    )


def _normalized_candidate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "character_guid": _text(row.get("character_guid"), label="character_guid"),
        "character_name": _text(row.get("character_name"), label="character_name"),
        "server": _text(row.get("server"), label="server", allow_none=True),
        "realm": _text(row.get("realm"), label="realm", allow_none=True),
        "instance_id": _text(row.get("instance_id"), label="instance_id"),
        "instance_name": _text(row.get("instance_name"), label="instance_name"),
        "started_at": _text(row.get("started_at"), label="started_at"),
        "uploaded_at": _text(row.get("uploaded_at"), label="uploaded_at"),
        "guild": _guild(row),
        "contamination_label": _text(
            row.get("contamination_label"), label="contamination_label"
        ),
        "encounter_id": _text(
            row.get("encounter_id"), label="encounter_id", allow_none=True
        ),
        "encounter_name": _text(row.get("encounter_name"), label="encounter_name"),
        "killed_at": _text(row.get("killed_at"), label="killed_at"),
        "damage_done": _number(row.get("damage_done"), label="damage_done"),
        "duration_secs": _number(
            row.get("duration_secs"), label="duration_secs", strictly_positive=True
        ),
        "dps": _number(row.get("dps"), label="dps"),
        "spec": _text(row.get("spec"), label="spec"),
        "role": _text(row.get("role"), label="role"),
        "ranking_record_id": _text(
            row.get("ranking_record_id"), label="ranking_record_id"
        ),
    }
    expected = normalized["damage_done"] / normalized["duration_secs"]
    if not math.isclose(normalized["dps"], expected, rel_tol=1e-9, abs_tol=1e-9):
        raise HistoricalFuryExpertCohortError(
            "row.dps disagrees with damage_done / duration_secs"
        )
    return normalized


def _same_observation_evidence(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left == right


def _guild_key(guild: Mapping[str, Any]) -> tuple[str, str]:
    return str(guild.get("id") or ""), str(guild["name"])


def _player_key(row: Mapping[str, Any]) -> str:
    """Return the sole player identity used by the exact-DPS cohort.

    Server, realm, and character name are descriptive metadata.  Letting any of
    them participate in the key would split one exact GUID after a rename or a
    metadata spelling change.  Conflicting server/realm metadata for one GUID
    is rejected later rather than used to manufacture separate players.
    """

    return str(row["character_guid"]).lower()


def _candidate_id(guid: str) -> str:
    return f"player:{guid}"


def select_frozen_cohort_rows(
    source_manifest: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    source_manifest_path: str,
) -> FrozenCohortSelection:
    """Apply the v2 cohort's one authoritative row-selection contract.

    Downstream exact-window joins use this function so their action evidence
    cannot silently drift from the identity/performance cohort selection.
    """

    source = _source_index_binding(
        source_manifest, manifest_path=source_manifest_path
    )
    boundary = ingest_v1.range_bug_boundary_contract()
    cutoff_text = str(boundary["postfix_known_clean_at_or_after_local"])
    _, cutoff = _timestamp(cutoff_text, label="frozen post-fix cutoff")
    eligible_labels = set(history_v1.TRAINING_CANDIDATE_LABELS)

    source_count = 0
    filtered_out: defaultdict[str, int] = defaultdict(int)
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    collapsed_duplicates = 0
    for raw in rows:
        source_count += 1
        row = _normalized_candidate_row(_mapping(raw, label="DPS row"))
        started_text, started = _timestamp(row["started_at"], label="row.started_at")
        row["started_at"] = started_text
        guild = row["guild"]
        guild_name = guild["name"] if guild is not None else None
        expected_label = ingest_v1.classify_range_bug(guild_name, started_text)
        if row["contamination_label"] != expected_label:
            raise HistoricalFuryExpertCohortError(
                "DPS row contamination_label disagrees with the current rule"
            )
        if row["instance_name"] != FROZEN_INSTANCE_NAME:
            filtered_out["other_instance"] += 1
            continue
        if row["spec"] != FROZEN_SPEC:
            filtered_out["other_spec"] += 1
            continue
        if row["role"].lower() != FROZEN_ROLE:
            filtered_out["other_role"] += 1
            continue
        if started < cutoff:
            filtered_out["before_frozen_postfix_cutoff"] += 1
            continue
        if row["contamination_label"] not in eligible_labels:
            filtered_out["contamination_nontraining"] += 1
            continue
        identity = _row_identity(row)
        prior = deduplicated.get(identity)
        if prior is not None:
            if not _same_observation_evidence(prior, row):
                raise HistoricalFuryExpertCohortError(
                    "conflicting rows share the exact DPS source unique key"
                )
            collapsed_duplicates += 1
            continue
        deduplicated[identity] = row

    if source_count != source["partition_record_count"]:
        raise HistoricalFuryExpertCohortError(
            "streamed DPS row count disagrees with source partition"
        )
    selected_rows = tuple(deduplicated.values())
    if not selected_rows:
        raise HistoricalFuryExpertCohortError(
            "frozen source contains no eligible Fury DPS rows"
        )
    return FrozenCohortSelection(
        source_binding=deepcopy(source),
        selected_rows=selected_rows,
        source_row_count=source_count,
        filtered_out_row_counts=dict(sorted(filtered_out.items())),
        same_evidence_duplicate_count_collapsed=collapsed_duplicates,
    )


def build_cohort_document(
    source_manifest: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    source_manifest_path: str,
) -> dict[str, Any]:
    """Build the deterministic v2 document from an already verified index."""

    selection = select_frozen_cohort_rows(
        source_manifest,
        rows,
        source_manifest_path=source_manifest_path,
    )
    source = selection.source_binding
    source_count = selection.source_row_count
    filtered_out = selection.filtered_out_row_counts
    selected_rows = list(selection.selected_rows)
    collapsed_duplicates = selection.same_evidence_duplicate_count_collapsed
    boundary = ingest_v1.range_bug_boundary_contract()
    cutoff_text = str(boundary["postfix_known_clean_at_or_after_local"])
    eligible_labels = set(history_v1.TRAINING_CANDIDATE_LABELS)

    encounter_groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        encounter_groups[_comparison_stratum(row)].append(row)
    local_ratios: dict[tuple[str, str, str], float | None] = {}
    for group in encounter_groups.values():
        median_dps = _median(row["dps"] for row in group)
        assert median_dps is not None
        for row in group:
            local_ratios[_row_identity(row)] = (
                _ratio(row["dps"], median_dps) if len(group) >= 2 else None
            )

    player_raids: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in selected_rows:
        player_raids[_player_key(row)][row["instance_id"]].append(row)

    player_candidates: list[dict[str, Any]] = []
    raid_to_players: dict[str, set[str]] = defaultdict(set)
    guild_to_players: dict[tuple[str, str], set[str]] = defaultdict(set)
    guild_to_raids: dict[tuple[str, str], set[str]] = defaultdict(set)
    guild_to_rows: dict[tuple[str, str], int] = defaultdict(int)
    for player_key in sorted(player_raids):
        raids: list[dict[str, Any]] = []
        all_rows: list[dict[str, Any]] = []
        for instance_id, raid_rows in sorted(player_raids[player_key].items()):
            raid_rows.sort(
                key=lambda row: (_comparison_stratum(row), row["ranking_record_id"])
            )
            all_rows.extend(raid_rows)
            guilds = {_guild_key(row["guild"]) for row in raid_rows}
            starts = {row["started_at"] for row in raid_rows}
            if len(guilds) != 1 or len(starts) != 1:
                raise HistoricalFuryExpertCohortError(
                    "one player/raid has conflicting guild or started_at evidence"
                )
            ratios = [
                local_ratios[_row_identity(row)]
                for row in raid_rows
                if local_ratios[_row_identity(row)] is not None
            ]
            total_damage = sum(row["damage_done"] for row in raid_rows)
            total_duration = sum(row["duration_secs"] for row in raid_rows)
            guild = deepcopy(raid_rows[0]["guild"])
            raids.append(
                {
                    "instance_id": instance_id,
                    "started_at": raid_rows[0]["started_at"],
                    "guild": guild,
                    "encounter_observation_count": len(raid_rows),
                    "local_peer_comparable_encounter_count": len(ratios),
                    "total_damage_done": total_damage,
                    "total_duration_secs": total_duration,
                    "descriptive_aggregate_dps": total_damage / total_duration,
                    "median_local_fury_dps_ratio": _median(ratios),
                }
            )
        raids.sort(key=lambda raid: (raid["started_at"], raid["instance_id"]))
        servers = {row["server"] for row in all_rows}
        realms = {row["realm"] for row in all_rows}
        if len(servers) != 1 or len(realms) != 1:
            raise HistoricalFuryExpertCohortError(
                "one exact character GUID has conflicting server or realm evidence"
            )
        names = sorted({str(row["character_name"]) for row in all_rows})
        latest = max(
            all_rows,
            key=lambda row: (row["started_at"], row["character_name"]),
        )
        guilds = sorted(
            {(_guild_key(row["guild"]), row["guild"]["id"], row["guild"]["name"])
             for row in all_rows},
            key=lambda value: value[0],
        )
        raid_ratios = [
            raid["median_local_fury_dps_ratio"]
            for raid in raids
            if raid["median_local_fury_dps_ratio"] is not None
        ]
        total_damage = sum(row["damage_done"] for row in all_rows)
        total_duration = sum(row["duration_secs"] for row in all_rows)
        candidate_id = _candidate_id(player_key)
        evidence_depth = (
            "REPEAT_RAID_IDENTITY_ANCHOR"
            if len(raids) >= 2
            else "SINGLE_RAID_RIGHT_CENSORED_IDENTITY_ANCHOR"
        )
        candidate = {
            "candidate_id": candidate_id,
            "character_guid": latest["character_guid"],
            "server": latest["server"],
            "realm": latest["realm"],
            "observed_character_names": names,
            "latest_character_name": latest["character_name"],
            "observed_guilds": [
                {"id": guild_id, "name": guild_name}
                for _, guild_id, guild_name in guilds
            ],
            "raid_count": len(raids),
            "encounter_observation_count": len(all_rows),
            "first_observed_started_at": raids[0]["started_at"],
            "last_observed_started_at": raids[-1]["started_at"],
            "total_damage_done": total_damage,
            "total_duration_secs": total_duration,
            "descriptive_aggregate_dps": total_damage / total_duration,
            "median_equal_raid_local_fury_dps_ratio": _median(raid_ratios),
            "performance_statistic_role": (
                "DESCRIPTIVE_ROUTING_EVIDENCE_ONLY_NOT_POLICY_COMPLETENESS"
            ),
            "evidence_depth": evidence_depth,
            "right_censored_after_last_observed_raid": True,
            "raids": raids,
        }
        player_candidates.append(candidate)
        for row in all_rows:
            guild_key = _guild_key(row["guild"])
            guild_to_players[guild_key].add(candidate_id)
            guild_to_raids[guild_key].add(row["instance_id"])
            guild_to_rows[guild_key] += 1
            raid_to_players[row["instance_id"]].add(candidate_id)

    raid_rows_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        raid_rows_map[row["instance_id"]].append(row)
    raid_strata = []
    for instance_id in sorted(raid_rows_map):
        group = raid_rows_map[instance_id]
        guilds = {_guild_key(row["guild"]) for row in group}
        starts = {row["started_at"] for row in group}
        if len(guilds) != 1 or len(starts) != 1:
            raise HistoricalFuryExpertCohortError(
                "one raid has conflicting guild or started_at evidence"
            )
        guild = group[0]["guild"]
        raid_strata.append(
            {
                "instance_id": instance_id,
                "started_at": group[0]["started_at"],
                "guild": deepcopy(guild),
                "player_candidate_count": len(raid_to_players[instance_id]),
                "encounter_observation_count": len(group),
            }
        )
    raid_strata.sort(key=lambda raid: (raid["started_at"], raid["instance_id"]))

    guild_strata = [
        {
            "guild": {"id": key[0] or None, "name": key[1]},
            "player_candidate_count": len(guild_to_players[key]),
            "raid_count": len(guild_to_raids[key]),
            "encounter_observation_count": guild_to_rows[key],
        }
        for key in sorted(guild_to_players)
    ]
    behavior_candidates = [
        {
            "prototype_candidate_id": f"behavior:{candidate['candidate_id']}",
            "player_candidate_id": candidate["candidate_id"],
            "prototype_family": (
                "PLAYER_CONDITIONED_REPEAT_RAID_CANDIDATE"
                if candidate["raid_count"] >= 2
                else "PLAYER_CONDITIONED_SINGLE_RAID_RIGHT_CENSORED_CANDIDATE"
            ),
            "identity_and_performance_evidence_available": True,
            "controllable_action_trace_status": ACTION_TRACE_MISSING,
            "queue_intent_status": QUEUE_UNKNOWN,
            "target_switch_intent_status": QUEUE_UNKNOWN,
            "right_censoring_status": (
                "CORPUS_RIGHT_CENSORED_AFTER_LAST_OBSERVED_RAID"
            ),
            "closed_loop_policy_training_eligible": False,
            "complete_expert_strategy_claim": COMPLETE_STRATEGY_REFUSED,
        }
        for candidate in player_candidates
    ]

    document = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "source_index": source,
        "frozen_cohort_contract": {
            "instance_name": FROZEN_INSTANCE_NAME,
            "spec": FROZEN_SPEC,
            "role": FROZEN_ROLE,
            "raid_date_field": "started_at",
            "started_at_not_before_local": cutoff_text,
            "timezone": boundary["timezone"],
            "contamination_rule": boundary,
            "accepted_contamination_labels": sorted(eligible_labels),
            "uploaded_at_used_for_date_split": False,
        },
        "deduplication_and_weighting_contract": {
            "source_observation_unique_key": [
                "character_guid",
                "instance_id",
                "ranking_record_id",
            ],
            "same_evidence_duplicate_policy": (
                "COLLAPSE_IDENTICAL_SOURCE_UNIQUE_KEY"
            ),
            "conflicting_duplicate_policy": "REJECT",
            "peer_comparison_stratum": (
                "INSTANCE_PLUS_ENCOUNTER_ID_OR_NULL_ID_TRASH_NAME_AND_KILLED_AT"
            ),
            "player_identity": ["character_guid"],
            "server_realm_role": (
                "DESCRIPTIVE_METADATA_MUST_BE_CONSISTENT_WITHIN_EXACT_GUID"
            ),
            "raid_identity": "instance_id",
            "player_summary_weighting": (
                "ONE_EQUAL_VOTE_PER_RAID_AFTER_WITHIN_RAID_MEDIAN"
            ),
            "guild_role": "EXPLICIT_STRATUM_NOT_A_POOLED_WEIGHT_MULTIPLIER",
            "candidate_order": "PLAYER_IDENTITY_NOT_PERFORMANCE_RANK",
        },
        "summary": {
            "source_exact_dps_row_count": source_count,
            "selected_exact_fury_encounter_observation_count": len(selected_rows),
            "same_evidence_duplicate_count_collapsed": collapsed_duplicates,
            "unique_player_candidate_count": len(player_candidates),
            "repeat_raid_player_candidate_count": sum(
                candidate["raid_count"] >= 2 for candidate in player_candidates
            ),
            "single_raid_right_censored_player_candidate_count": sum(
                candidate["raid_count"] == 1 for candidate in player_candidates
            ),
            "unique_player_raid_membership_count": sum(
                candidate["raid_count"] for candidate in player_candidates
            ),
            "unique_raid_count": len(raid_strata),
            "unique_guild_stratum_count": len(guild_strata),
            "local_peer_comparable_encounter_observation_count": sum(
                value is not None for value in local_ratios.values()
            ),
            "filtered_out_row_counts": dict(sorted(filtered_out.items())),
            "source_index_censored_membership_count": source[
                "source_censored_membership_count"
            ],
        },
        "player_candidates": player_candidates,
        "behavior_prototype_candidates": behavior_candidates,
        "strata": {"guilds": guild_strata, "raids": raid_strata},
        "uncertainty_and_use_boundaries": {
            "right_censoring": (
                "absence after a player's last observed raid is corpus censoring, "
                "not evidence that the player stopped or changed strategy"
            ),
            "source_missing_exact_memberships": (
                "the source index reports missing exact-DPS memberships, but its "
                "missing descriptor does not prove Fury spec; they are not silently "
                "converted into zero-DPS Fury rows"
            ),
            "queue_intent": QUEUE_UNKNOWN,
            "action_trace": ACTION_TRACE_MISSING,
            "future_behavior_backfill": False,
            "pooled_behavior_cloning_policy_authorized": False,
            "top_one_dps_as_complete_strategy_authorized": False,
            "closed_loop_baseline_or_comparison_authorized": False,
            "training_or_superiority_claim_authorized": False,
            "next_required_step": (
                "strict exact-GUID/instance and observation-window join to controllable "
                "action requests; nullable encounter IDs, START, GO, queue and "
                "target-switch uncertainty must remain separate"
            ),
        },
    }
    validate_cohort_document(document)
    return document


def validate_cohort_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate all internal v2 invariants without reading the source index."""

    if document.get("schema") != SCHEMA:
        raise HistoricalFuryExpertCohortError("cohort schema is unsupported")
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise HistoricalFuryExpertCohortError("cohort implementation revision differs")
    source = _mapping(document.get("source_index"), label="source_index")
    _text(source.get("content_sha256"), label="source_index.content_sha256")
    contract = _mapping(
        document.get("frozen_cohort_contract"), label="frozen_cohort_contract"
    )
    boundary = ingest_v1.range_bug_boundary_contract()
    if contract.get("started_at_not_before_local") != boundary[
        "postfix_known_clean_at_or_after_local"
    ]:
        raise HistoricalFuryExpertCohortError("cohort cutoff is not the frozen boundary")
    if contract.get("timezone") != boundary["timezone"]:
        raise HistoricalFuryExpertCohortError("cohort timezone is not the frozen boundary")
    if contract.get("uploaded_at_used_for_date_split") is not False:
        raise HistoricalFuryExpertCohortError("uploaded_at must not select the cohort")
    candidates = _array(document.get("player_candidates"), label="player_candidates")
    behavior = _array(
        document.get("behavior_prototype_candidates"),
        label="behavior_prototype_candidates",
    )
    summary = _mapping(document.get("summary"), label="summary")
    ids: set[str] = set()
    memberships = 0
    repeat = 0
    single = 0
    for raw in candidates:
        candidate = _mapping(raw, label="player candidate")
        candidate_id = _text(candidate.get("candidate_id"), label="candidate_id")
        assert candidate_id is not None
        if candidate_id in ids:
            raise HistoricalFuryExpertCohortError("duplicate player candidate_id")
        ids.add(candidate_id)
        raids = _array(candidate.get("raids"), label="candidate.raids")
        raid_count = _integer(candidate.get("raid_count"), label="candidate.raid_count", minimum=1)
        if raid_count != len(raids):
            raise HistoricalFuryExpertCohortError("candidate raid_count mismatch")
        if candidate.get("right_censored_after_last_observed_raid") is not True:
            raise HistoricalFuryExpertCohortError("candidate right censoring is missing")
        memberships += raid_count
        repeat += int(raid_count >= 2)
        single += int(raid_count == 1)
    if len(behavior) != len(candidates):
        raise HistoricalFuryExpertCohortError(
            "one behavior prototype candidate per player is required"
        )
    behavior_player_ids: set[str] = set()
    for raw in behavior:
        prototype = _mapping(raw, label="behavior prototype candidate")
        player_id = _text(
            prototype.get("player_candidate_id"), label="behavior.player_candidate_id"
        )
        assert player_id is not None
        if player_id not in ids or player_id in behavior_player_ids:
            raise HistoricalFuryExpertCohortError(
                "behavior prototype player reference is invalid or duplicate"
            )
        behavior_player_ids.add(player_id)
        if prototype.get("queue_intent_status") != QUEUE_UNKNOWN:
            raise HistoricalFuryExpertCohortError("queue uncertainty was overwritten")
        if prototype.get("controllable_action_trace_status") != ACTION_TRACE_MISSING:
            raise HistoricalFuryExpertCohortError("missing action trace was overwritten")
        if prototype.get("closed_loop_policy_training_eligible") is not False:
            raise HistoricalFuryExpertCohortError(
                "identity-only candidate cannot train a closed-loop policy"
            )
        if prototype.get("complete_expert_strategy_claim") != COMPLETE_STRATEGY_REFUSED:
            raise HistoricalFuryExpertCohortError("complete-strategy refusal is missing")
    expected = {
        "unique_player_candidate_count": len(candidates),
        "repeat_raid_player_candidate_count": repeat,
        "single_raid_right_censored_player_candidate_count": single,
        "unique_player_raid_membership_count": memberships,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise HistoricalFuryExpertCohortError(f"summary.{key} mismatch")
    boundaries = _mapping(
        document.get("uncertainty_and_use_boundaries"),
        label="uncertainty_and_use_boundaries",
    )
    for key in (
        "pooled_behavior_cloning_policy_authorized",
        "top_one_dps_as_complete_strategy_authorized",
        "closed_loop_baseline_or_comparison_authorized",
        "training_or_superiority_claim_authorized",
        "future_behavior_backfill",
    ):
        if boundaries.get(key) is not False:
            raise HistoricalFuryExpertCohortError(f"boundary {key} must be false")
    return deepcopy(dict(summary))


def _load_source_index_rows(
    path: str | Path,
    *,
    data_root: Path,
    zstd_executable: str | Path | None,
) -> tuple[dict[str, Any], Path, Iterator[dict[str, Any]]]:
    try:
        manifest, resolved = dps_index_v1.load_character_dps_index_manifest(
            path,
            data_root=data_root,
            zstd_executable=zstd_executable,
        )
    except dps_index_v1.CharacterDpsIndexError as error:
        raise HistoricalFuryExpertCohortError(str(error)) from error
    return (
        manifest,
        resolved,
        _iter_index_partition_rows(
            manifest,
            data_root=data_root,
            zstd_executable=zstd_executable,
        ),
    )


def load_frozen_cohort_selection(
    index_manifest_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> tuple[FrozenCohortSelection, Path]:
    """Replay the exact DPS index and return the shared frozen row selection."""

    root = _data_root(Path(data_root))
    manifest, resolved, rows = _load_source_index_rows(
        index_manifest_path,
        data_root=root,
        zstd_executable=zstd_executable,
    )
    selection = select_frozen_cohort_rows(
        manifest,
        rows,
        source_manifest_path=_relative_to_data_root(
            resolved, root, label="source index manifest"
        ),
    )
    return selection, resolved


def build_cohort_from_index(
    index_manifest_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    root = _data_root(Path(data_root))
    manifest, resolved, rows = _load_source_index_rows(
        index_manifest_path,
        data_root=root,
        zstd_executable=zstd_executable,
    )
    return build_cohort_document(
        manifest,
        rows,
        source_manifest_path=_relative_to_data_root(
            resolved, root, label="source index manifest"
        ),
    )


def publish_historical_fury_expert_cohort(
    index_manifest_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    output_path: str | Path | None = None,
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    root = _data_root(Path(data_root))
    document = build_cohort_from_index(
        index_manifest_path,
        data_root=root,
        zstd_executable=zstd_executable,
    )
    output = (
        root / OUTPUT_DIRECTORY / OUTPUT_FILENAME
        if output_path is None
        else Path(output_path).expanduser().resolve()
    )
    _relative_to_data_root(output, root, label="cohort output")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(document)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {
        "schema": SCHEMA,
        "status": "PUBLISHED_IDENTITY_AND_PERFORMANCE_COHORT_NOT_POLICY",
        "output_path": str(output.resolve()),
        "summary": deepcopy(document["summary"]),
    }


def audit_historical_fury_expert_cohort(
    cohort_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    root = _data_root(Path(data_root))
    path = Path(cohort_path).expanduser().resolve()
    _relative_to_data_root(path, root, label="cohort path")
    try:
        document = _strict_json(path.read_bytes(), label="cohort")
    except OSError as error:
        raise HistoricalFuryExpertCohortError(f"cannot read cohort: {error}") from error
    validate_cohort_document(document)
    source = _mapping(document.get("source_index"), label="source_index")
    source_relative = _text(source.get("path"), label="source_index.path")
    assert source_relative is not None
    relative_path = Path(source_relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HistoricalFuryExpertCohortError("source_index.path is unsafe")
    expected = build_cohort_from_index(
        root / relative_path,
        data_root=root,
        zstd_executable=zstd_executable,
    )
    if _canonical_bytes(document) != _canonical_bytes(expected):
        raise HistoricalFuryExpertCohortError(
            "cohort differs from deterministic source-index replay"
        )
    return {
        "schema": SCHEMA,
        "status": "PASS_DETERMINISTIC_SOURCE_INDEX_REPLAY",
        "cohort_path": str(path),
        "summary": deepcopy(document["summary"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build/audit the exact post-fix Fury expert candidate cohort v2"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--dps-index-manifest", type=Path, required=True)
    build.add_argument("--data-root", type=Path, default=history_v1.DEFAULT_DATA_ROOT)
    build.add_argument("--output", type=Path)
    build.add_argument("--zstd")
    audit = subparsers.add_parser("audit")
    audit.add_argument("--cohort", type=Path, required=True)
    audit.add_argument("--data-root", type=Path, default=history_v1.DEFAULT_DATA_ROOT)
    audit.add_argument("--zstd")
    return parser


def _write_output(
    value: Mapping[str, Any],
    *,
    stdout: TextIO | None,
    stdout_buffer: BinaryIO | None,
) -> None:
    payload = _canonical_bytes(value)
    if stdout is not None:
        if stdout_buffer is not None:
            raise HistoricalFuryExpertCohortError(
                "stdout and stdout_buffer are mutually exclusive"
            )
        stdout.write(payload.decode("utf-8"))
        return
    target = stdout_buffer if stdout_buffer is not None else getattr(sys.stdout, "buffer", None)
    if target is not None:
        target.write(payload)
    else:  # pragma: no cover
        sys.stdout.write(payload.decode("utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdout_buffer: BinaryIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        result = publish_historical_fury_expert_cohort(
            args.dps_index_manifest,
            data_root=args.data_root,
            output_path=args.output,
            zstd_executable=args.zstd,
        )
    else:
        result = audit_historical_fury_expert_cohort(
            args.cohort,
            data_root=args.data_root,
            zstd_executable=args.zstd,
        )
    _write_output(result, stdout=stdout, stdout_buffer=stdout_buffer)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalFuryExpertCohortError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)


__all__ = [
    "ACTION_TRACE_MISSING",
    "COMPLETE_STRATEGY_REFUSED",
    "FrozenCohortSelection",
    "HistoricalFuryExpertCohortError",
    "IMPLEMENTATION_REVISION",
    "KIND",
    "OUTPUT_DIRECTORY",
    "OUTPUT_FILENAME",
    "QUEUE_UNKNOWN",
    "SCHEMA",
    "audit_historical_fury_expert_cohort",
    "build_cohort_document",
    "build_cohort_from_index",
    "load_frozen_cohort_selection",
    "main",
    "publish_historical_fury_expert_cohort",
    "select_frozen_cohort_rows",
    "validate_cohort_document",
]
