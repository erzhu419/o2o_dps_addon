"""Continuous held-out replay for a frozen multi-cell Upper Kara route.

The older route evaluator reloaded every wave and summed isolated terminal
damage.  That loses rage, swing clocks, queued attacks, active procs and long
cooldowns at every boundary.  This module deliberately uses one bridge load
and one live simulator session per ``(lane, seed)``.  Cell schedules are only
different views of that same stateful route.

V1 supports an immutable route equipment binding.  A searched runtime weapon
swap is rejected until the native bridge both mutates combat mechanics and
exports the resulting equipment state.  This is preferable to pretending that
the existing Python sidecar changes simulator weapons.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Callable, Mapping, Protocol, Sequence

from .sim_bridge import ActionRef, AvailableAction
from .route_timing_calibration_v1 import (
    PairedContinuousRouteTimingObservationV1,
    RouteEncounterStartV1,
    RouteTimingShiftProfileV1,
)
from .upper_kara_burst_route_train_eval_v1 import (
    FrozenBurstRoutePlanV1,
    FrozenEncounterActionTableV1,
)
from .upper_kara_wave_target_gate_v1 import RuntimeTargetGateV1
from .wave_action_schedule_v1 import ScheduledActionPlan
from .wave_action_sequence_search_v1 import (
    FURY_RESULT_BEARING_ACTION_REFS_V1,
    _WaveTargetGateTrackerV1,
    _advance_to_input,
    _execute_plan,
)
from .wave_cooldown_package_measurement_v1 import CooldownActionBindingV1


JSONMap = dict[str, Any]


class ContinuousRouteReplayV1Error(RuntimeError):
    """A route cannot be replayed as one exact live simulator session."""


class ContinuousRouteReplayStatusV1(str, Enum):
    COMPLETE = "COMPLETE"
    FRONTIER = "FRONTIER"
    INVALID = "INVALID"


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContinuousRouteReplayV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContinuousRouteReplayV1Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ContinuousRouteReplayV1Error(
            f"{label} must be finite and >= {minimum}"
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContinuousRouteReplayV1Error(f"{label} must be an object")
    return value


@dataclass(frozen=True)
class ExpectedAcceptedCooldownUseV1:
    encounter_id: str
    resource_id: str
    action: ActionRef
    cooldown_ms: int
    earliest_route_time_ms: int
    latest_route_time_ms: int

    def __post_init__(self) -> None:
        _nonempty(self.encounter_id, "encounter_id")
        _nonempty(self.resource_id, "resource_id")
        if not isinstance(self.action, ActionRef):
            raise TypeError("action must be ActionRef")
        if type(self.cooldown_ms) is not int or self.cooldown_ms <= 0:
            raise ValueError("cooldown_ms must be a positive integer")
        if type(self.earliest_route_time_ms) is not int or type(
            self.latest_route_time_ms
        ) is not int:
            raise ValueError("route cooldown-use bounds must be integers")
        if self.earliest_route_time_ms > self.latest_route_time_ms:
            raise ValueError("cooldown-use lower bound exceeds upper bound")

    def to_dict(self) -> JSONMap:
        return {
            "encounter_id": self.encounter_id,
            "resource_id": self.resource_id,
            "action": self.action.to_wire(),
            "cooldown_ms": self.cooldown_ms,
            "earliest_route_time_ms": self.earliest_route_time_ms,
            "latest_route_time_ms": self.latest_route_time_ms,
        }


@dataclass(frozen=True)
class FrozenContinuousRouteCellV1:
    encounter_id: str
    raid_start_ms: int
    pull_time_ms: int
    schedule: tuple[ScheduledActionPlan, ...]
    expected_cooldown_uses: tuple[ExpectedAcceptedCooldownUseV1, ...] = ()
    target_gate: RuntimeTargetGateV1 | None = None
    package_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.encounter_id, "encounter_id")
        if type(self.raid_start_ms) is not int or self.raid_start_ms < 0:
            raise ValueError("raid_start_ms must be a nonnegative integer")
        if type(self.pull_time_ms) is not int or self.pull_time_ms <= 0:
            raise ValueError("pull_time_ms must be a positive integer")
        if not isinstance(self.schedule, tuple) or any(
            not isinstance(row, ScheduledActionPlan) for row in self.schedule
        ):
            raise TypeError("schedule must be a tuple of ScheduledActionPlan")
        if any(row.equipment_action is not None for row in self.schedule):
            raise ContinuousRouteReplayV1Error(
                "continuous route v1 rejects runtime equipment/weapon swaps: "
                "the current native bridge does not export and mutate exact "
                "equipment combat state"
            )
        if not isinstance(self.expected_cooldown_uses, tuple) or any(
            not isinstance(row, ExpectedAcceptedCooldownUseV1)
            for row in self.expected_cooldown_uses
        ):
            raise TypeError(
                "expected_cooldown_uses must contain typed cooldown uses"
            )
        if any(
            row.encounter_id != self.encounter_id
            for row in self.expected_cooldown_uses
        ):
            raise ValueError("expected cooldown use belongs to a different cell")
        if self.target_gate is not None and not isinstance(
            self.target_gate, RuntimeTargetGateV1
        ):
            raise TypeError(
                "target_gate must implement RuntimeTargetGateV1 or be None"
            )
        if self.package_id is not None:
            _nonempty(self.package_id, "package_id")

    def root_time_ms(
        self, route_clock_zero_ms: int, *, raid_start_ms: int | None = None
    ) -> int:
        start = self.raid_start_ms if raid_start_ms is None else raid_start_ms
        return route_clock_zero_ms + start - self.pull_time_ms

    def first_live_time_ms(
        self, route_clock_zero_ms: int, *, raid_start_ms: int | None = None
    ) -> int:
        start = self.raid_start_ms if raid_start_ms is None else raid_start_ms
        pull = route_clock_zero_ms + start
        if not self.schedule:
            return pull
        root = self.root_time_ms(route_clock_zero_ms, raid_start_ms=start)
        return min(pull, *(root + row.at_or_after_ms for row in self.schedule))


@dataclass(frozen=True)
class FrozenContinuousRouteLaneV1:
    lane_id: str
    cells: tuple[FrozenContinuousRouteCellV1, ...]
    static_equipment: tuple[tuple[str, int], ...]
    talents: tuple[tuple[str, int], ...]
    training_seeds: tuple[int, ...]

    def __post_init__(self) -> None:
        _nonempty(self.lane_id, "lane_id")
        if not self.cells or any(
            not isinstance(row, FrozenContinuousRouteCellV1)
            for row in self.cells
        ):
            raise TypeError("cells must be a nonempty typed tuple")
        ids = [row.encounter_id for row in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("continuous route encounter IDs must be unique")
        starts = [row.raid_start_ms for row in self.cells]
        if starts != sorted(starts):
            raise ValueError("continuous route cells must follow raid time")
        if not self.static_equipment or any(
            not isinstance(slot, str)
            or not slot
            or isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id < 0
            for slot, item_id in self.static_equipment
        ):
            raise ValueError("static_equipment must be an explicit slot/item binding")
        if len({slot for slot, _ in self.static_equipment}) != len(
            self.static_equipment
        ):
            raise ValueError("static_equipment repeats a slot")
        if any(
            not isinstance(name, str)
            or not name
            or isinstance(rank, bool)
            or not isinstance(rank, int)
            or rank < 0
            for name, rank in self.talents
        ):
            raise ValueError("talents must be explicit name/rank pairs")
        if not self.training_seeds or any(
            isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
            for seed in self.training_seeds
        ):
            raise ValueError("training_seeds must contain nonnegative integers")
        if len(set(self.training_seeds)) != len(self.training_seeds):
            raise ValueError("training_seeds must be unique")

    @property
    def route_identity(self) -> tuple[Any, ...]:
        return (
            tuple((row.encounter_id, row.raid_start_ms) for row in self.cells),
            self.static_equipment,
            self.talents,
        )


@dataclass(frozen=True)
class RoutePlayerStateWitnessV1:
    boundary: str
    encounter_id: str | None
    time_ms: int
    environment_generation: int
    dynamic_config_digest: str
    rage_current: float
    rage_maximum: float
    stance: str
    gcd_remaining_ms: int
    mh_swing_remaining_ms: int
    mh_swing_duration_ms: int
    oh_swing_remaining_ms: int | None
    swing_queue: Mapping[str, Any]
    current_cast: Mapping[str, Any] | None
    self_auras_and_procs: tuple[Mapping[str, Any], ...]
    cooldowns: tuple[Mapping[str, Any], ...]
    static_equipment: tuple[tuple[str, int], ...]
    autoattack_active: bool
    cumulative_effective_damage: float

    def to_dict(self) -> JSONMap:
        return {
            "boundary": self.boundary,
            "encounter_id": self.encounter_id,
            "time_ms": self.time_ms,
            "environment_generation": self.environment_generation,
            "dynamic_config_digest": self.dynamic_config_digest,
            "rage_current": self.rage_current,
            "rage_maximum": self.rage_maximum,
            "stance": self.stance,
            "gcd_remaining_ms": self.gcd_remaining_ms,
            "mh_swing_remaining_ms": self.mh_swing_remaining_ms,
            "mh_swing_duration_ms": self.mh_swing_duration_ms,
            "oh_swing_remaining_ms": self.oh_swing_remaining_ms,
            "swing_queue": deepcopy(dict(self.swing_queue)),
            "current_cast": (
                None if self.current_cast is None else deepcopy(dict(self.current_cast))
            ),
            "self_auras_and_procs": [
                deepcopy(dict(row)) for row in self.self_auras_and_procs
            ],
            "cooldowns": [deepcopy(dict(row)) for row in self.cooldowns],
            "equipment_state": {
                "source": "IMMUTABLE_ROUTE_REQUEST_BINDING",
                "slots": [
                    {"slot": slot, "item_id": item_id}
                    for slot, item_id in self.static_equipment
                ],
            },
            "autoattack_active": self.autoattack_active,
            "cumulative_effective_damage": self.cumulative_effective_damage,
        }


@dataclass(frozen=True)
class AcceptedCooldownUseAuditV1:
    encounter_id: str
    resource_id: str
    action: ActionRef
    simulator_time_ms: int
    route_time_ms: int
    cooldown_duration_ms: int
    ready_in_ms_after_accept: int

    def to_dict(self) -> JSONMap:
        return {
            "encounter_id": self.encounter_id,
            "resource_id": self.resource_id,
            "action": self.action.to_wire(),
            "simulator_time_ms": self.simulator_time_ms,
            "route_time_ms": self.route_time_ms,
            "cooldown_duration_ms": self.cooldown_duration_ms,
            "ready_in_ms_after_accept": self.ready_in_ms_after_accept,
            "accepted_and_cooldown_started": True,
        }


@dataclass(frozen=True)
class ContinuousRouteCellOutcomeV1:
    encounter_id: str
    raid_start_ms: int
    package_id: str | None
    entry_state: RoutePlayerStateWitnessV1
    exit_state: RoutePlayerStateWitnessV1
    receipts: tuple[Mapping[str, Any], ...]
    accepted_cooldown_uses: tuple[AcceptedCooldownUseAuditV1, ...]

    def to_dict(self) -> JSONMap:
        return {
            "encounter_id": self.encounter_id,
            "raid_start_ms": self.raid_start_ms,
            "package_id": self.package_id,
            "entry_state": self.entry_state.to_dict(),
            "exit_state": self.exit_state.to_dict(),
            "receipts": [deepcopy(dict(row)) for row in self.receipts],
            "accepted_cooldown_uses": [
                row.to_dict() for row in self.accepted_cooldown_uses
            ],
        }


@dataclass(frozen=True)
class ContinuousRouteReplayOutcomeV1:
    seed: int
    lane_id: str
    status: ContinuousRouteReplayStatusV1
    final_state: Mapping[str, Any]
    state_witnesses: tuple[RoutePlayerStateWitnessV1, ...] = ()
    cells: tuple[ContinuousRouteCellOutcomeV1, ...] = ()
    accepted_cooldown_uses: tuple[AcceptedCooldownUseAuditV1, ...] = ()
    invalid_reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("seed must be an integer")
        _nonempty(self.lane_id, "lane_id")
        if not isinstance(self.status, ContinuousRouteReplayStatusV1):
            raise TypeError("status must be ContinuousRouteReplayStatusV1")
        if not isinstance(self.final_state, Mapping):
            raise TypeError("final_state must be a mapping")
        if self.status is ContinuousRouteReplayStatusV1.INVALID:
            if not isinstance(self.invalid_reason, str) or not self.invalid_reason:
                raise ValueError("INVALID outcome requires invalid_reason")
        elif self.invalid_reason is not None:
            raise ValueError("only INVALID outcome may carry invalid_reason")

    @property
    def effective_damage(self) -> float:
        return _effective_damage(self.final_state)

    @property
    def elapsed_ms(self) -> int:
        return _integer(self.final_state.get("time_ms"), "final time_ms")

    def compact_dict(self) -> JSONMap:
        complete = self.status is ContinuousRouteReplayStatusV1.COMPLETE
        return {
            "seed": self.seed,
            "lane_id": self.lane_id,
            "status": self.status.value,
            "effective_damage": self.effective_damage if complete else None,
            "elapsed_ms": self.elapsed_ms if complete else None,
            "invalid_reason": self.invalid_reason,
            "single_live_session": True,
            "state_witness_count": len(self.state_witnesses),
            "accepted_cooldown_uses": [
                row.to_dict() for row in self.accepted_cooldown_uses
            ],
        }


class ContinuousRouteReplayV1(Protocol):
    lane: FrozenContinuousRouteLaneV1

    def replay(self, seed: int) -> ContinuousRouteReplayOutcomeV1: ...


@dataclass(frozen=True)
class ExplicitPerSeedRouteStartResolverV1:
    """Apply one explicitly observed, allocator-safe timing perturbation.

    V1 intentionally refuses an edge-dependent profile and refuses the joint
    composition of two nonzero package profiles.  Neither case is represented
    by the current scalar route planner.  No missing seed is interpolated.
    """

    expected_route_case_id: str
    route_encounter_ids: tuple[str, ...]
    baseline_start_ms: tuple[int, ...]
    profiles: tuple[RouteTimingShiftProfileV1, ...]

    def __post_init__(self) -> None:
        _nonempty(self.expected_route_case_id, "expected_route_case_id")
        if not self.route_encounter_ids or len(self.route_encounter_ids) != len(
            set(self.route_encounter_ids)
        ):
            raise ValueError("route_encounter_ids must be nonempty and unique")
        if len(self.baseline_start_ms) != len(self.route_encounter_ids) or any(
            type(value) is not int or value < 0
            for value in self.baseline_start_ms
        ):
            raise ValueError(
                "baseline_start_ms must cover the ordered route with nonnegative integers"
            )
        if any(
            not isinstance(row, RouteTimingShiftProfileV1)
            for row in self.profiles
        ):
            raise TypeError("profiles must contain RouteTimingShiftProfileV1")
        keys = [row.package_key for row in self.profiles]
        if len(keys) != len(set(keys)):
            raise ValueError("timing profiles repeat a package key")
        if any(
            row.route_case_id != self.expected_route_case_id
            for row in self.profiles
        ):
            raise ValueError("timing profile has a different route_case_id")
        if any(
            row.route_encounter_ids != self.route_encounter_ids
            for row in self.profiles
        ):
            raise ValueError("timing profile has a different encounter route")
        edge_dependent = [
            row.package_key
            for row in self.profiles
            if not row.is_persistent_per_seed
        ]
        if edge_dependent:
            raise ValueError(
                "edge-dependent route timing cannot drive continuous replay v1: "
                + repr(edge_dependent)
            )
        nonzero = [row for row in self.profiles if not row.is_exact_zero]
        if len(nonzero) > 1:
            raise ValueError(
                "independent package profiles do not prove multi-package timing "
                "composition: " + repr([row.package_key for row in nonzero])
            )
        seed_panels = {row.seeds for row in self.profiles}
        if len(seed_panels) > 1:
            raise ValueError("timing profiles use different paired seed panels")

    @property
    def seeds(self) -> tuple[int, ...]:
        return self.profiles[0].seeds if self.profiles else ()

    def __call__(self, seed: int, cell: FrozenContinuousRouteCellV1) -> int:
        try:
            route_index = self.route_encounter_ids.index(cell.encounter_id)
        except ValueError as error:
            raise ContinuousRouteReplayV1Error(
                "timing resolver received an unknown route cell"
            ) from error
        if not self.profiles:
            return self.baseline_start_ms[route_index]
        if seed not in self.seeds:
            raise ContinuousRouteReplayV1Error(
                f"timing profile has no explicit paired shift for seed {seed}"
            )
        shift = 0
        for profile in self.profiles:
            if profile.is_exact_zero:
                continue
            source_index = profile.route_encounter_ids.index(
                profile.source_encounter_id
            )
            if route_index <= source_index:
                continue
            # Persistent-per-seed validation above makes every downstream row
            # identical for this seed; use the exact current-cell row anyway.
            downstream = next(
                row for row in profile.downstream
                if row.route_index == route_index
            )
            shift = dict(downstream.seed_shifts_ms)[seed]
        resolved = self.baseline_start_ms[route_index] + shift
        if resolved < 0:
            raise ContinuousRouteReplayV1Error(
                "explicit calibrated start shift moves a cell before route zero"
            )
        return resolved


def build_explicit_route_start_resolver_v1(
    plan: FrozenBurstRoutePlanV1,
    lane: FrozenContinuousRouteLaneV1,
    *,
    expected_route_case_id: str,
    profiles: Sequence[RouteTimingShiftProfileV1],
) -> ExplicitPerSeedRouteStartResolverV1:
    """Bind explicit profile seeds to the exact selected package route."""

    if not isinstance(plan, FrozenBurstRoutePlanV1):
        raise TypeError("plan must be FrozenBurstRoutePlanV1")
    if not isinstance(lane, FrozenContinuousRouteLaneV1):
        raise TypeError("lane must be FrozenContinuousRouteLaneV1")
    rows = tuple(profiles)
    assigned = {
        (row.encounter_id, row.package.package_id): row
        for row in plan.planner_schedule.assignments
    }
    unknown = [row.package_key for row in rows if row.package_key not in assigned]
    if unknown:
        raise ValueError(
            "timing profile does not describe a selected route package: "
            + repr(unknown)
        )
    for profile in rows:
        assignment = assigned[profile.package_key]
        measured_bounds = profile.persistent_shift_bounds()
        allocated_increment = (
            assignment.downstream_route_shift_ms_min
            - assignment.route_start_shift_ms_min,
            assignment.downstream_route_shift_ms_max
            - assignment.route_start_shift_ms_max,
        )
        if measured_bounds != allocated_increment:
            raise ValueError(
                "explicit timing profile differs from the shift increment used "
                "by the frozen route allocator for "
                f"{profile.package_key!r}: profile={measured_bounds!r}, "
                f"allocator={allocated_increment!r}"
            )
    supplied = {row.package_key for row in rows}
    route_ids = tuple(row.encounter_id for row in lane.cells)
    plan_ids = tuple(row.encounter_id for row in plan.encounters)
    if route_ids != plan_ids:
        raise ValueError("continuous lane differs from frozen plan encounter order")
    missing = [
        key
        for key in assigned
        if route_ids.index(key[0]) < len(route_ids) - 1
        and key not in supplied
    ]
    if missing:
        raise ValueError(
            "selected package with a downstream route edge lacks an explicit "
            "paired timing profile (zero shifts must also be observed): "
            + repr(missing)
        )
    return ExplicitPerSeedRouteStartResolverV1(
        expected_route_case_id=expected_route_case_id,
        route_encounter_ids=route_ids,
        baseline_start_ms=tuple(row.raid_start_ms for row in lane.cells),
        profiles=rows,
    )


def build_frozen_burst_route_lane_v1(
    plan: FrozenBurstRoutePlanV1,
    *,
    lane_id: str,
    lane: str,
    cooldown_bindings: Sequence[CooldownActionBindingV1],
    target_gates_by_encounter: Mapping[str, RuntimeTargetGateV1] | None = None,
) -> FrozenContinuousRouteLaneV1:
    """Bind selected/baseline tables to exact planner cooldown expectations."""

    if not isinstance(plan, FrozenBurstRoutePlanV1):
        raise TypeError("plan must be FrozenBurstRoutePlanV1")
    if lane not in {"candidate", "baseline"}:
        raise ValueError("lane must be candidate or baseline")
    bindings = tuple(cooldown_bindings)
    if any(not isinstance(row, CooldownActionBindingV1) for row in bindings):
        raise TypeError("cooldown_bindings must contain CooldownActionBindingV1")
    by_resource = {row.resource_id: row for row in bindings}
    if len(by_resource) != len(bindings):
        raise ValueError("continuous route v1 requires one action per resource_id")
    tables = plan.encounters if lane == "candidate" else plan.baselines
    if not tables:
        raise ValueError("frozen route has no action tables")
    assignments = {
        row.encounter_id: row for row in plan.planner_schedule.assignments
    }
    gates = dict(target_gates_by_encounter or {})
    unknown_gates = set(gates) - {row.encounter_id for row in tables}
    if unknown_gates:
        raise ValueError(f"target gates name unknown encounters: {sorted(unknown_gates)}")

    equipment = tables[0].starting_equipment
    talents = tables[0].talents
    if any(row.starting_equipment != equipment for row in tables):
        raise ContinuousRouteReplayV1Error(
            "continuous route tables disagree on immutable starting equipment"
        )
    if any(row.talents != talents for row in tables):
        raise ContinuousRouteReplayV1Error(
            "continuous route tables disagree on frozen talents"
        )

    cells: list[FrozenContinuousRouteCellV1] = []
    training: set[int] = set()
    for table in tables:
        if not isinstance(table, FrozenEncounterActionTableV1):
            raise TypeError("route table has the wrong type")
        training.update(table.training_seeds)
        expected: list[ExpectedAcceptedCooldownUseV1] = []
        if lane == "candidate":
            assignment = assignments.get(table.encounter_id)
            if table.namespaced_package_id is None:
                if assignment is not None:
                    raise ContinuousRouteReplayV1Error(
                        "planner assignment has no selected candidate action table"
                    )
            else:
                if (
                    assignment is None
                    or assignment.package.package_id != table.namespaced_package_id
                ):
                    raise ContinuousRouteReplayV1Error(
                        "selected action table differs from planner assignment"
                    )
                for use in assignment.package.uses:
                    binding = by_resource.get(use.resource_id)
                    if binding is None:
                        raise ContinuousRouteReplayV1Error(
                            f"no simulator action binds cooldown resource {use.resource_id!r}"
                        )
                    if binding.cooldown_ms != use.cooldown_ms:
                        raise ContinuousRouteReplayV1Error(
                            "planner and simulator cooldown durations differ for "
                            f"{use.resource_id!r}"
                        )
                    expected.append(ExpectedAcceptedCooldownUseV1(
                        encounter_id=table.encounter_id,
                        resource_id=use.resource_id,
                        action=binding.action,
                        cooldown_ms=use.cooldown_ms,
                        earliest_route_time_ms=(
                            assignment.raid_start_ms
                            + assignment.route_start_shift_ms_min
                            + use.use_window_start_ms
                        ),
                        latest_route_time_ms=(
                            assignment.raid_start_ms
                            + assignment.route_start_shift_ms_max
                            + use.use_at_ms
                        ),
                    ))
        cells.append(FrozenContinuousRouteCellV1(
            encounter_id=table.encounter_id,
            raid_start_ms=table.raid_start_ms,
            pull_time_ms=table.pull_time_ms,
            schedule=table.schedule,
            expected_cooldown_uses=tuple(expected),
            target_gate=gates.get(table.encounter_id),
            package_id=(
                table.namespaced_package_id if lane == "candidate" else None
            ),
        ))
    return FrozenContinuousRouteLaneV1(
        lane_id=lane_id,
        cells=tuple(cells),
        static_equipment=equipment,
        talents=talents,
        training_seeds=tuple(sorted(training)),
    )


class _CooldownAuditedBridgeV1:
    def __init__(
        self,
        bridge: Any,
        bindings: Sequence[CooldownActionBindingV1],
        *,
        route_clock_zero_ms: int,
    ) -> None:
        self._bridge = bridge
        self._by_action = {row.action: row for row in bindings}
        if len(self._by_action) != len(tuple(bindings)):
            raise ValueError("cooldown bindings repeat an ActionRef")
        self._route_clock_zero_ms = route_clock_zero_ms
        self.current_encounter_id: str | None = None
        self.accepted: list[AcceptedCooldownUseAuditV1] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def act(self, action: ActionRef, *, attempt_id: str | None = None) -> Any:
        result = self._bridge.act(action, attempt_id=attempt_id)
        binding = self._by_action.get(action)
        if not bool(getattr(result, "casted", False)) or binding is None:
            return result
        if self.current_encounter_id is None:
            raise ContinuousRouteReplayV1Error(
                "a tracked cooldown was accepted outside a route cell"
            )
        after = {row.action: row for row in self._bridge.actions()}
        observed = after.get(action)
        if observed is None:
            raise ContinuousRouteReplayV1Error(
                "accepted cooldown action disappeared from the native spellbook"
            )
        if observed.cooldown_duration_ms != binding.cooldown_ms:
            raise ContinuousRouteReplayV1Error(
                f"accepted cooldown {binding.resource_id!r} reports duration "
                f"{observed.cooldown_duration_ms}, expected {binding.cooldown_ms}"
            )
        if observed.ready_in_ms != binding.cooldown_ms:
            raise ContinuousRouteReplayV1Error(
                f"accepted cooldown {binding.resource_id!r} did not start its "
                f"full native clock: ready_in_ms={observed.ready_in_ms}, "
                f"expected={binding.cooldown_ms}"
            )
        state = _mapping(getattr(result, "state", None), "act result state")
        time_ms = _integer(state.get("time_ms"), "accepted cooldown time_ms")
        self.accepted.append(AcceptedCooldownUseAuditV1(
            encounter_id=self.current_encounter_id,
            resource_id=binding.resource_id,
            action=action,
            simulator_time_ms=time_ms,
            route_time_ms=time_ms - self._route_clock_zero_ms,
            cooldown_duration_ms=observed.cooldown_duration_ms,
            ready_in_ms_after_accept=observed.ready_in_ms,
        ))
        return result


class NativeContinuousRouteReplayV1:
    """Execute all frozen cells after exactly one caller-supplied route load.

    ``load_route`` may call a native dynamic-v4 load directly or may use
    ``ResponsiveTeamDrivenBridgeV1.load_dynamic_v4``.  Its return value must be
    either a state mapping or an object with a ``state`` mapping.
    """

    def __init__(
        self,
        bridge_factory: Callable[[], Any],
        load_route: Callable[[Any, int], Any],
        lane: FrozenContinuousRouteLaneV1,
        *,
        cooldown_bindings: Sequence[CooldownActionBindingV1],
        route_clock_zero_ms: int,
        cell_start_resolver: Callable[
            [int, FrozenContinuousRouteCellV1], int
        ] | None = None,
        result_bearing_action_refs: Sequence[ActionRef] = tuple(
            FURY_RESULT_BEARING_ACTION_REFS_V1
        ),
    ) -> None:
        if not callable(bridge_factory) or not callable(load_route):
            raise TypeError("bridge_factory and load_route must be callable")
        if not isinstance(lane, FrozenContinuousRouteLaneV1):
            raise TypeError("lane must be FrozenContinuousRouteLaneV1")
        bindings = tuple(cooldown_bindings)
        if any(not isinstance(row, CooldownActionBindingV1) for row in bindings):
            raise TypeError("cooldown_bindings must contain typed bindings")
        if len({row.action for row in bindings}) != len(bindings):
            raise ValueError("cooldown bindings repeat an ActionRef")
        resources = {row.resource_id for row in bindings}
        expected_resources = {
            use.resource_id for cell in lane.cells for use in cell.expected_cooldown_uses
        }
        if not expected_resources <= resources:
            raise ValueError("expected route cooldown lacks an audit binding")
        if type(route_clock_zero_ms) is not int or route_clock_zero_ms < 0:
            raise ValueError("route_clock_zero_ms must be a nonnegative integer")
        if cell_start_resolver is not None and not callable(cell_start_resolver):
            raise TypeError("cell_start_resolver must be callable or None")
        action_refs = tuple(result_bearing_action_refs)
        if any(not isinstance(row, ActionRef) for row in action_refs):
            raise TypeError("result_bearing_action_refs must contain ActionRef")
        if len(set(action_refs)) != len(action_refs):
            raise ValueError("result_bearing_action_refs must be unique")
        for cell in lane.cells:
            if cell.first_live_time_ms(route_clock_zero_ms) < 0:
                raise ValueError(
                    "route_clock_zero_ms does not cover first-cell precombat time"
                )
        self._bridge_factory = bridge_factory
        self._load_route = load_route
        self.lane = lane
        self._bindings = bindings
        self._route_clock_zero_ms = route_clock_zero_ms
        self._cell_start_resolver = cell_start_resolver
        self._result_bearing = frozenset(action_refs)

    def replay(self, seed: int) -> ContinuousRouteReplayOutcomeV1:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        witnesses: list[RoutePlayerStateWitnessV1] = []
        cell_outcomes: list[ContinuousRouteCellOutcomeV1] = []
        accepted: tuple[AcceptedCooldownUseAuditV1, ...] = ()
        last_state: Mapping[str, Any] = {"time_ms": 0, "damage_done": 0.0}
        audit_bridge: _CooldownAuditedBridgeV1 | None = None
        try:
            with self._bridge_factory() as raw_bridge:
                loaded = self._load_route(raw_bridge, seed)
                raw_state = loaded if isinstance(loaded, Mapping) else getattr(
                    loaded, "state", None
                )
                state = dict(_mapping(raw_state, "continuous route load state"))
                bridge = _CooldownAuditedBridgeV1(
                    raw_bridge,
                    self._bindings,
                    route_clock_zero_ms=self._route_clock_zero_ms,
                )
                audit_bridge = bridge
                state = _advance_to_input(bridge, state)
                last_state = state
                initial = _capture_state_witness_v1(
                    bridge,
                    state,
                    boundary="ROUTE_LOADED",
                    encounter_id=None,
                    static_equipment=self.lane.static_equipment,
                )
                witnesses.append(initial)
                initial_cooldowns = {
                    ActionRef.from_wire(row["action"]): row["ready_in_ms"]
                    for row in initial.cooldowns
                }
                for binding in self._bindings:
                    if binding.action not in initial_cooldowns:
                        raise ContinuousRouteReplayV1Error(
                            f"tracked cooldown {binding.resource_id!r} is absent "
                            "from the native cooldown spellbook"
                        )
                    if initial_cooldowns[binding.action] != 0:
                        raise ContinuousRouteReplayV1Error(
                            f"route load starts with tracked cooldown "
                            f"{binding.resource_id!r} already active; hidden prefix "
                            "uses are unsupported in v1"
                        )

                route_step_index = 0
                previous_last_scheduled_ms: int | None = None
                for cell in self.lane.cells:
                    raid_start_ms = (
                        cell.raid_start_ms
                        if self._cell_start_resolver is None
                        else self._cell_start_resolver(seed, cell)
                    )
                    if type(raid_start_ms) is not int or raid_start_ms < 0:
                        raise ContinuousRouteReplayV1Error(
                            "cell start resolver must return a nonnegative integer"
                        )
                    entry_time = cell.first_live_time_ms(
                        self._route_clock_zero_ms,
                        raid_start_ms=raid_start_ms,
                    )
                    if (
                        previous_last_scheduled_ms is not None
                        and entry_time < previous_last_scheduled_ms
                    ):
                        raise ContinuousRouteReplayV1Error(
                            "cell schedules overlap on the continuous route clock"
                        )
                    state = _wait_until_absolute_v1(bridge, state, entry_time)
                    if bool(state.get("finished")):
                        raise ContinuousRouteReplayV1Error(
                            f"route finished before cell {cell.encounter_id!r}"
                        )
                    entry = _capture_state_witness_v1(
                        bridge,
                        state,
                        boundary="CELL_ENTRY",
                        encounter_id=cell.encounter_id,
                        static_equipment=self.lane.static_equipment,
                    )
                    witnesses.append(entry)
                    tracker = (
                        _WaveTargetGateTrackerV1(cell.target_gate)
                        if cell.target_gate is not None
                        else None
                    )
                    if tracker is not None:
                        tracker.observe(state)
                    receipts: list[JSONMap] = []
                    accepted_start = len(bridge.accepted)
                    bridge.current_encounter_id = cell.encounter_id
                    root_time = cell.root_time_ms(
                        self._route_clock_zero_ms,
                        raid_start_ms=raid_start_ms,
                    )
                    for plan in cell.schedule:
                        if bool(state.get("finished")):
                            break
                        state = _execute_plan(
                            bridge,
                            state,
                            plan,
                            root_time_ms=root_time,
                            step_index=route_step_index,
                            receipts=receipts,
                            result_bearing_action_refs=self._result_bearing,
                            target_gate_tracker=tracker,
                        )
                        route_step_index += 1
                        state = _advance_to_input(bridge, state)
                        if tracker is not None:
                            tracker.observe(state)
                    bridge.current_encounter_id = None
                    last_state = state
                    exit_state = _capture_state_witness_v1(
                        bridge,
                        state,
                        boundary="CELL_EXIT",
                        encounter_id=cell.encounter_id,
                        static_equipment=self.lane.static_equipment,
                    )
                    witnesses.append(exit_state)
                    cell_accepted = tuple(bridge.accepted[accepted_start:])
                    _verify_expected_cooldown_uses_v1(
                        cell.expected_cooldown_uses, cell_accepted
                    )
                    cell_outcomes.append(ContinuousRouteCellOutcomeV1(
                        encounter_id=cell.encounter_id,
                        raid_start_ms=raid_start_ms,
                        package_id=cell.package_id,
                        entry_state=entry,
                        exit_state=exit_state,
                        receipts=tuple(receipts),
                        accepted_cooldown_uses=cell_accepted,
                    ))
                    scheduled = [
                        root_time + row.at_or_after_ms for row in cell.schedule
                    ]
                    previous_last_scheduled_ms = max(
                        [entry_time, *scheduled]
                    )
                accepted = tuple(bridge.accepted)
                expected_all = tuple(
                    use
                    for cell in self.lane.cells
                    for use in cell.expected_cooldown_uses
                )
                _verify_expected_cooldown_uses_v1(expected_all, accepted)
                final = _capture_state_witness_v1(
                    bridge,
                    state,
                    boundary="ROUTE_FINAL",
                    encounter_id=None,
                    static_equipment=self.lane.static_equipment,
                )
                witnesses.append(final)
                _validate_witness_chain_v1(witnesses)
                status = (
                    ContinuousRouteReplayStatusV1.COMPLETE
                    if bool(state.get("finished"))
                    else ContinuousRouteReplayStatusV1.FRONTIER
                )
                return ContinuousRouteReplayOutcomeV1(
                    seed=seed,
                    lane_id=self.lane.lane_id,
                    status=status,
                    final_state=deepcopy(dict(state)),
                    state_witnesses=tuple(witnesses),
                    cells=tuple(cell_outcomes),
                    accepted_cooldown_uses=accepted,
                )
        except Exception as error:
            if audit_bridge is not None:
                accepted = tuple(audit_bridge.accepted)
            return ContinuousRouteReplayOutcomeV1(
                seed=seed,
                lane_id=self.lane.lane_id,
                status=ContinuousRouteReplayStatusV1.INVALID,
                final_state=deepcopy(dict(last_state)),
                state_witnesses=tuple(witnesses),
                cells=tuple(cell_outcomes),
                accepted_cooldown_uses=accepted,
                invalid_reason=f"{type(error).__name__}: {error}",
            )


def _wait_until_absolute_v1(
    bridge: Any, state: Mapping[str, Any], target_time_ms: int
) -> JSONMap:
    current = dict(state)
    loops = 0
    while True:
        now = _integer(current.get("time_ms"), "route state time_ms")
        if now == target_time_ms:
            return current
        if now > target_time_ms:
            raise ContinuousRouteReplayV1Error(
                f"continuous route overshot cell entry {target_time_ms} at {now}"
            )
        if bool(current.get("finished")):
            return current
        if not bool(current.get("needs_input")):
            current = _advance_to_input(bridge, current)
        else:
            current = dict(bridge.wait(target_time_ms - now))
            current = _advance_to_input(bridge, current)
        loops += 1
        if loops > 100_000:
            raise ContinuousRouteReplayV1Error(
                "cell-entry wait exceeded 100000 simulator transitions"
            )


def _effective_damage(state: Mapping[str, Any]) -> float:
    lifecycle = state.get("dynamic_team_background")
    raw = (
        lifecycle.get("simulated_damage_applied")
        if isinstance(lifecycle, Mapping)
        else state.get("damage_done")
    )
    return _number(raw, "cumulative candidate effective damage")


def _capture_state_witness_v1(
    bridge: Any,
    state: Mapping[str, Any],
    *,
    boundary: str,
    encounter_id: str | None,
    static_equipment: tuple[tuple[str, int], ...],
) -> RoutePlayerStateWitnessV1:
    now = _integer(state.get("time_ms"), "state.time_ms")
    power = _mapping(state.get("power"), "state.power")
    if power.get("type") != "rage":
        raise ContinuousRouteReplayV1Error("continuous Fury route requires rage")
    rage = _number(power.get("current"), "power.current")
    maximum = _number(power.get("maximum"), "power.maximum")
    if maximum <= 0 or rage > maximum:
        raise ContinuousRouteReplayV1Error("rage state is outside its maximum")
    stance = state.get("stance")
    if stance not in {"BATTLE", "DEFENSIVE", "BERSERKER"}:
        raise ContinuousRouteReplayV1Error("state lacks an exact Warrior stance")
    gcd = _integer(state.get("gcd_remaining_ms"), "gcd_remaining_ms")
    mh = _integer(state.get("mh_swing_remaining_ms"), "mh_swing_remaining_ms")
    mh_duration = _integer(
        state.get("mh_swing_duration_ms"), "mh_swing_duration_ms", minimum=1
    )
    if "oh_swing_remaining_ms" not in state:
        raise ContinuousRouteReplayV1Error(
            "state omits off-hand swing presence/absence"
        )
    oh_raw = state.get("oh_swing_remaining_ms")
    oh = None if oh_raw is None else _integer(oh_raw, "oh_swing_remaining_ms")
    queue = _mapping(state.get("swing_queue"), "state.swing_queue")
    if not isinstance(queue.get("kind"), str) or not isinstance(
        queue.get("status"), str
    ):
        raise ContinuousRouteReplayV1Error(
            "swing_queue lacks exact kind/status"
        )
    if not isinstance(state.get("autoattack_active"), bool):
        raise ContinuousRouteReplayV1Error("autoattack_active must be boolean")

    raw_auras = state.get("auras")
    if not isinstance(raw_auras, list):
        raise ContinuousRouteReplayV1Error("state.auras must be an explicit list")
    auras: list[Mapping[str, Any]] = []
    for index, raw in enumerate(raw_auras):
        aura = _mapping(raw, f"auras[{index}]")
        if not isinstance(aura.get("label"), str):
            raise ContinuousRouteReplayV1Error("aura label must be text")
        ActionRef.from_wire(_mapping(aura.get("action"), "aura action"))
        _integer(aura.get("stacks"), "aura stacks")
        # The native bridge uses -1 for NeverExpires (not a missing value).
        _integer(aura.get("remaining_ms"), "aura remaining_ms", minimum=-1)
        auras.append(deepcopy(dict(aura)))

    cast_raw = state.get("current_cast")
    current_cast: Mapping[str, Any] | None
    if cast_raw is None:
        current_cast = None
    else:
        cast = _mapping(cast_raw, "current_cast")
        ActionRef.from_wire(_mapping(cast.get("action"), "current_cast action"))
        _integer(cast.get("remaining_ms"), "current_cast remaining_ms")
        _integer(cast.get("duration_ms"), "current_cast duration_ms")
        current_cast = deepcopy(dict(cast))

    team = _mapping(
        state.get("dynamic_team_background"), "dynamic_team_background"
    )
    semantics = _mapping(
        state.get("dynamic_target_semantics"), "dynamic_target_semantics"
    )
    generation = _integer(
        team.get("environment_generation"), "environment_generation", minimum=1
    )
    if semantics.get("environment_generation") != generation:
        raise ContinuousRouteReplayV1Error(
            "dynamic state blocks disagree on environment generation"
        )
    digest = team.get("config_digest")
    if not isinstance(digest, str) or not digest:
        raise ContinuousRouteReplayV1Error("dynamic config digest is absent")
    if semantics.get("config_digest") != digest:
        raise ContinuousRouteReplayV1Error(
            "dynamic state blocks disagree on config digest"
        )

    available = bridge.actions()
    if not isinstance(available, list) or any(
        not isinstance(row, AvailableAction) for row in available
    ):
        raise ContinuousRouteReplayV1Error(
            "bridge.actions must return AvailableAction rows"
        )
    if len({row.action for row in available}) != len(available):
        raise ContinuousRouteReplayV1Error("native spellbook repeats an ActionRef")
    cooldowns = tuple(
        {
            "action": row.action.to_wire(),
            "label": row.label,
            "ready_in_ms": row.ready_in_ms,
            "cooldown_duration_ms": row.cooldown_duration_ms,
        }
        for row in sorted(
            (
                row
                for row in available
                if row.cooldown_duration_ms > 0 or row.ready_in_ms > 0
            ),
            key=lambda row: (
                row.action.spell_id,
                row.action.item_id,
                row.action.other_id,
                row.action.tag,
            ),
        )
    )
    return RoutePlayerStateWitnessV1(
        boundary=boundary,
        encounter_id=encounter_id,
        time_ms=now,
        environment_generation=generation,
        dynamic_config_digest=digest,
        rage_current=rage,
        rage_maximum=maximum,
        stance=stance,
        gcd_remaining_ms=gcd,
        mh_swing_remaining_ms=mh,
        mh_swing_duration_ms=mh_duration,
        oh_swing_remaining_ms=oh,
        swing_queue=deepcopy(dict(queue)),
        current_cast=current_cast,
        self_auras_and_procs=tuple(auras),
        cooldowns=cooldowns,
        static_equipment=static_equipment,
        autoattack_active=bool(state["autoattack_active"]),
        cumulative_effective_damage=_effective_damage(state),
    )


def _verify_expected_cooldown_uses_v1(
    expected: Sequence[ExpectedAcceptedCooldownUseV1],
    actual: Sequence[AcceptedCooldownUseAuditV1],
) -> None:
    expected_rows = tuple(expected)
    actual_rows = tuple(actual)
    expected_identity = tuple(
        (row.encounter_id, row.resource_id, row.action) for row in expected_rows
    )
    actual_identity = tuple(
        (row.encounter_id, row.resource_id, row.action) for row in actual_rows
    )
    if actual_identity != expected_identity:
        raise ContinuousRouteReplayV1Error(
            "accepted route cooldown uses differ from the frozen planner: "
            f"expected {expected_identity!r}, observed {actual_identity!r}"
        )
    for planned, observed in zip(expected_rows, actual_rows, strict=True):
        if not (
            planned.earliest_route_time_ms
            <= observed.route_time_ms
            <= planned.latest_route_time_ms
        ):
            raise ContinuousRouteReplayV1Error(
                f"accepted cooldown {planned.resource_id!r} at route time "
                f"{observed.route_time_ms} lies outside frozen window "
                f"[{planned.earliest_route_time_ms}, "
                f"{planned.latest_route_time_ms}]"
            )


def _validate_witness_chain_v1(
    witnesses: Sequence[RoutePlayerStateWitnessV1],
) -> None:
    rows = tuple(witnesses)
    if not rows:
        raise ContinuousRouteReplayV1Error("route produced no state witnesses")
    generation = rows[0].environment_generation
    digest = rows[0].dynamic_config_digest
    equipment = rows[0].static_equipment
    previous_time = -1
    for row in rows:
        if row.time_ms < previous_time:
            raise ContinuousRouteReplayV1Error("route state time moved backwards")
        if (
            row.environment_generation != generation
            or row.dynamic_config_digest != digest
        ):
            raise ContinuousRouteReplayV1Error(
                "route cell boundary crossed an environment reload"
            )
        if row.static_equipment != equipment:
            raise ContinuousRouteReplayV1Error(
                "immutable route equipment changed across cells"
            )
        previous_time = row.time_ms


def _seed_panel(values: Sequence[int], label: str) -> tuple[int, ...]:
    rows = tuple(values)
    if not rows or any(
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        for seed in rows
    ):
        raise ValueError(f"{label} must contain nonnegative integers")
    if len(set(rows)) != len(rows):
        raise ValueError(f"{label} must be unique")
    return rows


def paired_route_timing_observation_from_outcomes_v1(
    baseline: ContinuousRouteReplayOutcomeV1,
    candidate: ContinuousRouteReplayOutcomeV1,
    *,
    route_case_id: str,
    source_encounter_id: str,
    package_id: str,
) -> PairedContinuousRouteTimingObservationV1:
    """Extract one paired timing row from two complete continuous replays.

    ``raid_start_ms`` is the cell's resolved encounter-start clock.  It is used
    instead of the precombat execution-entry witness because a package may be
    accepted before pull without changing the pull's causal prefix.
    """

    if not isinstance(baseline, ContinuousRouteReplayOutcomeV1) or not isinstance(
        candidate, ContinuousRouteReplayOutcomeV1
    ):
        raise TypeError("baseline and candidate must be continuous outcomes")
    _nonempty(route_case_id, "route_case_id")
    _nonempty(source_encounter_id, "source_encounter_id")
    _nonempty(package_id, "package_id")
    if baseline.seed != candidate.seed:
        raise ValueError("paired route timing outcomes use different seeds")
    if (
        baseline.status is not ContinuousRouteReplayStatusV1.COMPLETE
        or candidate.status is not ContinuousRouteReplayStatusV1.COMPLETE
    ):
        raise ValueError("paired route timing requires two complete routes")
    baseline_ids = tuple(row.encounter_id for row in baseline.cells)
    candidate_ids = tuple(row.encounter_id for row in candidate.cells)
    if not baseline_ids or candidate_ids != baseline_ids:
        raise ValueError(
            "paired route timing outcomes must contain the identical ordered route"
        )
    if source_encounter_id not in baseline_ids:
        raise ValueError("source_encounter_id is absent from route outcomes")
    _validate_witness_chain_v1(baseline.state_witnesses)
    _validate_witness_chain_v1(candidate.state_witnesses)
    baseline_digest = baseline.state_witnesses[0].dynamic_config_digest
    candidate_digest = candidate.state_witnesses[0].dynamic_config_digest
    if baseline_digest != candidate_digest:
        raise ValueError(
            "paired route timing outcomes use different frozen route configs"
        )
    baseline_source = next(
        row for row in baseline.cells if row.encounter_id == source_encounter_id
    )
    candidate_source = next(
        row for row in candidate.cells if row.encounter_id == source_encounter_id
    )
    if baseline_source.package_id is not None:
        raise ValueError("timing baseline unexpectedly carries a selected package")
    if candidate_source.package_id != package_id:
        raise ValueError(
            "candidate source cell does not bind the declared package_id"
        )
    if not candidate_source.accepted_cooldown_uses:
        raise ValueError(
            "declared timing package has no actually accepted cooldown use"
        )
    if any(
        row.encounter_id != source_encounter_id
        for row in candidate_source.accepted_cooldown_uses
    ):
        raise ValueError(
            "candidate source cell contains a cooldown accepted in another cell"
        )
    if tuple(candidate_source.accepted_cooldown_uses) != tuple(
        row
        for row in candidate.accepted_cooldown_uses
        if row.encounter_id == source_encounter_id
    ):
        raise ValueError(
            "candidate source cooldown evidence differs from the full route audit"
        )
    return PairedContinuousRouteTimingObservationV1(
        route_case_id=route_case_id,
        source_encounter_id=source_encounter_id,
        package_id=package_id,
        seed=baseline.seed,
        baseline_starts=tuple(
            RouteEncounterStartV1(row.encounter_id, row.raid_start_ms)
            for row in baseline.cells
        ),
        candidate_starts=tuple(
            RouteEncounterStartV1(row.encounter_id, row.raid_start_ms)
            for row in candidate.cells
        ),
    )


def evaluate_continuous_held_out_route_v1(
    candidate_replay: ContinuousRouteReplayV1,
    baseline_replay: ContinuousRouteReplayV1,
    *,
    evaluation_seeds: Sequence[int],
) -> JSONMap:
    """Paired held-out evaluation using final cumulative route damage only."""

    if not hasattr(candidate_replay, "lane") or not hasattr(
        baseline_replay, "lane"
    ):
        raise TypeError("continuous replay objects must expose their frozen lane")
    candidate_lane = candidate_replay.lane
    baseline_lane = baseline_replay.lane
    if not isinstance(candidate_lane, FrozenContinuousRouteLaneV1) or not isinstance(
        baseline_lane, FrozenContinuousRouteLaneV1
    ):
        raise TypeError("replay lane has the wrong type")
    if candidate_lane.route_identity != baseline_lane.route_identity:
        raise ValueError("candidate and baseline continuous route identities differ")
    seeds = _seed_panel(evaluation_seeds, "evaluation_seeds")
    training = set(candidate_lane.training_seeds) | set(
        baseline_lane.training_seeds
    )
    overlap = sorted(training & set(seeds))
    if overlap:
        raise ValueError(f"training and evaluation seeds overlap: {overlap}")

    rows: list[JSONMap] = []
    deltas: list[float] = []
    for seed in seeds:
        candidate = candidate_replay.replay(seed)
        baseline = baseline_replay.replay(seed)
        if not isinstance(candidate, ContinuousRouteReplayOutcomeV1) or not isinstance(
            baseline, ContinuousRouteReplayOutcomeV1
        ):
            raise TypeError("continuous replay returned the wrong outcome type")
        if candidate.seed != seed or baseline.seed != seed:
            raise ValueError("continuous replay returned a different seed")
        if candidate.status is ContinuousRouteReplayStatusV1.COMPLETE:
            _validate_complete_lane_outcome_v1(candidate_lane, candidate)
        if baseline.status is ContinuousRouteReplayStatusV1.COMPLETE:
            _validate_complete_lane_outcome_v1(baseline_lane, baseline)
        complete = (
            candidate.status is ContinuousRouteReplayStatusV1.COMPLETE
            and baseline.status is ContinuousRouteReplayStatusV1.COMPLETE
        )
        if complete and (
            candidate.state_witnesses[0].dynamic_config_digest
            != baseline.state_witnesses[0].dynamic_config_digest
        ):
            raise ContinuousRouteReplayV1Error(
                "paired continuous lanes use different dynamic environment configs"
            )
        delta = (
            candidate.effective_damage - baseline.effective_damage
            if complete
            else None
        )
        if delta is not None:
            deltas.append(delta)
        rows.append({
            "seed": seed,
            "paired_complete": complete,
            "candidate": candidate.compact_dict(),
            "baseline": baseline.compact_dict(),
            "candidate_minus_baseline_route_effective_damage": delta,
            "candidate_minus_baseline_route_elapsed_ms": (
                candidate.elapsed_ms - baseline.elapsed_ms if complete else None
            ),
        })
    complete = all(row["paired_complete"] for row in rows)
    return {
        "schema": "continuous_held_out_route_replay/v1",
        "status": (
            "COMPLETE_CONTINUOUS_HELD_OUT_ROUTE_PAIRED_REPLAY"
            if complete
            else "INCOMPLETE_CONTINUOUS_HELD_OUT_ROUTE_PAIRED_REPLAY"
        ),
        "evaluation_seeds": list(seeds),
        "training_evaluation_seeds_disjoint": True,
        "seed_rows": rows,
        "summary": {
            "evaluation_seed_count": len(seeds),
            "paired_seed_count": len(deltas),
            "mean_candidate_minus_baseline_route_effective_damage": (
                sum(deltas) / len(deltas) if complete else None
            ),
        },
        "contract": {
            "one_native_environment_load_per_lane_seed": True,
            "route_state_preserved_between_cells": True,
            "route_value_uses_final_cumulative_damage": True,
            "independent_cell_damage_not_summed": True,
            "accepted_long_cooldown_uses_verified": True,
            "runtime_weapon_swaps_supported": False,
            "failed_lane_scored_as_zero": False,
        },
    }


def _validate_complete_lane_outcome_v1(
    lane: FrozenContinuousRouteLaneV1,
    outcome: ContinuousRouteReplayOutcomeV1,
) -> None:
    if outcome.lane_id != lane.lane_id:
        raise ContinuousRouteReplayV1Error(
            "complete replay outcome names a different lane"
        )
    expected_ids = tuple(row.encounter_id for row in lane.cells)
    observed_ids = tuple(row.encounter_id for row in outcome.cells)
    if observed_ids != expected_ids:
        raise ContinuousRouteReplayV1Error(
            "complete replay lacks the frozen route cell sequence"
        )
    _validate_witness_chain_v1(outcome.state_witnesses)
    final = outcome.state_witnesses[-1]
    if final.boundary != "ROUTE_FINAL":
        raise ContinuousRouteReplayV1Error(
            "complete replay lacks its final route-state witness"
        )
    if final.time_ms != outcome.elapsed_ms or not math.isclose(
        final.cumulative_effective_damage,
        outcome.effective_damage,
        rel_tol=0,
        abs_tol=0,
    ):
        raise ContinuousRouteReplayV1Error(
            "final route-state witness differs from terminal simulator state"
        )
    for expected_cell, observed_cell in zip(
        lane.cells, outcome.cells, strict=True
    ):
        if (
            observed_cell.entry_state.encounter_id != expected_cell.encounter_id
            or observed_cell.exit_state.encounter_id != expected_cell.encounter_id
        ):
            raise ContinuousRouteReplayV1Error(
                "cell boundary witness belongs to a different encounter"
            )
        _verify_expected_cooldown_uses_v1(
            expected_cell.expected_cooldown_uses,
            observed_cell.accepted_cooldown_uses,
        )
    _verify_expected_cooldown_uses_v1(
        tuple(
            use for cell in lane.cells for use in cell.expected_cooldown_uses
        ),
        outcome.accepted_cooldown_uses,
    )


__all__ = (
    "AcceptedCooldownUseAuditV1",
    "ContinuousRouteCellOutcomeV1",
    "ContinuousRouteReplayOutcomeV1",
    "ContinuousRouteReplayStatusV1",
    "ContinuousRouteReplayV1",
    "ContinuousRouteReplayV1Error",
    "ExplicitPerSeedRouteStartResolverV1",
    "ExpectedAcceptedCooldownUseV1",
    "FrozenContinuousRouteCellV1",
    "FrozenContinuousRouteLaneV1",
    "NativeContinuousRouteReplayV1",
    "RoutePlayerStateWitnessV1",
    "build_explicit_route_start_resolver_v1",
    "build_frozen_burst_route_lane_v1",
    "paired_route_timing_observation_from_outcomes_v1",
    "evaluate_continuous_held_out_route_v1",
)
