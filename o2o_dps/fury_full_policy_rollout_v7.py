"""Runtime-bound deployed-Contra Raid-A rollout, isolated from frozen v6.

V7 keeps v6's ordered executor semantics but replaces the v2 exact-type gate
with the runtime-bound adapter.  The complete binding is embedded in the
artifact content address.  A separate cache identity binds the same binding to
the request, seed, and dynamic-load contract so two profiles cannot reuse one
lane result accidentally.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types
from typing import Any, Mapping

from . import fury_full_policy_rollout_v5 as _v5
from . import fury_full_policy_rollout_v6 as _v6
from .deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    validate_deployed_contra_runtime_binding_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_ordered_sink_executor_v3 import EXECUTION_SCHEMA_V3
from .fury_ordered_sink_executor_v4 import (
    EXECUTION_SCHEMA_V4,
    audit_ordered_execution_v4,
    execute_ordered_sinks_v4,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    ADAPTER_SCHEMA_V7,
    RAID_A_CONTROLLER,
    RAID_B_BLOCKER,
    RAID_B_CONTROLLER,
    RUNTIME_BOUND_EXPERT_ID_V7,
    RuntimeBoundContraDeployedFuryAdapterV7,
)


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V7 = "fury_full_policy_simulator_rollout/v7"
IMPLEMENTATION_REVISION_V7 = "v7.0_deployed_contra_runtime_bound_raid_a"
ROLLOUT_CONTENT_ADDRESS_SCHEMA_V7 = "fury_full_policy_rollout_content/v7"
CACHE_IDENTITY_SCHEMA_V7 = "deployed_contra_lane_cache_identity/v7"


class FuryFullPolicyRolloutV7Error(RuntimeError):
    """The runtime-bound deployed-Contra v7 rollout is malformed."""


def _supported_adapter_v7(adapter: Any) -> bool:
    return (
        type(adapter) is RuntimeBoundContraDeployedFuryAdapterV7
        and adapter.expert_id == RUNTIME_BOUND_EXPERT_ID_V7
        and adapter.controller == RAID_A_CONTROLLER
    )


def _clone_v6_core_v7() -> Any:
    source = _v6._RUN_V6_CORE
    namespace = dict(source.__globals__)
    namespace.update(
        {
            "_supported_adapter": _supported_adapter_v7,
            "execute_ordered_sinks_v2": execute_ordered_sinks_v4,
            "_audit_ordered_execution": audit_ordered_execution_v4,
        }
    )
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v7_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V7_CORE = _clone_v6_core_v7()


def _clone_v5_wrapper_v7() -> Any:
    source = _v5.run_fury_full_policy_rollout_v5
    namespace = dict(source.__globals__)
    namespace.update(
        {
            "_RUN_V5_CORE": _RUN_V7_CORE,
            "validate_fury_full_policy_rollout_v5": lambda value, **_: value,
        }
    )
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v7_base",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V7_BASE = _clone_v5_wrapper_v7()


def build_deployed_contra_lane_cache_identity_v7(
    *,
    runtime_binding: Mapping[str, Any],
    request_sha256: str,
    simulator_seed: int,
    dynamic_load_contract_sha256: str,
    controller: str = RAID_A_CONTROLLER,
) -> JSONMap:
    """Build the pre-execution identity used to partition v7 lane artifacts."""

    binding = _validated_binding(runtime_binding)
    request_digest = _sha256(request_sha256, "request_sha256")
    contract_digest = _sha256(
        dynamic_load_contract_sha256, "dynamic_load_contract_sha256"
    )
    if isinstance(simulator_seed, bool) or not isinstance(simulator_seed, int):
        raise FuryFullPolicyRolloutV7Error("simulator_seed must be an integer")
    if controller not in {RAID_A_CONTROLLER, RAID_B_CONTROLLER}:
        raise FuryFullPolicyRolloutV7Error(
            f"unsupported deployed Contra controller {controller!r}"
        )
    core: JSONMap = {
        "schema": CACHE_IDENTITY_SCHEMA_V7,
        "source_policy_id": binding["source_policy_id"],
        "controller": controller,
        "runtime_bound_adapter_schema": ADAPTER_SCHEMA_V7,
        "runtime_binding_sha256": binding["binding_sha256"],
        "source_manifest_sha256": binding["source_manifest_sha256"],
        "runtime_snapshot_sha256": binding["runtime_snapshot_sha256"],
        "contra_savedvariables_sha256": binding[
            "contra_savedvariables_sha256"
        ],
        "request_sha256": request_digest,
        "simulator_seed": simulator_seed,
        "dynamic_load_contract_sha256": contract_digest,
        "rollout_schema": ROLLOUT_SCHEMA_V7,
        "implementation_revision": IMPLEMENTATION_REVISION_V7,
    }
    return {**core, "sha256": _canonical_sha256(core)}


def run_fury_full_policy_rollout_v7(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    runtime_binding: Mapping[str, Any],
    seed: int,
    target_contexts: Mapping[int, Any],
    dynamic_load: DynamicRolloutLoadV3,
    controller: str = RAID_A_CONTROLLER,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    if controller == RAID_B_CONTROLLER:
        raise FuryFullPolicyRolloutV7Error(
            f"{RAID_B_BLOCKER}: v7 currently covers only Raid-A single-target"
        )
    binding = _validated_binding(runtime_binding)
    adapter = RuntimeBoundContraDeployedFuryAdapterV7(
        binding, controller=controller
    )
    result = _RUN_V7_BASE(
        bridge,
        raid_sim_request,
        adapter,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        max_decisions=max_decisions,
        max_advances=max_advances,
        retain_steps=retain_steps,
    )
    if not isinstance(result, dict):
        raise FuryFullPolicyRolloutV7Error("v7 base returned a non-object artifact")
    result["schema"] = ROLLOUT_SCHEMA_V7
    result["implementation_revision"] = IMPLEMENTATION_REVISION_V7
    isolation = result.get("version_isolation")
    if not isinstance(isolation, dict):
        raise FuryFullPolicyRolloutV7Error("v7 base lacks version isolation")
    executor_module = __import__(
        "o2o_dps.fury_ordered_sink_executor_v3", fromlist=["ignored"]
    )
    isolation.update(
        {
            "frozen_v5_overlay_sha256": _source_sha256(_v5),
            "ordered_sink_executor_schema": EXECUTION_SCHEMA_V3,
            "ordered_sink_executor_v3_sha256": _source_sha256(executor_module),
            "frozen_v6_overlay_sha256": _source_sha256(_v6),
            "runtime_bound_adapter_schema": ADAPTER_SCHEMA_V7,
            "runtime_bound_adapter_v7_sha256": _source_sha256(
                __import__(
                    "o2o_dps.fury_runtime_bound_deployed_contra_adapter_v7",
                    fromlist=["ignored"],
                )
            ),
            "ordered_sink_executor_schema": EXECUTION_SCHEMA_V4,
            "ordered_sink_executor_v4_sha256": _source_sha256(
                __import__(
                    "o2o_dps.fury_ordered_sink_executor_v4",
                    fromlist=["ignored"],
                )
            ),
        }
    )
    command = result.get("bridge_command_contract")
    if not isinstance(command, dict):
        raise FuryFullPolicyRolloutV7Error("v7 base lacks bridge command contract")
    command["ordered_sink_executor_schema"] = EXECUTION_SCHEMA_V4
    command["deployed_contra_controller"] = controller
    command["runtime_binding_consumed_at_every_proposal"] = True
    result["deployed_contra_runtime_binding"] = binding
    result["controller_coverage"] = {
        "selected": controller,
        "implemented": [RAID_A_CONTROLLER],
        "unimplemented": {
            RAID_B_CONTROLLER: RAID_B_BLOCKER,
        },
        "complete_deployed_contra_controller_coverage": False,
    }
    result["lane_cache_identity"] = build_deployed_contra_lane_cache_identity_v7(
        runtime_binding=binding,
        request_sha256=str(result["request_sha256"]),
        simulator_seed=seed,
        dynamic_load_contract_sha256=dynamic_load.contract_sha256,
        controller=controller,
    )
    authority = result.setdefault("authority_boundary", {})
    if not isinstance(authority, dict):
        raise FuryFullPolicyRolloutV7Error("v7 authority boundary is malformed")
    authority.update(
        {
            "runtime_binding_consumed": True,
            "source_identity_bound": True,
            "savedvariables_snapshot_consumed": True,
            "same_character_runtime_parity_proven": False,
            "public_macro_entry_verified": False,
            "client_execution_observed": False,
            "comparison_ready": False,
        }
    )
    core = {key: value for key, value in result.items() if key != "content_address"}
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V7,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(core),
    }
    return validate_fury_full_policy_rollout_v7(result, dynamic_load=dynamic_load)


def validate_fury_full_policy_rollout_v7(
    value: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV3 | None = None,
) -> JSONMap:
    raw = _strict_json(value)
    if (
        raw.get("schema") != ROLLOUT_SCHEMA_V7
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION_V7
        or raw.get("expert_id") != RUNTIME_BOUND_EXPERT_ID_V7
    ):
        raise FuryFullPolicyRolloutV7Error("v7 rollout identity mismatch")
    binding = _validated_binding(
        _mapping(
            raw.get("deployed_contra_runtime_binding"),
            "deployed_contra_runtime_binding",
        )
    )
    coverage = _mapping(raw.get("controller_coverage"), "controller_coverage")
    if coverage != {
        "selected": RAID_A_CONTROLLER,
        "implemented": [RAID_A_CONTROLLER],
        "unimplemented": {RAID_B_CONTROLLER: RAID_B_BLOCKER},
        "complete_deployed_contra_controller_coverage": False,
    }:
        raise FuryFullPolicyRolloutV7Error("v7 controller coverage receipt mismatch")
    command = _mapping(raw.get("bridge_command_contract"), "bridge_command_contract")
    if (
        command.get("deployed_contra_controller") != RAID_A_CONTROLLER
        or command.get("runtime_binding_consumed_at_every_proposal") is not True
        or command.get("ordered_sink_executor_schema") != EXECUTION_SCHEMA_V4
    ):
        raise FuryFullPolicyRolloutV7Error("v7 runtime command binding is missing")
    cache = build_deployed_contra_lane_cache_identity_v7(
        runtime_binding=binding,
        request_sha256=str(raw.get("request_sha256")),
        simulator_seed=raw.get("seed"),
        dynamic_load_contract_sha256=str(
            _mapping(raw.get("dynamic_load_binding"), "dynamic_load_binding").get(
                "contract_sha256"
            )
        ),
        controller=RAID_A_CONTROLLER,
    )
    if raw.get("lane_cache_identity") != cache:
        raise FuryFullPolicyRolloutV7Error("v7 lane cache identity mismatch")
    authority = _mapping(raw.get("authority_boundary"), "authority_boundary")
    for field, expected in (
        ("runtime_binding_consumed", True),
        ("source_identity_bound", True),
        ("savedvariables_snapshot_consumed", True),
        ("same_character_runtime_parity_proven", False),
        ("public_macro_entry_verified", False),
        ("client_execution_observed", False),
        ("comparison_ready", False),
    ):
        if authority.get(field) is not expected:
            raise FuryFullPolicyRolloutV7Error(
                f"v7 authority boundary disagrees on {field}"
            )
    if raw.get("steps_retained") is True:
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps:
            raise FuryFullPolicyRolloutV7Error("retained v7 steps are malformed")
        for index, step in enumerate(steps):
            proposal = step.get("proposal") if isinstance(step, Mapping) else None
            ordered = (
                step.get("ordered_execution")
                if isinstance(step, Mapping)
                else None
            )
            metadata = (
                proposal.get("metadata") if isinstance(proposal, Mapping) else None
            )
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("runtime_binding_sha256")
                != binding["binding_sha256"]
                or metadata.get("runtime_binding_consumed") is not True
                or not isinstance(ordered, Mapping)
                or ordered.get("schema") != EXECUTION_SCHEMA_V4
                or _mapping(
                    ordered.get("v4_semantics"),
                    f"v7 step {index} executor semantics",
                ).get("runtime_binding_sha256")
                != binding["binding_sha256"]
            ):
                raise FuryFullPolicyRolloutV7Error(
                    f"v7 step {index} did not consume the runtime binding"
                )

    projected = deepcopy(raw)
    for field in (
        "deployed_contra_runtime_binding",
        "controller_coverage",
        "lane_cache_identity",
        "authority_boundary",
    ):
        projected.pop(field, None)
    projected["schema"] = _v6.ROLLOUT_SCHEMA_V6
    projected["implementation_revision"] = _v6.IMPLEMENTATION_REVISION_V6
    projected["version_isolation"].pop("frozen_v6_overlay_sha256", None)
    projected["version_isolation"].pop("runtime_bound_adapter_schema", None)
    projected["version_isolation"].pop("runtime_bound_adapter_v7_sha256", None)
    projected["version_isolation"].pop("ordered_sink_executor_v4_sha256", None)
    projected["version_isolation"]["ordered_sink_executor_schema"] = (
        EXECUTION_SCHEMA_V3
    )
    projected["bridge_command_contract"].pop("deployed_contra_controller", None)
    projected["bridge_command_contract"].pop(
        "runtime_binding_consumed_at_every_proposal", None
    )
    projected["bridge_command_contract"]["ordered_sink_executor_schema"] = (
        EXECUTION_SCHEMA_V3
    )
    if projected.get("steps_retained") is True:
        for step in projected.get("steps", []):
            ordered = step.get("ordered_execution")
            if isinstance(ordered, dict):
                ordered["schema"] = EXECUTION_SCHEMA_V3
                ordered["base_executor_schema"] = (
                    "fury_ordered_sink_execution/v2"
                )
                ordered["expert_id"] = RUNTIME_BOUND_EXPERT_ID_V7
                ordered.pop("v4_semantics", None)
    projected_core = {
        key: item for key, item in projected.items() if key != "content_address"
    }
    projected["content_address"] = {
        "schema": _v6.ROLLOUT_CONTENT_ADDRESS_SCHEMA_V6,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _v6._canonical_sha256(projected_core),
    }
    _v6.validate_fury_full_policy_rollout_v6(
        projected, dynamic_load=dynamic_load
    )
    expected_content = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V7,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if raw.get("content_address") != expected_content:
        raise FuryFullPolicyRolloutV7Error("v7 rollout content address mismatch")
    return raw


def _validated_binding(value: Mapping[str, Any]) -> JSONMap:
    try:
        return validate_deployed_contra_runtime_binding_v1(value)
    except DeployedContraRuntimeBindingError as error:
        raise FuryFullPolicyRolloutV7Error(str(error)) from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryFullPolicyRolloutV7Error(f"{label} must be an object")
    return value


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FuryFullPolicyRolloutV7Error(f"{label} must be a lowercase SHA-256")
    return value


def _strict_json(value: Mapping[str, Any]) -> JSONMap:
    try:
        raw = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FuryFullPolicyRolloutV7Error(
            f"v7 rollout is not strict JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise FuryFullPolicyRolloutV7Error("v7 rollout must be an object")
    return raw


def _source_sha256(module: Any) -> str:
    return hashlib.sha256(Path(module.__file__).resolve().read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = (
    "CACHE_IDENTITY_SCHEMA_V7",
    "FuryFullPolicyRolloutV7Error",
    "IMPLEMENTATION_REVISION_V7",
    "ROLLOUT_SCHEMA_V7",
    "build_deployed_contra_lane_cache_identity_v7",
    "run_fury_full_policy_rollout_v7",
    "validate_fury_full_policy_rollout_v7",
)
