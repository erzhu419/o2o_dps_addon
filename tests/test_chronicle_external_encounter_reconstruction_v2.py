from __future__ import annotations

import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_external_encounter_reconstruction_v2 import (
    ARTIFACT_SCHEMA,
    ChronicleExternalReconstructionV2Error,
    LANE_HOSTILE_OBJECT,
    LANE_HOSTILE_PLAYER,
    LANE_UNKNOWN,
    SCHEMA,
    STATUS,
    build_external_encounter_reconstruction,
    load_external_reconstruction_manifest,
)
from o2o_dps.chronicle_external_reconstruction_admission_v1 import (
    ChronicleExternalAdmissionError,
    IMPLEMENTATION_REVISION as ADMISSION_IMPLEMENTATION_REVISION,
    SCHEMA as ADMISSION_SCHEMA,
    STATUS as ADMISSION_STATUS,
)


INSTANCE = "instance-1"
INSTANCE_2 = "instance-2"
ENCOUNTER_1 = "encounter-1"
ENCOUNTER_2 = "encounter-2"
PLAYER_1 = "0x00000000000000A1"
PLAYER_2 = "0x00000000000000A2"
MOB_1 = "0xF130000001000001"
MOB_2 = "0xF130000002000002"
OBJECT = "0xF110000003000003"
PLAYER_3 = "0x00000000000000B1"
MOB_3 = "0xF130000004000004"

STREAM_FOR_TYPE = {
    "CLASS": "unit_classification",
    "DMG": "damage",
    "HEAL": "heal",
    "DEAD": "slain",
    "START": "spell_start",
    "GO": "spell_go",
    "FAIL": "spell_fail",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _class_row(
    index: int,
    *,
    encounter: str,
    ordinal: int,
    origin: int,
    target: str,
    unit_type: int,
    affiliation: int,
    owner: str | None = None,
    controller: str | None = None,
) -> dict[str, object]:
    offset = index * 10
    message = {
        "meta": {
            "event_index": index,
            "offset_ms": offset,
            "is_synthetic": False,
            "activity": [],
        },
        "target": target,
        "unit_type": unit_type,
        "affiliation": affiliation,
        "owner": owner,
        "controller": controller,
        "spell_id": 0,
    }
    return _row(
        index,
        encounter=encounter,
        ordinal=ordinal,
        origin=origin,
        event_type="CLASS",
        source_guid=None,
        target_guid=target,
        value=None,
        message=message,
    )


def _event_row(
    index: int,
    *,
    encounter: str,
    ordinal: int,
    origin: int,
    event_type: str,
    source_guid: str | None,
    target_guid: str | None,
    value: int | None,
    nested_attribution: bool = False,
) -> dict[str, object]:
    offset = index * 10
    message: dict[str, object] = {
        "meta": {
            "event_index": index,
            "offset_ms": offset,
            "is_synthetic": False,
            "activity": [],
        }
    }
    if event_type == "DMG":
        message.update(
            {
                "caster": source_guid,
                "target": target_guid,
                "amount": value,
                "hit_type": 1,
            }
        )
    elif event_type == "DEAD":
        message.update(
            {
                "caster": source_guid,
                "target": target_guid,
                "attribution": (
                    {"caster": source_guid, "target": target_guid, "amount": 999}
                    if nested_attribution
                    else None
                ),
            }
        )
    elif event_type == "HEAL":
        message.update(
            {"caster": source_guid, "target": target_guid, "amount": value}
        )
    return _row(
        index,
        encounter=encounter,
        ordinal=ordinal,
        origin=origin,
        event_type=event_type,
        source_guid=source_guid,
        target_guid=target_guid,
        value=value,
        message=message,
    )


def _row(
    index: int,
    *,
    encounter: str,
    ordinal: int,
    origin: int,
    event_type: str,
    source_guid: str | None,
    target_guid: str | None,
    value: int | None,
    message: dict[str, object],
) -> dict[str, object]:
    offset = index * 10
    stream = STREAM_FOR_TYPE[event_type]
    return {
        "schema": "chronicle_external_core_event/v1",
        "instance": INSTANCE,
        "encounter": encounter,
        "encounter_ordinal": ordinal,
        "first_timestamp_ms": origin,
        "event_index": index,
        "offset_ms": offset,
        "timestamp_ms": origin + offset,
        "time": "2026-09-02T00:00:00Z",
        "type": event_type,
        "source": None,
        "source_guid": source_guid,
        "target": None,
        "target_guid": target_guid,
        "spell": None,
        "spell_id": None,
        "value": value,
        "outcome": None,
        "synthetic": False,
        "flags": [],
        "activity": None,
        "official": {
            "stream_type": stream,
            "message_sha256": hashlib.sha256(
                f"{encounter}:{index}:{event_type}".encode()
            ).hexdigest(),
            "message": message,
        },
        "provenance": {
            "format": "chronicle_external_api_core_event_v1",
            "stream_type": stream,
            "frame_index": ordinal,
            "frame_message_index": index,
            "csv_line": index + 1 if ordinal == 0 else 10_000 + index,
            "raw_object_copied": False,
        },
    }


def _default_rows() -> list[dict[str, object]]:
    origin_1 = 1_788_000_000_000
    origin_2 = origin_1 + 60_000
    return [
        _class_row(
            0,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=PLAYER_1,
            unit_type=1,
            affiliation=1,
        ),
        _class_row(
            1,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=PLAYER_2,
            unit_type=1,
            affiliation=1,
        ),
        _class_row(
            2,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=MOB_1,
            unit_type=2,
            affiliation=2,
        ),
        _class_row(
            3,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=OBJECT,
            unit_type=3,
            affiliation=2,
            owner=PLAYER_1[2:],
        ),
        _event_row(
            4,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=PLAYER_1,
            target_guid=MOB_1,
            value=100,
        ),
        _event_row(
            5,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=OBJECT,
            target_guid=MOB_1,
            value=50,
        ),
        _event_row(
            6,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=MOB_1,
            target_guid=PLAYER_1,
            value=20,
        ),
        _event_row(
            7,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=PLAYER_1,
            target_guid=OBJECT,
            value=30,
        ),
        _event_row(
            8,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DEAD",
            source_guid=PLAYER_1,
            target_guid=MOB_1,
            value=None,
            nested_attribution=True,
        ),
        _event_row(
            9,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DEAD",
            source_guid=PLAYER_1,
            target_guid=OBJECT,
            value=None,
        ),
        _class_row(
            10,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=PLAYER_2,
            unit_type=1,
            affiliation=2,
            controller=MOB_1,
        ),
        _event_row(
            11,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=PLAYER_2,
            target_guid=MOB_1,
            value=10,
        ),
        _class_row(
            12,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=PLAYER_2,
            unit_type=1,
            affiliation=1,
        ),
        _class_row(
            0,
            encounter=ENCOUNTER_2,
            ordinal=1,
            origin=origin_2,
            target=MOB_2,
            unit_type=2,
            affiliation=2,
        ),
        _event_row(
            1,
            encounter=ENCOUNTER_2,
            ordinal=1,
            origin=origin_2,
            event_type="DMG",
            source_guid=PLAYER_1,
            target_guid=MOB_2,
            value=40,
        ),
    ]


def _write_fixture(base: Path, rows: list[dict[str, object]]) -> Path:
    data_root = base / "offline_data"
    partition_dir = data_root / "derived" / "chronicle_external_core_events" / "v1"
    partition_dir.mkdir(parents=True)
    logical = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    compressed = gzip.compress(logical, mtime=0)
    logical_sha = _sha256(logical)
    compressed_sha = _sha256(compressed)
    partition = partition_dir / f"{INSTANCE}.{logical_sha}.jsonl.gz"
    partition.write_bytes(compressed)

    players = [
        {
            "guid": PLAYER_1,
            "metadata": {
                "name": "Alice",
                "class": "WARRIOR",
                "race": "Human",
                "level": 60,
            },
        },
        {
            "guid": PLAYER_2,
            "metadata": {
                "name": "Bob",
                "class": "WARRIOR",
                "race": "Orc",
                "level": 60,
            },
        },
    ]
    players.sort(key=lambda row: row["guid"])
    resolver_sha = _sha256(_canonical_bytes(players))
    admission_core = {
        "schema": ADMISSION_SCHEMA,
        "kind": "chronicle_external_reconstruction_admission_manifest",
        "implementation_revision": ADMISSION_IMPLEMENTATION_REVISION,
        "status": ADMISSION_STATUS,
        "admission_validation_contract": {
            "normalized_rows_byte_exact_canonical_rederived": True
        },
        "inputs": {
            "raw_api_manifest": {
                "path": "chronicle_raw/external_api/v1/manifests/raw.json",
                "file_sha256": "1" * 64,
                "size_bytes": 1,
                "schema": "chronicle_external_api_ingest/v1",
            },
            "normalization_manifest": {
                "path": "derived/chronicle_external_core_events/v1/source.json",
                "file_sha256": "2" * 64,
                "content_sha256": "3" * 64,
                "size_bytes": 1,
                "schema": "chronicle_external_core_event_normalization/v1",
            },
        },
        "instances": [
            {
                "status": ADMISSION_STATUS,
                "instance_id": INSTANCE,
                "slug": "slug-1",
                "instance_name": "Upper Tower of Karazhan",
                "source_evidence": {
                    "kind": "external_api_stream_set",
                    "raw_api_manifest_sha256": "1" * 64,
                    "normalized_partition": {
                        "path": partition.relative_to(data_root).as_posix(),
                        "compressed_file_sha256": compressed_sha,
                        "compressed_size_bytes": len(compressed),
                        "logical_content_sha256": logical_sha,
                        "record_count": len(rows),
                        "encounter_count": len({row["encounter"] for row in rows}),
                        "all_rows_exactly_rederived_from_verified_raw_objects": True,
                    },
                },
                "temporal_and_guild_provenance": {
                    "started_at": "2026-09-03T12:48:06.919Z",
                    "started_at_source": "metadata.encounters.min(start_time)",
                    "guild": {"id": "guild-1", "name": "南北"},
                    "guild_evidence": "metadata.guild.name",
                    "contamination": {
                        "label": "POSTFIX_KNOWN_CLEAN",
                        "guild_context": "南北",
                        "guild_evidence": "metadata.guild.name",
                        "time_field": "started_at",
                        "uploaded_at_used": False,
                    },
                },
                "metadata_player_resolver": {
                    "kind": "metadata_exact_player_guid_resolver",
                    "player_count": len(players),
                    "players_sha256": resolver_sha,
                    "players": players,
                    "name_or_class_inference_used": False,
                },
                "combatant_info_evidence": {
                    "status": "VERIFIED_LOCAL_OFFICIAL_STREAM",
                    "message_count": 2,
                },
                "warrior_spec_evidence": {
                    "observation_count": 2,
                    "declared_counts": {"Arms": 0, "Fury": 2, "Other_or_unknown": 0},
                    "recomputed_counts": {"Arms": 0, "Fury": 2, "Other_or_unknown": 0},
                    "field_conflict_observation_count": 0,
                    "conflicts_preserved_not_resolved": True,
                    "observations": [],
                },
                "consumer_status": {
                    "versioned_reconstruction_input": True,
                    "legacy_plain_jsonl_reconstruction_input": False,
                    "legacy_manual_export_queue_entry": False,
                    "legacy_raw_csv_provenance": False,
                    "comparison_authorized": False,
                    "frozen_50_capsule_member": False,
                },
            }
        ],
        "summary": {
            "instance_count": 1,
            "record_count": len(rows),
            "metadata_player_count": len(players),
            "warrior_observation_count": 2,
            "raw_object_copy_count": 0,
            "normalized_row_copy_count": 0,
            "network_request_count": 0,
        },
    }
    content_sha = _sha256(_canonical_bytes(admission_core))
    admission = {
        **admission_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical_JSON_excluding_content_address",
            "sha256": content_sha,
        },
    }
    payload = _canonical_bytes(admission) + b"\n"
    admission_dir = (
        data_root / "derived" / "chronicle_external_reconstruction_admission" / "v1"
    )
    admission_dir.mkdir(parents=True)
    addressed = admission_dir / (
        f"chronicle_external_reconstruction_admission_v1.{content_sha}.manifest.json"
    )
    addressed.write_bytes(payload)
    stable = admission_dir / "manifest.json"
    stable.write_bytes(payload)
    return stable


def _write_two_instance_fixture(
    base: Path, *, cross_join_second_partition: bool = False
) -> Path:
    stable = _write_fixture(base, _default_rows())
    admission = json.loads(stable.read_text("utf-8"))
    admission.pop("content_address")
    data_root = base / "offline_data"
    partition_dir = data_root / "derived" / "chronicle_external_core_events" / "v1"
    origin = 1_788_500_000_000
    second_rows = [
        _class_row(
            0,
            encounter="encounter-second",
            ordinal=0,
            origin=origin,
            target=PLAYER_3,
            unit_type=1,
            affiliation=1,
        ),
        _class_row(
            1,
            encounter="encounter-second",
            ordinal=0,
            origin=origin,
            target=MOB_3,
            unit_type=2,
            affiliation=2,
        ),
        _event_row(
            2,
            encounter="encounter-second",
            ordinal=0,
            origin=origin,
            event_type="DMG",
            source_guid=PLAYER_3,
            target_guid=MOB_3,
            value=77,
        ),
    ]
    if not cross_join_second_partition:
        for row in second_rows:
            row["instance"] = INSTANCE_2
    logical = b"".join(_canonical_bytes(row) + b"\n" for row in second_rows)
    compressed = gzip.compress(logical, mtime=0)
    logical_sha = _sha256(logical)
    compressed_sha = _sha256(compressed)
    partition = partition_dir / f"{INSTANCE_2}.{logical_sha}.jsonl.gz"
    partition.write_bytes(compressed)

    players = [
        {
            "guid": PLAYER_3,
            "metadata": {
                "name": "Carol",
                "class": "WARRIOR",
                "race": "Orc",
                "level": 60,
            },
        }
    ]
    resolver_sha = _sha256(_canonical_bytes(players))
    second = deepcopy(admission["instances"][0])
    second.update(
        {
            "instance_id": INSTANCE_2,
            "slug": "slug-2",
            "source_evidence": {
                "kind": "external_api_stream_set",
                "raw_api_manifest_sha256": "1" * 64,
                "normalized_partition": {
                    "path": partition.relative_to(data_root).as_posix(),
                    "compressed_file_sha256": compressed_sha,
                    "compressed_size_bytes": len(compressed),
                    "logical_content_sha256": logical_sha,
                    "record_count": len(second_rows),
                    "encounter_count": 1,
                    "all_rows_exactly_rederived_from_verified_raw_objects": True,
                },
            },
            "temporal_and_guild_provenance": {
                "started_at": "2026-08-31T00:00:00+08:00",
                "started_at_source": "metadata.encounters.min(start_time)",
                "guild": {"id": "guild-2", "name": "南北"},
                "guild_evidence": "metadata.guild.name",
                "contamination": {
                    "label": "SUSPECT_36YD_RANGE_BUG",
                    "guild_context": "南北",
                    "guild_evidence": "metadata.guild.name",
                    "time_field": "started_at",
                    "uploaded_at_used": False,
                },
            },
            "metadata_player_resolver": {
                "kind": "metadata_exact_player_guid_resolver",
                "player_count": 1,
                "players_sha256": resolver_sha,
                "players": players,
                "name_or_class_inference_used": False,
            },
            "combatant_info_evidence": {
                "status": "VERIFIED_LOCAL_OFFICIAL_STREAM",
                "message_count": 1,
            },
            "warrior_spec_evidence": {
                "observation_count": 1,
                "declared_counts": {"Arms": 1, "Fury": 0, "Other_or_unknown": 0},
                "recomputed_counts": {"Arms": 1, "Fury": 0, "Other_or_unknown": 0},
                "field_conflict_observation_count": 0,
                "conflicts_preserved_not_resolved": True,
                "observations": [],
            },
        }
    )
    admission["instances"].append(second)
    admission["summary"] = {
        "instance_count": 2,
        "record_count": len(_default_rows()) + len(second_rows),
        "metadata_player_count": 3,
        "warrior_observation_count": 3,
        "raw_object_copy_count": 0,
        "normalized_row_copy_count": 0,
        "network_request_count": 0,
    }
    content_sha = _sha256(_canonical_bytes(admission))
    admission["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical_JSON_excluding_content_address",
        "sha256": content_sha,
    }
    payload = _canonical_bytes(admission) + b"\n"
    addressed = stable.with_name(
        f"chronicle_external_reconstruction_admission_v1.{content_sha}.manifest.json"
    )
    addressed.write_bytes(payload)
    stable.write_bytes(payload)
    return stable


def _load_artifact(manifest: dict[str, object], manifest_path: Path, index: int) -> dict[str, object]:
    reference = manifest["encounters"][index]["artifact"]
    with gzip.open(manifest_path.parent / reference["path"], "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _load_entry_artifact(
    entry: dict[str, object], manifest_path: Path
) -> dict[str, object]:
    reference = entry["artifact"]
    with gzip.open(
        manifest_path.parent / reference["path"], "rt", encoding="utf-8"
    ) as handle:
        return json.load(handle)


class ChronicleExternalEncounterReconstructionV2Tests(unittest.TestCase):
    def test_build_is_deterministic_content_addressed_and_manifest_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_fixture(base, _default_rows())
            output = (
                base
                / "offline_data"
                / "derived"
                / "chronicle_external_encounter_reconstruction"
                / "v2"
            )
            first = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            first_bytes = Path(first["manifest_path"]).read_bytes()
            second = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            parallel = build_external_encounter_reconstruction(
                admission_manifest_path=admission,
                output_directory=base / "offline_data" / "derived" / "v2_parallel",
                workers=2,
            )
            self.assertEqual(first["content_sha256"], parallel["content_sha256"])
            self.assertEqual(
                first["manifest_file_sha256"], parallel["manifest_file_sha256"]
            )
            self.assertEqual(first_bytes, Path(second["manifest_path"]).read_bytes())
            self.assertEqual(
                first_bytes,
                Path(first["content_addressed_manifest_path"]).read_bytes(),
            )
            manifest, _ = load_external_reconstruction_manifest(
                first["manifest_path"]
            )
            self.assertEqual(manifest["schema"], SCHEMA)
            self.assertEqual(manifest["status"], STATUS)
            self.assertEqual(manifest["summary"]["encounter_count"], 2)
            self.assertTrue(
                manifest["publication_contract"][
                    "wave_and_target_records_content_addressed"
                ]
            )
            self.assertFalse(
                manifest["reconstruction_contract"]["legacy_csv_v1_input_accepted"]
            )

    def test_two_instances_get_isolated_deterministic_artifact_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_two_instance_fixture(base)
            output = base / "offline_data" / "derived" / "v2"
            first = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            second = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            manifest, manifest_path = load_external_reconstruction_manifest(
                first["manifest_path"]
            )
            self.assertNotIn("encounters", manifest)
            self.assertEqual(manifest["summary"]["instance_count"], 2)
            self.assertEqual(manifest["summary"]["encounter_count"], 3)
            instances = manifest["instances"]
            self.assertEqual(
                [item["instance_id"] for item in instances],
                [INSTANCE, INSTANCE_2],
            )
            first_paths = {
                entry["artifact"]["path"] for entry in instances[0]["encounters"]
            }
            second_paths = {
                entry["artifact"]["path"] for entry in instances[1]["encounters"]
            }
            self.assertTrue(
                all(path.startswith(f"instances/{INSTANCE}/encounters/") for path in first_paths)
            )
            self.assertTrue(
                all(path.startswith(f"instances/{INSTANCE_2}/encounters/") for path in second_paths)
            )
            self.assertTrue(first_paths.isdisjoint(second_paths))
            first_artifact = _load_entry_artifact(
                instances[0]["encounters"][0], manifest_path
            )
            second_artifact = _load_entry_artifact(
                instances[1]["encounters"][0], manifest_path
            )
            self.assertEqual(first_artifact["instance_id"], INSTANCE)
            self.assertEqual(second_artifact["instance_id"], INSTANCE_2)
            self.assertEqual(
                first_artifact["instance_provenance"]["contamination"]["label"],
                "POSTFIX_KNOWN_CLEAN",
            )
            self.assertEqual(
                second_artifact["instance_provenance"]["contamination"]["label"],
                "SUSPECT_36YD_RANGE_BUG",
            )
            self.assertNotEqual(
                first_artifact["source_binding"]["normalized_partition"]["path"],
                second_artifact["source_binding"]["normalized_partition"]["path"],
            )

    def test_cross_instance_partition_rows_fail_before_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_two_instance_fixture(
                base, cross_join_second_partition=True
            )
            output = base / "offline_data" / "derived" / "v2"
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "row instance mismatch"
            ):
                build_external_encounter_reconstruction(
                    admission_manifest_path=admission, output_directory=output
                )
            self.assertFalse((output / "manifest.json").exists())

    def test_loader_rejects_cross_instance_artifact_entry_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_two_instance_fixture(base)
            output = base / "offline_data" / "derived" / "v2"
            result = build_external_encounter_reconstruction(
                admission_manifest_path=admission,
                output_directory=output,
                workers=2,
            )
            stable = Path(result["manifest_path"])
            document = json.loads(stable.read_text("utf-8"))
            document.pop("content_address")
            first = document["instances"][0]["encounters"]
            second = document["instances"][1]["encounters"]
            document["instances"][0]["encounters"] = second
            document["instances"][1]["encounters"] = first
            content_sha = _sha256(_canonical_bytes(document))
            document["content_address"] = {
                "algorithm": "sha256",
                "scope": "canonical JSON excluding content_address",
                "sha256": content_sha,
            }
            payload = _canonical_bytes(document) + b"\n"
            addressed = stable.with_name(
                "chronicle_external_encounter_reconstruction_v2."
                f"{content_sha}.manifest.json"
            )
            addressed.write_bytes(payload)
            stable.write_bytes(payload)
            with self.assertRaisesRegex(
                ChronicleExternalReconstructionV2Error,
                "escapes its instance artifact set",
            ):
                load_external_reconstruction_manifest(stable)

    def test_damage_slain_lanes_and_exact_owner_controller_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_fixture(base, _default_rows())
            output = base / "offline_data" / "derived" / "v2"
            result = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            manifest, path = load_external_reconstruction_manifest(
                result["manifest_path"]
            )
            artifact = _load_artifact(manifest, path, 0)
            self.assertEqual(artifact["schema"], ARTIFACT_SCHEMA)
            self.assertEqual(artifact["event_accounting"]["damage"]["amount"], 210)
            self.assertEqual(
                artifact["event_accounting"]["slain"]["amount_added_to_damage"], 0
            )
            self.assertEqual(
                artifact["event_accounting"]["slain"][
                    "nested_attribution_diagnostic_count"
                ],
                1,
            )
            self.assertEqual(artifact["summary"]["wave_count"], 1)
            self.assertEqual(
                artifact["classification"]["pair_transition_count"], 2
            )
            self.assertEqual(
                artifact["classification"]["controller_exact_player_resolution_count"],
                0,
            )
            self.assertEqual(
                len(artifact["nonvoting_target_lanes"][LANE_HOSTILE_OBJECT]), 1
            )
            self.assertEqual(
                len(artifact["nonvoting_target_lanes"][LANE_HOSTILE_PLAYER]), 1
            )

            wave = artifact["waves"][0]
            targets = {target["target_guid"]: target for target in wave["targets"]}
            mob = targets[MOB_1]
            self.assertEqual(mob["damage_received"]["amount"], 160)
            self.assertEqual(mob["damage_received"]["unattributed_amount"], 10)
            self.assertEqual(
                mob["damage_received"]["through_first_death"]["amount"], 150
            )
            self.assertEqual(
                mob["damage_received"]["after_first_death"]["amount"], 10
            )
            by_player = {
                row["player"]["guid"]: row
                for row in mob["damage_received"]["by_exact_player"]
            }
            self.assertEqual(by_player[PLAYER_1]["damage_amount"], 150)
            self.assertEqual(
                by_player[PLAYER_1]["attribution_status_counts"],
                {
                    "EXACT_FRIENDLY_PLAYER_SOURCE": 1,
                    "EXACT_OFFICIAL_OWNER_PLAYER_DEPTH_1": 1,
                },
            )
            self.assertFalse(targets[OBJECT]["voting_for_simulator_target_model"])
            self.assertFalse(targets[PLAYER_2]["voting_for_simulator_target_model"])

    def test_encounter_state_resets_and_unknown_metadata_player_does_not_vote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_fixture(base, _default_rows())
            output = base / "offline_data" / "derived" / "v2"
            result = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            manifest, path = load_external_reconstruction_manifest(
                result["manifest_path"]
            )
            artifact = _load_artifact(manifest, path, 1)
            wave = artifact["waves"][0]
            targets = {target["target_guid"]: target for target in wave["targets"]}
            self.assertIn(PLAYER_1, targets)
            self.assertIn(LANE_UNKNOWN, targets[PLAYER_1]["lane_membership_observation_counts"])
            self.assertFalse(targets[PLAYER_1]["voting_for_simulator_target_model"])
            mob = targets[MOB_2]
            self.assertEqual(mob["damage_received"]["unattributed_amount"], 40)
            self.assertEqual(mob["damage_received"]["by_exact_player"], [])

    def test_controller_has_priority_and_unresolved_controller_does_not_fall_back(self) -> None:
        for controller, expected_player, expected_unattributed in (
            (PLAYER_2, PLAYER_2, 10),
            (MOB_2, None, 60),
        ):
            with self.subTest(controller=controller), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                rows = _default_rows()
                rows[3]["official"]["message"]["controller"] = controller
                admission = _write_fixture(base, rows)
                output = base / "offline_data" / "derived" / "v2"
                result = build_external_encounter_reconstruction(
                    admission_manifest_path=admission, output_directory=output
                )
                manifest, path = load_external_reconstruction_manifest(
                    result["manifest_path"]
                )
                artifact = _load_artifact(manifest, path, 0)
                targets = {
                    target["target_guid"]: target
                    for target in artifact["waves"][0]["targets"]
                }
                mob = targets[MOB_1]
                by_player = {
                    row["player"]["guid"]: row
                    for row in mob["damage_received"]["by_exact_player"]
                }
                self.assertEqual(
                    mob["damage_received"]["unattributed_amount"],
                    expected_unattributed,
                )
                if expected_player is None:
                    self.assertNotIn(PLAYER_2, by_player)
                    self.assertEqual(by_player[PLAYER_1]["damage_amount"], 100)
                else:
                    self.assertEqual(by_player[expected_player]["damage_amount"], 50)
                    self.assertEqual(by_player[PLAYER_1]["damage_amount"], 100)

    def test_class_after_same_order_damage_does_not_backfill_the_past(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            origin = 1_788_000_000_000
            damage = _event_row(
                0,
                encounter=ENCOUNTER_1,
                ordinal=0,
                origin=origin,
                event_type="DMG",
                source_guid=PLAYER_1,
                target_guid=MOB_1,
                value=100,
            )
            classification = _class_row(
                0,
                encounter=ENCOUNTER_1,
                ordinal=0,
                origin=origin,
                target=MOB_1,
                unit_type=2,
                affiliation=2,
            )
            admission = _write_fixture(base, [damage, classification])
            output = base / "offline_data" / "derived" / "v2"
            result = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            manifest, path = load_external_reconstruction_manifest(
                result["manifest_path"]
            )
            artifact = _load_artifact(manifest, path, 0)
            self.assertEqual(artifact["summary"]["wave_count"], 0)
            self.assertEqual(
                artifact["classification"]["future_classification_backfill_count"], 0
            )
            self.assertEqual(artifact["unassigned_activity"]["event_count"], 1)
            unknown = artifact["nonvoting_target_lanes"][LANE_UNKNOWN]
            # Both the source player and target creature are still unknown at
            # the earlier DMG event.  The later CLASS row must not rewrite
            # either historical observation into a voting target.
            self.assertEqual(len(unknown), 2)
            self.assertIn(MOB_1, {item["target_guid"] for item in unknown})

    def test_dead_value_is_fail_closed_before_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            rows = _default_rows()
            rows[8]["value"] = 999
            admission = _write_fixture(base, rows)
            output = base / "offline_data" / "derived" / "v2"
            with self.assertRaisesRegex(
                ChronicleExternalReconstructionV2Error, "DEAD.value"
            ):
                build_external_encounter_reconstruction(
                    admission_manifest_path=admission, output_directory=output
                )
            self.assertFalse((output / "manifest.json").exists())

    def test_negative_damage_is_preserved_as_nonvoting_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            origin = 1_788_000_000_000
            rows = [
                _class_row(
                    0,
                    encounter=ENCOUNTER_1,
                    ordinal=0,
                    origin=origin,
                    target=PLAYER_1,
                    unit_type=1,
                    affiliation=1,
                ),
                _class_row(
                    1,
                    encounter=ENCOUNTER_1,
                    ordinal=0,
                    origin=origin,
                    target=MOB_1,
                    unit_type=2,
                    affiliation=2,
                ),
                _event_row(
                    2,
                    encounter=ENCOUNTER_1,
                    ordinal=0,
                    origin=origin,
                    event_type="DMG",
                    source_guid=PLAYER_1,
                    target_guid=MOB_1,
                    value=-254,
                ),
            ]
            admission = _write_fixture(base, rows)
            output = base / "offline_data" / "derived" / "v2"
            result = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            manifest, path = load_external_reconstruction_manifest(
                result["manifest_path"]
            )
            artifact = _load_artifact(manifest, path, 0)
            damage = artifact["event_accounting"]["damage"]
            self.assertEqual(damage["event_count"], 0)
            self.assertEqual(damage["amount"], 0)
            self.assertEqual(damage["negative_event_count_excluded"], 1)
            self.assertEqual(damage["negative_signed_amount_excluded"], -254)
            self.assertEqual(damage["absolute_amount_excluded"], 254)
            self.assertEqual(
                damage["policy"],
                "PRESERVED_DIAGNOSTIC_NONVOTING_NO_ABS_OR_CLAMP",
            )
            self.assertEqual(manifest["summary"]["negative_damage_event_count"], 1)
            target = artifact["waves"][0]["targets"][0]
            self.assertEqual(target["damage_received"]["amount"], 0)

    def test_eventmeta_order_is_fail_closed_before_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            rows = _default_rows()
            rows[4], rows[5] = rows[5], rows[4]
            admission = _write_fixture(base, rows)
            output = base / "offline_data" / "derived" / "v2"
            with self.assertRaisesRegex(
                ChronicleExternalReconstructionV2Error, "strictly increasing"
            ):
                build_external_encounter_reconstruction(
                    admission_manifest_path=admission, output_directory=output
                )
            self.assertFalse((output / "manifest.json").exists())

    def test_first_encounter_ordinal_must_be_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            origin = 1_788_000_000_000
            rows = [
                _class_row(
                    0,
                    encounter=ENCOUNTER_1,
                    ordinal=1,
                    origin=origin,
                    target=MOB_1,
                    unit_type=2,
                    affiliation=2,
                )
            ]
            admission = _write_fixture(base, rows)
            output = base / "offline_data" / "derived" / "v2"
            with self.assertRaisesRegex(
                ChronicleExternalReconstructionV2Error,
                "first encounter ordinal must be zero",
            ):
                build_external_encounter_reconstruction(
                    admission_manifest_path=admission, output_directory=output
                )
            self.assertFalse((output / "manifest.json").exists())

    def test_tampered_content_addressed_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_fixture(base, _default_rows())
            output = base / "offline_data" / "derived" / "v2"
            result = build_external_encounter_reconstruction(
                admission_manifest_path=admission, output_directory=output
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            artifact = output / manifest["encounters"][0]["artifact"]["path"]
            artifact.write_bytes(artifact.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalReconstructionV2Error, "size mismatch"
            ):
                load_external_reconstruction_manifest(result["manifest_path"])

    def test_output_cannot_escape_the_admitted_offline_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            admission = _write_fixture(base, _default_rows())
            with self.assertRaisesRegex(
                ChronicleExternalReconstructionV2Error, "must stay beneath"
            ):
                build_external_encounter_reconstruction(
                    admission_manifest_path=admission,
                    output_directory=base / "outside",
                )


if __name__ == "__main__":
    unittest.main()
