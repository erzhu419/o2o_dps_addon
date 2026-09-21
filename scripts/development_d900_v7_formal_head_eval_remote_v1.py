"""Score a completed v7 formal D-store on node004; never run full-wave sim."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, STAGE5, PYTHON

RUNTIME = f"{BASE}/runtime-src-20260921-white6603-v7"
OUTPUT = ROOT / "results/responsive-team-v4/v61-d900-v7-formal-d-dual-head-heldout.json"
EXPECTED = {
    "metrics": {"FIRST_ACQUISITION": 475, "RETARGET": 830},
    "white_metrics": {"FIRST_ACQUISITION": 247, "RETARGET": 425},
}


def check_result(result: dict, *, result_sha: str, model_sha: str) -> None:
    binding = result["new_reduced_model_binding"]
    if (
        result["schema"] != "upper_kara_target_head_heldout_eval/v2"
        or result["heldout_instance_id"] != "d900a97b-b53e-4444-943b-3e0f2be8d477"
        or result["cohort_wave_count"] != 37
        or binding["source_result_content_sha256"] != result_sha
        or binding["model_content_sha256"] != model_sha
        or binding["heldout_instance_excluded_by_runtime_store_binding"] is not True
    ):
        raise RuntimeError("v7 formal heldout identity differs")
    view = result["cohort_teammates_only"]
    if view["white_head_status"] != "STRICT_PREFIX_CHOICES_AVAILABLE":
        raise RuntimeError("v7 white prefix candidate rows unavailable")
    for intent, phases in EXPECTED.items():
        for phase, denominator in phases.items():
            if view[intent][phase]["eligible_choices"] != denominator:
                raise RuntimeError(f"v7 teammate {intent}.{phase} denominator differs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-store", required=True, help="node004 absolute path to completed formal v7 store")
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
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
        PYTHON, "-B", f"{RUNTIME}/scripts/development_d900_target_head_model_eval_v1.py",
        "--stage5", STAGE5, "--runtime-store", args.runtime_store,
        "--result-sha", args.result_sha, "--model-sha", args.model_sha,
    ]
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} " + shlex.join(argv)
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=900, check=False)
    if rc:
        raise RuntimeError(f"formal v7 heldout score failed (rc={rc}): {stderr[-2500:]}")
    result = json.loads(stdout)
    check_result(result, result_sha=args.result_sha, model_sha=args.model_sha)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
                      "model_binding_verified": True,
                      "teammate_denominators": EXPECTED}, ensure_ascii=False))


if __name__ == "__main__":
    main()
