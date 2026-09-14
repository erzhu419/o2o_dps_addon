from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.historical_build_catalog_v1 import (
    COVERAGE_SCHEMA,
    _coverage_entry,
    _segment_runtime_coverage,
    _talent_state,
)
from o2o_dps.wowsims_mechanics_coverage_registry_v1 import (
    build_registry,
    scan_historical_catalog,
    write_registry,
)
from o2o_dps.wowsims_profile import WARRIOR_TALENT_FIELDS


def _talent_tree() -> list[dict[str, object]]:
    return [
        {
            "name": f"Tree {tree_index}",
            "talents": [
                {
                    "fieldName": field_name,
                    "location": {"rowIdx": position // 4, "colIdx": position % 4},
                    "maxPoints": 5,
                }
                for position, field_name in enumerate(fields)
            ],
        }
        for tree_index, fields in enumerate(WARRIOR_TALENT_FIELDS)
    ]


def _catalog_row() -> dict[str, object]:
    slots: list[dict[str, object]] = []
    item_ids = [100, 101, 102, 999, 22798, 104]
    enchant_ids = [10, 11, 12]
    for index, item_id in enumerate(item_ids):
        slots.append(
            {
                "inventory_slot": index + 1,
                "status": "OBSERVED_EQUIPPED",
                "item_id": item_id,
                "permanent_enchant_id": enchant_ids[index] if index < 3 else None,
                "temporary_enchant_id": None,
                "gem_enchant_ids": [],
            }
        )
    return {
        "schema": "historical_build_segment/v1",
        "player": {"hero_class": "WARRIOR"},
        "version": {"client_build": "7272"},
        "equipment": {"slots": slots},
        "talents": {
            "original_tree_rank_strings": [
                "1" + "0" * 17,
                "0" * 17,
                "0" * 17 + "10",
            ]
        },
    }


def _pin_fixture_warrior_position_map(
    registry: dict[str, object], *, client_build: str = "7272"
) -> dict[str, object]:
    pinned = json.loads(json.dumps(registry))
    fields = pinned["simulator_talent_field_coverage"]["WARRIOR"]["tree_fields"]
    translation = pinned["talent_translations"]["WARRIOR"]
    translation["client_build_position_maps"] = {
        client_build: {
            "tree_fields": fields,
            "zero_only_unsupported_tail_may_be_trimmed": True,
        }
    }
    translation["definition_status"] = "PINNED"
    translation["translation_reason"] = None
    return pinned


class MechanicsCoverageRegistryTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        wowsims = root / "wowsims-turtle"
        database_path = wowsims / "assets" / "database" / "db.json"
        tree_path = wowsims / "ui" / "core" / "talents" / "trees" / "warrior.json"
        common = wowsims / "sim" / "common"
        warrior = wowsims / "sim" / "warrior"
        (common / "item_effects").mkdir(parents=True)
        warrior.mkdir(parents=True)
        database_path.parent.mkdir(parents=True)
        tree_path.parent.mkdir(parents=True)
        database_path.write_text(
            json.dumps(
                {
                    "items": [
                        {"id": 100, "name": "Static 2H", "handType": 4, "stats": [1]},
                        {
                            "id": 101,
                            "name": "Registered proc",
                            "handType": 2,
                            "effects": [{"spellId": 1}],
                            "hasImplementedEffects": True,
                        },
                        {
                            "id": 102,
                            "name": "Unimplemented proc",
                            "effects": [{"spellId": 2}],
                        },
                        {
                            "id": 103,
                            "name": "Registered but not historical proc",
                            "effects": [{"spellId": 3}],
                            "hasImplementedEffects": True,
                        },
                        {
                            "id": 104,
                            "name": "Static shield",
                            "handType": 3,
                            "stats": [1],
                        },
                        {
                            "id": 22798,
                            "name": "Might of Menethil",
                            "handType": 4,
                            "effects": [{"spellId": 51136}],
                        },
                    ],
                    "enchants": [
                        {"effectId": 10, "name": "Static strength", "stats": [0, 4]},
                        {"effectId": 11, "name": "Dynamic proc"},
                        {"effectId": 12, "name": "Unknown empty enchant"},
                        {"effectId": 13, "name": "Registered but not historical enchant"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        tree_path.write_text(json.dumps(_talent_tree()), encoding="utf-8")
        (common / "item_effects.go").write_text(
            """
package common
const Registered = 101
const RegisteredNotHistorical = 103
func init() {
    core.NewItemEffect(Registered, nil)
    core.NewItemEffect(RegisteredNotHistorical, nil)
    // core.NewItemEffect(102, nil)
    /* core.NewItemEffect(999, nil) */
}
""",
            encoding="utf-8",
        )
        (common / "enchant_effects.go").write_text(
            """
package common
func init() {
    core.NewEnchantEffect(11, nil)
    core.NewEnchantEffect(13, nil)
}
""",
            encoding="utf-8",
        )
        (warrior / "items.go").write_text("package warrior\n", encoding="utf-8")
        (warrior / "talents.go").write_text(
            "package warrior\nfunc f() { _ = warrior.Talents.ImprovedHeroicStrike }\n",
            encoding="utf-8",
        )
        catalog = root / "catalog.jsonl.gz"
        with gzip.open(catalog, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps(_catalog_row()) + "\n")
        return wowsims, database_path, catalog

    def test_three_layers_never_turn_unknown_proc_into_passive_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
        self.assertEqual(registry["schema"], COVERAGE_SCHEMA)
        self.assertTrue(registry["items"]["100"]["database_known"])
        self.assertEqual(registry["items"]["100"]["effect_status"], "NO_SPECIAL_EFFECT")
        self.assertEqual(registry["items"]["100"]["weapon_mode"], "TWO_HAND")
        self.assertEqual(registry["items"]["104"]["weapon_mode"], "OFF_HAND_ONLY")
        self.assertEqual(registry["items"]["101"]["effect_status"], "IMPLEMENTED")
        self.assertTrue(registry["items"]["101"]["source_registration_found"])
        self.assertEqual(
            registry["items"]["101"]["simulator_representation"]["status"],
            "RUNNABLE",
        )
        self.assertEqual(
            registry["items"]["101"]["turtle_calibration"]["status"],
            "NOT_CALIBRATED",
        )
        self.assertFalse(
            registry["items"]["101"]["comparison_eligibility"]["eligible"]
        )
        self.assertEqual(registry["items"]["102"]["effect_status"], "UNSUPPORTED")
        self.assertEqual(registry["items"]["22798"]["effect_status"], "UNSUPPORTED")
        self.assertEqual(
            registry["items"]["22798"]["class_effect_status"]["WARRIOR"]
            ["effective_effect_status"],
            "NO_SPECIAL_EFFECT",
        )
        self.assertNotIn(
            22798, registry["summary"]["warrior_priority"]["item_gap_ids"]
        )
        self.assertFalse(registry["items"]["102"]["representation_complete"])
        self.assertEqual(registry["items"]["999"]["effect_status"], "UNKNOWN")
        self.assertFalse(registry["items"]["999"]["database_known"])
        self.assertFalse(registry["coverage_contract"]["unknown_special_effect_defaults_to_passive"])
        self.assertFalse(
            registry["coverage_contract"]["empty_calibrated_scopes_are_comparison_eligible"]
        )
        self.assertEqual(
            registry["request_encoding_contract"]["temporary_enchant_in_item_spec"],
            "NOT_SUPPORTED_BY_BUILD_REQUEST_COMPOSER_V1",
        )

    def test_static_and_registered_enchants_are_separate_from_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )

        self.assertEqual(registry["enchants"]["10"]["effect_status"], "IMPLEMENTED")
        self.assertTrue(registry["enchants"]["10"]["static_stats_implemented"])
        self.assertEqual(registry["enchants"]["11"]["effect_status"], "IMPLEMENTED")
        self.assertTrue(
            registry["enchants"]["11"]["dynamic_source_registration_found"]
        )
        self.assertEqual(registry["enchants"]["12"]["effect_status"], "UNKNOWN")
        self.assertEqual(registry["enchants"]["12"]["calibrated_scopes"], [])

    def test_source_registered_ids_are_targets_without_historical_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )

        item = registry["items"]["103"]
        self.assertEqual(item["historical_segment_occurrences"], 0)
        self.assertEqual(item["effect_status"], "IMPLEMENTED")
        enchant = registry["enchants"]["13"]
        self.assertEqual(enchant["historical_segment_occurrences"], 0)
        self.assertEqual(enchant["effect_status"], "IMPLEMENTED")
        contract = registry["summary"]["target_id_contract"]
        self.assertEqual(
            contract["item_ids"],
            "HISTORICAL_OBSERVED_UNION_CURRENT_SOURCE_REGISTERED",
        )
        self.assertEqual(contract["source_registered_only_item_id_count"], 1)
        self.assertEqual(contract["source_registered_only_enchant_id_count"], 1)
        self.assertTrue(contract["full_database_is_not_an_output_target"])

    def test_class_scoped_item_effect_does_not_weaken_unscoped_status(self) -> None:
        registry = {
            "items": {
                "22798": {
                    "definition_status": "KNOWN",
                    "effect_status": "UNSUPPORTED",
                    "calibrated_scopes": [],
                    "class_effect_status": {
                        "WARRIOR": {
                            "effective_effect_status": "NO_SPECIAL_EFFECT",
                            "applicability_status": "INAPPLICABLE_SHAPESHIFT_ONLY",
                            "evidence": "spell:51136 requires forms",
                        }
                    },
                }
            }
        }
        warrior = _coverage_entry(
            registry, "items", 22798, hero_class="WARRIOR"
        )
        druid = _coverage_entry(registry, "items", 22798, hero_class="DRUID")
        self.assertEqual(warrior["effect_status"], "NO_SPECIAL_EFFECT")
        self.assertEqual(warrior["unscoped_effect_status"], "UNSUPPORTED")
        self.assertEqual(
            warrior["class_applicability_status"],
            "INAPPLICABLE_SHAPESHIFT_ONLY",
        )
        self.assertEqual(druid["effect_status"], "UNSUPPORTED")

    def test_talent_translation_reports_implementation_and_tail_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )

        warrior = registry["simulator_talent_field_coverage"]["WARRIOR"]
        first = warrior["tree_fields"][0][0]
        second = warrior["tree_fields"][0][1]
        self.assertEqual(first["profile_name"], "improvedHeroicStrike")
        self.assertEqual(first["effect_status"], "IMPLEMENTED")
        self.assertEqual(second["effect_status"], "UNSUPPORTED")
        translation = registry["talent_translations"]["WARRIOR"]
        self.assertEqual(translation["client_build_position_maps"], {})
        self.assertEqual(
            registry["summary"]["warrior_talents"]["observed_client_builds"],
            ["7272"],
        )
        tail = registry["summary"]["warrior_talents"][
            "observed_nonzero_positions_beyond_simulator_tree_lengths"
        ]
        self.assertEqual(tail, [{"tree_index": 2, "position": 17}])

    def test_observed_client_build_is_not_automatically_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
        talents = _talent_state(
            {
                "summary": [1, 0, 0],
                "trees": ["1" + "0" * 17, "0" * 17, "0" * 19],
            },
            hero_class="WARRIOR",
            client_build="7272",
            registry=registry,
        )
        self.assertEqual(talents["translation_status"], "OBSERVED_RAW_UNTRANSLATED")
        self.assertEqual(
            talents["translation_reason"],
            "CLIENT_BUILD_POSITION_MAP_NOT_PINNED",
        )

    def test_zero_only_new_turtle_tail_is_losslessly_trimmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
            registry = _pin_fixture_warrior_position_map(registry)

        talents = _talent_state(
            {
                "summary": [1, 0, 0],
                "trees": ["1" + "0" * 17, "0" * 17, "0" * 19],
            },
            hero_class="WARRIOR",
            client_build="7272",
            registry=registry,
        )
        self.assertEqual(talents["translation_status"], "TRANSLATED_EXACT")
        self.assertEqual(
            talents["trimmed_zero_only_unsupported_tail"],
            [
                {
                    "tree_index": 2,
                    "first_unsupported_position": 17,
                    "trimmed_zero_count": 2,
                }
            ],
        )

    def test_nonzero_unsupported_tail_and_unimplemented_talent_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
            registry = _pin_fixture_warrior_position_map(registry)

        tail = _talent_state(
            {
                "summary": [0, 0, 1],
                "trees": ["0" * 18, "0" * 17, "0" * 17 + "10"],
            },
            hero_class="WARRIOR",
            client_build="7272",
            registry=registry,
        )
        self.assertEqual(tail["translation_status"], "OBSERVED_RAW_UNTRANSLATED")
        self.assertEqual(tail["translation_reason"], "NONZERO_UNSUPPORTED_TALENT_POSITION")

        unsupported = _talent_state(
            {
                "summary": [1, 0, 0],
                "trees": ["01" + "0" * 16, "0" * 17, "0" * 19],
            },
            hero_class="WARRIOR",
            client_build="7272",
            registry=registry,
        )
        self.assertEqual(unsupported["translation_status"], "TRANSLATED_EXACT")
        coverage = _segment_runtime_coverage({"slots": []}, unsupported)
        self.assertFalse(coverage["runtime_executable"])
        self.assertIn("TALENT_EFFECT_NOT_COVERED", coverage["uncertainty"])

    def test_rank_above_pinned_max_rank_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
            registry = _pin_fixture_warrior_position_map(registry)

        talents = _talent_state(
            {
                "summary": [6, 0, 0],
                "trees": ["6" + "0" * 17, "0" * 17, "0" * 19],
            },
            hero_class="WARRIOR",
            client_build="7272",
            registry=registry,
        )
        self.assertEqual(talents["translation_status"], "OBSERVED_RAW_UNTRANSLATED")
        self.assertEqual(
            talents["translation_reason"],
            "TALENT_RANK_EXCEEDS_PINNED_MAX_RANK",
        )
        self.assertEqual(
            talents["rank_limit_violations"],
            [
                {
                    "tree_index": 0,
                    "position": 0,
                    "talent_id": "warrior.improvedHeroicStrike",
                    "rank": 6,
                    "max_rank": 5,
                }
            ],
        )

    def test_explicit_position_map_not_simulator_field_order_controls_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
            registry = _pin_fixture_warrior_position_map(registry)
            fields = registry["talent_translations"]["WARRIOR"][
                "client_build_position_maps"
            ]["7272"]["tree_fields"]
            fields[0][0], fields[0][1] = fields[0][1], fields[0][0]

        talents = _talent_state(
            {
                "summary": [1, 0, 0],
                "trees": ["1" + "0" * 17, "0" * 17, "0" * 19],
            },
            hero_class="WARRIOR",
            client_build="7272",
            registry=registry,
        )
        self.assertEqual(talents["translation_status"], "TRANSLATED_EXACT")
        self.assertEqual(
            talents["semantic_ranks"][0]["talent_id"], "warrior.deflection"
        )

    def test_runtime_representation_and_comparison_eligibility_are_not_conflated(self) -> None:
        item = {
            "definition_status": "KNOWN",
            "effect_status": "NO_SPECIAL_EFFECT",
            "calibrated_scopes": [],
        }
        slot = {
            "status": "OBSERVED_EQUIPPED",
            "item_id": 100,
            "temporary_enchant_id": None,
            "coverage": {"item": item, "enchants": {}, "gems": []},
        }
        talents = {
            "translation_status": "TRANSLATED_EXACT",
            "semantic_ranks": [],
        }
        coverage = _segment_runtime_coverage({"slots": [slot]}, talents)
        self.assertTrue(coverage["runtime_executable"])
        self.assertTrue(coverage["simulator_representation"]["runnable"])
        self.assertFalse(coverage["comparison"]["eligible"])
        self.assertIn(
            "TURTLE_CALIBRATION_SCOPE_MISSING", coverage["comparison"]["reasons"]
        )

    def test_temporary_enchant_is_not_runnable_until_composer_encodes_it(self) -> None:
        slot = {
            "status": "OBSERVED_EQUIPPED",
            "item_id": 100,
            "temporary_enchant_id": 11,
            "coverage": {
                "item": {
                    "definition_status": "KNOWN",
                    "effect_status": "NO_SPECIAL_EFFECT",
                    "calibrated_scopes": ["fixture"],
                },
                "enchants": {
                    "temporary": {
                        "id": 11,
                        "definition_status": "KNOWN",
                        "effect_status": "IMPLEMENTED",
                        "calibrated_scopes": ["fixture"],
                    }
                },
                "gems": [],
            },
        }
        coverage = _segment_runtime_coverage(
            {"slots": [slot]},
            {"translation_status": "TRANSLATED_EXACT", "semantic_ranks": []},
        )
        self.assertFalse(coverage["runtime_executable"])
        self.assertIn(
            "TEMPORARY_ENCHANT_ENCODING_NOT_SUPPORTED_BY_COMPOSER",
            coverage["uncertainty"],
        )

    def test_gem_enchant_is_not_runnable_until_composer_encodes_it(self) -> None:
        slot = {
            "status": "OBSERVED_EQUIPPED",
            "item_id": 100,
            "temporary_enchant_id": None,
            "coverage": {
                "item": {
                    "definition_status": "KNOWN",
                    "effect_status": "NO_SPECIAL_EFFECT",
                    "calibrated_scopes": ["fixture"],
                },
                "enchants": {},
                "gems": [
                    {
                        "id": 12,
                        "definition_status": "KNOWN",
                        "effect_status": "IMPLEMENTED",
                        "calibrated_scopes": ["fixture"],
                    }
                ],
            },
        }
        coverage = _segment_runtime_coverage(
            {"slots": [slot]},
            {"translation_status": "TRANSLATED_EXACT", "semantic_ranks": []},
        )
        self.assertFalse(coverage["runtime_executable"])
        self.assertIn(
            "GEM_ENCHANT_ENCODING_NOT_SUPPORTED_BY_COMPOSER",
            coverage["uncertainty"],
        )

    def test_catalog_contract_can_consume_generated_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database, catalog = self._fixture(root)
            output = root / "registry.json"
            registry = write_registry(
                output_path=output,
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=catalog,
            )
            loaded = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(loaded["schema"], COVERAGE_SCHEMA)
        self.assertEqual(
            _coverage_entry(registry, "items", 101)["effect_status"], "IMPLEMENTED"
        )
        self.assertEqual(
            _coverage_entry(registry, "enchants", 12)["effect_status"], "UNKNOWN"
        )

    def test_streaming_catalog_keeps_only_bounded_observation_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, catalog = self._fixture(root)
            observation = scan_historical_catalog(catalog)

        self.assertEqual(observation["segment_count"], 1)
        self.assertEqual(observation["warrior_item_counts"][101], 1)
        self.assertEqual(observation["warrior_enchant_counts"][11], 1)
        self.assertEqual(observation["warrior_tree_shapes"][(18, 17, 19)], 1)


if __name__ == "__main__":
    unittest.main()
