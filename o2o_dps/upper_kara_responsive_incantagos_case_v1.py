"""Compile the resolved Incantagos dev case for responsive teammates.

This is deliberately a narrow development adapter.  It removes the fixed
leave-one-out damage schedule before attaching the learned teammate runtime;
it does not turn the selected historical attempt into held-out evidence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .chronicle_external_team_wave_model_v2 import (
    PARTITION_RECORD_SCHEMA as TEAM_WAVE_RECORD_SCHEMA,
    STATUS as TEAM_WAVE_STATUS,
)
from .chronicle_external_teammate_response_model_v1 import (
    DynamicTeamRuntimeV1,
    _actor_metadata,
)
from .sim_bridge_dynamic_v4 import DynamicTargetSemanticsConfigV4
from .upper_kara_resolved_dynamic_v4_adapter_v1 import (
    CompiledResolvedIncantagosDynamicV4CaseV1,
)
from .upper_kara_trash_dynamic_v4_adapter_v1 import (
    ALL_THREE_FROM_T0_UNTIL_SIM_DEATH,
    CompiledResolvedTrashDynamicV4CaseV1,
    OBSERVED_ONSET_UNTIL_SIM_DEATH,
)


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_responsive_incantagos_case/v1"
STATUS = "DEVELOPMENT_ONLY_RESPONSIVE_TEAM_NOT_COMPARISON_AUTHORIZED"


class UpperKaraResponsiveIncantagosCaseV1Error(ValueError):
    """The fixed case and Stage-5 wave cannot be bound without claim drift."""


@dataclass(frozen=True)
class CompiledResponsiveIncantagosCaseV1:
    request: JSONMap
    dynamic_config: DynamicTargetSemanticsConfigV4
    runtime: DynamicTeamRuntimeV1
    native_target_guids: tuple[str, ...]
    target_introduced_at_ms_by_guid: Mapping[str, int]
    actors: tuple[JSONMap, ...]
    candidate_player_guid: str
    teammate_player_guids: tuple[str, ...]
    team_only_sidecar: tuple[JSONMap, ...]
    receipt: JSONMap


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            f"{label} must be a list"
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _case_focal_player_guid(
    fixed_case: CompiledResolvedIncantagosDynamicV4CaseV1,
) -> str:
    """Recover the focal binding retained in the team-only sidecar.

    The resolved adapter intentionally did not expose the compact model itself,
    but every Incantagos team-only row retains the same focal leave-one-out
    identity.  Refusing an empty or disagreeing set is safer than accepting a
    caller-supplied focal identity that is not bound to the fixed case.
    """

    focals: set[str] = set()
    for raw in fixed_case.team_only_sidecar:
        row = _mapping(raw, "team-only sidecar row")
        model = _mapping(
            row.get("team_kill_clock_model"), "team-only kill-clock model"
        )
        focals.add(
            _text(
                model.get("focal_player_guid"),
                "team-only kill-clock focal_player_guid",
            )
        )
    if len(focals) != 1:
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "Incantagos sidecar must bind exactly one focal player"
        )
    return next(iter(focals))


def _native_introductions(
    fixed_case: CompiledResolvedIncantagosDynamicV4CaseV1,
) -> tuple[tuple[str, ...], dict[str, int]]:
    registry = fixed_case.occurrence_index_registry
    expected_indexes = tuple(range(len(registry)))
    actual_indexes = tuple(row.target_index for row in registry)
    if actual_indexes != expected_indexes:
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "native occurrence registry must cover indexes 0..N-1 in order"
        )
    native_guids = tuple(row.target_guid for row in registry)
    if len(set(native_guids)) != len(native_guids):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "native occurrence registry repeats a target GUID"
        )

    receipt_rows = _array(
        fixed_case.receipt.get("activity_windows_inclusive_ms"),
        "fixed-case activity windows",
    )
    by_index: dict[int, Mapping[str, Any]] = {}
    for raw in receipt_rows:
        row = _mapping(raw, "activity-window row")
        index = _nonnegative_integer(row.get("target_index"), "target_index")
        if index in by_index:
            raise UpperKaraResponsiveIncantagosCaseV1Error(
                "activity windows repeat a native target index"
            )
        by_index[index] = row
    if set(by_index) != set(expected_indexes):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "activity windows must cover exactly the native target registry"
        )

    introductions: dict[str, int] = {}
    for registry_row in registry:
        row = by_index[registry_row.target_index]
        if row.get("occurrence_id") != registry_row.occurrence_id:
            raise UpperKaraResponsiveIncantagosCaseV1Error(
                "activity-window occurrence differs from native registry"
            )
        windows = _array(row.get("windows"), "target activity windows")
        if not windows:
            raise UpperKaraResponsiveIncantagosCaseV1Error(
                "each native target needs an activity window"
            )
        starts: list[int] = []
        previous_end = -1
        for raw_window in windows:
            if not isinstance(raw_window, (list, tuple)) or len(raw_window) != 2:
                raise UpperKaraResponsiveIncantagosCaseV1Error(
                    "each activity window must be [start_ms, end_ms]"
                )
            start = _nonnegative_integer(raw_window[0], "activity-window start")
            end = _nonnegative_integer(raw_window[1], "activity-window end")
            if end < start or start <= previous_end:
                raise UpperKaraResponsiveIncantagosCaseV1Error(
                    "activity windows must be ordered, disjoint, and nonempty"
                )
            starts.append(start)
            previous_end = end
        introductions[registry_row.target_guid] = starts[0]
    return native_guids, introductions


def _observed_actors(
    wave_record: Mapping[str, Any],
) -> tuple[tuple[JSONMap, ...], dict[str, Mapping[str, Any]]]:
    actors: list[JSONMap] = []
    records_by_guid: dict[str, Mapping[str, Any]] = {}
    for raw in _array(wave_record.get("players"), "wave players"):
        player_record = _mapping(raw, "wave player")
        player = _mapping(player_record.get("player"), "wave player metadata")
        guid = _text(player.get("guid"), "wave player guid")
        if guid in records_by_guid:
            raise UpperKaraResponsiveIncantagosCaseV1Error(
                "wave players repeat a player GUID"
            )
        records_by_guid[guid] = player_record
        trace_indices = _array(
            player_record.get("exact_trace_indices"), "exact_trace_indices"
        )
        if trace_indices:
            for value in trace_indices:
                _nonnegative_integer(value, "exact trace index")
            # Reuse the trainer's metadata projection exactly, including its
            # explicit non-warrior SPEC_NOT_AVAILABLE semantics.
            actors.append(deepcopy(_actor_metadata(player_record)))
    return tuple(actors), records_by_guid


def compile_responsive_incantagos_case_v1(
    fixed_case: CompiledResolvedIncantagosDynamicV4CaseV1,
    wave_record: Mapping[str, Any],
    *,
    source_membership_evidence: Mapping[str, Any],
) -> CompiledResponsiveIncantagosCaseV1:
    """Replace fixed LOO damage with a live, prefix-causal team runtime."""

    if not isinstance(fixed_case, CompiledResolvedIncantagosDynamicV4CaseV1):
        raise TypeError(
            "fixed_case must be CompiledResolvedIncantagosDynamicV4CaseV1"
        )
    return _compile_responsive_fixed_case_v1(
        fixed_case,
        wave_record,
        source_membership_evidence=source_membership_evidence,
        focal_guid=_case_focal_player_guid(fixed_case),
    )


def _compile_responsive_fixed_case_v1(
    fixed_case: Any,
    wave_record: Mapping[str, Any],
    *,
    source_membership_evidence: Mapping[str, Any],
    focal_guid: str,
) -> CompiledResponsiveIncantagosCaseV1:
    wave = _mapping(wave_record, "wave_record")
    if (
        wave.get("schema") != TEAM_WAVE_RECORD_SCHEMA
        or wave.get("status") != TEAM_WAVE_STATUS
    ):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "unexpected Stage-5 team-wave schema or status"
        )
    boundaries = _mapping(
        wave.get("scientific_boundaries"), "wave scientific boundaries"
    )
    if (
        boundaries.get("comparison_authorized") is not False
        or boundaries.get("policy_training_authorized") is not False
    ):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "source wave must remain descriptive, nontraining, and noncomparison"
        )

    source = _mapping(fixed_case.receipt.get("source"), "fixed-case source")
    compact_source = _mapping(
        source.get("compact_reduction"), "fixed-case compact reduction source"
    )
    wave_identity = _mapping(wave.get("wave"), "Stage-5 wave identity")
    for field in ("instance_id", "encounter_id", "wave_id"):
        expected = (
            source.get(field) if field != "wave_id" else compact_source.get(field)
        )
        if expected != wave_identity.get(field):
            raise UpperKaraResponsiveIncantagosCaseV1Error(
                f"fixed case and Stage-5 wave differ at {field}"
            )

    actors, records_by_guid = _observed_actors(wave)
    observed_guids = tuple(actor["player_guid"] for actor in actors)
    if focal_guid not in observed_guids:
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "fixed-case focal player has no exact Stage-5 trace rows"
        )
    focal_record = records_by_guid[focal_guid]
    focal_membership = deepcopy(
        dict(_mapping(focal_record.get("component_membership"), "focal membership"))
    )

    membership_evidence = deepcopy(
        dict(_mapping(source_membership_evidence, "source_membership_evidence"))
    )
    if (
        membership_evidence.get("heldout_performance_evidence_eligible")
        is not False
        or membership_evidence.get("comparison_authorized") is not False
    ):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "source membership evidence must remain performance-nonheldout and noncomparison"
        )
    model_training_held_out = membership_evidence.get(
        "model_training_held_out", False
    )
    if not isinstance(model_training_held_out, bool):
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "model_training_held_out must be boolean"
        )
    if model_training_held_out and membership_evidence.get("split") != "VALIDATION":
        raise UpperKaraResponsiveIncantagosCaseV1Error(
            "model-held-out source must belong to the validation component split"
        )

    native_guids, introductions = _native_introductions(fixed_case)
    base = fixed_case.dynamic_config
    responsive_config = DynamicTargetSemanticsConfigV4(
        target_health=base.target_health,
        idle_advance_horizon_ms=base.idle_advance_horizon_ms,
        background_damage_events=(),
        attackability_events=base.attackability_events,
        effective_armor_events=base.effective_armor_events,
        idle_advance_mode=base.idle_advance_mode,
        same_timestamp_order=base.same_timestamp_order,
        retarget_mode=base.retarget_mode,
    )
    health_by_guid = {
        native_guids[row.target_index]: row.current_health
        for row in responsive_config.target_health
    }
    runtime = DynamicTeamRuntimeV1(
        actors=actors,
        target_health_by_guid=health_by_guid,
        target_introduced_at_ms_by_guid=introductions,
    )
    teammate_guids = tuple(guid for guid in observed_guids if guid != focal_guid)
    team_only_sidecar = tuple(deepcopy(dict(row)) for row in fixed_case.team_only_sidecar)

    attackability_mode = fixed_case.receipt.get("attackability_mode")
    counterfactual_attackability = attackability_mode in {
        ALL_THREE_FROM_T0_UNTIL_SIM_DEATH,
        OBSERVED_ONSET_UNTIL_SIM_DEATH,
    }
    if attackability_mode == ALL_THREE_FROM_T0_UNTIL_SIM_DEATH:
        introduction_source = "EXPLICIT_ALL_TARGETS_T0_COUNTERFACTUAL_HYPOTHESIS"
    elif attackability_mode == OBSERVED_ONSET_UNTIL_SIM_DEATH:
        introduction_source = "FIRST_OBSERVED_ACTIVITY_ONSET_WITH_COUNTERFACTUAL_PERSISTENCE"
    else:
        introduction_source = "FIRST_START_OF_COMPILED_DESCRIPTIVE_ACTIVITY_WINDOWS"

    receipt: JSONMap = {
        "schema": SCHEMA,
        "status": STATUS,
        "source": {
            "instance_id": wave_identity.get("instance_id"),
            "encounter_id": wave_identity.get("encounter_id"),
            "wave_id": wave_identity.get("wave_id"),
            "focal_player_guid": focal_guid,
            "source_membership_evidence": membership_evidence,
            "focal_component_membership": focal_membership,
        },
        "fixed_dynamic_config_digest": base.content_sha256,
        "responsive_dynamic_config_digest": responsive_config.content_sha256,
        "fixed_background_event_count_removed": len(base.background_damage_events),
        "responsive_fixed_background_event_count": 0,
        "observed_actor_count": len(actors),
        "responsive_teammate_count": len(teammate_guids),
        "candidate_player_guid": focal_guid,
        "observed_actor_guids": list(observed_guids),
        "responsive_teammate_guids": list(teammate_guids),
        "actor_roster_selection": "NONEMPTY_EXACT_TRACE_INDICES_OUTCOME_DERIVED",
        "native_target_guids": list(native_guids),
        "target_introduced_at_ms_by_guid": dict(introductions),
        "target_introduction_source": introduction_source,
        "team_only_sidecar_count": len(team_only_sidecar),
        "team_only_schedule_reallocated": False,
        "scientific_boundaries": {
            "development_only": True,
            "comparison_authorized": False,
            "policy_training_authorized": False,
            "deployment_authorized": False,
            "heldout_performance_evidence_eligible": False,
            "actor_roster_is_outcome_derived": True,
            "maximum_and_current_health_are_point_hypotheses": True,
            "target_armor_is_hypothesized_not_observed": True,
            "attackability_and_introduction_are_descriptive_outcome_proxies": not counterfactual_attackability,
            "fixed_leave_one_out_damage_schedule_removed": True,
            "responsive_teammate_model_required_for_damage": True,
            "team_only_school_incompatible_targets_omitted_from_runtime": True,
            "team_only_damage_not_reallocated": True,
        },
    }
    if isinstance(fixed_case, CompiledResolvedTrashDynamicV4CaseV1) and counterfactual_attackability:
        receipt["fixed_attackability_mode"] = attackability_mode
        receipt["scientific_boundaries"]["historical_activity_end_closure_removed"] = True
    return CompiledResponsiveIncantagosCaseV1(
        request=deepcopy(fixed_case.request),
        dynamic_config=responsive_config,
        runtime=runtime,
        native_target_guids=native_guids,
        target_introduced_at_ms_by_guid=dict(introductions),
        actors=tuple(deepcopy(actor) for actor in actors),
        candidate_player_guid=focal_guid,
        teammate_player_guids=teammate_guids,
        team_only_sidecar=team_only_sidecar,
        receipt=receipt,
    )


def compile_responsive_trash_case_v1(
    fixed_case: CompiledResolvedTrashDynamicV4CaseV1,
    wave_record: Mapping[str, Any],
    *,
    source_membership_evidence: Mapping[str, Any],
) -> CompiledResponsiveIncantagosCaseV1:
    """Attach responsive teammates to an exact-registry trash wave."""

    if not isinstance(fixed_case, CompiledResolvedTrashDynamicV4CaseV1):
        raise TypeError("fixed_case must be CompiledResolvedTrashDynamicV4CaseV1")
    source = _mapping(fixed_case.receipt.get("source"), "fixed-case source")
    focal_guid = _text(source.get("focal_player_guid"), "fixed-case focal_player_guid")
    return _compile_responsive_fixed_case_v1(
        fixed_case,
        wave_record,
        source_membership_evidence=source_membership_evidence,
        focal_guid=focal_guid,
    )


__all__ = (
    "CompiledResponsiveIncantagosCaseV1",
    "SCHEMA",
    "STATUS",
    "UpperKaraResponsiveIncantagosCaseV1Error",
    "compile_responsive_incantagos_case_v1",
    "compile_responsive_trash_case_v1",
)
