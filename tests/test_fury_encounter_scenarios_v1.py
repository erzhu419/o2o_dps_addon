from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.fury_encounter_scenarios_v1 import (
    ARMOR_STAT_INDEX,
    HEALTH_STAT_INDEX,
    ScenarioCompileError,
    compile_fury_encounter_scenarios,
)


MOB_A = "0xF130000001000001"
MOB_B = "0xF130000002000002"
MOB_C = "0xF130000003000003"


def anchor(index: int, offset_ms: int) -> dict[str, object]:
    return {
        "encounter": "enc-1",
        "event_index": index,
        "offset_ms": offset_ms,
        "type": "DMG",
        "source_file": "compact-fixture.jsonl",
        "csv_line": index + 1,
    }


def target(
    guid: str,
    name: str,
    *,
    kill_budget: int | None,
    first_index: int,
) -> dict[str, object]:
    first = anchor(first_index, 1_000)
    last = anchor(first_index + 1, 4_000)
    return {
        "target_guid": guid,
        "target_name": name,
        "activity_interval": {
            "first_offset_ms": 1_000,
            "last_offset_ms": 4_000,
            "status": "OBSERVED",
            "first_anchor": first,
            "last_anchor": last,
        },
        "kill_budget_proxy": {
            "value": kill_budget,
            "status": "OBSERVED" if kill_budget is not None else "MISSING",
            "death_anchor": last if kill_budget is not None else None,
            "not_equal_to": "exact maximum or initial health",
        },
    }


def wave(
    ordinal: int,
    targets: list[dict[str, object]],
    *,
    duration_ms: int = 3_000,
    stacked: bool,
) -> dict[str, object]:
    guids = [str(value["target_guid"]) for value in targets]
    first = anchor(ordinal * 10, ordinal * 10_000)
    last = anchor(ordinal * 10 + 1, ordinal * 10_000 + duration_ms)
    if stacked:
        groups = [
            {
                "group_id": f"wave-{ordinal}-group-1",
                "target_guids": guids,
                "position_assumption": {
                    "value": "stacked",
                    "status": "INFERRED",
                    "basis": "connected same-batch melee/cleave co-hit evidence",
                    "coordinates": None,
                },
            }
        ]
        position = {
            "value": "stacked",
            "status": "INFERRED",
            "basis": "all observed targets connected by melee co-hit",
            "coordinates": None,
        }
    else:
        groups = [
            {
                "group_id": f"wave-{ordinal}-group-{index}",
                "target_guids": [guid],
                "position_assumption": {
                    "value": "unknown",
                    "status": "MISSING",
                    "basis": "no positive relative-position evidence",
                    "coordinates": None,
                },
            }
            for index, guid in enumerate(guids, start=1)
        ]
        position = {
            "value": "unknown",
            "status": "MISSING",
            "basis": "absence of co-hit cannot establish separation",
            "coordinates": None,
        }
    return {
        "wave_id": f"enc-1:wave:{ordinal}",
        "ordinal": ordinal,
        "start_offset_ms": first["offset_ms"],
        "end_offset_ms": last["offset_ms"],
        "duration_ms": duration_ms,
        "first_anchor": first,
        "last_anchor": last,
        "target_count": len(targets),
        "targets": targets,
        "target_groups": groups,
        "position_assumption": position,
    }


def report(waves: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "chronicle_encounter_reconstruction_v1",
        "generated_at": "2026-09-01T00:00:00Z",
        "source": {"normalized_file": "compact-normalized.jsonl"},
        "encounters": [
            {
                "instance": "instance-1",
                "encounter": "enc-1",
                "wave_count": len(waves),
                "waves": waves,
            }
        ],
    }


def base_request(*, use_health: bool | None = True) -> dict[str, object]:
    stats = [0] * 46
    stats[17] = 320
    stats[ARMOR_STAT_INDEX] = 9_999
    stats[HEALTH_STAT_INDEX] = 999_999
    encounter: dict[str, object] = {
        "duration": 300,
        "durationVariation": 30,
        "targets": [
            {
                "id": 12345,
                "name": "Template Boss",
                "level": 60,
                "mobType": "MobTypeDemon",
                "stats": stats,
                "minBaseDamage": 4_000,
                "swingSpeed": 2,
                "parryHaste": True,
                "tankIndex": 0,
            }
        ],
    }
    if use_health is not None:
        encounter["useHealth"] = use_health
    return {
        "raid": {"parties": [{"players": [{"name": "Fury"}]}]},
        "encounter": encounter,
        "simOptions": {"iterations": 1, "interactive": True},
    }


class FuryEncounterScenariosV1Tests(unittest.TestCase):
    def compile(
        self,
        reconstruction: dict[str, object],
        *,
        armors: tuple[int | float, ...] = (1_104,),
        levels: tuple[int, ...] = (63,),
        base: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return compile_fury_encounter_scenarios(
            reconstruction,
            base or base_request(),
            armor_hypotheses=armors,
            level_hypotheses=levels,
            base_request_label="clean-dual-fixture.json",
        )

    def test_melee_connected_wave_compiles_one_inferred_stacked_request(self) -> None:
        reconstruction = report(
            [
                wave(
                    1,
                    [
                        target(MOB_A, "Mob A", kill_budget=600, first_index=1),
                        target(MOB_B, "Mob B", kill_budget=800, first_index=3),
                    ],
                    stacked=True,
                )
            ]
        )
        catalog = self.compile(reconstruction)

        self.assertEqual(catalog["summary"]["scenario_count"], 1)
        scenario = catalog["scenarios"][0]
        self.assertEqual(scenario["pile"]["layout_variant"], "cohit_stacked")
        spatial = scenario["pile"]["spatial_assumption"]
        self.assertEqual(spatial["status"], "INFERRED")
        self.assertTrue(spatial["not_observed"])
        self.assertEqual(scenario["pile"]["target_count"], 2)
        self.assertEqual(scenario["request"]["encounter"]["duration"], 3)
        self.assertEqual(scenario["duration"]["observed_span_ms"], 3_000)
        self.assertEqual(
            scenario["source"]["wave_first_anchor"],
            reconstruction["encounters"][0]["waves"][0]["first_anchor"],
        )
        self.assertEqual(scenario["weight"]["value"], 1_400)
        self.assertTrue(scenario["weight"]["not_exact_health"])

    def test_unknown_space_emits_stacked_upper_and_separated_singletons(self) -> None:
        reconstruction = report(
            [
                wave(
                    1,
                    [
                        target(MOB_A, "Mob A", kill_budget=600, first_index=1),
                        target(MOB_B, "Mob B", kill_budget=800, first_index=3),
                    ],
                    stacked=False,
                )
            ]
        )
        catalog = self.compile(reconstruction)
        scenarios = catalog["scenarios"]

        self.assertEqual(len(scenarios), 3)
        self.assertEqual(
            [value["pile"]["layout_variant"] for value in scenarios],
            ["stacked_upper", "separated_singleton", "separated_singleton"],
        )
        upper = scenarios[0]
        self.assertEqual(upper["pile"]["target_count"], 2)
        self.assertEqual(upper["pile"]["layout_side"], "upper")
        self.assertEqual(
            upper["pile"]["spatial_assumption"]["role"], "sensitivity_upper"
        )
        for singleton in scenarios[1:]:
            self.assertEqual(singleton["pile"]["target_count"], 1)
            self.assertEqual(singleton["pile"]["layout_side"], "lower")
            self.assertEqual(
                singleton["pile"]["sensitivity_family"],
                upper["pile"]["sensitivity_family"],
            )
            self.assertEqual(
                singleton["pile"]["spatial_assumption"]["role"],
                "sensitivity_lower",
            )
            self.assertTrue(
                singleton["pile"]["spatial_assumption"]["not_observed"]
            )

        # Full requests are independent clones, not shared mutable patches.
        upper["request"]["encounter"]["targets"][0]["stats"][ARMOR_STAT_INDEX] = 1
        self.assertEqual(
            scenarios[1]["request"]["encounter"]["targets"][0]["stats"][ARMOR_STAT_INDEX],
            1_104,
        )

    def test_explicit_armor_and_level_grid_never_inherits_target_template(self) -> None:
        reconstruction = report(
            [wave(1, [target(MOB_A, "Mob A", kill_budget=600, first_index=1)], stacked=False)]
        )
        base = base_request()
        original = deepcopy(base)
        catalog = self.compile(
            reconstruction,
            armors=(1_104, 1_721),
            levels=(62, 63),
            base=base,
        )

        self.assertEqual(len(catalog["scenarios"]), 4)
        observed = {
            (
                value["target_hypotheses"]["armor"]["value"],
                value["target_hypotheses"]["level"]["value"],
            )
            for value in catalog["scenarios"]
        }
        self.assertEqual(observed, {(1_104, 62), (1_104, 63), (1_721, 62), (1_721, 63)})
        for scenario in catalog["scenarios"]:
            hypothesis = scenario["target_hypotheses"]
            sim_target = scenario["request"]["encounter"]["targets"][0]
            self.assertEqual(
                sim_target["stats"][ARMOR_STAT_INDEX], hypothesis["armor"]["value"]
            )
            self.assertEqual(sim_target["level"], hypothesis["level"]["value"])
            self.assertEqual(sim_target["mobType"], "MobTypeUnknown")
            self.assertNotIn("id", sim_target)
            self.assertNotIn("minBaseDamage", sim_target)
            self.assertEqual(sim_target["stats"][17], 0)
            self.assertTrue(hypothesis["armor"]["not_identified_by_chronicle"])
            self.assertTrue(hypothesis["level"]["not_identified_by_chronicle"])
        self.assertEqual(base, original)

    def test_duration_mode_disables_health_and_keeps_kill_budget_out_of_stats(self) -> None:
        reconstruction = report(
            [wave(1, [target(MOB_A, "Mob A", kill_budget=123_456, first_index=1)], stacked=False)]
        )
        for inherited in (True, None):
            with self.subTest(base_use_health=inherited):
                catalog = self.compile(
                    reconstruction,
                    base=base_request(use_health=inherited),
                )
                scenario = catalog["scenarios"][0]
                encounter = scenario["request"]["encounter"]
                self.assertIs(encounter["useHealth"], False)
                self.assertEqual(encounter["durationVariation"], 0)
                self.assertEqual(
                    encounter["targets"][0]["stats"][HEALTH_STAT_INDEX], 0
                )
                self.assertEqual(scenario["kill_budget_proxies"][0]["value"], 123_456)
                self.assertTrue(
                    scenario["kill_budget_proxies"][0]["not_exact_health"]
                )

    def test_each_wave_builds_independent_duration_request(self) -> None:
        reconstruction = report(
            [
                wave(1, [target(MOB_A, "Mob A", kill_budget=100, first_index=1)], duration_ms=2_000, stacked=False),
                wave(2, [target(MOB_C, "Mob C", kill_budget=200, first_index=3)], duration_ms=7_500, stacked=False),
            ]
        )
        catalog = self.compile(reconstruction)
        scenarios = catalog["scenarios"]

        self.assertEqual(catalog["summary"]["wave_count"], 2)
        self.assertEqual(len(scenarios), 2)
        self.assertEqual(
            [value["request"]["encounter"]["duration"] for value in scenarios],
            [2, 7.5],
        )
        self.assertNotEqual(
            scenarios[0]["source"]["wave_id"], scenarios[1]["source"]["wave_id"]
        )

    def test_missing_explicit_hypotheses_are_rejected(self) -> None:
        reconstruction = report(
            [wave(1, [target(MOB_A, "Mob A", kill_budget=None, first_index=1)], stacked=False)]
        )
        with self.assertRaisesRegex(ScenarioCompileError, "armor hypothesis"):
            compile_fury_encounter_scenarios(
                reconstruction,
                base_request(),
                armor_hypotheses=(),
                level_hypotheses=(63,),
            )
        with self.assertRaisesRegex(ScenarioCompileError, "level hypothesis"):
            compile_fury_encounter_scenarios(
                reconstruction,
                base_request(),
                armor_hypotheses=(1_104,),
                level_hypotheses=(),
            )


if __name__ == "__main__":
    unittest.main()
