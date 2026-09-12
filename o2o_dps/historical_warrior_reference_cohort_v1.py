"""Build a spec-preserving reference for the three user-named Warriors.

The exact Chronicle DPS index is sufficient to bind a name to one exact GUID
and to describe post-fix encounter windows.  The DPS index itself does not
contain requested actions, next-swing queue intent, or target-switch intent.  A
separate exact-GUID External-V2 timeline join can expose server-observed
START/GO/FAIL and the server-resolved target, but those events do not recover the
client request, queue intent, or target-switch intent.  Neither source supplies
a matched build/team counterfactual.  This artifact therefore remains an
identity, performance, and raid reference, not a Fury policy baseline or a
direct DPS win/loss lane.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence, TextIO

from . import chronicle_external_api_ingest_v1 as ingest_v1
from . import chronicle_external_character_dps_index_v1 as dps_index_v1
from . import chronicle_external_character_history_v1 as history_v1


SCHEMA = "historical_warrior_reference_cohort/v1"
IMPLEMENTATION_REVISION = (
    "historical_warrior_reference_cohort_v1.1_server_events_not_client_intents"
)
KIND = "historical_warrior_identity_performance_and_raid_reference"
CONFIG_SCHEMA = "historical_warrior_reference_players/v1"
CONFIG_REVISION = "historical_warrior_reference_players_v1.0_named_guid_pins"
OUTPUT_DIRECTORY = "derived/historical_warrior_reference_cohort/v1"
OUTPUT_FILENAME = "reference.json"
ACTION_TRACE_STATUS = (
    "NOT_IN_DPS_INDEX_SERVER_START_GO_FAIL_AVAILABLE_BY_SEPARATE_EXACT_GUID_"
    "EXTERNAL_V2_JOIN_NOT_CLIENT_REQUEST"
)
QUEUE_INTENT_STATUS = (
    "MISSING_IN_DPS_INDEX_AND_EXTERNAL_V2_START_GO_FAIL_DO_NOT_PROVE_CLIENT_"
    "QUEUE_INTENT"
)
TARGET_INTENT_STATUS = (
    "MISSING_IN_DPS_INDEX_AND_EXTERNAL_V2_RESOLVED_TARGET_DOES_NOT_PROVE_"
    "TARGET_SWITCH_INTENT"
)


class HistoricalWarriorReferenceError(RuntimeError):
    """The source, named-player config, or derived reference is inconsistent."""


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise HistoricalWarriorReferenceError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalWarriorReferenceError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HistoricalWarriorReferenceError(f"{label} must be non-empty text")
    return value.strip()


def _number(value: Any, *, label: str, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise HistoricalWarriorReferenceError(f"{label} must be finite")
    result = float(value)
    if result < 0 or (positive and result <= 0):
        raise HistoricalWarriorReferenceError(f"{label} is out of range")
    return result


def _timestamp(value: Any, *, label: str):
    text = _text(value, label=label)
    assert text is not None
    try:
        parsed = ingest_v1._parse_rfc3339(text, field=label)
    except ingest_v1.ChronicleIngestError as error:
        raise HistoricalWarriorReferenceError(str(error)) from error
    if parsed.year <= 1:
        raise HistoricalWarriorReferenceError(f"{label} is a missing timestamp")
    return text, parsed


def _canonical_bytes(value: Any) -> bytes:
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


def _load_json(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalWarriorReferenceError(f"cannot read {label}: {error}") from error
    return deepcopy(dict(_mapping(value, label=label)))


def _normalize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if config.get("schema") != CONFIG_SCHEMA or config.get(
        "implementation_revision"
    ) != CONFIG_REVISION:
        raise HistoricalWarriorReferenceError("named-player config is unsupported")
    boundary = ingest_v1.range_bug_boundary_contract()
    selection = _mapping(config.get("selection"), label="config.selection")
    if selection.get("identity_seed") != (
        "exact_character_name_then_pinned_exact_character_guid"
    ) or selection.get("identity_after_resolution") != "exact_character_guid_only":
        raise HistoricalWarriorReferenceError("config identity contract was weakened")
    if selection.get("raid_date_field") != "started_at":
        raise HistoricalWarriorReferenceError("config must select by started_at")
    if selection.get("started_at_not_before_local") != boundary[
        "postfix_known_clean_at_or_after_local"
    ]:
        raise HistoricalWarriorReferenceError("config post-fix cutoff is not current")
    if selection.get("accepted_contamination_labels") != [
        ingest_v1.POSTFIX_KNOWN_CLEAN
    ]:
        raise HistoricalWarriorReferenceError(
            "named South/North references must require POSTFIX_KNOWN_CLEAN"
        )
    if selection.get("pre_fix_rows") != (
        "COUNT_AS_EXCLUDED_EVIDENCE_NEVER_SELECT"
    ):
        raise HistoricalWarriorReferenceError("config pre-fix exclusion was weakened")
    requested = []
    seen_names: set[str] = set()
    seen_guids: set[str] = set()
    for index, raw in enumerate(_array(config.get("players"), label="config.players")):
        player = _mapping(raw, label=f"config.players[{index}]")
        name = _text(player.get("requested_name"), label="requested_name")
        guid = _text(
            player.get("expected_character_guid"), label="expected_character_guid"
        )
        purpose = _text(player.get("purpose"), label="purpose")
        assert name is not None and guid is not None and purpose is not None
        guid_key = guid.lower()
        if name in seen_names or guid_key in seen_guids:
            raise HistoricalWarriorReferenceError("configured names and GUIDs must be unique")
        seen_names.add(name)
        seen_guids.add(guid_key)
        requested.append(
            {
                "requested_name": name,
                "expected_character_guid": guid,
                "purpose": purpose,
            }
        )
    if not requested:
        raise HistoricalWarriorReferenceError("config.players must not be empty")
    boundaries = _mapping(config.get("use_boundaries"), label="config.use_boundaries")
    expected_boundaries = {
        "identity_and_raid_reference": True,
        "fury_policy_baseline": False,
        "action_trace_available": False,
        "queue_intent_available": False,
        "direct_same_spec_dps_win_loss": False,
    }
    if dict(boundaries) != expected_boundaries:
        raise HistoricalWarriorReferenceError("config use boundaries were weakened")
    instance_name = _text(config.get("instance_name"), label="config.instance_name")
    assert instance_name is not None
    return {
        "schema": CONFIG_SCHEMA,
        "implementation_revision": CONFIG_REVISION,
        "instance_name": instance_name,
        "selection": deepcopy(dict(selection)),
        "players": requested,
        "use_boundaries": expected_boundaries,
    }


def _guild(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = row.get("guild")
    if raw is None:
        return None
    value = _mapping(raw, label="row.guild")
    name = _text(value.get("name"), label="row.guild.name")
    guild_id = _text(value.get("id"), label="row.guild.id", allow_none=True)
    return {"id": guild_id, "name": name}


def _normalize_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        "character_guid": _text(raw.get("character_guid"), label="character_guid"),
        "character_name": _text(raw.get("character_name"), label="character_name"),
        "server": _text(raw.get("server"), label="server", allow_none=True),
        "realm": _text(raw.get("realm"), label="realm", allow_none=True),
        "instance_id": _text(raw.get("instance_id"), label="instance_id"),
        "instance_name": _text(raw.get("instance_name"), label="instance_name"),
        "started_at": _text(raw.get("started_at"), label="started_at"),
        "uploaded_at": _text(raw.get("uploaded_at"), label="uploaded_at"),
        "guild": _guild(raw),
        "contamination_label": _text(
            raw.get("contamination_label"), label="contamination_label"
        ),
        "encounter_id": _text(
            raw.get("encounter_id"), label="encounter_id", allow_none=True
        ),
        "encounter_name": _text(raw.get("encounter_name"), label="encounter_name"),
        "killed_at": _text(raw.get("killed_at"), label="killed_at"),
        "damage_done": _number(raw.get("damage_done"), label="damage_done"),
        "duration_secs": _number(
            raw.get("duration_secs"), label="duration_secs", positive=True
        ),
        "dps": _number(raw.get("dps"), label="dps"),
        "spec": _text(raw.get("spec"), label="spec"),
        "role": _text(raw.get("role"), label="role"),
        "ranking_record_id": _text(
            raw.get("ranking_record_id"), label="ranking_record_id"
        ),
    }
    expected_dps = row["damage_done"] / row["duration_secs"]
    if not math.isclose(row["dps"], expected_dps, rel_tol=1e-9, abs_tol=1e-9):
        raise HistoricalWarriorReferenceError(
            "row.dps disagrees with damage_done / duration_secs"
        )
    return row


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row["character_guid"]).lower(),
        str(row["instance_id"]),
        str(row["ranking_record_id"]),
    )


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    damage = sum(float(row["damage_done"]) for row in rows)
    duration = sum(float(row["duration_secs"]) for row in rows)
    return {
        "encounter_observation_count": len(rows),
        "total_damage_done_over_observed_windows": damage,
        "total_duration_secs_over_observed_windows": duration,
        "descriptive_observed_window_weighted_dps": damage / duration,
    }


def _build_player_reference(
    configured: Mapping[str, Any],
    all_rows: Sequence[Mapping[str, Any]],
    selected_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    guid = str(configured["expected_character_guid"])
    selected_by_raid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        selected_by_raid[str(row["instance_id"])].append(row)
    raids = []
    for instance_id, raid_rows in sorted(selected_by_raid.items()):
        ordered = sorted(
            raid_rows,
            key=lambda row: (
                str(row["killed_at"]),
                str(row["encounter_name"]),
                str(row["ranking_record_id"]),
            ),
        )
        started = sorted({str(row["started_at"]) for row in ordered})
        guilds = sorted(
            {
                (str(row["guild"].get("id") or ""), str(row["guild"]["name"]))
                for row in ordered
            }
        )
        if len(started) != 1 or len(guilds) != 1:
            raise HistoricalWarriorReferenceError(
                "one exact-GUID raid has conflicting started_at or guild evidence"
            )
        raid = {
            "instance_id": instance_id,
            "started_at": started[0],
            "uploaded_at_values": sorted({str(row["uploaded_at"]) for row in ordered}),
            "guild": {"id": guilds[0][0] or None, "name": guilds[0][1]},
            "observed_character_names": sorted(
                {str(row["character_name"]) for row in ordered}
            ),
            "specs": sorted({str(row["spec"]) for row in ordered}),
            "roles": sorted({str(row["role"]) for row in ordered}),
            "contamination_label": ingest_v1.POSTFIX_KNOWN_CLEAN,
            **_aggregate(ordered),
            "encounters": [
                {
                    "encounter_id": row["encounter_id"],
                    "encounter_name": row["encounter_name"],
                    "killed_at": row["killed_at"],
                    "spec": row["spec"],
                    "role": row["role"],
                    "damage_done": row["damage_done"],
                    "duration_secs": row["duration_secs"],
                    "dps": row["dps"],
                    "ranking_record_id": row["ranking_record_id"],
                }
                for row in ordered
            ],
        }
        raids.append(raid)

    by_spec_role: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in selected_rows:
        by_spec_role[(str(row["spec"]), str(row["role"]))].append(row)
    spec_role = [
        {
            "spec": key[0],
            "role": key[1],
            "raid_count": len({str(row["instance_id"]) for row in group}),
            **_aggregate(group),
        }
        for key, group in sorted(by_spec_role.items())
    ]
    excluded_by_label: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_rows:
        if row not in selected_rows:
            excluded_by_label[str(row["contamination_label"])].append(row)
    return {
        "reference_id": f"warrior:{guid.lower()}",
        "requested_name": configured["requested_name"],
        "character_guid": guid,
        "purpose": configured["purpose"],
        "identity": {
            "resolution": "EXACT_CONFIGURED_NAME_TO_ONE_PINNED_GUID_THEN_GUID_ONLY",
            "observed_character_names": sorted(
                {str(row["character_name"]) for row in all_rows}
            ),
            "servers": sorted(
                {str(row["server"]) for row in all_rows if row["server"] is not None}
            ),
            "realms": sorted(
                {str(row["realm"]) for row in all_rows if row["realm"] is not None}
            ),
        },
        "selected_specs": sorted({str(row["spec"]) for row in selected_rows}),
        "selected_roles": sorted({str(row["role"]) for row in selected_rows}),
        "selected_postfix_known_clean_raid_count": len(raids),
        "selected_postfix_known_clean_observation_count": len(selected_rows),
        "excluded_observations_by_contamination_label": {
            label: len(group) for label, group in sorted(excluded_by_label.items())
        },
        "excluded_raids_by_contamination_label": {
            label: len({str(row["instance_id"]) for row in group})
            for label, group in sorted(excluded_by_label.items())
        },
        "performance_by_spec_and_role": spec_role,
        "raids": raids,
        "reference_boundaries": {
            "identity_performance_and_raid_reference": True,
            "fury_policy_baseline": False,
            "controllable_action_trace_status": ACTION_TRACE_STATUS,
            "next_swing_queue_intent_status": QUEUE_INTENT_STATUS,
            "target_switch_intent_status": TARGET_INTENT_STATUS,
            "direct_same_spec_dps_win_loss_eligible": False,
            "reason": (
                "the DPS index has no action request, client queue intent, or "
                "target-switch intent; the local External-V2 timeline separately "
                "provides exact-GUID server-observed START/GO/FAIL and resolved "
                "target observations, which do not expose those client intents; "
                "neither source is a matched build/team policy counterfactual"
            ),
        },
    }


def build_reference_document(
    source_manifest: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    source_manifest_path: str,
    config_path: str,
) -> dict[str, Any]:
    normalized_config = _normalize_config(config)
    if source_manifest.get("schema") != dps_index_v1.SCHEMA or source_manifest.get(
        "implementation_revision"
    ) != dps_index_v1.IMPLEMENTATION_REVISION:
        raise HistoricalWarriorReferenceError("source exact DPS index is unsupported")
    source_summary = _mapping(source_manifest.get("summary"), label="source.summary")
    partition = _mapping(source_manifest.get("partition"), label="source.partition")
    expected_count = partition.get("record_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise HistoricalWarriorReferenceError("source partition count is invalid")
    source_content = _mapping(
        source_manifest.get("content_address"), label="source.content_address"
    )
    source_rows = []
    seen_keys: set[tuple[str, str, str]] = set()
    name_guids: dict[str, set[str]] = defaultdict(set)
    requested_names = {
        str(player["requested_name"]) for player in normalized_config["players"]
    }
    requested_guids = {
        str(player["expected_character_guid"]).lower()
        for player in normalized_config["players"]
    }
    count = 0
    for raw in rows:
        count += 1
        row = _normalize_row(_mapping(raw, label="DPS row"))
        key = _row_key(row)
        if key in seen_keys:
            raise HistoricalWarriorReferenceError("exact DPS unique key is duplicated")
        seen_keys.add(key)
        if row["character_name"] in requested_names:
            name_guids[str(row["character_name"])].add(key[0])
        if key[0] in requested_guids:
            source_rows.append(row)
    if count != expected_count:
        raise HistoricalWarriorReferenceError(
            "streamed DPS row count disagrees with source partition"
        )

    boundary = ingest_v1.range_bug_boundary_contract()
    cutoff_text = str(boundary["postfix_known_clean_at_or_after_local"])
    _, cutoff = _timestamp(cutoff_text, label="post-fix cutoff")
    players = []
    for configured in normalized_config["players"]:
        name = str(configured["requested_name"])
        guid = str(configured["expected_character_guid"]).lower()
        resolved = name_guids.get(name, set())
        if resolved != {guid}:
            raise HistoricalWarriorReferenceError(
                f"configured name {name!r} does not resolve to exactly pinned GUID {guid}"
            )
        player_rows = [
            row
            for row in source_rows
            if str(row["character_guid"]).lower() == guid
            and row["instance_name"] == normalized_config["instance_name"]
        ]
        selected = []
        for row in player_rows:
            started_text, started = _timestamp(row["started_at"], label="row.started_at")
            guild_name = row["guild"]["name"] if row["guild"] is not None else None
            expected_label = ingest_v1.classify_range_bug(guild_name, started_text)
            if row["contamination_label"] != expected_label:
                raise HistoricalWarriorReferenceError(
                    "DPS row contamination_label disagrees with guild + started_at"
                )
            if (
                started >= cutoff
                and row["contamination_label"] == ingest_v1.POSTFIX_KNOWN_CLEAN
            ):
                selected.append(row)
        if not selected:
            raise HistoricalWarriorReferenceError(
                f"configured player {name!r} has no post-fix clean reference rows"
            )
        players.append(_build_player_reference(configured, player_rows, selected))

    document = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "source_exact_dps_index": {
            "schema": source_manifest["schema"],
            "implementation_revision": source_manifest["implementation_revision"],
            "content_sha256": _text(
                source_content.get("sha256"), label="source content sha256"
            ),
            "path": source_manifest_path,
            "partition_record_count": expected_count,
            "training_eligible_exact_membership_count": source_summary.get(
                "training_eligible_exact_membership_count"
            ),
        },
        "named_player_config": {
            "schema": CONFIG_SCHEMA,
            "implementation_revision": CONFIG_REVISION,
            "path": config_path,
        },
        "selection_contract": {
            "instance_name": normalized_config["instance_name"],
            "identity_seed": normalized_config["selection"]["identity_seed"],
            "identity_after_resolution": "exact_character_guid_only",
            "raid_date_field": "started_at",
            "uploaded_at_used_for_date_split": False,
            "started_at_not_before_local": cutoff_text,
            "timezone": boundary["timezone"],
            "accepted_contamination_labels": [ingest_v1.POSTFIX_KNOWN_CLEAN],
            "pre_fix_policy": "COUNT_AS_EXCLUDED_EVIDENCE_NEVER_SELECT",
            "spec_policy": "PRESERVE_SOURCE_SPEC_NEVER_COERCE_TO_FURY",
        },
        "summary": {
            "configured_player_count": len(players),
            "selected_player_count": len(players),
            "selected_raid_membership_count": sum(
                player["selected_postfix_known_clean_raid_count"] for player in players
            ),
            "selected_encounter_observation_count": sum(
                player["selected_postfix_known_clean_observation_count"]
                for player in players
            ),
            "selected_specs": sorted(
                {spec for player in players for spec in player["selected_specs"]}
            ),
            "excluded_suspect_observation_count": sum(
                player["excluded_observations_by_contamination_label"].get(
                    ingest_v1.SUSPECT_36YD_RANGE_BUG, 0
                )
                for player in players
            ),
        },
        "players": players,
        "scientific_use_boundaries": {
            "historical_identity_performance_and_raid_reference": True,
            "fury_policy_baseline": False,
            "action_trace_available": False,
            "queue_intent_available": False,
            "target_intent_available": False,
            "direct_same_spec_dps_win_loss_eligible": False,
            "training_eligible": False,
            "superiority_claim_eligible": False,
        },
    }
    validate_reference_document(document)
    return document


def validate_reference_document(document: Mapping[str, Any]) -> dict[str, Any]:
    if document.get("schema") != SCHEMA or document.get(
        "implementation_revision"
    ) != IMPLEMENTATION_REVISION:
        raise HistoricalWarriorReferenceError("reference document is unsupported")
    contract = _mapping(document.get("selection_contract"), label="selection_contract")
    boundary = ingest_v1.range_bug_boundary_contract()
    if contract.get("started_at_not_before_local") != boundary[
        "postfix_known_clean_at_or_after_local"
    ] or contract.get("accepted_contamination_labels") != [
        ingest_v1.POSTFIX_KNOWN_CLEAN
    ]:
        raise HistoricalWarriorReferenceError("post-fix clean selection was weakened")
    if contract.get("spec_policy") != "PRESERVE_SOURCE_SPEC_NEVER_COERCE_TO_FURY":
        raise HistoricalWarriorReferenceError("source spec preservation is missing")
    players = _array(document.get("players"), label="players")
    ids: set[str] = set()
    observations = 0
    memberships = 0
    for raw in players:
        player = _mapping(raw, label="player")
        reference_id = _text(player.get("reference_id"), label="reference_id")
        if reference_id in ids:
            raise HistoricalWarriorReferenceError("duplicate reference_id")
        ids.add(str(reference_id))
        raids = _array(player.get("raids"), label="player.raids")
        if player.get("selected_postfix_known_clean_raid_count") != len(raids):
            raise HistoricalWarriorReferenceError("player clean raid count mismatch")
        memberships += len(raids)
        for raid in raids:
            raid_map = _mapping(raid, label="player.raid")
            if raid_map.get("contamination_label") != ingest_v1.POSTFIX_KNOWN_CLEAN:
                raise HistoricalWarriorReferenceError("non-clean raid entered reference")
            encounters = _array(raid_map.get("encounters"), label="raid.encounters")
            if raid_map.get("encounter_observation_count") != len(encounters):
                raise HistoricalWarriorReferenceError("raid observation count mismatch")
            observations += len(encounters)
        reference_boundaries = _mapping(
            player.get("reference_boundaries"), label="player.reference_boundaries"
        )
        if (
            reference_boundaries.get("fury_policy_baseline") is not False
            or reference_boundaries.get("direct_same_spec_dps_win_loss_eligible")
            is not False
            or reference_boundaries.get("controllable_action_trace_status")
            != ACTION_TRACE_STATUS
            or reference_boundaries.get("next_swing_queue_intent_status")
            != QUEUE_INTENT_STATUS
            or reference_boundaries.get("target_switch_intent_status")
            != TARGET_INTENT_STATUS
        ):
            raise HistoricalWarriorReferenceError("player policy boundary was weakened")
    summary = _mapping(document.get("summary"), label="summary")
    expected = {
        "configured_player_count": len(players),
        "selected_player_count": len(players),
        "selected_raid_membership_count": memberships,
        "selected_encounter_observation_count": observations,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise HistoricalWarriorReferenceError(f"summary.{key} mismatch")
    scientific = _mapping(
        document.get("scientific_use_boundaries"), label="scientific_use_boundaries"
    )
    if scientific.get("historical_identity_performance_and_raid_reference") is not True:
        raise HistoricalWarriorReferenceError("reference use was removed")
    for key in (
        "fury_policy_baseline",
        "action_trace_available",
        "queue_intent_available",
        "target_intent_available",
        "direct_same_spec_dps_win_loss_eligible",
        "training_eligible",
        "superiority_claim_eligible",
    ):
        if scientific.get(key) is not False:
            raise HistoricalWarriorReferenceError(f"scientific boundary {key} must be false")
    return deepcopy(dict(summary))


def _data_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.lower() for part in resolved.parts}:
        raise HistoricalWarriorReferenceError("data_root must be inside offline_data")
    return resolved


def _relative(path: Path, root: Path, *, label: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise HistoricalWarriorReferenceError(f"{label} must stay inside data_root") from error


def _iter_partition_rows(
    manifest: Mapping[str, Any], *, data_root: Path, zstd_executable: str | None
) -> Iterator[dict[str, Any]]:
    partition = _mapping(manifest.get("partition"), label="source.partition")
    relative = _text(partition.get("path"), label="source.partition.path")
    assert relative is not None
    path = (data_root / relative).resolve()
    _relative(path, data_root, label="source partition")
    executable = dps_index_v1._zstd_executable(zstd_executable)
    for line_number, raw in enumerate(
        dps_index_v1._iter_zstd_lines(path, zstd_executable=executable), start=1
    ):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HistoricalWarriorReferenceError(
                f"cannot decode DPS index row {line_number}: {error}"
            ) from error
        yield deepcopy(dict(_mapping(value, label=f"DPS index row {line_number}")))


def build_reference_from_index(
    index_manifest_path: str | Path,
    config_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | None = None,
) -> dict[str, Any]:
    root = _data_root(Path(data_root))
    try:
        manifest, resolved = dps_index_v1.load_character_dps_index_manifest(
            index_manifest_path, data_root=root, zstd_executable=zstd_executable
        )
    except dps_index_v1.CharacterDpsIndexError as error:
        raise HistoricalWarriorReferenceError(str(error)) from error
    config = _load_json(config_path, label="named-player config")
    return build_reference_document(
        manifest,
        _iter_partition_rows(
            manifest, data_root=root, zstd_executable=zstd_executable
        ),
        config,
        source_manifest_path=_relative(resolved, root, label="source manifest"),
        config_path=Path(config_path).as_posix(),
    )


def publish_reference(
    index_manifest_path: str | Path,
    config_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    output_path: str | Path | None = None,
    zstd_executable: str | None = None,
) -> dict[str, Any]:
    root = _data_root(Path(data_root))
    document = build_reference_from_index(
        index_manifest_path,
        config_path,
        data_root=root,
        zstd_executable=zstd_executable,
    )
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else root / OUTPUT_DIRECTORY / OUTPUT_FILENAME
    )
    _relative(destination, root, label="output")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(document)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "schema": SCHEMA,
        "status": "PUBLISHED_REFERENCE_ONLY_NOT_POLICY_OR_DPS_COMPARISON",
        "output_path": str(destination),
        "summary": deepcopy(document["summary"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the named post-fix historical Warrior reference"
    )
    parser.add_argument("--dps-index-manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=history_v1.DEFAULT_DATA_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--zstd")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdout_buffer: BinaryIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    result = publish_reference(
        args.dps_index_manifest,
        args.config,
        data_root=args.data_root,
        output_path=args.output,
        zstd_executable=args.zstd,
    )
    payload = _canonical_bytes(result)
    if stdout is not None:
        stdout.write(payload.decode("utf-8"))
    elif stdout_buffer is not None:
        stdout_buffer.write(payload)
    else:
        sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalWarriorReferenceError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)


__all__ = [
    "ACTION_TRACE_STATUS",
    "CONFIG_REVISION",
    "CONFIG_SCHEMA",
    "HistoricalWarriorReferenceError",
    "IMPLEMENTATION_REVISION",
    "KIND",
    "OUTPUT_DIRECTORY",
    "OUTPUT_FILENAME",
    "QUEUE_INTENT_STATUS",
    "SCHEMA",
    "TARGET_INTENT_STATUS",
    "build_reference_document",
    "build_reference_from_index",
    "main",
    "publish_reference",
    "validate_reference_document",
]
