"""Bounded causal action-program proposals for continuous two-wave training.

This module expands a native Fury action snapshot into complete, reactive
``OrderedGuardSelectorV1`` programs.  It does not evaluate them and it does not
wrap Cat or Contra: the train/eval driver scores those imported reactive
incumbents separately through the same replay interface.

Expert and offline inputs are proposal-order guides only.  The native snapshot
owns ordinary GCD and next-swing membership, while
``build_upper_kara_burst_reschedule_grid_v1`` owns the target/action/HP burst
grid.  Every emitted predicate is a current-observation causal guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import chain, permutations, product
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    OptionalOffGcdPrefixV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_burst_reschedule_grid_v1 import (
    DEFAULT_ARRIVAL_HP_THRESHOLDS_V1,
    BurstRescheduleCandidateV1,
    build_upper_kara_burst_reschedule_grid_v1,
)
from .upper_kara_two_wave_burst_case_v1 import SCHEMA as TWO_WAVE_CASE_SCHEMA
from .wave_action_schedule_v1 import QueueLaneOp
from .wave_action_sequence_search_v1 import (
    ActionGuideV1,
    ScheduleReplayOutcomeV1,
)
from .wave_action_sequence_pilot_v1 import DEFAULT_BRIDGE, DEFAULT_BRIDGE_CWD


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_causal_program_candidate_set/v1"
DEFAULT_QUEUE_RAGE_THRESHOLDS_V1 = (30, 45, 60, 75)
DEFAULT_ORDINARY_OFF_GCD_RAGE_LTE_THRESHOLDS_V1 = (0, 20, 40, 60)
SUNDER_ARMOR_ACTION_V1 = ActionRef(spell_id=11_597)
SUNDER_ARMOR_MAX_STACKS_V1 = 5
FURY_STANCE_ACTIONS_V1 = frozenset({
    ActionRef(spell_id=71),
    ActionRef(spell_id=2_457),
    ActionRef(spell_id=2_458),
})


class NativeSnapshotLoaderV1(Protocol):
    def __call__(
        self, case: DevelopmentPrecombatWaveCaseV1
    ) -> Sequence[AvailableAction]: ...


class ProposalPriorityProviderV1(Protocol):
    guide_id: str

    def action_priorities(
        self,
        case: DevelopmentPrecombatWaveCaseV1,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> Mapping[ActionRef, float]: ...


class ProposalGuideFactoryV1(Protocol):
    """Build case-bound proposal guides from the training panel only."""

    def __call__(
        self,
        train_cases: tuple[DevelopmentPrecombatWaveCaseV1, ...],
    ) -> Sequence[ProposalPriorityProviderV1]: ...


@dataclass(frozen=True)
class CallableProgramProposalGuideV1:
    """Adapter for a static expert/offline proposal-order projection."""

    guide_id: str
    provider: Callable[
        [DevelopmentPrecombatWaveCaseV1, tuple[AvailableAction, ...]],
        Mapping[ActionRef, float],
    ]

    def __post_init__(self) -> None:
        if not isinstance(self.guide_id, str) or not self.guide_id.strip():
            raise ValueError("guide_id must be nonempty")
        if not callable(self.provider):
            raise TypeError("provider must be callable")

    def action_priorities(
        self,
        case: DevelopmentPrecombatWaveCaseV1,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> Mapping[ActionRef, float]:
        return self.provider(case, native_snapshot)


TrainingOutcomeProviderV1 = Callable[
    [DevelopmentPrecombatWaveCaseV1, tuple[AvailableAction, ...]],
    ScheduleReplayOutcomeV1,
]


class WaveActionGuideProgramProposalAdapterV1:
    """Use an existing offline/expert wave guide to rank program proposals.

    ``ActionGuideV1`` needs a replay frontier.  The caller therefore supplies
    a train-only outcome provider rather than this adapter inventing an offline
    observation or consulting a held-out case.  Returned priorities remain a
    ranking hint; native snapshot membership is unchanged.
    """

    def __init__(
        self,
        wave_action_guide: ActionGuideV1,
        training_outcome_provider: TrainingOutcomeProviderV1,
    ) -> None:
        guide_id = getattr(wave_action_guide, "guide_id", None)
        priorities = getattr(wave_action_guide, "action_priorities", None)
        if not isinstance(guide_id, str) or not guide_id.strip():
            raise TypeError("wave_action_guide must expose a nonempty guide_id")
        if not callable(priorities):
            raise TypeError("wave_action_guide must expose action_priorities")
        if not callable(training_outcome_provider):
            raise TypeError("training_outcome_provider must be callable")
        self.guide_id = f"wave-action-guide:{guide_id.strip()}"
        self._wave_action_guide = wave_action_guide
        self._training_outcome_provider = training_outcome_provider

    def action_priorities(
        self,
        case: DevelopmentPrecombatWaveCaseV1,
        native_snapshot: tuple[AvailableAction, ...],
    ) -> Mapping[ActionRef, float]:
        outcome = self._training_outcome_provider(case, native_snapshot)
        if not isinstance(outcome, ScheduleReplayOutcomeV1):
            raise TypeError(
                "training_outcome_provider must return ScheduleReplayOutcomeV1"
            )
        return self._wave_action_guide.action_priorities(outcome, ())


def adapt_wave_action_guides_for_program_proposals_v1(
    wave_action_guides: Sequence[ActionGuideV1],
    training_outcome_provider: TrainingOutcomeProviderV1,
) -> tuple[WaveActionGuideProgramProposalAdapterV1, ...]:
    """Bind existing Cat/Contra/offline proposal guides to train frontiers."""

    return tuple(
        WaveActionGuideProgramProposalAdapterV1(
            guide, training_outcome_provider
        )
        for guide in wave_action_guides
    )


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _unique_ints(
    values: Iterable[int], label: str, *, minimum: int, maximum: int
) -> tuple[int, ...]:
    rows = tuple(values)
    if not rows or any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
        for value in rows
    ):
        raise ValueError(
            f"{label} must contain integers in [{minimum}, {maximum}]"
        )
    if len(rows) != len(set(rows)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(rows))


@dataclass(frozen=True)
class UpperKaraCausalProgramGenerationConfigV1:
    """Finite proposal budget shared by local smoke and remote expansion."""

    max_programs: int = 512
    max_gcd_priority_orders: int = 6
    max_gcd_order_assignments: int = 12
    max_burst_priority_orders: int = 4
    max_burst_order_assignments: int = 8
    max_burst_allocations: int = 128
    max_ordinary_off_gcd_priority_orders: int = 4
    max_ordinary_off_gcd_order_assignments: int = 8
    max_ordinary_off_gcd_allocations: int = 64
    hp_thresholds: tuple[int, ...] = DEFAULT_ARRIVAL_HP_THRESHOLDS_V1
    precombat_relative_times_ms: tuple[int, ...] = (-2_500, -1_500, -500)
    queue_rage_thresholds: tuple[int, ...] = DEFAULT_QUEUE_RAGE_THRESHOLDS_V1
    ordinary_off_gcd_rage_lte_thresholds: tuple[int, ...] = (
        DEFAULT_ORDINARY_OFF_GCD_RAGE_LTE_THRESHOLDS_V1
    )
    fallback_wait_ms: int = 250
    queue_or_off_gcd_wait_ms: int = 1
    include_no_queue_programs: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_programs",
            "max_gcd_priority_orders",
            "max_gcd_order_assignments",
            "max_burst_priority_orders",
            "max_burst_order_assignments",
            "max_burst_allocations",
            "max_ordinary_off_gcd_priority_orders",
            "max_ordinary_off_gcd_order_assignments",
            "max_ordinary_off_gcd_allocations",
            "fallback_wait_ms",
            "queue_or_off_gcd_wait_ms",
        ):
            _positive_int(getattr(self, name), name)
        object.__setattr__(
            self,
            "hp_thresholds",
            _unique_ints(
                self.hp_thresholds,
                "hp_thresholds",
                minimum=1,
                maximum=100,
            ),
        )
        object.__setattr__(
            self,
            "queue_rage_thresholds",
            _unique_ints(
                self.queue_rage_thresholds,
                "queue_rage_thresholds",
                minimum=0,
                maximum=100,
            ),
        )
        object.__setattr__(
            self,
            "ordinary_off_gcd_rage_lte_thresholds",
            _unique_ints(
                self.ordinary_off_gcd_rage_lte_thresholds,
                "ordinary_off_gcd_rage_lte_thresholds",
                minimum=0,
                maximum=100,
            ),
        )
        precombat = tuple(self.precombat_relative_times_ms)
        if not precombat or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -3_000 <= value < 0
            for value in precombat
        ):
            raise ValueError(
                "precombat_relative_times_ms must contain integers in [-3000, 0)"
            )
        if len(precombat) != len(set(precombat)):
            raise ValueError("precombat_relative_times_ms must be unique")
        object.__setattr__(
            self, "precombat_relative_times_ms", tuple(sorted(precombat))
        )
        if not isinstance(self.include_no_queue_programs, bool):
            raise TypeError("include_no_queue_programs must be boolean")

    def to_dict(self) -> JSONMap:
        return {
            "max_programs": self.max_programs,
            "max_gcd_priority_orders": self.max_gcd_priority_orders,
            "max_gcd_order_assignments": self.max_gcd_order_assignments,
            "max_burst_priority_orders": self.max_burst_priority_orders,
            "max_burst_order_assignments": self.max_burst_order_assignments,
            "max_burst_allocations": self.max_burst_allocations,
            "max_ordinary_off_gcd_priority_orders": (
                self.max_ordinary_off_gcd_priority_orders
            ),
            "max_ordinary_off_gcd_order_assignments": (
                self.max_ordinary_off_gcd_order_assignments
            ),
            "max_ordinary_off_gcd_allocations": (
                self.max_ordinary_off_gcd_allocations
            ),
            "hp_thresholds": list(self.hp_thresholds),
            "precombat_relative_times_ms": list(
                self.precombat_relative_times_ms
            ),
            "queue_rage_thresholds": list(self.queue_rage_thresholds),
            "ordinary_off_gcd_rage_lte_thresholds": list(
                self.ordinary_off_gcd_rage_lte_thresholds
            ),
            "fallback_wait_ms": self.fallback_wait_ms,
            "queue_or_off_gcd_wait_ms": self.queue_or_off_gcd_wait_ms,
            "include_no_queue_programs": self.include_no_queue_programs,
        }


@dataclass(frozen=True)
class UpperKaraCausalProgramCandidateSetV1:
    loadout_id: str
    training_seed_labels: tuple[int, ...]
    programs: tuple[CausalActionProgramV1, ...]
    native_snapshot: tuple[AvailableAction, ...]
    gcd_actions: tuple[ActionRef, ...]
    queue_actions: tuple[ActionRef, ...]
    burst_actions: tuple[ActionRef, ...]
    excluded_burst_actions: tuple[ActionRef, ...]
    ordinary_off_gcd_actions: tuple[ActionRef, ...]
    stance_actions_requiring_transition_grammar: tuple[ActionRef, ...]
    guide_ids: tuple[str, ...]
    ignored_guide_action_count: int
    factor_option_counts: Mapping[str, int]
    represented_factor_option_counts: Mapping[str, int]
    latest_train_first_wave_arrival_ms: int
    minimum_idle_decisions_before_latest_arrival: int
    recommended_max_replay_decisions: int
    config: UpperKaraCausalProgramGenerationConfigV1

    def __post_init__(self) -> None:
        if not self.programs:
            raise ValueError("candidate set must contain at least one program")
        keys = [program.program_key() for program in self.programs]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate programs must be semantically unique")

    def to_dict(self) -> JSONMap:
        def actions(values: Sequence[ActionRef]) -> list[JSONMap]:
            return [value.to_wire() for value in values]

        option_complete = all(
            self.represented_factor_option_counts.get(name) == count
            for name, count in self.factor_option_counts.items()
        )
        return {
            "schema": SCHEMA,
            "loadout_id": self.loadout_id,
            "training_seed_labels": list(self.training_seed_labels),
            "program_count": len(self.programs),
            "programs": [program.to_dict() for program in self.programs],
            "native_snapshot_action_count": len(self.native_snapshot),
            "gcd_actions": actions(self.gcd_actions),
            "queue_actions": actions(self.queue_actions),
            "burst_actions": actions(self.burst_actions),
            "excluded_burst_actions_absent_from_native_snapshot": actions(
                self.excluded_burst_actions
            ),
            "ordinary_off_gcd_actions": actions(self.ordinary_off_gcd_actions),
            "stance_actions_requiring_transition_grammar": actions(
                self.stance_actions_requiring_transition_grammar
            ),
            "guide_ids": list(self.guide_ids),
            "ignored_guide_action_count": self.ignored_guide_action_count,
            "factor_option_counts": dict(self.factor_option_counts),
            "represented_factor_option_counts": dict(
                self.represented_factor_option_counts
            ),
            "all_factor_options_represented_within_budget": option_complete,
            "decision_budget": {
                "latest_train_first_wave_arrival_ms": (
                    self.latest_train_first_wave_arrival_ms
                ),
                "minimum_idle_decisions_before_latest_arrival": (
                    self.minimum_idle_decisions_before_latest_arrival
                ),
                "recommended_max_replay_decisions": (
                    self.recommended_max_replay_decisions
                ),
                "fallback_wait_ms": self.config.fallback_wait_ms,
                "rationale": (
                    f"positive {self.config.fallback_wait_ms}ms idle epochs "
                    "keep the latest configured arrival below the driver "
                    "decision budget while remaining responsive"
                ),
            },
            "config": self.config.to_dict(),
            "grammar_limitations": [
                {
                    "code": "ATOMIC_QUEUE_AND_SEPARATELY_READY_GCD_NOT_REPRESENTABLE_V1",
                    "effect": (
                        "queue alternatives use a guarded queue set followed by "
                        "a positive wait; a later epoch independently selects a "
                        "ready GCD"
                    ),
                    "fabricated_combined_guard_used": False,
                },
                {
                    "code": "STANCE_STATE_AND_PAIRED_TRANSITION_GRAMMAR_REQUIRED",
                    "actions": actions(
                        self.stance_actions_requiring_transition_grammar
                    ),
                    "effect": (
                        "Battle, Defensive, and Berserker Stance are not "
                        "blindly rotated as ordinary off-GCD resources"
                    ),
                    "required_before_search": [
                        "current_stance_observation",
                        "tactical_mastery_rage_retention",
                        "paired_stance_transition_outcome",
                    ],
                },
            ],
            "contract": {
                "artifact_role": "CANDIDATE_GENERATION_ONLY",
                "search_or_evaluation_performed_here": False,
                "program_kind": "ORDERED_CURRENT_OBSERVATION_ALTERNATIVES",
                "fixed_timestamp_schedule": False,
                "future_fields_used": [],
                "native_snapshot_owns_ordinary_action_membership": True,
                "burst_grid_owns_target_action_hp_membership": True,
                "guides_can_remove_native_actions": False,
                "guides_rank_or_propose_only": True,
                "proposal_guides_supplied": list(self.guide_ids),
                "imported_incumbents_included": False,
                "driver_must_score_imported_incumbents_separately": True,
                "unconditional_positive_wait_fallback": True,
                "evaluation_examples_consumed": False,
                "candidate_generation_axes": {
                    "target_specific_gcd_priority_order": True,
                    "capped_target_debuff_refresh_suppression": True,
                    "next_swing_queue_action_and_rage_threshold": True,
                    "ordinary_off_gcd_resource_use_or_skip": True,
                    "ordinary_off_gcd_target_assignment": True,
                    "ordinary_off_gcd_target_hp_lower_bound": True,
                    "ordinary_off_gcd_rage_upper_bound": True,
                    "ordinary_off_gcd_cross_wave_reschedule_chain": True,
                    "per_burst_resource_use_or_skip": True,
                    "per_burst_resource_strict_precombat_time": True,
                    "per_burst_resource_target_assignment": True,
                    "per_burst_resource_hp_threshold": True,
                    "per_target_burst_action_order": True,
                    "cross_wave_reschedule_chain": True,
                },
            },
        }


def _action_token(action: ActionRef) -> str:
    if action.spell_id:
        return f"spell{action.spell_id}t{action.tag}"
    if action.item_id:
        return f"item{action.item_id}t{action.tag}"
    return f"other{action.other_id}t{action.tag}"


@dataclass(frozen=True)
class _BurstActionAllocationV1:
    """One action's planned causal opportunities across the route."""

    precombat_relative_ms: int | None = None
    target_thresholds: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if self.precombat_relative_ms is not None and (
            isinstance(self.precombat_relative_ms, bool)
            or not isinstance(self.precombat_relative_ms, int)
            or self.precombat_relative_ms >= 0
        ):
            raise ValueError("precombat allocation must be at a negative pull-relative time")
        if self.precombat_relative_ms is not None and self.target_thresholds:
            raise ValueError("one finite resource cannot be both precombat and in-combat")
        targets = [row[0] for row in self.target_thresholds]
        if len(targets) != len(set(targets)):
            raise ValueError("burst allocation cannot repeat a target")
        if any(
            type(target) is not int
            or target < 0
            or type(threshold) is not int
            or not 1 <= threshold <= 100
            for target, threshold in self.target_thresholds
        ):
            raise ValueError("burst target/threshold allocation is invalid")

    @property
    def skipped(self) -> bool:
        return self.precombat_relative_ms is None and not self.target_thresholds


def _burst_allocation_options_v1(
    actions: tuple[ActionRef, ...],
    target_indexes: tuple[int, ...],
    precombat_actions: frozenset[ActionRef],
    config: UpperKaraCausalProgramGenerationConfigV1,
) -> tuple[tuple[_BurstActionAllocationV1, ...], ...]:
    """Coverage-first action-specific use/skip/time/target/HP allocations."""

    skipped = _BurstActionAllocationV1()
    proposals: list[tuple[_BurstActionAllocationV1, ...]] = [
        (skipped,) * len(actions)
    ]

    def single(
        action_index: int, allocation: _BurstActionAllocationV1
    ) -> tuple[_BurstActionAllocationV1, ...]:
        row = [skipped] * len(actions)
        row[action_index] = allocation
        return tuple(row)

    # Interleave resources within each axis.  A bounded prefix therefore still
    # covers every native resource instead of exhausting all options for the
    # lexicographically first action.
    for relative_ms in config.precombat_relative_times_ms:
        for action_index in range(len(actions)):
            if actions[action_index] not in precombat_actions:
                continue
            proposals.append(single(
                action_index,
                _BurstActionAllocationV1(precombat_relative_ms=relative_ms),
            ))
    for target_index in target_indexes:
        for threshold in config.hp_thresholds:
            for action_index in range(len(actions)):
                proposals.append(single(
                    action_index,
                    _BurstActionAllocationV1(
                        target_thresholds=((target_index, threshold),)
                    ),
                ))
    if len(target_indexes) > 1:
        threshold_pairs = [
            (threshold, threshold) for threshold in config.hp_thresholds
        ]
        threshold_pairs.extend((
            (config.hp_thresholds[0], config.hp_thresholds[-1]),
            (config.hp_thresholds[-1], config.hp_thresholds[0]),
        ))
        for pair in threshold_pairs:
            for action_index in range(len(actions)):
                proposals.append(single(
                    action_index,
                    _BurstActionAllocationV1(
                        target_thresholds=tuple(
                            (target, pair[index])
                            for index, target in enumerate(target_indexes[:2])
                        )
                    ),
                ))

    # Uniform packages expose whole-package timing/order hypotheses.
    for relative_ms in config.precombat_relative_times_ms:
        allocation = _BurstActionAllocationV1(
            precombat_relative_ms=relative_ms
        )
        proposals.append(tuple(
            allocation if action in precombat_actions else skipped
            for action in actions
        ))
    for target_index in target_indexes:
        for threshold in config.hp_thresholds:
            allocation = _BurstActionAllocationV1(
                target_thresholds=((target_index, threshold),)
            )
            proposals.append((allocation,) * len(actions))
    if len(target_indexes) > 1:
        for threshold in config.hp_thresholds:
            allocation = _BurstActionAllocationV1(
                target_thresholds=tuple(
                    (target, threshold) for target in target_indexes
                )
            )
            proposals.append((allocation,) * len(actions))

        # Explicit mixed allocations make one resource a wave-1 choice and a
        # different resource a wave-2 choice (e.g. DW first, Rapid second).
        middle = config.hp_thresholds[len(config.hp_thresholds) // 2]
        for first_action in range(len(actions)):
            if actions[first_action] not in precombat_actions:
                continue
            for second_action in range(len(actions)):
                if first_action == second_action:
                    continue
                row = [skipped] * len(actions)
                row[first_action] = _BurstActionAllocationV1(
                    target_thresholds=((target_indexes[0], middle),)
                )
                row[second_action] = _BurstActionAllocationV1(
                    target_thresholds=((target_indexes[1], middle),)
                )
                proposals.append(tuple(row))
        # Likewise allow a planned pre-pull resource beside a separately held
        # second-wave resource without conditioning on future arrival/death.
        precombat_at = config.precombat_relative_times_ms[0]
        for first_action in range(len(actions)):
            for second_action in range(len(actions)):
                if first_action == second_action:
                    continue
                row = [skipped] * len(actions)
                row[first_action] = _BurstActionAllocationV1(
                    precombat_relative_ms=precombat_at
                )
                row[second_action] = _BurstActionAllocationV1(
                    target_thresholds=((target_indexes[1], middle),)
                )
                proposals.append(tuple(row))

    result: list[tuple[_BurstActionAllocationV1, ...]] = []
    for proposal in proposals:
        if proposal not in result:
            result.append(proposal)
        if len(result) >= config.max_burst_allocations:
            break
    return tuple(result)


@dataclass(frozen=True)
class _OrdinaryOffGcdActionAllocationV1:
    """Current-target HP and rage gates for one native off-GCD action."""

    target_hp_rage: tuple[tuple[int, int, int], ...] = ()

    def __post_init__(self) -> None:
        targets = [row[0] for row in self.target_hp_rage]
        if len(targets) != len(set(targets)):
            raise ValueError("ordinary off-GCD allocation cannot repeat a target")
        if any(
            type(target) is not int
            or target < 0
            or type(hp_threshold) is not int
            or not 1 <= hp_threshold <= 100
            or type(rage_threshold) is not int
            or not 0 <= rage_threshold <= 100
            for target, hp_threshold, rage_threshold in self.target_hp_rage
        ):
            raise ValueError(
                "ordinary off-GCD target/HP/rage allocation is invalid"
            )

    @property
    def skipped(self) -> bool:
        return not self.target_hp_rage


def _ordinary_off_gcd_allocation_options_v1(
    actions: tuple[ActionRef, ...],
    target_indexes: tuple[int, ...],
    config: UpperKaraCausalProgramGenerationConfigV1,
) -> tuple[tuple[_OrdinaryOffGcdActionAllocationV1, ...], ...]:
    """Coverage-first per-resource use/target/HP/rage/reschedule options."""

    if not actions:
        return ((),)
    skipped = _OrdinaryOffGcdActionAllocationV1()
    proposals: list[tuple[_OrdinaryOffGcdActionAllocationV1, ...]] = [
        (skipped,) * len(actions)
    ]

    def single(
        action_index: int,
        allocation: _OrdinaryOffGcdActionAllocationV1,
    ) -> tuple[_OrdinaryOffGcdActionAllocationV1, ...]:
        row = [skipped] * len(actions)
        row[action_index] = allocation
        return tuple(row)

    for hp_threshold in config.hp_thresholds:
        for rage_threshold in config.ordinary_off_gcd_rage_lte_thresholds:
            for target_index in target_indexes:
                for action_index in range(len(actions)):
                    proposals.append(single(
                        action_index,
                        _OrdinaryOffGcdActionAllocationV1(
                            target_hp_rage=((
                                target_index,
                                hp_threshold,
                                rage_threshold,
                            ),)
                        ),
                    ))

    if len(target_indexes) > 1:
        gate_pairs = [
            (
                (hp_threshold, rage_threshold),
                (hp_threshold, rage_threshold),
            )
            for hp_threshold in config.hp_thresholds
            for rage_threshold in config.ordinary_off_gcd_rage_lte_thresholds
        ]
        gate_pairs.extend((
            (
                (
                    config.hp_thresholds[0],
                    config.ordinary_off_gcd_rage_lte_thresholds[0],
                ),
                (
                    config.hp_thresholds[-1],
                    config.ordinary_off_gcd_rage_lte_thresholds[-1],
                ),
            ),
            (
                (
                    config.hp_thresholds[-1],
                    config.ordinary_off_gcd_rage_lte_thresholds[-1],
                ),
                (
                    config.hp_thresholds[0],
                    config.ordinary_off_gcd_rage_lte_thresholds[0],
                ),
            ),
        ))
        for pair in gate_pairs:
            for action_index in range(len(actions)):
                proposals.append(single(
                    action_index,
                    _OrdinaryOffGcdActionAllocationV1(
                        target_hp_rage=tuple(
                            (target, *pair[index])
                            for index, target in enumerate(target_indexes[:2])
                        )
                    ),
                ))

    # Multi-action snapshots remain bounded while still receiving package and
    # distinct-wave proposals.  This does not turn guides into membership
    # filters: every native ordinary off-GCD action receives single-action rows.
    middle_rage = config.ordinary_off_gcd_rage_lte_thresholds[
        len(config.ordinary_off_gcd_rage_lte_thresholds) // 2
    ]
    middle_hp = config.hp_thresholds[len(config.hp_thresholds) // 2]
    for target_index in target_indexes:
        allocation = _OrdinaryOffGcdActionAllocationV1(
            target_hp_rage=((target_index, middle_hp, middle_rage),)
        )
        proposals.append((allocation,) * len(actions))
    if len(target_indexes) > 1:
        for first_action in range(len(actions)):
            for second_action in range(len(actions)):
                if first_action == second_action:
                    continue
                row = [skipped] * len(actions)
                row[first_action] = _OrdinaryOffGcdActionAllocationV1(
                    target_hp_rage=((
                        target_indexes[0], middle_hp, middle_rage
                    ),)
                )
                row[second_action] = _OrdinaryOffGcdActionAllocationV1(
                    target_hp_rage=((
                        target_indexes[1], middle_hp, middle_rage
                    ),)
                )
                proposals.append(tuple(row))

    result: list[tuple[_OrdinaryOffGcdActionAllocationV1, ...]] = []
    for proposal in proposals:
        if proposal not in result:
            result.append(proposal)
        if len(result) >= config.max_ordinary_off_gcd_allocations:
            break
    return tuple(result)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_native_snapshot_v1(
    case: DevelopmentPrecombatWaveCaseV1,
    *,
    bridge_path: Path,
    bridge_cwd: Path,
) -> tuple[AvailableAction, ...]:
    with SimulatorBridgePrecombatV1(bridge_path, cwd=bridge_cwd) as bridge:
        bridge.load_dynamic_v3_precombat(
            case.request,
            case.dynamic_load.seed,
            case.dynamic_load.config,
            case.precombat,
        )
        return tuple(bridge.actions())


def _priority_scores_v1(
    cases: Sequence[DevelopmentPrecombatWaveCaseV1],
    snapshot: tuple[AvailableAction, ...],
    guides: Sequence[ProposalPriorityProviderV1],
) -> tuple[dict[ActionRef, float], tuple[str, ...], int]:
    native_actions = {row.action for row in snapshot}
    scores = {action: 0.0 for action in native_actions}
    guide_ids: list[str] = []
    ignored_actions: set[ActionRef] = set()
    for guide in guides:
        guide_id = getattr(guide, "guide_id", None)
        provider = getattr(guide, "action_priorities", None)
        if not isinstance(guide_id, str) or not guide_id.strip():
            raise TypeError("proposal guide must expose a nonempty guide_id")
        if guide_id in guide_ids:
            raise ValueError("proposal guide IDs must be unique")
        if not callable(provider):
            raise TypeError("proposal guide must expose action_priorities")
        for case in cases:
            priorities = provider(case, snapshot)
            if not isinstance(priorities, Mapping):
                raise TypeError("proposal guide priorities must be a mapping")
            for action, value in priorities.items():
                if not isinstance(action, ActionRef):
                    raise TypeError("proposal guide priority keys must be ActionRef")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(
                        "proposal guide priorities must be finite numbers"
                    )
                if action in native_actions:
                    scores[action] += float(value)
                else:
                    ignored_actions.add(action)
        guide_ids.append(guide_id)
    return scores, tuple(guide_ids), len(ignored_actions)


def _bounded_orders_v1(
    values: Sequence[ActionRef],
    *,
    scores: Mapping[ActionRef, float],
    limit: int,
) -> tuple[tuple[ActionRef, ...], ...]:
    items = tuple(sorted(values))
    if not items:
        return ((),)
    ranked = tuple(sorted(items, key=lambda row: (-scores.get(row, 0.0), row)))
    # When guides carry a signal, spend the first bounded proposal slot on
    # their aggregate order.  Native membership is unchanged and the canonical
    # order remains an explicit exploration proposal immediately afterward.
    proposals: list[tuple[ActionRef, ...]] = [ranked, items]
    proposals.extend((tuple(reversed(ranked)), tuple(reversed(items))))
    proposals.extend(items[offset:] + items[:offset] for offset in range(1, len(items)))
    proposals.extend(
        (*items[:index], items[index + 1], items[index], *items[index + 2 :])
        for index in range(len(items) - 1)
    )
    if len(items) <= 7:
        proposals.extend(permutations(items))
    result: list[tuple[ActionRef, ...]] = []
    for proposal in proposals:
        row = tuple(proposal)
        if row not in result:
            result.append(row)
        if len(result) >= limit:
            break
    return tuple(result)


def _bounded_assignments_v1(
    option_count: int,
    slot_count: int,
    limit: int,
) -> tuple[tuple[int, ...], ...]:
    if option_count <= 0 or slot_count <= 0:
        raise ValueError("assignment dimensions must be positive")
    proposals: list[tuple[int, ...]] = []
    proposals.extend((index,) * slot_count for index in range(option_count))
    proposals.extend(product(range(option_count), repeat=slot_count))
    result: list[tuple[int, ...]] = []
    for proposal in proposals:
        row = tuple(proposal)
        if row not in result:
            result.append(row)
        if len(result) >= limit:
            break
    return tuple(result)


def _coverage_first_combinations_v1(
    option_counts: tuple[int, ...],
) -> Iterator[tuple[int, ...]]:
    """Yield coverage mutations then the product without materializing it."""

    anchor = (0,) * len(option_counts)
    seen: set[tuple[int, ...]] = {anchor}
    yield anchor
    # Round-robin dimensions so a bounded prefix covers each axis instead of
    # spending its full budget on the first high-cardinality factor.
    for index in range(1, max(option_counts)):
        for dimension, count in enumerate(option_counts):
            if index >= count:
                continue
            row = list(anchor)
            row[dimension] = index
            proposal = tuple(row)
            if proposal in seen:
                continue
            seen.add(proposal)
            yield proposal
    for proposal in product(*(range(count) for count in option_counts)):
        row = tuple(proposal)
        if row in seen:
            continue
        seen.add(row)
        yield row


def _action_guard_v1(
    target_index: int,
    action: ActionRef,
    *,
    rage_gte: int | None = None,
    queue_status_is: str | None = None,
) -> ObservableCausalGuardV1:
    return ObservableCausalGuardV1(
        rage_gte=rage_gte,
        target_index=target_index,
        target_attackable_is=True,
        target_aura_action=(
            action if action == SUNDER_ARMOR_ACTION_V1 else None
        ),
        target_aura_stacks_lte=(
            SUNDER_ARMOR_MAX_STACKS_V1 - 1
            if action == SUNDER_ARMOR_ACTION_V1
            else None
        ),
        queue_status_is=queue_status_is,
        action_ready=action,
        false_semantics=SKIP_PLAN,
    )


def _burst_alternative_v1(
    candidate: BurstRescheduleCandidateV1,
    native: AvailableAction,
    *,
    wait_ms: int,
) -> GuardedAlternativeV1:
    token = _action_token(candidate.action)
    alternative_id = (
        f"burst:t{candidate.target_index}:{token}:"
        f"hp{candidate.hp_threshold_pct}"
    )
    if native.triggers_gcd:
        decision = ProgramDecisionV1(
            target_index=candidate.target_index,
            start_attack=True,
            gcd_action=candidate.action,
        )
    else:
        decision = ProgramDecisionV1(
            target_index=candidate.target_index,
            start_attack=True,
            optional_off_gcd_prefixes=(
                OptionalOffGcdPrefixV1(candidate.action, candidate.guard),
            ),
            wait_ms=wait_ms,
        )
    return GuardedAlternativeV1(
        alternative_id=alternative_id,
        guard=candidate.guard,
        decision=decision,
    )


def _precombat_burst_alternative_v1(
    *,
    action: ActionRef,
    native: AvailableAction,
    relative_ms: int,
    wait_ms: int,
) -> GuardedAlternativeV1:
    """Emit a strict pull-relative precombat use opportunity."""

    guard = ObservableCausalGuardV1(
        pull_relative_time_gte_ms=relative_ms,
        pull_relative_time_lte_ms=-1,
        action_ready=action,
        false_semantics=SKIP_PLAN,
    )
    if native.triggers_gcd:
        decision = ProgramDecisionV1(gcd_action=action)
    else:
        decision = ProgramDecisionV1(
            optional_off_gcd_prefixes=(
                OptionalOffGcdPrefixV1(action, guard),
            ),
            wait_ms=wait_ms,
        )
    return GuardedAlternativeV1(
        alternative_id=f"precombat:{_action_token(action)}:at{relative_ms}",
        guard=guard,
        decision=decision,
    )


def _ordinary_off_gcd_alternative_v1(
    *,
    target_index: int,
    action: ActionRef,
    hp_gte: int,
    rage_lte: int,
    wait_ms: int,
) -> GuardedAlternativeV1:
    guard = ObservableCausalGuardV1(
        rage_lte=rage_lte,
        target_index=target_index,
        target_hp_pct_gte=hp_gte,
        target_attackable_is=True,
        action_ready=action,
        false_semantics=SKIP_PLAN,
    )
    return GuardedAlternativeV1(
        alternative_id=(
            f"ordinary-off-gcd:t{target_index}:{_action_token(action)}:"
            f"hp{hp_gte}:rage-lte{rage_lte}"
        ),
        guard=guard,
        decision=ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            optional_off_gcd_prefixes=(
                OptionalOffGcdPrefixV1(action, guard),
            ),
            wait_ms=wait_ms,
        ),
    )


def _queue_alternative_v1(
    *,
    target_index: int,
    action: ActionRef,
    rage_threshold: int,
    wait_ms: int,
) -> GuardedAlternativeV1:
    return GuardedAlternativeV1(
        alternative_id=(
            f"queue:t{target_index}:{_action_token(action)}:rage{rage_threshold}"
        ),
        guard=_action_guard_v1(
            target_index,
            action,
            rage_gte=rage_threshold,
            queue_status_is="NONE",
        ),
        decision=ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            queue_op=QueueLaneOp.SET,
            queue_action=action,
            wait_ms=wait_ms,
        ),
    )


def _gcd_alternative_v1(
    *, target_index: int, action: ActionRef
) -> GuardedAlternativeV1:
    return GuardedAlternativeV1(
        alternative_id=f"gcd:t{target_index}:{_action_token(action)}",
        guard=_action_guard_v1(target_index, action),
        decision=ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            gcd_action=action,
        ),
    )


def build_upper_kara_causal_program_candidate_set_v1(
    *,
    loadout_id: str,
    train_cases: Sequence[DevelopmentPrecombatWaveCaseV1],
    train_examples: Sequence[Any] = (),
    config: UpperKaraCausalProgramGenerationConfigV1 = (
        UpperKaraCausalProgramGenerationConfigV1()
    ),
    snapshot_loader: NativeSnapshotLoaderV1 | None = None,
    proposal_guides: Sequence[ProposalPriorityProviderV1] = (),
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
) -> UpperKaraCausalProgramCandidateSetV1:
    """Generate a bounded, coverage-first declarative program family."""

    if not isinstance(loadout_id, str) or not loadout_id.strip():
        raise ValueError("loadout_id must be nonempty")
    cases = tuple(train_cases)
    if not cases or any(
        not isinstance(case, DevelopmentPrecombatWaveCaseV1) for case in cases
    ):
        raise ValueError(
            "train_cases must contain DevelopmentPrecombatWaveCaseV1 values"
        )
    if not isinstance(config, UpperKaraCausalProgramGenerationConfigV1):
        raise TypeError(
            "config must be UpperKaraCausalProgramGenerationConfigV1"
        )
    for case in cases:
        if case.case_spec.get("schema") != TWO_WAVE_CASE_SCHEMA:
            raise ValueError("every train case must be a continuous two-wave case")
        loadout = case.case_spec.get("burst_loadout")
        if not isinstance(loadout, Mapping) or loadout.get("loadout_id") != loadout_id:
            raise ValueError("train case loadout differs from loadout_id")
    request_ids = {case.dynamic_load.request_sha256 for case in cases}
    if len(request_ids) != 1:
        raise ValueError("train cases do not share one exact request/build/loadout")

    examples = tuple(train_examples)
    if examples and len(examples) != len(cases):
        raise ValueError("train_examples and train_cases differ in length")
    if examples:
        for example, case in zip(examples, cases, strict=True):
            if (
                getattr(example, "seed", None) != case.case_spec.get("seed")
                or getattr(example, "first_wave_arrival_ms", None)
                != case.case_spec["continuous_route"]["player_arrival"][
                    "first_wave_in_range_at_ms_relative_to_pull"
                ]
            ):
                raise ValueError("train example does not identify its paired train case")
        seed_labels = tuple(int(example.seed) for example in examples)
    else:
        seed_labels = tuple(int(case.case_spec["seed"]) for case in cases)
    if len(seed_labels) != len(set(seed_labels)):
        raise ValueError("training seed labels must be unique")

    loader = snapshot_loader or (
        lambda case: _load_native_snapshot_v1(
            case, bridge_path=bridge_path, bridge_cwd=bridge_cwd
        )
    )
    if not callable(loader):
        raise TypeError("snapshot_loader must be callable or None")
    snapshot = tuple(loader(cases[0]))
    if not snapshot or any(not isinstance(row, AvailableAction) for row in snapshot):
        raise ValueError("native snapshot must contain AvailableAction values")
    native_by_action = {row.action: row for row in snapshot}
    if len(native_by_action) != len(snapshot):
        raise ValueError("native snapshot contains duplicate action identities")

    scores, guide_ids, ignored_guide_actions = _priority_scores_v1(
        cases, snapshot, tuple(proposal_guides)
    )
    grid = build_upper_kara_burst_reschedule_grid_v1(
        cases[0], hp_thresholds=config.hp_thresholds
    )
    grid_actions = tuple(sorted({row.action for row in grid}))
    burst_actions = tuple(
        action for action in grid_actions if action in native_by_action
    )
    excluded_burst_actions = tuple(
        action for action in grid_actions if action not in native_by_action
    )
    burst_action_set = set(burst_actions)
    gcd_actions = tuple(
        sorted(
            row.action
            for row in snapshot
            if row.triggers_gcd and row.action not in burst_action_set
        )
    )
    queue_actions = tuple(
        sorted(
            row.action
            for row in snapshot
            if not row.triggers_gcd and row.action.tag == 1
        )
    )
    if not gcd_actions:
        raise ValueError("native snapshot exposes no ordinary Fury GCD actions")
    if not burst_actions:
        raise ValueError("native snapshot exposes no burst-grid actions")
    queue_set = set(queue_actions)
    stance_actions = tuple(
        sorted(
            row.action
            for row in snapshot
            if not row.triggers_gcd
            and row.action in FURY_STANCE_ACTIONS_V1
        )
    )
    ordinary_off_gcd_actions = tuple(
        sorted(
            row.action
            for row in snapshot
            if not row.triggers_gcd
            and row.action.tag != 1
            and row.action not in burst_action_set
            and row.action not in FURY_STANCE_ACTIONS_V1
        )
    )

    target_indexes = tuple(sorted(cases[0].target_contexts))
    if target_indexes != tuple(range(len(target_indexes))):
        raise ValueError("target indexes must be contiguous from zero")
    target_orders = tuple(permutations(target_indexes))
    gcd_orders = _bounded_orders_v1(
        gcd_actions,
        scores=scores,
        limit=config.max_gcd_priority_orders,
    )
    gcd_assignments = _bounded_assignments_v1(
        len(gcd_orders),
        len(target_indexes),
        config.max_gcd_order_assignments,
    )
    burst_orders = _bounded_orders_v1(
        burst_actions,
        scores=scores,
        limit=config.max_burst_priority_orders,
    )
    burst_assignments = _bounded_assignments_v1(
        len(burst_orders),
        len(target_indexes),
        config.max_burst_order_assignments,
    )
    burst_allocations = _burst_allocation_options_v1(
        burst_actions,
        target_indexes,
        frozenset(cases[0].precombat.self_actions),
        config,
    )
    ordinary_off_gcd_orders = _bounded_orders_v1(
        ordinary_off_gcd_actions,
        scores=scores,
        limit=config.max_ordinary_off_gcd_priority_orders,
    )
    ordinary_off_gcd_assignments = _bounded_assignments_v1(
        len(ordinary_off_gcd_orders),
        len(target_indexes),
        config.max_ordinary_off_gcd_order_assignments,
    )
    ordinary_off_gcd_allocations = _ordinary_off_gcd_allocation_options_v1(
        ordinary_off_gcd_actions,
        target_indexes,
        config,
    )
    resource_axis_orders = (
        (("burst", "ordinary_off_gcd"), ("ordinary_off_gcd", "burst"))
        if ordinary_off_gcd_actions
        else (("burst",),)
    )
    queue_orders = _bounded_orders_v1(
        queue_actions,
        scores=scores,
        limit=max(1, config.max_burst_priority_orders),
    )
    queue_options: list[tuple[tuple[ActionRef, ...], int] | None] = []
    if config.include_no_queue_programs:
        queue_options.append(None)
    queue_options.extend(
        (order, threshold)
        for order in queue_orders
        for threshold in config.queue_rage_thresholds
        if order
    )
    if not queue_options:
        queue_options.append(None)

    factors = (
        target_orders,
        gcd_assignments,
        tuple(queue_options),
        burst_allocations,
        burst_assignments,
        ordinary_off_gcd_allocations,
        ordinary_off_gcd_assignments,
        resource_axis_orders,
    )
    option_counts = tuple(len(rows) for rows in factors)
    anchor_combination = (0,) * len(option_counts)
    witness_combinations: list[tuple[int, ...]] = [anchor_combination]

    # Some ordering axes are command-invisible when paired with the all-skip
    # allocation at index zero.  Seed them with active allocations so every
    # represented option has an actual selector-order consequence.
    burst_order_witness = next((
        index
        for index, allocation in enumerate(burst_allocations)
        if all(
            sum(
                target in dict(action_allocation.target_thresholds)
                for action_allocation in allocation
            ) >= min(2, len(burst_actions))
            for target in target_indexes
        )
    ), None)
    if burst_order_witness is not None:
        for assignment_index in range(len(burst_assignments)):
            row = [0] * len(factors)
            row[3] = burst_order_witness
            row[4] = assignment_index
            witness_combinations.append(tuple(row))

    ordinary_order_witness = next((
        index
        for index, allocation in enumerate(ordinary_off_gcd_allocations)
        if ordinary_off_gcd_actions
        and all(
            sum(
                any(row[0] == target for row in action_allocation.target_hp_rage)
                for action_allocation in allocation
            ) >= min(2, len(ordinary_off_gcd_actions))
            for target in target_indexes
        )
    ), None)
    if ordinary_order_witness is not None:
        for assignment_index in range(len(ordinary_off_gcd_assignments)):
            row = [0] * len(factors)
            row[5] = ordinary_order_witness
            row[6] = assignment_index
            witness_combinations.append(tuple(row))

    burst_axis_witness = next((
        index
        for index, allocation in enumerate(burst_allocations)
        if any(
            target_indexes[0] in dict(action_allocation.target_thresholds)
            for action_allocation in allocation
        )
    ), None)
    ordinary_axis_witness = next((
        index
        for index, allocation in enumerate(ordinary_off_gcd_allocations)
        if any(
            any(
                row[0] == target_indexes[0]
                for row in action_allocation.target_hp_rage
            )
            for action_allocation in allocation
        )
    ), None)
    if burst_axis_witness is not None and ordinary_axis_witness is not None:
        for axis_index in range(len(resource_axis_orders)):
            row = [0] * len(factors)
            row[3] = burst_axis_witness
            row[5] = ordinary_axis_witness
            row[7] = axis_index
            witness_combinations.append(tuple(row))

    combinations = chain(
        dict.fromkeys(witness_combinations),
        _coverage_first_combinations_v1(option_counts),
    )
    represented: list[set[int]] = [set() for _ in factors]
    programs: list[CausalActionProgramV1] = []
    selector_keys: set[str] = set()
    grid_by_key = {
        (row.target_index, row.action, row.hp_threshold_pct): row for row in grid
    }
    burst_action_positions = {
        action: index for index, action in enumerate(burst_actions)
    }
    ordinary_off_gcd_action_positions = {
        action: index for index, action in enumerate(ordinary_off_gcd_actions)
    }
    for indexes in combinations:
        target_order = target_orders[indexes[0]]
        gcd_assignment = gcd_assignments[indexes[1]]
        queue_option = queue_options[indexes[2]]
        burst_allocation = burst_allocations[indexes[3]]
        burst_assignment = burst_assignments[indexes[4]]
        ordinary_off_gcd_allocation = ordinary_off_gcd_allocations[indexes[5]]
        ordinary_off_gcd_assignment = ordinary_off_gcd_assignments[indexes[6]]
        resource_axis_order = resource_axis_orders[indexes[7]]
        alternatives: list[GuardedAlternativeV1] = []

        # Strict precombat opportunities are independent per finite resource.
        # The first target's burst-order factor supplies their relative order;
        # skip allocations simply emit no alternative.
        precombat_order = burst_orders[burst_assignment[target_indexes[0]]]
        for action in precombat_order:
            allocation = burst_allocation[burst_action_positions[action]]
            if allocation.precombat_relative_ms is None:
                continue
            alternatives.append(
                _precombat_burst_alternative_v1(
                    action=action,
                    native=native_by_action[action],
                    relative_ms=allocation.precombat_relative_ms,
                    wait_ms=config.queue_or_off_gcd_wait_ms,
                )
            )

        for target_index in target_order:
            target_burst_alternatives: list[GuardedAlternativeV1] = []
            for action in burst_orders[burst_assignment[target_index]]:
                allocation = burst_allocation[burst_action_positions[action]]
                threshold = dict(allocation.target_thresholds).get(target_index)
                if threshold is None:
                    continue
                candidate = grid_by_key[(target_index, action, threshold)]
                target_burst_alternatives.append(
                    _burst_alternative_v1(
                        candidate,
                        native_by_action[action],
                        wait_ms=config.queue_or_off_gcd_wait_ms,
                    )
                )
            target_ordinary_off_gcd_alternatives: list[
                GuardedAlternativeV1
            ] = []
            ordinary_order = ordinary_off_gcd_orders[
                ordinary_off_gcd_assignment[target_index]
            ]
            for action in ordinary_order:
                allocation = ordinary_off_gcd_allocation[
                    ordinary_off_gcd_action_positions[action]
                ]
                gate = {
                    target: (hp_gte, rage_lte)
                    for target, hp_gte, rage_lte in allocation.target_hp_rage
                }.get(target_index)
                if gate is None:
                    continue
                hp_gte, rage_lte = gate
                target_ordinary_off_gcd_alternatives.append(
                    _ordinary_off_gcd_alternative_v1(
                        target_index=target_index,
                        action=action,
                        hp_gte=hp_gte,
                        rage_lte=rage_lte,
                        wait_ms=config.queue_or_off_gcd_wait_ms,
                    )
                )
            for axis in resource_axis_order:
                alternatives.extend(
                    target_burst_alternatives
                    if axis == "burst"
                    else target_ordinary_off_gcd_alternatives
                )
        if queue_option is not None:
            queue_order, rage_threshold = queue_option
            for target_index in target_order:
                for action in queue_order:
                    assert action in queue_set
                    alternatives.append(
                        _queue_alternative_v1(
                            target_index=target_index,
                            action=action,
                            rage_threshold=rage_threshold,
                            wait_ms=config.queue_or_off_gcd_wait_ms,
                        )
                    )
        for target_index in target_order:
            for action in gcd_orders[gcd_assignment[target_index]]:
                alternatives.append(
                    _gcd_alternative_v1(
                        target_index=target_index, action=action
                    )
                )
        selector = OrderedGuardSelectorV1(
            alternatives=tuple(alternatives),
            fallback=ProgramDecisionV1(wait_ms=config.fallback_wait_ms),
        )
        selector_key = _canonical_json(selector.to_dict())
        if selector_key in selector_keys:
            continue
        selector_keys.add(selector_key)
        for dimension, option_index in enumerate(indexes):
            represented[dimension].add(option_index)
        ordinal = len(programs)
        programs.append(
            CausalActionProgramV1(
                program_id=f"causal-candidate::{loadout_id}::{ordinal:04d}",
                selector=selector,
                origin=ProgramOriginV1.SEARCHED,
                source_refs=(
                    "native_fury_action_snapshot",
                    "upper_kara_burst_reschedule_grid/v1",
                    *(f"proposal-guide:{guide_id}" for guide_id in guide_ids),
                ),
            )
        )
        if len(programs) >= config.max_programs:
            break

    factor_names = (
        "target_order",
        "target_specific_gcd_order_assignment",
        "queue_choice_order_and_rage_threshold",
        "per_action_burst_use_time_target_hp_allocation",
        "target_specific_burst_order_assignment",
        "per_action_ordinary_off_gcd_use_target_hp_rage_allocation",
        "target_specific_ordinary_off_gcd_order_assignment",
        "per_target_resource_axis_order",
    )
    factor_counts = dict(zip(factor_names, option_counts, strict=True))
    represented_counts = {
        name: len(values)
        for name, values in zip(factor_names, represented, strict=True)
    }
    arrivals: list[int] = []
    for case in cases:
        arrival = case.case_spec["continuous_route"]["player_arrival"][
            "first_wave_in_range_at_ms_relative_to_pull"
        ]
        if type(arrival) is not int or arrival < 0:
            raise ValueError("train case first-wave arrival is invalid")
        arrivals.append(arrival)
    pull_times = {case.precombat.pull_time_ms for case in cases}
    if len(pull_times) != 1:
        raise ValueError("train cases disagree on pull_time_ms")
    latest_arrival = max(arrivals)
    minimum_idle_decisions = math.ceil(
        (next(iter(pull_times)) + latest_arrival) / config.fallback_wait_ms
    )
    return UpperKaraCausalProgramCandidateSetV1(
        loadout_id=loadout_id,
        training_seed_labels=seed_labels,
        programs=tuple(programs),
        native_snapshot=snapshot,
        gcd_actions=gcd_actions,
        queue_actions=queue_actions,
        burst_actions=burst_actions,
        excluded_burst_actions=excluded_burst_actions,
        ordinary_off_gcd_actions=ordinary_off_gcd_actions,
        stance_actions_requiring_transition_grammar=stance_actions,
        guide_ids=guide_ids,
        ignored_guide_action_count=ignored_guide_actions,
        factor_option_counts=factor_counts,
        represented_factor_option_counts=represented_counts,
        latest_train_first_wave_arrival_ms=latest_arrival,
        minimum_idle_decisions_before_latest_arrival=minimum_idle_decisions,
        recommended_max_replay_decisions=max(512, minimum_idle_decisions + 256),
        config=config,
    )


class UpperKaraCausalProgramGeneratorV1:
    """Stateful callable adapter accepted by the train/eval driver."""

    def __init__(
        self,
        *,
        config: UpperKaraCausalProgramGenerationConfigV1 = (
            UpperKaraCausalProgramGenerationConfigV1()
        ),
        snapshot_loader: NativeSnapshotLoaderV1 | None = None,
        proposal_guides: Sequence[ProposalPriorityProviderV1] = (),
        proposal_guide_factory: ProposalGuideFactoryV1 | None = None,
        bridge_path: Path = DEFAULT_BRIDGE,
        bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    ) -> None:
        self.config = config
        self.snapshot_loader = snapshot_loader
        self.proposal_guides = tuple(proposal_guides)
        if proposal_guide_factory is not None and not callable(
            proposal_guide_factory
        ):
            raise TypeError("proposal_guide_factory must be callable or None")
        self.proposal_guide_factory = proposal_guide_factory
        self.bridge_path = bridge_path
        self.bridge_cwd = bridge_cwd
        self.results_by_loadout: dict[
            str, UpperKaraCausalProgramCandidateSetV1
        ] = {}
        self.proposal_guides_by_loadout: dict[
            str, tuple[ProposalPriorityProviderV1, ...]
        ] = {}

    def __call__(
        self,
        *,
        loadout_id: str,
        train_examples: Sequence[Any],
        train_cases: Sequence[DevelopmentPrecombatWaveCaseV1],
    ) -> tuple[CausalActionProgramV1, ...]:
        cases = tuple(train_cases)
        case_bound_guides = (
            tuple(self.proposal_guide_factory(cases))
            if self.proposal_guide_factory is not None
            else ()
        )
        resolved_guides = (*self.proposal_guides, *case_bound_guides)
        result = build_upper_kara_causal_program_candidate_set_v1(
            loadout_id=loadout_id,
            train_examples=train_examples,
            train_cases=cases,
            config=self.config,
            snapshot_loader=self.snapshot_loader,
            proposal_guides=resolved_guides,
            bridge_path=self.bridge_path,
            bridge_cwd=self.bridge_cwd,
        )
        self.proposal_guides_by_loadout[loadout_id] = resolved_guides
        self.results_by_loadout[loadout_id] = result
        return result.programs


__all__ = (
    "DEFAULT_ORDINARY_OFF_GCD_RAGE_LTE_THRESHOLDS_V1",
    "DEFAULT_QUEUE_RAGE_THRESHOLDS_V1",
    "FURY_STANCE_ACTIONS_V1",
    "SUNDER_ARMOR_ACTION_V1",
    "SUNDER_ARMOR_MAX_STACKS_V1",
    "SCHEMA",
    "CallableProgramProposalGuideV1",
    "NativeSnapshotLoaderV1",
    "ProposalPriorityProviderV1",
    "ProposalGuideFactoryV1",
    "TrainingOutcomeProviderV1",
    "UpperKaraCausalProgramCandidateSetV1",
    "UpperKaraCausalProgramGeneratorV1",
    "UpperKaraCausalProgramGenerationConfigV1",
    "WaveActionGuideProgramProposalAdapterV1",
    "adapt_wave_action_guides_for_program_proposals_v1",
    "build_upper_kara_causal_program_candidate_set_v1",
)
