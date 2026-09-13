from __future__ import annotations

from pathlib import Path
import unittest

from o2o_dps.development_raid_b_wave_v1 import (
    build_dual_wield_raid_b_wave_v1,
    run_raid_b_four_policy_wave_v1,
)
from o2o_dps.development_wave_panel_v1 import DEFAULT_BRIDGE, WORKSPACE_ROOT
from o2o_dps.fury_runtime_bound_deployed_contra_raid_b_v1 import POLICY_ID


class DevelopmentRaidBWaveV1Tests(unittest.TestCase):
    def test_source_wave_is_preserved_but_build_is_controlled_dual_wield(self) -> None:
        case, scenario = build_dual_wield_raid_b_wave_v1(2026091401)
        self.assertEqual(case.case_spec["source_wave_model_target_count"], 2)
        self.assertEqual(case.case_spec["build_id"], "clean_dual_weapon_probe")
        self.assertTrue(case.case_spec["manual_target_switch_not_modeled"])
        self.assertEqual(case.case_spec["request_sha256"], case.dynamic_load.request_sha256)
        self.assertEqual(scenario["target_context_bundle"]["request_sha256"],
                         case.dynamic_load.request_sha256)
        self.assertEqual(len(case.target_contexts), 2)
        self.assertEqual(case.target_contexts[0].equipped_item_names,
                         case.target_contexts[1].equipped_item_names)
        self.assertIn("CONTROLLED_DUAL_WIELD_BUILD_NOT_SOURCE_FOCAL_BUILD",
                      scenario["scenario_model"]["limitation_codes"])

    def test_native_two_target_panel_keeps_distinct_raid_b_and_proxy_contra_new(self) -> None:
        if not DEFAULT_BRIDGE.exists() or not (WORKSPACE_ROOT / "wowsims-turtle").exists():
            self.skipTest("native Windows bridge unavailable")
        panel = run_raid_b_four_policy_wave_v1(2026091401)
        rows = {row["policy_id"]: row for row in panel["rows"]}
        self.assertTrue(panel["four_way_complete"])
        self.assertFalse(panel["comparison_ready"])
        self.assertEqual(panel["post_gcd_queue_acceptance"],
                         "REJECTED_BY_CLIENT_PROBE_NOT_SIMULATOR_CAST")
        self.assertTrue(panel["manual_target_switch_not_modeled"])
        self.assertEqual(panel["all_deployed_target_index"], 0)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(
            row["decision_opportunities"]["status"] == "OBSERVED_SIMULATOR_INVOCATIONS"
            for row in rows.values()
        ))
        self.assertIn(POLICY_ID, rows)
        self.assertNotIn("contra.deployed.fury.raid_a", rows)
        self.assertEqual(rows[POLICY_ID]["status"], "COMPLETED")
        self.assertEqual(rows[POLICY_ID]["artifact_status"], "COMPLETE_NONFAITHFUL")
        self.assertEqual(len(rows[POLICY_ID]["artifact_nonfatal_blocker_codes"]), 4)
        contra_new = rows["contra260817.fury.source_candidate"]
        self.assertEqual(contra_new["status"], "COMPLETED")
        self.assertIsInstance(contra_new["own_effective_damage"], float)
        self.assertIn("CONTRA260817_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY",
                      contra_new["artifact_nonfatal_blocker_codes"])


if __name__ == "__main__":
    unittest.main()
