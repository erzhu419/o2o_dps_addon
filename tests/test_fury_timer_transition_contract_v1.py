from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_timer_transition_contract_v1 import (
    DEFAULT_COMPARISON,
    DEFAULT_LIVE_SUMMARY,
    DEFAULT_REGISTRY,
    DEFAULT_REVIEW,
    LIVE_SUMMARY_KIND,
    LIVE_SUMMARY_SCHEMA,
    REQUIRED_COMPONENTS,
    TIMER_FIELDS,
    FuryTimerTransitionContractError,
    build_fury_timer_transition_contract,
    main,
    write_fury_timer_transition_contract,
)


class FuryTimerTransitionContractV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.live_summary = json.loads(
            DEFAULT_LIVE_SUMMARY.read_text(encoding="utf-8")
        )
        cls.report = build_fury_timer_transition_contract()

    def _write_live_summary(self, directory: str, value: object) -> Path:
        path = Path(directory) / "live-summary.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def test_inventory_is_readiness_only_and_never_reads_decision_rows(self) -> None:
        self.assertEqual(self.report["status"], "ok")
        self.assertEqual(self.report["scope"]["compact_rows_read"], 0)
        self.assertEqual(self.report["scope"]["decision_rows_rewritten"], 0)
        self.assertFalse(self.report["scope"]["full_state"])
        self.assertFalse(self.report["scope"]["behavior_cloning_started"])
        self.assertFalse(self.report["scope"]["offline_rl_started"])
        self.assertFalse(self.report["summary"]["registry_modified"])
        self.assertFalse(self.report["summary"]["wowsims_modified"])
        self.assertFalse(self.report["summary"]["addon_modified"])

    def test_all_four_remaining_fields_stay_blocked_after_live_contract(self) -> None:
        self.assertEqual(tuple(self.report["timers"]), TIMER_FIELDS)
        for field in TIMER_FIELDS:
            timer = self.report["timers"][field]
            self.assertEqual(set(timer["components"]), set(REQUIRED_COMPONENTS))
            self.assertTrue(timer["duration_is_not_remaining"])
            self.assertFalse(timer["reconstruction_allowed"])
            self.assertTrue(timer["blockers"])
        self.assertEqual(self.report["summary"]["reconstruction_allowed_count"], 0)
        self.assertTrue(
            self.report["summary"]["all_remaining_reconstruction_blocked"]
        )
        self.assertEqual(self.report["summary"]["compact_rows_read"], 0)
        self.assertFalse(
            self.report["summary"][
                "historical_reconstruction_allowed_by_live_summary"
            ]
        )

    def test_live_summary_schema_kind_status_and_gate_are_strict(self) -> None:
        mutations = {
            "schema": lambda value: value.__setitem__("schema", "wrong/v1"),
            "kind": lambda value: value.__setitem__("kind", "wrong"),
            "status": lambda value: value.__setitem__("status", "incomplete"),
            "gate": lambda value: value["evidence_gate"].__setitem__(
                "live_contract_complete", False
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                bad = copy.deepcopy(self.live_summary)
                mutate(bad)
                path = self._write_live_summary(directory, bad)
                with self.assertRaises(FuryTimerTransitionContractError):
                    build_fury_timer_transition_contract(live_summary_path=path)

    def test_live_source_and_recovery_run_link_is_required(self) -> None:
        live = self.report["live_source_recovery_contract"]
        self.assertEqual(live["schema"], LIVE_SUMMARY_SCHEMA)
        self.assertEqual(live["kind"], LIVE_SUMMARY_KIND)
        self.assertEqual(live["status"], "complete")
        self.assertTrue(live["composite_validation"]["trace_links_exact"])
        self.assertTrue(live["composite_validation"]["all_14_checks_true"])
        self.assertTrue(
            live["inputs"]["source_campaign_run_id"].startswith("timer-campaign-")
        )
        self.assertTrue(
            live["inputs"]["recovery_campaign_run_id"].startswith(
                "timer-recovery-"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            bad = copy.deepcopy(self.live_summary)
            bad["inputs"]["source_campaign_run_id"] = "wrong-source"
            path = self._write_live_summary(directory, bad)
            with self.assertRaises(FuryTimerTransitionContractError):
                build_fury_timer_transition_contract(live_summary_path=path)

    def test_stage_a_bloodthirst_retry_does_not_extend_ready_at(self) -> None:
        timer = self.report["timers"]["cooldown_remaining_ms"]
        start = timer["components"]["timer_start"]
        reset = timer["components"]["reset_cancel"]
        self.assertEqual(start["status"], "LIVE_CALIBRATED_PARTIAL")
        self.assertEqual(reset["status"], "LIVE_CALIBRATED_PARTIAL")
        retries = reset["evidence"]["bloodthirst_active_retry_confirmations"]
        self.assertEqual(len(retries), 3)
        for retry in retries:
            self.assertEqual(retry["evidence_kind"], "explicit_failure_event")
            self.assertLess(
                retry["remaining_after_seconds"], retry["remaining_before_seconds"]
            )
            self.assertLessEqual(abs(retry["timer_end_delta_seconds"]), 0.01)
        with tempfile.TemporaryDirectory() as directory:
            bad = copy.deepcopy(self.live_summary)
            retries = bad["stages"]["A_timer_chains"]["spells"][
                "bloodthirst"
            ]["active_retry_confirmations"]
            retries[0]["timer_end_delta_seconds"] = 0.5
            path = self._write_live_summary(directory, bad)
            with self.assertRaises(FuryTimerTransitionContractError):
                build_fury_timer_transition_contract(live_summary_path=path)

    def test_stage_b_calibrates_independent_dual_wield_intervals(self) -> None:
        stage = self.report["live_source_recovery_contract"]["stages"][
            "B_dual_wield_intervals"
        ]
        self.assertTrue(stage["complete"])
        self.assertEqual(stage["locked_loadout"]["source_kind"], "TIMEOUT_snapshot")
        self.assertEqual(stage["locked_loadout"]["main_hand_item_id"], 18832)
        self.assertEqual(stage["locked_loadout"]["off_hand_item_id"], 19866)
        for hand in ("main_hand", "off_hand"):
            self.assertGreaterEqual(len(stage["baseline"][hand]["intervals_seconds"]), 3)
            self.assertIn(hand, stage["stop_restart"]["post_restart_first_anchor"])
        for field in ("mainhand_swing_remaining_ms", "offhand_swing_remaining_ms"):
            timer = self.report["timers"][field]
            self.assertEqual(
                timer["components"]["duration"]["status"],
                "LIVE_CALIBRATED_PARTIAL",
            )
            self.assertEqual(
                timer["components"]["timer_start"]["status"],
                "LIVE_CALIBRATED_PARTIAL",
            )

    def test_stage_c_preserves_pre_patch_contradiction_and_current_alignment(self) -> None:
        stage = self.report["live_source_recovery_contract"]["stages"][
            "C_flurry_haste_boundaries"
        ]
        self.assertEqual(stage["flurry_spell_id"], 12970)
        self.assertEqual(stage["flurry_speed_multiplier"], 1.3)
        self.assertFalse(stage["debug_projection_has_original_prediction_numeric_values"])
        comparisons = {
            item["boundary"]: item
            for item in stage["opposite_hand_boundary_comparisons"]
        }
        self.assertAlmostEqual(
            comparisons["added"]["proportional_prediction"][
                "error_seconds_observed_minus_predicted"
            ],
            0.1706153255,
            places=6,
        )
        self.assertAlmostEqual(
            comparisons["removed"]["proportional_prediction"][
                "error_seconds_observed_minus_predicted"
            ],
            -0.2828001130,
            places=6,
        )
        for comparison in comparisons.values():
            proportional = abs(
                comparison["proportional_prediction"][
                    "error_seconds_observed_minus_predicted"
                ]
            )
            unchanged = abs(
                comparison["unchanged_deadline_prediction"][
                    "error_seconds_observed_minus_predicted"
                ]
            )
            self.assertGreater(proportional, unchanged)
        source = self.report["source_model_inventory"][
            "haste_rescales_remaining_swing"
        ]
        self.assertEqual(
            source["live_evidence_disposition"],
            "CURRENT_GENERIC_NON_WHITE_PROPORTIONAL_RESCALE_SOURCE_PRESENT",
        )
        self.assertEqual(
            source["pre_patch_live_comparison"]["disposition"],
            "PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY",
        )
        inventory = self.report["source_model_inventory"]
        for key in (
            "white_swing_haste_preserves_opposite_deadline",
            "warrior_flurry_uses_white_swing_boundary",
        ):
            self.assertTrue(inventory[key]["present"])
            self.assertEqual(
                inventory[key]["live_evidence_disposition"],
                "CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE",
            )
        self.assertTrue(inventory["warrior_flurry_proc_aura_ids"]["present"])
        self.assertEqual(
            inventory["warrior_flurry_proc_aura_ids"][
                "live_evidence_disposition"
            ],
            "CURRENT_FLURRY_AURA_IDS_ALIGNED_WITH_LIVE",
        )
        self.assertEqual(
            self.report["summary"][
                "simulator_flurry_remaining_rescale_disposition"
            ],
            "CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE",
        )
        self.assertEqual(
            self.report["summary"]["pre_patch_simulator_flurry_disposition"],
            "PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY",
        )
        for field in ("mainhand_swing_remaining_ms", "offhand_swing_remaining_ms"):
            reset = self.report["timers"][field]["components"]["reset_cancel"]
            self.assertFalse(
                any(
                    "current wowsims proportional rescale" in blocker
                    for blocker in reset["blockers"]
                )
            )
            self.assertTrue(
                any("non-white-boundary" in blocker for blocker in reset["blockers"])
            )

    def test_stage_d_closes_same_guid_cancel_but_not_target_switch(self) -> None:
        stage = self.report["live_source_recovery_contract"]["stages"][
            "D_heroic_strike_cancel_and_loadout"
        ]
        cancel = stage["cancel"]
        self.assertEqual(
            cancel["request"]["cancelRequestPath"],
            "ClearTarget_TargetUnit_same_guid",
        )
        self.assertTrue(cancel["window_close"]["boundedNoGoResult"])
        self.assertTrue(cancel["completion"]["nextMainHandWasWhite"])
        self.assertTrue(cancel["completion"]["offHandContinued"])
        self.assertTrue(cancel["completion"]["serverFailureSupport"])
        target = stage["target_switch"]
        self.assertEqual(target["status"], "EXTERNAL_HOLD")
        self.assertEqual(
            target["hold_reason"],
            "requires_exactly_two_adjacent_attackable_targets",
        )
        self.assertFalse(target["mechanic_observed"])
        self.assertTrue(target["disposition_resolved"])
        reset = self.report["timers"]["mainhand_swing_remaining_ms"][
            "components"
        ]["reset_cancel"]
        self.assertEqual(
            reset["evidence"]["same_guid_cancel_contract"]["status"],
            "LIVE_VERIFIED",
        )
        self.assertEqual(
            reset["evidence"]["same_guid_cancel_contract"][
                "simulator_disposition"
            ],
            "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS",
        )
        inventory = self.report["source_model_inventory"]
        for key in (
            "warrior_explicit_queue_cancel",
            "o2o_explicit_queue_cancel",
            "bridge_explicit_queue_cancel",
        ):
            self.assertTrue(inventory[key]["present"])
            self.assertEqual(
                inventory[key]["live_evidence_disposition"],
                "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS",
            )
        self.assertEqual(
            self.report["summary"][
                "same_guid_heroic_strike_cancel_simulator_disposition"
            ],
            "CURRENT_EXPLICIT_QUEUE_CANCEL_ALIGNED_WITH_LIVE_SAME_GUID_SEMANTICS",
        )
        self.assertFalse(
            self.report["live_source_recovery_contract"]["evidence_gate"][
                "simulator_patch_allowed"
            ]
        )
        self.assertFalse(
            any(
                "Heroic Strike cancellation and target-switch paths remain unresolved"
                in blocker
                for blocker in reset["blockers"]
            )
        )
        self.assertTrue(
            any("EXTERNAL_HOLD" in blocker for blocker in reset["blockers"])
        )

    def test_target_switch_hold_is_a_required_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = copy.deepcopy(self.live_summary)
            target = bad["stages"]["D_heroic_strike_cancel_and_loadout"][
                "target_switch"
            ]
            target["status"] = "LIVE_VERIFIED"
            path = self._write_live_summary(directory, bad)
            with self.assertRaises(FuryTimerTransitionContractError):
                build_fury_timer_transition_contract(live_summary_path=path)

    def test_verified_gcd_durations_and_live_start_stay_partial(self) -> None:
        timer = self.report["timers"]["gcd_remaining_ms"]
        actions = timer["components"]["duration"]["evidence"]["actions"]
        by_key = {action["action_key"]: action for action in actions}
        self.assertTrue(by_key["warrior.bloodthirst"]["historical_duration_ready"])
        self.assertTrue(by_key["warrior.execute"]["historical_duration_ready"])
        self.assertFalse(by_key["warrior.slam"]["historical_duration_ready"])
        self.assertFalse(timer["components"]["duration"]["complete"])
        self.assertEqual(
            timer["components"]["timer_start"]["status"],
            "LIVE_CALIBRATED_PARTIAL",
        )
        self.assertFalse(timer["reconstruction_allowed"])

    def test_whirlwind_rank_two_cooldown_is_current_character_only(self) -> None:
        timer = self.report["timers"]["cooldown_remaining_ms"]
        actions = timer["components"]["duration"]["evidence"]["actions"]
        whirlwind = next(
            action for action in actions if action["action_key"] == "warrior.whirlwind"
        )
        current = next(
            parameter
            for parameter in whirlwind["duration_parameters"]
            if parameter["field"] == "current_character_cooldown_seconds"
        )
        self.assertEqual(current["value"], 8.5)
        self.assertTrue(current["verified_deterministic"])
        self.assertEqual(current["scope"], "current_character_only")
        self.assertFalse(current["transferable_to_historical_builds"])

    def test_block_commented_sod_reset_is_not_active_turtle_source(self) -> None:
        check = self.report["source_model_inventory"][
            "gear_dependent_cooldown_reset"
        ]
        self.assertFalse(check["present"])
        self.assertIsNone(check["line"])
        self.assertEqual(check["active_match_lines"], [])
        self.assertIn(505, check["commented_out_match_lines"])
        self.assertEqual(check["evidence_role"], "excluded_block_comment_only")

    def test_action_registry_inventory_uses_existing_lane_mapping(self) -> None:
        actions = self.report["action_registry"]["actions"]
        by_key = {action["action_key"]: action for action in actions}
        self.assertEqual(
            by_key["warrior.heroic_strike.queue"]["lane"],
            "on_swing_unknown_intent",
        )
        self.assertEqual(
            by_key["warrior.cleave.queue"]["lane"],
            "on_swing_unknown_intent",
        )
        self.assertEqual(by_key["warrior.bloodrage"]["lane"], "off_gcd")
        self.assertEqual(by_key["warrior.bloodthirst"]["lane"], "gcd")

    def test_writer_and_cli_emit_only_a_small_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "timer.json"
            written = write_fury_timer_transition_contract(self.report, output)
            value = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(value["schema"], "fury_timer_transition_contract/v1")
            self.assertFalse(value["summary"]["line_level_output_materialized"])
            cli_output = Path(directory) / "cli.json"
            self.assertEqual(
                main(
                    [
                        "--registry",
                        str(DEFAULT_REGISTRY),
                        "--review",
                        str(DEFAULT_REVIEW),
                        "--comparison",
                        str(DEFAULT_COMPARISON),
                        "--live-summary",
                        str(DEFAULT_LIVE_SUMMARY),
                        "--output",
                        str(cli_output),
                    ]
                ),
                0,
            )
            self.assertTrue(cli_output.is_file())

    def test_invalid_review_kind_is_rejected_before_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad_review = Path(directory) / "review.json"
            bad_review.write_text(
                json.dumps({"kind": "wrong", "schema_version": 1}),
                encoding="utf-8",
            )
            with self.assertRaises(FuryTimerTransitionContractError):
                build_fury_timer_transition_contract(review_path=bad_review)


if __name__ == "__main__":
    unittest.main()
