"""Atomic queue plus ordinary-GCD proposals over the exact Cat policy.

This is the first post-v4 action-space expansion.  A nonzero candidate owns
the *whole* ordered next-swing queue and ordinary-GCD guard block.  Queue and
GCD sequences are independently recombined from causal source programs,
including an explicit no-queue/CANCEL mode.  Cat is still resolved first and continues to own
target selection, controls, ordinary off-GCD/burst prefixes, and every other
decision sink.  The searched block is applied only to Cat's selected target.

The module is deliberately not wired into the running v4 campaign.  Its first
candidate is the existing exact-Cat zero residual, preserving the established
paired lower-bound identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
import json
from typing import Any, Iterator, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedReactiveQueueGcdBlockSelectorV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
)
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
    exact_cat_fallback_selector_v1,
)
from .upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramCandidateSetV1,
    UpperKaraCausalProgramGeneratorV1,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_queue_gcd_block_candidate_set/v1"
BLOCK_SOURCE_REF_V1 = "cat-queue-gcd-block/v1:projected"
DEFAULT_MAX_BLOCK_PROGRAMS_V1 = 512


class UpperKaraCatQueueGcdBlockSearchV1Error(RuntimeError):
    """A source proposal cannot be projected as one atomic lane block."""


def _canonical_json(value: JSONMap) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _project_block_alternative_v1(
    alternative: GuardedAlternativeV1,
) -> GuardedAlternativeV1 | None:
    decision = alternative.decision
    if alternative.alternative_id.startswith("queue:"):
        if (
            decision.queue_op is not QueueLaneOp.SET
            or decision.queue_action is None
            or decision.gcd_action is not None
        ):
            raise UpperKaraCatQueueGcdBlockSearchV1Error(
                f"malformed source queue alternative {alternative.alternative_id!r}"
            )
        replacement = ProgramDecisionV1(
            queue_op=QueueLaneOp.SET,
            queue_action=decision.queue_action,
            wait_ms=decision.wait_ms,
        )
    elif alternative.alternative_id.startswith("gcd:"):
        if decision.gcd_action is None:
            raise UpperKaraCatQueueGcdBlockSearchV1Error(
                f"malformed source GCD alternative {alternative.alternative_id!r}"
            )
        replacement = ProgramDecisionV1(gcd_action=decision.gcd_action)
    else:
        return None
    return GuardedAlternativeV1(
        alternative_id=f"block:{alternative.alternative_id}",
        guard=alternative.guard,
        decision=replacement,
    )


def _project_lanes_v1(
    source_program: CausalActionProgramV1,
    *,
    result_bearing_gcd_actions: frozenset[Any] | None = None,
) -> tuple[
    tuple[GuardedAlternativeV1, ...], tuple[GuardedAlternativeV1, ...]
]:
    source_selector = source_program.selector
    if not isinstance(source_selector, OrderedGuardSelectorV1):
        raise UpperKaraCatQueueGcdBlockSearchV1Error(
            "source program must use ordered declarative alternatives"
        )
    alternatives = tuple(
        projected
        for row in source_selector.alternatives
        if (projected := _project_block_alternative_v1(row)) is not None
    )
    queue = tuple(
        row
        for row in alternatives
        if row.decision.queue_op is QueueLaneOp.SET
    )
    gcd = tuple(
        row
        for row in alternatives
        if row.decision.gcd_action is not None
        and (
            result_bearing_gcd_actions is None
            or row.decision.gcd_action in result_bearing_gcd_actions
        )
    )
    return queue, gcd


def _coverage_first_lane_pairs_v1(
    queue_count: int, gcd_count: int
) -> Iterator[tuple[int, int]]:
    """Cover every queue and GCD sequence before the remaining cross product."""

    seen: set[tuple[int, int]] = set()
    proposals = (
        ((0, 0),),
        tuple((index, 0) for index in range(queue_count)),
        tuple((0, index) for index in range(gcd_count)),
        product(range(queue_count), range(gcd_count)),
    )
    for rows in proposals:
        for row in rows:
            if row in seen:
                continue
            seen.add(row)
            yield row


@dataclass(frozen=True)
class UpperKaraCatQueueGcdBlockCandidateSetV1:
    loadout_id: str
    programs: tuple[CausalActionProgramV1, ...]
    source_candidate_set: UpperKaraCausalProgramCandidateSetV1
    source_without_complete_block_count: int
    duplicate_projection_count: int
    unique_queue_sequence_count: int
    unique_gcd_sequence_count: int
    represented_queue_sequence_count: int
    represented_gcd_sequence_count: int
    max_programs: int

    def __post_init__(self) -> None:
        if not isinstance(self.loadout_id, str) or not self.loadout_id.strip():
            raise ValueError("loadout_id must be nonempty")
        if self.source_candidate_set.loadout_id != self.loadout_id:
            raise ValueError("source candidate loadout differs from block loadout")
        if not self.programs:
            raise ValueError("queue/GCD block family must contain exact Cat")
        if self.programs[0] != cat_zero_residual_program_v1(self.loadout_id):
            raise ValueError("first queue/GCD block candidate must be exact Cat")
        for program in self.programs[1:]:
            selector = program.selector
            if (
                program.origin is not ProgramOriginV1.SEARCHED
                or not isinstance(
                    selector, ImportedReactiveQueueGcdBlockSelectorV1
                )
                or selector.imported_fallback
                != exact_cat_fallback_selector_v1()
            ):
                raise ValueError(
                    "every nonzero candidate must be an atomic block over exact Cat"
                )
        for name in (
            "source_without_complete_block_count",
            "duplicate_projection_count",
            "unique_queue_sequence_count",
            "unique_gcd_sequence_count",
            "represented_queue_sequence_count",
            "represented_gcd_sequence_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.represented_queue_sequence_count > self.unique_queue_sequence_count:
            raise ValueError("represented queue sequences exceed source sequences")
        if self.represented_gcd_sequence_count > self.unique_gcd_sequence_count:
            raise ValueError("represented GCD sequences exceed source sequences")
        if (
            isinstance(self.max_programs, bool)
            or not isinstance(self.max_programs, int)
            or self.max_programs < 2
        ):
            raise ValueError("max_programs must be an integer of at least two")

    def to_dict(self) -> JSONMap:
        return {
            "schema": SCHEMA,
            "loadout_id": self.loadout_id,
            "program_count": len(self.programs),
            "zero_residual_program_id": self.programs[0].program_id,
            "nonzero_block_program_count": len(self.programs) - 1,
            "source_candidate_count": len(self.source_candidate_set.programs),
            "source_without_complete_block_count": (
                self.source_without_complete_block_count
            ),
            "duplicate_projection_count": self.duplicate_projection_count,
            "unique_queue_sequence_count": self.unique_queue_sequence_count,
            "unique_gcd_sequence_count": self.unique_gcd_sequence_count,
            "represented_queue_sequence_count": (
                self.represented_queue_sequence_count
            ),
            "represented_gcd_sequence_count": self.represented_gcd_sequence_count,
            "all_source_lane_sequences_represented": (
                self.represented_queue_sequence_count
                == self.unique_queue_sequence_count
                and self.represented_gcd_sequence_count
                == self.unique_gcd_sequence_count
            ),
            "max_programs": self.max_programs,
            "source_proposal_guide_ids": list(
                self.source_candidate_set.guide_ids
            ),
            "programs": [row.to_dict() for row in self.programs],
            "contract": {
                "zero_residual_is_exact_cat": True,
                "queue_and_ordinary_gcd_replaced_atomically": True,
                "whole_ordered_guard_block_proposed": True,
                "cat_owns_target_selection": True,
                "cat_owns_controls_and_optional_off_gcd_prefixes": True,
                "resource_alternatives_imported_from_source": False,
                "explicit_no_queue_sequence_searched": True,
                "non_result_source_gcds_preserved": True,
                "wired_into_remote_campaign": False,
            },
        }


def project_cat_queue_gcd_block_candidate_set_v1(
    source: UpperKaraCausalProgramCandidateSetV1,
    *,
    max_programs: int = DEFAULT_MAX_BLOCK_PROGRAMS_V1,
) -> UpperKaraCatQueueGcdBlockCandidateSetV1:
    """Project complete source proposals into atomic Cat-relative blocks."""

    if not isinstance(source, UpperKaraCausalProgramCandidateSetV1):
        raise TypeError("source must be UpperKaraCausalProgramCandidateSetV1")
    if (
        isinstance(max_programs, bool)
        or not isinstance(max_programs, int)
        or max_programs < 2
    ):
        raise ValueError("max_programs must be an integer of at least two")
    programs: list[CausalActionProgramV1] = [
        cat_zero_residual_program_v1(source.loadout_id)
    ]
    queue_sequences: dict[
        str, tuple[tuple[GuardedAlternativeV1, ...] | None, str]
    ] = {}
    gcd_sequences: dict[
        str, tuple[tuple[GuardedAlternativeV1, ...], str]
    ] = {}
    missing = 0
    result_bearing_gcd_actions = frozenset(
        row.action
        for row in source.native_snapshot
        if row.triggers_gcd and row.result_bearing
    )
    for source_program in source.programs:
        queue, gcd = _project_lanes_v1(
            source_program,
            result_bearing_gcd_actions=result_bearing_gcd_actions,
        )
        if not gcd:
            missing += 1
        if queue:
            queue_sequences.setdefault(
                _canonical_json({"alternatives": [row.to_dict() for row in queue]}),
                (queue, source_program.program_id),
            )
        elif gcd:
            queue_sequences.setdefault(
                _canonical_json({"queue_mode": "EXPLICIT_NO_QUEUE"}),
                (None, source_program.program_id),
            )
        if gcd:
            gcd_sequences.setdefault(
                _canonical_json({"alternatives": [row.to_dict() for row in gcd]}),
                (gcd, source_program.program_id),
            )
    if not queue_sequences or not gcd_sequences:
        raise UpperKaraCatQueueGcdBlockSearchV1Error(
            "source family does not expose both queue and ordinary-GCD sequences"
        )

    queue_rows = tuple(
        sorted(
            queue_sequences.values(),
            key=lambda row: row[0] is None,
        )
    )
    gcd_rows = tuple(gcd_sequences.values())
    selector_keys: set[str] = set()
    duplicates = 0
    represented_queue: set[int] = set()
    represented_gcd: set[int] = set()
    for queue_index, gcd_index in _coverage_first_lane_pairs_v1(
        len(queue_rows), len(gcd_rows)
    ):
        queue, queue_source_ref = queue_rows[queue_index]
        gcd, gcd_source_ref = gcd_rows[gcd_index]
        projected_gcd = (
            tuple(
                replace(
                    row,
                    decision=replace(
                        row.decision,
                        queue_op=QueueLaneOp.CANCEL,
                        prefix_order=tuple(
                            ProgramPrefixOperationKindV1.QUEUE_CANCEL
                            if kind is ProgramPrefixOperationKindV1.QUEUE_KEEP
                            else kind
                            for kind in row.decision.prefix_order or ()
                        ),
                    ),
                )
                for row in gcd
            )
            if queue is None
            else gcd
        )
        selector = ImportedReactiveQueueGcdBlockSelectorV1(
            block_alternatives=(queue or ()) + projected_gcd,
            imported_fallback=exact_cat_fallback_selector_v1(),
        )
        key = _canonical_json(selector.to_dict())
        if key in selector_keys:
            duplicates += 1
            continue
        selector_keys.add(key)
        represented_queue.add(queue_index)
        represented_gcd.add(gcd_index)
        programs.append(
            CausalActionProgramV1(
                program_id=(
                    f"cat-queue-gcd-block::{source.loadout_id}::"
                    f"{len(programs):04d}"
                ),
                selector=selector,
                origin=ProgramOriginV1.SEARCHED,
                source_refs=(
                    CAT_POLICY_ID,
                    BLOCK_SOURCE_REF_V1,
                    queue_source_ref,
                    *(row for row in (gcd_source_ref,) if row != queue_source_ref),
                    *(f"proposal-guide:{guide_id}" for guide_id in source.guide_ids),
                ),
            )
        )
        if len(programs) >= max_programs:
            break
    return UpperKaraCatQueueGcdBlockCandidateSetV1(
        loadout_id=source.loadout_id,
        programs=tuple(programs),
        source_candidate_set=source,
        source_without_complete_block_count=missing,
        duplicate_projection_count=duplicates,
        unique_queue_sequence_count=len(queue_rows),
        unique_gcd_sequence_count=len(gcd_rows),
        represented_queue_sequence_count=len(represented_queue),
        represented_gcd_sequence_count=len(represented_gcd),
        max_programs=max_programs,
    )


class UpperKaraCatQueueGcdBlockGeneratorV1:
    """Train-compatible local wrapper; deliberately not used by v4 remote."""

    def __init__(
        self, source_generator: UpperKaraCausalProgramGeneratorV1
    ) -> None:
        if not isinstance(source_generator, UpperKaraCausalProgramGeneratorV1):
            raise TypeError(
                "source_generator must be UpperKaraCausalProgramGeneratorV1"
            )
        self.source_generator = source_generator
        self.results_by_loadout: dict[
            str, UpperKaraCatQueueGcdBlockCandidateSetV1
        ] = {}

    @property
    def proposal_guides_by_loadout(self):
        return self.source_generator.proposal_guides_by_loadout

    def __call__(
        self,
        *,
        loadout_id: str,
        train_examples: Sequence[Any],
        train_cases: Sequence[Any],
    ) -> tuple[CausalActionProgramV1, ...]:
        self.source_generator(
            loadout_id=loadout_id,
            train_examples=train_examples,
            train_cases=train_cases,
        )
        try:
            source = self.source_generator.results_by_loadout[loadout_id]
        except KeyError as error:
            raise UpperKaraCatQueueGcdBlockSearchV1Error(
                "source generator did not retain its candidate-set audit"
            ) from error
        result = project_cat_queue_gcd_block_candidate_set_v1(source)
        self.results_by_loadout[loadout_id] = result
        return result.programs


__all__ = (
    "BLOCK_SOURCE_REF_V1",
    "DEFAULT_MAX_BLOCK_PROGRAMS_V1",
    "SCHEMA",
    "UpperKaraCatQueueGcdBlockCandidateSetV1",
    "UpperKaraCatQueueGcdBlockGeneratorV1",
    "UpperKaraCatQueueGcdBlockSearchV1Error",
    "project_cat_queue_gcd_block_candidate_set_v1",
)
