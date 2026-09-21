"""Run one current teammate-response dispatch shard with bounded workers.

The existing teammate-response worker remains the only writer of task outputs.
Consequently, its atomic publication and exact ``RESUMED`` checks are unchanged.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc_v1


SCHEMA = hpc_v1.DISPATCH_SCHEMA
REVISION = hpc_v1.REVISION
WORKER_MODULE = hpc_v1.WORKER_MODULE
NODES = hpc_v1.NODES


class TeammateResponseNodeBatchV1Error(RuntimeError):
    pass


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TeammateResponseNodeBatchV1Error(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TeammateResponseNodeBatchV1Error(f"{label} must be non-empty text")
    return value


def _load_dispatch(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TeammateResponseNodeBatchV1Error(
            f"cannot read dispatch {path}: {error}"
        ) from error
    dispatch = dict(_mapping(value, "dispatch"))
    if (
        dispatch.get("schema") != SCHEMA
        or dispatch.get("revision") != REVISION
        or dispatch.get("status") != "PREPARED_NOT_LAUNCHED"
    ):
        raise TeammateResponseNodeBatchV1Error(
            "dispatch is not the prepared current teammate-response revision"
        )
    return dispatch


def _relative_release(shared_root: Path, locator: Any) -> Path:
    pure = PurePosixPath(_text(locator, "source release locator"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise TeammateResponseNodeBatchV1Error(
            "source release locator must be a relative path"
        )
    return shared_root.joinpath(*pure.parts).resolve()


def _task_rows(dispatch: Mapping[str, Any], node: str) -> list[Mapping[str, Any]]:
    execution = _mapping(dispatch.get("execution"), "dispatch execution")
    nodes = execution.get("nodes")
    if not isinstance(nodes, list) or nodes != list(NODES) or node not in nodes:
        raise TeammateResponseNodeBatchV1Error(
            "dispatch nodes or requested node differ from node001--node006"
        )
    raw_tasks = dispatch.get("tasks")
    if not isinstance(raw_tasks, list):
        raise TeammateResponseNodeBatchV1Error("dispatch tasks must be an array")
    seen: set[str] = set()
    selected: list[Mapping[str, Any]] = []
    for raw in raw_tasks:
        task = _mapping(raw, "dispatch task")
        instance_id = _text(task.get("instance_id"), "task instance_id")
        task_node = _text(task.get("node"), "task node")
        if instance_id in seen or task_node not in NODES:
            raise TeammateResponseNodeBatchV1Error(
                "dispatch tasks contain a duplicate instance or unknown node"
            )
        seen.add(instance_id)
        if task_node == node:
            selected.append(task)
    return sorted(selected, key=lambda task: str(task["instance_id"]))


def _worker_launch(
    dispatch: Mapping[str, Any], shared_root: Path
) -> tuple[list[str], dict[str, str]]:
    execution = _mapping(dispatch.get("execution"), "dispatch execution")
    runtime = _mapping(execution.get("remote_runtime"), "remote runtime")
    bindings = _mapping(dispatch.get("source_bindings"), "source bindings")
    source = _mapping(bindings.get("implementation_source"), "implementation source")
    python = _text(runtime.get("python_absolute_path"), "runtime Python")
    flags = runtime.get("python_flags")
    module = runtime.get("module")
    runtime_environment = runtime.get("environment")
    release_locator = source.get("release_locator")
    if (
        not Path(python).is_absolute()
        or flags != ["-B"]
        or module != WORKER_MODULE
        or not isinstance(runtime_environment, Mapping)
        or runtime.get("pythonpath_locator") != release_locator
    ):
        raise TeammateResponseNodeBatchV1Error(
            "dispatch runtime is not the bound teammate-response worker runtime"
        )
    environment = os.environ.copy()
    for key, value in runtime_environment.items():
        environment[_text(key, "runtime environment name")] = _text(
            value, "runtime environment value"
        )
    environment["PYTHONPATH"] = str(
        _relative_release(shared_root, release_locator)
    )
    environment["BOC_TEAMMATE_SOURCE_CLOSURE_SHA256"] = _text(
        source.get("source_archive_sha256"), "source closure identity"
    )
    return [python, "-B", "-m", WORKER_MODULE, "worker"], environment


def _tail(value: Any, limit: int = 2000) -> str:
    return value[-limit:] if isinstance(value, str) else repr(value)[-limit:]


def _run_one(
    *,
    base_command: Sequence[str],
    environment: Mapping[str, str],
    dispatch_path: Path,
    shared_root: Path,
    task: Mapping[str, Any],
    process_runner: Callable[..., Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    instance_id = str(task["instance_id"])
    node = str(task["node"])
    command = [
        *base_command,
        "--dispatch",
        str(dispatch_path),
        "--shared-root",
        str(shared_root),
        "--instance-id",
        instance_id,
        "--node",
        node,
    ]
    try:
        completed = process_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=dict(environment),
            cwd=environment["PYTHONPATH"],
        )
    except OSError as error:
        return {
            "instance_id": instance_id,
            "status": "FAILED",
            "error": str(error),
            "wall_seconds": time.perf_counter() - started,
        }
    if completed.returncode != 0:
        return {
            "instance_id": instance_id,
            "status": "FAILED",
            "returncode": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
            "wall_seconds": time.perf_counter() - started,
        }
    try:
        receipt = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        return {
            "instance_id": instance_id,
            "status": "FAILED",
            "error": f"worker stdout is not one JSON receipt: {error}",
            "stdout_tail": _tail(completed.stdout),
            "wall_seconds": time.perf_counter() - started,
        }
    if not isinstance(receipt, dict) or receipt.get("status") not in {
        "PUBLISHED",
        "RESUMED",
    }:
        return {
            "instance_id": instance_id,
            "status": "FAILED",
            "error": "worker stdout is not a publish/resume receipt",
            "stdout_tail": _tail(completed.stdout),
            "wall_seconds": time.perf_counter() - started,
        }
    return {"instance_id": instance_id, **receipt, "wall_seconds": time.perf_counter() - started}


def _publish_task_status(directory: Path, row: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{row['instance_id']}.json"
    temporary = directory / f"{row['instance_id']}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def run_node_batch_v1(
    *,
    dispatch_path: str | Path,
    shared_root: str | Path,
    node: str,
    workers: int,
    process_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run exactly the dispatch tasks assigned to ``node``."""

    if workers < 1:
        raise TeammateResponseNodeBatchV1Error("workers must be positive")
    dispatch_file = Path(dispatch_path).expanduser().resolve()
    root = Path(shared_root).expanduser().resolve()
    dispatch = _load_dispatch(dispatch_file)
    tasks = _task_rows(dispatch, node)
    base_command, environment = _worker_launch(dispatch, root)
    status_directory = root / _text(
        dispatch.get("output_root_locator"), "dispatch output root"
    ) / "task-status"
    rows: list[dict[str, Any]] = []
    effective_workers = min(workers, len(tasks))
    if tasks:
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = [
                pool.submit(
                    _run_one,
                    base_command=base_command,
                    environment=environment,
                    dispatch_path=dispatch_file,
                    shared_root=root,
                    task=task,
                    process_runner=process_runner,
                )
                for task in tasks
            ]
            for future in as_completed(futures):
                row = future.result()
                _publish_task_status(status_directory, row)
                rows.append(row)
    rows.sort(key=lambda row: str(row["instance_id"]))
    failures = [row for row in rows if row["status"] == "FAILED"]
    statuses = Counter(str(row["status"]) for row in rows)
    return {
        "schema": "chronicle_external_teammate_response_node_batch/v1",
        "status": "FAILED" if failures else "COMPLETE",
        "node": node,
        "requested_worker_limit": workers,
        "effective_worker_limit": effective_workers,
        "task_count": len(tasks),
        "status_counts": dict(sorted(statuses.items())),
        "tasks": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--node", choices=NODES, required=True)
    parser.add_argument("--workers", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_node_batch_v1(
            dispatch_path=args.dispatch,
            shared_root=args.shared_root,
            node=args.node,
            workers=args.workers,
        )
    except TeammateResponseNodeBatchV1Error as error:
        print(
            json.dumps(
                {"status": "FAILED_TO_START", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
