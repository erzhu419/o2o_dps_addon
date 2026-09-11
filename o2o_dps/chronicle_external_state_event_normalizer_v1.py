"""Build a nonvoting state-event sidecar from immutable Chronicle raw objects.

This module covers the official state-bearing streams intentionally excluded
from ``chronicle_external_event_normalizer_v1``.  It reads only a
content-addressed External API manifest and its verified object references;
there is no network path and no raw-object copy path.  The output is prefix
evidence only and is never, by itself, a policy or comparison input.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence

from .chronicle_external_event_normalizer_v1 import (
    ChronicleExternalEventNormalizerError as ChronicleExternalStateEventNormalizerError,
    ChronicleExternalWireError as ChronicleExternalStateWireError,
    DecodedEnvelope,
    WireField,
    _as_int32,
    _as_int64,
    _attach_unknown,
    _bytes_value,
    _canonical_bytes,
    _clean_stale_temporaries,
    _count_unknown_fields,
    _int_value,
    _load_source_manifest,
    _publish_immutable,
    _read_bytes,
    _read_object_reference,
    _read_uvarint,
    _resolve_raw_root,
    _sha256,
    _sha256_file,
    _text_value,
    _unknown,
    _utc_iso_from_milliseconds,
    _wire_fields,
    _write_temporary,
    decode_event_meta,
    decode_spell_data,
)


SCHEMA = "chronicle_external_state_event_normalization/v1"
RECORD_SCHEMA = "chronicle_external_state_event/v1"
SOURCE_MANIFEST_SCHEMA = "chronicle_external_api_ingest/v1"
OUTPUT_STATUS = "STATE_SIDECAR_PREFIX_INPUT_NOT_POLICY_NOT_COMPARISON"
IMPLEMENTATION_REVISION = (
    "v1.2_fixed_proto_boundary_nontraining_numeric_preservation_manifest_last"
)
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
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "chronicle_external_state_events" / "v1"
)

# This order is part of artifact identity and is used only after encounter
# timestamp and EventMeta.index.  It does not assign causal precedence.
STATE_STREAM_TYPES = (
    "resource_change",
    "aura",
    "aura_cast",
    "extra_attack",
    "consume",
)
STREAM_ORDER = {name: index for index, name in enumerate(STATE_STREAM_TYPES)}
STREAM_TO_RECORD_TYPE = {
    "resource_change": "RESOURCE_CHANGE",
    "aura": "AURA",
    "aura_cast": "AURA_CAST",
    "extra_attack": "EXTRA_ATTACK",
    "consume": "CONSUME",
}
ENUM_FIELDS_BY_STREAM = {
    "aura": ("application", "state"),
    "consume": ("kind", "confidence"),
}

AURA_APPLICATION_NAMES = {
    0: "ApplicationUnknown",
    1: "ApplicationGains",
    2: "ApplicationFades",
    3: "ApplicationRemoved",
}
AURA_STATE_NAMES = {
    0: "StateUnknown",
    1: "StateAdded",
    2: "StateRemoved",
    3: "StateModified",
}
EVIDENCE_KIND_NAMES = {
    0: "EvidenceUnknown",
    1: "EvidenceDirectItem",
    2: "EvidenceCast",
    3: "EvidenceAura",
    4: "EvidenceHeal",
    5: "EvidenceResource",
    6: "EvidenceDamage",
    7: "EvidenceActiveAtPull",
    8: "EvidenceCooldown",
}
EVIDENCE_CONFIDENCE_NAMES = {
    0: "ConfidenceUnknown",
    1: "ConfidenceDirect",
    2: "ConfidenceEffectDerived",
    3: "ConfidenceAmbiguous",
    4: "ConfidenceInferred",
}
CONTAMINATION_LABELS = {
    "SUSPECT_36YD_RANGE_BUG",
    "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
    "POSTFIX_KNOWN_CLEAN",
    "NO_KNOWN_RULE_MATCH",
    "UNKNOWN_NONVOTING",
}
_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    manifest_entry: dict[str, Any]


def _mark_singular(seen: set[int], field: WireField, *, label: str) -> None:
    if field.number in seen:
        raise ChronicleExternalStateWireError(
            f"protobuf message has duplicate singular field {label}"
        )
    seen.add(field.number)


def _bool_value(field: WireField, *, label: str) -> bool:
    value = _int_value(field, label=label)
    if value not in (0, 1):
        raise ChronicleExternalStateWireError(
            f"protobuf bool {label} must be encoded as 0 or 1"
        )
    return bool(value)


def _enum_value(
    field: WireField, names: Mapping[int, str], *, label: str
) -> dict[str, Any]:
    value = _as_int32(_int_value(field, label=label))
    name = names.get(value)
    if name is None:
        # Proto3 enums are open: a producer may emit a numeric value that the
        # pinned schema does not yet name. Preserve the wire value without
        # inventing semantics. The whole sidecar remains nonvoting, and any
        # consumer that needs an enum meaning must fail closed on this marker.
        return {
            "number": value,
            "name": None,
            "pinned_proto_known": False,
        }
    return {"number": value, "name": name}


def _unknown_enum_values(
    event: Mapping[str, Any], *, stream_type: str
) -> Counter[str]:
    values: Counter[str] = Counter()
    for field_name in ENUM_FIELDS_BY_STREAM.get(stream_type, ()):
        enum_value = event.get(field_name)
        if not isinstance(enum_value, Mapping):
            continue
        if enum_value.get("pinned_proto_known") is not False:
            continue
        number = enum_value.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise ChronicleExternalStateWireError(
                f"preserved unknown enum {stream_type}.{field_name} lacks numeric value"
            )
        if enum_value.get("name") is not None:
            raise ChronicleExternalStateWireError(
                f"preserved unknown enum {stream_type}.{field_name} fabricates a name"
            )
        values[f"{stream_type}.{field_name}:{number}"] += 1
    return values


def _packed_int32_values(field: WireField, *, label: str) -> Iterator[int]:
    if field.wire_type == 0:
        yield _as_int32(_int_value(field, label=label))
        return
    if field.wire_type != 2:
        raise ChronicleExternalStateWireError(
            f"protobuf field {label} has wire type {field.wire_type}, expected 0 or 2"
        )
    payload = _bytes_value(field, label=label)
    offset = 0
    while offset < len(payload):
        value, offset = _read_uvarint(payload, offset, len(payload))
        yield _as_int32(value)


def decode_resource_change(data: bytes) -> dict[str, Any]:
    """Decode pinned ``ResourceChange`` fields 1, 3..10."""

    result: dict[str, Any] = {
        "meta": None,
        "target": "",
        "amount": 0,
        "resource_type": "",
        "caster": None,
        "source_name": None,
        "direction": "",
        "spell_data": None,
        "over_resource": 0,
    }
    unknown: list[dict[str, Any]] = []
    seen: set[int] = set()
    for field in _wire_fields(data):
        if field.number == 1:
            _mark_singular(seen, field, label="ResourceChange.meta")
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="ResourceChange.meta")
            )
        elif field.number == 3:
            _mark_singular(seen, field, label="ResourceChange.target")
            result["target"] = _text_value(field, label="ResourceChange.target")
        elif field.number == 4:
            _mark_singular(seen, field, label="ResourceChange.amount")
            result["amount"] = _as_int32(
                _int_value(field, label="ResourceChange.amount")
            )
        elif field.number == 5:
            _mark_singular(seen, field, label="ResourceChange.resourceType")
            result["resource_type"] = _text_value(
                field, label="ResourceChange.resourceType"
            )
        elif field.number == 6:
            _mark_singular(seen, field, label="ResourceChange.caster")
            result["caster"] = _text_value(field, label="ResourceChange.caster")
        elif field.number == 7:
            _mark_singular(seen, field, label="ResourceChange.sourceName")
            result["source_name"] = _text_value(
                field, label="ResourceChange.sourceName"
            )
        elif field.number == 8:
            _mark_singular(seen, field, label="ResourceChange.direction")
            result["direction"] = _text_value(
                field, label="ResourceChange.direction"
            )
        elif field.number == 9:
            _mark_singular(seen, field, label="ResourceChange.spellData")
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="ResourceChange.spellData")
            )
        elif field.number == 10:
            _mark_singular(seen, field, label="ResourceChange.overResource")
            result["over_resource"] = _as_int32(
                _int_value(field, label="ResourceChange.overResource")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_aura(data: bytes) -> dict[str, Any]:
    """Decode pinned ``Aura`` fields and official enum names."""

    result: dict[str, Any] = {
        "meta": None,
        "target": "",
        "spell_name": "",
        "current_amount": 0,
        "application": {"number": 0, "name": AURA_APPLICATION_NAMES[0]},
        "state": {"number": 0, "name": AURA_STATE_NAMES[0]},
        "spell_data": None,
        "is_buff": False,
    }
    unknown: list[dict[str, Any]] = []
    seen: set[int] = set()
    for field in _wire_fields(data):
        if field.number == 1:
            _mark_singular(seen, field, label="Aura.meta")
            result["meta"] = decode_event_meta(_bytes_value(field, label="Aura.meta"))
        elif field.number == 2:
            _mark_singular(seen, field, label="Aura.target")
            result["target"] = _text_value(field, label="Aura.target")
        elif field.number == 3:
            _mark_singular(seen, field, label="Aura.spellName")
            result["spell_name"] = _text_value(field, label="Aura.spellName")
        elif field.number == 4:
            _mark_singular(seen, field, label="Aura.currentAmount")
            result["current_amount"] = _as_int32(
                _int_value(field, label="Aura.currentAmount")
            )
        elif field.number == 5:
            _mark_singular(seen, field, label="Aura.application")
            result["application"] = _enum_value(
                field, AURA_APPLICATION_NAMES, label="AuraApplication"
            )
        elif field.number == 6:
            _mark_singular(seen, field, label="Aura.state")
            result["state"] = _enum_value(field, AURA_STATE_NAMES, label="AuraState")
        elif field.number == 7:
            _mark_singular(seen, field, label="Aura.spellData")
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="Aura.spellData")
            )
        elif field.number == 8:
            _mark_singular(seen, field, label="Aura.isBuff")
            result["is_buff"] = _bool_value(field, label="Aura.isBuff")
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_aura_cast(data: bytes) -> dict[str, Any]:
    """Decode pinned ``AuraCast`` fields 1..10."""

    result: dict[str, Any] = {
        "meta": None,
        "spell": None,
        "caster": "",
        "target": None,
        "effect": 0,
        "amplitude": 0,
        "effect_misc_value": 0,
        "duration_ms": 0,
        "cap_status": 0,
        "effect_aura_name": 0,
    }
    unknown: list[dict[str, Any]] = []
    seen: set[int] = set()
    for field in _wire_fields(data):
        if field.number == 1:
            _mark_singular(seen, field, label="AuraCast.meta")
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="AuraCast.meta")
            )
        elif field.number == 2:
            _mark_singular(seen, field, label="AuraCast.spell")
            result["spell"] = decode_spell_data(
                _bytes_value(field, label="AuraCast.spell")
            )
        elif field.number == 3:
            _mark_singular(seen, field, label="AuraCast.caster")
            result["caster"] = _text_value(field, label="AuraCast.caster")
        elif field.number == 4:
            _mark_singular(seen, field, label="AuraCast.target")
            result["target"] = _text_value(field, label="AuraCast.target")
        elif field.number == 5:
            _mark_singular(seen, field, label="AuraCast.effect")
            result["effect"] = _as_int32(
                _int_value(field, label="AuraCast.effect")
            )
        elif field.number == 6:
            _mark_singular(seen, field, label="AuraCast.amplitude")
            result["amplitude"] = _as_int32(
                _int_value(field, label="AuraCast.amplitude")
            )
        elif field.number == 7:
            _mark_singular(seen, field, label="AuraCast.effectMiscValue")
            result["effect_misc_value"] = _as_int32(
                _int_value(field, label="AuraCast.effectMiscValue")
            )
        elif field.number == 8:
            _mark_singular(seen, field, label="AuraCast.durationMS")
            result["duration_ms"] = _as_int32(
                _int_value(field, label="AuraCast.durationMS")
            )
        elif field.number == 9:
            _mark_singular(seen, field, label="AuraCast.capStatus")
            result["cap_status"] = _as_int32(
                _int_value(field, label="AuraCast.capStatus")
            )
        elif field.number == 10:
            _mark_singular(seen, field, label="AuraCast.effectAuraName")
            result["effect_aura_name"] = _as_int32(
                _int_value(field, label="AuraCast.effectAuraName")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_extra_attack(data: bytes) -> dict[str, Any]:
    """Decode pinned ``ExtraAttack`` fields 1, 2, 3, 5 and 6."""

    result: dict[str, Any] = {
        "meta": None,
        "target": "",
        "amount": 0,
        "source_name": "",
        "spell_data": None,
    }
    unknown: list[dict[str, Any]] = []
    seen: set[int] = set()
    for field in _wire_fields(data):
        if field.number == 1:
            _mark_singular(seen, field, label="ExtraAttack.meta")
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="ExtraAttack.meta")
            )
        elif field.number == 2:
            _mark_singular(seen, field, label="ExtraAttack.target")
            result["target"] = _text_value(field, label="ExtraAttack.target")
        elif field.number == 3:
            _mark_singular(seen, field, label="ExtraAttack.amount")
            result["amount"] = _as_int32(
                _int_value(field, label="ExtraAttack.amount")
            )
        elif field.number == 5:
            _mark_singular(seen, field, label="ExtraAttack.sourceName")
            result["source_name"] = _text_value(
                field, label="ExtraAttack.sourceName"
            )
        elif field.number == 6:
            _mark_singular(seen, field, label="ExtraAttack.spellData")
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="ExtraAttack.spellData")
            )
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


def decode_consume(data: bytes) -> dict[str, Any]:
    """Decode pinned ``Consume``, including packed candidate item IDs."""

    result: dict[str, Any] = {
        "meta": None,
        "consume_id": "",
        "evidence_id": "",
        "player": "",
        "item_id": None,
        "candidate_item_ids": [],
        "spell_data": None,
        "kind": {"number": 0, "name": EVIDENCE_KIND_NAMES[0]},
        "confidence": {"number": 0, "name": EVIDENCE_CONFIDENCE_NAMES[0]},
        "consumed_at_unix_ms": None,
        "observed_at_unix_ms": 0,
        "amount": None,
        "resource_type": None,
        "is_projection": False,
        "item_name": None,
    }
    unknown: list[dict[str, Any]] = []
    seen: set[int] = set()
    for field in _wire_fields(data):
        if field.number == 1:
            _mark_singular(seen, field, label="Consume.meta")
            result["meta"] = decode_event_meta(
                _bytes_value(field, label="Consume.meta")
            )
        elif field.number == 2:
            _mark_singular(seen, field, label="Consume.consumeId")
            result["consume_id"] = _text_value(field, label="Consume.consumeId")
        elif field.number == 3:
            _mark_singular(seen, field, label="Consume.evidenceId")
            result["evidence_id"] = _text_value(field, label="Consume.evidenceId")
        elif field.number == 4:
            _mark_singular(seen, field, label="Consume.player")
            result["player"] = _text_value(field, label="Consume.player")
        elif field.number == 5:
            _mark_singular(seen, field, label="Consume.itemId")
            result["item_id"] = _as_int32(
                _int_value(field, label="Consume.itemId")
            )
        elif field.number == 6:
            result["candidate_item_ids"].extend(
                _packed_int32_values(field, label="Consume.candidateItemIds")
            )
        elif field.number == 7:
            _mark_singular(seen, field, label="Consume.spellData")
            result["spell_data"] = decode_spell_data(
                _bytes_value(field, label="Consume.spellData")
            )
        elif field.number == 8:
            _mark_singular(seen, field, label="Consume.kind")
            result["kind"] = _enum_value(
                field, EVIDENCE_KIND_NAMES, label="EvidenceKind"
            )
        elif field.number == 9:
            _mark_singular(seen, field, label="Consume.confidence")
            result["confidence"] = _enum_value(
                field, EVIDENCE_CONFIDENCE_NAMES, label="EvidenceConfidence"
            )
        elif field.number == 10:
            _mark_singular(seen, field, label="Consume.consumedAtUnixMilli")
            result["consumed_at_unix_ms"] = _as_int64(
                _int_value(field, label="Consume.consumedAtUnixMilli")
            )
        elif field.number == 11:
            _mark_singular(seen, field, label="Consume.observedAtUnixMilli")
            result["observed_at_unix_ms"] = _as_int64(
                _int_value(field, label="Consume.observedAtUnixMilli")
            )
        elif field.number == 12:
            _mark_singular(seen, field, label="Consume.amount")
            result["amount"] = _as_int32(_int_value(field, label="Consume.amount"))
        elif field.number == 13:
            _mark_singular(seen, field, label="Consume.resourceType")
            result["resource_type"] = _text_value(
                field, label="Consume.resourceType"
            )
        elif field.number == 14:
            _mark_singular(seen, field, label="Consume.isProjection")
            result["is_projection"] = _bool_value(
                field, label="Consume.isProjection"
            )
        elif field.number == 15:
            _mark_singular(seen, field, label="Consume.itemName")
            result["item_name"] = _text_value(field, label="Consume.itemName")
        else:
            unknown.append(_unknown(field))
    _attach_unknown(result, unknown)
    return result


DECODERS: Mapping[str, Callable[[bytes], dict[str, Any]]] = {
    "resource_change": decode_resource_change,
    "aura": decode_aura,
    "aura_cast": decode_aura_cast,
    "extra_attack": decode_extra_attack,
    "consume": decode_consume,
}


def decode_event_stream(
    compressed: bytes, *, stream_type: str
) -> list[dict[str, Any]]:
    """Decode Chronicle's bounded encounter frames for one state stream."""

    decoder = DECODERS.get(stream_type)
    if decoder is None:
        raise ChronicleExternalStateEventNormalizerError(
            f"unsupported state event stream: {stream_type!r}"
        )
    if not isinstance(compressed, bytes) or not compressed.startswith(b"\x1f\x8b"):
        raise ChronicleExternalStateWireError("event object is not a gzip byte stream")
    try:
        data = gzip.decompress(compressed)
    except (OSError, EOFError) as error:
        raise ChronicleExternalStateWireError(
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
            raise ChronicleExternalStateWireError(
                "encounter_id is not valid UTF-8"
            ) from error
        if not encounter_id:
            raise ChronicleExternalStateWireError("encounter_id must not be empty")
        first_timestamp_ms, offset = _read_uvarint(data, offset, end)
        count, offset = _read_uvarint(data, offset, end)
        data_length, offset = _read_uvarint(data, offset, end)
        body_end = offset + data_length
        if body_end > end:
            raise ChronicleExternalStateWireError(
                "encounter frame body exceeds stream"
            )
        if count > data_length:
            raise ChronicleExternalStateWireError(
                "encounter message count exceeds bounded frame capacity"
            )
        messages: list[dict[str, Any]] = []
        for _ in range(count):
            message_bytes, offset = _read_bytes(data, offset, body_end)
            event = decoder(message_bytes)
            if event.get("meta") is None:
                raise ChronicleExternalStateWireError(
                    f"{stream_type} message lacks EventMeta and cannot be ordered"
                )
            messages.append(
                {
                    "message_sha256": _sha256(message_bytes),
                    "event": event,
                }
            )
        if offset != body_end:
            raise ChronicleExternalStateWireError(
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


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _spell_projection(
    event: Mapping[str, Any], *, stream_type: str
) -> tuple[str | None, int | None, list[str]]:
    if stream_type == "aura_cast":
        spell_data = event.get("spell")
        top_name = None
    else:
        spell_data = event.get("spell_data")
        if stream_type == "aura":
            top_name = _identifier(event.get("spell_name"))
        elif stream_type in {"resource_change", "extra_attack"}:
            top_name = _identifier(event.get("source_name"))
        else:
            top_name = None
    spell_name: str | None = None
    spell_id: int | None = None
    if isinstance(spell_data, Mapping):
        spell_name = _identifier(spell_data.get("name"))
        raw_id = spell_data.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            spell_id = raw_id
    diagnostics: list[str] = []
    if top_name and spell_name and top_name != spell_name:
        diagnostics.append("TOP_LEVEL_AND_SPELL_DATA_NAME_CONFLICT_NO_NAME_PROJECTION")
        return None, spell_id, diagnostics
    return spell_name or top_name, spell_id, diagnostics


def _raid_provenance(instance: Mapping[str, Any]) -> dict[str, Any]:
    raw_label = instance.get("instance_contamination_label")
    if raw_label is None:
        label = "UNKNOWN_NONVOTING"
        label_source = "SOURCE_FIELD_MISSING_DEFAULT_NONVOTING"
    elif not isinstance(raw_label, str) or raw_label not in CONTAMINATION_LABELS:
        raise ChronicleExternalStateEventNormalizerError(
            "instance contamination label is outside the source contract"
        )
    else:
        label = raw_label
        label_source = "SOURCE_MANIFEST_INSTANCE_FIELD"
    return {
        "started_at": instance.get("started_at"),
        "contamination_label": label,
        "contamination_label_source": label_source,
        "contamination_guild_context": instance.get("contamination_guild_context"),
        "contamination_guild_evidence": instance.get("contamination_guild_evidence"),
        "classification_scope": "RAID_LEVEL_INHERITED_NO_PLAYER_NAME_FILTER",
    }


def _normalized_record(
    envelope: DecodedEnvelope,
    *,
    instance_id: str,
    source_manifest_sha256: str,
    source_object_sha256: str,
    encounter_ordinal: int,
    output_line: int,
    raid_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    event = envelope.event
    meta = event.get("meta")
    if not isinstance(meta, Mapping):
        raise ChronicleExternalStateEventNormalizerError("decoded event has no EventMeta")
    event_index = meta.get("event_index")
    offset_ms = meta.get("offset_ms")
    if isinstance(event_index, bool) or not isinstance(event_index, int):
        raise ChronicleExternalStateEventNormalizerError(
            "EventMeta.index is not an integer"
        )
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, int):
        raise ChronicleExternalStateEventNormalizerError(
            "EventMeta.offsetMilli is not an integer"
        )
    timestamp_ms = envelope.first_timestamp_ms + offset_ms
    stream_type = envelope.stream_type
    spell, spell_id, diagnostics = _spell_projection(
        event, stream_type=stream_type
    )

    source_guid: str | None
    target_guid: str | None
    value: int | None
    if stream_type == "resource_change":
        source_guid = _identifier(event.get("caster"))
        target_guid = _identifier(event.get("target"))
        value = int(event["amount"])
    elif stream_type == "aura":
        source_guid = None
        target_guid = _identifier(event.get("target"))
        value = int(event["current_amount"])
    elif stream_type == "aura_cast":
        source_guid = _identifier(event.get("caster"))
        target_guid = _identifier(event.get("target"))
        value = None
    elif stream_type == "extra_attack":
        source_guid = None
        target_guid = _identifier(event.get("target"))
        value = int(event["amount"])
    elif stream_type == "consume":
        source_guid = _identifier(event.get("player"))
        target_guid = None
        raw_amount = event.get("amount")
        value = (
            raw_amount
            if isinstance(raw_amount, int) and not isinstance(raw_amount, bool)
            else None
        )
    else:  # pragma: no cover - construction is closed over STATE_STREAM_TYPES.
        raise ChronicleExternalStateEventNormalizerError(
            f"cannot project unsupported state stream {stream_type}"
        )

    state_payload = {key: child for key, child in event.items() if key != "meta"}
    return {
        "schema": RECORD_SCHEMA,
        "status": OUTPUT_STATUS,
        "instance": instance_id,
        "encounter": envelope.encounter_id,
        "encounter_ordinal": encounter_ordinal,
        "first_timestamp_ms": envelope.first_timestamp_ms,
        "event_index": event_index,
        "offset_ms": offset_ms,
        "timestamp_ms": timestamp_ms,
        "time": _utc_iso_from_milliseconds(timestamp_ms),
        "stream_type": stream_type,
        "type": STREAM_TO_RECORD_TYPE[stream_type],
        "source_guid": source_guid,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "synthetic": bool(meta.get("is_synthetic")),
        "event_meta": meta,
        "state_payload": state_payload,
        "official": {
            "stream_type": stream_type,
            "message": event,
            "message_sha256": envelope.message_sha256,
        },
        "bridge": {
            "projection_fidelity": "EXACT_FIELDS_WITH_NONAUTHORITATIVE_COMMON_PROJECTION",
            "diagnostics": diagnostics,
            "identifier_policy": "official_identifiers_copied_verbatim_no_owner_or_name_inference",
        },
        "raid_provenance": dict(raid_provenance),
        "provenance": {
            "format": "chronicle_external_api_state_event_v1",
            "source_manifest_sha256": source_manifest_sha256,
            "source_object_sha256": source_object_sha256,
            "stream_type": stream_type,
            "frame_index": envelope.frame_index,
            "frame_message_index": envelope.frame_message_index,
            "output_line": output_line,
            "raw_object_copied": False,
        },
    }


def _instance_envelopes(
    instance: Mapping[str, Any], *, raw_root: Path
) -> tuple[
    list[DecodedEnvelope],
    dict[str, Any],
    int,
    dict[str, int],
    dict[str, int],
]:
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ChronicleExternalStateEventNormalizerError(
            "source instance row lacks instance_id"
        )
    streams = instance.get("streams")
    if not isinstance(streams, Mapping):
        raise ChronicleExternalStateEventNormalizerError(
            f"instance {instance_id} lacks streams"
        )

    envelopes: list[DecodedEnvelope] = []
    stream_evidence: dict[str, Any] = {}
    encounter_origins: dict[str, int] = {}
    unknown_count = 0
    unknown_enum_values: Counter[str] = Counter()
    for stream_type in STATE_STREAM_TYPES:
        wrapper = streams.get(stream_type)
        if not isinstance(wrapper, Mapping) or wrapper.get("status") != "AVAILABLE":
            raise ChronicleExternalStateEventNormalizerError(
                f"instance {instance_id} state stream {stream_type} is unavailable"
            )
        reference = wrapper.get("object")
        compressed, _ = _read_object_reference(
            raw_root, reference, label=f"{instance_id}.{stream_type}"
        )
        frames = decode_event_stream(compressed, stream_type=stream_type)
        seen_encounters: set[str] = set()
        message_count = 0
        stream_unknown = 0
        stream_unknown_enum_values: Counter[str] = Counter()
        frame_evidence: list[dict[str, Any]] = []
        for frame_index, frame in enumerate(frames):
            encounter_id = str(frame["encounter_id"])
            if encounter_id in seen_encounters:
                raise ChronicleExternalStateEventNormalizerError(
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
                raise ChronicleExternalStateEventNormalizerError(
                    f"encounter {encounter_id} has conflicting frame origins: "
                    f"{previous_origin} != {origin}"
                )
            for message_index, message in enumerate(frame["messages"]):
                decoded = message["event"]
                stream_unknown += _count_unknown_fields(decoded)
                stream_unknown_enum_values.update(
                    _unknown_enum_values(decoded, stream_type=stream_type)
                )
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
            "unknown_enum_value_count": sum(stream_unknown_enum_values.values()),
            "unknown_enum_values": dict(sorted(stream_unknown_enum_values.items())),
        }
        unknown_count += stream_unknown
        unknown_enum_values.update(stream_unknown_enum_values)

    encounter_order = {
        encounter_id: ordinal
        for ordinal, (encounter_id, _) in enumerate(
            sorted(encounter_origins.items(), key=lambda item: (item[1], item[0]))
        )
    }

    def order_key(envelope: DecodedEnvelope) -> tuple[int, int, int, int, int, int, str]:
        meta = envelope.event["meta"]
        assert isinstance(meta, Mapping)
        timestamp_ms = envelope.first_timestamp_ms + int(meta["offset_ms"])
        return (
            encounter_order[envelope.encounter_id],
            timestamp_ms,
            int(meta["event_index"]),
            STREAM_ORDER[envelope.stream_type],
            envelope.frame_index,
            envelope.frame_message_index,
            envelope.message_sha256,
        )

    envelopes.sort(key=order_key)
    return (
        envelopes,
        stream_evidence,
        unknown_count,
        dict(sorted(unknown_enum_values.items())),
        encounter_order,
    )


def _write_partition(
    instance: Mapping[str, Any],
    *,
    raw_root: Path,
    output_directory: Path,
    source_manifest_sha256: str,
) -> PartitionBuild:
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ChronicleExternalStateEventNormalizerError(
            "source instance row lacks instance_id"
        )
    if _SAFE_INSTANCE_ID.fullmatch(instance_id) is None:
        raise ChronicleExternalStateEventNormalizerError(
            f"instance id is unsafe for a partition filename: {instance_id!r}"
        )
    raid_provenance = _raid_provenance(instance)
    (
        envelopes,
        stream_evidence,
        unknown_count,
        unknown_enum_values,
        encounter_order,
    ) = _instance_envelopes(instance, raw_root=raw_root)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{instance_id}.state-events.",
        suffix=".jsonl.gz.tmp",
        dir=output_directory,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    logical_digest = hashlib.sha256()
    stream_counts: Counter[str] = Counter()
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
                        raid_provenance=raid_provenance,
                    )
                    line = _canonical_bytes(record) + b"\n"
                    logical_digest.update(line)
                    compressed.write(line)
                    stream_counts[envelope.stream_type] += 1
        logical_sha256 = logical_digest.hexdigest()
        final_path = output_directory / (
            f"{instance_id}.{logical_sha256}.state-events.jsonl.gz"
        )
        compressed_sha256 = _sha256_file(temporary_path)
        entry = {
            "instance_id": instance_id,
            "slug": instance.get("slug"),
            "raid_provenance": raid_provenance,
            "partition": final_path.name,
            "logical_content_sha256": logical_sha256,
            "compressed_file_sha256": compressed_sha256,
            "compressed_size_bytes": temporary_path.stat().st_size,
            "record_count": len(envelopes),
            "encounter_count": len(encounter_order),
            "stream_counts": dict(sorted(stream_counts.items())),
            "unknown_field_count": unknown_count,
            "unknown_enum_value_count": sum(unknown_enum_values.values()),
            "unknown_enum_values": unknown_enum_values,
            "source_streams": stream_evidence,
            "raw_object_open_count": len(STATE_STREAM_TYPES),
            "raw_object_copy_count": 0,
        }
        return PartitionBuild(temporary_path, final_path, entry)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_partitions(
    instances: Sequence[Mapping[str, Any]],
    *,
    raw_root: Path,
    output_directory: Path,
    source_manifest_sha256: str,
    workers: int,
) -> list[PartitionBuild]:
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
    with ProcessPoolExecutor(max_workers=min(workers, len(instances))) as executor:
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


def build_external_state_event_normalization(
    *,
    source_manifest_path: str | Path,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
    clean_stale_temporaries: bool = False,
) -> dict[str, Any]:
    """Build deterministic per-instance state-event sidecar partitions."""

    if isinstance(workers, bool) or not isinstance(workers, int):
        raise TypeError("workers must be an integer")
    if not 1 <= workers <= 32:
        raise ChronicleExternalStateEventNormalizerError("workers must be in 1..32")
    if not isinstance(clean_stale_temporaries, bool):
        raise TypeError("clean_stale_temporaries must be boolean")

    source_path = Path(source_manifest_path).expanduser().resolve()
    raw_root = _resolve_raw_root(Path(data_root))
    expected_manifest_directory = (raw_root / "manifests").resolve()
    try:
        source_path.relative_to(expected_manifest_directory)
    except ValueError as error:
        raise ChronicleExternalStateEventNormalizerError(
            "source manifest must be inside the External API manifest store"
        ) from error
    source, source_bytes, source_sha256 = _load_source_manifest(source_path)

    output_dir = Path(output_directory).expanduser().resolve()
    if output_dir == raw_root or raw_root in output_dir.parents:
        raise ChronicleExternalStateEventNormalizerError(
            "state sidecar output must not be inside the immutable raw store"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    stale_count = 0
    stale_bytes = 0
    if clean_stale_temporaries:
        stale_count, stale_bytes = _clean_stale_temporaries(output_dir)

    raw_instances = source["instances"]
    assert isinstance(raw_instances, list)
    instances: list[Mapping[str, Any]] = []
    seen_instance_ids: set[str] = set()
    for index, raw_instance in enumerate(raw_instances):
        if not isinstance(raw_instance, Mapping):
            raise ChronicleExternalStateEventNormalizerError(
                f"source instance {index} is not an object"
            )
        instance_id = raw_instance.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ChronicleExternalStateEventNormalizerError(
                f"source instance {index} lacks instance_id"
            )
        if instance_id in seen_instance_ids:
            raise ChronicleExternalStateEventNormalizerError(
                f"source manifest repeats instance_id {instance_id}"
            )
        seen_instance_ids.add(instance_id)
        instances.append(raw_instance)

    builds: list[PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        builds = _write_partitions(
            instances,
            raw_root=raw_root,
            output_directory=output_dir,
            source_manifest_sha256=source_sha256,
            workers=workers,
        )
        unknown_enum_values: Counter[str] = Counter()
        for build in builds:
            unknown_enum_values.update(build.manifest_entry["unknown_enum_values"])
        manifest_core: dict[str, Any] = {
            "schema": SCHEMA,
            "status": OUTPUT_STATUS,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": "chronicle_external_state_event_sidecar_manifest",
            "source": {
                "manifest_sha256": source_sha256,
                "manifest_size_bytes": len(source_bytes),
                "manifest_schema": SOURCE_MANIFEST_SCHEMA,
                "parser_contract_revision": source.get("parser_contract_revision"),
                "contamination_contract": source.get("contamination_contract"),
            },
            "official_contract": {
                "proto_commit": PROTO_COMMIT,
                "proto_url": PROTO_URL,
                "event_stream_documentation_url": EVENT_STREAM_DOC_URL,
                "messages": [
                    "ResourceChange",
                    "Aura",
                    "AuraCast",
                    "ExtraAttack",
                    "Consume",
                ],
                "framing": "gzip_then_repeated_bounded_encounter_frames_then_length_delimited_messages",
                "timestamp": "first_timestamp_ms_plus_EventMeta.offsetMilli",
                "enum_policy": (
                    "known_numeric_values_mapped_to_exact_pinned_proto_identifiers;"
                    "unknown_proto3_enum_numbers_preserved_with_name_null_and_"
                    "pinned_proto_known_false"
                ),
                "combatant_info_decoded_here": False,
            },
            "normalization_contract": {
                "state_stream_types": list(STATE_STREAM_TYPES),
                "event_order_within_each_instance": [
                    "encounter(first_timestamp_ms,encounter_id)",
                    "event_timestamp_ms",
                    "EventMeta.index",
                    "fixed_stream_tiebreaker",
                    "frame_index",
                    "frame_message_index",
                    "message_sha256",
                ],
                "fixed_stream_tiebreaker": dict(STREAM_ORDER),
                "cross_instance_merge": False,
                "unknown_protobuf_fields": "exact_raw_wire_field_base64_at_message_or_nested_message",
                "unknown_proto3_enum_values": {
                    "wire_number_preserved": True,
                    "semantic_name_fabricated": False,
                    "record_marker": {
                        "name": None,
                        "pinned_proto_known": False,
                    },
                    "semantic_consumers_must_fail_closed": True,
                    "voting_authorized": False,
                },
                "source_object_sha256_on_every_record": True,
                "raw_objects_copied": 0,
                "network_requests": 0,
                "manifest_committed_last": True,
            },
            "raid_provenance_contract": {
                "authority": "source_manifest_instance_fields_only",
                "missing_label": "UNKNOWN_NONVOTING",
                "guild_date_reclassification_applied": False,
                "player_name_filter_applied": False,
                "scope": "raid_level_not_person_level",
            },
            "claim_boundary": {
                "prefix_state_input_only": True,
                "policy_input_authorized": False,
                "comparison_input_authorized": False,
                "training_authorized": False,
                "future_fill_allowed": False,
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
                "unknown_enum_value_count": sum(unknown_enum_values.values()),
                "unknown_enum_values": dict(sorted(unknown_enum_values.items())),
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
            f"chronicle_external_state_events_v1.{content_sha256}.manifest.json"
        )
        stable_path = output_dir / "manifest.json"
        addressed_temporary = _write_temporary(addressed_path, manifest_payload)
        stable_temporary = _write_temporary(stable_path, manifest_payload)

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
        "status": OUTPUT_STATUS,
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
        "unknown_enum_value_count": manifest_core["summary"][
            "unknown_enum_value_count"
        ],
        "unknown_enum_values": manifest_core["summary"]["unknown_enum_values"],
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
            "Decode verified local Chronicle state streams into a nonvoting, "
            "content-addressed prefix sidecar"
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
        result = build_external_state_event_normalization(
            source_manifest_path=args.source_manifest,
            data_root=args.data_root,
            output_directory=args.output_dir,
            workers=args.workers,
            clean_stale_temporaries=args.clean_stale_temporaries,
        )
    except ChronicleExternalStateEventNormalizerError as error:
        print(f"Chronicle state-event sidecar normalization failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = [
    "OUTPUT_STATUS",
    "RECORD_SCHEMA",
    "SCHEMA",
    "STATE_STREAM_TYPES",
    "ChronicleExternalStateEventNormalizerError",
    "ChronicleExternalStateWireError",
    "build_external_state_event_normalization",
    "decode_aura",
    "decode_aura_cast",
    "decode_consume",
    "decode_event_stream",
    "decode_extra_attack",
    "decode_resource_change",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
