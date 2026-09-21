"""Bind Incantagos target mechanics to a candidate damage-school action space.

The target-universe resolver answers *which exact occurrences existed*.  This
module answers the separate question of which of those occurrences a candidate
policy can damage.  Affinity compatibility is derived only from a stable
creature-template entry and an explicit damage-school intersection.  A unique
same-attempt registry display-name alias may classify an F140 summon's
development mechanic role, but never changes its identity; activity/death
timing never grants target permission.

The output is a development contract.  It deliberately does not authorize a
Cat/Contra/offline-expert comparison.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Collection, Mapping

from .upper_kara_target_universe_resolution_v1 import (
    BOSS_OWNED_SUMMON_OCCURRENCE,
    EXACT_F130_CREATURE_OCCURRENCE,
    EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
    REGISTRY_CREATURE_OCCURRENCE,
    SCHEMA as RESOLUTION_SCHEMA,
    STATUS as RESOLUTION_STATUS,
)


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_incantagos_target_actionability_contract/v1"
STATUS = "DEVELOPMENT_ACTIONABILITY_ONLY_NOT_COMPARISON_AUTHORIZED"

INCANTAGOS_ENTRY_ID = 61946

BOSS = "BOSS"
COMBAT_PRIORITY_ADD = "COMBAT_PRIORITY_ADD"
COLLATERAL_ONLY_ADD = "COLLATERAL_ONLY_ADD"
AFFINITY_ADD = "AFFINITY_ADD"
OPTIONAL_ACTIONABLE_ADD = "OPTIONAL_ACTIONABLE_ADD"
PLAYER_OWNED_EXCLUDED = "PLAYER_OWNED_EXCLUDED"
UNCLASSIFIED_ELIGIBLE_TARGET = "UNCLASSIFIED_ELIGIBLE_TARGET"

DIRECT_AND_COLLATERAL_CANDIDATE = "DIRECT_AND_COLLATERAL_CANDIDATE"
COLLATERAL_ONLY_CANDIDATE = "COLLATERAL_ONLY_CANDIDATE"
TEAM_ONLY_SCHOOL_INCOMPATIBLE = "TEAM_ONLY_SCHOOL_INCOMPATIBLE"
EXCLUDED_FROM_ENCOUNTER_TARGETS = "EXCLUDED_FROM_ENCOUNTER_TARGETS"
BLOCKED_UNCLASSIFIED = "BLOCKED_UNCLASSIFIED"

DAMAGE_SCHOOL_ORDER_V1 = (
    "PHYSICAL",
    "HOLY",
    "FIRE",
    "NATURE",
    "FROST",
    "SHADOW",
    "ARCANE",
)
_DAMAGE_SCHOOLS_V1 = frozenset(DAMAGE_SCHOOL_ORDER_V1)

# Stable creature-template entries, not Chronicle F140 unit numbers.
AFFINITY_ENTRY_ALLOWED_SCHOOLS_V1: Mapping[int, frozenset[str]] = {
    59982: frozenset({"ARCANE"}),  # Mana Affinity
    59983: frozenset({"SHADOW"}),  # Black Affinity
    59984: frozenset({"FROST"}),  # Blue Affinity
    59985: frozenset({"NATURE"}),  # Green Affinity
    59986: frozenset({"FIRE"}),  # Red Affinity
    59987: frozenset({"PHYSICAL"}),  # Crystal Affinity
}

# One-based file-line numbers in wowsims-turtle/assets/db_inputs/Spell.csv.
# These rows are spell IDs 51188--51193.  Keeping both coordinates avoids
# confusing the requested CSV-row evidence with spell identifiers.
AFFINITY_SPELL_CSV_EVIDENCE_V1: Mapping[int, Mapping[str, Any]] = {
    59982: {"csv_line_number": 25702, "spell_id": 51193, "school": "ARCANE"},
    59983: {"csv_line_number": 25701, "spell_id": 51192, "school": "SHADOW"},
    59984: {"csv_line_number": 25700, "spell_id": 51191, "school": "FROST"},
    59985: {"csv_line_number": 25699, "spell_id": 51190, "school": "NATURE"},
    59986: {"csv_line_number": 25698, "spell_id": 51189, "school": "FIRE"},
    59987: {"csv_line_number": 25697, "spell_id": 51188, "school": "PHYSICAL"},
}

# These are mechanic roles, not identity joins.  Registry rows already carry a
# stable creature-template entry.  An F140 occurrence may inherit one of these
# roles only through an exact, unique display-name alias built from registry
# rows in this same attempt; the alias never changes its resolved identity.
INCANTAGOS_ADD_ROLE_BY_ENTRY_V1: Mapping[int, str] = {
    59989: COMBAT_PRIORITY_ADD,  # Manascale Ley-Seeker
    59955: COLLATERAL_ONLY_ADD,  # Manascale Whelp
}
INCANTAGOS_ADD_SPELL_CSV_EVIDENCE_V1: Mapping[int, Mapping[str, Any]] = {
    59989: {"csv_line_number": 25687, "spell_id": 51178},
    59955: {"csv_line_number": 25688, "spell_id": 51179},
}


class UpperKaraIncantagosActionabilityContractV1Error(ValueError):
    """The resolution or candidate-school input is structurally malformed."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraIncantagosActionabilityContractV1Error(
            f"{label} must be an object"
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraIncantagosActionabilityContractV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            f"{label} must be a positive integer or null"
        )
    return value


def _candidate_schools(value: Collection[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "candidate_damage_schools must be a nonempty collection"
        )
    rows = tuple(value)
    if not rows:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "candidate_damage_schools must be a nonempty collection"
        )
    if any(not isinstance(row, str) or not row.strip() for row in rows):
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "candidate damage schools must be nonempty text"
        )
    normalized = tuple(row.strip().upper() for row in rows)
    unknown = sorted(set(normalized) - _DAMAGE_SCHOOLS_V1)
    if unknown:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            f"unknown candidate damage schools: {unknown}"
        )
    return tuple(school for school in DAMAGE_SCHOOL_ORDER_V1 if school in normalized)


def _ordered_schools(values: Collection[str]) -> list[str]:
    selected = set(values)
    return [school for school in DAMAGE_SCHOOL_ORDER_V1 if school in selected]


def _display_name(target: Mapping[str, Any], label: str) -> str | None:
    evidence = target.get("descriptive_evidence")
    if evidence is None:
        return None
    evidence = _mapping(evidence, f"{label}.descriptive_evidence")
    value = evidence.get("display_name")
    if value is None:
        return None
    if not isinstance(value, str):
        raise UpperKaraIncantagosActionabilityContractV1Error(
            f"{label}.descriptive_evidence.display_name must be text or null"
        )
    value = value.strip()
    return value or None


def _same_attempt_registry_name_aliases(
    raw_targets: list[object],
) -> tuple[dict[str, int], list[JSONMap]]:
    """Return only exact display names that map to one known stable entry."""

    candidates: dict[str, set[int]] = {}
    for index, value in enumerate(raw_targets):
        target = _mapping(value, f"resolution.targets[{index}]")
        if target.get("resolution") != REGISTRY_CREATURE_OCCURRENCE:
            continue
        entry = _optional_positive_int(
            target.get("stable_template_creature_entry_id"),
            f"resolution.targets[{index}].stable_template_creature_entry_id",
        )
        name = _display_name(target, f"resolution.targets[{index}]")
        if name is not None:
            candidates.setdefault(name, set()).add(entry)

    aliases = {
        name: next(iter(entries))
        for name, entries in candidates.items()
        if len(entries) == 1
        and next(iter(entries)) in INCANTAGOS_ADD_ROLE_BY_ENTRY_V1
    }
    evidence = [
        {
            "registry_display_name": name,
            "stable_template_creature_entry_id": entry,
            "mechanic_role": INCANTAGOS_ADD_ROLE_BY_ENTRY_V1[entry],
            "scope": "SAME_ATTEMPT_DEVELOPMENT_MECHANIC_ROLE_ONLY_NOT_IDENTITY",
        }
        for name, entry in sorted(aliases.items())
    ]
    return aliases, evidence


def build_incantagos_target_actionability_contract_v1(
    resolution: Mapping[str, Any],
    *,
    candidate_damage_schools: Collection[str],
) -> JSONMap:
    """Classify exact Incantagos occurrences for one candidate action space.

    An unknown eligible mechanic returns an auditable blocked contract with no
    released direct/collateral lists.  Structural input defects raise instead.
    """

    raw = _mapping(resolution, "resolution")
    if raw.get("schema") != RESOLUTION_SCHEMA or raw.get("status") != RESOLUTION_STATUS:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "unexpected target-universe resolution schema or status"
        )
    resolution_gate = _mapping(raw.get("gate"), "resolution.gate")
    if resolution_gate.get("development_case_assembly_authorized") is not True:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "target-universe resolution is not authorized for development assembly"
        )
    boundaries = _mapping(raw.get("scientific_boundaries"), "resolution.scientific_boundaries")
    if boundaries.get("comparison_authorized") is not False:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "target-universe resolution must remain comparison-ineligible"
        )

    candidate_schools = _candidate_schools(candidate_damage_schools)
    candidate_school_set = frozenset(candidate_schools)
    exact_boss_guid = _text(raw.get("exact_registry_boss_guid"), "exact_registry_boss_guid")
    source = _mapping(raw.get("source"), "resolution.source")
    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "resolution.targets must be a nonempty list"
        )
    registry_name_aliases, registry_name_alias_evidence = (
        _same_attempt_registry_name_aliases(raw_targets)
    )

    target_guids: set[str] = set()
    occurrence_ids: set[str] = set()
    rows: list[JSONMap] = []
    direct_candidates: list[str] = []
    collateral_candidates: list[str] = []
    combat_priority_adds: list[str] = []
    collateral_only_adds: list[str] = []
    optional_actionable_adds: list[str] = []
    team_only: list[str] = []
    full_environment: list[str] = []
    excluded_player_owned: list[str] = []
    unclassified: list[str] = []
    boss_occurrences: list[str] = []

    allowed_identity_kinds = {
        REGISTRY_CREATURE_OCCURRENCE,
        EXACT_F130_CREATURE_OCCURRENCE,
        BOSS_OWNED_SUMMON_OCCURRENCE,
        EXCLUDED_EXACT_PLAYER_OWNED_UNIT,
    }

    for index, value in enumerate(raw_targets):
        target = _mapping(value, f"resolution.targets[{index}]")
        guid = _text(target.get("target_guid"), f"resolution.targets[{index}].target_guid")
        occurrence_id = _text(
            target.get("resolved_occurrence_id"),
            f"resolution.targets[{index}].resolved_occurrence_id",
        )
        if guid in target_guids:
            raise UpperKaraIncantagosActionabilityContractV1Error(
                f"resolution repeats target GUID {guid}"
            )
        if occurrence_id in occurrence_ids:
            raise UpperKaraIncantagosActionabilityContractV1Error(
                f"resolution repeats occurrence ID {occurrence_id}"
            )
        target_guids.add(guid)
        occurrence_ids.add(occurrence_id)

        identity_kind = _text(
            target.get("resolution"), f"resolution.targets[{index}].resolution"
        )
        if identity_kind not in allowed_identity_kinds:
            raise UpperKaraIncantagosActionabilityContractV1Error(
                f"unsupported resolved identity kind {identity_kind}"
            )
        eligible = target.get("eligible_encounter_hostile")
        if not isinstance(eligible, bool):
            raise UpperKaraIncantagosActionabilityContractV1Error(
                "resolved target eligibility must be boolean"
            )
        stable_entry = _optional_positive_int(
            target.get("stable_template_creature_entry_id"),
            f"resolution.targets[{index}].stable_template_creature_entry_id",
        )
        display_name = _display_name(target, f"resolution.targets[{index}]")
        aliased_role_entry = (
            registry_name_aliases.get(display_name)
            if identity_kind == BOSS_OWNED_SUMMON_OCCURRENCE
            and display_name is not None
            else None
        )
        direct_role_entry = (
            stable_entry
            if stable_entry in INCANTAGOS_ADD_ROLE_BY_ENTRY_V1
            else aliased_role_entry
        )
        name_alias_used = (
            direct_role_entry is not None
            and stable_entry not in INCANTAGOS_ADD_ROLE_BY_ENTRY_V1
            and aliased_role_entry == direct_role_entry
        )

        if identity_kind == EXCLUDED_EXACT_PLAYER_OWNED_UNIT:
            if eligible:
                raise UpperKaraIncantagosActionabilityContractV1Error(
                    "exact player-owned exclusions cannot be eligible encounter hostiles"
                )
            mechanic = PLAYER_OWNED_EXCLUDED
            target_gate_role = PLAYER_OWNED_EXCLUDED
            actionability = EXCLUDED_FROM_ENCOUNTER_TARGETS
            allowed_schools: frozenset[str] | None = None
            school_intersection: frozenset[str] = frozenset()
            excluded_player_owned.append(occurrence_id)
        else:
            if not eligible:
                raise UpperKaraIncantagosActionabilityContractV1Error(
                    "non-player resolved encounter targets must be eligible"
                )
            full_environment.append(occurrence_id)
            if guid == exact_boss_guid and stable_entry == INCANTAGOS_ENTRY_ID:
                mechanic = BOSS
                target_gate_role = BOSS
                allowed_schools = None
                school_intersection = candidate_school_set
                actionability = DIRECT_AND_COLLATERAL_CANDIDATE
                boss_occurrences.append(occurrence_id)
            elif stable_entry in AFFINITY_ENTRY_ALLOWED_SCHOOLS_V1:
                mechanic = AFFINITY_ADD
                allowed_schools = AFFINITY_ENTRY_ALLOWED_SCHOOLS_V1[stable_entry]
                school_intersection = candidate_school_set & allowed_schools
                actionability = (
                    DIRECT_AND_COLLATERAL_CANDIDATE
                    if school_intersection
                    else TEAM_ONLY_SCHOOL_INCOMPATIBLE
                )
                target_gate_role = (
                    OPTIONAL_ACTIONABLE_ADD
                    if school_intersection
                    else TEAM_ONLY_SCHOOL_INCOMPATIBLE
                )
            elif direct_role_entry in INCANTAGOS_ADD_ROLE_BY_ENTRY_V1:
                mechanic = INCANTAGOS_ADD_ROLE_BY_ENTRY_V1[direct_role_entry]
                target_gate_role = mechanic
                allowed_schools = None
                school_intersection = candidate_school_set
                actionability = (
                    DIRECT_AND_COLLATERAL_CANDIDATE
                    if mechanic == COMBAT_PRIORITY_ADD
                    else COLLATERAL_ONLY_CANDIDATE
                )
            else:
                mechanic = UNCLASSIFIED_ELIGIBLE_TARGET
                target_gate_role = UNCLASSIFIED_ELIGIBLE_TARGET
                allowed_schools = None
                school_intersection = frozenset()
                actionability = BLOCKED_UNCLASSIFIED
                unclassified.append(occurrence_id)

            if actionability == DIRECT_AND_COLLATERAL_CANDIDATE:
                direct_candidates.append(occurrence_id)
                collateral_candidates.append(occurrence_id)
                if target_gate_role == COMBAT_PRIORITY_ADD:
                    combat_priority_adds.append(occurrence_id)
                elif target_gate_role == OPTIONAL_ACTIONABLE_ADD:
                    optional_actionable_adds.append(occurrence_id)
            elif actionability == COLLATERAL_ONLY_CANDIDATE:
                collateral_candidates.append(occurrence_id)
                collateral_only_adds.append(occurrence_id)
            elif actionability == TEAM_ONLY_SCHOOL_INCOMPATIBLE:
                team_only.append(occurrence_id)

        if stable_entry in AFFINITY_SPELL_CSV_EVIDENCE_V1:
            evidence = deepcopy(dict(AFFINITY_SPELL_CSV_EVIDENCE_V1[stable_entry]))
        elif direct_role_entry in INCANTAGOS_ADD_SPELL_CSV_EVIDENCE_V1:
            evidence = deepcopy(
                dict(INCANTAGOS_ADD_SPELL_CSV_EVIDENCE_V1[direct_role_entry])
            )
        else:
            evidence = None
        rows.append(
            {
                "target_guid": guid,
                "resolved_occurrence_id": occurrence_id,
                "identity_resolution": identity_kind,
                "eligible_encounter_hostile": eligible,
                "stable_template_creature_entry_id": stable_entry,
                "mechanic_classification": mechanic,
                "target_gate_role": target_gate_role,
                "same_attempt_registry_name_alias_entry_id": (
                    direct_role_entry if name_alias_used else None
                ),
                "allowed_damage_schools": (
                    _ordered_schools(allowed_schools)
                    if allowed_schools is not None
                    else None
                ),
                "candidate_school_intersection": _ordered_schools(school_intersection),
                "candidate_actionability": actionability,
                "spell_csv_evidence": evidence,
                "policy_semantics": {
                    "name_used_for_mechanic_classification": name_alias_used,
                    "name_used_for_identity_resolution": False,
                    "name_alias_scope_is_development_mechanic_role_only": (
                        name_alias_used
                    ),
                    "activity_or_death_timing_used_for_actionability": False,
                    "actionability_uses_stable_template_entry": mechanic == AFFINITY_ADD,
                    "actionability_uses_explicit_school_intersection": mechanic == AFFINITY_ADD,
                },
            }
        )

    if exact_boss_guid not in target_guids:
        raise UpperKaraIncantagosActionabilityContractV1Error(
            "exact registry boss GUID has no resolved target row"
        )

    exact_single_incantagos = len(boss_occurrences) == 1
    authorized = exact_single_incantagos and not unclassified
    blocking_reasons: list[str] = []
    if not exact_single_incantagos:
        blocking_reasons.append("EXACT_SINGLE_INCANTAGOS_BOSS_OCCURRENCE_UNAVAILABLE")
    if unclassified:
        blocking_reasons.append("UNCLASSIFIED_ELIGIBLE_TARGET_MECHANICS_REMAIN")

    # Never release a partial actionability list: a downstream target gate must
    # not silently ignore an eligible occurrence whose mechanics are unknown.
    if not authorized:
        released_direct: list[str] = []
        released_collateral: list[str] = []
        released_combat_priority: list[str] = []
        released_collateral_only: list[str] = []
        released_optional_actionable: list[str] = []
    else:
        released_direct = direct_candidates
        released_collateral = collateral_candidates
        released_combat_priority = combat_priority_adds
        released_collateral_only = collateral_only_adds
        released_optional_actionable = optional_actionable_adds

    observed_affinity_entries = sorted(
        {
            row["stable_template_creature_entry_id"]
            for row in rows
            if row["mechanic_classification"] == AFFINITY_ADD
        }
    )
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "source": deepcopy(dict(source)),
        "candidate_damage_schools": list(candidate_schools),
        "boss_occurrence_id": boss_occurrences[0] if exact_single_incantagos else None,
        "full_environment_occurrence_ids": full_environment,
        "direct_candidate_occurrence_ids": released_direct,
        "collateral_candidate_occurrence_ids": released_collateral,
        "combat_priority_add_occurrence_ids": released_combat_priority,
        "collateral_only_occurrence_ids": released_collateral_only,
        "optional_actionable_occurrence_ids": released_optional_actionable,
        "team_only_occurrence_ids": team_only,
        "excluded_player_owned_occurrence_ids": excluded_player_owned,
        "targets": rows,
        "mechanic_evidence": {
            "source_path": "wowsims-turtle/assets/db_inputs/Spell.csv",
            "one_based_file_line_numbers": list(range(25697, 25703)),
            "spell_ids": list(range(51188, 51194)),
            "incantagos_add_summon_rows": [
                {
                    "stable_template_creature_entry_id": entry,
                    "mechanic_role": INCANTAGOS_ADD_ROLE_BY_ENTRY_V1[entry],
                    **deepcopy(dict(evidence)),
                }
                for entry, evidence in INCANTAGOS_ADD_SPELL_CSV_EVIDENCE_V1.items()
            ],
            "same_attempt_registry_display_name_aliases": (
                registry_name_alias_evidence
            ),
            "selected_exact_attempt": {
                "instance_id": source.get("instance_id"),
                "encounter_id": source.get("encounter_id"),
                "pull_ref": source.get("pull_ref"),
                "observed_affinity_template_entry_ids": observed_affinity_entries,
            },
        },
        "gate": {
            "source_resolution_development_authorized": True,
            "exact_single_incantagos_boss_occurrence": exact_single_incantagos,
            "eligible_target_mechanics_fully_classified": not unclassified,
            "unclassified_eligible_occurrence_ids": unclassified,
            "candidate_lists_released": authorized,
            "team_only_targets_block_boss": False,
            "collateral_only_targets_block_boss": False,
            "optional_actionable_targets_block_boss": False,
            "development_target_actionability_authorized": authorized,
            "blocking_reasons": blocking_reasons,
        },
        "comparison_authorized": False,
        "scientific_boundaries": {
            "comparison_authorized": False,
            "spell_csv_and_selected_attempt_are_development_evidence": True,
            "observed_future_activity_or_death_used_for_policy_actionability": False,
            "same_attempt_registry_display_name_alias_used_for_mechanics": any(
                row["policy_semantics"]["name_used_for_mechanic_classification"]
                for row in rows
            ),
            "display_names_used_for_identity_resolution": False,
            "display_name_alias_scope": (
                "DEVELOPMENT_MECHANIC_ROLE_ONLY_NOT_IDENTITY"
            ),
            "affinity_actionability_requires_stable_template_entry": True,
            "affinity_actionability_requires_explicit_school_intersection": True,
        },
    }


__all__ = (
    "AFFINITY_ADD",
    "AFFINITY_ENTRY_ALLOWED_SCHOOLS_V1",
    "AFFINITY_SPELL_CSV_EVIDENCE_V1",
    "BLOCKED_UNCLASSIFIED",
    "BOSS",
    "COLLATERAL_ONLY_ADD",
    "COLLATERAL_ONLY_CANDIDATE",
    "COMBAT_PRIORITY_ADD",
    "DIRECT_AND_COLLATERAL_CANDIDATE",
    "EXCLUDED_FROM_ENCOUNTER_TARGETS",
    "INCANTAGOS_ENTRY_ID",
    "INCANTAGOS_ADD_ROLE_BY_ENTRY_V1",
    "INCANTAGOS_ADD_SPELL_CSV_EVIDENCE_V1",
    "OPTIONAL_ACTIONABLE_ADD",
    "PLAYER_OWNED_EXCLUDED",
    "SCHEMA",
    "STATUS",
    "TEAM_ONLY_SCHOOL_INCOMPATIBLE",
    "UNCLASSIFIED_ELIGIBLE_TARGET",
    "UpperKaraIncantagosActionabilityContractV1Error",
    "build_incantagos_target_actionability_contract_v1",
)
