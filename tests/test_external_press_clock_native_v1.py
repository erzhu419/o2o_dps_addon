"""Small native Windows smoke for the opt-in dynamic-v3 press clock."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import unittest

from o2o_dps.cat_external_press_pilot_v1 import run_cat_external_press_pilot_v1
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3
from o2o_dps.development_wave_team_retarget_v1 import (
    SourceFittedTeamRetargetBridgeV1,
    V14ProjectedDynamicV3Bridge,
    build_retarget_wave_case_v1,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v18.exe"
ATOMIC_BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v19.exe"


@unittest.skipUnless(os.name == "nt" and BRIDGE.is_file(), "native v18 bridge required")
class NativeExternalPressClockTests(unittest.TestCase):
    @unittest.skipUnless(ATOMIC_BRIDGE.is_file(), "native v19 bridge required")
    def test_v19_cat_lane_records_initial_no_target_ticks(self) -> None:
        seed = 2026091409
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
        with V14ProjectedDynamicV3Bridge(
            ATOMIC_BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
        ) as bridge:
            result = run_cat_external_press_pilot_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100, policy_kind="cat",
                dynamic_load=case.dynamic_load, max_presses=3,
            )
        self.assertEqual("ATOMIC_DYNAMIC_V3_PRESS_CLOCK", result["press_clock_configuration_mode"])
        self.assertEqual([0, 100, 200], [row["time_ms"] for row in result["presses"]])
        self.assertEqual([0, 0, 1], [row["source_invocation_count"] for row in result["presses"]])
        self.assertTrue(all(
            row["policy_disposition"] == "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
            for row in result["presses"][:2]
        ))

    def test_cat_zero_residual_complete_wave_is_identical(self) -> None:
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        results = []
        for kind in ("cat", "zero_residual"):
            with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
                results.append(run_cat_external_press_pilot_v1(
                    bridge, case.request, case.target_contexts,
                    seed=seed, period_ms=100, policy_kind=kind,
                    dynamic_load=case.dynamic_load, max_presses=200,
                ))
        for result in results:
            self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", result["status"])
            self.assertTrue(result["terminal"]["required_hostiles_defeated"])
            self.assertIsNone(result["terminal"]["reason"])
        self.assertEqual(results[0]["press_count"], results[1]["press_count"])
        self.assertEqual(
            [(row["time_ms"], row["proposal"], row["ordered_execution"])
             for row in results[0]["presses"]],
            [(row["time_ms"], row["proposal"], row["ordered_execution"])
             for row in results[1]["presses"]],
        )
        self.assertEqual(results[0]["final_state"], results[1]["final_state"])

    def test_cat_and_zero_residual_dynamic_wave_pilot_share_ticks(self) -> None:
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        results = []
        for kind in ("cat", "zero_residual"):
            with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
                result = run_cat_external_press_pilot_v1(
                    bridge, case.request, case.target_contexts,
                    seed=seed, period_ms=100, policy_kind=kind,
                    dynamic_load=case.dynamic_load, max_presses=3,
                )
            self.assertEqual("DYNAMIC_V3_WHOLE_WAVE", result["mode"])
            self.assertFalse(result["comparison_ready"])
            self.assertEqual("WATCHDOG_TRUNCATED_NONVOTING", result["status"])
            self.assertEqual(3, result["press_count"])
            self.assertEqual([0, 100, 200], [row["time_ms"] for row in result["presses"]])
            results.append(result)
        self.assertEqual(
            [row["proposal"] for row in results[0]["presses"]],
            [row["proposal"] for row in results[1]["presses"]],
        )

    def test_dynamic_wave_uses_only_external_key_grid(self) -> None:
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            loaded = bridge.load_dynamic_v3(case.request, seed, case.dynamic_load.config)
            self.assertFalse(loaded.state["finished"])
            configured = bridge.configure_press_clock(100, 0)
            self.assertFalse(configured["press_clock"]["ready"])
            for index, expected_time in enumerate((0, 100, 200, 300), start=1):
                state = bridge.advance()
                self.assertEqual(expected_time, state["time_ms"])
                self.assertTrue(state["needs_input"])
                self.assertEqual(index, state["press_clock"]["press_index"])
                self.assertTrue(state["press_clock"]["ready"])
                closed = bridge.finish_press()
                self.assertFalse(closed["needs_input"])
                self.assertFalse(closed["press_clock"]["ready"])

    def test_responsive_wakes_do_not_create_extra_keys(self) -> None:
        seed = 2026091401
        case, _, events = build_retarget_wave_case_v1(seed)
        identity = case.case_spec["team_background"]["source_schedule_content_sha256"]
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as native:
            bridge = SourceFittedTeamRetargetBridgeV1(native, events, identity)
            bridge.load_dynamic_v3(case.request, seed, case.dynamic_load.config)
            native.configure_press_clock(100, 0)
            for index in range(1, 7):
                state = bridge.advance()
                self.assertEqual((index - 1) * 100, state["time_ms"])
                self.assertEqual(index, state["press_clock"]["press_index"])
                self.assertTrue(state["press_clock"]["ready"])
                native.finish_press()
            self.assertEqual(2, len(bridge._receipts))
            self.assertEqual([500, 500], [row["time_ms"] for row in bridge._receipts])


if __name__ == "__main__":
    unittest.main()
