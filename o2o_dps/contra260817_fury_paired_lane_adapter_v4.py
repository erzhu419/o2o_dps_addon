"""Explicit runner-v4 registration for Contra260817's native-v3 diagnostic."""

from __future__ import annotations

from typing import Any, Mapping

from .contra260817_fury_full_policy_v3 import (
    Contra260817FuryFullPolicyAdapterV3,
)
from .contra260817_fury_full_policy_rollout_v4 import (
    ROLLOUT_SCHEMA_V4,
    run_contra260817_fury_full_policy_rollout_v4,
    validate_contra260817_fury_full_policy_rollout_v4,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from .fury_paired_multiseed_runner_v4 import (
    CONTRA260817_POLICY_ID,
    LANE_CONTRACT_SCHEMA_V4,
    FuryPairedRunnerV4Error,
    bind_dynamic_v5_load,
    build_lane_result_v4,
    sha256_json,
)


JSONMap = dict[str, Any]
CONTRA260817_V4_PRODUCER = "contra260817_fury_full_policy_rollout_v4"


class Contra260817FuryPairedLaneAdapterV4Error(FuryPairedRunnerV4Error):
    """The runner lane is not bound to Contra260817's exact diagnostic."""


def contra260817_runner_v4_lane_contract_v4() -> JSONMap:
    return {
        "schema": LANE_CONTRACT_SCHEMA_V4,
        "policy_id": CONTRA260817_POLICY_ID,
        "producer": CONTRA260817_V4_PRODUCER,
        "artifact_schema": ROLLOUT_SCHEMA_V4,
        "source_oracle_status": "CONTRA260817_SOURCE_ORACLE_V3_READY",
        "ordered_sink_status": "CONTRA260817_ORDERED_SINK_V4_READY_PATH_DEPENDENT",
        "full_policy_status": "CONTRA260817_SOURCE_DEFAULT_NATIVE_DYNAMIC_V3_DIAGNOSTIC_READY",
        "dynamic_v5_executable": True,
        "blocker_codes": [],
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Contra260817FuryPairedLaneAdapterV4Error(f"{label} must be an object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Contra260817FuryPairedLaneAdapterV4Error(f"{label} must be positive")
    return value


def _artifact_summary_v4(artifact: Mapping[str, Any]) -> JSONMap:
    blockers = artifact.get("blockers")
    if not isinstance(blockers, list):
        raise Contra260817FuryPairedLaneAdapterV4Error("Contra blockers malformed")
    reasons: dict[str, int] = {}
    fatal_count = 0
    for index, raw in enumerate(blockers):
        row = _mapping(raw, f"Contra blocker {index}")
        code = row.get("code")
        if not isinstance(code, str) or not code:
            raise Contra260817FuryPairedLaneAdapterV4Error("Contra blocker code invalid")
        reasons[code] = reasons.get(code, 0) + 1
        fatal_count += int(row.get("execution_fatal") is True)
    final_state = _mapping(artifact.get("final_state"), "Contra final_state")
    team = final_state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    all_dead = (
        isinstance(targets, list)
        and bool(targets)
        and all(isinstance(row, Mapping) and row.get("dead") is True for row in targets)
    )
    complete = artifact.get("scenario_complete") is True
    completion_mode = (
        "ALL_TARGETS_DEAD"
        if complete and all_dead
        else "SCENARIO_HORIZON_REACHED"
        if complete
        else "INCOMPLETE"
    )
    closure = _mapping(
        artifact.get("dynamic_v3_runtime_receipt_closure"),
        "Contra dynamic runtime closure",
    )
    runtime_complete = closure.get("status") == "COMPLETE_BOUND"
    source_order_complete = artifact.get("source_to_simulator_order_faithful") is True
    if not source_order_complete:
        code = "CONTRA260817_V4_SOURCE_TO_SIMULATOR_ORDER_INCOMPLETE"
        reasons[code] = reasons.get(code, 0) + 1
    damage = artifact.get("damage_delta")
    elapsed = artifact.get("elapsed_ms")
    if isinstance(damage, bool) or not isinstance(damage, (int, float)):
        raise Contra260817FuryPairedLaneAdapterV4Error("Contra damage invalid")
    elapsed_ms = _positive_int(elapsed, "Contra elapsed_ms")
    return {
        "policy_id": CONTRA260817_POLICY_ID,
        "artifact_schema": ROLLOUT_SCHEMA_V4,
        "completion_mode": completion_mode,
        "damage": float(damage),
        "elapsed_ms": elapsed_ms,
        "dps": float(damage) * 1000.0 / elapsed_ms,
        "completion_criterion_met": complete,
        "offline_score_eligible": (
            complete
            and runtime_complete
            and source_order_complete
            and fatal_count == 0
        ),
        "omitted_lane_count": fatal_count + int(not source_order_complete),
        "fatal_error_count": fatal_count,
        "nonfaithful_reason_counts": dict(sorted(reasons.items())),
        "end_state_sha256": sha256_json(final_state),
        "dynamic_runtime_receipts_complete": runtime_complete,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_contra260817_runner_v4_artifact_v4(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    validated = validate_contra260817_fury_full_policy_rollout_v4(
        artifact, dynamic_load=dynamic_load
    )
    if producer_runtime_receipt != validated.get("dynamic_v3_runtime_receipt_closure"):
        raise Contra260817FuryPairedLaneAdapterV4Error(
            "Contra producer runtime receipt differs from artifact"
        )
    return _artifact_summary_v4(validated)


def build_contra260817_runner_v4_lane_result_v4(
    artifact: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
) -> JSONMap:
    request = _mapping(scenario.get("request"), "scenario.request")
    seed = _positive_int(group.get("simulator_seed"), "group.simulator_seed")
    dynamic_load = bind_dynamic_v5_load(
        request,
        seed,
        _mapping(scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"),
    )
    validated = validate_contra260817_fury_full_policy_rollout_v4(
        artifact, dynamic_load=dynamic_load
    )
    summary = _artifact_summary_v4(validated)
    return build_lane_result_v4(
        policy_id=CONTRA260817_POLICY_ID,
        producer=CONTRA260817_V4_PRODUCER,
        artifact=validated,
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
        dynamic_runtime_receipts_complete=bool(summary["dynamic_runtime_receipts_complete"]),
        producer_runtime_receipt=validated["dynamic_v3_runtime_receipt_closure"],
    )


def execute_contra260817_runner_v4_lane_v4(
    bridge: Any,
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> JSONMap:
    if policy.get("policy_id") != CONTRA260817_POLICY_ID:
        raise Contra260817FuryPairedLaneAdapterV4Error(
            f"Contra v4 cannot execute policy {policy.get('policy_id')!r}"
        )
    request = _mapping(scenario.get("request"), "scenario.request")
    seed = _positive_int(group.get("simulator_seed"), "group.simulator_seed")
    dynamic_load = bind_dynamic_v5_load(
        request,
        seed,
        _mapping(scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"),
    )
    if group.get("dynamic_load_contract_sha256") != dynamic_load.contract_sha256:
        raise Contra260817FuryPairedLaneAdapterV4Error(
            "group dynamic-load contract differs from request/config/seed"
        )
    contexts = target_contexts_from_runner_v4(
        _mapping(scenario.get("target_context_bundle"), "scenario.target_context_bundle")
    )
    artifact = run_contra260817_fury_full_policy_rollout_v4(
        bridge,
        request,
        Contra260817FuryFullPolicyAdapterV3(),
        seed=seed,
        target_contexts=contexts,
        dynamic_load=dynamic_load,
    )
    return {
        "lane_result": build_contra260817_runner_v4_lane_result_v4(
            artifact, group=group, scenario=scenario
        )
    }


def contra260817_runner_v4_artifact_validators_v4() -> dict[str, Any]:
    return {
        CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4
    }


__all__ = (
    "CONTRA260817_V4_PRODUCER",
    "Contra260817FuryPairedLaneAdapterV4Error",
    "build_contra260817_runner_v4_lane_result_v4",
    "contra260817_runner_v4_artifact_validators_v4",
    "contra260817_runner_v4_lane_contract_v4",
    "execute_contra260817_runner_v4_lane_v4",
    "validate_contra260817_runner_v4_artifact_v4",
)
