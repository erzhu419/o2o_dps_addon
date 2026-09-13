from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from o2o_dps.development_wave_panel_v1 import (
    DEFAULT_BINDING,
    DEFAULT_BRIDGE,
    _bridge_platform,
    run_development_wave_panel_v1,
)


class DevelopmentBridgePlatformTests(unittest.TestCase):
    def test_platform_receipt_matches_execution_host(self) -> None:
        for system, machine, expected in (
            ("Windows", "AMD64", "windows-amd64"),
            ("Linux", "x86_64", "linux-amd64"),
        ):
            with patch("platform.system", return_value=system), patch(
                "platform.machine", return_value=machine
            ):
                self.assertEqual(expected, _bridge_platform())


@unittest.skipUnless(
    os.name == "nt" and DEFAULT_BRIDGE.is_file() and DEFAULT_BINDING.is_file(),
    "pinned Windows bridge and deployed Contra runtime binding required",
)
class DevelopmentWavePanelV1Tests(unittest.TestCase):
    def test_deployed_low_rage_retry_seed_now_completes_without_fabricated_cast(self) -> None:
        panel = run_development_wave_panel_v1(
            master_seed=20260916, candidate_kind="anchor_13d",
        )
        self.assertTrue(panel["four_way_complete"])
        deployed = next(row for row in panel["rows"]
                        if row["policy_id"] == "contra.deployed.fury.raid_a")
        self.assertEqual("COMPLETED", deployed["status"])
        self.assertEqual(0, deployed["fatal_error_count"])
        self.assertAlmostEqual(5621.7136085966295, deployed["own_effective_damage"])

    def test_zero_residual_candidate_ties_native_cat(self) -> None:
        panel = run_development_wave_panel_v1(
            master_seed=20260913, candidate_kind="cat_residual",
            residual_discount_rage=0.0,
        )
        self.assertTrue(panel["four_way_complete"])
        self.assertEqual(0.0, panel["residual_discount_rage"])
        cat = next(row for row in panel["rows"] if row["policy_id"] == "cat.fury.profile1")
        candidate = next(row for row in panel["rows"] if row["role"] == "CANDIDATE")
        self.assertEqual(cat["own_effective_damage"], candidate["own_effective_damage"])
        self.assertEqual(cat["ttk_ms"], candidate["ttk_ms"])

    def test_one_complete_model_wave_all_four_native_lanes(self) -> None:
        for candidate_kind in ("anchor_13d", "cat_residual"):
            with self.subTest(candidate_kind=candidate_kind):
                panel = run_development_wave_panel_v1(
                    master_seed=20260913, candidate_kind=candidate_kind
                )
                self.assertEqual("FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY", panel["status"])
                self.assertTrue(panel["four_way_complete"])
                self.assertEqual(4, panel["completed_count"])
                self.assertFalse(panel["comparison_ready"])
                for row in panel["rows"]:
                    self.assertEqual("COMPLETED", row["status"])
                    self.assertEqual("ALL_TARGETS_DEAD", row["terminal_reason"])
                    self.assertEqual(0, row["omitted_lane_count"])
                    self.assertEqual(0, row["fatal_error_count"])
                    self.assertAlmostEqual(
                        126397.0,
                        row["own_effective_damage"]
                        + row["background_effective_damage"],
                        places=5,
                    )


if __name__ == "__main__":
    unittest.main()
