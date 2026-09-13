from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from o2o_dps.development_wave_panel_v1 import (
    DEFAULT_BINDING,
    DEFAULT_BRIDGE,
    _bridge_platform,
    _decision_opportunities,
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


class DecisionOpportunitySummaryTests(unittest.TestCase):
    def test_source_step_timing_counts_zero_and_short_reentry_separately(self) -> None:
        artifact = {
            "decision_count": 5,
            "steps": [
                {"simulator_state_before": {"time_ms": time_ms}}
                for time_ms in (0, 0, 50, 150, 150)
            ],
        }
        summary = _decision_opportunities(artifact)
        self.assertEqual("OBSERVED_SIMULATOR_INVOCATIONS", summary["status"])
        self.assertEqual(5, summary["invocation_count"])
        self.assertEqual(2, summary["same_millisecond_reentry_count"])
        self.assertEqual(1, summary["positive_sub_100ms_interval_count"])
        self.assertEqual(75.0, summary["positive_interval_median_ms"])
        self.assertEqual([0, 0, 50, 150, 150], summary["first_8_times_ms"])

    def test_cat2new_decision_timing_uses_its_own_retained_shape(self) -> None:
        summary = _decision_opportunities({
            "decisions": [{"time_ms": 100}, {"time_ms": 200}],
        })
        self.assertEqual("decisions.time_ms", summary["source"])
        self.assertEqual(2, summary["invocation_count"])
        self.assertEqual(0, summary["positive_sub_100ms_interval_count"])

    def test_missing_or_partial_timing_is_not_counted_as_zero(self) -> None:
        self.assertEqual("NOT_OBSERVED", _decision_opportunities({})["status"])
        self.assertEqual(
            "NOT_OBSERVED",
            _decision_opportunities({"steps": [{"decision_index": 1}]})["status"],
        )
        self.assertEqual(
            "NOT_OBSERVED",
            _decision_opportunities({"decision_count": 2, "steps": []})["status"],
        )


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
