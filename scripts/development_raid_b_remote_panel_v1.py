"""Run a small, development-only Raid-B four-lane panel from WSL on one CPU node.

The full per-seed panels stay remote. Standard output contains only terminal
rows and paired aggregates; no Chronicle CSV or checkpoint is transferred.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

from development_wave_remote_stage_v1 import stage_development_wave_v1


SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
REMOTE_BASE = "scheduleurm_work/o2o-dps-hpc/runs/development-wave-61944-v1"
OUTPUT_NAME = "raid-b-fourway-proxy-2026091801-n32.json"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPSULE = PROJECT_ROOT / (
    "offline_data/derived/fury_offline_scenario_capsules/v2/"
    "fury_offline_scenario_capsules_v2.23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz"
)


def scheduler():
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler as module  # type: ignore[import-not-found]
    return module


def inventory(node: str) -> dict:
    remote = scheduler()
    rc, home, stderr = remote.run_on(node, 'printf %s "$HOME"', timeout=20, check=False)
    if rc != 0 or not home.strip().startswith("/home/"):
        raise RuntimeError(f"remote home discovery failed: {stderr}")
    base = f"{home.strip()}/{REMOTE_BASE}"
    command = "cd " + shlex.quote(home.strip()) + " && " + " ".join(map(shlex.quote, [
        "find", base, "-type", "f", "-name", OUTPUT_NAME, "-print",
    ]))
    rc, stdout, stderr = remote.run_on(node, command, timeout=30, check=False)
    if rc != 0 and "No such file or directory" not in stderr:
        raise RuntimeError(f"remote result inventory failed: {stderr}")
    rc, processes, stderr = remote.run_on(
        node, "ps -eo pid,args", timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote process inventory failed: {stderr}")
    active = [line.strip() for line in processes.splitlines()
              if "raid-b-fourway-proxy-2026091801" in line
              and "ps -eo pid,args" not in line]
    rc, commands, stderr = remote.run_on(
        node, "ps -eo rss,comm", timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote bridge process inventory failed: {stderr}")
    bridge_rss_kb = [int(parts[0]) for line in commands.splitlines()
                     if (parts := line.split()) and len(parts) == 2
                     and parts[0].isdigit() and "o2obridge" in parts[1]]
    rc, memory, stderr = remote.run_on(node, "free -m", timeout=20, check=False)
    if rc != 0:
        raise RuntimeError(f"remote memory inventory failed: {stderr}")
    memory_line = next((line.strip() for line in memory.splitlines()
                        if line.strip().startswith("Mem:")), None)
    revisions = []
    for output_path in stdout.splitlines():
        source = str(Path(output_path).parents[1] /
                     "o2o_dps/contra260817_fury_ordered_sink_executor_v4.py")
        rc, lines, stderr = remote.run_on(
            node, " ".join(map(shlex.quote, ["grep", "IMPLEMENTATION_REVISION", source])),
            timeout=20, check=False,
        )
        if rc != 0:
            raise RuntimeError(f"remote source revision read failed: {stderr}")
        rc, stamps, stderr = remote.run_on(
            node, " ".join(map(shlex.quote, [
                "stat", "-c", "%W", str(Path(output_path).parents[4]), output_path,
            ])), timeout=20, check=False,
        )
        if rc != 0:
            raise RuntimeError(f"remote run duration read failed: {stderr}")
        first, last = map(int, stamps.splitlines())
        revisions.append({"output": output_path, "revision_line": lines.strip().splitlines()[0],
                          "stage_to_output_seconds": last - first if first > 0 and last > 0 else None})
    return {"node": node, "base": base,
            "existing_outputs": stdout.splitlines(),
            "matching_processes": [line[:140] for line in active],
            "active_bridge_processes": len(bridge_rss_kb),
            "bridge_rss_mib": round(sum(bridge_rss_kb) / 1024),
            "memory_line_mib": memory_line,
            "source_revisions": revisions}


def run(node: str, run_id: str, workers: int) -> dict:
    before = inventory(node)
    if before["existing_outputs"] or before["matching_processes"]:
        return {"status": "ALREADY_PRESENT_OR_RUNNING", "inventory": before}
    if not 1 <= workers <= 12:
        raise ValueError("workers must be 1..12 for this diagnostic panel")
    site = stage_development_wave_v1(node=node, run_id=run_id)
    remote = scheduler()
    project = site["project_root"]
    bridge = site["bridge"]
    remote_capsule = f"{project}/{CAPSULE.relative_to(PROJECT_ROOT).as_posix()}"
    rc, _, stderr = remote.run_on(
        node, "mkdir -p " + shlex.quote(str(Path(remote_capsule).parent)),
        timeout=20, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote capsule directory creation failed: {stderr}")
    subprocess.run([
        "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
        "-e", remote._ssh_rsync_shell_for_node(node), str(CAPSULE),
        remote._ssh_target_for_node(node) + ":" + remote_capsule,
    ], check=True, timeout=90)
    home = project.split("/scheduleurm_work/")[0]
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    output = f"{project}/results/{OUTPUT_NAME}"
    code = f'''
import concurrent.futures, json, math, statistics
from pathlib import Path
from o2o_dps.development_raid_b_wave_v1 import run_raid_b_four_policy_wave_v1

def one(seed):
    return run_raid_b_four_policy_wave_v1(
        seed, "multi_2_targets", bridge_path=Path({bridge!r}),
        bridge_cwd=Path({project!r}),
        runtime_binding_path=Path({site["runtime_binding"]!r}))

smoke = one(2026091401)
smoke_timing = [(r["policy_id"], r.get("decision_opportunities", {{}}).get("status"))
                for r in smoke["rows"]]
if not smoke["four_way_complete"] or any(status != "OBSERVED_SIMULATOR_INVOCATIONS"
                                          for _, status in smoke_timing):
    raise RuntimeError(f"remote four-lane timing smoke failed: {{smoke_timing}}")

seeds = list(range(2026091801, 2026091833))
with concurrent.futures.ThreadPoolExecutor(max_workers={workers}) as pool:
    panels = list(pool.map(one, seeds))
Path({output!r}).write_text(json.dumps({{"schema":"raid_b_fourway_proxy_panel/v1",
    "seeds":seeds,"panels":panels}}, ensure_ascii=False), encoding="utf-8")
terminal = []
for seed, panel in zip(seeds, panels):
    terminal.append({{"seed":seed,"status":panel["status"],
        "four_way_complete":panel["four_way_complete"],
        "comparison_ready":panel["comparison_ready"],
        "lanes":[{{"policy_id":r["policy_id"],"role":r["role"],"status":r["status"],
            "terminal_status":r.get("terminal_status"),
            "damage":r.get("own_effective_damage"),
            "ttk_ms":r.get("ttk_ms"),"error":r.get("error"),
            "decision_opportunities":{{key:r.get("decision_opportunities",{{}}).get(key)
                for key in ("status","invocation_count","same_millisecond_reentry_count",
                    "positive_sub_100ms_interval_count","positive_interval_median_ms")}}
            }} for r in panel["rows"]]}})
policies = [r["policy_id"] for r in panels[0]["rows"]]
candidate = next(r["policy_id"] for r in panels[0]["rows"] if r["role"] == "CANDIDATE")
paired = {{}}
for baseline in policies:
    if baseline == candidate:
        continue
    values = []
    for panel in panels:
        by_id = {{r["policy_id"]:r for r in panel["rows"]}}
        a, b = by_id[candidate], by_id[baseline]
        if a["status"] == b["status"] == "COMPLETED":
            values.append(a["own_effective_damage"] - b["own_effective_damage"])
    paired[baseline] = {{"n":len(values),
        "mean":statistics.mean(values) if values else None,
        "se":statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else None,
        "wins":sum(x>0 for x in values),"ties":sum(x==0 for x in values),
        "losses":sum(x<0 for x in values)}}
print(json.dumps({{"schema":"raid_b_fourway_proxy_summary/v1",
    "remote_output":{output!r},"seed_count":len(seeds),
    "four_way_complete_count":sum(x["four_way_complete"] for x in terminal),
    "comparison_ready_count":sum(x["comparison_ready"] for x in terminal),
    "unsupported_count":sum(r["status"]=="UNSUPPORTED" for x in terminal for r in x["lanes"]),
    "paired_candidate_minus_baseline":paired,"terminal_rows":terminal}}, ensure_ascii=False))
'''
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(
        shlex.quote, [python, "-c", code]))
    rc, stdout, stderr = remote.run_on(node, command, timeout=2400, check=False)
    if rc != 0:
        raise RuntimeError(f"remote four-lane panel failed rc={rc}: {stderr[-1800:]} {stdout[-500:]}")
    return {"status": "FINISHED", "node": node, "run_id": run_id,
            **json.loads(stdout.strip().splitlines()[-1])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node001")
    parser.add_argument("--run-id", default="attempt-20260913-raidb-fourway-proxy-002")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--inventory", action="store_true")
    args = parser.parse_args()
    if args.node not in {f"node{i:03d}" for i in range(1, 7)} or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.run_id):
        parser.error("invalid node or run ID")
    result = inventory(args.node) if args.inventory else run(args.node, args.run_id, args.workers)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
