"""Predeclared twelve-wave Upper Kara development registry.

The wave IDs are selected from the old50 capsule by source-only duration and
target-count strata. A direct source-GUID focal actor is removed from the
observed damage budget before fitting the exogenous team rate. Neither the
kill-budget HP nor that rate is a historical-exact NPC or team model.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import csv
from functools import partial
from functools import lru_cache
import gzip
import json
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from .development_wave_case_v1 import SOURCE_CAPSULE_BUNDLE_SHA256
from .development_wave_coverage_v1 import DEFAULT_MANIFEST, _choose_stratified
from .development_wave_stratified_v1 import build_source_stratified_wave_case_v1


SCHEMA = "development_wave_twelve_case/v1"
# Frozen before policy evaluation: six single-target duration quantiles and
# six multi-target count bins from the source-only coverage selection rule.
WAVE_STRATA_12 = {
    "single_duration_q05": "7ebfce3d-2817-40c9-b216-19699af9a48b:wave:1",
    "single_duration_q20": "2d3acf3f-5dd7-481f-8dcd-da203003b0e3:wave:1",
    "single_duration_q40": "bd6fbc02-70a4-4831-896d-6bb685902d5d:wave:1",
    "single_duration_q60": "b10d350d-1972-4b8d-ae91-905a9efa8a3d:wave:1",
    "single_duration_q80": "63b6f50e-10d0-473c-8767-8a0a2b85a294:wave:1",
    "single_duration_q95": "f989e71f-42a6-420f-8c68-eb65e9be731f:wave:1",
    "multi_2_targets": "5586159a-be6e-41a0-a6bb-7ca288e69137:wave:1",
    "multi_3_targets": "630e972b-7d9c-4c22-9d5f-048326b2b904:wave:1",
    "multi_4_targets": "b29dd9d0-2e22-4457-b1e5-628654502a3c:wave:1",
    "multi_5_6_targets": "33468648-5575-47e4-8ae7-d4eae13afbd1:wave:1",
    "multi_7_9_targets": "9fd47db5-311e-4e5f-9d9a-01e16d7fd7bf:wave:1",
    "multi_10plus_targets": "fdd7f01c-8500-4749-92e1-5fab8413259f:wave:1",
}

# Direct-GUID aggregates audited against the local source CSVs. These are
# bundled instead of uploading the raw raids to a CPU node.
SOURCE_FOCAL_ACTORS_12 = {
    "single_duration_q05": ("0x0000000000259B57", "\u59ec\u5854\u6b66\u5668\u6218", [2569], [6]),
    "single_duration_q20": ("0x00000000002CD7F6", "\u5927\u5409\u7238", [4660], [5]),
    "single_duration_q40": ("0x000000000029AC85", "\u5c10\u72c2\u66b4\u6218\u65e0\u654c", [3442], [13]),
    "single_duration_q60": ("0x0000000000259B57", "\u59ec\u5854\u6b66\u5668\u6218", [1768], [12]),
    "single_duration_q80": ("0x00000000003F2C6A", "\u559c\u60a6", [2667], [5]),
    "single_duration_q95": ("0x00000000003DBD6C", "\u963f\u4fea\u5854", [12000], [20]),
    "multi_2_targets": ("0x00000000004D4CBE", "\u55b3\u55b3\u6656", [3881, 2762], [10, 10]),
    "multi_3_targets": ("0x00000000003D0885", "\u731b\u51fb\u725b\u6b22\u559c", [2673, 1430, 0], [4, 3, 0]),
    "multi_4_targets": ("0x00000000004E8901", "Wsilence", [12069, 5462, 956, 3412], [10, 9, 2, 4]),
    "multi_5_6_targets": ("0x0000000000261A4F", "\u4f0a\u51e1\u4e36", [2695, 11368, 3139, 8555, 10107], [5, 12, 6, 10, 15]),
    "multi_7_9_targets": ("0x0000000000259B57", "\u59ec\u5854\u6b66\u5668\u6218", [3872, 4775, 4330, 1327, 5718, 613, 589, 7462], [5, 7, 9, 2, 9, 2, 2, 17]),
    "multi_10plus_targets": ("0x000000000052BD63", "\u6d3e\u5927\u661f\u808c\u8089\u7248", [4085, 724, 1435, 4071, 7210, 1681, 1543, 0, 815, 2550, 816, 865, 0, 2821, 0, 648], [5, 2, 2, 6, 5, 1, 1, 0, 1, 2, 1, 1, 0, 5, 0, 1]),
}


def focal_actor_12(stratum: str) -> dict[str, Any]:
    guid, name, damage, hits = SOURCE_FOCAL_ACTORS_12[stratum]
    return {
        "guid": guid, "name": name,
        "direct_damage_by_target": list(damage),
        "direct_hit_count_by_target": list(hits),
    }


@lru_cache(maxsize=1)
def source_rows_12() -> dict[str, dict[str, Any]]:
    with gzip.open(DEFAULT_MANIFEST, "rt", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest["content_address"]["sha256"] != SOURCE_CAPSULE_BUNDLE_SHA256:
        raise ValueError("old50 source capsule identity changed")
    selected = _choose_stratified(
        manifest["scenarios"],
        supported_wave_id="86b561f2-1428-4fa4-b9c8-287f351c5fd0:wave:1",
    )
    if {row["stratum"]: row["wave_id"] for row in selected} != WAVE_STRATA_12:
        raise ValueError("source-only twelve-wave selection changed")
    by_wave = {row["source_identity"]["wave_id"]: row for row in manifest["scenarios"]}
    return {stratum: by_wave[wave_id] for stratum, wave_id in WAVE_STRATA_12.items()}


def audit_direct_guid_focal_v1(source: dict[str, Any]) -> dict[str, Any] | None:
    """Read local normalized CSV once; return a small actor exclusion receipt.

    Candidate actor is the lowest GUID with a Warrior DPS marker action and
    positive direct damage to a required target before its first DEAD event.
    This is a deterministic source rule, not a DPS ranking or fitted policy.
    """

    targets = source["targets"]
    encounter = source["source_identity"]["encounter_id"]
    death_index = {
        target["target_guid"]: int(target["max_health_hypothesis_family"]
                                  ["observed_kill_budget_proxy"]["death_anchor"]["event_index"])
        for target in targets
    }
    raw_file = DEFAULT_MANIFEST.parents[4] / "offline_data" / targets[0][
        "max_health_hypothesis_family"]["observed_kill_budget_proxy"]["death_anchor"]["raw_file"]
    eligible: set[str] = set()
    names: dict[str, str] = {}
    damage_by_actor: dict[str, dict[str, int]] = {}
    hits_by_actor: dict[str, dict[str, int]] = {}
    observed = {guid: 0 for guid in death_index}
    marker_actions = {"Bloodthirst", "Mortal Strike", "Whirlwind", "Slam", "Execute"}
    with raw_file.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["Encounter"] != encounter:
                continue
            event_index = int(row["Event Index"])
            if event_index > max(death_index.values()):
                break
            actor = row["Source GUID"]
            guid = row["Target GUID"]
            if actor.startswith("0x0000") and row["Action / Ability"] in marker_actions:
                eligible.add(actor)
                names[actor] = row["Source"]
            if row["Type"] not in {"DMG", "DEAD"} or guid not in death_index:
                continue
            if event_index > death_index[guid]:
                continue
            damage = int(row["Value"].replace(",", ""))
            observed[guid] += damage
            by_target = damage_by_actor.setdefault(actor, {})
            by_target[guid] = by_target.get(guid, 0) + damage
            count_by_target = hits_by_actor.setdefault(actor, {})
            count_by_target[guid] = count_by_target.get(guid, 0) + 1
    expected = [int(target["max_health_hypothesis_family"]["observed_kill_budget_proxy"]["value"])
                for target in targets]
    if [observed[target["target_guid"]] for target in targets] != expected:
        raise ValueError(f"source kill budget does not match raw CSV: {source['scenario_id']}")
    candidates = sorted(actor for actor in eligible if sum(damage_by_actor.get(actor, {}).values()) > 0)
    if not candidates:
        return None
    actor = candidates[0]
    return {
        "guid": actor, "name": names[actor],
        "direct_damage_by_target": [damage_by_actor[actor].get(target["target_guid"], 0)
                                    for target in targets],
        "direct_hit_count_by_target": [hits_by_actor[actor].get(target["target_guid"], 0)
                                       for target in targets],
    }


def build_twelve_wave_case_v1(seed: int, stratum: str):
    if stratum not in WAVE_STRATA_12:
        raise ValueError(f"unknown predeclared stratum: {stratum}")
    source = source_rows_12()[stratum]
    case, scenario = build_source_stratified_wave_case_v1(
        seed, stratum, source, focal_actor_12(stratum), schema=SCHEMA,
    )
    case.case_spec["registry"] = {
        "schema": "development_wave_twelve_registry/v1",
        "selection": "SOURCE_ONLY_DURATION_QUANTILES_AND_TARGET_COUNT_BINS",
        "cohort_wave_count": 1197,
        "predeclared_wave_count": 12,
        "historical_exact": False,
    }
    return case, scenario


def run_twelve_wave_panel_v1(
    seed: int, *, bridge_path: Path | None = None,
    bridge_cwd: Path | None = None, runtime_binding_path: Path | None = None,
    only_stratum: str | None = None,
) -> dict[str, Any]:
    """Retain all twelve rows, including unsupported or failed native lanes."""

    from .development_wave_panel_v1 import (
        DEFAULT_BINDING, DEFAULT_BRIDGE, WORKSPACE_ROOT,
        run_development_wave_panel_v1,
    )
    from .development_two_wave_build_panel_v1 import _two_lane_registry
    from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
    from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID

    if only_stratum is not None and only_stratum not in WAVE_STRATA_12:
        raise ValueError(f"unknown predeclared stratum: {only_stratum}")
    selected = WAVE_STRATA_12 if only_stratum is None else {only_stratum: WAVE_STRATA_12[only_stratum]}
    rows = []
    for stratum, wave_id in selected.items():
        source = source_rows_12()[stratum]
        target_count = len(source["targets"])
        comparison_scope = (
            "FOUR_NATIVE_POLICIES_RAID_A" if target_count == 1
            else "CAT_VS_ANCHOR_ONLY_RAID_B_CONTRA_UNSUPPORTED"
        )
        row: dict[str, Any] = {
            "stratum": stratum, "source_wave_ref": wave_id,
            "source_instance_id": source["source_identity"]["instance_id"],
            "target_count": target_count, "comparison_scope": comparison_scope,
            "status": "NOT_RUN", "four_way_complete": False, "policies": [],
            "unavailable_baselines": ([{
                "policy_id": policy_id, "status": "UNSUPPORTED_RAID_B",
                "own_effective_damage": None,
                "reason": "native multi-target controller contract not implemented",
            } for policy_id in BASELINE_IDS[1:]] if target_count > 1 else []),
        }
        try:
            case, scenario = build_twelve_wave_case_v1(seed, stratum)
            multi = target_count > 1
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
            row.update({
                "status": panel["status"], "four_way_complete": panel["four_way_complete"],
                "team_background_model": case.case_spec["team_background"]["model"],
                "source_focal_guid": case.case_spec["team_background"]["source_focal_actor"]["guid"],
                "excluded_focal_direct_guid_damage": [
                    target["excluded_focal_direct_guid_damage"]
                    for target in case.case_spec["team_background"]["per_target"]
                ],
                "policies": [{key: lane.get(key) for key in (
                    "policy_id", "role", "status", "own_effective_damage", "ttk_ms",
                    "target_outcomes", "error", "terminal_reason",
                    "required_targets_dead", "execution_blockers",
                    "first_bridge_failure",
                )} for lane in panel["rows"]],
            })
        except (ValueError, RuntimeError) as error:
            row["status"] = "UNSUPPORTED_OR_FAILED"
            row["error"] = {"type": type(error).__name__, "message": str(error)}
        rows.append(row)
    return {
        "schema": "development_wave_twelve_panel/v1", "seed": seed,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY", "historical_exact": False,
        "real_superiority_authorized": False, "deployment_authorized": False,
        "source_capsule_bundle_sha256": SOURCE_CAPSULE_BUNDLE_SHA256,
        "wave_count": len(rows), "four_way_wave_count": sum(row["four_way_complete"] for row in rows),
        "rows": rows,
    }


def reduce_twelve_wave_panels_v1(
    panels: list[dict[str, Any]], *, expected_seed_count: int,
    only_stratum: str | None = None,
) -> dict[str, Any]:
    if len({panel["seed"] for panel in panels}) != len(panels):
        raise ValueError("duplicate panel seed")
    if any(panel["schema"] != "development_wave_twelve_panel/v1" for panel in panels):
        raise ValueError("mixed twelve-wave panel schemas")
    if only_stratum is not None and only_stratum not in WAVE_STRATA_12:
        raise ValueError(f"unknown predeclared stratum: {only_stratum}")
    selected = WAVE_STRATA_12 if only_stratum is None else {only_stratum: WAVE_STRATA_12[only_stratum]}
    if any([row["stratum"] for row in panel["rows"]] != list(selected)
           for panel in panels):
        raise ValueError("panel stratum rows do not match the requested registry")
    rows = []
    for stratum in selected:
        attempts = [next(row for row in panel["rows"] if row["stratum"] == stratum)
                    for panel in panels]
        complete = [row for row in attempts
                    if len(row["policies"]) == (4 if row["target_count"] == 1 else 2)
                    and all(lane["status"] == "COMPLETED" for lane in row["policies"])]
        ids = [lane["policy_id"] for lane in complete[0]["policies"]] if complete else []
        candidate = next((lane["policy_id"] for lane in complete[0]["policies"]
                          if lane["role"] == "CANDIDATE"), None) if complete else None
        scores = {policy_id: [next(lane["own_effective_damage"] for lane in row["policies"]
                                    if lane["policy_id"] == policy_id) for row in complete]
                  for policy_id in ids}
        paired = {}
        for baseline in (policy_id for policy_id in ids if policy_id != candidate):
            deltas = [x - y for x, y in zip(scores[candidate], scores[baseline])]
            paired[baseline] = {
                "mean_effective_damage_delta": mean(deltas),
                "standard_error": stdev(deltas) / len(deltas) ** 0.5 if len(deltas) > 1 else None,
            }
        rows.append({
            "stratum": stratum, "source_wave_ref": WAVE_STRATA_12[stratum],
            "comparison_scope": attempts[0]["comparison_scope"] if attempts else None,
            "matched_complete_seed_count": len(complete),
            "expected_seed_count": expected_seed_count,
            "full_matched_gate": len(complete) == expected_seed_count,
            "status_counts": {status: sum(row["status"] == status for row in attempts)
                              for status in sorted({row["status"] for row in attempts})},
            "policy_mean_effective_damage": {key: mean(values) for key, values in scores.items()},
            "candidate_minus_baselines": paired,
        })
    return {
        "schema": "development_wave_twelve_reduction/v1",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed_count": len(panels), "expected_seed_count": expected_seed_count,
        "selected_wave_count": len(selected),
        "all_selected_waves_matched": all(row["full_matched_gate"] for row in rows),
        "all_predeclared_waves_matched": (
            only_stratum is None and all(row["full_matched_gate"] for row in rows)
        ),
        "rows": rows,
    }


def run_parallel_twelve_wave_panel_v1(
    *, seed_start: int, seed_count: int, workers: int,
    bridge_path: Path | None = None, bridge_cwd: Path | None = None,
    runtime_binding_path: Path | None = None,
    only_stratum: str | None = None,
) -> dict[str, Any]:
    if seed_count <= 0 or workers <= 0:
        raise ValueError("seed_count and workers must be positive")
    execute = partial(
        run_twelve_wave_panel_v1,
        bridge_path=bridge_path, bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
        only_stratum=only_stratum,
    )
    seeds = range(seed_start, seed_start + seed_count)
    if workers == 1:
        panels = [execute(seed) for seed in seeds]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            panels = list(pool.map(execute, seeds))
    return {
        "schema": "development_wave_twelve_parallel_panel/v1",
        "seed_start": seed_start, "seed_count": seed_count, "workers": workers,
        "reduction": reduce_twelve_wave_panels_v1(
            panels, expected_seed_count=seed_count, only_stratum=only_stratum,
        ),
        "panels": panels,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-source", action="store_true")
    parser.add_argument("--stratum", choices=tuple(WAVE_STRATA_12))
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--seed-count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--bridge-cwd", type=Path)
    parser.add_argument("--runtime-binding", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.audit_source:
        print(json.dumps({label: audit_direct_guid_focal_v1(source)
                          for label, source in source_rows_12().items()}, ensure_ascii=True, indent=2))
        return
    if args.output is None:
        parser.error("--output is required unless auditing source")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if args.seed_count == 1:
        result = run_twelve_wave_panel_v1(
            args.seed, bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
            only_stratum=args.stratum,
        )
        summary = {"wave_count": result["wave_count"],
                   "status_counts": {status: sum(row["status"] == status for row in result["rows"])
                                     for status in sorted({row["status"] for row in result["rows"]})}}
    else:
        result = run_parallel_twelve_wave_panel_v1(
            seed_start=args.seed, seed_count=args.seed_count, workers=args.workers,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
            only_stratum=args.stratum,
        )
        summary = result["reduction"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
