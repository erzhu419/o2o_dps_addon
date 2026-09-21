from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.causal_action_program_v1 import (
    GuardedAlternativeV1,
    ImportedReactiveQueueGcdBlockSelectorV1,
    OptionalOffGcdPrefixV1,
    ProgramDecisionV1,
    ProgramPrefixOperationKindV1,
    _replace_queue_gcd_block_v1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.contra_turtle_burst_loadout_v1 import GOBLIN_SAPPER_ACTION
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_queue_gcd_block_search_v1 import (
    UpperKaraCatQueueGcdBlockGeneratorV1,
    project_cat_queue_gcd_block_candidate_set_v1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
    exact_cat_fallback_selector_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    CallableProgramProposalGuideV1,
    UpperKaraCausalProgramGenerationConfigV1,
    UpperKaraCausalProgramGeneratorV1,
    build_upper_kara_causal_program_candidate_set_v1,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    _required_imported_binding_ids_v1,
    build_imported_incumbent_program_replay_factory_v1,
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
BATTLE_SHOUT = ActionRef(spell_id=25_289)


def _available(index: int, action: ActionRef, *, gcd: bool, legal=False):
    return AvailableAction(
        index=index,
        action=action,
        label=str(action.to_wire()),
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=gcd,
        result_bearing=gcd,
    )


class UpperKaraCatQueueGcdBlockProjectionV1Tests(unittest.TestCase):
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
            max_programs=256,
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
        cls.projected = project_cat_queue_gcd_block_candidate_set_v1(cls.source)

    def test_exact_cat_is_the_existing_first_zero_candidate(self) -> None:
        self.assertEqual(
            cat_zero_residual_program_v1(self.loadout_id),
            self.projected.programs[0],
        )
        self.assertEqual(
            self.projected.programs[0],
            causal_action_program_from_dict_v1(
                self.projected.programs[0].to_dict()
            ),
        )

    def test_every_nonzero_candidate_owns_both_lanes_as_one_block(self) -> None:
        self.assertGreater(len(self.projected.programs), 1)
        for program in self.projected.programs[1:]:
            selector = program.selector
            self.assertIsInstance(
                selector, ImportedReactiveQueueGcdBlockSelectorV1
            )
            self.assertEqual(
                exact_cat_fallback_selector_v1(), selector.imported_fallback
            )
            self.assertTrue(
                any(
                    row.decision.queue_op is QueueLaneOp.SET
                    for row in selector.block_alternatives
                )
                or all(
                    row.decision.queue_op is QueueLaneOp.CANCEL
                    for row in selector.block_alternatives
                    if row.decision.gcd_action is not None
                )
            )
            self.assertTrue(
                any(
                    row.decision.gcd_action is not None
                    for row in selector.block_alternatives
                )
            )
            for row in selector.block_alternatives:
                self.assertTrue(
                    row.alternative_id.startswith(("block:queue:", "block:gcd:"))
                )
                self.assertIsNone(row.decision.target_index)
                self.assertFalse(row.decision.start_attack)
                self.assertFalse(row.decision.stop_cast)
                self.assertEqual((), row.decision.optional_off_gcd_prefixes)
            self.assertEqual(
                program, causal_action_program_from_dict_v1(program.to_dict())
            )

    def test_whole_ordered_guard_sequences_vary_not_only_thresholds(self) -> None:
        sequences = {
            tuple(row.alternative_id for row in program.selector.block_alternatives)
            for program in self.projected.programs[1:]
        }
        gcd_sequences = {
            tuple(value for value in sequence if value.startswith("block:gcd:"))
            for sequence in sequences
        }
        queue_sequences = {
            tuple(value for value in sequence if value.startswith("block:queue:"))
            for sequence in sequences
        }
        self.assertGreater(len(gcd_sequences), 1)
        self.assertGreater(len(queue_sequences), 1)

    def test_resources_are_not_imported_and_contract_is_local_only(self) -> None:
        wire = self.projected.to_dict()
        self.assertFalse(wire["contract"]["resource_alternatives_imported_from_source"])
        self.assertFalse(wire["contract"]["wired_into_remote_campaign"])
        self.assertTrue(
            wire["contract"]["queue_and_ordinary_gcd_replaced_atomically"]
        )
        self.assertTrue(wire["all_source_lane_sequences_represented"])
        for program in self.projected.programs[1:]:
            ids = tuple(
                row.alternative_id for row in program.selector.block_alternatives
            )
            self.assertFalse(
                any(
                    value.startswith(
                        (
                            "block:burst:",
                            "block:precombat:",
                            "block:ordinary-off-gcd:",
                        )
                    )
                    for value in ids
                )
            )

    def test_nonzero_block_declares_its_cat_runtime_binding(self) -> None:
        self.assertEqual(
            (CAT_POLICY_ID,),
            _required_imported_binding_ids_v1(self.projected.programs[1]),
        )

    def test_callable_wrapper_keeps_train_only_guide_provenance(self) -> None:
        source_generator = UpperKaraCausalProgramGeneratorV1(
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=64),
            snapshot_loader=lambda _: self.snapshot,
            proposal_guides=(
                CallableProgramProposalGuideV1(
                    "fixture-guide", lambda _case, _snapshot: {BT: 10.0}
                ),
            ),
        )
        wrapper = UpperKaraCatQueueGcdBlockGeneratorV1(source_generator)
        programs = wrapper(
            loadout_id=self.loadout_id,
            train_examples=self.examples,
            train_cases=self.cases,
        )
        result = wrapper.results_by_loadout[self.loadout_id]
        self.assertEqual(result.programs, programs)
        self.assertEqual(["fixture-guide"], result.to_dict()["source_proposal_guide_ids"])
        self.assertTrue(
            all(
                "proposal-guide:fixture-guide" in row.source_refs
                for row in programs[1:]
            )
        )


class ImportedQueueGcdBlockCompositionV1Tests(unittest.TestCase):
    def test_replacement_preserves_source_target_controls_and_off_gcd(self) -> None:
        off_gcd_guard = ObservableCausalGuardV1(
            rage_lte=50,
            action_ready=BLOODRAGE,
            false_semantics=SKIP_PLAN,
        )
        prefix = OptionalOffGcdPrefixV1(BLOODRAGE, off_gcd_guard)
        source = ProgramDecisionV1(
            target_index=1,
            start_attack=True,
            stop_cast=True,
            optional_off_gcd_prefixes=(prefix,),
            queue_op=QueueLaneOp.SET,
            queue_action=HS,
            gcd_action=BT,
            prefix_order=(
                ProgramPrefixOperationKindV1.STOP_CAST,
                ProgramPrefixOperationKindV1.SET_TARGET,
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD,
                ProgramPrefixOperationKindV1.START_ATTACK,
                ProgramPrefixOperationKindV1.QUEUE_SET,
            ),
        )
        selector = ImportedReactiveQueueGcdBlockSelectorV1(
            block_alternatives=(
                GuardedAlternativeV1(
                    "block:queue:t0:hs",
                    ObservableCausalGuardV1(
                        target_index=0,
                        target_attackable_is=True,
                        action_ready=HS,
                        false_semantics=SKIP_PLAN,
                    ),
                    ProgramDecisionV1(
                        queue_op=QueueLaneOp.SET,
                        queue_action=HS,
                        wait_ms=1,
                    ),
                ),
                GuardedAlternativeV1(
                    "block:gcd:t1:ww",
                    ObservableCausalGuardV1(
                        target_index=1,
                        target_attackable_is=True,
                        action_ready=WW,
                        false_semantics=SKIP_PLAN,
                    ),
                    ProgramDecisionV1(gcd_action=WW),
                ),
            ),
            imported_fallback=exact_cat_fallback_selector_v1(),
        )
        observation = CausalLiveStateProjectionV1(
            state={
                "time_ms": 100,
                "target_index": 1,
                "dynamic_target_semantics": {
                    "targets": [
                        {"target_index": 0, "attackable": True},
                        {"target_index": 1, "attackable": True},
                    ]
                },
            },
            policy_to_simulator_target_index=(0, 1),
            visibility_cutoff_ms=100,
        )
        available = (
            _available(0, HS, gcd=False, legal=True),
            _available(1, WW, gcd=True, legal=True),
            _available(2, BLOODRAGE, gcd=False, legal=True),
        )
        receipts = []
        composed = _replace_queue_gcd_block_v1(
            selector,
            source,
            observation,
            available,
            receipts=receipts,
            decision_index=3,
        )

        self.assertEqual(1, composed.target_index)
        self.assertTrue(composed.start_attack)
        self.assertTrue(composed.stop_cast)
        self.assertEqual((prefix,), composed.optional_off_gcd_prefixes)
        self.assertIs(QueueLaneOp.SET, composed.queue_op)
        self.assertEqual(HS, composed.queue_action)
        self.assertEqual(WW, composed.gcd_action)
        self.assertEqual(
            (
                ProgramPrefixOperationKindV1.STOP_CAST,
                ProgramPrefixOperationKindV1.SET_TARGET,
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD,
                ProgramPrefixOperationKindV1.START_ATTACK,
                ProgramPrefixOperationKindV1.QUEUE_SET,
            ),
            composed.prefix_order,
        )
        self.assertTrue(
            any(row["kind"] == "QUEUE_GCD_BLOCK_TARGET_DEFERRED" for row in receipts)
        )
        self.assertEqual(
            "block:gcd:t1:ww",
            next(
                row for row in receipts if row["kind"] == "QUEUE_GCD_BLOCK_SELECTED"
            )["alternative_id"],
        )

    def test_non_result_source_gcd_is_not_overridden(self) -> None:
        source = ProgramDecisionV1(gcd_action=BATTLE_SHOUT)
        selector = ImportedReactiveQueueGcdBlockSelectorV1(
            block_alternatives=(
                GuardedAlternativeV1(
                    "block:gcd:ww",
                    ObservableCausalGuardV1(
                        action_ready=WW, false_semantics=SKIP_PLAN
                    ),
                    ProgramDecisionV1(gcd_action=WW),
                ),
            ),
            imported_fallback=exact_cat_fallback_selector_v1(),
        )
        observation = CausalLiveStateProjectionV1(
            state={"time_ms": 100},
            policy_to_simulator_target_index=(0,),
            visibility_cutoff_ms=100,
        )
        available = (
            AvailableAction(
                index=0,
                action=BATTLE_SHOUT,
                label="Battle Shout",
                legal=True,
                ready_in_ms=0,
                triggers_gcd=True,
                result_bearing=False,
            ),
            _available(1, WW, gcd=True, legal=True),
        )
        receipts = []

        composed = _replace_queue_gcd_block_v1(
            selector,
            source,
            observation,
            available,
            receipts=receipts,
            decision_index=4,
        )

        self.assertEqual(source, composed)
        self.assertEqual(
            "QUEUE_GCD_BLOCK_SOURCE_NON_RESULT_GCD_PRESERVED",
            receipts[-1]["kind"],
        )

    def test_selector_accepts_a_sparse_gcd_override(self) -> None:
        selector = ImportedReactiveQueueGcdBlockSelectorV1(
            block_alternatives=(
                GuardedAlternativeV1(
                    "block:gcd",
                    ObservableCausalGuardV1(
                        action_ready=WW, false_semantics=SKIP_PLAN
                    ),
                    ProgramDecisionV1(gcd_action=WW),
                ),
            ),
            imported_fallback=exact_cat_fallback_selector_v1(),
        )
        self.assertEqual(1, len(selector.block_alternatives))


@unittest.skipUnless(
    DEFAULT_EXACT_BRIDGE.is_file(),
    "the exact precombat-late-arrival native bridge is not built",
)
class ImportedQueueGcdBlockNativeV1Tests(unittest.TestCase):
    def test_nonzero_block_reaches_a_complete_native_terminal(self) -> None:
        loadout_id = "contra_turtle_burst__quickness"
        seed = 311
        case = build_upper_kara_continuous_two_wave_burst_case_v1(
            seed,
            build_id="live_bonereaver",
            loadout_id=loadout_id,
            first_wave_arrival_ms=1_000,
        )
        source = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=loadout_id,
            train_cases=(case,),
            config=UpperKaraCausalProgramGenerationConfigV1(max_programs=64),
        )
        candidate = project_cat_queue_gcd_block_candidate_set_v1(
            source
        ).programs[1]
        outcome = build_imported_incumbent_program_replay_factory_v1(
            "live_bonereaver"
        )(loadout_id, {seed: case}).replay(seed, candidate, max_decisions=512)
        self.assertEqual("COMPLETE", outcome.status.value)
        self.assertIsNone(outcome.invalid_reason)
        self.assertTrue(
            any(
                row.get("kind") == "QUEUE_GCD_BLOCK_SELECTED"
                for row in outcome.receipts
            )
        )


if __name__ == "__main__":
    unittest.main()
