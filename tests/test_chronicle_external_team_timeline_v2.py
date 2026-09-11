from __future__ import annotations

import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from o2o_dps.chronicle_external_encounter_reconstruction_v2 import (
    build_external_encounter_reconstruction,
)
from o2o_dps.chronicle_external_event_normalizer_v1 import (
    IMPLEMENTATION_REVISION as NORMALIZATION_IMPLEMENTATION_REVISION,
    SCHEMA as NORMALIZATION_SCHEMA,
)
from o2o_dps.chronicle_external_team_timeline_v2 import (
    ChronicleExternalTeamTimelineV2Error,
    NEGATIVE_DAMAGE_DIAGNOSTIC_LANE,
    NEGATIVE_DAMAGE_VALUE_POLICY,
    PARTITION_RECORD_SCHEMA,
    SCHEMA,
    STATUS,
    build_external_team_timeline,
    load_external_team_timeline_manifest,
)
from tests.test_chronicle_external_encounter_reconstruction_v2 import (
    ENCOUNTER_1,
    ENCOUNTER_2,
    INSTANCE,
    MOB_1,
    MOB_2,
    OBJECT,
    PLAYER_1,
    PLAYER_2,
    _class_row,
    _event_row,
    _write_fixture,
)


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


def _content_addressed(core: dict[str, object]) -> dict[str, object]:
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical_JSON_excluding_content_address",
            "sha256": _sha256(_canonical_bytes(core)),
        },
    }


def _action_row(
    index: int,
    *,
    encounter: str,
    ordinal: int,
    origin: int,
    event_type: str,
    source_guid: str,
    target_guid: str,
    spell_id: int,
    spell_name: str,
) -> dict[str, object]:
    row = _event_row(
        index,
        encounter=encounter,
        ordinal=ordinal,
        origin=origin,
        event_type=event_type,
        source_guid=source_guid,
        target_guid=target_guid,
        value=None,
    )
    row["spell_id"] = spell_id
    row["spell"] = spell_name
    message = row["official"]["message"]
    message.update(
        {
            "caster": source_guid,
            "target": target_guid,
            "spell_data": {
                "id": spell_id,
                "name": spell_name,
                "attack_outcome": 0,
            },
        }
    )
    if event_type == "START":
        message.update(
            {
                "cast_time_ms": 0,
                "channel_time_ms": 0,
                "cast_flags": 0,
                "item_id": None,
            }
        )
    elif event_type == "GO":
        message.update({"num_hits": 1, "num_misses": 0, "item_id": None})
    return row


def _timeline_rows() -> list[dict[str, object]]:
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
        _action_row(
            4,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="START",
            source_guid=PLAYER_1,
            target_guid=MOB_1,
            spell_id=1001,
            spell_name="Direct Action",
        ),
        _action_row(
            5,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="GO",
            source_guid=OBJECT,
            target_guid=MOB_1,
            spell_id=1002,
            spell_name="Owned Object Action",
        ),
        _event_row(
            6,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=PLAYER_1,
            target_guid=MOB_1,
            value=100,
        ),
        _event_row(
            7,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=OBJECT,
            target_guid=MOB_1,
            value=50,
        ),
        _event_row(
            8,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=MOB_1,
            target_guid=PLAYER_1,
            value=20,
        ),
        _event_row(
            9,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=PLAYER_1,
            target_guid=OBJECT,
            value=30,
        ),
        _event_row(
            10,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="HEAL",
            source_guid=PLAYER_1,
            target_guid=PLAYER_1,
            value=25,
        ),
        _event_row(
            11,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DEAD",
            source_guid=PLAYER_1,
            target_guid=MOB_1,
            value=None,
            nested_attribution=True,
        ),
        _class_row(
            12,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            target=PLAYER_2,
            unit_type=1,
            affiliation=2,
            controller=MOB_1,
        ),
        _event_row(
            13,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="DMG",
            source_guid=PLAYER_2,
            target_guid=MOB_1,
            value=10,
        ),
        _action_row(
            14,
            encounter=ENCOUNTER_1,
            ordinal=0,
            origin=origin_1,
            event_type="GO",
            source_guid=PLAYER_2,
            target_guid=MOB_1,
            spell_id=1003,
            spell_name="Hostile Player Action",
        ),
        _class_row(
            15,
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
        _class_row(
            2,
            encounter=ENCOUNTER_2,
            ordinal=1,
            origin=origin_2,
            target=PLAYER_1,
            unit_type=1,
            affiliation=1,
        ),
        _event_row(
            3,
            encounter=ENCOUNTER_2,
            ordinal=1,
            origin=origin_2,
            event_type="DMG",
            source_guid=PLAYER_1,
            target_guid=MOB_2,
            value=60,
        ),
    ]


def _complete_input_closure(
    base: Path, rows: list[dict[str, object]]
) -> dict[str, Path]:
    admission_path = _write_fixture(base, rows)
    admission = json.loads(admission_path.read_text("utf-8"))
    data_root = base / "offline_data"
    partition_ref = admission["instances"][0]["source_evidence"][
        "normalized_partition"
    ]
    partition_path = data_root / partition_ref["path"]
    norm_core = {
        "schema": NORMALIZATION_SCHEMA,
        "implementation_revision": NORMALIZATION_IMPLEMENTATION_REVISION,
        "kind": "chronicle_external_core_event_normalization_manifest",
        "source": {"manifest_sha256": "1" * 64},
        "official_contract": {"proto_commit": "004acd9"},
        "normalization_contract": {"manifest_committed_last": True},
        "summary": {
            "instance_count": 1,
            "encounter_count": partition_ref["encounter_count"],
            "record_count": partition_ref["record_count"],
            "unknown_field_count": 0,
            "raw_object_copy_count": 0,
            "network_request_count": 0,
        },
        "partitions": [
            {
                "instance_id": INSTANCE,
                "slug": "slug-1",
                "partition": partition_path.name,
                "logical_content_sha256": partition_ref[
                    "logical_content_sha256"
                ],
                "compressed_file_sha256": partition_ref[
                    "compressed_file_sha256"
                ],
                "compressed_size_bytes": partition_ref[
                    "compressed_size_bytes"
                ],
                "record_count": partition_ref["record_count"],
                "encounter_count": partition_ref["encounter_count"],
                "event_type_counts": {},
                "unknown_field_count": 0,
                "source_streams": [],
                "raw_object_open_count": 7,
                "raw_object_copy_count": 0,
            }
        ],
    }
    normalization = _content_addressed(norm_core)
    norm_payload = _canonical_bytes(normalization) + b"\n"
    norm_dir = data_root / "derived" / "chronicle_external_core_events" / "v1"
    norm_stable = norm_dir / "manifest.json"
    norm_addressed = norm_dir / (
        "chronicle_external_core_events_v1."
        f"{normalization['content_address']['sha256']}.manifest.json"
    )
    norm_stable.write_bytes(norm_payload)
    norm_addressed.write_bytes(norm_payload)

    admission_core = {
        key: value for key, value in admission.items() if key != "content_address"
    }
    admission_core["inputs"]["normalization_manifest"] = {
        "path": norm_addressed.relative_to(data_root).as_posix(),
        "file_sha256": _sha256(norm_payload),
        "content_sha256": normalization["content_address"]["sha256"],
        "size_bytes": len(norm_payload),
        "schema": NORMALIZATION_SCHEMA,
        "implementation_revision": NORMALIZATION_IMPLEMENTATION_REVISION,
    }
    admission_core["instances"][0]["temporal_and_guild_provenance"][
        "contamination"
    ].update(
        {
            "guild_context": "南北",
            "guild_evidence": "metadata.guild.name",
            "uploaded_at_used": False,
        }
    )
    admission_core["summary"] = {
        "instance_count": 1,
        "record_count": len(rows),
        "metadata_player_count": 2,
        "warrior_observation_count": 2,
        "raw_object_copy_count": 0,
        "normalized_row_copy_count": 0,
        "network_request_count": 0,
    }
    admission_core["instances"][0]["warrior_spec_evidence"] = {
        "observation_count": 2,
        "declared_counts": {"Arms": 0, "Fury": 1, "Other_or_unknown": 1},
        "recomputed_counts": {"Arms": 0, "Fury": 1, "Other_or_unknown": 1},
        "field_conflict_observation_count": 1,
        "conflicts_preserved_not_resolved": True,
        "observations": [
            {
                "player_guid": PLAYER_1,
                "player_name": "Alice",
                "player_class": "WARRIOR",
                "player_spec": "Fury",
                "sources": ["instance_metadata"],
                "field_conflicts": {},
                "spec_evidence_status": "OBSERVED",
            },
            {
                "player_guid": PLAYER_2,
                "player_name": "Bob",
                "player_class": "WARRIOR",
                "player_spec": "Unknown",
                "sources": ["instance_metadata", "instance_ranking_records"],
                "field_conflicts": {"player_spec": ["Arms", "Fury"]},
                "spec_evidence_status": "UNKNOWN_NONVOTING",
            },
        ],
    }
    admission = _content_addressed(admission_core)
    admission_payload = _canonical_bytes(admission) + b"\n"
    admission_addressed = admission_path.with_name(
        "chronicle_external_reconstruction_admission_v1."
        f"{admission['content_address']['sha256']}.manifest.json"
    )
    admission_path.write_bytes(admission_payload)
    admission_addressed.write_bytes(admission_payload)

    reconstruction_dir = (
        data_root / "derived" / "chronicle_external_encounter_reconstruction" / "v2"
    )
    reconstruction = build_external_encounter_reconstruction(
        admission_manifest_path=admission_path,
        output_directory=reconstruction_dir,
    )
    return {
        "normalization": norm_stable,
        "admission": admission_path,
        "reconstruction": Path(reconstruction["manifest_path"]),
    }


def _load_partition(manifest: dict[str, object], path: Path) -> list[dict[str, object]]:
    partition = path.parent / manifest["instances"][0]["partition"]["path"]
    with gzip.open(partition, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def _complete_multi_instance_closure(base: Path) -> dict[str, Path]:
    closure = _complete_input_closure(base, _timeline_rows())
    data_root = base / "offline_data"
    second_instance = "instance-2"
    encounter_map = {
        ENCOUNTER_1: "encounter-3",
        ENCOUNTER_2: "encounter-4",
    }
    second_rows = deepcopy(_timeline_rows())
    for row in second_rows:
        row["instance"] = second_instance
        row["encounter"] = encounter_map[row["encounter"]]
        row["first_timestamp_ms"] += 120_000
        row["timestamp_ms"] += 120_000
    logical = b"".join(_canonical_bytes(row) + b"\n" for row in second_rows)
    compressed = gzip.compress(logical, mtime=0)
    logical_sha = _sha256(logical)
    compressed_sha = _sha256(compressed)
    partition_dir = data_root / "derived" / "chronicle_external_core_events" / "v1"
    second_partition = partition_dir / f"{second_instance}.{logical_sha}.jsonl.gz"
    second_partition.write_bytes(compressed)

    normalization_path = closure["normalization"]
    normalization = json.loads(normalization_path.read_text("utf-8"))
    normalization_core = {
        key: value
        for key, value in normalization.items()
        if key != "content_address"
    }
    second_partition_entry = deepcopy(normalization_core["partitions"][0])
    second_partition_entry.update(
        {
            "instance_id": second_instance,
            "slug": "slug-2",
            "partition": second_partition.name,
            "logical_content_sha256": logical_sha,
            "compressed_file_sha256": compressed_sha,
            "compressed_size_bytes": len(compressed),
            "record_count": len(second_rows),
            "encounter_count": 2,
        }
    )
    normalization_core["partitions"].append(second_partition_entry)
    normalization_core["partitions"].sort(key=lambda item: item["instance_id"])
    normalization_core["summary"].update(
        {
            "instance_count": 2,
            "encounter_count": 4,
            "record_count": len(_timeline_rows()) + len(second_rows),
        }
    )
    normalization = _content_addressed(normalization_core)
    normalization_payload = _canonical_bytes(normalization) + b"\n"
    normalization_addressed = normalization_path.with_name(
        "chronicle_external_core_events_v1."
        f"{normalization['content_address']['sha256']}.manifest.json"
    )
    normalization_path.write_bytes(normalization_payload)
    normalization_addressed.write_bytes(normalization_payload)

    admission_path = closure["admission"]
    admission = json.loads(admission_path.read_text("utf-8"))
    admission_core = {
        key: value for key, value in admission.items() if key != "content_address"
    }
    admission_core["inputs"]["normalization_manifest"].update(
        {
            "path": normalization_addressed.relative_to(data_root).as_posix(),
            "file_sha256": _sha256(normalization_payload),
            "content_sha256": normalization["content_address"]["sha256"],
            "size_bytes": len(normalization_payload),
        }
    )
    second_admission = deepcopy(admission_core["instances"][0])
    second_admission.update(
        {
            "instance_id": second_instance,
            "slug": "slug-2",
        }
    )
    second_admission["source_evidence"]["normalized_partition"].update(
        {
            "path": second_partition.relative_to(data_root).as_posix(),
            "compressed_file_sha256": compressed_sha,
            "compressed_size_bytes": len(compressed),
            "logical_content_sha256": logical_sha,
            "record_count": len(second_rows),
            "encounter_count": 2,
        }
    )
    admission_core["instances"].append(second_admission)
    admission_core["instances"].sort(key=lambda item: item["instance_id"])
    admission_core["summary"] = {
        "instance_count": 2,
        "record_count": len(_timeline_rows()) + len(second_rows),
        "metadata_player_count": 4,
        "warrior_observation_count": 4,
        "raw_object_copy_count": 0,
        "normalized_row_copy_count": 0,
        "network_request_count": 0,
    }
    admission = _content_addressed(admission_core)
    admission_payload = _canonical_bytes(admission) + b"\n"
    admission_addressed = admission_path.with_name(
        "chronicle_external_reconstruction_admission_v1."
        f"{admission['content_address']['sha256']}.manifest.json"
    )
    admission_path.write_bytes(admission_payload)
    admission_addressed.write_bytes(admission_payload)

    reconstruction_dir = (
        data_root
        / "derived"
        / "chronicle_external_encounter_reconstruction"
        / "multi"
    )
    reconstruction = build_external_encounter_reconstruction(
        admission_manifest_path=admission_path,
        output_directory=reconstruction_dir,
    )
    return {
        "normalization": normalization_path,
        "admission": admission_path,
        "reconstruction": Path(reconstruction["manifest_path"]),
    }


class ChronicleExternalTeamTimelineV2Tests(unittest.TestCase):
    def test_signed_negative_damage_is_a_separate_nonvoting_diagnostic_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            rows = _timeline_rows()
            rows[6]["value"] = -254
            rows[6]["official"]["message"]["amount"] = -254
            closure = _complete_input_closure(base, rows)
            output = base / "offline_data" / "derived" / "timeline" / "negative"
            result = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            manifest, _ = load_external_team_timeline_manifest(
                result["manifest_path"]
            )
            first = _load_partition(manifest, Path(result["manifest_path"]))[0]
            diagnostics = first["negative_damage_diagnostics"]
            self.assertEqual(len(diagnostics), 1)
            diagnostic = diagnostics[0]["negative_damage_diagnostic"]
            self.assertEqual(diagnostic["signed_amount"], -254)
            self.assertEqual(diagnostic["absolute_magnitude"], 254)
            self.assertEqual(diagnostic["lane"], NEGATIVE_DAMAGE_DIAGNOSTIC_LANE)
            self.assertEqual(diagnostic["policy"], NEGATIVE_DAMAGE_VALUE_POLICY)
            self.assertEqual(diagnostic["damage_amount_added"], 0)
            self.assertEqual(diagnostic["reward_amount_added"], 0)
            self.assertNotIn("damage", diagnostics[0])
            self.assertFalse(
                any(
                    event["anchor"] == diagnostics[0]["anchor"]
                    for player in first["players"]
                    for event in player["timeline"]
                )
            )
            self.assertFalse(
                any(
                    event["anchor"] == diagnostics[0]["anchor"]
                    for event in first["unattributed_lane"]["timeline"]
                )
            )
            self.assertEqual(first["event_accounting"]["damage"]["amount"], 110)
            self.assertEqual(
                first["event_accounting"]["damage"][
                    "negative_signed_amount_excluded"
                ],
                -254,
            )
            self.assertEqual(first["summary"]["damage_amount"], 110)
            self.assertEqual(
                first["summary"]["negative_damage_diagnostic_count"], 1
            )
            self.assertEqual(
                manifest["summary"]["negative_damage_absolute_amount_excluded"],
                254,
            )

    def test_multi_instance_serial_and_parallel_are_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            closure = _complete_multi_instance_closure(base)
            output = base / "offline_data" / "derived" / "timeline" / "serial"
            first = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
                workers=1,
            )
            parallel_output = (
                base / "offline_data" / "derived" / "timeline" / "parallel"
            )
            second = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=parallel_output,
                workers=2,
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first["workers_used"], 1)
            self.assertEqual(second["workers_used"], 2)
            self.assertEqual(
                Path(first["manifest_path"]).read_bytes(),
                Path(second["manifest_path"]).read_bytes(),
            )
            manifest, _ = load_external_team_timeline_manifest(
                first["manifest_path"]
            )
            self.assertEqual(manifest["instance_order"], [INSTANCE, "instance-2"])
            self.assertEqual(manifest["summary"]["instance_count"], 2)
            self.assertEqual(manifest["summary"]["encounter_count"], 4)
            self.assertEqual(manifest["summary"]["wave_count"], 4)
            self.assertEqual(len(manifest["instances"]), 2)
            partition_paths = {
                entry["partition"]["path"] for entry in manifest["instances"]
            }
            self.assertEqual(len(partition_paths), 2)
            for entry in manifest["instances"]:
                relative = entry["partition"]["path"]
                self.assertEqual(
                    (output / relative).read_bytes(),
                    (parallel_output / relative).read_bytes(),
                )
            self.assertTrue(
                manifest["streaming_contract"][
                    "one_admitted_partition_verified_then_streamed_per_instance"
                ]
            )
            self.assertFalse(
                manifest["streaming_contract"][
                    "normalized_events_materialized_as_full_instance_list"
                ]
            )

    def test_build_is_deterministic_content_addressed_and_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            closure = _complete_input_closure(base, _timeline_rows())
            output = base / "offline_data" / "derived" / "timeline" / "v2"
            first = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            first_bytes = Path(first["manifest_path"]).read_bytes()
            second = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first_bytes, Path(second["manifest_path"]).read_bytes())
            manifest, path = load_external_team_timeline_manifest(
                first["manifest_path"]
            )
            self.assertEqual(manifest["schema"], SCHEMA)
            self.assertEqual(manifest["status"], STATUS)
            self.assertEqual(manifest["summary"]["instance_count"], 1)
            self.assertEqual(manifest["summary"]["encounter_count"], 2)
            self.assertEqual(manifest["summary"]["wave_count"], 2)
            self.assertEqual(first_bytes, Path(first["content_addressed_manifest_path"]).read_bytes())
            self.assertEqual(path, Path(first["manifest_path"]))

    def test_exact_player_owner_hostile_object_and_hostile_player_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            closure = _complete_input_closure(base, _timeline_rows())
            output = base / "offline_data" / "derived" / "timeline" / "v2"
            result = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            waves = _load_partition(manifest, Path(result["manifest_path"]))
            first = waves[0]
            alice = next(
                item for item in first["players"] if item["player"]["guid"] == PLAYER_1
            )
            bob = next(
                item for item in first["players"] if item["player"]["guid"] == PLAYER_2
            )
            self.assertEqual(alice["player"]["class"], "WARRIOR")
            self.assertEqual(alice["player"]["race"], "Human")
            self.assertEqual(alice["warrior_spec_evidence"]["player_spec"], "Fury")
            self.assertTrue(
                alice["warrior_spec_evidence"]["voting_for_fury_or_arms_lane"]
            )
            self.assertEqual(bob["warrior_spec_evidence"]["player_spec"], "Unknown")
            self.assertFalse(
                bob["warrior_spec_evidence"]["voting_for_fury_or_arms_lane"]
            )
            kinds = [event["attribution"]["attribution_kind"] for event in alice["timeline"]]
            self.assertIn("DIRECT_FRIENDLY_PLAYER", kinds)
            self.assertIn("EXACT_OFFICIAL_OWNER", kinds)
            self.assertEqual(first["summary"]["hostile_object_source_attributed_event_count"], 2)
            self.assertEqual(first["summary"]["hostile_object_target_event_count"], 1)
            object_target = next(
                event
                for event in alice["timeline"]
                if event["target"]["guid"] == OBJECT
            )
            self.assertFalse(object_target["target"]["voting_enemy_target"])
            self.assertTrue(object_target["target"]["hostile_object_preserved_nonvoting"])
            self.assertEqual(first["summary"]["hostile_player_source_event_count"], 2)
            hostile_events = [
                event
                for event in first["unattributed_lane"]["timeline"]
                if event["source"]["guid"] == PLAYER_2
            ]
            self.assertEqual(len(hostile_events), 2)
            self.assertTrue(
                all(
                    event["attribution"]["status"]
                    == "SOURCE_IS_OFFICIAL_HOSTILE_PLAYER_AT_EVENT"
                    for event in hostile_events
                )
            )
            self.assertEqual(first["event_accounting"]["damage"]["amount"], 210)
            self.assertEqual(first["event_accounting"]["slain"]["amount_added_to_damage"], 0)
            self.assertEqual(len(first["death_markers"]), 1)
            self.assertTrue(first["death_markers"][0]["death"]["nested_attribution_amount_ignored"])

    def test_encounter_reset_and_later_class_do_not_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            closure = _complete_input_closure(base, _timeline_rows())
            output = base / "offline_data" / "derived" / "timeline" / "v2"
            result = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            second = _load_partition(manifest, Path(result["manifest_path"]))[1]
            alice = next(
                item for item in second["players"] if item["player"]["guid"] == PLAYER_1
            )
            self.assertEqual(alice["summary"]["damage_amount"], 60)
            self.assertEqual(second["unattributed_lane"]["summary"]["damage_amount"], 40)
            self.assertEqual(second["summary"]["classification_transition_count"], 1)
            self.assertEqual(second["scientific_boundaries"]["class_future_backfill_count"], 0)

    def test_tampered_partition_is_rejected_by_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            closure = _complete_input_closure(base, _timeline_rows())
            output = base / "offline_data" / "derived" / "timeline" / "v2"
            result = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            partition = output / manifest["instances"][0]["partition"]["path"]
            partition.write_bytes(partition.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalTeamTimelineV2Error, "compressed size mismatch"
            ):
                load_external_team_timeline_manifest(result["manifest_path"])

    def test_raid_contamination_contract_never_uses_player_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            closure = _complete_input_closure(base, _timeline_rows())
            output = base / "offline_data" / "derived" / "timeline" / "v2"
            result = build_external_team_timeline(
                normalization_manifest_path=closure["normalization"],
                admission_manifest_path=closure["admission"],
                reconstruction_manifest_path=closure["reconstruction"],
                output_directory=output,
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            self.assertEqual(manifest["contamination_contract"]["time_field"], "started_at")
            self.assertFalse(manifest["contamination_contract"]["player_name_used"])
            self.assertFalse(manifest["contamination_contract"]["uploaded_at_used"])
            self.assertFalse(
                manifest["contamination_contract"][
                    "named_player_blacklist_or_weighting_allowed"
                ]
            )
            self.assertEqual(
                manifest["contamination_contract"]["rules"][
                    "guild_is_nanbei_and_started_before_fix_date"
                ],
                "SUSPECT_36YD_RANGE_BUG",
            )
            self.assertEqual(
                manifest["contamination_contract"]["rules"][
                    "guild_is_nanbei_and_started_in_fix_morning_boundary"
                ],
                "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
            )
            self.assertEqual(
                manifest["contamination_contract"]["rules"][
                    "guild_is_nanbei_and_started_at_or_after_noon_safe_floor"
                ],
                "POSTFIX_KNOWN_CLEAN",
            )
            boundary = manifest["contamination_contract"]["boundary_policy"]
            self.assertEqual(
                boundary["pre_fix_suspect_before_local"],
                "2026-09-03T00:00:00+08:00",
            )
            self.assertEqual(
                boundary["postfix_known_clean_at_or_after_local"],
                "2026-09-03T12:00:00+08:00",
            )
            self.assertIn(
                "not a claim about the exact patch instant",
                boundary["boundary_provenance"]["safe_floor_policy"],
            )
            self.assertFalse(manifest["scientific_boundaries"]["team_model_built"])
            self.assertFalse(manifest["scientific_boundaries"]["comparison_authorized"])


if __name__ == "__main__":
    unittest.main()
