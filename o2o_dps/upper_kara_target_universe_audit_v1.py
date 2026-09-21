"""Audit one Upper Kara encounter's complete hostile-target universe.

The route registry and the exact Chronicle reduction are independent views of
the same encounter.  This module joins them by exact GUID and makes every
target-universe mismatch explicit before a development simulator case is
assembled.  Activity and death are retained as descriptive evidence only;
they never become policy-visible phase, target permission, or attackability.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .chronicle_external_compact_target_reducer_v1 import SCHEMA as REDUCTION_SCHEMA


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_target_universe_audit/v1"
STATUS = "TARGET_UNIVERSE_AUDITED"
REGISTRY_DECLARED = "REGISTRY_DECLARED"
UNRESOLVED_EXTRA_DAMAGED_HOSTILE = "UNRESOLVED_EXTRA_DAMAGED_HOSTILE"
EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED = "EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED"


class UpperKaraTargetUniverseAuditV1Error(ValueError):
    """The registry or compact hostile-target evidence is malformed."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraTargetUniverseAuditV1Error(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraTargetUniverseAuditV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpperKaraTargetUniverseAuditV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    result = _nonnegative_int(value, label)
    if result == 0:
        raise UpperKaraTargetUniverseAuditV1Error(
            f"{label} must be a positive integer"
        )
    return result


def _optional_reference(value: object, label: str) -> JSONMap | None:
    if value is None:
        return None
    return deepcopy(dict(_mapping(value, label)))


def _registry_targets(encounter: Mapping[str, Any]) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    raw_targets = encounter.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise UpperKaraTargetUniverseAuditV1Error(
            "encounter.targets must be a nonempty array"
        )
    ordered: list[str] = []
    by_guid: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(raw_targets):
        row = _mapping(value, f"encounter.targets[{index}]")
        guid = _text(row.get("target_guid"), f"encounter.targets[{index}].target_guid")
        if guid in by_guid:
            raise UpperKaraTargetUniverseAuditV1Error(
                f"encounter.targets repeats target GUID {guid}"
            )
        ordered.append(guid)
        by_guid[guid] = row
    return ordered, by_guid


def _reduction_targets(reduction: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_targets = reduction.get("hostile_targets")
    if not isinstance(raw_targets, list):
        raise UpperKaraTargetUniverseAuditV1Error(
            "reduction.hostile_targets must be an array"
        )
    by_guid: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(raw_targets):
        row = _mapping(value, f"reduction.hostile_targets[{index}]")
        guid = _text(
            row.get("target_guid"),
            f"reduction.hostile_targets[{index}].target_guid",
        )
        if guid in by_guid:
            raise UpperKaraTargetUniverseAuditV1Error(
                f"reduction.hostile_targets repeats target GUID {guid}"
            )
        by_guid[guid] = row
    return by_guid


def _metadata_identity(
    metadata_units: Mapping[str, Any], guid: str
) -> tuple[int | None, str | None, str | None]:
    value = metadata_units.get(guid)
    if value is None:
        return None, None, None
    unit = _mapping(value, f"metadata_units[{guid}]")
    return (
        _optional_positive_int(unit.get("entry"), f"metadata_units[{guid}].entry"),
        _optional_text(unit.get("name"), f"metadata_units[{guid}].name"),
        _optional_text(unit.get("owner"), f"metadata_units[{guid}].owner"),
    )


def _metadata_player_rows(
    metadata_players: Mapping[str, Any] | None,
) -> tuple[bool, dict[str, Mapping[str, Any]]]:
    if metadata_players is None:
        return False, {}
    raw = _mapping(metadata_players, "metadata_players")
    result: dict[str, Mapping[str, Any]] = {}
    for key, value in raw.items():
        guid = _text(key, "metadata_players GUID")
        result[guid] = _mapping(value, f"metadata_players[{guid}]")
    return True, result


def _registry_identity(
    target: Mapping[str, Any] | None, guid: str
) -> tuple[int | None, str | None]:
    if target is None:
        return None, None
    name = target.get("display_name")
    if name is None:
        name = target.get("creature_name")
    return (
        _optional_positive_int(
            target.get("creature_entry_id"),
            f"registry target {guid}.creature_entry_id",
        ),
        _optional_text(name, f"registry target {guid}.display_name"),
    )


def _target_evidence(
    reduction_target: Mapping[str, Any] | None, guid: str
) -> JSONMap:
    if reduction_target is None:
        return {
            "present_in_external_reduction": False,
            "activity": None,
            "death": None,
            "incoming_damage": None,
            "post_first_death_activity": None,
            "first_direct_friendly_player_action": None,
            "first_direct_friendly_player_damage": None,
            "first_direct_friendly_player_positive_damage": None,
            "has_target_side_damage_event": False,
            "has_positive_incoming_damage": False,
            "has_direct_friendly_player_target_action": False,
            "qualifies_as_damaged_or_directly_acted_target": False,
        }

    activity = _mapping(reduction_target.get("activity"), f"target {guid}.activity")
    first = _nonnegative_int(
        activity.get("first_relevant_offset_ms"),
        f"target {guid}.activity.first_relevant_offset_ms",
    )
    last = _nonnegative_int(
        activity.get("last_relevant_offset_ms"),
        f"target {guid}.activity.last_relevant_offset_ms",
    )
    if last < first:
        raise UpperKaraTargetUniverseAuditV1Error(
            f"target {guid} activity offsets are reversed"
        )

    incoming = _mapping(
        reduction_target.get("incoming_damage"), f"target {guid}.incoming_damage"
    )
    positive_sum = _nonnegative_int(
        incoming.get("positive_sum"), f"target {guid}.incoming_damage.positive_sum"
    )
    positive_count = _nonnegative_int(
        incoming.get("positive_event_count"),
        f"target {guid}.incoming_damage.positive_event_count",
    )
    unavailable_count = _nonnegative_int(
        incoming.get("amount_unavailable_event_count"),
        f"target {guid}.incoming_damage.amount_unavailable_event_count",
    )
    post_death = _mapping(
        reduction_target.get("post_first_death_activity"),
        f"target {guid}.post_first_death_activity",
    )
    post_damage = _mapping(
        post_death.get("incoming_damage"),
        f"target {guid}.post_first_death_activity.incoming_damage",
    )
    post_positive_sum = _nonnegative_int(
        post_damage.get("positive_sum"),
        f"target {guid}.post_first_death_activity.incoming_damage.positive_sum",
    )
    post_positive_count = _nonnegative_int(
        post_damage.get("positive_event_count"),
        f"target {guid}.post_first_death_activity.incoming_damage.positive_event_count",
    )
    post_unavailable_count = _nonnegative_int(
        post_damage.get("amount_unavailable_event_count"),
        f"target {guid}.post_first_death_activity.incoming_damage.amount_unavailable_event_count",
    )

    action = _optional_reference(
        reduction_target.get("first_direct_friendly_player_action"),
        f"target {guid}.first_direct_friendly_player_action",
    )
    damage = _optional_reference(
        reduction_target.get("first_direct_friendly_player_damage"),
        f"target {guid}.first_direct_friendly_player_damage",
    )
    positive_damage = _optional_reference(
        reduction_target.get("first_direct_friendly_player_positive_damage"),
        f"target {guid}.first_direct_friendly_player_positive_damage",
    )
    has_damage_event = bool(
        positive_count
        or unavailable_count
        or post_positive_count
        or post_unavailable_count
        or damage is not None
    )
    has_positive_damage = bool(
        positive_sum or post_positive_sum or positive_damage is not None
    )
    has_direct_action = bool(action is not None or damage is not None or positive_damage is not None)

    return {
        "present_in_external_reduction": True,
        "activity": deepcopy(dict(activity)),
        "death": deepcopy(
            dict(_mapping(reduction_target.get("death"), f"target {guid}.death"))
        ),
        "incoming_damage": deepcopy(dict(incoming)),
        "post_first_death_activity": deepcopy(dict(post_death)),
        "first_direct_friendly_player_action": action,
        "first_direct_friendly_player_damage": damage,
        "first_direct_friendly_player_positive_damage": positive_damage,
        "has_target_side_damage_event": has_damage_event,
        "has_positive_incoming_damage": has_positive_damage,
        "has_direct_friendly_player_target_action": has_direct_action,
        "qualifies_as_damaged_or_directly_acted_target": bool(
            has_damage_event or has_positive_damage or has_direct_action
        ),
    }


def audit_upper_kara_target_universe_v1(
    encounter: Mapping[str, Any],
    reduction: Mapping[str, Any],
    *,
    metadata_units: Mapping[str, Any] | None = None,
    metadata_players: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Join registry and reduced hostile GUIDs and issue an assembly gate."""

    raw_encounter = _mapping(encounter, "encounter")
    raw_reduction = _mapping(reduction, "reduction")
    if raw_reduction.get("schema") != REDUCTION_SCHEMA:
        raise UpperKaraTargetUniverseAuditV1Error(
            "unexpected compact target reduction schema"
        )
    if raw_reduction.get("status") != "DESCRIPTIVE_OUTCOME_ONLY":
        raise UpperKaraTargetUniverseAuditV1Error(
            "compact target reduction is not descriptive outcome evidence"
        )
    source = _mapping(
        raw_reduction.get("source_selection"), "reduction.source_selection"
    )
    for field in ("instance_id", "encounter_id"):
        if source.get(field) != raw_encounter.get(field):
            raise UpperKaraTargetUniverseAuditV1Error(
                f"reduction {field} differs from the registry encounter"
            )
    units = {} if metadata_units is None else _mapping(metadata_units, "metadata_units")
    players_supplied, player_rows = _metadata_player_rows(metadata_players)

    ordered_registry_guids, registry_by_guid = _registry_targets(raw_encounter)
    reduction_by_guid = _reduction_targets(raw_reduction)
    registry_guids = set(registry_by_guid)
    reduction_guids = set(reduction_by_guid)
    missing = sorted(registry_guids - reduction_guids)
    extra = sorted(reduction_guids - registry_guids)

    target_rows: list[JSONMap] = []
    excluded_player_controlled_guids: set[str] = set()
    for guid in [*ordered_registry_guids, *extra]:
        registry_target = registry_by_guid.get(guid)
        reduction_target = reduction_by_guid.get(guid)
        registry_entry, registry_name = _registry_identity(registry_target, guid)
        metadata_entry, metadata_name, metadata_owner = _metadata_identity(units, guid)
        owner_matches_player = bool(
            players_supplied
            and metadata_owner is not None
            and metadata_owner in player_rows
        )
        if owner_matches_player:
            excluded_player_controlled_guids.add(guid)
        evidence = _target_evidence(reduction_target, guid)
        target_rows.append(
            {
                "target_guid": guid,
                "classification": (
                    EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED
                    if owner_matches_player
                    else (
                        REGISTRY_DECLARED
                        if registry_target is not None
                        else UNRESOLVED_EXTRA_DAMAGED_HOSTILE
                    )
                ),
                "creature_entry_id": (
                    registry_entry if registry_entry is not None else metadata_entry
                ),
                "display_name": (
                    registry_name if registry_name is not None else metadata_name
                ),
                "identity_evidence": {
                    "registry_creature_entry_id": registry_entry,
                    "registry_display_name": registry_name,
                    "metadata_creature_entry_id": metadata_entry,
                    "metadata_display_name": metadata_name,
                },
                "owner_evidence": {
                    "metadata_owner_guid": metadata_owner,
                    "metadata_players_supplied": players_supplied,
                    "owner_exactly_matches_metadata_player_guid": owner_matches_player,
                    "matched_metadata_player": (
                        deepcopy(dict(player_rows[metadata_owner]))
                        if owner_matches_player and metadata_owner is not None
                        else None
                    ),
                    "exclusion_basis": (
                        "METADATA_UNIT_OWNER_EXACT_METADATA_PLAYER_GUID"
                        if owner_matches_player
                        else None
                    ),
                    "name_or_creature_entry_used_for_exclusion": False,
                },
                "registry_occurrence_id": (
                    registry_target.get("occurrence_id")
                    if registry_target is not None
                    else None
                ),
                "evidence": evidence,
                "policy_semantics": {
                    "observed_activity_grants_target_permission": False,
                    "observed_death_grants_target_permission": False,
                    "policy_visible_phase_inferred": False,
                    "policy_visible_attackability_inferred": False,
                },
            }
        )

    extra_positive = [
        row["target_guid"]
        for row in target_rows
        if row["classification"] == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
        and row["evidence"]["has_positive_incoming_damage"]
    ]
    extra_direct = [
        row["target_guid"]
        for row in target_rows
        if row["classification"] == UNRESOLVED_EXTRA_DAMAGED_HOSTILE
        and row["evidence"]["has_direct_friendly_player_target_action"]
    ]
    raw_extra_positive = [
        row["target_guid"]
        for row in target_rows
        if row["target_guid"] in extra
        and row["evidence"]["has_positive_incoming_damage"]
    ]
    raw_registry_guids = registry_guids
    raw_reduction_guids = reduction_guids
    eligible_registry_guids = raw_registry_guids - excluded_player_controlled_guids
    eligible_reduction_guids = raw_reduction_guids - excluded_player_controlled_guids
    eligible_missing = sorted(eligible_registry_guids - eligible_reduction_guids)
    eligible_extra = sorted(eligible_reduction_guids - eligible_registry_guids)
    raw_exact_match = not missing and not extra
    eligible_exact_match = not eligible_missing and not eligible_extra
    blocking_reasons: list[str] = []
    if not eligible_exact_match:
        blocking_reasons.append("REDUCTION_TARGET_UNIVERSE_DIFFERS_FROM_REGISTRY")
    if extra_positive:
        blocking_reasons.append("EXTRA_POSITIVE_DAMAGE_TARGETS_REQUIRE_REGISTRY_RESOLUTION")

    return {
        "schema": SCHEMA,
        "status": STATUS,
        "source": {
            "instance_id": raw_encounter.get("instance_id"),
            "encounter_id": raw_encounter.get("encounter_id"),
            "pull_ref": raw_encounter.get("pull_ref"),
            "external_reduction": deepcopy(dict(source)),
        },
        "targets": target_rows,
        "summary": {
            "registry_target_count": len(registry_guids),
            "reduction_hostile_target_count": len(reduction_guids),
            "union_target_count": len(target_rows),
            "registry_missing_from_reduction_count": len(missing),
            "extra_reduction_target_count": len(extra),
            "exact_player_controlled_unit_excluded_count": len(
                excluded_player_controlled_guids
            ),
            "eligible_registry_target_count": len(eligible_registry_guids),
            "eligible_reduction_hostile_target_count": len(
                eligible_reduction_guids
            ),
            "eligible_extra_reduction_target_count": len(eligible_extra),
            "extra_positive_damage_target_count": len(raw_extra_positive),
            "eligible_extra_positive_damage_target_count": len(extra_positive),
            "extra_direct_player_target_count": len(extra_direct),
        },
        "gate": {
            "registry_missing_from_reduction_target_guids": missing,
            "extra_reduction_target_guids": extra,
            "raw_target_universe_matches_registry_exactly": raw_exact_match,
            "exact_player_controlled_unit_excluded_guids": sorted(
                excluded_player_controlled_guids
            ),
            "eligible_registry_missing_from_reduction_target_guids": eligible_missing,
            "eligible_extra_reduction_target_guids": eligible_extra,
            "raw_extra_positive_damage_target_guids": raw_extra_positive,
            "extra_positive_damage_target_guids": extra_positive,
            "extra_direct_player_target_guids": extra_direct,
            "target_universe_matches_registry_exactly": raw_exact_match,
            "eligible_target_universe_matches_registry_exactly": eligible_exact_match,
            "development_case_assembly_authorized": eligible_exact_match,
            "blocking_reasons": blocking_reasons,
        },
        "scientific_boundaries": {
            "activity_and_death_are_descriptive_outcomes_only": True,
            "observed_activity_used_as_policy_visible_phase": False,
            "observed_activity_used_as_target_permission": False,
            "observed_death_used_as_target_permission": False,
            "attackability_inferred": False,
            "player_controlled_unit_exclusion_requires_exact_metadata_owner_join": True,
            "name_or_creature_entry_used_for_player_controlled_exclusion": False,
            "comparison_authorized": False,
        },
    }


__all__ = (
    "EXACT_PLAYER_CONTROLLED_UNIT_EXCLUDED",
    "REGISTRY_DECLARED",
    "SCHEMA",
    "STATUS",
    "UNRESOLVED_EXTRA_DAMAGED_HOSTILE",
    "UpperKaraTargetUniverseAuditV1Error",
    "audit_upper_kara_target_universe_v1",
)
