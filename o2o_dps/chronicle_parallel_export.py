"""Coordinate several independent Chronicle official-UI export workers on Windows.

This module deliberately does not call Chronicle's browser-only event API.  Each
worker opens the official instance page in its own Chrome profile, watches only
that worker's download directory, validates the resulting official CSV with the
existing importer, and commits the result through a short queue-file lock.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator, Sequence
import uuid

from .chronicle_export_assistant import (
    ChronicleAssistantError,
    DEFAULT_DATA_ROOT,
    OFFICIAL_UUID_DOWNLOAD,
    _entry_aliases,
    _file_version,
    _utc_now,
    load_queue,
    matching_downloads,
    official_uuid_downloads,
    queue_path,
    save_queue,
)
from .import_chronicle_csv import ChronicleCSVError, import_chronicle_csv


DEFAULT_CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_UI_DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "chronicle_ui_export.mjs"
PARALLEL_ROOT_RELATIVE = Path("chronicle_raw") / "parallel_export"
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_STALE_SECONDS = 120.0
LOCK_RETRY_SECONDS = 0.05
LOCK_RELEASE_TIMEOUT_SECONDS = 5.0


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_after(seconds: float) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _is_transient_windows_file_error(error: OSError) -> bool:
    return os.name == "nt" and (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) in {5, 32, 33}
    )


@contextmanager
def queue_file_lock(
    data_root: str | Path,
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize queue read-modify-write operations across worker processes."""

    path = queue_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout_seconds
    payload = json.dumps(
        {"pid": os.getpid(), "created_at": _utc_now()}, ensure_ascii=True
    ).encode("ascii")
    while True:
        contention_error: OSError | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            contention_error = error
        except OSError as error:
            if not _is_transient_windows_file_error(error):
                raise
            contention_error = error

        if contention_error is not None:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError as error:
                if not _is_transient_windows_file_error(error):
                    raise
                if time.monotonic() >= deadline:
                    raise ChronicleAssistantError(
                        f"timed out waiting for export queue lock: {lock_path}"
                    ) from error
                time.sleep(LOCK_RETRY_SECONDS)
                continue
            if age >= LOCK_STALE_SECONDS:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    if not _is_transient_windows_file_error(error):
                        raise
                    if time.monotonic() >= deadline:
                        raise ChronicleAssistantError(
                            f"timed out removing stale export queue lock: {lock_path}"
                        ) from error
                    time.sleep(LOCK_RETRY_SECONDS)
                continue
            if time.monotonic() >= deadline:
                raise ChronicleAssistantError(
                    f"timed out waiting for export queue lock: {lock_path}"
                ) from contention_error
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        break

    try:
        yield
    finally:
        release_deadline = time.monotonic() + LOCK_RELEASE_TIMEOUT_SECONDS
        while True:
            try:
                lock_path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if (
                    not _is_transient_windows_file_error(error)
                    or time.monotonic() >= release_deadline
                ):
                    raise
                time.sleep(LOCK_RETRY_SECONDS)


def _clear_claim(entry: dict[str, Any]) -> None:
    for key in (
        "claim_worker",
        "claim_token",
        "claimed_at",
        "claim_expires_at",
    ):
        entry.pop(key, None)


def _recover_expired_claims(queue: dict[str, Any], max_retries: int) -> int:
    now = datetime.now(timezone.utc)
    recovered = 0
    for entry in queue["entries"]:
        if entry.get("status") != "claimed":
            continue
        expires_at = _parse_utc(entry.get("claim_expires_at"))
        if expires_at is not None and expires_at > now:
            continue
        attempts = int(entry.get("attempt_count") or 0)
        entry["last_error"] = "worker lease expired before an import was committed"
        entry["last_failed_at"] = _utc_now()
        entry["status"] = "failed" if attempts >= max_retries else "pending"
        _clear_claim(entry)
        recovered += 1
    return recovered


def claim_next(
    data_root: str | Path,
    *,
    worker_id: str,
    lease_seconds: float,
    max_retries: int,
) -> dict[str, Any] | None:
    """Atomically claim the next pending entry and return a detached copy."""

    resolved_root = Path(data_root).expanduser().resolve()
    with queue_file_lock(resolved_root):
        queue = load_queue(resolved_root)
        changed = _recover_expired_claims(queue, max_retries)
        entry = next(
            (candidate for candidate in queue["entries"] if candidate.get("status") == "pending"),
            None,
        )
        if entry is None:
            if changed:
                save_queue(queue, resolved_root)
            return None
        token = uuid.uuid4().hex
        entry["status"] = "claimed"
        entry["claim_worker"] = worker_id
        entry["claim_token"] = token
        entry["claimed_at"] = _utc_now()
        entry["claim_expires_at"] = _utc_after(lease_seconds)
        entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
        save_queue(queue, resolved_root)
        return json.loads(json.dumps(entry, ensure_ascii=False))


def claim_instance(
    data_root: str | Path,
    *,
    instance_id: str,
    worker_id: str,
    lease_seconds: float,
    max_retries: int,
) -> dict[str, Any]:
    """Atomically claim one known pending entry, for an export already in flight."""

    resolved_root = Path(data_root).expanduser().resolve()
    with queue_file_lock(resolved_root):
        queue = load_queue(resolved_root)
        _recover_expired_claims(queue, max_retries)
        entry = next(
            (
                candidate
                for candidate in queue["entries"]
                if candidate.get("instance_id") == instance_id
            ),
            None,
        )
        if entry is None:
            raise ChronicleAssistantError(f"instance is not in the queue: {instance_id}")
        if entry.get("status") != "pending":
            raise ChronicleAssistantError(
                f"instance cannot be adopted from status {entry.get('status')}: {instance_id}"
            )
        token = uuid.uuid4().hex
        entry["status"] = "claimed"
        entry["claim_worker"] = worker_id
        entry["claim_token"] = token
        entry["claimed_at"] = _utc_now()
        entry["claim_expires_at"] = _utc_after(lease_seconds)
        entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
        save_queue(queue, resolved_root)
        return json.loads(json.dumps(entry, ensure_ascii=False))


def renew_claim(
    data_root: str | Path,
    *,
    instance_id: str,
    worker_id: str,
    claim_token: str,
    lease_seconds: float,
) -> bool:
    resolved_root = Path(data_root).expanduser().resolve()
    with queue_file_lock(resolved_root):
        queue = load_queue(resolved_root)
        entry = next(
            (
                candidate
                for candidate in queue["entries"]
                if candidate.get("instance_id") == instance_id
            ),
            None,
        )
        if entry is None or entry.get("status") != "claimed":
            return False
        if entry.get("claim_worker") != worker_id or entry.get("claim_token") != claim_token:
            return False
        entry["claim_expires_at"] = _utc_after(lease_seconds)
        save_queue(queue, resolved_root)
        return True


def release_claim(
    data_root: str | Path,
    *,
    instance_id: str,
    worker_id: str,
    claim_token: str,
    reason: str,
    max_retries: int,
) -> str:
    resolved_root = Path(data_root).expanduser().resolve()
    with queue_file_lock(resolved_root):
        queue = load_queue(resolved_root)
        entry = next(
            (
                candidate
                for candidate in queue["entries"]
                if candidate.get("instance_id") == instance_id
            ),
            None,
        )
        if entry is None:
            raise ChronicleAssistantError(f"instance is not in the queue: {instance_id}")
        if entry.get("status") != "claimed":
            return str(entry.get("status") or "pending")
        if entry.get("claim_worker") != worker_id or entry.get("claim_token") != claim_token:
            raise ChronicleAssistantError(f"claim no longer belongs to worker {worker_id}")
        attempts = int(entry.get("attempt_count") or 0)
        entry["status"] = "failed" if attempts >= max_retries else "pending"
        entry["last_error"] = reason
        entry["last_failed_at"] = _utc_now()
        _clear_claim(entry)
        save_queue(queue, resolved_root)
        return entry["status"]


def commit_import(
    data_root: str | Path,
    *,
    instance_id: str,
    worker_id: str,
    claim_token: str,
    source: Path,
    receipt: dict[str, Any],
) -> None:
    resolved_root = Path(data_root).expanduser().resolve()
    with queue_file_lock(resolved_root):
        queue = load_queue(resolved_root)
        entry = next(
            (
                candidate
                for candidate in queue["entries"]
                if candidate.get("instance_id") == instance_id
            ),
            None,
        )
        if entry is None:
            raise ChronicleAssistantError(f"instance is not in the queue: {instance_id}")
        if entry.get("status") == "imported":
            return
        if entry.get("status") != "claimed":
            raise ChronicleAssistantError(f"instance claim is no longer active: {instance_id}")
        if entry.get("claim_worker") != worker_id or entry.get("claim_token") != claim_token:
            raise ChronicleAssistantError(f"claim no longer belongs to worker {worker_id}")
        entry["status"] = "imported"
        entry["imported_at"] = _utc_now()
        entry["downloaded_file"] = str(source.resolve())
        filename_match = OFFICIAL_UUID_DOWNLOAD.match(source.name)
        if filename_match is not None:
            entry["download_instance_id"] = filename_match.group("uuid")
        entry["import_receipt"] = receipt
        entry["event_stream_selection"] = (
            "requested_by_parallel_official_ui_workflow; not machine-verifiable_from_csv"
        )
        _clear_claim(entry)
        save_queue(queue, resolved_root)


def reset_failed(data_root: str | Path) -> int:
    resolved_root = Path(data_root).expanduser().resolve()
    with queue_file_lock(resolved_root):
        queue = load_queue(resolved_root)
        reset = 0
        for entry in queue["entries"]:
            if entry.get("status") != "failed":
                continue
            entry["status"] = "pending"
            entry["attempt_count"] = 0
            _clear_claim(entry)
            reset += 1
        if reset:
            save_queue(queue, resolved_root)
        return reset


def _prepare_profile(profile: Path, downloads: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True)
    preferences = default / "Preferences"
    value: dict[str, Any] = {}
    if preferences.is_file():
        try:
            loaded = json.loads(preferences.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = {}
    download = value.setdefault("download", {})
    if not isinstance(download, dict):
        download = {}
        value["download"] = download
    download.update(
        {
            "default_directory": str(downloads.resolve()),
            "directory_upgrade": True,
            "prompt_for_download": False,
        }
    )
    preferences.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _start_official_ui_export(
    *,
    node: str,
    ui_driver: Path,
    chrome: Path,
    profile: Path,
    downloads: Path,
    url: str,
    timeout_minutes: float,
) -> subprocess.Popen[Any]:
    if not ui_driver.is_file():
        raise ChronicleAssistantError(f"Chronicle UI driver does not exist: {ui_driver}")
    return subprocess.Popen(
        [
            node,
            str(ui_driver),
            "--chrome",
            str(chrome),
            "--profile-dir",
            str(profile),
            "--download-dir",
            str(downloads),
            "--url",
            url,
            "--timeout-minutes",
            str(timeout_minutes),
        ],
        close_fds=True,
    )


def _candidates(entry: dict[str, Any], downloads: Path) -> list[Path]:
    candidates = matching_downloads(
        downloads,
        str(entry["instance_id"]),
        _entry_aliases(entry),
    )
    if entry.get("uuid_resolution") == "unresolved_external_api":
        known = set(candidates)
        candidates.extend(path for path in official_uuid_downloads(downloads) if path not in known)
        candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return candidates


def _wait_and_import(
    entry: dict[str, Any],
    *,
    data_root: Path,
    downloads: Path,
    worker_id: str,
    timeout_minutes: float,
    lease_seconds: float,
    poll_seconds: float,
    ui_process: subprocess.Popen[Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    baseline: dict[Path, tuple[int, int]] = {}
    for candidate in _candidates(entry, downloads):
        try:
            baseline[candidate] = _file_version(candidate)
        except OSError:
            pass
    deadline = time.monotonic() + timeout_minutes * 60
    next_renewal = time.monotonic() + min(60.0, lease_seconds / 3)
    failed_versions: dict[Path, tuple[int, int]] = {}
    stable_versions: dict[Path, tuple[tuple[int, int], float]] = {}
    while True:
        now = time.monotonic()
        if ui_process is not None:
            ui_code = ui_process.poll()
            if ui_code not in (None, 0):
                raise ChronicleAssistantError(
                    f"official UI export process exited with code {ui_code}"
                )
        if now >= deadline:
            raise TimeoutError(f"no completed CSV after {timeout_minutes:g} minutes")
        if now >= next_renewal:
            if not renew_claim(
                data_root,
                instance_id=str(entry["instance_id"]),
                worker_id=worker_id,
                claim_token=str(entry["claim_token"]),
                lease_seconds=lease_seconds,
            ):
                raise ChronicleAssistantError("claim was lost while waiting for the CSV")
            next_renewal = now + min(60.0, lease_seconds / 3)
        for candidate in _candidates(entry, downloads):
            try:
                version = _file_version(candidate)
            except OSError:
                continue
            if baseline.get(candidate) == version or failed_versions.get(candidate) == version:
                continue
            previous = stable_versions.get(candidate)
            if previous is None or previous[0] != version:
                stable_versions[candidate] = (version, now)
                continue
            if now - previous[1] < max(1.0, poll_seconds * 2):
                continue
            try:
                result = import_chronicle_csv(
                    candidate,
                    instance=str(entry["instance_id"]),
                    data_root=data_root,
                )
            except ChronicleCSVError:
                failed_versions[candidate] = version
                continue
            return candidate, result.as_dict()
        time.sleep(poll_seconds)


def run_worker(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    chrome = args.chrome.expanduser().resolve()
    if not chrome.is_file():
        raise ChronicleAssistantError(f"Chrome executable does not exist: {chrome}")
    node = shutil.which(str(args.node))
    if node is None:
        raise ChronicleAssistantError(f"Node.js executable was not found: {args.node}")
    ui_driver = args.ui_driver.expanduser().resolve()
    parallel_root = args.parallel_root.expanduser().resolve()
    worker_root = parallel_root / args.worker_id
    profile = worker_root / "chrome-profile"
    downloads = worker_root / "downloads"
    _prepare_profile(profile, downloads)
    completed = 0
    adopted = False
    while args.limit == 0 or completed < args.limit:
        is_adopted = bool(args.adopt_instance and not adopted)
        if is_adopted:
            entry = claim_instance(
                data_root,
                instance_id=args.adopt_instance,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                max_retries=args.max_retries,
            )
            adopted = True
        else:
            entry = claim_next(
                data_root,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                max_retries=args.max_retries,
            )
        if entry is None:
            return 0
        instance_id = str(entry["instance_id"])
        print(f"[{args.worker_id}] claimed {instance_id}", flush=True)
        ui_process: subprocess.Popen[Any] | None = None
        try:
            if is_adopted:
                active_downloads = args.adopt_downloads.expanduser().resolve()
                print(
                    f"[{args.worker_id}] adopting existing official UI export; "
                    f"downloads: {active_downloads}",
                    flush=True,
                )
            else:
                active_downloads = downloads
                ui_process = _start_official_ui_export(
                    node=node,
                    ui_driver=ui_driver,
                    chrome=chrome,
                    profile=profile,
                    downloads=active_downloads,
                    url=str(entry["export_url"]),
                    timeout_minutes=args.timeout_minutes,
                )
            source, receipt = _wait_and_import(
                entry,
                data_root=data_root,
                downloads=active_downloads,
                worker_id=args.worker_id,
                timeout_minutes=args.timeout_minutes,
                lease_seconds=args.lease_seconds,
                poll_seconds=args.poll_seconds,
                ui_process=ui_process,
            )
            if ui_process is not None:
                try:
                    ui_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    ui_process.terminate()
            commit_import(
                data_root,
                instance_id=instance_id,
                worker_id=args.worker_id,
                claim_token=str(entry["claim_token"]),
                source=source,
                receipt=receipt,
            )
            completed += 1
            print(f"[{args.worker_id}] imported {instance_id}: {source.name}", flush=True)
        except (ChronicleAssistantError, OSError, TimeoutError) as error:
            if ui_process is not None and ui_process.poll() is None:
                ui_process.terminate()
            status = release_claim(
                data_root,
                instance_id=instance_id,
                worker_id=args.worker_id,
                claim_token=str(entry["claim_token"]),
                reason=str(error),
                max_retries=args.max_retries,
            )
            print(f"[{args.worker_id}] {instance_id} -> {status}: {error}", flush=True)
    return 0


def _worker_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--parallel-root", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    parser.add_argument("--node", default="node")
    parser.add_argument("--ui-driver", type=Path, default=DEFAULT_UI_DRIVER)
    parser.add_argument("--timeout-minutes", type=float, default=360.0)
    parser.add_argument("--lease-seconds", type=float, default=300.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--adopt-instance")
    parser.add_argument("--adopt-downloads", type=Path)


def run_parallel(args: argparse.Namespace) -> int:
    data_root = args.data_root.expanduser().resolve()
    parallel_root = (
        args.parallel_root.expanduser().resolve()
        if args.parallel_root is not None
        else data_root / PARALLEL_ROOT_RELATIVE
    )
    logs = parallel_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[subprocess.Popen[Any], Any]] = []
    for number in range(args.worker_start, args.worker_start + args.concurrency):
        worker_id = f"worker-{number:02d}"
        log_handle = (logs / f"{worker_id}.log").open("a", encoding="utf-8")
        command = [
            sys.executable,
            "-B",
            "-m",
            "o2o_dps.chronicle_parallel_export",
            "worker",
            "--worker-id",
            worker_id,
            "--data-root",
            str(data_root),
            "--parallel-root",
            str(parallel_root),
            "--chrome",
            str(args.chrome),
            "--node",
            str(args.node),
            "--ui-driver",
            str(args.ui_driver),
            "--timeout-minutes",
            str(args.timeout_minutes),
            "--lease-seconds",
            str(args.lease_seconds),
            "--max-retries",
            str(args.max_retries),
            "--poll-seconds",
            str(args.poll_seconds),
            "--limit",
            str(args.worker_limit),
        ]
        if number == args.worker_start and args.adopt_instance:
            command += ["--adopt-instance", args.adopt_instance]
            command += ["--adopt-downloads", str(args.adopt_downloads)]
        process = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT)
        processes.append((process, log_handle))
        print(f"started {worker_id}; log: {log_handle.name}")
        if number == args.worker_start and args.adopt_instance:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ChronicleAssistantError(
                        f"adopting worker exited with code {process.returncode}"
                    )
                queue = load_queue(data_root)
                adopted = next(
                    (
                        entry
                        for entry in queue["entries"]
                        if entry.get("instance_id") == args.adopt_instance
                    ),
                    None,
                )
                if adopted is not None and adopted.get("claim_worker") == worker_id:
                    break
                time.sleep(0.1)
            else:
                raise ChronicleAssistantError(
                    f"worker did not adopt {args.adopt_instance} within 30 seconds"
                )
    return_code = 0
    try:
        while processes:
            remaining: list[tuple[subprocess.Popen[Any], Any]] = []
            for process, log_handle in processes:
                code = process.poll()
                if code is None:
                    remaining.append((process, log_handle))
                    continue
                log_handle.close()
                if code != 0:
                    return_code = code
            processes = remaining
            if processes:
                time.sleep(1)
    except KeyboardInterrupt:
        for process, _ in processes:
            process.terminate()
        return 130
    finally:
        for _, log_handle in processes:
            log_handle.close()
    return return_code


def print_status(data_root: Path, max_retries: int) -> None:
    with queue_file_lock(data_root):
        queue = load_queue(data_root)
        if _recover_expired_claims(queue, max_retries):
            save_queue(queue, data_root)
        counts: dict[str, int] = {}
        for entry in queue["entries"]:
            status = str(entry.get("status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        print(
            " | ".join(
                [f"total {len(queue['entries'])}"]
                + [f"{name} {counts.get(name, 0)}" for name in ("pending", "claimed", "imported", "failed", "skipped")]
            )
        )
        for entry in queue["entries"]:
            if entry.get("status") == "claimed":
                print(
                    f"{entry.get('claim_worker')}: {entry.get('instance_id')} "
                    f"until {entry.get('claim_expires_at')}"
                )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="start parallel official-UI workers")
    run_parser.add_argument("--concurrency", type=int, default=3)
    run_parser.add_argument("--worker-start", type=int, default=1)
    run_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    run_parser.add_argument("--parallel-root", type=Path)
    run_parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    run_parser.add_argument("--node", default="node")
    run_parser.add_argument("--ui-driver", type=Path, default=DEFAULT_UI_DRIVER)
    run_parser.add_argument("--timeout-minutes", type=float, default=360.0)
    run_parser.add_argument("--lease-seconds", type=float, default=300.0)
    run_parser.add_argument("--max-retries", type=int, default=3)
    run_parser.add_argument("--poll-seconds", type=float, default=1.0)
    run_parser.add_argument("--worker-limit", type=int, default=0)
    run_parser.add_argument(
        "--adopt-instance",
        help="claim an already-running official UI export as worker-01",
    )
    run_parser.add_argument(
        "--adopt-downloads",
        type=Path,
        help="download directory used by the already-running export",
    )
    run_parser.set_defaults(handler=run_parallel)

    worker_parser = subparsers.add_parser("worker", help=argparse.SUPPRESS)
    _worker_arguments(worker_parser)
    worker_parser.set_defaults(handler=run_worker)

    status_parser = subparsers.add_parser("status", help="show claims and queue progress")
    status_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    status_parser.add_argument("--max-retries", type=int, default=3)
    status_parser.set_defaults(handler=lambda args: (print_status(args.data_root, args.max_retries), 0)[1])

    reset_parser = subparsers.add_parser("retry-failed", help="return failed entries to pending")
    reset_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    reset_parser.set_defaults(handler=lambda args: (print(f"reset {reset_failed(args.data_root)} failed entries"), 0)[1])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        for name in ("concurrency", "worker_start", "max_retries"):
            if hasattr(args, name) and getattr(args, name) < 1:
                raise ChronicleAssistantError(f"--{name.replace('_', '-')} must be at least 1")
        for name in ("timeout_minutes", "lease_seconds", "poll_seconds"):
            if hasattr(args, name) and getattr(args, name) <= 0:
                raise ChronicleAssistantError(f"--{name.replace('_', '-')} must be greater than 0")
        if hasattr(args, "worker_limit") and args.worker_limit < 0:
            raise ChronicleAssistantError("--worker-limit must be 0 or greater")
        if hasattr(args, "limit") and args.limit < 0:
            raise ChronicleAssistantError("--limit must be 0 or greater")
        if bool(getattr(args, "adopt_instance", None)) != bool(
            getattr(args, "adopt_downloads", None)
        ):
            raise ChronicleAssistantError(
                "--adopt-instance and --adopt-downloads must be used together"
            )
        return int(args.handler(args))
    except ChronicleAssistantError as error:
        print(f"Chronicle parallel export failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
