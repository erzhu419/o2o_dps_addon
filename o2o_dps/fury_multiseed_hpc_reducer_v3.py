"""Reducer for complete four-lane native dynamic-v3 development outputs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CAT2NEW_PRODUCER,
    validate_cat2new_fury_paired_artifact_v3,
)
from .cat_fury_paired_lane_adapter_v6 import (
    CAT_V6_PRODUCER,
    validate_cat_runner_v4_artifact_v6,
)
from .contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    validate_contra260817_runner_v4_artifact_v4,
)
from .fury_multiseed_hpc_dispatch_v2 import FORMAL_BLOCKERS, FORMAL_MISSING_POLICY_IDS
from .fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    DEVELOPMENT_EXECUTION_KIND_V3,
    FuryMultiseedHpcDispatchV3Error,
    validate_dispatch_plan_v3,
)
from .fury_multiseed_hpc_worker_v3 import (
    GROUP_RECEIPT_SCHEMA_V3,
    FuryMultiseedHpcWorkerV3Error,
    _validate_paired_horizon_elapsed_v3,
    validate_rollout_row_v3,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    FuryPairedRunnerV4Error,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
REDUCTION_SCHEMA_V3 = "fury_multiseed_dynamic_v5_hpc_reduction/v3"


class FuryMultiseedHpcReducerV3Error(RuntimeError):
    """The four-lane output set is missing, conflicting, or invalid."""


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcReducerV3Error(f"could not read {label}: {error}") from error
    if not isinstance(value, dict):
        raise FuryMultiseedHpcReducerV3Error(f"{label} must be an object")
    return value


def _read_rows(path: Path) -> list[JSONMap]:
    rows: list[JSONMap] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise FuryMultiseedHpcReducerV3Error(
                        f"blank rollout row at {path}:{line_number}"
                    )
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise FuryMultiseedHpcReducerV3Error(
                        f"rollout row is not an object at {path}:{line_number}"
                    )
                rows.append(value)
    except FuryMultiseedHpcReducerV3Error:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcReducerV3Error(
            f"could not read rollout rows {path}: {error}"
        ) from error
    return rows


def _planned_node_by_group(dispatch: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in dispatch["nodes"]:
        for group_id in node["group_ids"]:
            if group_id in result:
                raise FuryMultiseedHpcReducerV3Error(
                    "dispatch assigns one group to multiple nodes"
                )
            result[group_id] = node["name"]
    return result


def _artifact_validators() -> dict[str, Any]:
    return {
        CAT_V6_PRODUCER: validate_cat_runner_v4_artifact_v6,
        CONTRA260817_V4_PRODUCER: validate_contra260817_runner_v4_artifact_v4,
        CAT2NEW_PRODUCER: validate_cat2new_fury_paired_artifact_v3,
    }


def _validate_group_receipt(
    receipt: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    group: Mapping[str, Any],
    rows: list[JSONMap],
) -> list[JSONMap]:
    expected_fields = {
        "schema",
        "status",
        "runner_plan_sha256",
        "execution_kind",
        "node",
        "group_id",
        "master_seed",
        "simulator_seed",
        "scenario_contract_sha256",
        "dynamic_load_contract_sha256",
        "policy_ids",
        "result_count",
        "completion_count",
        "offline_score_eligible_count",
        "result_file",
        "gomaxprocs",
        "heavy_execution_started",
        "live_fidelity",
        "comparison_ready",
        "scientific_result_available",
    }
    if (
        set(receipt) != expected_fields
        or receipt.get("schema") != GROUP_RECEIPT_SCHEMA_V3
        or receipt.get("status") != "COMPLETE_SIMULATOR_ONLY_NONVOTING"
    ):
        raise FuryMultiseedHpcReducerV3Error(
            "group receipt field set or schema mismatch"
        )
    node_by_group = _planned_node_by_group(dispatch)
    group_id = str(group["group_id"])
    exact = {
        "runner_plan_sha256": plan["plan_sha256"],
        "execution_kind": dispatch["execution_kind"],
        "node": node_by_group[group_id],
        "group_id": group_id,
        "master_seed": group["master_seed"],
        "simulator_seed": group["simulator_seed"],
        "scenario_contract_sha256": group["scenario_contract_sha256"],
        "dynamic_load_contract_sha256": group["dynamic_load_contract_sha256"],
        "policy_ids": list(ALLOWED_POLICY_IDS_V3),
        "result_count": len(ALLOWED_POLICY_IDS_V3),
        "result_file": f"groups/{group_id}.jsonl",
        "gomaxprocs": 1,
        "heavy_execution_started": dispatch["execution_kind"]
        == DEVELOPMENT_EXECUTION_KIND_V3,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }
    for field, expected in exact.items():
        if receipt.get(field) != expected:
            raise FuryMultiseedHpcReducerV3Error(f"group receipt {field} mismatch")
    if len(rows) != len(ALLOWED_POLICY_IDS_V3):
        raise FuryMultiseedHpcReducerV3Error("group rollout count mismatch")
    contract = plan["contract"]
    scenarios = [
        row
        for row in contract["scenarios"]
        if row["instance_id"] == group["instance_id"]
        and row["scenario_id"] == group["scenario_id"]
    ]
    if len(scenarios) != 1:
        raise FuryMultiseedHpcReducerV3Error("group scenario is not unique")
    scenario = scenarios[0]
    policies = {row["policy_id"]: row for row in contract["policies"]}
    validated_rows: list[JSONMap] = []
    observed_policy_ids: list[str] = []
    for row in rows:
        policy_identity = row.get("policy_identity")
        if not isinstance(policy_identity, Mapping):
            raise FuryMultiseedHpcReducerV3Error("rollout policy identity is missing")
        policy_id = policy_identity.get("policy_id")
        if policy_id not in policies:
            raise FuryMultiseedHpcReducerV3Error("rollout policy is not planned")
        try:
            validated_rows.append(
                validate_rollout_row_v3(
                    row,
                    plan=plan,
                    group=group,
                    scenario=scenario,
                    policy=policies[str(policy_id)],
                    artifact_validators=_artifact_validators(),
                )
            )
        except FuryMultiseedHpcWorkerV3Error as error:
            raise FuryMultiseedHpcReducerV3Error(str(error)) from error
        observed_policy_ids.append(str(policy_id))
    if tuple(observed_policy_ids) != ALLOWED_POLICY_IDS_V3:
        raise FuryMultiseedHpcReducerV3Error(
            "group rows are not the exact ordered four-lane policy set"
        )
    try:
        _validate_paired_horizon_elapsed_v3(validated_rows)
    except FuryMultiseedHpcWorkerV3Error as error:
        raise FuryMultiseedHpcReducerV3Error(str(error)) from error
    completion_count = sum(
        row["sufficient_statistics"]["completion_count"] for row in validated_rows
    )
    eligible_count = sum(
        row["sufficient_statistics"]["offline_score_eligible_count"]
        for row in validated_rows
    )
    if (
        receipt.get("completion_count") != completion_count
        or receipt.get("offline_score_eligible_count") != eligible_count
    ):
        raise FuryMultiseedHpcReducerV3Error(
            "group receipt aggregate differs from rollout rows"
        )
    return validated_rows


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


def reduce_dispatch_v3(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    output_directory: str | Path,
) -> JSONMap:
    """Reduce a complete four-lane diagnostic set without formal scoring."""

    try:
        plan = validate_runner_plan(runner_plan)
        checked_dispatch = validate_dispatch_plan_v3(dispatch, plan)
    except (FuryPairedRunnerV4Error, FuryMultiseedHpcDispatchV3Error) as error:
        raise FuryMultiseedHpcReducerV3Error(str(error)) from error
    root = Path(output_directory).expanduser().resolve()
    groups = {row["group_id"]: row for row in plan["contract"]["groups"]}
    expected_group_ids = sorted(groups)
    node_by_group = _planned_node_by_group(checked_dispatch)
    if sorted(node_by_group) != expected_group_ids:
        raise FuryMultiseedHpcReducerV3Error(
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
    total_rows = 0
    all_runtime_receipts_complete = True
    for group_id in expected_group_ids:
        receipt_path = root / "receipts" / f"{group_id}.json"
        if not receipt_path.is_file():
            raise FuryMultiseedHpcReducerV3Error(
                f"worker receipt missing for paired group {group_id}"
            )
        receipt = _read_json(receipt_path, "group receipt")
        if receipt.get("result_file") != f"groups/{group_id}.jsonl":
            raise FuryMultiseedHpcReducerV3Error("group result path mismatch")
        result_path = root / "groups" / f"{group_id}.jsonl"
        if not result_path.is_file():
            raise FuryMultiseedHpcReducerV3Error(
                f"rollout rows missing for paired group {group_id}"
            )
        rows = _validate_group_receipt(
            receipt,
            plan=plan,
            dispatch=checked_dispatch,
            group=groups[group_id],
            rows=_read_rows(result_path),
        )
        by_id = {row["policy_identity"]["policy_id"]: row for row in rows}
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
        for label, baseline_id in (
            ("candidate_minus_cat", CAT_POLICY_ID),
            ("candidate_minus_deployed_contra", CONTRA_DEPLOYED_POLICY_ID),
            ("candidate_minus_contra260817", CONTRA260817_POLICY_ID),
        ):
            baseline = by_id[baseline_id]["lane_result"]
            delta = float(candidate["dps"]) - float(baseline["dps"])
            if not math.isfinite(delta):
                raise FuryMultiseedHpcReducerV3Error("paired dps delta is not finite")
            pair = pair_stats[label]
            pair["pair_count"] += 1
            pair["dps_delta_sum"] += delta
            pair["dps_delta_squared_sum"] += delta * delta
            pair["both_offline_score_eligible_count"] += int(
                baseline["offline_score_eligible"] is True
                and candidate["offline_score_eligible"] is True
            )
        total_rows += len(rows)

    if total_rows != checked_dispatch["expected_rollout_count"]:
        raise FuryMultiseedHpcReducerV3Error(
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
    return {
        "schema": REDUCTION_SCHEMA_V3,
        "status": "COMPLETE_DIAGNOSTIC_SIMULATOR_ONLY_NONVOTING",
        "runner_plan_sha256": plan["plan_sha256"],
        "execution_kind": checked_dispatch["execution_kind"],
        "paired_group_count": len(expected_group_ids),
        "unique_rollout_count": total_rows,
        "expected_rollout_count": checked_dispatch["expected_rollout_count"],
        "policy_sufficient_statistics": by_policy,
        "scenario_policy_sufficient_statistics": scenario_rows,
        "completion_mode_counts": {
            policy_id: dict(sorted(counter.items()))
            for policy_id, counter in completion_modes.items()
        },
        "paired_sufficient_statistics": pair_stats,
        "dynamic_runtime_receipts_complete": all_runtime_receipts_complete,
        "formal_comparison_status": "BLOCKED",
        "formal_gate_source": "fury_multiseed_hpc_dispatch_v2",
        "formal_missing_policy_ids": list(FORMAL_MISSING_POLICY_IDS),
        "formal_blocker_codes": list(FORMAL_BLOCKERS),
        "statistical_test_performed": False,
        "heavy_execution_started": checked_dispatch["execution_kind"]
        == DEVELOPMENT_EXECUTION_KIND_V3,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


def _new_pair_stats() -> JSONMap:
    return {
        "pair_count": 0,
        "dps_delta_sum": 0.0,
        "dps_delta_squared_sum": 0.0,
        "both_offline_score_eligible_count": 0,
    }


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
        value = reduce_dispatch_v3(
            _read_json(args.runner_plan, "runner plan"),
            _read_json(args.dispatch_plan, "dispatch plan"),
            output_directory=args.output_directory,
        )
        _write_json(args.output, value)
        return 0
    except FuryMultiseedHpcReducerV3Error as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "REDUCTION_SCHEMA_V3",
    "FuryMultiseedHpcReducerV3Error",
    "reduce_dispatch_v3",
)
