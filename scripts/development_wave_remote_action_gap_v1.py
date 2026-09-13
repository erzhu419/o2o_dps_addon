"""Replay changed residual seeds on node001 and return only action-gap counts."""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys

from development_wave_remote_gap_probe_v1 import PROJECT


SEEDS = (
    20260913, 20260914, 20260915, 20260918, 20260924, 20260932,
    20260933, 20260934, 20260935, 20260936, 20260937, 20260938,
    20260939, 20260943,
)


def main() -> None:
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler  # type: ignore[import-not-found]

    source = Path(__file__).resolve().parents[1] / "o2o_dps/development_wave_action_gap_diagnostic_v1.py"
    target = PROJECT + "/o2o_dps/development_wave_action_gap_diagnostic_v1.py"
    subprocess.run([
        "rsync", "--archive", "--no-owner", "--no-group", "--no-perms",
        "-e", scheduler._ssh_rsync_shell_for_node("node001"),
        str(source), scheduler._ssh_target_for_node("node001") + ":" + target,
    ], check=True, timeout=180)

    code = '''
from collections import Counter
import json
from pathlib import Path
from o2o_dps.development_wave_action_gap_diagnostic_v1 import run_selected_seed_v1
seeds = SEEDS_HERE
root = Path("results/diagnostics/residual-d15-action-gap-20260913-v1")
root.mkdir(parents=True, exist_ok=False)
cat = "cat.fury.profile1"
cand = "cat_residual_candidate/v1"
rows = []
for seed in seeds:
    result = run_selected_seed_v1(
        seed, 15.0, bridge_path=Path("bin/o2obridge.linux-amd64"),
        bridge_cwd=Path("."), runtime_binding_path=Path("runtime-binding.json"),
    )
    existing = json.loads((Path("results/panels-residual")/f"seed-{seed}"/"discount-15.json").read_text())
    expected = {row["policy_id"]:row["own_effective_damage"] for row in existing["rows"]}
    for policy_id in (cat,cand):
        if abs(expected[policy_id]-result["paired_effective_damage"][policy_id]) > 1e-7:
            raise RuntimeError(f"seed {seed} two-lane replay differs from four-way panel: {policy_id}")
    (root/f"seed-{seed}.json").write_text(json.dumps(result,ensure_ascii=False))
    counts = {policy_id:result["traces"][policy_id]["accepted_counts"] for policy_id in (cat,cand)}
    typed = {policy_id:result["traces"][policy_id]["typed_result_counts"] for policy_id in (cat,cand)}
    def action_delta(name):
        return counts[cand].get(name,0)-counts[cat].get(name,0)
    rows.append({
        "seed":seed,
        "damage_delta":result["paired_effective_damage"][cand]-result["paired_effective_damage"][cat],
        "execute_delta":action_delta("gcd:warrior.execute"),
        "slam_delta":action_delta("gcd:warrior.slam"),
        "bloodthirst_delta":action_delta("gcd:warrior.bloodthirst"),
        "whirlwind_delta":action_delta("gcd:warrior.whirlwind"),
        "queue_delta":action_delta("swing_queue:QueueSpellByName"),
        "slam_cancel_delta":typed[cand].get("45961:CANCELED",0)-typed[cat].get("45961:CANCELED",0),
        "intervention_decisions":len(result["traces"][cand]["interventions"]),
    })
summary = {
    "schema":"development_wave_residual_d15_action_gap/v1",
    "replay_score_verified_against_existing_four_way_panels":True,
    "n":len(rows),
    "positive":sum(r["damage_delta"]>0 for r in rows),
    "negative":sum(r["damage_delta"]<0 for r in rows),
    "frequencies":{key:{"positive":sum(r[key]>0 for r in rows),"negative":sum(r[key]<0 for r in rows),"zero":sum(r[key]==0 for r in rows)} for key in ("execute_delta","slam_delta","bloodthirst_delta","whirlwind_delta","queue_delta","slam_cancel_delta")},
    "rows":rows,
    "remote_trace_dir":str(root),
}
(root/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False))
'''.replace("SEEDS_HERE", repr(SEEDS))
    python = "/home/zhengliang01/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    command = "cd " + shlex.quote(PROJECT) + " && " + python + " -c " + shlex.quote(code)
    rc, stdout, stderr = scheduler.run_on("node001", command, timeout=900, check=False)
    if rc:
        raise RuntimeError(f"remote diagnostic failed: {stderr[-1500:]} {stdout[-1500:]}")
    print(stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    main()
