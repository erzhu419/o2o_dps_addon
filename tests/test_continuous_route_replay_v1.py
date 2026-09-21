from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from o2o_dps.continuous_route_replay_v1 import (
    ContinuousRouteReplayOutcomeV1,
    ContinuousRouteReplayStatusV1,
    ContinuousRouteReplayV1Error,
    ExplicitPerSeedRouteStartResolverV1,
    ExpectedAcceptedCooldownUseV1,
    FrozenContinuousRouteCellV1,
    FrozenContinuousRouteLaneV1,
    NativeContinuousRouteReplayV1,
    build_explicit_route_start_resolver_v1,
    build_frozen_burst_route_lane_v1,
    evaluate_continuous_held_out_route_v1,
    paired_route_timing_observation_from_outcomes_v1,
)
from o2o_dps.raid_cooldown_schedule_v1 import (
    TRASH,
    CooldownPackageAssignmentV1,
    CooldownPackageV1,
    CooldownUseV1,
    RaidCooldownScheduleV1,
)
from o2o_dps.route_timing_calibration_v1 import (
    DownstreamStartShiftBoundsV1,
    RouteTimingShiftProfileV1,
)
from o2o_dps.sim_bridge import ActionRef, ActResult, AvailableAction
from o2o_dps.upper_kara_burst_route_train_eval_v1 import (
    FrozenBurstRoutePlanV1,
    FrozenEncounterActionTableV1,
)
from o2o_dps.upper_kara_wave_local_search_contract_v1 import (
    ObservedTargetStateV1,
)
from o2o_dps.upper_kara_wave_target_gate_v1 import (
    ReactiveBossAddsTargetGateV1,
)
from o2o_dps.wave_action_schedule_v1 import EquipmentAction, ScheduledActionPlan
from o2o_dps.wave_cooldown_package_measurement_v1 import CooldownActionBindingV1


DW = ActionRef(spell_id=12_328)
BT = ActionRef(spell_id=23_894)
BERSERKER_STANCE = ActionRef(spell_id=2_458)
DIGEST = "continuous-route-fixture"


def _binding() -> CooldownActionBindingV1:
    return CooldownActionBindingV1(
        action=DW,
        resource_id="warrior.death_wish",
        cooldown_group="death_wish",
        cooldown_ms=180_000,
        action_kind="SPELL",
    )


def _cell_one(*, expected: bool = True, cast_dw: bool = True):
    uses = (
        ExpectedAcceptedCooldownUseV1(
            encounter_id="wave-1",
            resource_id="warrior.death_wish",
            action=DW,
            cooldown_ms=180_000,
            earliest_route_time_ms=10,
            latest_route_time_ms=10,
        ),
    ) if expected else ()
    return FrozenContinuousRouteCellV1(
        encounter_id="wave-1",
        raid_start_ms=10,
        pull_time_ms=10,
        schedule=(ScheduledActionPlan(
            at_or_after_ms=10,
            off_gcd_actions=(DW,) if cast_dw else (),
            gcd_action=BT,
        ),),
        expected_cooldown_uses=uses,
        package_id="loadout::resources__death_wish" if expected else None,
    )


def _cell_two():
    return FrozenContinuousRouteCellV1(
        encounter_id="wave-2",
        raid_start_ms=100,
        pull_time_ms=10,
        schedule=(ScheduledActionPlan(at_or_after_ms=10, wait_ms=1),),
    )


def _lane(
    *, expected: bool = True, cast_dw: bool = True, lane_id: str = "candidate"
):
    return FrozenContinuousRouteLaneV1(
        lane_id=lane_id,
        cells=(
            _cell_one(expected=expected, cast_dw=cast_dw),
            _cell_two(),
        ),
        static_equipment=(("slot_14", 19_019), ("slot_15", 19_363)),
        talents=(("flurry", 5),),
        training_seeds=(11, 13),
    )


class _Bridge:
    def __init__(
        self,
        *,
        omit_queue=False,
        cooldown_starts=True,
        cooldown_after_accept_ms=180_000,
        damage=100.0,
        digest=DIGEST,
    ):
        self.omit_queue = omit_queue
        self.cooldown_starts = cooldown_starts
        self.cooldown_after_accept_ms = cooldown_after_accept_ms
        self.damage = damage
        self.load_calls = 0
        self.close_calls = 0
        self.cooldowns = {DW: 0, BT: 0}
        self._state = {
            "time_ms": 0,
            "needs_input": True,
            "finished": False,
            "power": {"type": "rage", "current": 80.0, "maximum": 100.0},
            "stance": "BERSERKER",
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 1_000,
            "mh_swing_duration_ms": 2_600,
            "oh_swing_remaining_ms": 500,
            "swing_queue": {"kind": "NONE", "status": "NONE"},
            "current_cast": None,
            "auras": [{
                "label": "Berserker Stance",
                "action": BERSERKER_STANCE.to_wire(),
                "stacks": 0,
                "remaining_ms": -1,
            }],
            "autoattack_active": True,
            "damage_done": 0.0,
            "dynamic_team_background": {
                "environment_generation": 7,
                "config_digest": digest,
                "simulated_damage_applied": 0.0,
            },
            "dynamic_target_semantics": {
                "environment_generation": 7,
                "config_digest": digest,
            },
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close_calls += 1
        return False

    def load(self, seed):
        self.load_calls += 1
        return SimpleNamespace(state=self.state())

    def state(self):
        state = {
            **self._state,
            "power": dict(self._state["power"]),
            "auras": [dict(row) for row in self._state["auras"]],
            "dynamic_team_background": dict(
                self._state["dynamic_team_background"]
            ),
            "dynamic_target_semantics": dict(
                self._state["dynamic_target_semantics"]
            ),
        }
        if self.omit_queue:
            state.pop("swing_queue", None)
        else:
            state["swing_queue"] = dict(self._state["swing_queue"])
        return state

    def actions(self):
        return [
            AvailableAction(
                index=0,
                action=DW,
                label="Death Wish",
                legal=self.cooldowns[DW] == 0,
                ready_in_ms=self.cooldowns[DW],
                triggers_gcd=False,
                cooldown_duration_ms=180_000,
            ),
            AvailableAction(
                index=1,
                action=BT,
                label="Bloodthirst",
                legal=self.cooldowns[BT] == 0,
                ready_in_ms=self.cooldowns[BT],
                triggers_gcd=True,
                result_bearing=True,
                cooldown_duration_ms=6_000,
            ),
        ]

    def wait(self, wait_ms):
        before = self._state["time_ms"]
        self._advance_time(wait_ms)
        if wait_ms == 1 and before >= 80:
            self._state["finished"] = True
            self._state["needs_input"] = False
        return self.state()

    def advance(self):
        self._state["needs_input"] = True
        return self.state()

    def act(self, action, *, attempt_id=None):
        if action == DW:
            if self.cooldowns[DW] != 0:
                raise RuntimeError("Death Wish is on cooldown")
            if self.cooldown_starts:
                self.cooldowns[DW] = self.cooldown_after_accept_ms
            self._state["auras"].append({
                "label": "Death Wish",
                "action": DW.to_wire(),
                "stacks": 1,
                "remaining_ms": 30_000,
            })
            consumes = False
        elif action == BT:
            self.cooldowns[BT] = 6_000
            self._state["power"]["current"] = 60.0
            self._state["gcd_remaining_ms"] = 1_500
            self._state["mh_swing_remaining_ms"] = 900
            self._state["oh_swing_remaining_ms"] = 400
            self._state["swing_queue"] = {
                "kind": "CLEAVE",
                "status": "PENDING",
            }
            self._state["damage_done"] = self.damage
            self._state["dynamic_team_background"][
                "simulated_damage_applied"
            ] = self.damage
            consumes = True
        else:
            raise RuntimeError("unknown action")
        return ActResult(
            casted=True,
            consumes_decision=consumes,
            finished=False,
            needs_input=True,
            state=self.state(),
        )

    def _advance_time(self, duration):
        self._state["time_ms"] += duration
        self._state["gcd_remaining_ms"] = max(
            0, self._state["gcd_remaining_ms"] - duration
        )
        self._state["mh_swing_remaining_ms"] = max(
            0, self._state["mh_swing_remaining_ms"] - duration
        )
        if self._state["oh_swing_remaining_ms"] is not None:
            self._state["oh_swing_remaining_ms"] = max(
                0, self._state["oh_swing_remaining_ms"] - duration
            )
        for action in self.cooldowns:
            self.cooldowns[action] = max(0, self.cooldowns[action] - duration)
        for aura in self._state["auras"]:
            if aura["remaining_ms"] >= 0:
                aura["remaining_ms"] = max(0, aura["remaining_ms"] - duration)


class _ReactiveTargetBridge(_Bridge):
    """Small continuous-route fixture with two causally observed add waves."""

    def __init__(self, *, incomplete_observation=False):
        super().__init__()
        self.incomplete_observation = incomplete_observation
        self.target_history = []
        self._state["target_index"] = 0
        self._refresh_observations()

    def set_target(self, target_index):
        self._state["target_index"] = target_index
        self.target_history.append(target_index)
        return SimpleNamespace(
            target_index=target_index,
            state=self.state(),
        )

    def _advance_time(self, duration):
        super()._advance_time(duration)
        self._refresh_observations()

    def _refresh_observations(self):
        now = self._state["time_ms"]
        boss = ObservedTargetStateV1(True, True, False)
        if now < 15:
            add_one = ObservedTargetStateV1(False, False, False)
            add_two = ObservedTargetStateV1(False, False, False)
        elif now < 20:
            add_one = ObservedTargetStateV1(True, True, False)
            add_two = ObservedTargetStateV1(False, False, False)
        elif now < 25:
            add_one = ObservedTargetStateV1(True, False, True)
            add_two = ObservedTargetStateV1(False, False, False)
        else:
            add_one = ObservedTargetStateV1(True, False, True)
            add_two = ObservedTargetStateV1(True, True, False)
        observations = {0: boss, 1: add_one, 2: add_two}
        if self.incomplete_observation:
            observations.pop(2)
        self._state["boss_add_observations"] = observations


def _reactive_target_gate():
    return ReactiveBossAddsTargetGateV1(
        boss_target_index=0,
        add_target_indexes=(1, 2),
        observation_provider=lambda state: state["boss_add_observations"],
    )


def _reactive_target_lane():
    return FrozenContinuousRouteLaneV1(
        lane_id="reactive-targets",
        cells=(FrozenContinuousRouteCellV1(
            encounter_id="boss",
            raid_start_ms=10,
            pull_time_ms=10,
            schedule=(
                ScheduledActionPlan(
                    at_or_after_ms=10, target_index=0, wait_ms=5
                ),
                ScheduledActionPlan(
                    at_or_after_ms=15, target_index=1, wait_ms=5
                ),
                ScheduledActionPlan(
                    at_or_after_ms=20, target_index=0, wait_ms=5
                ),
                ScheduledActionPlan(
                    at_or_after_ms=25, target_index=2, wait_ms=1
                ),
            ),
            target_gate=_reactive_target_gate(),
        ),),
        static_equipment=(("slot_14", 19_019),),
        talents=(("flurry", 5),),
        training_seeds=(11,),
    )


def _native(lane, *, cell_start_resolver=None, **bridge_options):
    bridges = []

    def bridge_factory():
        bridge = _Bridge(**bridge_options)
        bridges.append(bridge)
        return bridge

    replay = NativeContinuousRouteReplayV1(
        bridge_factory,
        lambda bridge, seed: bridge.load(seed),
        lane,
        cooldown_bindings=(_binding(),),
        route_clock_zero_ms=0,
        cell_start_resolver=cell_start_resolver,
    )
    return replay, bridges


class ContinuousRouteReplayV1Tests(unittest.TestCase):
    def test_reactive_gate_reenters_adds_inside_one_continuous_boss_cell(self):
        lane = _reactive_target_lane()
        bridges = []

        def bridge_factory():
            bridge = _ReactiveTargetBridge()
            bridges.append(bridge)
            return bridge

        replay = NativeContinuousRouteReplayV1(
            bridge_factory,
            lambda bridge, seed: bridge.load(seed),
            lane,
            cooldown_bindings=(_binding(),),
            route_clock_zero_ms=0,
        )
        outcome = replay.replay(101)

        self.assertEqual(ContinuousRouteReplayStatusV1.FRONTIER, outcome.status)
        # Every switch was accepted by the live gate: boss -> first add -> boss
        # -> later add.  The final add selection proves non-monotone re-entry.
        self.assertEqual([0, 1, 0, 2], bridges[0].target_history)
        self.assertEqual(2, outcome.final_state["target_index"])

    def test_reactive_gate_incomplete_current_observation_fails_closed(self):
        lane = _reactive_target_lane()

        replay = NativeContinuousRouteReplayV1(
            lambda: _ReactiveTargetBridge(incomplete_observation=True),
            lambda bridge, seed: bridge.load(seed),
            lane,
            cooldown_bindings=(_binding(),),
            route_clock_zero_ms=0,
        )
        outcome = replay.replay(101)

        self.assertEqual(ContinuousRouteReplayStatusV1.INVALID, outcome.status)
        self.assertIn(
            "exactly the boss and every declared add target index",
            outcome.invalid_reason,
        )

    def test_builds_candidate_expectation_from_frozen_route_assignment(self):
        use = CooldownUseV1(
            resource_id="warrior.death_wish",
            cooldown_group="death_wish",
            cooldown_ms=180_000,
            use_at_ms=-2,
            action_kind="SPELL",
        )
        package = CooldownPackageV1(
            package_id="loadout::resources__death_wish",
            uses=(use,),
            marginal_value=25.0,
        )
        candidate_table = FrozenEncounterActionTableV1(
            encounter_id="wave-1",
            encounter_kind=TRASH,
            raid_start_ms=12,
            loadout_id="loadout",
            source_kind="PLANNER_SELECTED_RESOURCE_ARM",
            namespaced_package_id=package.package_id,
            source_package_id="resources__death_wish",
            pull_time_ms=10,
            starting_equipment=(("slot_14", 19_019),),
            talents=(("flurry", 5),),
            training_seeds=(11, 13),
            schedule=(ScheduledActionPlan(
                at_or_after_ms=8,
                off_gcd_actions=(DW,),
                gcd_action=BT,
            ),),
        )
        baseline_table = replace(
            candidate_table,
            loadout_id="no-resource",
            source_kind="NO_RESOURCE_NO_POTION_BASELINE",
            namespaced_package_id=None,
            source_package_id=None,
            schedule=(ScheduledActionPlan(at_or_after_ms=10, gcd_action=BT),),
        )
        later_table = replace(
            baseline_table,
            encounter_id="wave-2",
            raid_start_ms=100,
            schedule=(ScheduledActionPlan(at_or_after_ms=10, wait_ms=1),),
        )
        plan = FrozenBurstRoutePlanV1(
            planner_schedule=RaidCooldownScheduleV1(
                assignments=(CooldownPackageAssignmentV1(
                    encounter_id="wave-1",
                    encounter_kind=TRASH,
                    raid_start_ms=12,
                    package=package,
                ),),
                total_marginal_value=25.0,
                boss_independent_groups=("combat_potion",),
            ),
            encounters=(candidate_table, later_table),
            baselines=(baseline_table, later_table),
            persistent_effect_windows=(),
            route_persistent_effects_resolved=True,
        )

        candidate = build_frozen_burst_route_lane_v1(
            plan,
            lane_id="candidate",
            lane="candidate",
            cooldown_bindings=(_binding(),),
        )
        baseline = build_frozen_burst_route_lane_v1(
            plan,
            lane_id="baseline",
            lane="baseline",
            cooldown_bindings=(_binding(),),
        )
        expected = candidate.cells[0].expected_cooldown_uses
        self.assertEqual(1, len(expected))
        self.assertEqual(10, expected[0].earliest_route_time_ms)
        self.assertEqual(10, expected[0].latest_route_time_ms)
        self.assertEqual((), baseline.cells[0].expected_cooldown_uses)
        resolver = build_explicit_route_start_resolver_v1(
            plan,
            candidate,
            expected_route_case_id="upper-kara/route-1/build-a",
            profiles=(RouteTimingShiftProfileV1(
                route_case_id="upper-kara/route-1/build-a",
                source_encounter_id="wave-1",
                package_id=package.package_id,
                route_encounter_ids=("wave-1", "wave-2"),
                downstream=(DownstreamStartShiftBoundsV1(
                    "wave-2", 1, ((101, 0),)
                ),),
            ),),
        )
        self.assertEqual(12, resolver(101, candidate.cells[0]))
        self.assertEqual(100, resolver(101, candidate.cells[1]))

        with self.assertRaisesRegex(ValueError, "zero shifts must also be observed"):
            build_explicit_route_start_resolver_v1(
                plan,
                candidate,
                expected_route_case_id="upper-kara/route-1/build-a",
                profiles=(),
            )

    def test_explicit_profile_resolves_only_recorded_seed_shift(self):
        profile = RouteTimingShiftProfileV1(
            route_case_id="upper-kara/route-1/build-a",
            source_encounter_id="wave-1",
            package_id="loadout::resources__death_wish",
            route_encounter_ids=("wave-1", "wave-2"),
            downstream=(DownstreamStartShiftBoundsV1(
                encounter_id="wave-2",
                route_index=1,
                seed_shifts_ms=((101, -10), (103, -20)),
            ),),
        )
        resolver = ExplicitPerSeedRouteStartResolverV1(
            expected_route_case_id="upper-kara/route-1/build-a",
            route_encounter_ids=("wave-1", "wave-2"),
            baseline_start_ms=(10, 100),
            profiles=(profile,),
        )
        cells = _lane().cells
        self.assertEqual(10, resolver(101, cells[0]))
        self.assertEqual(90, resolver(101, cells[1]))
        self.assertEqual(80, resolver(103, cells[1]))
        with self.assertRaisesRegex(
            ContinuousRouteReplayV1Error, "no explicit paired shift"
        ):
            resolver(107, cells[1])

    def test_edge_dependent_and_multi_package_timing_fail_closed(self):
        route = ("wave-1", "wave-2", "boss")
        edge_dependent = RouteTimingShiftProfileV1(
            route_case_id="route",
            source_encounter_id="wave-1",
            package_id="fast-a",
            route_encounter_ids=route,
            downstream=(
                DownstreamStartShiftBoundsV1("wave-2", 1, ((101, -10),)),
                DownstreamStartShiftBoundsV1("boss", 2, ((101, 0),)),
            ),
        )
        with self.assertRaisesRegex(ValueError, "edge-dependent"):
            ExplicitPerSeedRouteStartResolverV1(
                expected_route_case_id="route",
                route_encounter_ids=route,
                baseline_start_ms=(0, 100, 200),
                profiles=(edge_dependent,),
            )

        first = replace(
            edge_dependent,
            downstream=(
                DownstreamStartShiftBoundsV1("wave-2", 1, ((101, -10),)),
                DownstreamStartShiftBoundsV1("boss", 2, ((101, -10),)),
            ),
        )
        second = RouteTimingShiftProfileV1(
            route_case_id="route",
            source_encounter_id="wave-2",
            package_id="fast-b",
            route_encounter_ids=route,
            downstream=(
                DownstreamStartShiftBoundsV1("boss", 2, ((101, -5),)),
            ),
        )
        with self.assertRaisesRegex(ValueError, "multi-package"):
            ExplicitPerSeedRouteStartResolverV1(
                expected_route_case_id="route",
                route_encounter_ids=route,
                baseline_start_ms=(0, 100, 200),
                profiles=(first, second),
            )

    def test_timing_observation_uses_resolved_pull_starts_and_accepted_package(self):
        candidate_lane = _lane(lane_id="candidate")
        baseline_lane = _lane(
            expected=False,
            cast_dw=False,
            lane_id="baseline",
        )
        candidate, _ = _native(
            candidate_lane,
            cell_start_resolver=lambda seed, cell: (
                90 if cell.encounter_id == "wave-2" else cell.raid_start_ms
            ),
        )
        baseline, _ = _native(baseline_lane)
        candidate_outcome = candidate.replay(101)
        baseline_outcome = baseline.replay(101)
        observation = paired_route_timing_observation_from_outcomes_v1(
            baseline_outcome,
            candidate_outcome,
            route_case_id="upper-kara/route-1/build-a",
            source_encounter_id="wave-1",
            package_id="loadout::resources__death_wish",
        )
        self.assertEqual((10, 100), tuple(
            row.start_ms for row in observation.baseline_starts
        ))
        self.assertEqual((10, 90), tuple(
            row.start_ms for row in observation.candidate_starts
        ))
        missing_accept = replace(
            candidate_outcome,
            cells=(
                replace(
                    candidate_outcome.cells[0],
                    accepted_cooldown_uses=(),
                ),
                candidate_outcome.cells[1],
            ),
        )
        with self.assertRaisesRegex(ValueError, "no actually accepted"):
            paired_route_timing_observation_from_outcomes_v1(
                baseline_outcome,
                missing_accept,
                route_case_id="upper-kara/route-1/build-a",
                source_encounter_id="wave-1",
                package_id="loadout::resources__death_wish",
            )

    def test_one_load_carries_player_state_and_verifies_accepted_cooldown(self):
        replay, bridges = _native(_lane())
        outcome = replay.replay(101)

        self.assertEqual(ContinuousRouteReplayStatusV1.COMPLETE, outcome.status)
        self.assertEqual(1, len(bridges))
        self.assertEqual(1, bridges[0].load_calls)
        self.assertEqual(1, bridges[0].close_calls)
        self.assertEqual(1, len(outcome.accepted_cooldown_uses))
        use = outcome.accepted_cooldown_uses[0]
        self.assertEqual("warrior.death_wish", use.resource_id)
        self.assertEqual(10, use.route_time_ms)
        self.assertEqual(180_000, use.ready_in_ms_after_accept)

        first_exit = outcome.cells[0].exit_state
        second_entry = outcome.cells[1].entry_state
        self.assertEqual(60.0, first_exit.rage_current)
        self.assertEqual(60.0, second_entry.rage_current)
        self.assertEqual({"kind": "CLEAVE", "status": "PENDING"}, second_entry.swing_queue)
        self.assertEqual(310, second_entry.oh_swing_remaining_ms)
        self.assertEqual(
            ["Berserker Stance", "Death Wish"],
            [row["label"] for row in second_entry.self_auras_and_procs],
        )
        death_wish_cd = next(
            row for row in second_entry.cooldowns
            if row["action"] == DW.to_wire()
        )
        self.assertEqual(179_910, death_wish_cd["ready_in_ms"])
        self.assertEqual(
            (("slot_14", 19_019), ("slot_15", 19_363)),
            second_entry.static_equipment,
        )
        self.assertEqual(
            {7}, {row.environment_generation for row in outcome.state_witnesses}
        )

    def test_missing_planned_cooldown_use_fails_closed(self):
        replay, _ = _native(_lane(expected=True, cast_dw=False))
        outcome = replay.replay(101)
        self.assertEqual(ContinuousRouteReplayStatusV1.INVALID, outcome.status)
        self.assertIn("differ from the frozen planner", outcome.invalid_reason)

    def test_unplanned_bound_cooldown_use_fails_closed(self):
        replay, _ = _native(_lane(expected=False, cast_dw=True, lane_id="baseline"))
        outcome = replay.replay(101)
        self.assertEqual(ContinuousRouteReplayStatusV1.INVALID, outcome.status)
        self.assertIn("differ from the frozen planner", outcome.invalid_reason)

    def test_accepted_action_without_native_cooldown_start_fails_closed(self):
        replay, _ = _native(_lane(), cooldown_starts=False)
        outcome = replay.replay(101)
        self.assertEqual(ContinuousRouteReplayStatusV1.INVALID, outcome.status)
        self.assertIn("did not start its full native clock", outcome.invalid_reason)

    def test_short_lock_does_not_count_as_the_planned_long_cooldown(self):
        replay, _ = _native(_lane(), cooldown_after_accept_ms=1_500)
        outcome = replay.replay(101)
        self.assertEqual(ContinuousRouteReplayStatusV1.INVALID, outcome.status)
        self.assertIn("ready_in_ms=1500", outcome.invalid_reason)

    def test_missing_queue_checkpoint_field_fails_closed(self):
        replay, _ = _native(_lane(), omit_queue=True)
        outcome = replay.replay(101)
        self.assertEqual(ContinuousRouteReplayStatusV1.INVALID, outcome.status)
        self.assertIn("swing_queue", outcome.invalid_reason)

    def test_runtime_weapon_swap_is_rejected_before_replay(self):
        with self.assertRaisesRegex(
            ContinuousRouteReplayV1Error, "runtime equipment/weapon swaps"
        ):
            FrozenContinuousRouteCellV1(
                encounter_id="wave-1",
                raid_start_ms=10,
                pull_time_ms=10,
                schedule=(ScheduledActionPlan(
                    at_or_after_ms=10,
                    equipment_action=EquipmentAction(
                        "main_hand", 19_363, True
                    ),
                    wait_ms=1,
                ),),
            )

    def test_paired_eval_uses_terminal_route_damage_and_never_cell_sums(self):
        candidate_lane = _lane(lane_id="candidate")
        baseline_lane = _lane(
            expected=False,
            cast_dw=False,
            lane_id="baseline",
        )
        candidate, candidate_bridges = _native(
            candidate_lane, damage=250.0
        )
        baseline, baseline_bridges = _native(
            baseline_lane, damage=200.0
        )

        result = evaluate_continuous_held_out_route_v1(
            candidate,
            baseline,
            evaluation_seeds=(101, 103),
        )
        self.assertEqual(
            "COMPLETE_CONTINUOUS_HELD_OUT_ROUTE_PAIRED_REPLAY",
            result["status"],
        )
        self.assertEqual(
            50.0,
            result["summary"][
                "mean_candidate_minus_baseline_route_effective_damage"
            ],
        )
        self.assertTrue(
            result["contract"]["independent_cell_damage_not_summed"]
        )
        self.assertEqual(2, len(candidate_bridges))
        self.assertEqual(2, len(baseline_bridges))
        self.assertTrue(all(row.load_calls == 1 for row in candidate_bridges))
        self.assertTrue(all(row.load_calls == 1 for row in baseline_bridges))

    def test_paired_eval_rejects_training_seed_overlap(self):
        candidate_lane = _lane(lane_id="candidate")
        baseline_lane = _lane(
            expected=False,
            cast_dw=False,
            lane_id="baseline",
        )
        candidate, _ = _native(candidate_lane)
        baseline, _ = _native(baseline_lane)
        with self.assertRaisesRegex(ValueError, "overlap"):
            evaluate_continuous_held_out_route_v1(
                candidate,
                baseline,
                evaluation_seeds=(11,),
            )

    def test_paired_eval_rejects_different_dynamic_environment_configs(self):
        candidate, _ = _native(_lane(lane_id="candidate"), digest="candidate")
        baseline, _ = _native(
            _lane(expected=False, cast_dw=False, lane_id="baseline"),
            digest="baseline",
        )
        with self.assertRaisesRegex(
            ContinuousRouteReplayV1Error, "different dynamic environment configs"
        ):
            evaluate_continuous_held_out_route_v1(
                candidate,
                baseline,
                evaluation_seeds=(101,),
            )


if __name__ == "__main__":
    unittest.main()
