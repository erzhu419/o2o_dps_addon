"""Dynamic-v3 Fury rollout using deployed-Contra ordered executor v3.

The v2 execution base and v5 dynamic overlay are published byte identities.
V6 privately clones their function namespaces, replaces only the ordered-sink
executor and its independent audit, and records the new implementation
identity without mutating either frozen module.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types
from typing import Any, Mapping

from . import fury_full_policy_rollout_v5 as _v5
from .fury_full_policy_rollout_v3 import validate_v2_execution_base_identity_v3
from .fury_ordered_sink_executor_v3 import (
    EXECUTION_SCHEMA_V3,
    audit_ordered_execution_v3,
    execute_ordered_sinks_v3,
)
from .fury_full_policy_rollout_v5 import DynamicRolloutLoadV3


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V6 = "fury_full_policy_simulator_rollout/v6"
IMPLEMENTATION_REVISION_V6 = "v6.0_deployed_contra_noop_and_delayed_queue"
ROLLOUT_CONTENT_ADDRESS_SCHEMA_V6 = "fury_full_policy_rollout_content/v6"


class FuryFullPolicyRolloutV6Error(RuntimeError):
    """The version-isolated deployed-Contra v6 rollout is malformed."""


def _clone_v5_core_v6() -> Any:
    source = _v5._RUN_V5_CORE
    namespace = dict(source.__globals__)
    namespace.update(
        {
            "execute_ordered_sinks_v2": execute_ordered_sinks_v3,
            "_audit_ordered_execution": audit_ordered_execution_v3,
        }
    )
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v6_core",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V6_CORE = _clone_v5_core_v6()


def _clone_v5_wrapper_v6() -> Any:
    source = _v5.run_fury_full_policy_rollout_v5
    namespace = dict(source.__globals__)
    namespace.update(
        {
            "_RUN_V5_CORE": _RUN_V6_CORE,
            # The v6 wrapper validates the rewritten identity after the frozen
            # v5 finalization and receipt collection have completed.
            "validate_fury_full_policy_rollout_v5": lambda value, **_: value,
        }
    )
    cloned = types.FunctionType(
        source.__code__,
        namespace,
        name="_run_fury_full_policy_rollout_v6_base",
        argdefs=source.__defaults__,
        closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V6_BASE = _clone_v5_wrapper_v6()


def run_fury_full_policy_rollout_v6(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    adapter: Any,
    *,
    seed: int,
    target_contexts: Mapping[int, Any],
    dynamic_load: DynamicRolloutLoadV3,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    result = _RUN_V6_BASE(
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
        raise FuryFullPolicyRolloutV6Error("v6 base returned a non-object artifact")
    result["schema"] = ROLLOUT_SCHEMA_V6
    result["implementation_revision"] = IMPLEMENTATION_REVISION_V6
    isolation = result.get("version_isolation")
    if not isinstance(isolation, dict):
        raise FuryFullPolicyRolloutV6Error("v6 base lacks version isolation")
    isolation.update(
        {
            "frozen_v5_overlay_sha256": _source_sha256(_v5),
            "ordered_sink_executor_schema": EXECUTION_SCHEMA_V3,
            "ordered_sink_executor_v3_sha256": _source_sha256(
                __import__(
                    "o2o_dps.fury_ordered_sink_executor_v3",
                    fromlist=["ignored"],
                )
            ),
        }
    )
    command = result.get("bridge_command_contract")
    if not isinstance(command, dict):
        raise FuryFullPolicyRolloutV6Error("v6 base lacks bridge command contract")
    command["ordered_sink_executor_schema"] = EXECUTION_SCHEMA_V3
    core = {key: value for key, value in result.items() if key != "content_address"}
    result["content_address"] = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V6,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(core),
    }
    return validate_fury_full_policy_rollout_v6(result, dynamic_load=dynamic_load)


def validate_fury_full_policy_rollout_v6(
    value: Mapping[str, Any],
    *,
    dynamic_load: DynamicRolloutLoadV3 | None = None,
) -> JSONMap:
    raw = _strict_json(value)
    if (
        raw.get("schema") != ROLLOUT_SCHEMA_V6
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION_V6
    ):
        raise FuryFullPolicyRolloutV6Error("v6 rollout identity mismatch")
    isolation = raw.get("version_isolation")
    executor_module = __import__(
        "o2o_dps.fury_ordered_sink_executor_v3", fromlist=["ignored"]
    )
    expected_isolation = {
        "frozen_v2_source_sha256": validate_v2_execution_base_identity_v3(),
        "frozen_v4_overlay_sha256": _v5._frozen_v4_source_sha256(),
        "private_function_namespace_clone": True,
        "global_monkeypatch": False,
        "old_source_bytes_modified": False,
        "actual_process_load_command": "load_dynamic_v3",
        "frozen_v5_overlay_sha256": _source_sha256(_v5),
        "ordered_sink_executor_schema": EXECUTION_SCHEMA_V3,
        "ordered_sink_executor_v3_sha256": _source_sha256(executor_module),
    }
    if isolation != expected_isolation:
        raise FuryFullPolicyRolloutV6Error("v6 version isolation receipt mismatch")
    command = raw.get("bridge_command_contract")
    if not isinstance(command, Mapping) or (
        command.get("ordered_sink_executor_schema") != EXECUTION_SCHEMA_V3
    ):
        raise FuryFullPolicyRolloutV6Error("v6 executor command binding is missing")
    if raw.get("steps_retained") is True:
        steps = raw.get("steps")
        if not isinstance(steps, list):
            raise FuryFullPolicyRolloutV6Error("retained v6 steps are malformed")
        for step in steps:
            ordered = step.get("ordered_execution") if isinstance(step, Mapping) else None
            semantics = ordered.get("v3_semantics") if isinstance(ordered, Mapping) else None
            if (
                not isinstance(ordered, Mapping)
                or ordered.get("schema") != EXECUTION_SCHEMA_V3
                or not isinstance(semantics, Mapping)
                or semantics.get("fallback_used") is not False
            ):
                raise FuryFullPolicyRolloutV6Error(
                    "retained v6 step lacks ordered-executor v3 evidence"
                )

    projected = deepcopy(raw)
    projected["schema"] = _v5.ROLLOUT_SCHEMA_V5
    projected["implementation_revision"] = _v5.IMPLEMENTATION_REVISION
    projected["version_isolation"] = {
        key: expected_isolation[key]
        for key in (
            "frozen_v2_source_sha256",
            "frozen_v4_overlay_sha256",
            "private_function_namespace_clone",
            "global_monkeypatch",
            "old_source_bytes_modified",
            "actual_process_load_command",
        )
    }
    projected["bridge_command_contract"].pop("ordered_sink_executor_schema", None)
    projected_core = {
        key: item for key, item in projected.items() if key != "content_address"
    }
    projected["content_address"] = {
        "schema": _v5.ROLLOUT_CONTENT_ADDRESS_SCHEMA_V5,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _v5._canonical_sha256_v5(projected_core),
    }
    _v5.validate_fury_full_policy_rollout_v5(
        projected,
        dynamic_load=dynamic_load,
    )
    expected_content = {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V6,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _canonical_sha256(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }
    if raw.get("content_address") != expected_content:
        raise FuryFullPolicyRolloutV6Error("v6 rollout content address mismatch")
    return raw


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
        raise FuryFullPolicyRolloutV6Error(f"v6 rollout is not strict JSON: {error}") from error
    if not isinstance(raw, dict):
        raise FuryFullPolicyRolloutV6Error("v6 rollout must be an object")
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
    "FuryFullPolicyRolloutV6Error",
    "IMPLEMENTATION_REVISION_V6",
    "ROLLOUT_SCHEMA_V6",
    "run_fury_full_policy_rollout_v6",
    "validate_fury_full_policy_rollout_v6",
)
