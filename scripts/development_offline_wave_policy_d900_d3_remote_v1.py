"""Stage and run the bounded d900 D3 search smoke on node004."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.development_offline_wave_policy_d900_d2_remote_v1 import (
    BASE,
    NODE,
    RUNTIME,
    _d2_argv,
)


OUTPUT = ROOT / "results/offline-wave-policy-v1/d900-d3-tail-repair-smoke-v1.json"
SOURCE_FILES = (
    "configs/experts/contra260817_source_manifest_91baa120.json",
    "o2o_dps/causal_action_program_v1.py",
    "o2o_dps/causal_guard_v1.py",
    "o2o_dps/offline_wave_d2_panel_v1.py",
    "o2o_dps/offline_wave_policy_v1.py",
    "o2o_dps/offline_wave_searched_program_v1.py",
    "o2o_dps/offline_wave_searched_runtime_v1.py",
    "o2o_dps/offline_wave_execution_trace_v1.py",
    "o2o_dps/offline_wave_d3_train_eval_v1.py",
    "o2o_dps/offline_wave_d3_candidate_panel_v1.py",
    "o2o_dps/responsive_action_program_replay_v1.py",
    "scripts/development_offline_wave_policy_d900_v1.py",
    "scripts/development_offline_wave_policy_d900_d3_v1.py",
)


def _drop_option(argv: list[str], option: str) -> None:
    if option not in argv:
        return
    index = argv.index(option)
    del argv[index : index + 2]


def _d3_argv() -> list[str]:
    argv = _d2_argv()
    argv[1] = f"{RUNTIME}/scripts/development_offline_wave_policy_d900_d3_v1.py"
    _drop_option(argv, "--seed-count")
    _drop_option(argv, "--lane-workers")
    _drop_option(argv, "--simulator-seed")
    _drop_option(argv, "--teammate-seed")
    _drop_option(argv, "--max-decisions")
    argv.extend(
        (
            "--proposal-budget",
            "4",
            "--proposal-seed-count",
            "1",
            "--selection-seed-count",
            "1",
            "--selection-seed",
            "2026092202",
            "--heldout-seed-count",
            "1",
            "--heldout-seed",
            "2026092301",
            "--max-decisions",
            "300",
            "--lane-workers",
            "8",
            "--development-screened-selection-seed",
        )
    )
    return argv


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    mkdir_command = shlex.join(
        [
            "mkdir",
            "-p",
            f"{RUNTIME}/configs/experts",
            f"{RUNTIME}/o2o_dps",
            f"{RUNTIME}/scripts",
        ]
    )
    return_code, _stdout, stderr = scheduler.run_on(
        NODE, mkdir_command, timeout=30, check=False
    )
    if return_code:
        raise RuntimeError(f"could not prepare D3 runtime: {stderr[-2000:]}")
    for relative in SOURCE_FILES:
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        subprocess.run(
            [
                "rsync",
                "--archive",
                "--no-owner",
                "--no-group",
                "--no-perms",
                "-e",
                scheduler._ssh_rsync_shell_for_node(NODE),
                str(source),
                f"{scheduler._ssh_target_for_node(NODE)}:{RUNTIME}/{relative}",
            ],
            check=True,
            timeout=120,
        )
    command = (
        f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} "
        + shlex.join(_d3_argv())
    )
    return_code, stdout, stderr = scheduler.run_on(
        NODE,
        command,
        timeout=3600,
        check=False,
    )
    if return_code:
        raise RuntimeError(
            f"d900 D3 bounded smoke failed (rc={return_code}): {stderr[-5000:]}"
        )
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    frozen = result.get("selection_receipt", {}).get("frozen_candidate")
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "bytes": OUTPUT.stat().st_size,
                "status": result.get("status"),
                "candidate_count": len(result.get("candidate_manifests", [])),
                "frozen_candidate_id": (
                    frozen.get("candidate_id")
                    if isinstance(frozen, dict)
                    else None
                ),
                "heldout_status": (
                    result.get("heldout_receipt") or {}
                ).get("status"),
                "paired_valid_pair_count": result.get(
                    "paired_valid_pair_count"
                ),
                "paired_aggregate_valid_pairs_only": result.get(
                    "paired_aggregate_valid_pairs_only"
                ),
                "parallel_lane_workers": result.get("parallel_lane_workers"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
