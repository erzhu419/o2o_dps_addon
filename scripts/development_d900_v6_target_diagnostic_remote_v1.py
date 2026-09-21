"""Run one Cat-only d900 diagnostic replay on node004; fetch small JSON only."""

from __future__ import annotations

import json
import math
from pathlib import Path
import shlex
import subprocess
import sys

from development_d900_v6_followup_plan_v1 import BASE, build_plan_v1


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = f"{BASE}/runtime-src-20260921-causal-target-v6"
DISPATCH = (
    f"{BASE}/runs/teammate-response-current/single_scan_causal_target_choice_v6/"
    "7484e06074ef5a7a02a2bf6e06dcbca252777081040f75bfb2142616321c211a/"
    "attempt1/dispatch/dispatch.json"
)
RESULT_SHA = "8fc29addf548011ab72471758ae4fe96444633fca8df6146e1c31d7f0764f125"
MODEL_SHA = "a82aa8858dc8b2e7ad453ed5494cc39476d122a4c4eeb23149a8a93fb034fe09"
OUTPUT = ROOT / "results/responsive-team-v4/v56-d900-v6-cat-target-diagnostic-seed1.json"
BASELINE = ROOT / "results/responsive-team-v4/v55-d900-v6-formal-d-full-wave-seed1.json"


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler

    node = "node004"
    for relative in (
        "o2o_dps/responsive_incantagos_driven_bridge_v1.py",
        "o2o_dps/responsive_action_program_replay_v1.py",
    ):
        subprocess.run(
            ["rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
             "-e", scheduler._ssh_rsync_shell_for_node(node), str(ROOT / relative),
             f"{scheduler._ssh_target_for_node(node)}:{RUNTIME}/{relative}"],
            check=True, timeout=120,
        )
    plan = build_plan_v1(
        code_root=RUNTIME,
        simulator_root=RUNTIME,
        frozen_dispatch=DISPATCH,
        runtime_store=f"{RUNTIME}/results/formal-d-causal-target-v6.sqlite3",
        result_sha=RESULT_SHA,
        model_sha=MODEL_SHA,
        bridge=(f"{RUNTIME}/bin/"
                "o2obridge.seedfix-v26.observed-damage-v2.withdb.goamd64v1.linux-amd64"),
    )
    argv = list(plan["full_wave"]["argv"])
    argv[argv.index("--source") + 1] = "cat"
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(RUNTIME)} " + shlex.join(argv)
    rc, stdout, stderr = scheduler.run_on(node, command, timeout=1200, check=False)
    if rc:
        raise RuntimeError(f"d900 Cat diagnostic replay failed (rc={rc}): {stderr[-2000:]}")
    result = json.loads(stdout)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = result["paired_replays"]
    if len(rows) != 1 or rows[0]["source_policy_id"] != "cat.fury.profile1":
        raise RuntimeError("diagnostic replay did not return exactly one Cat source")
    new = rows[0]
    old = next(row for row in json.loads(BASELINE.read_text(encoding="utf-8"))["paired_replays"]
               if row["source_policy_id"] == "cat.fury.profile1")
    frozen_fields = (
        "status", "effective_damage", "elapsed_ms", "target_timeline_snapshots",
        "first_observed_dead_ms", "route_focus_receipts",
    )
    mismatches = [key for key in frozen_fields if new[key] != old[key]]
    diagnostic = new["responsive_drive"]["target_event_diagnostic"]
    group_count = sum(row["event_count"] for row in diagnostic["groups"])
    group_damage = sum(row["applied_damage"] for row in diagnostic["groups"])
    if group_count != new["responsive_drive"]["responsive_event_count"] or not math.isclose(
        group_damage, new["responsive_drive"]["responsive_applied_damage"], abs_tol=1e-6
    ):
        raise RuntimeError("target diagnostic event/damage aggregates do not conserve")
    print(json.dumps({
        "output": str(OUTPUT), "output_bytes": OUTPUT.stat().st_size,
        "frozen_replay_mismatches": mismatches,
        "effective_damage": new["effective_damage"], "elapsed_ms": new["elapsed_ms"],
        "target_event_group_count": len(diagnostic["groups"]),
        "first_white_6603_actor_count": diagnostic["first_white_6603_actor_count"],
        "direct_start_head_counts": diagnostic["direct_start_head_counts"],
    }, ensure_ascii=False))
    if mismatches:
        raise RuntimeError("diagnostic replay changed frozen Cat trajectory: " + ", ".join(mismatches))


if __name__ == "__main__":
    main()
