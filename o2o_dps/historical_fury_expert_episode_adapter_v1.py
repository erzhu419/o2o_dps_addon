"""Join the frozen exact-DPS Fury cohort to External-V2 action observations.

One non-null exact DPS row becomes one encounter-window observation episode.
The DPS window is checked against the content-bound Chronicle metadata
encounter window.  Reconstruction waves are observations inside that window;
their envelope may omit pre-boundary or post-boundary events and is therefore
required to be a subset, not an exact copy, of the metadata window.  Null
encounter IDs remain unresolved and are never guessed.

START is only a server-observed controllable-action proxy.  GO and FAIL are
outcomes, and no event is treated as client request, next-swing queue intent,
or target-switch intent.  The artifact is historical observation evidence; it
does not authorize a baseline comparison, training, or a superiority claim.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence, TextIO

from . import chronicle_external_api_ingest_v1 as ingest_v1
from . import chronicle_external_historical_fury_policy_v3 as policy_v3
from . import chronicle_external_team_timeline_v2 as timeline_v2
from . import historical_fury_expert_cohort_v2 as cohort_v2
from . import historical_named_warrior_episode_adapter_v1 as named_v1


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_expert_observation_episode/v1"
MANIFEST_SCHEMA = "historical_fury_expert_episode_manifest/v1"
IMPLEMENTATION_REVISION = (
    "v1.2_metadata_window_all_wave_gaps_fury_v3_start_proxy"
)
STATUS = "HISTORICAL_FURY_OBSERVATION_ONLY_NOT_COMPARISON"
NULL_ENCOUNTER_STATUS = "UNRESOLVED_NULL_ENCOUNTER_ID_NOT_GUESSED"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_COHORT = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_expert_cohort"
    / "v2"
    / "cohort.json"
)
DEFAULT_TIMELINE_MANIFEST = named_v1.DEFAULT_TIMELINE_MANIFEST
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_expert_episodes"
    / "v1"
)

_FURY_SPEC_BY_ID = {
    spell_id: spec
    for spec in policy_v3.ACTION_ONTOLOGY
    for spell_id in spec.spell_ids
}
_FURY_SPEC_BY_ALIAS = {
    alias: spec
    for spec in policy_v3.ACTION_ONTOLOGY
    for alias in spec.name_aliases
}


class HistoricalFuryExpertEpisodeError(RuntimeError):
    """The frozen-source, exact-window, or observation contract is violated."""


@dataclass(frozen=True)
class EpisodeBuildResult:
    manifest: Path
    content_addressed_manifest: Path
    partition_count: int
    selected_observation_count: int
    candidate_episode_count: int
    unresolved_null_encounter_count: int
    controllable_policy_label_count: int

    def as_dict(self) -> JSONMap:
        return {
            "schema": MANIFEST_SCHEMA,
            "status": STATUS,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "partition_count": self.partition_count,
            "selected_observation_count": self.selected_observation_count,
            "candidate_episode_count": self.candidate_episode_count,
            "unresolved_null_encounter_count": self.unresolved_null_encounter_count,
            "controllable_policy_label_count": self.controllable_policy_label_count,
        }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFuryExpertEpisodeError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFuryExpertEpisodeError(f"{label} must be an array")
    return value


def _text(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HistoricalFuryExpertEpisodeError(f"{label} must be nonempty text")
    return value.strip()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalFuryExpertEpisodeError(f"{label} must be an integer")
    return value


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalFuryExpertEpisodeError(f"cannot read {label}: {error}") from error
    return deepcopy(dict(_mapping(value, label)))


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFuryExpertEpisodeError(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalFuryExpertEpisodeError(f"cannot read {path}: {error}") from error
    return digest.hexdigest()


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
        },
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _under_offline_data(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in resolved.parts}:
        raise HistoricalFuryExpertEpisodeError(
            "generated episode output must stay under ignored offline_data"
        )
    return resolved


def _spell_parts(spell: Mapping[str, Any]) -> tuple[int | None, str | None]:
    raw_id = spell.get("id")
    spell_id = (
        raw_id
        if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0
        else None
    )
    raw_name = spell.get("name")
    name = raw_name.strip().casefold() if isinstance(raw_name, str) and raw_name.strip() else None
    return spell_id, name


def fury_action_spec(spell: Mapping[str, Any]) -> policy_v3.ActionSpec | None:
    """Resolve only the frozen historical Fury v3 15-action ontology."""

    spell_id, name = _spell_parts(spell)
    if spell_id is not None:
        return _FURY_SPEC_BY_ID.get(spell_id)
    return _FURY_SPEC_BY_ALIAS.get(name) if name is not None else None


def classify_fury_action(event: Mapping[str, Any]) -> JSONMap:
    """Classify START as a proxy label while keeping GO/FAIL outcome-only."""

    phase = str(event.get("event_type") or "").upper()
    if phase not in named_v1.ACTION_EVENT_TYPES:
        raise HistoricalFuryExpertEpisodeError(f"unsupported action phase {phase!r}")
    spell = _mapping(event.get("spell"), "event.spell")
    spec = fury_action_spec(spell)
    spell_id, name = _spell_parts(spell)
    if spec is None:
        if spell_id is not None:
            action_key = f"unmapped.spell_id.{spell_id}"
        elif name is not None:
            action_key = f"unmapped.spell_name.{name}"
        else:
            action_key = "unmapped.no_spell_identity"
        lane = "unmapped"
        ontology_status = "OUTSIDE_HISTORICAL_FURY_V3_ONTOLOGY_PRESERVED"
    else:
        action_key = spec.action_key
        lane = spec.lane
        ontology_status = "KNOWN_HISTORICAL_FURY_V3_CONTROLLABLE_ACTION"
    policy_label = phase == "START" and spec is not None
    if phase == "START":
        role = (
            "SERVER_OBSERVED_START_CONTROLLABLE_ACTION_PROXY"
            if policy_label
            else "SERVER_OBSERVED_START_UNCLASSIFIED_OBSERVATION"
        )
    else:
        role = "ACTION_RESULT_GO" if phase == "GO" else "ACTION_RESULT_FAIL"
    return {
        "phase": phase,
        "learning_role": role,
        "action_key": action_key,
        "action_lane": lane,
        "ontology_status": ontology_status,
        "server_observed_start_proxy": phase == "START",
        "client_action_request_observed": False,
        "client_next_swing_queue_intent_observed": False,
        "policy_decision_label": policy_label,
    }


def _epoch_milliseconds(value: str, label: str) -> int:
    try:
        parsed = ingest_v1._parse_rfc3339(value, field=label)
    except ingest_v1.ChronicleIngestError as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    utc = parsed.astimezone(timezone.utc)
    delta = utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
    microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if microseconds % 1000:
        raise HistoricalFuryExpertEpisodeError(f"{label} is not millisecond aligned")
    return microseconds // 1000


def _dps_window(row: Mapping[str, Any]) -> JSONMap:
    killed_at = _text(row.get("killed_at"), "DPS row killed_at")
    assert killed_at is not None
    killed_ms = _epoch_milliseconds(killed_at, "DPS row killed_at")
    try:
        duration_ms_decimal = Decimal(str(row.get("duration_secs"))) * Decimal(1000)
    except (InvalidOperation, ValueError) as error:
        raise HistoricalFuryExpertEpisodeError("DPS duration is invalid") from error
    if duration_ms_decimal != duration_ms_decimal.to_integral_value():
        raise HistoricalFuryExpertEpisodeError("DPS duration is not millisecond aligned")
    duration_ms = int(duration_ms_decimal)
    if duration_ms <= 0:
        raise HistoricalFuryExpertEpisodeError("DPS duration must be positive")
    return {
        "first_ms": killed_ms - duration_ms,
        "last_ms": killed_ms,
        "duration_ms": duration_ms,
        "killed_at": killed_at,
    }


def _timeline_group_window(waves: Sequence[Mapping[str, Any]]) -> JSONMap:
    if not waves:
        raise HistoricalFuryExpertEpisodeError("timeline encounter group is empty")
    windows: list[JSONMap] = []
    for wave in waves:
        binding = _mapping(wave.get("reconstruction_binding"), "reconstruction_binding")
        window = _mapping(binding.get("window"), "reconstruction window")
        first = _mapping(window.get("first_anchor"), "window.first_anchor")
        last = _mapping(window.get("last_context_anchor"), "window.last_context_anchor")
        first_ms = _integer(first.get("timestamp_ms"), "first_anchor.timestamp_ms")
        last_ms = _integer(last.get("timestamp_ms"), "last_context_anchor.timestamp_ms")
        windows.append(
            {
                "wave_id": wave.get("wave_id"),
                "wave_ordinal": wave.get("wave_ordinal"),
                "first_ms": first_ms,
                "last_ms": last_ms,
            }
        )
    windows.sort(
        key=lambda row: (
            int(row["first_ms"]),
            int(row["last_ms"]),
            str(row.get("wave_id") or ""),
        )
    )
    interior_gaps: list[JSONMap] = []
    covered_through = int(windows[0]["last_ms"])
    covered_through_wave_id = windows[0].get("wave_id")
    for window in windows[1:]:
        first_ms = int(window["first_ms"])
        last_ms = int(window["last_ms"])
        if first_ms > covered_through:
            interior_gaps.append(
                {
                    "after_wave_id": covered_through_wave_id,
                    "before_wave_id": window.get("wave_id"),
                    "previous_coverage_end_ms": covered_through,
                    "next_coverage_start_ms": first_ms,
                    "unobserved_ms": first_ms - covered_through,
                }
            )
        if last_ms > covered_through:
            covered_through = last_ms
            covered_through_wave_id = window.get("wave_id")
    return {
        "first_ms": int(windows[0]["first_ms"]),
        "last_ms": max(int(window["last_ms"]) for window in windows),
        "interior_gaps": interior_gaps,
        "interior_gap_count": len(interior_gaps),
        "interior_unobserved_ms": sum(
            int(gap["unobserved_ms"]) for gap in interior_gaps
        ),
    }


def _player_row(wave: Mapping[str, Any], guid: str) -> Mapping[str, Any]:
    key = guid.casefold()
    matches = []
    for raw in _array(wave.get("players"), "wave.players"):
        row = _mapping(raw, "wave player")
        player = _mapping(row.get("player"), "wave player identity")
        if str(player.get("guid") or "").casefold() == key:
            matches.append(row)
    if len(matches) != 1:
        raise HistoricalFuryExpertEpisodeError(
            "exact DPS player GUID is absent or duplicated in timeline wave"
        )
    player = _mapping(matches[0].get("player"), "wave player identity")
    if str(player.get("class") or "").upper() != "WARRIOR":
        raise HistoricalFuryExpertEpisodeError("exact DPS Fury row joined a non-Warrior")
    return matches[0]


def _timeline_spec_assessment(player_rows: Sequence[Mapping[str, Any]]) -> JSONMap:
    evidence_rows = []
    status_counts: Counter[str] = Counter()
    explicit_fury = 0
    unknown = 0
    for row in player_rows:
        evidence = _mapping(row.get("warrior_spec_evidence"), "warrior_spec_evidence")
        if evidence.get("inference_used") is not False:
            raise HistoricalFuryExpertEpisodeError("timeline Warrior spec used inference")
        status = str(evidence.get("status") or "MISSING")
        player_spec = evidence.get("player_spec")
        conflicts = evidence.get("field_conflicts")
        has_conflict = isinstance(conflicts, Mapping) and bool(conflicts)
        clean_observed = (
            status == "OBSERVED"
            and evidence.get("exact_player_guid_match") is True
            and not has_conflict
        )
        if clean_observed and player_spec != "Fury":
            raise HistoricalFuryExpertEpisodeError(
                "exact DPS Fury window conflicts with explicit conflict-free timeline non-Fury spec"
            )
        if clean_observed and player_spec == "Fury":
            explicit_fury += 1
            category = "EXPLICIT_CONFLICT_FREE_FURY"
        else:
            unknown += 1
            category = "UNKNOWN_OR_CONFLICT_PRESERVED_NONVOTING"
        status_counts[category] += 1
        evidence_rows.append(deepcopy(dict(evidence)))
    return {
        "window_level_spec_source": "EXACT_DPS_RANKING_ROW",
        "window_level_spec": "Fury",
        "timeline_evidence_role": "CORROBORATION_OR_RECORDED_UNCERTAINTY_ONLY",
        "explicit_conflict_free_fury_wave_count": explicit_fury,
        "unknown_or_conflicting_timeline_wave_count": unknown,
        "status_counts": dict(sorted(status_counts.items())),
        "per_wave_evidence": evidence_rows,
    }


def _aggregate_wave_summaries(waves: Sequence[Mapping[str, Any]]) -> JSONMap:
    totals = Counter()
    phases: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    unclassified: Counter[str] = Counter()
    for wave in waves:
        summary = _mapping(wave.get("summary"), "wave observation summary")
        for key in (
            "direct_source_event_count",
            "excluded_non_direct_attribution_event_count",
            "transition_count",
            "server_observed_start_count",
            "controllable_policy_label_count",
            "unclassified_start_count",
            "go_outcome_count",
            "fail_outcome_count",
        ):
            totals[key] += int(summary.get(key, 0))
        phases.update(_mapping(summary.get("phase_counts"), "phase_counts"))
        actions.update(_mapping(summary.get("action_event_counts"), "action_event_counts"))
        labels.update(
            _mapping(summary.get("controllable_label_action_counts"), "label counts")
        )
        unclassified.update(
            _mapping(summary.get("unclassified_start_action_counts"), "unclassified counts")
        )
    return {
        "wave_count": len(waves),
        **dict(totals),
        "phase_counts": dict(sorted(phases.items())),
        "action_event_counts": dict(sorted(actions.items())),
        "controllable_label_action_counts": dict(sorted(labels.items())),
        "unclassified_start_action_counts": dict(sorted(unclassified.items())),
    }


def _build_observation_episode(
    row: Mapping[str, Any],
    waves: Sequence[Mapping[str, Any]],
    metadata_window: Mapping[str, Any],
) -> JSONMap:
    guid = str(row["character_guid"])
    encounter_id = str(row["encounter_id"])
    expected_window = _dps_window(row)
    observed_window = _timeline_group_window(waves)
    if (
        metadata_window.get("first_ms") != expected_window["first_ms"]
        or metadata_window.get("last_ms") != expected_window["last_ms"]
    ):
        raise HistoricalFuryExpertEpisodeError(
            "metadata encounter window differs from exact DPS window: "
            f"instance_id={row['instance_id']}, encounter_id={encounter_id}, "
            f"player_guid={guid}, expected={expected_window}, "
            f"metadata={metadata_window}"
        )
    left_unobserved_ms = observed_window["first_ms"] - expected_window["first_ms"]
    right_unobserved_ms = expected_window["last_ms"] - observed_window["last_ms"]
    if left_unobserved_ms < 0 or right_unobserved_ms < 0:
        raise HistoricalFuryExpertEpisodeError(
            "timeline wave envelope extends outside the exact metadata DPS window: "
            f"instance_id={row['instance_id']}, encounter_id={encounter_id}, "
            f"player_guid={guid}, expected={expected_window}, observed={observed_window}"
        )
    interior_gaps = _array(
        observed_window.get("interior_gaps"), "timeline interior gaps"
    )
    interior_unobserved_ms = _integer(
        observed_window.get("interior_unobserved_ms"),
        "timeline interior unobserved milliseconds",
    )
    partial_wave_coverage = bool(
        left_unobserved_ms or right_unobserved_ms or interior_gaps
    )

    player_rows = [_player_row(wave, guid) for wave in waves]
    spec_assessment = _timeline_spec_assessment(player_rows)
    wave_observations = []
    for wave, player_row in zip(waves, player_rows):
        try:
            trace = named_v1.build_strict_prefix_trace(
                wave,
                player_row,
                player_guid=guid,
                classify_action=classify_fury_action,
            )
        except named_v1.HistoricalNamedWarriorEpisodeError as error:
            raise HistoricalFuryExpertEpisodeError(str(error)) from error
        wave_observations.append(
            {
                "wave_id": wave.get("wave_id"),
                "wave_ordinal": wave.get("wave_ordinal"),
                "encounter_ordinal": wave.get("encounter_ordinal"),
                "source_wave_content_sha256": _mapping(
                    wave.get("content_address"), "wave.content_address"
                ).get("sha256"),
                "window": deepcopy(
                    dict(
                        _mapping(
                            _mapping(
                                wave.get("reconstruction_binding"),
                                "reconstruction_binding",
                            ).get("window"),
                            "reconstruction window",
                        )
                    )
                ),
                "prefix_transitions": trace["prefix_transitions"],
                "summary": trace["summary"],
            }
        )

    episode_identity = {
        "instance_id": row["instance_id"],
        "encounter_id": encounter_id,
        "player_guid": guid.casefold(),
        "ranking_record_id": row["ranking_record_id"],
    }
    return {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "record_type": "historical_fury_exact_window_observation_episode",
        "status": STATUS,
        "episode_id": ":".join(str(episode_identity[key]) for key in episode_identity),
        "instance_id": row["instance_id"],
        "encounter_id": encounter_id,
        "player": {
            "guid": guid,
            "name": row["character_name"],
            "class": "WARRIOR",
            "window_level_spec": "Fury",
            "timeline_spec_assessment": spec_assessment,
        },
        "exact_dps_window": {
            key: deepcopy(row[key])
            for key in (
                "ranking_record_id",
                "encounter_name",
                "killed_at",
                "duration_secs",
                "damage_done",
                "dps",
                "spec",
                "role",
            )
        },
        "window_join": {
            "identity": [
                "instance_id",
                "encounter_id",
                "killed_at_ms",
                "duration_ms",
                "character_guid",
            ],
            "expected_from_exact_dps": expected_window,
            "authoritative_metadata_encounter_window": deepcopy(
                dict(metadata_window)
            ),
            "observed_timeline_group_envelope": observed_window,
            "metadata_exact_millisecond_window_match": True,
            "timeline_wave_envelope_is_subset": True,
            "coverage": {
                "partial": partial_wave_coverage,
                "left_unobserved_ms": left_unobserved_ms,
                "right_unobserved_ms": right_unobserved_ms,
                "interior_gap_count": len(interior_gaps),
                "interior_unobserved_ms": interior_unobserved_ms,
                "interior_gaps": deepcopy(interior_gaps),
                "wave_envelope_exactly_covers_metadata_window": (
                    not left_unobserved_ms and not right_unobserved_ms
                ),
                "wave_windows_contiguously_cover_metadata_window": (
                    not partial_wave_coverage
                ),
            },
        },
        "wave_observations": wave_observations,
        "summary": _aggregate_wave_summaries(wave_observations),
        "contracts": {
            "state_cutoff": "strictly before each current event inside its source wave",
            "action_ontology": "historical Fury v3 exact 15-action ontology",
            "start_semantics": "server-observed controllable-action proxy only",
            "go_fail_semantics": "outcome only; never a policy label",
            "client_request_and_queue_intent": "MISSING; never inferred from START, GO, or FAIL",
            "target_switch_intent": "MISSING; exact resolved target only",
            "outside_wave_actions": (
                "MISSING when coverage is partial; never inferred across left, "
                "interior, or right gaps"
            ),
        },
        "scientific_boundaries": {
            "historical_observation_only": True,
            "comparison_authorized": False,
            "training_authorized": False,
            "superiority_claim_authorized": False,
            "client_action_request_observed": False,
            "client_next_swing_queue_intent_observed": False,
            "target_switch_intent_observed": False,
        },
    }


def _cohort_selection_consistent(
    cohort: Mapping[str, Any], selection: cohort_v2.FrozenCohortSelection
) -> None:
    source = _mapping(cohort.get("source_index"), "cohort.source_index")
    if dict(source) != selection.source_binding:
        raise HistoricalFuryExpertEpisodeError(
            "frozen cohort source-index binding differs from replayed source"
        )
    summary = _mapping(cohort.get("summary"), "cohort.summary")
    if summary.get("selected_exact_fury_encounter_observation_count") != len(
        selection.selected_rows
    ):
        raise HistoricalFuryExpertEpisodeError("frozen cohort selected-row count differs")
    selected_guids = {str(row["character_guid"]).casefold() for row in selection.selected_rows}
    selected_memberships = {
        (str(row["character_guid"]).casefold(), str(row["instance_id"]))
        for row in selection.selected_rows
    }
    candidate_guids = set()
    candidate_memberships = set()
    for raw in _array(cohort.get("player_candidates"), "cohort.player_candidates"):
        candidate = _mapping(raw, "cohort candidate")
        guid = str(candidate.get("character_guid") or "").casefold()
        candidate_guids.add(guid)
        for raid in _array(candidate.get("raids"), "candidate.raids"):
            instance_id = str(_mapping(raid, "candidate raid").get("instance_id"))
            candidate_memberships.add((guid, instance_id))
    if selected_guids != candidate_guids or selected_memberships != candidate_memberships:
        raise HistoricalFuryExpertEpisodeError(
            "frozen cohort player or player-raid membership differs from source replay"
        )


def _source_index_path(cohort: Mapping[str, Any], data_root: Path) -> Path:
    source = _mapping(cohort.get("source_index"), "cohort.source_index")
    relative_text = _text(source.get("path"), "cohort.source_index.path")
    assert relative_text is not None
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise HistoricalFuryExpertEpisodeError("cohort source-index path is unsafe")
    resolved = (data_root / relative).resolve()
    if not resolved.is_relative_to(data_root) or not resolved.is_file():
        raise HistoricalFuryExpertEpisodeError("cohort source-index manifest is missing")
    return resolved


def _instance_entries(timeline: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(_mapping(raw, "timeline instance").get("instance_id")): _mapping(
            raw, "timeline instance"
        )
        for raw in _array(timeline.get("instances"), "timeline.instances")
    }


def _bound_raw_manifest(
    data_root: Path,
    binding: Mapping[str, Any],
    cache: dict[Path, tuple[JSONMap, Path]],
) -> tuple[JSONMap, Path]:
    relative_text = _text(binding.get("path"), "raw_api_manifest.path")
    assert relative_text is not None
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise HistoricalFuryExpertEpisodeError("raw API manifest path is unsafe")
    path = (data_root / relative).resolve()
    if not path.is_relative_to(data_root) or not path.is_file():
        raise HistoricalFuryExpertEpisodeError("bound raw API manifest is missing")
    expected_size = _integer(binding.get("size_bytes"), "raw API manifest size")
    expected_sha = _text(binding.get("file_sha256"), "raw API manifest SHA")
    assert expected_sha is not None
    if path.stat().st_size != expected_size or _sha256_file(path) != expected_sha:
        raise HistoricalFuryExpertEpisodeError(
            "bound raw API manifest hash or size differs from timeline binding"
        )
    cached = cache.get(path)
    if cached is not None:
        return cached
    manifest = _load_json(path, "bound raw API manifest")
    if (
        manifest.get("schema") != ingest_v1.SCHEMA
        or manifest.get("kind") != "chronicle_external_api_raw_snapshot"
    ):
        raise HistoricalFuryExpertEpisodeError("bound raw API manifest is unsupported")
    raw_root = path.parent.parent.resolve()
    if not raw_root.is_relative_to(data_root):
        raise HistoricalFuryExpertEpisodeError("raw API object root escapes offline_data")
    cache[path] = (manifest, raw_root)
    return manifest, raw_root


def _instance_metadata_windows(
    entry: Mapping[str, Any],
    *,
    data_root: Path,
    instance_id: str,
    required_encounter_ids: set[str],
    raw_manifest_cache: dict[Path, tuple[JSONMap, Path]],
) -> tuple[dict[str, JSONMap], JSONMap]:
    """Load only one selected metadata object and retain requested encounters."""

    source_binding = _mapping(entry.get("source_binding"), "instance source_binding")
    raw_binding = _mapping(
        source_binding.get("raw_api_manifest"), "source_binding.raw_api_manifest"
    )
    raw_manifest, raw_root = _bound_raw_manifest(
        data_root, raw_binding, raw_manifest_cache
    )
    matches = [
        _mapping(raw, "raw API instance")
        for raw in _array(raw_manifest.get("instances"), "raw API instances")
        if isinstance(raw, Mapping) and raw.get("instance_id") == instance_id
    ]
    if len(matches) != 1:
        raise HistoricalFuryExpertEpisodeError(
            "selected instance is absent or duplicated in bound raw API manifest"
        )
    wrapper = _mapping(matches[0].get("metadata"), "raw API instance metadata")
    reference = _mapping(wrapper.get("object"), "raw API metadata object")
    try:
        metadata_bytes = ingest_v1._read_object_reference(
            raw_root, dict(reference), label=f"metadata for {instance_id}"
        )
    except ingest_v1.ChronicleIngestError as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    try:
        metadata_raw = json.loads(metadata_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalFuryExpertEpisodeError(
            f"cannot decode metadata for {instance_id}: {error}"
        ) from error
    metadata = _mapping(metadata_raw, "raw API metadata document")
    if metadata.get("id") != instance_id:
        raise HistoricalFuryExpertEpisodeError("metadata instance id differs")

    selected: dict[str, JSONMap] = {}
    seen: set[str] = set()
    for raw in _array(metadata.get("encounters"), "metadata encounters"):
        encounter = _mapping(raw, "metadata encounter")
        encounter_id = _text(encounter.get("id"), "metadata encounter id")
        assert encounter_id is not None
        if encounter_id in seen:
            raise HistoricalFuryExpertEpisodeError("metadata encounter id is duplicated")
        seen.add(encounter_id)
        if encounter_id not in required_encounter_ids:
            continue
        if encounter.get("instance_id") != instance_id:
            raise HistoricalFuryExpertEpisodeError("metadata encounter instance differs")
        start_time = _text(encounter.get("start_time"), "metadata encounter start_time")
        end_time = _text(encounter.get("end_time"), "metadata encounter end_time")
        assert start_time is not None and end_time is not None
        first_ms = _epoch_milliseconds(start_time, "metadata encounter start_time")
        last_ms = _epoch_milliseconds(end_time, "metadata encounter end_time")
        if last_ms <= first_ms:
            raise HistoricalFuryExpertEpisodeError(
                "metadata encounter window must have positive duration"
            )
        remaining = encounter.get("remaining")
        if remaining is not None and not isinstance(remaining, list):
            raise HistoricalFuryExpertEpisodeError(
                "metadata encounter remaining must be an array when present"
            )
        selected[encounter_id] = {
            "first_ms": first_ms,
            "last_ms": last_ms,
            "start_time": start_time,
            "end_time": end_time,
            "name": encounter.get("name"),
            "boss": encounter.get("boss"),
            "kill_type": encounter.get("kill_type"),
            "remaining_count": len(remaining or []),
        }
    missing = sorted(required_encounter_ids - set(selected))
    if missing:
        raise HistoricalFuryExpertEpisodeError(
            f"selected encounters are absent from bound metadata: {missing}"
        )
    return selected, {
        "instance_id": instance_id,
        "raw_api_manifest": deepcopy(dict(raw_binding)),
        "metadata_object": deepcopy(dict(reference)),
        "selected_encounter_count": len(selected),
        "selected_encounter_ids": sorted(selected),
    }


def _load_instance_timeline_groups(
    timeline_manifest_path: Path,
    entry: Mapping[str, Any],
    *,
    instance_id: str,
    required_encounter_ids: set[str],
    expected_contamination_labels: set[str],
) -> tuple[dict[str, list[JSONMap]], JSONMap]:
    """Stream one instance and retain only required encounter groups in memory."""

    provenance = _mapping(entry.get("instance_provenance"), "instance_provenance")
    temporal = _mapping(
        provenance.get("temporal_and_guild_provenance"), "temporal provenance"
    )
    contamination = _mapping(temporal.get("contamination"), "contamination")
    observed_label = str(contamination.get("label") or "")
    if observed_label not in expected_contamination_labels:
        raise HistoricalFuryExpertEpisodeError(
            "timeline contamination label differs from frozen selected DPS rows"
        )
    groups: dict[str, list[JSONMap]] = defaultdict(list)
    try:
        partition_path, partition = named_v1._resolve_partition(
            timeline_manifest_path, entry
        )
        for wave in named_v1._iter_verified_waves(
            partition_path, partition, instance_id=instance_id
        ):
            encounter_id = wave.get("encounter_id")
            if encounter_id in required_encounter_ids:
                groups[str(encounter_id)].append(wave)
    except named_v1.HistoricalNamedWarriorEpisodeError as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    for waves in groups.values():
        waves.sort(key=lambda wave: (int(wave["encounter_ordinal"]), int(wave["wave_ordinal"])))
    return groups, {
        "instance_id": instance_id,
        "path": partition.get("path"),
        "record_count": partition.get("record_count"),
        "compressed_file_sha256": partition.get("compressed_file_sha256"),
        "logical_content_sha256": partition.get("logical_content_sha256"),
        "retained_required_encounter_group_count": len(groups),
    }


def _write_instance_partition(
    output_directory: Path,
    instance_id: str,
    episodes: Iterable[Mapping[str, Any]],
) -> JSONMap:
    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_directory,
        prefix=f".{instance_id}.",
        suffix=".jsonl.gz",
        delete=False,
    ) as raw_handle:
        temporary = Path(raw_handle.name)
        logical = hashlib.sha256()
        logical_size = 0
        count = 0
        labels = 0
        starts = 0
        unknown_specs = 0
        complete_wave_coverage = 0
        partial_wave_coverage = 0
        left_unobserved_ms = 0
        right_unobserved_ms = 0
        interior_gap_count = 0
        interior_unobserved_ms = 0
        maximum_interior_gap_ms = 0
        try:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as compressed:
                for episode in episodes:
                    payload = _canonical_bytes(episode, newline=True)
                    compressed.write(payload)
                    logical.update(payload)
                    logical_size += len(payload)
                    count += 1
                    summary = _mapping(episode.get("summary"), "episode.summary")
                    labels += int(summary.get("controllable_policy_label_count", 0))
                    starts += int(summary.get("server_observed_start_count", 0))
                    assessment = _mapping(
                        _mapping(episode.get("player"), "episode.player").get(
                            "timeline_spec_assessment"
                        ),
                        "timeline spec assessment",
                    )
                    unknown_specs += int(
                        assessment.get("unknown_or_conflicting_timeline_wave_count", 0)
                    )
                    coverage = _mapping(
                        _mapping(episode.get("window_join"), "episode.window_join").get(
                            "coverage"
                        ),
                        "episode window coverage",
                    )
                    if coverage.get("partial") is True:
                        partial_wave_coverage += 1
                    elif coverage.get("partial") is False:
                        complete_wave_coverage += 1
                    else:
                        raise HistoricalFuryExpertEpisodeError(
                            "episode window coverage partial flag is invalid"
                        )
                    left_unobserved_ms += _integer(
                        coverage.get("left_unobserved_ms"), "left_unobserved_ms"
                    )
                    right_unobserved_ms += _integer(
                        coverage.get("right_unobserved_ms"), "right_unobserved_ms"
                    )
                    gaps = _array(
                        coverage.get("interior_gaps"), "episode interior gaps"
                    )
                    declared_gap_count = _integer(
                        coverage.get("interior_gap_count"), "interior_gap_count"
                    )
                    if declared_gap_count != len(gaps):
                        raise HistoricalFuryExpertEpisodeError(
                            "episode interior gap count differs"
                        )
                    gap_total = sum(
                        _integer(
                            _mapping(gap, "episode interior gap").get(
                                "unobserved_ms"
                            ),
                            "interior gap unobserved_ms",
                        )
                        for gap in gaps
                    )
                    if gap_total != _integer(
                        coverage.get("interior_unobserved_ms"),
                        "interior_unobserved_ms",
                    ):
                        raise HistoricalFuryExpertEpisodeError(
                            "episode interior gap duration differs"
                        )
                    interior_gap_count += declared_gap_count
                    interior_unobserved_ms += gap_total
                    maximum_interior_gap_ms = max(
                        maximum_interior_gap_ms,
                        max(
                            (
                                _integer(
                                    _mapping(gap, "episode interior gap").get(
                                        "unobserved_ms"
                                    ),
                                    "interior gap unobserved_ms",
                                )
                                for gap in gaps
                            ),
                            default=0,
                        ),
                    )
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        except Exception:
            raw_handle.close()
            temporary.unlink(missing_ok=True)
            raise
    logical_sha = logical.hexdigest()
    final = output_directory / f"{instance_id}.{logical_sha}.jsonl.gz"
    os.replace(temporary, final)
    return {
        "instance_id": instance_id,
        "path": final.name,
        "record_schema": SCHEMA,
        "record_count": count,
        "server_observed_start_count": starts,
        "controllable_policy_label_count": labels,
        "unknown_or_conflicting_timeline_player_wave_count": unknown_specs,
        "complete_wave_coverage_episode_count": complete_wave_coverage,
        "partial_wave_coverage_episode_count": partial_wave_coverage,
        "left_unobserved_ms_across_episodes": left_unobserved_ms,
        "right_unobserved_ms_across_episodes": right_unobserved_ms,
        "interior_gap_count_across_episodes": interior_gap_count,
        "interior_unobserved_ms_across_episodes": interior_unobserved_ms,
        "maximum_interior_gap_ms": maximum_interior_gap_ms,
        "logical_size_bytes": logical_size,
        "logical_content_sha256": logical_sha,
        "compressed_size_bytes": final.stat().st_size,
        "compressed_file_sha256": _sha256_file(final),
        "gzip_mtime": 0,
    }


def build_historical_fury_expert_episodes(
    *,
    cohort_path: str | Path = DEFAULT_COHORT,
    timeline_manifest_path: str | Path = DEFAULT_TIMELINE_MANIFEST,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    zstd_executable: str | Path | None = None,
) -> EpisodeBuildResult:
    """Build exact-window Fury observation episodes without running training."""

    root = Path(data_root).expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in root.parts}:
        raise HistoricalFuryExpertEpisodeError("data_root must be an offline_data tree")
    cohort_file = Path(cohort_path).expanduser().resolve()
    cohort = _load_json(cohort_file, "frozen Fury cohort")
    try:
        cohort_v2.validate_cohort_document(cohort)
    except cohort_v2.HistoricalFuryExpertCohortError as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    try:
        cohort_v2.audit_historical_fury_expert_cohort(
            cohort_file,
            data_root=root,
            zstd_executable=zstd_executable,
        )
    except cohort_v2.HistoricalFuryExpertCohortError as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    source_path = _source_index_path(cohort, root)
    try:
        selection, resolved_source = cohort_v2.load_frozen_cohort_selection(
            source_path,
            data_root=root,
            zstd_executable=zstd_executable,
        )
    except cohort_v2.HistoricalFuryExpertCohortError as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    _cohort_selection_consistent(cohort, selection)

    selected_rows = list(selection.selected_rows)
    unresolved_rows = [row for row in selected_rows if row["encounter_id"] is None]
    candidate_rows = [row for row in selected_rows if row["encounter_id"] is not None]
    seen_ranking_ids: set[str] = set()
    for row in selected_rows:
        ranking_id = str(row["ranking_record_id"])
        if ranking_id in seen_ranking_ids:
            raise HistoricalFuryExpertEpisodeError(
                "selected ranking_record_id is duplicated"
            )
        seen_ranking_ids.add(ranking_id)
    seen_episode_join_keys: set[tuple[str, str, str]] = set()
    for row in candidate_rows:
        episode_join_key = (
            str(row["instance_id"]),
            str(row["encounter_id"]),
            str(row["character_guid"]).casefold(),
        )
        if episode_join_key in seen_episode_join_keys:
            raise HistoricalFuryExpertEpisodeError(
                "selected non-null episode join key is duplicated"
            )
        seen_episode_join_keys.add(episode_join_key)
    candidate_rows_by_instance: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    expected_contamination_by_instance: dict[str, set[str]] = defaultdict(set)
    for row in candidate_rows:
        instance_id = str(row["instance_id"])
        candidate_rows_by_instance[instance_id].append(row)
        expected_contamination_by_instance[instance_id].add(
            str(row["contamination_label"])
        )
    timeline_file = Path(timeline_manifest_path).expanduser().resolve()
    try:
        timeline, resolved_timeline = timeline_v2.load_external_team_timeline_manifest(
            timeline_file,
            verify_inputs=False,
            verify_partitions=False,
        )
    except timeline_v2.ChronicleExternalTeamTimelineV2Error as error:
        raise HistoricalFuryExpertEpisodeError(str(error)) from error
    entries = _instance_entries(timeline)
    missing_instances = sorted(set(candidate_rows_by_instance) - set(entries))
    if missing_instances:
        raise HistoricalFuryExpertEpisodeError(
            f"selected cohort raids are absent from local timeline: {missing_instances}"
        )

    destination = _under_offline_data(Path(output_directory))
    partitions = []
    timeline_bindings = []
    metadata_bindings = []
    raw_manifest_cache: dict[Path, tuple[JSONMap, Path]] = {}
    complete_wave_coverage_groups = 0
    partial_wave_coverage_groups = 0
    maximum_left_unobserved_ms = 0
    maximum_right_unobserved_ms = 0
    interior_gap_count_across_encounter_groups = 0
    interior_unobserved_ms_across_encounter_groups = 0
    maximum_interior_gap_ms = 0
    for instance_id in sorted(candidate_rows_by_instance):
        expected_labels = expected_contamination_by_instance[instance_id]
        if len(expected_labels) != 1:
            raise HistoricalFuryExpertEpisodeError(
                "one frozen selected instance has conflicting contamination labels"
            )
        instance_rows = candidate_rows_by_instance[instance_id]
        dps_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in instance_rows:
            dps_groups[str(row["encounter_id"])].append(row)
        metadata_windows, metadata_binding = _instance_metadata_windows(
            entries[instance_id],
            data_root=root,
            instance_id=instance_id,
            required_encounter_ids=set(dps_groups),
            raw_manifest_cache=raw_manifest_cache,
        )
        groups, binding = _load_instance_timeline_groups(
            resolved_timeline,
            entries[instance_id],
            instance_id=instance_id,
            required_encounter_ids=set(dps_groups),
            expected_contamination_labels=expected_labels,
        )
        missing_groups = sorted(set(dps_groups) - set(groups))
        if missing_groups:
            raise HistoricalFuryExpertEpisodeError(
                f"exact DPS encounter groups are absent from timeline: {missing_groups}"
            )
        for encounter_id, rows in dps_groups.items():
            windows = {
                (window["first_ms"], window["last_ms"])
                for window in (_dps_window(row) for row in rows)
            }
            if len(windows) != 1:
                raise HistoricalFuryExpertEpisodeError(
                    "one exact DPS encounter group has conflicting player windows"
                )
            expected = next(iter(windows))
            metadata_window = metadata_windows[encounter_id]
            if expected != (
                metadata_window["first_ms"],
                metadata_window["last_ms"],
            ):
                raise HistoricalFuryExpertEpisodeError(
                    "metadata encounter window differs from exact DPS window: "
                    f"instance_id={instance_id}, encounter_id={encounter_id}, "
                    f"expected={sorted(windows)}, metadata={metadata_window}"
                )
            observed = _timeline_group_window(groups[encounter_id])
            left_gap = observed["first_ms"] - expected[0]
            right_gap = expected[1] - observed["last_ms"]
            if left_gap < 0 or right_gap < 0:
                raise HistoricalFuryExpertEpisodeError(
                    "timeline wave envelope extends outside the exact metadata DPS window: "
                    f"instance_id={instance_id}, encounter_id={encounter_id}, "
                    f"expected={sorted(windows)}, observed={observed}"
                )
            interior_gaps = _array(
                observed.get("interior_gaps"), "timeline interior gaps"
            )
            if left_gap or right_gap or interior_gaps:
                partial_wave_coverage_groups += 1
            else:
                complete_wave_coverage_groups += 1
            maximum_left_unobserved_ms = max(maximum_left_unobserved_ms, left_gap)
            maximum_right_unobserved_ms = max(maximum_right_unobserved_ms, right_gap)
            interior_gap_count_across_encounter_groups += len(interior_gaps)
            interior_unobserved_ms_across_encounter_groups += _integer(
                observed.get("interior_unobserved_ms"),
                "timeline interior unobserved milliseconds",
            )
            maximum_interior_gap_ms = max(
                maximum_interior_gap_ms,
                max(
                    (
                        _integer(
                            _mapping(gap, "timeline interior gap").get(
                                "unobserved_ms"
                            ),
                            "timeline interior gap unobserved_ms",
                        )
                        for gap in interior_gaps
                    ),
                    default=0,
                ),
            )
        ordered_rows = sorted(
            instance_rows,
            key=lambda item: (
                str(item["encounter_id"]),
                str(item["character_guid"]).casefold(),
                str(item["ranking_record_id"]),
            ),
        )
        partitions.append(
            _write_instance_partition(
                destination,
                instance_id,
                (
                    _build_observation_episode(
                        row,
                        groups[str(row["encounter_id"])],
                        metadata_windows[str(row["encounter_id"])],
                    )
                    for row in ordered_rows
                ),
            )
        )
        timeline_bindings.append(binding)
        metadata_bindings.append(metadata_binding)
    unresolved = [
        {
            "status": NULL_ENCOUNTER_STATUS,
            "instance_id": row["instance_id"],
            "player_guid": row["character_guid"],
            "ranking_record_id": row["ranking_record_id"],
            "encounter_name": row["encounter_name"],
            "killed_at": row["killed_at"],
        }
        for row in sorted(
            unresolved_rows,
            key=lambda item: (
                str(item["instance_id"]),
                str(item["character_guid"]).casefold(),
                str(item["ranking_record_id"]),
            ),
        )
    ]
    summary = {
        "selected_exact_fury_observation_count": len(selected_rows),
        "candidate_nonnull_encounter_episode_count": len(candidate_rows),
        "unresolved_null_encounter_observation_count": len(unresolved),
        "instance_partition_count": len(partitions),
        "server_observed_start_count": sum(
            row["server_observed_start_count"] for row in partitions
        ),
        "controllable_policy_label_count": sum(
            row["controllable_policy_label_count"] for row in partitions
        ),
        "unknown_or_conflicting_timeline_player_wave_count": sum(
            row["unknown_or_conflicting_timeline_player_wave_count"]
            for row in partitions
        ),
        "complete_wave_coverage_encounter_group_count": (
            complete_wave_coverage_groups
        ),
        "partial_wave_coverage_encounter_group_count": partial_wave_coverage_groups,
        "complete_wave_coverage_episode_count": sum(
            row["complete_wave_coverage_episode_count"] for row in partitions
        ),
        "partial_wave_coverage_episode_count": sum(
            row["partial_wave_coverage_episode_count"] for row in partitions
        ),
        "maximum_left_unobserved_ms": maximum_left_unobserved_ms,
        "maximum_right_unobserved_ms": maximum_right_unobserved_ms,
        "interior_gap_count_across_encounter_groups": (
            interior_gap_count_across_encounter_groups
        ),
        "interior_unobserved_ms_across_encounter_groups": (
            interior_unobserved_ms_across_encounter_groups
        ),
        "interior_gap_count_across_episodes": sum(
            row["interior_gap_count_across_episodes"] for row in partitions
        ),
        "interior_unobserved_ms_across_episodes": sum(
            row["interior_unobserved_ms_across_episodes"] for row in partitions
        ),
        "maximum_interior_gap_ms": maximum_interior_gap_ms,
    }
    if (
        summary["candidate_nonnull_encounter_episode_count"]
        + summary["unresolved_null_encounter_observation_count"]
        != summary["selected_exact_fury_observation_count"]
    ):
        raise HistoricalFuryExpertEpisodeError("selected observation accounting mismatch")
    if (
        summary["complete_wave_coverage_episode_count"]
        + summary["partial_wave_coverage_episode_count"]
        != summary["candidate_nonnull_encounter_episode_count"]
    ):
        raise HistoricalFuryExpertEpisodeError("wave coverage accounting mismatch")
    manifest_core = {
        "schema": MANIFEST_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "input_closure": {
            "frozen_cohort": {
                "path": str(cohort_file),
                "schema": cohort.get("schema"),
                "file_sha256": _sha256_file(cohort_file),
            },
            "source_dps_index": {
                "path": str(resolved_source),
                **deepcopy(selection.source_binding),
            },
            "external_v2_timeline": {
                "path": str(resolved_timeline),
                "schema": timeline.get("schema"),
                "content_sha256": _mapping(
                    timeline.get("content_address"), "timeline.content_address"
                ).get("sha256"),
                "selected_instance_partitions": timeline_bindings,
            },
            "selected_raw_metadata": metadata_bindings,
            "network_request_count": 0,
        },
        "join_contract": {
            "candidate_identity": [
                "ranking_record_id",
                "instance_id",
                "encounter_id",
                "killed_at_ms",
                "duration_ms",
                "character_guid",
            ],
            "timeline_group_key": ["instance_id", "encounter_id"],
            "authoritative_window": (
                "content-bound raw metadata encounter start_time/end_time exactly equal "
                "killed_at-duration_secs/killed_at"
            ),
            "timeline_wave_coverage": (
                "the min first_anchor through max last_context_anchor is an inclusive "
                "subset of the authoritative window"
            ),
            "partial_coverage": (
                "left/interior/right unobserved milliseconds are recorded; actions "
                "outside reconstruction waves are missing and never inferred"
            ),
            "nullable_encounter_id": NULL_ENCOUNTER_STATUS,
            "window_level_spec": "exact DPS row Fury",
            "timeline_spec": (
                "explicit conflict-free non-Fury rejects; Unknown/conflicting evidence is "
                "preserved and counted"
            ),
        },
        "action_contract": {
            "ontology": "chronicle_external_historical_fury_policy_v3 ACTION_ONTOLOGY",
            "ontology_size": len(policy_v3.ACTION_ONTOLOGY),
            "policy_label": "recognized START only",
            "go_fail": "outcome only, never label",
            "client_request": "not observed",
            "next_swing_queue_intent": "not observed or inferred",
            "target_switch_intent": "not observed or inferred",
            "state": "strict prefix within each source wave",
            "outside_wave_actions": "missing and never inferred",
        },
        "unresolved_observations": unresolved,
        "partitions": partitions,
        "summary": summary,
        "scientific_boundaries": {
            "historical_observation_only": True,
            "comparison_authorized": False,
            "training_authorized": False,
            "closed_loop_baseline_authorized": False,
            "superiority_claim_authorized": False,
        },
    }
    manifest = _content_addressed(manifest_core)
    payload = _canonical_bytes(manifest, newline=True)
    stable = destination / "manifest.json"
    address = str(_mapping(manifest["content_address"], "content_address")["sha256"])
    addressed = destination / (
        f"historical_fury_expert_episode_adapter_v1.{address}.manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return EpisodeBuildResult(
        manifest=stable,
        content_addressed_manifest=addressed,
        partition_count=len(partitions),
        selected_observation_count=len(selected_rows),
        candidate_episode_count=len(candidate_rows),
        unresolved_null_encounter_count=len(unresolved),
        controllable_policy_label_count=summary["controllable_policy_label_count"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join frozen Fury exact-DPS windows to External-V2 observations"
    )
    parser.add_argument("--cohort", type=Path, default=DEFAULT_COHORT)
    parser.add_argument("--timeline-manifest", type=Path, default=DEFAULT_TIMELINE_MANIFEST)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--zstd", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_fury_expert_episodes(
        cohort_path=args.cohort,
        timeline_manifest_path=args.timeline_manifest,
        data_root=args.data_root,
        output_directory=args.output_directory,
        zstd_executable=args.zstd,
    )
    rendered = _canonical_bytes(result.as_dict(), newline=True).decode("utf-8")
    if stdout is None:
        print(rendered, end="")
    else:
        stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalFuryExpertEpisodeError as error:
        print(str(error), file=os.sys.stderr)
        raise SystemExit(2)


__all__ = [
    "DEFAULT_COHORT",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_TIMELINE_MANIFEST",
    "EpisodeBuildResult",
    "HistoricalFuryExpertEpisodeError",
    "IMPLEMENTATION_REVISION",
    "MANIFEST_SCHEMA",
    "NULL_ENCOUNTER_STATUS",
    "SCHEMA",
    "STATUS",
    "build_historical_fury_expert_episodes",
    "classify_fury_action",
    "fury_action_spec",
    "main",
]
