from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.causal_action_program_v1 import (
    ImportedFallbackOverlaySelectorV1,
    ProgramInsertionPointV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import (
    GOBLIN_SAPPER_ACTION,
    RAPID_GROWTH_ACTION,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    UpperKaraCatResidualOverlayGeneratorV1,
    cat_zero_residual_program_v1,
    project_cat_residual_overlay_candidate_set_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    CallableProgramProposalGuideV1,
    UpperKaraCausalProgramGenerationConfigV1,
    UpperKaraCausalProgramGeneratorV1,
    build_upper_kara_causal_program_candidate_set_v1,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
    build_imported_incumbent_program_replay_factory_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    TwoWaveExampleV1,
    imported_incumbent_programs_v1,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1


BT = ActionRef(spell_id=23_894)
WW = ActionRef(spell_id=1_680)
EXECUTE = ActionRef(spell_id=20_662)
SUNDER = ActionRef(spell_id=11_597)
HS = ActionRef(spell_id=25_286, tag=1)
CLEAVE = ActionRef(spell_id=20_569, tag=1)
BLOODRAGE = ActionRef(spell_id=2_687)
BRIDGE_AVAILABLE = DEFAULT_EXACT_BRIDGE.is_file()


def _available(
    index: int,
    action: ActionRef,
    *,
    gcd: bool,
) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=action,
        label=str(action.to_wire()),
        legal=False,
        ready_in_ms=0,
        triggers_gcd=gcd,
        result_bearing=gcd,
    )


def _execution_receipts(outcome):
    execution_kinds = {
        "SET_TARGET",
        "START_ATTACK",
        "STOP_CAST",
        "OPTIONAL_OFF_GCD_SKIPPED",
        "OPTIONAL_OFF_GCD_EXECUTED",
        "QUEUE_KEEP",
        "QUEUE_CANCEL",
        "QUEUE_SET",
        "TERMINAL_GCD",
        "TERMINAL_WAIT",
    }
    return tuple(
        row for row in outcome.receipts if row.get("kind") in execution_kinds
    )


class UpperKaraCatResidualOverlayProjectionV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loadout_id = "contra_turtle_burst__quickness"
        cls.examples = (TwoWaveExampleV1(101, 0), TwoWaveExampleV1(103, 7_000))
        cls.cases = tuple(
            build_upper_kara_continuous_two_wave_burst_case_v1(
                row.seed,
                build_id="live_bonereaver",
                loadout_id=cls.loadout_id,
                first_wave_arrival_ms=row.first_wave_arrival_ms,
            )
            for row in cls.examples
        )
        burst_actions = tuple(
            sorted({*cls.cases[0].precombat.self_actions, GOBLIN_SAPPER_ACTION})
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
        rows.append(_available(len(rows), BLOODRAGE, gcd=False))
        cls.snapshot = tuple(rows)
        cls.config = UpperKaraCausalProgramGenerationConfigV1(
            max_programs=512,
            hp_thresholds=(20, 35, 50, 80),
            ordinary_off_gcd_rage_lte_thresholds=(0, 40, 60),
        )
        cls.source = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=cls.loadout_id,
            train_examples=cls.examples,
            train_cases=cls.cases,
            config=cls.config,
            snapshot_loader=lambda _: cls.snapshot,
        )
        cls.projected = project_cat_residual_overlay_candidate_set_v1(cls.source)

    def test_every_loadout_family_starts_with_searched_exact_cat(self) -> None:
        zero = self.projected.programs[0]
        self.assertEqual(cat_zero_residual_program_v1(self.loadout_id), zero)
        self.assertIs(ProgramOriginV1.SEARCHED, zero.origin)
        self.assertIsInstance(zero.selector, ImportedFallbackOverlaySelectorV1)
        self.assertEqual((), zero.selector.terminal_alternatives)
        self.assertEqual((), zero.selector.off_gcd_insertions)
        self.assertEqual(CAT_POLICY_ID, zero.selector.imported_fallback.binding_id)
        self.assertEqual(
            CAT_POLICY_ID, zero.selector.imported_fallback.source_policy_id
        )
        self.assertEqual(
            OBSERVATION_CONTRACT_ID_V1,
            zero.selector.imported_fallback.observation_contract_id,
        )
        self.assertEqual(
            zero,
            causal_action_program_from_dict_v1(zero.to_dict()),
        )

    def test_residuals_leave_cat_gcd_and_queue_lanes_untouched(self) -> None:
        self.assertGreater(len(self.projected.programs), 1)
        saw_precombat = False
        saw_burst = False
        saw_ordinary_off_gcd = False
        for program in self.projected.programs:
            self.assertIs(ProgramOriginV1.SEARCHED, program.origin)
            selector = program.selector
            self.assertIsInstance(selector, ImportedFallbackOverlaySelectorV1)
            self.assertEqual(CAT_POLICY_ID, selector.imported_fallback.binding_id)
            for alternative in selector.terminal_alternatives:
                self.assertTrue(
                    alternative.alternative_id.startswith(
                        ("precombat:", "burst:")
                    )
                )
                self.assertIsNotNone(alternative.decision.gcd_action)
                saw_precombat |= alternative.alternative_id.startswith(
                    "precombat:"
                )
                saw_burst |= alternative.alternative_id.startswith("burst:")
            for insertion in selector.off_gcd_insertions:
                residual_id = insertion.insertion_id.removeprefix("residual:")
                self.assertTrue(
                    residual_id.startswith(
                        ("precombat:", "burst:", "ordinary-off-gcd:")
                    )
                )
                self.assertFalse(residual_id.startswith(("gcd:", "queue:")))
                if insertion.prefix.guard.target_index is None:
                    self.assertIs(
                        ProgramInsertionPointV1.BEFORE_SOURCE_PREFIX_ORDER,
                        insertion.insertion_point,
                    )
                else:
                    self.assertIs(
                        ProgramInsertionPointV1.AFTER_SOURCE_SET_TARGET,
                        insertion.insertion_point,
                    )
                saw_precombat |= residual_id.startswith("precombat:")
                saw_burst |= residual_id.startswith("burst:")
                saw_ordinary_off_gcd |= residual_id.startswith(
                    "ordinary-off-gcd:"
                )
        self.assertTrue(saw_precombat)
        self.assertTrue(saw_burst)
        self.assertTrue(saw_ordinary_off_gcd)

    def test_same_guarded_resource_can_skip_wave_zero_and_reconsider_wave_one(self):
        matches = []
        for program in self.projected.programs:
            selector = program.selector
            rapid = tuple(
                insertion
                for insertion in selector.off_gcd_insertions
                if insertion.prefix.action == RAPID_GROWTH_ACTION
            )
            guards = {
                (
                    row.prefix.guard.target_index,
                    row.prefix.guard.target_hp_pct_gte,
                )
                for row in rapid
            }
            if guards == {(0, 35.0), (1, 35.0)}:
                matches.append(program)
        self.assertTrue(matches)

    def test_remaining_time_is_an_additive_burst_axis_and_round_trips(self):
        projected = project_cat_residual_overlay_candidate_set_v1(
            self.source,
            remaining_attackable_ms_thresholds=(3_000,),
        )
        hp_only = []
        time_gated = []
        for program in projected.programs:
            selector = program.selector
            for insertion in selector.off_gcd_insertions:
                if not insertion.insertion_id.startswith("residual:burst:"):
                    continue
                guard = insertion.prefix.guard
                self.assertIsNotNone(guard.target_hp_pct_gte)
                if guard.estimated_remaining_attackable_gte_ms is None:
                    hp_only.append(program)
                else:
                    self.assertEqual(
                        3_000,
                        guard.estimated_remaining_attackable_gte_ms,
                    )
                    self.assertIn(
                        ":remaining-ms3000", insertion.insertion_id
                    )
                    time_gated.append(program)
                    self.assertEqual(
                        program,
                        causal_action_program_from_dict_v1(program.to_dict()),
                    )
        self.assertTrue(hp_only)
        self.assertTrue(time_gated)
        self.assertEqual(
            [3_000],
            projected.to_dict()["remaining_attackable_ms_thresholds"],
        )

    def test_callable_wrapper_retains_projection_audit(self) -> None:
        source_generator = UpperKaraCausalProgramGeneratorV1(
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=8),
            snapshot_loader=lambda _: self.snapshot,
            proposal_guides=(
                CallableProgramProposalGuideV1(
                    "fixture-source-guide",
                    lambda _case, _snapshot: {BT: 10.0},
                ),
            ),
        )
        wrapper = UpperKaraCatResidualOverlayGeneratorV1(source_generator)
        programs = wrapper(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        self.assertEqual(
            wrapper.results_by_loadout[self.loadout_id].programs,
            programs,
        )
        self.assertEqual(
            cat_zero_residual_program_v1(self.loadout_id), programs[0]
        )
        self.assertEqual(
            ["fixture-source-guide"],
            wrapper.results_by_loadout[self.loadout_id]
            .to_dict()["source_proposal_guide_ids"],
        )
        self.assertTrue(
            all(
                "proposal-guide:fixture-source-guide" in row.source_refs
                for row in programs[1:]
            )
        )


@unittest.skipUnless(
    BRIDGE_AVAILABLE,
    "the exact precombat-late-arrival native bridge is not built",
)
class UpperKaraCatResidualOverlayNativeV1Tests(unittest.TestCase):
    def test_zero_overlay_is_native_command_and_metric_equivalent_to_cat(self):
        build_id = "live_bonereaver"
        loadout_id = "contra_turtle_burst__quickness"
        seed = 311
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            seed,
            build_id=build_id,
            loadout_id=loadout_id,
            first_wave_arrival_ms=7_000,
        )
        imported_cat = next(
            row
            for row in imported_incumbent_programs_v1(build_id)
            if row.selector.source_policy_id == CAT_POLICY_ID
        )
        zero = cat_zero_residual_program_v1(loadout_id)
        factory = build_imported_incumbent_program_replay_factory_v1(build_id)
        cat_outcome = factory(loadout_id, {seed: case}).replay(
            seed, imported_cat, max_decisions=512
        )
        zero_outcome = factory(loadout_id, {seed: case}).replay(
            seed, zero, max_decisions=512
        )

        self.assertIs(ReplayStatusV1.COMPLETE, cat_outcome.status)
        self.assertIs(ReplayStatusV1.COMPLETE, zero_outcome.status)
        self.assertEqual(cat_outcome.state, zero_outcome.state)
        self.assertEqual(cat_outcome.elapsed_ms, zero_outcome.elapsed_ms)
        self.assertEqual(cat_outcome.effective_damage, zero_outcome.effective_damage)
        self.assertEqual(
            _execution_receipts(cat_outcome),
            _execution_receipts(zero_outcome),
        )

    def test_late_arrival_low_hp_holds_rapid_growth_for_second_wave(self):
        build_id = "live_bonereaver"
        loadout_id = "contra_turtle_burst__quickness"
        seed = 313
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            seed,
            build_id=build_id,
            loadout_id=loadout_id,
            first_wave_arrival_ms=7_000,
        )
        source = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=loadout_id,
            train_cases=(case,),
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=512),
        )
        projected = project_cat_residual_overlay_candidate_set_v1(source)
        matches = []
        for program in projected.programs:
            rapid = tuple(
                row
                for row in program.selector.off_gcd_insertions
                if row.prefix.action == RAPID_GROWTH_ACTION
            )
            if {
                (
                    row.prefix.guard.target_index,
                    row.prefix.guard.target_hp_pct_gte,
                )
                for row in rapid
            } == {(0, 35.0), (1, 35.0)}:
                matches.append(program)
        self.assertTrue(matches)
        program = min(matches, key=lambda row: row.program_id)
        outcome = build_imported_incumbent_program_replay_factory_v1(build_id)(
            loadout_id, {seed: case}
        ).replay(seed, program, max_decisions=512)

        self.assertIs(ReplayStatusV1.COMPLETE, outcome.status)
        rapid_skips = [
            row
            for row in outcome.receipts
            if row.get("kind") == "OPTIONAL_OFF_GCD_SKIPPED"
            and row.get("action") == RAPID_GROWTH_ACTION.to_wire()
            and row.get("state_time_ms", 0) < 15_000
        ]
        self.assertTrue(
            any(
                "target_hp_pct_gte"
                in row["evaluation"]["failed_predicates"]
                for row in rapid_skips
            )
        )
        rapid_uses = [
            row
            for row in outcome.receipts
            if row.get("kind") == "OPTIONAL_OFF_GCD_EXECUTED"
            and row.get("action") == RAPID_GROWTH_ACTION.to_wire()
        ]
        self.assertEqual(1, len(rapid_uses))
        self.assertGreaterEqual(rapid_uses[0]["state_time_ms"], 15_000)
        selected = [
            row
            for row in outcome.receipts
            if row.get("kind") == "SEARCHED_OFF_GCD_PREFIXES_PREPENDED"
            and "residual:burst:t1:item56113t0:hp35"
            in row.get("eligible_insertion_order", ())
        ]
        self.assertTrue(selected)


if __name__ == "__main__":
    unittest.main()
