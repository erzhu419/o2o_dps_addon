"""One-seed native smoke of an observable-HP target control plus Cat actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from o2o_dps.cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from o2o_dps.development_observed_target_switch_v1 import ObservedTargetSwitchBridgeV1
from o2o_dps.development_wave_case_v1 import adjudicate_development_wave_completion_v1
from o2o_dps.development_wave_stratified_v1 import build_stratified_wave_case_v1
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--bridge", type=Path, default=ROOT / "bin/o2obridge.seedfix-v11.dynamicv3horizonround.withdb.goamd64v1.windows-amd64.exe")
    args = parser.parse_args()
    case, _ = build_stratified_wave_case_v1(args.seed, "multi_two")
    common = dict(seed=args.seed, target_contexts=case.target_contexts,
                  dynamic_load=case.dynamic_load)
    with SimulatorBridgeDynamicV3(args.bridge) as raw:
        base = run_cat_fury_full_policy_rollout_v6(
            raw, case.request, CatFuryFullPolicyAdapterV4(), **common,
        )
        selected = ObservedTargetSwitchBridgeV1(raw)
        trial = run_cat_fury_full_policy_rollout_v6(
            selected, case.request, CatFuryFullPolicyAdapterV4(), **common,
        )
        raw.load_dynamic_v3(case.request, args.seed, case.dynamic_load.config)
        queue = next(row for row in raw.actions()
                     if row.action.tag == 1 and row.action.spell_id in {11567, 25286})
        queued = raw.act(queue.action)
        before_switch = queued.state
        switched = raw.set_target(1)
        after_switch = switched.state
        raw.wait(500)
        after_queue_delay = raw.advance()
        queue_probe = {
            "queue_casted": queued.casted,
            "queue_consumes_decision": queued.consumes_decision,
            "switch_changed": switched.changed,
            "switch_target_index": after_switch["target_index"],
            "queue_aura_before": [a for a in before_switch["auras"]
                                  if a["action"].get("tag") == 1],
            "queue_aura_after": [a for a in after_switch["auras"]
                                 if a["action"].get("tag") == 1],
            "queue_aura_after_delay": [a for a in after_queue_delay["auras"]
                                       if a["action"].get("tag") == 1],
            "target_after_delay": after_queue_delay["target_index"],
            "mainhand_swing_remaining_after_delay_ms": after_queue_delay["mh_swing_remaining_ms"],
            "swing_timer_preserved": before_switch["mh_swing_remaining_ms"] == after_switch["mh_swing_remaining_ms"],
            "decision_preserved": before_switch["needs_input"] == after_switch["needs_input"],
        }
    base_verdict = adjudicate_development_wave_completion_v1(case, base)
    trial_verdict = adjudicate_development_wave_completion_v1(case, trial)
    def dense_reentry(artifact: dict) -> dict:
        steps = [step for step in artifact.get("steps") or []
                 if 6400 <= step["simulator_state_before"]["time_ms"] <= 7900]
        return {
            "decision_count": len(steps),
            "target_indices": [step["simulator_state_before"]["target_index"] for step in steps],
            "gcd_kinds": [step["proposal"]["gcd"]["action"] for step in steps],
            "queue_kinds": [step["proposal"]["swing_queue"] for step in steps],
        }
    print(json.dumps({
        "scope": "MODEL_DEFINED_DEVELOPMENT_SMOKE_ONLY",
        "seed": args.seed,
        "target_hp": [row.health for row in case.dynamic_load.config.target_health],
        "cat_status": base.get("status"),
        "cat_terminal": base_verdict["status"],
        "cat_own_effective_damage": base_verdict.get("own_effective_damage"),
        "cat_ttk_ms": base.get("final_state", {}).get("time_ms"),
        "cat_decisions": len(base.get("steps") or []),
        "switch_plus_cat_status": trial.get("status"),
        "switch_plus_cat_terminal": trial_verdict["status"],
        "switch_plus_cat_own_effective_damage": trial_verdict.get("own_effective_damage"),
        "switch_plus_cat_ttk_ms": trial.get("final_state", {}).get("time_ms"),
        "switch_plus_cat_decisions": len(trial.get("steps") or []),
        "cat_decision_times_ms": [step["simulator_state_before"]["time_ms"] for step in base.get("steps") or []],
        "switch_plus_cat_decision_times_ms": [step["simulator_state_before"]["time_ms"] for step in trial.get("steps") or []],
        "cat_6400_7900_reentry": dense_reentry(base),
        "switch_plus_cat_6400_7900_reentry": dense_reentry(trial),
        "cat_target_death_times_ms": [row.get("death_time_ms") for row in base["final_state"]["dynamic_team_background"]["targets"]],
        "switch_plus_cat_target_death_times_ms": [row.get("death_time_ms") for row in trial["final_state"]["dynamic_team_background"]["targets"]],
        "first_cat_decision_target": (trial.get("steps") or [{}])[0].get("simulator_state_before", {}).get("target_index"),
        "switch_receipts": selected.target_switch_receipts,
        "queue_probe": queue_probe,
    }, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
