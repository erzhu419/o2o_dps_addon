"""Independent worker for adaptive Cat-gap v2 candidate registries."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import gzip
import hashlib
import json
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
    PRODUCER as CANDIDATE_PRODUCER_V2,
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
from .fury_cat_gap_hpc_plan_v1 import REAL_STAGE_EXECUTION_KIND_V1
from .fury_cat_gap_stage_transition_v2 import (
    FuryCatGapStageTransitionV2Error,
    validate_dispatch_plan_v2,
    validate_execution_plan_v2,
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
ROLLOUT_ROW_SCHEMA_V2 = "fury_cat_gap_adaptive_rollout_row/v2"
SHARD_RECEIPT_SCHEMA_V2 = "fury_cat_gap_adaptive_shard_receipt/v2"
SHARD_PARTIAL_SCHEMA_V2 = "fury_cat_gap_adaptive_shard_partial/v2"


class FuryCatGapHpcWorkerV2Error(RuntimeError):
    """A v2 registry, shard task, or output is incomplete."""


@dataclass(frozen=True)
class VariableLaneRegistryV2:
    executors: Mapping[str, Callable[..., Mapping[str, Any]]]
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapHpcWorkerV2Error(f"{label} must be an object")
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


def _candidate_design_by_id(plan: Mapping[str, Any]) -> dict[str, JSONMap]:
    update = _mapping(plan.get("candidate_update_receipt"), "candidate update receipt")
    rows = update.get("candidate_registry")
    if not isinstance(rows, list):
        raise FuryCatGapHpcWorkerV2Error("updated candidate registry is missing")
    catalog = {
        str(row.get("candidate_id")): dict(row)
        for row in rows
        if isinstance(row, Mapping)
    }
    if len(catalog) != len(rows):
        raise FuryCatGapHpcWorkerV2Error("updated candidate registry is not unique")
    return catalog


def build_variable_lane_registry_v2(
    bridge: Any, execution_plan: Mapping[str, Any]
) -> VariableLaneRegistryV2:
    """Bind baselines and adaptive children to one bridge process."""

    plan = validate_execution_plan_v2(execution_plan)
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
        feedback = build_cat_gap_policy_v1(str(candidate_id), asdict(parameters))
        policy_row = policies[str(candidate_id)]
        if (
            type(feedback) is not Cat2NewFuryCatGapPolicyV1
            or feedback.policy_id != candidate_id
            or sha256_json(asdict(feedback.config)) != policy_row["profile_sha256"]
        ):
            raise FuryCatGapHpcWorkerV2Error(
                "adaptive candidate factory differs from the planned profile"
            )
        executors[str(candidate_id)] = Cat2NewFuryPairedLaneAdapterV3(
            bridge=bridge,
            feedback_policy=feedback,
            optimizer_parameters=asdict(feedback.config),
        )
    return VariableLaneRegistryV2(
        executors=executors,
        artifact_validators={
            CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
            CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
            CANDIDATE_PRODUCER_V2: validate_cat2new_fury_paired_artifact_v3,
        },
    )


def _producer_for(policy: Mapping[str, Any]) -> str:
    policy_id = str(policy.get("policy_id"))
    if policy_id == CAT_POLICY_ID:
        return CAT_V6_PRODUCER
    if policy_id == CONTRA260817_POLICY_ID:
        return CONTRA260817_V4_PRODUCER
    if policy.get("role") == "CANDIDATE":
        return CANDIDATE_PRODUCER_V2
    raise FuryCatGapHpcWorkerV2Error(f"unregistered lane policy: {policy_id}")


def _validate_registry(
    registry: VariableLaneRegistryV2, policy_ids: Sequence[str]
) -> VariableLaneRegistryV2:
    if set(registry.executors) != set(policy_ids) or not all(
        callable(value) for value in registry.executors.values()
    ):
        raise FuryCatGapHpcWorkerV2Error(
            "registry must contain exactly the planned adaptive policy lanes"
        )
    expected = {
        CAT_V6_PRODUCER,
        CONTRA260817_V4_PRODUCER,
        CANDIDATE_PRODUCER_V2,
    }
    if set(registry.artifact_validators) != expected or not all(
        callable(value) for value in registry.artifact_validators.values()
    ):
        raise FuryCatGapHpcWorkerV2Error(
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
        plan = validate_execution_plan_v2(execution_plan)
        dispatch = validate_dispatch_plan_v2(dispatch_plan, plan)
    except FuryCatGapStageTransitionV2Error as error:
        raise FuryCatGapHpcWorkerV2Error(str(error)) from error
    nodes = [row for row in dispatch["nodes"] if row["name"] == node_name]
    if len(nodes) != 1 or shard_index not in nodes[0]["shard_indices"]:
        raise FuryCatGapHpcWorkerV2Error("shard is not assigned to this node")
    shards = [row for row in plan["shards"] if row["shard_index"] == shard_index]
    if len(shards) != 1:
        raise FuryCatGapHpcWorkerV2Error("execution shard is not unique")
    task_by_id = {str(row["task_id"]): row for row in plan["lane_tasks"]}
    try:
        tasks = [dict(task_by_id[str(task_id)]) for task_id in shards[0]["task_ids"]]
    except KeyError as error:
        raise FuryCatGapHpcWorkerV2Error(
            f"shard refers to an unknown task: {error.args[0]}"
        ) from error
    contract = plan["runner_plan"]["contract"]
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
        raise FuryCatGapHpcWorkerV2Error(
            "candidate artifact differs from the updated registry identity"
        )
    decisions = artifact.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise FuryCatGapHpcWorkerV2Error("candidate artifact has no decisions")
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
            raise FuryCatGapHpcWorkerV2Error(
                "candidate decision metadata differs from the updated registry"
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
        "schema": ROLLOUT_ROW_SCHEMA_V2,
        "execution_plan_sha256": execution_plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "runtime_snapshot_sha256": execution_plan["runtime_snapshot_binding"][
            "snapshot_sha256"
        ],
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


def validate_rollout_row_v2(
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
        "schema", "execution_plan_sha256", "dispatch_plan_sha256",
        "runtime_snapshot_sha256", "shard_index", "task_identity",
        "group_identity", "scenario_identity", "policy_identity",
        "dynamic_load_identity", "producer", "artifact_schema",
        "artifact_sha256", "producer_runtime_receipt_sha256",
        "sufficient_statistics", "lane_result",
    }
    if set(row) != expected_fields or row.get("schema") != ROLLOUT_ROW_SCHEMA_V2:
        raise FuryCatGapHpcWorkerV2Error("v2 rollout row field set or schema mismatch")
    exact = {
        "execution_plan_sha256": execution_plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "runtime_snapshot_sha256": execution_plan["runtime_snapshot_binding"][
            "snapshot_sha256"
        ],
        "shard_index": shard_index,
        "task_identity": dict(task),
        "group_identity": _group_identity(group),
        "scenario_identity": _scenario_identity(scenario),
        "policy_identity": dict(policy),
        "dynamic_load_identity": _load_identity(group, scenario),
    }
    if any(row.get(field) != value for field, value in exact.items()):
        raise FuryCatGapHpcWorkerV2Error("v2 rollout identity differs from lane task")
    lane = _mapping(row.get("lane_result"), "lane result")
    producer = _producer_for(policy)
    if lane.get("producer") != producer or row.get("producer") != producer:
        raise FuryCatGapHpcWorkerV2Error("rollout producer differs from policy lane")
    try:
        validated = validate_lane_result_v4(
            lane,
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=artifact_validators.get(producer),
        )
    except FuryPairedRunnerV4Error as error:
        raise FuryCatGapHpcWorkerV2Error(str(error)) from error
    if policy.get("role") == "CANDIDATE":
        catalog = _candidate_design_by_id(execution_plan)
        _validate_candidate_profile(validated, policy, catalog[str(policy["policy_id"])])
    derived = {
        "artifact_schema": validated["artifact_schema"],
        "artifact_sha256": validated["artifact_sha256"],
        "producer_runtime_receipt_sha256": validated[
            "producer_runtime_receipt_sha256"
        ],
        "sufficient_statistics": _sufficient_statistics(validated),
    }
    if any(row.get(field) != value for field, value in derived.items()):
        raise FuryCatGapHpcWorkerV2Error(
            "rollout summary differs from validated producer artifact"
        )
    return dict(row)


def _iter_shard_rows(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
    registry: VariableLaneRegistryV2,
    throughput_capture: Any | None = None,
) -> tuple[JSONMap, JSONMap, JSONMap, Iterable[JSONMap]]:
    if os.environ.get("GOMAXPROCS") != "1":
        raise FuryCatGapHpcWorkerV2Error("v2 worker requires GOMAXPROCS=1")
    plan, dispatch, shard, tasks, groups, scenarios, policies = _shard_context(
        execution_plan,
        dispatch_plan,
        node_name=node_name,
        shard_index=shard_index,
    )
    registry = _validate_registry(registry, plan["policy_ids"])

    def rows() -> Iterable[JSONMap]:
        for task in tasks:
            group = groups[str(task["group_id"])]
            scenario = scenarios[(str(group["instance_id"]), str(group["scenario_id"]))]
            policy = policies[str(task["policy_id"])]
            if throughput_capture is not None:
                throughput_capture.begin_rollout()
            with (
                throughput_capture.phase("ROLLOUT")
                if throughput_capture is not None
                else nullcontext()
            ):
                raw = registry.executors[str(task["policy_id"])](
                    group=dict(group), scenario=dict(scenario), policy=dict(policy)
                )
            with (
                throughput_capture.phase("VALIDATION")
                if throughput_capture is not None
                else nullcontext()
            ):
                if not isinstance(raw, Mapping) or set(raw) != {"lane_result"}:
                    raise FuryCatGapHpcWorkerV2Error(
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
                validated = validate_rollout_row_v2(
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
            if throughput_capture is not None:
                throughput_capture.finish_rollout(validated["lane_result"])
            yield validated

    return plan, dispatch, shard, rows()


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
        "schema": SHARD_RECEIPT_SCHEMA_V2,
        "status": "COMPLETE_SIMULATOR_ONLY_NONVOTING",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "runtime_snapshot_sha256": plan["runtime_snapshot_binding"][
            "snapshot_sha256"
        ],
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


def _partial_document(
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    *,
    node_name: str,
    compact_rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    rows = [dict(row) for row in compact_rows]
    core = {
        "schema": SHARD_PARTIAL_SCHEMA_V2,
        "status": "COMPLETE_VALIDATED_SHARD_SUFFICIENT_STATISTICS",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "runtime_snapshot_sha256": plan["runtime_snapshot_binding"][
            "snapshot_sha256"
        ],
        "stage_id": plan["stage_id"],
        "node": node_name,
        "shard_index": shard["shard_index"],
        "task_ids_sha256": sha256_json(shard["task_ids"]),
        "task_count": len(rows),
        "task_rows": rows,
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


def execute_shard_v2(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
    registry: VariableLaneRegistryV2,
) -> tuple[JSONMap, list[JSONMap]]:
    """Execute one shard in memory for bounded tests or a local smoke."""

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


def run_shard_worker_v2(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    node_name: str,
    shard_index: int,
    registry: VariableLaneRegistryV2,
    output_directory: str | Path,
    throughput_capture: Any | None = None,
) -> JSONMap:
    """Write one v2 shard atomically and publish its receipt last."""

    with (
        throughput_capture.phase("VALIDATION")
        if throughput_capture is not None
        else nullcontext()
    ):
        plan, dispatch, shard, iterator = _iter_shard_rows(
            execution_plan,
            dispatch_plan,
            node_name=node_name,
            shard_index=shard_index,
            registry=registry,
            throughput_capture=throughput_capture,
        )
    root = Path(output_directory).expanduser().resolve()
    result_path = root / f"shards/shard-{shard_index:05d}.jsonl.gz"
    receipt_path = root / f"receipts/shard-{shard_index:05d}.json"
    partial_path = root / f"partials/shard-{shard_index:05d}.json"
    if result_path.exists() or receipt_path.exists() or partial_path.exists():
        raise FuryCatGapHpcWorkerV2Error(
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
            compressed = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
            try:
                for row in iterator:
                    with (
                        throughput_capture.phase("SERIALIZATION")
                        if throughput_capture is not None
                        else nullcontext()
                    ):
                        payload = _canonical_line(row)
                        logical.update(payload)
                        compressed.write(payload)
                        count += 1
                        completion_count += row["sufficient_statistics"]["completion_count"]
                        eligible_count += row["sufficient_statistics"][
                            "offline_score_eligible_count"
                        ]
                        compact_rows.append(_compact_partial_row(row))
                        if throughput_capture is not None:
                            throughput_capture.add_serialization_bytes(
                                json_bytes=len(payload)
                            )
            finally:
                with (
                    throughput_capture.phase("SERIALIZATION")
                    if throughput_capture is not None
                    else nullcontext()
                ):
                    compressed.close()
            with (
                throughput_capture.phase("SERIALIZATION")
                if throughput_capture is not None
                else nullcontext()
            ):
                raw.flush()
                os.fsync(raw.fileno())
        with (
            throughput_capture.phase("SERIALIZATION")
            if throughput_capture is not None
            else nullcontext()
        ):
            if count != shard["task_count"]:
                raise FuryCatGapHpcWorkerV2Error(
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
            partial = _partial_document(
                plan,
                dispatch,
                shard,
                node_name=node_name,
                compact_rows=compact_rows,
            )
            partial_payload = _canonical_line(partial)
            _atomic_write(partial_path, partial_payload)
            receipt["partial_sha256"] = partial["content_address"]["sha256"]
            receipt_payload = _canonical_line(receipt)
            if throughput_capture is not None:
                throughput_capture.add_serialization_bytes(
                    json_bytes=len(partial_payload) + len(receipt_payload)
                )
                throughput_capture.set_output_bytes(
                    compressed_bytes=result_path.stat().st_size,
                    output_bytes=(
                        result_path.stat().st_size
                        + len(partial_payload)
                        + len(receipt_payload)
                    ),
                )
            _atomic_write(receipt_path, receipt_payload)
        return receipt
    except Exception:
        if throughput_capture is not None:
            throughput_capture.mark_shard_failed()
        temporary.unlink(missing_ok=True)
        if not receipt_path.exists():
            result_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
        raise


def _validate_process_runtime_environment_v2(
    execution_plan: Mapping[str, Any],
    *,
    bridge_path: Path,
    bridge_cwd: Path,
) -> None:
    if os.environ.get("GOMAXPROCS") != "1":
        raise FuryCatGapHpcWorkerV2Error("v2 worker requires GOMAXPROCS=1")
    if execution_plan.get("execution_kind") != REAL_STAGE_EXECUTION_KIND_V1:
        return
    from .fury_cat_gap_formal_preparation_v1 import (
        EXPECTED_LINUX_V11_BRIDGE_SHA256,
        validate_exact_linux_runtime_closure_v1,
    )

    closure = validate_exact_linux_runtime_closure_v1(
        _mapping(execution_plan.get("runtime_closure"), "runtime closure")
    )
    expected_environment = {
        name: (
            "1"
            if name == "GOMAXPROCS"
            else str(Path.home().joinpath(*PurePosixPath(relative).parts))
        )
        for name, relative in closure["environment"].items()
    }
    if any(os.environ.get(name) != expected for name, expected in expected_environment.items()):
        raise FuryCatGapHpcWorkerV2Error(
            "worker process environment differs from the admitted closure"
        )
    expected_bridge = Path.home().joinpath(
        *PurePosixPath(
            f"{closure['shared_root']}/releases/dynamic-v5/"
            f"{EXPECTED_LINUX_V11_BRIDGE_SHA256}/o2obridge.linux-amd64"
        ).parts
    )
    if bridge_path != expected_bridge or bridge_cwd != expected_bridge.parent:
        raise FuryCatGapHpcWorkerV2Error(
            "worker bridge path differs from the admitted v11 runtime release"
        )


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapHpcWorkerV2Error(f"could not read {label}: {error}") from error
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
    parser.add_argument("--throughput-telemetry", type=Path)
    args = parser.parse_args(argv)
    bridge: SimulatorBridgeDynamicV3 | None = None
    capture: Any | None = None
    try:
        plan = validate_execution_plan_v2(_read_json(args.execution_plan, "execution plan"))
        dispatch = validate_dispatch_plan_v2(
            _read_json(args.dispatch_plan, "dispatch plan"), plan
        )
        bridge_path = args.bridge.expanduser().resolve()
        bridge_cwd = args.bridge_cwd.expanduser().resolve()
        _validate_process_runtime_environment_v2(
            plan, bridge_path=bridge_path, bridge_cwd=bridge_cwd
        )
        bridge = SimulatorBridgeDynamicV3(bridge_path, cwd=bridge_cwd)
        if args.throughput_telemetry is not None:
            from .fury_cat_gap_throughput_capture_v1 import (
                ShardThroughputCaptureV1,
            )

            capture = ShardThroughputCaptureV1(
                batch_id=(
                    f"{plan['stage_id']}:{args.node}:shard-{args.shard_index:05d}"
                ),
                node=args.node,
                shard_index=args.shard_index,
                workload_class=str(plan["stage_id"]),
                child_pids=(bridge._process.pid,),
                logical_cpu_capacity=1,
                input_bytes=(
                    args.execution_plan.expanduser().resolve().stat().st_size
                    + args.dispatch_plan.expanduser().resolve().stat().st_size
                ),
            )
        run_shard_worker_v2(
            plan,
            dispatch,
            node_name=args.node,
            shard_index=args.shard_index,
            registry=build_variable_lane_registry_v2(bridge, plan),
            output_directory=args.output_directory,
            throughput_capture=capture,
        )
        if capture is not None:
            from .fury_cat_gap_throughput_capture_v1 import (
                write_capture_document_v1,
            )

            write_capture_document_v1(args.throughput_telemetry, capture.finish())
    except Exception as error:
        if capture is not None and not capture.finished:
            try:
                from .fury_cat_gap_throughput_capture_v1 import (
                    write_capture_document_v1,
                )

                write_capture_document_v1(
                    args.throughput_telemetry, capture.finish()
                )
            except Exception as telemetry_error:
                print(f"telemetry error: {telemetry_error}", file=sys.stderr)
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        if bridge is not None:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "FuryCatGapHpcWorkerV2Error",
    "ROLLOUT_ROW_SCHEMA_V2",
    "SHARD_PARTIAL_SCHEMA_V2",
    "SHARD_RECEIPT_SCHEMA_V2",
    "VariableLaneRegistryV2",
    "build_variable_lane_registry_v2",
    "execute_shard_v2",
    "run_shard_worker_v2",
    "validate_rollout_row_v2",
)
