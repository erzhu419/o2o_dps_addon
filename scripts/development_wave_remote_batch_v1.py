"""Run a staged development batch on one scheduleurm CPU node; return only a small summary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
NODES = {f"node{index:03d}" for index in range(1, 7)}


def run_remote_batch_v1(*, node: str, run_id: str, kind: str, tag: str,
                        seed_start: int, seed_count: int, workers: int,
                        reduce_only: bool = False) -> dict:
    if node not in NODES or kind not in {"anchor_13d", "cat_residual"}:
        raise ValueError("unknown node or batch kind")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value)
           for value in (run_id, tag)):
        raise ValueError("run_id and tag must be simple path components")
    if seed_start <= 0 or seed_count <= 0 or workers <= 0:
        raise ValueError("seed range and workers must be positive")
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]

    rc, home, stderr = scheduler.run_on(node, "cd; pwd", timeout=20, check=False)
    home = home.strip()
    if rc != 0 or not home.startswith("/home/") or "\n" in home:
        raise RuntimeError(f"node home discovery failed: {stderr}")
    project = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/"
        f"development-wave-61944-v1/{run_id}/AddOns/BrainOfCat/o2o-dps"
    )
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    module = ("o2o_dps.development_wave_parallel_v1" if kind == "anchor_13d"
              else "o2o_dps.development_wave_residual_search_v1")
    output = f"{project}/results/{kind}-{tag}.json"
    argv = [
        python, "-m", module,
        "--master-seed-start", str(seed_start),
        "--master-seed-count", str(seed_count),
        "--workers", str(workers),
        "--bridge", f"{project}/bin/o2obridge.linux-amd64",
        "--bridge-cwd", project,
        "--runtime-binding", f"{project}/runtime-binding.json",
        "--output", output,
    ]
    if reduce_only:
        argv.append("--reduce-only")
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, argv))
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=900, check=False)
    if rc != 0:
        raise RuntimeError(f"remote batch failed rc={rc}: {stderr[-1200:]} {stdout[-1200:]}")
    compact = json.loads(stdout.strip().splitlines()[-1])
    return {"node": node, "run_id": run_id, "kind": kind, "tag": tag,
            "remote_output": output, **compact}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=sorted(NODES), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--kind", choices=("anchor_13d", "cat_residual"), required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--reduce-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_remote_batch_v1(
        node=args.node, run_id=args.run_id, kind=args.kind, tag=args.tag,
        seed_start=args.seed_start, seed_count=args.seed_count, workers=args.workers,
        reduce_only=args.reduce_only,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
