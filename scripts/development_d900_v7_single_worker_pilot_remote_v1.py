"""Node004 single TRAIN worker heldout probe; retrieve only small JSON."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

from development_d900_v6_followup_plan_v1 import BASE, STAGE5, PYTHON


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = f"{BASE}/runtime-src-20260921-white6603-v7"
RUN = (
    f"{BASE}/runs/teammate-response-current/single_scan_direct_white6603_target_choice_v7/"
    "53a85ece0dbe56b95d072a47226ce6d2b5a73e540e1e9f350c1fe1cc04010c02/attempt1"
)
WORKER = f"{RUN}/outputs/workers/9a61ace6-0292-4a97-bafa-76cead47cf38.json.gz"
DISPATCH = f"{RUN}/dispatch/dispatch.json"
OUTPUT = ROOT / "results/responsive-team-v4/v60-d900-v7-single-train-worker-white-head-probe.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    for relative in (
        "o2o_dps/upper_kara_target_head_heldout_eval_v1.py",
        "scripts/development_d900_v7_single_worker_pilot_eval_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(node)}:{RUNTIME}/{relative}"],
            check=True, timeout=120,
        )
    argv = [
        PYTHON, "-B", f"{RUNTIME}/scripts/development_d900_v7_single_worker_pilot_eval_v1.py",
        "--worker", WORKER, "--dispatch", DISPATCH, "--stage5", STAGE5,
    ]
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} " + shlex.join(argv)
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=900, check=False)
    if rc:
        raise RuntimeError(f"single-worker heldout probe failed (rc={rc}): {stderr[-2500:]}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    white = result["views"]["cohort_teammates_only"]["white_6603"]
    print(json.dumps({"output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
                      "status": result["status"], "worker": result["pilot_worker"],
                      "teammate_white": white}, ensure_ascii=False))


if __name__ == "__main__":
    main()
