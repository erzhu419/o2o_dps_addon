"""Search resource-timing residuals over the exact imported Cat policy.

The standalone causal-program generator deliberately owns the complete Fury
rotation.  That is useful for unconstrained search, but it cannot establish the
important lower bound that the searched family contains Cat itself.  This
module projects that generator's resource decisions onto an unchanged Cat
reactive fallback:

* the zero-residual member is command-equivalent to imported Cat;
* searched precombat and in-combat burst choices are retained;
* searched ordinary off-GCD choices (for example Bloodrage) are retained; and
* ordinary GCD priority and next-swing queue choices are discarded, so Cat
  continues to own both lanes.

Target-specific off-GCD residuals are inserted only after Cat's target choice
for the current epoch.  A low-HP first wave therefore skips the guarded
resource without consuming it, and the same resource is reconsidered when Cat
selects the later wave.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    GuardedAlternativeV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveSelectorV1,
    ProgramInsertionPointV1,
    ProgramOriginV1,
    SearchedOffGcdInsertionV1,
)
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramCandidateSetV1,
    UpperKaraCausalProgramGeneratorV1,
)
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_residual_overlay_candidate_set/v1"
ZERO_RESIDUAL_SOURCE_REF_V1 = "cat-residual-overlay/v1:zero"
RESIDUAL_SOURCE_REF_V1 = "cat-residual-overlay/v1:projected"
_RESOURCE_ALTERNATIVE_PREFIXES = (
    "precombat:",
    "burst:",
    "ordinary-off-gcd:",
)
DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1 = (1_500, 3_000, 5_000, 8_000)


class UpperKaraCatResidualOverlaySearchV1Error(RuntimeError):
    """A source candidate cannot be projected without changing its meaning."""


def exact_cat_fallback_selector_v1() -> ImportedReactiveSelectorV1:
    """Return the exact runtime identity used by the native Cat baseline."""

    return ImportedReactiveSelectorV1(
        binding_id=CAT_POLICY_ID,
        source_policy_id=CAT_POLICY_ID,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_resource_alternative(row: GuardedAlternativeV1) -> bool:
    return row.alternative_id.startswith(_RESOURCE_ALTERNATIVE_PREFIXES)


def _project_selector_v1(
    source_program: CausalActionProgramV1,
    *,
    remaining_attackable_gte_ms: int | None = None,
) -> ImportedFallbackOverlaySelectorV1:
    selector = source_program.selector
    alternatives = getattr(selector, "alternatives", None)
    if not isinstance(alternatives, tuple):
        raise UpperKaraCatResidualOverlaySearchV1Error(
            "source program must use ordered declarative alternatives"
        )

    terminal: list[GuardedAlternativeV1] = []
    insertions: list[SearchedOffGcdInsertionV1] = []
    for alternative in alternatives:
        if not _is_resource_alternative(alternative):
            # This is the key residual boundary: Cat, not the searched source
            # program, continues to own normal GCD and next-swing decisions.
            continue
        decision = alternative.decision
        time_gated_burst = (
            remaining_attackable_gte_ms is not None
            and alternative.alternative_id.startswith("burst:")
        )
        guard = (
            replace(
                alternative.guard,
                estimated_remaining_attackable_gte_ms=(
                    remaining_attackable_gte_ms
                ),
            )
            if time_gated_burst
            else alternative.guard
        )
        id_suffix = (
            f":remaining-ms{remaining_attackable_gte_ms}"
            if time_gated_burst
            else ""
        )
        prefixes = decision.optional_off_gcd_prefixes
        if decision.gcd_action is not None:
            if prefixes:
                raise UpperKaraCatResidualOverlaySearchV1Error(
                    f"resource alternative {alternative.alternative_id!r} "
                    "mixes a terminal GCD with off-GCD prefixes"
                )
            if not alternative.alternative_id.startswith(
                ("precombat:", "burst:")
            ):
                raise UpperKaraCatResidualOverlaySearchV1Error(
                    "ordinary off-GCD alternative unexpectedly owns a GCD"
                )
            terminal.append(
                GuardedAlternativeV1(
                    alternative_id=alternative.alternative_id + id_suffix,
                    guard=guard,
                    decision=decision,
                )
            )
            continue
        if len(prefixes) != 1:
            raise UpperKaraCatResidualOverlaySearchV1Error(
                f"resource alternative {alternative.alternative_id!r} must "
                "contain exactly one off-GCD prefix"
            )
        prefix = prefixes[0]
        if alternative.guard != prefix.guard:
            raise UpperKaraCatResidualOverlaySearchV1Error(
                f"resource alternative {alternative.alternative_id!r} has "
                "different selector and prefix guards"
            )
        insertion_id = f"residual:{alternative.alternative_id}{id_suffix}"
        insertions.append(
            SearchedOffGcdInsertionV1(
                insertion_id=insertion_id,
                prefix=(
                    replace(prefix, guard=guard)
                    if time_gated_burst
                    else prefix
                ),
                insertion_point=(
                    ProgramInsertionPointV1.BEFORE_SOURCE_PREFIX_ORDER
                    if prefix.guard.target_index is None
                    else ProgramInsertionPointV1.AFTER_SOURCE_SET_TARGET
                ),
            )
        )

    return ImportedFallbackOverlaySelectorV1(
        terminal_alternatives=tuple(terminal),
        imported_fallback=exact_cat_fallback_selector_v1(),
        off_gcd_insertions=tuple(insertions),
        insertion_order=tuple(row.insertion_id for row in insertions),
    )


def cat_zero_residual_program_v1(loadout_id: str) -> CausalActionProgramV1:
    """Return Cat represented as a SEARCHED program with an empty overlay."""

    if not isinstance(loadout_id, str) or not loadout_id.strip():
        raise ValueError("loadout_id must be nonempty")
    selector = ImportedFallbackOverlaySelectorV1(
        terminal_alternatives=(),
        imported_fallback=exact_cat_fallback_selector_v1(),
        off_gcd_insertions=(),
        insertion_order=(),
    )
    return CausalActionProgramV1(
        program_id=f"cat-residual-overlay::{loadout_id}::zero",
        selector=selector,
        origin=ProgramOriginV1.SEARCHED,
        source_refs=(CAT_POLICY_ID, ZERO_RESIDUAL_SOURCE_REF_V1),
    )


@dataclass(frozen=True)
class UpperKaraCatResidualOverlayCandidateSetV1:
    loadout_id: str
    programs: tuple[CausalActionProgramV1, ...]
    source_candidate_set: UpperKaraCausalProgramCandidateSetV1
    duplicate_projection_count: int
    remaining_attackable_ms_thresholds: tuple[int, ...] = (
        DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1
    )

    def __post_init__(self) -> None:
        if not isinstance(self.loadout_id, str) or not self.loadout_id.strip():
            raise ValueError("loadout_id must be nonempty")
        if self.source_candidate_set.loadout_id != self.loadout_id:
            raise ValueError("source candidate loadout differs from overlay loadout")
        if not self.programs:
            raise ValueError("Cat residual family must contain the zero overlay")
        if self.programs[0] != cat_zero_residual_program_v1(self.loadout_id):
            raise ValueError("first Cat residual candidate must be the zero overlay")
        if any(
            row.origin is not ProgramOriginV1.SEARCHED
            or not isinstance(row.selector, ImportedFallbackOverlaySelectorV1)
            or row.selector.imported_fallback != exact_cat_fallback_selector_v1()
            for row in self.programs
        ):
            raise ValueError(
                "every Cat residual candidate must be SEARCHED over exact Cat"
            )
        if (
            isinstance(self.duplicate_projection_count, bool)
            or not isinstance(self.duplicate_projection_count, int)
            or self.duplicate_projection_count < 0
        ):
            raise ValueError("duplicate_projection_count must be nonnegative")
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in self.remaining_attackable_ms_thresholds
        ) or len(set(self.remaining_attackable_ms_thresholds)) != len(
            self.remaining_attackable_ms_thresholds
        ):
            raise ValueError(
                "remaining attackable thresholds must be unique positive integers"
            )

    def to_dict(self) -> JSONMap:
        residual_programs = self.programs[1:]
        return {
            "schema": SCHEMA,
            "loadout_id": self.loadout_id,
            "program_count": len(self.programs),
            "zero_residual_program_id": self.programs[0].program_id,
            "nonzero_residual_program_count": len(residual_programs),
            "duplicate_projection_count": self.duplicate_projection_count,
            "remaining_attackable_ms_thresholds": list(
                self.remaining_attackable_ms_thresholds
            ),
            "source_candidate_count": len(self.source_candidate_set.programs),
            "source_proposal_guide_ids": list(
                self.source_candidate_set.guide_ids
            ),
            "programs": [row.to_dict() for row in self.programs],
            "contract": {
                "program_origin": ProgramOriginV1.SEARCHED.value,
                "fallback_binding_id": CAT_POLICY_ID,
                "fallback_source_policy_id": CAT_POLICY_ID,
                "fallback_observation_contract_id": (
                    OBSERVATION_CONTRACT_ID_V1
                ),
                "zero_residual_is_exact_cat": True,
                "cat_owns_ordinary_gcd_lane": True,
                "cat_owns_next_swing_queue_lane": True,
                "searched_residual_axes": [
                    "precombat_resource_use",
                    "burst_target_hp_reschedule",
                    "burst_prefix_estimated_remaining_attackable_time",
                    "ordinary_off_gcd_target_hp_rage",
                ],
            },
        }


def project_cat_residual_overlay_candidate_set_v1(
    source: UpperKaraCausalProgramCandidateSetV1,
    *,
    remaining_attackable_ms_thresholds: Sequence[int] = (
        DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1
    ),
) -> UpperKaraCatResidualOverlayCandidateSetV1:
    """Project one complete causal family into Cat-relative residuals."""

    if not isinstance(source, UpperKaraCausalProgramCandidateSetV1):
        raise TypeError(
            "source must be UpperKaraCausalProgramCandidateSetV1"
        )
    thresholds = tuple(remaining_attackable_ms_thresholds)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in thresholds
    ) or len(set(thresholds)) != len(thresholds):
        raise ValueError(
            "remaining_attackable_ms_thresholds must be unique positive integers"
        )
    programs: list[CausalActionProgramV1] = [
        cat_zero_residual_program_v1(source.loadout_id)
    ]
    selector_keys = {
        _canonical_json(programs[0].selector.to_dict())
    }
    duplicates = 0
    for source_program in source.programs:
        for remaining_ms in (None, *thresholds):
            selector = _project_selector_v1(
                source_program,
                remaining_attackable_gte_ms=remaining_ms,
            )
            key = _canonical_json(selector.to_dict())
            if key in selector_keys:
                duplicates += 1
                continue
            selector_keys.add(key)
            programs.append(
                CausalActionProgramV1(
                    program_id=(
                        f"cat-residual-overlay::{source.loadout_id}::"
                        f"{len(programs):04d}"
                    ),
                    selector=selector,
                    origin=ProgramOriginV1.SEARCHED,
                    source_refs=(
                        CAT_POLICY_ID,
                        RESIDUAL_SOURCE_REF_V1,
                        source_program.program_id,
                        *(
                            source_ref
                            for source_ref in source_program.source_refs
                            if source_ref.startswith("proposal-guide:")
                        ),
                    ),
                )
            )
    return UpperKaraCatResidualOverlayCandidateSetV1(
        loadout_id=source.loadout_id,
        programs=tuple(programs),
        source_candidate_set=source,
        duplicate_projection_count=duplicates,
        remaining_attackable_ms_thresholds=thresholds,
    )


class UpperKaraCatResidualOverlayGeneratorV1:
    """Train/eval-compatible wrapper around the existing causal generator."""

    def __init__(
        self,
        source_generator: UpperKaraCausalProgramGeneratorV1,
        *,
        remaining_attackable_ms_thresholds: Sequence[int] = (
            DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1
        ),
    ) -> None:
        if not isinstance(source_generator, UpperKaraCausalProgramGeneratorV1):
            raise TypeError(
                "source_generator must be UpperKaraCausalProgramGeneratorV1"
            )
        self.source_generator = source_generator
        self.remaining_attackable_ms_thresholds = tuple(
            remaining_attackable_ms_thresholds
        )
        self.results_by_loadout: dict[
            str, UpperKaraCatResidualOverlayCandidateSetV1
        ] = {}

    @property
    def proposal_guides_by_loadout(self):
        """Expose the source generator audit without changing guide authority."""

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
            raise UpperKaraCatResidualOverlaySearchV1Error(
                "source generator did not retain its candidate-set audit"
            ) from error
        result = project_cat_residual_overlay_candidate_set_v1(
            source,
            remaining_attackable_ms_thresholds=(
                self.remaining_attackable_ms_thresholds
            ),
        )
        self.results_by_loadout[loadout_id] = result
        return result.programs


__all__ = (
    "DEFAULT_REMAINING_ATTACKABLE_MS_THRESHOLDS_V1",
    "RESIDUAL_SOURCE_REF_V1",
    "SCHEMA",
    "ZERO_RESIDUAL_SOURCE_REF_V1",
    "UpperKaraCatResidualOverlayCandidateSetV1",
    "UpperKaraCatResidualOverlayGeneratorV1",
    "UpperKaraCatResidualOverlaySearchV1Error",
    "cat_zero_residual_program_v1",
    "exact_cat_fallback_selector_v1",
    "project_cat_residual_overlay_candidate_set_v1",
)
