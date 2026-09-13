"""Additively sync and run the small uncertainty ensemble on a staged CPU node.

Only the new Python module is uploaded.  Per-panel artifacts and the summary
remain remote; stdout returns a compact result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
NODES = {f"node{i:03d}" for i in range(1, 7)}
MODULE_NAME = "development_wave_uncertainty_v1.py"


def run_remote_uncertainty_v1(*, node: str, run_id: str, tag: str,
                              seed_start: int, seed_count: int,
                              workers: int, discount: float) -> dict:
    if node not in NODES:
        raise ValueError("unknown node")
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
    rc, _, stderr = scheduler.run_on(
        node, f"test -f {shlex.quote(project)}/bin/o2obridge.linux-amd64",
        timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"staged bridge not found: {stderr}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
    ssh_target = scheduler._ssh_target_for_node(node)
    subprocess.run([
        "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
        "-e", ssh_shell, str(ROOT / "o2o_dps" / MODULE_NAME),
        f"{ssh_target}:{project}/o2o_dps/{MODULE_NAME}",
    ], check=True, timeout=120)

    output = f"{project}/results/uncertainty-{tag}.json"
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    argv = [
        python, "-m", "o2o_dps.development_wave_uncertainty_v1",
        "--seed-start", str(seed_start), "--seed-count", str(seed_count),
        "--workers", str(workers),
        "--residual-discount-rage", str(discount),
        "--bridge", f"{project}/bin/o2obridge.linux-amd64",
        "--bridge-cwd", project,
        "--runtime-binding", f"{project}/runtime-binding.json",
        "--output", output,
    ]
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, argv))
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=1200, check=False)
    if rc != 0:
        raise RuntimeError(f"remote uncertainty failed rc={rc}: {stderr[-1200:]} {stdout[-1200:]}")
    compact = json.loads(stdout.strip().splitlines()[-1])
    return {"node": node, "run_id": run_id, "tag": tag,
            "remote_output": output, **compact}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=sorted(NODES), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--residual-discount-rage", type=float, default=10)
    args = parser.parse_args()
    print(json.dumps(run_remote_uncertainty_v1(
        node=args.node, run_id=args.run_id, tag=args.tag,
        seed_start=args.seed_start, seed_count=args.seed_count,
        workers=args.workers, discount=args.residual_discount_rage,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
