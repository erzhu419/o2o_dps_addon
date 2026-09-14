from __future__ import annotations

import unittest

from o2o_dps.development_historical_build_wave_case_v1 import (
    DEFAULT_ITEM_DATABASE,
    _equipment_names,
    _weapon_mode,
    build_historical_representative_development_wave_case_v1,
    build_historical_representative_development_wave_scenario_v1,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import validate_dynamic_load_request_v3
from o2o_dps.fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from o2o_dps.fury_paired_multiseed_runner_v4 import normalize_runner_scenarios
from o2o_dps.historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)


class DevelopmentHistoricalBuildWaveCaseV1Tests(unittest.TestCase):
    def test_relocated_compact_build_artifacts_are_supported(self) -> None:
        derived = DEFAULT_SELECTOR_MANIFEST.parents[2]
        case = build_historical_representative_development_wave_case_v1(
            20260913,
            rank=7,
            selector_manifest_path=DEFAULT_SELECTOR_MANIFEST,
            profile_path_overrides={
                "representatives": DEFAULT_SELECTOR_MANIFEST.parent / "representatives.jsonl",
                "catalog_manifest": derived / "historical_build_catalog/v1/manifest.json",
                "catalog_data": derived / "historical_build_catalog/v1/catalog.jsonl.gz",
            },
        )
        self.assertEqual(7, case.case_spec["historical_build"]["representative_rank"])

    def test_rank7_exact_historical_dual_wield_build_in_model_wave(self) -> None:
        case = build_historical_representative_development_wave_case_v1(20260913)
        spec = case.case_spec
        player = case.request["raid"]["parties"][0]["players"][0]
        self.assertEqual("EXACT_HISTORICAL_BUILD_TRANSPLANTED_TO_MODEL_WAVE", spec["build_role"])
        self.assertEqual("0x00000000000943FC", spec["source_build_ref"]["player_guid"])
        self.assertFalse(spec["historical_exact"])
        self.assertFalse(spec["historical_player_policy_used"])
        self.assertEqual(
            "RAID_A_BEHAVIOR_TRANSPLANT_ON_DUAL_WIELD_BUILD",
            spec["baseline_fidelity"]["deployed_contra_build_interpretation"],
        )
        self.assertFalse(spec["baseline_fidelity"]["source_faithful_four_way_eligible"])
        self.assertEqual("ADMITTED", spec["historical_build"]["composer_admission"]["status"])
        self.assertEqual("RaceNightElf", player["race"])
        self.assertEqual("30205020302-05030005525010251", player["talentsString"])
        self.assertEqual((19019, 23577), tuple(
            player["equipment"]["items"][index]["id"] for index in (14, 15)
        ))
        self.assertEqual({}, player["consumes"])
        self.assertEqual({}, player["buffs"])
        self.assertEqual({}, case.request["raid"]["buffs"])
        self.assertEqual({}, case.request["raid"]["debuffs"])
        self.assertEqual(50, player["warrior"]["options"]["startingRage"])
        self.assertEqual(0, player["warrior"]["options"]["ravagerRank"])
        self.assertEqual(17, len(case.target_contexts[0].equipped_item_names))
        validate_dynamic_load_request_v3(case.dynamic_load, case.request)

    def test_runner_wire_keeps_build_request_and_context_bound(self) -> None:
        scenario = build_historical_representative_development_wave_scenario_v1(20260913)
        normalized = normalize_runner_scenarios([scenario])[0]
        case = build_historical_representative_development_wave_case_v1(20260913)
        self.assertEqual(case.request, scenario["request"])
        self.assertEqual(case.dynamic_load.request_sha256, normalized["request_sha256"])
        self.assertEqual(case.target_contexts, target_contexts_from_runner_v4(scenario["target_context_bundle"]))
        self.assertIn(
            "HISTORICAL_BUILD_TRANSPLANTED_TO_OTHER_RAID_MODEL",
            scenario["scenario_model"]["limitation_codes"],
        )
        self.assertIn(
            "RAID_A_BEHAVIOR_TRANSPLANT_ON_DUAL_WIELD_BUILD",
            scenario["scenario_model"]["limitation_codes"],
        )

    def test_rank9_is_a_two_hand_bloodthirst_native_panel_candidate(self) -> None:
        case = build_historical_representative_development_wave_case_v1(
            20260913, rank=9
        )
        scenario = build_historical_representative_development_wave_scenario_v1(
            20260913, rank=9
        )
        self.assertEqual(case.request, scenario["request"])
        self.assertEqual(
            case.dynamic_load.request_sha256,
            scenario["scenario_model"]["request_sha256"],
        )
        items = case.request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
        self.assertEqual(22815, items[14]["id"])
        self.assertEqual({}, items[15])
        self.assertEqual("RAID_A_TWO_HAND_BUILD_CANDIDATE", case.case_spec["baseline_fidelity"]["deployed_contra_build_interpretation"])
        self.assertEqual("NOT_YET_ASSESSED", case.case_spec["baseline_fidelity"]["source_faithful_four_way_eligible"])

    def test_rank2_and_rank14_shields_are_not_classified_as_dual_wield(self) -> None:
        for rank in (2, 14):
            with self.subTest(rank=rank):
                case = build_historical_representative_development_wave_case_v1(
                    20260913, rank=rank
                )
                items = case.request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
                self.assertEqual(23043, items[15]["id"])
                self.assertEqual(
                    "ONE_HAND_WITH_EQUIPPED_OFFHAND",
                    _weapon_mode(items, DEFAULT_ITEM_DATABASE),
                )
                self.assertEqual(
                    "ONE_HAND_WITH_EQUIPPED_OFFHAND",
                    case.case_spec["baseline_fidelity"]["weapon_mode"],
                )
                self.assertFalse(
                    case.case_spec["baseline_fidelity"]["source_faithful_four_way_eligible"]
                )

    def test_two_hand_empty_offhand_is_preserved_but_no_bt_is_not_panel_eligible(self) -> None:
        self.assertEqual(
            ("Might of Menethil",),
            _equipment_names([{"id": 22798}, {}], DEFAULT_ITEM_DATABASE),
        )
        with self.assertRaisesRegex(ValueError, "rank 1 lacks Bloodthirst; both pinned Contra Fury source lanes"):
            build_historical_representative_development_wave_case_v1(
                20260913, rank=1
            )


if __name__ == "__main__":
    unittest.main()
