"""Current-observation burst candidates for continuous Upper Kara routes.

The grid is deliberately independent of Contra's manual trigger and boss-HP
settings.  It exposes each modeled resource at each target and HP lower bound;
the action-program search decides which alternatives and order to retain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .contra_turtle_burst_loadout_v1 import GOBLIN_SAPPER_ACTION
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .sim_bridge import ActionRef
from .upper_kara_two_wave_burst_case_v1 import SCHEMA as TWO_WAVE_CASE_SCHEMA


JSONMap = dict[str, Any]
DEFAULT_ARRIVAL_HP_THRESHOLDS_V1 = (20, 35, 50, 65, 80)


@dataclass(frozen=True)
class BurstRescheduleCandidateV1:
    target_index: int
    hp_threshold_pct: int
    action: ActionRef
    guard: ObservableCausalGuardV1

    def __post_init__(self) -> None:
        if type(self.target_index) is not int or self.target_index < 0:
            raise ValueError("target_index must be a nonnegative integer")
        if (
            type(self.hp_threshold_pct) is not int
            or not 1 <= self.hp_threshold_pct <= 100
        ):
            raise ValueError("hp_threshold_pct must be an integer in [1, 100]")
        if not isinstance(self.action, ActionRef):
            raise TypeError("action must be ActionRef")
        expected = ObservableCausalGuardV1(
            target_index=self.target_index,
            target_hp_pct_gte=self.hp_threshold_pct,
            target_attackable_is=True,
            action_ready=self.action,
            false_semantics=SKIP_PLAN,
        )
        if self.guard != expected:
            raise ValueError("guard does not match the reschedule candidate")

    def to_dict(self) -> JSONMap:
        return {
            "schema": "upper_kara_burst_reschedule_candidate/v1",
            "target_index": self.target_index,
            "hp_threshold_pct": self.hp_threshold_pct,
            "action": self.action.to_wire(),
            "guard": self.guard.to_dict(),
            "execution_lane": "CLASSIFY_FROM_CURRENT_NATIVE_AVAILABLE_ACTION",
            "future_fields_used": [],
        }


def _thresholds(values: Iterable[int]) -> tuple[int, ...]:
    rows = tuple(values)
    if not rows or any(type(value) is not int or not 1 <= value <= 100 for value in rows):
        raise ValueError("hp_thresholds must contain integers in [1, 100]")
    if len(set(rows)) != len(rows):
        raise ValueError("hp_thresholds must be unique")
    return tuple(sorted(rows))


def build_upper_kara_burst_reschedule_grid_v1(
    case: DevelopmentPrecombatWaveCaseV1,
    *,
    hp_thresholds: Iterable[int] = DEFAULT_ARRIVAL_HP_THRESHOLDS_V1,
) -> tuple[BurstRescheduleCandidateV1, ...]:
    """Enumerate target/resource/threshold candidates without future data."""

    if not isinstance(case, DevelopmentPrecombatWaveCaseV1):
        raise TypeError("case must be DevelopmentPrecombatWaveCaseV1")
    if case.case_spec.get("schema") != TWO_WAVE_CASE_SCHEMA:
        raise ValueError("case must be a continuous two-wave burst case")
    thresholds = _thresholds(hp_thresholds)
    target_indexes = tuple(sorted(case.target_contexts))
    if target_indexes != tuple(range(len(target_indexes))):
        raise ValueError("case target indexes must be contiguous from zero")
    actions = tuple(sorted({*case.precombat.self_actions, GOBLIN_SAPPER_ACTION}))
    result = []
    for target_index in target_indexes:
        for action in actions:
            for threshold in thresholds:
                guard = ObservableCausalGuardV1(
                    target_index=target_index,
                    target_hp_pct_gte=threshold,
                    target_attackable_is=True,
                    action_ready=action,
                    false_semantics=SKIP_PLAN,
                )
                result.append(
                    BurstRescheduleCandidateV1(
                        target_index=target_index,
                        hp_threshold_pct=threshold,
                        action=action,
                        guard=guard,
                    )
                )
    return tuple(result)


__all__ = (
    "BurstRescheduleCandidateV1",
    "DEFAULT_ARRIVAL_HP_THRESHOLDS_V1",
    "build_upper_kara_burst_reschedule_grid_v1",
)
