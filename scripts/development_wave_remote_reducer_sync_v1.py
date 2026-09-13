"""Additively sync reducer-only code to an existing staged wave run.

The panel/policy simulator modules are deliberately not touched: this is for
reducing an already executed pilot and disjoint tail on the same remote host.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
FILES = (
    "development_wave_parallel_v1.py",
    "development_wave_residual_search_v1.py",
)


def sync_reducers(*, node: str, run_id: str) -> dict[str, object]:
    if node not in {f"node{i:03d}" for i in range(1, 7)}:
        raise ValueError("unknown node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
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
    rc, _, stderr = scheduler.run_on(node, f"test -d {project}/results/panels", timeout=20, check=False)
    if rc != 0:
        raise RuntimeError(f"staged panel directory not found: {stderr}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
    ssh_target = scheduler._ssh_target_for_node(node)
    for name in FILES:
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", ssh_shell, str(ROOT / "o2o_dps" / name),
            f"{ssh_target}:{project}/o2o_dps/{name}",
        ], check=True, timeout=120)
    return {"node": node, "run_id": run_id, "synced_reducer_files": list(FILES)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    import json
    print(json.dumps(sync_reducers(node=args.node, run_id=args.run_id)))


if __name__ == "__main__":
    main()
