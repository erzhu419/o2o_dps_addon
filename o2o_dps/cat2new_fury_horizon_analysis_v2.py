"""Strict dual-baseline analysis for the frozen Cat2_new Fury horizon run.

This additive analysis validates the immutable repaired v2 confirmation, calls
the single-pass v4 reducer exactly once for each frozen arm, and evaluates the
six predeclared Cat2_new-minus-baseline contrasts as one Holm family.  A gate
result may identify a candidate for another simulator validation; it never
authorizes a live addon, a scientific claim, or deployment.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence

from .cat2new_fury_horizon_analysis_v1 import (
    _linear_type7,
    _regularized_incomplete_beta,
)
from .cat2new_candidate_feedback_loop_v6 import IMPLEMENTATION_REVISION
from .cat2new_fury_horizon_confirmation_v2 import (
    BASELINE_POLICY_IDS,
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_HORIZON_MS,
    CONFIRMATION_MASTER_SEEDS,
    FAMILYWISE_ALPHA,
    INPUT_LOCK_SCHEMA,
    NONBINDING_TARGET_HEALTH,
    PRIOR_CONFIRMATION_ID,
    SOURCE_HORIZON_MS,
    STATUS as CONFIRMATION_STATUS,
    horizon_analysis_contract_v2,
)
from .cat2new_fury_screening_v1 import FROZEN_SCREENING_ARMS_V1
from .fury_cat2_horizon_confirmation_preparation_v2 import (
    AGGREGATE_WORKERS_PER_NODE,
    CONCURRENT_ARMS,
    MANIFEST_SCHEMA,
    WORKERS_PER_NODE_PER_ARM,
)
from .fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    DEVELOPMENT_EXECUTION_KIND_V3,
    FuryMultiseedHpcDispatchV3Error,
    validate_dispatch_plan_v3,
)
from .fury_multiseed_hpc_reducer_v4 import (
    REDUCTION_SCHEMA_V4,
    STATUS_V4 as REDUCTION_STATUS_V4,
    FuryMultiseedHpcReducerV4Error,
    compact_reduction_v4,
    reduce_dispatch_v4,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    FuryPairedRunnerV4Error,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
SCHEMA = "cat2new_fury_horizon_analysis/v2"
STATUS = "COMPLETE_SYNTHETIC_DUAL_BASELINE_MECHANISM_DIAGNOSTIC_NONVOTING"
EXPECTED_PAIR_COUNT = len(CONFIRMATION_MASTER_SEEDS)
FAMILY_SIZE = len(CONFIRMATION_ARM_IDS) * len(BASELINE_POLICY_IDS)
_SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class Cat2NewFuryHorizonAnalysisV2Error(RuntimeError):
    """The frozen identity, strict reductions, or analysis family is invalid."""


def analysis_contract_v2() -> JSONMap:
    return horizon_analysis_contract_v2()


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise Cat2NewFuryHorizonAnalysisV2Error(f"{label} must be an object")
    return value


def _hash_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise Cat2NewFuryHorizonAnalysisV2Error(f"{label} must be a SHA-256")
    return value


def _validate_input_lock(value: Any) -> tuple[JSONMap, str]:
    if not isinstance(value, Mapping):
        raise Cat2NewFuryHorizonAnalysisV2Error("input_lock must be an object")
    lock = dict(value)
    expected_fields = {
        "schema",
        "source_runner_plan_sha256",
        "source_scenario_contract_sha256",
        "arm_ids",
        "master_seeds",
        "source_horizon_ms",
        "confirmation_horizon_ms",
        "nonbinding_target_health",
        "candidate_policy_id",
        "candidate_implementation_revision",
        "analysis_contract",
        "repair_lineage",
        "content_address",
    }
    if set(lock) != expected_fields or lock.get("schema") != INPUT_LOCK_SCHEMA:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "input_lock field set or schema mismatch"
        )
    core = {key: item for key, item in lock.items() if key != "content_address"}
    address = lock.get("content_address")
    if not isinstance(address, Mapping) or dict(address) != {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "input_lock content address mismatch"
        )
    exact = {
        "arm_ids": list(CONFIRMATION_ARM_IDS),
        "master_seeds": list(CONFIRMATION_MASTER_SEEDS),
        "source_horizon_ms": SOURCE_HORIZON_MS,
        "confirmation_horizon_ms": CONFIRMATION_HORIZON_MS,
        "nonbinding_target_health": NONBINDING_TARGET_HEALTH,
        "candidate_policy_id": CAT2NEW_POLICY_ID,
        "candidate_implementation_revision": IMPLEMENTATION_REVISION,
        "analysis_contract": analysis_contract_v2(),
        "repair_lineage": {
            "prior_confirmation_id": PRIOR_CONFIRMATION_ID,
            "prior_master_seed_start": 257,
            "prior_master_seed_end": 512,
            "prior_result_usable_for_policy_comparison": False,
            "defect_code": "PENDING_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
            "repair_code": "EXACT_HORIZON_ACTIVE_HARDCAST_RIGHT_CENSOR",
            "fresh_seed_family_required": True,
        },
    }
    for field, expected in exact.items():
        if lock.get(field) != expected:
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"input_lock {field} differs from the frozen contract"
            )
    _hash_text(lock.get("source_runner_plan_sha256"), "source runner plan identity")
    _hash_text(
        lock.get("source_scenario_contract_sha256"),
        "source scenario contract identity",
    )
    return lock, str(address["sha256"])


def _expected_arm_specs() -> dict[str, JSONMap]:
    registry = {row.arm_id: row.to_wire() for row in FROZEN_SCREENING_ARMS_V1}
    return {arm_id: registry[arm_id] for arm_id in CONFIRMATION_ARM_IDS}


def _validate_frozen_scenario(
    scenario: Mapping[str, Any], lock: Mapping[str, Any], arm_id: str
) -> None:
    model = scenario.get("scenario_model")
    dynamic = scenario.get("dynamic_load_config")
    if not isinstance(model, Mapping) or not isinstance(dynamic, Mapping):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"{arm_id} frozen scenario metadata is malformed"
        )
    source = model.get("source_fixture")
    mechanism = model.get("mechanism_control")
    target_health = dynamic.get("target_health")
    if (
        not isinstance(source, Mapping)
        or source.get("scenario_contract_sha256")
        != lock["source_scenario_contract_sha256"]
        or source.get("source_horizon_ms") != SOURCE_HORIZON_MS
        or not isinstance(mechanism, Mapping)
        or mechanism.get("confirmation_horizon_ms") != CONFIRMATION_HORIZON_MS
        or mechanism.get("target_health") != NONBINDING_TARGET_HEALTH
        or mechanism.get("starting_rage") != 100
        or dynamic.get("idle_advance_horizon_ms") != CONFIRMATION_HORIZON_MS
        or not isinstance(target_health, list)
        or len(target_health) != 1
        or not isinstance(target_health[0], Mapping)
        or target_health[0].get("target_index") != 0
        or target_health[0].get("health") != NONBINDING_TARGET_HEALTH
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"{arm_id} scenario differs from the repaired horizon control"
        )


def _confirmation_identity(
    manifest: Mapping[str, Any], bridge_identity: Mapping[str, Any]
) -> JSONMap:
    return {
        "schema": MANIFEST_SCHEMA,
        "input_lock_sha256": manifest["input_lock"]["content_address"]["sha256"],
        "source_closure_sha256": manifest["source_closure_sha256"],
        "source_archive_sha256": manifest["source_archive_sha256"],
        "analysis_contract_sha256": manifest["analysis_contract_sha256"],
        "bridge_identity": dict(bridge_identity),
        "workers_per_node_per_arm": manifest["workers_per_node_per_arm"],
        "concurrent_arms": manifest["concurrent_arms"],
        "arms": [
            {
                "arm_id": arm["arm_spec"]["arm_id"],
                "runner_plan_sha256": arm["runner_plan_sha256"],
                "dispatch_plan_sha256": arm["dispatch_plan_sha256"],
            }
            for arm in manifest["arms"]
        ],
    }


def validate_frozen_confirmation_identity_v2(
    manifest: Mapping[str, Any], *, run_root: str | Path
) -> dict[str, tuple[JSONMap, JSONMap]]:
    """Validate the immutable repaired v2 confirmation and all plan identities."""

    expected_fields = {
        "schema",
        "status",
        "confirmation_id",
        "run_root_name",
        "input_lock",
        "source_closure_sha256",
        "source_archive_sha256",
        "source_release_relative_path",
        "workers_per_node_per_arm",
        "concurrent_arms",
        "aggregate_workers_per_node",
        "arm_count",
        "arms",
        "analysis_contract",
        "analysis_contract_sha256",
        "execution_started",
        "heavy_execution_started",
        "simulator_only",
        "live_fidelity",
        "comparison_ready",
        "scientific_result_available",
        "deployment_allowed",
    }
    if set(manifest) != expected_fields or manifest.get("schema") != MANIFEST_SCHEMA:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "confirmation manifest field set or schema mismatch"
        )
    if (
        manifest.get("status") != CONFIRMATION_STATUS
        or manifest.get("arm_count") != len(CONFIRMATION_ARM_IDS)
        or manifest.get("workers_per_node_per_arm") != WORKERS_PER_NODE_PER_ARM
        or manifest.get("concurrent_arms") != CONCURRENT_ARMS
        or manifest.get("aggregate_workers_per_node")
        != AGGREGATE_WORKERS_PER_NODE
        or manifest.get("execution_started") is not False
        or manifest.get("heavy_execution_started") is not False
        or manifest.get("simulator_only") is not True
        or manifest.get("live_fidelity") is not False
        or manifest.get("comparison_ready") is not False
        or manifest.get("scientific_result_available") is not False
        or manifest.get("deployment_allowed") is not False
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "confirmation manifest execution or scope contract mismatch"
        )
    lock, lock_sha = _validate_input_lock(manifest.get("input_lock"))
    analysis_contract = analysis_contract_v2()
    analysis_contract_sha = sha256_json(analysis_contract)
    if (
        manifest.get("analysis_contract") != analysis_contract
        or manifest.get("analysis_contract_sha256") != analysis_contract_sha
        or lock.get("analysis_contract") != analysis_contract
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "confirmation analysis contract or identity mismatch"
        )
    source_closure = _hash_text(
        manifest.get("source_closure_sha256"), "source closure identity"
    )
    _hash_text(manifest.get("source_archive_sha256"), "source archive identity")
    if manifest.get("source_release_relative_path") != (
        f"releases/fury-multiseed-source/{source_closure}"
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "source release path differs from its closure identity"
        )

    root = Path(run_root).expanduser().resolve()
    arms = manifest.get("arms")
    if not isinstance(arms, list) or [
        row.get("arm_spec", {}).get("arm_id")
        for row in arms
        if isinstance(row, Mapping)
    ] != list(CONFIRMATION_ARM_IDS):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "confirmation arm order differs from the frozen registry"
        )
    expected_specs = _expected_arm_specs()
    validated: dict[str, tuple[JSONMap, JSONMap]] = {}
    bridge_identity: JSONMap | None = None
    scenario_bundle_sha: str | None = None
    for raw in arms:
        if not isinstance(raw, Mapping):
            raise Cat2NewFuryHorizonAnalysisV2Error("confirmation arm is malformed")
        arm = dict(raw)
        arm_id = str(arm["arm_spec"]["arm_id"])
        expected_arm_fields = {
            "arm_spec",
            "factory_config",
            "candidate_policy_identity",
            "execution_bundle_identity",
            "runner_plan_sha256",
            "dispatch_plan_sha256",
            "runner_plan_path",
            "dispatch_plan_path",
            "attempt_root_path",
        }
        if set(arm) != expected_arm_fields or arm.get("arm_spec") != expected_specs[arm_id]:
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} arm specification or field set mismatch"
            )
        if (
            arm.get("runner_plan_path") != f"{arm_id}/runner-plan.json"
            or arm.get("dispatch_plan_path") != f"{arm_id}/dispatch-plan.json"
            or arm.get("attempt_root_path") != f"{arm_id}/attempts"
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} relative path contract mismatch"
            )
        try:
            runner = validate_runner_plan(
                _read_json(root / str(arm["runner_plan_path"]), f"{arm_id} runner plan")
            )
            dispatch = validate_dispatch_plan_v3(
                _read_json(
                    root / str(arm["dispatch_plan_path"]), f"{arm_id} dispatch plan"
                ),
                runner,
            )
        except (FuryPairedRunnerV4Error, FuryMultiseedHpcDispatchV3Error) as error:
            raise Cat2NewFuryHorizonAnalysisV2Error(str(error)) from error
        if (
            runner["plan_sha256"] != arm.get("runner_plan_sha256")
            or sha256_json(dispatch) != arm.get("dispatch_plan_sha256")
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} runner or dispatch identity mismatch"
            )
        contract = runner["contract"]
        scenarios = contract.get("scenarios")
        expected_protocol = {
            "kind": "cat2new_fury_horizon_confirmation_protocol/v2",
            "input_lock_sha256": lock_sha,
            "intent": "DEVELOPMENT_DIAGNOSTIC_NONVOTING",
            "fresh_after_terminal_accounting_repair": True,
        }
        if (
            contract.get("protocol_id")
            != "cat2new-fury-horizon-confirmation-v2"
            or contract.get("protocol_sha256") != sha256_json(expected_protocol)
            or contract.get("runner_inputs_sha256") != lock_sha
            or contract.get("seed_derivation", {}).get("master_seeds")
            != list(CONFIRMATION_MASTER_SEEDS)
            or contract.get("seed_derivation", {}).get("namespace")
            != "cat2new-fury-horizon-confirmation-v2-right-censor"
            or contract.get("group_count") != EXPECTED_PAIR_COUNT
            or contract.get("expected_rollout_count")
            != EXPECTED_PAIR_COUNT * len(ALLOWED_POLICY_IDS_V3)
            or not isinstance(scenarios, list)
            or len(scenarios) != 1
            or scenarios[0].get("horizon_ms") != CONFIRMATION_HORIZON_MS
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} runner differs from the frozen 256-pair horizon"
            )
        _validate_frozen_scenario(scenarios[0], lock, arm_id)
        execution_identity = contract.get("execution_bundle_identity")
        if (
            not isinstance(execution_identity, Mapping)
            or execution_identity.get("python_source_closure_sha256")
            != source_closure
            or arm.get("execution_bundle_identity") != execution_identity
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} execution source closure mismatch"
            )
        policies = {
            row.get("policy_id"): row
            for row in contract.get("policies", [])
            if isinstance(row, Mapping)
        }
        candidate = policies.get(CAT2NEW_POLICY_ID)
        if (
            not isinstance(candidate, Mapping)
            or arm.get("candidate_policy_identity") != candidate
            or not isinstance(arm.get("factory_config"), Mapping)
            or sha256_json(dict(arm["factory_config"]))
            != candidate.get("profile_sha256")
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} candidate profile identity mismatch"
            )
        if [node.get("workers") for node in dispatch["nodes"]] != [
            WORKERS_PER_NODE_PER_ARM
        ] * 6:
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} worker budget differs from the frozen budget"
            )
        current_bridge = dict(contract["bridge_identity"])
        if bridge_identity is None:
            bridge_identity = current_bridge
        elif bridge_identity != current_bridge:
            raise Cat2NewFuryHorizonAnalysisV2Error(
                "confirmation arms use different bridge identities"
            )
        current_scenario_bundle = str(contract["runner_scenario_bundle_sha256"])
        if scenario_bundle_sha is None:
            scenario_bundle_sha = current_scenario_bundle
        elif scenario_bundle_sha != current_scenario_bundle:
            raise Cat2NewFuryHorizonAnalysisV2Error(
                "confirmation arms use different scenario bundles"
            )
        validated[arm_id] = (runner, dispatch)

    assert bridge_identity is not None
    expected_confirmation_id = sha256_json(
        _confirmation_identity(manifest, bridge_identity)
    )
    if manifest.get("confirmation_id") != expected_confirmation_id:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "confirmation_id differs from the frozen identity"
        )
    if (
        manifest.get("run_root_name") != root.name
        or not str(manifest["run_root_name"]).endswith(expected_confirmation_id)
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "run root name differs from the confirmation identity"
        )
    if lock["content_address"]["sha256"] != lock_sha:  # explicit output binding
        raise Cat2NewFuryHorizonAnalysisV2Error("input lock identity changed")
    return validated


def _paired_t_test_v2(values: Sequence[float]) -> tuple[float | None, str, float]:
    count = len(values)
    mean = math.fsum(values) / count
    squared = math.fsum((value - mean) ** 2 for value in values)
    if squared == 0.0:
        if mean == 0.0:
            return 0.0, "FINITE", 1.0
        return (
            None,
            "POSITIVE_INFINITY" if mean > 0.0 else "NEGATIVE_INFINITY",
            0.0,
        )
    sample_variance = squared / (count - 1)
    statistic = mean / math.sqrt(sample_variance / count)
    degrees_of_freedom = count - 1
    x = degrees_of_freedom / (degrees_of_freedom + statistic * statistic)
    try:
        probability = _regularized_incomplete_beta(
            degrees_of_freedom / 2.0, 0.5, x
        )
    except RuntimeError as error:
        raise Cat2NewFuryHorizonAnalysisV2Error(str(error)) from error
    return statistic, "FINITE", min(1.0, max(0.0, probability))


def _holm_adjust(raw: Mapping[str, float]) -> dict[str, JSONMap]:
    if len(raw) != FAMILY_SIZE:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "Holm family must contain exactly six contrasts"
        )
    ordered = sorted(raw.items(), key=lambda row: (row[1], row[0]))
    running = 0.0
    result: dict[str, JSONMap] = {}
    for index, (contrast_id, probability) in enumerate(ordered):
        adjusted = min(1.0, (len(ordered) - index) * probability)
        running = max(running, adjusted)
        result[contrast_id] = {
            "holm_rank": index + 1,
            "raw_two_sided_p": probability,
            "holm_adjusted_p": running,
            "holm_reject_familywise_0_05": running <= FAMILYWISE_ALPHA,
        }
    return result


def summarize_horizon_deltas_v2(
    deltas_by_arm: Mapping[str, Mapping[str, Sequence[float]]],
) -> JSONMap:
    """Summarize the exact frozen 3-arm by 2-baseline paired family."""

    if set(deltas_by_arm) != set(CONFIRMATION_ARM_IDS):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            "delta vectors must contain exactly the frozen three arm IDs"
        )
    rows_by_contrast: dict[str, JSONMap] = {}
    raw_probabilities: dict[str, float] = {}
    for arm_id in CONFIRMATION_ARM_IDS:
        by_baseline = deltas_by_arm[arm_id]
        if set(by_baseline) != set(BASELINE_POLICY_IDS):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} must contain exactly the two frozen baselines"
            )
        for baseline_id in BASELINE_POLICY_IDS:
            raw_values = by_baseline[baseline_id]
            if (
                isinstance(raw_values, (str, bytes))
                or len(raw_values) != EXPECTED_PAIR_COUNT
            ):
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} against {baseline_id} must contain exactly "
                    f"{EXPECTED_PAIR_COUNT} paired deltas"
                )
            values: list[float] = []
            for raw in raw_values:
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise Cat2NewFuryHorizonAnalysisV2Error(
                        "paired delta must be numeric"
                    )
                value = float(raw)
                if not math.isfinite(value):
                    raise Cat2NewFuryHorizonAnalysisV2Error(
                        "paired delta must be finite"
                    )
                values.append(value)
            mean = math.fsum(values) / EXPECTED_PAIR_COUNT
            sample_sd = math.sqrt(
                math.fsum((value - mean) ** 2 for value in values)
                / (EXPECTED_PAIR_COUNT - 1)
            )
            statistic, statistic_kind, probability = _paired_t_test_v2(values)
            contrast_id = f"{arm_id}::{baseline_id}"
            raw_probabilities[contrast_id] = probability
            rows_by_contrast[contrast_id] = {
                "contrast_id": contrast_id,
                "arm_id": arm_id,
                "candidate_policy_id": CAT2NEW_POLICY_ID,
                "baseline_policy_id": baseline_id,
                "paired_n": EXPECTED_PAIR_COUNT,
                "candidate_minus_baseline_mean_dps": mean,
                "candidate_minus_baseline_sample_sd_dps": sample_sd,
                "candidate_minus_baseline_standard_error_dps": (
                    sample_sd / math.sqrt(EXPECTED_PAIR_COUNT)
                ),
                "wins": sum(value > 0.0 for value in values),
                "ties": sum(value == 0.0 for value in values),
                "losses": sum(value < 0.0 for value in values),
                "p05_dps": _linear_type7(values, 0.05),
                "median_dps": _linear_type7(values, 0.5),
                "p95_dps": _linear_type7(values, 0.95),
                "paired_t_statistic": statistic,
                "paired_t_statistic_kind": statistic_kind,
                "paired_t_degrees_of_freedom": EXPECTED_PAIR_COUNT - 1,
            }
    adjusted = _holm_adjust(raw_probabilities)
    contrast_rows = [
        {**rows_by_contrast[contrast_id], **adjusted[contrast_id]}
        for arm_id in CONFIRMATION_ARM_IDS
        for baseline_id in BASELINE_POLICY_IDS
        for contrast_id in (f"{arm_id}::{baseline_id}",)
    ]
    contrast_by_key = {
        (row["arm_id"], row["baseline_policy_id"]): row for row in contrast_rows
    }
    arm_gates: list[JSONMap] = []
    for arm_id in CONFIRMATION_ARM_IDS:
        rows = [contrast_by_key[(arm_id, baseline)] for baseline in BASELINE_POLICY_IDS]
        means = [row["candidate_minus_baseline_mean_dps"] for row in rows]
        both_positive = all(value > 0.0 for value in means)
        both_reject = all(row["holm_reject_familywise_0_05"] for row in rows)
        arm_gates.append(
            {
                "arm_id": arm_id,
                "both_baseline_means_positive": both_positive,
                "both_baseline_holm_reject": both_reject,
                "selection_gate_passed": both_positive and both_reject,
                "maximin_mean_dps": min(means),
            }
        )
    arm_order = {arm_id: index for index, arm_id in enumerate(CONFIRMATION_ARM_IDS)}
    ranking = sorted(
        arm_gates,
        key=lambda row: (-row["maximin_mean_dps"], arm_order[row["arm_id"]]),
    )
    passed = [row for row in ranking if row["selection_gate_passed"]]
    selected = passed[0]["arm_id"] if passed else None
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "analysis_contract": analysis_contract_v2(),
        "contrast_results": contrast_rows,
        "arm_selection_gates": arm_gates,
        "diagnostic_maximin_ranking": [
            {"rank": index, **row} for index, row in enumerate(ranking, 1)
        ],
        "selection_gate": {
            "status": (
                "SELECTED_FOR_NEXT_SIMULATOR_DIAGNOSTIC"
                if selected is not None
                else "NO_SELECTION"
            ),
            "selected_arm_id": selected,
            "passing_arm_ids": [row["arm_id"] for row in passed],
            "selection_rule": (
                "maximin_of_two_baseline_mean_dps_then_frozen_arm_order"
            ),
            "scope": "NEXT_SIMULATOR_DIAGNOSTIC_ONLY",
        },
        "simulator_only": True,
        "mechanism_diagnostic_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "next_simulator_diagnostic_selection_performed": selected is not None,
        "live_policy_selection_performed": False,
        "policy_promotion_performed": False,
        "optional_stopping_performed": False,
        "deployment_allowed": False,
    }


def _validated_trace_vectors(
    arm_id: str,
    runner: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    reduction: Mapping[str, Any],
) -> tuple[dict[str, list[float]], dict[str, list[JSONMap]]]:
    expected = EXPECTED_PAIR_COUNT
    if (
        reduction.get("schema") != REDUCTION_SCHEMA_V4
        or reduction.get("status") != REDUCTION_STATUS_V4
        or reduction.get("runner_plan_sha256") != runner["plan_sha256"]
        or reduction.get("dispatch_plan_sha256") != sha256_json(dispatch)
        or reduction.get("execution_kind") != DEVELOPMENT_EXECUTION_KIND_V3
        or reduction.get("paired_group_count") != expected
        or reduction.get("unique_rollout_count")
        != expected * len(ALLOWED_POLICY_IDS_V3)
        or reduction.get("expected_rollout_count")
        != expected * len(ALLOWED_POLICY_IDS_V3)
        or reduction.get("policy_ids") != list(ALLOWED_POLICY_IDS_V3)
        or reduction.get("trace_baseline_policy_ids")
        != list(BASELINE_POLICY_IDS)
        or reduction.get("heavy_execution_started") is not True
        or reduction.get("simulator_only") is not True
        or reduction.get("live_fidelity") is not False
        or reduction.get("comparison_ready") is not False
        or reduction.get("scientific_result_available") is not False
        or reduction.get("deployment_allowed") is not False
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"{arm_id} v4 reduction identity or scope mismatch"
        )
    policy_stats = reduction.get("policy_sufficient_statistics")
    pair_stats = reduction.get("paired_sufficient_statistics")
    traces = reduction.get("paired_traces")
    readiness = reduction.get("contrast_readiness")
    if not all(isinstance(value, Mapping) for value in (policy_stats, pair_stats, traces, readiness)):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"{arm_id} v4 reduction evidence is malformed"
        )
    if set(traces) != {"candidate_minus_cat", "candidate_minus_contra260817"}:
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"{arm_id} v4 reduction does not contain the exact two paired traces"
        )
    for policy_id in (CAT2NEW_POLICY_ID, *BASELINE_POLICY_IDS):
        stats = policy_stats.get(policy_id)
        if (
            not isinstance(stats, Mapping)
            or stats.get("rollout_count") != expected
            or stats.get("completion_count") != expected
            or stats.get("offline_score_eligible_count") != expected
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} policy {policy_id} lacks 256 complete eligible rows"
            )
    expected_groups = {
        str(row["group_id"]): row for row in runner["contract"]["groups"]
    }
    values_by_baseline: dict[str, list[float]] = {}
    trace_by_baseline: dict[str, list[JSONMap]] = {}
    for baseline_id, label in (
        (CAT_POLICY_ID, "candidate_minus_cat"),
        (CONTRA260817_POLICY_ID, "candidate_minus_contra260817"),
    ):
        pair = pair_stats.get(label)
        rows = traces.get(label)
        ready = readiness.get(label)
        if (
            not isinstance(pair, Mapping)
            or pair.get("pair_count") != expected
            or pair.get("both_offline_score_eligible_count") != expected
            or not isinstance(ready, Mapping)
            or ready.get("candidate_policy_id") != CAT2NEW_POLICY_ID
            or ready.get("baseline_policy_id") != baseline_id
            or ready.get("required_pair_count") != expected
            or ready.get("observed_pair_count") != expected
            or ready.get("complete_eligible_pair_count") != expected
            or ready.get("analysis_eligible") is not True
            or not isinstance(rows, list)
            or len(rows) != expected
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} contrast {label} lacks 256 eligible pairs"
            )
        observed_ids: list[str] = []
        values: list[float] = []
        checked_rows: list[JSONMap] = []
        expected_fields = {
            "group_id",
            "master_seed",
            "simulator_seed",
            "candidate_policy_id",
            "baseline_policy_id",
            "candidate_dps",
            "baseline_dps",
            "candidate_minus_baseline_dps",
        }
        for raw in rows:
            if not isinstance(raw, Mapping) or set(raw) != expected_fields:
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} contrast trace row is malformed"
                )
            row = dict(raw)
            group_id = str(row["group_id"])
            group = expected_groups.get(group_id)
            if (
                group is None
                or row.get("master_seed") != group["master_seed"]
                or row.get("simulator_seed") != group["simulator_seed"]
                or row.get("candidate_policy_id") != CAT2NEW_POLICY_ID
                or row.get("baseline_policy_id") != baseline_id
            ):
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} contrast trace identity mismatch"
                )
            try:
                candidate_dps = float(row["candidate_dps"])
                baseline_dps = float(row["baseline_dps"])
                delta = float(row["candidate_minus_baseline_dps"])
            except (TypeError, ValueError) as error:
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} contrast trace contains nonnumeric DPS"
                ) from error
            if not all(math.isfinite(value) for value in (candidate_dps, baseline_dps, delta)):
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} contrast trace contains nonfinite DPS"
                )
            if not math.isclose(
                candidate_dps - baseline_dps, delta, rel_tol=0.0, abs_tol=1e-12
            ):
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} contrast trace delta mismatch"
                )
            observed_ids.append(group_id)
            values.append(delta)
            checked_rows.append(row)
        if observed_ids != sorted(expected_groups):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} contrast trace group order or set mismatch"
            )
        if not (
            math.isclose(
                math.fsum(values),
                float(pair["dps_delta_sum"]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                math.fsum(value * value for value in values),
                float(pair["dps_delta_squared_sum"]),
                rel_tol=0.0,
                abs_tol=1e-7,
            )
        ):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                f"{arm_id} contrast trace differs from its strict reduction"
            )
        values_by_baseline[baseline_id] = values
        trace_by_baseline[baseline_id] = checked_rows
    cat_rows = trace_by_baseline[CAT_POLICY_ID]
    contra_rows = trace_by_baseline[CONTRA260817_POLICY_ID]
    if any(
        cat_row["group_id"] != contra_row["group_id"]
        or cat_row["candidate_dps"] != contra_row["candidate_dps"]
        for cat_row, contra_row in zip(cat_rows, contra_rows)
    ):
        raise Cat2NewFuryHorizonAnalysisV2Error(
            f"{arm_id} dual-baseline traces do not share one candidate lane"
        )
    return values_by_baseline, trace_by_baseline


def analyze_confirmation_run_v2(
    manifest: Mapping[str, Any],
    *,
    run_root: str | Path,
    arm_output_directories: Mapping[str, str | Path],
    reducer: Callable[..., Mapping[str, Any]] = reduce_dispatch_v4,
) -> JSONMap:
    """Run exactly one strict v4 reduction per arm, then analyze six contrasts."""

    try:
        plans = validate_frozen_confirmation_identity_v2(
            manifest, run_root=run_root
        )
        if set(arm_output_directories) != set(CONFIRMATION_ARM_IDS):
            raise Cat2NewFuryHorizonAnalysisV2Error(
                "arm output directories must contain exactly the frozen three arm IDs"
            )
        reductions: dict[str, JSONMap] = {}
        deltas: dict[str, dict[str, list[float]]] = {}
        traces: dict[str, dict[str, list[JSONMap]]] = {}
        for arm_id in CONFIRMATION_ARM_IDS:
            runner, dispatch = plans[arm_id]
            raw_reduction = reducer(
                runner,
                dispatch,
                output_directory=arm_output_directories[arm_id],
            )
            if not isinstance(raw_reduction, Mapping):
                raise Cat2NewFuryHorizonAnalysisV2Error(
                    f"{arm_id} reducer returned a non-object"
                )
            reduction = dict(raw_reduction)
            deltas[arm_id], traces[arm_id] = _validated_trace_vectors(
                arm_id, runner, dispatch, reduction
            )
            reductions[arm_id] = reduction
        summary = summarize_horizon_deltas_v2(deltas)
        summary["confirmation_id"] = manifest["confirmation_id"]
        summary["input_lock_sha256"] = manifest["input_lock"]["content_address"][
            "sha256"
        ]
        summary["source_closure_sha256"] = manifest["source_closure_sha256"]
        summary["analysis_contract_sha256"] = manifest[
            "analysis_contract_sha256"
        ]
        summary["analysis_contract_frozen_by_confirmation_identity"] = True
        summary["source_reductions"] = {
            arm_id: compact_reduction_v4(reductions[arm_id])
            for arm_id in CONFIRMATION_ARM_IDS
        }
        summary["paired_trace"] = traces
        return summary
    except Cat2NewFuryHorizonAnalysisV2Error:
        raise
    except (
        FuryMultiseedHpcReducerV4Error,
        FuryPairedRunnerV4Error,
        FuryMultiseedHpcDispatchV3Error,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise Cat2NewFuryHorizonAnalysisV2Error(str(error)) from error


def compact_analysis_v2(value: Mapping[str, Any]) -> JSONMap:
    result = dict(value)
    result.pop("paired_trace", None)
    return result


def _parse_arm_outputs(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        arm_id, separator, raw_path = value.partition("=")
        if not separator or not arm_id or not raw_path or arm_id in result:
            raise Cat2NewFuryHorizonAnalysisV2Error(
                "--arm-output values must be unique ARM_ID=PATH pairs"
            )
        result[arm_id] = Path(raw_path)
    return result


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--arm-output", action="append", default=[], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compact-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = analyze_confirmation_run_v2(
            _read_json(args.manifest, "confirmation manifest"),
            run_root=args.run_root,
            arm_output_directories=_parse_arm_outputs(args.arm_output),
        )
        _write_json(args.output, result)
        _write_json(args.compact_output, compact_analysis_v2(result))
        return 0
    except (Cat2NewFuryHorizonAnalysisV2Error, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "BASELINE_POLICY_IDS",
    "Cat2NewFuryHorizonAnalysisV2Error",
    "FAMILY_SIZE",
    "SCHEMA",
    "STATUS",
    "analysis_contract_v2",
    "analyze_confirmation_run_v2",
    "compact_analysis_v2",
    "summarize_horizon_deltas_v2",
    "validate_frozen_confirmation_identity_v2",
)
