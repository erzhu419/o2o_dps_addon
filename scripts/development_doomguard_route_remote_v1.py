"""Run the tiny d900 route extractor on node004; fetch only its JSON result."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASE = "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc"
REMOTE = f"{BASE}/runtime-src-20260920"
STAGE5 = (
    f"{BASE}/offline_data/derived/chronicle_external_team_wave_model/v2/"
    "utk_postfix_dev_20260903_noon/"
    "d900a97b-b53e-4444-943b-3e0f2be8d477."
    "7b5a910c5b70011e5a9a9767a8c007abca7b4850dcc44fcf5032f7a3582e2058.jsonl.gz"
)
OUTPUT = ROOT / "results/responsive-team-v4/v38-d900-doomguard-observed-route-evidence.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
    ssh_target = scheduler._ssh_target_for_node(node)
    for relative in (
        "o2o_dps/upper_kara_observed_route_evidence_v1.py",
        "scripts/development_doomguard_route_evidence_v1.py",
    ):
        source = ROOT / relative
        target = f"{ssh_target}:{REMOTE}/{relative}"
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms", "-e", ssh_shell, str(source), target],
            check=True, timeout=120,
        )
    command = (
        f"cd {shlex.quote(REMOTE)} && "
        f"/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10 "
        f"scripts/development_doomguard_route_evidence_v1.py --stage5 {shlex.quote(STAGE5)}"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=240, check=False)
    if rc != 0:
        raise RuntimeError(f"route extraction failed (rc={rc}): {stderr}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "trace_rows": result["trace_rows"], "bins": len(result["damage_timeline"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
