"""Inventory Fury timer-transition readiness without reconstructing decision rows.

This audit deliberately separates a known timer duration from a complete
transition contract.  A remaining-time field is reconstructable only when its
duration, causal start anchor, reset/cancel rules, build applicability, and
claim provenance are all complete.  The current audit reads only small
registries/reports, the strict live source/recovery summary, and selected
simulator source files; it never opens the compact Chronicle partitions.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from .chronicle_fury_decision_dataset import _registry_actions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_REGISTRY = (
    PROJECT_ROOT / "mechanics" / "registry" / "turtle_1_18_1" / "warrior_fury.json"
)
DEFAULT_REVIEW = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_review.json"
)
DEFAULT_COMPARISON = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_current_build_phase12_comparison.json"
)
DEFAULT_LIVE_SUMMARY = (
    PROJECT_ROOT
    / "offline_data"
    / "timer_calibration_summaries"
    / "fury_timer_source_recovery_summary_v1.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_timer_transition_contract_v1.json"
)

SCHEMA = "fury_timer_transition_contract/v1"
SCHEMA_VERSION = 1
LIVE_SUMMARY_SCHEMA = "fury_timer_source_recovery_summary/v1"
LIVE_SUMMARY_KIND = "fury_timer_source_recovery_summary"
LIVE_STAGE_KEYS = (
    "A_timer_chains",
    "B_dual_wield_intervals",
    "C_flurry_haste_boundaries",
    "D_heroic_strike_cancel_and_loadout",
)
LIVE_STAGE_GATE_KEYS = (
    "stage_A_timer_chains",
    "stage_B_dual_wield_intervals",
    "stage_C_flurry_boundaries",
    "stage_D_cancel_and_restore",
)
TIMER_FIELDS = (
    "gcd_remaining_ms",
    "cooldown_remaining_ms",
    "mainhand_swing_remaining_ms",
    "offhand_swing_remaining_ms",
)
REQUIRED_COMPONENTS = (
    "duration",
    "timer_start",
    "reset_cancel",
    "rank_talent_build_applicability",
    "provenance",
)

_VERIFIED_PARAMETER_STATUSES = frozenset({"verified_deterministic"})


class FuryTimerTransitionContractError(RuntimeError):
    """A required readiness input is absent or does not match its contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: str | Path, label: str) -> tuple[Path, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryTimerTransitionContractError(
            f"cannot read {label} {resolved}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryTimerTransitionContractError(f"{label} must be a JSON object")
    return resolved, value


def _live_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryTimerTransitionContractError(
            f"live timer summary {label} must be an object"
        )
    return value


def _validate_live_stage_evidence(live_summary: Mapping[str, Any]) -> None:
    stages = _live_mapping(live_summary.get("stages"), "stages")
    stage_a = _live_mapping(stages.get("A_timer_chains"), "stage A")
    spells = _live_mapping(stage_a.get("spells"), "stage A spells")
    sunder = _live_mapping(spells.get("sunder"), "stage A Sunder")
    bloodthirst = _live_mapping(
        spells.get("bloodthirst"), "stage A Bloodthirst"
    )
    if (
        sunder.get("completed_chain_count") != 3
        or bloodthirst.get("completed_chain_count") != 3
    ):
        raise FuryTimerTransitionContractError(
            "live timer summary must contain three Sunder and Bloodthirst chains"
        )
    retries = bloodthirst.get("active_retry_confirmations")
    if not isinstance(retries, list) or len(retries) != 3:
        raise FuryTimerTransitionContractError(
            "live timer summary must contain three Bloodthirst retry confirmations"
        )
    for retry in retries:
        retry = _live_mapping(retry, "Bloodthirst retry")
        before = retry.get("remaining_before_seconds")
        after = retry.get("remaining_after_seconds")
        delta = retry.get("timer_end_delta_seconds")
        if (
            retry.get("evidence_kind") != "explicit_failure_event"
            or not isinstance(before, (int, float))
            or isinstance(before, bool)
            or not isinstance(after, (int, float))
            or isinstance(after, bool)
            or not isinstance(delta, (int, float))
            or isinstance(delta, bool)
            or not after < before
            or abs(float(delta)) > 0.01
        ):
            raise FuryTimerTransitionContractError(
                "live Bloodthirst retry evidence does not prove no timer extension"
            )

    stage_b = _live_mapping(stages.get("B_dual_wield_intervals"), "stage B")
    loadout = _live_mapping(stage_b.get("locked_loadout"), "stage B loadout")
    baseline = _live_mapping(stage_b.get("baseline"), "stage B baseline")
    stop_restart = _live_mapping(
        stage_b.get("stop_restart"), "stage B stop/restart"
    )
    post_restart = _live_mapping(
        stop_restart.get("post_restart_first_anchor"),
        "stage B post-restart anchors",
    )
    for hand in ("main_hand", "off_hand"):
        speed = loadout.get(f"{hand}_speed_seconds")
        hand_baseline = _live_mapping(baseline.get(hand), f"stage B {hand}")
        intervals = hand_baseline.get("intervals_seconds")
        if (
            not isinstance(speed, (int, float))
            or isinstance(speed, bool)
            or speed <= 0
            or not isinstance(intervals, list)
            or len(intervals) < 3
            or not isinstance(post_restart.get(hand), Mapping)
        ):
            raise FuryTimerTransitionContractError(
                f"live stage B does not close the {hand} interval contract"
            )
    if loadout.get("source_kind") != "TIMEOUT_snapshot":
        raise FuryTimerTransitionContractError(
            "live stage B locked loadout provenance is not the timeout snapshot"
        )

    stage_c = _live_mapping(
        stages.get("C_flurry_haste_boundaries"), "stage C"
    )
    comparisons = stage_c.get("opposite_hand_boundary_comparisons")
    if (
        stage_c.get("flurry_spell_id") != 12970
        or stage_c.get("flurry_speed_multiplier") != 1.3
        or stage_c.get("debug_projection_has_original_prediction_numeric_values")
        is not False
        or not isinstance(comparisons, list)
        or len(comparisons) != 2
        or {item.get("boundary") for item in comparisons if isinstance(item, Mapping)}
        != {"added", "removed"}
    ):
        raise FuryTimerTransitionContractError(
            "live stage C does not match the fixed Flurry boundary contract"
        )
    for comparison in comparisons:
        comparison = _live_mapping(comparison, "stage C boundary comparison")
        proportional = _live_mapping(
            comparison.get("proportional_prediction"),
            "stage C proportional prediction",
        )
        unchanged = _live_mapping(
            comparison.get("unchanged_deadline_prediction"),
            "stage C unchanged prediction",
        )
        proportional_error = proportional.get(
            "error_seconds_observed_minus_predicted"
        )
        unchanged_error = unchanged.get("error_seconds_observed_minus_predicted")
        if (
            not isinstance(proportional_error, (int, float))
            or isinstance(proportional_error, bool)
            or not isinstance(unchanged_error, (int, float))
            or isinstance(unchanged_error, bool)
            or abs(float(proportional_error)) <= abs(float(unchanged_error))
        ):
            raise FuryTimerTransitionContractError(
                "live stage C does not contradict opposite-hand proportional rescaling"
            )

    stage_d = _live_mapping(
        stages.get("D_heroic_strike_cancel_and_loadout"), "stage D"
    )
    cancel = _live_mapping(stage_d.get("cancel"), "stage D cancel")
    request = _live_mapping(cancel.get("request"), "stage D cancel request")
    retarget = _live_mapping(cancel.get("retarget"), "stage D retarget")
    completion = _live_mapping(
        cancel.get("completion"), "stage D cancel completion"
    )
    window = _live_mapping(cancel.get("window_close"), "stage D cancel window")
    if request.get("cancelRequestPath") != "ClearTarget_TargetUnit_same_guid":
        raise FuryTimerTransitionContractError(
            "live stage D cancel path is not same-GUID retarget"
        )
    for flag in (
        "clearTargetIssued",
        "targetCleared",
        "targetUnitIssued",
        "targetRestored",
    ):
        if retarget.get(flag) is not True:
            raise FuryTimerTransitionContractError(
                f"live stage D same-GUID retarget lacks {flag}"
            )
    for flag in (
        "boundedNoGoResult",
        "serverFailureSupport",
        "nextMainHandWasWhite",
        "offHandContinued",
    ):
        if completion.get(flag) is not True:
            raise FuryTimerTransitionContractError(
                f"live stage D cancel completion lacks {flag}"
            )
    if window.get("boundedNoGoResult") is not True:
        raise FuryTimerTransitionContractError(
            "live stage D cancellation window is not bounded"
        )
    target_switch = _live_mapping(
        stage_d.get("target_switch"), "stage D target switch"
    )
    if (
        target_switch.get("status") != "EXTERNAL_HOLD"
        or target_switch.get("hold_reason")
        != "requires_exactly_two_adjacent_attackable_targets"
        or target_switch.get("mechanic_observed") is not False
        or target_switch.get("disposition_resolved") is not True
    ):
        raise FuryTimerTransitionContractError(
            "live stage D two-target disposition is not the exact EXTERNAL_HOLD"
        )
    restore = _live_mapping(
        stage_d.get("loadout_restore"), "stage D loadout restore"
    )
    if (
        restore.get("complete") is not True
        or restore.get("provenance")
        != "operator_supplied_explicit_item_ids"
    ):
        raise FuryTimerTransitionContractError(
            "live stage D loadout restoration provenance is incomplete"
        )


def _validate_inputs(
    registry: Mapping[str, Any],
    review: Mapping[str, Any],
    comparison: Mapping[str, Any],
    live_summary: Mapping[str, Any],
) -> None:
    if registry.get("schema_version") != 2 or not isinstance(
        registry.get("mechanics"), list
    ):
        raise FuryTimerTransitionContractError(
            "Warrior mechanics registry must use schema_version 2"
        )
    if review.get("kind") != "fury_current_build_phase12_evidence_review":
        raise FuryTimerTransitionContractError("unsupported Phase12 review kind")
    if comparison.get("kind") != "fury_current_build_phase12_simulator_comparison":
        raise FuryTimerTransitionContractError("unsupported Phase12 comparison kind")
    comparisons = comparison.get("comparisons")
    if not isinstance(comparisons, Mapping):
        raise FuryTimerTransitionContractError("Phase12 comparison has no comparisons")
    if live_summary.get("schema") != LIVE_SUMMARY_SCHEMA:
        raise FuryTimerTransitionContractError(
            "live timer summary must use " + LIVE_SUMMARY_SCHEMA
        )
    if live_summary.get("schema_version") != 1:
        raise FuryTimerTransitionContractError(
            "live timer summary schema_version must be 1"
        )
    if live_summary.get("kind") != LIVE_SUMMARY_KIND:
        raise FuryTimerTransitionContractError(
            "unsupported live timer source/recovery summary kind"
        )
    if live_summary.get("status") != "complete":
        raise FuryTimerTransitionContractError(
            "live timer source/recovery summary status must be complete"
        )
    live_inputs = live_summary.get("inputs")
    if not isinstance(live_inputs, Mapping):
        raise FuryTimerTransitionContractError(
            "live timer source/recovery summary has no inputs"
        )
    source_run = live_inputs.get("source_campaign_run_id")
    recovery_run = live_inputs.get("recovery_campaign_run_id")
    if not isinstance(source_run, str) or not source_run.startswith(
        "timer-campaign-"
    ):
        raise FuryTimerTransitionContractError(
            "live timer summary source campaign link is invalid"
        )
    if not isinstance(recovery_run, str) or not recovery_run.startswith(
        "timer-recovery-"
    ):
        raise FuryTimerTransitionContractError(
            "live timer summary recovery campaign link is invalid"
        )
    for path_field in ("composite", "source_trace", "recovery_trace"):
        value = live_inputs.get(path_field)
        if not isinstance(value, str) or not value:
            raise FuryTimerTransitionContractError(
                f"live timer summary input {path_field} is missing"
            )
    validation = live_summary.get("composite_validation")
    if not isinstance(validation, Mapping):
        raise FuryTimerTransitionContractError(
            "live timer summary has no composite validation"
        )
    checks = validation.get("checks")
    if (
        validation.get("required_check_count") != 14
        or validation.get("all_14_checks_true") is not True
        or validation.get("trace_links_exact") is not True
        or not isinstance(checks, Mapping)
        or len(checks) != 14
        or any(value is not True for value in checks.values())
    ):
        raise FuryTimerTransitionContractError(
            "live timer summary source/recovery composite gate is incomplete"
        )
    stages = live_summary.get("stages")
    if not isinstance(stages, Mapping) or set(stages) != set(LIVE_STAGE_KEYS):
        raise FuryTimerTransitionContractError(
            "live timer summary stages do not match the A-D contract"
        )
    for stage_key in LIVE_STAGE_KEYS:
        stage = stages.get(stage_key)
        if not isinstance(stage, Mapping) or stage.get("complete") is not True:
            raise FuryTimerTransitionContractError(
                f"live timer summary stage {stage_key} is incomplete"
            )
    evidence_gate = live_summary.get("evidence_gate")
    if not isinstance(evidence_gate, Mapping):
        raise FuryTimerTransitionContractError(
            "live timer summary has no evidence gate"
        )
    stage_checks = evidence_gate.get("checks")
    if (
        not isinstance(stage_checks, Mapping)
        or set(stage_checks) != set(LIVE_STAGE_GATE_KEYS)
        or any(stage_checks.get(key) is not True for key in LIVE_STAGE_GATE_KEYS)
    ):
        raise FuryTimerTransitionContractError(
            "live timer summary A-D evidence gate is incomplete"
        )
    if evidence_gate.get("live_contract_complete") is not True:
        raise FuryTimerTransitionContractError(
            "live timer source/recovery summary gate is not complete"
        )
    if evidence_gate.get("simulator_patch_allowed") is not False:
        raise FuryTimerTransitionContractError(
            "live timer summary simulator-patch boundary is invalid"
        )
    if evidence_gate.get("historical_reconstruction_allowed") is not False:
        raise FuryTimerTransitionContractError(
            "live timer summary must not authorize historical reconstruction"
        )
    _validate_live_stage_evidence(live_summary)


def _spell_ids(implementation: Mapping[str, Any]) -> list[int]:
    values: set[int] = set()
    for field in ("spell_id", "wrapper_spell_id"):
        value = implementation.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            values.add(value)
    repeated = implementation.get("spell_ids")
    if isinstance(repeated, list):
        values.update(
            value
            for value in repeated
            if isinstance(value, int) and not isinstance(value, bool)
        )
    return sorted(values)


def _mechanic_index(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(registry["mechanics"]):
        if not isinstance(value, dict):
            raise FuryTimerTransitionContractError(
                f"registry mechanics[{index}] is not an object"
            )
        key = value.get("key")
        if not isinstance(key, str) or not key:
            raise FuryTimerTransitionContractError(
                f"registry mechanics[{index}] has no key"
            )
        if key in result:
            raise FuryTimerTransitionContractError(f"duplicate mechanic key {key}")
        result[key] = value
    return result


def _action_catalog(
    registry_path: Path,
    mechanics: Mapping[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, str]]]:
    action_map, _metadata = _registry_actions(registry_path)
    by_key: dict[str, dict[str, Any]] = {}
    for spell_id, action in action_map.items():
        key = action.get("policy_action_key")
        if not isinstance(key, str) or key not in mechanics:
            raise FuryTimerTransitionContractError(
                f"action registry references unknown mechanic {key!r}"
            )
        entry = by_key.setdefault(
            key,
            {
                "action_key": key,
                "spell_ids": [],
                "lane": action.get("lane"),
                "mechanic_status": mechanics[key].get("status"),
            },
        )
        if entry["lane"] != action.get("lane"):
            raise FuryTimerTransitionContractError(
                f"action registry assigns multiple lanes to {key}"
            )
        entry["spell_ids"].append(spell_id)
    for entry in by_key.values():
        entry["spell_ids"].sort()
    return sorted(by_key.values(), key=lambda item: item["action_key"]), action_map


def _calibration_parameter(
    mechanic: Mapping[str, Any], field: str
) -> Mapping[str, Any] | None:
    calibration = mechanic.get("calibration")
    if not isinstance(calibration, Mapping):
        return None
    for collection_name in ("parameters", "effects"):
        collection = calibration.get(collection_name)
        if isinstance(collection, Mapping):
            value = collection.get(field)
            if isinstance(value, Mapping):
                return value
    return None


def _parameter_scope(field: str, value: Any, mechanic: Mapping[str, Any]) -> str:
    if field.startswith("current_character_"):
        return "current_character_only"
    if mechanic.get("status") == "simulator_only":
        return "simulator_source_only"
    if isinstance(value, str) and "rank" in value.lower():
        return "rank_dependent_formula"
    return "registry_declared"


def _duration_parameter(
    mechanic: Mapping[str, Any], field: str
) -> dict[str, Any]:
    implementation = mechanic.get("implementation")
    implementation = implementation if isinstance(implementation, Mapping) else {}
    value = implementation.get(field)
    calibration = _calibration_parameter(mechanic, field)
    calibration_status = calibration.get("status") if calibration else None
    scope = _parameter_scope(field, value, mechanic)
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    verified = calibration_status in _VERIFIED_PARAMETER_STATUSES
    transferable = verified and numeric and scope == "registry_declared"
    provenance: dict[str, Any] = {
        "registry_mechanic": mechanic.get("key"),
        "registry_status": mechanic.get("status"),
        "evidence": list(mechanic.get("evidence") or []),
    }
    if calibration:
        provenance["calibration_status"] = calibration_status
        provenance["source"] = calibration.get("source")
        provenance["acceptance"] = calibration.get("acceptance")
        provenance["decision_id"] = calibration.get("decision_id")
    return {
        "field": field,
        "value": value,
        "unit": "seconds",
        "scope": scope,
        "numeric": numeric,
        "verified_deterministic": verified,
        "transferable_to_historical_builds": transferable,
        "provenance": provenance,
    }


def _duration_action_inventory(
    actions: Sequence[Mapping[str, Any]],
    mechanics: Mapping[str, dict[str, Any]],
    *,
    timer: str,
) -> list[dict[str, Any]]:
    if timer == "gcd_remaining_ms":
        fields = ("gcd_seconds", "base_gcd_seconds")
        selected = [action for action in actions if action.get("lane") == "gcd"]
    elif timer == "cooldown_remaining_ms":
        fields = (
            "current_character_cooldown_seconds",
            "cooldown_seconds",
            "base_cooldown_seconds",
        )
        selected = []
        for action in actions:
            mechanic = mechanics[str(action["action_key"])]
            implementation = mechanic.get("implementation")
            implementation = (
                implementation if isinstance(implementation, Mapping) else {}
            )
            if any(field in implementation for field in fields):
                selected.append(action)
    else:
        return []

    result: list[dict[str, Any]] = []
    for action in selected:
        mechanic = mechanics[str(action["action_key"])]
        implementation = mechanic.get("implementation")
        implementation = implementation if isinstance(implementation, Mapping) else {}
        parameters = [
            _duration_parameter(mechanic, field)
            for field in fields
            if field in implementation
        ]
        transferable = any(
            parameter["transferable_to_historical_builds"] for parameter in parameters
        )
        result.append(
            {
                **dict(action),
                "duration_parameters": parameters,
                "historical_duration_ready": transferable,
                "duration_blockers": (
                    []
                    if transferable
                    else [
                        "no numeric verified deterministic duration transferable to all historical builds"
                    ]
                ),
            }
        )
    return result


def _source_checks(source_root: Path) -> dict[str, dict[str, Any]]:
    specifications = {
        "simulator_gcd_start": {
            "relative_path": "wowsims-turtle/sim/core/cast.go",
            "needle": "spell.Unit.SetGCDTimer(sim, sim.CurrentTime+effectiveTime)",
            "timers": ["gcd_remaining_ms"],
            "meaning": "simulator starts its GCD timer from cast acceptance/effective time",
        },
        "simulator_cooldown_start": {
            "relative_path": "wowsims-turtle/sim/core/cast.go",
            "needle": "spell.CD.Set(sim.CurrentTime + spell.CurCast.CastTime + spell.CD.Duration)",
            "timers": ["cooldown_remaining_ms"],
            "meaning": "simulator schedules spell cooldown readiness from cast time plus duration",
        },
        "gear_dependent_cooldown_reset": {
            "relative_path": "wowsims-turtle/sim/warrior/item_sets_pve.go",
            "needle": "spell.CD.Reset()",
            "timers": ["cooldown_remaining_ms"],
            "meaning": "checks for an active Warrior item-set cooldown reset; block-commented SoD source is excluded",
        },
        "normal_swing_reschedule": {
            "relative_path": "wowsims-turtle/sim/core/attack.go",
            "needle": "wa.swingAt = sim.CurrentTime + wa.curSwingDuration",
            "timers": [
                "mainhand_swing_remaining_ms",
                "offhand_swing_remaining_ms",
            ],
            "meaning": "simulator schedules a subsequent hand swing after a successful swing",
        },
        "haste_rescales_remaining_swing": {
            "relative_path": "wowsims-turtle/sim/core/attack.go",
            "needle": "aa.mh.swingAt = sim.CurrentTime + time.Duration(float64(remainingSwingTime)*f)",
            "timers": [
                "mainhand_swing_remaining_ms",
                "offhand_swing_remaining_ms",
            ],
            "meaning": "ordinary and non-white-boundary melee speed changes proportionally rescale in-progress swings",
        },
        "white_swing_haste_preserves_opposite_deadline": {
            "relative_path": "wowsims-turtle/sim/core/unit.go",
            "needle": "oppositeHand.swingAt = oppositeDeadline",
            "timers": [
                "mainhand_swing_remaining_ms",
                "offhand_swing_remaining_ms",
            ],
            "meaning": "a white-swing speed boundary rescales the triggering hand while preserving the already-scheduled opposite-hand deadline",
        },
        "warrior_flurry_uses_white_swing_boundary": {
            "relative_path": "wowsims-turtle/sim/warrior/talents.go",
            "needle": "warrior.MultiplyMeleeSpeedAtWhiteSwingBoundary(",
            "timers": [
                "mainhand_swing_remaining_ms",
                "offhand_swing_remaining_ms",
            ],
            "meaning": "Warrior Flurry routes white-swing add/remove transitions through the hand-aware boundary rule",
        },
        "warrior_flurry_proc_aura_ids": {
            "relative_path": "wowsims-turtle/sim/warrior/talents.go",
            "needle": "spellID := []int32{12966, 12967, 12968, 12969, 12970}[points-1]",
            "timers": [
                "mainhand_swing_remaining_ms",
                "offhand_swing_remaining_ms",
            ],
            "meaning": "Warrior Flurry ranks use the live proc aura IDs, including rank five 12970",
        },
        "queued_swing_can_expire": {
            "relative_path": "wowsims-turtle/sim/warrior/heroic_strike_cleave.go",
            "needle": "warrior.curQueueAura.Deactivate(sim)",
            "timers": ["mainhand_swing_remaining_ms"],
            "meaning": "queued Heroic Strike/Cleave source has replacement and expiration paths",
        },
        "warrior_explicit_queue_cancel": {
            "relative_path": "wowsims-turtle/sim/warrior/heroic_strike_cleave.go",
            "needle": "func (warrior *Warrior) CancelQueuedHSOrCleave(sim *core.Simulation) bool",
            "timers": ["mainhand_swing_remaining_ms"],
            "meaning": "Warrior exposes an explicit accepted HS/Cleave queue cancellation transition",
        },
        "o2o_explicit_queue_cancel": {
            "relative_path": "wowsims-turtle/sim/o2o/environment.go",
            "needle": "func (env *Environment) CancelQueuedMelee() (CancelQueuedMeleeResult, error)",
            "timers": ["mainhand_swing_remaining_ms"],
            "meaning": "the interactive environment exposes queue cancellation as a non-spell control operation",
        },
        "bridge_explicit_queue_cancel": {
            "relative_path": "wowsims-turtle/cmd/o2obridge/main.go",
            "needle": "case \"cancel_queue\":",
            "timers": ["mainhand_swing_remaining_ms"],
            "meaning": "the Windows policy bridge exposes the explicit cancel_queue command",
        },
        "stance_shared_cooldown": {
            "relative_path": "wowsims-turtle/sim/warrior/stances.go",
            "needle": "Duration: time.Second,",
            "timers": ["cooldown_remaining_ms"],
            "meaning": "simulator stances share a one-second timer",
        },
    }
    result: dict[str, dict[str, Any]] = {}
    for key, spec in specifications.items():
        path = (source_root / spec["relative_path"]).resolve()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        needle = str(spec["needle"])
        active_lines: list[str] = []
        inside_block_comment = False
        for line in lines:
            cursor = 0
            active_fragments: list[str] = []
            while cursor < len(line):
                if inside_block_comment:
                    comment_end = line.find("*/", cursor)
                    if comment_end < 0:
                        cursor = len(line)
                    else:
                        inside_block_comment = False
                        cursor = comment_end + 2
                else:
                    comment_start = line.find("/*", cursor)
                    if comment_start < 0:
                        active_fragments.append(line[cursor:])
                        cursor = len(line)
                    else:
                        active_fragments.append(line[cursor:comment_start])
                        inside_block_comment = True
                        cursor = comment_start + 2
            active_lines.append("".join(active_fragments))

        active_match_lines = [
            index
            for index, line in enumerate(active_lines, start=1)
            if needle in line
        ]
        all_match_lines = [
            index for index, line in enumerate(lines, start=1) if needle in line
        ]
        commented_out_match_lines = [
            index for index in all_match_lines if index not in active_match_lines
        ]
        line_number = active_match_lines[0] if active_match_lines else None
        if line_number is not None:
            evidence_role = "simulator_source_model_only"
        elif commented_out_match_lines:
            evidence_role = "excluded_block_comment_only"
        else:
            evidence_role = "source_absent"
        result[key] = {
            "path": str(path),
            "line": line_number,
            "present": line_number is not None,
            "active_match_lines": active_match_lines,
            "commented_out_match_lines": commented_out_match_lines,
            "evidence_role": evidence_role,
            "meaning": spec["meaning"],
            "timers": list(spec["timers"]),
        }
    return result


def _comparison_fact(
    comparison: Mapping[str, Any], key: str
) -> dict[str, Any] | None:
    comparisons = comparison.get("comparisons")
    if not isinstance(comparisons, Mapping):
        return None
    value = comparisons.get(key)
    if not isinstance(value, Mapping):
        return None
    return {
        "key": key,
        "comparison_result": value.get("comparison_result"),
        "gate_status": value.get("gate_status"),
        "scope_limit": value.get("scope_limit"),
        "unresolved": list(value.get("unresolved") or []),
        "incomplete_or_missing_stage_ids": list(
            value.get("incomplete_or_missing_stage_ids") or []
        ),
        "replacement_formula_identified": value.get(
            "replacement_formula_identified"
        ),
    }


def _component(
    *, status: str, complete: bool, evidence: Any, blockers: Sequence[str]
) -> dict[str, Any]:
    return {
        "status": status,
        "complete": bool(complete),
        "evidence": evidence,
        "blockers": list(blockers),
    }


def _duration_component(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ready = sum(bool(entry.get("historical_duration_ready")) for entry in entries)
    complete = bool(entries) and ready == len(entries)
    return _component(
        status="COMPLETE" if complete else ("PARTIAL" if entries else "MISSING"),
        complete=complete,
        evidence={
            "action_count": len(entries),
            "historical_duration_ready_action_count": ready,
            "actions": list(entries),
        },
        blockers=(
            []
            if complete
            else [
                "one or more registered actions lack a numeric, verified, historically transferable duration"
            ]
        ),
    )


def _timer_contracts(
    actions: Sequence[Mapping[str, Any]],
    mechanics: Mapping[str, dict[str, Any]],
    comparison: Mapping[str, Any],
    source_checks: Mapping[str, Mapping[str, Any]],
    live_summary: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    gcd_durations = _duration_action_inventory(
        actions, mechanics, timer="gcd_remaining_ms"
    )
    cooldown_durations = _duration_action_inventory(
        actions, mechanics, timer="cooldown_remaining_ms"
    )

    slam = mechanics.get("warrior.slam", {})
    slam_deadline = _calibration_parameter(slam, "main_hand_swing_deadline")
    queue_fact = _comparison_fact(comparison, "queue_cross_stance")
    dual_fact = _comparison_fact(comparison, "dual_wield_qualitative")
    duration_fact = _comparison_fact(
        comparison, "death_wish_recklessness_duration"
    )
    movement_slam_fact = _comparison_fact(comparison, "movement_slam")
    live_stages = _live_mapping(live_summary.get("stages"), "stages")
    live_stage_a = dict(
        _live_mapping(live_stages.get("A_timer_chains"), "stage A")
    )
    live_stage_b = dict(
        _live_mapping(live_stages.get("B_dual_wield_intervals"), "stage B")
    )
    live_stage_c = dict(
        _live_mapping(
            live_stages.get("C_flurry_haste_boundaries"), "stage C"
        )
    )
    live_stage_d = dict(
        _live_mapping(
            live_stages.get("D_heroic_strike_cancel_and_loadout"),
            "stage D",
        )
    )
    live_bloodthirst = dict(
        _live_mapping(
            _live_mapping(live_stage_a.get("spells"), "stage A spells").get(
                "bloodthirst"
            ),
            "stage A Bloodthirst",
        )
    )
    live_target_switch = dict(
        _live_mapping(live_stage_d.get("target_switch"), "stage D target switch")
    )
    gear_reset_check = source_checks.get("gear_dependent_cooldown_reset")
    active_gear_reset = bool(
        isinstance(gear_reset_check, Mapping)
        and gear_reset_check.get("present")
    )
    cooldown_reset_evidence: dict[str, Any] = {
        "stance_shared_timer": source_checks.get("stance_shared_cooldown"),
    }
    cooldown_reset_blockers = [
        "shared cooldown and reset/cancel behavior is not live-calibrated for all actions"
    ]
    cooldown_applicability_blockers = [
        "Whirlwind 8.5 seconds is verified only for the captured Ravager-rank-2 build",
        "Bloodrage, Death Wish, and stance cooldowns remain simulator-only in the registry",
    ]
    if active_gear_reset:
        cooldown_reset_evidence["gear_reset"] = gear_reset_check
        cooldown_reset_blockers.insert(
            0,
            "active simulator source contains a gear-dependent cooldown reset whose Turtle/build applicability is unestablished",
        )
        cooldown_applicability_blockers.append(
            "gear-dependent cooldown-reset applicability requires exact set context"
        )
    elif isinstance(gear_reset_check, Mapping) and gear_reset_check.get(
        "commented_out_match_lines"
    ):
        cooldown_reset_evidence["excluded_commented_sod_reset"] = gear_reset_check

    shared_provenance = _component(
        status="COMPLETE",
        complete=True,
        evidence=dict(provenance),
        blockers=[],
    )

    contracts: dict[str, dict[str, Any]] = {}

    gcd_components = {
        "duration": _duration_component(gcd_durations),
        "timer_start": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "source_check": source_checks.get("simulator_gcd_start"),
                "phase12_scope": movement_slam_fact,
                "live_stage": "A_timer_chains",
                "live_timer_chains": live_stage_a,
            },
            blockers=[
                "live Sunder chains calibrate the current build, but Chronicle has no client keypress/acceptance timestamp for the pre-START timer anchor",
                "the moving-Slam comparison proves castability, not numeric GCD timing",
            ],
        ),
        "reset_cancel": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "live_stage": "A_timer_chains",
                "three_sunder_chains_monotonic_to_zero": True,
            },
            blockers=[
                "the live run does not inventory GCD extension, interruption, reset, or cancellation across all actions"
            ],
        ),
        "rank_talent_build_applicability": _component(
            status="PARTIAL",
            complete=False,
            evidence={
                "slam_improved_slam_adjustment": (
                    slam.get("implementation", {}).get("improved_slam_adjustment")
                    if isinstance(slam.get("implementation"), Mapping)
                    else None
                ),
                "slam_unresolved": (
                    slam.get("calibration", {}).get("unresolved", [])
                    if isinstance(slam.get("calibration"), Mapping)
                    else []
                ),
            },
            blockers=[
                "GCD rules are not calibrated across every registered action rank/talent build",
                "current-character parameters cannot be transferred to all historical players",
            ],
        ),
        "provenance": shared_provenance,
    }
    contracts["gcd_remaining_ms"] = _finish_contract(
        "gcd_remaining_ms", gcd_components
    )

    cooldown_components = {
        "duration": _duration_component(cooldown_durations),
        "timer_start": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "source_check": source_checks.get("simulator_cooldown_start"),
                "phase12_duration_fact": duration_fact,
                "live_stage": "A_timer_chains",
                "bloodthirst": live_bloodthirst,
            },
            blockers=[
                "three live Bloodthirst chains calibrate a six-second current-build cooldown, but Chronicle lacks the client acceptance anchor",
                "Phase12 did not observe Death Wish or Recklessness expiration timing",
            ],
        ),
        "reset_cancel": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                **cooldown_reset_evidence,
                "live_stage": "A_timer_chains",
                "bloodthirst_active_retry_confirmations": live_bloodthirst.get(
                    "active_retry_confirmations"
                ),
                "live_result": "three explicit active-cooldown failures did not extend ReadyAt",
            },
            blockers=[
                *cooldown_reset_blockers,
                "the no-extension result covers Bloodthirst on the captured build, not every cooldown/shared-cooldown action",
            ],
        ),
        "rank_talent_build_applicability": _component(
            status="PARTIAL",
            complete=False,
            evidence={
                "whirlwind_current_character_ravager_rank": mechanics.get(
                    "warrior.whirlwind", {}
                ).get("implementation", {}).get("current_character_ravager_rank"),
                "whirlwind_other_ranks": mechanics.get("warrior.whirlwind", {})
                .get("calibration", {})
                .get("unresolved", []),
            },
            blockers=cooldown_applicability_blockers,
        ),
        "provenance": shared_provenance,
    }
    contracts["cooldown_remaining_ms"] = _finish_contract(
        "cooldown_remaining_ms", cooldown_components
    )

    mainhand_components = {
        "duration": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "source_schedule": source_checks.get("normal_swing_reschedule"),
                "source_haste_rescale": source_checks.get(
                    "haste_rescales_remaining_swing"
                ),
                "live_stage": "B_dual_wield_intervals",
                "locked_loadout": live_stage_b.get("locked_loadout"),
                "main_hand_baseline": (
                    live_stage_b.get("baseline", {}).get("main_hand")
                    if isinstance(live_stage_b.get("baseline"), Mapping)
                    else None
                ),
            },
            blockers=[
                "the live main-hand duration is calibrated only for item 18832 and the captured build",
                "historical Chronicle INFO does not bind exact weapon speed and complete haste state to every decision",
            ],
        ),
        "timer_start": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "source_schedule": source_checks.get("normal_swing_reschedule"),
                "live_stage": "B_dual_wield_intervals",
                "stop_restart": live_stage_b.get("stop_restart"),
                "independent_hand_baselines": live_stage_b.get("baseline"),
            },
            blockers=[
                "no hand-resolved pull anchor or complete preceding main-hand deadline is available at every decision"
            ],
        ),
        "reset_cancel": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "slam_main_hand_deadline_parameter": dict(slam_deadline or {}),
                "queue_source": source_checks.get("queued_swing_can_expire"),
                "phase12_queue_fact_superseded_for_same_guid_cancel": queue_fact,
                "live_stage_B_stop_restart": live_stage_b.get("stop_restart"),
                "live_stage_C_flurry": live_stage_c,
                "pre_patch_simulator_flurry_disposition": (
                    "PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY"
                ),
                "current_simulator_flurry_disposition": (
                    "CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE"
                ),
                "current_simulator_flurry_source": {
                    "white_swing_boundary": source_checks.get(
                        "white_swing_haste_preserves_opposite_deadline"
                    ),
                    "warrior_route": source_checks.get(
                        "warrior_flurry_uses_white_swing_boundary"
                    ),
                    "proc_aura_ids": source_checks.get(
                        "warrior_flurry_proc_aura_ids"
                    ),
                },
                "live_stage_D_same_guid_cancel": live_stage_d.get("cancel"),
                "same_guid_cancel_contract": {
                    "status": "LIVE_VERIFIED",
                    "path": "ClearTarget_TargetUnit_same_guid",
                    "queue_cancelled": True,
                    "next_main_hand_was_white": True,
                    "off_hand_continued": True,
                    "simulator_disposition": (
                        "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS"
                    ),
                    "simulator_source": {
                        "warrior": source_checks.get(
                            "warrior_explicit_queue_cancel"
                        ),
                        "environment": source_checks.get(
                            "o2o_explicit_queue_cancel"
                        ),
                        "bridge": source_checks.get(
                            "bridge_explicit_queue_cancel"
                        ),
                    },
                },
                "two_target_switch": live_target_switch,
            },
            blockers=[
                "Slam deadline handling is verified, but it is only one main-hand transition",
                "current white-swing Flurry is aligned with live Stage C, but other haste and non-white-boundary transitions are not live-calibrated",
                "the two-target target-switch mechanic is EXTERNAL_HOLD and remains unobserved",
                "extra attacks, parry haste, other haste effects, historical applicability, and all reset paths lack a complete live contract",
            ],
        ),
        "rank_talent_build_applicability": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "phase12_dual_wield": dual_fact,
                "live_locked_loadout": live_stage_b.get("locked_loadout"),
                "live_flurry_spell_id": live_stage_c.get("flurry_spell_id"),
                "live_flurry_speed_multiplier": live_stage_c.get(
                    "flurry_speed_multiplier"
                ),
            },
            blockers=[
                "the live contract covers one fixed dual-wield loadout and 5/5 Flurry, not every historical weapon/talent/build"
            ],
        ),
        "provenance": shared_provenance,
    }
    contracts["mainhand_swing_remaining_ms"] = _finish_contract(
        "mainhand_swing_remaining_ms", mainhand_components
    )

    offhand_components = {
        "duration": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "source_schedule": source_checks.get("normal_swing_reschedule"),
                "source_haste_rescale": source_checks.get(
                    "haste_rescales_remaining_swing"
                ),
                "live_stage": "B_dual_wield_intervals",
                "locked_loadout": live_stage_b.get("locked_loadout"),
                "off_hand_baseline": (
                    live_stage_b.get("baseline", {}).get("off_hand")
                    if isinstance(live_stage_b.get("baseline"), Mapping)
                    else None
                ),
            },
            blockers=[
                "the live off-hand duration is calibrated only for item 19866 and the captured build",
                "historical Chronicle INFO does not bind exact off-hand speed and complete haste state to every decision",
            ],
        ),
        "timer_start": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "source_schedule": source_checks.get("normal_swing_reschedule"),
                "live_stage": "B_dual_wield_intervals",
                "stop_restart": live_stage_b.get("stop_restart"),
                "independent_hand_baselines": live_stage_b.get("baseline"),
            },
            blockers=[
                "no hand-resolved pull anchor or complete preceding off-hand deadline is available at every decision"
            ],
        ),
        "reset_cancel": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "phase12_dual_wield": dual_fact,
                "live_stage_B_stop_restart": live_stage_b.get("stop_restart"),
                "live_stage_C_flurry": live_stage_c,
                "pre_patch_simulator_flurry_disposition": (
                    "PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY"
                ),
                "current_simulator_flurry_disposition": (
                    "CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE"
                ),
                "current_simulator_flurry_source": {
                    "white_swing_boundary": source_checks.get(
                        "white_swing_haste_preserves_opposite_deadline"
                    ),
                    "warrior_route": source_checks.get(
                        "warrior_flurry_uses_white_swing_boundary"
                    ),
                    "proc_aura_ids": source_checks.get(
                        "warrior_flurry_proc_aura_ids"
                    ),
                },
                "live_stage_D_off_hand_continued": (
                    live_stage_d.get("cancel", {}).get("completion")
                    if isinstance(live_stage_d.get("cancel"), Mapping)
                    else None
                ),
                "two_target_switch": live_target_switch,
            },
            blockers=[
                "current white-swing Flurry is aligned with live Stage C, but other haste and non-white-boundary transitions are not live-calibrated",
                "the two-target target-switch mechanic is EXTERNAL_HOLD and remains unobserved",
                "extra attacks, parry haste, other haste effects, historical applicability, and all off-hand reset paths lack a complete live contract",
            ],
        ),
        "rank_talent_build_applicability": _component(
            status="LIVE_CALIBRATED_PARTIAL",
            complete=False,
            evidence={
                "phase12_dual_wield": dual_fact,
                "live_locked_loadout": live_stage_b.get("locked_loadout"),
                "live_flurry_spell_id": live_stage_c.get("flurry_spell_id"),
                "live_flurry_speed_multiplier": live_stage_c.get(
                    "flurry_speed_multiplier"
                ),
            },
            blockers=[
                "off-hand existence, speed, haste, gear procs, and build effects are not bound into one historical-build contract"
            ],
        ),
        "provenance": shared_provenance,
    }
    contracts["offhand_swing_remaining_ms"] = _finish_contract(
        "offhand_swing_remaining_ms", offhand_components
    )
    return contracts


def _finish_contract(
    timer_field: str, components: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if set(components) != set(REQUIRED_COMPONENTS):
        raise FuryTimerTransitionContractError(
            f"{timer_field} components do not match the V1 contract"
        )
    reconstruction_allowed = all(
        bool(components[name].get("complete")) for name in REQUIRED_COMPONENTS
    )
    blockers: list[str] = []
    for name in REQUIRED_COMPONENTS:
        for blocker in components[name].get("blockers") or []:
            if blocker not in blockers:
                blockers.append(str(blocker))
    return {
        "field": timer_field,
        "contract_scope": "historical_fury_decision_state_before_START",
        "duration_is_not_remaining": True,
        "components": {name: dict(components[name]) for name in REQUIRED_COMPONENTS},
        "reconstruction_allowed": reconstruction_allowed,
        "blockers": blockers,
    }


def build_fury_timer_transition_contract(
    registry_path: str | Path = DEFAULT_REGISTRY,
    *,
    review_path: str | Path = DEFAULT_REVIEW,
    comparison_path: str | Path = DEFAULT_COMPARISON,
    live_summary_path: str | Path = DEFAULT_LIVE_SUMMARY,
    source_root: str | Path = WORKSPACE_ROOT,
) -> dict[str, Any]:
    """Build a small readiness inventory; compact decision rows are never read."""

    registry_file, registry = _load_object(registry_path, "Warrior registry")
    review_file, review = _load_object(review_path, "Phase12 review")
    comparison_file, comparison = _load_object(
        comparison_path, "Phase12 comparison"
    )
    live_summary_file, live_summary = _load_object(
        live_summary_path, "live timer source/recovery summary"
    )
    _validate_inputs(registry, review, comparison, live_summary)
    mechanics = _mechanic_index(registry)
    actions, action_map = _action_catalog(registry_file, mechanics)
    resolved_source_root = Path(source_root).expanduser().resolve()
    checks = _source_checks(resolved_source_root)
    live_stages = _live_mapping(live_summary.get("stages"), "stages")
    live_stage_c = _live_mapping(
        live_stages.get("C_flurry_haste_boundaries"), "stage C"
    )
    live_stage_d = _live_mapping(
        live_stages.get("D_heroic_strike_cancel_and_loadout"), "stage D"
    )
    live_target_switch = _live_mapping(
        live_stage_d.get("target_switch"), "stage D target switch"
    )
    checks["simulator_gcd_start"][
        "live_evidence_disposition"
    ] = "LIVE_CALIBRATED_PARTIAL"
    checks["simulator_cooldown_start"][
        "live_evidence_disposition"
    ] = "LIVE_CALIBRATED_PARTIAL"
    checks["haste_rescales_remaining_swing"][
        "live_evidence_disposition"
    ] = "CURRENT_GENERIC_NON_WHITE_PROPORTIONAL_RESCALE_SOURCE_PRESENT"
    checks["haste_rescales_remaining_swing"]["pre_patch_live_comparison"] = {
        "disposition": "PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY",
        "stage": "C_flurry_haste_boundaries",
        "flurry_spell_id": live_stage_c.get("flurry_spell_id"),
        "opposite_hand_boundary_comparisons": live_stage_c.get(
            "opposite_hand_boundary_comparisons"
        ),
        "scope": "current Turtle build and fixed calibration loadout",
    }
    for key in (
        "white_swing_haste_preserves_opposite_deadline",
        "warrior_flurry_uses_white_swing_boundary",
    ):
        checks[key][
            "live_evidence_disposition"
        ] = "CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE"
        checks[key]["live_evidence"] = {
            "stage": "C_flurry_haste_boundaries",
            "flurry_spell_id": live_stage_c.get("flurry_spell_id"),
            "opposite_hand_boundary_comparisons": live_stage_c.get(
                "opposite_hand_boundary_comparisons"
            ),
            "scope": "current Turtle build and fixed calibration loadout",
        }
    checks["warrior_flurry_proc_aura_ids"][
        "live_evidence_disposition"
    ] = "CURRENT_FLURRY_AURA_IDS_ALIGNED_WITH_LIVE"
    checks["warrior_flurry_proc_aura_ids"]["live_evidence"] = {
        "stage": "C_flurry_haste_boundaries",
        "live_rank_five_spell_id": live_stage_c.get("flurry_spell_id"),
        "source_rank_spell_ids": [12966, 12967, 12968, 12969, 12970],
    }
    checks["queued_swing_can_expire"][
        "live_evidence_disposition"
    ] = "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS"
    for key in (
        "warrior_explicit_queue_cancel",
        "o2o_explicit_queue_cancel",
        "bridge_explicit_queue_cancel",
    ):
        checks[key][
            "live_evidence_disposition"
        ] = "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS"
        checks[key]["live_evidence"] = {
            "stage": "D_heroic_strike_cancel_and_loadout",
            "request_path": "ClearTarget_TargetUnit_same_guid",
            "target_switch_status": live_target_switch.get("status"),
            "scope": "explicit same-GUID queue cancellation only",
        }
    provenance = {
        "registry": str(registry_file),
        "fury_action_registry": str(
            (PROJECT_ROOT / "o2o_dps" / "chronicle_fury_decision_dataset.py").resolve()
        ),
        "phase12_review": str(review_file),
        "phase12_review_status": review.get("status"),
        "phase12_comparison": str(comparison_file),
        "phase12_comparison_status": comparison.get("status"),
        "live_timer_summary": str(live_summary_file),
        "live_timer_summary_schema": live_summary.get("schema"),
        "live_timer_summary_kind": live_summary.get("kind"),
        "live_timer_summary_status": live_summary.get("status"),
        "live_contract_complete": _live_mapping(
            live_summary.get("evidence_gate"), "evidence gate"
        ).get("live_contract_complete"),
        "live_timer_provenance": {
            "generated_at": live_summary.get("generated_at"),
            "evidence_boundary": live_summary.get("evidence_boundary"),
            "composite_validation": live_summary.get("composite_validation"),
            "stage_B_locked_loadout_source_kind": _live_mapping(
                _live_mapping(
                    live_stages.get("B_dual_wield_intervals"), "stage B"
                ).get("locked_loadout"),
                "stage B locked loadout",
            ).get("source_kind"),
            "stage_D_loadout_restore_provenance": _live_mapping(
                _live_mapping(
                    live_stages.get("D_heroic_strike_cancel_and_loadout"),
                    "stage D",
                ).get("loadout_restore"),
                "stage D loadout restore",
            ).get("provenance"),
        },
        "live_timer_inputs": live_summary.get("inputs"),
        "source_root": str(resolved_source_root),
        "source_checks": checks,
    }
    timers = _timer_contracts(
        actions,
        mechanics,
        comparison,
        checks,
        live_summary,
        provenance,
    )
    allowed_count = sum(
        bool(timer["reconstruction_allowed"]) for timer in timers.values()
    )
    duration_action_count = sum(
        len(timer["components"]["duration"].get("evidence", {}).get("actions", []))
        for timer in timers.values()
        if isinstance(timer["components"]["duration"].get("evidence"), Mapping)
    )
    verified_duration_action_count = sum(
        timer["components"]["duration"]
        .get("evidence", {})
        .get("historical_duration_ready_action_count", 0)
        for timer in timers.values()
        if isinstance(timer["components"]["duration"].get("evidence"), Mapping)
    )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": "ok",
        "scope": {
            "class": "WARRIOR",
            "spec": "Fury",
            "state_boundary": "strictly before each Chronicle START",
            "full_state": False,
            "readiness_only": True,
            "compact_rows_read": 0,
            "decision_rows_rewritten": 0,
            "behavior_cloning_started": False,
            "offline_rl_started": False,
        },
        "inputs": {
            "registry": str(registry_file),
            "registry_schema_version": registry.get("schema_version"),
            "phase12_review": str(review_file),
            "phase12_review_kind": review.get("kind"),
            "phase12_comparison": str(comparison_file),
            "phase12_comparison_kind": comparison.get("kind"),
            "live_timer_summary": str(live_summary_file),
            "live_timer_summary_schema": live_summary.get("schema"),
            "live_timer_summary_kind": live_summary.get("kind"),
            "live_timer_summary_status": live_summary.get("status"),
            "live_contract_complete": _live_mapping(
                live_summary.get("evidence_gate"), "evidence gate"
            ).get("live_contract_complete"),
            "live_timer_summary_inputs": live_summary.get("inputs"),
            "live_timer_summary_evidence_boundary": live_summary.get(
                "evidence_boundary"
            ),
            "live_timer_summary_composite_validation": live_summary.get(
                "composite_validation"
            ),
            "source_root": str(resolved_source_root),
        },
        "action_registry": {
            "source": str(
                (
                    PROJECT_ROOT
                    / "o2o_dps"
                    / "chronicle_fury_decision_dataset.py"
                ).resolve()
            ),
            "role": "existing mechanics-to-active-action mapping; inventory only",
            "mechanic_action_count": len(actions),
            "spell_id_entry_count": len(action_map),
            "actions": actions,
        },
        "source_model_inventory": checks,
        "live_source_recovery_contract": {
            "source": str(live_summary_file),
            "schema": live_summary.get("schema"),
            "kind": live_summary.get("kind"),
            "status": live_summary.get("status"),
            "inputs": live_summary.get("inputs"),
            "evidence_boundary": live_summary.get("evidence_boundary"),
            "composite_validation": live_summary.get("composite_validation"),
            "evidence_gate": live_summary.get("evidence_gate"),
            "stages": live_summary.get("stages"),
        },
        "timers": timers,
        "summary": {
            "timer_field_count": len(timers),
            "reconstruction_allowed_count": allowed_count,
            "reconstruction_blocked_count": len(timers) - allowed_count,
            "all_remaining_reconstruction_blocked": allowed_count == 0,
            "duration_action_count": duration_action_count,
            "historically_transferable_verified_duration_action_count": (
                verified_duration_action_count
            ),
            "duration_parameter_presence_does_not_authorize_remaining": True,
            "live_source_recovery_contract_complete": True,
            "same_guid_heroic_strike_cancel_live_verified": True,
            "same_guid_heroic_strike_cancel_simulator_disposition": (
                "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS"
            ),
            "two_target_switch_status": live_target_switch.get("status"),
            "pre_patch_simulator_flurry_disposition": (
                "PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY"
            ),
            "simulator_flurry_remaining_rescale_disposition": (
                "CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE"
            ),
            "historical_reconstruction_allowed_by_live_summary": False,
            "compact_rows_read": 0,
            "line_level_output_materialized": False,
            "registry_modified": False,
            "wowsims_modified": False,
            "addon_modified": False,
            "behavior_cloning_started": False,
            "offline_rl_started": False,
        },
    }


def write_fury_timer_transition_contract(
    report: Mapping[str, Any], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, UnicodeError) as error:
        raise FuryTimerTransitionContractError(
            f"cannot write timer transition report {path}: {error}"
        ) from error
    return path


def audit_fury_timer_transition_contract(
    registry_path: str | Path = DEFAULT_REGISTRY,
    *,
    review_path: str | Path = DEFAULT_REVIEW,
    comparison_path: str | Path = DEFAULT_COMPARISON,
    live_summary_path: str | Path = DEFAULT_LIVE_SUMMARY,
    source_root: str | Path = WORKSPACE_ROOT,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> tuple[dict[str, Any], Path]:
    report = build_fury_timer_transition_contract(
        registry_path,
        review_path=review_path,
        comparison_path=comparison_path,
        live_summary_path=live_summary_path,
        source_root=source_root,
    )
    return report, write_fury_timer_transition_contract(report, output_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    parser.add_argument("--live-summary", type=Path, default=DEFAULT_LIVE_SUMMARY)
    parser.add_argument("--source-root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, output = audit_fury_timer_transition_contract(
            args.registry,
            review_path=args.review,
            comparison_path=args.comparison,
            live_summary_path=args.live_summary,
            source_root=args.source_root,
            output_path=args.output,
        )
    except FuryTimerTransitionContractError as error:
        print(f"Fury timer transition audit failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output),
                "timer_field_count": report["summary"]["timer_field_count"],
                "reconstruction_allowed_count": report["summary"][
                    "reconstruction_allowed_count"
                ],
                "duration_action_count": report["summary"][
                    "duration_action_count"
                ],
                "compact_rows_read": report["summary"]["compact_rows_read"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__ = [
    "DEFAULT_COMPARISON",
    "DEFAULT_LIVE_SUMMARY",
    "DEFAULT_OUTPUT",
    "DEFAULT_REGISTRY",
    "DEFAULT_REVIEW",
    "LIVE_SUMMARY_KIND",
    "LIVE_SUMMARY_SCHEMA",
    "REQUIRED_COMPONENTS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TIMER_FIELDS",
    "FuryTimerTransitionContractError",
    "audit_fury_timer_transition_contract",
    "build_fury_timer_transition_contract",
    "main",
    "write_fury_timer_transition_contract",
]


if __name__ == "__main__":
    raise SystemExit(main())
