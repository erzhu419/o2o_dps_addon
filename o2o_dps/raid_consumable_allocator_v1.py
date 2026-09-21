"""Exact raid-level allocation of measured consumable opportunities.

Per-wave search owns the in-wave action time.  This module only chooses which
wave receives which consumable after paired simulations have measured the
incremental objective for every admitted option.  A heuristic may order the
measurements, but it is deliberately absent from the optimizer's objective.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Iterable, Mapping


TRASH = "TRASH"
BOSS = "BOSS"


@dataclass(frozen=True)
class ConsumableOptionV1:
    """One paired-measured consumable alternative for one encounter unit."""

    consumable_id: str
    cooldown_group: str
    cooldown_ms: int
    marginal_value: float
    use_at_ms: int
    evidence_status: str = "PAIRED_MEASURED"

    def __post_init__(self) -> None:
        if not self.consumable_id.strip() or not self.cooldown_group.strip():
            raise ValueError("consumable_id and cooldown_group must be nonempty")
        if type(self.cooldown_ms) is not int or self.cooldown_ms <= 0:
            raise ValueError("cooldown_ms must be a positive integer")
        if type(self.use_at_ms) is not int:
            raise ValueError("use_at_ms must be an integer relative to pull")
        if (
            isinstance(self.marginal_value, bool)
            or not isinstance(self.marginal_value, (int, float))
            or not math.isfinite(float(self.marginal_value))
        ):
            raise ValueError("marginal_value must be finite")
        if self.evidence_status != "PAIRED_MEASURED":
            raise ValueError(
                "allocation values must come from paired measurements"
            )


@dataclass(frozen=True)
class EncounterConsumableCellV1:
    """Consumable alternatives for one ordered trash wave or boss."""

    encounter_id: str
    encounter_kind: str
    raid_start_ms: int
    options: tuple[ConsumableOptionV1, ...]

    def __post_init__(self) -> None:
        if not self.encounter_id.strip():
            raise ValueError("encounter_id must be nonempty")
        if self.encounter_kind not in {TRASH, BOSS}:
            raise ValueError("encounter_kind must be TRASH or BOSS")
        if type(self.raid_start_ms) is not int or self.raid_start_ms < 0:
            raise ValueError("raid_start_ms must be a nonnegative integer")
        if not isinstance(self.options, tuple) or any(
            not isinstance(option, ConsumableOptionV1)
            for option in self.options
        ):
            raise TypeError("options must be a tuple of ConsumableOptionV1")
        identities = [option.consumable_id for option in self.options]
        if len(identities) != len(set(identities)):
            raise ValueError("an encounter contains duplicate consumable_id values")


@dataclass(frozen=True)
class ConsumableAssignmentV1:
    encounter_id: str
    encounter_kind: str
    raid_use_time_ms: int
    option: ConsumableOptionV1

    def to_dict(self) -> dict[str, object]:
        return {
            "encounter_id": self.encounter_id,
            "encounter_kind": self.encounter_kind,
            "raid_use_time_ms": self.raid_use_time_ms,
            "consumable_id": self.option.consumable_id,
            "cooldown_group": self.option.cooldown_group,
            "cooldown_ms": self.option.cooldown_ms,
            "use_at_ms": self.option.use_at_ms,
            "precombat": self.option.use_at_ms < 0,
            "marginal_value": float(self.option.marginal_value),
            "evidence_status": self.option.evidence_status,
        }


@dataclass(frozen=True)
class ConsumableAllocationV1:
    assignments: tuple[ConsumableAssignmentV1, ...]
    total_marginal_value: float
    boss_cooldowns_independent: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "raid_consumable_allocation/v1",
            "boss_cooldowns_independent": self.boss_cooldowns_independent,
            "objective": "SUM_OF_PAIRED_MEASURED_PER_ENCOUNTER_MARGINAL_VALUE",
            "total_marginal_value": self.total_marginal_value,
            "assignments": [row.to_dict() for row in self.assignments],
        }


def allocate_raid_consumables_v1(
    cells: Iterable[EncounterConsumableCellV1],
    *,
    boss_cooldowns_independent: bool = True,
) -> ConsumableAllocationV1:
    """Choose at most one option per encounter under shared cooldown groups.

    With ``boss_cooldowns_independent=True``, boss uses neither read nor update
    the trash-wave cooldown clocks.  This is the explicit project assumption,
    not an inferred game mechanic.
    """

    if not isinstance(boss_cooldowns_independent, bool):
        raise TypeError("boss_cooldowns_independent must be boolean")
    ordered = tuple(sorted(cells, key=lambda row: (row.raid_start_ms, row.encounter_id)))
    if any(not isinstance(row, EncounterConsumableCellV1) for row in ordered):
        raise TypeError("cells must contain EncounterConsumableCellV1 values")
    encounter_ids = [row.encounter_id for row in ordered]
    if len(encounter_ids) != len(set(encounter_ids)):
        raise ValueError("encounter_id values must be unique")
    groups = tuple(sorted({
        option.cooldown_group
        for row in ordered
        for option in row.options
    }))
    group_index = {group: index for index, group in enumerate(groups)}

    @lru_cache(maxsize=None)
    def solve(
        index: int, next_available: tuple[int, ...]
    ) -> tuple[float, tuple[ConsumableAssignmentV1, ...]]:
        if index == len(ordered):
            return 0.0, ()
        cell = ordered[index]
        best_value, best_rows = solve(index + 1, next_available)
        for option in cell.options:
            if option.marginal_value <= 0:
                continue
            use_time = cell.raid_start_ms + option.use_at_ms
            group_slot = group_index[option.cooldown_group]
            boss_isolated = (
                boss_cooldowns_independent and cell.encounter_kind == BOSS
            )
            if not boss_isolated and use_time < next_available[group_slot]:
                continue
            updated = next_available
            if not boss_isolated:
                values = list(next_available)
                values[group_slot] = use_time + option.cooldown_ms
                updated = tuple(values)
            suffix_value, suffix_rows = solve(index + 1, updated)
            candidate_value = float(option.marginal_value) + suffix_value
            candidate = ConsumableAssignmentV1(
                encounter_id=cell.encounter_id,
                encounter_kind=cell.encounter_kind,
                raid_use_time_ms=use_time,
                option=option,
            )
            candidate_rows = (candidate, *suffix_rows)
            if _better(candidate_value, candidate_rows, best_value, best_rows):
                best_value, best_rows = candidate_value, candidate_rows
        return best_value, best_rows

    # The raid clock's zero is an arbitrary route anchor.  A first-wave pre-pot
    # may therefore have a negative timestamp and must not be rejected as if a
    # cooldown were already active at t=0.
    value, assignments = solve(0, tuple(-(1 << 60) for _ in groups))
    return ConsumableAllocationV1(
        assignments=assignments,
        total_marginal_value=value,
        boss_cooldowns_independent=boss_cooldowns_independent,
    )


def _better(
    candidate_value: float,
    candidate_rows: tuple[ConsumableAssignmentV1, ...],
    incumbent_value: float,
    incumbent_rows: tuple[ConsumableAssignmentV1, ...],
) -> bool:
    if not math.isclose(candidate_value, incumbent_value, abs_tol=1e-12):
        return candidate_value > incumbent_value
    candidate_key = tuple(
        (row.raid_use_time_ms, row.encounter_id, row.option.consumable_id)
        for row in candidate_rows
    )
    incumbent_key = tuple(
        (row.raid_use_time_ms, row.encounter_id, row.option.consumable_id)
        for row in incumbent_rows
    )
    return candidate_key < incumbent_key


__all__ = (
    "BOSS",
    "TRASH",
    "ConsumableAllocationV1",
    "ConsumableAssignmentV1",
    "ConsumableOptionV1",
    "EncounterConsumableCellV1",
    "allocate_raid_consumables_v1",
)
