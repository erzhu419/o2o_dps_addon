"""Matched-seed aggregate for the genuine two-wave, two-build development probe.

The CLI can run on a staged Linux node with the native bridge.  A seed enters
paired statistics only when both builds and both policies complete the same
two-wave timeline; failed and censored rows remain in the compact output.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Mapping, Sequence

from .development_two_wave_build_panel_v1 import (
    BUILD_IDS,
    SCHEMA as PANEL_SCHEMA,
    run_two_wave_build_panel_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, DEFAULT_BRIDGE, WORKSPACE_ROOT


SCHEMA = "development_two_wave_build_matched_aggregate/v1"
METRICS = (
    "own_effective_damage",
    "whole_two_wave_dps",
    "wave_1_own_effective_damage",
    "wave_2_own_effective_damage",
)


def _compact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    targets = row.get("target_outcomes")
    first, second = (targets if isinstance(targets, list) and len(targets) == 2 else (None, None))
    result = {
        key: row.get(key) for key in (
            "policy_id", "role", "status", "terminal_status", "terminal_reason",
            "error", "own_effective_damage", "whole_two_wave_dps", "ttk_ms",
            "two_wave_timeline_valid", "wave_1_ttk_ms", "inter_wave_gap_ms",
            "wave_2_active_ms", "first_bridge_failure", "execution_blockers",
        )
    }
    result["wave_1_own_effective_damage"] = (
        first.get("simulated_damage_applied") if isinstance(first, Mapping) else None
    )
    result["wave_2_own_effective_damage"] = (
        second.get("simulated_damage_applied") if isinstance(second, Mapping) else None
    )
    return result


def _paired_values(build: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    rows = build.get("rows")
    if not isinstance(rows, list) or len(rows) != 2:
        return None
    projected = [_compact_row(row) for row in rows if isinstance(row, Mapping)]
    if len(projected) != 2:
        return None
    baseline = next((row for row in projected if row["role"] == "BASELINE"), None)
    candidate = next((row for row in projected if row["role"] == "CANDIDATE"), None)
    if baseline is None or candidate is None:
        return None
    if not all(
        row["status"] == "COMPLETED" and row["two_wave_timeline_valid"] is True
        and all(isinstance(row[metric], (int, float)) and math.isfinite(row[metric]) for metric in METRICS)
        for row in (baseline, candidate)
    ):
        return None
    return baseline, candidate


def aggregate_two_wave_build_results_v1(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize exact matched seeds; retain every unsuccessful seed row."""

    if not results:
        raise ValueError("at least one seed result is required")
    seen: set[int] = set()
    seed_reports = []
    valid_pairs: dict[str, list[dict[str, float]]] = {build_id: [] for build_id in BUILD_IDS}
    for result in results:
        seed = result.get("master_seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed in seen:
            raise ValueError("master seeds must be unique integers")
        seen.add(seed)
        builds = result.get("builds")
        compact_builds = []
        pairs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        if isinstance(builds, list):
            for build in builds:
                if not isinstance(build, Mapping):
                    continue
                build_id = build.get("build_id")
                rows = build.get("rows")
                compact_builds.append({
                    "build_id": build_id,
                    "status": build.get("status"),
                    "rows": [_compact_row(row) for row in rows if isinstance(row, Mapping)]
                    if isinstance(rows, list) else None,
                })
                if build_id in valid_pairs:
                    pair = _paired_values(build)
                    if pair is not None:
                        pairs[build_id] = pair
        admitted = (
            result.get("schema") == PANEL_SCHEMA
            and result.get("status") == "TWO_BUILD_TWO_WAVE_COMPLETE_DEVELOPMENT_ONLY"
            and len(compact_builds) == len(BUILD_IDS)
            and set(pairs) == set(BUILD_IDS)
        )
        seed_reports.append({
            "master_seed": seed,
            "status": "MATCHED_COMPLETE" if admitted else "INCOMPLETE_OR_CENSORED",
            "panel_status": result.get("status"),
            "error": result.get("error"),
            "builds": compact_builds,
        })
        if admitted:
            for build_id in BUILD_IDS:
                baseline, candidate = pairs[build_id]
                valid_pairs[build_id].append({
                    metric: float(candidate[metric]) - float(baseline[metric])
                    for metric in METRICS
                })
    aggregates = {}
    for build_id in BUILD_IDS:
        paired = valid_pairs[build_id]
        n = len(paired)
        aggregates[build_id] = {
            "matched_n": n,
            "candidate_minus_cat": {
                metric: {
                    "mean": mean(row[metric] for row in paired) if n else None,
                    "se": stdev(row[metric] for row in paired) / math.sqrt(n) if n >= 2 else None,
                    "unit": "DAMAGE" if "damage" in metric else "DPS",
                }
                for metric in METRICS
            },
        }
    admitted_n = sum(row["status"] == "MATCHED_COMPLETE" for row in seed_reports)
    return {
        "schema": SCHEMA,
        "status": (
            "SMALL_PANEL_PAIRED_COMPLETE_DEVELOPMENT_ONLY"
            if admitted_n == len(seed_reports) and admitted_n >= 2 else
            "SMALL_PANEL_PARTIAL_DEVELOPMENT_ONLY" if admitted_n >= 2 else
            "INSUFFICIENT_MATCHED_SEEDS_DEVELOPMENT_ONLY"
        ),
        "requested_n": len(seed_reports),
        "matched_n": admitted_n,
        "excluded_n": len(seed_reports) - admitted_n,
        "aggregates": aggregates,
        "seed_reports": seed_reports,
        "historical_exact": False,
        "comparison_ready": False,
        "superiority_claim": False,
        "deployment_authorized": False,
    }


def run_two_wave_build_aggregate_v1(
    *, master_seeds: Sequence[int], bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
) -> dict[str, Any]:
    """Execute each small-panel seed on the supplied local or remote bridge."""

    if not master_seeds or len(set(master_seeds)) != len(master_seeds):
        raise ValueError("master_seeds must be nonempty and unique")
    results = []
    for seed in master_seeds:
        try:
            results.append(run_two_wave_build_panel_v1(
                master_seed=seed, bridge_path=bridge_path,
                bridge_cwd=bridge_cwd, runtime_binding_path=runtime_binding_path,
            ))
        except Exception as error:
            results.append({
                "schema": PANEL_SCHEMA,
                "master_seed": seed,
                "status": "FAILED",
                "error": f"{type(error).__name__}: {error}",
                "builds": [],
            })
    return aggregate_two_wave_build_results_v1(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seeds", type=int, nargs="+", required=True)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_two_wave_build_aggregate_v1(
        master_seeds=args.master_seeds,
        bridge_path=args.bridge,
        bridge_cwd=args.bridge_cwd,
        runtime_binding_path=args.runtime_binding,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "requested_n": result["requested_n"],
        "matched_n": result["matched_n"], "excluded_n": result["excluded_n"],
        "aggregates": result["aggregates"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
