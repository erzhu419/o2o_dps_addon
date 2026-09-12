from __future__ import annotations

import math
import unittest

from o2o_dps.cat2new_fury_horizon_analysis_v1 import (
    Cat2NewFuryHorizonAnalysisV1Error,
    summarize_horizon_deltas_v1,
)
from o2o_dps.cat2new_fury_horizon_confirmation_v1 import CONFIRMATION_ARM_IDS


class Cat2NewFuryHorizonAnalysisV1Tests(unittest.TestCase):
    def test_summarizes_exact_three_arm_family_and_holm(self) -> None:
        values = {
            CONFIRMATION_ARM_IDS[0]: [2.0] * 256,
            CONFIRMATION_ARM_IDS[1]: [0.0] * 256,
            CONFIRMATION_ARM_IDS[2]: [-1.0 if index % 2 else 2.0 for index in range(256)],
        }
        result = summarize_horizon_deltas_v1(values)
        rows = {row["arm_id"]: row for row in result["arm_results"]}
        first = rows[CONFIRMATION_ARM_IDS[0]]
        zero = rows[CONFIRMATION_ARM_IDS[1]]
        mixed = rows[CONFIRMATION_ARM_IDS[2]]
        self.assertEqual((256, 0, 0), (first["wins"], first["ties"], first["losses"]))
        self.assertEqual(0.0, first["raw_two_sided_p"])
        self.assertTrue(first["holm_reject_familywise_0_05"])
        self.assertEqual((0, 256, 0), (zero["wins"], zero["ties"], zero["losses"]))
        self.assertEqual(1.0, zero["raw_two_sided_p"])
        self.assertFalse(zero["holm_reject_familywise_0_05"])
        self.assertAlmostEqual(0.5, mixed["candidate_minus_cat_mean_dps"])
        self.assertEqual(-1.0, mixed["p05_dps"])
        self.assertEqual(0.5, mixed["median_dps"])
        self.assertEqual(2.0, mixed["p95_dps"])
        self.assertEqual(
            CONFIRMATION_ARM_IDS[0],
            result["diagnostic_ranking"][0]["arm_id"],
        )
        self.assertFalse(result["comparison_ready"])
        self.assertFalse(result["scientific_result_available"])
        self.assertFalse(result["deployment_allowed"])

    def test_finite_student_t_probability_for_nondegenerate_vector(self) -> None:
        values = {
            arm_id: [float((index % 7) - 2) for index in range(256)]
            for arm_id in CONFIRMATION_ARM_IDS
        }
        result = summarize_horizon_deltas_v1(values)
        for row in result["arm_results"]:
            self.assertTrue(math.isfinite(row["paired_t_statistic"]))
            self.assertGreaterEqual(row["raw_two_sided_p"], 0.0)
            self.assertLessEqual(row["raw_two_sided_p"], 1.0)
            self.assertGreaterEqual(row["holm_adjusted_p"], row["raw_two_sided_p"])

    def test_rejects_missing_short_or_nonfinite_vectors(self) -> None:
        complete = {arm_id: [0.0] * 256 for arm_id in CONFIRMATION_ARM_IDS}
        missing = dict(complete)
        missing.pop(CONFIRMATION_ARM_IDS[-1])
        with self.assertRaisesRegex(
            Cat2NewFuryHorizonAnalysisV1Error, "exactly the frozen three"
        ):
            summarize_horizon_deltas_v1(missing)
        short = dict(complete)
        short[CONFIRMATION_ARM_IDS[0]] = [0.0] * 255
        with self.assertRaisesRegex(Cat2NewFuryHorizonAnalysisV1Error, "exactly 256"):
            summarize_horizon_deltas_v1(short)
        nonfinite = dict(complete)
        nonfinite[CONFIRMATION_ARM_IDS[0]] = [0.0] * 255 + [math.inf]
        with self.assertRaisesRegex(Cat2NewFuryHorizonAnalysisV1Error, "nonfinite"):
            summarize_horizon_deltas_v1(nonfinite)


if __name__ == "__main__":
    unittest.main()
