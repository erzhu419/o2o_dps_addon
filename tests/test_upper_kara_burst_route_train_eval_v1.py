from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1
from o2o_dps.contra_turtle_burst_loadout_v1 import (
    ContraTurtleBurstLoadoutV1,
)
from o2o_dps.raid_cooldown_schedule_v1 import (
    TRASH,
    CooldownPackageV1,
    CooldownUseV1,
    EncounterCooldownCellV1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_burst_package_search_v1 import (
    BURST_LOADOUT_IDS_V1,
    UpperKaraBurstPackageSearchResultV1,
)
from o2o_dps.upper_kara_burst_route_train_eval_v1 import (
    aggregate_upper_kara_burst_loadouts_v1,
    allocate_and_freeze_upper_kara_burst_route_v1,
    evaluate_frozen_upper_kara_burst_route_v1,
)
from o2o_dps.wave_action_schedule_v1 import (
    EquipmentAction,
    ScheduledActionPlan,
    SearchCellIdentity,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    WaveActionSequenceSearchResultV1,
)
from o2o_dps.wave_cooldown_package_measurement_v1 import (
    MeasuredCooldownPackageV1,
    PairedScheduleDeltaV1,
)
from o2o_dps.wave_cooldown_package_search_v1 import (
    CooldownResourceArmResultV1,
    CooldownResourceSearchArmV1,
    WaveCooldownPackageSearchResultV1,
)


BT = ActionRef(spell_id=23_894)
DW = ActionRef(spell_id=12_328)
GUARD = ObservableCausalGuardV1(
    target_index=1,
    target_hp_pct_lte=63.0,
)
TRAIN_SEEDS = (11, 13)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _cell(loadout_id: str, *, attackability: str = "full_wave"):
    consumes = {"loadout": loadout_id}
    return SearchCellIdentity(
        scenario_id="Upper Tower of Karazhan:instance",
        wave_or_boss_id="wave-7",
        exact_build_id="exact-fury-build",
        talents=(("talent_01", 5),),
        equipment=(("slot_14", 19019), ("slot_15", 19363)),
        derived_mechanics=(
            ("starting_rage", 30),
            ("target_count", 3),
            ("initial_state_json", _json({"rage": 30, "consumes": consumes})),
            (
                "exact_build_context_json",
                _json({"talents": "fury", "consumes": consumes}),
            ),
        ),
        environment_branch_id=_json({
            "stratum": "q05",
            "attackability_branch": attackability,
            "team_background_model": "reactive-v1",
            "precombat": {
                "pull_time_ms": 3_000,
                "self_actions": [{"spell_id": 12_328}],
                "loadout_id": loadout_id,
            },
        }),
    )


def _state(damage: float = 100.0):
    return {
        "time_ms": 5_000,
        "damage_done": damage,
        "finished": True,
        "precombat": {
            "pull_time_ms": 3_000,
            "relative_time_ms": 2_000,
        },
    }


def _search(cell, schedule, damage=100.0):
    return WaveActionSequenceSearchResultV1(
        cell=cell,
        status="COMPLETE_PAIRED_SEED_SCHEDULE",
        schedule=tuple(schedule),
        outcomes=tuple(
            ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.COMPLETE,
                state=_state(damage + index),
            )
            for index, seed in enumerate(TRAIN_SEEDS)
        ),
        mean_dps=20.0,
        search_ranking_score=100.0,
        search_ranking_metric="fixture",
        evaluated_schedule_count=4,
        completed_depth=len(schedule),
        guide_ids=("contra:fixture",),
        replay_workers=1,
    )


def _plan(*, target: int, precombat: bool, guide: str):
    return ScheduledActionPlan(
        at_or_after_ms=1_000 if precombat else 3_500,
        target_index=target,
        equipment_action=EquipmentAction("main_hand", 19363, True),
        off_gcd_actions=(DW,),
        gcd_action=BT,
        guard=GUARD,
        guide_provenance=(guide,),
        guide_priority=88.0,
    )


def _result(
    loadout_id: str,
    *,
    encounter_id: str,
    raid_start_ms: int,
    marginal: float,
    attackability: str = "full_wave",
    resource_id: str = "warrior.death_wish",
    cooldown_group: str = "death_wish",
):
    cell = _cell(loadout_id, attackability=attackability)
    baseline = _search(
        cell,
        (_plan(target=0, precombat=False, guide="baseline-guide"),),
        90.0,
    )
    source_package = CooldownPackageV1(
        package_id="resources__death_wish",
        uses=(CooldownUseV1(
            resource_id=resource_id,
            cooldown_group=cooldown_group,
            cooldown_ms=120_000 if resource_id == "item.elixir_of_rapid_growth" else 180_000,
            use_at_ms=-2_000,
            action_kind="SPELL",
        ),),
        marginal_value=marginal,
    )
    arm_search = _search(
        cell,
        (_plan(target=1, precombat=True, guide=f"{loadout_id}-guide"),),
        110.0,
    )
    measurement = MeasuredCooldownPackageV1(
        package=source_package,
        paired_rows=tuple(
            PairedScheduleDeltaV1(
                seed=seed,
                baseline_effective_damage=90.0 + index,
                candidate_effective_damage=110.0 + index,
                baseline_elapsed_ms=5_000,
                candidate_elapsed_ms=5_000,
            )
            for index, seed in enumerate(TRAIN_SEEDS)
        ),
        objective="OWN_EFFECTIVE_DAMAGE",
        observed_use_times_by_seed=((-2_000,), (-2_000,)),
    )
    arm = CooldownResourceArmResultV1(
        arm=CooldownResourceSearchArmV1(
            "resources__death_wish", (resource_id,)
        ),
        status="PAIRED_MEASURED_PLANNER_ELIGIBLE",
        search=arm_search,
        measurement=measurement,
    )
    package_search = WaveCooldownPackageSearchResultV1(
        cell=cell,
        baseline_search=baseline,
        arms=(arm,),
        planner_cell=EncounterCooldownCellV1(
            encounter_id=encounter_id,
            encounter_kind=TRASH,
            raid_start_ms=raid_start_ms,
            packages=(source_package,),
        ),
    )
    loadout = ContraTurtleBurstLoadoutV1(
        loadout_id=loadout_id,
        player_consumes={"loadout": loadout_id},
        precombat_self_actions=(DW,),
        potion_resource_id=None,
        modeled_source_action_ids=("warrior.death_wish",),
        unsupported_source_action_ids=(),
    )
    return UpperKaraBurstPackageSearchResultV1(
        representative_rank=7,
        stratum="q05",
        loadout=loadout,
        first_seed=TRAIN_SEEDS[0],
        search_cell=cell,
        native_snapshot=(),
        projection=None,
        native_resolution=None,
        resource_selection=None,
        package_search=package_search,
        guide_ids=("contra:fixture",),
        snapshot_authority="FIXTURE",
        replay_authority="FIXTURE",
    )


def _encounter_results(
    encounter_id,
    raid_start_ms,
    marginals=None,
    *,
    resource_id="warrior.death_wish",
    cooldown_group="death_wish",
):
    values = marginals or {}
    return tuple(
        _result(
            loadout_id,
            encounter_id=encounter_id,
            raid_start_ms=raid_start_ms,
            marginal=values.get(loadout_id, 1.0),
            resource_id=resource_id,
            cooldown_group=cooldown_group,
        )
        for loadout_id in BURST_LOADOUT_IDS_V1
    )


class _Replay:
    def __init__(self, damage: float, *, invalid: bool = False):
        self.damage = damage
        self.invalid = invalid
        self.calls = []

    def replay(self, seed, schedule):
        self.calls.append((seed, tuple(schedule)))
        if self.invalid:
            return ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.INVALID,
                state={"time_ms": 100, "damage_done": 0.0},
                invalid_reason="fixture invalid lane",
            )
        return ScheduleReplayOutcomeV1(
            seed=seed,
            status=ReplayStatusV1.COMPLETE,
            state=_state(self.damage),
        )


class UpperKaraBurstRouteTrainEvalV1Tests(unittest.TestCase):
    def test_namespaces_colliding_arm_ids_and_freezes_complete_selected_table(self):
        marginals = {
            "contra_turtle_burst__no_potion": 3.0,
            "contra_turtle_burst__mighty_rage": 20.0,
            "contra_turtle_burst__rage": 8.0,
            "contra_turtle_burst__quickness": 9.0,
        }
        aggregate = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results("wave-a", 60_000, marginals)
        )
        package_ids = [
            row.package.package_id for row in aggregate.package_schedules
        ]
        self.assertEqual(4, len(package_ids))
        self.assertEqual(4, len(set(package_ids)))
        self.assertTrue(all("::resources__death_wish" in row for row in package_ids))

        plan = allocate_and_freeze_upper_kara_burst_route_v1((aggregate,))
        selected = plan.encounters[0]
        self.assertEqual(
            "contra_turtle_burst__mighty_rage", selected.loadout_id
        )
        self.assertEqual("PLANNER_SELECTED_RESOURCE_ARM", selected.source_kind)
        step = selected.schedule[0]
        self.assertEqual(1, step.target_index)
        self.assertEqual(DW, step.off_gcd_actions[0])
        self.assertEqual(GUARD, step.guard)
        self.assertEqual(EquipmentAction("main_hand", 19363, True), step.equipment_action)
        self.assertEqual((), step.guide_provenance)
        self.assertEqual(0.0, step.guide_priority)
        wire = selected.to_dict()
        self.assertTrue(wire["steps"][0]["precombat"])
        self.assertEqual(-2_000, wire["steps"][0]["relative_to_pull_ms"])
        self.assertNotIn("guide", wire["steps"][0])

    def test_unassigned_encounter_uses_no_resource_no_potion_baseline(self):
        aggregate = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results(
                "wave-negative",
                60_000,
                {loadout_id: -1.0 for loadout_id in BURST_LOADOUT_IDS_V1},
            )
        )
        plan = allocate_and_freeze_upper_kara_burst_route_v1((aggregate,))
        selected = plan.encounters[0]
        self.assertEqual("NO_RESOURCE_NO_POTION_BASELINE", selected.source_kind)
        self.assertEqual(
            "contra_turtle_burst__no_potion", selected.loadout_id
        )
        self.assertIsNone(selected.namespaced_package_id)
        self.assertEqual(0, selected.schedule[0].target_index)

    def test_rejects_non_loadout_core_cell_difference(self):
        rows = list(_encounter_results("wave-a", 60_000))
        rows[-1] = _result(
            BURST_LOADOUT_IDS_V1[-1],
            encounter_id="wave-a",
            raid_start_ms=60_000,
            marginal=1.0,
            attackability="delayed",
        )
        with self.assertRaisesRegex(ValueError, "outside consumes/precombat"):
            aggregate_upper_kara_burst_loadouts_v1(rows)

    def test_held_out_replay_is_paired_and_does_not_search(self):
        marginals = {
            "contra_turtle_burst__mighty_rage": 20.0,
        }
        aggregate = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results("wave-a", 60_000, marginals)
        )
        plan = allocate_and_freeze_upper_kara_burst_route_v1((aggregate,))
        candidate = _Replay(140.0)
        baseline = _Replay(100.0)
        payload = evaluate_frozen_upper_kara_burst_route_v1(
            plan,
            evaluation_seeds=(101, 103),
            replay_by_encounter_loadout={
                (
                    "wave-a",
                    "contra_turtle_burst__mighty_rage",
                ): candidate,
                ("wave-a", "contra_turtle_burst__no_potion"): baseline,
            },
        )
        self.assertEqual("COMPLETE_HELD_OUT_ROUTE_PAIRED_REPLAY", payload["status"])
        self.assertEqual(40.0, payload["summary"]["mean_candidate_minus_baseline_route_effective_damage"])
        self.assertEqual(2, len(candidate.calls))
        self.assertEqual(2, len(baseline.calls))
        for _, schedule in candidate.calls:
            self.assertEqual((), schedule[0].guide_provenance)
        self.assertFalse(payload["contract"]["search_or_guides_invoked_on_evaluation_seeds"])

    def test_failed_held_out_lane_is_incomplete_and_never_zero_filled(self):
        aggregate = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results(
                "wave-a",
                60_000,
                {"contra_turtle_burst__mighty_rage": 20.0},
            )
        )
        plan = allocate_and_freeze_upper_kara_burst_route_v1((aggregate,))
        payload = evaluate_frozen_upper_kara_burst_route_v1(
            plan,
            evaluation_seeds=(101,),
            replay_by_encounter_loadout={
                ("wave-a", "contra_turtle_burst__mighty_rage"): _Replay(
                    0.0, invalid=True
                ),
                ("wave-a", "contra_turtle_burst__no_potion"): _Replay(100.0),
            },
        )
        self.assertEqual("INCOMPLETE_HELD_OUT_ROUTE_PAIRED_REPLAY", payload["status"])
        row = payload["seed_rows"][0]["encounters"][0]
        self.assertIsNone(row["candidate"]["effective_damage"])
        self.assertIsNone(row["candidate_minus_baseline_effective_damage"])
        self.assertIsNone(payload["summary"]["mean_candidate_minus_baseline_route_effective_damage"])
        self.assertFalse(payload["contract"]["failed_lane_scored_as_zero"])

    def test_rejects_training_evaluation_seed_overlap_before_replay(self):
        aggregate = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results("wave-a", 60_000)
        )
        plan = allocate_and_freeze_upper_kara_burst_route_v1((aggregate,))
        with self.assertRaisesRegex(ValueError, "overlap"):
            evaluate_frozen_upper_kara_burst_route_v1(
                plan,
                evaluation_seeds=(TRAIN_SEEDS[0],),
                replay_by_encounter_loadout={},
            )

    def test_rapid_growth_cross_wave_carry_blocks_isolated_route_claim(self):
        first = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results(
                "wave-a",
                60_000,
                {"contra_turtle_burst__mighty_rage": 20.0},
                resource_id="item.elixir_of_rapid_growth",
                cooldown_group="item.elixir_of_rapid_growth",
            )
        )
        second = aggregate_upper_kara_burst_loadouts_v1(
            _encounter_results(
                "wave-b",
                100_000,
                {loadout_id: -1.0 for loadout_id in BURST_LOADOUT_IDS_V1},
            )
        )
        plan = allocate_and_freeze_upper_kara_burst_route_v1((first, second))
        self.assertFalse(plan.route_persistent_effects_resolved)
        positive = next(
            row for row in plan.persistent_effect_windows
            if row.effect_id == "RAPID_GROWTH_POSITIVE"
        )
        self.assertEqual(("wave-b",), positive.affected_later_encounter_ids)
        self.assertEqual((("strength", 30),), positive.stat_deltas)

        candidate = _Replay(140.0)
        payload = evaluate_frozen_upper_kara_burst_route_v1(
            plan,
            evaluation_seeds=(101,),
            replay_by_encounter_loadout={
                ("wave-a", "contra_turtle_burst__mighty_rage"): candidate,
            },
        )
        self.assertEqual(
            "INCOMPLETE_ROUTE_PERSISTENT_EFFECT_CARRY_UNMODELED",
            payload["status"],
        )
        self.assertEqual([], payload["seed_rows"])
        self.assertIsNone(
            payload["summary"][
                "mean_candidate_minus_baseline_route_effective_damage"
            ]
        )
        self.assertEqual([], candidate.calls)
        self.assertFalse(
            payload["contract"][
                "isolated_wave_rapid_growth_marginal_accepted_as_route_result"
            ]
        )


if __name__ == "__main__":
    unittest.main()
