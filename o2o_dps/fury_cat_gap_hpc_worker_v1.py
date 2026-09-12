"""Persistent-shard worker for the variable-lane Cat-gap development search."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .cat2new_fury_cat_gap_policy_v1 import (
    Cat2NewFuryCatGapPolicyV1,
    FuryCatGapPolicyParametersV1,
    build_cat_gap_policy_v1,
)
from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CANDIDATE_PRODUCER_V1,
    Cat2NewFuryPairedLaneAdapterV3,
    validate_cat2new_fury_paired_artifact_v3,
)
from .cat_fury_paired_lane_adapter_v6 import (
    CAT_V6_PRODUCER,
    execute_cat_runner_v4_lane_v6,
    validate_cat_runner_v4_artifact_v6,
)
from .contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    execute_contra260817_runner_v4_lane_v4,
    validate_contra260817_runner_v4_artifact_v4,
)
from .fury_cat_gap_hpc_plan_v1 import (
    REAL_STAGE_EXECUTION_KIND_V1,
    FuryCatGapHpcPlanV1Error,
    validate_dispatch_plan_v1,
    validate_execution_plan_v1,
)
from .fury_multiseed_hpc_worker_v3 import (
    _group_identity,
    _load_identity,
    _scenario_identity,
    _sufficient_statistics,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    FuryPairedRunnerV4Error,
    sha256_json,
    validate_lane_result_v4,
)
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


JSONMap = dict[str, Any]
ROLLOUT_ROW_SCHEMA_V1 = "fury_cat_gap_variable_lane_rollout_row/v1"
SHARD_RECEIPT_SCHEMA_V1 = "fury_cat_gap_persistent_shard_receipt/v1"
SHARD_PARTIAL_SCHEMA_V1 = "fury_cat_gap_validated_shard_partial/v1"


class FuryCatGapHpcWorkerV1Error(RuntimeError):
    """A shard task, runtime identity, or atomic output is incomplete."""


@dataclass(frozen=True)
class VariableLaneRegistryV1:
    executors: Mapping[str, Callable[..., Mapping[str, Any]]]
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapHpcWorkerV1Error(f"{label} must be an object")
    return value


def _canonical_line(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_process_runtime_environment_v1(
    execution_plan: Mapping[str, Any],
    *,
    bridge_path: Path,
    bridge_cwd: Path,
) -> None:
    if execution_plan.get("execution_kind") != REAL_STAGE_EXECUTION_KIND_V1:
        if os.environ.get("GOMAXPROCS") != "1":
            raise FuryCatGapHpcWorkerV1Error("worker requires GOMAXPROCS=1")
        return
    from .fury_cat_gap_formal_preparation_v1 import (
        EXPECTED_LINUX_V11_BRIDGE_SHA256,
        validate_exact_linux_runtime_closure_v1,
    )

    closure = validate_exact_linux_runtime_closure_v1(
        _mapping(execution_plan.get("runtime_closure"), "runtime closure")
    )
    expected_environment = {}
    for name, relative in closure["environment"].items():
        expected_environment[name] = (
            "1"
            if name == "GOMAXPROCS"
            else str(Path.home().joinpath(*PurePosixPath(relative).parts))
        )
    if any(
        os.environ.get(name) != expected
        for name, expected in expected_environment.items()
    ):
        raise FuryCatGapHpcWorkerV1Error(
            "worker process environment differs from the admitted runtime closure"
        )
    expected_bridge = Path.home().joinpath(
        *PurePosixPath(
            f"{closure['shared_root']}/releases/dynamic-v5/"
            f"{EXPECTED_LINUX_V11_BRIDGE_SHA256}/o2obridge.linux-amd64"
        ).parts
    )
    if bridge_path != expected_bridge or bridge_cwd != expected_bridge.parent:
        raise FuryCatGapHpcWorkerV1Error(
            "worker bridge path differs from the admitted v11 runtime release"
        )


def _candidate_design_by_id(plan: Mapping[str, Any]) -> dict[str, JSONMap]:
    search = _mapping(plan.get("search_plan"), "search plan")
    design = _mapping(search.get("parameter_design"), "parameter design").get(
        "candidates"
    )
    if not isinstance(design, list):
        raise FuryCatGapHpcWorkerV1Error("candidate design is missing")
    return {
        str(row["candidate_id"]): dict(row)
        for row in design
        if isinstance(row, Mapping)
    }


def build_variable_lane_registry_v1(
    bridge: Any, execution_plan: Mapping[str, Any]
) -> VariableLaneRegistryV1:
    """Bind both baselines and every retained candidate to one bridge."""

    plan = validate_execution_plan_v1(execution_plan)
    policies = {
        str(row["policy_id"]): row
        for row in plan["runner_plan"]["contract"]["policies"]
    }
    catalog = _candidate_design_by_id(plan)
    executors: dict[str, Callable[..., Mapping[str, Any]]] = {
        CAT_POLICY_ID: lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
            bridge, group=group, scenario=scenario, policy=policy
        ),
        CONTRA260817_POLICY_ID: (
            lambda *, group, scenario, policy: execute_contra260817_runner_v4_lane_v4(
                bridge, group=group, scenario=scenario, policy=policy
            )
        ),
    }
    for candidate_id in plan["candidate_ids"]:
        design = catalog[str(candidate_id)]
        parameters = FuryCatGapPolicyParametersV1.from_mapping(design["parameters"])
        feedback_policy = build_cat_gap_policy_v1(str(candidate_id), asdict(parameters))
        policy_row = policies[str(candidate_id)]
        if (
            type(feedback_policy) is not Cat2NewFuryCatGapPolicyV1
            or feedback_policy.policy_id != candidate_id
            or sha256_json(asdict(feedback_policy.config))
            != policy_row["profile_sha256"]
        ):
            raise FuryCatGapHpcWorkerV1Error(
                "candidate factory differs from planned 13D policy identity"
            )
        executors[str(candidate_id)] = Cat2NewFuryPairedLaneAdapterV3(
            bridge=bridge,
            feedback_policy=feedback_policy,
            optimizer_parameters=asdict(feedback_policy.config),
        )
    return VariableLaneRegistryV1(
        executors=executors,
        artifact_validators={
            CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
            CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
            CANDIDATE_PRODUCER_V1: validate_cat2new_fury_paired_artifact_v3,
        },
    )


def _producer_for(policy: Mapping[str, Any]) -> str:
    policy_id = str(policy.get("policy_id"))
    if policy_id == CAT_POLICY_ID:
        return CAT_V6_PRODUCER
    if policy_id == CONTRA260817_POLICY_ID:
        return CONTRA260817_V4_PRODUCER
    if policy.get("role") == "CANDIDATE":
        return CANDIDATE_PRODUCER_V1
    raise FuryCatGapHpcWorkerV1Error(f"unregistered lane policy: {policy_id}")


def _validate_registry(
    registry: VariableLaneRegistryV1, policy_ids: Sequence[str]
) -> VariableLaneRegistryV1:
    if set(registry.executors) != set(policy_ids) or not all(
        callable(value) for value in registry.executors.values()
    ):
        raise FuryCatGapHpcWorkerV1Error(
            "registry must contain exactly the planned variable policy lanes"
        )
    expected = {
        CAT_V6_PRODUCER,
        CONTRA260817_V4_PRODUCER,
        CANDIDATE_PRODUCER_V1,
    }
    if set(registry.artifact_validators) != expected or not all(
        callable(value) for value in registry.artifact_validators.values()
    ):
        raise FuryCatGapHpcWorkerV1Error(
            "registry is missing a native producer artifact validator"
        )
    return registry


def _shard_context(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
) -> tuple[JSONMap, JSONMap, JSONMap, list[JSONMap], dict[str, JSONMap], dict[tuple[str, str], JSONMap], dict[str, JSONMap]]:
    try:
        plan = validate_execution_plan_v1(execution_plan)
        dispatch = validate_dispatch_plan_v1(dispatch_plan, plan)
    except FuryCatGapHpcPlanV1Error as error:
        raise FuryCatGapHpcWorkerV1Error(str(error)) from error
    nodes = [row for row in dispatch["nodes"] if row["name"] == node_name]
    if len(nodes) != 1 or shard_index not in nodes[0]["shard_indices"]:
        raise FuryCatGapHpcWorkerV1Error("shard is not assigned to this node")
    shards = [row for row in plan["shards"] if row["shard_index"] == shard_index]
    if len(shards) != 1:
        raise FuryCatGapHpcWorkerV1Error("execution shard is not unique")
    task_by_id = {str(row["task_id"]): row for row in plan["lane_tasks"]}
    try:
        tasks = [dict(task_by_id[str(task_id)]) for task_id in shards[0]["task_ids"]]
    except KeyError as error:
        raise FuryCatGapHpcWorkerV1Error(
            f"shard refers to an unknown task: {error.args[0]}"
        ) from error
    runner = plan["runner_plan"]
    contract = runner["contract"]
    groups = {str(row["group_id"]): row for row in contract["groups"]}
    scenarios = {
        (str(row["instance_id"]), str(row["scenario_id"])): row
        for row in contract["scenarios"]
    }
    policies = {str(row["policy_id"]): row for row in contract["policies"]}
    return plan, dispatch, shards[0], tasks, groups, scenarios, policies


def _validate_candidate_profile(
    lane: Mapping[str, Any],
    policy: Mapping[str, Any],
    design: Mapping[str, Any],
) -> None:
    if policy.get("role") != "CANDIDATE":
        return
    artifact = _mapping(lane.get("artifact"), "candidate artifact")
    identity = _mapping(artifact.get("identity"), "candidate artifact identity")
    context = _mapping(artifact.get("policy_context"), "candidate policy context")
    profile = _mapping(context.get("optimizer_parameters"), "optimizer parameters")
    parameters = _mapping(profile.get("parameters"), "controller parameters")
    if (
        identity.get("policy_id") != policy.get("policy_id")
        or identity.get("optimizer_parameters_sha256") != policy.get("profile_sha256")
        or sha256_json(dict(profile)) != policy.get("profile_sha256")
        or dict(parameters) != design.get("parameters")
        or sha256_json(dict(parameters)) != design.get("parameter_sha256")
    ):
        raise FuryCatGapHpcWorkerV1Error(
            "candidate artifact differs from planned ID/profile/13D parameters"
        )
    decisions = artifact.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise FuryCatGapHpcWorkerV1Error("candidate artifact has no policy decisions")
    for decision in decisions:
        intent = decision.get("policy_intent") if isinstance(decision, Mapping) else None
        metadata = intent.get("policy_metadata") if isinstance(intent, Mapping) else None
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("candidate_id") != policy.get("policy_id")
            or metadata.get("parameter_sha256") != design.get("parameter_sha256")
            or metadata.get("parameters") != design.get("parameters")
            or metadata.get("simulator_only") is not True
            or metadata.get("deployment_allowed") is not False
        ):
            raise FuryCatGapHpcWorkerV1Error(
                "candidate decision metadata is not the exact 13D development policy"
            )


def _rollout_row(
    *,
    execution_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard_index: int,
    task: Mapping[str, Any],
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
    lane: Mapping[str, Any],
) -> JSONMap:
    return {
        "schema": ROLLOUT_ROW_SCHEMA_V1,
        "execution_plan_sha256": execution_plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "shard_index": shard_index,
        "task_identity": dict(task),
        "group_identity": _group_identity(group),
        "scenario_identity": _scenario_identity(scenario),
        "policy_identity": dict(policy),
        "dynamic_load_identity": _load_identity(group, scenario),
        "producer": lane["producer"],
        "artifact_schema": lane["artifact_schema"],
        "artifact_sha256": lane["artifact_sha256"],
        "producer_runtime_receipt_sha256": lane[
            "producer_runtime_receipt_sha256"
        ],
        "sufficient_statistics": _sufficient_statistics(lane),
        "lane_result": dict(lane),
    }


def validate_rollout_row_v1(
    row: Mapping[str, Any],
    *,
    execution_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard_index: int,
    task: Mapping[str, Any],
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]],
) -> JSONMap:
    expected_fields = {
        "schema", "execution_plan_sha256", "dispatch_plan_sha256", "shard_index",
        "task_identity", "group_identity", "scenario_identity", "policy_identity",
        "dynamic_load_identity", "producer", "artifact_schema", "artifact_sha256",
        "producer_runtime_receipt_sha256", "sufficient_statistics", "lane_result",
    }
    if set(row) != expected_fields or row.get("schema") != ROLLOUT_ROW_SCHEMA_V1:
        raise FuryCatGapHpcWorkerV1Error("rollout row field set or schema mismatch")
    exact = {
        "execution_plan_sha256": execution_plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "shard_index": shard_index,
        "task_identity": dict(task),
        "group_identity": _group_identity(group),
        "scenario_identity": _scenario_identity(scenario),
        "policy_identity": dict(policy),
        "dynamic_load_identity": _load_identity(group, scenario),
    }
    if any(row.get(field) != value for field, value in exact.items()):
        raise FuryCatGapHpcWorkerV1Error("rollout row identity differs from lane task")
    lane = _mapping(row.get("lane_result"), "lane result")
    producer = _producer_for(policy)
    if lane.get("producer") != producer or row.get("producer") != producer:
        raise FuryCatGapHpcWorkerV1Error("rollout producer differs from policy lane")
    try:
        validated = validate_lane_result_v4(
            lane,
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=artifact_validators.get(producer),
        )
    except FuryPairedRunnerV4Error as error:
        raise FuryCatGapHpcWorkerV1Error(str(error)) from error
    catalog = _candidate_design_by_id(execution_plan)
    if policy.get("role") == "CANDIDATE":
        _validate_candidate_profile(
            validated, policy, catalog[str(policy["policy_id"])]
        )
    derived = {
        "artifact_schema": validated["artifact_schema"],
        "artifact_sha256": validated["artifact_sha256"],
        "producer_runtime_receipt_sha256": validated[
            "producer_runtime_receipt_sha256"
        ],
        "sufficient_statistics": _sufficient_statistics(validated),
    }
    if any(row.get(field) != value for field, value in derived.items()):
        raise FuryCatGapHpcWorkerV1Error(
            "rollout summary differs from validated producer artifact"
        )
    return dict(row)


def _iter_shard_rows(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
    registry: VariableLaneRegistryV1,
) -> tuple[JSONMap, JSONMap, JSONMap, Iterable[JSONMap]]:
    if os.environ.get("GOMAXPROCS") != "1":
        raise FuryCatGapHpcWorkerV1Error("worker requires GOMAXPROCS=1")
    plan, dispatch, shard, tasks, groups, scenarios, policies = _shard_context(
        execution_plan,
        dispatch_plan,
        node_name=node_name,
        shard_index=shard_index,
    )
    registry = _validate_registry(registry, plan["policy_ids"])
    catalog = _candidate_design_by_id(plan)

    def rows() -> Iterable[JSONMap]:
        for task in tasks:
            group = groups[str(task["group_id"])]
            scenario = scenarios[(str(group["instance_id"]), str(group["scenario_id"]))]
            policy = policies[str(task["policy_id"])]
            raw = registry.executors[str(task["policy_id"])](
                group=dict(group), scenario=dict(scenario), policy=dict(policy)
            )
            if not isinstance(raw, Mapping) or set(raw) != {"lane_result"}:
                raise FuryCatGapHpcWorkerV1Error(
                    "registered executor must return only lane_result"
                )
            lane = _mapping(raw["lane_result"], "lane result")
            row = _rollout_row(
                execution_plan=plan,
                dispatch=dispatch,
                shard_index=shard_index,
                task=task,
                group=group,
                scenario=scenario,
                policy=policy,
                lane=lane,
            )
            if policy.get("role") == "CANDIDATE" and str(
                policy["policy_id"]
            ) not in catalog:
                raise FuryCatGapHpcWorkerV1Error("candidate task is outside design")
            yield validate_rollout_row_v1(
                row,
                execution_plan=plan,
                dispatch=dispatch,
                shard_index=shard_index,
                task=task,
                group=group,
                scenario=scenario,
                policy=policy,
                artifact_validators=registry.artifact_validators,
            )

    return plan, dispatch, shard, rows()


def _base_receipt(
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    *,
    node_name: str,
    result_count: int,
    completion_count: int,
    eligible_count: int,
) -> JSONMap:
    index = int(shard["shard_index"])
    return {
        "schema": SHARD_RECEIPT_SCHEMA_V1,
        "status": "COMPLETE_SIMULATOR_ONLY_NONVOTING",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "execution_kind": plan["execution_kind"],
        "stage_id": plan["stage_id"],
        "node": node_name,
        "shard_index": index,
        "task_ids_sha256": sha256_json(shard["task_ids"]),
        "result_count": result_count,
        "completion_count": completion_count,
        "offline_score_eligible_count": eligible_count,
        "result_file": f"shards/shard-{index:05d}.jsonl.gz",
        "partial_file": f"partials/shard-{index:05d}.json",
        "compression": "gzip_mtime_0",
        "logical_sha256": None,
        "compressed_sha256": None,
        "compressed_size_bytes": None,
        "partial_sha256": None,
        "gomaxprocs": 1,
        "bridge_process_count": 1,
        "persistent_bridge_for_all_shard_tasks": True,
        "heavy_execution_started": plan["execution_kind"]
        == REAL_STAGE_EXECUTION_KIND_V1,
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


def _compact_partial_row(row: Mapping[str, Any]) -> JSONMap:
    task = _mapping(row.get("task_identity"), "task identity")
    lane = _mapping(row.get("lane_result"), "lane result")
    return {
        "task_id": task["task_id"],
        "group_id": task["group_id"],
        "policy_id": task["policy_id"],
        "dps": lane["dps"],
        "elapsed_ms": lane["elapsed_ms"],
        "completion_mode": lane["completion_mode"],
        "completion_criterion_met": lane["completion_criterion_met"],
        "offline_score_eligible": lane["offline_score_eligible"],
        "omitted_lane_count": lane["omitted_lane_count"],
        "fatal_error_count": lane["fatal_error_count"],
        "dynamic_runtime_receipts_complete": lane[
            "dynamic_runtime_receipts_complete"
        ],
        "rollout_row_sha256": sha256_json(row),
    }


def _partial_document(
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    *,
    node_name: str,
    compact_rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    compact = [dict(row) for row in compact_rows]
    core = {
        "schema": SHARD_PARTIAL_SCHEMA_V1,
        "status": "COMPLETE_VALIDATED_SHARD_SUFFICIENT_STATISTICS",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "stage_id": plan["stage_id"],
        "node": node_name,
        "shard_index": shard["shard_index"],
        "task_ids_sha256": sha256_json(shard["task_ids"]),
        "task_count": len(compact),
        "task_rows": compact,
        "full_artifacts_validated_on_shard": True,
        "raw_gzip_retained_for_audit": True,
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


def execute_shard_v1(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
    registry: VariableLaneRegistryV1,
) -> tuple[JSONMap, list[JSONMap]]:
    """Execute one shard in memory; intended for bounded tests only."""

    plan, dispatch, shard, iterator = _iter_shard_rows(
        execution_plan,
        dispatch_plan,
        node_name=node_name,
        shard_index=shard_index,
        registry=registry,
    )
    rows = list(iterator)
    receipt = _base_receipt(
        plan,
        dispatch,
        shard,
        node_name=node_name,
        result_count=len(rows),
        completion_count=sum(
            row["sufficient_statistics"]["completion_count"] for row in rows
        ),
        eligible_count=sum(
            row["sufficient_statistics"]["offline_score_eligible_count"]
            for row in rows
        ),
    )
    return receipt, rows


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_shard_worker_v1(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
    registry: VariableLaneRegistryV1,
    output_directory: str | Path,
) -> JSONMap:
    """Stream one shard to an atomic gzip JSONL and publish its receipt last."""

    plan, dispatch, shard, rows = _iter_shard_rows(
        execution_plan,
        dispatch_plan,
        node_name=node_name,
        shard_index=shard_index,
        registry=registry,
    )
    root = Path(output_directory).expanduser().resolve()
    result_path = root / f"shards/shard-{shard_index:05d}.jsonl.gz"
    receipt_path = root / f"receipts/shard-{shard_index:05d}.json"
    partial_path = root / f"partials/shard-{shard_index:05d}.json"
    if result_path.exists() or receipt_path.exists() or partial_path.exists():
        raise FuryCatGapHpcWorkerV1Error(
            "shard output already exists; inspect it before retrying"
        )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{result_path.name}.", dir=result_path.parent
    )
    temporary = Path(temporary_name)
    logical = hashlib.sha256()
    count = completion_count = eligible_count = 0
    compact_rows: list[JSONMap] = []
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                for row in rows:
                    payload = _canonical_line(row)
                    logical.update(payload)
                    compressed.write(payload)
                    count += 1
                    completion_count += row["sufficient_statistics"][
                        "completion_count"
                    ]
                    eligible_count += row["sufficient_statistics"][
                        "offline_score_eligible_count"
                    ]
                    compact_rows.append(_compact_partial_row(row))
            raw.flush()
            os.fsync(raw.fileno())
        if count != shard["task_count"]:
            raise FuryCatGapHpcWorkerV1Error(
                "executed row count differs from assigned shard task count"
            )
        temporary.replace(result_path)
        receipt = _base_receipt(
            plan,
            dispatch,
            shard,
            node_name=node_name,
            result_count=count,
            completion_count=completion_count,
            eligible_count=eligible_count,
        )
        receipt["logical_sha256"] = logical.hexdigest()
        receipt["compressed_sha256"] = _file_sha256(result_path)
        receipt["compressed_size_bytes"] = result_path.stat().st_size
        try:
            partial = _partial_document(
                plan,
                dispatch,
                shard,
                node_name=node_name,
                compact_rows=compact_rows,
            )
            _atomic_write(partial_path, _canonical_line(partial))
            receipt["partial_sha256"] = partial["content_address"]["sha256"]
            _atomic_write(receipt_path, _canonical_line(receipt))
        except Exception:
            result_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
            raise
        return receipt
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapHpcWorkerV1Error(f"could not read {label}: {error}") from error
    return dict(_mapping(value, label))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--bridge-cwd", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = _read_json(args.execution_plan, "execution plan")
        dispatch = _read_json(args.dispatch_plan, "dispatch plan")
        checked = validate_execution_plan_v1(plan)
        bridge_path = args.bridge.expanduser().resolve()
        bridge_cwd = args.bridge_cwd.expanduser().resolve()
        _validate_process_runtime_environment_v1(
            checked,
            bridge_path=bridge_path,
            bridge_cwd=bridge_cwd,
        )
        if _file_sha256(bridge_path) != checked["runner_plan"]["contract"][
            "bridge_identity"
        ]["sha256"]:
            raise FuryCatGapHpcWorkerV1Error(
                "bridge binary differs from execution plan identity"
            )
        with SimulatorBridgeDynamicV3(bridge_path, cwd=bridge_cwd) as bridge:
            receipt = run_shard_worker_v1(
                checked,
                dispatch,
                node_name=args.node,
                shard_index=args.shard_index,
                registry=build_variable_lane_registry_v1(bridge, checked),
                output_directory=args.output_directory,
            )
        print(_canonical_line(receipt).decode("utf-8"), end="")
        return 0
    except (
        FuryCatGapHpcPlanV1Error,
        FuryCatGapHpcWorkerV1Error,
        FuryPairedRunnerV4Error,
        OSError,
        ValueError,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "FuryCatGapHpcWorkerV1Error",
    "ROLLOUT_ROW_SCHEMA_V1",
    "SHARD_PARTIAL_SCHEMA_V1",
    "SHARD_RECEIPT_SCHEMA_V1",
    "VariableLaneRegistryV1",
    "build_variable_lane_registry_v1",
    "execute_shard_v1",
    "run_shard_worker_v1",
    "validate_rollout_row_v1",
)
