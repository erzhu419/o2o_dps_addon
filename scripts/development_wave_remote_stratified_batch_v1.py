"""Run the small stratified whole-wave panel on a staged CPU node."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"


def run_stratified_batch(*, node: str, run_id: str, seed_start: int,
                         seed_count: int, workers: int, tag: str = "v1") -> dict:
    if node not in {f"node{i:03d}" for i in range(1, 7)}:
        raise ValueError("unknown node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tag):
        raise ValueError("invalid result tag")
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
    node_python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    output = f"{project}/results/stratified-{seed_start}-{seed_count}-{tag}.json"
    argv = [
        node_python, "-m", "o2o_dps.development_wave_stratified_v1",
        "--seed", str(seed_start), "--seed-count", str(seed_count),
        "--workers", str(workers),
        "--bridge", f"{project}/bin/o2obridge.linux-amd64",
        "--bridge-cwd", project,
        "--runtime-binding", f"{project}/runtime-binding.json",
        "--output", output,
    ]
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, argv))
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=1200, check=False)
    if rc != 0:
        raise RuntimeError(f"remote stratified batch failed rc={rc}: {stderr[-1200:]} {stdout[-1200:]}")
    return {"node": node, "run_id": run_id, "remote_output": output,
            "reduction": json.loads(stdout.strip().splitlines()[-1])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()
    print(json.dumps(run_stratified_batch(
        node=args.node, run_id=args.run_id, seed_start=args.seed_start,
        seed_count=args.seed_count, workers=args.workers, tag=args.tag,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
