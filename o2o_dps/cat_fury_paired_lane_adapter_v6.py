"""Runner-v4 lane adapter for Cat's native dynamic-v3 full policy."""

from __future__ import annotations

from typing import Any, Mapping

from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from .cat_fury_full_policy_rollout_v6 import (
    ROLLOUT_SCHEMA_V6,
    run_cat_fury_full_policy_rollout_v6,
    validate_cat_fury_full_policy_rollout_v6,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_dynamic_v5_baseline_adapter_v4 import (
    target_contexts_from_runner_v4,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    LANE_CONTRACT_SCHEMA_V4,
    FuryPairedRunnerV4Error,
    bind_dynamic_v5_load,
    build_lane_result_v4,
    sha256_json,
)


JSONMap = dict[str, Any]
CAT_V6_PRODUCER = "cat_fury_full_policy_rollout_v6"


class CatFuryPairedLaneAdapterV6Error(FuryPairedRunnerV4Error):
    """A Cat lane is not bound to its exact native-v3 contract."""


def cat_runner_v4_lane_contract_v6() -> JSONMap:
    """Return the explicit simulator-only registration for Cat v6."""

    return {
        "schema": LANE_CONTRACT_SCHEMA_V4,
        "policy_id": CAT_POLICY_ID,
        "producer": CAT_V6_PRODUCER,
        "artifact_schema": ROLLOUT_SCHEMA_V6,
        "source_oracle_status": "CAT_SOURCE_ORACLE_V4_READY",
        "ordered_sink_status": "CAT_ORDERED_SINK_V6_SOURCE_REENTRY_READY",
        "full_policy_status": (
            "CAT_FULL_POLICY_V6_1_NATIVE_DYNAMIC_V3_SOURCE_REENTRY_READY"
        ),
        "dynamic_v5_executable": True,
        "blocker_codes": [],
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatFuryPairedLaneAdapterV6Error(f"{label} must be an object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatFuryPairedLaneAdapterV6Error(f"{label} must be a positive integer")
    return value


def _artifact_summary_v6(artifact: Mapping[str, Any]) -> JSONMap:
    blockers = artifact.get("blockers")
    if not isinstance(blockers, list):
        raise CatFuryPairedLaneAdapterV6Error("Cat v6 blockers are malformed")
    reasons: dict[str, int] = {}
    fatal_count = 0
    for index, raw in enumerate(blockers):
        row = _mapping(raw, f"Cat v6 blocker {index}")
        code = row.get("code")
        if not isinstance(code, str) or not code:
            raise CatFuryPairedLaneAdapterV6Error("Cat v6 blocker code is invalid")
        reasons[code] = reasons.get(code, 0) + 1
        fatal_count += int(row.get("execution_fatal") is True)

    final_state = _mapping(artifact.get("final_state"), "Cat v6 final_state")
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
        "Cat v6 runtime closure",
    )
    runtime_complete = closure.get("status") == "COMPLETE_BOUND"
    source_order_complete = artifact.get("source_to_simulator_order_faithful") is True
    order_omission = int(not source_order_complete)
    if order_omission:
        code = "CAT_V6_SOURCE_TO_SIMULATOR_ORDER_INCOMPLETE"
        reasons[code] = reasons.get(code, 0) + 1
    damage = artifact.get("damage_delta")
    elapsed = artifact.get("elapsed_ms")
    if isinstance(damage, bool) or not isinstance(damage, (int, float)):
        raise CatFuryPairedLaneAdapterV6Error("Cat v6 damage_delta is invalid")
    elapsed_ms = _positive_int(elapsed, "Cat v6 elapsed_ms")
    return {
        "policy_id": CAT_POLICY_ID,
        "artifact_schema": ROLLOUT_SCHEMA_V6,
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
        "omitted_lane_count": fatal_count + order_omission,
        "fatal_error_count": fatal_count,
        "nonfaithful_reason_counts": dict(sorted(reasons.items())),
        "end_state_sha256": sha256_json(final_state),
        "dynamic_runtime_receipts_complete": runtime_complete,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_cat_runner_v4_artifact_v6(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    """Validate Cat v6 and return runner-v4's exact producer summary."""

    validated = validate_cat_fury_full_policy_rollout_v6(
        artifact, dynamic_load=dynamic_load
    )
    if producer_runtime_receipt != validated.get(
        "dynamic_v3_runtime_receipt_closure"
    ):
        raise CatFuryPairedLaneAdapterV6Error(
            "Cat producer runtime receipt differs from its artifact"
        )
    return _artifact_summary_v6(validated)


def build_cat_runner_v4_lane_result_v6(
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
        _mapping(
            scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"
        ),
    )
    validated = validate_cat_fury_full_policy_rollout_v6(
        artifact, dynamic_load=dynamic_load
    )
    summary = _artifact_summary_v6(validated)
    return build_lane_result_v4(
        policy_id=CAT_POLICY_ID,
        producer=CAT_V6_PRODUCER,
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
        dynamic_runtime_receipts_complete=bool(
            summary["dynamic_runtime_receipts_complete"]
        ),
        producer_runtime_receipt=validated[
            "dynamic_v3_runtime_receipt_closure"
        ],
    )


def execute_cat_runner_v4_lane_v6(
    bridge: Any,
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> JSONMap:
    """Execute one runner-v4 Cat lane without generic-policy substitution."""

    if policy.get("policy_id") != CAT_POLICY_ID:
        raise CatFuryPairedLaneAdapterV6Error(
            f"Cat v6 cannot execute policy {policy.get('policy_id')!r}"
        )
    request = _mapping(scenario.get("request"), "scenario.request")
    seed = _positive_int(group.get("simulator_seed"), "group.simulator_seed")
    dynamic_load = bind_dynamic_v5_load(
        request,
        seed,
        _mapping(
            scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"
        ),
    )
    if group.get("dynamic_load_contract_sha256") != dynamic_load.contract_sha256:
        raise CatFuryPairedLaneAdapterV6Error(
            "group dynamic-load contract differs from request/config/seed"
        )
    contexts = target_contexts_from_runner_v4(
        _mapping(
            scenario.get("target_context_bundle"),
            "scenario.target_context_bundle",
        )
    )
    artifact = run_cat_fury_full_policy_rollout_v6(
        bridge,
        request,
        CatFuryFullPolicyAdapterV4(),
        seed=seed,
        target_contexts=contexts,
        dynamic_load=dynamic_load,
    )
    return {
        "lane_result": build_cat_runner_v4_lane_result_v6(
            artifact, group=group, scenario=scenario
        )
    }


def cat_runner_v4_artifact_validators_v6() -> dict[str, Any]:
    return {CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6}


__all__ = (
    "CAT_V6_PRODUCER",
    "CatFuryPairedLaneAdapterV6Error",
    "build_cat_runner_v4_lane_result_v6",
    "cat_runner_v4_artifact_validators_v6",
    "cat_runner_v4_lane_contract_v6",
    "execute_cat_runner_v4_lane_v6",
    "validate_cat_runner_v4_artifact_v6",
)
