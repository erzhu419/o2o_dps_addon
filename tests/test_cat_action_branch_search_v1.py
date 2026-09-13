from __future__ import annotations

import unittest

from o2o_dps.branch_teacher_v1 import DEFAULT_BRIDGE
from o2o_dps.cat_action_branch_search_v1 import (
    ActionBranchV1, apply_action_branch_v1,
    available_branches_v1, run_cat_action_branch_search_v1,
)
from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4,
    validate_source_decision_v4,
)
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.expert_policy import SwingQueueOp, WAIT_ACTION
from o2o_dps.fury_expert_adapters import (
    BLOODTHIRST, WHIRLWIND, FuryExpertState, WeaponMode,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


class CatActionBranchSearchV1Tests(unittest.TestCase):
    def test_action_plans_are_current_state_alternatives(self) -> None:
        state = CatFuryFullPolicyStateV4(combat=FuryExpertState(
            rage=100.0, target_health_pct=50.0,
            weapon_mode=WeaponMode.TWO_HAND,
            mainhand_swing_remaining_s=1.0,
        ))
        base = CatFuryFullPolicyAdapterV4().propose(state)
        self.assertEqual(WHIRLWIND, base.gcd)
        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, base.swing_queue)
        self.assertEqual(
            ("SUPPRESS_QUEUE", "WW_TO_BT", "DEFER_GCD"),
            available_branches_v1(state, base),
        )
        suppressed = apply_action_branch_v1(state, base, "SUPPRESS_QUEUE")
        self.assertIs(SwingQueueOp.KEEP, suppressed.swing_queue)
        self.assertFalse(any(sink.channel == "swing_queue" for sink in suppressed.raw_sink_order))
        swapped = apply_action_branch_v1(state, base, "WW_TO_BT")
        self.assertEqual(BLOODTHIRST, swapped.gcd)
        self.assertEqual("嗜血", next(s.value for s in swapped.raw_sink_order if s.channel == "gcd"))
        deferred = apply_action_branch_v1(state, base, "DEFER_GCD")
        self.assertEqual(WAIT_ACTION, deferred.gcd)
        self.assertFalse(any(s.channel == "gcd" for s in deferred.raw_sink_order))
        self.assertEqual(WHIRLWIND, base.gcd)
        for decision in (suppressed, swapped, deferred):
            self.assertIs(decision, validate_source_decision_v4(decision))
        with self.assertRaisesRegex(ValueError, "not available"):
            apply_action_branch_v1(state, base, "BT_TO_WW")

    def test_branch_spec_rejects_unknown_action(self) -> None:
        with self.assertRaises(ValueError):
            ActionBranchV1(0, "OMNISCIENT_EXECUTE")

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file(), "native Windows bridge unavailable")
    def test_native_visited_state_search_completes_full_wave(self) -> None:
        case = build_development_wave_case_v1(2026091301)
        result = run_cat_action_branch_search_v1(
            case,
            lambda: SimulatorBridgeDynamicV3(
                DEFAULT_BRIDGE, cwd=DEFAULT_BRIDGE.parents[2] / "wowsims-turtle",
            ),
            max_states=6,
        )
        self.assertEqual("COMPLETED", result["baseline_terminal"]["status"])
        self.assertEqual(6, result["selected_state_count"])
        self.assertGreaterEqual(result["independent_action_branch_count"], 6)
        self.assertTrue(any(row["kind"] == "WW_TO_BT" for row in result["branches"]))
        self.assertTrue(all(row["status"] == "COMPLETE_BRANCH_SMOKE" for row in result["branches"]))
        self.assertTrue(all(row["branch_action_accepted"] for row in result["branches"]))
        self.assertTrue(all(row["branch_action_receipt"]["candidate"] for row in result["branches"]
                            if row["kind"] != "DEFER_GCD"))
        self.assertFalse(result["causal_effect_established"])
        self.assertFalse(result["hidden_rng_snapshot_verified"])


if __name__ == "__main__":
    unittest.main()
