from __future__ import annotations

import gzip
import json
import unittest

from o2o_dps.development_wave_case_v1 import (
    INSTANCE_ID, SOURCE_CAPSULE_BUNDLE_SHA256, SOURCE_CAPSULE_SHA256, WAVE_ID,
)
from o2o_dps.development_wave_coverage_v1 import (
    DEFAULT_MANIFEST, _model_source_blockers, build_development_wave_coverage_v1,
)


class DevelopmentWaveCoverageV1Tests(unittest.TestCase):
    def test_complete_proxy_is_required_for_model_design(self) -> None:
        scenario = {
            "source_identity": {"instance_id": "instance-a", "wave_id": "wave-a"},
            "horizon": {"milliseconds": 9000},
            "base_armor_hypothesis_family": [
                {"base_armor": 1721, "status": "SENSITIVITY_HYPOTHESIS"}
            ],
            "targets": [{
                "target_guid": "target-a",
                "max_health_hypothesis_family": {"observed_kill_budget_proxy": {
                    "status": "OBSERVED", "completeness": "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS",
                    "value": 10000, "unparsed_damage_event_count": 0,
                    "death_anchor": {"csv_line": 123},
                }},
                "attackable_window_hypothesis_family": [{"branch_id": "full_wave"}],
            }],
        }
        self.assertEqual([], _model_source_blockers(scenario))
        scenario["targets"][0]["max_health_hypothesis_family"]["observed_kill_budget_proxy"]["death_anchor"] = None
        self.assertEqual(
            ["MISSING_COMPLETE_PER_TARGET_KILL_BUDGET_PROXY"],
            _model_source_blockers(scenario),
        )

    @unittest.skipUnless(DEFAULT_MANIFEST.exists(), "local old50 capsule is not installed")
    def test_local_1197_wave_inventory_and_both_source_bindings(self) -> None:
        with gzip.open(DEFAULT_MANIFEST, "rt", encoding="utf-8") as source:
            manifest = json.load(source)
        row = next(
            scenario for scenario in manifest["scenarios"]
            if scenario["source_identity"]["instance_id"] == INSTANCE_ID
            and scenario["source_identity"]["wave_id"] == WAVE_ID
        )
        self.assertEqual(
            "4d0711570c4c4f4000e68676ab62f7625eae83d4850b3f86097b376d25330b3b",
            row["capsule_sha256"],
        )
        self.assertEqual(SOURCE_CAPSULE_SHA256, row["capsule_sha256"])
        self.assertEqual(SOURCE_CAPSULE_BUNDLE_SHA256, manifest["content_address"]["sha256"])
        result = build_development_wave_coverage_v1(manifest, manifest_reference=str(DEFAULT_MANIFEST))
        self.assertEqual(50, result["source_instance_count"])
        self.assertEqual(1197, result["source_wave_count"])
        self.assertEqual(3269, result["source_target_count"])
        self.assertEqual({"single_target": 618, "multi_target": 579}, result["source_strata"])
        self.assertEqual(1182, result["source_field_ready_for_model_design_count"])
        self.assertEqual(15, result["source_field_incomplete_count"])
        self.assertEqual(1, result["implemented_case_count"])
        self.assertEqual(1196, result["other_source_waves_not_configured_or_run_in_this_lane"])
        self.assertTrue(result["implemented_case"]["capsule_binding_matches_code"])
        self.assertTrue(result["implemented_case"]["bundle_binding_matches_code"])
        candidates = result["twelve_source_field_ready_candidates"]
        self.assertEqual(12, len(candidates))
        self.assertEqual(12, len({row["instance_id"] for row in candidates}))
        self.assertEqual(6, sum(row["target_count"] == 1 for row in candidates))
        self.assertEqual(6, sum(row["target_count"] > 1 for row in candidates))
        self.assertTrue(all(row["wave_id"] != WAVE_ID for row in candidates))


if __name__ == "__main__":
    unittest.main()
