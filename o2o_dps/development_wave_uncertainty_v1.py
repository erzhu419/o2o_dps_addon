"""Paired, model-defined uncertainty branches for one Upper Kara wave.

The source death budget is not exact max HP, and the source total damage rate
contains an unidentified focal contribution.  Team rates here are explicit
focal-excluded sensitivity inputs, never estimates from that total rate.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    SOURCE_CAPSULE_SHA256,
    TARGET_ARMOR_HYPOTHESIS,
    TARGET_HP_HYPOTHESIS,
    TEAM_HIT_INTERVAL_MS,
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
from .fury_contra_adapter_v2 import ContraEvidenceKindV2, ContraFieldEvidenceV2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


SCHEMA = "development_wave_uncertainty/v1"


@dataclass(frozen=True)
class UncertaintyBranchV1:
    name: str
    hp: int = TARGET_HP_HYPOTHESIS
    armor: int = TARGET_ARMOR_HYPOTHESIS
    team_dps: int = 13_000
    attackable_after_ms: int = 0


# Deliberate sensitivity brackets, not inferred confidence intervals.
BRANCHES = (
    UncertaintyBranchV1("reference"),
    UncertaintyBranchV1("team_9k", team_dps=9_000),
    UncertaintyBranchV1("team_17k", team_dps=17_000),
    UncertaintyBranchV1("hp_80pct", hp=round(TARGET_HP_HYPOTHESIS * .8)),
    UncertaintyBranchV1("hp_120pct", hp=round(TARGET_HP_HYPOTHESIS * 1.2)),
    UncertaintyBranchV1("armor_1000", armor=1_000),
    UncertaintyBranchV1("armor_2600", armor=2_600),
    UncertaintyBranchV1("attackable_after_3s", attackable_after_ms=3_000),
)


def build_uncertainty_branch_v1(
    seed: int, branch: UncertaintyBranchV1,
) -> tuple[DevelopmentWaveCaseV1, dict[str, Any]]:
    """Apply one branch to both the native request and causal dynamic load."""

    base = build_development_wave_case_v1(seed)
    request = deepcopy(base.request)
    target = request["encounter"]["targets"][0]
    target["stats"][26] = branch.armor
    target["stats"][34] = branch.hp
    delay = branch.attackable_after_ms
    if not 0 <= delay < WATCHDOG_MS:
        raise ValueError("attackability delay must precede watchdog")
    team_events = tuple(
        BackgroundDamageEventV1(
            schedule_index=index,
            time_ms=delay + TEAM_HIT_INTERVAL_MS * (index + 1),
            target_index=0,
            event_id=f"model-team-{index:03d}",
            damage=branch.team_dps * TEAM_HIT_INTERVAL_MS / 1000,
        )
        for index in range((WATCHDOG_MS - delay) // TEAM_HIT_INTERVAL_MS)
    )
    attackability = (
        (
            DynamicAttackabilityEventV2(0, 0, 0, False),
            DynamicAttackabilityEventV2(1, delay, 0, True),
        ) if delay else ()
    )
    config = DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, branch.hp),),
        idle_advance_horizon_ms=WATCHDOG_MS,
        background_damage_events=team_events,
        attackability_events=attackability,
    )
    load = DynamicRolloutLoadV3.bind(request, seed, config)
    hypothesis = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
        corpus_sha256=SOURCE_CAPSULE_SHA256,
        hypothesis_id=f"{branch.name}-hp-{branch.hp}",
    )
    # Equipment is unchanged by this branch; preserve its original evidence.
    equipment = base.target_contexts[0].equipment_evidence
    context = replace(
        base.target_contexts[0],
        context_id=f"upper-kara-61944-{branch.name}-v1",
        target_max_health=branch.hp,
        target_health_pct_evidence=hypothesis,
        target_max_health_evidence=hypothesis,
        equipment_evidence=equipment,
    )
    spec = deepcopy(base.case_spec)
    spec["uncertainty_branch"] = {
        "name": branch.name,
        "hp": branch.hp,
        "armor": branch.armor,
        "team_dps": branch.team_dps,
        "attackable_after_ms": delay,
        "interpretation": "EXPLICIT_SENSITIVITY_NOT_HISTORICAL_ESTIMATE",
        "team_focal_contribution": "EXCLUDED_BY_CONSTRUCTION_NOT_ESTIMATED_FROM_SOURCE_TOTAL",
    }
    spec["initial_state"].update({
        "target_max_hp": branch.hp,
        "target_current_hp": branch.hp,
        "target_base_armor": branch.armor,
        "target_attackable_at_ms": delay,
    })
    spec["team_background"].update({
        "branch": branch.name,
        "damage_per_500ms": branch.team_dps * TEAM_HIT_INTERVAL_MS / 1000,
        "team_dps_assumed": branch.team_dps,
    })
    spec["request_sha256"] = load.request_sha256
    spec["dynamic_load_contract_sha256"] = load.contract_sha256
    case = DevelopmentWaveCaseV1(spec, request, load, {0: context})

    scenario = build_development_wave_scenario_v1(seed)
    scenario["scenario_id"] = f"upper-kara-61944-{branch.name}"
    scenario["request"] = request
    scenario["dynamic_load_config"] = config.to_wire()
    scenario["scenario_model"]["request_sha256"] = load.request_sha256
    scenario["target_context_bundle"]["request_sha256"] = load.request_sha256
    wire = scenario["target_context_bundle"]["contexts"][0]
    wire["context_id"] = context.context_id
    wire["target_max_health"] = branch.hp
    wire["field_evidence"]["target_health_pct"] = hypothesis.to_dict()
    wire["field_evidence"]["target_max_health"] = hypothesis.to_dict()
    wire["field_evidence"]["equipped_item_names"] = equipment.to_dict()
    scenario["scenario_model"]["limitation_codes"] = [
        "KILL_BUDGET_HP_MODEL_NOT_EXACT",
        "FOCAL_EXCLUDED_TEAM_RATE_SENSITIVITY_NOT_SOURCE_ESTIMATE",
        "ATTACKABILITY_DELAY_ASSUMED" if delay else "ATTACKABLE_FROM_START_ASSUMED",
        "WATCHDOG_IS_CENSOR_NOT_WAVE_SUCCESS",
    ]
    return case, scenario


def _one_job(args: tuple[int, UncertaintyBranchV1, str, str, str, float]) -> dict[str, Any]:
    seed, branch, bridge, bridge_cwd, binding, discount = args
    case, scenario = build_uncertainty_branch_v1(seed, branch)
    return run_development_wave_panel_v1(
        master_seed=seed,
        bridge_path=Path(bridge),
        bridge_cwd=Path(bridge_cwd),
        runtime_binding_path=Path(binding),
        candidate_kind="cat_residual",
        residual_discount_rage=discount,
        case_override=case,
        scenario_override=scenario,
    )


def summarize_uncertainty_panels_v1(
    panels: list[dict[str, Any]], branches: tuple[UncertaintyBranchV1, ...],
) -> dict[str, Any]:
    branch_rows = []
    for branch in branches:
        subset = [p for p in panels if p["case"]["uncertainty_branch"]["name"] == branch.name]
        complete = [p for p in subset if p["four_way_complete"] is True]
        policies = [r["policy_id"] for r in complete[0]["rows"]] if complete else []
        means = {
            policy: mean(next(r["own_effective_damage"] for r in panel["rows"]
                              if r["policy_id"] == policy) for panel in complete)
            for policy in policies
        }
        candidate = next((r["policy_id"] for r in complete[0]["rows"]
                          if r["role"] == "CANDIDATE"), None) if complete else None
        deltas = {}
        for policy in policies:
            if policy == candidate:
                continue
            values = [
                next(r["own_effective_damage"] for r in panel["rows"] if r["policy_id"] == candidate)
                - next(r["own_effective_damage"] for r in panel["rows"] if r["policy_id"] == policy)
                for panel in complete
            ]
            deltas[policy] = {
                "mean": mean(values),
                "se": stdev(values) / len(values) ** .5 if len(values) > 1 else None,
            }
        branch_rows.append({
            "branch": branch.name,
            "requested_seeds": len(subset),
            "four_way_complete_seeds": len(complete),
            "mean_own_effective_damage": means,
            "candidate_minus_baseline": deltas,
            "ranking_by_mean_damage": sorted(means, key=means.get, reverse=True),
            "incomplete_seeds": [p["master_seed"] for p in subset if not p["four_way_complete"]],
        })
    return {
        "schema": SCHEMA,
        "status": "MODEL_DEFINED_SENSITIVITY_ONLY",
        "source_wave": "86b561f2-1428-4fa4-b9c8-287f351c5fd0:wave:1",
        "historical_exact": False,
        "real_superiority_authorized": False,
        "team_focal_subtraction": "NOT_NEEDED_FOR_EXOGENOUS_HYPOTHESIS; SOURCE_TOTAL_RATE_UNUSED",
        "branch_count": len(branches),
        "panel_count": len(panels),
        "branches": branch_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-start", type=int, default=20260913)
    parser.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--branches", nargs="*", default=[b.name for b in BRANCHES])
    parser.add_argument("--residual-discount-rage", type=float, default=10)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.seed_count < 1 or args.workers < 1:
        parser.error("seed-count and workers must be positive")
    selected = tuple(b for b in BRANCHES if b.name in args.branches)
    if not selected or len(selected) != len(set(args.branches)):
        parser.error("branches must name one or more unique registered branches")
    jobs = [
        (seed, branch, str(args.bridge), str(args.bridge_cwd),
         str(args.runtime_binding), args.residual_discount_rage)
        for branch in selected
        for seed in range(args.seed_start, args.seed_start + args.seed_count)
    ]
    if args.output.exists():
        raise FileExistsError(args.output)
    panels_dir = args.output.parent / f"{args.output.stem}-panels"
    if panels_dir.exists():
        raise FileExistsError(panels_dir)
    panels_dir.mkdir(parents=True)
    panels = []

    def retain(panel: dict[str, Any]) -> None:
        branch_name = panel["case"]["uncertainty_branch"]["name"]
        destination = panels_dir / branch_name / f"seed-{panel['master_seed']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(panel, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        panels.append(panel)

    if args.workers == 1:
        for job in jobs:
            retain(_one_job(job))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            for future in as_completed([executor.submit(_one_job, job) for job in jobs]):
                retain(future.result())
    result = summarize_uncertainty_panels_v1(panels, selected)
    result["seed_start"] = args.seed_start
    result["seed_count"] = args.seed_count
    result["residual_discount_rage"] = args.residual_discount_rage
    result["panel_artifacts_dir"] = str(panels_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "panel_count": result["panel_count"],
        "branches": [{
            "name": row["branch"],
            "complete": row["four_way_complete_seeds"],
            "top": row["ranking_by_mean_damage"][0] if row["ranking_by_mean_damage"] else None,
            "candidate_minus_cat": row["candidate_minus_baseline"].get("cat.fury.profile1", {}).get("mean"),
        } for row in result["branches"]],
    }))


if __name__ == "__main__":
    main()
