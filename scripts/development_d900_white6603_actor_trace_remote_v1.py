"""Run one isolated Cat replay and compare early 6603 actor timing to d900."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.development_d900_v6_followup_plan_v1 import BASE, STAGE5, PYTHON, build_plan_v1
from scripts.development_d900_v7_formal_cat_diagnostic_remote_v1 import (
    DISPATCH, STATIC_RUNTIME, RUNTIME,
)

NODE = "node006"
EVAL = f"{BASE}/evals/white6603-actor-deadline-seed1-v1"
OUTPUT = ROOT / "results/responsive-team-v4/v65-d900-v7-white6603-actor-deadline-seed1.json"


def _actor_rows(trace: list[dict], historical: dict) -> list[dict]:
    simulated: dict[str, list[dict]] = defaultdict(list)
    for row in trace:
        simulated[row["actor_guid"]].append(row)
    observed = {row["actor_guid"]: row for row in historical["per_actor"]}
    rows = []
    for actor in sorted(set(simulated) | set(observed)):
        sim_rows = sorted(simulated[actor], key=lambda row: (row["time_ms"], row["sequence"]))
        sim_times = [row["time_ms"] for row in sim_rows]
        hist = observed.get(actor, {})
        hist_times = hist.get("all_6603_times_ms", [])
        rows.append({
            "actor_guid": actor,
            "historical_emitted_count": len(hist_times),
            "historical_first_ms": hist_times[0] if hist_times else None,
            "historical_times_ms": hist_times,
            "historical_intervals_ms": [b - a for a, b in zip(hist_times, hist_times[1:])],
            "simulated_emitted_count": len(sim_times),
            "simulated_first_ms": sim_times[0] if sim_times else None,
            "simulated_times_ms": sim_times,
            "simulated_intervals_ms": [b - a for a, b in zip(sim_times, sim_times[1:])],
            "simulated_positive_applied_count": sum(
                row["status"] == "APPLIED" and row["applied_damage"] > 0
                for row in sim_rows
            ),
            "simulated_zero_or_canceled_count": sum(
                row["status"] != "APPLIED" or row["applied_damage"] <= 0
                for row in sim_rows
            ),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-store", required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    # A separate small code copy keeps the frozen v6/v7 releases and formal
    # stores untouched. The trained store is only opened read-only by replay.
    rc, stdout, stderr = scheduler.run_on(
        NODE, f"if test -e {shlex.quote(EVAL)}; then echo EXISTS; else echo MISSING; fi",
        timeout=30, check=False,
    )
    if rc or stdout.strip() != "MISSING":
        raise RuntimeError(f"isolated eval path not fresh: {stdout.strip()} {stderr.strip()}")
    scheduler.run_on(NODE, f"mkdir -p {shlex.quote(EVAL)}", timeout=30)
    for folder in ("o2o_dps", "scripts"):
        scheduler.run_on(
            NODE,
            f"cp -a {shlex.quote(f'{RUNTIME}/{folder}')} {shlex.quote(f'{EVAL}/{folder}')}",
            timeout=120,
        )
    for relative in (
        "o2o_dps/responsive_white6603_actor_trace_v1.py",
        "scripts/development_d900_white6603_actor_trace_runner_v1.py",
        "scripts/development_d900_white6603_timing_audit_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(NODE), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(NODE)}:{EVAL}/{relative}"],
            check=True, timeout=120,
        )
    plan = build_plan_v1(
        code_root=EVAL, simulator_root=STATIC_RUNTIME,
        frozen_dispatch=DISPATCH, runtime_store=args.runtime_store,
        result_sha=args.result_sha, model_sha=args.model_sha,
        exact_build=f"{STATIC_RUNTIME}/results/responsive-team-v4/v34-doomguard-exact-fury-build-request.json",
        deployed_binding=f"{STATIC_RUNTIME}/results/responsive-team-v4/deployed-contra-runtime-binding-v1.951b8faa.json",
        bridge=f"{STATIC_RUNTIME}/bin/o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64",
    )
    argv = list(plan["full_wave"]["argv"])
    argv[1] = f"{EVAL}/scripts/development_d900_white6603_actor_trace_runner_v1.py"
    argv[argv.index("--source") + 1] = "cat"
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(EVAL)} " + shlex.join(argv)
    rc, stdout, stderr = scheduler.run_on(NODE, command, timeout=1800, check=False)
    if rc:
        raise RuntimeError(f"isolated Cat trace failed (rc={rc}): {stderr[-2500:]}")
    replay = json.loads(stdout)
    if replay["seed"] != 2026092001 or replay["teammate_seed"] != 2026092002:
        raise RuntimeError("trace replay did not use frozen seed pair")
    historical_command = shlex.join([
        PYTHON, f"{EVAL}/scripts/development_d900_white6603_timing_audit_v1.py",
        "--stage5", STAGE5,
    ])
    rc, stdout, stderr = scheduler.run_on(
        NODE, f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(EVAL)} {historical_command}",
        timeout=300, check=False,
    )
    if rc:
        raise RuntimeError(f"historical one-wave timing scan failed (rc={rc}): {stderr[-2500:]}")
    historical = json.loads(stdout)
    trace = replay["early_direct_white6603_actor_trace"]["events"]
    if historical["wave_id"] != replay["wave_id"] or historical["all_6603_event_count"] != 71:
        raise RuntimeError("historical actor trace does not match audited d900 wave")
    result = {
        "schema": "development_d900_white6603_actor_timing_comparison/v1",
        "scope": "one held-out wave, one Cat seed; timing diagnosis only",
        "formal_result_sha": args.result_sha,
        "formal_model_sha": args.model_sha,
        "replay": replay,
        "historical_direct_white6603_event_count": historical["all_6603_event_count"],
        "historical_positive_three_target_count": historical["positive_three_target_count"],
        "historical_nonpositive_three_target_count": historical["nonpositive_three_target_count"],
        "historical_focus_actor_roster": historical["focus_actor_roster"],
        "actor_rows": _actor_rows(trace, historical),
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT), "replay_status": replay["status"],
        "historical_emitted": historical["all_6603_event_count"],
        "simulated_emitted": len(trace),
        "simulated_positive_applied": sum(
            row["status"] == "APPLIED" and row["applied_damage"] > 0 for row in trace
        ),
        "simulated_zero_or_canceled": sum(
            row["status"] != "APPLIED" or row["applied_damage"] <= 0 for row in trace
        ),
        "historical_actors": historical["direct_teammate_actor_count"],
        "simulated_actors": len({row["actor_guid"] for row in trace}),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
