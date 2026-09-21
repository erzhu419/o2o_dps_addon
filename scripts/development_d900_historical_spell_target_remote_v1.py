"""Aggregate one Stage5 wave on node004; fetch only the compact result."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

from development_d900_team_spill_remote_v1 import ROOT, REMOTE, STAGE5


OUTPUT = ROOT / "results/responsive-team-v4/v57-d900-historical-spell-target-through9098.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    relative = "scripts/development_d900_historical_spell_target_audit_v1.py"
    subprocess.run(
        ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
         "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
         f"{scheduler._ssh_target_for_node(node)}:{REMOTE}/{relative}"],
        check=True, timeout=120,
    )
    command = (
        f"cd {shlex.quote(REMOTE)} && "
        "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10 "
        f"{relative} --stage5 {shlex.quote(STAGE5)}"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=240, check=False)
    if rc:
        raise RuntimeError(f"historical spell target audit failed (rc={rc}): {stderr[-2000:]}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
                      "per_spell_groups": len(result["per_spell"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
