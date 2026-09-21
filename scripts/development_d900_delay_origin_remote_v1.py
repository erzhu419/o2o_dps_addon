"""Stage an isolated model revision on node004 and probe only first wakes."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASE = "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc"
ORIGINAL = f"{BASE}/runtime-src-20260920"
ISOLATED = f"{BASE}/runtime-src-20260920-delay-origin-v3"
STAGE5 = (
    f"{BASE}/offline_data/derived/chronicle_external_team_wave_model/v2/"
    "utk_postfix_dev_20260903_noon/"
    "d900a97b-b53e-4444-943b-3e0f2be8d477."
    "7b5a910c5b70011e5a9a9767a8c007abca7b4850dcc44fcf5032f7a3582e2058.jsonl.gz"
)
OLD = f"{ORIGINAL}/results/formal-d-v4-joint-v2-1389ce06.sqlite3"
NEW = f"{ISOLATED}/results/formal-d-v4-joint-v2-1389ce06-delay-origin-v3.sqlite3"
PYTHON = "/home/zhengliang01/scheduleurm_work/conda_envs/scomp-py310/bin/python3.10"
OUTPUT = ROOT / "results/responsive-team-v4/v48-d900-delay-origin-first-wake-diagnostic.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    target = scheduler._ssh_target_for_node(node)
    rc, _stdout, stderr = scheduler.run_on(
        node,
        f"mkdir -p {shlex.quote(ISOLATED)}/scripts {shlex.quote(ISOLATED)}/results && "
        f"if test ! -d {shlex.quote(ISOLATED)}/o2o_dps; then "
        f"cp -a {shlex.quote(ORIGINAL)}/o2o_dps {shlex.quote(ISOLATED)}/o2o_dps; fi",
        timeout=120,
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"cannot stage isolated source (rc={rc}): {stderr}")
    sources = (
        "o2o_dps/chronicle_external_teammate_response_model_v1.py",
        "o2o_dps/chronicle_external_teammate_response_hpc_v1.py",
        "o2o_dps/responsive_team_runtime_store_v1.py",
        "o2o_dps/responsive_team_delay_origin_store_derivation_v1.py",
        "scripts/development_d900_initial_delay_probe_v1.py",
    )
    for relative in sources:
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
             f"{target}:{ISOLATED}/{relative}"],
            check=True,
            timeout=120,
        )
    command = (
        f"cd {shlex.quote(ISOLATED)} && "
        f"{shlex.quote(PYTHON)} -m "
        "o2o_dps.responsive_team_delay_origin_store_derivation_v1 "
        f"{shlex.quote(OLD)} {shlex.quote(NEW)}"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=900, check=False)
    if rc != 0:
        raise RuntimeError(f"delay-head derivation failed (rc={rc}): {stderr}")
    derived = json.loads(stdout)
    command = (
        f"cd {shlex.quote(ISOLATED)} && {shlex.quote(PYTHON)} "
        "scripts/development_d900_initial_delay_probe_v1.py "
        f"--stage5 {shlex.quote(STAGE5)} --runtime-store {shlex.quote(NEW)} "
        f"--expected-model-sha {derived['model_content_sha256']} --seed-count 128"
    )
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=300, check=False)
    if rc != 0:
        raise RuntimeError(f"first-wake probe failed (rc={rc}): {stderr}")
    probe = json.loads(stdout)
    result = {
        "schema": "development_d900_delay_origin_remote_v1",
        "status": "DERIVED_STORE_FIRST_WAKE_ONLY_NO_FULL_WAVE_SIM",
        "source_store_remote_path": OLD,
        "derived_store_remote_path": NEW,
        "derived_model_content_sha256": derived["model_content_sha256"],
        "derivation": derived["derivation"],
        "probe": probe,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT),
        "remote_store": NEW,
        "model_sha": derived["model_content_sha256"],
        "derivation": derived["derivation"],
        "first_seed_lt_3s": probe["sampled_first_delay_before_3000_count"],
        "mean_128_seed_lt_3s": probe["before_3000_across_seeds"]["mean"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
