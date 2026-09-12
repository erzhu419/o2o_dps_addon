from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.turtle_talent_position_map_v1 import (
    ADMISSION_STATUS,
    SCHEMA,
    TurtleTalentPositionMapError,
    build_turtle_talent_position_map,
    build_turtle_talent_position_map_from_files,
    validate_admitted_position_map,
    write_turtle_talent_position_map,
)
from o2o_dps.wowsims_mechanics_coverage_registry_v1 import (
    MechanicsCoverageRegistryError,
    build_registry,
)
from o2o_dps.wowsims_profile import WARRIOR_TALENT_FIELDS


def _definitions() -> list[dict[str, object]]:
    return [
        {
            "tab": 1,
            "index": 1,
            "tier": 1,
            "column": 1,
            "name": "Improved Heroic Strike",
            "rank": 3,
            "maxRank": 3,
        },
        {
            "tab": 1,
            "index": 2,
            "tier": 1,
            "column": 2,
            "name": "Turtle Precision",
            "rank": 0,
            "maxRank": 2,
        },
        {
            "tab": 2,
            "index": 1,
            "tier": 5,
            "column": 1,
            "name": "碾碎",
            "rank": 2,
            "maxRank": 3,
        },
        {
            "tab": 2,
            "index": 2,
            "tier": 1,
            "column": 3,
            "name": "Cruelty",
            "rank": 5,
            "maxRank": 5,
        },
        {
            "tab": 3,
            "index": 1,
            "tier": 2,
            "column": 3,
            "name": "Toughness",
            "rank": 1,
            "maxRank": 5,
        },
    ]


def _static_profile() -> dict[str, object]:
    return {
        "event": "STATIC_PROFILE_CAPTURED",
        "sequence": 7,
        "marker": {"schemaVersion": 2},
        "state": {
            "playerGUID": "0x00000000000000A1",
            "characterIdentity": {
                "name": "MapTester",
                "classFile": "WARRIOR",
            },
            "clientBuild": {"build": "7272", "version": "1.18.1"},
            "talentDefinitions": _definitions(),
            "fieldProvenance": {
                "talentDefinitions": "OBSERVED_FULL_TALENT_TREE_API",
                "clientBuild": "OBSERVED_GETBUILDINFO_API",
            },
        },
    }


def _chronicle() -> dict[str, object]:
    return {
        "schema": "historical_build_segment/v1",
        "player": {"name": "MapTester", "hero_class": "WARRIOR"},
        "identity": {"player_guid": "0x00000000000000A1"},
        "version": {"client_build": "7272"},
        "source": {
            "chronicle_recorder": {
                "player_guid": "0x00000000000000A1",
                "name": "MapTester",
                "identity_status": "EXACT_METADATA_RECORDER_GUID",
            },
            "metadata_versions": {
                "addon": "0.35",
                "chronicle_companion": "0.35",
                "wow_build": "7272",
            },
            "game_format": "1.12a-cc-addon",
        },
        "talents": {
            "original_summary": [3, 7, 1],
            "original_tree_rank_strings": ["30", "25", "1"],
        },
    }


def _simulator_tree() -> list[dict[str, object]]:
    return [
        {
            "name": f"Tree {tree_index}",
            "talents": [
                {
                    "fieldName": field_name,
                    "location": {
                        "rowIdx": position // 4,
                        "colIdx": position % 4,
                    },
                    "maxPoints": 5,
                }
                for position, field_name in enumerate(fields)
            ],
        }
        for tree_index, fields in enumerate(WARRIOR_TALENT_FIELDS)
    ]


def _wowsims_fixture(root: Path) -> tuple[Path, Path]:
    wowsims = root / "wowsims-turtle"
    database = wowsims / "assets" / "database" / "db.json"
    tree = wowsims / "ui" / "core" / "talents" / "trees" / "warrior.json"
    common = wowsims / "sim" / "common"
    warrior = wowsims / "sim" / "warrior"
    (common / "item_effects").mkdir(parents=True)
    warrior.mkdir(parents=True)
    database.parent.mkdir(parents=True)
    tree.parent.mkdir(parents=True)
    database.write_text(json.dumps({"items": [], "enchants": []}), encoding="utf-8")
    tree.write_text(json.dumps(_simulator_tree()), encoding="utf-8")
    (common / "item_effects.go").write_text("package common\n", encoding="utf-8")
    (common / "enchant_effects.go").write_text("package common\n", encoding="utf-8")
    (warrior / "items.go").write_text("package warrior\n", encoding="utf-8")
    (warrior / "talents.go").write_text(
        "package warrior\nfunc f() { _ = warrior.Talents.ImprovedHeroicStrike }\n",
        encoding="utf-8",
    )
    return wowsims, database


class TurtleTalentPositionMapV1Tests(unittest.TestCase):
    def test_exact_alignment_preserves_turtle_only_and_maps_ravager(self) -> None:
        artifact = build_turtle_talent_position_map(
            _static_profile(), _chronicle()
        )

        self.assertEqual(artifact["schema"], SCHEMA)
        self.assertTrue(artifact["admission"])
        self.assertEqual(artifact["admission_status"], ADMISSION_STATUS)
        self.assertEqual(artifact["position_map"]["tree_shapes"], [2, 2, 1])
        fields = artifact["position_map"]["tree_fields"]
        self.assertEqual(fields[0][0]["profile_name"], "improvedHeroicStrike")
        self.assertEqual(
            fields[0][1]["simulator_mapping_status"],
            "UNSUPPORTED_TURTLE_ONLY",
        )
        self.assertEqual(fields[0][1]["client_semantic"]["name"], "Turtle Precision")
        self.assertEqual(fields[1][0]["profile_name"], "ravager")
        self.assertEqual(
            fields[1][0]["simulator_target"], "warrior.options.ravagerRank"
        )
        self.assertEqual(
            artifact["summary"]["simulator_mapping_status_counts"],
            {"TALENT_FIELD": 3, "OPTION_FIELD": 1, "UNSUPPORTED_TURTLE_ONLY": 1},
        )
        self.assertEqual(
            artifact["summary"]["alignment_mode"],
            "PINNED_RECORDER_SELF_CLIENT_POSITION_ORDER",
        )

    def test_continuity_rank_shape_build_and_raw_alignment_are_required(self) -> None:
        cases: list[tuple[str, dict[str, object], dict[str, object], str]] = []

        gap = _static_profile()
        gap["state"]["talentDefinitions"][1]["index"] = 3
        cases.append(("index gap", gap, _chronicle(), "contiguous"))

        invalid_rank = _static_profile()
        invalid_rank["state"]["talentDefinitions"][0]["maxRank"] = 2
        cases.append(("rank over max", invalid_rank, _chronicle(), "0..maxRank"))

        wrong_shape = _chronicle()
        wrong_shape["talents"]["original_tree_rank_strings"][0] = "300"
        cases.append(("shape", _static_profile(), wrong_shape, "tree shape mismatch"))

        wrong_rank = _chronicle()
        wrong_rank["talents"]["original_tree_rank_strings"][1] = "19"
        wrong_rank["talents"]["original_summary"] = [3, 10, 1]
        cases.append(("raw rank", _static_profile(), wrong_rank, "exceeds"))

        wrong_build = _chronicle()
        wrong_build["version"]["client_build"] = "8000"
        cases.append(("build", _static_profile(), wrong_build, "client build mismatch"))

        wrong_guid = _chronicle()
        wrong_guid["identity"]["player_guid"] = "0x00000000000000B2"
        cases.append(("guid", _static_profile(), wrong_guid, "remote-inspection"))

        for label, static, chronicle, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(TurtleTalentPositionMapError, message):
                    build_turtle_talent_position_map(static, chronicle)

    def test_jsonl_selection_and_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static_path = root / "calibration.jsonl"
            static_path.write_text(
                "\n".join(
                    [
                        json.dumps({"event": "OTHER"}),
                        json.dumps(_static_profile(), ensure_ascii=False),
                    ]
                ),
                encoding="utf-8",
            )
            metadata_path = root / "metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "id": "instance-1",
                        "recorder_guid": "0x00000000000000A1",
                        "recorder_name": "MapTester",
                        "format": "1.12a-cc-addon",
                        "versions": {
                            "addon": "0.35",
                            "chronicle_companion": "0.35",
                            "wow_build": "7272",
                        },
                    }
                ),
                encoding="utf-8",
            )
            row = _chronicle()
            row["identity"]["instance_id"] = "instance-1"
            row["source"] = {"metadata_object_path": str(metadata_path)}
            chronicle_path = root / "chronicle.jsonl"
            chronicle_path.write_text(
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            artifact = build_turtle_talent_position_map_from_files(
                static_profile_path=static_path,
                chronicle_evidence_path=chronicle_path,
                expected_player_guid="0x00000000000000A1",
                expected_client_build="7272",
            )
            output = root / "map.json"
            written = write_turtle_talent_position_map(
                artifact, output_path=output
            )

            self.assertEqual(written, output.resolve())
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(loaded["admission"])
            self.assertEqual(loaded["evidence"]["static_profile"]["line"], 2)
            self.assertEqual(
                loaded["evidence"]["chronicle"]["serialization_proof"][
                    "metadata_source_mode"
                ],
                "RAW_METADATA_OBJECT",
            )

    def test_shared_jsonl_filters_character_and_build_before_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other_character = deepcopy(_static_profile())
            other_character["sequence"] = 9000
            other_character["state"]["playerGUID"] = "0x00000000000000B2"
            other_character["state"]["characterIdentity"]["name"] = "OtherWarrior"

            wrong_build = deepcopy(_static_profile())
            wrong_build["sequence"] = 8000
            wrong_build["state"]["clientBuild"]["build"] = "8000"

            expected = deepcopy(_static_profile())
            expected["sequence"] = 1
            static_path = root / "shared-static.jsonl"
            static_path.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False)
                    for row in (other_character, wrong_build, expected)
                )
                + "\n",
                encoding="utf-8",
            )
            chronicle_path = root / "chronicle.jsonl"
            chronicle_path.write_text(
                json.dumps(_chronicle(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            artifact = build_turtle_talent_position_map_from_files(
                static_profile_path=static_path,
                chronicle_evidence_path=chronicle_path,
                expected_player_guid="0x00000000000000A1",
                expected_client_build="7272",
            )

            source = artifact["evidence"]["static_profile"]
            self.assertEqual(source["line"], 3)
            self.assertEqual(
                source["selection_mode"],
                "EXPECTED_PLAYER_GUID_AND_CLIENT_BUILD_LAST_PHYSICAL_LINE",
            )
            self.assertEqual(source["expected_player_guid"], "0x00000000000000A1")
            self.assertEqual(source["expected_client_build"], "7272")

    def test_same_character_update_uses_last_physical_line_not_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            earlier = deepcopy(_static_profile())
            earlier["sequence"] = 500
            later = deepcopy(_static_profile())
            later["sequence"] = 2
            static_path = root / "shared-static.jsonl"
            static_path.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False) for row in (earlier, later)
                )
                + "\n",
                encoding="utf-8",
            )
            chronicle_path = root / "chronicle.jsonl"
            chronicle_path.write_text(
                json.dumps(_chronicle(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            artifact = build_turtle_talent_position_map_from_files(
                static_profile_path=static_path,
                chronicle_evidence_path=chronicle_path,
                expected_player_guid="0x00000000000000A1",
                expected_client_build="7272",
            )

            self.assertEqual(artifact["evidence"]["static_profile"]["line"], 2)

    def test_remote_multi_player_cohort_never_admits_position_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static_path = root / "calibration.jsonl"
            static_path.write_text(
                json.dumps(_static_profile(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            first = _chronicle()
            first["player"]["name"] = "OtherOne"
            first["identity"]["player_guid"] = "0x00000000000000B1"
            second = deepcopy(first)
            second["player"]["name"] = "OtherTwo"
            second["identity"]["player_guid"] = "0x00000000000000B2"
            second["talents"]["original_tree_rank_strings"] = ["20", "15", "0"]
            second["talents"]["original_summary"] = [2, 6, 0]
            chronicle_path = root / "chronicle.jsonl"
            chronicle_path.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False) for row in (first, second)
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                TurtleTalentPositionMapError,
                "remote_inspection_rows_nonadmitting=2",
            ):
                build_turtle_talent_position_map_from_files(
                    static_profile_path=static_path,
                    chronicle_evidence_path=chronicle_path,
                    expected_player_guid="0x00000000000000A1",
                    expected_client_build="7272",
                )

    def test_pinned_other_recorder_admits_but_unsupported_version_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static_path = root / "calibration.jsonl"
            static_path.write_text(json.dumps(_static_profile()) + "\n", encoding="utf-8")
            unrelated = _chronicle()
            unrelated["player"]["name"] = "OtherOne"
            unrelated["identity"]["player_guid"] = "0x00000000000000B1"
            unrelated["source"]["chronicle_recorder"]["player_guid"] = (
                "0x00000000000000B1"
            )
            unrelated["source"]["chronicle_recorder"]["name"] = "OtherOne"
            single_path = root / "single.jsonl"
            single_path.write_text(json.dumps(unrelated) + "\n", encoding="utf-8")
            artifact = build_turtle_talent_position_map_from_files(
                static_profile_path=static_path,
                chronicle_evidence_path=single_path,
                expected_player_guid="0x00000000000000A1",
                expected_client_build="7272",
            )
            self.assertEqual(
                artifact["evidence"]["chronicle"]["serialization_proof"][
                    "recorder_guid"
                ],
                "0x00000000000000B1",
            )
            self.assertFalse(
                artifact["evidence"]["chronicle"]["same_character_as_static_capture"]
            )

            unsupported = deepcopy(unrelated)
            unsupported["source"]["metadata_versions"]["addon"] = "9.9"
            unsupported["source"]["metadata_versions"][
                "chronicle_companion"
            ] = "9.9"
            unsupported_path = root / "unsupported.jsonl"
            unsupported_path.write_text(json.dumps(unsupported) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                TurtleTalentPositionMapError, "not pinned"
            ):
                build_turtle_talent_position_map_from_files(
                    static_profile_path=static_path,
                    chronicle_evidence_path=unsupported_path,
                    expected_player_guid="0x00000000000000A1",
                    expected_client_build="7272",
                )

    def test_registry_merges_only_admitted_unique_build_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wowsims, database = _wowsims_fixture(root)
            artifact = build_turtle_talent_position_map(
                _static_profile(), _chronicle()
            )
            overlay = root / "talent-map.json"
            write_turtle_talent_position_map(artifact, output_path=overlay)
            registry = build_registry(
                database_path=database,
                wowsims_root=wowsims,
                historical_catalog_path=None,
                talent_position_map_paths=[overlay],
            )

            pinned = registry["talent_translations"]["WARRIOR"][
                "client_build_position_maps"
            ]["7272"]["tree_fields"]
            self.assertEqual(pinned[0][0]["effect_status"], "IMPLEMENTED")
            self.assertEqual(pinned[1][0]["effect_status"], "IMPLEMENTED")
            self.assertEqual(
                pinned[1][0]["simulator_target"], "warrior.options.ravagerRank"
            )
            self.assertEqual(pinned[0][1]["effect_status"], "UNSUPPORTED")
            self.assertEqual(
                registry["summary"]["warrior_talents"]["pinned_client_builds"],
                ["7272"],
            )

            with self.assertRaisesRegex(
                MechanicsCoverageRegistryError, "duplicate/conflicting"
            ):
                build_registry(
                    database_path=database,
                    wowsims_root=wowsims,
                    historical_catalog_path=None,
                    talent_position_map_paths=[overlay, overlay],
                )

            rejected = deepcopy(artifact)
            rejected["admission"] = False
            rejected_path = root / "rejected.json"
            rejected_path.write_text(json.dumps(rejected), encoding="utf-8")
            with self.assertRaisesRegex(
                MechanicsCoverageRegistryError, "not admitted"
            ):
                build_registry(
                    database_path=database,
                    wowsims_root=wowsims,
                    historical_catalog_path=None,
                    talent_position_map_paths=[rejected_path],
                )

    def test_admission_validator_rejects_invalid_map_contract(self) -> None:
        artifact = build_turtle_talent_position_map(_static_profile(), _chronicle())
        artifact["position_map"]["zero_only_unsupported_tail_may_be_trimmed"] = "false"
        with self.assertRaisesRegex(TurtleTalentPositionMapError, "must be boolean"):
            validate_admitted_position_map(artifact)

        artifact = build_turtle_talent_position_map(_static_profile(), _chronicle())
        artifact["evidence"]["chronicle"]["serialization_proof"][
            "companion_commit"
        ] = "fabricated"
        with self.assertRaisesRegex(TurtleTalentPositionMapError, "pinned contract"):
            validate_admitted_position_map(artifact)


if __name__ == "__main__":
    unittest.main()
