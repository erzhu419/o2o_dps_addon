"""Collect the small live Timer calibration debug export and matching WoW logs.

The addon overwrites one JSON document in ``WoW/Imports``.  This collector
polls that document, appends only unseen trace entries, and copies only the
bytes produced during the detected run.  The combat log receives a 64 KiB
prelude because the first export can arrive after the first combat event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEBUG_SCHEMAS = frozenset(
    {
        "brainofcat_timer_live_debug/v1",
        "brain_of_cat_timer_debug_v1",
    }
)
COMBAT_LOG_PRELUDE_BYTES = 64 * 1024
SUPPORT_LOG_PRELUDE_BYTES = 32 * 1024
TERMINAL_LOG_SYNC_GRACE_SECONDS = 10.0


class TimerLiveDebugError(RuntimeError):
    """The live debug export cannot be read or published on this poll."""


@dataclass(frozen=True)
class StableBytes:
    path: Path
    data: bytes
    size: int
    mtime_ns: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _utc_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def derive_wow_root(savedvariables_path: str | Path) -> Path | None:
    """Return the parent of the ``WTF`` directory in an explicit WoW path."""

    source = Path(savedvariables_path).expanduser().resolve()
    for parent in source.parents:
        if parent.name.casefold() == "wtf":
            return parent.parent
    return None


def _safe_run_name(campaign_run_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", campaign_run_id).strip("._-")
    if not cleaned:
        cleaned = "timer_run"
    return cleaned[:160]


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_stable(path: Path) -> StableBytes:
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise TimerLiveDebugError(f"cannot read live debug export {path}: {error}") from error
    before_signature = (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
    after_signature = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if before_signature != after_signature or len(data) != after.st_size:
        raise TimerLiveDebugError(f"live debug export changed while being read: {path}")
    return StableBytes(path=path, data=data, size=after.st_size, mtime_ns=after.st_mtime_ns)


def _parse_debug_document(stable: StableBytes) -> dict[str, Any]:
    try:
        text = stable.data.decode("utf-8-sig")
    except UnicodeError as error:
        raise TimerLiveDebugError(
            f"live debug export is not UTF-8: {stable.path}: {error}"
        ) from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise TimerLiveDebugError(
            f"live debug export is not complete JSON: {stable.path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise TimerLiveDebugError("live debug export must be one JSON object")
    schema = document.get("schema")
    if schema not in DEBUG_SCHEMAS:
        raise TimerLiveDebugError(f"unsupported live debug schema: {schema!r}")
    if document.get("schemaVersion") != SCHEMA_VERSION:
        raise TimerLiveDebugError(
            f"unsupported live debug schemaVersion: {document.get('schemaVersion')!r}"
        )
    campaign_run_id = document.get("campaignRunId")
    idle_handshake = document.get("status") == "idle" or document.get("ready") is True
    if (
        (not isinstance(campaign_run_id, str) or not campaign_run_id.strip())
        and not idle_handshake
    ):
        raise TimerLiveDebugError("running live debug export has no campaignRunId")
    revision = document.get("revision")
    if not isinstance(revision, (int, float)) or isinstance(revision, bool):
        raise TimerLiveDebugError("live debug export has no numeric revision")
    trace = document.get("trace", document.get("records", []))
    if not isinstance(trace, list):
        raise TimerLiveDebugError("live debug trace must be a list")
    return document


def _diagnostic_projection(document: dict[str, Any]) -> dict[str, Any]:
    snapshot = document.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    exported_at = document.get("exportedAt")
    if not isinstance(exported_at, dict):
        exported_at = {}
    trace = document.get("trace", document.get("records", []))
    latest_trace = trace[-1] if isinstance(trace, list) and trace else {}
    if not isinstance(latest_trace, dict):
        latest_trace = {}
    projected: dict[str, Any] = {}
    for key in (
        "stage",
        "substep",
        "subtest",
        "spell",
        "trial",
        "waitingFor",
        "reason",
        "gate",
        "lastObserved",
        "lastPress",
        "debugErrors",
        "latestError",
        "readiness",
    ):
        value = document.get(key)
        if value is None:
            value = snapshot.get(key)
        if value is None:
            value = latest_trace.get(key)
        if value is not None:
            projected[key] = value
    if document.get("status") is not None:
        projected["campaignStatus"] = document.get("status")
    if "lastObserved" not in projected and snapshot.get("lastEvent") is not None:
        projected["lastObserved"] = snapshot.get("lastEvent")
    gate = snapshot.get("gate")
    if (
        "waitingFor" not in projected
        and isinstance(gate, dict)
        and gate.get("waitingFor") is not None
    ):
        projected["waitingFor"] = gate.get("waitingFor")
    if "lastPress" not in projected and isinstance(trace, list):
        for item in reversed(trace):
            if not isinstance(item, dict):
                continue
            boundary = item.get("kind") or item.get("name") or item.get("event")
            if boundary in {"PRESS_ENTER", "PRESS_EXIT"}:
                projected["lastPress"] = item
                break
    projected["wallClock"] = (
        document.get("wallClock")
        or snapshot.get("wallClock")
        or exported_at.get("wallClock")
        or latest_trace.get("wallClock")
    )
    projected["gameTime"] = (
        document.get("gameTime")
        or snapshot.get("gameTime")
        or exported_at.get("getTime")
        or latest_trace.get("gameTime")
    )
    errors = projected.get("debugErrors")
    if "latestError" not in projected and isinstance(errors, list) and errors:
        projected["latestError"] = errors[-1]
    return projected


class TimerLiveDebugCollector:
    """Persist one compact diagnostic bundle per timer campaign run."""

    def __init__(
        self,
        savedvariables_path: str | Path,
        *,
        data_root: str | Path,
        runtime_directory: str | Path,
    ) -> None:
        self.source = Path(savedvariables_path).expanduser().resolve()
        self.data_root = Path(data_root).expanduser().resolve()
        self.runtime_directory = Path(runtime_directory).expanduser().resolve()
        self.wow_root = derive_wow_root(self.source)
        self.bundle_root = self.data_root / "timer_calibration_debug"
        self.state_path = self.runtime_directory / "timer_live_state.json"
        self.status_path = self.runtime_directory / "timer_live_status.json"

    def _write_status(self, status: str, message: str, **fields: Any) -> dict[str, Any]:
        document = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "message": message,
            "source": str(self.source),
            "wow_root": str(self.wow_root) if self.wow_root is not None else None,
            "updated_at": _utc_now(),
            **fields,
        }
        _atomic_json(self.status_path, document)
        return document

    def write_error_status(self, error: BaseException) -> dict[str, Any]:
        return self._write_status(
            "poll_error",
            "Timer live diagnostics failed on this poll; the main calibration watcher continues.",
            error=str(error),
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"schema_version": SCHEMA_VERSION, "runs": {}}
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TimerLiveDebugError(
                f"cannot read timer live state {self.state_path}: {error}"
            ) from error
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise TimerLiveDebugError(f"unsupported timer live state: {self.state_path}")
        if not isinstance(document.get("runs"), dict):
            raise TimerLiveDebugError(f"timer live state has no runs mapping: {self.state_path}")
        return document

    def _debug_export_path(self) -> Path | None:
        if self.wow_root is None:
            return None
        imports = self.wow_root / "Imports"
        candidates = [
            imports / "BrainOfCatTimerDebug.txt",
            imports / "BrainOfCatTimerDebug",
        ]
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            return None
        return max(existing, key=lambda path: path.stat().st_mtime_ns)

    def _allocate_safe_run(self, state: dict[str, Any], campaign_run_id: str) -> str:
        base = _safe_run_name(campaign_run_id)
        used = {
            item.get("safe_run")
            for run_id, item in state["runs"].items()
            if run_id != campaign_run_id and isinstance(item, dict)
        }
        if base not in used:
            return base
        suffix = 2
        while f"{base}_{suffix}" in used:
            suffix += 1
        return f"{base}_{suffix}"

    def _source_specs(self) -> tuple[tuple[str, Path, str, str], ...]:
        assert self.wow_root is not None
        logs = self.wow_root / "Logs"
        return (
            ("wow_combat_log", logs / "WoWCombatLog.txt", "WoWCombatLog", ".txt"),
            ("frame_xml", logs / "FrameXML.log", "FrameXML", ".log"),
            ("nampower_debug", logs / "nampower_debug.log", "nampower_debug", ".log"),
            (
                "nampower_debug_1",
                logs / "nampower_debug.log.1",
                "nampower_debug_1",
                ".log",
            ),
            (
                "nampower_debug_2",
                logs / "nampower_debug.log.2",
                "nampower_debug_2",
                ".log",
            ),
            (
                "nampower_debug_3",
                logs / "nampower_debug.log.3",
                "nampower_debug_3",
                ".log",
            ),
        )

    def _new_run(
        self,
        state: dict[str, Any],
        campaign_run_id: str,
        stable: StableBytes,
    ) -> dict[str, Any]:
        safe_run = self._allocate_safe_run(state, campaign_run_id)
        bundle = self.bundle_root / safe_run
        bundle.mkdir(parents=True, exist_ok=True)
        sources: dict[str, Any] = {}
        for key, path, stem, suffix in self._source_specs():
            try:
                stat = path.stat()
            except OSError:
                prelude = (
                    COMBAT_LOG_PRELUDE_BYTES
                    if key == "wow_combat_log"
                    else SUPPORT_LOG_PRELUDE_BYTES
                )
                sources[key] = {
                    "source": str(path),
                    "stem": stem,
                    "suffix": suffix,
                    "offset": 0,
                    "initial_source_offset": 0,
                    "part": 0,
                    "identity": None,
                    "bytes_copied": 0,
                    "truncations": 0,
                    "prelude_bytes": prelude,
                }
                continue
            prelude = (
                COMBAT_LOG_PRELUDE_BYTES
                if key == "wow_combat_log"
                else SUPPORT_LOG_PRELUDE_BYTES
            )
            offset = max(0, stat.st_size - prelude)
            sources[key] = {
                "source": str(path),
                "stem": stem,
                "suffix": suffix,
                "offset": offset,
                "initial_source_offset": offset,
                "part": 0,
                "identity": [stat.st_dev, stat.st_ino],
                "bytes_copied": 0,
                "truncations": 0,
                "prelude_bytes": prelude,
            }
        return {
            "campaign_run_id": campaign_run_id,
            "safe_run": safe_run,
            "bundle": str(bundle),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "debug_source": str(stable.path),
            "debug_source_mtime_ns": stable.mtime_ns,
            "last_revision": -1,
            "last_debug_sequence": 0,
            "trace_entries": 0,
            "terminal": False,
            "terminal_log_sync_status": "not_started",
            "sources": sources,
        }

    def _part_path(self, bundle: Path, source_state: dict[str, Any]) -> Path:
        return bundle / (
            f"{source_state['stem']}.part{int(source_state['part']):03d}"
            f"{source_state['suffix']}"
        )

    def _sync_source(
        self,
        bundle: Path,
        source_state: dict[str, Any],
        prior_identity_offsets: dict[tuple[int, int], int],
    ) -> dict[str, Any]:
        path = Path(source_state["source"])
        initial_archive_part = self._part_path(bundle, source_state)
        initial_archive_parts = sorted(
            str(part.resolve())
            for part in bundle.glob(
                f"{source_state['stem']}.part*{source_state['suffix']}"
            )
            if part.is_file()
        )
        try:
            stat = path.stat()
        except OSError as error:
            return {
                "status": "missing",
                "source": str(path),
                "error": str(error),
                "source_size": None,
                "source_mtime_ns": None,
                "offset": int(source_state.get("offset", 0)),
                "archive_part": (
                    str(initial_archive_part.resolve())
                    if initial_archive_part.is_file()
                    else None
                ),
                "archive_parts": initial_archive_parts,
            }
        identity = [stat.st_dev, stat.st_ino]
        offset = int(source_state.get("offset", 0))
        previous_identity = source_state.get("identity")
        identity_changed = previous_identity not in (None, identity)
        if identity_changed:
            source_state["part"] = int(source_state.get("part", 0)) + 1
            source_state["truncations"] = int(source_state.get("truncations", 0)) + 1
            continued_offset = prior_identity_offsets.get((stat.st_dev, stat.st_ino))
            if continued_offset is not None and continued_offset <= stat.st_size:
                offset = continued_offset
            else:
                offset = max(
                    0,
                    stat.st_size - int(
                        source_state.get("prelude_bytes", SUPPORT_LOG_PRELUDE_BYTES)
                    ),
                )
        elif previous_identity is None:
            continued_offset = prior_identity_offsets.get((stat.st_dev, stat.st_ino))
            if continued_offset is not None and continued_offset <= stat.st_size:
                offset = continued_offset
            else:
                offset = max(
                    0,
                    stat.st_size - int(
                        source_state.get("prelude_bytes", SUPPORT_LOG_PRELUDE_BYTES)
                    ),
                )
        elif stat.st_size < offset:
            source_state["part"] = int(source_state.get("part", 0)) + 1
            source_state["truncations"] = int(source_state.get("truncations", 0)) + 1
            offset = 0
        source_state["identity"] = identity
        try:
            with path.open("rb") as source_handle:
                source_handle.seek(offset)
                chunk = source_handle.read()
        except OSError as error:
            return {
                "status": "read_error",
                "source": str(path),
                "error": str(error),
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "offset": int(source_state.get("offset", 0)),
                "archive_part": (
                    str(self._part_path(bundle, source_state).resolve())
                    if self._part_path(bundle, source_state).is_file()
                    else None
                ),
                "archive_parts": initial_archive_parts,
            }
        if chunk:
            destination = self._part_path(bundle, source_state)
            with destination.open("ab") as output_handle:
                output_handle.write(chunk)
            offset += len(chunk)
            source_state["bytes_copied"] = int(source_state.get("bytes_copied", 0)) + len(chunk)
        source_state["offset"] = offset
        archive_part = self._part_path(bundle, source_state)
        existing_parts = sorted(
            str(path.resolve())
            for path in bundle.glob(
                f"{source_state['stem']}.part*{source_state['suffix']}"
            )
            if path.is_file()
        )
        return {
            "status": "ok",
            "source": str(path),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "offset": offset,
            "part": int(source_state["part"]),
            "bytes_copied": int(source_state.get("bytes_copied", 0)),
            "truncations": int(source_state.get("truncations", 0)),
            "archive_part": str(archive_part.resolve()) if archive_part.is_file() else None,
            "archive_parts": existing_parts,
        }

    def _sync_logs(self, run: dict[str, Any]) -> dict[str, Any]:
        bundle = Path(run["bundle"])
        results: dict[str, Any] = {}
        prior_identity_offsets: dict[tuple[int, int], int] = {}
        for source_state in run["sources"].values():
            identity = source_state.get("identity")
            if (
                isinstance(identity, list)
                and len(identity) == 2
                and all(isinstance(value, int) for value in identity)
            ):
                prior_identity_offsets[(identity[0], identity[1])] = int(
                    source_state.get("offset", 0)
                )
        for key, source_state in run["sources"].items():
            results[key] = self._sync_source(
                bundle, source_state, prior_identity_offsets
            )
        run["last_log_capture"] = results
        return results

    def _terminal_log_sync_info(self, run: dict[str, Any]) -> dict[str, Any]:
        raw_deadline = run.get("terminal_pending_log_sync_until")
        deadline = (
            float(raw_deadline)
            if isinstance(raw_deadline, (int, float))
            and not isinstance(raw_deadline, bool)
            else None
        )
        return {
            "status": run.get("terminal_log_sync_status", "not_started"),
            "grace_seconds": TERMINAL_LOG_SYNC_GRACE_SECONDS,
            "started_at": run.get("terminal_log_sync_started_at"),
            "terminal_pending_log_sync_until": deadline,
            "pending_until": _utc_from_epoch(deadline) if deadline is not None else None,
            "last_sync_at": run.get("terminal_log_sync_last_at"),
            "completed_at": run.get("terminal_log_sync_completed_at"),
        }

    def _arm_terminal_log_sync(
        self,
        run: dict[str, Any],
        now_epoch: float,
    ) -> None:
        run["terminal"] = True
        if run.get("terminal_log_sync_status") == "complete":
            return
        raw_deadline = run.get("terminal_pending_log_sync_until")
        if not (
            isinstance(raw_deadline, (int, float))
            and not isinstance(raw_deadline, bool)
        ):
            run["terminal_log_sync_started_at"] = _utc_now()
            run["terminal_pending_log_sync_until"] = (
                now_epoch + TERMINAL_LOG_SYNC_GRACE_SECONDS
            )
        run["terminal_log_sync_status"] = "pending"

    def _record_terminal_log_sync(
        self,
        run: dict[str, Any],
        now_epoch: float,
        *,
        force_complete: bool = False,
    ) -> None:
        run["terminal_log_sync_last_at"] = _utc_now()
        raw_deadline = run.get("terminal_pending_log_sync_until")
        deadline = (
            float(raw_deadline)
            if isinstance(raw_deadline, (int, float))
            and not isinstance(raw_deadline, bool)
            else now_epoch
        )
        if force_complete or now_epoch >= deadline:
            run["terminal_log_sync_status"] = "complete"
            run["terminal_log_sync_completed_at"] = _utc_now()
        else:
            run["terminal_log_sync_status"] = "pending"

    def _load_snapshot_document(self, run: dict[str, Any]) -> dict[str, Any] | None:
        snapshot_path = Path(run["bundle"]) / "latest_snapshot.json"
        if not snapshot_path.is_file():
            return None
        try:
            candidate = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TimerLiveDebugError(
                f"cannot read timer live snapshot {snapshot_path}: {error}"
            ) from error
        if not isinstance(candidate, dict):
            raise TimerLiveDebugError(
                f"timer live snapshot must be one JSON object: {snapshot_path}"
            )
        if candidate.get("campaignRunId") != run.get("campaign_run_id"):
            return None
        return candidate

    def _refresh_pending_terminal_runs(
        self,
        state: dict[str, Any],
        now_epoch: float,
        *,
        exclude_run_id: str | None = None,
    ) -> dict[str, Any]:
        refreshed: list[str] = []
        completed: list[str] = []
        log_capture: dict[str, Any] = {}
        for campaign_run_id, run in state["runs"].items():
            if campaign_run_id == exclude_run_id or not isinstance(run, dict):
                continue
            if (
                run.get("terminal") is not True
                or run.get("terminal_log_sync_status") != "pending"
            ):
                continue
            results = self._sync_logs(run)
            self._record_terminal_log_sync(run, now_epoch)
            run["updated_at"] = _utc_now()
            self._write_manifest(
                run,
                document=self._load_snapshot_document(run),
                log_results=results,
            )
            refreshed.append(campaign_run_id)
            log_capture[campaign_run_id] = results
            if run.get("terminal_log_sync_status") == "complete":
                completed.append(campaign_run_id)
        pending = [
            campaign_run_id
            for campaign_run_id, run in state["runs"].items()
            if campaign_run_id != exclude_run_id
            and isinstance(run, dict)
            and run.get("terminal") is True
            and run.get("terminal_log_sync_status") == "pending"
        ]
        return {
            "status": (
                "pending" if pending else "complete" if completed else "idle"
            ),
            "grace_seconds": TERMINAL_LOG_SYNC_GRACE_SECONDS,
            "refreshed_runs": refreshed,
            "pending_runs": pending,
            "completed_runs": completed,
            "log_capture": log_capture,
        }

    def _append_trace(
        self,
        run: dict[str, Any],
        document: dict[str, Any],
        revision: int,
    ) -> int:
        trace = document.get("trace", document.get("records", []))
        last_sequence = int(run.get("last_debug_sequence", 0))
        appended = 0
        destination = Path(run["bundle"]) / "trace.jsonl"
        for item in trace:
            if not isinstance(item, dict):
                continue
            raw_sequence = item.get("debugSequence", item.get("sequence"))
            if not isinstance(raw_sequence, (int, float)) or isinstance(raw_sequence, bool):
                continue
            sequence = int(raw_sequence)
            if sequence <= last_sequence:
                continue
            record = dict(item)
            record["debugSequence"] = sequence
            record["sourceRevision"] = revision
            record["collectedAt"] = _utc_now()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("a", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
            last_sequence = sequence
            appended += 1
        run["last_debug_sequence"] = last_sequence
        run["trace_entries"] = int(run.get("trace_entries", 0)) + appended
        return appended

    def _is_terminal(self, document: dict[str, Any]) -> bool:
        if document.get("terminal") is True:
            return True
        return document.get("status") in {
            "awaiting_export_reload",
            "exported_reload_seen",
            "aborted",
            "complete",
            "completed",
        }

    def _write_manifest(
        self,
        run: dict[str, Any],
        *,
        document: dict[str, Any] | None,
        log_results: dict[str, Any],
    ) -> None:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "brainofcat_timer_live_debug_bundle",
            "campaign_run_id": run["campaign_run_id"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "terminal": run.get("terminal") is True,
            "last_revision": run.get("last_revision"),
            "last_debug_sequence": run.get("last_debug_sequence"),
            "trace_entries": run.get("trace_entries"),
            "debug_source": run.get("debug_source"),
            "terminal_log_sync": self._terminal_log_sync_info(run),
            "log_capture": log_results,
            "files": {
                "trace": str(Path(run["bundle"]) / "trace.jsonl"),
                "latest_snapshot": str(Path(run["bundle"]) / "latest_snapshot.json"),
                "log_parts": {
                    key: result.get("archive_parts", [])
                    for key, result in log_results.items()
                },
            },
        }
        if document is not None:
            manifest["terminal_event"] = document.get("terminalEvent")
            manifest["campaign_mode"] = document.get("campaignMode")
            manifest["source_campaign_run_id"] = document.get(
                "sourceCampaignRunId"
            )
            manifest["stage"] = document.get("stage")
            manifest["substep"] = document.get("substep")
            manifest["status"] = document.get("status")
        _atomic_json(Path(run["bundle"]) / "manifest.json", manifest)

    @staticmethod
    def _marker_names(trace_path: Path) -> set[str]:
        names: set[str] = set()
        if not trace_path.is_file():
            return names
        with trace_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") == "MARKER" and isinstance(row.get("name"), str):
                    names.add(row["name"])
        return names

    def _write_recovery_composite(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        document: dict[str, Any],
    ) -> dict[str, Any] | None:
        if document.get("campaignMode") != "stage_d_recovery" or not run.get(
            "terminal"
        ):
            return None
        source_run_id = document.get("sourceCampaignRunId")
        source_run = (
            state.get("runs", {}).get(source_run_id)
            if isinstance(source_run_id, str)
            else None
        )
        source_bundle = (
            Path(source_run["bundle"])
            if isinstance(source_run, dict) and isinstance(source_run.get("bundle"), str)
            else None
        )
        recovery_bundle = Path(run["bundle"])
        source_markers = self._marker_names(
            source_bundle / "trace.jsonl" if source_bundle else Path("")
        )
        recovery_markers = self._marker_names(recovery_bundle / "trace.jsonl")
        snapshot = document.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        last_marker = snapshot.get("lastMarker")
        last_marker = last_marker if isinstance(last_marker, dict) else {}
        details = last_marker.get("details")
        details = details if isinstance(details, dict) else {}
        heroic = details.get("heroicStrike")
        heroic = heroic if isinstance(heroic, dict) else {}
        target_status = heroic.get("targetSwitchStatus")
        checks = {
            "source_run_link_resolved": source_bundle is not None,
            "source_timer_stage_completed": (
                "CALIBRATION_TIMER_STAGE_COMPLETED" in source_markers
            ),
            "source_swing_stage_completed": (
                "CALIBRATION_HASTE_STAGE_STARTED" in source_markers
            ),
            "source_haste_stage_completed": (
                "CALIBRATION_HASTE_STAGE_COMPLETED" in source_markers
            ),
            "source_reached_stage_d": (
                "CALIBRATION_QUEUE_STAGE_STARTED" in source_markers
            ),
            "recovery_start_marker": (
                "CALIBRATION_STAGE_D_RECOVERY_STARTED" in recovery_markers
            ),
            "recovery_cancel_completed_marker": (
                "CALIBRATION_HS_CANCEL_COMPLETED" in recovery_markers
            ),
            "recovery_terminal_marker": (
                document.get("terminalEvent")
                == "CALIBRATION_STAGE_D_RECOVERY_COMPLETED"
            ),
            "recovery_cancel_completed": heroic.get("cancelCompleted") is True,
            "recovery_strong_cancel_support": (
                heroic.get("strongCancelSupport") is True
            ),
            "recovery_next_main_hand_white": (
                heroic.get("nextMainHandWasWhite") is True
            ),
            "recovery_off_hand_continued": heroic.get("offHandContinued") is True,
            "recovery_target_switch_resolved": target_status
            in {"EXTERNAL_HOLD", "COMPLETED"},
            "recovery_loadout_restored": details.get("loadoutRestored") is True,
        }
        composite = {
            "schema_version": 1,
            "kind": "brainofcat_timer_source_recovery_composite",
            "created_at": _utc_now(),
            "status": "complete" if all(checks.values()) else "blocked",
            "source_campaign_run_id": source_run_id,
            "recovery_campaign_run_id": run["campaign_run_id"],
            "source_bundle": str(source_bundle) if source_bundle else None,
            "recovery_bundle": str(recovery_bundle),
            "evidence_boundary": {
                "source": "stages_A_to_C_and_failed_legacy_D_trials",
                "recovery": "stage_D_and_loadout_restore",
            },
            "loadout_provenance": details.get("recoveryLoadoutEvidence"),
            "checks": checks,
            "heroic_strike": heroic,
            "files": {
                "source_trace": (
                    str(source_bundle / "trace.jsonl") if source_bundle else None
                ),
                "recovery_trace": str(recovery_bundle / "trace.jsonl"),
            },
        }
        output = recovery_bundle / "composite_evidence.json"
        _atomic_json(output, composite)
        return {"status": composite["status"], "output": str(output), "checks": checks}

    def poll(self) -> dict[str, Any]:
        if self.wow_root is None:
            return self._write_status(
                "not_available",
                "The SavedVariables path has no WTF ancestor; no WoW log root can be derived.",
            )
        now_epoch = _utc_epoch()
        debug_path = self._debug_export_path()
        if debug_path is None:
            state = self._load_state()
            terminal_log_sync = self._refresh_pending_terminal_runs(
                state, now_epoch
            )
            if terminal_log_sync["refreshed_runs"]:
                state["updated_at"] = _utc_now()
                _atomic_json(self.state_path, state)
            return self._write_status(
                "waiting_for_debug_export",
                "Waiting for Imports/BrainOfCatTimerDebug.txt from a Timer campaign.",
                terminal_log_sync=terminal_log_sync,
            )
        stable = _read_stable(debug_path)
        document = _parse_debug_document(stable)
        revision = int(document["revision"])
        projection = _diagnostic_projection(document)
        raw_campaign_run_id = document.get("campaignRunId")
        if not isinstance(raw_campaign_run_id, str) or not raw_campaign_run_id.strip():
            state = self._load_state()
            terminal_log_sync = self._refresh_pending_terminal_runs(
                state, now_epoch
            )
            if terminal_log_sync["refreshed_runs"]:
                state["updated_at"] = _utc_now()
                _atomic_json(self.state_path, state)
            readiness = document.get("readiness")
            ready = document.get("ready") is True or (
                isinstance(readiness, dict) and readiness.get("ready") is True
            )
            return self._write_status(
                "ready_idle",
                "Timer live debug handshake is ready; no campaign run has started yet.",
                revision=revision,
                ready=ready,
                diagnostic_session_id=document.get("diagnosticSessionId"),
                debug_source=str(stable.path),
                debug_source_size=stable.size,
                debug_source_mtime_ns=stable.mtime_ns,
                terminal_log_sync=terminal_log_sync,
                **projection,
            )
        campaign_run_id = raw_campaign_run_id
        state = self._load_state()
        background_terminal_log_sync = self._refresh_pending_terminal_runs(
            state,
            now_epoch,
            exclude_run_id=campaign_run_id,
        )
        run = state["runs"].get(campaign_run_id)
        if not isinstance(run, dict):
            run = self._new_run(state, campaign_run_id, stable)
            state["runs"][campaign_run_id] = run
        run["debug_source"] = str(stable.path)
        run["debug_source_mtime_ns"] = stable.mtime_ns
        appended = self._append_trace(run, document, revision)
        run["last_revision"] = max(int(run.get("last_revision", -1)), revision)
        if self._is_terminal(document):
            self._arm_terminal_log_sync(run, now_epoch)
        run["updated_at"] = _utc_now()
        bundle = Path(run["bundle"])
        _atomic_json(bundle / "latest_snapshot.json", document)
        if (
            run.get("terminal") is not True
            or run.get("terminal_log_sync_status") == "pending"
        ):
            log_results = self._sync_logs(run)
            if run.get("terminal") is True:
                self._record_terminal_log_sync(run, now_epoch)
        else:
            prior_log_capture = run.get("last_log_capture")
            log_results = (
                prior_log_capture if isinstance(prior_log_capture, dict) else {}
            )
        self._write_manifest(run, document=document, log_results=log_results)
        recovery_composite = self._write_recovery_composite(
            state, run, document
        )
        state["updated_at"] = _utc_now()
        _atomic_json(self.state_path, state)
        status = "terminal_bundle_ready" if run["terminal"] else "capturing"
        return self._write_status(
            status,
            "Timer live diagnostics were captured without affecting the main importer.",
            campaign_run_id=campaign_run_id,
            revision=revision,
            appended_trace_entries=appended,
            trace_entries=run["trace_entries"],
            last_debug_sequence=run["last_debug_sequence"],
            terminal=run["terminal"],
            bundle=str(bundle),
            manifest=str(bundle / "manifest.json"),
            log_capture=log_results,
            terminal_log_sync=self._terminal_log_sync_info(run),
            background_terminal_log_sync=background_terminal_log_sync,
            recovery_composite=recovery_composite,
            diagnostic_session_id=document.get("diagnosticSessionId"),
            **projection,
        )

    def finalize(
        self,
        campaign_run_id: str,
        terminal_record: dict[str, Any] | None = None,
        *,
        complete_log_sync: bool = False,
    ) -> dict[str, Any]:
        state = self._load_state()
        run = state["runs"].get(campaign_run_id)
        if not isinstance(run, dict):
            return self._write_status(
                "bundle_unavailable",
                "No live debug export was observed for this terminal campaign.",
                campaign_run_id=campaign_run_id,
            )
        now_epoch = _utc_epoch()
        self._arm_terminal_log_sync(run, now_epoch)
        run["updated_at"] = _utc_now()
        bundle = Path(run["bundle"])
        snapshot_path = bundle / "latest_snapshot.json"
        document = self._load_snapshot_document(run)

        terminal_event = "CALIBRATION_STAGE_D_RECOVERY_COMPLETED"
        has_terminal_debug = (
            isinstance(document, dict)
            and document.get("campaignMode") == "stage_d_recovery"
            and document.get("terminalEvent") == terminal_event
            and self._is_terminal(document)
        )
        if not has_terminal_debug and isinstance(terminal_record, dict):
            marker = terminal_record.get("marker")
            if (
                terminal_record.get("event") == terminal_event
                and isinstance(marker, dict)
                and marker.get("campaignRunId") == campaign_run_id
            ):
                recovered = dict(document) if isinstance(document, dict) else {}
                recovered.update(
                    {
                        "campaignRunId": campaign_run_id,
                        "campaignMode": marker.get("campaignMode")
                        or "stage_d_recovery",
                        "sourceCampaignRunId": marker.get("sourceCampaignRunId"),
                        "status": marker.get("status")
                        or "awaiting_export_reload",
                        "terminal": True,
                        "terminalEvent": terminal_event,
                    }
                )
                snapshot = recovered.get("snapshot")
                snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
                snapshot["lastMarker"] = {
                    "event": terminal_event,
                    "details": marker,
                    "sequence": terminal_record.get("sequence"),
                    "time": terminal_record.get("time"),
                }
                recovered["snapshot"] = snapshot
                document = recovered
                _atomic_json(snapshot_path, recovered)

        log_results = self._sync_logs(run)
        self._record_terminal_log_sync(
            run,
            now_epoch,
            force_complete=complete_log_sync,
        )
        self._write_manifest(run, document=document, log_results=log_results)
        recovery_composite = (
            self._write_recovery_composite(state, run, document)
            if document is not None
            else None
        )
        state["updated_at"] = _utc_now()
        _atomic_json(self.state_path, state)
        return self._write_status(
            "terminal_bundle_ready",
            "Terminal Timer diagnostics and matching WoW log increments are saved.",
            campaign_run_id=campaign_run_id,
            terminal=True,
            bundle=str(bundle),
            manifest=str(bundle / "manifest.json"),
            trace_entries=run.get("trace_entries", 0),
            last_debug_sequence=run.get("last_debug_sequence", 0),
            log_capture=log_results,
            terminal_log_sync=self._terminal_log_sync_info(run),
            recovery_composite=recovery_composite,
        )

    def refresh_terminal_logs(self, campaign_run_id: str) -> dict[str, Any]:
        """Capture flushed terminal log bytes once, then close the grace window."""

        return self.finalize(campaign_run_id, complete_log_sync=True)

    def finalize_many(self, campaign_run_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        return {run_id: self.finalize(run_id) for run_id in campaign_run_ids}
