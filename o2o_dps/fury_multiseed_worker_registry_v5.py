"""Five-lane runner-v4 registry with runtime-bound deployed Contra v7.

This registry is additive to v4.  It keeps the same paired runner, policy
order, scenario, and simulator seed contracts, but the deployed-Contra Raid-A
lane now declares and executes the v7 producer from an explicit validated
runtime-binding JSON file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CAT2NEW_PRODUCER,
    Cat2NewFuryPairedLaneAdapterV3,
    cat2new_lane_contract_v3,
    validate_cat2new_fury_paired_artifact_v3,
)
from .cat_fury_paired_lane_adapter_v6 import (
    CAT_V6_PRODUCER,
    cat_runner_v4_lane_contract_v6,
    execute_cat_runner_v4_lane_v6,
    validate_cat_runner_v4_artifact_v6,
)
from .contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    contra260817_runner_v4_lane_contract_v4,
    execute_contra260817_runner_v4_lane_v4,
    validate_contra260817_runner_v4_artifact_v4,
)
from .deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    load_deployed_contra_runtime_binding_v1,
)
from .fury_dynamic_v5_deployed_contra_adapter_v7 import (
    DEPLOYED_CONTRA_V7_PRODUCER,
    execute_deployed_contra_v7_lane_v7,
    validate_deployed_contra_v7_artifact_v7,
)
from .fury_full_policy_rollout_v7 import ROLLOUT_SCHEMA_V7
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    LaneContractV4,
    execute_small_fixture_v4,
    validate_runner_plan,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    RAID_A_CONTROLLER,
    RAID_B_BLOCKER,
    RAID_B_CONTROLLER,
)
from .historical_behavior_clone_full_rollout_v1 import (
    POLICY_ID as BEHAVIOR_CLONE_POLICY_ID,
    PRODUCER as BEHAVIOR_CLONE_PRODUCER,
    HistoricalBehaviorCloneRunnerV4ExecutorV1,
    behavior_clone_lane_contract_v1,
    validate_behavior_clone_dynamic_v5_rollout_v1,
)


JSONMap = dict[str, Any]
REGISTRY_SCHEMA_V5 = "fury_multiseed_dynamic_v5_worker_registry/v5"
ALLOWED_POLICY_IDS_V5 = (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CAT2NEW_POLICY_ID,
    BEHAVIOR_CLONE_POLICY_ID,
)
EXPECTED_PRODUCERS_V5 = {
    CAT_POLICY_ID: CAT_V6_PRODUCER,
    CONTRA_DEPLOYED_POLICY_ID: DEPLOYED_CONTRA_V7_PRODUCER,
    CONTRA260817_POLICY_ID: CONTRA260817_V4_PRODUCER,
    CAT2NEW_POLICY_ID: CAT2NEW_PRODUCER,
    BEHAVIOR_CLONE_POLICY_ID: BEHAVIOR_CLONE_PRODUCER,
}
UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5 = {
    RAID_B_CONTROLLER: RAID_B_BLOCKER,
}


class FuryMultiseedWorkerRegistryV5Error(RuntimeError):
    """The runtime-bound five-lane plan or registry is inconsistent."""


@dataclass(frozen=True)
class RuntimeBoundDiagnosticLaneRegistryV5:
    schema: str
    executors: Mapping[str, Callable[..., Mapping[str, Any]]]
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]]
    behavior_clone_model_sha256: str
    deployed_contra_runtime_binding_sha256: str
    deployed_contra_loaded_source_closure_sha256: str
    deployed_contra_runtime_snapshot_sha256: str
    unimplemented_controller_blockers: Mapping[str, str]


def deployed_contra_runner_v4_lane_contract_v7() -> JSONMap:
    """Declare the exact runtime-bound Raid-A producer admitted by v5."""

    return LaneContractV4(
        policy_id=CONTRA_DEPLOYED_POLICY_ID,
        producer=DEPLOYED_CONTRA_V7_PRODUCER,
        artifact_schema=ROLLOUT_SCHEMA_V7,
        source_oracle_status="CONTRA_DEPLOYED_LOADED_SOURCE_BOUND_V1_READY",
        ordered_sink_status="CONTRA_DEPLOYED_ORDERED_SINK_V4_RAID_A_READY",
        full_policy_status="CONTRA_DEPLOYED_RUNTIME_BOUND_V7_RAID_A_ONLY",
        dynamic_v5_executable=True,
        blocker_codes=(),
    ).to_wire()


def runtime_bound_five_lane_contracts_v5() -> tuple[JSONMap, ...]:
    """Return the v4 policy order with only deployed Contra promoted to v7."""

    return (
        cat_runner_v4_lane_contract_v6(),
        deployed_contra_runner_v4_lane_contract_v7(),
        contra260817_runner_v4_lane_contract_v4(),
        cat2new_lane_contract_v3(),
        behavior_clone_lane_contract_v1(),
    )


def build_runtime_bound_diagnostic_lane_registry_v5(
    bridge: Any,
    cat2_feedback_policy: Any,
    *,
    behavior_clone_model_path: str | Path,
    deployed_contra_runtime_binding_path: str | Path,
    optimizer_parameters: Mapping[str, Any] | None = None,
) -> RuntimeBoundDiagnosticLaneRegistryV5:
    """Load the explicit binding and bind all five producers to one bridge."""

    try:
        binding = load_deployed_contra_runtime_binding_v1(
            deployed_contra_runtime_binding_path
        )
    except DeployedContraRuntimeBindingError as error:
        raise FuryMultiseedWorkerRegistryV5Error(str(error)) from error
    cat2_executor = Cat2NewFuryPairedLaneAdapterV3(
        bridge=bridge,
        feedback_policy=cat2_feedback_policy,
        optimizer_parameters=dict(optimizer_parameters or {}),
    )
    clone_executor = HistoricalBehaviorCloneRunnerV4ExecutorV1(
        behavior_clone_model_path,
        lambda **_: bridge,
    )

    def deployed_contra_executor(
        *,
        group: Mapping[str, Any],
        scenario: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        envelope = execute_deployed_contra_v7_lane_v7(
            bridge,
            group=group,
            scenario=scenario,
            policy=policy,
            runtime_binding=binding,
            controller=RAID_A_CONTROLLER,
        )
        if set(envelope) != {"lane_result", "lane_cache_identity"}:
            raise FuryMultiseedWorkerRegistryV5Error(
                "deployed-Contra v7 executor envelope is malformed"
            )
        lane = _mapping(envelope["lane_result"], "deployed-Contra v7 lane")
        artifact = _mapping(lane.get("artifact"), "deployed-Contra v7 artifact")
        if envelope["lane_cache_identity"] != artifact.get(
            "lane_cache_identity"
        ):
            raise FuryMultiseedWorkerRegistryV5Error(
                "deployed-Contra v7 cache identity was lost at the registry boundary"
            )
        return {"lane_result": lane}

    def clone_validator(
        artifact: Mapping[str, Any],
        runtime_receipt: Mapping[str, Any],
        dynamic_load: Any,
    ) -> Mapping[str, Any]:
        return validate_behavior_clone_dynamic_v5_rollout_v1(
            artifact,
            runtime_receipt,
            dynamic_load,
            expected_model_binding=clone_executor.binding,
        )

    registry = RuntimeBoundDiagnosticLaneRegistryV5(
        schema=REGISTRY_SCHEMA_V5,
        executors={
            CAT_POLICY_ID: lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
                bridge, group=group, scenario=scenario, policy=policy
            ),
            CONTRA_DEPLOYED_POLICY_ID: deployed_contra_executor,
            CONTRA260817_POLICY_ID: (
                lambda *, group, scenario, policy: execute_contra260817_runner_v4_lane_v4(
                    bridge, group=group, scenario=scenario, policy=policy
                )
            ),
            CAT2NEW_POLICY_ID: cat2_executor,
            BEHAVIOR_CLONE_POLICY_ID: clone_executor,
        },
        artifact_validators={
            CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
            DEPLOYED_CONTRA_V7_PRODUCER: validate_deployed_contra_v7_artifact_v7,
            CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
            CAT2NEW_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
            BEHAVIOR_CLONE_PRODUCER: clone_validator,
        },
        behavior_clone_model_sha256=str(clone_executor.binding["model_sha256"]),
        deployed_contra_runtime_binding_sha256=str(binding["binding_sha256"]),
        deployed_contra_loaded_source_closure_sha256=str(
            binding["loaded_source_closure_sha256"]
        ),
        deployed_contra_runtime_snapshot_sha256=str(
            binding["runtime_snapshot_sha256"]
        ),
        unimplemented_controller_blockers=dict(
            UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5
        ),
    )
    return validate_runtime_bound_diagnostic_lane_registry_v5(registry)


def validate_runtime_bound_diagnostic_lane_registry_v5(
    registry: RuntimeBoundDiagnosticLaneRegistryV5,
) -> RuntimeBoundDiagnosticLaneRegistryV5:
    if not isinstance(registry, RuntimeBoundDiagnosticLaneRegistryV5) or (
        registry.schema != REGISTRY_SCHEMA_V5
    ):
        raise FuryMultiseedWorkerRegistryV5Error("worker registry schema mismatch")
    if tuple(registry.executors) != ALLOWED_POLICY_IDS_V5:
        raise FuryMultiseedWorkerRegistryV5Error(
            "registry must preserve the five-lane v4 policy order"
        )
    expected_validators = set(EXPECTED_PRODUCERS_V5.values())
    if set(registry.artifact_validators) != expected_validators:
        raise FuryMultiseedWorkerRegistryV5Error(
            "registry producer-validator set is incomplete"
        )
    if not all(callable(value) for value in registry.executors.values()) or not all(
        callable(value) for value in registry.artifact_validators.values()
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "registry executors and validators must be callable"
        )
    for field, value in (
        ("behavior-clone model", registry.behavior_clone_model_sha256),
        (
            "deployed-Contra runtime binding",
            registry.deployed_contra_runtime_binding_sha256,
        ),
        (
            "deployed-Contra source closure",
            registry.deployed_contra_loaded_source_closure_sha256,
        ),
        (
            "deployed-Contra runtime snapshot",
            registry.deployed_contra_runtime_snapshot_sha256,
        ),
    ):
        _lower_sha256(value, field)
    if dict(registry.unimplemented_controller_blockers) != (
        UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "registry must retain the explicit Raid-B blocker"
        )
    return registry


def validate_runtime_bound_five_lane_plan_v5(
    plan: Mapping[str, Any],
    registry: RuntimeBoundDiagnosticLaneRegistryV5,
) -> JSONMap:
    """Validate plan-level producer and runtime identities before execution."""

    checked = validate_runner_plan(plan)
    registry = validate_runtime_bound_diagnostic_lane_registry_v5(registry)
    contract = checked["contract"]
    if tuple(contract.get("policy_ids", ())) != ALLOWED_POLICY_IDS_V5:
        raise FuryMultiseedWorkerRegistryV5Error(
            "runner plan must contain the exact runtime-bound five-lane order"
        )
    if contract.get("lane_contracts") != list(
        runtime_bound_five_lane_contracts_v5()
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "runner plan does not declare the deployed-Contra v7 producer"
        )
    policies = {row.get("policy_id"): row for row in contract.get("policies", [])}
    deployed = policies.get(CONTRA_DEPLOYED_POLICY_ID)
    if (
        not isinstance(deployed, Mapping)
        or deployed.get("role") != "BASELINE"
        or deployed.get("source_sha256")
        != registry.deployed_contra_loaded_source_closure_sha256
        or deployed.get("profile_sha256")
        != registry.deployed_contra_runtime_binding_sha256
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "deployed-Contra policy is not bound to the registry source/runtime identity"
        )
    clone = policies.get(BEHAVIOR_CLONE_POLICY_ID)
    if (
        not isinstance(clone, Mapping)
        or clone.get("role") != "BASELINE_CANDIDATE_NONVOTING"
        or clone.get("source_sha256") != registry.behavior_clone_model_sha256
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "pooled clone identity or non-voting role differs from the registry"
        )
    execution_bundle = _mapping(
        contract.get("execution_bundle_identity"), "execution bundle"
    )
    if execution_bundle.get("runtime_snapshot_sha256") != (
        registry.deployed_contra_runtime_snapshot_sha256
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "runner execution bundle is not bound to the deployed-Contra runtime snapshot"
        )
    if contract.get("status") != "READY_FOR_SMALL_FIXTURE":
        raise FuryMultiseedWorkerRegistryV5Error(
            "runtime-bound five-lane plan is not ready for a small fixture"
        )
    return checked


def execute_registered_small_fixture_v5(
    plan: Mapping[str, Any],
    registry: RuntimeBoundDiagnosticLaneRegistryV5,
) -> JSONMap:
    """Execute one matched group through all five explicitly bound lanes."""

    checked = validate_runtime_bound_five_lane_plan_v5(plan, registry)
    registry = validate_runtime_bound_diagnostic_lane_registry_v5(registry)
    policies = {row["policy_id"]: row for row in checked["contract"]["policies"]}
    if policies[BEHAVIOR_CLONE_POLICY_ID]["source_sha256"] != (
        registry.behavior_clone_model_sha256
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "runner plan clone identity differs from the worker model path"
        )

    def route(
        *,
        group: Mapping[str, Any],
        scenario: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        policy_id = str(policy.get("policy_id"))
        executor = registry.executors.get(policy_id)
        if executor is None:
            raise FuryMultiseedWorkerRegistryV5Error(
                f"no registered executor for {policy_id}"
            )
        return executor(group=group, scenario=scenario, policy=policy)

    result = execute_small_fixture_v4(
        checked,
        route,
        artifact_validators=registry.artifact_validators,
    )
    deployed = next(
        row
        for row in result["results"]
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    artifact = _mapping(deployed.get("artifact"), "deployed-Contra v7 artifact")
    cache = _mapping(
        artifact.get("lane_cache_identity"), "deployed-Contra v7 cache identity"
    )
    coverage = _mapping(
        artifact.get("controller_coverage"), "deployed-Contra controller coverage"
    )
    if (
        deployed.get("producer") != DEPLOYED_CONTRA_V7_PRODUCER
        or cache.get("runtime_binding_sha256")
        != registry.deployed_contra_runtime_binding_sha256
        or coverage.get("unimplemented")
        != UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5
        or result.get("comparison_ready") is not False
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            "runtime-bound fixture lost its v7 identity or authority boundary"
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryMultiseedWorkerRegistryV5Error(f"{label} must be an object")
    return value


def _lower_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FuryMultiseedWorkerRegistryV5Error(
            f"{label} identity must be a lowercase SHA-256"
        )
    return value


__all__ = (
    "ALLOWED_POLICY_IDS_V5",
    "BEHAVIOR_CLONE_POLICY_ID",
    "EXPECTED_PRODUCERS_V5",
    "FuryMultiseedWorkerRegistryV5Error",
    "REGISTRY_SCHEMA_V5",
    "RuntimeBoundDiagnosticLaneRegistryV5",
    "UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5",
    "build_runtime_bound_diagnostic_lane_registry_v5",
    "deployed_contra_runner_v4_lane_contract_v7",
    "execute_registered_small_fixture_v5",
    "runtime_bound_five_lane_contracts_v5",
    "validate_runtime_bound_diagnostic_lane_registry_v5",
    "validate_runtime_bound_five_lane_plan_v5",
)
