from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.raid_consumable_allocator_v1 import (
    BOSS,
    TRASH,
    ConsumableOptionV1,
    EncounterConsumableCellV1,
    allocate_raid_consumables_v1,
)


def _option(name: str, value: float, *, cooldown_ms: int = 120_000):
    return ConsumableOptionV1(
        consumable_id=name,
        cooldown_group="combat_potion",
        cooldown_ms=cooldown_ms,
        marginal_value=value,
        use_at_ms=0,
    )


class RaidConsumableAllocatorV1Tests(unittest.TestCase):
    def test_trash_allocation_uses_measured_gain_not_wave_order(self) -> None:
        result = allocate_raid_consumables_v1((
            EncounterConsumableCellV1("small", TRASH, 0, (_option("rage", 5),)),
            EncounterConsumableCellV1("large", TRASH, 60_000, (_option("rage", 20),)),
            EncounterConsumableCellV1("later", TRASH, 181_000, (_option("rage", 7),)),
        ))
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["large", "later"],
        )
        self.assertEqual(result.total_marginal_value, 27)

    def test_selects_potion_kind_with_largest_paired_marginal(self) -> None:
        result = allocate_raid_consumables_v1((
            EncounterConsumableCellV1(
                "pack", TRASH, 0,
                (_option("rage", 8), _option("strength", 11)),
            ),
        ))
        self.assertEqual(result.assignments[0].option.consumable_id, "strength")

    def test_boss_does_not_read_or_update_trash_cooldown_by_assumption(self) -> None:
        cells = (
            EncounterConsumableCellV1("trash-a", TRASH, 0, (_option("rage", 6),)),
            EncounterConsumableCellV1("boss", BOSS, 30_000, (_option("rage", 30),)),
            EncounterConsumableCellV1("trash-b", TRASH, 121_000, (_option("rage", 9),)),
        )
        independent = allocate_raid_consumables_v1(cells)
        coupled = allocate_raid_consumables_v1(
            cells, boss_cooldowns_independent=False
        )
        self.assertEqual(
            [row.encounter_id for row in independent.assignments],
            ["trash-a", "boss", "trash-b"],
        )
        self.assertEqual(
            [row.encounter_id for row in coupled.assignments],
            ["boss"],
        )

    def test_precombat_use_is_scheduled_before_pull_and_starts_cooldown_then(self) -> None:
        prepot = ConsumableOptionV1(
            "rage", "combat_potion", 120_000, 12, -3_000
        )
        result = allocate_raid_consumables_v1((
            EncounterConsumableCellV1("fast-pack", TRASH, 0, (prepot,)),
            EncounterConsumableCellV1("later-pack", TRASH, 118_000, (_option("rage", 9),)),
        ))
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["fast-pack", "later-pack"],
        )
        self.assertEqual(result.assignments[0].raid_use_time_ms, -3_000)
        self.assertTrue(result.assignments[0].to_dict()["precombat"])

    def test_rejects_unmeasured_heuristic_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "paired measurements"):
            ConsumableOptionV1(
                "rage", "combat_potion", 120_000, 10, 0,
                evidence_status="HP_DURATION_HEURISTIC",
            )


if __name__ == "__main__":
    unittest.main()
