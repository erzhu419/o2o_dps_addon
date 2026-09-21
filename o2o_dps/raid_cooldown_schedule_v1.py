"""Exact route-level allocation of paired-measured long-cooldown packages.

The inner wave search owns the complete action sequence for one encounter.  It
emits mutually exclusive packages such as ``no burst``, ``pre-pot + Death
Wish`` or ``Recklessness at +1.5 s`` together with paired-seed marginal value.
This module selects one package per ordered trash wave or boss while enforcing
the actual shared cooldown clocks across the route.

Historical casts from Chronicle and Cat/Contra decisions may propose packages;
they are not objective values.  Every admitted package must carry a marginal
value measured against the same cell, build, resources and seeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Iterable, Mapping

from .route_timing_calibration_v1 import (
    RouteTimingShiftProfileV1,
    persistent_route_time_shift_bounds_v1,
)


TRASH = "TRASH"
BOSS = "BOSS"


@dataclass(frozen=True)
class RoutePersistentStatPhaseV1:
    """One half-open stat-delta interval relative to an accepted action."""

    phase_id: str
    starts_after_use_ms: int
    duration_ms: int
    stat_deltas: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.phase_id, str) or not self.phase_id.strip():
            raise ValueError("phase_id must be nonempty")
        if type(self.starts_after_use_ms) is not int or self.starts_after_use_ms < 0:
            raise ValueError("starts_after_use_ms must be a nonnegative integer")
        if type(self.duration_ms) is not int or self.duration_ms <= 0:
            raise ValueError("duration_ms must be a positive integer")
        if not isinstance(self.stat_deltas, tuple) or not self.stat_deltas:
            raise TypeError("stat_deltas must be a nonempty tuple")
        names = []
        for name, value in self.stat_deltas:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("stat delta names must be nonempty")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) == 0
            ):
                raise ValueError("stat deltas must be finite nonzero numbers")
            names.append(name)
        if names != sorted(set(names)):
            raise ValueError("stat_deltas must have sorted unique stat names")

    @property
    def ends_after_use_ms(self) -> int:
        return self.starts_after_use_ms + self.duration_ms

    def to_dict(self) -> dict[str, object]:
        return {
            "phase_id": self.phase_id,
            "starts_after_use_ms": self.starts_after_use_ms,
            "ends_after_use_ms": self.ends_after_use_ms,
            "duration_ms": self.duration_ms,
            "stat_deltas": {
                name: float(value) for name, value in self.stat_deltas
            },
            "interval_semantics": "HALF_OPEN_START_INCLUSIVE_END_EXCLUSIVE",
        }


@dataclass(frozen=True)
class RoutePersistentEffectV1:
    """Deterministic post-use state phases that can cross encounter bounds."""

    effect_id: str
    phases: tuple[RoutePersistentStatPhaseV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id.strip():
            raise ValueError("effect_id must be nonempty")
        if not isinstance(self.phases, tuple) or not self.phases or any(
            not isinstance(phase, RoutePersistentStatPhaseV1)
            for phase in self.phases
        ):
            raise TypeError(
                "phases must be a nonempty tuple of RoutePersistentStatPhaseV1"
            )
        phase_ids = [phase.phase_id for phase in self.phases]
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("persistent effect phase IDs must be unique")
        starts = [phase.starts_after_use_ms for phase in self.phases]
        if starts != sorted(starts):
            raise ValueError("persistent effect phases must be time ordered")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "route_persistent_effect/v1",
            "effect_id": self.effect_id,
            "phases": [phase.to_dict() for phase in self.phases],
        }


@dataclass(frozen=True)
class RoutePersistentEffectIntervalV1:
    """One materialized absolute route interval from an accepted use."""

    effect_id: str
    resource_id: str
    phase_id: str
    starts_at_ms: int
    ends_at_ms: int
    stat_deltas: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("effect_id", self.effect_id),
            ("resource_id", self.resource_id),
            ("phase_id", self.phase_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonempty")
        if type(self.starts_at_ms) is not int or type(self.ends_at_ms) is not int:
            raise TypeError("persistent interval bounds must be integers")
        if self.ends_at_ms <= self.starts_at_ms:
            raise ValueError("persistent interval must have positive duration")

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "resource_id": self.resource_id,
            "phase_id": self.phase_id,
            "starts_at_ms": self.starts_at_ms,
            "ends_at_ms": self.ends_at_ms,
            "stat_deltas": {
                name: float(value) for name, value in self.stat_deltas
            },
            "interval_semantics": "HALF_OPEN_START_INCLUSIVE_END_EXCLUSIVE",
        }


@dataclass(frozen=True)
class CooldownUseV1:
    """One player-controlled long-cooldown action on the pull-relative clock."""

    resource_id: str
    cooldown_group: str
    cooldown_ms: int
    use_at_ms: int
    action_kind: str
    persistent_effect: RoutePersistentEffectV1 | None = None
    earliest_use_at_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.resource_id.strip() or not self.cooldown_group.strip():
            raise ValueError("resource_id and cooldown_group must be nonempty")
        if not self.action_kind.strip():
            raise ValueError("action_kind must be nonempty")
        if type(self.cooldown_ms) is not int or self.cooldown_ms <= 0:
            raise ValueError("cooldown_ms must be a positive integer")
        if type(self.use_at_ms) is not int:
            raise ValueError("use_at_ms must be an integer relative to pull")
        if self.earliest_use_at_ms is not None and type(
            self.earliest_use_at_ms
        ) is not int:
            raise ValueError("earliest_use_at_ms must be an integer or None")
        if self.use_window_start_ms > self.use_at_ms:
            raise ValueError(
                "earliest_use_at_ms cannot be later than use_at_ms"
            )
        if self.use_window_start_ms < 0 <= self.use_at_ms:
            raise ValueError(
                "paired-seed cooldown use times cannot cross the pull boundary"
            )
        if self.persistent_effect is not None and not isinstance(
            self.persistent_effect, RoutePersistentEffectV1
        ):
            raise TypeError(
                "persistent_effect must be RoutePersistentEffectV1 or None"
            )

    @property
    def use_window_start_ms(self) -> int:
        """Earliest paired-seed use; ``use_at_ms`` remains the latest."""

        return (
            self.use_at_ms
            if self.earliest_use_at_ms is None
            else self.earliest_use_at_ms
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "cooldown_group": self.cooldown_group,
            "cooldown_ms": self.cooldown_ms,
            "use_at_ms": self.use_at_ms,
            "earliest_use_at_ms": self.use_window_start_ms,
            "latest_use_at_ms": self.use_at_ms,
            "precombat": self.use_at_ms < 0,
            "action_kind": self.action_kind,
            "persistent_effect": (
                self.persistent_effect.to_dict()
                if self.persistent_effect is not None
                else None
            ),
        }


@dataclass(frozen=True)
class CooldownPackageV1:
    """One jointly simulated long-cooldown alternative for an encounter."""

    package_id: str
    uses: tuple[CooldownUseV1, ...]
    marginal_value: float
    evidence_status: str = "PAIRED_MEASURED"
    encounter_elapsed_ms_delta_min: int = 0
    encounter_elapsed_ms_delta_max: int = 0

    def __post_init__(self) -> None:
        if not self.package_id.strip():
            raise ValueError("package_id must be nonempty")
        if not isinstance(self.uses, tuple) or any(
            not isinstance(use, CooldownUseV1) for use in self.uses
        ):
            raise TypeError("uses must be a tuple of CooldownUseV1")
        identities = [(use.resource_id, use.use_at_ms) for use in self.uses]
        if len(identities) != len(set(identities)):
            raise ValueError("a package contains a duplicate resource use")
        if (
            isinstance(self.marginal_value, bool)
            or not isinstance(self.marginal_value, (int, float))
            or not math.isfinite(float(self.marginal_value))
        ):
            raise ValueError("marginal_value must be finite")
        if self.evidence_status != "PAIRED_MEASURED":
            raise ValueError("package values must come from paired measurements")
        if type(self.encounter_elapsed_ms_delta_min) is not int or type(
            self.encounter_elapsed_ms_delta_max
        ) is not int:
            raise ValueError("encounter elapsed-time delta bounds must be integers")
        if self.encounter_elapsed_ms_delta_min > self.encounter_elapsed_ms_delta_max:
            raise ValueError(
                "encounter elapsed-time delta minimum cannot exceed maximum"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "package_id": self.package_id,
            "marginal_value": float(self.marginal_value),
            "evidence_status": self.evidence_status,
            "encounter_elapsed_ms_delta_min": self.encounter_elapsed_ms_delta_min,
            "encounter_elapsed_ms_delta_max": self.encounter_elapsed_ms_delta_max,
            "uses": [use.to_dict() for use in self.uses],
        }


@dataclass(frozen=True)
class EncounterCooldownCellV1:
    """Mutually exclusive inner-search packages for one route encounter."""

    encounter_id: str
    encounter_kind: str
    raid_start_ms: int
    packages: tuple[CooldownPackageV1, ...]

    def __post_init__(self) -> None:
        if not self.encounter_id.strip():
            raise ValueError("encounter_id must be nonempty")
        if self.encounter_kind not in {TRASH, BOSS}:
            raise ValueError("encounter_kind must be TRASH or BOSS")
        if type(self.raid_start_ms) is not int or self.raid_start_ms < 0:
            raise ValueError("raid_start_ms must be a nonnegative integer")
        if not isinstance(self.packages, tuple) or any(
            not isinstance(package, CooldownPackageV1)
            for package in self.packages
        ):
            raise TypeError("packages must be a tuple of CooldownPackageV1")
        package_ids = [package.package_id for package in self.packages]
        if len(package_ids) != len(set(package_ids)):
            raise ValueError("package_id values must be unique within an encounter")


@dataclass(frozen=True)
class CooldownPackageAssignmentV1:
    encounter_id: str
    encounter_kind: str
    raid_start_ms: int
    package: CooldownPackageV1
    route_start_shift_ms_min: int = 0
    route_start_shift_ms_max: int = 0
    downstream_route_shift_ms_min: int = 0
    downstream_route_shift_ms_max: int = 0

    def __post_init__(self) -> None:
        if type(self.route_start_shift_ms_min) is not int or type(
            self.route_start_shift_ms_max
        ) is not int:
            raise ValueError("route start-shift bounds must be integers")
        if self.route_start_shift_ms_min > self.route_start_shift_ms_max:
            raise ValueError("route start-shift minimum cannot exceed maximum")
        if type(self.downstream_route_shift_ms_min) is not int or type(
            self.downstream_route_shift_ms_max
        ) is not int:
            raise ValueError("downstream route-shift bounds must be integers")
        if (
            self.downstream_route_shift_ms_min
            > self.downstream_route_shift_ms_max
        ):
            raise ValueError(
                "downstream route-shift minimum cannot exceed maximum"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "encounter_id": self.encounter_id,
            "encounter_kind": self.encounter_kind,
            "raid_start_ms": self.raid_start_ms,
            "raid_start_clock": "BASELINE_ROUTE_CLOCK",
            "route_start_shift_ms_min": self.route_start_shift_ms_min,
            "route_start_shift_ms_max": self.route_start_shift_ms_max,
            "downstream_route_shift_ms_min": (
                self.downstream_route_shift_ms_min
            ),
            "downstream_route_shift_ms_max": (
                self.downstream_route_shift_ms_max
            ),
            "earliest_raid_start_ms": (
                self.raid_start_ms + self.route_start_shift_ms_min
            ),
            "latest_raid_start_ms": (
                self.raid_start_ms + self.route_start_shift_ms_max
            ),
            "package": self.package.to_dict(),
            "absolute_uses": [
                {
                    **use.to_dict(),
                    "raid_use_time_ms": (
                        self.raid_start_ms
                        + self.route_start_shift_ms_min
                        + use.use_at_ms
                        if self.route_start_shift_ms_min
                        == self.route_start_shift_ms_max
                        and use.use_window_start_ms == use.use_at_ms
                        else None
                    ),
                    "earliest_raid_use_time_ms": (
                        self.raid_start_ms
                        + self.route_start_shift_ms_min
                        + use.use_window_start_ms
                    ),
                    "latest_raid_use_time_ms": (
                        self.raid_start_ms
                        + self.route_start_shift_ms_max
                        + use.use_at_ms
                    ),
                }
                for use in self.package.uses
            ],
        }


def materialize_route_persistent_effect_intervals_v1(
    assignments: Iterable[CooldownPackageAssignmentV1],
) -> tuple[RoutePersistentEffectIntervalV1, ...]:
    """Place all declared effect phases on the absolute raid clock."""

    rows = tuple(assignments)
    if any(not isinstance(row, CooldownPackageAssignmentV1) for row in rows):
        raise TypeError(
            "assignments must contain CooldownPackageAssignmentV1 values"
        )
    result: list[RoutePersistentEffectIntervalV1] = []
    for assignment in rows:
        for use in assignment.package.uses:
            effect = use.persistent_effect
            if effect is None:
                continue
            if (
                assignment.route_start_shift_ms_min
                != assignment.route_start_shift_ms_max
                or use.use_window_start_ms != use.use_at_ms
            ):
                raise ValueError(
                    "persistent effect materialization requires an exact route "
                    "start and use time"
                )
            absolute_use_ms = (
                assignment.raid_start_ms
                + assignment.route_start_shift_ms_min
                + use.use_at_ms
            )
            for phase in effect.phases:
                start = absolute_use_ms + phase.starts_after_use_ms
                result.append(RoutePersistentEffectIntervalV1(
                    effect_id=effect.effect_id,
                    resource_id=use.resource_id,
                    phase_id=phase.phase_id,
                    starts_at_ms=start,
                    ends_at_ms=start + phase.duration_ms,
                    stat_deltas=phase.stat_deltas,
                ))
    return tuple(sorted(
        result,
        key=lambda row: (
            row.starts_at_ms,
            row.ends_at_ms,
            row.effect_id,
            row.resource_id,
            row.phase_id,
        ),
    ))


def route_stat_deltas_at_ms_v1(
    intervals: Iterable[RoutePersistentEffectIntervalV1],
    at_ms: int,
) -> dict[str, float]:
    """Apply the transition contract at one route timestamp."""

    if type(at_ms) is not int:
        raise TypeError("at_ms must be an integer")
    rows = tuple(intervals)
    if any(not isinstance(row, RoutePersistentEffectIntervalV1) for row in rows):
        raise TypeError(
            "intervals must contain RoutePersistentEffectIntervalV1 values"
        )
    totals: dict[str, float] = {}
    for row in rows:
        if row.starts_at_ms <= at_ms < row.ends_at_ms:
            for name, value in row.stat_deltas:
                totals[name] = totals.get(name, 0.0) + float(value)
    return dict(sorted(totals.items()))


@dataclass(frozen=True)
class RaidCooldownScheduleV1:
    assignments: tuple[CooldownPackageAssignmentV1, ...]
    total_marginal_value: float
    boss_independent_groups: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "raid_cooldown_schedule/v1",
            "objective": "SUM_OF_PAIRED_MEASURED_ENCOUNTER_PACKAGE_MARGINAL_VALUE",
            "total_marginal_value": float(self.total_marginal_value),
            "boss_independent_groups": list(self.boss_independent_groups),
            "assignments": [row.to_dict() for row in self.assignments],
            "guide_role": "OFFLINE_AND_EXPERT_TIMINGS_PROPOSE_PACKAGES_ONLY",
            "route_timing_policy": (
                "EXPLICIT_CALIBRATED_DOWNSTREAM_SHIFT_AND_CONSERVATIVE_"
                "PAIRED_SEED_USE_TIME_BOUNDS"
            ),
        }


def allocate_raid_cooldown_packages_v1(
    cells: Iterable[EncounterCooldownCellV1],
    *,
    boss_independent_groups: Iterable[str] = ("combat_potion",),
    initial_next_available_ms: Mapping[str, int] | None = None,
    route_time_shift_bounds: Mapping[
        tuple[str, str], tuple[int, int]
    ] | None = None,
    route_timing_profiles: Iterable[RouteTimingShiftProfileV1] | None = None,
) -> RaidCooldownScheduleV1:
    """Select one profitable package per encounter under route cooldown clocks.

    ``boss_independent_groups`` implements only explicitly supplied route
    assumptions.  By default the user's two-minute potion assumption isolates
    the combat-potion clock at bosses; Death Wish, Recklessness, trinkets and
    other cooldowns remain coupled across every wave and boss.

    A local replay's elapsed-time delta is evidence about the encounter, not
    automatically the next pull's clock shift: travel, doors and fixed raid
    pacing may absorb it.  Every positive package with a nonzero measured
    elapsed delta therefore needs an explicit calibrated downstream shift in
    ``route_time_shift_bounds[(encounter_id, package_id)]``.  This avoids both
    mean-rounding at cooldown boundaries and silently assuming one-for-one
    propagation.  Prefer ``route_timing_profiles`` for new callers: it retains
    per-edge, per-seed evidence and only adapts to this allocator when the
    scalar persistent-shift model is actually valid.  The raw bounds argument
    remains the legacy expert interface and is mutually exclusive with typed
    profiles.
    """

    ordered = tuple(sorted(cells, key=lambda row: (row.raid_start_ms, row.encounter_id)))
    if any(not isinstance(row, EncounterCooldownCellV1) for row in ordered):
        raise TypeError("cells must contain EncounterCooldownCellV1 values")
    encounter_ids = [row.encounter_id for row in ordered]
    if len(encounter_ids) != len(set(encounter_ids)):
        raise ValueError("encounter_id values must be unique")

    persistent_resources = sorted({
        use.resource_id
        for row in ordered
        for package in row.packages
        for use in package.uses
        if use.persistent_effect is not None
    })
    if persistent_resources:
        raise ValueError(
            "route-persistent effects require cross-encounter state-aware "
            "replay before allocation; isolated encounter marginal values "
            "are not additive: " + ", ".join(persistent_resources)
        )

    independent = tuple(sorted(set(boss_independent_groups)))
    if any(not isinstance(group, str) or not group.strip() for group in independent):
        raise ValueError("boss_independent_groups must contain nonempty strings")
    initial = dict(initial_next_available_ms or {})
    if any(
        not isinstance(group, str)
        or not group.strip()
        or type(value) is not int
        for group, value in initial.items()
    ):
        raise ValueError("initial_next_available_ms must map group names to integers")

    if route_time_shift_bounds is not None and route_timing_profiles is not None:
        raise ValueError(
            "route_time_shift_bounds and route_timing_profiles are mutually "
            "exclusive"
        )
    if route_timing_profiles is not None:
        timing = persistent_route_time_shift_bounds_v1(
            route_timing_profiles,
            expected_route_encounter_ids=tuple(
                row.encounter_id for row in ordered
            ),
        )
    else:
        timing = dict(route_time_shift_bounds or {})
    known_package_keys = {
        (row.encounter_id, package.package_id)
        for row in ordered
        for package in row.packages
    }
    unknown_timing_keys = sorted(set(timing) - known_package_keys)
    if unknown_timing_keys:
        raise ValueError(
            "route_time_shift_bounds contains unknown package keys: "
            f"{unknown_timing_keys}"
        )
    for key, bounds in timing.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or any(not isinstance(value, str) or not value for value in key)
            or not isinstance(bounds, tuple)
            or len(bounds) != 2
            or any(type(value) is not int for value in bounds)
            or bounds[0] > bounds[1]
        ):
            raise ValueError(
                "route_time_shift_bounds must map (encounter_id, package_id) "
                "to ordered integer (minimum, maximum) bounds"
            )
    missing_timing = sorted(
        (row.encounter_id, package.package_id)
        for row in ordered
        for package in row.packages
        if package.marginal_value > 0
        and (
            package.encounter_elapsed_ms_delta_min != 0
            or package.encounter_elapsed_ms_delta_max != 0
        )
        and (row.encounter_id, package.package_id) not in timing
    )
    if missing_timing:
        raise ValueError(
            "nonzero encounter elapsed-time deltas require explicit calibrated "
            "route_time_shift_bounds: " + repr(missing_timing)
        )

    groups = tuple(sorted({
        use.cooldown_group
        for row in ordered
        for package in row.packages
        for use in package.uses
    } | set(initial)))
    group_index = {group: index for index, group in enumerate(groups)}
    independent_set = frozenset(independent)
    negative_infinity = -(1 << 60)
    initial_clock = tuple(initial.get(group, negative_infinity) for group in groups)

    @lru_cache(maxsize=None)
    def solve(
        index: int,
        next_available: tuple[int, ...],
        route_shift_min: int,
        route_shift_max: int,
    ) -> tuple[float, tuple[CooldownPackageAssignmentV1, ...]]:
        if index == len(ordered):
            return 0.0, ()
        cell = ordered[index]
        best_value, best_rows = solve(
            index + 1,
            next_available,
            route_shift_min,
            route_shift_max,
        )
        for package in cell.packages:
            if package.marginal_value <= 0:
                continue
            package_shift_min, package_shift_max = timing.get(
                (cell.encounter_id, package.package_id),
                (0, 0),
            )
            updated = _apply_package_v1(
                cell,
                package,
                next_available,
                group_index=group_index,
                boss_independent_groups=independent_set,
                route_shift_min=route_shift_min,
                route_shift_max=route_shift_max,
            )
            if updated is None:
                continue
            next_shift_min = (
                route_shift_min + package_shift_min
            )
            next_shift_max = (
                route_shift_max + package_shift_max
            )
            suffix_value, suffix_rows = solve(
                index + 1,
                updated,
                next_shift_min,
                next_shift_max,
            )
            candidate_value = float(package.marginal_value) + suffix_value
            candidate = CooldownPackageAssignmentV1(
                encounter_id=cell.encounter_id,
                encounter_kind=cell.encounter_kind,
                raid_start_ms=cell.raid_start_ms,
                package=package,
                route_start_shift_ms_min=route_shift_min,
                route_start_shift_ms_max=route_shift_max,
                downstream_route_shift_ms_min=package_shift_min,
                downstream_route_shift_ms_max=package_shift_max,
            )
            candidate_rows = (candidate, *suffix_rows)
            if _better_v1(candidate_value, candidate_rows, best_value, best_rows):
                best_value, best_rows = candidate_value, candidate_rows
        return best_value, best_rows

    value, assignments = solve(0, initial_clock, 0, 0)
    return RaidCooldownScheduleV1(
        assignments=assignments,
        total_marginal_value=value,
        boss_independent_groups=independent,
    )


def _apply_package_v1(
    cell: EncounterCooldownCellV1,
    package: CooldownPackageV1,
    next_available: tuple[int, ...],
    *,
    group_index: Mapping[str, int],
    boss_independent_groups: frozenset[str],
    route_shift_min: int,
    route_shift_max: int,
) -> tuple[int, ...] | None:
    updated = list(next_available)
    uses = sorted(
        package.uses,
        key=lambda use: (
            cell.raid_start_ms + use.use_at_ms,
            use.cooldown_group,
            use.resource_id,
        ),
    )
    isolated_clock: dict[str, int] = {}
    for use in uses:
        absolute_earliest_time = (
            cell.raid_start_ms
            + route_shift_min
            + use.use_window_start_ms
        )
        absolute_latest_time = (
            cell.raid_start_ms + route_shift_max + use.use_at_ms
        )
        isolated = (
            cell.encounter_kind == BOSS
            and use.cooldown_group in boss_independent_groups
        )
        if isolated:
            ready_at = isolated_clock.get(use.cooldown_group, -(1 << 60))
        else:
            slot = group_index[use.cooldown_group]
            ready_at = updated[slot]
        if absolute_earliest_time < ready_at:
            return None
        next_ready = absolute_latest_time + use.cooldown_ms
        if isolated:
            isolated_clock[use.cooldown_group] = next_ready
        else:
            updated[group_index[use.cooldown_group]] = next_ready
    return tuple(updated)


def _better_v1(
    candidate_value: float,
    candidate_rows: tuple[CooldownPackageAssignmentV1, ...],
    incumbent_value: float,
    incumbent_rows: tuple[CooldownPackageAssignmentV1, ...],
) -> bool:
    if not math.isclose(candidate_value, incumbent_value, abs_tol=1e-12):
        return candidate_value > incumbent_value
    candidate_key = tuple(
        (row.raid_start_ms, row.encounter_id, row.package.package_id)
        for row in candidate_rows
    )
    incumbent_key = tuple(
        (row.raid_start_ms, row.encounter_id, row.package.package_id)
        for row in incumbent_rows
    )
    return candidate_key < incumbent_key


__all__ = (
    "BOSS",
    "TRASH",
    "CooldownPackageAssignmentV1",
    "CooldownPackageV1",
    "CooldownUseV1",
    "EncounterCooldownCellV1",
    "RaidCooldownScheduleV1",
    "RoutePersistentEffectIntervalV1",
    "RoutePersistentEffectV1",
    "RoutePersistentStatPhaseV1",
    "allocate_raid_cooldown_packages_v1",
    "materialize_route_persistent_effect_intervals_v1",
    "route_stat_deltas_at_ms_v1",
)
