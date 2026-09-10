from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.white_rage_formula_audit import (
    WhiteRageFormulaAuditError,
    audit_phase6,
    current_wowsims_white_rage,
    wowsims_rage_conversion,
)


ARMORS = {0: 4211, 1: 3761, 3: 2861, 5: 1961}


def _trial(
    trial: int,
    stack: int,
    damage: int,
    base_rage: int,
    *,
    proc_rage: int = 0,
    identifiable: bool = True,
) -> dict:
    return {
        "trial": trial,
        "swing": {
            "hand": "main_hand",
            "damage": damage,
            "hit_info": 0,
            "critical": False,
            "glancing": trial % 2 == 0,
        },
        "rage": {
            "gain": base_rage + proc_rage,
            "identifiable": identifiable,
            "base_gain_after_known_proc": base_rage if identifiable else None,
            "unbridled_wrath_proc": {
                "observed": proc_rage > 0,
                "normalized_rage_gain": proc_rage,
            },
        },
        "combat_context": {
            "player_level": 60,
            "main_hand_speed": 2.342,
            "target_guid": "0xF13000C55226FDD2",
            "target_armor": ARMORS[stack],
        },
        "armor_stratum": {
            "planned_sunder_stacks": stack,
            "observed_sunder_stacks": stack,
        },
        "quality_flags": [],
    }


def _summary(trials: list[dict], *, completion_confirmed: bool = True) -> dict:
    return {
        "kind": "brainofcat_calibration_summary",
        "specialized_runs": [
            {
                "task_id": "warrior_white_swing_rage_armor_strata",
                "task_run_id": "phase6-task-run",
                "analyzer": "white_swing_rage_armor_strata_v1",
                "completion_confirmed": completion_confirmed,
                "trials": trials,
            }
        ],
    }


class WhiteRageFormulaAuditTests(unittest.TestCase):
    def test_level_sixty_conversion_matches_current_go_formula(self) -> None:
        expected = 0.0091107836 * 60 * 60 + 3.225598133 * 60 + 4.2652911
        self.assertAlmostEqual(wowsims_rage_conversion(60), expected)

    def test_four_strata_report_current_formula_mismatch_without_fitting(self) -> None:
        damages = {0: 267, 1: 300, 3: 350, 5: 400}
        trials = [
            _trial(index, stack, damages[stack], 15 + index, proc_rage=2 if stack == 3 else 0)
            for index, stack in enumerate((0, 1, 3, 5), start=1)
        ]

        document = audit_phase6(_summary(trials))

        self.assertEqual(document["validation_status"], "not_matched")
        self.assertTrue(document["evidence_gate"]["sufficient"])
        self.assertEqual(document["evidence_gate"]["covered_sunder_stacks"], [0, 1, 3, 5])
        self.assertGreater(document["totals"]["mismatch_sample_count"], 0)
        self.assertFalse(document["simulator_formula"]["replacement_formula_fitted"])
        proc_sample = document["armor_strata"][2]["samples"][0]
        self.assertEqual(proc_sample["observed_net_rage"], 20)
        self.assertEqual(proc_sample["known_proc_rage"], 2)
        self.assertEqual(proc_sample["observed_base_rage"], 18)

    def test_floor_or_ceil_integer_observations_match(self) -> None:
        trials = []
        for index, stack in enumerate((0, 1, 3, 5), start=1):
            damage = 240 + 20 * index
            observed = math.floor(current_wowsims_white_rage(damage, 60))
            trials.append(_trial(index, stack, damage, observed))

        document = audit_phase6(_summary(trials))

        self.assertEqual(document["validation_status"], "matched")
        self.assertEqual(document["totals"]["mismatch_sample_count"], 0)
        self.assertTrue(
            document["conclusion_gate"][
                "current_formula_matches_all_eligible_samples"
            ]
        )

    def test_missing_eligible_stratum_is_insufficient_evidence(self) -> None:
        trials = [
            _trial(index, stack, 260 + index, 15)
            for index, stack in enumerate((0, 1, 3), start=1)
        ]

        document = audit_phase6(_summary(trials))

        self.assertEqual(document["validation_status"], "insufficient_evidence")
        self.assertEqual(document["evidence_gate"]["missing_sunder_stacks"], [5])
        self.assertIsNone(
            document["conclusion_gate"][
                "current_formula_matches_all_eligible_samples"
            ]
        )

    def test_inconsistent_known_proc_subtraction_is_rejected(self) -> None:
        trials = [
            _trial(index, stack, 260 + index, 15)
            for index, stack in enumerate((0, 1, 3, 5), start=1)
        ]
        trials[0]["rage"]["gain"] = 20

        with self.assertRaises(WhiteRageFormulaAuditError) as raised:
            audit_phase6(_summary(trials))

        self.assertIn("gain minus known proc", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
