from __future__ import annotations

from copy import deepcopy

from o2o_dps.upper_kara_incantagos_actionability_contract_v1 import (
    AFFINITY_ADD,
    AFFINITY_ENTRY_ALLOWED_SCHOOLS_V1,
    AFFINITY_SPELL_CSV_EVIDENCE_V1,
    BLOCKED_UNCLASSIFIED,
    BOSS,
    COLLATERAL_ONLY_ADD,
    COLLATERAL_ONLY_CANDIDATE,
    COMBAT_PRIORITY_ADD,
    DIRECT_AND_COLLATERAL_CANDIDATE,
    EXCLUDED_FROM_ENCOUNTER_TARGETS,
    OPTIONAL_ACTIONABLE_ADD,
    PLAYER_OWNED_EXCLUDED,
    TEAM_ONLY_SCHOOL_INCOMPATIBLE,
    UNCLASSIFIED_ELIGIBLE_TARGET,
    build_incantagos_target_actionability_contract_v1,
)
from o2o_dps.upper_kara_target_universe_resolution_v1 import (
    BOSS_OWNED_SUMMON_OCCURRENCE,
    EXACT_F130_CREATURE_OCCURRENCE,
    EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
    REGISTRY_CREATURE_OCCURRENCE,
    SCHEMA as RESOLUTION_SCHEMA,
    STATUS as RESOLUTION_STATUS,
)


BOSS_GUID = "0xF13000F1FA276A32"


def _target(
    suffix: str,
    *,
    identity: str,
    stable_entry: int | None,
    eligible: bool = True,
    display_name: str | None = None,
) -> dict:
    guid = BOSS_GUID if suffix == "boss" else f"target-{suffix}"
    return {
        "target_guid": guid,
        "resolved_occurrence_id": f"raid-a:enc-a:{suffix}",
        "resolution": identity,
        "eligible_encounter_hostile": eligible,
        "stable_template_creature_entry_id": stable_entry,
        "descriptive_evidence": {"display_name": display_name},
    }


def _resolution(*extra_targets: dict) -> dict:
    return {
        "schema": RESOLUTION_SCHEMA,
        "status": RESOLUTION_STATUS,
        "source": {
            "instance_id": "raid-a",
            "encounter_id": "enc-a",
            "pull_ref": "raid-a:enc-a",
            "audit_source": {"kind": "exact-selected-attempt"},
        },
        "exact_registry_boss_guid": BOSS_GUID,
        "targets": [
            _target(
                "boss",
                identity=REGISTRY_CREATURE_OCCURRENCE,
                stable_entry=61946,
            ),
            *extra_targets,
        ],
        "gate": {"development_case_assembly_authorized": True},
        "scientific_boundaries": {"comparison_authorized": False},
    }


def _by_entry(contract: dict) -> dict[int, dict]:
    return {
        row["stable_template_creature_entry_id"]: row
        for row in contract["targets"]
        if row["stable_template_creature_entry_id"] is not None
    }


def test_all_six_affinity_entries_bind_exact_spell_csv_school_evidence() -> None:
    affinity_targets = [
        _target(
            str(entry),
            identity=EXACT_F130_CREATURE_OCCURRENCE,
            stable_entry=entry,
        )
        for entry in sorted(AFFINITY_ENTRY_ALLOWED_SCHOOLS_V1)
    ]
    contract = build_incantagos_target_actionability_contract_v1(
        _resolution(*affinity_targets),
        candidate_damage_schools={
            "PHYSICAL",
            "FIRE",
            "NATURE",
            "FROST",
            "SHADOW",
            "ARCANE",
        },
    )
    rows = _by_entry(contract)

    expected = {
        59982: ("ARCANE", 25702, 51193),
        59983: ("SHADOW", 25701, 51192),
        59984: ("FROST", 25700, 51191),
        59985: ("NATURE", 25699, 51190),
        59986: ("FIRE", 25698, 51189),
        59987: ("PHYSICAL", 25697, 51188),
    }
    for entry, (school, csv_row, spell_id) in expected.items():
        row = rows[entry]
        assert row["mechanic_classification"] == AFFINITY_ADD
        assert row["allowed_damage_schools"] == [school]
        assert row["candidate_school_intersection"] == [school]
        assert row["candidate_actionability"] == DIRECT_AND_COLLATERAL_CANDIDATE
        assert row["spell_csv_evidence"] == {
            "csv_line_number": csv_row,
            "spell_id": spell_id,
            "school": school,
        }
        assert AFFINITY_SPELL_CSV_EVIDENCE_V1[entry]["school"] == school

    assert contract["mechanic_evidence"]["one_based_file_line_numbers"] == list(
        range(25697, 25703)
    )
    assert contract["mechanic_evidence"]["spell_ids"] == list(range(51188, 51194))
    assert contract["comparison_authorized"] is False
    assert contract["scientific_boundaries"]["comparison_authorized"] is False


def test_physical_only_keeps_mana_affinity_team_only_without_blocking_boss() -> None:
    mana = _target(
        "mana-affinity",
        identity=EXACT_F130_CREATURE_OCCURRENCE,
        stable_entry=59982,
    )
    crystal = _target(
        "crystal-affinity",
        identity=EXACT_F130_CREATURE_OCCURRENCE,
        stable_entry=59987,
    )
    contract = build_incantagos_target_actionability_contract_v1(
        _resolution(mana, crystal), candidate_damage_schools={"PHYSICAL"}
    )
    by_entry = _by_entry(contract)
    boss_id = "raid-a:enc-a:boss"
    mana_id = "raid-a:enc-a:mana-affinity"
    crystal_id = "raid-a:enc-a:crystal-affinity"

    assert by_entry[61946]["mechanic_classification"] == BOSS
    assert by_entry[59982]["candidate_actionability"] == TEAM_ONLY_SCHOOL_INCOMPATIBLE
    assert by_entry[59982]["candidate_school_intersection"] == []
    assert by_entry[59987]["candidate_actionability"] == DIRECT_AND_COLLATERAL_CANDIDATE
    assert contract["boss_occurrence_id"] == boss_id
    assert mana_id in contract["full_environment_occurrence_ids"]
    assert contract["team_only_occurrence_ids"] == [mana_id]
    assert mana_id not in contract["direct_candidate_occurrence_ids"]
    assert mana_id not in contract["collateral_candidate_occurrence_ids"]
    assert mana_id not in contract["combat_priority_add_occurrence_ids"]
    assert boss_id in contract["direct_candidate_occurrence_ids"]
    assert crystal_id not in contract["combat_priority_add_occurrence_ids"]
    assert crystal_id in contract["optional_actionable_occurrence_ids"]
    assert by_entry[59987]["target_gate_role"] == OPTIONAL_ACTIONABLE_ADD
    assert contract["gate"]["team_only_targets_block_boss"] is False
    assert contract["gate"]["development_target_actionability_authorized"] is True


def test_exact_same_attempt_registry_name_alias_classifies_f140_roles_only() -> None:
    seeker_name = "Manascale Ley-Seeker"
    whelp_name = "Manascale Whelp"
    boss_owned_seeker = _target(
        "f140-seeker",
        identity=BOSS_OWNED_SUMMON_OCCURRENCE,
        stable_entry=None,
        display_name=seeker_name,
    )
    boss_owned_whelp = _target(
        "f140-whelp",
        identity=BOSS_OWNED_SUMMON_OCCURRENCE,
        stable_entry=None,
        display_name=whelp_name,
    )
    player_owned = _target(
        "player-pet",
        identity=EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
        stable_entry=None,
        eligible=False,
    )
    registry_seeker = _target(
        "registry-seeker",
        identity=REGISTRY_CREATURE_OCCURRENCE,
        stable_entry=59989,
        display_name=seeker_name,
    )
    registry_whelp = _target(
        "registry-whelp",
        identity=REGISTRY_CREATURE_OCCURRENCE,
        stable_entry=59955,
        display_name=whelp_name,
    )
    contract = build_incantagos_target_actionability_contract_v1(
        _resolution(
            boss_owned_seeker,
            boss_owned_whelp,
            player_owned,
            registry_seeker,
            registry_whelp,
        ),
        candidate_damage_schools={"PHYSICAL"},
    )
    by_id = {row["resolved_occurrence_id"]: row for row in contract["targets"]}
    boss_owned_seeker_id = "raid-a:enc-a:f140-seeker"
    boss_owned_whelp_id = "raid-a:enc-a:f140-whelp"
    player_owned_id = "raid-a:enc-a:player-pet"
    registry_seeker_id = "raid-a:enc-a:registry-seeker"
    registry_whelp_id = "raid-a:enc-a:registry-whelp"

    assert by_id[boss_owned_seeker_id]["mechanic_classification"] == COMBAT_PRIORITY_ADD
    assert by_id[boss_owned_seeker_id]["candidate_actionability"] == DIRECT_AND_COLLATERAL_CANDIDATE
    assert by_id[boss_owned_seeker_id]["same_attempt_registry_name_alias_entry_id"] == 59989
    assert by_id[boss_owned_seeker_id]["identity_resolution"] == BOSS_OWNED_SUMMON_OCCURRENCE
    assert by_id[boss_owned_whelp_id]["mechanic_classification"] == COLLATERAL_ONLY_ADD
    assert by_id[boss_owned_whelp_id]["candidate_actionability"] == COLLATERAL_ONLY_CANDIDATE
    assert by_id[boss_owned_whelp_id]["same_attempt_registry_name_alias_entry_id"] == 59955
    assert by_id[registry_seeker_id]["mechanic_classification"] == COMBAT_PRIORITY_ADD
    assert by_id[registry_whelp_id]["mechanic_classification"] == COLLATERAL_ONLY_ADD
    assert set(contract["combat_priority_add_occurrence_ids"]) == {
        boss_owned_seeker_id,
        registry_seeker_id,
    }
    assert set(contract["collateral_only_occurrence_ids"]) == {
        boss_owned_whelp_id,
        registry_whelp_id,
    }
    assert boss_owned_whelp_id not in contract["direct_candidate_occurrence_ids"]
    assert boss_owned_whelp_id in contract["collateral_candidate_occurrence_ids"]
    assert contract["scientific_boundaries"][
        "same_attempt_registry_display_name_alias_used_for_mechanics"
    ] is True
    assert contract["scientific_boundaries"][
        "display_names_used_for_identity_resolution"
    ] is False
    assert by_id[player_owned_id]["mechanic_classification"] == PLAYER_OWNED_EXCLUDED
    assert by_id[player_owned_id]["candidate_actionability"] == EXCLUDED_FROM_ENCOUNTER_TARGETS
    assert player_owned_id == contract["excluded_player_owned_occurrence_ids"][0]
    assert player_owned_id not in contract["full_environment_occurrence_ids"]


def test_unknown_boss_owned_f140_fails_closed() -> None:
    unknown = _target(
        "unknown-f140",
        identity=BOSS_OWNED_SUMMON_OCCURRENCE,
        stable_entry=None,
        display_name="Unmatched summon",
    )
    contract = build_incantagos_target_actionability_contract_v1(
        _resolution(unknown), candidate_damage_schools={"PHYSICAL"}
    )
    occurrence_id = "raid-a:enc-a:unknown-f140"
    by_id = {row["resolved_occurrence_id"]: row for row in contract["targets"]}

    assert by_id[occurrence_id]["mechanic_classification"] == UNCLASSIFIED_ELIGIBLE_TARGET
    assert contract["gate"]["development_target_actionability_authorized"] is False
    assert contract["gate"]["unclassified_eligible_occurrence_ids"] == [occurrence_id]
    assert contract["direct_candidate_occurrence_ids"] == []
    assert contract["collateral_candidate_occurrence_ids"] == []
    assert contract["collateral_only_occurrence_ids"] == []


def test_ambiguous_same_attempt_registry_name_does_not_classify_f140() -> None:
    shared_name = "Ambiguous localized display name"
    seeker = _target(
        "registry-seeker",
        identity=REGISTRY_CREATURE_OCCURRENCE,
        stable_entry=59989,
        display_name=shared_name,
    )
    whelp = _target(
        "registry-whelp",
        identity=REGISTRY_CREATURE_OCCURRENCE,
        stable_entry=59955,
        display_name=shared_name,
    )
    summon = _target(
        "f140-ambiguous",
        identity=BOSS_OWNED_SUMMON_OCCURRENCE,
        stable_entry=None,
        display_name=shared_name,
    )
    contract = build_incantagos_target_actionability_contract_v1(
        _resolution(seeker, whelp, summon),
        candidate_damage_schools={"PHYSICAL"},
    )
    summon_id = "raid-a:enc-a:f140-ambiguous"
    by_id = {row["resolved_occurrence_id"]: row for row in contract["targets"]}

    assert by_id[summon_id]["mechanic_classification"] == UNCLASSIFIED_ELIGIBLE_TARGET
    assert by_id[summon_id]["same_attempt_registry_name_alias_entry_id"] is None
    assert contract["mechanic_evidence"][
        "same_attempt_registry_display_name_aliases"
    ] == []
    assert contract["gate"]["development_target_actionability_authorized"] is False


def test_unknown_eligible_exact_target_fails_closed_without_partial_lists() -> None:
    unknown = _target(
        "unknown-exact-creature",
        identity=EXACT_F130_CREATURE_OCCURRENCE,
        stable_entry=60000,
    )
    contract = build_incantagos_target_actionability_contract_v1(
        _resolution(unknown), candidate_damage_schools={"PHYSICAL"}
    )
    unknown_id = "raid-a:enc-a:unknown-exact-creature"
    by_id = {row["resolved_occurrence_id"]: row for row in contract["targets"]}

    assert by_id[unknown_id]["mechanic_classification"] == UNCLASSIFIED_ELIGIBLE_TARGET
    assert by_id[unknown_id]["candidate_actionability"] == BLOCKED_UNCLASSIFIED
    assert contract["full_environment_occurrence_ids"] == [
        "raid-a:enc-a:boss",
        unknown_id,
    ]
    assert contract["direct_candidate_occurrence_ids"] == []
    assert contract["collateral_candidate_occurrence_ids"] == []
    assert contract["combat_priority_add_occurrence_ids"] == []
    assert contract["gate"]["candidate_lists_released"] is False
    assert contract["gate"]["development_target_actionability_authorized"] is False
    assert contract["gate"]["unclassified_eligible_occurrence_ids"] == [unknown_id]
    assert contract["gate"]["blocking_reasons"] == [
        "UNCLASSIFIED_ELIGIBLE_TARGET_MECHANICS_REMAIN"
    ]


def test_future_fire_candidate_changes_red_affinity_from_team_only_to_actionable() -> None:
    red = _target(
        "red-affinity",
        identity=EXACT_F130_CREATURE_OCCURRENCE,
        stable_entry=59986,
    )
    physical = build_incantagos_target_actionability_contract_v1(
        _resolution(red), candidate_damage_schools={"PHYSICAL"}
    )
    fire = build_incantagos_target_actionability_contract_v1(
        deepcopy(_resolution(red)), candidate_damage_schools={"PHYSICAL", "FIRE"}
    )
    red_id = "raid-a:enc-a:red-affinity"

    assert red_id in physical["team_only_occurrence_ids"]
    assert red_id not in physical["combat_priority_add_occurrence_ids"]
    assert red_id not in fire["team_only_occurrence_ids"]
    assert red_id not in fire["combat_priority_add_occurrence_ids"]
    assert red_id in fire["optional_actionable_occurrence_ids"]
