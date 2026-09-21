"""Development-only causal action-program replay on a responsive v4 bridge.

The bridge factory owns a fresh source-bound worker (and its model connection)
for each replay.  Imported bindings are opened afresh as well.  This module
does not translate Cat/Contra policies or confer comparison authority: callers
must supply their exact source resolver and a prefix-only observation projector.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramError,
    CausalActionProgramV1,
    CausalObservationProjectorV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveQueueGcdBlockSelectorV1,
    ImportedReactiveSelectorV1,
    _ImportedReactiveProgramSessionV1,
    _action,
    _available_by_action,
    _execute_decision,
    _project_observation,
    _select_decision,
    _state_damage,
    _terminal_telemetry_receipt_v1,
)
from .upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)
from .wave_action_sequence_search_v1 import (
    FURY_RESULT_BEARING_ACTION_REFS_V1,
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    _WaveTargetGateTrackerV1,
    _enforce_unmasked_collateral_action_v1,
)
from .sim_bridge import ActionRef
from .upper_kara_wave_target_gate_v1 import RuntimeTargetGateV1


def _advance_to_clean_input(bridge: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """A ready teammate wake outranks even a simultaneous candidate input."""

    current = dict(state)
    count = 0
    while not bool(current.get("finished")) and (
        not bool(current.get("needs_input"))
        or current.get("wake_ready") is not None
    ):
        current = dict(bridge.advance())
        count += 1
        if count > 100_000:
            raise CausalActionProgramError(
                "responsive advance loop exceeded 100000 transitions"
            )
    return current


def _required_binding_id(program: CausalActionProgramV1) -> str | None:
    selector = program.selector
    if isinstance(selector, ImportedReactiveSelectorV1):
        return selector.binding_id
    if isinstance(selector, (
        ImportedFallbackOverlaySelectorV1,
        ImportedReactiveQueueGcdBlockSelectorV1,
        ImportedReactiveBurstQueueGcdBlockSelectorV1,
    )):
        return selector.imported_fallback.binding_id
    return None


class NativeDynamicV4ResponsiveActionProgramReplayV1:
    """Run a current-state-only program through a fresh driven bridge.

    ``bridge_factory`` returns a context manager yielding a
    ``ResponsiveTeamDrivenBridgeV1`` already bound to the same case prefix.
    Its ``load_dynamic_v4`` arms teammate wakes, while its ``advance`` drains
    each ready wake before the next candidate decision.  The caller owns
    constructing honest prefix registries and the model/source binding.
    """

    def __init__(
        self,
        *,
        bridge_factory: Callable[[], Any],
        case_factory: Callable[[int], CompiledResponsiveIncantagosCaseV1],
        observation_projector_factory: Callable[
            [CompiledResponsiveIncantagosCaseV1], CausalObservationProjectorV1
        ],
        imported_bindings: Sequence[ImportedReactiveProgramBindingV1] = (),
        result_bearing_action_refs: Sequence[ActionRef] = tuple(
            FURY_RESULT_BEARING_ACTION_REFS_V1
        ),
        terminal_telemetry_action_refs: Sequence[ActionRef] = (),
        target_gate: RuntimeTargetGateV1 | None = None,
    ) -> None:
        if not all(callable(value) for value in (
            bridge_factory, case_factory, observation_projector_factory
        )):
            raise TypeError("bridge, case, and projector factories must be callable")
        if any(not isinstance(row, ImportedReactiveProgramBindingV1) for row in imported_bindings):
            raise TypeError("imported_bindings must contain reactive bindings")
        ids = [row.binding_id for row in imported_bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("imported binding IDs must be unique")
        self._bridge_factory = bridge_factory
        self._case_factory = case_factory
        self._projector_factory = observation_projector_factory
        self._bindings = tuple(imported_bindings)
        self._result_refs = frozenset(result_bearing_action_refs)
        telemetry_refs = tuple(terminal_telemetry_action_refs)
        for index, action in enumerate(telemetry_refs):
            _action(action, f"terminal_telemetry_action_refs[{index}]")
        if len(telemetry_refs) != len(set(telemetry_refs)):
            raise ValueError("terminal_telemetry_action_refs must be unique")
        self._terminal_telemetry_action_refs = frozenset(telemetry_refs)
        if target_gate is not None and not isinstance(
            target_gate, RuntimeTargetGateV1
        ):
            raise TypeError("target_gate must implement RuntimeTargetGateV1")
        self._target_gate = target_gate

    def replay(
        self,
        seed: int,
        program: CausalActionProgramV1,
        *,
        max_decisions: int = 10_000,
        stop_at_or_after_ms: int | None = None,
    ) -> ScheduleReplayOutcomeV1:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        if not isinstance(program, CausalActionProgramV1):
            raise TypeError("program must be CausalActionProgramV1")
        if isinstance(max_decisions, bool) or not isinstance(max_decisions, int) or max_decisions < 1:
            raise ValueError("max_decisions must be a positive integer")
        if stop_at_or_after_ms is not None and (
            isinstance(stop_at_or_after_ms, bool)
            or not isinstance(stop_at_or_after_ms, int)
            or stop_at_or_after_ms < 0
        ):
            raise ValueError("stop_at_or_after_ms must be a nonnegative integer")
        receipts: list[dict[str, Any]] = []
        last_state: dict[str, Any] = {"time_ms": 0, "damage_done": 0.0}
        try:
            case = self._case_factory(seed)
            if not isinstance(case, CompiledResponsiveIncantagosCaseV1):
                raise TypeError("case_factory must return CompiledResponsiveIncantagosCaseV1")
            projector = self._projector_factory(case)
            if not callable(projector):
                raise TypeError("observation_projector_factory must return a callable")
            tracker = (
                _WaveTargetGateTrackerV1(self._target_gate)
                if self._target_gate is not None
                else None
            )
            if self._target_gate is not None:
                self._target_gate.validate_case(case)
            required_id = _required_binding_id(program)
            sessions = {
                row.binding_id: _ImportedReactiveProgramSessionV1(
                    binding_id=row.binding_id,
                    source_policy_id=row.source_policy_id,
                    observation_contract_id=row.observation_contract_id,
                    resolver=row.open_session(),
                )
                for row in self._bindings if row.binding_id == required_id
            }
            if required_id is not None and required_id not in sessions:
                raise CausalActionProgramError(
                    f"missing imported source binding {required_id!r}"
                )
            with self._bridge_factory() as bridge:
                loaded = bridge.load_dynamic_v4(case.request, seed, case.dynamic_config)
                receipts.append({
                    "kind": "DEVELOPMENT_RESPONSIVE_V4_BINDING",
                    "simulator_seed": seed,
                    "dynamic_config_sha256": case.dynamic_config.content_sha256,
                    "comparison_authorized": False,
                })
                state = _advance_to_clean_input(bridge, dict(loaded.state))
                last_state = dict(state)
                if tracker is not None:
                    tracker.observe(state)
                    receipts.append({
                        "kind": "DEVELOPMENT_RUNTIME_TARGET_GATE_BOUND",
                        "target_gate": tracker.decision.to_dict(),
                        "comparison_authorized": False,
                    })
                decision_index = 0
                while not bool(state.get("finished")):
                    if (
                        stop_at_or_after_ms is not None
                        and state["time_ms"] >= stop_at_or_after_ms
                    ):
                        available, _ = _available_by_action(bridge)
                        receipts.append({
                            "kind": "DEVELOPMENT_RESPONSIVE_V4_BOUNDED_FRONTIER",
                            "requested_stop_ms": stop_at_or_after_ms,
                            "actual_stop_ms": state["time_ms"],
                            "decision_count": decision_index,
                            "responsive_event_count": getattr(
                                bridge, "responsive_event_count", None
                            ),
                            "responsive_applied_damage": getattr(
                                bridge, "responsive_applied_damage", None
                            ),
                            "comparison_authorized": False,
                        })
                        return ScheduleReplayOutcomeV1(
                            seed=seed, status=ReplayStatusV1.FRONTIER,
                            state=state, available_actions=available,
                            receipts=tuple(receipts),
                            target_gate=(
                                tracker.decision if tracker is not None else None
                            ),
                        )
                    if decision_index >= max_decisions:
                        raise CausalActionProgramError(
                            f"program exceeded max_decisions={max_decisions}"
                        )
                    if not bool(state.get("needs_input")):
                        state = _advance_to_clean_input(bridge, state)
                        last_state = dict(state)
                        continue
                    available, _ = _available_by_action(bridge)
                    observation = _project_observation(projector, state, available)

                    def validate_target(
                        current: Mapping[str, Any], simulator_index: int
                    ) -> Mapping[str, Any]:
                        assert tracker is not None
                        gate = tracker.observe(current)
                        if simulator_index not in gate.direct_target_indexes:
                            raise CausalActionProgramError(
                                "program target is outside the current "
                                "direct-target allowlist"
                            )
                        return gate.to_dict()

                    def validate_action(
                        current: Mapping[str, Any], action: ActionRef
                    ) -> Mapping[str, Any] | None:
                        assert tracker is not None
                        return _enforce_unmasked_collateral_action_v1(
                            action, current, tracker
                        )
                    try:
                        decision = _select_decision(
                            program, observation, available, sessions, receipts,
                            decision_index,
                        )
                        execution_receipt_start = len(receipts)
                        state = _execute_decision(
                            bridge, state, decision, decision_index=decision_index,
                            projector=projector, receipts=receipts,
                            result_bearing_action_refs=self._result_refs,
                            runtime_target_validator=(
                                validate_target if tracker is not None else None
                            ),
                            runtime_action_validator=(
                                validate_action if tracker is not None else None
                            ),
                        )
                    except Exception as error:
                        for session in sessions.values():
                            session.reject_pending_execution(
                                f"{type(error).__name__}: {error}"
                            )
                        raise
                    for session in sessions.values():
                        session.confirm_pending_execution(
                            decision,
                            receipts[execution_receipt_start:],
                        )
                    state = _advance_to_clean_input(bridge, state)
                    last_state = dict(state)
                    if tracker is not None and not bool(state.get("finished")):
                        tracker.observe(state)
                    decision_index += 1
                receipts.append({
                    "kind": "DEVELOPMENT_RESPONSIVE_V4_DRIVE",
                    "responsive_event_count": getattr(
                        bridge, "responsive_event_count", None
                    ),
                    "responsive_applied_damage": getattr(
                        bridge, "responsive_applied_damage", None
                    ),
                    "target_event_diagnostic": (
                        bridge.target_event_diagnostic()
                        if hasattr(bridge, "target_event_diagnostic") else None
                    ),
                    "comparison_authorized": False,
                })
                terminal_actions: tuple[AvailableAction, ...] = ()
                if self._terminal_telemetry_action_refs:
                    telemetry, terminal_actions = _terminal_telemetry_receipt_v1(
                        bridge,
                        state,
                        self._terminal_telemetry_action_refs,
                    )
                    receipts.append(telemetry)
                return ScheduleReplayOutcomeV1(
                    seed=seed, status=ReplayStatusV1.COMPLETE,
                    state=state,
                    available_actions=terminal_actions,
                    receipts=tuple(receipts),
                    target_gate=(tracker.decision if tracker is not None else None),
                )
        except Exception as error:
            if "damage_done" not in last_state and not isinstance(
                last_state.get("dynamic_team_background"), Mapping
            ):
                last_state["damage_done"] = _state_damage(last_state)
            return ScheduleReplayOutcomeV1(
                seed=seed, status=ReplayStatusV1.INVALID,
                state=last_state, receipts=tuple(receipts),
                invalid_reason=f"{type(error).__name__}: {error}",
            )


__all__ = ["NativeDynamicV4ResponsiveActionProgramReplayV1"]
