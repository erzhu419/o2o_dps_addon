from __future__ import annotations

from unittest import TestCase, mock

from o2o_dps.development_two_wave_build_aggregate_v1 import (
    aggregate_two_wave_build_results_v1,
    run_two_wave_build_aggregate_v1,
)
from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS, SCHEMA as PANEL_SCHEMA


def _row(role: str, damage: float, dps: float, wave1: float, wave2: float, *, status: str = "COMPLETED") -> dict:
    return {
        "policy_id": "cat.fury.profile1" if role == "BASELINE" else "candidate",
        "role": role,
        "status": status,
        "two_wave_timeline_valid": status == "COMPLETED",
        "own_effective_damage": damage if status == "COMPLETED" else None,
        "whole_two_wave_dps": dps if status == "COMPLETED" else None,
        "target_outcomes": [
            {"simulated_damage_applied": wave1},
            {"simulated_damage_applied": wave2},
        ] if status == "COMPLETED" else None,
        "error": "mock censored" if status != "COMPLETED" else None,
    }


def _result(seed: int, live_delta: float, dual_delta: float, *, censor: bool = False) -> dict:
    builds = []
    for build_id, delta in zip(BUILD_IDS, (live_delta, dual_delta)):
        builds.append({
            "build_id": build_id,
            "status": "EXECUTED_INCOMPLETE_DEVELOPMENT_ONLY" if censor and build_id == BUILD_IDS[1]
            else "TWO_WAVE_TWO_LANE_COMPLETE_DEVELOPMENT_ONLY",
            "rows": [
                _row("BASELINE", 100.0, 10.0, 40.0, 60.0),
                _row("CANDIDATE", 100.0 + delta, 10.0 + delta / 10.0,
                     40.0 + delta * 0.4, 60.0 + delta * 0.6,
                     status="CENSORED_WATCHDOG" if censor and build_id == BUILD_IDS[1] else "COMPLETED"),
            ],
        })
    return {
        "schema": PANEL_SCHEMA,
        "master_seed": seed,
        "status": "EXECUTED_INCOMPLETE_DEVELOPMENT_ONLY" if censor
        else "TWO_BUILD_TWO_WAVE_COMPLETE_DEVELOPMENT_ONLY",
        "builds": builds,
    }


class TwoWaveAggregateTests(TestCase):
    def test_matched_mean_se_and_censored_seed_retained_without_imputation(self) -> None:
        result = aggregate_two_wave_build_results_v1([
            _result(1, 10, -5), _result(2, 30, 15), _result(3, 999, 999, censor=True),
        ])
        self.assertEqual(result["matched_n"], 2)
        self.assertEqual(result["excluded_n"], 1)
        self.assertEqual(result["status"], "SMALL_PANEL_PARTIAL_DEVELOPMENT_ONLY")
        self.assertEqual(result["aggregates"][BUILD_IDS[0]]["candidate_minus_cat"]["own_effective_damage"],
                         {"mean": 20.0, "se": 10.0, "unit": "DAMAGE"})
        self.assertEqual(result["aggregates"][BUILD_IDS[1]]["candidate_minus_cat"]["own_effective_damage"],
                         {"mean": 5.0, "se": 10.0, "unit": "DAMAGE"})
        self.assertEqual(result["seed_reports"][2]["status"], "INCOMPLETE_OR_CENSORED")
        self.assertEqual(result["seed_reports"][2]["builds"][1]["rows"][1]["error"], "mock censored")
        self.assertFalse(result["superiority_claim"])

    def test_one_complete_seed_has_no_standard_error_or_superiority_label(self) -> None:
        result = aggregate_two_wave_build_results_v1([_result(7, 10, -5)])
        self.assertEqual(result["status"], "INSUFFICIENT_MATCHED_SEEDS_DEVELOPMENT_ONLY")
        self.assertIsNone(result["aggregates"][BUILD_IDS[0]]["candidate_minus_cat"]["own_effective_damage"]["se"])

    def test_runner_preserves_exception_and_continues_to_next_seed(self) -> None:
        with mock.patch(
            "o2o_dps.development_two_wave_build_aggregate_v1.run_two_wave_build_panel_v1",
            side_effect=[RuntimeError("native load failed"), _result(12, 1, 2)],
        ):
            result = run_two_wave_build_aggregate_v1(master_seeds=[11, 12])
        self.assertEqual(result["requested_n"], 2)
        self.assertEqual(result["matched_n"], 1)
        self.assertEqual(result["seed_reports"][0]["error"], "RuntimeError: native load failed")
        self.assertEqual(result["seed_reports"][1]["status"], "MATCHED_COMPLETE")

    def test_duplicate_seed_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique integers"):
            aggregate_two_wave_build_results_v1([_result(1, 1, 1), _result(1, 2, 2)])
