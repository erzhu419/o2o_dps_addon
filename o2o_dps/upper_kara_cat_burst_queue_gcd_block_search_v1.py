"""Search ordinary queue/GCD sequences over one frozen Cat-burst parent.

The v4 residual search already selected a complete burst overlay over exact
Cat.  This module holds that parent program fixed and independently recombines
whole ordered next-swing queue and ordinary-GCD sequences from the causal
proposal family.  Consequently, the first candidate is the *exact parent
program object*, while every nonzero candidate changes only the atomic
queue/GCD block.  A paired comparison against candidate zero can therefore
attribute any delta to the ordinary action sequence rather than to different
burst resources or timing.

The block selector represents explicit queue choices with ``SET`` and a
searched ``do not queue`` mode by coupling ``CANCEL`` to each selected GCD.
When no searched block guard matches, the frozen parent's imported Cat
decision remains unchanged for that epoch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ImportedReactiveQueueGcdBlockSelectorV1,
    ProgramOriginV1,
)
from .upper_kara_cat_queue_gcd_block_search_v1 import (
    DEFAULT_MAX_BLOCK_PROGRAMS_V1,
    UpperKaraCatQueueGcdBlockCandidateSetV1,
    project_cat_queue_gcd_block_candidate_set_v1,
)
from .upper_kara_cat_residual_overlay_search_v1 import (
    exact_cat_fallback_selector_v1,
)
from .upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramCandidateSetV1,
    UpperKaraCausalProgramGeneratorV1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_burst_queue_gcd_block_candidate_set/v1"
BLOCK_OVER_PARENT_SOURCE_REF_V1 = (
    "cat-burst-queue-gcd-block/v1:projected"
)


class UpperKaraCatBurstQueueGcdBlockSearchV1Error(RuntimeError):
    """A fixed burst parent cannot be extended by the queue/GCD search."""


def _validated_parent_selector_v1(
    parent_program: CausalActionProgramV1,
) -> ImportedFallbackOverlaySelectorV1:
    if not isinstance(parent_program, CausalActionProgramV1):
        raise TypeError("parent_program must be CausalActionProgramV1")
    selector = parent_program.selector
    if not isinstance(selector, ImportedFallbackOverlaySelectorV1):
        raise UpperKaraCatBurstQueueGcdBlockSearchV1Error(
            "parent program must use ImportedFallbackOverlaySelectorV1"
        )
    if selector.imported_fallback != exact_cat_fallback_selector_v1():
        raise UpperKaraCatBurstQueueGcdBlockSearchV1Error(
            "parent burst program must fall back to exact Cat"
        )
    if parent_program.origin is not ProgramOriginV1.SEARCHED:
        raise UpperKaraCatBurstQueueGcdBlockSearchV1Error(
            "parent burst program must have SEARCHED origin"
        )
    return selector


def _unique_source_refs_v1(*groups: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for value in group:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
    return tuple(result)


def _compose_selector_v1(
    parent: ImportedFallbackOverlaySelectorV1,
    block: ImportedReactiveQueueGcdBlockSelectorV1,
) -> ImportedReactiveBurstQueueGcdBlockSelectorV1:
    if block.imported_fallback != parent.imported_fallback:
        raise UpperKaraCatBurstQueueGcdBlockSearchV1Error(
            "block and frozen parent use different imported fallbacks"
        )
    return ImportedReactiveBurstQueueGcdBlockSelectorV1(
        terminal_alternatives=parent.terminal_alternatives,
        imported_fallback=parent.imported_fallback,
        off_gcd_insertions=parent.off_gcd_insertions,
        insertion_order=parent.insertion_order,
        insertion_position=parent.insertion_position,
        block_alternatives=block.block_alternatives,
    )


@dataclass(frozen=True)
class UpperKaraCatBurstQueueGcdBlockCandidateSetV1:
    loadout_id: str
    parent_program: CausalActionProgramV1
    programs: tuple[CausalActionProgramV1, ...]
    block_candidate_set: UpperKaraCatQueueGcdBlockCandidateSetV1

    def __post_init__(self) -> None:
        if not isinstance(self.loadout_id, str) or not self.loadout_id.strip():
            raise ValueError("loadout_id must be nonempty")
        if self.block_candidate_set.loadout_id != self.loadout_id:
            raise ValueError("block candidate loadout differs from result loadout")
        parent = _validated_parent_selector_v1(self.parent_program)
        if not self.programs or self.programs[0] is not self.parent_program:
            raise ValueError(
                "first candidate must preserve the exact parent program object"
            )
        if len(self.programs) != len(self.block_candidate_set.programs):
            raise ValueError("composite and block candidate counts differ")
        for composite, block_program in zip(
            self.programs[1:], self.block_candidate_set.programs[1:]
        ):
            selector = composite.selector
            block = block_program.selector
            if (
                composite.origin is not ProgramOriginV1.SEARCHED
                or not isinstance(
                    selector, ImportedReactiveBurstQueueGcdBlockSelectorV1
                )
                or not isinstance(block, ImportedReactiveQueueGcdBlockSelectorV1)
            ):
                raise ValueError(
                    "every nonzero candidate must be a searched composite block"
                )
            expected = _compose_selector_v1(parent, block)
            if selector != expected:
                raise ValueError(
                    "composite candidate changes frozen parent burst semantics"
                )

    def to_dict(self) -> JSONMap:
        block_wire = self.block_candidate_set.to_dict()
        return {
            "schema": SCHEMA,
            "loadout_id": self.loadout_id,
            "program_count": len(self.programs),
            "paired_zero_program_id": self.parent_program.program_id,
            "paired_zero_program_key": self.parent_program.program_key(),
            "parent_program": self.parent_program.to_dict(),
            "nonzero_block_program_count": len(self.programs) - 1,
            "block_projection_audit": {
                key: block_wire[key]
                for key in (
                    "source_candidate_count",
                    "source_without_complete_block_count",
                    "duplicate_projection_count",
                    "unique_queue_sequence_count",
                    "unique_gcd_sequence_count",
                    "represented_queue_sequence_count",
                    "represented_gcd_sequence_count",
                    "all_source_lane_sequences_represented",
                    "max_programs",
                    "source_proposal_guide_ids",
                )
            },
            "programs": [row.to_dict() for row in self.programs],
            "contract": {
                "paired_zero_is_exact_parent_program": True,
                "parent_burst_program_is_frozen": True,
                "queue_and_ordinary_gcd_replaced_atomically": True,
                "queue_and_gcd_sequences_recombined_independently": True,
                "cat_owns_target_selection_and_controls": True,
                "explicit_no_queue_sequence_searched": True,
                "non_result_source_gcds_preserved": True,
                "unmatched_block_keeps_parent_source_decision": True,
            },
        }


def project_cat_burst_queue_gcd_block_candidate_set_v1(
    source: UpperKaraCausalProgramCandidateSetV1,
    parent_program: CausalActionProgramV1,
    *,
    max_programs: int = DEFAULT_MAX_BLOCK_PROGRAMS_V1,
) -> UpperKaraCatBurstQueueGcdBlockCandidateSetV1:
    """Project atomic queue/GCD blocks over one unchanged burst parent."""

    if not isinstance(source, UpperKaraCausalProgramCandidateSetV1):
        raise TypeError("source must be UpperKaraCausalProgramCandidateSetV1")
    parent = _validated_parent_selector_v1(parent_program)
    block_candidates = project_cat_queue_gcd_block_candidate_set_v1(
        source,
        max_programs=max_programs,
    )
    programs: list[CausalActionProgramV1] = [parent_program]
    for block_program in block_candidates.programs[1:]:
        block = block_program.selector
        if not isinstance(block, ImportedReactiveQueueGcdBlockSelectorV1):
            raise UpperKaraCatBurstQueueGcdBlockSearchV1Error(
                "nonzero projected block has an unsupported selector"
            )
        selector = _compose_selector_v1(parent, block)
        programs.append(
            CausalActionProgramV1(
                program_id=(
                    f"cat-burst-queue-gcd-block::{source.loadout_id}::"
                    f"{len(programs):04d}"
                ),
                selector=selector,
                origin=ProgramOriginV1.SEARCHED,
                source_refs=_unique_source_refs_v1(
                    parent_program.source_refs,
                    (
                        BLOCK_OVER_PARENT_SOURCE_REF_V1,
                        f"parent-program:{parent_program.program_id}",
                    ),
                    block_program.source_refs,
                ),
            )
        )
    return UpperKaraCatBurstQueueGcdBlockCandidateSetV1(
        loadout_id=source.loadout_id,
        parent_program=parent_program,
        programs=tuple(programs),
        block_candidate_set=block_candidates,
    )


class UpperKaraCatBurstQueueGcdBlockGeneratorV1:
    """Train-compatible generator with one immutable burst parent."""

    def __init__(
        self,
        source_generator: UpperKaraCausalProgramGeneratorV1,
        parent_program: CausalActionProgramV1,
        *,
        max_programs: int = DEFAULT_MAX_BLOCK_PROGRAMS_V1,
    ) -> None:
        if not isinstance(source_generator, UpperKaraCausalProgramGeneratorV1):
            raise TypeError(
                "source_generator must be UpperKaraCausalProgramGeneratorV1"
            )
        _validated_parent_selector_v1(parent_program)
        if (
            isinstance(max_programs, bool)
            or not isinstance(max_programs, int)
            or max_programs < 2
        ):
            raise ValueError("max_programs must be an integer of at least two")
        self.source_generator = source_generator
        self.parent_program = parent_program
        self.max_programs = max_programs
        self.results_by_loadout: dict[
            str, UpperKaraCatBurstQueueGcdBlockCandidateSetV1
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
            raise UpperKaraCatBurstQueueGcdBlockSearchV1Error(
                "source generator did not retain its candidate-set audit"
            ) from error
        result = project_cat_burst_queue_gcd_block_candidate_set_v1(
            source,
            self.parent_program,
            max_programs=self.max_programs,
        )
        self.results_by_loadout[loadout_id] = result
        return result.programs


__all__ = (
    "BLOCK_OVER_PARENT_SOURCE_REF_V1",
    "SCHEMA",
    "UpperKaraCatBurstQueueGcdBlockCandidateSetV1",
    "UpperKaraCatBurstQueueGcdBlockGeneratorV1",
    "UpperKaraCatBurstQueueGcdBlockSearchV1Error",
    "project_cat_burst_queue_gcd_block_candidate_set_v1",
)
