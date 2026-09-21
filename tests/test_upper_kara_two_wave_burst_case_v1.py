from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.contra_turtle_burst_loadout_v1 import RAPID_GROWTH_ACTION
from o2o_dps.development_two_wave_build_panel_v1 import (
    BUILD_IDS,
    SECOND_WAVE_UNLOCK_MS,
    TEAM_DAMAGE_PER_HIT,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
    search_cell_from_continuous_two_wave_case_v1,
)


class UpperKaraTwoWaveBurstCaseV1Tests(unittest.TestCase):
    def test_two_waves_and_burst_share_one_shifted_native_environment(self):
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            101,
            build_id=BUILD_IDS[0],
            loadout_id="contra_turtle_burst__quickness",
        )
        config = case.dynamic_load.config
        self.assertEqual(2, len(config.target_health))
        unlock = next(
            row.time_ms
            for row in config.attackability_events
            if row.target_index == 1 and row.attackable
        )
        self.assertEqual(SECOND_WAVE_UNLOCK_MS + 3_000, unlock)
        self.assertIn(RAPID_GROWTH_ACTION, case.precombat.self_actions)
        player = case.request["raid"]["parties"][0]["players"][0]
        self.assertEqual("QuicknessPotion", player["consumes"]["defaultPotion"])
        self.assertTrue(
            player["consumes"]["miscConsumes"]["elixirOfRapidGrowth"]
        )
        self.assertFalse(
            case.case_spec["continuous_route"]["independent_wave_reset"]
        )
        self.assertEqual(
            "NATIVE_SAME_ENVIRONMENT",
            case.case_spec["two_wave_model"]["resource_cooldown_aura_carry"],
        )

    def test_identity_is_seed_stable_but_loadout_and_build_specific(self):
        def identity(seed, build_id, loadout_id):
            return search_cell_from_continuous_two_wave_case_v1(
                build_upper_kara_continuous_two_wave_burst_case_v1(
                    seed,
                    build_id=build_id,
                    loadout_id=loadout_id,
                )
            )

        first = identity(101, BUILD_IDS[0], "contra_turtle_burst__no_potion")
        second = identity(103, BUILD_IDS[0], "contra_turtle_burst__no_potion")
        quickness = identity(
            101, BUILD_IDS[0], "contra_turtle_burst__quickness"
        )
        other_build = identity(
            101, BUILD_IDS[1], "contra_turtle_burst__no_potion"
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first.cell_key(), quickness.cell_key())
        self.assertNotEqual(first.cell_key(), other_build.cell_key())

    def test_late_arrival_is_a_distinct_continuous_native_branch(self):
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            113,
            build_id=BUILD_IDS[0],
            loadout_id="contra_turtle_burst__quickness",
            first_wave_arrival_ms=7_000,
        )
        events = [
            event.to_wire()
            for event in case.dynamic_load.config.attackability_events
        ]
        self.assertFalse(
            any(
                row["time_ms"] == 3_000
                and row["target_index"] == 0
                and row["attackable"] is True
                for row in events
            )
        )
        self.assertTrue(
            any(
                row["time_ms"] == 10_000
                and row["target_index"] == 0
                and row["attackable"] is True
                for row in events
            )
        )
        arrival = case.case_spec["continuous_route"]["player_arrival"]
        self.assertEqual(7_000, arrival["first_wave_in_range_at_ms_relative_to_pull"])
        self.assertTrue(arrival["team_damage_continues_before_player_arrival"])
        self.assertFalse(arrival["future_target_death_time_visible_to_policy"])
        first_wave_team_catchup = [
            event
            for event in case.dynamic_load.config.background_damage_events
            if event.target_index == 0
            and event.time_ms == 10_000
            and event.event_id
            == "model-wave-1-team-catchup-at-player-arrival"
        ]
        self.assertEqual(1, len(first_wave_team_catchup))
        self.assertEqual(
            14 * TEAM_DAMAGE_PER_HIT,
            first_wave_team_catchup[0].damage,
        )

        on_time = search_cell_from_continuous_two_wave_case_v1(
            build_upper_kara_continuous_two_wave_burst_case_v1(
                113,
                build_id=BUILD_IDS[0],
                loadout_id="contra_turtle_burst__quickness",
            )
        )
        late = search_cell_from_continuous_two_wave_case_v1(case)
        self.assertNotEqual(on_time.cell_key(), late.cell_key())

    def test_arrival_must_precede_the_modeled_first_wave_kill_deadline(self):
        with self.assertRaisesRegex(ValueError, "first_wave_arrival_ms"):
            build_upper_kara_continuous_two_wave_burst_case_v1(
                113,
                build_id=BUILD_IDS[0],
                loadout_id="contra_turtle_burst__quickness",
                first_wave_arrival_ms=10_000,
            )


if __name__ == "__main__":
    unittest.main()
