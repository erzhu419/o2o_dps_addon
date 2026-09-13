"""Read only compact failed-lane receipts from a staged twelve-wave panel."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shlex
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--stratum", default="multi_5_6_targets")
    parser.add_argument("--live-closure", action="store_true")
    args = parser.parse_args()
    if args.node not in {f"node{i:03d}" for i in range(1, 7)}:
        parser.error("unknown node")
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value)
               for value in (args.run_id, args.tag, args.stratum)):
        parser.error("invalid run ID, tag, or stratum")
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler  # type: ignore[import-not-found]

    rc, home, stderr = scheduler.run_on(args.node, "cd; pwd", timeout=20, check=False)
    home = home.strip()
    if rc or not home.startswith("/home/") or "\n" in home:
        raise RuntimeError(f"node home discovery failed: {stderr}")
    project = (
        f"{home}/scheduleurm_work/o2o-dps-hpc/runs/development-wave-61944-v1/"
        f"{args.run_id}/AddOns/BrainOfCat/o2o-dps"
    )
    output = (
        f"{project}/results/stratified-twelve-{args.seed_start}-"
        f"{args.seed_count}-{args.tag}.json"
    )
    code = (
        "import collections,json\n"
        "d=json.load(open(" + repr(output) + ",encoding='utf-8'))\n"
        "rows=[(p['seed'],next(r for r in p['rows'] if r['stratum']==" + repr(args.stratum) + ")) "
        "for p in d['panels']]\n"
        "counts=collections.Counter((lane.get('policy_id'),lane.get('status'),"
        "str(lane.get('first_bridge_failure'))[:160]) "
        "for _,row in rows for lane in row['policies'])\n"
        "examples=[{'seed':seed,'row_status':row['status'],'policies':"
        "[{k:lane.get(k) for k in ('policy_id','status','error','terminal_reason',"
        "'required_targets_dead','execution_blockers','first_bridge_failure')} "
        "for lane in row['policies']]} for seed,row in rows[:2]]\n"
        "print(json.dumps({'stratum':" + repr(args.stratum) + ","
        "'seed_count':len(rows),'lane_status_counts':[(list(k),v) for k,v in counts.items()],"
        "'examples':examples},ensure_ascii=False))"
    )
    if args.live_closure:
        code = (
            "import json\n"
            "from o2o_dps.development_wave_twelve_v1 import build_twelve_wave_case_v1\n"
            "from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4\n"
            "from o2o_dps.cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6\n"
            "from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3\n"
            "case,_=build_twelve_wave_case_v1(" + str(args.seed_start) + "," + repr(args.stratum) + ")\n"
            "with SimulatorBridgeDynamicV3(" + repr(project + "/bin/o2obridge.linux-amd64") +
            ",cwd=" + repr(project) + ") as bridge:\n"
            "    result=run_cat_fury_full_policy_rollout_v6(bridge,case.request,CatFuryFullPolicyAdapterV4(),"
            "seed=case.dynamic_load.seed,target_contexts=case.target_contexts,dynamic_load=case.dynamic_load)\n"
            "closure=result['dynamic_v3_runtime_receipt_closure']\n"
            "print(json.dumps({'status':closure['status'],"
            "'checks':closure['cursor_and_lifecycle_checks'],"
            "'terminal_reason':result.get('configured_completion',{}).get('terminal_reason'),"
            "'background_events':len(closure['background_damage']['receipts']),"
            "'candidate_events':len(closure['candidate_damage']['receipts']),"
            "'background_sum':sum(r['applied_damage'] for r in closure['background_damage']['receipts']),"
            "'candidate_sum':sum(r['applied_damage'] for r in closure['candidate_damage']['receipts']),"
            "'background_cancel_count':sum(r['status']!='APPLIED' for r in closure['background_damage']['receipts']),"
            "'candidate_cancel_count':sum(r['status'].startswith('CANCELED_') for r in closure['candidate_damage']['receipts']),"
            "'lifecycle':closure['terminal_lifecycle']},ensure_ascii=False))"
        )
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    command = "cd " + shlex.quote(project) + " && " + " ".join(
        map(shlex.quote, [python, "-c", code])
    )
    rc, stdout, stderr = scheduler.run_on(args.node, command, timeout=60, check=False)
    if rc:
        raise RuntimeError(f"remote receipt projection failed: {stderr}")
    print(stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    main()
