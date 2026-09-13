"""Upload the fixed stratified-wave code and small derived capsule, not raw logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
CAPSULE = ROOT / (
    "offline_data/derived/fury_offline_scenario_capsules/v2/"
    "fury_offline_scenario_capsules_v2."
    "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
)
MODULE = ROOT / "o2o_dps/development_wave_stratified_v1.py"


def sync_stratified(*, node: str, run_id: str) -> dict[str, object]:
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
    rc, _, stderr = scheduler.run_on(node, f"test -d {project}/results", timeout=20, check=False)
    if rc != 0:
        raise RuntimeError(f"staged run not found: {stderr}")
    remote_capsule = f"{project}/{CAPSULE.relative_to(ROOT).as_posix()}"
    rc, _, stderr = scheduler.run_on(
        node, f"mkdir -p {Path(remote_capsule).parent.as_posix()}", timeout=20, check=False
    )
    if rc != 0:
        raise RuntimeError(f"cannot create capsule destination: {stderr}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
    ssh_target = scheduler._ssh_target_for_node(node)
    for source, target in ((MODULE, f"{project}/o2o_dps/{MODULE.name}"),
                           (CAPSULE, remote_capsule)):
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", ssh_shell, str(source), f"{ssh_target}:{target}",
        ], check=True, timeout=120)
    return {"node": node, "run_id": run_id, "module": MODULE.name,
            "derived_capsule_bytes": CAPSULE.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(sync_stratified(node=args.node, run_id=args.run_id)))


if __name__ == "__main__":
    main()
