"""Fetch and decode Chronicle's small ``combatant_info`` event stream.

This module intentionally stays separate from the compact decision dataset.  It
stores the official compressed response once per source instance and writes one
small JSONL sidecar row per CombatantInfo message.  In particular, it never
opens or rewrites the decision-dataset partitions.

The decoder implements only the protobuf messages needed for CombatantInfo and
the custom encounter framing documented by Chronicle.  It has no protobuf
runtime dependency.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
MANIFEST_SCHEMA = "chronicle_combatant_info_sidecar/v1"
RECORD_SCHEMA = "chronicle_combatant_info/v1"
DATASET_SCHEMA = "chronicle_fury_decision_dataset/v1"
STREAM_TYPE = "combatant_info"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_DATASET_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_fury_decision_dataset"
    / "v1"
    / "manifest.json"
)
DEFAULT_EXPORT_QUEUE = DEFAULT_DATA_ROOT / "chronicle_raw" / "export_queue.json"
DEFAULT_RAW_DIR = (
    DEFAULT_DATA_ROOT / "chronicle_raw" / "external_api" / STREAM_TYPE
)
DEFAULT_OUTPUT_DIR = (
    DEFAULT_DATA_ROOT / "derived" / "chronicle_combatant_info_sidecar" / "v1"
)
EXTERNAL_API_BASE = "https://capy.chronicleclassic.com/api/external/v1"

_FILE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")
BinaryGetter = Callable[[str], bytes]


class ChronicleCombatantSidecarError(RuntimeError):
    """The stream, source manifests, or generated sidecar is invalid."""


class ChronicleStreamDecodeError(ChronicleCombatantSidecarError):
    """The gzip, encounter framing, or supported protobuf payload is invalid."""


class ChronicleCombatantHTTPError(ChronicleCombatantSidecarError):
    """Chronicle returned a non-success HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleCombatantSidecarError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleCombatantSidecarError(f"{label} is not a JSON object: {path}")
    return value


def _read_uvarint(data: bytes, offset: int, end: int) -> tuple[int, int]:
    value = 0
    for byte_index in range(10):
        if offset >= end:
            raise ChronicleStreamDecodeError("unexpected EOF while reading varint")
        byte = data[offset]
        offset += 1
        if byte_index == 9 and byte > 1:
            raise ChronicleStreamDecodeError("varint exceeds 64 bits")
        value |= (byte & 0x7F) << (7 * byte_index)
        if byte < 0x80:
            return value, offset
    raise ChronicleStreamDecodeError("varint exceeds 10 bytes")


def _read_bytes(data: bytes, offset: int, end: int) -> tuple[bytes, int]:
    length, offset = _read_uvarint(data, offset, end)
    value_end = offset + length
    if value_end > end:
        raise ChronicleStreamDecodeError("length-delimited value exceeds its boundary")
    return data[offset:value_end], value_end


def _skip_group(data: bytes, offset: int, end: int, group_field: int) -> int:
    while offset < end:
        tag, offset = _read_uvarint(data, offset, end)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number == 0:
            raise ChronicleStreamDecodeError("protobuf field number zero is invalid")
        if wire_type == 4:
            if field_number != group_field:
                raise ChronicleStreamDecodeError("mismatched protobuf end-group field")
            return offset
        offset = _skip_wire_value(data, offset, end, wire_type, field_number)
    raise ChronicleStreamDecodeError("unterminated protobuf group")


def _skip_wire_value(
    data: bytes,
    offset: int,
    end: int,
    wire_type: int,
    field_number: int,
) -> int:
    if wire_type == 0:
        _, offset = _read_uvarint(data, offset, end)
        return offset
    if wire_type == 1:
        value_end = offset + 8
    elif wire_type == 2:
        _, value_end = _read_bytes(data, offset, end)
        return value_end
    elif wire_type == 3:
        return _skip_group(data, offset, end, field_number)
    elif wire_type == 5:
        value_end = offset + 4
    else:
        raise ChronicleStreamDecodeError(f"unsupported protobuf wire type {wire_type}")
    if value_end > end:
        raise ChronicleStreamDecodeError("protobuf value exceeds message boundary")
    return value_end


def _wire_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    offset = 0
    end = len(data)
    while offset < end:
        tag, offset = _read_uvarint(data, offset, end)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number == 0:
            raise ChronicleStreamDecodeError("protobuf field number zero is invalid")
        if wire_type == 0:
            value, offset = _read_uvarint(data, offset, end)
            yield field_number, wire_type, value
        elif wire_type == 1:
            value_end = offset + 8
            if value_end > end:
                raise ChronicleStreamDecodeError("fixed64 exceeds message boundary")
            yield field_number, wire_type, data[offset:value_end]
            offset = value_end
        elif wire_type == 2:
            value, offset = _read_bytes(data, offset, end)
            yield field_number, wire_type, value
        elif wire_type == 3:
            offset = _skip_group(data, offset, end, field_number)
        elif wire_type == 4:
            raise ChronicleStreamDecodeError("unexpected protobuf end-group field")
        elif wire_type == 5:
            value_end = offset + 4
            if value_end > end:
                raise ChronicleStreamDecodeError("fixed32 exceeds message boundary")
            yield field_number, wire_type, data[offset:value_end]
            offset = value_end
        else:
            raise ChronicleStreamDecodeError(f"unsupported protobuf wire type {wire_type}")


def _require_wire(actual: int, expected: int, *, field: str) -> None:
    if actual != expected:
        raise ChronicleStreamDecodeError(
            f"protobuf field {field} has wire type {actual}, expected {expected}"
        )


def _decode_utf8(value: bytes, *, field: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ChronicleStreamDecodeError(
            f"protobuf field {field} is not valid UTF-8"
        ) from error


def _as_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def _as_int64(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _decode_packed_int32(value: bytes, *, field: str) -> list[int]:
    result: list[int] = []
    offset = 0
    while offset < len(value):
        item, offset = _read_uvarint(value, offset, len(value))
        result.append(_as_int32(item))
    return result


def decode_event_meta(data: bytes) -> dict[str, Any]:
    """Decode the EventMeta subset that establishes a causal event anchor."""

    result: dict[str, Any] = {
        "event_index": 0,
        "offset_ms": 0,
        "is_synthetic": False,
    }
    for field_number, wire_type, value in _wire_fields(data):
        if field_number == 1:
            _require_wire(wire_type, 0, field="EventMeta.index")
            assert isinstance(value, int)
            result["event_index"] = _as_int32(value)
        elif field_number == 2:
            _require_wire(wire_type, 0, field="EventMeta.offsetMilli")
            assert isinstance(value, int)
            result["offset_ms"] = _as_int64(value)
        elif field_number == 4:
            _require_wire(wire_type, 0, field="EventMeta.is_synthetic")
            assert isinstance(value, int)
            result["is_synthetic"] = value != 0
        # EventMeta.activity and unknown future fields are deliberately ignored.
    return result


def decode_combatant_gear_slot(data: bytes) -> dict[str, Any]:
    """Decode one ordered Chronicle gear-slot entry, preserving optionals."""

    result: dict[str, Any] = {
        "item_id": 0,
        "enchant_id": None,
        "temporary_enchant_id": None,
        "gem_enchant_ids": [],
    }
    for field_number, wire_type, value in _wire_fields(data):
        if field_number == 1:
            _require_wire(wire_type, 0, field="CombatantGearSlot.itemId")
            assert isinstance(value, int)
            result["item_id"] = _as_int32(value)
        elif field_number == 2:
            _require_wire(wire_type, 0, field="CombatantGearSlot.enchantId")
            assert isinstance(value, int)
            result["enchant_id"] = _as_int32(value)
        elif field_number == 3:
            _require_wire(
                wire_type, 0, field="CombatantGearSlot.temporaryEnchantId"
            )
            assert isinstance(value, int)
            result["temporary_enchant_id"] = _as_int32(value)
        elif field_number == 4:
            if wire_type == 0:
                assert isinstance(value, int)
                result["gem_enchant_ids"].append(_as_int32(value))
            elif wire_type == 2:
                assert isinstance(value, bytes)
                result["gem_enchant_ids"].extend(
                    _decode_packed_int32(
                        value, field="CombatantGearSlot.gemEnchantIds"
                    )
                )
            else:
                raise ChronicleStreamDecodeError(
                    "protobuf field CombatantGearSlot.gemEnchantIds has "
                    f"wire type {wire_type}, expected 0 or 2"
                )
    return result


def decode_combatant_talents(data: bytes) -> dict[str, Any]:
    """Decode exact tree totals and rank strings without interpretation."""

    result: dict[str, Any] = {"summary": [], "trees": []}
    for field_number, wire_type, value in _wire_fields(data):
        if field_number == 1:
            if wire_type == 0:
                assert isinstance(value, int)
                result["summary"].append(_as_int32(value))
            elif wire_type == 2:
                assert isinstance(value, bytes)
                result["summary"].extend(
                    _decode_packed_int32(value, field="CombatantTalents.summary")
                )
            else:
                raise ChronicleStreamDecodeError(
                    "protobuf field CombatantTalents.summary has "
                    f"wire type {wire_type}, expected 0 or 2"
                )
        elif field_number == 2:
            _require_wire(wire_type, 2, field="CombatantTalents.trees")
            assert isinstance(value, bytes)
            result["trees"].append(
                _decode_utf8(value, field="CombatantTalents.trees")
            )
    return result


def decode_combatant_info(data: bytes) -> dict[str, Any]:
    """Decode one Chronicle CombatantInfo protobuf message."""

    result: dict[str, Any] = {
        "meta": None,
        "guid": "",
        "name": "",
        "hero_class": "",
        "race": "",
        "gender": 0,
        "guild_name": None,
        "gear": [],
        "talents": None,
    }
    string_fields = {
        2: ("guid", "CombatantInfo.guid"),
        3: ("name", "CombatantInfo.name"),
        4: ("hero_class", "CombatantInfo.heroClass"),
        5: ("race", "CombatantInfo.race"),
        7: ("guild_name", "CombatantInfo.guildName"),
    }
    for field_number, wire_type, value in _wire_fields(data):
        if field_number == 1:
            _require_wire(wire_type, 2, field="CombatantInfo.meta")
            assert isinstance(value, bytes)
            result["meta"] = decode_event_meta(value)
        elif field_number in string_fields:
            output_name, protobuf_name = string_fields[field_number]
            _require_wire(wire_type, 2, field=protobuf_name)
            assert isinstance(value, bytes)
            result[output_name] = _decode_utf8(value, field=protobuf_name)
        elif field_number == 6:
            _require_wire(wire_type, 0, field="CombatantInfo.gender")
            assert isinstance(value, int)
            result["gender"] = _as_int32(value)
        elif field_number == 8:
            _require_wire(wire_type, 2, field="CombatantInfo.gear")
            assert isinstance(value, bytes)
            result["gear"].append(decode_combatant_gear_slot(value))
        elif field_number == 9:
            _require_wire(wire_type, 2, field="CombatantInfo.talents")
            assert isinstance(value, bytes)
            result["talents"] = decode_combatant_talents(value)
    return result


def decode_combatant_info_stream(compressed: bytes) -> list[dict[str, Any]]:
    """Decode all custom-framed encounters in a gzip event-stream response."""

    if not isinstance(compressed, bytes):
        raise ChronicleStreamDecodeError("compressed stream must be bytes")
    try:
        data = gzip.decompress(compressed)
    except (OSError, EOFError) as error:
        raise ChronicleStreamDecodeError(f"invalid gzip event stream: {error}") from error

    frames: list[dict[str, Any]] = []
    offset = 0
    end = len(data)
    while offset < end:
        encounter_bytes, offset = _read_bytes(data, offset, end)
        encounter_id = _decode_utf8(encounter_bytes, field="encounter_id")
        first_timestamp_ms, offset = _read_uvarint(data, offset, end)
        count, offset = _read_uvarint(data, offset, end)
        data_length, offset = _read_uvarint(data, offset, end)
        body_end = offset + data_length
        if body_end > end:
            raise ChronicleStreamDecodeError("encounter frame body exceeds stream")

        messages: list[dict[str, Any]] = []
        for _ in range(count):
            message_bytes, offset = _read_bytes(data, offset, body_end)
            messages.append(decode_combatant_info(message_bytes))
        if offset != body_end:
            raise ChronicleStreamDecodeError(
                "encounter message count and data_length disagree"
            )
        frames.append(
            {
                "encounter_id": encounter_id,
                "first_timestamp_ms": first_timestamp_ms,
                "messages": messages,
            }
        )
    return frames


def sidecar_records(
    frames: Iterable[dict[str, Any]],
    *,
    instance_ref: str,
    slug: str,
) -> list[dict[str, Any]]:
    """Project decoded frames to lossless encounter/player INFO records.

    Repeated CombatantInfo messages are retained as separate records.  The
    message ordinal is a stable tie-breaker for duplicate event indices.
    """

    records: list[dict[str, Any]] = []
    message_ordinal = 0
    for frame_index, frame in enumerate(frames):
        encounter_id = str(frame["encounter_id"])
        first_timestamp_ms = int(frame["first_timestamp_ms"])
        messages = frame["messages"]
        if not isinstance(messages, list):
            raise ChronicleCombatantSidecarError("decoded frame messages is not a list")
        for frame_message_index, message in enumerate(messages):
            meta = message.get("meta")
            anchor = None
            if meta is not None:
                offset_ms = int(meta["offset_ms"])
                anchor = {
                    "event_index": int(meta["event_index"]),
                    "offset_ms": offset_ms,
                    "timestamp_ms": first_timestamp_ms + offset_ms,
                    "is_synthetic": bool(meta["is_synthetic"]),
                }
            gear = []
            for slot_index, slot in enumerate(message["gear"]):
                gear.append({"slot_index": slot_index, **slot})
            records.append(
                {
                    "schema": RECORD_SCHEMA,
                    "instance_ref": instance_ref,
                    "slug": slug,
                    "encounter_id": encounter_id,
                    "first_timestamp_ms": first_timestamp_ms,
                    "frame_index": frame_index,
                    "frame_message_index": frame_message_index,
                    "message_ordinal": message_ordinal,
                    "anchor": anchor,
                    "player": {
                        "guid": message["guid"],
                        "name": message["name"],
                        "hero_class": message["hero_class"],
                        "race": message["race"],
                        "gender": message["gender"],
                        "guild_name": message["guild_name"],
                    },
                    "gear": gear,
                    "talents": message["talents"],
                }
            )
            message_ordinal += 1
    return records


def select_latest_at_or_before_start(
    records: Iterable[dict[str, Any]],
    *,
    encounter_id: str,
    player_guid: str,
    start_event_index: int,
    instance_ref: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest matching INFO whose event index is not after START."""

    if isinstance(start_event_index, bool) or not isinstance(start_event_index, int):
        raise ChronicleCombatantSidecarError("start_event_index must be an integer")
    requested_guid = player_guid.strip().lower()
    if not requested_guid:
        raise ChronicleCombatantSidecarError("player_guid must not be empty")

    best: dict[str, Any] | None = None
    best_key: tuple[int, int] | None = None
    for record in records:
        if record.get("encounter_id") != encounter_id:
            continue
        if instance_ref is not None and record.get("instance_ref") != instance_ref:
            continue
        player = record.get("player")
        if not isinstance(player, dict):
            raise ChronicleCombatantSidecarError("sidecar record player is not an object")
        if str(player.get("guid") or "").strip().lower() != requested_guid:
            continue
        anchor = record.get("anchor")
        if anchor is None:
            continue
        if not isinstance(anchor, dict):
            raise ChronicleCombatantSidecarError("sidecar record anchor is not an object")
        event_index = anchor.get("event_index")
        if isinstance(event_index, bool) or not isinstance(event_index, int):
            raise ChronicleCombatantSidecarError(
                "sidecar anchor event_index must be an integer"
            )
        if event_index > start_event_index:
            continue
        ordinal = record.get("message_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise ChronicleCombatantSidecarError(
                "sidecar message_ordinal must be an integer"
            )
        key = (event_index, ordinal)
        if best_key is None or key > best_key:
            best = record
            best_key = key
    return best


def load_instance_requests(
    dataset_manifest: str | Path = DEFAULT_DATASET_MANIFEST,
    export_queue: str | Path = DEFAULT_EXPORT_QUEUE,
) -> list[dict[str, str]]:
    """Resolve the compact manifest's unique instance refs to queue slugs."""

    manifest_path = Path(dataset_manifest).expanduser().resolve()
    queue_path = Path(export_queue).expanduser().resolve()
    manifest = _load_json_object(manifest_path, label="compact dataset manifest")
    if manifest.get("schema") != DATASET_SCHEMA:
        raise ChronicleCombatantSidecarError(
            f"unsupported compact dataset schema: {manifest.get('schema')!r}"
        )
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list):
        raise ChronicleCombatantSidecarError(
            f"compact dataset manifest has no partitions list: {manifest_path}"
        )

    refs: list[str] = []
    seen_refs: set[str] = set()
    for partition in partitions:
        if not isinstance(partition, dict):
            raise ChronicleCombatantSidecarError(
                "compact dataset manifest partition is not an object"
            )
        source_refs = partition.get("source_instance_refs")
        if not isinstance(source_refs, list):
            raise ChronicleCombatantSidecarError(
                "compact dataset partition has no source_instance_refs list"
            )
        for raw_ref in source_refs:
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                raise ChronicleCombatantSidecarError(
                    "compact dataset source_instance_ref is not a non-empty string"
                )
            ref = raw_ref.strip()
            if ref not in seen_refs:
                refs.append(ref)
                seen_refs.add(ref)

    queue = _load_json_object(queue_path, label="Chronicle export queue")
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise ChronicleCombatantSidecarError(
            f"Chronicle export queue has no entries list: {queue_path}"
        )

    requests: list[dict[str, str]] = []
    resolved_instance_ids: set[str] = set()
    for ref in refs:
        matches: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            instance_id = str(entry.get("instance_id") or "").strip()
            slug = str(entry.get("slug") or "").strip()
            if instance_id.lower() == ref.lower() or slug == ref:
                matches.append(entry)
        if len(matches) != 1:
            raise ChronicleCombatantSidecarError(
                f"expected one export_queue entry for {ref!r}, found {len(matches)}"
            )
        slug = str(matches[0].get("slug") or "").strip()
        if not slug:
            raise ChronicleCombatantSidecarError(
                f"export_queue entry for {ref!r} has no public slug"
            )
        canonical_instance_id = str(matches[0].get("instance_id") or "").strip()
        canonical_key = canonical_instance_id.lower() or slug
        if canonical_key in resolved_instance_ids:
            raise ChronicleCombatantSidecarError(
                f"multiple compact refs resolve to one queue source: {canonical_instance_id or slug!r}"
            )
        resolved_instance_ids.add(canonical_key)
        requests.append(
            {
                "instance_ref": ref,
                "instance_id": canonical_instance_id,
                "slug": slug,
            }
        )
    return requests


def _file_name(instance_ref: str, suffix: str) -> str:
    if not _FILE_COMPONENT.fullmatch(instance_ref):
        raise ChronicleCombatantSidecarError(
            f"instance ref cannot be used as a sidecar file name: {instance_ref!r}"
        )
    return f"{instance_ref}.{suffix}"


def _event_stream_url(base_url: str, slug: str) -> str:
    return (
        f"{base_url.rstrip('/')}/raidlogs/instances/"
        f"{quote(slug, safe='')}/events/{STREAM_TYPE}"
    )


def _request_binary(url: str, *, timeout: float = 30.0) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "BrainOfCat-O2O-DPS/0.1 (Chronicle External API)",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise ChronicleCombatantHTTPError(
                    status, f"Chronicle combatant_info returned HTTP {status}"
                )
            return response.read()
    except HTTPError as error:
        raise ChronicleCombatantHTTPError(
            error.code, f"Chronicle combatant_info returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise ChronicleCombatantSidecarError(
            f"cannot reach Chronicle combatant_info stream: {error.reason}"
        ) from error
    except OSError as error:
        raise ChronicleCombatantSidecarError(
            f"cannot read Chronicle combatant_info response: {error}"
        ) from error


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_jsonl_gzip(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with gzip.open(
            temporary,
            mode="wt",
            encoding="utf-8",
            newline="\n",
            compresslevel=9,
        ) as compressed:
            for record in records:
                compressed.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                compressed.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


def build_combatant_sidecar(
    *,
    dataset_manifest: str | Path = DEFAULT_DATASET_MANIFEST,
    export_queue: str | Path = DEFAULT_EXPORT_QUEUE,
    raw_dir: str | Path = DEFAULT_RAW_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    base_url: str = EXTERNAL_API_BASE,
    timeout: float = 30.0,
    workers: int = 4,
    reuse_existing_raw: bool = True,
    get_binary: BinaryGetter | None = None,
) -> dict[str, Any]:
    """Build raw-cache-backed per-instance sidecars for the compact manifest."""

    manifest_path = Path(dataset_manifest).expanduser().resolve()
    queue_path = Path(export_queue).expanduser().resolve()
    resolved_raw_dir = Path(raw_dir).expanduser().resolve()
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ChronicleCombatantSidecarError("workers must be a positive integer")
    requests = load_instance_requests(manifest_path, queue_path)
    fetch = get_binary or (lambda url: _request_binary(url, timeout=timeout))

    def process(request: dict[str, str]) -> dict[str, Any]:
        instance_ref = request["instance_ref"]
        slug = request["slug"]
        raw_path = resolved_raw_dir / _file_name(
            instance_ref, f"{STREAM_TYPE}.events.gz"
        )
        sidecar_path = resolved_output_dir / _file_name(
            instance_ref, f"{STREAM_TYPE}.jsonl.gz"
        )
        source = "reused"
        if reuse_existing_raw and raw_path.is_file():
            try:
                compressed = raw_path.read_bytes()
            except OSError as error:
                raise ChronicleCombatantSidecarError(
                    f"cannot read cached stream {raw_path}: {error}"
                ) from error
        else:
            source = "downloaded"
            url = _event_stream_url(base_url, slug)
            try:
                compressed = fetch(url)
            except ChronicleCombatantHTTPError as error:
                if error.status_code == 404:
                    return {
                        "instance_ref": instance_ref,
                        "slug": slug,
                        "availability": "unavailable",
                        "http_status": 404,
                        "source": "404",
                        "decoded": False,
                        "raw_path": None,
                        "sidecar_path": None,
                        "sidecar_compressed_size_bytes": 0,
                        "compressed_size_bytes": 0,
                        "encounter_count": 0,
                        "combatant_info_count": 0,
                    }
                raise
            except HTTPError as error:
                if error.code == 404:
                    return {
                        "instance_ref": instance_ref,
                        "slug": slug,
                        "availability": "unavailable",
                        "http_status": 404,
                        "source": "404",
                        "decoded": False,
                        "raw_path": None,
                        "sidecar_path": None,
                        "sidecar_compressed_size_bytes": 0,
                        "compressed_size_bytes": 0,
                        "encounter_count": 0,
                        "combatant_info_count": 0,
                    }
                raise ChronicleCombatantHTTPError(
                    error.code,
                    f"Chronicle combatant_info returned HTTP {error.code}",
                ) from error
            except URLError as error:
                raise ChronicleCombatantSidecarError(
                    f"cannot reach Chronicle combatant_info stream: {error.reason}"
                ) from error
            if not isinstance(compressed, bytes):
                raise ChronicleCombatantSidecarError(
                    "combatant_info binary getter did not return bytes"
                )

        frames = decode_combatant_info_stream(compressed)
        records = sidecar_records(frames, instance_ref=instance_ref, slug=slug)
        if source == "downloaded":
            _atomic_write_bytes(raw_path, compressed)
        _atomic_write_jsonl_gzip(sidecar_path, records)
        try:
            sidecar_size = sidecar_path.stat().st_size
        except OSError as error:
            raise ChronicleCombatantSidecarError(
                f"cannot stat compressed sidecar {sidecar_path}: {error}"
            ) from error
        return {
            "instance_ref": instance_ref,
            "slug": slug,
            "availability": "available",
            "http_status": 200 if source == "downloaded" else None,
            "source": source,
            "decoded": True,
            "raw_path": str(raw_path),
            "sidecar_path": str(sidecar_path),
            "sidecar_compressed_size_bytes": sidecar_size,
            "compressed_size_bytes": len(compressed),
            "encounter_count": len(frames),
            "combatant_info_count": len(records),
        }

    instance_results: list[dict[str, Any]] = [{} for _ in requests]
    if requests:
        with ThreadPoolExecutor(max_workers=min(workers, len(requests))) as executor:
            futures = {
                executor.submit(process, request): index
                for index, request in enumerate(requests)
            }
            for future in as_completed(futures):
                instance_results[futures[future]] = future.result()

    source_counts = {
        source: sum(item["source"] == source for item in instance_results)
        for source in ("reused", "downloaded", "404")
    }

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": "chronicle_combatant_info_sidecar_manifest",
        "created_at": _utc_now(),
        "stream_type": STREAM_TYPE,
        "source_dataset_manifest": str(manifest_path),
        "source_export_queue": str(queue_path),
        "workers": min(workers, len(requests)) if requests else 0,
        "instance_ref_count": len(requests),
        "available_instance_count": sum(
            item["availability"] == "available" for item in instance_results
        ),
        "unavailable_instance_count": sum(
            item["availability"] == "unavailable" for item in instance_results
        ),
        "source_counts": source_counts,
        "decoded_instance_count": sum(item["decoded"] for item in instance_results),
        "decoded_encounter_count": sum(
            item["encounter_count"] for item in instance_results
        ),
        "combatant_info_message_count": sum(
            item["combatant_info_count"] for item in instance_results
        ),
        "compressed_bytes_total": sum(
            item["compressed_size_bytes"] for item in instance_results
        ),
        "downloaded_bytes_total": sum(
            item["compressed_size_bytes"]
            for item in instance_results
            if item["source"] == "downloaded"
        ),
        "reused_bytes_total": sum(
            item["compressed_size_bytes"]
            for item in instance_results
            if item["source"] == "reused"
        ),
        "sidecar_compressed_bytes_total": sum(
            item["sidecar_compressed_size_bytes"] for item in instance_results
        ),
        "instances": instance_results,
    }
    manifest_output = resolved_output_dir / "manifest.json"
    _atomic_write_json(manifest_output, manifest)
    return {**manifest, "manifest_path": str(manifest_output)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Chronicle combatant_info streams and build a small causal sidecar"
        )
    )
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_DATASET_MANIFEST)
    parser.add_argument("--export-queue", type=Path, default=DEFAULT_EXPORT_QUEUE)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--base-url", default=EXTERNAL_API_BASE)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="download even when the instance's raw .events.gz already exists",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_combatant_sidecar(
            dataset_manifest=args.dataset_manifest,
            export_queue=args.export_queue,
            raw_dir=args.raw_dir,
            output_dir=args.output_dir,
            base_url=args.base_url,
            timeout=args.timeout,
            workers=args.workers,
            reuse_existing_raw=not args.redownload,
        )
    except ChronicleCombatantSidecarError as error:
        print(f"error: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
