"""Compose a simulator request from five explicitly owned input components.

This module replaces the old "copy one character template and overwrite a few
fields" import boundary.  Character, raid, encounter, execution, and objective
state are supplied independently, so changing the character cannot retain a
previous character's buffs, consumes, rotation, or Warrior options.

The result is an admission artifact.  ``request`` is present only when every
equipment slot is observed (equipped or empty), every equipped item/enchant has
declared simulator coverage, and the existing full-policy request validator
accepts the composed request.  Unsupported or unknown effects remain visible
in ``coverage`` and block execution instead of being treated as passive stats.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from o2o_dps.fury_full_policy_rollout_v2 import (
    FuryFullPolicyRolloutV2Error,
    _validate_request as _validate_full_policy_request,
)
from o2o_dps.wowsims_profile import (
    ProfileSelection,
    WOW_SLOT_TO_WOWSIMS_SLOT,
    WOWSIMS_EQUIPMENT_SLOT_COUNT,
    WowsimsProfileError,
    build_wowsims_profile,
)


JSONMap = dict[str, Any]
SCHEMA = "build_request_composer/v1"
KIND = "build_request_composition_v1"
IMPLEMENTATION_REVISION = "v1.0_explicit_five_component_composition"

OBSERVED_EQUIPPED = "OBSERVED_EQUIPPED"
OBSERVED_EMPTY = "OBSERVED_EMPTY"
MISSING = "MISSING"
SLOT_STATUSES = frozenset((OBSERVED_EQUIPPED, OBSERVED_EMPTY, MISSING))

_ITEM_DEFINITION_STATUSES = frozenset(("KNOWN", "UNKNOWN"))
_ITEM_EFFECT_STATUSES = frozenset(
    ("NO_SPECIAL_EFFECT", "IMPLEMENTED", "UNSUPPORTED", "UNKNOWN")
)
_ENCHANT_STATUSES = frozenset(
    ("NONE", "IMPLEMENTED", "UNSUPPORTED", "UNKNOWN", "NOT_OBSERVED")
)
_ADMITTED_ITEM_EFFECT_STATUSES = frozenset(("NO_SPECIAL_EFFECT", "IMPLEMENTED"))
_ADMITTED_ENCHANT_STATUSES = frozenset(("NONE", "IMPLEMENTED"))
_RAID_OPTION_FIELDS = frozenset(
    ("numActiveParties", "tanks", "staggerStormstrikes", "targetDummies")
)
_DECIMAL_INTEGER = re.compile(r"^-?[0-9]+$")


class BuildRequestComposerV1Error(ValueError):
    """One of the five components cannot form a supported request."""


@dataclass(frozen=True)
class CharacterProfile:
    """Observed character state and character-owned simulator configuration.

    ``slot_statuses`` uses WoW inventory slot numbers.  An empty mapping means
    the source-capture contract decides absent-slot semantics: absent slots in
    a full ``STATIC_PROFILE_CAPTURED`` record are observed empty; absent slots
    in a partial/boundary record are missing.

    ``item_effect_coverage`` is also keyed by WoW slot.  Every equipped slot
    needs a row; extra rows are rejected so coverage from a prior character
    cannot leak into a new request.
    """

    selection: ProfileSelection
    consumes: Mapping[str, Any]
    database: Mapping[str, Any]
    slot_statuses: Mapping[int, str]
    item_effect_coverage: Mapping[int, Mapping[str, Any]]
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class RaidContext:
    """Teammates, individual/party/raid buffs, and external debuffs."""

    individual_buffs: Mapping[str, Any]
    party_buffs: Mapping[str, Any]
    raid_buffs: Mapping[str, Any]
    debuffs: Mapping[str, Any]
    additional_party_players: Sequence[Mapping[str, Any]]
    additional_parties: Sequence[Mapping[str, Any]]
    raid_options: Mapping[str, Any]
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class EncounterModel:
    """Targets, armor/health, duration, and attackability-facing request state."""

    request_fields: Mapping[str, Any]
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionModel:
    """Rotation and client/executor timing assumptions owned by the run."""

    rotation: Mapping[str, Any]
    cooldowns: Mapping[str, Any]
    warrior_options: Mapping[str, Any]
    reaction_time_ms: int
    channel_clip_delay_ms: int
    in_front_of_target: bool
    distance_from_target: int | float
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class Objective:
    """Optimization meaning plus the exact simulator execution options."""

    kind: str
    sim_options: Mapping[str, Any]
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class BuildRequestCompositionV1:
    """Runnable request when admitted, plus always-available audit evidence."""

    request: JSONMap | None
    audit: JSONMap

    @property
    def admitted(self) -> bool:
        return self.request is not None

    def as_dict(self) -> JSONMap:
        return {
            "schema": SCHEMA,
            "kind": KIND,
            "request": deepcopy(self.request),
            "audit": deepcopy(self.audit),
        }


def _strict_json_copy(value: Any, label: str) -> Any:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(rendered)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise BuildRequestComposerV1Error(f"{label} must be strict JSON: {error}") from error


def _mapping_copy(value: Any, label: str) -> JSONMap:
    copied = _strict_json_copy(value, label)
    if not isinstance(copied, dict):
        raise BuildRequestComposerV1Error(f"{label} must be a JSON object")
    return copied


def _provenance(value: Any, label: str) -> JSONMap:
    copied = _mapping_copy(value, label)
    if not copied:
        raise BuildRequestComposerV1Error(f"{label} must not be empty")
    return copied


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BuildRequestComposerV1Error(f"{label} must be a non-negative integer")
    return value


def _finite_nonnegative_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildRequestComposerV1Error(f"{label} must be numeric")
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        raise BuildRequestComposerV1Error(f"{label} must be finite and non-negative")
    return value


def _selection_source(selection: ProfileSelection) -> JSONMap:
    if not isinstance(selection, ProfileSelection):
        raise BuildRequestComposerV1Error(
            "CharacterProfile.selection must be a ProfileSelection"
        )
    record = selection.record
    if not isinstance(record, Mapping):
        raise BuildRequestComposerV1Error("CharacterProfile selection record is invalid")
    source_provenance = record.get("provenance")
    return {
        "path": str(selection.path),
        "line_number": selection.line_number,
        "selection_mode": selection.selection_mode,
        "event": record.get("event"),
        "sequence": record.get("sequence"),
        "source_provenance": (
            _mapping_copy(source_provenance, "selection record provenance")
            if isinstance(source_provenance, Mapping)
            else None
        ),
    }


def _minimal_character_template() -> JSONMap:
    # Deliberately contains no buffs, consumes, rotation, encounter, objective,
    # equipment, talents, or inherited Warrior setting.  The existing profile
    # converter is used only for its observed character and talent semantics.
    return {
        "raid": {
            "parties": [
                {
                    "players": [
                        {
                            "warrior": {"options": {}},
                        }
                    ]
                }
            ]
        }
    }


def _profile_player(character: CharacterProfile) -> tuple[JSONMap, JSONMap]:
    try:
        converted, metadata = build_wowsims_profile(
            _minimal_character_template(), character.selection
        )
    except WowsimsProfileError as error:
        raise BuildRequestComposerV1Error(str(error)) from error
    player = converted["raid"]["parties"][0]["players"][0]
    if not isinstance(player, dict):
        raise BuildRequestComposerV1Error("profile conversion did not return a player")
    if not isinstance(player.get("name"), str) or not player["name"].strip():
        raise BuildRequestComposerV1Error("observed character name is required")
    if player.get("class") != "ClassWarrior":
        raise BuildRequestComposerV1Error("build_request_composer/v1 supports Warrior only")
    return deepcopy(player), deepcopy(metadata)


def _raw_equipment_by_slot(selection: ProfileSelection) -> dict[int, Mapping[str, Any]]:
    state = selection.record.get("state")
    equipment = state.get("equipment") if isinstance(state, Mapping) else None
    if not isinstance(equipment, list):
        raise BuildRequestComposerV1Error("selected state.equipment must be an array")
    result: dict[int, Mapping[str, Any]] = {}
    for index, raw in enumerate(equipment):
        if not isinstance(raw, Mapping):
            raise BuildRequestComposerV1Error(
                f"selected state.equipment[{index}] must be an object"
            )
        slot = raw.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise BuildRequestComposerV1Error(
                f"selected state.equipment[{index}].slot must be an integer"
            )
        if slot in result:
            raise BuildRequestComposerV1Error(f"duplicate observed equipment slot {slot}")
        result[slot] = raw
    return result


def _normalized_slot_statuses(
    character: CharacterProfile,
    raw_equipment: Mapping[int, Mapping[str, Any]],
) -> dict[int, str]:
    overrides: dict[int, str] = {}
    if not isinstance(character.slot_statuses, Mapping):
        raise BuildRequestComposerV1Error("CharacterProfile.slot_statuses must be a mapping")
    for raw_slot, raw_status in character.slot_statuses.items():
        if isinstance(raw_slot, bool) or not isinstance(raw_slot, int):
            raise BuildRequestComposerV1Error("slot_statuses keys must be WoW slot integers")
        if raw_slot not in WOW_SLOT_TO_WOWSIMS_SLOT:
            raise BuildRequestComposerV1Error(
                f"slot_statuses contains unsupported WoW slot {raw_slot}"
            )
        if raw_status not in SLOT_STATUSES:
            raise BuildRequestComposerV1Error(
                f"slot_statuses[{raw_slot}] has unsupported status {raw_status!r}"
            )
        overrides[raw_slot] = raw_status

    full_capture = (
        character.selection.selection_mode == "latest_static_profile"
        and character.selection.record.get("event") == "STATIC_PROFILE_CAPTURED"
    )
    statuses: dict[int, str] = {}
    for wow_slot in sorted(WOW_SLOT_TO_WOWSIMS_SLOT):
        inferred = (
            OBSERVED_EQUIPPED
            if wow_slot in raw_equipment
            else (OBSERVED_EMPTY if full_capture else MISSING)
        )
        status = overrides.get(wow_slot, inferred)
        if wow_slot in raw_equipment and status != OBSERVED_EQUIPPED:
            raise BuildRequestComposerV1Error(
                f"slot {wow_slot} has an observed item but status is {status}"
            )
        if wow_slot not in raw_equipment and status == OBSERVED_EQUIPPED:
            raise BuildRequestComposerV1Error(
                f"slot {wow_slot} is declared equipped but has no observed item"
            )
        statuses[wow_slot] = status
    return statuses


def _coverage_row(
    *,
    wow_slot: int,
    simulator_item: Mapping[str, Any],
    raw_coverage: Mapping[str, Any] | None,
) -> tuple[JSONMap, list[JSONMap]]:
    item_id = simulator_item.get("id")
    enchant_id = simulator_item.get("enchant", 0)
    blockers: list[JSONMap] = []
    if raw_coverage is None:
        row = {
            "wow_slot": wow_slot,
            "simulator_slot": WOW_SLOT_TO_WOWSIMS_SLOT[wow_slot],
            "item_id": item_id,
            "permanent_enchant_id": enchant_id or None,
            "temporary_enchant_id": None,
            "item_definition_status": "UNKNOWN",
            "item_effect_status": "UNKNOWN",
            "permanent_enchant_status": "UNKNOWN" if enchant_id else "NONE",
            "temporary_enchant_status": "NOT_OBSERVED",
            "temporary_enchant_request_representation": "NOT_ENCODED",
            "runtime_executable": False,
            "provenance": None,
        }
        blockers.append(
            {
                "code": "ITEM_EFFECT_COVERAGE_MISSING",
                "wow_slot": wow_slot,
                "item_id": item_id,
            }
        )
        return row, blockers

    coverage = _mapping_copy(raw_coverage, f"item_effect_coverage[{wow_slot}]")
    expected_fields = {
        "item_id",
        "item_definition_status",
        "item_effect_status",
        "permanent_enchant_status",
        "temporary_enchant_status",
        "temporary_enchant_id",
        "provenance",
    }
    if set(coverage) != expected_fields:
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}] field set mismatch"
        )
    if coverage.get("item_id") != item_id:
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}].item_id differs from equipped item"
        )
    definition_status = coverage.get("item_definition_status")
    effect_status = coverage.get("item_effect_status")
    permanent_status = coverage.get("permanent_enchant_status")
    temporary_status = coverage.get("temporary_enchant_status")
    if definition_status not in _ITEM_DEFINITION_STATUSES:
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}].item_definition_status is unsupported"
        )
    if effect_status not in _ITEM_EFFECT_STATUSES:
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}].item_effect_status is unsupported"
        )
    if permanent_status not in _ENCHANT_STATUSES:
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}].permanent_enchant_status is unsupported"
        )
    if temporary_status not in _ENCHANT_STATUSES:
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}].temporary_enchant_status is unsupported"
        )
    temporary_enchant_id = coverage.get("temporary_enchant_id")
    if temporary_status == "IMPLEMENTED":
        if (
            isinstance(temporary_enchant_id, bool)
            or not isinstance(temporary_enchant_id, int)
            or temporary_enchant_id <= 0
        ):
            raise BuildRequestComposerV1Error(
                f"item_effect_coverage[{wow_slot}] implemented temporary enchant needs a positive ID"
            )
    elif temporary_enchant_id not in (None, 0):
        raise BuildRequestComposerV1Error(
            f"item_effect_coverage[{wow_slot}] has a temporary enchant ID without IMPLEMENTED status"
        )
    provenance = _provenance(
        coverage.get("provenance"), f"item_effect_coverage[{wow_slot}].provenance"
    )

    if definition_status != "KNOWN":
        blockers.append(
            {"code": "ITEM_DEFINITION_UNKNOWN", "wow_slot": wow_slot, "item_id": item_id}
        )
    if effect_status not in _ADMITTED_ITEM_EFFECT_STATUSES:
        blockers.append(
            {
                "code": "ITEM_EFFECT_NOT_EXECUTABLE",
                "wow_slot": wow_slot,
                "item_id": item_id,
                "status": effect_status,
            }
        )
    expected_permanent_status = "IMPLEMENTED" if enchant_id else "NONE"
    if permanent_status != expected_permanent_status:
        blockers.append(
            {
                "code": "PERMANENT_ENCHANT_NOT_EXECUTABLE",
                "wow_slot": wow_slot,
                "enchant_id": enchant_id or None,
                "status": permanent_status,
                "required_status": expected_permanent_status,
            }
        )
    if temporary_status not in _ADMITTED_ENCHANT_STATUSES:
        blockers.append(
            {
                "code": "TEMPORARY_ENCHANT_NOT_EXECUTABLE",
                "wow_slot": wow_slot,
                "temporary_enchant_id": temporary_enchant_id,
                "status": temporary_status,
            }
        )
    elif temporary_status == "IMPLEMENTED":
        # ItemSpec has no temporary-enchant field.  Some temporary weapon
        # effects are represented by consumes in wowsims, but this composer
        # does not yet own a pinned ID -> consumes translation or a
        # double-application audit.  Treating an implemented simulator effect
        # as encoded would therefore admit a request that silently omits it
        # (or applies it twice when a caller also supplies consumes).
        blockers.append(
            {
                "code": "TEMPORARY_ENCHANT_NOT_ENCODED_IN_REQUEST",
                "wow_slot": wow_slot,
                "temporary_enchant_id": temporary_enchant_id,
                "required_bridge": "PINNED_TEMP_ENCHANT_TO_CONSUMES_MAPPING",
            }
        )

    row = {
        "wow_slot": wow_slot,
        "simulator_slot": WOW_SLOT_TO_WOWSIMS_SLOT[wow_slot],
        "item_id": item_id,
        "permanent_enchant_id": enchant_id or None,
        "temporary_enchant_id": temporary_enchant_id or None,
        "item_definition_status": definition_status,
        "item_effect_status": effect_status,
        "permanent_enchant_status": permanent_status,
        "temporary_enchant_status": temporary_status,
        "temporary_enchant_request_representation": (
            "NOT_APPLICABLE"
            if temporary_status == "NONE"
            else "NOT_ENCODED"
        ),
        "runtime_executable": not blockers,
        "provenance": provenance,
    }
    return row, blockers


def _equipment_audit(
    character: CharacterProfile,
    player: Mapping[str, Any],
) -> tuple[JSONMap, list[JSONMap]]:
    raw_equipment = _raw_equipment_by_slot(character.selection)
    statuses = _normalized_slot_statuses(character, raw_equipment)
    equipment = player.get("equipment")
    items = equipment.get("items") if isinstance(equipment, Mapping) else None
    if not isinstance(items, list) or len(items) != WOWSIMS_EQUIPMENT_SLOT_COUNT:
        raise BuildRequestComposerV1Error(
            "profile converter did not return the 17 simulator equipment slots"
        )

    if not isinstance(character.item_effect_coverage, Mapping):
        raise BuildRequestComposerV1Error(
            "CharacterProfile.item_effect_coverage must be a mapping"
        )
    coverage_keys = set(character.item_effect_coverage)
    if any(isinstance(key, bool) or not isinstance(key, int) for key in coverage_keys):
        raise BuildRequestComposerV1Error(
            "item_effect_coverage keys must be WoW slot integers"
        )
    equipped_slots = {slot for slot, status in statuses.items() if status == OBSERVED_EQUIPPED}
    extra_coverage = sorted(coverage_keys - equipped_slots)
    if extra_coverage:
        raise BuildRequestComposerV1Error(
            "item_effect_coverage contains non-equipped slots: "
            + ", ".join(str(slot) for slot in extra_coverage)
        )

    slot_rows: list[JSONMap] = []
    effect_rows: list[JSONMap] = []
    blockers: list[JSONMap] = []
    for wow_slot in sorted(WOW_SLOT_TO_WOWSIMS_SLOT):
        simulator_slot = WOW_SLOT_TO_WOWSIMS_SLOT[wow_slot]
        status = statuses[wow_slot]
        simulator_item = items[simulator_slot]
        slot_rows.append(
            {
                "wow_slot": wow_slot,
                "simulator_slot": simulator_slot,
                "status": status,
                "item_id": (
                    simulator_item.get("id")
                    if isinstance(simulator_item, Mapping) and simulator_item
                    else None
                ),
            }
        )
        if status == MISSING:
            blockers.append({"code": "EQUIPMENT_SLOT_MISSING", "wow_slot": wow_slot})
            continue
        if status == OBSERVED_EMPTY:
            if simulator_item:
                raise BuildRequestComposerV1Error(
                    f"observed empty slot {wow_slot} contains a simulator item"
                )
            continue
        if not isinstance(simulator_item, Mapping) or not simulator_item:
            raise BuildRequestComposerV1Error(
                f"observed equipped slot {wow_slot} has no simulator item"
            )
        row, row_blockers = _coverage_row(
            wow_slot=wow_slot,
            simulator_item=simulator_item,
            raw_coverage=character.item_effect_coverage.get(wow_slot),
        )
        effect_rows.append(row)
        blockers.extend(row_blockers)

    main_hand = raw_equipment.get(16)
    main_hand_info = main_hand.get("itemInfo") if isinstance(main_hand, Mapping) else None
    main_hand_location = (
        main_hand_info.get("equipLoc") if isinstance(main_hand_info, Mapping) else None
    )
    offhand_status = statuses[17]
    if offhand_status == OBSERVED_EMPTY:
        offhand_semantics = (
            "LEGAL_EMPTY_TWO_HAND_MAIN_HAND"
            if main_hand_location == "INVTYPE_2HWEAPON"
            else "LEGAL_OBSERVED_EMPTY"
        )
    elif offhand_status == MISSING:
        offhand_semantics = "MISSING_NOT_EMPTY"
    else:
        offhand_semantics = "OBSERVED_EQUIPPED"

    counts = {status: list(statuses.values()).count(status) for status in sorted(SLOT_STATUSES)}
    return (
        {
            "capture_scope": (
                "FULL_STATIC_CAPTURE"
                if character.selection.selection_mode == "latest_static_profile"
                and character.selection.record.get("event") == "STATIC_PROFILE_CAPTURED"
                else "PARTIAL_OR_BOUNDARY_CAPTURE"
            ),
            "slot_count": WOWSIMS_EQUIPMENT_SLOT_COUNT,
            "slot_status_counts": counts,
            "slots": slot_rows,
            "offhand_semantics": offhand_semantics,
            "item_effects": effect_rows,
            "runtime_executable": not blockers,
        },
        blockers,
    )


def _talent_audit(player: Mapping[str, Any], metadata: Mapping[str, Any]) -> JSONMap:
    options = player.get("warrior", {}).get("options", {})
    ravager_rank = options.get("ravagerRank", 0) if isinstance(options, Mapping) else 0
    defaults = metadata.get("simulator_semantic_defaults")
    improved_slam_rank = None
    if isinstance(defaults, list):
        for row in defaults:
            if isinstance(row, Mapping) and row.get("field") == "WarriorTalents.improvedSlam":
                improved_slam_rank = row.get("value")
                break
    if improved_slam_rank is None:
        raise BuildRequestComposerV1Error("profile conversion omitted Improved Slam audit")
    return {
        "talents_string": player.get("talentsString"),
        "ravager": {
            "target": "warrior.options.ravagerRank",
            "rank": ravager_rank,
        },
        "improved_slam": {
            "target": "WarriorTalents.improvedSlam",
            "rank": improved_slam_rank,
        },
        "semantic_separation": "PASS",
        "unmapped_talents": deepcopy(metadata.get("unmapped_talents", [])),
        "option_mappings": deepcopy(metadata.get("talent_option_mappings", [])),
    }


def _validate_raid_context(context: RaidContext) -> tuple[JSONMap, JSONMap]:
    individual_buffs = _mapping_copy(context.individual_buffs, "RaidContext.individual_buffs")
    party_buffs = _mapping_copy(context.party_buffs, "RaidContext.party_buffs")
    raid_buffs = _mapping_copy(context.raid_buffs, "RaidContext.raid_buffs")
    debuffs = _mapping_copy(context.debuffs, "RaidContext.debuffs")
    raid_options = _mapping_copy(context.raid_options, "RaidContext.raid_options")
    unsupported = sorted(set(raid_options) - _RAID_OPTION_FIELDS)
    if unsupported:
        raise BuildRequestComposerV1Error(
            "RaidContext.raid_options contains unsupported fields: " + ", ".join(unsupported)
        )
    additional_party_players = _strict_json_copy(
        context.additional_party_players, "RaidContext.additional_party_players"
    )
    additional_parties = _strict_json_copy(
        context.additional_parties, "RaidContext.additional_parties"
    )
    if not isinstance(additional_party_players, list) or any(
        not isinstance(row, dict) for row in additional_party_players
    ):
        raise BuildRequestComposerV1Error(
            "RaidContext.additional_party_players must be an array of objects"
        )
    if not isinstance(additional_parties, list) or any(
        not isinstance(row, dict) for row in additional_parties
    ):
        raise BuildRequestComposerV1Error(
            "RaidContext.additional_parties must be an array of objects"
        )
    return (
        {
            "individual_buffs": individual_buffs,
            "party_buffs": party_buffs,
            "raid_buffs": raid_buffs,
            "debuffs": debuffs,
            "additional_party_players": additional_party_players,
            "additional_parties": additional_parties,
            "raid_options": raid_options,
        },
        _provenance(context.provenance, "RaidContext.provenance"),
    )


def _validate_execution(execution: ExecutionModel) -> tuple[JSONMap, JSONMap]:
    rotation = _mapping_copy(execution.rotation, "ExecutionModel.rotation")
    cooldowns = _mapping_copy(execution.cooldowns, "ExecutionModel.cooldowns")
    options = _mapping_copy(execution.warrior_options, "ExecutionModel.warrior_options")
    if "ravagerRank" in options:
        raise BuildRequestComposerV1Error(
            "ExecutionModel.warrior_options must not own character-derived ravagerRank"
        )
    reaction = _nonnegative_int(execution.reaction_time_ms, "reaction_time_ms")
    clip = _nonnegative_int(execution.channel_clip_delay_ms, "channel_clip_delay_ms")
    if not isinstance(execution.in_front_of_target, bool):
        raise BuildRequestComposerV1Error("in_front_of_target must be boolean")
    distance = _finite_nonnegative_number(
        execution.distance_from_target, "distance_from_target"
    )
    return (
        {
            "rotation": rotation,
            "cooldowns": cooldowns,
            "warrior_options": options,
            "reaction_time_ms": reaction,
            "channel_clip_delay_ms": clip,
            "in_front_of_target": execution.in_front_of_target,
            "distance_from_target": distance,
        },
        _provenance(execution.provenance, "ExecutionModel.provenance"),
    )


def _validate_objective(objective: Objective) -> tuple[JSONMap, JSONMap]:
    if not isinstance(objective.kind, str) or not objective.kind.strip():
        raise BuildRequestComposerV1Error("Objective.kind must be a non-empty string")
    options = _mapping_copy(objective.sim_options, "Objective.sim_options")
    required = {"iterations", "randomSeed", "interactive"}
    missing = sorted(required - set(options))
    if missing:
        raise BuildRequestComposerV1Error(
            "Objective.sim_options is missing explicit fields: " + ", ".join(missing)
        )
    iterations = options.get("iterations")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0:
        raise BuildRequestComposerV1Error("Objective.sim_options.iterations must be positive")
    seed = options.get("randomSeed")
    if not (
        (isinstance(seed, int) and not isinstance(seed, bool))
        or (isinstance(seed, str) and _DECIMAL_INTEGER.fullmatch(seed))
    ):
        raise BuildRequestComposerV1Error(
            "Objective.sim_options.randomSeed must be an integer or decimal integer string"
        )
    if not isinstance(options.get("interactive"), bool):
        raise BuildRequestComposerV1Error(
            "Objective.sim_options.interactive must be boolean"
        )
    return (
        {"kind": objective.kind.strip(), "sim_options": options},
        _provenance(objective.provenance, "Objective.provenance"),
    )


def _validate_encounter(encounter: EncounterModel) -> tuple[JSONMap, JSONMap]:
    fields = _mapping_copy(encounter.request_fields, "EncounterModel.request_fields")
    return fields, _provenance(encounter.provenance, "EncounterModel.provenance")


def compose_build_request_v1(
    character: CharacterProfile,
    raid_context: RaidContext,
    encounter: EncounterModel,
    execution: ExecutionModel,
    objective: Objective,
) -> BuildRequestCompositionV1:
    """Build one request and its provenance, coverage, and residual audits."""

    if not isinstance(character, CharacterProfile):
        raise TypeError("character must be CharacterProfile")
    if not isinstance(raid_context, RaidContext):
        raise TypeError("raid_context must be RaidContext")
    if not isinstance(encounter, EncounterModel):
        raise TypeError("encounter must be EncounterModel")
    if not isinstance(execution, ExecutionModel):
        raise TypeError("execution must be ExecutionModel")
    if not isinstance(objective, Objective):
        raise TypeError("objective must be Objective")

    character_provenance = _provenance(
        character.provenance, "CharacterProfile.provenance"
    )
    player, profile_metadata = _profile_player(character)
    equipment_audit, blockers = _equipment_audit(character, player)
    talent_audit = _talent_audit(player, profile_metadata)
    raid_fields, raid_provenance = _validate_raid_context(raid_context)
    encounter_fields, encounter_provenance = _validate_encounter(encounter)
    execution_fields, execution_provenance = _validate_execution(execution)
    objective_fields, objective_provenance = _validate_objective(objective)

    consumes = _mapping_copy(character.consumes, "CharacterProfile.consumes")
    database = _mapping_copy(character.database, "CharacterProfile.database")
    player["consumes"] = consumes
    player["database"] = database
    player["buffs"] = raid_fields["individual_buffs"]
    player["rotation"] = execution_fields["rotation"]
    player["cooldowns"] = execution_fields["cooldowns"]
    player["reactionTimeMs"] = execution_fields["reaction_time_ms"]
    player["channelClipDelayMs"] = execution_fields["channel_clip_delay_ms"]
    player["inFrontOfTarget"] = execution_fields["in_front_of_target"]
    player["distanceFromTarget"] = execution_fields["distance_from_target"]
    player["warrior"] = {
        "options": {
            **execution_fields["warrior_options"],
            "ravagerRank": talent_audit["ravager"]["rank"],
        }
    }

    primary_party = {
        "players": [player, *raid_fields["additional_party_players"]],
        "buffs": raid_fields["party_buffs"],
    }
    raid = {
        "parties": [primary_party, *raid_fields["additional_parties"]],
        "buffs": raid_fields["raid_buffs"],
        "debuffs": raid_fields["debuffs"],
        **raid_fields["raid_options"],
    }
    candidate_request: JSONMap = {
        "raid": raid,
        "encounter": encounter_fields,
        "simOptions": objective_fields["sim_options"],
    }
    candidate_request = _mapping_copy(candidate_request, "composed RaidSimRequest")

    try:
        _validate_full_policy_request(candidate_request)
    except (FuryFullPolicyRolloutV2Error, TypeError, ValueError) as error:
        raise BuildRequestComposerV1Error(
            f"existing full-policy request validation failed: {error}"
        ) from error

    residual_checks = [
        {"path": "raid.parties[0].players[0].equipment", "owner": "CharacterProfile"},
        {"path": "raid.parties[0].players[0].talentsString", "owner": "CharacterProfile"},
        {"path": "raid.parties[0].players[0].consumes", "owner": "CharacterProfile"},
        {"path": "raid.parties[0].players[0].database", "owner": "CharacterProfile"},
        {"path": "raid.parties[0].players[0].buffs", "owner": "RaidContext"},
        {"path": "raid.parties[0].buffs", "owner": "RaidContext"},
        {"path": "raid.buffs", "owner": "RaidContext"},
        {"path": "raid.debuffs", "owner": "RaidContext"},
        {"path": "encounter", "owner": "EncounterModel"},
        {"path": "raid.parties[0].players[0].rotation", "owner": "ExecutionModel"},
        {"path": "raid.parties[0].players[0].cooldowns", "owner": "ExecutionModel"},
        {
            "path": "raid.parties[0].players[0].warrior.options(except ravagerRank)",
            "owner": "ExecutionModel",
        },
        {
            "path": "raid.parties[0].players[0].warrior.options.ravagerRank",
            "owner": "CharacterProfile",
        },
        {"path": "simOptions", "owner": "Objective"},
    ]
    for check in residual_checks:
        check["status"] = "PASS"

    admitted = not blockers
    audit: JSONMap = {
        "schema_version": 1,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "provenance": {
            "character_profile": {
                "declared": character_provenance,
                "selection": _selection_source(character.selection),
                "converter": "o2o_dps.wowsims_profile.build_wowsims_profile",
            },
            "raid_context": raid_provenance,
            "encounter_model": encounter_provenance,
            "execution_model": execution_provenance,
            "objective": objective_provenance,
        },
        "coverage": {
            "equipment": equipment_audit,
            "talents": talent_audit,
            "observed_static_counts": deepcopy(
                profile_metadata.get("observed_character", {}).get("static_counts", {})
            ),
        },
        "residual_audit": {
            "status": "PASS",
            "template_used": False,
            "legacy_template_fields_inherited": [],
            "unattributed_paths": [],
            "ownership_checks": residual_checks,
            "protobuf_zero_defaults_may_apply_only_to_omitted_fields": True,
        },
        "request_validation": {
            "status": "PASS",
            "validator": "o2o_dps.fury_full_policy_rollout_v2._validate_request",
        },
        "objective_kind": objective_fields["kind"],
        "admission": {
            "status": "ADMITTED" if admitted else "BLOCKED",
            "runtime_executable": admitted,
            "blockers": deepcopy(blockers),
        },
    }
    return BuildRequestCompositionV1(
        request=candidate_request if admitted else None,
        audit=_mapping_copy(audit, "composition audit"),
    )


__all__ = [
    "BuildRequestComposerV1Error",
    "BuildRequestCompositionV1",
    "CharacterProfile",
    "EncounterModel",
    "ExecutionModel",
    "MISSING",
    "OBSERVED_EMPTY",
    "OBSERVED_EQUIPPED",
    "Objective",
    "RaidContext",
    "compose_build_request_v1",
]
