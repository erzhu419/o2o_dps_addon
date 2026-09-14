from __future__ import annotations

import unittest

from o2o_dps.development_wave_stratified_v1 import build_stratified_wave_case_v1
from o2o_dps.factored_historical_build_wave_case_v1 import bind_historical_build_to_wave_case_v1
from o2o_dps.fury_dynamic_target_semantics_v5 import validate_dynamic_load_request_v3


class FactoredHistoricalBuildWaveCaseV1Tests(unittest.TestCase):
    def test_dual_wield_build_keeps_destination_two_target_wave(self) -> None:
        destination, _ = build_stratified_wave_case_v1(101, "multi_two")
        result = bind_historical_build_to_wave_case_v1(destination, rank=7)
        player = result.request["raid"]["parties"][0]["players"][0]
        self.assertEqual(2, len(result.request["encounter"]["targets"]))
        self.assertEqual(destination.dynamic_load.config, result.dynamic_load.config)
        self.assertEqual(destination.case_spec["source_wave_ref"], result.case_spec["source_wave_ref"])
        self.assertEqual((19019, 23577), tuple(
            player["equipment"]["items"][index]["id"] for index in (14, 15)
        ))
        self.assertEqual(7, result.case_spec["build_transplant"]["representative_rank"])
        self.assertFalse(result.case_spec["build_transplant"]["copied_source_pull_state"])
        self.assertEqual(set(result.target_contexts), {0, 1})
        self.assertEqual(
            result.target_contexts[0].equipped_item_names,
            result.target_contexts[1].equipped_item_names,
        )
        validate_dynamic_load_request_v3(result.dynamic_load, result.request)

    def test_two_hand_build_keeps_destination_team_and_terminal_contract(self) -> None:
        destination, _ = build_stratified_wave_case_v1(102, "single_long")
        result = bind_historical_build_to_wave_case_v1(destination, rank=9)
        items = result.request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
        self.assertEqual(22815, items[14]["id"])
        self.assertEqual({}, items[15])
        self.assertEqual(destination.case_spec["team_background"], result.case_spec["team_background"])
        self.assertEqual(destination.case_spec["terminal"], result.case_spec["terminal"])
        self.assertEqual(destination.case_spec["required_target_ids"], result.case_spec["required_target_ids"])


if __name__ == "__main__":
    unittest.main()
