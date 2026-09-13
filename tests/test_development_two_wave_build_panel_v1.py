from __future__ import annotations

from pathlib import Path
import unittest

from o2o_dps.development_two_wave_build_panel_v1 import (
    BUILD_IDS,
    SECOND_WAVE_UNLOCK_MS,
    build_two_wave_build_case_v1,
    run_two_wave_build_panel_v1,
)
from o2o_dps.development_wave_panel_v1 import DEFAULT_BINDING, DEFAULT_BRIDGE, WORKSPACE_ROOT
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


class TwoWaveBuildPanelTests(unittest.TestCase):
    def test_model_is_two_targets_one_session_with_delayed_second_target(self) -> None:
        live, live_scenario = build_two_wave_build_case_v1(19, BUILD_IDS[0])
        dual, dual_scenario = build_two_wave_build_case_v1(19, BUILD_IDS[1])
        self.assertEqual(len(live.request["encounter"]["targets"]), 2)
        self.assertEqual(live.case_spec["required_target_indices"], [0, 1])
        self.assertEqual(live_scenario["stratum"], "multi_target")
        self.assertEqual(
            [(e.time_ms, e.target_index, e.attackable) for e in live.dynamic_load.config.attackability_events],
            [(0, 1, False), (SECOND_WAVE_UNLOCK_MS, 1, True)],
        )
        self.assertEqual(len(live.dynamic_load.config.background_damage_events), 40)
        self.assertEqual(live.case_spec["two_wave_model"]["source_wave_count"], 1)
        self.assertNotEqual(live.request["raid"]["parties"][0]["players"][0]["equipment"],
                            dual.request["raid"]["parties"][0]["players"][0]["equipment"])
        self.assertEqual(len(dual_scenario["target_context_bundle"]["contexts"]), 2)

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file() and DEFAULT_BINDING.is_file(), "native development bridge/binding absent")
    def test_native_two_wave_carries_bloodrage_cooldown_across_gap(self) -> None:
        case, _ = build_two_wave_build_case_v1(19, BUILD_IDS[0])
        with SimulatorBridgeDynamicV3(DEFAULT_BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            bridge.load_dynamic_v3(case.request, 19, case.dynamic_load.config)
            first = bridge.advance()
            self.assertFalse(first["dynamic_target_semantics"]["targets"][1]["attackable"])
            bloodrage = next(a for a in bridge.actions() if a.action.spell_id == 2687)
            self.assertTrue(bloodrage.legal)
            self.assertTrue(bridge.act(bloodrage.action).casted)
            bridge.wait(SECOND_WAVE_UNLOCK_MS)
            after_gap = bridge.advance()
            self.assertEqual(after_gap["time_ms"], SECOND_WAVE_UNLOCK_MS)
            self.assertTrue(after_gap["dynamic_target_semantics"]["targets"][1]["attackable"])
            self.assertTrue(after_gap["dynamic_team_background"]["targets"][0]["dead"])
            self.assertGreater(
                next(a.ready_in_ms for a in bridge.actions() if a.action.spell_id == 2687), 0,
            )

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file() and DEFAULT_BINDING.is_file(), "native development bridge/binding absent")
    def test_one_seed_two_build_two_wave_native_smoke(self) -> None:
        result = run_two_wave_build_panel_v1(master_seed=20260913)
        self.assertEqual(result["status"], "TWO_BUILD_TWO_WAVE_COMPLETE_DEVELOPMENT_ONLY")
        for build in result["builds"]:
            self.assertEqual(len(build["rows"]), 2)
            for row in build["rows"]:
                self.assertTrue(row["two_wave_timeline_valid"])
                self.assertGreaterEqual(row["inter_wave_gap_ms"], 2_000)
                self.assertEqual(len(row["target_outcomes"]), 2)
                own = sum(t["simulated_damage_applied"] for t in row["target_outcomes"])
                self.assertAlmostEqual(row["own_effective_damage"], own, places=6)


if __name__ == "__main__":
    unittest.main()
