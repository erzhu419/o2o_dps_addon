"""Isolated native dynamic-v3 rollout for deployed Contra dual-wield Raid-B.

The source body is ``Contra_SCKBZ_B``, not the Raid-A body.  This development
lane inherits the native v8 ordered-sink/reentry mechanism, while keeping a
different adapter, controller identity, and content address.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import fury_full_policy_rollout_v7 as _v7
from . import fury_full_policy_rollout_v8 as _v8
from .fury_ordered_sink_executor_raid_b_v1 import (
    SCHEMA as EXECUTION_SCHEMA,
    audit_ordered_execution_raid_b_v1,
    execute_ordered_sinks_raid_b_v1,
)
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    EXPERT_ID, SCHEMA as ADAPTER_SCHEMA,
    RuntimeBoundContraRaidBAdapterV1,
)


SCHEMA = "fury_full_policy_simulator_rollout/raid_b_v1"
REVISION = "raid_b_v1.0_source_derived_dual_wield_native"
CONTENT_SCHEMA = "fury_full_policy_rollout_content/raid_b_v1"
CACHE_SCHEMA = "deployed_contra_lane_cache_identity/raid_b_v1"


class FuryFullPolicyRaidBError(RuntimeError):
    """The native Raid-B lane has invalid input or receipts."""


def _supported_adapter(adapter: Any) -> bool:
    return type(adapter) is RuntimeBoundContraRaidBAdapterV1 and adapter.expert_id == EXPERT_ID


_RUN_CORE = _v8._clone(
    _v8._RUN_V8_CORE,
    {
        "_supported_adapter": _supported_adapter,
        "execute_ordered_sinks_v2": execute_ordered_sinks_raid_b_v1,
        "_audit_ordered_execution": audit_ordered_execution_raid_b_v1,
    },
    "_run_fury_full_policy_raid_b_core",
)
_RUN_BASE = _v8._clone(
    _v8._RUN_V8_BASE,
    {"_RUN_V5_CORE": _RUN_CORE},
    "_run_fury_full_policy_raid_b_base",
)


def _content_address(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": CONTENT_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _v7._canonical_sha256({
            key: row for key, row in value.items() if key != "content_address"
        }),
    }


def _cache_identity(
    binding: Mapping[str, Any], result: Mapping[str, Any], dynamic_load: Any,
) -> dict[str, Any]:
    core = {
        "schema": CACHE_SCHEMA,
        "controller": "raid_b",
        "source_policy_id": binding["source_policy_id"],
        "adapter_schema": ADAPTER_SCHEMA,
        "runtime_binding_sha256": binding["binding_sha256"],
        "request_sha256": result["request_sha256"],
        "simulator_seed": result["seed"],
        "dynamic_load_contract_sha256": dynamic_load.contract_sha256,
        "rollout_schema": SCHEMA,
        "implementation_revision": REVISION,
    }
    return {**core, "sha256": _v7._canonical_sha256(core)}


def run_fury_full_policy_raid_b_v1(
    bridge: Any,
    raid_sim_request: Mapping[str, Any],
    *,
    runtime_binding: Mapping[str, Any],
    seed: int,
    target_contexts: Mapping[int, Any],
    dynamic_load: Any,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> dict[str, Any]:
    targets = raid_sim_request.get("encounter", {}).get("targets", [])
    if not isinstance(targets, list) or len(targets) < 2:
        raise FuryFullPolicyRaidBError("Raid-B requires a native multi-target request")
    binding = _v7._validated_binding(runtime_binding)
    adapter = RuntimeBoundContraRaidBAdapterV1(binding)
    result = _RUN_BASE(
        bridge, raid_sim_request, adapter,
        seed=seed, target_contexts=target_contexts, dynamic_load=dynamic_load,
        max_decisions=max_decisions, max_advances=max_advances,
        retain_steps=True,
    )
    if not isinstance(result, dict) or not isinstance(result.get("steps"), list):
        raise FuryFullPolicyRaidBError("Raid-B base did not retain native steps")
    reentry_count = 0
    for step in result["steps"]:
        ordered = step["ordered_execution"]
        clock = ordered.get("source_reentry_clock")
        if isinstance(clock, dict):
            next_epoch = step.get("simulator_state_next_epoch", {})
            next_time = next_epoch.get("time_ms")
            if type(next_time) is not int or next_time < clock["scheduled_at_time_ms"]:
                raise FuryFullPolicyRaidBError("Raid-B source reentry clock did not close")
            clock["actual_next_epoch_time_ms"] = next_time
            reentry_count += 1
    result["schema"] = SCHEMA
    result["implementation_revision"] = REVISION
    result["deployed_contra_runtime_binding"] = binding
    result["controller_coverage"] = {
        "selected": "raid_b",
        "implemented": ["raid_b_dual_wield"],
        "unimplemented": ["raid_b_two_hand"],
        "complete_deployed_contra_controller_coverage": False,
    }
    result["lane_cache_identity"] = _cache_identity(binding, result, dynamic_load)
    result["bridge_command_contract"]["deployed_contra_controller"] = "raid_b"
    result["bridge_command_contract"]["ordered_sink_executor_schema"] = EXECUTION_SCHEMA
    result.setdefault("authority_boundary", {}).update({
        "runtime_binding_consumed": True,
        "source_identity_bound": True,
        "same_character_runtime_parity_proven": False,
        "public_macro_entry_verified": False,
        "client_execution_observed": False,
        "comparison_ready": False,
    })
    result["version_isolation"].update({
        "raid_b_adapter_schema": ADAPTER_SCHEMA,
        "ordered_sink_executor_schema": EXECUTION_SCHEMA,
    })
    result["development_reentry"] = {
        "source_reentry_count": reentry_count,
        "timing_authority": "RUNNER_FIXED_100MS_PROXY",
        "exact_client_cadence": False,
    }
    result["content_address"] = _content_address(result)
    return validate_fury_full_policy_raid_b_v1(result, dynamic_load=dynamic_load)


def validate_fury_full_policy_raid_b_v1(
    value: Mapping[str, Any], *, dynamic_load: Any | None = None,
) -> dict[str, Any]:
    raw = _v7._strict_json(value)
    if (raw.get("schema") != SCHEMA or raw.get("implementation_revision") != REVISION
            or raw.get("expert_id") != EXPERT_ID
            or raw.get("content_address") != _content_address(raw)):
        raise FuryFullPolicyRaidBError("Raid-B rollout identity mismatch")
    binding = _v7._validated_binding(raw["deployed_contra_runtime_binding"])
    coverage = raw.get("controller_coverage")
    if not isinstance(coverage, Mapping) or coverage.get("selected") != "raid_b":
        raise FuryFullPolicyRaidBError("Raid-B controller coverage mismatch")
    command = raw.get("bridge_command_contract")
    if (not isinstance(command, Mapping)
            or command.get("deployed_contra_controller") != "raid_b"
            or command.get("ordered_sink_executor_schema") != EXECUTION_SCHEMA):
        raise FuryFullPolicyRaidBError("Raid-B native command identity mismatch")
    if dynamic_load is not None and raw.get("lane_cache_identity") != _cache_identity(
        binding, raw, dynamic_load
    ):
        raise FuryFullPolicyRaidBError("Raid-B lane cache identity mismatch")
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise FuryFullPolicyRaidBError("Raid-B retained steps missing")
    for index, step in enumerate(steps):
        proposal = step.get("proposal") if isinstance(step, Mapping) else None
        ordered = step.get("ordered_execution") if isinstance(step, Mapping) else None
        metadata = proposal.get("metadata") if isinstance(proposal, Mapping) else None
        if (not isinstance(metadata, Mapping) or metadata.get("controller") != "raid_b"
                or metadata.get("runtime_binding_sha256") != binding["binding_sha256"]
                or not isinstance(ordered, Mapping)
                or ordered.get("schema") != EXECUTION_SCHEMA):
            raise FuryFullPolicyRaidBError(f"Raid-B step {index} lost source binding")
    return raw


__all__ = (
    "FuryFullPolicyRaidBError", "SCHEMA", "run_fury_full_policy_raid_b_v1",
    "validate_fury_full_policy_raid_b_v1",
)
