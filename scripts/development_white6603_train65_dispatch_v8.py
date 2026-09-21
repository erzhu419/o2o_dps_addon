"""Run the frozen 52-fit/13-selection v8 count pass on assigned HPC nodes.

Run from WSL with scheduleurm/skill on PYTHONPATH. The only local output is a
small per-shard status JSON; sufficient counts stay under --run-root remotely.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shlex
from threading import Lock
import time

import scheduler


PYTHON = "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10"


def _remote(node: str, command: str, timeout: int = 60) -> tuple[int, str, str]:
    return scheduler.run_on(node, command, timeout=timeout, check=False)


def _task_command(args: argparse.Namespace, task: dict, lane: str) -> str:
    instance_id = task["instance_id"]
    destination = f"{args.run_root}/{lane}/{instance_id}"
    paths = {
        "dispatch": args.dispatch,
        "data_root": args.shared_root,
        "formal_worker": f"{args.formal_run}/outputs/workers/{instance_id}.json.gz",
        "instance_id": instance_id,
        "output": destination + ".summary.json",
        "training_output": destination + ".joint-white.json.gz",
    }
    options = " ".join(
        f"--{key.replace('_', '-')} {shlex.quote(value)}"
        for key, value in paths.items()
    )
    source = shlex.quote(args.source_root)
    return (
        f"cd {source} && PYTHONPATH={source} {PYTHON} -B "
        f"scripts/development_white6603_opportunity_train_shard_pilot_v8.py {options}"
    )


def _run_task(args: argparse.Namespace, task: dict, lane: str) -> dict:
    node = task["node"]
    instance_id = task["instance_id"]
    destination = f"{args.run_root}/{lane}/{instance_id}"
    summary = destination + ".summary.json"
    training = destination + ".joint-white.json.gz"
    start = time.monotonic()
    code, output, error = _remote(
        node,
        f"if test -s {shlex.quote(summary)} && test -s {shlex.quote(training)}; "
        f"then cat {shlex.quote(summary)}; fi",
    )
    existing = None
    if code == 0 and output.strip():
        existing = json.loads(output)
    if existing and existing.get("status") == "PASS":
        receipt = existing
        state = "REUSED_PASS"
    else:
        code, output, error = _remote(
            node, _task_command(args, task, lane), timeout=3600
        )
        receipt = json.loads(output) if code == 0 and output.strip() else None
        state = "PASS" if receipt and receipt.get("status") == "PASS" else "FAILED"
    return {
        "instance_id": instance_id,
        "lane": lane,
        "node": node,
        "state": state,
        "seconds": round(time.monotonic() - start, 2),
        "row_count": receipt.get("compiled_row_count") if receipt else None,
        "white_count": receipt.get("v8_direct_white6603_mark_count") if receipt else None,
        "error": None if state != "FAILED" else error[-1000:],
    }


def run(args: argparse.Namespace) -> dict:
    code, output, error = _remote("node003", "cat " + shlex.quote(args.dispatch))
    if code != 0:
        raise RuntimeError(error)
    dispatch = json.loads(output)
    split = json.loads(args.split.read_text(encoding="utf-8"))
    fit = set(split["fit_instance_ids"])
    selection = set(split["selection_instance_ids"])
    tasks = [task for task in dispatch["tasks"] if task["split"] == "TRAIN"]
    if len(tasks) != 65 or len(fit) != 52 or len(selection) != 13:
        raise ValueError("frozen 65/52/13 task cardinality differs")
    if {task["instance_id"] for task in tasks} != fit | selection or fit & selection:
        raise ValueError("frozen train/selection partition differs from dispatch")
    if any(task["component_id"] != split["train_component_id"] for task in tasks):
        raise ValueError("frozen TRAIN component differs")
    by_node: dict[str, list[tuple[dict, str]]] = {}
    for task in tasks:
        lane = "fit" if task["instance_id"] in fit else "selection"
        by_node.setdefault(task["node"], []).append((task, lane))
    result = {
        "schema": "development_white6603_v8_train65_count_status/v1",
        "run_root": args.run_root,
        "source_root": args.source_root,
        "expected_fit": 52,
        "expected_selection": 13,
        "fit_instance_ids": sorted(fit),
        "selection_instance_ids": sorted(selection),
        "node_slots": {node: (4 if node in {"node001", "node002"} else 8)
                       for node in by_node},
        "shards": [],
    }
    args.status_output.parent.mkdir(parents=True, exist_ok=True)
    lock = Lock()

    def record(shard: dict) -> None:
        with lock:
            result["shards"].append(shard)
            result["shards"].sort(key=lambda item: item["instance_id"])
            args.status_output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                len(result["shards"]), "/65", shard["node"], shard["lane"],
                shard["instance_id"], shard["state"], shard["seconds"],
                flush=True,
            )

    def on_node(node: str, items: list[tuple[dict, str]]) -> None:
        with ThreadPoolExecutor(max_workers=result["node_slots"][node]) as pool:
            futures = {pool.submit(_run_task, args, *item): item for item in items}
            for future in as_completed(futures):
                task, lane = futures[future]
                try:
                    shard = future.result()
                except Exception as exc:
                    shard = {
                        "instance_id": task["instance_id"], "lane": lane,
                        "node": task["node"], "state": "FAILED",
                        "seconds": None, "row_count": None, "white_count": None,
                        "error": str(exc)[-1000:],
                    }
                record(shard)

    with ThreadPoolExecutor(max_workers=len(by_node)) as pool:
        list(pool.map(lambda entry: on_node(*entry), by_node.items()))
    result["status"] = "PASS" if len(result["shards"]) == 65 and all(
        item["state"] != "FAILED" for item in result["shards"]
    ) else "FAILED"
    args.status_output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--formal-run", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--status-output", type=Path, required=True)
    result = run(parser.parse_args())
    print(result["status"], len(result["shards"]), flush=True)


if __name__ == "__main__":
    main()
