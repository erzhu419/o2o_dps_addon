"""Prepare an isolated current teammate-response dispatch; smoke one worker first.

Run this file on the shared remote filesystem after staging the current source
closure.  ``prepare`` and ``smoke`` never submit the remaining 67+ workers.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import re
import shlex
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc
from o2o_dps import chronicle_external_teammate_response_model_v1 as response


DEFAULT_OLD_DISPATCH = (
    "runs/teammate-response-v4/"
    "67e50722af2ced5416335f92189540257d3dad0848e96605eeeb710c50c4a15a/"
    "attempt1/dispatch/dispatch.json"
)
DEFAULT_SHARED_ROOT = Path("/home/zhengliang01/scheduleurm_work/o2o-dps-hpc")
D900_INSTANCE = "d900a97b-b53e-4444-943b-3e0f2be8d477"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(hpc._canonical(value) + b"\n")


def _source_contract(shared_root: Path, closure_sha: str) -> dict[str, Any]:
    locator = f"releases/teammate-response-source/{closure_sha}"
    release = shared_root / locator
    modules = {
        relative: hashlib.sha256((release / relative).read_bytes()).hexdigest()
        for relative in hpc.SOURCE_MODULE_PATHS
    }
    return {
        "schema": hpc.SOURCE_CONTRACT_SCHEMA,
        "source_archive_sha256": closure_sha,
        "release_locator": locator,
        "module_sha256": modules,
    }


def _source_archive() -> tuple[bytes, str]:
    root = Path(__file__).resolve().parents[1]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for relative in hpc.SOURCE_MODULE_PATHS:
            payload = (root / relative).read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    payload = buffer.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def stage(shared_root: Path, old_dispatch: Path) -> dict[str, Any]:
    """Stage only the small source closure, then build a no-work dispatch."""

    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    remote_target = scheduler._ssh_target_for_node(node)
    remote_incoming = shared_root / "incoming/teammate-response-current"
    archive, closure_sha = _source_archive()
    archive_path = remote_incoming / f"source.{closure_sha}.tar"
    remote_script = remote_incoming / f"prepare.{closure_sha}.py"
    remote_batch = remote_incoming / f"node-batch.{closure_sha}.py"
    release = shared_root / f"releases/teammate-response-source/{closure_sha}"
    rc, _out, err = scheduler.run_on(
        node, f"mkdir -p {shlex.quote(str(remote_incoming))} {shlex.quote(str(release))}",
        timeout=60, check=False,
    )
    if rc:
        raise RuntimeError(f"cannot create remote source release: {err}")
    with tempfile.TemporaryDirectory(prefix="teammate-retrain-source-") as temp:
        local_archive = Path(temp) / "source.tar"
        local_archive.write_bytes(archive)
        for local, destination in (
            (local_archive, archive_path), (Path(__file__), remote_script),
            (Path(__file__).with_name("chronicle_external_teammate_response_node_batch_v1.py"), remote_batch),
        ):
            subprocess.run(
                ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
                 "-e", scheduler._ssh_rsync_shell_for_node(node), str(local),
                 f"{remote_target}:{destination}"],
                check=True, timeout=120,
            )
    rc, out, err = scheduler.run_on(
        node, f"sha256sum {shlex.quote(str(archive_path))}",
        timeout=60, check=False,
    )
    if rc or out.split()[0] != closure_sha:
        raise RuntimeError(f"remote source archive differs: {err}")
    command = (
        f"tar -xf {shlex.quote(str(archive_path))} -C {shlex.quote(str(release))} && "
        f"printf '%s\\n' {shlex.quote(closure_sha)} > "
        f"{shlex.quote(str(release / 'source-archive.sha256'))} && "
        f"PYTHONPATH={shlex.quote(str(release))} "
        f"{shlex.quote(str(shared_root.parent / 'conda_envs/scomp-py310/bin/python3.10'))} "
        f"-B {shlex.quote(str(remote_script))} prepare "
        f"--shared-root {shlex.quote(str(shared_root))} "
        f"--old-dispatch {shlex.quote(str(old_dispatch))} "
        f"--closure-sha {shlex.quote(closure_sha)}"
    )
    rc, out, err = scheduler.run_on(node, command, timeout=180, check=False)
    if rc:
        raise RuntimeError(f"remote dispatch preparation failed: {err[-3000:]}")
    return json.loads(out)


def prepare(shared_root: Path, old_dispatch_path: Path, closure_sha: str) -> dict[str, Any]:
    old = _json(old_dispatch_path)
    if old.get("schema") != hpc.DISPATCH_SCHEMA or old.get("revision") == hpc.REVISION:
        raise ValueError("expected an older frozen teammate-response dispatch")
    bindings = old["source_bindings"]
    source = _source_contract(shared_root, closure_sha)
    runtime = {
        "schema": hpc.RUNTIME_CONTRACT_SCHEMA,
        "python_absolute_path": old["execution"]["remote_runtime"]["python_absolute_path"],
        "pythonpath_locator": source["release_locator"],
        "module": hpc.WORKER_MODULE,
        "python_flags": ["-B"],
        "environment": {"GOMAXPROCS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    }
    manifest = shared_root / bindings["stage5"]["manifest_locator"]
    stage5 = _json(manifest)
    old50 = old["split_contract"]["old50_stage5_overlap_instance_ids"]
    overlay = hpc._content_addressed({
        "schema": "chronicle_old50_exact_fury_slot_overlay_manifest/v1",
        "source_bindings": {"stage5_manifest": {
            "content_sha256": stage5["content_address"]["sha256"],
            "file_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }},
        "instance_order": old50,
    })
    split = response.build_component_split_v1(
        stage5, old50_instance_ids=old50, validation_fraction=hpc.VALIDATION_FRACTION
    )
    plan = response.build_remote_training_plan_v1(
        stage5, split, nodes=hpc.NODES,
        root_protocol_reviewed=True,
        exact_dynamic_adapter_materialized_pass=True,
    )
    if hpc._canonical_sha256(split) != bindings["component_split_sha256"]:
        raise ValueError("frozen component split changed")
    run_locator = f"runs/teammate-response-current/{hpc.REVISION}/{closure_sha}/attempt1"
    run_root = shared_root / run_locator
    dispatch = hpc.build_dispatch_v1(
        stage5_manifest_path=manifest,
        old50_overlay=overlay,
        split=split,
        response_plan=plan,
        implementation_source=source,
        remote_runtime=runtime,
        shared_root=shared_root,
        output_root_locator=f"{run_locator}/outputs",
    )
    old_task_nodes = {row["instance_id"]: row["node"] for row in old["tasks"]}
    new_task_nodes = {row["instance_id"]: row["node"] for row in dispatch["tasks"]}
    if (new_task_nodes != old_task_nodes
            or len(new_task_nodes) != len(old["tasks"])):
        raise ValueError("current dispatch changed the frozen task/node cohort")
    for name, value in (
        ("old50_overlay", overlay), ("component_split", split),
        ("response_plan", plan), ("implementation_source", source),
        ("remote_runtime", runtime),
    ):
        _save(run_root / "inputs" / f"{name}.json", value)
    dispatch_path = hpc.save_dispatch_v1(dispatch, run_root / "dispatch")
    smallest = min(
        (row for row in dispatch["tasks"] if row["split"] == "TRAIN"),
        key=lambda row: row["partition"]["compressed_size_bytes"],
    )
    return {
        "status": "PREPARED_NOT_LAUNCHED",
        "revision": hpc.REVISION,
        "source_closure": closure_sha,
        "dispatch": str(dispatch_path),
        "output_root": str(run_root / "outputs"),
        "task_count": len(dispatch["tasks"]),
        "train_task_count": sum(row["split"] == "TRAIN" for row in dispatch["tasks"]),
        "validation_task_count": sum(row["split"] == "VALIDATION" for row in dispatch["tasks"]),
        "smoke_instance_id": smallest["instance_id"],
        "smoke_node": smallest["node"],
        "smoke_compressed_bytes": smallest["partition"]["compressed_size_bytes"],
    }


def smoke(shared_root: Path, dispatch_path: Path, instance_id: str | None = None) -> dict[str, Any]:
    dispatch = _json(dispatch_path)
    if dispatch["revision"] != hpc.REVISION:
        raise ValueError("dispatch revision is not the current worker revision")
    train_tasks = [row for row in dispatch["tasks"] if row["split"] == "TRAIN"]
    if instance_id is None:
        task = min(train_tasks, key=lambda row: row["partition"]["compressed_size_bytes"])
    else:
        task = next((row for row in train_tasks if row["instance_id"] == instance_id), None)
        if task is None:
            raise ValueError("smoke instance is not a TRAIN task in dispatch")
    source = dispatch["source_bindings"]["implementation_source"]
    runtime = dispatch["execution"]["remote_runtime"]
    release = shared_root / source["release_locator"]
    environment = os.environ.copy()
    environment.update(runtime["environment"])
    environment["PYTHONPATH"] = str(release)
    environment["BOC_TEAMMATE_SOURCE_CLOSURE_SHA256"] = source["source_archive_sha256"]
    command = [
        runtime["python_absolute_path"], "-B", "-m", hpc.WORKER_MODULE,
        "worker", "--dispatch", str(dispatch_path), "--shared-root", str(shared_root),
        "--instance-id", task["instance_id"], "--node", task["node"],
    ]
    start = time.perf_counter()
    completed = subprocess.run(
        command, cwd=release, env=environment, text=True, capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - start
    rss_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if completed.returncode != 0:
        return {
            "status": "FAILED", "instance_id": task["instance_id"],
            "wall_seconds": elapsed, "max_rss_kib": rss_kib,
            "stderr_tail": completed.stderr[-3000:],
        }
    worker = json.loads(completed.stdout)
    with gzip.open(worker["worker_output"], "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    receipt = payload["single_scan_receipt"]
    head = payload["joint_dynamic_training_counts"]["base_c"]["tables"]["target_choice_counts"]
    positive = negative = 0
    phase_support: dict[str, dict[str, int]] = {}
    for row in head:
        if row["key"][0] != ["GLOBAL"]:
            continue
        phase = row["key"][1]
        support = phase_support.setdefault(phase, {"positive": 0, "negative": 0})
        for count in row["counts"]:
            if count["value"] is True:
                positive += count["count"]
                support["positive"] += count["count"]
            elif count["value"] is False:
                negative += count["count"]
                support["negative"] += count["count"]
    if not positive or not negative:
        raise RuntimeError("target-choice smoke has no positive/negative GLOBAL support")
    return {
        "status": "SMOKE_COMPLETE_NO_FULL_DISPATCH",
        "worker_status": worker["status"],
        "instance_id": task["instance_id"], "node": task["node"],
        "compressed_bytes": task["partition"]["compressed_size_bytes"],
        "wall_seconds": elapsed, "max_rss_kib": rss_kib,
        "record_count": receipt["record_count"],
        "compiled_exact_player_row_count": receipt["compiled_exact_player_row_count"],
        "target_choice_global_positive_count": positive,
        "target_choice_global_negative_count": negative,
        "target_choice_global_phase_support": phase_support,
        "white6603_first_has_both_classes": all(
            phase_support.get("WHITE6603_FIRST_ACQUISITION", {}).get(kind, 0) > 0
            for kind in ("positive", "negative")
        ),
        "target_choice_context_feature_row_count": len(head),
        "partition_scan_count": worker["partition_scan_count"],
        "worker_output": worker["worker_output"],
    }


def status_remote(shared_root: Path, dispatch_path: Path) -> dict[str, Any]:
    dispatch = _json(dispatch_path)
    outputs = shared_root / dispatch["output_root_locator"]
    rows = []
    for task in dispatch["tasks"]:
        instance_id = task["instance_id"]
        status_path = outputs / "task-status" / f"{instance_id}.json"
        worker_path = outputs / "workers" / f"{instance_id}.json.gz"
        terminal = _json(status_path) if status_path.is_file() else None
        state = (
            "failed" if terminal and terminal["status"] == "FAILED"
            else "done" if worker_path.is_file() else "pending"
        )
        rows.append({
            "instance_id": instance_id, "node": task["node"], "state": state,
            "compressed_bytes": task["partition"]["compressed_size_bytes"],
            "wall_seconds": terminal.get("wall_seconds") if terminal else None,
            "partition_scan_count": terminal.get("partition_scan_count") if terminal else None,
        })
    return {"status": "CURRENT_DISPATCH_TASK_METADATA",
            "dispatch_revision": dispatch["revision"], "tasks": rows}


def membership_remote(shared_root: Path, dispatch_path: Path) -> dict[str, Any]:
    dispatch = _json(dispatch_path)
    stage5 = _json(shared_root / dispatch["source_bindings"]["stage5"]["manifest_locator"])
    old = _json(shared_root / DEFAULT_OLD_DISPATCH)
    focus = next((entry for entry in stage5["instances"]
                  if entry["instance_id"] == D900_INSTANCE), None)
    return {
        "status": "MEMBERSHIP_READ_ONLY",
        "d900_instance_id": D900_INSTANCE,
        "new_task": next(({
            "split": row["split"], "component_id": row["component_id"],
            "node": row["node"],
        } for row in dispatch["tasks"] if row["instance_id"] == D900_INSTANCE), None),
        "old_task": next(({
            "split": row["split"], "component_id": row["component_id"],
        } for row in old["tasks"] if row["instance_id"] == D900_INSTANCE), None),
        "stage5_entry": ({
            "contamination_lane": focus.get("contamination_lane"),
            "component": next((row.get("component_id") for row in
                stage5["split_graph"]["node_to_component"]
                if row.get("node_id") == response._instance_node_id(D900_INSTANCE)), None),
        } if focus else None),
        "validation_component_ids": dispatch["split_contract"]["validation_component_ids"],
        "validation_tasks": [{
            "instance_id": row["instance_id"], "component_id": row["component_id"],
            "node": row["node"], "split": row["split"],
        } for row in dispatch["tasks"] if row["split"] == "VALIDATION"],
    }


def status(shared_root: Path, dispatch_path: Path) -> dict[str, Any]:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    dispatch = _json(dispatch_path) if dispatch_path.is_file() else None
    if dispatch is None:
        # The dispatch is remote-only; the corresponding node004 prepare script
        # reads it and returns only small task metadata.
        closure = dispatch_path.parts[-4]
        remote_script = (
            shared_root / "incoming/teammate-response-current" /
            f"prepare.{closure}.py"
        )
        release = shared_root / f"releases/teammate-response-source/{closure}"
        python = shared_root.parent / "conda_envs/scomp-py310/bin/python3.10"
        command = (
            f"PYTHONPATH={shlex.quote(str(release))} "
            f"{shlex.quote(str(python))} -B {shlex.quote(str(remote_script))} "
            f"status-remote --shared-root {shlex.quote(str(shared_root))} "
            f"--dispatch {shlex.quote(str(dispatch_path))}"
        )
        rc, out, err = scheduler.run_on("node004", command, timeout=60, check=False)
        if rc:
            raise RuntimeError(f"remote status failed: {err[-1500:]}")
        base = json.loads(out)
    else:
        base = status_remote(shared_root, dispatch_path)
    ids = {row["instance_id"] for row in base["tasks"]}

    def active_on(node: str) -> tuple[str, set[str], int, dict[str, dict[str, Any]]]:
        rc, out, err = scheduler.run_on(
            node,
            "pgrep -af o2o_dps.chronicle_external_teammate_response_hpc_v1",
            timeout=60, check=False,
        )
        if rc not in {0, 1}:
            raise RuntimeError(f"cannot inspect {node} processes: {err[-1000:]}")
        active = set()
        pids = []
        pid_to_instance = {}
        for line in out.splitlines():
            if " worker " not in line or str(dispatch_path) not in line:
                continue
            match = re.search(r"--instance-id ([0-9a-f-]{36})", line)
            if match and match.group(1) in ids:
                active.add(match.group(1))
                pid = line.split(None, 1)[0]
                if pid.isdecimal():
                    pids.append(pid)
                    pid_to_instance[pid] = match.group(1)
        max_hwm = 0
        process_details = {}
        if pids:
            command = " ; ".join(
                f"grep VmHWM /proc/{pid}/status" for pid in pids
            )
            _rc, memory, _err = scheduler.run_on(
                node, command, timeout=60, check=False,
            )
            max_hwm = max(
                (int(match.group(1)) for match in re.finditer(
                    r"VmHWM:\s+(\d+) kB", memory
                )), default=0,
            )
            _rc, process_rows, _err = scheduler.run_on(
                node,
                "ps -o pid= -o etimes= -o %cpu= -o rss= -o stat= -p " + ",".join(pids),
                timeout=60, check=False,
            )
            for line in process_rows.splitlines():
                columns = line.split()
                if len(columns) == 5 and columns[0] in pid_to_instance:
                    process_details[pid_to_instance[columns[0]]] = {
                        "elapsed_seconds": int(columns[1]),
                        "cpu_percent": float(columns[2]),
                        "rss_kib": int(columns[3]),
                        "state": columns[4],
                    }
        return node, active, max_hwm, process_details

    with ThreadPoolExecutor(max_workers=6) as pool:
        observations = list(pool.map(active_on, hpc.NODES))
    by_node = {node: rows for node, rows, _hwm, _detail in observations}
    node_hwm = {node: hwm for node, _rows, hwm, _detail in observations}
    process_details = {instance_id: detail for _node, _rows, _hwm, details
                       in observations for instance_id, detail in details.items()}
    active = set().union(*by_node.values())
    counts = Counter()
    completed_bytes = total_bytes = 0
    observed_bytes = observed_wall = 0.0
    scan_walls = []
    for row in base["tasks"]:
        if row["state"] == "pending" and row["instance_id"] in active:
            row["state"] = "running"
        counts[row["state"]] += 1
        total_bytes += row["compressed_bytes"]
        if row["state"] == "done":
            completed_bytes += row["compressed_bytes"]
            if row["partition_scan_count"] == 1 and row["wall_seconds"]:
                observed_bytes += row["compressed_bytes"]
                observed_wall += row["wall_seconds"]
                scan_walls.append(row["wall_seconds"])
    rate = observed_bytes / observed_wall if observed_wall else 0.0
    eta = (
        (total_bytes - completed_bytes) / (rate * max(1, counts["running"]))
        if rate and counts["running"] else None
    )
    unfinished = sorted(({
        "instance_id": row["instance_id"], "node": row["node"],
        "compressed_bytes": row["compressed_bytes"],
        **process_details.get(row["instance_id"], {}),
    } for row in base["tasks"] if row["state"] != "done"),
        key=lambda row: -row["compressed_bytes"])
    return {
        "status": "CURRENT_DISPATCH_PROGRESS",
        "dispatch_revision": base["dispatch_revision"],
        "counts": {key: counts[key] for key in ("done", "running", "failed", "pending")},
        "node_process_counts": {node: len(rows) for node, rows in by_node.items()},
        "node_max_worker_hwm_kib": node_hwm,
        "completed_compressed_bytes": completed_bytes,
        "total_compressed_bytes": total_bytes,
        "eta_seconds_estimate": round(eta) if eta is not None else None,
        "completed_scan_wall_seconds": ({
            "count": len(scan_walls), "min": round(min(scan_walls), 1),
            "median": round(statistics.median(scan_walls), 1),
            "max": round(max(scan_walls), 1),
            "sum": round(sum(scan_walls), 1),
        } if scan_walls else None),
        "unfinished_tasks_by_size": unfinished,
    }


def launch(shared_root: Path, dispatch_path: Path, workers_per_node: int) -> dict[str, Any]:
    """Run the prepared 6-node batch; each existing worker self-resumes exactly."""

    if workers_per_node < 1:
        raise ValueError("workers_per_node must be positive")
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    closure = dispatch_path.parts[-4]
    release = shared_root / f"releases/teammate-response-source/{closure}"
    batch = shared_root / "incoming/teammate-response-current" / f"node-batch.{closure}.py"
    python = shared_root.parent / "conda_envs/scomp-py310/bin/python3.10"

    def run(node: str) -> dict[str, Any]:
        command = (
            f"PYTHONPATH={shlex.quote(str(release))} "
            f"{shlex.quote(str(python))} -B {shlex.quote(str(batch))} "
            f"--dispatch {shlex.quote(str(dispatch_path))} "
            f"--shared-root {shlex.quote(str(shared_root))} "
            f"--node {node} --workers {workers_per_node}"
        )
        rc, out, err = scheduler.run_on(node, command, timeout=7200, check=False)
        if rc:
            return {"node": node, "status": "FAILED", "returncode": rc,
                    "stdout_tail": out[-2000:], "stderr_tail": err[-2000:]}
        result = json.loads(out)
        return {"node": node, "status": result["status"],
                "task_count": result["task_count"],
                "status_counts": result["status_counts"]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(run, hpc.NODES))
    return {"status": "COMPLETE" if all(row["status"] == "COMPLETE" for row in rows)
            else "FAILED", "node_batches": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "prepare", "smoke", "status", "status-remote", "membership-remote", "launch"))
    parser.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED_ROOT)
    parser.add_argument("--old-dispatch", type=Path)
    parser.add_argument("--closure-sha")
    parser.add_argument("--dispatch", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--workers-per-node", type=int, default=8)
    args = parser.parse_args()
    if args.operation == "stage":
        result = stage(
            args.shared_root,
            args.old_dispatch or args.shared_root / DEFAULT_OLD_DISPATCH,
        )
    elif args.operation == "prepare":
        if args.old_dispatch is None or args.closure_sha is None:
            parser.error("prepare requires --old-dispatch and --closure-sha")
        result = prepare(args.shared_root, args.old_dispatch, args.closure_sha)
    elif args.operation == "smoke":
        if args.dispatch is None:
            parser.error("smoke requires --dispatch")
        result = smoke(args.shared_root, args.dispatch, args.instance_id)
    elif args.operation == "status-remote":
        if args.dispatch is None:
            parser.error("status-remote requires --dispatch")
        result = status_remote(args.shared_root, args.dispatch)
    elif args.operation == "membership-remote":
        if args.dispatch is None:
            parser.error("membership-remote requires --dispatch")
        result = membership_remote(args.shared_root, args.dispatch)
    elif args.operation == "status":
        if args.dispatch is None:
            parser.error("status requires --dispatch")
        result = status(args.shared_root, args.dispatch)
    else:
        if args.dispatch is None:
            parser.error("launch requires --dispatch")
        result = launch(args.shared_root, args.dispatch, args.workers_per_node)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
