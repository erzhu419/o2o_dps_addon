"""Run one read-only v7 delay calibration scan on node006; fetch small JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, PYTHON, STAGE5

NODE = "node006"
EVAL = f"{BASE}/evals/white6603-actor-deadline-seed1-v1"
RELATIVE = "scripts/development_d900_v7_initial_delay_calibration_audit_v1.py"
OUTPUT = ROOT / "results/responsive-team-v4/v67-d900-v7-initial-delay-context-score.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-store", required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    for relative in (RELATIVE, "scripts/development_d900_v6_followup_plan_v1.py"):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(NODE), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(NODE)}:{EVAL}/{relative}"],
            check=True, timeout=120,
        )
    command = (
        f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(EVAL)} "
        + shlex.join([
            PYTHON, f"{EVAL}/{RELATIVE}", "--stage5", STAGE5,
            "--runtime-store", args.runtime_store,
            "--result-sha", args.result_sha,
            "--model-sha", args.model_sha,
        ])
    )
    rc, stdout, stderr = scheduler.run_on(NODE, command, timeout=600, check=False)
    if rc:
        raise RuntimeError(f"v7 delay calibration scan failed (rc={rc}): {stderr[-3000:]}")
    result = json.loads(stdout)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT),
        "cohort_wave_count": result["heldout_multi_start_cohort"]["wave_count"],
        "cohort_actor_wave_count": result["heldout_multi_start_cohort"]["actor_wave_count_with_exact_event"],
        "model_global_support": result["model_global_wave_start_delay"]["support"],
        "d900_early_white_actors": result["d900"]["early_direct_white_actor_count"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
