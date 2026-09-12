"""Runner-v4 lane adapter for the version-isolated deployed-Contra v6 path.

This route is source-derived from the installed ``Contra_ALL.lua`` Warrior
Raid-A policy.  It fixes simulator command semantics without claiming that an
older SavedVariables snapshot belongs to the currently tested character.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .fury_contra_adapter_v2 import ContraDeployedFuryAdapterV2
from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
from .fury_full_policy_rollout_v6 import (
    ROLLOUT_SCHEMA_V6,
    run_fury_full_policy_rollout_v6,
    validate_fury_full_policy_rollout_v6,
)
from .fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    FuryPairedRunnerV4Error,
    bind_dynamic_v5_load,
    build_lane_result_v4,
    sha256_json,
)


JSONMap = dict[str, Any]
DEPLOYED_CONTRA_V6_PRODUCER = "fury_full_policy_rollout_v6_deployed_contra"


class FuryDynamicV5DeployedContraAdapterV5Error(FuryPairedRunnerV4Error):
    """The deployed-Contra v6 lane or its runner binding is malformed."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            f"{label} must be an object"
        )
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            f"{label} must be a positive integer"
        )
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            f"{label} must be numeric"
        )
    observed = float(value)
    if not math.isfinite(observed):
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            f"{label} must be finite"
        )
    return observed


def _artifact_summary_v6(artifact: Mapping[str, Any]) -> JSONMap:
    if artifact.get("expert_id") != ContraDeployedFuryAdapterV2.expert_id:
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            "deployed-Contra v6 expert identity mismatch"
        )
    blockers = artifact.get("blockers")
    if not isinstance(blockers, list):
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            "deployed-Contra v6 blockers are malformed"
        )
    reasons: dict[str, int] = {}
    fatal_count = 0
    for index, raw in enumerate(blockers):
        blocker = _mapping(raw, f"deployed-Contra v6 blocker {index}")
        code = blocker.get("code")
        if not isinstance(code, str) or not code:
            raise FuryDynamicV5DeployedContraAdapterV5Error(
                f"deployed-Contra v6 blocker {index}.code is invalid"
            )
        reasons[code] = reasons.get(code, 0) + 1
        fatal_count += int(blocker.get("execution_fatal") is True)

    final_state = _mapping(
        artifact.get("final_state"), "deployed-Contra v6 final_state"
    )
    lifecycle = final_state.get("dynamic_team_background")
    targets = lifecycle.get("targets") if isinstance(lifecycle, Mapping) else None
    all_dead = (
        isinstance(targets, list)
        and bool(targets)
        and all(
            isinstance(target, Mapping) and target.get("dead") is True
            for target in targets
        )
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
        "deployed-Contra v6 runtime closure",
    )
    runtime_complete = closure.get("status") == "COMPLETE_BOUND"
    damage = _finite(artifact.get("damage_delta"), "deployed-Contra v6 damage_delta")
    elapsed_ms = _positive_int(
        artifact.get("elapsed_ms"), "deployed-Contra v6 elapsed_ms"
    )
    return {
        "policy_id": CONTRA_DEPLOYED_POLICY_ID,
        "artifact_schema": ROLLOUT_SCHEMA_V6,
        "completion_mode": completion_mode,
        "damage": damage,
        "elapsed_ms": elapsed_ms,
        "dps": damage * 1000.0 / elapsed_ms,
        "completion_criterion_met": complete,
        "offline_score_eligible": complete and runtime_complete and fatal_count == 0,
        "omitted_lane_count": fatal_count,
        "fatal_error_count": fatal_count,
        "nonfaithful_reason_counts": dict(sorted(reasons.items())),
        "end_state_sha256": sha256_json(final_state),
        "dynamic_runtime_receipts_complete": runtime_complete,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def validate_deployed_contra_v6_artifact_v5(
    artifact: Mapping[str, Any],
    producer_runtime_receipt: Mapping[str, Any],
    dynamic_load: Any,
) -> JSONMap:
    """Validate v6 and return runner-v4's exact producer summary."""

    validated = validate_fury_full_policy_rollout_v6(
        artifact, dynamic_load=dynamic_load
    )
    if producer_runtime_receipt != validated.get(
        "dynamic_v3_runtime_receipt_closure"
    ):
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            "deployed-Contra runtime receipt differs from its v6 artifact"
        )
    return _artifact_summary_v6(validated)


def build_deployed_contra_v6_lane_result_v5(
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
    validated = validate_fury_full_policy_rollout_v6(
        artifact, dynamic_load=dynamic_load
    )
    summary = _artifact_summary_v6(validated)
    return build_lane_result_v4(
        policy_id=CONTRA_DEPLOYED_POLICY_ID,
        producer=DEPLOYED_CONTRA_V6_PRODUCER,
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


def execute_deployed_contra_v6_lane_v5(
    bridge: Any,
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> JSONMap:
    """Execute one explicitly selected deployed-Contra v6 lane."""

    if policy.get("policy_id") != CONTRA_DEPLOYED_POLICY_ID:
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            f"deployed-Contra v6 cannot execute policy {policy.get('policy_id')!r}"
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
        raise FuryDynamicV5DeployedContraAdapterV5Error(
            "group dynamic-load contract differs from request/config/seed"
        )
    contexts = target_contexts_from_runner_v4(
        _mapping(
            scenario.get("target_context_bundle"),
            "scenario.target_context_bundle",
        )
    )
    artifact = run_fury_full_policy_rollout_v6(
        bridge,
        request,
        ContraDeployedFuryAdapterV2(),
        seed=seed,
        target_contexts=contexts,
        dynamic_load=dynamic_load,
    )
    return {
        "lane_result": build_deployed_contra_v6_lane_result_v5(
            artifact, group=group, scenario=scenario
        )
    }


__all__ = (
    "DEPLOYED_CONTRA_V6_PRODUCER",
    "FuryDynamicV5DeployedContraAdapterV5Error",
    "build_deployed_contra_v6_lane_result_v5",
    "execute_deployed_contra_v6_lane_v5",
    "validate_deployed_contra_v6_artifact_v5",
)
