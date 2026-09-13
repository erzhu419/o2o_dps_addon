"""Development-only Cat source invocation on a simulator-owned key clock.

This pilot deliberately does not reuse the legacy full-rollout scheduler: Cat's
source WAIT is an abstention on the current physical key, not a request for a
new simulator decision after ``wait_ms``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from . import cat_fury_ordered_sink_executor_v5 as _ordered
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    validate_source_decision_v4,
)
from .cat_fury_full_policy_rollout_v5 import (
    CatFurySimulatorInputsV5,
    _RESULT_BEARING_GCD_ACTIONS_V5,
    _cat_state_mapper,
)
from .cat_residual_candidate_rollout_v1 import (
    CatQueueResidualV1,
    CatResidualCandidateV1,
)
from .expert_policy import WAIT_ACTION
from .fury_full_policy_rollout_v2 import _resolve_target_semantics
from .sim_bridge_dynamic_v3 import _press_clock_state_v1


SCHEMA = "cat_external_press_pilot/v1"
POLICY_KINDS = ("cat", "zero_residual")


def _abstain_wait(
    bridge: Any,
    decision: Any,
    current: dict[str, Any],
    *,
    blocked: bool,
    consumed: bool,
) -> dict[str, Any]:
    """Replace only the old executor's policy-timed WAIT side effect."""

    del bridge
    return {
        "kind": "SOURCE_WAIT_DECISION",
        "source_decision": WAIT_ACTION,
        "requested_wait_ms": decision.wait_ms,
        "external_press_disposition": "ABSTAINED_NO_EXTRA_KEY",
        "simulator_submission": {"status": "NOT_SUBMITTED_EXTERNAL_PRESS_CLOCK"},
        "simulator_acceptance": {"status": "NOT_APPLICABLE"},
        "decision_consumption": {
            "status": "PHYSICAL_KEY_CLOSED_BY_FINISH_PRESS",
            "consumes_decision": False,
        },
        "simulator_outcome": {"status": "NO_POLICY_WAIT_SCHEDULED"},
        "_state": current,
        "_reason": None,
        "_blocked": blocked,
    }


def run_cat_external_press_pilot_v1(
    bridge: Any,
    request: Mapping[str, Any],
    target_contexts: Mapping[int, Any],
    *,
    seed: int,
    period_ms: int,
    policy_kind: str,
    max_presses: int = 200,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
) -> dict[str, Any]:
    """Run one isolated static singleton; neither lane is a voting baseline."""

    if policy_kind not in POLICY_KINDS:
        raise ValueError("policy_kind must be cat or zero_residual")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")
    if len(request.get("encounter", {}).get("targets", [])) != 1 or set(target_contexts) != {0}:
        raise ValueError("press pilot requires one static target and its context")
    duration = request["encounter"].get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("press pilot requires a positive encounter duration")
    duration_ms = round(float(duration) * 1000)
    inputs = simulator_inputs or CatFurySimulatorInputsV5()
    if not isinstance(inputs, CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5")
    adapter = (
        CatFuryFullPolicyAdapterV4() if policy_kind == "cat"
        else CatResidualCandidateV1(CatQueueResidualV1(0.0))
    )
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
    status = "UNSUPPORTED_PRESS_PILOT_NONVOTING"
    terminal_kind = "UNSUPPORTED"
    reason: str | None = None
    state: Mapping[str, Any] | None = None
    last_gcd_action = ""
    try:
        state = bridge.load(request, seed)
        state = bridge.configure_press_clock(period_ms)
        while True:
            if state["finished"]:
                known = state.get("target_health_known") is True
                health = state.get("target_health")
                defeated = known and isinstance(health, (int, float)) and not isinstance(health, bool) and health <= 0
                if defeated:
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
            available = bridge.actions()
            target = _resolve_target_semantics(before, request, target_contexts)
            combat = mapper(
                before, available, request, target,
                last_gcd_action=last_gcd_action,
            )
            proposal = validate_source_decision_v4(adapter.propose(combat))
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
            closed = bridge.finish_press()
            presses.append({
                "press_index": clock.press_index,
                "time_ms": before["time_ms"],
                "source_invocation_count": 1,
                "proposal": proposal.to_dict(),
                "ordered_execution": execution,
                "source_wait_abstained": proposal.gcd == WAIT_ACTION,
                "finish_press_time_ms": closed["time_ms"],
                "finish_press_ready": _press_clock_state_v1(closed).ready,
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
            "required_hostiles_defeated": (
                state.get("target_health") <= 0
                if isinstance(state, Mapping)
                and state.get("target_health_known") is True
                and isinstance(state.get("target_health"), (int, float))
                and not isinstance(state.get("target_health"), bool)
                else None
            ),
            "target_health_known": (
                state.get("target_health_known")
                if isinstance(state, Mapping) else None
            ),
            "target_health": (
                state.get("target_health")
                if isinstance(state, Mapping) and state.get("target_health_known") is True
                else None
            ),
            "target_health_max": (
                state.get("target_health_max")
                if isinstance(state, Mapping) and state.get("target_health_known") is True
                else None
            ),
            "duration_ms": duration_ms,
            "watchdog_truncated": terminal_kind == "WATCHDOG_TRUNCATED",
            "reason": reason,
            "time_ms": state.get("time_ms") if isinstance(state, Mapping) else None,
        },
        "policy_kind": policy_kind,
        "seed": seed,
        "period_ms": period_ms,
        "press_count": len(presses),
        "presses": presses,
        "comparison_ready": False,
        "voting_eligible": False,
        "live_fidelity": False,
    }
