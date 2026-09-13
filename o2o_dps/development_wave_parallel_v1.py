"""Parallel, development-only execution of matched whole-wave candidate panels.

Each candidate/seed panel is an independent native bridge process.  Panel
artifacts stay on the execution host; the iteration JSON is the small result
to retrieve.  The existing reducer alone decides whether an update is legal.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

from .development_wave_iteration_v1 import (
    propose_development_wave_candidates_v1,
    reduce_development_wave_iteration_v1,
)
from .development_wave_panel_v1 import run_development_wave_panel_v1


SCHEMA = "development_wave_parallel_execution/v1"


def _failed_panel(seed: int, proposal: Mapping[str, Any], error: Exception) -> dict[str, Any]:
    return {
        "schema": "development_wave_four_policy_panel/v1",
        "candidate_kind": "anchor_13d",
        "anchor_parameters": proposal["parameters"],
        "master_seed": seed,
        "status": "FAILED",
        "error": f"{type(error).__name__}: {error}",
    }


def _execute_panel(
    panel_runner: Callable[..., Mapping[str, Any]], seed: int,
    proposal: Mapping[str, Any], panel_kwargs: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    try:
        panel = dict(panel_runner(
            master_seed=seed, candidate_kind="anchor_13d",
            anchor_parameters=proposal["parameters"], **panel_kwargs,
        ))
    except Exception as error:
        panel = _failed_panel(seed, proposal, error)
    return panel, time.perf_counter() - started


def run_parallel_development_wave_iteration_v1(
    *, master_seeds: Sequence[int], workers: int,
    panel_dir: Path | None = None,
    anchor_parameters: Mapping[str, Any] | None = None,
    panel_runner: Callable[..., Mapping[str, Any]] = run_development_wave_panel_v1,
    executor_factory: Callable[..., Any] = ProcessPoolExecutor,
    **panel_kwargs: Any,
) -> dict[str, Any]:
    """Run independent panels in parallel, then apply the strict 32-seed gate."""

    if not master_seeds or len(set(master_seeds)) != len(master_seeds):
        raise ValueError("master_seeds must be nonempty and unique")
    if workers < 1:
        raise ValueError("workers must be positive")
    proposals = propose_development_wave_candidates_v1(anchor_parameters)
    jobs = [
        (seed, proposal) for proposal in proposals for seed in master_seeds
    ]
    if panel_dir is not None:
        panel_dir = Path(panel_dir)
        existing = [
            panel_dir / f"seed-{seed}" / f"{proposal['candidate_id']}.json"
            for seed, proposal in jobs
            if (panel_dir / f"seed-{seed}" / f"{proposal['candidate_id']}.json").exists()
        ]
        if existing:
            raise FileExistsError(f"panel output already exists: {existing[0]}")
    started = time.perf_counter()
    received: dict[tuple[str, int], Mapping[str, Any]] = {}
    panel_seconds = 0.0
    with executor_factory(max_workers=workers) as executor:
        submitted = {
            executor.submit(_execute_panel, panel_runner, seed, proposal, panel_kwargs):
            (seed, proposal) for seed, proposal in jobs
        }
        for future in as_completed(submitted):
            seed, proposal = submitted[future]
            try:
                panel, elapsed = future.result()
            except Exception as error:
                panel, elapsed = _failed_panel(seed, proposal, error), 0.0
            received[(proposal["candidate_id"], seed)] = panel
            panel_seconds += elapsed
            if panel_dir is not None:
                path = panel_dir / f"seed-{seed}" / f"{proposal['candidate_id']}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(panel, ensure_ascii=False) + "\n", encoding="utf-8")
    panels = {
        proposal["candidate_id"]: [
            received[(proposal["candidate_id"], seed)] for seed in master_seeds
        ]
        for proposal in proposals
    }
    result = reduce_development_wave_iteration_v1(
        proposals=proposals, master_seeds=master_seeds,
        panels_by_candidate=panels,
    )
    incomplete_lanes = []
    for proposal in proposals:
        for seed, panel in zip(master_seeds, panels[proposal["candidate_id"]]):
            if panel.get("four_way_complete") is True:
                continue
            rows = panel.get("rows")
            if not isinstance(rows, list):
                incomplete_lanes.append({
                    "candidate_id": proposal["candidate_id"], "master_seed": seed,
                    "policy_id": None, "status": panel.get("status"),
                    "error": panel.get("error"),
                })
                continue
            for row in rows:
                if row.get("status") != "COMPLETED":
                    incomplete_lanes.append({
                        "candidate_id": proposal["candidate_id"], "master_seed": seed,
                        "policy_id": row.get("policy_id"), "status": row.get("status"),
                        "terminal_reason": row.get("terminal_reason"),
                        "blocker_codes": [
                            item.get("code") for item in row.get("execution_blockers", [])
                        ],
                        "error": row.get("error"),
                    })
    result["execution"] = {
        "schema": SCHEMA,
        "workers": workers,
        "panel_count": len(jobs),
        "four_way_complete_panel_count": sum(
            panel.get("four_way_complete") is True
            for candidate_panels in panels.values() for panel in candidate_panels
        ),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "sum_panel_seconds": round(panel_seconds, 3),
        "panel_artifact_dir": str(panel_dir) if panel_dir is not None else None,
        "incomplete_lane_count": len(incomplete_lanes),
        "incomplete_lane_status_counts": dict(Counter(
            row["status"] for row in incomplete_lanes
        )),
        "incomplete_lanes": incomplete_lanes,
    }
    return result


def reduce_existing_development_wave_panels_v1(
    *, master_seeds: Sequence[int], panel_dir: Path,
    anchor_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce a pilot plus disjoint tail without executing the pilot again."""

    proposals = propose_development_wave_candidates_v1(anchor_parameters)
    panels: dict[str, list[Mapping[str, Any]]] = {}
    loaded = 0
    for proposal in proposals:
        candidate_id = proposal["candidate_id"]
        panels[candidate_id] = []
        for seed in master_seeds:
            path = panel_dir / f"seed-{seed}" / f"{candidate_id}.json"
            if path.is_file():
                panels[candidate_id].append(json.loads(path.read_text(encoding="utf-8")))
                loaded += 1
    result = reduce_development_wave_iteration_v1(
        proposals=proposals, master_seeds=master_seeds,
        panels_by_candidate=panels,
    )
    result["execution"] = {
        "schema": SCHEMA, "mode": "REDUCE_EXISTING_NO_NEW_ROLLOUT",
        "panel_count": loaded, "panel_artifact_dir": str(panel_dir),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seed-start", type=int, required=True)
    parser.add_argument("--master-seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--bridge-cwd", type=Path, required=True)
    parser.add_argument("--runtime-binding", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--panel-dir", type=Path)
    parser.add_argument("--reduce-only", action="store_true")
    args = parser.parse_args()
    if args.master_seed_count < 1:
        parser.error("--master-seed-count must be positive")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    seeds = list(range(args.master_seed_start, args.master_seed_start + args.master_seed_count))
    panel_dir = args.panel_dir or args.output.parent / "panels"
    result = (
        reduce_existing_development_wave_panels_v1(master_seeds=seeds, panel_dir=panel_dir)
        if args.reduce_only else
        run_parallel_development_wave_iteration_v1(
            master_seeds=seeds, workers=args.workers, panel_dir=panel_dir,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "updated": result["updated"],
        "next_candidate_id": result["next_candidate_id"],
        "candidate_results": result["candidate_results"],
        "distinct_outcome_profile_count": result["distinct_outcome_profile_count"],
        "execution": result["execution"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
