"""Stage the compact factored-press experiment closure on shared CPU storage.

Run with WSL Python.  The six node001--node006 hosts must expose the same home
directory inode before ``--skip-launch-staging`` is safe.  This stages only
Python/configuration, the exact Linux bridge, compact derived wave/build
artifacts, and the wowsims item database.  Chronicle CSVs and checkpoints are
never copied.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PROJECT_ROOT.parent
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
NODE_NAMES = tuple(f"node{index:03d}" for index in range(1, 7))
SOURCE_CAPSULE = (
    PROJECT_ROOT
    / "offline_data/derived/fury_offline_scenario_capsules/v2"
    / (
        "fury_offline_scenario_capsules_v2."
        "23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
    )
)
SELECTOR_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data/derived/historical_representative_build_selector/v1"
)
CATALOG_DIRECTORY = (
    PROJECT_ROOT / "offline_data/derived/historical_build_catalog/v1"
)
ITEM_DATABASE = BRAIN_ROOT / "wowsims-turtle/assets/database/db.json"


def _scheduler():
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]

    return scheduler


def _shared_home(scheduler: Any) -> tuple[str, dict[str, str]]:
    receipts: dict[str, str] = {}
    homes: set[str] = set()
    identities: set[str] = set()
    for node in NODE_NAMES:
        rc, stdout, stderr = scheduler.run_on(
            node, "cd; pwd; stat -c '%d:%i' .", timeout=20, check=False,
        )
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if (
            rc != 0 or len(lines) != 2 or not lines[0].startswith("/home/")
            or "\n" in lines[0] or not re.fullmatch(r"[0-9]+:[0-9]+", lines[1])
        ):
            raise RuntimeError(
                f"shared-home probe failed on {node}: {stderr[-500:]}"
            )
        homes.add(lines[0])
        identities.add(lines[1])
        receipts[node] = f"{lines[0]}:{lines[1]}"
    if len(homes) != 1 or len(identities) != 1:
        raise RuntimeError("node001--node006 do not expose one shared home inode")
    return next(iter(homes)), receipts


def stage_factored_press_v1(
    *, staging_node: str, run_id: str, bridge: Path,
) -> dict[str, Any]:
    if staging_node not in NODE_NAMES:
        raise ValueError("staging node must be node001--node006")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    bridge = bridge.expanduser().resolve(strict=True)
    sources = (
        PROJECT_ROOT / "o2o_dps",
        PROJECT_ROOT / "configs",
        SOURCE_CAPSULE,
        SELECTOR_DIRECTORY,
        CATALOG_DIRECTORY,
        ITEM_DATABASE,
        bridge,
    )
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        raise FileNotFoundError("missing stage inputs: " + ", ".join(missing))

    scheduler = _scheduler()
    home, shared_home_receipts = _shared_home(scheduler)
    remote_root = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/"
        f"factored-press-v1/{run_id}"
    )
    project = f"{remote_root}/AddOns/BrainOfCat/o2o-dps"
    rc, _, stderr = scheduler.run_on(
        staging_node, f"test ! -e {project}", timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(
            f"run directory already exists; choose a new run_id: {project}: {stderr}"
        )
    destinations = (
        f"{project}/o2o_dps",
        f"{project}/configs",
        f"{project}/bin",
        f"{project}/results",
        f"{project}/offline_data/derived/fury_offline_scenario_capsules/v2",
        f"{project}/offline_data/derived/historical_representative_build_selector/v1",
        f"{project}/offline_data/derived/historical_build_catalog/v1",
        f"{remote_root}/AddOns/BrainOfCat/wowsims-turtle/assets/database",
    )
    rc, _, stderr = scheduler.run_on(
        staging_node, "mkdir -p " + " ".join(destinations),
        timeout=30, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote run directory creation failed: {stderr}")

    ssh_shell = scheduler._ssh_rsync_shell_for_node(staging_node)
    ssh_target = scheduler._ssh_target_for_node(staging_node)

    def copy(
        source: Path, destination: str, *, python_source_only: bool = False,
    ) -> None:
        command = [
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
        ]
        if python_source_only:
            command.extend([
                "--include=*/", "--include=*.py", "--exclude=*",
            ])
        source_arg = str(source) + ("/" if source.is_dir() else "")
        command.extend([
            "-e", ssh_shell, source_arg, f"{ssh_target}:{destination}",
        ])
        subprocess.run(command, check=True, timeout=240)

    copy(PROJECT_ROOT / "o2o_dps", f"{project}/o2o_dps/", python_source_only=True)
    copy(PROJECT_ROOT / "configs", f"{project}/configs/")
    copy(SOURCE_CAPSULE, f"{project}/offline_data/derived/fury_offline_scenario_capsules/v2/")
    copy(SELECTOR_DIRECTORY, f"{project}/offline_data/derived/historical_representative_build_selector/v1/")
    copy(CATALOG_DIRECTORY, f"{project}/offline_data/derived/historical_build_catalog/v1/")
    copy(ITEM_DATABASE, f"{remote_root}/AddOns/BrainOfCat/wowsims-turtle/assets/database/db.json")
    remote_bridge = f"{project}/bin/{bridge.name}"
    copy(bridge, remote_bridge)
    rc, _, stderr = scheduler.run_on(
        staging_node, f"chmod u+x {remote_bridge}",
        timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote bridge executable bit failed: {stderr}")
    return {
        "schema": "factored_press_remote_stage/v1",
        "status": "STAGED_NO_JOB_LAUNCHED",
        "staging_node": staging_node,
        "run_id": run_id,
        "run_root": remote_root,
        "project_root": project,
        "bridge": remote_bridge,
        "shared_home_receipts": shared_home_receipts,
        "raw_chronicle_csv_staged": False,
        "checkpoint_staged": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-node", choices=NODE_NAMES, default="node001")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    args = parser.parse_args()
    import json

    print(json.dumps(stage_factored_press_v1(
        staging_node=args.staging_node, run_id=args.run_id, bridge=args.bridge,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
