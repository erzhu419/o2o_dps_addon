"""Stage and run the small d900 D2 four-controller panel on node004."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.development_d900_v6_followup_plan_v1 import BASE, build_plan_v1


NODE = "node004"
RUNTIME = f"{BASE}/runtime-src-20260921-white6603-v7"
STATIC_RUNTIME = f"{BASE}/runtime-src-20260921-causal-target-v6"
DISPATCH = (
    f"{BASE}/runs/teammate-response-current/"
    "single_scan_direct_white6603_target_choice_v7/"
    "53a85ece0dbe56b95d072a47226ce6d2b5a73e540e1e9f350c1fe1cc04010c02/"
    "attempt1/dispatch/dispatch.json"
)
RESULT_SHA = "fd8130722cf3b1e69795a3d894757f821a79805950cffa16120e366a564ae6ea"
MODEL_SHA = "62407cf5e8ca19aa075088182ba117cde6e44977b918da46e144eee63b25503d"
OUTPUT = ROOT / "results/offline-wave-policy-v1/d900-d2-four-controller-seed3.json"

# The v7 runtime already contains the frozen simulator/model closure.  Transfer
# only the controller/panel files needed to evaluate the current local change.
SOURCE_FILES = (
    "configs/experts/contra260817_source_manifest_91baa120.json",
    "o2o_dps/responsive_action_program_replay_v1.py",
    "o2o_dps/offline_wave_policy_v1.py",
    "o2o_dps/offline_team_wave_policy_v1.py",
    "o2o_dps/offline_wave_d2_panel_v1.py",
    "scripts/development_responsive_upper_kara_trash_smoke_v1.py",
    "scripts/development_responsive_upper_kara_trash_full_wave_v1.py",
    "scripts/development_offline_wave_policy_d900_v1.py",
    "scripts/development_offline_wave_policy_d900_d2_v1.py",
)


def _d2_argv() -> list[str]:
    plan = build_plan_v1(
        code_root=RUNTIME,
        simulator_root=STATIC_RUNTIME,
        frozen_dispatch=DISPATCH,
        runtime_store=f"{RUNTIME}/results/formal-d-white6603-v7.sqlite3",
        result_sha=RESULT_SHA,
        model_sha=MODEL_SHA,
        exact_build=(
            f"{STATIC_RUNTIME}/results/responsive-team-v4/"
            "v34-doomguard-exact-fury-build-request.json"
        ),
        deployed_binding=(
            f"{STATIC_RUNTIME}/results/responsive-team-v4/"
            "deployed-contra-runtime-binding-v1.951b8faa.json"
        ),
        bridge=(
            f"{STATIC_RUNTIME}/bin/"
            "o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64"
        ),
    )
    argv = list(plan["full_wave"]["argv"])
    argv[1] = f"{RUNTIME}/scripts/development_offline_wave_policy_d900_d2_v1.py"
    source_index = argv.index("--source")
    del argv[source_index : source_index + 2]
    if "--route-focus" in argv:
        argv.remove("--route-focus")
    argv[argv.index("--seed-count") + 1] = "3"
    argv.extend(("--lane-workers", "4"))
    return argv


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    mkdir_command = shlex.join(
        [
            "mkdir",
            "-p",
            f"{RUNTIME}/configs/experts",
            f"{BASE}/Contra_new",
        ]
    )
    mkdir_rc, _mkdir_stdout, mkdir_stderr = scheduler.run_on(
        NODE, mkdir_command, timeout=30, check=False
    )
    if mkdir_rc:
        raise RuntimeError(
            f"could not prepare D2 source directories: {mkdir_stderr[-2000:]}"
        )
    for relative in SOURCE_FILES:
        subprocess.run(
            [
                "rsync",
                "--archive",
                "--no-owner",
                "--no-group",
                "--no-perms",
                "-e",
                scheduler._ssh_rsync_shell_for_node(NODE),
                str(ROOT / relative),
                f"{scheduler._ssh_target_for_node(NODE)}:{RUNTIME}/{relative}",
            ],
            check=True,
            timeout=120,
        )
    # Contra_new verifies its pinned readable source tree when each fresh
    # reactive session opens.  Transfer only the 4.3 MB code-manifest surface,
    # not bundled fonts/textures or offline raid data.
    subprocess.run(
        [
            "rsync",
            "--archive",
            "--no-owner",
            "--no-group",
            "--no-perms",
            "--include=*/",
            "--include=*.lua",
            "--include=*.toc",
            "--include=*.xml",
            "--exclude=*",
            "-e",
            scheduler._ssh_rsync_shell_for_node(NODE),
            str(ROOT.parent / "Contra_new") + "/",
            f"{scheduler._ssh_target_for_node(NODE)}:{BASE}/Contra_new/",
        ],
        check=True,
        timeout=120,
    )
    command = (
        f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} "
        + shlex.join(_d2_argv())
    )
    return_code, stdout, stderr = scheduler.run_on(
        NODE,
        command,
        timeout=1800,
        check=False,
    )
    if return_code:
        raise RuntimeError(
            f"d900 D2 controller panel failed (rc={return_code}): {stderr[-3000:]}"
        )
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "bytes": OUTPUT.stat().st_size,
                "status": result["status"],
                "paired_seed_count": result["paired_seed_count"],
                "valid_paired_seed_count": result["valid_paired_seed_count"],
                "lane_status_counts": result["lane_status_counts"],
                "aggregate_on_valid_pairs_only": result[
                    "aggregate_on_valid_pairs_only"
                ],
                "parallel_lane_workers": result["parallel_lane_workers"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
