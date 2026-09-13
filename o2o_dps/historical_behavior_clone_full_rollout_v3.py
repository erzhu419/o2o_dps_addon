"""Causal-observation wrapper for the frozen clone-v2 rollout.

The v2 rollout is an immutable scientific artifact.  This development-only
version executes that exact implementation in a per-call function namespace
whose observation factory projects raw bridge state onto the observed prefix.
The module never mutates v2 globals.  It retains the complete v2 artifact plus
every projected decision state in an independently addressed v3 envelope; the
validator reconstructs each raw decision state from v2 and re-runs projection.
"""

from __future__ import annotations

from copy import deepcopy
from types import FunctionType
from typing import Any, Mapping

from . import fury_paired_multiseed_runner_v4 as _runner_v4
from . import historical_behavior_clone_full_rollout_v2 as rollout_v2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .policy_observation_causal_projection_v1 import (
    PolicyObservationCausalProjectionV1Error,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    project_live_state_for_policy_v1,
    target_health_prefix_registry_from_wire_v1,
    target_introduction_registry_from_wire_v1,
)
from .sim_bridge import AvailableAction
from .sim_bridge_dynamic_v3 import DynamicLoadReceiptV3


JSONMap = dict[str, Any]
SCHEMA = "historical_behavior_clone_dynamic_v5_rollout/v3"
IMPLEMENTATION_REVISION = "v3.1_reprojectable_decision_evidence"
PRODUCER = "historical_behavior_clone_full_rollout_v3"
CONTENT_ADDRESS_SCHEMA = "historical_behavior_clone_rollout_content/v3"
POLICY_OBSERVATION_CONTRACT_SCHEMA = (
    "historical_behavior_clone_policy_observation/v3"
)
PROJECTION_EVIDENCE_SCHEMA = (
    "historical_behavior_clone_policy_projection_evidence/v3"
)


CLAIM_BOUNDARY_V3: JSONMap = {
    **deepcopy(rollout_v2.CLAIM_BOUNDARY_V2),
    "policy_observation_future_information_screened": True,
    "target_current_and_maximum_health_separated": True,
    "window_origin_character_checkpoint_complete": False,
    "responsive_teammate_model_connected": False,
    "per_decision_projection_evidence_recomputed": True,
}


class HistoricalBehaviorCloneFullRolloutV3Error(RuntimeError):
    """The causal wrapper, explicit prefix registries, or v2 artifact failed."""


def _require_registries(
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
) -> None:
    if not isinstance(
        target_introduction_registry, TargetIntroductionRegistryV1
    ):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "target_introduction_registry must be an explicit "
            "TargetIntroductionRegistryV1"
        )
    if not isinstance(
        target_health_prefix_registry, TargetHealthPrefixRegistryV1
    ):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "target_health_prefix_registry must be an explicit "
            "TargetHealthPrefixRegistryV1"
        )


def _policy_observation_contract(
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
) -> JSONMap:
    return {
        "schema": POLICY_OBSERVATION_CONTRACT_SCHEMA,
        "projection": "policy_observation_causal_projection_v1",
        "target_introduction_registry": (
            target_introduction_registry.to_wire()
        ),
        "target_health_prefix_registry": (
            target_health_prefix_registry.to_wire()
        ),
        "raw_bridge_state_visible_to_policy": False,
        "raw_simulator_target_health_visible_to_policy": False,
        "raw_simulator_target_armor_visible_to_policy": False,
        "future_target_rows_visible_to_policy": False,
        "control_plane_target_map_visible_to_policy": False,
        "per_decision_projected_state_retained": True,
        "validator_reprojects_reconstructible_raw_decision_states": True,
        "training_authorized": False,
        "comparison_authorized": False,
        "deployment_authorized": False,
    }


def _causal_observation_factory(
    *,
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
    evidence_sink: list[JSONMap],
):
    """Return a v2-compatible factory that projects every live state first."""

    def factory(*, root_time_ms: int, last_gcd: dict[str, Any]):
        raw_factory = rollout_v2._observation_factory(
            root_time_ms=root_time_ms,
            last_gcd=last_gcd,
        )

        def build(
            live_state: Mapping[str, Any],
            available: tuple[AvailableAction, ...],
            clone_adapter: Any,
        ) -> JSONMap:
            try:
                projection = project_live_state_for_policy_v1(
                    live_state,
                    target_introduction_registry,
                    target_health_prefix_registry,
                )
            except PolicyObservationCausalProjectionV1Error as error:
                raise HistoricalBehaviorCloneFullRolloutV3Error(
                    f"causal clone observation projection failed: {error}"
                ) from error
            observation = raw_factory(
                projection.state, available, clone_adapter
            )
            evidence_sink.append(
                {
                    "schema": PROJECTION_EVIDENCE_SCHEMA,
                    "observation_ordinal": len(evidence_sink),
                    "raw_state_sha256": _runner_v4.sha256_json(live_state),
                    "projected_live_state": deepcopy(projection.state),
                    "policy_to_simulator_target_index": list(
                        projection.policy_to_simulator_target_index
                    ),
                    "visibility_cutoff_ms": projection.visibility_cutoff_ms,
                }
            )
            return observation

        return build

    return factory


def _reconstruct_decision_raw_states(
    inner: Mapping[str, Any],
) -> list[tuple[int, int, Mapping[str, Any]]]:
    epochs = inner.get("epochs")
    if not isinstance(epochs, list):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "retained clone-v2 epochs are unavailable for projection replay"
        )
    result: list[tuple[int, int, Mapping[str, Any]]] = []
    for epoch_index, epoch in enumerate(epochs):
        if not isinstance(epoch, Mapping):
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                "retained clone-v2 epoch is not an object"
            )
        state = epoch.get("simulator_state_before")
        steps = epoch.get("steps")
        if not isinstance(state, Mapping) or not isinstance(steps, list):
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                "retained epoch cannot reconstruct its decision-state sequence"
            )
        for substep_index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                raise HistoricalBehaviorCloneFullRolloutV3Error(
                    "retained clone-v2 step is not an object"
                )
            result.append((epoch_index, substep_index, state))
            next_state = step.get("final_state")
            if not isinstance(next_state, Mapping):
                raise HistoricalBehaviorCloneFullRolloutV3Error(
                    "retained step lacks its final state"
                )
            state = next_state
        if state != epoch.get("final_state"):
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                "retained epoch final state differs from its step sequence"
            )
    if inner.get("decision_count") != len(result):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "reconstructed decision-state count differs from clone-v2"
        )
    return result


def _bind_projection_evidence(
    inner: Mapping[str, Any], evidence: list[JSONMap]
) -> list[JSONMap]:
    reconstructed = _reconstruct_decision_raw_states(inner)
    if len(evidence) != len(reconstructed):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "projected-state evidence does not cover every retained decision"
        )
    result: list[JSONMap] = []
    for row, (epoch_index, substep_index, raw_state) in zip(
        evidence, reconstructed, strict=True
    ):
        if row.get("raw_state_sha256") != _runner_v4.sha256_json(raw_state):
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                "captured projection is not tied to the retained raw state"
            )
        bound = deepcopy(row)
        bound["epoch_index"] = epoch_index
        bound["substep_index"] = substep_index
        result.append(bound)
    return result


def _validate_projection_evidence(
    raw: Mapping[str, Any],
    inner: Mapping[str, Any],
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
) -> None:
    evidence = raw.get("policy_projection_evidence")
    reconstructed = _reconstruct_decision_raw_states(inner)
    if not isinstance(evidence, list) or len(evidence) != len(reconstructed):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "policy projection evidence does not cover every decision"
        )
    for ordinal, (row, reconstructed_row) in enumerate(
        zip(evidence, reconstructed, strict=True)
    ):
        epoch_index, substep_index, raw_state = reconstructed_row
        if not isinstance(row, Mapping):
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                "policy projection evidence row is not an object"
            )
        try:
            projection = project_live_state_for_policy_v1(
                raw_state,
                target_introduction_registry,
                target_health_prefix_registry,
            )
        except PolicyObservationCausalProjectionV1Error as error:
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                f"retained raw decision state cannot be reprojected: {error}"
            ) from error
        expected = {
            "schema": PROJECTION_EVIDENCE_SCHEMA,
            "observation_ordinal": ordinal,
            "raw_state_sha256": _runner_v4.sha256_json(raw_state),
            "projected_live_state": projection.state,
            "policy_to_simulator_target_index": list(
                projection.policy_to_simulator_target_index
            ),
            "visibility_cutoff_ms": projection.visibility_cutoff_ms,
            "epoch_index": epoch_index,
            "substep_index": substep_index,
        }
        if dict(row) != expected:
            raise HistoricalBehaviorCloneFullRolloutV3Error(
                "policy projection evidence differs from validator reprojection"
            )


def _isolated_v2_rollout_callable(observation_factory: Any) -> Any:
    """Clone the v2 function with one private globals mapping for this call."""

    original = rollout_v2.run_behavior_clone_dynamic_v5_rollout_v2
    namespace = dict(original.__globals__)
    namespace["_observation_factory"] = observation_factory
    isolated = FunctionType(
        original.__code__,
        namespace,
        name=original.__name__,
        argdefs=original.__defaults__,
        closure=original.__closure__,
    )
    isolated.__kwdefaults__ = dict(original.__kwdefaults__ or {})
    return isolated


def run_behavior_clone_dynamic_v5_rollout_v3(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    model: Mapping[str, Any],
    expected_model_binding: Mapping[str, Any],
    seed: int,
    dynamic_load: DynamicRolloutLoadV3,
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    """Execute clone-v2 through an isolated causal observation namespace."""

    _require_registries(
        target_introduction_registry, target_health_prefix_registry
    )
    captured_projection_evidence: list[JSONMap] = []
    causal_factory = _causal_observation_factory(
        target_introduction_registry=target_introduction_registry,
        target_health_prefix_registry=target_health_prefix_registry,
        evidence_sink=captured_projection_evidence,
    )
    isolated_run = _isolated_v2_rollout_callable(causal_factory)
    try:
        inner = isolated_run(
            bridge,
            raid_sim_request,
            model=model,
            expected_model_binding=expected_model_binding,
            seed=seed,
            dynamic_load=dynamic_load,
            max_decisions=max_decisions,
            max_advances=max_advances,
            retain_steps=retain_steps,
        )
    except rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error as error:
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            f"nested clone-v2 rollout failed: {error}"
        ) from error

    projection_evidence = _bind_projection_evidence(
        inner, captured_projection_evidence
    )
    artifact: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "producer": PRODUCER,
        "status": inner["status"],
        "policy_id": inner["policy_id"],
        "prototype_id": inner["prototype_id"],
        "seed": seed,
        "policy_observation_contract": _policy_observation_contract(
            target_introduction_registry, target_health_prefix_registry
        ),
        "policy_projection_evidence": projection_evidence,
        "inner_rollout_v2": deepcopy(inner),
        "execution_isolation": {
            "frozen_v2_source_modified": False,
            "v2_module_globals_mutated": False,
            "per_call_function_namespace": True,
        },
        "claim_boundary": deepcopy(CLAIM_BOUNDARY_V3),
    }
    artifact["content_address"] = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _runner_v4.sha256_json(artifact),
    }
    validate_behavior_clone_dynamic_v5_rollout_v3(
        artifact,
        dynamic_load,
        target_introduction_registry=target_introduction_registry,
        target_health_prefix_registry=target_health_prefix_registry,
        expected_model_binding=inner["model_binding"],
        expected_exact_model_validation_receipt=expected_model_binding,
        expected_bridge_runtime_evidence=inner["bridge_runtime_evidence"],
        loaded_receipt=DynamicLoadReceiptV3(
            **inner["dynamic_load_binding"]["bridge_receipt"]
        ),
    )
    return artifact


def validate_behavior_clone_dynamic_v5_rollout_v3(
    artifact: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
    *,
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
    expected_model_binding: Mapping[str, Any],
    expected_exact_model_validation_receipt: Mapping[str, Any],
    expected_bridge_runtime_evidence: Mapping[str, Any],
    loaded_receipt: DynamicLoadReceiptV3 | None = None,
    expected_dynamic_load_binding: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Validate the v3 envelope and the complete retained clone-v2 artifact."""

    _require_registries(
        target_introduction_registry, target_health_prefix_registry
    )
    try:
        raw = rollout_v2._strict_json(artifact, "clone-v3 rollout")
    except rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error as error:
        raise HistoricalBehaviorCloneFullRolloutV3Error(str(error)) from error
    if not isinstance(raw, dict):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 rollout must be an object"
        )
    content = raw.get("content_address")
    expected_content = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _runner_v4.sha256_json(
            {key: value for key, value in raw.items() if key != "content_address"}
        ),
    }
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("producer") != PRODUCER
        or content != expected_content
    ):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 rollout identity or content address differs"
        )
    contract = raw.get("policy_observation_contract")
    expected_contract = _policy_observation_contract(
        target_introduction_registry, target_health_prefix_registry
    )
    if contract != expected_contract:
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 policy observation contract differs"
        )
    try:
        target_introduction_registry_from_wire_v1(
            contract["target_introduction_registry"]
        )
        target_health_prefix_registry_from_wire_v1(
            contract["target_health_prefix_registry"]
        )
    except (KeyError, PolicyObservationCausalProjectionV1Error) as error:
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            f"clone-v3 policy observation contract is invalid: {error}"
        ) from error
    if raw.get("execution_isolation") != {
        "frozen_v2_source_modified": False,
        "v2_module_globals_mutated": False,
        "per_call_function_namespace": True,
    }:
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 execution isolation receipt differs"
        )
    if raw.get("claim_boundary") != CLAIM_BOUNDARY_V3:
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 claim boundary differs"
        )
    inner = raw.get("inner_rollout_v2")
    if not isinstance(inner, Mapping):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 retained rollout must be an object"
        )
    try:
        checked = rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
            inner,
            inner.get("dynamic_v3_runtime_receipt_closure"),
            dynamic_load,
            expected_model_binding=expected_model_binding,
            expected_exact_model_validation_receipt=(
                expected_exact_model_validation_receipt
            ),
            expected_bridge_runtime_evidence=expected_bridge_runtime_evidence,
            loaded_receipt=loaded_receipt,
            expected_dynamic_load_binding=expected_dynamic_load_binding,
        )
    except (TypeError, rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error) as error:
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            f"retained clone-v2 rollout is invalid: {error}"
        ) from error
    _validate_projection_evidence(
        raw,
        checked,
        target_introduction_registry,
        target_health_prefix_registry,
    )
    if (
        raw.get("status") != checked.get("status")
        or raw.get("policy_id") != checked.get("policy_id")
        or raw.get("prototype_id") != checked.get("prototype_id")
        or raw.get("seed") != dynamic_load.seed
    ):
        raise HistoricalBehaviorCloneFullRolloutV3Error(
            "clone-v3 outer identity differs from retained rollout"
        )
    return raw


__all__ = [
    "CLAIM_BOUNDARY_V3",
    "CONTENT_ADDRESS_SCHEMA",
    "HistoricalBehaviorCloneFullRolloutV3Error",
    "IMPLEMENTATION_REVISION",
    "POLICY_OBSERVATION_CONTRACT_SCHEMA",
    "PROJECTION_EVIDENCE_SCHEMA",
    "PRODUCER",
    "SCHEMA",
    "run_behavior_clone_dynamic_v5_rollout_v3",
    "validate_behavior_clone_dynamic_v5_rollout_v3",
]
