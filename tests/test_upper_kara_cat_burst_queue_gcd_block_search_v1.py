from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.causal_action_program_v1 import (
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ImportedReactiveQueueGcdBlockSelectorV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import GOBLIN_SAPPER_ACTION
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_burst_queue_gcd_block_search_v1 import (
    BLOCK_OVER_PARENT_SOURCE_REF_V1,
    UpperKaraCatBurstQueueGcdBlockGeneratorV1,
    UpperKaraCatBurstQueueGcdBlockSearchV1Error,
    project_cat_burst_queue_gcd_block_candidate_set_v1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    UpperKaraCatResidualOverlayGeneratorV1,
    project_cat_residual_overlay_candidate_set_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    CallableProgramProposalGuideV1,
    UpperKaraCausalProgramGenerationConfigV1,
    UpperKaraCausalProgramGeneratorV1,
    build_upper_kara_causal_program_candidate_set_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp


BT = ActionRef(spell_id=23_894)
WW = ActionRef(spell_id=1_680)
EXECUTE = ActionRef(spell_id=20_662)
SUNDER = ActionRef(spell_id=11_597)
HS = ActionRef(spell_id=25_286, tag=1)
CLEAVE = ActionRef(spell_id=20_569, tag=1)
BLOODRAGE = ActionRef(spell_id=2_687)


def _available(index: int, action: ActionRef, *, gcd: bool) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=action,
        label=str(action.to_wire()),
        legal=False,
        ready_in_ms=0,
        triggers_gcd=gcd,
        result_bearing=gcd,
    )


class UpperKaraCatBurstQueueGcdBlockProjectionV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loadout_id = "contra_turtle_burst__mighty_rage"
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
        rows = [
            _available(0, BT, gcd=True),
            _available(1, WW, gcd=True),
            _available(2, EXECUTE, gcd=True),
            _available(3, SUNDER, gcd=True),
            _available(4, HS, gcd=False),
            _available(5, CLEAVE, gcd=False),
        ]
        burst_actions = tuple(
            sorted({*cls.cases[0].precombat.self_actions, GOBLIN_SAPPER_ACTION})
        )
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
            max_programs=128,
            max_gcd_priority_orders=4,
            max_gcd_order_assignments=8,
            queue_rage_thresholds=(30, 60),
        )
        cls.source = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=cls.loadout_id,
            train_examples=cls.examples,
            train_cases=cls.cases,
            config=cls.config,
            snapshot_loader=lambda _: cls.snapshot,
        )
        residuals = project_cat_residual_overlay_candidate_set_v1(
            cls.source,
            remaining_attackable_ms_thresholds=(1_500,),
        )
        cls.parent = residuals.programs[1]
        cls.projected = project_cat_burst_queue_gcd_block_candidate_set_v1(
            cls.source,
            cls.parent,
            max_programs=64,
        )

    def test_zero_is_the_exact_frozen_parent_object(self) -> None:
        self.assertIs(self.parent, self.projected.programs[0])
        self.assertEqual(
            self.parent.program_key(),
            self.projected.to_dict()["paired_zero_program_key"],
        )
        self.assertEqual(
            self.parent,
            causal_action_program_from_dict_v1(
                self.projected.programs[0].to_dict()
            ),
        )

    def test_nonzero_candidates_only_add_an_atomic_block(self) -> None:
        parent = self.parent.selector
        self.assertIsInstance(parent, ImportedFallbackOverlaySelectorV1)
        self.assertGreater(len(self.projected.programs), 1)
        for composite_program, block_program in zip(
            self.projected.programs[1:],
            self.projected.block_candidate_set.programs[1:],
        ):
            composite = composite_program.selector
            block = block_program.selector
            self.assertIsInstance(
                composite, ImportedReactiveBurstQueueGcdBlockSelectorV1
            )
            self.assertIsInstance(block, ImportedReactiveQueueGcdBlockSelectorV1)
            self.assertEqual(
                (
                    parent.terminal_alternatives,
                    parent.imported_fallback,
                    parent.off_gcd_insertions,
                    parent.insertion_order,
                    parent.insertion_position,
                ),
                (
                    composite.terminal_alternatives,
                    composite.imported_fallback,
                    composite.off_gcd_insertions,
                    composite.insertion_order,
                    composite.insertion_position,
                ),
            )
            self.assertEqual(
                block.block_alternatives,
                composite.block_alternatives,
            )
            self.assertTrue(
                any(
                    row.decision.queue_op is QueueLaneOp.SET
                    for row in composite.block_alternatives
                )
                or all(
                    row.decision.queue_op is QueueLaneOp.CANCEL
                    for row in composite.block_alternatives
                    if row.decision.gcd_action is not None
                )
            )
            self.assertTrue(
                any(
                    row.decision.gcd_action is not None
                    for row in composite.block_alternatives
                )
            )
            self.assertEqual(
                composite_program,
                causal_action_program_from_dict_v1(composite_program.to_dict()),
            )

    def test_provenance_keeps_parent_and_both_sequence_sources(self) -> None:
        for program in self.projected.programs[1:]:
            self.assertIn(BLOCK_OVER_PARENT_SOURCE_REF_V1, program.source_refs)
            self.assertIn(
                f"parent-program:{self.parent.program_id}", program.source_refs
            )
            self.assertTrue(
                all(value in program.source_refs for value in self.parent.source_refs)
            )

    def test_contract_reports_independent_sequence_recombination_scope(self) -> None:
        contract = self.projected.to_dict()["contract"]
        self.assertTrue(contract["paired_zero_is_exact_parent_program"])
        self.assertTrue(contract["parent_burst_program_is_frozen"])
        self.assertTrue(contract["queue_and_gcd_sequences_recombined_independently"])
        self.assertTrue(contract["explicit_no_queue_sequence_searched"])
        self.assertTrue(contract["unmatched_block_keeps_parent_source_decision"])

    def test_parent_must_be_an_overlay_over_exact_cat(self) -> None:
        with self.assertRaisesRegex(
            UpperKaraCatBurstQueueGcdBlockSearchV1Error,
            "ImportedFallbackOverlaySelectorV1",
        ):
            project_cat_burst_queue_gcd_block_candidate_set_v1(
                self.source,
                self.source.programs[0],
            )

    def test_wrapper_retains_source_guide_audit_and_fixed_parent(self) -> None:
        source_generator = UpperKaraCausalProgramGeneratorV1(
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=64),
            snapshot_loader=lambda _: self.snapshot,
            proposal_guides=(
                CallableProgramProposalGuideV1(
                    "fixture-guide", lambda _case, _snapshot: {BT: 10.0}
                ),
            ),
        )
        wrapper = UpperKaraCatBurstQueueGcdBlockGeneratorV1(
            source_generator,
            parent_program=self.parent,
            max_programs=32,
        )
        programs = wrapper(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        result = wrapper.results_by_loadout[self.loadout_id]
        self.assertEqual(result.programs, programs)
        self.assertIs(self.parent, programs[0])
        self.assertEqual(
            ["fixture-guide"],
            result.to_dict()["block_projection_audit"][
                "source_proposal_guide_ids"
            ],
        )
        self.assertTrue(
            all(
                "proposal-guide:fixture-guide" in row.source_refs
                for row in programs[1:]
            )
        )

    def test_composes_with_the_residual_generator_output(self) -> None:
        source_generator = UpperKaraCausalProgramGeneratorV1(
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=32),
            snapshot_loader=lambda _: self.snapshot,
        )
        residual_generator = UpperKaraCatResidualOverlayGeneratorV1(
            source_generator,
            remaining_attackable_ms_thresholds=(1_500,),
        )
        residual_programs = residual_generator(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        wrapper = UpperKaraCatBurstQueueGcdBlockGeneratorV1(
            source_generator,
            parent_program=residual_programs[1],
            max_programs=16,
        )
        programs = wrapper(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        self.assertIs(residual_programs[1], programs[0])
        self.assertGreater(len(programs), 1)


if __name__ == "__main__":
    unittest.main()
