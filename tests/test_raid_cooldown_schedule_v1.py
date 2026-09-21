from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.raid_cooldown_schedule_v1 import (
    BOSS,
    TRASH,
    CooldownPackageV1,
    CooldownUseV1,
    EncounterCooldownCellV1,
    CooldownPackageAssignmentV1,
    RoutePersistentEffectV1,
    RoutePersistentStatPhaseV1,
    allocate_raid_cooldown_packages_v1,
    materialize_route_persistent_effect_intervals_v1,
    route_stat_deltas_at_ms_v1,
)


def _use(
    resource: str,
    group: str,
    cooldown_ms: int,
    use_at_ms: int = 0,
    kind: str = "SPELL",
) -> CooldownUseV1:
    return CooldownUseV1(resource, group, cooldown_ms, use_at_ms, kind)


def _package(
    name: str,
    value: float,
    *uses: CooldownUseV1,
    elapsed_delta_min: int = 0,
    elapsed_delta_max: int = 0,
) -> CooldownPackageV1:
    return CooldownPackageV1(
        name,
        tuple(uses),
        value,
        encounter_elapsed_ms_delta_min=elapsed_delta_min,
        encounter_elapsed_ms_delta_max=elapsed_delta_max,
    )


RAPID_GROWTH = RoutePersistentEffectV1(
    "rapid-growth",
    (
        RoutePersistentStatPhaseV1(
            "growth", 0, 120_000, (("strength", 30.0),)
        ),
        RoutePersistentStatPhaseV1(
            "deterioration",
            120_000,
            120_000,
            (("stamina", -25.0), ("strength", -25.0)),
        ),
    ),
)


class RaidCooldownScheduleV1Tests(unittest.TestCase):
    def test_joint_package_can_use_potion_death_wish_and_recklessness(self) -> None:
        result = allocate_raid_cooldown_packages_v1((
            EncounterCooldownCellV1(
                "large-pack",
                TRASH,
                60_000,
                (
                    _package(
                        "all-burst",
                        40,
                        _use("mighty-rage", "combat_potion", 120_000, -500, "POTION"),
                        _use("death-wish", "death_wish", 180_000, 0),
                        _use("recklessness", "recklessness", 1_800_000, 1_500),
                    ),
                ),
            ),
        ))
        self.assertEqual(result.assignments[0].package.package_id, "all-burst")
        self.assertEqual(len(result.assignments[0].package.uses), 3)
        self.assertTrue(result.to_dict()["assignments"][0]["absolute_uses"][0]["precombat"])

    def test_death_wish_remains_coupled_through_boss(self) -> None:
        death_wish = _use("death-wish", "death_wish", 180_000)
        cells = (
            EncounterCooldownCellV1("trash", TRASH, 0, (_package("dw-trash", 8, death_wish),)),
            EncounterCooldownCellV1("boss", BOSS, 60_000, (_package("dw-boss", 30, death_wish),)),
            EncounterCooldownCellV1("later", TRASH, 181_000, (_package("dw-later", 9, death_wish),)),
        )
        result = allocate_raid_cooldown_packages_v1(cells)
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["boss"],
        )

    def test_only_potion_group_uses_default_boss_independence(self) -> None:
        potion = _use("mighty-rage", "combat_potion", 120_000, kind="POTION")
        cells = (
            EncounterCooldownCellV1("trash-a", TRASH, 0, (_package("pot-a", 5, potion),)),
            EncounterCooldownCellV1("boss", BOSS, 30_000, (_package("pot-boss", 20, potion),)),
            EncounterCooldownCellV1("trash-b", TRASH, 121_000, (_package("pot-b", 6, potion),)),
        )
        result = allocate_raid_cooldown_packages_v1(cells)
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["trash-a", "boss", "trash-b"],
        )

    def test_package_internal_shared_group_conflict_is_rejected(self) -> None:
        result = allocate_raid_cooldown_packages_v1((
            EncounterCooldownCellV1(
                "pack",
                TRASH,
                0,
                (
                    _package(
                        "two-potions-too-close",
                        100,
                        _use("rage", "combat_potion", 120_000, -1_000, "POTION"),
                        _use("haste", "combat_potion", 120_000, 0, "POTION"),
                    ),
                    _package("death-wish-only", 7, _use("dw", "death_wish", 180_000)),
                ),
            ),
        ))
        self.assertEqual(result.assignments[0].package.package_id, "death-wish-only")

    def test_offline_heuristic_cannot_enter_objective_as_measurement(self) -> None:
        with self.assertRaisesRegex(ValueError, "paired measurements"):
            CooldownPackageV1(
                "offline-common",
                (_use("death-wish", "death_wish", 180_000),),
                12,
                evidence_status="OFFLINE_GUIDE_ONLY",
            )

    def test_initial_cooldown_state_is_enforced(self) -> None:
        result = allocate_raid_cooldown_packages_v1(
            (
                EncounterCooldownCellV1(
                    "early",
                    TRASH,
                    10_000,
                    (_package("dw-early", 50, _use("dw", "death_wish", 180_000)),),
                ),
                EncounterCooldownCellV1(
                    "ready",
                    TRASH,
                    30_000,
                    (_package("dw-ready", 8, _use("dw", "death_wish", 180_000)),),
                ),
            ),
            initial_next_available_ms={"death_wish": 20_000},
        )
        self.assertEqual(result.assignments[0].encounter_id, "ready")

    def test_faster_policy_shifts_later_use_earlier_and_changes_allocation(self):
        death_wish = _use("death-wish", "death_wish", 180_000)
        result = allocate_raid_cooldown_packages_v1((
            EncounterCooldownCellV1(
                "early",
                TRASH,
                0,
                (_package(
                    "early-fast",
                    5,
                    death_wish,
                    elapsed_delta_min=-10_000,
                    elapsed_delta_max=-8_000,
                ),),
            ),
            EncounterCooldownCellV1(
                "later",
                TRASH,
                181_000,
                (_package("later", 10, death_wish),),
            ),
        ), route_time_shift_bounds={
            ("early", "early-fast"): (-10_000, -8_000),
        })
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["later"],
        )

    def test_slower_policy_can_make_later_cooldown_feasible(self):
        death_wish = _use("death-wish", "death_wish", 180_000)
        result = allocate_raid_cooldown_packages_v1((
            EncounterCooldownCellV1(
                "early",
                TRASH,
                0,
                (_package(
                    "early-slow",
                    5,
                    death_wish,
                    elapsed_delta_min=20_000,
                    elapsed_delta_max=22_000,
                ),),
            ),
            EncounterCooldownCellV1(
                "later",
                TRASH,
                170_000,
                (_package("later", 10, death_wish),),
            ),
        ), route_time_shift_bounds={
            ("early", "early-slow"): (20_000, 22_000),
        })
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["early", "later"],
        )
        later = result.assignments[1]
        self.assertEqual(later.route_start_shift_ms_min, 20_000)
        self.assertEqual(later.route_start_shift_ms_max, 22_000)
        wire = later.to_dict()
        self.assertEqual(wire["earliest_raid_start_ms"], 190_000)
        self.assertEqual(wire["latest_raid_start_ms"], 192_000)

    def test_nonzero_elapsed_delta_requires_calibrated_route_shift(self):
        with self.assertRaisesRegex(
            ValueError, "explicit calibrated route_time_shift_bounds"
        ):
            allocate_raid_cooldown_packages_v1((
                EncounterCooldownCellV1(
                    "early",
                    TRASH,
                    0,
                    (_package(
                        "faster",
                        5,
                        elapsed_delta_min=-200,
                        elapsed_delta_max=-100,
                    ),),
                ),
            ))

    def test_use_time_window_checks_earliest_and_advances_from_latest(self):
        first = CooldownUseV1(
            "first",
            "shared",
            100,
            20,
            "SPELL",
            earliest_use_at_ms=0,
        )
        second = CooldownUseV1(
            "second",
            "shared",
            100,
            125,
            "SPELL",
            earliest_use_at_ms=110,
        )
        result = allocate_raid_cooldown_packages_v1((
            EncounterCooldownCellV1(
                "wave-a", TRASH, 0, (_package("first", 5, first),)
            ),
            EncounterCooldownCellV1(
                "wave-b", TRASH, 0, (_package("second", 10, second),)
            ),
        ))
        self.assertEqual(
            [row.encounter_id for row in result.assignments],
            ["wave-b"],
        )

    def test_use_time_window_cannot_cross_pull_boundary(self):
        with self.assertRaisesRegex(ValueError, "cross the pull boundary"):
            CooldownUseV1(
                "mighty-rage",
                "combat_potion",
                120_000,
                20,
                "POTION",
                earliest_use_at_ms=-20,
            )

    def test_persistent_effect_contract_materializes_both_route_phases(self):
        use = CooldownUseV1(
            "rapid-growth",
            "rapid-growth",
            120_000,
            -2_000,
            "CONSUMABLE",
            RAPID_GROWTH,
        )
        assignment = CooldownPackageAssignmentV1(
            "wave-1",
            TRASH,
            10_000,
            _package("rapid", 8, use),
        )
        intervals = materialize_route_persistent_effect_intervals_v1(
            (assignment,)
        )
        self.assertEqual(
            [(row.starts_at_ms, row.ends_at_ms) for row in intervals],
            [(8_000, 128_000), (128_000, 248_000)],
        )
        self.assertEqual(route_stat_deltas_at_ms_v1(intervals, 8_000), {
            "strength": 30.0,
        })
        self.assertEqual(route_stat_deltas_at_ms_v1(intervals, 128_000), {
            "stamina": -25.0,
            "strength": -25.0,
        })
        self.assertEqual(route_stat_deltas_at_ms_v1(intervals, 248_000), {})

    def test_isolated_wave_marginal_with_persistent_effect_fails_closed(self):
        persistent = CooldownUseV1(
            "rapid-growth",
            "rapid-growth",
            120_000,
            0,
            "CONSUMABLE",
            RAPID_GROWTH,
        )
        with self.assertRaisesRegex(ValueError, "cross-encounter state-aware"):
            allocate_raid_cooldown_packages_v1((
                EncounterCooldownCellV1(
                    "wave-1",
                    TRASH,
                    0,
                    (_package("rapid", 8, persistent),),
                ),
            ))


if __name__ == "__main__":
    unittest.main()
