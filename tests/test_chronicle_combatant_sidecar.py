from __future__ import annotations

import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_combatant_sidecar import (
    ChronicleCombatantHTTPError,
    ChronicleStreamDecodeError,
    DATASET_SCHEMA,
    build_combatant_sidecar,
    decode_combatant_info_stream,
    select_latest_at_or_before_start,
    sidecar_records,
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


def _tag(field_number: int, wire_type: int) -> bytes:
    return _varint((field_number << 3) | wire_type)


def _v(field_number: int, value: int) -> bytes:
    return _tag(field_number, 0) + _varint(value)


def _b(field_number: int, value: bytes) -> bytes:
    return _tag(field_number, 2) + _varint(len(value)) + value


def _s(field_number: int, value: str) -> bytes:
    return _b(field_number, value.encode("utf-8"))


def _packed(field_number: int, values: list[int]) -> bytes:
    return _b(field_number, b"".join(_varint(value) for value in values))


def _meta(index: int, offset_ms: int, *, synthetic: bool = False) -> bytes:
    return _v(1, index) + _v(2, offset_ms) + _v(4, int(synthetic))


def _combatant(
    index: int,
    *,
    item_id: int = 19019,
    guid: str = "Player-1",
    offset_ms: int | None = None,
) -> bytes:
    gear = (
        _v(1, item_id)
        + _v(2, 2564)
        + _v(3, 37)
        + _packed(4, [3001, 0])
        + _v(4, 3003)
    )
    empty_optional_gear = _v(1, 17182)
    talents = (
        _packed(1, [17, 34, 0])
        + _s(2, "20305001302")
        + _s(2, "05050005525010051")
        + _s(2, "")
    )
    return b"".join(
        [
            _b(1, _meta(index, index * 10 if offset_ms is None else offset_ms)),
            _s(2, guid),
            _s(3, "Fury Cat"),
            _s(4, "Warrior"),
            _s(5, "Orc"),
            _v(6, 2),
            _s(7, "Raid Guild"),
            _b(8, gear),
            _b(8, empty_optional_gear),
            _b(9, talents),
            # A normal unknown fixed32 field must not affect the supported subset.
            _tag(30, 5) + b"\x01\x02\x03\x04",
        ]
    )


def _plain_frame(
    messages: list[bytes],
    *,
    encounter_id: str = "encounter-1",
    first_timestamp_ms: int = 1_700_000_000_000,
    declared_count: int | None = None,
    trailing_body: bytes = b"",
) -> bytes:
    body = b"".join(_varint(len(message)) + message for message in messages)
    body += trailing_body
    encoded_encounter = encounter_id.encode("utf-8")
    return b"".join(
        [
            _varint(len(encoded_encounter)),
            encoded_encounter,
            _varint(first_timestamp_ms),
            _varint(len(messages) if declared_count is None else declared_count),
            _varint(len(body)),
            body,
        ]
    )


def _stream(*frames: bytes) -> bytes:
    return gzip.compress(b"".join(frames), mtime=0)


class ChronicleCombatantDecoderTests(unittest.TestCase):
    def test_decodes_packed_repeated_gear_and_exact_talents(self) -> None:
        frames = decode_combatant_info_stream(_stream(_plain_frame([_combatant(7)])))

        self.assertEqual(len(frames), 1)
        message = frames[0]["messages"][0]
        self.assertEqual(
            message["meta"],
            {"event_index": 7, "offset_ms": 70, "is_synthetic": False},
        )
        self.assertEqual(message["guid"], "Player-1")
        self.assertEqual(message["guild_name"], "Raid Guild")
        self.assertEqual(
            message["gear"],
            [
                {
                    "item_id": 19019,
                    "enchant_id": 2564,
                    "temporary_enchant_id": 37,
                    "gem_enchant_ids": [3001, 0, 3003],
                },
                {
                    "item_id": 17182,
                    "enchant_id": None,
                    "temporary_enchant_id": None,
                    "gem_enchant_ids": [],
                },
            ],
        )
        self.assertEqual(message["talents"]["summary"], [17, 34, 0])
        self.assertEqual(
            message["talents"]["trees"],
            ["20305001302", "05050005525010051", ""],
        )

    def test_repeated_info_is_preserved_and_selector_is_causal(self) -> None:
        frames = decode_combatant_info_stream(
            _stream(
                _plain_frame(
                    [_combatant(5, item_id=100), _combatant(9, item_id=200), _combatant(13, item_id=300)]
                )
            )
        )
        records = sidecar_records(frames, instance_ref="instance-1", slug="slug-1")

        self.assertEqual(len(records), 3)
        selected = select_latest_at_or_before_start(
            records,
            encounter_id="encounter-1",
            player_guid="player-1",
            start_event_index=10,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["anchor"]["event_index"], 9)
        self.assertEqual(selected["gear"][0]["item_id"], 200)

    def test_selector_rejects_info_after_start(self) -> None:
        frames = decode_combatant_info_stream(
            _stream(_plain_frame([_combatant(11)]))
        )
        records = sidecar_records(frames, instance_ref="instance-1", slug="slug-1")
        self.assertIsNone(
            select_latest_at_or_before_start(
                records,
                encounter_id="encounter-1",
                player_guid="Player-1",
                start_event_index=10,
            )
        )

    def test_rejects_bad_encounter_frames(self) -> None:
        bad_frames = [
            _plain_frame([_combatant(1)], declared_count=2),
            _plain_frame([_combatant(1)], trailing_body=b"\x00"),
        ]
        for frame in bad_frames:
            with self.subTest(frame_size=len(frame)):
                with self.assertRaises(ChronicleStreamDecodeError):
                    decode_combatant_info_stream(_stream(frame))

        with self.assertRaises(ChronicleStreamDecodeError):
            decode_combatant_info_stream(b"not gzip")


class ChronicleCombatantBuildTests(unittest.TestCase):
    def _inputs(
        self,
        root: Path,
        *,
        refs: list[str] | None = None,
        duplicate_partition_ref: bool = False,
    ) -> tuple[Path, Path]:
        refs = refs or ["instance-1"]
        partitions = [
            {"partition": "never-opened.jsonl.gz", "source_instance_refs": [ref]}
            for ref in refs
        ]
        if duplicate_partition_ref:
            partitions.append(
                {
                    "partition": "also-never-opened.jsonl.gz",
                    "source_instance_refs": [refs[0]],
                }
            )
        manifest = root / "dataset-manifest.json"
        manifest.write_text(
            json.dumps({"schema": DATASET_SCHEMA, "partitions": partitions}),
            encoding="utf-8",
        )
        queue = root / "export-queue.json"
        queue.write_text(
            json.dumps(
                {
                    "entries": [
                        {"instance_id": ref, "slug": f"slug-{index + 1}"}
                        for index, ref in enumerate(refs)
                    ]
                }
            ),
            encoding="utf-8",
        )
        return manifest, queue

    def test_404_is_recorded_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, queue = self._inputs(root)

            def missing(url: str) -> bytes:
                raise HTTPError(url, 404, "missing", hdrs=None, fp=None)

            result = build_combatant_sidecar(
                dataset_manifest=manifest,
                export_queue=queue,
                raw_dir=root / "raw",
                output_dir=root / "sidecar",
                workers=1,
                get_binary=missing,
            )

            self.assertEqual(result["source_counts"], {"reused": 0, "downloaded": 0, "404": 1})
            self.assertEqual(result["unavailable_instance_count"], 1)
            self.assertEqual(result["compressed_bytes_total"], 0)
            self.assertEqual(result["instances"][0]["availability"], "unavailable")
            self.assertFalse(list((root / "raw").glob("*.events.gz")))

    def test_non_404_http_failure_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, queue = self._inputs(root)

            def failed(url: str) -> bytes:
                raise HTTPError(url, 503, "busy", hdrs=None, fp=None)

            with self.assertRaises(ChronicleCombatantHTTPError) as raised:
                build_combatant_sidecar(
                    dataset_manifest=manifest,
                    export_queue=queue,
                    raw_dir=root / "raw",
                    output_dir=root / "sidecar",
                    workers=1,
                    get_binary=failed,
                )
            self.assertEqual(raised.exception.status_code, 503)

    def test_existing_raw_is_reused_and_partitions_are_not_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, queue = self._inputs(root, duplicate_partition_ref=True)
            compressed = _stream(_plain_frame([_combatant(4)]))
            raw_dir = root / "raw"
            raw_dir.mkdir()
            (raw_dir / "instance-1.combatant_info.events.gz").write_bytes(compressed)

            def must_not_fetch(url: str) -> bytes:
                raise AssertionError(f"unexpected fetch: {url}")

            result = build_combatant_sidecar(
                dataset_manifest=manifest,
                export_queue=queue,
                raw_dir=raw_dir,
                output_dir=root / "sidecar",
                get_binary=must_not_fetch,
            )

            self.assertEqual(result["instance_ref_count"], 1)
            self.assertEqual(result["source_counts"], {"reused": 1, "downloaded": 0, "404": 0})
            self.assertEqual(result["reused_bytes_total"], len(compressed))
            self.assertEqual(result["combatant_info_message_count"], 1)
            sidecar = Path(result["instances"][0]["sidecar_path"])
            self.assertEqual(sidecar.suffixes[-2:], [".jsonl", ".gz"])
            with gzip.open(sidecar, mode="rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(rows[0]["gear"][0]["temporary_enchant_id"], 37)
            self.assertEqual(rows[0]["talents"]["summary"], [17, 34, 0])
            self.assertEqual(
                result["sidecar_compressed_bytes_total"], sidecar.stat().st_size
            )

    def test_parallel_fetch_requests_each_unique_source_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, queue = self._inputs(
                root, refs=["instance-1", "instance-2"], duplicate_partition_ref=True
            )
            calls: list[str] = []

            def fetched(url: str) -> bytes:
                calls.append(url)
                return _stream(_plain_frame([_combatant(2)]))

            result = build_combatant_sidecar(
                dataset_manifest=manifest,
                export_queue=queue,
                raw_dir=root / "raw",
                output_dir=root / "sidecar",
                workers=4,
                get_binary=fetched,
            )

            self.assertEqual(len(calls), 2)
            self.assertEqual(len(set(calls)), 2)
            self.assertEqual(result["workers"], 2)
            self.assertEqual(result["source_counts"]["downloaded"], 2)
            self.assertEqual(result["decoded_instance_count"], 2)
            self.assertEqual(result["combatant_info_message_count"], 2)
            self.assertGreater(result["downloaded_bytes_total"], 0)


if __name__ == "__main__":
    unittest.main()
