"""Controlled dual-wield build on a source-selected multi-target wave."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from .development_two_wave_build_panel_v1 import _build_player_and_names
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_wave_twelve_v1 import build_twelve_wave_case_v1
from .fury_contra_adapter_v2 import ContraEvidenceKindV2, ContraFieldEvidenceV2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import sha256_json
from .expert_proposals import QUEUE_REFS
from .sim_bridge import ActionRef, ActResult


V15_BRIDGE = Path(__file__).resolve().parents[1] / (
    "bin/o2obridge.seedfix-v15.postgcdq.withdb.goamd64v1.windows-amd64.exe"
)
POST_GCD_QUEUE_ACCEPTANCE = "SIMULATOR_ASSUMPTION_POST_GCD_QUEUE"


class _ContraNewPostGcdQueueBridge:
    """Isolate v15's same-invocation swing-queue hypothesis to Contra_new."""

    def __init__(self, bridge: Any, receipts: list[dict[str, Any]]) -> None:
        self._bridge = bridge
        self._receipts = receipts
        self._last_consuming_act: ActResult | None = None

    def load_dynamic_v3(self, request: Any, seed: int, config: Any) -> Any:
        self._last_consuming_act = None
        self._receipts.clear()
        return self._bridge.load_dynamic_v3(request, seed, config)

    def act(self, action: ActionRef, *, attempt_id: str | None = None) -> ActResult:
        previous = self._last_consuming_act
        if (
            action in QUEUE_REFS.values()
            and previous is not None
            and previous.casted and previous.consumes_decision
            and not previous.needs_input
            and self._bridge.state()["time_ms"] == previous.state["time_ms"]
        ):
            result = self._bridge.act_post_gcd_queue(action)
            self._receipts.append({
                "time_ms": previous.state["time_ms"],
                "action": action.to_wire(),
                "casted": result.casted,
                "consumes_decision": result.consumes_decision,
            })
            self._last_consuming_act = None
            return result
        result = self._bridge.act(action, attempt_id=attempt_id)
        self._last_consuming_act = result if result.casted and result.consumes_decision else None
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def build_dual_wield_raid_b_wave_v1(
    seed: int, stratum: str = "multi_2_targets",
) -> tuple[DevelopmentWaveCaseV1, dict]:
    case, scenario = build_twelve_wave_case_v1(seed, stratum)
    if len(case.request["encounter"]["targets"]) < 2:
        raise ValueError("Raid-B development case requires a multi-target wave")
    source_player, equipped_names = _build_player_and_names("clean_dual_weapon_probe")
    assert source_player is not None and equipped_names is not None
    request = deepcopy(case.request)
    player = request["raid"]["parties"][0]["players"][0]
    player["equipment"] = deepcopy(source_player["equipment"])
    load = DynamicRolloutLoadV3.bind(request, seed, case.dynamic_load.config)
    equipment_evidence = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT,
        source_sha256=sha256_json(request),
    )
    contexts = {
        index: replace(
            context,
            equipped_item_names=equipped_names,
            equipment_evidence=equipment_evidence,
        ) for index, context in case.target_contexts.items()
    }
    spec = deepcopy(case.case_spec)
    spec.update({
        "schema": "development_raid_b_dual_wave_case/v1",
        "build_id": "clean_dual_weapon_probe",
        "build_ref": "configs/wowsims/fury_warrior_clean_dual.json",
        "build_role": "CONTROLLED_DUAL_WIELD_PROBE_NOT_HISTORICAL_SOURCE_PLAYER",
        "manual_target_switch_not_modeled": True,
        "equipment_items": deepcopy(player["equipment"]["items"]),
        "request_sha256": load.request_sha256,
        "dynamic_load_contract_sha256": load.contract_sha256,
    })
    scenario = deepcopy(scenario)
    scenario["scenario_id"] += "__controlled_dual_wield"
    scenario["request"] = request
    scenario["source_scenario_sha256"] = sha256_json({
        "origin": scenario["source_scenario_sha256"],
        "build": "clean_dual_weapon_probe",
    })
    scenario["scenario_model"]["request_sha256"] = load.request_sha256
    scenario["scenario_model"]["limitation_codes"].append(
        "CONTROLLED_DUAL_WIELD_BUILD_NOT_SOURCE_FOCAL_BUILD"
    )
    bundle = scenario["target_context_bundle"]
    bundle["request_sha256"] = load.request_sha256
    for row in bundle["contexts"]:
        row["equipped_item_names"] = list(equipped_names)
        row["field_evidence"]["equipped_item_names"] = equipment_evidence.to_dict()
    return DevelopmentWaveCaseV1(spec, request, load, contexts), scenario


def _raid_b_registry(
    bridge, candidate, runtime_binding_path, *,
    post_gcd_queue_hypothesis: bool = False,
    queue_receipts: list[dict[str, Any]] | None = None,
):
    from .development_two_wave_build_panel_v1 import _two_lane_registry
    from .fury_cat_gap_three_baseline_registry_v1 import CatGapThreeBaselineRegistryV1
    from .contra260817_fury_paired_lane_adapter_v4 import (
        CONTRA260817_V4_PRODUCER as CONTRA_NEW_PRODUCER,
        build_contra260817_runner_v4_lane_result_v4,
        contra260817_runner_v4_lane_contract_v4,
        validate_contra260817_runner_v4_artifact_v4,
    )
    from .contra260817_fury_full_policy_rollout_v4 import run_contra260817_fury_full_policy_rollout_v4
    from .contra260817_fury_full_policy_v3 import Contra260817FuryFullPolicyAdapterV3
    from .fury_dynamic_v5_baseline_adapter_v4 import target_contexts_from_runner_v4
    from .fury_paired_multiseed_runner_v4 import (
        CONTRA260817_POLICY_ID, bind_dynamic_v5_load,
    )
    from .development_wave_panel_v1 import DevelopmentLaneUnsupported

    def contra_new_multi_executor(*, group, scenario, policy):
        request = scenario["request"]
        seed = group["simulator_seed"]
        load = bind_dynamic_v5_load(request, seed, scenario["dynamic_load_config"])
        contexts = target_contexts_from_runner_v4(scenario["target_context_bundle"])
        contra_bridge = (
            _ContraNewPostGcdQueueBridge(bridge, queue_receipts)
            if post_gcd_queue_hypothesis and queue_receipts is not None else bridge
        )
        artifact = run_contra260817_fury_full_policy_rollout_v4(
            contra_bridge, request, Contra260817FuryFullPolicyAdapterV3(),
            seed=seed, target_contexts=contexts, dynamic_load=load,
        )
        first = next((row for row in artifact["blockers"]
                      if row.get("execution_fatal") is True), None)
        if (
            post_gcd_queue_hypothesis
            and first is not None
            and first["code"] == "DECISION_NOT_CONSUMED_NO_FALLBACK"
            and artifact["steps"]
            and any(
                event.get("source_sink", {}).get("channel") == "swing_queue"
                and event.get("simulator_acceptance", {}).get("status") == "ACCEPTED"
                for event in artifact["steps"][-1]["ordered_execution"]["sink_events"]
            )
        ):
            raise DevelopmentLaneUnsupported(
                "Contra_new native multi-target macro reentry is unmodeled after "
                "a standalone accepted no-GCD Cleave queue at "
                f"decision {first['decision_index']}; "
                "client reentry cadence is unobserved; score remains null",
                blocker=first, artifact_status=artifact["status"],
            )
        if artifact["elapsed_ms"] <= 0:
            first_code = first["code"] if first else "UNRESOLVED_ZERO_ELAPSED"
            event = next((event for step in artifact.get("steps", [])
                          for event in step.get("ordered_execution", {}).get("sink_events", [])
                          if event.get("simulator_submission", {}).get("status") == "BRIDGE_ERROR_FAIL_CLOSED"), None)
            source = event.get("source_sink", {}) if event else {}
            raise DevelopmentLaneUnsupported(
                "Contra_new native multi-target source order unsupported by current bridge: "
                f"{first_code}; sink={source.get('channel')}@{source.get('source_ref')}; "
                "score remains null",
                blocker=first, artifact_status=artifact["status"],
            )
        return {"lane_result": build_contra260817_runner_v4_lane_result_v4(
            artifact, group=group, scenario=scenario,
        )}

    registry = _two_lane_registry(bridge, candidate, runtime_binding_path)
    executors = dict(registry.executors)
    executors[CONTRA260817_POLICY_ID] = contra_new_multi_executor
    validators = dict(registry.artifact_validators)
    validators[CONTRA_NEW_PRODUCER] = validate_contra260817_runner_v4_artifact_v4
    contracts = list(registry.contract["lane_contracts"])
    contracts.append(contra260817_runner_v4_lane_contract_v4())
    return CatGapThreeBaselineRegistryV1(
        executors=executors, artifact_validators=validators,
        contract={"lane_contracts": contracts},
    )


def run_raid_b_four_policy_wave_v1(
    seed: int, stratum: str = "multi_2_targets", *,
    bridge_path: Path | None = None, bridge_cwd: Path | None = None,
    runtime_binding_path: Path | None = None,
    post_gcd_queue_hypothesis: bool = False,
) -> dict:
    from .development_wave_panel_v1 import (
        DEFAULT_BINDING, DEFAULT_BRIDGE, WORKSPACE_ROOT,
        run_development_wave_panel_v1,
    )
    from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID, CONTRA260817_POLICY_ID
    from .fury_runtime_bound_deployed_contra_raid_b_v1 import POLICY_ID as RAID_B_POLICY_ID

    case, scenario = build_dual_wield_raid_b_wave_v1(seed, stratum)
    queue_receipts: list[dict[str, Any]] = []
    if post_gcd_queue_hypothesis:
        from .development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
        native_bridge_type = V14ProjectedDynamicV3Bridge
        case.case_spec["post_gcd_queue_acceptance"] = POST_GCD_QUEUE_ACCEPTANCE
    else:
        native_bridge_type = None
    registry_factory = lambda bridge, candidate, binding: _raid_b_registry(
        bridge, candidate, binding,
        post_gcd_queue_hypothesis=post_gcd_queue_hypothesis,
        queue_receipts=queue_receipts,
    )
    panel = run_development_wave_panel_v1(
        master_seed=seed, case_override=case, scenario_override=scenario,
        candidate_kind="anchor_13d", registry_factory=registry_factory,
        deployed_contra_controller="raid_b",
        baseline_ids=(CAT_POLICY_ID, CONTRA260817_POLICY_ID, RAID_B_POLICY_ID),
        bridge_path=bridge_path or DEFAULT_BRIDGE,
        bridge_cwd=bridge_cwd or WORKSPACE_ROOT / "wowsims-turtle",
        runtime_binding_path=runtime_binding_path or DEFAULT_BINDING,
        **({"native_bridge_type": native_bridge_type} if native_bridge_type else {}),
    )
    panel["comparison_scope"] = "FOUR_NATIVE_MODEL_POLICIES_DUAL_WIELD_RAID_B"
    panel["build_assumption"] = "CONTROLLED_CLEAN_DUAL_NOT_SOURCE_PLAYER_BUILD"
    panel["manual_target_switch_not_modeled"] = True
    panel["post_gcd_queue_acceptance"] = (
        POST_GCD_QUEUE_ACCEPTANCE if post_gcd_queue_hypothesis else "NOT_USED"
    )
    for row in panel["rows"]:
        if row["policy_id"] == CONTRA260817_POLICY_ID:
            row["post_gcd_queue_assumption_receipts"] = list(queue_receipts)
    deployed = next(row for row in panel["rows"] if row["policy_id"] == RAID_B_POLICY_ID)
    panel["all_deployed_target_index"] = (
        0 if deployed.get("target_indices_seen") == [0] else None
    )
    return panel


def main() -> None:
    from .development_wave_panel_v1 import DEFAULT_BINDING, DEFAULT_BRIDGE, WORKSPACE_ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--stratum", default="multi_2_targets")
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--post-gcd-queue-hypothesis", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    panel = run_raid_b_four_policy_wave_v1(
        args.seed, args.stratum, bridge_path=args.bridge,
        bridge_cwd=args.bridge_cwd, runtime_binding_path=args.runtime_binding,
        post_gcd_queue_hypothesis=args.post_gcd_queue_hypothesis,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(panel, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": panel["status"], "four_way_complete": panel["four_way_complete"],
        "lanes": [{key: row.get(key) for key in (
            "policy_id", "status", "own_effective_damage", "artifact_status", "error",
        )} for row in panel["rows"]],
    }, ensure_ascii=False))


__all__ = (
    "build_dual_wield_raid_b_wave_v1", "run_raid_b_four_policy_wave_v1",
)


if __name__ == "__main__":
    main()
