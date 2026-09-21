from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import (
    EquipmentAction,
    QueueLaneOp,
    ScheduledActionPlan,
    ScheduledOperationKind,
    SearchCellIdentity,
    enumerate_scheduled_action_plans,
)


HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)
CLEAVE = ActionRef(spell_id=20569, tag=1)
BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
DEATH_WISH = ActionRef(spell_id=12328)
BERSERKER_STANCE = ActionRef(spell_id=2458)
TRINKET = ActionRef(item_id=19949)
UNKNOWN_TAGGED = ActionRef(spell_id=99999, tag=1)
EXECUTE = ActionRef(spell_id=20662)
BATTLE_SHOUT = ActionRef(spell_id=25289)


def _available(
    action: ActionRef,
    *,
    legal: bool = True,
    ready_in_ms: int = 0,
    triggers_gcd: bool,
) -> AvailableAction:
    return AvailableAction(
        index=0,
        action=action,
        label=str(action),
        legal=legal,
        ready_in_ms=ready_in_ms,
        triggers_gcd=triggers_gcd,
    )


@dataclass(frozen=True)
class _Target:
    target_index: int
    dead: bool
    attackable: bool


class SearchCellIdentityTests(unittest.TestCase):
    def test_identity_canonicalizes_build_components(self) -> None:
        cell = SearchCellIdentity(
            scenario_id="upper-kara",
            wave_or_boss_id="wave-07",
            exact_build_id="fury-dw-001",
            talents=(("flurry", 5), ("bloodthirst", 1)),
            equipment=(("main_hand", 19352), ("trinket_1", 19949)),
            derived_mechanics=(("weapon_speed", 3.4), ("has_crusader", True)),
            environment_branch_id="armor-3600-team-clock-02",
        )
        reordered = SearchCellIdentity(
            scenario_id="upper-kara",
            wave_or_boss_id="wave-07",
            exact_build_id="fury-dw-001",
            talents=(("bloodthirst", 1), ("flurry", 5)),
            equipment=(("trinket_1", 19949), ("main_hand", 19352)),
            derived_mechanics=(("has_crusader", True), ("weapon_speed", 3.4)),
            environment_branch_id="armor-3600-team-clock-02",
        )

        self.assertEqual(cell.cell_key(), reordered.cell_key())
        self.assertEqual(cell.talents[0], ("bloodthirst", 1))
        self.assertEqual(cell.to_dict()["equipment"][0]["slot"], "main_hand")

    def test_identity_rejects_ambiguous_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "talents names must be unique"):
            SearchCellIdentity(
                "raid", "wave", "build", talents=(("flurry", 5), ("flurry", 4))
            )
        with self.assertRaisesRegex(ValueError, "equipment.*positive"):
            SearchCellIdentity(
                "raid", "wave", "build", equipment=(("main_hand", 0),)
            )


class ScheduledActionPlanTests(unittest.TestCase):
    def test_plan_preserves_one_order_across_all_lanes(self) -> None:
        equipment = EquipmentAction(
            slot="main_hand", item_id=19352, is_weapon_swap=True
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=500,
            target_index=2,
            equipment_action=equipment,
            off_gcd_actions=(BERSERKER_STANCE, DEATH_WISH, TRINKET),
            queue_op=QueueLaneOp.SET,
            queue_action=HEROIC_STRIKE,
            gcd_action=BLOODTHIRST,
            guide_provenance=("chronicle:episode-1", "cat:profile-1"),
            guide_priority=7.0,
        )

        operations = plan.ordered_operations()
        self.assertEqual(
            [operation.kind for operation in operations],
            [
                ScheduledOperationKind.SET_TARGET,
                ScheduledOperationKind.EQUIP,
                ScheduledOperationKind.ACT_OFF_GCD,
                ScheduledOperationKind.ACT_OFF_GCD,
                ScheduledOperationKind.ACT_OFF_GCD,
                ScheduledOperationKind.QUEUE_SET,
                ScheduledOperationKind.ACT_GCD,
            ],
        )
        self.assertEqual(
            [operation.action for operation in operations if operation.action],
            [
                BERSERKER_STANCE,
                DEATH_WISH,
                TRINKET,
                HEROIC_STRIKE,
                BLOODTHIRST,
            ],
        )
        serialized = plan.to_dict()
        self.assertEqual(serialized["queue"]["op"], "SET")
        self.assertEqual(serialized["guide"]["priority"], 7.0)
        self.assertEqual(serialized["ordered_operations"][0]["target_index"], 2)

    def test_semantic_plan_key_ignores_guide_metadata(self) -> None:
        first = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=BLOODTHIRST,
            guide_provenance=("cat",),
            guide_priority=12,
        )
        second = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=BLOODTHIRST,
            guide_provenance=("offline",),
            guide_priority=-4,
        )
        self.assertEqual(first.plan_key(), second.plan_key())

    def test_guard_is_part_of_plan_identity_but_is_optional(self) -> None:
        unguarded = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=BLOODTHIRST,
        )
        guarded = ScheduledActionPlan(
            at_or_after_ms=0,
            gcd_action=BLOODTHIRST,
            guard=ObservableCausalGuardV1(rage_gte=50),
        )

        self.assertNotEqual(unguarded.plan_key(), guarded.plan_key())
        self.assertNotIn("guard", unguarded.to_dict())
        self.assertEqual(
            guarded.to_dict()["guard"]["all_of"]["rage_gte"], 50.0
        )

    def test_prefix_lane_order_is_part_of_the_searched_identity(self) -> None:
        off_then_queue = ScheduledActionPlan(
            at_or_after_ms=0,
            off_gcd_actions=(DEATH_WISH,),
            queue_op=QueueLaneOp.SET,
            queue_action=HEROIC_STRIKE,
            gcd_action=BLOODTHIRST,
            prefix_order=(
                ScheduledOperationKind.ACT_OFF_GCD,
                ScheduledOperationKind.QUEUE_SET,
            ),
        )
        queue_then_off = ScheduledActionPlan(
            at_or_after_ms=0,
            off_gcd_actions=(DEATH_WISH,),
            queue_op=QueueLaneOp.SET,
            queue_action=HEROIC_STRIKE,
            gcd_action=BLOODTHIRST,
            prefix_order=(
                ScheduledOperationKind.QUEUE_SET,
                ScheduledOperationKind.ACT_OFF_GCD,
            ),
        )

        self.assertNotEqual(off_then_queue.plan_key(), queue_then_off.plan_key())
        self.assertEqual(
            [
                operation.action
                for operation in off_then_queue.ordered_operations()
                if operation.action
            ],
            [DEATH_WISH, HEROIC_STRIKE, BLOODTHIRST],
        )
        self.assertEqual(
            [
                operation.action
                for operation in queue_then_off.ordered_operations()
                if operation.action
            ],
            [HEROIC_STRIKE, DEATH_WISH, BLOODTHIRST],
        )

    def test_mutually_exclusive_lanes_are_strict(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one GCD action or WAIT"):
            ScheduledActionPlan(
                at_or_after_ms=0, gcd_action=BLOODTHIRST, wait_ms=100
            )
        with self.assertRaisesRegex(ValueError, "queue_action is only valid"):
            ScheduledActionPlan(
                at_or_after_ms=0,
                queue_op=QueueLaneOp.KEEP,
                queue_action=HEROIC_STRIKE,
                wait_ms=100,
            )
        with self.assertRaisesRegex(ValueError, "tagged spell"):
            ScheduledActionPlan(
                at_or_after_ms=0,
                queue_op=QueueLaneOp.SET,
                queue_action=ActionRef(spell_id=25286),
                wait_ms=100,
            )
        with self.assertRaisesRegex(ValueError, "multiple lanes"):
            ScheduledActionPlan(
                at_or_after_ms=0,
                off_gcd_actions=(DEATH_WISH,),
                gcd_action=DEATH_WISH,
            )

    def test_action_refs_and_times_are_validated_at_this_layer(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ScheduledActionPlan(at_or_after_ms=-1, wait_ms=100)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            ScheduledActionPlan(at_or_after_ms=0, wait_ms=0)
        with self.assertRaisesRegex(ValueError, "exactly one positive identity"):
            ScheduledActionPlan(
                at_or_after_ms=0,
                gcd_action=ActionRef(spell_id=1, item_id=2),
            )

    def test_guarded_off_gcd_prefix_can_leave_same_decision_epoch_open(self) -> None:
        guard = ObservableCausalGuardV1(
            target_index=0,
            target_hp_pct_gte=35,
            target_attackable_is=True,
            action_ready=DEATH_WISH,
            false_semantics=SKIP_PLAN,
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            target_index=0,
            off_gcd_actions=(DEATH_WISH,),
            guard=guard,
        )

        self.assertTrue(plan.conditional_prefix_only)
        self.assertIsNone(plan.gcd_action)
        self.assertIsNone(plan.wait_ms)
        self.assertNotIn(
            ScheduledOperationKind.WAIT,
            [operation.kind for operation in plan.ordered_operations()],
        )
        with self.assertRaisesRegex(ValueError, "guarded off-GCD action"):
            ScheduledActionPlan(
                at_or_after_ms=0,
                off_gcd_actions=(DEATH_WISH,),
            )


class ScheduleEnumerationTests(unittest.TestCase):
    def test_additional_waits_are_distinct_searchable_timing_choices(self) -> None:
        plans = enumerate_scheduled_action_plans(
            (),
            (),
            fallback_wait_ms=100,
            additional_wait_ms=(1500, 500, 1500),
        )
        self.assertEqual(
            sorted(plan.wait_ms for plan in plans if plan.wait_ms is not None),
            [100, 500, 1500],
        )

    def setUp(self) -> None:
        self.actions = [
            _available(HEROIC_STRIKE, triggers_gcd=False),
            _available(CLEAVE, legal=False, triggers_gcd=False),
            _available(DEATH_WISH, triggers_gcd=False),
            _available(TRINKET, ready_in_ms=250, triggers_gcd=False),
            _available(UNKNOWN_TAGGED, triggers_gcd=False),
            _available(BLOODTHIRST, triggers_gcd=True),
            _available(WHIRLWIND, ready_in_ms=500, triggers_gcd=True),
            _available(EXECUTE, legal=False, triggers_gcd=True),
        ]
        self.targets = [
            {"target_index": 0, "dead": False, "attackable": True},
            {"target_index": 1, "dead": True, "attackable": False},
            _Target(target_index=2, dead=False, attackable=False),
            _Target(target_index=3, dead=False, attackable=True),
        ]

    def test_enumeration_is_full_and_not_cat_relative(self) -> None:
        plans = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            equipment_actions=(
                EquipmentAction("main_hand", 19352, is_weapon_swap=True),
            ),
            max_off_gcd_actions=2,
            max_prefix_permutations=1,
        )

        # Opaque tag semantics make both tagged non-GCD actions queues.
        # Targets 2 * equipment 2 * ordered off-GCD sequences (1+2+2)
        # * queue (keep/two sets) 3 * endpoint (BT/WW/wait) 3.
        self.assertEqual(len(plans), 180)
        self.assertEqual({plan.target_index for plan in plans}, {0, 3})
        self.assertTrue(any(plan.gcd_action == WHIRLWIND for plan in plans))
        self.assertTrue(
            any(
                plan.off_gcd_actions == (TRINKET, DEATH_WISH)
                for plan in plans
            )
        )
        self.assertTrue(any(plan.queue_action == UNKNOWN_TAGGED for plan in plans))
        all_refs = {
            action
            for plan in plans
            for action in (
                *plan.off_gcd_actions,
                *((plan.queue_action,) if plan.queue_action else ()),
                *((plan.gcd_action,) if plan.gcd_action else ()),
            )
        }
        self.assertNotIn(CLEAVE, all_refs)
        self.assertNotIn(EXECUTE, all_refs)

    def test_stage_direct_gate_excludes_other_attackable_targets(self) -> None:
        plans = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            allowed_direct_target_indexes=(3,),
            max_off_gcd_actions=0,
        )

        self.assertEqual({plan.target_index for plan in plans}, {3})
        self.assertTrue(
            all(
                plan.ordered_operations()[0].kind
                is ScheduledOperationKind.SET_TARGET
                for plan in plans
            )
        )

        blocked = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            allowed_direct_target_indexes=(),
            max_off_gcd_actions=0,
        )
        self.assertTrue(blocked)
        self.assertTrue(all(plan.target_index is None for plan in blocked))
        self.assertTrue(all(plan.gcd_action is None for plan in blocked))

    def test_queue_cancel_is_only_emitted_for_an_active_queue(self) -> None:
        inactive = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            max_off_gcd_actions=0,
            max_prefix_permutations=1,
        )
        active = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            queue_active=True,
            max_off_gcd_actions=0,
            max_prefix_permutations=1,
        )
        self.assertNotIn(QueueLaneOp.CANCEL, {plan.queue_op for plan in inactive})
        self.assertIn(QueueLaneOp.CANCEL, {plan.queue_op for plan in active})
        self.assertTrue(
            all(plan.queue_action is None for plan in active if plan.queue_op is QueueLaneOp.CANCEL)
        )

    def test_enumerator_searches_prefix_lane_order(self) -> None:
        plans = enumerate_scheduled_action_plans(
            [
                _available(HEROIC_STRIKE, triggers_gcd=False),
                _available(DEATH_WISH, triggers_gcd=False),
                _available(BLOODTHIRST, triggers_gcd=True),
            ],
            [{"target_index": 0, "dead": False, "attackable": True}],
            max_off_gcd_actions=1,
            include_wait=False,
        )
        matching = [
            plan
            for plan in plans
            if plan.off_gcd_actions == (DEATH_WISH,)
            and plan.queue_action == HEROIC_STRIKE
            and plan.gcd_action == BLOODTHIRST
        ]

        self.assertEqual(len(matching), 6)
        operation_orders = {
            tuple(operation.kind for operation in plan.ordered_operations())
            for plan in matching
        }
        self.assertIn(
            (
                ScheduledOperationKind.SET_TARGET,
                ScheduledOperationKind.ACT_OFF_GCD,
                ScheduledOperationKind.QUEUE_SET,
                ScheduledOperationKind.ACT_GCD,
            ),
            operation_orders,
        )
        self.assertIn(
            (
                ScheduledOperationKind.QUEUE_SET,
                ScheduledOperationKind.ACT_OFF_GCD,
                ScheduledOperationKind.SET_TARGET,
                ScheduledOperationKind.ACT_GCD,
            ),
            operation_orders,
        )
        self.assertEqual(len({plan.plan_key() for plan in matching}), 6)

    def test_no_live_attackable_target_restricts_endpoint_to_wait(self) -> None:
        plans = enumerate_scheduled_action_plans(
            self.actions,
            [{"target_index": 0, "dead": False, "attackable": False}],
            queue_active=True,
            max_off_gcd_actions=1,
            max_prefix_permutations=4,
        )

        self.assertTrue(plans)
        self.assertTrue(all(plan.target_index is None for plan in plans))
        self.assertTrue(all(plan.gcd_action is None and plan.wait_ms == 100 for plan in plans))
        self.assertNotIn(QueueLaneOp.SET, {plan.queue_op for plan in plans})
        self.assertIn(QueueLaneOp.CANCEL, {plan.queue_op for plan in plans})

    def test_precombat_window_can_search_legal_gcd_self_buff_without_target(self) -> None:
        plans = enumerate_scheduled_action_plans(
            [
                _available(BATTLE_SHOUT, triggers_gcd=True),
                _available(BLOODTHIRST, legal=False, triggers_gcd=True),
            ],
            [{"target_index": 0, "dead": False, "attackable": False}],
            max_off_gcd_actions=0,
            allow_gcd_without_target=True,
        )

        self.assertIn(BATTLE_SHOUT, {plan.gcd_action for plan in plans})
        self.assertNotIn(BLOODTHIRST, {plan.gcd_action for plan in plans})
        self.assertTrue(all(plan.target_index is None for plan in plans))

    def test_guides_reorder_but_never_filter_the_full_space(self) -> None:
        unguided = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            max_off_gcd_actions=2,
            max_prefix_permutations=4,
        )
        guided = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            max_off_gcd_actions=2,
            max_prefix_permutations=4,
            guide_priorities={DEATH_WISH: 10, BLOODTHIRST: 5},
            guide_provenance=("offline:upper-kara:player-1",),
        )

        self.assertEqual(
            {plan.plan_key() for plan in unguided},
            {plan.plan_key() for plan in guided},
        )
        self.assertEqual(guided[0].off_gcd_actions[0], DEATH_WISH)
        self.assertEqual(guided[0].gcd_action, BLOODTHIRST)
        self.assertTrue(
            all(
                plan.guide_provenance == ("offline:upper-kara:player-1",)
                for plan in guided
            )
        )

    def test_unproposed_off_gcd_does_not_increase_expert_priority(self) -> None:
        guided = enumerate_scheduled_action_plans(
            self.actions,
            self.targets,
            max_off_gcd_actions=1,
            max_prefix_permutations=4,
            guide_priorities={BLOODTHIRST: 5},
            guide_provenance=("cat",),
        )
        plain = next(
            plan for plan in guided
            if plan.gcd_action == BLOODTHIRST and not plan.off_gcd_actions
        )
        with_unproposed = next(
            plan for plan in guided
            if plan.gcd_action == BLOODTHIRST
            and plan.off_gcd_actions == (DEATH_WISH,)
        )
        self.assertGreater(plain.guide_priority, with_unproposed.guide_priority)

    def test_empty_target_registry_keeps_current_target_unspecified(self) -> None:
        plans = enumerate_scheduled_action_plans(
            [_available(BLOODTHIRST, triggers_gcd=True)],
            [],
            max_off_gcd_actions=0,
        )
        self.assertEqual({plan.target_index for plan in plans}, {None})
        self.assertIn(BLOODTHIRST, {plan.gcd_action for plan in plans})

    def test_guards_expand_only_when_explicitly_supplied(self) -> None:
        kwargs = {
            "available_actions": [
                _available(BLOODTHIRST, triggers_gcd=True)
            ],
            "target_states": [],
            "max_off_gcd_actions": 0,
            "include_wait": False,
        }
        default = enumerate_scheduled_action_plans(**kwargs)
        guard = ObservableCausalGuardV1(rage_gte=50)
        explicit = enumerate_scheduled_action_plans(
            **kwargs,
            guard_options=(guard,),
        )

        self.assertEqual(len(default), 1)
        self.assertEqual(len(explicit), 2)
        self.assertEqual({plan.guard for plan in explicit}, {None, guard})

    def test_skip_guard_enumerates_zero_time_off_gcd_prefix(self) -> None:
        guard = ObservableCausalGuardV1(
            target_index=0,
            target_hp_pct_gte=35,
            target_attackable_is=True,
            action_ready=DEATH_WISH,
            false_semantics=SKIP_PLAN,
        )
        plans = enumerate_scheduled_action_plans(
            [
                _available(DEATH_WISH, triggers_gcd=False),
                _available(BLOODTHIRST, triggers_gcd=True),
            ],
            [{"target_index": 0, "dead": False, "attackable": True}],
            max_off_gcd_actions=1,
            guard_options=(guard,),
        )
        prefixes = [plan for plan in plans if plan.conditional_prefix_only]

        self.assertTrue(prefixes)
        self.assertTrue(
            all(plan.off_gcd_actions == (DEATH_WISH,) for plan in prefixes)
        )
        self.assertTrue(all(plan.guard == guard for plan in prefixes))


if __name__ == "__main__":
    unittest.main()
