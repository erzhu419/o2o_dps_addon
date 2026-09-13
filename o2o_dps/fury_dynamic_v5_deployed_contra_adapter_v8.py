"""Runner-v4 lane for development-only deployed-Contra v8 reentry."""

from __future__ import annotations

from typing import Any, Mapping

from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from .fury_full_policy_rollout_v8 import (
    ROLLOUT_SCHEMA_V8,
    run_fury_full_policy_rollout_v8,
    validate_fury_full_policy_rollout_v8,
)
from .fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    bind_dynamic_v5_load,
    build_lane_result_v4,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import RAID_A_CONTROLLER
from . import fury_dynamic_v5_deployed_contra_adapter_v7 as _v7


PRODUCER_V8 = "fury_full_policy_rollout_v8_runtime_bound_contra_reentry"


def validate_deployed_contra_v8_artifact_v8(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: Any,
) -> dict[str, Any]:
    validated = validate_fury_full_policy_rollout_v8(
        artifact, dynamic_load=dynamic_load
    )
    if producer_runtime_receipt != validated.get(
        "dynamic_v3_runtime_receipt_closure"
    ):
        raise ValueError("v8 producer runtime receipt differs from artifact")
    summary = _v7._artifact_summary_v7(validated)
    summary["artifact_schema"] = ROLLOUT_SCHEMA_V8
    return summary


def execute_deployed_contra_v8_lane_v8(
    bridge: Any,
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
    runtime_binding: Mapping[str, Any],
    controller: str = RAID_A_CONTROLLER,
) -> dict[str, Any]:
    if policy.get("policy_id") != CONTRA_DEPLOYED_POLICY_ID:
        raise ValueError("v8 lane requires deployed Contra policy identity")
    request = _v7._mapping(scenario.get("request"), "scenario.request")
    seed = _v7._positive_int(group.get("simulator_seed"), "group.simulator_seed")
    dynamic_load = bind_dynamic_v5_load(
        request,
        seed,
        _v7._mapping(
            scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"
        ),
    )
    if group.get("dynamic_load_contract_sha256") != dynamic_load.contract_sha256:
        raise ValueError("v8 group dynamic load differs from scenario and seed")
    contexts = target_contexts_from_runner_v4(
        _v7._mapping(
            scenario.get("target_context_bundle"), "scenario.target_context_bundle"
        )
    )
    artifact = run_fury_full_policy_rollout_v8(
        bridge,
        request,
        runtime_binding=runtime_binding,
        seed=seed,
        target_contexts=contexts,
        dynamic_load=dynamic_load,
        controller=controller,
    )
    summary = validate_deployed_contra_v8_artifact_v8(
        artifact,
        artifact["dynamic_v3_runtime_receipt_closure"],
        dynamic_load,
    )
    lane = build_lane_result_v4(
        policy_id=CONTRA_DEPLOYED_POLICY_ID,
        producer=PRODUCER_V8,
        artifact=artifact,
        request_sha256=str(scenario["request_sha256"]),
        simulator_seed=seed,
        dynamic_load_contract_sha256=str(group["dynamic_load_contract_sha256"]),
        completion_mode=str(summary["completion_mode"]),
        damage=float(summary["damage"]),
        elapsed_ms=int(summary["elapsed_ms"]),
        completion_criterion_met=bool(summary["completion_criterion_met"]),
        offline_score_eligible=bool(summary["offline_score_eligible"]),
        omitted_lane_count=int(summary["omitted_lane_count"]),
        fatal_error_count=int(summary["fatal_error_count"]),
        nonfaithful_reason_counts=summary["nonfaithful_reason_counts"],
        end_state_sha256=str(summary["end_state_sha256"]),
        dynamic_runtime_receipts_complete=bool(
            summary["dynamic_runtime_receipts_complete"]
        ),
        producer_runtime_receipt=artifact["dynamic_v3_runtime_receipt_closure"],
    )
    return {
        "lane_result": lane,
        "lane_cache_identity": artifact["lane_cache_identity"],
    }


__all__ = (
    "PRODUCER_V8",
    "execute_deployed_contra_v8_lane_v8",
    "validate_deployed_contra_v8_artifact_v8",
)
