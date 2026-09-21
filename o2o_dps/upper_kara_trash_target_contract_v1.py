"""Exact-GUID development target contract for one three-creature Upper Kara wave.

The Chronicle reduction establishes target identity and damage evidence, not
the raid leader's kill order or simultaneous melee reachability.  All three
registry creatures are exposed as *candidate* direct/collateral targets; that
actionability remains a simulator hypothesis and never authorizes comparison.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .upper_kara_target_universe_audit_v1 import REGISTRY_DECLARED
from .upper_kara_target_universe_resolution_v1 import (
    REGISTRY_CREATURE_OCCURRENCE,
    SCHEMA as RESOLUTION_SCHEMA,
    STATUS as RESOLUTION_STATUS,
    resolve_upper_kara_target_universe_v1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_trash_target_actionability_contract/v1"
STATUS = "DEVELOPMENT_ACTIONABILITY_ONLY_NOT_COMPARISON_AUTHORIZED"
ACTIONABILITY_SCHEMA = SCHEMA
ACTIONABILITY_STATUS = STATUS
DIRECT_AND_COLLATERAL_CANDIDATE = "DIRECT_AND_COLLATERAL_CANDIDATE"

INSTANCE_ID = "d900a97b-b53e-4444-943b-3e0f2be8d477"
ENCOUNTER_ID = "02829cd0-85c3-4b6f-adba-059398e6ae14"
EXACT_TARGETS = (
    ("0xF13000F240276CB6", 62016),
    ("0xF13000F244276CB4", 62020),
    ("0xF13000F245276CB3", 62021),
)


class UpperKaraTrashTargetContractV1Error(ValueError):
    """The target contract input is structurally invalid or not resolved."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraTrashTargetContractV1Error(f"{label} must be an object")
    return value


def _target_rows(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise UpperKaraTrashTargetContractV1Error(f"{label} must be an array")
    return [_mapping(row, f"{label}[{i}]") for i, row in enumerate(value)]


def resolve_upper_kara_trash_target_universe_v1(
    encounter: Mapping[str, Any], audit: Mapping[str, Any]
) -> JSONMap:
    """Release the existing compact-model resolution schema without a fake Boss.

    This wave is bound to three exact metadata/registry GUIDs and stable F130
    entries.  The generic resolver supplies the occurrence rows; its one-Boss
    gate is replaced by the explicit three-trash gate here.
    """

    encounter = _mapping(encounter, "encounter")
    audit = _mapping(audit, "audit")
    resolution = resolve_upper_kara_target_universe_v1(encounter, audit)
    registry = _target_rows(encounter.get("targets"), "encounter.targets")
    audited = _target_rows(audit.get("targets"), "audit.targets")
    resolved = _target_rows(resolution.get("targets"), "resolution.targets")
    expected = list(EXACT_TARGETS)
    source_exact = bool(
        encounter.get("instance_id") == INSTANCE_ID
        and encounter.get("encounter_id") == ENCOUNTER_ID
        and resolution["gate"]["source_join_exact"]
    )
    registry_exact = [
        (row.get("target_guid"), row.get("creature_entry_id")) for row in registry
    ] == expected and all(row.get("metadata_boss_flag") is False for row in registry)
    audit_exact = [row.get("target_guid") for row in audited] == [
        guid for guid, _ in expected
    ]
    metadata_entry_exact = audit_exact and all(
        row.get("classification") == REGISTRY_DECLARED
        and _mapping(row.get("identity_evidence"), "audit identity evidence").get(
            "registry_creature_entry_id"
        ) == entry
        and row["identity_evidence"].get("metadata_creature_entry_id") == entry
        and _mapping(row.get("evidence"), "audit target evidence").get(
            "present_in_external_reduction"
        ) is True
        for row, (_, entry) in zip(audited, expected)
    )
    resolved_exact = [
        (row.get("target_guid"), row.get("stable_template_creature_entry_id"))
        for row in resolved
    ] == expected and all(
        row.get("resolution") == REGISTRY_CREATURE_OCCURRENCE
        and row.get("eligible_encounter_hostile") is True
        for row in resolved
    )
    gate = resolution["gate"]
    gate.pop("registry_boss_target_guids", None)
    gate.pop("exact_single_registry_boss_guid_available", None)
    no_extras = not gate["unresolved_eligible_extra_target_guids"] and len(resolved) == 3
    authorized = bool(
        source_exact
        and registry_exact
        and audit_exact
        and metadata_entry_exact
        and resolved_exact
        and no_extras
    )
    blockers = []
    if not source_exact:
        blockers.append("TRASH_SOURCE_JOIN_OR_ENCOUNTER_ID_MISMATCH")
    if not registry_exact:
        blockers.append("EXACT_THREE_NONBOSS_REGISTRY_TARGETS_UNAVAILABLE")
    if not audit_exact or not metadata_entry_exact:
        blockers.append("AUDIT_GUID_OR_METADATA_ENTRY_JOIN_MISMATCH")
    if not resolved_exact or not no_extras:
        blockers.append("RESOLVED_TARGET_UNIVERSE_DIFFERS_FROM_THREE_REGISTRY_CREATURES")
    gate.update(
        {
            "expected_trash_target_guids": [guid for guid, _ in expected],
            "exact_three_nonboss_registry_targets": registry_exact,
            "audit_guid_and_metadata_entry_join_exact": bool(
                audit_exact and metadata_entry_exact
            ),
            "resolved_three_registry_creatures_no_extras": bool(
                resolved_exact and no_extras
            ),
            "exact_joins": authorized,
            "development_case_assembly_authorized": authorized,
            "blocking_reasons": blockers,
        }
    )
    resolution["scientific_boundaries"].update(
        {
            "nonboss_trash_encounter": True,
            "route_priority_resolved": False,
            "simultaneous_melee_reachability_observed": False,
            "comparison_authorized": False,
        }
    )
    return resolution


def build_upper_kara_trash_actionability_contract_v1(
    resolution: Mapping[str, Any],
) -> JSONMap:
    """Expose three exact occurrences under an explicit reachability hypothesis."""

    resolution = _mapping(resolution, "resolution")
    if (
        resolution.get("schema") != RESOLUTION_SCHEMA
        or resolution.get("status") != RESOLUTION_STATUS
        or _mapping(resolution.get("gate"), "resolution.gate").get(
            "development_case_assembly_authorized"
        ) is not True
    ):
        raise UpperKaraTrashTargetContractV1Error(
            "three-trash target universe is not resolved for development assembly"
        )
    rows = _target_rows(resolution.get("targets"), "resolution.targets")
    if [row.get("target_guid") for row in rows] != [
        guid for guid, _ in EXACT_TARGETS
    ]:
        raise UpperKaraTrashTargetContractV1Error(
            "resolved targets differ from the exact three-trash registry"
        )
    occurrences = [row["resolved_occurrence_id"] for row in rows]
    targets = [
        {
            "target_guid": row["target_guid"],
            "resolved_occurrence_id": row["resolved_occurrence_id"],
            "identity_resolution": row["resolution"],
            "stable_template_creature_entry_id": row[
                "stable_template_creature_entry_id"
            ],
            "candidate_actionability": DIRECT_AND_COLLATERAL_CANDIDATE,
            "target_gate_role": "TRASH_ROUTE_PRIORITY_UNRESOLVED",
            "policy_semantics": {
                "exact_guid_identity": True,
                "future_activity_or_death_used_for_target_permission": False,
                "simultaneous_melee_reachability_is_hypothesis": True,
            },
        }
        for row in rows
    ]
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "source": deepcopy(dict(_mapping(resolution.get("source"), "resolution.source"))),
        "full_environment_occurrence_ids": occurrences,
        "direct_candidate_occurrence_ids": list(occurrences),
        "collateral_candidate_occurrence_ids": list(occurrences),
        "targets": targets,
        "gate": {
            "development_target_actionability_authorized": True,
            "candidate_lists_released": True,
            "exact_three_registry_hostiles": True,
            "route_priority_resolved": False,
            "blocking_reasons": [],
        },
        "hypotheses": {
            "all_three_direct_targetable_when_engaged": True,
            "all_three_collateral_reachable_when_engaged": True,
            "raid_leader_kill_order": "UNRESOLVED",
            "simultaneous_melee_reachability": "UNVERIFIED",
        },
        "comparison_authorized": False,
        "scientific_boundaries": {
            "comparison_authorized": False,
            "name_or_future_timing_used_for_identity_or_target_permission": False,
            "direct_and_collateral_are_development_hypotheses": True,
            "route_priority_resolved": False,
        },
    }


__all__ = (
    "ACTIONABILITY_SCHEMA",
    "ACTIONABILITY_STATUS",
    "DIRECT_AND_COLLATERAL_CANDIDATE",
    "ENCOUNTER_ID",
    "EXACT_TARGETS",
    "INSTANCE_ID",
    "SCHEMA",
    "STATUS",
    "UpperKaraTrashTargetContractV1Error",
    "build_upper_kara_trash_actionability_contract_v1",
    "resolve_upper_kara_trash_target_universe_v1",
)
