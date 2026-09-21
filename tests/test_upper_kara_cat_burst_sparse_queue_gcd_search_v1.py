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
    causal_action_program_from_dict_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import GOBLIN_SAPPER_ACTION
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_burst_sparse_queue_gcd_search_v1 import (
    MAX_SPARSE_PROGRAMS_V1,
    SPARSE_BLOCK_SOURCE_REF_V1,
    UpperKaraCatBurstSparseQueueGcdGeneratorV1,
    UpperKaraCatBurstSparseQueueGcdSearchV1Error,
    project_cat_burst_sparse_queue_gcd_candidate_set_v1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
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


def _family(program) -> str:
    marker = next(
        value for value in program.source_refs if value.startswith("sparse-family:")
    )
    return marker.split(":", 1)[1]


class UpperKaraCatBurstSparseQueueGcdProjectionV1Tests(unittest.TestCase):
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
        cls.source = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=cls.loadout_id,
            train_examples=cls.examples,
            train_cases=cls.cases,
            config=UpperKaraCausalProgramGenerationConfigV1(
                max_programs=128,
                max_gcd_priority_orders=4,
                max_gcd_order_assignments=8,
                queue_rage_thresholds=(30, 60),
            ),
            snapshot_loader=lambda _: cls.snapshot,
        )
        cls.parent = project_cat_residual_overlay_candidate_set_v1(
            cls.source,
            remaining_attackable_ms_thresholds=(1_500,),
        ).programs[1]
        cls.projected = project_cat_burst_sparse_queue_gcd_candidate_set_v1(
            cls.source,
            cls.parent,
        )

    def test_zero_is_exact_parent_and_space_stays_bounded(self) -> None:
        self.assertIs(self.parent, self.projected.programs[0])
        self.assertLessEqual(len(self.projected.programs), MAX_SPARSE_PROGRAMS_V1)
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

    def test_coverage_first_space_contains_each_sparse_family(self) -> None:
        wire = self.projected.to_dict()
        expected = {
            "QUEUE_ONLY",
            "GCD_INDIVIDUAL",
            "GCD_PREFIX",
            "GCD_INDIVIDUAL_NO_QUEUE",
            "GCD_PREFIX_NO_QUEUE",
            "QUEUE_PLUS_GCD_INDIVIDUAL",
            "QUEUE_PLUS_GCD_PREFIX",
        }
        self.assertEqual(expected, set(wire["requested_family_counts"]))
        self.assertEqual(expected, set(wire["emitted_family_counts"]))
        self.assertFalse(wire["candidate_space_truncated"])
        self.assertEqual(
            wire["unique_queue_sequence_count"],
            wire["represented_queue_sequence_count"],
        )
        self.assertEqual(
            wire["unique_gcd_individual_count"]
            + wire["unique_gcd_prefix_count"]
            + wire["unique_no_queue_gcd_individual_count"]
            + wire["unique_no_queue_gcd_prefix_count"],
            wire["represented_gcd_fragment_count"],
        )

    def test_each_family_is_sparse_and_gcd_inherits_cat_queue(self) -> None:
        for program in self.projected.programs[1:]:
            selector = program.selector
            self.assertIsInstance(
                selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
            )
            family = _family(program)
            queue_rows = [
                row
                for row in selector.block_alternatives
                if row.decision.queue_op is QueueLaneOp.SET
            ]
            gcd_rows = [
                row
                for row in selector.block_alternatives
                if row.decision.gcd_action is not None
            ]
            if family == "QUEUE_ONLY":
                self.assertTrue(queue_rows)
                self.assertFalse(gcd_rows)
            elif family == "GCD_INDIVIDUAL":
                self.assertFalse(queue_rows)
                self.assertEqual(1, len(gcd_rows))
                self.assertTrue(
                    all(row.decision.queue_op is QueueLaneOp.KEEP for row in gcd_rows)
                )
            elif family == "GCD_PREFIX":
                self.assertFalse(queue_rows)
                self.assertIn(len(gcd_rows), {2, 3})
                self.assertTrue(
                    all(row.decision.queue_op is QueueLaneOp.KEEP for row in gcd_rows)
                )
            elif family == "GCD_INDIVIDUAL_NO_QUEUE":
                self.assertFalse(queue_rows)
                self.assertEqual(1, len(gcd_rows))
                self.assertTrue(
                    all(row.decision.queue_op is QueueLaneOp.CANCEL for row in gcd_rows)
                )
            elif family == "GCD_PREFIX_NO_QUEUE":
                self.assertFalse(queue_rows)
                self.assertIn(len(gcd_rows), {2, 3})
                self.assertTrue(
                    all(row.decision.queue_op is QueueLaneOp.CANCEL for row in gcd_rows)
                )
            elif family == "QUEUE_PLUS_GCD_INDIVIDUAL":
                self.assertTrue(queue_rows)
                self.assertEqual(1, len(gcd_rows))
                self.assertTrue(
                    all(row.decision.queue_op is QueueLaneOp.KEEP for row in gcd_rows)
                )
            elif family == "QUEUE_PLUS_GCD_PREFIX":
                self.assertTrue(queue_rows)
                self.assertIn(len(gcd_rows), {2, 3})
                self.assertTrue(
                    all(row.decision.queue_op is QueueLaneOp.KEEP for row in gcd_rows)
                )
            else:  # pragma: no cover - gives a useful failure on a new family
                self.fail(f"unexpected sparse family {family}")

    def test_parent_burst_and_cat_fallback_are_unchanged(self) -> None:
        parent = self.parent.selector
        self.assertIsInstance(parent, ImportedFallbackOverlaySelectorV1)
        for program in self.projected.programs[1:]:
            selector = program.selector
            self.assertEqual(parent.terminal_alternatives, selector.terminal_alternatives)
            self.assertEqual(parent.imported_fallback, selector.imported_fallback)
            self.assertEqual(parent.off_gcd_insertions, selector.off_gcd_insertions)
            self.assertEqual(parent.insertion_order, selector.insertion_order)
            self.assertEqual(parent.insertion_position, selector.insertion_position)
            self.assertEqual(
                program,
                causal_action_program_from_dict_v1(program.to_dict()),
            )

    def test_audit_is_honest_under_a_tight_budget(self) -> None:
        projected = project_cat_burst_sparse_queue_gcd_candidate_set_v1(
            self.source,
            self.parent,
            max_programs=10,
        )
        wire = projected.to_dict()
        self.assertEqual(10, len(projected.programs))
        self.assertTrue(wire["candidate_space_truncated"])
        self.assertEqual(
            wire["requested_nonzero_program_count"],
            sum(wire["requested_family_counts"].values()),
        )
        self.assertEqual(9, sum(wire["emitted_family_counts"].values()))
        # Queue-only witnesses are emitted first, so every queue coordinate is
        # still represented before later combination candidates are truncated.
        self.assertEqual(
            wire["unique_queue_sequence_count"],
            wire["represented_queue_sequence_count"],
        )

    def test_provenance_names_parent_family_source_and_guides(self) -> None:
        source_generator = UpperKaraCausalProgramGeneratorV1(
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=64),
            snapshot_loader=lambda _: self.snapshot,
            proposal_guides=(
                CallableProgramProposalGuideV1(
                    "fixture-guide", lambda _case, _snapshot: {BT: 10.0}
                ),
            ),
        )
        generator = UpperKaraCatBurstSparseQueueGcdGeneratorV1(
            source_generator,
            self.parent,
            max_programs=32,
        )
        programs = generator(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        result = generator.results_by_loadout[self.loadout_id]
        self.assertIs(self.parent, programs[0])
        self.assertEqual(["fixture-guide"], result.to_dict()["source_proposal_guide_ids"])
        for program in programs[1:]:
            self.assertIn(SPARSE_BLOCK_SOURCE_REF_V1, program.source_refs)
            self.assertIn(f"parent-program:{self.parent.program_id}", program.source_refs)
            self.assertIn("proposal-guide:fixture-guide", program.source_refs)
            self.assertTrue(
                any(
                    value.startswith("representative-source-program:")
                    for value in program.source_refs
                )
            )

    def test_invalid_parent_and_unbounded_budget_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            UpperKaraCatBurstSparseQueueGcdSearchV1Error,
            "ImportedFallbackOverlaySelectorV1",
        ):
            project_cat_burst_sparse_queue_gcd_candidate_set_v1(
                self.source,
                self.source.programs[0],
            )
        with self.assertRaisesRegex(ValueError, "2 to 512"):
            project_cat_burst_sparse_queue_gcd_candidate_set_v1(
                self.source,
                self.parent,
                max_programs=513,
            )


if __name__ == "__main__":
    unittest.main()
