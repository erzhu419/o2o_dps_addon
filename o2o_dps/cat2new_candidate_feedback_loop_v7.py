"""Causal policy-plane adapter for the frozen Cat2_new feedback loop.

The v6 loop remains byte-for-byte frozen.  This version places a mandatory
causal projection immediately in front of every policy decision while keeping
the full simulator state on the control plane.  The policy sees deterministic
prefix-local target tokens only.  A selected local token is privately
translated exactly once into an existing global unit token before the frozen
v6 loop receives it; the established v5 executor then consumes that global
token normally.

The resulting v7 document is an evidence envelope around the unchanged v6
control-plane rollout.  It is development-only and does not authorize a
scientific comparison or deployment.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .cat2new_candidate_executor_v4 import (
    DEFAULT_INSTALLED_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_SAVEDVARIABLES,
    DEFAULT_SOURCE_ROOT,
)
from .cat2new_candidate_feedback_loop_v6 import (
    ROLLOUT_SCHEMA_V6,
    Cat2NewPolicyIntentV6,
    _requires_explicit_retarget,
    run_cat2new_feedback_policy_v6,
    validate_cat2new_feedback_rollout_v6,
)
from .cat2new_candidate_simulator_executor_v5 import (
    Cat2NewSimulatorOperationBindingsV5,
    Cat2NewSimulatorRunBindingV5,
)
from .policy_observation_causal_projection_v1 import (
    PolicyObservationCausalProjectionV1Error,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    project_cat2_policy_input_v1,
    target_health_prefix_registry_from_wire_v1,
    target_introduction_registry_from_wire_v1,
)


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V7 = "cat2new_candidate_feedback_rollout/v7"
EXECUTOR_ID_V7 = "cat2new.candidate.feedback.executor.v7"
IMPLEMENTATION_REVISION_V7 = (
    "v7.1_replayable_causal_projection_and_private_target_routing"
)
POLICY_OBSERVATION_AUDIT_SCHEMA_V7 = (
    "cat2new_candidate_causal_policy_decision_audit/v7"
)
ROUTING_MODE_V7 = "POLICY_LOCAL_TOKEN_TO_GLOBAL_UNIT_ONCE_BEFORE_FROZEN_V6"
POLICY_TARGET_BINDINGS_FIELD_V7 = "policy_target_bindings"
POLICY_TARGET_TOKEN_PREFIX_V7 = "policy-target-"


class Cat2NewCandidateFeedbackLoopV7Error(RuntimeError):
    """A mandatory causal-policy or v7 envelope invariant failed."""


Cat2NewPolicyIntentV7 = Cat2NewPolicyIntentV6


class Cat2NewFeedbackPolicyV7(Protocol):
    policy_id: str

    def decide(self, policy_input: Mapping[str, Any]) -> Cat2NewPolicyIntentV7: ...


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Cat2NewCandidateFeedbackLoopV7Error(
            f"value is not finite canonical JSON: {error}"
        ) from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _json_copy(value: Any, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateFeedbackLoopV7Error(f"{label} must be an object")
    copy = json.loads(_canonical_bytes(value).decode("utf-8"))
    if not isinstance(copy, dict):
        raise AssertionError("JSON object did not remain an object")
    return copy


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        "algorithm": "sha256-canonical-json-v1",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _digest(core),
    }


def _require_registries(
    introduction: Any,
    health: Any,
) -> tuple[TargetIntroductionRegistryV1, TargetHealthPrefixRegistryV1]:
    """Validate mandatory registries before any bridge method can be called."""

    if not isinstance(introduction, TargetIntroductionRegistryV1):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "target_introduction_registry must be an explicit "
            "TargetIntroductionRegistryV1"
        )
    if not isinstance(health, TargetHealthPrefixRegistryV1):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "target_health_prefix_registry must be an explicit "
            "TargetHealthPrefixRegistryV1"
        )
    return introduction, health


def _policy_target_token(policy_target_index: int) -> str:
    if (
        isinstance(policy_target_index, bool)
        or not isinstance(policy_target_index, int)
        or policy_target_index < 0
    ):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "policy target index must be a nonnegative integer"
        )
    return f"{POLICY_TARGET_TOKEN_PREFIX_V7}{policy_target_index:06d}"


def _causal_policy_input_v7(
    projected_policy_input: Mapping[str, Any],
    *,
    visible_target_count: int,
) -> JSONMap:
    """Expose only deterministic prefix-local target identities to policy."""

    result = _json_copy(projected_policy_input, "projected policy input")
    result[POLICY_TARGET_BINDINGS_FIELD_V7] = [
        {
            "policy_target_index": index,
            "policy_target_token": _policy_target_token(index),
        }
        for index in range(visible_target_count)
    ]
    return _json_copy(result, "v7 causal policy input")


def _intent_from_wire(value: Mapping[str, Any]) -> Cat2NewPolicyIntentV6:
    wire = _json_copy(value, "policy intent")
    if set(wire) != {"schema", "operations", "policy_metadata"}:
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "policy intent fields are not exact"
        )
    operations = wire.get("operations")
    metadata = wire.get("policy_metadata")
    if not isinstance(operations, list) or not isinstance(metadata, Mapping):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "policy intent operations or metadata is malformed"
        )
    try:
        return Cat2NewPolicyIntentV6(
            operations=tuple(operations),
            policy_metadata=metadata,
            schema=str(wire.get("schema")),
        )
    except (TypeError, ValueError) as error:
        raise Cat2NewCandidateFeedbackLoopV7Error(
            f"policy intent is invalid: {error}"
        ) from error


def _global_units_by_simulator_index(
    target_units: Any,
) -> dict[int, tuple[str, ...]]:
    if not isinstance(target_units, list):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "control-plane target unit bindings must be an array"
        )
    grouped: dict[int, list[str]] = {}
    for row in target_units:
        if not isinstance(row, Mapping):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "control-plane target binding must be an object"
            )
        unit = row.get("unit")
        target_index = row.get("target_index")
        if (
            not isinstance(unit, str)
            or not unit
            or isinstance(target_index, bool)
            or not isinstance(target_index, int)
            or target_index < 0
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "control-plane target binding is malformed"
            )
        grouped.setdefault(target_index, []).append(unit)
    return {
        index: tuple(sorted(units)) for index, units in grouped.items()
    }


def _route_policy_intent_v7(
    pre_routing_intent: Mapping[str, Any],
    *,
    policy_to_simulator_target_index: tuple[int, ...],
    target_units: Any,
) -> tuple[JSONMap, list[JSONMap]]:
    """Translate each selected local token once into a global unit token."""

    intent = _intent_from_wire(pre_routing_intent).to_wire()
    global_units = _global_units_by_simulator_index(target_units)
    token_to_local_index = {
        _policy_target_token(index): index
        for index in range(len(policy_to_simulator_target_index))
    }
    routed_operations: list[JSONMap] = []
    receipts: list[JSONMap] = []
    for raw_operation in intent["operations"]:
        operation = _json_copy(raw_operation, "policy operation")
        if (
            operation.get("lane") == "target"
            and operation.get("intent") == "SET_EXACT_UNIT"
        ):
            arguments = operation.get("arguments")
            if not isinstance(arguments, Mapping):
                raise Cat2NewCandidateFeedbackLoopV7Error(
                    "SET_EXACT_UNIT arguments must be an object"
                )
            policy_token = arguments.get("unit")
            if not isinstance(policy_token, str) or not policy_token:
                raise Cat2NewCandidateFeedbackLoopV7Error(
                    "SET_EXACT_UNIT requires a policy-local target token"
                )
            local_index = token_to_local_index.get(policy_token)
            if local_index is None:
                raise Cat2NewCandidateFeedbackLoopV7Error(
                    "SET_EXACT_UNIT must use a currently visible policy-local "
                    "target token"
                )
            simulator_index = policy_to_simulator_target_index[local_index]
            units = global_units.get(simulator_index, ())
            if not units:
                raise Cat2NewCandidateFeedbackLoopV7Error(
                    "visible policy target lacks a global unit binding"
                )
            global_unit = units[0]
            routed_arguments = dict(arguments)
            routed_arguments["unit"] = global_unit
            operation["arguments"] = routed_arguments
            receipts.append(
                {
                    "operation_id": operation.get("operation_id"),
                    "policy_target_index": local_index,
                    "policy_target_token": policy_token,
                    "simulator_target_index": simulator_index,
                    "global_unit_token": global_unit,
                    "translation_count": 1,
                }
            )
        routed_operations.append(operation)
    routed = {
        "schema": intent["schema"],
        "operations": routed_operations,
        "policy_metadata": intent["policy_metadata"],
    }
    return _intent_from_wire(routed).to_wire(), receipts


@dataclass
class CausalFeedbackPolicyAdapterV7:
    """Project each v6 decision input before the wrapped policy can observe it."""

    policy: Cat2NewFeedbackPolicyV7
    target_introduction_registry: TargetIntroductionRegistryV1
    target_health_prefix_registry: TargetHealthPrefixRegistryV1
    operation_bindings: Cat2NewSimulatorOperationBindingsV5
    decision_audit: list[JSONMap] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        _require_registries(
            self.target_introduction_registry,
            self.target_health_prefix_registry,
        )
        if not isinstance(
            self.operation_bindings, Cat2NewSimulatorOperationBindingsV5
        ):
            raise TypeError("operation_bindings has the wrong type")
        if not callable(getattr(self.policy, "decide", None)):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "policy.decide is required"
            )

    @property
    def policy_id(self) -> str:
        return getattr(self.policy, "policy_id", None)

    def decide(self, raw_policy_input: Mapping[str, Any]) -> Cat2NewPolicyIntentV6:
        try:
            projection = project_cat2_policy_input_v1(
                raw_policy_input,
                self.target_introduction_registry,
                self.target_health_prefix_registry,
            )
        except PolicyObservationCausalProjectionV1Error as error:
            raise Cat2NewCandidateFeedbackLoopV7Error(
                f"causal policy observation projection failed: {error}"
            ) from error

        causal_policy_input = _causal_policy_input_v7(
            projection.policy_input,
            visible_target_count=len(
                projection.policy_to_simulator_target_index
            ),
        )
        raw_boundary = raw_policy_input.get("target_boundary")
        causal_boundary = causal_policy_input.get("target_boundary")
        if not isinstance(raw_boundary, Mapping) or not isinstance(
            causal_boundary, Mapping
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "raw and causal target boundaries must be objects"
            )
        if _requires_explicit_retarget(raw_boundary) != _requires_explicit_retarget(
            causal_boundary
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "raw control-plane retarget predicate differs from the causal "
                "policy predicate"
            )

        pre_routing_intent = self.policy.decide(deepcopy(causal_policy_input))
        if not isinstance(pre_routing_intent, Cat2NewPolicyIntentV6):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "policy.decide must return Cat2NewPolicyIntentV6"
            )
        pre_routing_wire = pre_routing_intent.to_wire()
        routed_wire, routing_receipts = _route_policy_intent_v7(
            pre_routing_wire,
            policy_to_simulator_target_index=(
                projection.policy_to_simulator_target_index
            ),
            target_units=self.operation_bindings.to_wire()["target_units"],
        )
        routed_intent = _intent_from_wire(routed_wire)
        audit_core: JSONMap = {
            "schema": POLICY_OBSERVATION_AUDIT_SCHEMA_V7,
            "decision_index": causal_policy_input["decision_index"],
            "visibility_cutoff_ms": projection.visibility_cutoff_ms,
            "visible_target_count": len(
                projection.policy_to_simulator_target_index
            ),
            "control_plane_raw_policy_input": _json_copy(
                raw_policy_input, "raw policy input"
            ),
            "causal_policy_input": causal_policy_input,
            "policy_observation_sha256": _digest(causal_policy_input),
            "pre_routing_policy_intent": pre_routing_wire,
            "pre_routing_policy_intent_sha256": _digest(pre_routing_wire),
            "routed_control_plane_intent": routed_wire,
            "routed_control_plane_intent_sha256": _digest(routed_wire),
            "private_target_routing_receipts": routing_receipts,
            "routing_mode": ROUTING_MODE_V7,
            "private_routing_translation_count": len(routing_receipts),
        }
        self.decision_audit.append(
            {**audit_core, "content_address": _content_address(audit_core)}
        )
        return routed_intent


def build_cat2new_feedback_rollout_v7(
    control_plane_rollout: Mapping[str, Any],
    *,
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
    decision_audit: list[Mapping[str, Any]],
) -> JSONMap:
    introduction, health = _require_registries(
        target_introduction_registry,
        target_health_prefix_registry,
    )
    v6 = validate_cat2new_feedback_rollout_v6(control_plane_rollout)
    audit = [_json_copy(row, "policy decision audit") for row in decision_audit]
    lifecycle = v6.get("lifecycle_receipt")
    if not isinstance(lifecycle, Mapping) or lifecycle.get(
        "decision_count"
    ) != len(audit):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "causal policy audit does not cover every v6 decision"
        )
    core: JSONMap = {
        "schema": ROLLOUT_SCHEMA_V7,
        "executor_id": EXECUTOR_ID_V7,
        "implementation_revision": IMPLEMENTATION_REVISION_V7,
        "policy_observation_contract": {
            "target_introduction_registry": introduction.to_wire(),
            "target_health_prefix_registry": health.to_wire(),
            "raw_bridge_state_visible_to_policy": False,
            "raw_simulator_target_health_visible_to_policy": False,
            "raw_simulator_target_armor_visible_to_policy": False,
            "run_binding_digest_visible_to_policy": False,
            "future_target_rows_visible_to_policy": False,
            "policy_target_index_space": "VISIBLE_PREFIX_LOCAL_CONTIGUOUS",
            "policy_target_tokens": "DETERMINISTIC_PREFIX_LOCAL_ONLY",
            "global_unit_tokens_visible_to_policy": False,
            "routing_map_visible_to_policy": False,
            "routing_mode": ROUTING_MODE_V7,
            "private_translation_per_selected_target_operation": 1,
        },
        "policy_decision_audit": audit,
        "control_plane_rollout_schema": ROLLOUT_SCHEMA_V6,
        "control_plane_rollout": v6,
        "offline_score_eligible": v6.get("offline_score_eligible") is True,
        "formal_runner_registration_authorized": False,
        "live_client_execution": False,
        "comparison_ready": False,
        "scientific_run_launched": False,
        "claim_boundary": (
            "causal Cat2 policy observations over a v6 control-plane rollout; "
            "development evidence only"
        ),
    }
    return validate_cat2new_feedback_rollout_v7(
        {**core, "content_address": _content_address(core)}
    )


def run_cat2new_feedback_policy_v7(
    bridge: Any,
    policy: Cat2NewFeedbackPolicyV7,
    *,
    simulator_request: Mapping[str, Any],
    dynamic_load_receipt: Any,
    run_binding: Cat2NewSimulatorRunBindingV5,
    target_introduction_registry: TargetIntroductionRegistryV1,
    target_health_prefix_registry: TargetHealthPrefixRegistryV1,
    operation_bindings: Cat2NewSimulatorOperationBindingsV5 = (
        Cat2NewSimulatorOperationBindingsV5()
    ),
    optimizer_parameters: Mapping[str, Any] | None = None,
    historical_prior: Mapping[str, Any] | None = None,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    installed_root: str | Path = DEFAULT_INSTALLED_ROOT,
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    """Run the frozen v6 control plane with mandatory per-decision projection."""

    introduction, health = _require_registries(
        target_introduction_registry,
        target_health_prefix_registry,
    )
    adapter = CausalFeedbackPolicyAdapterV7(
        policy=policy,
        target_introduction_registry=introduction,
        target_health_prefix_registry=health,
        operation_bindings=operation_bindings,
    )
    control = run_cat2new_feedback_policy_v6(
        bridge,
        adapter,
        simulator_request=simulator_request,
        dynamic_load_receipt=dynamic_load_receipt,
        run_binding=run_binding,
        operation_bindings=operation_bindings,
        optimizer_parameters=optimizer_parameters,
        historical_prior=historical_prior,
        source_root=source_root,
        manifest_path=manifest_path,
        installed_root=installed_root,
        savedvariables_path=savedvariables_path,
        max_decisions=max_decisions,
        max_advances=max_advances,
    )
    return build_cat2new_feedback_rollout_v7(
        control,
        target_introduction_registry=introduction,
        target_health_prefix_registry=health,
        decision_audit=adapter.decision_audit,
    )


def validate_cat2new_feedback_rollout_v7(value: Mapping[str, Any]) -> JSONMap:
    document = _json_copy(value, "v7 rollout")
    if document.get("schema") != ROLLOUT_SCHEMA_V7:
        raise Cat2NewCandidateFeedbackLoopV7Error("rollout schema mismatch")
    core = {
        key: item for key, item in document.items() if key != "content_address"
    }
    if document.get("content_address") != _content_address(core):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "rollout content address mismatch"
        )
    if (
        document.get("executor_id") != EXECUTOR_ID_V7
        or document.get("implementation_revision")
        != IMPLEMENTATION_REVISION_V7
        or document.get("control_plane_rollout_schema") != ROLLOUT_SCHEMA_V6
    ):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "v7 implementation identity mismatch"
        )
    for field in (
        "formal_runner_registration_authorized",
        "live_client_execution",
        "comparison_ready",
        "scientific_run_launched",
    ):
        if document.get(field) is not False:
            raise Cat2NewCandidateFeedbackLoopV7Error(
                f"rollout {field} must remain false"
            )

    observation = _json_copy(
        document.get("policy_observation_contract"),
        "policy_observation_contract",
    )
    try:
        introduction = target_introduction_registry_from_wire_v1(
            _json_copy(
                observation.get("target_introduction_registry"),
                "target introduction registry",
            )
        )
        health = target_health_prefix_registry_from_wire_v1(
            _json_copy(
                observation.get("target_health_prefix_registry"),
                "target health prefix registry",
            )
        )
    except PolicyObservationCausalProjectionV1Error as error:
        raise Cat2NewCandidateFeedbackLoopV7Error(
            f"policy observation contract is invalid: {error}"
        ) from error
    expected = {
        "raw_bridge_state_visible_to_policy": False,
        "raw_simulator_target_health_visible_to_policy": False,
        "raw_simulator_target_armor_visible_to_policy": False,
        "run_binding_digest_visible_to_policy": False,
        "future_target_rows_visible_to_policy": False,
        "policy_target_index_space": "VISIBLE_PREFIX_LOCAL_CONTIGUOUS",
        "policy_target_tokens": "DETERMINISTIC_PREFIX_LOCAL_ONLY",
        "global_unit_tokens_visible_to_policy": False,
        "routing_map_visible_to_policy": False,
        "routing_mode": ROUTING_MODE_V7,
        "private_translation_per_selected_target_operation": 1,
    }
    if any(
        observation.get(key) != expected_value
        for key, expected_value in expected.items()
    ):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "policy observation causal boundary differs"
        )

    control = validate_cat2new_feedback_rollout_v6(
        _json_copy(document.get("control_plane_rollout"), "control plane rollout")
    )
    audit = document.get("policy_decision_audit")
    lifecycle = control.get("lifecycle_receipt")
    if not isinstance(audit, list) or not isinstance(lifecycle, Mapping):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "policy audit or lifecycle is malformed"
        )
    if lifecycle.get("decision_count") != len(audit):
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "policy audit does not cover every decision"
        )
    identity = _json_copy(control.get("identity"), "control plane identity")
    operation_bindings = _json_copy(
        identity.get("operation_bindings"), "control plane operation bindings"
    )
    target_units = operation_bindings.get("target_units")
    for index, row in enumerate(audit, start=1):
        audit_row = _json_copy(row, f"policy decision audit {index}")
        audit_core = {
            key: item for key, item in audit_row.items() if key != "content_address"
        }
        if (
            audit_row.get("content_address") != _content_address(audit_core)
            or audit_row.get("schema") != POLICY_OBSERVATION_AUDIT_SCHEMA_V7
            or audit_row.get("decision_index") != index
            or audit_row.get("routing_mode") != ROUTING_MODE_V7
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "policy decision audit identity or routing differs"
            )
        decision = control["decisions"][index - 1]
        if not isinstance(decision, Mapping):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "control-plane decision must be an object"
            )
        raw_policy_input = _json_copy(
            audit_row.get("control_plane_raw_policy_input"),
            "audit raw policy input",
        )
        raw_live_state = _json_copy(
            raw_policy_input.get("live_state"), "audit raw live state"
        )
        if (
            raw_policy_input.get("decision_index") != index
            or decision.get("policy_input_sha256") != _digest(raw_policy_input)
            or decision.get("live_state_raw_sha256") != _digest(raw_live_state)
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "raw policy evidence differs from the frozen v6 decision"
            )
        try:
            reprojection = project_cat2_policy_input_v1(
                raw_policy_input,
                introduction,
                health,
            )
        except PolicyObservationCausalProjectionV1Error as error:
            raise Cat2NewCandidateFeedbackLoopV7Error(
                f"saved raw policy evidence cannot be reprojected: {error}"
            ) from error
        causal_policy_input = _causal_policy_input_v7(
            reprojection.policy_input,
            visible_target_count=len(
                reprojection.policy_to_simulator_target_index
            ),
        )
        saved_causal_policy_input = _json_copy(
            audit_row.get("causal_policy_input"),
            "audit causal policy input",
        )
        if (
            saved_causal_policy_input != causal_policy_input
            or audit_row.get("visibility_cutoff_ms")
            != reprojection.visibility_cutoff_ms
            or audit_row.get("visible_target_count")
            != len(reprojection.policy_to_simulator_target_index)
            or audit_row.get("policy_observation_sha256")
            != _digest(causal_policy_input)
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "causal policy evidence differs from deterministic reprojection"
            )
        pre_routing_intent = _json_copy(
            audit_row.get("pre_routing_policy_intent"),
            "pre-routing policy intent",
        )
        if audit_row.get("pre_routing_policy_intent_sha256") != _digest(
            pre_routing_intent
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "pre-routing policy intent digest mismatch"
            )
        routed_intent, routing_receipts = _route_policy_intent_v7(
            pre_routing_intent,
            policy_to_simulator_target_index=(
                reprojection.policy_to_simulator_target_index
            ),
            target_units=target_units,
        )
        saved_routed_intent = _json_copy(
            audit_row.get("routed_control_plane_intent"),
            "routed control-plane intent",
        )
        control_intent = decision.get("policy_intent")
        if (
            saved_routed_intent != routed_intent
            or audit_row.get("routed_control_plane_intent_sha256")
            != _digest(routed_intent)
            or audit_row.get("private_target_routing_receipts")
            != routing_receipts
            or audit_row.get("private_routing_translation_count")
            != len(routing_receipts)
            or not isinstance(control_intent, Mapping)
            or _json_copy(control_intent, "control-plane policy intent")
            != routed_intent
        ):
            raise Cat2NewCandidateFeedbackLoopV7Error(
                "private target routing differs from the frozen v6 intent"
            )
    expected_offline_eligibility = control.get("offline_score_eligible") is True
    if document.get("offline_score_eligible") != expected_offline_eligibility:
        raise Cat2NewCandidateFeedbackLoopV7Error(
            "v7 eligibility differs from its control-plane rollout"
        )
    return document


def serialize_cat2new_feedback_rollout_v7(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_cat2new_feedback_rollout_v7(value))


__all__ = (
    "CausalFeedbackPolicyAdapterV7",
    "Cat2NewCandidateFeedbackLoopV7Error",
    "Cat2NewFeedbackPolicyV7",
    "Cat2NewPolicyIntentV7",
    "EXECUTOR_ID_V7",
    "IMPLEMENTATION_REVISION_V7",
    "POLICY_OBSERVATION_AUDIT_SCHEMA_V7",
    "POLICY_TARGET_BINDINGS_FIELD_V7",
    "POLICY_TARGET_TOKEN_PREFIX_V7",
    "ROLLOUT_SCHEMA_V7",
    "ROUTING_MODE_V7",
    "build_cat2new_feedback_rollout_v7",
    "run_cat2new_feedback_policy_v7",
    "serialize_cat2new_feedback_rollout_v7",
    "validate_cat2new_feedback_rollout_v7",
)
