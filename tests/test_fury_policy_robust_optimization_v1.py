from __future__ import annotations

import unittest
from unittest.mock import patch

from o2o_dps.fury_policy_optimization_v1 import FuryPolicyParameters, PolicyScenario
from o2o_dps.fury_policy_robust_optimization_v1 import optimize_fury_policy_robust


def _scenario(name: str) -> PolicyScenario:
    return PolicyScenario(
        name,
        {
            "raid": {"parties": [{"players": [{}]}]},
            "encounter": {
                "duration": 10,
                "targets": [{"level": 60, "name": name}],
            },
            "simOptions": {"iterations": 1, "interactive": True},
        },
        10_000,
    )


class FuryPolicyRobustOptimizationV1Tests(unittest.TestCase):
    def test_selects_best_worst_branch_and_requires_every_validation_branch(self):
        candidates = (
            FuryPolicyParameters(heroic_strike_threshold=35, use_death_wish=False),
            FuryPolicyParameters(heroic_strike_threshold=65, use_death_wish=False),
        )

        def fake_rollout(bridge, request, adapter, *, seed, horizon_ms, **kwargs):
            branch = request["encounter"]["targets"][0]["name"]
            if adapter.expert_id == "cat.fury.profile1":
                dps = 100.0
            elif adapter.expert_id == "contra.deployed.fury.raid_a":
                dps = 90.0
            elif adapter.parameters.heroic_strike_threshold == 35:
                dps = 130.0 if branch == "easy" else 95.0
            else:
                dps = 112.0 if branch == "easy" else 111.0
            return {
                "damage_delta": dps * horizon_ms / 1000.0,
                "dps": dps,
                "decision_count": 1,
                "configured_horizon_complete": True,
                "all_lane_projections_faithful": True,
                "omitted_lane_count": 0,
                "omitted_lane_counts": {},
                "nonfaithful_reason_counts": {},
                "source_execution": False,
                "exact_lua_replay": False,
            }

        with patch(
            "o2o_dps.fury_policy_optimization_v1.run_fury_expert_closed_loop",
            side_effect=fake_rollout,
        ):
            result = optimize_fury_policy_robust(
                object(),
                {"easy": (_scenario("easy"),), "hard": (_scenario("hard"),)},
                training_seeds=(1, 2),
                validation_seeds=(3, 4),
                candidates=candidates,
            )

        self.assertEqual(result["selected_parameters"]["heroic_strike_threshold"], 65)
        self.assertEqual(result["passed_validation_branch_count"], 2)
        self.assertTrue(result["simulator_robust_gate_passed"])
        self.assertFalse(result["deployment_allowed"])
        self.assertNotIn("rollouts", result["training_branches"][0])


if __name__ == "__main__":
    unittest.main()
