from __future__ import annotations

from dataclasses import replace
import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import _state
from o2o_dps.development_two_wave_bloodrage_v1 import (
    BLOODRAGE_NAME, DeferFirstWaveBloodrageV1, FIRST_WAVE_OBSERVED_COMBAT_ELAPSED_LIMIT_S,
    run_two_wave_bloodrage_v1,
)
from o2o_dps.development_wave_panel_v1 import DEFAULT_BRIDGE


class TwoWaveBloodrageTests(unittest.TestCase):
    def test_predeclared_rule_uses_current_observation_and_returns_to_cat(self) -> None:
        state = _state(combat={
            "rage": 20.0, "bloodrage_ready": True,
            "bloodthirst_ready_in_s": 1.0, "whirlwind_ready_in_s": 1.0,
        })
        policy = DeferFirstWaveBloodrageV1()
        first = policy.propose(state)
        self.assertEqual(policy.interventions[0]["decision_index"], 0)
        self.assertFalse(any(s.value == BLOODRAGE_NAME for s in first.raw_sink_order))
        self.assertFalse(first.off_gcd)
        second = policy.propose(state)
        self.assertFalse(any(s.value == BLOODRAGE_NAME for s in second.raw_sink_order))
        self.assertEqual(len(policy.interventions), 2)
        self.assertTrue(any(s.value == BLOODRAGE_NAME for s in policy.propose(replace(
            state, combat_elapsed_s=FIRST_WAVE_OBSERVED_COMBAT_ELAPSED_LIMIT_S,
        )).raw_sink_order))

        after_limit = DeferFirstWaveBloodrageV1()
        late = after_limit.propose(replace(
            state, combat_elapsed_s=FIRST_WAVE_OBSERVED_COMBAT_ELAPSED_LIMIT_S,
        ))
        self.assertTrue(any(s.value == BLOODRAGE_NAME for s in late.raw_sink_order))
        self.assertFalse(after_limit.interventions)

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file(), "native bridge absent")
    def test_native_one_seed_keeps_cross_wave_cooldown_and_complete_terminals(self) -> None:
        result = run_two_wave_bloodrage_v1(20260913)
        self.assertEqual(result["status"], "COMPLETE_ACTION_COMPARISON")
        self.assertTrue(result["cat"]["two_wave_timeline_valid"])
        self.assertTrue(result["candidate"]["two_wave_timeline_valid"])
        self.assertTrue(result["cat_branch_bloodrage_accepted"])
        self.assertTrue(result["candidate_later_bloodrage_accepted"])
        self.assertLess(result["cat_bloodrage_time_ms"], result["candidate_bloodrage_time_ms"])
        self.assertGreaterEqual(result["candidate_bloodrage_time_ms"], 12_000)
        self.assertFalse(result["independent_wave_reset"])
        self.assertFalse(result["deployment_authorized"])

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file(), "native bridge absent")
    def test_native_no_op_when_cat_already_reserves_bloodrage(self) -> None:
        result = run_two_wave_bloodrage_v1(2026091607, build_id="clean_dual_weapon_probe")
        self.assertEqual(result["status"], "NO_OP_CAT_ALREADY_RESERVED")
        self.assertFalse(result["interventions"])
        self.assertEqual(result["paired_effective_damage_delta"], 0.0)
        self.assertTrue(result["cat"]["two_wave_timeline_valid"])
        self.assertTrue(result["candidate"]["two_wave_timeline_valid"])


if __name__ == "__main__":
    unittest.main()
