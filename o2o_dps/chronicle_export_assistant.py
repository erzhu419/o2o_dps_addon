"""Queue official Chronicle exports and import each completed CSV on Windows.

The assistant uses Chronicle's documented External API only to enumerate raid
instances for known characters.  Event rows remain a manual browser export;
Chronicle does not expose them through the External API.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import webbrowser

from .chronicle_board_manifest import ChronicleBoardPDFError, load_manifest
from .import_chronicle_csv import ChronicleCSVError, ImportResult, import_chronicle_csv


SCHEMA_VERSION = 1
EXTERNAL_API_BASE = "https://capy.chronicleclassic.com/api/external/v1"
SITE_BASE = "https://capy.chronicleclassic.com"
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
QUEUE_RELATIVE_PATH = Path("chronicle_raw") / "export_queue.json"
OFFICIAL_UUID_DOWNLOAD = re.compile(
    r"^all-activity-(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(?: \(\d+\))?\.csv$",
    re.IGNORECASE,
)
QUEUE_REPLACE_RETRY_SECONDS = 5.0
QUEUE_REPLACE_POLL_SECONDS = 0.05

JsonGetter = Callable[[str], Any]


class ChronicleAssistantError(RuntimeError):
    """The export queue or documented External API could not be used."""


class ChronicleExternalAPIHTTPError(ChronicleAssistantError):
    """The documented External API returned a specific HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "BrainOfCat-O2O-DPS/0.1 (Chronicle External API)",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        message = ""
        try:
            body = json.loads(error.read().decode("utf-8", errors="replace"))
            if isinstance(body, dict):
                message = str(body.get("message") or "").strip()
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        detail = f": {message}" if message else ""
        raise ChronicleExternalAPIHTTPError(
            error.code,
            f"Chronicle External API returned HTTP {error.code}{detail}",
        ) from error
    except URLError as error:
        raise ChronicleAssistantError(
            f"cannot reach Chronicle External API: {error.reason}"
        ) from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleAssistantError(
            f"Chronicle External API returned an unreadable response: {error}"
        ) from error


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChronicleAssistantError(f"Chronicle response field {label!r} is not an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleAssistantError(f"Chronicle response field {label!r} is not a list")
    return value


def list_servers(*, get_json: JsonGetter = _request_json) -> list[dict[str, Any]]:
    body = _require_mapping(get_json(f"{EXTERNAL_API_BASE}/explore/servers"), "root")
    servers = _require_list(body.get("servers"), "servers")
    return [_require_mapping(server, "servers[]") for server in servers]


def _character_instances_url(server: str, realm: str, character: str, page: int) -> str:
    path = "/".join(quote(value, safe="") for value in (server, realm, character))
    query = urlencode({"page": page, "page_size": 50})
    return f"{EXTERNAL_API_BASE}/characters/{path}/instances?{query}"


def _source_key(source: dict[str, str]) -> tuple[str, str, str]:
    return source["server"], source["realm"], source["character"]


def _entry_aliases(entry: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for field in ("instance_id", "slug"):
        value = str(entry.get(field) or "").strip()
        if value and value not in aliases:
            aliases.append(value)
    return aliases


def _instance_entry(log: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
    instance_id = str(log.get("id") or "").strip()
    if not instance_id:
        raise ChronicleAssistantError("Chronicle returned a log without an id")

    slug_value = log.get("slug")
    slug = str(slug_value).strip() if slug_value is not None else ""
    route_key = slug or instance_id
    page_url = f"{SITE_BASE}/instances/{quote(route_key, safe='')}"

    entry: dict[str, Any] = {
        "instance_id": instance_id,
        "page_url": page_url,
        # This legacy view parameter opens all encounters with All Activity on top.
        # It cannot select all event streams; collect() prints that limitation.
        "export_url": f"{page_url}?v=all...aa-e-e-e-e",
        "discovered_from": [source],
        "status": "pending",
    }
    if slug:
        entry["slug"] = slug

    for field in (
        "name",
        "guild",
        "difficulty",
        "max_players",
        "boss_kills",
        "started_at",
        "ended_at",
        "uploaded_at",
        "performance",
    ):
        if field in log:
            entry[field] = log[field]
    return entry


def discover_instances(
    characters: Iterable[dict[str, str]],
    *,
    get_json: JsonGetter = _request_json,
) -> list[dict[str, Any]]:
    """Enumerate and deduplicate all documented instance history for characters."""

    discovered: dict[str, dict[str, Any]] = {}
    for raw_source in characters:
        source = {
            "server": str(raw_source["server"]).strip(),
            "realm": str(raw_source["realm"]).strip(),
            "character": str(raw_source["character"]).strip(),
        }
        if not all(source.values()):
            raise ChronicleAssistantError("server, realm, and character must not be empty")

        page = 1
        while True:
            body = _require_mapping(
                get_json(
                    _character_instances_url(
                        source["server"], source["realm"], source["character"], page
                    )
                ),
                "root",
            )
            logs = _require_list(body.get("logs"), "logs")
            pagination = _require_mapping(body.get("pagination"), "pagination")
            has_more = pagination.get("has_more")
            if not isinstance(has_more, bool):
                raise ChronicleAssistantError(
                    "Chronicle response field 'pagination.has_more' is not a boolean"
                )

            for raw_log in logs:
                log = _require_mapping(raw_log, "logs[]")
                entry = _instance_entry(log, source)
                existing = discovered.get(entry["instance_id"])
                if existing is None:
                    discovered[entry["instance_id"]] = entry
                    continue
                known_sources = {_source_key(item) for item in existing["discovered_from"]}
                if _source_key(source) not in known_sources:
                    existing["discovered_from"].append(source)

            if not has_more:
                break
            page += 1

    return sorted(
        discovered.values(),
        key=lambda item: str(item.get("started_at") or ""),
        reverse=True,
    )


def _character_instance_logs(
    source: dict[str, str],
    *,
    get_json: JsonGetter,
) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    page = 1
    while True:
        body = _require_mapping(
            get_json(
                _character_instances_url(
                    source["server"], source["realm"], source["character"], page
                )
            ),
            "root",
        )
        page_logs = _require_list(body.get("logs"), "logs")
        logs.extend(_require_mapping(log, "logs[]") for log in page_logs)
        pagination = _require_mapping(body.get("pagination"), "pagination")
        has_more = pagination.get("has_more")
        if not isinstance(has_more, bool):
            raise ChronicleAssistantError(
                "Chronicle response field 'pagination.has_more' is not a boolean"
            )
        if not has_more:
            return logs
        page += 1


def resolve_manifest_instances(
    manifest: dict[str, Any],
    *,
    get_json: JsonGetter = _request_json,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Resolve PDF slugs to official UUIDs through the documented External API."""

    target_order: list[str] = []
    for row in manifest["entries"]:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("instance_slug") or "").strip()
        if slug and slug not in target_order:
            target_order.append(slug)
    unresolved = set(target_order)
    resolved: dict[str, dict[str, Any]] = {}
    queried: dict[tuple[str, str, str], list[dict[str, Any]] | None] = {}
    queried_sources: list[dict[str, str]] = []
    server = str(manifest.get("server") or "Capybara").strip()

    for raw_row in manifest["entries"]:
        if not unresolved:
            break
        if not isinstance(raw_row, dict):
            continue
        row_slug = str(raw_row.get("instance_slug") or "").strip()
        if row_slug not in unresolved:
            continue
        source = {
            "server": server,
            "realm": str(raw_row.get("realm") or "").strip(),
            "character": str(raw_row.get("character") or "").strip(),
        }
        if not all(source.values()):
            continue
        source_key = _source_key(source)
        if source_key not in queried:
            try:
                queried[source_key] = _character_instance_logs(
                    source, get_json=get_json
                )
            except ChronicleExternalAPIHTTPError as error:
                if error.status_code != 404:
                    raise
                queried[source_key] = None
            queried_sources.append(source)

        logs = queried[source_key]
        if logs is None:
            continue
        for log in logs:
            slug = str(log.get("slug") or "").strip()
            if slug not in unresolved:
                continue
            resolved[slug] = _instance_entry(log, source)
            unresolved.remove(slug)

    return (
        [resolved[slug] for slug in target_order if slug in resolved],
        queried_sources,
        [slug for slug in target_order if slug in unresolved],
    )


def queue_path(data_root: str | Path = DEFAULT_DATA_ROOT) -> Path:
    return Path(data_root).expanduser().resolve() / QUEUE_RELATIVE_PATH


def load_queue(data_root: str | Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    path = queue_path(data_root)
    if not path.is_file():
        raise ChronicleAssistantError(
            f"export queue does not exist: {path}\nRun the discover command first."
        )
    try:
        queue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleAssistantError(f"cannot read export queue {path}: {error}") from error
    if not isinstance(queue, dict) or not isinstance(queue.get("entries"), list):
        raise ChronicleAssistantError(f"export queue has an invalid structure: {path}")
    return queue


def save_queue(queue: dict[str, Any], data_root: str | Path = DEFAULT_DATA_ROOT) -> Path:
    path = queue_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    queue["updated_at"] = _utc_now()
    temporary = path.with_suffix(".json.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(queue, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        replace_deadline = time.monotonic() + QUEUE_REPLACE_RETRY_SECONDS
        while True:
            try:
                temporary.replace(path)
                break
            except OSError as error:
                transient_windows_share = os.name == "nt" and (
                    isinstance(error, PermissionError)
                    or getattr(error, "winerror", None) in {5, 32, 33}
                )
                if not transient_windows_share or time.monotonic() >= replace_deadline:
                    raise
                time.sleep(QUEUE_REPLACE_POLL_SECONDS)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleAssistantError(f"cannot write export queue {path}: {error}") from error
    return path


def _unique_sources(sources: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        key = _source_key(source)
        if key not in seen:
            seen.add(key)
            unique.append(source)
    return unique


def merge_discovery(
    existing_queue: dict[str, Any] | None,
    instances: Iterable[dict[str, Any]],
    characters: Iterable[dict[str, str]],
) -> tuple[dict[str, Any], int]:
    """Merge discovery into the persistent queue without losing completion state."""

    now = _utc_now()
    if existing_queue is None:
        queue: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "chronicle_manual_export_queue",
            "created_at": now,
            "updated_at": now,
            "characters": [],
            "entries": [],
        }
    else:
        queue = existing_queue

    old_by_alias: dict[str, dict[str, Any]] = {}
    for entry in queue["entries"]:
        if not isinstance(entry, dict):
            continue
        for alias in _entry_aliases(entry):
            old_by_alias[alias] = entry
    merged_entries = list(queue["entries"])
    added = 0
    protected_fields = {
        "status",
        "imported_at",
        "import_receipt",
        "downloaded_file",
        "skipped_at",
        "skip_reason",
    }

    for discovered in instances:
        existing = next(
            (
                old_by_alias[alias]
                for alias in _entry_aliases(discovered)
                if alias in old_by_alias
            ),
            None,
        )
        if existing is None:
            merged_entries.append(discovered)
            for alias in _entry_aliases(discovered):
                old_by_alias[alias] = discovered
            added += 1
            continue

        prior_state = {field: existing[field] for field in protected_fields if field in existing}
        sources = _unique_sources(
            [*existing.get("discovered_from", []), *discovered["discovered_from"]]
        )
        existing.update(discovered)
        existing["discovered_from"] = sources
        existing.update(prior_state)
        for alias in _entry_aliases(existing):
            old_by_alias[alias] = existing

    queue["entries"] = sorted(
        merged_entries,
        key=lambda item: str(item.get("started_at") or ""),
        reverse=True,
    )
    queue["characters"] = _unique_sources(
        [*queue.get("characters", []), *characters]
    )
    return queue, added


def _leaderboard_row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("source_file"),
        row.get("source_page"),
        row.get("board_class"),
        row.get("board_spec"),
        row.get("rank"),
        row.get("character"),
        row.get("instance_slug"),
    )


def merge_manifest(
    existing_queue: dict[str, Any] | None,
    manifest: dict[str, Any],
    *,
    manifest_path: str | Path,
) -> tuple[dict[str, Any], int]:
    """Merge PDF-selected instance slugs into the existing manual export queue."""

    now = _utc_now()
    if existing_queue is None:
        queue: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "chronicle_manual_export_queue",
            "created_at": now,
            "updated_at": now,
            "characters": [],
            "entries": [],
        }
    else:
        queue = existing_queue

    old_by_alias: dict[str, dict[str, Any]] = {}
    for entry in queue["entries"]:
        if not isinstance(entry, dict):
            continue
        for alias in _entry_aliases(entry):
            old_by_alias[alias] = entry

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_row in manifest["entries"]:
        if not isinstance(raw_row, dict):
            raise ChronicleAssistantError("leaderboard manifest entry is not an object")
        slug = str(raw_row.get("instance_slug") or "").strip()
        instance_url = str(raw_row.get("instance_url") or "").strip()
        if not slug or instance_url != f"{SITE_BASE}/instances/{slug}":
            raise ChronicleAssistantError(
                "leaderboard manifest entry has an invalid instance slug or URL"
            )
        grouped.setdefault(slug, []).append(dict(raw_row))

    added = 0
    raid = str(manifest.get("raid") or "Raid").strip() or "Raid"
    for slug, rows in grouped.items():
        page_url = f"{SITE_BASE}/instances/{slug}"
        existing = old_by_alias.get(slug)
        if existing is None:
            existing = {
                "instance_id": slug,
                "slug": slug,
                "name": raid,
                "page_url": page_url,
                "export_url": f"{page_url}?v=all...aa-e-e-e-e",
                "discovered_from": [],
                "leaderboard_rows": [],
                "status": "pending",
            }
            queue["entries"].append(existing)
            old_by_alias[slug] = existing
            added += 1
        else:
            existing["slug"] = slug
            existing["page_url"] = page_url
            existing["export_url"] = f"{page_url}?v=all...aa-e-e-e-e"
            if not existing.get("name"):
                existing["name"] = raid

        known_rows = {
            _leaderboard_row_key(row)
            for row in existing.get("leaderboard_rows", [])
            if isinstance(row, dict)
        }
        for row in rows:
            key = _leaderboard_row_key(row)
            if key not in known_rows:
                existing.setdefault("leaderboard_rows", []).append(row)
                known_rows.add(key)

    resolved_manifest = str(Path(manifest_path).expanduser().resolve())
    manifest_record = {
        "path": resolved_manifest,
        "generated_at": manifest.get("generated_at"),
        "selection_policy": manifest.get("selection_policy"),
        "summary": manifest.get("summary"),
    }
    records = [
        record
        for record in queue.get("leaderboard_manifests", [])
        if isinstance(record, dict) and record.get("path") != resolved_manifest
    ]
    records.append(manifest_record)
    queue["leaderboard_manifests"] = records
    return queue, added


def matching_downloads(
    downloads: str | Path,
    instance_id: str,
    aliases: Iterable[str] = (),
) -> list[Path]:
    directory = Path(downloads).expanduser().resolve()
    if not directory.is_dir():
        raise ChronicleAssistantError(f"Downloads directory does not exist: {directory}")
    keys = []
    for value in (instance_id, *aliases):
        key = str(value).strip()
        if key and key not in keys:
            keys.append(key)
    alternatives = "|".join(re.escape(key) for key in keys)
    pattern = re.compile(
        rf"^all-activity-(?:{alternatives})(?: \(\d+\))?\.csv$",
        re.IGNORECASE,
    )
    matches = [path for path in directory.iterdir() if path.is_file() and pattern.match(path.name)]
    return sorted(matches, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def official_uuid_downloads(downloads: str | Path) -> list[Path]:
    directory = Path(downloads).expanduser().resolve()
    if not directory.is_dir():
        raise ChronicleAssistantError(f"Downloads directory does not exist: {directory}")
    matches = [
        path
        for path in directory.iterdir()
        if path.is_file() and OFFICIAL_UUID_DOWNLOAD.match(path.name)
    ]
    return sorted(matches, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def _candidate_downloads(
    entry: dict[str, Any],
    downloads: Path,
    *,
    allow_any_official_uuid: bool,
) -> list[Path]:
    candidates = matching_downloads(
        downloads, entry["instance_id"], _entry_aliases(entry)
    )
    if allow_any_official_uuid:
        known = set(candidates)
        candidates.extend(
            path for path in official_uuid_downloads(downloads) if path not in known
        )
        candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return candidates


def _file_version(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _record_import(
    queue: dict[str, Any],
    entry: dict[str, Any],
    source: Path,
    result: ImportResult,
    data_root: Path,
) -> None:
    entry["status"] = "imported"
    entry["imported_at"] = _utc_now()
    entry["downloaded_file"] = str(source)
    filename_match = OFFICIAL_UUID_DOWNLOAD.match(source.name)
    if filename_match is not None:
        entry["download_instance_id"] = filename_match.group("uuid")
    entry["import_receipt"] = result.as_dict()
    entry["event_stream_selection"] = (
        "requested_by_export_workflow; not machine-verifiable from CSV"
    )
    save_queue(queue, data_root)


def _try_candidates(
    queue: dict[str, Any],
    entry: dict[str, Any],
    downloads: Path,
    data_root: Path,
    failed_versions: dict[Path, tuple[int, int]],
    *,
    allow_any_official_uuid: bool = False,
) -> bool:
    for candidate in _candidate_downloads(
        entry,
        downloads,
        allow_any_official_uuid=allow_any_official_uuid,
    ):
        try:
            version = _file_version(candidate)
        except OSError:
            continue
        if failed_versions.get(candidate) == version:
            continue
        try:
            result = import_chronicle_csv(
                candidate,
                instance=entry["instance_id"],
                data_root=data_root,
            )
        except ChronicleCSVError as error:
            failed_versions[candidate] = version
            print(f"CSV 尚不可导入，继续等待新文件或文件完成：{candidate.name}")
            print(f"  {error}")
            continue

        _record_import(queue, entry, candidate, result, data_root)
        print(
            f"已导入 {entry['instance_id']}：{result.row_count} rows -> {result.normalized}"
        )
        return True
    return False


def collect_pending(
    queue: dict[str, Any],
    *,
    downloads: str | Path = DEFAULT_DOWNLOADS,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    limit: int = 0,
    timeout_minutes: float = 0,
    open_browser: bool = True,
    accept_existing: bool = False,
    poll_seconds: float = 1.0,
) -> int:
    """Open, watch, validate, import, and checkpoint pending manual exports."""

    resolved_downloads = Path(downloads).expanduser().resolve()
    resolved_root = Path(data_root).expanduser().resolve()
    # Validate before opening the first page.
    matching_downloads(resolved_downloads, "__directory_check__")

    pending = [entry for entry in queue["entries"] if entry.get("status") == "pending"]
    if limit > 0:
        pending = pending[:limit]
    if not pending:
        print("队列没有待导出的实例。")
        return 0

    print(
        "完整训练数据要求每个实例都启用 17 类 streams。Chronicle 默认只启用 4 类；"
        "页面中还需启用 13 个灰色/划线图标，然后点击 Export CSV。"
    )
    print(
        "需启用：Resource, Extra Attack, Aura, Spell Go, Aura Cast, Spell Start, "
        "Spell Fail, Classification, Combatant Info, Dispel, Interrupt, Absorbed, Consume。"
    )
    print("CSV 本身不能证明这些 streams 已启用；请只在完成上述选择后点击 Export CSV。")

    completed = 0
    for position, entry in enumerate(pending, start=1):
        instance_id = entry["instance_id"]
        print()
        print(f"[{position}/{len(pending)}] {entry.get('name') or 'Raid'} | {instance_id}")
        print(f"页面：{entry['export_url']}")
        unresolved_uuid = entry.get("uuid_resolution") == "unresolved_external_api"
        if unresolved_uuid:
            print("等待文件：此页面新生成的 all-activity-<instance UUID>.csv")
        else:
            print(f"等待文件：all-activity-{instance_id}.csv")

        failed_versions: dict[Path, tuple[int, int]] = {}
        if accept_existing:
            if _try_candidates(
                queue, entry, resolved_downloads, resolved_root, failed_versions
            ):
                completed += 1
                continue
        else:
            # A valid old CSV can still contain only Chronicle's four default
            # streams.  Ignore every unchanged pre-existing file so the normal
            # workflow accepts only an export created after the instructions
            # above were displayed.  --accept-existing is the explicit escape
            # hatch for a previously completed, operator-checked export.
            for candidate in _candidate_downloads(
                entry,
                resolved_downloads,
                allow_any_official_uuid=unresolved_uuid,
            ):
                try:
                    failed_versions[candidate] = _file_version(candidate)
                except OSError:
                    continue

        if open_browser:
            opened = webbrowser.open(entry["export_url"], new=0, autoraise=True)
            if not opened:
                print("默认浏览器未确认打开；请复制上面的页面 URL。")

        deadline = (
            time.monotonic() + timeout_minutes * 60 if timeout_minutes > 0 else None
        )
        last_notice = time.monotonic()
        while True:
            if _try_candidates(
                queue,
                entry,
                resolved_downloads,
                resolved_root,
                failed_versions,
                allow_any_official_uuid=unresolved_uuid,
            ):
                completed += 1
                break
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                print(f"等待超时；{instance_id} 仍为 pending，下次运行会继续。")
                return completed
            if now - last_notice >= 30:
                print(f"仍在等待 {resolved_downloads / ('all-activity-' + instance_id + '.csv')} ...")
                last_notice = now
            time.sleep(poll_seconds)
    return completed


def _status_counts(queue: dict[str, Any]) -> dict[str, int]:
    counts = {"pending": 0, "imported": 0, "skipped": 0}
    for entry in queue["entries"]:
        status = str(entry.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _print_status(queue: dict[str, Any], path: Path) -> None:
    counts = _status_counts(queue)
    print(f"队列：{path}")
    print(
        f"总数 {len(queue['entries'])} | pending {counts.get('pending', 0)} | "
        f"imported {counts.get('imported', 0)} | skipped {counts.get('skipped', 0)}"
    )
    next_entry = next(
        (entry for entry in queue["entries"] if entry.get("status") == "pending"),
        None,
    )
    if next_entry is not None:
        print(
            f"下一条：{next_entry.get('name') or 'Raid'} | "
            f"{next_entry['instance_id']}\n{next_entry['export_url']}"
        )


def _load_optional_queue(data_root: Path) -> dict[str, Any] | None:
    if not queue_path(data_root).is_file():
        return None
    return load_queue(data_root)


def _command_servers(args: argparse.Namespace) -> int:
    servers = list_servers()
    if args.json:
        print(json.dumps({"servers": servers}, ensure_ascii=False, indent=2))
        return 0
    for server in servers:
        print(str(server.get("name") or server.get("id") or "<unnamed server>"))
        realms = server.get("realms")
        if isinstance(realms, list):
            for realm in realms:
                if isinstance(realm, dict):
                    print(f"  - {realm.get('name') or realm.get('id')}")
    return 0


def _command_discover(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    characters = [
        {"server": args.server, "realm": args.realm, "character": character}
        for character in args.character
    ]
    instances = discover_instances(characters)
    queue, added = merge_discovery(_load_optional_queue(data_root), instances, characters)
    path = save_queue(queue, data_root)
    print(f"发现 {len(instances)} 个去重后的实例；新增 {added}。")
    _print_status(queue, path)
    return 0


def _command_import_manifest(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    try:
        manifest = load_manifest(args.manifest)
    except ChronicleBoardPDFError as error:
        raise ChronicleAssistantError(str(error)) from error
    queue, added = merge_manifest(
        _load_optional_queue(data_root),
        manifest,
        manifest_path=args.manifest,
    )
    resolved_count = 0
    unresolved_slugs: list[str] = []
    if args.resolve_external_api:
        resolved_instances, queried_sources, unresolved_slugs = resolve_manifest_instances(
            manifest
        )
        queue, _ = merge_discovery(queue, resolved_instances, queried_sources)
        resolved_count = len(resolved_instances)
        unresolved_set = set(unresolved_slugs)
        for entry in queue["entries"]:
            slug = str(entry.get("slug") or "").strip()
            if not slug:
                continue
            entry["uuid_resolution"] = (
                "unresolved_external_api"
                if slug in unresolved_set
                else "resolved_external_api"
            )
    path = save_queue(queue, data_root)
    print(
        f"Manifest 含 {len(manifest['entries'])} 条排行榜记录；"
        f"队列新增 {added} 个去重实例。"
    )
    if args.resolve_external_api:
        print(f"External API 已把 {resolved_count} 个 slug 解析为官方 instance UUID。")
        if unresolved_slugs:
            print(
                f"另有 {len(unresolved_slugs)} 个公开 slug 未被 External API 返回；"
                "收集器将只接收打开该页面后新生成的官方 UUID CSV。"
            )
    _print_status(queue, path)
    return 0


def _command_collect(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    queue = load_queue(data_root)
    completed = collect_pending(
        queue,
        downloads=args.downloads,
        data_root=data_root,
        limit=args.limit,
        timeout_minutes=args.timeout_minutes,
        open_browser=not args.no_browser,
        accept_existing=args.accept_existing,
    )
    print(f"本次完成 {completed} 个实例。")
    _print_status(queue, queue_path(data_root))
    return 0


def _command_status(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    queue = load_queue(data_root)
    _print_status(queue, queue_path(data_root))
    return 0


def _command_skip(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    queue = load_queue(data_root)
    for entry in queue["entries"]:
        if entry.get("instance_id") == args.instance:
            entry["status"] = "skipped"
            entry["skipped_at"] = _utc_now()
            entry["skip_reason"] = args.reason
            save_queue(queue, data_root)
            print(f"已跳过 {args.instance}：{args.reason}")
            return 0
    raise ChronicleAssistantError(f"instance is not in the queue: {args.instance}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronicle_export_assistant",
        description=(
            "Queue Chronicle instances from the official External API or a local "
            "leaderboard PDF manifest, then import manual All Activity CSV exports."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    servers_parser = subparsers.add_parser("servers", help="list current server/realm names")
    servers_parser.add_argument("--json", action="store_true", help="print the API response as JSON")
    servers_parser.set_defaults(handler=_command_servers)

    discover_parser = subparsers.add_parser(
        "discover", help="enumerate all instances for one or more characters"
    )
    discover_parser.add_argument("--server", required=True)
    discover_parser.add_argument("--realm", required=True)
    discover_parser.add_argument(
        "--character",
        action="append",
        required=True,
        help="character name, GUID, or decimal game ID; repeat for more characters",
    )
    discover_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    discover_parser.set_defaults(handler=_command_discover)

    manifest_parser = subparsers.add_parser(
        "import-manifest", help="merge a local leaderboard PDF manifest into the queue"
    )
    manifest_parser.add_argument("manifest", type=Path)
    manifest_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    manifest_parser.add_argument(
        "--resolve-external-api",
        action="store_true",
        help="resolve route slugs to official instance UUIDs using leaderboard characters",
    )
    manifest_parser.set_defaults(handler=_command_import_manifest)

    collect_parser = subparsers.add_parser(
        "collect", help="open each queued instance and import its downloaded CSV"
    )
    collect_parser.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS)
    collect_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    collect_parser.add_argument(
        "--limit", type=int, default=0, help="maximum instances this run; 0 means all"
    )
    collect_parser.add_argument(
        "--timeout-minutes",
        type=float,
        default=0,
        help="stop waiting for an instance after this many minutes; 0 waits indefinitely",
    )
    collect_parser.add_argument(
        "--no-browser", action="store_true", help="watch Downloads without opening pages"
    )
    collect_parser.add_argument(
        "--accept-existing",
        action="store_true",
        help=(
            "accept a matching CSV already present at startup; use only after "
            "confirming all required event streams were enabled for that export"
        ),
    )
    collect_parser.set_defaults(handler=_command_collect)

    status_parser = subparsers.add_parser("status", help="show queue progress and next URL")
    status_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    status_parser.set_defaults(handler=_command_status)

    skip_parser = subparsers.add_parser("skip", help="skip one inaccessible instance")
    skip_parser.add_argument("instance")
    skip_parser.add_argument("--reason", required=True)
    skip_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    skip_parser.set_defaults(handler=_command_skip)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if getattr(args, "limit", 0) < 0:
            raise ChronicleAssistantError("--limit must be 0 or greater")
        if getattr(args, "timeout_minutes", 0) < 0:
            raise ChronicleAssistantError("--timeout-minutes must be 0 or greater")
        return int(args.handler(args))
    except ChronicleAssistantError as error:
        print(f"Chronicle export assistant failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止。已成功导入的进度已保存；当前实例仍为 pending。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
