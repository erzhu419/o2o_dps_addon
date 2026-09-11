"""Normalize Chronicle External API core event streams without copying raw data.

Chronicle's event endpoints return gzip-compressed custom encounter frames.  A
frame contains length-delimited protobuf messages of exactly one type.  This
module implements the fixed Chronicle protobuf contract needed by the team
timeline and projects it to rows compatible with the existing normalized CSV
shape.

The raw object store remains immutable and authoritative.  Every referenced
object is hash/size verified before decoding, unknown protobuf fields are kept
as exact base64-encoded wire fields, partitions are content addressed, and the
stable manifest is replaced only after all partitions are durable.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence

from .chronicle_external_api_ingest_v1 import (
    IMPLEMENTATION_REVISION as SOURCE_IMPLEMENTATION_REVISION,
    PARSER_CONTRACT_REVISION as SOURCE_PARSER_CONTRACT_REVISION,
)

SCHEMA = "chronicle_external_core_event_normalization/v1"
RECORD_SCHEMA = "chronicle_external_core_event/v1"
SOURCE_MANIFEST_SCHEMA = "chronicle_external_api_ingest/v1"
IMPLEMENTATION_REVISION = "v1.1_fixed_proto_current_raw_boundary_manifest_last"
PROTO_COMMIT = "004acd948773b7c914bb358ce09e3bc759650883"
PROTO_URL = (
    "https://github.com/Emyrk/chronicle/blob/"
    f"{PROTO_COMMIT}/api/chronicleproto/chronicle.proto"
)
EVENT_STREAM_DOC_URL = (
    "https://github.com/Emyrk/chronicle/blob/main/"
    "docs/external-api-event-streams.md"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_RAW_ROOT = DEFAULT_DATA_ROOT / "chronicle_raw" / "external_api" / "v1"
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "chronicle_external_core_events" / "v1"
)

CORE_STREAM_TYPES = (
    "damage",
    "heal",
    "slain",
    "spell_start",
    "spell_go",
    "spell_fail",
    "unit_classification",
)
STREAM_TO_NORMALIZED_TYPE = {
    "damage": "DMG",
    "heal": "HEAL",
    "slain": "DEAD",
    "spell_start": "START",
    "spell_go": "GO",
    "spell_fail": "FAIL",
    "unit_classification": "CLASS",
}
STREAM_ORDER = {name: index for index, name in enumerate(CORE_STREAM_TYPES)}
SCHOOL_NAMES = {
    0: "Unknown",
    1: "None",
    2: "Physical",
    3: "Holy",
    4: "Fire",
    5: "Nature",
    6: "Frost",
    7: "Shadow",
    8: "Arcane",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ChronicleExternalEventNormalizerError(RuntimeError):
    """A raw reference, event stream, protobuf message, or output is invalid."""


class ChronicleExternalWireError(ChronicleExternalEventNormalizerError):
    """A gzip envelope, encounter frame, or protobuf wire value is invalid."""


@dataclass(frozen=True)
class WireField:
    number: int
    wire_type: int
    value: int | bytes | None
    raw: bytes


@dataclass(frozen=True)
class DecodedEnvelope:
    stream_type: str
    encounter_id: str
    first_timestamp_ms: int
    frame_index: int
    frame_message_index: int
    message_sha256: str
    event: Mapping[str, Any]


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    manifest_entry: dict[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ChronicleExternalEventNormalizerError(
            f"value is not canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ChronicleExternalEventNormalizerError(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _read_uvarint(data: bytes, offset: int, end: int) -> tuple[int, int]:
    value = 0
    for byte_index in range(10):
        if offset >= end:
            raise ChronicleExternalWireError("unexpected EOF while reading varint")
        byte = data[offset]
        offset += 1
        if byte_index == 9 and byte > 1:
            raise ChronicleExternalWireError("varint exceeds 64 bits")
        value |= (byte & 0x7F) << (byte_index * 7)
        if byte < 0x80:
            return value, offset
    raise ChronicleExternalWireError("varint exceeds 10 bytes")


def _read_bytes(data: bytes, offset: int, end: int) -> tuple[bytes, int]:
    length, offset = _read_uvarint(data, offset, end)
    value_end = offset + length
    if value_end > end:
        raise ChronicleExternalWireError(
            "length-delimited value exceeds its boundary"
        )
    return data[offset:value_end], value_end


def _skip_group(
    data: bytes, offset: int, end: int, expected_field_number: int
) -> int:
    while offset < end:
        tag, after_tag = _read_uvarint(data, offset, end)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number == 0:
            raise ChronicleExternalWireError("protobuf field number zero is invalid")
        offset = after_tag
        if wire_type == 4:
            if field_number != expected_field_number:
                raise ChronicleExternalWireError("mismatched protobuf end-group field")
            return offset
        offset = _skip_wire_value(data, offset, end, wire_type, field_number)
    raise ChronicleExternalWireError("unterminated protobuf group")


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
        raise ChronicleExternalWireError(
            f"unsupported protobuf wire type {wire_type}"
        )
    if value_end > end:
        raise ChronicleExternalWireError("protobuf value exceeds message boundary")
    return value_end


def _wire_fields(data: bytes) -> Iterator[WireField]:
    offset = 0
    end = len(data)
    while offset < end:
        field_start = offset
        tag, offset = _read_uvarint(data, offset, end)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number == 0:
            raise ChronicleExternalWireError("protobuf field number zero is invalid")
        value: int | bytes | None
        if wire_type == 0:
            value, offset = _read_uvarint(data, offset, end)
        elif wire_type == 1:
            value_end = offset + 8
            if value_end > end:
                raise ChronicleExternalWireError("fixed64 exceeds message boundary")
            value = data[offset:value_end]
            offset = value_end
        elif wire_type == 2:
            value, offset = _read_bytes(data, offset, end)
        elif wire_type == 3:
            offset = _skip_group(data, offset, end, field_number)
            value = None
        elif wire_type == 4:
            raise ChronicleExternalWireError("unexpected protobuf end-group field")
        elif wire_type == 5:
            value_end = offset + 4
            if value_end > end:
                raise ChronicleExternalWireError("fixed32 exceeds message boundary")
            value = data[offset:value_end]
            offset = value_end
        else:
            raise ChronicleExternalWireError(
                f"unsupported protobuf wire type {wire_type}"
            )
        yield WireField(field_number, wire_type, value, data[field_start:offset])


def _require_wire(field: WireField, expected: int, *, label: str) -> None:
    if field.wire_type != expected:
        raise ChronicleExternalWireError(
            f"protobuf field {label} has wire type {field.wire_type}, "
            f"expected {expected}"
        )


def _int_value(field: WireField, *, label: str) -> int:
    _require_wire(field, 0, label=label)
    assert isinstance(field.value, int)
    return field.value


def _bytes_value(field: WireField, *, label: str) -> bytes:
    _require_wire(field, 2, label=label)
    assert isinstance(field.value, bytes)
    return field.value


def _text_value(field: WireField, *, label: str) -> str:
    try:
        return _bytes_value(field, label=label).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ChronicleExternalWireError(
            f"protobuf field {label} is not valid UTF-8"
        ) from error


def _as_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def _as_int64(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _as_uint32(value: int, *, label: str) -> int:
    if value > 0xFFFFFFFF:
        raise ChronicleExternalWireError(f"protobuf uint32 {label} exceeds 32 bits")
    return value


def _unknown(field: WireField) -> dict[str, Any]:
    return {
        "field_number": field.number,
        "wire_type": field.wire_type,
        "raw_field_base64": base64.b64encode(field.raw).decode("ascii"),
    }


def _attach_unknown(result: dict[str, Any], fields: list[dict[str, Any]]) -> None:
    if fields:
        result["unknown_fields"] = fields


def decode_activity_entry(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"guid": "", "event_type": ""}
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["guid"] = _text_value(field, label="ActivityEntry.guid")
        elif field.number == 2:
            result["event_type"] = _text_value(
                field, label="ActivityEntry.eventType"
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_event_meta(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "event_index": 0,
        "offset_ms": 0,
        "activity": [],
        "is_synthetic": False,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["event_index"] = _as_int32(
                _int_value(field, label="EventMeta.index")
            )
        elif field.number == 2:
            result["offset_ms"] = _as_int64(
                _int_value(field, label="EventMeta.offsetMilli")
            )
        elif field.number == 3:
            result["activity"].append(
                decode_activity_entry(_bytes_value(field, label="EventMeta.activity"))
            )
        elif field.number == 4:
            result["is_synthetic"] = (
                _int_value(field, label="EventMeta.is_synthetic") != 0
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_spell_data(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"id": 0, "name": "", "attack_outcome": 0}
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["id"] = _as_int32(_int_value(field, label="SpellData.id"))
        elif field.number == 2:
            result["name"] = _text_value(field, label="SpellData.name")
        elif field.number == 3:
            result["attack_outcome"] = _as_uint32(
                _int_value(field, label="SpellData.attack_outcome"),
                label="SpellData.attack_outcome",
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_tailer(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"amount": None, "hit_type": 0}
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["amount"] = _as_uint32(
                _int_value(field, label="Tailer.amount"), label="Tailer.amount"
            )
        elif field.number == 2:
            result["hit_type"] = _as_uint32(
                _int_value(field, label="Tailer.hitType"), label="Tailer.hitType"
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def _school(value: int) -> dict[str, Any]:
    return {"number": value, "name": SCHOOL_NAMES.get(value, f"UNKNOWN_{value}")}


def decode_damage(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "caster": None,
        "source_name": "",
        "target": "",
        "hit_type": 0,
        "amount": 0,
        "school": _school(0),
        "tailers": [],
        "spell_data": None,
        "overkill": 0,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="Damage.meta")
            )
        elif field.number == 3:
            result["caster"] = _text_value(field, label="Damage.caster")
        elif field.number == 4:
            result["source_name"] = _text_value(field, label="Damage.sourceName")
        elif field.number == 5:
            result["target"] = _text_value(field, label="Damage.target")
        elif field.number == 6:
            result["hit_type"] = _as_uint32(
                _int_value(field, label="Damage.hitType"), label="Damage.hitType"
            )
        elif field.number == 7:
            result["amount"] = _as_int32(
                _int_value(field, label="Damage.amount")
            )
        elif field.number == 8:
            result["school"] = _school(
                _as_int32(_int_value(field, label="Damage.school"))
            )
        elif field.number == 9:
            result["tailers"].append(
                decode_tailer(_bytes_value(field, label="Damage.tailers"))
            )
        elif field.number == 10:
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="Damage.spellData")
            )
        elif field.number == 11:
            result["overkill"] = _as_int32(
                _int_value(field, label="Damage.overkill")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_heal(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "caster": "",
        "target": "",
        "source_name": "",
        "amount": 0,
        "hit_type": 0,
        "spell_data": None,
        "school": _school(0),
        "overheal": 0,
        "absorbed": 0,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(_bytes_value(field, label="Heal.meta"))
        elif field.number == 3:
            result["caster"] = _text_value(field, label="Heal.caster")
        elif field.number == 4:
            result["target"] = _text_value(field, label="Heal.target")
        elif field.number == 5:
            result["source_name"] = _text_value(field, label="Heal.sourceName")
        elif field.number == 6:
            result["amount"] = _as_int32(_int_value(field, label="Heal.amount"))
        elif field.number == 7:
            result["hit_type"] = _as_uint32(
                _int_value(field, label="Heal.hitType"), label="Heal.hitType"
            )
        elif field.number == 8:
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="Heal.spellData")
            )
        elif field.number == 9:
            result["school"] = _school(
                _as_int32(_int_value(field, label="Heal.school"))
            )
        elif field.number == 10:
            result["overheal"] = _as_int32(
                _int_value(field, label="Heal.overheal")
            )
        elif field.number == 11:
            result["absorbed"] = _as_int32(
                _int_value(field, label="Heal.absorbed")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_slain(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "target": "",
        "caster": None,
        "attribution": None,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="Slain.meta")
            )
        elif field.number == 2:
            result["target"] = _text_value(field, label="Slain.target")
        elif field.number == 3:
            result["caster"] = _text_value(field, label="Slain.caster")
        elif field.number == 4:
            result["attribution"] = decode_damage(
                _bytes_value(field, label="Slain.attribution")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_spell_go(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "item_id": None,
        "spell_data": None,
        "caster": "",
        "target": None,
        "num_hits": 0,
        "num_misses": 0,
        "corpse_owner": None,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="SpellGo.meta")
            )
        elif field.number == 2:
            result["item_id"] = _as_int32(
                _int_value(field, label="SpellGo.itemID")
            )
        elif field.number == 3:
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="SpellGo.spellData")
            )
        elif field.number == 4:
            result["caster"] = _text_value(field, label="SpellGo.caster")
        elif field.number == 5:
            result["target"] = _text_value(field, label="SpellGo.target")
        elif field.number == 6:
            result["num_hits"] = _as_int32(
                _int_value(field, label="SpellGo.numHits")
            )
        elif field.number == 7:
            result["num_misses"] = _as_int32(
                _int_value(field, label="SpellGo.numMisses")
            )
        elif field.number == 8:
            result["corpse_owner"] = _text_value(
                field, label="SpellGo.corpseOwner"
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_spell_start(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "item_id": None,
        "spell_data": None,
        "caster": "",
        "target": None,
        "cast_flags": 0,
        "cast_time_ms": 0,
        "channel_time_ms": 0,
        "spell_type": 0,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="SpellStart.meta")
            )
        elif field.number == 2:
            result["item_id"] = _as_int32(
                _int_value(field, label="SpellStart.itemID")
            )
        elif field.number == 3:
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="SpellStart.spellData")
            )
        elif field.number == 4:
            result["caster"] = _text_value(field, label="SpellStart.caster")
        elif field.number == 5:
            result["target"] = _text_value(field, label="SpellStart.target")
        elif field.number == 6:
            result["cast_flags"] = _as_int32(
                _int_value(field, label="SpellStart.castFlags")
            )
        elif field.number == 7:
            result["cast_time_ms"] = _as_int32(
                _int_value(field, label="SpellStart.castTimeMilli")
            )
        elif field.number == 8:
            result["channel_time_ms"] = _as_int32(
                _int_value(field, label="SpellStart.channelTimeMilli")
            )
        elif field.number == 9:
            result["spell_type"] = _as_int32(
                _int_value(field, label="SpellStart.spellType")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_spell_fail(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "caster": "",
        "spell_data": None,
        "failed_by_server": False,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="SpellFail.meta")
            )
        elif field.number == 2:
            result["caster"] = _text_value(field, label="SpellFail.caster")
        elif field.number == 3:
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="SpellFail.spellData")
            )
        elif field.number == 4:
            # The source proto spells this field ``failedBySever``.
            result["failed_by_server"] = (
                _int_value(field, label="SpellFail.failedBySever") != 0
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_unit_classification(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "meta": None,
        "target": "",
        "unit_type": 0,
        "affiliation": 0,
        "owner": None,
        "controller": None,
        "spell_id": 0,
    }
    unknown: list[dict[str, Any]] = []
    for field in _wire_fields(data):
        if field.number == 1:
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="UnitClassification.meta")
            )
        elif field.number == 2:
            result["target"] = _text_value(
                field, label="UnitClassification.target"
            )
        elif field.number == 3:
            result["unit_type"] = _as_int32(
                _int_value(field, label="UnitClassification.unitType")
            )
        elif field.number == 4:
            result["affiliation"] = _as_int32(
                _int_value(field, label="UnitClassification.affiliation")
            )
        elif field.number == 5:
            result["owner"] = _text_value(
                field, label="UnitClassification.owner"
            )
        elif field.number == 6:
            result["controller"] = _text_value(
                field, label="UnitClassification.controller"
            )
        elif field.number == 7:
            result["spell_id"] = _as_int32(
                _int_value(field, label="UnitClassification.spellId")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


DECODERS: Mapping[str, Callable[[bytes], dict[str, Any]]] = {
    "damage": decode_damage,
    "heal": decode_heal,
    "slain": decode_slain,
    "spell_start": decode_spell_start,
    "spell_go": decode_spell_go,
    "spell_fail": decode_spell_fail,
    "unit_classification": decode_unit_classification,
}


def decode_event_stream(
    compressed: bytes, *, stream_type: str
) -> list[dict[str, Any]]:
    """Decode every custom-framed encounter for one supported core stream."""

    decoder = DECODERS.get(stream_type)
    if decoder is None:
        raise ChronicleExternalEventNormalizerError(
            f"unsupported core event stream: {stream_type!r}"
        )
    if not isinstance(compressed, bytes) or not compressed.startswith(b"\x1f\x8b"):
        raise ChronicleExternalWireError("event object is not a gzip byte stream")
    try:
        data = gzip.decompress(compressed)
    except (OSError, EOFError) as error:
        raise ChronicleExternalWireError(
            f"invalid gzip event stream for {stream_type}: {error}"
        ) from error

    frames: list[dict[str, Any]] = []
    offset = 0
    end = len(data)
    while offset < end:
        encounter_bytes, offset = _read_bytes(data, offset, end)
        try:
            encounter_id = encounter_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ChronicleExternalWireError(
                "encounter_id is not valid UTF-8"
            ) from error
        if not encounter_id:
            raise ChronicleExternalWireError("encounter_id must not be empty")
        first_timestamp_ms, offset = _read_uvarint(data, offset, end)
        count, offset = _read_uvarint(data, offset, end)
        data_length, offset = _read_uvarint(data, offset, end)
        body_end = offset + data_length
        if body_end > end:
            raise ChronicleExternalWireError("encounter frame body exceeds stream")
        messages: list[dict[str, Any]] = []
        for _ in range(count):
            message_bytes, offset = _read_bytes(data, offset, body_end)
            event = decoder(message_bytes)
            if event.get("meta") is None:
                raise ChronicleExternalWireError(
                    f"{stream_type} message lacks EventMeta and cannot be ordered"
                )
            messages.append(
                {
                    "message_sha256": _sha256(message_bytes),
                    "event": event,
                }
            )
        if offset != body_end:
            raise ChronicleExternalWireError(
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


def _load_source_manifest(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ChronicleExternalEventNormalizerError(
            f"cannot read source manifest {path}: {error}"
        ) from error
    digest = _sha256(payload)
    if not _SHA256_RE.fullmatch(path.stem):
        raise ChronicleExternalEventNormalizerError(
            "source manifest filename is not a SHA-256 content address"
        )
    if path.stem != digest:
        raise ChronicleExternalEventNormalizerError(
            f"source manifest filename hash mismatch: {path.stem} != {digest}"
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalEventNormalizerError(
            f"source manifest is not UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict) or value.get("schema") != SOURCE_MANIFEST_SCHEMA:
        raise ChronicleExternalEventNormalizerError(
            f"source manifest schema must be {SOURCE_MANIFEST_SCHEMA}"
        )
    if (
        value.get("implementation_revision") != SOURCE_IMPLEMENTATION_REVISION
        or value.get("parser_contract_revision")
        != SOURCE_PARSER_CONTRACT_REVISION
    ):
        raise ChronicleExternalEventNormalizerError(
            "source raw manifest is not the current ingest/parser revision; "
            "replay it from local raw objects before normalization"
        )
    if not isinstance(value.get("instances"), list):
        raise ChronicleExternalEventNormalizerError(
            "source manifest instances must be an array"
        )
    return value, payload, digest


def _resolve_raw_root(data_root: Path) -> Path:
    resolved = data_root.expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in resolved.parts}:
        raise ChronicleExternalEventNormalizerError(
            "data root must be an offline_data directory"
        )
    return resolved / "chronicle_raw" / "external_api" / "v1"


def _read_object_reference(
    raw_root: Path, reference: Any, *, label: str
) -> tuple[bytes, Path]:
    if not isinstance(reference, Mapping):
        raise ChronicleExternalEventNormalizerError(
            f"{label} object reference must be an object"
        )
    relative_raw = reference.get("relative_path")
    digest = reference.get("sha256")
    size = reference.get("size_bytes")
    if not isinstance(relative_raw, str) or not relative_raw:
        raise ChronicleExternalEventNormalizerError(
            f"{label} object reference lacks relative_path"
        )
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ChronicleExternalEventNormalizerError(
            f"{label} object reference has invalid sha256"
        )
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ChronicleExternalEventNormalizerError(
            f"{label} object reference has invalid size_bytes"
        )
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalEventNormalizerError(
            f"{label} object path escapes the raw store"
        )
    path = (raw_root / relative).resolve()
    try:
        path.relative_to(raw_root.resolve())
    except ValueError as error:
        raise ChronicleExternalEventNormalizerError(
            f"{label} object path escapes the raw store"
        ) from error
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ChronicleExternalEventNormalizerError(
            f"cannot read {label} object {path}: {error}"
        ) from error
    actual = _sha256(payload)
    if actual != digest or len(payload) != size:
        raise ChronicleExternalEventNormalizerError(
            f"{label} object hash/size verification failed: {path}"
        )
    return payload, path


def _count_unknown_fields(value: Any) -> int:
    if isinstance(value, Mapping):
        own = value.get("unknown_fields")
        count = len(own) if isinstance(own, list) else 0
        return count + sum(
            _count_unknown_fields(child)
            for key, child in value.items()
            if key != "unknown_fields"
        )
    if isinstance(value, list):
        return sum(_count_unknown_fields(child) for child in value)
    return 0


def _spell_projection(
    event: Mapping[str, Any], diagnostics: list[str]
) -> tuple[str | None, int | None]:
    source_name = event.get("source_name")
    source_name = source_name if isinstance(source_name, str) and source_name else None
    spell_data = event.get("spell_data")
    spell_name: str | None = None
    spell_id: int | None = None
    if isinstance(spell_data, Mapping):
        raw_name = spell_data.get("name")
        if isinstance(raw_name, str) and raw_name:
            spell_name = raw_name
        raw_id = spell_data.get("id")
        if isinstance(raw_id, int):
            spell_id = raw_id
    if source_name and spell_name and source_name != spell_name:
        diagnostics.append("SOURCE_NAME_AND_SPELL_DATA_NAME_CONFLICT_UNMAPPED")
        return None, spell_id
    return spell_name or source_name, spell_id


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _unit_classification_outcome(event: Mapping[str, Any]) -> str:
    # Numeric values are copied without assigning undocumented semantic names.
    parts = [
        f"unit_type={event['unit_type']}",
        f"affiliation={event['affiliation']}",
    ]
    owner = _identifier(event.get("owner"))
    controller = _identifier(event.get("controller"))
    if owner:
        parts.append(f"owner={owner}")
    if controller:
        parts.append(f"controller={controller}")
    parts.append(f"spell_id={event['spell_id']}")
    return " ".join(parts)


def _utc_iso_from_milliseconds(timestamp_ms: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError) as error:
        raise ChronicleExternalEventNormalizerError(
            f"event timestamp is outside the supported range: {timestamp_ms}"
        ) from error


def _normalized_record(
    envelope: DecodedEnvelope,
    *,
    instance_id: str,
    source_manifest_sha256: str,
    source_object_sha256: str,
    encounter_ordinal: int,
    output_line: int,
) -> dict[str, Any]:
    event = envelope.event
    meta = event.get("meta")
    if not isinstance(meta, Mapping):
        raise ChronicleExternalEventNormalizerError("decoded event has no EventMeta")
    event_index = meta.get("event_index")
    offset_ms = meta.get("offset_ms")
    if isinstance(event_index, bool) or not isinstance(event_index, int):
        raise ChronicleExternalEventNormalizerError("EventMeta.index is not an integer")
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, int):
        raise ChronicleExternalEventNormalizerError(
            "EventMeta.offsetMilli is not an integer"
        )
    timestamp_ms = envelope.first_timestamp_ms + offset_ms
    stream_type = envelope.stream_type
    diagnostics: list[str] = []
    spell, spell_id = _spell_projection(event, diagnostics)
    source_guid: str | None = None
    target_guid: str | None = None
    value: int | None = None
    outcome: str | None = None
    unmapped: list[str] = ["EventMeta.activity"]

    if stream_type == "damage":
        source_guid = _identifier(event.get("caster"))
        target_guid = _identifier(event.get("target"))
        value = int(event["amount"])
        outcome = f"hit_type={event['hit_type']}"
        unmapped.extend(
            ["school", "tailers", "overkill", "SpellData.attack_outcome"]
        )
    elif stream_type == "heal":
        source_guid = _identifier(event.get("caster"))
        target_guid = _identifier(event.get("target"))
        value = int(event["amount"])
        outcome = f"hit_type={event['hit_type']}"
        unmapped.extend(
            ["school", "overheal", "absorbed", "SpellData.attack_outcome"]
        )
    elif stream_type == "slain":
        source_guid = _identifier(event.get("caster"))
        target_guid = _identifier(event.get("target"))
        spell = None
        spell_id = None
        # Attribution often duplicates the damage stream, so it is deliberately
        # not projected to value.  The complete nested message remains below.
        unmapped.append("attribution_not_projected_to_avoid_double_counting")
    elif stream_type in {"spell_start", "spell_go", "spell_fail"}:
        source_guid = _identifier(event.get("caster"))
        target_guid = _identifier(event.get("target"))
        if stream_type == "spell_start":
            unmapped.extend(
                ["item_id", "cast_flags", "cast_time_ms", "channel_time_ms", "spell_type"]
            )
        elif stream_type == "spell_go":
            outcome = f"num_hits={event['num_hits']} num_misses={event['num_misses']}"
            unmapped.extend(["item_id", "corpse_owner", "SpellData.attack_outcome"])
        else:
            outcome = f"failed_by_server={str(bool(event['failed_by_server'])).lower()}"
            unmapped.append("SpellData.attack_outcome")
    elif stream_type == "unit_classification":
        target_guid = _identifier(event.get("target"))
        spell = None
        spell_id = int(event["spell_id"])
        outcome = _unit_classification_outcome(event)
        unmapped.append("unit_type_and_affiliation_semantic_labels_not_in_proto")
    else:  # pragma: no cover - construction is closed over CORE_STREAM_TYPES.
        raise ChronicleExternalEventNormalizerError(
            f"cannot project unsupported stream {stream_type}"
        )

    return {
        "schema": RECORD_SCHEMA,
        "instance": instance_id,
        "encounter": envelope.encounter_id,
        "encounter_ordinal": encounter_ordinal,
        "first_timestamp_ms": envelope.first_timestamp_ms,
        "event_index": event_index,
        "offset_ms": offset_ms,
        "timestamp_ms": timestamp_ms,
        "time": _utc_iso_from_milliseconds(timestamp_ms),
        "type": STREAM_TO_NORMALIZED_TYPE[stream_type],
        "source": None,
        "source_guid": source_guid,
        "target": None,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "outcome": outcome,
        "synthetic": bool(meta.get("is_synthetic")),
        "flags": [],
        "activity": None,
        "official": {
            "stream_type": stream_type,
            "message": event,
            "message_sha256": envelope.message_sha256,
        },
        "bridge": {
            "status": "PARTIAL_EXACT_NO_SEMANTIC_GUESSING"
            if unmapped or diagnostics
            else "EXACT",
            "identifier_policy": "caster_and_target_copied_verbatim_no_name_or_owner_inference",
            "unmapped_official_fields": unmapped,
            "diagnostics": diagnostics,
        },
        "provenance": {
            "format": "chronicle_external_api_core_event_v1",
            "source_manifest_sha256": source_manifest_sha256,
            "source_object_sha256": source_object_sha256,
            "stream_type": stream_type,
            "frame_index": envelope.frame_index,
            "frame_message_index": envelope.frame_message_index,
            "csv_line": output_line,
            "csv_line_semantics": "derived_jsonl_line_tiebreaker_not_an_original_csv_line",
            "raw_object_copied": False,
        },
    }


def _instance_envelopes(
    instance: Mapping[str, Any],
    *,
    raw_root: Path,
) -> tuple[list[DecodedEnvelope], dict[str, Any], int, dict[str, int]]:
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ChronicleExternalEventNormalizerError("instance row lacks instance_id")
    streams = instance.get("streams")
    if not isinstance(streams, Mapping):
        raise ChronicleExternalEventNormalizerError(
            f"instance {instance_id} lacks streams"
        )

    envelopes: list[DecodedEnvelope] = []
    stream_evidence: dict[str, Any] = {}
    encounter_origins: dict[str, int] = {}
    unknown_count = 0
    for stream_type in CORE_STREAM_TYPES:
        wrapper = streams.get(stream_type)
        if not isinstance(wrapper, Mapping) or wrapper.get("status") != "AVAILABLE":
            raise ChronicleExternalEventNormalizerError(
                f"instance {instance_id} core stream {stream_type} is unavailable"
            )
        reference = wrapper.get("object")
        compressed, _ = _read_object_reference(
            raw_root, reference, label=f"{instance_id}.{stream_type}"
        )
        frames = decode_event_stream(compressed, stream_type=stream_type)
        seen_encounters: set[str] = set()
        message_count = 0
        stream_unknown = 0
        frame_evidence: list[dict[str, Any]] = []
        for frame_index, frame in enumerate(frames):
            encounter_id = str(frame["encounter_id"])
            if encounter_id in seen_encounters:
                raise ChronicleExternalEventNormalizerError(
                    f"{stream_type} contains duplicate encounter frame {encounter_id}"
                )
            seen_encounters.add(encounter_id)
            origin = int(frame["first_timestamp_ms"])
            frame_message_count = len(frame["messages"])
            frame_evidence.append(
                {
                    "frame_index": frame_index,
                    "encounter_id": encounter_id,
                    "first_timestamp_ms": origin,
                    "message_count": frame_message_count,
                }
            )
            previous_origin = encounter_origins.get(encounter_id)
            # The official contract permits a zero origin on an empty frame.
            # Treat only that exact combination as an unspecified placeholder.
            origin_is_placeholder = origin == 0 and frame_message_count == 0
            if previous_origin is None:
                encounter_origins[encounter_id] = origin
            elif previous_origin == 0 and origin != 0:
                encounter_origins[encounter_id] = origin
            elif (
                not origin_is_placeholder
                and previous_origin != 0
                and previous_origin != origin
            ):
                raise ChronicleExternalEventNormalizerError(
                    f"encounter {encounter_id} has conflicting frame origins: "
                    f"{previous_origin} != {origin}"
                )
            for message_index, message in enumerate(frame["messages"]):
                decoded = message["event"]
                stream_unknown += _count_unknown_fields(decoded)
                envelopes.append(
                    DecodedEnvelope(
                        stream_type=stream_type,
                        encounter_id=encounter_id,
                        first_timestamp_ms=origin,
                        frame_index=frame_index,
                        frame_message_index=message_index,
                        message_sha256=str(message["message_sha256"]),
                        event=decoded,
                    )
                )
                message_count += 1
        assert isinstance(reference, Mapping)
        stream_evidence[stream_type] = {
            "object_sha256": reference["sha256"],
            "object_size_bytes": reference["size_bytes"],
            "frame_count": len(frames),
            "frames": frame_evidence,
            "message_count": message_count,
            "unknown_field_count": stream_unknown,
        }
        unknown_count += stream_unknown

    encounter_order = {
        encounter_id: ordinal
        for ordinal, (encounter_id, _) in enumerate(
            sorted(encounter_origins.items(), key=lambda item: (item[1], item[0]))
        )
    }

    def order_key(envelope: DecodedEnvelope) -> tuple[int, int, int, int, int]:
        meta = envelope.event["meta"]
        assert isinstance(meta, Mapping)
        timestamp_ms = envelope.first_timestamp_ms + int(meta["offset_ms"])
        return (
            encounter_order[envelope.encounter_id],
            timestamp_ms,
            int(meta["event_index"]),
            STREAM_ORDER[envelope.stream_type],
            envelope.frame_message_index,
        )

    envelopes.sort(key=order_key)
    return envelopes, stream_evidence, unknown_count, encounter_order


def _write_partition(
    instance: Mapping[str, Any],
    *,
    raw_root: Path,
    output_directory: Path,
    source_manifest_sha256: str,
) -> PartitionBuild:
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ChronicleExternalEventNormalizerError("instance row lacks instance_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", instance_id):
        raise ChronicleExternalEventNormalizerError(
            f"instance id is unsafe for a partition filename: {instance_id!r}"
        )
    envelopes, stream_evidence, unknown_count, encounter_order = _instance_envelopes(
        instance, raw_root=raw_root
    )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{instance_id}.core-events.",
        suffix=".jsonl.gz.tmp",
        dir=output_directory,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    logical_digest = hashlib.sha256()
    type_counts: Counter[str] = Counter()
    try:
        with temporary_path.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                compresslevel=9,
                mtime=0,
            ) as compressed:
                for output_line, envelope in enumerate(envelopes, 1):
                    stream_ref = stream_evidence[envelope.stream_type]
                    record = _normalized_record(
                        envelope,
                        instance_id=instance_id,
                        source_manifest_sha256=source_manifest_sha256,
                        source_object_sha256=stream_ref["object_sha256"],
                        encounter_ordinal=encounter_order[envelope.encounter_id],
                        output_line=output_line,
                    )
                    line = _canonical_bytes(record) + b"\n"
                    logical_digest.update(line)
                    compressed.write(line)
                    type_counts[str(record["type"])] += 1
        logical_sha256 = logical_digest.hexdigest()
        final_path = output_directory / f"{instance_id}.{logical_sha256}.jsonl.gz"
        compressed_sha256 = _sha256_file(temporary_path)
        entry = {
            "instance_id": instance_id,
            "slug": instance.get("slug"),
            "partition": final_path.name,
            "logical_content_sha256": logical_sha256,
            "compressed_file_sha256": compressed_sha256,
            "compressed_size_bytes": temporary_path.stat().st_size,
            "record_count": len(envelopes),
            "encounter_count": len(encounter_order),
            "event_type_counts": dict(sorted(type_counts.items())),
            "unknown_field_count": unknown_count,
            "source_streams": stream_evidence,
            "raw_object_open_count": len(CORE_STREAM_TYPES),
            "raw_object_copy_count": 0,
        }
        return PartitionBuild(temporary_path, final_path, entry)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_temporary(path: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_immutable(temporary: Path, final: Path, expected_sha256: str) -> None:
    if final.exists():
        if _sha256_file(final) != expected_sha256:
            raise ChronicleExternalEventNormalizerError(
                f"immutable content-addressed output differs: {final}"
            )
        temporary.unlink(missing_ok=True)
        return
    temporary.replace(final)


def _write_partitions(
    instances: Sequence[Mapping[str, Any]],
    *,
    raw_root: Path,
    output_directory: Path,
    source_manifest_sha256: str,
    workers: int,
) -> list[PartitionBuild]:
    """Build independent instance partitions with deterministic collection.

    Worker completion order is deliberately excluded from every artifact.  A
    failed worker is collected alongside all successful workers so the caller's
    manifest-last cleanup can remove every temporary file it knows about.
    """

    if workers == 1 or len(instances) <= 1:
        serial: list[PartitionBuild] = []
        try:
            for instance in instances:
                serial.append(
                    _write_partition(
                        instance,
                        raw_root=raw_root,
                        output_directory=output_directory,
                        source_manifest_sha256=source_manifest_sha256,
                    )
                )
        except BaseException:
            for build in serial:
                build.temporary_path.unlink(missing_ok=True)
            raise
        return serial

    results: dict[int, PartitionBuild] = {}
    failures: list[BaseException] = []
    effective_workers = min(workers, len(instances))
    with ProcessPoolExecutor(max_workers=effective_workers) as executor:
        futures = {
            executor.submit(
                _write_partition,
                instance,
                raw_root=raw_root,
                output_directory=output_directory,
                source_manifest_sha256=source_manifest_sha256,
            ): index
            for index, instance in enumerate(instances)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except BaseException as error:
                failures.append(error)

    ordered = [results[index] for index in sorted(results)]
    if failures:
        for build in ordered:
            build.temporary_path.unlink(missing_ok=True)
        raise failures[0]
    return ordered


def _clean_stale_temporaries(output_directory: Path) -> tuple[int, int]:
    """Remove only dot-prefixed ``*.tmp`` files from a dedicated output dir.

    This recovery path is opt-in because an active concurrent builder may own
    such a file.  Callers must first establish that no builder is using the
    directory; regular outputs and non-dot temporary-looking files are never
    touched.
    """

    removed_count = 0
    removed_bytes = 0
    resolved_output = output_directory.resolve()
    for path in output_directory.iterdir():
        if not path.name.startswith(".") or not path.name.endswith(".tmp"):
            continue
        if path.is_symlink() or not path.is_file():
            raise ChronicleExternalEventNormalizerError(
                f"refusing unsafe stale temporary entry: {path}"
            )
        resolved = path.resolve()
        if resolved.parent != resolved_output:
            raise ChronicleExternalEventNormalizerError(
                f"stale temporary escapes output directory: {path}"
            )
        removed_bytes += path.stat().st_size
        path.unlink()
        removed_count += 1
    return removed_count, removed_bytes


def build_external_core_event_normalization(
    *,
    source_manifest_path: str | Path,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
    clean_stale_temporaries: bool = False,
) -> dict[str, Any]:
    """Build verified, content-addressed normalized core-event partitions."""

    if isinstance(workers, bool) or not isinstance(workers, int):
        raise TypeError("workers must be an integer")
    if not 1 <= workers <= 32:
        raise ChronicleExternalEventNormalizerError("workers must be in 1..32")
    if not isinstance(clean_stale_temporaries, bool):
        raise TypeError("clean_stale_temporaries must be boolean")

    source_path = Path(source_manifest_path).expanduser().resolve()
    raw_root = _resolve_raw_root(Path(data_root))
    expected_manifest_directory = (raw_root / "manifests").resolve()
    try:
        source_path.relative_to(expected_manifest_directory)
    except ValueError as error:
        raise ChronicleExternalEventNormalizerError(
            "source manifest must be inside the External API manifest store"
        ) from error
    source, source_bytes, source_sha256 = _load_source_manifest(source_path)
    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stale_count = 0
    stale_bytes = 0
    if clean_stale_temporaries:
        stale_count, stale_bytes = _clean_stale_temporaries(output_dir)

    instances = source["instances"]
    assert isinstance(instances, list)
    builds: list[PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        normalized_instances: list[Mapping[str, Any]] = []
        for index, raw_instance in enumerate(instances):
            if not isinstance(raw_instance, Mapping):
                raise ChronicleExternalEventNormalizerError(
                    f"source instance {index} is not an object"
                )
            normalized_instances.append(raw_instance)
        builds = _write_partitions(
            normalized_instances,
            raw_root=raw_root,
            output_directory=output_dir,
            source_manifest_sha256=source_sha256,
            workers=workers,
        )

        manifest_core: dict[str, Any] = {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": "chronicle_external_core_event_normalization_manifest",
            "source": {
                "manifest_sha256": source_sha256,
                "manifest_size_bytes": len(source_bytes),
                "manifest_schema": SOURCE_MANIFEST_SCHEMA,
                "parser_contract_revision": source.get("parser_contract_revision"),
            },
            "official_contract": {
                "proto_commit": PROTO_COMMIT,
                "proto_url": PROTO_URL,
                "event_stream_documentation_url": EVENT_STREAM_DOC_URL,
                "framing": "gzip_then_repeated_bounded_encounter_frames_then_length_delimited_messages",
                "timestamp": "first_timestamp_ms_plus_EventMeta.offsetMilli",
                "cross_stream_tiebreaker": "EventMeta.index_within_encounter",
                "deprecated_cast_included": False,
            },
            "normalization_contract": {
                "core_stream_types": list(CORE_STREAM_TYPES),
                "event_order": [
                    "encounter(first_timestamp_ms,encounter_id)",
                    "event_timestamp_ms",
                    "EventMeta.index",
                    "fixed_stream_tiebreaker",
                    "frame_message_index",
                ],
                "identifier_policy": "caster_and_target_copied_verbatim_no_name_or_owner_inference",
                "unknown_protobuf_fields": "exact_raw_wire_field_base64_at_message_or_nested_message",
                "unit_classification_policy": "numeric_fields_and_explicit_owner_preserved_without_semantic_label_guessing",
                "slain_attribution_value_policy": "nested_attribution_preserved_but_not_projected_to_value_to_avoid_damage_double_counting",
                "output_compatible_fields": [
                    "instance",
                    "encounter",
                    "event_index",
                    "offset_ms",
                    "time",
                    "type",
                    "source",
                    "source_guid",
                    "target",
                    "target_guid",
                    "spell",
                    "spell_id",
                    "value",
                    "outcome",
                    "synthetic",
                    "flags",
                    "activity",
                    "provenance.csv_line",
                ],
                "raw_objects_copied": 0,
                "manifest_committed_last": True,
            },
            "summary": {
                "instance_count": len(builds),
                "encounter_count": sum(
                    build.manifest_entry["encounter_count"] for build in builds
                ),
                "record_count": sum(
                    build.manifest_entry["record_count"] for build in builds
                ),
                "unknown_field_count": sum(
                    build.manifest_entry["unknown_field_count"] for build in builds
                ),
                "raw_object_copy_count": 0,
                "network_request_count": 0,
            },
            "partitions": [build.manifest_entry for build in builds],
        }
        content_sha256 = _sha256(_canonical_bytes(manifest_core))
        manifest = {
            **manifest_core,
            "content_address": {
                "algorithm": "sha256",
                "scope": "canonical_JSON_excluding_content_address",
                "sha256": content_sha256,
            },
        }
        manifest_payload = _canonical_bytes(manifest) + b"\n"
        manifest_file_sha256 = _sha256(manifest_payload)
        addressed_path = output_dir / (
            f"chronicle_external_core_events_v1.{content_sha256}.manifest.json"
        )
        stable_path = output_dir / "manifest.json"
        addressed_temporary = _write_temporary(addressed_path, manifest_payload)
        stable_temporary = _write_temporary(stable_path, manifest_payload)

        # Content-addressed partitions may become harmless orphans if a later
        # publication step fails.  The stable manifest is the sole commit mark.
        for build in builds:
            _publish_immutable(
                build.temporary_path,
                build.final_path,
                build.manifest_entry["compressed_file_sha256"],
            )
        _publish_immutable(
            addressed_temporary, addressed_path, manifest_file_sha256
        )
        addressed_temporary = None
        stable_temporary.replace(stable_path)
        stable_temporary = None
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)

    return {
        "status": "NORMALIZED_LOCAL_VERIFIED_RAW",
        "schema": SCHEMA,
        "source_manifest_sha256": source_sha256,
        "content_sha256": content_sha256,
        "manifest_file_sha256": manifest_file_sha256,
        "manifest_path": str(stable_path),
        "content_addressed_manifest_path": str(addressed_path),
        "instance_count": manifest_core["summary"]["instance_count"],
        "encounter_count": manifest_core["summary"]["encounter_count"],
        "record_count": manifest_core["summary"]["record_count"],
        "unknown_field_count": manifest_core["summary"]["unknown_field_count"],
        "partitions": [str(build.final_path) for build in builds],
        "network_request_count": 0,
        "raw_object_copy_count": 0,
        "worker_count": min(workers, max(1, len(builds))),
        "stale_temporary_cleanup": {
            "requested": clean_stale_temporaries,
            "removed_count": stale_count,
            "removed_bytes": stale_bytes,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decode verified local Chronicle External API core streams into "
            "content-addressed normalized JSONL.gz partitions"
        )
    )
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--clean-stale-temporaries", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_external_core_event_normalization(
            source_manifest_path=args.source_manifest,
            data_root=args.data_root,
            output_directory=args.output_dir,
            workers=args.workers,
            clean_stale_temporaries=args.clean_stale_temporaries,
        )
    except ChronicleExternalEventNormalizerError as error:
        print(f"Chronicle core-event normalization failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = [
    "CORE_STREAM_TYPES",
    "ChronicleExternalEventNormalizerError",
    "ChronicleExternalWireError",
    "RECORD_SCHEMA",
    "SCHEMA",
    "build_external_core_event_normalization",
    "decode_damage",
    "decode_event_meta",
    "decode_event_stream",
    "decode_heal",
    "decode_slain",
    "decode_spell_fail",
    "decode_spell_go",
    "decode_spell_start",
    "decode_unit_classification",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
