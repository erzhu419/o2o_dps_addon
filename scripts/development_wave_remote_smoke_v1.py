"""Run one already-staged whole-wave panel on a scheduleurm CPU node."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


NODE_NAMES = {f"node{index:03d}" for index in range(1, 7)}
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"


def remote_smoke_v1(*, node: str, run_id: str, seed: int, candidate: str) -> dict:
    if node not in NODE_NAMES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("node or run_id is outside the development-wave site")
    if seed <= 0 or candidate not in {"anchor_13d", "cat_residual"}:
        raise ValueError("invalid seed or candidate")
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
    node_python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    output = f"{project}/results/{candidate}-seed-{seed}.json"
    args = [
        node_python, "-m", "o2o_dps.development_wave_panel_v1",
        "--candidate", candidate, "--master-seed", str(seed),
        "--bridge", f"{project}/bin/o2obridge.linux-amd64",
        "--bridge-cwd", project,
        "--runtime-binding", f"{project}/runtime-binding.json",
        "--output", output,
    ]
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, args))
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=120, check=False)
    if rc != 0:
        raise RuntimeError(f"node panel failed (rc={rc}): {stderr.strip()} {stdout[-1000:]}")
    result = json.loads(stdout.strip().splitlines()[-1])
    return {
        "node": node, "run_id": run_id, "candidate": candidate, "master_seed": seed,
        "remote_output": output, "status": result["status"],
        "completed_count": result["completed_count"],
        "four_way_complete": result["four_way_complete"],
        "rows": [
            {key: row.get(key) for key in ("policy_id", "status", "own_effective_damage", "ttk_ms", "error")}
            for row in result["rows"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=sorted(NODE_NAMES), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--candidate", choices=("anchor_13d", "cat_residual"), required=True)
    args = parser.parse_args()
    print(json.dumps(remote_smoke_v1(
        node=args.node, run_id=args.run_id, seed=args.seed, candidate=args.candidate,
    )))


if __name__ == "__main__":
    main()
