"""Run one read-only node006 Stage5 pass; retain only the compact strata JSON."""

from __future__ import annotations

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
RELATIVE = "scripts/development_d900_v7_white6603_residual_strata_v1.py"
STORE = f"{BASE}/runtime-src-20260921-white6603-v7/results/formal-d-white6603-v7.sqlite3"
RESULT_SHA = "fd8130722cf3b1e69795a3d894757f821a79805950cffa16120e366a564ae6ea"
MODEL_SHA = "62407cf5e8ca19aa075088182ba117cde6e44977b918da46e144eee63b25503d"
OUTPUT = ROOT / "results/responsive-team-v4/v71-d900-v7-white6603-residual-strata-37waves.json"
V69 = ROOT / "results/responsive-team-v4/v69-d900-v7-white6603-conditional-37waves.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    for relative in (
        RELATIVE,
        "scripts/development_d900_v7_white6603_conditional_audit_v1.py",
        "scripts/development_d900_v7_initial_delay_calibration_audit_v1.py",
        "scripts/development_d900_v6_followup_plan_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(NODE), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(NODE)}:{EVAL}/{relative}"],
            check=True, timeout=120,
        )
    argv = [
        PYTHON, f"{EVAL}/{RELATIVE}", "--stage5", STAGE5,
        "--runtime-store", STORE, "--result-sha", RESULT_SHA, "--model-sha", MODEL_SHA,
    ]
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(EVAL)} " + shlex.join(argv)
    rc, stdout, stderr = scheduler.run_on(NODE, command, timeout=1200, check=False)
    if rc:
        raise RuntimeError(f"white residual strata failed (rc={rc}): {stderr[-3000:]}")
    result = json.loads(stdout)
    prior = json.loads(V69.read_text(encoding="utf-8"))
    expected = sum(w["full_window"]["expected_direct_white6603_mark_at_observed_opportunities"] for w in prior["waves"])
    observed = sum(w["full_window"]["observed_direct_white6603_emitted_raw"] for w in prior["waves"])
    if (result["wave_ids"] != prior["wave_ids"]
            or result["overall"]["observed_white6603"] != observed
            or abs(result["overall"]["expected_white6603"] - expected) > 1e-6):
        raise RuntimeError("residual strata denominator differs from frozen v69 cohort")
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "overall": result["overall"],
                      "actor_count": result["actor_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
