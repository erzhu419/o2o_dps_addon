"""Run a small matched two-wave build panel on an already-staged CPU node.

Only the compact aggregate printed by the remote CLI is returned locally;
the detailed per-seed result stays under the remote run's results directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


NODE_NAMES = {f"node{index:03d}" for index in range(1, 7)}
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"


def remote_small_panel_v1(*, node: str, run_id: str, seeds: list[int]) -> dict:
    if node not in NODE_NAMES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("node or run_id is outside the staged development-wave site")
    if not seeds or any(seed <= 0 for seed in seeds) or len(seeds) != len(set(seeds)):
        raise ValueError("seeds must be nonempty, positive, and unique")
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
    output = f"{project}/results/two-wave-build-seeds-{'-'.join(map(str, seeds))}.json"
    args = [
        node_python, "-m", "o2o_dps.development_two_wave_build_aggregate_v1",
        "--master-seeds", *map(str, seeds),
        "--bridge", f"{project}/bin/o2obridge.linux-amd64",
        "--bridge-cwd", project,
        "--runtime-binding", f"{project}/runtime-binding.json",
        "--output", output,
    ]
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, args))
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=1200, check=False)
    if rc != 0:
        raise RuntimeError(f"node two-wave panel failed (rc={rc}): {stderr.strip()} {stdout[-1000:]}")
    compact = json.loads(stdout.strip().splitlines()[-1])
    return {
        "node": node, "run_id": run_id, "seeds": seeds,
        "remote_output": output, **compact,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=sorted(NODE_NAMES), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seeds", type=int, nargs="+", required=True)
    args = parser.parse_args()
    print(json.dumps(remote_small_panel_v1(
        node=args.node, run_id=args.run_id, seeds=args.seeds,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
