from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_timer_source_recovery_summary import (
    DEFAULT_COMPOSITE,
    FuryTimerSourceRecoverySummaryError,
    KIND,
    SCHEMA,
    build_fury_timer_source_recovery_summary,
    main,
    summarize_fury_timer_source_recovery,
)


class FuryTimerSourceRecoverySummaryTests(unittest.TestCase):
    def test_frozen_composite_builds_complete_four_stage_contract(self) -> None:
        report = build_fury_timer_source_recovery_summary(
            DEFAULT_COMPOSITE, combat_log_path=None
        )

        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["kind"], KIND)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(
            report["composite_validation"]["required_check_count"], 14
        )
        self.assertTrue(report["composite_validation"]["all_14_checks_true"])
        self.assertTrue(report["composite_validation"]["trace_links_exact"])

        stages = report["stages"]
        stage_a = stages["A_timer_chains"]
        self.assertEqual(
            stage_a["spells"]["sunder"]["completed_attempts"], [1, 2, 3]
        )
        self.assertEqual(
            stage_a["spells"]["bloodthirst"]["completed_attempts"], [1, 4, 8]
        )
        self.assertEqual(
            len(stage_a["spells"]["bloodthirst"]["active_retry_confirmations"]),
            3,
        )
        self.assertTrue(
            all(
                item["evidence_kind"] == "explicit_failure_event"
                for item in stage_a["spells"]["bloodthirst"][
                    "active_retry_confirmations"
                ]
            )
        )

        stage_b = stages["B_dual_wield_intervals"]
        loadout = stage_b["locked_loadout"]
        self.assertEqual(loadout["main_hand_item_id"], 18832)
        self.assertEqual(loadout["off_hand_item_id"], 19866)
        self.assertAlmostEqual(loadout["main_hand_speed_seconds"], 2.3790001129964)
        self.assertAlmostEqual(loadout["off_hand_speed_seconds"], 1.6170000768034)
        self.assertGreaterEqual(
            stage_b["baseline"]["main_hand"]["statistics_seconds"]["count"], 3
        )
        self.assertGreaterEqual(
            stage_b["baseline"]["off_hand"]["statistics_seconds"]["count"], 3
        )

        stage_c = stages["C_flurry_haste_boundaries"]
        self.assertFalse(
            stage_c["debug_projection_has_original_prediction_numeric_values"]
        )
        add, remove = stage_c["opposite_hand_boundary_comparisons"]
        self.assertEqual((add["boundary"], add["opposite_hand"]), ("added", "off_hand"))
        self.assertAlmostEqual(
            add["observed_cross_boundary_interval_seconds"], 1.651, places=3
        )
        self.assertAlmostEqual(
            add["proportional_prediction"]["deadline"], 249442.1913846745
        )
        self.assertAlmostEqual(
            add["proportional_prediction"][
                "error_seconds_observed_minus_predicted"
            ],
            0.1706153255,
        )
        self.assertAlmostEqual(
            add["unchanged_deadline_prediction"][
                "error_seconds_observed_minus_predicted"
            ],
            0.0339999232,
        )
        self.assertEqual(
            (remove["boundary"], remove["opposite_hand"]),
            ("removed", "main_hand"),
        )
        self.assertAlmostEqual(
            remove["observed_cross_boundary_interval_seconds"], 1.858, places=3
        )
        self.assertAlmostEqual(
            remove["proportional_prediction"]["deadline"], 249447.522800113
        )
        self.assertAlmostEqual(
            remove["proportional_prediction"][
                "error_seconds_observed_minus_predicted"
            ],
            -0.282800113,
        )
        self.assertAlmostEqual(
            remove["unchanged_deadline_prediction"][
                "error_seconds_observed_minus_predicted"
            ],
            0.0279999131,
        )

        stage_d = stages["D_heroic_strike_cancel_and_loadout"]
        self.assertTrue(stage_d["cancel"]["completion"]["boundedNoGoResult"])
        self.assertEqual(stage_d["cancel"]["correlated_events"]["34"]["event"], "SPELL_FAILED_SELF")
        self.assertEqual(stage_d["cancel"]["correlated_events"]["40"]["castKind"], "MAINHAND")
        self.assertEqual(stage_d["cancel"]["correlated_events"]["46"]["event"], "AUTO_ATTACK_SELF")
        self.assertEqual(stage_d["target_switch"]["status"], "EXTERNAL_HOLD")
        self.assertFalse(stage_d["target_switch"]["mechanic_observed"])
        self.assertEqual(
            stage_d["loadout_restore"]["original_main_hand_item_id"], 21679
        )

        gate = report["evidence_gate"]
        self.assertTrue(gate["live_contract_complete"])
        self.assertFalse(gate["simulator_patch_allowed"])
        self.assertFalse(gate["historical_reconstruction_allowed"])
        self.assertEqual(report["scope"]["chronicle_rows_read"], 0)

    def test_combat_log_keeps_only_recovery_token_and_narrow_sequence_windows(self) -> None:
        base = build_fury_timer_source_recovery_summary(
            DEFAULT_COMPOSITE, combat_log_path=None
        )
        token = base["combat_log_corroboration"]["run_token"]
        with tempfile.TemporaryDirectory() as directory:
            combat_log = Path(directory) / "WoWCombatLog.txt"
            lines = [
                "9/1 old BOC_TIMER|run=timer-campaign-decoy-1|seq=34|event=OLD",
            ]
            for sequence in (32, 33, 34, 35, 36, 39, 40, 41, 45, 46, 47, 48, 100):
                lines.append(
                    f"9/1 live BOC_TIMER_EVENT|run={token}|rc=1|seq={sequence}|event=E{sequence}"
                )
            combat_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

            report = build_fury_timer_source_recovery_summary(
                DEFAULT_COMPOSITE, combat_log_path=combat_log
            )

        combat = report["combat_log_corroboration"]
        self.assertEqual(combat["status"], "complete")
        self.assertEqual(combat["missing_target_sequences"], [])
        retained_sequences = [item["calibration_sequence"] for item in combat["lines"]]
        self.assertEqual(retained_sequences, [33, 34, 35, 39, 40, 41, 45, 46, 47])
        self.assertTrue(all(f"|run={token}|" in item["text"] for item in combat["lines"]))
        self.assertNotIn(32, retained_sequences)
        self.assertNotIn(48, retained_sequences)
        self.assertNotIn(100, retained_sequences)

    def test_composite_requires_exactly_fourteen_true_checks(self) -> None:
        original = json.loads(DEFAULT_COMPOSITE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "composite.json"
            for mutation in ("false_check", "extra_check"):
                composite = json.loads(json.dumps(original))
                if mutation == "false_check":
                    composite["checks"]["recovery_loadout_restored"] = False
                else:
                    composite["checks"]["invented_check"] = True
                path.write_text(
                    json.dumps(composite, ensure_ascii=False), encoding="utf-8"
                )
                with self.subTest(mutation=mutation):
                    with self.assertRaises(FuryTimerSourceRecoverySummaryError):
                        build_fury_timer_source_recovery_summary(
                            path, combat_log_path=None
                        )

    def test_trace_link_and_key_marker_are_strict(self) -> None:
        original = json.loads(DEFAULT_COMPOSITE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_link = json.loads(json.dumps(original))
            bad_link["source_bundle"] = str(root / "wrong_bundle")
            bad_link_path = root / "bad_link.json"
            bad_link_path.write_text(
                json.dumps(bad_link, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(FuryTimerSourceRecoverySummaryError):
                build_fury_timer_source_recovery_summary(
                    bad_link_path, combat_log_path=None
                )

            source_bundle = root / "source_bundle"
            source_bundle.mkdir()
            source_trace = source_bundle / "trace.jsonl"
            linked_source = Path(original["files"]["source_trace"])
            source_trace.write_text(
                "".join(
                    line
                    for line in linked_source.read_text(encoding="utf-8").splitlines(
                        keepends=True
                    )
                    if '"name": "CALIBRATION_TIMER_STAGE_COMPLETED"' not in line
                ),
                encoding="utf-8",
            )
            missing_marker = json.loads(json.dumps(original))
            missing_marker["source_bundle"] = str(source_bundle)
            missing_marker["files"]["source_trace"] = str(source_trace)
            missing_marker_path = root / "missing_marker.json"
            missing_marker_path.write_text(
                json.dumps(missing_marker, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(FuryTimerSourceRecoverySummaryError):
                build_fury_timer_source_recovery_summary(
                    missing_marker_path, combat_log_path=None
                )

    def test_writer_and_cli_publish_the_fixed_v1_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            combat_log = root / "empty_combat_log.txt"
            combat_log.write_text("", encoding="utf-8")
            output = root / "summary.json"
            result = summarize_fury_timer_source_recovery(
                DEFAULT_COMPOSITE,
                combat_log_path=None,
                output_path=output,
            )
            self.assertEqual(result.status, "complete")
            self.assertTrue(result.live_contract_complete)
            self.assertTrue(output.is_file())
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["schema"], SCHEMA)
            self.assertLess(output.stat().st_size, 32 * 1024)

            cli_output = root / "cli.json"
            self.assertEqual(
                main(
                    [
                        str(DEFAULT_COMPOSITE),
                        "--combat-log",
                        str(combat_log),
                        "--output",
                        str(cli_output),
                    ]
                ),
                0,
            )
            self.assertTrue(cli_output.is_file())


if __name__ == "__main__":
    unittest.main()
