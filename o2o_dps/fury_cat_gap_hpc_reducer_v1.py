"""Strict join, equal-instance ranking, and retention for Cat-gap stages."""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
import sys
import tempfile
from typing import Any, Mapping, NoReturn, Sequence

from .cat2new_fury_horizon_analysis_v2 import _paired_t_test_v2
from .fury_cat_gap_hpc_plan_v1 import (
    LOCAL_SMOKE_EXECUTION_KIND_V1,
    REAL_STAGE_EXECUTION_KIND_V1,
    FuryCatGapHpcPlanV1Error,
    validate_dispatch_plan_v1,
    validate_execution_plan_v1,
)
from .fury_cat_gap_hpc_worker_v1 import (
    SHARD_PARTIAL_SCHEMA_V1,
    SHARD_RECEIPT_SCHEMA_V1,
    FuryCatGapHpcWorkerV1Error,
    _compact_partial_row,
    validate_rollout_row_v1,
)
from .fury_multiseed_hpc_worker_v3 import _validate_paired_horizon_elapsed_v3
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    sha256_json,
)


JSONMap = dict[str, Any]
REDUCTION_SCHEMA_V1 = "fury_cat_gap_variable_lane_reduction/v1"
FAMILYWISE_ALPHA_V1 = 0.05
LOCAL_SMOKE_EVIDENCE_SCHEMA_V1 = "fury_cat_gap_real_bridge_smoke_evidence/v1"


class FuryCatGapHpcReducerV1Error(RuntimeError):
    """A missing, duplicate, failed, or ineligible lane forbids retention."""


def _fail(message: str) -> NoReturn:
    raise FuryCatGapHpcReducerV1Error(f"STAGE_FAILED_NO_RETENTION: {message}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"could not read {label}: {error}")
    return dict(_mapping(value, label))


def _producer_validators() -> dict[str, Any]:
    from .cat2new_fury_paired_lane_adapter_v3 import (
        PRODUCER as candidate_producer,
        validate_cat2new_fury_paired_artifact_v3,
    )
    from .cat_fury_paired_lane_adapter_v6 import (
        CAT_V6_PRODUCER,
        validate_cat_runner_v4_artifact_v6,
    )
    from .contra260817_fury_paired_lane_adapter_v4 import (
        CONTRA260817_V4_PRODUCER,
        validate_contra260817_runner_v4_artifact_v4,
    )

    return {
        CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
        CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
        candidate_producer: validate_cat2new_fury_paired_artifact_v3,
    }


def _node_by_shard(dispatch: Mapping[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for node in dispatch["nodes"]:
        for raw_index in node["shard_indices"]:
            index = int(raw_index)
            if index in result:
                _fail("one shard is assigned to multiple nodes")
            result[index] = str(node["name"])
    return result


def _validate_receipt(
    receipt: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    node: str,
    result_path: Path,
) -> None:
    fields = {
        "schema", "status", "execution_plan_sha256", "dispatch_plan_sha256",
        "execution_kind", "stage_id", "node", "shard_index", "task_ids_sha256",
        "result_count", "completion_count", "offline_score_eligible_count",
        "result_file", "partial_file", "compression", "logical_sha256",
        "compressed_sha256", "compressed_size_bytes", "partial_sha256",
        "gomaxprocs", "bridge_process_count",
        "persistent_bridge_for_all_shard_tasks", "heavy_execution_started",
        "simulator_only", "scientific_result_available", "deployment_allowed",
    }
    index = int(shard["shard_index"])
    expected_heavy = plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1
    if (
        set(receipt) != fields
        or receipt.get("schema") != SHARD_RECEIPT_SCHEMA_V1
        or receipt.get("status") != "COMPLETE_SIMULATOR_ONLY_NONVOTING"
        or receipt.get("execution_plan_sha256")
        != plan["content_address"]["sha256"]
        or receipt.get("dispatch_plan_sha256")
        != dispatch["content_address"]["sha256"]
        or receipt.get("execution_kind") != plan["execution_kind"]
        or receipt.get("stage_id") != plan["stage_id"]
        or receipt.get("node") != node
        or receipt.get("shard_index") != index
        or receipt.get("task_ids_sha256") != sha256_json(shard["task_ids"])
        or receipt.get("result_count") != shard["task_count"]
        or receipt.get("result_file") != f"shards/shard-{index:05d}.jsonl.gz"
        or receipt.get("partial_file") != f"partials/shard-{index:05d}.json"
        or receipt.get("compression") != "gzip_mtime_0"
        or receipt.get("compressed_sha256") != _file_sha256(result_path)
        or receipt.get("compressed_size_bytes") != result_path.stat().st_size
        or receipt.get("gomaxprocs") != 1
        or receipt.get("bridge_process_count") != 1
        or receipt.get("persistent_bridge_for_all_shard_tasks") is not True
        or receipt.get("heavy_execution_started") is not expected_heavy
        or receipt.get("simulator_only") is not True
        or receipt.get("scientific_result_available") is not False
        or receipt.get("deployment_allowed") is not False
    ):
        _fail(f"shard {index} receipt differs from its exact execution contract")
    try:
        header = result_path.read_bytes()[:8]
    except OSError as error:
        _fail(f"could not read shard {index} gzip header: {error}")
    if len(header) < 8 or header[:2] != b"\x1f\x8b" or header[4:8] != b"\0\0\0\0":
        _fail(f"shard {index} output is not deterministic gzip mtime 0")


def _read_and_validate_shard(
    *,
    root: Path,
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    node: str,
) -> list[JSONMap]:
    index = int(shard["shard_index"])
    receipt_path = root / f"receipts/shard-{index:05d}.json"
    result_path = root / f"shards/shard-{index:05d}.jsonl.gz"
    if not receipt_path.is_file() or not result_path.is_file():
        _fail(f"shard {index} output or receipt is missing")
    receipt = _read_json(receipt_path, f"shard {index} receipt")
    _validate_receipt(
        receipt,
        plan=plan,
        dispatch=dispatch,
        shard=shard,
        node=node,
        result_path=result_path,
    )
    tasks = {str(row["task_id"]): row for row in plan["lane_tasks"]}
    groups = {
        str(row["group_id"]): row for row in plan["runner_plan"]["contract"]["groups"]
    }
    scenarios = {
        (str(row["instance_id"]), str(row["scenario_id"])): row
        for row in plan["runner_plan"]["contract"]["scenarios"]
    }
    policies = {
        str(row["policy_id"]): row
        for row in plan["runner_plan"]["contract"]["policies"]
    }
    validators = _producer_validators()
    logical = hashlib.sha256()
    rows: list[JSONMap] = []
    try:
        with gzip.open(result_path, "rb") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line or not line.endswith(b"\n") or not line.strip():
                    _fail(f"shard {index} row {line_number} is blank or unterminated")
                logical.update(line)
                value = json.loads(line)
                row = dict(_mapping(value, f"shard {index} row {line_number}"))
                task_identity = _mapping(row.get("task_identity"), "task identity")
                task_id = str(task_identity.get("task_id"))
                if task_id not in shard["task_ids"] or task_id not in tasks:
                    _fail(f"shard {index} contains an unassigned lane task")
                task = tasks[task_id]
                group = groups[str(task["group_id"])]
                scenario = scenarios[
                    (str(group["instance_id"]), str(group["scenario_id"]))
                ]
                policy = policies[str(task["policy_id"])]
                rows.append(
                    validate_rollout_row_v1(
                        row,
                        execution_plan=plan,
                        dispatch=dispatch,
                        shard_index=index,
                        task=task,
                        group=group,
                        scenario=scenario,
                        policy=policy,
                        artifact_validators=validators,
                    )
                )
    except FuryCatGapHpcReducerV1Error:
        raise
    except (OSError, gzip.BadGzipFile, json.JSONDecodeError, KeyError) as error:
        _fail(f"could not validate shard {index} rows: {error}")
    if (
        len(rows) != shard["task_count"]
        or logical.hexdigest() != receipt.get("logical_sha256")
        or sum(row["sufficient_statistics"]["completion_count"] for row in rows)
        != receipt.get("completion_count")
        or sum(
            row["sufficient_statistics"]["offline_score_eligible_count"]
            for row in rows
        )
        != receipt.get("offline_score_eligible_count")
    ):
        _fail(f"shard {index} row accounting differs from its receipt")
    return rows


def _read_validated_partial(
    *,
    root: Path,
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    node: str,
) -> list[JSONMap]:
    """Read only the shard-local validated sufficient-statistics artifact."""

    index = int(shard["shard_index"])
    receipt_path = root / f"receipts/shard-{index:05d}.json"
    partial_path = root / f"partials/shard-{index:05d}.json"
    result_path = root / f"shards/shard-{index:05d}.jsonl.gz"
    if (
        not receipt_path.is_file()
        or not partial_path.is_file()
        or not result_path.is_file()
    ):
        _fail(f"shard {index} retained raw gzip, compact partial, or receipt is missing")
    receipt = _read_json(receipt_path, f"shard {index} receipt")
    partial = _read_json(partial_path, f"shard {index} compact partial")
    receipt_fields = {
        "schema", "status", "execution_plan_sha256", "dispatch_plan_sha256",
        "execution_kind", "stage_id", "node", "shard_index", "task_ids_sha256",
        "result_count", "completion_count", "offline_score_eligible_count",
        "result_file", "partial_file", "compression", "logical_sha256",
        "compressed_sha256", "compressed_size_bytes", "partial_sha256", "gomaxprocs",
        "bridge_process_count", "persistent_bridge_for_all_shard_tasks",
        "heavy_execution_started", "simulator_only", "scientific_result_available",
        "deployment_allowed",
    }
    expected_heavy = plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1
    if (
        set(receipt) != receipt_fields
        or receipt.get("schema") != SHARD_RECEIPT_SCHEMA_V1
        or receipt.get("status") != "COMPLETE_SIMULATOR_ONLY_NONVOTING"
        or receipt.get("execution_plan_sha256")
        != plan["content_address"]["sha256"]
        or receipt.get("dispatch_plan_sha256")
        != dispatch["content_address"]["sha256"]
        or receipt.get("execution_kind") != plan["execution_kind"]
        or receipt.get("stage_id") != plan["stage_id"]
        or receipt.get("node") != node
        or receipt.get("shard_index") != index
        or receipt.get("task_ids_sha256") != sha256_json(shard["task_ids"])
        or receipt.get("result_count") != shard["task_count"]
        or receipt.get("result_file") != f"shards/shard-{index:05d}.jsonl.gz"
        or receipt.get("partial_file") != f"partials/shard-{index:05d}.json"
        or receipt.get("compression") != "gzip_mtime_0"
        or not isinstance(receipt.get("logical_sha256"), str)
        or len(receipt["logical_sha256"]) != 64
        or not isinstance(receipt.get("compressed_sha256"), str)
        or len(receipt["compressed_sha256"]) != 64
        or not isinstance(receipt.get("compressed_size_bytes"), int)
        or isinstance(receipt.get("compressed_size_bytes"), bool)
        or receipt["compressed_size_bytes"] <= 0
        or receipt["compressed_size_bytes"] != result_path.stat().st_size
        or receipt.get("gomaxprocs") != 1
        or receipt.get("bridge_process_count") != 1
        or receipt.get("persistent_bridge_for_all_shard_tasks") is not True
        or receipt.get("heavy_execution_started") is not expected_heavy
        or receipt.get("simulator_only") is not True
        or receipt.get("scientific_result_available") is not False
        or receipt.get("deployment_allowed") is not False
    ):
        _fail(f"shard {index} compact receipt identity is invalid")
    try:
        with result_path.open("rb") as handle:
            header = handle.read(8)
    except OSError as error:
        _fail(f"could not read retained shard {index} gzip header: {error}")
    if len(header) < 8 or header[:2] != b"\x1f\x8b" or header[4:8] != b"\0\0\0\0":
        _fail(f"shard {index} retained raw output is not gzip mtime 0")
    core = deepcopy(partial)
    address = _mapping(core.pop("content_address", None), "partial content address")
    partial_fields = {
        "schema", "status", "execution_plan_sha256", "dispatch_plan_sha256",
        "stage_id", "node", "shard_index", "task_ids_sha256", "task_count",
        "task_rows", "full_artifacts_validated_on_shard",
        "raw_gzip_retained_for_audit", "simulator_only", "deployment_allowed",
    }
    if (
        set(core) != partial_fields
        or partial.get("schema") != SHARD_PARTIAL_SCHEMA_V1
        or partial.get("status")
        != "COMPLETE_VALIDATED_SHARD_SUFFICIENT_STATISTICS"
        or address.get("sha256") != sha256_json(core)
        or receipt.get("partial_sha256") != address.get("sha256")
        or partial.get("execution_plan_sha256")
        != plan["content_address"]["sha256"]
        or partial.get("dispatch_plan_sha256")
        != dispatch["content_address"]["sha256"]
        or partial.get("stage_id") != plan["stage_id"]
        or partial.get("node") != node
        or partial.get("shard_index") != index
        or partial.get("task_ids_sha256") != sha256_json(shard["task_ids"])
        or partial.get("task_count") != shard["task_count"]
        or partial.get("full_artifacts_validated_on_shard") is not True
        or partial.get("raw_gzip_retained_for_audit") is not True
        or partial.get("simulator_only") is not True
        or partial.get("deployment_allowed") is not False
    ):
        _fail(f"shard {index} compact partial differs from its plan/receipt")
    rows = partial.get("task_rows")
    if not isinstance(rows, list) or len(rows) != shard["task_count"]:
        _fail(f"shard {index} compact task row count differs")
    expected_task_ids = set(str(value) for value in shard["task_ids"])
    planned_tasks = {
        str(row["task_id"]): row
        for row in plan["lane_tasks"]
        if str(row["task_id"]) in expected_task_ids
    }
    observed_task_ids: set[str] = set()
    fields = {
        "task_id", "group_id", "policy_id", "dps", "elapsed_ms",
        "completion_mode", "completion_criterion_met", "offline_score_eligible",
        "omitted_lane_count", "fatal_error_count",
        "dynamic_runtime_receipts_complete", "rollout_row_sha256",
    }
    checked: list[JSONMap] = []
    for raw in rows:
        row = dict(_mapping(raw, "compact task row"))
        task_id = str(row.get("task_id"))
        if (
            set(row) != fields
            or task_id not in expected_task_ids
            or task_id in observed_task_ids
            or row.get("group_id") != planned_tasks[task_id]["group_id"]
            or row.get("policy_id") != planned_tasks[task_id]["policy_id"]
            or not isinstance(row.get("dps"), (int, float))
            or isinstance(row.get("dps"), bool)
            or not math.isfinite(float(row["dps"]))
            or not isinstance(row.get("elapsed_ms"), int)
            or isinstance(row.get("elapsed_ms"), bool)
            or row["elapsed_ms"] <= 0
            or not isinstance(row.get("rollout_row_sha256"), str)
            or len(row["rollout_row_sha256"]) != 64
        ):
            _fail(f"shard {index} compact task row is invalid or duplicate")
        observed_task_ids.add(task_id)
        checked.append(row)
    if observed_task_ids != expected_task_ids:
        _fail(f"shard {index} compact task set differs from assignment")
    if (
        sum(int(row["completion_criterion_met"] is True) for row in checked)
        != receipt.get("completion_count")
        or sum(int(row["offline_score_eligible"] is True) for row in checked)
        != receipt.get("offline_score_eligible_count")
    ):
        _fail(f"shard {index} compact completion accounting differs from receipt")
    return checked


def _equal_instance_contrast(
    group_rows: Mapping[str, Mapping[str, Mapping[str, Any]]],
    group_by_id: Mapping[str, Mapping[str, Any]],
    *,
    candidate_id: str,
    baseline_id: str,
) -> JSONMap:
    by_instance: dict[str, list[float]] = defaultdict(list)
    by_instance_seed: dict[tuple[str, int], list[float]] = defaultdict(list)
    seeds: set[int] = set()
    for group_id, rows in group_rows.items():
        group = group_by_id[group_id]
        candidate = float(rows[candidate_id]["dps"])
        baseline = float(rows[baseline_id]["dps"])
        delta = candidate - baseline
        if not math.isfinite(delta):
            _fail("paired DPS delta is not finite")
        instance_id = str(group["instance_id"])
        master_seed = int(group["master_seed"])
        by_instance[instance_id].append(delta)
        by_instance_seed[(instance_id, master_seed)].append(delta)
        seeds.add(master_seed)
    instance_ids = sorted(by_instance)
    if not instance_ids:
        _fail("paired contrast has no instance support")
    instance_means = {
        instance_id: fmean(by_instance[instance_id]) for instance_id in instance_ids
    }
    per_seed = []
    for seed in sorted(seeds):
        supported = [
            fmean(by_instance_seed[(instance_id, seed)])
            for instance_id in instance_ids
            if (instance_id, seed) in by_instance_seed
        ]
        if len(supported) != len(instance_ids):
            _fail("one seed lacks support in an equal-weight instance stratum")
        per_seed.append(fmean(supported))
    return {
        "candidate_policy_id": candidate_id,
        "baseline_policy_id": baseline_id,
        "pair_count": len(group_rows),
        "instance_count": len(instance_ids),
        "master_seed_count": len(seeds),
        "instance_mean_dps_deltas": instance_means,
        "equal_instance_weighted_paired_mean_dps": fmean(instance_means.values()),
        "per_seed_equal_instance_weighted_dps_deltas": per_seed,
    }


def _holm_adjust(raw: Mapping[str, float]) -> dict[str, JSONMap]:
    ordered = sorted(raw.items(), key=lambda row: (row[1], row[0]))
    running = 0.0
    result: dict[str, JSONMap] = {}
    for index, (contrast_id, probability) in enumerate(ordered):
        adjusted = min(1.0, (len(ordered) - index) * probability)
        running = max(running, adjusted)
        result[contrast_id] = {
            "holm_rank": index + 1,
            "raw_two_sided_p": probability,
            "holm_adjusted_p": running,
            "holm_reject_familywise_0_05": running <= FAMILYWISE_ALPHA_V1,
        }
    return result


def reduce_stage_v1(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    output_directory: str | Path,
) -> JSONMap:
    """Join every lane exactly once; any incomplete stage raises with no retention."""

    try:
        plan = validate_execution_plan_v1(execution_plan)
        dispatch = validate_dispatch_plan_v1(dispatch_plan, plan)
    except FuryCatGapHpcPlanV1Error as error:
        _fail(str(error))
    root = Path(output_directory).expanduser().resolve()
    node_by_shard = _node_by_shard(dispatch)
    expected_task_ids = {str(row["task_id"]) for row in plan["lane_tasks"]}
    observed: dict[str, JSONMap] = {}
    for shard in plan["shards"]:
        index = int(shard["shard_index"])
        if index not in node_by_shard:
            _fail(f"shard {index} has no dispatch node")
        try:
            rows = _read_validated_partial(
                root=root,
                plan=plan,
                dispatch=dispatch,
                shard=shard,
                node=node_by_shard[index],
            )
        except FuryCatGapHpcWorkerV1Error as error:
            _fail(str(error))
        for row in rows:
            task_id = str(row["task_id"])
            if task_id in observed:
                _fail(f"duplicate lane task result: {task_id}")
            observed[task_id] = row
    if set(observed) != expected_task_ids or len(observed) != plan["task_count"]:
        _fail("observed lane task set differs from the execution plan")

    group_by_id = {
        str(row["group_id"]): row for row in plan["runner_plan"]["contract"]["groups"]
    }
    by_group: dict[str, dict[str, JSONMap]] = defaultdict(dict)
    for row in observed.values():
        group_id = str(row["group_id"])
        policy_id = str(row["policy_id"])
        if policy_id in by_group[group_id]:
            _fail("one group contains a duplicate policy lane")
        if (
            row.get("offline_score_eligible") is not True
            or row.get("completion_criterion_met") is not True
            or row.get("omitted_lane_count") != 0
            or row.get("fatal_error_count") != 0
            or row.get("dynamic_runtime_receipts_complete") is not True
        ):
            _fail(f"lane {group_id}/{policy_id} is incomplete or noneligible")
        by_group[group_id][policy_id] = row
    expected_policies = set(plan["policy_ids"])
    if set(by_group) != set(group_by_id):
        _fail("group result set differs from runner groups")
    for group_id, rows in by_group.items():
        if set(rows) != expected_policies:
            _fail(f"group {group_id} does not contain every planned policy once")
        _validate_paired_horizon_elapsed_v3(
            [{"lane_result": row} for row in rows.values()]
        )

    candidates = list(plan["candidate_ids"])
    baselines = (CAT_POLICY_ID, CONTRA260817_POLICY_ID)
    contrasts: dict[str, JSONMap] = {}
    for candidate_id in candidates:
        for baseline_id in baselines:
            contrast_id = f"{candidate_id}__minus__{baseline_id}"
            contrasts[contrast_id] = _equal_instance_contrast(
                by_group,
                group_by_id,
                candidate_id=candidate_id,
                baseline_id=baseline_id,
            )
    rankings = []
    for candidate_id in candidates:
        means = {
            baseline_id: contrasts[
                f"{candidate_id}__minus__{baseline_id}"
            ]["equal_instance_weighted_paired_mean_dps"]
            for baseline_id in baselines
        }
        rankings.append(
            {
                "candidate_id": candidate_id,
                "equal_instance_weighted_mean_by_baseline": means,
                "dual_baseline_maximin_mean_dps": min(means.values()),
            }
        )
    rankings.sort(
        key=lambda row: (-float(row["dual_baseline_maximin_mean_dps"]), row["candidate_id"])
    )
    kind = plan["execution_kind"]
    stage_id = plan["stage_id"]
    selection: JSONMap | None = None
    if kind == LOCAL_SMOKE_EXECUTION_KIND_V1:
        status = "LOCAL_SMOKE_COMPLETE_NO_RETENTION"
        retained: list[str] = []
        retention_allowed = False
    elif stage_id == "selection_validation":
        if len(contrasts) != 4:
            _fail("selection stage must contain exactly four candidate-baseline contrasts")
        raw: dict[str, float] = {}
        statistics: dict[str, JSONMap] = {}
        for contrast_id, row in contrasts.items():
            statistic, statistic_kind, probability = _paired_t_test_v2(
                row["per_seed_equal_instance_weighted_dps_deltas"]
            )
            raw[contrast_id] = probability
            statistics[contrast_id] = {
                "paired_t_statistic": statistic,
                "paired_t_statistic_kind": statistic_kind,
                "paired_t_degrees_of_freedom": row["master_seed_count"] - 1,
            }
        adjusted = _holm_adjust(raw)
        for contrast_id in contrasts:
            contrasts[contrast_id].update(statistics[contrast_id])
            contrasts[contrast_id].update(adjusted[contrast_id])
        passing = []
        for candidate_id in candidates:
            ids = [f"{candidate_id}__minus__{baseline}" for baseline in baselines]
            if all(
                contrasts[contrast_id]["equal_instance_weighted_paired_mean_dps"]
                > 0.0
                and contrasts[contrast_id]["holm_reject_familywise_0_05"] is True
                for contrast_id in ids
            ):
                passing.append(candidate_id)
        selected = next(
            (row["candidate_id"] for row in rankings if row["candidate_id"] in passing),
            None,
        )
        retained = [selected] if selected is not None else []
        retention_allowed = selected is not None
        status = (
            "SELECTION_COMPLETE_POLICY_SELECTED_DEVELOPMENT_ONLY"
            if selected is not None
            else "SELECTION_COMPLETE_NO_SELECTION"
        )
        selection = {
            "contrast_family_size": 4,
            "paired_test": "two_sided_paired_student_t_over_per_seed_equal_instance_means",
            "multiplicity": "HOLM_FAMILYWISE_ALPHA_0.05",
            "passing_candidate_ids": passing,
            "selected_candidate_id": selected,
            "scientific_confirmation": False,
        }
    else:
        retained_count = int(plan["stage_contract"]["retained_candidate_count"])
        if retained_count <= 0 or retained_count > len(rankings):
            _fail("successive-halving retained count is invalid")
        retained = [row["candidate_id"] for row in rankings[:retained_count]]
        retention_allowed = True
        status = "STAGE_COMPLETE_RETENTION_READY"

    core = {
        "schema": REDUCTION_SCHEMA_V1,
        "status": status,
        "stage_id": stage_id,
        "execution_kind": kind,
        "search_plan_sha256": plan["search_plan_sha256"],
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "expected_task_count": plan["task_count"],
        "observed_unique_task_count": len(observed),
        "group_count": len(by_group),
        "candidate_count": len(candidates),
        "baseline_policy_ids": list(baselines),
        "contrast_count": len(contrasts),
        "contrasts": contrasts,
        "ranking_metric": "dual_baseline_maximin_equal_instance_weighted_paired_mean_dps",
        "ranking_tie_break": "candidate_id_ascending",
        "candidate_ranking": rankings,
        "retained_candidate_ids": retained,
        "retention_allowed": retention_allowed,
        "selection": selection,
        "complete_accounting": True,
        "failure_status_if_any_lane_missing_duplicate_or_ineligible": (
            "STAGE_FAILED_NO_RETENTION"
        ),
        "heavy_execution_started": kind == REAL_STAGE_EXECUTION_KIND_V1,
        "simulator_only": True,
        "development_only": True,
        "old50_heldout_evidence": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def validate_local_smoke_evidence_v1(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    output_directory: str | Path,
) -> JSONMap:
    """Validate all three raw lanes plus the compact merge for one bridge smoke."""

    try:
        plan = validate_execution_plan_v1(execution_plan)
        dispatch = validate_dispatch_plan_v1(dispatch_plan, plan)
    except FuryCatGapHpcPlanV1Error as error:
        _fail(str(error))
    if (
        plan["execution_kind"] != LOCAL_SMOKE_EXECUTION_KIND_V1
        or plan["task_count"] != 3
        or plan["shard_count"] != 1
        or len(plan["candidate_ids"]) != 1
        or dispatch["workers_per_node"] != 1
        or dispatch["gomaxprocs"] != 1
        or len(dispatch["nodes"]) != 1
        or dispatch["nodes"][0]["name"] != "local"
    ):
        _fail("executor admission requires the exact one-process three-lane smoke")
    root = Path(output_directory).expanduser().resolve()
    shard = plan["shards"][0]
    raw_rows = _read_and_validate_shard(
        root=root,
        plan=plan,
        dispatch=dispatch,
        shard=shard,
        node="local",
    )
    partial_rows = _read_validated_partial(
        root=root,
        plan=plan,
        dispatch=dispatch,
        shard=shard,
        node="local",
    )
    expected_partial_rows = [_compact_partial_row(row) for row in raw_rows]
    if partial_rows != expected_partial_rows:
        _fail("smoke compact partial differs from its fully validated raw lanes")
    reduction = reduce_stage_v1(plan, dispatch, output_directory=root)
    if (
        reduction.get("status") != "LOCAL_SMOKE_COMPLETE_NO_RETENTION"
        or reduction.get("observed_unique_task_count") != 3
        or reduction.get("retention_allowed") is not False
        or reduction.get("heavy_execution_started") is not False
        or reduction.get("deployment_allowed") is not False
    ):
        _fail("smoke reduction differs from the non-retaining execution contract")
    receipt_path = root / "receipts/shard-00000.json"
    receipt = _read_json(receipt_path, "local smoke shard receipt")
    contract = plan["runner_plan"]["contract"]
    group = contract["groups"][0]
    core = {
        "schema": LOCAL_SMOKE_EVIDENCE_SCHEMA_V1,
        "status": "PASS_REAL_BRIDGE_ONE_PROCESS_THREE_LANE_SMOKE",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "reduction_sha256": reduction["content_address"]["sha256"],
        "shard_receipt_sha256": sha256_json(receipt),
        "raw_logical_sha256": receipt["logical_sha256"],
        "raw_compressed_sha256": receipt["compressed_sha256"],
        "partial_sha256": receipt["partial_sha256"],
        "bridge_sha256": contract["bridge_identity"]["sha256"],
        "master_seed": group["master_seed"],
        "simulator_seed": group["simulator_seed"],
        "simulator_seed_namespace": contract["seed_derivation"]["namespace"],
        "candidate_id": plan["candidate_ids"][0],
        "policy_ids": list(plan["policy_ids"]),
        "validated_full_rollout_artifact_count": len(raw_rows),
        "task_count": plan["task_count"],
        "bridge_process_count": receipt["bridge_process_count"],
        "gomaxprocs": receipt["gomaxprocs"],
        "heavy_execution_started": receipt["heavy_execution_started"],
        "retention_allowed": False,
        "simulator_only": True,
        "deployment_allowed": False,
    }
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _atomic_write_json(path: Path | None, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if path is None:
        print(payload.decode("utf-8"), end="")
        return
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        import os

        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = reduce_stage_v1(
            _read_json(args.execution_plan, "execution plan"),
            _read_json(args.dispatch_plan, "dispatch plan"),
            output_directory=args.output_directory,
        )
        _atomic_write_json(args.output, result)
        return 0
    except (FuryCatGapHpcReducerV1Error, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "FAMILYWISE_ALPHA_V1",
    "FuryCatGapHpcReducerV1Error",
    "LOCAL_SMOKE_EVIDENCE_SCHEMA_V1",
    "REDUCTION_SCHEMA_V1",
    "reduce_stage_v1",
    "validate_local_smoke_evidence_v1",
)
