"""Complete-only compact reducer for adaptive Cat-gap v2 shards."""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, NoReturn, Sequence

from .cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from .cat2new_fury_horizon_analysis_v2 import _paired_t_test_v2
from .contra260817_fury_paired_lane_adapter_v4 import (
    contra260817_runner_v4_lane_contract_v4,
)
from .fury_cat_gap_hpc_plan_v1 import _canonical_baseline_policy_rows_v1
from .fury_cat_gap_hpc_plan_v1 import REAL_STAGE_EXECUTION_KIND_V1
from .fury_cat_gap_hpc_reducer_v1 import _equal_instance_contrast, _holm_adjust
from .fury_cat_gap_hpc_worker_v2 import (
    SHARD_PARTIAL_SCHEMA_V2,
    SHARD_RECEIPT_SCHEMA_V2,
)
from .fury_cat_gap_stage_transition_v2 import (
    LOCAL_SMOKE_EXECUTION_KIND_V2,
    FuryCatGapStageTransitionV2Error,
    validate_dispatch_plan_v2,
    validate_execution_plan_v2,
)
from .fury_multiseed_hpc_worker_v3 import _validate_paired_horizon_elapsed_v3
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    sha256_json,
)


JSONMap = dict[str, Any]
REDUCTION_SCHEMA_V2 = "fury_cat_gap_variable_lane_reduction/v1"
LOCAL_SMOKE_EVIDENCE_SCHEMA_V2 = "fury_cat_gap_adaptive_local_smoke_evidence/v2"
DEVELOPMENT_REFERENCE_AUDIT_SCHEMA_V1 = (
    "fury_cat_gap_development_optimization_reference_audit/v1"
)
DEVELOPMENT_REFERENCE_AUDIT_READY_V1 = (
    "DEVELOPMENT_OPTIMIZATION_REFERENCES_ELIGIBLE"
)
DEVELOPMENT_REFERENCE_AUDIT_BLOCKED_V1 = (
    "DEVELOPMENT_OPTIMIZATION_REFERENCES_INELIGIBLE"
)


class FuryCatGapHpcReducerV2Error(RuntimeError):
    """A missing, duplicate, failed, or ineligible v2 lane forbids reduction."""


def _fail(message: str) -> NoReturn:
    raise FuryCatGapHpcReducerV2Error(f"STAGE_FAILED_NO_RETENTION: {message}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"could not read {label}: {error}")
    return dict(_mapping(value, label))


def build_development_reference_audit_v1(
    execution_plan: Mapping[str, Any],
) -> JSONMap:
    """Audit the two source-simulator baselines used only to guide development.

    ``comparison_ready`` deliberately remains false.  This audit admits exact,
    executable, blocker-free source translations as optimization references; it
    does not promote them to live-faithful or scientific comparison evidence.
    """

    runner = _mapping(execution_plan.get("runner_plan"), "runner plan")
    contract = _mapping(runner.get("contract"), "runner contract")
    raw_lanes = contract.get("lane_contracts")
    raw_policies = contract.get("policies")
    if not isinstance(raw_lanes, list) or not isinstance(raw_policies, list):
        _fail("runner development reference declarations are missing")

    lane_rows = [
        dict(_mapping(row, "lane contract"))
        for row in raw_lanes
        if isinstance(row, Mapping)
        and row.get("policy_id") in {CAT_POLICY_ID, CONTRA260817_POLICY_ID}
    ]
    policy_rows = [
        dict(_mapping(row, "policy identity"))
        for row in raw_policies
        if isinstance(row, Mapping)
        and row.get("policy_id") in {CAT_POLICY_ID, CONTRA260817_POLICY_ID}
    ]
    lanes_by_id = {str(row.get("policy_id")): row for row in lane_rows}
    policies_by_id = {str(row.get("policy_id")): row for row in policy_rows}
    expected_lanes = {
        CAT_POLICY_ID: cat_runner_v4_lane_contract_v6(),
        CONTRA260817_POLICY_ID: contra260817_runner_v4_lane_contract_v4(),
    }
    expected_policies = {
        str(row["policy_id"]): row for row in _canonical_baseline_policy_rows_v1()
    }

    duplicate_or_missing = (
        len(lane_rows) != 2
        or len(lanes_by_id) != 2
        or len(policy_rows) != 2
        or len(policies_by_id) != 2
        or set(lanes_by_id) != set(expected_lanes)
        or set(policies_by_id) != set(expected_policies)
    )
    references: list[JSONMap] = []
    for policy_id in (CAT_POLICY_ID, CONTRA260817_POLICY_ID):
        observed_lane = lanes_by_id.get(policy_id)
        observed_policy = policies_by_id.get(policy_id)
        expected_lane = expected_lanes[policy_id]
        expected_policy = expected_policies[policy_id]
        lane_matches = observed_lane == expected_lane
        policy_matches = observed_policy == expected_policy
        executable = (
            observed_lane is not None
            and observed_lane.get("dynamic_v5_executable") is True
        )
        blocker_free = (
            observed_lane is not None and observed_lane.get("blocker_codes") == []
        )
        boundary_matches = (
            observed_lane is not None
            and observed_lane.get("simulator_only") is True
            and observed_lane.get("live_fidelity") is False
            and observed_lane.get("comparison_ready") is False
        )
        eligible = (
            not duplicate_or_missing
            and lane_matches
            and policy_matches
            and executable
            and blocker_free
            and boundary_matches
        )
        references.append(
            {
                "policy_id": policy_id,
                "producer": (
                    observed_lane.get("producer") if observed_lane is not None else None
                ),
                "artifact_schema": (
                    observed_lane.get("artifact_schema")
                    if observed_lane is not None
                    else None
                ),
                "source_oracle_status": (
                    observed_lane.get("source_oracle_status")
                    if observed_lane is not None
                    else None
                ),
                "ordered_sink_status": (
                    observed_lane.get("ordered_sink_status")
                    if observed_lane is not None
                    else None
                ),
                "full_policy_status": (
                    observed_lane.get("full_policy_status")
                    if observed_lane is not None
                    else None
                ),
                "exact_policy_identity_matches": policy_matches,
                "exact_lane_contract_matches": lane_matches,
                "dynamic_v5_executable": executable,
                "blocker_codes": (
                    list(observed_lane.get("blocker_codes", []))
                    if observed_lane is not None
                    else ["REFERENCE_LANE_MISSING"]
                ),
                "runtime_artifact_validation_required": True,
                "optimization_reference_eligible": eligible,
                "scientific_comparison_eligible": False,
            }
        )

    ready = all(row["optimization_reference_eligible"] for row in references)
    return {
        "schema": DEVELOPMENT_REFERENCE_AUDIT_SCHEMA_V1,
        "status": (
            DEVELOPMENT_REFERENCE_AUDIT_READY_V1
            if ready
            else DEVELOPMENT_REFERENCE_AUDIT_BLOCKED_V1
        ),
        "reference_policy_ids": [CAT_POLICY_ID, CONTRA260817_POLICY_ID],
        "references": references,
        "optimization_reference_eligible": ready,
        "scientific_comparison_eligible": False,
        "authority": (
            "EXACT_SOURCE_SIMULATOR_TRANSLATIONS_FOR_DEVELOPMENT_SEARCH_ONLY"
        ),
        "live_or_scientific_promotion_performed": False,
    }


def _node_by_shard(dispatch: Mapping[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for node in dispatch["nodes"]:
        for raw_index in node["shard_indices"]:
            index = int(raw_index)
            if index in result:
                _fail("one shard is assigned to multiple nodes")
            result[index] = str(node["name"])
    return result


def _validate_raw_gzip(path: Path, receipt: Mapping[str, Any], index: int) -> None:
    if not path.is_file():
        _fail(f"shard {index} retained raw gzip is missing")
    prefix = path.read_bytes()[:10]
    if (
        len(prefix) < 10
        or prefix[:2] != b"\x1f\x8b"
        or prefix[2] != 8
        or prefix[4:8] != b"\x00\x00\x00\x00"
    ):
        _fail(f"shard {index} raw result is not deterministic gzip mtime 0")
    if (
        receipt.get("compressed_sha256") != _file_sha256(path)
        or receipt.get("compressed_size_bytes") != path.stat().st_size
    ):
        _fail(f"shard {index} raw gzip identity differs from receipt")


def _read_validated_partial(
    *,
    root: Path,
    plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    shard: Mapping[str, Any],
    node: str,
) -> list[JSONMap]:
    index = int(shard["shard_index"])
    receipt_path = root / f"receipts/shard-{index:05d}.json"
    partial_path = root / f"partials/shard-{index:05d}.json"
    receipt = _read_json(receipt_path, f"shard {index} receipt")
    fields = {
        "schema", "status", "execution_plan_sha256", "dispatch_plan_sha256",
        "runtime_snapshot_sha256", "execution_kind", "stage_id", "node",
        "shard_index", "task_ids_sha256", "result_count", "completion_count",
        "offline_score_eligible_count", "result_file", "partial_file",
        "compression", "logical_sha256", "compressed_sha256",
        "compressed_size_bytes", "partial_sha256", "gomaxprocs",
        "bridge_process_count", "persistent_bridge_for_all_shard_tasks",
        "heavy_execution_started", "simulator_only",
        "scientific_result_available", "deployment_allowed",
    }
    expected_heavy = plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1
    if (
        set(receipt) != fields
        or receipt.get("schema") != SHARD_RECEIPT_SCHEMA_V2
        or receipt.get("status") != "COMPLETE_SIMULATOR_ONLY_NONVOTING"
        or receipt.get("execution_plan_sha256")
        != plan["content_address"]["sha256"]
        or receipt.get("dispatch_plan_sha256")
        != dispatch["content_address"]["sha256"]
        or receipt.get("runtime_snapshot_sha256")
        != plan["runtime_snapshot_binding"]["snapshot_sha256"]
        or receipt.get("execution_kind") != plan["execution_kind"]
        or receipt.get("stage_id") != plan["stage_id"]
        or receipt.get("node") != node
        or receipt.get("shard_index") != index
        or receipt.get("task_ids_sha256") != sha256_json(shard["task_ids"])
        or receipt.get("result_count") != shard["task_count"]
        or receipt.get("result_file") != f"shards/shard-{index:05d}.jsonl.gz"
        or receipt.get("partial_file") != f"partials/shard-{index:05d}.json"
        or receipt.get("compression") != "gzip_mtime_0"
        or not isinstance(receipt.get("logical_sha256"), str)
        or len(receipt["logical_sha256"]) != 64
        or receipt.get("gomaxprocs") != 1
        or receipt.get("bridge_process_count") != 1
        or receipt.get("persistent_bridge_for_all_shard_tasks") is not True
        or receipt.get("heavy_execution_started") is not expected_heavy
        or receipt.get("simulator_only") is not True
        or receipt.get("scientific_result_available") is not False
        or receipt.get("deployment_allowed") is not False
    ):
        _fail(f"shard {index} receipt differs from its v2 assignment")
    _validate_raw_gzip(root / receipt["result_file"], receipt, index)
    partial = _read_json(partial_path, f"shard {index} partial")
    core = deepcopy(partial)
    address = _mapping(core.pop("content_address", None), "partial address")
    partial_fields = {
        "schema", "status", "execution_plan_sha256", "dispatch_plan_sha256",
        "runtime_snapshot_sha256", "stage_id", "node", "shard_index",
        "task_ids_sha256", "task_count", "task_rows",
        "full_artifacts_validated_on_shard", "raw_gzip_retained_for_audit",
        "simulator_only", "deployment_allowed",
    }
    if (
        set(core) != partial_fields
        or address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON document without content_address"
        or address.get("sha256") != sha256_json(core)
        or receipt.get("partial_sha256") != address.get("sha256")
        or core.get("schema") != SHARD_PARTIAL_SCHEMA_V2
        or core.get("status")
        != "COMPLETE_VALIDATED_SHARD_SUFFICIENT_STATISTICS"
        or core.get("execution_plan_sha256")
        != plan["content_address"]["sha256"]
        or core.get("dispatch_plan_sha256")
        != dispatch["content_address"]["sha256"]
        or core.get("runtime_snapshot_sha256")
        != plan["runtime_snapshot_binding"]["snapshot_sha256"]
        or core.get("stage_id") != plan["stage_id"]
        or core.get("node") != node
        or core.get("shard_index") != index
        or core.get("task_ids_sha256") != sha256_json(shard["task_ids"])
        or core.get("task_count") != shard["task_count"]
        or core.get("full_artifacts_validated_on_shard") is not True
        or core.get("raw_gzip_retained_for_audit") is not True
        or core.get("simulator_only") is not True
        or core.get("deployment_allowed") is not False
    ):
        _fail(f"shard {index} compact partial differs from its v2 assignment")
    raw_rows = core.get("task_rows")
    if not isinstance(raw_rows, list):
        _fail(f"shard {index} compact task rows are missing")
    expected_ids = set(str(value) for value in shard["task_ids"])
    observed_ids: set[str] = set()
    rows: list[JSONMap] = []
    required = {
        "task_id", "group_id", "policy_id", "dps", "elapsed_ms",
        "completion_mode", "completion_criterion_met", "offline_score_eligible",
        "omitted_lane_count", "fatal_error_count",
        "dynamic_runtime_receipts_complete", "rollout_row_sha256",
    }
    for raw in raw_rows:
        row = dict(_mapping(raw, "compact task row"))
        task_id = str(row.get("task_id"))
        dps = row.get("dps")
        if (
            set(row) != required
            or task_id not in expected_ids
            or task_id in observed_ids
            or not isinstance(dps, (int, float))
            or isinstance(dps, bool)
            or not math.isfinite(float(dps))
            or not isinstance(row.get("elapsed_ms"), int)
            or isinstance(row.get("elapsed_ms"), bool)
            or row["elapsed_ms"] <= 0
            or not isinstance(row.get("rollout_row_sha256"), str)
            or len(row["rollout_row_sha256"]) != 64
        ):
            _fail(f"shard {index} compact task row is invalid or duplicate")
        observed_ids.add(task_id)
        rows.append(row)
    if observed_ids != expected_ids:
        _fail(f"shard {index} compact task set differs from assignment")
    if (
        sum(int(row["completion_criterion_met"] is True) for row in rows)
        != receipt.get("completion_count")
        or sum(int(row["offline_score_eligible"] is True) for row in rows)
        != receipt.get("offline_score_eligible_count")
    ):
        _fail(f"shard {index} compact accounting differs from receipt")
    return rows


def _reduce_stage_v2_impl(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    output_directory: str | Path,
    throughput_capture: Any | None = None,
) -> JSONMap:
    """Reduce exactly one complete v2 stage; partial stages never retain."""

    with (
        throughput_capture.phase("VALIDATION")
        if throughput_capture is not None
        else nullcontext()
    ):
        try:
            plan = validate_execution_plan_v2(execution_plan)
            dispatch = validate_dispatch_plan_v2(dispatch_plan, plan)
        except FuryCatGapStageTransitionV2Error as error:
            _fail(str(error))
        development_reference_audit = build_development_reference_audit_v1(plan)
        if development_reference_audit["optimization_reference_eligible"] is not True:
            _fail("development optimization reference audit failed")
    root = Path(output_directory).expanduser().resolve()
    node_by_shard = _node_by_shard(dispatch)
    expected_task_ids = {str(row["task_id"]) for row in plan["lane_tasks"]}
    observed: dict[str, JSONMap] = {}
    for shard in plan["shards"]:
        index = int(shard["shard_index"])
        if index not in node_by_shard:
            _fail(f"shard {index} has no dispatch node")
        with (
            throughput_capture.phase("VALIDATION")
            if throughput_capture is not None
            else nullcontext()
        ):
            validated_rows = _read_validated_partial(
                root=root,
                plan=plan,
                dispatch=dispatch,
                shard=shard,
                node=node_by_shard[index],
            )
        for row in validated_rows:
            task_id = str(row["task_id"])
            if task_id in observed:
                _fail(f"duplicate lane task result: {task_id}")
            observed[task_id] = row
    if set(observed) != expected_task_ids or len(observed) != plan["task_count"]:
        _fail("observed lane task set differs from the v2 execution plan")

    group_by_id = {
        str(row["group_id"]): row
        for row in plan["runner_plan"]["contract"]["groups"]
    }
    by_group: dict[str, dict[str, JSONMap]] = defaultdict(dict)
    for row in observed.values():
        group_id = str(row["group_id"])
        policy_id = str(row["policy_id"])
        if policy_id in by_group[group_id]:
            _fail("one group contains a duplicate policy lane")
        if (
            row.get("offline_score_eligible") is not True
            or row.get("completion_criterion_met") is not True
            or row.get("omitted_lane_count") != 0
            or row.get("fatal_error_count") != 0
            or row.get("dynamic_runtime_receipts_complete") is not True
        ):
            _fail(f"lane {group_id}/{policy_id} is incomplete or noneligible")
        by_group[group_id][policy_id] = row
    expected_policies = set(plan["policy_ids"])
    if set(by_group) != set(group_by_id):
        _fail("group result set differs from runner groups")
    for group_id, rows in by_group.items():
        if set(rows) != expected_policies:
            _fail(f"group {group_id} does not contain every planned policy once")
        _validate_paired_horizon_elapsed_v3(
            [{"lane_result": row} for row in rows.values()]
        )

    candidates = list(plan["candidate_ids"])
    baselines = (CAT_POLICY_ID, CONTRA260817_POLICY_ID)
    contrasts: dict[str, JSONMap] = {}
    for candidate_id in candidates:
        for baseline_id in baselines:
            contrast_id = f"{candidate_id}__minus__{baseline_id}"
            contrasts[contrast_id] = _equal_instance_contrast(
                by_group,
                group_by_id,
                candidate_id=candidate_id,
                baseline_id=baseline_id,
            )
    rankings = []
    for candidate_id in candidates:
        means = {
            baseline_id: contrasts[f"{candidate_id}__minus__{baseline_id}"][
                "equal_instance_weighted_paired_mean_dps"
            ]
            for baseline_id in baselines
        }
        rankings.append(
            {
                "candidate_id": candidate_id,
                "equal_instance_weighted_mean_by_baseline": means,
                "dual_baseline_maximin_mean_dps": min(means.values()),
            }
        )
    rankings.sort(
        key=lambda row: (
            -float(row["dual_baseline_maximin_mean_dps"]), row["candidate_id"]
        )
    )
    kind = plan["execution_kind"]
    stage_id = plan["stage_id"]
    selection: JSONMap | None = None
    if kind == LOCAL_SMOKE_EXECUTION_KIND_V2:
        status = "LOCAL_SMOKE_COMPLETE_NO_RETENTION"
        retained: list[str] = []
        retention_allowed = False
    elif stage_id == "selection_validation":
        if len(contrasts) != 4:
            _fail("selection stage must contain four candidate-baseline contrasts")
        raw: dict[str, float] = {}
        statistics: dict[str, JSONMap] = {}
        for contrast_id, row in contrasts.items():
            statistic, statistic_kind, probability = _paired_t_test_v2(
                row["per_seed_equal_instance_weighted_dps_deltas"]
            )
            raw[contrast_id] = probability
            statistics[contrast_id] = {
                "paired_t_statistic": statistic,
                "paired_t_statistic_kind": statistic_kind,
                "paired_t_degrees_of_freedom": row["master_seed_count"] - 1,
            }
        adjusted = _holm_adjust(raw)
        for contrast_id in contrasts:
            contrasts[contrast_id].update(statistics[contrast_id])
            contrasts[contrast_id].update(adjusted[contrast_id])
        passing = []
        for candidate_id in candidates:
            ids = [f"{candidate_id}__minus__{baseline}" for baseline in baselines]
            if all(
                contrasts[contrast_id]["equal_instance_weighted_paired_mean_dps"]
                > 0.0
                and contrasts[contrast_id]["holm_reject_familywise_0_05"] is True
                for contrast_id in ids
            ):
                passing.append(candidate_id)
        selected = next(
            (row["candidate_id"] for row in rankings if row["candidate_id"] in passing),
            None,
        )
        retained = [selected] if selected is not None else []
        retention_allowed = selected is not None
        status = (
            "SELECTION_COMPLETE_POLICY_SELECTED_DEVELOPMENT_ONLY"
            if selected is not None
            else "SELECTION_COMPLETE_NO_SELECTION"
        )
        selection = {
            "contrast_family_size": 4,
            "paired_test": "two_sided_paired_student_t_over_per_seed_equal_instance_means",
            "multiplicity": "HOLM_FAMILYWISE_ALPHA_0.05",
            "passing_candidate_ids": passing,
            "selected_candidate_id": selected,
            "scientific_confirmation": False,
        }
    else:
        retained_count = plan["stage_contract"].get(
            "post_evaluation_retained_candidate_count"
        )
        if (
            not isinstance(retained_count, int)
            or isinstance(retained_count, bool)
            or retained_count <= 0
            or retained_count > len(rankings)
        ):
            _fail("successive-halving retained count is invalid")
        retained = [row["candidate_id"] for row in rankings[:retained_count]]
        retention_allowed = True
        status = "STAGE_COMPLETE_RETENTION_READY"

    core = {
        # This is intentionally the unchanged semantic reduction schema consumed
        # by the pure updater; the v2 source identity lives in its plan/receipts.
        "schema": REDUCTION_SCHEMA_V2,
        "status": status,
        "stage_id": stage_id,
        "execution_kind": kind,
        "search_plan_sha256": plan["base_search_plan_sha256"],
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "expected_task_count": plan["task_count"],
        "observed_unique_task_count": len(observed),
        "group_count": len(by_group),
        "candidate_count": len(candidates),
        "baseline_policy_ids": list(baselines),
        "development_reference_audit": development_reference_audit,
        "contrast_count": len(contrasts),
        "contrasts": contrasts,
        "ranking_metric": "dual_baseline_maximin_equal_instance_weighted_paired_mean_dps",
        "ranking_tie_break": "candidate_id_ascending",
        "candidate_ranking": rankings,
        "retained_candidate_ids": retained,
        "retention_allowed": retention_allowed,
        "selection": selection,
        "complete_accounting": True,
        "failure_status_if_any_lane_missing_duplicate_or_ineligible": (
            "STAGE_FAILED_NO_RETENTION"
        ),
        "heavy_execution_started": kind == REAL_STAGE_EXECUTION_KIND_V1,
        "simulator_only": True,
        "development_only": True,
        "old50_heldout_evidence": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def reduce_stage_v2(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    output_directory: str | Path,
    throughput_capture: Any | None = None,
) -> JSONMap:
    """Reduce a stage, optionally separating validation from analysis telemetry."""

    with (
        throughput_capture.phase("ANALYSIS")
        if throughput_capture is not None
        else nullcontext()
    ):
        return _reduce_stage_v2_impl(
            execution_plan,
            dispatch_plan,
            output_directory=output_directory,
            throughput_capture=throughput_capture,
        )


def build_local_smoke_evidence_v2(
    execution_plan: Mapping[str, Any],
    dispatch_plan: Mapping[str, Any],
    *,
    output_directory: str | Path,
    throughput_capture: Any | None = None,
) -> JSONMap:
    plan = validate_execution_plan_v2(execution_plan)
    dispatch = validate_dispatch_plan_v2(dispatch_plan, plan)
    if (
        plan["execution_kind"] != LOCAL_SMOKE_EXECUTION_KIND_V2
        or plan["task_count"] != 3
        or plan["shard_count"] != 1
        or len(plan["candidate_ids"]) != 1
        or dispatch["workers_per_node"] != 1
        or len(dispatch["nodes"]) != 1
        or dispatch["nodes"][0]["name"] != "local"
    ):
        _fail("v2 smoke evidence requires one candidate, one group, and one shard")
    reduction = reduce_stage_v2(
        plan,
        dispatch,
        output_directory=output_directory,
        throughput_capture=throughput_capture,
    )
    if (
        reduction.get("status") != "LOCAL_SMOKE_COMPLETE_NO_RETENTION"
        or reduction.get("retention_allowed") is not False
        or reduction.get("heavy_execution_started") is not False
    ):
        _fail("v2 local smoke reduction crossed a retention boundary")
    core = {
        "schema": LOCAL_SMOKE_EVIDENCE_SCHEMA_V2,
        "status": "PASS_ONE_CANDIDATE_ONE_SEED_THREE_LANE_V2_SMOKE",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "dispatch_plan_sha256": dispatch["content_address"]["sha256"],
        "reduction_sha256": reduction["content_address"]["sha256"],
        "runtime_snapshot_sha256": plan["runtime_snapshot_binding"][
            "snapshot_sha256"
        ],
        "candidate_id": plan["candidate_ids"][0],
        "master_seed": plan["stage_contract"]["smoke_master_seed"],
        "task_count": 3,
        "retention_allowed": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "deployment_allowed": False,
    }
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _atomic_write_json(path: Path | None, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if path is None:
        print(payload.decode("utf-8"), end="")
        return
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--smoke-evidence", action="store_true")
    parser.add_argument("--throughput-telemetry", type=Path)
    args = parser.parse_args(argv)
    capture: Any | None = None
    try:
        plan = _read_json(args.execution_plan, "execution plan")
        dispatch = _read_json(args.dispatch_plan, "dispatch plan")
        if args.throughput_telemetry is not None:
            from .fury_cat_gap_throughput_capture_v1 import (
                ShardThroughputCaptureV1,
            )

            stage_id = str(plan.get("stage_id", "unknown-stage"))
            capture = ShardThroughputCaptureV1(
                batch_id=f"{stage_id}:reducer",
                node="reducer",
                shard_index=0,
                workload_class=stage_id,
                logical_cpu_capacity=1,
                trace_mode="COMPACT",
            )
            root = args.output_directory.expanduser().resolve()
            input_paths = [
                args.execution_plan.expanduser().resolve(),
                args.dispatch_plan.expanduser().resolve(),
            ]
            for shard in plan.get("shards", ()):
                if not isinstance(shard, Mapping):
                    continue
                index = shard.get("shard_index")
                if not isinstance(index, int) or isinstance(index, bool):
                    continue
                input_paths.extend(
                    (
                        root / f"receipts/shard-{index:05d}.json",
                        root / f"partials/shard-{index:05d}.json",
                        root / f"shards/shard-{index:05d}.jsonl.gz",
                    )
                )
            capture.add_phase_bytes(
                "VALIDATION",
                input_bytes=sum(
                    path.stat().st_size for path in input_paths if path.is_file()
                ),
            )
        value = (
            build_local_smoke_evidence_v2(
                plan,
                dispatch,
                output_directory=args.output_directory,
                throughput_capture=capture,
            )
            if args.smoke_evidence
            else reduce_stage_v2(
                plan,
                dispatch,
                output_directory=args.output_directory,
                throughput_capture=capture,
            )
        )
        with (
            capture.phase("MERGE") if capture is not None else nullcontext()
        ):
            _atomic_write_json(args.output, value)
        if capture is not None:
            output_bytes = (
                args.output.expanduser().resolve().stat().st_size
                if args.output is not None
                else len(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
                + 1
            )
            capture.add_phase_bytes(
                "MERGE", json_bytes=output_bytes, output_bytes=output_bytes
            )
            from .fury_cat_gap_throughput_capture_v1 import (
                write_capture_document_v1,
            )

            write_capture_document_v1(args.throughput_telemetry, capture.finish())
    except Exception as error:
        if capture is not None and not capture.finished:
            try:
                from .fury_cat_gap_throughput_capture_v1 import (
                    write_capture_document_v1,
                )

                write_capture_document_v1(
                    args.throughput_telemetry, capture.finish()
                )
            except Exception as telemetry_error:
                print(f"telemetry error: {telemetry_error}", file=sys.stderr)
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "DEVELOPMENT_REFERENCE_AUDIT_BLOCKED_V1",
    "DEVELOPMENT_REFERENCE_AUDIT_READY_V1",
    "DEVELOPMENT_REFERENCE_AUDIT_SCHEMA_V1",
    "FuryCatGapHpcReducerV2Error",
    "LOCAL_SMOKE_EVIDENCE_SCHEMA_V2",
    "REDUCTION_SCHEMA_V2",
    "build_development_reference_audit_v1",
    "build_local_smoke_evidence_v2",
    "reduce_stage_v2",
)
