from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

import pytest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    OptionalOffGcdPrefixV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramInsertionPointV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
    SearchedOffGcdInsertionV1,
    _replace_queue_gcd_block_v1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.causal_guard_v1 import (
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
    observable_causal_guard_from_dict_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import (
    ActResult,
    ActionRef,
    AvailableAction,
    CancelQueueResult,
    SetTargetResult,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1


BURST = ActionRef(item_id=56113)
STRIKE = ActionRef(spell_id=23894)
EXECUTE = ActionRef(spell_id=20662)
DEATH_WISH = ActionRef(spell_id=12328)
SAPPER = ActionRef(item_id=10646)
HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)


@dataclass(frozen=True)
class _Case:
    request: dict
    dynamic_load: object


def _case(_: int) -> _Case:
    return _Case(
        request={"request": "fake"},
        dynamic_load=SimpleNamespace(config={"dynamic": "fake"}),
    )


class _TwoWaveBridge:
    def __init__(self, *, finish_on_wait: bool = False) -> None:
        self.stage = 0
        self.target_index = 0
        self.burst_used = False
        self.finish_on_wait = finish_on_wait
        self.command_order: list[str] = []
        self._needs_input = True
        self._finished = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def _state(self) -> dict:
        first_dead = self.stage >= 1
        second_attackable = self.stage >= 1
        finished = self._finished
        first_health = 0.0 if first_dead else 10.0
        second_health = 0.0 if finished else 100.0
        time_ms = 0 if self.stage == 0 else (13_000 if finished else 12_000)
        damage = 0.0 if self.stage == 0 else (22.0 if finished else 11.0)
        return {
            "time_ms": time_ms,
            "needs_input": self._needs_input and not finished,
            "finished": finished,
            "target_index": self.target_index,
            "future_schedule": {"must_not_reach_policy": True},
            "dynamic_team_background": {
                "simulated_damage_applied": damage,
                "background_damage_applied": 90.0 if first_dead else 0.0,
                "targets": [
                    {
                        "target_index": 0,
                        "initial_health": 100.0,
                        "current_health": first_health,
                        "dead": first_dead,
                        "simulated_damage_applied": min(damage, 11.0),
                        "background_damage_applied": 90.0 if first_dead else 0.0,
                    },
                    {
                        "target_index": 1,
                        "initial_health": 100.0,
                        "current_health": second_health,
                        "dead": finished,
                        "simulated_damage_applied": max(0.0, damage - 11.0),
                        "background_damage_applied": 0.0,
                    },
                ],
            },
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "maximum_health": 100.0,
                        "current_health": first_health,
                        "dead": first_dead,
                        "attackable": not first_dead,
                    },
                    {
                        "target_index": 1,
                        "maximum_health": 100.0,
                        "current_health": second_health,
                        "dead": finished,
                        "attackable": second_attackable and not finished,
                    },
                ]
            },
        }

    def load_dynamic_v3(self, request, seed, config):
        del request, seed, config
        return SimpleNamespace(state=self._state())

    def actions(self):
        return [
            AvailableAction(
                0,
                BURST,
                "Rapid Growth",
                not self.burst_used,
                0 if not self.burst_used else 120_000,
                False,
            ),
            AvailableAction(
                1,
                STRIKE,
                "Bloodthirst",
                True,
                0,
                True,
                result_bearing=True,
            ),
            AvailableAction(
                2,
                DEATH_WISH,
                "Death Wish",
                self.stage == 0,
                0 if self.stage == 0 else 180_000,
                True,
            ),
            AvailableAction(
                3,
                SAPPER,
                "Goblin Sapper Charge",
                True,
                0,
                False,
            ),
        ]

    def set_target(self, target_index: int):
        self.target_index = target_index
        self.command_order.append(f"target:{target_index}")
        return SetTargetResult(
            changed=True,
            target_index=target_index,
            finished=False,
            needs_input=True,
            state=self._state(),
        )

    def start_attack(self):
        self.command_order.append("start_attack")
        return SimpleNamespace(
            accepted=True, consumes_decision=False, state=self._state()
        )

    def stop_cast(self):
        self.command_order.append("stop_cast")
        return SimpleNamespace(
            accepted=True, consumes_decision=False, state=self._state()
        )

    def act(self, action: ActionRef, *, attempt_id=None):
        del attempt_id
        if action == BURST:
            if self.burst_used:
                raise AssertionError("burst was consumed twice")
            self.burst_used = True
            self.command_order.append("burst")
            return ActResult(True, False, False, True, self._state())
        if action == SAPPER:
            self.command_order.append("sapper")
            return ActResult(True, False, False, True, self._state())
        if action != STRIKE:
            if action != DEATH_WISH:
                raise AssertionError(f"unexpected action {action}")
            self.command_order.append("death_wish")
            self.stage = 1
            self._needs_input = False
            return ActResult(True, True, False, False, self._state())
        self.command_order.append("strike")
        if self.stage == 0:
            self.stage = 1
            self._needs_input = False
        else:
            self.stage = 2
            self._finished = True
            self._needs_input = False
        return ActResult(
            True,
            True,
            self._finished,
            False,
            self._state(),
        )

    def cancel_queue(self):
        self.command_order.append("cancel_queue")
        return CancelQueueResult(True, False, False, True, self._state())

    def wait(self, wait_ms: int):
        self.command_order.append(f"wait:{wait_ms}")
        if self.finish_on_wait:
            self.stage = 2
            self._finished = True
            self._needs_input = False
        return self._state()

    def advance(self):
        self._needs_input = True
        return self._state()


def _causal_projector(state, available) -> CausalLiveStateProjectionV1:
    del available
    safe = {
        key: deepcopy(value)
        for key, value in state.items()
        if key != "future_schedule"
    }
    visible_count = 1 if state["time_ms"] < 12_000 else 2
    for block_name in (
        "dynamic_team_background",
        "dynamic_target_semantics",
    ):
        safe[block_name]["targets"] = safe[block_name]["targets"][:visible_count]
    return CausalLiveStateProjectionV1(
        state=safe,
        policy_to_simulator_target_index=(0, 1)[:visible_count],
        visibility_cutoff_ms=state["time_ms"],
    )


def _instant_guard(**kwargs) -> ObservableCausalGuardV1:
    return ObservableCausalGuardV1(false_semantics=SKIP_PLAN, **kwargs)


def _burst_prefix(target_index: int) -> OptionalOffGcdPrefixV1:
    return OptionalOffGcdPrefixV1(
        BURST,
        _instant_guard(
            target_index=target_index,
            target_hp_pct_gte=50,
            target_attackable_is=True,
            action_ready=BURST,
        ),
    )


def _decision(target_index: int, *, ordered_controls: bool) -> ProgramDecisionV1:
    kwargs = {}
    if ordered_controls:
        kwargs = {
            "start_attack": True,
            "stop_cast": True,
            "prefix_order": (
                ProgramPrefixOperationKindV1.START_ATTACK,
                ProgramPrefixOperationKindV1.SET_TARGET,
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD,
                ProgramPrefixOperationKindV1.STOP_CAST,
                ProgramPrefixOperationKindV1.QUEUE_KEEP,
            ),
        }
    return ProgramDecisionV1(
        target_index=target_index,
        optional_off_gcd_prefixes=(_burst_prefix(target_index),),
        gcd_action=STRIKE,
        **kwargs,
    )


def _two_wave_program() -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id="late-arrival-reschedule",
        selector=OrderedGuardSelectorV1(
            alternatives=(
                GuardedAlternativeV1(
                    "first-pack",
                    _instant_guard(
                        target_index=0,
                        target_attackable_is=True,
                        action_ready=STRIKE,
                    ),
                    _decision(0, ordered_controls=True),
                ),
                GuardedAlternativeV1(
                    "second-pack",
                    _instant_guard(
                        target_index=1,
                        target_attackable_is=True,
                        action_ready=STRIKE,
                    ),
                    _decision(1, ordered_controls=False),
                ),
            ),
            fallback=ProgramDecisionV1(wait_ms=100),
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=("unit-test-search",),
    )


class _SourceResolver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, observation, available) -> ProgramDecisionV1:
        self.calls += 1
        if "future_schedule" in observation.state:
            raise AssertionError("raw future schedule leaked to source")
        if not any(row.action == STRIKE for row in available):
            raise AssertionError("missing current action surface")
        target_index = (
            1
            if len(observation.policy_to_simulator_target_index) > 1
            else 0
        )
        return ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            stop_cast=True,
            gcd_action=STRIKE,
            prefix_order=(
                ProgramPrefixOperationKindV1.STOP_CAST,
                ProgramPrefixOperationKindV1.START_ATTACK,
                ProgramPrefixOperationKindV1.SET_TARGET,
                ProgramPrefixOperationKindV1.QUEUE_KEEP,
            ),
        )


class _FeedbackSourceResolver(_SourceResolver):
    def __init__(self) -> None:
        super().__init__()
        self.confirmed: list[ProgramDecisionV1] = []
        self.rejected: list[str] = []

    def record_last_executed_decision_v1(
        self, actual_decision: ProgramDecisionV1
    ) -> None:
        self.confirmed.append(actual_decision)

    def reject_last_execution_v1(self, reason: str) -> None:
        self.rejected.append(reason)


class _RejectingStrikeBridge(_TwoWaveBridge):
    def act(self, action: ActionRef, *, attempt_id=None):
        if action == STRIKE:
            self.command_order.append("rejected-strike")
            return ActResult(False, False, False, True, self._state())
        return super().act(action, attempt_id=attempt_id)


def _source_binding(sessions: list[_SourceResolver]):
    def factory():
        resolver = _SourceResolver()
        sessions.append(resolver)
        return resolver

    return ImportedReactiveProgramBindingV1(
        "source-binding-v1",
        "Cat-or-Contra-source",
        "causal-live-state/v1",
        factory,
    )


def _imported_selector(binding) -> ImportedReactiveSelectorV1:
    return ImportedReactiveSelectorV1(
        binding.binding_id,
        binding.source_policy_id,
        binding.observation_contract_id,
    )


def _imported_program(binding) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        "source-incumbent",
        _imported_selector(binding),
        ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
        source_refs=("source-policy.lua",),
    )


def test_searched_reactive_imported_selector_roundtrips_but_plain_searched_is_rejected():
    binding = _source_binding([])
    program = CausalActionProgramV1(
        "searched-reactive-sequence",
        _imported_selector(binding),
        ProgramOriginV1.SEARCHED_REACTIVE,
        source_refs=("frozen-two-wave-sequence",),
    )

    rebuilt = causal_action_program_from_dict_v1(program.to_dict())
    assert rebuilt == program
    assert rebuilt.program_key() == program.program_key()
    with pytest.raises(
        ValueError,
        match="imported or searched-reactive origin",
    ):
        CausalActionProgramV1(
            "misclassified-stateful-search",
            _imported_selector(binding),
            ProgramOriginV1.SEARCHED,
        )


def _overlay_program(
    binding,
    *,
    terminal_alternatives=(),
    insertions=(),
    insertion_order=(),
) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        "searched-burst-over-source",
        ImportedFallbackOverlaySelectorV1(
            terminal_alternatives=terminal_alternatives,
            imported_fallback=_imported_selector(binding),
            off_gcd_insertions=insertions,
            insertion_order=insertion_order,
        ),
        ProgramOriginV1.SEARCHED,
        source_refs=("source-policy.lua", "searched-burst-space"),
    )


def _queue_gcd_block_alternatives(target_index: int = 0):
    return (
        GuardedAlternativeV1(
            "heroic-strike-queue",
            _instant_guard(
                target_index=target_index,
                target_attackable_is=True,
                action_ready=HEROIC_STRIKE,
            ),
            ProgramDecisionV1(
                queue_op=QueueLaneOp.SET,
                queue_action=HEROIC_STRIKE,
                wait_ms=1,
            ),
        ),
        GuardedAlternativeV1(
            "death-wish-gcd",
            _instant_guard(
                target_index=target_index,
                target_attackable_is=True,
                action_ready=DEATH_WISH,
            ),
            ProgramDecisionV1(gcd_action=DEATH_WISH),
        ),
    )


def _composite_program(
    binding,
    *,
    terminal_alternatives=(),
) -> CausalActionProgramV1:
    insertion = SearchedOffGcdInsertionV1(
        "rapid-target-0",
        OptionalOffGcdPrefixV1(
            BURST,
            _instant_guard(
                target_index=0,
                target_attackable_is=True,
                action_ready=BURST,
            ),
        ),
    )
    return CausalActionProgramV1(
        "searched-burst-and-block-over-source",
        ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=terminal_alternatives,
            imported_fallback=_imported_selector(binding),
            off_gcd_insertions=(insertion,),
            insertion_order=(insertion.insertion_id,),
            block_alternatives=_queue_gcd_block_alternatives(),
        ),
        ProgramOriginV1.SEARCHED,
        source_refs=(
            "source-policy.lua",
            "searched-burst-space",
            "searched-queue-gcd-block-space",
        ),
    )


class CausalActionProgramV1Tests(unittest.TestCase):
    def test_rage_lte_guard_validates_round_trips_and_evaluates(self):
        guard = ObservableCausalGuardV1(
            rage_gte=20,
            rage_lte=40,
            false_semantics=SKIP_PLAN,
        )
        self.assertEqual(
            guard,
            observable_causal_guard_from_dict_v1(guard.to_dict()),
        )
        state = {"power": {"type": "rage", "current": 40.0}}
        self.assertTrue(evaluate_observable_guard_v1(guard, state, ()).satisfied)
        state["power"]["current"] = 41.0
        self.assertEqual(
            ("rage_lte",),
            evaluate_observable_guard_v1(
                guard, state, ()
            ).failed_predicates,
        )
        with self.assertRaisesRegex(
            ValueError, "rage_gte cannot exceed rage_lte"
        ):
            ObservableCausalGuardV1(rage_gte=60, rage_lte=40)

    def test_late_arrival_skips_burst_then_reschedules_on_later_wave(self):
        bridges: list[_TwoWaveBridge] = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        replay = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            result_bearing_action_refs=(STRIKE,),
        )
        result = replay.replay(7, _two_wave_program(), max_decisions=4)

        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual(22.0, result.effective_damage)
        kinds = [row["kind"] for row in result.receipts]
        self.assertEqual(1, kinds.count("OPTIONAL_OFF_GCD_SKIPPED"))
        self.assertEqual(1, kinds.count("OPTIONAL_OFF_GCD_EXECUTED"))
        executed = next(
            row
            for row in result.receipts
            if row["kind"] == "OPTIONAL_OFF_GCD_EXECUTED"
        )
        self.assertEqual(1, executed["decision_index"])
        self.assertTrue(bridges[0].burst_used)
        self.assertEqual(
            [
                "start_attack",
                "target:0",
                "stop_cast",
                "strike",
                "target:1",
                "burst",
                "strike",
            ],
            bridges[0].command_order,
        )

    def test_unconditional_wait_fallback_is_executed(self):
        bridges: list[_TwoWaveBridge] = []

        def bridge_factory():
            bridge = _TwoWaveBridge(finish_on_wait=True)
            bridges.append(bridge)
            return bridge

        program = CausalActionProgramV1(
            "fallback-only",
            OrderedGuardSelectorV1((), ProgramDecisionV1(wait_ms=125)),
            ProgramOriginV1.HAND_AUTHORED_DEVELOPMENT,
        )
        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory, _case, _causal_projector
        ).replay(8, program)
        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual(["wait:125"], bridges[0].command_order)
        self.assertIn(
            "UNCONDITIONAL_FALLBACK_SELECTED",
            [row["kind"] for row in result.receipts],
        )

    def test_wire_round_trip_preserves_semantic_identity_and_sink_order(self):
        program = _two_wave_program()
        restored = causal_action_program_from_dict_v1(program.to_dict())
        self.assertEqual(program, restored)
        self.assertEqual(program.program_key(), restored.program_key())
        first = restored.selector.alternatives[0].decision
        self.assertEqual(
            ProgramPrefixOperationKindV1.START_ATTACK,
            first.prefix_order[0],
        )

    def test_ordered_selector_expresses_multiple_ready_terminals_then_wait(self):
        selector = OrderedGuardSelectorV1(
            (
                GuardedAlternativeV1(
                    "execute-ready",
                    _instant_guard(action_ready=EXECUTE),
                    ProgramDecisionV1(gcd_action=EXECUTE),
                ),
                GuardedAlternativeV1(
                    "bloodthirst-ready",
                    _instant_guard(action_ready=STRIKE),
                    ProgramDecisionV1(gcd_action=STRIKE),
                ),
            ),
            ProgramDecisionV1(wait_ms=100),
        )
        self.assertEqual(EXECUTE, selector.alternatives[0].decision.gcd_action)
        self.assertEqual(STRIKE, selector.alternatives[1].decision.gcd_action)
        self.assertEqual(100, selector.fallback.wait_ms)

    def test_imported_order_can_preserve_repeated_off_gcd_source_sink(self):
        prefix = OptionalOffGcdPrefixV1(
            BURST, _instant_guard(action_ready=BURST)
        )
        decision = ProgramDecisionV1(
            optional_off_gcd_prefixes=(prefix, prefix),
            gcd_action=STRIKE,
        )
        self.assertEqual(2, len(decision.optional_off_gcd_prefixes))
        self.assertEqual(
            2,
            decision.prefix_order.count(
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
            ),
        )

    def test_not_yet_visible_target_guard_is_audited_nonmatch(self):
        bridges = []

        def bridge_factory():
            bridge = _TwoWaveBridge(finish_on_wait=True)
            bridges.append(bridge)
            return bridge

        program = CausalActionProgramV1(
            "hidden-target-fallthrough",
            OrderedGuardSelectorV1(
                (
                    GuardedAlternativeV1(
                        "visible-but-low",
                        _instant_guard(
                            target_index=0,
                            target_hp_pct_gte=50,
                            action_ready=STRIKE,
                        ),
                        ProgramDecisionV1(gcd_action=STRIKE),
                    ),
                    GuardedAlternativeV1(
                        "later-target-not-yet-visible",
                        _instant_guard(
                            target_index=1,
                            target_attackable_is=True,
                            action_ready=STRIKE,
                        ),
                        ProgramDecisionV1(target_index=1, gcd_action=STRIKE),
                    ),
                ),
                ProgramDecisionV1(wait_ms=100),
            ),
            ProgramOriginV1.SEARCHED,
        )
        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory, _case, _causal_projector
        ).replay(20, program)
        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        hidden = next(
            row
            for row in result.receipts
            if row.get("alternative_id") == "later-target-not-yet-visible"
        )
        self.assertFalse(hidden["matched"])
        self.assertEqual(
            ["target_not_prefix_visible"],
            hidden["evaluation"]["failed_predicates"],
        )
        self.assertFalse(hidden["evaluation"]["observed"]["target_visible"])
        self.assertEqual(["wait:100"], bridges[0].command_order)

    def test_malformed_visible_target_still_fails_closed(self):
        def malformed_projector(state, available):
            projected = _causal_projector(state, available)
            projected.state["dynamic_target_semantics"]["targets"][0].pop(
                "maximum_health"
            )
            projected.state["dynamic_team_background"]["targets"][0].pop(
                "initial_health"
            )
            return projected

        program = CausalActionProgramV1(
            "malformed-visible-target",
            OrderedGuardSelectorV1(
                (
                    GuardedAlternativeV1(
                        "requires-visible-hp",
                        _instant_guard(
                            target_index=0,
                            target_hp_pct_gte=50,
                        ),
                        ProgramDecisionV1(gcd_action=STRIKE),
                    ),
                ),
                ProgramDecisionV1(wait_ms=100),
            ),
            ProgramOriginV1.SEARCHED,
        )
        result = NativeDynamicV3ActionProgramReplayV1(
            _TwoWaveBridge, _case, malformed_projector
        ).replay(21, program)
        self.assertEqual(ReplayStatusV1.INVALID, result.status)
        self.assertIn("GuardObservationError", result.invalid_reason)

    def test_empty_overlay_is_command_equivalent_to_imported_incumbent(self):
        imported_sessions = []
        overlay_sessions = []
        imported_binding = _source_binding(imported_sessions)
        overlay_binding = _source_binding(overlay_sessions)
        imported_bridges = []
        overlay_bridges = []

        def imported_bridge_factory():
            bridge = _TwoWaveBridge()
            imported_bridges.append(bridge)
            return bridge

        def overlay_bridge_factory():
            bridge = _TwoWaveBridge()
            overlay_bridges.append(bridge)
            return bridge

        imported = NativeDynamicV3ActionProgramReplayV1(
            imported_bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(imported_binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(30, _imported_program(imported_binding))
        overlay = NativeDynamicV3ActionProgramReplayV1(
            overlay_bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(overlay_binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(30, _overlay_program(overlay_binding))
        self.assertEqual(ReplayStatusV1.COMPLETE, imported.status)
        self.assertEqual(ReplayStatusV1.COMPLETE, overlay.status)
        self.assertEqual(imported.state, overlay.state)
        self.assertEqual(
            imported_bridges[0].command_order,
            overlay_bridges[0].command_order,
        )
        self.assertEqual([2], [row.calls for row in imported_sessions])
        self.assertEqual([2], [row.calls for row in overlay_sessions])

    def test_native_replay_confirms_imported_decision_only_after_acceptance(self):
        sessions: list[_FeedbackSourceResolver] = []

        def open_resolver():
            resolver = _FeedbackSourceResolver()
            sessions.append(resolver)
            return resolver

        binding = ImportedReactiveProgramBindingV1(
            "feedback-source",
            "feedback-policy",
            "causal-live-state/v1",
            open_resolver,
        )
        result = NativeDynamicV3ActionProgramReplayV1(
            _TwoWaveBridge,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(301, _imported_program(binding))

        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual(2, len(sessions[0].confirmed))
        self.assertEqual([], sessions[0].rejected)

    def test_native_replay_rejects_imported_proposal_when_bridge_rejects(self):
        sessions: list[_FeedbackSourceResolver] = []

        def open_resolver():
            resolver = _FeedbackSourceResolver()
            sessions.append(resolver)
            return resolver

        binding = ImportedReactiveProgramBindingV1(
            "feedback-source",
            "feedback-policy",
            "causal-live-state/v1",
            open_resolver,
        )
        result = NativeDynamicV3ActionProgramReplayV1(
            _RejectingStrikeBridge,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(302, _imported_program(binding))

        self.assertEqual(ReplayStatusV1.INVALID, result.status)
        self.assertEqual([], sessions[0].confirmed)
        self.assertEqual(1, len(sessions[0].rejected))
        self.assertIn("terminal GCD action failed", sessions[0].rejected[0])

    def test_overlay_skips_low_hp_target_then_uses_burst_on_later_wave(self):
        sessions = []
        binding = _source_binding(sessions)
        insertions = (
            SearchedOffGcdInsertionV1("rapid-target-0", _burst_prefix(0)),
            SearchedOffGcdInsertionV1("rapid-target-1", _burst_prefix(1)),
        )
        program = _overlay_program(
            binding,
            insertions=insertions,
            insertion_order=("rapid-target-0", "rapid-target-1"),
        )
        bridges = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(31, program)
        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual([2], [row.calls for row in sessions])
        self.assertEqual(1, bridges[0].command_order.count("burst"))
        second_epoch = bridges[0].command_order.index("burst")
        self.assertLess(
            second_epoch,
            bridges[0].command_order.index("target:1"),
        )
        deferred_later_target = [
            row
            for row in result.receipts
            if row.get("kind")
            == "SEARCHED_OFF_GCD_INSERTION_TARGET_DEFERRED"
            and row.get("insertion_id") == "rapid-target-1"
            and row.get("decision_index") == 0
        ]
        self.assertEqual(1, len(deferred_later_target))
        composed = [
            row
            for row in result.receipts
            if row.get("kind") == "SEARCHED_OFF_GCD_PREFIXES_PREPENDED"
        ]
        self.assertEqual(2, len(composed))
        self.assertEqual(
            ["rapid-target-0", "rapid-target-1"],
            composed[0]["configured_insertion_order"],
        )
        self.assertEqual(
            ["rapid-target-0"],
            composed[0]["eligible_insertion_order"],
        )
        self.assertEqual(
            ["rapid-target-1"],
            composed[1]["eligible_insertion_order"],
        )
        self.assertEqual(
            [
                "STOP_CAST",
                "START_ATTACK",
                "SET_TARGET",
                "QUEUE_KEEP",
            ],
            composed[0]["source_prefix_order"],
        )

    def test_target_affecting_insertion_runs_after_source_target_switch(self):
        sessions = []
        binding = _source_binding(sessions)
        insertion = SearchedOffGcdInsertionV1(
            "sapper-target-1",
            OptionalOffGcdPrefixV1(
                SAPPER,
                _instant_guard(
                    target_index=1,
                    target_attackable_is=True,
                    action_ready=SAPPER,
                ),
            ),
            ProgramInsertionPointV1.AFTER_SOURCE_SET_TARGET,
        )
        program = _overlay_program(
            binding,
            insertions=(insertion,),
            insertion_order=("sapper-target-1",),
        )
        bridges = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(33, program)
        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        commands = bridges[0].command_order
        target_position = commands.index("target:1")
        sapper_position = commands.index("sapper")
        second_strike_position = len(commands) - 1
        self.assertLess(target_position, sapper_position)
        self.assertLess(sapper_position, second_strike_position)
        receipt = next(
            row
            for row in result.receipts
            if row.get("kind") == "SEARCHED_OFF_GCD_PREFIXES_PREPENDED"
            and row.get("decision_index") == 1
        )
        self.assertEqual(
            "AFTER_SOURCE_SET_TARGET",
            receipt["insertion_points"]["sapper-target-1"],
        )

    def test_overlay_gcd_alternative_consumes_epoch_before_source_fallback(self):
        sessions = []
        binding = _source_binding(sessions)
        terminal = GuardedAlternativeV1(
            "death-wish-first",
            _instant_guard(action_ready=DEATH_WISH),
            ProgramDecisionV1(gcd_action=DEATH_WISH),
        )
        program = _overlay_program(
            binding, terminal_alternatives=(terminal,)
        )
        bridges = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(32, program)
        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual("death_wish", bridges[0].command_order[0])
        self.assertEqual([1], [row.calls for row in sessions])
        imported_receipts = [
            row
            for row in result.receipts
            if row.get("kind") == "IMPORTED_REACTIVE_INCUMBENT_SELECTED"
        ]
        self.assertEqual([1], [row["decision_index"] for row in imported_receipts])

    def test_overlay_wire_roundtrip_freezes_insertion_order_and_import(self):
        sessions = []
        binding = _source_binding(sessions)
        insertions = (
            SearchedOffGcdInsertionV1("first", _burst_prefix(0)),
            SearchedOffGcdInsertionV1("second", _burst_prefix(1)),
        )
        terminal = GuardedAlternativeV1(
            "death-wish",
            _instant_guard(action_ready=DEATH_WISH),
            ProgramDecisionV1(gcd_action=DEATH_WISH),
        )
        program = _overlay_program(
            binding,
            terminal_alternatives=(terminal,),
            insertions=insertions,
            insertion_order=("second", "first"),
        )
        restored = causal_action_program_from_dict_v1(program.to_dict())
        self.assertEqual(program, restored)
        self.assertEqual(program.program_key(), restored.program_key())
        self.assertEqual(
            ("second", "first"), restored.selector.insertion_order
        )

    def test_composite_wire_roundtrip_freezes_burst_then_block_contract(self):
        binding = _source_binding([])
        program = _composite_program(binding)
        restored = causal_action_program_from_dict_v1(program.to_dict())

        self.assertEqual(program, restored)
        self.assertEqual(program.program_key(), restored.program_key())
        self.assertIsInstance(
            restored.selector,
            ImportedReactiveBurstQueueGcdBlockSelectorV1,
        )
        self.assertEqual(
            ("rapid-target-0",), restored.selector.insertion_order
        )
        self.assertEqual(
            ("heroic-strike-queue", "death-wish-gcd"),
            tuple(
                row.alternative_id
                for row in restored.selector.block_alternatives
            ),
        )

    def test_queue_only_replacement_inherits_source_gcd_and_wait_lane(self):
        binding = _source_binding([])
        selector = ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=(),
            imported_fallback=_imported_selector(binding),
            block_alternatives=(
                GuardedAlternativeV1(
                    "queue-only",
                    _instant_guard(
                        target_index=0,
                        target_attackable_is=True,
                        action_ready=HEROIC_STRIKE,
                    ),
                    ProgramDecisionV1(
                        queue_op=QueueLaneOp.SET,
                        queue_action=HEROIC_STRIKE,
                        wait_ms=1,
                    ),
                ),
            ),
        )
        source = ProgramDecisionV1(
            target_index=0,
            queue_op=QueueLaneOp.KEEP,
            gcd_action=STRIKE,
        )
        bridge = _TwoWaveBridge()
        available = tuple(
            bridge.actions()
            + [
                AvailableAction(
                    4,
                    HEROIC_STRIKE,
                    "Heroic Strike",
                    True,
                    0,
                    False,
                )
            ]
        )
        receipts = []

        composed = _replace_queue_gcd_block_v1(
            selector,
            source,
            _causal_projector(bridge._state(), available),
            available,
            receipts=receipts,
            decision_index=0,
        )

        self.assertEqual(QueueLaneOp.SET, composed.queue_op)
        self.assertEqual(HEROIC_STRIKE, composed.queue_action)
        self.assertEqual(STRIKE, composed.gcd_action)
        self.assertIsNone(composed.wait_ms)
        selected = next(
            row for row in receipts if row.get("kind") == "QUEUE_GCD_BLOCK_SELECTED"
        )
        self.assertTrue(selected["source_gcd_inherited"])
        self.assertFalse(selected["source_queue_inherited"])

    def test_composite_inserts_burst_before_atomic_queue_gcd_replacement(self):
        sessions = []
        binding = _source_binding(sessions)
        bridges = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(34, _composite_program(binding))

        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual([2], [row.calls for row in sessions])
        self.assertEqual(
            [
                "burst",
                "stop_cast",
                "start_attack",
                "target:0",
                "death_wish",
                "stop_cast",
                "start_attack",
                "target:1",
                "strike",
            ],
            bridges[0].command_order,
        )
        kinds = [row.get("kind") for row in result.receipts]
        first_insertion = kinds.index("SEARCHED_OFF_GCD_PREFIXES_PREPENDED")
        first_replacement = kinds.index("QUEUE_GCD_BLOCK_SELECTED")
        self.assertLess(first_insertion, first_replacement)
        replacement = result.receipts[first_replacement]
        self.assertEqual("death-wish-gcd", replacement["alternative_id"])
        self.assertEqual(
            ["OPTIONAL_OFF_GCD", "STOP_CAST", "START_ATTACK", "SET_TARGET"],
            replacement["preserved_source_prefix_order"],
        )

    def test_composite_terminal_burst_short_circuits_import_and_block(self):
        sessions = []
        binding = _source_binding(sessions)
        terminal = GuardedAlternativeV1(
            "death-wish-terminal",
            _instant_guard(action_ready=DEATH_WISH),
            ProgramDecisionV1(gcd_action=DEATH_WISH),
        )
        bridges = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        result = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        ).replay(
            35,
            _composite_program(
                binding, terminal_alternatives=(terminal,)
            ),
        )

        self.assertEqual(ReplayStatusV1.COMPLETE, result.status)
        self.assertEqual("death_wish", bridges[0].command_order[0])
        self.assertNotIn("burst", bridges[0].command_order)
        self.assertEqual([1], [row.calls for row in sessions])
        first_epoch_kinds = [
            row["kind"]
            for row in result.receipts
            if row.get("decision_index") == 0
        ]
        self.assertIn(
            "OVERLAY_TERMINAL_ALTERNATIVE_SELECTED", first_epoch_kinds
        )
        self.assertNotIn("IMPORTED_REACTIVE_INCUMBENT_SELECTED", first_epoch_kinds)
        self.assertNotIn("QUEUE_GCD_BLOCK_SELECTED", first_epoch_kinds)

    def test_imported_incumbent_is_same_program_type_and_session_is_per_replay(self):
        sessions = []

        class StatefulSourceResolver:
            def __init__(self):
                self.calls = 0

            def __call__(self, observation, available):
                self.calls += 1
                self.assert_causal(observation)
                self.assertTrue(any(row.action == STRIKE for row in available))
                return ProgramDecisionV1(
                    start_attack=True,
                    stop_cast=True,
                    gcd_action=STRIKE,
                    prefix_order=(
                        ProgramPrefixOperationKindV1.STOP_CAST,
                        ProgramPrefixOperationKindV1.START_ATTACK,
                        ProgramPrefixOperationKindV1.QUEUE_KEEP,
                    ),
                )

            @staticmethod
            def assert_causal(observation):
                if "future_schedule" in observation.state:
                    raise AssertionError("raw future schedule leaked to source")

            @staticmethod
            def assertTrue(value):
                if not value:
                    raise AssertionError("missing current action surface")

        def resolver_factory():
            resolver = StatefulSourceResolver()
            sessions.append(resolver)
            return resolver

        binding = ImportedReactiveProgramBindingV1(
            "cat-source-binding-v1",
            "Cat",
            "causal-live-state/v1",
            resolver_factory,
        )
        program = CausalActionProgramV1(
            "cat-imported-incumbent",
            ImportedReactiveSelectorV1(
                binding.binding_id,
                binding.source_policy_id,
                binding.observation_contract_id,
            ),
            ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
            source_refs=("Cat/WarriorFury.lua",),
        )
        bridges: list[_TwoWaveBridge] = []

        def bridge_factory():
            bridge = _TwoWaveBridge()
            bridges.append(bridge)
            return bridge

        replay = NativeDynamicV3ActionProgramReplayV1(
            bridge_factory,
            _case,
            _causal_projector,
            imported_bindings=(binding,),
            result_bearing_action_refs=(STRIKE,),
        )
        first = replay.replay(11, program)
        second = replay.replay(12, program)
        self.assertEqual(ReplayStatusV1.COMPLETE, first.status)
        self.assertEqual(ReplayStatusV1.COMPLETE, second.status)
        self.assertEqual([2, 2], [session.calls for session in sessions])
        self.assertIsInstance(program, CausalActionProgramV1)
        self.assertEqual(
            ["stop_cast", "start_attack", "strike"],
            bridges[0].command_order[:3],
        )


if __name__ == "__main__":
    unittest.main()
