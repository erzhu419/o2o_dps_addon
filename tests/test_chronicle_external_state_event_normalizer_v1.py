from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps.chronicle_external_state_event_normalizer_v1 import (
    OUTPUT_STATUS,
    RECORD_SCHEMA,
    SCHEMA,
    STATE_STREAM_TYPES,
    ChronicleExternalStateEventNormalizerError,
    ChronicleExternalStateWireError,
    build_external_state_event_normalization,
    decode_aura,
    decode_aura_cast,
    decode_consume,
    decode_event_stream,
    decode_extra_attack,
    decode_resource_change,
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


def _meta(index: int, offset: int, *, unknown: bool = False) -> bytes:
    activity = _sfield(1, "0xACTIVITY") + _sfield(2, "bump")
    return (
        _vfield(1, index)
        + _vfield(2, offset)
        + _bfield(3, activity)
        + _vfield(4, 1)
        + (_vfield(88, 9) if unknown else b"")
    )


def _spell(
    spell_id: int = 23894, name: str = "Bloodthirst", *, unknown: bool = False
) -> bytes:
    return (
        _vfield(1, spell_id)
        + _sfield(2, name)
        + _vfield(3, 17)
        + (_bfield(77, b"future") if unknown else b"")
    )


def _resource_change(
    index: int = 2, offset: int = 20, *, unknown: bool = False
) -> bytes:
    return (
        _bfield(1, _meta(index, offset, unknown=unknown))
        + _sfield(3, "0xWARRIOR")
        + _vfield(4, -25)
        + _sfield(5, "RAGE")
        + _sfield(6, "0xCASTER")
        + _sfield(7, "Bloodthirst")
        + _sfield(8, "LOSS")
        + _bfield(9, _spell(23894, "Bloodthirst", unknown=unknown))
        + _vfield(10, 3)
        + (_vfield(99, 123) if unknown else b"")
    )


def _aura(index: int = 2, offset: int = 10) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(2, "0xWARRIOR")
        + _sfield(3, "Flurry")
        + _vfield(4, 3)
        + _vfield(5, 1)
        + _vfield(6, 3)
        + _bfield(7, _spell(12974, "Flurry"))
        + _vfield(8, 1)
    )


def _aura_cast(index: int = 2, offset: int = 10) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _bfield(2, _spell(12974, "Flurry"))
        + _sfield(3, "0xWARRIOR")
        + _sfield(4, "0xWARRIOR")
        + _vfield(5, 6)
        + _vfield(6, 3000)
        + _vfield(7, 9)
        + _vfield(8, 12000)
        + _vfield(9, 2)
        + _vfield(10, 42)
    )


def _extra_attack(index: int = 1, offset: int = 10) -> bytes:
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(2, "0xWARRIOR")
        + _vfield(3, 2)
        + _sfield(5, "Sword Specialization")
        + _bfield(6, _spell(16459, "Sword Specialization"))
    )


def _consume(index: int = 2, offset: int = 10, *, kind: int = 5) -> bytes:
    packed_candidates = _varint(13442) + _varint(13443)
    return (
        _bfield(1, _meta(index, offset))
        + _sfield(2, "consume-1")
        + _sfield(3, "evidence-1")
        + _sfield(4, "0xWARRIOR")
        + _vfield(5, 13442)
        + _bfield(6, packed_candidates)
        + _vfield(6, 13444)
        + _bfield(7, _spell(17528, "Mighty Rage"))
        + _vfield(8, kind)
        + _vfield(9, 2)
        + _vfield(10, 1_700_000_000_005)
        + _vfield(11, 1_700_000_000_010)
        + _vfield(12, 45)
        + _sfield(13, "RAGE")
        + _vfield(14, 0)
        + _sfield(15, "Mighty Rage Potion")
    )


MESSAGES = {
    "resource_change": _resource_change,
    "aura": _aura,
    "aura_cast": _aura_cast,
    "extra_attack": _extra_attack,
    "consume": _consume,
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


class ChronicleExternalStateEventNormalizerV1Tests(unittest.TestCase):
    def _raw_fixture(
        self,
        base: Path,
        *,
        instance_count: int = 1,
        corrupt_second: bool = False,
        consume_kind: int = 5,
    ) -> tuple[Path, Path, Path]:
        data_root = base / "offline_data"
        raw_root = data_root / "chronicle_raw" / "external_api" / "v1"
        object_root = raw_root / "objects" / "sha256"
        instances = []
        for instance_number in range(instance_count):
            streams = {}
            for stream_type in STATE_STREAM_TYPES:
                if stream_type == "resource_change":
                    message = _resource_change(unknown=True)
                elif stream_type == "consume":
                    message = _consume(kind=consume_kind)
                else:
                    message = MESSAGES[stream_type]()
                payload = _event_stream([message])
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
            # This stream is deliberately unavailable. The state sidecar must
            # not repeat combatant_info admission or attempt to open it.
            streams["combatant_info"] = {"status": "UNAVAILABLE"}
            if corrupt_second and instance_number == 1:
                streams["aura"]["object"]["sha256"] = "0" * 64
            instances.append(
                {
                    "instance_id": f"instance-{instance_number + 1}",
                    "slug": f"slug-{instance_number + 1}",
                    "started_at": "2026-09-03T12:48:06.919Z",
                    "instance_contamination_label": (
                        "NO_KNOWN_RULE_MATCH"
                        if instance_number == 0
                        else "POSTFIX_KNOWN_CLEAN"
                    ),
                    "contamination_guild_context": (
                        "Another Guild" if instance_number == 0 else "南北"
                    ),
                    "contamination_guild_evidence": "metadata.guild.name",
                    "warrior_observations": [
                        {"player_name": "托尼牛"},
                        {"player_name": "桃姬儿"},
                    ],
                    "streams": streams,
                }
            )
        manifest = {
            "schema": "chronicle_external_api_ingest/v1",
            "implementation_revision": ingest_v1.IMPLEMENTATION_REVISION,
            "parser_contract_revision": ingest_v1.PARSER_CONTRACT_REVISION,
            "contamination_contract": ingest_v1._contamination_contract(),
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

    def test_pinned_decoders_preserve_fields_enums_meta_unknown_and_packed_values(
        self,
    ) -> None:
        resource = decode_resource_change(_resource_change(unknown=True))
        self.assertEqual(resource["meta"]["event_index"], 2)
        self.assertEqual(resource["meta"]["offset_ms"], 20)
        self.assertTrue(resource["meta"]["is_synthetic"])
        self.assertEqual(resource["amount"], -25)
        self.assertEqual(resource["resource_type"], "RAGE")
        self.assertEqual(resource["caster"], "0xCASTER")
        self.assertEqual(resource["source_name"], "Bloodthirst")
        self.assertEqual(resource["direction"], "LOSS")
        self.assertEqual(resource["spell_data"]["id"], 23894)
        self.assertEqual(resource["over_resource"], 3)
        self.assertEqual(
            base64.b64decode(resource["unknown_fields"][0]["raw_field_base64"]),
            _vfield(99, 123),
        )
        self.assertEqual(
            base64.b64decode(
                resource["meta"]["unknown_fields"][0]["raw_field_base64"]
            ),
            _vfield(88, 9),
        )
        self.assertEqual(
            base64.b64decode(
                resource["spell_data"]["unknown_fields"][0]["raw_field_base64"]
            ),
            _bfield(77, b"future"),
        )

        aura = decode_aura(_aura())
        self.assertEqual(
            aura["application"], {"number": 1, "name": "ApplicationGains"}
        )
        self.assertEqual(aura["state"], {"number": 3, "name": "StateModified"})
        self.assertEqual(aura["current_amount"], 3)
        self.assertTrue(aura["is_buff"])

        aura_cast = decode_aura_cast(_aura_cast())
        self.assertEqual(aura_cast["spell"]["id"], 12974)
        self.assertEqual(aura_cast["effect"], 6)
        self.assertEqual(aura_cast["amplitude"], 3000)
        self.assertEqual(aura_cast["effect_misc_value"], 9)
        self.assertEqual(aura_cast["duration_ms"], 12000)
        self.assertEqual(aura_cast["cap_status"], 2)
        self.assertEqual(aura_cast["effect_aura_name"], 42)

        extra = decode_extra_attack(_extra_attack())
        self.assertEqual(extra["amount"], 2)
        self.assertEqual(extra["source_name"], "Sword Specialization")

        consume = decode_consume(_consume())
        self.assertEqual(consume["candidate_item_ids"], [13442, 13443, 13444])
        self.assertEqual(
            consume["kind"], {"number": 5, "name": "EvidenceResource"}
        )
        self.assertEqual(
            consume["confidence"],
            {"number": 2, "name": "ConfidenceEffectDerived"},
        )
        self.assertEqual(consume["consumed_at_unix_ms"], 1_700_000_000_005)
        self.assertEqual(consume["observed_at_unix_ms"], 1_700_000_000_010)
        self.assertFalse(consume["is_projection"])

    def test_unknown_enum_is_numeric_only_while_malformed_wire_fails_closed(
        self,
    ) -> None:
        aura = decode_aura(_bfield(1, _meta(1, 1)) + _vfield(5, 99))
        self.assertEqual(
            aura["application"],
            {"number": 99, "name": None, "pinned_proto_known": False},
        )
        with self.assertRaisesRegex(ChronicleExternalStateWireError, "wire type"):
            decode_resource_change(
                _bfield(1, _meta(1, 1)) + _sfield(4, "not-an-int")
            )
        with self.assertRaisesRegex(ChronicleExternalStateWireError, "duplicate singular"):
            decode_extra_attack(
                _bfield(1, _meta(1, 1))
                + _sfield(2, "first")
                + _sfield(2, "second")
            )

    def test_unknown_evidence_kind_is_preserved_counted_and_nonvoting(self) -> None:
        decoded = decode_consume(_consume(kind=9))
        self.assertEqual(
            decoded["kind"],
            {"number": 9, "name": None, "pinned_proto_known": False},
        )

        with tempfile.TemporaryDirectory() as temporary:
            data_root, _, source_manifest = self._raw_fixture(
                Path(temporary), consume_kind=9
            )
            result = build_external_state_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=data_root / "derived" / "unknown-enum",
            )
            self.assertEqual(result["unknown_enum_value_count"], 1)
            self.assertEqual(result["unknown_enum_values"], {"consume.kind:9": 1})

            manifest = json.loads(
                Path(result["manifest_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["summary"]["unknown_enum_value_count"], 1)
            self.assertEqual(
                manifest["summary"]["unknown_enum_values"], {"consume.kind:9": 1}
            )
            self.assertEqual(
                manifest["partitions"][0]["unknown_enum_values"],
                {"consume.kind:9": 1},
            )
            self.assertEqual(
                manifest["partitions"][0]["source_streams"]["consume"][
                    "unknown_enum_value_count"
                ],
                1,
            )
            enum_contract = manifest["normalization_contract"][
                "unknown_proto3_enum_values"
            ]
            self.assertTrue(enum_contract["wire_number_preserved"])
            self.assertFalse(enum_contract["semantic_name_fabricated"])
            self.assertTrue(enum_contract["semantic_consumers_must_fail_closed"])
            self.assertFalse(enum_contract["voting_authorized"])
            self.assertFalse(manifest["claim_boundary"]["policy_input_authorized"])
            self.assertFalse(
                manifest["claim_boundary"]["comparison_input_authorized"]
            )

            rows = [
                json.loads(line)
                for line in gzip.decompress(Path(result["partitions"][0]).read_bytes()).splitlines()
            ]
            consume_row = next(row for row in rows if row["stream_type"] == "consume")
            self.assertEqual(
                consume_row["state_payload"]["kind"],
                {"number": 9, "name": None, "pinned_proto_known": False},
            )
            self.assertEqual(consume_row["status"], OUTPUT_STATUS)

    def test_all_state_stream_frames_decode_and_enforce_boundaries(self) -> None:
        for stream_type in STATE_STREAM_TYPES:
            with self.subTest(stream_type=stream_type):
                frames = decode_event_stream(
                    _event_stream([MESSAGES[stream_type]()]),
                    stream_type=stream_type,
                )
                self.assertEqual(frames[0]["encounter_id"], "encounter-1")
                self.assertEqual(len(frames[0]["messages"]), 1)
                self.assertIsNotNone(frames[0]["messages"][0]["event"]["meta"])
        with self.assertRaisesRegex(
            ChronicleExternalStateWireError, "message count and data_length"
        ):
            decode_event_stream(
                _event_stream([_aura()], trailing_body=b"x"), stream_type="aura"
            )
        with self.assertRaisesRegex(ChronicleExternalStateWireError, "lacks EventMeta"):
            decode_event_stream(
                _event_stream([_sfield(2, "0xTARGET")]), stream_type="aura"
            )

    def test_build_is_instance_isolated_sorted_and_worker_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root, raw_root, source_manifest = self._raw_fixture(
                Path(temporary), instance_count=2
            )
            serial = build_external_state_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=data_root / "derived" / "serial",
                workers=1,
            )
            parallel = build_external_state_event_normalization(
                source_manifest_path=source_manifest,
                data_root=data_root,
                output_directory=data_root / "derived" / "parallel",
                workers=32,
            )

            self.assertEqual(serial["status"], OUTPUT_STATUS)
            self.assertEqual(serial["content_sha256"], parallel["content_sha256"])
            self.assertEqual(
                serial["manifest_file_sha256"], parallel["manifest_file_sha256"]
            )
            self.assertEqual(serial["record_count"], 10)
            self.assertEqual(serial["raw_object_copy_count"], 0)
            self.assertEqual(serial["network_request_count"], 0)
            self.assertEqual(serial["unknown_field_count"], 6)
            self.assertEqual(serial["worker_count"], 1)
            self.assertEqual(parallel["worker_count"], 2)

            serial_manifest = json.loads(
                Path(serial["manifest_path"]).read_text(encoding="utf-8")
            )
            parallel_manifest = json.loads(
                Path(parallel["manifest_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(serial_manifest, parallel_manifest)
            self.assertEqual(serial_manifest["schema"], SCHEMA)
            self.assertEqual(serial_manifest["status"], OUTPUT_STATUS)
            self.assertTrue(
                serial_manifest["normalization_contract"]["manifest_committed_last"]
            )
            self.assertFalse(
                serial_manifest["raid_provenance_contract"][
                    "player_name_filter_applied"
                ]
            )
            self.assertFalse(
                serial_manifest["claim_boundary"]["policy_input_authorized"]
            )
            self.assertFalse(
                serial_manifest["claim_boundary"]["comparison_input_authorized"]
            )

            expected_order = [
                "extra_attack",
                "aura",
                "aura_cast",
                "consume",
                "resource_change",
            ]
            seen_instances: set[str] = set()
            for partition_entry, partition_path_text in zip(
                serial_manifest["partitions"], serial["partitions"], strict=True
            ):
                partition_path = Path(partition_path_text)
                compressed = partition_path.read_bytes()
                self.assertEqual(
                    hashlib.sha256(compressed).hexdigest(),
                    partition_entry["compressed_file_sha256"],
                )
                logical = gzip.decompress(compressed)
                self.assertEqual(
                    hashlib.sha256(logical).hexdigest(),
                    partition_entry["logical_content_sha256"],
                )
                rows = [json.loads(line) for line in logical.splitlines()]
                self.assertEqual([row["stream_type"] for row in rows], expected_order)
                self.assertEqual(len({row["instance"] for row in rows}), 1)
                seen_instances.add(rows[0]["instance"])
                self.assertTrue(all(row["schema"] == RECORD_SCHEMA for row in rows))
                self.assertTrue(all(row["status"] == OUTPUT_STATUS for row in rows))
                self.assertEqual(
                    [row["provenance"]["output_line"] for row in rows],
                    list(range(1, 6)),
                )
                self.assertTrue(
                    all(
                        row["provenance"]["source_object_sha256"]
                        == partition_entry["source_streams"][row["stream_type"]][
                            "object_sha256"
                        ]
                        for row in rows
                    )
                )
                self.assertTrue(
                    all(row["official"]["message"]["meta"]["activity"] for row in rows)
                )
                resource = next(
                    row for row in rows if row["stream_type"] == "resource_change"
                )
                self.assertIn("unknown_fields", resource["official"]["message"])
                self.assertIn(
                    "unknown_fields", resource["official"]["message"]["meta"]
                )
                self.assertIn(
                    "unknown_fields",
                    resource["official"]["message"]["spell_data"],
                )
                self.assertNotIn(str(raw_root), logical.decode("utf-8"))

            self.assertEqual(seen_instances, {"instance-1", "instance-2"})
            first_rows = [
                json.loads(line)
                for line in gzip.decompress(Path(serial["partitions"][0]).read_bytes()).splitlines()
            ]
            self.assertEqual(
                first_rows[0]["raid_provenance"]["contamination_label"],
                "NO_KNOWN_RULE_MATCH",
            )
            self.assertEqual(len(first_rows), 5)

    def test_worker_bound_hash_tamper_and_content_address_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root, raw_root, source_manifest = self._raw_fixture(base)
            for workers in (0, 33):
                with self.subTest(workers=workers):
                    with self.assertRaisesRegex(
                        ChronicleExternalStateEventNormalizerError,
                        "workers must be in 1..32",
                    ):
                        build_external_state_event_normalization(
                            source_manifest_path=source_manifest,
                            data_root=data_root,
                            output_directory=data_root / "derived" / str(workers),
                            workers=workers,
                        )
            with self.assertRaisesRegex(TypeError, "workers must be an integer"):
                build_external_state_event_normalization(
                    source_manifest_path=source_manifest,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "bool",
                    workers=True,
                )

            source = json.loads(source_manifest.read_text(encoding="utf-8"))
            reference = source["instances"][0]["streams"]["consume"]["object"]
            object_path = raw_root / reference["relative_path"]
            object_path.write_bytes(object_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ChronicleExternalStateEventNormalizerError,
                "hash/size verification failed",
            ):
                build_external_state_event_normalization(
                    source_manifest_path=source_manifest,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "tampered",
                )

            renamed = source_manifest.with_name("0" * 64 + ".json")
            renamed.write_bytes(source_manifest.read_bytes())
            with self.assertRaisesRegex(
                ChronicleExternalStateEventNormalizerError, "filename hash mismatch"
            ):
                build_external_state_event_normalization(
                    source_manifest_path=renamed,
                    data_root=data_root,
                    output_directory=data_root / "derived" / "renamed",
                )

    def test_manifest_is_committed_last_after_parallel_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root, _, source_manifest = self._raw_fixture(
                Path(temporary), instance_count=2, corrupt_second=True
            )
            output = data_root / "derived" / "state"
            output.mkdir(parents=True)
            stable = output / "manifest.json"
            stable.write_bytes(b"previous-stable-manifest\n")
            with self.assertRaises(ChronicleExternalStateEventNormalizerError):
                build_external_state_event_normalization(
                    source_manifest_path=source_manifest,
                    data_root=data_root,
                    output_directory=output,
                    workers=2,
                )
            self.assertEqual(stable.read_bytes(), b"previous-stable-manifest\n")
            self.assertEqual(list(output.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
