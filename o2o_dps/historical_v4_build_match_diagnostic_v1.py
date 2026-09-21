"""Read-only, first-segment historical build versus a controlled v4 request."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "historical_v4_build_match_diagnostic/v1"

# Wowsims equipment.items order, expressed as Chronicle inventory slots.
_REQUEST_SLOT_ORDER = (1, 2, 3, 15, 5, 9, 10, 6, 7, 8, 11, 12, 13, 14, 16, 17, 18)


def _jsonl_rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            yield line_number, json.loads(line)


def _talent_string(parts: list[str] | str) -> str:
    if isinstance(parts, str):
        parts = parts.split("-")
    normalized = [part.rstrip("0") for part in parts]
    while normalized and not normalized[-1]:
        normalized.pop()
    return "-".join(normalized)


def _race_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return (value[4:] if value.startswith("Race") else value).casefold()


def diagnose_historical_v4_build_match_v1(
    *,
    instance_id: str,
    encounter_id: str,
    focal_guid: str,
    controlled_request: Mapping[str, Any],
    join_manifest_path: Path,
    catalog_path: Path,
    wave_reconstruction_path: Path | None = None,
    controlled_stop_ms: int | None = None,
) -> dict[str, Any]:
    """Resolve the first exact causal build; do not imply simulator admissibility."""

    manifest = json.loads(join_manifest_path.read_text(encoding="utf-8"))
    partition = next(
        (row for row in manifest["mapping_partitions"] if row["instance_id"] == instance_id),
        None,
    )
    if partition is None:
        raise ValueError(f"instance has no build-join partition: {instance_id}")
    mapping_path = join_manifest_path.parent / partition["path"]
    selected = next(
        (
            row for _, row in _jsonl_rows(mapping_path)
            if row["source"]["encounter_id"] == encounter_id
            and row["source"]["player_guid"].lower() == focal_guid.lower()
        ),
        None,
    )
    if selected is None:
        raise ValueError("encounter/focal has no historical decision-build mapping")
    first_run = selected["binding_runs"][0]
    if first_run["join_status"] != "JOINED_EXACT_CAUSAL_PREFIX":
        raise ValueError(f"first build is not an exact causal join: {first_run['join_status']}")
    segment_ref = first_run["segment_ref"]
    dictionary_path = join_manifest_path.parent / manifest["segment_dictionary"]["partition"]["path"]
    reference = next(
        (row for _, row in _jsonl_rows(dictionary_path) if row["segment_ref"] == segment_ref),
        None,
    )
    if reference is None:
        raise ValueError(f"segment reference missing from dictionary: {segment_ref}")
    catalog_line = reference["catalog_line_number"]
    segment = next(
        (row for line_number, row in _jsonl_rows(catalog_path) if line_number == catalog_line),
        None,
    )
    if segment is None:
        raise ValueError(f"catalog line missing: {catalog_line}")

    player = controlled_request["raid"]["parties"][0]["players"][0]
    request_items = player["equipment"]["items"]
    if len(request_items) != len(_REQUEST_SLOT_ORDER):
        raise ValueError("controlled request does not use the 17-slot Wowsims layout")
    historical_slots = {row["inventory_slot"]: row for row in segment["equipment"]["slots"]}
    item_differences = []
    for request_index, inventory_slot in enumerate(_REQUEST_SLOT_ORDER):
        historical = historical_slots.get(inventory_slot)
        controlled = request_items[request_index]
        historical_id = historical["item_id"] if historical is not None else None
        controlled_id = controlled.get("id", 0)
        historical_enchant = historical["permanent_enchant_id"] if historical is not None else None
        controlled_enchant = controlled.get("enchant")
        if historical_id != controlled_id or historical_enchant != controlled_enchant:
            item_differences.append({
                "slot": historical["slot_name"] if historical is not None else inventory_slot,
                "historical_item_id": historical_id,
                "controlled_item_id": controlled_id,
                "historical_enchant_id": historical_enchant,
                "controlled_enchant_id": controlled_enchant,
            })

    # An equipped off-hand weapon proves dual wield for this controlled request;
    # an empty off-hand alone does not prove the main hand is two-handed.
    controlled_weapon_mode = "DUAL_WIELD" if request_items[15].get("id", 0) else "UNRESOLVED_NO_OFFHAND"
    talents = segment["talents"]
    historical_talents = _talent_string(talents["original_tree_rank_strings"])
    controlled_talents = _talent_string(player["talentsString"])
    bloodthirst_rank = (
        next(
            (
                row["rank"] for row in talents.get("semantic_ranks", [])
                if row.get("talent_id") == "warrior.bloodthirst"
            ),
            0,
        )
        if talents["translation_status"] == "TRANSLATED_EXACT" else None
    )
    coverage = segment["coverage"]
    historical_race = segment.get("player", {}).get("race")
    controlled_race = player.get("race")
    temporal_binding = None
    if wave_reconstruction_path is not None:
        with gzip.open(wave_reconstruction_path, "rt", encoding="utf-8") as source:
            reconstruction = json.load(source)
        wave = next(
            (row for row in reconstruction["waves"] if row["wave_id"] == selected["source"]["wave_id"]),
            None,
        )
        if wave is None:
            raise ValueError("selected historical wave is absent from reconstruction")
        wave_start = wave["window"]["first_anchor"]["timestamp_ms"]
        valid_from = segment["observation"]["valid_from"]["timestamp_ms"]
        valid_until = segment["observation"]["valid_until_or_unknown"]
        valid_until_ms = valid_until["timestamp_ms"] if valid_until is not None else None
        next_start = next(
            (
                row["timestamp_ms"] for row in selected["decision_bindings"]
                if row["decision_ordinal"] == first_run["last_decision_ordinal"] + 1
            ),
            None,
        )
        temporal_binding = {
            "wave_start_timestamp_ms": wave_start,
            "first_segment_valid_from_offset_ms": valid_from - wave_start,
            "first_segment_valid_until_offset_ms": (
                valid_until_ms - wave_start if valid_until_ms is not None else None
            ),
            "next_segment_first_historical_start_offset_ms": (
                next_start - wave_start if next_start is not None else None
            ),
            "controlled_stop_ms": controlled_stop_ms,
            "first_segment_covers_controlled_window": (
                valid_from <= wave_start
                and controlled_stop_ms is not None
                and (valid_until_ms is None or wave_start + controlled_stop_ms <= valid_until_ms)
            ),
        }
    return {
        "schema": SCHEMA,
        "source": {
            "instance_id": instance_id,
            "encounter_id": encounter_id,
            "focal_guid": focal_guid,
            "historical_start_count": selected["controllable_start_count"],
            "first_segment_historical_start_ordinals": [
                first_run["first_decision_ordinal"], first_run["last_decision_ordinal"]
            ],
            "segment_id": segment["identity"]["build_segment_id"],
            "catalog_line_number": catalog_line,
        },
        "historical_build": {
            "race": historical_race,
            "weapon_mode": reference["weapon_mode"],
            "talent_translation_status": talents["translation_status"],
            "talent_summary": talents["original_summary"],
            "bloodthirst_talent_rank": bloodthirst_rank,
            "observed_equipped_slot_count": sum(
                slot["status"] == "OBSERVED_EQUIPPED" for slot in historical_slots.values()
            ),
            "runtime_executable": coverage["runtime_executable"],
            "simulator_blockers": coverage["simulator_representation"]["reasons"],
            "comparison_eligible": coverage["comparison"]["eligible"],
        },
        "controlled_request": {
            "race": controlled_race,
            "weapon_mode": controlled_weapon_mode,
            "talents_string": player["talentsString"],
        },
        "temporal_binding": temporal_binding,
        "comparison": {
            "race_equal": _race_name(historical_race) == _race_name(controlled_race),
            "weapon_mode_equal": reference["weapon_mode"] == controlled_weapon_mode,
            "talent_string_equal": historical_talents == controlled_talents,
            "equipment_item_or_enchant_difference_count": len(item_differences),
            "equipment_item_or_enchant_differences": item_differences,
            "same_historical_build_as_controlled_request": (
                _race_name(historical_race) == _race_name(controlled_race)
                and reference["weapon_mode"] == controlled_weapon_mode
                and historical_talents == controlled_talents
                and not item_differences
            ),
        },
    }
