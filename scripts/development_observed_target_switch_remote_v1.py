"""Stage tiny target-control code and run the paired native batch on node001."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
from statistics import mean
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node001")
    parser.add_argument("--run-id", default="attempt-20260913-006")
    parser.add_argument("--seed-start", type=int, default=2026091501)
    parser.add_argument("--seed-count", type=int, default=32)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--strata", default="multi_two")
    args = parser.parse_args()
    if args.node not in {f"node{i:03d}" for i in range(1, 7)}:
        raise ValueError("unknown node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.run_id):
        raise ValueError("invalid remote run ID")
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]

    rc, home, stderr = scheduler.run_on(args.node, "cd; pwd", timeout=20, check=False)
    home = home.strip()
    if rc != 0 or not home.startswith("/home/"):
        raise RuntimeError(f"node home discovery failed: {stderr}")
    project = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/development-wave-61944-v1/"
        f"{args.run_id}/AddOns/BrainOfCat/o2o-dps"
    )
    bridge = f"{project}/bin/o2obridge.linux-amd64"
    suffix = (
        "multi-two" if args.strata == "multi_two" else
        "two-source" if args.strata == "two_short_q20,two_long_q80" else
        f"multi-{len(args.strata.split(','))}-strata"
    )
    output = f"{project}/results/observed-target-switch-{suffix}-{args.seed_start}-n{args.seed_count}.json"
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    rc, _, stderr = scheduler.run_on(
        args.node, f"test -f {shlex.quote(bridge)}", timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"staged bridge is absent: {stderr}")
    exists, _, _ = scheduler.run_on(
        args.node, f"test -f {shlex.quote(output)}", timeout=20, check=False,
    )
    if exists != 0:
        target = scheduler._ssh_target_for_node(args.node)
        shell = scheduler._ssh_rsync_shell_for_node(args.node)
        for local, remote in (
            (ROOT / "o2o_dps/development_observed_target_switch_v1.py", f"{project}/o2o_dps/"),
            (ROOT / "o2o_dps/development_two_wave_target_selection_v1.py", f"{project}/o2o_dps/"),
            (ROOT / "scripts/development_observed_target_switch_batch_v1.py", f"{project}/scripts/"),
        ):
            subprocess.run([
                "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
                "-e", shell, str(local), f"{target}:{remote}",
            ], check=True, timeout=120)
        command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, [
            python, "scripts/development_observed_target_switch_batch_v1.py",
            "--seed-start", str(args.seed_start), "--seed-count", str(args.seed_count),
            "--workers", str(args.workers), "--strata", args.strata,
            "--bridge", bridge, "--output", output,
        ]))
        rc, stdout, stderr = scheduler.run_on(
            args.node, command, timeout=max(600, args.seed_count * 90), check=False,
        )
        if rc != 0:
            raise RuntimeError(f"remote paired run failed rc={rc}: {stderr[-1600:]} {stdout[-800:]}")
    inspect = " ".join(map(shlex.quote, [python, "-c",
        "import json;d=json.load(open(" + repr(output) + ",encoding='utf-8'));print(json.dumps(d,ensure_ascii=False,separators=(',',':')))",
    ]))
    rc, stdout, stderr = scheduler.run_on(args.node, inspect, timeout=30, check=False)
    if rc != 0:
        raise RuntimeError(f"small remote terminal read failed: {stderr}")
    result = json.loads(stdout.strip().splitlines()[-1])
    source_summaries = result.get("summaries") or {"multi_two": result["summary"]}
    summaries = {}
    for stratum, source_summary in source_summaries.items():
        paired = [row for row in result["per_seed_terminal_rows"]
                  if row.get("stratum", "multi_two") == stratum
                  and row["paired_damage_delta"] is not None]
        summary = dict(source_summary)
        summary["paired_mean_ttk_delta_ms"] = mean(
            row["switch_plus_cat_ttk_ms"] - row["cat_ttk_ms"] for row in paired
        ) if paired else None
        summary["paired_mean_effective_dps_delta"] = mean(
            1000 * row["switch_plus_cat_effective_damage"] / row["switch_plus_cat_ttk_ms"]
            - 1000 * row["cat_effective_damage"] / row["cat_ttk_ms"]
            for row in paired
        ) if paired else None
        summaries[stratum] = summary
    terminal = [
        [row.get("stratum", "multi_two"), row["seed"], row["paired_damage_delta"]]
        for row in result["per_seed_terminal_rows"]
    ] if len(source_summaries) == 1 else None
    print(json.dumps({"node": args.node, "remote_output": output,
                      "summaries": summaries,
                      "per_seed_terminal_row_count": len(result["per_seed_terminal_rows"]),
                      **({"per_seed_terminal_projection": terminal} if terminal is not None else {})},
                     ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
