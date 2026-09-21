from dataclasses import replace
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.raid_cooldown_schedule_v1 import (
    RoutePersistentEffectV1,
    RoutePersistentStatPhaseV1,
)
from o2o_dps.wave_action_schedule_v1 import ScheduledActionPlan
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)
from o2o_dps.wave_cooldown_package_measurement_v1 import (
    CooldownActionBindingV1,
    measure_cooldown_package_from_outcomes_v1,
    measure_cooldown_package_v1,
)


DEATH_WISH = ActionRef(spell_id=12328)
HIT = ActionRef(spell_id=23894)


class _Replay:
    def __init__(self, *, incomplete_seed=None, omit_burst_seed=None):
        self.incomplete_seed = incomplete_seed
        self.omit_burst_seed = omit_burst_seed

    def replay(self, seed, schedule):
        burst = any(step.gcd_action == DEATH_WISH for step in schedule)
        status = (
            ReplayStatusV1.FRONTIER
            if seed == self.incomplete_seed and burst
            else ReplayStatusV1.COMPLETE
        )
        receipts = ()
        if burst and seed != self.omit_burst_seed:
            receipts = ({
                "kind": "ACT_GCD",
                "accepted": True,
                "action": DEATH_WISH.to_wire(),
                "state_time_before_ms": 100 if seed == 11 else 120,
            },)
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=status,
            state={
                "time_ms": 4_000 if burst else 4_200,
                "damage_done": 120.0 + seed if burst else 100.0 + seed,
                "precombat": {"pull_time_ms": 3_000, "relative_time_ms": 1_000},
            },
            available_actions=(
                ()
                if status is ReplayStatusV1.COMPLETE
                else (AvailableAction(0, HIT, "hit", True, 0, True),)
            ),
            receipts=receipts,
        )


class _VariableElapsedReplay(_Replay):
    def replay(self, seed, schedule):
        outcome = super().replay(seed, schedule)
        burst = any(step.gcd_action == DEATH_WISH for step in schedule)
        if burst:
            state = dict(outcome.state)
            state["time_ms"] = 3_900 if seed == 11 else 4_100
            return replace(outcome, state=state)
        return outcome


class WaveCooldownPackageMeasurementV1Tests(unittest.TestCase):
    def setUp(self):
        self.baseline = (ScheduledActionPlan(0, gcd_action=HIT),)
        self.candidate = (
            ScheduledActionPlan(0, gcd_action=DEATH_WISH),
            ScheduledActionPlan(1_500, gcd_action=HIT),
        )
        self.binding = CooldownActionBindingV1(
            action=DEATH_WISH,
            resource_id="warrior.death_wish",
            cooldown_group="warrior.death_wish",
            cooldown_ms=180_000,
            action_kind="SPELL",
        )

    def test_complete_same_seed_replays_emit_a_route_package(self):
        result = measure_cooldown_package_v1(
            _Replay(),
            package_id="prepull-death-wish",
            baseline_schedule=self.baseline,
            candidate_schedule=self.candidate,
            seeds=(11, 13),
            bindings=(self.binding,),
        )
        self.assertEqual(result.package.marginal_value, 20.0)
        self.assertEqual(len(result.package.uses), 1)
        # Latest observed use is conservative for the next route availability.
        self.assertEqual(result.package.uses[0].use_at_ms, -2_880)
        self.assertEqual(result.package.uses[0].use_window_start_ms, -2_900)
        self.assertEqual(result.observed_use_times_by_seed, ((-2_900,), (-2_880,)))
        self.assertEqual(result.package.encounter_elapsed_ms_delta_min, -200)
        self.assertEqual(result.package.encounter_elapsed_ms_delta_max, -200)
        self.assertEqual(result.to_dict()["paired_seed_count"], 2)

    def test_incomplete_side_cannot_become_planner_evidence(self):
        with self.assertRaisesRegex(ValueError, "not complete"):
            measure_cooldown_package_v1(
                _Replay(incomplete_seed=13),
                package_id="bad",
                baseline_schedule=self.baseline,
                candidate_schedule=self.candidate,
                seeds=(11, 13),
                bindings=(self.binding,),
            )

    def test_seed_dependent_resource_use_is_not_one_package(self):
        with self.assertRaisesRegex(ValueError, "differ across paired seeds"):
            measure_cooldown_package_v1(
                _Replay(omit_burst_seed=13),
                package_id="unstable",
                baseline_schedule=self.baseline,
                candidate_schedule=self.candidate,
                seeds=(11, 13),
                bindings=(self.binding,),
            )

    def test_existing_terminal_outcomes_are_reused_without_replay(self):
        replay = _Replay()
        baseline = tuple(replay.replay(seed, self.baseline) for seed in (11, 13))
        candidate = tuple(replay.replay(seed, self.candidate) for seed in (11, 13))
        result = measure_cooldown_package_from_outcomes_v1(
            package_id="cached-death-wish",
            baseline_outcomes=baseline,
            candidate_outcomes=candidate,
            bindings=(self.binding,),
        )
        self.assertEqual(result.package.marginal_value, 20.0)
        self.assertEqual(result.package.uses[0].use_at_ms, -2_880)

    def test_elapsed_delta_range_is_preserved_without_mean_rounding(self):
        result = measure_cooldown_package_v1(
            _VariableElapsedReplay(),
            package_id="variable-kill-clock",
            baseline_schedule=self.baseline,
            candidate_schedule=self.candidate,
            seeds=(11, 13),
            bindings=(self.binding,),
        )
        self.assertEqual(result.package.encounter_elapsed_ms_delta_min, -300)
        self.assertEqual(result.package.encounter_elapsed_ms_delta_max, -100)
        payload = result.to_dict()
        self.assertEqual(
            payload["encounter_elapsed_ms_delta_bounds"], [-300, -100]
        )
        self.assertEqual(
            payload["route_allocation_status"],
            "REQUIRES_EXPLICIT_CALIBRATED_ROUTE_TIME_SHIFT",
        )

    def test_persistent_effect_is_preserved_and_isolated_scope_is_explicit(self):
        effect = RoutePersistentEffectV1(
            "rapid-growth",
            (RoutePersistentStatPhaseV1(
                "growth", 0, 120_000, (("strength", 30.0),)
            ),),
        )
        binding = CooldownActionBindingV1(
            action=DEATH_WISH,
            resource_id="rapid-growth",
            cooldown_group="rapid-growth",
            cooldown_ms=120_000,
            action_kind="CONSUMABLE",
            persistent_effect=effect,
        )
        result = measure_cooldown_package_v1(
            _Replay(),
            package_id="persistent",
            baseline_schedule=self.baseline,
            candidate_schedule=self.candidate,
            seeds=(11, 13),
            bindings=(binding,),
        )
        self.assertEqual(result.package.uses[0].persistent_effect, effect)
        payload = result.to_dict()
        self.assertEqual(payload["objective_scope"], "ISOLATED_ENCOUNTER")
        self.assertEqual(
            payload["route_allocation_status"],
            "REQUIRES_CROSS_ENCOUNTER_STATE_AWARE_REPLAY",
        )


if __name__ == "__main__":
    unittest.main()
