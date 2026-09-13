"""Run the pinned Cat terminal guard on fresh node seeds; leave panels remote."""

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
MODULES = (
    "development_wave_panel_v1.py",
    "cat_terminal_queue_guard_v1.py",
    "cat_terminal_queue_guard_lane_v1.py",
    "development_wave_guard_search_v1.py",
)


def run_remote_guard(*, node: str, run_id: str, seed_start: int,
                     seed_count: int, workers: int) -> dict:
    if node not in {f"node{i:03d}" for i in range(1, 7)}:
        raise ValueError("unknown node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
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
        node, f"test -f {project}/bin/o2obridge.linux-amd64", timeout=20, check=False
    )
    if rc != 0:
        raise RuntimeError(f"staged bridge not found: {stderr}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
    ssh_target = scheduler._ssh_target_for_node(node)
    for name in MODULES:
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", ssh_shell, str(ROOT / "o2o_dps" / name),
            f"{ssh_target}:{project}/o2o_dps/{name}",
        ], check=True, timeout=120)

    output = f"{project}/results/cat-terminal-guard-fresh32.json"
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    argv = [
        python, "-m", "o2o_dps.development_wave_guard_search_v1",
        "--master-seed-start", str(seed_start),
        "--master-seed-count", str(seed_count), "--workers", str(workers),
        "--bridge", f"{project}/bin/o2obridge.linux-amd64",
        "--bridge-cwd", project,
        "--runtime-binding", f"{project}/runtime-binding.json",
        "--output", output,
    ]
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, argv))
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=1200, check=False)
    if rc != 0:
        raise RuntimeError(f"remote guard failed rc={rc}: {stderr[-1200:]} {stdout[-1200:]}")
    return {"node": node, "run_id": run_id, "remote_output": output,
            **json.loads(stdout.strip().splitlines()[-1])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    print(json.dumps(run_remote_guard(
        node=args.node, run_id=args.run_id, seed_start=args.seed_start,
        seed_count=args.seed_count, workers=args.workers,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
