"""Search whole-wave schedules under alternative long-cooldown budgets.

The finite sequence search decides action order, target, queue lane, waits,
observable guards and burst timing inside one exact encounter/build cell.  This
module repeats that search under explicit resource budgets so the route-level
allocator receives measured alternatives instead of Contra's manual trigger or
Boss-HP rule.

A resource budget is an experiment constraint, not an expert-policy filter.
All ordinary simulator actions remain available.  Cat, both Contra variants
and offline histories may still rank proposals, but cannot add or remove a
resource action from the declared budget.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any, Mapping, Sequence

from .raid_cooldown_schedule_v1 import EncounterCooldownCellV1
from .sim_bridge import ActionRef, AvailableAction
from .wave_action_schedule_v1 import (
    EquipmentAction,
    ObservableCausalGuardV1,
    ScheduledActionPlan,
    SearchCellIdentity,
)
from .wave_action_sequence_search_v1 import (
    ActionGuideV1,
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    ScheduleReplayV1,
    WaveActionSequenceSearchResultV1,
    search_wave_action_sequences_v1,
)
from .wave_cooldown_package_measurement_v1 import (
    CooldownActionBindingV1,
    MeasuredCooldownPackageV1,
    measure_cooldown_package_from_outcomes_v1,
)


JSONMap = dict[str, Any]
COMPLETE = "COMPLETE_PAIRED_SEED_SCHEDULE"


def _plan_action_refs(plan: ScheduledActionPlan) -> tuple[ActionRef, ...]:
    values = list(plan.off_gcd_actions)
    if plan.queue_action is not None:
        values.append(plan.queue_action)
    if plan.gcd_action is not None:
        values.append(plan.gcd_action)
    return tuple(values)


@dataclass(frozen=True)
class CooldownResourceSearchArmV1:
    """One exact set of long-cooldown resources admitted to inner search."""

    arm_id: str
    enabled_resource_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.arm_id, str) or not self.arm_id.strip():
            raise ValueError("arm_id must be nonempty")
        if not isinstance(self.enabled_resource_ids, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.enabled_resource_ids
        ):
            raise TypeError("enabled_resource_ids must contain nonempty strings")
        canonical = tuple(sorted(set(self.enabled_resource_ids)))
        if canonical != self.enabled_resource_ids:
            raise ValueError("enabled_resource_ids must be sorted and unique")

    def to_dict(self) -> JSONMap:
        return {
            "arm_id": self.arm_id,
            "enabled_resource_ids": list(self.enabled_resource_ids),
        }


def enumerate_cooldown_resource_arms_v1(
    bindings: Sequence[CooldownActionBindingV1],
    *,
    max_resources_per_arm: int | None = None,
) -> tuple[CooldownResourceSearchArmV1, ...]:
    """Enumerate nonempty resource subsets for a bounded inner search.

    The caller may cap joint package size when a larger inventory is available;
    this is search factorization, not an action-order restriction.  The current
    executable precombat closure has three resources, for which the uncapped
    enumeration is exhaustive.
    """

    rows = tuple(bindings)
    if any(not isinstance(row, CooldownActionBindingV1) for row in rows):
        raise TypeError("bindings must contain CooldownActionBindingV1")
    resource_ids = tuple(sorted(row.resource_id for row in rows))
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("bindings must have unique resource_id values")
    actions = tuple(row.action for row in rows)
    if len(actions) != len(set(actions)):
        raise ValueError("bindings must have unique ActionRef values")
    if max_resources_per_arm is None:
        maximum = len(resource_ids)
    elif (
        isinstance(max_resources_per_arm, bool)
        or not isinstance(max_resources_per_arm, int)
        or max_resources_per_arm <= 0
    ):
        raise ValueError("max_resources_per_arm must be positive or None")
    else:
        maximum = min(max_resources_per_arm, len(resource_ids))
    result: list[CooldownResourceSearchArmV1] = []
    for size in range(1, maximum + 1):
        for values in combinations(resource_ids, size):
            result.append(CooldownResourceSearchArmV1(
                arm_id="resources__" + "__".join(values),
                enabled_resource_ids=values,
            ))
    return tuple(result)


class ResourceConstrainedScheduleReplayV1:
    """Hide disabled long-CD actions while retaining every ordinary action."""

    def __init__(
        self,
        replay: ScheduleReplayV1,
        *,
        bindings: Sequence[CooldownActionBindingV1],
        enabled_resource_ids: Sequence[str],
    ) -> None:
        if not callable(getattr(replay, "replay", None)):
            raise TypeError("replay must expose replay(seed, schedule)")
        rows = tuple(bindings)
        if any(not isinstance(row, CooldownActionBindingV1) for row in rows):
            raise TypeError("bindings must contain CooldownActionBindingV1")
        by_action = {row.action: row for row in rows}
        by_resource = {row.resource_id: row for row in rows}
        if len(by_action) != len(rows) or len(by_resource) != len(rows):
            raise ValueError("bindings must have unique action and resource identities")
        enabled = frozenset(enabled_resource_ids)
        unknown = sorted(enabled - set(by_resource))
        if unknown:
            raise ValueError(f"unknown enabled resource IDs: {unknown}")
        self._replay = replay
        self._binding_by_action = by_action
        self._enabled = enabled

    def replay(
        self, seed: int, schedule: Sequence[ScheduledActionPlan]
    ) -> ScheduleReplayOutcomeV1:
        for plan in schedule:
            for action in _plan_action_refs(plan):
                binding = self._binding_by_action.get(action)
                if (
                    binding is not None
                    and binding.resource_id not in self._enabled
                ):
                    return ScheduleReplayOutcomeV1(
                        seed=seed,
                        status=ReplayStatusV1.INVALID,
                        state={"time_ms": 0, "damage_done": 0.0},
                        invalid_reason=(
                            "RESOURCE_NOT_ENABLED: " + binding.resource_id
                        ),
                    )
        outcome = self._replay.replay(seed, schedule)
        if not isinstance(outcome, ScheduleReplayOutcomeV1):
            raise TypeError("wrapped replay returned an invalid outcome")
        if outcome.status is not ReplayStatusV1.FRONTIER:
            return outcome
        available: list[AvailableAction] = []
        for row in outcome.available_actions:
            binding = self._binding_by_action.get(row.action)
            if binding is None or binding.resource_id in self._enabled:
                available.append(row)
            else:
                # Preserve a nonempty action snapshot for WAIT-only frontiers,
                # but make the resource neither legal nor a visible ready timer.
                available.append(replace(row, legal=False, ready_in_ms=0))
        return replace(outcome, available_actions=tuple(available))


@dataclass(frozen=True)
class CooldownResourceArmResultV1:
    arm: CooldownResourceSearchArmV1
    status: str
    search: WaveActionSequenceSearchResultV1
    measurement: MeasuredCooldownPackageV1 | None = None
    reason: str | None = None

    def to_dict(self) -> JSONMap:
        return {
            "arm": self.arm.to_dict(),
            "status": self.status,
            "reason": self.reason,
            "search": self.search.to_dict(),
            "measurement": (
                self.measurement.to_dict()
                if self.measurement is not None
                else None
            ),
        }


@dataclass(frozen=True)
class WaveCooldownPackageSearchResultV1:
    cell: SearchCellIdentity
    baseline_search: WaveActionSequenceSearchResultV1
    arms: tuple[CooldownResourceArmResultV1, ...]
    planner_cell: EncounterCooldownCellV1

    def to_dict(self) -> JSONMap:
        return {
            "schema": "wave_cooldown_package_search/v1",
            "cell": self.cell.to_dict(),
            "baseline_resource_budget": [],
            "baseline_search": self.baseline_search.to_dict(),
            "arms": [row.to_dict() for row in self.arms],
            "planner_cell": {
                "encounter_id": self.planner_cell.encounter_id,
                "encounter_kind": self.planner_cell.encounter_kind,
                "raid_start_ms": self.planner_cell.raid_start_ms,
                "packages": [
                    package.to_dict() for package in self.planner_cell.packages
                ],
            },
            "contract": {
                "whole_schedule_searched_per_resource_budget": True,
                "sequence_target_wait_guard_and_burst_timing_jointly_searched": True,
                "expert_conditions_enforced": False,
                "expert_role": "PROPOSAL_ORDER_ONLY",
                "same_cell_build_resources_and_seeds": True,
                "paired_terminal_outcomes_reused_without_duplicate_replay": True,
                "persistent_effect_packages_require_route_state_replay": True,
                "nonzero_elapsed_delta_requires_calibrated_route_shift": True,
            },
        }


def search_wave_cooldown_packages_v1(
    replay: ScheduleReplayV1,
    cell: SearchCellIdentity,
    *,
    encounter_id: str,
    encounter_kind: str,
    raid_start_ms: int,
    seeds: Sequence[int],
    bindings: Sequence[CooldownActionBindingV1],
    arms: Sequence[CooldownResourceSearchArmV1] | None = None,
    max_resources_per_arm: int | None = None,
    max_steps: int,
    beam_width: int,
    action_guides: Sequence[ActionGuideV1] = (),
    equipment_actions: Sequence[EquipmentAction] = (),
    max_off_gcd_actions: int = 1,
    max_prefix_permutations: int | None = None,
    wait_ms: int = 100,
    guard_options: Sequence[ObservableCausalGuardV1] = (),
    max_expansions_per_node: int | None = None,
    replay_workers: int = 1,
    continuation_max_steps: int = 0,
) -> WaveCooldownPackageSearchResultV1:
    """Search no-burst and alternative burst budgets on identical seeds."""

    binding_rows = tuple(bindings)
    generated_arms = (
        enumerate_cooldown_resource_arms_v1(
            binding_rows,
            max_resources_per_arm=max_resources_per_arm,
        )
        if arms is None
        else tuple(arms)
    )
    if any(not isinstance(row, CooldownResourceSearchArmV1) for row in generated_arms):
        raise TypeError("arms must contain CooldownResourceSearchArmV1")
    arm_ids = [row.arm_id for row in generated_arms]
    if len(arm_ids) != len(set(arm_ids)):
        raise ValueError("arm_id values must be unique")

    search_options = {
        "seeds": tuple(seeds),
        "max_steps": max_steps,
        "beam_width": beam_width,
        "action_guides": tuple(action_guides),
        "equipment_actions": tuple(equipment_actions),
        "max_off_gcd_actions": max_off_gcd_actions,
        "max_prefix_permutations": max_prefix_permutations,
        "wait_ms": wait_ms,
        "guard_options": tuple(guard_options),
        "max_expansions_per_node": max_expansions_per_node,
        "replay_workers": replay_workers,
        "continuation_max_steps": continuation_max_steps,
    }
    baseline_replay = ResourceConstrainedScheduleReplayV1(
        replay,
        bindings=binding_rows,
        enabled_resource_ids=(),
    )
    baseline = search_wave_action_sequences_v1(
        baseline_replay, cell, **search_options
    )
    if baseline.status != COMPLETE:
        raise RuntimeError(
            "no-resource baseline did not complete; package values are unavailable"
        )

    results: list[CooldownResourceArmResultV1] = []
    packages = []
    for arm in generated_arms:
        constrained = ResourceConstrainedScheduleReplayV1(
            replay,
            bindings=binding_rows,
            enabled_resource_ids=arm.enabled_resource_ids,
        )
        searched = search_wave_action_sequences_v1(
            constrained, cell, **search_options
        )
        if searched.status != COMPLETE:
            results.append(CooldownResourceArmResultV1(
                arm=arm,
                status="INCOMPLETE_NOT_PLANNER_ELIGIBLE",
                search=searched,
                reason="inner search did not produce paired complete terminals",
            ))
            continue
        try:
            measured = measure_cooldown_package_from_outcomes_v1(
                package_id=arm.arm_id,
                baseline_outcomes=baseline.outcomes,
                candidate_outcomes=searched.outcomes,
                bindings=binding_rows,
            )
        except ValueError as error:
            results.append(CooldownResourceArmResultV1(
                arm=arm,
                status="UNSTABLE_RESOURCE_USE_NOT_PLANNER_ELIGIBLE",
                search=searched,
                reason=str(error),
            ))
            continue
        actual = tuple(use.resource_id for use in measured.package.uses)
        if not actual:
            results.append(CooldownResourceArmResultV1(
                arm=arm,
                status="NO_BOUND_RESOURCE_ACCEPTED",
                search=searched,
                measurement=measured,
                reason="best schedule did not use an enabled long-cooldown resource",
            ))
            continue
        if not set(actual).issubset(arm.enabled_resource_ids):
            raise RuntimeError("measured package escaped its resource budget")
        persistent_resources = tuple(
            use.resource_id
            for use in measured.package.uses
            if use.persistent_effect is not None
        )
        results.append(CooldownResourceArmResultV1(
            arm=arm,
            status=(
                "PAIRED_MEASURED_ROUTE_STATE_REQUIRED"
                if persistent_resources
                else "PAIRED_MEASURED_PLANNER_ELIGIBLE"
            ),
            search=searched,
            measurement=measured,
            reason=(
                "isolated-wave marginal excludes persistent effects on later "
                "encounters: " + ", ".join(persistent_resources)
                if persistent_resources
                else None
            ),
        ))
        packages.append(measured.package)

    planner = EncounterCooldownCellV1(
        encounter_id=encounter_id,
        encounter_kind=encounter_kind,
        raid_start_ms=raid_start_ms,
        packages=tuple(packages),
    )
    return WaveCooldownPackageSearchResultV1(
        cell=cell,
        baseline_search=baseline,
        arms=tuple(results),
        planner_cell=planner,
    )


__all__ = (
    "COMPLETE",
    "CooldownResourceArmResultV1",
    "CooldownResourceSearchArmV1",
    "ResourceConstrainedScheduleReplayV1",
    "WaveCooldownPackageSearchResultV1",
    "enumerate_cooldown_resource_arms_v1",
    "search_wave_cooldown_packages_v1",
)
