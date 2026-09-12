"""Frozen eight-arm Cat2_new Fury diagnostic screening preparation.

This module only rebuilds content-addressed runner-v4/dispatch-v3 documents and
summarizes already-produced small reduction files.  It never launches a worker
or turns simulator diagnostics into a scientific or deployment claim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .fury_execution_source_identity_v2 import (
    build_fury_execution_source_identity_v2,
)
from .fury_multiseed_hpc_dispatch_v3 import (
    build_development_dispatch_plan_v3,
)
from .fury_multiseed_hpc_reducer_v3 import REDUCTION_SCHEMA_V3
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    build_runner_plan,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "cat2new_fury_screening/v1"
SUMMARY_SCHEMA = "cat2new_fury_screening_reduction_summary/v1"
FACTORY_MODULE = "o2o_dps.cat2new_fury_parametric_policy_v1"
EXPECTED_SEED_COUNT = 256

# These entrypoints define the actual remote Python execution and reduction
# closure.  The local-only screening plan generator is intentionally excluded.
EXECUTION_ENTRYPOINTS: tuple[str, ...] = (
    "o2o_dps/cat2new_fury_horizon_analysis_v1.py",
    "o2o_dps/cat2new_fury_parametric_policy_v1.py",
    "o2o_dps/fury_multiseed_hpc_dispatch_v3.py",
    "o2o_dps/fury_multiseed_hpc_worker_v3.py",
    "o2o_dps/fury_multiseed_hpc_reducer_v3.py",
)


class Cat2NewFuryScreeningV1Error(RuntimeError):
    """The frozen screening identity or an input artifact is invalid."""


@dataclass(frozen=True)
class ScreeningArmV1:
    arm_id: str
    factory_name: str
    single_target_priority: str
    filler: str
    two_hand_slam_mode: str

    @property
    def factory_path(self) -> str:
        return f"{FACTORY_MODULE}:{self.factory_name}"

    def to_wire(self) -> JSONMap:
        return {
            "arm_id": self.arm_id,
            "factory_path": self.factory_path,
            "factors": {
                "single_target_priority": self.single_target_priority,
                "filler": self.filler,
                "two_hand_slam_mode": self.two_hand_slam_mode,
            },
        }


# Frozen 2 x 2 x 2 registry.  Tuple order is part of the screening identity.
FROZEN_SCREENING_ARMS_V1: tuple[ScreeningArmV1, ...] = (
    ScreeningArmV1("bt_wait_disabled", "build_policy_bt_wait_no_slam", "BLOODTHIRST_FIRST", "WAIT", "DISABLED"),
    ScreeningArmV1("bt_hamstring_disabled", "build_policy_bt_hamstring_no_slam", "BLOODTHIRST_FIRST", "HAMSTRING", "DISABLED"),
    ScreeningArmV1("ww_wait_disabled", "build_policy_ww_wait_no_slam", "WHIRLWIND_FIRST", "WAIT", "DISABLED"),
    ScreeningArmV1("ww_hamstring_disabled", "build_policy_ww_hamstring_no_slam", "WHIRLWIND_FIRST", "HAMSTRING", "DISABLED"),
    ScreeningArmV1("bt_wait_cat_timing", "build_policy_bt_wait_cat_slam", "BLOODTHIRST_FIRST", "WAIT", "CAT_TIMING"),
    ScreeningArmV1("bt_hamstring_cat_timing", "build_policy_bt_hamstring_cat_slam", "BLOODTHIRST_FIRST", "HAMSTRING", "CAT_TIMING"),
    ScreeningArmV1("ww_wait_cat_timing", "build_policy_ww_wait_cat_slam", "WHIRLWIND_FIRST", "WAIT", "CAT_TIMING"),
    ScreeningArmV1("ww_hamstring_cat_timing", "build_policy_ww_hamstring_cat_slam", "WHIRLWIND_FIRST", "HAMSTRING", "CAT_TIMING"),
)


def _file_sha256(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise Cat2NewFuryScreeningV1Error(f"could not read {path}: {error}") from error
    return hashlib.sha256(payload).hexdigest()


def _factory_config(arm: ScreeningArmV1) -> JSONMap:
    module = importlib.import_module(FACTORY_MODULE)
    factory = getattr(module, arm.factory_name, None)
    if not callable(factory):
        raise Cat2NewFuryScreeningV1Error(
            f"frozen factory is unavailable: {arm.factory_path}"
        )
    policy = factory()
    if getattr(policy, "policy_id", None) != CAT2NEW_POLICY_ID:
        raise Cat2NewFuryScreeningV1Error(
            f"factory {arm.factory_name} does not produce the Cat2_new lane"
        )
    config = asdict(policy.config)
    parameters = config.get("parameters")
    expected = {
        "single_target_priority": arm.single_target_priority,
        "filler": arm.filler,
        "two_hand_slam_mode": arm.two_hand_slam_mode,
    }
    if not isinstance(parameters, Mapping) or any(
        parameters.get(field) != value for field, value in expected.items()
    ):
        raise Cat2NewFuryScreeningV1Error(
            f"factory {arm.factory_name} differs from its frozen factor registry"
        )
    if parameters.get("use_death_wish") is not False:
        raise Cat2NewFuryScreeningV1Error(
            f"factory {arm.factory_name} enabled the unsupported Death Wish lane"
        )
    return config


def _source_rows(source_identity: Mapping[str, Any]) -> dict[str, JSONMap]:
    rows = source_identity.get("files")
    if not isinstance(rows, list):
        raise Cat2NewFuryScreeningV1Error("source identity has no file rows")
    return {
        str(row["relative_path"]): dict(row)
        for row in rows
        if isinstance(row, Mapping) and "relative_path" in row
    }


def _required_source_sha(rows: Mapping[str, Mapping[str, Any]], path: str) -> str:
    row = rows.get(path)
    value = row.get("sha256") if isinstance(row, Mapping) else None
    if not isinstance(value, str) or len(value) != 64:
        raise Cat2NewFuryScreeningV1Error(
            f"execution source closure lacks {path}"
        )
    return value


def _arm_policy_identity(
    arm: ScreeningArmV1,
    config: Mapping[str, Any],
    source_rows: Mapping[str, Mapping[str, Any]],
) -> JSONMap:
    policy_source = _required_source_sha(
        source_rows, "o2o_dps/cat2new_fury_parametric_policy_v1.py"
    )
    adapter_source = _required_source_sha(
        source_rows, "o2o_dps/cat2new_fury_paired_lane_adapter_v3.py"
    )
    # Worker-v3 directly hashes the concrete policy and adapter files.  The
    # factory distinction belongs in profile_sha256, which worker-v3 recomputes
    # from policy.config.
    return {
        "policy_id": CAT2NEW_POLICY_ID,
        "source_sha256": policy_source,
        "adapter_sha256": adapter_source,
        "profile_sha256": sha256_json(dict(config)),
        "role": "CANDIDATE",
    }


def _execution_bundle_identity(
    *,
    source_identity: Mapping[str, Any],
    template_bundle: Mapping[str, Any],
    evaluation_relative_path: str = "o2o_dps/fury_multiseed_hpc_reducer_v3.py",
) -> JSONMap:
    rows = _source_rows(source_identity)
    canonical = source_identity.get("canonical_bundle")
    file_closure_sha = (
        canonical.get("sha256") if isinstance(canonical, Mapping) else None
    )
    if not isinstance(file_closure_sha, str):
        raise Cat2NewFuryScreeningV1Error("source identity lacks its bundle SHA-256")
    runtime_snapshot = template_bundle.get("runtime_snapshot_sha256")
    if not isinstance(runtime_snapshot, str) or len(runtime_snapshot) != 64:
        raise Cat2NewFuryScreeningV1Error(
            "template execution bundle lacks runtime snapshot identity"
        )
    return {
        "python_source_closure_sha256": file_closure_sha,
        "ordered_sink_executor_sha256": _required_source_sha(
            rows, "o2o_dps/cat2new_candidate_simulator_executor_v5.py"
        ),
        "full_policy_rollout_executor_sha256": _required_source_sha(
            rows, "o2o_dps/cat2new_candidate_feedback_loop_v6.py"
        ),
        "paired_runner_source_sha256": _required_source_sha(
            rows, "o2o_dps/fury_paired_multiseed_runner_v4.py"
        ),
        "evaluation_source_sha256": _required_source_sha(
            rows, evaluation_relative_path
        ),
        "runtime_snapshot_sha256": runtime_snapshot,
    }


def build_screening_plans_v1(
    template_runner_plan: Mapping[str, Any],
    *,
    bridge_path: str | Path,
    bridge_platform: str,
    bridge_build_id: str,
    workers_per_node: int,
    project_root: str | Path = PROJECT_ROOT,
    execution_entrypoints: Sequence[str] = EXECUTION_ENTRYPOINTS,
    evaluation_relative_path: str = "o2o_dps/fury_multiseed_hpc_reducer_v3.py",
) -> JSONMap:
    """Rebuild eight independent 256-seed runner and dispatch documents."""

    try:
        template = validate_runner_plan(template_runner_plan)
    except Exception as error:
        raise Cat2NewFuryScreeningV1Error(
            f"template runner-v4 plan is invalid: {error}"
        ) from error
    contract = template["contract"]
    bridge = Path(bridge_path).expanduser().resolve()
    if not bridge.is_file():
        raise Cat2NewFuryScreeningV1Error(f"bridge binary is missing: {bridge}")
    if not bridge_platform.strip() or not bridge_build_id.strip():
        raise Cat2NewFuryScreeningV1Error(
            "bridge platform and build ID must be explicit"
        )
    bridge_identity = {
        "sha256": _file_sha256(bridge),
        "platform": bridge_platform,
        "size_bytes": bridge.stat().st_size,
        "build_id": bridge_build_id,
    }
    source_identity = build_fury_execution_source_identity_v2(
        project_root=project_root,
        required_relative_paths=execution_entrypoints,
    )
    source_rows = _source_rows(source_identity)
    template_policies = [dict(row) for row in contract["policies"]]
    if sum(row.get("policy_id") == CAT2NEW_POLICY_ID for row in template_policies) != 1:
        raise Cat2NewFuryScreeningV1Error(
            "template must contain exactly one Cat2_new candidate lane"
        )

    arm_rows: list[JSONMap] = []
    for arm in FROZEN_SCREENING_ARMS_V1:
        config = _factory_config(arm)
        candidate_identity = _arm_policy_identity(arm, config, source_rows)
        policies = [
            candidate_identity if row["policy_id"] == CAT2NEW_POLICY_ID else row
            for row in template_policies
        ]
        execution_bundle = _execution_bundle_identity(
            source_identity=source_identity,
            template_bundle=contract["execution_bundle_identity"],
            evaluation_relative_path=evaluation_relative_path,
        )
        plan = build_runner_plan(
            protocol_id=contract["protocol_id"],
            protocol_sha256=contract["protocol_sha256"],
            phase=contract["phase"],
            corpus_manifest_sha256=contract["corpus_manifest_sha256"],
            runner_inputs_sha256=contract["runner_inputs_sha256"],
            runner_scenario_bundle_sha256=contract["runner_scenario_bundle_sha256"],
            corpus_binding_sha256=contract["corpus_binding_sha256"],
            master_seeds=contract["seed_derivation"]["master_seeds"],
            scenarios=contract["scenarios"],
            policies=policies,
            shard_count=contract["shard_count"],
            bridge_identity=bridge_identity,
            execution_bundle_identity=execution_bundle,
            execution_mode=contract["execution_mode"],
            seed_namespace=contract["seed_derivation"]["namespace"],
            plan_intent=contract["plan_intent"],
            lane_contracts=contract["lane_contracts"],
        )
        try:
            dispatch = build_development_dispatch_plan_v3(
                plan, workers_per_node=workers_per_node
            )
        except Exception as error:
            raise Cat2NewFuryScreeningV1Error(
                f"arm {arm.arm_id} dispatch is invalid: {error}"
            ) from error
        arm_rows.append(
            {
                "arm_spec": arm.to_wire(),
                "factory_config": config,
                "candidate_policy_identity": candidate_identity,
                "execution_bundle_identity": execution_bundle,
                "runner_plan": plan,
                "dispatch_plan": dispatch,
            }
        )

    registry_wire = [arm.to_wire() for arm in FROZEN_SCREENING_ARMS_V1]
    return {
        "schema": SCHEMA,
        "status": "PREPARED_DIAGNOSTIC_NOT_EXECUTED",
        "registry_sha256": sha256_json(registry_wire),
        "arm_count": len(arm_rows),
        "arms": arm_rows,
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


def _read_reduction(path: Path, arm_id: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Cat2NewFuryScreeningV1Error(
            f"could not read reduction for {arm_id}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise Cat2NewFuryScreeningV1Error(
            f"reduction for {arm_id} must be an object"
        )
    return value


def summarize_screening_reductions_v1(
    reduction_paths: Mapping[str, str | Path],
) -> JSONMap:
    """Rank only fully eligible candidate-vs-Cat 256-pair diagnostics."""

    expected_ids = tuple(arm.arm_id for arm in FROZEN_SCREENING_ARMS_V1)
    if set(reduction_paths) != set(expected_ids):
        raise Cat2NewFuryScreeningV1Error(
            "reduction paths must contain exactly the frozen eight arm IDs"
        )
    arm_rows: list[JSONMap] = []
    for arm_id in expected_ids:
        reduction = _read_reduction(Path(reduction_paths[arm_id]), arm_id)
        if (
            reduction.get("schema") != REDUCTION_SCHEMA_V3
            or reduction.get("status")
            != "COMPLETE_DIAGNOSTIC_SIMULATOR_ONLY_NONVOTING"
        ):
            raise Cat2NewFuryScreeningV1Error(
                f"reduction for {arm_id} is not a complete v3 diagnostic"
            )
        by_policy = reduction.get("policy_sufficient_statistics")
        paired = reduction.get("paired_sufficient_statistics")
        if not isinstance(by_policy, Mapping) or not isinstance(paired, Mapping):
            raise Cat2NewFuryScreeningV1Error(
                f"reduction for {arm_id} lacks sufficient statistics"
            )
        candidate = by_policy.get(CAT2NEW_POLICY_ID)
        cat = by_policy.get(CAT_POLICY_ID)
        pair = paired.get("candidate_minus_cat")
        if not all(isinstance(row, Mapping) for row in (candidate, cat, pair)):
            raise Cat2NewFuryScreeningV1Error(
                f"reduction for {arm_id} lacks candidate/Cat statistics"
            )
        eligibility_checks = {
            "candidate_rollout_count_256": candidate.get("rollout_count") == EXPECTED_SEED_COUNT,
            "candidate_completion_count_256": candidate.get("completion_count") == EXPECTED_SEED_COUNT,
            "candidate_eligible_count_256": candidate.get("offline_score_eligible_count") == EXPECTED_SEED_COUNT,
            "cat_rollout_count_256": cat.get("rollout_count") == EXPECTED_SEED_COUNT,
            "cat_completion_count_256": cat.get("completion_count") == EXPECTED_SEED_COUNT,
            "cat_eligible_count_256": cat.get("offline_score_eligible_count") == EXPECTED_SEED_COUNT,
            "paired_count_256": pair.get("pair_count") == EXPECTED_SEED_COUNT,
            "both_eligible_count_256": pair.get("both_offline_score_eligible_count") == EXPECTED_SEED_COUNT,
        }
        eligible = all(eligibility_checks.values())
        delta_sum = pair.get("dps_delta_sum")
        if isinstance(delta_sum, bool) or not isinstance(delta_sum, (int, float)):
            raise Cat2NewFuryScreeningV1Error(
                f"reduction for {arm_id} has invalid paired DPS sum"
            )
        arm_rows.append(
            {
                "arm_id": arm_id,
                "eligible_for_diagnostic_ranking": eligible,
                "eligibility_checks": eligibility_checks,
                "paired_n": EXPECTED_SEED_COUNT if eligible else None,
                "candidate_minus_cat_mean_dps": (
                    float(delta_sum) / EXPECTED_SEED_COUNT if eligible else None
                ),
                "source_status": reduction["status"],
                "source_formal_comparison_status": reduction.get(
                    "formal_comparison_status"
                ),
                "source_dynamic_runtime_receipts_complete": reduction.get(
                    "dynamic_runtime_receipts_complete"
                ),
                "source_live_fidelity": reduction.get("live_fidelity"),
                "source_comparison_ready": reduction.get("comparison_ready"),
                "source_scientific_result_available": reduction.get(
                    "scientific_result_available"
                ),
            }
        )
    ranked = sorted(
        (row for row in arm_rows if row["eligible_for_diagnostic_ranking"]),
        key=lambda row: (-row["candidate_minus_cat_mean_dps"], row["arm_id"]),
    )
    ranking = [
        {"rank": index, **row}
        for index, row in enumerate(ranked, start=1)
    ]
    excluded = [
        row for row in arm_rows if not row["eligible_for_diagnostic_ranking"]
    ]
    return {
        "schema": SUMMARY_SCHEMA,
        "status": "COMPLETE_DIAGNOSTIC_SCREENING_SUMMARY_NONVOTING",
        "expected_arm_count": len(expected_ids),
        "ranked_arm_count": len(ranking),
        "ranking": ranking,
        "excluded_arms": excluded,
        "ranking_metric": "paired_candidate_minus_cat_mean_dps",
        "required_pairs_per_arm": EXPECTED_SEED_COUNT,
        "statistical_test_performed": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


__all__: Sequence[str] = (
    "Cat2NewFuryScreeningV1Error",
    "EXECUTION_ENTRYPOINTS",
    "FROZEN_SCREENING_ARMS_V1",
    "SCHEMA",
    "SUMMARY_SCHEMA",
    "ScreeningArmV1",
    "build_screening_plans_v1",
    "summarize_screening_reductions_v1",
)
