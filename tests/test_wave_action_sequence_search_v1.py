from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_guard_v1 import (
    GuardTimeoutError,
    ObservableCausalGuardV1,
    SKIP_PLAN,
)
from o2o_dps.sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    SetTargetResult,
)
from o2o_dps.upper_kara_wave_local_search_contract_v1 import (
    ObservedTargetStateV1,
)
from o2o_dps.wave_action_schedule_v1 import (
    QueueLaneOp,
    ScheduledActionPlan,
    SearchCellIdentity,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    NativeDynamicV4ScheduleReplayV1,
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    _bounded_diverse_expansions_v1,
    _BeamNodeV1,
    _all_live_seeds_skipped_conditional_prefix_v1,
    _common_expansions,
    _execute_plan,
    _is_conditionally_common_skip_plan_v1,
    _observable_wait_options,
    _precombat_active,
    _queue_active,
    search_wave_action_sequences_v1,
)
from o2o_dps.upper_kara_wave_target_gate_v1 import (
    REACTIVE_ADDS_STAGE_ID_V1,
    REQUIRED_RETARGET_MODE_V1,
    ReactiveBossAddsTargetGateV1,
    WaveTargetGateDecisionV1,
)


A = ActionRef(spell_id=1001)
B = ActionRef(spell_id=1002)
SETUP = ActionRef(spell_id=1003)
HIT = ActionRef(spell_id=1004)
FINISH = ActionRef(spell_id=1005)
GUIDED_OFF_GCD = ActionRef(item_id=2001)
UNGUIDED_OFF_GCD = ActionRef(item_id=2002)
BURST = ActionRef(item_id=56113)
WHIRLWIND = ActionRef(spell_id=1680)
CLEAVE = ActionRef(spell_id=20569, tag=1)
SWEEPING_STRIKES = ActionRef(spell_id=12292)


def _available(
    action: ActionRef,
    *,
    legal: bool = True,
    ready_in_ms: int = 0,
    triggers_gcd: bool = True,
) -> AvailableAction:
    return AvailableAction(
        0,
        action,
        str(action),
        legal,
        ready_in_ms,
        triggers_gcd,
    )


class _TwoStepReplay:
    """B->A is the known optimum on both seeds."""

    def replay(self, seed, schedule):
        if any(step.gcd_action is None for step in schedule):
            return ScheduleReplayOutcomeV1(
                seed, ReplayStatusV1.INVALID,
                {"time_ms": len(schedule) * 100, "damage_done": 0.0},
                invalid_reason="fixture does not admit wait",
            )
        actions = [step.gcd_action.spell_id for step in schedule]
        if len(actions) > 2:
            return ScheduleReplayOutcomeV1(
                seed, ReplayStatusV1.INVALID,
                {"time_ms": 2000, "damage_done": 0.0},
                invalid_reason="too many actions",
            )
        damage = 0.0
        if actions:
            damage += 4 if actions[0] == A.spell_id else 10
        if len(actions) == 2:
            damage += 20 if actions == [B.spell_id, A.spell_id] else 3
        state = {"time_ms": len(actions) * 1000, "damage_done": damage}
        if len(actions) == 2:
            return ScheduleReplayOutcomeV1(seed, ReplayStatusV1.COMPLETE, state)
        return ScheduleReplayOutcomeV1(
            seed, ReplayStatusV1.FRONTIER, state,
            available_actions=(_available(A), _available(B)),
        )


class _BiasedGuide:
    guide_id = "cat-guide"

    def action_priorities(self, outcome, prefix):
        return {A: 100.0, B: 1.0}


class _NarrowBundleGuide:
    guide_id = "narrow-bundle-guide"

    def action_priorities(self, outcome, prefix):
        return {A: 100.0, GUIDED_OFF_GCD: 100.0}


class _FiniteCapDiversityReplay:
    """The unproposed off-GCD action is the one-step optimum."""

    def replay(self, seed, schedule):
        if not schedule:
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {"time_ms": 0, "damage_done": 0.0},
                available_actions=(
                    _available(A),
                    AvailableAction(
                        1,
                        GUIDED_OFF_GCD,
                        "guided-off-gcd",
                        True,
                        0,
                        False,
                    ),
                    AvailableAction(
                        2,
                        UNGUIDED_OFF_GCD,
                        "unguided-off-gcd",
                        True,
                        0,
                        False,
                    ),
                ),
            )
        if len(schedule) != 1:
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.INVALID,
                {"time_ms": 1_000, "damage_done": 0.0},
                invalid_reason="fixture admits exactly one plan",
            )
        step = schedule[0]
        damage = (
            100.0
            if UNGUIDED_OFF_GCD in step.off_gcd_actions
            else 10.0
            if GUIDED_OFF_GCD in step.off_gcd_actions
            else 1.0
        )
        return ScheduleReplayOutcomeV1(
            seed,
            ReplayStatusV1.COMPLETE,
            {"time_ms": 1_000, "damage_done": damage},
        )


class _ShortDenominatorWaitTrapReplay:
    """WAIT sees an early white hit but the GCD has more accumulated damage."""

    def replay(self, seed, schedule):
        if not schedule:
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {"time_ms": 0, "damage_done": 0.0},
                available_actions=(_available(A),),
            )
        step = schedule[-1]
        if step.wait_ms is not None:
            state = {"time_ms": 100, "damage_done": 10.0}
        else:
            state = {"time_ms": 1000, "damage_done": 20.0}
        return ScheduleReplayOutcomeV1(
            seed,
            ReplayStatusV1.FRONTIER,
            state,
            available_actions=(_available(A),),
        )


class _SetupContinuationReplay:
    """Immediate HIT leads on-prefix; zero-damage SETUP wins at wave end."""

    def replay(self, seed, schedule):
        if any(step.gcd_action is None for step in schedule):
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.INVALID,
                {"time_ms": 100, "damage_done": 0.0},
                invalid_reason="fixture does not admit wait",
            )
        actions = [step.gcd_action for step in schedule]
        if not actions:
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {"time_ms": 0, "damage_done": 0.0},
                available_actions=(_available(SETUP), _available(HIT)),
            )
        if len(actions) == 1:
            if actions[0] not in {SETUP, HIT}:
                return ScheduleReplayOutcomeV1(
                    seed,
                    ReplayStatusV1.INVALID,
                    {"time_ms": 1000, "damage_done": 0.0},
                    invalid_reason="invalid opener",
                )
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {
                    "time_ms": 1000,
                    "damage_done": 0.0 if actions[0] == SETUP else 10.0,
                    "opener": "SETUP" if actions[0] == SETUP else "HIT",
                },
                available_actions=(_available(FINISH),),
            )
        if len(actions) == 2 and actions[1] == FINISH:
            damage = 100.0 if actions[0] == SETUP else 20.0
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.COMPLETE,
                {"time_ms": 2000, "damage_done": damage},
            )
        return ScheduleReplayOutcomeV1(
            seed,
            ReplayStatusV1.INVALID,
            {"time_ms": 2000, "damage_done": 0.0},
            invalid_reason="invalid sequence",
        )


class _SetupContinuationGuide:
    guide_id = "complete-wave-guide"

    def action_priorities(self, outcome, prefix):
        return {action.action: 1.0 for action in outcome.available_actions}


class _AttemptBridge:
    def __init__(self):
        self.attempt_ids = []

    def actions(self):
        return (
            AvailableAction(0, A, "A", True, 0, True, True),
        )

    def act(self, action, *, attempt_id=None):
        self.attempt_ids.append(attempt_id)
        return SimpleNamespace(
            casted=True,
            consumes_decision=True,
            state={"time_ms": 0, "needs_input": False},
        )


class _MisdirectingBridge(_AttemptBridge):
    def __init__(self):
        super().__init__()
        self.set_target_calls = []

    def set_target(self, target_index):
        self.set_target_calls.append(target_index)
        return SimpleNamespace(
            target_index=target_index + 1,
            state={"time_ms": 0, "needs_input": True},
        )


class _ExactTargetActionBridge:
    def __init__(self, *actions):
        self._actions = tuple(actions)
        self.cast_actions = []
        self.set_target_calls = []
        self.current = {"time_ms": 0, "needs_input": True}

    def actions(self):
        return tuple(
            _available(
                action,
                triggers_gcd=(action != CLEAVE),
            )
            for action in self._actions
        )

    def set_target(self, target_index):
        self.set_target_calls.append(target_index)
        self.current = {**self.current, "target_index": target_index}
        return SimpleNamespace(
            target_index=target_index,
            state=self.current,
        )

    def act(self, action, *, attempt_id=None):
        self.cast_actions.append(action)
        self.current = {**self.current, "needs_input": False}
        return SimpleNamespace(
            casted=True,
            consumes_decision=(action != CLEAVE),
            state=self.current,
        )


class _StaticTargetGateTracker:
    def __init__(self, direct_target_indexes, collateral_target_indexes=(0, 1)):
        self.direct_target_indexes = tuple(direct_target_indexes)
        self.collateral_target_indexes = tuple(collateral_target_indexes)

    def observe(self, state):
        return WaveTargetGateDecisionV1(
            stage_id="stage",
            direct_target_indexes=self.direct_target_indexes,
            collateral_target_indexes=self.collateral_target_indexes,
        )


class _SeedGuardTimingBridge:
    def __init__(self, seed: int, *, finish_at_ms: int | None = None):
        self.seed = seed
        self.trigger_at_ms = {41: 200, 43: 400}.get(seed, 10_000)
        self.finish_at_ms = finish_at_ms
        self.current = self._state(0, needs_input=True)
        self.pending_until_ms = 0
        self.waits = []
        self.cast_times = []

    def _state(self, time_ms: int, *, needs_input: bool) -> dict:
        finished = (
            self.finish_at_ms is not None and time_ms >= self.finish_at_ms
        )
        return {
            "time_ms": time_ms,
            "finished": finished,
            "needs_input": needs_input and not finished,
            "power": {
                "type": "rage",
                "current": 60.0 if time_ms >= self.trigger_at_ms else 0.0,
                "maximum": 100.0,
            },
        }

    def actions(self):
        return (_available(A),)

    def wait(self, wait_ms):
        self.waits.append(wait_ms)
        self.pending_until_ms = self.current["time_ms"] + wait_ms
        self.current = self._state(
            self.current["time_ms"], needs_input=False
        )
        return self.current

    def advance(self):
        self.current = self._state(self.pending_until_ms, needs_input=True)
        return self.current

    def act(self, action, *, attempt_id=None):
        self.cast_times.append(self.current["time_ms"])
        self.current = {
            **self.current,
            "needs_input": False,
        }
        return SimpleNamespace(
            casted=True,
            consumes_decision=True,
            state=self.current,
        )


class _GuardPreferringReplay:
    def replay(self, seed, schedule):
        if not schedule:
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {"time_ms": 0, "damage_done": 0.0},
                available_actions=(_available(A),),
            )
        if len(schedule) != 1 or schedule[0].gcd_action != A:
            return ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.INVALID,
                {"time_ms": 1_000, "damage_done": 0.0},
                invalid_reason="fixture requires one A action",
            )
        damage = 100.0 if schedule[0].guard is not None else 10.0
        return ScheduleReplayOutcomeV1(
            seed,
            ReplayStatusV1.COMPLETE,
            {"time_ms": 1_000, "damage_done": damage},
        )


class _ArrivalRescheduleBridge:
    def __init__(self):
        self.current = self._state(
            target0_health=20.0,
            target0_attackable=True,
            target1_health=100.0,
            target1_attackable=False,
        )
        self.burst_available = True
        self.cast_actions = []
        self.waits = []

    def _state(
        self,
        *,
        target0_health: float,
        target0_attackable: bool,
        target1_health: float,
        target1_attackable: bool,
    ):
        return {
            "time_ms": getattr(self, "current", {}).get("time_ms", 0),
            "needs_input": True,
            "finished": False,
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "maximum_health": 100.0,
                        "current_health": target0_health,
                        "dead": target0_health <= 0,
                        "attackable": target0_attackable,
                    },
                    {
                        "target_index": 1,
                        "maximum_health": 100.0,
                        "current_health": target1_health,
                        "dead": target1_health <= 0,
                        "attackable": target1_attackable,
                    },
                ]
            },
        }

    def actions(self):
        return (
            _available(
                BURST,
                legal=self.burst_available,
                ready_in_ms=0 if self.burst_available else 120_000,
                triggers_gcd=False,
            ),
        )

    def set_target(self, target_index):
        return SimpleNamespace(target_index=target_index, state=self.current)

    def act(self, action, *, attempt_id=None):
        self.cast_actions.append(action)
        self.burst_available = False
        return SimpleNamespace(
            casted=True,
            consumes_decision=False,
            state=self.current,
        )

    def wait(self, wait_ms):
        self.waits.append(wait_ms)
        self.current = {
            **self.current,
            "time_ms": self.current["time_ms"] + wait_ms,
            "needs_input": False,
        }
        return self.current


class WaveActionSequenceSearchV1Tests(unittest.TestCase):
    def setUp(self):
        self.cell = SearchCellIdentity("upper-kara", "wave-1", "exact-build-1")

    def test_search_changes_the_entire_action_order_and_finds_known_optimum(self):
        result = search_wave_action_sequences_v1(
            _TwoStepReplay(), self.cell,
            seeds=(11, 13), max_steps=2, beam_width=4,
            max_off_gcd_actions=0,
            replay_workers=2,
        )
        self.assertEqual(result.status, "COMPLETE_PAIRED_SEED_SCHEDULE")
        self.assertEqual(
            [step.gcd_action for step in result.schedule],
            [B, A],
        )
        self.assertEqual(result.mean_dps, 15.0)
        self.assertEqual(result.replay_workers, 2)
        self.assertFalse(result.to_dict()["candidate_is_cat_or_contra_residual"])

    def test_guide_orders_expansion_but_cannot_remove_better_sequence(self):
        result = search_wave_action_sequences_v1(
            _TwoStepReplay(), self.cell,
            seeds=(17, 19), max_steps=2, beam_width=4,
            action_guides=(_BiasedGuide(),),
            max_off_gcd_actions=0,
        )
        self.assertEqual([step.gcd_action for step in result.schedule], [B, A])
        self.assertEqual(result.guide_ids, ("cat-guide",))
        self.assertEqual(
            result.to_dict()["guide_role"],
            "PROPOSAL_ORDER_ONLY_NOT_RUNTIME_FALLBACK",
        )

    def test_finite_cap_preserves_unguided_action_membership_and_can_win(self):
        result = search_wave_action_sequences_v1(
            _FiniteCapDiversityReplay(),
            self.cell,
            seeds=(21, 25),
            max_steps=1,
            beam_width=3,
            action_guides=(_NarrowBundleGuide(),),
            max_off_gcd_actions=1,
            max_expansions_per_node=3,
        )

        self.assertEqual(result.status, "COMPLETE_PAIRED_SEED_SCHEDULE")
        self.assertIn(UNGUIDED_OFF_GCD, result.schedule[0].off_gcd_actions)
        self.assertEqual(result.mean_dps, 100.0)
        self.assertEqual(result.guide_ids, ("narrow-bundle-guide",))

    def test_finite_cap_membership_is_guide_independent_but_output_is_ranked(self):
        a_plain = ScheduledActionPlan(at_or_after_ms=0, gcd_action=A)
        a_variant = ScheduledActionPlan(at_or_after_ms=1, gcd_action=A)
        b_plain = ScheduledActionPlan(at_or_after_ms=0, gcd_action=B)
        first_ranking = (
            replace(a_variant, guide_priority=200.0),
            replace(a_plain, guide_priority=100.0),
            replace(b_plain, guide_priority=0.0),
        )
        second_ranking = (
            replace(b_plain, guide_priority=300.0),
            replace(a_variant, guide_priority=2.0),
            replace(a_plain, guide_priority=1.0),
        )

        first = _bounded_diverse_expansions_v1(first_ranking, 2)
        second = _bounded_diverse_expansions_v1(second_ranking, 2)

        self.assertEqual(
            {plan.plan_key() for plan in first},
            {plan.plan_key() for plan in second},
        )
        self.assertEqual({plan.gcd_action for plan in first}, {A, B})
        self.assertEqual(first[0].gcd_action, A)
        self.assertEqual(second[0].gcd_action, B)

    def test_cells_are_explicit_and_search_results_do_not_share_winners(self):
        other = SearchCellIdentity("upper-kara", "boss-1", "exact-build-2")
        first = search_wave_action_sequences_v1(
            _TwoStepReplay(), self.cell,
            seeds=(1,), max_steps=2, beam_width=4, max_off_gcd_actions=0,
        )
        second = search_wave_action_sequences_v1(
            _TwoStepReplay(), other,
            seeds=(1,), max_steps=2, beam_width=4, max_off_gcd_actions=0,
        )
        self.assertNotEqual(first.cell.cell_key(), second.cell.cell_key())
        self.assertEqual(first.to_dict()["cell"]["wave_or_boss_id"], "wave-1")
        self.assertEqual(second.to_dict()["cell"]["wave_or_boss_id"], "boss-1")

    def test_frontier_ranking_does_not_reward_a_short_wait_denominator(self):
        result = search_wave_action_sequences_v1(
            _ShortDenominatorWaitTrapReplay(),
            self.cell,
            seeds=(23,),
            max_steps=1,
            beam_width=1,
            max_off_gcd_actions=0,
        )
        self.assertEqual(result.schedule[0].gcd_action, A)
        self.assertEqual(
            result.search_ranking_metric,
            "MEAN_OWN_EFFECTIVE_DAMAGE_AT_PREFIX",
        )
        self.assertEqual(result.search_ranking_score, 20.0)
        self.assertIsNone(result.mean_dps)

    def test_full_wave_continuation_keeps_zero_damage_setup_in_width_one_beam(self):
        replay = _SetupContinuationReplay()
        prefix_only = search_wave_action_sequences_v1(
            replay,
            self.cell,
            seeds=(29,),
            max_steps=2,
            beam_width=1,
            action_guides=(_SetupContinuationGuide(),),
            max_off_gcd_actions=0,
        )
        self.assertEqual(prefix_only.schedule[0].gcd_action, HIT)

        result = search_wave_action_sequences_v1(
            replay,
            self.cell,
            seeds=(29,),
            max_steps=2,
            beam_width=1,
            action_guides=(_SetupContinuationGuide(),),
            max_off_gcd_actions=0,
            continuation_max_steps=2,
        )
        self.assertEqual(
            [step.gcd_action for step in result.schedule],
            [SETUP, FINISH],
        )
        self.assertEqual(result.mean_dps, 50.0)
        self.assertEqual(
            result.search_ranking_metric,
            "BEST_GUIDED_FULL_WAVE_CONTINUATION_MEAN_DPS",
        )
        self.assertGreater(result.continuation_seed_replay_count, 0)
        self.assertEqual(
            result.continuation_scored_prefix_count,
            result.continuation_completed_prefix_count,
        )

    def test_native_executor_uses_runtime_result_bearing_metadata(self):
        bridge = _AttemptBridge()
        receipts = []
        _execute_plan(
            bridge,
            {"time_ms": 0, "needs_input": True},
            ScheduledActionPlan(at_or_after_ms=0, gcd_action=A),
            root_time_ms=0,
            step_index=3,
            receipts=receipts,
            result_bearing_action_refs=frozenset(),
        )
        self.assertEqual(bridge.attempt_ids, ["schedule-step-3:operation-1"])
        self.assertEqual(
            receipts[-1]["attempt_id"],
            "schedule-step-3:operation-1",
        )

    def test_runtime_rejects_a_target_that_left_the_current_stage(self):
        bridge = _MisdirectingBridge()
        with self.assertRaisesRegex(RuntimeError, "not legal"):
            _execute_plan(
                bridge,
                {"time_ms": 0, "needs_input": True},
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    target_index=1,
                    gcd_action=A,
                ),
                root_time_ms=0,
                step_index=0,
                receipts=[],
                result_bearing_action_refs=frozenset(),
                target_gate_tracker=_StaticTargetGateTracker((0,)),
            )
        self.assertEqual(bridge.set_target_calls, [])
        self.assertEqual(bridge.attempt_ids, [])

    def test_runtime_stops_when_set_target_selects_a_different_target(self):
        bridge = _MisdirectingBridge()
        with self.assertRaisesRegex(RuntimeError, "different target"):
            _execute_plan(
                bridge,
                {"time_ms": 0, "needs_input": True},
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    target_index=0,
                    gcd_action=A,
                ),
                root_time_ms=0,
                step_index=0,
                receipts=[],
                result_bearing_action_refs=frozenset(),
            )
        self.assertEqual(bridge.set_target_calls, [0])
        self.assertEqual(bridge.attempt_ids, [])

    def test_whirlwind_rejects_currently_attackable_forbidden_collateral(self):
        state = {
            "time_ms": 0,
            "needs_input": True,
            "target_index": 0,
            "dynamic_target_semantics": {
                "targets": [
                    {"target_index": 0, "dead": False, "attackable": True},
                    {"target_index": 1, "dead": False, "attackable": True},
                ]
            },
        }
        bridge = _ExactTargetActionBridge(WHIRLWIND)
        bridge.current = state
        with self.assertRaisesRegex(RuntimeError, "outside.*allowlist"):
            _execute_plan(
                bridge,
                state,
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    target_index=0,
                    gcd_action=WHIRLWIND,
                ),
                root_time_ms=0,
                step_index=0,
                receipts=[],
                result_bearing_action_refs=frozenset(),
                target_gate_tracker=_StaticTargetGateTracker(
                    (0,), collateral_target_indexes=(0,)
                ),
            )
        self.assertEqual(bridge.cast_actions, [])

    def test_whirlwind_allows_nonattackable_future_target(self):
        state = {
            "time_ms": 0,
            "needs_input": True,
            "target_index": 0,
            "dynamic_target_semantics": {
                "targets": [
                    {"target_index": 0, "dead": False, "attackable": True},
                    {"target_index": 1, "dead": False, "attackable": False},
                ]
            },
        }
        bridge = _ExactTargetActionBridge(WHIRLWIND)
        bridge.current = state
        receipts = []
        _execute_plan(
            bridge,
            state,
            ScheduledActionPlan(
                at_or_after_ms=0,
                target_index=0,
                gcd_action=WHIRLWIND,
            ),
            root_time_ms=0,
            step_index=0,
            receipts=receipts,
            result_bearing_action_refs=frozenset(),
            target_gate_tracker=_StaticTargetGateTracker(
                (0,), collateral_target_indexes=(0,)
            ),
        )
        self.assertEqual(bridge.cast_actions, [WHIRLWIND])
        self.assertEqual(
            receipts[-1]["collateral_gate"]["mode"],
            "CURRENT_ATTACKABLE_TARGETS_MUST_BE_ALLOWED",
        )

    def test_delayed_collateral_actions_reject_living_future_target(self):
        state = {
            "time_ms": 0,
            "needs_input": True,
            "target_index": 0,
            "dynamic_target_semantics": {
                "targets": [
                    {"target_index": 0, "dead": False, "attackable": True},
                    {"target_index": 1, "dead": False, "attackable": False},
                ]
            },
        }
        plans = (
            ScheduledActionPlan(
                at_or_after_ms=0,
                target_index=0,
                queue_op=QueueLaneOp.SET,
                queue_action=CLEAVE,
                gcd_action=A,
            ),
            ScheduledActionPlan(
                at_or_after_ms=0,
                target_index=0,
                gcd_action=SWEEPING_STRIKES,
            ),
        )
        for plan in plans:
            with self.subTest(plan=plan):
                bridge = _ExactTargetActionBridge(CLEAVE, A, SWEEPING_STRIKES)
                bridge.current = state
                with self.assertRaisesRegex(RuntimeError, "outside.*allowlist"):
                    _execute_plan(
                        bridge,
                        state,
                        plan,
                        root_time_ms=0,
                        step_index=0,
                        receipts=[],
                        result_bearing_action_refs=frozenset(),
                        target_gate_tracker=_StaticTargetGateTracker(
                            (0,), collateral_target_indexes=(0,)
                        ),
                    )
                self.assertEqual(bridge.cast_actions, [])

    def test_crusader_enchant_aura_is_not_a_swing_queue(self):
        state = {
            "auras": [
                {
                    "action": {"spell_id": 20007, "tag": 1},
                    "remaining_ms": 13_500,
                }
            ]
        }
        self.assertFalse(_queue_active(state))

    def test_only_active_precombat_state_opens_no_target_self_gcd_search(self):
        self.assertTrue(_precombat_active({"precombat": {"active": True}}))
        self.assertFalse(_precombat_active({"precombat": {"active": False}}))
        self.assertFalse(_precombat_active({}))

    def test_visible_pull_and_swing_clocks_create_timing_choices(self):
        outcome = ScheduleReplayOutcomeV1(
            31,
            ReplayStatusV1.FRONTIER,
            {
                "time_ms": 0,
                "damage_done": 0.0,
                "gcd_remaining_ms": 0,
                "mh_swing_remaining_ms": 700,
                "oh_swing_remaining_ms": None,
                "dynamic_idle_advance": {"horizon_ms": 30_000},
                "precombat": {"relative_time_ms": -3_000},
            },
            available_actions=(_available(A),),
        )
        self.assertEqual(
            _observable_wait_options((outcome,), 100),
            (700, 1500, 2900, 3000),
        )

    def test_one_guarded_plan_triggers_at_seed_specific_observed_times(self):
        guard = ObservableCausalGuardV1(
            rage_gte=50,
            timeout_ms=1_000,
            check_interval_ms=100,
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=A,
            guard=guard,
        )
        observed = {}
        for seed in (41, 43):
            bridge = _SeedGuardTimingBridge(seed)
            receipts = []
            _execute_plan(
                bridge,
                bridge.current,
                plan,
                root_time_ms=0,
                step_index=0,
                receipts=receipts,
                result_bearing_action_refs=frozenset(),
            )
            observed[seed] = bridge.cast_times[0]
            self.assertEqual(
                [row["kind"] for row in receipts if row["kind"].startswith("GUARD_")][
                    -1
                ],
                "GUARD_SATISFIED",
            )

        self.assertEqual(observed, {41: 200, 43: 400})
        self.assertEqual(
            plan.plan_key(),
            ScheduledActionPlan(
                at_or_after_ms=0,
                gcd_action=A,
                guard=guard,
            ).plan_key(),
        )

    def test_false_guard_has_terminal_and_timeout_semantics(self):
        terminal_plan = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=A,
            guard=ObservableCausalGuardV1(
                rage_gte=50,
                timeout_ms=1_000,
                check_interval_ms=100,
            ),
        )
        terminal_bridge = _SeedGuardTimingBridge(99, finish_at_ms=200)
        terminal_receipts = []
        terminal = _execute_plan(
            terminal_bridge,
            terminal_bridge.current,
            terminal_plan,
            root_time_ms=0,
            step_index=0,
            receipts=terminal_receipts,
            result_bearing_action_refs=frozenset(),
        )
        self.assertTrue(terminal["finished"])
        self.assertEqual(
            terminal_receipts[-1]["kind"],
            "GUARD_TERMINAL_BEFORE_SATISFIED",
        )
        self.assertEqual(terminal_bridge.cast_times, [])

        timeout_plan = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=A,
            guard=ObservableCausalGuardV1(
                rage_gte=50,
                timeout_ms=250,
                check_interval_ms=100,
            ),
        )
        timeout_bridge = _SeedGuardTimingBridge(99)
        timeout_receipts = []
        with self.assertRaisesRegex(GuardTimeoutError, "remained false"):
            _execute_plan(
                timeout_bridge,
                timeout_bridge.current,
                timeout_plan,
                root_time_ms=0,
                step_index=0,
                receipts=timeout_receipts,
                result_bearing_action_refs=frozenset(),
            )
        self.assertEqual(timeout_bridge.waits, [100, 100, 50])
        self.assertEqual(timeout_receipts[-1]["kind"], "GUARD_TIMEOUT")
        self.assertEqual(timeout_bridge.cast_times, [])

    def test_search_considers_guards_only_when_explicitly_passed(self):
        guard = ObservableCausalGuardV1(rage_gte=50)
        result = search_wave_action_sequences_v1(
            _GuardPreferringReplay(),
            self.cell,
            seeds=(47, 53),
            max_steps=1,
            beam_width=8,
            max_off_gcd_actions=0,
            guard_options=(guard,),
        )

        self.assertEqual(result.status, "COMPLETE_PAIRED_SEED_SCHEDULE")
        self.assertEqual(result.schedule[0].gcd_action, A)
        self.assertEqual(result.schedule[0].guard, guard)
        self.assertEqual(result.mean_dps, 100.0)

    def test_low_hp_skip_preserves_burst_for_next_attackable_wave(self):
        bridge = _ArrivalRescheduleBridge()
        wave1 = ScheduledActionPlan(
            at_or_after_ms=0,
            target_index=0,
            off_gcd_actions=(BURST,),
            guard=ObservableCausalGuardV1(
                target_index=0,
                target_hp_pct_gte=35,
                target_attackable_is=True,
                action_ready=BURST,
                false_semantics=SKIP_PLAN,
            ),
        )
        receipts = []
        skipped = _execute_plan(
            bridge,
            bridge.current,
            wave1,
            root_time_ms=0,
            step_index=0,
            receipts=receipts,
            result_bearing_action_refs=frozenset(),
        )
        self.assertEqual(skipped["time_ms"], 0)
        self.assertEqual(bridge.cast_actions, [])
        self.assertEqual(bridge.waits, [])
        self.assertTrue(bridge.burst_available)
        self.assertEqual(receipts[-1]["kind"], "GUARD_FALSE_PLAN_SKIPPED")
        self.assertEqual(
            receipts[-1]["evaluation"]["failed_predicates"],
            ["target_hp_pct_gte"],
        )

        bridge.current = bridge._state(
            target0_health=0.0,
            target0_attackable=False,
            target1_health=100.0,
            target1_attackable=True,
        )
        wave2 = ScheduledActionPlan(
            at_or_after_ms=0,
            target_index=1,
            off_gcd_actions=(BURST,),
            guard=ObservableCausalGuardV1(
                target_index=1,
                target_hp_pct_gte=35,
                target_attackable_is=True,
                action_ready=BURST,
                false_semantics=SKIP_PLAN,
            ),
        )
        _execute_plan(
            bridge,
            bridge.current,
            wave2,
            root_time_ms=0,
            step_index=1,
            receipts=receipts,
            result_bearing_action_refs=frozenset(),
        )
        self.assertEqual(bridge.cast_actions, [BURST])
        self.assertFalse(bridge.burst_available)

    def test_common_expansion_keeps_only_single_action_ready_skip_plan(self):
        guard = ObservableCausalGuardV1(
            target_index=0,
            target_hp_pct_gte=35,
            target_attackable_is=True,
            action_ready=BURST,
            false_semantics=SKIP_PLAN,
        )
        state = {
            "time_ms": 10_000,
            "damage_done": 0.0,
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "maximum_health": 100.0,
                        "current_health": 80.0,
                        "dead": False,
                        "attackable": True,
                    }
                ]
            },
        }
        ready = ScheduleReplayOutcomeV1(
            1,
            ReplayStatusV1.FRONTIER,
            state,
            available_actions=(
                _available(A),
                _available(BURST, triggers_gcd=False),
            ),
        )
        spent = ScheduleReplayOutcomeV1(
            2,
            ReplayStatusV1.FRONTIER,
            state,
            available_actions=(
                _available(A),
                _available(
                    BURST,
                    legal=False,
                    ready_in_ms=120_000,
                    triggers_gcd=False,
                ),
            ),
        )
        expansions = _common_expansions(
            _BeamNodeV1((), (ready, spent), 0.0),
            action_guides=(),
            equipment_actions=(),
            max_off_gcd_actions=1,
            max_prefix_permutations=8,
            wait_ms=100,
            guard_options=(guard,),
        )
        conditional_burst = [
            plan
            for plan in expansions
            if plan.off_gcd_actions == (BURST,) and plan.guard == guard
        ]
        self.assertTrue(conditional_burst)
        self.assertTrue(
            all(plan.conditional_prefix_only for plan in conditional_burst)
        )
        self.assertFalse(
            any(
                plan.off_gcd_actions == (BURST,) and plan.guard is None
                for plan in expansions
            )
        )

    def test_conditional_common_cannot_bypass_a_seed_target_gate(self):
        guard = ObservableCausalGuardV1(
            target_index=0,
            target_attackable_is=True,
            action_ready=BURST,
            false_semantics=SKIP_PLAN,
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            target_index=0,
            off_gcd_actions=(BURST,),
            guard=guard,
        )
        state = {"time_ms": 0, "damage_done": 0.0}
        outcomes = (
            ScheduleReplayOutcomeV1(
                1,
                ReplayStatusV1.FRONTIER,
                state,
                available_actions=(_available(BURST, triggers_gcd=False),),
                target_gate=WaveTargetGateDecisionV1(
                    "stage-a", (0,), (0, 1)
                ),
            ),
            ScheduleReplayOutcomeV1(
                2,
                ReplayStatusV1.FRONTIER,
                state,
                available_actions=(_available(BURST, triggers_gcd=False),),
                target_gate=WaveTargetGateDecisionV1(
                    "stage-b", (1,), (0, 1)
                ),
            ),
        )

        self.assertFalse(_is_conditionally_common_skip_plan_v1(plan, outcomes))

    def test_all_seed_zero_time_skip_is_rejected_as_no_progress(self):
        guard = ObservableCausalGuardV1(
            action_ready=BURST,
            false_semantics=SKIP_PLAN,
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=10_000,
            off_gcd_actions=(BURST,),
            guard=guard,
        )
        prior = tuple(
            ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {"time_ms": 10_000, "damage_done": 0.0},
                available_actions=(
                    _available(
                        BURST,
                        legal=False,
                        ready_in_ms=100_000,
                        triggers_gcd=False,
                    ),
                ),
            )
            for seed in (1, 2)
        )
        skipped = tuple(
            ScheduleReplayOutcomeV1(
                seed,
                ReplayStatusV1.FRONTIER,
                {"time_ms": 10_000, "damage_done": 0.0},
                available_actions=(
                    _available(
                        BURST,
                        legal=False,
                        ready_in_ms=100_000,
                        triggers_gcd=False,
                    ),
                ),
                receipts=(
                    {
                        "step_index": 3,
                        "kind": "GUARD_FALSE_PLAN_SKIPPED",
                    },
                ),
            )
            for seed in (1, 2)
        )
        self.assertTrue(
            _all_live_seeds_skipped_conditional_prefix_v1(
                prior,
                skipped,
                plan=plan,
                step_index=3,
            )
        )


class NativeDynamicV4ScheduleReplayV1Tests(unittest.TestCase):
    def _case(self, *, precombat=None):
        case = SimpleNamespace(
            request={"raid": "fixture"},
            dynamic_load=SimpleNamespace(
                config=SimpleNamespace(
                    retarget_mode=REQUIRED_RETARGET_MODE_V1
                )
            ),
        )
        if precombat is not None:
            case.precombat = precombat
        return case

    @staticmethod
    def _state(*, selected=0, add_dead=False, finished=False):
        observations = {
            0: ObservedTargetStateV1(True, True, False),
            1: ObservedTargetStateV1(
                True,
                not add_dead,
                add_dead,
            ),
        }
        return {
            "time_ms": 0,
            "damage_done": 1.0 if finished else 0.0,
            "needs_input": not finished,
            "finished": finished,
            "target_index": selected,
            "observations": observations,
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": index,
                        "dead": observation.dead,
                        "attackable": observation.attackable,
                    }
                    for index, observation in observations.items()
                ]
            },
        }

    @staticmethod
    def _gate():
        return ReactiveBossAddsTargetGateV1(
            boss_target_index=0,
            add_target_indexes=(1,),
            observation_provider=lambda state: state["observations"],
        )

    def test_v4_loader_reaches_frontier_with_reactive_add_gate(self):
        calls = []
        initial = self._state()

        class Bridge:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def load_dynamic_v4(self, request, seed, config):
                calls.append((request, seed, config))
                return SimpleNamespace(state=initial)

            def actions(self):
                return (_available(HIT),)

        replay = NativeDynamicV4ScheduleReplayV1(
            Bridge,
            lambda seed: self._case(),
            target_gate=self._gate(),
        )

        outcome = replay.replay(37, ())

        self.assertIs(ReplayStatusV1.FRONTIER, outcome.status)
        self.assertEqual(1, len(calls))
        self.assertEqual(37, calls[0][1])
        self.assertIsNotNone(outcome.target_gate)
        self.assertEqual(
            REACTIVE_ADDS_STAGE_ID_V1,
            outcome.target_gate.stage_id,
        )
        self.assertEqual((1,), outcome.target_gate.direct_target_indexes)

    def test_v4_replay_preserves_result_receipt_and_complete_status(self):
        initial = self._state()
        finished = self._state(selected=1, add_dead=True, finished=True)

        class Bridge:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def load_dynamic_v4(self, request, seed, config):
                return SimpleNamespace(state=initial)

            def actions(self):
                return (
                    AvailableAction(
                        0,
                        HIT,
                        "hit",
                        True,
                        0,
                        True,
                        result_bearing=True,
                    ),
                )

            def set_target(self, target_index):
                state = NativeDynamicV4ScheduleReplayV1Tests._state(
                    selected=target_index
                )
                return SetTargetResult(
                    True,
                    target_index,
                    False,
                    True,
                    state,
                )

            def act(self, action, *, attempt_id=None):
                self.attempt_id = attempt_id
                return ActResult(True, True, True, False, finished)

        bridge = Bridge()
        replay = NativeDynamicV4ScheduleReplayV1(
            lambda: bridge,
            lambda seed: self._case(),
            target_gate=self._gate(),
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            target_index=1,
            gcd_action=HIT,
        )

        outcome = replay.replay(41, (plan,))

        self.assertIs(ReplayStatusV1.COMPLETE, outcome.status)
        action_receipt = next(
            row for row in outcome.receipts if row.get("action") == HIT.to_wire()
        )
        self.assertEqual(
            "schedule-step-0:operation-2",
            action_receipt["attempt_id"],
        )
        self.assertEqual(action_receipt["attempt_id"], bridge.attempt_id)
        self.assertTrue(action_receipt["consumes_decision"])

    def test_v4_precombat_rejects_bridge_without_v4_precombat_loader(self):
        load_calls = []

        class Bridge:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def load_dynamic_v4(self, request, seed, config):
                load_calls.append((request, seed, config))
                return SimpleNamespace(state={})

        replay = NativeDynamicV4ScheduleReplayV1(
            Bridge,
            lambda seed: self._case(precombat={"pull_time_ms": 3_000}),
        )

        outcome = replay.replay(43, ())

        self.assertIs(ReplayStatusV1.INVALID, outcome.status)
        self.assertIn(
            "bridge does not support load_dynamic_v4_precombat",
            outcome.invalid_reason,
        )
        self.assertEqual([], load_calls)


if __name__ == "__main__":
    unittest.main()
