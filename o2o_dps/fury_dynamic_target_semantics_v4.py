"""Versioned dynamic-v2 hypothesis binding for Fury simulator diagnostics.

Unlike the retained schedules in v3, the schedules compiled here are executed
by ``load_dynamic_v2``.  Execution proves only simulator mechanism and content
binding.  Initial health, effective armor, attackability, and fixed team damage
remain named hypotheses and therefore cannot enter comparison or voting gates.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import struct
from typing import Any, Mapping

from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
    target_semantics_context_receipt_v3,
)
from .fury_paired_multiseed_runner_v2 import (
    SCENARIO_MODEL_KIND,
    TARGET_CONTEXT_BUNDLE_KIND,
    sha256_json,
)
from .sim_bridge_dynamic_v2 import (
    DYNAMIC_SAME_TIMESTAMP_ORDER_V2,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
    DynamicTargetSemanticsConfigV2,
    dynamic_target_semantics_config_from_wire_v2,
)


JSONMap = dict[str, Any]

SCHEMA_VERSION = 4
SCHEMA = "fury_dynamic_target_semantics_binding/v4"
KIND = "fury_dynamic_target_semantics_binding_v4"
IMPLEMENTATION_REVISION = "v4.0_load_dynamic_v2_executable_hypotheses"
EXECUTION_RECEIPT_SCHEMA = "fury_dynamic_target_semantics_execution_receipt/v4"
DYNAMIC_ROLLOUT_LOAD_SCHEMA_V2 = "fury_full_policy_dynamic_load/v2"
DYNAMIC_LOAD_BINDING_SCHEMA_V2 = "fury_full_policy_dynamic_load_binding/v2"
SCENARIO_PROJECTION_SCHEMA_V4 = "fury_dynamic_v2_scenario_projection/v4"
STATUS = "DYNAMIC_V2_SIMULATOR_HYPOTHESIS_EXECUTABLE_NONVOTING"
MODEL_STATUS = "DYNAMIC_V2_SIMULATOR_HYPOTHESIS_NONVOTING"
BINDING_STATUS = "DYNAMIC_V2_SIMULATOR_HYPOTHESIS_NONVOTING"
LIMITATION_CODES = (
    "TARGET_HEALTH_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
    "TARGET_ARMOR_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
    "TARGET_ATTACKABILITY_SIMULATOR_HYPOTHESIS_NOT_HISTORICAL",
    "TEAM_RESPONSE_FIXED_DRAW_NOT_ENDOGENOUS_MODEL",
    "DYNAMIC_V2_MECHANISM_NOT_COMPARISON_ADMITTED",
)
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HYPOTHESIS_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDENTITY_FIELDS = frozenset(
    {
        "instance_id",
        "component_id",
        "scenario_id",
        "stratum",
        "scenario_weight",
        "horizon_ms",
        "estimated_cost_units",
        "corpus_entry_sha256",
        "source_scenario_sha256",
        "catalog_sha256",
    }
)


class FuryDynamicTargetSemanticsV4Error(RuntimeError):
    """The executable dynamic-v2 hypothesis binding is malformed."""


def _parse_dynamic_config_v4(
    value: Mapping[str, Any],
) -> DynamicTargetSemanticsConfigV2:
    try:
        return dynamic_target_semantics_config_from_wire_v2(value)
    except (TypeError, ValueError) as error:
        raise FuryDynamicTargetSemanticsV4Error(
            f"invalid dynamic-v2 config: {error}"
        ) from error


@dataclass(frozen=True)
class DynamicRolloutLoadV2:
    """One exact request, seed, and full dynamic-v2 config binding."""

    request_sha256: str
    seed: int
    config: DynamicTargetSemanticsConfigV2
    contract_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _sha256(self.request_sha256, "request_sha256")
        _signed_int64(self.seed, "seed")
        if not isinstance(self.config, DynamicTargetSemanticsConfigV2):
            raise TypeError("config must be DynamicTargetSemanticsConfigV2")
        identity = {
            "schema": DYNAMIC_ROLLOUT_LOAD_SCHEMA_V2,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "config_digest": self.config.content_sha256,
        }
        object.__setattr__(self, "contract_sha256", sha256_json(identity))

    @classmethod
    def bind(
        cls,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTargetSemanticsConfigV2,
    ) -> "DynamicRolloutLoadV2":
        request_copy = _strict_json_copy(request, "request")
        contract = cls(
            request_sha256=sha256_json(request_copy),
            seed=_strict_int(seed, "seed"),
            config=config,
        )
        validate_dynamic_load_request_v2(contract, request_copy)
        return contract

    @classmethod
    def from_wire(
        cls, request: Mapping[str, Any], value: Mapping[str, Any]
    ) -> "DynamicRolloutLoadV2":
        raw = _mapping(value, "dynamic rollout load")
        if set(raw) != {
            "schema",
            "request_sha256",
            "seed",
            "config",
            "contract_sha256",
        }:
            raise FuryDynamicTargetSemanticsV4Error(
                "dynamic rollout load field set mismatch"
            )
        if raw.get("schema") != DYNAMIC_ROLLOUT_LOAD_SCHEMA_V2:
            raise FuryDynamicTargetSemanticsV4Error(
                "dynamic rollout load schema is unsupported"
            )
        contract = cls(
            request_sha256=_sha256(raw.get("request_sha256"), "request_sha256"),
            seed=_signed_int64(raw.get("seed"), "seed"),
            config=_parse_dynamic_config_v4(
                _mapping(raw.get("config"), "config")
            ),
        )
        request_copy = _strict_json_copy(request, "request")
        validate_dynamic_load_request_v2(contract, request_copy)
        if raw.get("contract_sha256") != contract.contract_sha256:
            raise FuryDynamicTargetSemanticsV4Error(
                "dynamic rollout load contract SHA-256 mismatch"
            )
        return contract

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_ROLLOUT_LOAD_SCHEMA_V2,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "config": self.config.to_wire(),
            "contract_sha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class DynamicScenarioSourceBindingV4:
    """Content identities for every executed hypothesis family."""

    background_draw_content_sha256: str
    background_schedule_content_sha256: str
    target_health_hypothesis_content_sha256: str
    attackability_schedule_hypothesis_content_sha256: str
    effective_armor_schedule_hypothesis_content_sha256: str
    target_health_hypothesis_id: str
    target_semantics_source_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "background_draw_content_sha256",
            "background_schedule_content_sha256",
            "target_health_hypothesis_content_sha256",
            "attackability_schedule_hypothesis_content_sha256",
            "effective_armor_schedule_hypothesis_content_sha256",
            "target_semantics_source_sha256",
        ):
            _sha256(getattr(self, name), name)
        if (
            not isinstance(self.target_health_hypothesis_id, str)
            or _HYPOTHESIS_ID_RE.fullmatch(self.target_health_hypothesis_id)
            is None
        ):
            raise ValueError(
                "target_health_hypothesis_id must be a normalized identifier"
            )

    @classmethod
    def bind_config(
        cls,
        *,
        config: DynamicTargetSemanticsConfigV2,
        background_draw_content_sha256: str,
        target_health_hypothesis_id: str,
        target_semantics_source_sha256: str,
    ) -> "DynamicScenarioSourceBindingV4":
        if not isinstance(config, DynamicTargetSemanticsConfigV2):
            raise TypeError("config must be DynamicTargetSemanticsConfigV2")
        return cls(
            background_draw_content_sha256=background_draw_content_sha256,
            background_schedule_content_sha256=sha256_json(
                [row.to_wire() for row in config.background_damage_events]
            ),
            target_health_hypothesis_content_sha256=sha256_json(
                [row.to_wire() for row in config.target_health]
            ),
            attackability_schedule_hypothesis_content_sha256=sha256_json(
                [row.to_wire() for row in config.attackability_events]
            ),
            effective_armor_schedule_hypothesis_content_sha256=sha256_json(
                [row.to_wire() for row in config.effective_armor_events]
            ),
            target_health_hypothesis_id=target_health_hypothesis_id,
            target_semantics_source_sha256=target_semantics_source_sha256,
        )

    def to_dict(self) -> JSONMap:
        return {
            "background_draw_content_sha256": self.background_draw_content_sha256,
            "background_schedule_content_sha256": (
                self.background_schedule_content_sha256
            ),
            "target_health_hypothesis_content_sha256": (
                self.target_health_hypothesis_content_sha256
            ),
            "attackability_schedule_hypothesis_content_sha256": (
                self.attackability_schedule_hypothesis_content_sha256
            ),
            "effective_armor_schedule_hypothesis_content_sha256": (
                self.effective_armor_schedule_hypothesis_content_sha256
            ),
            "target_health_hypothesis_id": self.target_health_hypothesis_id,
            "target_semantics_source_sha256": self.target_semantics_source_sha256,
            "historical_truth": False,
        }


@dataclass(frozen=True)
class CompiledDynamicTargetSemanticsV4:
    artifact: JSONMap
    scenario: JSONMap
    target_contexts: Mapping[int, TargetSemanticsContextV3]
    dynamic_load: DynamicRolloutLoadV2


def dynamic_rollout_load_from_adapter_wire_v2(
    value: Mapping[str, Any],
) -> tuple[JSONMap, DynamicRolloutLoadV2]:
    """Strictly consume one generator ``load_dynamic_v2`` command."""

    raw = _mapping(value, "adapter wire request")
    if set(raw) != {"command", "request", "seed", "dynamic"}:
        raise FuryDynamicTargetSemanticsV4Error(
            "adapter wire request field set mismatch"
        )
    if raw.get("command") != "load_dynamic_v2":
        raise FuryDynamicTargetSemanticsV4Error(
            "adapter wire command must be load_dynamic_v2"
        )
    request = _strict_json_copy(
        _mapping(raw.get("request"), "adapter request"), "adapter request"
    )
    config = _parse_dynamic_config_v4(
        _mapping(raw.get("dynamic"), "adapter dynamic config")
    )
    contract = DynamicRolloutLoadV2.bind(
        request, _signed_int64(raw.get("seed"), "seed"), config
    )
    return request, contract


def dynamic_rollout_load_from_config_wire_v2(
    request: Mapping[str, Any], seed: int, value: Mapping[str, Any]
) -> DynamicRolloutLoadV2:
    config = _parse_dynamic_config_v4(value)
    return DynamicRolloutLoadV2.bind(request, seed, config)


def validate_dynamic_load_request_v2(
    dynamic_load: DynamicRolloutLoadV2, request: Mapping[str, Any]
) -> None:
    if not isinstance(dynamic_load, DynamicRolloutLoadV2):
        raise TypeError("dynamic_load must be DynamicRolloutLoadV2")
    request_copy = _strict_json_copy(request, "request")
    if dynamic_load.request_sha256 != sha256_json(request_copy):
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic load request SHA-256 differs from request"
        )
    encounter = _mapping(request_copy.get("encounter"), "request.encounter")
    if encounter.get("useHealth") is not True:
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v2 request requires encounter.useHealth == true"
        )
    targets = _request_targets(request_copy)
    if len(targets) != len(dynamic_load.config.target_health):
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v2 target count differs from request"
        )
    for index, configured in enumerate(dynamic_load.config.target_health):
        stats = targets[index].get("stats")
        if not isinstance(stats, list) or len(stats) <= max(
            ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
        ):
            raise FuryDynamicTargetSemanticsV4Error(
                f"request target {index} lacks explicit armor/health stats"
            )
        health = _positive_number(stats[HEALTH_STAT_INDEX], "target health")
        _nonnegative_number(stats[ARMOR_STAT_INDEX], "target armor")
        if struct.pack(">d", health) != struct.pack(">d", configured.health):
            raise FuryDynamicTargetSemanticsV4Error(
                f"request target {index} health bits differ from config"
            )


def compile_dynamic_target_semantics_binding_v4(
    *,
    scenario_identity: Mapping[str, Any],
    request: Mapping[str, Any],
    dynamic_config: DynamicTargetSemanticsConfigV2,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    source_binding: DynamicScenarioSourceBindingV4,
    seed: int,
) -> CompiledDynamicTargetSemanticsV4:
    """Compile an executable, permanently non-voting dynamic-v2 scenario."""

    identity = _normalize_scenario_identity(scenario_identity)
    request_copy = _strict_json_copy(request, "request")
    if not isinstance(dynamic_config, DynamicTargetSemanticsConfigV2):
        raise TypeError("dynamic_config must be DynamicTargetSemanticsConfigV2")
    if not isinstance(source_binding, DynamicScenarioSourceBindingV4):
        raise TypeError("source_binding must be DynamicScenarioSourceBindingV4")
    load = DynamicRolloutLoadV2.bind(request_copy, seed, dynamic_config)
    _validate_horizon(request_copy, identity["horizon_ms"])
    _validate_source_binding(source_binding, dynamic_config)
    contexts = _normalize_contexts(
        target_contexts,
        config=dynamic_config,
        request=request_copy,
        health_hypothesis_id=source_binding.target_health_hypothesis_id,
    )
    request_sha = load.request_sha256
    context_receipts = [
        target_semantics_context_receipt_v3(contexts[index])
        for index in range(len(contexts))
    ]
    target_bundle: JSONMap = {
        "schema_version": 4,
        "kind": TARGET_CONTEXT_BUNDLE_KIND,
        "binding_status": BINDING_STATUS,
        "request_sha256": request_sha,
        "target_count": len(contexts),
        "contexts": context_receipts,
        "comparison_eligible": False,
        "bridge_execution_eligible": True,
        "limitation_codes": list(LIMITATION_CODES),
    }
    target_bundle_sha = sha256_json(target_bundle)
    execution_receipt: JSONMap = {
        "schema": EXECUTION_RECEIPT_SCHEMA,
        "status": "LOAD_DYNAMIC_V2_MECHANISM_EXECUTABLE_HYPOTHESES_ONLY",
        "request_sha256": request_sha,
        "dynamic_v2_config_sha256": dynamic_config.content_sha256,
        "dynamic_rollout_load_contract_sha256": load.contract_sha256,
        "source_binding": source_binding.to_dict(),
        "same_timestamp_order": dynamic_config.same_timestamp_order,
        "executed_by_load_dynamic_v2": {
            "hypothesized_initial_health": True,
            "live_policy_target_state_from_bridge": True,
            "team_background_damage": True,
            "attackability_schedule": True,
            "effective_armor_schedule": True,
            "central_candidate_and_background_cancellation": True,
            "per_target_death_and_retarget": True,
            "python_policy_pause_emulation": False,
        },
        "runtime_receipts_required": [
            "dynamic_attackability_receipts",
            "dynamic_armor_receipts",
            "dynamic_damage_receipts",
            "dynamic_candidate_damage_receipts",
            "terminal_dynamic_target_state",
        ],
        "exact_historical_health_claimed": False,
        "exact_historical_armor_claimed": False,
        "exact_historical_attackability_claimed": False,
    }
    scenario_model: JSONMap = {
        "schema_version": 4,
        "kind": SCENARIO_MODEL_KIND,
        "model_status": MODEL_STATUS,
        "request_sha256": request_sha,
        "target_context_bundle_sha256": target_bundle_sha,
        "historical_truth": False,
        "comparison_eligible": False,
        "bridge_execution_eligible": True,
        "dynamic_armor_schedule_status": (
            "SIMULATOR_HYPOTHESIS_EXECUTED_BY_LOAD_DYNAMIC_V2"
        ),
        "dynamic_attackability_schedule_status": (
            "SIMULATOR_HYPOTHESIS_EXECUTED_BY_LOAD_DYNAMIC_V2"
        ),
        "health_or_horizon_status": (
            "HYPOTHESIS_BOUND_ENDOGENOUS_TARGET_DEATH_WITH_WATCHDOG"
        ),
        "dynamic_semantics_receipt": execution_receipt,
        "limitation_codes": list(LIMITATION_CODES),
    }
    candidate: JSONMap = {
        **identity,
        "request": request_copy,
        "scenario_model": scenario_model,
        "scenario_model_sha256": sha256_json(scenario_model),
        "target_context_bundle": target_bundle,
        "target_context_bundle_sha256": target_bundle_sha,
        "dynamic_load_config": dynamic_config.to_wire(),
    }
    # Do not pass a v2 dynamic config through the frozen v1-only formal runner.
    # This is a standalone, content-addressed projection for diagnostic rollout
    # tests; later admission needs its own versioned runner contract.
    scenario = _normalize_scenario_projection_v4(candidate)
    scenario_projection = {
        "schema": SCENARIO_PROJECTION_SCHEMA_V4,
        "scenario": scenario,
        "scenario_bundle_sha256": sha256_json(
            {
                "schema": SCENARIO_PROJECTION_SCHEMA_V4,
                "scenarios": [scenario],
            }
        ),
        "scenario_model_sha256": scenario["scenario_model_sha256"],
        "target_context_bundle_sha256": scenario[
            "target_context_bundle_sha256"
        ],
        "formal_runner_connected": False,
    }
    core: JSONMap = {
        "schema_version": SCHEMA_VERSION,
        "schema": SCHEMA,
        "kind": KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "source_binding": source_binding.to_dict(),
        "bridge_capability": {
            "load_command": "load_dynamic_v2",
            "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            "same_timestamp_order": DYNAMIC_SAME_TIMESTAMP_ORDER_V2,
            "dynamic_armor_schedule_executed": True,
            "dynamic_attackability_schedule_executed": True,
            "live_policy_target_state_from_bridge": True,
            "runtime_receipts_required": True,
            "formal_runner_connected": False,
        },
        "scenario_projection": scenario_projection,
        "dynamic_rollout_load": load.to_wire(),
        "claim_boundary": _claim_boundary(),
    }
    artifact = deepcopy(core)
    artifact["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_dynamic_target_semantics_binding_v4(artifact)
    return CompiledDynamicTargetSemanticsV4(
        artifact=artifact,
        scenario=scenario,
        target_contexts=contexts,
        dynamic_load=load,
    )


def validate_dynamic_target_semantics_binding_v4(
    value: Mapping[str, Any],
) -> JSONMap:
    raw = _strict_json_copy(value, "dynamic-v4 binding")
    if set(raw) != {
        "schema_version",
        "schema",
        "kind",
        "implementation_revision",
        "status",
        "source_binding",
        "bridge_capability",
        "scenario_projection",
        "dynamic_rollout_load",
        "claim_boundary",
        "content_address",
    }:
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 binding field set mismatch"
        )
    if (
        raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("schema") != SCHEMA
        or raw.get("kind") != KIND
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("status") != STATUS
        or raw.get("claim_boundary") != _claim_boundary()
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 identity or claim boundary mismatch"
        )
    capability = raw.get("bridge_capability")
    if capability != {
        "load_command": "load_dynamic_v2",
        "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
        "same_timestamp_order": DYNAMIC_SAME_TIMESTAMP_ORDER_V2,
        "dynamic_armor_schedule_executed": True,
        "dynamic_attackability_schedule_executed": True,
        "live_policy_target_state_from_bridge": True,
        "runtime_receipts_required": True,
        "formal_runner_connected": False,
    }:
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 bridge capability mismatch"
        )
    projection = _mapping(
        raw.get("scenario_projection"), "scenario_projection"
    )
    if set(projection) != {
        "schema",
        "scenario",
        "scenario_bundle_sha256",
        "scenario_model_sha256",
        "target_context_bundle_sha256",
        "formal_runner_connected",
    }:
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario projection field set mismatch"
        )
    if (
        projection.get("schema") != SCENARIO_PROJECTION_SCHEMA_V4
        or projection.get("formal_runner_connected") is not False
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario projection was connected to an unversioned runner"
        )
    scenario = _normalize_scenario_projection_v4(
        _mapping(projection.get("scenario"), "scenario")
    )
    if scenario != projection["scenario"]:
        raise FuryDynamicTargetSemanticsV4Error(
            "stored scenario projection is not canonical"
        )
    if (
        projection.get("scenario_bundle_sha256")
        != sha256_json(
            {
                "schema": SCENARIO_PROJECTION_SCHEMA_V4,
                "scenarios": [scenario],
            }
        )
        or projection.get("scenario_model_sha256")
        != scenario["scenario_model_sha256"]
        or projection.get("target_context_bundle_sha256")
        != scenario["target_context_bundle_sha256"]
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario projection content address mismatch"
        )
    model = _mapping(scenario.get("scenario_model"), "scenario_model")
    receipt = _mapping(model.get("dynamic_semantics_receipt"), "execution receipt")
    expected_execution = {
        "hypothesized_initial_health": True,
        "live_policy_target_state_from_bridge": True,
        "team_background_damage": True,
        "attackability_schedule": True,
        "effective_armor_schedule": True,
        "central_candidate_and_background_cancellation": True,
        "per_target_death_and_retarget": True,
        "python_policy_pause_emulation": False,
    }
    required_runtime_receipts = [
        "dynamic_attackability_receipts",
        "dynamic_armor_receipts",
        "dynamic_damage_receipts",
        "dynamic_candidate_damage_receipts",
        "terminal_dynamic_target_state",
    ]
    if (
        model.get("historical_truth") is not False
        or model.get("comparison_eligible") is not False
        or model.get("dynamic_armor_schedule_status")
        != "SIMULATOR_HYPOTHESIS_EXECUTED_BY_LOAD_DYNAMIC_V2"
        or model.get("dynamic_attackability_schedule_status")
        != "SIMULATOR_HYPOTHESIS_EXECUTED_BY_LOAD_DYNAMIC_V2"
        or model.get("health_or_horizon_status")
        != "HYPOTHESIS_BOUND_ENDOGENOUS_TARGET_DEATH_WITH_WATCHDOG"
        or set(receipt) != {
            "schema",
            "status",
            "request_sha256",
            "dynamic_v2_config_sha256",
            "dynamic_rollout_load_contract_sha256",
            "source_binding",
            "same_timestamp_order",
            "executed_by_load_dynamic_v2",
            "runtime_receipts_required",
            "exact_historical_health_claimed",
            "exact_historical_armor_claimed",
            "exact_historical_attackability_claimed",
        }
        or receipt.get("schema") != EXECUTION_RECEIPT_SCHEMA
        or receipt.get("status")
        != "LOAD_DYNAMIC_V2_MECHANISM_EXECUTABLE_HYPOTHESES_ONLY"
        or receipt.get("request_sha256") != sha256_json(scenario["request"])
        or receipt.get("source_binding") != raw.get("source_binding")
        or receipt.get("same_timestamp_order")
        != DYNAMIC_SAME_TIMESTAMP_ORDER_V2
        or receipt.get("executed_by_load_dynamic_v2") != expected_execution
        or receipt.get("runtime_receipts_required")
        != required_runtime_receipts
        or receipt.get("exact_historical_health_claimed") is not False
        or receipt.get("exact_historical_armor_claimed") is not False
        or receipt.get("exact_historical_attackability_claimed") is not False
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 hypothesis was promoted or emulated in Python"
        )
    dynamic_load = DynamicRolloutLoadV2.from_wire(
        scenario["request"],
        _mapping(raw.get("dynamic_rollout_load"), "dynamic_rollout_load"),
    )
    scenario_config = _parse_dynamic_config_v4(
        _mapping(scenario.get("dynamic_load_config"), "dynamic_load_config")
    )
    if (
        dynamic_load.config.content_sha256 != scenario_config.content_sha256
        or receipt.get("dynamic_v2_config_sha256")
        != dynamic_load.config.content_sha256
        or receipt.get("dynamic_rollout_load_contract_sha256")
        != dynamic_load.contract_sha256
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 config/load/receipt binding mismatch"
        )
    source = DynamicScenarioSourceBindingV4(
        **{
            key: item
            for key, item in _mapping(
                raw.get("source_binding"), "source_binding"
            ).items()
            if key != "historical_truth"
        }
    )
    if raw["source_binding"].get("historical_truth") is not False:
        raise FuryDynamicTargetSemanticsV4Error(
            "source binding historical_truth must be false"
        )
    _validate_source_binding(source, dynamic_load.config)
    content = _mapping(raw.get("content_address"), "content_address")
    if content != {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(
            {key: item for key, item in raw.items() if key != "content_address"}
        ),
    }:
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 content address mismatch"
        )
    return raw


def _normalize_scenario_projection_v4(
    value: Mapping[str, Any],
) -> JSONMap:
    """Validate a dynamic-v2 scenario without invoking the v1 formal runner."""

    raw = _strict_json_copy(value, "dynamic-v4 scenario projection")
    expected_fields = set(_IDENTITY_FIELDS) | {
        "request",
        "scenario_model",
        "scenario_model_sha256",
        "target_context_bundle",
        "target_context_bundle_sha256",
        "dynamic_load_config",
    }
    if set(raw) != expected_fields:
        raise FuryDynamicTargetSemanticsV4Error(
            "dynamic-v4 scenario projection field set mismatch"
        )
    identity = _normalize_scenario_identity(
        {key: raw[key] for key in _IDENTITY_FIELDS}
    )
    request = _strict_json_copy(
        _mapping(raw.get("request"), "scenario request"),
        "scenario request",
    )
    _validate_horizon(request, identity["horizon_ms"])
    config = _parse_dynamic_config_v4(
        _mapping(raw.get("dynamic_load_config"), "dynamic_load_config")
    )
    # The seed-independent projection still validates the same request/config
    # target contract used by DynamicRolloutLoadV2.
    targets = _request_targets(request)
    if len(targets) != len(config.target_health):
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario projection target count differs from dynamic-v2 config"
        )
    encounter = _mapping(request.get("encounter"), "request.encounter")
    if encounter.get("useHealth") is not True:
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario projection requires encounter.useHealth == true"
        )
    for index, configured in enumerate(config.target_health):
        stats = targets[index].get("stats")
        if not isinstance(stats, list) or len(stats) <= max(
            ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
        ):
            raise FuryDynamicTargetSemanticsV4Error(
                f"scenario target {index} lacks explicit armor/health stats"
            )
        health = _positive_number(stats[HEALTH_STAT_INDEX], "target health")
        _nonnegative_number(stats[ARMOR_STAT_INDEX], "target armor")
        if struct.pack(">d", health) != struct.pack(">d", configured.health):
            raise FuryDynamicTargetSemanticsV4Error(
                f"scenario target {index} health bits differ from config"
            )

    bundle = _mapping(
        raw.get("target_context_bundle"), "target_context_bundle"
    )
    if set(bundle) != {
        "schema_version",
        "kind",
        "binding_status",
        "request_sha256",
        "target_count",
        "contexts",
        "comparison_eligible",
        "bridge_execution_eligible",
        "limitation_codes",
    }:
        raise FuryDynamicTargetSemanticsV4Error(
            "target context bundle field set mismatch"
        )
    request_sha = sha256_json(request)
    contexts = bundle.get("contexts")
    if (
        bundle.get("schema_version") != SCHEMA_VERSION
        or bundle.get("kind") != TARGET_CONTEXT_BUNDLE_KIND
        or bundle.get("binding_status") != BINDING_STATUS
        or bundle.get("request_sha256") != request_sha
        or bundle.get("target_count") != len(config.target_health)
        or not isinstance(contexts, list)
        or len(contexts) != len(config.target_health)
        or bundle.get("comparison_eligible") is not False
        or bundle.get("bridge_execution_eligible") is not True
        or bundle.get("limitation_codes") != list(LIMITATION_CODES)
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "target context bundle identity or claim boundary mismatch"
        )
    for index, context in enumerate(contexts):
        if (
            not isinstance(context, Mapping)
            or context.get("target_index") != index
            or context.get("mode") != TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS.value
            or context.get("exact_by_declared_contract") is not False
        ):
            raise FuryDynamicTargetSemanticsV4Error(
                "target context bundle does not contain indexed hypotheses"
            )
    bundle_sha = sha256_json(bundle)
    if raw.get("target_context_bundle_sha256") != bundle_sha:
        raise FuryDynamicTargetSemanticsV4Error(
            "target context bundle SHA-256 mismatch"
        )

    model = _mapping(raw.get("scenario_model"), "scenario_model")
    if set(model) != {
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
    }:
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario model field set mismatch"
        )
    if (
        model.get("schema_version") != SCHEMA_VERSION
        or model.get("kind") != SCENARIO_MODEL_KIND
        or model.get("model_status") != MODEL_STATUS
        or model.get("request_sha256") != request_sha
        or model.get("target_context_bundle_sha256") != bundle_sha
        or model.get("historical_truth") is not False
        or model.get("comparison_eligible") is not False
        or model.get("bridge_execution_eligible") is not True
        or model.get("limitation_codes") != list(LIMITATION_CODES)
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario model identity or claim boundary mismatch"
        )
    if raw.get("scenario_model_sha256") != sha256_json(model):
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario model SHA-256 mismatch"
        )
    return {
        **identity,
        "request": request,
        "scenario_model": model,
        "scenario_model_sha256": raw["scenario_model_sha256"],
        "target_context_bundle": bundle,
        "target_context_bundle_sha256": bundle_sha,
        "dynamic_load_config": config.to_wire(),
    }


def _claim_boundary() -> JSONMap:
    return {
        "bridge_execution_eligible": True,
        "diagnostic_only": True,
        "comparison_eligible": False,
        "voting_eligible": False,
        "historical_truth": False,
        "exact_target_health": False,
        "exact_target_armor": False,
        "exact_attackability": False,
        "superiority_claim_allowed": False,
        "formal_runner_connected": False,
    }


def _validate_source_binding(
    source: DynamicScenarioSourceBindingV4,
    config: DynamicTargetSemanticsConfigV2,
) -> None:
    expected = (
        sha256_json([row.to_wire() for row in config.background_damage_events]),
        sha256_json([row.to_wire() for row in config.target_health]),
        sha256_json([row.to_wire() for row in config.attackability_events]),
        sha256_json([row.to_wire() for row in config.effective_armor_events]),
    )
    observed = (
        source.background_schedule_content_sha256,
        source.target_health_hypothesis_content_sha256,
        source.attackability_schedule_hypothesis_content_sha256,
        source.effective_armor_schedule_hypothesis_content_sha256,
    )
    if observed != expected:
        raise FuryDynamicTargetSemanticsV4Error(
            "source schedule hashes differ from the executable dynamic-v2 config"
        )


def _normalize_contexts(
    value: Mapping[int, TargetSemanticsContextV3],
    *,
    config: DynamicTargetSemanticsConfigV2,
    request: Mapping[str, Any],
    health_hypothesis_id: str,
) -> dict[int, TargetSemanticsContextV3]:
    if not isinstance(value, Mapping) or any(type(key) is not int for key in value):
        raise TypeError("target_contexts must be an integer-keyed mapping")
    target_count = len(config.target_health)
    if set(value) != set(range(target_count)):
        raise FuryDynamicTargetSemanticsV4Error(
            "target_contexts must cover target indexes 0..N-1 exactly"
        )
    request_targets = _request_targets(request)
    result: dict[int, TargetSemanticsContextV3] = {}
    for index in range(target_count):
        context = value[index]
        if not isinstance(context, TargetSemanticsContextV3):
            raise TypeError("target contexts must be TargetSemanticsContextV3")
        if (
            context.target_index != index
            or context.mode is not TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS
            or context.target_health_pct_evidence.hypothesis_id
            != health_hypothesis_id
            or context.target_max_health_evidence.hypothesis_id
            != health_hypothesis_id
        ):
            raise FuryDynamicTargetSemanticsV4Error(
                "target context is not bound to the indexed health hypothesis"
            )
        health = config.target_health[index].health
        if not health.is_integer() or context.target_max_health != int(health):
            raise FuryDynamicTargetSemanticsV4Error(
                f"target {index} max health differs from dynamic-v2 config"
            )
        if request_targets[index].get("name") != context.target_name:
            raise FuryDynamicTargetSemanticsV4Error(
                f"target {index} name differs from request"
            )
        result[index] = context
    return result


def _validate_horizon(request: Mapping[str, Any], horizon_ms: int) -> None:
    encounter = _mapping(request.get("encounter"), "request.encounter")
    duration = _positive_number(encounter.get("duration"), "encounter.duration")
    if abs(duration * 1000.0 - horizon_ms) > 1.0:
        raise FuryDynamicTargetSemanticsV4Error(
            "request duration does not match scenario horizon_ms"
        )


def _normalize_scenario_identity(value: Mapping[str, Any]) -> JSONMap:
    raw = _mapping(value, "scenario_identity")
    if set(raw) != _IDENTITY_FIELDS:
        raise FuryDynamicTargetSemanticsV4Error(
            "scenario_identity field set mismatch"
        )
    stratum = _text(raw.get("stratum"), "stratum")
    if stratum not in {"single_target", "multi_target"}:
        raise FuryDynamicTargetSemanticsV4Error(
            "stratum must be single_target or multi_target"
        )
    return {
        "instance_id": _text(raw.get("instance_id"), "instance_id"),
        "component_id": _text(raw.get("component_id"), "component_id"),
        "scenario_id": _text(raw.get("scenario_id"), "scenario_id"),
        "stratum": stratum,
        "scenario_weight": _positive_number(
            raw.get("scenario_weight"), "scenario_weight"
        ),
        "horizon_ms": _positive_int(raw.get("horizon_ms"), "horizon_ms"),
        "estimated_cost_units": _positive_int(
            raw.get("estimated_cost_units"), "estimated_cost_units"
        ),
        "corpus_entry_sha256": _sha256(
            raw.get("corpus_entry_sha256"), "corpus_entry_sha256"
        ),
        "source_scenario_sha256": _sha256(
            raw.get("source_scenario_sha256"), "source_scenario_sha256"
        ),
        "catalog_sha256": _sha256(
            raw.get("catalog_sha256"), "catalog_sha256"
        ),
    }


def _request_targets(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    encounter = _mapping(request.get("encounter"), "request.encounter")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or not targets or any(
        not isinstance(target, Mapping) for target in targets
    ):
        raise FuryDynamicTargetSemanticsV4Error(
            "request.encounter.targets must be a nonempty object array"
        )
    return targets


def _strict_json_copy(value: Mapping[str, Any], label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        result = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FuryDynamicTargetSemanticsV4Error(
            f"{label} is not strict JSON: {error}"
        ) from error
    if not isinstance(result, dict):
        raise TypeError(f"{label} must be an object")
    return result


def _mapping(value: Any, label: str) -> JSONMap:
    if isinstance(value, Mapping):
        return dict(value)
    raise FuryDynamicTargetSemanticsV4Error(f"{label} must be an object")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryDynamicTargetSemanticsV4Error(f"{label} must be nonempty text")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise FuryDynamicTargetSemanticsV4Error(
            f"{label} must be lowercase SHA-256"
        )
    return value


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _signed_int64(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed < -(1 << 63) or parsed > (1 << 63) - 1:
        raise FuryDynamicTargetSemanticsV4Error(
            f"{label} exceeds the signed int64 range"
        )
    return parsed


def _positive_int(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed <= 0:
        raise FuryDynamicTargetSemanticsV4Error(f"{label} must be positive")
    return parsed


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise FuryDynamicTargetSemanticsV4Error(
            f"{label} must be finite and nonnegative"
        )
    return parsed


def _positive_number(value: Any, label: str) -> float:
    parsed = _nonnegative_number(value, label)
    if parsed <= 0:
        raise FuryDynamicTargetSemanticsV4Error(f"{label} must be positive")
    return parsed


__all__ = (
    "BINDING_STATUS",
    "CompiledDynamicTargetSemanticsV4",
    "DYNAMIC_LOAD_BINDING_SCHEMA_V2",
    "DYNAMIC_ROLLOUT_LOAD_SCHEMA_V2",
    "DynamicRolloutLoadV2",
    "DynamicScenarioSourceBindingV4",
    "FuryDynamicTargetSemanticsV4Error",
    "IMPLEMENTATION_REVISION",
    "LIMITATION_CODES",
    "SCHEMA",
    "STATUS",
    "compile_dynamic_target_semantics_binding_v4",
    "dynamic_rollout_load_from_adapter_wire_v2",
    "dynamic_rollout_load_from_config_wire_v2",
    "validate_dynamic_load_request_v2",
    "validate_dynamic_target_semantics_binding_v4",
)
