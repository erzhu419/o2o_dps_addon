from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
)
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    derive_simulator_seed,
    sha256_json,
)
from o2o_dps.fury_runtime_bound_deployed_contra_raid_b_v1 import (
    POLICY_ID as RAID_B_POLICY_ID,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import normalize_runner_scenarios
from o2o_dps.development_precombat_wave_case_v1 import (
    DEATH_WISH_ACTION,
    MIGHTY_RAGE_POTION_ACTION,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import (
    _PrecombatBaselineBridgeFacade,
    build_upper_kara_exact_baseline_case_v1,
    run_upper_kara_exact_baseline_cell_v1,
)
from o2o_dps.precombat_contract_v1 import PrecombatActionsConfigV1
from o2o_dps.sim_bridge_dynamic_v3 import DynamicLoadResultV3


class UpperKaraExactBaselinePanelV1Tests(unittest.TestCase):
    def test_unmodified_source_policy_idles_to_pull_before_first_decision(self):
        class Bridge:
            def __init__(self):
                self.calls = []

            def load_dynamic_v3_precombat(self, request, seed, config, precombat):
                self.calls.append(("load", seed))
                return DynamicLoadResultV3(
                    receipt=None,
                    state={
                        "time_ms": 0,
                        "needs_input": True,
                        "finished": False,
                        "precombat": {
                            "active": True,
                            "relative_time_ms": -3_000,
                        },
                    },
                )

            def wait(self, wait_ms):
                self.calls.append(("wait", wait_ms))
                return {
                    "time_ms": 0,
                    "needs_input": False,
                    "finished": False,
                    "precombat": {
                        "active": True,
                        "relative_time_ms": -3_000,
                    },
                }

            def advance(self):
                self.calls.append(("advance",))
                return {
                    "time_ms": 3_000,
                    "needs_input": True,
                    "finished": False,
                    "precombat": {
                        "active": False,
                        "relative_time_ms": 0,
                    },
                }

        bridge = Bridge()
        facade = _PrecombatBaselineBridgeFacade(
            bridge,
            PrecombatActionsConfigV1(
                pull_time_ms=3_000,
                self_actions=(DEATH_WISH_ACTION,),
            ),
        )
        loaded = facade.load_dynamic_v3({}, 17, object())
        self.assertEqual(3_000, loaded.state["time_ms"])
        self.assertEqual(
            [("load", 17), ("wait", 3_000), ("advance",)],
            bridge.calls,
        )

    def test_scenario_wire_uses_the_bound_build_and_unchanged_wave(self):
        case, scenario = build_upper_kara_exact_baseline_case_v1(
            2026091421,
            representative_rank=11,
            stratum="multi_2",
        )
        self.assertEqual(scenario["request"], case.request)
        self.assertEqual(
            scenario["dynamic_load_config"],
            case.dynamic_load.config.to_wire(),
        )
        self.assertEqual(
            scenario["scenario_model"]["request_sha256"],
            case.dynamic_load.request_sha256,
        )
        self.assertEqual(2, len(scenario["target_context_bundle"]["contexts"]))
        for row in scenario["target_context_bundle"]["contexts"]:
            context = case.target_contexts[row["target_index"]]
            self.assertEqual(row["equipped_item_names"], list(context.equipped_item_names))

    def test_multi_target_uses_all_three_native_raid_b_policy_ids(self):
        captured = {}

        def fake_panel(**kwargs):
            captured.update(kwargs)
            return {
                "simulator_seed": derive_simulator_seed(
                    kwargs["master_seed"],
                    sha256_json(kwargs["scenario_override"]["request"]),
                    namespace=PROTOCOL_ID,
                ),
                "rows": [
                    {
                        "policy_id": policy_id,
                        "status": "COMPLETED",
                        "own_effective_damage": float(index + 1),
                    }
                    for index, policy_id in enumerate(kwargs["baseline_ids"])
                ]
            }

        result = run_upper_kara_exact_baseline_cell_v1(
            2026091422,
            representative_rank=7,
            stratum="multi_2",
            panel_runner=fake_panel,
        )
        self.assertEqual(result["status"], "COMPLETE_THREE_NATIVE_BASELINES")
        self.assertEqual(
            tuple(result["baseline_policy_ids"]),
            (CAT_POLICY_ID, CONTRA260817_POLICY_ID, RAID_B_POLICY_ID),
        )
        self.assertEqual(captured["deployed_contra_controller"], "raid_b")
        self.assertTrue(result["comparison_contract"]["multi_target_uses_raid_b_executors"])

    def test_failed_native_lane_stays_incomplete_instead_of_zero(self):
        def fake_panel(**kwargs):
            rows = []
            for index, policy_id in enumerate(kwargs["baseline_ids"]):
                rows.append({
                    "policy_id": policy_id,
                    "status": "COMPLETED" if index < 2 else "UNSUPPORTED",
                    "own_effective_damage": float(index + 1) if index < 2 else None,
                })
            return {
                "simulator_seed": derive_simulator_seed(
                    kwargs["master_seed"],
                    sha256_json(kwargs["scenario_override"]["request"]),
                    namespace=PROTOCOL_ID,
                ),
                "rows": rows,
            }

        result = run_upper_kara_exact_baseline_cell_v1(
            2026091423,
            representative_rank=9,
            stratum="q05",
            panel_runner=fake_panel,
        )
        self.assertEqual(result["status"], "INCOMPLETE_NATIVE_BASELINE_LANES")
        self.assertIsNone(result["rows"][-1]["own_effective_damage"])
        self.assertFalse(result["comparison_contract"]["missing_or_failed_lane_scored_as_zero"])

    def test_wrong_native_simulator_seed_is_rejected(self):
        def fake_panel(**kwargs):
            return {
                "simulator_seed": 1,
                "rows": [
                    {"policy_id": policy_id, "status": "COMPLETED"}
                    for policy_id in kwargs["baseline_ids"]
                ],
            }

        with self.assertRaisesRegex(ValueError, "simulator_seed differs"):
            run_upper_kara_exact_baseline_cell_v1(
                2026091425,
                representative_rank=7,
                stratum="q05",
                panel_runner=fake_panel,
            )

    def test_precombat_request_consumes_and_shifted_horizon_are_shared(self):
        case, scenario = build_upper_kara_exact_baseline_case_v1(
            2026091424,
            representative_rank=7,
            stratum="q05",
            precombat_self_actions=(
                MIGHTY_RAGE_POTION_ACTION,
                DEATH_WISH_ACTION,
            ),
            pull_time_ms=3_000,
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
        self.assertAlmostEqual(
            scenario["request"]["encounter"]["duration"] * 1000.0,
            scenario["horizon_ms"],
        )
        player = scenario["request"]["raid"]["parties"][0]["players"][0]
        self.assertEqual("MightyRagePotion", player["consumes"]["defaultPotion"])
        self.assertTrue(scenario["precombat"]["enabled"])
        normalized = normalize_runner_scenarios([scenario])[0]
        self.assertEqual(scenario["horizon_ms"], normalized["horizon_ms"])

        captured = {}

        def fake_panel(**kwargs):
            captured.update(kwargs)
            return {
                "simulator_seed": derive_simulator_seed(
                    kwargs["master_seed"],
                    sha256_json(kwargs["scenario_override"]["request"]),
                    namespace=PROTOCOL_ID,
                ),
                "rows": [
                    {
                        "policy_id": policy_id,
                        "status": "COMPLETED",
                        "own_effective_damage": float(index + 1),
                    }
                    for index, policy_id in enumerate(kwargs["baseline_ids"])
                ],
            }

        result = run_upper_kara_exact_baseline_cell_v1(
            2026091424,
            representative_rank=7,
            stratum="q05",
            precombat_self_actions=(
                MIGHTY_RAGE_POTION_ACTION,
                DEATH_WISH_ACTION,
            ),
            pull_time_ms=3_000,
            panel_runner=fake_panel,
        )
        self.assertIn("bridge_factory", captured)
        self.assertIn("native_bridge_type", captured)
        self.assertTrue(result["comparison_contract"]["precombat_action_allowlist_shared"])


if __name__ == "__main__":
    unittest.main()
