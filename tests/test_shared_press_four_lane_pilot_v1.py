from __future__ import annotations

import unittest
import os
from dataclasses import replace
from pathlib import Path

from o2o_dps.contra260817_fury_full_policy_rollout_v4 import Contra260817SimulatorInputsV4
from o2o_dps.shared_press_four_lane_pilot_v1 import (
    SharedPressObservableContextV1,
    _row,
    run_shared_press_four_lane_pilot_v1,
)
from o2o_dps.development_raid_b_wave_v1 import build_dual_wield_raid_b_wave_v1
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.deployed_contra_runtime_binding_v1 import load_deployed_contra_runtime_binding_v1
from o2o_dps.development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
NATIVE_BRIDGE = PROJECT_ROOT / "bin/o2obridge.press-v19.exe"
RUNTIME_BINDING = PROJECT_ROOT / ".hpc-local/smokes/cat-gap-three-baseline-v1/deployed-contra-runtime-binding-v1.951b8faa.json"


def _complete_artifact() -> dict:
    return {
        "status": "TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING",
        "mode": "DYNAMIC_V3_WHOLE_WAVE",
        "seed": 41,
        "period_ms": 100,
        "press_phase_ms": 0,
        "press_clock_configured_at_ms": 0,
        "first_scheduled_press_ms": 0,
        "press_clock_configuration_mode": "SEPARATE_AFTER_DYNAMIC_LOAD",
        "press_count": 2,
        "presses": [
            {"press_index": 1, "time_ms": 0, "source_invocation_count": 1,
             "finish_press_ready": False, "press_closure": "FINISH_PRESS"},
            {"press_index": 2, "time_ms": 100, "source_invocation_count": 1,
             "finish_press_ready": False, "press_closure": "FINISH_PRESS"},
        ],
        "terminal": {"kind": "MODEL_TARGET_DEFEATED",
                     "required_hostiles_defeated": True, "reason": None},
        "final_state": {"time_ms": 200, "dynamic_team_background": {
            "simulated_damage_applied": 4200.0,
        }},
    }


class SharedPressFourLanePilotV1Tests(unittest.TestCase):
    @unittest.skipUnless(
        os.name == "nt" and NATIVE_BRIDGE.is_file() and RUNTIME_BINDING.is_file(),
        "Windows v19 bridge and captured Contra runtime binding required",
    )
    def test_native_dual_target_four_lanes_use_same_grid(self) -> None:
        case, _ = build_dual_wield_raid_b_wave_v1(2026091401)
        binding = load_deployed_contra_runtime_binding_v1(RUNTIME_BINDING)
        panel = run_shared_press_four_lane_pilot_v1(
            case,
            lambda: V14ProjectedDynamicV3Bridge(
                NATIVE_BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
            ),
            runtime_binding=binding, period_ms=100, max_presses=400,
        )
        self.assertEqual("FOUR_LANE_COMPLETE_NONVOTING", panel["status"])
        self.assertFalse(panel["comparison_ready"])
        self.assertEqual(4, len(panel["rows"]))
        self.assertTrue(all(row["clock_receipts_valid"] for row in panel["rows"]))
        self.assertTrue(all(
            row["press_clock_configuration_mode"]
            == "ATOMIC_DYNAMIC_V3_PRESS_CLOCK"
            for row in panel["rows"]
        ))
        self.assertTrue(all(row["model_wave_complete"] for row in panel["rows"]))
        self.assertTrue(panel["shared_context_receipts_match"])
        self.assertTrue(all(
            row["shared_context_receipt_matches"] for row in panel["rows"]
        ))
        by_lane = {row["lane"]: row for row in panel["rows"]}
        self.assertEqual(
            by_lane["cat"]["own_effective_damage"],
            by_lane["candidate"]["own_effective_damage"],
        )

    @unittest.skipUnless(
        os.name == "nt" and NATIVE_BRIDGE.is_file() and RUNTIME_BINDING.is_file(),
        "Windows v19 bridge and captured Contra runtime binding required",
    )
    def test_native_initial_no_target_ticks_close_in_all_four_lanes(self) -> None:
        seed = 2026091410
        case = build_development_wave_case_v1(seed)
        old = case.dynamic_load.config
        config = DynamicTargetSemanticsConfigV3(
            target_health=old.target_health,
            idle_advance_horizon_ms=old.idle_advance_horizon_ms,
            background_damage_events=old.background_damage_events,
            attackability_events=(
                DynamicAttackabilityEventV2(0, 0, 0, False),
                DynamicAttackabilityEventV2(1, 200, 0, True),
            ),
            effective_armor_events=old.effective_armor_events,
        )
        case = replace(
            case,
            dynamic_load=DynamicRolloutLoadV3.bind(case.request, seed, config),
        )
        binding = load_deployed_contra_runtime_binding_v1(RUNTIME_BINDING)
        panel = run_shared_press_four_lane_pilot_v1(
            case,
            lambda: V14ProjectedDynamicV3Bridge(
                NATIVE_BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
            ),
            runtime_binding=binding, period_ms=100, max_presses=3,
        )
        self.assertFalse(panel["comparison_ready"])
        self.assertEqual(4, len(panel["artifacts"]))
        for artifact in panel["artifacts"].values():
            self.assertEqual(
                "ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
                artifact["press_clock_configuration_mode"],
            )
            self.assertEqual([0, 100], [
                row["time_ms"] for row in artifact["presses"][:2]
            ])
            self.assertEqual([0, 0], [
                row["source_invocation_count"] for row in artifact["presses"][:2]
            ])
            self.assertTrue(all(
                row["policy_disposition"] == "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
                for row in artifact["presses"][:2]
            ))

    def test_complete_lane_has_model_score_only_with_bound_clock(self) -> None:
        row = _row("cat", _complete_artifact(), seed=41, period_ms=100)
        self.assertTrue(row["model_wave_complete"])
        self.assertEqual(4200.0, row["own_effective_damage"])

    def test_wrong_or_extra_press_keeps_score_null(self) -> None:
        for change in (
            lambda artifact: artifact["presses"][1].update(time_ms=150),
            lambda artifact: artifact["presses"][0].update(source_invocation_count=2),
            lambda artifact: artifact.update(seed=42),
            lambda artifact: artifact["presses"][0].update(finish_press_ready=True),
        ):
            artifact = _complete_artifact()
            change(artifact)
            row = _row("cat", artifact, seed=41, period_ms=100)
            self.assertFalse(row["model_wave_complete"])
            self.assertIsNone(row["own_effective_damage"])

    def test_nonzero_phase_and_nonzero_start_use_reported_grid(self) -> None:
        artifact = _complete_artifact()
        artifact.update(
            press_phase_ms=37,
            press_clock_configured_at_ms=12,
            first_scheduled_press_ms=37,
            press_clock_configuration_mode="ATOMIC_DYNAMIC_V3_PRESS_CLOCK",
        )
        artifact["presses"][0]["time_ms"] = 37
        artifact["presses"][1]["time_ms"] = 137
        row = _row("cat", artifact, seed=41, period_ms=100, phase_ms=37)
        self.assertTrue(row["clock_receipts_valid"])
        self.assertTrue(row["model_wave_complete"])

        artifact["first_scheduled_press_ms"] = 0
        self.assertFalse(
            _row("cat", artifact, seed=41, period_ms=100, phase_ms=37)[
                "clock_receipts_valid"
            ]
        )

    def test_context_mismatch_keeps_an_otherwise_complete_score_null(self) -> None:
        artifact = _complete_artifact()
        artifact["shared_context_receipt"] = {"schema": "wrong"}
        row = _row(
            "cat", artifact, seed=41, period_ms=100,
            expected_context_receipt={"schema": "expected"},
        )
        self.assertTrue(row["model_wave_complete"])
        self.assertFalse(row["shared_context_receipt_matches"])
        self.assertIsNone(row["own_effective_damage"])

    def test_lane_projection_mismatch_keeps_score_null(self) -> None:
        artifact = _complete_artifact()
        expected = {
            "lane_projections": {"cat_and_candidate": {"input": "expected"}},
        }
        artifact["shared_context_receipt"] = expected
        artifact["simulator_input_receipt"] = {"input": "different"}
        row = _row(
            "cat", artifact, seed=41, period_ms=100,
            expected_context_receipt=expected,
        )
        self.assertTrue(row["model_wave_complete"])
        self.assertFalse(row["shared_context_receipt_matches"])
        self.assertIsNone(row["own_effective_damage"])

    def test_context_rejects_cross_lane_common_field_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "initial_autoattack_active"):
            SharedPressObservableContextV1(
                contra_new_inputs=Contra260817SimulatorInputsV4(),
            )

    def test_terminal_sink_may_close_final_key_without_finish_command(self) -> None:
        artifact = _complete_artifact()
        artifact["final_state"]["finished"] = True
        artifact["presses"][-1].update(
            press_closure="SKIPPED_MODEL_TERMINAL", finish_press_ready=None,
        )
        self.assertTrue(_row("cat", artifact, seed=41, period_ms=100)["model_wave_complete"])
        artifact["presses"][0].update(
            press_closure="SKIPPED_MODEL_TERMINAL", finish_press_ready=None,
        )
        self.assertFalse(_row("cat", artifact, seed=41, period_ms=100)["model_wave_complete"])

    def test_incomplete_lane_does_not_turn_into_zero_damage(self) -> None:
        artifact = _complete_artifact()
        artifact["status"] = "UNSUPPORTED_PRESS_LANE_NONVOTING"
        row = _row("contra_new", artifact, seed=41, period_ms=100)
        self.assertFalse(row["model_wave_complete"])
        self.assertIsNone(row["own_effective_damage"])


if __name__ == "__main__":
    unittest.main()
