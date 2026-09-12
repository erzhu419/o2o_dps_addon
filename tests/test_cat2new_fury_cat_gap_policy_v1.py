from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import unittest

from o2o_dps.cat2new_fury_cat_gap_policy_v1 import (
    Cat2NewFuryCatGapPolicyV1Error,
    FuryCatGapControllerV1,
    FuryCatGapPolicyParametersV1,
    PARAMETER_AXES,
    build_cat_gap_policy_v1,
)
from o2o_dps.cat2new_candidate_feedback_loop_v6 import POLICY_INPUT_SCHEMA_V6
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.expert_policy import SwingQueueOp, WAIT_ACTION
from o2o_dps.fury_cat_gap_search_plan_v1 import (
    PARAMETER_AXES as PLANNED_PARAMETER_AXES,
)
from o2o_dps.fury_expert_adapters import (
    BLOODRAGE,
    BLOODTHIRST,
    EXECUTE,
    HAMSTRING,
    SLAM,
    WHIRLWIND,
    FuryExpertState,
    WeaponMode,
)


BASE_PARAMETERS = {
    "heroic_strike_base_rage": 35,
    "cleave_base_rage": 35,
    "queue_cancel_margin_rage": 8,
    "primary_cooldown_reserve_window_ms": 1400,
    "bloodrage_trigger_below_rage": 30,
    "single_target_priority": "BLOODTHIRST_FIRST",
    "multi_target_priority": "WHIRLWIND_FIRST",
    "hamstring_min_rage": 10,
    "hamstring_min_primary_gap_ms": 1400,
    "slam_min_swing_remaining_ms": 2000,
    "slam_min_primary_gap_ms": 1400,
    "execute_reserve_rage": 30,
    "wait_ms": 100,
}


def _parameters(**updates):
    value = dict(BASE_PARAMETERS)
    value.update(updates)
    return FuryCatGapPolicyParametersV1.from_mapping(value)


def _decision(parameters, **state_updates):
    values = {
        "rage": 0,
        "target_health_pct": 50,
        "weapon_mode": WeaponMode.DUAL_WIELD,
        "gcd_ready": True,
        "bloodthirst_ready_in_s": 9,
        "whirlwind_ready_in_s": 9,
        "bloodrage_ready": False,
        "queued_swing": SwingQueueOp.KEEP,
    }
    values.update(state_updates)
    state = FuryExpertState(**values)
    return FuryCatGapControllerV1(parameters).propose(state)


def _action(ref, *, ready_in_ms=0, triggers_gcd=True):
    return {
        "index": 1,
        "action": {
            "spell_id": ref.spell_id,
            "item_id": ref.item_id,
            "other_id": ref.other_id,
            "tag": ref.tag,
        },
        "label": "fixture",
        "legal": True,
        "ready_in_ms": ready_in_ms,
        "triggers_gcd": triggers_gcd,
    }


def _policy_input(policy):
    actions = [
        _action(QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE], triggers_gcd=False),
        _action(QUEUE_REFS[SwingQueueOp.CLEAVE], triggers_gcd=False),
        _action(
            ACTION_KEY_TO_REF["warrior.bloodrage"],
            ready_in_ms=5000,
            triggers_gcd=False,
        ),
    ]
    actions.extend(
        _action(ACTION_KEY_TO_REF[key], ready_in_ms=5000)
        for key in (
            "warrior.battle_shout",
            "warrior.bloodthirst",
            "warrior.whirlwind",
            "warrior.execute",
            "warrior.slam",
            "warrior.hamstring",
        )
    )
    return {
        "schema": POLICY_INPUT_SCHEMA_V6,
        "policy_id": policy.policy_id,
        "decision_index": 1,
        "run_binding_content_sha256": "a" * 64,
        "live_state": {
            "time_ms": 0,
            "needs_input": True,
            "finished": False,
            "target_index": 0,
            "power": {"type": "rage", "current": 0, "maximum": 100},
            "target_health_percent": 100,
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 800,
            "mh_swing_duration_ms": 2400,
            "oh_swing_remaining_ms": None,
            "auras": [
                {
                    "action": {"spell_id": 2458},
                    "label": "Berserker Stance",
                    "remaining_ms": -1,
                    "stacks": 0,
                },
                {
                    "action": {"spell_id": 25289},
                    "label": "Battle Shout",
                    "remaining_ms": 120000,
                    "stacks": 0,
                },
            ],
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "attackable": True,
                        "dead": False,
                        "current_health": 1000,
                        "effective_armor": 1721,
                    }
                ]
            },
        },
        "available_actions": actions,
        "pending_attempt_ids": [],
        "target_boundary": {},
        "optimizer_parameters": asdict(policy.config),
        "historical_prior": {},
    }


class Cat2NewFuryCatGapPolicyV1Tests(unittest.TestCase):
    def test_policy_axes_are_exactly_the_frozen_search_axes(self) -> None:
        self.assertEqual(PLANNED_PARAMETER_AXES, PARAMETER_AXES)
        self.assertEqual(13, len(PARAMETER_AXES))

    def test_factory_requires_canonical_candidate_identity_and_exact_fields(self) -> None:
        parameters = _parameters()
        policy = build_cat_gap_policy_v1(
            parameters.candidate_id,
            asdict(parameters),
        )
        self.assertEqual(parameters.candidate_id, policy.candidate_id)
        self.assertEqual(parameters.parameter_sha256, policy.parameter_sha256)
        with self.assertRaisesRegex(
            Cat2NewFuryCatGapPolicyV1Error, "candidate_id"
        ):
            build_cat_gap_policy_v1("cat-gap-v1-wrong", asdict(parameters))
        missing = asdict(parameters)
        missing.pop("wait_ms")
        with self.assertRaisesRegex(
            Cat2NewFuryCatGapPolicyV1Error, "exact 13-axis"
        ):
            build_cat_gap_policy_v1(parameters.candidate_id, missing)

    def test_policy_decision_reports_exact_candidate_and_all_axes(self) -> None:
        parameters = _parameters(wait_ms=75)
        policy = build_cat_gap_policy_v1(
            parameters.candidate_id,
            asdict(parameters),
        )
        intent = policy.decide(_policy_input(policy))
        self.assertEqual("WAIT", intent.operations[-1]["intent"])
        self.assertEqual(75, intent.operations[-1]["arguments"]["wait_ms"])
        self.assertEqual(parameters.candidate_id, intent.policy_metadata["candidate_id"])
        self.assertEqual(
            parameters.parameter_sha256,
            intent.policy_metadata["parameter_sha256"],
        )
        self.assertEqual(asdict(parameters), intent.policy_metadata["parameters"])
        self.assertTrue(intent.policy_metadata["simulator_only"])
        self.assertFalse(intent.policy_metadata["deployment_allowed"])

    def test_policy_rejects_runtime_profile_mismatch(self) -> None:
        parameters = _parameters()
        policy = build_cat_gap_policy_v1(
            parameters.candidate_id,
            asdict(parameters),
        )
        value = _policy_input(policy)
        value["optimizer_parameters"] = {}
        with self.assertRaisesRegex(
            Cat2NewFuryCatGapPolicyV1Error, "optimizer_parameters"
        ):
            policy.decide(value)
        scalar_drift = _policy_input(policy)
        scalar_drift["optimizer_parameters"] = deepcopy(
            scalar_drift["optimizer_parameters"]
        )
        scalar_drift["optimizer_parameters"]["parameters"][
            "heroic_strike_base_rage"
        ] = 35.0
        with self.assertRaisesRegex(
            Cat2NewFuryCatGapPolicyV1Error, "optimizer_parameters"
        ):
            policy.decide(scalar_drift)

    def test_heroic_strike_base_rage_changes_single_target_queue(self) -> None:
        state = dict(rage=50, gcd_ready=False)
        self.assertEqual(
            SwingQueueOp.HEROIC_STRIKE,
            _decision(_parameters(heroic_strike_base_rage=35), **state).swing_queue,
        )
        self.assertEqual(
            SwingQueueOp.KEEP,
            _decision(_parameters(heroic_strike_base_rage=75), **state).swing_queue,
        )

    def test_cleave_base_rage_changes_multi_target_queue(self) -> None:
        state = dict(rage=50, nearby_enemies=3, gcd_ready=False)
        self.assertEqual(
            SwingQueueOp.CLEAVE,
            _decision(_parameters(cleave_base_rage=35), **state).swing_queue,
        )
        self.assertEqual(
            SwingQueueOp.KEEP,
            _decision(_parameters(cleave_base_rage=75), **state).swing_queue,
        )

    def test_queue_cancel_margin_changes_existing_queue_guard(self) -> None:
        state = dict(
            rage=40,
            queued_swing=SwingQueueOp.HEROIC_STRIKE,
            gcd_ready=False,
        )
        self.assertEqual(
            SwingQueueOp.CANCEL,
            _decision(
                _parameters(
                    heroic_strike_base_rage=50,
                    queue_cancel_margin_rage=4,
                ),
                **state,
            ).swing_queue,
        )
        self.assertEqual(
            SwingQueueOp.KEEP,
            _decision(
                _parameters(
                    heroic_strike_base_rage=50,
                    queue_cancel_margin_rage=16,
                ),
                **state,
            ).swing_queue,
        )

    def test_primary_reserve_window_changes_queue_guard(self) -> None:
        state = dict(
            rage=40,
            bloodthirst_ready_in_s=1.2,
            whirlwind_ready_in_s=9,
            gcd_ready=False,
        )
        self.assertEqual(
            SwingQueueOp.HEROIC_STRIKE,
            _decision(
                _parameters(primary_cooldown_reserve_window_ms=800),
                **state,
            ).swing_queue,
        )
        self.assertEqual(
            SwingQueueOp.KEEP,
            _decision(
                _parameters(primary_cooldown_reserve_window_ms=1800),
                **state,
            ).swing_queue,
        )

    def test_bloodrage_threshold_changes_off_gcd_lane(self) -> None:
        state = dict(rage=20, bloodrage_ready=True, gcd_ready=False)
        self.assertNotIn(
            BLOODRAGE,
            _decision(
                _parameters(bloodrage_trigger_below_rage=15), **state
            ).off_gcd,
        )
        self.assertIn(
            BLOODRAGE,
            _decision(
                _parameters(bloodrage_trigger_below_rage=25), **state
            ).off_gcd,
        )

    def test_single_target_priority_changes_primary_action(self) -> None:
        state = dict(rage=100, bloodthirst_ready_in_s=0, whirlwind_ready_in_s=0)
        self.assertEqual(
            BLOODTHIRST,
            _decision(
                _parameters(single_target_priority="BLOODTHIRST_FIRST"), **state
            ).gcd,
        )
        self.assertEqual(
            WHIRLWIND,
            _decision(
                _parameters(single_target_priority="WHIRLWIND_FIRST"), **state
            ).gcd,
        )

    def test_multi_target_priority_changes_primary_action(self) -> None:
        state = dict(
            rage=100,
            nearby_enemies=3,
            bloodthirst_ready_in_s=0,
            whirlwind_ready_in_s=0,
        )
        self.assertEqual(
            BLOODTHIRST,
            _decision(
                _parameters(multi_target_priority="BLOODTHIRST_FIRST"), **state
            ).gcd,
        )
        self.assertEqual(
            WHIRLWIND,
            _decision(
                _parameters(multi_target_priority="WHIRLWIND_FIRST"), **state
            ).gcd,
        )

    def test_hamstring_min_rage_changes_filler(self) -> None:
        state = dict(rage=20, bloodthirst_ready_in_s=3, whirlwind_ready_in_s=3)
        self.assertEqual(
            HAMSTRING,
            _decision(_parameters(hamstring_min_rage=10), **state).gcd,
        )
        self.assertEqual(
            WAIT_ACTION,
            _decision(_parameters(hamstring_min_rage=30), **state).gcd,
        )

    def test_hamstring_primary_gap_changes_filler(self) -> None:
        state = dict(rage=100, bloodthirst_ready_in_s=1.5, whirlwind_ready_in_s=1.5)
        self.assertEqual(
            HAMSTRING,
            _decision(
                _parameters(hamstring_min_primary_gap_ms=1400), **state
            ).gcd,
        )
        self.assertEqual(
            WAIT_ACTION,
            _decision(
                _parameters(hamstring_min_primary_gap_ms=1600), **state
            ).gcd,
        )

    def test_slam_swing_window_changes_two_hand_action(self) -> None:
        state = dict(
            rage=15,
            weapon_mode=WeaponMode.TWO_HAND,
            mainhand_swing_remaining_s=2.0,
            bloodthirst_ready_in_s=3,
            whirlwind_ready_in_s=3,
        )
        self.assertEqual(
            SLAM,
            _decision(
                _parameters(
                    slam_min_swing_remaining_ms=1600,
                    hamstring_min_rage=70,
                ),
                **state,
            ).gcd,
        )
        self.assertEqual(
            WAIT_ACTION,
            _decision(
                _parameters(
                    slam_min_swing_remaining_ms=2200,
                    hamstring_min_rage=70,
                ),
                **state,
            ).gcd,
        )

    def test_slam_primary_gap_changes_two_hand_action(self) -> None:
        state = dict(
            rage=15,
            weapon_mode=WeaponMode.TWO_HAND,
            mainhand_swing_remaining_s=2.6,
            bloodthirst_ready_in_s=1.5,
            whirlwind_ready_in_s=1.5,
        )
        self.assertEqual(
            SLAM,
            _decision(
                _parameters(
                    slam_min_primary_gap_ms=1400,
                    hamstring_min_rage=70,
                ),
                **state,
            ).gcd,
        )
        self.assertEqual(
            WAIT_ACTION,
            _decision(
                _parameters(
                    slam_min_primary_gap_ms=1600,
                    hamstring_min_rage=70,
                ),
                **state,
            ).gcd,
        )

    def test_execute_reserve_changes_execute_phase_action(self) -> None:
        state = dict(
            rage=55,
            target_health_pct=15,
            bloodthirst_ready_in_s=0,
            whirlwind_ready_in_s=9,
        )
        self.assertEqual(
            BLOODTHIRST,
            _decision(_parameters(execute_reserve_rage=20), **state).gcd,
        )
        self.assertEqual(
            EXECUTE,
            _decision(_parameters(execute_reserve_rage=40), **state).gcd,
        )

    def test_every_adjacent_execute_reserve_level_has_a_reachable_boundary(self) -> None:
        levels = [0, 10, 20, 30, 40]
        for lower, upper in zip(levels, levels[1:]):
            with self.subTest(lower=lower, upper=upper):
                state = dict(
                    rage=30 + lower + 5,
                    target_health_pct=15,
                    bloodthirst_ready_in_s=0,
                    whirlwind_ready_in_s=9,
                )
                self.assertEqual(
                    BLOODTHIRST,
                    _decision(
                        _parameters(execute_reserve_rage=lower), **state
                    ).gcd,
                )
                self.assertEqual(
                    EXECUTE,
                    _decision(
                        _parameters(execute_reserve_rage=upper), **state
                    ).gcd,
                )

    def test_wait_ms_changes_idle_decision_duration(self) -> None:
        state = dict(rage=0, bloodthirst_ready_in_s=3, whirlwind_ready_in_s=3)
        self.assertEqual(50, _decision(_parameters(wait_ms=50), **state).wait_ms)
        self.assertEqual(150, _decision(_parameters(wait_ms=150), **state).wait_ms)

    def test_boolean_axis_value_is_rejected(self) -> None:
        value = dict(BASE_PARAMETERS)
        value["wait_ms"] = True
        with self.assertRaisesRegex(
            Cat2NewFuryCatGapPolicyV1Error, "outside the frozen axis"
        ):
            FuryCatGapPolicyParametersV1.from_mapping(value)
        value = dict(BASE_PARAMETERS)
        value["heroic_strike_base_rage"] = 35.0
        with self.assertRaisesRegex(
            Cat2NewFuryCatGapPolicyV1Error, "outside the frozen axis"
        ):
            FuryCatGapPolicyParametersV1.from_mapping(value)


if __name__ == "__main__":
    unittest.main()
