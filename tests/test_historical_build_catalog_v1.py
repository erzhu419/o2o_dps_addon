from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.historical_build_catalog_v1 import (
    AMBIGUOUS,
    COVERAGE_SCHEMA,
    MISSING,
    OBSERVED_EMPTY,
    OBSERVED_EQUIPPED,
    InstanceBuildInput,
    build_catalog_from_instances,
    compile_instance_segments,
    evaluate_prefix_joins,
    historical_segment_to_character_profile,
    iter_admitted_instance_inputs,
    select_prefix_segment,
)
from tests.test_chronicle_combatant_sidecar import _combatant, _plain_frame, _stream


GUID = "0x0000000000000001"
MISSING_GUID = "0x0000000000000002"
UNANCHORED_GUID = "0x0000000000000003"


def _gear(
    item_id: int,
    *,
    complete: bool = True,
    representative: bool = False,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    count = 19 if complete else 2
    for index in range(count):
        inventory_slot = index + 1
        equipped = index == 15 or (
            representative and inventory_slot not in {4, 17, 19}
        )
        rows.append(
            {
                "slot_index": index,
                "item_id": item_id if equipped else 0,
                "enchant_id": 10 if index == 15 else None,
                "temporary_enchant_id": None,
                "gem_enchant_ids": [],
            }
        )
    return rows


def _record(
    *,
    guid: str = GUID,
    timestamp_ms: int | None,
    event_index: int,
    ordinal: int,
    item_id: int,
    complete: bool = True,
    representative: bool = False,
) -> dict[str, object]:
    return {
        "schema": "chronicle_combatant_info/v1",
        "instance_ref": "instance-1",
        "slug": "slug-1",
        "encounter_id": "encounter-1",
        "message_ordinal": ordinal,
        "anchor": (
            {
                "timestamp_ms": timestamp_ms,
                "event_index": event_index,
                "offset_ms": timestamp_ms - 1_000,
                "is_synthetic": False,
            }
            if timestamp_ms is not None
            else None
        ),
        "player": {
            "guid": guid,
            "name": "Fury",
            "hero_class": "WARRIOR",
            "race": "Orc",
            "gender": 2,
            "guild_name": "Guild",
        },
        "gear": _gear(
            item_id,
            complete=complete,
            representative=representative,
        ),
        "talents": {"summary": [3, 0], "trees": ["12", "0"]},
    }


def _instance(records: tuple[dict[str, object], ...]) -> InstanceBuildInput:
    return InstanceBuildInput(
        server="Capybara",
        realm="Basin of Stars",
        realm_id="realm-1",
        instance_id="instance-1",
        instance_name="Upper Tower of Karazhan",
        slug="slug-1",
        metadata_versions={"wow_build": "7272", "wow_client": "1.18.1"},
        game_flavor="vanilla",
        game_format="1.12a-cc-addon",
        players=(
            {
                "guid": GUID,
                "metadata": {
                    "name": "Fury",
                    "class": "WARRIOR",
                    "race": "Orc",
                    "level": 60,
                },
            },
            {
                "guid": MISSING_GUID,
                "metadata": {
                    "name": "No Info",
                    "class": "WARRIOR",
                    "race": "Human",
                    "level": 60,
                },
            },
            {
                "guid": UNANCHORED_GUID,
                "metadata": {
                    "name": "Unanchored",
                    "class": "WARRIOR",
                    "race": "Human",
                    "level": 60,
                },
            },
        ),
        records=records,
        source={"admission_status": "ADMITTED_FOR_VERSIONED_RECONSTRUCTION_INPUT"},
    )


def _coverage() -> dict[str, object]:
    return {
        "schema": COVERAGE_SCHEMA,
        "item_dataset": "fixture-items-v1",
        "items": {
            "100": {
                "definition_status": "KNOWN",
                "effect_status": "NO_SPECIAL_EFFECT",
                "calibrated_scopes": ["fixture"],
                "weapon_mode": "TWO_HAND",
            },
            "200": {
                "definition_status": "KNOWN",
                "effect_status": "IMPLEMENTED",
                "calibrated_scopes": ["fixture"],
                "weapon_mode": "ONE_HAND",
            },
        },
        "enchants": {
            "10": {
                "definition_status": "KNOWN",
                "effect_status": "IMPLEMENTED",
                "calibrated_scopes": ["fixture"],
            }
        },
        "talent_translations": {
            "WARRIOR": {
                "translation_version": "fixture-warrior-talents-v1",
                "client_build_position_maps": {
                    "7272": {
                        "tree_fields": [
                            [
                                {
                                    "talent_id": "talent.a",
                                    "profile_name": "Improved Heroic Strike",
                                    "max_rank": 3,
                                },
                                {
                                    "talent_id": "talent.b",
                                    "profile_name": "Deflection",
                                    "max_rank": 5,
                                },
                            ],
                            [
                                {
                                    "talent_id": "talent.c",
                                    "profile_name": "Booming Voice",
                                    "max_rank": 5,
                                }
                            ],
                        ]
                    }
                },
            }
        },
    }


class HistoricalBuildCatalogV1Tests(unittest.TestCase):
    def test_identical_messages_merge_and_build_change_creates_segment(self) -> None:
        records = (
            _record(timestamp_ms=1_100, event_index=10, ordinal=0, item_id=100),
            _record(timestamp_ms=1_200, event_index=20, ordinal=1, item_id=100),
            _record(timestamp_ms=1_300, event_index=30, ordinal=2, item_id=200),
            _record(
                guid=UNANCHORED_GUID,
                timestamp_ms=None,
                event_index=0,
                ordinal=3,
                item_id=300,
            ),
        )
        segments = compile_instance_segments(
            _instance(records), coverage_registry=_coverage()
        )
        observed = [
            row
            for row in segments
            if row["identity"]["player_guid"] == GUID
        ]
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0]["identity"]["build_segment_id"], "segment-0001")
        self.assertEqual(observed[0]["observation"]["source_message_count"], 2)
        self.assertEqual(
            observed[0]["observation"]["duplicate_identical_message_count"], 1
        )
        self.assertEqual(
            observed[0]["observation"]["valid_until_or_unknown"]["event_index"],
            30,
        )
        self.assertEqual(
            observed[0]["observation"]["valid_until_semantics"],
            "RETROSPECTIVE_NEXT_INFO_BOUNDARY_NOT_POLICY_INPUT",
        )
        main_hand = observed[0]["equipment"]["slots"][15]
        off_hand = observed[0]["equipment"]["slots"][16]
        self.assertEqual(main_hand["status"], OBSERVED_EQUIPPED)
        self.assertEqual(main_hand["item_id"], 100)
        self.assertEqual(off_hand["status"], OBSERVED_EMPTY)
        self.assertEqual(
            observed[0]["talents"]["semantic_ranks"],
            [
                {
                    "talent_id": "talent.a",
                    "profile_name": "Improved Heroic Strike",
                    "max_rank": 3,
                    "rank": 1,
                    "tree_index": 0,
                    "position": 0,
                },
                {
                    "talent_id": "talent.b",
                    "profile_name": "Deflection",
                    "max_rank": 5,
                    "rank": 2,
                    "tree_index": 0,
                    "position": 1,
                },
            ],
        )
        self.assertTrue(observed[0]["coverage"]["runtime_executable"])
        self.assertFalse(observed[0]["coverage"]["representative_build_eligible"])
        self.assertFalse(observed[0]["coverage"]["development_build_eligible"])

        missing = next(
            row for row in segments if row["identity"]["player_guid"] == MISSING_GUID
        )
        self.assertEqual(missing["observation"]["status"], "MISSING_COMBATANT_INFO")
        self.assertTrue(
            all(slot["status"] == MISSING for slot in missing["equipment"]["slots"])
        )
        unanchored = next(
            row
            for row in segments
            if row["identity"]["player_guid"] == UNANCHORED_GUID
        )
        self.assertEqual(
            unanchored["observation"]["status"],
            "AMBIGUOUS_UNANCHORED_COMBATANT_INFO",
        )
        self.assertFalse(unanchored["coverage"]["runtime_executable"])

    def test_partial_and_contradictory_slot_evidence_are_not_empty(self) -> None:
        record = _record(
            timestamp_ms=1_100,
            event_index=10,
            ordinal=0,
            item_id=100,
            complete=False,
        )
        record["gear"][0] = {
            "slot_index": 0,
            "item_id": 0,
            "enchant_id": 10,
            "temporary_enchant_id": None,
            "gem_enchant_ids": [],
        }
        segment = next(
            row
            for row in compile_instance_segments(_instance((record,)))
            if row["identity"]["player_guid"] == GUID
        )
        self.assertEqual(segment["equipment"]["slots"][0]["status"], AMBIGUOUS)
        self.assertEqual(segment["equipment"]["slots"][2]["status"], MISSING)
        self.assertNotEqual(segment["equipment"]["slots"][2]["status"], OBSERVED_EMPTY)

    def test_missing_cosmetic_slots_do_not_block_representative_runtime(self) -> None:
        record = _record(
            timestamp_ms=1_100,
            event_index=10,
            ordinal=0,
            item_id=100,
            representative=True,
        )
        record["gear"] = [
            slot for slot in record["gear"] if slot["slot_index"] not in {3, 18}
        ]
        segment = next(
            row
            for row in compile_instance_segments(
                _instance((record,)), coverage_registry=_coverage()
            )
            if row["identity"]["player_guid"] == GUID
        )
        self.assertEqual(segment["equipment"]["slots"][3]["status"], MISSING)
        self.assertEqual(segment["equipment"]["slots"][18]["status"], MISSING)
        self.assertTrue(segment["coverage"]["runtime_executable"])
        self.assertTrue(segment["coverage"]["representative_build_eligible"])
        simulator = segment["coverage"]["simulator_representation"]
        self.assertEqual(simulator["ignored_cosmetic_slots"], ["SHIRT", "TABARD"])

    def test_unknown_tabard_item_is_data_evidence_not_a_simulator_blocker(self) -> None:
        record = _record(
            timestamp_ms=1_100,
            event_index=10,
            ordinal=0,
            item_id=100,
            representative=True,
        )
        record["gear"][18]["item_id"] = 999
        segment = next(
            row
            for row in compile_instance_segments(
                _instance((record,)), coverage_registry=_coverage()
            )
            if row["identity"]["player_guid"] == GUID
        )
        tabard = segment["equipment"]["slots"][18]
        self.assertEqual(tabard["coverage"]["item"]["definition_status"], "UNKNOWN_TO_REGISTRY")
        self.assertTrue(segment["coverage"]["runtime_executable"])
        self.assertTrue(segment["coverage"]["representative_build_eligible"])

    def test_prefix_lookup_never_uses_future_info(self) -> None:
        records = (
            _record(timestamp_ms=1_100, event_index=10, ordinal=0, item_id=100),
            _record(timestamp_ms=1_300, event_index=30, ordinal=1, item_id=200),
        )
        segments = [
            row
            for row in compile_instance_segments(
                _instance(records), coverage_registry=_coverage()
            )
            if row["identity"]["player_guid"] == GUID
        ]

        def select(timestamp: int, event_index: int):
            return select_prefix_segment(
                segments,
                server="Capybara",
                realm="Basin of Stars",
                player_guid=GUID,
                instance_id="instance-1",
                decision_timestamp_ms=timestamp,
                encounter_id="encounter-1",
                decision_event_index=event_index,
            )

        self.assertIsNone(select(1_000, 999))
        self.assertIsNone(select(1_100, 9))
        self.assertEqual(select(1_100, 10)["equipment"]["slots"][15]["item_id"], 100)
        self.assertEqual(select(1_300, 29)["equipment"]["slots"][15]["item_id"], 100)
        self.assertEqual(select(1_300, 30)["equipment"]["slots"][15]["item_id"], 200)

        stats = evaluate_prefix_joins(
            segments,
            (
                {
                    "server": "Capybara",
                    "realm": "Basin of Stars",
                    "player_guid": GUID,
                    "instance_id": "instance-1",
                    "decision_timestamp_ms": 1_000,
                    "encounter_id": "encounter-1",
                    "decision_event_index": 999,
                },
                {
                    "server": "Capybara",
                    "realm": "Basin of Stars",
                    "player_guid": GUID,
                    "instance_id": "instance-1",
                    "decision_timestamp_ms": 1_200,
                    "encounter_id": "encounter-1",
                    "decision_event_index": 20,
                },
            ),
        )
        self.assertEqual(stats["joined_count"], 1)
        self.assertEqual(stats["missing_no_prior_info_count"], 1)
        self.assertEqual(stats["future_info_backfill_count"], 0)

    def test_streaming_builder_writes_catalogue_and_coverage_summary(self) -> None:
        records = (
            _record(timestamp_ms=1_100, event_index=10, ordinal=0, item_id=100),
            _record(timestamp_ms=1_200, event_index=20, ordinal=1, item_id=100),
            _record(timestamp_ms=1_300, event_index=30, ordinal=2, item_id=200),
            _record(
                guid=UNANCHORED_GUID,
                timestamp_ms=None,
                event_index=0,
                ordinal=3,
                item_id=300,
            ),
        )
        query = {
            "server": "Capybara",
            "realm": "Basin of Stars",
            "player_guid": GUID,
            "instance_id": "instance-1",
            "decision_timestamp_ms": 1_250,
            "encounter_id": "encounter-1",
            "decision_event_index": 25,
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = build_catalog_from_instances(
                (_instance(records),),
                output_directory=temporary,
                coverage_registry=_coverage(),
                prefix_queries=(query,),
            )
            self.assertEqual(result["summary"]["instance_count"], 1)
            self.assertEqual(result["summary"]["unique_player_count"], 3)
            self.assertEqual(result["summary"]["build_segment_count"], 4)
            self.assertEqual(
                result["summary"]["source_combatant_info_message_count"], 4
            )
            self.assertEqual(
                result["summary"]["unique_equipment_signature_count"], 2
            )
            self.assertEqual(
                result["summary"]["unique_fully_observed_equipment_signature_count"],
                2,
            )
            self.assertEqual(result["summary"]["representative_build_segment_count"], 0)
            self.assertEqual(result["summary"]["development_build_segment_count"], 0)
            self.assertEqual(
                result["summary"]["strict_prefix_join"]["rate"], 1.0
            )
            self.assertEqual(
                result["summary"]["legal_empty_offhand"]["evaluated_count"], 2
            )
            self.assertEqual(result["summary"]["legal_empty_offhand"]["legal_count"], 1)
            warrior = result["summary"]["by_hero_class"]["WARRIOR"]
            self.assertEqual(warrior["unique_player_count"], 3)
            self.assertEqual(warrior["causal_build_segment_count"], 2)
            self.assertEqual(warrior["unique_equipment_signature_count"], 2)
            self.assertEqual(warrior["unique_talent_signature_count"], 1)
            self.assertEqual(warrior["weapon_mode_counts"]["TWO_HAND"], 1)
            self.assertEqual(
                warrior["weapon_mode_counts"]["ONE_HAND_NO_OFFHAND"], 1
            )
            self.assertEqual(
                warrior["semantic_talent_combinations"]["unique_signature_count"],
                1,
            )
            with gzip.open(result["catalog_path"], mode="rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(len(rows), 4)
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["inputs"]["coverage_registry_identity"]["schema"],
                COVERAGE_SCHEMA,
            )
            self.assertFalse(manifest["causal_contract"]["future_info_backfill_allowed"])
            self.assertFalse(
                manifest["source_contract"]["large_normalized_event_partitions_opened"]
            )

    def test_explicit_composer_adapter_preserves_historical_provenance(self) -> None:
        segment = next(
            row
            for row in compile_instance_segments(
                _instance(
                    (
                        _record(
                            timestamp_ms=1_100,
                            event_index=10,
                            ordinal=0,
                            item_id=100,
                            representative=True,
                        ),
                    )
                ),
                coverage_registry=_coverage(),
            )
            if row["identity"]["player_guid"] == GUID
        )
        profile = historical_segment_to_character_profile(
            segment,
            catalog_path="historical_catalog.jsonl.gz",
            consumes={},
            database={},
        )
        self.assertEqual(
            profile.selection.selection_mode, "historical_build_segment_prefix"
        )
        self.assertEqual(
            profile.selection.record["event"],
            "HISTORICAL_BUILD_SEGMENT_OBSERVED",
        )
        self.assertNotEqual(
            profile.selection.record["event"], "STATIC_PROFILE_CAPTURED"
        )
        self.assertEqual(profile.slot_statuses[16], OBSERVED_EQUIPPED)
        self.assertEqual(profile.slot_statuses[17], OBSERVED_EMPTY)
        self.assertEqual(
            profile.selection.record["provenance"]["identity"]["player_guid"],
            GUID,
        )
        self.assertEqual(
            [row["name"] for row in profile.selection.record["state"]["talents"]],
            ["Improved Heroic Strike", "Deflection"],
        )
        self.assertTrue(segment["coverage"]["representative_build_eligible"])
        self.assertTrue(segment["coverage"]["development_build_eligible"])
        from o2o_dps.build_request_composer_v1 import compose_build_request_v1
        from tests.test_build_request_composer_v1 import (
            _encounter,
            _execution,
            _objective,
            _raid_context,
        )

        composed = compose_build_request_v1(
            profile, _raid_context(), _encounter(), _execution(), _objective()
        )
        self.assertTrue(composed.admitted)
        selection_source = composed.audit["provenance"]["character_profile"][
            "selection"
        ]
        self.assertEqual(
            selection_source["selection_mode"],
            "historical_build_segment_prefix",
        )
        self.assertEqual(
            selection_source["event"], "HISTORICAL_BUILD_SEGMENT_OBSERVED"
        )

    def test_dual_wield_requires_both_items_to_have_pinned_weapon_semantics(self) -> None:
        record = _record(
            timestamp_ms=1_100,
            event_index=10,
            ordinal=0,
            item_id=200,
        )
        record["gear"][16] = {
            "slot_index": 16,
            "item_id": 200,
            "enchant_id": None,
            "temporary_enchant_id": None,
            "gem_enchant_ids": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = build_catalog_from_instances(
                (_instance((record,)),),
                output_directory=temporary,
                coverage_registry=_coverage(),
            )
        modes = result["summary"]["by_hero_class"]["WARRIOR"][
            "weapon_mode_counts"
        ]
        self.assertEqual(modes["DUAL_WIELD"], 1)
        self.assertEqual(modes["UNKNOWN"], 0)

        unknown_coverage = _coverage()
        del unknown_coverage["items"]["200"]
        with tempfile.TemporaryDirectory() as temporary:
            unknown = build_catalog_from_instances(
                (_instance((record,)),),
                output_directory=temporary,
                coverage_registry=unknown_coverage,
            )
        unknown_modes = unknown["summary"]["by_hero_class"]["WARRIOR"][
            "weapon_mode_counts"
        ]
        self.assertEqual(unknown_modes["DUAL_WIELD"], 0)
        self.assertEqual(unknown_modes["UNKNOWN"], 1)

    def test_admission_adapter_uses_small_verified_objects(self) -> None:
        compressed = _stream(
            _plain_frame(
                [_combatant(7, guid=GUID, item_id=100)],
                first_timestamp_ms=1_000,
            )
        )
        metadata = json.dumps(
            {
                "id": "instance-1",
                "server_name": "Capybara",
                "realm_name": "Basin of Stars",
                "realm_id": "realm-1",
                "name": "Upper Tower of Karazhan",
                "slug": "slug-1",
                "versions": {"wow_build": "7272", "wow_client": "1.18.1"},
                "recorder_guid": GUID,
                "recorder_name": "Fury",
                "flavor": "vanilla",
                "format": "1.12a-cc-addon",
                "players": {
                    GUID: {
                        "name": "Fury",
                        "class": "WARRIOR",
                        "race": "Orc",
                        "level": 60,
                    }
                },
            }
        ).encode("utf-8")
        admission = {
            "instances": [
                {
                    "status": "ADMITTED_FOR_VERSIONED_RECONSTRUCTION_INPUT",
                    "instance_id": "instance-1",
                    "slug": "slug-1",
                    "metadata_player_resolver": {
                        "players": [
                            {
                                "guid": GUID,
                                "metadata": {
                                    "name": "Fury",
                                    "class": "WARRIOR",
                                    "race": "Orc",
                                    "level": 60,
                                },
                            }
                        ]
                    },
                    "combatant_info_evidence": {
                        "object": {"kind": "fixture"},
                        "message_count": 1,
                        "selection_contract": "latest INFO at or before action",
                    },
                    "source_evidence": {
                        "metadata_object": {"kind": "fixture"},
                        "normalized_partition": {"record_count": 999},
                    },
                }
            ]
        }

        def read_object(_root, reference, *, label):
            if label.endswith(".combatant_info"):
                return compressed, Path("combatant.events.gz")
            return metadata, Path("metadata.json")

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "offline_data"
            data_root.mkdir()
            with patch(
                "o2o_dps.historical_build_catalog_v1.load_admission_manifest",
                return_value=(admission, Path("admission.json")),
            ), patch(
                "o2o_dps.historical_build_catalog_v1._read_object_reference",
                side_effect=read_object,
            ):
                instances = list(
                    iter_admitted_instance_inputs(
                        "admission.json", data_root=data_root
                    )
                )
        self.assertEqual(len(instances), 1)
        self.assertEqual(len(instances[0].records), 1)
        self.assertEqual(instances[0].server, "Capybara")
        self.assertEqual(
            instances[0].source["rows_copied_from_large_event_partitions"], 0
        )
        self.assertEqual(
            instances[0].source["chronicle_recorder"],
            {
                "player_guid": GUID,
                "name": "Fury",
                "identity_status": "EXACT_METADATA_RECORDER_GUID",
            },
        )
        self.assertEqual(
            instances[0].source["metadata_versions"]["wow_build"], "7272"
        )
        self.assertEqual(instances[0].source["game_format"], "1.12a-cc-addon")


if __name__ == "__main__":
    unittest.main()
