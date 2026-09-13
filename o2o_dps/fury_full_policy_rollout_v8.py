"""Development-only deployed-Contra Raid-A rollout with typed low-HP reentry.

This is an additive diagnostic identity.  The frozen v2 verdict and the v7
runtime-bound lane are not rewritten.  The 100 ms clock is runner-side and
does not assert observed player input cadence or live policy parity.
"""

from __future__ import annotations

from copy import deepcopy
import types
from typing import Any, Mapping

from . import fury_full_policy_rollout_v7 as _v7
from .fury_ordered_sink_executor_v5 import (
    EXECUTION_SCHEMA_V5,
    SOURCE_REENTRY_CLOCK_SCHEMA_V5,
    audit_ordered_execution_v5,
    execute_ordered_sinks_v5,
)
from .fury_ordered_sink_executor_v4 import EXECUTION_SCHEMA_V4


JSONMap = dict[str, Any]
ROLLOUT_SCHEMA_V8 = "fury_full_policy_simulator_rollout/v8"
IMPLEMENTATION_REVISION_V8 = "v8.1_deployed_contra_low_hp_resource_runner_reentry"
ROLLOUT_CONTENT_ADDRESS_SCHEMA_V8 = "fury_full_policy_rollout_content/v8"
CACHE_IDENTITY_SCHEMA_V8 = "deployed_contra_lane_cache_identity/v8"


class FuryFullPolicyRolloutV8Error(RuntimeError):
    """The development-only deployed Contra v8 artifact is malformed."""


def _clone(source: Any, updates: Mapping[str, Any], name: str) -> Any:
    namespace = dict(source.__globals__)
    namespace.update(updates)
    cloned = types.FunctionType(
        source.__code__, namespace, name=name,
        argdefs=source.__defaults__, closure=source.__closure__,
    )
    cloned.__kwdefaults__ = dict(source.__kwdefaults__ or {})
    return cloned


_RUN_V8_CORE = _clone(
    _v7._RUN_V7_CORE,
    {
        "execute_ordered_sinks_v2": execute_ordered_sinks_v5,
        "_audit_ordered_execution": audit_ordered_execution_v5,
    },
    "_run_fury_full_policy_rollout_v8_core",
)
_RUN_V8_BASE = _clone(
    _v7._RUN_V7_BASE,
    {"_RUN_V5_CORE": _RUN_V8_CORE},
    "_run_fury_full_policy_rollout_v8_base",
)


def build_deployed_contra_lane_cache_identity_v8(
    *,
    runtime_binding: Mapping[str, Any],
    request_sha256: str,
    simulator_seed: int,
    dynamic_load_contract_sha256: str,
    controller: str = _v7.RAID_A_CONTROLLER,
) -> JSONMap:
    identity = _v7.build_deployed_contra_lane_cache_identity_v7(
        runtime_binding=runtime_binding,
        request_sha256=request_sha256,
        simulator_seed=simulator_seed,
        dynamic_load_contract_sha256=dynamic_load_contract_sha256,
        controller=controller,
    )
    identity.pop("sha256")
    identity["schema"] = CACHE_IDENTITY_SCHEMA_V8
    identity["rollout_schema"] = ROLLOUT_SCHEMA_V8
    identity["implementation_revision"] = IMPLEMENTATION_REVISION_V8
    identity["ordered_sink_executor_schema"] = EXECUTION_SCHEMA_V5
    identity["sha256"] = _v7._canonical_sha256(identity)
    return identity


_RUN_V8_OUTER = _clone(
    _v7.run_fury_full_policy_rollout_v7,
    {
        "_RUN_V7_BASE": _RUN_V8_BASE,
        "ROLLOUT_SCHEMA_V7": ROLLOUT_SCHEMA_V8,
        "IMPLEMENTATION_REVISION_V7": IMPLEMENTATION_REVISION_V8,
        "ROLLOUT_CONTENT_ADDRESS_SCHEMA_V7": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V8,
        "EXECUTION_SCHEMA_V4": EXECUTION_SCHEMA_V5,
        "build_deployed_contra_lane_cache_identity_v7":
            build_deployed_contra_lane_cache_identity_v8,
        "validate_fury_full_policy_rollout_v7": lambda result, **_: result,
    },
    "_run_fury_full_policy_rollout_v8_outer",
)


def _content_address_v8(result: Mapping[str, Any]) -> JSONMap:
    core = {key: value for key, value in result.items() if key != "content_address"}
    return {
        "schema": ROLLOUT_CONTENT_ADDRESS_SCHEMA_V8,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _v7._canonical_sha256(core),
    }


def run_fury_full_policy_rollout_v8(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    runtime_binding: Mapping[str, Any],
    seed: int,
    target_contexts: Mapping[int, Any],
    dynamic_load: Any,
    controller: str = _v7.RAID_A_CONTROLLER,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
    retain_steps: bool = True,
) -> JSONMap:
    if retain_steps is not True:
        raise FuryFullPolicyRolloutV8Error(
            "development-only v8 requires retained rejection/reentry evidence"
        )
    result = _RUN_V8_OUTER(
        bridge, raid_sim_request,
        runtime_binding=runtime_binding,
        seed=seed,
        target_contexts=target_contexts,
        dynamic_load=dynamic_load,
        controller=controller,
        max_decisions=max_decisions,
        max_advances=max_advances,
        retain_steps=retain_steps,
    )
    if not isinstance(result, dict):
        raise FuryFullPolicyRolloutV8Error("v8 core returned a non-object")
    steps = result.get("steps")
    if not isinstance(steps, list):
        raise FuryFullPolicyRolloutV8Error("v8 retained steps are missing")
    count = 0
    for step in steps:
        if not isinstance(step, Mapping):
            raise FuryFullPolicyRolloutV8Error("v8 step is malformed")
        ordered = step.get("ordered_execution")
        clock = ordered.get("source_reentry_clock") if isinstance(ordered, dict) else None
        if not isinstance(clock, dict):
            continue
        next_epoch = step.get("simulator_state_next_epoch")
        next_time = next_epoch.get("time_ms") if isinstance(next_epoch, Mapping) else None
        if type(next_time) is not int or next_time < clock["scheduled_at_time_ms"]:
            raise FuryFullPolicyRolloutV8Error("source reentry next epoch is invalid")
        clock["actual_next_epoch_time_ms"] = next_time
        count += 1
    result["development_reentry"] = {
        "schema": "deployed_contra_low_hp_resource_reentry_summary/v8",
        "source_reentry_count": count,
        "timing_authority": "RUNNER_FIXED_100MS_PROXY",
        "policy_action_count": 0,
        "source_sink_count": 0,
        "exact_client_cadence": False,
        "comparison_ready": False,
    }
    result["version_isolation"]["ordered_sink_executor_v5_sha256"] = (
        _v7._source_sha256(__import__(
            "o2o_dps.fury_ordered_sink_executor_v5", fromlist=["ignored"]
        ))
    )
    result["content_address"] = _content_address_v8(result)
    return validate_fury_full_policy_rollout_v8(result, dynamic_load=dynamic_load)


def validate_fury_full_policy_rollout_v8(
    value: Mapping[str, Any], *, dynamic_load: Any | None = None
) -> JSONMap:
    raw = _v7._strict_json(value)
    if (
        raw.get("schema") != ROLLOUT_SCHEMA_V8
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION_V8
        or raw.get("content_address") != _content_address_v8(raw)
    ):
        raise FuryFullPolicyRolloutV8Error("v8 artifact identity mismatch")
    summary = raw.get("development_reentry")
    steps = raw.get("steps")
    if not isinstance(summary, Mapping) or not isinstance(steps, list):
        raise FuryFullPolicyRolloutV8Error("v8 reentry evidence is missing")
    count = 0
    for index, step in enumerate(steps):
        ordered = step.get("ordered_execution") if isinstance(step, Mapping) else None
        proposal = step.get("proposal") if isinstance(step, Mapping) else None
        if not isinstance(ordered, Mapping) or not isinstance(proposal, Mapping):
            raise FuryFullPolicyRolloutV8Error("v8 step evidence is malformed")
        if ordered.get("schema") != EXECUTION_SCHEMA_V5:
            raise FuryFullPolicyRolloutV8Error("v8 step executor identity mismatch")
        clock = ordered.get("source_reentry_clock")
        if clock is None:
            continue
        count += 1
        next_epoch = step.get("simulator_state_next_epoch")
        next_time = next_epoch.get("time_ms") if isinstance(next_epoch, Mapping) else None
        if (
            not isinstance(clock, Mapping)
            or clock.get("schema") != SOURCE_REENTRY_CLOCK_SCHEMA_V5
            or clock.get("actual_next_epoch_time_ms") != next_time
            or clock.get("exact_client_cadence") is not False
            or clock.get("policy_action") is not False
            or clock.get("source_sink") is not False
        ):
            raise FuryFullPolicyRolloutV8Error(
                f"v8 step {index} source reentry clock invalid"
            )
    if (
        summary.get("source_reentry_count") != count
        or summary.get("timing_authority") != "RUNNER_FIXED_100MS_PROXY"
        or summary.get("policy_action_count") != 0
        or summary.get("source_sink_count") != 0
        or summary.get("exact_client_cadence") is not False
        or summary.get("comparison_ready") is not False
    ):
        raise FuryFullPolicyRolloutV8Error("v8 reentry summary mismatch")

    projected = deepcopy(raw)
    projected.pop("development_reentry")
    projected["schema"] = _v7.ROLLOUT_SCHEMA_V7
    projected["implementation_revision"] = _v7.IMPLEMENTATION_REVISION_V7
    projected["lane_cache_identity"] = _v7.build_deployed_contra_lane_cache_identity_v7(
        runtime_binding=projected["deployed_contra_runtime_binding"],
        request_sha256=projected["request_sha256"],
        simulator_seed=projected["seed"],
        dynamic_load_contract_sha256=projected["dynamic_load_binding"]["contract_sha256"],
    )
    projected["version_isolation"].pop("ordered_sink_executor_v5_sha256", None)
    projected["version_isolation"]["ordered_sink_executor_schema"] = (
        EXECUTION_SCHEMA_V4
    )
    projected["bridge_command_contract"]["ordered_sink_executor_schema"] = (
        EXECUTION_SCHEMA_V4
    )
    for step in projected["steps"]:
        ordered = step["ordered_execution"]
        ordered["schema"] = EXECUTION_SCHEMA_V4
        ordered["base_executor_schema"] = "fury_ordered_sink_execution/v3"
        ordered.pop("source_reentry_clock", None)
        ordered.pop("v5_semantics", None)
    projected["content_address"] = {
        "schema": _v7.ROLLOUT_CONTENT_ADDRESS_SCHEMA_V7,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _v7._canonical_sha256({
            key: item for key, item in projected.items() if key != "content_address"
        }),
    }
    _v7.validate_fury_full_policy_rollout_v7(projected, dynamic_load=dynamic_load)
    return raw


__all__ = (
    "ROLLOUT_SCHEMA_V8",
    "run_fury_full_policy_rollout_v8",
    "validate_fury_full_policy_rollout_v8",
    "build_deployed_contra_lane_cache_identity_v8",
)
