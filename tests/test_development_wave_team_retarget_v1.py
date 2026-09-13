from __future__ import annotations

import unittest
from unittest.mock import patch

from o2o_dps.development_wave_team_retarget_v1 import (
    SourceFittedTeamRetargetBridgeV1,
    V14ProjectedDynamicV3Bridge,
    V14_BRIDGE,
    build_retarget_wave_case_v1,
    run_retarget_wave_panel_v1,
)


class DevelopmentWaveTeamRetargetV1Test(unittest.TestCase):
    def test_source_case_has_no_fixed_duplicate_damage(self) -> None:
        case, scenario, events = build_retarget_wave_case_v1(20260913)
        self.assertEqual(len(case.case_spec["required_target_ids"]), 2)
        self.assertTrue(events)
        self.assertEqual(case.dynamic_load.config.background_damage_events, ())
        self.assertEqual(scenario["dynamic_load_config"]["background_damage_events"], [])
        self.assertFalse(case.case_spec["team_background"]["learned_teammate_model_used"])

    def test_target_selection_uses_alive_attackable_prefix(self) -> None:
        state = {"dynamic_target_semantics": {"targets": [
            {"target_index": 0, "dead": True, "attackable": False},
            {"target_index": 1, "dead": False, "attackable": True},
        ]}}
        self.assertEqual(SourceFittedTeamRetargetBridgeV1._recipient(state, 0), 1)

    @unittest.skipUnless(V14_BRIDGE.exists(), "local native v14 bridge not installed")
    def test_native_candidate_replay_is_deterministic_and_retargets(self) -> None:
        first = run_retarget_wave_panel_v1(20260913)
        second = run_retarget_wave_panel_v1(20260913)
        candidate_first = next(row for row in first["rows"] if row["role"] == "CANDIDATE")
        candidate_second = next(row for row in second["rows"] if row["role"] == "CANDIDATE")
        cat = next(row for row in first["rows"] if row["policy_id"] == "cat.fury.profile1")
        cat_second = next(row for row in second["rows"] if row["policy_id"] == "cat.fury.profile1")
        self.assertEqual(cat["status"], "COMPLETED")
        self.assertEqual(cat["status"], cat_second["status"])
        self.assertEqual(cat["own_reported_damage"], cat_second["own_reported_damage"])
        self.assertEqual(cat["terminal_reason"], "ALL_TARGETS_DEAD")
        self.assertEqual(cat["own_effective_damage"], cat["own_reported_damage"])
        self.assertFalse(first["comparison_ready"])
        self.assertTrue(first["team_retarget_comparison_eligible"])
        self.assertEqual(first["team_retarget_comparison_gate"], "MATCHED_NATIVE_DEVELOPMENT_ONLY")
        self.assertEqual(candidate_first["status"], "COMPLETED")
        self.assertEqual(
            candidate_first["paired_own_damage_minus_baselines"],
            {"cat.fury.profile1": candidate_first["own_effective_damage"] - cat["own_effective_damage"]},
        )
        self.assertEqual(candidate_first["own_effective_damage"], candidate_second["own_effective_damage"])
        self.assertEqual(candidate_first["ttk_ms"], candidate_second["ttk_ms"])
        receipt = candidate_first["team_response_evidence"]
        self.assertGreater(receipt["events_emitted"], 0)
        self.assertGreater(receipt["events_retargeted_after_endogenous_death"], 0)
        self.assertEqual(receipt["native_runtime_receipt_status"], "COMPLETE_BOUND")
        self.assertTrue(all(receipt["native_runtime_receipt_checks"].values()))
        self.assertFalse(receipt["learned_teammate_model_used"])

    @unittest.skipUnless(V14_BRIDGE.exists(), "local native v14 bridge not installed")
    def test_responsive_receipt_ordinal_gap_remains_ineligible(self) -> None:
        original = V14ProjectedDynamicV3Bridge._request

        def corrupt_first_ordinal(bridge, command, **kwargs):
            response = original(bridge, command, **kwargs)
            if command == "dynamic_team_response_receipts":
                response["dynamic_team_response_receipts"]["receipts"][0]["damage_ordinal"] += 1
            return response

        with patch.object(V14ProjectedDynamicV3Bridge, "_request", corrupt_first_ordinal):
            panel = run_retarget_wave_panel_v1(20260913)
        cat = next(row for row in panel["rows"] if row["policy_id"] == "cat.fury.profile1")
        candidate = next(row for row in panel["rows"] if row["role"] == "CANDIDATE")
        self.assertEqual(cat["status"], "COMPLETED_BUT_INELIGIBLE")
        self.assertEqual(candidate["status"], "COMPLETED_BUT_INELIGIBLE")
        self.assertFalse(panel["team_retarget_comparison_eligible"])


if __name__ == "__main__":
    unittest.main()
