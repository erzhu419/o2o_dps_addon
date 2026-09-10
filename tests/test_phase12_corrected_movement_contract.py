from __future__ import annotations

import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_summary import _phase12_exact_chain


def _row(sequence: int, event: str, **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "sequence": sequence,
        "time": float(sequence),
        "event": event,
        "state": {},
    }
    row.update(fields)
    return row


class Phase12CorrectedMovementContractTests(unittest.TestCase):
    def test_moving_slam_is_a_success_chain(self) -> None:
        trial_rows = [
            _row(1, "CALIBRATION_ACTION_REQUESTED", marker={"action": "cast"}),
            _row(2, "SPELL_CAST_EVENT", spellID=45961, castSucceeded=True),
            _row(3, "SPELL_START_SELF", spellID=45961),
            _row(4, "SPELL_GO_SELF", spellID=45961),
            _row(5, "SPELL_DAMAGE_EVENT_SELF", spellID=45961),
        ]

        result = _phase12_exact_chain(
            rows=trial_rows,
            trial_rows=trial_rows,
            completion={"phase12Coverage": "observed"},
            task_id="warrior_fury_movement_range_latency_phase12",
            stage_id="slam_moving",
        )

        self.assertTrue(result["exact_action_chain_complete"])
        self.assertEqual(result["failure_reasons"], [])

    def test_outside_whirlwind_requires_successful_cast_and_zero_targets(self) -> None:
        trial_rows = [
            _row(1, "CALIBRATION_ACTION_REQUESTED", marker={"action": "cast"}),
            _row(2, "SPELL_CAST_EVENT", spellID=1680, castSucceeded=True),
            _row(3, "SPELL_GO_SELF", spellID=1680, targetsHit=0, targetsMissed=0),
        ]

        result = _phase12_exact_chain(
            rows=trial_rows,
            trial_rows=trial_rows,
            completion={"phase12Coverage": "observed"},
            task_id="warrior_fury_movement_range_latency_phase12",
            stage_id="ww_outside_8",
        )

        self.assertTrue(result["exact_action_chain_complete"])
        self.assertEqual(result["failure_reasons"], [])

        trial_rows[-1]["targetsHit"] = 1
        failed = _phase12_exact_chain(
            rows=trial_rows,
            trial_rows=trial_rows,
            completion={"phase12Coverage": "observed"},
            task_id="warrior_fury_movement_range_latency_phase12",
            stage_id="ww_outside_8",
        )
        self.assertFalse(failed["exact_action_chain_complete"])
        self.assertIn(
            "server_go_did_not_report_zero_targets_hit",
            failed["failure_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
