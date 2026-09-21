from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.upper_kara_target_universe_audit_v1 import (
    EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED,
    REGISTRY_DECLARED,
    SCHEMA as AUDIT_SCHEMA,
    STATUS as AUDIT_STATUS,
    UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
)
from o2o_dps.upper_kara_target_universe_resolution_v1 import (
    BOSS_OWNED_SUMMON_OCCURRENCE,
    EXACT_F130_CREATURE_OCCURRENCE,
    EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
    REGISTRY_CREATURE_OCCURRENCE,
    UNRESOLVED_ELIGIBLE_EXTRA,
    UpperKaraTargetUniverseResolutionV1Error,
    resolve_upper_kara_target_universe_v1,
)


BOSS = "0xF13000F1FA276A32"
REGISTRY_ADD = "0xF13000EA55276A31"
BOSS_SUMMON = "0xF140008176000001"
PLAYER_SUMMON = "0xF130002E5327A9D7"
UNKNOWN = "0xF13000EA4E000099"
NON_F130_UNKNOWN = "0xF140008999000099"
PLAYER = "0x00000000002D053B"


def _encounter() -> dict:
    return {
        "instance_id": "raid-a",
        "encounter_id": "enc-a",
        "pull_ref": "raid-a:enc-a",
        "targets": [
            {
                "occurrence_id": "raid-a:enc-a:boss",
                "target_guid": BOSS,
                "creature_entry_id": 61946,
                "metadata_boss_flag": True,
            },
            {
                "occurrence_id": "raid-a:enc-a:add",
                "target_guid": REGISTRY_ADD,
                "creature_entry_id": 59989,
                "metadata_boss_flag": False,
            },
        ],
    }


def _evidence(*, present: bool = True) -> dict:
    return {
        "present_in_external_reduction": present,
        "activity": {"first_relevant_offset_ms": 100},
        "death": {"status": "OBSERVED", "offset_ms": 900},
    }


def _audit_row(
    guid: str,
    classification: str,
    *,
    entry: int | None = None,
    owner: str | None = None,
    exact_player_owner: bool = False,
    present: bool = True,
) -> dict:
    return {
        "target_guid": guid,
        "classification": classification,
        "display_name": "descriptive-only name",
        "identity_evidence": {
            "registry_creature_entry_id": entry if classification == REGISTRY_DECLARED else None,
            "registry_display_name": None,
            "metadata_creature_entry_id": entry,
            "metadata_display_name": "descriptive-only name",
        },
        "owner_evidence": {
            "metadata_owner_guid": owner,
            "owner_exactly_matches_metadata_player_guid": exact_player_owner,
            "exclusion_basis": (
                "METADATA_UNIT_OWNER_EXACT_METADATA_PLAYER_GUID"
                if exact_player_owner
                else None
            ),
        },
        "evidence": _evidence(present=present),
    }


def _audit(*extra_rows: dict) -> dict:
    rows = [
        _audit_row(BOSS, REGISTRY_DECLARED, entry=61946),
        _audit_row(REGISTRY_ADD, REGISTRY_DECLARED, entry=59989),
        *extra_rows,
    ]
    return {
        "schema": AUDIT_SCHEMA,
        "status": AUDIT_STATUS,
        "source": {
            "instance_id": "raid-a",
            "encounter_id": "enc-a",
            "pull_ref": "raid-a:enc-a",
        },
        "targets": rows,
        "summary": {
            "registry_target_count": 2,
            "reduction_hostile_target_count": len(rows),
        },
    }


def test_resolves_registry_player_owned_and_boss_owned_by_exact_guid_only() -> None:
    resolution = resolve_upper_kara_target_universe_v1(
        _encounter(),
        _audit(
            _audit_row(
                BOSS_SUMMON,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=33142,
                owner=BOSS,
            ),
            _audit_row(
                PLAYER_SUMMON,
                EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED,
                entry=11859,
                owner=PLAYER,
                exact_player_owner=True,
            ),
        ),
    )

    by_guid = {row["target_guid"]: row for row in resolution["targets"]}
    assert by_guid[BOSS]["resolution"] == REGISTRY_CREATURE_OCCURRENCE
    assert by_guid[BOSS]["resolved_occurrence_id"] == "raid-a:enc-a:boss"
    assert by_guid[BOSS]["resolved_occurrence_id_basis"] == (
        "EXACT_REGISTRY_OCCURRENCE_ID"
    )
    assert by_guid[BOSS]["stable_template_creature_entry_id"] == 61946
    assert by_guid[REGISTRY_ADD]["resolution"] == REGISTRY_CREATURE_OCCURRENCE

    boss_summon = by_guid[BOSS_SUMMON]
    assert boss_summon["resolution"] == BOSS_OWNED_SUMMON_OCCURRENCE
    assert boss_summon["identity_basis"] == (
        "EXACT_METADATA_OWNER_EQUALS_EXACT_REGISTRY_BOSS_GUID"
    )
    assert boss_summon["metadata_unit_entry_or_number"] == 33142
    assert boss_summon["stable_template_creature_entry_id"] is None
    assert boss_summon["metadata_unit_entry_treated_as_stable_template_id"] is False
    assert boss_summon["resolved_occurrence_id"] == (
        f"raid-a:enc-a:{BOSS_SUMMON}"
    )

    player_summon = by_guid[PLAYER_SUMMON]
    assert player_summon["resolution"] == EXCLUDED_EXACT_PLAYER_OWNED_UNIT
    assert player_summon["eligible_encounter_hostile"] is False
    assert player_summon["resolved_occurrence_id"] == (
        f"raid-a:enc-a:{PLAYER_SUMMON}"
    )

    assert resolution["summary"] == {
        "raw_target_count": 4,
        "raw_registry_target_count": 2,
        "raw_reduction_hostile_target_count": 4,
        "eligible_target_count": 3,
        "resolved_eligible_target_count": 3,
        "unresolved_eligible_target_count": 0,
        "resolved_eligible_extra_count": 1,
        "unresolved_eligible_extra_count": 0,
        "excluded_exact_player_owned_count": 1,
        "registry_creature_occurrence_count": 2,
        "exact_f130_creature_occurrence_count": 0,
        "boss_owned_summon_occurrence_count": 1,
    }
    assert resolution["gate"]["exact_joins"] is True
    assert resolution["gate"]["resolved_occurrence_ids_unique"] is True
    assert resolution["gate"]["development_case_assembly_authorized"] is True
    assert resolution["gate"]["blocking_reasons"] == []


def test_exact_nonregistry_f130_guid_and_metadata_entry_resolve_creature() -> None:
    resolution = resolve_upper_kara_target_universe_v1(
        _encounter(),
        _audit(
            _audit_row(
                UNKNOWN,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=59982,
            )
        ),
    )

    unknown = resolution["targets"][-1]
    assert unknown["resolution"] == EXACT_F130_CREATURE_OCCURRENCE
    assert unknown["metadata_unit_entry_or_number"] == 59982
    assert unknown["f130_guid_decoded_creature_entry_id"] == 59982
    assert unknown["f130_guid_entry_exactly_matches_metadata"] is True
    assert unknown["stable_template_creature_entry_id"] == 59982
    assert unknown["metadata_unit_entry_treated_as_stable_template_id"] is True
    assert resolution["summary"]["exact_f130_creature_occurrence_count"] == 1
    assert resolution["summary"]["resolved_eligible_extra_count"] == 1
    assert resolution["summary"]["unresolved_eligible_extra_count"] == 0
    assert resolution["gate"]["unresolved_eligible_extra_target_guids"] == []
    assert resolution["gate"]["exact_joins"] is True
    assert resolution["gate"]["development_case_assembly_authorized"] is True
    assert resolution["gate"]["blocking_reasons"] == []


def test_boss_owned_f130_keeps_exact_guid_metadata_template_but_f140_does_not() -> None:
    resolution = resolve_upper_kara_target_universe_v1(
        _encounter(),
        _audit(
            _audit_row(
                UNKNOWN,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=59982,
                owner=BOSS,
            ),
            _audit_row(
                BOSS_SUMMON,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=33142,
                owner=BOSS,
            ),
        ),
    )

    by_guid = {row["target_guid"]: row for row in resolution["targets"]}
    f130 = by_guid[UNKNOWN]
    assert f130["resolution"] == BOSS_OWNED_SUMMON_OCCURRENCE
    assert f130["stable_template_creature_entry_id"] == 59982
    assert f130["f130_guid_entry_exactly_matches_metadata"] is True
    assert by_guid[BOSS_SUMMON]["stable_template_creature_entry_id"] is None
    assert resolution["gate"]["development_case_assembly_authorized"] is True
    assert resolution["scientific_boundaries"][
        "registry_and_owner_identity_joins_use_exact_guid_equality"
    ] is True


def test_non_f130_or_mismatched_entry_extra_remains_a_blocker() -> None:
    non_f130 = resolve_upper_kara_target_universe_v1(
        _encounter(),
        _audit(
            _audit_row(
                NON_F130_UNKNOWN,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=59982,
            )
        ),
    )
    assert non_f130["targets"][-1]["resolution"] == UNRESOLVED_ELIGIBLE_EXTRA
    assert non_f130["gate"]["development_case_assembly_authorized"] is False

    mismatched = resolve_upper_kara_target_universe_v1(
        _encounter(),
        _audit(
            _audit_row(
                UNKNOWN,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=59983,
            )
        ),
    )
    row = mismatched["targets"][-1]
    assert row["f130_guid_decoded_creature_entry_id"] == 59982
    assert row["f130_guid_entry_exactly_matches_metadata"] is False
    assert row["resolution"] == UNRESOLVED_ELIGIBLE_EXTRA
    assert mismatched["gate"]["blocking_reasons"] == [
        "UNRESOLVED_ELIGIBLE_EXTRA_TARGETS_REMAIN"
    ]


def test_owner_must_equal_exact_registry_boss_guid() -> None:
    resolution = resolve_upper_kara_target_universe_v1(
        _encounter(),
        _audit(
            _audit_row(
                BOSS_SUMMON,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=33142,
                owner="0xF13000OTHERBOSS",
            )
        ),
    )

    row = resolution["targets"][-1]
    assert row["resolution"] == UNRESOLVED_ELIGIBLE_EXTRA
    assert row["identity_basis"] == "UNRESOLVED"
    assert resolution["gate"]["development_case_assembly_authorized"] is False


def test_source_and_registry_audit_mismatch_fail_the_exact_join_gate() -> None:
    wrong_source = _audit()
    wrong_source["source"]["pull_ref"] = "raid-a:other"
    source_result = resolve_upper_kara_target_universe_v1(_encounter(), wrong_source)
    assert source_result["gate"]["source_join_exact"] is False
    assert source_result["gate"]["development_case_assembly_authorized"] is False
    assert source_result["gate"]["blocking_reasons"] == [
        "AUDIT_SOURCE_DOES_NOT_EXACTLY_JOIN_REGISTRY_ENCOUNTER"
    ]

    missing = _audit()
    missing["targets"] = missing["targets"][:1]
    missing_result = resolve_upper_kara_target_universe_v1(_encounter(), missing)
    assert missing_result["gate"]["registry_target_guids_missing_from_audit"] == [
        REGISTRY_ADD
    ]
    assert missing_result["gate"]["exact_joins"] is False
    assert missing_result["gate"]["development_case_assembly_authorized"] is False


def test_missing_or_ambiguous_registry_boss_is_not_inferred_from_name_or_timing() -> None:
    no_boss = _encounter()
    for row in no_boss["targets"]:
        row["metadata_boss_flag"] = False
    result = resolve_upper_kara_target_universe_v1(
        no_boss,
        _audit(
            _audit_row(
                BOSS_SUMMON,
                UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
                entry=33142,
                owner=BOSS,
            )
        ),
    )
    assert result["exact_registry_boss_guid"] is None
    assert result["targets"][-1]["resolution"] == UNRESOLVED_ELIGIBLE_EXTRA
    assert result["gate"]["exact_joins"] is False
    assert result["scientific_boundaries"][
        "name_or_timing_used_for_identity_resolution"
    ] is False


def test_player_exclusion_requires_completed_exact_owner_evidence() -> None:
    incomplete = _audit_row(
        PLAYER_SUMMON,
        EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED,
        entry=11859,
        owner=PLAYER,
        exact_player_owner=False,
    )
    result = resolve_upper_kara_target_universe_v1(
        _encounter(), _audit(incomplete)
    )
    assert result["targets"][-1]["resolution"] == UNRESOLVED_ELIGIBLE_EXTRA
    assert result["gate"]["development_case_assembly_authorized"] is False


def test_duplicate_audit_guid_and_bad_schema_fail_structurally() -> None:
    duplicate = _audit()
    duplicate["targets"].append(deepcopy(duplicate["targets"][0]))
    with pytest.raises(
        UpperKaraTargetUniverseResolutionV1Error, match="repeats target GUID"
    ):
        resolve_upper_kara_target_universe_v1(_encounter(), duplicate)

    bad_schema = _audit()
    bad_schema["schema"] = "wrong/v1"
    with pytest.raises(
        UpperKaraTargetUniverseResolutionV1Error,
        match="unexpected target-universe audit schema",
    ):
        resolve_upper_kara_target_universe_v1(_encounter(), bad_schema)


def test_duplicate_resolved_occurrence_id_fails_structurally() -> None:
    encounter = _encounter()
    encounter["targets"][1]["occurrence_id"] = encounter["targets"][0][
        "occurrence_id"
    ]
    with pytest.raises(
        UpperKaraTargetUniverseResolutionV1Error,
        match="occurrence IDs must be unique",
    ):
        resolve_upper_kara_target_universe_v1(encounter, _audit())
