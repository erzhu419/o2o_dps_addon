"""Compile causal historical character-build segments from Chronicle INFO.

The catalogue is deliberately a small derived product.  It reads one admitted
instance and its small ``combatant_info`` object at a time, merges repeated
identical snapshots per exact player GUID, and writes one row per contiguous
build segment.  It does not open the large normalized combat-event partitions.

An INFO snapshot only becomes usable at its own EventMeta anchor.  The prefix
lookup in this module never substitutes a later snapshot for an earlier query;
``valid_until`` is retrospective catalogue metadata, not a policy feature.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .chronicle_combatant_sidecar import (
    RECORD_SCHEMA as COMBATANT_RECORD_SCHEMA,
    decode_combatant_info_stream,
    sidecar_records,
)
from .chronicle_external_event_normalizer_v1 import (
    _read_object_reference,
    _resolve_raw_root,
)
from .chronicle_external_reconstruction_admission_v1 import (
    canonical_guid,
    load_admission_manifest,
)


SCHEMA = "historical_build_catalog/v1"
RECORD_SCHEMA = "historical_build_segment/v1"
COVERAGE_SCHEMA = "historical_build_coverage_registry/v1"
IMPLEMENTATION_REVISION = (
    "v1.3_prefix_only_info_segments_recorder_provenance"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_ADMISSION_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_external_reconstruction_admission"
    / "v1"
    / "utk_postfix_dev_20260903_noon"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "historical_build_catalog" / "v1"
)

EXPECTED_SLOT_COUNT = 19
SLOT_NAMES = {
    1: "HEAD",
    2: "NECK",
    3: "SHOULDER",
    4: "SHIRT",
    5: "CHEST",
    6: "WAIST",
    7: "LEGS",
    8: "FEET",
    9: "WRIST",
    10: "HANDS",
    11: "FINGER_1",
    12: "FINGER_2",
    13: "TRINKET_1",
    14: "TRINKET_2",
    15: "BACK",
    16: "MAIN_HAND",
    17: "OFF_HAND",
    18: "RANGED",
    19: "TABARD",
}

OBSERVED_EQUIPPED = "OBSERVED_EQUIPPED"
OBSERVED_EMPTY = "OBSERVED_EMPTY"
MISSING = "MISSING"
AMBIGUOUS = "AMBIGUOUS"

# Shirt and tabard are cosmetic.  Off hand is conditional on the main-hand
# weapon mode and is checked separately below.  Every other combat-relevant
# slot must contain an observed item before a historical row can stand in for a
# representative end-game build.  This is intentionally separate from the
# simulator's ability to execute a technically valid (including naked) request.
REPRESENTATIVE_REQUIRED_EQUIPPED_SLOTS = frozenset(
    slot for slot in SLOT_NAMES if slot not in {4, 17, 19}
)
SIMULATOR_RELEVANT_SLOTS = frozenset(
    slot for slot in SLOT_NAMES if slot not in {4, 19}
)


class HistoricalBuildCatalogError(RuntimeError):
    """The admitted source, build state, or output contract is invalid."""


@dataclass(frozen=True)
class InstanceBuildInput:
    """One small, independently consumable Chronicle instance."""

    server: str
    realm: str
    realm_id: str | None
    instance_id: str
    instance_name: str
    slug: str
    metadata_versions: Mapping[str, Any]
    game_flavor: Any
    game_format: str | None
    players: tuple[Mapping[str, Any], ...]
    records: tuple[Mapping[str, Any], ...]
    source: Mapping[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalBuildCatalogError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalBuildCatalogError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise HistoricalBuildCatalogError(f"{label} must be a trimmed string")
    if not allow_empty and not value:
        raise HistoricalBuildCatalogError(f"{label} must not be empty")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalBuildCatalogError(f"{label} must be an integer")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json_object(payload: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalBuildCatalogError(f"{label} is not UTF-8 JSON: {error}") from error
    return _mapping(value, label=label)


def _load_coverage_registry(path: str | Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalBuildCatalogError(
            f"cannot read coverage registry {resolved}: {error}"
        ) from error
    registry = _mapping(document, label="coverage registry")
    if registry.get("schema") != COVERAGE_SCHEMA:
        raise HistoricalBuildCatalogError(
            f"coverage registry schema must be {COVERAGE_SCHEMA}"
        )
    for key in ("items", "enchants", "talent_translations"):
        _mapping(registry.get(key, {}), label=f"coverage registry {key}")
    return registry


def _coverage_entry(
    registry: Mapping[str, Any] | None,
    section: str,
    identifier: int,
) -> dict[str, Any]:
    if registry is None:
        return {
            "definition_status": "NOT_EVALUATED_NO_REGISTRY",
            "effect_status": "NOT_EVALUATED_NO_REGISTRY",
            "calibrated_scopes": [],
        }
    entries = _mapping(registry.get(section, {}), label=f"coverage registry {section}")
    raw = entries.get(str(identifier))
    if raw is None:
        return {
            "definition_status": "UNKNOWN_TO_REGISTRY",
            "effect_status": "UNKNOWN_TO_REGISTRY",
            "calibrated_scopes": [],
        }
    entry = _mapping(raw, label=f"coverage registry {section}.{identifier}")
    definition_status = entry.get("definition_status")
    effect_status = entry.get("effect_status")
    scopes = entry.get("calibrated_scopes", [])
    if not isinstance(definition_status, str) or not definition_status:
        raise HistoricalBuildCatalogError(
            f"coverage registry {section}.{identifier}.definition_status is invalid"
        )
    if not isinstance(effect_status, str) or not effect_status:
        raise HistoricalBuildCatalogError(
            f"coverage registry {section}.{identifier}.effect_status is invalid"
        )
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) and scope for scope in scopes
    ):
        raise HistoricalBuildCatalogError(
            f"coverage registry {section}.{identifier}.calibrated_scopes is invalid"
        )
    result = {
        "definition_status": definition_status,
        "effect_status": effect_status,
        "calibrated_scopes": list(scopes),
    }
    if section == "items" and "weapon_mode" in entry:
        result["weapon_mode"] = entry["weapon_mode"]
    return result


def _slot_state(raw_gear: Any, registry: Mapping[str, Any] | None) -> dict[str, Any]:
    """Turn ordered protobuf slots into explicit 1..19 semantic states."""

    if not isinstance(raw_gear, list):
        raw_gear = []
        source_is_array = False
    else:
        source_is_array = True
    by_slot: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    extras: list[Any] = []
    for raw_slot in raw_gear:
        if not isinstance(raw_slot, Mapping):
            extras.append(raw_slot)
            continue
        slot_index = raw_slot.get("slot_index")
        if isinstance(slot_index, bool) or not isinstance(slot_index, int):
            extras.append(dict(raw_slot))
            continue
        wow_slot = slot_index + 1
        if wow_slot not in SLOT_NAMES:
            extras.append(dict(raw_slot))
            continue
        by_slot[wow_slot].append(raw_slot)

    slots: list[dict[str, Any]] = []
    for wow_slot in range(1, EXPECTED_SLOT_COUNT + 1):
        candidates = by_slot.get(wow_slot, [])
        base: dict[str, Any] = {
            "inventory_slot": wow_slot,
            "slot_name": SLOT_NAMES[wow_slot],
            "status": MISSING,
            "item_id": None,
            "permanent_enchant_id": None,
            "temporary_enchant_id": None,
            "gem_enchant_ids": [],
            "random_suffix": None,
            "raw_candidate_count": len(candidates),
            "coverage": None,
        }
        if len(candidates) != 1:
            if len(candidates) > 1:
                base["status"] = AMBIGUOUS
                base["raw_candidates"] = [dict(candidate) for candidate in candidates]
            slots.append(base)
            continue
        candidate = candidates[0]
        item_id = candidate.get("item_id")
        enchant_id = candidate.get("enchant_id")
        temporary = candidate.get("temporary_enchant_id")
        gems = candidate.get("gem_enchant_ids", [])
        valid_optional_ids = all(
            value is None or (isinstance(value, int) and not isinstance(value, bool))
            for value in (enchant_id, temporary)
        )
        valid_gems = isinstance(gems, list) and all(
            isinstance(value, int) and not isinstance(value, bool) for value in gems
        )
        if (
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id < 0
            or not valid_optional_ids
            or not valid_gems
        ):
            base["status"] = AMBIGUOUS
            base["raw_candidates"] = [dict(candidate)]
            slots.append(base)
            continue
        base.update(
            {
                "item_id": item_id,
                "permanent_enchant_id": enchant_id,
                "temporary_enchant_id": temporary,
                "gem_enchant_ids": list(gems),
                "random_suffix": candidate.get("random_suffix"),
            }
        )
        nonzero_aux = any(
            value not in (None, 0)
            for value in (enchant_id, temporary, *gems)
        )
        if item_id == 0 and nonzero_aux:
            base["status"] = AMBIGUOUS
            base["ambiguity_reason"] = "EMPTY_ITEM_WITH_ENCHANT_EVIDENCE"
        elif item_id == 0:
            base["status"] = OBSERVED_EMPTY
        else:
            base["status"] = OBSERVED_EQUIPPED
            item_coverage = _coverage_entry(registry, "items", item_id)
            enchant_coverage: dict[str, Any] = {}
            for label, identifier in (
                ("permanent", enchant_id),
                ("temporary", temporary),
            ):
                if isinstance(identifier, int) and identifier > 0:
                    enchant_coverage[label] = {
                        "id": identifier,
                        **_coverage_entry(registry, "enchants", identifier),
                    }
            gem_coverage = [
                {"id": identifier, **_coverage_entry(registry, "enchants", identifier)}
                for identifier in gems
                if identifier > 0
            ]
            base["coverage"] = {
                "item": item_coverage,
                "enchants": enchant_coverage,
                "gems": gem_coverage,
            }
        slots.append(base)
    return {
        "raw_slot_count": len(raw_gear) if source_is_array else None,
        "expected_slot_count": EXPECTED_SLOT_COUNT,
        "slots": slots,
        "unmapped_raw_entries": extras,
        "item_dataset": registry.get("item_dataset") if registry is not None else None,
    }


def _talent_state(
    raw_talents: Any,
    *,
    hero_class: str,
    client_build: str | None,
    registry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    base = {
        "original_summary": None,
        "original_tree_rank_strings": None,
        "semantic_ranks": [],
        "translation_version": None,
        "translation_status": MISSING,
        "translation_reason": "COMBATANT_INFO_TALENTS_ABSENT",
    }
    if raw_talents is None:
        return base
    if not isinstance(raw_talents, Mapping):
        return {
            **base,
            "translation_status": AMBIGUOUS,
            "translation_reason": "TALENTS_NOT_AN_OBJECT",
        }
    summary = raw_talents.get("summary")
    trees = raw_talents.get("trees")
    base["original_summary"] = summary
    base["original_tree_rank_strings"] = trees
    if not isinstance(summary, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in summary
    ):
        return {
            **base,
            "translation_status": AMBIGUOUS,
            "translation_reason": "INVALID_SUMMARY",
        }
    if not isinstance(trees, list) or not all(isinstance(tree, str) for tree in trees):
        return {
            **base,
            "translation_status": AMBIGUOUS,
            "translation_reason": "INVALID_TREE_RANK_STRINGS",
        }
    if any(any(character < "0" or character > "9" for character in tree) for tree in trees):
        return {
            **base,
            "translation_status": AMBIGUOUS,
            "translation_reason": "NON_DECIMAL_TREE_RANK",
        }
    if len(summary) != len(trees) or any(
        summary[index] != sum(int(character) for character in tree)
        for index, tree in enumerate(trees)
    ):
        return {
            **base,
            "translation_status": AMBIGUOUS,
            "translation_reason": "SUMMARY_TREE_TOTAL_MISMATCH",
        }
    if registry is None:
        return {
            **base,
            "translation_status": "OBSERVED_RAW_UNTRANSLATED",
            "translation_reason": "NO_PINNED_TALENT_DATASET",
        }
    translations = _mapping(
        registry.get("talent_translations", {}),
        label="coverage registry talent_translations",
    )
    raw_translation = translations.get(hero_class.upper())
    if raw_translation is None:
        return {
            **base,
            "translation_status": "OBSERVED_RAW_UNTRANSLATED",
            "translation_reason": "CLASS_NOT_IN_PINNED_TALENT_DATASET",
        }
    translation = _mapping(raw_translation, label=f"talent translation {hero_class}")
    version = translation.get("translation_version")
    if not isinstance(version, str) or not version:
        raise HistoricalBuildCatalogError("talent translation version is invalid")
    position_maps = _mapping(
        translation.get("client_build_position_maps", {}),
        label="talent translation client_build_position_maps",
    )
    if client_build is None or client_build not in position_maps:
        return {
            **base,
            "translation_version": version,
            "translation_status": "OBSERVED_RAW_UNTRANSLATED",
            "translation_reason": "CLIENT_BUILD_POSITION_MAP_NOT_PINNED",
        }
    position_map = _mapping(
        position_maps[client_build],
        label=f"talent translation client build position map {client_build}",
    )
    fields = position_map.get("tree_fields")
    zero_tail_trim_allowed = position_map.get(
        "zero_only_unsupported_tail_may_be_trimmed", False
    )
    if not isinstance(zero_tail_trim_allowed, bool):
        raise HistoricalBuildCatalogError(
            "talent translation zero_only_unsupported_tail_may_be_trimmed must be boolean"
        )
    if not isinstance(fields, list) or not all(isinstance(tree, list) for tree in fields):
        raise HistoricalBuildCatalogError("talent translation tree_fields is invalid")
    if len(fields) != len(trees):
        return {
            **base,
            "translation_version": version,
            "translation_status": AMBIGUOUS,
            "translation_reason": "TREE_COUNT_DIFFERS_FROM_PINNED_DATASET",
        }
    trimmed_zero_tail: list[dict[str, Any]] = []
    for tree_index, tree in enumerate(trees):
        supported_length = len(fields[tree_index])
        if len(tree) <= supported_length:
            continue
        unsupported_tail = tree[supported_length:]
        nonzero_positions = [
            supported_length + offset
            for offset, character in enumerate(unsupported_tail)
            if character != "0"
        ]
        if nonzero_positions:
            return {
                **base,
                "translation_version": version,
                "translation_status": "OBSERVED_RAW_UNTRANSLATED",
                "translation_reason": "NONZERO_UNSUPPORTED_TALENT_POSITION",
                "unsupported_nonzero_positions": [
                    {"tree_index": tree_index, "position": position}
                    for position in nonzero_positions
                ],
            }
        if not zero_tail_trim_allowed:
            return {
                **base,
                "translation_version": version,
                "translation_status": AMBIGUOUS,
                "translation_reason": "TREE_SHAPE_EXCEEDS_PINNED_DATASET",
            }
        trimmed_zero_tail.append(
            {
                "tree_index": tree_index,
                "first_unsupported_position": supported_length,
                "trimmed_zero_count": len(unsupported_tail),
            }
        )
    semantic: list[dict[str, Any]] = []
    rank_limit_violations: list[dict[str, Any]] = []
    for tree_index, tree in enumerate(trees):
        for position, character in enumerate(tree[: len(fields[tree_index])]):
            raw_field = fields[tree_index][position]
            field = dict(
                _mapping(
                    raw_field,
                    label=(
                        f"talent translation tree {tree_index} position {position}"
                    ),
                )
            )
            talent_id = field.get("talent_id")
            if not isinstance(talent_id, str) or not talent_id:
                raise HistoricalBuildCatalogError("talent translation field id is invalid")
            max_rank = field.get("max_rank")
            if (
                isinstance(max_rank, bool)
                or not isinstance(max_rank, int)
                or max_rank <= 0
            ):
                raise HistoricalBuildCatalogError(
                    "talent translation field max_rank must be a positive integer"
                )
            rank = int(character)
            if rank and rank > max_rank:
                rank_limit_violations.append(
                    {
                        "tree_index": tree_index,
                        "position": position,
                        "talent_id": talent_id,
                        "rank": rank,
                        "max_rank": max_rank,
                    }
                )
                continue
            if rank:
                semantic.append({
                    **field,
                    "talent_id": talent_id,
                    "rank": rank,
                    "tree_index": tree_index,
                    "position": position,
                })
    if rank_limit_violations:
        return {
            **base,
            "translation_version": version,
            "translation_status": "OBSERVED_RAW_UNTRANSLATED",
            "translation_reason": "TALENT_RANK_EXCEEDS_PINNED_MAX_RANK",
            "rank_limit_violations": rank_limit_violations,
        }
    return {
        **base,
        "semantic_ranks": semantic,
        "translation_version": version,
        "translation_status": "TRANSLATED_EXACT",
        "translation_reason": None,
        "trimmed_zero_only_unsupported_tail": trimmed_zero_tail,
    }


def _record_anchor(record: Mapping[str, Any]) -> dict[str, Any] | None:
    anchor = record.get("anchor")
    if anchor is None:
        return None
    anchor = _mapping(anchor, label="CombatantInfo anchor")
    return {
        "timestamp_ms": _integer(anchor.get("timestamp_ms"), label="anchor.timestamp_ms"),
        "encounter_id": _text(record.get("encounter_id"), label="record.encounter_id"),
        "event_index": _integer(anchor.get("event_index"), label="anchor.event_index"),
        "message_ordinal": _integer(record.get("message_ordinal"), label="message_ordinal"),
        "offset_ms": _integer(anchor.get("offset_ms", 0), label="anchor.offset_ms"),
        "is_synthetic": bool(anchor.get("is_synthetic", False)),
    }


def _anchor_key(anchor: Mapping[str, Any]) -> tuple[int, str, int, int]:
    return (
        int(anchor["timestamp_ms"]),
        str(anchor["encounter_id"]),
        int(anchor["event_index"]),
        int(anchor["message_ordinal"]),
    )


def _raw_build_signature(record: Mapping[str, Any]) -> str:
    return _canonical_json({"gear": record.get("gear"), "talents": record.get("talents")})


def _metadata_by_guid(players: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_player in enumerate(players):
        player = _mapping(raw_player, label=f"metadata player {index}")
        try:
            guid = canonical_guid(player.get("guid"), label="metadata player GUID")
        except Exception as error:
            raise HistoricalBuildCatalogError(str(error)) from error
        if guid in result:
            raise HistoricalBuildCatalogError("metadata player GUID is duplicated")
        value = player.get("metadata", player)
        result[guid] = {
            **dict(_mapping(value, label=f"metadata player {guid}")),
            "_catalog_provenance": "ADMISSION_EXACT_GUID_RESOLVER",
        }
    return result


def _record_guid(record: Mapping[str, Any]) -> str:
    if record.get("schema") != COMBATANT_RECORD_SCHEMA:
        raise HistoricalBuildCatalogError(
            f"unsupported CombatantInfo row schema: {record.get('schema')!r}"
        )
    player = _mapping(record.get("player"), label="CombatantInfo player")
    try:
        return canonical_guid(player.get("guid"), label="CombatantInfo player GUID")
    except Exception as error:
        raise HistoricalBuildCatalogError(str(error)) from error


def _representative_build_coverage(
    equipment: Mapping[str, Any], talents: Mapping[str, Any]
) -> dict[str, Any]:
    equipment_reasons: list[str] = []
    raw_slots = equipment.get("slots")
    if not isinstance(raw_slots, list) or len(raw_slots) != EXPECTED_SLOT_COUNT:
        equipment_reasons.append("EQUIPMENT_SLOT_VECTOR_NOT_CANONICAL")
        slots_by_id: dict[int, Mapping[str, Any]] = {}
    else:
        slots_by_id = {
            int(slot.get("inventory_slot")): slot
            for slot in raw_slots
            if isinstance(slot, Mapping)
            and isinstance(slot.get("inventory_slot"), int)
            and not isinstance(slot.get("inventory_slot"), bool)
        }
        if set(slots_by_id) != set(SLOT_NAMES):
            equipment_reasons.append("EQUIPMENT_SLOT_VECTOR_NOT_CANONICAL")

    if slots_by_id:
        if any(
            slots_by_id[slot_id].get("status") in {MISSING, AMBIGUOUS}
            for slot_id in SIMULATOR_RELEVANT_SLOTS
        ):
            equipment_reasons.append("EQUIPMENT_NOT_FULLY_OBSERVED")
        for slot_id in sorted(REPRESENTATIVE_REQUIRED_EQUIPPED_SLOTS):
            slot = slots_by_id.get(slot_id)
            if slot is None or slot.get("status") != OBSERVED_EQUIPPED:
                equipment_reasons.append(
                    f"CORE_SLOT_NOT_EQUIPPED:{SLOT_NAMES[slot_id]}"
                )

        main_hand = slots_by_id.get(16)
        off_hand = slots_by_id.get(17)
        if (
            main_hand is not None
            and main_hand.get("status") == OBSERVED_EQUIPPED
            and off_hand is not None
        ):
            main_mode = (
                ((main_hand.get("coverage") or {}).get("item") or {}).get(
                    "weapon_mode"
                )
            )
            if main_mode == "TWO_HAND":
                if off_hand.get("status") != OBSERVED_EMPTY:
                    equipment_reasons.append(
                        "TWO_HAND_MAIN_REQUIRES_OBSERVED_EMPTY_OFFHAND"
                    )
            elif main_mode == "ONE_HAND":
                if off_hand.get("status") != OBSERVED_EQUIPPED:
                    equipment_reasons.append(
                        "ONE_HAND_MAIN_REQUIRES_OBSERVED_EQUIPPED_OFFHAND"
                    )
            else:
                equipment_reasons.append("MAIN_HAND_WEAPON_MODE_NOT_PINNED")

    equipment_reasons = sorted(set(equipment_reasons))
    build_reasons = list(equipment_reasons)
    talent_semantics_exact = talents.get("translation_status") == "TRANSLATED_EXACT"
    if not talent_semantics_exact:
        build_reasons.append("HISTORICAL_TALENT_SEMANTICS_NOT_EXACT")
    build_reasons = sorted(set(build_reasons))
    return {
        "eligible": not build_reasons,
        "equipment_plausible": not equipment_reasons,
        "equipment_reasons": equipment_reasons,
        "talent_semantics_exact": talent_semantics_exact,
        "reasons": build_reasons,
        "contract": {
            "population": "END_GAME_BUILD_WITH_ALL_COMBAT_RELEVANT_SLOTS_EQUIPPED",
            "required_equipped_slots": [
                SLOT_NAMES[slot]
                for slot in sorted(REPRESENTATIVE_REQUIRED_EQUIPPED_SLOTS)
            ],
            "optional_cosmetic_slots": [SLOT_NAMES[4], SLOT_NAMES[19]],
            "offhand_rule": "EMPTY_FOR_PINNED_TWO_HAND_OTHERWISE_EQUIPPED_FOR_PINNED_ONE_HAND",
            "technical_simulator_runnability_is_separate": True,
        },
    }


def _segment_runtime_coverage(
    equipment: Mapping[str, Any], talents: Mapping[str, Any]
) -> dict[str, Any]:
    reasons: list[str] = []
    calibration_scope_sets: list[set[str]] = []
    calibration_missing: list[str] = []

    def collect_calibration(label: str, evidence: Mapping[str, Any]) -> None:
        scopes = evidence.get("calibrated_scopes")
        if not isinstance(scopes, list) or not scopes:
            calibration_missing.append(label)
            return
        calibration_scope_sets.append(
            {scope for scope in scopes if isinstance(scope, str) and scope}
        )

    slots = equipment["slots"]
    simulator_slots = [
        slot
        for slot in slots
        if slot.get("inventory_slot") not in {4, 19}
    ]
    if any(slot["status"] in {MISSING, AMBIGUOUS} for slot in simulator_slots):
        reasons.append("EQUIPMENT_MISSING_OR_AMBIGUOUS")
    if talents["translation_status"] != "TRANSLATED_EXACT":
        reasons.append("TALENTS_NOT_EXACTLY_TRANSLATED")
    for talent in talents.get("semantic_ranks", []):
        if talent.get("definition_status") not in (None, "KNOWN"):
            reasons.append("TALENT_DEFINITION_NOT_COVERED")
        if talent.get("effect_status") not in (None, "IMPLEMENTED"):
            reasons.append("TALENT_EFFECT_NOT_COVERED")
        if "calibrated_scopes" in talent:
            collect_calibration(
                f"talent:{talent.get('talent_id') or talent.get('profile_name')}",
                talent,
            )
    for slot in simulator_slots:
        if slot["status"] != OBSERVED_EQUIPPED:
            continue
        coverage = slot.get("coverage") or {}
        item = coverage.get("item") or {}
        if item.get("definition_status") != "KNOWN":
            reasons.append("ITEM_DEFINITION_NOT_COVERED")
        if item.get("effect_status") not in {"IMPLEMENTED", "NO_SPECIAL_EFFECT"}:
            reasons.append("ITEM_EFFECT_NOT_COVERED")
        if "calibrated_scopes" in item:
            collect_calibration(f"item:{slot.get('item_id')}", item)
        for enchant in (coverage.get("enchants") or {}).values():
            if enchant.get("definition_status") != "KNOWN" or enchant.get(
                "effect_status"
            ) != "IMPLEMENTED":
                reasons.append("ENCHANT_NOT_COVERED")
            if "calibrated_scopes" in enchant:
                collect_calibration(f"enchant:{enchant.get('id')}", enchant)
        for enchant in coverage.get("gems") or []:
            reasons.append("GEM_ENCHANT_ENCODING_NOT_SUPPORTED_BY_COMPOSER")
            if enchant.get("definition_status") != "KNOWN" or enchant.get(
                "effect_status"
            ) != "IMPLEMENTED":
                reasons.append("GEM_ENCHANT_NOT_COVERED")
            if "calibrated_scopes" in enchant:
                collect_calibration(f"gem_enchant:{enchant.get('id')}", enchant)
        temporary = slot.get("temporary_enchant_id")
        if isinstance(temporary, int) and not isinstance(temporary, bool) and temporary > 0:
            reasons.append("TEMPORARY_ENCHANT_ENCODING_NOT_SUPPORTED_BY_COMPOSER")
    reasons = sorted(set(reasons))
    common_scopes = (
        set.intersection(*calibration_scope_sets)
        if calibration_scope_sets
        else set()
    )
    representative = _representative_build_coverage(equipment, talents)
    development_reasons: list[str] = []
    if reasons:
        development_reasons.append("SIMULATOR_REPRESENTATION_NOT_RUNNABLE")
    if not representative["eligible"]:
        development_reasons.append("HISTORICAL_BUILD_NOT_REPRESENTATIVE")
    development_reasons = sorted(set(development_reasons))
    comparison_reasons: list[str] = list(development_reasons)
    if calibration_missing:
        comparison_reasons.append("TURTLE_CALIBRATION_SCOPE_MISSING")
    if calibration_scope_sets and not common_scopes:
        comparison_reasons.append("NO_COMMON_TURTLE_CALIBRATION_SCOPE")
    if not calibration_scope_sets:
        comparison_reasons.append("NO_DECLARED_TURTLE_CALIBRATION_SCOPE")
    comparison_reasons = sorted(set(comparison_reasons))
    return {
        "runtime_executable": not reasons,
        "representative_build_eligible": representative["eligible"],
        "development_build_eligible": not development_reasons,
        "uncertainty": reasons,
        "coverage_claim": (
            "RUNTIME_EXECUTABLE_IN_DECLARED_REGISTRY_SCOPE"
            if not reasons
            else "NOT_RUNTIME_EXECUTABLE_FROM_CATALOGUE_EVIDENCE"
        ),
        "simulator_representation": {
            "runnable": not reasons,
            "reasons": reasons,
            "evaluated_inventory_slots": sorted(SIMULATOR_RELEVANT_SLOTS),
            "ignored_cosmetic_slots": [SLOT_NAMES[4], SLOT_NAMES[19]],
        },
        "historical_representativeness": representative,
        "development": {
            "eligible": not development_reasons,
            "reasons": development_reasons,
        },
        "turtle_calibration": {
            "fully_covered": not calibration_missing and bool(common_scopes),
            "missing_mechanisms": sorted(set(calibration_missing)),
            "common_scopes": sorted(common_scopes),
        },
        "comparison": {
            "eligible": not comparison_reasons,
            "reasons": comparison_reasons,
        },
    }


def _missing_segment(instance: InstanceBuildInput, guid: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    equipment = _slot_state(None, None)
    talents = _talent_state(
        None,
        hero_class=str(metadata.get("class") or ""),
        client_build=str(instance.metadata_versions.get("wow_build") or "") or None,
        registry=None,
    )
    return _base_segment(
        instance,
        guid,
        metadata,
        segment_number=1,
        observation_status="MISSING_COMBATANT_INFO",
        first_anchor=None,
        last_anchor=None,
        next_anchor=None,
        message_count=0,
        duplicate_count=0,
        unanchored_count=0,
        encounter_ids=[],
        equipment=equipment,
        talents=talents,
        raw_signature=None,
    )


def _base_segment(
    instance: InstanceBuildInput,
    guid: str,
    metadata: Mapping[str, Any],
    *,
    segment_number: int,
    observation_status: str,
    first_anchor: Mapping[str, Any] | None,
    last_anchor: Mapping[str, Any] | None,
    next_anchor: Mapping[str, Any] | None,
    message_count: int,
    duplicate_count: int,
    unanchored_count: int,
    encounter_ids: Sequence[str],
    equipment: Mapping[str, Any],
    talents: Mapping[str, Any],
    raw_signature: str | None,
) -> dict[str, Any]:
    versions = dict(instance.metadata_versions)
    client_build = str(versions.get("wow_build") or "") or None
    coverage = _segment_runtime_coverage(equipment, talents)
    if first_anchor is None:
        coverage["runtime_executable"] = False
        coverage["coverage_claim"] = "NOT_RUNTIME_EXECUTABLE_FROM_CATALOGUE_EVIDENCE"
        coverage["uncertainty"] = sorted(
            set([*coverage["uncertainty"], "NO_CAUSAL_INFO_ANCHOR"])
        )
        coverage["simulator_representation"] = {
            "runnable": False,
            "reasons": list(coverage["uncertainty"]),
        }
        coverage["representative_build_eligible"] = False
        coverage["development_build_eligible"] = False
        representative = dict(coverage["historical_representativeness"])
        representative["eligible"] = False
        representative["reasons"] = sorted(
            set([*representative["reasons"], "NO_CAUSAL_INFO_ANCHOR"])
        )
        coverage["historical_representativeness"] = representative
        coverage["development"] = {
            "eligible": False,
            "reasons": sorted(
                set(
                    [
                        *coverage["development"]["reasons"],
                        "NO_CAUSAL_INFO_ANCHOR",
                    ]
                )
            ),
        }
        coverage["comparison"] = {
            "eligible": False,
            "reasons": sorted(
                set(
                    [
                        *coverage["comparison"]["reasons"],
                        "SIMULATOR_REPRESENTATION_NOT_RUNNABLE",
                    ]
                )
            ),
        }
    return {
        "schema": RECORD_SCHEMA,
        "event": "HISTORICAL_BUILD_SEGMENT_OBSERVED",
        "selection_mode": (
            "historical_build_segment_prefix"
            if first_anchor is not None
            else "historical_build_segment_unavailable_for_prefix"
        ),
        "identity": {
            "server": instance.server,
            "realm": instance.realm,
            "realm_id": instance.realm_id,
            "player_guid": guid,
            "instance_id": instance.instance_id,
            "build_segment_id": f"segment-{segment_number:04d}",
        },
        "player": {
            "name": metadata.get("name"),
            "hero_class": metadata.get("class"),
            "race": metadata.get("race"),
            "level": metadata.get("level"),
            "metadata_provenance": metadata.get(
                "_catalog_provenance", "ADMISSION_EXACT_GUID_RESOLVER"
            ),
        },
        "observation": {
            "status": observation_status,
            "observed_at": dict(first_anchor) if first_anchor is not None else None,
            "last_identical_observation_at": (
                dict(last_anchor) if last_anchor is not None else None
            ),
            "valid_from": dict(first_anchor) if first_anchor is not None else None,
            "valid_until_or_unknown": (
                dict(next_anchor) if next_anchor is not None else None
            ),
            "valid_until_semantics": (
                "RETROSPECTIVE_NEXT_INFO_BOUNDARY_NOT_POLICY_INPUT"
                if next_anchor is not None
                else "UNKNOWN_NO_LATER_BUILD_CHANGE_OBSERVED"
            ),
            "source_message_count": message_count,
            "causally_anchored_message_count": message_count - unanchored_count,
            "duplicate_identical_message_count": duplicate_count,
            "unanchored_message_count_for_player": unanchored_count,
            "encounter_ids": list(encounter_ids),
            "prefix_policy_contract": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY",
        },
        "version": {
            "game_flavor": instance.game_flavor,
            "format": instance.game_format,
            "patch": None,
            "client_build": client_build,
            "wow_client": versions.get("wow_client"),
            "addon": versions.get("addon"),
            "chronicle_companion": versions.get("chronicle_companion"),
            "chronicle": versions.get("chronicle"),
            "talent_tree_dataset": talents.get("translation_version"),
            "item_dataset": equipment.get("item_dataset"),
        },
        "equipment": dict(equipment),
        "talents": dict(talents),
        "context": {
            "weapon_skill_evidence": {
                "status": "MISSING_NOT_IN_COMBATANT_INFO",
                "provenance": "CHRONICLE_COMBATANT_INFO_PROTO",
            },
            "static_buffs": {
                "status": "NOT_IN_COMBATANT_INFO",
                "provenance": "SEPARATE_ADMITTED_STATE_EVIDENCE_REQUIRED",
            },
            "time_varying_auras": {
                "status": "SEPARATE_PREFIX_STATE_STREAM_REQUIRED",
                "provenance": "chronicle_external_state_event_normalizer_v1",
            },
        },
        "coverage": coverage,
        "provenance": {
            "source_kind": "CHRONICLE_COMBATANT_INFO",
            "identity_key": "SERVER_REALM_EXACT_PLAYER_GUID_INSTANCE_SEGMENT",
            "prefix_policy_contract": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY",
            "future_info_backfill_used": False,
        },
        "source": dict(instance.source),
    }


def compile_instance_segments(
    instance: InstanceBuildInput,
    *,
    coverage_registry: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compile one instance without retaining any other instance in memory."""

    metadata = _metadata_by_guid(instance.players)
    by_guid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in instance.records:
        record = _mapping(record, label="CombatantInfo record")
        if record.get("instance_ref") != instance.instance_id:
            raise HistoricalBuildCatalogError("CombatantInfo instance identity differs")
        guid = _record_guid(record)
        by_guid[guid].append(record)
        if guid not in metadata:
            player = _mapping(record.get("player"), label="CombatantInfo player")
            metadata[guid] = {
                "name": player.get("name"),
                "class": player.get("hero_class"),
                "race": player.get("race"),
                "level": None,
                "_catalog_provenance": "COMBATANT_INFO_EXACT_GUID_ONLY",
            }

    output: list[dict[str, Any]] = []
    client_build = str(instance.metadata_versions.get("wow_build") or "") or None
    for guid in sorted(metadata):
        records = by_guid.get(guid, [])
        if not records:
            output.append(_missing_segment(instance, guid, metadata[guid]))
            continue
        anchored: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
        unanchored_count = 0
        for record in records:
            anchor = _record_anchor(record)
            if anchor is None:
                unanchored_count += 1
            else:
                anchored.append((anchor, record))
        anchored.sort(key=lambda pair: _anchor_key(pair[0]))
        if not anchored:
            # Preserve the fact that INFO exists, but do not turn an unanchored
            # snapshot into a causal initial state.
            representative = records[-1]
            equipment = _slot_state(representative.get("gear"), coverage_registry)
            talents = _talent_state(
                representative.get("talents"),
                hero_class=str(metadata[guid].get("class") or ""),
                client_build=client_build,
                registry=coverage_registry,
            )
            output.append(
                _base_segment(
                    instance,
                    guid,
                    metadata[guid],
                    segment_number=1,
                    observation_status="AMBIGUOUS_UNANCHORED_COMBATANT_INFO",
                    first_anchor=None,
                    last_anchor=None,
                    next_anchor=None,
                    message_count=len(records),
                    duplicate_count=0,
                    unanchored_count=unanchored_count,
                    encounter_ids=sorted(
                        {
                            str(record.get("encounter_id"))
                            for record in records
                            if record.get("encounter_id") is not None
                        }
                    ),
                    equipment=equipment,
                    talents=talents,
                    raw_signature=_raw_build_signature(representative),
                )
            )
            continue

        groups: list[dict[str, Any]] = []
        for anchor, record in anchored:
            signature = _raw_build_signature(record)
            if groups and groups[-1]["signature"] == signature:
                groups[-1]["last_anchor"] = anchor
                groups[-1]["message_count"] += 1
                groups[-1]["encounter_ids"].add(str(record.get("encounter_id")))
                continue
            groups.append(
                {
                    "signature": signature,
                    "record": record,
                    "first_anchor": anchor,
                    "last_anchor": anchor,
                    "message_count": 1,
                    "encounter_ids": {str(record.get("encounter_id"))},
                }
            )
        for index, group in enumerate(groups):
            record = group["record"]
            equipment = _slot_state(record.get("gear"), coverage_registry)
            talents = _talent_state(
                record.get("talents"),
                hero_class=str(metadata[guid].get("class") or ""),
                client_build=client_build,
                registry=coverage_registry,
            )
            next_anchor = groups[index + 1]["first_anchor"] if index + 1 < len(groups) else None
            output.append(
                _base_segment(
                    instance,
                    guid,
                    metadata[guid],
                    segment_number=index + 1,
                    observation_status="OBSERVED_CAUSAL_PREFIX",
                    first_anchor=group["first_anchor"],
                    last_anchor=group["last_anchor"],
                    next_anchor=next_anchor,
                    message_count=(
                        group["message_count"]
                        + (unanchored_count if index == 0 else 0)
                    ),
                    duplicate_count=group["message_count"] - 1,
                    unanchored_count=unanchored_count if index == 0 else 0,
                    encounter_ids=sorted(group["encounter_ids"]),
                    equipment=equipment,
                    talents=talents,
                    raw_signature=group["signature"],
                )
            )
    return output


def select_prefix_segment(
    segments: Iterable[Mapping[str, Any]],
    *,
    server: str,
    realm: str,
    player_guid: str,
    instance_id: str,
    decision_timestamp_ms: int,
    encounter_id: str,
    decision_event_index: int,
) -> Mapping[str, Any] | None:
    """Select only INFO known at or before a decision event.

    A later INFO is never used when no prior segment exists.  Message ordinal
    is deliberately not needed for a policy-event query: at equal timestamps
    and encounter, EventMeta.index establishes the strict boundary.
    """

    try:
        requested_guid = canonical_guid(player_guid, label="prefix player GUID")
    except Exception as error:
        raise HistoricalBuildCatalogError(str(error)) from error
    decision_timestamp_ms = _integer(
        decision_timestamp_ms, label="decision_timestamp_ms"
    )
    decision_event_index = _integer(decision_event_index, label="decision_event_index")
    best: Mapping[str, Any] | None = None
    best_key: tuple[int, str, int, int] | None = None
    for segment in segments:
        identity = _mapping(segment.get("identity"), label="catalogue identity")
        if (
            identity.get("server") != server
            or identity.get("realm") != realm
            or identity.get("instance_id") != instance_id
            or identity.get("player_guid") != requested_guid
        ):
            continue
        observation = _mapping(segment.get("observation"), label="catalogue observation")
        anchor = observation.get("valid_from")
        if anchor is None:
            continue
        anchor = _mapping(anchor, label="catalogue valid_from")
        timestamp = _integer(anchor.get("timestamp_ms"), label="valid_from.timestamp_ms")
        if timestamp > decision_timestamp_ms:
            continue
        if timestamp == decision_timestamp_ms:
            if anchor.get("encounter_id") != encounter_id:
                continue
            if _integer(anchor.get("event_index"), label="valid_from.event_index") > decision_event_index:
                continue
        key = _anchor_key(anchor)
        if best_key is None or key > best_key:
            best = segment
            best_key = key
    return best


def evaluate_prefix_joins(
    segments: Iterable[Mapping[str, Any]],
    queries: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Measure exact-prefix availability for a streamed decision-query set."""

    by_identity: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments:
        identity = _mapping(segment.get("identity"), label="catalogue identity")
        key = (
            _text(identity.get("server"), label="identity.server"),
            _text(identity.get("realm"), label="identity.realm"),
            _text(identity.get("player_guid"), label="identity.player_guid"),
            _text(identity.get("instance_id"), label="identity.instance_id"),
        )
        by_identity[key].append(segment)
    query_count = 0
    joined_count = 0
    missing_no_prior_count = 0
    for raw_query in queries:
        query = _mapping(raw_query, label="prefix query")
        query_count += 1
        selected = select_prefix_segment(
            by_identity.get(
                (
                    _text(query.get("server"), label="query.server"),
                    _text(query.get("realm"), label="query.realm"),
                    _text(query.get("player_guid"), label="query.player_guid"),
                    _text(query.get("instance_id"), label="query.instance_id"),
                ),
                (),
            ),
            server=str(query["server"]),
            realm=str(query["realm"]),
            player_guid=str(query["player_guid"]),
            instance_id=str(query["instance_id"]),
            decision_timestamp_ms=_integer(
                query.get("decision_timestamp_ms"),
                label="query.decision_timestamp_ms",
            ),
            encounter_id=_text(
                query.get("encounter_id"), label="query.encounter_id"
            ),
            decision_event_index=_integer(
                query.get("decision_event_index"),
                label="query.decision_event_index",
            ),
        )
        if selected is None:
            missing_no_prior_count += 1
        else:
            joined_count += 1
    return {
        "status": "COMPUTED_STRICT_CAUSAL_PREFIX",
        "query_count": query_count,
        "joined_count": joined_count,
        "missing_no_prior_info_count": missing_no_prior_count,
        "rate": joined_count / query_count if query_count else None,
        "future_info_backfill_count": 0,
    }


def historical_segment_to_character_profile(
    segment: Mapping[str, Any],
    *,
    catalog_path: str | Path,
    catalog_line_number: int = 0,
    consumes: Mapping[str, Any],
    database: Mapping[str, Any],
):
    """Adapt one exact historical segment to ``build_request_composer_v1``.

    The adapter intentionally uses a historical event and selection mode.  It
    never labels the row as a live ``STATIC_PROFILE_CAPTURED`` record.  Missing
    slots remain explicit; ambiguous slots, unanchored INFO, untranslated
    talents, unsupported gem encoding, and uncovered temporary enchants stop at
    this boundary instead of being coerced into a runnable profile.
    """

    # Imported lazily so the catalogue remains usable without the request
    # composer and to keep the dependency direction explicit.
    from .build_request_composer_v1 import CharacterProfile
    from .wowsims_profile import ProfileSelection, WOW_SLOT_TO_WOWSIMS_SLOT

    if segment.get("schema") != RECORD_SCHEMA:
        raise HistoricalBuildCatalogError(
            f"historical segment schema must be {RECORD_SCHEMA}"
        )
    identity = _mapping(segment.get("identity"), label="historical identity")
    player = _mapping(segment.get("player"), label="historical player")
    observation = _mapping(segment.get("observation"), label="historical observation")
    if observation.get("status") != "OBSERVED_CAUSAL_PREFIX":
        raise HistoricalBuildCatalogError(
            "only causally anchored historical build segments can be composed"
        )
    valid_from = _mapping(observation.get("valid_from"), label="historical valid_from")
    segment_coverage = _mapping(segment.get("coverage"), label="historical coverage")
    if segment_coverage.get("representative_build_eligible") is not True:
        representativeness = _mapping(
            segment_coverage.get("historical_representativeness"),
            label="historical representativeness",
        )
        raise HistoricalBuildCatalogError(
            "historical segment is not an admitted representative build: "
            + ", ".join(str(value) for value in representativeness.get("reasons", []))
        )
    if segment_coverage.get("runtime_executable") is not True:
        raise HistoricalBuildCatalogError(
            "historical segment is not runnable in the declared simulator representation: "
            + ", ".join(str(value) for value in segment_coverage.get("uncertainty", []))
        )
    talents = _mapping(segment.get("talents"), label="historical talents")
    if talents.get("translation_status") != "TRANSLATED_EXACT":
        raise HistoricalBuildCatalogError(
            "historical talents need a pinned exact semantic translation before composition"
        )
    uncovered_talents = [
        str(rank.get("talent_id") or rank.get("profile_name") or "UNKNOWN")
        for rank in talents.get("semantic_ranks", [])
        if isinstance(rank, Mapping)
        and (
            rank.get("definition_status") not in (None, "KNOWN")
            or rank.get("effect_status") not in (None, "IMPLEMENTED")
        )
    ]
    if uncovered_talents:
        raise HistoricalBuildCatalogError(
            "historical learned talents lack simulator effect coverage: "
            + ", ".join(uncovered_talents)
        )

    equipment = _mapping(segment.get("equipment"), label="historical equipment")
    raw_slots = _array(equipment.get("slots"), label="historical equipment.slots")
    slot_statuses: dict[int, str] = {}
    profile_equipment: list[dict[str, Any]] = []
    item_effect_coverage: dict[int, dict[str, Any]] = {}
    for raw_slot in raw_slots:
        slot = _mapping(raw_slot, label="historical equipment slot")
        wow_slot = _integer(slot.get("inventory_slot"), label="inventory_slot")
        if wow_slot not in SLOT_NAMES:
            raise HistoricalBuildCatalogError("historical equipment slot is outside 1..19")
        status = slot.get("status")
        if status == AMBIGUOUS:
            raise HistoricalBuildCatalogError(
                f"historical slot {wow_slot} is ambiguous and cannot be composed"
            )
        if status not in {OBSERVED_EQUIPPED, OBSERVED_EMPTY, MISSING}:
            raise HistoricalBuildCatalogError(
                f"historical slot {wow_slot} has unsupported status {status!r}"
            )
        if wow_slot not in WOW_SLOT_TO_WOWSIMS_SLOT:
            continue
        slot_statuses[wow_slot] = status
        if status != OBSERVED_EQUIPPED:
            continue
        item_id = _integer(slot.get("item_id"), label=f"slot {wow_slot} item_id")
        if item_id <= 0:
            raise HistoricalBuildCatalogError(
                f"historical equipped slot {wow_slot} lacks a positive item id"
            )
        permanent = slot.get("permanent_enchant_id")
        temporary = slot.get("temporary_enchant_id")
        random_suffix = slot.get("random_suffix")
        for value, label in (
            (permanent, "permanent enchant"),
            (temporary, "temporary enchant"),
            (random_suffix, "random suffix"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise HistoricalBuildCatalogError(
                    f"historical slot {wow_slot} {label} is not an integer"
                )
        gems = slot.get("gem_enchant_ids", [])
        if not isinstance(gems, list) or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in gems
        ):
            raise HistoricalBuildCatalogError(
                f"historical slot {wow_slot} gem enchant ids are invalid"
            )
        if any(value > 0 for value in gems):
            raise HistoricalBuildCatalogError(
                "build_request_composer_v1 cannot encode historical gem enchant ids yet"
            )
        if isinstance(temporary, int) and temporary > 0:
            raise HistoricalBuildCatalogError(
                "build_request_composer_v1 cannot encode historical temporary enchants yet"
            )
        slot_coverage = _mapping(slot.get("coverage"), label=f"slot {wow_slot} coverage")
        item_coverage = _mapping(
            slot_coverage.get("item"), label=f"slot {wow_slot} item coverage"
        )
        enchant_coverage = _mapping(
            slot_coverage.get("enchants", {}),
            label=f"slot {wow_slot} enchant coverage",
        )

        def composer_enchant_status(label: str, identifier: Any) -> str:
            if identifier in (None, 0):
                return "NONE"
            evidence = enchant_coverage.get(label)
            if not isinstance(evidence, Mapping):
                return "UNKNOWN"
            return (
                "IMPLEMENTED"
                if evidence.get("definition_status") == "KNOWN"
                and evidence.get("effect_status") == "IMPLEMENTED"
                else "UNKNOWN"
            )

        permanent_status = composer_enchant_status("permanent", permanent)
        temporary_status = composer_enchant_status("temporary", temporary)
        if temporary not in (None, 0) and temporary_status != "IMPLEMENTED":
            raise HistoricalBuildCatalogError(
                "an observed historical temporary enchant must be covered before composition"
            )
        definition_status = (
            "KNOWN"
            if item_coverage.get("definition_status") == "KNOWN"
            else "UNKNOWN"
        )
        raw_effect_status = item_coverage.get("effect_status")
        effect_status = (
            raw_effect_status
            if raw_effect_status
            in {"NO_SPECIAL_EFFECT", "IMPLEMENTED", "UNSUPPORTED", "UNKNOWN"}
            else "UNKNOWN"
        )
        link = f"item:{item_id}:{int(permanent or 0)}:{int(random_suffix or 0)}"
        profile_row: dict[str, Any] = {"slot": wow_slot, "link": link}
        weapon_mode = item_coverage.get("weapon_mode")
        if weapon_mode == "TWO_HAND":
            profile_row["itemInfo"] = {"equipLoc": "INVTYPE_2HWEAPON"}
        elif weapon_mode == "ONE_HAND":
            profile_row["itemInfo"] = {"equipLoc": "INVTYPE_WEAPON"}
        profile_equipment.append(profile_row)
        item_effect_coverage[wow_slot] = {
            "item_id": item_id,
            "item_definition_status": definition_status,
            "item_effect_status": effect_status,
            "permanent_enchant_status": permanent_status,
            "temporary_enchant_status": temporary_status,
            "temporary_enchant_id": int(temporary) if temporary else None,
            "provenance": {
                "source": "historical_build_catalog_v1",
                "build_segment": dict(identity),
                "catalogue_item_dataset": equipment.get("item_dataset"),
                "calibrated_scopes": item_coverage.get("calibrated_scopes", []),
            },
        }
    missing_supported_slots = set(WOW_SLOT_TO_WOWSIMS_SLOT) - set(slot_statuses)
    if missing_supported_slots:
        raise HistoricalBuildCatalogError(
            "historical segment omitted supported slot states: "
            + ", ".join(str(value) for value in sorted(missing_supported_slots))
        )

    profile_talents: list[dict[str, Any]] = []
    for index, raw_rank in enumerate(
        _array(talents.get("semantic_ranks"), label="semantic talent ranks")
    ):
        rank = _mapping(raw_rank, label=f"semantic talent rank {index}")
        profile_name = rank.get("profile_name")
        if not isinstance(profile_name, str) or not profile_name:
            raise HistoricalBuildCatalogError(
                "pinned historical talent translation lacks profile_name for composer"
            )
        profile_talent = {
            "name": profile_name,
            "rank": _integer(rank.get("rank"), label="talent rank"),
        }
        for source_key, target_key in (
            ("tab", "tab"),
            ("index", "index"),
            ("tier", "tier"),
            ("column", "column"),
            ("max_rank", "maxRank"),
        ):
            if source_key in rank:
                profile_talent[target_key] = rank[source_key]
        profile_talents.append(profile_talent)

    segment_id = _text(identity.get("build_segment_id"), label="build_segment_id")
    catalog_line_number = _integer(
        catalog_line_number, label="catalog_line_number"
    )
    if catalog_line_number < 0:
        raise HistoricalBuildCatalogError("catalog_line_number must not be negative")
    profile_path = Path(catalog_path).expanduser().resolve()
    provenance = {
        "source": "historical_build_catalog_v1",
        "event": "HISTORICAL_BUILD_SEGMENT_OBSERVED",
        "selection_mode": "historical_build_segment_prefix",
        "identity": dict(identity),
        "valid_from": dict(valid_from),
        "prefix_policy_contract": observation.get("prefix_policy_contract"),
        "catalog_path": str(profile_path),
        "catalog_line_number": (
            catalog_line_number
            if catalog_line_number > 0
            else "NOT_BOUND_BY_IN_MEMORY_ADAPTER"
        ),
        "source_evidence": segment.get("source"),
        "historical_coverage": dict(segment_coverage),
    }
    selection = ProfileSelection(
        path=profile_path,
        line_number=catalog_line_number,
        record={
            "event": "HISTORICAL_BUILD_SEGMENT_OBSERVED",
            "sequence": valid_from.get("event_index"),
            "provenance": provenance,
            "state": {
                "characterIdentity": {
                    "name": player.get("name"),
                    "level": player.get("level"),
                    "raceName": player.get("race"),
                    "className": player.get("hero_class"),
                },
                "equipment": profile_equipment,
                "talents": profile_talents,
            },
        },
        selection_mode="historical_build_segment_prefix",
    )
    return CharacterProfile(
        selection=selection,
        consumes=dict(consumes),
        database=dict(database),
        slot_statuses=slot_statuses,
        item_effect_coverage=item_effect_coverage,
        provenance=provenance,
    )


def _metadata_context(metadata: Mapping[str, Any], instance: Mapping[str, Any]) -> tuple[Any, ...]:
    resolver = _mapping(
        instance.get("metadata_player_resolver"),
        label="admission metadata_player_resolver",
    )
    resolver_players = tuple(
        dict(_mapping(player, label="admission resolved metadata player"))
        for player in _array(
            resolver.get("players"), label="metadata_player_resolver.players"
        )
    )
    versions = _mapping(metadata.get("versions", {}), label="metadata.versions")
    raw_recorder_guid = metadata.get("recorder_guid")
    recorder_guid = None
    if isinstance(raw_recorder_guid, str) and raw_recorder_guid.strip():
        try:
            recorder_guid = canonical_guid(raw_recorder_guid)
        except Exception as error:
            raise HistoricalBuildCatalogError(
                f"metadata.recorder_guid is invalid: {raw_recorder_guid!r}"
            ) from error
    recorder_name = metadata.get("recorder_name")
    if not isinstance(recorder_name, str) or not recorder_name.strip():
        recorder_name = None
    return (
        _text(metadata.get("server_name"), label="metadata.server_name"),
        _text(metadata.get("realm_name"), label="metadata.realm_name"),
        metadata.get("realm_id"),
        _text(metadata.get("name"), label="metadata.name"),
        _text(metadata.get("slug"), label="metadata.slug"),
        dict(versions),
        metadata.get("flavor"),
        metadata.get("format"),
        resolver_players,
        {
            "admission_status": instance.get("status"),
            "combatant_info_selection_contract": _mapping(
                instance.get("combatant_info_evidence"), label="combatant_info_evidence"
            ).get("selection_contract"),
            "normalized_event_source": _mapping(
                instance.get("source_evidence"), label="source_evidence"
            ).get("normalized_partition"),
            "chronicle_recorder": {
                "player_guid": recorder_guid,
                "name": recorder_name,
                "identity_status": (
                    "EXACT_METADATA_RECORDER_GUID"
                    if recorder_guid is not None
                    else "MISSING_IN_METADATA"
                ),
            },
            "metadata_versions": dict(versions),
            "game_format": metadata.get("format"),
        },
    )


def iter_admitted_instance_inputs(
    admission_manifest: str | Path = DEFAULT_ADMISSION_MANIFEST,
    *,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> Iterator[InstanceBuildInput]:
    """Yield one verified, decoded CombatantInfo instance at a time."""

    try:
        admission, _ = load_admission_manifest(admission_manifest)
        raw_root = _resolve_raw_root(Path(data_root))
    except Exception as error:
        raise HistoricalBuildCatalogError(str(error)) from error
    instances = _array(admission.get("instances"), label="admission.instances")
    for index, raw_instance in enumerate(instances):
        instance = _mapping(raw_instance, label=f"admission.instances[{index}]")
        instance_id = _text(instance.get("instance_id"), label="instance_id")
        combatant = _mapping(
            instance.get("combatant_info_evidence"), label="combatant_info_evidence"
        )
        source_evidence = _mapping(instance.get("source_evidence"), label="source_evidence")
        try:
            compressed, combatant_path = _read_object_reference(
                raw_root,
                combatant.get("object"),
                label=f"{instance_id}.combatant_info",
            )
            metadata_payload, metadata_path = _read_object_reference(
                raw_root,
                source_evidence.get("metadata_object"),
                label=f"{instance_id}.metadata",
            )
            frames = decode_combatant_info_stream(compressed)
        except Exception as error:
            raise HistoricalBuildCatalogError(str(error)) from error
        records = tuple(
            sidecar_records(
                frames,
                instance_ref=instance_id,
                slug=str(instance.get("slug") or ""),
            )
        )
        if len(records) != combatant.get("message_count"):
            raise HistoricalBuildCatalogError(
                f"{instance_id} CombatantInfo message count differs from admission"
            )
        metadata = _load_json_object(metadata_payload, label=f"{instance_id} metadata")
        if metadata.get("id") != instance_id:
            raise HistoricalBuildCatalogError(
                f"{instance_id} metadata object has a different instance id"
            )
        (
            server,
            realm,
            realm_id,
            instance_name,
            slug,
            versions,
            flavor,
            game_format,
            players,
            source,
        ) = _metadata_context(metadata, instance)
        yield InstanceBuildInput(
            server=server,
            realm=realm,
            realm_id=str(realm_id) if realm_id is not None else None,
            instance_id=instance_id,
            instance_name=instance_name,
            slug=slug,
            metadata_versions=versions,
            game_flavor=flavor,
            game_format=str(game_format) if game_format is not None else None,
            players=players,
            records=records,
            source={
                **source,
                "combatant_info_object_path": str(combatant_path),
                "metadata_object_path": str(metadata_path),
                "rows_copied_from_large_event_partitions": 0,
            },
        )


def _semantic_signature(segment: Mapping[str, Any], field: str) -> str | None:
    if field == "equipment":
        slots = segment["equipment"]["slots"]
        return _canonical_json(
            [
                {
                    key: slot.get(key)
                    for key in (
                        "inventory_slot",
                        "status",
                        "item_id",
                        "permanent_enchant_id",
                        "temporary_enchant_id",
                        "gem_enchant_ids",
                        "random_suffix",
                    )
                }
                for slot in slots
            ]
        )
    talents = segment["talents"]
    if talents["original_tree_rank_strings"] is None:
        return None
    return _canonical_json(
        {
            "summary": talents["original_summary"],
            "trees": talents["original_tree_rank_strings"],
        }
    )


def _weapon_mode(segment: Mapping[str, Any]) -> str:
    slots = segment["equipment"]["slots"]
    main_hand = slots[15]
    off_hand = slots[16]
    if main_hand["status"] != OBSERVED_EQUIPPED:
        return "UNKNOWN"
    main_mode = ((main_hand.get("coverage") or {}).get("item") or {}).get(
        "weapon_mode"
    )
    if off_hand["status"] == OBSERVED_EQUIPPED:
        off_mode = ((off_hand.get("coverage") or {}).get("item") or {}).get(
            "weapon_mode"
        )
        return "DUAL_WIELD" if main_mode == "ONE_HAND" and off_mode == "ONE_HAND" else "UNKNOWN"
    if off_hand["status"] != OBSERVED_EMPTY:
        return "UNKNOWN"
    if main_mode == "TWO_HAND":
        return "TWO_HAND"
    if main_mode == "ONE_HAND":
        return "ONE_HAND_NO_OFFHAND"
    return "UNKNOWN"


def build_catalog_from_instances(
    instances: Iterable[InstanceBuildInput],
    *,
    output_directory: str | Path,
    coverage_registry: Mapping[str, Any] | None = None,
    prefix_queries: Iterable[Mapping[str, Any]] | None = None,
    source_admission_manifest: str | Path | None = None,
    coverage_registry_source: str | Path | None = None,
) -> dict[str, Any]:
    """Stream catalogue rows and exact small-cardinality aggregate statistics."""

    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_catalog = output_dir / "catalog.jsonl.gz"
    final_manifest = output_dir / "manifest.json"
    slot_status_counts = {
        str(slot): Counter() for slot in range(1, EXPECTED_SLOT_COUNT + 1)
    }
    observation_counts: Counter[str] = Counter()
    translation_counts: Counter[str] = Counter()
    item_definition_counts: Counter[str] = Counter()
    item_effect_counts: Counter[str] = Counter()
    unique_players: set[tuple[str, str, str]] = set()
    player_instances: Counter[tuple[str, str, str]] = Counter()
    equipment_signatures: set[str] = set()
    fully_observed_equipment_signatures: set[str] = set()
    talent_signatures: set[str] = set()
    unknown_item_ids: set[int] = set()
    unknown_enchant_ids: set[int] = set()
    unevaluated_item_ids: set[int] = set()
    unevaluated_enchant_ids: set[int] = set()
    untranslated_positions: set[str] = set()
    instance_count = 0
    segment_count = 0
    source_message_count = 0
    duplicate_message_count = 0
    unanchored_message_count = 0
    runtime_executable_count = 0
    representative_equipment_count = 0
    representative_build_count = 0
    development_build_count = 0
    comparison_eligible_count = 0
    representative_reason_counts: Counter[str] = Counter()
    offhand_empty_count = 0
    legal_empty_offhand_count = 0
    legal_empty_offhand_evaluated_count = 0
    prefix_segments: list[Mapping[str, Any]] | None = (
        [] if prefix_queries is not None else None
    )
    class_stats: dict[str, dict[str, Any]] = {}

    def stats_for(hero_class: str) -> dict[str, Any]:
        if hero_class not in class_stats:
            class_stats[hero_class] = {
                "players": set(),
                "segments": 0,
                "causal_segments": 0,
                "runtime_executable_segments": 0,
                "representative_equipment_segments": 0,
                "representative_build_segments": 0,
                "development_build_segments": 0,
                "comparison_eligible_segments": 0,
                "equipment_signatures": set(),
                "talent_signatures": set(),
                "semantic_talent_signatures": set(),
                "semantic_talent_unavailable_segments": 0,
                "weapon_modes": Counter(),
            }
        return class_stats[hero_class]

    fd, temporary_name = tempfile.mkstemp(
        prefix=".catalog.", suffix=".jsonl.gz.tmp", dir=output_dir
    )
    os.close(fd)
    temporary_catalog = Path(temporary_name)
    try:
        with gzip.open(temporary_catalog, mode="wt", encoding="utf-8", newline="\n") as handle:
            for instance in instances:
                instance_count += 1
                segments = compile_instance_segments(
                    instance, coverage_registry=coverage_registry
                )
                seen_players: set[tuple[str, str, str]] = set()
                for segment in segments:
                    handle.write(_canonical_json(segment) + "\n")
                    if prefix_segments is not None:
                        prefix_segments.append(segment)
                    segment_count += 1
                    identity = segment["identity"]
                    player_key = (
                        identity["server"],
                        identity["realm"],
                        identity["player_guid"],
                    )
                    unique_players.add(player_key)
                    seen_players.add(player_key)
                    observation = segment["observation"]
                    hero_class = str(
                        segment["player"].get("hero_class") or "UNKNOWN"
                    ).upper()
                    class_row = stats_for(hero_class)
                    class_row["players"].add(player_key)
                    class_row["segments"] += 1
                    class_row["runtime_executable_segments"] += int(
                        segment["coverage"]["runtime_executable"]
                    )
                    representativeness = segment["coverage"][
                        "historical_representativeness"
                    ]
                    class_row["representative_equipment_segments"] += int(
                        representativeness["equipment_plausible"]
                    )
                    class_row["representative_build_segments"] += int(
                        segment["coverage"]["representative_build_eligible"]
                    )
                    class_row["development_build_segments"] += int(
                        segment["coverage"]["development_build_eligible"]
                    )
                    class_row["comparison_eligible_segments"] += int(
                        segment["coverage"]["comparison"]["eligible"]
                    )
                    observation_counts[observation["status"]] += 1
                    source_message_count += observation["source_message_count"]
                    duplicate_message_count += observation[
                        "duplicate_identical_message_count"
                    ]
                    unanchored_message_count += observation[
                        "unanchored_message_count_for_player"
                    ]
                    translation_counts[segment["talents"]["translation_status"]] += 1
                    runtime_executable_count += int(
                        segment["coverage"]["runtime_executable"]
                    )
                    representative_equipment_count += int(
                        representativeness["equipment_plausible"]
                    )
                    representative_build_count += int(
                        segment["coverage"]["representative_build_eligible"]
                    )
                    development_build_count += int(
                        segment["coverage"]["development_build_eligible"]
                    )
                    comparison_eligible_count += int(
                        segment["coverage"]["comparison"]["eligible"]
                    )
                    representative_reason_counts.update(
                        representativeness["reasons"]
                    )
                    slots = segment["equipment"]["slots"]
                    if observation["status"] == "OBSERVED_CAUSAL_PREFIX":
                        class_row["causal_segments"] += 1
                        class_row["weapon_modes"][_weapon_mode(segment)] += 1
                        equipment_signature = _semantic_signature(
                            segment, "equipment"
                        )
                        if equipment_signature is not None:
                            equipment_signatures.add(equipment_signature)
                            class_row["equipment_signatures"].add(
                                equipment_signature
                            )
                            if all(
                                slot["status"]
                                in {OBSERVED_EQUIPPED, OBSERVED_EMPTY}
                                for slot in slots
                            ):
                                fully_observed_equipment_signatures.add(
                                    equipment_signature
                                )
                        talent_signature = _semantic_signature(segment, "talents")
                        if talent_signature is not None:
                            talent_signatures.add(talent_signature)
                            class_row["talent_signatures"].add(talent_signature)
                        if (
                            segment["talents"]["translation_status"]
                            == "TRANSLATED_EXACT"
                        ):
                            class_row["semantic_talent_signatures"].add(
                                _canonical_json(
                                    [
                                        {
                                            "talent_id": rank.get("talent_id"),
                                            "rank": rank.get("rank"),
                                        }
                                        for rank in segment["talents"][
                                            "semantic_ranks"
                                        ]
                                    ]
                                )
                            )
                        else:
                            class_row[
                                "semantic_talent_unavailable_segments"
                            ] += 1
                    for slot in slots:
                        slot_status_counts[str(slot["inventory_slot"])][slot["status"]] += 1
                        coverage = slot.get("coverage")
                        if coverage:
                            item = coverage["item"]
                            item_definition_counts[item["definition_status"]] += 1
                            item_effect_counts[item["effect_status"]] += 1
                            if item["definition_status"] == "UNKNOWN_TO_REGISTRY":
                                unknown_item_ids.add(slot["item_id"])
                            elif item["definition_status"] == "NOT_EVALUATED_NO_REGISTRY":
                                unevaluated_item_ids.add(slot["item_id"])
                            for enchant in coverage["enchants"].values():
                                if enchant["definition_status"] == "UNKNOWN_TO_REGISTRY":
                                    unknown_enchant_ids.add(enchant["id"])
                                elif enchant["definition_status"] == "NOT_EVALUATED_NO_REGISTRY":
                                    unevaluated_enchant_ids.add(enchant["id"])
                            for enchant in coverage["gems"]:
                                if enchant["definition_status"] == "UNKNOWN_TO_REGISTRY":
                                    unknown_enchant_ids.add(enchant["id"])
                                elif enchant["definition_status"] == "NOT_EVALUATED_NO_REGISTRY":
                                    unevaluated_enchant_ids.add(enchant["id"])
                    offhand = slots[16]
                    mainhand = slots[15]
                    if offhand["status"] == OBSERVED_EMPTY:
                        offhand_empty_count += 1
                        item_coverage = (mainhand.get("coverage") or {}).get("item") or {}
                        weapon_mode = item_coverage.get("weapon_mode")
                        if weapon_mode is not None:
                            legal_empty_offhand_evaluated_count += 1
                            legal_empty_offhand_count += int(weapon_mode == "TWO_HAND")
                    talents = segment["talents"]
                    if talents["translation_status"] != "TRANSLATED_EXACT":
                        trees = talents.get("original_tree_rank_strings")
                        if isinstance(trees, list):
                            hero_class = str(segment["player"].get("hero_class") or "UNKNOWN")
                            for tree_index, tree in enumerate(trees):
                                if not isinstance(tree, str):
                                    continue
                                for position, character in enumerate(tree):
                                    if character.isdigit() and character != "0":
                                        untranslated_positions.add(
                                            f"{hero_class}:{tree_index}:{position}"
                                        )
                for player_key in seen_players:
                    player_instances[player_key] += 1
        os.replace(temporary_catalog, final_catalog)
    except Exception:
        temporary_catalog.unlink(missing_ok=True)
        raise

    total_slot_observations = max(segment_count, 1)
    slot_coverage = {
        slot: {
            "counts": dict(sorted(counts.items())),
            "observed_rate": (
                counts[OBSERVED_EQUIPPED] + counts[OBSERVED_EMPTY]
            )
            / total_slot_observations,
        }
        for slot, counts in slot_status_counts.items()
    }
    by_hero_class = {}
    for hero_class, raw in sorted(class_stats.items()):
        semantic_count = len(raw["semantic_talent_signatures"])
        by_hero_class[hero_class] = {
            "unique_player_count": len(raw["players"]),
            "build_segment_count": raw["segments"],
            "causal_build_segment_count": raw["causal_segments"],
            "runtime_executable_segment_count": raw[
                "runtime_executable_segments"
            ],
            "representative_equipment_segment_count": raw[
                "representative_equipment_segments"
            ],
            "representative_build_segment_count": raw[
                "representative_build_segments"
            ],
            "development_build_segment_count": raw[
                "development_build_segments"
            ],
            "comparison_eligible_segment_count": raw[
                "comparison_eligible_segments"
            ],
            "unique_equipment_signature_count": len(
                raw["equipment_signatures"]
            ),
            "unique_talent_signature_count": len(raw["talent_signatures"]),
            "weapon_mode_counts": {
                mode: raw["weapon_modes"].get(mode, 0)
                for mode in (
                    "TWO_HAND",
                    "DUAL_WIELD",
                    "ONE_HAND_NO_OFFHAND",
                    "UNKNOWN",
                )
            },
            "semantic_talent_combinations": {
                "status": (
                    "AVAILABLE_FROM_PINNED_TRANSLATION"
                    if semantic_count
                    else "NOT_AVAILABLE_NO_TRANSLATION"
                ),
                "unique_signature_count": semantic_count,
                "unavailable_causal_segment_count": raw[
                    "semantic_talent_unavailable_segments"
                ],
            },
        }
    manifest = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": "historical_build_catalog_manifest",
        "created_at": _utc_now(),
        "catalog_path": str(final_catalog),
        "inputs": {
            "admission_manifest": (
                str(Path(source_admission_manifest).expanduser().resolve())
                if source_admission_manifest is not None
                else "IN_MEMORY_INSTANCE_INPUTS"
            ),
            "coverage_registry": (
                str(Path(coverage_registry_source).expanduser().resolve())
                if coverage_registry_source is not None
                else "NONE_OR_IN_MEMORY"
            ),
            "coverage_registry_identity": (
                {
                    "schema": coverage_registry.get("schema"),
                    "implementation_revision": coverage_registry.get(
                        "implementation_revision"
                    ),
                    "created_at": coverage_registry.get("created_at"),
                    "item_dataset": coverage_registry.get("item_dataset"),
                }
                if coverage_registry is not None
                else None
            ),
        },
        "source_contract": {
            "admission_required_for_default_loader": True,
            "combatant_decoder": "o2o_dps/chronicle_combatant_sidecar.py",
            "large_normalized_event_partitions_opened": False,
            "processing_unit": "ONE_INSTANCE_AT_A_TIME",
        },
        "causal_contract": {
            "policy_join": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY",
            "future_info_backfill_allowed": False,
            "retrospective_valid_until_is_policy_input": False,
            "missing_initial_state_preserved": True,
            "unanchored_info_is_policy_input": False,
        },
        "summary": {
            "instance_count": instance_count,
            "unique_player_count": len(unique_players),
            "player_instance_count": sum(player_instances.values()),
            "players_in_multiple_instances": sum(
                count > 1 for count in player_instances.values()
            ),
            "player_instance_count_histogram": dict(
                sorted(
                    Counter(player_instances.values()).items()
                )
            ),
            "build_segment_count": segment_count,
            "source_combatant_info_message_count": source_message_count,
            "duplicate_identical_message_count": duplicate_message_count,
            "unanchored_message_count": unanchored_message_count,
            "unique_equipment_signature_count": len(equipment_signatures),
            "unique_equipment_signature_scope": (
                "CAUSALLY_ANCHORED_COMBATANT_INFO_INCLUDING_EXPLICIT_PARTIAL_SLOT_STATE"
            ),
            "unique_fully_observed_equipment_signature_count": len(
                fully_observed_equipment_signatures
            ),
            "unique_talent_signature_count": len(talent_signatures),
            "unique_talent_signature_scope": "CAUSALLY_ANCHORED_COMBATANT_INFO_ONLY",
            "observation_status_counts": dict(sorted(observation_counts.items())),
            "talent_translation_status_counts": dict(
                sorted(translation_counts.items())
            ),
            "slot_coverage": slot_coverage,
            "observed_empty_offhand_count": offhand_empty_count,
            "legal_empty_offhand": {
                "evaluated_count": legal_empty_offhand_evaluated_count,
                "legal_count": legal_empty_offhand_count,
                "rate": (
                    legal_empty_offhand_count / legal_empty_offhand_evaluated_count
                    if legal_empty_offhand_evaluated_count
                    else None
                ),
                "status": (
                    "EVALUATED_FROM_PINNED_ITEM_WEAPON_MODE"
                    if legal_empty_offhand_evaluated_count
                    else "NOT_EVALUATED_WITHOUT_PINNED_ITEM_WEAPON_MODE"
                ),
            },
            "item_definition_status_counts": dict(
                sorted(item_definition_counts.items())
            ),
            "item_effect_status_counts": dict(sorted(item_effect_counts.items())),
            "unknown_item_ids": sorted(unknown_item_ids),
            "unknown_enchant_ids": sorted(unknown_enchant_ids),
            "item_ids_not_evaluated_without_registry": sorted(
                unevaluated_item_ids
            ),
            "enchant_ids_not_evaluated_without_registry": sorted(
                unevaluated_enchant_ids
            ),
            "unknown_talent_ids": None,
            "unknown_talent_ids_reason": (
                "COMBATANT_INFO_EXPOSES_TREE_POSITIONS_NOT_TALENT_IDS"
            ),
            "untranslated_nonzero_talent_positions": sorted(
                untranslated_positions
            ),
            "runtime_executable_segment_count": runtime_executable_count,
            "representative_equipment_segment_count": representative_equipment_count,
            "representative_build_segment_count": representative_build_count,
            "development_build_segment_count": development_build_count,
            "comparison_eligible_segment_count": comparison_eligible_count,
            "representative_build_reason_counts": dict(
                sorted(representative_reason_counts.items())
            ),
            "by_hero_class": by_hero_class,
            "strict_prefix_join": (
                evaluate_prefix_joins(prefix_segments or (), prefix_queries)
                if prefix_queries is not None
                else {
                    "status": "NOT_COMPUTED_NO_DECISION_QUERY_STREAM",
                    "query_count": 0,
                    "joined_count": 0,
                    "rate": None,
                    "future_info_backfill_count": 0,
                }
            ),
        },
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=".manifest.", suffix=".json.tmp", dir=output_dir
    )
    os.close(fd)
    temporary_manifest = Path(temporary_name)
    try:
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_manifest, final_manifest)
    except Exception:
        temporary_manifest.unlink(missing_ok=True)
        raise
    return {**manifest, "manifest_path": str(final_manifest)}


def build_historical_build_catalog(
    *,
    admission_manifest: str | Path = DEFAULT_ADMISSION_MANIFEST,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    coverage_registry_path: str | Path | None = None,
) -> dict[str, Any]:
    registry = _load_coverage_registry(coverage_registry_path)
    instances = iter_admitted_instance_inputs(
        admission_manifest, data_root=data_root
    )
    result = build_catalog_from_instances(
        instances,
        output_directory=output_directory,
        coverage_registry=registry,
        source_admission_manifest=admission_manifest,
        coverage_registry_source=coverage_registry_path,
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the causal Chronicle historical build catalogue v1"
    )
    parser.add_argument("--admission-manifest", default=str(DEFAULT_ADMISSION_MANIFEST))
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--output-directory", default=str(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument("--coverage-registry")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_build_catalog(
        admission_manifest=args.admission_manifest,
        data_root=args.data_root,
        output_directory=args.output_directory,
        coverage_registry_path=args.coverage_registry,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
