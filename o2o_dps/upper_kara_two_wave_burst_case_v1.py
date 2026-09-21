"""Continuous two-wave development cases with searchable Turtle burst actions.

Both waves execute in one native simulator environment.  The second target
becomes attackable only after the inter-wave gap, so rage, cooldowns, auras,
consumable state, weapons, and proc state are carried by the simulator rather
than reconstructed from independent wave summaries.  Contra contributes the
burst inventory; it does not contribute execution conditions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from typing import Any, Mapping

from .contra_turtle_burst_loadout_v1 import (
    ContraTurtleBurstLoadoutV1,
    build_contra_turtle_burst_loadouts_v1,
)
from .development_precombat_wave_case_v1 import (
    DevelopmentPrecombatWaveCaseV1,
    wrap_development_wave_case_with_burst_precombat_v1,
)
from .development_two_wave_build_panel_v1 import (
    BUILD_IDS,
    build_two_wave_build_case_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .sim_bridge import BackgroundDamageEventV1
from .sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from .wave_action_schedule_v1 import SearchCellIdentity
from .wave_action_sequence_pilot_v1 import exact_build_identity_v1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_continuous_two_wave_burst_case/v1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _selected_loadout_v1(
    request: Mapping[str, Any], loadout_id: str
) -> ContraTurtleBurstLoadoutV1:
    rows = {
        row.loadout_id: row
        for row in build_contra_turtle_burst_loadouts_v1(request)
    }
    try:
        return rows[loadout_id]
    except KeyError as error:
        raise ValueError(f"unknown Turtle burst loadout {loadout_id!r}") from error


def build_upper_kara_continuous_two_wave_burst_case_v1(
    seed: int,
    *,
    build_id: str,
    loadout_id: str,
    pull_time_ms: int = 3_000,
    first_wave_arrival_ms: int = 0,
) -> DevelopmentPrecombatWaveCaseV1:
    """Bind one burst inventory to the existing native two-wave environment."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    if (
        isinstance(first_wave_arrival_ms, bool)
        or not isinstance(first_wave_arrival_ms, int)
        or not 0 <= first_wave_arrival_ms < 10_000
    ):
        raise ValueError(
            "first_wave_arrival_ms must be an integer in [0, 10000)"
        )
    base, _ = build_two_wave_build_case_v1(seed, build_id)
    loadout = _selected_loadout_v1(base.request, loadout_id)
    wrapped = wrap_development_wave_case_with_burst_precombat_v1(
        base,
        self_actions=loadout.precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=loadout.player_consumes,
    )
    if first_wave_arrival_ms:
        # The dynamic target flag is the player's current reachability, while
        # background damage continues to represent teammates already on the
        # pack.  Keep both processes in one native load so the HP observed on
        # arrival is caused by the same earlier timeline, not reconstructed.
        arrival_at_ms = pull_time_ms + first_wave_arrival_ms
        source = wrapped.dynamic_load.config
        rows = [
            event
            for event in source.attackability_events
            if not (
                event.target_index == 0
                and event.time_ms == pull_time_ms
                and event.attackable
            )
        ]
        rows.append(
            DynamicAttackabilityEventV2(
                schedule_index=0,
                time_ms=arrival_at_ms,
                target_index=0,
                attackable=True,
            )
        )
        rows.sort(
            key=lambda event: (
                event.time_ms,
                event.target_index,
                event.attackable,
            )
        )
        prior_team_damage = [
            event
            for event in source.background_damage_events
            if event.target_index == 0 and event.time_ms <= arrival_at_ms
        ]
        remaining_team_damage = [
            event
            for event in source.background_damage_events
            if event not in prior_team_damage
        ]
        if prior_team_damage:
            remaining_team_damage.append(
                BackgroundDamageEventV1(
                    schedule_index=0,
                    time_ms=arrival_at_ms,
                    target_index=0,
                    event_id="model-wave-1-team-catchup-at-player-arrival",
                    damage=sum(event.damage for event in prior_team_damage),
                )
            )
        remaining_team_damage.sort(
            key=lambda event: (
                event.time_ms,
                event.target_index,
                event.event_id,
            )
        )
        config = replace(
            source,
            background_damage_events=tuple(
                BackgroundDamageEventV1(
                    schedule_index=index,
                    time_ms=event.time_ms,
                    target_index=event.target_index,
                    event_id=event.event_id,
                    damage=event.damage,
                )
                for index, event in enumerate(remaining_team_damage)
            ),
            attackability_events=tuple(
                DynamicAttackabilityEventV2(
                    schedule_index=index,
                    time_ms=event.time_ms,
                    target_index=event.target_index,
                    attackable=event.attackable,
                )
                for index, event in enumerate(rows)
            ),
        )
        load = DynamicRolloutLoadV3.bind(
            wrapped.request,
            wrapped.dynamic_load.seed,
            config,
        )
        spec = deepcopy(wrapped.case_spec)
        spec["request_sha256"] = load.request_sha256
        spec["dynamic_load_contract_sha256"] = load.contract_sha256
        spec["initial_state"]["target_0_attackable_at_pull"] = False
        spec["two_wave_model"]["target_0_attackable_from_ms"] = (
            first_wave_arrival_ms
        )
        spec["two_wave_model"]["movement_during_first_wave"] = (
            "PLAYER_REACHABILITY_EVENT_WITH_TEAM_DAMAGE_CATCHUP"
        )
        wrapped = replace(wrapped, case_spec=spec, dynamic_load=load)
    wrapped.case_spec["schema"] = SCHEMA
    wrapped.case_spec["continuous_route"] = {
        "wave_count": 2,
        "single_native_environment": True,
        "independent_wave_reset": False,
        "state_carried": [
            "rage",
            "cooldowns",
            "auras",
            "consumable_inventory",
            "equipment",
            "proc_state",
        ],
        "search_decides_burst_wave_timing_target_and_order": True,
        "contra_manual_trigger_enforced": False,
        "contra_boss_hp_threshold_enforced": False,
        "player_arrival": {
            "first_wave_in_range_at_ms_relative_to_pull": (
                first_wave_arrival_ms
            ),
            "team_damage_continues_before_player_arrival": True,
            "team_damage_before_arrival_representation": (
                "NATIVE_AGGREGATE_CATCHUP_EVENT_AT_FIRST_OBSERVABLE_ARRIVAL"
            ),
            "decision_observations": [
                "current_target_attackable",
                "current_target_hp_percent",
                "current_action_ready",
            ],
            "future_target_death_time_visible_to_policy": False,
        },
    }
    wrapped.case_spec["burst_loadout"] = loadout.to_dict()
    return wrapped


def search_cell_from_continuous_two_wave_case_v1(
    case: DevelopmentPrecombatWaveCaseV1,
) -> SearchCellIdentity:
    """Derive identity from the fully materialized request and route config."""

    if not isinstance(case, DevelopmentPrecombatWaveCaseV1):
        raise TypeError("case must be DevelopmentPrecombatWaveCaseV1")
    if case.case_spec.get("schema") != SCHEMA:
        raise ValueError("case is not a continuous two-wave burst case")
    player = case.request["raid"]["parties"][0]["players"][0]
    equipment = tuple(
        (f"slot_{index:02d}", int(item["id"]))
        for index, item in enumerate(
            player.get("equipment", {}).get("items", [])
        )
        if isinstance(item, Mapping) and int(item.get("id", 0)) > 0
    )
    talents_string = player.get("talentsString")
    if not isinstance(talents_string, str) or not talents_string:
        raise ValueError("case player lacks talentsString")
    talents = tuple(
        (f"tree_{tree_index}_position_{position:02d}", int(rank))
        for tree_index, tree in enumerate(
            talents_string.split("-"), start=1
        )
        for position, rank in enumerate(tree, start=1)
    )
    loadout = case.case_spec.get("burst_loadout")
    if not isinstance(loadout, Mapping):
        raise ValueError("case lacks burst_loadout")
    loadout_id = loadout.get("loadout_id")
    if not isinstance(loadout_id, str) or not loadout_id:
        raise ValueError("case has invalid burst loadout identity")
    build_id = case.case_spec.get("build_id")
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("case lacks build_id")
    dynamic_wire = case.dynamic_load.config.to_wire()
    target_health = [
        {"target_index": row.target_index, "health": row.health}
        for row in case.dynamic_load.config.target_health
    ]
    return SearchCellIdentity(
        scenario_id="Upper Tower of Karazhan:continuous-two-wave-development",
        wave_or_boss_id=str(case.case_spec["source_wave_ref"]) + ":repeat",
        exact_build_id=_canonical_json({
            "build_id": build_id,
            "build_ref": case.case_spec.get("build_ref"),
            "build": exact_build_identity_v1(case),
            "loadout_id": loadout_id,
        }),
        talents=talents,
        equipment=equipment,
        derived_mechanics=(
            ("starting_rage", case.case_spec["initial_state"]["rage"]),
            ("target_count", len(target_health)),
            ("target_health_json", _canonical_json(target_health)),
            (
                "dynamic_route_contract_json",
                _canonical_json(dynamic_wire),
            ),
            (
                "exact_build_context_json",
                _canonical_json(exact_build_identity_v1(case)),
            ),
        ),
        environment_branch_id=_canonical_json({
            "kind": "NATIVE_CONTINUOUS_TWO_WAVE",
            "team_background": deepcopy(case.case_spec["team_background"]),
            "two_wave_model": deepcopy(case.case_spec["two_wave_model"]),
            "precombat": deepcopy(case.case_spec["precombat"]),
            "loadout_id": loadout_id,
        }),
    )


__all__ = (
    "SCHEMA",
    "build_upper_kara_continuous_two_wave_burst_case_v1",
    "search_cell_from_continuous_two_wave_case_v1",
)
