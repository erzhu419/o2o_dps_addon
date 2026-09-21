from pathlib import Path
import json
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_guard_v1 import (
    SKIP_PLAN,
    observable_causal_guard_from_dict_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import GOBLIN_SAPPER_ACTION
from o2o_dps.upper_kara_burst_reschedule_grid_v1 import (
    DEFAULT_ARRIVAL_HP_THRESHOLDS_V1,
    build_upper_kara_burst_reschedule_grid_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)


class UpperKaraBurstRescheduleGridV1Tests(unittest.TestCase):
    def test_grid_covers_every_target_resource_and_threshold_once(self):
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            127,
            build_id="live_bonereaver",
            loadout_id="contra_turtle_burst__quickness",
            first_wave_arrival_ms=7_000,
        )
        rows = build_upper_kara_burst_reschedule_grid_v1(case)
        actions = {*case.precombat.self_actions, GOBLIN_SAPPER_ACTION}
        expected = {
            (target, action, threshold)
            for target in (0, 1)
            for action in actions
            for threshold in DEFAULT_ARRIVAL_HP_THRESHOLDS_V1
        }
        observed = {
            (row.target_index, row.action, row.hp_threshold_pct)
            for row in rows
        }
        self.assertEqual(expected, observed)
        self.assertEqual(len(expected), len(rows))

    def test_guards_are_action_bound_current_only_and_round_trip(self):
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            131,
            build_id="clean_dual_weapon_probe",
            loadout_id="contra_turtle_burst__no_potion",
        )
        rows = build_upper_kara_burst_reschedule_grid_v1(
            case, hp_thresholds=(35, 65)
        )
        for row in rows:
            self.assertEqual(row.action, row.guard.action_ready)
            self.assertEqual(row.target_index, row.guard.target_index)
            self.assertTrue(row.guard.target_attackable_is)
            self.assertEqual(SKIP_PLAN, row.guard.false_semantics)
            self.assertEqual(
                row.guard,
                observable_causal_guard_from_dict_v1(row.guard.to_dict()),
            )
            wire = row.to_dict()
            self.assertEqual([], wire["future_fields_used"])
            self.assertNotIn("death_time", json.dumps(wire, sort_keys=True))

    def test_threshold_grid_is_explicit_and_unique(self):
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            137,
            build_id="live_bonereaver",
            loadout_id="contra_turtle_burst__rage",
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            build_upper_kara_burst_reschedule_grid_v1(
                case, hp_thresholds=(35, 35)
            )


if __name__ == "__main__":
    unittest.main()
