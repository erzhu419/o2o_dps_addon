"""A reachable Cat queue choice absent from the frozen 13-axis candidate."""

from __future__ import annotations

import unittest

from o2o_dps.cat2new_fury_cat_gap_policy_v1 import (
    FuryCatGapControllerV1,
    FuryCatGapPolicyParametersV1,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_expert_adapters import (
    CatFurySourceAdapter,
    FuryExpertState,
    WeaponMode,
)
from tests.test_cat2new_fury_cat_gap_policy_v1 import BASE_PARAMETERS


class CatGapQueueExpressivityCounterexampleTests(unittest.TestCase):
    def test_ready_bt_and_ww_reserve_cannot_match_cat_within_base_axis(self) -> None:
        state = FuryExpertState(
            rage=80,
            target_health_pct=50,
            weapon_mode=WeaponMode.TWO_HAND,
            bloodthirst_ready_in_s=0,
            whirlwind_ready_in_s=0,
            heroic_strike_cost=15,
            whirlwind_cost=25,
            queued_swing=SwingQueueOp.KEEP,
        )
        cat_queue, cat_reserve = CatFurySourceAdapter()._queue_proposal(state)
        self.assertEqual((cat_queue, cat_reserve), (SwingQueueOp.KEEP, 85.0))

        for base in range(35, 76, 5):
            parameters = FuryCatGapPolicyParametersV1.from_mapping(
                {**BASE_PARAMETERS, "heroic_strike_base_rage": base}
            )
            with self.subTest(base=base):
                self.assertEqual(
                    FuryCatGapControllerV1(parameters)._queue_operation(state),
                    SwingQueueOp.HEROIC_STRIKE,
                )


if __name__ == "__main__":
    unittest.main()
