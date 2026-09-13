"""Fresh-seed four-way development evaluation of one pinned terminal HS guard.

The candidate was designed from 20260913..20260944 diagnostics.  This module
does not tune it: 15 rage discount and the 35% observed-HP guard are pinned.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path
from statistics import mean, stdev
import time
from typing import Any, Mapping, Sequence

from .development_wave_panel_v1 import run_development_wave_panel_v1
from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
from .cat_terminal_queue_guard_v1 import POLICY_ID


SCHEMA = "development_wave_terminal_guard_fresh_eval/v1"
CANDIDATE_KIND = "cat_terminal_guard"
DISCOUNT_RAGE = 15.0
EXECUTE_APPROACH_HEALTH_PCT = 35.0
MIN_VALIDATION_SEEDS = 32


def reduce_guard_panels_v1(
    *, seeds: Sequence[int], panels: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be nonempty and unique")
    failures: list[dict[str, Any]] = []
    scores: list[dict[str, float]] = []
    guard_activity: list[tuple[int, int]] = []
    for seed in seeds:
        panel = panels.get(seed)
        if panel is None:
            failures.append({"seed": seed, "reason": "NOT_RUN"})
            continue
        rows = panel.get("rows")
        if (
            panel.get("master_seed") != seed
            or panel.get("candidate_kind") != CANDIDATE_KIND
            or panel.get("residual_discount_rage") != DISCOUNT_RAGE
            or panel.get("terminal_guard_execute_approach_health_pct") != EXECUTE_APPROACH_HEALTH_PCT
            or panel.get("status") != "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY"
            or panel.get("four_way_complete") is not True
            or not isinstance(rows, list) or len(rows) != 4
        ):
            failures.append({"seed": seed, "reason": "FOUR_WAY_OR_PROTOCOL_INCOMPLETE"})
            continue
        by_policy = {row.get("policy_id"): row for row in rows}
        if set(by_policy) != {POLICY_ID, *BASELINE_IDS} or any(
            row.get("status") != "COMPLETED"
            or type(row.get("own_effective_damage")) not in (int, float)
            for row in rows
        ):
            failures.append({"seed": seed, "reason": "LANE_SCORE_INCOMPLETE"})
            continue
        candidate_row = by_policy[POLICY_ID]
        interventions = candidate_row.get("guard_intervention_count")
        suppressed = candidate_row.get("guard_suppressed_opportunity_count")
        if any(type(value) is not int or value < 0 for value in (interventions, suppressed)):
            failures.append({"seed": seed, "reason": "GUARD_ACTIVITY_MISSING"})
            continue
        guard_activity.append((interventions, suppressed))
        scores.append({policy_id: float(row["own_effective_damage"])
                       for policy_id, row in by_policy.items()})
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "scope": "PINNED_MODEL_DEFINED_DEVELOPMENT_VALIDATION_ONLY",
        "candidate_policy_id": POLICY_ID,
        "candidate_kind": CANDIDATE_KIND,
        "reserve_discount_rage": DISCOUNT_RAGE,
        "execute_approach_health_pct": EXECUTE_APPROACH_HEALTH_PCT,
        "seeds": list(seeds),
        "complete_paired_seeds": len(scores),
        "failures": failures,
        "baseline_ids": list(BASELINE_IDS),
        "development_gate_passed": False,
        "guard_activity": {
            "intervened_seed_count": sum(count > 0 for count, _ in guard_activity),
            "suppressed_seed_count": sum(count > 0 for _, count in guard_activity),
            "total_interventions": sum(count for count, _ in guard_activity),
            "total_suppressed_opportunities": sum(count for _, count in guard_activity),
        },
        "comparison_ready": False,
        "live_fidelity": False,
        "deployment_authorized": False,
    }
    if failures:
        result["status"] = "INCOMPLETE_NO_VERDICT"
        return result
    comparisons = []
    for baseline_id in BASELINE_IDS:
        delta = [row[POLICY_ID] - row[baseline_id] for row in scores]
        average = mean(delta)
        se = stdev(delta) / len(delta) ** 0.5 if len(delta) > 1 else None
        comparisons.append({
            "baseline_id": baseline_id,
            "mean_baseline_effective_damage": mean(row[baseline_id] for row in scores),
            "mean_candidate_effective_damage": mean(row[POLICY_ID] for row in scores),
            "mean_paired_effective_damage_delta": average,
            "paired_delta_standard_error": se,
            "approx_lower_95_delta": average - 2.04 * se if se is not None else None,
            "positive_seed_count": sum(value > 0 for value in delta),
            "negative_seed_count": sum(value < 0 for value in delta),
            "equal_seed_count": sum(value == 0 for value in delta),
        })
    result["comparisons"] = comparisons
    if len(seeds) < MIN_VALIDATION_SEEDS:
        result["status"] = "SMOKE_ONLY_NO_VERDICT"
    elif all(row["approx_lower_95_delta"] > 0 for row in comparisons):
        result["status"] = "POSITIVE_MODEL_DEFINED_DEVELOPMENT_ONLY"
        result["development_gate_passed"] = True
    else:
        result["status"] = "NO_CONFIDENT_FOUR_WAY_GAIN_DEVELOPMENT_ONLY"
    return result


def _run_one(seed: int, panel_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return run_development_wave_panel_v1(
            master_seed=seed, candidate_kind=CANDIDATE_KIND,
            residual_discount_rage=DISCOUNT_RAGE,
            terminal_guard_execute_approach_health_pct=EXECUTE_APPROACH_HEALTH_PCT,
            **panel_kwargs,
        )
    except Exception as error:
        return {"master_seed": seed, "candidate_kind": CANDIDATE_KIND,
                "status": "FAILED", "error": f"{type(error).__name__}: {error}"}


def run_guard_search_v1(
    *, seeds: Sequence[int], workers: int, panel_dir: Path, **panel_kwargs: Any,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be positive")
    for seed in seeds:
        path = panel_dir / f"seed-{seed}.json"
        if path.exists():
            raise FileExistsError(path)
    started = time.perf_counter()
    panels: dict[int, Mapping[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        jobs = {executor.submit(_run_one, seed, panel_kwargs): seed for seed in seeds}
        for future in as_completed(jobs):
            seed = jobs[future]
            panel = future.result()
            panels[seed] = panel
            path = panel_dir / f"seed-{seed}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(panel, ensure_ascii=False) + "\n", encoding="utf-8")
    result = reduce_guard_panels_v1(seeds=seeds, panels=panels)
    result["execution"] = {
        "mode": "NATIVE_FOUR_WAY",
        "workers": workers,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "panel_artifact_dir": str(panel_dir),
    }
    return result


def reduce_existing_guard_panels_v1(*, seeds: Sequence[int], panel_dir: Path) -> dict[str, Any]:
    panels = {}
    for seed in seeds:
        path = panel_dir / f"seed-{seed}.json"
        if path.is_file():
            panels[seed] = json.loads(path.read_text(encoding="utf-8"))
    result = reduce_guard_panels_v1(seeds=seeds, panels=panels)
    result["execution"] = {
        "mode": "REDUCE_EXISTING_NO_NEW_ROLLOUT",
        "panel_count": len(panels),
        "panel_artifact_dir": str(panel_dir),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seed-start", type=int, required=True)
    parser.add_argument("--master-seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--bridge-cwd", type=Path)
    parser.add_argument("--runtime-binding", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reduce-only", action="store_true")
    args = parser.parse_args()
    if args.master_seed_count < 1 or args.output.exists():
        parser.error("seed count must be positive and output must not exist")
    seeds = list(range(args.master_seed_start, args.master_seed_start + args.master_seed_count))
    panel_dir = args.output.parent / "panels-terminal-guard"
    if args.reduce_only:
        result = reduce_existing_guard_panels_v1(seeds=seeds, panel_dir=panel_dir)
    else:
        if not args.bridge or not args.bridge_cwd or not args.runtime_binding:
            parser.error("native run requires --bridge, --bridge-cwd and --runtime-binding")
        result = run_guard_search_v1(
            seeds=seeds, workers=args.workers, panel_dir=panel_dir,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "complete_paired_seeds": result["complete_paired_seeds"],
        "development_gate_passed": result["development_gate_passed"],
        "guard_activity": result["guard_activity"],
        "comparisons": result.get("comparisons"),
        "failures": result["failures"][:4],
        "execution": result["execution"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
