from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_reconstruction_admission_v1 as admission_v1
from o2o_dps.chronicle_external_event_normalizer_v1 import (
    CORE_STREAM_TYPES,
    build_external_core_event_normalization,
)
from o2o_dps.chronicle_external_reconstruction_admission_v1 import (
    ChronicleExternalAdmissionError,
    SCHEMA,
    SOURCE_EVIDENCE_KIND,
    STATUS,
    build_external_reconstruction_admission,
    canonicalize_optional_0x_owner,
    iter_versioned_reconstruction_rows,
)


PLAYER_GUID = "0x00000000000000A1"
OWNER_WITHOUT_PREFIX = "00000000000000A1"
MOB_GUID = "0xF130000001000001"
INSTANCE_ID = "instance-1"
ENCOUNTER_ID = "encounter-1"
STARTED_AT = "2026-09-03T12:48:06.919Z"
FIRST_TIMESTAMP_MS = 1_788_439_686_919
PLAYER_GUID_2 = "0x00000000000000B2"
MOB_GUID_2 = "0xF130000002000002"
INSTANCE_ID_2 = "instance-2"
ENCOUNTER_ID_2 = "encounter-2"
STARTED_AT_2 = "2026-09-03T13:40:30.768Z"
FIRST_TIMESTAMP_MS_2 = 1_788_442_830_768


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


def _varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _vfield(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _bfield(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _sfield(number: int, value: str) -> bytes:
    return _bfield(number, value.encode("utf-8"))


def _meta(index: int, offset_ms: int) -> bytes:
    return _vfield(1, index) + _vfield(2, offset_ms)


def _spell(spell_id: int, name: str) -> bytes:
    return _vfield(1, spell_id) + _sfield(2, name)


def _event_message(
    stream_type: str,
    index: int,
    *,
    player_guid: str = PLAYER_GUID,
    mob_guid: str = MOB_GUID,
) -> bytes:
    head = _bfield(1, _meta(index, index))
    if stream_type == "damage":
        return (
            head
            + _sfield(3, player_guid)
            + _sfield(5, mob_guid)
            + _vfield(7, 100 + index)
            + _vfield(8, 1)
            + _bfield(10, _spell(78, "Heroic Strike"))
        )
    if stream_type == "heal":
        return (
            head
            + _sfield(3, player_guid)
            + _sfield(4, player_guid)
            + _vfield(6, 50)
            + _vfield(7, 1)
            + _bfield(8, _spell(1, "Test Heal"))
        )
    if stream_type == "slain":
        return head + _sfield(2, mob_guid) + _sfield(3, player_guid)
    if stream_type == "spell_start":
        return (
            head
            + _bfield(3, _spell(1464, "Slam"))
            + _sfield(4, player_guid)
            + _sfield(5, mob_guid)
        )
    if stream_type == "spell_go":
        return (
            head
            + _bfield(3, _spell(1680, "Whirlwind"))
            + _sfield(4, player_guid)
            + _sfield(5, mob_guid)
            + _vfield(6, 1)
        )
    if stream_type == "spell_fail":
        return (
            head
            + _sfield(2, player_guid)
            + _bfield(3, _spell(5308, "Execute"))
            + _vfield(4, 1)
        )
    if stream_type == "unit_classification":
        return (
            head
            + _sfield(2, player_guid)
            + _vfield(3, 7)
            + _vfield(4, 2)
            + _sfield(5, player_guid[2:])
            + _vfield(7, 123)
        )
    raise AssertionError(stream_type)


def _framed_stream(
    message: bytes,
    *,
    encounter_id: str = ENCOUNTER_ID,
    first_timestamp_ms: int = FIRST_TIMESTAMP_MS,
) -> bytes:
    return _framed_messages(
        [message],
        encounter_id=encounter_id,
        first_timestamp_ms=first_timestamp_ms,
    )


def _framed_messages(
    messages: list[bytes],
    *,
    encounter_id: str = ENCOUNTER_ID,
    first_timestamp_ms: int = FIRST_TIMESTAMP_MS,
) -> bytes:
    body = b"".join(_varint(len(message)) + message for message in messages)
    encounter = encounter_id.encode("utf-8")
    frame = (
        _varint(len(encounter))
        + encounter
        + _varint(first_timestamp_ms)
        + _varint(len(messages))
        + _varint(len(body))
        + body
    )
    return gzip.compress(frame, mtime=0)


def _combatant_info_stream(
    *,
    guid: str = PLAYER_GUID,
    encounter_id: str = ENCOUNTER_ID,
    first_timestamp_ms: int = FIRST_TIMESTAMP_MS,
    name: str = "Alice",
    names: tuple[str, ...] | None = None,
    hero_classes: tuple[str, ...] | None = None,
    races: tuple[str, ...] | None = None,
    guild_names: tuple[str | None, ...] | None = None,
    extra_combatants: tuple[tuple[str, str], ...] = (),
) -> bytes:
    gear = _vfield(1, 19019)
    talents = _bfield(1, _varint(17) + _varint(34) + _varint(0)) + _sfield(
        2, "20305001302"
    )
    primary_names = names or (name,)
    primary_count = len(primary_names)
    primary_classes = hero_classes or ("Warrior",) * primary_count
    primary_races = races or ("Human",) * primary_count
    primary_guild_names = guild_names or ("南北",) * primary_count
    if not (
        len(primary_classes)
        == len(primary_races)
        == len(primary_guild_names)
        == primary_count
    ):
        raise ValueError("primary CombatantInfo field series lengths must match")
    identities = [
        (guid, primary_name, hero_class, race, guild_name)
        for primary_name, hero_class, race, guild_name in zip(
            primary_names,
            primary_classes,
            primary_races,
            primary_guild_names,
            strict=True,
        )
    ]
    identities.extend(
        (combatant_guid, combatant_name, "Warrior", "Human", "南北")
        for combatant_guid, combatant_name in extra_combatants
    )
    messages = []
    for index, (
        combatant_guid,
        combatant_name,
        hero_class,
        race,
        guild_name,
    ) in enumerate(identities, 1):
        fields = [
            _bfield(1, _meta(index, index - 1)),
            _sfield(2, combatant_guid),
            _sfield(3, combatant_name),
            _sfield(4, hero_class),
            _sfield(5, race),
            _vfield(6, 1),
        ]
        if guild_name is not None:
            fields.append(_sfield(7, guild_name))
        fields.extend((_bfield(8, gear), _bfield(9, talents)))
        messages.append(b"".join(fields))
    return _framed_messages(
        messages,
        encounter_id=encounter_id,
        first_timestamp_ms=first_timestamp_ms,
    )


class ChronicleExternalReconstructionAdmissionTests(unittest.TestCase):
    def test_rfc3339_two_digit_fraction_is_cross_version_valid(self) -> None:
        parsed = admission_v1._parse_rfc3339(
            "2026-08-21T11:56:34.88Z", label="started_at"
        )
        self.assertEqual(880000, parsed.microsecond)

    def _write_object(
        self,
        raw_root: Path,
        payload: bytes,
        *,
        suffix: str,
    ) -> tuple[dict[str, object], Path]:
        digest = _sha256(payload)
        path = raw_root / "objects" / "sha256" / digest[:2] / f"{digest}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return (
            {
                "sha256": digest,
                "size_bytes": len(payload),
                "media_type": "application/octet-stream",
                "relative_path": path.relative_to(raw_root).as_posix(),
            },
            path,
        )

    def _fixture(
        self,
        base: Path,
        *,
        combatant_guid: str = PLAYER_GUID,
        combatant_names: tuple[str, ...] | None = None,
        combatant_hero_classes: tuple[str, ...] | None = None,
        combatant_races: tuple[str, ...] | None = None,
        combatant_guild_names: tuple[str | None, ...] | None = None,
        event_player_guid: str = PLAYER_GUID,
        extra_combatants: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, Path | str]:
        data_root = base / "offline_data"
        raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
        streams: dict[str, object] = {}
        stream_paths: dict[str, Path] = {}
        for index, stream_type in enumerate(CORE_STREAM_TYPES, 1):
            reference, path = self._write_object(
                raw_root,
                _framed_stream(
                    _event_message(
                        stream_type, index, player_guid=event_player_guid
                    )
                ),
                suffix=f"{stream_type}.events.gz",
            )
            streams[stream_type] = {"status": "AVAILABLE", "object": reference}
            stream_paths[stream_type] = path

        combatant_reference, combatant_path = self._write_object(
            raw_root,
            _combatant_info_stream(
                guid=combatant_guid,
                names=combatant_names,
                hero_classes=combatant_hero_classes,
                races=combatant_races,
                guild_names=combatant_guild_names,
                extra_combatants=extra_combatants,
            ),
            suffix="combatant_info.events.gz",
        )
        streams["combatant_info"] = {
            "status": "AVAILABLE",
            "object": combatant_reference,
        }
        metadata = {
            "id": INSTANCE_ID,
            "slug": "slug-1",
            "name": "Upper Tower of Karazhan",
            "guild": {"id": "guild-1", "name": "南北"},
            "encounters": [
                {
                    "id": ENCOUNTER_ID,
                    "instance_id": INSTANCE_ID,
                    "start_time": STARTED_AT,
                }
            ],
            "players": {
                PLAYER_GUID: {
                    "name": "Alice",
                    "class": "WARRIOR",
                    "race": "Human",
                    "level": 60,
                }
            },
        }
        metadata_reference, metadata_path = self._write_object(
            raw_root,
            _canonical_bytes(metadata),
            suffix="metadata.json",
        )
        raw_manifest = {
            "schema": "chronicle_external_api_ingest/v1",
            "kind": "chronicle_external_api_raw_snapshot",
            "implementation_revision": ingest_v1.IMPLEMENTATION_REVISION,
            "parser_contract_revision": ingest_v1.PARSER_CONTRACT_REVISION,
            "instances": [
                {
                    "instance_id": INSTANCE_ID,
                    "slug": "slug-1",
                    "instance_name": "Upper Tower of Karazhan",
                    "metadata": {"object": metadata_reference},
                    "streams": streams,
                    "started_at": STARTED_AT,
                    "started_at_source": "metadata.encounters[].start_time",
                    "started_at_source_field": "start_time",
                    "uploaded_at": None,
                    "uploaded_at_source": None,
                    "uploaded_at_source_field": None,
                    "contamination_guild_context": "南北",
                    "contamination_guild_evidence": "metadata.guild.name",
                    "instance_contamination_label": "POSTFIX_KNOWN_CLEAN",
                    "warrior_observations": [
                        {
                            "player_guid": PLAYER_GUID,
                            "player_name": "Alice",
                            "player_class": "Warrior",
                            "player_spec": "Fury",
                            "spec_evidence_status": "OBSERVED",
                            "contamination_label": "POSTFIX_KNOWN_CLEAN",
                            "contamination_guild_context": "南北",
                            "contamination_guild_evidence": "metadata.guild.name",
                            "field_conflicts": {},
                            "sources": ["instance_metadata"],
                        }
                    ],
                    "warrior_spec_counts": {
                        "Arms": 0,
                        "Fury": 1,
                        "Other_or_unknown": 0,
                    },
                }
            ],
        }
        raw_payload = _canonical_bytes(raw_manifest) + b"\n"
        raw_sha = _sha256(raw_payload)
        raw_manifest_path = raw_root / "manifests" / f"{raw_sha}.json"
        raw_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        raw_manifest_path.write_bytes(raw_payload)

        normalized_dir = (
            data_root / "derived" / "chronicle_external_core_events" / "v1"
        )
        normalized = build_external_core_event_normalization(
            source_manifest_path=raw_manifest_path,
            data_root=data_root,
            output_directory=normalized_dir,
        )
        return {
            "data_root": data_root,
            "raw_root": raw_root,
            "raw_manifest": raw_manifest_path,
            "raw_sha": raw_sha,
            "normalization_manifest": Path(
                normalized["content_addressed_manifest_path"]
            ),
            "normalized_partition": Path(normalized["partitions"][0]),
            "metadata_object": metadata_path,
            "combatant_object": combatant_path,
            "damage_object": stream_paths["damage"],
        }

    def _two_instance_fixture(self, base: Path) -> dict[str, Path | str]:
        fixture = self._fixture(base)
        raw_root = Path(fixture["raw_root"])
        raw_manifest = json.loads(
            Path(fixture["raw_manifest"]).read_text("utf-8")
        )
        streams: dict[str, object] = {}
        for index, stream_type in enumerate(CORE_STREAM_TYPES, 1):
            payload = _framed_stream(
                _event_message(
                    stream_type,
                    index,
                    player_guid=PLAYER_GUID_2,
                    mob_guid=MOB_GUID_2,
                ),
                encounter_id=ENCOUNTER_ID_2,
                first_timestamp_ms=FIRST_TIMESTAMP_MS_2,
            )
            reference, _ = self._write_object(
                raw_root, payload, suffix=f"second.{stream_type}.events.gz"
            )
            streams[stream_type] = {"status": "AVAILABLE", "object": reference}
        combatant_reference, _ = self._write_object(
            raw_root,
            _combatant_info_stream(
                guid=PLAYER_GUID_2,
                encounter_id=ENCOUNTER_ID_2,
                first_timestamp_ms=FIRST_TIMESTAMP_MS_2,
                name="Bob",
            ),
            suffix="second.combatant_info.events.gz",
        )
        streams["combatant_info"] = {
            "status": "AVAILABLE",
            "object": combatant_reference,
        }
        metadata = {
            "id": INSTANCE_ID_2,
            "slug": "slug-2",
            "name": "Upper Tower of Karazhan",
            "guild": None,
            "encounters": [
                {
                    "id": ENCOUNTER_ID_2,
                    "instance_id": INSTANCE_ID_2,
                    "start_time": STARTED_AT_2,
                }
            ],
            "players": {
                PLAYER_GUID_2: {
                    "name": "Bob",
                    "class": "WARRIOR",
                    "race": "Human",
                    "level": 60,
                }
            },
        }
        metadata_reference, _ = self._write_object(
            raw_root, _canonical_bytes(metadata), suffix="second.metadata.json"
        )
        raw_manifest["instances"].append(
            {
                "instance_id": INSTANCE_ID_2,
                "slug": "slug-2",
                "instance_name": "Upper Tower of Karazhan",
                "metadata": {"object": metadata_reference},
                "streams": streams,
                "started_at": STARTED_AT_2,
                "started_at_source": "metadata.encounters[].start_time",
                "started_at_source_field": "start_time",
                "uploaded_at": None,
                "uploaded_at_source": None,
                "uploaded_at_source_field": None,
                "contamination_guild_context": None,
                "contamination_guild_evidence": None,
                "instance_contamination_label": "UNKNOWN_NONVOTING",
                "warrior_observations": [
                    {
                        "player_guid": PLAYER_GUID_2,
                        "player_name": "Bob",
                        "player_class": "Warrior",
                        "player_spec": "Fury",
                        "spec_evidence_status": "OBSERVED",
                        "contamination_label": "UNKNOWN_NONVOTING",
                        "contamination_guild_context": None,
                        "contamination_guild_evidence": None,
                        "field_conflicts": {},
                        "sources": ["instance_metadata"],
                    }
                ],
                "warrior_spec_counts": {
                    "Arms": 0,
                    "Fury": 1,
                    "Other_or_unknown": 0,
                },
            }
        )
        raw_payload = _canonical_bytes(raw_manifest) + b"\n"
        raw_sha = _sha256(raw_payload)
        raw_path = raw_root / "manifests" / f"{raw_sha}.json"
        raw_path.write_bytes(raw_payload)
        normalized_dir = (
            Path(fixture["data_root"])
            / "derived"
            / "chronicle_external_core_events"
            / "v1"
            / "two_instances"
        )
        normalized = build_external_core_event_normalization(
            source_manifest_path=raw_path,
            data_root=fixture["data_root"],
            output_directory=normalized_dir,
        )
        fixture.update(
            {
                "raw_manifest": raw_path,
                "raw_sha": raw_sha,
                "normalization_manifest": Path(
                    normalized["content_addressed_manifest_path"]
                ),
            }
        )
        return fixture

    def _build(self, fixture: dict[str, Path | str], output: Path) -> dict[str, object]:
        return build_external_reconstruction_admission(
            raw_manifest_path=fixture["raw_manifest"],
            normalization_manifest_path=fixture["normalization_manifest"],
            data_root=fixture["data_root"],
            output_directory=output,
        )

    def _rewrite_normalization(
        self,
        manifest_path: Path,
        mutator: object,
    ) -> Path:
        document = json.loads(manifest_path.read_text("utf-8"))
        document.pop("content_address")
        mutator(document)
        content_sha = _sha256(_canonical_bytes(document))
        document["content_address"] = {
            "algorithm": "sha256",
            "scope": "canonical_JSON_excluding_content_address",
            "sha256": content_sha,
        }
        payload = _canonical_bytes(document) + b"\n"
        rewritten = manifest_path.with_name(
            f"chronicle_external_core_events_v1.{content_sha}.manifest.json"
        )
        rewritten.write_bytes(payload)
        return rewritten

    def test_build_is_content_addressed_manifest_last_and_scientifically_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            first = self._build(fixture, output)
            second = self._build(fixture, output)

            self.assertEqual(first["status"], STATUS)
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first["manifest_file_sha256"], second["manifest_file_sha256"])
            self.assertEqual(first["record_count"], len(CORE_STREAM_TYPES))
            self.assertFalse(first["comparison_authorized"])
            manifest = json.loads(Path(first["manifest_path"]).read_text("utf-8"))
            self.assertEqual(manifest["schema"], SCHEMA)
            self.assertEqual(manifest["status"], STATUS)
            self.assertTrue(
                manifest["publication_contract"]["stable_manifest_committed_last"]
            )
            instance = manifest["instances"][0]
            self.assertEqual(instance["source_evidence"]["kind"], SOURCE_EVIDENCE_KIND)
            self.assertEqual(
                instance["temporal_and_guild_provenance"]["started_at"], STARTED_AT
            )
            self.assertEqual(
                instance["temporal_and_guild_provenance"]["contamination"]["label"],
                "POSTFIX_KNOWN_CLEAN",
            )
            self.assertEqual(instance["metadata_player_resolver"]["player_count"], 1)
            self.assertEqual(instance["combatant_info_evidence"]["message_count"], 1)
            self.assertEqual(
                instance["warrior_spec_evidence"]["recomputed_counts"]["Fury"], 1
            )
            consumer = instance["consumer_status"]
            self.assertTrue(consumer["versioned_reconstruction_input"])
            self.assertFalse(consumer["legacy_manual_export_queue_entry"])
            self.assertFalse(consumer["legacy_raw_csv_provenance"])
            self.assertFalse(consumer["frozen_50_capsule_member"])
            self.assertFalse(consumer["comparison_authorized"])
            self.assertEqual(manifest["summary"]["raw_object_copy_count"], 0)
            self.assertEqual(manifest["summary"]["normalized_row_copy_count"], 0)
            self.assertTrue(
                manifest["admission_validation_contract"][
                    "normalized_rows_byte_exact_canonical_rederived"
                ]
            )
            self.assertFalse(
                manifest["admission_validation_contract"][
                    "slain_attribution_projected_to_value"
                ]
            )

    def test_streaming_adapter_preserves_rows_and_adds_only_exact_identity_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            result = self._build(fixture, output)
            source_rows = [
                json.loads(line)
                for line in gzip.decompress(
                    Path(fixture["normalized_partition"]).read_bytes()
                ).splitlines()
            ]
            adapted = list(
                iter_versioned_reconstruction_rows(result["manifest_path"])
            )
            self.assertEqual(len(adapted), len(source_rows))
            for source, row in zip(source_rows, adapted, strict=True):
                added = row["external_admission"]
                original_fields = {
                    key: value for key, value in row.items() if key != "external_admission"
                }
                self.assertEqual(original_fields, source)
                self.assertFalse(added["hostile_classification_inferred"])
                self.assertFalse(added["legacy_outcome_rewritten"])

            class_source = next(row for row in source_rows if row["type"] == "CLASS")
            class_row = next(row for row in adapted if row["type"] == "CLASS")
            evidence = class_row["external_admission"]
            self.assertEqual(
                evidence["classification"]["entity_resolution"],
                "METADATA_PLAYER_EXACT",
            )
            self.assertEqual(
                evidence["classification"]["semantic"], "UNKNOWN_NONVOTING"
            )
            self.assertFalse(evidence["classification"]["semantic_label_inferred"])
            self.assertEqual(
                evidence["classification"]["official_affiliation_numeric"], 2
            )
            self.assertEqual(
                evidence["target_player"]["evidence"],
                "EXACT_METADATA_PLAYER_GUID_MATCH",
            )
            self.assertEqual(
                evidence["owner"]["canonical_hex_without_optional_0x"],
                OWNER_WITHOUT_PREFIX,
            )
            self.assertEqual(evidence["owner"]["resolved_player"]["guid"], PLAYER_GUID)
            self.assertEqual(class_row["outcome"], class_source["outcome"])
            damage = next(row for row in adapted if row["type"] == "DMG")
            self.assertEqual(damage["external_admission"]["source_player"]["guid"], PLAYER_GUID)
            self.assertIsNone(damage["external_admission"]["target_player"])
            self.assertIsNone(next(row for row in adapted if row["type"] == "DEAD")["value"])

    def test_two_instances_are_admitted_and_streamed_without_cross_join(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._two_instance_fixture(Path(temporary))
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
                / "two_instances"
            )
            result = self._build(fixture, output)
            self.assertEqual(result["instance_count"], 2)
            parallel = build_external_reconstruction_admission(
                raw_manifest_path=fixture["raw_manifest"],
                normalization_manifest_path=fixture["normalization_manifest"],
                data_root=fixture["data_root"],
                output_directory=output.with_name("two_instances_parallel"),
                workers=2,
            )
            self.assertEqual(result["content_sha256"], parallel["content_sha256"])
            self.assertEqual(
                result["manifest_file_sha256"], parallel["manifest_file_sha256"]
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            self.assertEqual(
                [row["instance_id"] for row in manifest["instances"]],
                [INSTANCE_ID, INSTANCE_ID_2],
            )
            self.assertEqual(
                manifest["instances"][0]["temporal_and_guild_provenance"][
                    "contamination"
                ]["label"],
                "POSTFIX_KNOWN_CLEAN",
            )
            self.assertEqual(
                manifest["instances"][1]["temporal_and_guild_provenance"][
                    "contamination"
                ]["label"],
                "UNKNOWN_NONVOTING",
            )
            rows = list(iter_versioned_reconstruction_rows(result["manifest_path"]))
            self.assertEqual(len(rows), 2 * len(CORE_STREAM_TYPES))
            first_ids = {row["instance"] for row in rows[: len(CORE_STREAM_TYPES)]}
            second_ids = {row["instance"] for row in rows[len(CORE_STREAM_TYPES) :]}
            self.assertEqual(first_ids, {INSTANCE_ID})
            self.assertEqual(second_ids, {INSTANCE_ID_2})
            first_damage = next(
                row for row in rows if row["instance"] == INSTANCE_ID and row["type"] == "DMG"
            )
            second_damage = next(
                row for row in rows if row["instance"] == INSTANCE_ID_2 and row["type"] == "DMG"
            )
            self.assertEqual(
                first_damage["external_admission"]["source_player"]["guid"],
                PLAYER_GUID,
            )
            self.assertEqual(
                second_damage["external_admission"]["source_player"]["guid"],
                PLAYER_GUID_2,
            )

    def test_two_instance_partition_set_mismatch_fails_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._two_instance_fixture(Path(temporary))
            original = Path(fixture["normalization_manifest"])

            def remove_second(document: dict[str, object]) -> None:
                document["partitions"] = document["partitions"][:1]

            fixture["normalization_manifest"] = self._rewrite_normalization(
                original, remove_second
            )
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
                / "mismatch"
            )
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "exact set match"
            ):
                self._build(fixture, output)
            self.assertFalse((output / "manifest.json").exists())

    def test_optional_0x_owner_normalization_preserves_leading_zeroes(self) -> None:
        self.assertEqual(
            canonicalize_optional_0x_owner("0x0000aB01"), "0000AB01"
        )
        self.assertEqual(canonicalize_optional_0x_owner("0000Ab01"), "0000AB01")
        self.assertIsNone(canonicalize_optional_0x_owner(None))
        with self.assertRaises(ChronicleExternalAdmissionError):
            canonicalize_optional_0x_owner("pet-1")
        with self.assertRaises(ChronicleExternalAdmissionError):
            canonicalize_optional_0x_owner(" 0x0000AB01")

    def test_raw_object_tamper_fails_without_replacing_stable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            output.mkdir(parents=True)
            stable = output / "manifest.json"
            stable.write_bytes(b"previous-stable\n")
            damage = Path(fixture["damage_object"])
            damage.write_bytes(damage.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "hash/size verification failed"
            ):
                self._build(fixture, output)
            self.assertEqual(stable.read_bytes(), b"previous-stable\n")

    def test_normalized_compressed_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            partition = Path(fixture["normalized_partition"])
            partition.write_bytes(partition.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "compressed hash/size"
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1",
                )

    def test_logical_hash_and_raw_manifest_binding_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            original = Path(fixture["normalization_manifest"])
            bad_logical = self._rewrite_normalization(
                original,
                lambda document: document["partitions"][0].__setitem__(
                    "logical_content_sha256", "0" * 64
                ),
            )
            fixture["normalization_manifest"] = bad_logical
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "logical SHA"
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1"
                    / "logical",
                )

            bad_source = self._rewrite_normalization(
                original,
                lambda document: document["source"].__setitem__(
                    "manifest_sha256", "f" * 64
                ),
            )
            fixture["normalization_manifest"] = bad_source
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "not bound"
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1"
                    / "source",
                )

    def test_self_consistent_forged_projection_is_rejected_by_raw_rederive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            original_partition = Path(fixture["normalized_partition"])
            rows = [
                json.loads(line)
                for line in gzip.decompress(original_partition.read_bytes()).splitlines()
            ]
            rows[0]["value"] = 999_999
            logical = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
            compressed = gzip.compress(logical, mtime=0)
            forged_partition = original_partition.with_name("forged.jsonl.gz")
            forged_partition.write_bytes(compressed)

            def forge(document: dict[str, object]) -> None:
                partition = document["partitions"][0]
                partition["partition"] = forged_partition.name
                partition["compressed_size_bytes"] = len(compressed)
                partition["compressed_file_sha256"] = _sha256(compressed)
                partition["logical_content_sha256"] = _sha256(logical)

            forged_manifest = self._rewrite_normalization(
                Path(fixture["normalization_manifest"]), forge
            )
            fixture["normalization_manifest"] = forged_manifest
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError,
                "exact canonical raw-derived projection",
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1"
                    / "forged",
                )

    def test_missing_combatant_info_player_with_exact_core_events_is_diagnostic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary), combatant_guid="0x00000000000000B2"
            )
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            result = self._build(fixture, output)
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            evidence = manifest["instances"][0]["combatant_info_evidence"]
            self.assertFalse(evidence["metadata_player_guid_set_equal"])
            self.assertFalse(
                evidence["metadata_player_guid_subset_of_combatant_info"]
            )
            self.assertEqual(
                evidence["missing_metadata_combatant_info_player_count"], 1
            )
            self.assertEqual(
                manifest["summary"][
                    "missing_metadata_combatant_info_player_count"
                ],
                1,
            )
            missing = evidence["missing_metadata_combatant_info_players"][0]
            self.assertEqual(missing["guid"], PLAYER_GUID)
            self.assertEqual(missing["metadata_name"], "Alice")
            self.assertEqual(missing["metadata_hero_class"], "WARRIOR")
            self.assertEqual(missing["metadata_race"], "Human")
            self.assertEqual(
                missing["identity_resolution"],
                "EXACT_METADATA_GUID_WITH_VERIFIED_CORE_EVENT_PRESENCE",
            )
            observation = missing["verified_core_event_evidence"]
            self.assertEqual(
                observation["source_or_target_event_count"], len(CORE_STREAM_TYPES)
            )
            self.assertEqual(observation["encounter_count"], 1)
            self.assertEqual(
                sum(observation["event_type_counts"].values()),
                len(CORE_STREAM_TYPES),
            )
            self.assertEqual(
                missing["combatant_info_static_context"]["gear"],
                "UNAVAILABLE_NOT_IMPUTED",
            )
            self.assertEqual(
                missing["combatant_info_static_context"]["spec"],
                "UNKNOWN_NOT_INFERRED_FROM_MISSING_COMBATANT_INFO",
            )
            # Exercise the strict loader and row adapter: exact metadata GUID
            # attribution remains available even when CombatantInfo is absent.
            rows = list(iter_versioned_reconstruction_rows(result["manifest_path"]))
            self.assertEqual(len(rows), len(CORE_STREAM_TYPES))
            self.assertTrue(
                any(
                    row["external_admission"]["source_player"] is not None
                    for row in rows
                )
            )

    def test_missing_combatant_info_player_without_core_events_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                combatant_guid=PLAYER_GUID_2,
                event_player_guid=PLAYER_GUID_2,
            )
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError,
                "lacks verified core-event source/target evidence",
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1",
                )

    def test_transient_unknown_display_name_is_diagnostic_not_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary), combatant_names=("未知目标", "Alice")
            )
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            result = self._build(fixture, output)
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            instance = manifest["instances"][0]
            evidence = instance["combatant_info_evidence"]
            self.assertEqual(evidence["display_name_transition_player_count"], 1)
            self.assertEqual(evidence["display_race_transition_player_count"], 0)
            self.assertEqual(evidence["display_guild_transition_player_count"], 0)
            contract = evidence["identity_resolution_contract"]
            self.assertFalse(
                contract["display_name_used_for_identity_or_attribution"]
            )
            self.assertEqual(evidence["identity_conflict_count"], 0)
            self.assertTrue(evidence["metadata_player_guid_set_equal"])
            self.assertEqual(instance["metadata_player_resolver"]["player_count"], 1)
            diagnostic = evidence["display_transition_diagnostics"]["name"][0]
            self.assertEqual(diagnostic["guid"], PLAYER_GUID)
            self.assertEqual(diagnostic["metadata_value"], "Alice")
            self.assertEqual(diagnostic["sequential_transition_count"], 1)

    def test_display_race_transition_is_diagnostic_when_metadata_race_appears(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                combatant_names=("Alice", "Alice"),
                combatant_races=("Scourge", "Human"),
            )
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            result = self._build(fixture, output)
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            evidence = manifest["instances"][0]["combatant_info_evidence"]
            self.assertEqual(evidence["metadata_race_observed_player_count"], 1)
            self.assertEqual(evidence["hero_class_transition_player_count"], 0)
            self.assertEqual(evidence["display_race_transition_player_count"], 1)
            self.assertEqual(
                manifest["summary"]["display_race_transition_player_count"], 1
            )
            contract = evidence["identity_resolution_contract"]
            self.assertTrue(
                contract["combatant_info_present_metadata_race_must_appear"]
            )
            self.assertTrue(
                contract[
                    "metadata_race_appearance_is_integrity_check_not_attribution"
                ]
            )
            self.assertFalse(
                contract["display_race_used_for_identity_or_attribution"]
            )
            diagnostic = evidence["display_transition_diagnostics"]["race"][0]
            self.assertEqual(diagnostic["guid"], PLAYER_GUID)
            self.assertEqual(diagnostic["metadata_value"], "Human")
            self.assertEqual(diagnostic["sequential_transition_count"], 1)
            self.assertEqual(
                {
                    row["value"]: row["message_count"]
                    for row in diagnostic["observed_value_counts"]
                },
                {"Scourge": 1, "Human": 1},
            )
            # Exercise the strict loader, including aggregate transition counts.
            self.assertEqual(
                len(list(iter_versioned_reconstruction_rows(result["manifest_path"]))),
                len(CORE_STREAM_TYPES),
            )

    def test_true_hero_class_transition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                combatant_names=("Alice", "Alice"),
                combatant_hero_classes=("Priest", "Warrior"),
            )
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "hero_class_transition"
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1",
                )

    def test_metadata_race_must_appear_even_though_display_race_is_nonattributing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                combatant_names=("Alice", "Alice"),
                combatant_races=("Scourge", "Scourge"),
            )
            with self.assertRaisesRegex(
                ChronicleExternalAdmissionError, "metadata_race_not_observed"
            ):
                self._build(
                    fixture,
                    Path(fixture["data_root"])
                    / "derived"
                    / "chronicle_external_reconstruction_admission"
                    / "v1",
                )

    def test_late_joiner_is_hashed_nonresolver_without_expanding_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                extra_combatants=((PLAYER_GUID_2, "LateJoiner"),),
            )
            output = (
                Path(fixture["data_root"])
                / "derived"
                / "chronicle_external_reconstruction_admission"
                / "v1"
            )
            result = self._build(fixture, output)
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            instance = manifest["instances"][0]
            evidence = instance["combatant_info_evidence"]
            self.assertFalse(evidence["metadata_player_guid_set_equal"])
            self.assertTrue(evidence["metadata_player_guid_subset_of_combatant_info"])
            self.assertEqual(evidence["nonmetadata_combatant_guid_count"], 1)
            self.assertEqual(
                evidence["nonmetadata_combatant_guids_sha256"],
                _sha256(_canonical_bytes([PLAYER_GUID_2])),
            )
            self.assertEqual(evidence["nonmetadata_combatants_added_to_player_resolver"], 0)
            resolver_guids = {
                row["guid"] for row in instance["metadata_player_resolver"]["players"]
            }
            self.assertEqual(resolver_guids, {PLAYER_GUID})


if __name__ == "__main__":
    unittest.main()
