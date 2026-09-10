from __future__ import annotations

import unittest

from o2o_dps.fury_policy_sensitivity_summary_v1 import (
    SensitivitySummaryError,
    summarize_sensitivity_artifacts,
)


def _artifact(*, armor: int, level: int, delta: float, wins: int, losses: int, gate: bool):
    candidate_id = "candidate"
    baseline_id = "cat"
    return {
        "kind": "fury_policy_optimization_v1",
        "selected_policy_id": candidate_id,
        "strongest_validation_baseline_id": baseline_id,
        "simulator_improvement_gate_passed": gate,
        "scenario_contract": {
            "provenance": [
                {
                    "provenance": {
                        "target_hypotheses": {
                            "armor": {"value": armor},
                            "level": {"value": level},
                        }
                    }
                }
            ]
        },
        "validation": {
            "ranking": [
                {
                    "expert_id": candidate_id,
                    "weighted_mean_dps": 110.0,
                    "omitted_lane_count": 0,
                },
                {"expert_id": baseline_id, "weighted_mean_dps": 100.0},
                {
                    "expert_id": "contra.deployed.fury.raid_a",
                    "weighted_mean_dps": 90.0,
                },
            ]
        },
        "paired_selected_vs_strongest_baseline": {
            "mean_paired_dps": delta,
            "pair_count": wins + losses,
            "wins": wins,
            "ties": 0,
            "losses": losses,
        },
    }


class FuryPolicySensitivitySummaryV1Tests(unittest.TestCase):
    def test_failed_branch_blocks_robust_gate_without_hiding_positive_mean(self):
        result = summarize_sensitivity_artifacts(
            [
                _artifact(armor=1961, level=60, delta=12.0, wins=6, losses=2, gate=True),
                _artifact(armor=1961, level=63, delta=-3.0, wins=3, losses=5, gate=False),
            ]
        )
        self.assertEqual(result["branch_count"], 2)
        self.assertEqual(result["gate_failed_branch_count"], 1)
        self.assertFalse(result["simulator_sensitivity_gate_passed"])
        self.assertEqual(result["paired_delta_dps_diagnostic"]["negative_mean_branch_count"], 1)
        self.assertFalse(result["deployment_allowed"])

    def test_duplicate_hypothesis_is_rejected(self):
        artifact = _artifact(
            armor=1961, level=60, delta=1.0, wins=2, losses=1, gate=True
        )
        with self.assertRaises(SensitivitySummaryError):
            summarize_sensitivity_artifacts([artifact, artifact])


if __name__ == "__main__":
    unittest.main()
