"""Exact, GUID-keyed Chronicle character DPS index.

The only input is a published
``chronicle_external_character_instance_inventory/v1`` manifest.  Parse
percentiles are deliberately ignored: rows are emitted only from an inventory
membership whose ``coverage.ranking_exact_dps.status`` is
``EXACT_RANKING_DPS_AVAILABLE``.  Missing exact-GUID evidence is represented in
the manifest and never materialized as a synthetic zero-DPS row.

The JSONL payload is written through ``zstd`` in deterministic key order.  Its
logical bytes and compressed envelope are both hashed, the immutable partition
is committed first, and the content-addressed manifest is committed last.
Loading performs a full streaming replay against the bound inventory.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from itertools import zip_longest
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence, TextIO

from . import chronicle_external_api_ingest_v1 as ingest_v1
from . import chronicle_external_character_history_v1 as history_v1


SCHEMA = "chronicle_external_character_dps_index/v1"
IMPLEMENTATION_REVISION = (
    "chronicle_external_character_dps_index_v1.2_boundary_training_eligibility"
)
KIND = "chronicle_external_character_exact_dps_index"
RECORD_SCHEMA = "chronicle_external_character_exact_dps_record/v1"

OUTPUT_DIRECTORY = "derived/chronicle_external_character_dps_index/v1"
PARTITION_DIRECTORY = "partitions"
MANIFEST_DIRECTORY = "manifests"
MANIFEST_PREFIX = "chronicle_external_character_dps_index_v1"
PARTITION_PREFIX = "chronicle_external_character_dps_index_v1"

EXACT_AVAILABLE = "EXACT_RANKING_DPS_AVAILABLE"
RANKING_MISSING = "MISSING"
CAPTURED_GUID_ABSENT = "RANKING_ENDPOINT_CAPTURED_EXACT_GUID_ABSENT"
CAPTURED_GUID_ABSENT_NAME_CONFLICT = (
    "RANKING_ENDPOINT_CAPTURED_EXACT_GUID_ABSENT_NAME_PRESENT_CONFLICT"
)
LEGACY_EXPLICIT_STATE_UNKNOWN = "LEGACY_INVENTORY_EXPLICIT_ENDPOINT_STATE_UNKNOWN"

_EXPLICIT_ENDPOINT_FIELDS = {
    "exact_dps_available",
    "usable_for_exact_dps_training",
    "raid_contamination_training_eligible",
    "ranking_endpoint_captured",
    "ranking_endpoint_capture_count",
    "expected_name_exact_match_record_count",
    "expected_name_match_guids",
    "ranking_fetch_required",
}

_ROW_FIELDS = {
    "character_guid",
    "character_name",
    "server",
    "realm",
    "instance_id",
    "instance_name",
    "started_at",
    "uploaded_at",
    "guild",
    "contamination_label",
    "encounter_id",
    "encounter_name",
    "killed_at",
    "damage_done",
    "duration_secs",
    "dps",
    "spec",
    "role",
    "ranking_record_id",
}


class CharacterDpsIndexError(RuntimeError):
    """An inventory, exact-DPS projection, or committed index is invalid."""


@dataclass(frozen=True)
class InventoryAnalysis:
    """Small pre-pass result; exact ranking rows remain streaming."""

    summary: dict[str, Any]
    missing_memberships: tuple[dict[str, Any], ...]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_document_bytes(value: Any) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(core))
    value.pop("content_address", None)
    digest = _sha256(_canonical_document_bytes(value))
    value["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document excluding content_address",
        "sha256": digest,
    }
    return value


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = value.get("content_address")
    if not isinstance(address, dict) or set(address) != {
        "algorithm",
        "scope",
        "sha256",
    }:
        raise CharacterDpsIndexError(f"{label} content_address schema is invalid")
    if (
        address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON document excluding content_address"
    ):
        raise CharacterDpsIndexError(f"{label} content_address contract is unsupported")
    declared = _digest(address.get("sha256"), label=f"{label} content sha256")
    core = {key: child for key, child in value.items() if key != "content_address"}
    if _sha256(_canonical_document_bytes(core)) != declared:
        raise CharacterDpsIndexError(f"{label} content hash mismatch")
    return declared


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CharacterDpsIndexError(f"{label} must be a JSON object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CharacterDpsIndexError(f"{label} must be a JSON array")
    return value


def _text(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CharacterDpsIndexError(f"{label} must be a non-empty string")
    return value.strip()


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CharacterDpsIndexError(f"{label} must be an integer >= {minimum}")
    return value


def _number(
    value: Any,
    *,
    label: str,
    minimum: float = 0.0,
    strictly_positive: bool = False,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise CharacterDpsIndexError(f"{label} must be a finite number")
    result = float(value)
    if (strictly_positive and result <= minimum) or (
        not strictly_positive and result < minimum
    ):
        operator = ">" if strictly_positive else ">="
        raise CharacterDpsIndexError(f"{label} must be {operator} {minimum}")
    return result


def _digest(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CharacterDpsIndexError(f"{label} must be a lowercase sha256 digest")
    return value


def _timestamp(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    try:
        parsed = ingest_v1._parse_rfc3339(value, field=label)
    except ingest_v1.ChronicleIngestError as error:
        raise CharacterDpsIndexError(str(error)) from error
    if parsed.year <= 1:
        if allow_none:
            return None
        raise CharacterDpsIndexError(f"{label} is a zero/missing timestamp")
    assert isinstance(value, str)
    return value.strip()


def _character_guid(value: Any, *, label: str) -> str:
    try:
        return history_v1._character_guid(value, label=label)
    except history_v1.CharacterHistoryError as error:
        raise CharacterDpsIndexError(str(error)) from error


def _instance_id(value: Any, *, label: str) -> str:
    try:
        return history_v1._instance_id(value, label=label)
    except history_v1.CharacterHistoryError as error:
        raise CharacterDpsIndexError(str(error)) from error


def _guild(value: Any, *, label: str) -> dict[str, Any] | None:
    try:
        return history_v1._parse_guild(value, label=label)
    except history_v1.CharacterHistoryError as error:
        raise CharacterDpsIndexError(str(error)) from error


def _data_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.lower() for part in resolved.parts}:
        raise CharacterDpsIndexError(
            f"character DPS index must stay inside an offline_data tree: {resolved}"
        )
    return resolved


def _output_root(data_root: Path) -> Path:
    return _data_root(data_root) / Path(OUTPUT_DIRECTORY)


def _safe_relative(path: Path, root: Path, *, label: str) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise CharacterDpsIndexError(f"{label} escapes data_root") from error
    return relative.as_posix()


def _resolve_relative(
    root: Path,
    value: Any,
    *,
    expected_parent: Path,
    label: str,
) -> Path:
    text = _text(value, label=label)
    assert text is not None
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise CharacterDpsIndexError(f"{label} must be a safe relative path")
    requested = root / relative
    if requested.is_symlink():
        raise CharacterDpsIndexError(f"{label} must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise CharacterDpsIndexError(f"cannot resolve {label}: {error}") from error
    if resolved.parent != expected_parent.resolve():
        raise CharacterDpsIndexError(f"{label} is outside its expected directory")
    return resolved


def _selected_query(membership: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return server/realm only from the selected exact history version.

    There is intentionally no lookup by character name.  A legacy or synthetic
    inventory lacking query provenance yields null server/realm fields.
    """

    direct_server = membership.get("server")
    direct_realm = membership.get("realm")
    if (direct_server is None) != (direct_realm is None):
        raise CharacterDpsIndexError("membership server/realm must be both present or absent")
    direct: tuple[str | None, str | None] = (None, None)
    if direct_server is not None:
        direct = (
            _text(direct_server, label="membership.server"),
            _text(direct_realm, label="membership.realm"),
        )

    audit_value = membership.get("history_version_audit")
    if audit_value is None:
        return direct
    audit = _mapping(audit_value, label="history_version_audit")
    versions = _array(audit.get("versions"), label="history_version_audit.versions")
    selected = [
        _mapping(value, label="selected history version")
        for value in versions
        if isinstance(value, dict) and value.get("selected") is True
    ]
    if len(selected) != 1:
        raise CharacterDpsIndexError(
            "history_version_audit must contain exactly one selected version"
        )
    query = _mapping(selected[0].get("query"), label="selected history query")
    expected_name = _text(membership.get("character_name"), label="character_name")
    query_name = _text(query.get("character"), label="selected query.character")
    if query_name != expected_name:
        raise CharacterDpsIndexError(
            "selected history query character differs from membership character_name"
        )
    from_query = (
        _text(query.get("server"), label="selected query.server"),
        _text(query.get("realm"), label="selected query.realm"),
    )
    if direct != (None, None) and direct != from_query:
        raise CharacterDpsIndexError(
            "membership server/realm conflicts with selected history query"
        )
    return from_query


def _membership_identity(membership: Mapping[str, Any]) -> dict[str, Any]:
    character_guid = _character_guid(
        membership.get("character_guid"), label="membership.character_guid"
    )
    character_name = _text(
        membership.get("character_name"), label="membership.character_name"
    )
    instance_id = _instance_id(
        membership.get("instance_id"), label="membership.instance_id"
    )
    instance_name = _text(membership.get("name"), label="membership.name")
    started_at = _timestamp(
        membership.get("started_at"), label="membership.started_at"
    )
    uploaded_at = _timestamp(
        membership.get("uploaded_at"), label="membership.uploaded_at"
    )
    guild = _guild(membership.get("guild"), label="membership.guild")
    contamination = _text(
        membership.get("contamination_label"), label="membership.contamination_label"
    )
    expected_contamination = ingest_v1.classify_range_bug(
        guild.get("name") if guild is not None else None,
        started_at,
    )
    if contamination != expected_contamination:
        raise CharacterDpsIndexError(
            "membership contamination_label disagrees with guild + started_at"
        )
    server, realm = _selected_query(membership)
    return {
        "character_guid": character_guid,
        "character_name": character_name,
        "server": server,
        "realm": realm,
        "instance_id": instance_id,
        "instance_name": instance_name,
        "started_at": started_at,
        "uploaded_at": uploaded_at,
        "guild": guild,
        "contamination_label": contamination,
    }


def _endpoint_semantics(
    exact: Mapping[str, Any],
    *,
    record_count: int,
    contamination_label: str,
) -> tuple[str, bool]:
    status = _text(exact.get("status"), label="ranking_exact_dps.status")
    fields_present = _EXPLICIT_ENDPOINT_FIELDS & set(exact)
    if fields_present and fields_present != _EXPLICIT_ENDPOINT_FIELDS:
        missing = sorted(_EXPLICIT_ENDPOINT_FIELDS - set(exact))
        raise CharacterDpsIndexError(
            f"ranking endpoint semantics are partial; missing {missing}"
        )
    explicit = fields_present == _EXPLICIT_ENDPOINT_FIELDS
    if not explicit:
        if status == EXACT_AVAILABLE and record_count > 0:
            return status, False
        if status == RANKING_MISSING and record_count == 0:
            return LEGACY_EXPLICIT_STATE_UNKNOWN, False
        raise CharacterDpsIndexError(
            "legacy ranking coverage has an unsupported status/record combination"
        )

    exact_available = exact.get("exact_dps_available")
    usable = exact.get("usable_for_exact_dps_training")
    contamination_training_eligible = exact.get(
        "raid_contamination_training_eligible"
    )
    captured = exact.get("ranking_endpoint_captured")
    fetch_required = exact.get("ranking_fetch_required")
    capture_count = _integer(
        exact.get("ranking_endpoint_capture_count"),
        label="ranking_endpoint_capture_count",
    )
    name_count = _integer(
        exact.get("expected_name_exact_match_record_count"),
        label="expected_name_exact_match_record_count",
    )
    match_guids_raw = _array(
        exact.get("expected_name_match_guids"), label="expected_name_match_guids"
    )
    # This list is diagnostic evidence about *other* ranking identities.  The
    # upstream endpoint may expose a non-character or otherwise opaque GUID;
    # preserve it as text but never treat it as a join candidate.
    match_guids = [
        _text(value, label="expected_name_match_guids[]")
        for value in match_guids_raw
    ]
    if match_guids != sorted(set(match_guids)):
        raise CharacterDpsIndexError(
            "expected_name_match_guids must be unique and deterministically sorted"
        )
    for label, value in (
        ("exact_dps_available", exact_available),
        ("usable_for_exact_dps_training", usable),
        (
            "raid_contamination_training_eligible",
            contamination_training_eligible,
        ),
        ("ranking_endpoint_captured", captured),
        ("ranking_fetch_required", fetch_required),
    ):
        if not isinstance(value, bool):
            raise CharacterDpsIndexError(f"{label} must be boolean")

    expected_training_eligible = (
        contamination_label in history_v1.TRAINING_CANDIDATE_LABELS
    )
    if contamination_training_eligible is not expected_training_eligible:
        raise CharacterDpsIndexError(
            "raid_contamination_training_eligible disagrees with contamination_label"
        )

    if status == EXACT_AVAILABLE:
        if not (
            record_count > 0
            and exact_available is True
            and usable is expected_training_eligible
            and captured is True
            and capture_count > 0
            and fetch_required is False
        ):
            raise CharacterDpsIndexError("EXACT ranking endpoint semantics are inconsistent")
        return status, False
    if status == RANKING_MISSING:
        if not (
            record_count == 0
            and exact_available is False
            and usable is False
            and captured is False
            and capture_count == 0
            and name_count == 0
            and not match_guids
            and fetch_required is True
        ):
            raise CharacterDpsIndexError("MISSING ranking endpoint semantics are inconsistent")
        return status, False
    if status == CAPTURED_GUID_ABSENT:
        if not (
            record_count == 0
            and exact_available is False
            and usable is False
            and captured is True
            and capture_count > 0
            and name_count == 0
            and not match_guids
            and fetch_required is False
        ):
            raise CharacterDpsIndexError(
                "captured exact-GUID-absent semantics are inconsistent"
            )
        return status, True
    if status == CAPTURED_GUID_ABSENT_NAME_CONFLICT:
        if not (
            record_count == 0
            and exact_available is False
            and usable is False
            and captured is True
            and capture_count > 0
            and name_count > 0
            and fetch_required is False
        ):
            raise CharacterDpsIndexError(
                "captured name-conflict semantics are inconsistent"
            )
        return status, True
    raise CharacterDpsIndexError(f"unsupported exact ranking status {status!r}")


def _coverage(membership: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Any]]:
    coverage = _mapping(membership.get("coverage"), label="membership.coverage")
    exact = _mapping(
        coverage.get("ranking_exact_dps"), label="coverage.ranking_exact_dps"
    )
    if exact.get("identity_match") != "EXACT_PLAYER_GUID_ONLY":
        raise CharacterDpsIndexError(
            "ranking evidence identity must be EXACT_PLAYER_GUID_ONLY"
        )
    records = _array(exact.get("records"), label="ranking_exact_dps.records")
    declared = _integer(exact.get("record_count"), label="ranking_exact_dps.record_count")
    if declared != len(records):
        raise CharacterDpsIndexError("ranking exact record_count disagrees with records")
    return exact, records


def _missing_descriptor(
    identity: Mapping[str, Any],
    exact: Mapping[str, Any],
    status: str,
    *,
    censored: bool,
) -> dict[str, Any]:
    return {
        "character_guid": identity["character_guid"],
        "character_name": identity["character_name"],
        "server": identity["server"],
        "realm": identity["realm"],
        "instance_id": identity["instance_id"],
        "instance_name": identity["instance_name"],
        "started_at": identity["started_at"],
        "uploaded_at": identity["uploaded_at"],
        "contamination_label": identity["contamination_label"],
        "missing_status": status,
        "censored_membership": censored,
        "expected_name_exact_match_record_count": exact.get(
            "expected_name_exact_match_record_count"
        ),
        "expected_name_match_guids": deepcopy(
            exact.get("expected_name_match_guids")
        ),
    }


def analyze_inventory(inventory: Mapping[str, Any]) -> InventoryAnalysis:
    """Validate membership-level evidence and summarize missing exact DPS."""

    if inventory.get("schema") != history_v1.INVENTORY_SCHEMA:
        raise CharacterDpsIndexError("unsupported character inventory schema")
    if (
        inventory.get("implementation_revision")
        != history_v1.INVENTORY_IMPLEMENTATION_REVISION
    ):
        raise CharacterDpsIndexError(
            "character inventory is not the current boundary-aware revision; "
            "rebuild it from migrated history and ingest manifests"
        )
    memberships = _array(inventory.get("instances"), label="inventory.instances")
    membership_keys: set[tuple[str, str]] = set()
    status_counts: Counter[str] = Counter()
    contamination_memberships: Counter[str] = Counter()
    missing: list[dict[str, Any]] = []
    exact_membership_count = 0
    exact_record_count = 0
    training_eligible_exact_membership_count = 0
    descriptive_nontraining_exact_membership_count = 0
    censored_count = 0
    for index, value in enumerate(memberships):
        membership = _mapping(value, label=f"inventory.instances[{index}]")
        identity = _membership_identity(membership)
        membership_key = (
            str(identity["character_guid"]).lower(),
            str(identity["instance_id"]),
        )
        if membership_key in membership_keys:
            raise CharacterDpsIndexError(
                "duplicate character_guid + instance_id membership"
            )
        membership_keys.add(membership_key)
        exact, records = _coverage(membership)
        status, censored = _endpoint_semantics(
            exact,
            record_count=len(records),
            contamination_label=str(identity["contamination_label"]),
        )
        status_counts[status] += 1
        contamination_memberships[str(identity["contamination_label"])] += 1
        if status == EXACT_AVAILABLE:
            exact_membership_count += 1
            exact_record_count += len(records)
            if (
                identity["contamination_label"]
                in history_v1.TRAINING_CANDIDATE_LABELS
            ):
                training_eligible_exact_membership_count += 1
            else:
                descriptive_nontraining_exact_membership_count += 1
        else:
            censored_count += int(censored)
            missing.append(
                _missing_descriptor(
                    identity, exact, status, censored=censored
                )
            )

    missing.sort(
        key=lambda row: (
            str(row["character_guid"]).lower(),
            str(row["instance_id"]),
            str(row["missing_status"]),
        )
    )
    summary = {
        "source_membership_count": len(memberships),
        "exact_membership_count": exact_membership_count,
        "exact_ranking_record_count": exact_record_count,
        "training_eligible_exact_membership_count": (
            training_eligible_exact_membership_count
        ),
        "descriptive_nontraining_exact_membership_count": (
            descriptive_nontraining_exact_membership_count
        ),
        "missing_membership_count": len(missing),
        "censored_membership_count": censored_count,
        "uncaptured_membership_count": status_counts[RANKING_MISSING],
        "legacy_endpoint_state_unknown_membership_count": status_counts[
            LEGACY_EXPLICIT_STATE_UNKNOWN
        ],
        "name_conflict_censored_membership_count": status_counts[
            CAPTURED_GUID_ABSENT_NAME_CONFLICT
        ],
        "ranking_status_counts": dict(sorted(status_counts.items())),
        "membership_contamination_label_counts": dict(
            sorted(contamination_memberships.items())
        ),
    }
    return InventoryAnalysis(summary=summary, missing_memberships=tuple(missing))


def _normalized_ranking_record(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    record = _mapping(value, label=label)
    return {
        "ranking_record_id": _instance_id(
            record.get("ranking_record_id"), label=f"{label}.ranking_record_id"
        ),
        "encounter_id": _text(
            record.get("encounter_id"),
            label=f"{label}.encounter_id",
            allow_none=True,
        ),
        "encounter_name": _text(
            record.get("encounter_name"),
            label=f"{label}.encounter_name",
            allow_none=True,
        ),
        "killed_at": _timestamp(
            record.get("killed_at"), label=f"{label}.killed_at", allow_none=True
        ),
        "damage_done": _number(
            record.get("damage_done"), label=f"{label}.damage_done"
        ),
        "duration_secs": _number(
            record.get("duration_secs"),
            label=f"{label}.duration_secs",
            strictly_positive=True,
        ),
        "dps": _number(record.get("dps"), label=f"{label}.dps"),
        "spec": _text(
            record.get("player_spec"), label=f"{label}.player_spec", allow_none=True
        ),
        "role": _text(
            record.get("player_role"), label=f"{label}.player_role", allow_none=True
        ),
    }


def iter_character_dps_rows(inventory: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield canonical index rows in strict unique-key order."""

    # Run the full membership pre-pass first; publication must fail before a
    # partition can be committed when endpoint semantics are inconsistent.
    analyze_inventory(inventory)
    memberships = [
        _mapping(value, label="inventory membership")
        for value in _array(inventory.get("instances"), label="inventory.instances")
    ]
    memberships.sort(
        key=lambda membership: (
            str(membership.get("character_guid", "")).lower(),
            str(membership.get("instance_id", "")),
        )
    )
    prior_key: tuple[str, str, str] | None = None
    seen: set[tuple[str, str, str]] = set()
    for membership in memberships:
        identity = _membership_identity(membership)
        exact, records = _coverage(membership)
        status, _ = _endpoint_semantics(
            exact,
            record_count=len(records),
            contamination_label=str(identity["contamination_label"]),
        )
        if status != EXACT_AVAILABLE:
            continue
        normalized = [
            _normalized_ranking_record(
                value,
                label=(
                    f"ranking record {identity['character_guid']}/"
                    f"{identity['instance_id']}[{index}]"
                ),
            )
            for index, value in enumerate(records)
        ]
        normalized.sort(key=lambda row: str(row["ranking_record_id"]))
        for ranking in normalized:
            key = (
                str(identity["character_guid"]).lower(),
                str(identity["instance_id"]),
                str(ranking["ranking_record_id"]),
            )
            if key in seen:
                raise CharacterDpsIndexError(
                    "duplicate character_guid + instance_id + ranking_record_id key"
                )
            if prior_key is not None and key <= prior_key:
                raise CharacterDpsIndexError("index key order is not strict")
            seen.add(key)
            prior_key = key
            yield {
                **identity,
                **ranking,
            }


def _validate_index_row(value: Any, *, label: str) -> dict[str, Any]:
    row = _mapping(value, label=label)
    if set(row) != _ROW_FIELDS:
        raise CharacterDpsIndexError(f"{label} field set is invalid")
    identity = {
        "character_guid": _character_guid(
            row.get("character_guid"), label=f"{label}.character_guid"
        ),
        "character_name": _text(
            row.get("character_name"), label=f"{label}.character_name"
        ),
        "server": _text(row.get("server"), label=f"{label}.server", allow_none=True),
        "realm": _text(row.get("realm"), label=f"{label}.realm", allow_none=True),
        "instance_id": _instance_id(
            row.get("instance_id"), label=f"{label}.instance_id"
        ),
        "instance_name": _text(
            row.get("instance_name"), label=f"{label}.instance_name"
        ),
        "started_at": _timestamp(
            row.get("started_at"), label=f"{label}.started_at"
        ),
        "uploaded_at": _timestamp(
            row.get("uploaded_at"), label=f"{label}.uploaded_at"
        ),
        "guild": _guild(row.get("guild"), label=f"{label}.guild"),
        "contamination_label": _text(
            row.get("contamination_label"), label=f"{label}.contamination_label"
        ),
    }
    expected = ingest_v1.classify_range_bug(
        identity["guild"].get("name") if identity["guild"] is not None else None,
        identity["started_at"],
    )
    if identity["contamination_label"] != expected:
        raise CharacterDpsIndexError(
            f"{label} contamination_label is not derived from started_at"
        )
    ranking = {
        "ranking_record_id": _instance_id(
            row.get("ranking_record_id"), label=f"{label}.ranking_record_id"
        ),
        "encounter_id": _text(
            row.get("encounter_id"), label=f"{label}.encounter_id", allow_none=True
        ),
        "encounter_name": _text(
            row.get("encounter_name"),
            label=f"{label}.encounter_name",
            allow_none=True,
        ),
        "killed_at": _timestamp(
            row.get("killed_at"), label=f"{label}.killed_at", allow_none=True
        ),
        "damage_done": _number(
            row.get("damage_done"), label=f"{label}.damage_done"
        ),
        "duration_secs": _number(
            row.get("duration_secs"),
            label=f"{label}.duration_secs",
            strictly_positive=True,
        ),
        "dps": _number(row.get("dps"), label=f"{label}.dps"),
        "spec": _text(row.get("spec"), label=f"{label}.spec", allow_none=True),
        "role": _text(row.get("role"), label=f"{label}.role", allow_none=True),
    }
    return {**identity, **ranking}


def _zstd_executable(value: str | Path | None) -> str:
    candidate = str(value) if value is not None else shutil.which("zstd")
    if not candidate:
        raise CharacterDpsIndexError(
            "zstd executable is required to write/read .jsonl.zst"
        )
    resolved = shutil.which(candidate) if not Path(candidate).is_file() else candidate
    if not resolved:
        raise CharacterDpsIndexError(f"zstd executable not found: {candidate}")
    return str(resolved)


def _stream_zstd_write(
    rows: Iterable[Mapping[str, Any]],
    destination: Path,
    *,
    zstd_executable: str,
) -> tuple[str, int, int]:
    logical = hashlib.sha256()
    logical_size = 0
    count = 0
    with destination.open("wb") as output:
        process = subprocess.Popen(
            [zstd_executable, "-q", "-3", "-T1", "-c"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stderr is not None
        try:
            for row in rows:
                payload = _canonical_document_bytes(row)
                process.stdin.write(payload)
                logical.update(payload)
                logical_size += len(payload)
                count += 1
            process.stdin.close()
            stderr = process.stderr.read()
            return_code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            process.stderr.close()
        if return_code != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise CharacterDpsIndexError(f"zstd compression failed: {message}")
        output.flush()
        os.fsync(output.fileno())
    return logical.hexdigest(), logical_size, count


def _iter_zstd_lines(path: Path, *, zstd_executable: str) -> Iterator[bytes]:
    process = subprocess.Popen(
        [zstd_executable, "-q", "-d", "-c", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    completed = False
    try:
        for raw_line in process.stdout:
            yield raw_line
        stderr = process.stderr.read()
        return_code = process.wait()
        completed = True
        if return_code != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise CharacterDpsIndexError(f"zstd decompression failed: {message}")
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
        if not completed and process.poll() is None:
            process.kill()
            process.wait()


def _inventory_binding(
    manifest: Mapping[str, Any], resolved: Path, data_root: Path
) -> dict[str, Any]:
    try:
        content_sha = history_v1._verify_content_address(
            manifest, label="character instance inventory"
        )
    except history_v1.CharacterHistoryError as error:
        raise CharacterDpsIndexError(str(error)) from error
    return {
        "schema": history_v1.INVENTORY_SCHEMA,
        "implementation_revision": manifest.get("implementation_revision"),
        "content_sha256": content_sha,
        "file_sha256": _sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
        "path": _safe_relative(resolved, data_root, label="inventory manifest"),
    }


def _commit_partition(temporary: Path, final: Path) -> None:
    if final.exists():
        if final.is_symlink() or not final.is_file():
            raise CharacterDpsIndexError(
                f"immutable partition target is not a regular file: {final}"
            )
        temporary.unlink()
    else:
        os.replace(temporary, final)


def _fixed_manifest_contracts() -> dict[str, Any]:
    return {
        "evidence_contract": {
            "accepted_coverage_status": EXACT_AVAILABLE,
            "exact_dps_source": "inventory coverage.ranking_exact_dps.records",
            "performance_parse_used": False,
            "percentile_converted_to_dps": False,
            "player_name_used_for_ranking_identity": False,
            "identity_join": "exact character GUID only",
            "synthetic_zero_dps_rows": 0,
            "legacy_missing_endpoint_state_inferred": False,
            "exact_dps_availability_is_training_eligibility": False,
            "training_use_requires_usable_for_exact_dps_training": True,
            "nontraining_exact_dps_rows_are_descriptive_only": True,
        },
        "temporal_contract": {
            "raid_date_field": "started_at",
            "contamination_split_field": "started_at",
            "uploaded_at_role": "informational provenance only",
            "uploaded_at_used_for_date_split": False,
            "future_arrival_or_later_event_used_for_identity_join": False,
        },
        "publication_contract": {
            "streamed_jsonl": True,
            "partition_committed_before_manifest": True,
            "manifest_content_addressed": True,
            "immutable_partition_not_overwritten": True,
        },
        "scientific_status": {
            "descriptive_exact_dps_index": True,
            "comparison_ready": False,
            "superiority_claim": False,
        },
    }


def publish_character_dps_index(
    inventory_manifest_path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Project and atomically publish one exact character-DPS index."""

    root = _data_root(Path(data_root))
    try:
        inventory, inventory_path = history_v1.load_character_instance_inventory_manifest(
            inventory_manifest_path, data_root=root
        )
    except history_v1.CharacterHistoryError as error:
        raise CharacterDpsIndexError(str(error)) from error
    analysis = analyze_inventory(inventory)
    output_root = _output_root(root)
    partition_directory = output_root / PARTITION_DIRECTORY
    manifest_directory = output_root / MANIFEST_DIRECTORY
    partition_directory.mkdir(parents=True, exist_ok=True)
    manifest_directory.mkdir(parents=True, exist_ok=True)
    zstd = _zstd_executable(zstd_executable)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{PARTITION_PREFIX}.", suffix=".jsonl.zst.tmp", dir=partition_directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        logical_sha, logical_size, row_count = _stream_zstd_write(
            iter_character_dps_rows(inventory), temporary, zstd_executable=zstd
        )
        if row_count != analysis.summary["exact_ranking_record_count"]:
            raise CharacterDpsIndexError(
                "streamed exact row count differs from inventory analysis"
            )
        final = partition_directory / (
            f"{PARTITION_PREFIX}.{logical_sha}.jsonl.zst"
        )
        temporary_compressed_sha = _sha256_file(temporary)
        temporary_size = temporary.stat().st_size
        _commit_partition(temporary, final)
        # If another producer already committed the same logical identity, use
        # that immutable envelope after strict replay below.  Compression-tool
        # version differences therefore never overwrite content.
        compressed_sha = _sha256_file(final)
        compressed_size = final.stat().st_size
        partition = {
            "path": _safe_relative(final, root, label="DPS index partition"),
            "compression": "zstd",
            "compression_level": 3,
            "compression_threads": 1,
            "record_schema": RECORD_SCHEMA,
            "logical_content_sha256": logical_sha,
            "logical_size_bytes": logical_size,
            "compressed_file_sha256": compressed_sha,
            "compressed_size_bytes": compressed_size,
            "record_count": row_count,
            "sort_key": [
                "character_guid_case_insensitive",
                "instance_id",
                "ranking_record_id",
            ],
            "unique_key": [
                "character_guid",
                "instance_id",
                "ranking_record_id",
            ],
        }
        manifest_core = {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": KIND,
            "source_inventory": _inventory_binding(inventory, inventory_path, root),
            "partition": partition,
            "missing_exact_dps": {
                "memberships": list(analysis.missing_memberships),
                "contract": (
                    "endpoint-captured exact-GUID absence is censored; no synthetic "
                    "zero-DPS record is emitted"
                ),
            },
            "summary": deepcopy(analysis.summary),
            **_fixed_manifest_contracts(),
        }
        manifest = _content_addressed(manifest_core)
        manifest_sha = _verify_content_address(manifest, label="DPS index manifest")
        manifest_path = manifest_directory / (
            f"{MANIFEST_PREFIX}.{manifest_sha}.manifest.json"
        )
        try:
            ingest_v1._atomic_write_bytes(
                manifest_path, _canonical_document_bytes(manifest)
            )
        except ingest_v1.ChronicleIngestError as error:
            raise CharacterDpsIndexError(str(error)) from error
        # Immediately exercise the same strict replay used by audit consumers.
        load_character_dps_index_manifest(
            manifest_path,
            data_root=root,
            zstd_executable=zstd,
        )
        return {
            "schema": SCHEMA,
            "content_sha256": manifest_sha,
            "manifest_path": str(manifest_path),
            "partition_path": str(final),
            "partition_logical_sha256": logical_sha,
            "summary": deepcopy(analysis.summary),
            "new_compressed_envelope_sha256": temporary_compressed_sha,
            "new_compressed_envelope_size_bytes": temporary_size,
            "committed_compressed_envelope_sha256": compressed_sha,
            "committed_compressed_envelope_size_bytes": compressed_size,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_manifest_bytes(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise CharacterDpsIndexError(f"cannot read DPS index manifest: {error}") from error
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CharacterDpsIndexError(f"DPS index manifest is invalid JSON: {error}") from error
    manifest = _mapping(value, label="DPS index manifest")
    if payload != _canonical_document_bytes(manifest):
        raise CharacterDpsIndexError("DPS index manifest is not canonical JSON")
    return deepcopy(dict(manifest))


def load_character_dps_index_manifest(
    path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Load, hash-check, and stream-replay an exact DPS index and its source."""

    root = _data_root(Path(data_root))
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise CharacterDpsIndexError("DPS index manifest must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise CharacterDpsIndexError(f"cannot resolve DPS index manifest: {error}") from error
    expected_manifest_directory = _output_root(root) / MANIFEST_DIRECTORY
    if resolved.parent != expected_manifest_directory.resolve():
        raise CharacterDpsIndexError("DPS index manifest is outside expected directory")
    manifest = _load_manifest_bytes(resolved)
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
        or manifest.get("kind") != KIND
    ):
        raise CharacterDpsIndexError("unsupported DPS index manifest")
    content_sha = _verify_content_address(manifest, label="DPS index manifest")
    if resolved.name != f"{MANIFEST_PREFIX}.{content_sha}.manifest.json":
        raise CharacterDpsIndexError("DPS index manifest filename/content hash mismatch")

    source = _mapping(manifest.get("source_inventory"), label="source_inventory")
    if source.get("schema") != history_v1.INVENTORY_SCHEMA:
        raise CharacterDpsIndexError("source inventory schema binding is unsupported")
    inventory_path = _resolve_relative(
        root,
        source.get("path"),
        expected_parent=(
            root / Path(history_v1.INVENTORY_MANIFEST_DIRECTORY)
        ),
        label="source inventory path",
    )
    if (
        inventory_path.stat().st_size
        != _integer(source.get("size_bytes"), label="source inventory size_bytes")
        or _sha256_file(inventory_path)
        != _digest(source.get("file_sha256"), label="source inventory file_sha256")
    ):
        raise CharacterDpsIndexError("source inventory file binding changed")
    try:
        inventory, loaded_inventory_path = (
            history_v1.load_character_instance_inventory_manifest(
                inventory_path, data_root=root
            )
        )
        inventory_content_sha = history_v1._verify_content_address(
            inventory, label="source inventory"
        )
    except history_v1.CharacterHistoryError as error:
        raise CharacterDpsIndexError(str(error)) from error
    if loaded_inventory_path != inventory_path:
        raise CharacterDpsIndexError("source inventory resolved path changed")
    if (
        inventory_content_sha
        != _digest(source.get("content_sha256"), label="source inventory content_sha256")
        or inventory.get("implementation_revision")
        != source.get("implementation_revision")
    ):
        raise CharacterDpsIndexError("source inventory semantic binding changed")

    analysis = analyze_inventory(inventory)
    if manifest.get("summary") != analysis.summary:
        raise CharacterDpsIndexError("manifest summary differs from inventory replay")
    missing = _mapping(manifest.get("missing_exact_dps"), label="missing_exact_dps")
    if missing.get("contract") != (
        "endpoint-captured exact-GUID absence is censored; no synthetic "
        "zero-DPS record is emitted"
    ):
        raise CharacterDpsIndexError("missing/censored evidence contract was weakened")
    if missing.get("memberships") != list(analysis.missing_memberships):
        raise CharacterDpsIndexError(
            "manifest missing/censored memberships differ from inventory replay"
        )
    for field, expected in _fixed_manifest_contracts().items():
        if manifest.get(field) != expected:
            raise CharacterDpsIndexError(f"DPS index {field} was weakened or changed")

    partition = _mapping(manifest.get("partition"), label="partition")
    if (
        partition.get("compression") != "zstd"
        or partition.get("compression_level") != 3
        or partition.get("compression_threads") != 1
        or partition.get("record_schema") != RECORD_SCHEMA
        or partition.get("sort_key")
        != ["character_guid_case_insensitive", "instance_id", "ranking_record_id"]
        or partition.get("unique_key")
        != ["character_guid", "instance_id", "ranking_record_id"]
    ):
        raise CharacterDpsIndexError("DPS index partition contract is unsupported")
    logical_sha = _digest(
        partition.get("logical_content_sha256"), label="partition logical sha256"
    )
    partition_path = _resolve_relative(
        root,
        partition.get("path"),
        expected_parent=_output_root(root) / PARTITION_DIRECTORY,
        label="DPS index partition path",
    )
    if partition_path.name != f"{PARTITION_PREFIX}.{logical_sha}.jsonl.zst":
        raise CharacterDpsIndexError("partition filename/logical hash mismatch")
    expected_compressed_size = _integer(
        partition.get("compressed_size_bytes"), label="partition compressed_size_bytes"
    )
    expected_compressed_sha = _digest(
        partition.get("compressed_file_sha256"), label="partition compressed_file_sha256"
    )
    if (
        partition_path.stat().st_size != expected_compressed_size
        or _sha256_file(partition_path) != expected_compressed_sha
    ):
        raise CharacterDpsIndexError("partition compressed envelope changed")

    zstd = _zstd_executable(zstd_executable)
    logical = hashlib.sha256()
    logical_size = 0
    count = 0
    prior_key: tuple[str, str, str] | None = None
    expected_rows = iter_character_dps_rows(inventory)
    sentinel = object()
    actual_lines = _iter_zstd_lines(partition_path, zstd_executable=zstd)
    for line_number, pair in enumerate(
        zip_longest(actual_lines, expected_rows, fillvalue=sentinel), start=1
    ):
        raw_line, expected_row = pair
        if raw_line is sentinel or expected_row is sentinel:
            raise CharacterDpsIndexError(
                "partition row count differs from source inventory replay"
            )
        assert isinstance(raw_line, bytes)
        logical.update(raw_line)
        logical_size += len(raw_line)
        if not raw_line.strip():
            raise CharacterDpsIndexError("partition contains an empty JSONL row")
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CharacterDpsIndexError(
                f"invalid partition JSONL row {line_number}: {error}"
            ) from error
        row = _validate_index_row(parsed, label=f"partition row {line_number}")
        if raw_line != _canonical_document_bytes(row):
            raise CharacterDpsIndexError(
                f"partition row {line_number} is not canonical JSONL"
            )
        if row != expected_row:
            raise CharacterDpsIndexError(
                f"partition row {line_number} differs from source inventory replay"
            )
        key = (
            str(row["character_guid"]).lower(),
            str(row["instance_id"]),
            str(row["ranking_record_id"]),
        )
        if prior_key is not None and key <= prior_key:
            raise CharacterDpsIndexError("partition unique-key order is not strict")
        prior_key = key
        count += 1
    if (
        count != _integer(partition.get("record_count"), label="partition record_count")
        or logical_size
        != _integer(partition.get("logical_size_bytes"), label="partition logical_size_bytes")
        or logical.hexdigest() != logical_sha
    ):
        raise CharacterDpsIndexError("partition logical identity/count mismatch")
    return manifest, resolved


def audit_character_dps_index(
    path: str | Path,
    *,
    data_root: Path = history_v1.DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    manifest, resolved = load_character_dps_index_manifest(
        path, data_root=data_root, zstd_executable=zstd_executable
    )
    return {
        "schema": SCHEMA,
        "status": "PASS_STRICT_HASH_AND_SOURCE_REPLAY",
        "manifest_path": str(resolved),
        "content_sha256": manifest["content_address"]["sha256"],
        "partition_logical_sha256": manifest["partition"][
            "logical_content_sha256"
        ],
        "summary": deepcopy(manifest["summary"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish/audit exact GUID-keyed Chronicle character DPS index"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--inventory-manifest", type=Path, required=True)
    build.add_argument("--data-root", type=Path, default=history_v1.DEFAULT_DATA_ROOT)
    build.add_argument("--zstd")
    audit = subparsers.add_parser("audit")
    audit.add_argument("--manifest", type=Path, required=True)
    audit.add_argument("--data-root", type=Path, default=history_v1.DEFAULT_DATA_ROOT)
    audit.add_argument("--zstd")
    return parser


def _write_output(
    value: Mapping[str, Any],
    *,
    stdout: TextIO | None,
    stdout_buffer: BinaryIO | None,
) -> None:
    payload = _canonical_document_bytes(value)
    if stdout is not None:
        if stdout_buffer is not None:
            raise CharacterDpsIndexError("stdout and stdout_buffer are mutually exclusive")
        stdout.write(payload.decode("utf-8"))
        return
    target = stdout_buffer if stdout_buffer is not None else getattr(sys.stdout, "buffer", None)
    if target is not None:
        target.write(payload)
    else:  # pragma: no cover - embedded text-only hosts
        sys.stdout.write(payload.decode("utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdout_buffer: BinaryIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        result = publish_character_dps_index(
            args.inventory_manifest,
            data_root=args.data_root,
            zstd_executable=args.zstd,
        )
    else:
        result = audit_character_dps_index(
            args.manifest,
            data_root=args.data_root,
            zstd_executable=args.zstd,
        )
    _write_output(result, stdout=stdout, stdout_buffer=stdout_buffer)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except CharacterDpsIndexError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)


__all__ = [
    "CAPTURED_GUID_ABSENT",
    "CAPTURED_GUID_ABSENT_NAME_CONFLICT",
    "CharacterDpsIndexError",
    "EXACT_AVAILABLE",
    "IMPLEMENTATION_REVISION",
    "InventoryAnalysis",
    "LEGACY_EXPLICIT_STATE_UNKNOWN",
    "RANKING_MISSING",
    "RECORD_SCHEMA",
    "SCHEMA",
    "analyze_inventory",
    "audit_character_dps_index",
    "iter_character_dps_rows",
    "load_character_dps_index_manifest",
    "main",
    "publish_character_dps_index",
]
