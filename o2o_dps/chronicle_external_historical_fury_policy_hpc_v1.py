"""Six-node map/reduce entry for the fixed External Historical Fury V2 fit.

Each Stage-5 instance is scanned once by one worker.  Workers retain only the
model sufficient statistics and the already accepted decision rows.  ``reduce``
merges the statistics and replays those small decision streams in manifest
order, then delegates artifact publication to the existing V2 compiler.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile
from typing import Any

from . import chronicle_external_historical_fury_policy_v2 as policy_v2


SCHEMA = "chronicle_external_historical_fury_policy_hpc/v1"
REVISION = "external84_one_instance_per_worker_v1"
NODES = tuple(f"node{index:03d}" for index in range(1, 7))


class HistoricalFuryHpcError(RuntimeError):
    pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = policy_v2._canonical_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path_value: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalFuryHpcError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict) or payload != policy_v2._canonical_bytes(value) + b"\n":
        raise HistoricalFuryHpcError(f"not canonical JSON plus LF: {path}")
    return path, value


def _load_plan(path_value: str | Path) -> tuple[Path, dict[str, Any]]:
    path, plan = _load_json(path_value)
    if plan.get("schema") != SCHEMA or plan.get("revision") != REVISION:
        raise HistoricalFuryHpcError("unsupported historical-policy HPC plan")
    return path, plan


def make_plan(
    *,
    team_wave_model_manifest: str | Path,
    output_directory: str | Path,
    work_directory: str | Path,
    fold_count: int = policy_v2.DEFAULT_FOLD_COUNT,
    split_seed: int = policy_v2.DEFAULT_SPLIT_SEED,
    smoothing_alpha: float = policy_v2.DEFAULT_SMOOTHING_ALPHA,
    backoff_strength: float = policy_v2.DEFAULT_BACKOFF_STRENGTH,
) -> Path:
    policy_v2._fold_assignment(
        ("validation-a", "validation-b"),
        fold_count=fold_count,
        split_seed=split_seed,
    )
    policy_v2._fit_aggregate(
        policy_v2._Aggregate(),
        alpha=smoothing_alpha,
        backoff_strength=backoff_strength,
    )
    manifest, manifest_path, source = policy_v2._load_published_input_shallow(
        team_wave_model_manifest
    )
    data_root = policy_v2._data_root(manifest_path)
    output = policy_v2._under(Path(output_directory), data_root, "policy output")
    work = policy_v2._under(Path(work_directory), data_root, "policy HPC work")
    instance_order = policy_v2._array(manifest.get("instance_order"), "instance_order")
    tasks = [
        {
            "position": position,
            "instance_id": policy_v2._text(instance_id, "instance_id"),
            "node": NODES[position % len(NODES)],
        }
        for position, instance_id in enumerate(instance_order)
    ]
    plan = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": "PREPARED_NOT_EXECUTED",
        "policy_implementation_revision": policy_v2.IMPLEMENTATION_REVISION,
        "team_wave_model_manifest": str(manifest_path),
        "team_wave_model_manifest_sha256": source["manifest_content_sha256"],
        "output_directory": str(output),
        "work_directory": str(work),
        "nodes": list(NODES),
        "parameters": {
            "fold_count": fold_count,
            "split_seed": split_seed,
            "smoothing_alpha": smoothing_alpha,
            "backoff_strength": backoff_strength,
        },
        "tasks": tasks,
        "heavy_execution_started": False,
        "comparison_authorized": False,
    }
    plan_path = work / "plan.json"
    _write_json(plan_path, plan)
    return plan_path


def _task_paths(plan: Mapping[str, Any], position: int) -> tuple[Path, Path, Path]:
    root = Path(str(plan["work_directory"])) / "shards"
    stem = f"{position:03d}"
    return (
        root / f"{stem}.aggregate.pkl",
        root / f"{stem}.decisions.jsonl.gz",
        root / f"{stem}.done.json",
    )


def _task(plan: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    rows = [row for row in plan["tasks"] if row.get("instance_id") == instance_id]
    if len(rows) != 1:
        raise HistoricalFuryHpcError(f"instance is not a unique plan task: {instance_id}")
    return rows[0]


def run_worker(*, plan_path: str | Path, instance_id: str) -> dict[str, Any]:
    _, plan = _load_plan(plan_path)
    if plan.get("policy_implementation_revision") != policy_v2.IMPLEMENTATION_REVISION:
        raise HistoricalFuryHpcError("historical-policy implementation changed after planning")
    task = _task(plan, instance_id)
    position = int(task["position"])
    aggregate_path, decisions_path, receipt_path = _task_paths(plan, position)
    if receipt_path.is_file() and aggregate_path.is_file() and decisions_path.is_file():
        return {**_load_json(receipt_path)[1], "status": "RESUMED"}

    manifest, manifest_path, source = policy_v2._load_published_input_shallow(
        plan["team_wave_model_manifest"]
    )
    if source["manifest_content_sha256"] != plan["team_wave_model_manifest_sha256"]:
        raise HistoricalFuryHpcError("Stage-5 manifest changed after planning")

    aggregate_path.parent.mkdir(parents=True, exist_ok=True)
    decision_temp = decisions_path.with_name(f".{decisions_path.name}.{os.getpid()}.tmp")
    aggregate_temp = aggregate_path.with_name(f".{aggregate_path.name}.{os.getpid()}.tmp")
    try:
        with gzip.open(decision_temp, "wb", compresslevel=1) as accepted:
            by_outer, focal_units, _focal_index, accounting = (
                policy_v2._scan_training_evidence(
                    manifest,
                    manifest_path,
                    instance_ids={instance_id},
                    accepted_decision_writer=accepted,
                )
            )
        with aggregate_temp.open("wb") as handle:
            pickle.dump(
                {
                    "instance_id": instance_id,
                    "by_outer": by_outer,
                    "focal_units": focal_units,
                    "accounting": accounting,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        decision_temp.replace(decisions_path)
        aggregate_temp.replace(aggregate_path)
        receipt = {
            "schema": SCHEMA,
            "revision": REVISION,
            "status": "COMPLETE",
            "position": position,
            "instance_id": instance_id,
            "node": task["node"],
            "accepted_action_labels": accounting["action_label_audit"].get(
                "accepted_action_labels", 0
            ),
            "accepted_decision_logical_sha256": accounting[
                "accepted_decision_logical_sha256"
            ],
        }
        _write_json(receipt_path, receipt)
        return receipt
    finally:
        decision_temp.unlink(missing_ok=True)
        aggregate_temp.unlink(missing_ok=True)


def _merge_worker_aggregate(
    by_outer: dict[str, dict[str, policy_v2._Aggregate]],
    focal_units: dict[tuple[str, str], policy_v2._Aggregate],
    worker: Mapping[str, Any],
) -> None:
    for lane in policy_v2.LANES:
        for component, aggregate in worker["by_outer"][lane].items():
            by_outer[lane][component].merge(aggregate)
    for unit, aggregate in worker["focal_units"].items():
        focal_units[unit].merge(aggregate)


def reduce(*, plan_path: str | Path) -> dict[str, Any]:
    _, plan = _load_plan(plan_path)
    if plan.get("policy_implementation_revision") != policy_v2.IMPLEMENTATION_REVISION:
        raise HistoricalFuryHpcError("historical-policy implementation changed after planning")
    manifest, manifest_path, source = policy_v2._load_published_input_shallow(
        plan["team_wave_model_manifest"]
    )
    if source["manifest_content_sha256"] != plan["team_wave_model_manifest_sha256"]:
        raise HistoricalFuryHpcError("Stage-5 manifest changed after planning")

    by_outer = policy_v2._new_lane_map()
    focal_units: dict[tuple[str, str], policy_v2._Aggregate] = defaultdict(
        policy_v2._Aggregate
    )
    audit: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    accepted_digest = hashlib.sha256()
    accepted_count = 0
    for task in plan["tasks"]:
        position = int(task["position"])
        aggregate_path, decisions_path, receipt_path = _task_paths(plan, position)
        if not (aggregate_path.is_file() and decisions_path.is_file() and receipt_path.is_file()):
            raise HistoricalFuryHpcError(
                f"worker output incomplete for {task['instance_id']}"
            )
        receipt = _load_json(receipt_path)[1]
        if receipt.get("instance_id") != task["instance_id"] or receipt.get("status") != "COMPLETE":
            raise HistoricalFuryHpcError(f"worker receipt differs at position {position}")
        with aggregate_path.open("rb") as handle:
            worker = pickle.load(handle)
        if worker.get("instance_id") != task["instance_id"]:
            raise HistoricalFuryHpcError(f"worker aggregate differs at position {position}")
        _merge_worker_aggregate(by_outer, focal_units, worker)
        accounting = worker["accounting"]
        audit.update(accounting["action_label_audit"])
        exclusions.update(accounting["excluded_episode_reasons"])
        instance_digest = hashlib.sha256()
        instance_count = 0
        with gzip.open(decisions_path, "rb") as handle:
            for row in handle:
                accepted_digest.update(row)
                instance_digest.update(row)
                instance_count += 1
        if (
            instance_digest.hexdigest()
            != accounting["accepted_decision_logical_sha256"]
            or instance_count != int(receipt["accepted_action_labels"])
        ):
            raise HistoricalFuryHpcError(
                f"accepted-decision stream differs for {task['instance_id']}"
            )
        accepted_count += instance_count

    entries = policy_v2._array(manifest.get("instances"), "instances")
    training_ids: list[str] = []
    nontraining_ids: list[str] = []
    for raw in entries:
        entry = policy_v2._mapping(raw, "instance")
        destination = (
            training_ids
            if policy_v2._mapping(
                entry.get("contamination_lane"), "contamination_lane"
            ).get("candidate_filter_passed")
            is True
            else nontraining_ids
        )
        destination.append(policy_v2._text(entry.get("instance_id"), "instance_id"))
    if accepted_count != audit.get("accepted_action_labels", 0):
        raise HistoricalFuryHpcError("merged accepted-action count differs")
    _node_index, outer_components = policy_v2._outer_component_index(manifest)
    accounting = {
        "outer_component_count_total": len(outer_components),
        "training_instance_count": len(training_ids),
        "descriptive_nontraining_instance_count": len(nontraining_ids),
        "training_instance_ids_sha256": policy_v2._sha256_json(training_ids),
        "descriptive_nontraining_instance_ids_sha256": policy_v2._sha256_json(
            nontraining_ids
        ),
        "action_label_audit": dict(sorted(audit.items())),
        "excluded_episode_reasons": dict(sorted(exclusions.items())),
        "accepted_decision_logical_sha256": accepted_digest.hexdigest(),
    }
    evidence = (
        by_outer,
        focal_units,
        policy_v2._focal_component_index(focal_units),
        accounting,
    )
    parameters = plan["parameters"]
    result = policy_v2.build_external_historical_fury_policy_v2(
        team_wave_model_manifest_path=plan["team_wave_model_manifest"],
        output_directory=plan["output_directory"],
        fold_count=int(parameters["fold_count"]),
        split_seed=int(parameters["split_seed"]),
        smoothing_alpha=float(parameters["smoothing_alpha"]),
        backoff_strength=float(parameters["backoff_strength"]),
        _training_evidence=evidence,
        _loaded_input=(manifest, manifest_path, source),
    )
    receipt = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": "COMPLETE_NONVOTING",
        "worker_count": len(plan["tasks"]),
        "accepted_action_labels": accepted_count,
        "result": result.as_dict(),
        "comparison_authorized": False,
    }
    _write_json(Path(str(plan["work_directory"])) / "reduce.json", receipt)
    return receipt


def status(*, plan_path: str | Path) -> dict[str, Any]:
    _, plan = _load_plan(plan_path)
    complete = 0
    for task in plan["tasks"]:
        aggregate, decisions, receipt = _task_paths(plan, int(task["position"]))
        complete += int(aggregate.is_file() and decisions.is_file() and receipt.is_file())
    return {
        "status": "COMPLETE" if complete == len(plan["tasks"]) else "INCOMPLETE",
        "complete": complete,
        "total": len(plan["tasks"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--team-wave-model-manifest", type=Path, required=True)
    plan.add_argument("--output-directory", type=Path, required=True)
    plan.add_argument("--work-directory", type=Path, required=True)
    plan.add_argument("--fold-count", type=int, default=policy_v2.DEFAULT_FOLD_COUNT)
    plan.add_argument("--split-seed", type=int, default=policy_v2.DEFAULT_SPLIT_SEED)
    plan.add_argument("--smoothing-alpha", type=float, default=policy_v2.DEFAULT_SMOOTHING_ALPHA)
    plan.add_argument("--backoff-strength", type=float, default=policy_v2.DEFAULT_BACKOFF_STRENGTH)
    worker = commands.add_parser("worker")
    worker.add_argument("--plan", type=Path, required=True)
    worker.add_argument("--instance-id", required=True)
    reduce_parser = commands.add_parser("reduce")
    reduce_parser.add_argument("--plan", type=Path, required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--plan", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        value: Any = {
            "status": "PREPARED_NOT_EXECUTED",
            "plan_path": str(
                make_plan(
                    team_wave_model_manifest=args.team_wave_model_manifest,
                    output_directory=args.output_directory,
                    work_directory=args.work_directory,
                    fold_count=args.fold_count,
                    split_seed=args.split_seed,
                    smoothing_alpha=args.smoothing_alpha,
                    backoff_strength=args.backoff_strength,
                )
            ),
        }
    elif args.command == "worker":
        value = run_worker(plan_path=args.plan, instance_id=args.instance_id)
    elif args.command == "reduce":
        value = reduce(plan_path=args.plan)
    else:
        value = status(plan_path=args.plan)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
