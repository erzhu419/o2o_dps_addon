from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.cat2new_fury_parametric_policy_v1 import (
    Cat2NewFuryParametricPolicyConfigV1,
    Cat2NewFuryParametricPolicyV1,
    Cat2NewFuryParametricPolicyV1Error,
    _warrior_flurry_active,
    build_policy,
    build_policy_bt_hamstring_cat_slam,
    build_policy_bt_hamstring_no_slam,
    build_policy_bt_wait_cat_slam,
    build_policy_bt_wait_no_slam,
    build_policy_ww_hamstring_cat_slam,
    build_policy_ww_hamstring_no_slam,
    build_policy_ww_wait_cat_slam,
    build_policy_ww_wait_no_slam,
)
from o2o_dps.cat2new_candidate_feedback_loop_v6 import POLICY_INPUT_SCHEMA_V6
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT2NEW_POLICY_ID
from o2o_dps.fury_policy_optimization_v1 import FuryPolicyParameters


def _action(ref, *, legal=True, ready_in_ms=0, triggers_gcd=True):
    return {
        "index": 1,
        "action": {
            "spell_id": ref.spell_id,
            "item_id": ref.item_id,
            "other_id": ref.other_id,
            "tag": ref.tag,
        },
        "label": "fixture",
        "legal": legal,
        "ready_in_ms": ready_in_ms,
        "triggers_gcd": triggers_gcd,
    }


def _input(
    *,
    rage=100,
    health_pct=100.0,
    targets=1,
    battle_shout=False,
    offhand_remaining_ms=400,
    mh_remaining_ms=800,
    flurry_active=False,
):
    target_rows = [
        {
            "target_index": index,
            "attackable": True,
            "dead": False,
            "current_health": 1000.0,
            "effective_armor": 1721.0,
        }
        for index in range(targets)
    ]
    auras = [
        {
            "action": {"spell_id": 2458},
            "label": "Berserker Stance",
            "remaining_ms": -1,
            "stacks": 0,
        }
    ]
    if battle_shout:
        auras.append(
            {
                "action": {"spell_id": 25289},
                "label": "Battle Shout",
                "remaining_ms": 120000,
                "stacks": 0,
            }
        )
    if flurry_active:
        auras.append(
            {
                "action": {"spell_id": 12970},
                "label": "Flurry Proc (12970)",
                "remaining_ms": -1,
                "stacks": 3,
            }
        )
    actions = [
        _action(
            QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
            triggers_gcd=False,
        ),
        _action(
            QUEUE_REFS[SwingQueueOp.CLEAVE],
            triggers_gcd=False,
        ),
        _action(ACTION_KEY_TO_REF["warrior.bloodrage"], triggers_gcd=False),
        _action(ACTION_KEY_TO_REF["warrior.battle_shout"]),
        _action(ACTION_KEY_TO_REF["warrior.bloodthirst"]),
        _action(ACTION_KEY_TO_REF["warrior.whirlwind"]),
        _action(ACTION_KEY_TO_REF["warrior.execute"]),
        _action(ACTION_KEY_TO_REF["warrior.slam"]),
        _action(ACTION_KEY_TO_REF["warrior.hamstring"]),
    ]
    return {
        "schema": POLICY_INPUT_SCHEMA_V6,
        "policy_id": CAT2NEW_POLICY_ID,
        "decision_index": 1,
        "run_binding_content_sha256": "a" * 64,
        "live_state": {
            "time_ms": 0,
            "needs_input": True,
            "finished": False,
            "target_index": 0,
            "power": {"type": "rage", "current": rage, "maximum": 100},
            "target_health_percent": health_pct,
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": mh_remaining_ms,
            "mh_swing_duration_ms": 2400,
            "oh_swing_remaining_ms": offhand_remaining_ms,
            "auras": auras,
            "dynamic_target_semantics": {"targets": target_rows},
        },
        "available_actions": actions,
        "pending_attempt_ids": [],
        "target_boundary": {},
        "optimizer_parameters": {},
        "historical_prior": {},
    }


class Cat2NewFuryParametricPolicyV1Tests(unittest.TestCase):
    def test_factory_uses_runner_candidate_identity(self) -> None:
        self.assertEqual(CAT2NEW_POLICY_ID, build_policy().policy_id)

    def test_single_target_opening_queues_hs_and_refreshes_shout(self) -> None:
        intent = Cat2NewFuryParametricPolicyV1().decide(_input())
        self.assertEqual(
            [(row["lane"], row["intent"]) for row in intent.operations],
            [("swing_queue", "HEROIC_STRIKE"), ("gcd", "CAST_ACTION")],
        )
        self.assertEqual(
            "warrior.battle_shout",
            intent.operations[1]["arguments"]["action_key"],
        )

    def test_multi_target_uses_cleave_and_whirlwind(self) -> None:
        intent = Cat2NewFuryParametricPolicyV1().decide(
            _input(targets=3, battle_shout=True)
        )
        self.assertEqual("CLEAVE", intent.operations[0]["intent"])
        self.assertEqual(
            "warrior.whirlwind",
            intent.operations[1]["arguments"]["action_key"],
        )

    def test_two_hand_null_offhand_uses_cat_timing_slam_window(self) -> None:
        policy = Cat2NewFuryParametricPolicyV1(
            Cat2NewFuryParametricPolicyConfigV1(
                parameters=FuryPolicyParameters(
                    two_hand_slam_mode="CAT_TIMING",
                    use_death_wish=False,
                )
            )
        )
        intent = policy.decide(
            _input(
                rage=65,
                battle_shout=True,
                offhand_remaining_ms=None,
                mh_remaining_ms=2324,
                flurry_active=True,
            )
        )
        self.assertEqual(
            "warrior.slam", intent.operations[-1]["arguments"]["action_key"]
        )

    def test_seed2_flurry_fixture_queues_heroic_strike_then_slam(self) -> None:
        intent = build_policy_ww_hamstring_cat_slam().decide(
            _input(
                rage=65,
                health_pct=74.92,
                battle_shout=True,
                offhand_remaining_ms=None,
                mh_remaining_ms=2324,
                flurry_active=True,
            )
        )
        self.assertEqual(
            [(row["lane"], row["intent"]) for row in intent.operations],
            [("swing_queue", "HEROIC_STRIKE"), ("gcd", "CAST_ACTION")],
        )
        self.assertEqual(
            "warrior.slam", intent.operations[-1]["arguments"]["action_key"]
        )
        self.assertEqual(
            45961,
            ACTION_KEY_TO_REF[
                intent.operations[-1]["arguments"]["action_key"]
            ].spell_id,
        )

    def test_all_warrior_flurry_ranks_accept_never_expires_and_not_triggers(self) -> None:
        for spell_id in range(12966, 12971):
            with self.subTest(spell_id=spell_id):
                aura = {
                    "action": {"spell_id": spell_id},
                    "label": f"Flurry Proc ({spell_id})",
                    "remaining_ms": -1,
                    "stacks": 3,
                }
                self.assertTrue(_warrior_flurry_active((aura,)))
                aura["remaining_ms"] = 0
                self.assertFalse(_warrior_flurry_active((aura,)))
        triggers = (
            {
                "action": {},
                "label": "Flurry Consume Trigger",
                "remaining_ms": -1,
                "stacks": 0,
            },
            {
                "action": {},
                "label": "Flurry Proc Trigger",
                "remaining_ms": -1,
                "stacks": 0,
            },
        )
        self.assertFalse(_warrior_flurry_active(triggers))

    def test_single_target_whirlwind_first_is_explicit(self) -> None:
        policy = Cat2NewFuryParametricPolicyV1(
            Cat2NewFuryParametricPolicyConfigV1(
                parameters=FuryPolicyParameters(
                    single_target_priority="WHIRLWIND_FIRST",
                    use_death_wish=False,
                )
            )
        )
        intent = policy.decide(_input(battle_shout=True))
        self.assertEqual(
            "warrior.whirlwind",
            intent.operations[-1]["arguments"]["action_key"],
        )

    def test_screening_factories_have_eight_unique_bound_configs(self) -> None:
        factories = (
            build_policy_bt_wait_no_slam,
            build_policy_bt_hamstring_no_slam,
            build_policy_ww_wait_no_slam,
            build_policy_ww_hamstring_no_slam,
            build_policy_bt_wait_cat_slam,
            build_policy_bt_hamstring_cat_slam,
            build_policy_ww_wait_cat_slam,
            build_policy_ww_hamstring_cat_slam,
        )
        policies = [factory() for factory in factories]
        self.assertEqual(8, len({policy._controller.expert_id for policy in policies}))
        self.assertTrue(all(policy.policy_id == CAT2NEW_POLICY_ID for policy in policies))

    def test_execute_masks_next_swing_queue(self) -> None:
        intent = Cat2NewFuryParametricPolicyV1().decide(
            _input(rage=20, health_pct=15.0, battle_shout=True)
        )
        self.assertNotIn("swing_queue", [row["lane"] for row in intent.operations])
        self.assertEqual(
            "warrior.execute", intent.operations[-1]["arguments"]["action_key"]
        )

    def test_illegal_selected_gcd_becomes_typed_wait(self) -> None:
        value = _input(rage=100, battle_shout=True)
        for row in value["available_actions"]:
            if row["action"]["spell_id"] == 23894:
                row["legal"] = False
            if row["action"]["spell_id"] == 1680:
                row["legal"] = False
        intent = Cat2NewFuryParametricPolicyV1().decide(value)
        self.assertEqual("WAIT", intent.operations[-1]["intent"])
        self.assertIn(
            "gcd:warrior.bloodthirst",
            intent.policy_metadata["masked_illegal_intents"],
        )

    def test_missing_live_action_table_fails_closed(self) -> None:
        value = deepcopy(_input())
        value.pop("available_actions")
        with self.assertRaisesRegex(
            Cat2NewFuryParametricPolicyV1Error, "available_actions"
        ):
            Cat2NewFuryParametricPolicyV1().decide(value)


if __name__ == "__main__":
    unittest.main()
