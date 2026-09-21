"""Bind the compiled Incantagos hypothesis to one responsive bridge session.

This module is intentionally separate from the exact source-bound rollout
worker.  The compiled Incantagos case contains outcome-derived target and
roster hypotheses, so it may exercise the responsive wire path but cannot
create comparison, training, voting, or deployment evidence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .responsive_team_bridge_adapter_v1 import (
    CausalBranchBindingV1,
    LoadedResponsiveTeammateModelV1,
    ResponsiveTeamBridgeAdapterV1,
)
from .sim_bridge_dynamic_v4 import DynamicLoadResultV4
from .upper_kara_responsive_incantagos_case_v1 import (
    SCHEMA as CASE_SCHEMA,
    STATUS as CASE_STATUS,
    CompiledResponsiveIncantagosCaseV1,
)


JSONMap = dict[str, Any]

SCHEMA = "responsive_incantagos_development_session/v1"
STATUS = "COMPLETE_DEVELOPMENT_WIRE_BINDING_NOT_COMPARISON_AUTHORIZED"


class ResponsiveIncantagosDevelopmentSessionV1Error(RuntimeError):
    """The development case, model, or bridge load cannot be bound safely."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            f"{label} must be an object"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            f"{label} must be nonempty text"
        )
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            f"development session prefix is not strict JSON: {error}"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _validate_development_boundaries(
    case: CompiledResponsiveIncantagosCaseV1,
    loaded_model: LoadedResponsiveTeammateModelV1,
) -> None:
    receipt = _mapping(case.receipt, "compiled case receipt")
    if receipt.get("schema") != CASE_SCHEMA or receipt.get("status") != CASE_STATUS:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "compiled case schema or status differs from the development contract"
        )
    boundaries = _mapping(
        receipt.get("scientific_boundaries"), "compiled case scientific boundaries"
    )
    if (
        boundaries.get("development_only") is not True
        or boundaries.get("comparison_authorized") is not False
        or boundaries.get("policy_training_authorized") is not False
        or boundaries.get("deployment_authorized") is not False
        or boundaries.get("heldout_performance_evidence_eligible") is not False
        or boundaries.get("actor_roster_is_outcome_derived") is not True
    ):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "compiled case scientific boundary differs from development-only scope"
        )
    source = _mapping(receipt.get("source"), "compiled case source")
    membership = _mapping(
        source.get("source_membership_evidence"),
        "compiled case source membership evidence",
    )
    if (
        membership.get("heldout_performance_evidence_eligible") is not False
        or membership.get("comparison_authorized") is not False
    ):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "compiled source must remain performance-ineligible and noncomparison"
        )
    model_training_held_out = membership.get("model_training_held_out", False)
    if not isinstance(model_training_held_out, bool):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "case model_training_held_out must be boolean"
        )
    if model_training_held_out and membership.get("split") != "VALIDATION":
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "model-held-out case must use the validation component split"
        )
    if loaded_model.provenance.current_source_held_out is not model_training_held_out:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "loaded model current source differs from case model-training split"
        )
    current_source = _mapping(
        loaded_model.current_source_evidence,
        "loaded model current source evidence",
    )
    if current_source.get("current_source_held_out") is not model_training_held_out:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "loaded model evidence differs from case model-training split"
        )
    for membership_field, current_field in (
        ("stage5_content_sha256", "current_source_stage5_content_sha256"),
        ("component_id", "current_source_component_id"),
    ):
        expected = membership.get(membership_field)
        observed = current_source.get(current_field)
        if expected is not None and observed is not None and expected != observed:
            raise ResponsiveIncantagosDevelopmentSessionV1Error(
                f"loaded model current source differs from the case at {membership_field}"
            )


def responsive_incantagos_development_prefix_sha256_v1(
    case: CompiledResponsiveIncantagosCaseV1,
    loaded_model: LoadedResponsiveTeammateModelV1,
) -> str:
    """Address the concrete development hypothesis supplied before the branch."""

    if not isinstance(case, CompiledResponsiveIncantagosCaseV1):
        raise TypeError("case must be CompiledResponsiveIncantagosCaseV1")
    if not isinstance(loaded_model, LoadedResponsiveTeammateModelV1):
        raise TypeError("loaded_model must be LoadedResponsiveTeammateModelV1")
    _validate_development_boundaries(case, loaded_model)
    runtime = case.runtime
    actors = []
    for guid in sorted(runtime.actors):
        actor = runtime.actors[guid]
        actors.append(
            {
                "player_guid": guid,
                "metadata": dict(actor.metadata),
                "current_target_guid": actor.current_target_guid,
                "live_prefix_snapshot": runtime.snapshot_for_actor(guid),
            }
        )
    document = {
        "schema": f"{SCHEMA}/development_prefix_identity",
        "compiled_case_receipt": case.receipt,
        "request": case.request,
        "dynamic_config": case.dynamic_config.to_wire(),
        "runtime": {
            "time_ms": runtime.time_ms,
            "kill_clock_ms": runtime.kill_clock_ms,
            "actors": actors,
            "target_health_by_guid": [
                {"target_guid": guid, "current_health": runtime.health[guid]}
                for guid in sorted(runtime.health)
            ],
            "target_introduced_at_ms_by_guid": dict(
                sorted(runtime.target_introduced_at_ms_by_guid.items())
            ),
        },
        "candidate_player_guid": case.candidate_player_guid,
        "teammate_player_guids": list(case.teammate_player_guids),
        "native_target_guids": list(case.native_target_guids),
        "model_provenance": loaded_model.provenance.to_wire(),
        "current_source_evidence": dict(loaded_model.current_source_evidence),
    }
    return _canonical_sha256(document)


@dataclass(frozen=True)
class BoundResponsiveIncantagosDevelopmentSessionV1:
    """One loaded environment plus its live responsive-team adapter."""

    load_result: DynamicLoadResultV4
    branch: CausalBranchBindingV1
    adapter: ResponsiveTeamBridgeAdapterV1
    initial_wake: JSONMap | None
    receipt: JSONMap


def bind_responsive_incantagos_development_session_v1(
    *,
    bridge: Any,
    case: CompiledResponsiveIncantagosCaseV1,
    loaded_model: LoadedResponsiveTeammateModelV1,
    simulator_seed: int,
    teammate_seed: int,
    pair_id: str,
    branch_id: str,
    candidate_suffix_id: str,
) -> BoundResponsiveIncantagosDevelopmentSessionV1:
    """Load, branch-bind, and arm one Incantagos development session."""

    if not isinstance(case, CompiledResponsiveIncantagosCaseV1):
        raise TypeError("case must be CompiledResponsiveIncantagosCaseV1")
    if not isinstance(loaded_model, LoadedResponsiveTeammateModelV1):
        raise TypeError("loaded_model must be LoadedResponsiveTeammateModelV1")
    simulator_seed = _nonnegative_integer(simulator_seed, "simulator_seed")
    teammate_seed = _nonnegative_integer(teammate_seed, "teammate_seed")
    pair_id = _text(pair_id, "pair_id")
    branch_id = _text(branch_id, "branch_id")
    candidate_suffix_id = _text(candidate_suffix_id, "candidate_suffix_id")
    _validate_development_boundaries(case, loaded_model)

    if case.dynamic_config.background_damage_events:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "responsive development case must remove fixed background damage"
        )
    target_guids = tuple(case.native_target_guids)
    runtime = deepcopy(case.runtime)
    if not target_guids or len(set(target_guids)) != len(target_guids):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "native target registry must be nonempty and unique"
        )
    if set(target_guids) != set(runtime.health):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "native target registry differs from responsive runtime health"
        )
    if dict(case.target_introduced_at_ms_by_guid) != dict(
        runtime.target_introduced_at_ms_by_guid
    ):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "compiled target introductions differ from responsive runtime"
        )
    actor_guids = set(runtime.actors)
    candidate = case.candidate_player_guid
    if candidate not in actor_guids:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "candidate player is absent from responsive runtime"
        )
    if set(case.teammate_player_guids) != actor_guids - {candidate}:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "teammate registry differs from responsive runtime actors"
        )

    prefix_sha = responsive_incantagos_development_prefix_sha256_v1(
        case, loaded_model
    )
    load_result = bridge.load_dynamic_v4(
        deepcopy(case.request), simulator_seed, case.dynamic_config
    )
    if not isinstance(load_result, DynamicLoadResultV4):
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "bridge load_dynamic_v4 did not return DynamicLoadResultV4"
        )
    if load_result.receipt.config_digest != case.dynamic_config.content_sha256:
        raise ResponsiveIncantagosDevelopmentSessionV1Error(
            "bridge load receipt differs from the compiled dynamic config"
        )
    branch = CausalBranchBindingV1(
        pair_id=pair_id,
        branch_id=branch_id,
        candidate_suffix_id=candidate_suffix_id,
        prefix_content_sha256=prefix_sha,
        simulator_seed=simulator_seed,
        teammate_seed=teammate_seed,
        environment_generation=load_result.receipt.environment_generation,
        dynamic_config_sha256=case.dynamic_config.content_sha256,
        model_provenance_sha256=loaded_model.provenance.content_sha256,
    )
    adapter = ResponsiveTeamBridgeAdapterV1(
        bridge=bridge,
        runtime=runtime,
        loaded_model=loaded_model,
        candidate_actor_guid=candidate,
        target_guid_by_index=target_guids,
        branch=branch,
        wake_horizon_exclusive_ms=case.dynamic_config.idle_advance_horizon_ms,
    )
    initial_wake = adapter.arm_global_next()
    receipt: JSONMap = {
        "schema": SCHEMA,
        "status": STATUS,
        "compiled_case_schema": CASE_SCHEMA,
        "compiled_case_status": CASE_STATUS,
        "development_prefix_content_sha256": prefix_sha,
        "branch_binding": branch.to_wire(),
        "dynamic_config_sha256": case.dynamic_config.content_sha256,
        "environment_generation": load_result.receipt.environment_generation,
        "initial_wake": dict(initial_wake) if initial_wake is not None else None,
        "wake_horizon_exclusive_ms": case.dynamic_config.idle_advance_horizon_ms,
        "future_target_arrival_supported": True,
        "fixed_background_damage_event_count": 0,
        "responsive_teammate_count": len(case.teammate_player_guids),
        "exact_source_bound_worker_used": False,
        "prefix_identity_scope": "DEVELOPMENT_OUTCOME_DERIVED_HYPOTHESIS",
        "scientific_boundaries": {
            "development_only": True,
            "comparison_authorized": False,
            "policy_training_authorized": False,
            "voting_eligible": False,
            "deployment_authorized": False,
            "heldout_performance_evidence_eligible": False,
            "future_candidate_suffix_visible_to_teammate_model": False,
        },
    }
    return BoundResponsiveIncantagosDevelopmentSessionV1(
        load_result=load_result,
        branch=branch,
        adapter=adapter,
        initial_wake=(dict(initial_wake) if initial_wake is not None else None),
        receipt=receipt,
    )


__all__ = (
    "BoundResponsiveIncantagosDevelopmentSessionV1",
    "ResponsiveIncantagosDevelopmentSessionV1Error",
    "SCHEMA",
    "STATUS",
    "bind_responsive_incantagos_development_session_v1",
    "responsive_incantagos_development_prefix_sha256_v1",
)
