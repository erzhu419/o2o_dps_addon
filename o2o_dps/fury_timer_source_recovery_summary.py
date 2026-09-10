"""Build a compact Fury timer contract from frozen source/recovery evidence.

The source campaign supplies stages A-C.  A separate stage-D recovery run
supplies Heroic Strike cancellation and loadout restoration.  This decoder
validates that composite boundary before projecting only the observations that
are useful to a later simulator comparison.  It never changes simulator data.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import statistics
import sys
from typing import Any


SCHEMA = "fury_timer_source_recovery_summary/v1"
SCHEMA_VERSION = 1
KIND = "fury_timer_source_recovery_summary"
COMPOSITE_KIND = "brainofcat_timer_source_recovery_composite"
FLURRY_SPEED_MULTIPLIER = 1.30
HEROIC_STRIKE_SPELL_ID = 25286
FLURRY_SPELL_ID = 12970
AUTO_ATTACK_SPELL_ID = 6603
COMBAT_NEIGHBOR_SEQUENCES = (34, 40, 46)
COMBAT_NEIGHBOR_RADIUS = 1

_O2O_ROOT = Path(__file__).resolve().parents[1]
_WOW_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_COMPOSITE = (
    _O2O_ROOT
    / "offline_data"
    / "timer_calibration_debug"
    / "timer-recovery-_-252585301-1"
    / "composite_evidence.json"
)
DEFAULT_COMBAT_LOG = _WOW_ROOT / "Logs" / "WoWCombatLog.txt"
DEFAULT_OUTPUT = (
    _O2O_ROOT
    / "offline_data"
    / "timer_calibration_summaries"
    / "fury_timer_source_recovery_summary_v1.json"
)

REQUIRED_COMPOSITE_CHECKS = frozenset(
    {
        "source_run_link_resolved",
        "source_timer_stage_completed",
        "source_swing_stage_completed",
        "source_haste_stage_completed",
        "source_reached_stage_d",
        "recovery_start_marker",
        "recovery_cancel_completed_marker",
        "recovery_terminal_marker",
        "recovery_cancel_completed",
        "recovery_strong_cancel_support",
        "recovery_next_main_hand_white",
        "recovery_off_hand_continued",
        "recovery_target_switch_resolved",
        "recovery_loadout_restored",
    }
)


class FuryTimerSourceRecoverySummaryError(ValueError):
    """Frozen source/recovery evidence failed a structural or semantic gate."""


@dataclass(frozen=True)
class FuryTimerSourceRecoverySummaryResult:
    status: str
    output: Path
    source_campaign_run_id: str
    recovery_campaign_run_id: str
    live_contract_complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output": str(self.output),
            "schema": SCHEMA,
            "kind": KIND,
            "source_campaign_run_id": self.source_campaign_run_id,
            "recovery_campaign_run_id": self.recovery_campaign_run_id,
            "live_contract_complete": self.live_contract_complete,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _fail(message: str) -> None:
    raise FuryTimerSourceRecoverySummaryError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"{label} must be an integer")
    return value


def _number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(f"{label} must be numeric")
    result = float(value)
    if not (-sys.float_info.max <= result <= sys.float_info.max):
        _fail(f"{label} must be finite")
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryTimerSourceRecoverySummaryError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        _fail(f"{label} must contain one JSON object: {path}")
    return value


def _load_trace(path: Path, label: str) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    physical_lines = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for physical_lines, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FuryTimerSourceRecoverySummaryError(
                        f"invalid {label} JSONL at {path}:{physical_lines}: {error.msg}"
                    ) from error
                if not isinstance(row, dict):
                    _fail(f"{label} row {physical_lines} is not an object")
                rows.append(row)
    except (OSError, UnicodeError) as error:
        raise FuryTimerSourceRecoverySummaryError(
            f"cannot read {label} {path}: {error}"
        ) from error
    _require(bool(rows), f"{label} is empty: {path}")
    sequences = [_integer(row.get("sequence"), f"{label} sequence") for row in rows]
    _require(
        all(current > prior for prior, current in zip(sequences, sequences[1:])),
        f"{label} sequence values must be unique and strictly increasing",
    )
    return rows, physical_lines


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.expanduser().resolve())))


def _linked_trace_path(
    composite: Mapping[str, Any], *, key: str, bundle_key: str
) -> Path:
    files = _mapping(composite.get("files"), "composite.files")
    raw_trace = files.get(key)
    raw_bundle = composite.get(bundle_key)
    _require(
        isinstance(raw_trace, str) and bool(raw_trace.strip()),
        f"composite.files.{key} must be a path",
    )
    _require(
        isinstance(raw_bundle, str) and bool(raw_bundle.strip()),
        f"composite.{bundle_key} must be a path",
    )
    trace = Path(raw_trace).expanduser().resolve()
    expected = Path(raw_bundle).expanduser().resolve() / "trace.jsonl"
    _require(
        _path_key(trace) == _path_key(expected),
        f"composite {key} does not link exactly to {bundle_key}/trace.jsonl",
    )
    _require(trace.is_file(), f"linked {key} does not exist: {trace}")
    return trace


def _validate_composite(
    composite_path: Path,
) -> tuple[dict[str, Any], Path, Path, str, str]:
    composite = _load_json(composite_path, "timer source/recovery composite")
    _require(composite.get("schema_version") == 1, "composite schema_version must be 1")
    _require(composite.get("kind") == COMPOSITE_KIND, "composite kind is not exact")
    _require(composite.get("status") == "complete", "composite status is not complete")
    checks = _mapping(composite.get("checks"), "composite.checks")
    actual_keys = frozenset(checks)
    _require(
        actual_keys == REQUIRED_COMPOSITE_CHECKS,
        "composite must contain exactly the 14 required checks",
    )
    failed = sorted(name for name in REQUIRED_COMPOSITE_CHECKS if checks.get(name) is not True)
    _require(not failed, f"composite checks are not all true: {', '.join(failed)}")
    source_run = composite.get("source_campaign_run_id")
    recovery_run = composite.get("recovery_campaign_run_id")
    _require(
        isinstance(source_run, str) and source_run.startswith("timer-campaign-"),
        "source campaign run id must start with timer-campaign-",
    )
    _require(
        isinstance(recovery_run, str) and recovery_run.startswith("timer-recovery-"),
        "recovery campaign run id must start with timer-recovery-",
    )
    boundary = _mapping(composite.get("evidence_boundary"), "composite.evidence_boundary")
    _require(
        boundary.get("source") == "stages_A_to_C_and_failed_legacy_D_trials",
        "composite source evidence boundary is not exact",
    )
    _require(
        boundary.get("recovery") == "stage_D_and_loadout_restore",
        "composite recovery evidence boundary is not exact",
    )
    _require(
        composite.get("loadout_provenance") == "operator_supplied_explicit_item_ids",
        "recovery loadout provenance is not explicit operator item ids",
    )
    source_trace = _linked_trace_path(
        composite, key="source_trace", bundle_key="source_bundle"
    )
    recovery_trace = _linked_trace_path(
        composite, key="recovery_trace", bundle_key="recovery_bundle"
    )
    return composite, source_trace, recovery_trace, source_run, recovery_run


def _source(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("source")
    return value if isinstance(value, Mapping) else {}


def _details(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _source(row).get("details")
    return value if isinstance(value, Mapping) else {}


def _marker_rows(
    rows: Sequence[Mapping[str, Any]], name: str
) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("kind") == "MARKER" and row.get("name") == name]


def _event_rows(
    rows: Sequence[Mapping[str, Any]], name: str
) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("kind") == "EVENT" and row.get("name") == name]


def _one_marker(rows: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    matches = _marker_rows(rows, name)
    _require(len(matches) == 1, f"requires exactly one {name}; found {len(matches)}")
    return matches[0]


def _debug_sequence(row: Mapping[str, Any]) -> int:
    return _integer(row.get("sequence"), "debug sequence")


def _calibration_sequence(row: Mapping[str, Any]) -> int:
    return _integer(_source(row).get("calibrationSequence"), "calibration sequence")


def _event_time(row: Mapping[str, Any]) -> float:
    source = _source(row)
    value = source.get("eventTime", source.get("markerTime", row.get("time")))
    return _number(value, "trace event time")


def _paired_event(
    marker: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], expected_name: str | None = None
) -> Mapping[str, Any]:
    source_sequence = _integer(_source(marker).get("sourceSequence"), "marker sourceSequence")
    matches = [
        row
        for row in rows
        if row.get("kind") == "EVENT"
        and _source(row).get("calibrationSequence") == source_sequence
    ]
    _require(
        len(matches) == 1,
        f"marker {marker.get('name')} sourceSequence {source_sequence} must link one event",
    )
    event = matches[0]
    if expected_name is not None:
        _require(
            event.get("name") == expected_name,
            f"marker {marker.get('name')} must link {expected_name}, got {event.get('name')}",
        )
    return event


def _selected(mapping: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: mapping[field] for field in fields if field in mapping}


def _stage_a(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    started = _one_marker(rows, "CALIBRATION_TIMER_CAMPAIGN_STARTED")
    completed = _one_marker(rows, "CALIBRATION_TIMER_STAGE_COMPLETED")
    _require(_debug_sequence(started) < _debug_sequence(completed), "timer stage order is invalid")
    result: dict[str, Any] = {}
    expected_duration = {"sunder": 1.5, "bloodthirst": 6.0}
    for spell_key in ("sunder", "bloodthirst"):
        chains = [
            row
            for row in _marker_rows(rows, "CALIBRATION_TIMER_CHAIN_COMPLETED")
            if _details(row).get("spellKey") == spell_key
        ]
        _require(len(chains) == 3, f"{spell_key} requires exactly three completed timer chains")
        attempts: list[int] = []
        chain_output: list[dict[str, Any]] = []
        for chain in chains:
            details = _details(chain)
            attempt = _integer(details.get("attempt"), f"{spell_key} chain attempt")
            attempts.append(attempt)
            for flag in (
                "clientCastSeen",
                "startSeen",
                "serverGoSeen",
                "cooldownEventSeen",
                "monotonicToZero",
            ):
                _require(details.get(flag) is True, f"{spell_key} attempt {attempt} lacks {flag}")
            retry_required = spell_key == "bloodthirst"
            _require(
                details.get("retryRequired") is retry_required,
                f"{spell_key} attempt {attempt} retryRequired mismatch",
            )
            for flag in ("retryRequested", "retryFailureSeen", "retryNoExtension"):
                _require(
                    details.get(flag) is retry_required,
                    f"{spell_key} attempt {attempt} {flag} mismatch",
                )
            first_rows = [
                row
                for row in _marker_rows(rows, "CALIBRATION_TIMER_FIRST_NONZERO")
                if _details(row).get("spellKey") == spell_key
                and _details(row).get("attempt") == attempt
                and _debug_sequence(row) < _debug_sequence(chain)
            ]
            _require(bool(first_rows), f"{spell_key} attempt {attempt} lacks first-nonzero marker")
            first = _details(first_rows[-1])
            duration = _number(first.get("spellbookDuration"), "spellbook duration")
            _require(
                abs(duration - expected_duration[spell_key]) <= 0.01,
                f"{spell_key} spellbook duration is not {expected_duration[spell_key]}",
            )
            chain_output.append(
                {
                    "attempt": attempt,
                    "completion_debug_sequence": _debug_sequence(chain),
                    "first_nonzero_debug_sequence": _debug_sequence(first_rows[-1]),
                    "first_nonzero": _selected(
                        first,
                        (
                            "sourceEvent",
                            "spellbookDuration",
                            "spellbookRemaining",
                            "cat2GCDRemaining",
                            "cat2SpellCooldownRemaining",
                            "elapsedFromAction",
                            "elapsedFromServerGo",
                        ),
                    ),
                    "active_retry_required": retry_required,
                }
            )
            chain_output[-1]["first_nonzero"]["sourceEvent"] = _source(
                first_rows[-1]
            ).get("sourceEvent")
        _require(len(set(attempts)) == 3, f"{spell_key} completed attempts are not unique")
        all_first = [
            row
            for row in _marker_rows(rows, "CALIBRATION_TIMER_FIRST_NONZERO")
            if _details(row).get("spellKey") == spell_key
        ]
        retry_output: list[dict[str, Any]] = []
        if spell_key == "bloodthirst":
            retries = _marker_rows(rows, "CALIBRATION_TIMER_ACTIVE_RETRY_CONFIRMED")
            retries = [row for row in retries if _details(row).get("spellKey") == spell_key]
            _require(len(retries) == 3, "bloodthirst requires exactly three active retry confirmations")
            for retry in retries:
                details = _details(retry)
                before = _number(details.get("remainingBeforeRetry"), "retry remaining before")
                after = _number(details.get("remainingAfterObservation"), "retry remaining after")
                delta = _number(details.get("timerEndDelta"), "retry timer end delta")
                _require(details.get("retryEvidenceKind") == "explicit_failure_event", "retry evidence is not an explicit failure event")
                _require(after < before, "bloodthirst retry timer did not continue decreasing")
                _require(abs(delta) <= 0.01, "bloodthirst retry extended the timer end")
                retry_output.append(
                    {
                        "attempt": details.get("attempt"),
                        "debug_sequence": _debug_sequence(retry),
                        "evidence_kind": details.get("retryEvidenceKind"),
                        "remaining_before_seconds": before,
                        "remaining_after_seconds": after,
                        "timer_end_delta_seconds": delta,
                    }
                )
        result[spell_key] = {
            "completed_chain_count": 3,
            "completed_attempts": attempts,
            "first_nonzero_observation_count": len(all_first),
            "chains": chain_output,
            "active_retry_confirmations": retry_output,
        }
    return {
        "complete": True,
        "start_debug_sequence": _debug_sequence(started),
        "completed_debug_sequence": _debug_sequence(completed),
        "spells": result,
    }


def _locked_loadout(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidates: list[Mapping[str, Any]] = []
    for row in rows:
        if row.get("kind") != "TIMEOUT":
            continue
        snapshot = row.get("snapshot")
        if not isinstance(snapshot, Mapping):
            continue
        gate = snapshot.get("gate")
        if isinstance(gate, Mapping) and gate.get("name") == "dual_wield_intervals":
            candidates.append(row)
    _require(bool(candidates), "source trace lacks dual_wield_intervals TIMEOUT snapshot")
    gate = _mapping(_mapping(candidates[-1].get("snapshot"), "timeout snapshot").get("gate"), "timeout gate")
    observed = _mapping(gate.get("observed"), "dual-wield observed gate")
    output = {
        "main_hand_item_id": _integer(observed.get("mainHandItemID"), "locked main item"),
        "off_hand_item_id": _integer(observed.get("offHandItemID"), "locked off item"),
        "main_hand_speed_seconds": _number(observed.get("lockedMainSpeed"), "locked main speed"),
        "off_hand_speed_seconds": _number(observed.get("lockedOffSpeed"), "locked off speed"),
        "source_debug_sequence": _debug_sequence(candidates[-1]),
        "source_kind": "TIMEOUT_snapshot",
    }
    _require(output["main_hand_speed_seconds"] > 0, "locked main speed must be positive")
    _require(output["off_hand_speed_seconds"] > 0, "locked off speed must be positive")
    return output


def _anchor_event(
    marker: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    event = _paired_event(marker, rows, "UNIT_CASTEVENT")
    source = _source(event)
    hand = source.get("castKind")
    _require(hand in {"MAINHAND", "OFFHAND"}, "swing anchor has no resolved hand")
    _require(source.get("spellID") == AUTO_ATTACK_SPELL_ID, "swing anchor is not spell 6603")
    return {
        "hand": "main_hand" if hand == "MAINHAND" else "off_hand",
        "event_time": _event_time(event),
        "event_calibration_sequence": _calibration_sequence(event),
        "marker_debug_sequence": _debug_sequence(marker),
    }


def _intervals(values: Sequence[float]) -> list[float]:
    return [current - prior for prior, current in zip(values, values[1:])]


def _small_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "minimum": None, "median": None, "maximum": None}
    return {
        "count": len(values),
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _stage_b(rows: Sequence[Mapping[str, Any]], loadout: Mapping[str, Any]) -> dict[str, Any]:
    stop = _one_marker(rows, "CALIBRATION_AUTO_ATTACK_STOP_REQUESTED")
    restart = _one_marker(rows, "CALIBRATION_AUTO_ATTACK_RESTART_REQUESTED")
    haste_start = _one_marker(rows, "CALIBRATION_HASTE_STAGE_STARTED")
    _require(
        _debug_sequence(stop) < _debug_sequence(restart) < _debug_sequence(haste_start),
        "stop/restart/haste stage order is invalid",
    )
    anchors = [
        _anchor_event(marker, rows)
        for marker in _marker_rows(rows, "CALIBRATION_SWING_ANCHOR")
        if _debug_sequence(marker) < _debug_sequence(haste_start)
    ]
    pre_stop: dict[str, list[dict[str, Any]]] = {"main_hand": [], "off_hand": []}
    post_restart: dict[str, list[dict[str, Any]]] = {"main_hand": [], "off_hand": []}
    for anchor in anchors:
        sequence = anchor["marker_debug_sequence"]
        hand = str(anchor["hand"])
        if sequence < _debug_sequence(stop):
            pre_stop[hand].append(anchor)
        elif _debug_sequence(restart) < sequence < _debug_sequence(haste_start):
            post_restart[hand].append(anchor)
    interval_output: dict[str, Any] = {}
    for hand in ("main_hand", "off_hand"):
        times = [item["event_time"] for item in pre_stop[hand]]
        values = _intervals(times)
        _require(len(values) >= 3, f"stage B lacks three accepted {hand} intervals")
        _require(bool(post_restart[hand]), f"stage B lacks post-restart {hand} anchor")
        interval_output[hand] = {
            "anchor_count": len(times),
            "intervals_seconds": values,
            "statistics_seconds": _small_stats(values),
        }
    return {
        "complete": True,
        "locked_loadout": dict(loadout),
        "baseline": interval_output,
        "stop_restart": {
            "stop_debug_sequence": _debug_sequence(stop),
            "stop_time": _event_time(stop),
            "restart_debug_sequence": _debug_sequence(restart),
            "restart_time": _event_time(restart),
            "post_restart_first_anchor": {
                hand: post_restart[hand][0] for hand in ("main_hand", "off_hand")
            },
        },
        "stage_completion_evidence": {
            "next_stage_marker": "CALIBRATION_HASTE_STAGE_STARTED",
            "debug_sequence": _debug_sequence(haste_start),
        },
    }


def _boundary_prediction(
    *,
    boundary_kind: str,
    boundary_time: float,
    hand: str,
    previous_anchor: Mapping[str, Any],
    next_anchor: Mapping[str, Any],
    locked_speed: float,
) -> dict[str, Any]:
    hasted_speed = locked_speed / FLURRY_SPEED_MULTIPLIER
    old_speed = locked_speed if boundary_kind == "added" else hasted_speed
    new_speed = hasted_speed if boundary_kind == "added" else locked_speed
    previous_time = _number(previous_anchor.get("event_time"), "previous boundary anchor")
    observed_time = _number(next_anchor.get("event_time"), "next boundary anchor")
    elapsed = boundary_time - previous_time
    _require(0 <= elapsed <= old_speed, f"{boundary_kind} boundary falls outside {hand} swing")
    remaining_fraction = 1.0 - elapsed / old_speed
    proportional_deadline = boundary_time + remaining_fraction * new_speed
    unchanged_deadline = previous_time + old_speed
    return {
        "boundary": boundary_kind,
        "opposite_hand": hand,
        "previous_anchor": dict(previous_anchor),
        "next_anchor": dict(next_anchor),
        "observed_cross_boundary_interval_seconds": observed_time - previous_time,
        "old_speed_seconds": old_speed,
        "new_speed_seconds": new_speed,
        "proportional_prediction": {
            "deadline": proportional_deadline,
            "error_seconds_observed_minus_predicted": observed_time - proportional_deadline,
        },
        "unchanged_deadline_prediction": {
            "deadline": unchanged_deadline,
            "error_seconds_observed_minus_predicted": observed_time - unchanged_deadline,
        },
        "derivation": "trace boundary and hand anchors; locked speed rescaled by Flurry 1.30 multiplier",
    }


def _stage_c(
    rows: Sequence[Mapping[str, Any]], loadout: Mapping[str, Any]
) -> dict[str, Any]:
    started = _one_marker(rows, "CALIBRATION_HASTE_STAGE_STARTED")
    completed = _one_marker(rows, "CALIBRATION_HASTE_STAGE_COMPLETED")
    boundary_markers = _marker_rows(rows, "CALIBRATION_HASTE_AURA_BOUNDARY")
    _require(len(boundary_markers) == 2, "stage C requires exactly two Flurry aura boundaries")
    anchors = [
        _anchor_event(marker, rows)
        for marker in _marker_rows(rows, "CALIBRATION_HASTE_SWING_ANCHOR")
        if _debug_sequence(started) < _debug_sequence(marker) < _debug_sequence(completed)
    ]
    _require(bool(anchors), "stage C has no hand-resolved haste swing anchors")
    _require({item["hand"] for item in anchors} == {"main_hand", "off_hand"}, "stage C does not resolve both hands")
    boundaries: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    expected = (("BUFF_ADDED_SELF", "added"), ("BUFF_REMOVED_SELF", "removed"))
    for marker, (event_name, boundary_kind) in zip(boundary_markers, expected):
        event = _paired_event(marker, rows, event_name)
        source = _source(event)
        _require(source.get("spellID") == FLURRY_SPELL_ID, f"{boundary_kind} boundary is not Flurry 12970")
        boundary_time = _event_time(event)
        prior = [item for item in anchors if item["event_time"] < boundary_time]
        later = [item for item in anchors if item["event_time"] > boundary_time]
        _require(bool(prior) and bool(later), f"{boundary_kind} boundary lacks surrounding anchors")
        triggering_hand = max(prior, key=lambda item: item["event_time"])["hand"]
        opposite_hand = "off_hand" if triggering_hand == "main_hand" else "main_hand"
        opposite_prior = [item for item in prior if item["hand"] == opposite_hand]
        opposite_later = [item for item in later if item["hand"] == opposite_hand]
        _require(bool(opposite_prior) and bool(opposite_later), f"{boundary_kind} lacks opposite-hand anchors")
        previous_anchor = max(opposite_prior, key=lambda item: item["event_time"])
        next_anchor = min(opposite_later, key=lambda item: item["event_time"])
        locked_speed = _number(
            loadout[f"{opposite_hand}_speed_seconds"], f"locked {opposite_hand} speed"
        )
        boundaries.append(
            {
                "boundary": boundary_kind,
                "source_event": event_name,
                "spell_id": FLURRY_SPELL_ID,
                "event_time": boundary_time,
                "event_calibration_sequence": _calibration_sequence(event),
                "marker_debug_sequence": _debug_sequence(marker),
                "triggering_hand_from_nearest_prior_anchor": triggering_hand,
            }
        )
        comparisons.append(
            _boundary_prediction(
                boundary_kind=boundary_kind,
                boundary_time=boundary_time,
                hand=opposite_hand,
                previous_anchor=previous_anchor,
                next_anchor=next_anchor,
                locked_speed=locked_speed,
            )
        )
    _require(boundaries[0]["event_time"] < boundaries[1]["event_time"], "Flurry boundary order is invalid")
    return {
        "complete": True,
        "flurry_spell_id": FLURRY_SPELL_ID,
        "flurry_speed_multiplier": FLURRY_SPEED_MULTIPLIER,
        "boundaries": boundaries,
        "hand_boundary_anchor_observations": {
            hand: [item for item in anchors if item["hand"] == hand]
            for hand in ("main_hand", "off_hand")
        },
        "opposite_hand_boundary_comparisons": comparisons,
        "debug_projection_has_original_prediction_numeric_values": False,
        "projection_limit": (
            "the debug projection retains aura boundaries and hand-resolved anchors "
            "but not the addon's original predicted rescale numeric fields; the two "
            "comparisons above are derived from trace times and locked speeds"
        ),
        "completed_debug_sequence": _debug_sequence(completed),
    }


def _event_by_calibration_sequence(
    rows: Sequence[Mapping[str, Any]], sequence: int
) -> Mapping[str, Any]:
    matches = [
        row
        for row in rows
        if row.get("kind") == "EVENT"
        and _source(row).get("calibrationSequence") == sequence
    ]
    _require(len(matches) == 1, f"recovery calibration sequence {sequence} must identify one event")
    return matches[0]


def _stage_d(
    rows: Sequence[Mapping[str, Any]],
    composite: Mapping[str, Any],
    source_run: str,
) -> dict[str, Any]:
    names = (
        "CALIBRATION_STAGE_D_RECOVERY_STARTED",
        "CALIBRATION_QUEUE_STAGE_STARTED",
        "CALIBRATION_HS_QUEUE_REQUESTED",
        "CALIBRATION_HS_QUEUE_ACCEPTED",
        "CALIBRATION_HS_CANCEL_REQUESTED",
        "CALIBRATION_HS_CANCEL_RETARGET_APPLIED",
        "CALIBRATION_HS_CANCEL_WINDOW_CLOSED",
        "CALIBRATION_HS_CANCEL_COMPLETED",
        "CALIBRATION_EXTERNAL_HOLD",
        "CALIBRATION_LOADOUT_RESTORE_STARTED",
        "CALIBRATION_LOADOUT_RESTORED",
        "CALIBRATION_STAGE_D_RECOVERY_COMPLETED",
    )
    markers = {name: _one_marker(rows, name) for name in names}
    ordered = [_debug_sequence(markers[name]) for name in names]
    _require(ordered == sorted(ordered), "stage D recovery marker order is invalid")
    start = _details(markers["CALIBRATION_STAGE_D_RECOVERY_STARTED"])
    terminal = _details(markers["CALIBRATION_STAGE_D_RECOVERY_COMPLETED"])
    _require(start.get("campaignMode") == "stage_d_recovery", "recovery start mode mismatch")
    _require(start.get("sourceCampaignRunId") == source_run, "recovery start source link mismatch")
    _require(terminal.get("sourceCampaignRunId") == source_run, "recovery terminal source link mismatch")
    _require(terminal.get("campaignMode") == "stage_d_recovery", "recovery terminal mode mismatch")
    _require(terminal.get("loadoutRestored") is True, "recovery terminal does not restore loadout")
    _require(
        terminal.get("recoveryLoadoutEvidence") == "operator_supplied_explicit_item_ids",
        "recovery terminal loadout provenance mismatch",
    )
    composite_heroic = dict(_mapping(composite.get("heroic_strike"), "composite.heroic_strike"))
    terminal_heroic = dict(_mapping(terminal.get("heroicStrike"), "recovery terminal heroicStrike"))
    _require(terminal_heroic == composite_heroic, "terminal Heroic Strike evidence differs from composite")
    queue = _details(markers["CALIBRATION_HS_QUEUE_REQUESTED"])
    requested = _details(markers["CALIBRATION_HS_CANCEL_REQUESTED"])
    retarget = _details(markers["CALIBRATION_HS_CANCEL_RETARGET_APPLIED"])
    closed = _details(markers["CALIBRATION_HS_CANCEL_WINDOW_CLOSED"])
    cancel = _details(markers["CALIBRATION_HS_CANCEL_COMPLETED"])
    queue_remaining = _number(queue.get("mainHandRemaining"), "HS queue main-hand remaining")
    queue_minimum = _number(queue.get("earlyQueueMinimum"), "HS early queue minimum")
    _require(queue_remaining >= queue_minimum, "HS queue was not early")
    for details, label in ((requested, "cancel request"), (retarget, "retarget"), (cancel, "cancel completion")):
        _require(details.get("actionSlotWasCurrent") is True, f"{label} action slot was not current")
        _require(details.get("cancelRequestPath") == "ClearTarget_TargetUnit_same_guid", f"{label} path mismatch")
    for flag in ("clearTargetIssued", "targetCleared", "targetUnitIssued", "targetRestored"):
        _require(retarget.get(flag) is True, f"retarget lacks {flag}")
    _require(closed.get("boundedNoGoResult") is True, "cancel window was not bounded no-GO/no-result")
    _require(
        _source(markers["CALIBRATION_HS_CANCEL_WINDOW_CLOSED"]).get("sourceSequence")
        == 40,
        "cancel window source sequence mismatch",
    )
    for flag in ("boundedNoGoResult", "nextMainHandWasWhite", "offHandContinued", "serverFailureSupport"):
        _require(cancel.get(flag) is True, f"cancel completion lacks {flag}")
    event34 = _event_by_calibration_sequence(rows, 34)
    event40 = _event_by_calibration_sequence(rows, 40)
    event46 = _event_by_calibration_sequence(rows, 46)
    _require(event34.get("name") == "SPELL_FAILED_SELF", "sequence 34 is not SPELL_FAILED_SELF")
    _require(_source(event34).get("spellID") == HEROIC_STRIKE_SPELL_ID, "sequence 34 is not Heroic Strike")
    _require(event40.get("name") == "UNIT_CASTEVENT", "sequence 40 is not UNIT_CASTEVENT")
    _require(_source(event40).get("spellID") == AUTO_ATTACK_SPELL_ID, "sequence 40 is not white swing 6603")
    _require(_source(event40).get("castKind") == "MAINHAND", "sequence 40 is not main hand")
    _require(event46.get("name") == "AUTO_ATTACK_SELF", "sequence 46 is not AUTO_ATTACK_SELF")
    _require(cancel.get("nextMainHandSequence") == 40, "cancel marker does not link main-hand sequence 40")
    _require(cancel.get("offHandSequence") == 46, "cancel marker does not link off-hand sequence 46")
    hold = markers["CALIBRATION_EXTERNAL_HOLD"]
    _require(hold.get("reason") == "requires_exactly_two_adjacent_attackable_targets", "target-switch hold reason mismatch")
    restore_started = _details(markers["CALIBRATION_LOADOUT_RESTORE_STARTED"])
    restored = _details(markers["CALIBRATION_LOADOUT_RESTORED"])
    original_main = _integer(terminal.get("originalMainHandItemID"), "original main-hand item")
    _require(start.get("originalMainHandItemID") == original_main, "start original main item mismatch")
    _require(restore_started.get("originalMainHandItemID") == original_main, "restore start item mismatch")
    _require(restored.get("originalMainHandItemID") == original_main, "restored item mismatch")

    def compact_event(row: Mapping[str, Any]) -> dict[str, Any]:
        source = _source(row)
        return {
            "calibration_sequence": _calibration_sequence(row),
            "debug_sequence": _debug_sequence(row),
            "event": row.get("name"),
            "event_time": _event_time(row),
            **_selected(source, ("spellID", "castKind", "hitInfo", "amount")),
        }

    return {
        "complete": True,
        "queue": _selected(queue, ("mainHandRemaining", "earlyQueueMinimum", "rage", "rageCost", "transaction")),
        "cancel": {
            "request": _selected(requested, ("actionSlot", "actionSlotWasCurrent", "actionSpellID", "cancelRequestPath", "mainHandRemaining")),
            "retarget": _selected(retarget, ("clearTargetIssued", "targetCleared", "targetUnitIssued", "targetRestored", "restartAttackRequested")),
            "window_close": _selected(closed, ("boundedNoGoResult", "closeEvidence", "sourceSequence")),
            "completion": _selected(cancel, ("boundedNoGoResult", "serverFailureSupport", "nextMainHandSequence", "nextMainHandWasWhite", "offHandSequence", "offHandContinued")),
            "correlated_events": {
                "34": compact_event(event34),
                "40": compact_event(event40),
                "46": compact_event(event46),
            },
        },
        "target_switch": {
            "status": terminal_heroic.get("targetSwitchStatus"),
            "hold_reason": terminal_heroic.get("targetSwitchHoldReason"),
            "mechanic_observed": False,
            "disposition_resolved": True,
        },
        "loadout_restore": {
            "complete": True,
            "provenance": terminal.get("recoveryLoadoutEvidence"),
            "original_main_hand_item_id": original_main,
            "restore_started_debug_sequence": _debug_sequence(markers["CALIBRATION_LOADOUT_RESTORE_STARTED"]),
            "restored_debug_sequence": _debug_sequence(markers["CALIBRATION_LOADOUT_RESTORED"]),
            "terminal_debug_sequence": _debug_sequence(markers["CALIBRATION_STAGE_D_RECOVERY_COMPLETED"]),
        },
    }


_RUN_RE = re.compile(r"\|run=([^|]+)\|")
_SEQ_RE = re.compile(r"\|seq=(\d+)\|")


def _combat_token(run_id: str) -> str:
    return "".join(chr(byte) if 32 <= byte < 127 else "_" for byte in run_id.encode("utf-8"))


def _combat_corroboration(
    path_value: str | Path | None, recovery_run: str
) -> dict[str, Any]:
    if path_value is None:
        return {
            "status": "not_requested",
            "source": None,
            "run_token": _combat_token(recovery_run),
            "target_sequences": list(COMBAT_NEIGHBOR_SEQUENCES),
            "neighbor_radius": COMBAT_NEIGHBOR_RADIUS,
            "lines": [],
            "lines_read": 0,
            "lines_retained": 0,
        }
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        return {
            "status": "not_available",
            "source": str(path),
            "run_token": _combat_token(recovery_run),
            "target_sequences": list(COMBAT_NEIGHBOR_SEQUENCES),
            "neighbor_radius": COMBAT_NEIGHBOR_RADIUS,
            "lines": [],
            "lines_read": 0,
            "lines_retained": 0,
        }
    token = _combat_token(recovery_run)
    retained: list[dict[str, Any]] = []
    found_targets: set[int] = set()
    lines_read = 0
    allowed = {
        sequence
        for target in COMBAT_NEIGHBOR_SEQUENCES
        for sequence in range(target - COMBAT_NEIGHBOR_RADIUS, target + COMBAT_NEIGHBOR_RADIUS + 1)
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                lines_read = line_number
                if "BOC_TIMER" not in line or f"|run={token}|" not in line:
                    continue
                run_match = _RUN_RE.search(line)
                sequence_match = _SEQ_RE.search(line)
                if run_match is None or run_match.group(1) != token or sequence_match is None:
                    continue
                sequence = int(sequence_match.group(1))
                if sequence in COMBAT_NEIGHBOR_SEQUENCES:
                    found_targets.add(sequence)
                if sequence in allowed:
                    retained.append(
                        {
                            "line_number": line_number,
                            "calibration_sequence": sequence,
                            "text": line.rstrip("\r\n"),
                        }
                    )
    except OSError as error:
        raise FuryTimerSourceRecoverySummaryError(
            f"cannot read optional WoW combat log {path}: {error}"
        ) from error
    missing = sorted(set(COMBAT_NEIGHBOR_SEQUENCES) - found_targets)
    return {
        "status": "complete" if not missing else "incomplete_optional_corroboration",
        "source": str(path),
        "run_token": token,
        "target_sequences": list(COMBAT_NEIGHBOR_SEQUENCES),
        "neighbor_radius": COMBAT_NEIGHBOR_RADIUS,
        "missing_target_sequences": missing,
        "lines": retained,
        "lines_read": lines_read,
        "lines_retained": len(retained),
    }


def build_fury_timer_source_recovery_summary(
    composite_path: str | Path = DEFAULT_COMPOSITE,
    *,
    combat_log_path: str | Path | None = DEFAULT_COMBAT_LOG,
) -> dict[str, Any]:
    composite_source = Path(composite_path).expanduser().resolve()
    composite, source_trace_path, recovery_trace_path, source_run, recovery_run = (
        _validate_composite(composite_source)
    )
    source_rows, source_lines = _load_trace(source_trace_path, "source trace")
    recovery_rows, recovery_lines = _load_trace(recovery_trace_path, "recovery trace")
    stage_a = _stage_a(source_rows)
    loadout = _locked_loadout(source_rows)
    stage_b = _stage_b(source_rows, loadout)
    stage_c = _stage_c(source_rows, loadout)
    stage_d = _stage_d(recovery_rows, composite, source_run)
    combat = _combat_corroboration(combat_log_path, recovery_run)
    stage_checks = {
        "stage_A_timer_chains": stage_a["complete"] is True,
        "stage_B_dual_wield_intervals": stage_b["complete"] is True,
        "stage_C_flurry_boundaries": stage_c["complete"] is True,
        "stage_D_cancel_and_restore": stage_d["complete"] is True,
    }
    _require(all(stage_checks.values()), "one or more source/recovery stage gates failed")
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": _utc_now(),
        "status": "complete",
        "inputs": {
            "composite": str(composite_source),
            "source_trace": str(source_trace_path),
            "recovery_trace": str(recovery_trace_path),
            "wow_combat_log": str(Path(combat_log_path).expanduser().resolve()) if combat_log_path is not None else None,
            "source_campaign_run_id": source_run,
            "recovery_campaign_run_id": recovery_run,
        },
        "evidence_boundary": dict(_mapping(composite.get("evidence_boundary"), "evidence boundary")),
        "composite_validation": {
            "required_check_count": len(REQUIRED_COMPOSITE_CHECKS),
            "all_14_checks_true": True,
            "trace_links_exact": True,
            "checks": dict(_mapping(composite.get("checks"), "composite checks")),
        },
        "stages": {
            "A_timer_chains": stage_a,
            "B_dual_wield_intervals": stage_b,
            "C_flurry_haste_boundaries": stage_c,
            "D_heroic_strike_cancel_and_loadout": stage_d,
        },
        "combat_log_corroboration": combat,
        "evidence_gate": {
            "checks": stage_checks,
            "live_contract_complete": True,
            "simulator_patch_allowed": False,
            "historical_reconstruction_allowed": False,
            "restrictions": [
                "a separate simulator comparison is required before changing wowsims-turtle",
                "the live source/recovery contract must not be generalized into historical reconstruction",
            ],
        },
        "scope": {
            "source_trace_rows_read": len(source_rows),
            "source_trace_physical_lines": source_lines,
            "recovery_trace_rows_read": len(recovery_rows),
            "recovery_trace_physical_lines": recovery_lines,
            "combat_log_lines_read": combat["lines_read"],
            "combat_log_lines_retained": combat["lines_retained"],
            "chronicle_rows_read": 0,
        },
    }


def write_fury_timer_source_recovery_summary(
    report: Mapping[str, Any], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise FuryTimerSourceRecoverySummaryError(
            f"cannot write source/recovery timer summary {destination}: {error}"
        ) from error
    return destination


def summarize_fury_timer_source_recovery(
    composite_path: str | Path = DEFAULT_COMPOSITE,
    *,
    combat_log_path: str | Path | None = DEFAULT_COMBAT_LOG,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> FuryTimerSourceRecoverySummaryResult:
    report = build_fury_timer_source_recovery_summary(
        composite_path, combat_log_path=combat_log_path
    )
    output = write_fury_timer_source_recovery_summary(report, output_path)
    inputs = _mapping(report.get("inputs"), "summary inputs")
    gate = _mapping(report.get("evidence_gate"), "summary evidence gate")
    return FuryTimerSourceRecoverySummaryResult(
        status=str(report["status"]),
        output=output,
        source_campaign_run_id=str(inputs["source_campaign_run_id"]),
        recovery_campaign_run_id=str(inputs["recovery_campaign_run_id"]),
        live_contract_complete=gate.get("live_contract_complete") is True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("composite", nargs="?", type=Path, default=DEFAULT_COMPOSITE)
    parser.add_argument("--combat-log", type=Path, default=DEFAULT_COMBAT_LOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = summarize_fury_timer_source_recovery(
            args.composite,
            combat_log_path=args.combat_log,
            output_path=args.output,
        )
    except FuryTimerSourceRecoverySummaryError as error:
        print(f"Fury timer source/recovery summary failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "DEFAULT_COMBAT_LOG",
    "DEFAULT_COMPOSITE",
    "DEFAULT_OUTPUT",
    "FuryTimerSourceRecoverySummaryError",
    "FuryTimerSourceRecoverySummaryResult",
    "KIND",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_fury_timer_source_recovery_summary",
    "main",
    "summarize_fury_timer_source_recovery",
    "write_fury_timer_source_recovery_summary",
]


if __name__ == "__main__":
    raise SystemExit(main())
