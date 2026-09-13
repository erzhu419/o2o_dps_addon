"""A small source-stratified, model-defined Upper Kara whole-wave panel.

Chronicle death/damage anchors choose hypotheses, not historical NPC HP or a
player-conditioned team model. Every policy in a paired seed gets the same
controlled reset and background schedule; target deaths remain endogenous.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import lru_cache, partial
import argparse
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
from math import ceil
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1, SOURCE_CAPSULE_BUNDLE_SHA256,
    build_development_wave_case_v1, build_development_wave_scenario_v1,
)
from .development_wave_coverage_v1 import DEFAULT_MANIFEST, _complete_proxy
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2, ContraFieldEvidenceV2,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


SCHEMA = "development_wave_stratified_case/v3"
TEAM_INTERVAL_MS = 500
WATCHDOG_MARGIN_MS = 5_000
# Fixed before evaluating policies: short, medium, long, and a two-target wave.
WAVE_STRATA = {
    "single_short": "7ebfce3d-2817-40c9-b216-19699af9a48b:wave:1",
    "single_medium": "b10d350d-1972-4b8d-ae91-905a9efa8a3d:wave:1",
    "single_long": "f989e71f-42a6-420f-8c68-eb65e9be731f:wave:1",
    "multi_two": "5586159a-be6e-41a0-a6bb-7ca288e69137:wave:1",
}
# Source CSV, through each target's first DEAD. Select the lexicographically
# first direct player GUID observed using Bloodthirst, Mortal Strike,
# Whirlwind, Slam, or Execute in the wave, with positive damage to its targets.
# Selection does not inspect policy results or rank actors by DPS. These small
# direct-GUID aggregates let remote runs avoid transferring the raw CSVs.
SOURCE_FOCAL_ACTORS = {
    "single_short": {
        "guid": "0x0000000000259B57", "name": "\u59ec\u5854\u6b66\u5668\u6218",
        "direct_damage_by_target": [2569], "direct_hit_count_by_target": [6],
    },
    "single_medium": {
        "guid": "0x0000000000259B57", "name": "\u59ec\u5854\u6b66\u5668\u6218",
        "direct_damage_by_target": [1768], "direct_hit_count_by_target": [12],
    },
    "single_long": {
        "guid": "0x00000000003DBD6C", "name": "\u963f\u4fea\u5854",
        "direct_damage_by_target": [12000], "direct_hit_count_by_target": [20],
    },
    "multi_two": {
        "guid": "0x00000000004D4CBE", "name": "\u55b3\u55b3\u6656",
        "direct_damage_by_target": [3881, 2762],
        "direct_hit_count_by_target": [10, 10],
    },
}


@lru_cache(maxsize=1)
def _source_rows() -> dict[str, dict[str, Any]]:
    with gzip.open(DEFAULT_MANIFEST, "rt", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest["content_address"]["sha256"] != SOURCE_CAPSULE_BUNDLE_SHA256:
        raise ValueError("source capsule bundle identity changed")
    rows = {
        row["source_identity"]["wave_id"]: row
        for row in manifest["scenarios"]
        if row["source_identity"]["wave_id"] in WAVE_STRATA.values()
    }
    if set(rows) != set(WAVE_STRATA.values()):
        raise ValueError("fixed stratified source waves are absent")
    return rows


def build_stratified_wave_case_v1(
    seed: int, stratum: str, *, attackability_branch: str = "full_wave",
) -> tuple[DevelopmentWaveCaseV1, dict[str, Any]]:
    """Construct a complete native load for one fixed source wave.

    `observed_hostile_activity_proxy` is a sensitivity branch: the first
    hostile action is not proof that the target was unattackable earlier.
    """

    if stratum not in WAVE_STRATA:
        raise ValueError(f"unknown fixed stratum: {stratum}")
    if attackability_branch not in {"full_wave", "observed_hostile_activity_proxy"}:
        raise ValueError(f"unsupported attackability branch: {attackability_branch}")
    source = _source_rows()[WAVE_STRATA[stratum]]
    targets = source["targets"]
    hp = [_complete_proxy(target) for target in targets]
    if any(value is None for value in hp):
        raise ValueError("complete per-target kill-budget proxy required")
    hp = [int(value) for value in hp]
    armor_branches = [
        branch for branch in source["base_armor_hypothesis_family"]
        if branch["status"] == "SENSITIVITY_HYPOTHESIS"
        and branch["base_armor"] > 0
    ]
    if not armor_branches:
        raise ValueError("no supported base-armor hypothesis")
    armor = int(armor_branches[0]["base_armor"])
    deaths = [
        int(target["max_health_hypothesis_family"]["observed_kill_budget_proxy"]
            ["death_anchor"]["offset_ms"])
        for target in targets
    ]
    focal = SOURCE_FOCAL_ACTORS[stratum]
    focal_damage = focal["direct_damage_by_target"]
    if len(focal_damage) != len(targets) or any(
        not 0 <= damage < total for damage, total in zip(focal_damage, hp)
    ):
        raise ValueError("direct-GUID focal damage does not fit the source targets")
    watchdog_ms = ceil((max(deaths) + WATCHDOG_MARGIN_MS) / TEAM_INTERVAL_MS) * TEAM_INTERVAL_MS
    base = build_development_wave_case_v1(seed)
    request = deepcopy(base.request)
    template = request["encounter"]["targets"][0]
    request["encounter"]["targets"] = []
    request["encounter"]["duration"] = watchdog_ms / 1000
    for index, target in enumerate(targets):
        model = deepcopy(template)
        model["name"] = f"Upper Kara creature {target['creature_entry_id']} (model {index})"
        model["stats"][26] = armor
        model["stats"][34] = hp[index]
        request["encounter"]["targets"].append(model)

    raw_events: list[tuple[int, int, float]] = []
    attack_events = []
    per_target_team = []
    for index, target in enumerate(targets):
        first = int(target["observed_hostile_activity_proxy"]["start_ms"])
        unlock = first if attackability_branch == "observed_hostile_activity_proxy" else 0
        if unlock > 0:
            attack_events.extend((
                DynamicAttackabilityEventV2(len(attack_events), 0, index, False),
                DynamicAttackabilityEventV2(len(attack_events) + 1, unlock, index, True),
            ))
        # The source total includes the historical focal actor. Replace that
        # actor with the simulated player, so their directly attributed damage
        # cannot occur a second time as background team damage.
        team_budget = hp[index] - focal_damage[index]
        source_fit_count = max(1, ceil((deaths[index] - unlock) / TEAM_INTERVAL_MS))
        hit = team_budget / source_fit_count
        # The original raid did not stop attacking a still-alive replacement
        # target. Continue the fitted exogenous rate to the watchdog; events
        # after model death have no effect. This tail is a sensitivity
        # extrapolation, not an observed historical team action trace.
        scheduled_count = max(source_fit_count, (watchdog_ms - unlock) // TEAM_INTERVAL_MS)
        for step in range(scheduled_count):
            raw_events.append((unlock + (step + 1) * TEAM_INTERVAL_MS, index, hit))
        per_target_team.append({
            "target_index": index, "modeled_team_damage_budget": team_budget,
            "observed_total_incoming_to_death": hp[index],
            "excluded_focal_direct_guid_damage": focal_damage[index],
            "excluded_focal_direct_guid_hit_count": focal["direct_hit_count_by_target"][index],
            "source_death_offset_ms": deaths[index], "model_unlock_ms": unlock,
            "source_fit_hit_count": source_fit_count,
            "scheduled_hit_count_to_watchdog": scheduled_count,
            "post_source_death_rate_extrapolated": True,
            "damage_per_hit": hit,
            "focal_player_direct_guid_excluded_from_source_budget": True,
        })
    raw_events.sort(key=lambda item: (item[0], item[1]))
    background = tuple(
        BackgroundDamageEventV1(index, time_ms, target_index,
                                f"stratified-team-{index:04d}", damage)
        for index, (time_ms, target_index, damage) in enumerate(raw_events)
    )
    config = DynamicTargetSemanticsConfigV3(
        target_health=tuple(DynamicTargetHealthV1(index, value) for index, value in enumerate(hp)),
        idle_advance_horizon_ms=watchdog_ms,
        background_damage_events=background,
        attackability_events=tuple(attack_events),
    )
    load = DynamicRolloutLoadV3.bind(request, seed, config)
    context_evidence = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
        corpus_sha256=source["capsule_sha256"],
        hypothesis_id=f"{stratum}-proxy-center-{attackability_branch}",
    )
    equipment_evidence = base.target_contexts[0].equipment_evidence
    contexts = {}
    for index, target in enumerate(targets):
        contexts[index] = replace(
            base.target_contexts[0],
            context_id=f"{stratum}-target-{index}-{attackability_branch}",
            target_index=index,
            target_name=request["encounter"]["targets"][index]["name"],
            target_max_health=hp[index],
            target_health_pct_evidence=context_evidence,
            target_max_health_evidence=context_evidence,
            target_classification_evidence=context_evidence,
            target_name_evidence=context_evidence,
            target_position_evidence=context_evidence,
            equipment_evidence=equipment_evidence,
        )
    spec = deepcopy(base.case_spec)
    death_anchor = targets[0]["max_health_hypothesis_family"]["observed_kill_budget_proxy"]["death_anchor"]
    spec.update({
        "schema": SCHEMA, "source_instance_id": source["source_identity"]["instance_id"],
        "source_wave_ref": source["source_identity"]["wave_id"],
        "source_target_ref": [target["target_guid"] for target in targets],
        "source_wave_model_target_count": len(targets),
        "required_target_ids": [target["target_guid"] for target in targets],
        "required_target_indices": list(range(len(targets))),
        "stratum": stratum, "attackability_branch": attackability_branch,
        "source_raid_date": None,
        "source_evidence": {
            "wave_capsule": str(DEFAULT_MANIFEST.relative_to(DEFAULT_MANIFEST.parents[4])).replace("\\", "/"),
            "wave_capsule_sha256": source["capsule_sha256"],
            "wave_capsule_bundle_sha256": SOURCE_CAPSULE_BUNDLE_SHA256,
            "source_csv": "offline_data/" + death_anchor["raw_file"],
            "per_target_death_csv_lines": [
                target["max_health_hypothesis_family"]["observed_kill_budget_proxy"]
                ["death_anchor"]["csv_line"] for target in targets
            ],
            "per_target_observed_incoming_damage_to_death": hp,
            "hp_interpretation": "MODEL_HP_FROM_COMPLETE_NORMALIZED_KILL_BUDGET_NOT_EXACT_MAX_HP",
        },
        "initial_state": {
            **base.case_spec["initial_state"],
            "target_max_hp": hp, "target_current_hp": hp,
            "target_base_armor": [armor] * len(targets),
            "target_attackable_at_ms": [row["model_unlock_ms"] for row in per_target_team],
            "target_placement": source["layout"]["variant"] + "_hypothesis",
        },
        "team_background": {
            "model": "EXOGENOUS_PER_TARGET_DIRECT_GUID_LEAVE_ONE_OUT_RATE_EXTRAPOLATED",
            "per_target": per_target_team,
            "source_focal_actor": {
                "guid": focal["guid"], "name": focal["name"],
                "selection_rule": "LOWEST_GUID_WITH_WARRIOR_DPS_ACTION_AND_POSITIVE_TARGET_DAMAGE",
                "exclusion_scope": "DIRECT_SOURCE_GUID_DMG_AND_DEAD_TO_FIRST_TARGET_DEAD",
            },
            "focal_owned_pet_or_unattributed_damage_identified": False,
            "source_focal_build_equivalent_to_controlled_live_build": False,
            "future_schedule_policy_visible": False,
            "player_conditioned": False,
        },
        "terminal": {
            "success": "ALL_REQUIRED_TARGETS_DEAD_CONFIRMED_BY_RECEIPT_AND_FINAL_STATE",
            "watchdog_ms": watchdog_ms,
            "watchdog_outcome": "CENSORED_WATCHDOG_NOT_COMPLETE",
        },
        "request_sha256": load.request_sha256,
        "dynamic_load_contract_sha256": load.contract_sha256,
    })
    # The capsule identifies this raid by instance_id, not a URL slug.
    spec.pop("source_instance_slug", None)
    case = DevelopmentWaveCaseV1(spec, request, load, contexts)
    scenario = build_development_wave_scenario_v1(seed)
    scenario.update({
        "instance_id": source["source_identity"]["instance_id"],
        "component_id": source["runner_projection"]["component_id"],
        "scenario_id": f"{source['scenario_id']}__development_{attackability_branch}",
        "stratum": "single_target" if len(targets) == 1 else "multi_target",
        "horizon_ms": watchdog_ms, "estimated_cost_units": watchdog_ms,
        "request": request, "dynamic_load_config": config.to_wire(),
        "corpus_entry_sha256": source["provenance_hashes"]["corpus_entry_sha256"],
        "source_scenario_sha256": source["provenance_hashes"]["source_scenario_sha256"],
        "catalog_sha256": SOURCE_CAPSULE_BUNDLE_SHA256,
    })
    scenario["scenario_model"].update({
        "request_sha256": load.request_sha256,
        "limitation_codes": [
            "KILL_BUDGET_HP_MODEL_NOT_EXACT", "TEAM_RATE_FROM_DIRECT_GUID_LEAVE_ONE_OUT_DEATH_ANCHOR",
            "FOCAL_OWNED_PET_OR_UNATTRIBUTED_DAMAGE_UNIDENTIFIED",
            "POST_SOURCE_DEATH_TEAM_RATE_EXTRAPOLATED",
            "ATTACKABILITY_HYPOTHESIS_NOT_IDENTIFIED", "WATCHDOG_IS_CENSOR_NOT_WAVE_SUCCESS",
        ],
    })
    bundle = scenario["target_context_bundle"]
    bundle["request_sha256"] = load.request_sha256
    bundle["target_count"] = len(targets)
    original_wire = bundle["contexts"][0]
    bundle["contexts"] = []
    for index, context in contexts.items():
        wire = deepcopy(original_wire)
        wire.update({
            "context_id": context.context_id, "target_index": index,
            "target_name": context.target_name, "target_max_health": hp[index],
        })
        wire["field_evidence"] = {
            "target_health_pct": context.target_health_pct_evidence.to_dict(),
            "target_max_health": context.target_max_health_evidence.to_dict(),
            "target_classification": context.target_classification_evidence.to_dict(),
            "target_name": context.target_name_evidence.to_dict(),
            "equipped_item_names": context.equipment_evidence.to_dict(),
            "target_position": context.target_position_evidence.to_dict(),
        }
        bundle["contexts"].append(wire)
    return case, scenario


def run_stratified_wave_panel_v1(
    seed: int, *, bridge_path: Path | None = None,
    bridge_cwd: Path | None = None, runtime_binding_path: Path | None = None,
) -> dict[str, Any]:
    """One paired seed per fixed wave; multi-target is Cat/anchor only.

    The deployed Contra Raid-B controller is not implemented, so it is not
    silently converted into a single-target or four-way comparison.
    """

    from .development_wave_panel_v1 import (
        DEFAULT_BINDING, DEFAULT_BRIDGE, WORKSPACE_ROOT,
        run_development_wave_panel_v1,
    )
    from .development_two_wave_build_panel_v1 import _two_lane_registry
    from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
    from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID

    rows = []
    for stratum in WAVE_STRATA:
        case, scenario = build_stratified_wave_case_v1(seed, stratum)
        multi = stratum == "multi_two"
        panel = run_development_wave_panel_v1(
            master_seed=seed,
            bridge_path=bridge_path or DEFAULT_BRIDGE,
            bridge_cwd=bridge_cwd or WORKSPACE_ROOT / "wowsims-turtle",
            runtime_binding_path=runtime_binding_path or DEFAULT_BINDING,
            case_override=case, scenario_override=scenario,
            candidate_kind="anchor_13d" if multi else "cat_residual",
            residual_discount_rage=10.0,
            baseline_ids=(CAT_POLICY_ID,) if multi else BASELINE_IDS,
            registry_factory=_two_lane_registry if multi else None,
        )
        rows.append({
            "stratum": stratum, "source_wave_ref": case.case_spec["source_wave_ref"],
            "target_count": len(case.case_spec["required_target_ids"]),
            "team_background_model": case.case_spec["team_background"]["model"],
            "source_focal_guid": case.case_spec["team_background"]["source_focal_actor"]["guid"],
            "excluded_focal_direct_guid_damage": [
                target["excluded_focal_direct_guid_damage"]
                for target in case.case_spec["team_background"]["per_target"]
            ],
            "status": panel["status"], "four_way_complete": panel["four_way_complete"],
            "comparison_scope": "CAT_VS_ANCHOR_ONLY_RAID_B" if multi else "FOUR_NATIVE_POLICIES_RAID_A",
            "policies": [
                {key: lane.get(key) for key in (
                    "policy_id", "role", "status", "own_effective_damage", "ttk_ms",
                    "target_outcomes", "error",
                )} for lane in panel["rows"]
            ],
        })
    return {
        "schema": "development_wave_stratified_panel/v3", "seed": seed,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY", "historical_exact": False,
        "real_superiority_authorized": False, "deployment_authorized": False,
        "source_capsule_bundle_sha256": SOURCE_CAPSULE_BUNDLE_SHA256,
        "wave_count": len(rows), "four_way_wave_count": sum(row["four_way_complete"] for row in rows),
        "rows": rows,
    }


def reduce_stratified_wave_panels_v1(
    panels: list[dict[str, Any]], *, expected_seed_count: int,
) -> dict[str, Any]:
    """Keep complete paired waves distinct from incomplete or unsupported lanes."""

    if len({panel["seed"] for panel in panels}) != len(panels):
        raise ValueError("duplicate panel seed")
    if any(panel.get("schema") != "development_wave_stratified_panel/v3" for panel in panels):
        raise ValueError("cannot mix pre-extrapolation and corrected stratified panels")
    results = []
    for stratum in WAVE_STRATA:
        complete = []
        for panel in panels:
            wave = next(row for row in panel["rows"] if row["stratum"] == stratum)
            lanes = wave["policies"]
            if all(lane["status"] == "COMPLETED" for lane in lanes):
                complete.append((panel["seed"], lanes))
        expected_lane_count = 2 if stratum == "multi_two" else 4
        lane_count_ok = all(len(lanes) == expected_lane_count for _, lanes in complete)
        if not lane_count_ok:
            raise ValueError("unexpected stratum lane count")
        policy_ids = [lane["policy_id"] for lane in complete[0][1]] if complete else []
        scores = {
            policy_id: [
                next(lane["own_effective_damage"] for lane in lanes if lane["policy_id"] == policy_id)
                for _, lanes in complete
            ] for policy_id in policy_ids
        }
        candidate_ids = [lane["policy_id"] for lane in complete[0][1]
                         if lane["role"] == "CANDIDATE"] if complete else []
        if complete and len(candidate_ids) != 1:
            raise ValueError("expected one candidate lane per complete wave")
        candidate_id = candidate_ids[0] if candidate_ids else None
        baseline_ids = [policy_id for policy_id in policy_ids if policy_id != candidate_id]
        paired = {}
        for baseline_id in baseline_ids:
            deltas = [
                candidate - baseline
                for candidate, baseline in zip(scores[candidate_id], scores[baseline_id])
            ]
            paired[baseline_id] = {
                "mean_effective_damage_delta": mean(deltas),
                "standard_error": stdev(deltas) / len(deltas) ** 0.5 if len(deltas) > 1 else None,
            }
        results.append({
            "stratum": stratum,
            "comparison_scope": "CAT_VS_ANCHOR_ONLY_RAID_B" if stratum == "multi_two"
                else "FOUR_NATIVE_POLICIES_RAID_A",
            "matched_complete_seed_count": len(complete),
            "expected_seed_count": expected_seed_count,
            "full_matched_gate": len(complete) == expected_seed_count,
            "policy_mean_effective_damage": {
                policy_id: mean(values) for policy_id, values in scores.items()
            },
            "candidate_minus_baselines": paired,
        })
    return {
        "schema": "development_wave_stratified_reduction/v3",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed_count": len(panels), "expected_seed_count": expected_seed_count,
        "all_fixed_waves_matched": all(row["full_matched_gate"] for row in results),
        "rows": results,
    }


def run_parallel_stratified_wave_panel_v1(
    *, seed_start: int, seed_count: int, workers: int,
    bridge_path: Path | None = None, bridge_cwd: Path | None = None,
    runtime_binding_path: Path | None = None,
) -> dict[str, Any]:
    if seed_count <= 0 or workers <= 0:
        raise ValueError("seed_count and workers must be positive")
    seeds = range(seed_start, seed_start + seed_count)
    execute = partial(
        run_stratified_wave_panel_v1,
        bridge_path=bridge_path, bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
    )
    if workers == 1:
        panels = [execute(seed) for seed in seeds]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            panels = list(pool.map(execute, seeds))
    return {
        "schema": "development_wave_stratified_parallel_panel/v3",
        "seed_start": seed_start, "seed_count": seed_count, "workers": workers,
        "reduction": reduce_stratified_wave_panels_v1(
            panels, expected_seed_count=seed_count
        ),
        "panels": panels,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--bridge-cwd", type=Path)
    parser.add_argument("--runtime-binding", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if args.seed_count == 1:
        result = run_stratified_wave_panel_v1(
            args.seed, bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
        )
    else:
        result = run_parallel_stratified_wave_panel_v1(
            seed_start=args.seed, seed_count=args.seed_count, workers=args.workers,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.seed_count == 1:
        summary = {
            "wave_count": result["wave_count"],
            "four_way_wave_count": result["four_way_wave_count"],
            "statuses": {row["stratum"]: row["status"] for row in result["rows"]},
        }
    else:
        summary = result["reduction"]
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
