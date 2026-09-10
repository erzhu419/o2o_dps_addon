from __future__ import annotations

import math
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from o2o_dps.calibration_watch import CalibrationWatcher
from o2o_dps.white_rage_formula_audit import wowsims_rage_conversion
from o2o_dps.white_rage_phase11_external_holdout_audit import (
    DEFAULT_PREREGISTRATION,
    WhiteRagePhase11AuditError,
    audit_phase11,
    run_from_path,
)


_SAMPLES = (
    ("sunder_0", "critical", 480),
    ("sunder_0", "critical", 520),
    ("sunder_0", "ordinary", 220),
    ("sunder_0", "ordinary", 250),
    ("sunder_5", "critical", 600),
    ("sunder_5", "critical", 650),
    ("sunder_5", "ordinary", 300),
    ("sunder_5", "ordinary", 340),
)


def _phase10_audit() -> dict:
    def comparison(trial: int, outcome: str, damage: int, role: str) -> dict:
        damage_term = damage * 7.5 / wowsims_rage_conversion(60)
        speed_coefficient = 3.5 if outcome == "critical" else 1.75
        observed_raw = math.floor(
            (1.09 * damage_term + speed_coefficient * 3.2) * 10
        )
        return {
            "trial": trial,
            "sample_quota": outcome,
            "analysis_role": role,
            "damage_term": damage_term,
            "observed_base_rage": observed_raw / 10,
        }

    ordinary_rows = [
        comparison(1, "ordinary", 289, "identification"),
        comparison(3, "ordinary", 309, "identification"),
        comparison(9, "ordinary", 362, "identification"),
        comparison(11, "ordinary", 423, "identification"),
    ]
    critical_rows = [
        comparison(2, "critical", 640, "identification"),
        comparison(5, "critical", 582, "identification"),
        comparison(7, "critical", 740, "identification"),
        comparison(8, "critical", 846, "identification"),
    ]
    holdout_rows = [
        comparison(4, "ordinary", 298, "internal_holdout"),
        comparison(6, "critical", 648, "internal_holdout"),
        comparison(10, "critical", 864, "internal_holdout"),
        comparison(12, "ordinary", 355, "internal_holdout"),
    ]
    return {
        "kind": "white_rage_phase10_identification_audit",
        "status": "phase11_candidate_ready",
        "evidence_gate": {
            "sufficient": True,
            "clean_sample_count": 12,
            "identification_sample_count": 8,
            "internal_holdout_sample_count": 4,
        },
        "conclusion_gate": {
            "identification_fit_passed": True,
            "internal_holdout_passed": True,
            "phase11_candidate_ready": True,
            "phase11_candidate": {
                "candidate_id": "phase10_two_hand_outcome_linear_fit_v1",
                "ordinary": {
                    "damage_coefficient_a": 1.0862479771695912,
                    "speed_coefficient_b": 1.7609405012612533,
                },
                "critical": {
                    "damage_coefficient_a": 1.0923798405483482,
                    "speed_coefficient_b": 3.479394975756973,
                },
                "source_identification_sample_count": 8,
                "internal_holdout_sample_count": 4,
            },
        },
        "identification_models": {
            "ordinary": {"identification_comparisons": ordinary_rows},
            "critical": {"identification_comparisons": critical_rows},
        },
        "internal_holdout": {"comparisons": holdout_rows},
    }


def _preregistration() -> dict:
    return json.loads(DEFAULT_PREREGISTRATION.read_text(encoding="utf-8"))


def _observed_raw(outcome: str, damage: int) -> int:
    damage_term = damage * 7.5 / wowsims_rage_conversion(60)
    speed_coefficient = 3.5 if outcome == "critical" else 1.75
    prediction = 1.09 * damage_term + speed_coefficient * 3.6
    return math.floor(prediction * 10)


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
        observed_raw = _observed_raw(outcome, damage)
        warmup_sequence = 100 + trial_number * 10
        swing_sequence = warmup_sequence + 2
        trials.append(
            {
                "trial": trial_number,
                "stratum": stratum,
                "sample_quota": outcome,
                "planned_sunder_stacks": stacks,
                "observed_sunder_stacks": stacks,
                "attack_power": 1018,
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
                    "raw_gain": observed_raw,
                    "raw_scale": 10,
                    "capped": False,
                    "identifiable": True,
                    "unbridled_wrath_proc": {
                        "observed": False,
                        "spell_id": None,
                        "raw_rage_gain": 0,
                    },
                    "base_gain_after_known_proc": observed_raw / 10,
                    "base_gain_raw_after_known_proc": observed_raw,
                },
                "combat_context": {
                    "player_level": 60,
                    "target_guid": "0xF13000C55226FDD2",
                    "main_hand_base_speed": 3.6,
                    "main_hand_item_id": 55504,
                    "flurry_active": False,
                    "flurry_stacks": 0,
                    "attack_power": 1018,
                    "target_armor": armor,
                    "weapon_skill_name": "Two-Handed Maces",
                    "weapon_skill_rank": 303,
                    "weapon_skill_maximum": 303,
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
                "same_batch_spell_51277_sequences": [],
                "same_batch_spell_damage_sequences": [],
                "quality_flags": [],
            }
        )
    return {
        "kind": "brainofcat_calibration_summary",
        "specialized_runs": [
            {
                "task_id": "warrior_white_swing_rage_two_hand_external_holdout",
                "task_run_id": "phase11-run-1",
                "analyzer": "white_swing_rage_two_hand_external_holdout_v1",
                "campaign": {
                    "campaign_id": (
                        "warrior_white_swing_rage_two_hand_external_holdout_phase11"
                    ),
                    "campaign_run_id": "phase11-campaign-1",
                },
                "completion_confirmed": True,
                "requested_trials": 8,
                "completed_trials": 8,
                "fixed_control": {
                    "target_guid": "0xF13000C55226FDD2",
                    "player_level": 60,
                    "attack_power": 1018,
                    "baseline_target_armor": 4211,
                    "main_hand_item_id": 55504,
                    "main_hand_item_name": "Anchor of the Wavecutter",
                    "main_hand_base_speed": 3.6,
                    "weapon_skill_name": "Two-Handed Maces",
                    "weapon_skill_rank": 303,
                    "weapon_skill_maximum": 303,
                    "same_target_attack_power_weapon_skill_and_level_all_valid_samples": True,
                    "raw_rage_scale": 10,
                    "known_damage_proc_spell_id": 51277,
                    "all_valid_samples_exclude_same_batch_known_damage_proc": True,
                    "known_rage_proc_spell_id": 12964,
                    "known_rage_proc_raw_gain": 20,
                    "all_valid_samples_exclude_other_energize": True,
                    "candidate_id": "phase11_simple_common_damage_base_speed_v1",
                    "fit_permitted": False,
                    "holdout_use": "external_validation_only_no_refit",
                    "combat_warmup_required": True,
                    "all_valid_swings_in_combat_after_warmup": True,
                },
                "sample_quota": {
                    "required_per_armor_outcome": 2,
                    "counts": counts,
                    "complete": True,
                },
                "armor_strata": {
                    "sunder_0": {
                        "planned_sunder_stacks": 0,
                        "observed_sunder_stacks": 0,
                        "observed_target_armor": 4211,
                        "armor_reduction_from_baseline": 0,
                        "valid_clean_sample_count": 4,
                        "outcome_counts": {"critical": 2, "ordinary": 2},
                    },
                    "sunder_5": {
                        "planned_sunder_stacks": 5,
                        "observed_sunder_stacks": 5,
                        "observed_target_armor": 1961,
                        "armor_reduction_from_baseline": 2250,
                        "valid_clean_sample_count": 4,
                        "outcome_counts": {"critical": 2, "ordinary": 2},
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


class WhiteRagePhase11ExternalHoldoutAuditTests(unittest.TestCase):
    def test_all_eight_external_samples_authorize_only_scoped_registry_patch(self) -> None:
        document = audit_phase11(_summary(), _phase10_audit())

        self.assertEqual(document["status"], "external_holdout_validated")
        self.assertTrue(document["phase10_source_gate"]["validated"])
        self.assertEqual(document["phase10_source_gate"]["clean_sample_count"], 12)
        self.assertEqual(document["external_holdout"]["observed_sample_count"], 8)
        self.assertEqual(document["external_holdout"]["mismatch_count"], 0)
        self.assertTrue(document["conclusion_gate"]["external_holdout_passed"])
        self.assertTrue(document["conclusion_gate"]["simulator_patch_allowed"])
        self.assertFalse(
            document["conclusion_gate"][
                "unconditional_global_rage_go_patch_allowed"
            ]
        )
        patch = document["conclusion_gate"]["simulator_patch"]
        self.assertEqual(
            patch["validated_scope"]["landed_outcomes"], ["ordinary", "critical"]
        )
        self.assertEqual(patch["validated_scope"]["player_level"], 60)
        self.assertEqual(
            patch["validated_scope"]["weapon_hand_type"], "two_hand"
        )
        self.assertTrue(
            patch["validated_scope"]["weapon_skill_at_maximum"]
        )
        self.assertEqual(
            patch["validated_scope"]["validated_base_speeds"], [3.2, 3.6]
        )
        self.assertFalse(
            patch["validated_scope"]["base_speed_interpolation_validated"]
        )
        self.assertIn("off_hand", patch["excluded_outcomes"])
        self.assertIn("glancing", patch["excluded_outcomes"])
        self.assertIn("one_hand_weapons", patch["unvalidated_dimensions"])
        self.assertFalse(
            document["preregistered_candidate"]["phase11_refitting_allowed"]
        )
        self.assertEqual(
            document["phase10_source_gate"][
                "frozen_candidate_phase10_compatible_count"
            ],
            12,
        )
        preregistration_gate = document["preregistration_gate"]
        self.assertEqual(
            preregistration_gate["patch_destination"],
            "mechanics/turtle_1_18_1 calibration registry",
        )
        self.assertEqual(
            preregistration_gate["validated_scope"], patch["validated_scope"]
        )
        self.assertEqual(
            preregistration_gate["excluded_outcomes"], patch["excluded_outcomes"]
        )
        self.assertEqual(
            preregistration_gate["unvalidated_dimensions"],
            patch["unvalidated_dimensions"],
        )
        self.assertFalse(
            preregistration_gate["unconditional_global_rage_go_patch_allowed"]
        )

    def test_any_single_mismatch_fails_closed_without_refitting(self) -> None:
        summary = _summary()
        trial = summary["specialized_runs"][0]["trials"][7]
        trial["rage"]["raw_gain"] += 10
        trial["rage"]["base_gain_raw_after_known_proc"] += 10
        trial["rage"]["base_gain_after_known_proc"] += 1

        document = audit_phase11(summary, _phase10_audit())

        self.assertEqual(document["status"], "holdout_failed")
        self.assertEqual(document["external_holdout"]["mismatch_count"], 1)
        self.assertFalse(document["conclusion_gate"]["external_holdout_passed"])
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])
        self.assertIsNone(document["conclusion_gate"]["simulator_patch"])

    def test_combat_gate_failure_is_insufficient_evidence(self) -> None:
        summary = _summary()
        summary["specialized_runs"][0]["trials"][3]["combat_gate"][
            "swing_in_combat"
        ] = False

        document = audit_phase11(summary, _phase10_audit())

        self.assertEqual(document["status"], "insufficient_evidence")
        self.assertIn(
            "phase11_trial_combat_gate_failed", document["evidence_gate"]["reasons"]
        )
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])

    def test_retained_anchor_proc_is_a_contract_error(self) -> None:
        summary = _summary()
        summary["specialized_runs"][0]["trials"][0][
            "same_batch_spell_51277_sequences"
        ] = [123]

        with self.assertRaisesRegex(
            WhiteRagePhase11AuditError, "spell-51277 evidence"
        ):
            audit_phase11(summary, _phase10_audit())

    def test_generic_same_batch_spell_damage_is_a_contract_error(self) -> None:
        summary = _summary()
        summary["specialized_runs"][0]["trials"][0][
            "same_batch_spell_damage_sequences"
        ] = [124]

        with self.assertRaisesRegex(
            WhiteRagePhase11AuditError, "no-spell-damage evidence"
        ):
            audit_phase11(summary, _phase10_audit())

    def test_unbridled_wrath_is_subtracted_before_candidate_comparison(self) -> None:
        summary = _summary()
        rage = summary["specialized_runs"][0]["trials"][0]["rage"]
        rage["unbridled_wrath_proc"] = {
            "observed": True,
            "spell_id": 12964,
            "raw_rage_gain": 20,
        }
        rage["raw_gain"] += 20

        document = audit_phase11(summary, _phase10_audit())

        self.assertEqual(document["status"], "external_holdout_validated")
        self.assertEqual(document["external_holdout"]["mismatch_count"], 0)

    def test_phase10_source_must_have_passed_8_plus_4_gate(self) -> None:
        source = _phase10_audit()
        source["evidence_gate"]["internal_holdout_sample_count"] = 3

        with self.assertRaisesRegex(
            WhiteRagePhase11AuditError, r"8\+4 identification/holdout gate"
        ):
            audit_phase11(_summary(), source)

    def test_phase10_source_must_reproduce_all_twelve_frozen_bins(self) -> None:
        source = _phase10_audit()
        source["internal_holdout"]["comparisons"][0]["observed_base_rage"] += 1

        with self.assertRaisesRegex(
            WhiteRagePhase11AuditError, "all 12 Phase-10 raw-tenths bins"
        ):
            audit_phase11(_summary(), source)

    def test_preregistration_drift_is_rejected(self) -> None:
        preregistration = _preregistration()
        preregistration["candidate"]["damage_coefficient"] = 1.10

        with self.assertRaisesRegex(
            WhiteRagePhase11AuditError, "preregistration drifted"
        ):
            audit_phase11(_summary(), _phase10_audit(), preregistration)

    def test_preregistration_rejects_any_patch_scope_drift(self) -> None:
        mutations = {
            "destination": lambda decision: decision.__setitem__(
                "patch_destination", "global rage implementation"
            ),
            "one_hand": lambda decision: decision["validated_scope"].__setitem__(
                "weapon_hand_type", "one_hand"
            ),
            "skill": lambda decision: decision["validated_scope"].__setitem__(
                "weapon_skill_at_maximum", False
            ),
            "speed": lambda decision: decision["validated_scope"].__setitem__(
                "validated_base_speeds", [3.2, 3.4, 3.6]
            ),
            "excluded": lambda decision: decision.__setitem__(
                "excluded_outcomes", ["off_hand"]
            ),
            "unvalidated": lambda decision: decision.__setitem__(
                "unvalidated_dimensions", []
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                preregistration = _preregistration()
                mutate(preregistration["decision_rule"])
                with self.assertRaisesRegex(
                    WhiteRagePhase11AuditError, "preregistration drifted"
                ):
                    audit_phase11(_summary(), _phase10_audit(), preregistration)

    def test_run_from_path_requires_preregistration_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary_path = root / "phase11-summary.json"
            phase10_path = root / "phase10-audit.json"
            summary_path.write_text(json.dumps(_summary()), encoding="utf-8")
            phase10_path.write_text(json.dumps(_phase10_audit()), encoding="utf-8")

            with self.assertRaisesRegex(
                WhiteRagePhase11AuditError, "cannot read Phase-11 preregistration"
            ):
                run_from_path(
                    summary_path,
                    phase10_path,
                    root / "missing-preregistration.json",
                )

    def test_watcher_runs_phase11_audit_once_and_retains_scoped_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "BrainOfCat.lua"
            source.write_text(_savedvariables("campaign-phase11"), encoding="utf-8")
            summary_path = root / "phase11-summary.json"
            summary_path.write_text(json.dumps(_summary()), encoding="utf-8")
            audit_output = root / "phase11-audit.json"
            calls: list[Path] = []

            def fake_auditor(path: Path) -> dict[str, object]:
                calls.append(path)
                return {
                    "schema_version": 1,
                    "kind": "white_rage_phase11_external_holdout_audit",
                    "status": "external_holdout_validated",
                    "evidence_gate": {"sufficient": True, "reasons": []},
                    "conclusion_gate": {
                        "external_holdout_passed": True,
                        "candidate_external_validated": True,
                        "simulator_patch_allowed": True,
                        "simulator_patch": {
                            "destination": "mechanics/turtle_1_18_1 calibration registry",
                            "validated_scope": {
                                "player_level": 60,
                                "hand": "main_hand",
                                "weapon_hand_type": "two_hand",
                                "weapon_skill_at_maximum": True,
                                "validated_base_speeds": [3.2, 3.6],
                                "landed_outcomes": ["ordinary", "critical"],
                            },
                        },
                        "unconditional_global_rage_go_patch_allowed": False,
                    },
                }

            watcher = CalibrationWatcher(
                source,
                data_root=root / "offline_data",
                summarizer=lambda _calibration: SimpleNamespace(
                    as_dict=lambda: {"status": "ok", "output": str(summary_path)}
                ),
                phase11_external_holdout_auditor=fake_auditor,
                phase11_external_holdout_audit_output=audit_output,
            )

            first = watcher.process_once()
            second = watcher.process_once()

            self.assertEqual(
                first["status"],
                "campaign_imported_phase11_external_holdout_validated",
            )
            self.assertEqual(calls, [summary_path])
            phase11 = first["phase11_external_holdout_audit"]
            self.assertTrue(phase11["external_holdout_passed"])
            self.assertTrue(phase11["simulator_patch_allowed"])
            self.assertFalse(
                phase11["unconditional_global_rage_go_patch_allowed"]
            )
            self.assertEqual(second["status"], "campaign_already_imported")
            self.assertTrue(
                second["phase11_external_holdout_audit"][
                    "candidate_external_validated"
                ]
            )
            self.assertEqual(
                json.loads(audit_output.read_text(encoding="utf-8"))["status"],
                "external_holdout_validated",
            )


if __name__ == "__main__":
    unittest.main()
