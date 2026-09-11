from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps.chronicle_external_event_normalizer_v1 import (
    CORE_STREAM_TYPES,
    ChronicleExternalEventNormalizerError,
    ChronicleExternalWireError,
    RECORD_SCHEMA,
    SCHEMA,
    build_external_core_event_normalization,
    decode_damage,
    decode_event_stream,
)


def _varint(value: int) -> bytes:
    if value < 0:
        value &= 0xFFFFFFFFFFFFFFFF
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


def _activity(guid: str = "0xA1", event_type: str = "start") -> bytes:
    return _sfield(1, guid) + _sfield(2, event_type)


def _meta(index: int, offset: int, *, synthetic: bool = False) -> bytes:
    return (
        _vfield(1, index)
        + _vfield(2, offset)
        + _bfield(3, _activity())
        + (_vfield(4, 1) if synthetic else b"")
    )


def _spell(spell_id: int = 23894, name: str = "Bloodthirst") -> bytes:
    return _vfield(1, spell_id) + _sfield(2, name) + _vfield(3, 17)


def _damage(index: int = 7, offset: int = 10, *, unknown: bool = False) -> bytes:
    spell = _spell() + (_bfield(77, b"future") if unknown else b"")
    return (
        _bfield(1, _meta(index, offset, synthetic=True))
        + _sfield(3, "0xC0FFEE")
        + _sfield(4, "Bloodthirst")
        + _sfield(5, "0xBADD00D")
        + _vfield(6, 4)
        + _vfield(7, 1234)
        + _vfield(8, 2)
        + _bfield(9, _vfield(1, 11) + _vfield(2, 8))
        + _bfield(10, spell)
        + _vfield(11, 34)
        + (_vfield(99, 123) if unknown else b"")
    )


def _heal(index: int = 9, offset: int = 5) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(3, "0xHEALER")
        + _sfield(4, "0xTARGET")
        + _sfield(5, "Flash Heal")
        + _vfield(6, 456)
        + _vfield(7, 2)
        + _bfield(8, _spell(2061, "Flash Heal"))
        + _vfield(9, 3)
        + _vfield(10, 12)
        + _vfield(11, 3)
    )


def _slain(index: int = 10, offset: int = 6) -> bytes:
    attribution = _damage(index, offset)
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(2, "0xDEAD")
        + _sfield(3, "0xKILLER")
        + _bfield(4, attribution)
    )


def _spell_start(index: int = 8, offset: int = 5) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _vfield(2, 42)
        + _bfield(3, _spell(1464, "Slam"))
        + _sfield(4, "0xWARRIOR")
        + _sfield(5, "0xMOB")
        + _vfield(6, 3)
        + _vfield(7, 1500)
        + _vfield(8, 0)
        + _vfield(9, 1)
    )


def _spell_go(index: int = 11, offset: int = 7) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _bfield(3, _spell(1680, "Whirlwind"))
        + _sfield(4, "0xWARRIOR")
        + _sfield(5, "0xMOB")
        + _vfield(6, 2)
        + _vfield(7, 1)
    )


def _spell_fail(index: int = 12, offset: int = 8) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(2, "0xWARRIOR")
        + _bfield(3, _spell(5308, "Execute"))
        + _vfield(4, 1)
    )


def _classification(index: int = 1, offset: int = 1) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(2, "0xPET")
        + _vfield(3, 7)
        + _vfield(4, 2)
        + _sfield(5, "0xOWNER")
        + _sfield(6, "0xCONTROLLER")
        + _vfield(7, 123)
    )


MESSAGES = {
    "damage": _damage,
    "heal": _heal,
    "slain": _slain,
    "spell_start": _spell_start,
    "spell_go": _spell_go,
    "spell_fail": _spell_fail,
    "unit_classification": _classification,
}


def _event_stream(
    messages: list[bytes],
    *,
    encounter: str = "encounter-1",
    origin: int = 1_700_000_000_000,
    declared_count: int | None = None,
    trailing_body: bytes = b"",
) -> bytes:
    body = b"".join(_varint(len(message)) + message for message in messages)
    body += trailing_body
    frame = (
        _varint(len(encounter.encode("utf-8")))
        + encounter.encode("utf-8")
        + _varint(origin)
        + _varint(len(messages) if declared_count is None else declared_count)
        + _varint(len(body))
        + body
    )
    return gzip.compress(frame, mtime=0)


class ChronicleExternalEventNormalizerTests(unittest.TestCase):
    def _raw_fixture(
        self,
        base: Path,
        *,
        instance_count: int = 1,
        corrupt_second: bool = False,
    ) -> tuple[Path, Path, Path]:
        data_root = base / "offline_data"
        raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
        object_root = raw_root / "objects" / "sha256"
        instances = []
        for instance_number in range(instance_count):
            streams = {}
            for stream_type in CORE_STREAM_TYPES:
                payload = _event_stream([MESSAGES[stream_type]()])
                digest = hashlib.sha256(payload).hexdigest()
                path = (
                    object_root
                    / digest[:2]
                    / f"{digest}.{stream_type}.events.gz"
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                streams[stream_type] = {
                    "status": "AVAILABLE",
                    "object": {
                        "sha256": digest,
                        "size_bytes": len(payload),
                        "media_type": "application/octet-stream",
                        "relative_path": path.relative_to(raw_root).as_posix(),
                    },
                }
            if corrupt_second and instance_number == 1:
                streams["heal"]["object"]["sha256"] = "0" * 64
            instances.append(
                {
                    "instance_id": f"instance-{instance_number + 1}",
                    "slug": f"slug-{instance_number + 1}",
                    "streams": streams,
                }
            )
        manifest = {
            "schema": "chronicle_external_api_ingest/v1",
            "implementation_revision": ingest_v1.IMPLEMENTATION_REVISION,
            "parser_contract_revision": ingest_v1.PARSER_CONTRACT_REVISION,
            "instances": instances,
        }
        payload = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        manifest_path = raw_root / "manifests" / f"{digest}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(payload)
        return data_root, raw_root, manifest_path

    def test_damage_decoder_preserves_fixed_fields_and_unknown_wire_bytes(self) -> None:
        message = _damage(unknown=True)
        decoded = decode_damage(message)
        self.assertEqual(decoded["meta"]["event_index"], 7)
        self.assertEqual(decoded["meta"]["offset_ms"], 10)
        self.assertTrue(decoded["meta"]["is_synthetic"])
        self.assertEqual(decoded["caster"], "0xC0FFEE")
        self.assertEqual(decoded["target"], "0xBADD00D")
        self.assertEqual(decoded["amount"], 1234)
        self.assertEqual(decoded["school"], {"number": 2, "name": "Physical"})
        self.assertEqual(decoded["spell_data"]["id"], 23894)
        self.assertEqual(decoded["tailers"], [{"amount": 11, "hit_type": 8}])
        outer = decoded["unknown_fields"][0]
        nested = decoded["spell_data"]["unknown_fields"][0]
        self.assertEqual(base64.b64decode(outer["raw_field_base64"]), _vfield(99, 123))
        self.assertEqual(base64.b64decode(nested["raw_field_base64"]), _bfield(77, b"future"))

    def test_all_core_stream_synthetic_wire_fixtures_decode(self) -> None:
        for stream_type in CORE_STREAM_TYPES:
            with self.subTest(stream_type=stream_type):
                frames = decode_event_stream(
                    _event_stream([MESSAGES[stream_type]()]),
                    stream_type=stream_type,
                )
                self.assertEqual(len(frames), 1)
                self.assertEqual(frames[0]["encounter_id"], "encounter-1")
                self.assertEqual(len(frames[0]["messages"]), 1)
                self.assertIsNotNone(frames[0]["messages"][0]["event"]["meta"])

    def test_framing_enforces_count_and_data_length(self) -> None:
        with self.assertRaisesRegex(
            ChronicleExternalWireError, "message count and data_length"
        ):
            decode_event_stream(
                _event_stream([_damage()], trailing_body=b"x"),
                stream_type="damage",
            )
        with self.assertRaises(ChronicleExternalWireError):
            decode_event_stream(
                _event_stream([_damage()], declared_count=2),
                stream_type="damage",
            )

    def test_missing_meta_and_known_field_wrong_wire_type_fail_closed(self) -> None:
        without_meta = _sfield(3, "0xC") + _sfield(5, "0xT")
        with self.assertRaisesRegex(ChronicleExternalWireError, "lacks EventMeta"):
            decode_event_stream(
                _event_stream([without_meta]), stream_type="damage"
            )
        with self.assertRaisesRegex(ChronicleExternalWireError, "wire type"):
            decode_damage(_sfield(7, "not-an-int"))

    def test_build_is_deterministic_content_addressed_and_bridge_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, raw_root, source_manifest = self._raw_fixture(base)
            output = data_root / "derived" / "core"
            first = build_external_core_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=output,
            )
            second = build_external_core_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=output,
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(first["manifest_file_sha256"], second["manifest_file_sha256"])
            self.assertEqual(first["record_count"], 7)
            self.assertEqual(first["raw_object_copy_count"], 0)
            self.assertEqual(first["network_request_count"], 0)

            manifest = json.loads(Path(first["manifest_path"]).read_text("utf-8"))
            self.assertEqual(manifest["schema"], SCHEMA)
            self.assertTrue(manifest["normalization_contract"]["manifest_committed_last"])
            self.assertEqual(manifest["summary"]["raw_object_copy_count"], 0)
            partition = Path(first["partitions"][0])
            compressed = partition.read_bytes()
            self.assertEqual(
                hashlib.sha256(compressed).hexdigest(),
                manifest["partitions"][0]["compressed_file_sha256"],
            )
            logical = gzip.decompress(compressed)
            self.assertEqual(
                hashlib.sha256(logical).hexdigest(),
                manifest["partitions"][0]["logical_content_sha256"],
            )
            rows = [json.loads(line) for line in logical.splitlines()]
            self.assertEqual(
                [row["type"] for row in rows],
                ["CLASS", "START", "HEAL", "DEAD", "GO", "FAIL", "DMG"],
            )
            self.assertEqual([row["provenance"]["csv_line"] for row in rows], list(range(1, 8)))
            self.assertTrue(all(row["schema"] == RECORD_SCHEMA for row in rows))
            self.assertEqual(rows[0]["target_guid"], "0xPET")
            self.assertIn("owner=0xOWNER", rows[0]["outcome"])
            dead = next(row for row in rows if row["type"] == "DEAD")
            self.assertIsNone(dead["value"])
            self.assertIsNotNone(dead["official"]["message"]["attribution"])
            self.assertNotIn(str(raw_root), logical.decode("utf-8"))

    def test_worker_count_does_not_change_partition_or_manifest_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, _, source_manifest = self._raw_fixture(
                base, instance_count=2
            )
            serial = build_external_core_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=data_root / "derived" / "serial",
                workers=1,
            )
            parallel = build_external_core_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=data_root / "derived" / "parallel",
                workers=2,
            )

            self.assertEqual(serial["worker_count"], 1)
            self.assertEqual(parallel["worker_count"], 2)
            self.assertEqual(serial["content_sha256"], parallel["content_sha256"])
            self.assertEqual(
                serial["manifest_file_sha256"], parallel["manifest_file_sha256"]
            )
            serial_manifest = json.loads(Path(serial["manifest_path"]).read_text("utf-8"))
            parallel_manifest = json.loads(
                Path(parallel["manifest_path"]).read_text("utf-8")
            )
            self.assertEqual(serial_manifest, parallel_manifest)
            self.assertEqual(
                [row["compressed_file_sha256"] for row in serial_manifest["partitions"]],
                [
                    row["compressed_file_sha256"]
                    for row in parallel_manifest["partitions"]
                ],
            )

    def test_worker_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, _, source_manifest = self._raw_fixture(base)
            for workers in (0, 33):
                with self.subTest(workers=workers):
                    with self.assertRaisesRegex(
                        ChronicleExternalEventNormalizerError, "workers must be in 1..32"
                    ):
                        build_external_core_event_normalization(
                            source_manifest_path=source_manifest,
                            data_root=data_root,
                            output_directory=data_root / "derived" / str(workers),
                            workers=workers,
                        )
            with self.assertRaisesRegex(TypeError, "workers must be an integer"):
                build_external_core_event_normalization(
                    source_manifest_path=source_manifest,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "bool",
                    workers=True,
                )

    def test_opt_in_stale_cleanup_is_narrow_and_not_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, _, source_manifest = self._raw_fixture(base)
            output = data_root / "derived" / "core"
            output.mkdir(parents=True)
            stale = output / ".interrupted.core-events.token.jsonl.gz.tmp"
            stale.write_bytes(b"stale")
            preserved = output / "manual.tmp"
            preserved.write_bytes(b"keep")

            result = build_external_core_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=output,
                clean_stale_temporaries=True,
            )

            self.assertFalse(stale.exists())
            self.assertEqual(preserved.read_bytes(), b"keep")
            self.assertEqual(
                result["stale_temporary_cleanup"],
                {"requested": True, "removed_count": 1, "removed_bytes": 5},
            )
            manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
            self.assertNotIn("stale_temporary_cleanup", manifest)

    def test_hash_mismatch_is_rejected_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, raw_root, source_manifest = self._raw_fixture(base)
            source = json.loads(source_manifest.read_text("utf-8"))
            reference = source["instances"][0]["streams"]["damage"]["object"]
            object_path = raw_root / reference["relative_path"]
            object_path.write_bytes(object_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalEventNormalizerError, "hash/size verification failed"
            ):
                build_external_core_event_normalization(
                    source_manifest_path=source_manifest,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "core",
                )

    def test_manifest_is_not_committed_after_late_instance_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, _, source_manifest = self._raw_fixture(
                base, instance_count=2, corrupt_second=True
            )
            output = data_root / "derived" / "core"
            output.mkdir(parents=True)
            stable = output / "manifest.json"
            stable.write_bytes(b"previous-stable-manifest\n")
            with self.assertRaises(ChronicleExternalEventNormalizerError):
                build_external_core_event_normalization(
                    source_manifest_path=source_manifest,
                    data_root=data_root,
                    output_directory=output,
                )
            self.assertEqual(stable.read_bytes(), b"previous-stable-manifest\n")
            self.assertEqual(list(output.glob("*.tmp")), [])

    def test_source_manifest_must_be_content_addressed_in_raw_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, _, source_manifest = self._raw_fixture(base)
            renamed = source_manifest.with_name("0" * 64 + ".json")
            renamed.write_bytes(source_manifest.read_bytes())
            with self.assertRaisesRegex(
                ChronicleExternalEventNormalizerError, "filename hash mismatch"
            ):
                build_external_core_event_normalization(
                    source_manifest_path=renamed,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "core",
                )
            unaddressed = source_manifest.with_name("snapshot.json")
            unaddressed.write_bytes(source_manifest.read_bytes())
            with self.assertRaisesRegex(
                ChronicleExternalEventNormalizerError, "not a SHA-256 content address"
            ):
                build_external_core_event_normalization(
                    source_manifest_path=unaddressed,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "core",
                )

    def test_legacy_raw_revision_requires_local_replay_before_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, _, source_manifest = self._raw_fixture(base)
            legacy = json.loads(source_manifest.read_text("utf-8"))
            legacy["implementation_revision"] = (
                ingest_v1.LEGACY_IMPLEMENTATION_REVISION
            )
            legacy["parser_contract_revision"] = (
                ingest_v1.LEGACY_PARSER_CONTRACT_REVISION
            )
            payload = (
                json.dumps(
                    legacy,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            legacy_path = source_manifest.with_name(
                f"{hashlib.sha256(payload).hexdigest()}.json"
            )
            legacy_path.write_bytes(payload)
            output = data_root / "derived" / "legacy-rejected"
            with self.assertRaisesRegex(
                ChronicleExternalEventNormalizerError,
                "replay it from local raw objects before normalization",
            ):
                build_external_core_event_normalization(
                    source_manifest_path=legacy_path,
                    data_root=data_root,
                    output_directory=output,
                )
            self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
