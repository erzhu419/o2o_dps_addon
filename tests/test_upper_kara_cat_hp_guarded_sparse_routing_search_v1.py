from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.upper_kara_cat_hp_guarded_sparse_routing_search_v1 import (
    FRESH_ARRIVAL_SCHEDULE_MS_V1,
    FreshSeedProtocolV1,
    HP_BANDS_V1,
    HP_ROUTING_SOURCE_REF_V1,
    HP_THRESHOLD_GRID_V1,
    UpperKaraCatHpGuardedSparseRoutingGeneratorV1,
    UpperKaraCatHpGuardedSparseRoutingV1Error,
    build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    FixedParentQueueGcdBlockSearchV1,
    load_continuous_two_wave_remote_campaign_v1,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp


V5_CONFIG = (
    PROJECT_ROOT
    / "configs/evaluation/upper_kara_fixed_burst_block_remote_v5_256x256_v1.json"
)
LOADOUT = "contra_turtle_burst__mighty_rage"
WW = ActionRef(spell_id=1_680)
BT = ActionRef(spell_id=23_894)
SLAM = ActionRef(spell_id=45_961)
HS = ActionRef(spell_id=25_286, tag=1)
CLEAVE = ActionRef(spell_id=20_569, tag=1)


def _alternative(
    alternative_id: str,
    *,
    target_index: int,
    action: ActionRef,
    queue_op: QueueLaneOp,
    rage_gte: float | None = None,
    queue_status_is: str | None = None,
) -> GuardedAlternativeV1:
    guard = ObservableCausalGuardV1(
        rage_gte=rage_gte,
        target_index=target_index,
        target_attackable_is=True,
        queue_status_is=queue_status_is,
        action_ready=action,
        false_semantics=SKIP_PLAN,
    )
    if queue_op is QueueLaneOp.SET:
        decision = ProgramDecisionV1(
            queue_op=queue_op,
            queue_action=action,
            wait_ms=1,
        )
    else:
        decision = ProgramDecisionV1(
            queue_op=queue_op,
            gcd_action=action,
        )
    return GuardedAlternativeV1(alternative_id, guard, decision)


def _prototype(
    parent: CausalActionProgramV1,
    index: int,
    alternatives: tuple[GuardedAlternativeV1, ...],
) -> CausalActionProgramV1:
    selector = parent.selector
    return CausalActionProgramV1(
        program_id=(
            f"cat-burst-sparse-queue-gcd::{LOADOUT}::{index:04d}"
        ),
        selector=ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=selector.terminal_alternatives,
            imported_fallback=selector.imported_fallback,
            block_alternatives=alternatives,
            off_gcd_insertions=selector.off_gcd_insertions,
            insertion_order=selector.insertion_order,
            insertion_position=selector.insertion_position,
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=(
            *parent.source_refs,
            "cat-burst-sparse-queue-gcd/v1:projected",
            f"fixture-v5-prototype:{index}",
        ),
    )


def _prototype_panel(
    parent: CausalActionProgramV1,
) -> dict[int, CausalActionProgramV1]:
    queue_159 = (
        _alternative(
            "sparse:block:queue:t0:spell20569t1:rage60",
            target_index=0,
            action=CLEAVE,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
        _alternative(
            "sparse:block:queue:t0:spell25286t1:rage60",
            target_index=0,
            action=HS,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
        _alternative(
            "sparse:block:queue:t1:spell20569t1:rage60",
            target_index=1,
            action=CLEAVE,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
        _alternative(
            "sparse:block:queue:t1:spell25286t1:rage60",
            target_index=1,
            action=HS,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
    )
    queue_262 = (
        _alternative(
            "sparse:block:queue:t0:spell25286t1:rage60",
            target_index=0,
            action=HS,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
        _alternative(
            "sparse:block:queue:t0:spell20569t1:rage60",
            target_index=0,
            action=CLEAVE,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
        _alternative(
            "sparse:block:queue:t1:spell25286t1:rage60",
            target_index=1,
            action=HS,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
        _alternative(
            "sparse:block:queue:t1:spell20569t1:rage60",
            target_index=1,
            action=CLEAVE,
            queue_op=QueueLaneOp.SET,
            rage_gte=60,
            queue_status_is="NONE",
        ),
    )
    return {
        42: _prototype(
            parent,
            42,
            (
                _alternative(
                    "sparse:block:gcd:t0:spell1680t0:no-queue",
                    target_index=0,
                    action=WW,
                    queue_op=QueueLaneOp.CANCEL,
                ),
            ),
        ),
        47: _prototype(
            parent,
            47,
            (
                _alternative(
                    "sparse:block:gcd:t1:spell23894t0:no-queue",
                    target_index=1,
                    action=BT,
                    queue_op=QueueLaneOp.CANCEL,
                ),
            ),
        ),
        159: _prototype(
            parent,
            159,
            (
                *queue_159,
                _alternative(
                    "sparse:block:gcd:t1:spell45961t0",
                    target_index=1,
                    action=SLAM,
                    queue_op=QueueLaneOp.KEEP,
                ),
            ),
        ),
        262: _prototype(
            parent,
            262,
            (
                *queue_262,
                _alternative(
                    "sparse:block:gcd:t1:spell23894t0",
                    target_index=1,
                    action=BT,
                    queue_op=QueueLaneOp.KEEP,
                ),
            ),
        ),
    }


def _route_refs(program: CausalActionProgramV1) -> set[str]:
    return {
        value for value in program.source_refs if value.startswith("hp-route:")
    }


class UpperKaraCatHpGuardedSparseRoutingV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        campaign = load_continuous_two_wave_remote_campaign_v1(V5_CONFIG)
        if not isinstance(campaign.search_spec, FixedParentQueueGcdBlockSearchV1):
            raise AssertionError("fixture campaign lost its fixed v5 parent")
        cls.parent = campaign.search_spec.parent_program
        cls.prototypes = _prototype_panel(cls.parent)
        cls.result = (
            build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1(
                loadout_id=LOADOUT,
                parent_program=cls.parent,
                prototype_programs=cls.prototypes,
            )
        )

    def test_exact_zero_anchors_and_bounded_family_counts(self) -> None:
        self.assertEqual(309, len(self.result.programs))
        self.assertIs(self.parent, self.result.programs[0])
        self.assertEqual(
            tuple(self.prototypes[index] for index in (42, 47, 159, 262)),
            self.result.programs[1:5],
        )
        self.assertEqual(
            {
                "PAIRED_CAT_ZERO": 1,
                "V5_PROTOTYPE_ANCHOR": 4,
                "HP_SINGLE": 24,
                "HP_PAIR_CANCEL": 60,
                "HP_PAIR_QUEUE_GCD": 60,
                "HP_TRIPLE_CANCEL": 160,
            },
            dict(self.result.family_counts),
        )
        wire = self.result.to_dict()
        self.assertEqual([20, 35, 50, 65, 80], wire["hp_threshold_grid"])
        self.assertEqual(309, wire["search_space"]["emitted_bounded_programs"])
        self.assertEqual(
            1_457,
            wire["search_space"]["same_selector_class_full_assignments"],
        )
        self.assertEqual(
            15_625,
            wire["search_space"][
                "unconstrained_zero_or_four_prototypes_per_band"
            ],
        )

    def test_every_new_route_uses_only_current_allowed_signals(self) -> None:
        forbidden = (
            "pull_relative_time_gte_ms",
            "pull_relative_time_lte_ms",
            "target_aura_action",
            "target_aura_stacks_lte",
            "estimated_remaining_attackable_gte_ms",
            "live_target_count_gte",
            "live_target_count_lte",
            "attackable_target_count_gte",
            "attackable_target_count_lte",
            "mh_swing_remaining_lte_ms",
            "aura_action",
            "aura_remaining_gte_ms",
            "aura_remaining_lte_ms",
        )
        threshold_values = set(HP_THRESHOLD_GRID_V1)
        for program in self.result.programs[5:]:
            self.assertNotIn("first_wave_arrival_ms", program.program_key())
            self.assertIn(HP_ROUTING_SOURCE_REF_V1, program.source_refs)
            selector = program.selector
            self.assertIsInstance(
                selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
            )
            for alternative in selector.block_alternatives:
                guard = alternative.guard
                self.assertIs(guard.target_attackable_is, True)
                self.assertIsNotNone(guard.action_ready)
                self.assertEqual(SKIP_PLAN, guard.false_semantics)
                self.assertTrue(
                    guard.target_hp_pct_gte is not None
                    or guard.target_hp_pct_lte is not None
                )
                if guard.target_hp_pct_gte is not None:
                    self.assertIn(guard.target_hp_pct_gte, threshold_values)
                if guard.target_hp_pct_lte is not None:
                    self.assertIn(guard.target_hp_pct_lte, threshold_values)
                self.assertTrue(
                    all(getattr(guard, field) is None for field in forbidden)
                )

    def test_signal_preservation_and_structural_classes_do_not_mix(self) -> None:
        single_159 = next(
            row
            for row in self.result.programs[5:]
            if _route_refs(row) == {"hp-route:hp35_50:p0159"}
        )
        selector = single_159.selector
        self.assertIsInstance(
            selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
        )
        self.assertEqual(5, len(selector.block_alternatives))
        for alternative in selector.block_alternatives[:4]:
            self.assertEqual(60.0, alternative.guard.rage_gte)
            self.assertEqual("NONE", alternative.guard.queue_status_is)
            self.assertIs(alternative.decision.queue_op, QueueLaneOp.SET)
        for program in self.result.programs[5:]:
            selector = program.selector
            ops = {
                alternative.decision.queue_op
                for alternative in selector.block_alternatives
            }
            self.assertFalse(
                QueueLaneOp.SET in ops and QueueLaneOp.CANCEL in ops
            )
        self.assertTrue(
            any(
                {"hp-route:hp80_100:p0047", "hp-route:hp50_65:p0042"}
                <= _route_refs(row)
                for row in self.result.programs[5:]
            )
        )
        self.assertTrue(
            any(
                {"hp-route:hp65_80:p0159", "hp-route:hp35_50:p0262"}
                <= _route_refs(row)
                for row in self.result.programs[5:]
            )
        )

    def test_parent_composition_and_wire_round_trip_are_preserved(self) -> None:
        parent_selector = self.parent.selector
        representatives = (
            self.result.programs[5],
            next(
                row
                for row in self.result.programs[5:]
                if "hp-router-family:HP_PAIR_CANCEL" in row.source_refs
            ),
            self.result.programs[-1],
        )
        for program in representatives:
            selector = program.selector
            self.assertEqual(
                parent_selector.terminal_alternatives,
                selector.terminal_alternatives,
            )
            self.assertEqual(
                parent_selector.imported_fallback,
                selector.imported_fallback,
            )
            self.assertEqual(
                parent_selector.off_gcd_insertions,
                selector.off_gcd_insertions,
            )
            self.assertEqual(
                program,
                causal_action_program_from_dict_v1(program.to_dict()),
            )

    def test_fresh_seed_protocol_is_disjoint_balanced_and_not_v5(self) -> None:
        protocol = FreshSeedProtocolV1()
        train = protocol.examples("train")
        evaluation = protocol.examples("evaluation")
        self.assertEqual((720_001, 0), train[0])
        self.assertEqual(720_256, train[-1][0])
        self.assertEqual((820_001, 0), evaluation[0])
        self.assertEqual(820_256, evaluation[-1][0])
        self.assertFalse({seed for seed, _ in train} & {seed for seed, _ in evaluation})
        self.assertEqual(
            set(FRESH_ARRIVAL_SCHEDULE_MS_V1),
            {arrival for _, arrival in train},
        )
        counts = Counter(arrival for _, arrival in train)
        self.assertEqual({42, 43}, set(counts.values()))
        self.assertNotIn(520_001, {seed for seed, _ in train})
        self.assertNotIn(620_001, {seed for seed, _ in evaluation})

    def test_prototype_identity_or_observation_drift_fails_closed(self) -> None:
        missing = dict(self.prototypes)
        del missing[262]
        with self.assertRaisesRegex(
            UpperKaraCatHpGuardedSparseRoutingV1Error, "exactly"
        ):
            build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1(
                loadout_id=LOADOUT,
                parent_program=self.parent,
                prototype_programs=missing,
            )

        original = self.prototypes[47]
        selector = original.selector
        first = selector.block_alternatives[0]
        drifted_alternative = replace(
            first,
            guard=replace(
                first.guard,
                estimated_remaining_attackable_gte_ms=1_500,
            ),
        )
        drifted = replace(
            original,
            selector=replace(
                selector, block_alternatives=(drifted_alternative,)
            ),
        )
        panel = dict(self.prototypes)
        panel[47] = drifted
        with self.assertRaisesRegex(
            UpperKaraCatHpGuardedSparseRoutingV1Error,
            "non-v6 observation signal",
        ):
            build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1(
                loadout_id=LOADOUT,
                parent_program=self.parent,
                prototype_programs=panel,
            )

    def test_generator_is_deterministic_and_requires_aligned_inputs(self) -> None:
        generator = UpperKaraCatHpGuardedSparseRoutingGeneratorV1(
            loadout_id=LOADOUT,
            parent_program=self.parent,
            prototype_programs=self.prototypes,
        )
        programs = generator(
            loadout_id=LOADOUT,
            train_examples=(object(), object()),
            train_cases=(object(), object()),
        )
        self.assertEqual(
            [row.program_key() for row in self.result.programs],
            [row.program_key() for row in programs],
        )
        self.assertIn(LOADOUT, generator.results_by_loadout)
        with self.assertRaisesRegex(ValueError, "align"):
            generator(
                loadout_id=LOADOUT,
                train_examples=(object(),),
                train_cases=(),
            )


if __name__ == "__main__":
    unittest.main()
