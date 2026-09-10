"""Decode one completed Fury timer campaign from imported calibration JSONL.

The decoder is intentionally independent from the generic calibration summary.
It binds one run only through the frozen timer campaign identity and reports the
observed timer, dual-wield, Flurry, Heroic Strike cancel, and loadout-restore
evidence without modifying the mechanics registry or simulator.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import sys
from typing import Any


CAMPAIGN_ID = "warrior_fury_timer_campaign_v1"
COMPLETION_EVENT = "CALIBRATION_CAMPAIGN_COMPLETED"
START_EVENT = "CALIBRATION_TIMER_CAMPAIGN_STARTED"
SCHEMA = "fury_timer_calibration_summary/v1"
SCHEMA_VERSION = 1
KIND = "fury_timer_calibration_summary"
REQUIRED_CHAINS_PER_SPELL = 3
REQUIRED_INTERVALS_PER_HAND = 3


class FuryTimerCalibrationSummaryError(ValueError):
    """The selected JSONL does not contain one structurally valid timer run."""


@dataclass(frozen=True)
class FuryTimerCalibrationSummaryResult:
    status: str
    output: Path
    campaign_run_id: str
    evidence_complete: bool
    blocker_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output": str(self.output),
            "kind": KIND,
            "campaign_id": CAMPAIGN_ID,
            "campaign_run_id": self.campaign_run_id,
            "evidence_complete": self.evidence_complete,
            "blocker_count": self.blocker_count,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    line_count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_count, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FuryTimerCalibrationSummaryError(
                        f"invalid calibration JSONL at {path}:{line_count}: {error.msg}"
                    ) from error
                if not isinstance(row, dict):
                    raise FuryTimerCalibrationSummaryError(
                        f"calibration JSONL row {line_count} is not an object"
                    )
                rows.append(row)
    except (OSError, UnicodeError) as error:
        raise FuryTimerCalibrationSummaryError(
            f"cannot read calibration JSONL {path}: {error}"
        ) from error
    return rows, line_count


def _identity(container: Any) -> tuple[str | None, str | None]:
    if not isinstance(container, Mapping):
        return None, None
    run_id = container.get("campaignRunId")
    campaign_id = container.get("campaignId")
    return (
        run_id.strip() if isinstance(run_id, str) and run_id.strip() else None,
        campaign_id.strip()
        if isinstance(campaign_id, str) and campaign_id.strip()
        else None,
    )


def _belongs_to_run(row: Mapping[str, Any], campaign_run_id: str) -> bool:
    identities = (_identity(row.get("marker")), _identity(row.get("task")))
    for run_id, campaign_id in identities:
        if run_id == campaign_run_id and campaign_id == CAMPAIGN_ID:
            return True
        if run_id == campaign_run_id and campaign_id not in {None, CAMPAIGN_ID}:
            raise FuryTimerCalibrationSummaryError(
                f"campaign run {campaign_run_id!r} has conflicting campaignId "
                f"{campaign_id!r}"
            )
    return False


def _sequence(row: Mapping[str, Any]) -> int:
    value = row.get("sequence")
    if not isinstance(value, int) or isinstance(value, bool):
        raise FuryTimerCalibrationSummaryError("timer campaign row lacks integer sequence")
    return value


def _marker(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("marker")
    return dict(value) if isinstance(value, Mapping) else {}


def _event_rows(
    rows: Sequence[Mapping[str, Any]], event_name: str
) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("event") == event_name]


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _small_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "minimum": None, "median": None, "maximum": None}
    return {
        "count": len(values),
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _selected(mapping: Any, fields: Sequence[str]) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        return {}
    return {field: mapping.get(field) for field in fields if field in mapping}


def _marker_matches(
    row: Mapping[str, Any], *, spell_key: str | None = None, attempt: Any = None
) -> bool:
    details = _marker(row)
    if spell_key is not None and details.get("spellKey") != spell_key:
        return False
    if attempt is not None and details.get("attempt") != attempt:
        return False
    return True


def _summarize_timer_spell(
    rows: Sequence[Mapping[str, Any]], spell_key: str
) -> dict[str, Any]:
    retry_required = spell_key == "bloodthirst"
    completions = [
        row
        for row in _event_rows(rows, "CALIBRATION_TIMER_CHAIN_COMPLETED")
        if _marker_matches(row, spell_key=spell_key)
    ]
    chains: list[dict[str, Any]] = []
    required_flags = (
        "clientCastSeen",
        "startSeen",
        "goSeen",
        "cooldownEventSeen",
        "monotonicToZero",
    )
    retry_flags = ("retryRequested", "retryFailureSeen", "retryNoExtension")
    durations: list[float] = []
    first_remaining: list[float] = []
    for row in completions:
        details = _marker(row)
        attempt = details.get("attempt")
        first_rows = [
            candidate
            for candidate in _event_rows(rows, "CALIBRATION_TIMER_FIRST_NONZERO")
            if _marker_matches(candidate, spell_key=spell_key, attempt=attempt)
            and _sequence(candidate) < _sequence(row)
        ]
        retry_rows = [
            candidate
            for candidate in _event_rows(
                rows, "CALIBRATION_TIMER_ACTIVE_RETRY_REQUESTED"
            )
            if _marker_matches(candidate, spell_key=spell_key, attempt=attempt)
            and _sequence(candidate) < _sequence(row)
        ]
        confirmed_rows = [
            candidate
            for candidate in _event_rows(
                rows, "CALIBRATION_TIMER_ACTIVE_RETRY_CONFIRMED"
            )
            if _marker_matches(candidate, spell_key=spell_key, attempt=attempt)
            and _sequence(candidate) < _sequence(row)
        ]
        zero_rows = [
            candidate
            for candidate in _event_rows(rows, "CALIBRATION_TIMER_REACHED_ZERO")
            if _marker_matches(candidate, spell_key=spell_key, attempt=attempt)
            and _sequence(candidate) < _sequence(row)
        ]
        first = _marker(first_rows[-1]) if first_rows else {}
        snapshot = first.get("timerSnapshot")
        snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
        spellbook = snapshot.get("spellbook")
        spellbook = dict(spellbook) if isinstance(spellbook, Mapping) else {}
        duration = _numeric(spellbook.get("duration"))
        remaining = _numeric(first.get("timerRemaining"))
        if duration is not None:
            durations.append(duration)
        if remaining is not None:
            first_remaining.append(remaining)
        flags = {field: details.get(field) is True for field in required_flags}
        flags.update(
            {
                field: details.get(field) is True
                for field in retry_flags
            }
        )
        structural = {
            "first_nonzero_marker": bool(first_rows),
            "active_retry_request_marker": bool(retry_rows),
            "active_retry_confirmed_marker": bool(confirmed_rows),
            "reached_zero_marker": bool(zero_rows),
        }
        required_event_flags = all(flags[field] for field in required_flags)
        required_retry_flags = (
            all(flags[field] for field in retry_flags) if retry_required else True
        )
        required_structural_markers = (
            structural["first_nonzero_marker"]
            and structural["reached_zero_marker"]
            and (
                not retry_required
                or (
                    structural["active_retry_request_marker"]
                    and structural["active_retry_confirmed_marker"]
                )
            )
        )
        chains.append(
            {
                "completion_sequence": _sequence(row),
                "attempt": attempt,
                "completed_chains": details.get("completedChains"),
                "required_chains": details.get("requiredChains"),
                "event_chain": flags,
                "structural_markers": structural,
                "first_nonzero_sequence": (
                    _sequence(first_rows[-1]) if first_rows else None
                ),
                "first_nonzero_source_event": first.get("sourceEvent"),
                "first_nonzero_remaining_seconds": remaining,
                "first_nonzero_timer_snapshot": snapshot,
                "retry_required": retry_required,
                "complete": (
                    required_event_flags
                    and required_retry_flags
                    and required_structural_markers
                ),
            }
        )
    complete = (
        len(chains) == REQUIRED_CHAINS_PER_SPELL
        and all(chain["complete"] for chain in chains)
        and [chain["completed_chains"] for chain in chains]
        == list(range(1, REQUIRED_CHAINS_PER_SPELL + 1))
    )
    return {
        "spell_key": spell_key,
        "retry_required": retry_required,
        "required_chain_count": REQUIRED_CHAINS_PER_SPELL,
        "completed_chain_count": len(chains),
        "observed_spellbook_duration_seconds": _small_stats(durations),
        "first_nonzero_remaining_seconds": _small_stats(first_remaining),
        "chains": chains,
        "complete": complete,
    }


def _summarize_swing_stage(
    rows: Sequence[Mapping[str, Any]], terminal: Mapping[str, Any]
) -> dict[str, Any]:
    anchors = _event_rows(rows, "CALIBRATION_SWING_ANCHOR")
    stop_rows = _event_rows(rows, "CALIBRATION_AUTO_ATTACK_STOP_REQUESTED")
    restart_rows = _event_rows(rows, "CALIBRATION_AUTO_ATTACK_RESTART_REQUESTED")
    speed_rows = _event_rows(rows, "CALIBRATION_SWING_SPEED_LOCKED")
    stop_sequence = _sequence(stop_rows[-1]) if stop_rows else sys.maxsize
    restart_sequence = _sequence(restart_rows[-1]) if restart_rows else sys.maxsize
    by_hand: dict[str, list[float]] = {"main_hand": [], "off_hand": []}
    post_restart = {"main_hand": 0, "off_hand": 0}
    for row in anchors:
        details = _marker(row)
        hand = details.get("hand")
        interval = _numeric(details.get("interval"))
        sequence = _sequence(row)
        if hand in by_hand and interval is not None and interval > 0 and sequence < stop_sequence:
            by_hand[hand].append(interval)
        if hand in post_restart and sequence > restart_sequence:
            post_restart[hand] += 1
    terminal_counts = terminal.get("swingIntervals")
    terminal_counts = (
        dict(terminal_counts) if isinstance(terminal_counts, Mapping) else {}
    )
    checks = {
        "speed_locked": bool(speed_rows),
        "main_hand_three_intervals": (
            len(by_hand["main_hand"]) >= REQUIRED_INTERVALS_PER_HAND
            and terminal_counts.get("mainHand") == REQUIRED_INTERVALS_PER_HAND
        ),
        "off_hand_three_intervals": (
            len(by_hand["off_hand"]) >= REQUIRED_INTERVALS_PER_HAND
            and terminal_counts.get("offHand") == REQUIRED_INTERVALS_PER_HAND
        ),
        "stop_requested": bool(stop_rows),
        "restart_requested": bool(restart_rows),
        "post_restart_main_anchor": post_restart["main_hand"] > 0,
        "post_restart_off_anchor": post_restart["off_hand"] > 0,
        "terminal_stop_start_complete": terminal.get("stopStartCompleted") is True,
    }
    return {
        "required_intervals_per_hand": REQUIRED_INTERVALS_PER_HAND,
        "locked_speed": _marker(speed_rows[-1]) if speed_rows else None,
        "baseline_intervals_seconds": {
            "main_hand": by_hand["main_hand"],
            "off_hand": by_hand["off_hand"],
        },
        "interval_statistics_seconds": {
            "main_hand": _small_stats(by_hand["main_hand"]),
            "off_hand": _small_stats(by_hand["off_hand"]),
        },
        "post_restart_anchor_count": post_restart,
        "checks": checks,
        "complete": all(checks.values()),
    }


def _summarize_haste_stage(
    rows: Sequence[Mapping[str, Any]], terminal: Mapping[str, Any]
) -> dict[str, Any]:
    boundary_rows = _event_rows(rows, "CALIBRATION_HASTE_AURA_BOUNDARY")
    anchor_rows = _event_rows(rows, "CALIBRATION_HASTE_SWING_ANCHOR")
    completed_rows = _event_rows(rows, "CALIBRATION_HASTE_STAGE_COMPLETED")
    boundaries: list[dict[str, Any]] = []
    for row in boundary_rows:
        details = _marker(row)
        boundaries.append(
            {
                "sequence": _sequence(row),
                **_selected(
                    details,
                    (
                        "boundary",
                        "sourceEvent",
                        "sourceSequence",
                        "spellID",
                        "stackCount",
                        "auraLuaSlot",
                        "auraSlot",
                        "liveSpellID",
                        "liveStacks",
                        "beforeAttackSnapshot",
                        "afterAttackSnapshot",
                        "proportionalRescaleCandidates",
                        "cancellationForbidden",
                    ),
                ),
            }
        )
    observed_anchors: list[dict[str, Any]] = []
    errors_by_hand: dict[str, list[float]] = {"main_hand": [], "off_hand": []}
    for row in anchor_rows:
        details = _marker(row)
        crossed = details.get("crossedAuraBoundary")
        if crossed is None:
            continue
        hand = details.get("hand")
        error = _numeric(details.get("proportionalDeadlineError"))
        if error is None:
            predicted = _numeric(details.get("predictedRescaledDeadline"))
            observed = _numeric(details.get("observedNextHandAnchorTime"))
            if predicted is not None and observed is not None:
                error = observed - predicted
        if hand in errors_by_hand and error is not None:
            errors_by_hand[hand].append(error)
        observed_anchors.append(
            {
                "sequence": _sequence(row),
                **_selected(
                    details,
                    (
                        "hand",
                        "interval",
                        "crossedAuraBoundary",
                        "attackSnapshot",
                        "boundaryPrediction",
                        "predictedRescaledRemaining",
                        "predictedRescaledDeadline",
                        "observedNextHandAnchorTime",
                        "observedNextHandAnchorProvenance",
                        "proportionalDeadlineError",
                    ),
                ),
            }
        )
    final = terminal.get("hasteRescale")
    final = dict(final) if isinstance(final, Mapping) else {}
    bool_fields = (
        "criticalWhiteSeen",
        "flurryAddedSeen",
        "flurryRemovedSeen",
        "mainHandJoinedFlurry",
        "mainHandLeftFlurry",
        "offHandJoinedFlurry",
        "offHandLeftFlurry",
        "postRemovalMain",
        "postRemovalOff",
    )
    checks = {field: final.get(field) is True for field in bool_fields}
    checks.update(
        {
            "main_hand_two_boundaries": (
                (_numeric(final.get("mainHandBoundaryCount")) or 0) >= 2
            ),
            "off_hand_two_boundaries": (
                (_numeric(final.get("offHandBoundaryCount")) or 0) >= 2
            ),
            "added_and_removed_markers": (
                {item.get("boundary") for item in boundaries} >= {"added", "removed"}
            ),
            "stage_completed_marker": bool(completed_rows),
        }
    )
    return {
        "terminal": final,
        "aura_boundaries": boundaries,
        "boundary_crossing_anchors": observed_anchors,
        "proportional_deadline_error_seconds": {
            hand: _small_stats(values) for hand, values in errors_by_hand.items()
        },
        "reload_continuity_reset_count": len(
            _event_rows(rows, "CALIBRATION_HASTE_CONTINUITY_RESET")
        ),
        "reload_aura_resync_count": len(
            _event_rows(rows, "CALIBRATION_HASTE_RELOAD_AURA_RESYNC")
        ),
        "checks": checks,
        "complete": all(checks.values()),
    }


def _summarize_queue_stage(
    rows: Sequence[Mapping[str, Any]], terminal: Mapping[str, Any]
) -> dict[str, Any]:
    queue_rows = _event_rows(rows, "CALIBRATION_HS_QUEUE_REQUESTED")
    cancel_request_rows = _event_rows(rows, "CALIBRATION_HS_CANCEL_REQUESTED")
    retarget_rows = _event_rows(rows, "CALIBRATION_HS_CANCEL_RETARGET_APPLIED")
    window_rows = _event_rows(rows, "CALIBRATION_HS_CANCEL_WINDOW_CLOSED")
    completed_rows = _event_rows(rows, "CALIBRATION_HS_CANCEL_COMPLETED")
    hold_rows = _event_rows(rows, "CALIBRATION_EXTERNAL_HOLD")
    rule_rows = _event_rows(rows, "CALIBRATION_HS_TARGET_SWITCH_RULE")
    final = terminal.get("heroicStrike")
    final = dict(final) if isinstance(final, Mapping) else {}
    minimum = _numeric(final.get("earlyQueueMinimum"))
    remaining = _numeric(final.get("primaryMainHandRemaining"))
    target_status = final.get("targetSwitchStatus")
    target_ok = (
        target_status == "EXTERNAL_HOLD"
        and isinstance(final.get("targetSwitchHoldReason"), str)
        and bool(final.get("targetSwitchHoldReason"))
        and any(
            _marker(row).get("holdScope") == "optional_target_switch"
            and _marker(row).get("campaignContinues") is True
            for row in hold_rows
        )
    ) or (
        target_status == "COMPLETED"
        and isinstance(final.get("targetSwitchRule"), str)
        and bool(final.get("targetSwitchRule"))
        and bool(rule_rows)
    )
    checks = {
        "queue_request_marker": bool(queue_rows),
        "cancel_request_marker": bool(cancel_request_rows),
        "cancel_window_closed_marker": bool(window_rows),
        "cancel_completed_marker": bool(completed_rows),
        "cancel_completed": final.get("cancelCompleted") is True,
        "early_queue_threshold_met": (
            minimum is not None and remaining is not None and remaining >= minimum
        ),
        "cancel_action_slot_observed": isinstance(final.get("cancelActionSlot"), int),
        "cancel_action_slot_was_current": final.get("cancelActionSlotWasCurrent") is True,
        "cancel_retarget_marker": bool(retarget_rows),
        "cancel_request_path": (
            final.get("cancelRequestPath") == "ClearTarget_TargetUnit_same_guid"
        ),
        "cancel_clear_target_issued": final.get("cancelClearTargetIssued") is True,
        "cancel_target_cleared": final.get("cancelTargetCleared") is True,
        "cancel_target_unit_issued": final.get("cancelTargetUnitIssued") is True,
        "cancel_same_target_restored": final.get("cancelTargetRestored") is True,
        "strong_cancel_support": final.get("strongCancelSupport") is True,
        "bounded_no_go_result": final.get("boundedNoGoResult") is True,
        "no_unexpected_server_go": final.get("unexpectedServerGo") is False,
        "no_unexpected_result": final.get("unexpectedResult") is False,
        "cast_by_name_cancel_forbidden": (
            final.get("castSpellByNameCancelForbidden") is True
        ),
        "same_spell_use_action_cancel_forbidden": (
            final.get("sameSpellUseActionCancelForbidden") is True
        ),
        "queue_code_one_auxiliary_only": final.get("queueCodeOneIsAuxiliaryOnly") is True,
        "next_main_hand_was_white": final.get("nextMainHandWasWhite") is True,
        "off_hand_continued": final.get("offHandContinued") is True,
        "optional_target_switch_resolved": target_ok,
    }
    return {
        "terminal": final,
        "queue_request": _marker(queue_rows[-1]) if queue_rows else None,
        "cancel_request": (
            _marker(cancel_request_rows[-1]) if cancel_request_rows else None
        ),
        "cancel_retarget": _marker(retarget_rows[-1]) if retarget_rows else None,
        "cancel_window_closed": _marker(window_rows[-1]) if window_rows else None,
        "cancel_completed": _marker(completed_rows[-1]) if completed_rows else None,
        "target_switch": {
            "status": target_status,
            "rule": final.get("targetSwitchRule"),
            "hold_reason": final.get("targetSwitchHoldReason"),
            "final_action_target_guid": final.get("finalActionTargetGUID"),
        },
        "checks": checks,
        "complete": all(checks.values()),
    }


def _summarize_restore(
    rows: Sequence[Mapping[str, Any]], terminal: Mapping[str, Any]
) -> dict[str, Any]:
    started_rows = _event_rows(rows, "CALIBRATION_LOADOUT_RESTORE_STARTED")
    restored_rows = _event_rows(rows, "CALIBRATION_LOADOUT_RESTORED")
    restored = _marker(restored_rows[-1]) if restored_rows else {}
    original_main = terminal.get("originalMainHandItemID")
    original_off = terminal.get("originalOffHandItemID")
    checks = {
        "restore_started_marker": bool(started_rows),
        "restore_completed_marker": bool(restored_rows),
        "terminal_loadout_restored": terminal.get("loadoutRestored") is True,
        "main_hand_matches_original": (
            restored.get("originalMainHandItemID") == original_main
            and restored.get("restoredMainHandItemID") == original_main
        ),
        "off_hand_matches_original": (
            restored.get("originalOffHandItemID") == original_off
            and restored.get("restoredOffHandItemID") == original_off
        ),
    }
    return {
        "started": _marker(started_rows[-1]) if started_rows else None,
        "restored": restored or None,
        "checks": checks,
        "complete": all(checks.values()),
    }


def build_fury_timer_calibration_summary(
    calibration_jsonl: str | Path, *, campaign_run_id: str
) -> dict[str, Any]:
    source = Path(calibration_jsonl).expanduser().resolve()
    run_id = campaign_run_id.strip() if isinstance(campaign_run_id, str) else ""
    if not run_id:
        raise FuryTimerCalibrationSummaryError("campaign_run_id must be non-empty")
    if not run_id.startswith("timer-campaign-"):
        raise FuryTimerCalibrationSummaryError(
            "timer campaign_run_id must start with 'timer-campaign-'"
        )
    all_rows, line_count = _load_rows(source)
    selected = [row for row in all_rows if _belongs_to_run(row, run_id)]
    starts = [row for row in selected if row.get("event") == START_EVENT]
    completions = [
        row for row in selected if row.get("event") == COMPLETION_EVENT
    ]
    if len(starts) != 1:
        raise FuryTimerCalibrationSummaryError(
            f"timer campaign {run_id!r} requires exactly one {START_EVENT}; found {len(starts)}"
        )
    if len(completions) != 1:
        raise FuryTimerCalibrationSummaryError(
            f"timer campaign {run_id!r} requires exactly one {COMPLETION_EVENT}; "
            f"found {len(completions)}"
        )
    start_sequence = _sequence(starts[0])
    terminal_sequence = _sequence(completions[0])
    if terminal_sequence <= start_sequence:
        raise FuryTimerCalibrationSummaryError(
            "timer completion sequence must follow its start sequence"
        )
    rows = sorted(
        (
            row
            for row in selected
            if start_sequence <= _sequence(row) <= terminal_sequence
        ),
        key=_sequence,
    )
    if len({_sequence(row) for row in rows}) != len(rows):
        raise FuryTimerCalibrationSummaryError(
            f"timer campaign {run_id!r} contains duplicate sequence values"
        )
    start = _marker(starts[0])
    terminal = _marker(completions[0])
    terminal_identity_checks = {
        "schema_version_one": terminal.get("schemaVersion") == 1,
        "phase_campaign_completed": terminal.get("phase") == "campaign_completed",
        "campaign_id_exact": terminal.get("campaignId") == CAMPAIGN_ID,
        "campaign_run_id_exact": terminal.get("campaignRunId") == run_id,
        "campaign_run_id_prefix": run_id.startswith("timer-campaign-"),
        "status_awaiting_export_reload": (
            terminal.get("status") == "awaiting_export_reload"
        ),
        "stage_restore_original_loadout": (
            terminal.get("stage") == "restore_original_loadout"
        ),
        "next_instruction_exact": (
            terminal.get("nextInstruction")
            == "reload_once_for_automatic_import"
        ),
    }
    start_checks = {
        "schema_version_one": start.get("schemaVersion") == 1,
        "campaign_id_exact": start.get("campaignId") == CAMPAIGN_ID,
        "campaign_run_id_exact": start.get("campaignRunId") == run_id,
        "stage_count_four": start.get("stageCount") == 4,
        "three_chains_per_spell": (
            start.get("requiredChainsPerSpell") == REQUIRED_CHAINS_PER_SPELL
        ),
        "three_intervals_per_hand": (
            start.get("requiredIntervalsPerHand") == REQUIRED_INTERVALS_PER_HAND
        ),
    }
    timer_spells = {
        key: _summarize_timer_spell(rows, key)
        for key in ("sunder", "bloodthirst")
    }
    terminal_chains = terminal.get("timerChains")
    terminal_chains = (
        dict(terminal_chains) if isinstance(terminal_chains, Mapping) else {}
    )
    timer_terminal_checks = {
        key: terminal_chains.get(key) == REQUIRED_CHAINS_PER_SPELL
        for key in timer_spells
    }
    timer_complete = all(timer_terminal_checks.values()) and all(
        value["complete"] for value in timer_spells.values()
    )
    swing = _summarize_swing_stage(rows, terminal)
    haste = _summarize_haste_stage(rows, terminal)
    queue = _summarize_queue_stage(rows, terminal)
    restore = _summarize_restore(rows, terminal)
    gates = {
        "start_contract": all(start_checks.values()),
        "terminal_identity": all(terminal_identity_checks.values()),
        "timer_transitions": timer_complete,
        "dual_wield_swing": swing["complete"],
        "flurry_haste_rescale": haste["complete"],
        "heroic_strike_cancel": queue["complete"],
        "loadout_restore": restore["complete"],
    }
    blockers = [name for name, passed in gates.items() if not passed]
    evidence_complete = not blockers
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": _utc_now(),
        "status": "complete" if evidence_complete else "incomplete_evidence",
        "campaign_id": CAMPAIGN_ID,
        "campaign_run_id": run_id,
        "source": str(source),
        "bounds": {
            "start_sequence": start_sequence,
            "terminal_sequence": terminal_sequence,
            "campaign_record_count": len(rows),
            "jsonl_line_count": line_count,
        },
        "start_contract": {"marker": start, "checks": start_checks},
        "terminal_contract": {
            "event": COMPLETION_EVENT,
            "marker": terminal,
            "checks": terminal_identity_checks,
        },
        "timer_transitions": {
            "spells": timer_spells,
            "terminal_checks": timer_terminal_checks,
            "complete": timer_complete,
        },
        "dual_wield_swing": swing,
        "flurry_haste_rescale": haste,
        "heroic_strike": queue,
        "loadout_restore": restore,
        "evidence_gate": {
            "complete": evidence_complete,
            "checks": gates,
            "blockers": blockers,
        },
        "conclusion": {
            "timer_transition_evidence_complete": evidence_complete,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
            "reason": (
                "completed live evidence is ready for a separate simulator comparison"
                if evidence_complete
                else "one or more frozen live-evidence gates are incomplete"
            ),
        },
    }


def _safe_run_fragment(campaign_run_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", campaign_run_id).strip("_.-")
    return value or "timer_campaign"


def write_fury_timer_calibration_summary(
    report: Mapping[str, Any], output_path: str | Path
) -> Path:
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise FuryTimerCalibrationSummaryError(
            f"cannot write timer calibration summary {destination}: {error}"
        ) from error
    return destination


def summarize_fury_timer_calibration(
    calibration_jsonl: str | Path,
    *,
    campaign_run_id: str,
    output_path: str | Path | None = None,
) -> FuryTimerCalibrationSummaryResult:
    report = build_fury_timer_calibration_summary(
        calibration_jsonl, campaign_run_id=campaign_run_id
    )
    source = Path(calibration_jsonl).expanduser().resolve()
    if output_path is None:
        output_path = source.parent.parent / "timer_calibration_summaries" / (
            f"{source.stem}__{_safe_run_fragment(campaign_run_id)}.json"
        )
    output = write_fury_timer_calibration_summary(report, output_path)
    return FuryTimerCalibrationSummaryResult(
        status=str(report["status"]),
        output=output,
        campaign_run_id=campaign_run_id,
        evidence_complete=report["evidence_gate"]["complete"] is True,
        blocker_count=len(report["evidence_gate"]["blockers"]),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calibration", type=Path)
    parser.add_argument("--campaign-run-id", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = summarize_fury_timer_calibration(
            args.calibration,
            campaign_run_id=args.campaign_run_id,
            output_path=args.output,
        )
    except FuryTimerCalibrationSummaryError as error:
        print(f"Fury timer calibration summary failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "CAMPAIGN_ID",
    "COMPLETION_EVENT",
    "FuryTimerCalibrationSummaryError",
    "FuryTimerCalibrationSummaryResult",
    "KIND",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_fury_timer_calibration_summary",
    "main",
    "summarize_fury_timer_calibration",
    "write_fury_timer_calibration_summary",
]


if __name__ == "__main__":
    raise SystemExit(main())
