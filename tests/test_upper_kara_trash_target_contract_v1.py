from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.upper_kara_target_universe_audit_v1 import (
    REGISTRY_DECLARED,
    SCHEMA as AUDIT_SCHEMA,
    STATUS as AUDIT_STATUS,
    UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
)
from o2o_dps.upper_kara_trash_target_contract_v1 import (
    ENCOUNTER_ID,
    EXACT_TARGETS,
    INSTANCE_ID,
    UpperKaraTrashTargetContractV1Error,
    build_upper_kara_trash_actionability_contract_v1,
    resolve_upper_kara_trash_target_universe_v1,
)


def _inputs() -> tuple[dict, dict]:
    pull_ref = f"{INSTANCE_ID}:{ENCOUNTER_ID}"
    encounter = {
        "instance_id": INSTANCE_ID,
        "encounter_id": ENCOUNTER_ID,
        "pull_ref": pull_ref,
        "targets": [
            {
                "target_guid": guid,
                "creature_entry_id": entry,
                "occurrence_id": f"{pull_ref}:{guid}",
                "metadata_boss_flag": False,
            }
            for guid, entry in EXACT_TARGETS
        ],
    }
    audit = {
        "schema": AUDIT_SCHEMA,
        "status": AUDIT_STATUS,
        "source": {
            "instance_id": INSTANCE_ID,
            "encounter_id": ENCOUNTER_ID,
            "pull_ref": pull_ref,
        },
        "targets": [
            {
                "target_guid": guid,
                "classification": REGISTRY_DECLARED,
                "display_name": f"descriptive-{i}",
                "identity_evidence": {
                    "registry_creature_entry_id": entry,
                    "metadata_creature_entry_id": entry,
                },
                "owner_evidence": {
                    "metadata_owner_guid": None,
                    "owner_exactly_matches_metadata_player_guid": False,
                    "exclusion_basis": None,
                },
                "evidence": {
                    "present_in_external_reduction": True,
                    "activity": {"first_relevant_offset_ms": i * 1000},
                    "death": {"offset_ms": 9000 + i * 1000},
                },
            }
            for i, (guid, entry) in enumerate(EXACT_TARGETS)
        ],
        "summary": {"reduction_hostile_target_count": 3},
    }
    return encounter, audit


def test_exact_three_nonboss_registry_targets_release_development_candidates() -> None:
    encounter, audit = _inputs()
    resolution = resolve_upper_kara_trash_target_universe_v1(encounter, audit)
    contract = build_upper_kara_trash_actionability_contract_v1(resolution)

    assert resolution["exact_registry_boss_guid"] is None
    assert resolution["gate"]["development_case_assembly_authorized"] is True
    assert resolution["gate"]["exact_three_nonboss_registry_targets"] is True
    assert len(contract["targets"]) == 3
    assert [row["target_guid"] for row in contract["targets"]] == [
        guid for guid, _ in EXACT_TARGETS
    ]
    assert contract["full_environment_occurrence_ids"] == (
        contract["direct_candidate_occurrence_ids"]
    ) == contract["collateral_candidate_occurrence_ids"]
    assert all(
        row["candidate_actionability"] == "DIRECT_AND_COLLATERAL_CANDIDATE"
        for row in contract["targets"]
    )
    assert contract["gate"]["route_priority_resolved"] is False
    assert contract["comparison_authorized"] is False


@pytest.mark.parametrize(
    "mutation, reason",
    [
        (lambda e, a: a["source"].update(pull_ref="wrong"), "SOURCE"),
        (
            lambda e, a: e["targets"][0].update(metadata_boss_flag=True),
            "NONBOSS",
        ),
        (
            lambda e, a: a["targets"][1]["identity_evidence"].update(
                metadata_creature_entry_id=62019
            ),
            "METADATA_ENTRY",
        ),
    ],
)
def test_identity_or_source_mismatch_blocks_assembly(mutation, reason: str) -> None:
    encounter, audit = _inputs()
    mutation(encounter, audit)
    resolution = resolve_upper_kara_trash_target_universe_v1(encounter, audit)
    assert resolution["gate"]["development_case_assembly_authorized"] is False
    assert any(reason in item for item in resolution["gate"]["blocking_reasons"])
    with pytest.raises(UpperKaraTrashTargetContractV1Error):
        build_upper_kara_trash_actionability_contract_v1(resolution)


def test_eligible_extra_hostile_blocks_three_target_contract() -> None:
    encounter, audit = _inputs()
    extra = deepcopy(audit["targets"][0])
    extra.update(
        target_guid="0xF13000F246276CB2",
        classification=UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
    )
    audit["targets"].append(extra)
    resolution = resolve_upper_kara_trash_target_universe_v1(encounter, audit)
    assert resolution["gate"]["development_case_assembly_authorized"] is False
    assert len(resolution["targets"]) == 4


def test_descriptive_timing_does_not_change_target_permission() -> None:
    encounter, audit = _inputs()
    a = build_upper_kara_trash_actionability_contract_v1(
        resolve_upper_kara_trash_target_universe_v1(encounter, audit)
    )
    for row in audit["targets"]:
        row["display_name"] = "changed-description"
        row["evidence"]["activity"]["first_relevant_offset_ms"] = 999999
        row["evidence"]["death"]["offset_ms"] = 1
    b = build_upper_kara_trash_actionability_contract_v1(
        resolve_upper_kara_trash_target_universe_v1(encounter, audit)
    )
    assert b["direct_candidate_occurrence_ids"] == a[
        "direct_candidate_occurrence_ids"
    ]
    assert b["collateral_candidate_occurrence_ids"] == a[
        "collateral_candidate_occurrence_ids"
    ]
