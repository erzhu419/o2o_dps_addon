"""Stage only the one-wave executable closure on a scheduleurm CPU node.

Run this script with WSL Python.  It uploads source/configuration and a Linux
bridge, never Chronicle raw CSVs or checkpoints, and does not launch a job.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PROJECT_ROOT.parent
ADDONS_ROOT = BRAIN_ROOT.parent
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
BRIDGE = PROJECT_ROOT / "bin/o2obridge.seedfix-v11.withdb.goamd64v1.linux-amd64"
BINDING = PROJECT_ROOT / ".hpc-local/smokes/cat-gap-three-baseline-v1/deployed-contra-runtime-binding-v1.951b8faa.json"
NODE_NAMES = {f"node{index:03d}" for index in range(1, 7)}


def _scheduler():
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]
    return scheduler


def stage_development_wave_v1(*, node: str, run_id: str) -> dict[str, str]:
    if node not in NODE_NAMES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("node or run_id is outside the development-wave site")
    scheduler = _scheduler()
    rc, home, stderr = scheduler.run_on(node, "cd; pwd", timeout=20, check=False)
    home = home.strip()
    if rc != 0 or not home.startswith("/home/") or "\n" in home:
        raise RuntimeError(f"node home discovery failed: {stderr}")
    remote_root = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/"
        f"development-wave-61944-v1/{run_id}"
    )
    project = f"{remote_root}/AddOns/BrainOfCat/o2o-dps"
    rc, _, stderr = scheduler.run_on(
        node, f"test ! -e {project}", timeout=20, check=False
    )
    if rc != 0:
        raise RuntimeError(f"run directory already exists; choose a new run_id: {project}: {stderr}")
    destinations = [
        project + "/o2o_dps", project + "/configs", project + "/bin",
        remote_root + "/AddOns/Cat", remote_root + "/AddOns/Contra",
        remote_root + "/AddOns/Cat2",
        remote_root + "/AddOns/BrainOfCat/Cat2_new",
        remote_root + "/AddOns/BrainOfCat/Contra_new",
    ]
    rc, _, stderr = scheduler.run_on(
        node, "mkdir -p " + " ".join(destinations), timeout=30, check=False
    )
    if rc != 0:
        raise RuntimeError(f"remote run directory creation failed: {stderr}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
    ssh_target = scheduler._ssh_target_for_node(node)

    def copy(source: Path, destination: str, *, source_code_only: bool = False) -> None:
        if not source.exists():
            raise FileNotFoundError(source)
        command = ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms"]
        if source_code_only:
            command.extend([
                "--include=*/", "--include=*.py", "--include=*.lua",
                "--include=*.toc", "--include=*.xml", "--exclude=*",
            ])
        source_arg = str(source) + ("/" if source.is_dir() else "")
        command.extend(["-e", ssh_shell, source_arg, ssh_target + ":" + destination])
        subprocess.run(command, check=True, timeout=180)

    copy(PROJECT_ROOT / "o2o_dps/", project + "/o2o_dps/", source_code_only=True)
    copy(PROJECT_ROOT / "configs/", project + "/configs/")
    copy(BRIDGE, project + "/bin/o2obridge.linux-amd64")
    copy(BINDING, project + "/runtime-binding.json")
    for source, destination in (
        (ADDONS_ROOT / "Cat/", remote_root + "/AddOns/Cat/"),
        (ADDONS_ROOT / "Contra/", remote_root + "/AddOns/Contra/"),
        (ADDONS_ROOT / "Cat2/", remote_root + "/AddOns/Cat2/"),
        (BRAIN_ROOT / "Cat2_new/", remote_root + "/AddOns/BrainOfCat/Cat2_new/"),
        (BRAIN_ROOT / "Contra_new/", remote_root + "/AddOns/BrainOfCat/Contra_new/"),
    ):
        # Cat2's pinned identity covers every file in the tree, including
        # non-code metadata.  Its complete source tree is only about 2 MB.
        copy(source, destination, source_code_only=source.name != "Cat2_new")
    rc, _, stderr = scheduler.run_on(
        node, f"chmod u+x {project}/bin/o2obridge.linux-amd64", timeout=20, check=False
    )
    if rc != 0:
        raise RuntimeError(f"remote bridge executable bit failed: {stderr}")
    return {
        "node": node, "run_root": remote_root, "project_root": project,
        "bridge": project + "/bin/o2obridge.linux-amd64",
        "runtime_binding": project + "/runtime-binding.json",
        "status": "STAGED_NO_JOB_LAUNCHED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=sorted(NODE_NAMES), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    import json
    print(json.dumps(stage_development_wave_v1(node=args.node, run_id=args.run_id)))


if __name__ == "__main__":
    main()
