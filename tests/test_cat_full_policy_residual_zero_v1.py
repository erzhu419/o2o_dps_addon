from __future__ import annotations

from dataclasses import replace
import unittest

from o2o_dps.cat_full_policy_residual_zero_v1 import CatFullPolicyResidualZeroV1
from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    CatInventoryItemV4,
)
from o2o_dps.expert_policy import StanceOp, SwingQueueOp
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode


class CatFullPolicyResidualZeroV1Tests(unittest.TestCase):
    def test_complete_decision_matches_current_cat_full_policy(self) -> None:
        combat = FuryExpertState(
            rage=80.0,
            target_health_pct=50.0,
            weapon_mode=WeaponMode.TWO_HAND,
            current_stance=StanceOp.BERSERKER,
            bloodthirst_ready_in_s=0.0,
            whirlwind_ready_in_s=0.0,
        )
        base = CatFuryFullPolicyStateV4(combat=combat)
        cases = {
            "ready_two_hand": base,
            "dual_wield": replace(base, combat=replace(combat, weapon_mode=WeaponMode.DUAL_WIELD)),
            "execute": replace(base, combat=replace(combat, target_health_pct=19.0)),
            "no_target": replace(base, combat=replace(combat, target_exists=False)),
            "banished": replace(base, target_banished=True),
            "autoattack": replace(base, autoattack_active=False),
            "battle_shout": replace(base, combat=replace(combat, has_battle_shout=False, battle_shout_remaining_s=0.0)),
            "trinkets": replace(
                base,
                upper_trinket_supported=True,
                upper_trinket_cooldown_s=0.0,
                lower_trinket_supported=True,
                lower_trinket_cooldown_s=0.0,
            ),
            "health_items": replace(
                base,
                player_health_pct=10.0,
                inventory_items=(CatInventoryItemV4("特效治疗石", 0, 1),),
            ),
        }
        cat = CatFuryFullPolicyAdapterV4()
        residual = CatFullPolicyResidualZeroV1()
        self.assertIs(type(residual.controller), CatFuryFullPolicyAdapterV4)
        for name, state in cases.items():
            with self.subTest(name=name):
                expected = cat.propose(state)
                actual = residual.propose(state)
                self.assertEqual(actual, expected)
                self.assertEqual(actual.raw_sink_order, expected.raw_sink_order)
                self.assertEqual(actual.to_dict(), expected.to_dict())

    def test_two_hand_80_rage_bt_ww_ready_does_not_queue_hs(self) -> None:
        state = CatFuryFullPolicyStateV4(
            combat=FuryExpertState(
                rage=80.0,
                target_health_pct=50.0,
                weapon_mode=WeaponMode.TWO_HAND,
                current_stance=StanceOp.BERSERKER,
                bloodthirst_ready_in_s=0.0,
                whirlwind_ready_in_s=0.0,
                nearby_enemies=1,
            )
        )
        cat = CatFuryFullPolicyAdapterV4().propose(state)
        zero = CatFullPolicyResidualZeroV1().propose(state)
        self.assertEqual(cat.swing_queue, SwingQueueOp.KEEP)
        self.assertFalse(any(sink.channel == "swing_queue" for sink in cat.raw_sink_order))
        self.assertEqual(zero, cat)


if __name__ == "__main__":
    unittest.main()
