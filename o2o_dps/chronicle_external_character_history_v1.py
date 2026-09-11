"""Auditable discovery of one or more characters' Chronicle raid history.

This module deliberately stops at discovery and a bounded fetch plan.  It does
not download instance event streams.  Character identity and paginated history
responses can be captured as immutable objects; an existing
``chronicle_external_api_ingest/v1`` snapshot is then used to describe which
metadata, exact ranking records, and event-stream lanes are already present.
An explicitly captured ``UNAVAILABLE_404`` stream remains semantic negative
evidence and is not confused with an endpoint that has never been requested.

The character instances endpoint is authoritative for instance identifiers.
Leaderboard ``id`` fields are never used (the live API may currently expose a
zero UUID there).  Performance parses are discovery hints, not exact DPS
evidence; exact damage, duration, and DPS come only from ranking records.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, BinaryIO, Iterable, Mapping, Sequence, TextIO
from urllib.parse import quote

from . import chronicle_external_api_ingest_v1 as ingest_v1


SCHEMA = "chronicle_external_character_history/v1"
LEGACY_IMPLEMENTATION_REVISION = "chronicle_external_character_history_v1.0"
IMPLEMENTATION_REVISION = (
    "chronicle_external_character_history_v1.1_range_bug_boundary_20260903_noon"
)
SUPPORTED_IMPLEMENTATION_REVISIONS = frozenset(
    {LEGACY_IMPLEMENTATION_REVISION, IMPLEMENTATION_REVISION}
)
KIND = "chronicle_external_character_history_raw_snapshot"
INVENTORY_SCHEMA = "chronicle_external_character_instance_inventory/v1"
LEGACY_INVENTORY_IMPLEMENTATION_REVISION = (
    "chronicle_external_character_instance_inventory_v1.0"
)
RANKING_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION = (
    "chronicle_external_character_instance_inventory_v1.1_ranking_negative_evidence"
)
STREAM_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION = (
    "chronicle_external_character_instance_inventory_v1.2_stream_negative_evidence"
)
INVENTORY_IMPLEMENTATION_REVISION = (
    "chronicle_external_character_instance_inventory_v1.4_boundary_training_eligibility"
)
SUPPORTED_INVENTORY_IMPLEMENTATION_REVISIONS = frozenset(
    {
        LEGACY_INVENTORY_IMPLEMENTATION_REVISION,
        RANKING_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION,
        STREAM_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION,
        INVENTORY_IMPLEMENTATION_REVISION,
    }
)
INVENTORY_KIND = "chronicle_external_character_instance_inventory"
INVENTORY_MANIFEST_DIRECTORY = (
    "derived/chronicle_external_character_instance_inventory/v1/manifests"
)
INVENTORY_MANIFEST_PREFIX = "chronicle_external_character_instance_inventory_v1"

DEFAULT_DATA_ROOT = ingest_v1.DEFAULT_DATA_ROOT
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_INSTANCES = 10_000
DEFAULT_MAX_FETCH_INSTANCES = 500

ACTION_STREAMS = ("spell_fail", "spell_go", "spell_start")
DEFAULT_REQUIRED_STREAMS = (
    "combatant_info",
    "damage",
    "spell_fail",
    "spell_go",
    "spell_start",
    "unit_classification",
)
ZERO_INSTANCE_IDS = {
    "0",
    "00000000-0000-0000-0000-000000000000",
}
CHARACTER_GUID_PATTERN = re.compile(r"0x[0-9A-Fa-f]{16}\Z")
_MISSING_PERFORMANCE = object()
TRAINING_CANDIDATE_LABELS = {
    ingest_v1.POSTFIX_KNOWN_CLEAN,
    ingest_v1.NO_KNOWN_RULE_MATCH,
}
NONTRAINING_LABELS = {
    ingest_v1.SUSPECT_36YD_RANGE_BUG,
    ingest_v1.RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
    ingest_v1.UNKNOWN_NONVOTING,
}


class CharacterHistoryError(RuntimeError):
    """Character history response, manifest, or evidence closure is invalid."""


class CharacterHistorySafetyLimitError(CharacterHistoryError):
    """A bounded request/plan would be incomplete or unexpectedly large."""


@dataclass(frozen=True, order=True)
class CharacterQuery:
    """The three official path components that identify a character."""

    server: str
    realm: str
    character: str

    def __post_init__(self) -> None:
        for field in ("server", "realm", "character"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise CharacterHistoryError(f"character query {field} must be non-empty")
            object.__setattr__(self, field, value.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "server": self.server,
            "realm": self.realm,
            "character": self.character,
        }

    @property
    def api_path(self) -> str:
        segments = (self.server, self.realm, self.character)
        encoded = "/".join(quote(value, safe="") for value in segments)
        return f"/characters/{encoded}"


def _error(message: str, cause: Exception | None = None) -> CharacterHistoryError:
    result = CharacterHistoryError(message)
    if cause is not None:
        result.__cause__ = cause
    return result


def _canonical_document_bytes(value: Any) -> bytes:
    return ingest_v1._canonical_json_bytes(value)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    materialized = deepcopy(dict(core))
    materialized.pop("content_address", None)
    digest = _sha256(_canonical_document_bytes(materialized))
    materialized["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": digest,
    }
    return materialized


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = value.get("content_address")
    if not isinstance(address, dict):
        raise CharacterHistoryError(f"{label} lacks content_address")
    if set(address) != {"algorithm", "scope", "sha256"}:
        raise CharacterHistoryError(f"{label} content_address has an invalid schema")
    if address.get("algorithm") != "sha256" or address.get("scope") != (
        "canonical JSON excluding content_address"
    ):
        raise CharacterHistoryError(f"{label} content_address contract is unsupported")
    declared = address.get("sha256")
    if (
        not isinstance(declared, str)
        or len(declared) != 64
        or any(char not in "0123456789abcdef" for char in declared)
    ):
        raise CharacterHistoryError(f"{label} content_address sha256 is invalid")
    core = {key: child for key, child in value.items() if key != "content_address"}
    actual = _sha256(_canonical_document_bytes(core))
    if actual != declared:
        raise CharacterHistoryError(f"{label} content hash mismatch")
    return declared


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CharacterHistoryError(f"{label} must be a JSON object")
    return value


def _list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CharacterHistoryError(f"{label} must be a JSON list")
    return value


def _text(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CharacterHistoryError(f"{label} must be a non-empty string")
    return value.strip()


def _scalar_identifier(value: Any, *, label: str, allow_none: bool = False) -> Any:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CharacterHistoryError(f"{label} must be a string or integer identifier")
    if isinstance(value, str) and not value.strip():
        raise CharacterHistoryError(f"{label} must not be empty")
    return value.strip() if isinstance(value, str) else value


def _timestamp(
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    try:
        parsed = ingest_v1._parse_rfc3339(value, field=label)
    except ingest_v1.ChronicleIngestError as error:
        raise _error(str(error), error)
    # Keep the official spelling in the raw-derived record; parsing is what
    # makes timestamps comparable without rewriting source evidence.
    assert isinstance(value, str)
    if parsed.year <= 1:
        if allow_none:
            return None
        raise CharacterHistoryError(f"{label} is a missing/zero timestamp")
    return value.strip()


def _timestamp_key(value: str | None) -> float:
    if value is None:
        return float("-inf")
    try:
        return ingest_v1._parse_rfc3339(value, field="timestamp").timestamp()
    except ingest_v1.ChronicleIngestError as error:  # already validated
        raise _error(str(error), error)


def _is_zero_instance_id(value: str) -> bool:
    compact = value.strip().lower()
    if compact in ZERO_INSTANCE_IDS:
        return True
    return compact.replace("-", "").strip("0") == ""


def _instance_id(value: Any, *, label: str) -> str:
    result = _text(value, label=label)
    assert result is not None
    if _is_zero_instance_id(result):
        raise CharacterHistoryError(
            f"{label} is a zero instance id and cannot identify a raid instance"
        )
    return result


def _character_guid(value: Any, *, label: str) -> str:
    result = _text(value, label=label)
    assert result is not None
    if CHARACTER_GUID_PATTERN.fullmatch(result) is None:
        raise CharacterHistoryError(
            f"{label} must use the Chronicle 0x plus 16 hexadecimal digit format"
        )
    if int(result[2:], 16) == 0:
        raise CharacterHistoryError(f"{label} must not be an all-zero identity")
    return result


def _parse_named_entity(
    value: Any,
    *,
    label: str,
    require_server_id: bool = False,
) -> dict[str, Any]:
    entity = _mapping(value, label=label)
    result: dict[str, Any] = {
        "id": _scalar_identifier(entity.get("id"), label=f"{label}.id", allow_none=True),
        "name": _text(entity.get("name"), label=f"{label}.name"),
    }
    if require_server_id:
        result["server_id"] = _scalar_identifier(
            entity.get("server_id"),
            label=f"{label}.server_id",
            allow_none=True,
        )
    return result


def _parse_guild(value: Any, *, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    guild = _mapping(value, label=label)
    return {
        "id": _scalar_identifier(
            guild.get("id"), label=f"{label}.id", allow_none=True
        ),
        "name": _text(guild.get("name"), label=f"{label}.name"),
    }


def _parse_identity(
    value: Any,
    *,
    query: CharacterQuery,
    label: str,
) -> dict[str, Any]:
    identity = _mapping(value, label=label)
    guid = _character_guid(identity.get("guid"), label=f"{label}.guid")
    name = _text(identity.get("name"), label=f"{label}.name")
    server = _parse_named_entity(identity.get("server"), label=f"{label}.server")
    realm = _parse_named_entity(
        identity.get("realm"), label=f"{label}.realm", require_server_id=True
    )
    assert name is not None
    if name != query.character:
        raise CharacterHistoryError(
            f"{label} identity name does not match query: {name!r} != {query.character!r}"
        )
    if server["name"] != query.server or realm["name"] != query.realm:
        raise CharacterHistoryError(f"{label} server/realm identity does not match query")
    if (
        server["id"] is not None
        and realm["server_id"] is not None
        and server["id"] != realm["server_id"]
    ):
        raise CharacterHistoryError(f"{label} realm.server_id conflicts with server.id")

    result: dict[str, Any] = {
        "guid": guid,
        "name": name,
        "class": identity.get("class"),
        "race": identity.get("race"),
        "gender": identity.get("gender"),
        "level": identity.get("level"),
        "spec": identity.get("spec"),
        "role": identity.get("role"),
        "item_level": identity.get("item_level"),
        "server": server,
        "realm": realm,
        "guild": _parse_guild(identity.get("guild"), label=f"{label}.guild"),
        "updated_at": _timestamp(
            identity.get("updated_at"),
            label=f"{label}.updated_at",
            allow_none=True,
        ),
    }
    for field in ("class", "race", "gender", "spec", "role"):
        item = result[field]
        if item is not None and not isinstance(item, str):
            raise CharacterHistoryError(f"{label}.{field} must be a string or null")
    if result["level"] is not None and (
        isinstance(result["level"], bool) or not isinstance(result["level"], int)
    ):
        raise CharacterHistoryError(f"{label}.level must be an integer or null")
    item_level = result["item_level"]
    if item_level is not None and (
        isinstance(item_level, bool)
        or not isinstance(item_level, (int, float))
        or not math.isfinite(float(item_level))
    ):
        raise CharacterHistoryError(f"{label}.item_level must be finite or null")
    return result


def _identity_key(identity: Mapping[str, Any]) -> tuple[Any, ...]:
    server = _mapping(identity.get("server"), label="identity.server")
    realm = _mapping(identity.get("realm"), label="identity.realm")
    return (
        str(identity.get("guid")).lower(),
        identity.get("name"),
        server.get("id"),
        server.get("name"),
        realm.get("id"),
        realm.get("server_id"),
        realm.get("name"),
    )


def _parse_performance(
    value: Any,
    *,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    # The live API omits ``performance`` on some otherwise valid historical
    # character-instance memberships.  Preserve that exact ABSENT state and
    # treat it as missing parse evidence.  Explicit JSON null and all other
    # non-array shapes remain invalid so the compatibility is fail-closed.
    if value is _MISSING_PERFORMANCE:
        return [], {
            "source": "official character instances response",
            "source_field": "logs[].performance",
            "raw_field_state": "ABSENT",
            "raw_field_present": False,
            "raw_page_bytes_preserved_on_capture": True,
            "normalized_row_count": 0,
            "interpretation": "MISSING_PARSE_EVIDENCE_NOT_EMPTY_PARSE_RESULT",
        }
    rows = _list(value, label=label)
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row = _mapping(raw, label=f"{label}[{index}]")
        encounter = _text(
            row.get("encounter_name"), label=f"{label}[{index}].encounter_name"
        )
        for field in ("dps_parse", "hps_parse"):
            item = row.get(field)
            if item is not None and (
                isinstance(item, bool) or not isinstance(item, (dict, int, float))
            ):
                raise CharacterHistoryError(
                    f"{label}[{index}].{field} has an unsupported value"
                )
        result.append(
            {
                "encounter_name": encounter,
                "dps_parse": deepcopy(row.get("dps_parse")),
                "hps_parse": deepcopy(row.get("hps_parse")),
            }
        )
    return result, None


def _parse_log(value: Any, *, label: str) -> dict[str, Any]:
    log = _mapping(value, label=label)
    instance_id = _instance_id(log.get("id"), label=f"{label}.id")
    slug = _text(log.get("slug"), label=f"{label}.slug")
    name = _text(log.get("name"), label=f"{label}.name")
    boss_kills = log.get("boss_kills")
    if isinstance(boss_kills, bool) or not isinstance(boss_kills, int) or boss_kills < 0:
        raise CharacterHistoryError(f"{label}.boss_kills must be a non-negative integer")
    performance, performance_provenance = _parse_performance(
        (
            log["performance"]
            if "performance" in log
            else _MISSING_PERFORMANCE
        ),
        label=f"{label}.performance",
    )
    result = {
        "instance_id": instance_id,
        "slug": slug,
        "name": name,
        "guild": _parse_guild(log.get("guild"), label=f"{label}.guild"),
        "boss_kills": boss_kills,
        "started_at": _timestamp(log.get("started_at"), label=f"{label}.started_at"),
        "ended_at": _timestamp(
            log.get("ended_at"), label=f"{label}.ended_at", allow_none=True
        ),
        "uploaded_at": _timestamp(
            log.get("uploaded_at"), label=f"{label}.uploaded_at"
        ),
        "performance": performance,
    }
    if performance_provenance is not None:
        result["performance_provenance"] = {
            **performance_provenance,
            "raw_page_lookup": {
                "log_id": instance_id,
                "field": "performance",
            },
        }
    return result


def _parse_page(
    value: Any,
    *,
    query: CharacterQuery,
    identity: Mapping[str, Any],
    requested_page: int,
    requested_page_size: int,
    label: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    page = _mapping(value, label=label)
    for field in ("character", "logs", "pagination"):
        if field not in page:
            raise CharacterHistoryError(f"{label} lacks required field {field}")
    page_identity = _parse_identity(page["character"], query=query, label=f"{label}.character")
    if _identity_key(page_identity) != _identity_key(identity):
        raise CharacterHistoryError(f"{label} character identity/GUID conflicts with identity endpoint")
    pagination = _mapping(page["pagination"], label=f"{label}.pagination")
    actual_page = pagination.get("page")
    actual_page_size = pagination.get("page_size")
    has_more = pagination.get("has_more")
    if isinstance(actual_page, bool) or actual_page != requested_page:
        raise CharacterHistoryError(f"{label} pagination.page does not match request")
    if isinstance(actual_page_size, bool) or actual_page_size != requested_page_size:
        raise CharacterHistoryError(f"{label} pagination.page_size does not match request")
    if not isinstance(has_more, bool):
        raise CharacterHistoryError(f"{label} pagination.has_more must be boolean")
    logs = [
        _parse_log(row, label=f"{label}.logs[{index}]")
        for index, row in enumerate(_list(page["logs"], label=f"{label}.logs"))
    ]
    return {
        "page": requested_page,
        "page_size": requested_page_size,
        "has_more": has_more,
    }, logs


@dataclass(frozen=True)
class _RawResponse:
    url: str
    body: bytes


@dataclass(frozen=True)
class _FetchedCharacter:
    query: CharacterQuery
    identity: dict[str, Any]
    identity_response: _RawResponse
    pages: tuple[tuple[dict[str, Any], _RawResponse], ...]
    instances: tuple[dict[str, Any], ...]


def _validate_bounds(
    *, page_size: int, max_pages: int, max_instances: int
) -> None:
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 50:
        raise CharacterHistorySafetyLimitError("page_size must be in 1..50")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise CharacterHistorySafetyLimitError("max_pages must be positive")
    if (
        isinstance(max_instances, bool)
        or not isinstance(max_instances, int)
        or max_instances < 1
    ):
        raise CharacterHistorySafetyLimitError("max_instances must be positive")


def _normalize_queries(queries: Sequence[CharacterQuery]) -> list[CharacterQuery]:
    if not isinstance(queries, Sequence) or isinstance(queries, (str, bytes)):
        raise CharacterHistoryError("queries must be a sequence of CharacterQuery")
    normalized: set[CharacterQuery] = set()
    for query in queries:
        if not isinstance(query, CharacterQuery):
            raise CharacterHistoryError("queries must contain CharacterQuery values")
        normalized.add(query)
    if not normalized:
        raise CharacterHistoryError("at least one character query is required")
    return sorted(normalized)


def _decode_response_json(
    response: Any,
    value: Any,
    *,
    label: str,
    expected_url: str,
) -> tuple[_RawResponse, Any]:
    if not isinstance(response.url, str) or not response.url:
        raise CharacterHistoryError(f"{label} response lacks request URL")
    if response.url != expected_url:
        raise CharacterHistoryError(
            f"{label} final response URL does not match the exact requested URL"
        )
    if not isinstance(response.body, bytes):
        raise CharacterHistoryError(f"{label} response body must be bytes")
    # ChronicleClient already decoded ``value`` from these bytes.  Re-decode
    # once here so a custom/malformed client cannot make stored bytes disagree
    # with the validated representation.
    try:
        decoded = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error(f"{label} response is not valid UTF-8 JSON: {error}", error)
    if decoded != value:
        raise CharacterHistoryError(f"{label} decoded value disagrees with response bytes")
    return _RawResponse(response.url, response.body), decoded


def _fetch_character_histories(
    queries: Sequence[CharacterQuery],
    *,
    client: ingest_v1.ChronicleClient,
    page_size: int,
    max_pages: int,
    max_instances: int,
) -> list[_FetchedCharacter]:
    _validate_bounds(
        page_size=page_size, max_pages=max_pages, max_instances=max_instances
    )
    normalized_queries = _normalize_queries(queries)
    output: list[_FetchedCharacter] = []
    guid_owners: dict[str, CharacterQuery] = {}
    total_unique_ids: set[str] = set()

    for query in normalized_queries:
        identity_url = f"{client.api_base}{query.api_path}"
        try:
            identity_response, identity_value = client.get_json(query.api_path)
        except ingest_v1.ChronicleIngestError as error:
            raise _error(f"failed character identity request for {query}: {error}", error)
        raw_identity, decoded_identity = _decode_response_json(
            identity_response,
            identity_value,
            label=f"identity {query.character}",
            expected_url=identity_url,
        )
        identity = _parse_identity(
            decoded_identity, query=query, label=f"identity {query.character}"
        )
        guid_key = str(identity["guid"]).lower()
        previous_query = guid_owners.get(guid_key)
        if previous_query is not None and previous_query != query:
            raise CharacterHistoryError(
                "the same character GUID was returned for conflicting player queries"
            )
        guid_owners[guid_key] = query

        page_records: list[tuple[dict[str, Any], _RawResponse]] = []
        instances_by_id: dict[str, dict[str, Any]] = {}
        instance_ids_by_slug: dict[str, str] = {}
        page_number = 1
        while True:
            page_url = (
                f"{client.api_base}{query.api_path}/instances"
                f"?page={page_number}&page_size={page_size}"
            )
            try:
                page_response, page_value = client.get_json(
                    f"{query.api_path}/instances",
                    params=(("page", page_number), ("page_size", page_size)),
                )
            except ingest_v1.ChronicleIngestError as error:
                raise _error(
                    f"failed character instances page {page_number} for {query}: {error}",
                    error,
                )
            raw_page, decoded_page = _decode_response_json(
                page_response,
                page_value,
                label=f"instances {query.character} page {page_number}",
                expected_url=page_url,
            )
            pagination, logs = _parse_page(
                decoded_page,
                query=query,
                identity=identity,
                requested_page=page_number,
                requested_page_size=page_size,
                label=f"instances {query.character} page {page_number}",
            )
            page_records.append((pagination, raw_page))
            for log in logs:
                instance_id = str(log["instance_id"])
                existing = instances_by_id.get(instance_id)
                if existing is not None and existing != log:
                    raise CharacterHistoryError(
                        f"duplicate instance {instance_id} has conflicting records"
                    )
                slug = str(log["slug"])
                previous_id = instance_ids_by_slug.get(slug)
                if previous_id is not None and previous_id != instance_id:
                    raise CharacterHistoryError(
                        f"duplicate instance slug {slug!r} maps to conflicting ids"
                    )
                instances_by_id[instance_id] = log
                instance_ids_by_slug[slug] = instance_id
                total_unique_ids.add(instance_id)
                if len(total_unique_ids) > max_instances:
                    raise CharacterHistorySafetyLimitError(
                        f"character history exceeds max_instances={max_instances}"
                    )
            if not pagination["has_more"]:
                break
            if page_number >= max_pages:
                raise CharacterHistorySafetyLimitError(
                    f"character history still has more pages after max_pages={max_pages}"
                )
            page_number += 1

        instances = sorted(
            instances_by_id.values(),
            key=lambda row: (
                -_timestamp_key(row.get("uploaded_at")),
                -_timestamp_key(row.get("started_at")),
                str(row["instance_id"]),
            ),
        )
        output.append(
            _FetchedCharacter(
                query=query,
                identity=identity,
                identity_response=raw_identity,
                pages=tuple(page_records),
                instances=tuple(instances),
            )
        )
    return output


def _contamination_for_log(
    log: Mapping[str, Any],
    *,
    implementation_revision: str = IMPLEMENTATION_REVISION,
) -> str:
    """Apply only the frozen raid-guild plus raid-start-time rule.

    Character/player names are deliberately ignored.  A character may belong
    to a guild now without every historical raid carrying that guild context;
    the log-level guild is the only guild evidence used here.
    """

    guild = log.get("guild")
    guild_name: str | None = None
    if isinstance(guild, dict) and isinstance(guild.get("name"), str):
        stripped = guild["name"].strip()
        guild_name = stripped or None
    started_at = log.get("started_at")
    if implementation_revision == LEGACY_IMPLEMENTATION_REVISION:
        classifier = ingest_v1.classify_range_bug_legacy_v1
    elif implementation_revision == IMPLEMENTATION_REVISION:
        classifier = ingest_v1.classify_range_bug
    else:
        raise CharacterHistoryError(
            "unsupported character history implementation revision"
        )
    return classifier(
        guild_name,
        started_at if isinstance(started_at, str) else None,
    )


def _capture_contract() -> dict[str, Any]:
    return {
        "authoritative_instance_id_source": "character_instances.id",
        "leaderboard_instance_id_used": False,
        "leaderboard_zero_id_rejected_as_identity": True,
        "pagination": {
            "complete_until_has_more_false": True,
            "page_size_max": 50,
            "max_pages_fail_closed": True,
        },
        "identity_validation": (
            "exact character GUID/name/server/realm across identity and every page"
        ),
        "duplicate_policy": (
            "same id and identical normalized record deduplicated; conflicts rejected"
        ),
        "instance_order": "uploaded_at descending, started_at descending, instance_id",
        "manifest_publication": "raw objects first; content-addressed manifest last",
    }


def _contamination_contract(
    implementation_revision: str = IMPLEMENTATION_REVISION,
) -> dict[str, Any]:
    if implementation_revision == LEGACY_IMPLEMENTATION_REVISION:
        boundary = {
            "cutoff_local": ingest_v1.LEGACY_RANGE_BUG_CUTOFF_LOCAL,
        }
        labels = [
            ingest_v1.SUSPECT_36YD_RANGE_BUG,
            ingest_v1.POSTFIX_KNOWN_CLEAN,
            ingest_v1.NO_KNOWN_RULE_MATCH,
            ingest_v1.UNKNOWN_NONVOTING,
        ]
        nontraining_labels = {
            ingest_v1.SUSPECT_36YD_RANGE_BUG,
            ingest_v1.UNKNOWN_NONVOTING,
        }
    elif implementation_revision == IMPLEMENTATION_REVISION:
        boundary = ingest_v1.range_bug_boundary_contract()
        labels = [
            ingest_v1.SUSPECT_36YD_RANGE_BUG,
            ingest_v1.RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
            ingest_v1.POSTFIX_KNOWN_CLEAN,
            ingest_v1.NO_KNOWN_RULE_MATCH,
            ingest_v1.UNKNOWN_NONVOTING,
        ]
        nontraining_labels = NONTRAINING_LABELS
    else:
        raise CharacterHistoryError(
            "unsupported character history implementation revision"
        )
    return {
        "scope": "raid/log level discovery diagnostic only",
        "guild_field": "character instances log.guild.name",
        "started_at_field": "character instances log.started_at",
        "player_name_used": False,
        "character_current_guild_used": False,
        **boundary,
        "timezone": ingest_v1.RANGE_BUG_CUTOFF_TIMEZONE,
        "labels": labels,
        "training_candidate_labels": sorted(TRAINING_CANDIDATE_LABELS),
        "nontraining_labels": sorted(nontraining_labels),
    }


def _derived_instance(
    log: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    implementation_revision: str = IMPLEMENTATION_REVISION,
) -> dict[str, Any]:
    row = deepcopy(dict(log))
    row["character_guid"] = identity["guid"]
    row["character_name"] = identity["name"]
    row["contamination_label"] = _contamination_for_log(
        row,
        implementation_revision=implementation_revision,
    )
    row["performance_contract"] = {
        "coverage_lane": "parse",
        "exact_damage_duration_dps": False,
    }
    return row


def _materialize_character(
    fetched: _FetchedCharacter,
    *,
    identity_object: Mapping[str, Any],
    page_objects: Sequence[Mapping[str, Any]],
    implementation_revision: str = IMPLEMENTATION_REVISION,
) -> dict[str, Any]:
    if len(page_objects) != len(fetched.pages):
        raise CharacterHistoryError("page object count disagrees with fetched pages")
    pages: list[dict[str, Any]] = []
    for index, ((pagination, raw), object_ref) in enumerate(
        zip(fetched.pages, page_objects), start=1
    ):
        pages.append(
            {
                "page": pagination["page"],
                "page_size": pagination["page_size"],
                "has_more": pagination["has_more"],
                "request_url": raw.url,
                "object": deepcopy(dict(object_ref)),
            }
        )
    return {
        "query": fetched.query.to_dict(),
        "identity": deepcopy(fetched.identity),
        "identity_response": {
            "request_url": fetched.identity_response.url,
            "object": deepcopy(dict(identity_object)),
        },
        "pages": pages,
        "page_count": len(pages),
        "instances": [
            _derived_instance(
                log,
                fetched.identity,
                implementation_revision=implementation_revision,
            )
            for log in fetched.instances
        ],
    }


def _manifest_core(
    characters: Sequence[Mapping[str, Any]],
    *,
    api_base: str,
    implementation_revision: str = IMPLEMENTATION_REVISION,
) -> dict[str, Any]:
    unique_instance_ids = {
        str(instance["instance_id"])
        for character in characters
        for instance in character["instances"]
    }
    return {
        "schema": SCHEMA,
        "implementation_revision": implementation_revision,
        "kind": KIND,
        "api_base": api_base,
        "capture_contract": _capture_contract(),
        "contamination_contract": _contamination_contract(
            implementation_revision
        ),
        "characters": [deepcopy(dict(row)) for row in characters],
        "summary": {
            "character_count": len(characters),
            "page_count": sum(int(row["page_count"]) for row in characters),
            "character_instance_membership_count": sum(
                len(row["instances"]) for row in characters
            ),
            "unique_instance_count": len(unique_instance_ids),
        },
    }


def list_character_histories(
    queries: Sequence[CharacterQuery],
    *,
    client: ingest_v1.ChronicleClient,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_instances: int = DEFAULT_MAX_INSTANCES,
) -> dict[str, Any]:
    """Read and validate complete histories without touching the filesystem."""

    fetched = _fetch_character_histories(
        queries,
        client=client,
        page_size=page_size,
        max_pages=max_pages,
        max_instances=max_instances,
    )
    characters = [
        {
            "query": row.query.to_dict(),
            "identity": deepcopy(row.identity),
            "page_count": len(row.pages),
            "instances": [
                _derived_instance(log, row.identity) for log in row.instances
            ],
        }
        for row in fetched
    ]
    unique_ids = {
        instance["instance_id"]
        for character in characters
        for instance in character["instances"]
    }
    return {
        "schema": f"{SCHEMA}/list",
        "write_performed": False,
        "characters": characters,
        "summary": {
            "character_count": len(characters),
            "page_count": sum(row["page_count"] for row in characters),
            "character_instance_membership_count": sum(
                len(row["instances"]) for row in characters
            ),
            "unique_instance_count": len(unique_ids),
        },
        "capture_contract": _capture_contract(),
        "contamination_contract": _contamination_contract(),
    }


def capture_character_histories(
    queries: Sequence[CharacterQuery],
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    client: ingest_v1.ChronicleClient,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_instances: int = DEFAULT_MAX_INSTANCES,
) -> dict[str, Any]:
    """Capture immutable raw identity/pages and publish one addressed manifest.

    Every network response is fetched and validated before ``RawObjectStore``
    is instantiated.  A pagination/safety failure therefore publishes neither
    raw objects nor a misleading partial manifest.
    """

    fetched = _fetch_character_histories(
        queries,
        client=client,
        page_size=page_size,
        max_pages=max_pages,
        max_instances=max_instances,
    )
    try:
        store = ingest_v1.RawObjectStore(Path(data_root))
        characters: list[dict[str, Any]] = []
        for row in fetched:
            identity_ref = store.put(
                row.identity_response.body,
                suffix="character.identity.json",
                media_type="application/json",
            )
            page_refs = [
                store.put(
                    raw.body,
                    suffix=f"character.instances.page-{pagination['page']}.json",
                    media_type="application/json",
                )
                for pagination, raw in row.pages
            ]
            characters.append(
                _materialize_character(
                    row,
                    identity_object=identity_ref,
                    page_objects=page_refs,
                )
            )
        document = _content_addressed(
            _manifest_core(characters, api_base=client.api_base)
        )
        digest = _verify_content_address(document, label="character history manifest")
        payload = _canonical_document_bytes(document)
        manifest_path = (
            store.root
            / "character_history_manifests"
            / f"chronicle_external_character_history_v1.{digest}.manifest.json"
        )
        ingest_v1._atomic_write_bytes(manifest_path, payload)
    except CharacterHistoryError:
        raise
    except ingest_v1.ChronicleIngestError as error:
        raise _error(f"cannot publish character history capture: {error}", error)
    return {
        "schema": SCHEMA,
        "content_sha256": digest,
        "manifest_path": str(manifest_path),
        "character_count": document["summary"]["character_count"],
        "unique_instance_count": document["summary"]["unique_instance_count"],
    }


def _json_from_bytes(data: bytes, *, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error(f"{label} is not valid UTF-8 JSON: {error}", error)


def _raw_root(data_root: Path) -> Path:
    try:
        return ingest_v1._ensure_offline_root(Path(data_root))
    except ingest_v1.ChronicleIngestError as error:
        raise _error(str(error), error)


def _read_ref(raw_root: Path, reference: Any, *, label: str) -> bytes:
    try:
        return ingest_v1._read_object_reference(raw_root, reference, label=label)
    except ingest_v1.ChronicleIngestError as error:
        raise _error(str(error), error)


def _resolved_manifest_path(
    path: str | Path,
    *,
    raw_root: Path,
    directory_name: str,
) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise CharacterHistoryError("manifest path must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise _error(f"cannot resolve manifest {requested}: {error}", error)
    allowed = (raw_root / directory_name).resolve()
    if resolved.parent != allowed:
        raise CharacterHistoryError(
            f"manifest path escapes the expected {directory_name} directory"
        )
    return resolved


def _query_from_manifest(value: Any, *, label: str) -> CharacterQuery:
    query = _mapping(value, label=label)
    if set(query) != {"server", "realm", "character"}:
        raise CharacterHistoryError(f"{label} must contain only server/realm/character")
    return CharacterQuery(
        server=query["server"], realm=query["realm"], character=query["character"]
    )


def _response_wrapper(value: Any, *, label: str) -> tuple[str, Mapping[str, Any]]:
    wrapper = _mapping(value, label=label)
    if set(wrapper) != {"request_url", "object"}:
        raise CharacterHistoryError(f"{label} has an invalid wrapper schema")
    url = _text(wrapper.get("request_url"), label=f"{label}.request_url")
    obj = _mapping(wrapper.get("object"), label=f"{label}.object")
    assert url is not None
    return url, obj


def _replay_manifest_character(
    value: Any,
    *,
    index: int,
    raw_root: Path,
    api_base: str,
    implementation_revision: str,
    validate_declared: bool = True,
) -> dict[str, Any]:
    character = _mapping(value, label=f"characters[{index}]")
    query = _query_from_manifest(character.get("query"), label=f"characters[{index}].query")
    identity_url, identity_ref = _response_wrapper(
        character.get("identity_response"),
        label=f"characters[{index}].identity_response",
    )
    expected_identity_url = f"{api_base}{query.api_path}"
    if identity_url != expected_identity_url:
        raise CharacterHistoryError(
            f"characters[{index}] identity request URL does not match its query"
        )
    identity_body = _read_ref(
        raw_root, identity_ref, label=f"characters[{index}].identity_response"
    )
    identity = _parse_identity(
        _json_from_bytes(identity_body, label=f"characters[{index}] identity raw object"),
        query=query,
        label=f"characters[{index}].identity_raw",
    )
    declared_identity = character.get("identity")
    if declared_identity != identity:
        raise CharacterHistoryError(
            f"characters[{index}] derived identity disagrees with raw object"
        )

    page_values = _list(character.get("pages"), label=f"characters[{index}].pages")
    if not page_values:
        raise CharacterHistoryError(f"characters[{index}] must contain at least one page")
    fetched_pages: list[tuple[dict[str, Any], _RawResponse]] = []
    instances_by_id: dict[str, dict[str, Any]] = {}
    slugs: dict[str, str] = {}
    page_refs: list[Mapping[str, Any]] = []
    for page_index, raw_wrapper in enumerate(page_values, start=1):
        wrapper = _mapping(raw_wrapper, label=f"characters[{index}].pages[{page_index - 1}]")
        expected_keys = {"page", "page_size", "has_more", "request_url", "object"}
        if set(wrapper) != expected_keys:
            raise CharacterHistoryError(
                f"characters[{index}].pages[{page_index - 1}] has an invalid schema"
            )
        page_number = wrapper.get("page")
        page_size = wrapper.get("page_size")
        if isinstance(page_number, bool) or page_number != page_index:
            raise CharacterHistoryError(f"characters[{index}] page sequence is invalid")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 50:
            raise CharacterHistoryError(f"characters[{index}] page_size is invalid")
        url = _text(
            wrapper.get("request_url"),
            label=f"characters[{index}].pages[{page_index - 1}].request_url",
        )
        expected_page_url = (
            f"{api_base}{query.api_path}/instances"
            f"?page={page_index}&page_size={page_size}"
        )
        if url != expected_page_url:
            raise CharacterHistoryError(
                f"characters[{index}] page request URL does not match query/pagination"
            )
        ref = _mapping(
            wrapper.get("object"),
            label=f"characters[{index}].pages[{page_index - 1}].object",
        )
        body = _read_ref(
            raw_root,
            ref,
            label=f"characters[{index}].pages[{page_index - 1}]",
        )
        pagination, logs = _parse_page(
            _json_from_bytes(body, label=f"characters[{index}] page {page_index} raw object"),
            query=query,
            identity=identity,
            requested_page=page_index,
            requested_page_size=page_size,
            label=f"characters[{index}].page_raw[{page_index}]",
        )
        if wrapper.get("has_more") is not pagination["has_more"]:
            raise CharacterHistoryError(f"characters[{index}] page has_more disagrees with raw object")
        if page_index < len(page_values) and not pagination["has_more"]:
            raise CharacterHistoryError(f"characters[{index}] has pages after has_more=false")
        if page_index == len(page_values) and pagination["has_more"]:
            raise CharacterHistoryError(f"characters[{index}] capture is pagination-incomplete")
        assert url is not None
        fetched_pages.append((pagination, _RawResponse(url=url, body=body)))
        page_refs.append(ref)
        for log in logs:
            instance_id = str(log["instance_id"])
            old = instances_by_id.get(instance_id)
            if old is not None and old != log:
                raise CharacterHistoryError(
                    f"duplicate instance {instance_id} conflicts while replaying manifest"
                )
            slug = str(log["slug"])
            old_id = slugs.get(slug)
            if old_id is not None and old_id != instance_id:
                raise CharacterHistoryError(
                    f"duplicate instance slug {slug!r} conflicts while replaying manifest"
                )
            instances_by_id[instance_id] = log
            slugs[slug] = instance_id

    ordered = sorted(
        instances_by_id.values(),
        key=lambda row: (
            -_timestamp_key(row.get("uploaded_at")),
            -_timestamp_key(row.get("started_at")),
            str(row["instance_id"]),
        ),
    )
    fetched = _FetchedCharacter(
        query=query,
        identity=identity,
        identity_response=_RawResponse(identity_url, identity_body),
        pages=tuple(fetched_pages),
        instances=tuple(ordered),
    )
    expected = _materialize_character(
        fetched,
        identity_object=identity_ref,
        page_objects=page_refs,
        implementation_revision=implementation_revision,
    )
    if validate_declared and dict(character) != expected:
        raise CharacterHistoryError(
            f"characters[{index}] derived fields disagree with raw response closure"
        )
    return expected


def load_character_history_manifest(
    path: str | Path,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
) -> tuple[dict[str, Any], Path]:
    """Load, hash-check, and replay every raw response in a capture manifest."""

    raw_root = _raw_root(Path(data_root))
    resolved = _resolved_manifest_path(
        path, raw_root=raw_root, directory_name="character_history_manifests"
    )
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise _error(f"cannot read character history manifest {resolved}: {error}", error)
    value = _json_from_bytes(payload, label="character history manifest")
    manifest = _mapping(value, label="character history manifest")
    if payload != _canonical_document_bytes(manifest):
        raise CharacterHistoryError("character history manifest is not canonical JSON")
    digest = _verify_content_address(manifest, label="character history manifest")
    expected_name = f"chronicle_external_character_history_v1.{digest}.manifest.json"
    if resolved.name != expected_name:
        raise CharacterHistoryError("character history manifest filename/content hash mismatch")
    if manifest.get("schema") != SCHEMA:
        raise CharacterHistoryError("character history manifest schema is unsupported")
    implementation_revision = manifest.get("implementation_revision")
    if implementation_revision not in SUPPORTED_IMPLEMENTATION_REVISIONS:
        raise CharacterHistoryError("character history implementation revision is unsupported")
    if manifest.get("kind") != KIND:
        raise CharacterHistoryError("character history manifest kind is unsupported")
    if manifest.get("capture_contract") != _capture_contract():
        raise CharacterHistoryError("character history capture contract is invalid")
    if manifest.get("contamination_contract") != _contamination_contract(
        str(implementation_revision)
    ):
        raise CharacterHistoryError("character history contamination contract is invalid")
    api_base = _text(manifest.get("api_base"), label="manifest.api_base")
    assert api_base is not None
    if not api_base.lower().startswith("https://") or api_base.endswith("/"):
        raise CharacterHistoryError("character history api_base must be canonical HTTPS")
    try:
        ingest_v1._verify_manifest_object_closure(
            manifest, raw_root, label="character_history_manifest"
        )
    except ingest_v1.ChronicleIngestError as error:
        raise _error(str(error), error)

    characters_raw = _list(manifest.get("characters"), label="manifest.characters")
    replayed = [
        _replay_manifest_character(
            row,
            index=index,
            raw_root=raw_root,
            api_base=api_base,
            implementation_revision=str(implementation_revision),
        )
        for index, row in enumerate(characters_raw)
    ]
    if replayed != sorted(
        replayed,
        key=lambda row: (
            row["query"]["server"],
            row["query"]["realm"],
            row["query"]["character"],
        ),
    ):
        raise CharacterHistoryError("character history manifest queries are not stable-sorted")
    query_keys = [tuple(row["query"].values()) for row in replayed]
    if len(query_keys) != len(set(query_keys)):
        raise CharacterHistoryError("character history manifest has duplicate queries")
    guid_keys = [str(row["identity"]["guid"]).lower() for row in replayed]
    if len(guid_keys) != len(set(guid_keys)):
        raise CharacterHistoryError("character history manifest has conflicting duplicate GUIDs")
    expected_core = _manifest_core(
        replayed,
        api_base=api_base,
        implementation_revision=str(implementation_revision),
    )
    actual_core = {key: child for key, child in manifest.items() if key != "content_address"}
    if actual_core != expected_core:
        raise CharacterHistoryError("character history manifest summary/contract mismatch")
    return deepcopy(dict(manifest)), resolved


def replay_character_history_manifest_from_local_raw(
    source_manifest_path: str | Path,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    """Migrate a verified legacy capture to the current label contract locally.

    The source manifest and every identity/page object are hash-checked through
    the normal revision-aware loader.  Only a new content-addressed manifest is
    written; immutable raw identity and page objects are reused byte-for-byte.
    """

    source, resolved = load_character_history_manifest(
        source_manifest_path,
        data_root=Path(data_root),
    )
    source_revision = str(source["implementation_revision"])
    source_sha = _verify_content_address(
        source, label="source character history manifest"
    )
    raw_root = _raw_root(Path(data_root))
    api_base = _text(source.get("api_base"), label="manifest.api_base")
    assert api_base is not None
    characters = [
        _replay_manifest_character(
            row,
            index=index,
            raw_root=raw_root,
            api_base=api_base,
            implementation_revision=IMPLEMENTATION_REVISION,
            validate_declared=False,
        )
        for index, row in enumerate(
            _list(source.get("characters"), label="manifest.characters")
        )
    ]
    document = _content_addressed(
        _manifest_core(
            characters,
            api_base=api_base,
            implementation_revision=IMPLEMENTATION_REVISION,
        )
    )
    digest = _verify_content_address(
        document, label="replayed character history manifest"
    )
    payload = _canonical_document_bytes(document)
    destination = (
        raw_root
        / "character_history_manifests"
        / f"chronicle_external_character_history_v1.{digest}.manifest.json"
    )
    try:
        ingest_v1._atomic_write_bytes(destination, payload)
    except ingest_v1.ChronicleIngestError as error:
        raise _error(
            f"cannot publish replayed character history manifest: {error}", error
        )
    return {
        "status": (
            "CURRENT_REVISION_REPLAYED"
            if source_revision == IMPLEMENTATION_REVISION
            else "LEGACY_REVISION_MIGRATED_TO_CURRENT"
        ),
        "source_manifest_path": str(resolved),
        "source_manifest_sha256": source_sha,
        "source_implementation_revision": source_revision,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "manifest_sha256": digest,
        "manifest_path": str(destination),
        "character_count": document["summary"]["character_count"],
        "unique_instance_count": document["summary"]["unique_instance_count"],
        "network_requests_made": 0,
        "raw_objects_written": 0,
        "source_manifest_mutated": False,
    }


@dataclass
class _IngestEvidence:
    instance_id: str
    slug: str | None
    instance_name: str | None
    started_at: str | None
    metadata_available: bool
    ranking_objects_available: int
    ranking_rows: list[dict[str, Any]]
    available_streams: set[str]
    unavailable_404_streams: set[str]
    stream_capture_statuses: dict[str, set[str]]
    available_stream_object_sha256: dict[str, str]
    source_manifest_sha256: set[str]


def _strict_ingest_manifest(
    path: str | Path,
    *,
    raw_root: Path,
    expected_implementation_revision: str,
    expected_parser_contract_revision: str,
) -> tuple[dict[str, Any], str, Path]:
    resolved = _resolved_manifest_path(
        path, raw_root=raw_root, directory_name="manifests"
    )
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise _error(f"cannot read ingest manifest {resolved}: {error}", error)
    value = _json_from_bytes(payload, label="ingest manifest")
    manifest = _mapping(value, label="ingest manifest")
    if payload != ingest_v1._canonical_json_bytes(manifest):
        raise CharacterHistoryError("ingest manifest is not canonical JSON")
    digest = _sha256(payload)
    if resolved.name != f"{digest}.json":
        raise CharacterHistoryError("ingest manifest filename/content hash mismatch")
    if manifest.get("schema") != ingest_v1.SCHEMA:
        raise CharacterHistoryError("ingest manifest schema is unsupported")
    if manifest.get("implementation_revision") != expected_implementation_revision:
        raise CharacterHistoryError("ingest manifest implementation revision is unsupported")
    if manifest.get("parser_contract_revision") != expected_parser_contract_revision:
        raise CharacterHistoryError("ingest manifest parser contract revision is unsupported")
    if manifest.get("kind") != "chronicle_external_api_raw_snapshot":
        raise CharacterHistoryError("ingest manifest kind is unsupported")
    try:
        ingest_v1._verify_manifest_object_closure(
            manifest, raw_root, label="ingest_manifest"
        )
    except ingest_v1.ChronicleIngestError as error:
        raise _error(str(error), error)
    _list(manifest.get("instances"), label="ingest manifest.instances")
    return deepcopy(dict(manifest)), digest, resolved


def _nullable_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label=label)


def _finite_number(
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
        raise CharacterHistoryError(f"{label} must be a finite number")
    number = float(value)
    if (strictly_positive and number <= minimum) or (
        not strictly_positive and number < minimum
    ):
        comparator = ">" if strictly_positive else ">="
        raise CharacterHistoryError(f"{label} must be {comparator} {minimum}")
    return number


def _ranking_rows(
    raw_root: Path,
    wrapper: Any,
    *,
    label: str,
) -> tuple[int, list[dict[str, Any]]]:
    if wrapper is None:
        return 0, []
    ranking = _mapping(wrapper, label=label)
    ref = ranking.get("object")
    if not isinstance(ref, dict):
        raise CharacterHistoryError(f"{label} lacks an object reference")
    body = _read_ref(raw_root, ref, label=label)
    value = _json_from_bytes(body, label=f"{label} object")
    rows = _list(value, label=f"{label} object")
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        item = _mapping(row, label=f"{label}[{index}]")
        normalized.append(deepcopy(dict(item)))
    declared_count = ranking.get("record_count")
    if declared_count is not None and (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(normalized)
    ):
        raise CharacterHistoryError(f"{label}.record_count disagrees with raw object")
    return 1, normalized


def _metadata_available(
    raw_root: Path,
    wrapper: Any,
    *,
    instance_id: str,
    slug: str | None,
    label: str,
) -> bool:
    if wrapper is None:
        return False
    metadata = _mapping(wrapper, label=label)
    ref = metadata.get("object")
    if not isinstance(ref, dict):
        raise CharacterHistoryError(f"{label} lacks an object reference")
    body = _read_ref(raw_root, ref, label=label)
    value = _mapping(_json_from_bytes(body, label=f"{label} object"), label=f"{label} object")
    metadata_id = value.get("id")
    if metadata_id is not None:
        parsed_id = _instance_id(metadata_id, label=f"{label}.id")
        if parsed_id != instance_id:
            raise CharacterHistoryError(f"{label} id conflicts with ingest instance row")
    metadata_slug = value.get("slug")
    if metadata_slug is not None and slug is not None:
        parsed_slug = _text(metadata_slug, label=f"{label}.slug")
        if parsed_slug != slug:
            raise CharacterHistoryError(f"{label} slug conflicts with ingest instance row")
    return True


def _ingest_evidence_rows(
    manifests: Sequence[str | Path],
    *,
    raw_root: Path,
    expected_implementation_revision: str,
    expected_parser_contract_revision: str,
) -> tuple[list[_IngestEvidence], list[str]]:
    loaded: list[tuple[dict[str, Any], str]] = []
    seen_manifest_sha: set[str] = set()
    for path in manifests:
        manifest, digest, _ = _strict_ingest_manifest(
            path,
            raw_root=raw_root,
            expected_implementation_revision=expected_implementation_revision,
            expected_parser_contract_revision=expected_parser_contract_revision,
        )
        if digest not in seen_manifest_sha:
            loaded.append((manifest, digest))
            seen_manifest_sha.add(digest)

    by_id: dict[str, _IngestEvidence] = {}
    for manifest, digest in sorted(loaded, key=lambda item: item[1]):
        for index, raw_row in enumerate(manifest["instances"]):
            row = _mapping(raw_row, label=f"ingest {digest}.instances[{index}]")
            instance_id = _instance_id(
                row.get("instance_id"),
                label=f"ingest {digest}.instances[{index}].instance_id",
            )
            slug = _nullable_text(
                row.get("slug"), label=f"ingest {digest}.instances[{index}].slug"
            )
            instance_name = _nullable_text(
                row.get("instance_name"),
                label=f"ingest {digest}.instances[{index}].instance_name",
            )
            started_at = _timestamp(
                row.get("started_at"),
                label=f"ingest {digest}.instances[{index}].started_at",
                allow_none=True,
            )
            metadata = _metadata_available(
                raw_root,
                row.get("metadata"),
                instance_id=instance_id,
                slug=slug,
                label=f"ingest {digest}.instances[{index}].metadata",
            )
            ranking_count, rankings = _ranking_rows(
                raw_root,
                row.get("ranking_records"),
                label=f"ingest {digest}.instances[{index}].ranking_records",
            )
            streams_raw = _mapping(
                row.get("streams", {}),
                label=f"ingest {digest}.instances[{index}].streams",
            )
            available_streams: set[str] = set()
            unavailable_404_streams: set[str] = set()
            stream_capture_statuses: dict[str, set[str]] = {}
            available_stream_object_sha256: dict[str, str] = {}
            for stream_name, stream_value in streams_raw.items():
                if stream_name not in ingest_v1.EVENT_STREAM_TYPES:
                    raise CharacterHistoryError(
                        f"ingest {digest} contains unknown stream {stream_name!r}"
                    )
                stream = _mapping(
                    stream_value,
                    label=f"ingest {digest}.instances[{index}].streams.{stream_name}",
                )
                status = stream.get("status")
                if status == "AVAILABLE":
                    object_ref = stream.get("object")
                    if not isinstance(object_ref, dict):
                        raise CharacterHistoryError(
                            f"available stream {stream_name} lacks object evidence"
                        )
                    # Object closure has already verified bytes/hash.  Decoding is
                    # not needed to answer the coverage question.
                    available_streams.add(stream_name)
                    available_stream_object_sha256[stream_name] = str(
                        object_ref["sha256"]
                    )
                elif status == "UNAVAILABLE_404":
                    if stream.get("object") is not None:
                        raise CharacterHistoryError(
                            f"unavailable stream {stream_name} must not have an object"
                        )
                    unavailable_404_streams.add(stream_name)
                else:
                    raise CharacterHistoryError(
                        f"ingest stream {stream_name} has unsupported status {status!r}"
                    )
                stream_capture_statuses[stream_name] = {str(status)}

            evidence = _IngestEvidence(
                instance_id=instance_id,
                slug=slug,
                instance_name=instance_name,
                started_at=started_at,
                metadata_available=metadata,
                ranking_objects_available=ranking_count,
                ranking_rows=rankings,
                available_streams=available_streams,
                unavailable_404_streams=unavailable_404_streams,
                stream_capture_statuses=stream_capture_statuses,
                available_stream_object_sha256=available_stream_object_sha256,
                source_manifest_sha256={digest},
            )
            previous = by_id.get(instance_id)
            if previous is None:
                by_id[instance_id] = evidence
                continue
            for field in ("slug", "instance_name", "started_at"):
                old_value = getattr(previous, field)
                new_value = getattr(evidence, field)
                if old_value is not None and new_value is not None and old_value != new_value:
                    raise CharacterHistoryError(
                        f"ingest instance {instance_id} has conflicting {field} values"
                    )
                if old_value is None and new_value is not None:
                    setattr(previous, field, new_value)
            previous.metadata_available = previous.metadata_available or evidence.metadata_available
            previous.ranking_objects_available += evidence.ranking_objects_available
            previous.ranking_rows.extend(evidence.ranking_rows)
            for stream_name, object_sha256 in (
                evidence.available_stream_object_sha256.items()
            ):
                previous_sha256 = previous.available_stream_object_sha256.get(
                    stream_name
                )
                if previous_sha256 is not None and previous_sha256 != object_sha256:
                    raise CharacterHistoryError(
                        f"ingest instance {instance_id} has conflicting AVAILABLE "
                        f"objects for stream {stream_name}"
                    )
                previous.available_stream_object_sha256[stream_name] = object_sha256
            for stream_name, statuses in evidence.stream_capture_statuses.items():
                previous.stream_capture_statuses.setdefault(stream_name, set()).update(
                    statuses
                )
            previous.available_streams.update(evidence.available_streams)
            previous.unavailable_404_streams.update(
                evidence.unavailable_404_streams
            )
            # A later (or simply separately captured) AVAILABLE object is usable
            # evidence and supersedes an older 404.  The observed two-state history
            # remains visible in stream_capture_statuses.
            previous.unavailable_404_streams.difference_update(
                previous.available_streams
            )
            previous.source_manifest_sha256.update(evidence.source_manifest_sha256)
    return sorted(by_id.values(), key=lambda row: row.instance_id), sorted(seen_manifest_sha)


def _history_memberships(
    manifests: Sequence[str | Path],
    *,
    data_root: Path,
    expected_implementation_revision: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    versions_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    manifest_shas: set[str] = set()
    for path in manifests:
        manifest, _ = load_character_history_manifest(path, data_root=data_root)
        if manifest.get("implementation_revision") != expected_implementation_revision:
            raise CharacterHistoryError(
                "character history manifest revision is not valid for this "
                "inventory revision; replay legacy raw history first"
            )
        manifest_sha = _verify_content_address(manifest, label="character history manifest")
        if manifest_sha in manifest_shas:
            continue
        manifest_shas.add(manifest_sha)
        for character in manifest["characters"]:
            identity = character["identity"]
            guid = str(identity["guid"])
            identity_updated_at = identity.get("updated_at")
            for instance in character["instances"]:
                row = deepcopy(instance)
                key = (guid.lower(), str(row["instance_id"]))
                membership_sha = _sha256(_canonical_document_bytes(row))
                versions_by_key.setdefault(key, []).append(
                    {
                        "source_manifest_sha256": manifest_sha,
                        "character_identity_updated_at": identity_updated_at,
                        "query": deepcopy(character["query"]),
                        "membership_sha256": membership_sha,
                        "membership": row,
                    }
                )

    selected_rows: list[dict[str, Any]] = []
    selection_policy = (
        "maximum character identity.updated_at instant; then lexicographically maximum "
        "source manifest sha256 and membership sha256; retain every version"
    )
    for key, raw_versions in versions_by_key.items():
        version_identity: set[tuple[str, str]] = set()
        versions: list[dict[str, Any]] = []
        for version in raw_versions:
            identity_key = (
                str(version["source_manifest_sha256"]),
                str(version["membership_sha256"]),
            )
            if identity_key in version_identity:
                continue
            version_identity.add(identity_key)
            versions.append(version)
        versions.sort(
            key=lambda version: (
                _timestamp_key(version.get("character_identity_updated_at")),
                str(version["source_manifest_sha256"]),
                str(version["membership_sha256"]),
            )
        )
        selected = versions[-1]
        selected_identity = (
            str(selected["source_manifest_sha256"]),
            str(selected["membership_sha256"]),
        )
        audited_versions: list[dict[str, Any]] = []
        for version in versions:
            descriptor = deepcopy(version)
            descriptor["selected"] = (
                str(version["source_manifest_sha256"]),
                str(version["membership_sha256"]),
            ) == selected_identity
            audited_versions.append(descriptor)
        row = deepcopy(selected["membership"])
        row["history_version_audit"] = {
            "player_guid": key[0],
            "instance_id": key[1],
            "selection_policy": selection_policy,
            "version_count": len(audited_versions),
            "distinct_membership_count": len(
                {str(version["membership_sha256"]) for version in versions}
            ),
            "selected_source_manifest_sha256": selected["source_manifest_sha256"],
            "selected_membership_sha256": selected["membership_sha256"],
            "versions": audited_versions,
        }
        selected_rows.append(row)
    return sorted(
        selected_rows,
        key=lambda row: (
            -_timestamp_key(row.get("uploaded_at")),
            str(row["instance_id"]),
            str(row["character_guid"]).lower(),
        ),
    ), sorted(manifest_shas)


def _exact_ranking_records(
    rankings: Iterable[Mapping[str, Any]],
    *,
    character_guid: str,
) -> list[dict[str, Any]]:
    guid_key = character_guid.strip().lower()
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rankings):
        ranking_guid = raw.get("player_guid")
        if not isinstance(ranking_guid, str) or ranking_guid.strip().lower() != guid_key:
            continue
        record_id = _text(raw.get("id"), label=f"ranking[{index}].id")
        assert record_id is not None
        if _is_zero_instance_id(record_id):
            raise CharacterHistoryError(f"ranking[{index}].id must not be zero")
        damage = _finite_number(
            raw.get("damage_done"), label=f"ranking[{index}].damage_done"
        )
        duration = _finite_number(
            raw.get("duration_secs"),
            label=f"ranking[{index}].duration_secs",
            strictly_positive=True,
        )
        dps = _finite_number(raw.get("dps"), label=f"ranking[{index}].dps")
        encounter_id = _nullable_text(
            raw.get("encounter_id"), label=f"ranking[{index}].encounter_id"
        )
        encounter_name = _nullable_text(
            raw.get("encounter_name"), label=f"ranking[{index}].encounter_name"
        )
        player_spec = _nullable_text(
            raw.get("player_spec"), label=f"ranking[{index}].player_spec"
        )
        player_role = _nullable_text(
            raw.get("player_role"), label=f"ranking[{index}].player_role"
        )
        killed_at = _timestamp(
            raw.get("killed_at"),
            label=f"ranking[{index}].killed_at",
            allow_none=True,
        )
        record = {
            "ranking_record_id": record_id,
            "encounter_id": encounter_id,
            "encounter_name": encounter_name,
            "player_spec": player_spec,
            "player_role": player_role,
            "killed_at": killed_at,
            "damage_done": damage,
            "duration_secs": duration,
            "dps": dps,
        }
        previous = by_id.get(record_id)
        if previous is not None and previous != record:
            raise CharacterHistoryError(
                f"exact ranking evidence conflicts for ranking record id {record_id}"
            )
        by_id[record_id] = record
    return [by_id[record_id] for record_id in sorted(by_id)]


def _coverage_for_membership(
    membership: Mapping[str, Any],
    evidence: _IngestEvidence | None,
    *,
    join_method: str,
    required_streams: Sequence[str],
    ranking_negative_evidence: bool,
    stream_negative_evidence: bool,
    contamination_training_gate: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    parse_rows = [
        row
        for row in membership.get("performance", [])
        if isinstance(row, dict) and row.get("dps_parse") is not None
    ]
    exact_records = (
        _exact_ranking_records(
            evidence.ranking_rows,
            character_guid=str(membership["character_guid"]),
        )
        if evidence is not None
        else []
    )
    ranking_endpoint_capture_count = (
        evidence.ranking_objects_available if evidence is not None else 0
    )
    ranking_endpoint_captured = ranking_endpoint_capture_count > 0
    expected_name = membership.get("character_name")
    name_matches = {
        (str(row.get("id")), str(row.get("player_guid")))
        for row in (evidence.ranking_rows if evidence is not None else [])
        if isinstance(expected_name, str) and row.get("player_name") == expected_name
    }
    name_match_guids = sorted(
        {
            guid
            for _, guid in name_matches
            if guid and guid != "None"
        }
    )
    if exact_records:
        ranking_status = "EXACT_RANKING_DPS_AVAILABLE"
    elif ranking_endpoint_captured and not name_matches:
        ranking_status = "RANKING_ENDPOINT_CAPTURED_EXACT_GUID_ABSENT"
    elif ranking_endpoint_captured:
        ranking_status = (
            "RANKING_ENDPOINT_CAPTURED_EXACT_GUID_ABSENT_NAME_PRESENT_CONFLICT"
        )
    else:
        ranking_status = "MISSING"
    contamination_label = str(membership.get("contamination_label"))
    contamination_training_eligible = (
        contamination_label in TRAINING_CANDIDATE_LABELS
    )
    available_streams = evidence.available_streams if evidence is not None else set()
    unavailable_404_streams = (
        evidence.unavailable_404_streams if evidence is not None else set()
    )
    capture_statuses = (
        evidence.stream_capture_statuses if evidence is not None else {}
    )
    required_set = set(required_streams)
    action_set = set(ACTION_STREAMS)
    missing_required = sorted(required_set - available_streams)
    missing_actions = sorted(action_set - available_streams)
    terminal_unavailable_required = sorted(
        required_set & unavailable_404_streams
    )
    terminal_unavailable_actions = sorted(action_set & unavailable_404_streams)
    fetch_required_streams = sorted(
        required_set - available_streams - unavailable_404_streams
    )
    fetch_required_actions = sorted(
        action_set - available_streams - unavailable_404_streams
    )
    available_after_404 = sorted(
        stream_name
        for stream_name in required_set & available_streams
        if capture_statuses.get(stream_name) == {"AVAILABLE", "UNAVAILABLE_404"}
    )
    coverage = {
        "join": {
            "status": "MATCHED" if evidence is not None else "MISSING",
            "method": join_method,
            "matched_ingest_instance_id": (
                evidence.instance_id if evidence is not None else None
            ),
        },
        "metadata": {
            "status": (
                "AVAILABLE"
                if evidence is not None and evidence.metadata_available
                else "MISSING"
            ),
        },
        "performance_parse": {
            "status": (
                "PARSE_AVAILABLE_NOT_EXACT_DPS" if parse_rows else "MISSING"
            ),
            "encounter_count": len(parse_rows),
            "exact_damage_duration_dps": False,
        },
        "ranking_exact_dps": (
            {
                "status": ranking_status,
                "identity_match": "EXACT_PLAYER_GUID_ONLY",
                "record_count": len(exact_records),
                "records": exact_records,
                "exact_dps_available": bool(exact_records),
                "usable_for_exact_dps_training": (
                    bool(exact_records)
                    and (
                        contamination_training_eligible
                        if contamination_training_gate
                        else True
                    )
                ),
                **(
                    {
                        "raid_contamination_training_eligible": (
                            contamination_training_eligible
                        )
                    }
                    if contamination_training_gate
                    else {}
                ),
                "ranking_endpoint_captured": ranking_endpoint_captured,
                "ranking_endpoint_capture_count": ranking_endpoint_capture_count,
                "expected_name_exact_match_record_count": len(name_matches),
                "expected_name_match_guids": name_match_guids,
                "ranking_fetch_required": not ranking_endpoint_captured,
                "negative_evidence_contract": (
                    "captured endpoint plus absent exact GUID never fabricates zero DPS "
                    "or substitutes performance parse; retry only after new upstream evidence"
                ),
            }
            if ranking_negative_evidence
            else {
                "status": (
                    "EXACT_RANKING_DPS_AVAILABLE" if exact_records else "MISSING"
                ),
                "identity_match": "EXACT_PLAYER_GUID_ONLY",
                "record_count": len(exact_records),
                "records": exact_records,
            }
        ),
        "required_streams": {
            "status": "COMPLETE" if not missing_required else "INCOMPLETE",
            "required": list(required_streams),
            "available": sorted(required_set & available_streams),
            "missing": missing_required,
            **(
                {
                    "terminal_unavailable_404": terminal_unavailable_required,
                    "uncaptured": fetch_required_streams,
                    "fetch_required": fetch_required_streams,
                    "available_after_prior_unavailable_404": available_after_404,
                    "network_capture_status": (
                        "FETCH_REQUIRED"
                        if fetch_required_streams
                        else "CAPTURED_NO_FETCH_REQUIRED"
                    ),
                }
                if stream_negative_evidence
                else {}
            ),
        },
        "action_events": {
            "status": "COMPLETE" if not missing_actions else "INCOMPLETE",
            "required": list(ACTION_STREAMS),
            "available": sorted(action_set & available_streams),
            "missing": missing_actions,
            **(
                {
                    "terminal_unavailable_404": terminal_unavailable_actions,
                    "uncaptured": fetch_required_actions,
                    "fetch_required": fetch_required_actions,
                }
                if stream_negative_evidence
                else {}
            ),
        },
        "source_ingest_manifest_sha256": (
            sorted(evidence.source_manifest_sha256) if evidence is not None else []
        ),
    }
    missing = {
        "metadata": coverage["metadata"]["status"] != "AVAILABLE",
        "ranking_exact_dps": not exact_records,
        **(
            {"ranking_fetch_required": not ranking_endpoint_captured}
            if ranking_negative_evidence
            else {}
        ),
        "required_streams": missing_required,
        **(
            {
                "required_streams_fetch_required": fetch_required_streams,
                "required_streams_terminal_unavailable_404": (
                    terminal_unavailable_required
                ),
            }
            if stream_negative_evidence
            else {}
        ),
    }
    return coverage, missing


def _validate_required_streams(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise CharacterHistoryError("required_streams must be a sequence")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value not in ingest_v1.EVENT_STREAM_TYPES:
            raise CharacterHistoryError(f"unsupported required stream {value!r}")
        normalized.add(value)
    if not normalized:
        raise CharacterHistoryError("at least one required stream is required")
    return tuple(sorted(normalized))


def build_character_instance_inventory(
    character_manifest_paths: Sequence[str | Path],
    ingest_manifest_paths: Sequence[str | Path],
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    instance_name: str | None = None,
    started_at_not_before: str | None = None,
    required_streams: Sequence[str] = DEFAULT_REQUIRED_STREAMS,
    max_fetch_instances: int = DEFAULT_MAX_FETCH_INSTANCES,
    _implementation_revision: str = INVENTORY_IMPLEMENTATION_REVISION,
) -> dict[str, Any]:
    """Compare character history against verified ingest evidence.

    The returned object is itself content-addressed but is intentionally not
    written.  Its two fetch lanes are bounded plans for the existing ingest
    module; this function never initiates those downloads.
    """

    if _implementation_revision not in SUPPORTED_INVENTORY_IMPLEMENTATION_REVISIONS:
        raise CharacterHistoryError("unsupported inventory implementation revision")
    ranking_negative_evidence = _implementation_revision in {
        RANKING_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION,
        STREAM_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION,
        INVENTORY_IMPLEMENTATION_REVISION,
    }
    stream_negative_evidence = _implementation_revision in {
        STREAM_NEGATIVE_EVIDENCE_INVENTORY_IMPLEMENTATION_REVISION,
        INVENTORY_IMPLEMENTATION_REVISION,
    }
    current_boundary_evidence = (
        _implementation_revision == INVENTORY_IMPLEMENTATION_REVISION
    )
    expected_history_revision = (
        IMPLEMENTATION_REVISION
        if current_boundary_evidence
        else LEGACY_IMPLEMENTATION_REVISION
    )
    expected_ingest_revision = (
        ingest_v1.IMPLEMENTATION_REVISION
        if current_boundary_evidence
        else ingest_v1.LEGACY_IMPLEMENTATION_REVISION
    )
    expected_ingest_parser_revision = (
        ingest_v1.PARSER_CONTRACT_REVISION
        if current_boundary_evidence
        else ingest_v1.LEGACY_PARSER_CONTRACT_REVISION
    )
    if not character_manifest_paths:
        raise CharacterHistoryError("at least one character history manifest is required")
    if (
        isinstance(max_fetch_instances, bool)
        or not isinstance(max_fetch_instances, int)
        or max_fetch_instances < 0
    ):
        raise CharacterHistorySafetyLimitError("max_fetch_instances must be non-negative")
    normalized_required = _validate_required_streams(required_streams)
    normalized_name = None
    if instance_name is not None:
        normalized_name = _text(instance_name, label="instance_name")
    cutoff = None
    normalized_cutoff = None
    if started_at_not_before is not None:
        normalized_cutoff = _timestamp(
            started_at_not_before, label="started_at_not_before"
        )
        assert normalized_cutoff is not None
        cutoff = ingest_v1._parse_rfc3339(
            normalized_cutoff, field="started_at_not_before"
        )

    raw_root = _raw_root(Path(data_root))
    memberships, history_shas = _history_memberships(
        character_manifest_paths,
        data_root=Path(data_root),
        expected_implementation_revision=expected_history_revision,
    )
    ingest_rows, ingest_shas = _ingest_evidence_rows(
        ingest_manifest_paths,
        raw_root=raw_root,
        expected_implementation_revision=expected_ingest_revision,
        expected_parser_contract_revision=expected_ingest_parser_revision,
    )
    by_id = {row.instance_id: row for row in ingest_rows}
    by_slug: dict[str, list[_IngestEvidence]] = {}
    for row in ingest_rows:
        if row.slug is not None:
            by_slug.setdefault(row.slug, []).append(row)

    filtered: list[dict[str, Any]] = []
    training_ids: set[str] = set()
    diagnostic_ids: set[str] = set()
    missing_ids: set[str] = set()
    training_missing_streams: set[str] = set()
    training_needs_ranking = False
    for membership in memberships:
        if normalized_name is not None and membership.get("name") != normalized_name:
            continue
        if cutoff is not None:
            started_raw = membership.get("started_at")
            if not isinstance(started_raw, str):
                continue
            started = ingest_v1._parse_rfc3339(started_raw, field="started_at")
            if started < cutoff:
                continue

        instance_id = str(membership["instance_id"])
        evidence = by_id.get(instance_id)
        join_method = "INSTANCE_ID" if evidence is not None else "NONE"
        if evidence is not None and (
            evidence.slug is not None and evidence.slug != membership.get("slug")
        ):
            raise CharacterHistoryError(
                f"instance-id join for {instance_id} has conflicting slug"
            )
        if evidence is None:
            candidates = by_slug.get(str(membership.get("slug")), [])
            if len(candidates) > 1:
                raise CharacterHistoryError(
                    f"strict slug join for {membership.get('slug')!r} is ambiguous"
                )
            if len(candidates) == 1:
                evidence = candidates[0]
                join_method = "STRICT_UNIQUE_SLUG"
        if evidence is None:
            missing_ids.add(instance_id)

        coverage, missing = _coverage_for_membership(
            membership,
            evidence,
            join_method=join_method,
            required_streams=normalized_required,
            ranking_negative_evidence=ranking_negative_evidence,
            stream_negative_evidence=stream_negative_evidence,
            contamination_training_gate=current_boundary_evidence,
        )
        label = str(membership["contamination_label"])
        if label not in TRAINING_CANDIDATE_LABELS | NONTRAINING_LABELS:
            raise CharacterHistoryError(f"unsupported contamination label {label!r}")
        ranking_fetch_required = bool(
            missing.get("ranking_fetch_required", missing["ranking_exact_dps"])
        )
        required_streams_fetch_required = list(
            missing.get(
                "required_streams_fetch_required", missing["required_streams"]
            )
        )
        fetch_incomplete = bool(
            missing["metadata"]
            or ranking_fetch_required
            or required_streams_fetch_required
        )
        training_candidate = label in TRAINING_CANDIDATE_LABELS
        if training_candidate and fetch_incomplete:
            training_ids.add(instance_id)
            training_missing_streams.update(required_streams_fetch_required)
            training_needs_ranking = training_needs_ranking or bool(
                ranking_fetch_required
            )
        elif (
            not training_candidate
            and (missing["metadata"] or ranking_fetch_required)
        ):
            diagnostic_ids.add(instance_id)

        row = deepcopy(membership)
        row["coverage"] = coverage
        row["missing_plan"] = {
            **missing,
            "training_candidate": training_candidate,
            "training_fetch_planned": training_candidate and fetch_incomplete,
            "diagnostic_metadata_ranking_fetch_planned": (
                not training_candidate
                and bool(missing["metadata"] or ranking_fetch_required)
            ),
        }
        filtered.append(row)

    planned_ids = training_ids | diagnostic_ids
    if len(planned_ids) > max_fetch_instances:
        raise CharacterHistorySafetyLimitError(
            f"fetch plan contains {len(planned_ids)} instances, exceeding "
            f"max_fetch_instances={max_fetch_instances}"
        )

    core = {
        "schema": INVENTORY_SCHEMA,
        "implementation_revision": _implementation_revision,
        "kind": INVENTORY_KIND,
        "inventory_request": {
            "instance_name": normalized_name,
            "started_at_not_before": normalized_cutoff,
            "required_streams": list(normalized_required),
            "max_fetch_instances": max_fetch_instances,
        },
        "source_bindings": {
            "character_history_manifest_sha256": history_shas,
            "ingest_manifest_sha256": ingest_shas,
        },
        "filters": {
            "instance_name": normalized_name,
            "started_at_not_before": normalized_cutoff,
        },
        "instances": filtered,
        "missing_instance_ids": sorted(missing_ids),
        "fetch_plan": {
            "execution": "NOT_EXECUTED_PLAN_ONLY",
            "training": {
                "explicit_instance_ids": sorted(training_ids),
                "stream_types": sorted(training_missing_streams),
                "include_ranking_records": training_needs_ranking,
                "max_instances": max_fetch_instances,
                "eligibility_labels": sorted(TRAINING_CANDIDATE_LABELS),
            },
            "diagnostic_metadata_and_ranking": {
                "explicit_instance_ids": sorted(diagnostic_ids),
                "stream_types": [],
                "include_ranking_records": bool(diagnostic_ids),
                "max_instances": max_fetch_instances,
                "never_training_fetch": True,
            },
        },
        "evidence_contract": {
            "character_instances_id_authoritative": True,
            "leaderboard_instance_id_used": False,
            "leaderboard_zero_id_rejected_as_identity": True,
            "performance_parse_is_exact_dps": False,
            "exact_dps_source": "instance ranking-records exact player_guid",
            **(
                {
                    "exact_dps_availability_is_training_eligibility": False,
                    "exact_dps_training_requires_raid_contamination_eligibility": True,
                    "nontraining_exact_dps_remains_descriptive_evidence": True,
                }
                if current_boundary_evidence
                else {}
            ),
            "action_coverage_source": list(ACTION_STREAMS),
            "player_name_used_for_ranking_identity": False,
            **(
                {
                    "ranking_negative_evidence": {
                        "exact_dps_missing_preserved": True,
                        "zero_dps_fabricated": False,
                        "performance_parse_substituted": False,
                        "fetch_required_only_when_endpoint_object_absent": True,
                        "captured_exact_guid_and_name_absence_status": (
                            "RANKING_ENDPOINT_CAPTURED_EXACT_GUID_ABSENT"
                        ),
                    }
                }
                if ranking_negative_evidence
                else {}
            ),
            **(
                {
                    "stream_negative_evidence": {
                        "unavailable_404_is_semantic_absence": True,
                        "unavailable_404_fetch_required": False,
                        "no_wrapper_fetch_required": True,
                        "available_supersedes_prior_unavailable_404": True,
                        "conflicting_available_objects": "FAIL_CLOSED",
                        "manual_refresh_path": (
                            "explicit chronicle_external_api_ingest/v1 capture; "
                            "planner does not auto-retry terminal 404"
                        ),
                    }
                }
                if stream_negative_evidence
                else {}
            ),
            "upstream_ranking_endpoint_binding": {
                "status": "INHERITED_INGEST_V1_MANIFEST_ROW_BINDING",
                "request_url_preserved_by_upstream_schema": False,
                "limitation": (
                    "ranking raw bytes are hash-verified and attached to an exact ingest "
                    "instance row, but chronicle_external_api_ingest/v1 does not preserve "
                    "the ranking-records response request URL"
                ),
            },
        },
        "contamination_contract": _contamination_contract(
            expected_history_revision
        ),
        "summary": {
            "player_instance_count": len(filtered),
            "unique_instance_count": len(
                {str(row["instance_id"]) for row in filtered}
            ),
            "missing_instance_count": len(missing_ids),
            "training_fetch_instance_count": len(training_ids),
            "diagnostic_fetch_instance_count": len(diagnostic_ids),
        },
        "comparison_status": {
            "voting_eligible": False,
            "comparison_ready": False,
            "reason": "discovery and coverage inventory only",
        },
    }
    return _content_addressed(core)


def _inventory_manifest_directory(data_root: Path) -> Path:
    # Reuse the raw-root guard so publication cannot be redirected outside the
    # explicitly scoped offline_data tree, while keeping this derived artifact
    # out of the immutable raw-object directory.
    _raw_root(Path(data_root))
    return Path(data_root).resolve() / Path(INVENTORY_MANIFEST_DIRECTORY)


def _validated_sha256_text(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CharacterHistoryError(f"{label} must be a lowercase sha256 digest")
    return value


def _resolved_inventory_manifest_path(
    path: str | Path,
    *,
    data_root: Path,
) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise CharacterHistoryError("inventory manifest path must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise _error(f"cannot resolve inventory manifest {requested}: {error}", error)
    allowed = _inventory_manifest_directory(data_root).resolve()
    if resolved.parent != allowed:
        raise CharacterHistoryError(
            "inventory manifest path escapes the expected derived manifest directory"
        )
    return resolved


def publish_character_instance_inventory(
    character_manifest_paths: Sequence[str | Path],
    ingest_manifest_paths: Sequence[str | Path],
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    instance_name: str | None = None,
    started_at_not_before: str | None = None,
    required_streams: Sequence[str] = DEFAULT_REQUIRED_STREAMS,
    max_fetch_instances: int = DEFAULT_MAX_FETCH_INSTANCES,
) -> dict[str, Any]:
    """Build and atomically publish a replayable content-addressed inventory."""

    inventory = build_character_instance_inventory(
        character_manifest_paths,
        ingest_manifest_paths,
        data_root=data_root,
        instance_name=instance_name,
        started_at_not_before=started_at_not_before,
        required_streams=required_streams,
        max_fetch_instances=max_fetch_instances,
    )
    digest = _verify_content_address(inventory, label="character instance inventory")
    payload = _canonical_document_bytes(inventory)
    manifest_path = _inventory_manifest_directory(Path(data_root)) / (
        f"{INVENTORY_MANIFEST_PREFIX}.{digest}.manifest.json"
    )
    try:
        ingest_v1._atomic_write_bytes(manifest_path, payload)
    except ingest_v1.ChronicleIngestError as error:
        raise _error(f"cannot publish character instance inventory: {error}", error)
    return {
        "schema": INVENTORY_SCHEMA,
        "content_sha256": digest,
        "manifest_path": str(manifest_path),
        "summary": deepcopy(inventory["summary"]),
    }


def load_character_instance_inventory_manifest(
    path: str | Path,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
) -> tuple[dict[str, Any], Path]:
    """Hash-check an inventory and replay it from all bound source manifests."""

    resolved = _resolved_inventory_manifest_path(path, data_root=Path(data_root))
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise _error(f"cannot read inventory manifest {resolved}: {error}", error)
    value = _json_from_bytes(payload, label="character instance inventory manifest")
    manifest = _mapping(value, label="character instance inventory manifest")
    if payload != _canonical_document_bytes(manifest):
        raise CharacterHistoryError("character instance inventory is not canonical JSON")
    digest = _verify_content_address(manifest, label="character instance inventory")
    expected_name = f"{INVENTORY_MANIFEST_PREFIX}.{digest}.manifest.json"
    if resolved.name != expected_name:
        raise CharacterHistoryError("inventory manifest filename/content hash mismatch")
    if manifest.get("schema") != INVENTORY_SCHEMA:
        raise CharacterHistoryError("inventory manifest schema is unsupported")
    implementation_revision = manifest.get("implementation_revision")
    if implementation_revision not in SUPPORTED_INVENTORY_IMPLEMENTATION_REVISIONS:
        raise CharacterHistoryError("inventory implementation revision is unsupported")
    if manifest.get("kind") != INVENTORY_KIND:
        raise CharacterHistoryError("inventory manifest kind is unsupported")

    bindings = _mapping(manifest.get("source_bindings"), label="source_bindings")
    if set(bindings) != {
        "character_history_manifest_sha256",
        "ingest_manifest_sha256",
    }:
        raise CharacterHistoryError("inventory source_bindings schema is invalid")
    history_shas = [
        _validated_sha256_text(value, label=f"history source sha256[{index}]")
        for index, value in enumerate(
            _list(
                bindings.get("character_history_manifest_sha256"),
                label="character history source bindings",
            )
        )
    ]
    ingest_shas = [
        _validated_sha256_text(value, label=f"ingest source sha256[{index}]")
        for index, value in enumerate(
            _list(
                bindings.get("ingest_manifest_sha256"),
                label="ingest source bindings",
            )
        )
    ]
    if not history_shas or history_shas != sorted(set(history_shas)):
        raise CharacterHistoryError(
            "inventory history source bindings must be non-empty, unique, and sorted"
        )
    if ingest_shas != sorted(set(ingest_shas)):
        raise CharacterHistoryError(
            "inventory ingest source bindings must be unique and sorted"
        )

    request = _mapping(manifest.get("inventory_request"), label="inventory_request")
    if set(request) != {
        "instance_name",
        "started_at_not_before",
        "required_streams",
        "max_fetch_instances",
    }:
        raise CharacterHistoryError("inventory_request schema is invalid")
    required_streams = _list(
        request.get("required_streams"), label="inventory_request.required_streams"
    )
    raw_root = _raw_root(Path(data_root))
    history_paths = [
        raw_root
        / "character_history_manifests"
        / f"chronicle_external_character_history_v1.{sha}.manifest.json"
        for sha in history_shas
    ]
    ingest_paths = [raw_root / "manifests" / f"{sha}.json" for sha in ingest_shas]
    rebuilt = build_character_instance_inventory(
        history_paths,
        ingest_paths,
        data_root=Path(data_root),
        instance_name=request.get("instance_name"),
        started_at_not_before=request.get("started_at_not_before"),
        required_streams=required_streams,
        max_fetch_instances=request.get("max_fetch_instances"),
        _implementation_revision=implementation_revision,
    )
    if dict(manifest) != rebuilt:
        raise CharacterHistoryError(
            "inventory manifest disagrees with deterministic source-manifest replay"
        )
    return deepcopy(dict(manifest)), resolved


def _add_history_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--player",
        action="append",
        nargs=3,
        metavar=("SERVER", "REALM", "CHARACTER"),
        required=True,
        help="repeatable official character path tuple",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-instances", type=int, default=DEFAULT_MAX_INSTANCES)


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit/capture Chronicle character histories and plan bounded gaps"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="query and validate histories without writing"
    )
    _add_history_request_arguments(list_parser)

    capture_parser = subparsers.add_parser(
        "capture", help="capture validated identity/pages as immutable raw objects"
    )
    _add_history_request_arguments(capture_parser)
    capture_parser.add_argument(
        "--data-root", type=Path, default=DEFAULT_DATA_ROOT
    )

    replay_parser = subparsers.add_parser(
        "replay",
        help="migrate a verified legacy history manifest from local raw objects",
    )
    replay_parser.add_argument("--source-manifest", type=Path, required=True)
    replay_parser.add_argument(
        "--data-root", type=Path, default=DEFAULT_DATA_ROOT
    )

    inventory_parser = subparsers.add_parser(
        "inventory", help="compare captured history against existing ingest manifests"
    )
    inventory_parser.add_argument(
        "--character-manifest", action="append", type=Path, required=True
    )
    inventory_parser.add_argument(
        "--ingest-manifest", action="append", type=Path, default=[]
    )
    inventory_parser.add_argument(
        "--data-root", type=Path, default=DEFAULT_DATA_ROOT
    )
    inventory_parser.add_argument("--instance-name")
    inventory_parser.add_argument("--started-at-not-before")
    inventory_parser.add_argument(
        "--required-stream",
        action="append",
        choices=ingest_v1.EVENT_STREAM_TYPES,
        default=None,
    )
    inventory_parser.add_argument(
        "--max-fetch-instances", type=int, default=DEFAULT_MAX_FETCH_INSTANCES
    )
    inventory_parser.add_argument(
        "--publish",
        action="store_true",
        help="atomically publish the content-addressed inventory; default is read-only",
    )
    return parser


def _cli_queries(values: Sequence[Sequence[str]]) -> list[CharacterQuery]:
    return [
        CharacterQuery(server=row[0], realm=row[1], character=row[2])
        for row in values
    ]


def _write_cli_output(
    value: Any,
    *,
    stdout: TextIO | None,
    stdout_buffer: BinaryIO | None,
) -> None:
    payload = _canonical_document_bytes(value)
    if stdout is not None:
        if stdout_buffer is not None:
            raise CharacterHistoryError("stdout and stdout_buffer are mutually exclusive")
        stdout.write(payload.decode("utf-8"))
        return
    target = stdout_buffer
    if target is None:
        target = getattr(sys.stdout, "buffer", None)
    if target is not None:
        target.write(payload)
        return
    # Embedded hosts may replace sys.stdout with a text-only object.  This path
    # remains useful there; the normal CLI always takes the binary UTF-8 branch.
    sys.stdout.write(payload.decode("utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdout_buffer: BinaryIO | None = None,
) -> int:
    args = _cli_parser().parse_args(argv)
    if args.command == "replay":
        value = replay_character_history_manifest_from_local_raw(
            args.source_manifest,
            data_root=args.data_root,
        )
    elif args.command == "inventory":
        inventory_request = {
            "data_root": args.data_root,
            "instance_name": args.instance_name,
            "started_at_not_before": args.started_at_not_before,
            "required_streams": (
                args.required_stream
                if args.required_stream is not None
                else DEFAULT_REQUIRED_STREAMS
            ),
            "max_fetch_instances": args.max_fetch_instances,
        }
        if args.publish:
            value = publish_character_instance_inventory(
                args.character_manifest,
                args.ingest_manifest,
                **inventory_request,
            )
        else:
            value = build_character_instance_inventory(
                args.character_manifest,
                args.ingest_manifest,
                **inventory_request,
            )
    else:
        client = ingest_v1.ChronicleClient()
        request = {
            "client": client,
            "page_size": args.page_size,
            "max_pages": args.max_pages,
            "max_instances": args.max_instances,
        }
        queries = _cli_queries(args.player)
        if args.command == "list":
            value = list_character_histories(queries, **request)
        elif args.command == "capture":
            value = capture_character_histories(
                queries, data_root=args.data_root, **request
            )
        else:  # pragma: no cover - argparse enforces the subcommand set
            raise CharacterHistoryError(f"unsupported command {args.command!r}")
    _write_cli_output(value, stdout=stdout, stdout_buffer=stdout_buffer)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via CLI smoke test
    try:
        raise SystemExit(main())
    except CharacterHistoryError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
