from __future__ import annotations

import math
import unittest

from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
from o2o_dps import chronicle_external_historical_fury_policy_v4 as policy_v4
from o2o_dps import chronicle_external_historical_fury_policy_v4_hpc_v1 as hpc_v4
from o2o_dps import chronicle_external_historical_fury_policy_v5 as policy_v5


ACTION_A = '{"action_key":"warrior.bloodthirst","target_role":"CURRENT_ENEMY"}'
ACTION_B = '{"action_key":"warrior.whirlwind","target_role":"CURRENT_ENEMY"}'


def _view(context: str) -> policy_v4.FeatureView:
    return policy_v4.FeatureView(
        v2_contexts={
            "coarse": f"coarse-{context}",
            "tactical": f"tactical-{context}",
            "full": f"full-{context}",
        },
        prefix_atoms=(policy_v4.FeatureAtom("base.wave_coarse", context),),
        dynamic=policy_v4.ObservedDynamicState(
            buckets={"rage": policy_v4.MISSING},
            atoms=(),
            missing_fields=("rage",),
            source="CURRENT_STAGE5_FIELD_ABSENT",
        ),
        provenance={"exact_inventory_used_as_model_context": False},
    )


def _component(action_a_count: int, action_b_count: int) -> policy_v4.AdditiveAggregate:
    aggregate = policy_v4.AdditiveAggregate()
    view = _view("shared")
    for _ in range(action_a_count):
        aggregate.add(view, ACTION_A, arm=policy_v4.ARM_A)
    for _ in range(action_b_count):
        aggregate.add(view, ACTION_B, arm=policy_v4.ARM_A)
    return aggregate


class ChronicleExternalHistoricalFuryPolicyV5Tests(unittest.TestCase):
    def test_positive_temperature_preserves_order_ties_and_tiny_ranks(self) -> None:
        tied = {"action-b": 0.4, "action-a": 0.4, "action-c": 0.1}
        expected = ("action-a", "action-b", "action-c")
        for beta in (0.05, 0.5, 1.0, 2.0, 20.0):
            calibrated = policy_v5.calibrate_distribution(
                tied, 0.1, beta=beta
            )
            self.assertEqual(calibrated.ranked_actions, expected)
            self.assertAlmostEqual(
                sum(calibrated.probabilities.values())
                + calibrated.unknown_probability,
                1.0,
            )
            self.assertEqual(
                calibrated.log_probabilities["action-a"],
                calibrated.log_probabilities["action-b"],
            )

        tiny = policy_v5.calibrate_distribution(
            {"a": 1.0, "b": 1e-200, "c": 1e-250},
            1e-300,
            beta=20.0,
        )
        self.assertEqual(tiny.ranked_actions, ("a", "b", "c"))
        self.assertGreater(tiny.log_probabilities["b"], tiny.log_probabilities["c"])
        self.assertEqual(tiny.probabilities["b"], 0.0)

    def test_temperature_fit_is_single_parameter_convex_solution(self) -> None:
        logs = (math.log(0.9), math.log(0.1))
        rows = (
            policy_v5.LogProbabilityRow(logs, true_index=0, weight=6),
            policy_v5.LogProbabilityRow(logs, true_index=1, weight=4),
        )
        fit = policy_v5.fit_inverse_temperature(rows)
        expected_beta = math.log(0.6 / 0.4) / math.log(0.9 / 0.1)
        self.assertAlmostEqual(fit.beta, expected_beta, places=12)
        self.assertEqual(fit.boundary_status, "INTERIOR")
        self.assertEqual(fit.calibration_weight, 10)
        self.assertLess(fit.calibrated_log_loss, fit.baseline_log_loss)

    def test_component_inner_oof_matches_v2_baseline_without_outer_leakage(self) -> None:
        components = {
            "component-a": _component(7, 3),
            "component-b": _component(5, 5),
            "component-c": _component(6, 4),
        }
        baseline = hpc_v4.evaluate_component_split(
            components,
            fold_count=5,
            split_seed=20260911,
            alpha=0.5,
            backoff_strength=8.0,
        )["metrics"]
        result = policy_v5.evaluate_rank_calibration(
            components,
            fold_count=5,
            split_seed=20260911,
            alpha=0.5,
            backoff_strength=8.0,
        )
        observed = result["baseline_v2"]
        for key in (
            "heldout_decision_count",
            "known_action_count",
            "known_action_coverage",
            "top1_accuracy",
            "top3_accuracy",
            "contextual_log_loss",
            "expected_calibration_error",
        ):
            self.assertEqual(observed[key], baseline[key])
        self.assertEqual(
            result["baseline_v2"]["top1_accuracy"],
            result["calibrated_v5"]["top1_accuracy"],
        )
        self.assertEqual(
            result["baseline_v2"]["top3_accuracy"],
            result["calibrated_v5"]["top3_accuracy"],
        )
        self.assertTrue(result["gate"]["top1_exactly_preserved"])
        self.assertTrue(result["gate"]["top3_exactly_preserved"])
        for fold in result["folds"]:
            outer_train = set(fold["outer_train_component_ids"])
            outer_test = set(fold["outer_test_component_ids"])
            self.assertFalse(outer_train & outer_test)
            for inner in fold["inner_component_fits"]:
                used = set(inner["inner_train_component_ids"])
                used.add(inner["inner_test_component_id"])
                self.assertEqual(used, outer_train)
                self.assertFalse(used & outer_test)
                self.assertEqual(inner["outer_test_overlap_count"], 0)

    def test_preregistration_retains_v4_negative_and_forbids_execution(self) -> None:
        plan = policy_v5.load_preregistered_plan()
        negative = policy_v5.load_v4_negative_result()
        summary = policy_v5.preregistration_summary()
        self.assertEqual(plan["calibrator"]["parameter_count"], 1)
        self.assertFalse(plan["calibrator"]["grid_search"])
        self.assertFalse(plan["calibrator"]["model_selection"])
        self.assertEqual(plan["budget"]["hyperparameter_trials"], 1)
        self.assertFalse(plan["budget"]["execution_authorized_now"])
        self.assertEqual(negative["status"], "RETAIN_NEGATIVE_NO_NEXT_HPC")
        self.assertFalse(
            negative["decision"]["low_cardinality_replacement_adopted"]
        )
        self.assertFalse(summary["final_v2_model_alone_is_sufficient"])
        self.assertTrue(
            summary["component_separated_v4_arm_a_aggregates_are_sufficient"]
        )
        self.assertFalse(summary["stage5_partitions_required"])

    def test_nonpositive_beta_is_rejected(self) -> None:
        with self.assertRaises(policy_v5.HistoricalFuryPolicyV5Error):
            policy_v5.calibrate_distribution({"a": 0.9}, 0.1, beta=0.0)

    def test_evaluator_rejects_a_different_outer_fold_protocol(self) -> None:
        components = {
            "component-a": _component(7, 3),
            "component-b": _component(5, 5),
            "component-c": _component(6, 4),
        }
        with self.assertRaises(policy_v5.HistoricalFuryPolicyV5Error):
            policy_v5.evaluate_rank_calibration(
                components,
                fold_count=3,
                split_seed=20260911,
            )


if __name__ == "__main__":
    unittest.main()
