"""Compact, mergeable throughput accounting for policy optimization.

The profiler deliberately separates simulator work from serialization,
validation, analysis, and merge work.  It consumes telemetry emitted by the
workers; it does not infer CPU cost from rollout counts or from cluster size.
Only ``COMPLETE_VALID`` rollouts enter the denominator; CPU spent on failed or
incomplete attempts remains charged.  Node and cluster utilization are emitted
only for an explicit shared job window.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "optimization_throughput_profile/v1"
TASK_SCHEMA = "optimization_throughput_task/v1"
BATCH_SCHEMA = "optimization_throughput_batch/v1"
WORKLOAD_SCHEMA = "optimization_search_workload_budget/v1"

_TASK_KINDS = {"ROLLOUT", "SERIALIZATION", "VALIDATION", "ANALYSIS", "MERGE"}
_COMPLETION = {"COMPLETE_VALID", "INCOMPLETE", "FAILED"}
_TRACE_MODES = {"COMPACT", "FULL"}
_CPU_ATTRIBUTION = {
    "EXACT_HOMOGENEOUS_SHARD",
    "UNAVAILABLE_MIXED_SHARD",
    "UNAVAILABLE_IDENTITY_NOT_RECORDED",
    "NOT_APPLICABLE",
}
_BATCH_SCOPES = {"SHARD_PROCESS", "NODE_JOB_WINDOW"}


class OptimizationThroughputProfileV1Error(ValueError):
    """Telemetry cannot support a truthful throughput statement."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OptimizationThroughputProfileV1Error(f"{label} must be non-empty text")
    return value.strip()


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OptimizationThroughputProfileV1Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        qualifier = "positive finite" if positive else "finite and nonnegative"
        raise OptimizationThroughputProfileV1Error(f"{label} must be {qualifier}")
    return result


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OptimizationThroughputProfileV1Error(f"{label} must be an integer")
    if value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "nonnegative"
        raise OptimizationThroughputProfileV1Error(f"{label} must be {qualifier}")
    return value


def _optional_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _string_set(value: Any, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise OptimizationThroughputProfileV1Error(f"{label} must be an array")
    rows = sorted({_text(row, f"{label} entry") for row in value})
    if len(rows) != len(value):
        raise OptimizationThroughputProfileV1Error(
            f"{label} entries must be unique"
        )
    return rows


def _representative_strata(value: Any, label: str) -> list[list[str]]:
    if not isinstance(value, (list, tuple)):
        raise OptimizationThroughputProfileV1Error(f"{label} must be an array")
    rows = [_string_set(row, f"{label} entry") for row in value]
    if any(not row for row in rows):
        raise OptimizationThroughputProfileV1Error(
            f"{label} entries cannot be empty"
        )
    canonical = sorted({tuple(row) for row in rows})
    if len(canonical) != len(rows):
        raise OptimizationThroughputProfileV1Error(
            f"{label} entries must be unique"
        )
    return [list(row) for row in canonical]


def _strict_json(value: Any, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as error:
        raise OptimizationThroughputProfileV1Error(
            f"{label} must be strict JSON: {error}"
        ) from error


def normalize_task_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one independently measured phase/shard telemetry row.

    A row may summarize many rollouts.  Keeping the validity cardinalities in
    the row avoids emitting one telemetry document per simulator invocation,
    which would make profiling materially change the workload being measured.
    """

    if not isinstance(value, Mapping):
        raise OptimizationThroughputProfileV1Error("task must be an object")
    kind = _text(value.get("task_kind"), "task_kind")
    completion = _text(value.get("completion_status"), "completion_status")
    trace = _text(value.get("trace_mode"), "trace_mode")
    if kind not in _TASK_KINDS:
        raise OptimizationThroughputProfileV1Error(f"unsupported task_kind {kind!r}")
    if completion not in _COMPLETION:
        raise OptimizationThroughputProfileV1Error(
            f"unsupported completion_status {completion!r}"
        )
    if trace not in _TRACE_MODES:
        raise OptimizationThroughputProfileV1Error(f"unsupported trace_mode {trace!r}")

    attribution = _text(
        value.get("rollout_cpu_attribution"), "rollout_cpu_attribution"
    )
    if attribution not in _CPU_ATTRIBUTION:
        raise OptimizationThroughputProfileV1Error(
            f"unsupported rollout_cpu_attribution {attribution!r}"
        )
    producer = _optional_text(value.get("producer"), "producer")
    policy_id = _optional_text(value.get("policy_id"), "policy_id")
    workload_strata = _string_set(
        value.get("workload_strata", ()), "workload_strata"
    )

    attempted = _integer(
        value.get("attempted_rollout_count"), "attempted_rollout_count"
    )
    valid = _integer(
        value.get("complete_valid_rollout_count"),
        "complete_valid_rollout_count",
    )
    incomplete = _integer(
        value.get("incomplete_rollout_count"), "incomplete_rollout_count"
    )
    failed = _integer(
        value.get("failed_rollout_count"), "failed_rollout_count"
    )
    if kind == "ROLLOUT":
        if attempted <= 0 or valid + incomplete + failed != attempted:
            raise OptimizationThroughputProfileV1Error(
                "rollout validity counts must partition a positive attempted count"
            )
        expected_status = (
            "COMPLETE_VALID"
            if valid == attempted
            else "FAILED"
            if failed == attempted
            else "INCOMPLETE"
        )
        if completion != expected_status:
            raise OptimizationThroughputProfileV1Error(
                "completion_status differs from rollout validity counts"
            )
    elif any((attempted, valid, incomplete, failed)):
        raise OptimizationThroughputProfileV1Error(
            "non-rollout task must have zero rollout validity counts"
        )
    if kind == "ROLLOUT":
        if attribution == "NOT_APPLICABLE":
            raise OptimizationThroughputProfileV1Error(
                "rollout task requires a rollout CPU attribution status"
            )
        if attribution == "EXACT_HOMOGENEOUS_SHARD" and (
            producer is None or policy_id is None or not workload_strata
        ):
            raise OptimizationThroughputProfileV1Error(
                "exact rollout CPU attribution requires producer, policy_id, and workload_strata"
            )
    elif (
        attribution != "NOT_APPLICABLE"
        or producer is not None
        or policy_id is not None
        or workload_strata
    ):
        raise OptimizationThroughputProfileV1Error(
            "non-rollout task cannot claim rollout identity or CPU attribution"
        )

    row = {
        "schema": TASK_SCHEMA,
        "task_id": _text(value.get("task_id"), "task_id"),
        "batch_id": _text(value.get("batch_id"), "batch_id"),
        "task_kind": kind,
        "workload_class": _text(value.get("workload_class"), "workload_class"),
        "policy_role": _text(value.get("policy_role"), "policy_role"),
        "policy_id": policy_id,
        "producer": producer,
        "workload_strata": workload_strata,
        "rollout_cpu_attribution": attribution,
        "rollout_breakdown": _strict_json(
            value.get("rollout_breakdown", []), "rollout_breakdown"
        ),
        "source_shard_batch_id": _optional_text(
            value.get("source_shard_batch_id"), "source_shard_batch_id"
        ),
        "completion_status": completion,
        "trace_mode": trace,
        "attempted_rollout_count": attempted,
        "complete_valid_rollout_count": valid,
        "incomplete_rollout_count": incomplete,
        "failed_rollout_count": failed,
        "user_cpu_seconds": _number(
            value.get("user_cpu_seconds"), "user_cpu_seconds"
        ),
        "system_cpu_seconds": _number(
            value.get("system_cpu_seconds"), "system_cpu_seconds"
        ),
        "child_user_cpu_seconds": _number(
            value.get("child_user_cpu_seconds"), "child_user_cpu_seconds"
        ),
        "child_system_cpu_seconds": _number(
            value.get("child_system_cpu_seconds"), "child_system_cpu_seconds"
        ),
        "wall_seconds": _number(value.get("wall_seconds"), "wall_seconds", positive=True),
        "peak_rss_bytes": _integer(value.get("peak_rss_bytes"), "peak_rss_bytes"),
        "simulated_combat_seconds": _number(
            value.get("simulated_combat_seconds"), "simulated_combat_seconds"
        ),
        "event_count": _optional_integer(value.get("event_count"), "event_count"),
        "state_interaction_count": _optional_integer(
            value.get("state_interaction_count"), "state_interaction_count"
        ),
        "input_bytes": _integer(value.get("input_bytes"), "input_bytes"),
        "json_bytes": _integer(value.get("json_bytes"), "json_bytes"),
        "compressed_bytes": _integer(
            value.get("compressed_bytes"), "compressed_bytes"
        ),
        "output_bytes": _integer(value.get("output_bytes"), "output_bytes"),
    }
    if kind != "ROLLOUT" and row["simulated_combat_seconds"] != 0:
        raise OptimizationThroughputProfileV1Error(
            "non-rollout task cannot claim simulated combat seconds"
        )
    if kind == "ROLLOUT":
        if valid > 0 and row["simulated_combat_seconds"] <= 0:
            raise OptimizationThroughputProfileV1Error(
                "valid rollouts require positive simulated combat seconds"
            )
        if valid == 0 and row["simulated_combat_seconds"] != 0:
            raise OptimizationThroughputProfileV1Error(
                "invalid-only rollout rows cannot claim valid simulated combat seconds"
            )
    return row


def normalize_batch_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a measured concurrent batch boundary."""

    if not isinstance(value, Mapping):
        raise OptimizationThroughputProfileV1Error("batch must be an object")
    scope = _text(value.get("measurement_scope", "SHARD_PROCESS"), "measurement_scope")
    if scope not in _BATCH_SCOPES:
        raise OptimizationThroughputProfileV1Error(
            f"unsupported measurement_scope {scope!r}"
        )
    row = {
        "schema": BATCH_SCHEMA,
        "batch_id": _text(value.get("batch_id"), "batch_id"),
        "node": _text(value.get("node"), "node"),
        "measurement_scope": scope,
        "wall_seconds": _number(value.get("wall_seconds"), "wall_seconds", positive=True),
        "logical_cpu_capacity": _integer(
            value.get("logical_cpu_capacity"), "logical_cpu_capacity", positive=True
        ),
        "peak_rss_bytes": _integer(value.get("peak_rss_bytes"), "peak_rss_bytes"),
        "job_window_id": _optional_text(value.get("job_window_id"), "job_window_id"),
        "window_started_at_epoch_seconds": (
            _number(
                value.get("window_started_at_epoch_seconds"),
                "window_started_at_epoch_seconds",
            )
            if value.get("window_started_at_epoch_seconds") is not None
            else None
        ),
        "window_ended_at_epoch_seconds": (
            _number(
                value.get("window_ended_at_epoch_seconds"),
                "window_ended_at_epoch_seconds",
            )
            if value.get("window_ended_at_epoch_seconds") is not None
            else None
        ),
        "worker_count": _optional_integer(value.get("worker_count"), "worker_count"),
        "shard_count": _optional_integer(value.get("shard_count"), "shard_count"),
        "representative_workload_strata": _representative_strata(
            value.get("representative_workload_strata", ()),
            "representative_workload_strata",
        ),
    }
    if scope == "NODE_JOB_WINDOW":
        required = (
            row["job_window_id"],
            row["window_started_at_epoch_seconds"],
            row["window_ended_at_epoch_seconds"],
            row["worker_count"],
            row["shard_count"],
        )
        if any(item is None for item in required):
            raise OptimizationThroughputProfileV1Error(
                "node job-window batch requires window, worker, and shard metadata"
            )
        if row["window_ended_at_epoch_seconds"] <= row["window_started_at_epoch_seconds"]:
            raise OptimizationThroughputProfileV1Error(
                "job window end must be after its start"
            )
        if abs(
            row["wall_seconds"]
            - (
                row["window_ended_at_epoch_seconds"]
                - row["window_started_at_epoch_seconds"]
            )
        ) > 1e-6:
            raise OptimizationThroughputProfileV1Error(
                "job-window wall_seconds differs from the explicit shared window"
            )
        if row["worker_count"] <= 0 or row["shard_count"] <= 0:
            raise OptimizationThroughputProfileV1Error(
                "job-window worker_count and shard_count must be positive"
            )
    elif any(
        row[field] is not None
        for field in (
            "job_window_id",
            "window_started_at_epoch_seconds",
            "window_ended_at_epoch_seconds",
            "worker_count",
            "shard_count",
        )
    ) or row["representative_workload_strata"]:
        raise OptimizationThroughputProfileV1Error(
            "shard-process batches cannot claim shared job-window metadata"
        )
    return row


def _cpu_seconds(task: Mapping[str, Any]) -> float:
    return sum(
        float(task[field])
        for field in (
            "user_cpu_seconds",
            "system_cpu_seconds",
            "child_user_cpu_seconds",
            "child_system_cpu_seconds",
        )
    )


def _aggregate_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cpu = sum(_cpu_seconds(row) for row in rows)
    attempted = sum(int(row["attempted_rollout_count"]) for row in rows)
    valid = sum(int(row["complete_valid_rollout_count"]) for row in rows)
    incomplete = sum(int(row["incomplete_rollout_count"]) for row in rows)
    failed = sum(int(row["failed_rollout_count"]) for row in rows)
    simulated = sum(float(row["simulated_combat_seconds"]) for row in rows)
    return {
        "task_count": len(rows),
        "attempted_rollout_count": attempted,
        "complete_valid_rollout_count": valid,
        "incomplete_rollout_count": incomplete,
        "failed_rollout_count": failed,
        "incomplete_task_count": sum(
            row["completion_status"] == "INCOMPLETE" for row in rows
        ),
        "failed_task_count": sum(
            row["completion_status"] == "FAILED" for row in rows
        ),
        "cpu_seconds": cpu,
        "wall_seconds_sum": sum(float(row["wall_seconds"]) for row in rows),
        "peak_rss_bytes_max": max((int(row["peak_rss_bytes"]) for row in rows), default=0),
        "simulated_combat_seconds": simulated,
        "event_count": (
            sum(int(row["event_count"]) for row in rows)
            if all(row["event_count"] is not None for row in rows)
            else None
        ),
        "event_count_observed_task_count": sum(
            row["event_count"] is not None for row in rows
        ),
        "state_interaction_count": (
            sum(int(row["state_interaction_count"]) for row in rows)
            if all(row["state_interaction_count"] is not None for row in rows)
            else None
        ),
        "state_interaction_count_observed_task_count": sum(
            row["state_interaction_count"] is not None for row in rows
        ),
        "input_bytes": sum(int(row["input_bytes"]) for row in rows),
        "json_bytes": sum(int(row["json_bytes"]) for row in rows),
        "compressed_bytes": sum(int(row["compressed_bytes"]) for row in rows),
        "output_bytes": sum(int(row["output_bytes"]) for row in rows),
        "cpu_seconds_per_complete_valid_rollout": (
            cpu / valid if valid else None
        ),
        "simulated_seconds_per_cpu_second": (
            simulated / cpu
            if valid and cpu > 0
            else None
        ),
    }


def build_throughput_profile_v1(
    tasks: Iterable[Mapping[str, Any]],
    batches: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic profile without projecting unmeasured workloads."""

    normalized_tasks = [normalize_task_v1(row) for row in tasks]
    normalized_batches = [normalize_batch_v1(row) for row in batches]
    task_ids = [row["task_id"] for row in normalized_tasks]
    batch_ids = [row["batch_id"] for row in normalized_batches]
    if len(task_ids) != len(set(task_ids)):
        raise OptimizationThroughputProfileV1Error("task_id values must be unique")
    if len(batch_ids) != len(set(batch_ids)):
        raise OptimizationThroughputProfileV1Error("batch_id values must be unique")
    known_batches = set(batch_ids)
    if any(row["batch_id"] not in known_batches for row in normalized_tasks):
        raise OptimizationThroughputProfileV1Error(
            "every task must reference an observed batch"
        )

    by_kind: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_workload: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_batch: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_exact_producer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_exact_role: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_exact_policy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_exact_strata: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in normalized_tasks:
        by_kind[row["task_kind"]].append(row)
        by_workload[row["workload_class"]].append(row)
        by_batch[row["batch_id"]].append(row)
        if (
            row["task_kind"] == "ROLLOUT"
            and row["rollout_cpu_attribution"] == "EXACT_HOMOGENEOUS_SHARD"
        ):
            by_exact_producer[str(row["producer"])].append(row)
            by_exact_role[str(row["policy_role"])].append(row)
            by_exact_policy[str(row["policy_id"])].append(row)
            by_exact_strata["|".join(row["workload_strata"])].append(row)

    batch_rows = []
    for batch in normalized_batches:
        rows = by_batch[batch["batch_id"]]
        cpu = sum(_cpu_seconds(row) for row in rows)
        is_job_window = batch["measurement_scope"] == "NODE_JOB_WINDOW"
        effective_cores = cpu / float(batch["wall_seconds"]) if is_job_window else None
        batch_rows.append(
            {
                **deepcopy(batch),
                "task_count": len(rows),
                "attempted_rollout_count": sum(
                    int(row["attempted_rollout_count"]) for row in rows
                ),
                "complete_valid_rollout_count": sum(
                    int(row["complete_valid_rollout_count"]) for row in rows
                ),
                "task_cpu_seconds": cpu,
                "measured_effective_cores": effective_cores,
                "capacity_utilization": (
                    effective_cores / float(batch["logical_cpu_capacity"])
                    if effective_cores is not None
                    else None
                ),
                "utilization_status": (
                    "MEASURED_SHARED_JOB_WINDOW"
                    if is_job_window
                    else "UNAVAILABLE_SINGLE_SHARD_WINDOW"
                ),
            }
        )

    job_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for batch in batch_rows:
        if batch["measurement_scope"] == "NODE_JOB_WINDOW":
            job_groups[str(batch["job_window_id"])].append(batch)
    job_windows = []
    for job_window_id in sorted(job_groups):
        rows = job_groups[job_window_id]
        starts = {float(row["window_started_at_epoch_seconds"]) for row in rows}
        ends = {float(row["window_ended_at_epoch_seconds"]) for row in rows}
        nodes = [str(row["node"]) for row in rows]
        strata = {
            tuple(tuple(tags) for tags in row["representative_workload_strata"])
            for row in rows
        }
        if len(starts) != 1 or len(ends) != 1 or len(nodes) != len(set(nodes)):
            raise OptimizationThroughputProfileV1Error(
                f"job window {job_window_id!r} is not one shared node window"
            )
        if len(strata) != 1:
            raise OptimizationThroughputProfileV1Error(
                f"job window {job_window_id!r} has inconsistent representative strata"
            )
        wall = next(iter(ends)) - next(iter(starts))
        cpu = sum(float(row["task_cpu_seconds"]) for row in rows)
        capacity = sum(int(row["logical_cpu_capacity"]) for row in rows)
        exact_rollout_cpu = 0.0
        unavailable_rollout_cpu = 0.0
        for batch in rows:
            for task_row in by_batch[str(batch["batch_id"])]:
                if task_row["task_kind"] != "ROLLOUT":
                    continue
                if (
                    task_row["rollout_cpu_attribution"]
                    == "EXACT_HOMOGENEOUS_SHARD"
                ):
                    exact_rollout_cpu += _cpu_seconds(task_row)
                else:
                    unavailable_rollout_cpu += _cpu_seconds(task_row)
        effective_cores = cpu / wall
        job_windows.append(
            {
                "job_window_id": job_window_id,
                "window_started_at_epoch_seconds": next(iter(starts)),
                "window_ended_at_epoch_seconds": next(iter(ends)),
                "wall_seconds": wall,
                "node_count": len(rows),
                "nodes": sorted(nodes),
                "logical_cpu_capacity": capacity,
                "worker_count": sum(int(row["worker_count"]) for row in rows),
                "shard_count": sum(int(row["shard_count"]) for row in rows),
                "task_cpu_seconds": cpu,
                "measured_effective_cores": effective_cores,
                "capacity_utilization": effective_cores / float(capacity),
                "exactly_attributed_rollout_cpu_seconds": exact_rollout_cpu,
                "unavailable_mixed_rollout_cpu_seconds": unavailable_rollout_cpu,
                "representative_workload_strata": [
                    list(tags) for tags in next(iter(strata))
                ],
                "utilization_status": "MEASURED_SHARED_JOB_WINDOW",
            }
        )

    overall = _aggregate_rows(normalized_tasks)
    valid_rollouts = overall["complete_valid_rollout_count"]
    exact_valid_rollouts = sum(
        int(row["complete_valid_rollout_count"])
        for rows in by_exact_producer.values()
        for row in rows
    )
    status = (
        "MEASURED_EXACT_ROLLOUT_COST_AVAILABLE"
        if exact_valid_rollouts > 0
        else "MEASURED_MIXED_ROLLOUT_COST_ONLY"
        if valid_rollouts > 0
        else "NO_COMPLETE_VALID_ROLLOUT_MEASUREMENT"
    )
    return {
        "schema": SCHEMA,
        "status": status,
        "measurement_only": True,
        "projection_is_separate": True,
        "tasks": normalized_tasks,
        "batches": batch_rows,
        "overall": overall,
        "by_task_kind": {
            key: _aggregate_rows(by_kind[key]) for key in sorted(by_kind)
        },
        "by_workload_class": {
            key: _aggregate_rows(by_workload[key]) for key in sorted(by_workload)
        },
        "by_exact_rollout_producer": {
            key: _aggregate_rows(by_exact_producer[key])
            for key in sorted(by_exact_producer)
        },
        "by_exact_rollout_role": {
            key: _aggregate_rows(by_exact_role[key]) for key in sorted(by_exact_role)
        },
        "by_exact_rollout_policy_id": {
            key: _aggregate_rows(by_exact_policy[key])
            for key in sorted(by_exact_policy)
        },
        "by_exact_workload_strata": {
            key: _aggregate_rows(by_exact_strata[key])
            for key in sorted(by_exact_strata)
        },
        "job_windows": job_windows,
        "claim_boundary": {
            "cluster_capacity_inferred_from_task_count": False,
            "single_shard_batch_claimed_as_node_or_cluster_utilization": False,
            "node_or_cluster_utilization_requires_shared_job_window": True,
            "incomplete_rollouts_counted_as_valid": False,
            "twenty_second_scene_extrapolated_to_full_raid": False,
            "cpu_and_wall_time_conflated": False,
            "mixed_lane_cpu_used_for_heterogeneous_lane_projection": False,
            "projection_requires_exact_producer_and_workload_strata": True,
        },
    }


def build_search_workload_budget_v1(
    stages: Iterable[Mapping[str, Any]], *, baseline_count: int
) -> dict[str, Any]:
    """Count candidate work while reusing each identical baseline once per group."""

    baselines = _integer(baseline_count, "baseline_count", positive=True)
    rows = []
    for index, value in enumerate(stages):
        if not isinstance(value, Mapping):
            raise OptimizationThroughputProfileV1Error(
                f"stage {index} must be an object"
            )
        candidates = _integer(value.get("candidate_count"), "candidate_count", positive=True)
        scenarios = _integer(value.get("scenario_count"), "scenario_count", positive=True)
        seeds = _integer(value.get("seed_count"), "seed_count", positive=True)
        groups = scenarios * seeds
        candidate_rollouts = candidates * groups
        baseline_rollouts = baselines * groups
        rows.append(
            {
                "stage_id": _text(value.get("stage_id"), "stage_id"),
                "candidate_count": candidates,
                "scenario_count": scenarios,
                "seed_count": seeds,
                "paired_group_count": groups,
                "candidate_rollout_count": candidate_rollouts,
                "baseline_rollout_count": baseline_rollouts,
                "baseline_reused_across_candidates": True,
                "total_rollout_count": candidate_rollouts + baseline_rollouts,
            }
        )
    return {
        "schema": WORKLOAD_SCHEMA,
        "baseline_count": baselines,
        "stages": rows,
        "candidate_rollout_count": sum(row["candidate_rollout_count"] for row in rows),
        "baseline_rollout_count": sum(row["baseline_rollout_count"] for row in rows),
        "total_rollout_count": sum(row["total_rollout_count"] for row in rows),
        "baseline_cache_key_requirement": (
            "complete request + build + raid context + encounter + execution model + "
            "objective + policy identity + simulator/mechanics version + seed"
        ),
    }


def project_wall_time_v1(
    profile: Mapping[str, Any],
    *,
    rollout_count: int,
    job_window_id: str,
    workload_class: str,
    workload_strata: Sequence[str],
    producer: str,
    policy_role: str,
    policy_id: str | None = None,
    serial_overhead_seconds: float = 0.0,
) -> dict[str, Any]:
    """Project one exactly matched lane/stratum from a shared job window."""

    count = _integer(rollout_count, "rollout_count", positive=True)
    window_id = _text(job_window_id, "job_window_id")
    workload = _text(workload_class, "workload_class")
    target_strata = _string_set(workload_strata, "workload_strata")
    if not target_strata:
        raise OptimizationThroughputProfileV1Error(
            "projection requires a non-empty workload stratum"
        )
    target_producer = _text(producer, "producer")
    target_role = _text(policy_role, "policy_role")
    target_policy = _optional_text(policy_id, "policy_id")
    serial = _number(serial_overhead_seconds, "serial_overhead_seconds")
    raw_windows = profile.get("job_windows") if isinstance(profile, Mapping) else None
    if not isinstance(raw_windows, list):
        raise OptimizationThroughputProfileV1Error(
            "profile has no shared job-window measurements"
        )
    windows = [
        row
        for row in raw_windows
        if isinstance(row, Mapping) and row.get("job_window_id") == window_id
    ]
    if len(windows) != 1 or windows[0].get("utilization_status") != "MEASURED_SHARED_JOB_WINDOW":
        raise OptimizationThroughputProfileV1Error(
            "projection requires one valid shared job window"
        )
    representative = {
        tuple(row)
        for row in windows[0].get("representative_workload_strata", ())
        if isinstance(row, list)
    }
    if tuple(target_strata) not in representative:
        raise OptimizationThroughputProfileV1Error(
            "target workload stratum was not declared representative in this job window"
        )
    batch_ids = {
        row.get("batch_id")
        for row in profile.get("batches", ())
        if isinstance(row, Mapping) and row.get("job_window_id") == window_id
    }
    matching = []
    for row in profile.get("tasks", ()):
        if not isinstance(row, Mapping) or row.get("batch_id") not in batch_ids:
            continue
        if (
            row.get("task_kind") == "ROLLOUT"
            and row.get("rollout_cpu_attribution") == "EXACT_HOMOGENEOUS_SHARD"
            and row.get("workload_class") == workload
            and row.get("workload_strata") == target_strata
            and row.get("producer") == target_producer
            and row.get("policy_role") == target_role
            and (target_policy is None or row.get("policy_id") == target_policy)
        ):
            matching.append(row)
    aggregate = _aggregate_rows(matching)
    cost = aggregate["cpu_seconds_per_complete_valid_rollout"]
    if cost is None or aggregate["cpu_seconds"] <= 0:
        raise OptimizationThroughputProfileV1Error(
            "profile has no complete-valid exactly attributed rollout CPU measurement "
            "for the requested producer and workload stratum"
        )
    cores = aggregate["cpu_seconds"] / float(windows[0]["wall_seconds"])
    if cores <= 0:
        raise OptimizationThroughputProfileV1Error(
            "matching job-window rollout CPU cannot establish effective cores"
        )
    cpu_seconds = _number(cost, "cpu_seconds_per_complete_valid_rollout", positive=True) * count
    return {
        "schema": "optimization_wall_time_projection/v1",
        "rollout_count": count,
        "job_window_id": window_id,
        "workload_class": workload,
        "workload_strata": target_strata,
        "producer": target_producer,
        "policy_role": target_role,
        "policy_id": target_policy,
        "measured_complete_valid_rollout_count": aggregate[
            "complete_valid_rollout_count"
        ],
        "measured_attempted_rollout_count": aggregate["attempted_rollout_count"],
        "measured_cpu_seconds_per_complete_valid_rollout": cost,
        "measured_effective_cores": cores,
        "projected_parallel_seconds": cpu_seconds / cores,
        "serial_overhead_seconds": serial,
        "projected_wall_seconds": cpu_seconds / cores + serial,
        "assumption": (
            "future workload matches this exact producer, role, workload class, "
            "representative stratum, and measured worker allocation"
        ),
        "full_raid_generalization_claimed": False,
        "mixed_lane_cpu_used": False,
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OptimizationThroughputProfileV1Error(
            f"cannot read telemetry input {path}: {error}"
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = _load_json(args.input)
    if not isinstance(document, Mapping):
        raise OptimizationThroughputProfileV1Error("input must be a JSON object")
    profile = build_throughput_profile_v1(
        document.get("tasks", ()), document.get("batches", ())
    )
    output = _strict_json(profile, "throughput profile")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BATCH_SCHEMA",
    "OptimizationThroughputProfileV1Error",
    "SCHEMA",
    "TASK_SCHEMA",
    "WORKLOAD_SCHEMA",
    "build_search_workload_budget_v1",
    "build_throughput_profile_v1",
    "normalize_batch_v1",
    "normalize_task_v1",
    "project_wall_time_v1",
)
