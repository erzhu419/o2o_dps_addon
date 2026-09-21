"""Node006 d900 smoke then 37-wave read-only conditional white calibration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, PYTHON, STAGE5, WAVE

NODE = "node006"
EVAL = f"{BASE}/evals/white6603-actor-deadline-seed1-v1"
RELATIVE = "scripts/development_d900_v7_white6603_conditional_audit_v1.py"
SMOKE_OUTPUT = ROOT / "results/responsive-team-v4/v68-d900-v7-white6603-conditional-smoke.json"
FULL_OUTPUT = ROOT / "results/responsive-team-v4/v69-d900-v7-white6603-conditional-37waves.json"
V61 = ROOT / "results/responsive-team-v4/v61-d900-v7-formal-d-dual-head-heldout.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-store", required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    for relative in (
        RELATIVE,
        "scripts/development_d900_v7_initial_delay_calibration_audit_v1.py",
        "scripts/development_d900_v6_followup_plan_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(NODE), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(NODE)}:{EVAL}/{relative}"],
            check=True, timeout=120,
        )
    base_argv = [
        PYTHON, f"{EVAL}/{RELATIVE}", "--stage5", STAGE5,
        "--runtime-store", args.runtime_store,
        "--result-sha", args.result_sha,
        "--model-sha", args.model_sha,
    ]
    results = []
    for only_wave in (WAVE, None):
        argv = base_argv + (["--only-wave", only_wave] if only_wave else [])
        command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(EVAL)} " + shlex.join(argv)
        rc, stdout, stderr = scheduler.run_on(NODE, command, timeout=1200, check=False)
        if rc:
            raise RuntimeError(f"conditional white audit failed (rc={rc}): {stderr[-3000:]}")
        result = json.loads(stdout)
        results.append(result)
        if only_wave:
            early = result["waves"][0]["through_9098ms"]
            if (
                result["wave_ids"] != [WAVE]
                or early["observed_direct_white6603_emitted_raw"] != 71
                or early["observed_direct_white6603_positive_raw"] != 61
                or early["observed_actor_count_with_white6603"] != 13
            ):
                raise RuntimeError("d900 raw white smoke denominator differs from v65 historical audit")
            SMOKE_OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            reference = json.loads(V61.read_text(encoding="utf-8"))
            if result["wave_ids"] != reference["cohort_wave_ids"]:
                raise RuntimeError("37-wave cohort differs from frozen v61 heldout evaluator")
            FULL_OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    smoke, full = results
    print(json.dumps({
        "smoke_output": str(SMOKE_OUTPUT), "full_output": str(FULL_OUTPUT),
        "smoke_early_historical_white": smoke["waves"][0]["through_9098ms"]["observed_direct_white6603_emitted_raw"],
        "smoke_early_expected_white": smoke["waves"][0]["through_9098ms"]["expected_direct_white6603_mark_at_observed_opportunities"],
        "full_wave_count": full["wave_count"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
