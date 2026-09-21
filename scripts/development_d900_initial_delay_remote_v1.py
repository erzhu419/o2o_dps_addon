"""Run the read-only initial teammate-delay probe on node004."""

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
STORE = f"{REMOTE}/results/formal-d-v4-joint-v2-1389ce06.sqlite3"
OUTPUT = ROOT / "results/responsive-team-v4/v47-d900-model-initial-delay-diagnostic.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    relative = "scripts/development_d900_initial_delay_probe_v1.py"
    subprocess.run(
        ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
         "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
         f"{scheduler._ssh_target_for_node(node)}:{REMOTE}/{relative}"],
        check=True, timeout=120,
    )
    command = (
        f"cd {shlex.quote(REMOTE)} && "
        "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10 "
        f"{relative} --stage5 {shlex.quote(STAGE5)} --runtime-store {shlex.quote(STORE)}"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=240, check=False)
    if rc != 0:
        raise RuntimeError(f"initial-delay probe failed (rc={rc}): {stderr}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "context_levels": result["context_level_counts"], "first_lt_3s": result["sampled_first_delay_before_3000_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
