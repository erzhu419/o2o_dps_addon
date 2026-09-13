"""Runner-v4 lane for the executable Cat-relative residual candidate."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from .cat_residual_candidate_rollout_v1 import (
    POLICY_ID,
    SCHEMA,
    CatResidualCandidateV1,
    run_cat_residual_candidate_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from .fury_paired_multiseed_runner_v4 import (
    LANE_CONTRACT_SCHEMA_V4,
    bind_dynamic_v5_load,
    build_lane_result_v4,
    sha256_json,
)


PRODUCER = "cat_residual_candidate_rollout_v1"


def cat_residual_lane_contract_v1() -> dict[str, Any]:
    return {
        "schema": LANE_CONTRACT_SCHEMA_V4,
        "policy_id": POLICY_ID,
        "producer": PRODUCER,
        "artifact_schema": SCHEMA,
        "source_oracle_status": "CAT_V4_BASE_PROPOSAL_AVAILABLE",
        "ordered_sink_status": "CAT_V6_COMPATIBLE_CANDIDATE_SINK_EXECUTOR",
        "full_policy_status": "CAT_RESIDUAL_V1_NATIVE_DYNAMIC_V3_CANDIDATE",
        "dynamic_v5_executable": True,
        "blocker_codes": [],
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def _summary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    if artifact.get("schema") != SCHEMA or artifact.get("expert_id") != POLICY_ID:
        raise ValueError("residual candidate artifact identity mismatch")
    if artifact.get("cat_baseline_artifact") is not False:
        raise ValueError("residual candidate must not be labeled Cat baseline")
    steps = artifact.get("steps")
    interventions = artifact.get("interventions")
    if not isinstance(steps, list) or not isinstance(interventions, list):
        raise ValueError("residual candidate steps/interventions missing")
    if artifact.get("intervention_count") != len(interventions):
        raise ValueError("residual intervention count mismatch")
    by_index = {row["decision_index"]: row for row in interventions}
    if len(by_index) != len(interventions):
        raise ValueError("duplicate residual intervention index")
    if artifact.get("cat_source_order_claim") is not (not interventions):
        raise ValueError("Cat source-order claim must exclude changed decisions")
    for step in steps:
        index = step.get("decision_index")
        row = by_index.get(index)
        if row:
            if (
                step.get("policy_proposal_origin") != "CAT_RELATIVE_RESIDUAL"
                or step.get("cat_baseline_proposal") != row.get("cat_proposal")
                or step.get("proposal") != row.get("candidate_proposal")
            ):
                raise ValueError(f"residual step {index} lost Cat/candidate proposal binding")
        elif step.get("policy_proposal_origin") != "CAT_UNCHANGED":
            raise ValueError(f"unchanged step {index} has false residual origin")
    if set(by_index) != {
        step["decision_index"]
        for step in steps if step.get("policy_proposal_origin") == "CAT_RELATIVE_RESIDUAL"
    }:
        raise ValueError("residual ledger contains an unexecuted or missing intervention")
    # The generic core's ordered_projection_faithful also goes false for
    # typed, nonfatal simulator rejections.  That is not a missing candidate
    # sink and must not turn a legal residual intervention into an omission.
    order_complete = bool(steps) and all(
        isinstance(step.get("ordered_execution"), Mapping)
        and step["ordered_execution"].get("source_to_simulator_order_faithful") is True
        and step["ordered_execution"].get("raw_sink_order")
        == step.get("proposal", {}).get("raw_sink_order")
        and [event.get("source_sink") for event in step["ordered_execution"].get("sink_events", [])]
        == step.get("proposal", {}).get("raw_sink_order")
        for step in steps
    )
    blockers = artifact.get("blockers")
    if not isinstance(blockers, list):
        raise ValueError("residual candidate blockers missing")
    reasons: Counter[str] = Counter()
    fatal = 0
    for row in blockers:
        code = row.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("residual candidate blocker code missing")
        reasons[code] += 1
        fatal += int(row.get("execution_fatal") is True)
    final_state = artifact.get("final_state")
    if not isinstance(final_state, Mapping):
        raise ValueError("residual candidate final state missing")
    team = final_state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    all_dead = (
        isinstance(targets, list)
        and bool(targets)
        and all(isinstance(row, Mapping) and row.get("dead") is True for row in targets)
    )
    complete = artifact.get("scenario_complete") is True
    mode = (
        "ALL_TARGETS_DEAD" if complete and all_dead else
        "SCENARIO_HORIZON_REACHED" if complete else "INCOMPLETE"
    )
    closure = artifact.get("dynamic_v3_runtime_receipt_closure")
    if not isinstance(closure, Mapping):
        raise ValueError("residual dynamic-v3 receipt missing")
    runtime_complete = closure.get("status") == "COMPLETE_BOUND"
    if not order_complete:
        reasons["CANDIDATE_ORDERED_SINK_INCOMPLETE"] += 1
    damage = artifact.get("damage_delta")
    elapsed = artifact.get("elapsed_ms")
    if isinstance(damage, bool) or not isinstance(damage, (int, float)):
        raise ValueError("residual candidate damage missing")
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed <= 0:
        raise ValueError("residual candidate elapsed_ms missing")
    return {
        "policy_id": POLICY_ID,
        "artifact_schema": SCHEMA,
        "completion_mode": mode,
        "damage": float(damage),
        "elapsed_ms": elapsed,
        "dps": float(damage) * 1000.0 / elapsed,
        "completion_criterion_met": complete,
        "offline_score_eligible": (
            complete and runtime_complete and order_complete and fatal == 0
        ),
        "omitted_lane_count": fatal + int(not order_complete),
        "fatal_error_count": fatal,
        "nonfaithful_reason_counts": dict(sorted(reasons.items())),
        "end_state_sha256": sha256_json(final_state),
        "dynamic_runtime_receipts_complete": runtime_complete,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_cat_residual_runner_v4_artifact_v1(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> dict[str, Any]:
    if producer_runtime_receipt != artifact.get("dynamic_v3_runtime_receipt_closure"):
        raise ValueError("residual runtime receipt differs from artifact")
    binding = artifact.get("dynamic_load_binding")
    if not isinstance(binding, Mapping) or binding.get("contract_sha256") != dynamic_load.contract_sha256:
        raise ValueError("residual dynamic load binding mismatch")
    return _summary(artifact)


def execute_cat_residual_runner_v4_lane_v1(
    bridge: Any,
    candidate: CatResidualCandidateV1,
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    if type(candidate) is not CatResidualCandidateV1 or policy.get("policy_id") != POLICY_ID:
        raise ValueError("residual lane requires its exact candidate/policy identity")
    request = scenario["request"]
    seed = group["simulator_seed"]
    dynamic = bind_dynamic_v5_load(request, seed, scenario["dynamic_load_config"])
    if group.get("dynamic_load_contract_sha256") != dynamic.contract_sha256:
        raise ValueError("residual group dynamic load differs from scenario")
    contexts = target_contexts_from_runner_v4(scenario["target_context_bundle"])
    artifact = run_cat_residual_candidate_v1(
        bridge, request, candidate, seed=seed, target_contexts=contexts,
        dynamic_load=dynamic,
    )
    summary = validate_cat_residual_runner_v4_artifact_v1(
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
    "PRODUCER", "cat_residual_lane_contract_v1",
    "execute_cat_residual_runner_v4_lane_v1",
    "validate_cat_residual_runner_v4_artifact_v1",
)
