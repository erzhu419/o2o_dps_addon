"""Pull only compact, observable action-branch rows from a staged remote run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


COMBAT_FIELDS = (
    "rage", "target_health_pct", "nearby_enemies", "bloodthirst_ready_in_s",
    "whirlwind_ready_in_s", "mainhand_swing_remaining_s",
    "mainhand_swing_duration_s", "flurry_active", "queued_swing",
    "weapon_mode", "current_stance", "heroic_strike_cost", "whirlwind_cost",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node001")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--remote-result", required=True,
                        help="basename under the staged results directory")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.node not in {f"node{i:03d}" for i in range(1, 7)}:
        parser.error("unknown node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", args.run_id):
        parser.error("invalid run ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.json", args.remote_result):
        parser.error("remote result must be a JSON basename")
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
    remote_result = f"{project}/results/{args.remote_result}"
    code = (
        "import json\n"
        f"d=json.load(open({remote_result!r},encoding='utf-8'))\n"
        f"fields={COMBAT_FIELDS!r}\n"
        "for run in d['runs']:\n"
        " for branch in run['branches']:\n"
        "  observation=branch.get('policy_observation',{}).get('observation',{})\n"
        "  combat=observation.get('combat',{})\n"
        "  row={'seed':run['seed'],'source_wave_ref':run['source_wave_ref'],"
        "'baseline_status':run['baseline_terminal']['status'],"
        "'status':branch['status'],'kind':branch['kind'],"
        "'decision_index':branch['decision_index'],"
        "'branch_action_accepted':branch.get('branch_action_accepted'),"
        "'branch_state_changed_after_commands':branch.get('branch_state_changed_after_commands'),"
        "'paired_effective_damage_delta':branch.get('paired_effective_damage_delta'),"
        "'combat':{key:combat.get(key) for key in fields},"
        "'combat_elapsed_s':observation.get('combat_elapsed_s')}\n"
        "  print(json.dumps(row,ensure_ascii=False,separators=(',',':')))\n"
    )
    python = f"{home}/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    command = "cd " + shlex.quote(project) + " && " + " ".join(
        map(shlex.quote, [python, "-c", code])
    )
    rc, stdout, stderr = scheduler.run_on(args.node, command, timeout=60, check=False)
    if rc:
        raise RuntimeError(f"remote branch projection failed: {stderr}")
    rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if not rows:
        raise RuntimeError("remote branch projection returned no rows")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                  for row in rows) + "\n", encoding="utf-8",
    )
    print(json.dumps({"rows": len(rows), "bytes": args.output.stat().st_size,
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
