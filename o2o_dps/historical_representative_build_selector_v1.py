"""Select exact historical Warrior build representatives from catalog v1.

The selector consumes the compact historical build catalogue as a stream.  It
never opens Chronicle combat-event partitions and it never manufactures an
average loadout: every selected representative embeds one exact catalogue
segment.  Repeated INFO messages and repeated uploads do not increase a
player's mass.  Each exact player contributes total mass one, split uniformly
over that player's distinct eligible builds.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
import gzip
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "historical_representative_build_selection/v1"
REPRESENTATIVE_SCHEMA = "historical_representative_build/v1"
CATALOG_SCHEMA = "historical_build_catalog/v1"
CATALOG_RECORD_SCHEMA = "historical_build_segment/v1"
IMPLEMENTATION_REVISION = "v1.1_player_weighted_exact_k_medoids_build"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_build_catalog"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_representative_build_selector"
    / "v1"
)

EXPECTED_SLOT_IDS = frozenset(range(1, 20))
COSMETIC_SLOT_IDS = frozenset({4, 19})
COMBAT_SLOT_IDS = EXPECTED_SLOT_IDS - COSMETIC_SLOT_IDS
REQUIRED_EQUIPPED_SLOT_IDS = COMBAT_SLOT_IDS - {17}
ALLOWED_SLOT_STATUSES = frozenset(
    {"OBSERVED_EQUIPPED", "OBSERVED_EMPTY", "MISSING", "AMBIGUOUS"}
)


class HistoricalRepresentativeBuildSelectorError(RuntimeError):
    """The source catalogue or an eligible exact-build feature is invalid."""


@dataclass
class _Candidate:
    canonical_key: str
    features: dict[str, Any]
    exemplar: dict[str, Any]
    exemplar_line_number: int
    player_keys: set[tuple[str, str, str]] = field(default_factory=set)
    source_segment_count: int = 0
    player_mass: Fraction = Fraction(0, 1)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalRepresentativeBuildSelectorError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalRepresentativeBuildSelectorError(f"{label} must be an array")
    return value


def _trimmed_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HistoricalRepresentativeBuildSelectorError(
            f"{label} must be a non-empty trimmed string"
        )
    return value


def _integer(value: Any, *, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalRepresentativeBuildSelectorError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise HistoricalRepresentativeBuildSelectorError(
            f"{label} must be >= {minimum}"
        )
    return value


def _optional_integer(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label=label, minimum=0)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_manifest(path: str | Path) -> tuple[Path, Mapping[str, Any], Path]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalRepresentativeBuildSelectorError(
            f"cannot read catalogue manifest {manifest_path}: {error}"
        ) from error
    manifest = _mapping(document, label="catalogue manifest")
    if manifest.get("schema") != CATALOG_SCHEMA:
        raise HistoricalRepresentativeBuildSelectorError(
            f"catalogue manifest schema must be {CATALOG_SCHEMA}"
        )
    raw_catalog_path = _trimmed_text(
        manifest.get("catalog_path"), label="catalogue manifest catalog_path"
    )
    catalog_path = Path(raw_catalog_path).expanduser()
    if not catalog_path.is_absolute():
        catalog_path = manifest_path.parent / catalog_path
    catalog_path = catalog_path.resolve()
    if not catalog_path.is_file():
        raise HistoricalRepresentativeBuildSelectorError(
            f"catalogue data does not exist: {catalog_path}"
        )
    return manifest_path, manifest, catalog_path


def _player_key(segment: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = _mapping(segment.get("identity"), label="segment identity")
    return (
        _trimmed_text(identity.get("server"), label="identity.server"),
        _trimmed_text(identity.get("realm"), label="identity.realm"),
        _trimmed_text(identity.get("player_guid"), label="identity.player_guid"),
    )


def _source_order_key(segment: Mapping[str, Any]) -> tuple[Any, ...]:
    identity = _mapping(segment.get("identity"), label="segment identity")
    observation = _mapping(segment.get("observation"), label="segment observation")
    anchor = observation.get("valid_from")
    if anchor is None:
        anchor_key: tuple[Any, ...] = (-1, "", -1, -1)
    else:
        anchor = _mapping(anchor, label="segment valid_from")
        anchor_key = (
            _integer(anchor.get("timestamp_ms"), label="valid_from.timestamp_ms"),
            _trimmed_text(anchor.get("encounter_id"), label="valid_from.encounter_id"),
            _integer(anchor.get("event_index"), label="valid_from.event_index"),
            _integer(anchor.get("message_ordinal"), label="valid_from.message_ordinal"),
        )
    return (
        _trimmed_text(identity.get("server"), label="identity.server"),
        _trimmed_text(identity.get("realm"), label="identity.realm"),
        _trimmed_text(identity.get("player_guid"), label="identity.player_guid"),
        _trimmed_text(identity.get("instance_id"), label="identity.instance_id"),
        _trimmed_text(identity.get("build_segment_id"), label="identity.build_segment_id"),
        *anchor_key,
    )


def _coverage_shape(
    coverage: Any,
    *,
    label: str,
    identifier: int | str,
    kind: str,
    slot: int | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    row = _mapping(coverage, label=label)
    definition_status = row.get("definition_status")
    effect_status = row.get("effect_status")
    if definition_status is None:
        definition_status = "NOT_DECLARED"
    else:
        definition_status = _trimmed_text(
            definition_status, label=f"{label}.definition_status"
        )
    if effect_status is None:
        effect_status = "NOT_DECLARED"
    else:
        effect_status = _trimmed_text(
            effect_status, label=f"{label}.effect_status"
        )
    raw_scopes = row.get("calibrated_scopes", [])
    scopes = _array(raw_scopes, label=f"{label}.calibrated_scopes")
    if not all(isinstance(value, str) and value and value == value.strip() for value in scopes):
        raise HistoricalRepresentativeBuildSelectorError(
            f"{label}.calibrated_scopes must contain non-empty trimmed strings"
        )
    shaped: dict[str, Any] = {
        "kind": kind,
        "id": identifier,
        "definition_status": definition_status,
        "effect_status": effect_status,
        "calibrated_scopes": sorted(set(scopes)),
    }
    if slot is not None:
        shaped["inventory_slot"] = slot
    if rank is not None:
        shaped["rank"] = rank
    weapon_mode = row.get("weapon_mode")
    if weapon_mode is not None:
        shaped["weapon_mode"] = _trimmed_text(
            weapon_mode, label=f"{label}.weapon_mode"
        )
    return shaped


def _exact_features(segment: Mapping[str, Any]) -> dict[str, Any]:
    if segment.get("schema") != CATALOG_RECORD_SCHEMA:
        raise HistoricalRepresentativeBuildSelectorError(
            f"eligible segment schema must be {CATALOG_RECORD_SCHEMA}"
        )
    player = _mapping(segment.get("player"), label="segment player")
    if player.get("hero_class") != "WARRIOR":
        raise HistoricalRepresentativeBuildSelectorError(
            "eligible representative segment must be WARRIOR"
        )
    coverage = _mapping(segment.get("coverage"), label="segment coverage")
    development = _mapping(coverage.get("development"), label="coverage.development")
    if (
        coverage.get("development_build_eligible") is not True
        or development.get("eligible") is not True
        or coverage.get("runtime_executable") is not True
        or coverage.get("representative_build_eligible") is not True
    ):
        raise HistoricalRepresentativeBuildSelectorError(
            "eligible segment has inconsistent development/runtime/representative gates"
        )

    equipment = _mapping(segment.get("equipment"), label="segment equipment")
    slots = _array(equipment.get("slots"), label="equipment.slots")
    if len(slots) != len(EXPECTED_SLOT_IDS):
        raise HistoricalRepresentativeBuildSelectorError(
            "equipment.slots must contain the canonical 19-slot vector"
        )
    slots_by_id: dict[int, Mapping[str, Any]] = {}
    slot_vector: list[dict[str, Any]] = []
    mechanics: list[dict[str, Any]] = []
    for raw_slot in slots:
        slot = _mapping(raw_slot, label="equipment slot")
        slot_id = _integer(
            slot.get("inventory_slot"), label="equipment slot inventory_slot", minimum=1
        )
        if slot_id not in EXPECTED_SLOT_IDS or slot_id in slots_by_id:
            raise HistoricalRepresentativeBuildSelectorError(
                "equipment slots must have each inventory slot 1..19 exactly once"
            )
        slots_by_id[slot_id] = slot
    if set(slots_by_id) != EXPECTED_SLOT_IDS:
        raise HistoricalRepresentativeBuildSelectorError(
            "equipment slots must have each inventory slot 1..19 exactly once"
        )

    for slot_id in sorted(slots_by_id):
        slot = slots_by_id[slot_id]
        status = _trimmed_text(slot.get("status"), label=f"slot {slot_id} status")
        if status not in ALLOWED_SLOT_STATUSES:
            raise HistoricalRepresentativeBuildSelectorError(
                f"slot {slot_id} has unsupported status {status}"
            )
        if slot_id in REQUIRED_EQUIPPED_SLOT_IDS and status != "OBSERVED_EQUIPPED":
            raise HistoricalRepresentativeBuildSelectorError(
                f"development-eligible core slot {slot_id} must be observed equipped"
            )
        if slot_id == 17 and status not in {"OBSERVED_EQUIPPED", "OBSERVED_EMPTY"}:
            raise HistoricalRepresentativeBuildSelectorError(
                "development-eligible off-hand must be observed equipped or empty"
            )
        item_id = _optional_integer(slot.get("item_id"), label=f"slot {slot_id} item_id")
        permanent = _optional_integer(
            slot.get("permanent_enchant_id"),
            label=f"slot {slot_id} permanent_enchant_id",
        )
        temporary = _optional_integer(
            slot.get("temporary_enchant_id"),
            label=f"slot {slot_id} temporary_enchant_id",
        )
        raw_gems = _array(slot.get("gem_enchant_ids", []), label=f"slot {slot_id} gems")
        gems = [
            _integer(value, label=f"slot {slot_id} gem enchant", minimum=0)
            for value in raw_gems
        ]
        random_suffix = _optional_integer(
            slot.get("random_suffix"), label=f"slot {slot_id} random_suffix"
        )
        slot_atom = {
            "inventory_slot": slot_id,
            "slot_name": _trimmed_text(
                slot.get("slot_name"), label=f"slot {slot_id} slot_name"
            ),
            "status": status,
            "item_id": item_id,
            "permanent_enchant_id": permanent,
            "temporary_enchant_id": temporary,
            "gem_enchant_ids": gems,
            "random_suffix": random_suffix,
        }
        slot_vector.append(slot_atom)
        if status != "OBSERVED_EQUIPPED":
            continue
        if item_id is None or item_id <= 0:
            raise HistoricalRepresentativeBuildSelectorError(
                f"equipped slot {slot_id} must have a positive item_id"
            )
        slot_coverage = _mapping(slot.get("coverage"), label=f"slot {slot_id} coverage")
        if slot_id in COMBAT_SLOT_IDS:
            mechanics.append(
                _coverage_shape(
                    slot_coverage.get("item"),
                    label=f"slot {slot_id} item coverage",
                    identifier=item_id,
                    kind="ITEM",
                    slot=slot_id,
                )
            )
        enchant_coverage = _mapping(
            slot_coverage.get("enchants", {}),
            label=f"slot {slot_id} enchant coverage",
        )
        for enchant_kind, enchant_id in (
            ("permanent", permanent),
            ("temporary", temporary),
        ):
            if enchant_id in (None, 0):
                continue
            if slot_id in COMBAT_SLOT_IDS:
                mechanics.append(
                    _coverage_shape(
                        enchant_coverage.get(enchant_kind),
                        label=f"slot {slot_id} {enchant_kind} enchant coverage",
                        identifier=int(enchant_id),
                        kind=f"{enchant_kind.upper()}_ENCHANT",
                        slot=slot_id,
                    )
                )
        gem_coverages = _array(
            slot_coverage.get("gems", []), label=f"slot {slot_id} gem coverage"
        )
        if len(gem_coverages) != len([value for value in gems if value > 0]):
            raise HistoricalRepresentativeBuildSelectorError(
                f"slot {slot_id} gem coverage count differs from nonzero gem ids"
            )
        for gem_id, gem_coverage in zip(
            (value for value in gems if value > 0), gem_coverages
        ):
            if slot_id in COMBAT_SLOT_IDS:
                mechanics.append(
                    _coverage_shape(
                        gem_coverage,
                        label=f"slot {slot_id} gem enchant coverage",
                        identifier=gem_id,
                        kind="GEM_ENCHANT",
                        slot=slot_id,
                    )
                )

    main_hand = slots_by_id[16]
    off_hand = slots_by_id[17]
    main_item_coverage = _mapping(
        _mapping(main_hand.get("coverage"), label="main-hand coverage").get("item"),
        label="main-hand item coverage",
    )
    main_mode = main_item_coverage.get("weapon_mode")
    if main_mode == "TWO_HAND" and off_hand.get("status") == "OBSERVED_EMPTY":
        weapon_mode = "TWO_HAND"
    elif main_mode == "ONE_HAND" and off_hand.get("status") == "OBSERVED_EQUIPPED":
        off_item_coverage = _mapping(
            _mapping(off_hand.get("coverage"), label="off-hand coverage").get("item"),
            label="off-hand item coverage",
        )
        weapon_mode = (
            "DUAL_WIELD"
            if off_item_coverage.get("weapon_mode") == "ONE_HAND"
            else "ONE_HAND_WITH_EQUIPPED_OFFHAND"
        )
    else:
        raise HistoricalRepresentativeBuildSelectorError(
            "development-eligible segment has no exact supported weapon mode"
        )

    talents = _mapping(segment.get("talents"), label="segment talents")
    if talents.get("translation_status") != "TRANSLATED_EXACT":
        raise HistoricalRepresentativeBuildSelectorError(
            "development-eligible segment talents must be TRANSLATED_EXACT"
        )
    raw_semantic = _array(talents.get("semantic_ranks"), label="talents.semantic_ranks")
    talent_vector: list[dict[str, Any]] = []
    seen_talents: set[str] = set()
    for raw_talent in raw_semantic:
        talent = _mapping(raw_talent, label="semantic talent")
        talent_id = _trimmed_text(talent.get("talent_id"), label="semantic talent id")
        if talent_id in seen_talents:
            raise HistoricalRepresentativeBuildSelectorError(
                f"duplicate semantic talent id: {talent_id}"
            )
        seen_talents.add(talent_id)
        rank = _integer(talent.get("rank"), label=f"talent {talent_id} rank", minimum=1)
        max_rank = _integer(
            talent.get("max_rank"), label=f"talent {talent_id} max_rank", minimum=1
        )
        if rank > max_rank:
            raise HistoricalRepresentativeBuildSelectorError(
                f"talent {talent_id} rank exceeds max_rank"
            )
        talent_atom = {
            "talent_id": talent_id,
            "profile_name": _trimmed_text(
                talent.get("profile_name"), label=f"talent {talent_id} profile_name"
            ),
            "rank": rank,
            "max_rank": max_rank,
            "tree_index": _integer(
                talent.get("tree_index"), label=f"talent {talent_id} tree_index", minimum=0
            ),
            "position": _integer(
                talent.get("position"), label=f"talent {talent_id} position", minimum=0
            ),
        }
        talent_vector.append(talent_atom)
        mechanics.append(
            _coverage_shape(
                talent,
                label=f"talent {talent_id} coverage",
                identifier=talent_id,
                kind="TALENT",
                rank=rank,
            )
        )
    talent_vector.sort(key=lambda row: row["talent_id"])
    mechanics.sort(key=_canonical_json)

    version = _mapping(segment.get("version"), label="segment version")
    simulator = _mapping(
        coverage.get("simulator_representation"),
        label="coverage.simulator_representation",
    )
    if simulator.get("runnable") is not True or simulator.get("reasons") not in ([], ()):
        raise HistoricalRepresentativeBuildSelectorError(
            "development-eligible segment simulator coverage is inconsistent"
        )
    return {
        "weapon_mode": weapon_mode,
        "equipment_slot_vector": slot_vector,
        "combat_equipment_slot_vector": [
            row for row in slot_vector if row["inventory_slot"] in COMBAT_SLOT_IDS
        ],
        "semantic_talent_rank_vector": talent_vector,
        "mechanics_coverage_vector": mechanics,
        "mechanics_versions": {
            "client_build": version.get("client_build"),
            "talent_tree_dataset": version.get("talent_tree_dataset"),
            "item_dataset": version.get("item_dataset"),
            "coverage_claim": coverage.get("coverage_claim"),
        },
    }


def _canonical_candidate_key(features: Mapping[str, Any]) -> str:
    """Identify a combat build while deliberately ignoring cosmetic slots."""

    return _canonical_json(
        {
            "weapon_mode": features["weapon_mode"],
            "combat_equipment_slot_vector": features[
                "combat_equipment_slot_vector"
            ],
            "semantic_talent_rank_vector": features[
                "semantic_talent_rank_vector"
            ],
            "mechanics_coverage_vector": features["mechanics_coverage_vector"],
            "mechanics_versions": features["mechanics_versions"],
        }
    )


def _talent_map(candidate: _Candidate) -> dict[str, tuple[int, int]]:
    return {
        row["talent_id"]: (row["rank"], row["max_rank"])
        for row in candidate.features["semantic_talent_rank_vector"]
    }


def _mechanics_tokens(candidate: _Candidate) -> frozenset[str]:
    values = [
        _canonical_json(row)
        for row in candidate.features["mechanics_coverage_vector"]
    ]
    values.append(
        _canonical_json({"versions": candidate.features["mechanics_versions"]})
    )
    return frozenset(values)


def _distance(left: _Candidate, right: _Candidate) -> tuple[Fraction, dict[str, Fraction]]:
    weapon = Fraction(int(left.features["weapon_mode"] != right.features["weapon_mode"]), 1)

    left_slots = left.features["combat_equipment_slot_vector"]
    right_slots = right.features["combat_equipment_slot_vector"]
    if len(left_slots) != len(right_slots) or not left_slots:
        raise HistoricalRepresentativeBuildSelectorError(
            "candidate combat equipment vectors are not comparable"
        )
    equipment = Fraction(
        sum(a != b for a, b in zip(left_slots, right_slots)), len(left_slots)
    )

    left_talents = _talent_map(left)
    right_talents = _talent_map(right)
    all_talent_ids = sorted(set(left_talents) | set(right_talents))
    talent_numerator = 0
    talent_denominator = 0
    for talent_id in all_talent_ids:
        left_rank, left_max = left_talents.get(talent_id, (0, 0))
        right_rank, right_max = right_talents.get(talent_id, (0, 0))
        if left_max and right_max and left_max != right_max:
            raise HistoricalRepresentativeBuildSelectorError(
                f"candidate talent max_rank differs for {talent_id}"
            )
        talent_numerator += abs(left_rank - right_rank)
        talent_denominator += max(left_max, right_max)
    talents = (
        Fraction(talent_numerator, talent_denominator)
        if talent_denominator
        else Fraction(0, 1)
    )

    left_mechanics = _mechanics_tokens(left)
    right_mechanics = _mechanics_tokens(right)
    mechanics_union = left_mechanics | right_mechanics
    mechanics = (
        Fraction(len(mechanics_union - (left_mechanics & right_mechanics)), len(mechanics_union))
        if mechanics_union
        else Fraction(0, 1)
    )
    components = {
        "weapon_mode": weapon,
        "equipment": equipment,
        "semantic_talents": talents,
        "mechanics_coverage": mechanics,
    }
    return sum(components.values(), Fraction(0, 1)) / len(components), components


def _fraction_payload(value: Fraction) -> dict[str, Any]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "value": float(value),
    }


def _select_representatives(
    candidates: Mapping[str, _Candidate], *, max_representatives: int
) -> tuple[list[_Candidate], dict[str, Any]]:
    if max_representatives <= 0:
        raise HistoricalRepresentativeBuildSelectorError(
            "max_representatives must be positive"
        )
    ordered = [candidates[key] for key in sorted(candidates)]
    if not ordered:
        return [], {
            "weighted_mean_nearest_distance": None,
            "maximum_nearest_distance": None,
            "assigned_build_count_by_rank": [],
            "assigned_player_mass_by_rank": [],
        }
    target_count = min(len(ordered), max_representatives)
    distance_cache: dict[tuple[str, str], Fraction] = {}

    def distance(left: _Candidate, right: _Candidate) -> Fraction:
        if left.canonical_key == right.canonical_key:
            return Fraction(0, 1)
        key = tuple(sorted((left.canonical_key, right.canonical_key)))
        cached = distance_cache.get(key)
        if cached is None:
            cached = _distance(left, right)[0]
            distance_cache[key] = cached
        return cached

    # The first representative is the exact weighted medoid: an observed build
    # minimizing total player-mass distance to every eligible observed build.
    # Sorted canonical order is the final deterministic tie-break.
    first = min(
        ordered,
        key=lambda proposed: (
            sum(
                (
                    candidate.player_mass * distance(candidate, proposed)
                    for candidate in ordered
                ),
                Fraction(0, 1),
            ),
            proposed.canonical_key,
        ),
    )
    selected = [first]
    selected_keys = {first.canonical_key}
    nearest = {
        candidate.canonical_key: distance(candidate, first)
        for candidate in ordered
    }

    # This is the deterministic BUILD phase of weighted k-medoids.  Each next
    # exact observed build is the facility that minimizes the new global
    # weighted nearest-medoid objective.  It therefore optimizes population
    # coverage rather than rewarding a rare point merely for being far away.
    while len(selected) < target_count:
        best: _Candidate | None = None
        best_objective: Fraction | None = None
        for proposed in ordered:
            if proposed.canonical_key in selected_keys:
                continue
            objective = sum(
                (
                    candidate.player_mass
                    * min(
                        nearest[candidate.canonical_key],
                        distance(candidate, proposed),
                    )
                    for candidate in ordered
                ),
                Fraction(0, 1),
            )
            if best_objective is None or objective < best_objective:
                best = proposed
                best_objective = objective
        assert best is not None
        selected.append(best)
        selected_keys.add(best.canonical_key)
        for candidate in ordered:
            nearest[candidate.canonical_key] = min(
                nearest[candidate.canonical_key], distance(candidate, best)
            )

    assigned_build_counts = [0 for _ in selected]
    assigned_player_mass = [Fraction(0, 1) for _ in selected]
    weighted_distance_sum = Fraction(0, 1)
    total_mass = sum((row.player_mass for row in ordered), Fraction(0, 1))
    maximum_distance = Fraction(0, 1)
    for candidate in ordered:
        distances = [distance(candidate, representative) for representative in selected]
        closest_rank = min(range(len(selected)), key=lambda index: (distances[index], index))
        closest_distance = distances[closest_rank]
        assigned_build_counts[closest_rank] += 1
        assigned_player_mass[closest_rank] += candidate.player_mass
        weighted_distance_sum += candidate.player_mass * closest_distance
        maximum_distance = max(maximum_distance, closest_distance)
    return selected, {
        "weighted_mean_nearest_distance": _fraction_payload(
            weighted_distance_sum / total_mass
        ),
        "maximum_nearest_distance": _fraction_payload(maximum_distance),
        "assigned_build_count_by_rank": assigned_build_counts,
        "assigned_player_mass_by_rank": [
            _fraction_payload(value) for value in assigned_player_mass
        ],
    }


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_jsonl_atomic(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(_canonical_json(row) + "\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def select_historical_representative_builds(
    *,
    catalog_manifest: str | Path = DEFAULT_CATALOG_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    max_representatives: int = 64,
) -> dict[str, Any]:
    """Stream catalogue v1 and write exact Warrior representatives or a blocker."""

    max_representatives = _integer(
        max_representatives, label="max_representatives", minimum=1
    )
    manifest_path, source_manifest, catalog_path = _load_manifest(catalog_manifest)
    source_summary = _mapping(source_manifest.get("summary"), label="catalogue summary")
    by_hero_class = _mapping(
        source_summary.get("by_hero_class"), label="catalogue summary by_hero_class"
    )
    warrior_summary = _mapping(
        by_hero_class.get("WARRIOR"), label="catalogue summary WARRIOR"
    )
    declared_development_count = _integer(
        warrior_summary.get("development_build_segment_count"),
        label="catalogue WARRIOR development_build_segment_count",
        minimum=0,
    )

    catalog_row_count = 0
    warrior_segment_count = 0
    eligible_flagged_count = 0
    eligible_feature_valid_count = 0
    selector_exclusions: Counter[str] = Counter()
    ineligible_segment_reasons: Counter[str] = Counter()
    ineligible_players_by_reason: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    candidates: dict[str, _Candidate] = {}
    player_builds: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    try:
        handle_context = gzip.open(catalog_path, mode="rt", encoding="utf-8")
        with handle_context as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                catalog_row_count += 1
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HistoricalRepresentativeBuildSelectorError(
                        f"catalogue line {line_number} is invalid JSON: {error}"
                    ) from error
                segment = _mapping(raw, label=f"catalogue line {line_number}")
                if segment.get("schema") != CATALOG_RECORD_SCHEMA:
                    raise HistoricalRepresentativeBuildSelectorError(
                        f"catalogue line {line_number} schema must be {CATALOG_RECORD_SCHEMA}"
                    )
                player = _mapping(segment.get("player"), label="segment player")
                if player.get("hero_class") != "WARRIOR":
                    continue
                warrior_segment_count += 1
                player_key = _player_key(segment)
                coverage = _mapping(segment.get("coverage"), label="segment coverage")
                if coverage.get("development_build_eligible") is not True:
                    development = _mapping(
                        coverage.get("development"), label="coverage.development"
                    )
                    raw_reasons = _array(
                        development.get("reasons", []),
                        label="coverage.development.reasons",
                    )
                    reasons = [
                        value
                        for value in raw_reasons
                        if isinstance(value, str) and value and value == value.strip()
                    ]
                    if len(reasons) != len(raw_reasons):
                        raise HistoricalRepresentativeBuildSelectorError(
                            "coverage.development.reasons contains an invalid reason"
                        )
                    if not reasons:
                        reasons = ["DEVELOPMENT_BUILD_NOT_ELIGIBLE_UNSPECIFIED"]
                    for reason in sorted(set(reasons)):
                        ineligible_segment_reasons[reason] += 1
                        ineligible_players_by_reason[reason].add(player_key)
                    continue

                eligible_flagged_count += 1
                try:
                    features = _exact_features(segment)
                except HistoricalRepresentativeBuildSelectorError as error:
                    selector_exclusions[str(error)] += 1
                    continue
                eligible_feature_valid_count += 1
                canonical_key = _canonical_candidate_key(features)
                player_builds[player_key].add(canonical_key)
                candidate = candidates.get(canonical_key)
                if candidate is None:
                    candidate = _Candidate(
                        canonical_key=canonical_key,
                        features=features,
                        exemplar=dict(segment),
                        exemplar_line_number=line_number,
                    )
                    candidates[canonical_key] = candidate
                elif _source_order_key(segment) < _source_order_key(candidate.exemplar):
                    candidate.exemplar = dict(segment)
                    candidate.exemplar_line_number = line_number
                candidate.player_keys.add(player_key)
                candidate.source_segment_count += 1
    except (OSError, UnicodeDecodeError) as error:
        raise HistoricalRepresentativeBuildSelectorError(
            f"cannot stream catalogue {catalog_path}: {error}"
        ) from error

    if declared_development_count != eligible_flagged_count:
        raise HistoricalRepresentativeBuildSelectorError(
            "catalogue manifest development count differs from streamed catalogue: "
            f"declared={declared_development_count}, streamed={eligible_flagged_count}"
        )

    for player_key, build_keys in player_builds.items():
        contribution = Fraction(1, len(build_keys))
        for build_key in build_keys:
            candidates[build_key].player_mass += contribution

    # An eligibility flag is a contract from the catalogue, not a hint.  If a
    # flagged row cannot be represented exactly, selecting from the remaining
    # subset would silently change the declared population.
    selected, assignment = (
        _select_representatives(candidates, max_representatives=max_representatives)
        if not selector_exclusions
        else (
            [],
            {
                "weighted_mean_nearest_distance": None,
                "maximum_nearest_distance": None,
                "assigned_build_count_by_rank": [],
                "assigned_player_mass_by_rank": [],
            },
        )
    )
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    representatives_path = output_directory / "representatives.jsonl"
    if selector_exclusions:
        status = "BLOCKED_INVALID_ELIGIBLE_SEGMENTS"
    elif selected:
        status = "READY"
    else:
        status = "BLOCKED_NO_ELIGIBLE_BUILDS"

    representative_rows: list[dict[str, Any]] = []
    if selected:
        for rank, candidate in enumerate(selected, start=1):
            if rank == 1:
                nearest_at_selection = Fraction(0, 1)
                selection_reason = "GLOBAL_PLAYER_WEIGHTED_MEDOID"
            else:
                nearest_at_selection = min(
                    _distance(candidate, prior)[0] for prior in selected[: rank - 1]
                )
                selection_reason = "GREEDY_PLAYER_WEIGHTED_K_MEDOIDS_BUILD"
            representative_rows.append(
                {
                    "schema": REPRESENTATIVE_SCHEMA,
                    "rank": rank,
                    "selection_reason": selection_reason,
                    "player_mass": _fraction_payload(candidate.player_mass),
                    "unique_supporting_player_count": len(candidate.player_keys),
                    "source_segment_count_not_used_as_weight": candidate.source_segment_count,
                    "nearest_prior_representative_distance": _fraction_payload(
                        nearest_at_selection
                    ),
                    "assigned_unique_build_count": assignment[
                        "assigned_build_count_by_rank"
                    ][rank - 1],
                    "assigned_player_mass": assignment["assigned_player_mass_by_rank"][
                        rank - 1
                    ],
                    "exact_features": candidate.features,
                    "historical_segment": candidate.exemplar,
                    "provenance": {
                        "catalog_manifest": str(manifest_path),
                        "catalog_path": str(catalog_path),
                        "catalog_line_number": candidate.exemplar_line_number,
                        "source_identity": candidate.exemplar.get("identity"),
                        "future_info_backfill_used": False,
                    },
                }
            )
        _write_jsonl_atomic(representatives_path, representative_rows)
    else:
        # A rerun that becomes blocked must not leave an earlier, apparently
        # usable representative set beside the new blocker manifest.
        representatives_path.unlink(missing_ok=True)

    top_level_blockers = [] if selected else [status]
    result = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": "historical_representative_build_selection_manifest",
        "created_at": _utc_now(),
        "status": status,
        "blockers": top_level_blockers,
        "inputs": {
            "catalog_manifest": str(manifest_path),
            "catalog_path": str(catalog_path),
            "catalog_identity": {
                "schema": source_manifest.get("schema"),
                "implementation_revision": source_manifest.get("implementation_revision"),
                "created_at": source_manifest.get("created_at"),
                "coverage_registry_identity": _mapping(
                    source_manifest.get("inputs"), label="catalogue inputs"
                ).get("coverage_registry_identity"),
            },
        },
        "selection_contract": {
            "hero_class": "WARRIOR",
            "eligibility_gate": "coverage.development_build_eligible == true",
            "algorithm": "DETERMINISTIC_PLAYER_WEIGHTED_K_MEDOIDS_BUILD",
            "first_representative": "MINIMUM_GLOBAL_PLAYER_WEIGHTED_DISTANCE_THEN_CANONICAL_FEATURE_ORDER",
            "subsequent_priority": "MINIMUM_GLOBAL_PLAYER_WEIGHTED_NEAREST_MEDOID_OBJECTIVE_THEN_CANONICAL_FEATURE_ORDER",
            "representatives_are_exact_catalog_segments": True,
            "synthetic_or_averaged_builds_allowed": False,
            "player_weighting": "EACH_SERVER_REALM_PLAYER_GUID_TOTAL_MASS_ONE_SPLIT_UNIFORMLY_ACROSS_DISTINCT_ELIGIBLE_BUILDS",
            "message_or_segment_frequency_used_as_weight": False,
            "feature_components": [
                "weapon_mode",
                "combat_equipment_item_enchant_slot_vector",
                "semantic_talent_rank_vector",
                "mechanics_coverage_and_versions",
            ],
            "component_distance": "EQUAL_MEAN_OF_WEAPON_MISMATCH_SLOT_HAMMING_NORMALIZED_TALENT_RANK_L1_AND_MECHANICS_JACCARD",
            "max_representatives": max_representatives,
            "selected_count_rule": "MIN(UNIQUE_ELIGIBLE_EXACT_BUILDS, MAX_REPRESENTATIVES)",
        },
        "summary": {
            "catalog_row_count": catalog_row_count,
            "warrior_segment_count": warrior_segment_count,
            "declared_development_build_segment_count": declared_development_count,
            "development_eligible_segment_count": eligible_flagged_count,
            "feature_valid_development_segment_count": eligible_feature_valid_count,
            "unique_eligible_player_count": len(player_builds),
            "unique_eligible_exact_build_count": len(candidates),
            "selected_representative_count": len(selected),
            "total_exact_player_mass": _fraction_payload(
                sum((row.player_mass for row in candidates.values()), Fraction(0, 1))
            ),
            "source_ineligible_segment_reason_counts": dict(
                sorted(ineligible_segment_reasons.items())
            ),
            "source_ineligible_unique_player_reason_counts": {
                reason: len(players)
                for reason, players in sorted(ineligible_players_by_reason.items())
            },
            "selector_feature_exclusion_counts": dict(sorted(selector_exclusions.items())),
            "assignment": assignment,
        },
        "outputs": {
            "representatives_path": str(representatives_path) if selected else None,
            "representative_schema": REPRESENTATIVE_SCHEMA,
        },
        "source_contract": {
            "catalog_streamed_once": True,
            "large_normalized_event_partitions_opened": False,
            "raw_or_untranslated_build_fallback_allowed": False,
        },
    }
    manifest_output = output_directory / "manifest.json"
    _write_json_atomic(manifest_output, result)
    return {**result, "manifest_path": str(manifest_output)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select exact player-weighted Warrior representatives from historical catalog v1"
    )
    parser.add_argument("--catalog-manifest", default=str(DEFAULT_CATALOG_MANIFEST))
    parser.add_argument("--output-directory", default=str(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument("--max-representatives", type=int, default=64)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = select_historical_representative_builds(
        catalog_manifest=args.catalog_manifest,
        output_directory=args.output_directory,
        max_representatives=args.max_representatives,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
