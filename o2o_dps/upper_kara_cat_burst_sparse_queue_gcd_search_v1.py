"""Sparse ordinary-action residuals over one frozen Cat-burst parent.

The earlier queue/GCD projector replaced a complete queue sequence and a
complete ordinary-GCD priority sequence together.  That is too coarse for a
residual search: a useful queue threshold or one useful GCD guard cannot be
retained unless every other replacement in the same block is also harmless.

This projector exposes a bounded, coverage-first neighbourhood around the
exact frozen parent instead.  Candidate zero is the parent object itself.
Nonzero candidates contain one of:

* a queue sequence only;
* one ordinary-GCD guard, either inheriting or explicitly canceling queue;
* a proper (never complete) prefix of an ordinary-GCD sequence, likewise
  with inherited or explicitly canceled queue; or
* one queue sequence combined with one of those sparse GCD fragments.

Every projected GCD decision uses ``QueueLaneOp.KEEP``.  The composition
runtime interprets that as inheriting the imported Cat queue sink, so a GCD
experiment does not silently cancel Heroic Strike or Cleave.  If no projected
guard matches, the imported Cat decision from the frozen burst parent remains
unchanged.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import product
import json
from typing import Any, Iterator, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    OrderedGuardSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    exact_cat_fallback_selector_v1,
)
from .upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramCandidateSetV1,
    UpperKaraCausalProgramGeneratorV1,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_burst_sparse_queue_gcd_candidate_set/v1"
SPARSE_BLOCK_SOURCE_REF_V1 = "cat-burst-sparse-queue-gcd/v1:projected"
DEFAULT_MAX_SPARSE_PROGRAMS_V1 = 384
DEFAULT_MAX_GCD_PREFIX_LENGTH_V1 = 3
MAX_SPARSE_PROGRAMS_V1 = 512


class UpperKaraCatBurstSparseQueueGcdSearchV1Error(RuntimeError):
    """A source family cannot be projected into the sparse residual space."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unique_strings_v1(*groups: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for value in group:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
    return tuple(result)


def _validated_parent_v1(
    parent_program: CausalActionProgramV1,
) -> ImportedFallbackOverlaySelectorV1:
    if not isinstance(parent_program, CausalActionProgramV1):
        raise TypeError("parent_program must be CausalActionProgramV1")
    selector = parent_program.selector
    if not isinstance(selector, ImportedFallbackOverlaySelectorV1):
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            "parent program must use ImportedFallbackOverlaySelectorV1"
        )
    if selector.imported_fallback != exact_cat_fallback_selector_v1():
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            "parent burst program must fall back to exact Cat"
        )
    if parent_program.origin is not ProgramOriginV1.SEARCHED:
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            "parent burst program must have SEARCHED origin"
        )
    return selector


def _compose_selector_v1(
    parent: ImportedFallbackOverlaySelectorV1,
    block_alternatives: tuple[GuardedAlternativeV1, ...],
) -> ImportedReactiveBurstQueueGcdBlockSelectorV1:
    return ImportedReactiveBurstQueueGcdBlockSelectorV1(
        terminal_alternatives=parent.terminal_alternatives,
        imported_fallback=parent.imported_fallback,
        off_gcd_insertions=parent.off_gcd_insertions,
        insertion_order=parent.insertion_order,
        insertion_position=parent.insertion_position,
        block_alternatives=block_alternatives,
    )


def _project_queue_alternative_v1(
    alternative: GuardedAlternativeV1,
) -> GuardedAlternativeV1:
    decision = alternative.decision
    if (
        not alternative.alternative_id.startswith("queue:")
        or decision.queue_op is not QueueLaneOp.SET
        or decision.queue_action is None
        or decision.gcd_action is not None
    ):
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            f"malformed source queue alternative {alternative.alternative_id!r}"
        )
    return GuardedAlternativeV1(
        alternative_id=f"sparse:block:{alternative.alternative_id}",
        guard=alternative.guard,
        decision=ProgramDecisionV1(
            queue_op=QueueLaneOp.SET,
            queue_action=decision.queue_action,
            wait_ms=decision.wait_ms,
        ),
    )


def _project_gcd_alternative_v1(
    alternative: GuardedAlternativeV1,
) -> GuardedAlternativeV1:
    decision = alternative.decision
    if (
        not alternative.alternative_id.startswith("gcd:")
        or decision.gcd_action is None
    ):
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            f"malformed source GCD alternative {alternative.alternative_id!r}"
        )
    return GuardedAlternativeV1(
        alternative_id=f"sparse:block:{alternative.alternative_id}",
        guard=alternative.guard,
        decision=ProgramDecisionV1(
            queue_op=QueueLaneOp.KEEP,
            gcd_action=decision.gcd_action,
        ),
    )


def _cancel_queue_for_gcd_fragment_v1(
    alternatives: tuple[GuardedAlternativeV1, ...],
) -> tuple[GuardedAlternativeV1, ...]:
    return tuple(
        GuardedAlternativeV1(
            alternative_id=f"{alternative.alternative_id}:no-queue",
            guard=alternative.guard,
            decision=ProgramDecisionV1(
                queue_op=QueueLaneOp.CANCEL,
                gcd_action=alternative.decision.gcd_action,
            ),
        )
        for alternative in alternatives
    )


def _project_source_lanes_v1(
    source_program: CausalActionProgramV1,
    *,
    result_bearing_gcd_actions: frozenset[Any],
) -> tuple[
    tuple[GuardedAlternativeV1, ...], tuple[GuardedAlternativeV1, ...]
]:
    selector = source_program.selector
    if not isinstance(selector, OrderedGuardSelectorV1):
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            "source program must use ordered declarative alternatives"
        )
    queues: list[GuardedAlternativeV1] = []
    gcds: list[GuardedAlternativeV1] = []
    for alternative in selector.alternatives:
        if alternative.alternative_id.startswith("queue:"):
            queues.append(_project_queue_alternative_v1(alternative))
        elif alternative.alternative_id.startswith("gcd:"):
            if alternative.decision.gcd_action in result_bearing_gcd_actions:
                gcds.append(_project_gcd_alternative_v1(alternative))
    return tuple(queues), tuple(gcds)


@dataclass(frozen=True)
class _LaneSequenceV1:
    alternatives: tuple[GuardedAlternativeV1, ...]
    representative_source_program_id: str


@dataclass(frozen=True)
class _SparseBlockSpecV1:
    family: str
    alternatives: tuple[GuardedAlternativeV1, ...]
    representative_source_program_ids: tuple[str, ...]
    queue_sequence_index: int | None = None
    gcd_fragment_id: str | None = None


def _coverage_first_pairs_v1(
    left_count: int, right_count: int
) -> Iterator[tuple[int, int]]:
    """Represent every axis early, then enumerate the remaining product."""

    if left_count <= 0 or right_count <= 0:
        return
    seen: set[tuple[int, int]] = set()
    for index in range(max(left_count, right_count)):
        pair = (index % left_count, index % right_count)
        if pair not in seen:
            seen.add(pair)
            yield pair
    for pair in product(range(left_count), range(right_count)):
        if pair in seen:
            continue
        seen.add(pair)
        yield pair


def _sequence_key_v1(
    alternatives: Sequence[GuardedAlternativeV1],
) -> str:
    return _canonical_json(
        {"alternatives": [row.to_dict() for row in alternatives]}
    )


def _unique_lane_sequences_v1(
    source: UpperKaraCausalProgramCandidateSetV1,
) -> tuple[
    tuple[_LaneSequenceV1, ...],
    tuple[_LaneSequenceV1, ...],
    int,
    int,
    int,
]:
    result_bearing_gcd_actions = frozenset(
        row.action
        for row in source.native_snapshot
        if row.triggers_gcd and row.result_bearing
    )
    queues: dict[str, _LaneSequenceV1] = {}
    gcds: dict[str, _LaneSequenceV1] = {}
    without_queue = 0
    without_gcd = 0
    duplicate_sequences = 0
    for source_program in source.programs:
        queue, gcd = _project_source_lanes_v1(
            source_program,
            result_bearing_gcd_actions=result_bearing_gcd_actions,
        )
        if queue:
            key = _sequence_key_v1(queue)
            if key in queues:
                duplicate_sequences += 1
            else:
                queues[key] = _LaneSequenceV1(queue, source_program.program_id)
        else:
            without_queue += 1
        if gcd:
            key = _sequence_key_v1(gcd)
            if key in gcds:
                duplicate_sequences += 1
            else:
                gcds[key] = _LaneSequenceV1(gcd, source_program.program_id)
        else:
            without_gcd += 1
    return (
        tuple(queues.values()),
        tuple(gcds.values()),
        without_queue,
        without_gcd,
        duplicate_sequences,
    )


def _sparse_block_specs_v1(
    queue_sequences: Sequence[_LaneSequenceV1],
    gcd_sequences: Sequence[_LaneSequenceV1],
    *,
    max_gcd_prefix_length: int,
) -> tuple[tuple[_SparseBlockSpecV1, ...], int, int, int, int, int]:
    specs: list[_SparseBlockSpecV1] = []
    duplicate_projections = 0

    for index, sequence in enumerate(queue_sequences):
        specs.append(
            _SparseBlockSpecV1(
                family="QUEUE_ONLY",
                alternatives=sequence.alternatives,
                representative_source_program_ids=(
                    sequence.representative_source_program_id,
                ),
                queue_sequence_index=index,
            )
        )

    individual_by_key: dict[str, _SparseBlockSpecV1] = {}
    for sequence in gcd_sequences:
        for alternative in sequence.alternatives:
            key = _sequence_key_v1((alternative,))
            if key in individual_by_key:
                duplicate_projections += 1
                continue
            individual_by_key[key] = _SparseBlockSpecV1(
                family="GCD_INDIVIDUAL",
                alternatives=(alternative,),
                representative_source_program_ids=(
                    sequence.representative_source_program_id,
                ),
                gcd_fragment_id=f"individual:{key}",
            )
    individual_specs = tuple(individual_by_key.values())
    specs.extend(individual_specs)

    prefix_by_key: dict[str, _SparseBlockSpecV1] = {}
    for prefix_length in range(2, max_gcd_prefix_length + 1):
        for sequence in gcd_sequences:
            # Proper prefixes only: the dense full sequence is deliberately
            # outside this residual neighbourhood.
            if prefix_length >= len(sequence.alternatives):
                continue
            alternatives = sequence.alternatives[:prefix_length]
            key = _sequence_key_v1(alternatives)
            if key in prefix_by_key:
                duplicate_projections += 1
                continue
            prefix_by_key[key] = _SparseBlockSpecV1(
                family="GCD_PREFIX",
                alternatives=alternatives,
                representative_source_program_ids=(
                    sequence.representative_source_program_id,
                ),
                gcd_fragment_id=f"prefix:{key}",
            )
    prefix_specs = tuple(prefix_by_key.values())
    specs.extend(prefix_specs)

    no_queue_individual_specs = tuple(
        _SparseBlockSpecV1(
            family="GCD_INDIVIDUAL_NO_QUEUE",
            alternatives=_cancel_queue_for_gcd_fragment_v1(spec.alternatives),
            representative_source_program_ids=(
                spec.representative_source_program_ids
            ),
            gcd_fragment_id=f"individual-no-queue:{spec.gcd_fragment_id}",
        )
        for spec in individual_specs
    )
    specs.extend(no_queue_individual_specs)
    no_queue_prefix_specs = tuple(
        _SparseBlockSpecV1(
            family="GCD_PREFIX_NO_QUEUE",
            alternatives=_cancel_queue_for_gcd_fragment_v1(spec.alternatives),
            representative_source_program_ids=(
                spec.representative_source_program_ids
            ),
            gcd_fragment_id=f"prefix-no-queue:{spec.gcd_fragment_id}",
        )
        for spec in prefix_specs
    )
    specs.extend(no_queue_prefix_specs)

    gcd_fragments = (*individual_specs, *prefix_specs)
    for queue_index, fragment_index in _coverage_first_pairs_v1(
        len(queue_sequences), len(gcd_fragments)
    ):
        queue = queue_sequences[queue_index]
        fragment = gcd_fragments[fragment_index]
        specs.append(
            _SparseBlockSpecV1(
                family=(
                    "QUEUE_PLUS_GCD_INDIVIDUAL"
                    if fragment.family == "GCD_INDIVIDUAL"
                    else "QUEUE_PLUS_GCD_PREFIX"
                ),
                alternatives=queue.alternatives + fragment.alternatives,
                representative_source_program_ids=_unique_strings_v1(
                    (queue.representative_source_program_id,),
                    fragment.representative_source_program_ids,
                ),
                queue_sequence_index=queue_index,
                gcd_fragment_id=fragment.gcd_fragment_id,
            )
        )

    unique_specs: list[_SparseBlockSpecV1] = []
    seen: set[str] = set()
    for spec in specs:
        key = _sequence_key_v1(spec.alternatives)
        if key in seen:
            duplicate_projections += 1
            continue
        seen.add(key)
        unique_specs.append(spec)
    return (
        tuple(unique_specs),
        len(individual_specs),
        len(prefix_specs),
        len(no_queue_individual_specs),
        len(no_queue_prefix_specs),
        duplicate_projections,
    )


@dataclass(frozen=True)
class UpperKaraCatBurstSparseQueueGcdCandidateSetV1:
    loadout_id: str
    parent_program: CausalActionProgramV1
    programs: tuple[CausalActionProgramV1, ...]
    source_candidate_set: UpperKaraCausalProgramCandidateSetV1
    max_programs: int
    max_gcd_prefix_length: int
    source_without_queue_count: int
    source_without_result_gcd_count: int
    duplicate_source_sequence_count: int
    duplicate_projection_count: int
    unique_queue_sequence_count: int
    unique_gcd_sequence_count: int
    unique_gcd_individual_count: int
    unique_gcd_prefix_count: int
    unique_no_queue_gcd_individual_count: int
    unique_no_queue_gcd_prefix_count: int
    requested_nonzero_program_count: int
    requested_family_counts: Mapping[str, int]
    emitted_family_counts: Mapping[str, int]
    represented_queue_sequence_count: int
    represented_gcd_fragment_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.loadout_id, str) or not self.loadout_id.strip():
            raise ValueError("loadout_id must be nonempty")
        if self.source_candidate_set.loadout_id != self.loadout_id:
            raise ValueError("source candidate loadout differs from result loadout")
        parent = _validated_parent_v1(self.parent_program)
        if not self.programs or self.programs[0] is not self.parent_program:
            raise ValueError("first candidate must preserve the exact parent object")
        if len(self.programs) > self.max_programs:
            raise ValueError("program count exceeds max_programs")
        for program in self.programs[1:]:
            selector = program.selector
            if (
                program.origin is not ProgramOriginV1.SEARCHED
                or not isinstance(
                    selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
                )
            ):
                raise ValueError("nonzero sparse candidates must be searched composites")
            if not selector.block_alternatives:
                raise ValueError("nonzero sparse candidate has an empty block")
            if (
                selector.terminal_alternatives != parent.terminal_alternatives
                or selector.imported_fallback != parent.imported_fallback
                or selector.off_gcd_insertions != parent.off_gcd_insertions
                or selector.insertion_order != parent.insertion_order
                or selector.insertion_position != parent.insertion_position
            ):
                raise ValueError("sparse candidate changes frozen parent semantics")
            for alternative in selector.block_alternatives:
                decision = alternative.decision
                is_queue = (
                    decision.queue_op is QueueLaneOp.SET
                    and decision.queue_action is not None
                    and decision.gcd_action is None
                )
                is_gcd = (
                    decision.queue_op in (QueueLaneOp.KEEP, QueueLaneOp.CANCEL)
                    and decision.queue_action is None
                    and decision.gcd_action is not None
                )
                if not (is_queue or is_gcd):
                    raise ValueError(
                        "sparse alternatives must set queue or inherit it with one GCD"
                    )
        for name in (
            "source_without_queue_count",
            "source_without_result_gcd_count",
            "duplicate_source_sequence_count",
            "duplicate_projection_count",
            "unique_queue_sequence_count",
            "unique_gcd_sequence_count",
            "unique_gcd_individual_count",
            "unique_gcd_prefix_count",
            "unique_no_queue_gcd_individual_count",
            "unique_no_queue_gcd_prefix_count",
            "requested_nonzero_program_count",
            "represented_queue_sequence_count",
            "represented_gcd_fragment_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")

    def to_dict(self) -> JSONMap:
        emitted_nonzero = len(self.programs) - 1
        return {
            "schema": SCHEMA,
            "loadout_id": self.loadout_id,
            "program_count": len(self.programs),
            "paired_zero_program_id": self.parent_program.program_id,
            "paired_zero_program_key": self.parent_program.program_key(),
            "parent_program": self.parent_program.to_dict(),
            "source_candidate_count": len(self.source_candidate_set.programs),
            "source_proposal_guide_ids": list(self.source_candidate_set.guide_ids),
            "source_without_queue_count": self.source_without_queue_count,
            "source_without_result_gcd_count": self.source_without_result_gcd_count,
            "duplicate_source_sequence_count": self.duplicate_source_sequence_count,
            "duplicate_projection_count": self.duplicate_projection_count,
            "unique_queue_sequence_count": self.unique_queue_sequence_count,
            "unique_gcd_sequence_count": self.unique_gcd_sequence_count,
            "unique_gcd_individual_count": self.unique_gcd_individual_count,
            "unique_gcd_prefix_count": self.unique_gcd_prefix_count,
            "unique_no_queue_gcd_individual_count": (
                self.unique_no_queue_gcd_individual_count
            ),
            "unique_no_queue_gcd_prefix_count": (
                self.unique_no_queue_gcd_prefix_count
            ),
            "requested_nonzero_program_count": self.requested_nonzero_program_count,
            "emitted_nonzero_program_count": emitted_nonzero,
            "requested_family_counts": dict(self.requested_family_counts),
            "emitted_family_counts": dict(self.emitted_family_counts),
            "represented_queue_sequence_count": self.represented_queue_sequence_count,
            "represented_gcd_fragment_count": self.represented_gcd_fragment_count,
            "candidate_space_truncated": (
                emitted_nonzero < self.requested_nonzero_program_count
            ),
            "max_programs": self.max_programs,
            "max_gcd_prefix_length": self.max_gcd_prefix_length,
            "programs": [row.to_dict() for row in self.programs],
            "contract": {
                "paired_zero_is_exact_parent_program": True,
                "parent_burst_program_is_frozen": True,
                "queue_only_candidates_are_explicit": True,
                "gcd_individual_candidates_are_explicit": True,
                "gcd_prefixes_are_proper_not_dense_sequences": True,
                "gcd_keep_overrides_inherit_source_queue": True,
                "explicit_no_queue_gcd_fragments_are_searched": True,
                "unmatched_sparse_guard_keeps_parent_source_decision": True,
                "candidate_budget_is_coverage_first": True,
                "candidate_budget_hard_cap": MAX_SPARSE_PROGRAMS_V1,
            },
        }


def project_cat_burst_sparse_queue_gcd_candidate_set_v1(
    source: UpperKaraCausalProgramCandidateSetV1,
    parent_program: CausalActionProgramV1,
    *,
    max_programs: int = DEFAULT_MAX_SPARSE_PROGRAMS_V1,
    max_gcd_prefix_length: int = DEFAULT_MAX_GCD_PREFIX_LENGTH_V1,
) -> UpperKaraCatBurstSparseQueueGcdCandidateSetV1:
    """Build the bounded sparse neighbourhood around ``parent_program``."""

    if not isinstance(source, UpperKaraCausalProgramCandidateSetV1):
        raise TypeError("source must be UpperKaraCausalProgramCandidateSetV1")
    if (
        isinstance(max_programs, bool)
        or not isinstance(max_programs, int)
        or not 2 <= max_programs <= MAX_SPARSE_PROGRAMS_V1
    ):
        raise ValueError(
            f"max_programs must be an integer from 2 to {MAX_SPARSE_PROGRAMS_V1}"
        )
    if (
        isinstance(max_gcd_prefix_length, bool)
        or not isinstance(max_gcd_prefix_length, int)
        or max_gcd_prefix_length < 2
    ):
        raise ValueError("max_gcd_prefix_length must be an integer of at least two")
    parent = _validated_parent_v1(parent_program)
    (
        queue_sequences,
        gcd_sequences,
        without_queue,
        without_gcd,
        duplicate_source_sequences,
    ) = _unique_lane_sequences_v1(source)
    if not queue_sequences or not gcd_sequences:
        raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
            "source family does not expose queue and result-bearing GCD sequences"
        )
    (
        specs,
        unique_individual_count,
        unique_prefix_count,
        unique_no_queue_individual_count,
        unique_no_queue_prefix_count,
        duplicate_projections,
    ) = _sparse_block_specs_v1(
        queue_sequences,
        gcd_sequences,
        max_gcd_prefix_length=max_gcd_prefix_length,
    )
    requested_counts = Counter(spec.family for spec in specs)
    programs: list[CausalActionProgramV1] = [parent_program]
    emitted_specs: list[_SparseBlockSpecV1] = []
    for spec in specs[: max_programs - 1]:
        selector = _compose_selector_v1(parent, spec.alternatives)
        ordinal = len(programs)
        programs.append(
            CausalActionProgramV1(
                program_id=(
                    f"cat-burst-sparse-queue-gcd::{source.loadout_id}::"
                    f"{ordinal:04d}"
                ),
                selector=selector,
                origin=ProgramOriginV1.SEARCHED,
                source_refs=_unique_strings_v1(
                    parent_program.source_refs,
                    (
                        SPARSE_BLOCK_SOURCE_REF_V1,
                        f"parent-program:{parent_program.program_id}",
                        f"sparse-family:{spec.family}",
                    ),
                    tuple(
                        "representative-source-program:" + value
                        for value in spec.representative_source_program_ids
                    ),
                    tuple(
                        f"proposal-guide:{guide_id}" for guide_id in source.guide_ids
                    ),
                ),
            )
        )
        emitted_specs.append(spec)
    emitted_counts = Counter(spec.family for spec in emitted_specs)
    represented_queue = {
        spec.queue_sequence_index
        for spec in emitted_specs
        if spec.queue_sequence_index is not None
    }
    represented_gcd = {
        spec.gcd_fragment_id
        for spec in emitted_specs
        if spec.gcd_fragment_id is not None
    }
    return UpperKaraCatBurstSparseQueueGcdCandidateSetV1(
        loadout_id=source.loadout_id,
        parent_program=parent_program,
        programs=tuple(programs),
        source_candidate_set=source,
        max_programs=max_programs,
        max_gcd_prefix_length=max_gcd_prefix_length,
        source_without_queue_count=without_queue,
        source_without_result_gcd_count=without_gcd,
        duplicate_source_sequence_count=duplicate_source_sequences,
        duplicate_projection_count=duplicate_projections,
        unique_queue_sequence_count=len(queue_sequences),
        unique_gcd_sequence_count=len(gcd_sequences),
        unique_gcd_individual_count=unique_individual_count,
        unique_gcd_prefix_count=unique_prefix_count,
        unique_no_queue_gcd_individual_count=(
            unique_no_queue_individual_count
        ),
        unique_no_queue_gcd_prefix_count=unique_no_queue_prefix_count,
        requested_nonzero_program_count=len(specs),
        requested_family_counts=dict(requested_counts),
        emitted_family_counts=dict(emitted_counts),
        represented_queue_sequence_count=len(represented_queue),
        represented_gcd_fragment_count=len(represented_gcd),
    )


class UpperKaraCatBurstSparseQueueGcdGeneratorV1:
    """Train-compatible sparse projector with one immutable burst parent."""

    def __init__(
        self,
        source_generator: UpperKaraCausalProgramGeneratorV1,
        parent_program: CausalActionProgramV1,
        *,
        max_programs: int = DEFAULT_MAX_SPARSE_PROGRAMS_V1,
        max_gcd_prefix_length: int = DEFAULT_MAX_GCD_PREFIX_LENGTH_V1,
    ) -> None:
        if not isinstance(source_generator, UpperKaraCausalProgramGeneratorV1):
            raise TypeError(
                "source_generator must be UpperKaraCausalProgramGeneratorV1"
            )
        _validated_parent_v1(parent_program)
        if (
            isinstance(max_programs, bool)
            or not isinstance(max_programs, int)
            or not 2 <= max_programs <= MAX_SPARSE_PROGRAMS_V1
        ):
            raise ValueError(
                f"max_programs must be an integer from 2 to {MAX_SPARSE_PROGRAMS_V1}"
            )
        if (
            isinstance(max_gcd_prefix_length, bool)
            or not isinstance(max_gcd_prefix_length, int)
            or max_gcd_prefix_length < 2
        ):
            raise ValueError(
                "max_gcd_prefix_length must be an integer of at least two"
            )
        self.source_generator = source_generator
        self.parent_program = parent_program
        self.max_programs = max_programs
        self.max_gcd_prefix_length = max_gcd_prefix_length
        self.results_by_loadout: dict[
            str, UpperKaraCatBurstSparseQueueGcdCandidateSetV1
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
            raise UpperKaraCatBurstSparseQueueGcdSearchV1Error(
                "source generator did not retain its candidate-set audit"
            ) from error
        result = project_cat_burst_sparse_queue_gcd_candidate_set_v1(
            source,
            self.parent_program,
            max_programs=self.max_programs,
            max_gcd_prefix_length=self.max_gcd_prefix_length,
        )
        self.results_by_loadout[loadout_id] = result
        return result.programs


__all__ = (
    "DEFAULT_MAX_GCD_PREFIX_LENGTH_V1",
    "DEFAULT_MAX_SPARSE_PROGRAMS_V1",
    "MAX_SPARSE_PROGRAMS_V1",
    "SCHEMA",
    "SPARSE_BLOCK_SOURCE_REF_V1",
    "UpperKaraCatBurstSparseQueueGcdCandidateSetV1",
    "UpperKaraCatBurstSparseQueueGcdGeneratorV1",
    "UpperKaraCatBurstSparseQueueGcdSearchV1Error",
    "project_cat_burst_sparse_queue_gcd_candidate_set_v1",
)
