from __future__ import annotations

import unittest

from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_uncertainty_v1 import (
    BRANCHES,
    build_uncertainty_branch_v1,
    summarize_uncertainty_panels_v1,
)


class DevelopmentWaveUncertaintyV1Tests(unittest.TestCase):
    def test_reference_preserves_native_dynamic_load(self) -> None:
        case, scenario = build_uncertainty_branch_v1(20260913, BRANCHES[0])
        original = build_development_wave_case_v1(20260913)
        self.assertEqual(original.dynamic_load.contract_sha256, case.dynamic_load.contract_sha256)
        self.assertEqual(case.dynamic_load.request_sha256, scenario["scenario_model"]["request_sha256"])
        self.assertEqual(60, len(scenario["dynamic_load_config"]["background_damage_events"]))
        self.assertEqual([], scenario["dynamic_load_config"]["attackability_events"])
        self.assertEqual("EXCLUDED_BY_CONSTRUCTION_NOT_ESTIMATED_FROM_SOURCE_TOTAL",
                         case.case_spec["uncertainty_branch"]["team_focal_contribution"])

    def test_attackability_delay_moves_team_clock_and_binds_context(self) -> None:
        branch = next(b for b in BRANCHES if b.name == "attackable_after_3s")
        case, scenario = build_uncertainty_branch_v1(20260913, branch)
        config = scenario["dynamic_load_config"]
        self.assertEqual([False, True], [e["attackable"] for e in config["attackability_events"]])
        self.assertEqual([0, 3000], [e["time_ms"] for e in config["attackability_events"]])
        self.assertEqual(3500, config["background_damage_events"][0]["time_ms"])
        self.assertEqual(54, len(config["background_damage_events"]))
        self.assertEqual(case.dynamic_load.request_sha256,
                         scenario["target_context_bundle"]["request_sha256"])

    def test_hp_and_armor_branches_change_request_and_load_together(self) -> None:
        for branch in BRANCHES:
            with self.subTest(branch=branch.name):
                case, scenario = build_uncertainty_branch_v1(20260913, branch)
                target = scenario["request"]["encounter"]["targets"][0]
                self.assertEqual(branch.hp, target["stats"][34])
                self.assertEqual(branch.armor, target["stats"][26])
                self.assertEqual(branch.hp, scenario["dynamic_load_config"]["target_health"][0]["health"])
                self.assertEqual(branch.hp, scenario["target_context_bundle"]["contexts"][0]["target_max_health"])
                self.assertEqual(case.dynamic_load.config.content_sha256,
                                 scenario["dynamic_load_config"]["content_sha256"])

    def test_missing_lane_does_not_become_zero_damage(self) -> None:
        panels = [{
            "case": {"uncertainty_branch": {"name": "reference"}},
            "master_seed": 20260913,
            "four_way_complete": False,
            "rows": [{"policy_id": "cat.fury.profile1", "own_effective_damage": None}],
        }]
        summary = summarize_uncertainty_panels_v1(panels, (BRANCHES[0],))
        row = summary["branches"][0]
        self.assertEqual(0, row["four_way_complete_seeds"])
        self.assertEqual({}, row["mean_own_effective_damage"])
        self.assertEqual([], row["ranking_by_mean_damage"])
        self.assertEqual([20260913], row["incomplete_seeds"])


if __name__ == "__main__":
    unittest.main()
