"""Bind reconstructed waves to the executable dynamic-v1 simulator subset.

This module deliberately separates two facts which used to be easy to blur:

* ``load_dynamic_v1`` really executes hypothesised target HP, team-background
  damage, per-target death, canceled post-death damage, and retargeting; and
* the pinned bridge does *not* execute time-varying attackability or armor.

The returned runner scenario is therefore useful for production diagnostic
rollouts, but is permanently non-voting.  Retained armor/attackability
schedules are content-addressed provenance only.  A non-trivial schedule must
not be emulated by pausing the Python policy or by reloading the simulator,
because auto attacks, DoTs, travel events, pending actions, and RNG state would
not be preserved correctly.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
import struct
from typing import Any, Mapping, Sequence

from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
    dynamic_rollout_load_from_config_wire_v1,
    target_semantics_context_receipt_v3,
)
from .fury_paired_multiseed_runner_v2 import (
    SCENARIO_MODEL_KIND,
    TARGET_CONTEXT_BUNDLE_KIND,
    FuryPairedRunnerError,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
    sha256_json,
)
from .sim_bridge import DynamicTeamBackgroundConfigV1


JSONMap = dict[str, Any]

SCHEMA_VERSION = 3
SCHEMA = "fury_dynamic_target_semantics_binding/v3"
KIND = "fury_dynamic_target_semantics_binding_v3"
IMPLEMENTATION_REVISION = (
    "v3.1_dynamic_v1_team_lifecycle_live_hypothesis_hp_fail_closed_schedules"
)
EXECUTION_RECEIPT_SCHEMA = "fury_dynamic_target_semantics_execution_receipt/v3"
STATUS = "DYNAMIC_V1_TEAM_LIFECYCLE_DIAGNOSTIC_EXECUTABLE"
MODEL_STATUS = "DYNAMIC_V1_TEAM_LIFECYCLE_HYPOTHESIS_NONVOTING"
BINDING_STATUS = "DYNAMIC_V1_TEAM_LIFECYCLE_HYPOTHESIS_NONVOTING"

LIMITATION_CODES = (
    "ATTACKABILITY_SCHEDULE_RETAINED_NOT_EXECUTED",
    "DYNAMIC_ARMOR_SCHEDULE_RETAINED_NOT_EXECUTED",
    "TARGET_ARMOR_NOT_EXACT_HISTORICAL_TRUTH",
    "TARGET_HEALTH_NOT_EXACT_HISTORICAL_TRUTH",
    "TEAM_RESPONSE_FIXED_DRAW_NOT_ENDOGENOUS_MODEL",
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


class FuryDynamicTargetSemanticsV3Error(RuntimeError):
    """The diagnostic dynamic binding is malformed or widens its claims."""


@dataclass(frozen=True)
class DynamicScenarioSourceBindingV3:
    """Content addresses for the draw and the explicit HP hypothesis."""

    background_draw_content_sha256: str
    background_schedule_content_sha256: str
    target_health_hypothesis_content_sha256: str
    target_health_hypothesis_id: str
    target_semantics_source_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "background_draw_content_sha256",
            "background_schedule_content_sha256",
            "target_health_hypothesis_content_sha256",
            "target_semantics_source_sha256",
        ):
            _sha256(getattr(self, field_name), field_name)
        if (
            not isinstance(self.target_health_hypothesis_id, str)
            or _HYPOTHESIS_ID_RE.fullmatch(self.target_health_hypothesis_id) is None
        ):
            raise ValueError(
                "target_health_hypothesis_id must be a normalized identifier"
            )

    def to_dict(self) -> JSONMap:
        return {
            "background_draw_content_sha256": self.background_draw_content_sha256,
            "background_schedule_content_sha256": self.background_schedule_content_sha256,
            "target_health_hypothesis_content_sha256": (
                self.target_health_hypothesis_content_sha256
            ),
            "target_health_hypothesis_id": self.target_health_hypothesis_id,
            "target_semantics_source_sha256": self.target_semantics_source_sha256,
        }


@dataclass(frozen=True)
class RetainedTargetScheduleEvidenceV3:
    """One target's unexecuted, content-addressed dynamic evidence."""

    target_index: int
    armor_schedule_content_sha256: str
    armor_transition_count: int
    attackability_schedule_content_sha256: str
    attackability_transition_count: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.target_index, "target_index")
        _sha256(
            self.armor_schedule_content_sha256,
            "armor_schedule_content_sha256",
        )
        _sha256(
            self.attackability_schedule_content_sha256,
            "attackability_schedule_content_sha256",
        )
        _nonnegative_int(self.armor_transition_count, "armor_transition_count")
        _nonnegative_int(
            self.attackability_transition_count,
            "attackability_transition_count",
        )

    def to_dict(self) -> JSONMap:
        return {
            "target_index": self.target_index,
            "armor_schedule": {
                "content_sha256": self.armor_schedule_content_sha256,
                "transition_count": self.armor_transition_count,
                "status": "RETAINED_PROVENANCE_NOT_EXECUTED",
                "historical_effective_armor_exact": False,
            },
            "attackability_schedule": {
                "content_sha256": self.attackability_schedule_content_sha256,
                "transition_count": self.attackability_transition_count,
                "status": "RETAINED_PROVENANCE_NOT_EXECUTED",
                "historical_attackability_exact": False,
            },
        }


@dataclass(frozen=True)
class CompiledDynamicTargetSemanticsV3:
    """JSON artifact plus runtime contexts for one normalized runner row."""

    artifact: JSONMap
    scenario: JSONMap
    target_contexts: Mapping[int, TargetSemanticsContextV3]


def dynamic_v2_upstream_gap_contract_v3() -> JSONMap:
    """Return the bounded bridge/core patch required for schedule execution."""

    contract: JSONMap = {
        "required_bridge_command": "load_dynamic_v2",
        "minimum_config_fields": [
            "target_health",
            "background_damage_events",
            "attackability_events",
            "effective_armor_events",
            "same_timestamp_order",
            "retarget_mode",
            "content_sha256",
        ],
        "attackability_application_sites": [
            "interactive_action_legality_and_start_attack",
            "auto_swing_target_selection_before_replacement_or_rng",
            "direct_periodic_aoe_and_travel_damage_resolution_without_rng_rewind",
            "background_damage_application",
            "retarget_on_unattackable_and_reactivation",
        ],
        "armor_application": {
            "mechanism": "target.AddStatsDynamic(armor_delta) at a pending action",
            "receipt_requires": [
                "requested_effective_armor",
                "previous_target_armor",
                "resulting_target_armor",
                "time_ms",
                "target_index",
                "schedule_index",
            ],
            "aura_equivalence_claim_allowed": False,
            "historical_armor_truth_claim_allowed": False,
        },
        "same_timestamp_order_required": (
            "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE"
        ),
        "required_receipts": [
            "load_config_digest_and_environment_generation",
            "contiguous_schedule_cursors",
            "attackability_transition_receipts",
            "armor_transition_receipts",
            "candidate_and_background_cancellation_reasons",
            "per_target_final_attackable_armor_health_death_state",
        ],
        "forbidden_emulations": [
            "python_policy_only_action_suppression",
            "segmenting_a_wave_into_reload_calls",
            "mapping_combat_log_silence_to_exact_unattackable",
            "mapping_aura_presence_to_exact_numeric_effective_armor_without_a_hypothesis",
        ],
        "required_tests": [
            "auto_direct_dot_aoe_travel_and_background_attackability_boundaries",
            "same_timestamp_transition_damage_order",
            "armor_change_affects_only_post_transition_physical_resolution",
            "death_unattackable_retarget_and_reactivation_interactions",
            "pending_actions_rng_and_swing_state_survive_transitions",
            "python_go_ieee754_content_digest_equivalence",
        ],
        "current_pinned_bridge_supports_this_contract": False,
    }
    return {
        **contract,
        "content_sha256": sha256_json(contract),
    }


def compile_dynamic_target_semantics_binding_v3(
    *,
    scenario_identity: Mapping[str, Any],
    request: Mapping[str, Any],
    dynamic_config: DynamicTeamBackgroundConfigV1,
    target_contexts: Mapping[int, TargetSemanticsContextV3],
    source_binding: DynamicScenarioSourceBindingV3,
    retained_schedules: Sequence[RetainedTargetScheduleEvidenceV3],
) -> CompiledDynamicTargetSemanticsV3:
    """Compile one dynamic-v1 lifecycle scenario for non-voting diagnostics.

    Current HP seen by the policy comes from the bridge, not a fixed historical
    curve.  Its initial HP, armor, attackability, and fixed team draw remain
    named hypotheses/limitations, so the result cannot enter a comparison.
    """

    identity = _normalize_scenario_identity(scenario_identity)
    request_copy = _strict_json_copy(request, "request")
    if not isinstance(dynamic_config, DynamicTeamBackgroundConfigV1):
        raise TypeError("dynamic_config must be DynamicTeamBackgroundConfigV1")
    if not isinstance(source_binding, DynamicScenarioSourceBindingV3):
        raise TypeError("source_binding must be DynamicScenarioSourceBindingV3")
    horizon_ms = identity["horizon_ms"]
    _validate_request_and_dynamic_config(
        request_copy,
        dynamic_config,
        horizon_ms=horizon_ms,
    )
    contexts = _normalize_contexts(
        target_contexts,
        dynamic_config=dynamic_config,
        request=request_copy,
        health_hypothesis_id=source_binding.target_health_hypothesis_id,
    )
    retained = _normalize_retained_schedules(
        retained_schedules,
        target_count=len(dynamic_config.target_health),
    )
    source = source_binding.to_dict()
    request_sha = sha256_json(request_copy)
    context_receipts = [
        target_semantics_context_receipt_v3(contexts[index])
        for index in range(len(contexts))
    ]
    base_armor_rows = _request_base_armor_rows(request_copy)

    target_bundle: JSONMap = {
        "schema_version": 2,
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
    upstream_gap = dynamic_v2_upstream_gap_contract_v3()
    execution_receipt: JSONMap = {
        "schema": EXECUTION_RECEIPT_SCHEMA,
        "status": "EXECUTABLE_SUBSET_WITH_FAIL_CLOSED_SCHEDULE_GAP",
        "request_sha256": request_sha,
        "dynamic_v1_config_sha256": dynamic_config.content_sha256,
        "source_binding": source,
        "executed_by_load_dynamic_v1": {
            "hypothesized_initial_health": True,
            "live_policy_health_from_bridge_state": True,
            "team_background_damage": True,
            "candidate_plus_background_damage_conservation": True,
            "per_target_death_and_future_damage_cancel": True,
            "retarget_mode": dynamic_config.retarget_mode,
        },
        "request_static_armor_hypotheses": base_armor_rows,
        "retained_not_executed": retained,
        "upstream_gap_contract_sha256": upstream_gap["content_sha256"],
        "exact_historical_health_claimed": False,
        "exact_historical_armor_claimed": False,
        "exact_historical_attackability_claimed": False,
    }
    scenario_model: JSONMap = {
        "schema_version": 2,
        "kind": SCENARIO_MODEL_KIND,
        "model_status": MODEL_STATUS,
        "request_sha256": request_sha,
        "target_context_bundle_sha256": target_bundle_sha,
        "historical_truth": False,
        "comparison_eligible": False,
        "bridge_execution_eligible": True,
        "dynamic_armor_schedule_status": (
            "STATIC_REQUEST_HYPOTHESIS_ONLY_TRANSITIONS_RETAINED_NOT_EXECUTED"
        ),
        "dynamic_attackability_schedule_status": (
            "FULL_WAVE_ASSUMPTION_ONLY_TRANSITIONS_RETAINED_NOT_EXECUTED"
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
    try:
        scenario = normalize_runner_scenarios([candidate])[0]
    except FuryPairedRunnerError as error:
        raise FuryDynamicTargetSemanticsV3Error(
            f"paired runner rejected dynamic diagnostic scenario: {error}"
        ) from error

    runner_projection: JSONMap = {
        "scenario": scenario,
        "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256([scenario]),
        "scenario_model_sha256": scenario["scenario_model_sha256"],
        "target_context_bundle_sha256": scenario[
            "target_context_bundle_sha256"
        ],
    }
    core: JSONMap = {
        "schema_version": SCHEMA_VERSION,
        "schema": SCHEMA,
        "kind": KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "source_binding": source,
        "bridge_capability": {
            "load_command": "load_dynamic_v1",
            "dynamic_config_schema": "o2o_dynamic_team_background/v1",
            "health_team_damage_death_cancel_retarget_executed": True,
            "live_policy_health_reads_bridge_state": True,
            "dynamic_armor_schedule_executed": False,
            "dynamic_attackability_schedule_executed": False,
        },
        "runner_projection": runner_projection,
        "upstream_gap_contract": upstream_gap,
        "claim_boundary": {
            "bridge_execution_eligible": True,
            "diagnostic_only": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "historical_truth": False,
            "exact_target_health": False,
            "exact_target_armor": False,
            "exact_attackability": False,
            "superiority_claim_allowed": False,
        },
    }
    artifact = deepcopy(core)
    artifact["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_dynamic_target_semantics_binding_v3(artifact)
    return CompiledDynamicTargetSemanticsV3(
        artifact=artifact,
        scenario=scenario,
        target_contexts=contexts,
    )


def validate_dynamic_target_semantics_binding_v3(
    value: Mapping[str, Any],
) -> JSONMap:
    """Validate the immutable diagnostic binding without widening it."""

    raw = _strict_json_copy(value, "dynamic target-semantics binding")
    expected_fields = {
        "schema_version",
        "schema",
        "kind",
        "implementation_revision",
        "status",
        "source_binding",
        "bridge_capability",
        "runner_projection",
        "upstream_gap_contract",
        "claim_boundary",
        "content_address",
    }
    if set(raw) != expected_fields:
        raise FuryDynamicTargetSemanticsV3Error(
            "dynamic target-semantics binding field set mismatch"
        )
    if (
        raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("schema") != SCHEMA
        or raw.get("kind") != KIND
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("status") != STATUS
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "unsupported dynamic target-semantics binding identity"
        )
    source = _mapping(raw.get("source_binding"), "source_binding")
    if set(source) != {
        "background_draw_content_sha256",
        "background_schedule_content_sha256",
        "target_health_hypothesis_content_sha256",
        "target_health_hypothesis_id",
        "target_semantics_source_sha256",
    }:
        raise FuryDynamicTargetSemanticsV3Error("source_binding field set mismatch")
    DynamicScenarioSourceBindingV3(**source)

    capability = _mapping(raw.get("bridge_capability"), "bridge_capability")
    if capability != {
        "load_command": "load_dynamic_v1",
        "dynamic_config_schema": "o2o_dynamic_team_background/v1",
        "health_team_damage_death_cancel_retarget_executed": True,
        "live_policy_health_reads_bridge_state": True,
        "dynamic_armor_schedule_executed": False,
        "dynamic_attackability_schedule_executed": False,
    }:
        raise FuryDynamicTargetSemanticsV3Error(
            "bridge capability claim differs from pinned dynamic-v1 support"
        )
    boundary = _mapping(raw.get("claim_boundary"), "claim_boundary")
    if boundary != {
        "bridge_execution_eligible": True,
        "diagnostic_only": True,
        "comparison_eligible": False,
        "voting_eligible": False,
        "historical_truth": False,
        "exact_target_health": False,
        "exact_target_armor": False,
        "exact_attackability": False,
        "superiority_claim_allowed": False,
    }:
        raise FuryDynamicTargetSemanticsV3Error("claim boundary was widened")

    expected_gap = dynamic_v2_upstream_gap_contract_v3()
    if raw.get("upstream_gap_contract") != expected_gap:
        raise FuryDynamicTargetSemanticsV3Error(
            "upstream gap contract differs from the pinned fail-closed audit"
        )
    projection = _mapping(raw.get("runner_projection"), "runner_projection")
    if set(projection) != {
        "scenario",
        "runner_scenario_bundle_sha256",
        "scenario_model_sha256",
        "target_context_bundle_sha256",
    }:
        raise FuryDynamicTargetSemanticsV3Error(
            "runner_projection field set mismatch"
        )
    try:
        normalized = normalize_runner_scenarios(
            [_mapping(projection.get("scenario"), "runner scenario")]
        )[0]
    except FuryPairedRunnerError as error:
        raise FuryDynamicTargetSemanticsV3Error(
            f"runner rejected stored scenario: {error}"
        ) from error
    if normalized != projection["scenario"]:
        raise FuryDynamicTargetSemanticsV3Error(
            "stored runner scenario is not canonical"
        )
    if (
        projection.get("runner_scenario_bundle_sha256")
        != runner_scenario_bundle_sha256([normalized])
        or projection.get("scenario_model_sha256")
        != normalized["scenario_model_sha256"]
        or projection.get("target_context_bundle_sha256")
        != normalized["target_context_bundle_sha256"]
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "runner projection content address mismatch"
        )
    model = normalized["scenario_model"]
    target_bundle = normalized["target_context_bundle"]
    if (
        model.get("comparison_eligible") is not False
        or model.get("bridge_execution_eligible") is not True
        or target_bundle.get("comparison_eligible") is not False
        or target_bundle.get("bridge_execution_eligible") is not True
        or model.get("historical_truth") is not False
        or tuple(model.get("limitation_codes", ())) != LIMITATION_CODES
        or tuple(target_bundle.get("limitation_codes", ())) != LIMITATION_CODES
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "stored runner scenario widened dynamic-v1 scientific eligibility"
        )
    contexts = target_bundle.get("contexts")
    if not isinstance(contexts, list) or not contexts:
        raise FuryDynamicTargetSemanticsV3Error(
            "stored target contexts must be a nonempty array"
        )
    for index, context in enumerate(contexts):
        if (
            not isinstance(context, dict)
            or context.get("target_index") != index
            or context.get("mode") != TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS.value
            or context.get("exact_by_declared_contract") is not False
            or context.get("health_pct_schedule") != []
        ):
            raise FuryDynamicTargetSemanticsV3Error(
                "stored context is not a live simulator hypothesis"
            )
    receipt = _mapping(
        model.get("dynamic_semantics_receipt"),
        "dynamic_semantics_receipt",
    )
    if (
        receipt.get("schema") != EXECUTION_RECEIPT_SCHEMA
        or receipt.get("status")
        != "EXECUTABLE_SUBSET_WITH_FAIL_CLOSED_SCHEDULE_GAP"
        or receipt.get("request_sha256") != normalized["request_sha256"]
        or receipt.get("dynamic_v1_config_sha256")
        != normalized["dynamic_load_config"]["content_sha256"]
        or receipt.get("source_binding") != source
        or receipt.get("upstream_gap_contract_sha256")
        != expected_gap["content_sha256"]
        or receipt.get("exact_historical_health_claimed") is not False
        or receipt.get("exact_historical_armor_claimed") is not False
        or receipt.get("exact_historical_attackability_claimed") is not False
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "dynamic execution receipt is not bound to the executable subset"
        )
    expected_executed = {
        "hypothesized_initial_health": True,
        "live_policy_health_from_bridge_state": True,
        "team_background_damage": True,
        "candidate_plus_background_damage_conservation": True,
        "per_target_death_and_future_damage_cancel": True,
        "retarget_mode": normalized["dynamic_load_config"]["retarget_mode"],
    }
    if receipt.get("executed_by_load_dynamic_v1") != expected_executed:
        raise FuryDynamicTargetSemanticsV3Error(
            "dynamic execution receipt overstates or omits the dynamic-v1 subset"
        )
    if receipt.get("request_static_armor_hypotheses") != _request_base_armor_rows(
        normalized["request"]
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "static armor/health receipt differs from the exact simulator request"
        )
    retained = receipt.get("retained_not_executed")
    if not isinstance(retained, list) or len(retained) != len(contexts):
        raise FuryDynamicTargetSemanticsV3Error(
            "retained target schedule evidence does not cover every target"
        )
    for index, row in enumerate(retained):
        _validate_retained_schedule_row(row, expected_index=index)

    address = _mapping(raw.get("content_address"), "content_address")
    if set(address) != {"algorithm", "scope", "sha256"} or (
        address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON document without content_address"
    ):
        raise FuryDynamicTargetSemanticsV3Error("invalid content-address contract")
    unsigned = deepcopy(raw)
    unsigned.pop("content_address")
    if address.get("sha256") != sha256_json(unsigned):
        raise FuryDynamicTargetSemanticsV3Error(
            "dynamic target-semantics content SHA-256 mismatch"
        )
    return raw


def _normalize_scenario_identity(value: Mapping[str, Any]) -> JSONMap:
    row = _mapping(value, "scenario_identity")
    if set(row) != _IDENTITY_FIELDS:
        raise FuryDynamicTargetSemanticsV3Error(
            "scenario_identity field set mismatch"
        )
    stratum = _text(row.get("stratum"), "stratum")
    if stratum not in {"single_target", "multi_target"}:
        raise FuryDynamicTargetSemanticsV3Error(
            "stratum must be single_target or multi_target"
        )
    weight = _positive_number(row.get("scenario_weight"), "scenario_weight")
    return {
        "instance_id": _text(row.get("instance_id"), "instance_id"),
        "component_id": _text(row.get("component_id"), "component_id"),
        "scenario_id": _text(row.get("scenario_id"), "scenario_id"),
        "stratum": stratum,
        "scenario_weight": weight,
        "horizon_ms": _positive_int(row.get("horizon_ms"), "horizon_ms"),
        "estimated_cost_units": _positive_int(
            row.get("estimated_cost_units"), "estimated_cost_units"
        ),
        "corpus_entry_sha256": _sha256(
            row.get("corpus_entry_sha256"), "corpus_entry_sha256"
        ),
        "source_scenario_sha256": _sha256(
            row.get("source_scenario_sha256"), "source_scenario_sha256"
        ),
        "catalog_sha256": _sha256(row.get("catalog_sha256"), "catalog_sha256"),
    }


def _validate_request_and_dynamic_config(
    request: Mapping[str, Any],
    config: DynamicTeamBackgroundConfigV1,
    *,
    horizon_ms: int,
) -> None:
    encounter = _mapping(request.get("encounter"), "request.encounter")
    if encounter.get("useHealth") is not True:
        raise FuryDynamicTargetSemanticsV3Error(
            "dynamic diagnostic request requires encounter.useHealth == true"
        )
    duration = _positive_number(encounter.get("duration"), "encounter.duration")
    if abs(duration * 1000.0 - horizon_ms) > 1.0:
        raise FuryDynamicTargetSemanticsV3Error(
            "request duration does not match scenario horizon_ms"
        )
    try:
        dynamic_rollout_load_from_config_wire_v1(request, 0, config.to_wire())
    except (TypeError, ValueError) as error:
        raise FuryDynamicTargetSemanticsV3Error(
            f"request and dynamic-v1 config are not bit-bound: {error}"
        ) from error


def _normalize_contexts(
    value: Mapping[int, TargetSemanticsContextV3],
    *,
    dynamic_config: DynamicTeamBackgroundConfigV1,
    request: Mapping[str, Any],
    health_hypothesis_id: str,
) -> dict[int, TargetSemanticsContextV3]:
    if not isinstance(value, Mapping):
        raise TypeError("target_contexts must be a mapping")
    target_count = len(dynamic_config.target_health)
    if any(type(index) is not int for index in value) or set(value) != set(
        range(target_count)
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "target_contexts must cover target indexes 0..N-1 exactly"
        )
    request_targets = _request_targets(request)
    result: dict[int, TargetSemanticsContextV3] = {}
    for index in range(target_count):
        context = value[index]
        if not isinstance(context, TargetSemanticsContextV3):
            raise TypeError("target_contexts values must be TargetSemanticsContextV3")
        if (
            context.target_index != index
            or context.mode is not TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS
        ):
            raise FuryDynamicTargetSemanticsV3Error(
                "dynamic target context must be indexed SIMULATOR_HYPOTHESIS"
            )
        if (
            context.target_health_pct_evidence.hypothesis_id
            != health_hypothesis_id
            or context.target_max_health_evidence.hypothesis_id
            != health_hypothesis_id
        ):
            raise FuryDynamicTargetSemanticsV3Error(
                f"target {index} health evidence differs from the source hypothesis identity"
            )
        configured_health = dynamic_config.target_health[index].health
        if not configured_health.is_integer() or (
            context.target_max_health != int(configured_health)
        ):
            raise FuryDynamicTargetSemanticsV3Error(
                f"target {index} context max health differs from the integral dynamic hypothesis"
            )
        request_name = request_targets[index].get("name")
        if request_name != context.target_name:
            raise FuryDynamicTargetSemanticsV3Error(
                f"target {index} context name differs from the simulator request"
            )
        result[index] = context
    return result


def _normalize_retained_schedules(
    values: Sequence[RetainedTargetScheduleEvidenceV3],
    *,
    target_count: int,
) -> list[JSONMap]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("retained_schedules must be a sequence")
    if len(values) != target_count:
        raise FuryDynamicTargetSemanticsV3Error(
            "retained_schedules must cover every dynamic target"
        )
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, RetainedTargetScheduleEvidenceV3):
            raise TypeError(
                "retained_schedules entries must be RetainedTargetScheduleEvidenceV3"
            )
        if value.target_index != index:
            raise FuryDynamicTargetSemanticsV3Error(
                "retained_schedules must cover target indexes in ascending order"
            )
        result.append(value.to_dict())
    return result


def _request_targets(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    encounter = _mapping(request.get("encounter"), "request.encounter")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or not targets or any(
        not isinstance(target, Mapping) for target in targets
    ):
        raise FuryDynamicTargetSemanticsV3Error(
            "request.encounter.targets must be a nonempty object array"
        )
    return targets


def _request_base_armor_rows(request: Mapping[str, Any]) -> list[JSONMap]:
    rows = []
    for index, target in enumerate(_request_targets(request)):
        stats = target.get("stats")
        if not isinstance(stats, list) or len(stats) <= max(
            ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
        ):
            raise FuryDynamicTargetSemanticsV3Error(
                f"request target {index} lacks explicit armor/health stats"
            )
        armor = _nonnegative_number(
            stats[ARMOR_STAT_INDEX], f"request target {index} armor"
        )
        health = _positive_number(
            stats[HEALTH_STAT_INDEX], f"request target {index} health"
        )
        rows.append(
            {
                "target_index": index,
                "armor_ieee754_binary64_hex": struct.pack(">d", armor).hex(),
                "health_ieee754_binary64_hex": struct.pack(">d", health).hex(),
                "armor_status": "STATIC_REQUEST_SENSITIVITY_HYPOTHESIS",
                "health_status": "NAMED_INITIAL_HP_HYPOTHESIS",
                "historical_truth": False,
            }
        )
    return rows


def _validate_retained_schedule_row(value: Any, *, expected_index: int) -> None:
    row = _mapping(value, "retained schedule row")
    if set(row) != {"target_index", "armor_schedule", "attackability_schedule"}:
        raise FuryDynamicTargetSemanticsV3Error(
            "retained schedule row field set mismatch"
        )
    if row.get("target_index") != expected_index:
        raise FuryDynamicTargetSemanticsV3Error(
            "retained schedule target indexes are not contiguous"
        )
    for field, exact_field in (
        ("armor_schedule", "historical_effective_armor_exact"),
        ("attackability_schedule", "historical_attackability_exact"),
    ):
        schedule = _mapping(row.get(field), field)
        if set(schedule) != {
            "content_sha256",
            "transition_count",
            "status",
            exact_field,
        }:
            raise FuryDynamicTargetSemanticsV3Error(
                f"{field} field set mismatch"
            )
        _sha256(schedule.get("content_sha256"), f"{field}.content_sha256")
        _nonnegative_int(schedule.get("transition_count"), f"{field}.transition_count")
        if (
            schedule.get("status") != "RETAINED_PROVENANCE_NOT_EXECUTED"
            or schedule.get(exact_field) is not False
        ):
            raise FuryDynamicTargetSemanticsV3Error(
                f"{field} was promoted beyond retained provenance"
            )


def _strict_json_copy(value: Mapping[str, Any], label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        result = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FuryDynamicTargetSemanticsV3Error(
            f"{label} is not strict JSON: {error}"
        ) from error
    if not isinstance(result, dict):
        raise TypeError(f"{label} must be an object")
    return result


def _mapping(value: Any, label: str) -> JSONMap:
    if not isinstance(value, dict):
        if isinstance(value, Mapping):
            return dict(value)
        raise FuryDynamicTargetSemanticsV3Error(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryDynamicTargetSemanticsV3Error(f"{label} must be nonempty text")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise FuryDynamicTargetSemanticsV3Error(
            f"{label} must be lowercase SHA-256"
        )
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FuryDynamicTargetSemanticsV3Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result <= 0:
        raise FuryDynamicTargetSemanticsV3Error(f"{label} must be positive")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryDynamicTargetSemanticsV3Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise FuryDynamicTargetSemanticsV3Error(
            f"{label} must be finite and nonnegative"
        )
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _nonnegative_number(value, label)
    if result <= 0:
        raise FuryDynamicTargetSemanticsV3Error(f"{label} must be positive")
    return result


__all__ = (
    "CompiledDynamicTargetSemanticsV3",
    "DynamicScenarioSourceBindingV3",
    "FuryDynamicTargetSemanticsV3Error",
    "RetainedTargetScheduleEvidenceV3",
    "compile_dynamic_target_semantics_binding_v3",
    "dynamic_v2_upstream_gap_contract_v3",
    "validate_dynamic_target_semantics_binding_v3",
)
