"""Bounded, resumable ingestion for Chronicle's public External API.

The module deliberately stores API responses as immutable raw objects under
``offline_data``.  It publishes a deterministic manifest only after every
selected response is durable, then advances an ``uploaded_at`` watermark.
``started_at`` is retained for scientific provenance and the documented range
bug label, but is never used as the incremental cursor.

Chronicle event responses are kept as the original gzip-compressed protobuf
streams.  Decoding belongs to downstream modules which implement Chronicle's
custom encounter framing and protobuf schemas.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import email.utils
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SCHEMA = "chronicle_external_api_ingest/v1"
WATERMARK_SCHEMA = "chronicle_external_api_watermark/v1"
LEGACY_IMPLEMENTATION_REVISION = "chronicle_external_api_ingest_v1.1"
LEGACY_PARSER_CONTRACT_REVISION = "chronicle_external_api_real_schema_v2"
IMPLEMENTATION_REVISION = (
    "chronicle_external_api_ingest_v1.2_range_bug_boundary_20260903_noon"
)
PARSER_CONTRACT_REVISION = (
    "chronicle_external_api_real_schema_v3_range_bug_boundary_20260903_noon"
)
SUPPORTED_REPLAY_REVISION_PAIRS = frozenset(
    {
        (LEGACY_IMPLEMENTATION_REVISION, LEGACY_PARSER_CONTRACT_REVISION),
        (IMPLEMENTATION_REVISION, PARSER_CONTRACT_REVISION),
    }
)
EXTERNAL_API_BASE = "https://capy.chronicleclassic.com/api/external/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_RAW_ROOT = DEFAULT_DATA_ROOT / "chronicle_raw" / "external_api" / "v1"

# Names are taken verbatim from Chronicle's official event-stream contract.
# ``ressurection`` is intentionally the API's historical misspelling.
EVENT_STREAM_TYPES = (
    "damage",
    "heal",
    "resource_change",
    "extra_attack",
    "slain",
    "ressurection",
    "cast",
    "aura",
    "spell_go",
    "aura_cast",
    "spell_start",
    "spell_fail",
    "unit_classification",
    "combatant_info",
    "dispel",
    "interrupt",
    "absorbed",
    "companion_stats",
    "consume",
)

SUSPECT_36YD_RANGE_BUG = "SUSPECT_36YD_RANGE_BUG"
RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING = (
    "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING"
)
POSTFIX_KNOWN_CLEAN = "POSTFIX_KNOWN_CLEAN"
NO_KNOWN_RULE_MATCH = "NO_KNOWN_RULE_MATCH"
UNKNOWN_NONVOTING = "UNKNOWN_NONVOTING"
RANGE_BUG_CUTOFF_TIMEZONE = "Asia/Shanghai"
LEGACY_RANGE_BUG_CUTOFF_LOCAL = "2026-09-01T00:00:00+08:00"
RANGE_BUG_SUSPECT_BEFORE_LOCAL = "2026-09-03T00:00:00+08:00"
RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL = "2026-09-03T12:00:00+08:00"
# Retained as a compatibility alias for consumers which render one inclusive
# clean boundary.  New contracts must also expose the preceding uncertain
# window instead of presenting this value as an exact patch timestamp.
RANGE_BUG_CUTOFF_LOCAL = RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL
_CHINA_TZ = timezone(timedelta(hours=8), name=RANGE_BUG_CUTOFF_TIMEZONE)
_LEGACY_RANGE_BUG_CUTOFF = datetime(2026, 9, 1, tzinfo=_CHINA_TZ)
_RANGE_BUG_SUSPECT_BEFORE = datetime(2026, 9, 3, tzinfo=_CHINA_TZ)
_RANGE_BUG_POSTFIX_AT_OR_AFTER = datetime(
    2026, 9, 3, 12, tzinfo=_CHINA_TZ
)


class ChronicleIngestError(RuntimeError):
    """The API response, request, or durable output is invalid."""


class ChronicleHTTPError(ChronicleIngestError):
    """Chronicle returned an HTTP status which the caller did not allow."""

    def __init__(self, status_code: int, url: str, message: str):
        super().__init__(f"Chronicle HTTP {status_code} for {url}: {message}")
        self.status_code = status_code
        self.url = url


class ChronicleSafetyLimitError(ChronicleIngestError):
    """A bounded request would be incomplete or unexpectedly large."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


Transport = Callable[[str, str, Mapping[str, str], float], HTTPResponse]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> HTTPResponse:
    request = Request(url, method=method, headers=dict(headers))
    try:
        with urlopen(request, timeout=timeout) as response:
            return HTTPResponse(
                status=int(response.status),
                headers={str(key).lower(): str(value) for key, value in response.headers.items()},
                body=response.read(),
                url=response.geturl(),
            )
    except HTTPError as error:
        try:
            body = error.read()
        except OSError:
            body = b""
        headers_map = (
            {str(key).lower(): str(value) for key, value in error.headers.items()}
            if error.headers is not None
            else {}
        )
        return HTTPResponse(
            status=int(error.code),
            headers=headers_map,
            body=body,
            url=url,
        )


class RequestPacer:
    """Conservative fixed-interval pacing (1.05 s is below 60 requests/min)."""

    def __init__(
        self,
        interval_seconds: float = 1.05,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds < 0:
            raise ChronicleIngestError("request interval must be non-negative")
        self.interval_seconds = float(interval_seconds)
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_request_at: float | None = None

    def acquire(self) -> None:
        now = self._monotonic()
        if self._next_request_at is not None and now < self._next_request_at:
            self._sleep(self._next_request_at - now)
            now = self._monotonic()
        self._next_request_at = now + self.interval_seconds


class ChronicleClient:
    """Small unauthenticated client with pacing and bounded retry behavior."""

    def __init__(
        self,
        *,
        api_base: str = EXTERNAL_API_BASE,
        timeout_seconds: float = 60.0,
        max_attempts: int = 5,
        request_interval_seconds: float = 1.05,
        transport: Transport = _default_transport,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not api_base.lower().startswith("https://"):
            raise ChronicleIngestError("Chronicle API base must use HTTPS")
        if timeout_seconds <= 0 or max_attempts < 1:
            raise ChronicleIngestError("timeout and max_attempts must be positive")
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = int(max_attempts)
        self._transport = transport
        self._sleep = sleep
        self._now = now
        self._pacer = RequestPacer(
            request_interval_seconds, monotonic=monotonic, sleep=sleep
        )

    @staticmethod
    def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
        if value is None:
            return None
        stripped = value.strip()
        try:
            return max(0.0, float(stripped))
        except ValueError:
            pass
        try:
            parsed = email.utils.parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed.astimezone(timezone.utc) - now).total_seconds())

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        accept: str = "application/json",
        allowed_statuses: Iterable[int] = (),
    ) -> HTTPResponse:
        if not path.startswith("/"):
            raise ChronicleIngestError("API path must begin with /")
        query = urlencode(params or {}, doseq=True)
        url = f"{self.api_base}{path}" + (f"?{query}" if query else "")
        allowed = set(int(value) for value in allowed_statuses)
        headers = {
            "Accept": accept,
            "User-Agent": "BrainOfCat-o2o-dps-research-ingest/1",
        }
        # There is deliberately no Authorization, Cookie, or API-key hook.
        retryable_statuses = {429, 500, 502, 503, 504}
        last_transport_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pacer.acquire()
            try:
                response = self._transport("GET", url, headers, self.timeout_seconds)
            except (URLError, TimeoutError, OSError) as error:
                last_transport_error = error
                if attempt == self.max_attempts:
                    break
                self._sleep(min(30.0, float(2 ** (attempt - 1))))
                continue

            if 200 <= response.status < 300 or response.status in allowed:
                return response
            if response.status not in retryable_statuses or attempt == self.max_attempts:
                message = response.body[:256].decode("utf-8", errors="replace")
                raise ChronicleHTTPError(response.status, url, message)
            retry_after = self._retry_after_seconds(
                _header(response.headers, "retry-after"), self._now()
            )
            self._sleep(
                retry_after
                if retry_after is not None
                else min(30.0, float(2 ** (attempt - 1)))
            )
        raise ChronicleIngestError(
            f"Chronicle transport failed after {self.max_attempts} attempts for {url}: "
            f"{last_transport_error}"
        )

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    ) -> tuple[HTTPResponse, Any]:
        response = self.get(path, params=params)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChronicleIngestError(
                f"Chronicle returned invalid JSON for {response.url}: {error}"
            ) from error
        return response, value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_rfc3339(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleIngestError(f"{field} must be a non-empty RFC3339 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    # Python 3.10 rejects otherwise valid RFC3339 fractions unless they have
    # exactly three or six digits.  Chronicle emits values such as ``.88Z``;
    # right-padding preserves the instant and keeps the parser identical on
    # the Windows 3.13 control host and the Linux 3.10 compute nodes.
    normalized = re.sub(
        r"\.(\d{1,5})(?=[+-]\d{2}:\d{2}$)",
        lambda match: "." + match.group(1).ljust(6, "0"),
        normalized,
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ChronicleIngestError(f"invalid {field}: {value!r}") from error
    if parsed.tzinfo is None:
        raise ChronicleIngestError(f"{field} must include an RFC3339 UTC offset")
    return parsed.astimezone(timezone.utc)


def _rfc3339_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _classify_range_bug_at_cutoff(
    guild_name: str | None,
    started_at: str | None,
    *,
    cutoff: datetime,
) -> str:
    """Apply the exact 南北/start-time rule at an explicit frozen cutoff."""

    if not isinstance(guild_name, str) or not guild_name.strip():
        return UNKNOWN_NONVOTING
    if not isinstance(started_at, str) or not started_at.strip():
        return UNKNOWN_NONVOTING
    try:
        started = _parse_rfc3339(started_at, field="started_at")
    except ChronicleIngestError:
        return UNKNOWN_NONVOTING
    # Chronicle JSON examples and Go zero values use year 1 for an absent time.
    # Treat that sentinel as missing evidence, never as a genuine pre-fix run.
    if started.year <= 1:
        return UNKNOWN_NONVOTING
    if guild_name.strip() != "南北":
        return NO_KNOWN_RULE_MATCH
    local_started = started.astimezone(_CHINA_TZ)
    return (
        SUSPECT_36YD_RANGE_BUG
        if local_started < cutoff
        else POSTFIX_KNOWN_CLEAN
    )


def classify_range_bug(
    guild_name: str | None,
    started_at: str | None,
) -> str:
    """Apply the current conservative 南北/start-time contamination rule."""

    if not isinstance(guild_name, str) or not guild_name.strip():
        return UNKNOWN_NONVOTING
    if not isinstance(started_at, str) or not started_at.strip():
        return UNKNOWN_NONVOTING
    try:
        started = _parse_rfc3339(started_at, field="started_at")
    except ChronicleIngestError:
        return UNKNOWN_NONVOTING
    if started.year <= 1:
        return UNKNOWN_NONVOTING
    if guild_name.strip() != "南北":
        return NO_KNOWN_RULE_MATCH
    local_started = started.astimezone(_CHINA_TZ)
    if local_started < _RANGE_BUG_SUSPECT_BEFORE:
        return SUSPECT_36YD_RANGE_BUG
    if local_started < _RANGE_BUG_POSTFIX_AT_OR_AFTER:
        return RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING
    return POSTFIX_KNOWN_CLEAN


def classify_range_bug_legacy_v1(
    guild_name: str | None,
    started_at: str | None,
) -> str:
    """Replay the superseded 2026-09-01 cutoff for immutable-manifest audit."""

    return _classify_range_bug_at_cutoff(
        guild_name,
        started_at,
        cutoff=_LEGACY_RANGE_BUG_CUTOFF,
    )


def _ensure_offline_root(data_root: Path) -> Path:
    resolved = data_root.resolve()
    if "offline_data" not in {part.lower() for part in resolved.parts}:
        raise ChronicleIngestError(
            f"raw Chronicle data root must be inside an offline_data directory: {resolved}"
        )
    return resolved / "chronicle_raw" / "external_api" / "v1"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if path.read_bytes() != data:
                raise ChronicleIngestError(f"immutable output already exists with different bytes: {path}")
            temporary_path.unlink()
        else:
            os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Atomically replace a mutable pointer such as the incremental watermark."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _safe_json_object(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleIngestError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ChronicleIngestError(f"{label} must be a JSON object")
    return value


class RawObjectStore:
    def __init__(self, data_root: Path) -> None:
        self.root = _ensure_offline_root(data_root)

    def put(self, data: bytes, *, suffix: str, media_type: str) -> dict[str, Any]:
        digest = _sha256(data)
        clean_suffix = suffix.lstrip(".")
        path = self.root / "objects" / "sha256" / digest[:2] / f"{digest}.{clean_suffix}"
        _atomic_write_bytes(path, data)
        return {
            "sha256": digest,
            "size_bytes": len(data),
            "media_type": media_type,
            "relative_path": path.relative_to(self.root).as_posix(),
        }


def _query_pairs(values: Mapping[str, Any]) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for key in sorted(values):
        value = values[key]
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, item) for item in value)
        else:
            pairs.append((key, value))
    return pairs


def _recent_query(
    *,
    upload_after: str,
    instance_names: Sequence[str],
    realm_id: str | None,
    guild_id: str | None,
    has_video: bool | None,
    page: int,
    page_size: int,
) -> list[tuple[str, Any]]:
    return _query_pairs(
        {
            "guild_id": guild_id,
            "has_video": None if has_video is None else str(has_video).lower(),
            "instance_name": list(instance_names),
            "page": page,
            "page_size": page_size,
            "realm_id": realm_id,
            "upload_after": upload_after,
        }
    )


def fetch_recent_pages(
    client: ChronicleClient,
    *,
    upload_after: str,
    instance_names: Sequence[str] = (),
    realm_id: str | None = None,
    guild_id: str | None = None,
    has_video: bool | None = None,
    page_size: int = 25,
    max_pages: int = 1,
    require_complete: bool = True,
) -> tuple[list[tuple[HTTPResponse, dict[str, Any]]], bool]:
    _parse_rfc3339(upload_after, field="upload_after")
    if not 1 <= page_size <= 50 or max_pages < 1:
        raise ChronicleSafetyLimitError("page_size must be 1..50 and max_pages >= 1")
    pages: list[tuple[HTTPResponse, dict[str, Any]]] = []
    has_more = False
    for page in range(1, max_pages + 1):
        response, value = client.get_json(
            "/raidlogs/recent",
            params=_recent_query(
                upload_after=upload_after,
                instance_names=instance_names,
                realm_id=realm_id,
                guild_id=guild_id,
                has_video=has_video,
                page=page,
                page_size=page_size,
            ),
        )
        if not isinstance(value, dict) or not isinstance(value.get("activities"), list):
            raise ChronicleIngestError("recent response lacks an activities list")
        pagination = value.get("pagination")
        if not isinstance(pagination, dict):
            raise ChronicleIngestError("recent response lacks pagination metadata")
        has_more = bool(pagination.get("has_more"))
        pages.append((response, value))
        if not has_more:
            break
    if has_more and require_complete:
        raise ChronicleSafetyLimitError(
            "recent results exceed max_pages; refusing a partial ingest because advancing "
            "the uploaded_at watermark could skip instances"
        )
    return pages, has_more


def _scope_descriptor(
    *,
    scope_name: str,
    instance_names: Sequence[str],
    realm_id: str | None,
    guild_id: str | None,
    has_video: bool | None,
) -> dict[str, Any]:
    descriptor = {
        "scope_name": scope_name,
        "instance_names": sorted(set(instance_names)),
        "realm_id": realm_id,
        "guild_id": guild_id,
        "has_video": has_video,
    }
    descriptor["scope_id"] = _sha256(_canonical_json_bytes(descriptor))
    return descriptor


def _watermark_path(raw_root: Path, scope_id: str) -> Path:
    return raw_root / "watermarks" / f"{scope_id}.json"


def _load_watermark(path: Path, scope_id: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = _safe_json_object(path.read_bytes(), label="watermark")
    if value.get("schema") != WATERMARK_SCHEMA or value.get("scope_id") != scope_id:
        raise ChronicleIngestError(f"watermark schema/scope mismatch: {path}")
    _parse_rfc3339(value.get("upload_after"), field="watermark.upload_after")
    ids = value.get("boundary_instance_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ChronicleIngestError("watermark boundary_instance_ids must be strings")
    return value


def _activity_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChronicleIngestError("recent activity must be an object")
    instance_id = value.get("id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ChronicleIngestError("recent activity has no instance id")
    uploaded_at = value.get("uploaded_at")
    started_at = value.get("started_at")
    uploaded_dt = _parse_rfc3339(uploaded_at, field="activity.uploaded_at")
    if uploaded_dt.year <= 1:
        raise ChronicleIngestError(
            "activity.uploaded_at is Chronicle's zero-time sentinel; cannot advance a cursor"
        )
    if started_at is not None:
        _parse_rfc3339(started_at, field="activity.started_at")
    return {
        "instance_id": instance_id,
        "slug": value.get("slug") if isinstance(value.get("slug"), str) else None,
        "instance_name": value.get("name") if isinstance(value.get("name"), str) else None,
        "uploaded_at": str(uploaded_at),
        "uploaded_at_utc": _rfc3339_utc(uploaded_dt),
        "started_at": started_at if isinstance(started_at, str) else None,
        "guild_name": value.get("guild_name") if isinstance(value.get("guild_name"), str) else None,
        "discovery_source": "recent_upload_after",
    }


def _extract_guild(player: Mapping[str, Any]) -> str | None:
    for key in ("guild_name", "guildName"):
        if isinstance(player.get(key), str) and str(player[key]).strip():
            return str(player[key]).strip()
    guild = player.get("guild")
    if isinstance(guild, str) and guild.strip():
        return guild.strip()
    if isinstance(guild, dict) and isinstance(guild.get("name"), str):
        return guild["name"].strip() or None
    return None


def _parse_nonzero_timestamp(value: Any, *, field: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _parse_rfc3339(value, field=field)
    except ChronicleIngestError:
        return None
    return None if parsed.year <= 1 else parsed


def _derive_started_at(
    activity: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str | None, str, str | None]:
    """Resolve run start without ever turning it into an upload cursor."""

    direct_candidates = (
        (activity.get("started_at"), "activity.started_at"),
        (metadata.get("started_at"), "metadata.started_at"),
    )
    for value, field in direct_candidates:
        if _parse_nonzero_timestamp(value, field=field) is not None:
            assert isinstance(value, str)
            return value, "direct", field

    candidates: list[tuple[datetime, int, str]] = []
    encounters = metadata.get("encounters")
    if isinstance(encounters, list):
        for index, encounter in enumerate(encounters):
            if not isinstance(encounter, dict):
                continue
            value = encounter.get("start_time")
            parsed = _parse_nonzero_timestamp(
                value, field=f"metadata.encounters[{index}].start_time"
            )
            if parsed is not None:
                assert isinstance(value, str)
                candidates.append((parsed, index, value))
    if candidates:
        _, _, value = min(candidates, key=lambda row: (row[0], row[1]))
        return value, "metadata.encounters.min(start_time)", None
    return None, "missing", None


def _derive_uploaded_at(
    activity: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str | None, str, str | None]:
    """Preserve a direct uploaded time; never infer one from encounter times."""

    for value, field in (
        (activity.get("uploaded_at"), "activity.uploaded_at"),
        (metadata.get("uploaded_at"), "metadata.uploaded_at"),
    ):
        if _parse_nonzero_timestamp(value, field=field) is not None:
            assert isinstance(value, str)
            return value, "direct", field
    return None, "missing", None


def _instance_guild_context(
    metadata: Mapping[str, Any],
    *,
    activity_guild_name: str | None,
    leaderboard_entries: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    guild = metadata.get("guild")
    if isinstance(guild, dict) and isinstance(guild.get("name"), str):
        value = guild["name"].strip()
        if value:
            return value, "metadata.guild.name"
    if isinstance(metadata.get("guild_name"), str) and metadata["guild_name"].strip():
        return metadata["guild_name"].strip(), "metadata.guild_name"
    if isinstance(activity_guild_name, str) and activity_guild_name.strip():
        return activity_guild_name.strip(), "activity.guild_name"
    leaderboard_guilds = sorted(
        {
            value
            for entry in leaderboard_entries
            for value in [_extract_guild(entry)]
            if value is not None
        }
    )
    if len(leaderboard_guilds) == 1:
        return leaderboard_guilds[0], "leaderboard_snapshot.unique_guild_name"
    return None, None


def _metadata_players(metadata: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    players = metadata.get("players")
    if isinstance(players, list):
        for player in players:
            if isinstance(player, dict):
                yield player
        return
    if isinstance(players, dict):
        for guid in sorted(players):
            player = players[guid]
            if not isinstance(player, dict):
                continue
            normalized = dict(player)
            # The actual External API encodes players as GUID -> player object.
            # The mapping key is authoritative exact identity evidence.
            normalized["player_guid"] = guid
            yield normalized


def _extract_warrior_observations(
    metadata: Mapping[str, Any],
    *,
    started_at: str | None,
    leaderboard_entries: Sequence[Mapping[str, Any]],
    instance_ranking_records: Sequence[Mapping[str, Any]] = (),
    contamination_guild_context: str | None = None,
    contamination_guild_evidence: str | None = None,
) -> list[dict[str, Any]]:
    observations: dict[tuple[str, str], dict[str, Any]] = {}

    def add(player: Mapping[str, Any], source: str) -> None:
        player_class = player.get("player_class", player.get("class"))
        if not isinstance(player_class, str) or player_class.strip().lower() != "warrior":
            return
        spec_raw = player.get("player_spec", player.get("spec", player.get("specialization")))
        spec = spec_raw.strip() if isinstance(spec_raw, str) and spec_raw.strip() else "Unknown"
        name_raw = player.get("player_name", player.get("name"))
        name = name_raw.strip() if isinstance(name_raw, str) else ""
        guid_raw = player.get("player_guid", player.get("guid"))
        guid = guid_raw.strip() if isinstance(guid_raw, str) else ""
        if guid:
            identity = ("guid", guid.lower())
        elif name:
            # Exact decoded name only; this is an identity fallback, not fuzzy matching.
            identity = ("name", name)
        else:
            identity = ("anonymous", _sha256(_canonical_json_bytes(dict(player))))
        candidate: dict[str, Any] = {
            "player_guid": guid or None,
            "player_name": name or None,
            "player_class": "Warrior",
            "player_spec": spec,
            "sources": [source],
            "field_conflicts": {},
        }
        existing = observations.get(identity)
        if existing is None:
            observations[identity] = candidate
            return
        existing["sources"] = sorted(set(existing["sources"]) | {source})
        for field in ("player_guid", "player_name"):
            if existing[field] is None and candidate[field] is not None:
                existing[field] = candidate[field]
        for field, unknown in (("player_spec", "Unknown"),):
            old_value = existing[field]
            new_value = candidate[field]
            if field in existing["field_conflicts"]:
                if new_value != unknown:
                    conflicts = set(existing["field_conflicts"][field])
                    conflicts.add(str(new_value))
                    existing["field_conflicts"][field] = sorted(conflicts)
                existing[field] = "Unknown"
                continue
            if old_value == unknown and new_value != unknown:
                existing[field] = new_value
            elif new_value != unknown and old_value != new_value:
                conflicts = set(existing["field_conflicts"].get(field, []))
                conflicts.update((str(old_value), str(new_value)))
                existing["field_conflicts"][field] = sorted(conflicts)
                existing[field] = "Unknown"

    def finalize(row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["contamination_guild_context"] = contamination_guild_context
        row["contamination_guild_evidence"] = contamination_guild_evidence
        row["contamination_label"] = classify_range_bug(
            contamination_guild_context, started_at
        )
        row["spec_evidence_status"] = (
            UNKNOWN_NONVOTING
            if row["player_spec"] == "Unknown"
            or "player_spec" in row["field_conflicts"]
            else "OBSERVED"
        )
        return row

    for player in _metadata_players(metadata):
        add(player, "instance_metadata")
    for ranking in instance_ranking_records:
        if isinstance(ranking, dict):
            add(ranking, "instance_ranking_records")
    for entry in leaderboard_entries:
        add(entry, "leaderboard_snapshot")
    return sorted(
        (finalize(row) for row in observations.values()),
        key=lambda row: (
            str(row.get("player_spec")),
            str(row.get("player_name")),
            str(row.get("player_guid")),
        ),
    )


def _spec_counts(observations: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"Arms": 0, "Fury": 0, "Other_or_unknown": 0}
    for row in observations:
        spec = str(row.get("player_spec", "")).strip().lower()
        if spec == "arms":
            counts["Arms"] += 1
        elif spec == "fury":
            counts["Fury"] += 1
        else:
            counts["Other_or_unknown"] += 1
    return counts


def validate_event_gzip(data: bytes) -> int:
    """Validate the complete gzip envelope without retaining decompressed bytes."""

    if not data.startswith(b"\x1f\x8b"):
        raise ChronicleIngestError("event response is not a gzip stream (missing 1f8b magic)")
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
    except (OSError, EOFError) as error:
        raise ChronicleIngestError(f"event response has an invalid gzip envelope: {error}") from error
    return total


def _leaderboard_entries_for_slug(
    snapshots: Sequence[dict[str, Any]], slug: str | None
) -> list[Mapping[str, Any]]:
    if not slug:
        return []
    output: list[Mapping[str, Any]] = []
    for snapshot in snapshots:
        value = snapshot["decoded"]
        entries = value.get("entries") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("log_hashed_slug") == slug:
                output.append(entry)
    return output


def _manifest_path(raw_root: Path, data: bytes) -> Path:
    digest = _sha256(data)
    return raw_root / "manifests" / f"{digest}.json"


def range_bug_boundary_contract() -> dict[str, Any]:
    """Return the single current time-boundary contract for all own consumers."""

    return {
        "pre_fix_suspect_before_local": RANGE_BUG_SUSPECT_BEFORE_LOCAL,
        "boundary_uncertain_at_or_after_local": (
            RANGE_BUG_SUSPECT_BEFORE_LOCAL
        ),
        "postfix_known_clean_at_or_after_local": (
            RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL
        ),
        "timezone": RANGE_BUG_CUTOFF_TIMEZONE,
        "boundary_policy": (
            "guild exactly 南北: before 2026-09-03 local is suspect; "
            "2026-09-03 00:00 through 11:59:59.999999 local is uncertain "
            "and nontraining; noon or later is conservatively post-fix"
        ),
        "boundary_provenance": {
            "source": "user report attributed to Tony",
            "reported_fact": "the 36-yard bug was fixed during the morning of 2026-09-03",
            "safe_floor_policy": (
                "12:00 Asia/Shanghai is a conservative reproducible boundary, "
                "not a claim about the exact patch instant"
            ),
        },
    }


def _contamination_contract() -> dict[str, Any]:
    return {
        "guild": "南北",
        "guild_match_policy": (
            "exact_UTF8_name_after_outer_whitespace_only_no_fuzzy_matching"
        ),
        **range_bug_boundary_contract(),
        "player_name_special_cases": False,
        "labels": [
            SUSPECT_36YD_RANGE_BUG,
            RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
            POSTFIX_KNOWN_CLEAN,
            NO_KNOWN_RULE_MATCH,
            UNKNOWN_NONVOTING,
        ],
        "scope": (
            "raid/log-level rule copied to observations; not personal guild membership"
        ),
        "no_known_rule_match_meaning": (
            "no preregistered contamination rule matched; not verified clean"
        ),
    }


def _read_object_reference(raw_root: Path, reference: Any, *, label: str) -> bytes:
    if not isinstance(reference, dict):
        raise ChronicleIngestError(f"{label} object reference must be an object")
    relative_raw = reference.get("relative_path")
    digest = reference.get("sha256")
    size = reference.get("size_bytes")
    if not isinstance(relative_raw, str) or not relative_raw:
        raise ChronicleIngestError(f"{label} object reference lacks relative_path")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ChronicleIngestError(f"{label} object reference has invalid sha256")
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleIngestError(f"{label} object path escapes the raw store")
    path = (raw_root / relative).resolve()
    try:
        path.relative_to(raw_root.resolve())
    except ValueError as error:
        raise ChronicleIngestError(f"{label} object path escapes the raw store") from error
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ChronicleIngestError(f"cannot read {label} object {path}: {error}") from error
    if _sha256(data) != digest or not isinstance(size, int) or len(data) != size:
        raise ChronicleIngestError(f"{label} object hash/size verification failed: {path}")
    return data


def _verify_manifest_object_closure(value: Any, raw_root: Path, *, label: str) -> None:
    if isinstance(value, dict):
        if {"relative_path", "sha256", "size_bytes"}.issubset(value):
            _read_object_reference(raw_root, value, label=label)
            return
        for key, child in value.items():
            _verify_manifest_object_closure(child, raw_root, label=f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _verify_manifest_object_closure(child, raw_root, label=f"{label}[{index}]")


def replay_manifest_from_local_raw(
    source_manifest_path: Path,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    """Rebuild derived manifest rows entirely from verified local raw objects."""

    raw_root = _ensure_offline_root(Path(data_root))
    manifests_root = (raw_root / "manifests").resolve()
    source_path = Path(source_manifest_path).expanduser().resolve()
    if source_path.parent != manifests_root:
        raise ChronicleIngestError(
            "source manifest must be a direct child of the configured raw manifests directory"
        )
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise ChronicleIngestError(
            f"cannot read source manifest {source_manifest_path}: {error}"
        ) from error
    source = _safe_json_object(source_bytes, label="source manifest")
    source_sha256 = _sha256(source_bytes)
    if source_path.name != f"{source_sha256}.json":
        raise ChronicleIngestError(
            "source manifest filename is not its lowercase SHA-256 content address"
        )
    if source_bytes != _canonical_json_bytes(source):
        raise ChronicleIngestError("source manifest bytes are not canonical JSON")
    if source.get("schema") != SCHEMA:
        raise ChronicleIngestError("source manifest schema is not supported")
    if source.get("kind") != "chronicle_external_api_raw_snapshot":
        raise ChronicleIngestError("source manifest kind is not supported")
    revision_pair = (
        source.get("implementation_revision"),
        source.get("parser_contract_revision"),
    )
    if revision_pair not in SUPPORTED_REPLAY_REVISION_PAIRS:
        raise ChronicleIngestError(
            "source manifest implementation/parser revision pair is not supported"
        )
    instances = source.get("instances")
    if not isinstance(instances, list):
        raise ChronicleIngestError("source manifest instances must be a list")
    _verify_manifest_object_closure(source, raw_root, label="source_manifest")

    source_is_current = revision_pair == (
        IMPLEMENTATION_REVISION,
        PARSER_CONTRACT_REVISION,
    )
    if source_is_current:
        if source.get("contamination_contract") != _contamination_contract():
            raise ChronicleIngestError(
                "current source manifest contamination contract is not current"
            )

    recent_activities: dict[str, dict[str, Any]] = {}
    source_recent_pages = source.get("recent_pages", [])
    if not isinstance(source_recent_pages, list):
        raise ChronicleIngestError("source recent_pages must be a list")
    for page_index, page in enumerate(source_recent_pages):
        if not isinstance(page, dict) or not isinstance(page.get("object"), dict):
            raise ChronicleIngestError(f"recent page {page_index} is invalid")
        body = _read_object_reference(
            raw_root, page["object"], label=f"recent_pages[{page_index}]"
        )
        decoded = _safe_json_object(body, label=f"recent page {page_index}")
        activities = decoded.get("activities")
        if not isinstance(activities, list):
            raise ChronicleIngestError(
                f"recent page {page_index} lacks an activities list"
            )
        for activity_index, raw_activity in enumerate(activities):
            try:
                activity = _activity_record(raw_activity)
            except ChronicleIngestError as error:
                raise ChronicleIngestError(
                    f"recent_pages[{page_index}].activities[{activity_index}]: {error}"
                ) from error
            instance_id = activity["instance_id"]
            previous = recent_activities.get(instance_id)
            if previous is not None and previous != activity:
                raise ChronicleIngestError(
                    f"recent pages disagree about instance {instance_id}"
                )
            recent_activities[instance_id] = activity

    leaderboard_snapshots: list[dict[str, Any]] = []
    source_leaderboards = source.get("leaderboard_snapshots", [])
    if not isinstance(source_leaderboards, list):
        raise ChronicleIngestError("source leaderboard_snapshots must be a list")
    for index, snapshot in enumerate(source_leaderboards):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("object"), dict):
            raise ChronicleIngestError(f"leaderboard snapshot {index} is invalid")
        body = _read_object_reference(
            raw_root, snapshot["object"], label=f"leaderboard_snapshots[{index}]"
        )
        leaderboard_snapshots.append(
            {
                "query": snapshot.get("query", {}),
                "decoded": _safe_json_object(body, label=f"leaderboard snapshot {index}"),
            }
        )

    rebuilt_rows: list[dict[str, Any]] = []
    for index, source_row in enumerate(instances):
        if not isinstance(source_row, dict):
            raise ChronicleIngestError(f"source instance row {index} must be an object")
        metadata_wrapper = source_row.get("metadata")
        if not isinstance(metadata_wrapper, dict):
            raise ChronicleIngestError(f"source instance row {index} lacks metadata")
        metadata_bytes = _read_object_reference(
            raw_root,
            metadata_wrapper.get("object"),
            label=f"instances[{index}].metadata",
        )
        metadata = _safe_json_object(metadata_bytes, label=f"instance metadata {index}")
        instance_id = source_row.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ChronicleIngestError(f"source instance row {index} lacks instance_id")
        if isinstance(metadata.get("id"), str) and metadata["id"] != instance_id:
            raise ChronicleIngestError(f"source instance row {index} metadata id mismatch")
        slug = source_row.get("slug")
        if not isinstance(slug, str) or not slug:
            slug = metadata.get("slug") if isinstance(metadata.get("slug"), str) else None
        matching_leaderboard = _leaderboard_entries_for_slug(
            leaderboard_snapshots, slug
        )

        rankings: list[Mapping[str, Any]] = []
        ranking_wrapper = source_row.get("ranking_records")
        if isinstance(ranking_wrapper, dict) and isinstance(ranking_wrapper.get("object"), dict):
            ranking_bytes = _read_object_reference(
                raw_root,
                ranking_wrapper["object"],
                label=f"instances[{index}].ranking_records",
            )
            try:
                ranking_value = json.loads(ranking_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ChronicleIngestError(
                    f"instance ranking records {index} are invalid JSON: {error}"
                ) from error
            if not isinstance(ranking_value, list):
                raise ChronicleIngestError(
                    f"instance ranking records {index} must be a list"
                )
            rankings = [row for row in ranking_value if isinstance(row, dict)]

        recent_activity = recent_activities.get(instance_id, {})
        # Reconstruct the raid start from independently captured metadata first.
        # A recent-page activity is the second raw source.  The materialized
        # source-row timestamp is used only when neither raw object carries it,
        # and its provenance explicitly records that weaker fallback.
        started_at, started_source, started_field = _derive_started_at({}, metadata)
        if started_at is None:
            started_at, started_source, started_field = _derive_started_at(
                recent_activity, {}
            )
            if started_at is not None:
                started_source = "recent_pages.activity.started_at"
                started_field = "started_at"
        if started_at is None:
            source_started = source_row.get("started_at")
            if _parse_nonzero_timestamp(
                source_started, field="source_manifest.instance.started_at"
            ) is not None:
                assert isinstance(source_started, str)
                started_at = source_started
                started_source = "source_manifest_fallback"
                started_field = (
                    "source_manifest.instance.started_at_no_independent_raw_timestamp"
                )

        # uploaded_at is reconstructed from the immutable recent-page response
        # when present, then metadata, with the source-row value as an explicit
        # last-resort fallback.  It remains cursor provenance, never the raid
        # contamination clock.
        uploaded_at, uploaded_source, uploaded_field = _derive_uploaded_at(
            recent_activity, {}
        )
        if uploaded_at is not None:
            uploaded_source = "recent_pages.activity.uploaded_at"
            uploaded_field = "uploaded_at"
        else:
            uploaded_at, uploaded_source, uploaded_field = _derive_uploaded_at(
                {}, metadata
            )
        if uploaded_at is None:
            source_uploaded = source_row.get("uploaded_at")
            if _parse_nonzero_timestamp(
                source_uploaded, field="source_manifest.instance.uploaded_at"
            ) is not None:
                assert isinstance(source_uploaded, str)
                uploaded_at = source_uploaded
                uploaded_source = "source_manifest_fallback"
                uploaded_field = (
                    "source_manifest.instance.uploaded_at_no_independent_raw_timestamp"
                )
        guild_context, guild_evidence = _instance_guild_context(
            metadata,
            activity_guild_name=(
                source_row.get("contamination_guild_context")
                if isinstance(source_row.get("contamination_guild_context"), str)
                else None
            ),
            leaderboard_entries=matching_leaderboard,
        )
        observations = _extract_warrior_observations(
            metadata,
            started_at=started_at,
            leaderboard_entries=matching_leaderboard,
            instance_ranking_records=rankings,
            contamination_guild_context=guild_context,
            contamination_guild_evidence=guild_evidence,
        )
        rebuilt = dict(source_row)
        rebuilt.update(
            {
                "slug": slug,
                "instance_name": (
                    source_row.get("instance_name")
                    if isinstance(source_row.get("instance_name"), str)
                    and source_row["instance_name"]
                    else metadata.get("name")
                    if isinstance(metadata.get("name"), str)
                    else None
                ),
                "uploaded_at": uploaded_at,
                "uploaded_at_source": uploaded_source,
                "uploaded_at_source_field": uploaded_field,
                "started_at": started_at,
                "started_at_source": started_source,
                "started_at_source_field": started_field,
                "contamination_guild_context": guild_context,
                "contamination_guild_evidence": guild_evidence,
                "instance_contamination_label": classify_range_bug(
                    guild_context, started_at
                ),
                "warrior_observations": observations,
                "warrior_spec_counts": _spec_counts(observations),
            }
        )
        rebuilt_rows.append(rebuilt)

    if source_is_current:
        # Re-derive every current semantic projection from independently stored
        # raw objects when available before treating the manifest as a fixed
        # point.  Provenance-only source labels are excluded from equality;
        # explicit source-row timestamp fallbacks above are used only when no
        # metadata/recent-page timestamp exists.
        semantic_fields = (
            "slug",
            "instance_name",
            "uploaded_at",
            "started_at",
            "contamination_guild_context",
            "contamination_guild_evidence",
            "instance_contamination_label",
            "warrior_observations",
            "warrior_spec_counts",
        )
        for index, (source_row, rebuilt_row) in enumerate(
            zip(instances, rebuilt_rows, strict=True)
        ):
            mismatches = [
                field
                for field in semantic_fields
                if source_row.get(field) != rebuilt_row.get(field)
            ]
            if mismatches:
                raise ChronicleIngestError(
                    "current source manifest semantic projections do not match "
                    f"verified local raw objects at instances[{index}]: {mismatches}"
                )
        # A semantically valid current manifest is a replay fixed point.
        # Reusing it avoids provenance nesting and a new hash on every call.
        return {
            "status": "ALREADY_CURRENT_LOCAL_RAW",
            "implementation_revision": IMPLEMENTATION_REVISION,
            "parser_contract_revision": PARSER_CONTRACT_REVISION,
            "source_manifest_sha256": source_sha256,
            "manifest_sha256": source_sha256,
            "manifest_path": str(source_path),
            "instance_count": len(instances),
            "network_requests_made": 0,
            "watermark_mutated": False,
        }

    rebuilt_manifest = dict(source)
    rebuilt_manifest.update(
        {
            "implementation_revision": IMPLEMENTATION_REVISION,
            "parser_contract_revision": PARSER_CONTRACT_REVISION,
            "instances": rebuilt_rows,
            "replay_provenance": {
                "method": "local_verified_raw_object_replay",
                "network_requests_made": 0,
                "source_manifest_sha256": source_sha256,
                "source_implementation_revision": source.get("implementation_revision"),
                "source_parser_contract_revision": source.get(
                    "parser_contract_revision"
                ),
                "source_manifest_adoption_status": (
                    "LEGACY_REVISION_MIGRATED_TO_CURRENT"
                ),
            },
        }
    )
    rebuilt_manifest["contamination_contract"] = _contamination_contract()
    manifest_bytes = _canonical_json_bytes(rebuilt_manifest)
    manifest_path = _manifest_path(raw_root, manifest_bytes)
    _atomic_write_bytes(manifest_path, manifest_bytes)
    digest = _sha256(manifest_bytes)
    return {
        "status": "REPLAYED_LOCAL_RAW",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "parser_contract_revision": PARSER_CONTRACT_REVISION,
        "source_manifest_sha256": source_sha256,
        "manifest_sha256": digest,
        "manifest_path": str(manifest_path),
        "instance_count": len(rebuilt_rows),
        "network_requests_made": 0,
        "watermark_mutated": False,
    }


def ingest_external_api(
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    client: ChronicleClient | None = None,
    upload_after: str | None = None,
    use_watermark: bool = False,
    explicit_instance_ids: Sequence[str] = (),
    instance_names: Sequence[str] = (),
    realm_id: str | None = None,
    guild_id: str | None = None,
    has_video: bool | None = None,
    scope_name: str = "default",
    page_size: int = 25,
    max_pages: int = 1,
    max_instances: int = 10,
    stream_types: Sequence[str] = (),
    include_ranking_records: bool = False,
    leaderboard_queries: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Fetch one bounded snapshot and publish its manifest before its watermark."""

    if max_instances < 1:
        raise ChronicleSafetyLimitError("max_instances must be positive")
    unknown_streams = sorted(set(stream_types) - set(EVENT_STREAM_TYPES))
    if unknown_streams:
        raise ChronicleIngestError(f"unknown Chronicle stream types: {unknown_streams}")
    if use_watermark and upload_after is not None:
        raise ChronicleIngestError("choose either upload_after or use_watermark, not both")
    if not (
        upload_after is not None
        or use_watermark
        or explicit_instance_ids
        or leaderboard_queries
    ):
        raise ChronicleSafetyLimitError(
            "ingest requires upload_after/use_watermark, explicit instance ids, or a leaderboard snapshot"
        )

    api = client or ChronicleClient()
    store = RawObjectStore(Path(data_root))
    raw_root = store.root
    scope = _scope_descriptor(
        scope_name=scope_name,
        instance_names=instance_names,
        realm_id=realm_id,
        guild_id=guild_id,
        has_video=has_video,
    )
    watermark_path = _watermark_path(raw_root, scope["scope_id"])
    prior_watermark = _load_watermark(watermark_path, scope["scope_id"])
    effective_upload_after: str | None
    if use_watermark:
        if prior_watermark is None:
            raise ChronicleSafetyLimitError(
                "no watermark exists for this scope; provide an explicit upload_after once"
            )
        effective_upload_after = str(prior_watermark["upload_after"])
    else:
        effective_upload_after = upload_after

    recent_pages: list[tuple[HTTPResponse, dict[str, Any]]] = []
    if effective_upload_after is not None:
        recent_pages, _ = fetch_recent_pages(
            api,
            upload_after=effective_upload_after,
            instance_names=instance_names,
            realm_id=realm_id,
            guild_id=guild_id,
            has_video=has_video,
            page_size=page_size,
            max_pages=max_pages,
            require_complete=True,
        )

    activities: dict[str, dict[str, Any]] = {}
    for _, page in recent_pages:
        for raw_activity in page["activities"]:
            activity = _activity_record(raw_activity)
            existing = activities.get(activity["instance_id"])
            if existing is not None and existing != activity:
                raise ChronicleIngestError(
                    f"recent pages disagree about instance {activity['instance_id']}"
                )
            activities[activity["instance_id"]] = activity

    if effective_upload_after is not None:
        cursor_dt = _parse_rfc3339(effective_upload_after, field="upload_after")
        for activity in activities.values():
            assert activity["uploaded_at_utc"] is not None
            if _parse_rfc3339(
                activity["uploaded_at_utc"], field="activity.uploaded_at"
            ) < cursor_dt:
                raise ChronicleIngestError(
                    "recent API returned an uploaded_at before the requested upload_after"
                )

    if prior_watermark is not None and effective_upload_after == prior_watermark["upload_after"]:
        boundary = set(prior_watermark["boundary_instance_ids"])
        activities = {
            key: value
            for key, value in activities.items()
            if not (
                value["uploaded_at_utc"] == prior_watermark["upload_after"]
                and key in boundary
            )
        }

    for instance_id in explicit_instance_ids:
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ChronicleIngestError("explicit instance ids must be non-empty strings")
        activities.setdefault(
            instance_id.strip(),
            {
                "instance_id": instance_id.strip(),
                "slug": None,
                "instance_name": None,
                "uploaded_at": None,
                "uploaded_at_utc": None,
                "started_at": None,
                "guild_name": None,
                "discovery_source": "explicit_instance_id",
            },
        )

    ordered = sorted(
        activities.values(),
        key=lambda row: (row["uploaded_at_utc"] or "", row["instance_id"]),
    )
    if len(ordered) > max_instances:
        raise ChronicleSafetyLimitError(
            f"selection contains {len(ordered)} instances, exceeding max_instances={max_instances}; "
            "nothing was published"
        )

    leaderboard_snapshots: list[dict[str, Any]] = []
    for query in leaderboard_queries:
        limit = int(query.get("limit", 20))
        if not 1 <= limit <= 200:
            raise ChronicleSafetyLimitError("leaderboard limit must be 1..200")
        normalized_query = dict(query)
        normalized_query["limit"] = limit
        response, decoded = api.get_json(
            "/leaderboards", params=_query_pairs(normalized_query)
        )
        if not isinstance(decoded, dict) or not isinstance(decoded.get("entries"), list):
            raise ChronicleIngestError("leaderboard response lacks an entries list")
        leaderboard_snapshots.append(
            {"query": normalized_query, "response": response, "decoded": decoded}
        )

    recent_manifest_rows = []
    for response, decoded in recent_pages:
        recent_manifest_rows.append(
            {
                "request_url": response.url,
                "object": store.put(
                    response.body, suffix="json", media_type="application/json"
                ),
                "activity_count": len(decoded["activities"]),
            }
        )
    leaderboard_manifest_rows = []
    for snapshot in leaderboard_snapshots:
        response = snapshot["response"]
        leaderboard_manifest_rows.append(
            {
                "query": snapshot["query"],
                "request_url": response.url,
                "object": store.put(
                    response.body, suffix="json", media_type="application/json"
                ),
                "entry_count": len(snapshot["decoded"]["entries"]),
            }
        )

    instance_rows: list[dict[str, Any]] = []
    for activity in ordered:
        instance_id = activity["instance_id"]
        encoded_id = quote(instance_id, safe="")
        metadata_response, metadata = api.get_json(
            f"/raidlogs/instances/{encoded_id}"
        )
        if not isinstance(metadata, dict):
            raise ChronicleIngestError("instance metadata must be a JSON object")
        metadata_id = metadata.get("id")
        if isinstance(metadata_id, str) and metadata_id and metadata_id != instance_id:
            raise ChronicleIngestError(
                f"instance metadata id mismatch: requested {instance_id}, received {metadata_id}"
            )
        metadata_ref = store.put(
            metadata_response.body, suffix="json", media_type="application/json"
        )
        slug = activity["slug"]
        if slug is None and isinstance(metadata.get("slug"), str):
            slug = metadata["slug"]
        elif (
            slug is not None
            and isinstance(metadata.get("slug"), str)
            and metadata["slug"]
            and metadata["slug"] != slug
        ):
            raise ChronicleIngestError(
                f"instance slug mismatch for {instance_id}: {slug} != {metadata['slug']}"
            )
        recorded_started_at, started_at_source, started_at_source_field = (
            _derive_started_at(activity, metadata)
        )
        recorded_uploaded_at, uploaded_at_source, uploaded_at_source_field = (
            _derive_uploaded_at(activity, metadata)
        )
        matching_leaderboard = _leaderboard_entries_for_slug(
            leaderboard_snapshots, slug
        )

        ranking_ref = None
        rankings: list[Any] = []
        if include_ranking_records:
            ranking_response, ranking_value = api.get_json(
                f"/raidlogs/instances/{encoded_id}/ranking-records"
            )
            if not isinstance(ranking_value, list):
                raise ChronicleIngestError("ranking-records response must be a JSON list")
            rankings = ranking_value
            ranking_ref = {
                "object": store.put(
                    ranking_response.body, suffix="json", media_type="application/json"
                ),
                "record_count": len(rankings),
            }

        guild_context, guild_evidence = _instance_guild_context(
            metadata,
            activity_guild_name=activity["guild_name"],
            leaderboard_entries=matching_leaderboard,
        )
        observations = _extract_warrior_observations(
            metadata,
            started_at=recorded_started_at,
            leaderboard_entries=matching_leaderboard,
            instance_ranking_records=[
                row for row in rankings if isinstance(row, dict)
            ],
            contamination_guild_context=guild_context,
            contamination_guild_evidence=guild_evidence,
        )

        streams: dict[str, Any] = {}
        for stream_type in sorted(set(stream_types)):
            stream_response = api.get(
                f"/raidlogs/instances/{encoded_id}/events/{stream_type}",
                accept="application/octet-stream",
                allowed_statuses=(404,),
            )
            if stream_response.status == 404:
                streams[stream_type] = {
                    "status": "UNAVAILABLE_404",
                    "object": None,
                    "note": "official API permits unavailable streams for older instances",
                }
                continue
            uncompressed_size = validate_event_gzip(stream_response.body)
            streams[stream_type] = {
                "status": "AVAILABLE",
                "object": store.put(
                    stream_response.body,
                    suffix=f"{stream_type}.events.gz",
                    media_type="application/octet-stream",
                ),
                "gzip_uncompressed_size_bytes": uncompressed_size,
                "encoding_contract": "gzip -> encounter frames -> length-delimited protobuf",
            }

        instance_rows.append(
            {
                "instance_id": instance_id,
                "slug": slug,
                "instance_name": (
                    activity["instance_name"]
                    if activity["instance_name"] is not None
                    else metadata.get("name")
                    if isinstance(metadata.get("name"), str)
                    else None
                ),
                "discovery_source": activity["discovery_source"],
                "uploaded_at": recorded_uploaded_at,
                "uploaded_at_source": uploaded_at_source,
                "uploaded_at_source_field": uploaded_at_source_field,
                "started_at": recorded_started_at,
                "started_at_source": started_at_source,
                "started_at_source_field": started_at_source_field,
                "cursor_order_key": [activity["uploaded_at_utc"], instance_id],
                "contamination_guild_context": guild_context,
                "contamination_guild_evidence": guild_evidence,
                "instance_contamination_label": classify_range_bug(
                    guild_context, recorded_started_at
                ),
                "warrior_observations": observations,
                "warrior_spec_counts": _spec_counts(observations),
                "metadata": {"request_url": metadata_response.url, "object": metadata_ref},
                "ranking_records": ranking_ref,
                "streams": streams,
            }
        )

    manifest = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "parser_contract_revision": PARSER_CONTRACT_REVISION,
        "kind": "chronicle_external_api_raw_snapshot",
        "api_base": api.api_base,
        "cursor_contract": {
            "query_parameter": "upload_after",
            "source_field": "uploaded_at",
            "inclusive_boundary_deduplication": True,
            "started_at_role": "provenance_and_contamination_only_never_cursor",
            "future_information_allowed": False,
        },
        "contamination_contract": _contamination_contract(),
        "request": {
            "scope": scope,
            "upload_after": effective_upload_after,
            "page_size": page_size,
            "max_pages": max_pages,
            "max_instances": max_instances,
            "explicit_instance_ids": sorted(set(explicit_instance_ids)),
            "stream_types": sorted(set(stream_types)),
            "include_ranking_records": include_ranking_records,
        },
        "recent_pages": recent_manifest_rows,
        "leaderboard_snapshots": leaderboard_manifest_rows,
        "instances": instance_rows,
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    manifest_path = _manifest_path(raw_root, manifest_bytes)
    # Publication barrier: every referenced raw object exists before this write.
    _atomic_write_bytes(manifest_path, manifest_bytes)
    manifest_sha256 = _sha256(manifest_bytes)

    next_watermark = None
    if effective_upload_after is not None:
        current_dt = _parse_rfc3339(effective_upload_after, field="upload_after")
        all_recent = [
            _activity_record(raw)
            for _, page in recent_pages
            for raw in page["activities"]
        ]
        if all_recent:
            maximum = max(
                _parse_rfc3339(row["uploaded_at"], field="activity.uploaded_at")
                for row in all_recent
            )
            if maximum < current_dt:
                raise ChronicleIngestError("recent API returned an uploaded_at before upload_after")
            next_dt = maximum
            boundary_ids = sorted(
                row["instance_id"]
                for row in all_recent
                if _parse_rfc3339(row["uploaded_at"], field="activity.uploaded_at") == maximum
            )
            if prior_watermark is not None and _rfc3339_utc(maximum) == prior_watermark["upload_after"]:
                boundary_ids = sorted(set(boundary_ids) | set(prior_watermark["boundary_instance_ids"]))
        else:
            next_dt = current_dt
            boundary_ids = (
                list(prior_watermark["boundary_instance_ids"])
                if prior_watermark is not None
                and prior_watermark["upload_after"] == _rfc3339_utc(current_dt)
                else []
            )
        next_watermark = {
            "schema": WATERMARK_SCHEMA,
            "scope_id": scope["scope_id"],
            "cursor_field": "uploaded_at",
            "upload_after": _rfc3339_utc(next_dt),
            "boundary_instance_ids": boundary_ids,
            "last_manifest_sha256": manifest_sha256,
        }
        # Commit cursor only after the complete manifest is durable.
        _atomic_replace_bytes(watermark_path, _canonical_json_bytes(next_watermark))

    return {
        "status": "INGESTED",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "parser_contract_revision": PARSER_CONTRACT_REVISION,
        "manifest_sha256": manifest_sha256,
        "manifest_path": str(manifest_path),
        "instance_count": len(instance_rows),
        "raw_object_count": (
            len(recent_manifest_rows)
            + len(leaderboard_manifest_rows)
            + sum(
                1
                + int(row["ranking_records"] is not None)
                + sum(1 for stream in row["streams"].values() if stream["object"] is not None)
                for row in instance_rows
            )
        ),
        "watermark": next_watermark,
    }


def list_external_api(
    *,
    client: ChronicleClient,
    upload_after: str | None,
    instance_names: Sequence[str],
    realm_id: str | None,
    guild_id: str | None,
    has_video: bool | None,
    page_size: int,
    max_pages: int,
    leaderboard_queries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {"writes": False, "recent": None, "leaderboards": []}
    if upload_after is not None:
        pages, truncated = fetch_recent_pages(
            client,
            upload_after=upload_after,
            instance_names=instance_names,
            realm_id=realm_id,
            guild_id=guild_id,
            has_video=has_video,
            page_size=page_size,
            max_pages=max_pages,
            require_complete=False,
        )
        result["recent"] = {
            "upload_after": upload_after,
            "pages": [decoded for _, decoded in pages],
            "truncated_by_max_pages": truncated,
        }
    for query in leaderboard_queries:
        response, value = client.get_json("/leaderboards", params=_query_pairs(query))
        result["leaderboards"].append(
            {"query": dict(query), "request_url": response.url, "response": value}
        )
    return result


def _add_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--upload-after", help="RFC3339 uploaded_at cursor")
    parser.add_argument("--instance-name", action="append", default=[])
    parser.add_argument("--realm-id")
    parser.add_argument("--guild-id")
    parser.add_argument("--has-video", choices=("true", "false"))
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--leaderboard", action="store_true")
    parser.add_argument("--leaderboard-instance")
    parser.add_argument("--leaderboard-class", default="WARRIOR")
    parser.add_argument("--leaderboard-spec")
    parser.add_argument("--leaderboard-limit", type=int, default=20)


def _leaderboard_query_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.leaderboard:
        return []
    if not 1 <= args.leaderboard_limit <= 200:
        raise ChronicleSafetyLimitError("--leaderboard-limit must be 1..200")
    return [
        {
            key: value
            for key, value in {
                "instance_names": args.leaderboard_instance,
                "class": args.leaderboard_class,
                "spec": args.leaderboard_spec,
                "role": "dps",
                "metric": "dps",
                "limit": args.leaderboard_limit,
                "offset": 0,
            }.items()
            if value is not None
        }
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_external_api_ingest_v1",
        description="Bounded Chronicle External API listing and raw ingestion",
    )
    parser.add_argument("--api-base", default=EXTERNAL_API_BASE)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--request-interval-seconds", type=float, default=1.05)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry = subparsers.add_parser("dry-run", help="print a bounded plan; no HTTP and no writes")
    _add_query_arguments(dry)
    dry.add_argument("--instance-id", action="append", default=[])
    dry.add_argument("--stream", action="append", choices=EVENT_STREAM_TYPES, default=[])
    dry.add_argument("--all-streams", action="store_true")
    dry.add_argument("--include-ranking-records", action="store_true")
    dry.add_argument("--max-instances", type=int, default=10)

    listing = subparsers.add_parser("list", help="bounded API listing; no writes")
    _add_query_arguments(listing)

    ingest = subparsers.add_parser("ingest", help="publish raw objects and a manifest")
    _add_query_arguments(ingest)
    ingest.add_argument("--use-watermark", action="store_true")
    ingest.add_argument("--instance-id", action="append", default=[])
    ingest.add_argument("--stream", action="append", choices=EVENT_STREAM_TYPES, default=[])
    ingest.add_argument("--all-streams", action="store_true")
    ingest.add_argument("--include-ranking-records", action="store_true")
    ingest.add_argument("--max-instances", type=int, default=10)
    ingest.add_argument("--scope-name", default="default")
    ingest.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)

    replay = subparsers.add_parser(
        "replay", help="rebuild a manifest from verified local raw objects; no HTTP"
    )
    replay.add_argument("--source-manifest", type=Path, required=True)
    replay.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    return parser


def _bool_arg(value: str | None) -> bool | None:
    return None if value is None else value == "true"


def _make_client(args: argparse.Namespace) -> ChronicleClient:
    return ChronicleClient(
        api_base=args.api_base,
        timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts,
        request_interval_seconds=args.request_interval_seconds,
    )


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or __import__("sys").stdout
    if args.command == "replay":
        result = replay_manifest_from_local_raw(
            args.source_manifest, data_root=args.data_root
        )
        output.write(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return 0
    leaderboard_queries = _leaderboard_query_from_args(args)
    streams = list(EVENT_STREAM_TYPES) if getattr(args, "all_streams", False) else list(getattr(args, "stream", []))
    common = {
        "upload_after": args.upload_after,
        "instance_names": args.instance_name,
        "realm_id": args.realm_id,
        "guild_id": args.guild_id,
        "has_video": _bool_arg(args.has_video),
        "page_size": args.page_size,
        "max_pages": args.max_pages,
    }
    if args.command == "dry-run":
        result = {
            "command": "dry-run",
            "network_requests_made": 0,
            "writes": False,
            "selection": common,
            "explicit_instance_ids": args.instance_id,
            "max_instances": args.max_instances,
            "stream_types": streams,
            "include_ranking_records": args.include_ranking_records,
            "leaderboard_queries": leaderboard_queries,
            "safety": "ingest is bounded and must be requested explicitly",
        }
    elif args.command == "list":
        if args.upload_after is None and not leaderboard_queries:
            raise ChronicleSafetyLimitError("list requires --upload-after and/or --leaderboard")
        result = list_external_api(
            client=_make_client(args),
            leaderboard_queries=leaderboard_queries,
            **common,
        )
    else:
        result = ingest_external_api(
            data_root=args.data_root,
            client=_make_client(args),
            use_watermark=args.use_watermark,
            explicit_instance_ids=args.instance_id,
            scope_name=args.scope_name,
            max_instances=args.max_instances,
            stream_types=streams,
            include_ranking_records=args.include_ranking_records,
            leaderboard_queries=leaderboard_queries,
            **common,
        )
    output.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
