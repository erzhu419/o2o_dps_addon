"""Bounded HP-observable routing around the exact v5 Cat parent.

The v5 first-wave-arrival report is useful only as a diagnostic: arrival time
is control-plane configuration and is not visible to the policy.  This module
therefore does not accept an arrival value.  It turns the four v5 diagnostic
prototype blocks into candidates guarded by the already predeclared target-HP
grid ``[20, 35, 50, 65, 80]``.  Every new predicate is evaluated from the
current observation and preserves the prototype's attackability, action-ready,
queue-status, and rage predicates.

The current queue/GCD block contract intentionally forbids mixing a queue SET
block with a GCD-coupled queue CANCEL block.  The bounded family consequently
keeps the two structural classes separate.  It covers every single HP-band
edit, every same-class two-band route, and every three-band route made from the
two independently validated CANCEL prototypes.  Candidate zero is the exact
parent object, and the four original v5 prototypes are retained as anchors.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations, product
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .sim_bridge import ActionRef
from .upper_kara_burst_reschedule_grid_v1 import (
    DEFAULT_ARRIVAL_HP_THRESHOLDS_V1,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    exact_cat_fallback_selector_v1,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_hp_guarded_sparse_routing_candidate_set/v1"
HP_ROUTING_SOURCE_REF_V1 = "cat-hp-guarded-sparse-routing/v1:projected"
HP_THRESHOLD_GRID_V1 = DEFAULT_ARRIVAL_HP_THRESHOLDS_V1
V5_PROTOTYPE_INDEXES_V1 = (42, 47, 159, 262)
V5_CANCEL_PROTOTYPE_INDEXES_V1 = (42, 47)
V5_QUEUE_GCD_PROTOTYPE_INDEXES_V1 = (159, 262)
HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1 = 309
FRESH_TRAIN_SEED_START_V1 = 720_001
FRESH_EVALUATION_SEED_START_V1 = 820_001
FRESH_SEED_COUNT_V1 = 256
FRESH_ARRIVAL_SCHEDULE_MS_V1 = (0, 1_000, 3_000, 5_000, 7_000, 9_000)


class UpperKaraCatHpGuardedSparseRoutingV1Error(RuntimeError):
    """The frozen parent or one diagnostic prototype differs from v5."""


@dataclass(frozen=True)
class HpBandV1:
    band_id: str
    lower_bound_pct: int | None
    upper_bound_pct: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.band_id, str) or not self.band_id:
            raise ValueError("band_id must be nonempty")
        for name in ("lower_bound_pct", "upper_bound_pct"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 100
            ):
                raise ValueError(f"{name} must be an integer in [0, 100]")
        if self.lower_bound_pct is None and self.upper_bound_pct is None:
            raise ValueError("an HP band must have at least one bound")
        if (
            self.lower_bound_pct is not None
            and self.upper_bound_pct is not None
            and self.lower_bound_pct >= self.upper_bound_pct
        ):
            raise ValueError("HP band lower bound must be below upper bound")

    def to_dict(self) -> JSONMap:
        return {
            "band_id": self.band_id,
            "target_hp_pct_gte": self.lower_bound_pct,
            "target_hp_pct_lte": self.upper_bound_pct,
        }


HP_BANDS_V1 = (
    HpBandV1("hp00_20", None, 20),
    HpBandV1("hp20_35", 20, 35),
    HpBandV1("hp35_50", 35, 50),
    HpBandV1("hp50_65", 50, 65),
    HpBandV1("hp65_80", 65, 80),
    HpBandV1("hp80_100", 80, None),
)


@dataclass(frozen=True)
class FreshSeedProtocolV1:
    train_seed_start: int = FRESH_TRAIN_SEED_START_V1
    evaluation_seed_start: int = FRESH_EVALUATION_SEED_START_V1
    seed_count: int = FRESH_SEED_COUNT_V1
    arrival_schedule_ms: tuple[int, ...] = FRESH_ARRIVAL_SCHEDULE_MS_V1

    def __post_init__(self) -> None:
        for name in ("train_seed_start", "evaluation_seed_start", "seed_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            not isinstance(self.arrival_schedule_ms, tuple)
            or not self.arrival_schedule_ms
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in self.arrival_schedule_ms
            )
            or len(set(self.arrival_schedule_ms)) != len(
                self.arrival_schedule_ms
            )
        ):
            raise ValueError(
                "arrival_schedule_ms must contain unique nonnegative integers"
            )
        if self.seed_count < 2 * len(self.arrival_schedule_ms):
            raise ValueError(
                "seed_count must give every arrival stratum proposal and "
                "selection-validation examples"
            )
        train = set(range(self.train_seed_start, self.train_seed_start + self.seed_count))
        evaluation = set(
            range(
                self.evaluation_seed_start,
                self.evaluation_seed_start + self.seed_count,
            )
        )
        if train & evaluation:
            raise ValueError("fresh train and evaluation seed ranges overlap")

    def examples(self, split: str) -> tuple[tuple[int, int], ...]:
        if split == "train":
            start = self.train_seed_start
        elif split == "evaluation":
            start = self.evaluation_seed_start
        else:
            raise ValueError("split must be train or evaluation")
        return tuple(
            (
                start + offset,
                self.arrival_schedule_ms[
                    offset % len(self.arrival_schedule_ms)
                ],
            )
            for offset in range(self.seed_count)
        )

    def to_dict(self) -> JSONMap:
        train = self.examples("train")
        evaluation = self.examples("evaluation")
        return {
            "schema": "upper_kara_cat_hp_guarded_fresh_seed_protocol/v1",
            "train_seed_range": [train[0][0], train[-1][0]],
            "evaluation_seed_range": [evaluation[0][0], evaluation[-1][0]],
            "seed_count_per_split": self.seed_count,
            "first_wave_arrival_schedule_ms": list(self.arrival_schedule_ms),
            "training_internal_split": (
                "WITHIN_ARRIVAL_STRATUM_OCCURRENCE_PARITY_"
                "EVEN_PROPOSAL_ODD_SELECTION_VALIDATION"
            ),
            "evaluation_role": "UNTOUCHED_FINAL_HELDOUT",
            "v5_seed_reuse": False,
        }


@dataclass(frozen=True)
class _RouteAssignmentV1:
    band_index: int
    prototype_index: int


def _prototype_program_id(loadout_id: str, index: int) -> str:
    return (
        "cat-burst-sparse-queue-gcd::"
        f"{loadout_id}::{index:04d}"
    )


def _validated_parent_v1(
    parent_program: CausalActionProgramV1,
) -> ImportedFallbackOverlaySelectorV1:
    if not isinstance(parent_program, CausalActionProgramV1):
        raise TypeError("parent_program must be CausalActionProgramV1")
    selector = parent_program.selector
    if not isinstance(selector, ImportedFallbackOverlaySelectorV1):
        raise UpperKaraCatHpGuardedSparseRoutingV1Error(
            "parent program must use ImportedFallbackOverlaySelectorV1"
        )
    if selector.imported_fallback != exact_cat_fallback_selector_v1():
        raise UpperKaraCatHpGuardedSparseRoutingV1Error(
            "parent program must retain the exact Cat fallback"
        )
    if parent_program.origin is not ProgramOriginV1.SEARCHED:
        raise UpperKaraCatHpGuardedSparseRoutingV1Error(
            "parent program must have SEARCHED origin"
        )
    return selector


def _prototype_group_v1(index: int) -> str:
    if index in V5_CANCEL_PROTOTYPE_INDEXES_V1:
        return "CANCEL_GCD"
    if index in V5_QUEUE_GCD_PROTOTYPE_INDEXES_V1:
        return "QUEUE_SET_PLUS_GCD_KEEP"
    raise ValueError(f"unsupported v5 prototype index {index}")


def _non_hp_observation_fields_are_closed_v1(
    guard: ObservableCausalGuardV1,
) -> bool:
    return all(
        getattr(guard, name) is None
        for name in (
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
    )


def _expected_signature_v1(
    alternative: GuardedAlternativeV1,
) -> tuple[Any, ...]:
    guard = alternative.guard
    decision = alternative.decision
    action = decision.queue_action or decision.gcd_action
    return (
        alternative.alternative_id,
        guard.target_index,
        action,
        decision.queue_op,
        guard.rage_gte,
        guard.queue_status_is,
        decision.wait_ms,
    )


def _expected_prototype_signatures_v1() -> Mapping[int, tuple[tuple[Any, ...], ...]]:
    whirlwind = ActionRef(spell_id=1_680)
    bloodthirst = ActionRef(spell_id=23_894)
    slam = ActionRef(spell_id=45_961)
    heroic_strike = ActionRef(spell_id=25_286, tag=1)
    cleave = ActionRef(spell_id=20_569, tag=1)
    queue_rows = (
        (
            "sparse:block:queue:t0:spell20569t1:rage60",
            0,
            cleave,
            QueueLaneOp.SET,
            60.0,
            "NONE",
            1,
        ),
        (
            "sparse:block:queue:t0:spell25286t1:rage60",
            0,
            heroic_strike,
            QueueLaneOp.SET,
            60.0,
            "NONE",
            1,
        ),
        (
            "sparse:block:queue:t1:spell20569t1:rage60",
            1,
            cleave,
            QueueLaneOp.SET,
            60.0,
            "NONE",
            1,
        ),
        (
            "sparse:block:queue:t1:spell25286t1:rage60",
            1,
            heroic_strike,
            QueueLaneOp.SET,
            60.0,
            "NONE",
            1,
        ),
    )
    return {
        42: (
            (
                "sparse:block:gcd:t0:spell1680t0:no-queue",
                0,
                whirlwind,
                QueueLaneOp.CANCEL,
                None,
                None,
                None,
            ),
        ),
        47: (
            (
                "sparse:block:gcd:t1:spell23894t0:no-queue",
                1,
                bloodthirst,
                QueueLaneOp.CANCEL,
                None,
                None,
                None,
            ),
        ),
        159: (
            *queue_rows,
            (
                "sparse:block:gcd:t1:spell45961t0",
                1,
                slam,
                QueueLaneOp.KEEP,
                None,
                None,
                None,
            ),
        ),
        262: (
            (
                "sparse:block:queue:t0:spell25286t1:rage60",
                0,
                heroic_strike,
                QueueLaneOp.SET,
                60.0,
                "NONE",
                1,
            ),
            (
                "sparse:block:queue:t0:spell20569t1:rage60",
                0,
                cleave,
                QueueLaneOp.SET,
                60.0,
                "NONE",
                1,
            ),
            (
                "sparse:block:queue:t1:spell25286t1:rage60",
                1,
                heroic_strike,
                QueueLaneOp.SET,
                60.0,
                "NONE",
                1,
            ),
            (
                "sparse:block:queue:t1:spell20569t1:rage60",
                1,
                cleave,
                QueueLaneOp.SET,
                60.0,
                "NONE",
                1,
            ),
            (
                "sparse:block:gcd:t1:spell23894t0",
                1,
                bloodthirst,
                QueueLaneOp.KEEP,
                None,
                None,
                None,
            ),
        ),
    }


def _validated_prototypes_v1(
    loadout_id: str,
    parent: ImportedFallbackOverlaySelectorV1,
    prototype_programs: Mapping[int, CausalActionProgramV1],
) -> Mapping[int, CausalActionProgramV1]:
    if not isinstance(prototype_programs, Mapping):
        raise TypeError("prototype_programs must be a mapping")
    if set(prototype_programs) != set(V5_PROTOTYPE_INDEXES_V1):
        raise UpperKaraCatHpGuardedSparseRoutingV1Error(
            "prototype_programs must contain exactly 42, 47, 159, and 262"
        )
    expected_signatures = _expected_prototype_signatures_v1()
    resolved: dict[int, CausalActionProgramV1] = {}
    for index in V5_PROTOTYPE_INDEXES_V1:
        program = prototype_programs[index]
        if not isinstance(program, CausalActionProgramV1):
            raise TypeError(f"prototype {index} must be CausalActionProgramV1")
        if program.program_id != _prototype_program_id(loadout_id, index):
            raise UpperKaraCatHpGuardedSparseRoutingV1Error(
                f"prototype {index} has the wrong v5 program identity"
            )
        selector = program.selector
        if not isinstance(
            selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
        ):
            raise UpperKaraCatHpGuardedSparseRoutingV1Error(
                f"prototype {index} is not a burst queue/GCD block"
            )
        if (
            selector.terminal_alternatives != parent.terminal_alternatives
            or selector.imported_fallback != parent.imported_fallback
            or selector.off_gcd_insertions != parent.off_gcd_insertions
            or selector.insertion_order != parent.insertion_order
            or selector.insertion_position != parent.insertion_position
        ):
            raise UpperKaraCatHpGuardedSparseRoutingV1Error(
                f"prototype {index} differs from the exact v5 parent"
            )
        for alternative in selector.block_alternatives:
            guard = alternative.guard
            if (
                guard.false_semantics != SKIP_PLAN
                or guard.target_index is None
                or guard.target_attackable_is is not True
                or guard.target_hp_pct_gte is not None
                or guard.target_hp_pct_lte is not None
                or guard.action_ready is None
                or not _non_hp_observation_fields_are_closed_v1(guard)
            ):
                raise UpperKaraCatHpGuardedSparseRoutingV1Error(
                    f"prototype {index} uses a non-v6 observation signal"
                )
        observed = tuple(
            _expected_signature_v1(row)
            for row in selector.block_alternatives
        )
        if observed != expected_signatures[index]:
            raise UpperKaraCatHpGuardedSparseRoutingV1Error(
                f"prototype {index} block differs from the frozen v5 receipt"
            )
        resolved[index] = program
    return resolved


def validate_upper_kara_cat_hp_guarded_sparse_routing_inputs_v1(
    *,
    loadout_id: str,
    parent_program: CausalActionProgramV1,
    prototype_programs: Mapping[int, CausalActionProgramV1],
) -> None:
    """Validate the frozen parent/prototype panel without expanding routes."""

    if not isinstance(loadout_id, str) or not loadout_id.strip():
        raise ValueError("loadout_id must be nonempty")
    parent = _validated_parent_v1(parent_program)
    _validated_prototypes_v1(loadout_id, parent, prototype_programs)


def _unique_strings_v1(*groups: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
    return tuple(result)


def _guarded_alternative_v1(
    alternative: GuardedAlternativeV1,
    *,
    prototype_index: int,
    band: HpBandV1,
) -> GuardedAlternativeV1:
    guard = alternative.guard
    return GuardedAlternativeV1(
        alternative_id=(
            f"hp-router:p{prototype_index:04d}:{band.band_id}:"
            f"{alternative.alternative_id}"
        ),
        guard=replace(
            guard,
            target_hp_pct_gte=band.lower_bound_pct,
            target_hp_pct_lte=band.upper_bound_pct,
        ),
        decision=alternative.decision,
    )


def _route_family_v1(assignments: Sequence[_RouteAssignmentV1]) -> str:
    if len(assignments) == 1:
        return "HP_SINGLE"
    group = _prototype_group_v1(assignments[0].prototype_index)
    if len(assignments) == 2:
        return (
            "HP_PAIR_CANCEL"
            if group == "CANCEL_GCD"
            else "HP_PAIR_QUEUE_GCD"
        )
    if len(assignments) == 3 and group == "CANCEL_GCD":
        return "HP_TRIPLE_CANCEL"
    raise ValueError("unsupported bounded route assignment")


def _route_program_v1(
    *,
    loadout_id: str,
    ordinal: int,
    parent_program: CausalActionProgramV1,
    parent: ImportedFallbackOverlaySelectorV1,
    prototypes: Mapping[int, CausalActionProgramV1],
    assignments: Sequence[_RouteAssignmentV1],
) -> CausalActionProgramV1:
    if not assignments:
        raise ValueError("route assignments cannot be empty")
    if len({row.band_index for row in assignments}) != len(assignments):
        raise ValueError("route assignments must use distinct HP bands")
    groups = {_prototype_group_v1(row.prototype_index) for row in assignments}
    if len(groups) != 1:
        raise ValueError("one route cannot mix CANCEL and queue-SET blocks")
    # Higher-HP bands are evaluated first when two inclusive bounds meet at
    # one of the predeclared thresholds.  No control-plane timing participates.
    ordered = tuple(
        sorted(assignments, key=lambda row: row.band_index, reverse=True)
    )
    alternatives: list[GuardedAlternativeV1] = []
    for assignment in ordered:
        band = HP_BANDS_V1[assignment.band_index]
        selector = prototypes[assignment.prototype_index].selector
        assert isinstance(
            selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
        )
        alternatives.extend(
            _guarded_alternative_v1(
                alternative,
                prototype_index=assignment.prototype_index,
                band=band,
            )
            for alternative in selector.block_alternatives
        )
    family = _route_family_v1(assignments)
    route_refs = tuple(
        f"hp-route:{HP_BANDS_V1[row.band_index].band_id}:p{row.prototype_index:04d}"
        for row in ordered
    )
    prototype_refs = tuple(
        f"v5-prototype-program:{prototypes[index].program_id}"
        for index in sorted({row.prototype_index for row in assignments})
    )
    return CausalActionProgramV1(
        program_id=(
            f"cat-hp-guarded-sparse-router::{loadout_id}::{ordinal:04d}"
        ),
        selector=ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=parent.terminal_alternatives,
            imported_fallback=parent.imported_fallback,
            block_alternatives=tuple(alternatives),
            off_gcd_insertions=parent.off_gcd_insertions,
            insertion_order=parent.insertion_order,
            insertion_position=parent.insertion_position,
        ),
        origin=ProgramOriginV1.SEARCHED,
        source_refs=_unique_strings_v1(
            parent_program.source_refs,
            (
                HP_ROUTING_SOURCE_REF_V1,
                f"parent-program:{parent_program.program_id}",
                f"hp-router-family:{family}",
            ),
            prototype_refs,
            route_refs,
        ),
    )


def _bounded_route_assignments_v1() -> tuple[tuple[_RouteAssignmentV1, ...], ...]:
    result: list[tuple[_RouteAssignmentV1, ...]] = []
    band_indexes = range(len(HP_BANDS_V1))
    for band_index in band_indexes:
        for prototype_index in V5_PROTOTYPE_INDEXES_V1:
            result.append((_RouteAssignmentV1(band_index, prototype_index),))
    for prototype_group in (
        V5_CANCEL_PROTOTYPE_INDEXES_V1,
        V5_QUEUE_GCD_PROTOTYPE_INDEXES_V1,
    ):
        for selected_bands in combinations(band_indexes, 2):
            for selected_prototypes in product(prototype_group, repeat=2):
                result.append(
                    tuple(
                        _RouteAssignmentV1(band_index, prototype_index)
                        for band_index, prototype_index in zip(
                            selected_bands,
                            selected_prototypes,
                            strict=True,
                        )
                    )
                )
    for selected_bands in combinations(band_indexes, 3):
        for selected_prototypes in product(
            V5_CANCEL_PROTOTYPE_INDEXES_V1, repeat=3
        ):
            result.append(
                tuple(
                    _RouteAssignmentV1(band_index, prototype_index)
                    for band_index, prototype_index in zip(
                        selected_bands,
                        selected_prototypes,
                        strict=True,
                    )
                )
            )
    return tuple(result)


@dataclass(frozen=True)
class UpperKaraCatHpGuardedSparseRoutingCandidateSetV1:
    loadout_id: str
    parent_program: CausalActionProgramV1
    prototype_programs: tuple[CausalActionProgramV1, ...]
    programs: tuple[CausalActionProgramV1, ...]
    family_counts: Mapping[str, int]
    guide_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.loadout_id, str) or not self.loadout_id:
            raise ValueError("loadout_id must be nonempty")
        if not self.programs or self.programs[0] is not self.parent_program:
            raise ValueError("first program must be the exact parent object")
        if self.programs[1:5] != self.prototype_programs:
            raise ValueError("v5 prototype anchors must immediately follow zero")
        ids = [row.program_id for row in self.programs]
        keys = [row.program_key() for row in self.programs]
        if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
            raise ValueError("candidate programs must have unique identities")
        if len(self.programs) != HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1:
            raise ValueError(
                "the bounded v6 family must contain "
                f"{HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1} programs"
            )
        if sum(self.family_counts.values()) != len(self.programs):
            raise ValueError("family_counts do not cover all programs")

    def to_dict(self) -> JSONMap:
        compatible_full_space = 3 ** len(HP_BANDS_V1) * 2 - 1
        unconstrained_full_space = 5 ** len(HP_BANDS_V1)
        return {
            "schema": SCHEMA,
            "loadout_id": self.loadout_id,
            "program_count": len(self.programs),
            "paired_zero_program_id": self.parent_program.program_id,
            "paired_zero_program_key": self.parent_program.program_key(),
            "v5_prototype_program_ids": [
                row.program_id for row in self.prototype_programs
            ],
            "hp_threshold_grid": list(HP_THRESHOLD_GRID_V1),
            "hp_bands": [row.to_dict() for row in HP_BANDS_V1],
            "family_counts": dict(self.family_counts),
            "proposal_guide_ids": list(self.guide_ids),
            "program_ids": [row.program_id for row in self.programs],
            "search_space": {
                "emitted_bounded_programs": len(self.programs),
                "same_selector_class_full_assignments": compatible_full_space,
                "unconstrained_zero_or_four_prototypes_per_band": (
                    unconstrained_full_space
                ),
                "mixed_cancel_and_queue_set_routes_emitted": False,
            },
            "fresh_seed_protocol": FreshSeedProtocolV1().to_dict(),
            "contract": {
                "paired_zero_is_exact_v5_parent_object": True,
                "v5_prototype_anchors_preserved": True,
                "routing_uses_current_target_hp_only": True,
                "prototype_attackability_action_ready_queue_rage_preserved": True,
                "first_wave_arrival_visible_to_policy": False,
                "future_or_terminal_rollout_fields_used": [],
                "hp_thresholds_predeclared_before_fresh_campaign": True,
                "inclusive_threshold_tie_order": "HIGHER_HP_BAND_FIRST",
            },
        }


def build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1(
    *,
    loadout_id: str,
    parent_program: CausalActionProgramV1,
    prototype_programs: Mapping[int, CausalActionProgramV1],
) -> UpperKaraCatHpGuardedSparseRoutingCandidateSetV1:
    """Build the fixed 309-program v6 development family."""

    if not isinstance(loadout_id, str) or not loadout_id.strip():
        raise ValueError("loadout_id must be nonempty")
    if tuple(HP_THRESHOLD_GRID_V1) != (20, 35, 50, 65, 80):
        raise AssertionError("the predeclared HP threshold grid drifted")
    parent = _validated_parent_v1(parent_program)
    prototypes = _validated_prototypes_v1(
        loadout_id, parent, prototype_programs
    )
    ordered_prototypes = tuple(
        prototypes[index] for index in V5_PROTOTYPE_INDEXES_V1
    )
    programs: list[CausalActionProgramV1] = [
        parent_program,
        *ordered_prototypes,
    ]
    families = Counter(
        {"PAIRED_CAT_ZERO": 1, "V5_PROTOTYPE_ANCHOR": 4}
    )
    for assignments in _bounded_route_assignments_v1():
        family = _route_family_v1(assignments)
        programs.append(
            _route_program_v1(
                loadout_id=loadout_id,
                ordinal=len(programs),
                parent_program=parent_program,
                parent=parent,
                prototypes=prototypes,
                assignments=assignments,
            )
        )
        families[family] += 1
    guide_ids = tuple(
        value.removeprefix("proposal-guide:")
        for value in parent_program.source_refs
        if value.startswith("proposal-guide:")
    )
    return UpperKaraCatHpGuardedSparseRoutingCandidateSetV1(
        loadout_id=loadout_id,
        parent_program=parent_program,
        prototype_programs=ordered_prototypes,
        programs=tuple(programs),
        family_counts=dict(families),
        guide_ids=guide_ids,
    )


class UpperKaraCatHpGuardedSparseRoutingGeneratorV1:
    """Train-worker-compatible deterministic v6 candidate provider."""

    def __init__(
        self,
        *,
        loadout_id: str,
        parent_program: CausalActionProgramV1,
        prototype_programs: Mapping[int, CausalActionProgramV1],
    ) -> None:
        self.loadout_id = loadout_id
        self.parent_program = parent_program
        self.prototype_programs = dict(prototype_programs)
        self.results_by_loadout: dict[
            str, UpperKaraCatHpGuardedSparseRoutingCandidateSetV1
        ] = {}

    def __call__(
        self,
        *,
        loadout_id: str,
        train_examples: Sequence[Any],
        train_cases: Sequence[Any],
    ) -> tuple[CausalActionProgramV1, ...]:
        if loadout_id != self.loadout_id:
            raise ValueError("requested loadout differs from the frozen v6 loadout")
        if len(train_examples) != len(train_cases):
            raise ValueError("train_examples and train_cases must align")
        result = build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1(
            loadout_id=loadout_id,
            parent_program=self.parent_program,
            prototype_programs=self.prototype_programs,
        )
        self.results_by_loadout[loadout_id] = result
        return result.programs


__all__ = (
    "FRESH_ARRIVAL_SCHEDULE_MS_V1",
    "FRESH_EVALUATION_SEED_START_V1",
    "FRESH_SEED_COUNT_V1",
    "FRESH_TRAIN_SEED_START_V1",
    "FreshSeedProtocolV1",
    "HP_BANDS_V1",
    "HP_GUARDED_SPARSE_ROUTING_PROGRAM_COUNT_V1",
    "HP_ROUTING_SOURCE_REF_V1",
    "HP_THRESHOLD_GRID_V1",
    "SCHEMA",
    "UpperKaraCatHpGuardedSparseRoutingCandidateSetV1",
    "UpperKaraCatHpGuardedSparseRoutingGeneratorV1",
    "UpperKaraCatHpGuardedSparseRoutingV1Error",
    "V5_CANCEL_PROTOTYPE_INDEXES_V1",
    "V5_PROTOTYPE_INDEXES_V1",
    "V5_QUEUE_GCD_PROTOTYPE_INDEXES_V1",
    "build_upper_kara_cat_hp_guarded_sparse_routing_candidate_set_v1",
    "validate_upper_kara_cat_hp_guarded_sparse_routing_inputs_v1",
)
