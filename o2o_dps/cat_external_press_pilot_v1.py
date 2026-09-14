"""Development-only Cat source invocation on a simulator-owned key clock.

This pilot deliberately does not reuse the legacy full-rollout scheduler: Cat's
source WAIT is an abstention on the current physical key, not a request for a
new simulator decision after ``wait_ms``.
"""

from __future__ import annotations

import math
from copy import deepcopy
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
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    validate_dynamic_load_request_v3,
)
from .fury_full_policy_rollout_v2 import _resolve_target_semantics
from .fury_full_policy_rollout_v4 import _resolve_target_semantics_v4
from .sim_bridge import SimBridgeCommandError
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


def _required_targets_dead(
    state: Mapping[str, Any] | None, *, dynamic: bool, target_count: int,
) -> bool | None:
    if state is None:
        return None
    if dynamic:
        background = state.get("dynamic_team_background")
        rows = background.get("targets") if isinstance(background, Mapping) else None
        if not isinstance(rows, list) or len(rows) != target_count:
            return None
        if any(not isinstance(row, Mapping) or row.get("target_index") != index
               or type(row.get("dead")) is not bool
               for index, row in enumerate(rows)):
            return None
        return all(row["dead"] for row in rows)
    health = state.get("target_health")
    return (
        health <= 0
        if state.get("target_health_known") is True
        and isinstance(health, (int, float)) and not isinstance(health, bool)
        else None
    )


def _normalize_shared_context_receipt(
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if receipt is None:
        return None
    if not isinstance(receipt, Mapping):
        raise TypeError("shared_context_receipt must be a mapping or None")
    return deepcopy(dict(receipt))


def _no_live_target_press_reason(
    state: Mapping[str, Any], *, target_count: int,
) -> str | None:
    """Recognize a typed dynamic tick where no current target can be acted on."""

    semantics = state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list) or len(rows) != target_count:
        return None
    if any(
        not isinstance(row, Mapping)
        or row.get("target_index") != index
        or type(row.get("dead")) is not bool
        or type(row.get("attackable")) is not bool
        for index, row in enumerate(rows)
    ):
        return None
    live = [
        index for index, row in enumerate(rows)
        if row["dead"] is False and row["attackable"] is True
    ]
    raw_index = state.get("target_index")
    if not live:
        return "NO_LIVE_ATTACKABLE_TARGET"
    if (
        isinstance(raw_index, bool)
        or not isinstance(raw_index, int)
        or raw_index not in live
    ):
        return "NO_LIVE_ATTACKABLE_CURRENT_TARGET"
    return None


def _close_no_live_target_press(
    bridge: Any,
    clock: Any,
    before: Mapping[str, Any],
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    closed = bridge.finish_press()
    return ({
        "press_index": clock.press_index,
        "time_ms": before["time_ms"],
        "target_index": before.get("target_index"),
        "source_invocation_count": 0,
        "proposal": None,
        "ordered_execution": None,
        "source_wait_abstained": None,
        "policy_disposition": "NO_LIVE_TARGET_ENVIRONMENT_NOOP",
        "no_live_target_reason": reason,
        "finish_press_time_ms": closed["time_ms"],
        "finish_press_ready": _press_clock_state_v1(closed).ready,
        "press_closure": "FINISH_PRESS",
    }, dict(closed))


def _first_scheduled_press_ms(
    configured_at_ms: int, *, period_ms: int, phase_ms: int,
) -> int:
    if type(configured_at_ms) is not int or configured_at_ms < 0:
        raise ValueError("press clock configuration time must be nonnegative")
    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(phase_ms) is not int or phase_ms < 0 or phase_ms >= period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    if phase_ms >= configured_at_ms:
        return phase_ms
    return phase_ms + (
        (configured_at_ms - phase_ms + period_ms - 1) // period_ms
    ) * period_ms


def _configured_first_press_receipt(
    state: Mapping[str, Any], *, configured_at_ms: int,
    period_ms: int, phase_ms: int,
) -> int:
    """Bind the derived first tick to the simulator's configure receipt."""

    expected = _first_scheduled_press_ms(
        configured_at_ms, period_ms=period_ms, phase_ms=phase_ms,
    )
    clock = _press_clock_state_v1(state)
    if (
        clock.period_ms != period_ms
        or clock.phase_ms != phase_ms
        or clock.press_index != 0
        or clock.ready
        or clock.next_time_ms != expected
    ):
        raise RuntimeError("configured press clock differs from requested grid")
    return clock.next_time_ms


def _load_dynamic_press_clock_v1(
    bridge: Any,
    request: Mapping[str, Any],
    seed: int,
    config: Any,
    *,
    period_ms: int,
    phase_ms: int,
) -> tuple[dict[str, Any], int, int, str]:
    state, configured_at, first_press, mode, _ = (
        _load_dynamic_press_clock_with_receipt_v1(
            bridge, request, seed, config,
            period_ms=period_ms, phase_ms=phase_ms,
        )
    )
    return state, configured_at, first_press, mode


def _load_dynamic_press_clock_with_receipt_v1(
    bridge: Any,
    request: Mapping[str, Any],
    seed: int,
    config: Any,
    *,
    period_ms: int,
    phase_ms: int,
) -> tuple[dict[str, Any], int, int, str, Any]:
    """Prefer the v19 atomic load; retain old bridges as an explicit fallback."""

    atomic_owner = getattr(type(bridge), "load_dynamic_v3_press_clock", None)
    atomic_loader = getattr(bridge, "load_dynamic_v3_press_clock", None)
    if callable(atomic_owner) and callable(atomic_loader):
        try:
            loaded = atomic_loader(
                request, seed, config, period_ms=period_ms, phase_ms=phase_ms,
            )
        except SimBridgeCommandError as error:
            message = str(error)
            if "unknown command" not in message or "load_dynamic_v3_press_clock" not in message:
                raise
        else:
            state = dict(loaded.state)
            clock = _press_clock_state_v1(state)
            return (
                state, 0, clock.next_time_ms,
                "ATOMIC_DYNAMIC_V3_PRESS_CLOCK", loaded,
            )

    loaded = bridge.load_dynamic_v3(request, seed, config)
    configured_at_ms = loaded.state.get("time_ms")
    state = bridge.configure_press_clock(period_ms, phase_ms)
    first_scheduled_press_ms = _configured_first_press_receipt(
        state, configured_at_ms=configured_at_ms,
        period_ms=period_ms, phase_ms=phase_ms,
    )
    return (
        dict(state), configured_at_ms, first_scheduled_press_ms,
        "SEPARATE_AFTER_DYNAMIC_LOAD", loaded,
    )


def run_cat_external_press_pilot_v1(
    bridge: Any,
    request: Mapping[str, Any],
    target_contexts: Mapping[int, Any],
    *,
    seed: int,
    period_ms: int,
    policy_kind: str,
    max_presses: int = 200,
    phase_ms: int = 0,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    dynamic_load: DynamicRolloutLoadV3 | None = None,
    shared_context_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Cat or exact zero residual on static or dynamic-v3 model key ticks."""

    if policy_kind not in POLICY_KINDS:
        raise ValueError("policy_kind must be cat or zero_residual")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")
    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    target_count = len(request.get("encounter", {}).get("targets", []))
    if dynamic_load is None:
        if target_count != 1 or set(target_contexts) != {0}:
            raise ValueError("static press pilot requires one target and its context")
    else:
        validate_dynamic_load_request_v3(dynamic_load, request)
        if dynamic_load.seed != seed:
            raise ValueError("dynamic load seed differs from press pilot seed")
        if target_count < 1 or set(target_contexts) != set(range(target_count)):
            raise ValueError("dynamic press pilot requires a context for every target")
    duration = request["encounter"].get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("press pilot requires a positive encounter duration")
    duration_ms = round(float(duration) * 1000)
    inputs = simulator_inputs or CatFurySimulatorInputsV5()
    if not isinstance(inputs, CatFurySimulatorInputsV5):
        raise TypeError("simulator_inputs must be CatFurySimulatorInputsV5")
    context_receipt = _normalize_shared_context_receipt(shared_context_receipt)
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
    configured_at_ms: int | None = None
    first_scheduled_press_ms: int | None = None
    press_clock_configuration_mode: str | None = None
    try:
        if dynamic_load is None:
            state = bridge.load(request, seed)
            configured_at_ms = state.get("time_ms")
            state = bridge.configure_press_clock(period_ms, phase_ms)
            first_scheduled_press_ms = _configured_first_press_receipt(
                state, configured_at_ms=configured_at_ms,
                period_ms=period_ms, phase_ms=phase_ms,
            )
            press_clock_configuration_mode = "SEPARATE_AFTER_STATIC_LOAD"
        else:
            (
                state, configured_at_ms, first_scheduled_press_ms,
                press_clock_configuration_mode,
            ) = _load_dynamic_press_clock_v1(
                bridge, request, seed, dynamic_load.config,
                period_ms=period_ms, phase_ms=phase_ms,
            )
        while True:
            if state["finished"]:
                if _required_targets_dead(
                    state, dynamic=dynamic_load is not None, target_count=target_count,
                ) is True:
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
            no_target_reason = (
                _no_live_target_press_reason(before, target_count=target_count)
                if dynamic_load is not None else None
            )
            if no_target_reason is not None:
                press, state = _close_no_live_target_press(
                    bridge, clock, before, no_target_reason,
                )
                presses.append(press)
                continue
            available = bridge.actions()
            target = (
                _resolve_target_semantics_v4(before, request, target_contexts)
                if dynamic_load is not None else
                _resolve_target_semantics(before, request, target_contexts)
            )
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
            terminal_after_sink = (
                execution.get("final_state") if isinstance(execution, Mapping) else None
            )
            if isinstance(terminal_after_sink, Mapping) and terminal_after_sink.get("finished") is True:
                # A terminal sink makes finish_press illegal. The terminal
                # state may still echo ready=true, so do not report a false
                # normal closure receipt.
                closed = dict(terminal_after_sink)
                press_closure = "SKIPPED_MODEL_TERMINAL"
                finish_press_ready = None
            else:
                closed = bridge.finish_press()
                press_closure = "FINISH_PRESS"
                finish_press_ready = _press_clock_state_v1(closed).ready
            presses.append({
                "press_index": clock.press_index,
                "time_ms": before["time_ms"],
                "target_index": before.get("target_index"),
                "source_invocation_count": 1,
                "proposal": proposal.to_dict(),
                "ordered_execution": execution,
                "source_wait_abstained": proposal.gcd == WAIT_ACTION,
                "policy_disposition": "SOURCE_INVOKED",
                "finish_press_time_ms": closed["time_ms"],
                "finish_press_ready": finish_press_ready,
                "press_closure": press_closure,
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
                dynamic=dynamic_load is not None, target_count=target_count,
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
        "mode": "DYNAMIC_V3_WHOLE_WAVE" if dynamic_load is not None else "STATIC_SINGLE_TARGET",
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
