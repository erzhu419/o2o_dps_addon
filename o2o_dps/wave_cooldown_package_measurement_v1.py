"""Turn complete paired wave replays into route-planner cooldown packages.

The inner search chooses the whole action schedule.  This adapter measures one
such schedule against a no-package (or other declared) schedule on the same
seeds, extracts the long-cooldown actions that were *actually accepted*, and
emits the ``CooldownPackageV1`` consumed by the route allocator.

The default scalar is paired own effective damage, which is additive across a
fixed route.  Elapsed-time deltas are retained separately; per-encounter DPS
deltas are deliberately not summed as if they were raid DPS.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean
from typing import Any, Mapping, Sequence

from .raid_cooldown_schedule_v1 import (
    CooldownPackageV1,
    CooldownUseV1,
    RoutePersistentEffectV1,
)
from .sim_bridge import ActionRef
from .wave_action_schedule_v1 import ScheduledActionPlan
from .wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    ScheduleReplayV1,
)


JSONMap = dict[str, Any]
OWN_EFFECTIVE_DAMAGE = "OWN_EFFECTIVE_DAMAGE"


@dataclass(frozen=True)
class CooldownActionBindingV1:
    """Route-level cooldown identity for one native simulator action."""

    action: ActionRef
    resource_id: str
    cooldown_group: str
    cooldown_ms: int
    action_kind: str
    persistent_effect: RoutePersistentEffectV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionRef):
            raise TypeError("action must be ActionRef")
        for name, value in (
            ("resource_id", self.resource_id),
            ("cooldown_group", self.cooldown_group),
            ("action_kind", self.action_kind),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.cooldown_ms) is not int or self.cooldown_ms <= 0:
            raise ValueError("cooldown_ms must be a positive integer")
        if self.persistent_effect is not None and not isinstance(
            self.persistent_effect, RoutePersistentEffectV1
        ):
            raise TypeError(
                "persistent_effect must be RoutePersistentEffectV1 or None"
            )


@dataclass(frozen=True)
class PairedScheduleDeltaV1:
    seed: int
    baseline_effective_damage: float
    candidate_effective_damage: float
    baseline_elapsed_ms: int
    candidate_elapsed_ms: int

    @property
    def effective_damage_delta(self) -> float:
        return self.candidate_effective_damage - self.baseline_effective_damage

    @property
    def elapsed_ms_delta(self) -> int:
        return self.candidate_elapsed_ms - self.baseline_elapsed_ms

    def to_dict(self) -> JSONMap:
        return {
            "seed": self.seed,
            "baseline_effective_damage": self.baseline_effective_damage,
            "candidate_effective_damage": self.candidate_effective_damage,
            "effective_damage_delta": self.effective_damage_delta,
            "baseline_elapsed_ms": self.baseline_elapsed_ms,
            "candidate_elapsed_ms": self.candidate_elapsed_ms,
            "elapsed_ms_delta": self.elapsed_ms_delta,
        }


@dataclass(frozen=True)
class MeasuredCooldownPackageV1:
    package: CooldownPackageV1
    paired_rows: tuple[PairedScheduleDeltaV1, ...]
    objective: str
    observed_use_times_by_seed: tuple[tuple[int, ...], ...]

    def to_dict(self) -> JSONMap:
        persistent_resources = [
            use.resource_id
            for use in self.package.uses
            if use.persistent_effect is not None
        ]
        elapsed_delta_bounds = (
            min(row.elapsed_ms_delta for row in self.paired_rows),
            max(row.elapsed_ms_delta for row in self.paired_rows),
        )
        return {
            "schema": "wave_cooldown_package_measurement/v1",
            "package": self.package.to_dict(),
            "objective": self.objective,
            "paired_seed_count": len(self.paired_rows),
            "paired_rows": [row.to_dict() for row in self.paired_rows],
            "mean_effective_damage_delta": mean(
                row.effective_damage_delta for row in self.paired_rows
            ),
            "mean_elapsed_ms_delta": mean(
                row.elapsed_ms_delta for row in self.paired_rows
            ),
            "encounter_elapsed_ms_delta_bounds": list(elapsed_delta_bounds),
            "observed_use_times_by_seed": [
                list(row) for row in self.observed_use_times_by_seed
            ],
            "route_use_time_policy": (
                "LATEST_OBSERVED_PAIRED_SEED_RELATIVE_TIME_PER_OCCURRENCE"
            ),
            "comparison_contract": (
                "FRESH_REPLAY_SAME_CELL_BUILD_RESOURCES_AND_SEEDS"
            ),
            "objective_scope": "ISOLATED_ENCOUNTER",
            "persistent_effect_resource_ids": persistent_resources,
            "route_allocation_status": (
                "REQUIRES_CROSS_ENCOUNTER_STATE_AWARE_REPLAY"
                if persistent_resources
                else (
                    "REQUIRES_EXPLICIT_CALIBRATED_ROUTE_TIME_SHIFT"
                    if elapsed_delta_bounds != (0, 0)
                    else "ADDITIVE_ENCOUNTER_MARGINAL_ZERO_TIME_SHIFT"
                )
            ),
        }


def measure_cooldown_package_v1(
    replay: ScheduleReplayV1,
    *,
    package_id: str,
    baseline_schedule: Sequence[ScheduledActionPlan],
    candidate_schedule: Sequence[ScheduledActionPlan],
    seeds: Sequence[int],
    bindings: Sequence[CooldownActionBindingV1],
    objective: str = OWN_EFFECTIVE_DAMAGE,
) -> MeasuredCooldownPackageV1:
    """Measure one full-schedule alternative and extract its cooldown uses.

    Every seed is replayed independently for baseline and candidate.  Both
    sides must finish.  A candidate is not eligible for route allocation when
    a bound action is skipped on only some seeds, because then it is not one
    well-defined cooldown package.
    """

    if not isinstance(package_id, str) or not package_id.strip():
        raise ValueError("package_id must be nonempty")
    normalized_seeds = tuple(seeds)
    if not normalized_seeds or any(
        isinstance(seed, bool) or not isinstance(seed, int)
        for seed in normalized_seeds
    ):
        raise ValueError("seeds must contain integers")
    if len(set(normalized_seeds)) != len(normalized_seeds):
        raise ValueError("seeds must be unique")
    if objective != OWN_EFFECTIVE_DAMAGE:
        raise ValueError(
            "v1 admits only additive paired own effective damage"
        )
    binding_rows = tuple(bindings)
    if any(not isinstance(row, CooldownActionBindingV1) for row in binding_rows):
        raise TypeError("bindings must contain CooldownActionBindingV1")
    if len({row.action for row in binding_rows}) != len(binding_rows):
        raise ValueError("bindings must have unique ActionRef values")

    baseline_outcomes: list[ScheduleReplayOutcomeV1] = []
    candidate_outcomes: list[ScheduleReplayOutcomeV1] = []
    for seed in normalized_seeds:
        baseline_outcomes.append(replay.replay(seed, baseline_schedule))
        candidate_outcomes.append(replay.replay(seed, candidate_schedule))
    return measure_cooldown_package_from_outcomes_v1(
        package_id=package_id,
        baseline_outcomes=baseline_outcomes,
        candidate_outcomes=candidate_outcomes,
        bindings=binding_rows,
        objective=objective,
    )


def measure_cooldown_package_from_outcomes_v1(
    *,
    package_id: str,
    baseline_outcomes: Sequence[ScheduleReplayOutcomeV1],
    candidate_outcomes: Sequence[ScheduleReplayOutcomeV1],
    bindings: Sequence[CooldownActionBindingV1],
    objective: str = OWN_EFFECTIVE_DAMAGE,
) -> MeasuredCooldownPackageV1:
    """Measure a package from already-computed paired terminal outcomes.

    Search workers already hold the full final-state and operation receipts for
    every seed.  Reusing those terminal artifacts preserves the same paired
    measurement contract without launching two redundant simulator replays per
    package arm.
    """

    if not isinstance(package_id, str) or not package_id.strip():
        raise ValueError("package_id must be nonempty")
    if objective != OWN_EFFECTIVE_DAMAGE:
        raise ValueError("v1 admits only additive paired own effective damage")
    baseline_rows = tuple(baseline_outcomes)
    candidate_rows = tuple(candidate_outcomes)
    if not baseline_rows or len(baseline_rows) != len(candidate_rows):
        raise ValueError(
            "baseline_outcomes and candidate_outcomes must be nonempty and paired"
        )
    binding_rows = tuple(bindings)
    if any(not isinstance(row, CooldownActionBindingV1) for row in binding_rows):
        raise TypeError("bindings must contain CooldownActionBindingV1")
    by_action = {row.action: row for row in binding_rows}
    if len(by_action) != len(binding_rows):
        raise ValueError("bindings must have unique ActionRef values")

    paired: list[PairedScheduleDeltaV1] = []
    uses_per_seed: list[tuple[tuple[CooldownActionBindingV1, int], ...]] = []
    for baseline, candidate in zip(
        baseline_rows, candidate_rows, strict=True
    ):
        if not isinstance(baseline, ScheduleReplayOutcomeV1) or not isinstance(
            candidate, ScheduleReplayOutcomeV1
        ):
            raise TypeError("paired outcomes must be ScheduleReplayOutcomeV1")
        if baseline.seed != candidate.seed:
            raise ValueError("paired outcomes have different seeds")
        seed = baseline.seed
        _require_complete(seed, "baseline", baseline)
        _require_complete(seed, "candidate", candidate)
        paired.append(PairedScheduleDeltaV1(
            seed=seed,
            baseline_effective_damage=baseline.effective_damage,
            candidate_effective_damage=candidate.effective_damage,
            baseline_elapsed_ms=baseline.elapsed_ms,
            candidate_elapsed_ms=candidate.elapsed_ms,
        ))
        uses_per_seed.append(_accepted_uses(candidate, by_action))

    identities = [tuple(row.resource_id for row, _ in uses) for uses in uses_per_seed]
    if any(identity != identities[0] for identity in identities[1:]):
        raise ValueError(
            "candidate bound cooldown actions differ across paired seeds"
        )
    route_uses: list[CooldownUseV1] = []
    if uses_per_seed:
        for occurrence in range(len(uses_per_seed[0])):
            exemplar = uses_per_seed[0][occurrence][0]
            times = [uses[occurrence][1] for uses in uses_per_seed]
            if any(uses[occurrence][0] != exemplar for uses in uses_per_seed):
                raise ValueError(
                    "candidate cooldown action order differs across paired seeds"
                )
            route_uses.append(CooldownUseV1(
                resource_id=exemplar.resource_id,
                cooldown_group=exemplar.cooldown_group,
                cooldown_ms=exemplar.cooldown_ms,
                use_at_ms=max(times),
                action_kind=exemplar.action_kind,
                persistent_effect=exemplar.persistent_effect,
                earliest_use_at_ms=min(times),
            ))

    marginal = mean(row.effective_damage_delta for row in paired)
    if not math.isfinite(marginal):
        raise ValueError("paired marginal objective is not finite")
    return MeasuredCooldownPackageV1(
        package=CooldownPackageV1(
            package_id=package_id,
            uses=tuple(route_uses),
            marginal_value=marginal,
            encounter_elapsed_ms_delta_min=min(
                row.elapsed_ms_delta for row in paired
            ),
            encounter_elapsed_ms_delta_max=max(
                row.elapsed_ms_delta for row in paired
            ),
        ),
        paired_rows=tuple(paired),
        objective=objective,
        observed_use_times_by_seed=tuple(
            tuple(time_ms for _, time_ms in uses) for uses in uses_per_seed
        ),
    )


def _require_complete(
    seed: int,
    label: str,
    outcome: ScheduleReplayOutcomeV1,
) -> None:
    if not isinstance(outcome, ScheduleReplayOutcomeV1):
        raise TypeError("replay returned a non-ScheduleReplayOutcomeV1 value")
    if outcome.seed != seed:
        raise ValueError(f"{label} replay returned the wrong seed")
    if outcome.status is not ReplayStatusV1.COMPLETE:
        raise ValueError(
            f"{label} schedule is not complete for seed {seed}: "
            f"{outcome.status.value}"
        )


def _accepted_uses(
    outcome: ScheduleReplayOutcomeV1,
    by_action: Mapping[ActionRef, CooldownActionBindingV1],
) -> tuple[tuple[CooldownActionBindingV1, int], ...]:
    precombat = outcome.state.get("precombat")
    pull_time_ms = (
        precombat.get("pull_time_ms")
        if isinstance(precombat, Mapping)
        else 0
    )
    if type(pull_time_ms) is not int or pull_time_ms < 0:
        raise ValueError("candidate state has malformed pull_time_ms")
    result: list[tuple[CooldownActionBindingV1, int]] = []
    for receipt in outcome.receipts:
        if not isinstance(receipt, Mapping) or receipt.get("accepted") is not True:
            continue
        action_wire = receipt.get("action")
        if not isinstance(action_wire, Mapping):
            continue
        action = ActionRef.from_wire(action_wire)
        binding = by_action.get(action)
        if binding is None:
            continue
        time_ms = receipt.get("state_time_before_ms")
        if type(time_ms) is not int or time_ms < 0:
            raise ValueError("accepted cooldown receipt lacks state_time_before_ms")
        result.append((binding, time_ms - pull_time_ms))
    return tuple(result)


__all__ = (
    "OWN_EFFECTIVE_DAMAGE",
    "CooldownActionBindingV1",
    "MeasuredCooldownPackageV1",
    "PairedScheduleDeltaV1",
    "measure_cooldown_package_from_outcomes_v1",
    "measure_cooldown_package_v1",
)
