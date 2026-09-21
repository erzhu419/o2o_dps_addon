"""Read-only v6 D-store heldout target score with both player denominators."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

from development_d900_v6_followup_plan_v1 import BASE, STAGE5, PYTHON


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = f"{BASE}/runtime-src-20260921-causal-target-v6"
RESULT_SHA = "8fc29addf548011ab72471758ae4fe96444633fca8df6146e1c31d7f0764f125"
MODEL_SHA = "a82aa8858dc8b2e7ad453ed5494cc39476d122a4c4eeb23149a8a93fb034fe09"
OUTPUT = ROOT / "results/responsive-team-v4/v59-d900-v6-formal-d-dual-player-head-heldout.json"
OLD_SUMMARY = ROOT / "results/responsive-team-v4/v54-d900-v6-formal-d-target-head-heldout-summary.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    for relative in (
        "o2o_dps/upper_kara_target_head_heldout_eval_v1.py",
        "scripts/development_d900_target_head_model_eval_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(node)}:{RUNTIME}/{relative}"],
            check=True, timeout=120,
        )
    argv = [
        PYTHON, f"{RUNTIME}/scripts/development_d900_target_head_model_eval_v1.py",
        "--stage5", STAGE5,
        "--runtime-store", f"{RUNTIME}/results/formal-d-causal-target-v6.sqlite3",
        "--result-sha", RESULT_SHA, "--model-sha", MODEL_SHA,
    ]
    rc, stdout, stderr = scheduler.run_on(
        node, f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} "
        + shlex.join(argv), timeout=360, check=False,
    )
    if rc:
        raise RuntimeError(f"v6 dual-player heldout score failed (rc={rc}): {stderr[-2000:]}")
    result = json.loads(stdout)
    old = json.loads(OLD_SUMMARY.read_text(encoding="utf-8"))
    for phase, old_key in (("FIRST_ACQUISITION", "first_acquisition"), ("RETARGET", "retarget")):
        new = result["cohort"]["metrics"][phase]
        previous = old["cohort"][old_key]
        for key in ("eligible_choices", "uniform_expected_accuracy", "learned_expected_accuracy", "learned_top1_accuracy"):
            if abs(new[key] - previous[key]) > 1e-9:
                raise RuntimeError(f"frozen v54 all-player START metric changed: {phase}.{key}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    concise = {}
    for view in ("cohort", "cohort_teammates_only", "selected_d900", "selected_d900_teammates_only"):
        concise[view] = {
            phase: {
                key: result[view]["metrics"][phase][key]
                for key in ("eligible_choices", "uniform_expected_accuracy", "learned_expected_accuracy", "learned_top1_accuracy")
            } for phase in ("FIRST_ACQUISITION", "RETARGET")
        }
    print(json.dumps({"output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
                      "v54_all_player_start_exact_match": True,
                      "start_metrics": concise}, ensure_ascii=False))


if __name__ == "__main__":
    main()
