from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from o2o_dps.upper_kara_boss_encounter_contract_v1 import (
    INCANTAGOS_BOSS_ENTRY,
    INCANTAGOS_MID_ADD_ENTRY,
    INCANTAGOS_OPENING_ADD_ENTRY,
    REQUIRED_CONTINUOUS_STATE_FIELDS,
    STATE_CONTINUITY,
    UpperKaraBossEncounterContractError,
    boss_encounter_search_cell_from_dict_v1,
    build_incantagos_clean_attempt_search_cell_v1,
    incantagos_authoritative_stage_plan_v1,
)
from o2o_dps.upper_kara_route_wave_registry_v1 import build_route_wave_registry_v1
from o2o_dps.upper_kara_wave_local_search_contract_v1 import (
    BaselineRefV1,
    FrozenPlayerIdentityV1,
    ObservedTargetStateV1,
    ResourceRefV1,
    SeedNamespaceV1,
    WaveTargetBindingV1,
)
from o2o_dps.upper_kara_wave_target_gate_v1 import UpperKaraWaveTargetGateV1


_ORIGIN = datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc)


def _ts(milliseconds: int) -> str:
    return (_ORIGIN + timedelta(milliseconds=milliseconds)).isoformat().replace(
        "+00:00", "Z"
    )


def _metadata_document() -> dict:
    target_specs = [
        ("boss-guid", INCANTAGOS_BOSS_ENTRY, "Incantagos", True, 0, 150_000),
        (
            "opening-1",
            INCANTAGOS_OPENING_ADD_ENTRY,
            "Mana Seeker",
            False,
            1_200,
            7_700,
        ),
        (
            "opening-2",
            INCANTAGOS_OPENING_ADD_ENTRY,
            "Mana Seeker",
            False,
            9_600,
            15_800,
        ),
        (
            "opening-3",
            INCANTAGOS_OPENING_ADD_ENTRY,
            "Mana Seeker",
            False,
            15_900,
            22_100,
        ),
        (
            "opening-4",
            INCANTAGOS_OPENING_ADD_ENTRY,
            "Mana Seeker",
            False,
            24_800,
            54_700,
        ),
        *[
            (
                f"mid-{index}",
                INCANTAGOS_MID_ADD_ENTRY,
                "Whelp",
                False,
                100_700 + index * 100,
                105_000 + index * 1_000,
            )
            for index in range(6)
        ],
    ]
    return {
        "id": "raid-incantagos",
        "name": "Upper Tower of Karazhan",
        "units": {
            guid: {"entry": entry, "name": name}
            for guid, entry, name, _, _, _ in target_specs
        },
        "encounters": [
            {
                "id": "enc-incantagos",
                "instance_id": "raid-incantagos",
                "boss": True,
                "name": "Ley-Watcher Incantagos",
                "kill_type": "clean",
                "start_time": _ts(0),
                "end_time": _ts(150_000),
                "hostiles": [
                    {
                        "id": guid,
                        "boss": boss,
                        "periods": [
                            {
                                "start": _ts(start),
                                "end": _ts(end),
                                "last_active": _ts(end),
                                "end_state": "slain",
                            }
                        ],
                    }
                    for guid, _, _, boss, start, end in target_specs
                ],
            }
        ],
    }


def _registry_and_encounter() -> tuple[dict, dict]:
    registry = build_route_wave_registry_v1([_metadata_document()])
    return registry, registry["instances"][0]["encounters"][0]


def _bindings(encounter: dict) -> dict[str, WaveTargetBindingV1]:
    return {
        target["occurrence_id"]: WaveTargetBindingV1(
            target["occurrence_id"],
            target["target_guid"],
            "REGISTRY_CREATURE_OCCURRENCE",
            target["creature_entry_id"],
            f"hp/{target['occurrence_id']}",
            f"armor/{target['occurrence_id']}",
            f"team-clock/{target['occurrence_id']}",
            f"attackability/{target['occurrence_id']}",
        )
        for target in encounter["targets"]
    }


def _baselines() -> tuple[BaselineRefV1, ...]:
    return (
        BaselineRefV1("cat", "CAT", "policy/cat/frozen"),
        BaselineRefV1(
            "contra-deployed", "DEPLOYED_CONTRA", "policy/contra/deployed"
        ),
        BaselineRefV1("contra-new", "CONTRA_NEW", "policy/contra-new/frozen"),
        BaselineRefV1(
            "offline-expert", "OFFLINE_EXPERT", "offline/expert/same-build"
        ),
    )


def _build_kwargs(encounter: dict) -> dict:
    return {
        "instance_id": encounter["instance_id"],
        "pull_ref": encounter["pull_ref"],
        "campaign_id": "incantagos-real-cell-smoke",
        "frozen_player": FrozenPlayerIdentityV1(
            "fury-build", "talents/fury", "equipment/set-a", "loadout/raid"
        ),
        "target_bindings": _bindings(encounter),
        "entry_state_ref": "checkpoint/incantagos/pull-start/v1",
        "encounter_model_ref": "encounter/incantagos/exact-attempt/v1",
        "variant_budget": 256,
        "baselines": _baselines(),
        "train_seeds": SeedNamespaceV1("incantagos/train", (101, 102)),
        "selection_seeds": SeedNamespaceV1("incantagos/selection", (201, 202)),
        "heldout_seeds": SeedNamespaceV1("incantagos/heldout", (301, 302)),
        "resources": (
            ResourceRefV1("death-wish", "LONG_COOLDOWN"),
            ResourceRefV1("mighty-rage", "POTION"),
        ),
    }


def test_incantagos_plan_is_one_continuous_four_stage_encounter() -> None:
    _, encounter = _registry_and_encounter()

    plan = incantagos_authoritative_stage_plan_v1(encounter)

    assert [stage.stage_id for stage in plan.stages] == [
        "incantagos-opening-adds",
        "incantagos-boss-main",
        "incantagos-mid-adds",
        "incantagos-boss-finish",
    ]
    assert plan.stages[0].direct_target_mode == "FOCUS_ONE_UNTIL_DEAD"
    assert plan.stages[2].direct_target_mode == "FOCUS_ONE_UNTIL_DEAD"
    assert len(plan.stages[0].direct_target_occurrence_ids) == 4
    assert len(plan.stages[2].direct_target_occurrence_ids) == 6
    boss_id = plan.stages[1].direct_target_occurrence_ids[0]
    assert boss_id not in plan.stages[0].collateral_target_occurrence_ids
    assert boss_id not in plan.stages[2].collateral_target_occurrence_ids


def test_incantagos_cell_roundtrip_binds_models_and_forbids_stage_resets() -> None:
    registry, encounter = _registry_and_encounter()

    cell = build_incantagos_clean_attempt_search_cell_v1(
        registry, **_build_kwargs(encounter)
    )
    restored = boss_encounter_search_cell_from_dict_v1(cell.to_dict())

    assert restored == cell
    assert cell.contract.search_unit.unit_kind == "BOSS_ENCOUNTER"
    assert cell.contract.search_unit.pull_or_phase_ref == encounter["pull_ref"]
    assert len(cell.contract.targets) == 11
    assert cell.state_continuity == STATE_CONTINUITY
    assert cell.required_continuous_state_fields == REQUIRED_CONTINUOUS_STATE_FIELDS
    assert "player.weapon_state" in cell.required_continuous_state_fields
    assert {
        case.available_resource_ids
        for case in cell.contract.resource_availability_cases
    } == {
        (),
        ("death-wish",),
        ("mighty-rage",),
        ("death-wish", "mighty-rage"),
    }


def test_incantagos_runtime_gate_focuses_one_add_then_switches_causally() -> None:
    registry, encounter = _registry_and_encounter()
    cell = build_incantagos_clean_attempt_search_cell_v1(
        registry, **_build_kwargs(encounter)
    )
    contract = cell.contract
    target_index = {
        target.occurrence_id: index for index, target in enumerate(contract.targets)
    }
    boss_id = contract.target_stages[1].direct_target_occurrence_ids[0]
    opening_ids = contract.target_stages[0].direct_target_occurrence_ids
    mid_ids = contract.target_stages[2].direct_target_occurrence_ids
    observations = {
        target.occurrence_id: ObservedTargetStateV1(
            visible=target.occurrence_id == boss_id,
            attackable=target.occurrence_id == boss_id,
            dead=False,
        )
        for target in contract.targets
    }
    gate = UpperKaraWaveTargetGateV1(
        contract, lambda state: state["wave_observations"]
    )

    state = {"target_index": target_index[boss_id], "wave_observations": observations}
    before_first_add = gate.evaluate_state(state)
    assert before_first_add.stage_id == "incantagos-opening-adds"
    assert before_first_add.direct_target_indexes == ()

    observations[opening_ids[0]] = ObservedTargetStateV1(True, True, False)
    first_add = gate.evaluate_state(
        state, previous_stage_id=before_first_add.stage_id
    )
    assert first_add.direct_target_indexes == (target_index[opening_ids[0]],)

    for occurrence_id in opening_ids:
        observations[occurrence_id] = ObservedTargetStateV1(True, False, True)
    boss_main = gate.evaluate_state(state, previous_stage_id=first_add.stage_id)
    assert boss_main.stage_id == "incantagos-boss-main"
    assert boss_main.direct_target_indexes == (target_index[boss_id],)

    for occurrence_id in mid_ids[:2]:
        observations[occurrence_id] = ObservedTargetStateV1(True, True, False)
    mid_entry = gate.evaluate_state(state, previous_stage_id=boss_main.stage_id)
    assert mid_entry.stage_id == "incantagos-mid-adds"
    assert mid_entry.direct_target_indexes == tuple(
        target_index[occurrence_id] for occurrence_id in mid_ids[:2]
    )

    state["target_index"] = target_index[mid_ids[1]]
    locked = gate.evaluate_state(state, previous_stage_id=mid_entry.stage_id)
    assert locked.direct_target_indexes == (target_index[mid_ids[1]],)

    observations[mid_ids[1]] = ObservedTargetStateV1(True, False, True)
    next_add = gate.evaluate_state(state, previous_stage_id=locked.stage_id)
    assert next_add.direct_target_indexes == (target_index[mid_ids[0]],)


def test_incantagos_builder_rejects_wrong_or_censored_attempt_shape() -> None:
    registry, encounter = _registry_and_encounter()
    broken = deepcopy(encounter)
    broken["targets"][1]["creature_entry_id"] = 1
    with pytest.raises(UpperKaraBossEncounterContractError, match="multiset"):
        incantagos_authoritative_stage_plan_v1(broken)

    censored = deepcopy(encounter)
    censored["targets"][1]["death_right_censored"] = True
    with pytest.raises(UpperKaraBossEncounterContractError, match="observed deaths"):
        incantagos_authoritative_stage_plan_v1(censored)


def test_incantagos_cell_requires_every_exact_target_model_binding() -> None:
    registry, encounter = _registry_and_encounter()
    kwargs = _build_kwargs(encounter)
    kwargs["target_bindings"].pop(next(iter(kwargs["target_bindings"])))

    with pytest.raises(
        UpperKaraBossEncounterContractError,
        match="cover exactly every encounter target",
    ):
        build_incantagos_clean_attempt_search_cell_v1(registry, **kwargs)
