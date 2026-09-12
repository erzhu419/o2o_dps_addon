"""Admit verified Chronicle External API evidence for a versioned reconstructor.

This is deliberately not an adapter to the legacy manual-CSV pipeline.  It
binds an immutable External API raw manifest to the corresponding normalized
core-event manifest and publishes a new, content-addressed evidence contract.
No raw object or normalized row is copied into the admission artifact.

The streaming row adapter adds structured, evidence-bounded identity facts for
a future reconstruction consumer.  It never rewrites legacy ``outcome`` text,
never guesses hostile classification, and never claims membership in the
frozen 50-instance capsule or authorization for policy comparison.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, Mapping, Sequence

from .chronicle_combatant_sidecar import (
    ChronicleStreamDecodeError,
    decode_combatant_info_stream,
)
from .chronicle_external_api_ingest_v1 import (
    IMPLEMENTATION_REVISION as RAW_IMPLEMENTATION_REVISION,
    PARSER_CONTRACT_REVISION as RAW_PARSER_CONTRACT_REVISION,
    classify_range_bug,
)
from .chronicle_external_event_normalizer_v1 import (
    CORE_STREAM_TYPES,
    ChronicleExternalEventNormalizerError,
    DecodedEnvelope,
    IMPLEMENTATION_REVISION as NORMALIZER_IMPLEMENTATION_REVISION,
    RECORD_SCHEMA as NORMALIZED_RECORD_SCHEMA,
    SCHEMA as NORMALIZATION_SCHEMA,
    STREAM_ORDER,
    STREAM_TO_NORMALIZED_TYPE,
    _normalized_record,
    decode_event_stream,
)


SCHEMA = "chronicle_external_reconstruction_admission/v1"
IMPLEMENTATION_REVISION = (
    "v1.4_exact_guid_core_observed_missing_combatant_info_diagnostic"
)
RAW_MANIFEST_SCHEMA = "chronicle_external_api_ingest/v1"
RAW_MANIFEST_KIND = "chronicle_external_api_raw_snapshot"
STATUS = "ADMITTED_FOR_VERSIONED_RECONSTRUCTION_INPUT"
SOURCE_EVIDENCE_KIND = "external_api_stream_set"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_external_reconstruction_admission"
    / "v1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RAW_MANIFEST_NAME_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_NORMALIZATION_MANIFEST_NAME_RE = re.compile(
    r"^chronicle_external_core_events_v1\.([0-9a-f]{64})\.manifest\.json$"
)
_ADMISSION_MANIFEST_NAME_RE = re.compile(
    r"^chronicle_external_reconstruction_admission_v1\."
    r"([0-9a-f]{64})\.manifest\.json$"
)
_GUID_RE = re.compile(r"^(?:0x)?[0-9A-F]+$", re.IGNORECASE)


class ChronicleExternalAdmissionError(RuntimeError):
    """An input identity, evidence relationship, or admission artifact is invalid."""


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
        raise ChronicleExternalAdmissionError(
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
        raise ChronicleExternalAdmissionError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalAdmissionError(
            f"{label} is not UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleExternalAdmissionError(f"{label} must be a JSON object")
    return value


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalAdmissionError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalAdmissionError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleExternalAdmissionError(f"{label} must be a non-empty string")
    return value.strip()


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalAdmissionError(f"{label} must be an integer")
    return value


def _sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ChronicleExternalAdmissionError(f"{label} must be a lowercase SHA-256")
    return value


def _parse_rfc3339(value: Any, *, label: str) -> datetime:
    text = _text(value, label=label)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    # Python 3.10 rejects otherwise valid RFC3339 fractions unless they have
    # exactly three or six digits. Chronicle API timestamps may use a shorter
    # fraction (for example ``.88Z``), so normalize only that representation.
    normalized = re.sub(
        r"\.(\d{1,5})(?=[+-]\d{2}:\d{2}$)",
        lambda match: "." + match.group(1).ljust(6, "0"),
        normalized,
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ChronicleExternalAdmissionError(
            f"{label} is not RFC3339: {text!r}"
        ) from error
    if parsed.tzinfo is None:
        raise ChronicleExternalAdmissionError(f"{label} lacks a UTC offset")
    return parsed.astimezone(timezone.utc)


def _datetime_epoch_milliseconds(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - epoch
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def canonical_guid(value: Any, *, label: str = "guid") -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ChronicleExternalAdmissionError(
            f"{label} must be a non-empty GUID without surrounding whitespace"
        )
    text = value
    if not _GUID_RE.fullmatch(text):
        raise ChronicleExternalAdmissionError(f"{label} is not a hexadecimal GUID")
    digits = text[2:] if text[:2].casefold() == "0x" else text
    return "0x" + digits.upper()


def canonicalize_optional_0x_owner(value: Any) -> str | None:
    """Return the exact owner hex digits in the legacy-neutral no-``0x`` form.

    Removing the optional notation prefix is representation normalization, not
    suffix matching or player attribution.  Leading zeroes are retained.
    """

    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ChronicleExternalAdmissionError(
            "UnitClassification.owner must not contain surrounding whitespace"
        )
    text = value
    if not _GUID_RE.fullmatch(text):
        raise ChronicleExternalAdmissionError(
            "UnitClassification.owner is not hexadecimal"
        )
    return (text[2:] if text[:2].casefold() == "0x" else text).upper()


def _under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.expanduser().resolve())
    except ValueError as error:
        raise ChronicleExternalAdmissionError(
            f"{label} must remain under data_root"
        ) from error
    return resolved


def _relative_to_data_root(path: Path, data_root: Path) -> str:
    return path.resolve().relative_to(data_root.resolve()).as_posix()


def _load_raw_manifest(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"cannot read raw API manifest {path}: {error}"
        ) from error
    digest = _sha256(payload)
    match = _RAW_MANIFEST_NAME_RE.fullmatch(path.name)
    if match is None or match.group(1) != digest:
        raise ChronicleExternalAdmissionError(
            "raw API manifest filename/content SHA-256 mismatch"
        )
    value = _json_object(payload, label="raw API manifest")
    if value.get("schema") != RAW_MANIFEST_SCHEMA:
        raise ChronicleExternalAdmissionError(
            f"raw API manifest schema must be {RAW_MANIFEST_SCHEMA}"
        )
    if (
        value.get("kind") != RAW_MANIFEST_KIND
        or value.get("implementation_revision") != RAW_IMPLEMENTATION_REVISION
        or value.get("parser_contract_revision") != RAW_PARSER_CONTRACT_REVISION
    ):
        raise ChronicleExternalAdmissionError(
            "raw API manifest kind/ingest/parser revision is not the corrected locked contract"
        )
    _array(value.get("instances"), label="raw API manifest.instances")
    return value, payload, digest


def _verify_content_address(document: Mapping[str, Any], *, label: str) -> str:
    address = _mapping(document.get("content_address"), label=f"{label}.content_address")
    if address.get("algorithm") != "sha256" or address.get("scope") != (
        "canonical_JSON_excluding_content_address"
    ):
        raise ChronicleExternalAdmissionError(
            f"{label} has an unsupported content-address contract"
        )
    declared = _sha(address.get("sha256"), label=f"{label}.content_address.sha256")
    core = {key: value for key, value in document.items() if key != "content_address"}
    actual = _sha256(_canonical_bytes(core))
    if actual != declared:
        raise ChronicleExternalAdmissionError(
            f"{label} content address mismatch: {declared} != {actual}"
        )
    return declared


def _load_normalization_manifest(
    path: Path,
) -> tuple[dict[str, Any], bytes, str, str]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"cannot read normalization manifest {path}: {error}"
        ) from error
    value = _json_object(payload, label="normalization manifest")
    if value.get("schema") != NORMALIZATION_SCHEMA:
        raise ChronicleExternalAdmissionError(
            f"normalization manifest schema must be {NORMALIZATION_SCHEMA}"
        )
    content_sha = _verify_content_address(value, label="normalization manifest")
    match = _NORMALIZATION_MANIFEST_NAME_RE.fullmatch(path.name)
    if match is None or match.group(1) != content_sha:
        raise ChronicleExternalAdmissionError(
            "normalization manifest filename/content address mismatch"
        )
    return value, payload, _sha256(payload), content_sha


def _read_object_reference(
    raw_root: Path,
    reference: Any,
    *,
    label: str,
) -> tuple[bytes, Path]:
    ref = _mapping(reference, label=f"{label} object reference")
    relative_raw = _text(ref.get("relative_path"), label=f"{label}.relative_path")
    digest = _sha(ref.get("sha256"), label=f"{label}.sha256")
    size = _integer(ref.get("size_bytes"), label=f"{label}.size_bytes")
    if size < 0:
        raise ChronicleExternalAdmissionError(f"{label}.size_bytes is negative")
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalAdmissionError(f"{label} object path escapes raw root")
    path = (raw_root / relative).resolve()
    try:
        path.relative_to(raw_root.resolve())
    except ValueError as error:
        raise ChronicleExternalAdmissionError(
            f"{label} object path escapes raw root"
        ) from error
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"cannot read {label} object {path}: {error}"
        ) from error
    if len(payload) != size or _sha256(payload) != digest:
        raise ChronicleExternalAdmissionError(
            f"{label} object hash/size verification failed"
        )
    return payload, path


def _verify_raw_object_closure(
    document: Any, raw_root: Path
) -> tuple[dict[tuple[str, str, int], bytes], int]:
    cache: dict[tuple[str, str, int], bytes] = {}
    total_bytes = 0

    def visit(value: Any, label: str) -> None:
        nonlocal total_bytes
        if isinstance(value, Mapping):
            if {"relative_path", "sha256", "size_bytes"}.issubset(value):
                relative = _text(value.get("relative_path"), label=f"{label}.relative_path")
                digest = _sha(value.get("sha256"), label=f"{label}.sha256")
                size = _integer(value.get("size_bytes"), label=f"{label}.size_bytes")
                key = (relative, digest, size)
                if key not in cache:
                    payload, _ = _read_object_reference(raw_root, value, label=label)
                    cache[key] = payload
                    total_bytes += len(payload)
                return
            for key, child in value.items():
                visit(child, f"{label}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{label}[{index}]")

    visit(document, "raw_manifest")
    return cache, total_bytes


def _cached_object(
    cache: Mapping[tuple[str, str, int], bytes], reference: Mapping[str, Any]
) -> bytes:
    key = (
        _text(reference.get("relative_path"), label="object.relative_path"),
        _sha(reference.get("sha256"), label="object.sha256"),
        _integer(reference.get("size_bytes"), label="object.size_bytes"),
    )
    try:
        return cache[key]
    except KeyError as error:
        raise ChronicleExternalAdmissionError(
            "verified raw object is absent from closure cache"
        ) from error


def _player_resolver(metadata: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    players_raw = _mapping(metadata.get("players"), label="metadata.players")
    players: list[dict[str, Any]] = []
    by_guid: dict[str, dict[str, Any]] = {}
    for raw_guid, raw_player in players_raw.items():
        guid = canonical_guid(raw_guid, label="metadata player GUID")
        if guid in by_guid:
            raise ChronicleExternalAdmissionError(
                f"duplicate canonical metadata player GUID {guid}"
            )
        player = _mapping(raw_player, label=f"metadata.players[{raw_guid!r}]")
        record = {"guid": guid, "metadata": deepcopy(dict(player))}
        players.append(record)
        by_guid[guid] = record
    players.sort(key=lambda row: row["guid"])
    if not players:
        raise ChronicleExternalAdmissionError("metadata player resolver is empty")
    return players, by_guid


def _metadata_evidence(
    raw_instance: Mapping[str, Any],
    *,
    cache: Mapping[tuple[str, str, int], bytes],
) -> tuple[dict[str, Any], Mapping[str, Any], dict[str, dict[str, Any]]]:
    wrapper = _mapping(raw_instance.get("metadata"), label="instance.metadata")
    reference = _mapping(wrapper.get("object"), label="instance.metadata.object")
    metadata = _json_object(_cached_object(cache, reference), label="metadata object")
    instance_id = _text(raw_instance.get("instance_id"), label="instance.instance_id")
    if metadata.get("id") != instance_id:
        raise ChronicleExternalAdmissionError("metadata instance id mismatch")
    slug = _text(raw_instance.get("slug"), label="instance.slug")
    if metadata.get("slug") != slug:
        raise ChronicleExternalAdmissionError("metadata instance slug mismatch")
    if raw_instance.get("instance_name") != metadata.get("name"):
        raise ChronicleExternalAdmissionError("metadata instance name mismatch")

    players, by_guid = _player_resolver(metadata)
    encounters = _array(metadata.get("encounters"), label="metadata.encounters")
    encounter_starts: list[datetime] = []
    encounter_ids: set[str] = set()
    for index, raw in enumerate(encounters):
        encounter = _mapping(raw, label=f"metadata.encounters[{index}]")
        encounter_id = _text(encounter.get("id"), label="metadata encounter id")
        if encounter_id in encounter_ids:
            raise ChronicleExternalAdmissionError("duplicate metadata encounter id")
        encounter_ids.add(encounter_id)
        if encounter.get("instance_id") != instance_id:
            raise ChronicleExternalAdmissionError("metadata encounter instance mismatch")
        encounter_starts.append(
            _parse_rfc3339(encounter.get("start_time"), label="encounter.start_time")
        )
    if not encounter_starts:
        raise ChronicleExternalAdmissionError("metadata contains no encounters")

    started_at = _text(raw_instance.get("started_at"), label="instance.started_at")
    started_dt = _parse_rfc3339(started_at, label="instance.started_at")
    if started_dt != min(encounter_starts):
        raise ChronicleExternalAdmissionError(
            "instance started_at does not equal metadata encounter minimum"
        )
    raw_guild = metadata.get("guild")
    guild = deepcopy(dict(raw_guild)) if isinstance(raw_guild, Mapping) else None
    guild_context = raw_instance.get("contamination_guild_context")
    guild_evidence = raw_instance.get("contamination_guild_evidence")
    if guild_context is not None:
        guild_context = _text(
            guild_context, label="instance.contamination_guild_context"
        )
        if not isinstance(guild_evidence, str) or not guild_evidence:
            raise ChronicleExternalAdmissionError(
                "nonempty contamination guild context lacks evidence"
            )
    elif guild_evidence is not None:
        raise ChronicleExternalAdmissionError(
            "missing contamination guild context must also have missing evidence"
        )

    # Metadata-derived guild evidence is independently rechecked here. Other
    # evidence forms stay labelled as upstream provenance and are never
    # silently upgraded to metadata evidence.
    if guild_evidence == "metadata.guild.name":
        if not isinstance(guild, Mapping) or guild.get("name") != guild_context:
            raise ChronicleExternalAdmissionError(
                "contamination guild context disagrees with metadata.guild.name"
            )
    elif guild_evidence == "metadata.guild_name":
        if metadata.get("guild_name") != guild_context:
            raise ChronicleExternalAdmissionError(
                "contamination guild context disagrees with metadata.guild_name"
            )
    elif guild_evidence not in {
        None,
        "activity.guild_name",
        "leaderboard_snapshot.unique_guild_name",
    }:
        raise ChronicleExternalAdmissionError(
            "contamination guild evidence kind is unsupported"
        )

    expected_contamination = classify_range_bug(guild_context, started_at)
    if raw_instance.get("instance_contamination_label") != expected_contamination:
        raise ChronicleExternalAdmissionError(
            "instance contamination label fails frozen started_at/guild rule"
        )

    evidence = {
        "metadata_object": deepcopy(dict(reference)),
        "instance_name": metadata.get("name"),
        "started_at": started_at,
        "started_at_source": raw_instance.get("started_at_source"),
        "started_at_source_field": raw_instance.get("started_at_source_field"),
        "admission_verified_started_at_source": "metadata.encounters.min(start_time)",
        "started_at_verified_against_metadata_encounter_minimum": True,
        "uploaded_at": raw_instance.get("uploaded_at"),
        "uploaded_at_source": raw_instance.get("uploaded_at_source"),
        "guild": guild,
        "guild_evidence": guild_evidence,
        "contamination": {
            "label": expected_contamination,
            "guild_context": guild_context,
            "guild_evidence": guild_evidence,
            "time_field": "started_at",
            "uploaded_at_used": False,
        },
        "encounter_count": len(encounter_ids),
        "encounter_ids_sha256": _sha256(_canonical_bytes(sorted(encounter_ids))),
        "player_resolver": {
            "kind": "metadata_exact_player_guid_resolver",
            "player_count": len(players),
            "players_sha256": _sha256(_canonical_bytes(players)),
            "players": players,
            "name_or_class_inference_used": False,
        },
    }
    return evidence, metadata, by_guid


def _combatant_info_evidence(
    raw_instance: Mapping[str, Any],
    *,
    cache: Mapping[tuple[str, str, int], bytes],
    metadata: Mapping[str, Any],
    players_by_guid: Mapping[str, Mapping[str, Any]],
    verified_player_event_observations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    streams = _mapping(raw_instance.get("streams"), label="instance.streams")
    wrapper = _mapping(streams.get("combatant_info"), label="combatant_info stream")
    if wrapper.get("status") != "AVAILABLE":
        raise ChronicleExternalAdmissionError("combatant_info stream is unavailable")
    reference = _mapping(wrapper.get("object"), label="combatant_info.object")
    payload = _cached_object(cache, reference)
    try:
        frames = decode_combatant_info_stream(payload)
    except ChronicleStreamDecodeError as error:
        raise ChronicleExternalAdmissionError(
            f"combatant_info stream cannot be decoded: {error}"
        ) from error

    metadata_encounter_ids = {
        _text(_mapping(row, label="metadata encounter").get("id"), label="encounter id")
        for row in _array(metadata.get("encounters"), label="metadata.encounters")
    }
    frame_ids = [str(frame["encounter_id"]) for frame in frames]
    if len(frame_ids) != len(set(frame_ids)) or set(frame_ids) != metadata_encounter_ids:
        raise ChronicleExternalAdmissionError(
            "combatant_info frames do not exactly match metadata encounters"
        )

    encounter_starts_ms = {
        _text(_mapping(row, label="metadata encounter").get("id"), label="encounter id"):
        _datetime_epoch_milliseconds(
            _parse_rfc3339(
                _mapping(row, label="metadata encounter").get("start_time"),
                label="metadata encounter start_time",
            )
        )
        for row in _array(metadata.get("encounters"), label="metadata.encounters")
    }
    for frame in frames:
        encounter_id = str(frame["encounter_id"])
        if frame.get("first_timestamp_ms") != encounter_starts_ms[encounter_id]:
            raise ChronicleExternalAdmissionError(
                "combatant_info frame origin disagrees with metadata encounter start"
            )

    messages = [message for frame in frames for message in frame["messages"]]
    if any(message.get("meta") is None for message in messages):
        raise ChronicleExternalAdmissionError(
            "combatant_info contains an unanchored message"
        )
    for message in messages:
        meta = _mapping(message.get("meta"), label="combatant_info EventMeta")
        _integer(meta.get("event_index"), label="combatant_info EventMeta.index")
        _integer(meta.get("offset_ms"), label="combatant_info EventMeta.offset_ms")
    by_guid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in messages:
        guid = canonical_guid(message.get("guid"), label="CombatantInfo.guid")
        by_guid[guid].append(message)
    metadata_guids = set(players_by_guid)
    combatant_guids = set(by_guid)
    missing_metadata_guids = sorted(metadata_guids - combatant_guids)
    extra_combatant_guids = sorted(combatant_guids - metadata_guids)
    missing_without_core_event_evidence = [
        guid
        for guid in missing_metadata_guids
        if _integer(
            _mapping(
                verified_player_event_observations.get(guid),
                label=f"verified core-event observations for {guid}",
            ).get("source_or_target_event_count"),
            label=f"verified core-event source_or_target_event_count for {guid}",
        )
        <= 0
    ]
    if missing_without_core_event_evidence:
        raise ChronicleExternalAdmissionError(
            "combatant_info missing metadata player GUID lacks verified "
            "core-event source/target evidence: "
            f"{missing_without_core_event_evidence[:5]!r}"
        )

    missing_metadata_player_diagnostics: list[dict[str, Any]] = []
    for guid in missing_metadata_guids:
        metadata_player = _mapping(
            players_by_guid[guid].get("metadata"), label="resolved metadata player"
        )
        observation = deepcopy(
            dict(
                _mapping(
                    verified_player_event_observations.get(guid),
                    label=f"verified core-event observations for {guid}",
                )
            )
        )
        missing_metadata_player_diagnostics.append(
            {
                "guid": guid,
                "metadata_name": metadata_player.get("name"),
                "metadata_hero_class": metadata_player.get("class"),
                "metadata_race": metadata_player.get("race"),
                "combatant_info_status": "MISSING_FROM_ALL_METADATA_ENCOUNTER_FRAMES",
                "combatant_info_frame_count_searched": len(frames),
                "identity_resolution": (
                    "EXACT_METADATA_GUID_WITH_VERIFIED_CORE_EVENT_PRESENCE"
                ),
                "verified_core_event_evidence": observation,
                "combatant_info_static_context": {
                    "display_name": "UNAVAILABLE_NOT_IMPUTED",
                    "hero_class": "UNAVAILABLE_NOT_IMPUTED",
                    "display_race": "UNAVAILABLE_NOT_IMPUTED",
                    "display_guild": "UNAVAILABLE_NOT_IMPUTED",
                    "gear": "UNAVAILABLE_NOT_IMPUTED",
                    "talents": "UNAVAILABLE_NOT_IMPUTED",
                    "spec": "UNKNOWN_NOT_INFERRED_FROM_MISSING_COMBATANT_INFO",
                },
            }
        )

    identity_conflicts: list[dict[str, Any]] = []
    name_transition_player_count = 0
    race_transition_player_count = 0
    guild_transition_player_count = 0
    optional_guild_presence_transition_player_count = 0
    display_transition_diagnostics: dict[str, list[dict[str, Any]]] = {
        "name": [],
        "race": [],
        "guild_name": [],
    }

    def display_transition_diagnostic(
        *,
        guid: str,
        rows: Sequence[Mapping[str, Any]],
        field: str,
        metadata_value: Any,
        casefold: bool = False,
    ) -> dict[str, Any] | None:
        values = [row.get(field) for row in rows]

        def normalized(value: Any) -> Any:
            if casefold and isinstance(value, str):
                return value.casefold()
            return value

        normalized_values = [normalized(value) for value in values]
        if len(set(normalized_values)) <= 1:
            return None
        counts = Counter(values)
        ordered_values = sorted(
            counts,
            key=lambda value: (
                value is not None,
                str(value).casefold(),
                str(value),
            ),
        )
        return {
            "guid": guid,
            "metadata_value": metadata_value,
            "observed_value_counts": [
                {"value": value, "message_count": counts[value]}
                for value in ordered_values
            ],
            "sequential_transition_count": sum(
                previous != current
                for previous, current in zip(
                    normalized_values, normalized_values[1:]
                )
            ),
        }

    for guid, rows in sorted(by_guid.items()):
        if guid not in players_by_guid:
            # CombatantInfo can contain a late joiner absent from the instance
            # metadata roster. Preserve its presence as non-resolving evidence;
            # it must never expand the exact metadata player resolver.
            continue
        metadata_player = _mapping(
            players_by_guid[guid].get("metadata"), label="resolved metadata player"
        )
        names = {str(row.get("name") or "") for row in rows}
        classes = {str(row.get("hero_class") or "").casefold() for row in rows}
        races = {str(row.get("race") or "").casefold() for row in rows}
        metadata_name = _text(
            metadata_player.get("name"), label="metadata player name"
        )
        metadata_class_raw = _text(
            metadata_player.get("class"), label="metadata player class"
        )
        metadata_race_raw = _text(
            metadata_player.get("race"), label="metadata player race"
        )
        metadata_class = metadata_class_raw.casefold()
        metadata_race = metadata_race_raw.casefold()
        if metadata_name not in names:
            identity_conflicts.append(
                {"guid": guid, "fields": ["metadata_name_not_observed"]}
            )
        if metadata_class not in classes:
            identity_conflicts.append(
                {"guid": guid, "fields": ["metadata_hero_class_not_observed"]}
            )
        if len(classes) > 1:
            identity_conflicts.append(
                {"guid": guid, "fields": ["hero_class_transition"]}
            )
        if metadata_race not in races:
            # Race is not an attribution key, but requiring the immutable
            # metadata value to occur at least once prevents an unrelated or
            # wholly corrupted display-race series from passing admission.
            identity_conflicts.append(
                {"guid": guid, "fields": ["metadata_race_not_observed"]}
            )

        name_transition = display_transition_diagnostic(
            guid=guid,
            rows=rows,
            field="name",
            metadata_value=metadata_name,
        )
        if name_transition is not None:
            # Some official frames temporarily expose an unknown-target display
            # name for the same exact GUID. Names never drive attribution, so
            # retain the transition diagnostically while requiring the metadata
            # identity to appear in at least one frame.
            name_transition_player_count += 1
            display_transition_diagnostics["name"].append(name_transition)

        race_transition = display_transition_diagnostic(
            guid=guid,
            rows=rows,
            field="race",
            metadata_value=metadata_player.get("race"),
            casefold=True,
        )
        if race_transition is not None:
            # Chronicle reports the character's current display race here.
            # Disguises or transformations may therefore change it while the
            # exact GUID, class, name and guild remain stable.  Preserve the
            # observation, but never use it to resolve or attribute an entity.
            race_transition_player_count += 1
            display_transition_diagnostics["race"].append(race_transition)

        guild_transition = display_transition_diagnostic(
            guid=guid,
            rows=rows,
            field="guild_name",
            metadata_value=None,
        )
        if guild_transition is not None:
            guild_transition_player_count += 1
            display_transition_diagnostics["guild_name"].append(guild_transition)
            guild_values = {row.get("guild_name") for row in rows}
            if None in guild_values and any(
                value is not None for value in guild_values
            ):
                optional_guild_presence_transition_player_count += 1
    if identity_conflicts:
        raise ChronicleExternalAdmissionError(
            "combatant_info has a metadata identity or stable-class conflict: "
            f"{identity_conflicts[:5]!r}"
        )

    result = {
        "status": "VERIFIED_LOCAL_OFFICIAL_STREAM",
        "object": deepcopy(dict(reference)),
        "frame_count": len(frames),
        "message_count": len(messages),
        "unique_player_guid_count": len(by_guid),
        "metadata_player_guid_set_equal": not (
            missing_metadata_guids or extra_combatant_guids
        ),
        "metadata_player_guid_subset_of_combatant_info": not missing_metadata_guids,
        "combatant_info_guid_subset_of_metadata_players": not extra_combatant_guids,
        "missing_metadata_combatant_info_player_count": len(
            missing_metadata_guids
        ),
        "missing_metadata_combatant_info_guids_sha256": _sha256(
            _canonical_bytes(missing_metadata_guids)
        ),
        "missing_metadata_combatant_info_players": (
            missing_metadata_player_diagnostics
        ),
        "message_with_talents_count": sum(
            message.get("talents") is not None for message in messages
        ),
        "message_with_gear_count": sum(bool(message.get("gear")) for message in messages),
        "unanchored_message_count": 0,
        "identity_conflict_count": 0,
        "metadata_name_observed_player_count": len(metadata_guids)
        - len(missing_metadata_guids),
        "metadata_hero_class_observed_player_count": len(metadata_guids)
        - len(missing_metadata_guids),
        "metadata_race_observed_player_count": len(metadata_guids)
        - len(missing_metadata_guids),
        "hero_class_transition_player_count": 0,
        "display_name_transition_player_count": name_transition_player_count,
        "display_race_transition_player_count": race_transition_player_count,
        "display_guild_transition_player_count": guild_transition_player_count,
        "optional_guild_presence_transition_player_count": (
            optional_guild_presence_transition_player_count
        ),
        "display_transition_diagnostics": display_transition_diagnostics,
        "identity_resolution_contract": {
            "attribution_key": "metadata exact canonical player GUID",
            "combatant_info_present_metadata_name_must_appear": True,
            "combatant_info_present_metadata_hero_class_must_appear": True,
            "combatant_info_present_hero_class_must_be_stable": True,
            "combatant_info_present_metadata_race_must_appear": True,
            "metadata_race_appearance_is_integrity_check_not_attribution": True,
            "missing_metadata_player_requires_verified_core_event_guid_evidence": True,
            "missing_player_combatant_info_static_fields_imputed": False,
            "missing_player_talents_gear_or_spec_inferred": False,
            "display_name_used_for_identity_or_attribution": False,
            "display_race_used_for_identity_or_attribution": False,
            "display_guild_used_for_identity_or_attribution": False,
            "display_name_race_or_guild_transitions_are_diagnostic_only": True,
        },
        "frame_origin_matches_metadata_encounter_start": True,
        "anchor_locator_verified_count": len(messages),
        "selection_contract": (
            "same encounter and exact player GUID; maximum "
            "(EventMeta.index, stream-global message_ordinal) among records with "
            "EventMeta.index <= reconstruction action EventMeta.index"
        ),
        "rows_copied_into_admission_manifest": 0,
    }
    if extra_combatant_guids:
        result["nonmetadata_combatant_guid_count"] = len(extra_combatant_guids)
        result["nonmetadata_combatant_guids_sha256"] = _sha256(
            _canonical_bytes(extra_combatant_guids)
        )
        result["nonmetadata_combatants_added_to_player_resolver"] = 0
    return result


def _warrior_spec_evidence(
    raw_instance: Mapping[str, Any],
    *,
    players_by_guid: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    raw_observations = _array(
        raw_instance.get("warrior_observations"), label="warrior_observations"
    )
    observations: list[dict[str, Any]] = []
    recomputed = {"Arms": 0, "Fury": 0, "Other_or_unknown": 0}
    conflict_count = 0
    unknown_count = 0
    instance_label = raw_instance.get("instance_contamination_label")
    metadata_warriors = {
        guid: _mapping(record.get("metadata"), label="metadata warrior")
        for guid, record in players_by_guid.items()
        if str(
            _mapping(record.get("metadata"), label="metadata player").get("class")
            or ""
        ).casefold()
        == "warrior"
    }
    observed_guids: set[str] = set()
    for index, raw in enumerate(raw_observations):
        row = _mapping(raw, label=f"warrior_observations[{index}]")
        guid = canonical_guid(row.get("player_guid"), label="warrior player_guid")
        if guid not in metadata_warriors:
            raise ChronicleExternalAdmissionError(
                "warrior observation GUID is absent from metadata Warrior set"
            )
        if guid in observed_guids:
            raise ChronicleExternalAdmissionError("duplicate warrior observation GUID")
        observed_guids.add(guid)
        if str(row.get("player_class") or "").casefold() != "warrior":
            raise ChronicleExternalAdmissionError(
                "warrior observation player_class is not Warrior"
            )
        if row.get("player_name") != metadata_warriors[guid].get("name"):
            raise ChronicleExternalAdmissionError(
                "warrior observation player_name disagrees with metadata"
            )
        if row.get("contamination_label") != instance_label:
            raise ChronicleExternalAdmissionError(
                "warrior observation contamination label mismatch"
            )
        if (
            row.get("contamination_guild_context")
            != raw_instance.get("contamination_guild_context")
            or row.get("contamination_guild_evidence")
            != raw_instance.get("contamination_guild_evidence")
        ):
            raise ChronicleExternalAdmissionError(
                "warrior observation guild contamination provenance mismatch"
            )
        spec = str(row.get("player_spec") or "")
        if spec == "Arms":
            recomputed["Arms"] += 1
        elif spec == "Fury":
            recomputed["Fury"] += 1
        else:
            recomputed["Other_or_unknown"] += 1
        conflicts = row.get("field_conflicts")
        if not isinstance(conflicts, Mapping):
            raise ChronicleExternalAdmissionError(
                "warrior observation field_conflicts must be an object"
            )
        if conflicts:
            conflict_count += 1
        if row.get("spec_evidence_status") != "OBSERVED":
            unknown_count += 1
        observations.append(deepcopy(dict(row)))

    if observed_guids != set(metadata_warriors):
        raise ChronicleExternalAdmissionError(
            "warrior observations do not exactly cover metadata Warriors"
        )

    declared = _mapping(
        raw_instance.get("warrior_spec_counts"), label="warrior_spec_counts"
    )
    if dict(declared) != recomputed:
        raise ChronicleExternalAdmissionError(
            "warrior_spec_counts disagree with preserved observations"
        )
    return {
        "observations": observations,
        "declared_counts": deepcopy(dict(declared)),
        "recomputed_counts": recomputed,
        "observation_count": len(observations),
        "field_conflict_observation_count": conflict_count,
        "non_observed_or_unknown_count": unknown_count,
        "arms_and_fury_merged": False,
        "conflicts_preserved_not_resolved": True,
    }


def _partition_path(
    normalization_manifest_path: Path, partition: Mapping[str, Any]
) -> Path:
    name = _text(partition.get("partition"), label="partition.partition")
    if Path(name).name != name:
        raise ChronicleExternalAdmissionError("partition filename must be a basename")
    path = (normalization_manifest_path.parent / name).resolve()
    try:
        path.relative_to(normalization_manifest_path.parent.resolve())
    except ValueError as error:
        raise ChronicleExternalAdmissionError("partition path escapes its manifest") from error
    return path


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


def _decoded_raw_core_expectations(
    raw_instance: Mapping[str, Any],
    *,
    cache: Mapping[tuple[str, str, int], bytes],
    metadata_encounter_ids: set[str],
) -> tuple[
    list[DecodedEnvelope],
    dict[str, dict[str, Any]],
    dict[str, int],
]:
    """Re-decode verified raw objects exactly as the locked normalizer does."""

    raw_streams = _mapping(raw_instance.get("streams"), label="raw instance streams")
    envelopes: list[DecodedEnvelope] = []
    stream_evidence: dict[str, dict[str, Any]] = {}
    encounter_origins: dict[str, int] = {}
    framed_encounters: set[str] = set()
    for stream_type in CORE_STREAM_TYPES:
        wrapper = _mapping(raw_streams.get(stream_type), label=f"raw {stream_type}")
        if wrapper.get("status") != "AVAILABLE":
            raise ChronicleExternalAdmissionError(
                f"raw core stream {stream_type} is unavailable"
            )
        reference = _mapping(wrapper.get("object"), label=f"raw {stream_type}.object")
        try:
            frames = decode_event_stream(
                _cached_object(cache, reference), stream_type=stream_type
            )
        except ChronicleExternalEventNormalizerError as error:
            raise ChronicleExternalAdmissionError(
                f"raw core stream {stream_type} cannot be re-decoded: {error}"
            ) from error

        frames_evidence: list[dict[str, Any]] = []
        seen_in_stream: set[str] = set()
        stream_unknown_count = 0
        message_count = 0
        for frame_index, frame in enumerate(frames):
            encounter_id = _text(
                frame.get("encounter_id"), label=f"raw {stream_type} encounter id"
            )
            if encounter_id in seen_in_stream:
                raise ChronicleExternalAdmissionError(
                    f"raw {stream_type} contains duplicate encounter frame"
                )
            if encounter_id not in metadata_encounter_ids:
                raise ChronicleExternalAdmissionError(
                    f"raw {stream_type} contains an encounter absent from metadata"
                )
            seen_in_stream.add(encounter_id)
            framed_encounters.add(encounter_id)
            first_timestamp_ms = _integer(
                frame.get("first_timestamp_ms"),
                label=f"raw {stream_type} first_timestamp_ms",
            )
            messages = _array(
                frame.get("messages"), label=f"raw {stream_type} frame messages"
            )
            frame_message_count = len(messages)
            frames_evidence.append(
                {
                    "frame_index": frame_index,
                    "encounter_id": encounter_id,
                    "first_timestamp_ms": first_timestamp_ms,
                    "message_count": frame_message_count,
                }
            )
            placeholder_origin = first_timestamp_ms == 0 and frame_message_count == 0
            previous_origin = encounter_origins.get(encounter_id)
            if previous_origin is None:
                encounter_origins[encounter_id] = first_timestamp_ms
            elif previous_origin == 0 and first_timestamp_ms != 0:
                encounter_origins[encounter_id] = first_timestamp_ms
            elif (
                not placeholder_origin
                and previous_origin != 0
                and previous_origin != first_timestamp_ms
            ):
                raise ChronicleExternalAdmissionError(
                    f"raw encounter {encounter_id} has conflicting frame origins"
                )

            for frame_message_index, raw_message in enumerate(messages):
                message = _mapping(
                    raw_message,
                    label=f"raw {stream_type} frame message",
                )
                event = _mapping(
                    message.get("event"), label=f"raw {stream_type} decoded event"
                )
                meta = _mapping(
                    event.get("meta"), label=f"raw {stream_type} EventMeta"
                )
                event_index = _integer(
                    meta.get("event_index"), label=f"raw {stream_type} EventMeta.index"
                )
                offset_ms = _integer(
                    meta.get("offset_ms"), label=f"raw {stream_type} EventMeta.offset"
                )
                message_sha = _sha(
                    message.get("message_sha256"),
                    label=f"raw {stream_type} message SHA",
                )
                envelopes.append(
                    DecodedEnvelope(
                        stream_type=stream_type,
                        encounter_id=encounter_id,
                        first_timestamp_ms=first_timestamp_ms,
                        frame_index=frame_index,
                        frame_message_index=frame_message_index,
                        message_sha256=message_sha,
                        event=dict(event),
                    )
                )
                stream_unknown_count += _count_unknown_fields(event)
                message_count += 1
        stream_evidence[stream_type] = {
            "frame_count": len(frames),
            "frames": frames_evidence,
            "message_count": message_count,
            "unknown_field_count": stream_unknown_count,
        }

    if framed_encounters != metadata_encounter_ids:
        raise ChronicleExternalAdmissionError(
            "raw core encounter-frame union and metadata encounters differ"
        )
    encounter_order = {
        encounter_id: ordinal
        for ordinal, (encounter_id, _) in enumerate(
            sorted(encounter_origins.items(), key=lambda item: (item[1], item[0]))
        )
    }

    def order_key(envelope: DecodedEnvelope) -> tuple[int, int, int, int, int]:
        meta = _mapping(envelope.event.get("meta"), label="decoded raw EventMeta")
        return (
            encounter_order[envelope.encounter_id],
            envelope.first_timestamp_ms
            + _integer(meta.get("offset_ms"), label="decoded raw offset_ms"),
            _integer(meta.get("event_index"), label="decoded raw event_index"),
            STREAM_ORDER[envelope.stream_type],
            envelope.frame_message_index,
        )

    envelopes.sort(key=order_key)
    return envelopes, stream_evidence, encounter_order


def _verify_partition(
    partition: Mapping[str, Any],
    *,
    normalization_manifest_path: Path,
    raw_instance: Mapping[str, Any],
    raw_manifest_sha256: str,
    cache: Mapping[tuple[str, str, int], bytes],
    metadata: Mapping[str, Any],
    players_by_guid: Mapping[str, Mapping[str, Any]],
) -> tuple[Path, dict[str, Any], dict[str, dict[str, Any]]]:
    instance_id = _text(raw_instance.get("instance_id"), label="instance id")
    if partition.get("instance_id") != instance_id:
        raise ChronicleExternalAdmissionError("partition instance id mismatch")
    path = _partition_path(normalization_manifest_path, partition)
    expected_size = _integer(
        partition.get("compressed_size_bytes"), label="partition compressed size"
    )
    expected_compressed_sha = _sha(
        partition.get("compressed_file_sha256"), label="partition compressed SHA"
    )
    try:
        actual_size = path.stat().st_size
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"cannot stat normalized partition {path}: {error}"
        ) from error
    if actual_size != expected_size or _sha256_file(path) != expected_compressed_sha:
        raise ChronicleExternalAdmissionError(
            "normalized partition compressed hash/size verification failed"
        )

    raw_streams = _mapping(raw_instance.get("streams"), label="raw instance streams")
    declared_streams = _mapping(
        partition.get("source_streams"), label="partition.source_streams"
    )
    if set(declared_streams) != set(CORE_STREAM_TYPES):
        raise ChronicleExternalAdmissionError(
            "normalized source_streams is not the exact admitted core set"
        )
    metadata_encounter_ids = {
        _text(
            _mapping(row, label="metadata encounter").get("id"),
            label="metadata encounter id",
        )
        for row in _array(metadata.get("encounters"), label="metadata.encounters")
    }
    envelopes, raw_stream_evidence, encounter_order = _decoded_raw_core_expectations(
        raw_instance,
        cache=cache,
        metadata_encounter_ids=metadata_encounter_ids,
    )
    stream_hashes: dict[str, str] = {}
    for stream_type in CORE_STREAM_TYPES:
        raw_wrapper = _mapping(raw_streams.get(stream_type), label=f"raw {stream_type}")
        if raw_wrapper.get("status") != "AVAILABLE":
            raise ChronicleExternalAdmissionError(
                f"raw core stream {stream_type} is unavailable"
            )
        raw_ref = _mapping(raw_wrapper.get("object"), label=f"raw {stream_type}.object")
        declared = _mapping(
            declared_streams.get(stream_type), label=f"normalized {stream_type} evidence"
        )
        raw_sha = _sha(raw_ref.get("sha256"), label=f"raw {stream_type} SHA")
        raw_size = _integer(raw_ref.get("size_bytes"), label=f"raw {stream_type} size")
        if declared.get("object_sha256") != raw_sha or declared.get(
            "object_size_bytes"
        ) != raw_size:
            raise ChronicleExternalAdmissionError(
                f"normalized {stream_type} source evidence disagrees with raw manifest"
            )
        expected_stream_evidence = raw_stream_evidence[stream_type]
        for field in (
            "frame_count",
            "frames",
            "message_count",
            "unknown_field_count",
        ):
            if declared.get(field) != expected_stream_evidence[field]:
                raise ChronicleExternalAdmissionError(
                    f"normalized {stream_type} {field} disagrees with raw decode"
                )
        stream_hashes[stream_type] = raw_sha

    if partition.get("slug") != raw_instance.get("slug"):
        raise ChronicleExternalAdmissionError("partition slug mismatch")
    if (
        partition.get("raw_object_copy_count") != 0
        or partition.get("raw_object_open_count") != len(CORE_STREAM_TYPES)
    ):
        raise ChronicleExternalAdmissionError(
            "partition raw-object accounting disagrees with the locked normalizer"
        )

    logical_digest = hashlib.sha256()
    record_count = 0
    event_counts: Counter[str] = Counter()
    encounters: set[str] = set()
    unknown_count = 0
    owner_count = 0
    player_event_observations: dict[str, dict[str, Any]] = {
        guid: {
            "source_event_count": 0,
            "target_event_count": 0,
            "source_or_target_event_count": 0,
            "encounter_ids": set(),
            "event_type_counts": Counter(),
            "stream_type_counts": Counter(),
        }
        for guid in players_by_guid
    }
    previous_order: tuple[int, int, int, int, int] | None = None
    encounter_rows: dict[str, tuple[int, int]] = {}
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.endswith(b"\n"):
                    raise ChronicleExternalAdmissionError(
                        "normalized JSONL record lacks its canonical newline"
                    )
                logical_digest.update(raw_line)
                record = _json_object(raw_line, label=f"normalized row {line_number}")
                if record.get("schema") != NORMALIZED_RECORD_SCHEMA:
                    raise ChronicleExternalAdmissionError(
                        "normalized row schema mismatch"
                    )
                if record.get("instance") != instance_id:
                    raise ChronicleExternalAdmissionError(
                        "normalized row instance mismatch"
                    )
                encounter = _text(record.get("encounter"), label="row.encounter")
                encounters.add(encounter)
                event_index = _integer(record.get("event_index"), label="row.event_index")
                offset_ms = _integer(record.get("offset_ms"), label="row.offset_ms")
                timestamp_ms = _integer(record.get("timestamp_ms"), label="row.timestamp_ms")
                first_timestamp_ms = _integer(
                    record.get("first_timestamp_ms"), label="row.first_timestamp_ms"
                )
                if timestamp_ms != first_timestamp_ms + offset_ms:
                    raise ChronicleExternalAdmissionError(
                        "normalized row timestamp formula mismatch"
                    )
                provenance = _mapping(record.get("provenance"), label="row.provenance")
                stream_type = _text(
                    provenance.get("stream_type"), label="row provenance stream_type"
                )
                if stream_type not in CORE_STREAM_TYPES:
                    raise ChronicleExternalAdmissionError(
                        "normalized row stream is not in the admitted core set"
                    )
                if provenance.get("source_manifest_sha256") != raw_manifest_sha256:
                    raise ChronicleExternalAdmissionError(
                        "normalized row raw-manifest binding mismatch"
                    )
                if provenance.get("source_object_sha256") != stream_hashes[stream_type]:
                    raise ChronicleExternalAdmissionError(
                        "normalized row raw-object binding mismatch"
                    )
                if provenance.get("raw_object_copied") is not False:
                    raise ChronicleExternalAdmissionError(
                        "normalized row claims a copied raw object"
                    )
                if provenance.get("csv_line") != line_number:
                    raise ChronicleExternalAdmissionError(
                        "normalized row line tie-break mismatch"
                    )
                official = _mapping(record.get("official"), label="row.official")
                if official.get("stream_type") != stream_type:
                    raise ChronicleExternalAdmissionError(
                        "official/provenance stream type mismatch"
                    )
                message = _mapping(official.get("message"), label="official.message")
                meta = _mapping(message.get("meta"), label="official.message.meta")
                if meta.get("event_index") != event_index or meta.get("offset_ms") != offset_ms:
                    raise ChronicleExternalAdmissionError(
                        "normalized row EventMeta projection mismatch"
                    )
                expected_type = STREAM_TO_NORMALIZED_TYPE[stream_type]
                if record.get("type") != expected_type:
                    raise ChronicleExternalAdmissionError(
                        "normalized event type/stream mapping mismatch"
                    )
                encounter_ordinal = _integer(
                    record.get("encounter_ordinal"), label="row.encounter_ordinal"
                )
                frame_message_index = _integer(
                    provenance.get("frame_message_index"),
                    label="row provenance frame_message_index",
                )
                frame_index = _integer(
                    provenance.get("frame_index"), label="row provenance frame_index"
                )
                if frame_index < 0 or frame_message_index < 0:
                    raise ChronicleExternalAdmissionError(
                        "normalized row has a negative frame locator"
                    )
                order = (
                    encounter_ordinal,
                    timestamp_ms,
                    event_index,
                    STREAM_ORDER[stream_type],
                    frame_message_index,
                )
                if previous_order is not None and order <= previous_order:
                    raise ChronicleExternalAdmissionError(
                        "normalized partition event ordering is not strict"
                    )
                previous_order = order
                prior_encounter_row = encounter_rows.get(encounter)
                encounter_row = (encounter_ordinal, first_timestamp_ms)
                if prior_encounter_row is None:
                    encounter_rows[encounter] = encounter_row
                elif prior_encounter_row != encounter_row:
                    raise ChronicleExternalAdmissionError(
                        "normalized encounter ordinal/origin is inconsistent"
                    )

                if record_count >= len(envelopes):
                    raise ChronicleExternalAdmissionError(
                        "normalized partition contains a row absent from raw core streams"
                    )
                envelope = envelopes[record_count]
                expected_record = _normalized_record(
                    envelope,
                    instance_id=instance_id,
                    source_manifest_sha256=raw_manifest_sha256,
                    source_object_sha256=stream_hashes[envelope.stream_type],
                    encounter_ordinal=encounter_order[envelope.encounter_id],
                    output_line=line_number,
                )
                if raw_line != _canonical_bytes(expected_record) + b"\n":
                    raise ChronicleExternalAdmissionError(
                        f"normalized row {line_number} is not the exact canonical raw-derived projection"
                    )
                source_guid_raw = record.get("source_guid")
                target_guid_raw = record.get("target_guid")
                source_guid = (
                    canonical_guid(source_guid_raw, label="row source GUID")
                    if source_guid_raw is not None
                    else None
                )
                target_guid = (
                    canonical_guid(target_guid_raw, label="row target GUID")
                    if target_guid_raw is not None
                    else None
                )
                source_observation = player_event_observations.get(source_guid or "")
                target_observation = player_event_observations.get(target_guid or "")
                if source_observation is not None:
                    source_observation["source_event_count"] += 1
                    source_observation["source_or_target_event_count"] += 1
                    source_observation["encounter_ids"].add(encounter)
                    source_observation["event_type_counts"][str(record["type"])] += 1
                    source_observation["stream_type_counts"][stream_type] += 1
                if target_observation is not None:
                    target_observation["target_event_count"] += 1
                    if target_guid != source_guid:
                        target_observation["source_or_target_event_count"] += 1
                        target_observation["encounter_ids"].add(encounter)
                        target_observation["event_type_counts"][
                            str(record["type"])
                        ] += 1
                        target_observation["stream_type_counts"][stream_type] += 1
                if stream_type == "unit_classification":
                    owner = message.get("owner")
                    if owner is not None:
                        canonicalize_optional_0x_owner(owner)
                        owner_count += 1
                event_counts[str(record["type"])] += 1
                unknown_count += _count_unknown_fields(message)
                record_count += 1
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ChronicleExternalAdmissionError(
            f"cannot stream normalized partition {path}: {error}"
        ) from error

    logical_sha = logical_digest.hexdigest()
    if logical_sha != _sha(
        partition.get("logical_content_sha256"), label="partition logical SHA"
    ):
        raise ChronicleExternalAdmissionError(
            "normalized partition logical SHA verification failed"
        )
    if record_count != _integer(partition.get("record_count"), label="partition record_count"):
        raise ChronicleExternalAdmissionError("partition record count mismatch")
    if record_count != len(envelopes):
        raise ChronicleExternalAdmissionError(
            "normalized partition omits raw core-stream messages"
        )
    if encounters != metadata_encounter_ids:
        raise ChronicleExternalAdmissionError(
            "normalized encounter set and metadata encounter set differ"
        )
    if {
        encounter_id: value[0] for encounter_id, value in encounter_rows.items()
    } != encounter_order:
        raise ChronicleExternalAdmissionError(
            "normalized encounter ordinal mapping disagrees with raw decode"
        )
    if len(encounters) != _integer(
        partition.get("encounter_count"), label="partition encounter_count"
    ):
        raise ChronicleExternalAdmissionError("partition encounter count mismatch")
    declared_counts = _mapping(
        partition.get("event_type_counts"), label="partition.event_type_counts"
    )
    if dict(declared_counts) != dict(sorted(event_counts.items())):
        raise ChronicleExternalAdmissionError("partition event type counts mismatch")
    if unknown_count != _integer(
        partition.get("unknown_field_count"), label="partition unknown_field_count"
    ):
        raise ChronicleExternalAdmissionError("partition unknown field count mismatch")

    verified_player_event_observations: dict[str, dict[str, Any]] = {}
    for guid, raw_observation in sorted(player_event_observations.items()):
        observation_encounters = sorted(raw_observation["encounter_ids"])
        verified_player_event_observations[guid] = {
            "source_event_count": raw_observation["source_event_count"],
            "target_event_count": raw_observation["target_event_count"],
            "source_or_target_event_count": raw_observation[
                "source_or_target_event_count"
            ],
            "encounter_count": len(observation_encounters),
            "encounter_ids_sha256": _sha256(
                _canonical_bytes(observation_encounters)
            ),
            "event_type_counts": dict(
                sorted(raw_observation["event_type_counts"].items())
            ),
            "stream_type_counts": dict(
                sorted(raw_observation["stream_type_counts"].items())
            ),
            "evidence_source": (
                "byte-exact normalized rows rederived from verified raw core streams"
            ),
        }

    return path, {
        "compressed_size_bytes": actual_size,
        "compressed_file_sha256": expected_compressed_sha,
        "logical_content_sha256": logical_sha,
        "record_count": record_count,
        "encounter_count": len(encounters),
        "event_type_counts": dict(sorted(event_counts.items())),
        "unknown_field_count": unknown_count,
        "unit_classification_owner_count": owner_count,
        "all_rows_exactly_rederived_from_verified_raw_objects": True,
        "raw_core_stream_message_count": len(envelopes),
        "normalized_manifest_frame_evidence_matches_raw_decode": True,
        "eventmeta_projection_and_order_verified": True,
    }, verified_player_event_observations


def _normalization_partitions(
    normalization: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    partitions: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(
        _array(normalization.get("partitions"), label="normalization.partitions")
    ):
        partition = _mapping(raw, label=f"normalization.partitions[{index}]")
        instance_id = _text(partition.get("instance_id"), label="partition.instance_id")
        if instance_id in partitions:
            raise ChronicleExternalAdmissionError(
                f"duplicate normalization partition for {instance_id}"
            )
        partitions[instance_id] = partition
    return partitions


def _required_instance_object_cache(
    raw_instance: Mapping[str, Any], raw_root: Path
) -> dict[tuple[str, str, int], bytes]:
    """Load only objects required by one admission worker.

    The parent has already verified the complete raw-manifest object closure.
    Workers recheck their own metadata, combatant-info, and seven core-stream
    references so no large shared byte cache needs to be serialized to child
    processes on Windows.
    """

    wrappers = [
        _mapping(raw_instance.get("metadata"), label="instance.metadata")
    ]
    streams = _mapping(raw_instance.get("streams"), label="instance.streams")
    wrappers.extend(
        _mapping(streams.get(stream_type), label=f"instance.streams.{stream_type}")
        for stream_type in (*CORE_STREAM_TYPES, "combatant_info")
    )
    cache: dict[tuple[str, str, int], bytes] = {}
    for wrapper in wrappers:
        reference = _mapping(wrapper.get("object"), label="required object")
        relative = _text(reference.get("relative_path"), label="object.relative_path")
        digest = _sha(reference.get("sha256"), label="object.sha256")
        size = _integer(reference.get("size_bytes"), label="object.size_bytes")
        key = (relative, digest, size)
        if key not in cache:
            payload, _ = _read_object_reference(
                raw_root, reference, label="required instance object"
            )
            cache[key] = payload
    return cache


def _admit_instance(
    *,
    instance_id: str,
    raw_instance: Mapping[str, Any],
    partition: Mapping[str, Any],
    normalization_manifest_path: Path,
    raw_manifest_sha256: str,
    cache: Mapping[tuple[str, str, int], bytes],
    data_root: Path,
) -> dict[str, Any]:
    metadata_evidence, metadata, players_by_guid = _metadata_evidence(
        raw_instance, cache=cache
    )
    spec_evidence = _warrior_spec_evidence(
        raw_instance, players_by_guid=players_by_guid
    )
    (
        partition_path,
        partition_verification,
        verified_player_event_observations,
    ) = _verify_partition(
        partition,
        normalization_manifest_path=normalization_manifest_path,
        raw_instance=raw_instance,
        raw_manifest_sha256=raw_manifest_sha256,
        cache=cache,
        metadata=metadata,
        players_by_guid=players_by_guid,
    )
    combatant_evidence = _combatant_info_evidence(
        raw_instance,
        cache=cache,
        metadata=metadata,
        players_by_guid=players_by_guid,
        verified_player_event_observations=verified_player_event_observations,
    )
    streams = _mapping(raw_instance.get("streams"), label="raw streams")
    core_objects = {
        stream_type: deepcopy(
            dict(
                _mapping(
                    _mapping(streams.get(stream_type), label=stream_type).get(
                        "object"
                    ),
                    label=f"{stream_type}.object",
                )
            )
        )
        for stream_type in CORE_STREAM_TYPES
    }
    partition_contract = {
        "path": _relative_to_data_root(partition_path, data_root),
        **partition_verification,
    }
    return {
        "status": STATUS,
        "instance_id": instance_id,
        "slug": raw_instance.get("slug"),
        "instance_name": raw_instance.get("instance_name"),
        "source_evidence": {
            "kind": SOURCE_EVIDENCE_KIND,
            "raw_api_manifest_sha256": raw_manifest_sha256,
            "metadata_object": metadata_evidence["metadata_object"],
            "core_stream_objects": core_objects,
            "normalized_partition": partition_contract,
            "raw_object_copy_count": 0,
            "normalized_row_copy_count": 0,
        },
        "temporal_and_guild_provenance": {
            key: metadata_evidence[key]
            for key in (
                "started_at",
                "started_at_source",
                "started_at_source_field",
                "admission_verified_started_at_source",
                "started_at_verified_against_metadata_encounter_minimum",
                "uploaded_at",
                "uploaded_at_source",
                "guild",
                "guild_evidence",
                "contamination",
            )
        },
        "metadata_player_resolver": metadata_evidence["player_resolver"],
        "combatant_info_evidence": combatant_evidence,
        "warrior_spec_evidence": spec_evidence,
        "classification_contract": {
            "metadata_entity_rule": (
                "METADATA_PLAYER_EXACT only after full canonical GUID match"
            ),
            "friend_or_hostile_semantic_rule": (
                "UNKNOWN_NONVOTING_NO_GUESS_IN_V1_ADMISSION"
            ),
            "metadata_roster_membership_does_not_imply_current_affiliation": True,
            "official_unit_type_and_affiliation_preserved_in_normalized_row": True,
            "owner_input_forms": ["0xHEX", "HEX"],
            "owner_canonical_form": (
                "uppercase HEX without optional 0x; leading zeroes retained"
            ),
            "owner_canonicalization_is_attribution": False,
            "owner_exact_player_resolution_requires_full_canonical_guid_match": True,
        },
        "consumer_status": {
            "versioned_reconstruction_input": True,
            "legacy_plain_jsonl_reconstruction_input": False,
            "legacy_manual_export_queue_entry": False,
            "legacy_raw_csv_provenance": False,
            "frozen_50_capsule_member": False,
            "comparison_authorized": False,
        },
    }


def _admit_instance_worker(
    arguments: tuple[
        str,
        dict[str, Any],
        dict[str, Any],
        Path,
        str,
        Path,
        Path,
    ]
) -> tuple[str, dict[str, Any]]:
    (
        instance_id,
        raw_instance,
        partition,
        normalization_manifest_path,
        raw_manifest_sha256,
        raw_root,
        data_root,
    ) = arguments
    try:
        cache = _required_instance_object_cache(raw_instance, raw_root)
        admitted = _admit_instance(
            instance_id=instance_id,
            raw_instance=raw_instance,
            partition=partition,
            normalization_manifest_path=normalization_manifest_path,
            raw_manifest_sha256=raw_manifest_sha256,
            cache=cache,
            data_root=data_root,
        )
    except ChronicleExternalAdmissionError as error:
        raise ChronicleExternalAdmissionError(
            f"instance {instance_id}: {error}"
        ) from error
    return instance_id, admitted


def _atomic_temporary(path: Path, payload: bytes) -> Path:
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
            raise ChronicleExternalAdmissionError(
                f"immutable admission manifest differs: {final}"
            )
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(final)


def build_external_reconstruction_admission(
    *,
    raw_manifest_path: str | Path,
    normalization_manifest_path: str | Path,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
) -> dict[str, Any]:
    """Verify and publish an External API evidence admission manifest."""

    if isinstance(workers, bool) or not isinstance(workers, int):
        raise ChronicleExternalAdmissionError("workers must be an integer")
    if workers < 1 or workers > 64:
        raise ChronicleExternalAdmissionError("workers must be between 1 and 64")

    data_root_path = Path(data_root).expanduser().resolve()
    if data_root_path.name.casefold() != "offline_data":
        raise ChronicleExternalAdmissionError(
            "data_root must be the offline_data directory itself"
        )
    raw_path = _under(Path(raw_manifest_path), data_root_path, label="raw manifest")
    normalization_path = _under(
        Path(normalization_manifest_path), data_root_path, label="normalization manifest"
    )
    output_dir = _under(Path(output_directory), data_root_path, label="output directory")
    raw_root = data_root_path / "chronicle_raw" / "external_api" / "v1"
    normalization_root = (
        data_root_path / "derived" / "chronicle_external_core_events" / "v1"
    )
    if raw_path.parent != (raw_root / "manifests").resolve():
        raise ChronicleExternalAdmissionError(
            "raw manifest must be in chronicle_raw/external_api/v1/manifests"
        )
    try:
        normalization_path.relative_to(normalization_root.resolve())
    except ValueError as error:
        raise ChronicleExternalAdmissionError(
            "normalization manifest must remain below "
            "derived/chronicle_external_core_events/v1"
        ) from error
    admission_root = (
        data_root_path
        / "derived"
        / "chronicle_external_reconstruction_admission"
        / "v1"
    ).resolve()
    try:
        output_dir.relative_to(admission_root)
    except ValueError as error:
        raise ChronicleExternalAdmissionError(
            "output directory must remain below the dedicated admission v1 root"
        ) from error

    raw, raw_bytes, raw_sha = _load_raw_manifest(raw_path)
    normalization, normalization_bytes, normalization_file_sha, normalization_content_sha = (
        _load_normalization_manifest(normalization_path)
    )
    normalization_source = _mapping(
        normalization.get("source"), label="normalization.source"
    )
    if (
        normalization.get("kind")
        != "chronicle_external_core_event_normalization_manifest"
        or normalization.get("implementation_revision")
        != NORMALIZER_IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalAdmissionError(
            "normalization manifest kind/implementation revision is not the locked v1 contract"
        )
    if normalization_source.get("manifest_sha256") != raw_sha:
        raise ChronicleExternalAdmissionError(
            "normalization manifest is not bound to the selected raw API manifest"
        )
    if (
        normalization_source.get("manifest_size_bytes") != len(raw_bytes)
        or normalization_source.get("manifest_schema") != RAW_MANIFEST_SCHEMA
        or normalization_source.get("parser_contract_revision")
        != raw.get("parser_contract_revision")
    ):
        raise ChronicleExternalAdmissionError(
            "normalization source descriptor disagrees with the selected raw manifest"
        )
    normalization_contract = _mapping(
        normalization.get("normalization_contract"),
        label="normalization.normalization_contract",
    )
    if normalization_contract.get("core_stream_types") != list(CORE_STREAM_TYPES):
        raise ChronicleExternalAdmissionError(
            "normalization core stream contract is not the exact fixed v1 set"
        )
    if (
        normalization_contract.get("raw_objects_copied") != 0
        or normalization_contract.get("manifest_committed_last") is not True
        or normalization_contract.get("slain_attribution_value_policy")
        != "nested_attribution_preserved_but_not_projected_to_value_to_avoid_damage_double_counting"
    ):
        raise ChronicleExternalAdmissionError(
            "normalization scientific/commit contract is not the locked v1 contract"
        )

    cache, verified_raw_bytes = _verify_raw_object_closure(raw, raw_root)
    verified_raw_object_count = len(cache)
    raw_instances: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(_array(raw.get("instances"), label="raw instances")):
        instance = _mapping(value, label=f"raw instances[{index}]")
        instance_id = _text(instance.get("instance_id"), label="raw instance_id")
        if instance_id in raw_instances:
            raise ChronicleExternalAdmissionError("duplicate raw API instance")
        raw_instances[instance_id] = instance
    if not raw_instances:
        raise ChronicleExternalAdmissionError(
            "admission v1 requires at least one raw API instance"
        )
    partitions = _normalization_partitions(normalization)
    if set(raw_instances) != set(partitions):
        raise ChronicleExternalAdmissionError(
            "raw API instances and normalized partitions are not an exact set match"
        )

    ordered_instance_ids = sorted(raw_instances)
    admitted_by_id: dict[str, dict[str, Any]] = {}
    effective_workers = min(workers, len(ordered_instance_ids))
    if effective_workers == 1:
        for instance_id in ordered_instance_ids:
            try:
                admitted_by_id[instance_id] = _admit_instance(
                    instance_id=instance_id,
                    raw_instance=raw_instances[instance_id],
                    partition=partitions[instance_id],
                    normalization_manifest_path=normalization_path,
                    raw_manifest_sha256=raw_sha,
                    cache=cache,
                    data_root=data_root_path,
                )
            except ChronicleExternalAdmissionError as error:
                raise ChronicleExternalAdmissionError(
                    f"instance {instance_id}: {error}"
                ) from error
    else:
        # The complete closure cache may include optional non-core streams.
        # Workers re-open and reverify only their own required objects, so
        # release the parent's byte cache before spawning bounded processes.
        cache.clear()
        arguments = [
            (
                instance_id,
                dict(raw_instances[instance_id]),
                dict(partitions[instance_id]),
                normalization_path,
                raw_sha,
                raw_root,
                data_root_path,
            )
            for instance_id in ordered_instance_ids
        ]
        with ProcessPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(_admit_instance_worker, argument): argument[0]
                for argument in arguments
            }
            try:
                for future in as_completed(futures):
                    instance_id, admitted = future.result()
                    if instance_id != futures[future]:
                        raise ChronicleExternalAdmissionError(
                            "admission worker returned a cross-instance result"
                        )
                    admitted_by_id[instance_id] = admitted
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    admitted_instances = [admitted_by_id[value] for value in ordered_instance_ids]

    expected_normalization_summary = {
        "instance_count": len(admitted_instances),
        "encounter_count": sum(
            item["source_evidence"]["normalized_partition"]["encounter_count"]
            for item in admitted_instances
        ),
        "record_count": sum(
            item["source_evidence"]["normalized_partition"]["record_count"]
            for item in admitted_instances
        ),
        "unknown_field_count": sum(
            item["source_evidence"]["normalized_partition"]["unknown_field_count"]
            for item in admitted_instances
        ),
        "network_request_count": 0,
        "raw_object_copy_count": 0,
    }
    normalization_summary = _mapping(
        normalization.get("summary"), label="normalization.summary"
    )
    if dict(normalization_summary) != expected_normalization_summary:
        raise ChronicleExternalAdmissionError(
            "normalization summary does not exactly recompute from admitted partitions"
        )

    manifest_core: dict[str, Any] = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "kind": "chronicle_external_reconstruction_admission_manifest",
        "publication_contract": {
            "content_addressed_manifest_published_before_stable_pointer": True,
            "stable_manifest_committed_last": True,
            "stable_and_addressed_bytes_must_match_on_load": True,
            "addressed_manifest_is_immutable": True,
        },
        "inputs": {
            "raw_api_manifest": {
                "path": _relative_to_data_root(raw_path, data_root_path),
                "file_sha256": raw_sha,
                "size_bytes": len(raw_bytes),
                "schema": RAW_MANIFEST_SCHEMA,
                "verified_object_count": verified_raw_object_count,
                "verified_object_bytes": verified_raw_bytes,
            },
            "normalization_manifest": {
                "path": _relative_to_data_root(normalization_path, data_root_path),
                "file_sha256": normalization_file_sha,
                "size_bytes": len(normalization_bytes),
                "content_sha256": normalization_content_sha,
                "schema": NORMALIZATION_SCHEMA,
                "implementation_revision": NORMALIZER_IMPLEMENTATION_REVISION,
            },
        },
        "admission_validation_contract": {
            "raw_object_closure_hash_and_size_verified": True,
            "raw_core_streams_redecoded": True,
            "normalized_rows_byte_exact_canonical_rederived": True,
            "raw_message_coverage_exact_no_duplicate_or_omission": True,
            "metadata_encounter_set_exact": True,
            "normalization_summary_recomputed": True,
            "combatant_exact_guid_is_only_attribution_key": True,
            "combatant_info_present_hero_class_stable_and_metadata_matched": True,
            "combatant_info_present_metadata_race_observed_as_integrity_check": True,
            "combatant_display_race_transition_is_diagnostic_only": True,
            "missing_combatant_info_metadata_player_requires_verified_core_event_guid_evidence": True,
            "missing_combatant_info_static_fields_not_imputed": True,
            "slain_attribution_projected_to_value": False,
        },
        "streaming_adapter_contract": {
            "entrypoint": "iter_versioned_reconstruction_rows",
            "container": "gzip JSON Lines",
            "pre_yield_verification": [
                "compressed_size_and_sha256",
                "decompressed_logical_sha256",
                "record_count",
            ],
            "same_open_compressed_file_handle_retained_through_iteration": True,
            "content_addressed_partition_requires_no_in_place_mutation": True,
            "uncompressed_partition_loaded_whole": False,
            "row_fields_are_not_rewritten": True,
            "structured_evidence_field_added": "external_admission",
            "exact_player_resolution_only": True,
            "hostile_classification_inference": False,
            "owner_optional_0x_normalization": True,
        },
        "scientific_boundaries": {
            "legacy_manual_csv_contract_modified": False,
            "manual_export_queue_impersonated": False,
            "raw_or_normalized_rows_copied": False,
            "frozen_50_capsule_membership_claimed": False,
            "comparison_or_policy_promotion_authorized": False,
            "next_required_consumer": "versioned external-api-aware reconstruction and capsule source-evidence union",
        },
        "summary": {
            "instance_count": len(admitted_instances),
            "record_count": sum(
                instance["source_evidence"]["normalized_partition"]["record_count"]
                for instance in admitted_instances
            ),
            "metadata_player_count": sum(
                instance["metadata_player_resolver"]["player_count"]
                for instance in admitted_instances
            ),
            "warrior_observation_count": sum(
                instance["warrior_spec_evidence"]["observation_count"]
                for instance in admitted_instances
            ),
            "display_name_transition_player_count": sum(
                instance["combatant_info_evidence"][
                    "display_name_transition_player_count"
                ]
                for instance in admitted_instances
            ),
            "display_race_transition_player_count": sum(
                instance["combatant_info_evidence"][
                    "display_race_transition_player_count"
                ]
                for instance in admitted_instances
            ),
            "display_guild_transition_player_count": sum(
                instance["combatant_info_evidence"][
                    "display_guild_transition_player_count"
                ]
                for instance in admitted_instances
            ),
            "missing_metadata_combatant_info_player_count": sum(
                instance["combatant_info_evidence"][
                    "missing_metadata_combatant_info_player_count"
                ]
                for instance in admitted_instances
            ),
            "raw_object_copy_count": 0,
            "normalized_row_copy_count": 0,
            "network_request_count": 0,
        },
        "instances": admitted_instances,
    }
    content_sha = _sha256(_canonical_bytes(manifest_core))
    manifest = {
        **manifest_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical_JSON_excluding_content_address",
            "sha256": content_sha,
        },
    }
    payload = _canonical_bytes(manifest) + b"\n"
    file_sha = _sha256(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    addressed_path = output_dir / (
        f"chronicle_external_reconstruction_admission_v1.{content_sha}.manifest.json"
    )
    stable_path = output_dir / "manifest.json"
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        addressed_temporary = _atomic_temporary(addressed_path, payload)
        stable_temporary = _atomic_temporary(stable_path, payload)
        _publish_immutable(addressed_temporary, addressed_path, file_sha)
        addressed_temporary = None
        # Stable pointer is the only mutable commit mark and is published last.
        stable_temporary.replace(stable_path)
        stable_temporary = None
    finally:
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)

    return {
        "status": STATUS,
        "schema": SCHEMA,
        "content_sha256": content_sha,
        "manifest_file_sha256": file_sha,
        "manifest_path": str(stable_path),
        "content_addressed_manifest_path": str(addressed_path),
        "instance_count": manifest_core["summary"]["instance_count"],
        "record_count": manifest_core["summary"]["record_count"],
        "comparison_authorized": False,
        "frozen_50_capsule_member": False,
        "network_request_count": 0,
    }


def load_admission_manifest(path: str | Path) -> tuple[dict[str, Any], Path]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"cannot read admission manifest {resolved}: {error}"
        ) from error
    document = _json_object(payload, label="admission manifest")
    if (
        document.get("schema") != SCHEMA
        or document.get("status") != STATUS
        or document.get("kind")
        != "chronicle_external_reconstruction_admission_manifest"
        or document.get("implementation_revision") != IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalAdmissionError("unsupported admission manifest")
    validation = _mapping(
        document.get("admission_validation_contract"),
        label="admission_validation_contract",
    )
    if (
        validation.get("normalized_rows_byte_exact_canonical_rederived") is not True
        or validation.get("combatant_exact_guid_is_only_attribution_key") is not True
        or validation.get(
            "combatant_info_present_hero_class_stable_and_metadata_matched"
        )
        is not True
        or validation.get(
            "combatant_info_present_metadata_race_observed_as_integrity_check"
        )
        is not True
        or validation.get("combatant_display_race_transition_is_diagnostic_only")
        is not True
        or validation.get(
            "missing_combatant_info_metadata_player_requires_verified_core_event_guid_evidence"
        )
        is not True
        or validation.get("missing_combatant_info_static_fields_not_imputed")
        is not True
    ):
        raise ChronicleExternalAdmissionError(
            "admission manifest lacks exact raw re-derivation or combatant "
            "identity evidence"
        )
    instances = _array(document.get("instances"), label="admission.instances")
    if not instances:
        raise ChronicleExternalAdmissionError(
            "admission v1 consumer requires at least one instance"
        )
    instance_ids: list[str] = []
    record_count = 0
    metadata_player_count = 0
    warrior_observation_count = 0
    display_name_transition_player_count = 0
    display_race_transition_player_count = 0
    display_guild_transition_player_count = 0
    missing_metadata_combatant_info_player_count = 0
    partition_paths: set[str] = set()
    for index, raw_instance in enumerate(instances):
        admission_instance = _mapping(
            raw_instance, label=f"admission.instances[{index}]"
        )
        if admission_instance.get("status") != STATUS:
            raise ChronicleExternalAdmissionError(
                "admission instance has an unsupported status"
            )
        instance_id = _text(
            admission_instance.get("instance_id"),
            label=f"admission.instances[{index}].instance_id",
        )
        if instance_id in instance_ids:
            raise ChronicleExternalAdmissionError(
                "admission manifest contains duplicate instance ids"
            )
        instance_ids.append(instance_id)
        source_evidence = _mapping(
            admission_instance.get("source_evidence"),
            label=f"admission.instances[{index}].source_evidence",
        )
        if source_evidence.get("kind") != SOURCE_EVIDENCE_KIND:
            raise ChronicleExternalAdmissionError(
                "admission instance source evidence kind is unsupported"
            )
        inputs = _mapping(document.get("inputs"), label="admission.inputs")
        raw_input = _mapping(
            inputs.get("raw_api_manifest"), label="inputs.raw_api_manifest"
        )
        if source_evidence.get("raw_api_manifest_sha256") != raw_input.get(
            "file_sha256"
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance raw-manifest binding mismatch"
            )
        admitted_partition = _mapping(
            source_evidence.get("normalized_partition"),
            label=f"admission.instances[{index}].normalized_partition",
        )
        if admitted_partition.get(
            "all_rows_exactly_rederived_from_verified_raw_objects"
        ) is not True:
            raise ChronicleExternalAdmissionError(
                "admission instance lacks exact raw-derived row evidence"
            )
        partition_path = _text(
            admitted_partition.get("path"),
            label=f"admission.instances[{index}].normalized_partition.path",
        )
        partition_key = partition_path.casefold()
        if partition_key in partition_paths:
            raise ChronicleExternalAdmissionError(
                "multiple admission instances reference the same normalized partition"
            )
        partition_paths.add(partition_key)
        partition_record_count = _integer(
            admitted_partition.get("record_count"),
            label=f"admission.instances[{index}].record_count",
        )
        if partition_record_count < 0:
            raise ChronicleExternalAdmissionError(
                "admission instance record count is negative"
            )
        record_count += partition_record_count
        resolver = _mapping(
            admission_instance.get("metadata_player_resolver"),
            label=f"admission.instances[{index}].metadata_player_resolver",
        )
        instance_metadata_player_count = _integer(
            resolver.get("player_count"),
            label=f"admission.instances[{index}].player_count",
        )
        resolver_players = _array(
            resolver.get("players"),
            label=f"admission.instances[{index}].metadata_player_resolver.players",
        )
        if (
            len(resolver_players) != instance_metadata_player_count
            or resolver.get("players_sha256")
            != _sha256(_canonical_bytes(resolver_players))
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance metadata player resolver identity mismatch"
            )
        resolver_by_guid: dict[str, Mapping[str, Any]] = {}
        resolver_guid_order: list[str] = []
        for player_index, raw_player in enumerate(resolver_players):
            player = _mapping(
                raw_player,
                label=(
                    f"admission.instances[{index}].metadata_player_resolver."
                    f"players[{player_index}]"
                ),
            )
            guid = canonical_guid(player.get("guid"), label="resolved metadata GUID")
            if guid in resolver_by_guid:
                raise ChronicleExternalAdmissionError(
                    "admission instance metadata player resolver has duplicate GUIDs"
                )
            resolver_by_guid[guid] = player
            resolver_guid_order.append(guid)
        if resolver_guid_order != sorted(resolver_guid_order):
            raise ChronicleExternalAdmissionError(
                "admission instance metadata player resolver order is not deterministic"
            )
        metadata_player_count += instance_metadata_player_count
        combatant = _mapping(
            admission_instance.get("combatant_info_evidence"),
            label=f"admission.instances[{index}].combatant_info_evidence",
        )
        identity_contract = _mapping(
            combatant.get("identity_resolution_contract"),
            label=f"admission.instances[{index}].identity_resolution_contract",
        )
        if (
            identity_contract.get("attribution_key")
            != "metadata exact canonical player GUID"
            or identity_contract.get(
                "combatant_info_present_metadata_name_must_appear"
            )
            is not True
            or identity_contract.get(
                "combatant_info_present_metadata_hero_class_must_appear"
            )
            is not True
            or identity_contract.get(
                "combatant_info_present_hero_class_must_be_stable"
            )
            is not True
            or identity_contract.get(
                "combatant_info_present_metadata_race_must_appear"
            )
            is not True
            or identity_contract.get(
                "metadata_race_appearance_is_integrity_check_not_attribution"
            )
            is not True
            or identity_contract.get("display_name_used_for_identity_or_attribution")
            is not False
            or identity_contract.get("display_race_used_for_identity_or_attribution")
            is not False
            or identity_contract.get("display_guild_used_for_identity_or_attribution")
            is not False
            or identity_contract.get(
                "display_name_race_or_guild_transitions_are_diagnostic_only"
            )
            is not True
            or identity_contract.get(
                "missing_metadata_player_requires_verified_core_event_guid_evidence"
            )
            is not True
            or identity_contract.get(
                "missing_player_combatant_info_static_fields_imputed"
            )
            is not False
            or identity_contract.get(
                "missing_player_talents_gear_or_spec_inferred"
            )
            is not False
            or combatant.get("identity_conflict_count") != 0
            or combatant.get("hero_class_transition_player_count") != 0
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance combatant identity contract is unsafe"
            )
        missing_count = _integer(
            combatant.get("missing_metadata_combatant_info_player_count"),
            label=(
                f"admission.instances[{index}]."
                "missing_metadata_combatant_info_player_count"
            ),
        )
        missing_rows = _array(
            combatant.get("missing_metadata_combatant_info_players"),
            label=(
                f"admission.instances[{index}]."
                "missing_metadata_combatant_info_players"
            ),
        )
        if (
            missing_count < 0
            or missing_count != len(missing_rows)
            or missing_count > instance_metadata_player_count
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance missing CombatantInfo player count mismatch"
            )
        missing_guids: list[str] = []
        for missing_index, raw_missing in enumerate(missing_rows):
            missing = _mapping(
                raw_missing,
                label=(
                    f"admission.instances[{index}]."
                    f"missing_metadata_combatant_info_players[{missing_index}]"
                ),
            )
            guid = canonical_guid(
                missing.get("guid"), label="missing CombatantInfo metadata GUID"
            )
            if guid in missing_guids or guid not in resolver_by_guid:
                raise ChronicleExternalAdmissionError(
                    "missing CombatantInfo diagnostic has an invalid metadata GUID"
                )
            player_metadata = _mapping(
                resolver_by_guid[guid].get("metadata"),
                label="missing CombatantInfo resolved metadata",
            )
            static_context = _mapping(
                missing.get("combatant_info_static_context"),
                label="missing CombatantInfo static context",
            )
            expected_static_context = {
                "display_name": "UNAVAILABLE_NOT_IMPUTED",
                "hero_class": "UNAVAILABLE_NOT_IMPUTED",
                "display_race": "UNAVAILABLE_NOT_IMPUTED",
                "display_guild": "UNAVAILABLE_NOT_IMPUTED",
                "gear": "UNAVAILABLE_NOT_IMPUTED",
                "talents": "UNAVAILABLE_NOT_IMPUTED",
                "spec": "UNKNOWN_NOT_INFERRED_FROM_MISSING_COMBATANT_INFO",
            }
            observation = _mapping(
                missing.get("verified_core_event_evidence"),
                label="missing CombatantInfo verified core-event evidence",
            )
            source_count = _integer(
                observation.get("source_event_count"), label="source_event_count"
            )
            target_count = _integer(
                observation.get("target_event_count"), label="target_event_count"
            )
            union_count = _integer(
                observation.get("source_or_target_event_count"),
                label="source_or_target_event_count",
            )
            encounter_count = _integer(
                observation.get("encounter_count"), label="encounter_count"
            )
            event_type_counts = _mapping(
                observation.get("event_type_counts"), label="event_type_counts"
            )
            stream_type_counts = _mapping(
                observation.get("stream_type_counts"), label="stream_type_counts"
            )
            event_type_count_sum = 0
            for event_type, raw_count in event_type_counts.items():
                if event_type not in set(STREAM_TO_NORMALIZED_TYPE.values()):
                    raise ChronicleExternalAdmissionError(
                        "missing CombatantInfo evidence has an unsupported event type"
                    )
                count = _integer(
                    raw_count,
                    label=f"missing CombatantInfo {event_type} event count",
                )
                if count < 0:
                    raise ChronicleExternalAdmissionError(
                        "missing CombatantInfo evidence has a negative event count"
                    )
                event_type_count_sum += count
            stream_type_count_sum = 0
            for stream_type, raw_count in stream_type_counts.items():
                if stream_type not in CORE_STREAM_TYPES:
                    raise ChronicleExternalAdmissionError(
                        "missing CombatantInfo evidence has an unsupported stream type"
                    )
                count = _integer(
                    raw_count,
                    label=f"missing CombatantInfo {stream_type} stream count",
                )
                if count < 0:
                    raise ChronicleExternalAdmissionError(
                        "missing CombatantInfo evidence has a negative stream count"
                    )
                stream_type_count_sum += count
            if (
                missing.get("metadata_name") != player_metadata.get("name")
                or missing.get("metadata_hero_class") != player_metadata.get("class")
                or missing.get("metadata_race") != player_metadata.get("race")
                or missing.get("combatant_info_status")
                != "MISSING_FROM_ALL_METADATA_ENCOUNTER_FRAMES"
                or missing.get("combatant_info_frame_count_searched")
                != combatant.get("frame_count")
                or missing.get("identity_resolution")
                != "EXACT_METADATA_GUID_WITH_VERIFIED_CORE_EVENT_PRESENCE"
                or dict(static_context) != expected_static_context
                or source_count < 0
                or target_count < 0
                or union_count <= 0
                or union_count < max(source_count, target_count)
                or union_count > source_count + target_count
                or encounter_count <= 0
                or encounter_count > union_count
                or event_type_count_sum != union_count
                or stream_type_count_sum != union_count
                or observation.get("evidence_source")
                != "byte-exact normalized rows rederived from verified raw core streams"
            ):
                raise ChronicleExternalAdmissionError(
                    "missing CombatantInfo diagnostic evidence is unsafe"
                )
            _sha(
                observation.get("encounter_ids_sha256"),
                label="missing CombatantInfo encounter ids SHA-256",
            )
            missing_guids.append(guid)
        extra_count = _integer(
            combatant.get("nonmetadata_combatant_guid_count", 0),
            label="nonmetadata_combatant_guid_count",
        )
        if extra_count < 0:
            raise ChronicleExternalAdmissionError(
                "admission instance has a negative nonmetadata combatant count"
            )
        if extra_count:
            _sha(
                combatant.get("nonmetadata_combatant_guids_sha256"),
                label="nonmetadata combatant GUIDs SHA-256",
            )
            if combatant.get("nonmetadata_combatants_added_to_player_resolver") != 0:
                raise ChronicleExternalAdmissionError(
                    "nonmetadata combatant was added to the metadata resolver"
                )
        present_metadata_count = instance_metadata_player_count - missing_count
        if (
            missing_guids != sorted(missing_guids)
            or combatant.get("missing_metadata_combatant_info_guids_sha256")
            != _sha256(_canonical_bytes(missing_guids))
            or combatant.get("metadata_player_guid_set_equal")
            is not (missing_count == 0 and extra_count == 0)
            or combatant.get("metadata_player_guid_subset_of_combatant_info")
            is not (missing_count == 0)
            or combatant.get("combatant_info_guid_subset_of_metadata_players")
            is not (extra_count == 0)
            or combatant.get("unique_player_guid_count")
            != present_metadata_count + extra_count
            or combatant.get("metadata_name_observed_player_count")
            != present_metadata_count
            or combatant.get("metadata_hero_class_observed_player_count")
            != present_metadata_count
            or combatant.get("metadata_race_observed_player_count")
            != present_metadata_count
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance CombatantInfo coverage accounting is unsafe"
            )
        missing_metadata_combatant_info_player_count += missing_count
        diagnostics = _mapping(
            combatant.get("display_transition_diagnostics"),
            label=f"admission.instances[{index}].display_transition_diagnostics",
        )
        transition_counts: dict[str, int] = {}
        for field, count_key in (
            ("name", "display_name_transition_player_count"),
            ("race", "display_race_transition_player_count"),
            ("guild_name", "display_guild_transition_player_count"),
        ):
            count = _integer(
                combatant.get(count_key),
                label=f"admission.instances[{index}].{count_key}",
            )
            rows = _array(
                diagnostics.get(field),
                label=(
                    f"admission.instances[{index}]."
                    f"display_transition_diagnostics.{field}"
                ),
            )
            if count != len(rows):
                raise ChronicleExternalAdmissionError(
                    "admission instance display-transition evidence count mismatch"
                )
            transition_counts[field] = count
        display_name_transition_player_count += transition_counts["name"]
        display_race_transition_player_count += transition_counts["race"]
        display_guild_transition_player_count += transition_counts["guild_name"]
        warrior = _mapping(
            admission_instance.get("warrior_spec_evidence"),
            label=f"admission.instances[{index}].warrior_spec_evidence",
        )
        warrior_observation_count += _integer(
            warrior.get("observation_count"),
            label=f"admission.instances[{index}].warrior_observation_count",
        )
        temporal = _mapping(
            admission_instance.get("temporal_and_guild_provenance"),
            label=f"admission.instances[{index}].temporal_and_guild_provenance",
        )
        started_at = _text(
            temporal.get("started_at"),
            label=f"admission.instances[{index}].started_at",
        )
        _parse_rfc3339(started_at, label=f"admission.instances[{index}].started_at")
        contamination = _mapping(
            temporal.get("contamination"),
            label=f"admission.instances[{index}].contamination",
        )
        expected_label = classify_range_bug(
            contamination.get("guild_context"), started_at
        )
        if (
            contamination.get("label") != expected_label
            or contamination.get("guild_evidence")
            != temporal.get("guild_evidence")
            or contamination.get("time_field") != "started_at"
            or contamination.get("uploaded_at_used") is not False
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance contamination provenance fails the frozen rule"
            )
        consumer = _mapping(
            admission_instance.get("consumer_status"),
            label=f"admission.instances[{index}].consumer_status",
        )
        if (
            consumer.get("versioned_reconstruction_input") is not True
            or consumer.get("legacy_plain_jsonl_reconstruction_input") is not False
            or consumer.get("legacy_manual_export_queue_entry") is not False
            or consumer.get("legacy_raw_csv_provenance") is not False
            or consumer.get("frozen_50_capsule_member") is not False
            or consumer.get("comparison_authorized") is not False
        ):
            raise ChronicleExternalAdmissionError(
                "admission instance consumer boundary is unsafe"
            )
    if instance_ids != sorted(instance_ids):
        raise ChronicleExternalAdmissionError(
            "admission instances are not in deterministic instance-id order"
        )
    summary = _mapping(document.get("summary"), label="admission.summary")
    expected_summary = {
        "instance_count": len(instances),
        "record_count": record_count,
        "metadata_player_count": metadata_player_count,
        "warrior_observation_count": warrior_observation_count,
        "display_name_transition_player_count": (
            display_name_transition_player_count
        ),
        "display_race_transition_player_count": (
            display_race_transition_player_count
        ),
        "display_guild_transition_player_count": (
            display_guild_transition_player_count
        ),
        "missing_metadata_combatant_info_player_count": (
            missing_metadata_combatant_info_player_count
        ),
        "raw_object_copy_count": 0,
        "normalized_row_copy_count": 0,
        "network_request_count": 0,
    }
    if dict(summary) != expected_summary:
        raise ChronicleExternalAdmissionError(
            "admission summary does not exactly recompute from instances"
        )
    content_sha = _verify_content_address(document, label="admission manifest")
    addressed_name = (
        f"chronicle_external_reconstruction_admission_v1.{content_sha}.manifest.json"
    )
    if resolved.name not in {"manifest.json", addressed_name}:
        raise ChronicleExternalAdmissionError(
            "admission manifest filename/content address mismatch"
        )
    stable = resolved.with_name("manifest.json")
    addressed = resolved.with_name(addressed_name)
    try:
        if stable.read_bytes() != payload or addressed.read_bytes() != payload:
            raise ChronicleExternalAdmissionError(
                "stable admission manifest and addressed sibling differ"
            )
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"stable or addressed admission manifest is absent: {error}"
        ) from error
    return document, resolved


def _find_data_root(path: Path) -> Path:
    for candidate in (path.parent, *path.parents):
        if candidate.name.casefold() == "offline_data":
            return candidate.resolve()
    raise ChronicleExternalAdmissionError(
        "admission manifest is not stored below offline_data"
    )


def _resolve_admitted_partition(
    data_root: Path, instance: Mapping[str, Any]
) -> tuple[Path, Mapping[str, Any]]:
    source = _mapping(instance.get("source_evidence"), label="instance.source_evidence")
    if source.get("kind") != SOURCE_EVIDENCE_KIND:
        raise ChronicleExternalAdmissionError("unsupported source evidence kind")
    partition = _mapping(
        source.get("normalized_partition"), label="source_evidence.normalized_partition"
    )
    relative = Path(_text(partition.get("path"), label="normalized partition path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalAdmissionError("admitted partition path escapes data root")
    path = (data_root / relative).resolve()
    try:
        path.relative_to(data_root)
    except ValueError as error:
        raise ChronicleExternalAdmissionError(
            "admitted partition path escapes data root"
        ) from error
    return path, partition


@contextmanager
def _verified_admitted_partition_handle(
    path: Path, partition: Mapping[str, Any]
) -> Iterator[Any]:
    expected_size = _integer(
        partition.get("compressed_size_bytes"), label="admitted compressed size"
    )
    expected_compressed = _sha(
        partition.get("compressed_file_sha256"), label="admitted compressed SHA"
    )
    try:
        compressed_handle = path.open("rb")
    except OSError as error:
        raise ChronicleExternalAdmissionError(
            f"cannot open admitted partition {path}: {error}"
        ) from error
    with compressed_handle:
        compressed_digest = hashlib.sha256()
        compressed_size = 0
        while chunk := compressed_handle.read(1024 * 1024):
            compressed_digest.update(chunk)
            compressed_size += len(chunk)
        if (
            compressed_size != expected_size
            or compressed_digest.hexdigest() != expected_compressed
        ):
            raise ChronicleExternalAdmissionError(
                "admitted partition compressed identity mismatch"
            )
        compressed_handle.seek(0)
        logical = hashlib.sha256()
        count = 0
        try:
            with gzip.GzipFile(fileobj=compressed_handle, mode="rb") as handle:
                for raw_line in handle:
                    logical.update(raw_line)
                    count += 1
        except (OSError, EOFError, gzip.BadGzipFile) as error:
            raise ChronicleExternalAdmissionError(
                f"cannot verify admitted gzip partition: {error}"
            ) from error
        if logical.hexdigest() != _sha(
            partition.get("logical_content_sha256"), label="admitted logical SHA"
        ):
            raise ChronicleExternalAdmissionError(
                "admitted partition logical identity mismatch"
            )
        if count != _integer(
            partition.get("record_count"), label="admitted record_count"
        ):
            raise ChronicleExternalAdmissionError(
                "admitted partition record count mismatch"
            )
        compressed_handle.seek(0)
        yield compressed_handle


def _compact_player(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(record.get("metadata"), label="resolved player metadata")
    return {
        "guid": record.get("guid"),
        "name": metadata.get("name"),
        "class": metadata.get("class"),
        "race": metadata.get("race"),
        "level": metadata.get("level"),
        "evidence": "EXACT_METADATA_PLAYER_GUID_MATCH",
    }


def iter_versioned_reconstruction_rows(
    admission_manifest_path: str | Path,
    *,
    instance_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield verified rows with structured facts for a future reconstructor.

    Compressed and logical partition identities are checked before the first
    row is yielded.  Original normalized fields remain byte-semantically
    unchanged; the adapter adds only ``external_admission``.
    """

    admission, manifest_path = load_admission_manifest(admission_manifest_path)
    instances = _array(admission.get("instances"), label="admission.instances")
    selected = []
    for raw in instances:
        item = _mapping(raw, label="admission instance")
        if instance_id is None or item.get("instance_id") == instance_id:
            selected.append(item)
    if not selected:
        raise ChronicleExternalAdmissionError("requested admission instance is absent")
    if instance_id is not None and len(selected) != 1:
        raise ChronicleExternalAdmissionError("requested admission instance is duplicated")
    data_root = _find_data_root(manifest_path)

    for item in selected:
        admitted_instance = _text(item.get("instance_id"), label="admitted instance id")
        resolver_wrapper = _mapping(
            item.get("metadata_player_resolver"), label="metadata_player_resolver"
        )
        resolver: dict[str, Mapping[str, Any]] = {}
        for raw_player in _array(
            resolver_wrapper.get("players"), label="metadata player resolver players"
        ):
            player = _mapping(raw_player, label="resolved player")
            guid = canonical_guid(player.get("guid"), label="resolved player GUID")
            if guid in resolver:
                raise ChronicleExternalAdmissionError(
                    "duplicate player in admission resolver"
                )
            resolver[guid] = player
        path, partition = _resolve_admitted_partition(data_root, item)
        with _verified_admitted_partition_handle(
            path, partition
        ) as compressed_handle, gzip.GzipFile(
            fileobj=compressed_handle, mode="rb"
        ) as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ChronicleExternalAdmissionError(
                        f"invalid admitted JSON row {line_number}: {error}"
                    ) from error
                if not isinstance(row, dict) or row.get("instance") != admitted_instance:
                    raise ChronicleExternalAdmissionError(
                        "admitted stream row instance mismatch"
                    )
                source_guid = row.get("source_guid")
                target_guid = row.get("target_guid")
                source_key = (
                    canonical_guid(source_guid, label="row source GUID")
                    if source_guid is not None
                    else None
                )
                target_key = (
                    canonical_guid(target_guid, label="row target GUID")
                    if target_guid is not None
                    else None
                )
                source_player = resolver.get(source_key or "")
                target_player = resolver.get(target_key or "")
                official = row.get("official")
                message = (
                    official.get("message")
                    if isinstance(official, Mapping)
                    and isinstance(official.get("message"), Mapping)
                    else {}
                )
                is_classification = row.get("type") == "CLASS"
                classification = {
                    "entity_resolution": (
                        "METADATA_PLAYER_EXACT"
                        if is_classification and target_player is not None
                        else "UNRESOLVED"
                    ),
                    "semantic": "UNKNOWN_NONVOTING",
                    "semantic_label_inferred": False,
                    "official_unit_type_numeric": (
                        message.get("unit_type") if is_classification else None
                    ),
                    "official_affiliation_numeric": (
                        message.get("affiliation") if is_classification else None
                    ),
                    "evidence": (
                        "EXACT_METADATA_PLAYER_GUID_MATCH_WITH_OFFICIAL_NUMERIC_"
                        "CLASSIFICATION_PRESERVED"
                        if is_classification and target_player is not None
                        else "NO_EXACT_METADATA_PLAYER_GUID_MATCH_NO_SEMANTIC_GUESS"
                    ),
                }
                owner = message.get("owner") if row.get("type") == "CLASS" else None
                canonical_owner = canonicalize_optional_0x_owner(owner)
                owner_player = None
                if canonical_owner is not None:
                    owner_player = resolver.get("0x" + canonical_owner)
                adapted = dict(row)
                adapted["external_admission"] = {
                    "schema": SCHEMA,
                    "source_evidence_kind": SOURCE_EVIDENCE_KIND,
                    "source_player": (
                        _compact_player(source_player) if source_player else None
                    ),
                    "target_player": (
                        _compact_player(target_player) if target_player else None
                    ),
                    "classification": classification,
                    "owner": {
                        "official_value": owner,
                        "canonical_hex_without_optional_0x": canonical_owner,
                        "resolved_player": (
                            _compact_player(owner_player) if owner_player else None
                        ),
                        "resolution_policy": "exact full GUID only; no suffix or name inference",
                    },
                    "hostile_classification_inferred": False,
                    "legacy_outcome_rewritten": False,
                }
                yield adapted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Admit a verified Chronicle External API raw/normalization pair for "
            "a future versioned reconstruction consumer"
        )
    )
    parser.add_argument("--raw-manifest", type=Path, required=True)
    parser.add_argument("--normalization-manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_external_reconstruction_admission(
            raw_manifest_path=args.raw_manifest,
            normalization_manifest_path=args.normalization_manifest,
            data_root=args.data_root,
            output_directory=args.output_dir,
            workers=args.workers,
        )
    except ChronicleExternalAdmissionError as error:
        print(f"Chronicle External API admission failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = [
    "ChronicleExternalAdmissionError",
    "SCHEMA",
    "SOURCE_EVIDENCE_KIND",
    "STATUS",
    "build_external_reconstruction_admission",
    "canonical_guid",
    "canonicalize_optional_0x_owner",
    "iter_versioned_reconstruction_rows",
    "load_admission_manifest",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
