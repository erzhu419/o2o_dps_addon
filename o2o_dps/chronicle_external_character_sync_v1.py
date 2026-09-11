"""Incrementally synchronize Chronicle character evidence without broad downloads.

The official character endpoints are the identity authority and are always
walked through ``has_more == false``.  ``uploaded_at`` discovery is separately
probed through ``/raidlogs/recent`` using the exact ``upload_after`` query
parameter.  The recent endpoint is never used as a character identity source.

After a small immutable character-history capture, existing ingest manifests
are replayed and hash/size checked.  Exact ranking records are joined only by
the character GUID.  Missing event streams are fetched only for raids whose
raid-level guild plus ``started_at`` label is training eligible.  Suspect or
unknown raids may receive metadata/ranking diagnostics, but never event-stream
downloads.  A captured ``UNAVAILABLE_404`` is terminal negative evidence for
automatic planning; only an uncaptured stream is a network gap.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, BinaryIO, Mapping, Sequence, TextIO
import unicodedata

from . import chronicle_external_api_ingest_v1 as ingest_v1
from . import chronicle_external_character_history_v1 as history_v1
from . import chronicle_external_team_timeline_v2 as timeline_v2


SCHEMA = "chronicle_external_character_sync/v1"
IMPLEMENTATION_REVISION = (
    "chronicle_external_character_sync_v1.1_stream_negative_evidence"
)
DEFAULT_DATA_ROOT = ingest_v1.DEFAULT_DATA_ROOT
DEFAULT_REQUIRED_STREAMS = tuple(sorted(ingest_v1.EVENT_STREAM_TYPES))
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_INSTANCES = 10_000
DEFAULT_MAX_FETCH_INSTANCES = 500
EVIDENCE_TIERS = ("history", "ranking", "eligible-streams")


class CharacterSyncError(RuntimeError):
    """The incremental sync plan or its evidence closure is invalid."""


class CharacterSyncSafetyLimitError(CharacterSyncError):
    """A bounded sync would be incomplete or unexpectedly large."""


@dataclass(frozen=True, order=True)
class GapBatch:
    """One exact, de-duplicated explicit-instance ingest call."""

    lane: str
    instance_ids: tuple[str, ...]
    stream_types: tuple[str, ...]
    include_ranking_records: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "explicit_instance_ids": list(self.instance_ids),
            "stream_types": list(self.stream_types),
            "include_ranking_records": self.include_ranking_records,
        }


@dataclass(frozen=True)
class ResolvedCharacterQueries:
    """Merged manual/timeline query set plus compact auditable provenance."""

    queries: tuple[history_v1.CharacterQuery, ...]
    expected_guids: Mapping[history_v1.CharacterQuery, str]
    source: Mapping[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _normalized_queries(
    queries: Sequence[history_v1.CharacterQuery],
) -> tuple[history_v1.CharacterQuery, ...]:
    if not queries:
        raise CharacterSyncError("at least one character query is required")
    if not all(isinstance(value, history_v1.CharacterQuery) for value in queries):
        raise CharacterSyncError("queries must contain CharacterQuery values")
    result = tuple(sorted(set(queries)))
    if len(result) != len(queries):
        raise CharacterSyncError("duplicate character queries are not allowed")
    return result


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterSyncError(f"{label} must be a non-empty string")
    return value.strip()


def _canonical_player_name(value: Any, *, label: str) -> str:
    name = unicodedata.normalize("NFC", _required_text(value, label=label))
    if not name:
        raise CharacterSyncError(f"{label} must be a non-empty string")
    return name


def _normalized_query_path(
    query: history_v1.CharacterQuery,
) -> tuple[str, str, str]:
    return tuple(
        unicodedata.normalize("NFC", value).casefold()
        for value in (query.server, query.realm, query.character)
    )


def character_queries_from_team_timeline_manifest(
    path: str | Path,
    *,
    default_server: str,
    default_realm: str,
) -> ResolvedCharacterQueries:
    """Extract a de-duplicated warrior directory from a verified timeline manifest.

    Only the manifest and each embedded instance entry are content-address
    checked here.  Partition payloads are intentionally not re-streamed: no
    query identity is taken from them, and a 293-character history/ranking sync
    must not first scan the multi-gigabyte timeline partitions.
    """

    server = _required_text(default_server, label="default_server")
    realm = _required_text(default_realm, label="default_realm")
    try:
        manifest, resolved = timeline_v2.load_external_team_timeline_manifest(
            path,
            verify_inputs=False,
            verify_partitions=False,
        )
    except timeline_v2.ChronicleExternalTeamTimelineV2Error as error:
        raise CharacterSyncError(f"invalid External timeline/v2 manifest: {error}") from error

    guid_to_name: dict[str, str] = {}
    name_to_guid: dict[str, str] = {}
    observation_count = 0
    instances = manifest.get("instances")
    if not isinstance(instances, list):
        raise CharacterSyncError("External timeline/v2 manifest instances must be a list")
    for instance_index, raw_instance in enumerate(instances):
        if not isinstance(raw_instance, dict):
            raise CharacterSyncError("External timeline/v2 instance must be an object")
        try:
            timeline_v2._verify_content_address(
                raw_instance, label=f"timeline instance[{instance_index}]"
            )
        except timeline_v2.ChronicleExternalTeamTimelineV2Error as error:
            raise CharacterSyncError(
                f"invalid External timeline/v2 instance entry: {error}"
            ) from error
        provenance = raw_instance.get("instance_provenance")
        if not isinstance(provenance, dict):
            raise CharacterSyncError("timeline instance lacks instance_provenance")
        evidence = provenance.get("warrior_spec_evidence")
        if not isinstance(evidence, dict):
            raise CharacterSyncError(
                "timeline instance lacks warrior_spec_evidence"
            )
        observations = evidence.get("observations")
        if not isinstance(observations, list):
            raise CharacterSyncError(
                "warrior_spec_evidence.observations must be a list"
            )
        declared_count = evidence.get("observation_count")
        if (
            isinstance(declared_count, bool)
            or not isinstance(declared_count, int)
            or declared_count != len(observations)
        ):
            raise CharacterSyncError(
                "warrior_spec_evidence observation_count mismatch"
            )
        for observation_index, raw_observation in enumerate(observations):
            if not isinstance(raw_observation, dict):
                raise CharacterSyncError("warrior observation must be an object")
            player_class = _required_text(
                raw_observation.get("player_class"),
                label=(
                    f"instances[{instance_index}].observations[{observation_index}]"
                    ".player_class"
                ),
            )
            if player_class.casefold() != "warrior":
                raise CharacterSyncError(
                    "non-warrior row appears in warrior_spec_evidence.observations"
                )
            guid = _required_text(
                raw_observation.get("player_guid"),
                label=(
                    f"instances[{instance_index}].observations[{observation_index}]"
                    ".player_guid"
                ),
            )
            if (
                history_v1.CHARACTER_GUID_PATTERN.fullmatch(guid) is None
                or int(guid[2:], 16) == 0
            ):
                raise CharacterSyncError(f"invalid warrior GUID {guid!r}")
            guid_key = guid.lower()
            name = _canonical_player_name(
                raw_observation.get("player_name"),
                label=(
                    f"instances[{instance_index}].observations[{observation_index}]"
                    ".player_name"
                ),
            )
            prior_name = guid_to_name.get(guid_key)
            if prior_name is not None and prior_name != name:
                raise CharacterSyncError(
                    f"warrior GUID {guid} maps to multiple names: "
                    f"{prior_name!r}, {name!r}"
                )
            name_key = name.casefold()
            prior_guid = name_to_guid.get(name_key)
            if prior_guid is not None and prior_guid != guid_key:
                raise CharacterSyncError(
                    f"warrior name {name!r} maps to multiple GUIDs"
                )
            guid_to_name[guid_key] = name
            name_to_guid[name_key] = guid_key
            observation_count += 1
    if not guid_to_name:
        raise CharacterSyncError("External timeline/v2 contains no warrior observations")

    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        raise CharacterSyncError("External timeline/v2 manifest summary must be an object")
    if summary.get("warrior_spec_observation_count") != observation_count:
        raise CharacterSyncError(
            "External timeline/v2 warrior observation total disagrees with summary"
        )

    players = sorted(
        ((name, guid) for guid, name in guid_to_name.items()),
        key=lambda row: (row[0].casefold(), row[0], row[1]),
    )
    queries = tuple(
        history_v1.CharacterQuery(server=server, realm=realm, character=name)
        for name, _ in players
    )
    expected_guids = {
        query: "0x" + guid[2:].upper()
        for query, (_, guid) in zip(queries, players)
    }
    bindings = [
        {
            **query.to_dict(),
            "expected_guid": expected_guids[query],
        }
        for query in queries
    ]
    try:
        manifest_bytes = resolved.read_bytes()
    except OSError as error:
        raise CharacterSyncError(f"cannot hash timeline manifest {resolved}: {error}") from error
    source = {
        "kind": "external_team_timeline_v2_warrior_directory",
        "manifest_path": str(resolved),
        "manifest_file_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_content_sha256": manifest["content_address"]["sha256"],
        "manifest_instance_count": len(instances),
        "warrior_observation_count": observation_count,
        "unique_warrior_guid_count": len(guid_to_name),
        "character_query_count": len(queries),
        "expected_guid_binding_count": len(bindings),
        "expected_guid_bindings": bindings,
        "expected_guid_bindings_sha256": hashlib.sha256(
            _canonical_bytes(bindings)
        ).hexdigest(),
        "default_server": server,
        "default_realm": realm,
        "server_realm_source": "explicit CLI defaults; not inferred from timeline",
        "verification_scope": (
            "timeline manifest plus every embedded instance-entry content address; "
            "partitions not re-streamed"
        ),
    }
    return ResolvedCharacterQueries(
        queries=queries,
        expected_guids=expected_guids,
        source=source,
    )


def resolve_character_queries(
    manual_queries: Sequence[history_v1.CharacterQuery] = (),
    *,
    players_from_team_timeline_manifest: str | Path | None = None,
    default_server: str | None = None,
    default_realm: str | None = None,
) -> ResolvedCharacterQueries:
    """Merge hand-written and timeline-derived players without identity guessing."""

    manual = (
        _normalized_queries(manual_queries)
        if manual_queries
        else ()
    )
    manual_by_path: dict[tuple[str, str, str], history_v1.CharacterQuery] = {}
    for query in manual:
        path_key = _normalized_query_path(query)
        previous = manual_by_path.get(path_key)
        if previous is not None and previous != query:
            raise CharacterSyncError(
                f"manual character query {query!r} repeats a normalized API path"
            )
        manual_by_path[path_key] = query
    timeline_result: ResolvedCharacterQueries | None = None
    if players_from_team_timeline_manifest is not None:
        if default_server is None or default_realm is None:
            raise CharacterSyncError(
                "--players-from-team-timeline-manifest requires both "
                "--default-server and --default-realm"
            )
        timeline_result = character_queries_from_team_timeline_manifest(
            players_from_team_timeline_manifest,
            default_server=default_server,
            default_realm=default_realm,
        )
    elif default_server is not None or default_realm is not None:
        raise CharacterSyncError(
            "--default-server/--default-realm are only valid with a timeline manifest"
        )

    merged: dict[tuple[str, str, str], history_v1.CharacterQuery] = {
        (query.server, query.realm, query.character): query for query in manual
    }
    expected_guids: dict[history_v1.CharacterQuery, str] = {}
    overlap = 0
    if timeline_result is not None:
        for query in timeline_result.queries:
            key = (query.server, query.realm, query.character)
            path_key = _normalized_query_path(query)
            manual_same_path = manual_by_path.get(path_key)
            if manual_same_path is not None and manual_same_path != query:
                raise CharacterSyncError(
                    f"timeline/manual character query {query!r} repeats a "
                    "normalized API path"
                )
            if key in merged:
                overlap += 1
            else:
                merged[key] = query
            expected_guids[merged[key]] = timeline_result.expected_guids[query]
    if not merged:
        raise CharacterSyncError(
            "at least one --player or --players-from-team-timeline-manifest is required"
        )
    ordered = tuple(
        sorted(
            merged.values(),
            key=lambda query: (
                query.character.casefold(),
                query.character,
                query.server,
                query.realm,
            ),
        )
    )
    merged_bindings = [
        {**query.to_dict(), "expected_guid": expected_guids[query]}
        for query in ordered
        if query in expected_guids
    ]
    source = {
        "manual_character_query_count": len(manual),
        "timeline": deepcopy(timeline_result.source) if timeline_result else None,
        "manual_timeline_exact_overlap_count": overlap,
        "merged_character_query_count": len(ordered),
        "merged_expected_guid_binding_count": len(merged_bindings),
        "merged_expected_guid_bindings": merged_bindings,
        "merged_expected_guid_bindings_sha256": hashlib.sha256(
            _canonical_bytes(merged_bindings)
        ).hexdigest(),
        "merge_contract": {
            "exact_query_overlap_deduplicated": True,
            "duplicate_manual_queries_rejected": True,
            "timeline_guid_name_conflicts_rejected": True,
            "server_realm_inference_from_timeline": False,
        },
    }
    return ResolvedCharacterQueries(
        queries=ordered,
        expected_guids=expected_guids,
        source=source,
    )


def _normalized_expected_guids(
    queries: Sequence[history_v1.CharacterQuery],
    expected_guids: Mapping[history_v1.CharacterQuery, str] | None,
) -> dict[history_v1.CharacterQuery, str]:
    if expected_guids is None:
        return {}
    if not isinstance(expected_guids, Mapping):
        raise CharacterSyncError("expected_guids must be a mapping")
    allowed = set(queries)
    output: dict[history_v1.CharacterQuery, str] = {}
    for query, raw_guid in expected_guids.items():
        if not isinstance(query, history_v1.CharacterQuery) or query not in allowed:
            raise CharacterSyncError("expected GUID binding contains an unknown query")
        guid = _required_text(raw_guid, label="expected_guid")
        if (
            history_v1.CHARACTER_GUID_PATTERN.fullmatch(guid) is None
            or int(guid[2:], 16) == 0
        ):
            raise CharacterSyncError(f"invalid expected character GUID {guid!r}")
        output[query] = "0x" + guid[2:].upper()
    return output


def _manual_query_source(
    queries: Sequence[history_v1.CharacterQuery],
    expected_guids: Mapping[history_v1.CharacterQuery, str],
) -> dict[str, Any]:
    bindings = [
        {**query.to_dict(), "expected_guid": expected_guids[query]}
        for query in sorted(expected_guids)
    ]
    return {
        "manual_character_query_count": len(queries),
        "timeline": None,
        "manual_timeline_exact_overlap_count": 0,
        "merged_character_query_count": len(queries),
        "merged_expected_guid_binding_count": len(bindings),
        "merged_expected_guid_bindings": bindings,
        "merged_expected_guid_bindings_sha256": hashlib.sha256(
            _canonical_bytes(bindings)
        ).hexdigest(),
        "merge_contract": {
            "exact_query_overlap_deduplicated": True,
            "duplicate_manual_queries_rejected": True,
            "timeline_guid_name_conflicts_rejected": True,
            "server_realm_inference_from_timeline": False,
        },
    }


def _query_source_output(
    queries: Sequence[history_v1.CharacterQuery],
    expected_guids: Mapping[history_v1.CharacterQuery, str],
    query_source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if query_source is None:
        return _manual_query_source(queries, expected_guids)
    if not isinstance(query_source, Mapping):
        raise CharacterSyncError("query_source must be a mapping")
    source = deepcopy(dict(query_source))
    if source.get("merged_character_query_count") != len(queries):
        raise CharacterSyncError("query_source count differs from resolved queries")
    if source.get("merged_expected_guid_binding_count") != len(expected_guids):
        raise CharacterSyncError("query_source expected-GUID count mismatch")
    bindings = source.get("merged_expected_guid_bindings")
    if not isinstance(bindings, list):
        raise CharacterSyncError("query_source expected-GUID bindings must be a list")
    observed: dict[history_v1.CharacterQuery, str] = {}
    for row in bindings:
        if not isinstance(row, Mapping):
            raise CharacterSyncError("query_source expected-GUID binding must be an object")
        try:
            query = history_v1.CharacterQuery(
                server=row["server"],
                realm=row["realm"],
                character=row["character"],
            )
        except (KeyError, history_v1.CharacterHistoryError) as error:
            raise CharacterSyncError(
                "query_source expected-GUID binding has an invalid query"
            ) from error
        guid = _required_text(row.get("expected_guid"), label="expected_guid")
        if query in observed:
            raise CharacterSyncError("query_source repeats an expected-GUID binding")
        observed[query] = guid
    if {
        query: guid.lower() for query, guid in observed.items()
    } != {
        query: guid.lower() for query, guid in expected_guids.items()
    }:
        raise CharacterSyncError("query_source expected-GUID bindings disagree")
    binding_hash = source.get("merged_expected_guid_bindings_sha256")
    if binding_hash != hashlib.sha256(_canonical_bytes(bindings)).hexdigest():
        raise CharacterSyncError("query_source expected-GUID binding hash mismatch")
    return source


def _validate_live_identities(
    characters: Sequence[Mapping[str, Any]],
    expected_guids: Mapping[history_v1.CharacterQuery, str],
) -> None:
    if not expected_guids:
        return
    seen: set[history_v1.CharacterQuery] = set()
    for row in characters:
        query_raw = row.get("query")
        identity = row.get("identity")
        if not isinstance(query_raw, Mapping) or not isinstance(identity, Mapping):
            raise CharacterSyncError("live character row lacks query/identity")
        query = history_v1.CharacterQuery(
            server=query_raw.get("server"),
            realm=query_raw.get("realm"),
            character=query_raw.get("character"),
        )
        expected = expected_guids.get(query)
        if expected is None:
            continue
        actual = identity.get("guid")
        if not isinstance(actual, str) or actual.lower() != expected.lower():
            raise CharacterSyncError(
                f"live GUID mismatch for {query.server}/{query.realm}/"
                f"{query.character}: expected {expected}, received {actual!r}"
            )
        seen.add(query)
    missing = sorted(set(expected_guids) - seen)
    if missing:
        raise CharacterSyncError(
            "live identity response is missing expected GUID bindings for "
            + ", ".join(query.character for query in missing)
        )


class _ExpectedGuidClient:
    """Validate a timeline-derived GUID immediately after identity retrieval.

    The guard runs inside the history fetcher's identity call, before that
    fetcher can request even page 1 of ``/instances``.  The completed live
    document is validated again after pagination as a second line of defence.
    """

    def __init__(
        self,
        client: ingest_v1.ChronicleClient,
        expected_guids: Mapping[history_v1.CharacterQuery, str],
    ) -> None:
        self._client = client
        self.api_base = client.api_base
        self._expected_by_path = {
            query.api_path: (query, guid)
            for query, guid in expected_guids.items()
        }

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    ) -> tuple[ingest_v1.HTTPResponse, Any]:
        response, value = self._client.get_json(path, params=params)
        binding = self._expected_by_path.get(path)
        if binding is None:
            return response, value
        query, expected = binding
        actual = value.get("guid") if isinstance(value, Mapping) else None
        if not isinstance(actual, str) or actual.lower() != expected.lower():
            raise CharacterSyncError(
                f"live GUID mismatch for {query.server}/{query.realm}/"
                f"{query.character}: expected {expected}, received {actual!r}"
            )
        return response, value


def _query_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    try:
        return (
            str(value["server"]),
            str(value["realm"]),
            str(value["character"]),
        )
    except KeyError as error:
        raise CharacterSyncError("history manifest query is incomplete") from error


def _requested_query_keys(
    queries: Sequence[history_v1.CharacterQuery],
) -> set[tuple[str, str, str]]:
    return {
        (query.server, query.realm, query.character)
        for query in _normalized_queries(queries)
    }


def _raw_root(data_root: Path) -> Path:
    try:
        return ingest_v1._ensure_offline_root(Path(data_root))
    except ingest_v1.ChronicleIngestError as error:
        raise CharacterSyncError(str(error)) from error


def discover_character_manifests(
    data_root: Path,
    queries: Sequence[history_v1.CharacterQuery],
) -> list[Path]:
    """Return current verified snapshots whose query set exactly matches scope.

    Superseded, immutable history revisions are migration inputs, not current
    evidence.  Discovery skips them before strict current replay so their
    presence cannot break or contaminate an incremental sync.
    """

    wanted = _requested_query_keys(queries)
    directory = _raw_root(Path(data_root)) / "character_history_manifests"
    output: list[Path] = []
    for path in sorted(directory.glob("chronicle_external_character_history_v1.*.manifest.json")):
        try:
            candidate = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CharacterSyncError(
                f"cannot inspect character history manifest {path}: {error}"
            ) from error
        if not isinstance(candidate, dict):
            raise CharacterSyncError(
                f"character history manifest {path} must be a JSON object"
            )
        revision = candidate.get("implementation_revision")
        if (
            candidate.get("schema") == history_v1.SCHEMA
            and candidate.get("kind") == history_v1.KIND
            and revision == history_v1.LEGACY_IMPLEMENTATION_REVISION
        ):
            continue
        if revision != history_v1.IMPLEMENTATION_REVISION:
            raise CharacterSyncError(
                f"unsupported character history revision in {path}: {revision!r}"
            )
        try:
            manifest, resolved = history_v1.load_character_history_manifest(
                path, data_root=Path(data_root)
            )
        except history_v1.CharacterHistoryError as error:
            raise CharacterSyncError(
                f"invalid character history manifest {path}: {error}"
            ) from error
        present = {
            _query_key(character["query"])
            for character in manifest["characters"]
        }
        # Exact scope matching prevents a sync for one requested player from
        # accidentally fetching gaps belonging to another player captured in
        # an unrelated multi-character snapshot.
        if present == wanted:
            output.append(resolved)
    return output


def discover_ingest_manifests(data_root: Path) -> list[Path]:
    """Return only current-revision candidates; strict replay follows in inventory.

    Legacy raw snapshots remain immutable on disk.  They are not silently
    treated as current evidence: a separately replayed current-revision
    manifest can be selected, while an unreplayed legacy-only object simply
    remains a gap and is fetched through the bounded current ingest path.
    """

    directory = _raw_root(Path(data_root)) / "manifests"
    output: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CharacterSyncError(f"cannot inspect ingest manifest {path}: {error}") from error
        if not isinstance(value, dict):
            raise CharacterSyncError(f"ingest manifest {path} must be a JSON object")
        if (
            value.get("schema") == ingest_v1.SCHEMA
            and value.get("implementation_revision")
            == ingest_v1.IMPLEMENTATION_REVISION
            and value.get("parser_contract_revision")
            == ingest_v1.PARSER_CONTRACT_REVISION
            and value.get("kind") == "chronicle_external_api_raw_snapshot"
        ):
            output.append(path.resolve())
    return output


def _known_memberships(
    manifest_paths: Sequence[Path],
    *,
    data_root: Path,
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for path in manifest_paths:
        manifest, _ = history_v1.load_character_history_manifest(
            path, data_root=Path(data_root)
        )
        for character in manifest["characters"]:
            guid = str(character["identity"]["guid"]).lower()
            for instance in character["instances"]:
                key = (guid, str(instance["instance_id"]))
                previous = rows.get(key)
                if previous is not None and previous != instance:
                    # Multiple immutable history versions are legitimate.  For
                    # cursor/delta purposes keep the record with the newest
                    # uploaded_at; the inventory performs the stronger version
                    # audit before any gap execution.
                    prior_time = ingest_v1._parse_rfc3339(
                        previous["uploaded_at"], field="history.uploaded_at"
                    )
                    current_time = ingest_v1._parse_rfc3339(
                        instance["uploaded_at"], field="history.uploaded_at"
                    )
                    if current_time < prior_time:
                        continue
                rows[key] = deepcopy(instance)
    return [rows[key] for key in sorted(rows)]


def _normalize_upload_after(value: str) -> str:
    try:
        parsed = ingest_v1._parse_rfc3339(value, field="upload_after")
    except ingest_v1.ChronicleIngestError as error:
        raise CharacterSyncError(str(error)) from error
    return ingest_v1._rfc3339_utc(parsed)


def derive_upload_after(
    memberships: Sequence[Mapping[str, Any]],
    *,
    explicit_upload_after: str | None,
) -> tuple[str, set[str]]:
    """Choose an inclusive ``uploaded_at`` cursor and its known boundary IDs."""

    if explicit_upload_after is not None:
        cursor = _normalize_upload_after(explicit_upload_after)
    else:
        timestamps = [
            ingest_v1._parse_rfc3339(row.get("uploaded_at"), field="history.uploaded_at")
            for row in memberships
        ]
        if not timestamps:
            raise CharacterSyncSafetyLimitError(
                "no prior character snapshot exists; bootstrap with --upload-after"
            )
        cursor = ingest_v1._rfc3339_utc(max(timestamps))
    cursor_dt = ingest_v1._parse_rfc3339(cursor, field="upload_after")
    boundary = {
        str(row["instance_id"])
        for row in memberships
        if ingest_v1._parse_rfc3339(
            row.get("uploaded_at"), field="history.uploaded_at"
        )
        == cursor_dt
    }
    return cursor, boundary


def probe_recent_uploads(
    *,
    client: ingest_v1.ChronicleClient,
    upload_after: str,
    known_boundary_instance_ids: Sequence[str] = (),
    instance_names: Sequence[str] = (),
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Read a complete recent window and remove its inclusive known boundary."""

    cursor = _normalize_upload_after(upload_after)
    cursor_dt = ingest_v1._parse_rfc3339(cursor, field="upload_after")
    try:
        pages, has_more = ingest_v1.fetch_recent_pages(
            client,
            upload_after=cursor,
            instance_names=tuple(sorted(set(instance_names))),
            page_size=page_size,
            max_pages=max_pages,
            require_complete=True,
        )
    except ingest_v1.ChronicleIngestError as error:
        raise CharacterSyncError(f"recent upload probe failed: {error}") from error
    if has_more:  # pragma: no cover - require_complete already fails closed
        raise CharacterSyncSafetyLimitError("recent upload probe was unexpectedly truncated")

    activities: dict[str, dict[str, Any]] = {}
    raw_count = 0
    for _, page in pages:
        for raw in page["activities"]:
            raw_count += 1
            try:
                activity = ingest_v1._activity_record(raw)
            except ingest_v1.ChronicleIngestError as error:
                raise CharacterSyncError(str(error)) from error
            if ingest_v1._parse_rfc3339(
                activity["uploaded_at_utc"], field="activity.uploaded_at"
            ) < cursor_dt:
                raise CharacterSyncError(
                    "recent API returned an uploaded_at before upload_after"
                )
            instance_id = activity["instance_id"]
            previous = activities.get(instance_id)
            if previous is not None and previous != activity:
                raise CharacterSyncError(
                    f"recent pages disagree about instance {instance_id}"
                )
            activities[instance_id] = activity

    known_boundary = set(known_boundary_instance_ids)
    boundary_duplicates = sorted(
        instance_id
        for instance_id, row in activities.items()
        if row["uploaded_at_utc"] == cursor and instance_id in known_boundary
    )
    fresh = {
        instance_id: row
        for instance_id, row in activities.items()
        if instance_id not in boundary_duplicates
    }
    ordered = sorted(
        fresh.values(),
        key=lambda row: (row["uploaded_at_utc"], row["instance_id"]),
    )
    latest = cursor_dt
    if activities:
        latest = max(
            latest,
            *(
                ingest_v1._parse_rfc3339(
                    row["uploaded_at_utc"], field="activity.uploaded_at"
                )
                for row in activities.values()
            ),
        )
    return {
        "query_parameter": "upload_after",
        "upload_after": cursor,
        "instance_names": sorted(set(instance_names)),
        "page_count": len(pages),
        "raw_activity_count": raw_count,
        "unique_activity_count": len(activities),
        "inclusive_boundary_activity_ids": sorted(
            instance_id
            for instance_id, row in activities.items()
            if row["uploaded_at_utc"] == cursor
        ),
        "inclusive_boundary_duplicate_ids": boundary_duplicates,
        "unknown_boundary_activity_ids": sorted(
            instance_id
            for instance_id, row in activities.items()
            if row["uploaded_at_utc"] == cursor
            and instance_id not in known_boundary
        ),
        "strictly_after_activity_ids": sorted(
            instance_id
            for instance_id, row in activities.items()
            if ingest_v1._parse_rfc3339(
                row["uploaded_at_utc"], field="activity.uploaded_at"
            )
            > cursor_dt
        ),
        "new_activity_ids": [row["instance_id"] for row in ordered],
        "next_upload_after": ingest_v1._rfc3339_utc(latest),
        "complete": True,
    }


def _evidence_tier(value: str) -> str:
    if value not in EVIDENCE_TIERS:
        raise CharacterSyncError(
            f"evidence_tier must be one of {', '.join(EVIDENCE_TIERS)}"
        )
    return value


def _evidence_tier_contract(value: str) -> dict[str, Any]:
    tier = _evidence_tier(value)
    return {
        "selected": tier,
        "history": "capture character identity and fully paginated history only",
        "ranking": "capture history plus missing metadata and exact ranking only",
        "eligible-streams": (
            "capture history/ranking and fetch missing event streams only for "
            "raid-level training-eligible contamination labels"
        ),
        "default": "eligible-streams",
    }


def build_gap_batches(
    inventory: Mapping[str, Any],
    *,
    evidence_tier: str = "eligible-streams",
) -> list[GapBatch]:
    """Build exact per-gap batches, de-duplicating shared character raids."""

    tier = _evidence_tier(evidence_tier)
    if inventory.get("schema") != history_v1.INVENTORY_SCHEMA:
        raise CharacterSyncError("unsupported character inventory schema")
    if tier == "history":
        return []
    if (
        inventory.get("implementation_revision")
        != history_v1.INVENTORY_IMPLEMENTATION_REVISION
    ):
        raise CharacterSyncError(
            "gap planning requires the current ranking-and-stream-negative-evidence "
            "inventory revision; rebuild legacy inventory from bound sources"
        )
    aggregate: dict[str, dict[str, Any]] = {}
    for row in inventory.get("instances", []):
        if not isinstance(row, dict):
            raise CharacterSyncError("inventory instances must be objects")
        instance_id = str(row.get("instance_id", ""))
        if not instance_id or instance_id in history_v1.ZERO_INSTANCE_IDS:
            raise CharacterSyncError("inventory contains an invalid instance id")
        label = str(row.get("contamination_label", ""))
        if label not in history_v1.TRAINING_CANDIDATE_LABELS | history_v1.NONTRAINING_LABELS:
            raise CharacterSyncError(f"unsupported contamination label {label!r}")
        plan = row.get("missing_plan")
        if not isinstance(plan, dict):
            raise CharacterSyncError("inventory row lacks missing_plan")
        ranking_fetch_required = plan.get("ranking_fetch_required")
        if not isinstance(ranking_fetch_required, bool):
            raise CharacterSyncError(
                "inventory row lacks boolean ranking_fetch_required"
            )
        current = aggregate.setdefault(
            instance_id,
            {
                "labels": set(),
                "metadata": False,
                "ranking": False,
                "streams": set(),
                "terminal_streams": set(),
            },
        )
        current["labels"].add(label)
        current["metadata"] = current["metadata"] or bool(plan.get("metadata"))
        current["ranking"] = current["ranking"] or bool(
            ranking_fetch_required
        )
        semantic_missing_streams = plan.get("required_streams")
        fetch_required_streams = plan.get("required_streams_fetch_required")
        terminal_streams = plan.get(
            "required_streams_terminal_unavailable_404"
        )
        for field_name, streams in (
            ("required_streams", semantic_missing_streams),
            ("required_streams_fetch_required", fetch_required_streams),
            (
                "required_streams_terminal_unavailable_404",
                terminal_streams,
            ),
        ):
            if not isinstance(streams, list):
                raise CharacterSyncError(f"missing {field_name} must be a list")
            if streams != sorted(set(streams)):
                raise CharacterSyncError(
                    f"missing {field_name} must be unique and sorted"
                )
            for stream in streams:
                if stream not in ingest_v1.EVENT_STREAM_TYPES:
                    raise CharacterSyncError(f"unsupported missing stream {stream!r}")
        semantic_set = set(semantic_missing_streams)
        fetch_set = set(fetch_required_streams)
        terminal_set = set(terminal_streams)
        if fetch_set & terminal_set or fetch_set | terminal_set != semantic_set:
            raise CharacterSyncError(
                "required stream semantic absence must be partitioned exactly "
                "into fetch-required and terminal UNAVAILABLE_404 evidence"
            )
        if fetch_set & current["terminal_streams"] or terminal_set & current["streams"]:
            raise CharacterSyncError(
                f"shared raid {instance_id} has conflicting stream capture states"
            )
        for stream in fetch_required_streams:
            if stream not in ingest_v1.EVENT_STREAM_TYPES:
                raise CharacterSyncError(f"unsupported missing stream {stream!r}")
            current["streams"].add(stream)
        current["terminal_streams"].update(terminal_streams)

    grouped: dict[tuple[str, bool, tuple[str, ...]], list[str]] = defaultdict(list)
    for instance_id, missing in sorted(aggregate.items()):
        labels = missing["labels"]
        if len(labels) != 1:
            raise CharacterSyncError(
                f"shared raid {instance_id} has conflicting contamination labels"
            )
        label = next(iter(labels))
        metadata_missing = bool(missing["metadata"])
        ranking_missing = bool(missing["ranking"])
        if tier == "ranking":
            if not (metadata_missing or ranking_missing):
                continue
            key = ("RANKING_INDEX", ranking_missing, ())
        elif label in history_v1.TRAINING_CANDIDATE_LABELS:
            streams = tuple(sorted(missing["streams"]))
            if not (metadata_missing or ranking_missing or streams):
                continue
            key = ("TRAINING_ELIGIBLE", ranking_missing, streams)
        else:
            # The hard gate: event-stream absence alone never schedules a
            # suspect/unknown raid.  Only light metadata/ranking diagnostics
            # can enter this lane, and its stream tuple is structurally empty.
            if not (metadata_missing or ranking_missing):
                continue
            key = ("NONTRAINING_DIAGNOSTIC", ranking_missing, ())
        grouped[key].append(instance_id)

    return [
        GapBatch(
            lane=key[0],
            instance_ids=tuple(sorted(instance_ids)),
            include_ranking_records=key[1],
            stream_types=key[2],
        )
        for key, instance_ids in sorted(grouped.items())
    ]


def _bounded_gap_instance_ids(
    batches: Sequence[GapBatch],
    *,
    max_fetch_instances: int,
) -> set[str]:
    if (
        isinstance(max_fetch_instances, bool)
        or not isinstance(max_fetch_instances, int)
        or max_fetch_instances < 0
    ):
        raise CharacterSyncSafetyLimitError(
            "max_fetch_instances must be a non-negative integer"
        )
    instance_ids = {
        instance_id
        for batch in batches
        for instance_id in batch.instance_ids
    }
    if len(instance_ids) > max_fetch_instances:
        raise CharacterSyncSafetyLimitError(
            f"sync contains {len(instance_ids)} instances, exceeding "
            f"max_fetch_instances={max_fetch_instances}"
        )
    return instance_ids


def _build_inventory(
    *,
    history_paths: Sequence[Path],
    ingest_paths: Sequence[Path],
    data_root: Path,
    instance_name: str | None,
    started_at_not_before: str | None,
    required_streams: Sequence[str],
    max_fetch_instances: int,
) -> dict[str, Any]:
    try:
        return history_v1.build_character_instance_inventory(
            history_paths,
            ingest_paths,
            data_root=Path(data_root),
            instance_name=instance_name,
            started_at_not_before=started_at_not_before,
            required_streams=required_streams,
            max_fetch_instances=max_fetch_instances,
        )
    except history_v1.CharacterHistoryError as error:
        raise CharacterSyncError(f"cannot build verified gap inventory: {error}") from error


def _live_memberships(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for character in value.get("characters", []):
        for instance in character.get("instances", []):
            output.append(deepcopy(instance))
    return output


def _delta_summary(
    live_memberships: Sequence[Mapping[str, Any]],
    known_memberships: Sequence[Mapping[str, Any]],
    recent_probe: Mapping[str, Any],
) -> dict[str, Any]:
    live_ids = {str(row["instance_id"]) for row in live_memberships}
    known_ids = {str(row["instance_id"]) for row in known_memberships}
    new_ids = sorted(live_ids - known_ids)
    no_longer_live = sorted(known_ids - live_ids)
    recent_ids = set(recent_probe["new_activity_ids"])
    return {
        "new_character_instance_ids": new_ids,
        "captured_instance_ids_not_in_live_history": no_longer_live,
        "new_character_ids_seen_in_recent_upload_window": sorted(
            set(new_ids) & recent_ids
        ),
        "new_character_ids_not_seen_in_recent_upload_window": sorted(
            set(new_ids) - recent_ids
        ),
        "identity_source": "characters/{server}/{realm}/{character}/instances.id",
        "leaderboard_instance_id_used": False,
    }


def _live_document_from_fetched(fetched: Sequence[Any]) -> dict[str, Any]:
    characters = [
        {
            "query": row.query.to_dict(),
            "identity": deepcopy(row.identity),
            "page_count": len(row.pages),
            "instances": [
                history_v1._derived_instance(instance, row.identity)
                for instance in row.instances
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
        "schema": f"{history_v1.SCHEMA}/list",
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
        "capture_contract": history_v1._capture_contract(),
        "contamination_contract": history_v1._contamination_contract(),
    }


def _fetch_validated_live_histories(
    queries: Sequence[history_v1.CharacterQuery],
    *,
    client: ingest_v1.ChronicleClient,
    expected_guids: Mapping[history_v1.CharacterQuery, str],
    page_size: int,
    max_pages: int,
    max_instances: int,
) -> tuple[list[Any], dict[str, Any]]:
    guarded_client: Any = (
        _ExpectedGuidClient(client, expected_guids)
        if expected_guids
        else client
    )
    try:
        fetched = history_v1._fetch_character_histories(
            queries,
            client=guarded_client,
            page_size=page_size,
            max_pages=max_pages,
            max_instances=max_instances,
        )
    except history_v1.CharacterHistoryError as error:
        raise CharacterSyncError(f"live character history request failed: {error}") from error
    live = _live_document_from_fetched(fetched)
    _validate_live_identities(live["characters"], expected_guids)
    return fetched, live


def _publish_fetched_histories(
    fetched: Sequence[Any],
    *,
    data_root: Path,
    api_base: str,
) -> dict[str, Any]:
    """Publish already identity-validated response bytes without refetching."""

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
                    suffix=(
                        f"character.instances.page-{pagination['page']}.json"
                    ),
                    media_type="application/json",
                )
                for pagination, raw in row.pages
            ]
            characters.append(
                history_v1._materialize_character(
                    row,
                    identity_object=identity_ref,
                    page_objects=page_refs,
                )
            )
        document = history_v1._content_addressed(
            history_v1._manifest_core(characters, api_base=api_base)
        )
        digest = history_v1._verify_content_address(
            document, label="character history manifest"
        )
        payload = history_v1._canonical_document_bytes(document)
        manifest_path = (
            store.root
            / "character_history_manifests"
            / f"chronicle_external_character_history_v1.{digest}.manifest.json"
        )
        ingest_v1._atomic_write_bytes(manifest_path, payload)
    except (history_v1.CharacterHistoryError, ingest_v1.ChronicleIngestError) as error:
        raise CharacterSyncError(
            f"cannot publish validated character history capture: {error}"
        ) from error
    return {
        "schema": history_v1.SCHEMA,
        "content_sha256": digest,
        "manifest_path": str(manifest_path),
        "character_count": document["summary"]["character_count"],
        "unique_instance_count": document["summary"]["unique_instance_count"],
    }


def audit_character_sync(
    queries: Sequence[history_v1.CharacterQuery],
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    client: ingest_v1.ChronicleClient | None = None,
    upload_after: str | None = None,
    instance_names: Sequence[str] = (),
    instance_name: str | None = None,
    started_at_not_before: str | None = None,
    required_streams: Sequence[str] = DEFAULT_REQUIRED_STREAMS,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_instances: int = DEFAULT_MAX_INSTANCES,
    max_fetch_instances: int = DEFAULT_MAX_FETCH_INSTANCES,
    evidence_tier: str = "eligible-streams",
    expected_guids: Mapping[history_v1.CharacterQuery, str] | None = None,
    query_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform a live, fully paginated audit without writing or downloading streams."""

    normalized = _normalized_queries(queries)
    tier = _evidence_tier(evidence_tier)
    expected = _normalized_expected_guids(normalized, expected_guids)
    source = _query_source_output(normalized, expected, query_source)
    history_paths = discover_character_manifests(Path(data_root), normalized)
    known = _known_memberships(history_paths, data_root=Path(data_root))
    cursor, boundary = derive_upload_after(
        known, explicit_upload_after=upload_after
    )
    api = client or ingest_v1.ChronicleClient()
    _, live = _fetch_validated_live_histories(
        normalized,
        client=api,
        expected_guids=expected,
        page_size=page_size,
        max_pages=max_pages,
        max_instances=max_instances,
    )
    recent_scope = (
        tuple(instance_names)
        if instance_names
        else (instance_name,) if instance_name is not None else ()
    )
    recent = probe_recent_uploads(
        client=api,
        upload_after=cursor,
        known_boundary_instance_ids=sorted(boundary),
        instance_names=recent_scope,
        page_size=page_size,
        max_pages=max_pages,
    )

    inventory = None
    batches: list[GapBatch] = []
    if history_paths and tier != "history":
        # The generic inventory also describes missing streams.  Those stream
        # gaps must not consume the ranking tier's explicit fetch allowance.
        inventory_plan_limit = (
            max_fetch_instances if tier == "eligible-streams" else max_instances
        )
        inventory = _build_inventory(
            history_paths=history_paths,
            ingest_paths=discover_ingest_manifests(Path(data_root)),
            data_root=Path(data_root),
            instance_name=instance_name,
            started_at_not_before=started_at_not_before,
            required_streams=required_streams,
            max_fetch_instances=inventory_plan_limit,
        )
        batches = build_gap_batches(inventory, evidence_tier=tier)
        _bounded_gap_instance_ids(
            batches, max_fetch_instances=max_fetch_instances
        )
    delta = _delta_summary(_live_memberships(live), known, recent)
    gap_ids = sorted({item for batch in batches for item in batch.instance_ids})
    return {
        "schema": f"{SCHEMA}/audit",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "evidence_tier": tier,
        "evidence_tier_contract": _evidence_tier_contract(tier),
        "query_source": source,
        "write_performed": False,
        "network_contract": {
            "evidence_tier": tier,
            "character_history_full_pagination": True,
            "recent_query_parameter": "upload_after",
            "leaderboard_used": False,
            "metadata_requests": 0,
            "ranking_requests": 0,
            "event_stream_requests": 0,
        },
        "eligibility_contract": history_v1._contamination_contract(),
        "idempotency_contract": (
            "strict manifest replay verifies sha256 and size before existing evidence "
            "is excluded from the gap plan"
        ),
        "recent_probe": recent,
        "live_summary": deepcopy(live["summary"]),
        "history_delta": delta,
        "inventory_summary": (
            deepcopy(inventory["summary"]) if inventory is not None else None
        ),
        "gap_batches": [batch.to_dict() for batch in batches],
        "status": (
            "NO_GAPS"
            if not gap_ids and not delta["new_character_instance_ids"]
            else "SYNC_REQUIRED"
        ),
    }


def sync_character_histories(
    queries: Sequence[history_v1.CharacterQuery],
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    client: ingest_v1.ChronicleClient | None = None,
    upload_after: str | None = None,
    instance_names: Sequence[str] = (),
    instance_name: str | None = None,
    started_at_not_before: str | None = None,
    required_streams: Sequence[str] = DEFAULT_REQUIRED_STREAMS,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_instances: int = DEFAULT_MAX_INSTANCES,
    max_fetch_instances: int = DEFAULT_MAX_FETCH_INSTANCES,
    publish_inventory: bool = True,
    evidence_tier: str = "eligible-streams",
    expected_guids: Mapping[history_v1.CharacterQuery, str] | None = None,
    query_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture current histories and fetch only verified, eligible evidence gaps."""

    normalized = _normalized_queries(queries)
    tier = _evidence_tier(evidence_tier)
    expected = _normalized_expected_guids(normalized, expected_guids)
    source = _query_source_output(normalized, expected, query_source)
    before_paths = discover_character_manifests(Path(data_root), normalized)
    known_before = _known_memberships(before_paths, data_root=Path(data_root))
    cursor, boundary = derive_upload_after(
        known_before, explicit_upload_after=upload_after
    )
    api = client or ingest_v1.ChronicleClient()
    fetched, live = _fetch_validated_live_histories(
        normalized,
        client=api,
        expected_guids=expected,
        page_size=page_size,
        max_pages=max_pages,
        max_instances=max_instances,
    )
    recent_scope = (
        tuple(instance_names)
        if instance_names
        else (instance_name,) if instance_name is not None else ()
    )
    recent = probe_recent_uploads(
        client=api,
        upload_after=cursor,
        known_boundary_instance_ids=sorted(boundary),
        instance_names=recent_scope,
        page_size=page_size,
        max_pages=max_pages,
    )
    capture = _publish_fetched_histories(
        fetched,
        data_root=Path(data_root),
        api_base=api.api_base,
    )

    history_paths = discover_character_manifests(Path(data_root), normalized)
    if Path(capture["manifest_path"]).resolve() not in history_paths:
        raise CharacterSyncError("fresh character manifest is missing from verified scope")
    delta = _delta_summary(_live_memberships(live), known_before, recent)
    if tier == "history":
        return {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "evidence_tier": tier,
            "evidence_tier_contract": _evidence_tier_contract(tier),
            "query_source": source,
            "status": "COMPLETE",
            "network_contract": {
                "evidence_tier": tier,
                "recent_query_parameter": "upload_after",
                "character_history_full_pagination": True,
                "leaderboard_used": False,
                "ranking_requests": 0,
                "event_stream_requests": 0,
            },
            "eligibility_contract": history_v1._contamination_contract(),
            "recent_probe": recent,
            "history_delta": delta,
            "character_capture": capture,
            "initial_inventory_summary": None,
            "executed_batches": [],
            "final_inventory_summary": None,
            "remaining_gap_batches": [],
            "inventory_publication": None,
        }
    ingest_paths = discover_ingest_manifests(Path(data_root))
    inventory_plan_limit = (
        max_fetch_instances if tier == "eligible-streams" else max_instances
    )
    initial_inventory = _build_inventory(
        history_paths=history_paths,
        ingest_paths=ingest_paths,
        data_root=Path(data_root),
        instance_name=instance_name,
        started_at_not_before=started_at_not_before,
        required_streams=required_streams,
        max_fetch_instances=inventory_plan_limit,
    )
    batches = build_gap_batches(initial_inventory, evidence_tier=tier)
    _bounded_gap_instance_ids(
        batches, max_fetch_instances=max_fetch_instances
    )

    executions: list[dict[str, Any]] = []
    for index, batch in enumerate(batches, start=1):
        if tier != "eligible-streams" and batch.stream_types:
            raise CharacterSyncError(
                f"{tier} evidence tier requested event streams"
            )
        if batch.lane == "NONTRAINING_DIAGNOSTIC" and batch.stream_types:
            raise CharacterSyncError("nontraining diagnostic batch requested event streams")
        try:
            result = ingest_v1.ingest_external_api(
                data_root=Path(data_root),
                client=api,
                explicit_instance_ids=batch.instance_ids,
                max_instances=len(batch.instance_ids),
                page_size=1,
                max_pages=1,
                stream_types=batch.stream_types,
                include_ranking_records=batch.include_ranking_records,
                scope_name=f"character_sync_{batch.lane.lower()}_{index}",
            )
        except ingest_v1.ChronicleIngestError as error:
            raise CharacterSyncError(
                f"{batch.lane} gap ingest failed for {batch.instance_ids}: {error}"
            ) from error
        executions.append({"batch": batch.to_dict(), "result": result})

    final_ingest_paths = discover_ingest_manifests(Path(data_root))
    final_inventory = _build_inventory(
        history_paths=history_paths,
        ingest_paths=final_ingest_paths,
        data_root=Path(data_root),
        instance_name=instance_name,
        started_at_not_before=started_at_not_before,
        required_streams=required_streams,
        max_fetch_instances=inventory_plan_limit,
    )
    remaining = build_gap_batches(final_inventory, evidence_tier=tier)
    publication = None
    if publish_inventory:
        try:
            publication = history_v1.publish_character_instance_inventory(
                history_paths,
                final_ingest_paths,
                data_root=Path(data_root),
                instance_name=instance_name,
                started_at_not_before=started_at_not_before,
                required_streams=required_streams,
                max_fetch_instances=inventory_plan_limit,
            )
        except history_v1.CharacterHistoryError as error:
            raise CharacterSyncError(f"inventory publication failed: {error}") from error

    return {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "evidence_tier": tier,
        "evidence_tier_contract": _evidence_tier_contract(tier),
        "query_source": source,
        "status": "COMPLETE" if not remaining else "REMAINING_GAPS",
        "network_contract": {
            "evidence_tier": tier,
            "recent_query_parameter": "upload_after",
            "character_history_full_pagination": True,
            "leaderboard_used": False,
            "ranking_identity": "exact character GUID",
            "nontraining_event_stream_downloads": 0,
            "metadata_requests": sum(len(batch.instance_ids) for batch in batches),
            "ranking_requests": sum(
                len(batch.instance_ids)
                for batch in batches
                if batch.include_ranking_records
            ),
            "event_stream_requests": sum(
                len(batch.instance_ids) * len(batch.stream_types)
                for batch in batches
            ),
        },
        "eligibility_contract": history_v1._contamination_contract(),
        "idempotency_contract": (
            "strict manifest replay verifies sha256 and size before existing evidence "
            "is excluded from the gap plan"
        ),
        "recent_probe": recent,
        "history_delta": delta,
        "character_capture": capture,
        "initial_inventory_summary": deepcopy(initial_inventory["summary"]),
        "executed_batches": executions,
        "final_inventory_summary": deepcopy(final_inventory["summary"]),
        "remaining_gap_batches": [batch.to_dict() for batch in remaining],
        "inventory_publication": publication,
    }


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--player",
        action="append",
        nargs=3,
        metavar=("SERVER", "REALM", "CHARACTER"),
        default=[],
    )
    parser.add_argument(
        "--players-from-team-timeline-manifest",
        type=Path,
        help=(
            "derive warrior names and expected GUIDs from a verified External "
            "team-timeline/v2 manifest"
        ),
    )
    parser.add_argument(
        "--default-server",
        help="required explicit server for timeline-derived character paths",
    )
    parser.add_argument(
        "--default-realm",
        help="required explicit realm for timeline-derived character paths",
    )
    parser.add_argument(
        "--evidence-tier",
        choices=EVIDENCE_TIERS,
        default="eligible-streams",
        help=(
            "history: character snapshots only; ranking: metadata/exact ranking only; "
            "eligible-streams: current full eligibility-gated behavior"
        ),
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--upload-after",
        help=(
            "RFC3339 uploaded_at cursor; required only for bootstrap when no matching "
            "character snapshot exists"
        ),
    )
    parser.add_argument("--recent-instance-name", action="append", default=[])
    parser.add_argument("--instance-name")
    parser.add_argument("--started-at-not-before")
    parser.add_argument(
        "--required-stream",
        action="append",
        choices=ingest_v1.EVENT_STREAM_TYPES,
        default=None,
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-instances", type=int, default=DEFAULT_MAX_INSTANCES)
    parser.add_argument(
        "--max-fetch-instances", type=int, default=DEFAULT_MAX_FETCH_INSTANCES
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_external_character_sync_v1",
        description=(
            "Incrementally capture exact Chronicle character/ranking evidence and fetch "
            "only training-eligible event-stream gaps"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="live paginated/read-only gap audit")
    _add_common_arguments(audit)
    sync = commands.add_parser("sync", help="capture and execute bounded eligible gaps")
    _add_common_arguments(sync)
    sync.add_argument("--no-publish-inventory", action="store_true")
    return parser


def _queries_from_args(values: Sequence[Sequence[str]]) -> list[history_v1.CharacterQuery]:
    return [
        history_v1.CharacterQuery(
            server=value[0], realm=value[1], character=value[2]
        )
        for value in values
    ]


def _write_output(
    value: Any,
    *,
    stdout: TextIO | None,
    stdout_buffer: BinaryIO | None,
) -> None:
    payload = _canonical_bytes(value)
    if stdout is not None:
        if stdout_buffer is not None:
            raise CharacterSyncError("stdout and stdout_buffer are mutually exclusive")
        stdout.write(payload.decode("utf-8"))
        return
    target = stdout_buffer if stdout_buffer is not None else getattr(sys.stdout, "buffer", None)
    if target is not None:
        target.write(payload)
    else:  # pragma: no cover - normal CLI exposes sys.stdout.buffer
        sys.stdout.write(payload.decode("utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdout_buffer: BinaryIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    required = (
        args.required_stream
        if args.required_stream is not None
        else DEFAULT_REQUIRED_STREAMS
    )
    resolved = resolve_character_queries(
        _queries_from_args(args.player),
        players_from_team_timeline_manifest=(
            args.players_from_team_timeline_manifest
        ),
        default_server=args.default_server,
        default_realm=args.default_realm,
    )
    common = {
        "data_root": args.data_root,
        "upload_after": args.upload_after,
        "instance_names": args.recent_instance_name,
        "instance_name": args.instance_name,
        "started_at_not_before": args.started_at_not_before,
        "required_streams": required,
        "page_size": args.page_size,
        "max_pages": args.max_pages,
        "max_instances": args.max_instances,
        "max_fetch_instances": args.max_fetch_instances,
        "evidence_tier": args.evidence_tier,
        "expected_guids": resolved.expected_guids,
        "query_source": resolved.source,
    }
    queries = resolved.queries
    if args.command == "audit":
        value = audit_character_sync(queries, **common)
    elif args.command == "sync":
        value = sync_character_histories(
            queries,
            publish_inventory=not args.no_publish_inventory,
            **common,
        )
    else:  # pragma: no cover - argparse enforces the command set
        raise CharacterSyncError(f"unsupported command {args.command!r}")
    _write_output(value, stdout=stdout, stdout_buffer=stdout_buffer)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except CharacterSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
