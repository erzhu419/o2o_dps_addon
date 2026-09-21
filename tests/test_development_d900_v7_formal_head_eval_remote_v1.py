from __future__ import annotations

import unittest

from scripts.development_d900_v7_formal_head_eval_remote_v1 import check_result


def _result() -> dict:
    return {
        "schema": "upper_kara_target_head_heldout_eval/v2",
        "heldout_instance_id": "d900a97b-b53e-4444-943b-3e0f2be8d477",
        "cohort_wave_count": 37,
        "new_reduced_model_binding": {
            "source_result_content_sha256": "result",
            "model_content_sha256": "model",
            "heldout_instance_excluded_by_runtime_store_binding": True,
        },
        "cohort_teammates_only": {
            "white_head_status": "STRICT_PREFIX_CHOICES_AVAILABLE",
            "metrics": {
                "FIRST_ACQUISITION": {"eligible_choices": 475},
                "RETARGET": {"eligible_choices": 830},
            },
            "white_metrics": {
                "FIRST_ACQUISITION": {"eligible_choices": 247},
                "RETARGET": {"eligible_choices": 425},
            },
        },
    }


class V7FormalHeadEvalRemoteTests(unittest.TestCase):
    def test_frozen_heldout_denominators_and_identity(self) -> None:
        check_result(_result(), result_sha="result", model_sha="model")
        wrong = _result()
        wrong["cohort_teammates_only"]["white_metrics"]["RETARGET"]["eligible_choices"] = 424
        with self.assertRaisesRegex(RuntimeError, "denominator differs"):
            check_result(wrong, result_sha="result", model_sha="model")


if __name__ == "__main__":
    unittest.main()
