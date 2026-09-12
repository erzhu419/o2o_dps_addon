"""Freeze the repaired dual-baseline Cat2_new Fury horizon confirmation.

The v1 execution exposed a policy-dependent terminal accounting defect: an
accepted Slam that was still casting at exactly the scoring horizon remained
in the pending ledger.  This additive contract keeps the same three policy
arms and synthetic 20.001-second mechanism control, binds the repaired Cat2
implementation, and uses a fresh 256-seed family.  Cat and Contra260817 are
both predeclared baselines in one six-contrast Holm family.

This module prepares plans only.  It does not launch workers and cannot
authorize a live addon, a scientific claim, or deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .cat2new_candidate_feedback_loop_v6 import IMPLEMENTATION_REVISION
from .cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_HORIZON_MS,
    NONBINDING_TARGET_HEALTH,
    SOURCE_HORIZON_MS,
    extend_screening_scenario_v1,
)
from .cat2new_fury_screening_v1 import build_screening_plans_v1
from .cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
SCHEMA = "cat2new_fury_horizon_confirmation/v2"
INPUT_LOCK_SCHEMA = "cat2new_fury_horizon_confirmation_input_lock/v2"
STATUS = "PREPARED_REPAIRED_SYNTHETIC_DUAL_BASELINE_DIAGNOSTIC_NOT_EXECUTED"
CONFIRMATION_MASTER_SEEDS = tuple(range(513, 769))
CONFIRMATION_ARM_IDS = (
    "ww_hamstring_cat_timing",
    "ww_wait_cat_timing",
    "bt_hamstring_cat_timing",
)
BASELINE_POLICY_IDS = (CAT_POLICY_ID, CONTRA260817_POLICY_ID)
FAMILYWISE_ALPHA = 0.05
PRIOR_CONFIRMATION_ID = (
    "3090403318a678968be3f4428cbdd15f8375b8b8615d4aa8ac2dd5041b7acff4"
)

# These are the roots of the exact source tree shipped to the workers and the
# single-pass reducer/analysis node.  Recursive local imports are added by the
# source-identity builder.
EXECUTION_ENTRYPOINTS_V2: tuple[str, ...] = (
    "o2o_dps/cat2new_fury_horizon_analysis_v2.py",
    "o2o_dps/cat2new_fury_parametric_policy_v1.py",
    "o2o_dps/fury_multiseed_hpc_dispatch_v3.py",
    "o2o_dps/fury_multiseed_hpc_worker_v3.py",
    "o2o_dps/fury_multiseed_hpc_reducer_v4.py",
)


class Cat2NewFuryHorizonConfirmationV2Error(RuntimeError):
    """The repaired horizon confirmation contract was violated."""


def horizon_analysis_contract_v2() -> JSONMap:
    """Return the frozen six-contrast decision contract."""

    return {
        "baseline_policy_ids": list(BASELINE_POLICY_IDS),
        "primary_metric": "paired_candidate_minus_baseline_mean_dps",
        "paired_test": "two_sided_paired_student_t",
        "multiplicity_method": "HOLM",
        "multiplicity_family": "three_arms_by_two_baselines",
        "multiplicity_family_size": len(CONFIRMATION_ARM_IDS)
        * len(BASELINE_POLICY_IDS),
        "familywise_alpha": FAMILYWISE_ALPHA,
        "required_complete_eligible_pairs_per_contrast": len(
            CONFIRMATION_MASTER_SEEDS
        ),
        "distribution_diagnostics": ["wins_ties_losses", "p05", "median", "p95"],
        "quantile_method": "linear_hyndman_fan_type_7",
        "tie_rule": "candidate_minus_baseline_dps_exactly_zero",
        "arm_pass_rule": "both_baseline_means_positive_and_both_holm_reject",
        "selection_rule": "maximin_of_two_baseline_mean_dps_then_frozen_arm_order",
        "no_passing_arm_result": "NO_SELECTION",
        "optional_stopping_allowed": False,
        "same_seed_extension_or_retuning_allowed": False,
        "selection_scope": "NEXT_SIMULATOR_DIAGNOSTIC_ONLY",
        "zero_variance_t_statistic_encoding": (
            "null_with_POSITIVE_INFINITY_or_NEGATIVE_INFINITY_kind"
        ),
    }


def build_horizon_confirmation_v2(
    template_runner_plan: Mapping[str, Any],
    *,
    bridge_path: str | Path,
    bridge_platform: str,
    bridge_build_id: str,
    workers_per_node: int,
    project_root: str | Path,
    execution_entrypoints: Sequence[str] = EXECUTION_ENTRYPOINTS_V2,
) -> JSONMap:
    """Prepare the three repaired fresh-seed runner/dispatch pairs."""

    try:
        template = validate_runner_plan(template_runner_plan)
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV2Error(
            f"template runner plan is invalid: {error}"
        ) from error
    contract = template["contract"]
    scenarios = contract.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 1:
        raise Cat2NewFuryHorizonConfirmationV2Error(
            "horizon confirmation requires exactly one source scenario"
        )
    try:
        scenario = extend_screening_scenario_v1(scenarios[0])
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV2Error(
            f"could not extend the frozen mechanism-control scenario: {error}"
        ) from error

    analysis_contract = horizon_analysis_contract_v2()
    input_lock_core: JSONMap = {
        "schema": INPUT_LOCK_SCHEMA,
        "source_runner_plan_sha256": template["plan_sha256"],
        "source_scenario_contract_sha256": scenarios[0][
            "scenario_contract_sha256"
        ],
        "arm_ids": list(CONFIRMATION_ARM_IDS),
        "master_seeds": list(CONFIRMATION_MASTER_SEEDS),
        "source_horizon_ms": SOURCE_HORIZON_MS,
        "confirmation_horizon_ms": CONFIRMATION_HORIZON_MS,
        "nonbinding_target_health": NONBINDING_TARGET_HEALTH,
        "candidate_policy_id": CAT2NEW_POLICY_ID,
        "candidate_implementation_revision": IMPLEMENTATION_REVISION,
        "analysis_contract": analysis_contract,
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
    input_lock = {
        **input_lock_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(input_lock_core),
        },
    }
    protocol = {
        "kind": "cat2new_fury_horizon_confirmation_protocol/v2",
        "input_lock_sha256": input_lock["content_address"]["sha256"],
        "intent": "DEVELOPMENT_DIAGNOSTIC_NONVOTING",
        "fresh_after_terminal_accounting_repair": True,
    }
    lane_contracts = [
        cat_runner_v4_lane_contract_v6()
        if row.get("policy_id") == CAT_POLICY_ID
        else dict(row)
        for row in contract["lane_contracts"]
    ]
    base_plan = build_runner_plan(
        protocol_id="cat2new-fury-horizon-confirmation-v2",
        protocol_sha256=sha256_json(protocol),
        phase="development",
        corpus_manifest_sha256=sha256_json(
            {"kind": "single-synthetic-mechanism-control-v2", "scenario": scenario}
        ),
        runner_inputs_sha256=input_lock["content_address"]["sha256"],
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
        corpus_binding_sha256=sha256_json(
            {
                "source_scenario_contract_sha256": scenarios[0][
                    "scenario_contract_sha256"
                ],
                "extended_scenario_contract_sha256": scenario[
                    "scenario_contract_sha256"
                ],
                "terminal_accounting_revision": IMPLEMENTATION_REVISION,
            }
        ),
        master_seeds=CONFIRMATION_MASTER_SEEDS,
        scenarios=[scenario],
        policies=contract["policies"],
        shard_count=6,
        bridge_identity=contract["bridge_identity"],
        execution_bundle_identity=contract["execution_bundle_identity"],
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace="cat2new-fury-horizon-confirmation-v2-right-censor",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=lane_contracts,
    )
    try:
        all_arms = build_screening_plans_v1(
            base_plan,
            bridge_path=bridge_path,
            bridge_platform=bridge_platform,
            bridge_build_id=bridge_build_id,
            workers_per_node=workers_per_node,
            project_root=project_root,
            execution_entrypoints=execution_entrypoints,
            evaluation_relative_path="o2o_dps/fury_multiseed_hpc_reducer_v4.py",
        )
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV2Error(
            f"could not build repaired screening arms: {error}"
        ) from error
    by_id = {row["arm_spec"]["arm_id"]: row for row in all_arms["arms"]}
    try:
        selected = [by_id[arm_id] for arm_id in CONFIRMATION_ARM_IDS]
    except KeyError as error:
        raise Cat2NewFuryHorizonConfirmationV2Error(
            f"frozen arm is unavailable: {error.args[0]}"
        ) from error
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "input_lock": input_lock,
        "arm_count": len(selected),
        "arms": selected,
        "analysis_contract": analysis_contract,
        "analysis_contract_sha256": sha256_json(analysis_contract),
        "source_screening_registry_sha256": all_arms["registry_sha256"],
        "master_seed_count": len(CONFIRMATION_MASTER_SEEDS),
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


__all__: Sequence[str] = (
    "BASELINE_POLICY_IDS",
    "CONFIRMATION_ARM_IDS",
    "CONFIRMATION_HORIZON_MS",
    "CONFIRMATION_MASTER_SEEDS",
    "Cat2NewFuryHorizonConfirmationV2Error",
    "EXECUTION_ENTRYPOINTS_V2",
    "FAMILYWISE_ALPHA",
    "INPUT_LOCK_SCHEMA",
    "NONBINDING_TARGET_HEALTH",
    "PRIOR_CONFIRMATION_ID",
    "SCHEMA",
    "SOURCE_HORIZON_MS",
    "STATUS",
    "build_horizon_confirmation_v2",
    "horizon_analysis_contract_v2",
)
