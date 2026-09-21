from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.simulator_cooldown_binding_v1 import (
    RAPID_GROWTH_ROUTE_EFFECT_V1,
    SimulatorCooldownResourceSpecV1,
    TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1,
    default_fury_burst_resource_specs_v1,
    exact_trinket_item_ids_from_request_v1,
    project_contra_fury_resource_specs_v1,
    project_contra_turtle_resource_specs_for_request_v1,
    resolve_simulator_cooldown_bindings_v1,
)


class SimulatorCooldownBindingV1Tests(unittest.TestCase):
    def test_exact_snapshot_duration_overrides_static_source_expectation(self):
        death_wish = ActionRef(spell_id=12328)
        result = resolve_simulator_cooldown_bindings_v1(
            (
                AvailableAction(
                    1,
                    death_wish,
                    "Death Wish",
                    True,
                    0,
                    True,
                    cooldown_duration_ms=90_000,
                ),
            ),
            resource_specs=(
                SimulatorCooldownResourceSpecV1(
                    death_wish,
                    "warrior.death_wish",
                    "warrior.death_wish",
                    "SPELL",
                    "test",
                ),
            ),
        )
        self.assertEqual(result.bindings[0].cooldown_ms, 90_000)
        self.assertTrue(result.to_dict()["contract"]["build_modified_cooldown_preserved"])

    def test_absent_and_zero_duration_actions_do_not_become_planner_resources(self):
        specs = default_fury_burst_resource_specs_v1()
        result = resolve_simulator_cooldown_bindings_v1(
            (
                AvailableAction(
                    0,
                    ActionRef(item_id=13442),
                    "Mighty Rage",
                    True,
                    0,
                    False,
                    cooldown_duration_ms=120_000,
                ),
                AvailableAction(
                    1,
                    ActionRef(spell_id=12328),
                    "Death Wish",
                    True,
                    0,
                    True,
                    cooldown_duration_ms=0,
                ),
            ),
            resource_specs=specs,
        )
        self.assertEqual(
            [row.resource_id for row in result.bindings],
            ["item.mighty_rage_potion"],
        )
        self.assertEqual(result.zero_duration_resource_ids, ("warrior.death_wish",))
        self.assertEqual(result.unavailable_resource_ids, ("warrior.recklessness",))

    def test_contra_inventory_projects_spells_and_keeps_unknown_items_explicit(self):
        projection = project_contra_fury_resource_specs_v1()
        resources = {row.resource_id for row in projection.resource_specs}
        self.assertIn("warrior.recklessness", resources)
        self.assertIn("warrior.death_wish", resources)
        self.assertIn("warrior.sweeping_strikes", resources)
        self.assertIn("item.mighty_rage_potion", resources)
        self.assertIn("trinket.slot_13", projection.unresolved_action_ids)
        self.assertIn("item.goblin_sapper_charge", projection.unresolved_action_ids)
        self.assertIn(
            "warrior.sweeping_strikes",
            projection.configured_not_emitted_action_ids,
        )
        self.assertFalse(
            projection.to_dict()["contract"]["contra_condition_enforced"]
        )

    def test_exact_trinket_and_named_item_ids_complete_source_identities(self):
        projection = project_contra_fury_resource_specs_v1(
            named_item_ids={"item.goblin_sapper_charge": 10646},
            trinket_item_ids={13: 23041, 14: 22954},
        )
        by_resource = {
            row.resource_id: row.action for row in projection.resource_specs
        }
        self.assertEqual(by_resource["trinket.slot_13"], ActionRef(item_id=23041))
        self.assertEqual(by_resource["trinket.slot_14"], ActionRef(item_id=22954))
        self.assertEqual(
            by_resource["item.goblin_sapper_charge"],
            ActionRef(item_id=10646),
        )

    def test_turtle_catalog_maps_juju_to_native_spell_action(self):
        self.assertEqual(
            TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1["item.juju_flurry"],
            ActionRef(spell_id=16322),
        )
        projection = project_contra_fury_resource_specs_v1(
            named_action_refs=TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1,
            include_mighty_rage_extension=False,
        )
        by_resource = {
            row.resource_id: row.action for row in projection.resource_specs
        }
        self.assertEqual(
            by_resource["item.mighty_rage_potion"],
            ActionRef(item_id=13442),
        )
        self.assertEqual(
            by_resource["item.juju_flurry"],
            ActionRef(spell_id=16322),
        )

    def test_rapid_growth_has_native_item_identity_and_own_cooldown_group(self):
        self.assertEqual(
            TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1["item.elixir_of_rapid_growth"],
            ActionRef(item_id=56113),
        )
        projection = project_contra_fury_resource_specs_v1(
            named_action_refs=TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1,
            include_mighty_rage_extension=False,
        )
        by_resource = {
            row.resource_id: row for row in projection.resource_specs
        }
        rapid = by_resource["item.elixir_of_rapid_growth"]
        mighty = by_resource["item.mighty_rage_potion"]
        self.assertEqual(rapid.cooldown_group, "item.elixir_of_rapid_growth")
        self.assertEqual(mighty.cooldown_group, "combat_potion")
        self.assertNotEqual(rapid.cooldown_group, mighty.cooldown_group)
        self.assertEqual(rapid.persistent_effect, RAPID_GROWTH_ROUTE_EFFECT_V1)
        resolution = resolve_simulator_cooldown_bindings_v1(
            (
                AvailableAction(
                    0,
                    rapid.action,
                    "Elixir of Rapid Growth",
                    True,
                    0,
                    False,
                    cooldown_duration_ms=120_000,
                ),
            ),
            resource_specs=(rapid,),
        )
        self.assertEqual(
            resolution.bindings[0].persistent_effect,
            RAPID_GROWTH_ROUTE_EFFECT_V1,
        )

    def test_exact_request_resolves_both_trinket_slots(self):
        items = [{} for _ in range(17)]
        items[12] = {"id": 23041}
        items[13] = {"id": 22954}
        request = {
            "raid": {"parties": [{"players": [{"equipment": {"items": items}}]}]}
        }
        self.assertEqual(
            exact_trinket_item_ids_from_request_v1(request),
            {13: 23041, 14: 22954},
        )
        projection = project_contra_turtle_resource_specs_for_request_v1(request)
        by_resource = {
            row.resource_id: row.action for row in projection.resource_specs
        }
        self.assertEqual(by_resource["trinket.slot_13"], ActionRef(item_id=23041))
        self.assertEqual(by_resource["trinket.slot_14"], ActionRef(item_id=22954))


if __name__ == "__main__":
    unittest.main()
