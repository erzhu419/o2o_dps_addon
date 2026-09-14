"""Development-only conditional Cat branch on the simulator's physical key clock.

One ready clock tick invokes the candidate once.  Its Cat fallback owns the
source policy memory; WAIT abstains on this key, and every ordered sink still
passes through the same Cat executor.  This lane is not a voting comparison.
"""

from __future__ import annotations

from dataclasses import asdict
import math
from collections.abc import Mapping
from typing import Any

from . import cat_fury_ordered_sink_executor_v5 as _ordered
from .cat_external_press_pilot_v1 import (
    _abstain_wait,
    _close_no_live_target_press,
    _load_dynamic_press_clock_v1,
    _no_live_target_press_reason,
    _normalize_shared_context_receipt,
    _required_targets_dead,
)
from .cat_fury_full_policy_readiness_v4 import validate_source_decision_v4
from .cat_fury_full_policy_rollout_v5 import (
    CatFurySimulatorInputsV5,
    _RESULT_BEARING_GCD_ACTIONS_V5,
    _cat_state_mapper,
)
from .conditional_cat_branch_v1 import ConditionalCatBranchCandidateV1, FrozenRuleV1
from .expert_policy import WAIT_ACTION
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3, validate_dynamic_load_request_v3
from .fury_full_policy_rollout_v4 import _resolve_target_semantics_v4
from .sim_bridge import SimBridgeCommandError
from .sim_bridge_dynamic_v3 import _press_clock_state_v1


SCHEMA = "conditional_cat_external_press_lane/v1"


def run_conditional_cat_external_press_lane_v1(
    bridge: Any,
    request: Mapping[str, Any],
    target_contexts: Mapping[int, Any],
    *,
    seed: int,
    period_ms: int,
    rule: FrozenRuleV1,
    dynamic_load: DynamicRolloutLoadV3,
    max_presses: int = 200,
    phase_ms: int = 0,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    shared_context_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one frozen conditional rule across a complete dynamic-v3 wave."""

    if not isinstance(rule, FrozenRuleV1):
        raise TypeError("rule must be FrozenRuleV1")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")
    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    validate_dynamic_load_request_v3(dynamic_load, request)
    if dynamic_load.seed != seed:
        raise ValueError("dynamic load seed differs from press lane seed")
    target_count = len(request.get("encounter", {}).get("targets", []))
    if target_count < 1 or set(target_contexts) != set(range(target_count)):
        raise ValueError("a target context is required for every dynamic target")
    duration = request["encounter"].get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise ValueError("press lane requires a positive encounter duration")
    duration_ms = round(float(duration) * 1000)
    inputs = simulator_inputs or CatFurySimulatorInputsV5()
    if not isinstance(inputs, CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5")
    context_receipt = _normalize_shared_context_receipt(shared_context_receipt)

    candidate = ConditionalCatBranchCandidateV1(rule)
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
    presses: list[dict[str, Any]] = []
    status = "UNSUPPORTED_PRESS_LANE_NONVOTING"
    terminal_kind = "UNSUPPORTED"
    reason: str | None = None
    state: Mapping[str, Any] | None = None
    last_gcd_action = ""
    configured_at_ms: int | None = None
    first_scheduled_press_ms: int | None = None
    press_clock_configuration_mode: str | None = None
    try:
        (
            state, configured_at_ms, first_scheduled_press_ms,
            press_clock_configuration_mode,
        ) = _load_dynamic_press_clock_v1(
            bridge, request, seed, dynamic_load.config,
            period_ms=period_ms, phase_ms=phase_ms,
        )
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
            if state["needs_input"] is not True:
                raise RuntimeError("ready press lacks simulator input opportunity")
            before = dict(state)
            no_target_reason = _no_live_target_press_reason(
                before, target_count=target_count,
            )
            if no_target_reason is not None:
                press, state = _close_no_live_target_press(
                    bridge, clock, before, no_target_reason,
                )
                press["policy_proposal_origin"] = "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
                presses.append(press)
                continue
            target = _resolve_target_semantics_v4(before, request, target_contexts)
            combat = mapper(
                before, bridge.actions(), request, target,
                last_gcd_action=last_gcd_action,
            )
            intervention_count = len(candidate.interventions)
            proposal = validate_source_decision_v4(candidate.propose(combat))
            changed = len(candidate.interventions) != intervention_count
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
            else:
                execution = None
            # A sink may defeat the last target on this very key.  Native
            # FinishPress then rejects the now-dead opportunity, although the
            # source invocation and accepted sink both genuinely happened.
            immediate = execution.get("final_state") if execution is not None else None
            if isinstance(immediate, Mapping) and immediate.get("finished") is True:
                closed = dict(immediate)
                finish_disposition = "SKIPPED_MODEL_TERMINAL"
            else:
                try:
                    closed = bridge.finish_press()
                    finish_disposition = "FINISH_PRESS"
                except SimBridgeCommandError:
                    observed = bridge.state()
                    if observed.get("finished") is not True:
                        raise
                    closed = observed
                    finish_disposition = "SKIPPED_MODEL_TERMINAL"
            presses.append({
                "press_index": clock.press_index,
                "time_ms": before["time_ms"],
                "target_index": before.get("target_index"),
                "source_invocation_count": 1,
                "proposal": proposal.to_dict(),
                "ordered_execution": execution,
                "policy_proposal_origin": "CONDITIONAL_BRANCH" if changed else "CAT_UNCHANGED",
                "source_wait_abstained": proposal.gcd == WAIT_ACTION,
                "policy_disposition": "SOURCE_INVOKED",
                "press_closure": finish_disposition,
                "finish_press_time_ms": closed["time_ms"],
                "finish_press_ready": (
                    _press_clock_state_v1(closed).ready if finish_disposition == "FINISH_PRESS" else None
                ),
            })
            state = closed
            if not proposal.valid:
                reason = "source proposal invalid at a scheduled press"
                break
            if execution["execution_blocked"] or execution["nonfaithful_reasons"]:
                reason = "source ordered sink could not be executed faithfully"
                break
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
    return {
        "schema": SCHEMA,
        "status": status,
        "terminal": {
            "kind": terminal_kind,
            "model_finished": terminal_kind in {
                "MODEL_TARGET_DEFEATED", "DURATION_CENSORED", "MODEL_TERMINAL_UNRESOLVED",
            },
            "required_hostiles_defeated": _required_targets_dead(
                state if isinstance(state, Mapping) else None,
                dynamic=True, target_count=target_count,
            ),
            "target_health_known": state.get("target_health_known") if isinstance(state, Mapping) else None,
            "target_health": (
                state.get("target_health") if isinstance(state, Mapping)
                and state.get("target_health_known") is True else None
            ),
            "target_health_max": (
                state.get("target_health_max") if isinstance(state, Mapping)
                and state.get("target_health_known") is True else None
            ),
            "duration_ms": duration_ms,
            "watchdog_truncated": terminal_kind == "WATCHDOG_TRUNCATED",
            "reason": reason,
            "time_ms": state.get("time_ms") if isinstance(state, Mapping) else None,
        },
        "policy_kind": "conditional_candidate",
        "rule": asdict(rule),
        "interventions": list(candidate.interventions),
        "intervention_count": len(candidate.interventions),
        "mode": "DYNAMIC_V3_WHOLE_WAVE",
        "seed": seed,
        "period_ms": period_ms,
        "press_phase_ms": phase_ms,
        "press_clock_configured_at_ms": configured_at_ms,
        "first_scheduled_press_ms": first_scheduled_press_ms,
        "press_clock_configuration_mode": press_clock_configuration_mode,
        "press_count": len(presses),
        "presses": presses,
        "final_state": dict(state) if isinstance(state, Mapping) else None,
        "simulator_input_receipt": inputs.receipt(),
        "shared_context_receipt": context_receipt,
        "shared_context_bound": context_receipt is not None,
        "comparison_ready": False,
        "voting_eligible": False,
        "live_fidelity": False,
    }
