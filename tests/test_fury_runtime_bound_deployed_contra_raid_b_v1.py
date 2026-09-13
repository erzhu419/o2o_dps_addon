from __future__ import annotations

from dataclasses import replace
import unittest

from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_expert_adapters import BLOODTHIRST, EXECUTE, WHIRLWIND, WeaponMode
from o2o_dps.fury_runtime_bound_deployed_contra_raid_b_v1 import (
    EXPERT_ID,
    RuntimeBoundContraRaidBAdapterV1,
)
from tests.test_fury_contra_adapter_v2 import _state
from tests.test_fury_runtime_bound_deployed_contra_adapter_v7 import _binding


def _dual(**changes):
    state = _state(weapon_mode=WeaponMode.DUAL_WIELD, **changes)
    return state


class RuntimeBoundContraRaidBAdapterV1Tests(unittest.TestCase):
    def test_source_order_cleave_then_bloodthirst_with_current_xuanfeng_off(self):
        adapter = RuntimeBoundContraRaidBAdapterV1(_binding())
        decision = adapter.propose(_dual(
            rage=65, contra_st_s=1.0, whirlwind_ready_in_s=3.0,
        ))
        self.assertTrue(decision.valid)
        self.assertEqual(decision.expert_id, EXPERT_ID)
        self.assertEqual(decision.swing_queue, SwingQueueOp.CLEAVE)
        self.assertEqual(decision.gcd, BLOODTHIRST)
        self.assertEqual(
            [(sink.channel, sink.value) for sink in decision.raw_sink_order[-2:]],
            [("swing_queue", "顺劈斩"), ("gcd", "嗜血")],
        )
        self.assertEqual(decision.metadata["controller"], "raid_b")
        self.assertTrue(decision.metadata["complete_dual_wield_raid_b_body"])
        self.assertNotIn("complete_two_hand_raid_a_body", decision.metadata)

    def test_execute_and_bloodthirst_preserve_source_order_below_twenty(self):
        adapter = RuntimeBoundContraRaidBAdapterV1(_binding())
        state = replace(_dual(
            rage=30, contra_st_s=1.0, whirlwind_ready_in_s=3.0,
            bloodthirst_ready_in_s=2.0,
        ), target_health_pct=19.0)
        decision = adapter.propose(state)
        self.assertEqual(decision.gcd, BLOODTHIRST)
        self.assertEqual(decision.metadata["raw_gcd_calls"], [EXECUTE, BLOODTHIRST])
        self.assertEqual(decision.swing_queue, SwingQueueOp.CLEAVE)

    def test_tauren_whirlwind_precedence_bug_retained(self):
        adapter = RuntimeBoundContraRaidBAdapterV1(_binding())
        decision = adapter.propose(_dual(
            rage=0, race_is_tauren=True, target_distance_yards=5.0,
        ))
        self.assertEqual(decision.gcd, WHIRLWIND)
        self.assertEqual(decision.metadata["raw_gcd_calls"], [WHIRLWIND])

    def test_two_hand_is_explicitly_not_raid_b_dual_wield(self):
        decision = RuntimeBoundContraRaidBAdapterV1(_binding()).propose(_state())
        self.assertFalse(decision.valid)
        self.assertIn("only Contra_SCKBZ_B", decision.reason)


if __name__ == "__main__":
    unittest.main()
