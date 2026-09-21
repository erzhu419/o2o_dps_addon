"""Run held-out white-target denominator on node004; fetch small JSON only."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

from development_d900_white_swing_remote_v1 import ROOT, REMOTE, STAGE5, COHORT


OUTPUT = ROOT / "results/responsive-team-v4/v58-d900-white-target-head-heldout-gate.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    relative = "scripts/development_d900_white_target_head_gate_v1.py"
    subprocess.run(
        ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
         "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
         f"{scheduler._ssh_target_for_node(node)}:{REMOTE}/{relative}"],
        check=True, timeout=120,
    )
    command = (
        f"cd {shlex.quote(REMOTE)} && "
        "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10 "
        f"{relative} --stage5 {shlex.quote(STAGE5)} "
        f"--cohort-json {shlex.quote(f'{REMOTE}/{COHORT}')}"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=240, check=False)
    if rc:
        raise RuntimeError(f"remote white-target gate failed (rc={rc}): {stderr[-2000:]}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
                      "cohort_wave_count": result["cohort_wave_count"],
                      "phases": result["phases"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
