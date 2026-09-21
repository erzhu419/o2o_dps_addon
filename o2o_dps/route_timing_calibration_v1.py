"""Paired continuous-route calibration for downstream pull timing.

An isolated wave replay can measure how much faster or slower a candidate
finishes that wave.  It cannot establish how much of that difference survives
travel, doors, scripted waits, or raid regrouping.  This module therefore uses
paired *continuous-route* observations and records the candidate-minus-
baseline start-time shift separately at every downstream encounter.

The result deliberately retains every paired seed shift.  Bounds are extrema,
not an average rounded onto the route clock.  The v1 cooldown allocator still
has a scalar shift state, so only the narrow case where each seed's shift is
unchanged at every downstream encounter can be adapted to that allocator.
Edge-dependent profiles remain useful calibration evidence but fail closed at
the legacy allocation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RouteEncounterStartV1:
    """One encounter start on an absolute continuous-route clock."""

    encounter_id: str
    start_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.encounter_id, str) or not self.encounter_id.strip():
            raise ValueError("encounter_id must be nonempty")
        if type(self.start_ms) is not int or self.start_ms < 0:
            raise ValueError("start_ms must be a nonnegative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "encounter_id": self.encounter_id,
            "start_ms": self.start_ms,
        }


@dataclass(frozen=True)
class PairedContinuousRouteTimingObservationV1:
    """Same-seed baseline/candidate route starts for one package perturbation.

    ``route_case_id`` binds the route variant, build, loadout, initial state and
    all non-candidate decisions.  The contract cannot inspect those inputs, so
    the producer must issue a distinct ID whenever any of them changes.
    """

    route_case_id: str
    source_encounter_id: str
    package_id: str
    seed: int
    baseline_starts: tuple[RouteEncounterStartV1, ...]
    candidate_starts: tuple[RouteEncounterStartV1, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("route_case_id", self.route_case_id),
            ("source_encounter_id", self.source_encounter_id),
            ("package_id", self.package_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonempty")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        for label, starts in (
            ("baseline_starts", self.baseline_starts),
            ("candidate_starts", self.candidate_starts),
        ):
            if not isinstance(starts, tuple) or not starts or any(
                not isinstance(row, RouteEncounterStartV1) for row in starts
            ):
                raise TypeError(
                    f"{label} must be a nonempty tuple of RouteEncounterStartV1"
                )
            ids = [row.encounter_id for row in starts]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} encounter IDs must be unique")
            times = [row.start_ms for row in starts]
            if times != sorted(times):
                raise ValueError(f"{label} must be ordered by nondecreasing start")

        baseline_ids = tuple(row.encounter_id for row in self.baseline_starts)
        candidate_ids = tuple(row.encounter_id for row in self.candidate_starts)
        if candidate_ids != baseline_ids:
            raise ValueError(
                "baseline and candidate must contain the identical ordered route"
            )
        try:
            source_index = baseline_ids.index(self.source_encounter_id)
        except ValueError as exc:
            raise ValueError("source_encounter_id is absent from the route") from exc
        if source_index == len(baseline_ids) - 1:
            raise ValueError(
                "continuous-route timing calibration needs a downstream encounter"
            )
        baseline_prefix = tuple(
            row.start_ms for row in self.baseline_starts[: source_index + 1]
        )
        candidate_prefix = tuple(
            row.start_ms for row in self.candidate_starts[: source_index + 1]
        )
        if candidate_prefix != baseline_prefix:
            raise ValueError(
                "candidate route diverges before or at the source encounter"
            )

    @property
    def route_encounter_ids(self) -> tuple[str, ...]:
        return tuple(row.encounter_id for row in self.baseline_starts)

    @property
    def source_index(self) -> int:
        return self.route_encounter_ids.index(self.source_encounter_id)


@dataclass(frozen=True)
class DownstreamStartShiftBoundsV1:
    """Per-seed and extrema evidence for one downstream encounter start."""

    encounter_id: str
    route_index: int
    seed_shifts_ms: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.encounter_id, str) or not self.encounter_id.strip():
            raise ValueError("encounter_id must be nonempty")
        if type(self.route_index) is not int or self.route_index < 0:
            raise ValueError("route_index must be a nonnegative integer")
        if not isinstance(self.seed_shifts_ms, tuple) or not self.seed_shifts_ms:
            raise TypeError("seed_shifts_ms must be a nonempty tuple")
        seeds: list[int] = []
        for row in self.seed_shifts_ms:
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or type(row[0]) is not int
                or row[0] < 0
                or type(row[1]) is not int
            ):
                raise ValueError(
                    "seed_shifts_ms must contain (nonnegative seed, integer shift)"
                )
            seeds.append(row[0])
        if seeds != sorted(set(seeds)):
            raise ValueError("seed_shifts_ms must have sorted unique seeds")

    @property
    def shift_ms_min(self) -> int:
        return min(shift for _, shift in self.seed_shifts_ms)

    @property
    def shift_ms_max(self) -> int:
        return max(shift for _, shift in self.seed_shifts_ms)

    def to_dict(self) -> dict[str, object]:
        return {
            "encounter_id": self.encounter_id,
            "route_index": self.route_index,
            "shift_ms_min": self.shift_ms_min,
            "shift_ms_max": self.shift_ms_max,
            "paired_seed_count": len(self.seed_shifts_ms),
            "seed_shifts_ms": [
                {"seed": seed, "shift_ms": shift}
                for seed, shift in self.seed_shifts_ms
            ],
        }


@dataclass(frozen=True)
class RouteTimingShiftProfileV1:
    """Edge-aware downstream start shifts for one isolated package change."""

    route_case_id: str
    source_encounter_id: str
    package_id: str
    route_encounter_ids: tuple[str, ...]
    downstream: tuple[DownstreamStartShiftBoundsV1, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("route_case_id", self.route_case_id),
            ("source_encounter_id", self.source_encounter_id),
            ("package_id", self.package_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonempty")
        if (
            not isinstance(self.route_encounter_ids, tuple)
            or not self.route_encounter_ids
            or any(not isinstance(value, str) or not value for value in self.route_encounter_ids)
            or len(self.route_encounter_ids) != len(set(self.route_encounter_ids))
        ):
            raise ValueError("route_encounter_ids must be nonempty and unique")
        if self.source_encounter_id not in self.route_encounter_ids:
            raise ValueError("source_encounter_id is absent from route_encounter_ids")
        if not isinstance(self.downstream, tuple) or not self.downstream or any(
            not isinstance(row, DownstreamStartShiftBoundsV1)
            for row in self.downstream
        ):
            raise TypeError(
                "downstream must be a nonempty tuple of start-shift bounds"
            )
        source_index = self.route_encounter_ids.index(self.source_encounter_id)
        expected = tuple(
            (index, encounter_id)
            for index, encounter_id in enumerate(self.route_encounter_ids)
            if index > source_index
        )
        observed = tuple(
            (row.route_index, row.encounter_id) for row in self.downstream
        )
        if observed != expected:
            raise ValueError(
                "downstream shifts must cover every encounter after the source"
            )
        seed_orders = [tuple(seed for seed, _ in row.seed_shifts_ms) for row in self.downstream]
        if any(order != seed_orders[0] for order in seed_orders[1:]):
            raise ValueError("every downstream encounter must use the same paired seeds")

    @property
    def package_key(self) -> tuple[str, str]:
        return self.source_encounter_id, self.package_id

    @property
    def seeds(self) -> tuple[int, ...]:
        return tuple(seed for seed, _ in self.downstream[0].seed_shifts_ms)

    @property
    def is_exact_zero(self) -> bool:
        return all(
            shift == 0
            for row in self.downstream
            for _, shift in row.seed_shifts_ms
        )

    @property
    def is_persistent_per_seed(self) -> bool:
        """Whether the legacy scalar state represents every observed seed."""

        first = dict(self.downstream[0].seed_shifts_ms)
        return all(dict(row.seed_shifts_ms) == first for row in self.downstream[1:])

    def persistent_shift_bounds(self) -> tuple[int, int]:
        """Return legacy scalar bounds, rejecting absorption or other edge effects."""

        if not self.is_persistent_per_seed:
            raise ValueError(
                "edge-dependent route timing cannot enter the scalar cooldown "
                "allocator; a door, wait, travel edge, or downstream state change "
                "absorbs or changes the package shift"
            )
        first = self.downstream[0]
        return first.shift_ms_min, first.shift_ms_max

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "route_timing_shift_profile/v1",
            "route_case_id": self.route_case_id,
            "source_encounter_id": self.source_encounter_id,
            "package_id": self.package_id,
            "route_encounter_ids": list(self.route_encounter_ids),
            "paired_seeds": list(self.seeds),
            "downstream": [row.to_dict() for row in self.downstream],
            "bounds_method": "MIN_MAX_OVER_PAIRED_SEEDS_NO_AVERAGING",
            "is_persistent_per_seed": self.is_persistent_per_seed,
            "is_exact_zero": self.is_exact_zero,
            "composition_scope": "ONE_PACKAGE_PERTURBATION_AGAINST_BASELINE",
        }


def calibrate_route_timing_shift_profile_v1(
    observations: Iterable[PairedContinuousRouteTimingObservationV1],
) -> RouteTimingShiftProfileV1:
    """Build per-downstream-cell bounds from same-seed route observations."""

    rows = tuple(observations)
    if not rows or any(
        not isinstance(row, PairedContinuousRouteTimingObservationV1)
        for row in rows
    ):
        raise TypeError(
            "observations must contain PairedContinuousRouteTimingObservationV1"
        )
    exemplar = rows[0]
    identity = (
        exemplar.route_case_id,
        exemplar.source_encounter_id,
        exemplar.package_id,
        exemplar.route_encounter_ids,
    )
    for row in rows[1:]:
        if (
            row.route_case_id,
            row.source_encounter_id,
            row.package_id,
            row.route_encounter_ids,
        ) != identity:
            raise ValueError(
                "all timing observations must describe the same route and package"
            )
    seeds = [row.seed for row in rows]
    if len(seeds) != len(set(seeds)):
        raise ValueError("paired timing observations must have unique seeds")

    ordered = tuple(sorted(rows, key=lambda row: row.seed))
    downstream: list[DownstreamStartShiftBoundsV1] = []
    for route_index in range(exemplar.source_index + 1, len(exemplar.route_encounter_ids)):
        shifts = tuple(
            (
                row.seed,
                row.candidate_starts[route_index].start_ms
                - row.baseline_starts[route_index].start_ms,
            )
            for row in ordered
        )
        downstream.append(DownstreamStartShiftBoundsV1(
            encounter_id=exemplar.route_encounter_ids[route_index],
            route_index=route_index,
            seed_shifts_ms=shifts,
        ))
    return RouteTimingShiftProfileV1(
        route_case_id=exemplar.route_case_id,
        source_encounter_id=exemplar.source_encounter_id,
        package_id=exemplar.package_id,
        route_encounter_ids=exemplar.route_encounter_ids,
        downstream=tuple(downstream),
    )


def persistent_route_time_shift_bounds_v1(
    profiles: Iterable[RouteTimingShiftProfileV1],
    *,
    expected_route_encounter_ids: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Adapt only allocator-safe profiles to legacy scalar shift bounds.

    Each profile was measured as one perturbation against the same baseline.
    Such observations do not identify interactions between two nonzero timing
    perturbations.  Consequently v1 admits at most one nonzero profile; any
    number of profiles proven to have exactly zero downstream shift are safe.
    """

    rows = tuple(profiles)
    if any(not isinstance(row, RouteTimingShiftProfileV1) for row in rows):
        raise TypeError("profiles must contain RouteTimingShiftProfileV1 values")
    if expected_route_encounter_ids is not None:
        if (
            not isinstance(expected_route_encounter_ids, tuple)
            or not expected_route_encounter_ids
            or any(
                not isinstance(value, str) or not value
                for value in expected_route_encounter_ids
            )
        ):
            raise ValueError(
                "expected_route_encounter_ids must be a nonempty tuple of strings"
            )
        mismatched = [
            row.package_key
            for row in rows
            if row.route_encounter_ids != expected_route_encounter_ids
        ]
        if mismatched:
            raise ValueError(
                "route timing profile does not match allocator encounter order: "
                + repr(mismatched)
            )
    keys = [row.package_key for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("route timing profile package keys must be unique")
    route_case_ids = {row.route_case_id for row in rows}
    if len(route_case_ids) > 1:
        raise ValueError(
            "route timing profiles must share one frozen route_case_id"
        )
    nonzero = [row.package_key for row in rows if not row.is_exact_zero]
    if len(nonzero) > 1:
        raise ValueError(
            "independent one-package timing profiles do not prove multi-package "
            "composition; continuous-route joint calibration is required: "
            + repr(nonzero)
        )
    return {
        row.package_key: row.persistent_shift_bounds()
        for row in rows
    }


__all__ = (
    "DownstreamStartShiftBoundsV1",
    "PairedContinuousRouteTimingObservationV1",
    "RouteEncounterStartV1",
    "RouteTimingShiftProfileV1",
    "calibrate_route_timing_shift_profile_v1",
    "persistent_route_time_shift_bounds_v1",
)
