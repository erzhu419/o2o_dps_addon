from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_fury_decision_windows import _registry_actions


class MechanicsRegistryTests(unittest.TestCase):
    @staticmethod
    def _mechanic(key: str) -> dict[str, object]:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return next(
            mechanic
            for mechanic in registry["mechanics"]
            if mechanic["key"] == key
        )

    def test_execute_records_wrapper_and_triggered_result_spell_ids(self) -> None:
        execute = self._mechanic("warrior.execute")

        implementation = execute["implementation"]
        self.assertEqual(implementation["spell_id"], 20662)
        self.assertEqual(implementation["wrapper_spell_id"], 20662)
        self.assertEqual(implementation["result_spell_id"], 20647)

    def test_bloodthirst_rank_four_uses_live_tooltip_damage_model(self) -> None:
        bloodthirst = self._mechanic("warrior.bloodthirst")

        self.assertEqual(
            bloodthirst["implementation"]["damage_model"],
            "rank 4: 200 base damage plus 0.35 times melee attack power",
        )
        parameter = bloodthirst["calibration"]["parameters"]["damage_model"]
        self.assertEqual(parameter["sample_count"], 8)
        self.assertEqual(
            parameter["source"]["comparison"],
            "LIVE_MATCHES_SIMULATOR_TWO_AP_STRATA",
        )
        self.assertFalse(
            bloodthirst["implementation"][
                "two_handed_weapon_specialization_applies"
            ]
        )

    def test_two_handed_specialization_uses_turtle_rank_three_tooltip(self) -> None:
        specialization = self._mechanic(
            "warrior.two_handed_weapon_specialization"
        )
        implementation = specialization["implementation"]
        self.assertEqual(
            implementation["current_character_weapon_damage_multiplier"],
            1.06,
        )
        self.assertEqual(
            implementation["current_character_weapon_skill_bonus"],
            3,
        )
        self.assertFalse(implementation["ap_based_bloodthirst_affected"])

    def test_phase5_records_sunder_cost_and_four_stratum_armor_response(self) -> None:
        sunder = self._mechanic("warrior.sunder_armor")
        implementation = sunder["implementation"]
        self.assertEqual(implementation["current_character_rage_cost"], 10)
        self.assertEqual(implementation["gcd_seconds"], 1.5)
        self.assertEqual(implementation["armor_reduction_per_stack"], 450)
        self.assertEqual(implementation["max_stacks"], 5)
        self.assertEqual(implementation["duration_seconds"], 30)

        bloodthirst = self._mechanic("warrior.bloodthirst")
        response = bloodthirst["calibration"]["parameters"]["armor_response"]
        self.assertEqual(response["sample_count"], 16)
        self.assertEqual(
            response["source"]["comparison"],
            "LIVE_MATCHES_SIMULATOR_FOUR_ARMOR_STRATA",
        )
        self.assertTrue(
            response["simulator_value"][
                "all_live_values_in_prediction_floor_ceil_sets"
            ]
        )

    def test_core_utility_actions_map_to_their_simulator_lanes(self) -> None:
        actions, _ = _registry_actions(REGISTRY_PATH)
        expected = {
            25289: ("gcd", "warrior.battle_shout"),
            2687: ("off_gcd", "warrior.bloodrage"),
            12328: ("gcd", "warrior.death_wish"),
            2457: ("off_gcd", "warrior.battle_stance"),
            71: ("off_gcd", "warrior.defensive_stance"),
            2458: ("off_gcd", "warrior.berserker_stance"),
        }

        for spell_id, (lane, key) in expected.items():
            with self.subTest(spell_id=spell_id):
                self.assertEqual(actions[spell_id]["lane"], lane)
                self.assertEqual(actions[spell_id]["policy_action_key"], key)

        for key in (
            "warrior.battle_stance",
            "warrior.defensive_stance",
            "warrior.berserker_stance",
        ):
            implementation = self._mechanic(key)["implementation"]
            self.assertFalse(implementation["consumes_gcd"])
            self.assertEqual(implementation["cooldown_seconds"], 1)
            self.assertEqual(
                implementation["shared_cooldown_group"], "warrior.stances"
            )


if __name__ == "__main__":
    unittest.main()
