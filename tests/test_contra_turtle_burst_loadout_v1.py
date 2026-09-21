from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.contra_turtle_burst_loadout_v1 import (
    GOBLIN_SAPPER_ACTION,
    JUJU_FLURRY_ACTION,
    MIGHTY_RAGE_ACTION,
    QUICKNESS_POTION_ACTION,
    RAPID_GROWTH_ACTION,
    build_contra_turtle_burst_loadouts_v1,
    build_upper_kara_contra_burst_loadout_case_v1,
)
from o2o_dps.sim_bridge import ActionRef


def _request(first: int, second: int) -> dict:
    items = [{} for _ in range(17)]
    items[12] = {"id": first}
    items[13] = {"id": second}
    return {
        "raid": {"parties": [{"players": [{"equipment": {"items": items}}]}]}
    }


class ContraTurtleBurstLoadoutV1Tests(unittest.TestCase):
    def test_variants_factor_shared_inventory_from_potion_choice(self):
        rows = build_contra_turtle_burst_loadouts_v1(_request(23041, 19406))
        self.assertEqual(
            [row.loadout_id for row in rows],
            [
                "contra_turtle_burst__no_potion",
                "contra_turtle_burst__mighty_rage",
                "contra_turtle_burst__rage",
                "contra_turtle_burst__quickness",
            ],
        )
        for row in rows:
            self.assertTrue(row.player_consumes["miscConsumes"]["jujuFlurry"])
            self.assertTrue(
                row.player_consumes["miscConsumes"]["elixirOfRapidGrowth"]
            )
            self.assertEqual(
                row.player_consumes["sapperExplosive"],
                "SapperGoblinSapper",
            )
            self.assertIn(JUJU_FLURRY_ACTION, row.precombat_self_actions)
            self.assertIn(RAPID_GROWTH_ACTION, row.precombat_self_actions)
            self.assertIn(ActionRef(item_id=23041), row.precombat_self_actions)
            self.assertNotIn(ActionRef(item_id=19406), row.precombat_self_actions)
            self.assertNotIn(GOBLIN_SAPPER_ACTION, row.precombat_self_actions)
        self.assertIn(MIGHTY_RAGE_ACTION, rows[1].precombat_self_actions)
        self.assertIn(QUICKNESS_POTION_ACTION, rows[3].precombat_self_actions)

    def test_unknown_equipped_trinket_is_not_invented_as_prepull_action(self):
        rows = build_contra_turtle_burst_loadouts_v1(_request(61194, 13965))
        for row in rows:
            self.assertNotIn(ActionRef(item_id=61194), row.precombat_self_actions)
            self.assertNotIn(ActionRef(item_id=13965), row.precombat_self_actions)
            self.assertEqual((), row.unsupported_source_action_ids)

    def test_exact_upper_kara_loadout_rebinds_request_and_precombat(self):
        case = build_upper_kara_contra_burst_loadout_case_v1(
            2026091425,
            representative_rank=11,
            stratum="q05",
            loadout_id="contra_turtle_burst__quickness",
        )
        player = case.request["raid"]["parties"][0]["players"][0]
        self.assertEqual(player["consumes"]["defaultPotion"], "QuicknessPotion")
        self.assertTrue(player["consumes"]["miscConsumes"]["jujuFlurry"])
        self.assertIn(QUICKNESS_POTION_ACTION, case.precombat.self_actions)
        self.assertIn(ActionRef(item_id=23041), case.precombat.self_actions)
        self.assertEqual(
            case.case_spec["request_sha256"],
            case.dynamic_load.request_sha256,
        )


if __name__ == "__main__":
    unittest.main()
