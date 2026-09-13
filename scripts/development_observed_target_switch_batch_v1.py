"""Parallel native paired wave: Cat default target vs observed-HP control + Cat.

The output deliberately keeps only small terminal rows, not full rollouts.
Target control is a separate development component, not part of Cat source.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import math
from pathlib import Path
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from o2o_dps.cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from o2o_dps.development_observed_target_switch_v1 import (
    ObservedTargetSwitchBridgeV1, TwoTargetOnlyObservedSwitchBridgeV1,
)
from o2o_dps.development_wave_case_v1 import adjudicate_development_wave_completion_v1
from o2o_dps.development_wave_stratified_v1 import build_stratified_wave_case_v1
from o2o_dps.development_wave_twelve_v1 import build_twelve_wave_case_v1
from o2o_dps.development_two_wave_target_selection_v1 import (
    SELECTED as TWO_SELECTED, build_two_wave_target_case_v1,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


def _one(seed: int, bridge_path: str, stratum: str) -> dict:
    case, _ = (
        build_stratified_wave_case_v1(seed, "multi_two") if stratum == "multi_two"
        else build_two_wave_target_case_v1(seed, stratum) if stratum in TWO_SELECTED
        else build_twelve_wave_case_v1(seed, stratum)
    )
    common = dict(seed=seed, target_contexts=case.target_contexts,
                  dynamic_load=case.dynamic_load)
    with SimulatorBridgeDynamicV3(bridge_path) as raw:
        base = run_cat_fury_full_policy_rollout_v6(
            raw, case.request, CatFuryFullPolicyAdapterV4(), **common,
        )
        controller = (
            TwoTargetOnlyObservedSwitchBridgeV1(raw) if stratum in TWO_SELECTED
            else ObservedTargetSwitchBridgeV1(raw)
        )
        trial = run_cat_fury_full_policy_rollout_v6(
            controller, case.request, CatFuryFullPolicyAdapterV4(), **common,
        )
    base_terminal = adjudicate_development_wave_completion_v1(case, base)
    trial_terminal = adjudicate_development_wave_completion_v1(case, trial)
    both = base_terminal["status"] == trial_terminal["status"] == "COMPLETED"
    receipts = controller.target_switch_receipts
    return {
        "seed": seed, "stratum": stratum,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "cat_status": base_terminal["status"],
        "switch_plus_cat_status": trial_terminal["status"],
        "cat_effective_damage": base_terminal.get("own_effective_damage") if both else None,
        "switch_plus_cat_effective_damage": trial_terminal.get("own_effective_damage") if both else None,
        "paired_damage_delta": (
            trial_terminal["own_effective_damage"] - base_terminal["own_effective_damage"]
            if both else None
        ),
        "cat_ttk_ms": base.get("final_state", {}).get("time_ms") if both else None,
        "switch_plus_cat_ttk_ms": trial.get("final_state", {}).get("time_ms") if both else None,
        "cat_decisions": len(base.get("steps") or []),
        "switch_plus_cat_decisions": len(trial.get("steps") or []),
        "native_switch_accepted": bool(receipts) and all(row["accepted"] for row in receipts),
        "no_switch_needed": not receipts and (
            (trial.get("steps") or [{}])[0].get("simulator_state_before", {}).get("target_index") == 0
        ),
        "selected_target_indices": [row["requested_target_index"] for row in receipts],
        "first_cat_target_index": (base.get("steps") or [{}])[0].get("simulator_state_before", {}).get("target_index"),
        "first_switch_plus_cat_target_index": (trial.get("steps") or [{}])[0].get("simulator_state_before", {}).get("target_index"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-start", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--strata", default="multi_two")
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    strata = tuple(args.strata.split(","))
    allowed = {"multi_two", "multi_3_targets", "multi_4_targets", "multi_5_6_targets",
               "multi_7_9_targets", "multi_10plus_targets", *TWO_SELECTED}
    if not strata or len(set(strata)) != len(strata) or any(x not in allowed for x in strata):
        raise ValueError("strata must be distinct supported target-count waves")
    jobs = [(stratum, seed) for stratum in strata
            for seed in range(args.seed_start, args.seed_start + args.seed_count)]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(
            _one, [seed for _, seed in jobs], [str(args.bridge)] * len(jobs),
            [stratum for stratum, _ in jobs],
        ))
    def summary_for(stratum: str) -> dict:
        wave_rows = [row for row in rows if row["stratum"] == stratum]
        paired = [row["paired_damage_delta"] for row in wave_rows
                  if row["paired_damage_delta"] is not None]
        return {
        "scope": "MODEL_DEFINED_TARGET_CONTROL_DEVELOPMENT_ONLY",
        "rule": "CURRENT_OBSERVED_HIGHEST_HP_INITIAL; INVALID_TARGET_ONLY_LATER",
        "source_policy": "CAT_PROFILE1_UNMODIFIED_ACTIONS",
        "stratum": stratum,
        "source_wave_ref": wave_rows[0]["source_wave_ref"],
        "seed_start": args.seed_start, "seed_count": args.seed_count,
        "both_complete_count": len(paired),
        "censored_count": len(wave_rows) - len(paired),
        "cat_complete_count": sum(row["cat_status"] == "COMPLETED" for row in wave_rows),
        "switch_plus_cat_complete_count": sum(row["switch_plus_cat_status"] == "COMPLETED" for row in wave_rows),
        "native_switch_accepted_count": sum(row["native_switch_accepted"] for row in wave_rows),
        "no_switch_needed_count": sum(row["no_switch_needed"] for row in wave_rows),
        "paired_mean_effective_damage_delta": mean(paired) if paired else None,
        "paired_standard_error": stdev(paired) / math.sqrt(len(paired)) if len(paired) > 1 else None,
        "paired_wins": sum(value > 0 for value in paired),
        "paired_ties": sum(value == 0 for value in paired),
        "paired_losses": sum(value < 0 for value in paired),
        }
    summaries = {stratum: summary_for(stratum) for stratum in strata}
    result = {"schema": "development_observed_target_switch_batch/v1",
              "summaries": summaries, "per_seed_terminal_rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
