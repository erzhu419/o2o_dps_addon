from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.causal_action_program_v1 import (
    OrderedGuardSelectorV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import GOBLIN_SAPPER_ACTION
from o2o_dps.causal_guard_v1 import evaluate_observable_guard_v1
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_causal_program_search_v1 import (
    CallableProgramProposalGuideV1,
    UpperKaraCausalProgramGeneratorV1,
    UpperKaraCausalProgramGenerationConfigV1,
    SUNDER_ARMOR_ACTION_V1,
    SUNDER_ARMOR_MAX_STACKS_V1,
    adapt_wave_action_guides_for_program_proposals_v1,
    build_upper_kara_causal_program_candidate_set_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1
from o2o_dps.wave_action_guides_v1 import StaticActionGuideV1
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


BT = ActionRef(spell_id=23_894)
WW = ActionRef(spell_id=1_680)
EXECUTE = ActionRef(spell_id=20_662)
SUNDER = SUNDER_ARMOR_ACTION_V1
HS = ActionRef(spell_id=25_286, tag=1)
CLEAVE = ActionRef(spell_id=20_569, tag=1)
UNKNOWN_GUIDE_ACTION = ActionRef(spell_id=999_999)
BLOODRAGE = ActionRef(spell_id=2_687)
STANCE_ACTIONS = {
    ActionRef(spell_id=71),
    ActionRef(spell_id=2_457),
    ActionRef(spell_id=2_458),
}


def _available(index, action, *, gcd, legal=False):
    return AvailableAction(
        index=index,
        action=action,
        label=str(action.to_wire()),
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=gcd,
        result_bearing=gcd,
    )


class UpperKaraCausalProgramSearchV1Tests(unittest.TestCase):
    _cached_unguided_result = None

    def setUp(self):
        self.loadout_id = "contra_turtle_burst__quickness"
        self.examples = (TwoWaveExampleV1(101, 0), TwoWaveExampleV1(103, 7_000))
        self.cases = tuple(
            build_upper_kara_continuous_two_wave_burst_case_v1(
                example.seed,
                build_id="clean_dual_weapon_probe",
                loadout_id=self.loadout_id,
                first_wave_arrival_ms=example.first_wave_arrival_ms,
            )
            for example in self.examples
        )
        burst_actions = tuple(
            sorted({*self.cases[0].precombat.self_actions, GOBLIN_SAPPER_ACTION})
        )
        rows = [
            _available(0, BT, gcd=True),
            _available(1, WW, gcd=True),
            _available(2, EXECUTE, gcd=True),
            _available(3, SUNDER, gcd=True),
            _available(4, HS, gcd=False),
            _available(5, CLEAVE, gcd=False),
        ]
        for action in burst_actions:
            rows.append(
                _available(
                    len(rows),
                    action,
                    gcd=action.spell_id in {12_328, 1_719},
                )
            )
        # One genuine native off-GCD action outside the requested queue/burst
        # axes remains explicit in the audit instead of silently entering them.
        rows.append(_available(len(rows), BLOODRAGE, gcd=False))
        for action in sorted(STANCE_ACTIONS):
            rows.append(_available(len(rows), action, gcd=False))
        self.snapshot = tuple(rows)
        self.config = UpperKaraCausalProgramGenerationConfigV1(
            max_programs=320,
            max_gcd_priority_orders=3,
            max_gcd_order_assignments=6,
            max_burst_priority_orders=3,
            max_burst_order_assignments=6,
            max_burst_allocations=128,
            max_ordinary_off_gcd_priority_orders=2,
            max_ordinary_off_gcd_order_assignments=2,
            max_ordinary_off_gcd_allocations=40,
            hp_thresholds=(20, 35, 50, 80),
            queue_rage_thresholds=(30, 60),
            ordinary_off_gcd_rage_lte_thresholds=(0, 40, 60),
        )

    def _build(self, *, guides=()):
        if not guides and self.__class__._cached_unguided_result is not None:
            return self.__class__._cached_unguided_result
        result = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
            config=self.config,
            snapshot_loader=lambda _: self.snapshot,
            proposal_guides=guides,
        )
        if not guides:
            self.__class__._cached_unguided_result = result
        return result

    @staticmethod
    def _actions(program, prefix, target):
        return tuple(
            alternative.decision.gcd_action
            for alternative in program.selector.alternatives
            if alternative.alternative_id.startswith(f"{prefix}:t{target}:")
            and alternative.decision.gcd_action is not None
        )

    def test_target_specific_gcd_priority_permutations_are_proposed(self):
        result = self._build()
        self.assertTrue(
            all(isinstance(program.selector, OrderedGuardSelectorV1) for program in result.programs)
        )
        target0_orders = {
            self._actions(program, "gcd", 0) for program in result.programs
        }
        self.assertGreater(len(target0_orders), 1)
        self.assertTrue(
            any(
                self._actions(program, "gcd", 0)
                != self._actions(program, "gcd", 1)
                for program in result.programs
            )
        )
        for program in result.programs:
            for target in (0, 1):
                self.assertEqual(
                    set(result.gcd_actions),
                    set(self._actions(program, "gcd", target)),
                )

    def test_sunder_gcd_is_guarded_below_real_five_stack_cap_only(self):
        result = self._build()
        sunder_rows = []
        ordinary_rows = []
        for program in result.programs:
            for alternative in program.selector.alternatives:
                if not alternative.alternative_id.startswith("gcd:"):
                    continue
                if alternative.decision.gcd_action == SUNDER:
                    sunder_rows.append(alternative)
                else:
                    ordinary_rows.append(alternative)

        self.assertTrue(sunder_rows)
        self.assertTrue(ordinary_rows)
        self.assertTrue(
            all(row.guard.target_aura_action == SUNDER for row in sunder_rows)
        )
        self.assertTrue(
            all(
                row.guard.target_aura_stacks_lte
                == SUNDER_ARMOR_MAX_STACKS_V1 - 1
                for row in sunder_rows
            )
        )
        self.assertTrue(
            all(row.guard.target_aura_action is None for row in ordinary_rows)
        )
        self.assertTrue(
            all(row.guard.target_aura_stacks_lte is None for row in ordinary_rows)
        )
        self.assertTrue(
            result.to_dict()["contract"]["candidate_generation_axes"][
                "capped_target_debuff_refresh_suppression"
            ]
        )

    def test_burst_reschedule_is_target_specific_and_varies_hp_threshold(self):
        result = self._build()
        rapid = ActionRef(item_id=56_113)
        observed = {0: set(), 1: set()}
        for program in result.programs:
            for alternative in program.selector.alternatives:
                if not alternative.alternative_id.startswith("burst:"):
                    continue
                if alternative.guard.action_ready != rapid:
                    continue
                target = alternative.guard.target_index
                observed[target].add(alternative.guard.target_hp_pct_gte)
                self.assertTrue(alternative.guard.target_attackable_is)
                self.assertIsNone(alternative.guard.target_aura_action)
                self.assertIsNone(alternative.guard.target_aura_stacks_lte)
                self.assertEqual("SKIP_PLAN", alternative.guard.false_semantics)
                self.assertEqual(target, alternative.decision.target_index)
        self.assertEqual({20.0, 35.0, 50.0, 80.0}, observed[0])
        self.assertEqual({20.0, 35.0, 50.0, 80.0}, observed[1])

    def test_each_burst_resource_can_skip_precombat_or_reschedule_across_waves(self):
        result = self._build()
        self.assertTrue(
            any(
                not any(
                    row.alternative_id.startswith(("burst:", "precombat:"))
                    for row in program.selector.alternatives
                )
                for program in result.programs
            )
        )
        precombat = [
            row
            for program in result.programs
            for row in program.selector.alternatives
            if row.alternative_id.startswith("precombat:")
        ]
        self.assertEqual(
            {-2_500, -1_500, -500},
            {row.guard.pull_relative_time_gte_ms for row in precombat},
        )
        self.assertTrue(
            all(row.guard.pull_relative_time_lte_ms == -1 for row in precombat)
        )
        self.assertTrue(
            all(row.guard.target_attackable_is is None for row in precombat)
        )
        self.assertTrue(
            all(row.guard.target_hp_pct_gte is None for row in precombat)
        )

        # One finite resource can first try target 0, then remain available for
        # target 1 when the first condition was false.  If target 0 consumed it,
        # action_ready naturally suppresses the later alternative.
        self.assertTrue(
            any(
                any(
                    first.guard.action_ready == second.guard.action_ready
                    and first.guard.target_index == 0
                    and second.guard.target_index == 1
                    for first, second in zip(
                        [
                            row for row in program.selector.alternatives
                            if row.alternative_id.startswith("burst:")
                        ],
                        [
                            row for row in program.selector.alternatives
                            if row.alternative_id.startswith("burst:")
                        ][1:],
                    )
                )
                for program in result.programs
            )
        )

        # Distinct finite resources may be allocated exclusively to different
        # waves instead of forcing a whole-package target assignment.
        self.assertTrue(
            any(
                len({
                    row.guard.action_ready
                    for row in program.selector.alternatives
                    if row.alternative_id.startswith("burst:")
                    and row.guard.target_index == 0
                }) == 1
                and len({
                    row.guard.action_ready
                    for row in program.selector.alternatives
                    if row.alternative_id.startswith("burst:")
                    and row.guard.target_index == 1
                }) == 1
                and {
                    row.guard.action_ready
                    for row in program.selector.alternatives
                    if row.alternative_id.startswith("burst:")
                    and row.guard.target_index == 0
                } != {
                    row.guard.action_ready
                    for row in program.selector.alternatives
                    if row.alternative_id.startswith("burst:")
                    and row.guard.target_index == 1
                }
                for program in result.programs
            )
        )

    def test_queue_choice_and_rage_action_ready_conjunction_are_represented(self):
        result = self._build()
        queue_rows = [
            alternative
            for program in result.programs
            for alternative in program.selector.alternatives
            if alternative.alternative_id.startswith("queue:")
        ]
        self.assertEqual({HS, CLEAVE}, {row.guard.action_ready for row in queue_rows})
        self.assertEqual({30.0, 60.0}, {row.guard.rage_gte for row in queue_rows})
        self.assertTrue(all(row.guard.queue_status_is == "NONE" for row in queue_rows))
        self.assertTrue(all(row.decision.wait_ms == 1 for row in queue_rows))
        limitations = result.to_dict()["grammar_limitations"]
        self.assertEqual(
            "ATOMIC_QUEUE_AND_SEPARATELY_READY_GCD_NOT_REPRESENTABLE_V1",
            limitations[0]["code"],
        )
        self.assertFalse(limitations[0]["fabricated_combined_guard_used"])

    def test_bloodrage_is_an_independent_rage_capped_reschedule_axis(self):
        result = self._build()
        self.assertEqual((BLOODRAGE,), result.ordinary_off_gcd_actions)
        rows = [
            row
            for program in result.programs
            for row in program.selector.alternatives
            if row.alternative_id.startswith("ordinary-off-gcd:")
        ]
        self.assertEqual({BLOODRAGE}, {row.guard.action_ready for row in rows})
        self.assertEqual({0.0, 40.0, 60.0}, {row.guard.rage_lte for row in rows})
        self.assertEqual(
            {20.0, 35.0, 50.0, 80.0},
            {row.guard.target_hp_pct_gte for row in rows},
        )
        self.assertEqual({0, 1}, {row.guard.target_index for row in rows})
        self.assertTrue(all(row.guard.target_attackable_is is True for row in rows))
        self.assertTrue(all(row.decision.wait_ms == 1 for row in rows))
        self.assertTrue(
            any(
                not any(
                    row.alternative_id.startswith("ordinary-off-gcd:")
                    for row in program.selector.alternatives
                )
                for program in result.programs
            )
        )

        reschedule = next(
            pair
            for program in result.programs
            for pair in [tuple(
                row for row in program.selector.alternatives
                if row.alternative_id.startswith("ordinary-off-gcd:")
                and row.guard.target_hp_pct_gte == 35
                and row.guard.rage_lte == 40
            )]
            if len(pair) == 2
            and pair[0].guard.target_index == 0
            and pair[1].guard.target_index == 1
        )
        state = {
            "power": {"type": "rage", "current": 40.0, "maximum": 100.0},
            "dynamic_target_semantics": {"targets": [
                {
                    "target_index": 0,
                    "maximum_health": 100.0,
                    "current_health": 28.0,
                    "dead": False,
                    "attackable": True,
                },
                {
                    "target_index": 1,
                    "maximum_health": 100.0,
                    "current_health": 100.0,
                    "dead": False,
                    "attackable": True,
                },
            ]},
        }
        available = (_available(0, BLOODRAGE, gcd=False, legal=True),)
        self.assertEqual(
            ("target_hp_pct_gte",),
            evaluate_observable_guard_v1(
                reschedule[0].guard, state, available
            ).failed_predicates,
        )
        self.assertTrue(
            evaluate_observable_guard_v1(
                reschedule[1].guard, state, available
            ).satisfied
        )
        self.assertTrue(
            any(
                any(
                    first.guard.action_ready == BLOODRAGE
                    and second.guard.action_ready == BLOODRAGE
                    and first.guard.target_index == 0
                    and second.guard.target_index == 1
                    for first, second in zip(
                        [
                            row for row in program.selector.alternatives
                            if row.alternative_id.startswith("ordinary-off-gcd:")
                        ],
                        [
                            row for row in program.selector.alternatives
                            if row.alternative_id.startswith("ordinary-off-gcd:")
                        ][1:],
                    )
                )
                for program in result.programs
            )
        )

    def test_stances_are_not_blindly_rotated_as_ordinary_off_gcd_actions(self):
        result = self._build()
        self.assertEqual(
            STANCE_ACTIONS,
            set(result.stance_actions_requiring_transition_grammar),
        )
        searched_actions = {
            row.guard.action_ready
            for program in result.programs
            for row in program.selector.alternatives
        }
        self.assertTrue(STANCE_ACTIONS.isdisjoint(searched_actions))
        limitation = next(
            row
            for row in result.to_dict()["grammar_limitations"]
            if row["code"]
            == "STANCE_STATE_AND_PAIRED_TRANSITION_GRAMMAR_REQUIRED"
        )
        self.assertEqual(3, len(limitation["actions"]))

    def test_guides_rank_only_and_cannot_remove_native_actions(self):
        guide = CallableProgramProposalGuideV1(
            "fixture-guide",
            lambda _case, _snapshot: {
                BT: 1_000.0,
                UNKNOWN_GUIDE_ACTION: 2_000.0,
            },
        )
        result = self._build(guides=(guide,))
        self.assertEqual(1, result.ignored_guide_action_count)
        self.assertEqual(("fixture-guide",), result.guide_ids)
        self.assertEqual(BT, self._actions(result.programs[0], "gcd", 0)[0])
        for program in result.programs:
            for target in (0, 1):
                self.assertEqual(
                    set(result.gcd_actions),
                    set(self._actions(program, "gcd", target)),
                )
        wire = result.to_dict()
        self.assertFalse(wire["contract"]["guides_can_remove_native_actions"])
        self.assertTrue(wire["contract"]["guides_rank_or_propose_only"])

    def test_existing_wave_guide_adapter_uses_only_caller_train_frontier(self):
        calls = []
        wave_guide = StaticActionGuideV1("existing-offline-fixture", {BT: 9.0})

        def training_outcome(case, snapshot):
            calls.append(case.case_spec["seed"])
            available = tuple(
                AvailableAction(
                    row.index,
                    row.action,
                    row.label,
                    True,
                    row.ready_in_ms,
                    row.triggers_gcd,
                    result_bearing=row.result_bearing,
                )
                for row in snapshot
            )
            return ScheduleReplayOutcomeV1(
                seed=case.case_spec["seed"],
                status=ReplayStatusV1.FRONTIER,
                state={"time_ms": 0, "damage_done": 0.0},
                available_actions=available,
            )

        guides = adapt_wave_action_guides_for_program_proposals_v1(
            (wave_guide,), training_outcome
        )
        result = self._build(guides=guides)
        self.assertEqual([101, 103], calls)
        self.assertEqual(
            ("wave-action-guide:existing-offline-fixture",), result.guide_ids
        )
        self.assertEqual(
            ["wave-action-guide:existing-offline-fixture"],
            result.to_dict()["contract"]["proposal_guides_supplied"],
        )

    def test_programs_are_semantically_unique_with_positive_wait_fallback(self):
        result = self._build()
        selector_wires = {
            json.dumps(
                program.selector.to_dict(), sort_keys=True, separators=(",", ":")
            )
            for program in result.programs
        }
        self.assertEqual(len(result.programs), len(selector_wires))
        self.assertEqual(
            len(result.programs), len({program.program_key() for program in result.programs})
        )
        self.assertTrue(
            all(
                causal_action_program_from_dict_v1(program.to_dict()) == program
                for program in result.programs
            )
        )
        self.assertTrue(
            all(program.selector.fallback.wait_ms == 250 for program in result.programs)
        )
        serialized = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn("death_time", serialized)
        self.assertEqual([], result.to_dict()["contract"]["future_fields_used"])
        self.assertTrue(result.to_dict()["all_factor_options_represented_within_budget"])
        self.assertFalse(
            result.to_dict()["contract"]["search_or_evaluation_performed_here"]
        )
        self.assertLess(
            result.minimum_idle_decisions_before_latest_arrival,
            96,
        )
        self.assertGreaterEqual(result.recommended_max_replay_decisions, 512)

    def test_driver_callable_consumes_training_cases_only(self):
        loaded = []
        generator = UpperKaraCausalProgramGeneratorV1(
            config=self.config,
            snapshot_loader=lambda case: loaded.append(case.case_spec["seed"])
            or self.snapshot,
        )
        programs = generator(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        self.assertTrue(programs)
        self.assertEqual([101], loaded)
        audit = generator.results_by_loadout[self.loadout_id]
        self.assertEqual((101, 103), audit.training_seed_labels)
        self.assertNotIn(211, audit.training_seed_labels)
        self.assertFalse(audit.to_dict()["contract"]["evaluation_examples_consumed"])

    def test_dynamic_guide_factory_receives_only_bound_training_cases(self):
        observed = []

        def guide_factory(cases):
            observed.append(tuple(case.case_spec["seed"] for case in cases))
            return (
                CallableProgramProposalGuideV1(
                    "train-only-factory-guide",
                    lambda _case, _snapshot: {BT: 100.0},
                ),
            )

        generator = UpperKaraCausalProgramGeneratorV1(
            config=self.config,
            snapshot_loader=lambda _case: self.snapshot,
            proposal_guide_factory=guide_factory,
        )
        generator(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )

        self.assertEqual([tuple(row.seed for row in self.examples)], observed)
        result = generator.results_by_loadout[self.loadout_id]
        self.assertEqual(("train-only-factory-guide",), result.guide_ids)
        self.assertNotIn(211, result.training_seed_labels)


if __name__ == "__main__":
    unittest.main()
