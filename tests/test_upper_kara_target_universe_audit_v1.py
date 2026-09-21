from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.chronicle_external_compact_target_reducer_v1 import (
    SCHEMA as REDUCTION_SCHEMA,
)
from o2o_dps.upper_kara_target_universe_audit_v1 import (
    EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED,
    REGISTRY_DECLARED,
    UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
    UpperKaraTargetUniverseAuditV1Error,
    audit_upper_kara_target_universe_v1,
)


REGISTRY_GUID = "0xF130000100000001"
EXTRA_GUID = "0xF130000200000002"


def _encounter() -> dict:
    return {
        "instance_id": "raid-a",
        "encounter_id": "enc-a",
        "pull_ref": "raid-a:enc-a",
        "targets": [
            {
                "occurrence_id": "raid-a:enc-a:target-a",
                "target_guid": REGISTRY_GUID,
                "creature_entry_id": 59989,
                "display_name": "Registry Boss",
            }
        ],
    }


def _target(guid: str, *, damage: int = 100, direct_action: bool = True) -> dict:
    reference = {
        "offset_ms": 120,
        "trace_index": 7,
        "event_type": "GO",
        "player_guid": "0x0000000000000001",
        "source_guid": "0x0000000000000001",
        "spell": {"id": 23881, "name": "Bloodthirst"},
    }
    return {
        "target_guid": guid,
        "activity": {
            "first_relevant_offset_ms": 100,
            "last_relevant_offset_ms": 1_000,
            "status": "OBSERVED",
        },
        "incoming_damage": {
            "positive_sum": damage,
            "positive_event_count": 1 if damage else 0,
            "amount_unavailable_event_count": 0,
            "scope": "STRICTLY_BEFORE_FIRST_DEAD_MARKER",
        },
        "post_first_death_activity": {
            "incoming_damage": {
                "positive_sum": 0,
                "positive_event_count": 0,
                "amount_unavailable_event_count": 0,
            },
            "healing_received": {
                "positive_sum": 0,
                "positive_event_count": 0,
                "amount_unavailable_event_count": 0,
            },
            "excluded_from_hp_balance_and_team_kill_clock": True,
        },
        "death": {"status": "OBSERVED", "offset_ms": 1_000},
        "first_direct_friendly_player_action": reference if direct_action else None,
        "first_direct_friendly_player_damage": reference if damage else None,
        "first_direct_friendly_player_positive_damage": reference if damage else None,
    }


def _reduction(*targets: dict) -> dict:
    return {
        "schema": REDUCTION_SCHEMA,
        "status": "DESCRIPTIVE_OUTCOME_ONLY",
        "source_selection": {
            "instance_id": "raid-a",
            "encounter_id": "enc-a",
            "wave_id": "wave-a",
        },
        "hostile_targets": list(targets),
    }


def test_exact_registry_universe_authorizes_development_assembly_only() -> None:
    audit = audit_upper_kara_target_universe_v1(
        _encounter(),
        _reduction(_target(REGISTRY_GUID)),
        metadata_units={
            REGISTRY_GUID: {"entry": 59989, "name": "Metadata Boss"}
        },
    )

    assert audit["gate"]["development_case_assembly_authorized"] is True
    assert audit["gate"]["target_universe_matches_registry_exactly"] is True
    assert audit["scientific_boundaries"]["comparison_authorized"] is False
    row = audit["targets"][0]
    assert row["classification"] == REGISTRY_DECLARED
    assert row["creature_entry_id"] == 59989
    assert row["display_name"] == "Registry Boss"
    assert row["identity_evidence"]["metadata_display_name"] == "Metadata Boss"
    assert row["evidence"]["activity"]["first_relevant_offset_ms"] == 100
    assert row["evidence"]["death"]["offset_ms"] == 1_000
    assert row["evidence"]["incoming_damage"]["positive_sum"] == 100
    assert row["policy_semantics"]["policy_visible_phase_inferred"] is False
    assert row["policy_semantics"]["observed_activity_grants_target_permission"] is False


def test_extra_positive_damage_target_is_retained_named_and_blocks_assembly() -> None:
    audit = audit_upper_kara_target_universe_v1(
        _encounter(),
        _reduction(_target(REGISTRY_GUID), _target(EXTRA_GUID, damage=55)),
        metadata_units={EXTRA_GUID: {"entry": 33142, "name": "Mana Seeker"}},
    )

    assert audit["gate"]["development_case_assembly_authorized"] is False
    assert audit["gate"]["extra_reduction_target_guids"] == [EXTRA_GUID]
    assert audit["gate"]["extra_positive_damage_target_guids"] == [EXTRA_GUID]
    assert audit["summary"]["extra_positive_damage_target_count"] == 1
    extra = next(row for row in audit["targets"] if row["target_guid"] == EXTRA_GUID)
    assert extra["classification"] == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
    assert extra["creature_entry_id"] == 33142
    assert extra["display_name"] == "Mana Seeker"
    assert extra["evidence"]["incoming_damage"]["positive_sum"] == 55
    assert extra["evidence"]["qualifies_as_damaged_or_directly_acted_target"] is True


def test_direct_player_action_without_positive_damage_is_not_silently_dropped() -> None:
    audit = audit_upper_kara_target_universe_v1(
        _encounter(),
        _reduction(
            _target(REGISTRY_GUID),
            _target(EXTRA_GUID, damage=0, direct_action=True),
        ),
    )

    assert audit["gate"]["development_case_assembly_authorized"] is False
    assert audit["gate"]["extra_positive_damage_target_guids"] == []
    assert audit["gate"]["extra_direct_player_target_guids"] == [EXTRA_GUID]
    extra = audit["targets"][1]
    assert extra["classification"] == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
    assert extra["evidence"]["has_positive_incoming_damage"] is False
    assert extra["evidence"]["has_direct_friendly_player_target_action"] is True
    assert extra["evidence"]["qualifies_as_damaged_or_directly_acted_target"] is True


def test_registry_target_missing_from_reduction_is_retained_and_blocks() -> None:
    audit = audit_upper_kara_target_universe_v1(_encounter(), _reduction())

    assert audit["gate"]["development_case_assembly_authorized"] is False
    assert audit["gate"]["registry_missing_from_reduction_target_guids"] == [
        REGISTRY_GUID
    ]
    assert audit["targets"][0]["classification"] == REGISTRY_DECLARED
    assert audit["targets"][0]["evidence"]["present_in_external_reduction"] is False


def test_duplicate_guid_and_reversed_activity_fail_closed() -> None:
    duplicate = _encounter()
    duplicate["targets"].append(deepcopy(duplicate["targets"][0]))
    with pytest.raises(UpperKaraTargetUniverseAuditV1Error, match="repeats target GUID"):
        audit_upper_kara_target_universe_v1(
            duplicate, _reduction(_target(REGISTRY_GUID))
        )

    reversed_target = _target(REGISTRY_GUID)
    reversed_target["activity"]["last_relevant_offset_ms"] = 99
    with pytest.raises(UpperKaraTargetUniverseAuditV1Error, match="offsets are reversed"):
        audit_upper_kara_target_universe_v1(
            _encounter(), _reduction(reversed_target)
        )


def test_reduction_must_belong_to_the_same_encounter() -> None:
    reduction = _reduction(_target(REGISTRY_GUID))
    reduction["source_selection"]["encounter_id"] = "enc-other"
    with pytest.raises(UpperKaraTargetUniverseAuditV1Error, match="encounter_id differs"):
        audit_upper_kara_target_universe_v1(_encounter(), reduction)


def test_exact_metadata_player_owner_excludes_unit_from_eligible_universe() -> None:
    owner = "0x00000000002D053B"
    audit = audit_upper_kara_target_universe_v1(
        _encounter(),
        _reduction(_target(REGISTRY_GUID), _target(EXTRA_GUID, damage=55)),
        metadata_units={
            EXTRA_GUID: {
                "entry": 11859,
                "name": "Doomguard",
                "owner": owner,
            }
        },
        metadata_players={owner: {"name": "Exact Owner", "class": "WARLOCK"}},
    )

    gate = audit["gate"]
    assert gate["raw_target_universe_matches_registry_exactly"] is False
    assert gate["target_universe_matches_registry_exactly"] is False
    assert gate["raw_extra_positive_damage_target_guids"] == [EXTRA_GUID]
    assert gate["exact_player_controlled_unit_excluded_guids"] == [EXTRA_GUID]
    assert gate["eligible_extra_reduction_target_guids"] == []
    assert gate["extra_positive_damage_target_guids"] == []
    assert gate["eligible_target_universe_matches_registry_exactly"] is True
    assert gate["development_case_assembly_authorized"] is True
    assert gate["blocking_reasons"] == []
    assert audit["summary"]["extra_positive_damage_target_count"] == 1
    assert audit["summary"]["eligible_extra_positive_damage_target_count"] == 0

    extra = audit["targets"][1]
    assert extra["classification"] == EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED
    assert extra["evidence"]["incoming_damage"]["positive_sum"] == 55
    assert extra["owner_evidence"] == {
        "metadata_owner_guid": owner,
        "metadata_players_supplied": True,
        "owner_exactly_matches_metadata_player_guid": True,
        "matched_metadata_player": {"name": "Exact Owner", "class": "WARLOCK"},
        "exclusion_basis": "METADATA_UNIT_OWNER_EXACT_METADATA_PLAYER_GUID",
        "name_or_creature_entry_used_for_exclusion": False,
    }


def test_owner_is_not_excluded_without_exact_metadata_player_membership() -> None:
    owner = "0x00000000002D053B"
    units = {
        EXTRA_GUID: {
            "entry": 11859,
            "name": "Doomguard",
            "owner": owner,
            "controller": "0x0000000000CONTROLLER",
        }
    }
    reduction = _reduction(_target(REGISTRY_GUID), _target(EXTRA_GUID, damage=55))

    without_players = audit_upper_kara_target_universe_v1(
        _encounter(), reduction, metadata_units=units
    )
    extra = without_players["targets"][1]
    assert extra["classification"] == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
    assert extra["owner_evidence"]["metadata_owner_guid"] == owner
    assert extra["owner_evidence"]["metadata_players_supplied"] is False
    assert without_players["gate"]["development_case_assembly_authorized"] is False

    wrong_player = audit_upper_kara_target_universe_v1(
        _encounter(),
        reduction,
        metadata_units=units,
        metadata_players={
            "0x0000000000CONTROLLER": {
                "name": "Controller Is Not The Exact Owner"
            }
        },
    )
    extra = wrong_player["targets"][1]
    assert extra["classification"] == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
    assert extra["owner_evidence"]["owner_exactly_matches_metadata_player_guid"] is False
    assert wrong_player["gate"]["development_case_assembly_authorized"] is False
