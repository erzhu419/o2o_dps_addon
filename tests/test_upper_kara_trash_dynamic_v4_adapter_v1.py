from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.upper_kara_compact_encounter_model_v1 import (
    SCHEMA as COMPACT_SCHEMA,
    STATUS as COMPACT_STATUS,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ALL_THREE_FROM_T0_UNTIL_SIM_DEATH,
    OBSERVED_ONSET_UNTIL_SIM_DEATH,
    UpperKaraTrashDynamicV4AdapterV1Error,
    compile_resolved_trash_dynamic_v4_case_v1,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import _native_introductions
from o2o_dps.upper_kara_trash_target_contract_v1 import (
    ACTIONABILITY_SCHEMA,
    ACTIONABILITY_STATUS,
)


SOURCE = {"instance_id": "raid-1", "encounter_id": "trash-1", "pull_ref": "raid-1:trash-1"}


def _inputs():
    targets = []
    action_rows = []
    for index in range(3):
        occurrence = f"trash-{index}"
        guid = f"0xF13000000000000{index}"
        entry = 62016 + index
        targets.append(
            {
                "occurrence_id": occurrence,
                "target_guid": guid,
                "identity_kind": "REGISTRY_CREATURE_OCCURRENCE",
                "creature_entry_id": entry,
                "hp_model": {
                    "kind": "OBSERVED_KILL_DAMAGE_BALANCE_POINT_HYPOTHESIS",
                    "point_health": 100 + index,
                    "positive_healing_received": 0,
                    "observed_max_health": None,
                },
                "armor_model": {
                    "kind": "EXPLICIT_SENSITIVITY_GRID",
                    "base_armor_hypotheses": [0, 1721],
                    "chronicle_observed_armor": None,
                },
                "attackability_model": {
                    "activity_windows": [
                        {"start_offset_ms": index * 10, "end_offset_ms": 100}
                    ],
                    "descriptive_outcome_proxy": True,
                },
                "team_kill_clock_model": {
                    "kind": "EXACT_ATTEMPT_BINNED_FOCAL_LEAVE_ONE_OUT",
                    "focal_player_guid": "player-fury",
                    "focal_positive_damage_removed": 5,
                    "leave_one_out_positive_damage": 25,
                    "damage_bins": [
                        {
                            "start_offset_ms": index * 10,
                            "end_offset_ms_exclusive": 50,
                            "leave_one_out_positive_damage": 25,
                        }
                    ],
                },
            }
        )
        action_rows.append(
            {
                "resolved_occurrence_id": occurrence,
                "target_guid": guid,
                "identity_resolution": "REGISTRY_CREATURE_OCCURRENCE",
                "stable_template_creature_entry_id": entry,
                "candidate_actionability": "DIRECT_AND_COLLATERAL_CANDIDATE",
            }
        )
    ids = [target["occurrence_id"] for target in targets]
    compact = {
        "schema": COMPACT_SCHEMA,
        "status": COMPACT_STATUS,
        "source": dict(SOURCE),
        "focal_player_guid": "player-fury",
        "targets": targets,
        "scientific_boundaries": {"comparison_authorized": False},
    }
    actionability = {
        "schema": ACTIONABILITY_SCHEMA,
        "status": ACTIONABILITY_STATUS,
        "source": dict(SOURCE),
        "full_environment_occurrence_ids": list(ids),
        "direct_candidate_occurrence_ids": list(ids),
        "collateral_candidate_occurrence_ids": list(ids),
        "targets": action_rows,
        "gate": {
            "development_target_actionability_authorized": True,
            "route_priority_resolved": False,
        },
        "comparison_authorized": False,
        "scientific_boundaries": {"comparison_authorized": False},
    }
    request = {
        "raid": {},
        "encounter": {
            "duration": 1,
            "targets": [{"id": 999, "name": "template", "level": 60, "stats": [0] * 35}],
        },
        "simOptions": {"iterations": 1},
    }
    return compact, actionability, request, {value: 1721 for value in ids}


def _compile():
    compact, actionability, request, armors = _inputs()
    return compile_resolved_trash_dynamic_v4_case_v1(
        compact,
        actionability,
        request,
        selected_armor_by_occurrence_id=armors,
        horizon_ms=100,
        target_level=63,
    )


def test_compiles_three_ordinary_targets_without_boss_framing() -> None:
    case = _compile()
    assert [row.occurrence_id for row in case.occurrence_index_registry] == [
        "trash-0", "trash-1", "trash-2"
    ]
    assert len(case.request["encounter"]["targets"]) == 3
    assert [row.current_health for row in case.dynamic_config.target_health] == [
        100, 101, 102
    ]
    assert [row.maximum_health for row in case.dynamic_config.target_health] == [
        100, 101, 102
    ]
    assert len(case.dynamic_config.background_damage_events) == 3
    assert len(case.dynamic_config.attackability_events) == 5
    assert len(case.dynamic_config.effective_armor_events) == 3
    assert case.dynamic_config.retarget_mode == "REQUIRE_EXPLICIT"
    assert case.team_only_sidecar == ()
    assert case.receipt["source"]["focal_player_guid"] == "player-fury"
    assert case.receipt["route_priority_resolved"] is False
    assert "boss_index" not in case.receipt
    assert "priority_add_indexes" not in case.receipt
    assert case.receipt["scientific_boundaries"]["comparison_authorized"] is False


def test_rejects_identity_mismatch_and_claimed_priority() -> None:
    compact, actionability, request, armors = _inputs()
    bad_identity = deepcopy(actionability)
    bad_identity["targets"][1]["target_guid"] = "other"
    with pytest.raises(UpperKaraTrashDynamicV4AdapterV1Error, match="identity"):
        compile_resolved_trash_dynamic_v4_case_v1(
            compact, bad_identity, request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=100, target_level=63,
        )
    bad_priority = deepcopy(actionability)
    bad_priority["gate"]["route_priority_resolved"] = True
    with pytest.raises(UpperKaraTrashDynamicV4AdapterV1Error, match="route priority"):
        compile_resolved_trash_dynamic_v4_case_v1(
            compact, bad_priority, request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=100, target_level=63,
        )


def test_rejects_missing_target_or_unmodeled_healing() -> None:
    compact, actionability, request, armors = _inputs()
    missing = deepcopy(actionability)
    missing["full_environment_occurrence_ids"].pop()
    with pytest.raises(UpperKaraTrashDynamicV4AdapterV1Error, match="three-target"):
        compile_resolved_trash_dynamic_v4_case_v1(
            compact, missing, request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=100, target_level=63,
        )
    healed = deepcopy(compact)
    healed["targets"][0]["hp_model"]["positive_healing_received"] = 1
    with pytest.raises(UpperKaraTrashDynamicV4AdapterV1Error, match="healing"):
        compile_resolved_trash_dynamic_v4_case_v1(
            healed, actionability, request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=100, target_level=63,
        )


def test_counterfactual_attackability_modes_keep_targets_open_without_moving_team_damage() -> None:
    compact, actionability, request, armors = _inputs()
    original = deepcopy(compact)
    common = dict(
        selected_armor_by_occurrence_id=armors,
        horizon_ms=200,
        target_level=63,
    )
    historical = compile_resolved_trash_dynamic_v4_case_v1(
        compact, actionability, request, **common
    )
    all_at_zero = compile_resolved_trash_dynamic_v4_case_v1(
        compact, actionability, request,
        attackability_mode=ALL_THREE_FROM_T0_UNTIL_SIM_DEATH, **common,
    )
    observed_onsets = compile_resolved_trash_dynamic_v4_case_v1(
        compact, actionability, request,
        attackability_mode=OBSERVED_ONSET_UNTIL_SIM_DEATH, **common,
    )
    assert compact == original
    assert historical.request == all_at_zero.request == observed_onsets.request
    assert historical.dynamic_config.background_damage_events == all_at_zero.dynamic_config.background_damage_events == observed_onsets.dynamic_config.background_damage_events
    assert [(row.time_ms, row.target_index, row.attackable) for row in historical.dynamic_config.attackability_events][-3:] == [
        (101, 0, False), (101, 1, False), (101, 2, False)
    ]
    assert [(row.time_ms, row.target_index, row.attackable) for row in all_at_zero.dynamic_config.attackability_events] == [
        (0, 0, True), (0, 1, True), (0, 2, True)
    ]
    assert [(row.time_ms, row.target_index, row.attackable) for row in observed_onsets.dynamic_config.attackability_events] == [
        (0, 0, True), (0, 1, False), (0, 2, False),
        (10, 1, True), (20, 2, True),
    ]
    assert _native_introductions(all_at_zero)[1] == {
        row.target_guid: 0 for row in all_at_zero.occurrence_index_registry
    }
    assert _native_introductions(observed_onsets)[1] == {
        row.target_guid: row.target_index * 10
        for row in observed_onsets.occurrence_index_registry
    }
    for case in (all_at_zero, observed_onsets):
        assert case.receipt["scientific_boundaries"]["observed_activity_end_closes_simulator_attackability"] is False
        assert case.receipt["scientific_boundaries"]["comparison_authorized"] is False
        assert all(row["windows"][0][1] == 200 for row in case.receipt["activity_windows_inclusive_ms"])
        assert all(row["windows"][0][1] == 100 for row in case.receipt["observed_activity_windows_inclusive_ms"])


def test_unknown_counterfactual_attackability_mode_is_rejected() -> None:
    compact, actionability, request, armors = _inputs()
    with pytest.raises(UpperKaraTrashDynamicV4AdapterV1Error, match="unknown trash attackability mode"):
        compile_resolved_trash_dynamic_v4_case_v1(
            compact, actionability, request,
            selected_armor_by_occurrence_id=armors,
            horizon_ms=100, target_level=63,
            attackability_mode="FUTURE_DEATH_TIMES_AS_POLICY",
        )
