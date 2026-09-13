"""Matched whole-wave search over a small Cat-relative queue rule.

The zero-residual controller is native Cat.  Selection here is development
only; a new scenario/seed confirmation and real-client test remain separate.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path
from statistics import mean, stdev
import time
from typing import Any, Mapping, Sequence

from .development_wave_panel_v1 import run_development_wave_panel_v1
from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS


SCHEMA = "development_wave_residual_search/v1"
DISCOUNTS = (0.0, 5.0, 10.0, 15.0, 20.0, 25.0)
MIN_SELECTION_SEEDS = 32


def _key(discount: float) -> str:
    return f"{discount:g}"


def reduce_residual_search_v1(
    *, seeds: Sequence[int], discounts: Sequence[float],
    panels: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    if len(seeds) < 1 or len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be nonempty and unique")
    if not discounts or discounts[0] != 0 or len(set(discounts)) != len(discounts):
        raise ValueError("discounts must be distinct and start with zero residual")
    reference: dict[int, tuple[Any, Any, tuple[float, ...]]] = {}
    scores: dict[str, list[tuple[float, dict[str, float]]]] = {}
    failures: list[dict[str, Any]] = []
    for discount in discounts:
        key = _key(discount)
        samples: list[tuple[float, dict[str, float]]] = []
        candidate_panels = panels.get(key, ())
        indexed = {row.get("master_seed"): row for row in candidate_panels}
        if len(indexed) != len(candidate_panels):
            failures.append({"discount": discount, "reason": "DUPLICATE_OR_MALFORMED_SEED"})
        for seed in seeds:
            panel = indexed.get(seed)
            if not isinstance(panel, Mapping):
                failures.append({"discount": discount, "seed": seed, "reason": "NOT_RUN"})
                continue
            rows = panel.get("rows")
            if (
                panel.get("status") != "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY"
                or panel.get("four_way_complete") is not True
                or panel.get("candidate_kind") != "cat_residual"
                or panel.get("residual_discount_rage") != discount
                or not isinstance(rows, list) or len(rows) != 4
                or any(row.get("status") != "COMPLETED" for row in rows)
            ):
                failures.append({"discount": discount, "seed": seed,
                                 "reason": "FOUR_WAY_NOT_COMPLETE"})
                continue
            baseline = {row["policy_id"]: row["own_effective_damage"]
                        for row in rows if row["policy_id"] in BASELINE_IDS}
            candidate = [row for row in rows if row.get("role") == "CANDIDATE"]
            if len(baseline) != 3 or len(candidate) != 1 or any(
                type(value) not in (int, float) for value in
                (*baseline.values(), candidate[0]["own_effective_damage"])
            ):
                failures.append({"discount": discount, "seed": seed,
                                 "reason": "LANE_SCORE_MISSING"})
                continue
            identity = (panel.get("case"), panel.get("simulator_seed"),
                        tuple(baseline[policy_id] for policy_id in BASELINE_IDS))
            if seed in reference and reference[seed] != identity:
                failures.append({"discount": discount, "seed": seed,
                                 "reason": "PAIRED_SCENARIO_OR_BASELINE_DRIFT"})
                continue
            reference[seed] = identity
            samples.append((float(candidate[0]["own_effective_damage"]),
                            {policy_id: float(baseline[policy_id]) for policy_id in BASELINE_IDS}))
        scores[key] = samples
    summary = {
        "schema": SCHEMA, "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seeds": list(seeds), "discounts": list(discounts),
        "panel_count": sum(len(rows) for rows in panels.values()),
        "failures": failures, "selected_discount_rage": 0.0,
        "updated": False, "comparison_ready": False,
        "deployment_authorized": False, "candidate_results": [],
    }
    if failures:
        summary["status"] = "EVIDENCE_INCOMPLETE_NO_UPDATE"
        return summary
    for discount in discounts:
        pairs = scores[_key(discount)]
        delta = [candidate - baseline[BASELINE_IDS[0]] for candidate, baseline in pairs]
        avg = mean(delta)
        se = stdev(delta) / len(delta) ** 0.5 if len(delta) > 1 else None
        summary["candidate_results"].append({
            "reserve_discount_rage": discount,
            "mean_cat_effective_damage": mean(baseline[BASELINE_IDS[0]] for _, baseline in pairs),
            "mean_candidate_effective_damage": mean(candidate for candidate, _ in pairs),
            "mean_paired_damage_vs_cat": avg,
            "mean_paired_damage_vs_baselines": {
                policy_id: mean(candidate - baseline[policy_id] for candidate, baseline in pairs)
                for policy_id in BASELINE_IDS
            },
            "paired_delta_se": se,
            "approx_lower_95_paired_delta": avg - 2.04 * se if se is not None else None,
        })
    if len(seeds) < MIN_SELECTION_SEEDS:
        summary["status"] = "SMOKE_ONLY_NO_UPDATE"
        return summary
    best = max(summary["candidate_results"], key=lambda row: row["mean_paired_damage_vs_cat"])
    if best["reserve_discount_rage"] and best["approx_lower_95_paired_delta"] > 0:
        summary.update({"status": "UPDATED_DEVELOPMENT_ONLY", "updated": True,
                        "selected_discount_rage": best["reserve_discount_rage"]})
    else:
        summary["status"] = "NO_CONFIDENT_POSITIVE_UPDATE_DEVELOPMENT_ONLY"
    return summary


def _run_one(seed: int, discount: float, panel_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return run_development_wave_panel_v1(
            master_seed=seed, candidate_kind="cat_residual",
            residual_discount_rage=discount, **panel_kwargs,
        )
    except Exception as error:
        return {"master_seed": seed, "candidate_kind": "cat_residual",
                "residual_discount_rage": discount, "status": "FAILED",
                "error": f"{type(error).__name__}: {error}"}


def run_residual_search_v1(
    *, seeds: Sequence[int], workers: int, panel_dir: Path,
    discounts: Sequence[float] = DISCOUNTS, **panel_kwargs: Any,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be positive")
    jobs = [(seed, discount) for seed in seeds for discount in discounts]
    for seed, discount in jobs:
        path = panel_dir / f"seed-{seed}" / f"discount-{_key(discount)}.json"
        if path.exists():
            raise FileExistsError(path)
    panels: dict[str, list[Mapping[str, Any]]] = {_key(discount): [] for discount in discounts}
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as executor:
        pending = {executor.submit(_run_one, seed, discount, panel_kwargs): (seed, discount)
                   for seed, discount in jobs}
        for future in as_completed(pending):
            seed, discount = pending[future]
            panel = future.result()
            panels[_key(discount)].append(panel)
            path = panel_dir / f"seed-{seed}" / f"discount-{_key(discount)}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(panel, ensure_ascii=False) + "\n", encoding="utf-8")
    result = reduce_residual_search_v1(seeds=seeds, discounts=discounts, panels=panels)
    incomplete = [
        {"seed": panel.get("master_seed"), "discount": discount,
         "policy_id": row.get("policy_id"), "status": row.get("status"),
         "blocker_codes": [item.get("code") for item in row.get("execution_blockers", ())]}
        for discount in discounts for panel in panels[_key(discount)]
        for row in panel.get("rows", ()) if row.get("status") != "COMPLETED"
    ]
    result["execution"] = {"workers": workers, "wall_seconds": round(time.perf_counter() - started, 3),
                           "panel_artifact_dir": str(panel_dir),
                           "incomplete_lane_count": len(incomplete),
                           "incomplete_lane_status_counts": dict(Counter(
                               row["status"] for row in incomplete)),
                           "incomplete_lanes": incomplete[:8]}
    return result


def reduce_existing_residual_panels_v1(
    *, seeds: Sequence[int], panel_dir: Path,
    discounts: Sequence[float] = DISCOUNTS,
) -> dict[str, Any]:
    """Reduce disjoint staged batches without recomputing existing seeds."""

    panels: dict[str, list[Mapping[str, Any]]] = {}
    loaded = 0
    for discount in discounts:
        key = _key(discount)
        panels[key] = []
        for seed in seeds:
            path = panel_dir / f"seed-{seed}" / f"discount-{key}.json"
            if path.is_file():
                panels[key].append(json.loads(path.read_text(encoding="utf-8")))
                loaded += 1
    result = reduce_residual_search_v1(seeds=seeds, discounts=discounts, panels=panels)
    result["execution"] = {"mode": "REDUCE_EXISTING_NO_NEW_ROLLOUT",
                           "panel_count": loaded, "panel_artifact_dir": str(panel_dir)}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seed-start", type=int, required=True)
    parser.add_argument("--master-seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--bridge", required=True, type=Path)
    parser.add_argument("--bridge-cwd", required=True, type=Path)
    parser.add_argument("--runtime-binding", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reduce-only", action="store_true")
    args = parser.parse_args()
    if args.master_seed_count < 1 or args.output.exists():
        parser.error("seed count must be positive and output must not exist")
    seeds = list(range(args.master_seed_start, args.master_seed_start + args.master_seed_count))
    panel_dir = args.output.parent / "panels-residual"
    result = (
        reduce_existing_residual_panels_v1(seeds=seeds, panel_dir=panel_dir)
        if args.reduce_only else
        run_residual_search_v1(
            seeds=seeds, workers=args.workers, panel_dir=panel_dir,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "updated": result["updated"],
                      "selected_discount_rage": result["selected_discount_rage"],
                      "candidate_results": result["candidate_results"],
                      "execution": result["execution"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
