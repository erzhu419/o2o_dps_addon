from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.promote_calibration import (
    CalibrationPromotionError,
    build_calibration_promotion,
    build_phase3_calibration_promotion,
    build_slam_calibration_promotion,
    main,
    promote_calibration,
)


def _parameter(
    mechanic: str,
    field: str,
    estimate,
    comparison: str,
    sample_count: int,
    simulator_value,
    unit: str,
) -> dict:
    return {
        "registry_target": {"mechanic": mechanic, "field": field},
        "estimate": estimate,
        "comparison": comparison,
        "sample_count": sample_count,
        "simulator_value": simulator_value,
        "unit": unit,
    }


def _summary() -> dict:
    return {
        "schema_version": 2,
        "kind": "brainofcat_calibration_summary",
        "game_patch": "Turtle WoW 1.18.1",
        "source": {"calibration_jsonl": "calibration.jsonl"},
        "task_completions": [
            {"task_id": f"task-{index}", "analysis_status": "detailed", "completed_trials": count}
            for index, count in enumerate((10, 10, 10, 1), start=1)
        ],
        "campaigns": [
            {
                "campaign_id": "warrior_fury_dummy_phase1",
                "campaign_run_id": "campaign-1",
                "completed_task_count": 4,
            }
        ],
        "deferred_analysis": [],
        "runs": [
            {
                "task_id": "warrior_bloodthirst_transition",
                "task_run_id": "bt-1",
                "parameters": [
                    _parameter("warrior.bloodthirst", "rage_cost", 30, "CONSISTENT", 10, 30, "rage"),
                    _parameter("warrior.bloodthirst", "cooldown_seconds", 6, "CONSISTENT", 10, 6, "seconds"),
                    _parameter("warrior.bloodthirst", "gcd_seconds", 1.5, "CONSISTENT", 10, 1.5, "seconds"),
                ],
            }
        ],
        "specialized_runs": [
            {
                "task_id": "warrior_heroic_strike_queue_swing",
                "task_run_id": "hs-1",
                "evidence_comparisons": [
                    _parameter("warrior.heroic_strike.queue", "consumes_gcd", False, "CONSISTENT", 1, False, "boolean"),
                    _parameter("warrior.heroic_strike.queue", "replaces_next_main_hand_swing", True, "CONSISTENT", 10, True, "boolean"),
                    _parameter("warrior.heroic_strike.queue", "rage_cost", 12, "CONSISTENT_WITH_DESCRIPTION", 5, "15 minus Improved Heroic Strike rank", "rage"),
                    _parameter("warrior.heroic_strike.queue", "miss_refund_fraction", 0, "OBSERVED_DIFFERS_SINGLE_TRIAL", 1, 0.8, "fraction_of_effective_base_cost"),
                ],
            },
            {
                "task_id": "warrior_execute_transition",
                "task_run_id": "execute-1",
                "evidence_comparisons": [
                    {
                        **_parameter("warrior.execute", "base_rage_cost", 10, "CONSISTENT_WITH_CURRENT_BUILD_DESCRIPTION", 1, "15 minus Improved Execute reduction", "rage"),
                        "evidence": "current build observed; generic formula not uniquely identified",
                    },
                    _parameter("warrior.execute", "gcd_seconds", 1.5, "CONSISTENT", 10, 1.5, "seconds"),
                    {
                        "registry_target": {"mechanic": "warrior.execute", "field": "execute_phase"},
                        "comparison": "CONSISTENT_BELOW_20_PERCENT",
                        "observations": [18.4 - index * 0.01 for index in range(10)],
                        "maximum_observed": 18.4,
                        "simulator_value": "configured by encounter execute proportion",
                        "unit": "target_health_percent",
                    },
                    _parameter("warrior.execute", "miss_refund_fraction", 0, "OBSERVED_DIFFERS_SINGLE_TRIAL", 1, 0.8, "fraction_of_effective_base_cost"),
                    _parameter("warrior.execute", "extra_rage_retained_on_miss", True, "OBSERVED_DIFFERS_SINGLE_TRIAL", 1, False, "boolean"),
                ],
            },
            {
                "task_id": "warrior_bloodthirst_until_crit",
                "task_run_id": "crit-1",
                "completion_confirmed": True,
                "total_damage_attempts": 6,
                "evidence_comparisons": [
                    _parameter("warrior.bloodthirst", "miss_refund_fraction", 0, "OBSERVED_DIFFERS_SINGLE_TRIAL", 1, 0.8, "fraction_of_effective_base_cost"),
                ],
            },
        ],
    }


def _registry() -> dict:
    return {
        "schema_version": 1,
        "game_patch": "Turtle WoW 1.18.1",
        "overall_status": "implemented_in_wowsims_turtle_but_not_game_calibrated",
        "mechanics": [
            {"key": "warrior.bloodthirst", "status": "implemented_not_game_calibrated", "implementation": {}, "evidence": []},
            {"key": "warrior.heroic_strike.queue", "status": "implemented_not_game_calibrated", "implementation": {}, "evidence": []},
            {"key": "warrior.execute", "status": "implemented_not_game_calibrated", "implementation": {}, "evidence": []},
        ],
        "calibration_required": [],
    }


def _slam_summary() -> dict:
    trials = []
    for trial, mode, flurry, cast_ms, classification in (
        (2, "no_flurry_late", False, 2379, "released_at_server_go"),
        (3, "no_flurry_late", False, 2379, "released_at_server_go"),
        (4, "flurry_early", True, 1830, "preserved_precast_deadline"),
        (5, "flurry_late", True, 1830, "released_at_server_go"),
        (6, "flurry_late", True, 1830, "released_at_server_go"),
    ):
        trials.append(
            {
                "trial": trial,
                "mode": mode,
                "result_spell_id": 45961,
                "cast_timing_ms": {"advertised": cast_ms},
                "precast": {"flurry_active": flurry},
                "swing_deadline": {"classification": classification},
                "rage_drop_evidence": {
                    "identifiable": trial == 4,
                    "net_drop": 15 if trial == 4 else 0,
                },
            }
        )
    return {
        "schema_version": 2,
        "kind": "brainofcat_calibration_summary",
        "game_patch": "Turtle WoW 1.18.1",
        "source": {"calibration_jsonl": "slam.jsonl"},
        "task_completions": [
            {
                "task_id": "warrior_slam_timing_transition",
                "task_run_id": "slam-1",
                "analysis_status": "partial_retained",
                "completed_trials": 6,
                "retained_evidence_trials": 5,
            }
        ],
        "campaigns": [
            {
                "campaign_id": "warrior_fury_dummy_slam_phase2",
                "campaign_run_id": "campaign-slam-1",
                "completed_task_count": 1,
            }
        ],
        "deferred_analysis": [],
        "runs": [],
        "specialized_runs": [
            {
                "task_id": "warrior_slam_timing_transition",
                "task_run_id": "slam-1",
                "terminal_completion_confirmed": True,
                "requested_trials": 6,
                "completed_trials": 6,
                "retained_evidence_trials": 5,
                "missing_trial_numbers": [1],
                "missing_trial_modes": ["no_flurry_early"],
                "timing_coverage_sufficient": True,
                "rage_cost_coverage_sufficient": True,
                "trials": trials,
            }
        ],
    }


def _slam_registry() -> dict:
    return {
        "schema_version": 2,
        "game_patch": "Turtle WoW 1.18.1",
        "overall_status": "partially_game_calibrated",
        "mechanics": [
            {
                "key": "warrior.slam",
                "status": "official_and_static_aligned_awaiting_live_calibration",
                "implementation": {
                    "rage_cost": 15,
                    "result_spell_id": 53214,
                    "cast_speed_model": "base divided by melee speed",
                    "main_hand_swing_deadline": "continues unchanged",
                },
                "evidence": [],
                "calibration": {"status": "awaiting_current_game_six_trial_campaign"},
            }
        ],
        "calibration_required": [],
    }


def _phase3_summary() -> dict:
    return {
        "schema_version": 2,
        "kind": "brainofcat_calibration_summary",
        "game_patch": "Turtle WoW 1.18.1",
        "source": {"calibration_jsonl": "phase3.jsonl"},
        "task_completions": [
            {
                "task_id": task_id,
                "task_run_id": run_id,
                "analysis_status": "detailed",
                "completed_trials": count,
            }
            for task_id, run_id, count in (
                (
                    "warrior_whirlwind_cooldown_transition",
                    "phase3-whirlwind-1",
                    1,
                ),
                ("warrior_cleave_queue_swing", "phase3-cleave-1", 1),
                ("warrior_white_swing_rage_transition", "phase3-white-1", 3),
            )
        ],
        "campaigns": [
            {
                "campaign_id": "warrior_fury_dummy_ravager_rage_phase3",
                "campaign_run_id": "phase3-campaign-1",
                "completed_task_count": 3,
            }
        ],
        "deferred_analysis": [],
        "runs": [],
        "specialized_runs": [
            {
                "task_id": "warrior_whirlwind_cooldown_transition",
                "task_run_id": "phase3-whirlwind-1",
                "terminal_completion_confirmed": True,
                "requested_trials": 1,
                "completed_trials": 1,
                "retained_evidence_trials": 1,
                "promotion_gate": {
                    "ready": True,
                    "exact_duration_source": "GetSpellCooldown.duration",
                    "successful_go_interval_is_supporting_only": True,
                },
                "talent_context": {
                    "ravager": {"rank": 2, "rank2_confirmed": True}
                },
                "evidence_comparisons": [
                    _parameter(
                        "warrior.whirlwind",
                        "current_character_cooldown_seconds",
                        8.5,
                        "CONSISTENT",
                        1,
                        8.5,
                        "seconds",
                    )
                ],
            },
            {
                "task_id": "warrior_cleave_queue_swing",
                "task_run_id": "phase3-cleave-1",
                "terminal_completion_confirmed": True,
                "requested_trials": 1,
                "completed_trials": 1,
                "retained_evidence_trials": 1,
                "promotion_gate": {"ready": True},
                "talent_context": {
                    "ravager": {"rank": 2, "rank2_confirmed": True}
                },
                "evidence_comparisons": [
                    _parameter(
                        "warrior.cleave.queue",
                        "current_character_rage_cost",
                        18,
                        "CONSISTENT",
                        1,
                        18,
                        "rage",
                    ),
                    _parameter(
                        "warrior.cleave.queue",
                        "replaces_next_main_hand_swing",
                        True,
                        "CONSISTENT",
                        1,
                        True,
                        "boolean",
                    ),
                ],
            },
            {
                "task_id": "warrior_white_swing_rage_transition",
                "task_run_id": "phase3-white-1",
                "terminal_completion_confirmed": True,
                "requested_trials": 3,
                "completed_trials": 3,
                "retained_evidence_trials": 3,
                "observation_gate": {
                    "status": "observations_only",
                    "clean_sample_count": 3,
                    "hand_coverage": ["main_hand"],
                    "outcome_coverage": ["critical", "glancing", "ordinary"],
                    "formula_identified": False,
                    "registry_promotion_allowed": False,
                },
            },
        ],
    }


def _phase3_registry() -> dict:
    return {
        "schema_version": 2,
        "game_patch": "Turtle WoW 1.18.1",
        "overall_status": "partially_game_calibrated",
        "mechanics": [
            {
                "key": "warrior.whirlwind",
                "status": "implemented_not_game_calibrated",
                "implementation": {
                    "current_character_ravager_rank": 2,
                    "current_character_cooldown_seconds": 8.5,
                },
                "evidence": [],
            },
            {
                "key": "warrior.cleave.queue",
                "status": "implemented_not_game_calibrated",
                "implementation": {
                    "current_character_ravager_rank": 2,
                    "current_character_rage_cost": 18,
                    "replaces_next_main_hand_swing": True,
                },
                "evidence": [],
            },
            {
                "key": "warrior.ravager",
                "status": "static_profile_mapped_awaiting_live_calibration",
                "implementation": {"current_character_rank": 2},
                "evidence": [],
            },
        ],
        "calibration_required": [],
    }


class PromoteCalibrationTests(unittest.TestCase):
    def test_phase3_promotes_only_exact_duration_and_cleave_chain(self) -> None:
        report, registry = build_phase3_calibration_promotion(
            _phase3_summary(),
            _phase3_registry(),
            summary_path=PROJECT_ROOT / "phase3-summary.json",
            registry_path=PROJECT_ROOT / "registry.json",
            output_path=PROJECT_ROOT / "phase3-acceptance.json",
        )

        self.assertEqual(
            report["decision"], "phase3_ravager_rank2_and_queue_promotion"
        )
        self.assertEqual(len(report["accepted_parameters"]), 2)
        self.assertEqual(len(report["supporting_parameters"]), 1)
        self.assertEqual(len(report["unresolved_parameters"]), 2)
        self.assertEqual(
            report["simulator_replay_status"], "NOT_RECORDED_BY_PROMOTION"
        )
        self.assertFalse(
            report["white_rage_observation_gate"]["formula_identified"]
        )
        mechanics = {item["key"]: item for item in registry["mechanics"]}
        self.assertEqual(
            mechanics["warrior.whirlwind"]["calibration"]["parameters"][
                "current_character_cooldown_seconds"
            ]["evidence_role"],
            "GetSpellCooldown duration is the exact value; the successful GO interval is consistency evidence only",
        )
        self.assertEqual(
            mechanics["warrior.cleave.queue"]["calibration"]["parameters"][
                "current_character_rage_cost"
            ]["status"],
            "observed_single_clean_transition",
        )
        self.assertEqual(
            mechanics["warrior.ravager"]["calibration"]["observed_rank"], 2
        )
        self.assertNotIn(
            "warrior.white_swing_rage",
            mechanics,
        )

    def test_phase3_rejects_successful_go_interval_without_exact_duration_gate(self) -> None:
        summary = _phase3_summary()
        summary["specialized_runs"][0]["promotion_gate"]["ready"] = False
        with self.assertRaisesRegex(
            CalibrationPromotionError, "exact-duration promotion gate is not ready"
        ):
            build_phase3_calibration_promotion(
                summary,
                _phase3_registry(),
                summary_path=PROJECT_ROOT / "phase3-summary.json",
                registry_path=PROJECT_ROOT / "registry.json",
                output_path=PROJECT_ROOT / "phase3-acceptance.json",
            )

    def test_phase3_cli_dispatches_and_preserves_white_formula_nonclaim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary_path = root / "phase3-summary.json"
            registry_path = root / "registry.json"
            output_path = root / "phase3-acceptance.json"
            summary_path.write_text(json.dumps(_phase3_summary()), encoding="utf-8")
            registry_path.write_text(json.dumps(_phase3_registry()), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                return_code = main(
                    [
                        str(summary_path),
                        "--registry",
                        str(registry_path),
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(return_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["decision"], "phase3_ravager_rank2_and_queue_promotion"
            )
            white_unresolved = next(
                item
                for item in report["unresolved_parameters"]
                if item["mechanic"] == "warrior.white_swing_rage"
            )
            self.assertEqual(
                white_unresolved["status"], "observations_only_not_identified"
            )

    def test_build_promotes_only_identified_parameters(self) -> None:
        report, registry = build_calibration_promotion(
            _summary(),
            _registry(),
            summary_path=PROJECT_ROOT / "summary.json",
            registry_path=PROJECT_ROOT / "registry.json",
            output_path=PROJECT_ROOT / "acceptance.json",
        )
        self.assertEqual(report["decision"], "partial_registry_promotion")
        self.assertEqual(len(report["accepted_parameters"]), 5)
        self.assertEqual(len(report["supporting_parameters"]), 4)
        self.assertEqual(len(report["unresolved_parameters"]), 7)
        self.assertEqual(report["simulator_overrides"], [])
        mechanics = {item["key"]: item for item in registry["mechanics"]}
        self.assertEqual(registry["overall_status"], "partially_game_calibrated")
        self.assertEqual(
            mechanics["warrior.bloodthirst"]["calibration"]["parameters"]["rage_cost"]["status"],
            "verified_deterministic",
        )
        self.assertNotIn(
            "rage_cost",
            mechanics["warrior.heroic_strike.queue"]["calibration"]["parameters"],
        )
        self.assertNotIn(
            "consumes_gcd",
            mechanics["warrior.heroic_strike.queue"]["calibration"]["parameters"],
        )
        self.assertNotIn(
            "base_rage_cost",
            mechanics["warrior.execute"]["calibration"]["parameters"],
        )
        self.assertEqual(
            {
                item["field"]
                for item in mechanics["warrior.heroic_strike.queue"]["calibration"][
                    "unresolved"
                ]
            },
            {"miss_refund_fraction"},
        )

    def test_inconsistent_deterministic_value_is_rejected(self) -> None:
        summary = _summary()
        summary["runs"][0]["parameters"][0]["estimate"] = 29
        with self.assertRaisesRegex(CalibrationPromotionError, "expected 30"):
            build_calibration_promotion(
                summary,
                _registry(),
                summary_path=PROJECT_ROOT / "summary.json",
                registry_path=PROJECT_ROOT / "registry.json",
                output_path=PROJECT_ROOT / "acceptance.json",
            )

    def test_cli_writes_acceptance_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary_path = root / "summary.json"
            registry_path = root / "registry.json"
            output_path = root / "acceptance.json"
            summary_path.write_text(json.dumps(_summary()), encoding="utf-8")
            registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                return_code = main(
                    [
                        str(summary_path),
                        "--registry",
                        str(registry_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(return_code, 0)
            self.assertTrue(output_path.is_file())
            updated = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["schema_version"], 2)
            self.assertEqual(updated["overall_status"], "partially_game_calibrated")

    def test_slam_promotes_retained_timing_and_cost_coverage(self) -> None:
        report, registry = build_slam_calibration_promotion(
            _slam_summary(),
            _slam_registry(),
            summary_path=PROJECT_ROOT / "slam-summary.json",
            registry_path=PROJECT_ROOT / "registry.json",
            output_path=PROJECT_ROOT / "slam-acceptance.json",
        )

        self.assertEqual(report["decision"], "slam_timing_and_cost_promotion")
        self.assertEqual(len(report["accepted_parameters"]), 3)
        self.assertEqual(len(report["supporting_parameters"]), 1)
        self.assertEqual(report["campaign"]["completed_trial_count"], 6)
        self.assertEqual(report["campaign"]["retained_evidence_trial_count"], 5)
        self.assertEqual(
            report["retention_limitations"]["missing_trial_numbers"], [1]
        )
        slam = registry["mechanics"][0]
        self.assertEqual(slam["status"], "partially_game_calibrated")
        self.assertEqual(slam["implementation"]["result_spell_id"], 45961)
        self.assertIn("release the pending swing", slam["implementation"]["main_hand_swing_deadline"])
        self.assertEqual(
            slam["calibration"]["parameters"]["rage_cost"]["observed_value"],
            15,
        )
        self.assertEqual(
            slam["calibration"]["parameters"]["rage_cost"]["status"],
            "observed_single_clean_transition",
        )
        self.assertEqual(
            slam["calibration"]["parameters"]["result_spell_id"]["simulator_value"],
            45961,
        )
        self.assertEqual(
            slam["calibration"]["parameters"]["result_spell_id"]["previous_registry_value"],
            53214,
        )

    def test_slam_rejects_missing_timing_coverage(self) -> None:
        summary = _slam_summary()
        summary["specialized_runs"][0]["timing_coverage_sufficient"] = False
        with self.assertRaisesRegex(
            CalibrationPromotionError, "timing coverage is insufficient"
        ):
            build_slam_calibration_promotion(
                summary,
                _slam_registry(),
                summary_path=PROJECT_ROOT / "slam-summary.json",
                registry_path=PROJECT_ROOT / "registry.json",
                output_path=PROJECT_ROOT / "slam-acceptance.json",
            )

    def test_slam_cli_dispatches_and_updates_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary_path = root / "slam-summary.json"
            registry_path = root / "registry.json"
            output_path = root / "slam-acceptance.json"
            summary_path.write_text(json.dumps(_slam_summary()), encoding="utf-8")
            registry_path.write_text(json.dumps(_slam_registry()), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                return_code = main(
                    [
                        str(summary_path),
                        "--registry",
                        str(registry_path),
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(return_code, 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["decision"], "slam_timing_and_cost_promotion")
            updated = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(
                updated["mechanics"][0]["calibration"]["status"],
                "timing_and_cost_game_calibrated",
            )

    def test_armor_strata_cannot_fall_through_generic_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary_path = root / "armor-summary.json"
            registry_path = root / "registry.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "specialized_runs": [
                            {
                                "task_id": "warrior_bloodthirst_armor_strata_damage",
                                "analyzer": "bloodthirst_armor_strata_damage_v1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
            with self.assertRaisesRegex(
                CalibrationPromotionError, "dedicated matched simulator replay"
            ):
                promote_calibration(summary_path, registry=registry_path)


if __name__ == "__main__":
    unittest.main()
