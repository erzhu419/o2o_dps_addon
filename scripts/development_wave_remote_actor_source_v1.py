"""Small node001 actor-prefix probe; never transfers Chronicle raw CSV."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys


SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
ROOT = Path(__file__).resolve().parents[1]
NODE = "node001"
REMOTE_HOME = "/home/zhengliang01"
RUNS = f"{REMOTE_HOME}/scheduleurm_work/o2o-dps-hpc/runs/development-wave-61944-v1"
SOURCE_RUN = f"{RUNS}/attempt-20260913-006"
RUN_ID = "attempt-20260913-actor-001"
RUN_ROOT = f"{RUNS}/{RUN_ID}"
PROJECT = f"{RUN_ROOT}/AddOns/BrainOfCat/o2o-dps"
NODE_PYTHON = f"{REMOTE_HOME}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
DERIVED = ROOT / ".local/actor-schedule-multi-two-derived-v1.json"
LINUX_BRIDGE = ROOT / "bin/o2obridge.seedfix-v15.withdb.goamd64v1.linux-amd64"
REMOTE_DERIVED = f"{PROJECT}/.local/{DERIVED.name}"
CODE_FILES = (
    "o2o_dps/development_wave_actor_schedule_v1.py",
    "o2o_dps/development_wave_team_retarget_v1.py",
    "o2o_dps/development_wave_panel_v1.py",
    "o2o_dps/cat_fury_full_policy_rollout_v6.py",
)


def _scheduler():
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]
    return scheduler


def stage_actor_source() -> dict[str, object]:
    """Fork the tiny staged closure, then overlay only actor code and data."""

    if not DERIVED.is_file() or not LINUX_BRIDGE.is_file():
        raise FileNotFoundError("derived actor JSON or Linux v15 bridge is missing")
    scheduler = _scheduler()
    command = (
        f"test -d {shlex.quote(SOURCE_RUN)} && test ! -e {shlex.quote(RUN_ROOT)}"
        f" && mkdir -p {shlex.quote(RUN_ROOT)}"
        f" && rsync -a --exclude=results/ {shlex.quote(SOURCE_RUN)}/ {shlex.quote(RUN_ROOT)}/"
        f" && mkdir -p {shlex.quote(PROJECT)}/results {shlex.quote(PROJECT)}/.local"
    )
    rc, _, stderr = scheduler.run_on(NODE, command, timeout=120, check=False)
    if rc != 0:
        raise RuntimeError(f"fresh actor run stage failed: {stderr[-600:]}")
    ssh_shell = scheduler._ssh_rsync_shell_for_node(NODE)
    ssh_target = scheduler._ssh_target_for_node(NODE)
    files = [*CODE_FILES, str(DERIVED.relative_to(ROOT)), str(LINUX_BRIDGE.relative_to(ROOT))]
    subprocess.run(
        ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms", "--relative",
         "-e", ssh_shell, *files, f"{ssh_target}:{PROJECT}/"],
        cwd=ROOT, check=True, timeout=180,
    )
    remote_staged_bridge = f"{PROJECT}/{LINUX_BRIDGE.relative_to(ROOT).as_posix()}"
    rc, _, stderr = scheduler.run_on(
        NODE, f"chmod u+x {shlex.quote(remote_staged_bridge)}",
        timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"Linux bridge executable bit failed: {stderr[-600:]}")
    return {
        "node": NODE, "run_id": RUN_ID, "derived_bytes": DERIVED.stat().st_size,
        "binary_bytes": LINUX_BRIDGE.stat().st_size,
        "remote_bridge": remote_staged_bridge, "remote_derived": REMOTE_DERIVED,
        "status": "STAGED_NO_JOB_LAUNCHED",
    }


def sync_panel_module() -> dict[str, object]:
    """Send the default-path lazy-import repair without touching run data."""

    scheduler = _scheduler()
    panel = ROOT / "o2o_dps/development_wave_panel_v1.py"
    subprocess.run(
        ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
         "-e", scheduler._ssh_rsync_shell_for_node(NODE), str(panel),
         scheduler._ssh_target_for_node(NODE) + f":{PROJECT}/o2o_dps/{panel.name}"],
        check=True, timeout=120,
    )
    return {"node": NODE, "run_id": RUN_ID, "module": panel.name, "status": "SYNCED"}


def run_actor_source_8seed(seed_start: int) -> dict[str, object]:
    """Retain tiny server terminal rows and return their reduced summary."""

    sys.path.insert(0, str(ROOT))
    from o2o_dps.development_wave_actor_schedule_v1 import reduce_actor_terminal_rows_v1

    scheduler = _scheduler()
    bridge = f"{PROJECT}/{LINUX_BRIDGE.relative_to(ROOT).as_posix()}"
    terminal_path = f"{PROJECT}/results/actor-source-terminal-{seed_start}-n8.jsonl"
    code = f'''\
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from o2o_dps.development_wave_actor_schedule_v1 import run_actor_schedule_wave_panel_v1

def one(seed):
    panel = run_actor_schedule_wave_panel_v1(
        seed, bridge_path=Path({bridge!r}), bridge_cwd=Path({PROJECT!r}),
        runtime_binding_path=Path({PROJECT + "/runtime-binding.json"!r}),
        derived_json=Path({REMOTE_DERIVED!r}),
    )
    return [{{
        "seed": seed, "lane": ("CAT", "CANDIDATE")[index],
        "status": row["status"],
        "own_effective_damage": row.get("own_effective_damage"),
        "censor_reason": row.get("terminal_reason") if row["status"] != "COMPLETED" else None,
    }} for index, row in enumerate(panel["rows"])]

terminal_path = Path({terminal_path!r})
if terminal_path.exists():
    raise RuntimeError("terminal rows already exist; do not rerun the same seeds")
with ProcessPoolExecutor(max_workers=8) as pool:
    rows = [row for pair in pool.map(one, range({seed_start}, {seed_start + 8})) for row in pair]
with terminal_path.open("x", encoding="utf-8") as stream:
    for row in rows:
        stream.write(json.dumps(row, separators=(",", ":")) + "\\n")
print(json.dumps({{"remote_terminal_jsonl": str(terminal_path), "terminal_rows": rows}}, separators=(",", ":")))
'''
    command = "cd " + shlex.quote(PROJECT) + " && " + " ".join(
        map(shlex.quote, [NODE_PYTHON, "-c", code])
    )
    rc, stdout, stderr = scheduler.run_on(NODE, command, timeout=600, check=False)
    if rc != 0:
        raise RuntimeError(f"actor 8-seed probe failed: {stderr[-1000:]} {stdout[-500:]}")
    result = json.loads(stdout.strip().splitlines()[-1])
    summary = reduce_actor_terminal_rows_v1(result["terminal_rows"])
    summary["remote_terminal_jsonl"] = result["remote_terminal_jsonl"]
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--sync-panel", action="store_true")
    parser.add_argument("--run-8seed", action="store_true")
    parser.add_argument("--seed-start", type=int, default=20260913)
    args = parser.parse_args()
    if args.stage:
        print(json.dumps(stage_actor_source(), sort_keys=True))
    elif args.sync_panel:
        print(json.dumps(sync_panel_module(), sort_keys=True))
    elif args.run_8seed:
        print(json.dumps(run_actor_source_8seed(args.seed_start), sort_keys=True))


if __name__ == "__main__":
    main()
