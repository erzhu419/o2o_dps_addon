"""Compact descriptive Cat gap audit; no post-divergence causal attribution.

The frozen 3x256 Horizon-v2 comparison and a full one-seed group are different
evidence.  The latter exposes accepted commands and some result receipts, not
a restorable same-prefix intervention.  Missing event counts remain missing.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .fury_paired_action_trace_diagnostic_v1 import (
    CANDIDATE_ID,
    CAT_ID,
    diagnose_group_rows,
)


SCHEMA = "value_gap_attribution/v1"
HORIZON_SCHEMA = "cat2new_fury_horizon_analysis/v2"
FOCAL_ARM = "ww_wait_cat_timing"
COMPLETE = "SCENARIO_HORIZON_REACHED"


def _horizon_snapshot(compact: Mapping[str, Any] | None) -> dict[str, Any]:
    if compact is None:
        return {"status": "NOT_PROVIDED", "paired_contrast": None}
    if compact.get("schema") != HORIZON_SCHEMA or compact.get("selection_gate", {}).get("status") != "NO_SELECTION":
        raise ValueError("not the completed, non-selected Horizon-v2 compact analysis")
    matches = [
        row for row in compact.get("contrast_results", [])
        if row.get("arm_id") == FOCAL_ARM and row.get("baseline_policy_id") == CAT_ID
    ]
    if len(matches) != 1 or matches[0].get("paired_n") != 256:
        raise ValueError("Horizon-v2 focal arm lacks its 256 Cat pairs")
    row = matches[0]
    return {
        "status": "COMPLETED_PAIRED_SIMULATOR_COMPARISON",
        "confirmation_id": compact.get("confirmation_id"),
        "selection_gate": "NO_SELECTION",
        "paired_contrast": {
            "arm_id": FOCAL_ARM,
            "paired_n": 256,
            "candidate_minus_cat_mean_dps": row["candidate_minus_baseline_mean_dps"],
            "candidate_minus_cat_standard_error_dps": row["candidate_minus_baseline_standard_error_dps"],
            "wins": row["wins"],
            "ties": row["ties"],
            "losses": row["losses"],
            "holm_adjusted_p": row["holm_adjusted_p"],
        },
    }


def _accepted_actions(projection: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        event for event in projection["actions"]
        if event["acceptance"] == "ACCEPTED" and event["lane"] != "cvar"
    ]


def _action_metrics(projection: Mapping[str, Any]) -> dict[str, Any]:
    accepted = _accepted_actions(projection)
    by_action = Counter(
        (event["lane"], str(event["action"])) for event in accepted
    )
    queue = [event for event in accepted if event["lane"] == "swing_queue"]
    rejected_queue = [
        event for event in projection["actions"]
        if event["lane"] == "swing_queue" and event["acceptance"] != "ACCEPTED"
    ]
    gcd = [event for event in accepted if event["lane"] == "gcd"]
    return {
        "accepted_by_lane_action": [
            {"lane": lane, "action": action, "count": count}
            for (lane, action), count in sorted(by_action.items())
        ],
        "queue_accepted_count": len(queue),
        "queue_cancel_accepted_count": sum(
            "CANCEL" in str(event["action"]).upper() for event in queue
        ),
        "queue_rejected_count": len(rejected_queue),
        "accepted_gcd_timeline": [
            {"time_ms": event["time_ms"], "action": event["action"]}
            for event in gcd
        ],
        "accepted_slam_swing_window": [
            {
                "time_ms": event["time_ms"],
                "mh_swing_remaining_ms_before": event["mh_swing_remaining_ms_before"],
            }
            for event in gcd if event["action"] == "warrior.slam"
        ],
    }


def _pre_divergence_observation(
    cat_artifact: Mapping[str, Any],
    candidate_artifact: Mapping[str, Any],
    time_ms: int,
) -> dict[str, Any]:
    cat = next(
        (step["simulator_state_before"] for step in cat_artifact["steps"]
         if step["simulator_state_before"]["time_ms"] == time_ms),
        None,
    )
    candidate = next(
        (decision["carried_state_before"] for decision in candidate_artifact["decisions"]
         if decision["time_ms"] == time_ms),
        None,
    )
    fields = (
        "power", "gcd_remaining_ms", "mh_swing_remaining_ms",
        "oh_swing_remaining_ms", "target_index", "auras", "target_auras",
    )
    if cat is None or candidate is None:
        return {"status": "NOT_RETAINED", "projected_fields_equal": None}
    mismatches = [field for field in fields if cat.get(field) != candidate.get(field)]
    return {
        "status": "PROJECTED_OBSERVATION_ONLY",
        "projected_fields_equal": not mismatches,
        "mismatched_fields": mismatches,
        "rage_before": cat["power"]["current"],
        "mh_swing_remaining_ms_before": cat["mh_swing_remaining_ms"],
        "gcd_remaining_ms_before": cat["gcd_remaining_ms"],
        "full_simulator_state_and_rng_equal": None,
    }


def _candidate_white_receipts(artifact: Mapping[str, Any]) -> dict[str, Any]:
    receipts = artifact["lifecycle_receipt"]["terminal_dynamic_receipts"]["candidate_damage"]["receipts"]
    white = [
        receipt for receipt in receipts
        if receipt["action"].get("other_id") == 7
        and receipt["action"].get("spell_id") == 0
    ]
    return {
        "source": "native candidate damage receipts; OtherActionAttack=7",
        "attempts": len(white),
        "positive_damage_events": sum(receipt["applied_damage"] > 0 for receipt in white),
        "applied_damage": sum(receipt["applied_damage"] for receipt in white),
    }


def diagnose_value_gap(
    group_rows: Sequence[Mapping[str, Any]],
    horizon_compact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe one completed group and the separately frozen 256-pair result."""
    selected = {
        row["policy_identity"]["policy_id"]: row
        for row in group_rows
        if row["policy_identity"]["policy_id"] in (CAT_ID, CANDIDATE_ID)
    }
    if set(selected) != {CAT_ID, CANDIDATE_ID}:
        raise ValueError("group lacks Cat or candidate")
    for row in selected.values():
        if row["lane_result"]["completion_mode"] != COMPLETE:
            raise ValueError("descriptive value-gap group requires two complete horizon lanes")
    trace = diagnose_group_rows(group_rows)
    divergence = trace["first_accepted_control_divergence"]
    cat_artifact = selected[CAT_ID]["lane_result"]["artifact"]
    candidate_artifact = selected[CANDIDATE_ID]["lane_result"]["artifact"]
    cat_results = trace["cat"]["typed_result_events"]
    return {
        "schema": SCHEMA,
        "inference_class": "SIMULATOR_DESCRIPTIVE_DIAGNOSTIC_ONLY",
        "horizon_v2_aggregate": _horizon_snapshot(horizon_compact),
        "one_seed_group": {
            "group_id": trace["group_id"],
            "master_seed": selected[CAT_ID]["group_identity"]["master_seed"],
            "simulator_seed": trace["simulator_seed"],
            "scenario_id": trace["scenario_id"],
            "first_accepted_control_difference": divergence,
            "observed_before_first_difference": (
                _pre_divergence_observation(
                    cat_artifact, candidate_artifact, divergence["time_ms"]
                ) if divergence else None
            ),
            "first_accepted_gcd_difference": trace["first_accepted_gcd_divergence"],
            "cat": {
                "termination": trace["cat"]["lane_summary"],
                "ending_rage": cat_artifact["final_state"]["power"]["current"],
                **_action_metrics(trace["cat"]),
                "typed_action_result_count": len(cat_results),
                "typed_action_result_damage": sum(event["damage"] for event in cat_results),
                "white_attack_receipts": None,
            },
            "candidate": {
                "termination": trace["candidate"]["lane_summary"],
                "ending_rage": candidate_artifact["lifecycle_receipt"]["final_state"]["state"]["power"]["current"],
                **_action_metrics(trace["candidate"]),
                "typed_action_result_count": None,
                "white_attack_receipts": _candidate_white_receipts(candidate_artifact),
            },
            "unmeasured": [
                "Cat white-hit count: this Cat-v6 artifact retains action result stream but not the native white-hit ledger",
                "rage overcap amount and rage-insufficient WAIT cause: no event-level counter in both lane artifacts",
                "ready-to-accepted delay and causal Slam swing shift: no common restorable state timeline",
            ],
        },
        "same_prefix_counterfactual": {
            "status": "NOT_EXECUTED",
            "reason": "Cat-v6 rollouter requires exact CatFuryFullPolicyAdapterV4 type; current paired artifacts have no cloneable shared simulator/RNG state plus one-action intervention executor",
            "single_action_replacement_value": None,
            "future_rng_branch_values": None,
            "post_divergence_differences_are_causal_contributions": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group_jsonl", type=Path)
    parser.add_argument("--horizon-compact", type=Path)
    parser.add_argument("--horizon-attempt-directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.horizon_compact and args.horizon_attempt_directory:
        parser.error("select one compact Horizon-v2 source")
    compact = None
    if args.horizon_compact:
        compact = json.loads(args.horizon_compact.read_text(encoding="utf-8"))
    elif args.horizon_attempt_directory:
        from .fury_cat2_horizon_remote_orchestrator_v1 import fetch_compact_result_v1
        compact = fetch_compact_result_v1(attempt_directory=args.horizon_attempt_directory)
    with args.group_jsonl.open(encoding="utf-8") as source:
        rows = [json.loads(line) for line in source if line.strip()]
    result = diagnose_value_gap(rows, compact)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"schema": result["schema"], "output": str(args.output)}, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
