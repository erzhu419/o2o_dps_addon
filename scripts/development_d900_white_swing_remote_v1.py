"""Run the 37-wave 6603 coverage query remotely; fetch only compact JSON."""

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
COHORT = "results/responsive-team-v4/v51-d900-exact-target-head-denominator.json"
OUTPUT = ROOT / "results/responsive-team-v4/v53-d900-white-swing-first-target-coverage.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    for relative in (
        "o2o_dps/chronicle_external_teammate_response_model_v1.py",
        "scripts/development_d900_white_swing_acquisition_probe_v1.py",
        COHORT,
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(node)}:{REMOTE}/{relative}"],
            check=True, timeout=120,
        )
    command = (
        f"cd {shlex.quote(REMOTE)} && "
        "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10 "
        "scripts/development_d900_white_swing_acquisition_probe_v1.py "
        f"--stage5 {shlex.quote(STAGE5)} "
        f"--cohort-json {shlex.quote(f'{REMOTE}/{COHORT}')}"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=240, check=False)
    if rc != 0:
        raise RuntimeError(f"remote 6603 coverage query failed (rc={rc}): {stderr}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "cohort_wave_count": result["cohort_wave_count"],
                      "output_bytes": OUTPUT.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
