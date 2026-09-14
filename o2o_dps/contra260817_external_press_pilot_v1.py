"""Development-only Contra260817 source on simulator-owned physical key ticks.

The source is invoked once per ready press. Its raw sinks execute in source
order, but neither source WAIT nor legacy 100 ms reentry can schedule a key.
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
from .contra260817_fury_full_policy_v3 import (
    SOURCE_DEFAULT_PROFILE_V3,
    Contra260817FuryFullPolicyAdapterV3,
    validate_source_decision_v3,
)
from .contra260817_fury_full_policy_rollout_v4 import (
    Contra260817SimulatorInputsV4,
    _RESULT_BEARING_GCD_ACTIONS_V4,
    _contra_state_mapper,
)
from .contra260817_fury_ordered_sink_executor_v4 import (
    Contra260817SimulatorControlFacadeV4,
    execute_contra260817_ordered_sinks_v4,
)
from .expert_policy import WAIT_ACTION
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    validate_dynamic_load_request_v3,
)
from .fury_full_policy_rollout_v4 import _resolve_target_semantics_v4
from .sim_bridge_dynamic_v3 import _press_clock_state_v1


SCHEMA = "contra260817_external_press_pilot/v1"


def _simulator_input_receipt_v1(
    inputs: Contra260817SimulatorInputsV4,
) -> dict[str, Any]:
    """Include the policy-side source reads omitted by the legacy receipt."""

    receipt = inputs.receipt()
    receipt.update({
        "current_target_in_front": inputs.current_target_in_front,
        "switch_throttle_open": inputs.switch_throttle_open,
        "current_target_friendly": inputs.current_target_friendly,
        "current_target_is_player": inputs.current_target_is_player,
        "target_banished_indices": list(inputs.target_banished_indices),
        "attack_actionbar_present": inputs.attack_actionbar_present,
        "start_attack_banish_branch_active": inputs.start_attack_banish_branch_active,
        "heroic_strike_probe_texture_present": inputs.heroic_strike_probe_texture_present,
        "cleave_probe_texture_present": inputs.cleave_probe_texture_present,
        "target_affecting_combat": inputs.target_affecting_combat,
        "target_casting_spell": inputs.target_casting_spell,
        "target_of_target_is_player": inputs.target_of_target_is_player,
        "offhand_is_shield_after_prelude": inputs.offhand_is_shield_after_prelude,
        "five_yard_guid_candidate": inputs.five_yard_guid_candidate,
        "five_yard_guid_is_current": inputs.five_yard_guid_is_current,
        "previous_target_guid": inputs.previous_target_guid,
        "nearest_enemy_changed_target": inputs.nearest_enemy_changed_target,
        "nearest_enemy_target_within_five_yards": (
            inputs.nearest_enemy_target_within_five_yards
        ),
    })
    return receipt
POLICY_KIND = "contra_new"


def run_contra260817_external_press_pilot_v1(
    bridge: Any,
    request: Mapping[str, Any],
    target_contexts: Mapping[int, Any],
    *,
    seed: int,
    period_ms: int,
    dynamic_load: DynamicRolloutLoadV3,
    max_presses: int = 200,
    phase_ms: int = 0,
    simulator_inputs: Contra260817SimulatorInputsV4 | None = None,
    adapter: Contra260817FuryFullPolicyAdapterV3 | None = None,
    shared_context_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the source-default Contra_new lane on a dynamic-v3 whole wave."""

    if not isinstance(dynamic_load, DynamicRolloutLoadV3):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV3")
    validate_dynamic_load_request_v3(dynamic_load, request)
    if dynamic_load.seed != seed:
        raise ValueError("dynamic load seed differs from press pilot seed")
    if type(max_presses) is not int or max_presses < 1:
        raise ValueError("max_presses must be positive")
    if type(period_ms) is not int or period_ms < 1:
        raise ValueError("period_ms must be positive")
    if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
        raise ValueError("phase_ms must be in [0, period_ms)")
    target_count = len(request.get("encounter", {}).get("targets", []))
    if target_count < 1 or set(target_contexts) != set(range(target_count)):
        raise ValueError("press pilot requires a context for every dynamic target")
    duration = request["encounter"].get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise ValueError("press pilot requires a positive encounter duration")
    duration_ms = round(float(duration) * 1000)
    inputs = simulator_inputs or Contra260817SimulatorInputsV4()
    if not isinstance(inputs, Contra260817SimulatorInputsV4):
        raise TypeError("simulator_inputs must be Contra260817SimulatorInputsV4")
    context_receipt = _normalize_shared_context_receipt(shared_context_receipt)
    source = adapter or Contra260817FuryFullPolicyAdapterV3()
    if type(source) is not Contra260817FuryFullPolicyAdapterV3:
        raise TypeError("adapter must be exact Contra260817FuryFullPolicyAdapterV3")
    if source.profile != SOURCE_DEFAULT_PROFILE_V3:
        raise ValueError("press pilot accepts only the source-default Contra profile")
    controls = Contra260817SimulatorControlFacadeV4(
        bridge,
        target_bindings=inputs.target_bindings,
        item_bindings=inputs.item_action_bindings,
        initial_autoattack_active=inputs.initial_autoattack_active,
        initial_equipment={
            16: inputs.equipped_mainhand_name,
            17: inputs.equipped_offhand_name,
        },
    )
    mapper = _contra_state_mapper(controls, inputs, dynamic_load.contract_sha256)
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
        controls.reset_receipts()
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
            proposal = None
            execution = None
            invocation_count = 0
            press_error: str | None = None
            press_closure = "FINISH_PRESS"
            try:
                available = bridge.actions()
                target = _resolve_target_semantics_v4(before, request, target_contexts)
                combat = mapper(
                    before, available, request, target,
                    last_gcd_action=last_gcd_action,
                )
                invocation_count = 1
                proposal = validate_source_decision_v3(source.propose(combat))
                if proposal.valid:
                    execution = execute_contra260817_ordered_sinks_v4(
                        controls, proposal, before,
                        attempt_id_prefix=f"press-{clock.press_index}",
                        result_bearing_action_keys=tuple(sorted(_RESULT_BEARING_GCD_ACTIONS_V4)),
                        external_press_clock=True,
                    )
                    accepted = execution.get("accepted_gcd_actions") or []
                    if accepted:
                        last_gcd_action = accepted[-1]
                else:
                    press_error = "source proposal invalid at a scheduled press"
            except Exception as error:
                press_error = f"{type(error).__name__}: {error}"
            finally:
                after_sinks = execution.get("final_state") if isinstance(execution, Mapping) else None
                if isinstance(after_sinks, Mapping) and after_sinks.get("finished") is True:
                    # A killing sink terminates the Go load and closes this key itself.
                    state = dict(after_sinks)
                    press_closure = "SKIPPED_MODEL_TERMINAL"
                else:
                    state = bridge.finish_press()
            presses.append({
                "press_index": clock.press_index,
                "time_ms": before["time_ms"],
                "target_index": before.get("target_index"),
                "source_invocation_count": invocation_count,
                "proposal": proposal.to_dict() if proposal is not None else None,
                "ordered_execution": execution,
                "source_wait_abstained": proposal.gcd == WAIT_ACTION if proposal is not None else None,
                "policy_disposition": "SOURCE_INVOKED",
                "finish_press_time_ms": state["time_ms"],
                "finish_press_ready": (
                    _press_clock_state_v1(state).ready
                    if press_closure == "FINISH_PRESS" else None
                ),
                "press_closure": press_closure,
                "reason": press_error,
            })
            if press_error is not None:
                reason = press_error
                break
            if execution is not None and (
                execution["execution_blocked"] or execution["nonfaithful_reasons"]
            ):
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
        "policy_kind": POLICY_KIND,
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
        "simulator_input_receipt": _simulator_input_receipt_v1(inputs),
        "shared_context_receipt": context_receipt,
        "shared_context_bound": context_receipt is not None,
        "source_profile": "SOURCE_DEFAULT_NOT_PLAYER_CONTRADB",
        "comparison_ready": False,
        "voting_eligible": False,
        "live_fidelity": False,
    }
