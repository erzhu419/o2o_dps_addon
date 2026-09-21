from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS
from o2o_dps.fury_paired_multiseed_runner_v2 import derive_simulator_seed
from o2o_dps.upper_kara_two_wave_baseline_panel_v1 import (
    PROTOCOL_ID,
    baseline_policy_ids_for_build_v1,
    build_upper_kara_continuous_two_wave_baseline_case_v1,
    run_upper_kara_continuous_two_wave_baselines_v1,
)


class UpperKaraTwoWaveBaselinePanelV1Tests(unittest.TestCase):
    def test_scenario_exactly_matches_shifted_two_wave_case(self):
        case, scenario = build_upper_kara_continuous_two_wave_baseline_case_v1(
            107,
            build_id=BUILD_IDS[0],
            loadout_id="contra_turtle_burst__quickness",
            first_wave_arrival_ms=7_000,
        )
        self.assertEqual(case.request, scenario["request"])
        self.assertEqual(
            case.dynamic_load.config.to_wire(),
            scenario["dynamic_load_config"],
        )
        self.assertEqual(
            case.dynamic_load.config.idle_advance_horizon_ms,
            scenario["horizon_ms"],
        )
        self.assertFalse(
            scenario["precombat"][
                "native_baseline_policy_receives_manual_burst_inputs"
            ]
        )
        self.assertEqual(
            7_000,
            case.case_spec["continuous_route"]["player_arrival"][
                "first_wave_in_range_at_ms_relative_to_pull"
            ],
        )

    def test_runner_binds_three_source_policies_and_paired_seed(self):
        captured = {}
        build_id = BUILD_IDS[0]
        baseline_policy_ids = baseline_policy_ids_for_build_v1(build_id)

        def fake_panel(**kwargs):
            captured.update(kwargs)
            case = kwargs["case_override"]
            simulator_seed = derive_simulator_seed(
                109,
                case.dynamic_load.request_sha256,
                namespace=PROTOCOL_ID,
            )
            return {
                "simulator_seed": simulator_seed,
                "rows": [
                    {
                        "policy_id": policy_id,
                        "status": "COMPLETED",
                        "own_effective_damage": 100.0 + index,
                    }
                    for index, policy_id in enumerate(baseline_policy_ids)
                ],
            }

        payload = run_upper_kara_continuous_two_wave_baselines_v1(
            109,
            build_id=build_id,
            loadout_id="contra_turtle_burst__mighty_rage",
            first_wave_arrival_ms=7_000,
            panel_runner=fake_panel,
        )
        self.assertEqual("COMPLETE_THREE_NATIVE_BASELINES", payload["status"])
        self.assertEqual(baseline_policy_ids, captured["baseline_ids"])
        self.assertEqual("raid_a", captured["deployed_contra_controller"])
        self.assertEqual(3, len(payload["rows"]))
        self.assertEqual(7_000, payload["first_wave_arrival_ms"])
        self.assertTrue(payload["comparison_contract"]["same_player_arrival_branch"])
        self.assertEqual(
            "raid_a", payload["comparison_contract"]["deployed_contra_controller"]
        )
        self.assertFalse(
            payload["comparison_contract"][
                "external_manual_burst_inputs_supplied"
            ]
        )

    def test_dual_wield_build_selects_deployed_raid_b_controller(self):
        captured = {}
        build_id = BUILD_IDS[1]
        baseline_policy_ids = baseline_policy_ids_for_build_v1(build_id)

        def fake_panel(**kwargs):
            captured.update(kwargs)
            case = kwargs["case_override"]
            return {
                "simulator_seed": derive_simulator_seed(
                    113,
                    case.dynamic_load.request_sha256,
                    namespace=PROTOCOL_ID,
                ),
                "rows": [
                    {"policy_id": policy_id, "status": "COMPLETED"}
                    for policy_id in baseline_policy_ids
                ],
            }

        payload = run_upper_kara_continuous_two_wave_baselines_v1(
            113,
            build_id=build_id,
            loadout_id="contra_turtle_burst__mighty_rage",
            panel_runner=fake_panel,
        )
        self.assertEqual("raid_b", captured["deployed_contra_controller"])
        self.assertEqual(baseline_policy_ids, captured["baseline_ids"])
        self.assertEqual(
            "raid_b", payload["comparison_contract"]["deployed_contra_controller"]
        )


if __name__ == "__main__":
    unittest.main()
