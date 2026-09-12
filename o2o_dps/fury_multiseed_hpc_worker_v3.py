"""Native dynamic-v3 worker for the four-lane development diagnostic."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CAT2NEW_PRODUCER,
    Cat2NewFuryPairedLaneAdapterV3,
    validate_cat2new_fury_paired_artifact_v3,
)
from .cat2new_fury_parametric_policy_v1 import Cat2NewFuryParametricPolicyV1
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
from .fury_dynamic_v5_baseline_adapter_v4 import execute_dynamic_v5_baseline_v4
from .fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    DEVELOPMENT_EXECUTION_KIND_V3,
    EXPECTED_PRODUCERS_V3,
    FuryMultiseedHpcDispatchV3Error,
    validate_dispatch_plan_v3,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    FuryPairedRunnerV4Error,
    sha256_json,
    validate_lane_result_v4,
    validate_runner_plan,
)
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


JSONMap = dict[str, Any]
ROLLOUT_ROW_SCHEMA_V3 = "fury_multiseed_dynamic_v5_rollout_row/v3"
GROUP_RECEIPT_SCHEMA_V3 = "fury_multiseed_dynamic_v5_group_receipt/v3"


class FuryMultiseedHpcWorkerV3Error(RuntimeError):
    """A four-lane worker registry, rollout, or output is invalid."""


@dataclass(frozen=True)
class NativeDiagnosticLaneRegistryV3:
    executors: Mapping[str, Callable[..., Mapping[str, Any]]]
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]]


def build_native_diagnostic_lane_registry_v3(
    bridge: Any,
    cat2_feedback_policy: Any,
    *,
    optimizer_parameters: Mapping[str, Any] | None = None,
) -> NativeDiagnosticLaneRegistryV3:
    """Bind all four real producers to the same worker-owned bridge."""

    cat2_executor = Cat2NewFuryPairedLaneAdapterV3(
        bridge=bridge,
        feedback_policy=cat2_feedback_policy,
        optimizer_parameters=dict(optimizer_parameters or {}),
    )

    def deployed_contra_executor(
        *, group: Mapping[str, Any], scenario: Mapping[str, Any], policy: Mapping[str, Any]
    ) -> JSONMap:
        return execute_dynamic_v5_baseline_v4(
            bridge, group=group, scenario=scenario, policy=policy
        )

    def contra260817_executor(
        *, group: Mapping[str, Any], scenario: Mapping[str, Any], policy: Mapping[str, Any]
    ) -> JSONMap:
        return execute_contra260817_runner_v4_lane_v4(
            bridge, group=group, scenario=scenario, policy=policy
        )

    return NativeDiagnosticLaneRegistryV3(
        executors={
            CAT_POLICY_ID: lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
                bridge, group=group, scenario=scenario, policy=policy
            ),
            CONTRA_DEPLOYED_POLICY_ID: deployed_contra_executor,
            CONTRA260817_POLICY_ID: contra260817_executor,
            CAT2NEW_POLICY_ID: cat2_executor,
        },
        artifact_validators={
            CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
            CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
            CAT2NEW_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
        },
    )


def _validate_registry(
    registry: NativeDiagnosticLaneRegistryV3,
) -> NativeDiagnosticLaneRegistryV3:
    if set(registry.executors) != set(ALLOWED_POLICY_IDS_V3):
        raise FuryMultiseedHpcWorkerV3Error(
            "registry must contain exactly Cat, deployed Contra, Contra260817, and Cat2_new"
        )
    expected_validators = {
        CAT_V6_PRODUCER,
        CONTRA260817_V4_PRODUCER,
        CAT2NEW_PRODUCER,
    }
    if set(registry.artifact_validators) != expected_validators:
        raise FuryMultiseedHpcWorkerV3Error(
            "native Cat, Contra260817, and Cat2_new producer validators are required"
        )
    if not all(callable(value) for value in registry.executors.values()) or not all(
        callable(value) for value in registry.artifact_validators.values()
    ):
        raise FuryMultiseedHpcWorkerV3Error("registry entries must be callable")
    return registry


def _group_context(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
) -> tuple[JSONMap, JSONMap, JSONMap, JSONMap, dict[str, JSONMap]]:
    try:
        plan = validate_runner_plan(runner_plan)
        checked_dispatch = validate_dispatch_plan_v3(dispatch, plan)
    except (FuryPairedRunnerV4Error, FuryMultiseedHpcDispatchV3Error) as error:
        raise FuryMultiseedHpcWorkerV3Error(str(error)) from error
    nodes = [row for row in checked_dispatch["nodes"] if row["name"] == node_name]
    if len(nodes) != 1 or group_id not in nodes[0]["group_ids"]:
        raise FuryMultiseedHpcWorkerV3Error(
            "paired group is not assigned to this dispatch node"
        )
    contract = plan["contract"]
    groups = [row for row in contract["groups"] if row["group_id"] == group_id]
    if len(groups) != 1:
        raise FuryMultiseedHpcWorkerV3Error("paired group is not unique")
    group = groups[0]
    scenarios = [
        row
        for row in contract["scenarios"]
        if row["instance_id"] == group["instance_id"]
        and row["scenario_id"] == group["scenario_id"]
    ]
    if len(scenarios) != 1:
        raise FuryMultiseedHpcWorkerV3Error("group scenario is not unique")
    policies = {row["policy_id"]: row for row in contract["policies"]}
    if tuple(contract["policy_ids"]) != ALLOWED_POLICY_IDS_V3 or set(policies) != set(
        ALLOWED_POLICY_IDS_V3
    ):
        raise FuryMultiseedHpcWorkerV3Error("worker policy set is not admitted")
    return plan, checked_dispatch, group, scenarios[0], policies


def _scenario_identity(scenario: Mapping[str, Any]) -> JSONMap:
    fields = (
        "instance_id",
        "component_id",
        "scenario_id",
        "stratum",
        "horizon_ms",
        "scenario_contract_sha256",
        "scenario_model_sha256",
        "target_context_bundle_sha256",
        "corpus_entry_sha256",
        "source_scenario_sha256",
        "catalog_sha256",
    )
    return {field: scenario[field] for field in fields}


def _group_identity(group: Mapping[str, Any]) -> JSONMap:
    fields = (
        "group_id",
        "protocol_sha256",
        "corpus_manifest_sha256",
        "phase",
        "instance_id",
        "scenario_id",
        "scenario_contract_sha256",
        "master_seed",
        "simulator_seed",
    )
    return {field: group[field] for field in fields}


def _load_identity(group: Mapping[str, Any], scenario: Mapping[str, Any]) -> JSONMap:
    return {
        "request_sha256": scenario["request_sha256"],
        "simulator_seed": group["simulator_seed"],
        "dynamic_load_contract_sha256": group["dynamic_load_contract_sha256"],
        "dynamic_v5_binding": group["dynamic_v5_binding"],
    }


def _sufficient_statistics(lane: Mapping[str, Any]) -> JSONMap:
    dps = float(lane["dps"])
    if not math.isfinite(dps):
        raise FuryMultiseedHpcWorkerV3Error("lane dps must be finite")
    return {
        "rollout_count": 1,
        "damage_sum": float(lane["damage"]),
        "elapsed_ms_sum": int(lane["elapsed_ms"]),
        "dps_sum": dps,
        "dps_squared_sum": dps * dps,
        "completion_count": int(lane["completion_criterion_met"] is True),
        "offline_score_eligible_count": int(lane["offline_score_eligible"] is True),
        "omitted_lane_count_sum": int(lane["omitted_lane_count"]),
        "fatal_error_count_sum": int(lane["fatal_error_count"]),
    }


def _validate_candidate_profile_binding_v3(
    lane: Mapping[str, Any], policy: Mapping[str, Any]
) -> None:
    if policy.get("policy_id") != CAT2NEW_POLICY_ID:
        return
    artifact = lane.get("artifact")
    if not isinstance(artifact, Mapping):
        raise FuryMultiseedHpcWorkerV3Error("Cat2_new artifact must be an object")
    identity = artifact.get("identity")
    context = artifact.get("policy_context")
    if not isinstance(identity, Mapping) or not isinstance(context, Mapping):
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2_new optimizer identity/context is missing"
        )
    parameters = context.get("optimizer_parameters")
    if not isinstance(parameters, Mapping):
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2_new optimizer parameters are missing"
        )
    expected = policy.get("profile_sha256")
    if (
        not isinstance(expected, str)
        or sha256_json(dict(parameters)) != expected
        or identity.get("optimizer_parameters_sha256") != expected
    ):
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2_new runtime parameters differ from planned profile identity"
        )
    controller_parameters = parameters.get("parameters")
    decisions = artifact.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2_new artifact has no bound policy decisions"
        )
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise FuryMultiseedHpcWorkerV3Error(
                "Cat2_new decision is not an object"
            )
        intent = decision.get("policy_intent")
        metadata = intent.get("policy_metadata") if isinstance(intent, Mapping) else None
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("parameters") != controller_parameters
        ):
            raise FuryMultiseedHpcWorkerV3Error(
                "Cat2_new decision parameters differ from planned profile"
            )


def _validate_paired_horizon_elapsed_v3(rows: Sequence[Mapping[str, Any]]) -> None:
    elapsed = {
        int(lane["elapsed_ms"])
        for row in rows
        if isinstance((lane := row.get("lane_result")), Mapping)
        and lane.get("completion_mode") == "SCENARIO_HORIZON_REACHED"
    }
    if len(elapsed) > 1:
        raise FuryMultiseedHpcWorkerV3Error(
            "paired horizon lanes use different policy-active elapsed times"
        )


def _rollout_row(
    *,
    plan: Mapping[str, Any],
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
    lane: Mapping[str, Any],
) -> JSONMap:
    return {
        "schema": ROLLOUT_ROW_SCHEMA_V3,
        "runner_plan_sha256": plan["plan_sha256"],
        "group_identity": _group_identity(group),
        "scenario_identity": _scenario_identity(scenario),
        "policy_identity": dict(policy),
        "dynamic_load_identity": _load_identity(group, scenario),
        "producer": lane["producer"],
        "artifact_schema": lane["artifact_schema"],
        "artifact_sha256": lane["artifact_sha256"],
        "producer_runtime_receipt_sha256": lane["producer_runtime_receipt_sha256"],
        "sufficient_statistics": _sufficient_statistics(lane),
        "lane_result": dict(lane),
    }


def validate_rollout_row_v3(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]],
) -> JSONMap:
    expected_fields = {
        "schema",
        "runner_plan_sha256",
        "group_identity",
        "scenario_identity",
        "policy_identity",
        "dynamic_load_identity",
        "producer",
        "artifact_schema",
        "artifact_sha256",
        "producer_runtime_receipt_sha256",
        "sufficient_statistics",
        "lane_result",
    }
    if set(row) != expected_fields or row.get("schema") != ROLLOUT_ROW_SCHEMA_V3:
        raise FuryMultiseedHpcWorkerV3Error("rollout row field set or schema mismatch")
    exact = {
        "runner_plan_sha256": plan["plan_sha256"],
        "group_identity": _group_identity(group),
        "scenario_identity": _scenario_identity(scenario),
        "policy_identity": dict(policy),
        "dynamic_load_identity": _load_identity(group, scenario),
    }
    for field, expected in exact.items():
        if row.get(field) != expected:
            raise FuryMultiseedHpcWorkerV3Error(f"rollout row {field} mismatch")
    lane = row.get("lane_result")
    if not isinstance(lane, Mapping):
        raise FuryMultiseedHpcWorkerV3Error("rollout lane_result must be an object")
    producer = EXPECTED_PRODUCERS_V3[str(policy["policy_id"])]
    if lane.get("producer") != producer:
        raise FuryMultiseedHpcWorkerV3Error(
            "rollout producer differs from the registered policy lane"
        )
    try:
        validated = validate_lane_result_v4(
            lane,
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=artifact_validators.get(producer),
        )
    except FuryPairedRunnerV4Error as error:
        raise FuryMultiseedHpcWorkerV3Error(str(error)) from error
    _validate_candidate_profile_binding_v3(validated, policy)
    derived = {
        "producer": validated["producer"],
        "artifact_schema": validated["artifact_schema"],
        "artifact_sha256": validated["artifact_sha256"],
        "producer_runtime_receipt_sha256": validated[
            "producer_runtime_receipt_sha256"
        ],
        "sufficient_statistics": _sufficient_statistics(validated),
    }
    for field, expected in derived.items():
        if row.get(field) != expected:
            raise FuryMultiseedHpcWorkerV3Error(
                f"rollout row {field} differs from validated lane result"
            )
    return dict(row)


def execute_group_v3(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
    registry: NativeDiagnosticLaneRegistryV3,
) -> tuple[JSONMap, list[JSONMap]]:
    """Execute the four registered policies for one indivisible paired group."""

    if os.environ.get("GOMAXPROCS") != "1":
        raise FuryMultiseedHpcWorkerV3Error("worker requires GOMAXPROCS=1")
    registry = _validate_registry(registry)
    plan, checked_dispatch, group, scenario, policies = _group_context(
        runner_plan, dispatch, node_name=node_name, group_id=group_id
    )
    rows: list[JSONMap] = []
    for policy_id in ALLOWED_POLICY_IDS_V3:
        raw = registry.executors[policy_id](
            group=dict(group), scenario=dict(scenario), policy=dict(policies[policy_id])
        )
        if not isinstance(raw, Mapping) or set(raw) != {"lane_result"}:
            raise FuryMultiseedHpcWorkerV3Error(
                "registered executor must return only lane_result"
            )
        lane = raw["lane_result"]
        if not isinstance(lane, Mapping):
            raise FuryMultiseedHpcWorkerV3Error("lane_result must be an object")
        row = _rollout_row(
            plan=plan,
            group=group,
            scenario=scenario,
            policy=policies[policy_id],
            lane=lane,
        )
        rows.append(
            validate_rollout_row_v3(
                row,
                plan=plan,
                group=group,
                scenario=scenario,
                policy=policies[policy_id],
                artifact_validators=registry.artifact_validators,
            )
        )
    _validate_paired_horizon_elapsed_v3(rows)
    receipt = {
        "schema": GROUP_RECEIPT_SCHEMA_V3,
        "status": "COMPLETE_SIMULATOR_ONLY_NONVOTING",
        "runner_plan_sha256": plan["plan_sha256"],
        "execution_kind": checked_dispatch["execution_kind"],
        "node": node_name,
        "group_id": group_id,
        "master_seed": group["master_seed"],
        "simulator_seed": group["simulator_seed"],
        "scenario_contract_sha256": group["scenario_contract_sha256"],
        "dynamic_load_contract_sha256": group["dynamic_load_contract_sha256"],
        "policy_ids": list(ALLOWED_POLICY_IDS_V3),
        "result_count": len(rows),
        "completion_count": sum(
            row["sufficient_statistics"]["completion_count"] for row in rows
        ),
        "offline_score_eligible_count": sum(
            row["sufficient_statistics"]["offline_score_eligible_count"]
            for row in rows
        ),
        "result_file": f"groups/{group_id}.jsonl",
        "gomaxprocs": 1,
        "heavy_execution_started": checked_dispatch["execution_kind"]
        == DEVELOPMENT_EXECUTION_KIND_V3,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }
    return receipt, rows


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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


def run_group_worker_v3(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
    registry: NativeDiagnosticLaneRegistryV3,
    output_directory: str | Path,
) -> JSONMap:
    receipt, rows = execute_group_v3(
        runner_plan,
        dispatch,
        node_name=node_name,
        group_id=group_id,
        registry=registry,
    )
    root = Path(output_directory).expanduser().resolve()
    result_path = root / receipt["result_file"]
    receipt_path = root / "receipts" / f"{group_id}.json"
    if result_path.exists() or receipt_path.exists():
        raise FuryMultiseedHpcWorkerV3Error(
            "group output already exists; inspect it before retrying"
        )
    result_payload = ("\n".join(_canonical_json(row) for row in rows) + "\n").encode(
        "utf-8"
    )
    _atomic_write(result_path, result_payload)
    try:
        _atomic_write(receipt_path, (_canonical_json(receipt) + "\n").encode("utf-8"))
    except Exception:
        result_path.unlink(missing_ok=True)
        raise
    return receipt


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcWorkerV3Error(f"could not read {label}: {error}") from error
    if not isinstance(value, dict):
        raise FuryMultiseedHpcWorkerV3Error(f"{label} must be an object")
    return value


def _load_factory(specification: str) -> Callable[[], Any]:
    module_name, separator, attribute = specification.partition(":")
    if not separator or not module_name or not attribute:
        raise FuryMultiseedHpcWorkerV3Error(
            "cat2 policy factory must be MODULE:CALLABLE"
        )
    try:
        value = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise FuryMultiseedHpcWorkerV3Error(
            f"could not import Cat2_new policy factory: {error}"
        ) from error
    if not callable(value):
        raise FuryMultiseedHpcWorkerV3Error("Cat2_new policy factory is not callable")
    return value


def _bind_cat2_factory_to_plan_v3(
    feedback_policy: Any, plan: Mapping[str, Any]
) -> JSONMap:
    if type(feedback_policy) is not Cat2NewFuryParametricPolicyV1:
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2 policy factory must return Cat2NewFuryParametricPolicyV1"
        )
    config = getattr(feedback_policy, "config", None)
    if not is_dataclass(config):
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2 policy config must be a dataclass"
        )
    parameters = asdict(config)
    policies = plan.get("contract", {}).get("policies")
    if not isinstance(policies, list):
        raise FuryMultiseedHpcWorkerV3Error("runner plan policies are missing")
    candidates = [
        row
        for row in policies
        if isinstance(row, Mapping) and row.get("policy_id") == CAT2NEW_POLICY_ID
    ]
    if len(candidates) != 1:
        raise FuryMultiseedHpcWorkerV3Error(
            "runner plan must contain one Cat2_new candidate"
        )
    planned = candidates[0]
    source_file = inspect.getsourcefile(type(feedback_policy))
    adapter_file = inspect.getsourcefile(Cat2NewFuryPairedLaneAdapterV3)
    if source_file is None or adapter_file is None:
        raise FuryMultiseedHpcWorkerV3Error(
            "Cat2 policy or adapter source file is unavailable"
        )
    observed = {
        "source_sha256": hashlib.sha256(Path(source_file).read_bytes()).hexdigest(),
        "adapter_sha256": hashlib.sha256(Path(adapter_file).read_bytes()).hexdigest(),
        "profile_sha256": sha256_json(parameters),
    }
    for field, value in observed.items():
        if planned.get(field) != value:
            raise FuryMultiseedHpcWorkerV3Error(
                f"Cat2 policy factory {field} differs from runner plan"
            )
    return parameters


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--bridge-cwd", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--cat2-policy-factory", required=True)
    args = parser.parse_args(argv)
    try:
        if os.environ.get("GOMAXPROCS") != "1":
            raise FuryMultiseedHpcWorkerV3Error("worker requires GOMAXPROCS=1")
        runner_plan = _load_json(args.runner_plan, "runner plan")
        dispatch = _load_json(args.dispatch_plan, "dispatch plan")
        validated = validate_runner_plan(runner_plan)
        bridge_path = args.bridge.expanduser().resolve()
        try:
            observed_bridge_sha = hashlib.sha256(bridge_path.read_bytes()).hexdigest()
        except OSError as error:
            raise FuryMultiseedHpcWorkerV3Error(
                f"could not read bridge binary: {error}"
            ) from error
        expected_bridge_sha = validated["contract"]["bridge_identity"]["sha256"]
        if observed_bridge_sha != expected_bridge_sha:
            raise FuryMultiseedHpcWorkerV3Error(
                "bridge binary differs from runner-v4 plan identity"
            )
        feedback_policy = _load_factory(args.cat2_policy_factory)()
        optimizer_parameters = _bind_cat2_factory_to_plan_v3(
            feedback_policy, validated
        )
        with SimulatorBridgeDynamicV3(bridge_path, cwd=args.bridge_cwd) as bridge:
            receipt = run_group_worker_v3(
                runner_plan,
                dispatch,
                node_name=args.node,
                group_id=args.group_id,
                registry=build_native_diagnostic_lane_registry_v3(
                    bridge,
                    feedback_policy,
                    optimizer_parameters=optimizer_parameters,
                ),
                output_directory=args.output_directory,
            )
        print(_canonical_json(receipt))
        return 0
    except (FuryMultiseedHpcWorkerV3Error, FuryPairedRunnerV4Error) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "GROUP_RECEIPT_SCHEMA_V3",
    "FuryMultiseedHpcWorkerV3Error",
    "NativeDiagnosticLaneRegistryV3",
    "ROLLOUT_ROW_SCHEMA_V3",
    "build_native_diagnostic_lane_registry_v3",
    "execute_group_v3",
    "run_group_worker_v3",
    "validate_rollout_row_v3",
)
