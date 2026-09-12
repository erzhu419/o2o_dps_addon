"""Native dynamic-v5 paired-runner contract and bounded fixture executor.

Version 2 of the paired runner binds ``load_dynamic_v1`` and therefore cannot
transport the dynamic-v5 armor, attackability, and central-idle semantics.  This
module defines a new contract directly over ``DynamicRolloutLoadV3``.  It does
not project a v3 config to v1 and it does not launch a multiseed experiment.

The execution surface in this revision is deliberately limited to one in-memory
paired group.  That is enough to close the scenario, lane-result, and producer
validation contracts before a later worker/shard implementation is admitted.
Simulator evidence remains offline and non-voting; no field in this module is a
WoW client-fidelity claim.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import fury_paired_multiseed_runner_v2 as _v2
from .fury_dynamic_target_semantics_v5 import (
    DYNAMIC_LOAD_BINDING_SCHEMA_V3,
    DynamicRolloutLoadV3,
    FuryDynamicTargetSemanticsV5Error,
)
from .fury_full_policy_rollout_v5 import (
    ROLLOUT_SCHEMA_V5,
    validate_fury_full_policy_rollout_v5,
)
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_MODE_V3,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicV3ConfigError,
    dynamic_target_semantics_config_from_wire_v3,
)


JSONMap = dict[str, Any]

PLAN_KIND = "fury_paired_multiseed_runner_plan_v4"
PLAN_CONTRACT_KIND = "fury_paired_multiseed_runner_contract_v4"
DYNAMIC_SCENARIO_BINDING_SCHEMA_V4 = "fury_paired_dynamic_v5_scenario_binding/v4"
LANE_CONTRACT_SCHEMA_V4 = "fury_paired_dynamic_v5_lane_contract/v4"
LANE_RESULT_SCHEMA_V4 = "fury_paired_dynamic_v5_lane_result/v4"
FIXTURE_RECEIPT_SCHEMA_V4 = "fury_paired_dynamic_v5_fixture_receipt/v4"

SINGLE_BRIDGE_MODE = _v2.SINGLE_BRIDGE_MODE
SYNTHETIC_MODE = _v2.SYNTHETIC_MODE
DIAGNOSTIC_INTENT = _v2.DIAGNOSTIC_INTENT
COMPARISON_INTENT = _v2.COMPARISON_INTENT

CAT_POLICY_ID = "cat.fury.profile1"
CONTRA_DEPLOYED_POLICY_ID = "contra.deployed.fury.raid_a"
CONTRA260817_POLICY_ID = "contra260817.fury.source_candidate"
HISTORICAL_POLICY_ID = "chronicle.external_v2.fury.historical_player_policy"
CAT2NEW_POLICY_ID = "cat2new.fury.candidate"

REQUIRED_BASELINE_IDS = (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CONTRA260817_POLICY_ID,
    HISTORICAL_POLICY_ID,
)

FURY_V5_PRODUCER = "fury_full_policy_rollout_v5"
CAT2NEW_V6_PRODUCER = "cat2new_feedback_loop_v6"
COMPLETION_MODES = (
    "ALL_TARGETS_DEAD",
    "SCENARIO_HORIZON_REACHED",
    "INCOMPLETE",
)

_LANE_RESULT_FIELDS = {
    "schema",
    "policy_id",
    "producer",
    "artifact_schema",
    "request_sha256",
    "simulator_seed",
    "dynamic_load_contract_sha256",
    "completion_mode",
    "damage",
    "elapsed_ms",
    "dps",
    "completion_criterion_met",
    "offline_score_eligible",
    "omitted_lane_count",
    "fatal_error_count",
    "nonfaithful_reason_counts",
    "end_state_sha256",
    "dynamic_runtime_receipts_complete",
    "producer_runtime_receipt_sha256",
    "producer_runtime_receipt",
    "live_fidelity",
    "comparison_ready",
    "artifact_sha256",
    "artifact",
}
_PRODUCER_SUMMARY_FIELDS = {
    "policy_id",
    "artifact_schema",
    "completion_mode",
    "damage",
    "elapsed_ms",
    "dps",
    "completion_criterion_met",
    "offline_score_eligible",
    "omitted_lane_count",
    "fatal_error_count",
    "nonfaithful_reason_counts",
    "end_state_sha256",
    "dynamic_runtime_receipts_complete",
    "live_fidelity",
    "comparison_ready",
}


class FuryPairedRunnerV4Error(RuntimeError):
    """A native dynamic-v5 runner contract was violated."""


@dataclass(frozen=True)
class LaneContractV4:
    """One project policy's currently admitted simulator execution surface."""

    policy_id: str
    producer: str
    artifact_schema: str
    source_oracle_status: str
    ordered_sink_status: str
    full_policy_status: str
    dynamic_v5_executable: bool
    blocker_codes: tuple[str, ...]

    def to_wire(self) -> JSONMap:
        return {
            "schema": LANE_CONTRACT_SCHEMA_V4,
            "policy_id": self.policy_id,
            "producer": self.producer,
            "artifact_schema": self.artifact_schema,
            "source_oracle_status": self.source_oracle_status,
            "ordered_sink_status": self.ordered_sink_status,
            "full_policy_status": self.full_policy_status,
            "dynamic_v5_executable": self.dynamic_v5_executable,
            "blocker_codes": list(self.blocker_codes),
            "simulator_only": True,
            "live_fidelity": False,
            "comparison_ready": False,
        }


def builtin_lane_contracts_v4() -> tuple[JSONMap, ...]:
    """Report the audited Cat/Contra execution surfaces without promotion."""

    lanes = (
        LaneContractV4(
            policy_id=CAT_POLICY_ID,
            producer="cat_fury_full_policy_rollout_v5",
            artifact_schema="cat_fury_full_policy_simulator_rollout/v5",
            source_oracle_status="CAT_SOURCE_ORACLE_V4_READY",
            ordered_sink_status="CAT_ORDERED_SINK_V5_READY",
            full_policy_status="CAT_FULL_POLICY_V5_LOAD_DYNAMIC_V2_ONLY",
            dynamic_v5_executable=False,
            blocker_codes=(
                "CAT_V5_DYNAMIC_V3_FULL_POLICY_ADAPTER_MISSING",
            ),
        ),
        LaneContractV4(
            policy_id=CONTRA_DEPLOYED_POLICY_ID,
            producer=FURY_V5_PRODUCER,
            artifact_schema=ROLLOUT_SCHEMA_V5,
            source_oracle_status="CONTRA_DEPLOYED_SOURCE_ADAPTER_V2_READY",
            ordered_sink_status="CONTRA_DEPLOYED_ORDERED_SINK_V2_READY",
            full_policy_status="GENERIC_FULL_POLICY_V5_DYNAMIC_V3_READY",
            dynamic_v5_executable=True,
            blocker_codes=(),
        ),
        LaneContractV4(
            policy_id=CONTRA260817_POLICY_ID,
            producer="contra260817_fury_full_policy_v3",
            artifact_schema="contra260817_fury_full_policy_source_diagnostic/v3",
            source_oracle_status="CONTRA260817_SOURCE_ORACLE_V3_READY",
            ordered_sink_status="TARGET_ITEM_EQUIPMENT_CHANNELS_MISSING",
            full_policy_status="DYNAMIC_V3_FULL_POLICY_ROLLOUT_MISSING",
            dynamic_v5_executable=False,
            blocker_codes=(
                "CONTRA260817_DYNAMIC_V3_FULL_POLICY_EXECUTOR_MISSING",
                "CONTRA260817_TARGET_ITEM_EQUIPMENT_ORDERED_SINK_MISSING",
            ),
        ),
        LaneContractV4(
            policy_id=HISTORICAL_POLICY_ID,
            producer="chronicle_external_v2_historical_policy",
            artifact_schema="UNREGISTERED",
            source_oracle_status="DESCRIPTIVE_ARTIFACT_ONLY",
            ordered_sink_status="FULL_SCENARIO_ORDERED_EXECUTION_MISSING",
            full_policy_status="RUNNER_V4_PRODUCER_ADAPTER_MISSING",
            dynamic_v5_executable=False,
            blocker_codes=(
                "HISTORICAL_EXTERNAL_V2_DYNAMIC_V5_LANE_ADAPTER_MISSING",
            ),
        ),
        LaneContractV4(
            policy_id=CAT2NEW_POLICY_ID,
            producer=CAT2NEW_V6_PRODUCER,
            artifact_schema="cat2new_candidate_feedback_rollout/v6",
            source_oracle_status="CAT2NEW_V6_SOURCE_FROZEN",
            ordered_sink_status="CAT2NEW_V5_SIMULATOR_EXECUTOR_AVAILABLE",
            full_policy_status="RUNNER_V4_PRODUCER_ADAPTER_NOT_YET_REGISTERED",
            dynamic_v5_executable=False,
            blocker_codes=(
                "CAT2NEW_V6_RUNNER_V4_LANE_ADAPTER_MISSING",
            ),
        ),
    )
    return tuple(lane.to_wire() for lane in lanes)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryPairedRunnerV4Error(f"{label} must be an object")
    return value


def _strict_json(value: Any, label: str) -> Any:
    try:
        return _v2._strict_json_copy(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


def _text(value: Any, label: str) -> str:
    try:
        return _v2._text(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


def _sha256(value: Any, label: str) -> str:
    try:
        return _v2._lower_sha256(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


def _positive_int(value: Any, label: str) -> int:
    try:
        return _v2._positive_int(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


def _nonnegative_int(value: Any, label: str) -> int:
    try:
        return _v2._nonnegative_int(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        if positive:
            return _v2._positive_finite(value, label)
        return _v2._nonnegative_finite(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


def _bool(value: Any, label: str) -> bool:
    try:
        return _v2._boolean(value, label)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error


canonical_json_bytes = _v2.canonical_json_bytes
sha256_json = _v2.sha256_json
derive_simulator_seed = _v2.derive_simulator_seed


def bind_dynamic_v5_load(
    request: Mapping[str, Any], seed: int, config_wire: Mapping[str, Any]
) -> DynamicRolloutLoadV3:
    """Bind the exact v3 config; there is intentionally no v1 projection."""

    try:
        config = dynamic_target_semantics_config_from_wire_v3(config_wire)
        return DynamicRolloutLoadV3.bind(request, seed, config)
    except (
        TypeError,
        ValueError,
        DynamicV3ConfigError,
        FuryDynamicTargetSemanticsV5Error,
    ) as error:
        raise FuryPairedRunnerV4Error(
            f"invalid native dynamic-v5 scenario: {error}"
        ) from error


def _dynamic_binding(load: DynamicRolloutLoadV3) -> JSONMap:
    config = load.config
    return {
        "schema": DYNAMIC_SCENARIO_BINDING_SCHEMA_V4,
        "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        "required_bridge_command": "load_dynamic_v3",
        "dynamic_load_binding_schema": DYNAMIC_LOAD_BINDING_SCHEMA_V3,
        "request_sha256": load.request_sha256,
        "simulator_seed": load.seed,
        "dynamic_load_contract_sha256": load.contract_sha256,
        "config_digest": config.content_sha256,
        "target_count": len(config.target_health),
        "background_event_count": len(config.background_damage_events),
        "attackability_event_count": len(config.attackability_events),
        "effective_armor_event_count": len(config.effective_armor_events),
        "idle_advance_mode": config.idle_advance_mode,
        "idle_advance_horizon_ms": config.idle_advance_horizon_ms,
        "same_timestamp_order": config.same_timestamp_order,
        "retarget_mode": config.retarget_mode,
        "central_simulator_idle_advance": True,
        "historical_truth": False,
        "comparison_ready": False,
    }


def _default_scenario_model(request_sha256: str) -> JSONMap:
    return {
        "schema": "fury_dynamic_v5_scenario_model/v4",
        "status": "SIMULATOR_HYPOTHESIS_NONVOTING",
        "request_sha256": request_sha256,
        "bridge_execution_eligible": True,
        "historical_truth": False,
        "comparison_eligible": False,
        "limitation_codes": [
            "DYNAMIC_TARGET_SCHEDULES_ARE_SIMULATOR_HYPOTHESES",
        ],
    }


def _default_target_context_bundle(request_sha256: str, target_count: int) -> JSONMap:
    return {
        "schema": "fury_dynamic_v5_target_context_bundle/v4",
        "status": "POLICY_STATIC_CONTEXT_NOT_BOUND",
        "request_sha256": request_sha256,
        "target_count": target_count,
        "contexts": [],
        "bridge_execution_eligible": False,
        "comparison_eligible": False,
        "limitation_codes": ["POLICY_STATIC_TARGET_CONTEXT_MISSING"],
    }


def _normalize_dynamic_v5_scenario(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "scenario")
    request = _strict_json(_mapping(row.get("request"), "scenario.request"), "request")
    encounter = _mapping(request.get("encounter"), "scenario.request.encounter")
    if encounter.get("useHealth") is not True:
        raise FuryPairedRunnerV4Error(
            "native dynamic-v5 scenarios require encounter.useHealth == true"
        )
    horizon_ms = _positive_int(row.get("horizon_ms"), "scenario.horizon_ms")
    duration = _finite(
        encounter.get("duration"), "scenario.request.encounter.duration", positive=True
    )
    if abs(duration * 1000.0 - horizon_ms) > 1.0:
        raise FuryPairedRunnerV4Error(
            "scenario request duration does not match horizon_ms"
        )
    config_wire = _mapping(
        row.get("dynamic_load_config"), "scenario.dynamic_load_config"
    )
    load = bind_dynamic_v5_load(request, 0, config_wire)
    config = load.config.to_wire()
    if config.get("schema") != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3:
        raise FuryPairedRunnerV4Error("dynamic-v5 config schema drifted")
    if load.config.idle_advance_mode != DYNAMIC_IDLE_ADVANCE_MODE_V3:
        raise FuryPairedRunnerV4Error("dynamic-v5 central idle mode mismatch")
    if load.config.idle_advance_horizon_ms != horizon_ms:
        raise FuryPairedRunnerV4Error(
            "dynamic-v5 idle horizon differs from scenario horizon_ms"
        )
    request_sha = sha256_json(request)
    scenario_model = _strict_json(
        row.get("scenario_model", _default_scenario_model(request_sha)),
        "scenario.scenario_model",
    )
    target_bundle = _strict_json(
        row.get(
            "target_context_bundle",
            _default_target_context_bundle(request_sha, len(load.config.target_health)),
        ),
        "scenario.target_context_bundle",
    )
    if scenario_model.get("request_sha256") != request_sha:
        raise FuryPairedRunnerV4Error("scenario model request SHA-256 mismatch")
    if target_bundle.get("request_sha256") != request_sha:
        raise FuryPairedRunnerV4Error("target-context request SHA-256 mismatch")
    if scenario_model.get("comparison_eligible") is not False:
        raise FuryPairedRunnerV4Error(
            "dynamic-v5 simulator-hypothesis scenario cannot claim comparison eligibility"
        )
    if target_bundle.get("comparison_eligible") is not False:
        raise FuryPairedRunnerV4Error(
            "dynamic-v5 target-context bundle cannot claim comparison eligibility"
        )
    target_count = target_bundle.get("target_count")
    if target_count != len(load.config.target_health):
        raise FuryPairedRunnerV4Error(
            "target-context count differs from dynamic-v5 config"
        )
    base: JSONMap = {
        "instance_id": _text(row.get("instance_id"), "scenario.instance_id"),
        "component_id": _text(row.get("component_id"), "scenario.component_id"),
        "scenario_id": _text(row.get("scenario_id"), "scenario.scenario_id"),
        "stratum": _text(row.get("stratum"), "scenario.stratum"),
        "scenario_weight": _finite(
            row.get("scenario_weight"), "scenario.scenario_weight", positive=True
        ),
        "horizon_ms": horizon_ms,
        "estimated_cost_units": _positive_int(
            row.get("estimated_cost_units", horizon_ms),
            "scenario.estimated_cost_units",
        ),
        "request": request,
        "request_sha256": request_sha,
        "dynamic_load_config": config,
        "dynamic_v5_seed_zero_binding": _dynamic_binding(load),
        "scenario_model": scenario_model,
        "scenario_model_sha256": sha256_json(scenario_model),
        "target_context_bundle": target_bundle,
        "target_context_bundle_sha256": sha256_json(target_bundle),
        "corpus_entry_sha256": _sha256(
            row.get("corpus_entry_sha256"), "scenario.corpus_entry_sha256"
        ),
        "source_scenario_sha256": _sha256(
            row.get("source_scenario_sha256"), "scenario.source_scenario_sha256"
        ),
        "catalog_sha256": _sha256(
            row.get("catalog_sha256"), "scenario.catalog_sha256"
        ),
    }
    if base["stratum"] not in {"single_target", "multi_target"}:
        raise FuryPairedRunnerV4Error(
            "scenario.stratum must be single_target or multi_target"
        )
    base["scenario_contract_sha256"] = sha256_json(base)
    return base


def normalize_runner_scenarios(
    scenarios: Iterable[Mapping[str, Any]],
) -> tuple[JSONMap, ...]:
    normalized = tuple(
        sorted(
            (_normalize_dynamic_v5_scenario(row) for row in scenarios),
            key=lambda row: (str(row["instance_id"]), str(row["scenario_id"])),
        )
    )
    if not normalized:
        raise FuryPairedRunnerV4Error("scenarios must not be empty")
    keys = [(row["instance_id"], row["scenario_id"]) for row in normalized]
    if len(keys) != len(set(keys)):
        raise FuryPairedRunnerV4Error("scenario identities must be unique")
    return normalized


def runner_scenario_bundle_sha256(
    scenarios: Iterable[Mapping[str, Any]],
) -> str:
    return sha256_json(list(normalize_runner_scenarios(scenarios)))


def _normalize_lane_contract(value: Mapping[str, Any]) -> JSONMap:
    row = _strict_json(value, "lane contract")
    expected = {
        "schema",
        "policy_id",
        "producer",
        "artifact_schema",
        "source_oracle_status",
        "ordered_sink_status",
        "full_policy_status",
        "dynamic_v5_executable",
        "blocker_codes",
        "simulator_only",
        "live_fidelity",
        "comparison_ready",
    }
    if set(row) != expected or row.get("schema") != LANE_CONTRACT_SCHEMA_V4:
        raise FuryPairedRunnerV4Error("lane contract field set or schema mismatch")
    for field in (
        "policy_id",
        "producer",
        "artifact_schema",
        "source_oracle_status",
        "ordered_sink_status",
        "full_policy_status",
    ):
        _text(row.get(field), f"lane contract.{field}")
    executable = _bool(
        row.get("dynamic_v5_executable"), "lane contract.dynamic_v5_executable"
    )
    blockers_raw = row.get("blocker_codes")
    if not isinstance(blockers_raw, list):
        raise FuryPairedRunnerV4Error("lane contract.blocker_codes must be a list")
    blockers = [_text(item, "lane blocker") for item in blockers_raw]
    if blockers != sorted(set(blockers)):
        raise FuryPairedRunnerV4Error("lane blocker codes must be sorted and unique")
    if executable == bool(blockers):
        raise FuryPairedRunnerV4Error(
            "lane executable status and blocker codes disagree"
        )
    if (
        row.get("simulator_only") is not True
        or row.get("live_fidelity") is not False
        or row.get("comparison_ready") is not False
    ):
        raise FuryPairedRunnerV4Error("lane contract exceeds simulator-only scope")
    row["blocker_codes"] = blockers
    return row


def _lane_contracts_for_policies(
    policies: Sequence[Mapping[str, Any]],
    lane_contracts: Iterable[Mapping[str, Any]] | None,
) -> tuple[JSONMap, ...]:
    supplied = (
        builtin_lane_contracts_v4()
        if lane_contracts is None
        else tuple(lane_contracts)
    )
    by_id: dict[str, JSONMap] = {}
    for raw in supplied:
        lane = _normalize_lane_contract(raw)
        policy_id = str(lane["policy_id"])
        if policy_id in by_id:
            raise FuryPairedRunnerV4Error("duplicate lane contract")
        by_id[policy_id] = lane
    ordered = []
    for policy in policies:
        policy_id = str(policy["policy_id"])
        if policy_id not in by_id:
            raise FuryPairedRunnerV4Error(
                f"policy lane contract missing: {policy_id}"
            )
        ordered.append(by_id[policy_id])
    return tuple(ordered)


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
    plan_intent: str = DIAGNOSTIC_INTENT,
    lane_contracts: Iterable[Mapping[str, Any]] | None = None,
) -> JSONMap:
    """Build a deterministic native-v3 plan without authorizing execution."""

    mode = _text(execution_mode, "execution_mode")
    if mode not in {SYNTHETIC_MODE, SINGLE_BRIDGE_MODE}:
        raise FuryPairedRunnerV4Error("unsupported execution_mode")
    intent = _text(plan_intent, "plan_intent")
    if intent not in {DIAGNOSTIC_INTENT, COMPARISON_INTENT}:
        raise FuryPairedRunnerV4Error("unsupported plan_intent")
    seeds = tuple(_positive_int(seed, "master seed") for seed in master_seeds)
    if not seeds or len(seeds) != len(set(seeds)):
        raise FuryPairedRunnerV4Error("master seeds must be nonempty and unique")
    try:
        normalized_policies = tuple(_v2._normalize_policy(row) for row in policies)
        normalized_bridge = _v2._normalize_bridge_identity(bridge_identity)
        normalized_bundle = _v2._normalize_execution_bundle(
            execution_bundle_identity
        )
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error
    if not normalized_policies:
        raise FuryPairedRunnerV4Error("policies must not be empty")
    policy_ids = [str(row["policy_id"]) for row in normalized_policies]
    if len(policy_ids) != len(set(policy_ids)):
        raise FuryPairedRunnerV4Error("policy IDs must be unique")
    normalized_scenarios = normalize_runner_scenarios(scenarios)
    observed_bundle = sha256_json(list(normalized_scenarios))
    if observed_bundle != _sha256(
        runner_scenario_bundle_sha256, "runner_scenario_bundle_sha256"
    ):
        raise FuryPairedRunnerV4Error("runner scenario bundle SHA-256 mismatch")
    lanes = _lane_contracts_for_policies(normalized_policies, lane_contracts)
    shard_total = _positive_int(shard_count, "shard_count")
    group_count = len(normalized_scenarios) * len(seeds)
    if shard_total > group_count:
        raise FuryPairedRunnerV4Error("shard_count exceeds paired group count")

    groups: list[JSONMap] = []
    namespace = _text(seed_namespace, "seed_namespace")
    for scenario in normalized_scenarios:
        for master_seed in seeds:
            simulator_seed = derive_simulator_seed(
                master_seed, scenario["request_sha256"], namespace=namespace
            )
            load = bind_dynamic_v5_load(
                scenario["request"], simulator_seed, scenario["dynamic_load_config"]
            )
            identity = {
                "protocol_sha256": _sha256(protocol_sha256, "protocol_sha256"),
                "corpus_manifest_sha256": _sha256(
                    corpus_manifest_sha256, "corpus_manifest_sha256"
                ),
                "phase": _text(phase, "phase"),
                "instance_id": scenario["instance_id"],
                "scenario_id": scenario["scenario_id"],
                "scenario_contract_sha256": scenario["scenario_contract_sha256"],
                "master_seed": master_seed,
                "simulator_seed": simulator_seed,
                "dynamic_load_contract_sha256": load.contract_sha256,
            }
            groups.append(
                {
                    "group_id": sha256_json(identity),
                    **identity,
                    "request_sha256": scenario["request_sha256"],
                    "horizon_ms": scenario["horizon_ms"],
                    "estimated_cost_units": scenario["estimated_cost_units"]
                    * len(normalized_policies),
                    "dynamic_v5_binding": _dynamic_binding(load),
                }
            )
    try:
        assignment, loads = _v2._lpt_assignment(groups, shard_total)
    except _v2.FuryPairedRunnerError as error:
        raise FuryPairedRunnerV4Error(str(error)) from error
    assigned_groups = [
        {**row, "shard_index": assignment[str(row["group_id"])]}
        for row in sorted(groups, key=lambda item: str(item["group_id"]))
    ]
    shards = []
    for index in range(shard_total):
        group_ids = sorted(
            str(row["group_id"])
            for row in assigned_groups
            if row["shard_index"] == index
        )
        shards.append(
            {
                "shard_index": index,
                "group_ids": group_ids,
                "group_count": len(group_ids),
                "expected_rollout_count": len(group_ids) * len(policy_ids),
                "estimated_cost_units": loads[index],
            }
        )
    blockers = sorted(
        {
            code
            for lane in lanes
            for code in lane["blocker_codes"]
        }
    )
    if intent == COMPARISON_INTENT:
        blockers.append("DYNAMIC_V5_SIMULATOR_HYPOTHESIS_NONVOTING")
    if mode == SINGLE_BRIDGE_MODE and not all(
        scenario["scenario_model"].get("bridge_execution_eligible") is True
        and scenario["target_context_bundle"].get("bridge_execution_eligible") is True
        for scenario in normalized_scenarios
    ):
        blockers.append("DYNAMIC_V5_POLICY_CONTEXT_NOT_BRIDGE_EXECUTABLE")
    blockers = sorted(set(blockers))
    contract: JSONMap = {
        "schema_version": 4,
        "kind": PLAN_CONTRACT_KIND,
        "protocol_id": _text(protocol_id, "protocol_id"),
        "protocol_sha256": _sha256(protocol_sha256, "protocol_sha256"),
        "phase": _text(phase, "phase"),
        "corpus_manifest_sha256": _sha256(
            corpus_manifest_sha256, "corpus_manifest_sha256"
        ),
        "runner_inputs_sha256": _sha256(
            runner_inputs_sha256, "runner_inputs_sha256"
        ),
        "runner_scenario_bundle_sha256": observed_bundle,
        "corpus_binding_sha256": _sha256(
            corpus_binding_sha256, "corpus_binding_sha256"
        ),
        "seed_derivation": {
            "algorithm": _v2.SEED_DERIVATION_ALGORITHM,
            "namespace": namespace,
            "master_seeds": list(seeds),
        },
        "bridge_identity": normalized_bridge,
        "execution_bundle_identity": normalized_bundle,
        "execution_mode": mode,
        "plan_intent": intent,
        "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
        "required_bridge_command": "load_dynamic_v3",
        "dynamic_load_schema": "fury_full_policy_dynamic_load/v3",
        "policies": list(normalized_policies),
        "policy_ids": policy_ids,
        "lane_contracts": list(lanes),
        "scenarios": list(normalized_scenarios),
        "groups": assigned_groups,
        "group_count": len(assigned_groups),
        "expected_rollout_count": len(assigned_groups) * len(policy_ids),
        "shard_count": shard_total,
        "shards": shards,
        "execution_surface": "ONE_IN_MEMORY_PAIRED_GROUP_FIXTURE_ONLY",
        "blocker_codes": blockers,
        "status": "READY_FOR_SMALL_FIXTURE" if not blockers else "BLOCKED",
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
    }
    return {
        "schema_version": 4,
        "kind": PLAN_KIND,
        "generated_at": _v2._now(),
        "plan_sha256": sha256_json(contract),
        "contract": contract,
        "execution_started": False,
        "scientific_result_available": False,
    }


def validate_runner_plan(value: Mapping[str, Any]) -> JSONMap:
    plan = _strict_json(value, "runner plan")
    expected = {
        "schema_version",
        "kind",
        "generated_at",
        "plan_sha256",
        "contract",
        "execution_started",
        "scientific_result_available",
    }
    if set(plan) != expected or plan.get("schema_version") != 4 or plan.get("kind") != PLAN_KIND:
        raise FuryPairedRunnerV4Error("runner-v4 plan field set or schema mismatch")
    contract = _mapping(plan.get("contract"), "runner plan.contract")
    if plan.get("plan_sha256") != sha256_json(contract):
        raise FuryPairedRunnerV4Error("runner-v4 plan content address mismatch")
    seeds = _mapping(contract.get("seed_derivation"), "seed_derivation")
    rebuilt = build_runner_plan(
        protocol_id=contract.get("protocol_id"),
        protocol_sha256=contract.get("protocol_sha256"),
        phase=contract.get("phase"),
        corpus_manifest_sha256=contract.get("corpus_manifest_sha256"),
        runner_inputs_sha256=contract.get("runner_inputs_sha256"),
        runner_scenario_bundle_sha256=contract.get("runner_scenario_bundle_sha256"),
        corpus_binding_sha256=contract.get("corpus_binding_sha256"),
        master_seeds=seeds.get("master_seeds", []),
        scenarios=contract.get("scenarios", []),
        policies=contract.get("policies", []),
        shard_count=contract.get("shard_count"),
        bridge_identity=contract.get("bridge_identity", {}),
        execution_bundle_identity=contract.get("execution_bundle_identity", {}),
        execution_mode=contract.get("execution_mode"),
        seed_namespace=seeds.get("namespace"),
        plan_intent=contract.get("plan_intent"),
        lane_contracts=contract.get("lane_contracts", []),
    )
    if rebuilt["contract"] != contract or rebuilt["plan_sha256"] != plan["plan_sha256"]:
        raise FuryPairedRunnerV4Error("runner-v4 plan is not canonical")
    if plan.get("execution_started") is not False or plan.get(
        "scientific_result_available"
    ) is not False:
        raise FuryPairedRunnerV4Error("runner plan cannot claim execution or science")
    return plan


def build_lane_result_v4(
    *,
    policy_id: str,
    producer: str,
    artifact: Mapping[str, Any],
    request_sha256: str,
    simulator_seed: int,
    dynamic_load_contract_sha256: str,
    completion_mode: str,
    damage: float,
    elapsed_ms: int,
    completion_criterion_met: bool,
    offline_score_eligible: bool,
    omitted_lane_count: int,
    fatal_error_count: int,
    nonfaithful_reason_counts: Mapping[str, int],
    end_state_sha256: str,
    dynamic_runtime_receipts_complete: bool,
    producer_runtime_receipt: Mapping[str, Any],
    live_fidelity: bool = False,
    comparison_ready: bool = False,
) -> JSONMap:
    document = _strict_json(artifact, "lane artifact")
    runtime_receipt = _strict_json(
        producer_runtime_receipt, "producer runtime receipt"
    )
    elapsed = _positive_int(elapsed_ms, "lane elapsed_ms")
    observed_damage = _finite(damage, "lane damage")
    mode = _text(completion_mode, "completion_mode")
    if mode not in COMPLETION_MODES:
        raise FuryPairedRunnerV4Error("unsupported completion_mode")
    complete = _bool(completion_criterion_met, "completion_criterion_met")
    if complete != (mode != "INCOMPLETE"):
        raise FuryPairedRunnerV4Error(
            "completion_mode and completion_criterion_met disagree"
        )
    if live_fidelity is not False or comparison_ready is not False:
        raise FuryPairedRunnerV4Error(
            "runner-v4 fixture cannot claim live fidelity or comparison readiness"
        )
    reasons: dict[str, int] = {}
    for key, count in _mapping(
        nonfaithful_reason_counts, "nonfaithful_reason_counts"
    ).items():
        reasons[_text(key, "nonfaithful reason")] = _nonnegative_int(
            count, "nonfaithful reason count"
        )
    result: JSONMap = {
        "schema": LANE_RESULT_SCHEMA_V4,
        "policy_id": _text(policy_id, "policy_id"),
        "producer": _text(producer, "producer"),
        "artifact_schema": _text(document.get("schema"), "artifact.schema"),
        "request_sha256": _sha256(request_sha256, "request_sha256"),
        "simulator_seed": _positive_int(simulator_seed, "simulator_seed"),
        "dynamic_load_contract_sha256": _sha256(
            dynamic_load_contract_sha256, "dynamic_load_contract_sha256"
        ),
        "completion_mode": mode,
        "damage": observed_damage,
        "elapsed_ms": elapsed,
        "dps": observed_damage * 1000.0 / elapsed,
        "completion_criterion_met": complete,
        "offline_score_eligible": _bool(
            offline_score_eligible, "offline_score_eligible"
        ),
        "omitted_lane_count": _nonnegative_int(
            omitted_lane_count, "omitted_lane_count"
        ),
        "fatal_error_count": _nonnegative_int(
            fatal_error_count, "fatal_error_count"
        ),
        "nonfaithful_reason_counts": dict(sorted(reasons.items())),
        "end_state_sha256": _sha256(end_state_sha256, "end_state_sha256"),
        "dynamic_runtime_receipts_complete": _bool(
            dynamic_runtime_receipts_complete,
            "dynamic_runtime_receipts_complete",
        ),
        "producer_runtime_receipt_sha256": sha256_json(runtime_receipt),
        "producer_runtime_receipt": runtime_receipt,
        "live_fidelity": False,
        "comparison_ready": False,
        "artifact_sha256": sha256_json(document),
        "artifact": document,
    }
    if result["offline_score_eligible"] and (
        not complete
        or result["omitted_lane_count"]
        or result["fatal_error_count"]
        or not result["dynamic_runtime_receipts_complete"]
    ):
        raise FuryPairedRunnerV4Error(
            "offline score eligibility exceeds the lane receipt evidence"
        )
    return result


def _fury_v5_expected_summary(
    artifact: Mapping[str, Any], *, policy_id: str
) -> JSONMap:
    expected_expert_id = {
        CAT_POLICY_ID: CAT_POLICY_ID,
        CONTRA_DEPLOYED_POLICY_ID: "contra.deployed.fury.raid_a.v2",
    }.get(policy_id)
    if expected_expert_id is None or artifact.get("expert_id") != expected_expert_id:
        raise FuryPairedRunnerV4Error(
            "Fury v5 artifact expert identity differs from planned policy"
        )
    blockers = artifact.get("blockers")
    if not isinstance(blockers, list):
        raise FuryPairedRunnerV4Error("Fury v5 artifact blockers are malformed")
    reason_counts: dict[str, int] = {}
    fatal_count = 0
    for index, raw in enumerate(blockers):
        blocker = _mapping(raw, f"Fury v5 blocker {index}")
        code = _text(blocker.get("code"), f"Fury v5 blocker {index}.code")
        reason_counts[code] = reason_counts.get(code, 0) + 1
        fatal_count += int(blocker.get("execution_fatal") is True)
    final_state = _mapping(artifact.get("final_state"), "Fury v5 final_state")
    lifecycle = final_state.get("dynamic_team_background")
    targets = lifecycle.get("targets") if isinstance(lifecycle, Mapping) else None
    all_dead = (
        isinstance(targets, list)
        and bool(targets)
        and all(isinstance(row, Mapping) and row.get("dead") is True for row in targets)
    )
    complete = artifact.get("scenario_complete") is True
    completion_mode = (
        "ALL_TARGETS_DEAD"
        if complete and all_dead
        else "SCENARIO_HORIZON_REACHED"
        if complete
        else "INCOMPLETE"
    )
    closure = _mapping(
        artifact.get("dynamic_v3_runtime_receipt_closure"),
        "Fury v5 runtime closure",
    )
    runtime_complete = closure.get("status") == "COMPLETE_BOUND"
    damage = _finite(artifact.get("damage_delta"), "Fury v5 damage_delta")
    elapsed = _positive_int(artifact.get("elapsed_ms"), "Fury v5 elapsed_ms")
    return {
        "policy_id": policy_id,
        "artifact_schema": ROLLOUT_SCHEMA_V5,
        "completion_mode": completion_mode,
        "damage": damage,
        "elapsed_ms": elapsed,
        "dps": damage * 1000.0 / elapsed,
        "completion_criterion_met": complete,
        "offline_score_eligible": complete and runtime_complete and fatal_count == 0,
        "omitted_lane_count": fatal_count,
        "fatal_error_count": fatal_count,
        "nonfaithful_reason_counts": dict(sorted(reason_counts.items())),
        "end_state_sha256": sha256_json(final_state),
        "dynamic_runtime_receipts_complete": runtime_complete,
        "live_fidelity": False,
        "comparison_ready": False,
    }


def build_fury_v5_lane_result_v4(
    artifact: Mapping[str, Any],
    *,
    policy_id: str,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
) -> JSONMap:
    """Build the common receipt from a validated generic Fury v5 artifact."""

    load = bind_dynamic_v5_load(
        _mapping(scenario.get("request"), "scenario.request"),
        _positive_int(group.get("simulator_seed"), "group.simulator_seed"),
        _mapping(
            scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"
        ),
    )
    validated = validate_fury_full_policy_rollout_v5(artifact, dynamic_load=load)
    expected = _fury_v5_expected_summary(validated, policy_id=policy_id)
    return build_lane_result_v4(
        policy_id=policy_id,
        producer=FURY_V5_PRODUCER,
        artifact=validated,
        request_sha256=str(scenario["request_sha256"]),
        simulator_seed=int(group["simulator_seed"]),
        dynamic_load_contract_sha256=str(group["dynamic_load_contract_sha256"]),
        completion_mode=str(expected["completion_mode"]),
        damage=float(expected["damage"]),
        elapsed_ms=int(expected["elapsed_ms"]),
        completion_criterion_met=bool(expected["completion_criterion_met"]),
        offline_score_eligible=bool(expected["offline_score_eligible"]),
        omitted_lane_count=int(expected["omitted_lane_count"]),
        fatal_error_count=int(expected["fatal_error_count"]),
        nonfaithful_reason_counts=expected["nonfaithful_reason_counts"],
        end_state_sha256=str(expected["end_state_sha256"]),
        dynamic_runtime_receipts_complete=bool(
            expected["dynamic_runtime_receipts_complete"]
        ),
        producer_runtime_receipt=validated[
            "dynamic_v3_runtime_receipt_closure"
        ],
    )


def validate_lane_result_v4(
    value: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    scenario: Mapping[str, Any],
    policy: Mapping[str, Any],
    artifact_validator: Callable[
        [Mapping[str, Any], Mapping[str, Any], DynamicRolloutLoadV3],
        Mapping[str, Any],
    ]
    | None = None,
) -> JSONMap:
    result = _strict_json(value, "lane result")
    if set(result) != _LANE_RESULT_FIELDS or result.get("schema") != LANE_RESULT_SCHEMA_V4:
        raise FuryPairedRunnerV4Error("lane result field set or schema mismatch")
    load = bind_dynamic_v5_load(
        _mapping(scenario.get("request"), "scenario.request"),
        _positive_int(group.get("simulator_seed"), "group.simulator_seed"),
        _mapping(
            scenario.get("dynamic_load_config"), "scenario.dynamic_load_config"
        ),
    )
    exact = {
        "policy_id": policy.get("policy_id"),
        "request_sha256": scenario.get("request_sha256"),
        "simulator_seed": group.get("simulator_seed"),
        "dynamic_load_contract_sha256": group.get(
            "dynamic_load_contract_sha256"
        ),
    }
    for field, expected in exact.items():
        if result.get(field) != expected:
            raise FuryPairedRunnerV4Error(f"lane result {field} mismatch")
    if result.get("dynamic_load_contract_sha256") != load.contract_sha256:
        raise FuryPairedRunnerV4Error("lane result dynamic-v5 binding mismatch")
    artifact = _mapping(result.get("artifact"), "lane result.artifact")
    if result.get("artifact_schema") != artifact.get("schema"):
        raise FuryPairedRunnerV4Error("lane artifact schema binding mismatch")
    if result.get("artifact_sha256") != sha256_json(artifact):
        raise FuryPairedRunnerV4Error("lane artifact SHA-256 mismatch")
    runtime_receipt = _mapping(
        result.get("producer_runtime_receipt"), "producer runtime receipt"
    )
    if result.get("producer_runtime_receipt_sha256") != sha256_json(
        runtime_receipt
    ):
        raise FuryPairedRunnerV4Error(
            "producer runtime receipt SHA-256 mismatch"
        )
    if result.get("producer") == FURY_V5_PRODUCER:
        if artifact_validator is not None:
            raise FuryPairedRunnerV4Error(
                "generic Fury v5 producer uses the runner-owned validator"
            )
        validate_fury_full_policy_rollout_v5(artifact, dynamic_load=load)
        if runtime_receipt != artifact.get("dynamic_v3_runtime_receipt_closure"):
            raise FuryPairedRunnerV4Error(
                "Fury v5 producer runtime receipt differs from its artifact closure"
            )
        expected_summary = _fury_v5_expected_summary(
            artifact, policy_id=str(policy.get("policy_id"))
        )
    else:
        if artifact_validator is None:
            raise FuryPairedRunnerV4Error(
                f"producer-specific artifact validator missing: {result.get('producer')}"
            )
        expected_summary = artifact_validator(artifact, runtime_receipt, load)
        if not isinstance(expected_summary, Mapping):
            raise FuryPairedRunnerV4Error(
                "producer artifact validator returned a non-object"
            )
    if set(expected_summary) != _PRODUCER_SUMMARY_FIELDS:
        raise FuryPairedRunnerV4Error(
            "producer artifact validator summary field set mismatch"
        )
    for field in sorted(_PRODUCER_SUMMARY_FIELDS):
        if canonical_json_bytes(result.get(field)) != canonical_json_bytes(
            expected_summary.get(field)
        ):
            raise FuryPairedRunnerV4Error(
                f"lane result {field} differs from producer artifact"
            )
    elapsed = _positive_int(result.get("elapsed_ms"), "lane elapsed_ms")
    damage = _finite(result.get("damage"), "lane damage")
    dps = _finite(result.get("dps"), "lane dps")
    expected_dps = damage * 1000.0 / elapsed
    if abs(dps - expected_dps) > max(1e-6, abs(expected_dps) * 1e-9):
        raise FuryPairedRunnerV4Error("lane result dps is inconsistent")
    mode = _text(result.get("completion_mode"), "completion_mode")
    if mode not in COMPLETION_MODES or _bool(
        result.get("completion_criterion_met"), "completion_criterion_met"
    ) != (mode != "INCOMPLETE"):
        raise FuryPairedRunnerV4Error("lane completion contract mismatch")
    if elapsed > _positive_int(scenario.get("horizon_ms"), "scenario.horizon_ms"):
        raise FuryPairedRunnerV4Error("lane elapsed time exceeds scenario horizon")
    if result.get("live_fidelity") is not False or result.get("comparison_ready") is not False:
        raise FuryPairedRunnerV4Error("lane result exceeds simulator-only scope")
    if result.get("offline_score_eligible") is True and (
        result.get("completion_criterion_met") is not True
        or _nonnegative_int(result.get("omitted_lane_count"), "omitted_lane_count")
        or _nonnegative_int(result.get("fatal_error_count"), "fatal_error_count")
        or result.get("dynamic_runtime_receipts_complete") is not True
    ):
        raise FuryPairedRunnerV4Error("offline score eligibility is unsupported")
    return result


def execute_small_fixture_v4(
    plan: Mapping[str, Any],
    executor: Callable[..., Mapping[str, Any]],
    *,
    artifact_validators: Mapping[
        str,
        Callable[
            [Mapping[str, Any], Mapping[str, Any], DynamicRolloutLoadV3],
            Mapping[str, Any],
        ],
    ] | None = None,
) -> JSONMap:
    """Execute exactly one paired group in memory; heavy dispatch is absent."""

    validated = validate_runner_plan(plan)
    contract = validated["contract"]
    if contract.get("status") != "READY_FOR_SMALL_FIXTURE":
        raise FuryPairedRunnerV4Error(
            "runner-v4 plan is blocked: " + ", ".join(contract["blocker_codes"])
        )
    if contract.get("group_count") != 1:
        raise FuryPairedRunnerV4Error(
            "runner-v4 execution is limited to one small fixture group"
        )
    if not callable(executor):
        raise TypeError("executor must be callable")
    group = contract["groups"][0]
    scenario = next(
        row
        for row in contract["scenarios"]
        if row["instance_id"] == group["instance_id"]
        and row["scenario_id"] == group["scenario_id"]
    )
    policies = {row["policy_id"]: row for row in contract["policies"]}
    validators = dict(artifact_validators or {})
    results = []
    for policy_id in contract["policy_ids"]:
        raw = executor(
            group=copy.deepcopy(group),
            scenario=copy.deepcopy(scenario),
            policy=copy.deepcopy(policies[policy_id]),
        )
        envelope = _mapping(raw, "fixture executor result")
        if set(envelope) != {"lane_result"}:
            raise FuryPairedRunnerV4Error(
                "fixture executor must return only lane_result"
            )
        result = _mapping(envelope["lane_result"], "lane_result")
        results.append(
            validate_lane_result_v4(
                result,
                group=group,
                scenario=scenario,
                policy=policies[policy_id],
                artifact_validator=validators.get(str(result.get("producer"))),
            )
        )
    fixture_complete = all(
        row["completion_criterion_met"] is True
        and row["fatal_error_count"] == 0
        for row in results
    )
    return {
        "schema": FIXTURE_RECEIPT_SCHEMA_V4,
        "status": (
            "COMPLETE_SIMULATOR_ONLY_NONVOTING"
            if fixture_complete
            else "EXECUTED_WITH_INCOMPLETE_LANES_NONVOTING"
        ),
        "plan_sha256": validated["plan_sha256"],
        "group_id": group["group_id"],
        "result_count": len(results),
        "results": results,
        "heavy_execution_started": False,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


__all__ = (
    "CAT2NEW_POLICY_ID",
    "CAT2NEW_V6_PRODUCER",
    "CAT_POLICY_ID",
    "COMPARISON_INTENT",
    "COMPLETION_MODES",
    "CONTRA260817_POLICY_ID",
    "CONTRA_DEPLOYED_POLICY_ID",
    "DIAGNOSTIC_INTENT",
    "DYNAMIC_SCENARIO_BINDING_SCHEMA_V4",
    "FIXTURE_RECEIPT_SCHEMA_V4",
    "FURY_V5_PRODUCER",
    "FuryPairedRunnerV4Error",
    "HISTORICAL_POLICY_ID",
    "LANE_CONTRACT_SCHEMA_V4",
    "LANE_RESULT_SCHEMA_V4",
    "LaneContractV4",
    "PLAN_CONTRACT_KIND",
    "PLAN_KIND",
    "REQUIRED_BASELINE_IDS",
    "SINGLE_BRIDGE_MODE",
    "SYNTHETIC_MODE",
    "bind_dynamic_v5_load",
    "build_lane_result_v4",
    "build_fury_v5_lane_result_v4",
    "build_runner_plan",
    "builtin_lane_contracts_v4",
    "canonical_json_bytes",
    "derive_simulator_seed",
    "execute_small_fixture_v4",
    "normalize_runner_scenarios",
    "runner_scenario_bundle_sha256",
    "sha256_json",
    "validate_lane_result_v4",
    "validate_runner_plan",
)
