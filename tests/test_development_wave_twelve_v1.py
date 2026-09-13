from __future__ import annotations

from pathlib import Path
import unittest

from o2o_dps.development_wave_twelve_v1 import (
    SOURCE_FOCAL_ACTORS_12, WAVE_STRATA_12, audit_direct_guid_focal_v1,
    build_twelve_wave_case_v1, focal_actor_12, reduce_twelve_wave_panels_v1,
    source_rows_12,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import validate_dynamic_load_request_v3


class DevelopmentWaveTwelveV1Tests(unittest.TestCase):
    def test_predeclared_source_selection_and_all_target_models(self) -> None:
        rows = source_rows_12()
        self.assertEqual(12, len(rows))
        self.assertEqual(len(set(WAVE_STRATA_12.values())), 12)
        self.assertEqual([1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 8, 16],
                         [len(row["targets"]) for row in rows.values()])
        for stratum, wave_id in WAVE_STRATA_12.items():
            with self.subTest(stratum=stratum):
                case, scenario = build_twelve_wave_case_v1(20260913, stratum)
                spec = case.case_spec
                count = len(rows[stratum]["targets"])
                self.assertEqual("development_wave_twelve_case/v1", spec["schema"])
                self.assertEqual(wave_id, spec["source_wave_ref"])
                self.assertEqual(count, len(spec["required_target_ids"]))
                self.assertEqual(list(range(count)), spec["required_target_indices"])
                self.assertEqual(count, len(case.request["encounter"]["targets"]))
                self.assertEqual(count, len(case.dynamic_load.config.target_health))
                self.assertEqual(count, len(spec["team_background"]["per_target"]))
                self.assertFalse(spec["historical_exact"])
                self.assertFalse(spec["real_superiority_authorized"])
                self.assertFalse(spec["team_background"]["future_schedule_policy_visible"])
                self.assertEqual("ALL_REQUIRED_TARGETS_DEAD_CONFIRMED_BY_RECEIPT_AND_FINAL_STATE",
                                 spec["terminal"]["success"])
                self.assertEqual("CENSORED_WATCHDOG_NOT_COMPLETE", spec["terminal"]["watchdog_outcome"])
                for index, target in enumerate(spec["team_background"]["per_target"]):
                    self.assertEqual(target["modeled_team_damage_budget"]
                                     + target["excluded_focal_direct_guid_damage"],
                                     spec["initial_state"]["target_max_hp"][index])
                validate_dynamic_load_request_v3(case.dynamic_load, case.request)
                self.assertEqual(scenario["catalog_sha256"],
                                 spec["source_evidence"]["wave_capsule_bundle_sha256"])

    def test_frozen_direct_guid_exclusions_match_local_source(self) -> None:
        first = next(iter(source_rows_12().values()))
        raw = (Path(__file__).resolve().parents[1] / "offline_data" /
               first["targets"][0]["max_health_hypothesis_family"]
               ["observed_kill_budget_proxy"]["death_anchor"]["raw_file"])
        if not raw.exists():
            self.skipTest("source CSV is intentionally absent on compute nodes")
        self.assertEqual(12, len(SOURCE_FOCAL_ACTORS_12))
        for stratum, source in source_rows_12().items():
            with self.subTest(stratum=stratum):
                self.assertEqual(focal_actor_12(stratum), audit_direct_guid_focal_v1(source))

    def test_reduction_keeps_missing_wave_and_no_zero_score(self) -> None:
        rows = []
        for index, (stratum, wave_id) in enumerate(WAVE_STRATA_12.items()):
            complete = index != 8
            lanes = ([
                {"policy_id": "cat", "role": "BASELINE", "status": "COMPLETED",
                 "own_effective_damage": 100.0},
                {"policy_id": "candidate", "role": "CANDIDATE", "status": "COMPLETED",
                 "own_effective_damage": 110.0},
            ] if complete else [
                {"policy_id": "cat", "role": "BASELINE", "status": "FAILED",
                 "own_effective_damage": None},
                {"policy_id": "candidate", "role": "CANDIDATE", "status": "COMPLETED",
                 "own_effective_damage": 110.0},
            ])
            if index < 6:
                lanes[1:1] = [
                    {"policy_id": "contra_new", "role": "BASELINE", "status": "COMPLETED",
                     "own_effective_damage": 90.0},
                    {"policy_id": "contra_deployed", "role": "BASELINE", "status": "COMPLETED",
                     "own_effective_damage": 95.0},
                ]
            rows.append({
                "stratum": stratum, "source_wave_ref": wave_id,
                "target_count": 1 if index < 6 else 2,
                "comparison_scope": "FOUR_NATIVE_POLICIES_RAID_A" if index < 6 else "CAT_VS_ANCHOR_ONLY_RAID_B_CONTRA_UNSUPPORTED",
                "status": "COMPLETE" if complete else "EXECUTED_WITH_INCOMPLETE_LANES_DEVELOPMENT_ONLY",
                "policies": lanes,
            })
        panel = {"schema": "development_wave_twelve_panel/v1", "seed": 1, "rows": rows}
        reduced = reduce_twelve_wave_panels_v1([panel], expected_seed_count=1)
        self.assertFalse(reduced["all_predeclared_waves_matched"])
        self.assertEqual(11, sum(row["full_matched_gate"] for row in reduced["rows"]))
        self.assertEqual({}, reduced["rows"][8]["policy_mean_effective_damage"])
        self.assertEqual(10.0, reduced["rows"][0]["candidate_minus_baselines"]["cat"]
                         ["mean_effective_damage_delta"])
        with self.assertRaisesRegex(ValueError, "duplicate panel seed"):
            reduce_twelve_wave_panels_v1([panel, panel], expected_seed_count=2)

    def test_single_stratum_repair_does_not_claim_all_twelve_waves(self) -> None:
        stratum = "multi_5_6_targets"
        panel = {
            "schema": "development_wave_twelve_panel/v1", "seed": 2026091401,
            "rows": [{
                "stratum": stratum, "comparison_scope": "CAT_VS_ANCHOR_ONLY_RAID_B_CONTRA_UNSUPPORTED",
                "target_count": 5, "status": "2_LANE_COMPLETE_DEVELOPMENT_ONLY",
                "policies": [
                    {"policy_id": "cat", "role": "BASELINE", "status": "COMPLETED",
                     "own_effective_damage": 100.0},
                    {"policy_id": "candidate", "role": "CANDIDATE", "status": "COMPLETED",
                     "own_effective_damage": 90.0},
                ],
            }],
        }
        reduced = reduce_twelve_wave_panels_v1(
            [panel], expected_seed_count=1, only_stratum=stratum,
        )
        self.assertEqual(1, reduced["selected_wave_count"])
        self.assertTrue(reduced["all_selected_waves_matched"])
        self.assertFalse(reduced["all_predeclared_waves_matched"])
        self.assertEqual(-10.0, reduced["rows"][0]["candidate_minus_baselines"]["cat"]
                         ["mean_effective_damage_delta"])
        with self.assertRaisesRegex(ValueError, "do not match"):
            reduce_twelve_wave_panels_v1([panel], expected_seed_count=1)


if __name__ == "__main__":
    unittest.main()
