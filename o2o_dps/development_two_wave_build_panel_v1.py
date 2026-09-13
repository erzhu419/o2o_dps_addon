"""Small build-conditioned, two-wave development experiment.

Both model waves live in one native environment.  The second target is
unattackable until 12 s; no checkpoint/reload is used between pulls.  This is
an identical-creature synthetic pair, not two recovered historical waves.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from typing import Any

from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    EQUIPPED_NAMES_PATH,
    PROJECT_ROOT,
    TARGET_HP_HYPOTHESIS,
    TARGET_GUID,
    WATCHDOG_MS,
    build_development_wave_case_v1,
    build_development_wave_scenario_v1,
)
from .development_wave_panel_v1 import (
    DEFAULT_BINDING,
    DEFAULT_BRIDGE,
    WORKSPACE_ROOT,
    run_development_wave_panel_v1,
)
from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CANDIDATE_PRODUCER,
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
from .fury_contra_adapter_v2 import ContraEvidenceKindV2, ContraFieldEvidenceV2
from .fury_cat_gap_three_baseline_registry_v1 import CatGapThreeBaselineRegistryV1
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v2 import sha256_json
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


SCHEMA = "development_two_wave_build_panel/v1"
BUILD_IDS = ("live_bonereaver", "clean_dual_weapon_probe")
SECOND_WAVE_UNLOCK_MS = 12_000
TEAM_DAMAGE_PER_HIT = 6_500.0
TEAM_HIT_INTERVAL_MS = 500
SECOND_TARGET_ID = f"{TARGET_GUID}:MODEL_REPEAT"


def _two_lane_registry(bridge: Any, candidate: Any, _: Path) -> CatGapThreeBaselineRegistryV1:
    candidate_contract = cat2new_lane_contract_v3()
    candidate_contract["policy_id"] = candidate.policy_id
    return CatGapThreeBaselineRegistryV1(
        executors={
            CAT_POLICY_ID: lambda *, group, scenario, policy: execute_cat_runner_v4_lane_v6(
                bridge, group=group, scenario=scenario, policy=policy
            ),
            candidate.policy_id: Cat2NewFuryPairedLaneAdapterV3(
                bridge=bridge, feedback_policy=candidate,
                optimizer_parameters=asdict(candidate.config),
            ),
        },
        artifact_validators={
            CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
            CANDIDATE_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
        },
        contract={"lane_contracts": [cat_runner_v4_lane_contract_v6(), candidate_contract]},
    )


def _build_player_and_names(build_id: str) -> tuple[dict[str, Any] | None, tuple[str, ...] | None]:
    if build_id == BUILD_IDS[0]:
        return None, None
    if build_id != BUILD_IDS[1]:
        raise ValueError(f"unknown development build: {build_id}")
    clean = json.loads(
        (PROJECT_ROOT / "configs/wowsims/fury_warrior_clean_dual.json").read_text(encoding="utf-8")
    )
    metadata = json.loads(
        (PROJECT_ROOT / "configs/wowsims/fury_warrior_clean_dual.metadata.json").read_text(encoding="utf-8")
    )
    live_names = json.loads(EQUIPPED_NAMES_PATH.read_text(encoding="utf-8"))[
        "equipped_item_names"
    ]
    names = list(live_names[:-1])
    names.extend((metadata["weapon_override"]["main_hand"]["name"], metadata["weapon_override"]["off_hand"]["name"]))
    return clean["raid"]["parties"][0]["players"][0], tuple(names)


def build_two_wave_build_case_v1(seed: int, build_id: str) -> tuple[DevelopmentWaveCaseV1, dict[str, Any]]:
    """Repeat one modeled creature after an enforced idle interval, one load."""

    base = build_development_wave_case_v1(seed)
    request = deepcopy(base.request)
    source_player, equipped_names = _build_player_and_names(build_id)
    player = request["raid"]["parties"][0]["players"][0]
    if source_player is not None:
        player["equipment"] = deepcopy(source_player["equipment"])
    target = request["encounter"]["targets"][0]
    second = deepcopy(target)
    second["name"] = "Upper Kara creature 61944 (model repeat after gap)"
    request["encounter"]["targets"].append(second)
    request["encounter"]["duration"] = WATCHDOG_MS / 1000
    background = tuple(
        BackgroundDamageEventV1(
            schedule_index=wave * 20 + hit,
            time_ms=(0 if wave == 0 else SECOND_WAVE_UNLOCK_MS) + (hit + 1) * TEAM_HIT_INTERVAL_MS,
            target_index=wave,
            event_id=f"model-wave-{wave + 1}-team-{hit:02d}",
            damage=TEAM_DAMAGE_PER_HIT,
        )
        for wave in (0, 1) for hit in range(20)
    )
    config = DynamicTargetSemanticsConfigV3(
        target_health=(
            DynamicTargetHealthV1(0, TARGET_HP_HYPOTHESIS),
            DynamicTargetHealthV1(1, TARGET_HP_HYPOTHESIS),
        ),
        idle_advance_horizon_ms=WATCHDOG_MS,
        background_damage_events=background,
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 1, False),
            DynamicAttackabilityEventV2(1, SECOND_WAVE_UNLOCK_MS, 1, True),
        ),
    )
    load = DynamicRolloutLoadV3.bind(request, seed, config)
    equipment_evidence = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT, source_sha256=sha256_json(request)
    )
    context0 = replace(
        base.target_contexts[0],
        equipped_item_names=equipped_names or base.target_contexts[0].equipped_item_names,
        equipment_evidence=equipment_evidence,
    )
    context1 = replace(
        context0,
        target_index=1,
        context_id="upper-kara-61944-model-repeat-after-gap-v1",
        target_name=second["name"],
    )
    spec = deepcopy(base.case_spec)
    spec.update({
        "schema": "development_two_wave_build_case/v1",
        "source_wave_model_target_count": 2,
        "source_target_ref": [TARGET_GUID, SECOND_TARGET_ID],
        "required_target_ids": [TARGET_GUID, SECOND_TARGET_ID],
        "required_target_indices": [0, 1],
        "build_ref": (
            "configs/wowsims/fury_warrior_live.json" if build_id == BUILD_IDS[0]
            else "configs/wowsims/fury_warrior_clean_dual.json"
        ),
        "build_id": build_id,
        "build_role": "CONTROLLED_BUILD_PROBE_NOT_HISTORICAL_SOURCE_PLAYER",
        "equipment_items": deepcopy(player["equipment"]["items"]),
        "two_wave_model": {
            "kind": "SAME_NATIVE_ENVIRONMENT_SYNTHETIC_REPEAT",
            "source_wave_count": 1,
            "model_wave_count": 2,
            "target_0_attackable_from_ms": 0,
            "target_1_attackable_from_ms": SECOND_WAVE_UNLOCK_MS,
            "first_wave_team_kill_deadline_ms": 10_000,
            "inter_wave_gap_min_ms": 2_000,
            "movement_during_gap": "NOT_MODELED",
            "equipment_change_during_gap": "NONE",
            "resource_cooldown_aura_carry": "NATIVE_SAME_ENVIRONMENT",
        },
        "request_sha256": load.request_sha256,
        "dynamic_load_contract_sha256": load.contract_sha256,
    })
    spec["initial_state"]["target_count"] = 2
    spec["initial_state"]["second_target_attackable"] = False
    spec["team_background"]["two_wave_schedule"] = "6500_DAMAGE_PER_500MS_PER_ACTIVE_WAVE"
    case = DevelopmentWaveCaseV1(spec, request, load, {0: context0, 1: context1})
    scenario = build_development_wave_scenario_v1(seed)
    scenario["scenario_id"] = f"upper-kara-61944-two-wave-{build_id}-controlled-reset"
    scenario["stratum"] = "multi_target"
    scenario["request"] = request
    scenario["dynamic_load_config"] = config.to_wire()
    scenario["scenario_model"]["request_sha256"] = load.request_sha256
    scenario["scenario_model"]["limitation_codes"].extend(
        ["SECOND_WAVE_SYNTHETIC_REPEAT", "MOVEMENT_NOT_MODELED"]
    )
    bundle = scenario["target_context_bundle"]
    bundle["request_sha256"] = load.request_sha256
    bundle["target_count"] = 2
    wire0 = bundle["contexts"][0]
    wire0["equipped_item_names"] = list(context0.equipped_item_names)
    wire0["field_evidence"]["equipped_item_names"] = equipment_evidence.to_dict()
    wire1 = deepcopy(wire0)
    wire1.update({"context_id": context1.context_id, "target_index": 1, "target_name": second["name"]})
    bundle["contexts"].append(wire1)
    scenario["source_scenario_sha256"] = sha256_json({
        "origin": scenario["source_scenario_sha256"],
        "model": spec["two_wave_model"],
        "build": build_id,
    })
    return case, scenario


def run_two_wave_build_panel_v1(
    *, master_seed: int,
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
    candidate_kind: str = "anchor_13d",
) -> dict[str, Any]:
    """Two controlled builds, Cat and candidate on the same 2-wave seed."""

    if candidate_kind != "anchor_13d":
        raise ValueError("the two-wave two-lane probe currently supports anchor_13d only")

    builds = []
    for build_id in BUILD_IDS:
        case, scenario = build_two_wave_build_case_v1(master_seed, build_id)
        panel = run_development_wave_panel_v1(
            master_seed=master_seed, bridge_path=bridge_path,
            bridge_cwd=bridge_cwd, runtime_binding_path=runtime_binding_path,
            candidate_kind=candidate_kind,
            case_override=case, scenario_override=scenario,
            baseline_ids=(CAT_POLICY_ID,), registry_factory=_two_lane_registry,
        )
        rows = []
        for row in panel["rows"]:
            target_rows = row.get("target_outcomes") or []
            deaths = [target.get("death_time_ms") for target in target_rows]
            timeline_valid = (
                row["status"] == "COMPLETED"
                and len(deaths) == 2
                and isinstance(deaths[0], int) and deaths[0] <= 10_000
                and isinstance(deaths[1], int) and deaths[1] >= SECOND_WAVE_UNLOCK_MS
            )
            rows.append({
                **row,
                "two_wave_timeline_valid": timeline_valid,
                "wave_1_ttk_ms": deaths[0] if timeline_valid else None,
                "inter_wave_gap_ms": SECOND_WAVE_UNLOCK_MS - deaths[0] if timeline_valid else None,
                "wave_2_active_ms": deaths[1] - SECOND_WAVE_UNLOCK_MS if timeline_valid else None,
                "whole_two_wave_dps": (
                    row["own_effective_damage"] * 1000.0 / deaths[1]
                    if timeline_valid and deaths[1] else None
                ),
            })
        builds.append({
            "build_id": build_id,
            "status": (
                "TWO_WAVE_TWO_LANE_COMPLETE_DEVELOPMENT_ONLY"
                if all(row["two_wave_timeline_valid"] for row in rows)
                else "EXECUTED_INCOMPLETE_DEVELOPMENT_ONLY"
            ),
            "rows": rows,
            "case": case.case_spec,
        })
    return {
        "schema": SCHEMA,
        "status": (
            "TWO_BUILD_TWO_WAVE_COMPLETE_DEVELOPMENT_ONLY"
            if all(build["status"] == "TWO_WAVE_TWO_LANE_COMPLETE_DEVELOPMENT_ONLY" for build in builds)
            else "EXECUTED_INCOMPLETE_DEVELOPMENT_ONLY"
        ),
        "master_seed": master_seed,
        "builds": builds,
        "historical_exact": False,
        "deployment_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seed", type=int, default=20260913)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_two_wave_build_panel_v1(
        master_seed=args.master_seed,
        bridge_path=args.bridge,
        bridge_cwd=args.bridge_cwd,
        runtime_binding_path=args.runtime_binding,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "master_seed": args.master_seed,
        "builds": [
            {"build_id": build["build_id"], "status": build["status"], "rows": [
                {"policy_id": row["policy_id"], "status": row["status"],
                 "own_effective_damage": row["own_effective_damage"],
                 "whole_two_wave_dps": row["whole_two_wave_dps"]}
                for row in build["rows"]
            ]} for build in result["builds"]
        ],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
