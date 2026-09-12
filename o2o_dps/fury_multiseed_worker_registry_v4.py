"""Additive five-lane worker registry with a pooled behavior-clone lane.

The existing four-lane v3 worker remains frozen.  This registry adds the
explicitly bound pooled clean-Fury clone as a fifth, non-voting simulator lane
and routes all lanes through runner-v4's common one-group receipt validator.
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
from .fury_dynamic_v5_baseline_adapter_v4 import execute_dynamic_v5_baseline_v4
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    FURY_V5_PRODUCER,
    execute_small_fixture_v4,
    builtin_lane_contracts_v4,
    validate_runner_plan,
)
from .historical_behavior_clone_full_rollout_v1 import (
    POLICY_ID as BEHAVIOR_CLONE_POLICY_ID,
    PRODUCER as BEHAVIOR_CLONE_PRODUCER,
    HistoricalBehaviorCloneRunnerV4ExecutorV1,
    behavior_clone_lane_contract_v1,
    validate_behavior_clone_dynamic_v5_rollout_v1,
)


JSONMap = dict[str, Any]
REGISTRY_SCHEMA_V4 = "fury_multiseed_dynamic_v5_worker_registry/v4"
ALLOWED_POLICY_IDS_V4 = (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CAT2NEW_POLICY_ID,
    BEHAVIOR_CLONE_POLICY_ID,
)
EXPECTED_PRODUCERS_V4 = {
    CAT_POLICY_ID: CAT_V6_PRODUCER,
    CONTRA_DEPLOYED_POLICY_ID: FURY_V5_PRODUCER,
    CONTRA260817_POLICY_ID: CONTRA260817_V4_PRODUCER,
    CAT2NEW_POLICY_ID: CAT2NEW_PRODUCER,
    BEHAVIOR_CLONE_POLICY_ID: BEHAVIOR_CLONE_PRODUCER,
}


class FuryMultiseedWorkerRegistryV4Error(RuntimeError):
    """The five-lane plan or worker-owned registry is inconsistent."""


@dataclass(frozen=True)
class NativeDiagnosticLaneRegistryV4:
    schema: str
    executors: Mapping[str, Callable[..., Mapping[str, Any]]]
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]]
    behavior_clone_model_sha256: str


def five_lane_contracts_v4() -> tuple[JSONMap, ...]:
    """Return the exact additive lane order, retaining the v3 four-lane prefix."""

    deployed = next(
        row
        for row in builtin_lane_contracts_v4()
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    return (
        cat_runner_v4_lane_contract_v6(),
        deployed,
        contra260817_runner_v4_lane_contract_v4(),
        cat2new_lane_contract_v3(),
        behavior_clone_lane_contract_v1(),
    )


def build_native_diagnostic_lane_registry_v4(
    bridge: Any,
    cat2_feedback_policy: Any,
    *,
    behavior_clone_model_path: str | Path,
    optimizer_parameters: Mapping[str, Any] | None = None,
) -> NativeDiagnosticLaneRegistryV4:
    """Bind all five producers to one worker-owned bridge and explicit model."""

    cat2_executor = Cat2NewFuryPairedLaneAdapterV3(
        bridge=bridge,
        feedback_policy=cat2_feedback_policy,
        optimizer_parameters=dict(optimizer_parameters or {}),
    )
    clone_executor = HistoricalBehaviorCloneRunnerV4ExecutorV1(
        behavior_clone_model_path,
        lambda **_: bridge,
    )

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

    registry = NativeDiagnosticLaneRegistryV4(
        schema=REGISTRY_SCHEMA_V4,
        executors={
            CAT_POLICY_ID: lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
                bridge, group=group, scenario=scenario, policy=policy
            ),
            CONTRA_DEPLOYED_POLICY_ID: lambda *, group, scenario, policy: execute_dynamic_v5_baseline_v4(
                bridge, group=group, scenario=scenario, policy=policy
            ),
            CONTRA260817_POLICY_ID: lambda *, group, scenario, policy: execute_contra260817_runner_v4_lane_v4(
                bridge, group=group, scenario=scenario, policy=policy
            ),
            CAT2NEW_POLICY_ID: cat2_executor,
            BEHAVIOR_CLONE_POLICY_ID: clone_executor,
        },
        artifact_validators={
            CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
            CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
            CAT2NEW_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
            BEHAVIOR_CLONE_PRODUCER: clone_validator,
        },
        behavior_clone_model_sha256=str(
            clone_executor.binding["model_sha256"]
        ),
    )
    return validate_native_diagnostic_lane_registry_v4(registry)


def validate_native_diagnostic_lane_registry_v4(
    registry: NativeDiagnosticLaneRegistryV4,
) -> NativeDiagnosticLaneRegistryV4:
    if not isinstance(registry, NativeDiagnosticLaneRegistryV4) or (
        registry.schema != REGISTRY_SCHEMA_V4
    ):
        raise FuryMultiseedWorkerRegistryV4Error("worker registry schema mismatch")
    if tuple(registry.executors) != ALLOWED_POLICY_IDS_V4:
        raise FuryMultiseedWorkerRegistryV4Error(
            "registry must preserve the four-lane v3 prefix and append pooled clone"
        )
    expected_validators = {
        CAT_V6_PRODUCER,
        CONTRA260817_V4_PRODUCER,
        CAT2NEW_PRODUCER,
        BEHAVIOR_CLONE_PRODUCER,
    }
    if set(registry.artifact_validators) != expected_validators:
        raise FuryMultiseedWorkerRegistryV4Error(
            "registry producer-validator set is incomplete"
        )
    if not all(callable(value) for value in registry.executors.values()) or not all(
        callable(value) for value in registry.artifact_validators.values()
    ):
        raise FuryMultiseedWorkerRegistryV4Error(
            "registry executors and validators must be callable"
        )
    if len(registry.behavior_clone_model_sha256) != 64:
        raise FuryMultiseedWorkerRegistryV4Error(
            "behavior-clone model identity is malformed"
        )
    return registry


def _validate_five_lane_plan(plan: Mapping[str, Any]) -> JSONMap:
    checked = validate_runner_plan(plan)
    contract = checked["contract"]
    if tuple(contract.get("policy_ids", ())) != ALLOWED_POLICY_IDS_V4:
        raise FuryMultiseedWorkerRegistryV4Error(
            "runner plan must contain the exact five-lane additive order"
        )
    lanes = contract.get("lane_contracts")
    if lanes != list(five_lane_contracts_v4()):
        raise FuryMultiseedWorkerRegistryV4Error(
            "runner plan lane contracts differ from the registered five lanes"
        )
    policies = {row.get("policy_id"): row for row in contract.get("policies", [])}
    clone = policies.get(BEHAVIOR_CLONE_POLICY_ID)
    if not isinstance(clone, Mapping) or clone.get("role") != "BASELINE_CANDIDATE_NONVOTING":
        raise FuryMultiseedWorkerRegistryV4Error(
            "pooled clone must remain a non-voting baseline candidate"
        )
    if contract.get("status") != "READY_FOR_SMALL_FIXTURE":
        raise FuryMultiseedWorkerRegistryV4Error(
            "five-lane runner plan is not ready for a small fixture"
        )
    return checked


def execute_registered_small_fixture_v4(
    plan: Mapping[str, Any],
    registry: NativeDiagnosticLaneRegistryV4,
) -> JSONMap:
    """Execute one runner-v4 paired group through the five-lane registry."""

    checked = _validate_five_lane_plan(plan)
    registry = validate_native_diagnostic_lane_registry_v4(registry)
    policies = {
        row["policy_id"]: row for row in checked["contract"]["policies"]
    }
    clone = policies[BEHAVIOR_CLONE_POLICY_ID]
    if clone.get("source_sha256") != registry.behavior_clone_model_sha256:
        raise FuryMultiseedWorkerRegistryV4Error(
            "runner plan clone identity differs from worker model path"
        )

    def route(
        *, group: Mapping[str, Any], scenario: Mapping[str, Any], policy: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        policy_id = str(policy.get("policy_id"))
        executor = registry.executors.get(policy_id)
        if executor is None:
            raise FuryMultiseedWorkerRegistryV4Error(
                f"no registered executor for {policy_id}"
            )
        return executor(group=group, scenario=scenario, policy=policy)

    return execute_small_fixture_v4(
        checked,
        route,
        artifact_validators=registry.artifact_validators,
    )


__all__ = (
    "ALLOWED_POLICY_IDS_V4",
    "BEHAVIOR_CLONE_POLICY_ID",
    "EXPECTED_PRODUCERS_V4",
    "FuryMultiseedWorkerRegistryV4Error",
    "NativeDiagnosticLaneRegistryV4",
    "REGISTRY_SCHEMA_V4",
    "build_native_diagnostic_lane_registry_v4",
    "execute_registered_small_fixture_v4",
    "five_lane_contracts_v4",
    "validate_native_diagnostic_lane_registry_v4",
)
