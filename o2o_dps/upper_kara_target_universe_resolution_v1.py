"""Resolve an audited Upper Karazhan target universe without identity guesses.

This layer is deliberately narrower than encounter reconstruction.  It accepts
only exact GUID relationships already present in the route registry and target
universe audit.  In particular, Chronicle ``F140`` unit entries are retained as
metadata unit numbers for boss-owned summon occurrences; they are never
promoted to stable creature-template identifiers.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from .upper_kara_target_universe_audit_v1 import (
    EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED,
    REGISTRY_DECLARED,
    SCHEMA as AUDIT_SCHEMA,
    STATUS as AUDIT_STATUS,
    UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
)


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_target_universe_resolution/v1"
STATUS = "TARGET_IDENTITIES_RESOLVED"

REGISTRY_CREATURE_OCCURRENCE = "REGISTRY_CREATURE_OCCURRENCE"
EXACT_F130_CREATURE_OCCURRENCE = "EXACT_F130_CREATURE_OCCURRENCE"
EXCLUDED_EXACT_PLAYER_OWNED_UNIT = "EXCLUDED_EXACT_PLAYER_OWNED_UNIT"
BOSS_OWNED_SUMMON_OCCURRENCE = "BOSS_OWNED_SUMMON_OCCURRENCE"
UNRESOLVED_ELIGIBLE_EXTRA = "UNRESOLVED_ELIGIBLE_EXTRA"

_F130_CREATURE_ENTRY_RE = re.compile(r"^0xF130([0-9A-F]{6})[0-9A-F]{6}$", re.IGNORECASE)


class UpperKaraTargetUniverseResolutionV1Error(ValueError):
    """The registry or target-universe audit is structurally malformed."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraTargetUniverseResolutionV1Error(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise UpperKaraTargetUniverseResolutionV1Error(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraTargetUniverseResolutionV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UpperKaraTargetUniverseResolutionV1Error(
            f"{label} must be a positive integer"
        )
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label)


def _exact_f130_creature_entry(guid: str) -> int | None:
    match = _F130_CREATURE_ENTRY_RE.fullmatch(guid)
    return int(match.group(1), 16) if match is not None else None


def _registry_targets(
    encounter: Mapping[str, Any],
) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    ordered: list[str] = []
    by_guid: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(_array(encounter.get("targets"), "encounter.targets")):
        row = _mapping(value, f"encounter.targets[{index}]")
        guid = _text(row.get("target_guid"), f"encounter.targets[{index}].target_guid")
        if guid in by_guid:
            raise UpperKaraTargetUniverseResolutionV1Error(
                f"encounter.targets repeats target GUID {guid}"
            )
        _positive_int(
            row.get("creature_entry_id"),
            f"encounter.targets[{index}].creature_entry_id",
        )
        _text(
            row.get("occurrence_id"),
            f"encounter.targets[{index}].occurrence_id",
        )
        ordered.append(guid)
        by_guid[guid] = row
    if not ordered:
        raise UpperKaraTargetUniverseResolutionV1Error(
            "encounter.targets must not be empty"
        )
    return ordered, by_guid


def _audit_targets(
    audit: Mapping[str, Any],
) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    ordered: list[str] = []
    by_guid: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(_array(audit.get("targets"), "audit.targets")):
        row = _mapping(value, f"audit.targets[{index}]")
        guid = _text(row.get("target_guid"), f"audit.targets[{index}].target_guid")
        if guid in by_guid:
            raise UpperKaraTargetUniverseResolutionV1Error(
                f"audit.targets repeats target GUID {guid}"
            )
        _text(row.get("classification"), f"audit.targets[{index}].classification")
        ordered.append(guid)
        by_guid[guid] = row
    return ordered, by_guid


def _exact_registry_boss_guids(
    registry_order: list[str], registry_by_guid: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    return [
        guid
        for guid in registry_order
        if registry_by_guid[guid].get("metadata_boss_flag") is True
    ]


def resolve_upper_kara_target_universe_v1(
    encounter: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> JSONMap:
    """Resolve registry, player-owned, and boss-owned target occurrences.

    Names, activity offsets, damage timing, and death timing are carried only as
    descriptive audit evidence and never participate in identity resolution.
    """

    raw_encounter = _mapping(encounter, "encounter")
    raw_audit = _mapping(audit, "audit")
    if raw_audit.get("schema") != AUDIT_SCHEMA:
        raise UpperKaraTargetUniverseResolutionV1Error(
            "unexpected target-universe audit schema"
        )
    if raw_audit.get("status") != AUDIT_STATUS:
        raise UpperKaraTargetUniverseResolutionV1Error(
            "target-universe audit status is not complete"
        )

    source = _mapping(raw_audit.get("source"), "audit.source")
    instance_id = _text(raw_encounter.get("instance_id"), "encounter.instance_id")
    encounter_id = _text(
        raw_encounter.get("encounter_id"), "encounter.encounter_id"
    )
    source_fields = ("instance_id", "encounter_id", "pull_ref")
    source_join = {
        field: source.get(field) == raw_encounter.get(field) for field in source_fields
    }
    source_join_exact = all(source_join.values())

    registry_order, registry_by_guid = _registry_targets(raw_encounter)
    audit_order, audit_by_guid = _audit_targets(raw_audit)
    registry_guids = set(registry_by_guid)
    audit_guids = set(audit_by_guid)

    boss_guids = _exact_registry_boss_guids(registry_order, registry_by_guid)
    exact_boss_guid = boss_guids[0] if len(boss_guids) == 1 else None

    missing_audit_rows = sorted(registry_guids - audit_guids)
    registry_classification_mismatches = sorted(
        guid
        for guid in registry_guids & audit_guids
        if audit_by_guid[guid].get("classification") != REGISTRY_DECLARED
    )
    registry_missing_reduction_evidence = sorted(
        guid
        for guid in registry_guids & audit_guids
        if not bool(
            _mapping(
                audit_by_guid[guid].get("evidence"),
                f"audit target {guid}.evidence",
            ).get("present_in_external_reduction")
        )
    )

    allowed_audit_classes = {
        REGISTRY_DECLARED,
        EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED,
        UNRESOLVED_EXTRA_DAMAGED_HOSTILE,
    }
    unknown_classification_guids = sorted(
        guid
        for guid, row in audit_by_guid.items()
        if row.get("classification") not in allowed_audit_classes
    )

    rows: list[JSONMap] = []
    for guid in audit_order:
        audit_row = audit_by_guid[guid]
        audit_classification = str(audit_row.get("classification"))
        registry_target = registry_by_guid.get(guid)
        owner_evidence = _mapping(
            audit_row.get("owner_evidence"), f"audit target {guid}.owner_evidence"
        )
        identity_evidence = _mapping(
            audit_row.get("identity_evidence"),
            f"audit target {guid}.identity_evidence",
        )
        owner_guid = _optional_text(
            owner_evidence.get("metadata_owner_guid"),
            f"audit target {guid}.owner_evidence.metadata_owner_guid",
        )
        metadata_unit_entry = _optional_positive_int(
            identity_evidence.get("metadata_creature_entry_id"),
            f"audit target {guid}.identity_evidence.metadata_creature_entry_id",
        )
        f130_creature_entry = _exact_f130_creature_entry(guid)
        exact_f130_entry_join = bool(
            f130_creature_entry is not None
            and metadata_unit_entry == f130_creature_entry
        )

        exact_player_owner = bool(
            audit_classification == EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED
            and owner_evidence.get("owner_exactly_matches_metadata_player_guid") is True
            and owner_evidence.get("exclusion_basis")
            == "METADATA_UNIT_OWNER_EXACT_METADATA_PLAYER_GUID"
        )

        if exact_player_owner:
            resolution = EXCLUDED_EXACT_PLAYER_OWNED_UNIT
            eligible = False
            stable_template_entry = None
        elif (
            registry_target is not None
            and audit_classification == REGISTRY_DECLARED
        ):
            resolution = REGISTRY_CREATURE_OCCURRENCE
            eligible = True
            stable_template_entry = _positive_int(
                registry_target.get("creature_entry_id"),
                f"registry target {guid}.creature_entry_id",
            )
        elif (
            registry_target is None
            and audit_classification == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
            and exact_boss_guid is not None
            and owner_guid == exact_boss_guid
        ):
            resolution = BOSS_OWNED_SUMMON_OCCURRENCE
            eligible = True
            stable_template_entry = (
                f130_creature_entry if exact_f130_entry_join else None
            )
        elif (
            registry_target is None
            and audit_classification == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
            and exact_f130_entry_join
        ):
            resolution = EXACT_F130_CREATURE_OCCURRENCE
            eligible = True
            stable_template_entry = f130_creature_entry
        else:
            resolution = UNRESOLVED_ELIGIBLE_EXTRA
            eligible = True
            stable_template_entry = None

        if registry_target is not None:
            resolved_occurrence_id = _text(
                registry_target.get("occurrence_id"),
                f"registry target {guid}.occurrence_id",
            )
            resolved_occurrence_id_basis = "EXACT_REGISTRY_OCCURRENCE_ID"
        else:
            resolved_occurrence_id = f"{instance_id}:{encounter_id}:{guid}"
            resolved_occurrence_id_basis = (
                "DETERMINISTIC_SOURCE_IDS_AND_EXACT_TARGET_GUID"
            )

        rows.append(
            {
                "target_guid": guid,
                "resolved_occurrence_id": resolved_occurrence_id,
                "resolved_occurrence_id_basis": resolved_occurrence_id_basis,
                "resolution": resolution,
                "eligible_encounter_hostile": eligible,
                "registry_occurrence_id": (
                    registry_target.get("occurrence_id")
                    if registry_target is not None
                    else None
                ),
                "stable_template_creature_entry_id": stable_template_entry,
                "metadata_unit_entry_or_number": metadata_unit_entry,
                "metadata_unit_entry_treated_as_stable_template_id": bool(
                    exact_f130_entry_join
                ),
                "f130_guid_decoded_creature_entry_id": f130_creature_entry,
                "f130_guid_entry_exactly_matches_metadata": exact_f130_entry_join,
                "exact_owner_guid": owner_guid,
                "exact_registry_boss_guid": exact_boss_guid,
                "audit_classification": audit_classification,
                "identity_basis": (
                    "EXACT_REGISTRY_TARGET_GUID"
                    if resolution == REGISTRY_CREATURE_OCCURRENCE
                    else (
                        "EXACT_METADATA_PLAYER_OWNER_GUID"
                        if resolution == EXCLUDED_EXACT_PLAYER_OWNED_UNIT
                        else (
                            "EXACT_METADATA_OWNER_EQUALS_EXACT_REGISTRY_BOSS_GUID"
                            if resolution == BOSS_OWNED_SUMMON_OCCURRENCE
                            else (
                                "EXACT_F130_GUID_ENTRY_EQUALS_METADATA_ENTRY"
                                if resolution == EXACT_F130_CREATURE_OCCURRENCE
                                else "UNRESOLVED"
                            )
                        )
                    )
                ),
                "descriptive_evidence": {
                    "display_name": audit_row.get("display_name"),
                    "activity": deepcopy(
                        dict(
                            _mapping(
                                audit_row.get("evidence"),
                                f"audit target {guid}.evidence",
                            )
                        )
                    ).get("activity"),
                    "death": deepcopy(
                        dict(
                            _mapping(
                                audit_row.get("evidence"),
                                f"audit target {guid}.evidence",
                            )
                        )
                    ).get("death"),
                },
                "policy_semantics": {
                    "name_used_for_identity_resolution": False,
                    "activity_used_for_identity_resolution": False,
                    "death_used_for_identity_resolution": False,
                    "activity_grants_target_permission": False,
                },
            }
        )

    resolved_occurrence_ids = [row["resolved_occurrence_id"] for row in rows]
    if len(resolved_occurrence_ids) != len(set(resolved_occurrence_ids)):
        raise UpperKaraTargetUniverseResolutionV1Error(
            "resolved target occurrence IDs must be unique"
        )

    resolution_counts = {
        kind: sum(row["resolution"] == kind for row in rows)
        for kind in (
            REGISTRY_CREATURE_OCCURRENCE,
            EXACT_F130_CREATURE_OCCURRENCE,
            EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
            BOSS_OWNED_SUMMON_OCCURRENCE,
            UNRESOLVED_ELIGIBLE_EXTRA,
        )
    }
    eligible_rows = [row for row in rows if row["eligible_encounter_hostile"]]
    resolved_eligible_rows = [
        row
        for row in eligible_rows
        if row["resolution"]
        in {
            REGISTRY_CREATURE_OCCURRENCE,
            EXACT_F130_CREATURE_OCCURRENCE,
            BOSS_OWNED_SUMMON_OCCURRENCE,
        }
    ]
    unresolved_eligible_rows = [
        row
        for row in eligible_rows
        if row["resolution"] == UNRESOLVED_ELIGIBLE_EXTRA
    ]

    exact_joins = bool(
        source_join_exact
        and len(boss_guids) == 1
        and not missing_audit_rows
        and not registry_classification_mismatches
        and not registry_missing_reduction_evidence
        and not unknown_classification_guids
    )
    unresolved_guids = sorted(row["target_guid"] for row in unresolved_eligible_rows)
    authorized = bool(exact_joins and not unresolved_guids)
    blocking_reasons: list[str] = []
    if not source_join_exact:
        blocking_reasons.append("AUDIT_SOURCE_DOES_NOT_EXACTLY_JOIN_REGISTRY_ENCOUNTER")
    if len(boss_guids) != 1:
        blocking_reasons.append("EXACT_SINGLE_REGISTRY_BOSS_GUID_UNAVAILABLE")
    if missing_audit_rows:
        blocking_reasons.append("REGISTRY_TARGET_MISSING_FROM_AUDIT")
    if registry_classification_mismatches:
        blocking_reasons.append("REGISTRY_TARGET_AUDIT_CLASSIFICATION_MISMATCH")
    if registry_missing_reduction_evidence:
        blocking_reasons.append("REGISTRY_TARGET_MISSING_REDUCTION_EVIDENCE")
    if unknown_classification_guids:
        blocking_reasons.append("UNKNOWN_AUDIT_TARGET_CLASSIFICATION")
    if unresolved_guids:
        blocking_reasons.append("UNRESOLVED_ELIGIBLE_EXTRA_TARGETS_REMAIN")

    audit_summary = _mapping(raw_audit.get("summary"), "audit.summary")
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "source": {
            "instance_id": raw_encounter.get("instance_id"),
            "encounter_id": raw_encounter.get("encounter_id"),
            "pull_ref": raw_encounter.get("pull_ref"),
            "audit_source": deepcopy(dict(source)),
        },
        "exact_registry_boss_guid": exact_boss_guid,
        "targets": rows,
        "summary": {
            "raw_target_count": len(rows),
            "raw_registry_target_count": len(registry_guids),
            "raw_reduction_hostile_target_count": audit_summary.get(
                "reduction_hostile_target_count"
            ),
            "eligible_target_count": len(eligible_rows),
            "resolved_eligible_target_count": len(resolved_eligible_rows),
            "unresolved_eligible_target_count": len(unresolved_eligible_rows),
            "resolved_eligible_extra_count": (
                resolution_counts[BOSS_OWNED_SUMMON_OCCURRENCE]
                + resolution_counts[EXACT_F130_CREATURE_OCCURRENCE]
            ),
            "unresolved_eligible_extra_count": resolution_counts[
                UNRESOLVED_ELIGIBLE_EXTRA
            ],
            "excluded_exact_player_owned_count": resolution_counts[
                EXCLUDED_EXACT_PLAYER_OWNED_UNIT
            ],
            "registry_creature_occurrence_count": resolution_counts[
                REGISTRY_CREATURE_OCCURRENCE
            ],
            "exact_f130_creature_occurrence_count": resolution_counts[
                EXACT_F130_CREATURE_OCCURRENCE
            ],
            "boss_owned_summon_occurrence_count": resolution_counts[
                BOSS_OWNED_SUMMON_OCCURRENCE
            ],
        },
        "gate": {
            "source_field_matches": source_join,
            "source_join_exact": source_join_exact,
            "registry_boss_target_guids": boss_guids,
            "exact_single_registry_boss_guid_available": len(boss_guids) == 1,
            "registry_target_guids_missing_from_audit": missing_audit_rows,
            "registry_classification_mismatch_guids": registry_classification_mismatches,
            "registry_target_guids_missing_reduction_evidence": (
                registry_missing_reduction_evidence
            ),
            "unknown_audit_classification_guids": unknown_classification_guids,
            "resolved_occurrence_ids_unique": True,
            "unresolved_eligible_extra_target_guids": unresolved_guids,
            "exact_joins": exact_joins,
            "development_case_assembly_authorized": authorized,
            "blocking_reasons": blocking_reasons,
        },
        "scientific_boundaries": {
            "registry_and_owner_identity_joins_use_exact_guid_equality": True,
            "name_or_timing_used_for_identity_resolution": False,
            "boss_owned_f140_metadata_unit_entry_is_a_stable_template_id": False,
            "nonregistry_f130_template_requires_guid_metadata_entry_equality": True,
            "activity_or_death_used_as_policy_visible_state": False,
            "comparison_authorized": False,
        },
    }


__all__ = (
    "BOSS_OWNED_SUMMON_OCCURRENCE",
    "EXACT_F130_CREATURE_OCCURRENCE",
    "EXCLUDED_EXACT_PLAYER_OWNED_UNIT",
    "REGISTRY_CREATURE_OCCURRENCE",
    "SCHEMA",
    "STATUS",
    "UNRESOLVED_ELIGIBLE_EXTRA",
    "UpperKaraTargetUniverseResolutionV1Error",
    "resolve_upper_kara_target_universe_v1",
)
