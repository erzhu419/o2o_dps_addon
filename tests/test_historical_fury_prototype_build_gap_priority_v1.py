from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from o2o_dps import historical_build_catalog_v1 as catalog_v1
from o2o_dps import historical_fury_decision_build_join_v1 as join_v1
from o2o_dps import historical_fury_prototype_build_gap_priority_v1 as gap_v1


def _canonical(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _write_gzip_partition(
    directory: Path,
    name: str,
    rows: list[dict[str, object]],
    record_schema: str,
    *,
    controllable_start_count: int | None = None,
) -> dict[str, object]:
    logical = b"".join(_canonical(row, newline=True) for row in rows)
    path = directory / name
    with path.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as stream:
            stream.write(logical)
    compressed = path.read_bytes()
    descriptor: dict[str, object] = {
        "path": path.name,
        "record_schema": record_schema,
        "record_count": len(rows),
        "compressed_size_bytes": len(compressed),
        "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
        "gzip_mtime": 0,
    }
    if controllable_start_count is not None:
        descriptor["controllable_start_count"] = controllable_start_count
    return descriptor


def _slot(
    inventory_slot: int,
    item_id: int,
    *,
    slot_name: str,
    status: str = catalog_v1.OBSERVED_EQUIPPED,
    item_definition: str = "KNOWN",
    item_effect: str = "NO_SPECIAL_EFFECT",
    enchant_id: int | None = None,
    enchant_definition: str = "KNOWN",
    enchant_effect: str = "IMPLEMENTED",
) -> dict[str, object]:
    if status != catalog_v1.OBSERVED_EQUIPPED:
        return {
            "inventory_slot": inventory_slot,
            "slot_name": slot_name,
            "status": status,
            "item_id": item_id,
            "coverage": None,
        }
    enchants: dict[str, object] = {}
    if enchant_id is not None:
        enchants["permanent"] = {
            "id": enchant_id,
            "definition_status": enchant_definition,
            "effect_status": enchant_effect,
            "calibrated_scopes": [],
        }
    item: dict[str, object] = {
        "definition_status": item_definition,
        "effect_status": item_effect,
        "calibrated_scopes": [],
    }
    if inventory_slot == 16:
        item["weapon_mode"] = "TWO_HAND"
    return {
        "inventory_slot": inventory_slot,
        "slot_name": slot_name,
        "status": status,
        "item_id": item_id,
        "coverage": {"item": item, "enchants": enchants, "gems": []},
    }


def _segment(
    *,
    guid: str,
    segment_id: str,
    item_gap: bool,
) -> dict[str, object]:
    identity = {
        "server": "Capybara",
        "realm": "Basin of Stars",
        "realm_id": "realm-1",
        "player_guid": guid,
        "instance_id": "instance-1",
        "build_segment_id": segment_id,
    }
    if item_gap:
        evaluated = [1, 16, 17]
        reasons = ["ITEM_DEFINITION_NOT_COVERED", "ITEM_EFFECT_NOT_COVERED"]
        primary = _slot(
            1,
            100,
            slot_name="HEAD",
            item_definition="UNKNOWN",
            item_effect="UNKNOWN",
        )
        semantic_ranks = [
            {
                "talent_id": "warrior.flurry",
                "profile_name": "flurry",
                "rank": 5,
                "effect_status": "IMPLEMENTED",
            }
        ]
        missing = ["item:100"]
    else:
        evaluated = [1, 16, 17]
        reasons = ["ENCHANT_NOT_COVERED", "TALENT_EFFECT_NOT_COVERED"]
        primary = _slot(
            1,
            200,
            slot_name="HEAD",
            enchant_id=77,
            enchant_definition="UNKNOWN",
            enchant_effect="UNKNOWN",
        )
        semantic_ranks = [
            {
                "talent_id": "turtle.warrior.testTalent",
                "profile_name": "testTalent",
                "rank": 1,
                "effect_status": "UNSUPPORTED",
            }
        ]
        missing = ["enchant:77", "talent:turtle.warrior.testTalent"]
    slots = [
        primary,
        _slot(16, 300, slot_name="MAIN_HAND"),
        _slot(
            17,
            0,
            slot_name="OFF_HAND",
            status=catalog_v1.OBSERVED_EMPTY,
        ),
        # These observed but cosmetic slots deliberately look unsupported.  They
        # are outside evaluated_inventory_slots and must never become blockers.
        _slot(
            4,
            904,
            slot_name="SHIRT",
            item_definition="UNKNOWN",
            item_effect="UNKNOWN",
        ),
        _slot(
            19,
            919,
            slot_name="TABARD",
            item_definition="UNKNOWN",
            item_effect="UNKNOWN",
        ),
    ]
    return {
        "schema": catalog_v1.RECORD_SCHEMA,
        "identity": identity,
        "observation": {
            "valid_from": {
                "timestamp_ms": 1000 if item_gap else 2000,
                "encounter_id": "encounter-1",
                "event_index": 10 if item_gap else 20,
                "message_ordinal": 1 if item_gap else 2,
            }
        },
        "equipment": {"slots": slots},
        "talents": {
            "original_summary": [{"tree_index": 1, "points": 31}],
            "translation_status": "TRANSLATED_EXACT",
            "semantic_ranks": semantic_ranks,
        },
        "coverage": {
            "runtime_executable": False,
            "representative_build_eligible": True,
            "development_build_eligible": False,
            "simulator_representation": {
                "evaluated_inventory_slots": evaluated,
                "ignored_cosmetic_slots": ["SHIRT", "TABARD"],
                "reasons": reasons,
                "runnable": False,
            },
            "development": {
                "eligible": False,
                "reasons": ["SIMULATOR_REPRESENTATION_NOT_RUNNABLE"],
            },
            "comparison": {
                "eligible": False,
                "reasons": [
                    "NO_DECLARED_TURTLE_CALIBRATION_SCOPE",
                    "SIMULATOR_REPRESENTATION_NOT_RUNNABLE",
                    "TURTLE_CALIBRATION_SCOPE_MISSING",
                ],
            },
            "turtle_calibration": {
                "fully_covered": False,
                "missing_mechanisms": missing,
            },
        },
    }


def _portable_sha(value: object) -> str:
    return hashlib.sha256(_canonical(join_v1._portable_projection(value))).hexdigest()


def _make_fixture(root: Path) -> tuple[Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    catalog_directory = root / "catalog"
    join_directory = root / "join"
    catalog_directory.mkdir()
    join_directory.mkdir()

    segments = [
        _segment(guid="0x0000000000000001", segment_id="segment-0001", item_gap=True),
        _segment(guid="0x0000000000000002", segment_id="segment-0002", item_gap=False),
    ]
    catalog_descriptor = _write_gzip_partition(
        catalog_directory,
        "catalog.jsonl.gz",
        segments,
        catalog_v1.RECORD_SCHEMA,
    )
    catalog_core = {
        "schema": catalog_v1.SCHEMA,
        "implementation_revision": catalog_v1.IMPLEMENTATION_REVISION,
        "kind": "historical_build_catalog_manifest",
        "causal_contract": {
            "policy_join": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY",
            "future_info_backfill_allowed": False,
            "retrospective_valid_until_is_policy_input": False,
            "missing_initial_state_preserved": True,
            "unanchored_info_is_policy_input": False,
        },
    }
    catalog_manifest = {
        **catalog_core,
        # An absolute locator is intentional: the consumer must resolve a
        # relocated colocated basename without putting this path in its identity.
        "catalog_path": str((catalog_directory / catalog_descriptor["path"]).resolve()),
    }
    catalog_manifest_path = catalog_directory / "manifest.json"
    catalog_manifest_path.write_bytes(_canonical(catalog_manifest, newline=True))
    catalog_contract_sha = hashlib.sha256(_canonical(catalog_core)).hexdigest()

    dictionary_rows: list[dict[str, object]] = []
    refs: list[str] = []
    for line_number, (segment, union_support, by_prototype) in enumerate(
        (
            (segments[0], 2, {"p1": 2, "p2": 2}),
            (segments[1], 1, {"p2": 1}),
        ),
        1,
    ):
        source_sha = _portable_sha(segment)
        segment_ref = f"sha256:{source_sha}"
        refs.append(segment_ref)
        dictionary_rows.append(
            {
                "schema": join_v1.SEGMENT_DICTIONARY_SCHEMA,
                "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
                "segment_ref": segment_ref,
                "portable_source_segment_content_sha256": source_sha,
                "catalog_line_number": line_number,
                "identity": deepcopy(segment["identity"]),
                "valid_from": deepcopy(segment["observation"]["valid_from"]),
                "equipment_content_sha256": _portable_sha(segment["equipment"]),
                "talents_content_sha256": _portable_sha(segment["talents"]),
                "semantic_talent_count": 1,
                "semantic_talent_rank_vector_sha256": str(line_number) * 64,
                "talent_translation_status": "TRANSLATED_EXACT",
                "weapon_mode": "TWO_HAND",
                "coverage_flags": {
                    "runtime_executable": False,
                    "representative_build_eligible": True,
                    "development_build_eligible": False,
                    "comparison_eligible": False,
                },
                "decision_support": {
                    "all_source": union_support,
                    "prototype_member_union": union_support,
                    "by_prototype": by_prototype,
                },
            }
        )
    dictionary_descriptor = _write_gzip_partition(
        join_directory,
        "dictionary.jsonl.gz",
        dictionary_rows,
        join_v1.SEGMENT_DICTIONARY_SCHEMA,
    )

    mapping_rows = [
        {
            "schema": join_v1.MAPPING_SCHEMA,
            "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
            "prototype_ids": ["p1", "p2"],
            "controllable_start_count": 2,
            "decision_bindings": [
                {
                    "decision_ordinal": ordinal,
                    "action_key": "warrior.heroic_strike",
                    "join_status": join_v1.JOINED,
                    "segment_ref": refs[0],
                }
                for ordinal in range(2)
            ],
        },
        {
            "schema": join_v1.MAPPING_SCHEMA,
            "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
            "prototype_ids": ["p2"],
            "controllable_start_count": 1,
            "decision_bindings": [
                {
                    "decision_ordinal": 0,
                    "action_key": "warrior.bloodthirst",
                    "join_status": join_v1.JOINED,
                    "segment_ref": refs[1],
                }
            ],
        },
    ]
    mapping_descriptor = _write_gzip_partition(
        join_directory,
        "mapping.jsonl.gz",
        mapping_rows,
        join_v1.MAPPING_SCHEMA,
        controllable_start_count=3,
    )
    catalog_binding = {
        "schema": catalog_v1.SCHEMA,
        "implementation_revision": catalog_v1.IMPLEMENTATION_REVISION,
        "portable_manifest_contract_sha256": catalog_contract_sha,
        "portable_retained_segment_content_sha256": "c" * 64,
        "record_count": 2,
        "retained_exact_player_instance_segment_count": 2,
        "manifest_and_catalog_physical_bytes_verified": True,
        "declared_record_class_and_status_counts_verified": True,
    }
    join_core = {
        "schema": join_v1.MANIFEST_SCHEMA,
        "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
        "status": join_v1.STATUS,
        "input_closure": {"historical_build_catalog": catalog_binding},
        "segment_dictionary": {
            "deduplication_key": "portable source segment SHA-256",
            "lookup": "catalog line plus portable segment content SHA-256",
            "partition": dictionary_descriptor,
        },
        "mapping_partitions": [mapping_descriptor],
        "statistics": {
            "prototype_member_union": {
                "distinct_joined_segment_count": 2,
                "controllable_start_count": 3,
                "action_counts": {
                    "warrior.bloodthirst": 1,
                    "warrior.heroic_strike": 2,
                },
            },
            "by_prototype": {"p1": {}, "p2": {}},
        },
        "portability_contract": {
            "host_absolute_input_locators_stored": False,
            "content_address_host_path_independent": True,
        },
        "scientific_boundaries": {
            "training_authorized": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }
    join_sha = hashlib.sha256(_canonical(join_core)).hexdigest()
    join_manifest = {
        **join_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": join_sha,
        },
    }
    join_manifest_path = join_directory / "manifest.json"
    join_manifest_path.write_bytes(_canonical(join_manifest, newline=True))
    return join_manifest_path, catalog_manifest_path, join_sha


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _readdress(document: dict[str, object]) -> dict[str, object]:
    core = {key: value for key, value in document.items() if key != "content_address"}
    return gap_v1._content_addressed(core)


class HistoricalFuryPrototypeBuildGapPriorityV1Tests(unittest.TestCase):
    def test_compact_priority_uses_union_support_and_ignores_empty_cosmetic_slots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_gap_priority_") as raw:
            root = Path(raw)
            join_manifest, catalog_manifest, join_sha = _make_fixture(root / "inputs")
            result = gap_v1.build_historical_fury_prototype_build_gap_priority_v1(
                join_manifest_path=join_manifest,
                catalog_manifest_path=catalog_manifest,
                output_directory=root / "output",
                candidates_per_prototype=2,
            )
            artifact = _load(result.manifest)
            gap_v1.validate_historical_fury_prototype_build_gap_priority_v1(
                artifact, expected_join_content_sha256=join_sha
            )

            self.assertEqual(3, result.weighted_decision_support)
            blockers = {
                row["blocker_id"]: row for row in artifact["runtime_blocker_priority"]
            }
            self.assertEqual(
                {
                    "ITEM_DEFINITION:100",
                    "ITEM_EFFECT:100",
                    "ENCHANT_DEFINITION:77",
                    "ENCHANT_EFFECT:77",
                    "TALENT_EFFECT:turtle.warrior.testTalent",
                },
                set(blockers),
            )
            self.assertEqual(2, blockers["ITEM_DEFINITION:100"]["weighted_decision_support"])
            self.assertEqual(2, blockers["ITEM_DEFINITION:100"]["distinct_prototype_count"])
            self.assertEqual(
                {"warrior.heroic_strike": 2},
                blockers["ITEM_DEFINITION:100"]["action_coverage_upper_bound"],
            )
            self.assertNotIn("ITEM_DEFINITION:904", blockers)
            self.assertNotIn("ITEM_DEFINITION:919", blockers)
            self.assertNotIn("ITEM_DEFINITION:0", blockers)
            self.assertEqual(
                "COMPARISON_ONLY_NOT_A_DEVELOPMENT_BLOCKER",
                artifact["comparison_calibration"]["classification"],
            )
            self.assertEqual([], artifact["development_only_constraints"])
            self.assertEqual({"p1", "p2"}, set(artifact["prototype_candidate_selection"]))
            for source in artifact["source_segment_candidates"]:
                self.assertNotIn("equipment", source)
                self.assertNotIn("semantic_ranks", source)
                self.assertIn("catalog_line_number", source)
                self.assertIn("runtime_blocker_ids", source)
            self.assertFalse(artifact["scientific_boundaries"]["training_authorized"])
            self.assertFalse(artifact["scientific_boundaries"]["comparison_authorized"])
            self.assertFalse(artifact["scientific_boundaries"]["deployment_authorized"])
            self.assertEqual(result.manifest.read_bytes(), result.content_addressed_manifest.read_bytes())

    def test_content_identity_survives_input_relocation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_gap_relocate_") as raw:
            root = Path(raw)
            first_inputs = root / "first" / "inputs"
            join_manifest, catalog_manifest, _ = _make_fixture(first_inputs)
            first = gap_v1.build_historical_fury_prototype_build_gap_priority_v1(
                join_manifest_path=join_manifest,
                catalog_manifest_path=catalog_manifest,
                output_directory=root / "first" / "output",
            )
            second_inputs = root / "second" / "inputs"
            shutil.copytree(first_inputs, second_inputs)
            second = gap_v1.build_historical_fury_prototype_build_gap_priority_v1(
                join_manifest_path=second_inputs / "join" / "manifest.json",
                catalog_manifest_path=second_inputs / "catalog" / "manifest.json",
                output_directory=root / "second" / "output",
            )

            self.assertEqual(first.content_sha256, second.content_sha256)
            self.assertEqual(first.manifest.read_bytes(), second.manifest.read_bytes())
            self.assertNotIn(str(root), first.manifest.read_text(encoding="utf-8"))

    def test_validator_rejects_unaddressed_tamper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_gap_tamper_") as raw:
            root = Path(raw)
            join_manifest, catalog_manifest, join_sha = _make_fixture(root / "inputs")
            result = gap_v1.build_historical_fury_prototype_build_gap_priority_v1(
                join_manifest_path=join_manifest,
                catalog_manifest_path=catalog_manifest,
                output_directory=root / "output",
            )
            artifact = _load(result.manifest)
            artifact["runtime_blocker_priority"][0]["weighted_decision_support"] += 1

            with self.assertRaisesRegex(
                gap_v1.HistoricalFuryPrototypeBuildGapPriorityV1Error,
                "content address differs",
            ):
                gap_v1.validate_historical_fury_prototype_build_gap_priority_v1(
                    artifact, expected_join_content_sha256=join_sha
                )

    def test_validator_rejects_readdressed_action_accounting_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_gap_accounting_") as raw:
            root = Path(raw)
            join_manifest, catalog_manifest, join_sha = _make_fixture(root / "inputs")
            result = gap_v1.build_historical_fury_prototype_build_gap_priority_v1(
                join_manifest_path=join_manifest,
                catalog_manifest_path=catalog_manifest,
                output_directory=root / "output",
            )
            artifact = _load(result.manifest)
            actions = artifact["runtime_blocker_priority"][0][
                "action_coverage_upper_bound"
            ]
            action = next(iter(actions))
            actions[action] += 1
            artifact = _readdress(artifact)

            with self.assertRaisesRegex(
                gap_v1.HistoricalFuryPrototypeBuildGapPriorityV1Error,
                "blocker action support does not close",
            ):
                gap_v1.validate_historical_fury_prototype_build_gap_priority_v1(
                    artifact, expected_join_content_sha256=join_sha
                )


if __name__ == "__main__":
    unittest.main()
