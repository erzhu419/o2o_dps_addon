"""Build compact Fury decision partitions directly from normalized Chronicle JSONL.

The reconstruction-readiness report is the selection authority.  Only rows
whose identity is verified and whose leaderboard evidence says ``Fury`` are
included.  Each normalized source is opened once and all selected players in
that source are reconstructed together; the larger per-player partial
trajectory artifact is deliberately not materialized.

The output remains partial-observation behavior evidence.  ``START`` is a
server-observed action candidate, GO/FAIL is linked only when one pending
candidate is unique, and neither client queue intent nor causal reward is
invented.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterator, Sequence

from .chronicle_fury_trajectory import parse_combatant_info, parse_type_amount


SCHEMA_VERSION = 1
SCHEMA_NAME = "chronicle_fury_decision_dataset/v1"
RECORD_SCHEMA = "chronicle_fury_decision/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_READINESS = (
    DEFAULT_DATA_ROOT / "reports" / "chronicle_warrior_reconstruction_readiness.json"
)
DEFAULT_REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
DEFAULT_OUTPUT = (
    DEFAULT_DATA_ROOT / "derived" / "chronicle_fury_decision_dataset" / "v1"
)

MISSING_STATE_FIELDS = (
    "absolute_rage",
    "gcd_remaining_ms",
    "cooldown_remaining_ms",
    "mainhand_swing_remaining_ms",
    "offhand_swing_remaining_ms",
    "queue_intent",
    "client_keypress_ms",
    "target_hp",
    "player_hp",
    "boss_phase",
    "target_count",
    "stance",
    "range",
    "behind_target",
    "gear_item_ids",
    "exact_talent_ranks",
)


class FuryDecisionDatasetError(RuntimeError):
    """The supplied evidence cannot produce the compact Fury dataset."""


@dataclass(frozen=True)
class Target:
    normalized_file: Path
    encounter_id: str
    player_guid: str
    player_name: str
    identity_status: str
    fury_leaderboard_rows: tuple[dict[str, Any], ...]
    full_state_ready: bool
    full_state_blockers: tuple[str, ...]


@dataclass(frozen=True)
class SourceJob:
    normalized_file: Path
    targets: tuple[Target, ...]


@dataclass
class PlayerState:
    target: Target
    talent_tree: list[int] | None = None
    gear_slot_count: int | None = None
    context_anchor: dict[str, Any] | None = None
    rage_gain_total: float = 0.0
    rage_gain_rows: int = 0
    rage_loss_total: float = 0.0
    rage_loss_rows: int = 0
    last_rage_gain_anchor: dict[str, Any] | None = None
    last_rage_loss_anchor: dict[str, Any] | None = None
    last_auto_attack_offset_ms: int | None = None
    last_auto_attack_anchor: dict[str, Any] | None = None
    damaged_target_guids: set[str] = field(default_factory=set)
    last_damage_anchor: dict[str, Any] | None = None
    player_auras: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    outgoing_target_auras: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    last_player_aura_anchor: dict[str, Any] | None = None
    last_outgoing_aura_anchor: dict[str, Any] | None = None
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    last_result_anchor: dict[str, Any] | None = None
    pending: dict[tuple[str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    decisions: list[dict[str, Any]] = field(default_factory=list)
    decisions_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_decision: dict[str, Any] | None = None
    selected_source_rows: int = 0


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    manifest_entry: dict[str, Any]


@dataclass(frozen=True)
class FuryDecisionDatasetResult:
    decision_count: int
    partition_count: int
    manifest: Path
    partitions: tuple[Path, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "schema": SCHEMA_NAME,
            "decision_count": self.decision_count,
            "partition_count": self.partition_count,
            "manifest": str(self.manifest),
            "partitions": [str(path) for path in self.partitions],
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryDecisionDatasetError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise FuryDecisionDatasetError(f"{label} is not a JSON object: {path}")
    return value


def _guid_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _safe_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    if not safe:
        raise FuryDecisionDatasetError(f"unsafe empty output name from {value!r}")
    return safe


def _resolve_normalized(value: Any, readiness_path: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        raise FuryDecisionDatasetError("eligible readiness row lacks normalized_file")
    supplied = Path(text).expanduser()
    if supplied.is_file():
        return supplied.resolve()
    relocated = readiness_path.parent.parent / "normalized" / supplied.name
    if relocated.is_file():
        return relocated.resolve()
    raise FuryDecisionDatasetError(f"normalized JSONL does not exist: {supplied}")


def _fury_rows(item: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "board_spec": "Fury",
            "rank": row.get("rank"),
            "source_file": row.get("source_file"),
        }
        for row in item.get("leaderboard_rows") or []
        if isinstance(row, dict)
        and str(row.get("board_spec") or "").strip().casefold() == "fury"
    )


def _select_jobs(
    report: dict[str, Any], readiness_path: Path
) -> tuple[list[SourceJob], dict[str, int]]:
    rows = report.get("player_encounters")
    if not isinstance(rows, list):
        raise FuryDecisionDatasetError("readiness report has no player_encounters list")

    counters: Counter[str] = Counter()
    by_source: dict[Path, dict[tuple[str, str], Target]] = defaultdict(dict)
    for item in rows:
        if not isinstance(item, dict):
            continue
        counters["readiness_player_encounters"] += 1
        fury_rows = _fury_rows(item)
        if not fury_rows:
            counters["non_fury_excluded"] += 1
            continue
        identity = item.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        if identity.get("verified") is not True:
            counters["identity_unverified_excluded"] += 1
            continue

        normalized = _resolve_normalized(item.get("normalized_file"), readiness_path)
        encounter = str(item.get("encounter_id") or "").strip()
        guid = str(item.get("player_guid") or identity.get("player_guid") or "").strip()
        name = str(item.get("player_name") or "").strip()
        if not encounter or not guid or not name:
            raise FuryDecisionDatasetError(
                "eligible readiness row lacks encounter, player GUID, or player name"
            )
        target = Target(
            normalized_file=normalized,
            encounter_id=encounter,
            player_guid=guid,
            player_name=name,
            identity_status=str(identity.get("status") or "VERIFIED"),
            fury_leaderboard_rows=fury_rows,
            full_state_ready=item.get("full_state_ready") is True,
            full_state_blockers=tuple(
                str(value) for value in item.get("full_state_blockers") or []
            ),
        )
        key = (encounter, _guid_key(guid))
        previous = by_source[normalized].get(key)
        if previous is not None and previous != target:
            raise FuryDecisionDatasetError(
                f"conflicting readiness rows for {name}@{encounter} in {normalized.name}"
            )
        by_source[normalized][key] = target
        counters["eligible_fury_player_encounters"] += 1
        if target.full_state_ready:
            counters["full_state_ready_fury_player_encounters"] += 1

    jobs = [
        SourceJob(path, tuple(targets[key] for key in sorted(targets)))
        for path, targets in sorted(by_source.items(), key=lambda item: str(item[0]))
    ]
    if not jobs:
        raise FuryDecisionDatasetError(
            "readiness contains no identity-verified board_spec=Fury player encounters"
        )
    counters["normalized_sources"] = len(jobs)
    counters["eligible_fury_players"] = len(
        {
            (str(target.normalized_file), _guid_key(target.player_guid))
            for job in jobs
            for target in job.targets
        }
    )
    for name in (
        "readiness_player_encounters",
        "non_fury_excluded",
        "identity_unverified_excluded",
        "eligible_fury_player_encounters",
        "full_state_ready_fury_player_encounters",
        "normalized_sources",
        "eligible_fury_players",
    ):
        counters.setdefault(name, 0)
    return jobs, dict(counters)


def _registry_actions(path: Path) -> tuple[dict[int, dict[str, str]], dict[str, Any]]:
    registry = _load_object(path, "Warrior mechanics registry")
    actions: dict[int, dict[str, str]] = {}
    for mechanic in registry.get("mechanics") or []:
        if not isinstance(mechanic, dict):
            continue
        implementation = mechanic.get("implementation")
        implementation = implementation if isinstance(implementation, dict) else {}
        ids = {
            value
            for key, value in implementation.items()
            if key in ("spell_id", "wrapper_spell_id") and isinstance(value, int)
        }
        ids.update(
            value
            for value in implementation.get("spell_ids") or []
            if isinstance(value, int)
        )
        if implementation.get("queue_tag") == 1 or implementation.get(
            "replaces_next_main_hand_swing"
        ):
            lane = "on_swing_unknown_intent"
        elif implementation.get("consumes_gcd") is False:
            lane = "off_gcd"
        elif any(
            key in implementation
            for key in (
                "gcd_seconds",
                "base_gcd_seconds",
                "consumes_gcd",
                "cooldown_seconds",
            )
        ):
            lane = "gcd"
        else:
            lane = "unknown"
        for spell_id in ids:
            actions[spell_id] = {
                "lane": lane,
                "policy_action_key": str(mechanic.get("key") or ""),
            }
    return actions, {
        "path": str(path.resolve()),
        "schema_version": registry.get("schema_version"),
        "role": "action-lane classification only; no historical state is filled",
    }


def _raw_anchor(row: dict[str, Any], *, kind: str = "OBSERVED", note: str) -> dict[str, Any]:
    provenance = row.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    return {
        "kind": kind,
        "event_index": row.get("event_index"),
        "csv_line": provenance.get("csv_line"),
        "offset_ms": row.get("offset_ms"),
        "note": note,
    }


def _missing(note: str) -> dict[str, Any]:
    return {
        "kind": "MISSING",
        "event_index": None,
        "csv_line": None,
        "offset_ms": None,
        "note": note,
    }


def _reconstructed_anchor(
    anchor: dict[str, Any] | None,
    current_row: dict[str, Any],
    *,
    note: str,
) -> dict[str, Any]:
    value = dict(
        anchor
        or _raw_anchor(
            current_row,
            kind="RECONSTRUCTED",
            note=note,
        )
    )
    value["kind"] = "RECONSTRUCTED"
    value["note"] = note
    return value


def _read_row(line: str, path: Path, line_number: int) -> dict[str, Any]:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as error:
        raise FuryDecisionDatasetError(
            f"invalid normalized JSON at {path}:{line_number}: {error}"
        ) from error
    if not isinstance(row, dict):
        raise FuryDecisionDatasetError(
            f"normalized row is not an object at {path}:{line_number}"
        )
    provenance = row.get("provenance")
    if not isinstance(row.get("event_index"), int) or not isinstance(provenance, dict) or not isinstance(
        provenance.get("csv_line"), int
    ):
        raise FuryDecisionDatasetError(
            f"normalized row lacks integer event_index/csv_line at {path}:{line_number}"
        )
    return row


def _iter_normalized_rows(path: Path) -> Iterator[tuple[int, str]]:
    """Open one source once; callers may prefilter lines before JSON decoding."""

    try:
        with path.open("r", encoding="utf-8", buffering=1024 * 1024) as handle:
            for line_number, line in enumerate(handle, start=1):
                if line.strip():
                    yield line_number, line
    except (OSError, UnicodeError) as error:
        raise FuryDecisionDatasetError(f"cannot stream normalized JSONL {path}: {error}") from error


def _action_key(row: dict[str, Any]) -> tuple[str, str]:
    spell_id = row.get("spell_id")
    if isinstance(spell_id, int):
        return str(row.get("encounter") or ""), f"id:{spell_id}"
    return (
        str(row.get("encounter") or ""),
        "name:" + str(row.get("spell") or "").casefold(),
    )


def _candidate_id(row: dict[str, Any], player_guid: str) -> str:
    csv_line = (row.get("provenance") or {}).get("csv_line")
    return (
        f"{row.get('encounter')}:{_guid_key(player_guid)}:"
        f"{row.get('event_index')}:{csv_line}"
    )


def _resource_detail(row: dict[str, Any]) -> tuple[str | None, str | None, float | None]:
    detail = str(row.get("outcome") or "")
    parts = [part.strip() for part in re.split(r"[\u00b7\u2022]", detail) if part.strip()]
    direction = parts[0].casefold() if parts else None
    resource = parts[1].casefold() if len(parts) > 1 else None
    amount = parse_type_amount("RES", row.get("value"))
    return direction, resource, float(amount) if amount is not None else None


def _aura_change(row: dict[str, Any]) -> tuple[str, int | None]:
    detail = str(row.get("outcome") or "").strip()
    change_match = re.match(r"^(Added|Removed|Updated)", detail, re.IGNORECASE)
    stack_match = re.search(r"stacks=(\d+)", detail, re.IGNORECASE)
    return (
        change_match.group(1).casefold() if change_match else "unknown",
        int(stack_match.group(1)) if stack_match else None,
    )


def _compact_aura(row: dict[str, Any], change: str, stacks: int | None) -> dict[str, Any]:
    return {
        "target_guid": row.get("target_guid"),
        "spell_id": row.get("spell_id"),
        "spell_name": row.get("spell"),
        "source_guid": row.get("source_guid"),
        "change": change,
        "stacks": stacks,
        "event_index": row.get("event_index"),
    }


def _sorted_aura_values(values: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
    return [values[key] for key in sorted(values, key=lambda value: tuple(map(str, value)))]


def _state_snapshot(state: PlayerState, row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool], dict[str, dict[str, Any]]]:
    start_offset = int(row.get("offset_ms") or 0)
    last_swing_elapsed = (
        start_offset - state.last_auto_attack_offset_ms
        if state.last_auto_attack_offset_ms is not None
        and start_offset >= state.last_auto_attack_offset_ms
        else None
    )
    values: dict[str, Any] = {
        "combat_time_ms": start_offset,
        "recent_uniquely_linked_server_actions": list(state.recent_actions),
        "rage_gain_total_chronicle_units": state.rage_gain_total,
        "rage_gain_rows_observed": state.rage_gain_rows,
        "rage_loss_total_chronicle_units": (
            state.rage_loss_total if state.rage_loss_rows else None
        ),
        "rage_loss_rows_observed": state.rage_loss_rows,
        "rage_scale_to_wow": None,
        "last_auto_attack_elapsed_ms": last_swing_elapsed,
        "known_player_aura_event_ledger": {
            "initial_state_complete": False,
            "duration_complete": False,
            "entries": _sorted_aura_values(state.player_auras),
        },
        "known_outgoing_target_aura_event_ledger": {
            "initial_state_complete": False,
            "duration_complete": False,
            "entries": _sorted_aura_values(state.outgoing_target_auras),
        },
        "damaged_target_guids_seen": sorted(state.damaged_target_guids),
        "talent_tree_point_totals": state.talent_tree,
        "gear_slot_count": state.gear_slot_count,
    }
    values.update({name: None for name in MISSING_STATE_FIELDS})

    masks = {
        "combat_time_ms": True,
        "recent_uniquely_linked_server_actions": True,
        "rage_gain_total_chronicle_units": True,
        "rage_gain_rows_observed": True,
        "rage_loss_total_chronicle_units": state.rage_loss_rows > 0,
        "rage_loss_rows_observed": True,
        "rage_scale_to_wow": False,
        "last_auto_attack_elapsed_ms": last_swing_elapsed is not None,
        "known_player_aura_event_ledger": True,
        "known_outgoing_target_aura_event_ledger": True,
        "damaged_target_guids_seen": True,
        "talent_tree_point_totals": state.talent_tree is not None,
        "gear_slot_count": state.gear_slot_count is not None,
        **{name: False for name in MISSING_STATE_FIELDS},
    }
    current = _raw_anchor(row, note="current START candidate")
    provenance = {
        "combat_time_ms": current,
        "recent_uniquely_linked_server_actions": _reconstructed_anchor(
            state.last_result_anchor,
            row,
            note="prior uniquely linked GO/FAIL result ledger",
        ),
        "rage_gain_total_chronicle_units": _reconstructed_anchor(
            state.last_rage_gain_anchor,
            row,
            note="sum of prior exported Gain Rage rows; not absolute rage",
        ),
        "rage_gain_rows_observed": _reconstructed_anchor(
            state.last_rage_gain_anchor,
            row,
            note="count of prior exported Gain Rage rows",
        ),
        "rage_loss_total_chronicle_units": (
            _reconstructed_anchor(
                state.last_rage_loss_anchor,
                row,
                note="sum of prior exported Loss Rage rows; not an absolute resource anchor",
            )
            if state.rage_loss_rows
            else _missing("no prior exported Loss Rage row; absence is not zero cost")
        ),
        "rage_loss_rows_observed": _reconstructed_anchor(
            state.last_rage_loss_anchor,
            row,
            note="count of prior exported Loss Rage rows",
        ),
        "rage_scale_to_wow": _missing("Chronicle-unit to WoW-rage scale is not assumed"),
        "last_auto_attack_elapsed_ms": (
            _reconstructed_anchor(
                state.last_auto_attack_anchor,
                row,
                note="elapsed from prior observed Auto Attack to current START",
            )
            if state.last_auto_attack_anchor is not None
            else _missing("no prior observed Auto Attack timestamp")
        ),
        "known_player_aura_event_ledger": _reconstructed_anchor(
            state.last_player_aura_anchor,
            row,
            note="prior player-target Aura event ledger; initial state and durations are incomplete",
        ),
        "known_outgoing_target_aura_event_ledger": _reconstructed_anchor(
            state.last_outgoing_aura_anchor,
            row,
            note="prior outgoing target Aura event ledger; initial state and durations are incomplete",
        ),
        "damaged_target_guids_seen": _reconstructed_anchor(
            state.last_damage_anchor,
            row,
            note="encounter-to-date outgoing damage targets; not simultaneous target count",
        ),
        "talent_tree_point_totals": (
            _reconstructed_anchor(
                state.context_anchor,
                row,
                note="parsed from prior WARRIOR Combatant Info detail",
            )
            if state.context_anchor is not None
            else _missing("no prior parseable WARRIOR INFO talent-tree totals")
        ),
        "gear_slot_count": (
            _reconstructed_anchor(
                state.context_anchor,
                row,
                note="parsed from prior WARRIOR Combatant Info detail",
            )
            if state.context_anchor is not None
            else _missing("no prior parseable WARRIOR INFO gear-slot count")
        ),
    }
    provenance.update(
        {
            name: _missing(
                "not directly observed or safely reconstructed in Chronicle normalized CSV"
            )
            for name in MISSING_STATE_FIELDS
        }
    )
    return values, masks, provenance


def _close_window(state: PlayerState, next_start: dict[str, Any] | None) -> None:
    if state.open_decision is None:
        return
    window = state.open_decision["window_until_next_start_candidate"]
    window["next_start_anchor"] = (
        _raw_anchor(next_start, note="exclusive next START boundary")
        if next_start is not None
        else None
    )
    state.open_decision = None


def _start_decision(
    state: PlayerState,
    row: dict[str, Any],
    actions: dict[int, dict[str, str]],
    normalized_file: Path,
) -> None:
    _close_window(state, row)
    decision_id = _candidate_id(row, state.target.player_guid)
    spell_id = row.get("spell_id") if isinstance(row.get("spell_id"), int) else None
    catalog = actions.get(spell_id) if spell_id is not None else None
    lane = catalog["lane"] if catalog else "unknown"
    state_values, state_mask, state_provenance = _state_snapshot(state, row)
    start_anchor = _raw_anchor(row, note="server-observed START candidate")
    record = {
        "schema_version": SCHEMA_VERSION,
        "schema": RECORD_SCHEMA,
        "identity": {
            "source_instance_ref": row.get("instance"),
            "encounter_id": state.target.encounter_id,
            "player_guid": state.target.player_guid,
            "player_name": state.target.player_name,
            "board_spec": "Fury",
            "leaderboard_rows": list(state.target.fury_leaderboard_rows),
            "identity_status": state.target.identity_status,
        },
        "source": {
            "normalized_file": str(normalized_file),
            "start_anchor": start_anchor,
        },
        "action": {
            "decision_id": decision_id,
            "semantics": "server_observed_START_candidate",
            "spell_id": spell_id,
            "spell_name": row.get("spell"),
            "target_guid": row.get("target_guid"),
            "target_name": row.get("target"),
            "policy_action_key": catalog["policy_action_key"] if catalog else None,
            "catalog_status": "MAPPED_ACTIVE" if catalog else "UNMAPPED",
            "lane": lane,
        },
        "result": {
            "association": "unlinked",
            "status": "missing",
            "anchor": None,
            "start_to_result_ms": None,
            "candidate_action_ids": [decision_id],
        },
        "eligibility": {
            "observable_behavior_label": False,
            "partial_observation_bc": False,
            "full_state_bc": False,
            "offline_rl": False,
            "queue_intent_supervision": False,
            "causal_reward_supervision": False,
            "readiness_full_state_ready": state.target.full_state_ready,
            "full_state_blockers": list(state.target.full_state_blockers),
        },
        "state_before": state_values,
        "state_mask": state_mask,
        "state_provenance": state_provenance,
        "window_until_next_start_candidate": {
            "outgoing_damage_observed": 0.0,
            "rage_gain_chronicle_units": 0.0,
            "auto_attack_rows": 0,
            "next_start_anchor": None,
            "causal_reward": None,
            "causal_attribution": "MISSING",
        },
    }
    state.decisions.append(record)
    state.decisions_by_id[decision_id] = record
    state.pending[_action_key(row)].append(decision_id)
    state.open_decision = record


def _result_event(state: PlayerState, row: dict[str, Any]) -> None:
    candidate_ids = state.pending.pop(_action_key(row), [])
    if not candidate_ids:
        return
    association = "unique" if len(candidate_ids) == 1 else "ambiguous"
    status = "succeeded" if str(row.get("type") or "").upper() == "GO" else "failed"
    result_anchor = _raw_anchor(row, note=f"server-observed {row.get('type')} result")
    for candidate_id in candidate_ids:
        decision = state.decisions_by_id[candidate_id]
        start_offset = int(decision["source"]["start_anchor"].get("offset_ms") or 0)
        result_offset = int(row.get("offset_ms") or 0)
        decision["result"] = {
            "association": association,
            "status": status,
            "anchor": result_anchor,
            "start_to_result_ms": (
                result_offset - start_offset if result_offset >= start_offset else None
            ),
            "candidate_action_ids": list(candidate_ids),
        }
        behavior = (
            association == "unique"
            and status == "succeeded"
            and decision["action"]["catalog_status"] == "MAPPED_ACTIVE"
            and decision["action"]["lane"] != "on_swing_unknown_intent"
        )
        decision["eligibility"]["observable_behavior_label"] = behavior
        decision["eligibility"]["partial_observation_bc"] = behavior
        decision["eligibility"]["full_state_bc"] = (
            behavior and state.target.full_state_ready
        )
    if association == "unique":
        decision = state.decisions_by_id[candidate_ids[0]]
        state.recent_actions.append(
            {
                "decision_id": candidate_ids[0],
                "spell_id": decision["action"]["spell_id"],
                "spell_name": decision["action"]["spell_name"],
                "result": status,
                "result_event_index": row.get("event_index"),
            }
        )
        state.recent_actions = state.recent_actions[-10:]
        state.last_result_anchor = result_anchor


def _observe_damage(state: PlayerState, row: dict[str, Any]) -> None:
    amount = parse_type_amount(str(row.get("type") or ""), row.get("value"))
    numeric = float(amount) if amount is not None else 0.0
    auto_attack = row.get("spell_id") == 6603 or str(
        row.get("spell") or ""
    ).casefold() == "auto attack"
    if state.open_decision is not None:
        window = state.open_decision["window_until_next_start_candidate"]
        window["outgoing_damage_observed"] += numeric
        if auto_attack:
            window["auto_attack_rows"] += 1
    target_guid = str(row.get("target_guid") or "").strip()
    if target_guid:
        state.damaged_target_guids.add(target_guid)
    state.last_damage_anchor = _raw_anchor(row, note="prior outgoing damage event")
    if auto_attack:
        state.last_auto_attack_offset_ms = int(row.get("offset_ms") or 0)
        state.last_auto_attack_anchor = _raw_anchor(
            row, note="prior observed Auto Attack timestamp"
        )


def _observe_resource(state: PlayerState, row: dict[str, Any]) -> None:
    direction, resource, amount = _resource_detail(row)
    if resource != "rage" or amount is None:
        return
    if direction == "gain":
        if state.open_decision is not None:
            state.open_decision["window_until_next_start_candidate"][
                "rage_gain_chronicle_units"
            ] += amount
        state.rage_gain_total += amount
        state.rage_gain_rows += 1
        state.last_rage_gain_anchor = _raw_anchor(
            row, note="prior observed gain Rage delta"
        )
    elif direction == "loss":
        state.rage_loss_total += amount
        state.rage_loss_rows += 1
        state.last_rage_loss_anchor = _raw_anchor(
            row, note="prior observed loss Rage delta"
        )
    else:
        return


def _observe_context(state: PlayerState, row: dict[str, Any]) -> None:
    parsed = parse_combatant_info(row.get("outcome"))
    if parsed is None or str(parsed.get("class") or "").upper() != "WARRIOR":
        return
    state.talent_tree = list(parsed["talent_tree"])
    state.gear_slot_count = int(parsed["gear_slot_count"])
    state.context_anchor = _raw_anchor(
        row, note="prior parseable WARRIOR Combatant Info"
    )


def _observe_aura(
    state: PlayerState,
    row: dict[str, Any],
    *,
    player_target: bool,
    outgoing_target: bool,
) -> None:
    change, stacks = _aura_change(row)
    compact = _compact_aura(row, change, stacks)
    spell = str(row.get("spell_id") or row.get("spell") or "")
    source = str(row.get("source_guid") or row.get("source") or "")
    target = str(row.get("target_guid") or row.get("target") or "")
    if player_target:
        key = (spell, source)
        if change == "removed":
            state.player_auras.pop(key, None)
        else:
            state.player_auras[key] = compact
        state.last_player_aura_anchor = _raw_anchor(
            row, note="prior player-target Aura event"
        )
    if outgoing_target:
        key = (target, spell, source)
        if change == "removed":
            state.outgoing_target_auras.pop(key, None)
        else:
            state.outgoing_target_auras[key] = compact
        state.last_outgoing_aura_anchor = _raw_anchor(
            row, note="prior outgoing target Aura event"
        )


def _partition_base(path: Path) -> str:
    return _safe_component(path.stem)


def _write_partition(
    output_dir: Path,
    source: Path,
    records: list[dict[str, Any]],
) -> tuple[Path, Path]:
    final_path = output_dir / f"{_partition_base(source)}.jsonl.gz"
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{_partition_base(source)}__",
        suffix=".jsonl.gz.tmp",
        dir=output_dir,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with gzip.open(temporary, mode="wt", encoding="utf-8", newline="\n") as handle:
            for record in records:
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary, final_path


def _build_partition(
    job: SourceJob,
    actions: dict[int, dict[str, str]],
    output_dir: Path,
) -> PartitionBuild:
    by_encounter: dict[str, dict[str, PlayerState]] = defaultdict(dict)
    all_states: list[PlayerState] = []
    for target in job.targets:
        state = PlayerState(target)
        by_encounter[target.encounter_id][_guid_key(target.player_guid)] = state
        all_states.append(state)

    guid_tokens = tuple(
        sorted({target.player_guid for target in job.targets}, key=len, reverse=True)
    )
    lines_scanned = 0
    rows_decoded = 0
    event_counts: Counter[str] = Counter()
    instance_refs: set[str] = set()
    for line_number, line in _iter_normalized_rows(job.normalized_file):
        lines_scanned += 1
        if not any(guid in line for guid in guid_tokens):
            continue
        row = _read_row(line, job.normalized_file, line_number)
        rows_decoded += 1
        encounter = str(row.get("encounter") or "")
        states = by_encounter.get(encounter)
        if not states:
            continue
        source_state = states.get(_guid_key(row.get("source_guid")))
        target_state = states.get(_guid_key(row.get("target_guid")))
        involved = {id(value): value for value in (source_state, target_state) if value}
        if not involved:
            continue
        for state in involved.values():
            state.selected_source_rows += 1
        instance_ref = str(row.get("instance") or "").strip()
        if instance_ref:
            instance_refs.add(instance_ref)
        event_type = str(row.get("type") or "").upper()
        event_counts[event_type] += 1

        if event_type == "INFO" and target_state is not None:
            _observe_context(target_state, row)
        elif event_type == "RES":
            resource_owner = (
                target_state
                if str(row.get("target_guid") or "").strip()
                else source_state
            )
            if resource_owner is not None:
                _observe_resource(resource_owner, row)
        elif event_type in ("DMG", "DEAD") and source_state is not None:
            _observe_damage(source_state, row)
        elif event_type == "AURA":
            if target_state is not None:
                _observe_aura(
                    target_state,
                    row,
                    player_target=True,
                    outgoing_target=False,
                )
            if source_state is not None and source_state is not target_state:
                _observe_aura(
                    source_state,
                    row,
                    player_target=False,
                    outgoing_target=True,
                )
        elif event_type in ("GO", "FAIL") and source_state is not None:
            _result_event(source_state, row)
        elif event_type == "START" and source_state is not None:
            _start_decision(source_state, row, actions, job.normalized_file)

    records: list[dict[str, Any]] = []
    unresolved_candidates = 0
    future_state_anchors = 0
    non_null_missing_fields = 0
    for state in all_states:
        _close_window(state, None)
        unresolved_candidates += sum(len(values) for values in state.pending.values())
        for record in state.decisions:
            start = int(record["source"]["start_anchor"]["event_index"])
            for evidence in record["state_provenance"].values():
                event_index = evidence.get("event_index")
                if isinstance(event_index, int) and event_index > start:
                    future_state_anchors += 1
            non_null_missing_fields += sum(
                1
                for name in MISSING_STATE_FIELDS
                if record["state_before"][name] is not None
                or record["state_mask"][name] is not False
            )
            records.append(record)
    if future_state_anchors:
        raise FuryDecisionDatasetError(
            f"future state evidence detected in {future_state_anchors} fields of {job.normalized_file}"
        )
    if non_null_missing_fields:
        raise FuryDecisionDatasetError(
            f"MISSING state fields were populated {non_null_missing_fields} times in {job.normalized_file}"
        )
    records.sort(
        key=lambda value: (
            int(value["source"]["start_anchor"]["event_index"]),
            int(value["source"]["start_anchor"]["csv_line"]),
            _guid_key(value["identity"]["player_guid"]),
        )
    )
    temporary, final = _write_partition(output_dir, job.normalized_file, records)
    observable = sum(
        1 for record in records if record["eligibility"]["observable_behavior_label"]
    )
    mapped = sum(
        1 for record in records if record["action"]["catalog_status"] == "MAPPED_ACTIVE"
    )
    action_lane_counts = Counter(record["action"]["lane"] for record in records)
    mapped_unknown_lane_decisions = sum(
        1
        for record in records
        if record["action"]["catalog_status"] == "MAPPED_ACTIVE"
        and record["action"]["lane"] == "unknown"
    )
    if mapped_unknown_lane_decisions:
        raise FuryDecisionDatasetError(
            f"{mapped_unknown_lane_decisions} mapped actions lack a queue/GCD/off-GCD "
            f"lane in {job.normalized_file}"
        )
    entry = {
        "normalized_file": str(job.normalized_file),
        "normalized_size_bytes": job.normalized_file.stat().st_size,
        "partition": str(final),
        "compressed_size_bytes": temporary.stat().st_size,
        "source_scan_count": 1,
        "source_lines_scanned": lines_scanned,
        "source_rows_decoded_after_guid_prefilter": rows_decoded,
        "selected_source_rows": sum(state.selected_source_rows for state in all_states),
        "selected_player_encounters": len(job.targets),
        "selected_players": len({_guid_key(target.player_guid) for target in job.targets}),
        "selected_encounters": len({target.encounter_id for target in job.targets}),
        "source_instance_refs": sorted(instance_refs),
        "decision_count": len(records),
        "mapped_active_decisions": mapped,
        "unmapped_decisions": len(records) - mapped,
        "observable_behavior_labels": observable,
        "unresolved_candidates": unresolved_candidates,
        "action_lane_counts": dict(sorted(action_lane_counts.items())),
        "mapped_unknown_lane_decisions": mapped_unknown_lane_decisions,
        "selected_event_type_counts": dict(sorted(event_counts.items())),
        "state_future_leakage_count": future_state_anchors,
        "non_null_missing_fields": non_null_missing_fields,
    }
    return PartitionBuild(temporary, final, entry)


def _scan_jobs(
    jobs: list[SourceJob],
    actions: dict[int, dict[str, str]],
    output_dir: Path,
    workers: int,
) -> list[PartitionBuild]:
    if workers < 1:
        raise FuryDecisionDatasetError("workers must be at least 1")
    if workers == 1 or len(jobs) == 1:
        return [_build_partition(job, actions, output_dir) for job in jobs]
    results: dict[int, PartitionBuild] = {}
    with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
        futures = {
            executor.submit(_build_partition, job, actions, output_dir): index
            for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [results[index] for index in range(len(jobs))]


def _write_manifest_temporary(output_dir: Path, manifest: dict[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=".manifest__",
        suffix=".json.tmp",
        dir=output_dir,
        delete=False,
    ) as handle:
        path = Path(handle.name)
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def build_fury_decision_dataset(
    readiness_report: str | Path = DEFAULT_READINESS,
    *,
    registry: str | Path = DEFAULT_REGISTRY,
    output_dir: str | Path = DEFAULT_OUTPUT,
    workers: int = 1,
) -> FuryDecisionDatasetResult:
    readiness_path = Path(readiness_report).expanduser().resolve()
    registry_path = Path(registry).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()
    report = _load_object(readiness_path, "reconstruction-readiness report")
    jobs, selection = _select_jobs(report, readiness_path)
    del report
    actions, registry_info = _registry_actions(registry_path)
    output_path.mkdir(parents=True, exist_ok=True)

    builds: list[PartitionBuild] = []
    manifest_temporary: Path | None = None
    try:
        builds = _scan_jobs(jobs, actions, output_path, workers)
        decision_count = sum(value.manifest_entry["decision_count"] for value in builds)
        if decision_count == 0:
            raise FuryDecisionDatasetError(
                "eligible Fury targets produced no START decision candidates"
            )
        observable_count = sum(
            value.manifest_entry["observable_behavior_labels"] for value in builds
        )
        action_lane_counts: Counter[str] = Counter()
        for value in builds:
            action_lane_counts.update(value.manifest_entry["action_lane_counts"])
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "schema": SCHEMA_NAME,
            "kind": "chronicle_fury_decision_dataset_manifest",
            "generated_at": _utc_now(),
            "inputs": {
                "readiness_report": str(readiness_path),
                "registry": registry_info,
                "normalized_inputs_opened_read_only": True,
                "partial_trajectory_intermediate": None,
                "workers": workers,
            },
            "selection": {
                **selection,
                "rule": "identity.verified == true AND leaderboard_rows.board_spec == Fury",
            },
            "output": {
                "format": "gzip-compressed JSON Lines",
                "partition_grain": "one partition per normalized source",
                "partition_count": len(builds),
                "decision_count": decision_count,
                "observable_behavior_labels": observable_count,
                "action_lane_counts": dict(sorted(action_lane_counts.items())),
            },
            "quality": {
                "state_future_leakage_count": sum(
                    value.manifest_entry["state_future_leakage_count"] for value in builds
                ),
                "non_null_missing_fields": sum(
                    value.manifest_entry["non_null_missing_fields"] for value in builds
                ),
                "queue_intent_labels": 0,
                "causal_reward_rows": 0,
                "mapped_unknown_lane_decisions": sum(
                    value.manifest_entry["mapped_unknown_lane_decisions"]
                    for value in builds
                ),
                "queue_intent_policy": "MISSING; server START/GO does not reveal client queue intent",
                "reward_policy": "window outcomes are observed, not causally attributed to the candidate action",
                "offline_rl_ready": False,
                "training_scope": "partial-observation server-behavior analysis and BC labels only",
            },
            "partitions": [value.manifest_entry for value in builds],
        }
        manifest_temporary = _write_manifest_temporary(output_path, manifest)
        for value in builds:
            value.temporary_path.replace(value.final_path)
        manifest_path = output_path / "manifest.json"
        manifest_temporary.replace(manifest_path)
        manifest_temporary = None
    finally:
        for value in builds:
            value.temporary_path.unlink(missing_ok=True)
        if manifest_temporary is not None:
            manifest_temporary.unlink(missing_ok=True)

    partitions = tuple(value.final_path for value in builds)
    return FuryDecisionDatasetResult(
        decision_count=decision_count,
        partition_count=len(partitions),
        manifest=manifest_path,
        partitions=partitions,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build compact identity-verified Fury decision partitions directly "
            "from normalized Chronicle JSONL."
        )
    )
    parser.add_argument(
        "--readiness-report", type=Path, default=DEFAULT_READINESS
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="normalized-source workers; each source is still opened exactly once",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_fury_decision_dataset(
            args.readiness_report,
            registry=args.registry,
            output_dir=args.output_dir,
            workers=args.workers,
        )
    except FuryDecisionDatasetError as error:
        print(f"Fury decision-dataset ETL failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
