"""Stage and run a small native two-wave Bloodrage panel; return summary only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys

from development_wave_remote_stage_v1 import stage_development_wave_v1


SCHEDULER_SKILL = Path.home() / "mine_code/scheduleurm/skill"


def inspect_remote_bloodrage(*, node: str, run_id: str, seed_start: int,
                             seed_count: int) -> dict:
    """Read only incomplete-row diagnostics from an already staged panel."""
    if node not in {f"node{i:03d}" for i in range(1, 7)} or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise ValueError("unknown staged panel site")
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]
    rc, home, stderr = scheduler.run_on(node, "cd; pwd", timeout=20, check=False)
    home = home.strip()
    if rc != 0:
        raise RuntimeError(f"node home discovery failed: {stderr}")
    project = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/development-wave-61944-v1/"
        f"{run_id}/AddOns/BrainOfCat/o2o-dps"
    )
    output = f"{project}/results/two-wave-bloodrage-{seed_start}-n{seed_count}.json"
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    code = f"""
import json, math, statistics
d=json.load(open({output!r},encoding='utf-8'))
incomplete=[{{
    'seed':r['seed'],'build_id':r['build_id'],'status':r['status'],
    'intervention_count':len(r['interventions']),
    'cat_branch_accepted':r['cat_branch_bloodrage_accepted'],
    'cat_bloodrage_time_ms':r['cat_bloodrage_time_ms'],
    'candidate_bloodrage_time_ms':r['candidate_bloodrage_time_ms'],
    'raw_damage_delta':r['candidate']['own_effective_damage']-r['cat']['own_effective_damage'],
    'cat':r['cat']['status'],'candidate':r['candidate']['status'],
    'cat_ttk':[t['death_time_ms'] for t in r['cat'].get('target_outcomes') or []],
    'candidate_ttk':[t['death_time_ms'] for t in r['candidate'].get('target_outcomes') or []],
}} for r in d['rows'] if r['status']!='COMPLETE_ACTION_COMPARISON']
metrics={{}}
for build in ('live_bonereaver','clean_dual_weapon_probe'):
    rows=[r for r in d['rows'] if r['build_id']==build and r['cat']['two_wave_timeline_valid'] and r['candidate']['two_wave_timeline_valid']]
    damage=[r['candidate']['own_effective_damage']-r['cat']['own_effective_damage'] for r in rows]
    dps=[r['candidate']['whole_two_wave_dps']-r['cat']['whole_two_wave_dps'] for r in rows]
    ttk=[r['candidate']['target_outcomes'][-1]['death_time_ms']-r['cat']['target_outcomes'][-1]['death_time_ms'] for r in rows]
    metrics[build]={{'terminal_complete_count':len(rows),'mean_damage_delta':statistics.mean(damage),
        'damage_standard_error':statistics.stdev(damage)/math.sqrt(len(damage)),
        'mean_whole_two_wave_dps_delta':statistics.mean(dps),
        'mean_final_ttk_delta_ms':statistics.mean(ttk)}}
print(json.dumps({{'incomplete_rows':incomplete,'all_terminal_summary':metrics}}))
"""
    rc, stdout, stderr = scheduler.run_on(
        node, " ".join(map(shlex.quote, [python, "-c", code])), timeout=30, check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote panel inspection failed: {stderr}")
    return {"remote_output": output, **json.loads(stdout.strip().splitlines()[-1])}


def run_remote_bloodrage(*, node: str, run_id: str, seed_start: int,
                        seed_count: int, workers: int) -> dict:
    if seed_start <= 0 or not 1 <= seed_count <= 64 or not 1 <= workers <= 16:
        raise ValueError("seed range or worker count is outside the small-panel scope")
    site = stage_development_wave_v1(node=node, run_id=run_id)
    sys.path.insert(0, str(SCHEDULER_SKILL))
    import scheduler  # type: ignore[import-not-found]

    project = site["project_root"]
    bridge = site["bridge"]
    home = project.split("/scheduleurm_work/")[0]
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    output = f"{project}/results/two-wave-bloodrage-{seed_start}-n{seed_count}.json"
    code = f"""
import concurrent.futures, json, math, statistics
from pathlib import Path
from o2o_dps.development_two_wave_bloodrage_v1 import run_two_wave_bloodrage_v1, BUILD_IDS

bridge = Path({bridge!r})
project = Path({project!r})
def run(item):
    build, seed = item
    return run_two_wave_bloodrage_v1(seed, build_id=build, bridge_path=bridge, bridge_cwd=project)

items = [(build, seed) for build in BUILD_IDS for seed in range({seed_start}, {seed_start + seed_count})]
with concurrent.futures.ThreadPoolExecutor(max_workers={workers}) as pool:
    rows = list(pool.map(run, items))
summary = {{}}
for build in BUILD_IDS:
    selected = [r for r in rows if r['build_id'] == build]
    complete = [r for r in selected if r['cat']['two_wave_timeline_valid'] and r['candidate']['two_wave_timeline_valid']]
    valid = [r for r in selected if r['status'] == 'COMPLETE_ACTION_COMPARISON']
    values = [r['paired_effective_damage_delta'] for r in complete if r['paired_effective_damage_delta'] is not None]
    summary[build] = {{
        'seed_count': len(selected), 'terminal_complete_count': len(complete),
        'actionable_count': len(valid),
        'no_op_count': sum(r['status'] == 'NO_OP_CAT_ALREADY_RESERVED' for r in selected),
        'wins': sum(v > 0 for v in values), 'ties': sum(v == 0 for v in values),
        'losses': sum(v < 0 for v in values),
        'mean_damage_delta': statistics.mean(values) if values else None,
        'standard_error': statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None,
        'mean_whole_two_wave_dps_delta': statistics.mean(r['paired_whole_two_wave_dps_delta'] for r in complete if r['paired_whole_two_wave_dps_delta'] is not None),
        'candidate_second_wave_bloodrage_accepted_count': sum(r['candidate_later_bloodrage_accepted'] for r in complete),
        'cat_mean_effective_damage': statistics.mean(r['cat']['own_effective_damage'] for r in complete) if complete else None,
        'candidate_mean_effective_damage': statistics.mean(r['candidate']['own_effective_damage'] for r in complete) if complete else None,
    }}
output = {{'schema': 'two_wave_bloodrage_remote_panel/v1', 'seed_start': {seed_start},
          'seed_count': {seed_count}, 'rows': rows, 'summary': summary}}
Path({output!r}).write_text(json.dumps(output, ensure_ascii=False), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
"""
    command = "cd " + shlex.quote(project) + " && " + " ".join(map(shlex.quote, [python, "-c", code]))
    rc, stdout, stderr = scheduler.run_on(
        node, command, timeout=max(180, seed_count * 30), check=False,
    )
    if rc != 0:
        raise RuntimeError(f"remote two-wave panel failed rc={rc}: {stderr[-2000:]} {stdout[-500:]}")
    return {
        "node": node, "run_id": run_id, "remote_output": output,
        "summary": json.loads(stdout.strip().splitlines()[-1]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node001")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, default=16)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args()
    if args.inspect_only:
        print(json.dumps(inspect_remote_bloodrage(
            node=args.node, run_id=args.run_id, seed_start=args.seed_start,
            seed_count=args.seed_count,
        ), ensure_ascii=False))
        return
    print(json.dumps(run_remote_bloodrage(
        node=args.node, run_id=args.run_id, seed_start=args.seed_start,
        seed_count=args.seed_count, workers=args.workers,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
