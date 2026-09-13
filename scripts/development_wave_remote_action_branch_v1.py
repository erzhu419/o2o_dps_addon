"""Run Cat-visited ActionPlan teacher branches on staged node001; pull summary only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"
MODULE = ROOT / "o2o_dps/cat_action_branch_search_v1.py"


def run_remote_action_branch(*, node: str, run_id: str, stratum: str,
                             seed_start: int, seed_count: int, max_states: int,
                             workers: int, summary_only: bool = False,
                             artifact_version: str = "v2") -> dict:
    if node not in {f"node{i:03d}" for i in range(1, 7)}:
        raise ValueError("unknown node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    if stratum not in {"controlled", "single_short", "single_medium", "single_long", "multi_two"}:
        raise ValueError("unknown source stratum")
    if artifact_version not in {"v1", "v2"}:
        raise ValueError("unknown artifact version")
    if seed_start <= 0 or seed_count <= 0 or max_states <= 0 or workers <= 0:
        raise ValueError("seed range, max states, and workers must be positive")
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]

    rc, home, stderr = scheduler.run_on(node, "cd; pwd", timeout=20, check=False)
    home = home.strip()
    if rc != 0 or not home.startswith("/home/") or "\n" in home:
        raise RuntimeError(f"node home discovery failed: {stderr}")
    project = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/"
        f"development-wave-61944-v1/{run_id}/AddOns/BrainOfCat/o2o-dps"
    )
    bridge = f"{project}/bin/o2obridge.linux-amd64"
    rc, _, stderr = scheduler.run_on(node, f"test -f {shlex.quote(bridge)}", timeout=20, check=False)
    if rc != 0:
        raise RuntimeError(f"staged native bridge is absent: {stderr}")
    output = (
        f"{project}/results/cat-action-branch-{stratum}-"
        f"{seed_start}-n{seed_count}-s{max_states}-{artifact_version}.json"
    )
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    if not summary_only:
        ssh_shell = scheduler._ssh_rsync_shell_for_node(node)
        target = scheduler._ssh_target_for_node(node)
        subprocess.run([
            "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
            "-e", ssh_shell, str(MODULE), f"{target}:{project}/o2o_dps/{MODULE.name}",
        ], check=True, timeout=120)
        argv = [
            python, "-m", "o2o_dps.cat_action_branch_search_v1",
            "--stratum", stratum, "--seed", str(seed_start),
            "--seed-count", str(seed_count), "--max-states", str(max_states),
            "--workers", str(workers),
            "--bridge", bridge, "--bridge-cwd", project, "--output", output,
        ]
        command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, argv))
        rc, stdout, stderr = scheduler.run_on(
            node, f"test -f {shlex.quote(output)} || ({command})",
            timeout=max(1200, seed_count * 120), check=False,
        )
        if rc != 0:
            raise RuntimeError(f"remote ActionPlan teacher failed rc={rc}: {stderr[-1600:]} {stdout[-800:]}")
    summary_script = (
        "import json, math, statistics\n"
        "d=json.load(open(" + repr(output) + ",encoding='utf-8'))\n"
        "summary={k:v for k,v in d.items() if k!='runs'}\n"
        "groups={}\n"
        "for run in d.get('runs', [d]):\n"
        "    seen=set()\n"
        "    for branch in run.get('branches', []):\n"
        "        kind=branch['kind']\n"
        "        if kind in seen or branch.get('status')!='COMPLETE_BRANCH_SMOKE':\n"
        "            continue\n"
        "        seen.add(kind)\n"
        "        groups.setdefault(kind, []).append(branch['paired_effective_damage_delta'])\n"
        "summary['first_opportunity_by_kind']={kind:{\n"
        "    'seed_count':len(values), 'wins':sum(v>0 for v in values),\n"
        "    'ties':sum(v==0 for v in values), 'losses':sum(v<0 for v in values),\n"
        "    'mean_delta':statistics.mean(values),\n"
        "    'standard_error':(statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else None)\n"
        "} for kind,values in sorted(groups.items())}\n"
        "def accepted(branch):\n"
        "    receipt=branch.get('branch_action_receipt') or {}\n"
        "    kind=branch['kind']\n"
        "    if kind in ('ADD_HS_QUEUE','BT_TO_WW','WW_TO_BT'):\n"
        "        rows=receipt.get('candidate') or []\n"
        "        return any((r.get('source_sink') or {}).get('source_ref','').startswith('cat_action_branch_candidate/v1') and (r.get('simulator_acceptance') or {}).get('status')=='ACCEPTED' for r in rows)\n"
        "    rows=receipt.get('cat') or []\n"
        "    channel='swing_queue' if kind=='SUPPRESS_QUEUE' else 'gcd'\n"
        "    return any((r.get('source_sink') or {}).get('channel')==channel and (r.get('simulator_acceptance') or {}).get('status')=='ACCEPTED' for r in rows)\n"
        "summary['accepted_action_branch_count']=sum(accepted(b) for run in d.get('runs',[d]) for b in run.get('branches',[]))\n"
        "print(json.dumps(summary,ensure_ascii=False))"
    )
    inspect = " ".join(map(shlex.quote, [python, "-c", summary_script]))
    rc, stdout, stderr = scheduler.run_on(node, inspect, timeout=30, check=False)
    if rc != 0:
        raise RuntimeError(f"remote summary read failed: {stderr}")
    return {"node": node, "run_id": run_id, "remote_output": output,
            **json.loads(stdout.strip().splitlines()[-1])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stratum", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--max-states", type=int, default=6)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--artifact-version", choices=("v1", "v2"), default="v2")
    args = parser.parse_args()
    print(json.dumps(run_remote_action_branch(
        node=args.node, run_id=args.run_id, stratum=args.stratum,
        seed_start=args.seed_start, seed_count=args.seed_count,
        max_states=args.max_states, workers=args.workers,
        summary_only=args.summary_only, artifact_version=args.artifact_version,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
