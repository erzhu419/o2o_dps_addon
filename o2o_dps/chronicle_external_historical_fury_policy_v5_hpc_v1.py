"""Thin six-node execution path for frozen Fury V5 rank calibration.

Each of the 84 map tasks reads exactly one already-published V4 worker pickle
and retains Arm A only.  Reduction consumes only those V5 map outputs, merges
the three independent components, and runs the preregistered V2-versus-V5
paired evaluation.  Stage5 partitions are never opened.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import time
from typing import Any

from . import chronicle_external_historical_fury_policy_v2 as policy_v2
from . import chronicle_external_historical_fury_policy_v4 as policy_v4
from . import chronicle_external_historical_fury_policy_v5 as policy_v5


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_historical_fury_policy_v5_hpc/v1"
REVISION = "rank_calibration_external84_v1"
EXPECTED_V4_PLAN_ID = (
    "7127597d1c57ee95638077a4d881c00e87108a5131a932649983683dfdaee312"
)
EXPECTED_WORKER_COUNT = 84
EXPECTED_LABEL_COUNT = 182481
EXPECTED_COMPONENT_COUNT = 3
DEFAULT_NODES = tuple(f"node{index:03d}" for index in range(1, 7))
WORK_DIRECTORY_NAME = ".hpc-v5-rank-calibration-20260911-r1"

SOURCE_RELATIVE_PATHS = {
    "policy_v2": "o2o_dps/chronicle_external_historical_fury_policy_v2.py",
    "policy_v3": "o2o_dps/chronicle_external_historical_fury_policy_v3.py",
    "policy_v4": "o2o_dps/chronicle_external_historical_fury_policy_v4.py",
    "policy_v5": "o2o_dps/chronicle_external_historical_fury_policy_v5.py",
    "orchestrator": "o2o_dps/chronicle_external_historical_fury_policy_v5_hpc_v1.py",
    "preregistration": "configs/evaluation/chronicle_external_historical_fury_policy_v5_rank_calibration.json",
    "v4_negative_result": "configs/evaluation/chronicle_external_historical_fury_policy_v4_negative_result.json",
}


class HistoricalFuryV5HpcError(RuntimeError):
    """The V5 execution plan, map output, or reduction is inconsistent."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalFuryV5HpcError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise HistoricalFuryV5HpcError(f"JSON root must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _resource_usage(started_wall: float, started_cpu: float) -> JSONMap:
    result: JSONMap = {
        "wall_seconds": time.perf_counter() - started_wall,
        "cpu_seconds": time.process_time() - started_cpu,
        "gomaxprocs": os.environ.get("GOMAXPROCS"),
    }
    try:
        import resource

        result["max_rss_kib"] = int(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        )
    except ImportError:
        result["max_rss_kib"] = None
    return result


def _implementation_hashes(source_root: Path) -> JSONMap:
    result: JSONMap = {}
    for role, relative in SOURCE_RELATIVE_PATHS.items():
        path = source_root / relative
        if not path.is_file():
            raise HistoricalFuryV5HpcError(f"missing V5 source artifact: {path}")
        result[role] = _sha256_file(path)
    return result


def _lpt_assign(
    tasks: Sequence[tuple[int, str, int]], nodes: Sequence[str]
) -> tuple[dict[int, str], dict[str, int]]:
    if tuple(nodes) != DEFAULT_NODES:
        raise HistoricalFuryV5HpcError("V5 is frozen to node001-node006")
    loads = {node: 0 for node in nodes}
    assignments: dict[int, str] = {}
    for position, _instance_id, size in sorted(
        tasks, key=lambda row: (-row[2], row[0])
    ):
        node = min(nodes, key=lambda name: (loads[name], name))
        assignments[position] = node
        loads[node] += size
    return assignments, loads


def create_plan(
    *,
    v4_plan_path: str | Path,
    output_directory: str | Path,
    source_root: str | Path,
    nodes: Sequence[str] = DEFAULT_NODES,
) -> tuple[Path, JSONMap]:
    """Bind the immutable V4 maps and fixed V5 preregistration."""

    v4_plan_path = Path(v4_plan_path).resolve()
    output_directory = Path(output_directory).resolve()
    source_root = Path(source_root).resolve()
    v4_plan = _read_json(v4_plan_path)
    if (
        v4_plan.get("plan_id") != EXPECTED_V4_PLAN_ID
        or v4_plan.get("status") != "PREPARED_NOT_EXECUTED"
        or v4_plan.get("revision") != "paired_abc_low_card_prefix_external84_v1"
    ):
        raise HistoricalFuryV5HpcError("unexpected frozen V4 plan")
    v4_tasks = v4_plan.get("tasks")
    if not isinstance(v4_tasks, list) or len(v4_tasks) != EXPECTED_WORKER_COUNT:
        raise HistoricalFuryV5HpcError("V4 plan does not contain 84 tasks")
    parameters = v4_plan.get("fixed_parameters")
    if not isinstance(parameters, dict) or (
        parameters.get("fold_count") != policy_v5.FIXED_REQUESTED_FOLD_COUNT
        or parameters.get("split_seed") != policy_v5.FIXED_SPLIT_SEED
        or parameters.get("smoothing_alpha")
        != policy_v2.DEFAULT_SMOOTHING_ALPHA
        or parameters.get("backoff_strength")
        != policy_v2.DEFAULT_BACKOFF_STRENGTH
        or parameters.get("hyperparameter_search") is not False
    ):
        raise HistoricalFuryV5HpcError("V4 learner or fold protocol drifted")
    preregistration = policy_v5.load_preregistered_plan(
        source_root / SOURCE_RELATIVE_PATHS["preregistration"]
    )
    negative = policy_v5.load_v4_negative_result(
        source_root / SOURCE_RELATIVE_PATHS["v4_negative_result"]
    )
    if negative.get("plan_id") != v4_plan.get("plan_id"):
        raise HistoricalFuryV5HpcError("V4 negative result belongs to another plan")
    v4_work = Path(str(v4_plan["work_directory"])).resolve()
    discovered: list[tuple[int, str, int]] = []
    task_inputs: dict[int, tuple[Path, Path]] = {}
    for expected_position, task in enumerate(v4_tasks):
        if not isinstance(task, dict):
            raise HistoricalFuryV5HpcError("V4 task must be an object")
        position = task.get("position")
        instance_id = task.get("instance_id")
        if position != expected_position or not isinstance(instance_id, str):
            raise HistoricalFuryV5HpcError("V4 task order or identity drifted")
        aggregate = v4_work / "shards" / f"{position:03d}.aggregate.pkl"
        receipt = v4_work / "shards" / f"{position:03d}.done.json"
        if not aggregate.is_file() or not receipt.is_file():
            raise HistoricalFuryV5HpcError(
                f"V4 worker artifact is incomplete at position {position}"
            )
        size = aggregate.stat().st_size
        discovered.append((position, instance_id, size))
        task_inputs[position] = (aggregate, receipt)
    total_input_bytes = sum(row[2] for row in discovered)
    maximum_input = int(preregistration["budget"]["maximum_input_bytes"])
    if total_input_bytes > maximum_input:
        raise HistoricalFuryV5HpcError("V4 aggregates exceed V5 input budget")
    assignments, lpt_load = _lpt_assign(discovered, nodes)
    work_directory = output_directory / WORK_DIRECTORY_NAME
    tasks: list[JSONMap] = []
    for position, instance_id, size in sorted(discovered):
        aggregate, receipt = task_inputs[position]
        tasks.append(
            {
                "position": position,
                "instance_id": instance_id,
                "node": assignments[position],
                "v4_aggregate": str(aggregate),
                "v4_receipt": str(receipt),
                "v4_aggregate_size_bytes": size,
            }
        )
    body: JSONMap = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": "PREPARED_NOT_EXECUTED",
        "source_root": str(source_root),
        "v4_plan": str(v4_plan_path),
        "v4_plan_id": EXPECTED_V4_PLAN_ID,
        "output_directory": str(output_directory),
        "work_directory": str(work_directory),
        "nodes": list(nodes),
        "lpt_input_bytes_by_node": lpt_load,
        "implementation_sha256": _implementation_hashes(source_root),
        "fixed_protocol": {
            "strict_label_count": EXPECTED_LABEL_COUNT,
            "outer_component_count": EXPECTED_COMPONENT_COUNT,
            "requested_fold_count": policy_v5.FIXED_REQUESTED_FOLD_COUNT,
            "effective_fold_count": policy_v5.FIXED_EFFECTIVE_FOLD_COUNT,
            "split_seed": policy_v5.FIXED_SPLIT_SEED,
            "smoothing_alpha": policy_v2.DEFAULT_SMOOTHING_ALPHA,
            "backoff_strength": policy_v2.DEFAULT_BACKOFF_STRENGTH,
            "beta_lower": policy_v5.DEFAULT_BETA_LOWER,
            "beta_upper": policy_v5.DEFAULT_BETA_UPPER,
            "bisection_iterations": policy_v5.DEFAULT_BISECTION_ITERATIONS,
            "inner_component_fit_count": 6,
            "hyperparameter_search": False,
        },
        "budget": preregistration["budget"],
        "total_v4_aggregate_bytes": total_input_bytes,
        "stage5_partition_bytes_read": 0,
        "tasks": tasks,
        "runner_or_deployment_authorized": False,
    }
    plan_id = _sha256_bytes(_canonical(body))
    plan: JSONMap = {**body, "plan_id": plan_id}
    plan_path = work_directory / "plan.json"
    if plan_path.exists():
        existing = _read_json(plan_path)
        if existing != plan:
            raise HistoricalFuryV5HpcError("existing V5 plan differs")
    else:
        _write_json(plan_path, plan)
    return plan_path, plan


def _load_plan(path: str | Path) -> tuple[Path, JSONMap]:
    plan_path = Path(path).resolve()
    plan = _read_json(plan_path)
    if plan.get("schema") != SCHEMA or plan.get("revision") != REVISION:
        raise HistoricalFuryV5HpcError("unexpected V5 plan schema or revision")
    plan_id = plan.get("plan_id")
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    if plan_id != _sha256_bytes(_canonical(body)):
        raise HistoricalFuryV5HpcError("V5 plan identity mismatch")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != EXPECTED_WORKER_COUNT:
        raise HistoricalFuryV5HpcError("V5 plan task count drifted")
    return plan_path, plan


def _live_code_matches(plan: Mapping[str, Any]) -> None:
    expected = plan.get("implementation_sha256")
    if not isinstance(expected, dict):
        raise HistoricalFuryV5HpcError("implementation binding is missing")
    observed = _implementation_hashes(Path(str(plan["source_root"])))
    if observed != expected:
        raise HistoricalFuryV5HpcError("live V5 source differs from plan")


def _task(plan: Mapping[str, Any], position: int) -> JSONMap:
    if isinstance(position, bool) or not isinstance(position, int):
        raise HistoricalFuryV5HpcError("position must be an integer")
    tasks = plan["tasks"]
    if not 0 <= position < len(tasks):
        raise HistoricalFuryV5HpcError("position is outside the plan")
    task = tasks[position]
    if task.get("position") != position:
        raise HistoricalFuryV5HpcError("task position differs from its index")
    return task


def _map_paths(plan: Mapping[str, Any], position: int) -> tuple[Path, Path]:
    directory = Path(str(plan["work_directory"])) / "maps"
    return (
        directory / f"{position:03d}.arm_a.pkl",
        directory / f"{position:03d}.done.json",
    )


def run_worker(*, plan_path: str | Path, position: int, node: str) -> JSONMap:
    """Read one V4 aggregate and emit only its Arm-A component counts."""

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    if os.environ.get("GOMAXPROCS") != "1":
        raise HistoricalFuryV5HpcError("V5 worker requires GOMAXPROCS=1")
    _path, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    task = _task(plan, position)
    if task.get("node") != node:
        raise HistoricalFuryV5HpcError("worker node differs from LPT assignment")
    output_path, receipt_path = _map_paths(plan, position)
    if output_path.is_file() and receipt_path.is_file():
        receipt = _read_json(receipt_path)
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("position") != position
            or receipt.get("instance_id") != task["instance_id"]
        ):
            raise HistoricalFuryV5HpcError("existing V5 map receipt differs")
        return {**receipt, "status": "RESUMED"}
    v4_receipt = _read_json(Path(str(task["v4_receipt"])))
    if (
        v4_receipt.get("status") != "COMPLETE"
        or v4_receipt.get("plan_id") != plan["v4_plan_id"]
        or v4_receipt.get("position") != position
        or v4_receipt.get("instance_id") != task["instance_id"]
    ):
        raise HistoricalFuryV5HpcError("V4 worker receipt differs from V5 plan")
    aggregate_path = Path(str(task["v4_aggregate"]))
    if aggregate_path.stat().st_size != task["v4_aggregate_size_bytes"]:
        raise HistoricalFuryV5HpcError("V4 worker aggregate size changed")
    try:
        with aggregate_path.open("rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError) as error:
        raise HistoricalFuryV5HpcError(
            f"cannot load V4 worker aggregate {aggregate_path}"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("plan_id") != plan["v4_plan_id"]
        or payload.get("instance_id") != task["instance_id"]
    ):
        raise HistoricalFuryV5HpcError("V4 aggregate identity differs")
    by_arm = payload.get("by_arm")
    if not isinstance(by_arm, dict) or policy_v4.ARM_A not in by_arm:
        raise HistoricalFuryV5HpcError("V4 aggregate has no frozen Arm A")
    components = by_arm[policy_v4.ARM_A]
    if not isinstance(components, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, policy_v4.AdditiveAggregate)
        for key, value in components.items()
    ):
        raise HistoricalFuryV5HpcError("V4 Arm-A component map is invalid")
    map_payload = {
        "plan_id": plan["plan_id"],
        "position": position,
        "instance_id": task["instance_id"],
        "components": components,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            pickle.dump(map_payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt: JSONMap = {
        "schema": SCHEMA + "/map_receipt",
        "status": "COMPLETE",
        "plan_id": plan["plan_id"],
        "position": position,
        "instance_id": task["instance_id"],
        "node": node,
        "v4_aggregate_bytes_read": task["v4_aggregate_size_bytes"],
        "stage5_partition_bytes_read": 0,
        "component_count": len(components),
        "decision_count": sum(value.decision_count for value in components.values()),
        "resource_usage": _resource_usage(started_wall, started_cpu),
    }
    _write_json(receipt_path, receipt)
    return receipt


def status(*, plan_path: str | Path) -> JSONMap:
    _path, plan = _load_plan(plan_path)
    complete_by_node: Counter[str] = Counter()
    complete = 0
    for task in plan["tasks"]:
        output, receipt = _map_paths(plan, int(task["position"]))
        if output.is_file() and receipt.is_file():
            complete += 1
            complete_by_node[str(task["node"])] += 1
    return {
        "status": "COMPLETE" if complete == EXPECTED_WORKER_COUNT else "INCOMPLETE",
        "complete": complete,
        "total": EXPECTED_WORKER_COUNT,
        "complete_by_node": dict(sorted(complete_by_node.items())),
    }


def _merge_components(
    destination: dict[str, policy_v4.AdditiveAggregate],
    source: Mapping[str, policy_v4.AdditiveAggregate],
) -> None:
    for component, aggregate in source.items():
        destination.setdefault(component, policy_v4.AdditiveAggregate()).merge(
            aggregate
        )


def _aggregate_worker_resources(receipts: Sequence[Mapping[str, Any]]) -> JSONMap:
    by_node: dict[str, JSONMap] = {}
    for receipt in receipts:
        node = str(receipt["node"])
        row = by_node.setdefault(
            node,
            {
                "worker_count": 0,
                "input_bytes": 0,
                "decision_count": 0,
                "cpu_seconds_sum": 0.0,
                "wall_seconds_sum": 0.0,
                "wall_seconds_max": 0.0,
                "max_rss_kib": 0,
            },
        )
        usage = receipt["resource_usage"]
        row["worker_count"] += 1
        row["input_bytes"] += int(receipt["v4_aggregate_bytes_read"])
        row["decision_count"] += int(receipt["decision_count"])
        row["cpu_seconds_sum"] += float(usage["cpu_seconds"])
        row["wall_seconds_sum"] += float(usage["wall_seconds"])
        row["wall_seconds_max"] = max(
            float(row["wall_seconds_max"]), float(usage["wall_seconds"])
        )
        if usage.get("max_rss_kib") is not None:
            row["max_rss_kib"] = max(
                int(row["max_rss_kib"]), int(usage["max_rss_kib"])
            )
    return dict(sorted(by_node.items()))


def reduce(*, plan_path: str | Path) -> JSONMap:
    """Merge only V5 Arm-A maps, then run the fixed paired evaluation."""

    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    if os.environ.get("GOMAXPROCS") != "1":
        raise HistoricalFuryV5HpcError("V5 reduce requires GOMAXPROCS=1")
    _path, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    components: dict[str, policy_v4.AdditiveAggregate] = {}
    receipts: list[JSONMap] = []
    for task in plan["tasks"]:
        position = int(task["position"])
        output_path, receipt_path = _map_paths(plan, position)
        if not output_path.is_file() or not receipt_path.is_file():
            raise HistoricalFuryV5HpcError(
                f"V5 map output incomplete at position {position}"
            )
        receipt = _read_json(receipt_path)
        if (
            receipt.get("status") != "COMPLETE"
            or receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("position") != position
            or receipt.get("instance_id") != task["instance_id"]
            or receipt.get("node") != task["node"]
            or receipt.get("stage5_partition_bytes_read") != 0
        ):
            raise HistoricalFuryV5HpcError("V5 map receipt differs from plan")
        try:
            with output_path.open("rb") as handle:
                payload = pickle.load(handle)
        except (OSError, EOFError, pickle.UnpicklingError) as error:
            raise HistoricalFuryV5HpcError(
                f"cannot load V5 map output {output_path}"
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("plan_id") != plan["plan_id"]
            or payload.get("position") != position
            or payload.get("instance_id") != task["instance_id"]
            or not isinstance(payload.get("components"), dict)
        ):
            raise HistoricalFuryV5HpcError("V5 map payload differs from plan")
        _merge_components(components, payload["components"])
        receipts.append(receipt)
    decision_count = sum(value.decision_count for value in components.values())
    if decision_count != EXPECTED_LABEL_COUNT:
        raise HistoricalFuryV5HpcError(
            f"strict label count is {decision_count}, expected {EXPECTED_LABEL_COUNT}"
        )
    if len(components) != EXPECTED_COMPONENT_COUNT:
        raise HistoricalFuryV5HpcError("outer component count is not three")
    evaluation = policy_v5.evaluate_rank_calibration(
        components,
        fold_count=policy_v5.FIXED_REQUESTED_FOLD_COUNT,
        split_seed=policy_v5.FIXED_SPLIT_SEED,
        alpha=policy_v2.DEFAULT_SMOOTHING_ALPHA,
        backoff_strength=policy_v2.DEFAULT_BACKOFF_STRENGTH,
        beta_lower=policy_v5.DEFAULT_BETA_LOWER,
        beta_upper=policy_v5.DEFAULT_BETA_UPPER,
        bisection_iterations=policy_v5.DEFAULT_BISECTION_ITERATIONS,
    )
    reduced: JSONMap = {
        "schema": SCHEMA + "/reduction",
        "revision": REVISION,
        "status": "REDUCED_AWAITING_VALIDATION",
        "plan_id": plan["plan_id"],
        "source": {
            "v4_plan_id": plan["v4_plan_id"],
            "v4_worker_aggregates_read": EXPECTED_WORKER_COUNT,
            "stage5_partitions_read": 0,
        },
        "accounting": {
            "worker_count": len(receipts),
            "strict_controllable_labels": decision_count,
            "outer_component_count": len(components),
            "total_v4_aggregate_bytes_read": sum(
                int(row["v4_aggregate_bytes_read"]) for row in receipts
            ),
        },
        "paired_evaluation": evaluation,
        "resource_usage": {
            "maps_by_node": _aggregate_worker_resources(receipts),
            "reduce": _resource_usage(started_wall, started_cpu),
        },
        "runner_or_deployment_authorized": False,
    }
    output_directory = Path(str(plan["output_directory"]))
    _write_json(output_directory / "reduction.json", reduced)
    _write_json(Path(str(plan["work_directory"])) / "reduce.json", reduced)
    return reduced


def _metric_equal(left: Mapping[str, Any], right: Mapping[str, Any], key: str) -> bool:
    return left.get(key) == right.get(key)


def validate(
    *, plan_path: str | Path, reduction_path: str | Path | None = None
) -> JSONMap:
    _path, plan = _load_plan(plan_path)
    _live_code_matches(plan)
    source = (
        Path(reduction_path)
        if reduction_path is not None
        else Path(str(plan["output_directory"])) / "reduction.json"
    )
    reduction = _read_json(source)
    if (
        reduction.get("schema") != SCHEMA + "/reduction"
        or reduction.get("revision") != REVISION
        or reduction.get("status") != "REDUCED_AWAITING_VALIDATION"
        or reduction.get("plan_id") != plan["plan_id"]
    ):
        raise HistoricalFuryV5HpcError("reduction does not belong to this V5 plan")
    evaluation = reduction["paired_evaluation"]
    baseline = evaluation["baseline_v2"]
    calibrated = evaluation["calibrated_v5"]
    negative = policy_v5.load_v4_negative_result(
        Path(str(plan["source_root"]))
        / SOURCE_RELATIVE_PATHS["v4_negative_result"]
    )
    expected = negative["metrics"]["arm_a_frozen_v2"]
    folds = evaluation["folds"]
    betas = [
        float(fold["temperature_fit"]["inverse_temperature_beta"])
        for fold in folds
    ]
    inner_fits = [
        inner for fold in folds for inner in fold["inner_component_fits"]
    ]
    accounting = reduction["accounting"]
    resources = reduction["resource_usage"]
    map_resources = resources["maps_by_node"]
    worker_cpu = sum(float(row["cpu_seconds_sum"]) for row in map_resources.values())
    reduce_cpu = float(resources["reduce"]["cpu_seconds"])
    maximum_rss_kib = max(
        [int(row["max_rss_kib"]) for row in map_resources.values()]
        + [int(resources["reduce"].get("max_rss_kib") or 0)]
    )
    gates = {
        "all_84_maps_present": accounting.get("worker_count")
        == EXPECTED_WORKER_COUNT,
        "all_six_nodes_used": set(map_resources) == set(DEFAULT_NODES),
        "strict_label_count_reproduced": accounting.get(
            "strict_controllable_labels"
        )
        == EXPECTED_LABEL_COUNT,
        "three_outer_components_reproduced": accounting.get(
            "outer_component_count"
        )
        == EXPECTED_COMPONENT_COUNT,
        "requested_fold_count_5": evaluation.get("effective_fold_count")
        == policy_v5.FIXED_EFFECTIVE_FOLD_COUNT
        and plan["fixed_protocol"]["requested_fold_count"]
        == policy_v5.FIXED_REQUESTED_FOLD_COUNT,
        "same_labels_components_and_folds": evaluation.get(
            "same_labels_components_and_folds"
        )
        is True,
        "outer_test_not_used_for_temperature_fit": evaluation.get(
            "outer_test_used_for_temperature_fit"
        )
        is False,
        "three_outer_fold_betas": len(betas) == 3
        and all(math.isfinite(value) and value > 0 for value in betas),
        "six_inner_component_fits": len(inner_fits) == 6,
        "inner_fits_have_no_outer_test_overlap": all(
            inner.get("outer_test_overlap_count") == 0 for inner in inner_fits
        ),
        "v2_top1_reproduced_exactly": baseline.get("top1_accuracy")
        == expected.get("top1_accuracy"),
        "v2_top3_reproduced_exactly": baseline.get("top3_accuracy")
        == expected.get("top3_accuracy"),
        "v2_logloss_reproduced_exactly": baseline.get("contextual_log_loss")
        == expected.get("contextual_log_loss"),
        "v2_ece_reproduced_exactly": baseline.get("expected_calibration_error")
        == expected.get("expected_calibration_error"),
        "v5_top1_equals_v2_exactly": _metric_equal(
            calibrated, baseline, "top1_accuracy"
        ),
        "v5_top3_equals_v2_exactly": _metric_equal(
            calibrated, baseline, "top3_accuracy"
        ),
        "rank_preserving_runtime_assertions_passed": evaluation.get(
            "rank_preserving_by_construction"
        )
        is True,
        "no_stage5_partitions_read": reduction["source"].get(
            "stage5_partitions_read"
        )
        == 0,
        "input_budget_respected": accounting.get(
            "total_v4_aggregate_bytes_read"
        )
        <= int(plan["budget"]["maximum_input_bytes"]),
        "cpu_budget_respected": worker_cpu + reduce_cpu
        <= float(plan["budget"]["maximum_cpu_seconds"]),
        "rss_budget_respected": maximum_rss_kib
        <= int(plan["budget"]["maximum_peak_rss_mib"]) * 1024,
        "no_hyperparameter_search": plan["fixed_protocol"][
            "hyperparameter_search"
        ]
        is False,
    }
    integrity_pass = all(gates.values())
    logloss_improved = (
        calibrated["contextual_log_loss"] < baseline["contextual_log_loss"]
    )
    ece_improved = (
        calibrated["expected_calibration_error"]
        < baseline["expected_calibration_error"]
    )
    scientific_pass = (
        integrity_pass
        and logloss_improved
        and ece_improved
        and gates["v5_top1_equals_v2_exactly"]
        and gates["v5_top3_equals_v2_exactly"]
    )
    if not integrity_pass:
        status_value = "INVALID_DIAGNOSTIC"
    elif scientific_pass:
        status_value = "CALIBRATION_METRICS_PASS_NO_DEPLOYMENT"
    else:
        status_value = "RETAIN_NEGATIVE_NO_NEXT_HPC"
    validation: JSONMap = {
        "schema": SCHEMA + "/validation",
        "revision": REVISION,
        "status": status_value,
        "plan_id": plan["plan_id"],
        "integrity_pass": integrity_pass,
        "gates": gates,
        "scientific_gate": {
            "top1_exactly_preserved": gates["v5_top1_equals_v2_exactly"],
            "top3_exactly_preserved": gates["v5_top3_equals_v2_exactly"],
            "contextual_log_loss_strictly_improved": logloss_improved,
            "expected_calibration_error_strictly_improved": ece_improved,
            "joint_pass": scientific_pass,
        },
        "metrics": {
            "A_frozen_v2": baseline,
            "D_rank_calibrated_v5": calibrated,
            "D_minus_A": {
                key: float(calibrated[key]) - float(baseline[key])
                for key in (
                    "top1_accuracy",
                    "top3_accuracy",
                    "contextual_log_loss",
                    "expected_calibration_error",
                )
            },
        },
        "outer_fold_inverse_temperature_beta": betas,
        "inner_component_fit_count": len(inner_fits),
        "resources": {
            "total_input_bytes": accounting["total_v4_aggregate_bytes_read"],
            "worker_cpu_seconds_sum": worker_cpu,
            "reduce_cpu_seconds": reduce_cpu,
            "maximum_rss_kib": maximum_rss_kib,
            "maps_by_node": map_resources,
        },
        "v4_negative_result_retained": True,
        "runner_or_deployment_authorized": False,
    }
    output = Path(str(plan["output_directory"]))
    _write_json(output / "validation.json", validation)
    diagnostic = {
        "schema": SCHEMA + "/diagnostic",
        "status": status_value,
        "plan_id": plan["plan_id"],
        "integrity_pass": integrity_pass,
        "scientific_gate": validation["scientific_gate"],
        "metrics": validation["metrics"],
        "outer_fold_inverse_temperature_beta": betas,
        "requested_fold_count": policy_v5.FIXED_REQUESTED_FOLD_COUNT,
        "effective_fold_count": policy_v5.FIXED_EFFECTIVE_FOLD_COUNT,
        "inner_component_fit_count": len(inner_fits),
        "stage5_partitions_read": 0,
        "v4_negative_result_retained": True,
        "runner_or_deployment_authorized": False,
    }
    _write_json(output / "diagnostic.json", diagnostic)
    return validation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--v4-plan", required=True)
    plan_parser.add_argument("--output-directory", required=True)
    plan_parser.add_argument("--source-root", required=True)
    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--plan", required=True)
    worker_parser.add_argument("--position", type=int, required=True)
    worker_parser.add_argument("--node", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--plan", required=True)
    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--plan", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--plan", required=True)
    validate_parser.add_argument("--reduction")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        path, result = create_plan(
            v4_plan_path=arguments.v4_plan,
            output_directory=arguments.output_directory,
            source_root=arguments.source_root,
        )
        value: Any = {"plan_path": str(path), "plan": result}
    elif arguments.command == "worker":
        value = run_worker(
            plan_path=arguments.plan,
            position=arguments.position,
            node=arguments.node,
        )
    elif arguments.command == "status":
        value = status(plan_path=arguments.plan)
    elif arguments.command == "reduce":
        value = reduce(plan_path=arguments.plan)
    else:
        value = validate(
            plan_path=arguments.plan,
            reduction_path=arguments.reduction,
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
