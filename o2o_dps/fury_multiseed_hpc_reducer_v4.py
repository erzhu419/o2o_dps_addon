"""Single-pass strict reducer with paired traces for Fury horizon analysis v2.

The v3 worker receipt and rollout validators remain authoritative.  This
additive reducer walks every planned paired group exactly once, retains the
same aggregate diagnostics, and captures the two predeclared candidate
contrasts needed by the dual-baseline horizon analysis.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    DEVELOPMENT_EXECUTION_KIND_V3,
    FuryMultiseedHpcDispatchV3Error,
    validate_dispatch_plan_v3,
)
from .fury_multiseed_hpc_reducer_v3 import (
    FuryMultiseedHpcReducerV3Error,
    _validate_group_receipt,
)
from .fury_multiseed_hpc_worker_v3 import FuryMultiseedHpcWorkerV3Error
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    FuryPairedRunnerV4Error,
    HISTORICAL_POLICY_ID,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
REDUCTION_SCHEMA_V4 = "fury_multiseed_dynamic_v5_hpc_reduction_with_trace/v4"
STATUS_V4 = "COMPLETE_DIAGNOSTIC_SIMULATOR_ONLY_NONVOTING"
TRACE_BASELINES_V4 = (CAT_POLICY_ID, CONTRA260817_POLICY_ID)
TRACE_LABEL_BY_BASELINE_V4 = {
    CAT_POLICY_ID: "candidate_minus_cat",
    CONTRA260817_POLICY_ID: "candidate_minus_contra260817",
}
CURRENT_FORMAL_BLOCKERS_V4 = (
    "DYNAMIC_V5_SIMULATOR_HYPOTHESIS_NONVOTING",
    "LIVE_CLIENT_FIDELITY_NOT_ESTABLISHED",
    "HISTORICAL_DYNAMIC_V5_LANE_MISSING",
)


class FuryMultiseedHpcReducerV4Error(RuntimeError):
    """The four-lane output set is missing, conflicting, or invalid."""


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcReducerV4Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryMultiseedHpcReducerV4Error(f"{label} must be an object")
    return value


def _read_rows(path: Path) -> list[JSONMap]:
    rows: list[JSONMap] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise FuryMultiseedHpcReducerV4Error(
                        f"blank rollout row at {path}:{line_number}"
                    )
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise FuryMultiseedHpcReducerV4Error(
                        f"rollout row is not an object at {path}:{line_number}"
                    )
                rows.append(value)
    except FuryMultiseedHpcReducerV4Error:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcReducerV4Error(
            f"could not read rollout rows {path}: {error}"
        ) from error
    return rows


def _planned_node_by_group(dispatch: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in dispatch["nodes"]:
        for group_id in node["group_ids"]:
            if group_id in result:
                raise FuryMultiseedHpcReducerV4Error(
                    "dispatch assigns one group to multiple nodes"
                )
            result[str(group_id)] = str(node["name"])
    return result


def _new_stats() -> JSONMap:
    return {
        "rollout_count": 0,
        "damage_sum": 0.0,
        "elapsed_ms_sum": 0,
        "dps_sum": 0.0,
        "dps_squared_sum": 0.0,
        "completion_count": 0,
        "offline_score_eligible_count": 0,
        "omitted_lane_count_sum": 0,
        "fatal_error_count_sum": 0,
    }


def _merge_stats(destination: JSONMap, source: Mapping[str, Any]) -> None:
    for field in destination:
        destination[field] += source[field]


def _new_pair_stats() -> JSONMap:
    return {
        "pair_count": 0,
        "dps_delta_sum": 0.0,
        "dps_delta_squared_sum": 0.0,
        "both_offline_score_eligible_count": 0,
    }


def reduce_dispatch_v4(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    output_directory: str | Path,
) -> JSONMap:
    """Validate one arm in one pass and return aggregates plus two traces."""

    try:
        plan = validate_runner_plan(runner_plan)
        checked_dispatch = validate_dispatch_plan_v3(dispatch, plan)
    except (FuryPairedRunnerV4Error, FuryMultiseedHpcDispatchV3Error) as error:
        raise FuryMultiseedHpcReducerV4Error(str(error)) from error

    root = Path(output_directory).expanduser().resolve()
    groups = {str(row["group_id"]): row for row in plan["contract"]["groups"]}
    expected_group_ids = sorted(groups)
    node_by_group = _planned_node_by_group(checked_dispatch)
    if sorted(node_by_group) != expected_group_ids:
        raise FuryMultiseedHpcReducerV4Error(
            "dispatch group set differs from runner plan"
        )

    by_policy = {policy_id: _new_stats() for policy_id in ALLOWED_POLICY_IDS_V3}
    by_scenario_policy: dict[tuple[str, str, str], JSONMap] = defaultdict(_new_stats)
    completion_modes = {
        policy_id: Counter() for policy_id in ALLOWED_POLICY_IDS_V3
    }
    pair_stats = {
        "candidate_minus_cat": _new_pair_stats(),
        "candidate_minus_deployed_contra": _new_pair_stats(),
        "candidate_minus_contra260817": _new_pair_stats(),
    }
    traces: dict[str, list[JSONMap]] = {
        label: [] for label in TRACE_LABEL_BY_BASELINE_V4.values()
    }
    total_rows = 0
    all_runtime_receipts_complete = True

    for group_id in expected_group_ids:
        group = groups[group_id]
        receipt_path = root / "receipts" / f"{group_id}.json"
        if not receipt_path.is_file():
            raise FuryMultiseedHpcReducerV4Error(
                f"worker receipt missing for paired group {group_id}"
            )
        receipt = _read_json(receipt_path, "group receipt")
        if receipt.get("result_file") != f"groups/{group_id}.jsonl":
            raise FuryMultiseedHpcReducerV4Error("group result path mismatch")
        result_path = root / "groups" / f"{group_id}.jsonl"
        if not result_path.is_file():
            raise FuryMultiseedHpcReducerV4Error(
                f"rollout rows missing for paired group {group_id}"
            )
        try:
            rows = _validate_group_receipt(
                receipt,
                plan=plan,
                dispatch=checked_dispatch,
                group=group,
                rows=_read_rows(result_path),
            )
        except (
            FuryMultiseedHpcReducerV3Error,
            FuryMultiseedHpcWorkerV3Error,
        ) as error:
            raise FuryMultiseedHpcReducerV4Error(str(error)) from error

        by_id = {row["policy_identity"]["policy_id"]: row for row in rows}
        if tuple(row["policy_identity"]["policy_id"] for row in rows) != tuple(
            ALLOWED_POLICY_IDS_V3
        ):
            raise FuryMultiseedHpcReducerV4Error(
                "group rows are not the exact ordered four-lane policy set"
            )
        for policy_id, row in by_id.items():
            stats = row["sufficient_statistics"]
            _merge_stats(by_policy[policy_id], stats)
            scenario = row["scenario_identity"]
            key = (scenario["instance_id"], scenario["scenario_id"], policy_id)
            _merge_stats(by_scenario_policy[key], stats)
            lane = row["lane_result"]
            completion_modes[policy_id][lane["completion_mode"]] += 1
            all_runtime_receipts_complete = (
                all_runtime_receipts_complete
                and lane["dynamic_runtime_receipts_complete"] is True
            )

        candidate = by_id[CAT2NEW_POLICY_ID]["lane_result"]
        candidate_dps = float(candidate["dps"])
        for label, baseline_id in (
            ("candidate_minus_cat", CAT_POLICY_ID),
            ("candidate_minus_deployed_contra", CONTRA_DEPLOYED_POLICY_ID),
            ("candidate_minus_contra260817", CONTRA260817_POLICY_ID),
        ):
            baseline = by_id[baseline_id]["lane_result"]
            baseline_dps = float(baseline["dps"])
            delta = candidate_dps - baseline_dps
            if not math.isfinite(delta):
                raise FuryMultiseedHpcReducerV4Error(
                    "paired dps delta is not finite"
                )
            pair = pair_stats[label]
            pair["pair_count"] += 1
            pair["dps_delta_sum"] += delta
            pair["dps_delta_squared_sum"] += delta * delta
            pair["both_offline_score_eligible_count"] += int(
                baseline["offline_score_eligible"] is True
                and candidate["offline_score_eligible"] is True
            )
            if baseline_id in TRACE_BASELINES_V4:
                traces[label].append(
                    {
                        "group_id": group_id,
                        "master_seed": group["master_seed"],
                        "simulator_seed": group["simulator_seed"],
                        "candidate_policy_id": CAT2NEW_POLICY_ID,
                        "baseline_policy_id": baseline_id,
                        "candidate_dps": candidate_dps,
                        "baseline_dps": baseline_dps,
                        "candidate_minus_baseline_dps": delta,
                    }
                )
        total_rows += len(rows)

    if total_rows != checked_dispatch["expected_rollout_count"]:
        raise FuryMultiseedHpcReducerV4Error(
            "reduced rollout count differs from dispatch plan"
        )
    scenario_rows = [
        {
            "instance_id": instance_id,
            "scenario_id": scenario_id,
            "policy_id": policy_id,
            "sufficient_statistics": stats,
        }
        for (instance_id, scenario_id, policy_id), stats in sorted(
            by_scenario_policy.items()
        )
    ]
    contrast_readiness = {
        label: {
            "candidate_policy_id": CAT2NEW_POLICY_ID,
            "baseline_policy_id": baseline_id,
            "required_pair_count": len(expected_group_ids),
            "observed_pair_count": pair_stats[label]["pair_count"],
            "complete_eligible_pair_count": pair_stats[label][
                "both_offline_score_eligible_count"
            ],
            "analysis_eligible": (
                pair_stats[label]["pair_count"] == len(expected_group_ids)
                and pair_stats[label]["both_offline_score_eligible_count"]
                == len(expected_group_ids)
            ),
        }
        for label, baseline_id in (
            ("candidate_minus_cat", CAT_POLICY_ID),
            ("candidate_minus_deployed_contra", CONTRA_DEPLOYED_POLICY_ID),
            ("candidate_minus_contra260817", CONTRA260817_POLICY_ID),
        )
    }
    return {
        "schema": REDUCTION_SCHEMA_V4,
        "status": STATUS_V4,
        "runner_plan_sha256": plan["plan_sha256"],
        "dispatch_plan_sha256": sha256_json(checked_dispatch),
        "execution_kind": checked_dispatch["execution_kind"],
        "paired_group_count": len(expected_group_ids),
        "unique_rollout_count": total_rows,
        "expected_rollout_count": checked_dispatch["expected_rollout_count"],
        "policy_ids": list(ALLOWED_POLICY_IDS_V3),
        "policy_sufficient_statistics": by_policy,
        "scenario_policy_sufficient_statistics": scenario_rows,
        "completion_mode_counts": {
            policy_id: dict(sorted(counter.items()))
            for policy_id, counter in completion_modes.items()
        },
        "paired_sufficient_statistics": pair_stats,
        "contrast_readiness": contrast_readiness,
        "paired_traces": traces,
        "trace_baseline_policy_ids": list(TRACE_BASELINES_V4),
        "dynamic_runtime_receipts_complete": all_runtime_receipts_complete,
        "formal_comparison_status": "BLOCKED",
        "formal_gate_source": "fury_multiseed_hpc_reducer_v4_current_readiness",
        "formal_missing_policy_ids": [HISTORICAL_POLICY_ID],
        "formal_blocker_codes": list(CURRENT_FORMAL_BLOCKERS_V4),
        "statistical_test_performed": False,
        "heavy_execution_started": checked_dispatch["execution_kind"]
        == DEVELOPMENT_EXECUTION_KIND_V3,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


def compact_reduction_v4(value: Mapping[str, Any]) -> JSONMap:
    """Return the aggregate reduction without per-group paired traces."""

    result = dict(value)
    result.pop("paired_traces", None)
    return result


def _write_json(path: Path | None, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if path is None:
        print(payload, end="")
        return
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = reduce_dispatch_v4(
            _read_json(args.runner_plan, "runner plan"),
            _read_json(args.dispatch_plan, "dispatch plan"),
            output_directory=args.output_directory,
        )
        _write_json(args.output, result)
        return 0
    except (FuryMultiseedHpcReducerV4Error, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "FuryMultiseedHpcReducerV4Error",
    "REDUCTION_SCHEMA_V4",
    "STATUS_V4",
    "TRACE_BASELINES_V4",
    "compact_reduction_v4",
    "reduce_dispatch_v4",
)
