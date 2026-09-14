"""Cat-relative one-ActionPlan teacher on the physical press clock.

The baseline and every branch start from an independent native load with the
same simulator seed and press period.  A branch changes exactly one Cat
proposal at one observed press, then resumes the same Cat controller.  Whole
wave terminal damage is an offline teacher label; it is never a policy input.

This module is development-only and non-voting.  Its compact ``branches`` rows
match the input projection consumed by ``factored_cat_branch_router_v1``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from enum import Enum
from itertools import combinations
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

from . import cat_fury_ordered_sink_executor_v5 as _ordered
from .branch_teacher_v1 import BranchReplayMismatchV1, _observation_receipt, _run_fresh
from .cat_action_branch_search_v1 import (
    POLICY_ID as ACTION_BRANCH_POLICY_ID,
    ActionBranchV1,
    BRANCH_KINDS,
    CatActionBranchCandidateV1,
    available_branches_v1,
)
from .cat_external_press_pilot_v1 import (
    _abstain_wait,
    _first_scheduled_press_ms,
    _load_dynamic_press_clock_with_receipt_v1,
    _no_live_target_press_reason,
    _required_targets_dead,
)
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    validate_source_decision_v4,
)
from .cat_fury_full_policy_rollout_v5 import (
    CatFurySimulatorInputsV5,
    _RESULT_BEARING_GCD_ACTIONS_V5,
    _cat_state_mapper,
)
from .conditional_cat_branch_v1 import FrozenRuleV1, _signature
from .cat_sparse_guard_policy_v2 import (
    FEATURE_ORDER,
    SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
    exact_current_branch_kinds_v2,
    sparse_guard_features_v2,
)
from .development_wave_case_v1 import DevelopmentWaveCaseV1, build_development_wave_case_v1
from .expert_policy import WAIT_ACTION
from .fury_dynamic_target_semantics_v5 import validate_dynamic_load_request_v3
from .fury_full_policy_rollout_v4 import _resolve_target_semantics_v4
from .sim_bridge import SimBridgeCommandError
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3, _press_clock_state_v1


SCHEMA = "cat_external_press_action_teacher/v1"
LANE_SCHEMA = "cat_external_press_action_branch_lane/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = PROJECT_ROOT / "bin/o2obridge.press-v19.exe"
FLOAT_IDENTITY_TOLERANCE = 1e-9
_SELECTION_PHASES = ("EARLY", "MIDDLE", "LATE")
STATE_SELECTION_CONTRACT = (
    "DETERMINISTIC_STREAMING_FIRST_OPPORTUNITY_DEPTH_1_OR_2_SPARSE_GUARD_"
    "BASIS_COVERAGE;CANONICAL_FEATURE_ORDER;NO_CARTESIAN_UNOBSERVED_GUARDS;"
    "UNIQUE_DECISIONS_LIMITED_BY_MAX_STATES;ALL_NEW_GUARD_KINDS_AT_SELECTED_"
    "DECISION_EMITTED;"
    "EVERY_BRANCH_REQUIRES_EXACT_CURRENT_ACTION_REF_LEGAL_READY_AND_EXPECTED_GCD_LANE;"
    "CURRENT_OBSERVATION_PREFIX_ONLY;NO_REWARD_OR_FUTURE_SUFFIX_INPUT;"
    "FULL_BASELINE_EXACT_ACTION_OPPORTUNITY_COVERAGE_INDEPENDENT_OF_MAX_STATES;"
    "FULL_BASELINE_SPARSE_ACTION_OPPORTUNITIES_INDEPENDENT_OF_MAX_STATES"
)
ACTION_OPPORTUNITY_CONTRACT = (
    "EVERY_FULL_BASELINE_PRESS_WITH_BRANCH_OFFER_AND_EXACT_CURRENT_ACTION_REF;"
    "ACTION_LEGAL_READY_ZERO_AND_EXPECTED_GCD_LANE;"
    "CURRENT_OBSERVATION_RULE_SIGNATURE_ONLY;NO_BRANCH_OUTCOME_OR_SUFFIX"
)

_FULL_PREFIX_FIELDS = (
    "decision_index",
    "press_index",
    "time_ms",
    "target_index",
    "source_invocation_count",
    "simulator_state_before",
    "available_actions_before",
    "target_semantics",
    "expert_state",
    "proposal",
    "ordered_execution",
    "press_closure",
    "finish_press_time_ms",
    "finish_press_ready",
    "simulator_state_after_press",
)
_BRANCH_OBSERVATION_FIELDS = (
    "decision_index",
    "press_index",
    "time_ms",
    "target_index",
    "simulator_state_before",
    "available_actions_before",
    "target_semantics",
    "expert_state",
)


def _wire(value: Any) -> Any:
    """Project typed current-state receipts to deterministic JSON values."""

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _wire(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    return value


def _normalized_paired_delta(candidate: float, baseline: float) -> float:
    """Remove deterministic floating residue from otherwise identical totals."""

    delta = float(candidate) - float(baseline)
    return 0.0 if abs(delta) <= FLOAT_IDENTITY_TOLERANCE else delta


def _clock_receipts_valid(artifact: Mapping[str, Any]) -> bool:
    presses = artifact.get("presses") or []
    period_ms = artifact.get("period_ms")
    final = artifact.get("final_state") or {}
    if type(period_ms) is not int or period_ms < 1:
        return False
    first_ms = artifact.get("first_scheduled_press_ms")
    if type(first_ms) is not int or first_ms < 0:
        return False
    phase_ms = artifact.get("press_phase_ms")
    configured_at_ms = artifact.get("press_clock_configured_at_ms")
    if (
        type(phase_ms) is not int
        or not 0 <= phase_ms < period_ms
        or type(configured_at_ms) is not int
        or configured_at_ms < 0
        or first_ms != _first_scheduled_press_ms(
            configured_at_ms, period_ms=period_ms, phase_ms=phase_ms,
        )
        or not (
            artifact.get("press_clock_configuration_mode")
            == "ATOMIC_DYNAMIC_V3_PRESS_CLOCK"
            or (
                artifact.get("press_clock_configuration_mode")
                == "SEPARATE_AFTER_DYNAMIC_LOAD"
                and configured_at_ms == 0
            )
        )
    ):
        return False
    expected_decision = 0
    for index, press in enumerate(presses):
        closure = press.get("press_closure")
        closed = (
            closure == "FINISH_PRESS" and press.get("finish_press_ready") is False
        ) or (
            closure == "SKIPPED_MODEL_TERMINAL"
            and index == len(presses) - 1
            and final.get("finished") is True
            and press.get("finish_press_ready") is None
        )
        invoked = press.get("source_invocation_count")
        valid_disposition = (
            invoked == 1 and press.get("decision_index") == expected_decision
        ) or (
            invoked == 0
            and press.get("decision_index") is None
            and press.get("policy_disposition") == "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
            and press.get("proposal") is None
        )
        if not (
            press.get("press_index") == index + 1
            and press.get("time_ms") == first_ms + index * period_ms
            and valid_disposition and closed
        ):
            return False
        if invoked == 1:
            expected_decision += 1
    return artifact.get("press_count") == len(presses)


def _terminal_receipt(
    case: DevelopmentWaveCaseV1, artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Adjudicate a complete dynamic wave from external-press receipts."""

    terminal = artifact.get("terminal") or {}
    final = artifact.get("final_state") or {}
    team = final.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required = case.case_spec["required_target_indices"]
    dead = (
        isinstance(targets, list)
        and all(
            index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("target_index") == index
            and targets[index].get("dead") is True
            for index in required
        )
    )
    score = team.get("simulated_damage_applied") if isinstance(team, Mapping) else None
    score_valid = (
        type(score) in (int, float) and math.isfinite(score)
    )
    complete = (
        artifact.get("status") == "TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING"
        and terminal.get("kind") == "MODEL_TARGET_DEFEATED"
        and terminal.get("required_hostiles_defeated") is True
        and terminal.get("reason") is None
        and final.get("finished") is True
        and dead
        and score_valid
        and _clock_receipts_valid(artifact)
    )
    return {
        "status": "COMPLETED" if complete else (
            "CENSORED_DURATION" if terminal.get("kind") == "DURATION_CENSORED"
            else "CENSORED_PRESS_WATCHDOG" if terminal.get("kind") == "WATCHDOG_TRUNCATED"
            else "FAILED_TERMINAL_INCONSISTENT_OR_INCOMPLETE"
        ),
        "terminal_reason": terminal.get("kind"),
        "required_target_ids": case.case_spec["required_target_ids"],
        "required_targets_dead": bool(dead),
        "elapsed_ms": final.get("time_ms") if complete else None,
        "own_effective_damage": float(score) if complete else None,
        "own_reported_damage": final.get("damage_done") if complete else None,
        "background_effective_damage": (
            team.get("background_damage_applied") if complete else None
        ) if isinstance(team, Mapping) else None,
        "clock_receipts_valid": _clock_receipts_valid(artifact),
        "press_count": artifact.get("press_count"),
        "target_outcomes": _wire(targets) if isinstance(targets, list) else None,
    }


def _run_press_lane(
    bridge: Any,
    case: DevelopmentWaveCaseV1,
    adapter: Any,
    *,
    period_ms: int,
    max_presses: int,
    phase_ms: int = 0,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
) -> dict[str, Any]:
    """Run one Cat-compatible adapter once at every physical key press."""

    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be a positive integer")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be a positive integer")
    if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    seed = case.dynamic_load.seed
    validate_dynamic_load_request_v3(case.dynamic_load, case.request)
    target_count = len(case.request.get("encounter", {}).get("targets", []))
    if target_count < 1 or set(case.target_contexts) != set(range(target_count)):
        raise ValueError("a target context is required for every dynamic target")
    duration = case.request["encounter"].get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise ValueError("press teacher requires a positive encounter duration")
    duration_ms = round(float(duration) * 1000)
    inputs = simulator_inputs or CatFurySimulatorInputsV5()
    if not isinstance(inputs, CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5")

    controls = _ordered.CatSimulatorControlFacadeV5(
        bridge,
        item_bindings=inputs.item_action_bindings,
        initial_autoattack_active=inputs.initial_autoattack_active,
        initial_cvars={
            "NP_QueueCastTimeSpells": inputs.initial_np_queue_cast_time_spells,
            "NP_QueueInstantSpells": inputs.initial_np_queue_instant_spells,
        },
    )
    mapper = _cat_state_mapper(controls, inputs)
    (
        state, configured_at_ms, first_scheduled_press_ms,
        press_clock_configuration_mode, loaded,
    ) = _load_dynamic_press_clock_with_receipt_v1(
        bridge, case.request, seed, case.dynamic_load.config,
        period_ms=period_ms, phase_ms=phase_ms,
    )
    presses: list[dict[str, Any]] = []
    decision_count = 0
    last_gcd_action = ""
    status = "UNSUPPORTED_PRESS_TEACHER_LANE_NONVOTING"
    terminal_kind = "UNSUPPORTED"
    while True:
        if state["finished"]:
            if _required_targets_dead(state, dynamic=True, target_count=target_count) is True:
                status = "TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING"
                terminal_kind = "MODEL_TARGET_DEFEATED"
            elif state["time_ms"] >= duration_ms:
                status = "DURATION_CENSORED_NONVOTING"
                terminal_kind = "DURATION_CENSORED"
            else:
                status = "MODEL_TERMINAL_UNRESOLVED_NONVOTING"
                terminal_kind = "MODEL_TERMINAL_UNRESOLVED"
            break
        if len(presses) >= max_presses:
            status = "WATCHDOG_TRUNCATED_NONVOTING"
            terminal_kind = "WATCHDOG_TRUNCATED"
            break
        if not _press_clock_state_v1(state).ready:
            state = bridge.advance()
            continue
        clock = _press_clock_state_v1(state)
        if state.get("needs_input") is not True:
            raise RuntimeError("ready press lacks simulator input opportunity")
        before = dict(state)
        no_target_reason = _no_live_target_press_reason(
            before, target_count=target_count,
        )
        if no_target_reason is not None:
            closed = bridge.finish_press()
            presses.append({
                "decision_index": None,
                "press_index": clock.press_index,
                "time_ms": before["time_ms"],
                "target_index": before.get("target_index"),
                "source_invocation_count": 0,
                "simulator_state_before": _wire(before),
                "available_actions_before": None,
                "target_semantics": None,
                "expert_state": None,
                "available_branch_kinds": [],
                "proposal": None,
                "ordered_execution": None,
                "source_wait_abstained": None,
                "policy_disposition": "NO_LIVE_TARGET_ENVIRONMENT_NOOP",
                "no_live_target_reason": no_target_reason,
                "press_closure": "FINISH_PRESS",
                "finish_press_time_ms": closed["time_ms"],
                "finish_press_ready": _press_clock_state_v1(closed).ready,
                "simulator_state_after_press": _wire(closed),
            })
            state = closed
            continue
        available = bridge.actions()
        target = _resolve_target_semantics_v4(before, case.request, case.target_contexts)
        policy_state = mapper(
            before, available, case.request, target,
            last_gcd_action=last_gcd_action,
        )
        if not isinstance(policy_state, CatFuryFullPolicyStateV4):
            raise TypeError("Cat mapper returned an undeclared policy state")
        bind_actions = getattr(adapter, "bind_current_available_actions_v2", None)
        if bind_actions is not None:
            if not callable(bind_actions):
                raise TypeError("adapter current-action binder must be callable")
            bind_actions(available)
        proposal = validate_source_decision_v4(adapter.propose(policy_state))
        kinds = available_branches_v1(policy_state, proposal)
        execution = None
        if proposal.valid:
            execution = _ordered.execute_cat_fury_ordered_sinks_v5(
                controls, proposal, before,
                attempt_id_prefix=f"press-{clock.press_index}",
                result_bearing_action_keys=tuple(sorted(_RESULT_BEARING_GCD_ACTIONS_V5)),
                wait_executor=_abstain_wait,
            )
            accepted = execution.get("accepted_gcd_actions") or []
            if accepted:
                last_gcd_action = accepted[-1]
        immediate = execution.get("final_state") if isinstance(execution, Mapping) else None
        if isinstance(immediate, Mapping) and immediate.get("finished") is True:
            closed = dict(immediate)
            closure = "SKIPPED_MODEL_TERMINAL"
            finish_ready = None
        else:
            try:
                closed = bridge.finish_press()
                closure = "FINISH_PRESS"
                finish_ready = _press_clock_state_v1(closed).ready
            except SimBridgeCommandError:
                observed = bridge.state()
                if observed.get("finished") is not True:
                    raise
                closed = observed
                closure = "SKIPPED_MODEL_TERMINAL"
                finish_ready = None
        presses.append({
            "decision_index": decision_count,
            "press_index": clock.press_index,
            "time_ms": before["time_ms"],
            "target_index": before.get("target_index"),
            "source_invocation_count": 1,
            "simulator_state_before": _wire(before),
            "available_actions_before": _wire(available),
            "target_semantics": _wire(target),
            "expert_state": _wire(asdict(policy_state)),
            "available_branch_kinds": list(kinds),
            "proposal": proposal.to_dict(),
            "ordered_execution": _wire(execution),
            "source_wait_abstained": proposal.gcd == WAIT_ACTION,
            "policy_disposition": "SOURCE_INVOKED",
            "press_closure": closure,
            "finish_press_time_ms": closed["time_ms"],
            "finish_press_ready": finish_ready,
            "simulator_state_after_press": _wire(closed),
        })
        decision_count += 1
        state = closed
        if not proposal.valid:
            raise RuntimeError("source proposal invalid at a scheduled press")
        if execution["execution_blocked"] or execution["nonfaithful_reasons"]:
            raise RuntimeError("source ordered sink could not be executed faithfully")

    return {
        "schema": LANE_SCHEMA,
        "status": status,
        "terminal": {
            "kind": terminal_kind,
            "model_finished": terminal_kind in {
                "MODEL_TARGET_DEFEATED", "DURATION_CENSORED", "MODEL_TERMINAL_UNRESOLVED",
            },
            "required_hostiles_defeated": _required_targets_dead(
                state, dynamic=True, target_count=target_count,
            ),
            "duration_ms": duration_ms,
            "watchdog_truncated": terminal_kind == "WATCHDOG_TRUNCATED",
            "reason": None,
            "time_ms": state.get("time_ms"),
        },
        "mode": "DYNAMIC_V3_WHOLE_WAVE",
        "seed": seed,
        "period_ms": period_ms,
        "press_phase_ms": phase_ms,
        "press_clock_configured_at_ms": configured_at_ms,
        "first_scheduled_press_ms": first_scheduled_press_ms,
        "press_clock_configuration_mode": press_clock_configuration_mode,
        "press_count": len(presses),
        "presses": presses,
        "final_state": _wire(state),
        "dynamic_load_receipt": _wire(getattr(loaded, "receipt", None)),
        "comparison_ready": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _same_press_prefix(
    baseline: Mapping[str, Any], alternative: Mapping[str, Any], decision_index: int,
) -> int:
    """Verify equal physical presses and observations before one intervention."""

    if (
        baseline.get("seed") != alternative.get("seed")
        or baseline.get("period_ms") != alternative.get("period_ms")
        or baseline.get("mode") != "DYNAMIC_V3_WHOLE_WAVE"
        or alternative.get("mode") != "DYNAMIC_V3_WHOLE_WAVE"
    ):
        raise BranchReplayMismatchV1("branch seed, period, or dynamic mode differs")
    base_presses = baseline.get("presses") or []
    other_presses = alternative.get("presses") or []
    base_position = next((
        index for index, press in enumerate(base_presses)
        if press.get("decision_index") == decision_index
    ), None)
    other_position = next((
        index for index, press in enumerate(other_presses)
        if press.get("decision_index") == decision_index
    ), None)
    if base_position is None or other_position is None:
        raise BranchReplayMismatchV1("one press lane ended before the proposed branch")
    if base_position != other_position:
        raise BranchReplayMismatchV1("branch decision reached on a different physical press")
    for index in range(base_position):
        for field in _FULL_PREFIX_FIELDS:
            if base_presses[index].get(field) != other_presses[index].get(field):
                raise BranchReplayMismatchV1(
                    f"physical press prefix differs at {index}: {field}"
                )
    for field in _BRANCH_OBSERVATION_FIELDS:
        if base_presses[base_position].get(field) != other_presses[base_position].get(field):
            raise BranchReplayMismatchV1(f"branch press observation differs: {field}")
    return base_position


def _selected_points(
    baseline: Mapping[str, Any], *, max_states: int,
) -> list[tuple[int, int, str]]:
    """Cover each observed sparse guard at its first executable opportunity."""

    selected: list[tuple[int, int, tuple[str, ...]]] = []
    covered_guards: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for physical_index, press in enumerate(baseline.get("presses") or []):
        kinds = _eligible_point_kinds(press)
        if not kinds:
            continue
        observation = press.get("expert_state")
        if not isinstance(observation, Mapping):
            raise BranchReplayMismatchV1(
                "eligible Cat press lacks its complete current observation"
            )
        features = sparse_guard_features_v2(observation)
        items = tuple((name, features[name]) for name in FEATURE_ORDER)
        keys_by_kind = {
            kind: {
                (kind, predicates)
                for depth in (1, 2)
                for predicates in combinations(items, depth)
            }
            for kind in kinds
        }
        new_kinds = tuple(
            kind for kind in kinds
            if not keys_by_kind[kind].issubset(covered_guards)
        )
        if not new_kinds:
            continue
        selected.append((physical_index, int(press["decision_index"]), new_kinds))
        for kind in kinds:
            covered_guards.update(keys_by_kind[kind])
        if len(selected) >= max_states:
            break
    return [
        (physical_index, decision_index, kind)
        for physical_index, decision_index, kinds in selected for kind in kinds
    ]


def _eligible_point_kinds(press: Mapping[str, Any]) -> tuple[str, ...]:
    """Return branches physically executable at this observed Cat press."""

    offered = list(press.get("available_branch_kinds") or ())
    if not offered:
        return ()
    proposal = press.get("proposal")
    available = press.get("available_actions_before")
    if not isinstance(proposal, Mapping) or not isinstance(available, list):
        raise BranchReplayMismatchV1(
            "Cat press lacks its proposal or current available-action snapshot"
        )
    return exact_current_branch_kinds_v2(
        offered, proposal, available,
    )


def _action_opportunity_coverage(
    baseline: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Inventory exact executable rules over the complete baseline trajectory.

    This inventory is independent of ``max_states`` and branch rewards.  It is
    therefore suitable for deciding whether a pooled rule can ever trigger on
    its destination route, without pretending that an opportunity is an effect
    estimate.
    """

    rows: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for press in baseline.get("presses") or []:
        kinds = _eligible_point_kinds(press)
        if not kinds:
            continue
        decision_index = press.get("decision_index")
        expert = press.get("expert_state")
        combat = expert.get("combat") if isinstance(expert, Mapping) else None
        if type(decision_index) is not int or not isinstance(combat, Mapping):
            raise BranchReplayMismatchV1(
                "eligible Cat press lacks a decision index or current combat observation"
            )
        phase = _selection_phase(press)
        for kind in kinds:
            signature = _signature(combat, kind)
            key = (kind, signature)
            row = rows.setdefault(key, {
                "rule": asdict(FrozenRuleV1(kind, *signature)),
                "opportunity_press_count": 0,
                "first_decision_index": decision_index,
                "last_decision_index": decision_index,
                "phase_counts": {name: 0 for name in _SELECTION_PHASES},
            })
            row["opportunity_press_count"] += 1
            row["last_decision_index"] = decision_index
            row["phase_counts"][phase] += 1
    kind_order = {kind: index for index, kind in enumerate(BRANCH_KINDS)}
    return [
        rows[key]
        for key in sorted(rows, key=lambda item: (kind_order[item[0]], item[1]))
    ]


def _sparse_action_opportunities(
    baseline: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Retain each executable press for sparse-guard reachability only."""

    rows = []
    for press in baseline.get("presses") or []:
        kinds = _eligible_point_kinds(press)
        if not kinds:
            continue
        decision_index = press.get("decision_index")
        observation = press.get("expert_state")
        if type(decision_index) is not int or not isinstance(observation, Mapping):
            raise BranchReplayMismatchV1(
                "eligible Cat press lacks a decision index or complete observation"
            )
        features = sparse_guard_features_v2(observation)
        rows.extend({
            "kind": kind,
            "decision_index": decision_index,
            "features": dict(features),
        } for kind in kinds)
    return rows


def _selection_phase(press: Mapping[str, Any]) -> str:
    expert = press.get("expert_state") or {}
    combat = expert.get("combat") if isinstance(expert, Mapping) else None
    health = combat.get("target_health_pct") if isinstance(combat, Mapping) else None
    if (
        isinstance(health, bool)
        or not isinstance(health, (int, float))
        or not math.isfinite(health)
    ):
        raise BranchReplayMismatchV1(
            "eligible Cat press lacks finite current target_health_pct"
        )
    if health > 70.0:
        return _SELECTION_PHASES[0]
    if health >= 20.0:
        return _SELECTION_PHASES[1]
    return _SELECTION_PHASES[2]


def _action_receipts(press: Mapping[str, Any]) -> list[dict[str, Any]]:
    execution = press.get("ordered_execution") or {}
    return [
        {
            "source_sink": event.get("source_sink"),
            "simulator_submission": event.get("simulator_submission"),
            "simulator_acceptance": event.get("simulator_acceptance"),
        }
        for event in execution.get("sink_events") or []
        if isinstance(event, Mapping)
        and isinstance(event.get("source_sink"), Mapping)
        and event["source_sink"].get("channel") in {"gcd", "swing_queue"}
    ]


def _branch_action_accepted(
    kind: str, baseline_press: Mapping[str, Any], candidate_press: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    baseline_actions = _action_receipts(baseline_press)
    candidate_actions = _action_receipts(candidate_press)
    if kind in {"ADD_HS_QUEUE", "BT_TO_WW", "WW_TO_BT"}:
        accepted = any(
            str((row.get("source_sink") or {}).get("source_ref", "")).startswith(
                ACTION_BRANCH_POLICY_ID
            )
            and (row.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
            for row in candidate_actions
        )
    else:
        removed = "swing_queue" if kind == "SUPPRESS_QUEUE" else "gcd"
        accepted = any(
            (row.get("source_sink") or {}).get("channel") == removed
            and (row.get("simulator_acceptance") or {}).get("status") == "ACCEPTED"
            for row in baseline_actions
        )
    return accepted, {"cat": baseline_actions, "candidate": candidate_actions}


def run_cat_external_press_action_teacher_v1(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    *,
    period_ms: int = 100,
    max_states: int = 1,
    max_presses: int = 400,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
) -> dict[str, Any]:
    """Evaluate current-observation ActionPlans on independent full waves."""

    if type(max_states) is not int or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    run = lambda bridge, adapter: _run_press_lane(
        bridge, case, adapter, period_ms=period_ms, max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    baseline = _run_fresh(
        bridge_factory, lambda bridge: run(bridge, CatFuryFullPolicyAdapterV4()),
    )
    baseline_terminal = _terminal_receipt(case, baseline)
    action_opportunities = _action_opportunity_coverage(baseline)
    sparse_action_opportunities = _sparse_action_opportunities(baseline)
    common = {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "period_ms": period_ms,
        "replay_method": "FRESH_SAME_SEED_SAME_PERIOD_DYNAMIC_WHOLE_WAVE_PER_BRANCH",
        "state_selection_contract": STATE_SELECTION_CONTRACT,
        "action_opportunity_contract": ACTION_OPPORTUNITY_CONTRACT,
        "action_opportunity_rule_count": len(action_opportunities),
        "action_opportunity_press_count": sum(
            row["opportunity_press_count"] for row in action_opportunities
        ),
        "action_opportunity_coverage": action_opportunities,
        "sparse_action_opportunity_contract": (
            SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2
        ),
        "sparse_action_opportunity_count": len(sparse_action_opportunities),
        "sparse_action_opportunities": sparse_action_opportunities,
        "policy_input_contract": "CURRENT_CAT_FURY_FULL_POLICY_STATE_V4_ONLY",
        "terminal_label_contract": "OFFLINE_WHOLE_WAVE_OUTCOME_NOT_VISIBLE_TO_POLICY",
        "hidden_rng_snapshot_verified": False,
        "causal_effect_established": False,
        "comparison_ready": False,
        "voting_eligible": False,
        "deployment_eligible": False,
        "scientific_run_launched": False,
        "adapter_contract": {
            "consumer": "factored_cat_branch_router_v1._teacher_rows",
            "directly_compatible": True,
            "required_top_level_fields": [
                "seed", "source_wave_ref", "request_sha256",
                "dynamic_load_contract_sha256", "baseline_terminal", "branches",
            ],
            "required_branch_fields": [
                "kind", "status", "branch_action_accepted",
                "paired_effective_damage_delta", "policy_observation",
            ],
        },
        "baseline_terminal": baseline_terminal,
        "baseline_press_count": baseline.get("press_count"),
        "baseline_press_clock_configuration_mode": baseline.get(
            "press_clock_configuration_mode"
        ),
    }
    if baseline_terminal["status"] != "COMPLETED":
        return {
            **common,
            "status": "BASELINE_INCOMPLETE_NO_BRANCHES_SCORED",
            "selected_state_count": 0,
            "selected_phase_kind_count": 0,
            "selected_phase_kind_coverage": [],
            "independent_action_branch_count": 0,
            "accepted_action_branch_count": 0,
            "completed_teacher_label_count": 0,
            "positive_single_seed_label_count": 0,
            "policy_update_rounds_completed": 0,
            "branches": [],
        }

    branches: list[dict[str, Any]] = []
    for physical_index, decision_index, kind in _selected_points(
        baseline, max_states=max_states,
    ):
        candidate = CatActionBranchCandidateV1(ActionBranchV1(decision_index, kind))
        alternative = _run_fresh(
            bridge_factory, lambda bridge, candidate=candidate: run(bridge, candidate),
        )
        _same_press_prefix(baseline, alternative, decision_index)
        interventions = candidate.interventions
        changed_presses = [
            press for press in alternative["presses"]
            if isinstance(press.get("proposal"), Mapping)
            and press["proposal"].get("metadata", {}).get("action_branch_policy")
            == ACTION_BRANCH_POLICY_ID
        ]
        if (
            len(interventions) != 1
            or interventions[0].get("decision_index") != decision_index
            or len(changed_presses) != 1
            or changed_presses[0].get("decision_index") != decision_index
        ):
            raise BranchReplayMismatchV1("targeted physical-press branch did not occur exactly once")
        branch_press = alternative["presses"][physical_index]
        base_press = baseline["presses"][physical_index]
        if interventions[0]["cat_proposal"] != base_press["proposal"]:
            raise BranchReplayMismatchV1("Cat proposal changed at the branch observation")
        if interventions[0]["candidate_proposal"] != branch_press["proposal"]:
            raise BranchReplayMismatchV1("branch proposal differs from its intervention receipt")
        accepted, actions = _branch_action_accepted(kind, base_press, branch_press)
        terminal = _terminal_receipt(case, alternative)
        complete = terminal["status"] == baseline_terminal["status"] == "COMPLETED"
        delta = (
            _normalized_paired_delta(
                terminal["own_effective_damage"], baseline_terminal["own_effective_damage"],
            )
            if complete else None
        )
        branches.append({
            "decision_index": decision_index,
            "press_index": base_press["press_index"],
            "kind": kind,
            "selection_phase": _selection_phase(base_press),
            "accepted_prefix_presses_verified": physical_index,
            "accepted_prefix_decisions_verified": decision_index,
            "policy_observation": _observation_receipt(
                interventions[0]["policy_observation"]
            ),
            "cat_proposal": interventions[0]["cat_proposal"],
            "candidate_proposal": interventions[0]["candidate_proposal"],
            "branch_action_receipt": actions,
            "branch_action_accepted": accepted,
            "strict_single_intervention_verified": True,
            "branch_state_changed_after_press": (
                base_press["simulator_state_after_press"]
                != branch_press["simulator_state_after_press"]
            ),
            "branch_terminal": terminal,
            "press_clock_configuration_mode": alternative.get(
                "press_clock_configuration_mode"
            ),
            "paired_effective_damage_delta": delta,
            "status": (
                "COMPLETE_BRANCH_SMOKE" if complete and accepted
                else "COMPLETE_NO_ACCEPTED_ACTION" if complete
                else "CENSORED_OR_FAILED_BRANCH"
            ),
        })
    phase_order = {phase: index for index, phase in enumerate(_SELECTION_PHASES)}
    kind_order = {kind: index for index, kind in enumerate(BRANCH_KINDS)}
    phase_kind_coverage = sorted(
        {
            (row["selection_phase"], row["kind"])
            for row in branches
        },
        key=lambda cell: (phase_order[cell[0]], kind_order[cell[1]]),
    )
    return {
        **common,
        "status": "COMPLETE_EXTERNAL_PRESS_TEACHER_NONVOTING",
        "selected_state_count": len({row["decision_index"] for row in branches}),
        "selected_phase_kind_count": len(phase_kind_coverage),
        "selected_phase_kind_coverage": [
            {"phase": phase, "kind": kind}
            for phase, kind in phase_kind_coverage
        ],
        "independent_action_branch_count": len(branches),
        "accepted_action_branch_count": sum(
            row["branch_action_accepted"] for row in branches
        ),
        "completed_teacher_label_count": sum(
            row["status"] == "COMPLETE_BRANCH_SMOKE" for row in branches
        ),
        "positive_single_seed_label_count": sum(
            row["status"] == "COMPLETE_BRANCH_SMOKE"
            and row["paired_effective_damage_delta"] > 0
            for row in branches
        ),
        "policy_update_rounds_completed": 0,
        "branches": branches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=2026091401)
    parser.add_argument("--period-ms", type=int, default=100)
    parser.add_argument("--max-states", type=int, default=1)
    parser.add_argument("--max-presses", type=int, default=400)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=PROJECT_ROOT.parent / "wowsims-turtle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    case = build_development_wave_case_v1(args.seed)
    result = run_cat_external_press_action_teacher_v1(
        case,
        lambda: SimulatorBridgeDynamicV3(args.bridge, cwd=args.bridge_cwd),
        period_ms=args.period_ms,
        max_states=args.max_states,
        max_presses=args.max_presses,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()


__all__ = (
    "STATE_SELECTION_CONTRACT", "ACTION_OPPORTUNITY_CONTRACT",
    "run_cat_external_press_action_teacher_v1",
    "_same_press_prefix", "_eligible_point_kinds", "_action_opportunity_coverage",
    "_sparse_action_opportunities",
)
