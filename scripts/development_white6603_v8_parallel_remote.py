"""Run frozen v8 fit projection and selection scoring across node001--node006.

Only small status/metric receipts are written locally; count and Stage-5 data
remain on the shared remote filesystem. Use `--phase fit` before `--phase score`.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
import scheduler

NODES = [f"node{index:03d}" for index in range(1, 7)]
PYTHON = "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10"
SHARED = "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc"
SOURCE = f"{SHARED}/runtime-eval-white-opportunity-v8-pilot-node006"
RUN = f"{SHARED}/runs/teammate-response-current/development_white6603_v8_counts_20260921"
DISPATCH = (
    f"{SHARED}/runs/teammate-response-current/single_scan_causal_target_choice_v6/"
    "7484e06074ef5a7a02a2bf6e06dcbca252777081040f75bfb2142616321c211a/"
    "attempt1/dispatch/dispatch.json"
)
SPLIT = f"{RUN}/frozen_fit_selection.json"
PROJECTION = f"{RUN}/fit-only.score-projection.json"
LOCAL_SPLIT = ROOT / "configs/evaluation/development_white6603_v8_inner_selection.json"
SERIAL_REFERENCE = ROOT / "results/responsive-team-v4/v74-white6603-v8-intra-selection-summary.json"


def _remote(node: str, argv: list[str], *, timeout: int) -> tuple[int, str, str]:
    command = f"cd {shlex.quote(SOURCE)} && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(SOURCE)} " + shlex.join(argv)
    return scheduler.run_on(node, command, timeout=timeout, check=False)


def _stage() -> None:
    for relative in (
        "o2o_dps/development_white6603_selection_eval_v8.py",
        "scripts/development_white6603_merge_fit_v8.py",
        "scripts/development_white6603_v8_selection_remote_v1.py",
    ):
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", scheduler._ssh_rsync_shell_for_node("node004"),
            str(ROOT / relative),
            f"{scheduler._ssh_target_for_node('node004')}:{SOURCE}/{relative}",
        ], check=True, timeout=120)


def _run_map(phase: str, count: int, split: dict) -> list[dict]:
    tasks = sorted(split["fit_instance_ids"]) if phase == "fit" else list(range(count))
    buckets = {node: [] for node in NODES}
    for index, task in enumerate(tasks):
        buckets[NODES[index % len(NODES)]].append(task)

    def completed_score_receipt(index: int) -> dict | None:
        path = f"{RUN}/selection/score-part-{index:02d}.json"
        program = (
            "import json,sys; "
            "d=json.load(open(sys.argv[1],encoding='utf-8')); "
            "print(json.dumps({'schema':d['schema'],'shard_index':d['shard_index'],"
            "'shard_count':d['shard_count'],'raid_ids':sorted(d['raids']),"
            "'fit_instance_ids':d['fit_instance_ids'],"
            "'fit_projection_source':d['fit_projection_source'],"
            "'source_dispatch':d['source_dispatch'],"
            "'opportunity_count':sum(r['opportunities'] for r in d['raids'].values())}))"
        )
        code, stdout, _ = _remote("node004", [PYTHON, "-B", "-c", program, path], timeout=60)
        if code:
            return None
        receipt = json.loads(stdout)
        expected = sorted(split["selection_instance_ids"][index::count])
        if (
            receipt["schema"] != "development_white6603_selection_partial/v1"
            or receipt["shard_index"] != index
            or receipt["shard_count"] != count
            or receipt["raid_ids"] != expected
            or receipt["fit_instance_ids"] != split["fit_instance_ids"]
            or receipt["fit_projection_source"] != PROJECTION
            or receipt["source_dispatch"] != DISPATCH
            or receipt["opportunity_count"] <= 0
        ):
            return None
        return receipt

    def one(node: str, task: str | int) -> dict:
        started = time.monotonic()
        if phase == "score":
            existing = completed_score_receipt(task)
            if existing is not None:
                return {
                    "node": node, "task": task, "status": "PASS",
                    "seconds": round(time.monotonic() - started, 2),
                    "reused": True, "receipt": existing, "error": None,
                }
        if phase == "fit":
            argv = [
                PYTHON, "-B", "scripts/development_white6603_merge_fit_v8.py",
                "--run-root", RUN, "--split", SPLIT,
                "--mode", "project-shard", "--instance-id", task,
            ]
        else:
            argv = [
                PYTHON, "-B", "scripts/development_white6603_v8_selection_remote_v1.py",
                "--mode", "shard", "--split", SPLIT, "--dispatch", DISPATCH,
                "--fit-projection", PROJECTION, "--data-root", SHARED,
                "--output", f"{RUN}/selection/score-part-{task:02d}.json",
                "--shard-index", str(task), "--shard-count", str(count),
            ]
        code, stdout, stderr = _remote(node, argv, timeout=3600)
        if code and phase == "score":
            recovered = completed_score_receipt(task)
            if recovered is not None:
                return {
                    "node": node, "task": task, "status": "PASS",
                    "seconds": round(time.monotonic() - started, 2),
                    "recovered_after_transport_error": True,
                    "receipt": recovered, "error": None,
                }
        return {
            "node": node, "task": task,
            "status": "PASS" if code == 0 else "FAILED",
            "seconds": round(time.monotonic() - started, 2),
            "receipt": json.loads(stdout) if code == 0 else None,
            "error": stderr[-1200:] if code else None,
        }

    results = []
    def on_node(node: str, items: list[str | int]) -> list[dict]:
        workers = 3 if phase == "score" and count == 13 else 2
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda task: one(node, task), items))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(on_node, node, items): node for node, items in buckets.items()}
        for future in as_completed(futures):
            results.extend(future.result())
    return sorted(results, key=lambda row: str(row["task"]))


def _compare_old_selection() -> dict:
    code, stdout, stderr = scheduler.run_on(
        "node004", f"cat {shlex.quote(RUN + '/selection-parallel-summary.json')}",
        timeout=60, check=False,
    )
    if code:
        raise RuntimeError(stderr[-1200:])
    old = json.loads(SERIAL_REFERENCE.read_text(encoding="utf-8"))
    new = json.loads(stdout)
    exact_keys = (
        "status", "selected_candidate", "opportunity_count", "wave_count",
        "observed_white", "observed_early_white", "nonwhite_opportunity_count",
        "waves_with_observed_white", "selection_instance_ids", "gates",
    )
    exact_equal = all(old[key] == new[key] for key in exact_keys)
    differences = []
    for model, old_values in old["metrics"].items():
        new_values = new["metrics"][model]
        for metric, old_value in old_values.items():
            new_value = new_values[metric]
            if type(old_value) in (float, int):
                if not math.isclose(old_value, new_value, rel_tol=1e-9, abs_tol=1e-7):
                    differences.append(f"{model}.{metric}: {old_value} vs {new_value}")
            elif old_value != new_value:
                differences.append(f"{model}.{metric}: {old_value} vs {new_value}")
    return {
        "status": "PASS" if exact_equal and not differences else "FAIL",
        "exact_gate_alpha_count_and_coverage": exact_equal,
        "metric_difference_count": len(differences),
        "metric_differences": differences[:12],
        "comparison_relative_tolerance": 1e-9,
        "comparison_absolute_tolerance": 1e-7,
    }


def run(phase: str, shard_count: int) -> dict:
    split = json.loads(LOCAL_SPLIT.read_text(encoding="utf-8"))
    if shard_count not in (6, 12, 13):
        raise ValueError("selection shard count must use 6, 12, or 13 distributed workers")
    _stage()
    started = time.monotonic()
    tasks = _run_map(phase, shard_count, split)
    if any(task["status"] != "PASS" for task in tasks):
        return {"phase": phase, "status": "FAILED", "tasks": tasks}
    if phase == "fit":
        argv = [
            PYTHON, "-B", "scripts/development_white6603_merge_fit_v8.py",
            "--run-root", RUN, "--split", SPLIT, "--mode", "merge-projections",
        ]
    else:
        argv = [
            PYTHON, "-B", "scripts/development_white6603_v8_selection_remote_v1.py",
            "--mode", "reduce", "--split", SPLIT, "--dispatch", DISPATCH,
            "--fit-projection", PROJECTION,
            "--output", f"{RUN}/selection-parallel-summary.json",
        ]
        for index in range(shard_count):
            argv.extend(["--part", f"{RUN}/selection/score-part-{index:02d}.json"])
    code, stdout, stderr = _remote("node004", argv, timeout=1800)
    identity = _compare_old_selection() if phase == "score" and code == 0 else None
    return {
        "phase": phase,
        "status": "PASS" if code == 0 and (identity is None or identity["status"] == "PASS") else "FAILED",
        "seconds": round(time.monotonic() - started, 2),
        "node_count": 6, "max_workers_per_node": 3 if phase == "score" and shard_count == 13 else 2,
        "task_count": len(tasks),
        "task_max_seconds": max(task["seconds"] for task in tasks),
        "merge_receipt": json.loads(stdout) if code == 0 else None,
        "serial_identity": identity,
        "error": stderr[-1200:] if code else None,
        "failed_tasks": [task for task in tasks if task["status"] != "PASS"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("fit", "score"), required=True)
    parser.add_argument("--selection-shards", type=int, default=12)
    parser.add_argument("--status-output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.phase, args.selection_shards)
    args.status_output.parent.mkdir(parents=True, exist_ok=True)
    args.status_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
