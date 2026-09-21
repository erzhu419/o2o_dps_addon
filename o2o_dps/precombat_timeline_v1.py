"""Pull-relative scheduling over the simulator's non-negative clock.

Historical and searched actions are expressed relative to pull (negative means
precombat).  This module shifts a dynamic-v3 wave and its request by one fixed
offset, then uses an atomic bridge load that exposes only explicitly declared
self-actions while every target is still unattackable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import math
from typing import Any, Mapping

from .precombat_contract_v1 import (
    PrecombatActionsConfigV1,
    precombat_state_from_wire_v1,
)
from .sim_bridge import ActionRef, BackgroundDamageEventV1
from .sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from .sim_bridge_dynamic_v3 import (
    DynamicLoadResultV3,
    DynamicTargetSemanticsConfigV3,
    SimulatorBridgeDynamicV3,
    _validate_atomic_press_clock_load_v1,
)
from .wave_action_schedule_v1 import ScheduledActionPlan


JSONMap = dict[str, Any]


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


@dataclass(frozen=True)
class PullRelativeTimelineV1:
    """Map t=0 at pull onto a non-negative simulator clock."""

    pull_time_ms: int

    def __post_init__(self) -> None:
        if _strict_int(self.pull_time_ms, "pull_time_ms") <= 0:
            raise ValueError("pull_time_ms must be positive")

    def to_simulator_time_ms(self, relative_to_pull_ms: int) -> int:
        relative = _strict_int(relative_to_pull_ms, "relative_to_pull_ms")
        shifted = self.pull_time_ms + relative
        if shifted < 0:
            raise ValueError("relative action precedes the modeled precombat window")
        return shifted

    def to_pull_relative_time_ms(self, simulator_time_ms: int) -> int:
        simulator = _strict_int(simulator_time_ms, "simulator_time_ms")
        if simulator < 0:
            raise ValueError("simulator_time_ms must be non-negative")
        return simulator - self.pull_time_ms


@dataclass(frozen=True)
class PullRelativeScheduledActionV1:
    """One existing finite-schedule plan placed relative to pull."""

    relative_to_pull_ms: int
    plan: ScheduledActionPlan

    def __post_init__(self) -> None:
        _strict_int(self.relative_to_pull_ms, "relative_to_pull_ms")
        if not isinstance(self.plan, ScheduledActionPlan):
            raise TypeError("plan must be ScheduledActionPlan")

    def to_simulator_plan(
        self, timeline: PullRelativeTimelineV1
    ) -> ScheduledActionPlan:
        if not isinstance(timeline, PullRelativeTimelineV1):
            raise TypeError("timeline must be PullRelativeTimelineV1")
        return replace(
            self.plan,
            at_or_after_ms=timeline.to_simulator_time_ms(
                self.relative_to_pull_ms
            ),
        )


def shift_dynamic_config_for_precombat_v1(
    config: DynamicTargetSemanticsConfigV3,
    timeline: PullRelativeTimelineV1,
    *,
    attackable_at_pull: Mapping[int, bool] | None = None,
) -> DynamicTargetSemanticsConfigV3:
    """Shift one pull-origin dynamic-v3 scenario onto ``timeline``.

    Existing event times are interpreted relative to pull and shifted.  A time
    zero all-unattackable state is prepended.  Each target's explicit original
    t=0 attackability wins; otherwise ``attackable_at_pull`` (default True)
    supplies its state at the pull boundary.
    """

    if not isinstance(config, DynamicTargetSemanticsConfigV3):
        raise TypeError("config must be DynamicTargetSemanticsConfigV3")
    if not isinstance(timeline, PullRelativeTimelineV1):
        raise TypeError("timeline must be PullRelativeTimelineV1")
    pull = timeline.pull_time_ms
    target_count = len(config.target_health)
    requested = dict(attackable_at_pull or {})
    if any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= target_count
        or not isinstance(value, bool)
        for index, value in requested.items()
    ):
        raise ValueError("attackable_at_pull must map valid target indexes to booleans")

    pull_state = {index: requested.get(index, True) for index in range(target_count)}
    later_attackability: list[tuple[int, int, bool]] = []
    for event in config.attackability_events:
        if event.time_ms == 0:
            pull_state[event.target_index] = event.attackable
        else:
            later_attackability.append(
                (event.time_ms + pull, event.target_index, event.attackable)
            )
    attackability_rows = [
        (0, index, False) for index in range(target_count)
    ] + [
        (pull, index, pull_state[index]) for index in range(target_count)
    ] + later_attackability
    attackability_rows.sort(key=lambda row: (row[0], row[1]))

    background = tuple(
        BackgroundDamageEventV1(
            schedule_index=index,
            time_ms=event.time_ms + pull,
            target_index=event.target_index,
            event_id=event.event_id,
            damage=event.damage,
        )
        for index, event in enumerate(config.background_damage_events)
    )
    attackability = tuple(
        DynamicAttackabilityEventV2(
            schedule_index=index,
            time_ms=time_ms,
            target_index=target_index,
            attackable=attackable,
        )
        for index, (time_ms, target_index, attackable) in enumerate(
            attackability_rows
        )
    )
    armor = tuple(
        DynamicEffectiveArmorEventV2(
            schedule_index=index,
            time_ms=event.time_ms + pull,
            target_index=event.target_index,
            effective_armor=event.effective_armor,
        )
        for index, event in enumerate(config.effective_armor_events)
    )
    return DynamicTargetSemanticsConfigV3(
        target_health=config.target_health,
        idle_advance_horizon_ms=config.idle_advance_horizon_ms + pull,
        background_damage_events=background,
        attackability_events=attackability,
        effective_armor_events=armor,
        idle_advance_mode=config.idle_advance_mode,
        same_timestamp_order=config.same_timestamp_order,
        retarget_mode=config.retarget_mode,
    )


def shift_raid_request_for_precombat_v1(
    request: Mapping[str, Any], timeline: PullRelativeTimelineV1
) -> JSONMap:
    """Deep-copy a RaidSimRequest and extend only its declared horizon."""

    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    if not isinstance(timeline, PullRelativeTimelineV1):
        raise TypeError("timeline must be PullRelativeTimelineV1")
    result = copy.deepcopy(dict(request))
    encounter = result.get("encounter")
    if not isinstance(encounter, dict):
        raise ValueError("request.encounter must be an object")
    duration = encounter.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise TypeError("request.encounter.duration must be numeric")
    numeric_duration = float(duration)
    if not math.isfinite(numeric_duration) or numeric_duration <= 0:
        raise ValueError("request.encounter.duration must be finite and positive")
    duration_ms = math.floor(numeric_duration * 1000 + 0.5)
    encounter["duration"] = (duration_ms + timeline.pull_time_ms) / 1000.0
    return result


class SimulatorBridgePrecombatV1(SimulatorBridgeDynamicV3):
    """Dynamic-v3 bridge with one atomically configured precombat window."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._precombat_binding: PrecombatActionsConfigV1 | None = None
        super().__init__(*args, **kwargs)

    def load_dynamic_v3_precombat(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV3,
        precombat: PrecombatActionsConfigV1,
    ) -> DynamicLoadResultV3:
        if not isinstance(precombat, PrecombatActionsConfigV1):
            raise TypeError("precombat must be PrecombatActionsConfigV1")
        if precombat.pull_time_ms >= config.idle_advance_horizon_ms:
            raise ValueError("precombat pull must be before the dynamic horizon")
        self._precombat_binding = None
        result = self._load_dynamic_v3_command(
            "load_dynamic_v3_precombat",
            request,
            seed,
            config,
            precombat=precombat.to_wire(),
        )
        precombat_state_from_wire_v1(result.state, config=precombat)
        self._precombat_binding = precombat
        return result

    def load_dynamic_v3_precombat_press_clock(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV3,
        precombat: PrecombatActionsConfigV1,
        period_ms: int,
        phase_ms: int = 0,
    ) -> DynamicLoadResultV3:
        """Atomically bind precombat semantics and the physical key grid."""

        if not isinstance(precombat, PrecombatActionsConfigV1):
            raise TypeError("precombat must be PrecombatActionsConfigV1")
        if precombat.pull_time_ms >= config.idle_advance_horizon_ms:
            raise ValueError("precombat pull must be before the dynamic horizon")
        if type(period_ms) is not int or not 1 <= period_ms <= 60_000:
            raise ValueError("period_ms must be an integer in 1..60000")
        if type(phase_ms) is not int or not 0 <= phase_ms < period_ms:
            raise ValueError("phase_ms must be an integer in 0..period_ms-1")
        self._precombat_binding = None
        result = self._load_dynamic_v3_command(
            "load_dynamic_v3_precombat_press_clock",
            request,
            seed,
            config,
            precombat=precombat.to_wire(),
            press_period_ms=period_ms,
            press_phase_ms=phase_ms,
        )
        try:
            precombat_state_from_wire_v1(result.state, config=precombat)
            _validate_atomic_press_clock_load_v1(
                result,
                period_ms=period_ms,
                phase_ms=phase_ms,
                context="dynamic-v3 precombat",
            )
        except Exception:
            self._dynamic_binding = None
            raise
        self._precombat_binding = precombat
        return result

    def _validate_bound_state(self, state: Mapping[str, Any]) -> None:
        super()._validate_bound_state(state)
        binding = getattr(self, "_precombat_binding", None)
        if binding is not None:
            precombat_state_from_wire_v1(state, config=binding)


__all__ = (
    "PrecombatActionsConfigV1",
    "PullRelativeScheduledActionV1",
    "PullRelativeTimelineV1",
    "SimulatorBridgePrecombatV1",
    "shift_dynamic_config_for_precombat_v1",
    "shift_raid_request_for_precombat_v1",
)
