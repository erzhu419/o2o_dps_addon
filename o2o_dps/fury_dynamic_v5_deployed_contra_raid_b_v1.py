"""Runner-v4 lane producer for native dual-wield deployed Contra Raid-B."""

from __future__ import annotations

from typing import Any, Mapping

from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from .fury_full_policy_rollout_raid_b_v1 import (
    SCHEMA as ARTIFACT_SCHEMA,
    run_fury_full_policy_raid_b_v1,
    validate_fury_full_policy_raid_b_v1,
)
from .fury_runtime_bound_deployed_contra_raid_b_v1 import POLICY_ID
from .fury_paired_multiseed_runner_v4 import (
    bind_dynamic_v5_load,
    build_lane_result_v4,
    sha256_json,
)


PRODUCER = "fury_full_policy_rollout_native_deployed_contra_raid_b_v1"


def validate_deployed_contra_raid_b_artifact_v1(
    artifact: Mapping[str, Any], producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: Any,
) -> dict[str, Any]:
    result = validate_fury_full_policy_raid_b_v1(artifact, dynamic_load=dynamic_load)
    closure = result["dynamic_v3_runtime_receipt_closure"]
    if producer_runtime_receipt != closure:
        raise ValueError("Raid-B producer receipt differs from native artifact")
    final = result["final_state"]
    lifecycle = final.get("dynamic_team_background", {})
    targets = lifecycle.get("targets", [])
    all_dead = bool(targets) and all(row.get("dead") is True for row in targets)
    complete = result["scenario_complete"] is True
    fatal = [row for row in result["blockers"] if row.get("execution_fatal") is True]
    reasons: dict[str, int] = {}
    for row in result["blockers"]:
        code = row["code"]
        reasons[code] = reasons.get(code, 0) + 1
    damage = float(result["damage_delta"])
    elapsed_ms = int(result["elapsed_ms"])
    return {
        "policy_id": POLICY_ID,
        "artifact_schema": ARTIFACT_SCHEMA,
        "completion_mode": (
            "ALL_TARGETS_DEAD" if complete and all_dead else
            "SCENARIO_HORIZON_REACHED" if complete else "INCOMPLETE"
        ),
        "damage": damage,
        "elapsed_ms": elapsed_ms,
        "dps": damage * 1000 / elapsed_ms if elapsed_ms > 0 else None,
        "completion_criterion_met": complete,
        "offline_score_eligible": complete and closure["status"] == "COMPLETE_BOUND" and not fatal,
        "omitted_lane_count": len(fatal),
        "fatal_error_count": len(fatal),
        "nonfaithful_reason_counts": dict(sorted(reasons.items())),
        "end_state_sha256": sha256_json(final),
        "dynamic_runtime_receipts_complete": closure["status"] == "COMPLETE_BOUND",
        "live_fidelity": False,
        "comparison_ready": False,
    }


def execute_deployed_contra_raid_b_lane_v1(
    bridge: Any,
    *, group: Mapping[str, Any], scenario: Mapping[str, Any],
    policy: Mapping[str, Any], runtime_binding: Mapping[str, Any],
) -> dict[str, Any]:
    if policy.get("policy_id") != POLICY_ID:
        raise ValueError("Raid-B lane requires deployed Contra policy identity")
    request = scenario["request"]
    seed = group["simulator_seed"]
    load = bind_dynamic_v5_load(request, seed, scenario["dynamic_load_config"])
    if group["dynamic_load_contract_sha256"] != load.contract_sha256:
        raise ValueError("Raid-B matched group dynamic load mismatch")
    contexts = target_contexts_from_runner_v4(scenario["target_context_bundle"])
    artifact = run_fury_full_policy_raid_b_v1(
        bridge, request, runtime_binding=runtime_binding, seed=seed,
        target_contexts=contexts, dynamic_load=load,
    )
    summary = validate_deployed_contra_raid_b_artifact_v1(
        artifact, artifact["dynamic_v3_runtime_receipt_closure"], load,
    )
    lane = build_lane_result_v4(
        policy_id=POLICY_ID,
        producer=PRODUCER,
        artifact=artifact,
        request_sha256=scenario["request_sha256"],
        simulator_seed=seed,
        dynamic_load_contract_sha256=group["dynamic_load_contract_sha256"],
        completion_mode=summary["completion_mode"],
        damage=summary["damage"],
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
    return {"lane_result": lane, "lane_cache_identity": artifact["lane_cache_identity"]}


__all__ = (
    "PRODUCER", "execute_deployed_contra_raid_b_lane_v1",
    "validate_deployed_contra_raid_b_artifact_v1",
)
