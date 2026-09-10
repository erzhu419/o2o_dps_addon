"""Build a versioned, provenance-explicit Fury partial trajectory from Chronicle.

The input is the immutable normalized JSONL produced by
``import_chronicle_csv``.  This module deliberately does not turn a Chronicle
``START`` row into a confirmed player action: START is a candidate, while GO
and FAIL are separate result observations that can be linked only when the
preceding candidate is unique.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
SCHEMA_NAME = "chronicle_fury_partial_trajectory/v1"
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
QUEUE_RELATIVE_PATH = Path("chronicle_raw") / "export_queue.json"
DERIVED_RELATIVE_PATH = (
    Path("derived") / "chronicle_fury_partial_trajectory" / "v1"
)

PROVENANCE_KINDS = frozenset(("OBSERVED", "RECONSTRUCTED", "INFERRED", "MISSING"))
AMOUNT_EVENT_TYPES = frozenset(("DMG", "DEAD", "RES"))
AMOUNT = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$"
)
UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
INFO_DETAIL = re.compile(
    r"^(?P<class>[A-Z]+)\s+(?P<race>.+?)\s+"
    r"talents=(?P<talents>\d+/\d+/\d+)\s+"
    r"gear=(?P<gear>\d+)\s+slots(?:\s+guild=(?P<guild>.*))?$"
)
AURA_DETAIL = re.compile(
    r"^(?P<change>Added|Removed|Updated)(?:\s+\(stacks=(?P<stacks>\d+)\))?$",
    re.IGNORECASE,
)
DETAIL_SEPARATOR = re.compile(r"\s*[\u00b7\u2022]\s*")


class FuryTrajectoryError(RuntimeError):
    """The source cannot produce the requested auditable partial trajectory."""


@dataclass(frozen=True)
class InstanceIdentity:
    canonical_instance_id: str
    source_instance_ref: str
    public_slug: str | None
    queue_path: Path | None
    queue_entry: dict[str, Any] | None


@dataclass(frozen=True)
class TrajectoryResult:
    canonical_instance_id: str
    player_guid: str
    record_count: int
    trajectory: Path
    manifest: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "schema": SCHEMA_NAME,
            "canonical_instance_id": self.canonical_instance_id,
            "player_guid": self.player_guid,
            "record_count": self.record_count,
            "trajectory": str(self.trajectory),
            "manifest": str(self.manifest),
        }


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    encounter_id: str
    event_index: int
    csv_line: int
    offset_ms: int
    spell_id: int | None
    spell_name: str | None


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _safe_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not safe:
        raise FuryTrajectoryError(f"unsafe empty filename component from {value!r}")
    return safe


def _guid_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _is_guid(value: Any, player_guid: str) -> bool:
    return _guid_key(value) == _guid_key(player_guid)


def parse_type_amount(event_type: str, value: Any) -> int | float | None:
    """Parse amounts only for DMG, DEAD, and RES, including thousands commas.

    Other event types use ``value`` for different semantics (for example GO
    hit counts), so they intentionally return ``None`` rather than being
    interpreted as damage or resource amounts.
    """

    if str(event_type or "").strip().upper() not in AMOUNT_EVENT_TYPES:
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value

    text = str(value).strip()
    if not AMOUNT.fullmatch(text):
        return None
    number = text.replace(",", "")
    if "." in number:
        return float(number)
    return int(number)


def parse_combatant_info(detail: Any) -> dict[str, Any] | None:
    """Parse the compact Chronicle INFO detail without inventing gear items."""

    if not isinstance(detail, str):
        return None
    match = INFO_DETAIL.fullmatch(detail.strip())
    if match is None:
        return None
    return {
        "class": match.group("class"),
        "race": match.group("race"),
        "talent_tree": [int(part) for part in match.group("talents").split("/")],
        "gear_slot_count": int(match.group("gear")),
        "guild": match.group("guild") or None,
    }


def parse_consume_detail(detail: Any) -> tuple[dict[str, str], list[str]]:
    """Preserve CONS key=value tokens and bare markers in their export order."""

    if not isinstance(detail, str) or not detail.strip():
        return {}, []
    attributes: dict[str, str] = {}
    markers: list[str] = []
    for token in DETAIL_SEPARATOR.split(detail.strip()):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            attributes[key.strip()] = value.strip()
        else:
            markers.append(token)
    return attributes, markers


def _parse_aura_detail(detail: Any) -> tuple[str | None, int | None]:
    if not isinstance(detail, str):
        return None, None
    match = AURA_DETAIL.fullmatch(detail.strip())
    if match is None:
        return None, None
    stacks = match.group("stacks")
    return match.group("change").lower(), int(stacks) if stacks is not None else None


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryTrajectoryError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise FuryTrajectoryError(f"{label} is not a JSON object: {path}")
    return value


def _queue_entry(
    data_root: Path, instance: str
) -> tuple[Path | None, dict[str, Any] | None]:
    queue_path = data_root / QUEUE_RELATIVE_PATH
    if not queue_path.is_file():
        return None, None
    queue = _load_json_object(queue_path, label="Chronicle export queue")
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise FuryTrajectoryError(f"queue has no entries list: {queue_path}")

    requested = instance.strip()
    requested_lower = requested.lower()
    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        instance_id = str(entry.get("instance_id") or "").strip()
        slug = str(entry.get("slug") or "").strip()
        if instance_id.lower() == requested_lower or slug == requested:
            matches.append(entry)
    if len(matches) > 1:
        raise FuryTrajectoryError(f"multiple queue entries match instance {instance!r}")
    return queue_path, matches[0] if matches else None


def _receipt_normalized(entry: dict[str, Any] | None, data_root: Path) -> Path | None:
    if entry is None:
        return None
    receipt = entry.get("import_receipt")
    if not isinstance(receipt, dict):
        return None
    raw_path = receipt.get("normalized")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path).expanduser()
    if path.is_file():
        return path.resolve()
    relocated = data_root / "normalized" / path.name
    return relocated.resolve() if relocated.is_file() else None


def _resolve_normalized(
    *,
    data_root: Path,
    requested_instance: str,
    entry: dict[str, Any] | None,
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FuryTrajectoryError(f"normalized JSONL does not exist: {path}")
        return path

    receipt_path = _receipt_normalized(entry, data_root)
    if receipt_path is not None:
        return receipt_path

    aliases = [requested_instance]
    if entry is not None:
        aliases.extend(
            str(entry.get(key) or "").strip() for key in ("instance_id", "slug")
        )
    candidates: set[Path] = set()
    normalized_dir = data_root / "normalized"
    for alias in aliases:
        if not alias:
            continue
        safe = _safe_component(alias)
        candidates.update(path.resolve() for path in normalized_dir.glob(f"{safe}__*.jsonl"))
    if not candidates:
        raise FuryTrajectoryError(
            f"no normalized JSONL found for instance {requested_instance!r}"
        )
    if len(candidates) != 1:
        rendered = ", ".join(str(path) for path in sorted(candidates))
        raise FuryTrajectoryError(
            "multiple normalized JSONL files match; pass --normalized: " + rendered
        )
    return next(iter(candidates))


def _read_normalized_line(line: str, input_line: int, path: Path) -> dict[str, Any]:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as error:
        raise FuryTrajectoryError(
            f"invalid normalized JSON at {path}:{input_line}: {error}"
        ) from error
    if not isinstance(row, dict):
        raise FuryTrajectoryError(
            f"normalized row is not an object at {path}:{input_line}"
        )
    provenance = row.get("provenance")
    event_index = row.get("event_index")
    csv_line = provenance.get("csv_line") if isinstance(provenance, dict) else None
    if not isinstance(event_index, int) or not isinstance(csv_line, int):
        raise FuryTrajectoryError(
            f"normalized row lacks integer event_index/csv_line at {path}:{input_line}"
        )
    return row


def _peek_source_instance_ref(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for input_line, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = _read_normalized_line(line, input_line, path)
                source_ref = str(row.get("instance") or "").strip()
                if not source_ref:
                    raise FuryTrajectoryError(
                        f"normalized row has no instance reference at {path}:{input_line}"
                    )
                return source_ref
    except (OSError, UnicodeError) as error:
        raise FuryTrajectoryError(f"cannot read normalized JSONL {path}: {error}") from error
    raise FuryTrajectoryError(f"normalized JSONL is empty: {path}")


def _resolve_identity(
    *,
    requested_instance: str,
    source_instance_ref: str,
    queue_path: Path | None,
    entry: dict[str, Any] | None,
    public_slug: str | None,
) -> InstanceIdentity:
    entry_id = str(entry.get("instance_id") or "").strip() if entry else ""
    entry_slug = str(entry.get("slug") or "").strip() if entry else ""
    canonical = entry_id
    if not canonical and UUID.fullmatch(source_instance_ref):
        canonical = source_instance_ref
    if not canonical and UUID.fullmatch(requested_instance):
        canonical = requested_instance
    if not canonical:
        raise FuryTrajectoryError(
            "canonical instance UUID is unavailable; provide an instance present in "
            "chronicle_raw/export_queue.json"
        )
    if entry_id and source_instance_ref.lower() not in (
        entry_id.lower(),
        entry_slug.lower(),
    ):
        raise FuryTrajectoryError(
            f"normalized instance {source_instance_ref!r} does not match queue identity"
        )

    resolved_slug = public_slug.strip() if public_slug else entry_slug or None
    if public_slug and entry_slug and resolved_slug != entry_slug:
        raise FuryTrajectoryError(
            f"--public-slug {resolved_slug!r} disagrees with queue slug {entry_slug!r}"
        )
    return InstanceIdentity(
        canonical_instance_id=canonical,
        source_instance_ref=source_instance_ref,
        public_slug=resolved_slug,
        queue_path=queue_path,
        queue_entry=entry,
    )


def _event_anchor(row: dict[str, Any]) -> tuple[int, int]:
    provenance = row["provenance"]
    return int(row["event_index"]), int(provenance["csv_line"])


def _evidence(
    kind: str,
    row: dict[str, Any],
    *,
    note: str | None = None,
    source_artifact: str | None = None,
) -> dict[str, Any]:
    if kind not in PROVENANCE_KINDS:
        raise AssertionError(f"unknown provenance kind: {kind}")
    event_index, csv_line = _event_anchor(row)
    result: dict[str, Any] = {
        "kind": kind,
        "event_index": event_index,
        "csv_line": csv_line,
    }
    if source_artifact:
        result["source_artifact"] = source_artifact
    if note:
        result["note"] = note
    return result


def _field_evidence(
    value: Any,
    row: dict[str, Any],
    *,
    observed_note: str | None = None,
    missing_note: str | None = None,
) -> dict[str, Any]:
    if value is None:
        return _evidence("MISSING", row, note=missing_note or "not present in export row")
    return _evidence("OBSERVED", row, note=observed_note)


def _player_name_from_row(row: dict[str, Any], player_guid: str) -> str | None:
    for side in ("source", "target"):
        if _is_guid(row.get(f"{side}_guid"), player_guid):
            name = row.get(side)
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _discover_player_name(
    path: Path, player_guid: str, explicit_name: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    discovered_name: str | None = None
    discovered_row: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for input_line, line in enumerate(handle, start=1):
                if player_guid.lower() not in line.lower():
                    continue
                row = _read_normalized_line(line, input_line, path)
                name = _player_name_from_row(row, player_guid)
                if name:
                    discovered_name = name
                    discovered_row = row
                    break
    except (OSError, UnicodeError) as error:
        raise FuryTrajectoryError(f"cannot read normalized JSONL {path}: {error}") from error

    if explicit_name is not None:
        explicit = explicit_name.strip()
        if not explicit:
            raise FuryTrajectoryError("--player-name must not be empty")
        if discovered_name is not None and explicit != discovered_name:
            raise FuryTrajectoryError(
                f"--player-name {explicit!r} disagrees with GUID name {discovered_name!r}"
            )
        return explicit, discovered_row
    return discovered_name, discovered_row


def _record(
    *,
    record_kind: str,
    row: dict[str, Any],
    identity: InstanceIdentity,
    player_guid: str,
    player_name: str | None,
    player_name_row: dict[str, Any] | None,
    fields: dict[str, Any],
    field_provenance: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing = sorted(set(fields).difference(field_provenance))
    extra = sorted(set(field_provenance).difference(fields))
    if missing or extra:
        raise AssertionError(
            f"field provenance mismatch: missing={missing!r}, extra={extra!r}"
        )

    queue_artifact = str(identity.queue_path) if identity.queue_path else None
    event_index, csv_line = _event_anchor(row)
    identity_values = {
        "canonical_instance_id": identity.canonical_instance_id,
        "source_instance_ref": identity.source_instance_ref,
        "public_slug": identity.public_slug,
        "encounter_id": row.get("encounter"),
        "player_guid": player_guid,
        "player_name": player_name,
    }
    event_values = {
        "event_index": event_index,
        "csv_line": csv_line,
        "offset_ms": row.get("offset_ms"),
        "time": row.get("time"),
        "source_event_type": row.get("type"),
    }
    provenance = {
        "identity.canonical_instance_id": _evidence(
            "OBSERVED",
            row,
            note="canonical identity from export queue or normalized UUID",
            source_artifact=queue_artifact,
        ),
        "identity.source_instance_ref": _evidence("OBSERVED", row),
        "identity.public_slug": _evidence(
            "OBSERVED" if identity.public_slug else "MISSING",
            row,
            note=(
                "public page slug from export queue"
                if identity.public_slug
                else "no public slug was available"
            ),
            source_artifact=queue_artifact,
        ),
        "identity.encounter_id": _field_evidence(row.get("encounter"), row),
        "identity.player_guid": _evidence(
            "OBSERVED",
            row,
            note="selected GUID occurs as source or target in this trajectory",
        ),
        "identity.player_name": (
            _evidence(
                "OBSERVED" if _player_name_from_row(row, player_guid) else "RECONSTRUCTED",
                player_name_row or row,
                note="name attached to the selected GUID",
            )
            if player_name
            else _evidence("MISSING", row, note="GUID has no exported name")
        ),
        "event.event_index": _evidence("OBSERVED", row),
        "event.csv_line": _evidence("OBSERVED", row),
        "event.offset_ms": _field_evidence(row.get("offset_ms"), row),
        "event.time": _field_evidence(row.get("time"), row),
        "event.source_event_type": _field_evidence(row.get("type"), row),
    }
    provenance.update(
        {f"fields.{key}": value for key, value in field_provenance.items()}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": SCHEMA_NAME,
        "record_kind": record_kind,
        "identity": identity_values,
        "event": event_values,
        "fields": fields,
        "field_provenance": provenance,
    }


def _common_actor_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_name": row.get("source"),
        "source_guid": row.get("source_guid"),
        "target_name": row.get("target"),
        "target_guid": row.get("target_guid"),
        "spell_name": row.get("spell"),
        "spell_id": row.get("spell_id"),
        "synthetic": row.get("synthetic"),
        "flags": list(row.get("flags") or []),
    }


def _common_actor_provenance(
    row: dict[str, Any], fields: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        key: _field_evidence(value, row)
        for key, value in fields.items()
        if key in {
            "source_name",
            "source_guid",
            "target_name",
            "target_guid",
            "spell_name",
            "spell_id",
            "synthetic",
            "flags",
        }
    }


def _action_key(row: dict[str, Any]) -> tuple[str, str]:
    encounter = str(row.get("encounter") or "")
    spell_id = row.get("spell_id")
    if spell_id is not None:
        return encounter, f"id:{spell_id}"
    return encounter, "name:" + str(row.get("spell") or "").casefold()


def _candidate_id(row: dict[str, Any]) -> str:
    event_index, csv_line = _event_anchor(row)
    return f"{row.get('encounter')}:{event_index}:{csv_line}"


def _player_involvement(row: dict[str, Any], player_guid: str) -> bool:
    return _is_guid(row.get("source_guid"), player_guid) or _is_guid(
        row.get("target_guid"), player_guid
    )


def _player_is_source(row: dict[str, Any], player_guid: str) -> bool:
    return _is_guid(row.get("source_guid"), player_guid)


def _selected_event(row: dict[str, Any], player_guid: str) -> bool:
    event_type = str(row.get("type") or "").upper()
    if event_type in ("START", "GO", "FAIL", "CONS"):
        return _player_is_source(row, player_guid)
    if event_type in ("INFO", "CLASS", "AURA", "RES", "DMG", "DEAD"):
        return _player_involvement(row, player_guid)
    return False


def _info_record(
    row: dict[str, Any], parsed: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    fields = _common_actor_fields(row)
    fields.update(
        {
            "class": parsed.get("class") if parsed else None,
            "race": parsed.get("race") if parsed else None,
            "talent_tree": parsed.get("talent_tree") if parsed else None,
            "gear_slot_count": parsed.get("gear_slot_count") if parsed else None,
            "gear_items": None,
            "guild": parsed.get("guild") if parsed else None,
            "raw_detail": row.get("outcome"),
            "parse_status": "parsed" if parsed else "partial_unparsed",
        }
    )
    evidence = _common_actor_provenance(row, fields)
    for key in ("class", "race", "talent_tree", "gear_slot_count", "guild"):
        evidence[key] = _field_evidence(
            fields[key],
            row,
            missing_note="field was not recoverable from INFO detail",
        )
    evidence["gear_items"] = _evidence(
        "MISSING",
        row,
        note="All Activity INFO reports a slot count, not per-slot item IDs",
    )
    evidence["raw_detail"] = _field_evidence(row.get("outcome"), row)
    evidence["parse_status"] = _evidence("RECONSTRUCTED", row)
    return fields, evidence


def _consume_record(
    row: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    attributes, markers = parse_consume_detail(row.get("outcome"))
    flags = [str(flag) for flag in row.get("flags") or []]
    projected = any(flag.upper() == "PROJECTED" for flag in flags) or any(
        marker.casefold() == "projection" for marker in markers
    )
    fields = _common_actor_fields(row)
    fields.update(
        {
            "raw_value": row.get("value"),
            "raw_detail": row.get("outcome"),
            "attributes": attributes,
            "markers": markers,
            "confidence": attributes.get("confidence"),
            "projected": projected,
        }
    )
    evidence = _common_actor_provenance(row, fields)
    for key in ("raw_value", "raw_detail", "attributes", "markers", "confidence"):
        evidence[key] = _field_evidence(
            fields[key], row, missing_note=f"CONS {key} is absent"
        )
    evidence["projected"] = _evidence(
        "RECONSTRUCTED",
        row,
        note="derived only from the exported PROJECTED flag or projection marker",
    )
    return fields, evidence


def _candidate_record(
    row: dict[str, Any],
    context: tuple[dict[str, Any], dict[str, Any]] | None,
    rage_gain: int | float,
    rage_anchor: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], Candidate]:
    fields = _common_actor_fields(row)
    candidate_id = _candidate_id(row)
    fields.update(
        {
            "candidate_action_id": candidate_id,
            "candidate_only": True,
            "cast_detail": row.get("outcome"),
            "raw_value": row.get("value"),
            "state_class": context[0].get("class") if context else None,
            "state_race": context[0].get("race") if context else None,
            "state_talent_tree": context[0].get("talent_tree") if context else None,
            "state_gear_slot_count": (
                context[0].get("gear_slot_count") if context else None
            ),
            "state_gear_items": None,
            "state_observed_rage_gain_chronicle_units": rage_gain,
            "state_absolute_rage": None,
            "state_gcd_remaining_ms": None,
            "state_mainhand_swing_remaining_ms": None,
            "state_offhand_swing_remaining_ms": None,
            "state_moving": None,
            "state_target_range": None,
            "state_target_hp": None,
            "state_target_armor": None,
        }
    )
    evidence = _common_actor_provenance(row, fields)
    evidence["candidate_action_id"] = _evidence(
        "RECONSTRUCTED", row, note="stable reference from encounter/event/csv anchors"
    )
    evidence["candidate_only"] = _evidence(
        "RECONSTRUCTED",
        row,
        note="START is server-start evidence, not client keypress or confirmed completion",
    )
    evidence["cast_detail"] = _field_evidence(row.get("outcome"), row)
    evidence["raw_value"] = _field_evidence(row.get("value"), row)
    for key, parsed_key in (
        ("state_class", "class"),
        ("state_race", "race"),
        ("state_talent_tree", "talent_tree"),
        ("state_gear_slot_count", "gear_slot_count"),
    ):
        if context and fields[key] is not None:
            evidence[key] = _evidence(
                "RECONSTRUCTED",
                context[1],
                note=f"latest prior INFO.{parsed_key} in this encounter",
            )
        else:
            evidence[key] = _evidence(
                "MISSING", row, note="no prior parseable INFO row in this encounter"
            )
    evidence["state_gear_items"] = _evidence(
        "MISSING", row, note="INFO exposes only gear slot count in this CSV"
    )
    evidence["state_observed_rage_gain_chronicle_units"] = _evidence(
        "RECONSTRUCTED",
        rage_anchor or row,
        note="causal sum of exported Gain Rage rows; this is not current rage",
    )
    evidence["state_absolute_rage"] = _evidence(
        "MISSING",
        row,
        note="CSV has no absolute rage snapshot and selected data has no Loss Rage rows",
    )
    missing_state_notes = {
        "state_gcd_remaining_ms": "not exported by Chronicle All Activity CSV",
        "state_mainhand_swing_remaining_ms": "not exported by Chronicle All Activity CSV",
        "state_offhand_swing_remaining_ms": "not exported by Chronicle All Activity CSV",
        "state_moving": "movement state is not exported",
        "state_target_range": "range is not exported",
        "state_target_hp": "exact target HP snapshot is not exported",
        "state_target_armor": "exact target armor snapshot is not exported",
    }
    for key, note in missing_state_notes.items():
        evidence[key] = _evidence("MISSING", row, note=note)

    event_index, csv_line = _event_anchor(row)
    candidate = Candidate(
        candidate_id=candidate_id,
        encounter_id=str(row.get("encounter") or ""),
        event_index=event_index,
        csv_line=csv_line,
        offset_ms=int(row.get("offset_ms") or 0),
        spell_id=row.get("spell_id") if isinstance(row.get("spell_id"), int) else None,
        spell_name=row.get("spell") if isinstance(row.get("spell"), str) else None,
    )
    return fields, evidence, candidate


def _result_record(
    row: dict[str, Any], candidates: list[Candidate]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    event_type = str(row.get("type") or "").upper()
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidates) == 1:
        association = "unique"
        matched = candidate_ids[0]
        latency_ms: int | None = int(row.get("offset_ms") or 0) - candidates[0].offset_ms
    elif len(candidates) > 1:
        association = "ambiguous"
        matched = None
        latency_ms = None
    else:
        association = "unlinked"
        matched = None
        latency_ms = None

    fields = _common_actor_fields(row)
    fields.update(
        {
            "result_status": "succeeded" if event_type == "GO" else "failed",
            "raw_value": row.get("value"),
            "raw_detail": row.get("outcome"),
            "association_status": association,
            "candidate_action_ids": candidate_ids,
            "matched_candidate_action_id": matched,
            "start_to_result_ms": latency_ms,
        }
    )
    evidence = _common_actor_provenance(row, fields)
    for key in ("result_status", "raw_value", "raw_detail"):
        evidence[key] = _field_evidence(fields[key], row)
    evidence["association_status"] = _evidence(
        "RECONSTRUCTED",
        row,
        note="exact encounter/spell causal matching against preceding START candidates",
    )
    evidence["candidate_action_ids"] = _evidence(
        "RECONSTRUCTED",
        row,
        note="all still-pending exact encounter/spell START candidates",
    )
    evidence["matched_candidate_action_id"] = _evidence(
        "RECONSTRUCTED" if matched else "MISSING",
        row,
        note=(
            "unique preceding candidate"
            if matched
            else "no unique candidate; no START was guessed"
        ),
    )
    evidence["start_to_result_ms"] = _evidence(
        "RECONSTRUCTED" if latency_ms is not None else "MISSING",
        row,
        note=(
            "result offset minus unique START offset"
            if latency_ms is not None
            else "latency requires a unique START association"
        ),
    )
    return fields, evidence, association


def _reward_record(
    row: dict[str, Any], player_guid: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    amount = parse_type_amount(str(row.get("type") or ""), row.get("value"))
    source_player = _is_guid(row.get("source_guid"), player_guid)
    target_player = _is_guid(row.get("target_guid"), player_guid)
    if source_player and target_player:
        direction = "self"
        component = "self_damage"
    elif source_player:
        direction = "outgoing"
        component = "outgoing_damage"
    else:
        direction = "incoming"
        component = "incoming_damage"
    flags = [str(flag).upper() for flag in row.get("flags") or []]
    outcome = str(row.get("outcome") or "")
    critical = "CRIT" in flags or outcome.casefold().startswith("crit")
    lethal = str(row.get("type") or "").upper() == "DEAD"

    fields = _common_actor_fields(row)
    fields.update(
        {
            "raw_value": row.get("value"),
            "damage_amount": amount,
            "damage_detail": row.get("outcome"),
            "direction": direction,
            "reward_component": component,
            "critical": critical,
            "lethal_damage_event": lethal,
            "terminal_for_player": lethal and target_player,
        }
    )
    evidence = _common_actor_provenance(row, fields)
    evidence["raw_value"] = _field_evidence(row.get("value"), row)
    evidence["damage_amount"] = _evidence(
        "OBSERVED" if amount is not None else "MISSING",
        row,
        note="type-specific numeric parse; thousands separators are removed",
    )
    evidence["damage_detail"] = _field_evidence(row.get("outcome"), row)
    for key in ("direction", "reward_component", "critical", "terminal_for_player"):
        evidence[key] = _evidence(
            "RECONSTRUCTED", row, note="derived from observed actor GUID/type/flags"
        )
    evidence["lethal_damage_event"] = _evidence(
        "OBSERVED", row, note="true only when the exported type is DEAD"
    )
    return fields, evidence


def _resource_record(
    row: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str | None, str | None]:
    detail = str(row.get("outcome") or "")
    parts = [part.strip() for part in DETAIL_SEPARATOR.split(detail) if part.strip()]
    direction = parts[0] if parts else None
    resource = parts[1] if len(parts) > 1 else None
    amount = parse_type_amount("RES", row.get("value"))
    candidate_delta: int | float | None = None
    if resource and resource.casefold() == "rage" and amount is not None:
        sign = -1 if direction and direction.casefold() == "loss" else 1
        candidate_delta = sign * amount / 10

    fields = _common_actor_fields(row)
    fields.update(
        {
            "raw_value": row.get("value"),
            "raw_detail": row.get("outcome"),
            "direction": direction,
            "resource": resource,
            "amount_chronicle_units": amount,
            "wow_rage_delta_candidate": candidate_delta,
            "candidate_scale_divisor": 10 if candidate_delta is not None else None,
            "absolute_rage": None,
        }
    )
    evidence = _common_actor_provenance(row, fields)
    for key in ("raw_value", "raw_detail", "direction", "resource"):
        evidence[key] = _field_evidence(fields[key], row)
    evidence["amount_chronicle_units"] = _evidence(
        "OBSERVED" if amount is not None else "MISSING",
        row,
        note="numeric RES value as exported by Chronicle",
    )
    for key in ("wow_rage_delta_candidate", "candidate_scale_divisor"):
        evidence[key] = _evidence(
            "INFERRED" if candidate_delta is not None else "MISSING",
            row,
            note="uncalibrated candidate conversion Chronicle units / 10; not absolute rage",
        )
    evidence["absolute_rage"] = _evidence(
        "MISSING",
        row,
        note="the CSV contains a delta-like Gain Rage row, not an absolute snapshot",
    )
    return fields, evidence, direction, resource


def _aura_record(
    row: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    change, stacks = _parse_aura_detail(row.get("outcome"))
    fields = _common_actor_fields(row)
    fields.update(
        {
            "aura_change": change,
            "stacks": stacks,
            "raw_value": row.get("value"),
            "raw_detail": row.get("outcome"),
        }
    )
    evidence = _common_actor_provenance(row, fields)
    evidence["aura_change"] = _evidence(
        "RECONSTRUCTED" if change else "MISSING",
        row,
        note="parsed from the exported AURA detail",
    )
    evidence["stacks"] = _evidence(
        "RECONSTRUCTED" if stacks is not None else "MISSING",
        row,
        note="parsed from the exported AURA detail when present",
    )
    evidence["raw_value"] = _field_evidence(row.get("value"), row)
    evidence["raw_detail"] = _field_evidence(row.get("outcome"), row)
    return fields, evidence


def _classification_record(
    row: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    fields = _common_actor_fields(row)
    fields.update(
        {
            "classification": row.get("outcome"),
            "raw_value": row.get("value"),
        }
    )
    evidence = _common_actor_provenance(row, fields)
    evidence["classification"] = _field_evidence(row.get("outcome"), row)
    evidence["raw_value"] = _field_evidence(row.get("value"), row)
    return fields, evidence


def _write_jsonl_record(handle: Any, record: dict[str, Any]) -> None:
    json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
    handle.write("\n")


def _iter_selected_encounters(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    selected = {str(value).strip() for value in values if str(value).strip()}
    if not selected:
        raise FuryTrajectoryError("--encounter values must not be empty")
    return selected


def build_fury_partial_trajectory(
    *,
    instance: str,
    player_guid: str,
    player_name: str | None = None,
    encounters: Iterable[str] | None = None,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    normalized: str | Path | None = None,
    output_dir: str | Path | None = None,
    public_slug: str | None = None,
) -> TrajectoryResult:
    """Stream one normalized instance into a player-scoped partial trajectory."""

    requested_instance = instance.strip()
    requested_guid = player_guid.strip()
    if not requested_instance:
        raise FuryTrajectoryError("instance must not be empty")
    if not requested_guid:
        raise FuryTrajectoryError("player GUID must not be empty")

    resolved_root = Path(data_root).expanduser().resolve()
    queue_path, entry = _queue_entry(resolved_root, requested_instance)
    normalized_path = _resolve_normalized(
        data_root=resolved_root,
        requested_instance=requested_instance,
        entry=entry,
        explicit=normalized,
    )
    source_instance_ref = _peek_source_instance_ref(normalized_path)
    identity = _resolve_identity(
        requested_instance=requested_instance,
        source_instance_ref=source_instance_ref,
        queue_path=queue_path,
        entry=entry,
        public_slug=public_slug,
    )
    selected_encounters = _iter_selected_encounters(encounters)
    resolved_name, player_name_row = _discover_player_name(
        normalized_path, requested_guid, player_name
    )

    resolved_output_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else resolved_root / DERIVED_RELATIVE_PATH
    )
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    guid_component = _safe_component(requested_guid.replace("0x", "", 1))
    base = f"{_safe_component(identity.canonical_instance_id)}__{guid_component}"
    if selected_encounters is not None:
        if len(selected_encounters) == 1:
            base += "__" + _safe_component(next(iter(selected_encounters)))
        else:
            base += "__selected-encounters"
    trajectory_path = resolved_output_dir / f"{base}.jsonl"
    manifest_path = resolved_output_dir / f"{base}.manifest.json"

    source_event_counts: Counter[str] = Counter()
    record_counts: Counter[str] = Counter()
    association_counts: Counter[str] = Counter()
    encounters_seen: set[str] = set()
    contexts_seen: set[str] = set()
    info_parse_failures = 0
    classes_seen: set[str] = set()
    talent_trees_seen: set[tuple[int, ...]] = set()
    rage_gain_rows = 0
    rage_loss_rows = 0
    rage_other_rows = 0
    lines_scanned = 0
    selected_source_rows = 0
    record_count = 0

    contexts: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    rage_gain_by_encounter: defaultdict[str, int | float] = defaultdict(int)
    rage_anchor_by_encounter: dict[str, dict[str, Any]] = {}
    pending: defaultdict[tuple[str, str], list[Candidate]] = defaultdict(list)
    ambiguous_candidate_count = 0
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{base}__",
            suffix=".jsonl.tmp",
            dir=resolved_output_dir,
            delete=False,
        ) as temporary_handle:
            temporary_path = Path(temporary_handle.name)
            try:
                with normalized_path.open("r", encoding="utf-8") as source_handle:
                    for input_line, line in enumerate(source_handle, start=1):
                        if not line.strip():
                            continue
                        lines_scanned += 1
                        row = _read_normalized_line(line, input_line, normalized_path)
                        if str(row.get("instance") or "") != source_instance_ref:
                            raise FuryTrajectoryError(
                                f"mixed instance refs at {normalized_path}:{input_line}"
                            )
                        encounter_id = str(row.get("encounter") or "")
                        if selected_encounters is not None and encounter_id not in selected_encounters:
                            continue
                        if not _selected_event(row, requested_guid):
                            continue

                        selected_source_rows += 1
                        encounters_seen.add(encounter_id)
                        event_type = str(row.get("type") or "").upper()
                        source_event_counts[event_type] += 1
                        fields: dict[str, Any]
                        field_provenance: dict[str, dict[str, Any]]
                        record_kind: str

                        if event_type == "INFO":
                            parsed = parse_combatant_info(row.get("outcome"))
                            fields, field_provenance = _info_record(row, parsed)
                            record_kind = "encounter_context"
                            if parsed is None:
                                info_parse_failures += 1
                            else:
                                contexts[encounter_id] = (parsed, row)
                                contexts_seen.add(encounter_id)
                                classes_seen.add(str(parsed["class"]))
                                talent_trees_seen.add(tuple(parsed["talent_tree"]))
                        elif event_type == "CONS":
                            fields, field_provenance = _consume_record(row)
                            record_kind = "consume_evidence"
                        elif event_type == "START":
                            fields, field_provenance, candidate = _candidate_record(
                                row,
                                contexts.get(encounter_id),
                                rage_gain_by_encounter[encounter_id],
                                rage_anchor_by_encounter.get(encounter_id),
                            )
                            pending[_action_key(row)].append(candidate)
                            record_kind = "candidate_action"
                        elif event_type in ("GO", "FAIL"):
                            key = _action_key(row)
                            candidates = pending.pop(key, [])
                            fields, field_provenance, association = _result_record(
                                row, candidates
                            )
                            association_counts[association] += 1
                            if association == "ambiguous":
                                ambiguous_candidate_count += len(candidates)
                            record_kind = "action_result"
                        elif event_type in ("DMG", "DEAD"):
                            fields, field_provenance = _reward_record(row, requested_guid)
                            record_kind = "reward_event"
                        elif event_type == "RES":
                            fields, field_provenance, direction, resource = _resource_record(row)
                            record_kind = "resource_event"
                            if resource and resource.casefold() == "rage":
                                if direction and direction.casefold() == "gain":
                                    rage_gain_rows += 1
                                    amount = fields["amount_chronicle_units"]
                                    if amount is not None:
                                        rage_gain_by_encounter[encounter_id] += amount
                                        rage_anchor_by_encounter[encounter_id] = row
                                elif direction and direction.casefold() == "loss":
                                    rage_loss_rows += 1
                                else:
                                    rage_other_rows += 1
                        elif event_type == "AURA":
                            fields, field_provenance = _aura_record(row)
                            record_kind = "aura_event"
                        elif event_type == "CLASS":
                            fields, field_provenance = _classification_record(row)
                            record_kind = "classification_event"
                        else:  # Kept unreachable by _selected_event on purpose.
                            continue

                        record = _record(
                            record_kind=record_kind,
                            row=row,
                            identity=identity,
                            player_guid=requested_guid,
                            player_name=resolved_name,
                            player_name_row=player_name_row,
                            fields=fields,
                            field_provenance=field_provenance,
                        )
                        _write_jsonl_record(temporary_handle, record)
                        record_count += 1
                        record_counts[record_kind] += 1
            except (OSError, UnicodeError) as error:
                raise FuryTrajectoryError(
                    f"cannot stream normalized JSONL {normalized_path}: {error}"
                ) from error

        if record_count == 0:
            raise FuryTrajectoryError(
                f"no selected events found for player GUID {requested_guid!r}"
            )
        if classes_seen and classes_seen != {"WARRIOR"}:
            raise FuryTrajectoryError(
                "Fury trajectory contains non-WARRIOR INFO class values: "
                + ", ".join(sorted(classes_seen))
            )

        unresolved_candidates = sum(len(values) for values in pending.values())
        source_stat = normalized_path.stat()
        leaderboard_specs: list[str] = []
        if entry is not None:
            for board_row in entry.get("leaderboard_rows") or []:
                if not isinstance(board_row, dict):
                    continue
                if str(board_row.get("character") or "") != str(resolved_name or ""):
                    continue
                spec = str(board_row.get("observed_spec") or "").strip()
                if spec and spec not in leaderboard_specs:
                    leaderboard_specs.append(spec)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "schema": SCHEMA_NAME,
            "kind": "chronicle_fury_partial_trajectory_manifest",
            "generated_at": _utc_iso(datetime.now(timezone.utc)),
            "identity": {
                "canonical_instance_id": identity.canonical_instance_id,
                "source_instance_ref": identity.source_instance_ref,
                "public_slug": identity.public_slug,
            },
            "player": {
                "guid": requested_guid,
                "name": resolved_name,
                "leaderboard_observed_specs": leaderboard_specs,
                "info_classes": sorted(classes_seen),
                "info_talent_trees": [list(value) for value in sorted(talent_trees_seen)],
            },
            "selection": {
                "encounters_requested": (
                    sorted(selected_encounters) if selected_encounters is not None else None
                ),
                "encounters_emitted": sorted(encounters_seen),
                "player_scoped": True,
            },
            "source": {
                "normalized_file": str(normalized_path),
                "size_bytes": source_stat.st_size,
                "modified_at": _utc_iso(
                    datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc)
                ),
                "opened_read_only": True,
                "lines_scanned": lines_scanned,
                "selected_source_rows": selected_source_rows,
            },
            "output": {
                "trajectory_jsonl": str(trajectory_path),
                "manifest_json": str(manifest_path),
                "record_count": record_count,
                "record_counts": dict(sorted(record_counts.items())),
                "source_event_counts": dict(sorted(source_event_counts.items())),
            },
            "action_association": {
                "start_semantics": "candidate_only",
                "result_semantics": "GO_or_FAIL_observation",
                "unique_results": association_counts["unique"],
                "ambiguous_results": association_counts["ambiguous"],
                "unlinked_results": association_counts["unlinked"],
                "ambiguous_candidate_count": ambiguous_candidate_count,
                "unresolved_candidate_count": unresolved_candidates,
                "rule": "exact encounter and spell id/name; only one pending START is linked",
            },
            "combatant_info": {
                "encounters_with_parseable_info": sorted(contexts_seen),
                "parse_failures": info_parse_failures,
                "gear_scope": "slot_count_only; per-slot items are missing from this CSV",
            },
            "rage": {
                "gain_rows": rage_gain_rows,
                "loss_rows": rage_loss_rows,
                "other_rage_rows": rage_other_rows,
                "loss_rage_observed_in_selected_csv": rage_loss_rows > 0,
                "absolute_rage": "MISSING",
                "candidate_conversion": {
                    "chronicle_units_per_wow_rage": 10,
                    "provenance": "INFERRED",
                    "status": "uncalibrated_candidate_only",
                },
                "interpretation": (
                    "The selected CSV has no Loss Rage rows; Gain Rage values and the /10 "
                    "candidate cannot reconstruct observed absolute rage."
                    if rage_loss_rows == 0
                    else "Gain/Loss rows are deltas; /10 remains an uncalibrated candidate."
                ),
            },
            "quality": {
                "trajectory_status": "PARTIAL",
                "field_provenance_kinds": sorted(PROVENANCE_KINDS),
                "known_missing_state": [
                    "client keypress and queue intent",
                    "absolute rage",
                    "exact GCD and main/offhand swing timers",
                    "movement, range, exact target HP, and armor",
                    "per-slot gear item IDs in the All Activity INFO row",
                ],
                "training_note": (
                    "Use as event evidence and partial behavior trajectory. Do not treat "
                    "candidate START rows or inferred rage scale as ground-truth full state."
                ),
            },
        }

        if temporary_path is None:
            raise AssertionError("temporary trajectory path was not created")
        temporary_path.replace(trajectory_path)
        temporary_path = None
        try:
            with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except (OSError, UnicodeError) as error:
            trajectory_path.unlink(missing_ok=True)
            raise FuryTrajectoryError(f"cannot write manifest {manifest_path}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return TrajectoryResult(
        canonical_instance_id=identity.canonical_instance_id,
        player_guid=requested_guid,
        record_count=record_count,
        trajectory=trajectory_path,
        manifest=manifest_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_fury_trajectory",
        description=(
            "Build an auditable Fury player partial trajectory from immutable "
            "Chronicle normalized JSONL."
        ),
    )
    parser.add_argument("--instance", required=True, help="canonical UUID or public slug")
    parser.add_argument("--player-guid", required=True, help="Chronicle player GUID")
    parser.add_argument("--player-name", help="optional exact name check")
    parser.add_argument(
        "--encounter",
        action="append",
        dest="encounters",
        help="optional encounter UUID; repeat to select more than one",
    )
    parser.add_argument("--normalized", type=Path, help="explicit normalized JSONL path")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"offline data root (default: {DEFAULT_DATA_ROOT})",
    )
    parser.add_argument("--output-dir", type=Path, help="derived output directory")
    parser.add_argument(
        "--public-slug",
        help="public slug only when it cannot be resolved from the export queue",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = build_fury_partial_trajectory(
            instance=args.instance,
            player_guid=args.player_guid,
            player_name=args.player_name,
            encounters=args.encounters,
            data_root=args.data_root,
            normalized=args.normalized,
            output_dir=args.output_dir,
            public_slug=args.public_slug,
        )
    except FuryTrajectoryError as error:
        print(f"Fury trajectory ETL failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
