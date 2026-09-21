from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.causal_action_program_v1 import ProgramDecisionV1
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import (
    ActionPlanBranchV8,
    BLOODTHIRST,
    CLEAVE,
    CatActionPlanBranchSessionV8,
    CatDecisionPointV8,
    DEATH_WISH,
    EXECUTE,
    HEROIC_STRIKE,
    RECKLESSNESS,
    TURTLE_SLAM,
    WHIRLWIND,
    enumerate_cat_relative_action_plans_v8,
    run_upper_kara_cat_action_plan_teacher_v8,
    select_cat_branch_points_v8,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_heterogeneous_two_wave_remote_v7 import (
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp


def _available(
    index: int,
    action: ActionRef,
    *,
    legal: bool = True,
    ready_in_ms: int = 0,
    triggers_gcd: bool = True,
) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=action,
        label=str(action.to_wire()),
        legal=legal,
        ready_in_ms=ready_in_ms,
        triggers_gcd=triggers_gcd,
        result_bearing=triggers_gcd,
    )


def _observation(
    simulator_indexes: tuple[int, ...] = (0, 1),
    *,
    queued_swing: str = "KEEP",
    time_ms: int = 3_000,
) -> CausalLiveStateProjectionV1:
    rows = [
        {
            "target_index": policy_index,
            "attackable": True,
            "dead": False,
            "current_health": 1_000.0,
            "maximum_health": 1_000.0,
        }
        for policy_index in range(len(simulator_indexes))
    ]
    return CausalLiveStateProjectionV1(
        state={
            "time_ms": time_ms,
            "target_index": 0,
            "queued_swing": queued_swing,
            "dynamic_target_semantics": {"targets": rows},
        },
        policy_to_simulator_target_index=simulator_indexes,
        visibility_cutoff_ms=time_ms,
    )


def _cat_decision() -> ProgramDecisionV1:
    return ProgramDecisionV1(
        queue_op=QueueLaneOp.SET,
        queue_action=CLEAVE,
        gcd_action=BLOODTHIRST,
    )


class CatActionPlanTeacherV8UnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actions = (
            _available(0, BLOODTHIRST),
            _available(1, WHIRLWIND, ready_in_ms=700),
            _available(2, TURTLE_SLAM),
            _available(3, EXECUTE, legal=False),
            _available(4, DEATH_WISH),
            _available(5, RECKLESSNESS),
            _available(6, HEROIC_STRIKE, triggers_gcd=False),
            _available(7, CLEAVE, triggers_gcd=False),
        )

    def test_enumeration_uses_only_current_visible_ready_actions(self) -> None:
        point = CatDecisionPointV8(
            4,
            _observation(),
            self.actions,
            _cat_decision(),
        )
        plans = enumerate_cat_relative_action_plans_v8(point)
        self.assertTrue(plans)
        gcds = {row.gcd_action for row in plans if row.gcd_action is not None}
        queues = {row.queue_action for row in plans if row.queue_action is not None}
        self.assertIn(TURTLE_SLAM, gcds)
        self.assertIn(DEATH_WISH, gcds)
        self.assertNotIn(RECKLESSNESS, gcds)
        self.assertNotIn(WHIRLWIND, gcds)
        self.assertNotIn(EXECUTE, gcds)
        self.assertEqual({HEROIC_STRIKE, CLEAVE}, queues)
        self.assertLessEqual({row.target_index for row in plans}, {None, 0, 1})
        for row in plans:
            if row.target_index is not None:
                self.assertIsNone(row.gcd_action)
                self.assertEqual(1, row.wait_ms)
                self.assertEqual(QueueLaneOp.KEEP, row.queue_op)
                self.assertEqual((), row.optional_off_gcd_prefixes)
        self.assertNotIn(_cat_decision(), plans)

    def test_queue_cancel_is_only_offered_for_an_active_queue(self) -> None:
        inactive = CatDecisionPointV8(
            0, _observation(), self.actions, _cat_decision()
        )
        active = CatDecisionPointV8(
            0,
            _observation(queued_swing="CLEAVE"),
            self.actions,
            _cat_decision(),
        )
        self.assertNotIn(
            QueueLaneOp.CANCEL,
            {row.queue_op for row in enumerate_cat_relative_action_plans_v8(inactive)},
        )
        self.assertIn(
            QueueLaneOp.CANCEL,
            {row.queue_op for row in enumerate_cat_relative_action_plans_v8(active)},
        )

    def test_one_branch_then_same_cat_session_continues(self) -> None:
        class FakeCat:
            def __init__(self) -> None:
                self.last_gcd_action = "before"
                self.calls = 0
                self.seen_last_gcd_actions = []

            def __call__(self, observation, available):
                del observation, available
                self.calls += 1
                self.seen_last_gcd_actions.append(self.last_gcd_action)
                self.last_gcd_action = "warrior.bloodthirst"
                return _cat_decision()

        point = CatDecisionPointV8(
            0,
            _observation(),
            (
                _available(0, BLOODTHIRST),
                _available(1, WHIRLWIND),
                _available(2, CLEAVE, triggers_gcd=False),
            ),
            _cat_decision(),
        )
        replacement = next(
            row
            for row in enumerate_cat_relative_action_plans_v8(point)
            if row.gcd_action == WHIRLWIND
            and row.queue_op is QueueLaneOp.SET
            and row.queue_action == CLEAVE
            and row.target_index is None
        )
        cat = FakeCat()
        session = CatActionPlanBranchSessionV8(
            cat, ActionPlanBranchV8(0, replacement)
        )
        first = session(_observation(), point.available_actions)
        self.assertEqual(WHIRLWIND, first.gcd_action)
        self.assertEqual("before", cat.last_gcd_action)
        second = session(_observation(time_ms=4_500), point.available_actions)
        self.assertEqual(BLOODTHIRST, second.gcd_action)
        self.assertEqual(2, cat.calls)
        self.assertEqual(
            ["before", "warrior.whirlwind"],
            cat.seen_last_gcd_actions,
        )
        self.assertEqual(1, len(session.interventions))

    def test_state_selection_balances_wave_strata_without_rewards(self) -> None:
        available = (
            _available(0, BLOODTHIRST),
            _available(1, TURTLE_SLAM),
            _available(2, CLEAVE, triggers_gcd=False),
        )
        points = (
            CatDecisionPointV8(0, _observation((0, 1)), available, _cat_decision()),
            CatDecisionPointV8(1, _observation((0,)), available, _cat_decision()),
            CatDecisionPointV8(2, _observation((2,)), available, _cat_decision()),
            CatDecisionPointV8(3, _observation((0, 1)), available, _cat_decision()),
        )
        selected = select_cat_branch_points_v8(points, max_states=3)
        self.assertEqual(
            (("WAVE_1", 2), ("WAVE_1", 1), ("WAVE_2", 1)),
            tuple(row.stratum for row in selected),
        )


@unittest.skipUnless(
    DEFAULT_EXACT_BRIDGE.is_file(),
    "the exact native bridge is not built",
)
class CatActionPlanTeacherV8NativeSmokeTests(unittest.TestCase):
    def test_one_real_branch_reaches_a_terminal_label(self) -> None:
        case = build_upper_kara_heterogeneous_two_wave_burst_case_v7(
            1_120_001,
            build_id="live_bonereaver",
            loadout_id="contra_turtle_burst__mighty_rage",
            first_wave_arrival_ms=0,
        )
        result = run_upper_kara_cat_action_plan_teacher_v8(
            case,
            build_id="live_bonereaver",
            loadout_id="contra_turtle_burst__mighty_rage",
            simulator_seed=1_129_991,
            max_states=1,
            max_plans_per_state=2,
            branch_workers=2,
            max_decisions=512,
        )
        self.assertEqual(
            "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING", result["status"]
        )
        self.assertEqual("COMPLETED", result["baseline_terminal"]["status"])
        self.assertEqual(1_120_001, result["master_seed"])
        self.assertEqual(1_129_991, result["simulator_seed"])
        self.assertEqual(2, result["independent_action_plan_branch_count"])
        self.assertEqual(2, result["branch_workers"])
        self.assertEqual(0, result["branches"][0]["candidate_plan_index"])
        self.assertEqual(1, result["branches"][1]["candidate_plan_index"])
        self.assertGreaterEqual(
            result["candidate_action_plan_counts"][0][
                "candidate_action_plan_count"
            ],
            1,
        )
        self.assertEqual(2, result["completed_teacher_label_count"])
        self.assertTrue(
            result["branches"][0]["strict_single_intervention_verified"]
        )


if __name__ == "__main__":
    unittest.main()
