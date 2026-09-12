"""Prepare the bounded long-horizon follow-up to the Cat2 Fury screen.

The first eight-arm screen used one 5.001-second synthetic target.  A filler
cast in the last GCD can therefore look profitable without paying its later
rage or swing opportunity cost.  This module extends that *same* fixture to
20.001 seconds, gives the target nonbinding health, and prepares three frozen
development diagnostics on fresh master seeds.  It does not launch workers,
select a deployable policy, or turn the synthetic fixture into comparison
evidence.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cat2new_fury_screening_v1 import (
    FROZEN_SCREENING_ARMS_V1,
    build_screening_plans_v1,
)
from .cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from .fury_encounter_scenarios_v1 import HEALTH_STAT_INDEX
from .fury_paired_multiseed_runner_v4 import (
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
)
from .sim_bridge import DynamicTargetHealthV1
from .sim_bridge_dynamic_v3 import (
    dynamic_target_semantics_config_from_wire_v3,
)


JSONMap = dict[str, Any]

SCHEMA = "cat2new_fury_horizon_confirmation/v1"
INPUT_LOCK_SCHEMA = "cat2new_fury_horizon_confirmation_input_lock/v1"
STATUS = "PREPARED_SYNTHETIC_MECHANISM_DIAGNOSTIC_NOT_EXECUTED"
SOURCE_HORIZON_MS = 5_001
CONFIRMATION_HORIZON_MS = 20_001
NONBINDING_TARGET_HEALTH = 1_000_000.0
CONFIRMATION_MASTER_SEEDS = tuple(range(257, 513))
CONFIRMATION_ARM_IDS = (
    "ww_hamstring_cat_timing",
    "ww_wait_cat_timing",
    "bt_hamstring_cat_timing",
)


class Cat2NewFuryHorizonConfirmationV1Error(RuntimeError):
    """The frozen long-horizon diagnostic contract was violated."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Cat2NewFuryHorizonConfirmationV1Error(f"{label} must be an object")
    return value


def _single_player(request: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        parties = request["raid"]["parties"]
    except (KeyError, TypeError) as error:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "fixture request does not contain one raid player"
        ) from error
    if not isinstance(parties, list) or len(parties) != 1:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "fixture request must contain exactly one raid player"
        )
    players = parties[0].get("players") if isinstance(parties[0], Mapping) else None
    if (
        not isinstance(players, list)
        or len(players) != 1
        or not isinstance(players[0], Mapping)
    ):
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "fixture request must contain exactly one raid player"
        )
    return players[0]


def extend_screening_scenario_v1(value: Mapping[str, Any]) -> JSONMap:
    """Return a canonical 20.001-second mechanism-control scenario."""

    try:
        source = normalize_runner_scenarios([value])[0]
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            f"source scenario is invalid: {error}"
        ) from error
    request = deepcopy(source["request"])
    encounter = _mapping(request.get("encounter"), "request.encounter")
    targets = encounter.get("targets")
    if (
        source.get("stratum") != "single_target"
        or source.get("horizon_ms") != SOURCE_HORIZON_MS
        or not isinstance(targets, list)
        or len(targets) != 1
        or not isinstance(targets[0], Mapping)
    ):
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "long-horizon confirmation requires the frozen 5.001-second single-target fixture"
        )
    player = _single_player(request)
    options = _mapping(
        _mapping(player.get("warrior"), "player.warrior").get("options"),
        "player.warrior.options",
    )
    if options.get("startingRage") != 100:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "mechanism control requires the frozen 100-rage initial state"
        )
    target = dict(targets[0])
    stats = target.get("stats")
    if not isinstance(stats, list) or len(stats) <= HEALTH_STAT_INDEX:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "fixture target lacks the simulator health stat"
        )
    stats = list(stats)
    stats[HEALTH_STAT_INDEX] = NONBINDING_TARGET_HEALTH
    target["stats"] = stats
    request["encounter"] = {
        **dict(encounter),
        "duration": CONFIRMATION_HORIZON_MS / 1000.0,
        "durationVariation": 0,
        "useHealth": True,
        "targets": [target],
    }

    try:
        config = dynamic_target_semantics_config_from_wire_v3(
            source["dynamic_load_config"]
        )
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            f"fixture dynamic config is invalid: {error}"
        ) from error
    if len(config.target_health) != 1:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "mechanism control requires exactly one dynamic target"
        )
    config = replace(
        config,
        target_health=(DynamicTargetHealthV1(0, NONBINDING_TARGET_HEALTH),),
        idle_advance_horizon_ms=CONFIRMATION_HORIZON_MS,
    )
    request_sha = sha256_json(request)

    source_model = _mapping(source.get("scenario_model"), "scenario_model")
    scenario_model = {
        "schema": "cat2new_fury_horizon_confirmation_scenario/v1",
        "status": "SYNTHETIC_LONG_HORIZON_MECHANISM_CONTROL_NONVOTING",
        "request_sha256": request_sha,
        "bridge_execution_eligible": True,
        "historical_truth": False,
        "comparison_eligible": False,
        "source_fixture": {
            "scenario_contract_sha256": source["scenario_contract_sha256"],
            "request_sha256": source["request_sha256"],
            "scenario_model_sha256": source["scenario_model_sha256"],
            "source_horizon_ms": SOURCE_HORIZON_MS,
        },
        "mechanism_control": {
            "confirmation_horizon_ms": CONFIRMATION_HORIZON_MS,
            "target_health": NONBINDING_TARGET_HEALTH,
            "starting_rage": 100,
            "late_filler_cost_can_reach_later_gcd_and_mainhand_swing": True,
            "background_schedule_unchanged": True,
            "armor_schedule_unchanged": True,
            "attackability_prefix_unchanged": True,
        },
        "source_limitation_codes": list(source_model.get("limitation_codes", [])),
        "limitation_codes": [
            "SINGLE_SYNTHETIC_TARGET_MECHANISM_CONTROL",
            "NONBINDING_HEALTH_IS_NOT_HISTORICAL_TARGET_HEALTH",
            "NO_SELECTION_OR_DEPLOYMENT_AUTHORITY",
        ],
    }

    source_context = _mapping(
        source.get("target_context_bundle"), "target_context_bundle"
    )
    contexts = deepcopy(source_context.get("contexts"))
    if not isinstance(contexts, list) or len(contexts) != 1 or not isinstance(
        contexts[0], dict
    ):
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "fixture target context must contain one bound target"
        )
    contexts[0]["target_max_health"] = int(NONBINDING_TARGET_HEALTH)
    evidence = dict(
        _mapping(contexts[0].get("field_evidence"), "target field_evidence")
    )
    for field in ("target_health_pct", "target_max_health"):
        row = dict(_mapping(evidence.get(field), f"field_evidence.{field}"))
        row.update(
            {
                "kind": "SENSITIVITY_HYPOTHESIS",
                "hypothesis_id": "nonbinding-health-1000000-v1",
                "source_sha256": None,
            }
        )
        evidence[field] = row
    contexts[0]["field_evidence"] = dict(evidence)
    target_context = {
        **dict(source_context),
        "status": "SYNTHETIC_LONG_HORIZON_POLICY_CONTEXT_BOUND",
        "request_sha256": request_sha,
        "contexts": contexts,
        "comparison_eligible": False,
        "limitation_codes": [
            "SINGLE_SYNTHETIC_TARGET_MECHANISM_CONTROL",
            "NONBINDING_HEALTH_IS_NOT_HISTORICAL_TARGET_HEALTH",
        ],
    }

    candidate = {
        "instance_id": source["instance_id"],
        "component_id": source["component_id"],
        "scenario_id": (
            f"{source['scenario_id']}__mechanism-horizon-{CONFIRMATION_HORIZON_MS}"
            f"__health-{int(NONBINDING_TARGET_HEALTH)}"
        ),
        "stratum": "single_target",
        "scenario_weight": source["scenario_weight"],
        "horizon_ms": CONFIRMATION_HORIZON_MS,
        "estimated_cost_units": CONFIRMATION_HORIZON_MS,
        "request": request,
        "dynamic_load_config": config.to_wire(),
        "scenario_model": scenario_model,
        "target_context_bundle": target_context,
        "corpus_entry_sha256": source["corpus_entry_sha256"],
        "source_scenario_sha256": source["source_scenario_sha256"],
        "catalog_sha256": source["catalog_sha256"],
    }
    try:
        return normalize_runner_scenarios([candidate])[0]
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            f"extended scenario is invalid: {error}"
        ) from error


def build_horizon_confirmation_v1(
    template_runner_plan: Mapping[str, Any],
    *,
    bridge_path: str | Path,
    bridge_platform: str,
    bridge_build_id: str,
    workers_per_node: int,
    project_root: str | Path | None = None,
) -> JSONMap:
    """Prepare three fresh-seed long-horizon runner/dispatch pairs."""

    try:
        template = validate_runner_plan(template_runner_plan)
    except Exception as error:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            f"template runner plan is invalid: {error}"
        ) from error
    contract = template["contract"]
    scenarios = contract.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 1:
        raise Cat2NewFuryHorizonConfirmationV1Error(
            "horizon confirmation requires exactly one source scenario"
        )
    scenario = extend_screening_scenario_v1(scenarios[0])
    input_lock_core = {
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
        "primary_baseline_policy_id": "cat.fury.profile1",
        "primary_metric": "paired_candidate_minus_cat_mean_dps",
        "distribution_diagnostics": ["wins_ties_losses", "p05", "median", "p95"],
        "paired_test": "two_sided_paired_student_t",
        "familywise_alpha": 0.05,
        "quantile_method": "linear_hyndman_fan_type_7",
        "tie_rule": "candidate_minus_cat_dps_exactly_zero",
        "multiplicity": {
            "method": "HOLM",
            "family_size": len(CONFIRMATION_ARM_IDS),
            "family": "three candidate-minus-Cat contrasts",
        },
        "optional_stopping_allowed": False,
        "selection_or_promotion_allowed": False,
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
        "kind": "cat2new_fury_horizon_confirmation_protocol/v1",
        "input_lock_sha256": input_lock["content_address"]["sha256"],
        "intent": "DEVELOPMENT_DIAGNOSTIC_NONVOTING",
    }
    lane_contracts = [
        cat_runner_v4_lane_contract_v6()
        if row.get("policy_id") == "cat.fury.profile1"
        else dict(row)
        for row in contract["lane_contracts"]
    ]
    base_plan = build_runner_plan(
        protocol_id="cat2new-fury-horizon-confirmation-v1",
        protocol_sha256=sha256_json(protocol),
        phase="development",
        corpus_manifest_sha256=sha256_json(
            {"kind": "single-synthetic-mechanism-control", "scenario": scenario}
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
            }
        ),
        master_seeds=CONFIRMATION_MASTER_SEEDS,
        scenarios=[scenario],
        policies=contract["policies"],
        shard_count=6,
        bridge_identity=contract["bridge_identity"],
        execution_bundle_identity=contract["execution_bundle_identity"],
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace="cat2new-fury-horizon-confirmation-v1",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=lane_contracts,
    )
    kwargs: JSONMap = {
        "bridge_path": bridge_path,
        "bridge_platform": bridge_platform,
        "bridge_build_id": bridge_build_id,
        "workers_per_node": workers_per_node,
    }
    if project_root is not None:
        kwargs["project_root"] = project_root
    all_arms = build_screening_plans_v1(base_plan, **kwargs)
    by_id = {
        row["arm_spec"]["arm_id"]: row for row in all_arms["arms"]
    }
    selected = [by_id[arm_id] for arm_id in CONFIRMATION_ARM_IDS]
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "input_lock": input_lock,
        "arm_count": len(selected),
        "arms": selected,
        "analysis_contract": {
            "primary_baseline_policy_id": "cat.fury.profile1",
            "primary_metric": "paired_candidate_minus_cat_mean_dps",
            "distribution_diagnostics": [
                "wins_ties_losses",
                "p05",
                "median",
                "p95",
            ],
            "multiplicity_method": "HOLM",
            "multiplicity_family_size": len(selected),
            "paired_test": "two_sided_paired_student_t",
            "familywise_alpha": 0.05,
            "quantile_method": "linear_hyndman_fan_type_7",
            "tie_rule": "candidate_minus_cat_dps_exactly_zero",
            "required_complete_eligible_pairs_per_arm": len(
                CONFIRMATION_MASTER_SEEDS
            ),
            "no_arm_is_preapproved_for_promotion": True,
        },
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


def _write_bundle(directory: Path, bundle: Mapping[str, Any]) -> None:
    root = directory.expanduser().resolve()
    if root.exists():
        raise Cat2NewFuryHorizonConfirmationV1Error(
            f"output directory already exists: {root}"
        )
    root.mkdir(parents=True)
    manifest = deepcopy(dict(bundle))
    arm_rows = []
    for row in bundle["arms"]:
        arm_id = row["arm_spec"]["arm_id"]
        arm_root = root / arm_id
        arm_root.mkdir()
        runner = arm_root / "runner-plan.json"
        dispatch = arm_root / "dispatch-plan.json"
        runner.write_text(
            json.dumps(row["runner_plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        dispatch.write_text(
            json.dumps(row["dispatch_plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        arm_rows.append(
            {
                "arm_spec": row["arm_spec"],
                "factory_config": row["factory_config"],
                "candidate_policy_identity": row["candidate_policy_identity"],
                "execution_bundle_identity": row["execution_bundle_identity"],
                "runner_plan_path": f"{arm_id}/runner-plan.json",
                "dispatch_plan_path": f"{arm_id}/dispatch-plan.json",
                "runner_plan_sha256": row["runner_plan"]["plan_sha256"],
            }
        )
    manifest["arms"] = arm_rows
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-runner-plan", type=Path, required=True)
    parser.add_argument("--linux-bridge", type=Path, required=True)
    parser.add_argument("--bridge-build-id", required=True)
    parser.add_argument("--workers-per-node", type=int, default=160)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        template = json.loads(args.template_runner_plan.read_text(encoding="utf-8"))
        bundle = build_horizon_confirmation_v1(
            template,
            bridge_path=args.linux_bridge,
            bridge_platform="linux-amd64",
            bridge_build_id=args.bridge_build_id,
            workers_per_node=args.workers_per_node,
        )
        _write_bundle(args.output_directory, bundle)
        print(
            json.dumps(
                {
                    "status": STATUS,
                    "output_directory": str(args.output_directory.resolve()),
                    "arm_count": bundle["arm_count"],
                    "master_seed_count": bundle["master_seed_count"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, json.JSONDecodeError, Cat2NewFuryHorizonConfirmationV1Error) as error:
        print(f"BLOCKED: {error}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "CONFIRMATION_ARM_IDS",
    "CONFIRMATION_HORIZON_MS",
    "CONFIRMATION_MASTER_SEEDS",
    "Cat2NewFuryHorizonConfirmationV1Error",
    "NONBINDING_TARGET_HEALTH",
    "SCHEMA",
    "STATUS",
    "build_horizon_confirmation_v1",
    "extend_screening_scenario_v1",
)
