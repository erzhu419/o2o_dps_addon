from __future__ import annotations

import copy
import math
import unittest

from o2o_dps.white_rage_formula_audit import wowsims_rage_conversion
from o2o_dps.white_rage_phase9_formula_audit import (
    CURRENT_ID,
    M0_ID,
    M1_ID,
    M2_ID,
    WhiteRagePhase9AuditError,
    audit_phase9,
)


_CANDIDATES = {
    M0_ID: (1.125, 1.5, 3.3),
    M1_ID: (1.10, 1.575, 3.40),
    M2_ID: (10 / 9, 14 / 9, 10 / 3),
    CURRENT_ID: (1.0, 0.0, 0.0),
}
_SAMPLES = (
    ("sunder_0", "critical", 360),
    ("sunder_0", "critical", 420),
    ("sunder_0", "ordinary", 180),
    ("sunder_0", "ordinary", 190),
    ("sunder_5", "critical", 500),
    ("sunder_5", "critical", 540),
    ("sunder_5", "ordinary", 240),
    ("sunder_5", "ordinary", 260),
)


def _allowed(candidate_id: str, damage: int, critical: bool) -> set[float]:
    damage_coefficient, ordinary_speed, critical_speed = _CANDIDATES[candidate_id]
    damage_term = damage * 7.5 / wowsims_rage_conversion(60)
    predicted = damage_coefficient * damage_term + (
        critical_speed if critical else ordinary_speed
    ) * 3.2
    scaled = predicted * 10
    return {math.floor(scaled) / 10, math.ceil(scaled) / 10}


def _observations(candidate_id: str, *, choose: str) -> list[float]:
    values = []
    for _stratum, outcome, damage in _SAMPLES:
        allowed = _allowed(candidate_id, damage, outcome == "critical")
        values.append(min(allowed) if choose == "min" else max(allowed))
    return values


def _ambiguous_m1_m2_observations() -> list[float]:
    values = []
    for _stratum, outcome, damage in _SAMPLES:
        critical = outcome == "critical"
        shared = _allowed(M1_ID, damage, critical) & _allowed(
            M2_ID, damage, critical
        )
        if not shared:
            raise AssertionError("test damage must leave an M1/M2 shared quantized value")
        values.append(min(shared))
    return values


def _summary(observations: list[float]) -> dict:
    trials = []
    for trial_number, ((stratum, outcome, damage), observed) in enumerate(
        zip(_SAMPLES, observations), start=1
    ):
        stacks = 0 if stratum == "sunder_0" else 5
        armor = 4211 if stacks == 0 else 1961
        raw_gain = int(round(observed * 10))
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
                    "raw_gain": raw_gain,
                    "raw_scale": 10,
                    "capped": False,
                    "identifiable": True,
                    "unbridled_wrath_proc": {
                        "observed": False,
                        "raw_rage_gain": 0,
                    },
                    "base_gain_after_known_proc": observed,
                    "base_gain_raw_after_known_proc": raw_gain,
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
                },
                "quality_flags": [],
            }
        )
    return {
        "kind": "brainofcat_calibration_summary",
        "specialized_runs": [
            {
                "task_id": "warrior_white_swing_rage_formula_holdout",
                "task_run_id": "phase9-run-1",
                "analyzer": "white_swing_rage_formula_holdout_v1",
                "campaign": {
                    "campaign_id": "warrior_white_swing_rage_formula_holdout_phase9",
                    "campaign_run_id": "phase9-campaign-1",
                },
                "completion_confirmed": True,
                "requested_trials": 8,
                "completed_trials": 8,
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
                },
                "sample_quota": {
                    "required_per_armor_outcome": 2,
                    "counts": {
                        "sunder_0_critical": 2,
                        "sunder_0_ordinary": 2,
                        "sunder_5_critical": 2,
                        "sunder_5_ordinary": 2,
                    },
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


class WhiteRagePhase9FormulaAuditTests(unittest.TestCase):
    def test_identifies_m1_and_publishes_only_its_exact_patch(self) -> None:
        document = audit_phase9(_summary(_observations(M1_ID, choose="max")))

        self.assertEqual(document["status"], "formula_identified")
        gate = document["conclusion_gate"]
        self.assertEqual(gate["selected_candidate_id"], M1_ID)
        self.assertTrue(gate["M1_matches_all_samples"])
        self.assertFalse(gate["M2_matches_all_samples"])
        self.assertTrue(gate["M0_rejected"])
        self.assertTrue(gate["current_damage_only_rejected"])
        self.assertTrue(gate["simulator_patch_allowed"])
        self.assertEqual(gate["simulator_patch"]["candidate_id"], M1_ID)
        self.assertEqual(gate["simulator_patch"]["damage_coefficient"], 1.10)

    def test_identifies_rational_m2_and_preserves_exact_coefficients(self) -> None:
        document = audit_phase9(_summary(_observations(M2_ID, choose="min")))

        self.assertEqual(document["status"], "formula_identified")
        gate = document["conclusion_gate"]
        self.assertEqual(gate["selected_candidate_id"], M2_ID)
        self.assertFalse(gate["M1_matches_all_samples"])
        self.assertTrue(gate["M2_matches_all_samples"])
        self.assertTrue(gate["simulator_patch_allowed"])
        self.assertEqual(
            gate["simulator_patch"]["damage_coefficient_exact"], "10/9"
        )
        self.assertEqual(
            gate["simulator_patch"]["critical_speed_coefficient_exact"], "10/3"
        )

    def test_fails_closed_when_m1_and_m2_are_quantization_compatible(self) -> None:
        document = audit_phase9(_summary(_ambiguous_m1_m2_observations()))

        self.assertEqual(document["status"], "formula_not_identified")
        gate = document["conclusion_gate"]
        self.assertEqual(
            gate["replacement_candidates_matching_all"], [M1_ID, M2_ID]
        )
        self.assertFalse(gate["replacement_formula_identified"])
        self.assertFalse(gate["simulator_patch_allowed"])
        self.assertIsNone(gate["simulator_patch"])
        self.assertEqual(
            gate["reason"], "M1_and_M2_both_match_phase9_holdout_evidence"
        )

    def test_m0_compatible_evidence_never_authorizes_a_patch(self) -> None:
        document = audit_phase9(_summary(_observations(M0_ID, choose="min")))

        self.assertEqual(document["status"], "formula_not_identified")
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])

    def test_rejects_nonclean_or_non_tenths_input_contract(self) -> None:
        dirty = _summary(_observations(M1_ID, choose="max"))
        dirty["specialized_runs"][0]["trials"][0]["quality_flags"] = ["proc"]
        with self.assertRaisesRegex(WhiteRagePhase9AuditError, "not clean"):
            audit_phase9(dirty)

        wrong_scale = _summary(_observations(M1_ID, choose="max"))
        wrong_scale["specialized_runs"][0]["trials"][0]["rage"]["raw_scale"] = 1
        with self.assertRaisesRegex(WhiteRagePhase9AuditError, "scale must be 10"):
            audit_phase9(wrong_scale)

    def test_declared_quota_drift_is_insufficient_and_cannot_patch(self) -> None:
        summary = _summary(_observations(M1_ID, choose="max"))
        summary["specialized_runs"][0]["sample_quota"]["complete"] = False

        document = audit_phase9(summary)

        self.assertEqual(document["status"], "insufficient_evidence")
        self.assertFalse(document["conclusion_gate"]["simulator_patch_allowed"])
        self.assertIn(
            "phase9_declared_sample_quota_is_not_exact",
            document["evidence_gate"]["reasons"],
        )

    def test_fixed_control_drift_is_rejected_before_model_comparison(self) -> None:
        summary = _summary(_observations(M1_ID, choose="max"))
        summary["specialized_runs"][0]["trials"][7]["combat_context"][
            "attack_power"
        ] = 1181

        with self.assertRaisesRegex(WhiteRagePhase9AuditError, "control drifted"):
            audit_phase9(summary)

    def test_input_is_not_mutated(self) -> None:
        summary = _summary(_observations(M1_ID, choose="max"))
        before = copy.deepcopy(summary)

        audit_phase9(summary)

        self.assertEqual(summary, before)


if __name__ == "__main__":
    unittest.main()
