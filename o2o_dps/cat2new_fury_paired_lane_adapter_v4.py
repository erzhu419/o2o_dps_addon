"""Runner-v4 paired lane for mandatory causal Cat2 policy observations.

This adapter leaves the frozen v3 adapter and all of its historical callers
unchanged.  It validates the two prefix registries before touching the bridge,
then supplies the v7 causal policy adapter to the established dynamic-v5 lane
and wraps its v6 control-plane rollout in a v7 evidence envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .cat2new_candidate_executor_v4 import (
    DEFAULT_INSTALLED_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_SAVEDVARIABLES,
    DEFAULT_SOURCE_ROOT,
)
from .cat2new_candidate_feedback_loop_v7 import (
    ROLLOUT_SCHEMA_V7,
    CausalFeedbackPolicyAdapterV7,
    build_cat2new_feedback_rollout_v7,
    validate_cat2new_feedback_rollout_v7,
)
from .cat2new_candidate_simulator_executor_v5 import (
    Cat2NewSimulatorOperationBindingsV5,
)
from .cat2new_fury_paired_lane_adapter_v3 import (
    Cat2NewFuryPairedLaneAdapterV3,
    validate_cat2new_fury_paired_artifact_v3,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    LaneContractV4,
    build_lane_result_v4,
    validate_lane_result_v4,
)
from .policy_observation_causal_projection_v1 import (
    PolicyObservationCausalProjectionV1Error,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    target_health_prefix_registry_from_wire_v1,
    target_introduction_registry_from_wire_v1,
)


JSONMap = dict[str, Any]
PRODUCER = "cat2new_feedback_loop_v7"
OFFLINE_LANE = "OFFLINE_SIMULATOR_ONLY"


class Cat2NewFuryPairedLaneAdapterV4Error(RuntimeError):
    """The causal Cat2 paired lane contract failed."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Cat2NewFuryPairedLaneAdapterV4Error(f"{label} must be an object")
    return value


def _scenario_registries(
    scenario: Mapping[str, Any],
) -> tuple[TargetIntroductionRegistryV1, TargetHealthPrefixRegistryV1]:
    """Parse mandatory registries without accessing a simulator bridge."""

    target_context = _mapping(
        scenario.get("target_context_bundle"),
        "scenario.target_context_bundle",
    )
    introduction_wire = target_context.get(
        "policy_target_introduction_registry"
    )
    health_wire = target_context.get("policy_target_health_prefix_registry")
    if introduction_wire is None:
        raise Cat2NewFuryPairedLaneAdapterV4Error(
            "scenario lacks policy_target_introduction_registry"
        )
    if health_wire is None:
        raise Cat2NewFuryPairedLaneAdapterV4Error(
            "scenario lacks policy_target_health_prefix_registry"
        )
    try:
        introduction = target_introduction_registry_from_wire_v1(
            _mapping(
                introduction_wire,
                "scenario policy_target_introduction_registry",
            )
        )
        health = target_health_prefix_registry_from_wire_v1(
            _mapping(
                health_wire,
                "scenario policy_target_health_prefix_registry",
            )
        )
    except PolicyObservationCausalProjectionV1Error as error:
        raise Cat2NewFuryPairedLaneAdapterV4Error(
            f"scenario policy observation registry is invalid: {error}"
        ) from error
    return introduction, health


def cat2new_lane_contract_v4() -> JSONMap:
    return LaneContractV4(
        policy_id=CAT2NEW_POLICY_ID,
        producer=PRODUCER,
        artifact_schema=ROLLOUT_SCHEMA_V7,
        source_oracle_status="CAT2NEW_V6_SOURCE_FROZEN_V7_CAUSAL_ADAPTER",
        ordered_sink_status="CAT2NEW_V5_EXACT_GLOBAL_UNIT_ROUTING",
        full_policy_status="CAT2NEW_V7_CAUSAL_DYNAMIC_V5_ADAPTER_READY",
        dynamic_v5_executable=True,
        blocker_codes=(),
    ).to_wire()


def validate_cat2new_fury_paired_artifact_v4(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    validated = validate_cat2new_feedback_rollout_v7(artifact)
    summary = validate_cat2new_fury_paired_artifact_v3(
        _mapping(
            validated.get("control_plane_rollout"),
            "v7 control_plane_rollout",
        ),
        producer_runtime_receipt,
        dynamic_load,
    )
    return {**summary, "artifact_schema": ROLLOUT_SCHEMA_V7}


@dataclass
class Cat2NewFuryPairedLaneAdapterV4:
    """Callable causal lane over one worker-owned dynamic-v3 bridge."""

    bridge: Any
    feedback_policy: Any
    operation_bindings: Cat2NewSimulatorOperationBindingsV5 = field(
        default_factory=Cat2NewSimulatorOperationBindingsV5
    )
    optimizer_parameters: Mapping[str, Any] = field(default_factory=dict)
    historical_prior: Mapping[str, Any] = field(default_factory=dict)
    source_root: str | Path = DEFAULT_SOURCE_ROOT
    manifest_path: str | Path = DEFAULT_MANIFEST
    installed_root: str | Path = DEFAULT_INSTALLED_ROOT
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES
    max_decisions: int = 10_000
    max_advances: int = 100_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.bridge, "load_dynamic_v3", None)):
            raise Cat2NewFuryPairedLaneAdapterV4Error(
                "bridge must expose load_dynamic_v3"
            )
        if not callable(getattr(self.feedback_policy, "decide", None)):
            raise Cat2NewFuryPairedLaneAdapterV4Error(
                "feedback_policy must expose decide"
            )

    def __call__(
        self,
        *,
        group: Mapping[str, Any],
        scenario: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> JSONMap:
        group_row = _mapping(group, "group")
        scenario_row = _mapping(scenario, "scenario")
        policy_row = _mapping(policy, "policy")

        # This is deliberately before constructing or invoking the v3 adapter:
        # a missing registry cannot trigger load_dynamic_v3 or any bridge read.
        introduction, health = _scenario_registries(scenario_row)
        causal_policy = CausalFeedbackPolicyAdapterV7(
            policy=self.feedback_policy,
            target_introduction_registry=introduction,
            target_health_prefix_registry=health,
            operation_bindings=self.operation_bindings,
        )
        v3_adapter = Cat2NewFuryPairedLaneAdapterV3(
            bridge=self.bridge,
            feedback_policy=causal_policy,
            operation_bindings=self.operation_bindings,
            optimizer_parameters=self.optimizer_parameters,
            historical_prior=self.historical_prior,
            source_root=self.source_root,
            manifest_path=self.manifest_path,
            installed_root=self.installed_root,
            savedvariables_path=self.savedvariables_path,
            max_decisions=self.max_decisions,
            max_advances=self.max_advances,
        )
        old = v3_adapter(
            group=group_row,
            scenario=scenario_row,
            policy=policy_row,
        )
        old_result = _mapping(old.get("lane_result"), "v3 lane_result")
        v7_artifact = build_cat2new_feedback_rollout_v7(
            _mapping(old_result.get("artifact"), "v3 lane artifact"),
            target_introduction_registry=introduction,
            target_health_prefix_registry=health,
            decision_audit=causal_policy.decision_audit,
        )
        lane_result = build_lane_result_v4(
            policy_id=str(old_result.get("policy_id")),
            producer=PRODUCER,
            artifact=v7_artifact,
            request_sha256=str(old_result.get("request_sha256")),
            simulator_seed=old_result.get("simulator_seed"),
            dynamic_load_contract_sha256=str(
                old_result.get("dynamic_load_contract_sha256")
            ),
            completion_mode=str(old_result.get("completion_mode")),
            damage=old_result.get("damage"),
            elapsed_ms=old_result.get("elapsed_ms"),
            completion_criterion_met=old_result.get(
                "completion_criterion_met"
            ),
            offline_score_eligible=old_result.get("offline_score_eligible"),
            omitted_lane_count=old_result.get("omitted_lane_count"),
            fatal_error_count=old_result.get("fatal_error_count"),
            nonfaithful_reason_counts=_mapping(
                old_result.get("nonfaithful_reason_counts"),
                "v3 nonfaithful_reason_counts",
            ),
            end_state_sha256=str(old_result.get("end_state_sha256")),
            dynamic_runtime_receipts_complete=old_result.get(
                "dynamic_runtime_receipts_complete"
            ),
            producer_runtime_receipt=_mapping(
                old_result.get("producer_runtime_receipt"),
                "v3 producer_runtime_receipt",
            ),
            live_fidelity=False,
            comparison_ready=False,
        )
        validated = validate_lane_result_v4(
            lane_result,
            group=group_row,
            scenario=scenario_row,
            policy=policy_row,
            artifact_validator=validate_cat2new_fury_paired_artifact_v4,
        )
        return {"lane_result": validated}


__all__ = (
    "Cat2NewFuryPairedLaneAdapterV4",
    "Cat2NewFuryPairedLaneAdapterV4Error",
    "OFFLINE_LANE",
    "PRODUCER",
    "cat2new_lane_contract_v4",
    "validate_cat2new_fury_paired_artifact_v4",
)
