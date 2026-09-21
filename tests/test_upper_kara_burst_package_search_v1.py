from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.contra_fury_burst_inventory_v1 import (
    build_contra_fury_burst_inventory_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import (
    build_contra_turtle_burst_loadouts_v1,
)
from o2o_dps.raid_cooldown_schedule_v1 import TRASH
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.simulator_cooldown_binding_v1 import (
    project_contra_turtle_resource_specs_for_request_v1,
    resolve_simulator_cooldown_bindings_v1,
)
from o2o_dps.upper_kara_burst_package_search_v1 import (
    UpperKaraBurstPackageSearchV1Error,
    build_upper_kara_burst_package_campaign_manifest_v1,
    run_upper_kara_burst_package_search_v1,
    select_burst_route_resources_v1,
)
import o2o_dps.upper_kara_burst_package_search_v1 as burst_driver
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


HIT = ActionRef(spell_id=23894)


def _request() -> dict:
    items = [{} for _ in range(17)]
    items[12] = {"id": 23041}
    items[13] = {"id": 19406}
    return {
        "raid": {"parties": [{"players": [{"equipment": {"items": items}}]}]}
    }


def _available(index, action, cooldown_ms, label=""):
    return AvailableAction(
        index,
        action,
        label,
        True,
        0,
        action.spell_id > 0,
        cooldown_duration_ms=cooldown_ms,
    )


class _OneHitReplay:
    def replay(self, seed, schedule):
        actions = [
            action
            for step in schedule
            for action in (step.gcd_action, *step.off_gcd_actions)
            if action is not None
        ]
        finished = HIT in actions
        if finished:
            return ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.COMPLETE,
                state={
                    "time_ms": 1500,
                    "damage_done": 100.0 + seed,
                    "finished": True,
                    "needs_input": False,
                    "dynamic_target_semantics": {
                        "targets": [{"target_index": 0, "dead": True}]
                    },
                },
                receipts=({
                    "step_index": 0,
                    "kind": "ACT_GCD",
                    "accepted": True,
                    "action": HIT.to_wire(),
                    "state_time_before_ms": 0,
                },),
            )
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=ReplayStatusV1.FRONTIER,
            state={
                "time_ms": 0,
                "damage_done": 0.0,
                "finished": False,
                "needs_input": True,
                "dynamic_target_semantics": {
                    "targets": [
                        {"target_index": 0, "dead": False, "attackable": True}
                    ]
                },
            },
            available_actions=(_available(0, HIT, 0, "Bloodthirst"),),
        )


class _NeverFinishesReplay(_OneHitReplay):
    def replay(self, seed, schedule):
        del schedule
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=ReplayStatusV1.FRONTIER,
            state={
                "time_ms": 0,
                "damage_done": 0.0,
                "finished": False,
                "needs_input": True,
                "dynamic_target_semantics": {
                    "targets": [
                        {"target_index": 0, "dead": False, "attackable": True}
                    ]
                },
            },
            available_actions=(_available(0, HIT, 0, "Bloodthirst"),),
        )


class UpperKaraBurstPackageSearchV1Tests(unittest.TestCase):
    def _selection(self, loadout_id):
        request = _request()
        inventory = build_contra_fury_burst_inventory_v1()
        projection = project_contra_turtle_resource_specs_for_request_v1(
            request, inventory=inventory
        )
        snapshot = (
            _available(0, ActionRef(item_id=61181), 120_000, "Quickness"),
            _available(1, ActionRef(item_id=13442), 120_000, "Mighty Rage"),
            _available(2, ActionRef(item_id=23041), 120_000, "Slayer's Crest"),
            _available(3, ActionRef(spell_id=12328), 180_000, "Death Wish"),
            _available(4, ActionRef(spell_id=2687), 60_000, "Bloodrage"),
            _available(5, ActionRef(spell_id=18499), 30_000, "Berserker Rage"),
            _available(6, ActionRef(item_id=56113), 120_000, "Rapid Growth"),
            # Sapper 10646 is deliberately absent from this exact snapshot.
        )
        resolution = resolve_simulator_cooldown_bindings_v1(
            snapshot, resource_specs=projection.resource_specs
        )
        loadout = {
            row.loadout_id: row
            for row in build_contra_turtle_burst_loadouts_v1(request)
        }[loadout_id]
        return select_burst_route_resources_v1(
            loadout, inventory, projection, resolution, snapshot
        )

    def test_quickness_and_mighty_rage_are_mutually_exclusive_by_loadout(self):
        quickness_selection = self._selection(
            "contra_turtle_burst__quickness"
        )
        quickness = {
            row.resource_id: row
            for row in quickness_selection.decisions
        }
        self.assertTrue(
            quickness["item.quickness_potion"].route_resource_included
        )
        self.assertEqual(
            quickness["item.mighty_rage_potion"].disposition,
            "EXCLUDED_MUTUALLY_EXCLUSIVE_LOADOUT",
        )
        self.assertFalse(
            quickness["item.mighty_rage_potion"].remains_available_in_every_arm
        )
        self.assertIn(
            "item.mighty_rage_potion",
            {
                row.resource_id
                for row in quickness_selection.always_disabled_bindings
            },
        )
        self.assertIn(
            "item.quickness_potion",
            {row.resource_id for row in quickness_selection.route_bindings},
        )

        mighty = {
            row.resource_id: row
            for row in self._selection(
                "contra_turtle_burst__mighty_rage"
            ).decisions
        }
        self.assertTrue(
            mighty["item.mighty_rage_potion"].route_resource_included
        )
        self.assertEqual(
            mighty["item.quickness_potion"].disposition,
            "EXCLUDED_MUTUALLY_EXCLUSIVE_LOADOUT",
        )

    def test_trinket_is_bound_but_short_cooldowns_stay_in_inner_search(self):
        selection = self._selection("contra_turtle_burst__quickness")
        decisions = {row.resource_id: row for row in selection.decisions}
        self.assertEqual(
            decisions["trinket.slot_13"].disposition,
            "INCLUDED_LONG_COOLDOWN",
        )
        self.assertEqual(
            decisions["trinket.slot_13"].action,
            ActionRef(item_id=23041),
        )
        for resource_id in ("warrior.bloodrage", "warrior.berserker_rage"):
            self.assertEqual(
                decisions[resource_id].disposition,
                "EXCLUDED_SHORT_COOLDOWN_REMAINS_ORDINARY",
            )
            self.assertTrue(
                decisions[resource_id].remains_available_in_every_arm
            )
            self.assertNotIn(
                resource_id,
                {row.resource_id for row in selection.search_constraint_bindings},
            )

    def test_rapid_growth_supported_and_sapper_native_absence_are_explicit(self):
        decisions = {
            row.resource_id: row
            for row in self._selection(
                "contra_turtle_burst__quickness"
            ).decisions
        }
        self.assertEqual(
            decisions["item.elixir_of_rapid_growth"].disposition,
            "INCLUDED_EXPLICITLY_FINITE_CONSUMABLE",
        )
        self.assertEqual(
            decisions["item.goblin_sapper_charge"].disposition,
            "EXCLUDED_NATIVE_ACTION_ABSENT",
        )

    def test_no_route_resource_still_runs_paired_no_resource_baseline(self):
        result = run_upper_kara_burst_package_search_v1(
            seeds=(1, 2),
            representative_rank=11,
            stratum="q05",
            loadout_id="contra_turtle_burst__no_potion",
            encounter_id="utk:q05:rank11",
            encounter_kind=TRASH,
            raid_start_ms=10_000,
            max_steps=1,
            beam_width=4,
            max_off_gcd_actions=0,
            snapshot_loader=lambda case: (_available(0, HIT, 0, "Bloodthirst"),),
            replay=_OneHitReplay(),
            action_guides=(),
        )
        self.assertEqual(
            result.package_search.baseline_search.status,
            "COMPLETE_PAIRED_SEED_SCHEDULE",
        )
        self.assertEqual(result.resource_selection.route_bindings, ())
        self.assertEqual(result.planner_cell.packages, ())
        payload = result.to_dict()
        self.assertEqual(payload["package_search"]["baseline_resource_budget"], [])
        self.assertTrue(
            payload["contract"]["no_resource_baseline_is_paired_on_same_seeds"]
        )

    def test_incomplete_baseline_fails_without_an_encounter_cell(self):
        with self.assertRaisesRegex(
            UpperKaraBurstPackageSearchV1Error,
            "PACKAGE_SEARCH_FAILED_CLOSED",
        ):
            run_upper_kara_burst_package_search_v1(
                seeds=(1,),
                representative_rank=11,
                stratum="q05",
                loadout_id="contra_turtle_burst__no_potion",
                encounter_id="utk:q05:rank11",
                encounter_kind=TRASH,
                raid_start_ms=10_000,
                max_steps=1,
                beam_width=4,
                max_off_gcd_actions=0,
                snapshot_loader=lambda case: (
                    _available(0, HIT, 0, "Bloodthirst"),
                ),
                replay=_NeverFinishesReplay(),
                action_guides=(),
            )

    def test_campaign_manifest_uses_disjoint_train_and_eval_panels(self):
        train = tuple(range(1000, 1064))
        evaluation = tuple(range(2000, 2064))
        # One exact-cell slice exercises all four request-level potion variants;
        # production defaults expand the same product over all 12 exact cells.
        with patch.object(
            burst_driver, "UPPER_KARA_HISTORICAL_FURY_RANKS", (11,)
        ), patch.object(burst_driver, "UPPER_KARA_WAVE_STRATA", ("q05",)):
            payload = build_upper_kara_burst_package_campaign_manifest_v1(
                train_seeds=train,
                evaluation_seeds=evaluation,
            )
        self.assertEqual(payload["status"], "PREPARED_NOT_RUN")
        self.assertFalse(payload["execution_evidence"])
        self.assertEqual(payload["exact_cell_count"], 1)
        self.assertEqual(payload["loadout_count_per_exact_cell"], 4)
        self.assertEqual(len(payload["cells"]), 4)
        self.assertEqual(
            len({
                json.dumps(row["search_cell"], sort_keys=True)
                for row in payload["cells"]
            }),
            4,
        )
        for row in payload["cells"]:
            self.assertEqual(row["status"], "PREPARED_NOT_RUN")
            self.assertEqual(row["seeds"], list(train))
            self.assertEqual(row["evaluation_seeds"], list(evaluation))
            self.assertFalse(set(row["seeds"]) & set(row["evaluation_seeds"]))

    def test_campaign_manifest_rejects_overlapping_seed_panels(self):
        with self.assertRaisesRegex(ValueError, "must be disjoint"):
            build_upper_kara_burst_package_campaign_manifest_v1(
                train_seeds=tuple(range(64)),
                evaluation_seeds=tuple(range(63, 127)),
            )


if __name__ == "__main__":
    unittest.main()
