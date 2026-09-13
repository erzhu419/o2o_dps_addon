"""Runner-v4 lane for the observable-state Cat terminal queue guard."""

from __future__ import annotations

from typing import Any, Mapping

from .cat_residual_candidate_rollout_v1 import POLICY_ID as OLD_ID, SCHEMA as OLD_SCHEMA
from .cat_residual_paired_lane_adapter_v1 import _summary as residual_structure_summary
from .cat_terminal_queue_guard_v1 import (
    POLICY_ID, SCHEMA, CatTerminalQueueGuardV1, run_cat_terminal_queue_guard_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from .fury_paired_multiseed_runner_v4 import (
    LANE_CONTRACT_SCHEMA_V4, bind_dynamic_v5_load, build_lane_result_v4,
)


PRODUCER = "cat_terminal_queue_guard_v1"


def cat_terminal_queue_guard_lane_contract_v1() -> dict[str, Any]:
    return {
        "schema": LANE_CONTRACT_SCHEMA_V4,
        "policy_id": POLICY_ID,
        "producer": PRODUCER,
        "artifact_schema": SCHEMA,
        "source_oracle_status": "CAT_V4_BASE_PROPOSAL_AVAILABLE",
        "ordered_sink_status": "CAT_V6_COMPATIBLE_CANDIDATE_SINK_EXECUTOR",
        "full_policy_status": "CAT_TERMINAL_GUARD_V1_NATIVE_DYNAMIC_V3_CANDIDATE",
        "dynamic_v5_executable": True,
        "blocker_codes": [],
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_cat_terminal_queue_guard_artifact_v1(
    artifact: Mapping[str, Any], producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> dict[str, Any]:
    if artifact.get("schema") != SCHEMA or artifact.get("expert_id") != POLICY_ID:
        raise ValueError("guarded candidate artifact identity mismatch")
    if producer_runtime_receipt != artifact.get("dynamic_v3_runtime_receipt_closure"):
        raise ValueError("guarded candidate runtime receipt mismatch")
    binding = artifact.get("dynamic_load_binding")
    if not isinstance(binding, Mapping) or binding.get("contract_sha256") != dynamic_load.contract_sha256:
        raise ValueError("guarded candidate dynamic load binding mismatch")
    opportunities = artifact.get("guarded_opportunities")
    if not isinstance(opportunities, list) or artifact.get("guarded_opportunity_count") != len(opportunities):
        raise ValueError("guarded opportunities ledger mismatch")
    if len({row["decision_index"] for row in opportunities}) != len(opportunities):
        raise ValueError("duplicate guarded decision")
    interventions = artifact.get("interventions")
    if not isinstance(interventions, list) or (
        {row["decision_index"] for row in opportunities}
        & {row["decision_index"] for row in interventions}
    ):
        raise ValueError("guarded decision also changed Cat proposal")
    # Reuse the existing structural/ordered-sink validation on a temporary
    # identity-neutral view.  The returned result is then relabeled with the
    # guarded producer; the actual artifact is never represented as old Cat.
    structural_view = dict(artifact, schema=OLD_SCHEMA, expert_id=OLD_ID)
    summary = residual_structure_summary(structural_view)
    summary["policy_id"] = POLICY_ID
    summary["artifact_schema"] = SCHEMA
    return summary


def execute_cat_terminal_queue_guard_lane_v1(
    bridge: Any, candidate: CatTerminalQueueGuardV1, *, group: Mapping[str, Any],
    scenario: Mapping[str, Any], policy: Mapping[str, Any],
) -> dict[str, Any]:
    if type(candidate) is not CatTerminalQueueGuardV1 or policy.get("policy_id") != POLICY_ID:
        raise ValueError("guarded lane requires exact candidate and policy identity")
    request = scenario["request"]
    seed = group["simulator_seed"]
    dynamic = bind_dynamic_v5_load(request, seed, scenario["dynamic_load_config"])
    if group.get("dynamic_load_contract_sha256") != dynamic.contract_sha256:
        raise ValueError("guarded group dynamic load differs from scenario")
    contexts = target_contexts_from_runner_v4(scenario["target_context_bundle"])
    artifact = run_cat_terminal_queue_guard_v1(
        bridge, request, candidate, seed=seed, target_contexts=contexts,
        dynamic_load=dynamic,
    )
    summary = validate_cat_terminal_queue_guard_artifact_v1(
        artifact, artifact["dynamic_v3_runtime_receipt_closure"], dynamic
    )
    lane = build_lane_result_v4(
        policy_id=POLICY_ID, producer=PRODUCER, artifact=artifact,
        request_sha256=scenario["request_sha256"], simulator_seed=seed,
        dynamic_load_contract_sha256=group["dynamic_load_contract_sha256"],
        completion_mode=summary["completion_mode"], damage=summary["damage"],
        elapsed_ms=summary["elapsed_ms"],
        completion_criterion_met=summary["completion_criterion_met"],
        offline_score_eligible=summary["offline_score_eligible"],
        omitted_lane_count=summary["omitted_lane_count"],
        fatal_error_count=summary["fatal_error_count"],
        nonfaithful_reason_counts=summary["nonfaithful_reason_counts"],
        end_state_sha256=summary["end_state_sha256"],
        dynamic_runtime_receipts_complete=summary["dynamic_runtime_receipts_complete"],
        producer_runtime_receipt=artifact["dynamic_v3_runtime_receipt_closure"],
    )
    return {"lane_result": lane}


__all__ = (
    "PRODUCER", "cat_terminal_queue_guard_lane_contract_v1",
    "validate_cat_terminal_queue_guard_artifact_v1",
    "execute_cat_terminal_queue_guard_lane_v1",
)
