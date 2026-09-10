from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from o2o_dps.calibration_watch import CalibrationWatcher
from o2o_dps.white_rage_formula_audit import wowsims_rage_conversion
from o2o_dps.white_rage_phase10_identification_audit import audit_phase10


_SAMPLES = (
    ("sunder_0", "critical", 360),
    ("sunder_0", "critical", 420),
    ("sunder_0", "critical", 480),
    ("sunder_0", "ordinary", 180),
    ("sunder_0", "ordinary", 210),
    ("sunder_0", "ordinary", 240),
    ("sunder_5", "critical", 500),
    ("sunder_5", "critical", 540),
    ("sunder_5", "critical", 580),
    ("sunder_5", "ordinary", 260),
    ("sunder_5", "ordinary", 280),
    ("sunder_5", "ordinary", 300),
)


def _observed(outcome: str, damage: int) -> float:
    damage_term = damage * 7.5 / wowsims_rage_conversion(60)
    if outcome == "critical":
        prediction = 1.08 * damage_term + 10.5
    else:
        prediction = 1.12 * damage_term + 5.0
    return math.floor(prediction * 10) / 10


def _summary() -> dict:
    trials = []
    counts = {
        "sunder_0_critical": 0,
        "sunder_0_ordinary": 0,
        "sunder_5_critical": 0,
        "sunder_5_ordinary": 0,
    }
    for trial_number, (stratum, outcome, damage) in enumerate(_SAMPLES, start=1):
        stacks = 0 if stratum == "sunder_0" else 5
        armor = 4211 if stacks == 0 else 1961
        cell = f"{stratum}_{outcome}"
        counts[cell] += 1
        observed = _observed(outcome, damage)
        raw = observed * 10
        warmup_sequence = 100 + trial_number * 10
        swing_sequence = warmup_sequence + 2
        trials.append(
            {
                "trial": trial_number,
                "stratum": stratum,
                "sample_quota": outcome,
                "planned_sunder_stacks": stacks,
                "observed_sunder_stacks": stacks,
                "attack_power": 1180,
                "target_armor": armor,
                "baseline_target_armor": 4211,
                "armor_reduction_from_baseline": 4211 - armor,
                "swing": {
                    "damage": damage,
                    "outcome": outcome,
                    "critical": outcome == "critical",
                    "glancing": False,
                    "sub_damage_count": 1,
                },
                "rage": {
                    "raw_gain": raw,
                    "raw_scale": 10,
                    "capped": False,
                    "identifiable": True,
                    "unbridled_wrath_proc": {
                        "observed": False,
                        "raw_rage_gain": 0,
                    },
                    "base_gain_after_known_proc": observed,
                    "base_gain_raw_after_known_proc": raw,
                },
                "combat_context": {
                    "player_level": 60,
                    "target_guid": "0xF13000C55226FDD2",
                    "main_hand_base_speed": 3.2,
                    "main_hand_item_id": 21679,
                    "flurry_active": False,
                    "flurry_stacks": 0,
                    "attack_power": 1180,
                    "target_armor": armor,
                    "weapon_skill_name": "Two-Handed Swords",
                    "weapon_skill_rank": 300,
                    "weapon_skill_maximum": 300,
                    "in_combat": True,
                    "combat_warmup_satisfied": True,
                    "combat_warmup_sequence": warmup_sequence,
                },
                "combat_gate": {
                    "combat_warmup_satisfied": True,
                    "combat_warmup_sequence": warmup_sequence,
                    "swing_sequence": swing_sequence,
                    "swing_in_combat": True,
                },
                "quality_flags": [],
            }
        )
    return {
        "kind": "brainofcat_calibration_summary",
        "specialized_runs": [
            {
                "task_id": "warrior_white_swing_rage_two_hand_identification",
                "task_run_id": "phase10-run-1",
                "analyzer": "white_swing_rage_two_hand_identification_v1",
                "campaign": {
                    "campaign_id": (
                        "warrior_white_swing_rage_two_hand_identification_phase10"
                    ),
                    "campaign_run_id": "phase10-campaign-1",
                },
                "completion_confirmed": True,
                "requested_trials": 12,
                "completed_trials": 12,
                "fixed_control": {
                    "target_guid": "0xF13000C55226FDD2",
                    "player_level": 60,
                    "attack_power": 1180,
                    "baseline_target_armor": 4211,
                    "main_hand_item_id": 21679,
                    "main_hand_item_name": "Kalimdor's Revenge",
                    "main_hand_base_speed": 3.2,
                    "weapon_skill_name": "Two-Handed Swords",
                    "weapon_skill_rank": 300,
                    "weapon_skill_maximum": 300,
                    "same_target_attack_power_weapon_skill_and_level_all_valid_samples": True,
                    "raw_rage_scale": 10,
                    "combat_warmup_required": True,
                    "all_valid_swings_in_combat_after_warmup": True,
                },
                "sample_quota": {
                    "required_per_armor_outcome": 3,
                    "counts": counts,
                    "complete": True,
                },
                "armor_strata": {
                    "sunder_0": {
                        "planned_sunder_stacks": 0,
                        "observed_sunder_stacks": 0,
                        "observed_target_armor": 4211,
                        "armor_reduction_from_baseline": 0,
                        "valid_clean_sample_count": 6,
                        "outcome_counts": {"critical": 3, "ordinary": 3},
                    },
                    "sunder_5": {
                        "planned_sunder_stacks": 5,
                        "observed_sunder_stacks": 5,
                        "observed_target_armor": 1961,
                        "armor_reduction_from_baseline": 2250,
                        "valid_clean_sample_count": 6,
                        "outcome_counts": {"critical": 3, "ordinary": 3},
                    },
                },
                "trials": trials,
            }
        ],
    }


def _savedvariables(campaign_run_id: str) -> str:
    return f'''BrainOfCatCharacterDB = {{
    ["schemaVersion"] = 1,
    ["entries"] = {{}},
    ["calibration"] = {{
        ["schemaVersion"] = 1,
        ["maxEntries"] = 15000,
        ["count"] = 1,
        ["nextIndex"] = 2,
        ["nextSequence"] = 2,
        ["entries"] = {{
            [1] = {{
                ["sequence"] = 1,
                ["time"] = 100.5,
                ["event"] = "CALIBRATION_CAMPAIGN_COMPLETED",
                ["state"] = {{}},
                ["marker"] = {{ ["campaignRunId"] = "{campaign_run_id}" }},
            }},
        }},
    }},
}}
'''


class WhiteRagePhase10IdentificationAuditTests(unittest.TestCase):
    def test_fit_and_internal_holdout_publish_phase11_candidate_only(self) -> None:
        document = audit_phase10(_summary())

        self.assertEqual(document["status"], "phase11_candidate_ready")
        self.assertTrue(document["evidence_gate"]["sufficient"])
        self.assertEqual(document["evidence_gate"]["identification_sample_count"], 8)
        self.assertEqual(document["evidence_gate"]["internal_holdout_sample_count"], 4)
        self.assertTrue(document["conclusion_gate"]["identification_fit_passed"])
        self.assertTrue(document["conclusion_gate"]["internal_holdout_passed"])
        self.assertIsNotNone(document["conclusion_gate"]["phase11_candidate"])
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])
        self.assertIsNone(document["conclusion_gate"]["simulator_patch"])
        self.assertEqual(len(document["phase9_candidate_diagnostics"]), 4)

    def test_combat_gate_failure_is_insufficient_evidence(self) -> None:
        summary = _summary()
        summary["specialized_runs"][0]["trials"][5]["combat_gate"][
            "swing_in_combat"
        ] = False

        document = audit_phase10(summary)

        self.assertEqual(document["status"], "insufficient_evidence")
        self.assertIn(
            "phase10_trial_combat_gate_failed", document["evidence_gate"]["reasons"]
        )
        self.assertFalse(document["conclusion_gate"]["phase11_candidate_ready"])
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])

    def test_internal_holdout_failure_does_not_publish_candidate(self) -> None:
        summary = _summary()
        held_out = summary["specialized_runs"][0]["trials"][2]
        held_out["rage"]["base_gain_after_known_proc"] += 1.0
        held_out["rage"]["base_gain_raw_after_known_proc"] += 10.0
        held_out["rage"]["raw_gain"] += 10.0

        document = audit_phase10(summary)

        self.assertEqual(document["status"], "holdout_failed")
        self.assertTrue(document["conclusion_gate"]["identification_fit_passed"])
        self.assertFalse(document["conclusion_gate"]["internal_holdout_passed"])
        self.assertIsNone(document["conclusion_gate"]["phase11_candidate"])
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])

    def test_watcher_auto_runs_phase10_audit_and_retains_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase10"), encoding="utf-8")
            data_root = root / "offline_data"
            summary_path = root / "phase10-summary.json"
            summary_path.write_text(json.dumps(_summary()), encoding="utf-8")
            calls: list[Path] = []

            def fake_auditor(path: Path) -> dict[str, object]:
                calls.append(path)
                return {
                    "schema_version": 1,
                    "kind": "white_rage_phase10_identification_audit",
                    "status": "phase11_candidate_ready",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "phase11_candidate_ready": True,
                        "phase11_candidate": {"candidate_id": "phase10-fit"},
                        "simulator_patch_allowed": False,
                    },
                }

            audit_output = root / "phase10-audit.json"
            watcher = CalibrationWatcher(
                source,
                data_root=data_root,
                summarizer=lambda _calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(summary_path)}
                ),
                phase10_identification_auditor=fake_auditor,
                phase10_identification_audit_output=audit_output,
            )

            first = watcher.process_once()
            second = watcher.process_once()

            self.assertEqual(
                first["status"], "campaign_imported_phase10_phase11_candidate_ready"
            )
            self.assertEqual(calls, [summary_path])
            self.assertTrue(
                first["phase10_identification_audit"]["phase11_candidate_ready"]
            )
            self.assertFalse(
                first["phase10_identification_audit"]["simulator_patch_allowed"]
            )
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertTrue(
                second["phase10_identification_audit"]["phase11_candidate_ready"]
            )
            self.assertEqual(
                json.loads(audit_output.read_text(encoding="utf-8"))["status"],
                "phase11_candidate_ready",
            )


if __name__ == "__main__":
    unittest.main()
