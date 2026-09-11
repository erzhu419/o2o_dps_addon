"""Content-addressed shard runner contract for paired Fury evaluations.

This module is deliberately separate from the statistical multi-seed gate.  It
turns a frozen protocol, scenario set, and policy set into indivisible paired
groups, assigns those groups to deterministic LPT shards, and validates compact
per-rollout JSONL produced by a worker-owned executor.

The executor is injectable so the plumbing can be tested without launching the
simulator.  A production executor must own exactly one ``SimulatorBridge``
process for the lifetime of one worker; a bridge may never be shared between
workers.  No historical v1 artifact or source is modified by this contract.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
import struct
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .fury_full_policy_rollout_v2 import (
    DYNAMIC_LOAD_BINDING_SCHEMA,
    dynamic_rollout_load_from_config_wire_v1,
)


JSONMap = dict[str, Any]

PLAN_KIND = "fury_paired_multiseed_runner_plan_v2"
PLAN_CONTRACT_KIND = "fury_paired_multiseed_runner_contract_v2"
ROLLOUT_KIND = "fury_paired_multiseed_rollout_v2"
SHARD_MANIFEST_KIND = "fury_paired_multiseed_shard_manifest_v2"
REDUCTION_KIND = "fury_paired_multiseed_reduction_v2"
REDUCTION_RECEIPT_KIND = "fury_paired_multiseed_reduction_receipt_v2"
SEED_DERIVATION_ALGORITHM = "sha256_namespace_master_request_u63_v1"
EXECUTION_BUNDLE_FIELDS = (
    "python_source_closure_sha256",
    "ordered_sink_executor_sha256",
    "full_policy_rollout_executor_sha256",
    "paired_runner_source_sha256",
    "evaluation_source_sha256",
    "runtime_snapshot_sha256",
)
SINGLE_BRIDGE_MODE = "CALLER_OWNED_SINGLE_BRIDGE"
SYNTHETIC_MODE = "SYNTHETIC_TEST"
COMPARISON_INTENT = "SCIENTIFIC_COMPARISON"
DIAGNOSTIC_INTENT = "DIAGNOSTIC_NONVOTING"
SCENARIO_MODEL_KIND = "fury_runner_scenario_model_v2"
TARGET_CONTEXT_BUNDLE_KIND = "fury_target_context_bundle_v2"
EXACT_STATIC_REQUEST_SEMANTICS_SCHEMA = (
    "fury_exact_static_request_semantics/v1"
)
SYNTHETIC_NONVOTING_REASON = "SYNTHETIC_TEST_NON_SCIENTIFIC"
DIAGNOSTIC_NONVOTING_REASON = "DIAGNOSTIC_PLAN_NONVOTING"
FULL_POLICY_ROLLOUT_SCHEMA = "fury_full_policy_simulator_rollout/v2"
_POLICY_TO_FULL_ROLLOUT_EXPERT_ID = {
    "cat.fury.profile1": "cat.fury.profile1",
    "contra.deployed.fury.raid_a": "contra.deployed.fury.raid_a.v2",
    "contra260817.fury.source_candidate": "contra260817.fury.source_candidate",
}
_FULL_POLICY_COMMAND_CONTRACT = {
    "initial_load_command": "load",
    "load_state_actions_act_wait_advance": (
        "source-driven full-scenario loop; no fallback command"
    ),
    "cancel_queue": (
        "capability-audited; never invoked unless an audited raw source "
        "cancellation operation exists (none in current Cat/Contra v2)"
    ),
    "set_target": (
        "capability-audited; never invoked unless an audited raw source "
        "target-selection operation exists (none in current Cat/Contra v2)"
    ),
    "start_attack_stop_cast": (
        "optional bridge controls invoked only by ordered raw source sinks"
    ),
    "server_results_since_last_decision": (
        "typed follow-up evidence; immediate act acceptance is never a result"
    ),
}
_FULL_POLICY_CLAIMS_EXCLUDED = [
    "original Cat, Contra, or Contra260817 Lua execution",
    "act acceptance as hit, crit, miss, or damage outcome",
    "DPS superiority when any blocker is present",
    "sensitivity target inputs as exact target truth",
]
_TARGET_CONTEXT_RECEIPT_FIELDS = {
    "context_id",
    "mode",
    "target_index",
    "target_classification",
    "target_name",
    "equipped_item_count",
    "target_max_health",
    "health_pct_schedule",
    "exact_by_declared_contract",
    "field_evidence",
}
_TARGET_EVIDENCE_FIELDS = {
    "target_health_pct",
    "target_max_health",
    "target_classification",
    "target_name",
    "equipped_item_names",
    "target_position",
}
_FIELD_EVIDENCE_FIELDS = {
    "schema",
    "kind",
    "source_sha256",
    "corpus_sha256",
    "hypothesis_id",
}
_FULL_POLICY_STEP_FIELDS = {
    "decision_index",
    "simulator_state_before",
    "available_actions_before",
    "target_semantics",
    "expert_state",
    "proposal",
    "ordered_execution",
    "independent_ordered_audit",
    "server_observation_after_advance",
    "simulator_state_after_commands",
    "simulator_state_next_epoch",
    "time_delta_ms",
    "damage_delta",
    "blockers",
}

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_POSITIVE_INT64 = (1 << 63) - 1


class FuryPairedRunnerError(RuntimeError):
    """A plan, shard, rollout, or reduction invariant was violated."""


@dataclass(frozen=True)
class ValidatedShard:
    """One fully validated shard and its canonical rollout rows."""

    manifest_path: Path
    manifest: JSONMap
    rows: tuple[JSONMap, ...]


def canonical_json_bytes(value: Any) -> bytes:
    """Return strict deterministic JSON bytes used by every content address."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def derive_simulator_seed(
    master_seed: int,
    request_sha256: str,
    *,
    namespace: str,
) -> int:
    """Derive a stable nonzero int64 seed from a master seed and request bytes.

    All policies in one paired group receive the same result.  Different
    request bytes receive a domain-separated stream even when the same master
    seed is reused across the fixed corpus.
    """

    master = _positive_int64(master_seed, "master_seed")
    request_digest = _lower_sha256(request_sha256, "request_sha256")
    domain = _text(namespace, "namespace")
    counter = 0
    while True:
        payload = {
            "algorithm": SEED_DERIVATION_ALGORITHM,
            "counter": counter,
            "master_seed": master,
            "namespace": domain,
            "request_sha256": request_digest,
        }
        value = int.from_bytes(hashlib.sha256(canonical_json_bytes(payload)).digest()[:8], "big")
        value &= _MAX_POSITIVE_INT64
        if value != 0:
            return value
        counter += 1


def normalize_runner_scenarios(
    scenarios: Iterable[Mapping[str, Any]],
) -> tuple[JSONMap, ...]:
    """Return the canonical scenario projection bound by every runner plan."""

    normalized = tuple(
        sorted(
            (_normalize_scenario(value) for value in scenarios),
            key=lambda value: (
                str(value["instance_id"]),
                str(value["scenario_id"]),
            ),
        )
    )
    if not normalized:
        raise FuryPairedRunnerError("scenarios must not be empty")
    keys = [
        (str(value["instance_id"]), str(value["scenario_id"]))
        for value in normalized
    ]
    if len(set(keys)) != len(keys):
        raise FuryPairedRunnerError(
            "(instance_id, scenario_id) pairs must be unique"
        )
    return normalized


def runner_scenario_bundle_sha256(
    scenarios: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the exact canonical scenario projection accepted by the runner."""

    return sha256_json(list(normalize_runner_scenarios(scenarios)))


def build_exact_static_request_semantics_receipt(
    request: Mapping[str, Any],
) -> JSONMap:
    """Project the only comparison-safe scenario semantics implemented today.

    The simulator bridge currently consumes a static request. It does not yet
    interpret a versioned armor, attackability, health, or tactic schedule and
    does not return a receipt proving such a schedule was consumed. Therefore
    this receipt is deliberately just a byte-stable projection of encounter
    fields physically present in the request passed to the bridge. A supplied
    status label cannot widen this projection.
    """

    request_copy = _strict_json_copy(
        _mapping(request, "exact static semantics request"),
        "exact static semantics request",
    )
    encounter = _mapping(
        request_copy.get("encounter"),
        "exact static semantics request.encounter",
    )
    if encounter.get("useHealth") is not False:
        raise FuryPairedRunnerError(
            "exact static request semantics require encounter.useHealth == false"
        )
    duration = _positive_finite(
        encounter.get("duration"),
        "exact static semantics request.encounter.duration",
    )
    targets = _sequence(
        encounter.get("targets"),
        "exact static semantics request.encounter.targets",
    )
    target_models: list[JSONMap] = []
    for index, raw_target in enumerate(targets):
        target = _strict_json_copy(
            _mapping(raw_target, f"exact static semantics target {index}"),
            f"exact static semantics target {index}",
        )
        target_models.append(
            {
                "target_index": index,
                "request_target_sha256": sha256_json(target),
                "request_target": target,
            }
        )
    return {
        "schema": EXACT_STATIC_REQUEST_SEMANTICS_SCHEMA,
        "status": "EXACT_STATIC_REQUEST_SEMANTICS",
        "request_sha256": sha256_json(request_copy),
        "encounter_duration_seconds": duration,
        "use_health": False,
        "target_models": target_models,
    }


def runner_scenario_model_bundle_sha256(
    scenarios: Iterable[Mapping[str, Any]],
) -> str:
    """Hash ordered scenario-model identities without recursive phase metadata."""

    normalized = normalize_runner_scenarios(scenarios)
    return sha256_json(
        [
            {
                "instance_id": row["instance_id"],
                "scenario_id": row["scenario_id"],
                "scenario_model_sha256": row["scenario_model_sha256"],
            }
            for row in normalized
        ]
    )


def runner_target_context_bundle_set_sha256(
    scenarios: Iterable[Mapping[str, Any]],
) -> str:
    """Hash ordered target-context identities without recursive phase metadata."""

    normalized = normalize_runner_scenarios(scenarios)
    return sha256_json(
        [
            {
                "instance_id": row["instance_id"],
                "scenario_id": row["scenario_id"],
                "target_context_bundle_sha256": row[
                    "target_context_bundle_sha256"
                ],
            }
            for row in normalized
        ]
    )


def build_runner_plan(
    *,
    protocol_id: str,
    protocol_sha256: str,
    phase: str,
    corpus_manifest_sha256: str,
    runner_inputs_sha256: str,
    runner_scenario_bundle_sha256: str,
    corpus_binding_sha256: str,
    master_seeds: Iterable[int],
    scenarios: Iterable[Mapping[str, Any]],
    policies: Iterable[Mapping[str, Any]],
    shard_count: int,
    bridge_identity: Mapping[str, Any],
    execution_bundle_identity: Mapping[str, Any],
    execution_mode: str,
    seed_namespace: str,
    plan_intent: str | None = None,
) -> JSONMap:
    """Build a deterministic, content-addressed paired-group execution plan."""

    protocol_name = _text(protocol_id, "protocol_id")
    protocol_digest = _lower_sha256(protocol_sha256, "protocol_sha256")
    phase_name = _text(phase, "phase")
    corpus_digest = _lower_sha256(
        corpus_manifest_sha256, "corpus_manifest_sha256"
    )
    runner_inputs_digest = _lower_sha256(
        runner_inputs_sha256, "runner_inputs_sha256"
    )
    expected_scenario_bundle_digest = _lower_sha256(
        runner_scenario_bundle_sha256, "runner_scenario_bundle_sha256"
    )
    corpus_binding_digest = _lower_sha256(
        corpus_binding_sha256, "corpus_binding_sha256"
    )
    shards = _positive_int(shard_count, "shard_count")
    namespace = _text(seed_namespace, "seed_namespace")
    required_execution_mode = _text(execution_mode, "execution_mode")
    if required_execution_mode not in {SYNTHETIC_MODE, SINGLE_BRIDGE_MODE}:
        raise FuryPairedRunnerError(
            f"unsupported execution_mode: {required_execution_mode}"
        )
    if plan_intent is None:
        if required_execution_mode != SYNTHETIC_MODE:
            raise FuryPairedRunnerError(
                "production runner plans require explicit plan_intent"
            )
        intent = DIAGNOSTIC_INTENT
    else:
        intent = _text(plan_intent, "plan_intent")
    if intent not in {COMPARISON_INTENT, DIAGNOSTIC_INTENT}:
        raise FuryPairedRunnerError(f"unsupported plan_intent: {intent}")
    if intent == COMPARISON_INTENT and required_execution_mode != SINGLE_BRIDGE_MODE:
        raise FuryPairedRunnerError(
            "scientific comparison plans require production single-bridge execution"
        )

    seed_values = tuple(master_seeds)
    if not seed_values:
        raise FuryPairedRunnerError("master_seeds must not be empty")
    normalized_seeds = tuple(
        _positive_int64(value, f"master_seeds[{index}]")
        for index, value in enumerate(seed_values)
    )
    if len(set(normalized_seeds)) != len(normalized_seeds):
        raise FuryPairedRunnerError("master_seeds must be unique")

    normalized_policies = tuple(_normalize_policy(value) for value in policies)
    if not normalized_policies:
        raise FuryPairedRunnerError("policies must not be empty")
    policy_ids = [str(value["policy_id"]) for value in normalized_policies]
    if len(set(policy_ids)) != len(policy_ids):
        raise FuryPairedRunnerError("policy IDs must be unique")

    normalized_scenarios = normalize_runner_scenarios(scenarios)
    observed_scenario_bundle_digest = sha256_json(list(normalized_scenarios))
    if observed_scenario_bundle_digest != expected_scenario_bundle_digest:
        raise FuryPairedRunnerError(
            "normalized scenario bundle differs from runner_scenario_bundle_sha256"
        )
    if intent == COMPARISON_INTENT:
        noncomparison = [
            str(value["scenario_id"])
            for value in normalized_scenarios
            if value["scenario_model"]["comparison_eligible"] is not True
            or value["target_context_bundle"]["comparison_eligible"] is not True
        ]
        if noncomparison:
            raise FuryPairedRunnerError(
                "scientific comparison plan contains non-comparison scenario models: "
                + ", ".join(noncomparison[:5])
            )
    if required_execution_mode == SINGLE_BRIDGE_MODE:
        nonexecutable = [
            str(value["scenario_id"])
            for value in normalized_scenarios
            if value["scenario_model"]["bridge_execution_eligible"] is not True
            or value["target_context_bundle"]["bridge_execution_eligible"] is not True
        ]
        if nonexecutable:
            raise FuryPairedRunnerError(
                "production plan contains unbound/non-executable scenario semantics: "
                + ", ".join(nonexecutable[:5])
            )

    group_count = len(normalized_scenarios) * len(normalized_seeds)
    if shards > group_count:
        raise FuryPairedRunnerError(
            "shard_count cannot exceed the number of paired groups"
        )
    normalized_bridge = _normalize_bridge_identity(bridge_identity)
    normalized_execution_bundle = _normalize_execution_bundle(
        execution_bundle_identity
    )

    group_bases: list[JSONMap] = []
    for scenario in normalized_scenarios:
        for master_seed in normalized_seeds:
            simulator_seed = derive_simulator_seed(
                master_seed,
                str(scenario["request_sha256"]),
                namespace=namespace,
            )
            identity = {
                "protocol_sha256": protocol_digest,
                "corpus_manifest_sha256": corpus_digest,
                "phase": phase_name,
                "instance_id": scenario["instance_id"],
                "scenario_id": scenario["scenario_id"],
                "scenario_contract_sha256": scenario["scenario_contract_sha256"],
                "master_seed": master_seed,
                "simulator_seed": simulator_seed,
            }
            dynamic_contract_sha256 = None
            if "dynamic_load_config" in scenario:
                try:
                    dynamic_contract_sha256 = (
                        dynamic_rollout_load_from_config_wire_v1(
                            scenario["request"],
                            simulator_seed,
                            scenario["dynamic_load_config"],
                        ).contract_sha256
                    )
                except (TypeError, ValueError) as error:
                    raise FuryPairedRunnerError(
                        f"failed to bind dynamic scenario to simulator seed: {error}"
                    ) from error
                identity["dynamic_load_contract_sha256"] = (
                    dynamic_contract_sha256
                )
            group_bases.append(
                {
                    "group_id": sha256_json(identity),
                    **identity,
                    "request_sha256": scenario["request_sha256"],
                    "scenario_model_sha256": scenario["scenario_model_sha256"],
                    "target_context_bundle_sha256": scenario[
                        "target_context_bundle_sha256"
                    ],
                    "horizon_ms": scenario["horizon_ms"],
                    "estimated_cost_units": int(scenario["estimated_cost_units"])
                    * len(normalized_policies),
                }
            )

    assignment, shard_loads = _lpt_assignment(group_bases, shards)
    groups = tuple(
        {
            **group,
            "shard_index": assignment[str(group["group_id"])],
        }
        for group in sorted(group_bases, key=lambda value: str(value["group_id"]))
    )
    shard_rows: list[JSONMap] = []
    for shard_index in range(shards):
        group_ids = sorted(
            str(value["group_id"])
            for value in groups
            if int(value["shard_index"]) == shard_index
        )
        shard_rows.append(
            {
                "shard_index": shard_index,
                "group_count": len(group_ids),
                "expected_rollout_count": len(group_ids) * len(normalized_policies),
                "estimated_cost_units": shard_loads[shard_index],
                "group_ids": group_ids,
                "group_ids_sha256": sha256_json(group_ids),
            }
        )

    contract: JSONMap = {
        "schema_version": 2,
        "kind": PLAN_CONTRACT_KIND,
        "protocol_id": protocol_name,
        "protocol_sha256": protocol_digest,
        "corpus_manifest_sha256": corpus_digest,
        "runner_inputs_sha256": runner_inputs_digest,
        "runner_scenario_bundle_sha256": observed_scenario_bundle_digest,
        "scenario_model_bundle_sha256": runner_scenario_model_bundle_sha256(
            normalized_scenarios
        ),
        "target_context_bundle_set_sha256": runner_target_context_bundle_set_sha256(
            normalized_scenarios
        ),
        "corpus_binding_sha256": corpus_binding_digest,
        "phase": phase_name,
        "seed_derivation": {
            "algorithm": SEED_DERIVATION_ALGORITHM,
            "namespace": namespace,
            "master_seed_count": len(normalized_seeds),
            "master_seeds": list(normalized_seeds),
            "master_seed_list_sha256": sha256_json(list(normalized_seeds)),
            "zero_seed_allowed": False,
        },
        "bridge_identity": normalized_bridge,
        "execution_bundle_identity": normalized_execution_bundle,
        "execution_bundle_sha256": sha256_json(normalized_execution_bundle),
        "execution_mode": required_execution_mode,
        "plan_intent": intent,
        "scientific_comparison_planned": intent == COMPARISON_INTENT,
        "diagnostic_nonvoting": intent == DIAGNOSTIC_INTENT,
        "policies": list(normalized_policies),
        "policy_ids": policy_ids,
        "policy_bundle_sha256": sha256_json(list(normalized_policies)),
        "scenarios": list(normalized_scenarios),
        "scenario_bundle_sha256": sha256_json(list(normalized_scenarios)),
        "groups": list(groups),
        "group_count": len(groups),
        "expected_rollout_count": len(groups) * len(normalized_policies),
        "shard_count": shards,
        "shards": shard_rows,
        "execution_contract": {
            "assignment_algorithm": "deterministic_lpt_cost_then_group_sha_v1",
            "paired_group_is_indivisible": True,
            "one_bridge_process_per_worker": True,
            "shared_mutable_bridge_between_workers": False,
            "worker_owns_complete_shard": True,
            "policies_within_group_execute_serially": True,
            "rollout_rows_are_compact": True,
            "scenario_model_and_target_context_are_content_addressed": True,
            "production_target_context_receipts_byte_match_plan": True,
            "synthetic_rows_scientifically_eligible": False,
        },
    }
    return {
        "schema_version": 2,
        "kind": PLAN_KIND,
        "generated_at": _now(),
        "plan_sha256": sha256_json(contract),
        "contract": contract,
        "execution_started": False,
        "victory_claim_allowed": False,
    }


def validate_runner_plan(plan: Mapping[str, Any]) -> JSONMap:
    """Reconstruct the plan and reject any content or assignment drift."""

    if plan.get("schema_version") != 2 or plan.get("kind") != PLAN_KIND:
        raise FuryPairedRunnerError("invalid runner plan schema or kind")
    contract = _mapping(plan.get("contract"), "plan.contract")
    expected_digest = sha256_json(contract)
    if plan.get("plan_sha256") != expected_digest:
        raise FuryPairedRunnerError("runner plan SHA-256 does not match its contract")
    if contract.get("schema_version") != 2 or contract.get("kind") != PLAN_CONTRACT_KIND:
        raise FuryPairedRunnerError("invalid runner plan contract schema or kind")

    seed_contract = _mapping(contract.get("seed_derivation"), "seed_derivation")
    rebuilt = build_runner_plan(
        protocol_id=_text(contract.get("protocol_id"), "protocol_id"),
        protocol_sha256=_lower_sha256(
            contract.get("protocol_sha256"), "protocol_sha256"
        ),
        phase=_text(contract.get("phase"), "phase"),
        corpus_manifest_sha256=_lower_sha256(
            contract.get("corpus_manifest_sha256"), "corpus_manifest_sha256"
        ),
        runner_inputs_sha256=_lower_sha256(
            contract.get("runner_inputs_sha256"), "runner_inputs_sha256"
        ),
        runner_scenario_bundle_sha256=_lower_sha256(
            contract.get("runner_scenario_bundle_sha256"),
            "runner_scenario_bundle_sha256",
        ),
        corpus_binding_sha256=_lower_sha256(
            contract.get("corpus_binding_sha256"), "corpus_binding_sha256"
        ),
        master_seeds=_sequence(seed_contract.get("master_seeds"), "master_seeds"),
        scenarios=_sequence(contract.get("scenarios"), "scenarios"),
        policies=_sequence(contract.get("policies"), "policies"),
        shard_count=_positive_int(contract.get("shard_count"), "shard_count"),
        bridge_identity=_mapping(contract.get("bridge_identity"), "bridge_identity"),
        execution_bundle_identity=_mapping(
            contract.get("execution_bundle_identity"),
            "execution_bundle_identity",
        ),
        execution_mode=_text(contract.get("execution_mode"), "execution_mode"),
        seed_namespace=_text(seed_contract.get("namespace"), "seed namespace"),
        plan_intent=_text(contract.get("plan_intent"), "plan_intent"),
    )
    if rebuilt["contract"] != contract or rebuilt["plan_sha256"] != expected_digest:
        raise FuryPairedRunnerError(
            "runner plan is not the canonical deterministic plan for its inputs"
        )
    return copy.deepcopy(dict(plan))


def execute_shard(
    plan: Mapping[str, Any],
    shard_index: int,
    executor: Callable[..., Mapping[str, Any]],
    output_directory: str | Path,
    *,
    execution_mode: str | None = None,
    worker_id: str = "synthetic-worker",
    replace_existing: bool = False,
) -> Path:
    """Execute one complete shard through an injected paired-rollout executor.

    ``executor`` is called with keyword arguments ``group``, ``scenario``, and
    ``policy``.  A production callable is responsible for opening exactly one
    bridge before this function and reusing only that bridge in this worker.
    """

    validated_plan = validate_runner_plan(plan)
    contract = validated_plan["contract"]
    shard_value = _nonnegative_int(shard_index, "shard_index")
    if shard_value >= int(contract["shard_count"]):
        raise FuryPairedRunnerError("shard_index is outside the plan")
    if not callable(executor):
        raise TypeError("executor must be callable")
    mode = (
        str(contract["execution_mode"])
        if execution_mode is None
        else _text(execution_mode, "execution_mode")
    )
    if mode not in {SYNTHETIC_MODE, SINGLE_BRIDGE_MODE}:
        raise FuryPairedRunnerError(f"unsupported execution_mode: {mode}")
    if mode != contract["execution_mode"]:
        raise FuryPairedRunnerError(
            "worker execution_mode differs from the frozen runner plan"
        )
    worker_name = _text(worker_id, "worker_id")

    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"shard-{shard_value:05d}"
    jsonl_path = destination / f"{stem}.rollouts.jsonl"
    manifest_path = destination / f"{stem}.manifest.json"
    if not replace_existing and (jsonl_path.exists() or manifest_path.exists()):
        raise FuryPairedRunnerError(
            f"shard output already exists for shard {shard_value}"
        )

    scenarios = {
        (str(value["instance_id"]), str(value["scenario_id"])): value
        for value in contract["scenarios"]
    }
    policies = {str(value["policy_id"]): value for value in contract["policies"]}
    groups = {
        str(value["group_id"]): value
        for value in contract["groups"]
        if int(value["shard_index"]) == shard_value
    }
    shard_contract = contract["shards"][shard_value]
    ordered_group_ids = list(shard_contract["group_ids"])
    rows: list[JSONMap] = []
    artifact_payloads: dict[str, bytes] = {}
    artifact_entries: list[JSONMap] = []
    for group_id in ordered_group_ids:
        group = groups[group_id]
        scenario = scenarios[(str(group["instance_id"]), str(group["scenario_id"]))]
        for policy_id in contract["policy_ids"]:
            policy = policies[str(policy_id)]
            raw_result = executor(
                group=copy.deepcopy(group),
                scenario=copy.deepcopy(scenario),
                policy=copy.deepcopy(policy),
            )
            if mode == SINGLE_BRIDGE_MODE:
                result, full_policy_artifact = _normalize_full_policy_executor_result(
                    raw_result,
                    group=group,
                    scenario=scenario,
                    policy=policy,
                )
            else:
                result = {
                    **_normalize_executor_result(raw_result),
                    "full_policy_rollout_sha256": None,
                }
                full_policy_artifact = None
            result = _finalize_result_for_plan(
                result,
                scenario=scenario,
                execution_mode=mode,
                plan_intent=str(contract["plan_intent"]),
            )
            row: JSONMap = {
                "schema_version": 2,
                "kind": ROLLOUT_KIND,
                "plan_sha256": validated_plan["plan_sha256"],
                "protocol_id": contract["protocol_id"],
                "protocol_sha256": contract["protocol_sha256"],
                "corpus_manifest_sha256": contract["corpus_manifest_sha256"],
                "runner_inputs_sha256": contract["runner_inputs_sha256"],
                "corpus_binding_sha256": contract["corpus_binding_sha256"],
                "phase": contract["phase"],
                "plan_intent": contract["plan_intent"],
                "shard_index": shard_value,
                "group_id": group_id,
                "master_seed": group["master_seed"],
                # Compatibility with the statistical analyzer: seed is the
                # master pairing label, never the request-derived bridge seed.
                "seed": group["master_seed"],
                "simulator_seed": group["simulator_seed"],
                "instance_id": scenario["instance_id"],
                "component_id": scenario["component_id"],
                "scenario_id": scenario["scenario_id"],
                "stratum": scenario["stratum"],
                "scenario_weight": scenario["scenario_weight"],
                "horizon_ms": scenario["horizon_ms"],
                "request_sha256": scenario["request_sha256"],
                "scenario_contract_sha256": scenario["scenario_contract_sha256"],
                "scenario_model_sha256": scenario["scenario_model_sha256"],
                "target_context_bundle_sha256": scenario[
                    "target_context_bundle_sha256"
                ],
                "corpus_entry_sha256": scenario["corpus_entry_sha256"],
                "source_scenario_sha256": scenario["source_scenario_sha256"],
                "catalog_sha256": scenario["catalog_sha256"],
                "policy_id": policy["policy_id"],
                "policy_source_sha256": policy["source_sha256"],
                "policy_adapter_sha256": policy["adapter_sha256"],
                "policy_profile_sha256": policy["profile_sha256"],
                "bridge_sha256": contract["bridge_identity"]["sha256"],
                "execution_bundle_sha256": contract["execution_bundle_sha256"],
                "execution_mode": mode,
                **result,
            }
            row["row_sha256"] = sha256_json(row)
            _validate_rollout_row(validated_plan, row, expected_shard=shard_value)
            rows.append(row)
            if full_policy_artifact is not None:
                artifact_digest = str(row["full_policy_rollout_sha256"])
                artifact_payload = canonical_json_bytes(full_policy_artifact)
                if hashlib.sha256(artifact_payload).hexdigest() != artifact_digest:
                    raise FuryPairedRunnerError(
                        "full-policy artifact bytes differ from their content address"
                    )
                previous_payload = artifact_payloads.get(artifact_digest)
                if previous_payload is not None and previous_payload != artifact_payload:
                    raise FuryPairedRunnerError("full-policy artifact SHA-256 collision")
                artifact_payloads[artifact_digest] = artifact_payload
                artifact_entries.append(
                    {
                        "group_id": group_id,
                        "policy_id": policy["policy_id"],
                        "row_sha256": row["row_sha256"],
                        "sha256": artifact_digest,
                        "file": f"full-policy-artifacts/{artifact_digest}.json",
                        "size_bytes": len(artifact_payload),
                    }
                )

    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    _atomic_write_bytes(jsonl_path, payload)
    for artifact_digest, artifact_payload in sorted(artifact_payloads.items()):
        _atomic_write_bytes(
            destination / "full-policy-artifacts" / f"{artifact_digest}.json",
            artifact_payload,
        )
    file_digest = hashlib.sha256(payload).hexdigest()
    manifest: JSONMap = {
        "schema_version": 2,
        "kind": SHARD_MANIFEST_KIND,
        "generated_at": _now(),
        "status": "COMPLETED",
        "plan_sha256": validated_plan["plan_sha256"],
        "protocol_id": contract["protocol_id"],
        "phase": contract["phase"],
        "plan_intent": contract["plan_intent"],
        "shard_index": shard_value,
        "group_count": len(ordered_group_ids),
        "group_ids_sha256": sha256_json(ordered_group_ids),
        "expected_rollout_count": int(shard_contract["expected_rollout_count"]),
        "rollout_count": len(rows),
        "policy_ids": list(contract["policy_ids"]),
        "bridge_identity": copy.deepcopy(contract["bridge_identity"]),
        "worker_contract": {
            "worker_id": worker_name,
            "execution_mode": mode,
            "declared_bridge_process_count": 0 if mode == SYNTHETIC_MODE else 1,
            "one_bridge_process_per_worker": True,
            "shared_mutable_bridge_between_workers": False,
                "contract_runtime_evidence": (
                "NOT_APPLICABLE_SYNTHETIC"
                if mode == SYNTHETIC_MODE
                    else "FULL_POLICY_ARTIFACT_VALIDATED_BY_RUNNER"
            ),
        },
        "rollout_jsonl": {
            "file": jsonl_path.name,
            "size_bytes": len(payload),
            "sha256": file_digest,
            "canonical_json_lines": True,
        },
        "full_policy_artifacts": {
            "storage_contract": "CONTENT_ADDRESSED_CANONICAL_JSON_SHA256_V1",
            "entry_count": len(artifact_entries),
            "unique_file_count": len(artifact_payloads),
            "entries": artifact_entries,
            "entries_sha256": sha256_json(artifact_entries),
        },
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    _atomic_write_bytes(manifest_path, _pretty_json_bytes(manifest))
    return manifest_path


def validate_shard(
    plan: Mapping[str, Any], manifest_path: str | Path
) -> ValidatedShard:
    """Validate a completed shard against the exact plan Cartesian product."""

    validated_plan = validate_runner_plan(plan)
    contract = validated_plan["contract"]
    path = Path(manifest_path).expanduser().resolve()
    manifest = _read_json_object(path, "shard manifest")
    observed_manifest_digest = manifest.get("manifest_sha256")
    _lower_sha256(observed_manifest_digest, "shard manifest SHA-256")
    unsigned_manifest = dict(manifest)
    unsigned_manifest.pop("manifest_sha256", None)
    if observed_manifest_digest != sha256_json(unsigned_manifest):
        raise FuryPairedRunnerError("shard manifest SHA-256 mismatch")
    if manifest.get("schema_version") != 2 or manifest.get("kind") != SHARD_MANIFEST_KIND:
        raise FuryPairedRunnerError("invalid shard manifest schema or kind")
    if set(manifest) != {
        "schema_version",
        "kind",
        "generated_at",
        "status",
        "plan_sha256",
        "protocol_id",
        "phase",
        "plan_intent",
        "shard_index",
        "group_count",
        "group_ids_sha256",
        "expected_rollout_count",
        "rollout_count",
        "policy_ids",
        "bridge_identity",
        "worker_contract",
        "rollout_jsonl",
        "full_policy_artifacts",
        "manifest_sha256",
    }:
        raise FuryPairedRunnerError("shard manifest field set mismatch")
    if manifest.get("status") != "COMPLETED":
        raise FuryPairedRunnerError("shard manifest is not completed")
    if manifest.get("plan_sha256") != validated_plan["plan_sha256"]:
        raise FuryPairedRunnerError("shard manifest plan SHA-256 mismatch")
    if manifest.get("protocol_id") != contract["protocol_id"]:
        raise FuryPairedRunnerError("shard manifest protocol mismatch")
    if manifest.get("phase") != contract["phase"]:
        raise FuryPairedRunnerError("shard manifest phase mismatch")
    if manifest.get("plan_intent") != contract["plan_intent"]:
        raise FuryPairedRunnerError("shard manifest plan-intent mismatch")

    shard_index = _nonnegative_int(manifest.get("shard_index"), "shard_index")
    if shard_index >= int(contract["shard_count"]):
        raise FuryPairedRunnerError("shard manifest index is outside the plan")
    expected_shard = contract["shards"][shard_index]
    if manifest.get("group_count") != expected_shard["group_count"]:
        raise FuryPairedRunnerError("shard group count mismatch")
    if manifest.get("group_ids_sha256") != expected_shard["group_ids_sha256"]:
        raise FuryPairedRunnerError("shard group-set digest mismatch")
    if manifest.get("expected_rollout_count") != expected_shard["expected_rollout_count"]:
        raise FuryPairedRunnerError("shard expected rollout count mismatch")
    if manifest.get("policy_ids") != contract["policy_ids"]:
        raise FuryPairedRunnerError("shard policy order/identity mismatch")
    if manifest.get("bridge_identity") != contract["bridge_identity"]:
        raise FuryPairedRunnerError("shard bridge identity mismatch")
    _validate_worker_contract(manifest.get("worker_contract"))
    if manifest["worker_contract"].get("execution_mode") != contract[
        "execution_mode"
    ]:
        raise FuryPairedRunnerError(
            "shard worker execution mode differs from the runner plan"
        )

    file_contract = _mapping(manifest.get("rollout_jsonl"), "rollout_jsonl")
    if set(file_contract) != {
        "file",
        "size_bytes",
        "sha256",
        "canonical_json_lines",
    }:
        raise FuryPairedRunnerError("rollout JSONL contract field set mismatch")
    if file_contract.get("canonical_json_lines") is not True:
        raise FuryPairedRunnerError("rollout JSONL is not declared canonical")
    filename = _text(file_contract.get("file"), "rollout_jsonl.file")
    if Path(filename).name != filename or Path(filename).is_absolute():
        raise FuryPairedRunnerError("rollout JSONL path must be a local filename")
    jsonl_path = path.parent / filename
    try:
        payload = jsonl_path.read_bytes()
    except OSError as exc:
        raise FuryPairedRunnerError(f"could not read rollout JSONL {jsonl_path}: {exc}") from exc
    if file_contract.get("size_bytes") != len(payload):
        raise FuryPairedRunnerError("rollout JSONL size mismatch")
    if file_contract.get("sha256") != hashlib.sha256(payload).hexdigest():
        raise FuryPairedRunnerError("rollout JSONL SHA-256 mismatch")

    rows: list[JSONMap] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            raise FuryPairedRunnerError(
                f"rollout JSONL contains a blank row at line {line_number}"
            )
        try:
            row = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FuryPairedRunnerError(
                f"invalid rollout JSON at {jsonl_path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(row, Mapping):
            raise FuryPairedRunnerError(
                f"rollout JSON row is not an object at {jsonl_path}:{line_number}"
            )
        if raw_line != canonical_json_bytes(row):
            raise FuryPairedRunnerError(
                f"rollout JSON row is not canonical at {jsonl_path}:{line_number}"
            )
        canonical_row = copy.deepcopy(dict(row))
        _validate_rollout_row(
            validated_plan,
            canonical_row,
            expected_shard=shard_index,
        )
        rows.append(canonical_row)

    if manifest.get("rollout_count") != len(rows):
        raise FuryPairedRunnerError("shard manifest rollout count mismatch")
    if len(rows) != int(expected_shard["expected_rollout_count"]):
        raise FuryPairedRunnerError("shard does not contain its exact rollout count")
    expected_keys = {
        (str(group_id), str(policy_id))
        for group_id in expected_shard["group_ids"]
        for policy_id in contract["policy_ids"]
    }
    actual_keys: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["group_id"]), str(row["policy_id"]))
        if key in actual_keys:
            raise FuryPairedRunnerError(f"duplicate rollout key inside shard: {key}")
        actual_keys.add(key)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys.difference(actual_keys))[:5]
        unexpected = sorted(actual_keys.difference(expected_keys))[:5]
        raise FuryPairedRunnerError(
            f"shard rollout Cartesian product mismatch; missing={missing}, unexpected={unexpected}"
        )
    _validate_full_policy_artifact_index(
        validated_plan,
        manifest,
        rows,
        manifest_directory=path.parent,
    )
    return ValidatedShard(path, manifest, tuple(rows))


def _validate_full_policy_artifact_index(
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    manifest_directory: Path,
) -> None:
    contract = _mapping(
        manifest.get("full_policy_artifacts"), "full_policy_artifacts"
    )
    expected_fields = {
        "storage_contract",
        "entry_count",
        "unique_file_count",
        "entries",
        "entries_sha256",
    }
    if set(contract) != expected_fields:
        raise FuryPairedRunnerError("full-policy artifact index field set mismatch")
    if contract.get("storage_contract") != "CONTENT_ADDRESSED_CANONICAL_JSON_SHA256_V1":
        raise FuryPairedRunnerError("unsupported full-policy artifact storage contract")
    entries = _sequence(contract.get("entries"), "full_policy_artifacts.entries")
    if contract.get("entry_count") != len(entries):
        raise FuryPairedRunnerError("full-policy artifact entry count mismatch")
    if contract.get("entries_sha256") != sha256_json(entries):
        raise FuryPairedRunnerError("full-policy artifact entry-set SHA-256 mismatch")
    rows_by_key = {
        (str(row["group_id"]), str(row["policy_id"])): row for row in rows
    }
    scenarios = {
        (str(value["instance_id"]), str(value["scenario_id"])): value
        for value in plan["contract"]["scenarios"]
    }
    groups = {
        str(value["group_id"]): value for value in plan["contract"]["groups"]
    }
    policies = {
        str(value["policy_id"]): value for value in plan["contract"]["policies"]
    }
    observed_keys: set[tuple[str, str]] = set()
    observed_files: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"full_policy_artifacts.entries[{index}]")
        if set(entry) != {
            "group_id",
            "policy_id",
            "row_sha256",
            "sha256",
            "file",
            "size_bytes",
        }:
            raise FuryPairedRunnerError("full-policy artifact entry field set mismatch")
        key = (
            _text(entry.get("group_id"), "artifact entry group_id"),
            _text(entry.get("policy_id"), "artifact entry policy_id"),
        )
        if key in observed_keys:
            raise FuryPairedRunnerError("duplicate full-policy artifact index key")
        observed_keys.add(key)
        row = rows_by_key.get(key)
        if row is None:
            raise FuryPairedRunnerError("full-policy artifact index references no row")
        digest = _lower_sha256(entry.get("sha256"), "artifact entry sha256")
        expected_filename = f"full-policy-artifacts/{digest}.json"
        if entry.get("file") != expected_filename:
            raise FuryPairedRunnerError("full-policy artifact filename is not content-addressed")
        if entry.get("row_sha256") != row["row_sha256"]:
            raise FuryPairedRunnerError("full-policy artifact row binding mismatch")
        if row.get("full_policy_rollout_sha256") != digest:
            raise FuryPairedRunnerError("full-policy artifact digest differs from row")
        artifact_path = (manifest_directory / expected_filename).resolve()
        try:
            artifact_path.relative_to(manifest_directory.resolve())
            payload = artifact_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise FuryPairedRunnerError(
                f"could not read bound full-policy artifact {artifact_path}: {exc}"
            ) from exc
        if entry.get("size_bytes") != len(payload):
            raise FuryPairedRunnerError("full-policy artifact size mismatch")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise FuryPairedRunnerError("full-policy artifact file SHA-256 mismatch")
        try:
            artifact = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FuryPairedRunnerError("full-policy artifact is not strict JSON") from exc
        if not isinstance(artifact, Mapping) or canonical_json_bytes(artifact) != payload:
            raise FuryPairedRunnerError(
                "full-policy artifact is not canonical content-addressed JSON"
            )
        group = groups[key[0]]
        scenario = scenarios[(str(group["instance_id"]), str(group["scenario_id"]))]
        normalized, _ = _normalize_full_policy_executor_result(
            {"full_policy_rollout": artifact},
            group=group,
            scenario=scenario,
            policy=policies[key[1]],
        )
        finalized = _finalize_result_for_plan(
            normalized,
            scenario=scenario,
            execution_mode=str(plan["contract"]["execution_mode"]),
            plan_intent=str(plan["contract"]["plan_intent"]),
        )
        for field, expected in finalized.items():
            if row.get(field) != expected:
                raise FuryPairedRunnerError(
                    f"full-policy artifact-derived rollout {field} mismatch"
                )
        if finalized["full_policy_rollout_sha256"] != digest:
            raise FuryPairedRunnerError("full-policy artifact normalization digest mismatch")
        observed_files.add(expected_filename)

    if contract.get("unique_file_count") != len(observed_files):
        raise FuryPairedRunnerError("full-policy unique artifact file count mismatch")
    expected_keys = (
        set(rows_by_key)
        if plan["contract"]["execution_mode"] == SINGLE_BRIDGE_MODE
        else set()
    )
    if observed_keys != expected_keys:
        raise FuryPairedRunnerError(
            "full-policy artifact index does not cover the exact production row set"
        )


def validate_rollout_rows(
    plan: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]
) -> tuple[JSONMap, ...]:
    """Validate one exact reduced Cartesian product against its runner plan."""

    validated_plan = validate_runner_plan(plan)
    contract = validated_plan["contract"]
    supplied = tuple(rows)
    expected_keys = {
        (str(group["group_id"]), str(policy_id))
        for group in contract["groups"]
        for policy_id in contract["policy_ids"]
    }
    by_key: dict[tuple[str, str], JSONMap] = {}
    for index, raw in enumerate(supplied):
        row = copy.deepcopy(dict(_mapping(raw, f"rollout row {index}")))
        shard_index = _nonnegative_int(
            row.get("shard_index"), f"rollout row {index}.shard_index"
        )
        _validate_rollout_row(validated_plan, row, expected_shard=shard_index)
        key = (
            _text(row.get("group_id"), f"rollout row {index}.group_id"),
            _text(row.get("policy_id"), f"rollout row {index}.policy_id"),
        )
        if key in by_key:
            raise FuryPairedRunnerError(f"duplicate reduced rollout key: {key}")
        by_key[key] = row
    actual_keys = set(by_key)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys.difference(actual_keys))[:5]
        unexpected = sorted(actual_keys.difference(expected_keys))[:5]
        raise FuryPairedRunnerError(
            "reduced rollout Cartesian product mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )
    policy_order = {
        str(policy_id): index for index, policy_id in enumerate(contract["policy_ids"])
    }
    return tuple(
        by_key[key]
        for key in sorted(by_key, key=lambda value: (value[0], policy_order[value[1]]))
    )


def _reduction_physical_evidence(
    plan: Mapping[str, Any],
    ordered_rows: Sequence[Mapping[str, Any]],
    *,
    manifest_paths: Iterable[str | Path] | None,
) -> JSONMap:
    """Require physical artifact validation for every production receipt."""

    if plan["contract"]["execution_mode"] == SYNTHETIC_MODE:
        if manifest_paths is not None:
            tuple(manifest_paths)
        return {"status": "NOT_APPLICABLE_SYNTHETIC", "shards": []}
    if manifest_paths is None:
        raise FuryPairedRunnerError(
            "production reduction receipt requires physical shard manifests"
        )
    paths = tuple(manifest_paths)
    if not paths:
        raise FuryPairedRunnerError(
            "production reduction receipt requires physical shard manifests"
        )
    validated = tuple(validate_shard(plan, path) for path in paths)
    expected_indices = set(range(int(plan["contract"]["shard_count"])))
    if {int(value.manifest["shard_index"]) for value in validated} != expected_indices:
        raise FuryPairedRunnerError(
            "production reduction physical shard set is incomplete"
        )
    expected_rows = {
        (str(row["group_id"]), str(row["policy_id"])): canonical_json_bytes(row)
        for row in ordered_rows
    }
    observed_rows: dict[tuple[str, str], bytes] = {}
    by_shard: dict[int, JSONMap] = {}
    for shard in validated:
        shard_index = int(shard.manifest["shard_index"])
        stable_evidence = {
            "shard_index": shard_index,
            "plan_sha256": shard.manifest["plan_sha256"],
            "group_ids_sha256": shard.manifest["group_ids_sha256"],
            "rollout_jsonl": copy.deepcopy(shard.manifest["rollout_jsonl"]),
            "full_policy_artifacts": copy.deepcopy(
                shard.manifest["full_policy_artifacts"]
            ),
        }
        previous_evidence = by_shard.get(shard_index)
        if previous_evidence is not None and previous_evidence != stable_evidence:
            raise FuryPairedRunnerError(
                "duplicate production shard copies have different physical evidence"
            )
        by_shard[shard_index] = stable_evidence
        for row in shard.rows:
            key = (str(row["group_id"]), str(row["policy_id"]))
            encoded = canonical_json_bytes(row)
            previous = observed_rows.get(key)
            if previous is not None and previous != encoded:
                raise FuryPairedRunnerError(
                    "production physical shards contain conflicting rollout rows"
                )
            observed_rows[key] = encoded
    if observed_rows != expected_rows:
        raise FuryPairedRunnerError(
            "production reduction rows differ from physical shard manifests"
        )
    return {
        "status": "VALIDATED_PHYSICAL_SHARD_MANIFESTS_AND_ARTIFACTS",
        "shards": [by_shard[index] for index in sorted(by_shard)],
    }


def build_reduction_receipt(
    plan: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    manifest_paths: Iterable[str | Path] | None = None,
) -> JSONMap:
    """Content-address the exact plan-ordered statistical input row set."""

    validated_plan = validate_runner_plan(plan)
    contract = validated_plan["contract"]
    ordered_rows = validate_rollout_rows(validated_plan, rows)
    per_policy = {
        str(policy_id): sum(
            row["policy_id"] == policy_id for row in ordered_rows
        )
        for policy_id in contract["policy_ids"]
    }
    artifact_bindings = [
        {
            "group_id": str(row["group_id"]),
            "policy_id": str(row["policy_id"]),
            "full_policy_rollout_sha256": row["full_policy_rollout_sha256"],
        }
        for row in ordered_rows
        if row["full_policy_rollout_sha256"] is not None
    ]
    physical_evidence = _reduction_physical_evidence(
        validated_plan,
        ordered_rows,
        manifest_paths=manifest_paths,
    )
    core: JSONMap = {
        "schema_version": 2,
        "kind": REDUCTION_RECEIPT_KIND,
        "plan_sha256": validated_plan["plan_sha256"],
        "protocol_id": contract["protocol_id"],
        "protocol_sha256": contract["protocol_sha256"],
        "phase": contract["phase"],
        "execution_mode": contract["execution_mode"],
        "plan_intent": contract["plan_intent"],
        "row_set_hash_algorithm": (
            "sha256_canonical_json_plan_ordered_row_sha256_v1"
        ),
        "expected_rollout_count": int(contract["expected_rollout_count"]),
        "unique_rollout_count": len(ordered_rows),
        "per_policy_rollout_count": per_policy,
        "canonical_rollout_set_sha256": sha256_json(
            [str(row["row_sha256"]) for row in ordered_rows]
        ),
        "full_policy_artifact_count": len(artifact_bindings),
        "full_policy_artifact_set_sha256": sha256_json(artifact_bindings),
        "physical_artifact_validation": physical_evidence["status"],
        "shard_physical_evidence": physical_evidence["shards"],
        "shard_physical_evidence_sha256": sha256_json(
            physical_evidence["shards"]
        ),
        "complete_cartesian_product": True,
        "duplicate_conflict_count": 0,
        "statistical_analysis_performed": False,
        "victory_claim_allowed": False,
    }
    return {**core, "receipt_sha256": sha256_json(core)}


def validate_reduction_receipt(
    plan: Mapping[str, Any],
    receipt: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    manifest_paths: Iterable[str | Path] | None = None,
) -> tuple[JSONMap, ...]:
    """Rebuild a reduction receipt and return its canonical validated rows."""

    supplied = dict(_mapping(receipt, "reduction_receipt"))
    expected_fields = {
        "schema_version",
        "kind",
        "plan_sha256",
        "protocol_id",
        "protocol_sha256",
        "phase",
        "execution_mode",
        "plan_intent",
        "row_set_hash_algorithm",
        "expected_rollout_count",
        "unique_rollout_count",
        "per_policy_rollout_count",
        "canonical_rollout_set_sha256",
        "full_policy_artifact_count",
        "full_policy_artifact_set_sha256",
        "physical_artifact_validation",
        "shard_physical_evidence",
        "shard_physical_evidence_sha256",
        "complete_cartesian_product",
        "duplicate_conflict_count",
        "statistical_analysis_performed",
        "victory_claim_allowed",
        "receipt_sha256",
    }
    if set(supplied) != expected_fields:
        raise FuryPairedRunnerError("reduction receipt field set mismatch")
    buffered_rows = tuple(rows)
    expected = build_reduction_receipt(
        plan,
        buffered_rows,
        manifest_paths=manifest_paths,
    )
    if canonical_json_bytes(supplied) != canonical_json_bytes(expected):
        raise FuryPairedRunnerError(
            "reduction receipt differs from the validated plan-ordered row set"
        )
    return validate_rollout_rows(plan, buffered_rows)


def reduce_shards(
    plan: Mapping[str, Any], manifest_paths: Iterable[str | Path]
) -> JSONMap:
    """Validate and reduce shard copies, rejecting missing or conflicting rows.

    Exact duplicate shard copies are deduplicated and counted.  A duplicate key
    with different canonical row content is a hard conflict.
    """

    validated_plan = validate_runner_plan(plan)
    contract = validated_plan["contract"]
    paths = tuple(manifest_paths)
    if not paths:
        raise FuryPairedRunnerError("manifest_paths must not be empty")
    shards = tuple(validate_shard(validated_plan, path) for path in paths)
    observed_shard_indices = {
        int(value.manifest["shard_index"]) for value in shards
    }
    expected_shard_indices = set(range(int(contract["shard_count"])))
    if observed_shard_indices != expected_shard_indices:
        missing = sorted(expected_shard_indices.difference(observed_shard_indices))
        unexpected = sorted(observed_shard_indices.difference(expected_shard_indices))
        raise FuryPairedRunnerError(
            f"reduction shard set is incomplete; missing={missing}, unexpected={unexpected}"
        )

    unique_rows: dict[tuple[str, str], JSONMap] = {}
    row_hashes: dict[tuple[str, str], str] = {}
    exact_duplicate_rows = 0
    shard_copy_counts: dict[int, int] = {}
    for shard in shards:
        shard_index = int(shard.manifest["shard_index"])
        shard_copy_counts[shard_index] = shard_copy_counts.get(shard_index, 0) + 1
        for row in shard.rows:
            key = (str(row["group_id"]), str(row["policy_id"]))
            digest = str(row["row_sha256"])
            previous = row_hashes.get(key)
            if previous is None:
                row_hashes[key] = digest
                unique_rows[key] = row
            elif previous == digest and unique_rows[key] == row:
                exact_duplicate_rows += 1
            else:
                raise FuryPairedRunnerError(
                    f"conflicting duplicate rollout for group/policy {key}"
                )

    expected_keys = {
        (str(group["group_id"]), str(policy_id))
        for group in contract["groups"]
        for policy_id in contract["policy_ids"]
    }
    actual_keys = set(unique_rows)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys.difference(actual_keys))[:5]
        unexpected = sorted(actual_keys.difference(expected_keys))[:5]
        raise FuryPairedRunnerError(
            f"reduction rollout set is incomplete; missing={missing}, unexpected={unexpected}"
        )

    policy_order = {
        str(policy_id): index for index, policy_id in enumerate(contract["policy_ids"])
    }
    ordered_rows = [
        unique_rows[key]
        for key in sorted(
            unique_rows,
            key=lambda value: (value[0], policy_order[value[1]]),
        )
    ]
    per_policy = {
        str(policy_id): sum(
            row["policy_id"] == policy_id for row in ordered_rows
        )
        for policy_id in contract["policy_ids"]
    }
    artifact_bindings = [
        {
            "group_id": str(row["group_id"]),
            "policy_id": str(row["policy_id"]),
            "full_policy_rollout_sha256": row["full_policy_rollout_sha256"],
        }
        for row in ordered_rows
        if row["full_policy_rollout_sha256"] is not None
    ]
    reduction_contract: JSONMap = {
        "schema_version": 2,
        "kind": REDUCTION_KIND,
        "plan_sha256": validated_plan["plan_sha256"],
        "protocol_id": contract["protocol_id"],
        "phase": contract["phase"],
        "plan_intent": contract["plan_intent"],
        "required_shard_count": int(contract["shard_count"]),
        "unique_shard_count": len(observed_shard_indices),
        "input_shard_copy_count": len(shards),
        "duplicate_shard_copy_count": sum(
            max(0, count - 1) for count in shard_copy_counts.values()
        ),
        "expected_rollout_count": int(contract["expected_rollout_count"]),
        "input_rollout_count": sum(len(value.rows) for value in shards),
        "unique_rollout_count": len(ordered_rows),
        "exact_duplicate_rollout_count": exact_duplicate_rows,
        "per_policy_rollout_count": per_policy,
        "canonical_rollout_set_sha256": sha256_json(
            [str(row["row_sha256"]) for row in ordered_rows]
        ),
        "full_policy_artifact_count": len(artifact_bindings),
        "full_policy_artifact_set_sha256": sha256_json(artifact_bindings),
        "complete_cartesian_product": True,
        "duplicate_conflict_count": 0,
        "one_bridge_process_per_worker_contract": True,
        "statistical_analysis_performed": False,
        "victory_claim_allowed": False,
    }
    return {
        **reduction_contract,
        "generated_at": _now(),
        "reduction_sha256": sha256_json(reduction_contract),
        "reduction_receipt": build_reduction_receipt(
            validated_plan,
            ordered_rows,
            manifest_paths=paths,
        ),
        # The statistical analyzer can consume these directly.  It interprets
        # ``seed`` as the master pairing label and independently validates the
        # request-derived ``simulator_seed``.
        "rollout_rows": ordered_rows,
    }


def _lpt_assignment(
    groups: Sequence[Mapping[str, Any]], shard_count: int
) -> tuple[dict[str, int], list[int]]:
    """Assign indivisible groups by deterministic longest-processing-time first."""

    loads = [0 for _ in range(shard_count)]
    counts = [0 for _ in range(shard_count)]
    assignment: dict[str, int] = {}
    ordered = sorted(
        groups,
        key=lambda value: (
            -int(value["estimated_cost_units"]),
            str(value["group_id"]),
        ),
    )
    for group in ordered:
        shard = min(
            range(shard_count),
            key=lambda index: (loads[index], counts[index], index),
        )
        group_id = str(group["group_id"])
        assignment[group_id] = shard
        loads[shard] += int(group["estimated_cost_units"])
        counts[shard] += 1
    return assignment, loads


def _normalize_policy(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "policy")
    result: JSONMap = {
        "policy_id": _text(row.get("policy_id"), "policy.policy_id"),
        "source_sha256": _lower_sha256(
            row.get("source_sha256"), "policy.source_sha256"
        ),
        "adapter_sha256": _lower_sha256(
            row.get("adapter_sha256"), "policy.adapter_sha256"
        ),
        "profile_sha256": _lower_sha256(
            row.get("profile_sha256"), "policy.profile_sha256"
        ),
    }
    role = row.get("role", "UNSPECIFIED")
    result["role"] = _text(role, "policy.role")
    return result


def _strict_json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FuryPairedRunnerError(f"{label} is not strict JSON: {exc}") from exc


def _normalized_limitation_codes(value: Any, label: str) -> list[str]:
    raw_codes = _sequence(value, label)
    codes = [_text(code, f"{label}[{index}]") for index, code in enumerate(raw_codes)]
    if len(set(codes)) != len(codes) or codes != sorted(codes):
        raise FuryPairedRunnerError(f"{label} must be sorted and unique")
    return codes


def _legacy_diagnostic_semantics(
    *, request_sha256: str, target_count: int
) -> tuple[JSONMap, JSONMap]:
    """Make old synthetic fixtures explicit without granting execution status."""

    limitations = [
        "DYNAMIC_SCENARIO_SEMANTICS_UNBOUND",
        "TARGET_CONTEXT_UNBOUND",
    ]
    target_bundle: JSONMap = {
        "schema_version": 2,
        "kind": TARGET_CONTEXT_BUNDLE_KIND,
        "binding_status": "LEGACY_SYNTHETIC_DIAGNOSTIC_UNBOUND",
        "request_sha256": request_sha256,
        "target_count": target_count,
        "contexts": [],
        "comparison_eligible": False,
        "bridge_execution_eligible": False,
        "limitation_codes": limitations,
    }
    target_bundle_sha = sha256_json(target_bundle)
    scenario_model: JSONMap = {
        "schema_version": 2,
        "kind": SCENARIO_MODEL_KIND,
        "model_status": "LEGACY_SYNTHETIC_DIAGNOSTIC_UNBOUND",
        "request_sha256": request_sha256,
        "target_context_bundle_sha256": target_bundle_sha,
        "historical_truth": False,
        "comparison_eligible": False,
        "bridge_execution_eligible": False,
        "dynamic_armor_schedule_status": "UNBOUND",
        "dynamic_attackability_schedule_status": "UNBOUND",
        "health_or_horizon_status": "DURATION_REQUEST_ONLY",
        "dynamic_semantics_receipt": {
            "status": "UNBOUND_LEGACY_SYNTHETIC_DIAGNOSTIC",
            "target_models": [],
        },
        "limitation_codes": limitations,
    }
    return scenario_model, target_bundle


def _normalize_target_context_bundle(
    value: Any,
    *,
    request_sha256: str,
    request: Mapping[str, Any],
) -> JSONMap:
    raw = _mapping(value, "scenario.target_context_bundle")
    expected_fields = {
        "schema_version",
        "kind",
        "binding_status",
        "request_sha256",
        "target_count",
        "contexts",
        "comparison_eligible",
        "bridge_execution_eligible",
        "limitation_codes",
    }
    if set(raw) != expected_fields:
        raise FuryPairedRunnerError(
            "scenario.target_context_bundle field set mismatch"
        )
    if raw.get("schema_version") != 2 or raw.get("kind") != TARGET_CONTEXT_BUNDLE_KIND:
        raise FuryPairedRunnerError("invalid scenario target-context bundle schema")
    if raw.get("request_sha256") != request_sha256:
        raise FuryPairedRunnerError(
            "scenario target-context bundle request SHA-256 mismatch"
        )
    encounter = _mapping(request.get("encounter"), "scenario.request.encounter")
    targets = _sequence(encounter.get("targets"), "scenario.request.encounter.targets")
    target_count = _nonnegative_int(
        raw.get("target_count"), "scenario.target_context_bundle.target_count"
    )
    if target_count != len(targets):
        raise FuryPairedRunnerError(
            "scenario target-context bundle target count differs from request"
        )
    contexts = _strict_json_copy(
        _sequence(raw.get("contexts"), "scenario.target_context_bundle.contexts"),
        "scenario.target_context_bundle.contexts",
    )
    comparison_eligible = _boolean(
        raw.get("comparison_eligible"),
        "scenario.target_context_bundle.comparison_eligible",
    )
    bridge_eligible = _boolean(
        raw.get("bridge_execution_eligible"),
        "scenario.target_context_bundle.bridge_execution_eligible",
    )
    limitations = _normalized_limitation_codes(
        raw.get("limitation_codes"),
        "scenario.target_context_bundle.limitation_codes",
    )
    binding_status = _text(
        raw.get("binding_status"), "scenario.target_context_bundle.binding_status"
    )
    if comparison_eligible:
        if not bridge_eligible or binding_status != "EXACT_COMPARISON" or limitations:
            raise FuryPairedRunnerError(
                "comparison target-context bundle is not exact and limitation-free"
            )
        _validate_exact_target_context_receipts(contexts, request=request)
    elif not limitations:
        raise FuryPairedRunnerError(
            "non-comparison target-context bundle must declare limitations"
        )
    if bridge_eligible and len(contexts) != target_count:
        raise FuryPairedRunnerError(
            "bridge-executable target-context bundle must cover every request target"
        )
    return {
        "schema_version": 2,
        "kind": TARGET_CONTEXT_BUNDLE_KIND,
        "binding_status": binding_status,
        "request_sha256": request_sha256,
        "target_count": target_count,
        "contexts": contexts,
        "comparison_eligible": comparison_eligible,
        "bridge_execution_eligible": bridge_eligible,
        "limitation_codes": limitations,
    }


def _normalize_scenario_model(
    value: Any,
    *,
    request_sha256: str,
    request: Mapping[str, Any],
    target_context_bundle_sha256: str,
    target_context_bundle: Mapping[str, Any],
) -> JSONMap:
    raw = _mapping(value, "scenario.scenario_model")
    expected_fields = {
        "schema_version",
        "kind",
        "model_status",
        "request_sha256",
        "target_context_bundle_sha256",
        "historical_truth",
        "comparison_eligible",
        "bridge_execution_eligible",
        "dynamic_armor_schedule_status",
        "dynamic_attackability_schedule_status",
        "health_or_horizon_status",
        "dynamic_semantics_receipt",
        "limitation_codes",
    }
    if set(raw) != expected_fields:
        raise FuryPairedRunnerError("scenario.scenario_model field set mismatch")
    if raw.get("schema_version") != 2 or raw.get("kind") != SCENARIO_MODEL_KIND:
        raise FuryPairedRunnerError("invalid scenario-model schema")
    if raw.get("request_sha256") != request_sha256:
        raise FuryPairedRunnerError("scenario-model request SHA-256 mismatch")
    if raw.get("target_context_bundle_sha256") != target_context_bundle_sha256:
        raise FuryPairedRunnerError("scenario-model target-context SHA-256 mismatch")
    comparison_eligible = _boolean(
        raw.get("comparison_eligible"), "scenario.scenario_model.comparison_eligible"
    )
    bridge_eligible = _boolean(
        raw.get("bridge_execution_eligible"),
        "scenario.scenario_model.bridge_execution_eligible",
    )
    historical_truth = _boolean(
        raw.get("historical_truth"), "scenario.scenario_model.historical_truth"
    )
    limitations = _normalized_limitation_codes(
        raw.get("limitation_codes"), "scenario.scenario_model.limitation_codes"
    )
    if comparison_eligible != target_context_bundle["comparison_eligible"]:
        raise FuryPairedRunnerError(
            "scenario-model and target-context comparison eligibility differ"
        )
    if bridge_eligible != target_context_bundle["bridge_execution_eligible"]:
        raise FuryPairedRunnerError(
            "scenario-model and target-context bridge eligibility differ"
        )
    model_status = _text(raw.get("model_status"), "scenario.scenario_model.model_status")
    armor_status = _text(
        raw.get("dynamic_armor_schedule_status"),
        "scenario.scenario_model.dynamic_armor_schedule_status",
    )
    attackability_status = _text(
        raw.get("dynamic_attackability_schedule_status"),
        "scenario.scenario_model.dynamic_attackability_schedule_status",
    )
    health_status = _text(
        raw.get("health_or_horizon_status"),
        "scenario.scenario_model.health_or_horizon_status",
    )
    dynamic_receipt = _strict_json_copy(
        _mapping(
            raw.get("dynamic_semantics_receipt"),
            "scenario.scenario_model.dynamic_semantics_receipt",
        ),
        "scenario.scenario_model.dynamic_semantics_receipt",
    )
    if comparison_eligible:
        if model_status != "COMPARISON_BOUND" or limitations:
            raise FuryPairedRunnerError(
                "comparison scenario model is not bound and limitation-free"
            )
        if (
            armor_status != "EXACT_STATIC_SCENARIO"
            or attackability_status != "EXACT_STATIC_SCENARIO"
            or health_status != "EXACT_FIXED_DURATION_SCENARIO"
        ):
            raise FuryPairedRunnerError(
                "comparison scenario contains unbound/proxy/hypothesis dynamic "
                "semantics or an unsupported versioned schedule"
            )
        expected_static_receipt = build_exact_static_request_semantics_receipt(
            request
        )
        if dynamic_receipt != expected_static_receipt:
            raise FuryPairedRunnerError(
                "comparison dynamic-semantics receipt is not the exact static "
                "projection physically present in the bridge request"
            )
    elif not limitations:
        raise FuryPairedRunnerError(
            "non-comparison scenario model must declare limitations"
        )
    return {
        "schema_version": 2,
        "kind": SCENARIO_MODEL_KIND,
        "model_status": model_status,
        "request_sha256": request_sha256,
        "target_context_bundle_sha256": target_context_bundle_sha256,
        "historical_truth": historical_truth,
        "comparison_eligible": comparison_eligible,
        "bridge_execution_eligible": bridge_eligible,
        "dynamic_armor_schedule_status": armor_status,
        "dynamic_attackability_schedule_status": attackability_status,
        "health_or_horizon_status": health_status,
        "dynamic_semantics_receipt": dynamic_receipt,
        "limitation_codes": limitations,
    }


def _normalize_scenario(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "scenario")
    request = _mapping(row.get("request"), "scenario.request")
    request_copy = _strict_json_copy(request, "scenario request")
    horizon = _positive_int(row.get("horizon_ms"), "scenario.horizon_ms")
    encounter = _mapping(request_copy.get("encounter"), "scenario.request.encounter")
    raw_dynamic_config = row.get("dynamic_load_config")
    dynamic_mode = raw_dynamic_config is not None
    expected_use_health = True if dynamic_mode else False
    if encounter.get("useHealth") is not expected_use_health:
        raise FuryPairedRunnerError(
            "dynamic paired scenarios require encounter.useHealth == true"
            if dynamic_mode
            else "static paired scenarios require encounter.useHealth == false"
        )
    duration = _positive_finite(
        encounter.get("duration"), "scenario.request.encounter.duration"
    )
    if abs(duration * 1000.0 - horizon) > 1.0:
        raise FuryPairedRunnerError(
            "scenario request duration does not match horizon_ms"
        )
    request_sha = sha256_json(request_copy)
    dynamic_config = None
    if dynamic_mode:
        try:
            dynamic_contract = dynamic_rollout_load_from_config_wire_v1(
                request_copy,
                0,
                _mapping(
                    raw_dynamic_config,
                    "scenario.dynamic_load_config",
                ),
            )
        except (TypeError, ValueError) as error:
            raise FuryPairedRunnerError(
                f"invalid scenario dynamic-load config: {error}"
            ) from error
        dynamic_config = dynamic_contract.config.to_wire()
    targets = _sequence(encounter.get("targets"), "scenario.request.encounter.targets")
    raw_model = row.get("scenario_model")
    raw_target_bundle = row.get("target_context_bundle")
    if raw_model is None and raw_target_bundle is None:
        scenario_model, target_bundle = _legacy_diagnostic_semantics(
            request_sha256=request_sha,
            target_count=len(targets),
        )
    elif raw_model is None or raw_target_bundle is None:
        raise FuryPairedRunnerError(
            "scenario model and target-context bundle must be supplied together"
        )
    else:
        target_bundle = _normalize_target_context_bundle(
            raw_target_bundle,
            request_sha256=request_sha,
            request=request_copy,
        )
        target_bundle_sha = sha256_json(target_bundle)
        declared_target_sha = row.get("target_context_bundle_sha256")
        if declared_target_sha is not None and declared_target_sha != target_bundle_sha:
            raise FuryPairedRunnerError(
                "declared target-context bundle SHA-256 mismatch"
            )
        scenario_model = _normalize_scenario_model(
            raw_model,
            request_sha256=request_sha,
            request=request_copy,
            target_context_bundle_sha256=target_bundle_sha,
            target_context_bundle=target_bundle,
        )
        declared_model_sha = row.get("scenario_model_sha256")
        if declared_model_sha is not None and declared_model_sha != sha256_json(scenario_model):
            raise FuryPairedRunnerError("declared scenario-model SHA-256 mismatch")
    if dynamic_mode and scenario_model["comparison_eligible"] is True:
        raise FuryPairedRunnerError(
            "dynamic-load scenarios remain non-comparison until a versioned exact "
            "dynamic-semantics receipt is admitted"
        )
    target_bundle_sha = sha256_json(target_bundle)
    scenario_model_sha = sha256_json(scenario_model)
    weight = _positive_finite(row.get("scenario_weight"), "scenario.scenario_weight")
    estimated = row.get("estimated_cost_units", horizon)
    estimated_cost = _positive_int(estimated, "scenario.estimated_cost_units")
    base: JSONMap = {
        "instance_id": _text(row.get("instance_id"), "scenario.instance_id"),
        "component_id": _text(row.get("component_id"), "scenario.component_id"),
        "scenario_id": _text(row.get("scenario_id"), "scenario.scenario_id"),
        "stratum": _text(row.get("stratum"), "scenario.stratum"),
        "scenario_weight": weight,
        "horizon_ms": horizon,
        "estimated_cost_units": estimated_cost,
        "request": request_copy,
        "request_sha256": request_sha,
        "scenario_model": scenario_model,
        "scenario_model_sha256": scenario_model_sha,
        "target_context_bundle": target_bundle,
        "target_context_bundle_sha256": target_bundle_sha,
        "corpus_entry_sha256": _lower_sha256(
            row.get("corpus_entry_sha256"), "scenario.corpus_entry_sha256"
        ),
        "source_scenario_sha256": _lower_sha256(
            row.get("source_scenario_sha256"),
            "scenario.source_scenario_sha256",
        ),
        "catalog_sha256": _lower_sha256(
            row.get("catalog_sha256"), "scenario.catalog_sha256"
        ),
    }
    if dynamic_config is not None:
        base["dynamic_load_config"] = dynamic_config
    if base["stratum"] not in {"single_target", "multi_target"}:
        raise FuryPairedRunnerError(
            "scenario.stratum must be single_target or multi_target"
        )
    base["scenario_contract_sha256"] = sha256_json(base)
    return base


def _normalize_bridge_identity(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "bridge_identity")
    result: JSONMap = {
        "sha256": _lower_sha256(row.get("sha256"), "bridge_identity.sha256"),
        "platform": _text(row.get("platform"), "bridge_identity.platform"),
    }
    if "size_bytes" in row:
        result["size_bytes"] = _positive_int(
            row.get("size_bytes"), "bridge_identity.size_bytes"
        )
    if "build_id" in row:
        result["build_id"] = _text(row.get("build_id"), "bridge_identity.build_id")
    return result


def _normalize_execution_bundle(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "execution_bundle_identity")
    if set(row) != set(EXECUTION_BUNDLE_FIELDS):
        raise FuryPairedRunnerError(
            "execution_bundle_identity must contain exactly "
            f"{list(EXECUTION_BUNDLE_FIELDS)}"
        )
    return {
        field: _lower_sha256(row.get(field), f"execution_bundle_identity.{field}")
        for field in EXECUTION_BUNDLE_FIELDS
    }


def _normalize_executor_result(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "executor result")
    damage = _nonnegative_finite(row.get("damage"), "executor damage")
    dps = _nonnegative_finite(row.get("dps"), "executor dps")
    elapsed = _positive_int(row.get("elapsed_ms"), "executor elapsed_ms")
    completion = _boolean(
        row.get("completion_criterion_met"), "executor completion_criterion_met"
    )
    eligible = _boolean(row.get("evaluation_eligible"), "executor evaluation_eligible")
    omitted = _nonnegative_int(
        row.get("omitted_lane_count"), "executor omitted_lane_count"
    )
    fatal = _nonnegative_int(row.get("fatal_error_count"), "executor fatal_error_count")
    end_digest = _lower_sha256(
        row.get("end_state_sha256"), "executor end_state_sha256"
    )
    computed_dps = damage * 1000.0 / elapsed
    if abs(dps - computed_dps) > max(1e-6, abs(computed_dps) * 1e-9):
        raise FuryPairedRunnerError(
            "executor dps is inconsistent with damage and elapsed_ms"
        )
    reasons = row.get("nonfaithful_reason_counts", {})
    reason_map = _mapping(reasons, "executor nonfaithful_reason_counts")
    normalized_reasons: dict[str, int] = {}
    for key, count in reason_map.items():
        reason = _text(key, "nonfaithful reason")
        normalized_reasons[reason] = _nonnegative_int(
            count, f"nonfaithful reason {reason}"
        )
    return {
        "damage": damage,
        "elapsed_ms": elapsed,
        "dps": dps,
        "completion_criterion_met": completion,
        "evaluation_eligible": eligible,
        "omitted_lane_count": omitted,
        "fatal_error_count": fatal,
        "nonfaithful_reason_counts": dict(sorted(normalized_reasons.items())),
        "end_state_sha256": end_digest,
    }


def _finalize_result_for_plan(
    value: Mapping[str, Any],
    *,
    scenario: Mapping[str, Any],
    execution_mode: str,
    plan_intent: str,
) -> JSONMap:
    """Separate a complete test contract from scientific eligibility."""

    result = copy.deepcopy(dict(value))
    dynamic_scenario = "dynamic_load_config" in scenario
    if (
        not dynamic_scenario
        and result["completion_criterion_met"] is True
        and int(result["elapsed_ms"]) != int(scenario["horizon_ms"])
    ):
        raise FuryPairedRunnerError(
            "a completed duration-mode rollout must reach its exact planned horizon_ms"
        )
    if (
        dynamic_scenario
        and result["completion_criterion_met"] is True
        and int(result["elapsed_ms"]) > int(scenario["horizon_ms"])
    ):
        raise FuryPairedRunnerError(
            "a completed dynamic rollout exceeded its watchdog horizon_ms"
        )
    completed_contract = (
        result["completion_criterion_met"] is True
        and (
            int(result["elapsed_ms"]) <= int(scenario["horizon_ms"])
            if dynamic_scenario
            else int(result["elapsed_ms"]) == int(scenario["horizon_ms"])
        )
    )
    contract_complete = (
        completed_contract
        and int(result["omitted_lane_count"]) == 0
        and int(result["fatal_error_count"]) == 0
    )
    original_reasons = dict(result["nonfaithful_reason_counts"])
    executor_derived_eligible = contract_complete and not any(
        int(count) > 0 for count in original_reasons.values()
    )
    if result["evaluation_eligible"] is not executor_derived_eligible:
        raise FuryPairedRunnerError(
            "executor evaluation_eligible does not match its raw contract result"
        )

    reasons = dict(original_reasons)
    if execution_mode == SYNTHETIC_MODE:
        reasons[SYNTHETIC_NONVOTING_REASON] = 1
    if plan_intent == DIAGNOSTIC_INTENT:
        reasons[DIAGNOSTIC_NONVOTING_REASON] = 1
    scenario_comparison_eligible = (
        scenario["scenario_model"]["comparison_eligible"] is True
        and scenario["target_context_bundle"]["comparison_eligible"] is True
    )
    scientific_eligible = (
        executor_derived_eligible
        and execution_mode == SINGLE_BRIDGE_MODE
        and plan_intent == COMPARISON_INTENT
        and scenario_comparison_eligible
    )
    result["contract_complete"] = contract_complete
    result["evaluation_eligible"] = scientific_eligible
    result["nonfaithful_reason_counts"] = dict(sorted(reasons.items()))
    return result


def _validate_full_policy_command_and_claim_contract(
    artifact: Mapping[str, Any],
    *,
    dynamic_scenario: bool,
) -> None:
    command_contract = _mapping(
        artifact.get("bridge_command_contract"),
        "full_policy_rollout.bridge_command_contract",
    )
    expected_command_contract = copy.deepcopy(_FULL_POLICY_COMMAND_CONTRACT)
    if dynamic_scenario:
        expected_command_contract["initial_load_command"] = "load_dynamic_v1"
    if canonical_json_bytes(command_contract) != canonical_json_bytes(
        expected_command_contract
    ):
        raise FuryPairedRunnerError(
            "full-policy bridge command contract differs from the executor contract"
        )
    claims = _sequence(
        artifact.get("claims_excluded"), "full_policy_rollout.claims_excluded"
    )
    if canonical_json_bytes(claims) != canonical_json_bytes(
        _FULL_POLICY_CLAIMS_EXCLUDED
    ):
        raise FuryPairedRunnerError(
            "full-policy claim boundaries differ from the executor contract"
        )


def _validate_exact_target_context_receipts(
    contexts: Sequence[Any],
    *,
    request: Mapping[str, Any],
) -> None:
    encounter = _mapping(request.get("encounter"), "scenario request encounter")
    request_targets = _sequence(
        encounter.get("targets"), "scenario request encounter.targets"
    )
    expected_indices = set(range(len(request_targets)))
    observed_indices: set[int] = set()
    observed_context_ids: set[str] = set()
    classifications = {
        "worldboss",
        "rareelite",
        "elite",
        "rare",
        "normal",
        "trivial",
        "minus",
    }
    evidence_kinds = {
        "OBSERVED_SOURCE",
        "PINNED_STATIC_INPUT",
        "SIMULATOR_STATE",
    }
    for index, raw_context in enumerate(contexts):
        context = _mapping(
            raw_context, f"full_policy_rollout target-context receipt {index}"
        )
        if set(context) != _TARGET_CONTEXT_RECEIPT_FIELDS:
            raise FuryPairedRunnerError(
                "full-policy target-context receipt field set mismatch"
            )
        context_id = _text(
            context.get("context_id"),
            f"full_policy_rollout target-context receipt {index}.context_id",
        )
        target_index = _nonnegative_int(
            context.get("target_index"),
            f"full_policy_rollout target-context receipt {index}.target_index",
        )
        if target_index != index:
            raise FuryPairedRunnerError(
                "full-policy target-context receipts are not in target-index order"
            )
        if context_id in observed_context_ids or target_index in observed_indices:
            raise FuryPairedRunnerError(
                "full-policy target-context receipts contain duplicate identities"
            )
        observed_context_ids.add(context_id)
        observed_indices.add(target_index)
        if (
            context.get("mode") != "DECLARED_EXACT"
            or context.get("exact_by_declared_contract") is not True
            or context.get("target_max_health") is not None
            or context.get("health_pct_schedule") != []
        ):
            raise FuryPairedRunnerError(
                "comparison-eligible target-context receipt is not declared exact"
            )
        if context.get("target_classification") not in classifications:
            raise FuryPairedRunnerError(
                "full-policy target-context classification is invalid"
            )
        _text(
            context.get("target_name"),
            f"full_policy_rollout target-context receipt {index}.target_name",
        )
        _nonnegative_int(
            context.get("equipped_item_count"),
            f"full_policy_rollout target-context receipt {index}.equipped_item_count",
        )
        evidence = _mapping(
            context.get("field_evidence"),
            f"full_policy_rollout target-context receipt {index}.field_evidence",
        )
        if set(evidence) != _TARGET_EVIDENCE_FIELDS:
            raise FuryPairedRunnerError(
                "full-policy target-context field evidence set mismatch"
            )
        for field, raw_evidence in evidence.items():
            evidence_row = _mapping(
                raw_evidence,
                f"full_policy_rollout target-context receipt {index}.{field}",
            )
            if set(evidence_row) != _FIELD_EVIDENCE_FIELDS:
                raise FuryPairedRunnerError(
                    "full-policy target-context evidence field set mismatch"
                )
            if evidence_row.get("schema") != "contra_field_evidence/v2":
                raise FuryPairedRunnerError(
                    "full-policy target-context evidence schema mismatch"
                )
            kind = evidence_row.get("kind")
            if kind not in evidence_kinds:
                raise FuryPairedRunnerError(
                    "comparison-eligible target-context evidence is not exact"
                )
            if (
                field in {"target_health_pct", "target_max_health"}
                and kind != "SIMULATOR_STATE"
            ):
                raise FuryPairedRunnerError(
                    "exact target health evidence must come from simulator state"
                )
            if evidence_row.get("hypothesis_id") is not None:
                raise FuryPairedRunnerError(
                    "exact target-context evidence cannot carry a hypothesis"
                )
            source_digest = evidence_row.get("source_sha256")
            corpus_digest = evidence_row.get("corpus_sha256")
            for label, digest in (
                ("source_sha256", source_digest),
                ("corpus_sha256", corpus_digest),
            ):
                if digest is not None:
                    _lower_sha256(
                        digest,
                        "full_policy_rollout target-context evidence " + label,
                    )
            if source_digest is None and corpus_digest is None:
                raise FuryPairedRunnerError(
                    "exact target-context evidence lacks a content identity"
                )
    if observed_indices != expected_indices:
        raise FuryPairedRunnerError(
            "full-policy target-context receipts do not cover every request target"
        )


def _validate_step_target_semantics(
    value: Any,
    *,
    expected_contexts: Sequence[Any],
    label: str,
) -> None:
    target = _mapping(value, label)
    target_index = _nonnegative_int(target.get("target_index"), f"{label}.target_index")
    if target_index >= len(expected_contexts):
        raise FuryPairedRunnerError("full-policy step target index is outside the plan")
    expected = _mapping(expected_contexts[target_index], "planned target context")
    exact_fields = {
        "context_id": "context_id",
        "mode": "mode",
        "target_index": "target_index",
        "target_classification": "target_classification",
        "target_name": "target_name",
        "exact_by_declared_contract": "exact_by_declared_contract",
        "field_evidence": "field_evidence",
    }
    for observed_field, expected_field in exact_fields.items():
        if canonical_json_bytes(target.get(observed_field)) != canonical_json_bytes(
            expected.get(expected_field)
        ):
            raise FuryPairedRunnerError(
                f"full-policy step {observed_field} differs from frozen target context"
            )
    equipped_names = _sequence(
        target.get("equipped_item_names"), f"{label}.equipped_item_names"
    )
    if len(equipped_names) != expected.get("equipped_item_count"):
        raise FuryPairedRunnerError(
            "full-policy step equipment differs from frozen target context"
        )
    health_pct = _nonnegative_finite(
        target.get("target_health_pct"), f"{label}.target_health_pct"
    )
    if health_pct > 100:
        raise FuryPairedRunnerError("full-policy step target health percent exceeds 100")
    if _positive_finite(
        target.get("target_max_health"), f"{label}.target_max_health"
    ) <= 0:
        raise FuryPairedRunnerError("full-policy step target max health is invalid")


def _validate_full_policy_dynamic_load_binding(
    artifact: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
) -> None:
    raw_config = scenario.get("dynamic_load_config")
    raw_binding = artifact.get("dynamic_load_binding")
    if raw_config is None:
        if raw_binding is not None:
            raise FuryPairedRunnerError(
                "static scenario full-policy artifact claims a dynamic load"
            )
        if "dynamic_load_contract_sha256" in group:
            raise FuryPairedRunnerError(
                "static paired group claims a dynamic load contract"
            )
        return
    if not isinstance(raw_binding, Mapping):
        raise FuryPairedRunnerError(
            "dynamic scenario full-policy artifact lacks dynamic_load_binding"
        )
    try:
        expected = dynamic_rollout_load_from_config_wire_v1(
            scenario["request"],
            int(group["simulator_seed"]),
            _mapping(raw_config, "scenario.dynamic_load_config"),
        )
    except (TypeError, ValueError) as error:
        raise FuryPairedRunnerError(
            f"failed to reconstruct planned dynamic load binding: {error}"
        ) from error
    if group.get("dynamic_load_contract_sha256") != expected.contract_sha256:
        raise FuryPairedRunnerError(
            "paired group dynamic-load contract SHA-256 mismatch"
        )
    expected_fields = {
        "schema",
        "contract_sha256",
        "request_sha256",
        "simulator_seed",
        "config_digest",
        "target_count",
        "background_event_count",
        "same_timestamp_order",
        "retarget_mode",
        "load_succeeded",
        "bridge_receipt",
        "target_health_ieee754_binary64_hex",
    }
    if set(raw_binding) != expected_fields:
        raise FuryPairedRunnerError(
            "full-policy dynamic-load binding field set mismatch"
        )
    config = expected.config
    expected_values = {
        "schema": DYNAMIC_LOAD_BINDING_SCHEMA,
        "contract_sha256": expected.contract_sha256,
        "request_sha256": expected.request_sha256,
        "simulator_seed": expected.seed,
        "config_digest": config.content_sha256,
        "target_count": len(config.target_health),
        "background_event_count": len(config.background_damage_events),
        "same_timestamp_order": config.same_timestamp_order,
        "retarget_mode": config.retarget_mode,
        "load_succeeded": True,
        "target_health_ieee754_binary64_hex": [
            struct.pack(">d", target.health).hex()
            for target in config.target_health
        ],
    }
    for field, expected_value in expected_values.items():
        if raw_binding.get(field) != expected_value:
            raise FuryPairedRunnerError(
                f"full-policy dynamic-load binding {field} mismatch"
            )
    bridge_receipt = _mapping(
        raw_binding.get("bridge_receipt"),
        "full_policy_rollout.dynamic_load_binding.bridge_receipt",
    )
    expected_receipt_fields = {
        "schema",
        "config_digest",
        "environment_generation",
        "target_count",
        "background_event_count",
        "same_timestamp_order",
        "retarget_mode",
    }
    if set(bridge_receipt) != expected_receipt_fields:
        raise FuryPairedRunnerError(
            "full-policy dynamic bridge receipt field set mismatch"
        )
    _positive_int(
        bridge_receipt.get("environment_generation"),
        "full-policy dynamic environment_generation",
    )
    expected_receipt = {
        "schema": "o2o_dynamic_team_background/v1",
        "config_digest": config.content_sha256,
        "target_count": len(config.target_health),
        "background_event_count": len(config.background_damage_events),
        "same_timestamp_order": config.same_timestamp_order,
        "retarget_mode": config.retarget_mode,
    }
    for field, expected_value in expected_receipt.items():
        if bridge_receipt.get(field) != expected_value:
            raise FuryPairedRunnerError(
                f"full-policy dynamic bridge receipt {field} mismatch"
            )


def _normalize_full_policy_executor_result(
    value: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[JSONMap, JSONMap]:
    """Derive a production row from a complete full-policy artifact.

    Production workers are not allowed to submit the compact DPS summary used
    by synthetic tests.  The runner recomputes that summary from the detailed
    v2 full-policy artifact and binds the artifact's content hash into the row.
    """

    envelope = _mapping(value, "production executor result")
    if set(envelope) != {"full_policy_rollout"}:
        raise FuryPairedRunnerError(
            "production executor must return only full_policy_rollout"
        )
    artifact = _mapping(
        envelope.get("full_policy_rollout"), "full_policy_rollout"
    )
    if artifact.get("schema") != FULL_POLICY_ROLLOUT_SCHEMA:
        raise FuryPairedRunnerError("invalid full-policy rollout schema")
    expected_artifact_fields = {
        "schema",
        "status",
        "expert_id",
        "seed",
        "request_sha256",
        "source_execution",
        "exact_lua_replay",
        "fallback",
        "scenario_complete",
        "ordered_projection_faithful",
        "simulator_dps_comparison_eligible",
        "bridge_capabilities",
        "bridge_command_contract",
        "blockers",
        "blocker_summary",
        "final_state",
        "claims_excluded",
        "root_state",
        "bridge_scenario_finished",
        "configured_completion",
        "decision_count",
        "advance_count",
        "elapsed_ms",
        "damage_delta",
        "diagnostic_dps",
        "target_context_receipts",
        "autonomous_advance_observations",
        "steps_retained",
        "steps",
    }
    dynamic_scenario = "dynamic_load_config" in scenario
    if dynamic_scenario:
        expected_artifact_fields.add("dynamic_load_binding")
    if set(artifact) != expected_artifact_fields:
        missing = sorted(expected_artifact_fields.difference(artifact))
        unexpected = sorted(set(artifact).difference(expected_artifact_fields))
        raise FuryPairedRunnerError(
            "full-policy rollout field set mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )
    if artifact.get("request_sha256") != scenario["request_sha256"]:
        raise FuryPairedRunnerError(
            "full-policy rollout request SHA-256 differs from the scenario"
        )
    if artifact.get("seed") != group["simulator_seed"]:
        raise FuryPairedRunnerError(
            "full-policy rollout seed differs from the request-derived simulator seed"
        )
    _validate_full_policy_dynamic_load_binding(
        artifact,
        group=group,
        scenario=scenario,
    )
    policy_id = str(policy["policy_id"])
    expected_expert_id = _POLICY_TO_FULL_ROLLOUT_EXPERT_ID.get(
        policy_id, policy_id
    )
    if artifact.get("expert_id") != expected_expert_id:
        raise FuryPairedRunnerError(
            "full-policy rollout expert identity differs from the planned policy"
        )

    fallback = _mapping(artifact.get("fallback"), "full_policy_rollout.fallback")
    if fallback.get("used") is not False or fallback.get("allowed") is not False:
        raise FuryPairedRunnerError("full-policy rollout used or allowed a fallback")
    if artifact.get("source_execution") is not False:
        raise FuryPairedRunnerError(
            "source-derived full-policy rollout cannot claim original Lua execution"
        )
    if artifact.get("exact_lua_replay") is not False:
        raise FuryPairedRunnerError(
            "source-derived full-policy rollout cannot claim exact Lua replay"
        )
    _validate_full_policy_command_and_claim_contract(
        artifact,
        dynamic_scenario=dynamic_scenario,
    )
    blockers = _sequence(artifact.get("blockers"), "full_policy_rollout.blockers")
    blocker_codes: list[str] = []
    fatal_count = 0
    for index, raw_blocker in enumerate(blockers):
        blocker = _mapping(raw_blocker, f"full_policy_rollout.blockers[{index}]")
        code = _text(
            blocker.get("code"), f"full_policy_rollout.blockers[{index}].code"
        )
        if blocker.get("comparison_fatal") is not True:
            raise FuryPairedRunnerError(
                "every full-policy blocker must be comparison-fatal"
            )
        execution_fatal = _boolean(
            blocker.get("execution_fatal"),
            f"full_policy_rollout.blockers[{index}].execution_fatal",
        )
        blocker_codes.append(code)
        fatal_count += int(execution_fatal)

    scenario_complete = _boolean(
        artifact.get("scenario_complete"),
        "full_policy_rollout.scenario_complete",
    )
    ordered_faithful = _boolean(
        artifact.get("ordered_projection_faithful"),
        "full_policy_rollout.ordered_projection_faithful",
    )
    reported_eligible = _boolean(
        artifact.get("simulator_dps_comparison_eligible"),
        "full_policy_rollout.simulator_dps_comparison_eligible",
    )
    derived_eligible = (
        artifact.get("status") == "COMPLETE_FAITHFUL"
        and scenario_complete
        and not blockers
    )
    if ordered_faithful is not derived_eligible or reported_eligible is not derived_eligible:
        raise FuryPairedRunnerError(
            "full-policy rollout faithful/eligibility flags differ from its detailed blockers"
        )
    capabilities = _mapping(
        artifact.get("bridge_capabilities"),
        "full_policy_rollout.bridge_capabilities",
    )
    expected_capability_names = {
        "load",
        "state",
        "actions",
        "act",
        "cancel_queue",
        "set_target",
        "wait",
        "advance",
        "start_attack",
        "stop_cast",
        "server_results_since_last_decision",
    }
    if dynamic_scenario:
        expected_capability_names.add("load_dynamic_v1")
    if set(capabilities) != expected_capability_names or any(
        not isinstance(value, bool) for value in capabilities.values()
    ):
        raise FuryPairedRunnerError(
            "full-policy rollout bridge capability receipt is incomplete"
        )
    required_capability_names = expected_capability_names - (
        {"load"} if dynamic_scenario else set()
    )
    if derived_eligible and not all(
        capabilities[name] for name in required_capability_names
    ):
        raise FuryPairedRunnerError(
            "comparison-eligible full-policy rollout lacks a bridge capability"
        )

    blocker_summary = _sequence(
        artifact.get("blocker_summary"),
        "full_policy_rollout.blocker_summary",
    )
    expected_blocker_summary = [
        {"code": code, "count": count}
        for code, count in sorted(Counter(blocker_codes).items())
    ]
    if blocker_summary != expected_blocker_summary:
        raise FuryPairedRunnerError(
            "full-policy blocker summary differs from detailed blockers"
        )

    elapsed = _positive_int(
        artifact.get("elapsed_ms"), "full_policy_rollout.elapsed_ms"
    )
    damage = _nonnegative_finite(
        artifact.get("damage_delta"), "full_policy_rollout.damage_delta"
    )
    diagnostic_dps = _nonnegative_finite(
        artifact.get("diagnostic_dps"), "full_policy_rollout.diagnostic_dps"
    )
    configured_completion = _mapping(
        artifact.get("configured_completion"),
        "full_policy_rollout.configured_completion",
    )
    if dynamic_scenario:
        if (
            configured_completion.get("mode") != "DYNAMIC_ALL_TARGETS_DEAD"
            or configured_completion.get("watchdog_horizon_ms")
            != scenario["horizon_ms"]
            or configured_completion.get("criterion_met") is not scenario_complete
            or configured_completion.get("bridge_finished")
            is not artifact.get("bridge_scenario_finished")
            or configured_completion.get("elapsed_ms") != elapsed
            or configured_completion.get("target_count")
            != len(scenario["request"]["encounter"]["targets"])
            or (
                scenario_complete
                and (
                    configured_completion.get("all_targets_dead") is not True
                    or configured_completion.get("live_target_count") != 0
                    or configured_completion.get("within_watchdog_horizon") is not True
                )
            )
        ):
            raise FuryPairedRunnerError(
                "full-policy dynamic completion receipt differs from the planned load/watchdog"
            )
    elif (
        configured_completion.get("mode") != "CONFIGURED_DURATION"
        or configured_completion.get("configured_duration_ms")
        != scenario["horizon_ms"]
        or configured_completion.get("criterion_met") is not scenario_complete
        or configured_completion.get("bridge_finished")
        is not artifact.get("bridge_scenario_finished")
    ):
        raise FuryPairedRunnerError(
            "full-policy configured completion receipt differs from the planned horizon"
        )
    if scenario_complete and artifact.get("bridge_scenario_finished") is not True:
        raise FuryPairedRunnerError(
            "a complete full-policy rollout requires the bridge to be finished"
        )
    contexts = _sequence(
        artifact.get("target_context_receipts"),
        "full_policy_rollout.target_context_receipts",
    )
    target_bundle = _mapping(
        scenario.get("target_context_bundle"), "scenario.target_context_bundle"
    )
    expected_contexts = _sequence(
        target_bundle.get("contexts"), "scenario.target_context_bundle.contexts"
    )
    if not expected_contexts:
        raise FuryPairedRunnerError(
            "production scenario lacks a frozen target-context bundle"
        )
    if canonical_json_bytes(contexts) != canonical_json_bytes(expected_contexts):
        raise FuryPairedRunnerError(
            "full-policy target_context_receipts do not byte-match the frozen plan bundle"
        )
    if target_bundle.get("comparison_eligible") is True:
        _validate_exact_target_context_receipts(
            contexts,
            request=scenario["request"],
        )
    if artifact.get("steps_retained") is not True:
        raise FuryPairedRunnerError(
            "production full-policy rollout must retain its decision steps"
        )
    decision_count = _positive_int(
        artifact.get("decision_count"), "full_policy_rollout.decision_count"
    )
    _nonnegative_int(
        artifact.get("advance_count"), "full_policy_rollout.advance_count"
    )
    steps = _sequence(artifact.get("steps"), "full_policy_rollout.steps")
    if len(steps) != decision_count:
        raise FuryPairedRunnerError(
            "full-policy retained step count differs from decision_count"
        )
    previous_next_state: Mapping[str, Any] | None = None
    for index, raw_step in enumerate(steps):
        step = _mapping(raw_step, f"full_policy_rollout.steps[{index}]")
        if set(step) != _FULL_POLICY_STEP_FIELDS:
            raise FuryPairedRunnerError(
                "full-policy retained step field set mismatch"
            )
        if step.get("decision_index") != index:
            raise FuryPairedRunnerError(
                "full-policy decision indices are not contiguous"
            )
        audit = _mapping(
            step.get("independent_ordered_audit"),
            f"full_policy_rollout.steps[{index}].independent_ordered_audit",
        )
        if audit.get("derived_from_sink_events") is not True:
            raise FuryPairedRunnerError(
                "full-policy step lacks the independent ordered audit"
            )
        proposal = _mapping(
            step.get("proposal"), f"full_policy_rollout.steps[{index}].proposal"
        )
        if proposal.get("expert_id") != expected_expert_id:
            raise FuryPairedRunnerError(
                "full-policy step proposal identity differs from its policy"
            )
        step_blockers = _sequence(
            step.get("blockers"), f"full_policy_rollout.steps[{index}].blockers"
        )
        if (
            audit.get("executor_faithful_flag_trusted") is not False
            or audit.get("executor_blocked_flag_trusted") is not False
            or canonical_json_bytes(audit.get("blockers"))
            != canonical_json_bytes(step_blockers)
            or audit.get("faithful") is not (not step_blockers)
        ):
            raise FuryPairedRunnerError(
                "full-policy independent ordered audit is internally inconsistent"
            )
        _validate_step_target_semantics(
            step.get("target_semantics"),
            expected_contexts=expected_contexts,
            label=f"full_policy_rollout.steps[{index}].target_semantics",
        )
        before = _mapping(
            step.get("simulator_state_before"),
            f"full_policy_rollout.steps[{index}].simulator_state_before",
        )
        after_commands = _mapping(
            step.get("simulator_state_after_commands"),
            f"full_policy_rollout.steps[{index}].simulator_state_after_commands",
        )
        next_epoch = _mapping(
            step.get("simulator_state_next_epoch"),
            f"full_policy_rollout.steps[{index}].simulator_state_next_epoch",
        )
        if previous_next_state is not None and canonical_json_bytes(
            before
        ) != canonical_json_bytes(previous_next_state):
            raise FuryPairedRunnerError(
                "full-policy retained step state chain is discontinuous"
            )
        previous_next_state = next_epoch
        before_time = _nonnegative_int(
            before.get("time_ms"), "full-policy step before time_ms"
        )
        after_time = _nonnegative_int(
            after_commands.get("time_ms"), "full-policy step after-command time_ms"
        )
        next_time = _nonnegative_int(
            next_epoch.get("time_ms"), "full-policy step next time_ms"
        )
        before_damage = _nonnegative_finite(
            before.get("damage_done"), "full-policy step before damage_done"
        )
        after_damage = _nonnegative_finite(
            after_commands.get("damage_done"),
            "full-policy step after-command damage_done",
        )
        next_damage = _nonnegative_finite(
            next_epoch.get("damage_done"), "full-policy step next damage_done"
        )
        if (
            after_time < before_time
            or next_time < after_time
            or after_damage < before_damage
            or next_damage < after_damage
        ):
            raise FuryPairedRunnerError(
                "full-policy step simulator state is not monotone"
            )
        if step.get("time_delta_ms") != next_time - before_time:
            raise FuryPairedRunnerError("full-policy step time delta mismatch")
        reported_damage_delta = _nonnegative_finite(
            step.get("damage_delta"), "full-policy step damage_delta"
        )
        if abs(reported_damage_delta - (next_damage - before_damage)) > max(
            1e-6, abs(next_damage - before_damage) * 1e-9
        ):
            raise FuryPairedRunnerError("full-policy step damage delta mismatch")
        _sequence(
            step.get("available_actions_before"),
            f"full_policy_rollout.steps[{index}].available_actions_before",
        )
        _mapping(
            step.get("ordered_execution"),
            f"full_policy_rollout.steps[{index}].ordered_execution",
        )
        if derived_eligible and (
            step_blockers
            or audit.get("faithful") is not True
            or audit.get("blockers") != []
        ):
            raise FuryPairedRunnerError(
                "comparison-eligible full-policy step is not independently faithful"
            )
    root_state = _mapping(
        artifact.get("root_state"), "full_policy_rollout.root_state"
    )
    final_state = _mapping(
        artifact.get("final_state"), "full_policy_rollout.final_state"
    )
    if canonical_json_bytes(steps[0]["simulator_state_before"]) != canonical_json_bytes(
        root_state
    ) or canonical_json_bytes(steps[-1]["simulator_state_next_epoch"]) != canonical_json_bytes(
        final_state
    ):
        raise FuryPairedRunnerError(
            "full-policy retained steps do not span root_state through final_state"
        )
    root_time = _nonnegative_int(
        root_state.get("time_ms"), "full_policy_rollout.root_state.time_ms"
    )
    final_time = _nonnegative_int(
        final_state.get("time_ms"), "full_policy_rollout.final_state.time_ms"
    )
    if final_time - root_time != elapsed:
        raise FuryPairedRunnerError(
            "full-policy rollout elapsed_ms differs from its state transition"
        )
    root_damage = _nonnegative_finite(
        root_state.get("damage_done"),
        "full_policy_rollout.root_state.damage_done",
    )
    final_damage = _nonnegative_finite(
        final_state.get("damage_done"),
        "full_policy_rollout.final_state.damage_done",
    )
    if abs((final_damage - root_damage) - damage) > max(
        1e-6, abs(damage) * 1e-9
    ):
        raise FuryPairedRunnerError(
            "full-policy rollout damage_delta differs from its state transition"
        )

    reason_counts = dict(sorted(Counter(blocker_codes).items()))
    summary = _normalize_executor_result(
        {
            "damage": damage,
            "elapsed_ms": elapsed,
            "dps": diagnostic_dps,
            "completion_criterion_met": scenario_complete,
            "evaluation_eligible": derived_eligible,
            "omitted_lane_count": 0,
            "fatal_error_count": fatal_count,
            "nonfaithful_reason_counts": reason_counts,
            "end_state_sha256": sha256_json(final_state),
        }
    )
    artifact_copy = _strict_json_copy(dict(artifact), "full-policy rollout artifact")
    return (
        {
            **summary,
            "full_policy_rollout_sha256": sha256_json(artifact_copy),
        },
        artifact_copy,
    )


def _validate_rollout_row(
    plan: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    expected_shard: int,
) -> None:
    contract = plan["contract"]
    if row.get("schema_version") != 2 or row.get("kind") != ROLLOUT_KIND:
        raise FuryPairedRunnerError("invalid rollout row schema or kind")
    if row.get("plan_sha256") != plan["plan_sha256"]:
        raise FuryPairedRunnerError("rollout plan SHA-256 mismatch")
    for field in (
        "protocol_id",
        "protocol_sha256",
        "corpus_manifest_sha256",
        "runner_inputs_sha256",
        "corpus_binding_sha256",
        "phase",
        "plan_intent",
    ):
        if row.get(field) != contract[field]:
            raise FuryPairedRunnerError(f"rollout {field} mismatch")
    if row.get("bridge_sha256") != contract["bridge_identity"]["sha256"]:
        raise FuryPairedRunnerError("rollout bridge SHA-256 mismatch")
    if row.get("execution_bundle_sha256") != contract["execution_bundle_sha256"]:
        raise FuryPairedRunnerError("rollout execution bundle SHA-256 mismatch")
    if row.get("execution_mode") != contract["execution_mode"]:
        raise FuryPairedRunnerError("rollout execution_mode mismatch")
    if contract["execution_mode"] == SINGLE_BRIDGE_MODE:
        _lower_sha256(
            row.get("full_policy_rollout_sha256"),
            "rollout full_policy_rollout_sha256",
        )
    elif row.get("full_policy_rollout_sha256") is not None:
        raise FuryPairedRunnerError(
            "synthetic rollout must not claim a full-policy artifact"
        )
    if row.get("shard_index") != expected_shard:
        raise FuryPairedRunnerError("rollout shard index mismatch")

    group_id = _text(row.get("group_id"), "rollout group_id")
    groups = {str(value["group_id"]): value for value in contract["groups"]}
    group = groups.get(group_id)
    if group is None or int(group["shard_index"]) != expected_shard:
        raise FuryPairedRunnerError("rollout group is absent or assigned to another shard")
    scenarios = {
        (str(value["instance_id"]), str(value["scenario_id"])): value
        for value in contract["scenarios"]
    }
    scenario = scenarios[(str(group["instance_id"]), str(group["scenario_id"]))]
    policies = {str(value["policy_id"]): value for value in contract["policies"]}
    policy_id = _text(row.get("policy_id"), "rollout policy_id")
    policy = policies.get(policy_id)
    if policy is None:
        raise FuryPairedRunnerError("rollout policy is not in the plan")

    expected_values = {
        "master_seed": group["master_seed"],
        "seed": group["master_seed"],
        "simulator_seed": group["simulator_seed"],
        "instance_id": scenario["instance_id"],
        "component_id": scenario["component_id"],
        "scenario_id": scenario["scenario_id"],
        "stratum": scenario["stratum"],
        "scenario_weight": scenario["scenario_weight"],
        "horizon_ms": scenario["horizon_ms"],
        "request_sha256": scenario["request_sha256"],
        "scenario_contract_sha256": scenario["scenario_contract_sha256"],
        "scenario_model_sha256": scenario["scenario_model_sha256"],
        "target_context_bundle_sha256": scenario[
            "target_context_bundle_sha256"
        ],
        "corpus_entry_sha256": scenario["corpus_entry_sha256"],
        "source_scenario_sha256": scenario["source_scenario_sha256"],
        "catalog_sha256": scenario["catalog_sha256"],
        "policy_source_sha256": policy["source_sha256"],
        "policy_adapter_sha256": policy["adapter_sha256"],
        "policy_profile_sha256": policy["profile_sha256"],
    }
    for field, expected in expected_values.items():
        if row.get(field) != expected:
            raise FuryPairedRunnerError(f"rollout {field} differs from its paired group")
    normalized_result = _normalize_executor_result(row)
    if int(normalized_result["elapsed_ms"]) > int(scenario["horizon_ms"]):
        raise FuryPairedRunnerError(
            "rollout elapsed_ms cannot exceed its planned horizon_ms"
        )
    dynamic_scenario = "dynamic_load_config" in scenario
    completed_contract = (
        normalized_result["completion_criterion_met"] is True
        and (
            int(normalized_result["elapsed_ms"]) <= int(scenario["horizon_ms"])
            if dynamic_scenario
            else int(normalized_result["elapsed_ms"])
            == int(scenario["horizon_ms"])
        )
    )
    if (
        normalized_result["completion_criterion_met"] is True
        and not completed_contract
    ):
        raise FuryPairedRunnerError(
            "completed rollout violates its duration/dynamic completion horizon"
        )
    contract_complete = (
        completed_contract
        and int(normalized_result["omitted_lane_count"]) == 0
        and int(normalized_result["fatal_error_count"]) == 0
    )
    if row.get("contract_complete") is not contract_complete:
        raise FuryPairedRunnerError(
            "rollout contract_complete differs from completion/omission/fatal state"
        )
    reasons = normalized_result["nonfaithful_reason_counts"]
    if contract["execution_mode"] == SYNTHETIC_MODE:
        if reasons.get(SYNTHETIC_NONVOTING_REASON) != 1:
            raise FuryPairedRunnerError(
                "synthetic rollout lacks its scientific nonvoting reason"
            )
    if contract["plan_intent"] == DIAGNOSTIC_INTENT:
        if reasons.get(DIAGNOSTIC_NONVOTING_REASON) != 1:
            raise FuryPairedRunnerError(
                "diagnostic rollout lacks its nonvoting reason"
            )
    derived_eligible = (
        contract_complete
        and not any(
            int(count) > 0
            for count in reasons.values()
        )
        and contract["execution_mode"] == SINGLE_BRIDGE_MODE
        and contract["plan_intent"] == COMPARISON_INTENT
        and scenario["scenario_model"]["comparison_eligible"] is True
        and scenario["target_context_bundle"]["comparison_eligible"] is True
    )
    if normalized_result["evaluation_eligible"] is not derived_eligible:
        raise FuryPairedRunnerError(
            "executor evaluation_eligible does not match the runner-derived "
            "completion/omission/fatal/nonfaithful contract"
        )
    digest = row.get("row_sha256")
    _lower_sha256(digest, "rollout row_sha256")
    unsigned = dict(row)
    unsigned.pop("row_sha256", None)
    if digest != sha256_json(unsigned):
        raise FuryPairedRunnerError("rollout row SHA-256 mismatch")


def _validate_worker_contract(value: Any) -> None:
    contract = _mapping(value, "worker_contract")
    if set(contract) != {
        "worker_id",
        "execution_mode",
        "declared_bridge_process_count",
        "one_bridge_process_per_worker",
        "shared_mutable_bridge_between_workers",
        "contract_runtime_evidence",
    }:
        raise FuryPairedRunnerError("worker contract field set mismatch")
    _text(contract.get("worker_id"), "worker_contract.worker_id")
    mode = _text(contract.get("execution_mode"), "worker_contract.execution_mode")
    count = _nonnegative_int(
        contract.get("declared_bridge_process_count"),
        "worker_contract.declared_bridge_process_count",
    )
    if contract.get("one_bridge_process_per_worker") is not True:
        raise FuryPairedRunnerError("worker must declare one bridge process per worker")
    if contract.get("shared_mutable_bridge_between_workers") is not False:
        raise FuryPairedRunnerError("worker must not share a mutable bridge")
    if mode == SYNTHETIC_MODE:
        if count != 0 or contract.get("contract_runtime_evidence") != "NOT_APPLICABLE_SYNTHETIC":
            raise FuryPairedRunnerError("invalid synthetic worker bridge declaration")
    elif mode == SINGLE_BRIDGE_MODE:
        if (
            count != 1
            or contract.get("contract_runtime_evidence")
            != "FULL_POLICY_ARTIFACT_VALIDATED_BY_RUNNER"
        ):
            raise FuryPairedRunnerError("production worker must declare exactly one bridge")
    else:
        raise FuryPairedRunnerError(f"unsupported worker execution mode: {mode}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode("utf-8")


def _read_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FuryPairedRunnerError(f"could not read {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise FuryPairedRunnerError(f"{label} root must be an object")
    return copy.deepcopy(dict(value))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryPairedRunnerError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FuryPairedRunnerError(f"{label} must be a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryPairedRunnerError(f"{label} must be a nonempty string")
    return value


def _lower_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FuryPairedRunnerError(f"{label} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FuryPairedRunnerError(f"{label} must be a positive integer")
    return value


def _positive_int64(value: Any, label: str) -> int:
    result = _positive_int(value, label)
    if result > _MAX_POSITIVE_INT64:
        raise FuryPairedRunnerError(f"{label} exceeds positive int64")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FuryPairedRunnerError(f"{label} must be a nonnegative integer")
    return value


def _positive_finite(value: Any, label: str) -> float:
    result = _nonnegative_finite(value, label)
    if result <= 0:
        raise FuryPairedRunnerError(f"{label} must be positive")
    return result


def _nonnegative_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryPairedRunnerError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result) or result < 0:
        raise FuryPairedRunnerError(f"{label} must be finite and nonnegative")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise FuryPairedRunnerError(f"{label} must be boolean")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = (
    "COMPARISON_INTENT",
    "DIAGNOSTIC_INTENT",
    "FuryPairedRunnerError",
    "PLAN_KIND",
    "REDUCTION_KIND",
    "REDUCTION_RECEIPT_KIND",
    "ROLLOUT_KIND",
    "SEED_DERIVATION_ALGORITHM",
    "SHARD_MANIFEST_KIND",
    "SINGLE_BRIDGE_MODE",
    "SCENARIO_MODEL_KIND",
    "EXACT_STATIC_REQUEST_SEMANTICS_SCHEMA",
    "SYNTHETIC_MODE",
    "TARGET_CONTEXT_BUNDLE_KIND",
    "ValidatedShard",
    "build_runner_plan",
    "build_exact_static_request_semantics_receipt",
    "canonical_json_bytes",
    "derive_simulator_seed",
    "execute_shard",
    "normalize_runner_scenarios",
    "reduce_shards",
    "runner_scenario_bundle_sha256",
    "runner_scenario_model_bundle_sha256",
    "runner_target_context_bundle_set_sha256",
    "sha256_json",
    "validate_runner_plan",
    "validate_rollout_rows",
    "build_reduction_receipt",
    "validate_reduction_receipt",
    "validate_shard",
)
