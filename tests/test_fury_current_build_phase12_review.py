import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_current_build_phase12_review import (
    DEFAULT_PREREGISTRATION,
    FuryPhase12ReviewError,
    build_phase12_review,
    run_from_path,
)


def _step(name: str, *sequences: int, satisfied: bool = True) -> dict:
    return {
        "name": name,
        "satisfied": satisfied,
        "sequences": list(sequences),
    }


def _trial(
    stage_id: str,
    steps: list[dict],
    *,
    complete: bool = True,
    failure_reasons: list[str] | None = None,
    moving_observations: list[bool] | None = None,
) -> dict:
    return {
        "stage_id": stage_id,
        "stage_status": "completed" if complete else "incomplete",
        "status": "completed" if complete else "incomplete",
        "exact_action_chain_complete": complete,
        "completion_sequence": max(
            (sequence for step in steps for sequence in step["sequences"]),
            default=1,
        ),
        "combined_evidence_source": "repair" if not complete else "base",
        "environment_evidence": {
            "observed": {"moving": moving_observations or []}
        },
        "exact_action_chain": {
            "exact_action_chain_complete": complete,
            "steps": steps,
            "failure_reasons": failure_reasons or [],
        },
    }


def _task(task_id: str, trials: list[dict]) -> dict:
    return {
        "task_id": task_id,
        "task_run_id": f"run-{task_id}",
        "trials": trials,
    }


class FuryCurrentBuildPhase12ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.preregistration = json.loads(
            DEFAULT_PREREGISTRATION.read_text(encoding="utf-8")
        )

    def _audit(self) -> dict:
        gates = {
            name: {
                "status": "COMPLETE",
                "current_tasks": [],
                "retest_required_stage_ids": [],
            }
            for name in self.preregistration["mechanism_gates"]
        }
        gates["movement_range"] = {
            "status": "PARTIAL",
            "retest_required_stage_ids": ["slam_moving"],
            "current_tasks": [
                _task(
                    "warrior_fury_movement_range_latency_phase12",
                    [
                        _trial(
                            "slam_stationary",
                            [
                                _step("action_requested", 10),
                                _step("client_cast_succeeded", 11),
                                _step("server_go_observed", 12),
                                _step("result_observed", 13),
                            ],
                        ),
                        _trial(
                            "slam_moving",
                            [
                                _step("action_requested", 20),
                                _step("client_cast_succeeded", 21),
                                _step("spell_start_observed", 22),
                                _step("server_go_observed", 23),
                                _step("result_observed", 24),
                                _step(
                                    "terminal_coverage_observed", satisfied=False
                                ),
                            ],
                            complete=False,
                            failure_reasons=[
                                "terminal_marker_reports_partial_or_deferred_coverage",
                            ],
                            moving_observations=[False, True],
                        ),
                        _trial(
                            "ww_outside_8",
                            [
                                _step("action_requested", 25),
                                _step("cast_succeeded", 26),
                                _step("server_go_observed", 27),
                                _step("zero_targets_hit_observed", 27),
                                _step(
                                    "terminal_coverage_observed", satisfied=False
                                ),
                            ],
                            complete=False,
                            failure_reasons=[
                                "terminal_marker_reports_partial_or_deferred_coverage",
                            ],
                        ),
                    ],
                )
            ],
        }
        gates["queue"] = {
            "status": "PARTIAL",
            "retest_required_stage_ids": ["hs_cancel"],
            "current_tasks": [
                _task(
                    "warrior_fury_queue_execution_phase12",
                    [
                        _trial(
                            "hs_stance_preserve",
                            [
                                _step("initial_hs_queue_observed", 30),
                                _step("stance_server_go_observed", 31),
                                _step("stance_aura_observed", 32),
                                _step("queued_hs_server_go_observed", 33),
                                _step("queued_hs_result_observed", 34),
                            ],
                        ),
                        _trial(
                            "hs_cancel",
                            [_step("initial_hs_queue_observed", 35)],
                            complete=False,
                            failure_reasons=["missing_queue_pop_after_cancel_step"],
                        ),
                    ],
                )
            ],
        }
        gates["burst_stance"] = {
            "status": "COMPLETE",
            "retest_required_stage_ids": [],
            "current_tasks": [
                _task(
                    "warrior_fury_self_buffs_stances_phase12",
                    [
                        _trial(
                            "bloodrage",
                            [
                                _step("bloodrage_immediate_energize", 40, 41),
                                _step("bloodrage_periodic_ticks", *range(42, 62)),
                            ],
                        ),
                        *[
                            _trial(
                                stage_id,
                                [
                                    _step("client_cast_succeeded", sequence),
                                    _step("server_go_observed", sequence + 1),
                                    _step("stance_aura_observed", sequence + 2),
                                ],
                            )
                            for stage_id, sequence in (
                                ("stance_battle", 70),
                                ("stance_defensive", 80),
                                ("stance_berserker", 90),
                            )
                        ],
                        _trial(
                            "death_wish_dedup",
                            [
                                _step("deduplication_marker", 100),
                                _step("prior_aura_observed", 101),
                            ],
                        ),
                        _trial(
                            "recklessness_dedup",
                            [
                                _step("client_cast_succeeded", 110),
                                _step("buff_aura_observed", 111),
                            ],
                        ),
                    ],
                )
            ],
        }
        gates["flurry_deep_wounds"] = {
            "status": "COMPLETE",
            "retest_required_stage_ids": [],
            "current_tasks": [
                _task(
                    "warrior_fury_flurry_deep_wounds_phase12",
                    [
                        _trial(
                            "death_wish_window",
                            [_step("death_wish_aura_observed", 120)],
                        ),
                        _trial(
                            "flurry_dw_post_death_wish",
                            [_step("main_hand_swing_observed", 121, 122)],
                        ),
                        _trial(
                            "flurry_refresh",
                            [
                                _step("critical_swing_observed", 123),
                                _step("flurry_aura_observed", 124),
                            ],
                        ),
                        _trial(
                            "flurry_timer_rescale",
                            [
                                _step("critical_swing_observed", 125),
                                _step("flurry_aura_observed", 126),
                                _step("post_flurry_swing_observed", 127),
                            ],
                        ),
                    ],
                )
            ],
        }
        gates["dual_wield"] = {
            "status": "PARTIAL",
            "retest_required_stage_ids": ["dual_hs_cancel"],
            "current_tasks": [
                _task(
                    "warrior_fury_dual_wield_mechanics_phase12",
                    [
                        _trial(
                            "dual_unqueued_1",
                            [
                                _step("main_hand_swing_observed", 130),
                                _step("off_hand_swing_observed", 131),
                            ],
                        ),
                        _trial(
                            "dual_hs_queued_1",
                            [
                                _step("on_swing_queue_observed", 132),
                                _step("off_hand_swing_observed", 133),
                            ],
                        ),
                        _trial(
                            "dual_hs_cancel",
                            [_step("initial_hs_queue_observed", 134)],
                            complete=False,
                            failure_reasons=["missing_queue_pop_after_cancel_step"],
                        ),
                    ],
                )
            ],
        }
        return {
            "schema_version": 1,
            "kind": "fury_current_build_phase12_audit",
            "status": "partial",
            "campaign_id": self.preregistration["campaign_id"],
            "campaign_run_id": "base-run",
            "repair_campaign_run_id": "repair-run",
            "analyzer": self.preregistration["analyzer"],
            "scope": copy.deepcopy(self.preregistration["scope"]),
            "sources": {"calibration_summary": "summary.json"},
            "completion_gate": {
                "retest_required_stage_ids": [
                    "dual_hs_cancel",
                    "hs_cancel",
                    "slam_moving",
                    "ww_outside_8",
                ]
            },
            "mechanism_gates": gates,
            "external_mechanism_boundaries": copy.deepcopy(
                self.preregistration["external_mechanism_boundaries"]
            ),
            "conclusion_gate": {
                "campaign_terminal": False,
                "collection_complete": False,
                "simulator_patch_allowed": False,
                "simulator_patch": None,
            },
            "simulator_overrides": [],
        }

    @staticmethod
    def _summary() -> dict:
        return {
            "kind": "brainofcat_calibration_summary",
            "specialized_campaigns": [
                {
                    "campaign_id": "warrior_fury_current_build_dummy_phase12_repair_v2",
                    "campaign_run_id": "repair-run",
                    "terminal_event": "CALIBRATION_CAMPAIGN_INCOMPLETE",
                    "status": "coverage_partial",
                    "simulator_patch_allowed": False,
                    "simulator_patch": None,
                }
            ],
        }

    @staticmethod
    def _action_state_evidence(*, moving: bool = True) -> dict:
        return {
            "source": "calibration.jsonl",
            "rows_scanned": 1,
            "retained_row_count": 1,
            "stages": {
                "slam_moving": [
                    {
                        "sequence": 20,
                        "event": "CALIBRATION_ACTION_REQUESTED",
                        "task_stage_id": "slam_moving",
                        "marker_stage_id": "slam_moving",
                        "state_moving": moving,
                        "marker_moving": moving,
                        "moving_field_provenance": "INFERRED_MAP_DELTA",
                    }
                ]
            },
        }

    def _build(
        self,
        audit: dict | None = None,
        summary: dict | None = None,
        action_state_evidence: dict | None = None,
    ) -> dict:
        return build_phase12_review(
            audit or self._audit(),
            summary or self._summary(),
            copy.deepcopy(self.preregistration),
            audit_path=Path("audit.json"),
            summary_path=Path("summary.json"),
            preregistration_path=DEFAULT_PREREGISTRATION,
            action_state_evidence=(
                action_state_evidence
                if action_state_evidence is not None
                else self._action_state_evidence()
            ),
        )

    def test_partial_gate_completed_stage_is_promoted_as_fact_only(self) -> None:
        document = self._build()

        queue = document["evidence_reviews"]["queue_cross_stance"]
        self.assertEqual(
            queue["promotion_decision"]["decision"],
            "PROMOTE_OBSERVED_FACT_ONLY",
        )
        self.assertTrue(
            queue["promotion_decision"][
                "stage_level_promotion_despite_partial_gate"
            ]
        )
        self.assertIn(
            "hs_cancel",
            queue["observed_fact"]["incomplete_or_missing_stage_ids"],
        )
        movement = document["evidence_reviews"]["movement_slam"]
        self.assertEqual(
            movement["observed_fact"]["completed_support_stage_ids"],
            ["slam_stationary"],
        )
        self.assertIn(
            "slam_moving",
            movement["observed_fact"][
                "historically_adjudicated_support_stage_ids"
            ],
        )
        self.assertNotIn(
            "whether Turtle Slam is castable while moving",
            movement["promotion_decision"]["unresolved"],
        )

    def test_historical_contract_adjudicates_slam_and_whirlwind_not_retest(self) -> None:
        document = self._build()

        adjudication = document["historical_contract_adjudication"]
        self.assertEqual(
            adjudication["stages"]["slam_moving"]["decision"],
            "ADJUDICATE_OBSERVED_FACT_NOT_RETEST",
        )
        self.assertTrue(
            adjudication["stages"]["slam_moving"][
                "associated_action_moving_true"
            ]
        )
        self.assertEqual(
            adjudication["stages"]["ww_outside_8"]["decision"],
            "ADJUDICATE_OBSERVED_FACT_NOT_RETEST",
        )
        source_context = document["source_gate_context"]
        self.assertIn(
            "slam_moving", source_context["source_retest_required_stage_ids"]
        )
        self.assertIn(
            "ww_outside_8", source_context["source_retest_required_stage_ids"]
        )
        self.assertEqual(
            source_context["review_adjudicated_not_retest_stage_ids"],
            ["slam_moving", "ww_outside_8"],
        )
        self.assertEqual(
            source_context["review_remaining_retest_required_stage_ids"],
            ["dual_hs_cancel", "hs_cancel"],
        )

        whirlwind = document["evidence_reviews"][
            "whirlwind_no_current_target_hit"
        ]
        self.assertEqual(
            whirlwind["promotion_decision"]["decision"],
            "PROMOTE_OBSERVED_FACT_ONLY",
        )
        self.assertIn(
            "exact Whirlwind hit radius",
            whirlwind["promotion_decision"]["unresolved"],
        )

    def test_slam_historical_adjudication_requires_associated_moving_action(self) -> None:
        document = self._build(
            action_state_evidence=self._action_state_evidence(moving=False)
        )

        slam = document["historical_contract_adjudication"]["stages"][
            "slam_moving"
        ]
        self.assertEqual(slam["decision"], "RETEST_REMAINS")
        movement = document["evidence_reviews"]["movement_slam"]
        self.assertIn(
            "whether Turtle Slam is castable while moving",
            movement["promotion_decision"]["unresolved"],
        )
        self.assertNotIn(
            "slam_moving",
            document["source_gate_context"][
                "review_adjudicated_not_retest_stage_ids"
            ],
        )

    def test_event_facts_do_not_promote_unmeasured_parameters(self) -> None:
        document = self._build()

        stance = document["evidence_reviews"]["stance_rage_retention"]
        self.assertIn(
            "the exact retained-rage cap for the observed talent build",
            stance["promotion_decision"]["unresolved"],
        )
        duration = document["evidence_reviews"][
            "death_wish_recklessness_duration"
        ]
        self.assertIn(
            "Recklessness aura duration",
            duration["promotion_decision"]["unresolved"],
        )
        bloodrage = document["evidence_reviews"]["bloodrage_event_contract"]
        self.assertIn(
            "unique periodic tick count after mirrored-event deduplication",
            bloodrage["promotion_decision"]["unresolved"],
        )
        self.assertEqual(
            bloodrage["simulator_comparison"]["status"], "NOT_EVALUATED"
        )

    def test_flurry_and_dual_wield_are_qualitative_only(self) -> None:
        document = self._build()

        flurry = document["evidence_reviews"]["flurry_death_wish_qualitative"]
        self.assertEqual(
            flurry["promotion_decision"]["decision"],
            "PROMOTE_OBSERVED_FACT_ONLY",
        )
        self.assertIn(
            "Flurry haste multiplier", flurry["promotion_decision"]["unresolved"]
        )
        dual = document["evidence_reviews"]["dual_wield_qualitative"]
        self.assertTrue(
            dual["promotion_decision"][
                "stage_level_promotion_despite_partial_gate"
            ]
        )
        self.assertIn(
            "dual-wield miss penalty", dual["promotion_decision"]["unresolved"]
        )

    def test_review_never_emits_registry_or_simulator_mutations(self) -> None:
        document = self._build()

        self.assertFalse(document["publication_gate"]["simulator_patch_allowed"])
        self.assertIsNone(document["publication_gate"]["simulator_patch"])
        self.assertEqual(document["mechanics_registry_mutations"], [])
        self.assertEqual(document["simulator_overrides"], [])
        self.assertFalse(document["mechanics_registry_modified"])
        self.assertFalse(document["simulator_modified"])
        for review in document["evidence_reviews"].values():
            self.assertIsNone(
                review["promotion_decision"]["simulator_override"]
            )

    def test_rejects_any_source_simulator_override_or_patch_authority(self) -> None:
        audit = self._audit()
        audit["simulator_overrides"] = [{"mechanic": "warrior.slam"}]
        with self.assertRaisesRegex(
            FuryPhase12ReviewError, "contains simulator overrides"
        ):
            self._build(audit=audit)

        audit = self._audit()
        audit["conclusion_gate"]["simulator_patch_allowed"] = True
        with self.assertRaisesRegex(
            FuryPhase12ReviewError, "attempts to authorize a simulator patch"
        ):
            self._build(audit=audit)

    def test_rejects_summary_not_bound_to_audited_run(self) -> None:
        summary = self._summary()
        summary["specialized_campaigns"][0]["campaign_run_id"] = "other-run"
        with self.assertRaisesRegex(FuryPhase12ReviewError, "not bound"):
            self._build(summary=summary)

    def test_run_from_path_uses_audit_summary_source_and_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audit_path = root / "audit.json"
            summary_path = root / "summary.json"
            preregistration_path = root / "preregistration.json"
            output = root / "review.json"
            audit = self._audit()
            audit["sources"]["calibration_summary"] = str(summary_path)
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            summary_path.write_text(json.dumps(self._summary()), encoding="utf-8")
            preregistration_path.write_text(
                json.dumps(self.preregistration), encoding="utf-8"
            )

            document = run_from_path(
                audit_path,
                preregistration_path=preregistration_path,
                output=output,
            )

            self.assertTrue(output.is_file())
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], document["status"])
            self.assertEqual(written["simulator_overrides"], [])


if __name__ == "__main__":
    unittest.main()
