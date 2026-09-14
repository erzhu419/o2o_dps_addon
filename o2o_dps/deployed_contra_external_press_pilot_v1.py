"""Development-only deployed Contra Raid-B on simulator-owned physical keys.

The installed runtime binding and source-order sink executor remain authoritative.
Source WAIT and declared no-op retry waits abstain on this key: neither schedules
an extra decision nor changes the simulator's external press clock.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .cat_external_press_pilot_v1 import (
    _close_no_live_target_press,
    _load_dynamic_press_clock_v1,
    _no_live_target_press_reason,
    _normalize_shared_context_receipt,
    _required_targets_dead,
)
from .deployed_contra_runtime_binding_v1 import validate_deployed_contra_runtime_binding_v1
from .expert_policy import WAIT_ACTION
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    validate_dynamic_load_request_v3,
)
from .fury_full_policy_rollout_v2 import (
    _RESULT_BEARING_GCD_ACTIONS,
    _combat_state,
    _proposal,
)
from .fury_full_policy_rollout_v4 import _resolve_target_semantics_v4
from .fury_ordered_sink_executor_raid_b_v1 import execute_ordered_sinks_raid_b_v1
from .fury_runtime_bound_deployed_contra_raid_b_v1 import RuntimeBoundContraRaidBAdapterV1
from .sim_bridge_dynamic_v3 import _press_clock_state_v1


SCHEMA = "deployed_contra_external_press_pilot/v1"
POLICY_KIND = "deployed_contra_raid_b"


class _ExternalKeyBridge:
    """Intercept only legacy policy-timed waits after ordered source sinks."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self.intercepted_waits: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bridge, name)

    def wait(self, wait_ms: int) -> dict[str, Any]:
        self.intercepted_waits.append(wait_ms)
        # The legacy executor expects a consumed-looking receipt.  It is only
        # local to that invocation; the simulator remains on the current key.
        state = dict(self.bridge.state())
        state["needs_input"] = False
        return state


def _externalize_wait(execution: dict[str, Any], bridge: _ExternalKeyBridge) -> None:
    if not bridge.intercepted_waits:
        return
    if len(bridge.intercepted_waits) != 1:
        raise RuntimeError("one Contra source invocation scheduled multiple waits")
    wait = execution.get("wait_event")
    if not isinstance(wait, dict) or wait.get("requested_wait_ms") != bridge.intercepted_waits[0]:
        raise RuntimeError("Contra source wait receipt differs from intercepted wait")
    wait["external_press_disposition"] = "ABSTAINED_NO_EXTRA_KEY"
    wait["simulator_submission"] = {"status": "NOT_SUBMITTED_EXTERNAL_PRESS_CLOCK"}
    wait["client_acceptance"] = {"status": "NOT_APPLICABLE"}
    wait["decision_consumption"] = {
        "status": "PHYSICAL_KEY_CLOSED_BY_FINISH_PRESS", "consumes_decision": False,
    }
    wait["server_result"] = {"status": "NO_POLICY_WAIT_SCHEDULED"}
    wait.pop("simulator_state_after_immediate", None)
    clock = execution.get("source_reentry_clock")
    if isinstance(clock, Mapping):
        execution["external_press_reentry"] = {
            "trigger": clock.get("trigger"),
            "source_requested_ms_ignored": clock.get("requested_ms"),
            "timing_authority": "SIMULATOR_EXTERNAL_PRESS_CLOCK",
        }
        execution["source_reentry_clock"] = None
    execution["decision_consumed"] = False
    execution["final_state"] = dict(bridge.state())


def run_deployed_contra_external_press_pilot_v1(
    bridge: Any,
    request: Mapping[str, Any],
    target_contexts: Mapping[int, Any],
    *,
    runtime_binding: Mapping[str, Any],
    seed: int,
    period_ms: int,
    dynamic_load: DynamicRolloutLoadV3,
    max_presses: int = 200,
    phase_ms: int = 0,
    shared_context_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one Raid-B source invocation per physical key over a dynamic-v3 wave."""

    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")
    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    targets = request.get("encounter", {}).get("targets", [])
    target_count = len(targets)
    if target_count < 1 or set(target_contexts) != set(range(target_count)):
        raise ValueError("Raid-B press pilot requires a context for every wave target")
    validate_dynamic_load_request_v3(dynamic_load, request)
    if dynamic_load.seed != seed:
        raise ValueError("dynamic load seed differs from press pilot seed")
    duration = request["encounter"].get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise ValueError("press pilot requires a positive encounter duration")
    duration_ms = round(float(duration) * 1000)
    binding = validate_deployed_contra_runtime_binding_v1(runtime_binding)
    context_receipt = _normalize_shared_context_receipt(shared_context_receipt)
    adapter = RuntimeBoundContraRaidBAdapterV1(binding)
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
                presses.append(press)
                continue
            available = bridge.actions()
            target = _resolve_target_semantics_v4(before, request, target_contexts)
            combat = _combat_state(
                before, available, request, target, last_gcd_action=last_gcd_action,
            )
            proposal = _proposal(adapter, combat, target)
            if proposal.valid:
                facade = _ExternalKeyBridge(bridge)
                execution = execute_ordered_sinks_raid_b_v1(
                    facade, proposal, before,
                    attempt_id_prefix=f"press-{clock.press_index}",
                    result_bearing_action_keys=tuple(sorted(_RESULT_BEARING_GCD_ACTIONS)),
                )
                _externalize_wait(execution, facade)
                accepted = execution.get("accepted_gcd_actions") or []
                if accepted:
                    last_gcd_action = accepted[-1]
            else:
                execution = None
            after_sinks = bridge.state()
            # A lethal sink can finish the model during this physical key.  The
            # native bridge then retires the press opportunity itself.
            terminal_on_key = after_sinks["finished"] is True
            closed = after_sinks if terminal_on_key else bridge.finish_press()
            presses.append({
                "press_index": clock.press_index,
                "time_ms": before["time_ms"],
                "target_index": before.get("target_index"),
                "source_invocation_count": 1,
                "proposal": proposal.to_dict(),
                "ordered_execution": execution,
                "source_wait_abstained": bool(
                    proposal.gcd == WAIT_ACTION or
                    (execution and execution.get("external_press_reentry"))
                ),
                "policy_disposition": "SOURCE_INVOKED",
                "finish_press_time_ms": closed["time_ms"],
                "finish_press_ready": (
                    None if terminal_on_key else _press_clock_state_v1(closed).ready
                ),
                "press_closure": (
                    "SKIPPED_MODEL_TERMINAL" if terminal_on_key else "FINISH_PRESS"
                ),
                "finish_press_disposition": (
                    "MODEL_TERMINAL_DURING_KEY" if terminal_on_key else "CLOSED_BY_FINISH_PRESS"
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
            "duration_ms": duration_ms,
            "watchdog_truncated": terminal_kind == "WATCHDOG_TRUNCATED",
            "reason": reason,
            "time_ms": state.get("time_ms") if isinstance(state, Mapping) else None,
        },
        "policy_kind": POLICY_KIND,
        "runtime_binding_sha256": binding["binding_sha256"],
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
        "simulator_input_receipt": {
            "source": "BRIDGE_STATE_PLUS_RUNTIME_BINDING",
            "runtime_binding_sha256": binding["binding_sha256"],
        },
        "shared_context_receipt": context_receipt,
        "shared_context_bound": context_receipt is not None,
        "comparison_ready": False,
        "voting_eligible": False,
        "live_fidelity": False,
    }


__all__ = ("POLICY_KIND", "SCHEMA", "run_deployed_contra_external_press_pilot_v1")
