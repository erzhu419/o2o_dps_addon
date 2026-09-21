from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.raid_cooldown_schedule_v1 import (
    TRASH,
    RoutePersistentEffectV1,
    RoutePersistentStatPhaseV1,
    allocate_raid_cooldown_packages_v1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import SearchCellIdentity
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)
from o2o_dps.wave_cooldown_package_measurement_v1 import (
    CooldownActionBindingV1,
)
from o2o_dps.wave_cooldown_package_search_v1 import (
    ResourceConstrainedScheduleReplayV1,
    enumerate_cooldown_resource_arms_v1,
    search_wave_cooldown_packages_v1,
)


DEATH_WISH = ActionRef(spell_id=12328)
RECKLESSNESS = ActionRef(spell_id=1719)
HIT = ActionRef(spell_id=23894)


def _state(time_ms, damage, *, finished, needs_input):
    return {
        "time_ms": time_ms,
        "damage_done": damage,
        "finished": finished,
        "needs_input": needs_input,
        "dynamic_target_semantics": {
            "targets": [{
                "target_index": 0,
                "dead": finished,
                "attackable": not finished,
            }]
        },
    }


class _Replay:
    def replay(self, seed, schedule):
        actions = [
            action
            for step in schedule
            for action in (step.gcd_action, *step.off_gcd_actions)
            if action is not None
        ]
        has_death_wish = DEATH_WISH in actions
        hits = actions.count(HIT)
        finished = hits >= 1
        if finished:
            damage = 120.0 + seed if has_death_wish else 100.0 + seed
            available = ()
            status = ReplayStatusV1.COMPLETE
        else:
            damage = 0.0
            available = (
                AvailableAction(0, HIT, "hit", True, 0, True),
                AvailableAction(1, DEATH_WISH, "death wish", True, 0, False),
                AvailableAction(2, RECKLESSNESS, "reck", True, 0, True),
            )
            status = ReplayStatusV1.FRONTIER
        receipts = tuple(
            {
                "kind": "ACT_GCD",
                "accepted": True,
                "action": action.to_wire(),
                "state_time_before_ms": index * 1500,
            }
            for index, action in enumerate(actions)
        )
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=status,
            state=_state(hits * 1500, damage, finished=finished, needs_input=not finished),
            available_actions=available,
            receipts=receipts,
        )


class WaveCooldownPackageSearchV1Tests(unittest.TestCase):
    def setUp(self):
        self.bindings = (
            CooldownActionBindingV1(
                DEATH_WISH,
                "warrior.death_wish",
                "warrior.death_wish",
                180_000,
                "SPELL",
            ),
            CooldownActionBindingV1(
                RECKLESSNESS,
                "warrior.recklessness",
                "warrior.recklessness",
                1_800_000,
                "SPELL",
            ),
        )
        self.cell = SearchCellIdentity("utk", "wave-1", "build-1")

    def test_arm_enumeration_covers_singletons_and_joint_package(self):
        arms = enumerate_cooldown_resource_arms_v1(self.bindings)
        self.assertEqual(
            [row.enabled_resource_ids for row in arms],
            [
                ("warrior.death_wish",),
                ("warrior.recklessness",),
                ("warrior.death_wish", "warrior.recklessness"),
            ],
        )

    def test_constraint_hides_only_disabled_resource_actions(self):
        replay = ResourceConstrainedScheduleReplayV1(
            _Replay(),
            bindings=self.bindings,
            enabled_resource_ids=("warrior.death_wish",),
        )
        outcome = replay.replay(1, ())
        by_action = {row.action: row for row in outcome.available_actions}
        self.assertTrue(by_action[HIT].legal)
        self.assertTrue(by_action[DEATH_WISH].legal)
        self.assertFalse(by_action[RECKLESSNESS].legal)

    def test_search_reuses_terminal_outcomes_and_emits_measured_package(self):
        result = search_wave_cooldown_packages_v1(
            _Replay(),
            self.cell,
            encounter_id="utk-wave-1",
            encounter_kind=TRASH,
            raid_start_ms=10_000,
            seeds=(1, 2),
            bindings=(self.bindings[0],),
            max_steps=2,
            beam_width=16,
            max_off_gcd_actions=1,
            max_prefix_permutations=1,
            replay_workers=1,
        )
        self.assertEqual(result.baseline_search.status, "COMPLETE_PAIRED_SEED_SCHEDULE")
        self.assertEqual(len(result.planner_cell.packages), 1)
        package = result.planner_cell.packages[0]
        self.assertEqual(package.uses[0].resource_id, "warrior.death_wish")
        self.assertEqual(package.marginal_value, 20.0)
        self.assertEqual(result.arms[0].status, "PAIRED_MEASURED_PLANNER_ELIGIBLE")
        payload = result.to_dict()
        self.assertTrue(
            payload["contract"]["paired_terminal_outcomes_reused_without_duplicate_replay"]
        )

    def test_persistent_arm_is_measured_but_not_claimed_route_ready(self):
        effect = RoutePersistentEffectV1(
            "rapid-growth",
            (RoutePersistentStatPhaseV1(
                "growth", 0, 120_000, (("strength", 30.0),)
            ),),
        )
        binding = CooldownActionBindingV1(
            DEATH_WISH,
            "rapid-growth",
            "rapid-growth",
            120_000,
            "CONSUMABLE",
            effect,
        )
        result = search_wave_cooldown_packages_v1(
            _Replay(),
            self.cell,
            encounter_id="utk-wave-1",
            encounter_kind=TRASH,
            raid_start_ms=10_000,
            seeds=(1, 2),
            bindings=(binding,),
            max_steps=2,
            beam_width=16,
            max_off_gcd_actions=1,
            max_prefix_permutations=1,
            replay_workers=1,
        )
        self.assertEqual(
            result.arms[0].status,
            "PAIRED_MEASURED_ROUTE_STATE_REQUIRED",
        )
        with self.assertRaisesRegex(ValueError, "cross-encounter state-aware"):
            allocate_raid_cooldown_packages_v1((result.planner_cell,))


if __name__ == "__main__":
    unittest.main()
