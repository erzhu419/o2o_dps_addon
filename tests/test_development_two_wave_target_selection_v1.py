from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.development_two_wave_target_selection_v1 import (
    SELECTED, build_two_wave_target_case_v1, source_rows_two_v1,
)


class TwoWaveSourceSelectionTests(unittest.TestCase):
    def test_frozen_source_only_quantiles_and_distinct_raids(self) -> None:
        rows = source_rows_two_v1()
        self.assertEqual(set(rows), set(SELECTED))
        self.assertEqual(len({row["source_identity"]["instance_id"] for row in rows.values()}), 2)
        self.assertLess(rows["two_short_q20"]["horizon"]["milliseconds"],
                        rows["two_long_q80"]["horizon"]["milliseconds"])

    def test_both_cases_subtract_direct_guid_focal_from_team_model(self) -> None:
        for stratum in SELECTED:
            with self.subTest(stratum=stratum):
                case, _ = build_two_wave_target_case_v1(2026091701, stratum)
                self.assertEqual(len(case.dynamic_load.config.target_health), 2)
                modeled = case.case_spec["team_background"]["per_target"]
                focal = SELECTED[stratum][3]["direct_damage_by_target"]
                for index, target in enumerate(modeled):
                    self.assertEqual(target["excluded_focal_direct_guid_damage"], focal[index])


if __name__ == "__main__":
    unittest.main()
