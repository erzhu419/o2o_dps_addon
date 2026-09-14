from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS, build_two_wave_build_case_v1
from o2o_dps.factored_policy_context_v1 import extract_factored_policy_context_v1


class FactoredPolicyContextV1Tests(unittest.TestCase):
    def test_single_target_build_and_declared_wave_assumptions(self) -> None:
        case = build_development_wave_case_v1(123)
        player = case.request["raid"]["parties"][0]["players"][0]
        main_id = player["equipment"]["items"][14]["id"]
        context = extract_factored_policy_context_v1(
            case,
            item_database={"items": [
                {"id": main_id, "type": 13, "handType": 4, "weaponSpeed": 3.4},
            ]},
        )
        self.assertEqual("TWO_HAND", context.build.weapon_mode)
        self.assertEqual(3.4, context.build.main_hand_speed_s)
        self.assertIsNone(context.build.off_hand_speed_s)
        self.assertIs(context.build.bloodthirst_known, True)
        self.assertEqual(1, context.wave.route_target_count)
        self.assertEqual("SINGLE_WAVE", context.wave.wave_topology)
        self.assertEqual((1721,), context.wave.target_base_armor_by_index)
        self.assertEqual((126397,), context.wave.target_max_hp_by_index)
        self.assertEqual(13000.0, context.wave.team_dps_assumed)
        self.assertEqual((13000.0,), context.wave.team_dps_prior_by_target)
        self.assertEqual(("8k_to_16k",), context.wave.team_dps_prior_band_by_target)
        self.assertAlmostEqual(126397 / 13000, context.wave.background_team_ttk_s_prior_by_target[0])
        self.assertEqual(("8s_to_15s",), context.wave.background_team_ttk_band_by_target)
        self.assertEqual("DECLARED_STATIC_TEAM_DPS_PRIOR", context.wave.team_prior_source)

    def test_multi_target_excludes_death_fitted_team_rate_and_ids(self) -> None:
        base = build_development_wave_case_v1(456)
        request, spec = deepcopy(base.request), deepcopy(base.case_spec)
        request["encounter"]["targets"].append(deepcopy(request["encounter"]["targets"][0]))
        player = request["raid"]["parties"][0]["players"][0]
        player["equipment"]["items"][14] = {"id": 18832}
        player["equipment"]["items"][15] = {"id": 19866}
        spec["initial_state"]["target_max_hp"] = [50000, 80000]
        spec["initial_state"]["target_base_armor"] = [1700, 2100]
        spec["team_background"] = {
            "model": "DEATH_FITTED_RATE",
            "per_target": [{"source_death_offset_ms": 1200, "damage_per_hit": 99999}],
        }
        context = extract_factored_policy_context_v1(
            replace(base, request=request, case_spec=spec),
            item_database={"items": [
                {"id": 18832, "type": 13, "handType": 2, "weaponSpeed": 2.5},
                {"id": 19866, "type": 13, "handType": 3, "weaponSpeed": 1.7},
            ]},
        )
        self.assertEqual("DUAL_WIELD", context.build.weapon_mode)
        self.assertEqual(2.5, context.build.main_hand_speed_s)
        self.assertEqual(1.7, context.build.off_hand_speed_s)
        self.assertEqual(2, context.wave.route_target_count)
        self.assertEqual("SINGLE_WAVE", context.wave.wave_topology)
        self.assertEqual((1700, 2100), context.wave.target_base_armor_by_index)
        self.assertEqual((50000, 80000), context.wave.target_max_hp_by_index)
        self.assertIsNone(context.wave.team_dps_assumed)
        self.assertEqual((None, None), context.wave.team_dps_prior_by_target)
        self.assertEqual(("unknown", "unknown"), context.wave.team_dps_prior_band_by_target)
        self.assertEqual("UNAVAILABLE", context.wave.team_prior_source)
        self.assertNotIn("seed", str(context.as_dict()))
        self.assertNotIn("source_death", str(context.as_dict()))
        self.assertNotIn("build_id", str(context.as_dict()))

    def test_leave_one_out_team_rate_becomes_labeled_offline_prior(self) -> None:
        base = build_development_wave_case_v1(457)
        request, spec = deepcopy(base.request), deepcopy(base.case_spec)
        request["encounter"]["targets"].append(deepcopy(request["encounter"]["targets"][0]))
        spec["initial_state"]["target_max_hp"] = [120000, 80000]
        spec["initial_state"]["target_base_armor"] = [1721, 1721]
        spec["team_background"] = {
            "model": "EXOGENOUS_PER_TARGET_DIRECT_GUID_LEAVE_ONE_OUT_RATE_EXTRAPOLATED",
            "future_schedule_policy_visible": False,
            "per_target": [
                {
                    "damage_per_hit": 10000,
                    "damage_interval_ms": 500,
                    "focal_player_direct_guid_excluded_from_source_budget": True,
                },
                {
                    "damage_per_hit": 5000,
                    "damage_interval_ms": 500,
                    "focal_player_direct_guid_excluded_from_source_budget": True,
                },
            ],
        }

        context = extract_factored_policy_context_v1(
            replace(base, request=request, case_spec=spec)
        )

        self.assertEqual((20000.0, 10000.0), context.wave.team_dps_prior_by_target)
        self.assertEqual(("16k_plus", "8k_to_16k"), context.wave.team_dps_prior_band_by_target)
        self.assertEqual((6.0, 8.0), context.wave.background_team_ttk_s_prior_by_target)
        self.assertEqual(("under_8s", "8s_to_15s"), context.wave.background_team_ttk_band_by_target)
        self.assertEqual(
            "OFFLINE_REGISTRY_LEAVE_ONE_OUT_RATE_PRIOR",
            context.wave.team_prior_source,
        )
        self.assertNotIn("source_death", str(context.as_dict()))

    def test_missing_item_definitions_do_not_invent_weapon_mechanics(self) -> None:
        context = extract_factored_policy_context_v1(build_development_wave_case_v1(789))
        self.assertEqual("UNKNOWN", context.build.weapon_mode)
        self.assertIsNone(context.build.main_hand_speed_s)
        self.assertIs(context.build.bloodthirst_known, True)

    def test_two_sequential_targets_are_not_one_two_target_pull(self) -> None:
        case, _ = build_two_wave_build_case_v1(790, BUILD_IDS[0])
        context = extract_factored_policy_context_v1(case)
        self.assertEqual(2, context.wave.route_target_count)
        self.assertEqual("SEQUENTIAL_WAVES", context.wave.wave_topology)


if __name__ == "__main__":
    unittest.main()
