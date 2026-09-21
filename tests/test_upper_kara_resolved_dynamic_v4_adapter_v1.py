from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.sim_bridge_dynamic_v4 import DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4
from o2o_dps.upper_kara_compact_encounter_model_v1 import (
    SCHEMA as COMPACT_SCHEMA,
    STATUS as COMPACT_STATUS,
)
from o2o_dps.upper_kara_incantagos_actionability_contract_v1 import (
    BOSS,
    COLLATERAL_ONLY_CANDIDATE,
    COMBAT_PRIORITY_ADD,
    DIRECT_AND_COLLATERAL_CANDIDATE,
    OPTIONAL_ACTIONABLE_ADD,
    SCHEMA as ACTIONABILITY_SCHEMA,
    STATUS as ACTIONABILITY_STATUS,
    TEAM_ONLY_SCHOOL_INCOMPATIBLE,
)
from o2o_dps.upper_kara_resolved_dynamic_v4_adapter_v1 import (
    UpperKaraResolvedDynamicV4AdapterV1Error,
    compile_resolved_incantagos_dynamic_v4_case_v1,
)
from o2o_dps.upper_kara_wave_target_gate_v1 import (
    REACTIVE_ADDS_STAGE_ID_V1,
    REACTIVE_BOSS_STAGE_ID_V1,
    REQUIRED_RETARGET_MODE_V1,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    NativeDynamicV4ScheduleReplayV1,
    ReplayStatusV1,
)


SOURCE = {
    "instance_id": "instance-1",
    "encounter_id": "encounter-1",
    "pull_ref": "instance-1:encounter-1",
}


def _target(
    occurrence_id: str,
    *,
    guid: str,
    identity: str,
    entry: int | None,
    health: int,
    focal_damage: int,
    loo_damage: int,
    window: tuple[int, int],
    bin_window: tuple[int, int],
    healing: int = 0,
) -> dict:
    total = focal_damage + loo_damage
    return {
        "occurrence_id": occurrence_id,
        "target_guid": guid,
        "identity_kind": identity,
        "creature_entry_id": entry,
        "hp_model_ref": f"model/{occurrence_id}/hp",
        "armor_model_ref": f"model/{occurrence_id}/armor",
        "team_kill_clock_ref": f"model/{occurrence_id}/team",
        "attackability_ref": f"model/{occurrence_id}/attackability",
        "hp_model": {
            "kind": "OBSERVED_KILL_DAMAGE_BALANCE_POINT_HYPOTHESIS",
            "point_health": health,
            "overkill_adjusted_positive_damage": health + healing,
            "positive_healing_received": healing,
            "observed_max_health": None,
            "status": "DEVELOPMENT_HYPOTHESIS",
            "post_first_death_activity": {},
        },
        "armor_model": {
            "kind": "EXPLICIT_SENSITIVITY_GRID",
            "base_armor_hypotheses": [0, 1721],
            "chronicle_observed_armor": None,
        },
        "team_kill_clock_model": {
            "kind": "EXACT_ATTEMPT_BINNED_FOCAL_LEAVE_ONE_OUT",
            "focal_player_guid": "player-1",
            "all_positive_damage": total,
            "focal_positive_damage_removed": focal_damage,
            "leave_one_out_positive_damage": loo_damage,
            "teammate_positive_damage_by_player_guid": {},
            "residual_positive_damage_without_exact_player_guid": loo_damage,
            "explicit_unattributed_positive_damage": 0,
            "damage_bins": [
                {
                    "start_offset_ms": bin_window[0],
                    "end_offset_ms_exclusive": bin_window[1],
                    "all_positive_damage": total,
                    "focal_positive_damage_removed": focal_damage,
                    "leave_one_out_positive_damage": loo_damage,
                    "residual_positive_damage_without_exact_player_guid": loo_damage,
                    "teammate_positive_damage_by_player_guid": {},
                }
            ],
            "death_offset_ms": window[1],
            "death_offset_source": "FIXTURE",
            "death_offset_is_descriptive_outcome": True,
            "responsive_to_candidate_policy": False,
        },
        "attackability_model": {
            "kind": "FIXTURE_PROXY",
            "activity_windows": [
                {
                    "start_offset_ms": window[0],
                    "end_offset_ms": window[1],
                }
            ],
            "exact_attackability": None,
            "descriptive_outcome_proxy": True,
            "policy_visible_future_schedule": False,
        },
    }


def _fixtures() -> tuple[dict, dict, dict, dict[str, int]]:
    # Deliberately not boss-first: native compilation must reorder exactly once.
    targets = [
        _target(
            "whelp",
            guid="0xF140WHELP",
            identity="BOSS_OWNED_SUMMON_OCCURRENCE",
            entry=None,
            health=30,
            focal_damage=0,
            loo_damage=12,
            window=(40, 100),
            bin_window=(40, 80),
        ),
        _target(
            "priority",
            guid="0xF130PRIORITY",
            identity="REGISTRY_CREATURE_OCCURRENCE",
            entry=59989,
            health=100,
            focal_damage=20,
            loo_damage=25,
            window=(20, 80),
            bin_window=(0, 25),
        ),
        _target(
            "optional",
            guid="0xF130AFFINITYPHYSICAL",
            identity="EXACT_F130_CREATURE_OCCURRENCE",
            entry=59987,
            health=50,
            focal_damage=3,
            loo_damage=7,
            window=(60, 120),
            bin_window=(60, 90),
        ),
        _target(
            "team-only",
            guid="0xF130AFFINITYARCANE",
            identity="EXACT_F130_CREATURE_OCCURRENCE",
            entry=59982,
            health=40,
            focal_damage=0,
            loo_damage=40,
            window=(30, 90),
            bin_window=(30, 60),
        ),
        _target(
            "boss",
            guid="0xF130BOSS",
            identity="REGISTRY_CREATURE_OCCURRENCE",
            entry=61946,
            health=1000,
            focal_damage=100,
            loo_damage=60,
            window=(0, 150),
            bin_window=(0, 50),
        ),
    ]
    compact = {
        "schema": COMPACT_SCHEMA,
        "status": COMPACT_STATUS,
        "model_id": "model-1",
        "source": dict(SOURCE),
        "focal_player_guid": "player-1",
        "focal_player_source_evidence": {},
        "targets": targets,
        "scientific_boundaries": {"comparison_authorized": False},
    }
    roles = {
        "whelp": (COLLATERAL_ONLY_CANDIDATE, "INCANTAGOS_WHELP", None),
        "priority": (
            DIRECT_AND_COLLATERAL_CANDIDATE,
            COMBAT_PRIORITY_ADD,
            59989,
        ),
        "optional": (
            DIRECT_AND_COLLATERAL_CANDIDATE,
            OPTIONAL_ACTIONABLE_ADD,
            59987,
        ),
        "team-only": (
            TEAM_ONLY_SCHOOL_INCOMPATIBLE,
            "AFFINITY_ADD",
            59982,
        ),
        "boss": (DIRECT_AND_COLLATERAL_CANDIDATE, BOSS, 61946),
    }
    action_targets = []
    for target in targets:
        actionability, mechanic, stable_entry = roles[target["occurrence_id"]]
        action_targets.append(
            {
                "target_guid": target["target_guid"],
                "resolved_occurrence_id": target["occurrence_id"],
                "identity_resolution": target["identity_kind"],
                "eligible_encounter_hostile": True,
                "stable_template_creature_entry_id": stable_entry,
                "mechanic_classification": mechanic,
                "target_gate_role": mechanic,
                "candidate_actionability": actionability,
            }
        )
    actionability = {
        "schema": ACTIONABILITY_SCHEMA,
        "status": ACTIONABILITY_STATUS,
        "source": dict(SOURCE),
        "candidate_damage_schools": ["PHYSICAL"],
        "boss_occurrence_id": "boss",
        "full_environment_occurrence_ids": [
            "whelp",
            "priority",
            "optional",
            "team-only",
            "boss",
        ],
        "direct_candidate_occurrence_ids": ["priority", "optional", "boss"],
        "collateral_candidate_occurrence_ids": [
            "whelp",
            "priority",
            "optional",
            "boss",
        ],
        "combat_priority_add_occurrence_ids": ["priority"],
        "collateral_only_occurrence_ids": ["whelp"],
        "optional_actionable_occurrence_ids": ["optional"],
        "team_only_occurrence_ids": ["team-only"],
        "excluded_player_owned_occurrence_ids": [],
        "targets": action_targets,
        "gate": {"development_target_actionability_authorized": True},
        "comparison_authorized": False,
        "scientific_boundaries": {"comparison_authorized": False},
    }
    base_request = {
        "raid": {},
        "encounter": {
            "duration": 1,
            "durationVariation": 1,
            "useHealth": False,
            "targets": [
                {
                    "id": 999,
                    "name": "template",
                    "level": 60,
                    "mobType": "MobTypeUnknown",
                    "stats": [7] * 35,
                }
            ],
        },
        "simOptions": {"iterations": 1},
    }
    armors = {target: 1721 for target in ("boss", "whelp", "priority", "optional")}
    return compact, actionability, base_request, armors


def _compile():
    compact, actionability, request, armors = _fixtures()
    return compile_resolved_incantagos_dynamic_v4_case_v1(
        compact,
        actionability,
        request,
        selected_armor_by_occurrence_id=armors,
        horizon_ms=150,
        target_level=63,
    )


def _state(case, *, attackable: tuple[bool, ...], dead: tuple[bool, ...] | None = None):
    count = len(case.occurrence_index_registry)
    dead = dead or (False,) * count
    currents = tuple(
        0.0 if dead[index] else row.current_health
        for index, row in enumerate(case.dynamic_config.target_health)
    )
    generation = 7
    return {
        "time_ms": 0,
        "finished": False,
        "needs_input": True,
        "target_index": 0,
        "total_target_count": count,
        "num_targets": sum(
            value and not dead[index] for index, value in enumerate(attackable)
        ),
        "dynamic_team_background": {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            "config_digest": case.dynamic_config.content_sha256,
            "environment_generation": generation,
            "targets": [
                {
                    "target_index": index,
                    "initial_health": row.current_health,
                    "current_health": currents[index],
                    "dead": dead[index],
                }
                for index, row in enumerate(case.dynamic_config.target_health)
            ],
        },
        "dynamic_target_semantics": {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            "config_digest": case.dynamic_config.content_sha256,
            "environment_generation": generation,
            "targets": [
                {
                    "target_index": index,
                    "attackable": attackable[index] and not dead[index],
                    "effective_armor": 1721.0,
                    "maximum_health": row.maximum_health,
                    "current_health": currents[index],
                    "dead": dead[index],
                }
                for index, row in enumerate(case.dynamic_config.target_health)
            ],
        },
    }


def test_compile_orders_native_targets_and_keeps_team_only_sidecar() -> None:
    case = _compile()

    assert [row.occurrence_id for row in case.occurrence_index_registry] == [
        "boss",
        "whelp",
        "priority",
        "optional",
    ]
    assert case.boss_index == 0
    assert case.priority_add_indexes == (2,)
    assert case.collateral_only_indexes == (1,)
    assert case.optional_actionable_indexes == (3,)
    assert case.dynamic_load.config is case.dynamic_config
    assert case.dynamic_config.retarget_mode == REQUIRED_RETARGET_MODE_V1
    assert len(case.request["encounter"]["targets"]) == 4
    assert case.request["encounter"]["duration"] == 0.15
    assert case.request["encounter"]["durationVariation"] == 0
    assert case.request["encounter"]["useHealth"] is True
    assert [row.maximum_health for row in case.dynamic_config.target_health] == [
        1000.0,
        30.0,
        100.0,
        50.0,
    ]
    assert [row.current_health for row in case.dynamic_config.target_health] == [
        1000.0,
        30.0,
        100.0,
        50.0,
    ]
    assert len(case.team_only_sidecar) == 1
    assert case.team_only_sidecar[0]["occurrence_id"] == "team-only"
    assert case.team_only_sidecar[0]["schedule_reallocated_to_native_targets"] is False
    assert case.receipt["scientific_boundaries"]["comparison_authorized"] is False


def test_compile_preserves_bin_totals_and_uses_latest_activity_intersection() -> None:
    case = _compile()
    events = case.dynamic_config.background_damage_events

    assert [(row.time_ms, row.target_index, row.damage) for row in events] == [
        (24, 2, 25.0),
        (49, 0, 60.0),
        (79, 1, 12.0),
        (89, 3, 7.0),
    ]
    assert sum(row.damage for row in events) == 104.0
    assert [row.effective_armor for row in case.dynamic_config.effective_armor_events] == [
        1721.0,
        1721.0,
        1721.0,
        1721.0,
    ]
    assert all(row.time_ms == 0 for row in case.dynamic_config.effective_armor_events)


def test_final_fixed_width_bin_is_clipped_for_placement_not_damage() -> None:
    compact, actionability, request, armors = _fixtures()
    boss = next(row for row in compact["targets"] if row["occurrence_id"] == "boss")
    boss["team_kill_clock_model"]["damage_bins"][0].update(
        start_offset_ms=140,
        end_offset_ms_exclusive=200,
    )
    case = compile_resolved_incantagos_dynamic_v4_case_v1(
        compact,
        actionability,
        request,
        selected_armor_by_occurrence_id=armors,
        horizon_ms=150,
        target_level=63,
    )

    boss_event = next(
        row
        for row in case.dynamic_config.background_damage_events
        if row.target_index == case.boss_index
    )
    assert boss_event.time_ms == 150
    assert boss_event.damage == 60.0


def test_observation_provider_and_gate_keep_roles_separate() -> None:
    case = _compile()
    # Boss, whelp and optional affinity are live; the priority add has not spawned.
    boss_state = _state(case, attackable=(True, True, False, True))
    decision = case.target_gate.evaluate_state(boss_state)
    assert decision.stage_id == REACTIVE_BOSS_STAGE_ID_V1
    assert decision.direct_target_indexes == (0, 3)
    # The not-yet-visible priority add remains in the conservative collateral
    # set for delayed next-swing/AOE validation, but is not a direct target.
    assert decision.collateral_target_indexes == (0, 2, 3, 1)

    # Once the priority add appears it is the only direct focus choice.  Boss,
    # optional affinity and whelp all remain legal collateral.
    adds_state = _state(case, attackable=(True, True, True, True))
    decision = case.target_gate.evaluate_state(adds_state)
    assert decision.stage_id == REACTIVE_ADDS_STAGE_ID_V1
    assert decision.direct_target_indexes == (2,)
    assert set(decision.collateral_target_indexes) == {0, 1, 2, 3}
    case.target_gate.validate_case(case)


def test_compiled_case_loads_through_native_v4_replay() -> None:
    case = _compile()
    state = _state(case, attackable=(True, True, False, True))
    calls = []

    class Bridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def load_dynamic_v4(self, request, seed, config):
            calls.append((request, seed, config))
            return SimpleNamespace(state=state)

        def actions(self):
            return (
                AvailableAction(0, ActionRef(spell_id=23881), "Bloodthirst", True, 0, True),
            )

    replay = NativeDynamicV4ScheduleReplayV1(
        Bridge,
        lambda seed: case,
        target_gate=case.target_gate,
    )
    outcome = replay.replay(19, ())

    assert outcome.status is ReplayStatusV1.FRONTIER
    assert calls == [(case.request, 19, case.dynamic_config)]


@pytest.mark.parametrize("defect", ["armor", "healing", "team-only-focal", "bin-window"])
def test_compile_fails_closed_on_unsupported_native_evidence(defect: str) -> None:
    compact, actionability, request, armors = _fixtures()
    if defect == "armor":
        armors.pop("optional")
    elif defect == "healing":
        compact["targets"][1]["hp_model"]["positive_healing_received"] = 1
    elif defect == "team-only-focal":
        compact["targets"][3]["team_kill_clock_model"][
            "focal_positive_damage_removed"
        ] = 1
    else:
        compact["targets"][1]["team_kill_clock_model"]["damage_bins"][0].update(
            start_offset_ms=0,
            end_offset_ms_exclusive=10,
        )

    with pytest.raises(UpperKaraResolvedDynamicV4AdapterV1Error):
        compile_resolved_incantagos_dynamic_v4_case_v1(
            compact,
            actionability,
            request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=150,
            target_level=63,
        )


def test_observation_provider_rejects_cross_block_drift() -> None:
    case = _compile()
    state = _state(case, attackable=(True, True, False, True))
    state["dynamic_target_semantics"]["environment_generation"] = 8
    with pytest.raises(
        UpperKaraResolvedDynamicV4AdapterV1Error,
        match="different environment generations",
    ):
        case.observation_provider(state)

    state = _state(case, attackable=(True, True, False, True))
    state["dynamic_target_semantics"]["targets"][0]["current_health"] -= 1
    with pytest.raises(
        UpperKaraResolvedDynamicV4AdapterV1Error,
        match="lifecycle and semantics",
    ):
        case.observation_provider(state)


def test_comparison_authority_cannot_be_widened() -> None:
    compact, actionability, request, armors = _fixtures()
    actionability["scientific_boundaries"]["comparison_authorized"] = True
    with pytest.raises(
        UpperKaraResolvedDynamicV4AdapterV1Error,
        match="comparison-ineligible",
    ):
        compile_resolved_incantagos_dynamic_v4_case_v1(
            compact,
            actionability,
            request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=150,
            target_level=63,
        )
