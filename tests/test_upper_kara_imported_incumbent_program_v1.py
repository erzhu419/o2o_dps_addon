from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.contra_turtle_burst_loadout_v1 import RAPID_GROWTH_ACTION
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_paired_multiseed_runner_v2 import derive_simulator_seed
from o2o_dps.precombat_timeline_v1 import SimulatorBridgePrecombatV1
from o2o_dps.upper_kara_burst_package_search_v1 import BURST_LOADOUT_IDS_V1
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
    build_upper_kara_causal_program_candidate_set_v1,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    build_continuous_two_wave_observation_projector_v1,
    build_imported_incumbent_bindings_v1,
    build_imported_incumbent_program_replay_factory_v1,
    deployed_controller_for_build_v1,
)
from o2o_dps.upper_kara_two_wave_baseline_panel_v1 import (
    baseline_policy_ids_for_build_v1,
    run_upper_kara_continuous_two_wave_baselines_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    imported_incumbent_programs_v1,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1


BRIDGE_AVAILABLE = DEFAULT_EXACT_BRIDGE.is_file()
BRIDGE_CWD = PROJECT_ROOT.parent / "wowsims-turtle"


class ImportedIncumbentBindingV1Tests(unittest.TestCase):
    def test_binding_identity_and_deployed_weapon_mode_are_exact(self) -> None:
        expected_controller = {
            "live_bonereaver": "raid_a",
            "clean_dual_weapon_probe": "raid_b",
        }
        loadout_id = BURST_LOADOUT_IDS_V1[0]
        for build_id, controller in expected_controller.items():
            with self.subTest(build_id=build_id):
                case = build_upper_kara_continuous_two_wave_burst_case_v1(
                    17,
                    build_id=build_id,
                    loadout_id=loadout_id,
                    first_wave_arrival_ms=0,
                )
                bindings = build_imported_incumbent_bindings_v1(build_id, case)
                self.assertEqual(controller, deployed_controller_for_build_v1(build_id))
                self.assertEqual(
                    baseline_policy_ids_for_build_v1(build_id),
                    tuple(binding.binding_id for binding in bindings),
                )
                for binding in bindings:
                    self.assertIsNot(
                        binding.resolver_factory(), binding.resolver_factory()
                    )


@unittest.skipUnless(
    BRIDGE_AVAILABLE,
    "the exact precombat-late-arrival native bridge is not built",
)
class ImportedIncumbentNativeV1Tests(unittest.TestCase):
    def test_searched_replay_does_not_load_imported_bindings(self) -> None:
        build_id = "live_bonereaver"
        loadout_id = BURST_LOADOUT_IDS_V1[0]
        seed = 109
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            seed,
            build_id=build_id,
            loadout_id=loadout_id,
            first_wave_arrival_ms=0,
        )
        program = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=loadout_id,
            train_cases=(case,),
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=1),
        ).programs[0]
        replay = build_imported_incumbent_program_replay_factory_v1(build_id)(
            loadout_id, {seed: case}
        )

        with patch(
            "o2o_dps.upper_kara_imported_incumbent_program_v1."
            "build_imported_incumbent_bindings_v1",
            side_effect=AssertionError("searched replay loaded imported bindings"),
        ):
            outcome = replay.replay(seed, program, max_decisions=512)

        self.assertIs(ReplayStatusV1.COMPLETE, outcome.status)

    def test_hidden_future_target_hp_cannot_change_time_zero_observation(self) -> None:
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            113,
            build_id="live_bonereaver",
            loadout_id="contra_turtle_burst__quickness",
            first_wave_arrival_ms=7_000,
        )
        bridge = SimulatorBridgePrecombatV1(DEFAULT_EXACT_BRIDGE, cwd=BRIDGE_CWD)
        try:
            loaded = bridge.load_dynamic_v3_precombat(
                case.request,
                case.dynamic_load.seed,
                case.dynamic_load.config,
                case.precombat,
            )
            left = deepcopy(loaded.state)
        finally:
            bridge.close()
        self.assertEqual(1, left["target_index"])
        self.assertFalse(
            left["dynamic_target_semantics"]["targets"][1]["attackable"]
        )
        right = deepcopy(left)
        right_life = right["dynamic_team_background"]["targets"][1]
        right_semantic = right["dynamic_target_semantics"]["targets"][1]
        right_life["initial_health"] = 9_999_999.0
        right_life["current_health"] = 9_999_999.0
        right_semantic["current_health"] = 9_999_999.0
        right["target_health"] = 9_999_999.0
        right["target_health_max"] = 9_999_999.0

        left_projector = build_continuous_two_wave_observation_projector_v1(case)
        right_projector = build_continuous_two_wave_observation_projector_v1(case)
        left_view = left_projector(left, ())
        right_view = right_projector(right, ())

        self.assertEqual(left_view.state, right_view.state)
        self.assertEqual((0,), left_view.policy_to_simulator_target_index)
        self.assertEqual(0, left_view.state["target_index"])
        self.assertEqual(1, left_projector.hidden_selected_target_substitutions)
        self.assertEqual(1, left_projector.last_hidden_selected_target_index)

    def test_integrated_imports_match_standalone_native_damage_and_deaths(self) -> None:
        master_seed = 113
        loadout_id = BURST_LOADOUT_IDS_V1[0]
        for build_id, arrival_ms in (
            ("live_bonereaver", 7_000),
            ("clean_dual_weapon_probe", 0),
        ):
            with self.subTest(build_id=build_id, arrival_ms=arrival_ms):
                case = build_upper_kara_continuous_two_wave_burst_case_v1(
                    master_seed,
                    build_id=build_id,
                    loadout_id=loadout_id,
                    first_wave_arrival_ms=arrival_ms,
                )
                simulator_seed = derive_simulator_seed(
                    master_seed,
                    case.dynamic_load.request_sha256,
                    namespace=PROTOCOL_ID,
                )
                baseline = run_upper_kara_continuous_two_wave_baselines_v1(
                    master_seed,
                    build_id=build_id,
                    loadout_id=loadout_id,
                    first_wave_arrival_ms=arrival_ms,
                )
                self.assertEqual(simulator_seed, baseline["simulator_seed"])
                replay = build_imported_incumbent_program_replay_factory_v1(
                    build_id
                )(loadout_id, {simulator_seed: case})
                programs = {
                    program.selector.source_policy_id: program
                    for program in imported_incumbent_programs_v1(build_id)
                }
                for native_row in baseline["rows"]:
                    policy_id = native_row["policy_id"]
                    outcome = replay.replay(
                        simulator_seed, programs[policy_id], max_decisions=512
                    )
                    self.assertIs(ReplayStatusV1.COMPLETE, outcome.status)
                    self.assertAlmostEqual(
                        native_row["own_effective_damage"],
                        outcome.effective_damage,
                        places=9,
                    )
                    self.assertEqual(
                        native_row["ttk_ms"],
                        outcome.elapsed_ms
                        - min(
                            row.time_ms
                            for row in case.dynamic_load.config.attackability_events
                            if row.target_index == 0 and row.attackable
                        ),
                    )
                    projected_targets = outcome.state[
                        "dynamic_team_background"
                    ]["targets"]
                    self.assertEqual(
                        tuple(
                            (row["target_index"], row["dead"], row["death_time_ms"])
                            for row in native_row["target_outcomes"]
                        ),
                        tuple(
                            (row["target_index"], row["dead"], row["death_time_ms"])
                            for row in projected_targets
                        ),
                    )
                    self.assertEqual(1, outcome.state["target_index"])

    def test_generated_program_reschedules_rapid_growth_to_second_wave(self) -> None:
        build_id = "live_bonereaver"
        loadout_id = "contra_turtle_burst__quickness"
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            113,
            build_id=build_id,
            loadout_id=loadout_id,
            first_wave_arrival_ms=7_000,
        )
        candidate_set = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=loadout_id,
            train_cases=(case,),
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=512),
        )

        semantic_matches = []
        for program in candidate_set.programs:
            rapid = tuple(
                row
                for row in program.selector.alternatives
                if row.guard.action_ready == RAPID_GROWTH_ACTION
            )
            if len(rapid) == 2 and {
                (row.guard.target_index, row.guard.target_hp_pct_gte)
                for row in rapid
            } == {(0, 35.0), (1, 35.0)}:
                semantic_matches.append(program)
        self.assertTrue(semantic_matches)
        program = min(semantic_matches, key=lambda row: row.program_id)

        replay = build_imported_incumbent_program_replay_factory_v1(build_id)(
            loadout_id, {113: case}
        )
        outcome = replay.replay(113, program, max_decisions=512)

        self.assertIs(ReplayStatusV1.COMPLETE, outcome.status)
        self.assertEqual(25_000, outcome.elapsed_ms)
        rapid_guards = {
            (row["state_time_ms"], row["alternative_id"]): row
            for row in outcome.receipts
            if row.get("kind") == "ALTERNATIVE_GUARD"
            and "item56113" in row.get("alternative_id", "")
            and row.get("state_time_ms") in {10_000, 15_000}
        }
        first = rapid_guards[(10_000, "burst:t0:item56113t0:hp35")]
        self.assertFalse(first["matched"])
        self.assertEqual(
            ["target_hp_pct_gte"], first["evaluation"]["failed_predicates"]
        )
        self.assertAlmostEqual(
            28.004620362825065,
            first["evaluation"]["observed"]["target_hp_pct"],
            places=9,
        )
        second = rapid_guards[(15_000, "burst:t1:item56113t0:hp35")]
        self.assertTrue(second["matched"])
        self.assertEqual(
            100.0, second["evaluation"]["observed"]["target_hp_pct"]
        )
        rapid_uses = [
            row
            for row in outcome.receipts
            if row.get("action") == RAPID_GROWTH_ACTION.to_wire()
        ]
        self.assertEqual(1, len(rapid_uses))
        self.assertEqual(15_000, rapid_uses[0]["state_time_ms"])
        self.assertEqual(
            {
                "simulator_seed": 113,
                "hidden_selected_target_substitutions": 12,
                "last_hidden_selected_target_index": 1,
            },
            replay.last_observation_audit,
        )


if __name__ == "__main__":
    unittest.main()
