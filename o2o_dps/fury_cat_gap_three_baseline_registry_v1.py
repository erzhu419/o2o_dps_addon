"""Additive single-target Cat-gap registry with three native baseline lanes.

This builds a development-only execution surface.  It does not alter the
frozen Cat-gap plans, dispatch work, or admit a result for selection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cat2new_fury_cat_gap_policy_v1 import Cat2NewFuryCatGapPolicyV1
from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CAT_GAP_PRODUCER,
    Cat2NewFuryPairedLaneAdapterV3,
    cat2new_lane_contract_v3,
    validate_cat2new_fury_paired_artifact_v3,
)
from .cat_fury_paired_lane_adapter_v6 import (
    CAT_V6_PRODUCER,
    cat_runner_v4_lane_contract_v6,
    execute_cat_runner_v4_lane_v6,
    validate_cat_runner_v4_artifact_v6,
)
from .contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    contra260817_runner_v4_lane_contract_v4,
    execute_contra260817_runner_v4_lane_v4,
    validate_contra260817_runner_v4_artifact_v4,
)
from .deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from .fury_dynamic_v5_deployed_contra_adapter_v7 import (
    DEPLOYED_CONTRA_V7_PRODUCER,
    execute_deployed_contra_v7_lane_v7,
    validate_deployed_contra_v7_artifact_v7,
)
from .fury_multiseed_worker_registry_v5 import (
    deployed_contra_runner_v4_lane_contract_v7,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import RAID_A_CONTROLLER


SCHEMA = "fury_cat_gap_three_baseline_registry/v1"
BASELINE_IDS = (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
)


class CatGapThreeBaselineRegistryV1Error(ValueError):
    """A requested registry contains an unsupported lane or scenario."""


@dataclass(frozen=True)
class CatGapThreeBaselineRegistryV1:
    executors: Mapping[str, Callable[..., Mapping[str, Any]]]
    artifact_validators: Mapping[str, Callable[..., Mapping[str, Any]]]
    contract: Mapping[str, Any]


def _single_target_scenarios(scenarios: Sequence[Mapping[str, Any]]) -> None:
    if not scenarios:
        raise CatGapThreeBaselineRegistryV1Error("one single-target scenario is required")
    for index, scenario in enumerate(scenarios):
        request = scenario.get("request")
        encounter = request.get("encounter") if isinstance(request, Mapping) else None
        targets = encounter.get("targets") if isinstance(encounter, Mapping) else None
        config = scenario.get("dynamic_load_config")
        target_health = config.get("target_health") if isinstance(config, Mapping) else None
        if (
            scenario.get("stratum") != "single_target"
            or not isinstance(targets, list)
            or len(targets) != 1
            or not isinstance(target_health, list)
            or len(target_health) != 1
        ):
            raise CatGapThreeBaselineRegistryV1Error(
                f"scenario {index} requires Raid-B or has no single-target load"
            )


def build_cat_gap_three_baseline_registry_v1(
    bridge: Any,
    *,
    scenarios: Sequence[Mapping[str, Any]],
    candidate_policies: Mapping[str, Cat2NewFuryCatGapPolicyV1],
    runtime_binding_path: str | Path,
    controller: str = RAID_A_CONTROLLER,
) -> CatGapThreeBaselineRegistryV1:
    """Bind native Cat, Contra_new, deployed Contra, and Cat-gap candidates.

    The deployed runtime binding is an explicit caller-supplied file; no
    installed-addon or SavedVariables state is silently selected here.
    """

    if controller != RAID_A_CONTROLLER:
        raise CatGapThreeBaselineRegistryV1Error(
            "Raid-B multi-target deployed Contra is not implemented by v7"
        )
    _single_target_scenarios(scenarios)
    if not candidate_policies:
        raise CatGapThreeBaselineRegistryV1Error("at least one Cat-gap candidate is required")
    for candidate_id, feedback in candidate_policies.items():
        if (
            candidate_id in BASELINE_IDS
            or type(feedback) is not Cat2NewFuryCatGapPolicyV1
            or feedback.policy_id != candidate_id
        ):
            raise CatGapThreeBaselineRegistryV1Error(
                f"candidate {candidate_id!r} is not a bound Cat-gap v3 policy"
            )

    path = Path(runtime_binding_path).expanduser().resolve()
    binding = load_deployed_contra_runtime_binding_v1(path)

    def deployed_contra_executor(
        *, group: Mapping[str, Any], scenario: Mapping[str, Any], policy: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        envelope = execute_deployed_contra_v7_lane_v7(
            bridge,
            group=group,
            scenario=scenario,
            policy=policy,
            runtime_binding=binding,
            controller=RAID_A_CONTROLLER,
        )
        if set(envelope) != {"lane_result", "lane_cache_identity"}:
            raise CatGapThreeBaselineRegistryV1Error(
                "deployed-Contra v7 executor envelope is malformed"
            )
        lane = envelope["lane_result"]
        artifact = lane.get("artifact") if isinstance(lane, Mapping) else None
        if not isinstance(artifact, Mapping) or envelope["lane_cache_identity"] != artifact.get(
            "lane_cache_identity"
        ):
            raise CatGapThreeBaselineRegistryV1Error(
                "deployed-Contra v7 cache identity was lost at the registry boundary"
            )
        return {"lane_result": lane}

    executors: dict[str, Callable[..., Mapping[str, Any]]] = {
        CAT_POLICY_ID: lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
            bridge, group=group, scenario=scenario, policy=policy
        ),
        CONTRA260817_POLICY_ID: lambda *, group, scenario, policy: execute_contra260817_runner_v4_lane_v4(
            bridge, group=group, scenario=scenario, policy=policy
        ),
        CONTRA_DEPLOYED_POLICY_ID: deployed_contra_executor,
    }
    contracts = [
        cat_runner_v4_lane_contract_v6(),
        contra260817_runner_v4_lane_contract_v4(),
        deployed_contra_runner_v4_lane_contract_v7(),
    ]
    for candidate_id, feedback in candidate_policies.items():
        executors[candidate_id] = Cat2NewFuryPairedLaneAdapterV3(
            bridge=bridge,
            feedback_policy=feedback,
            optimizer_parameters=asdict(feedback.config),
        )
        lane_contract = cat2new_lane_contract_v3()
        lane_contract["policy_id"] = candidate_id
        contracts.append(lane_contract)

    validators = {
        CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
        CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
        DEPLOYED_CONTRA_V7_PRODUCER: validate_deployed_contra_v7_artifact_v7,
        CAT_GAP_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
    }
    return CatGapThreeBaselineRegistryV1(
        executors=executors,
        artifact_validators=validators,
        contract={
            "schema": SCHEMA,
            "status": "DEVELOPMENT_ONLY_NO_RUN",
            "controller": RAID_A_CONTROLLER,
            "scenario_count": len(scenarios),
            "baseline_ids": list(BASELINE_IDS),
            "candidate_ids": list(candidate_policies),
            "runtime_binding_path": str(path),
            "runtime_binding_sha256": binding["binding_sha256"],
            "lane_contracts": contracts,
            "comparison_ready": False,
            "live_fidelity": False,
        },
    )


__all__ = (
    "BASELINE_IDS",
    "CatGapThreeBaselineRegistryV1",
    "CatGapThreeBaselineRegistryV1Error",
    "SCHEMA",
    "build_cat_gap_three_baseline_registry_v1",
)
