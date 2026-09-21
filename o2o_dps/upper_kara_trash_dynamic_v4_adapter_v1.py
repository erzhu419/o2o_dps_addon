"""Compile one exact three-target trash wave into a development-only v4 case.

The three registry occurrences are ordinary simultaneous targets.  The
contract permits candidate direct/collateral damage but does not assert a
leader's focus order.  Health, armor, activity, and fixed team damage retain
the same explicit hypothesis/proxy status as the compact source model.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2, DynamicEffectiveArmorEventV2
from .sim_bridge_dynamic_v4 import DynamicTargetHealthV4, DynamicTargetSemanticsConfigV4
from .upper_kara_compact_encounter_model_v1 import (
    SCHEMA as COMPACT_SCHEMA,
    STATUS as COMPACT_STATUS,
)
from .upper_kara_resolved_dynamic_v4_adapter_v1 import (
    ResolvedDynamicLoadV4,
    ResolvedDynamicTargetIndexV1,
    _ResolvedDynamicV4ObservationProviderV1,
    _array,
    _comparison_false,
    _compile_attackability_events,
    _compile_background_events,
    _compile_request,
    _integer,
    _mapping,
    _number,
    _optional_positive_int,
    _text,
    _unique_text_list,
)
from .upper_kara_trash_target_contract_v1 import (
    ACTIONABILITY_SCHEMA,
    ACTIONABILITY_STATUS,
)
from .upper_kara_wave_local_search_contract_v1 import ObservedTargetStateV1
from .upper_kara_wave_target_gate_v1 import REQUIRED_RETARGET_MODE_V1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_trash_dynamic_v4_case/v1"
STATUS = "DEVELOPMENT_ONLY_FIXED_LOO_NOT_COMPARISON_AUTHORIZED"
OBSERVED_ACTIVITY_WINDOWS = "OBSERVED_ACTIVITY_WINDOWS"
ALL_THREE_FROM_T0_UNTIL_SIM_DEATH = "ALL_THREE_FROM_T0_UNTIL_SIM_DEATH"
OBSERVED_ONSET_UNTIL_SIM_DEATH = "OBSERVED_ONSET_UNTIL_SIM_DEATH"
ATTACKABILITY_MODES = frozenset({
    OBSERVED_ACTIVITY_WINDOWS,
    ALL_THREE_FROM_T0_UNTIL_SIM_DEATH,
    OBSERVED_ONSET_UNTIL_SIM_DEATH,
})


class UpperKaraTrashDynamicV4AdapterV1Error(ValueError):
    """The exact trash contract and compact model do not compile together."""


@dataclass(frozen=True)
class CompiledResolvedTrashDynamicV4CaseV1:
    request: JSONMap
    dynamic_config: DynamicTargetSemanticsConfigV4
    dynamic_load: ResolvedDynamicLoadV4
    occurrence_index_registry: tuple[ResolvedDynamicTargetIndexV1, ...]
    team_only_sidecar: tuple[JSONMap, ...]
    observation_provider: Callable[
        [Mapping[str, Any]], Mapping[int, ObservedTargetStateV1]
    ]
    receipt: JSONMap


def _counterfactual_attackability(
    observed_windows: tuple[tuple[tuple[int, int], ...], ...],
    *, horizon_ms: int, mode: str,
) -> tuple[tuple[DynamicAttackabilityEventV2, ...], tuple[tuple[tuple[int, int], ...], ...]]:
    """Keep current attackability open; only simulated death can close it."""
    starts = [
        0 if mode == ALL_THREE_FROM_T0_UNTIL_SIM_DEATH else windows[0][0]
        for windows in observed_windows
    ]
    rows: list[tuple[int, int, bool]] = []
    for target_index, start in enumerate(starts):
        rows.append((0, target_index, start == 0))
        if start > 0:
            rows.append((start, target_index, True))
    rows.sort(key=lambda row: (row[0], row[1]))
    return (
        tuple(
            DynamicAttackabilityEventV2(index, time_ms, target_index, attackable)
            for index, (time_ms, target_index, attackable) in enumerate(rows)
        ),
        tuple(((start, horizon_ms),) for start in starts),
    )


def compile_resolved_trash_dynamic_v4_case_v1(
    compact_model: Mapping[str, Any],
    actionability_contract: Mapping[str, Any],
    base_request: Mapping[str, Any],
    *,
    selected_armor_by_occurrence_id: Mapping[str, int | float],
    horizon_ms: int,
    target_level: int,
    attackability_mode: str = OBSERVED_ACTIVITY_WINDOWS,
) -> CompiledResolvedTrashDynamicV4CaseV1:
    """Bind exact target identities to the existing fixed-LOO v4 mechanics."""

    compact = _mapping(compact_model, "compact_model")
    actionability = _mapping(actionability_contract, "actionability_contract")
    if compact.get("schema") != COMPACT_SCHEMA or compact.get("status") != COMPACT_STATUS:
        raise UpperKaraTrashDynamicV4AdapterV1Error("unexpected compact model")
    if (
        actionability.get("schema") != ACTIONABILITY_SCHEMA
        or actionability.get("status") != ACTIONABILITY_STATUS
    ):
        raise UpperKaraTrashDynamicV4AdapterV1Error("unexpected trash actionability contract")
    _comparison_false(compact, "compact_model")
    _comparison_false(actionability, "actionability_contract")
    if actionability.get("comparison_authorized") is not False:
        raise UpperKaraTrashDynamicV4AdapterV1Error("trash comparison is not authorized")
    gate = _mapping(actionability.get("gate"), "actionability gate")
    if (
        gate.get("development_target_actionability_authorized") is not True
        or gate.get("route_priority_resolved") is not False
    ):
        raise UpperKaraTrashDynamicV4AdapterV1Error(
            "trash target actionability must be development-only without route priority"
        )
    horizon = _integer(horizon_ms, "horizon_ms", positive=True)
    level = _integer(target_level, "target_level", positive=True)
    if attackability_mode not in ATTACKABILITY_MODES:
        raise UpperKaraTrashDynamicV4AdapterV1Error("unknown trash attackability mode")
    if not isinstance(selected_armor_by_occurrence_id, Mapping):
        raise UpperKaraTrashDynamicV4AdapterV1Error("selected armors must be a mapping")

    compact_source = _mapping(compact.get("source"), "compact source")
    action_source = _mapping(actionability.get("source"), "actionability source")
    for field in ("instance_id", "encounter_id", "pull_ref"):
        if compact_source.get(field) != action_source.get(field):
            raise UpperKaraTrashDynamicV4AdapterV1Error(
                f"compact/actionability source mismatch at {field}"
            )
    focal_guid = _text(compact.get("focal_player_guid"), "focal_player_guid")
    full_ids = _unique_text_list(
        actionability.get("full_environment_occurrence_ids"),
        "full_environment_occurrence_ids",
    )
    direct_ids = _unique_text_list(
        actionability.get("direct_candidate_occurrence_ids"),
        "direct_candidate_occurrence_ids",
    )
    collateral_ids = _unique_text_list(
        actionability.get("collateral_candidate_occurrence_ids"),
        "collateral_candidate_occurrence_ids",
    )
    if len(full_ids) != 3 or full_ids != direct_ids or full_ids != collateral_ids:
        raise UpperKaraTrashDynamicV4AdapterV1Error(
            "exact three-target trash registry must be wholly direct and collateral"
        )
    compact_rows = [_mapping(row, "compact target") for row in _array(compact.get("targets"), "compact targets")]
    compact_by_id = {
        _text(row.get("occurrence_id"), "compact occurrence_id"): row
        for row in compact_rows
    }
    action_rows = [_mapping(row, "actionability target") for row in _array(actionability.get("targets"), "actionability targets")]
    action_by_id = {
        _text(row.get("resolved_occurrence_id"), "resolved occurrence_id"): row
        for row in action_rows
    }
    if (
        len(compact_by_id) != len(compact_rows)
        or len(action_by_id) != len(action_rows)
        or set(compact_by_id) != set(full_ids)
        or set(action_by_id) != set(full_ids)
    ):
        raise UpperKaraTrashDynamicV4AdapterV1Error("trash occurrence registries differ")
    for occurrence_id in full_ids:
        compact_row = compact_by_id[occurrence_id]
        action_row = action_by_id[occurrence_id]
        if (
            compact_row.get("target_guid") != action_row.get("target_guid")
            or compact_row.get("identity_kind") != action_row.get("identity_resolution")
            or compact_row.get("creature_entry_id")
            != action_row.get("stable_template_creature_entry_id")
            or action_row.get("candidate_actionability")
            != "DIRECT_AND_COLLATERAL_CANDIDATE"
        ):
            raise UpperKaraTrashDynamicV4AdapterV1Error(
                f"trash target identity/actionability differs for {occurrence_id}"
            )
    if set(selected_armor_by_occurrence_id) != set(full_ids):
        raise UpperKaraTrashDynamicV4AdapterV1Error(
            "selected armor keys must equal the exact trash occurrences"
        )

    targets = tuple(compact_by_id[occurrence_id] for occurrence_id in full_ids)
    selected_armors: list[float] = []
    point_health: list[float] = []
    registry: list[ResolvedDynamicTargetIndexV1] = []
    for index, (occurrence_id, target) in enumerate(zip(full_ids, targets)):
        armor_model = _mapping(target.get("armor_model"), "target armor_model")
        if armor_model.get("kind") != "EXPLICIT_SENSITIVITY_GRID":
            raise UpperKaraTrashDynamicV4AdapterV1Error("target armor is not an explicit grid")
        grid = tuple(
            _number(value, "armor hypothesis")
            for value in _array(armor_model.get("base_armor_hypotheses"), "armor grid")
        )
        selected = _number(selected_armor_by_occurrence_id[occurrence_id], "selected armor")
        if selected not in grid:
            raise UpperKaraTrashDynamicV4AdapterV1Error("selected armor is outside the grid")
        hp_model = _mapping(target.get("hp_model"), "target hp_model")
        if hp_model.get("kind") != "OBSERVED_KILL_DAMAGE_BALANCE_POINT_HYPOTHESIS":
            raise UpperKaraTrashDynamicV4AdapterV1Error("target health is not a point hypothesis")
        if _number(hp_model.get("positive_healing_received"), "target healing") != 0:
            raise UpperKaraTrashDynamicV4AdapterV1Error("fixed v4 damage cannot represent target healing")
        health = _number(hp_model.get("point_health"), "target point health", positive=True)
        selected_armors.append(selected)
        point_health.append(health)
        registry.append(
            ResolvedDynamicTargetIndexV1(
                target_index=index,
                occurrence_id=occurrence_id,
                target_guid=_text(target.get("target_guid"), "target_guid"),
                identity_kind=_text(target.get("identity_kind"), "identity_kind"),
                creature_entry_id=_optional_positive_int(
                    target.get("creature_entry_id"), "creature_entry_id"
                ),
            )
        )

    observed_attackability_events, observed_windows = _compile_attackability_events(
        targets, horizon_ms=horizon
    )
    if attackability_mode == OBSERVED_ACTIVITY_WINDOWS:
        attackability_events, windows = observed_attackability_events, observed_windows
    else:
        attackability_events, windows = _counterfactual_attackability(
            observed_windows, horizon_ms=horizon, mode=attackability_mode
        )
    background_events, background_receipts = _compile_background_events(
        targets, activity_windows=observed_windows, horizon_ms=horizon
    )
    config = DynamicTargetSemanticsConfigV4(
        target_health=tuple(
            DynamicTargetHealthV4(index, health, health)
            for index, health in enumerate(point_health)
        ),
        idle_advance_horizon_ms=horizon,
        background_damage_events=background_events,
        attackability_events=attackability_events,
        effective_armor_events=tuple(
            DynamicEffectiveArmorEventV2(index, 0, index, armor)
            for index, armor in enumerate(selected_armors)
        ),
        retarget_mode=REQUIRED_RETARGET_MODE_V1,
    )
    request = _compile_request(
        base_request,
        targets,
        selected_armors=selected_armors,
        point_health=point_health,
        horizon_ms=horizon,
        target_level=level,
    )
    receipt_source = deepcopy(dict(compact_source))
    receipt_source["focal_player_guid"] = focal_guid
    receipt: JSONMap = {
        "schema": SCHEMA,
        "status": STATUS,
        "source": receipt_source,
        "focal_player_guid": focal_guid,
        "full_encounter_start_offset_ms": 0,
        "horizon_ms": horizon,
        "target_level": level,
        "dynamic_config_digest": config.content_sha256,
        "native_target_count": 3,
        "native_occurrence_ids": list(full_ids),
        "occurrence_index_registry": [row.to_dict() for row in registry],
        "direct_target_indexes": [0, 1, 2],
        "collateral_target_indexes": [0, 1, 2],
        "route_priority_resolved": False,
        "team_only_occurrence_ids": [],
        "team_only_schedule_reallocated": False,
        "selected_armor_by_occurrence_id": {
            occurrence_id: selected_armors[index]
            for index, occurrence_id in enumerate(full_ids)
        },
        "activity_windows_inclusive_ms": [
            {
                "target_index": index,
                "occurrence_id": full_ids[index],
                "windows": [list(window) for window in target_windows],
            }
            for index, target_windows in enumerate(windows)
        ],
        "fixed_leave_one_out_schedule": {
            "bin_total_timestamp_proxy": "LATEST_MILLISECOND_INTERSECTING_ACTIVITY_WINDOW",
            "final_bin_placement_clipped_to_horizon_without_clipping_damage": True,
            "target_receipts": background_receipts,
            "event_count": len(background_events),
            "responsive_to_candidate_policy": False,
        },
        "scientific_boundaries": {
            "development_only": True,
            "comparison_authorized": False,
            "maximum_and_current_health_equal_point_hypothesis": True,
            "full_encounter_checkpoint_only": True,
            "target_armor_observed": False,
            "attackability_is_descriptive_outcome_proxy": attackability_mode == OBSERVED_ACTIVITY_WINDOWS,
            "policy_observes_only_current_attackability_and_death": True,
            "fixed_team_schedule_is_not_candidate_responsive": True,
            "route_priority_observed": False,
        },
    }
    if attackability_mode != OBSERVED_ACTIVITY_WINDOWS:
        receipt["attackability_mode"] = attackability_mode
        receipt["observed_activity_windows_inclusive_ms"] = [
            {
                "target_index": index,
                "occurrence_id": full_ids[index],
                "windows": [list(window) for window in target_windows],
            }
            for index, target_windows in enumerate(observed_windows)
        ]
        receipt["fixed_leave_one_out_schedule"]["bin_total_timestamp_proxy"] = (
            "LATEST_MILLISECOND_INTERSECTING_OBSERVED_ACTIVITY_WINDOW"
        )
        receipt["scientific_boundaries"].update({
            "attackability_is_explicit_counterfactual_hypothesis": True,
            "observed_activity_end_closes_simulator_attackability": False,
            "observed_onset_used_as_hypothesis": attackability_mode == OBSERVED_ONSET_UNTIL_SIM_DEATH,
        })
    return CompiledResolvedTrashDynamicV4CaseV1(
        request=request,
        dynamic_config=config,
        dynamic_load=ResolvedDynamicLoadV4(config),
        occurrence_index_registry=tuple(registry),
        team_only_sidecar=(),
        observation_provider=_ResolvedDynamicV4ObservationProviderV1(config),
        receipt=receipt,
    )


__all__ = (
    "ALL_THREE_FROM_T0_UNTIL_SIM_DEATH",
    "OBSERVED_ACTIVITY_WINDOWS",
    "OBSERVED_ONSET_UNTIL_SIM_DEATH",
    "CompiledResolvedTrashDynamicV4CaseV1",
    "SCHEMA",
    "STATUS",
    "UpperKaraTrashDynamicV4AdapterV1Error",
    "compile_resolved_trash_dynamic_v4_case_v1",
)
