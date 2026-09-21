from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from o2o_dps.upper_kara_registry_local_contract_adapter_v1 import (
    UpperKaraRegistryLocalContractAdapterError,
    registry_phase_to_local_search_contract_v1,
    resource_availability_cases_v1,
)
from o2o_dps.upper_kara_route_wave_registry_v1 import (
    apply_authoritative_target_overlay_v1,
    build_route_wave_registry_v1,
)
from o2o_dps.upper_kara_wave_local_search_contract_v1 import (
    BaselineRefV1,
    FrozenPlayerIdentityV1,
    ResourceRefV1,
    SeedNamespaceV1,
    WaveTargetBindingV1,
)


def _ts(second: int) -> str:
    value = datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc) + timedelta(
        seconds=second
    )
    return value.isoformat().replace("+00:00", "Z")


def _metadata_document() -> dict:
    return {
        "id": "raid-a",
        "name": "Upper Tower of Karazhan",
        "units": {
            "boss-guid": {"entry": 2001, "name": "Boss"},
            "add-one-guid": {"entry": 3001, "name": "Priority Add"},
            "add-two-guid": {"entry": 3001, "name": "Priority Add"},
        },
        "encounters": [
            {
                "id": "encounter-a",
                "instance_id": "raid-a",
                "boss": True,
                "name": "Boss and Adds",
                "kill_type": "clean",
                "start_time": _ts(0),
                "end_time": _ts(20),
                "hostiles": [
                    {
                        "id": "boss-guid",
                        "boss": True,
                        "periods": [
                            {
                                "start": _ts(0),
                                "end": _ts(20),
                                "last_active": _ts(20),
                                "end_state": "slain",
                            }
                        ],
                    },
                    {
                        "id": "add-one-guid",
                        "boss": False,
                        "periods": [
                            {
                                "start": _ts(5),
                                "end": _ts(10),
                                "last_active": _ts(10),
                                "end_state": "slain",
                            }
                        ],
                    },
                    {
                        "id": "add-two-guid",
                        "boss": False,
                        "periods": [
                            {
                                "start": _ts(5),
                                "end": _ts(12),
                                "last_active": _ts(12),
                                "end_state": "slain",
                            }
                        ],
                    },
                ],
            }
        ],
    }


def _selected_phase(registry: dict) -> tuple[dict, dict]:
    encounter = registry["instances"][0]["encounters"][0]
    phase = next(
        row
        for row in encounter["phase_candidates"]
        if len(row["active_occurrence_ids"]) == 3
    )
    return encounter, phase


def _baselines() -> tuple[BaselineRefV1, ...]:
    return (
        BaselineRefV1("cat", "CAT", "policy/cat/frozen"),
        BaselineRefV1(
            "contra-deployed", "DEPLOYED_CONTRA", "policy/contra/deployed"
        ),
        BaselineRefV1("contra-new", "CONTRA_NEW", "policy/contra-new/frozen"),
        BaselineRefV1(
            "offline-expert", "OFFLINE_EXPERT", "offline/expert/clean-window"
        ),
    )


def _bindings(encounter: dict) -> dict[str, WaveTargetBindingV1]:
    return {
        row["occurrence_id"]: WaveTargetBindingV1(
            occurrence_id=row["occurrence_id"],
            target_guid=row["target_guid"],
            identity_kind="REGISTRY_CREATURE_OCCURRENCE",
            creature_entry_id=row["creature_entry_id"],
            hp_model_ref=f"hp/{row['occurrence_id']}",
            armor_model_ref=f"armor/{row['occurrence_id']}",
            team_kill_clock_ref=f"team-clock/{row['occurrence_id']}",
            attackability_ref=f"attackability/{row['occurrence_id']}",
        )
        for row in encounter["targets"]
    }


def _adapter_kwargs(registry: dict, encounter: dict, phase: dict) -> dict:
    return {
        "registry": registry,
        "instance_id": "raid-a",
        "pull_ref": encounter["pull_ref"],
        "phase_ref": phase["phase_ref"],
        "campaign_id": "upper-kara-real-wave-test",
        "frozen_player": FrozenPlayerIdentityV1(
            "fury-build", "talents/fury", "equipment/set-a", "loadout/rage"
        ),
        "target_bindings": _bindings(encounter),
        "variant_budget": 256,
        "baselines": _baselines(),
        "train_seeds": SeedNamespaceV1("wave/train", (101, 102)),
        "selection_seeds": SeedNamespaceV1("wave/selection", (201, 202)),
        "heldout_seeds": SeedNamespaceV1("wave/heldout", (301, 302)),
        "resources": (
            ResourceRefV1("death-wish", "LONG_COOLDOWN"),
            ResourceRefV1("mighty-rage", "POTION"),
        ),
    }


def test_unresolved_phase_cannot_become_a_search_contract() -> None:
    registry = build_route_wave_registry_v1([_metadata_document()])
    encounter, phase = _selected_phase(registry)

    with pytest.raises(
        UpperKaraRegistryLocalContractAdapterError, match="rules are unresolved"
    ):
        registry_phase_to_local_search_contract_v1(
            **_adapter_kwargs(registry, encounter, phase)
        )


def test_authoritative_phase_preserves_direct_collateral_and_resource_cases() -> None:
    registry = build_route_wave_registry_v1([_metadata_document()])
    encounter, phase = _selected_phase(registry)
    boss = next(
        row["occurrence_id"]
        for row in encounter["targets"]
        if row["metadata_boss_flag"] is True
    )
    adds = tuple(
        row["occurrence_id"]
        for row in encounter["targets"]
        if row["metadata_boss_flag"] is not True
    )
    resolved = apply_authoritative_target_overlay_v1(
        registry,
        {
            "pull_ref": encounter["pull_ref"],
            "phase_ref": phase["phase_ref"],
            "source_kind": "USER_AUTHORITATIVE",
            "source_ref": "raid-leader:adds-first",
            "allowed_primary_occurrence_ids": list(adds),
            "forbidden_primary_occurrence_ids": [boss],
            "priority_partial_order": [[adds[0], adds[1]]],
            "collateral_occurrence_ids": [boss, *adds],
        },
    )

    contract = registry_phase_to_local_search_contract_v1(
        **_adapter_kwargs(resolved, encounter, phase)
    )

    stage = contract.target_stages[0]
    assert contract.search_unit.unit_kind == "BOSS_PHASE"
    assert contract.search_unit.route_ref == resolved["instances"][0][
        "route_variant_id"
    ]
    assert stage.stage_role == "ADDS"
    assert stage.direct_target_mode == "FIXED_SEQUENCE"
    assert stage.direct_target_occurrence_ids == adds
    assert stage.collateral_target_occurrence_ids == (boss, *adds)
    assert boss not in stage.direct_target_occurrence_ids
    assert tuple(row.occurrence_id for row in contract.targets) == tuple(
        row["occurrence_id"] for row in encounter["targets"]
    )
    assert {
        row.available_resource_ids for row in contract.resource_availability_cases
    } == {
        (),
        ("death-wish",),
        ("mighty-rage",),
        ("death-wish", "mighty-rage"),
    }


def test_non_total_priority_order_is_not_silently_linearized() -> None:
    registry = build_route_wave_registry_v1([_metadata_document()])
    encounter, phase = _selected_phase(registry)
    target_ids = tuple(row["occurrence_id"] for row in encounter["targets"])
    resolved = apply_authoritative_target_overlay_v1(
        registry,
        {
            "pull_ref": encounter["pull_ref"],
            "phase_ref": phase["phase_ref"],
            "source_kind": "MECHANIC_AUTHORITATIVE",
            "source_ref": "mechanic:one-add-before-second",
            "allowed_primary_occurrence_ids": list(target_ids),
            "forbidden_primary_occurrence_ids": [],
            "priority_partial_order": [[target_ids[1], target_ids[2]]],
            "collateral_occurrence_ids": list(target_ids),
        },
    )

    with pytest.raises(
        UpperKaraRegistryLocalContractAdapterError, match="not a total order"
    ):
        registry_phase_to_local_search_contract_v1(
            **_adapter_kwargs(resolved, encounter, phase)
        )


def test_target_models_must_cover_the_exact_encounter() -> None:
    registry = build_route_wave_registry_v1([_metadata_document()])
    encounter, phase = _selected_phase(registry)
    target_ids = tuple(row["occurrence_id"] for row in encounter["targets"])
    resolved = apply_authoritative_target_overlay_v1(
        registry,
        {
            "pull_ref": encounter["pull_ref"],
            "phase_ref": phase["phase_ref"],
            "source_kind": "USER_AUTHORITATIVE",
            "source_ref": "raid-leader:any-target",
            "allowed_primary_occurrence_ids": list(target_ids),
            "forbidden_primary_occurrence_ids": [],
            "priority_partial_order": [],
            "collateral_occurrence_ids": list(target_ids),
        },
    )
    kwargs = _adapter_kwargs(resolved, encounter, phase)
    kwargs["target_bindings"].pop(target_ids[-1])

    with pytest.raises(
        UpperKaraRegistryLocalContractAdapterError,
        match="cover exactly every encounter target",
    ):
        registry_phase_to_local_search_contract_v1(**kwargs)


def test_empty_resource_panel_still_exposes_the_none_ready_case() -> None:
    cases = resource_availability_cases_v1(())

    assert len(cases) == 1
    assert cases[0].case_id == "none-ready"
    assert cases[0].available_resource_ids == ()
