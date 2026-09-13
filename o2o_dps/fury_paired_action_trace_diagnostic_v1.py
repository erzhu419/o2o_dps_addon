"""Compact, simulator-only Cat versus Cat2_new action trace from one group row.

The rollout row already contains the producer artifacts.  This projection does
not replay them and deliberately does not assign interval damage to an action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "fury_paired_action_trace_diagnostic/v1"
CAT_ID = "cat.fury.profile1"
CANDIDATE_ID = "cat2new.fury.candidate"


def _cat_projection(artifact: Mapping[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    damage_samples: list[dict[str, Any]] = []
    typed_results: list[dict[str, Any]] = []
    for step in artifact["steps"]:
        before = step["simulator_state_before"]
        after = step["simulator_state_next_epoch"]
        proposal = step["proposal"]
        time_ms = before["time_ms"]
        damage_samples.append(
            {
                "time_ms": after["time_ms"],
                "cumulative_damage": after["damage_done"],
            }
        )
        for event in step["ordered_execution"]["sink_events"]:
            lane = event["source_sink"]["channel"]
            if lane == "swing_queue":
                action = proposal["swing_queue"]
            else:
                action = event["operation_contract"].get("canonical_action")
            actions.append(
                {
                    "time_ms": time_ms,
                    "lane": lane,
                    "action": action,
                    "acceptance": event["simulator_acceptance"]["status"],
                    "rage_before": before["power"]["current"],
                    "mh_swing_remaining_ms_before": before["mh_swing_remaining_ms"],
                    "gcd_remaining_ms_before": before["gcd_remaining_ms"],
                }
            )
        typed_results.extend(
            {
                "time_ms": event["time_ms"],
                "spell_id": event["action"].get("spell_id"),
                "outcome": event["outcome"],
                "damage": event["damage"],
            }
            for event in step["server_observation_after_advance"]["result_stream"]["events"]
        )
    return {
        "actions": actions,
        "damage_samples": damage_samples,
        "typed_result_events": typed_results,
        "damage_sample_scope": "state at each completed Cat step; interval damage is not action attribution",
    }


def _candidate_projection(artifact: Mapping[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for decision in artifact["decisions"]:
        before = decision["carried_state_before"]
        for event in decision["v5_execution"]["ordered_events"]:
            lane = event["lane"]
            if lane == "wait":
                continue
            action = (
                event["arguments"].get("action_key")
                if event["intent"] == "CAST_ACTION"
                else event["intent"]
            )
            actions.append(
                {
                    "time_ms": decision["time_ms"],
                    "lane": lane,
                    "action": action,
                    "acceptance": event["simulator_acceptance"]["status"],
                    "rage_before": before["power"]["current"],
                    "mh_swing_remaining_ms_before": before["mh_swing_remaining_ms"],
                    "gcd_remaining_ms_before": before["gcd_remaining_ms"],
                }
            )
    samples = [
        {
            "time_ms": advance["after_time_ms"],
            "cumulative_damage": advance["state_after"]["state"]["damage_done"],
        }
        for advance in artifact["idle_advances"]
    ]
    return {
        "actions": actions,
        "damage_samples": samples,
        "typed_result_events": None,
        "damage_sample_scope": "state after central idle advances only; typed per-hit result is not exposed here",
    }


def _first_control_divergence(
    cat_actions: Sequence[Mapping[str, Any]],
    candidate_actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    def at_time(actions: Sequence[Mapping[str, Any]], time_ms: int) -> list[dict[str, str]]:
        return [
            {"lane": row["lane"], "action": row["action"]}
            for row in actions
            if row["time_ms"] == time_ms and row["acceptance"] == "ACCEPTED"
        ]

    times = sorted(
        {row["time_ms"] for row in cat_actions}
        | {row["time_ms"] for row in candidate_actions}
    )
    for time_ms in times:
        cat = at_time(cat_actions, time_ms)
        candidate = at_time(candidate_actions, time_ms)
        if cat != candidate:
            return {"time_ms": time_ms, "cat": cat, "candidate": candidate}
    return None


def _first_gcd_divergence(
    cat_actions: Sequence[Mapping[str, Any]],
    candidate_actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    cat = [
        row for row in cat_actions
        if row["lane"] == "gcd" and row["acceptance"] == "ACCEPTED"
    ]
    candidate = [
        row for row in candidate_actions
        if row["lane"] == "gcd" and row["acceptance"] == "ACCEPTED"
    ]
    for index in range(max(len(cat), len(candidate))):
        left = cat[index] if index < len(cat) else None
        right = candidate[index] if index < len(candidate) else None
        if left is None or right is None or (left["time_ms"], left["action"]) != (
            right["time_ms"], right["action"]
        ):
            return {"ordinal": index + 1, "cat": left, "candidate": right}
    return None


def diagnose_group_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected = {
        row["policy_identity"]["policy_id"]: row
        for row in rows
        if row["policy_identity"]["policy_id"] in (CAT_ID, CANDIDATE_ID)
    }
    if set(selected) != {CAT_ID, CANDIDATE_ID}:
        raise ValueError("group must contain one Cat and one Cat2_new rollout")
    cat_row = selected[CAT_ID]
    candidate_row = selected[CANDIDATE_ID]
    identity = cat_row["group_identity"]
    if candidate_row["group_identity"] != identity:
        raise ValueError("Cat and Cat2_new rows are not from the same paired group")
    if cat_row["dynamic_load_identity"] != candidate_row["dynamic_load_identity"]:
        raise ValueError("Cat and Cat2_new dynamic loads differ")
    cat = _cat_projection(cat_row["lane_result"]["artifact"])
    candidate = _candidate_projection(candidate_row["lane_result"]["artifact"])
    for row, projection in ((cat_row, cat), (candidate_row, candidate)):
        lane = row["lane_result"]
        projection["lane_summary"] = {
            "damage": lane["damage"],
            "elapsed_ms": lane["elapsed_ms"],
            "dps": lane["dps"],
            "completion_mode": lane["completion_mode"],
        }
    return {
        "schema": SCHEMA,
        "evidence_scope": "one paired simulator group; development diagnostic, not a win/loss test",
        "group_id": identity["group_id"],
        "simulator_seed": identity["simulator_seed"],
        "scenario_id": identity["scenario_id"],
        "cat": cat,
        "candidate": candidate,
        "first_accepted_control_divergence": _first_control_divergence(
            cat["actions"], candidate["actions"]
        ),
        "first_accepted_gcd_divergence": _first_gcd_divergence(
            cat["actions"], candidate["actions"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group_jsonl", type=Path)
    args = parser.parse_args()
    with args.group_jsonl.open(encoding="utf-8") as source:
        rows = [json.loads(line) for line in source if line.strip()]
    print(json.dumps(diagnose_group_rows(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
