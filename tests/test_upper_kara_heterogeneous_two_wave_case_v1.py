from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.development_wave_stratified_v1 import (
    build_stratified_wave_case_v1,
)
from o2o_dps.development_wave_panel_v1 import WORKSPACE_ROOT
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_heterogeneous_two_wave_case_v1 import (
    COMPLETE_STATUS,
    INCOMPLETE_STATUS,
    SEEN_OUT_OF_RANGE,
    UNSEEN_UNTIL_ARRIVAL,
    UpperKaraHeterogeneousTwoWaveCaseV1Error,
    build_heterogeneous_two_wave_observation_projector_v1,
    build_upper_kara_heterogeneous_two_wave_case_v1,
    visible_simulator_target_indices_v1,
)


def _raw_state(case, *, hidden_hp: float) -> dict[str, object]:
    hp = [row.health for row in case.dynamic_load.config.target_health]
    hp[2] = hidden_hp
    armor = case.case_spec["initial_state"]["target_base_armor"]
    life = [
        {
            "target_index": index,
            "initial_health": value,
            "current_health": value,
            "dead": False,
            "simulated_damage_applied": 0.0,
            "background_damage_applied": 0.0,
        }
        for index, value in enumerate(hp)
    ]
    semantics = [
        {
            "target_index": index,
            "attackable": index < 2,
            "effective_armor": armor[index],
            "maximum_health": value,
            "current_health": value,
            "dead": False,
        }
        for index, value in enumerate(hp)
    ]
    return {
        "time_ms": 0,
        "target_index": 2,
        "target_health_known": True,
        "target_health": hidden_hp,
        "target_health_max": hidden_hp,
        "target_health_percent": 100.0,
        "environment_generation": 1,
        "dynamic_team_background": {
            "environment_generation": 1,
            "targets": life,
        },
        "dynamic_target_semantics": {
            "environment_generation": 1,
            "targets": semantics,
        },
    }


class UpperKaraHeterogeneousTwoWaveCaseV1Tests(unittest.TestCase):
    def test_frozen_multi_then_long_single_share_one_native_environment(self):
        built = build_upper_kara_heterogeneous_two_wave_case_v1(101)
        self.assertEqual(COMPLETE_STATUS, built.status)
        case = built.require_case()
        first, _ = build_stratified_wave_case_v1(101, "multi_two")
        second, _ = build_stratified_wave_case_v1(101, "single_long")
        first_horizon = first.dynamic_load.config.idle_advance_horizon_ms

        self.assertEqual(3, len(case.request["encounter"]["targets"]))
        self.assertEqual(
            [row.health for row in first.dynamic_load.config.target_health]
            + [row.health for row in second.dynamic_load.config.target_health],
            [row.health for row in case.dynamic_load.config.target_health],
        )
        self.assertEqual(
            first_horizon + second.dynamic_load.config.idle_advance_horizon_ms,
            case.dynamic_load.config.idle_advance_horizon_ms,
        )
        self.assertEqual(
            first.case_spec["initial_state"]["target_base_armor"]
            + second.case_spec["initial_state"]["target_base_armor"],
            case.case_spec["initial_state"]["target_base_armor"],
        )

        attacks = [
            (row.time_ms, row.target_index, row.attackable)
            for row in case.dynamic_load.config.attackability_events
        ]
        self.assertEqual(
            [
                (0, 0, True),
                (0, 1, True),
                (0, 2, False),
                (first_horizon, 2, True),
            ],
            attacks,
        )
        second_source = second.dynamic_load.config.background_damage_events[0]
        second_bound = next(
            row
            for row in case.dynamic_load.config.background_damage_events
            if row.event_id
            == f"hetero-w2:{second_source.event_id}"
        )
        self.assertEqual(first_horizon + second_source.time_ms, second_bound.time_ms)
        self.assertEqual(2 + second_source.target_index, second_bound.target_index)
        self.assertEqual(second_source.damage, second_bound.damage)
        route = case.case_spec["continuous_route"]
        self.assertFalse(route["independent_wave_reset"])
        self.assertEqual(
            {
                "rage",
                "cooldowns",
                "auras",
                "consumable_inventory",
                "equipment",
                "proc_state",
            },
            set(route["state_carried"]),
        )

    def test_delayed_unseen_arrival_hides_hp_and_preserves_team_damage_budget(self):
        arrival = 7_000
        built = build_upper_kara_heterogeneous_two_wave_case_v1(
            103,
            first_wave_arrival_ms=arrival,
            first_wave_visibility=UNSEEN_UNTIL_ARRIVAL,
        )
        case = built.require_case()
        first, _ = build_stratified_wave_case_v1(103, "multi_two")
        first_horizon = first.dynamic_load.config.idle_advance_horizon_ms

        introductions = built.target_introduction_registry.to_wire()
        self.assertEqual(
            [arrival, arrival, first_horizon],
            [row["introduced_at_ms"] for row in introductions["targets"]],
        )
        self.assertEqual((), visible_simulator_target_indices_v1(case, arrival - 1))
        self.assertEqual(
            (0, 1), visible_simulator_target_indices_v1(case, arrival)
        )
        self.assertEqual(
            (0, 1, 2),
            visible_simulator_target_indices_v1(case, first_horizon),
        )
        self.assertEqual(
            {"simulator_target_index", "introduced_at_ms"},
            set(introductions["targets"][0]),
        )
        self.assertNotIn("model_max_hp", introductions["targets"][0])
        self.assertFalse(
            case.case_spec["policy_observation"][
                "environment_registry_passed_to_policy"
            ]
        )
        self.assertTrue(
            any(
                "model_max_hp" in target
                for wave in built.environment_registry["waves"]
                for target in wave["targets"]
            )
        )

        attacks = [
            (row.time_ms, row.target_index, row.attackable)
            for row in case.dynamic_load.config.attackability_events
        ]
        self.assertEqual(
            [
                (0, 0, False),
                (0, 1, False),
                (0, 2, False),
                (arrival, 0, True),
                (arrival, 1, True),
                (first_horizon, 2, True),
            ],
            attacks,
        )
        for target_index in (0, 1):
            expected = sum(
                row.damage
                for row in first.dynamic_load.config.background_damage_events
                if row.target_index == target_index and row.time_ms < arrival
            )
            catchup = next(
                row
                for row in case.dynamic_load.config.background_damage_events
                if row.event_id
                == f"hetero-w1:unseen-team-catchup-t{target_index}"
            )
            self.assertEqual(arrival, catchup.time_ms)
            self.assertAlmostEqual(expected, catchup.damage, places=9)
            self.assertFalse(
                any(
                    row.target_index == target_index
                    and 0 < row.time_ms < arrival
                    for row in case.dynamic_load.config.background_damage_events
                )
            )

    def test_hidden_second_wave_source_local_target_zero_cannot_leak_hp(self):
        case = build_upper_kara_heterogeneous_two_wave_case_v1(107).require_case()
        left_projector = build_heterogeneous_two_wave_observation_projector_v1(
            case
        )
        right_projector = build_heterogeneous_two_wave_observation_projector_v1(
            case
        )
        left = left_projector(_raw_state(case, hidden_hp=499_465.0))
        right = right_projector(_raw_state(case, hidden_hp=9_999_999.0))

        self.assertEqual(left.state, right.state)
        self.assertEqual((0, 1), left.policy_to_simulator_target_index)
        self.assertEqual((0, 1), left_projector.captured_target_indexes)
        self.assertEqual(1, left_projector.hidden_selected_target_substitutions)
        second_wave_target = case.case_spec["environment_registry"]["waves"][1][
            "targets"
        ][0]
        self.assertEqual(0, second_wave_target["source_local_target_index"])
        self.assertEqual(2, second_wave_target["simulator_target_index"])

    @unittest.skipUnless(
        DEFAULT_EXACT_BRIDGE.is_file(),
        "the pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_native_delayed_load_reaches_first_introduction_with_prefix_hp(self):
        arrival = 7_000
        case = build_upper_kara_heterogeneous_two_wave_case_v1(
            117, first_wave_arrival_ms=arrival
        ).require_case()
        with SimulatorBridgeDynamicV3(
            DEFAULT_EXACT_BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle"
        ) as bridge:
            loaded = bridge.load_dynamic_v3(
                case.request, case.dynamic_load.seed, case.dynamic_load.config
            )
        self.assertEqual(arrival, loaded.state["time_ms"])
        self.assertTrue(loaded.state["needs_input"])
        self.assertEqual(
            [True, True, False],
            [
                row["attackable"]
                for row in loaded.state["dynamic_target_semantics"]["targets"]
            ],
        )
        projected = build_heterogeneous_two_wave_observation_projector_v1(case)(
            loaded.state, ()
        )
        self.assertEqual((0, 1), projected.policy_to_simulator_target_index)
        self.assertLess(
            projected.state["target_health"],
            case.dynamic_load.config.target_health[0].health,
        )

    def test_visible_out_of_range_branch_fails_closed_when_delayed(self):
        built = build_upper_kara_heterogeneous_two_wave_case_v1(
            109,
            first_wave_arrival_ms=7_000,
            first_wave_visibility=SEEN_OUT_OF_RANGE,
        )
        self.assertEqual(INCOMPLETE_STATUS, built.status)
        self.assertFalse(built.complete)
        self.assertIn("GLOBAL_TARGET_ATTACKABILITY", built.blockers[0])
        with self.assertRaises(UpperKaraHeterogeneousTwoWaveCaseV1Error):
            built.require_case()

    def test_arrival_must_precede_the_frozen_first_wave_horizon(self):
        with self.assertRaisesRegex(ValueError, "must precede"):
            build_upper_kara_heterogeneous_two_wave_case_v1(
                113,
                first_wave_arrival_ms=15_000,
            )


if __name__ == "__main__":
    unittest.main()
