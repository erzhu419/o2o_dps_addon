"""Build conservative historical-build coverage for the local wowsims tree.

The registry is deliberately narrower than the simulator database.  A row in
``db.json`` proves that an item or enchant definition is known; it does not by
itself prove that a declared proc/use effect is implemented.  Direct item
effects therefore need both the generated database marker and a registration
site in the current Warrior/common source closure.  Unknown effects are never
reclassified as passive statistics.

Only the compact historical build catalogue is streamed.  The large Chronicle
event partitions are not opened by this module.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping

from .historical_build_catalog_v1 import COVERAGE_SCHEMA, RECORD_SCHEMA
from .turtle_talent_position_map_v1 import (
    TurtleTalentPositionMapError,
    load_admitted_position_map,
)
from .wowsims_profile import WARRIOR_TALENT_FIELDS


IMPLEMENTATION_REVISION = "v1.5_admitted_talent_map_and_class_applicability"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WOWSIMS_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
DEFAULT_DATABASE = DEFAULT_WOWSIMS_ROOT / "assets" / "database" / "db.json"
DEFAULT_CATALOG = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_build_catalog"
    / "v1"
    / "catalog.jsonl.gz"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "wowsims_mechanics_coverage_registry"
    / "v1"
    / "registry.json"
)

ITEM_REGISTRATION_CALLS = (
    "NewItemEffect",
    "NewSimpleStatItemActiveEffect",
    "NewSimpleStatItemEffect",
    "NewSimpleStatOffensiveTrinketEffectWithOtherEffects",
    "NewSimpleStatOffensiveTrinketEffect",
    "NewSimpleStatDefensiveTrinketEffect",
    "NewMobTypeAttackPowerEffect",
    "NewMobTypeSpellPowerEffect",
    "CreateWeaponProcDamage",
    "CreateWeaponCoHProcDamage",
    "CreateWeaponEquipProcDamage",
    "CreateWeaponProcSpell",
    "CreateWeaponProcAura",
)
ENCHANT_REGISTRATION_CALLS = ("NewEnchantEffect", "AddWeaponEffect")
HAND_TYPE_TO_MODE = {
    1: "ONE_HAND",  # main-hand-only
    2: "ONE_HAND",
    # Off-hand-only items include shields and held items.  They occupy the
    # off-hand slot but cannot establish a second swinging weapon.
    3: "OFF_HAND_ONLY",
    4: "TWO_HAND",
}

# These are effects whose pinned DBC conditions make them inert for the
# current Warrior build catalogue.  The unscoped item remains unsupported; a
# class-specific catalogue consumer may use the effective status only after
# the declared spell identity below has been rechecked.
WARRIOR_INAPPLICABLE_ITEM_EFFECTS = {
    22798: {
        "expected_spell_ids": (51136,),
        "status": "INAPPLICABLE_SHAPESHIFT_ONLY",
        "evidence": "spell:51136 requires Cat/Bear/Dire Bear/Moonkin form",
    },
}


class MechanicsCoverageRegistryError(RuntimeError):
    """The local database, source closure, or compact catalogue is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MechanicsCoverageRegistryError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise MechanicsCoverageRegistryError(f"{label} must be a JSON object")
    return value


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _nonzero_stats(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(number, (int, float))
        and not isinstance(number, bool)
        and number != 0
        for number in value
    )


def scan_historical_catalog(path: str | Path | None) -> dict[str, Any]:
    """Stream the small build catalogue and retain only bounded counters."""

    if path is None:
        return {
            "status": "NOT_PROVIDED",
            "catalog_path": None,
            "segment_count": 0,
            "item_counts": Counter(),
            "warrior_item_counts": Counter(),
            "enchant_counts": Counter(),
            "warrior_enchant_counts": Counter(),
            "client_builds": set(),
            "warrior_tree_shapes": Counter(),
            "warrior_nonzero_positions": set(),
        }
    resolved = Path(path).expanduser().resolve()
    opener = gzip.open if resolved.suffix == ".gz" else open
    result = {
        "status": "STREAMED_COMPACT_HISTORICAL_BUILD_CATALOG",
        "catalog_path": str(resolved),
        "segment_count": 0,
        "item_counts": Counter(),
        "warrior_item_counts": Counter(),
        "enchant_counts": Counter(),
        "warrior_enchant_counts": Counter(),
        "client_builds": set(),
        "warrior_tree_shapes": Counter(),
        "warrior_nonzero_positions": set(),
    }
    try:
        with opener(resolved, "rt", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise MechanicsCoverageRegistryError(
                        f"catalogue line {line_number} is invalid JSON: {error}"
                    ) from error
                if not isinstance(row, Mapping) or row.get("schema") != RECORD_SCHEMA:
                    raise MechanicsCoverageRegistryError(
                        f"catalogue line {line_number} is not {RECORD_SCHEMA}"
                    )
                result["segment_count"] += 1
                hero_class = str((row.get("player") or {}).get("hero_class") or "").upper()
                is_warrior = hero_class == "WARRIOR"
                client_build = (row.get("version") or {}).get("client_build")
                if is_warrior and isinstance(client_build, str) and client_build:
                    result["client_builds"].add(client_build)
                slots = (row.get("equipment") or {}).get("slots")
                if isinstance(slots, list):
                    for slot in slots:
                        if not isinstance(slot, Mapping) or slot.get("status") != "OBSERVED_EQUIPPED":
                            continue
                        item_id = _positive_id(slot.get("item_id"))
                        if item_id is not None:
                            result["item_counts"][item_id] += 1
                            if is_warrior:
                                result["warrior_item_counts"][item_id] += 1
                        enchant_ids: list[Any] = [
                            slot.get("permanent_enchant_id"),
                            slot.get("temporary_enchant_id"),
                        ]
                        gems = slot.get("gem_enchant_ids")
                        if isinstance(gems, list):
                            enchant_ids.extend(gems)
                        for raw_id in enchant_ids:
                            enchant_id = _positive_id(raw_id)
                            if enchant_id is None:
                                continue
                            result["enchant_counts"][enchant_id] += 1
                            if is_warrior:
                                result["warrior_enchant_counts"][enchant_id] += 1
                if not is_warrior:
                    continue
                trees = (row.get("talents") or {}).get("original_tree_rank_strings")
                if not isinstance(trees, list) or not all(isinstance(tree, str) for tree in trees):
                    continue
                result["warrior_tree_shapes"][tuple(len(tree) for tree in trees)] += 1
                for tree_index, tree in enumerate(trees):
                    for position, character in enumerate(tree):
                        if character != "0":
                            result["warrior_nonzero_positions"].add((tree_index, position))
    except (OSError, UnicodeDecodeError) as error:
        raise MechanicsCoverageRegistryError(f"cannot stream catalogue {resolved}: {error}") from error
    return result


def _mask_go_comments_and_literals(text: str) -> str:
    """Replace comments and literals with spaces while preserving newlines."""

    output = list(text)
    index = 0
    state = "CODE"
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "CODE":
            if current == "/" and following == "/":
                output[index] = output[index + 1] = " "
                state = "LINE_COMMENT"
                index += 2
                continue
            if current == "/" and following == "*":
                output[index] = output[index + 1] = " "
                state = "BLOCK_COMMENT"
                index += 2
                continue
            if current == '"':
                output[index] = " "
                state = "STRING"
            elif current == "'":
                output[index] = " "
                state = "RUNE"
            elif current == "`":
                output[index] = " "
                state = "RAW_STRING"
        elif state == "LINE_COMMENT":
            if current == "\n":
                state = "CODE"
            else:
                output[index] = " "
        elif state == "BLOCK_COMMENT":
            if current == "*" and following == "/":
                output[index] = output[index + 1] = " "
                state = "CODE"
                index += 2
                continue
            if current != "\n":
                output[index] = " "
        elif state in {"STRING", "RUNE"}:
            delimiter = '"' if state == "STRING" else "'"
            if current == "\\":
                output[index] = " "
                if index + 1 < len(text):
                    if text[index + 1] != "\n":
                        output[index + 1] = " "
                    index += 2
                    continue
            if current == delimiter:
                output[index] = " "
                state = "CODE"
            elif current != "\n":
                output[index] = " "
        elif state == "RAW_STRING":
            if current == "`":
                output[index] = " "
                state = "CODE"
            elif current != "\n":
                output[index] = " "
        index += 1
    return "".join(output)


def _source_files(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    common = root / "sim" / "common"
    warrior = root / "sim" / "warrior"
    item_files = [common / "item_effects.go", warrior / "items.go"]
    item_files.extend(sorted((common / "item_effects").glob("*.go")))
    enchant_files = [common / "enchant_effects.go"]
    talent_files = sorted(
        path for path in warrior.glob("*.go") if not path.name.endswith("_test.go")
    )
    required = [*item_files, *enchant_files, *talent_files]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise MechanicsCoverageRegistryError("missing wowsims source: " + ", ".join(missing))
    return item_files, enchant_files, talent_files


def _read_masked_sources(paths: Iterable[Path]) -> dict[Path, str]:
    result: dict[Path, str] = {}
    for path in paths:
        try:
            result[path] = _mask_go_comments_and_literals(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            raise MechanicsCoverageRegistryError(f"cannot read source {path}: {error}") from error
    return result


def _numeric_constants(sources: Mapping[Path, str]) -> dict[str, int]:
    constants: dict[str, int] = {}
    pattern = re.compile(
        r"(?m)^\s*(?:const\s+)?([A-Za-z_]\w*)\s*(?:[A-Za-z_]\w*)?\s*=\s*(\d+)\b"
    )
    for text in sources.values():
        for match in pattern.finditer(text):
            name, raw_value = match.groups()
            value = int(raw_value)
            previous = constants.get(name)
            if previous is not None and previous != value:
                raise MechanicsCoverageRegistryError(
                    f"source constant {name} has conflicting values {previous} and {value}"
                )
            constants[name] = value
    return constants


def _registration_evidence(
    sources: Mapping[Path, str],
    *,
    root: Path,
    calls: Iterable[str],
) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    constants = _numeric_constants(sources)
    call_pattern = re.compile(
        r"(?:[A-Za-z_]\w*\.)?(" + "|".join(re.escape(call) for call in calls) + r")\s*\(\s*([A-Za-z_]\w*|\d+)"
    )
    evidence: dict[int, list[dict[str, Any]]] = defaultdict(list)
    unresolved: list[dict[str, Any]] = []
    for path, text in sources.items():
        for match in call_pattern.finditer(text):
            call, token = match.groups()
            identifier = int(token) if token.isdigit() else constants.get(token)
            row = {
                "source": str(path.relative_to(root)).replace("\\", "/"),
                "line": text.count("\n", 0, match.start()) + 1,
                "registration_call": call,
                "source_identifier": token,
            }
            if identifier is None:
                # Function definitions use parameter names and are not concrete
                # registrations; keep other unresolved calls visible.
                if token not in {"id", "itemID", "itemId", "itemID1", "itemID2"}:
                    unresolved.append(row)
                continue
            evidence[identifier].append(row)
    return dict(evidence), unresolved


def _talent_translation(
    root: Path,
    talent_sources: Mapping[Path, str],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tree_path = root / "ui" / "core" / "talents" / "trees" / "warrior.json"
    tree_document = _load_json_array(tree_path, label="Warrior talent tree")
    actual_fields = tuple(
        tuple(str(talent.get("fieldName")) for talent in tree.get("talents", []))
        for tree in tree_document
    )
    if actual_fields != WARRIOR_TALENT_FIELDS:
        raise MechanicsCoverageRegistryError(
            "Warrior UI talent order differs from the request composer mapping"
        )
    combined_source = "\n".join(talent_sources.values())
    fields: list[list[dict[str, Any]]] = []
    implemented_count = 0
    unsupported: list[str] = []
    for tree_index, tree in enumerate(tree_document):
        output_tree: list[dict[str, Any]] = []
        for position, raw_talent in enumerate(tree["talents"]):
            field_name = str(raw_talent["fieldName"])
            go_name = field_name[0].upper() + field_name[1:]
            matches = list(re.finditer(r"\bTalents\." + re.escape(go_name) + r"\b", combined_source))
            implemented = bool(matches)
            if implemented:
                implemented_count += 1
            else:
                unsupported.append(field_name)
            location = raw_talent.get("location") or {}
            output_tree.append(
                {
                    "talent_id": f"warrior.{field_name}",
                    "profile_name": field_name,
                    "tab": tree_index + 1,
                    "index": position + 1,
                    "tier": int(location.get("rowIdx", 0)) + 1,
                    "column": int(location.get("colIdx", 0)) + 1,
                    "max_rank": int(raw_talent.get("maxPoints", 0)),
                    "definition_status": "KNOWN",
                    "effect_status": "IMPLEMENTED" if implemented else "UNSUPPORTED",
                    "effect_implemented": implemented,
                    "calibrated_scopes": [],
                    **_three_layer_status(
                        representation_complete=implemented,
                        mechanism=f"talent:warrior.{field_name}",
                    ),
                }
            )
        fields.append(output_tree)
    observed_shapes = observation.get("warrior_tree_shapes", Counter())
    positions_beyond_simulator_tree_lengths = sorted(
        [
            {"tree_index": tree_index, "position": position}
            for tree_index, position in observation.get("warrior_nonzero_positions", set())
            if tree_index >= len(fields) or position >= len(fields[tree_index])
        ],
        key=lambda row: (row["tree_index"], row["position"]),
    )
    simulator_field_coverage = {
        "coverage_version": "wowsims-warrior-ui-fields-v1",
        "tree_fields": fields,
        "definition_status": "KNOWN",
        "effect_implemented_field_count": implemented_count,
        "calibrated_scopes": [],
        "source": str(tree_path.relative_to(root)).replace("\\", "/"),
    }
    translation = {
        "translation_version": "turtle-client-build-position-map-v1",
        "client_build_position_maps": {},
        "definition_status": "UNPINNED",
        "translation_reason": "NO_EXPLICIT_TURTLE_CLIENT_BUILD_POSITION_MAP",
    }
    summary = {
        "field_count": sum(len(tree) for tree in fields),
        "implemented_field_count": implemented_count,
        "unsupported_fields": unsupported,
        "observed_client_builds": sorted(observation.get("client_builds", set())),
        "pinned_client_builds": [],
        "observed_tree_shapes": {
            "-".join(str(value) for value in shape): count
            for shape, count in sorted(observed_shapes.items())
        },
        "observed_nonzero_positions_beyond_simulator_tree_lengths": (
            positions_beyond_simulator_tree_lengths
        ),
    }
    return simulator_field_coverage, translation, summary


def _merge_talent_position_map_overlays(
    *,
    paths: Iterable[str | Path],
    simulator_field_coverage: Mapping[str, Any],
    translation: dict[str, Any],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_simulator_trees = simulator_field_coverage.get("tree_fields")
    if not isinstance(raw_simulator_trees, list):
        raise MechanicsCoverageRegistryError(
            "simulator Warrior talent field coverage lacks tree_fields"
        )
    simulator_by_profile: dict[str, Mapping[str, Any]] = {}
    for raw_tree in raw_simulator_trees:
        if not isinstance(raw_tree, list):
            raise MechanicsCoverageRegistryError(
                "simulator Warrior talent field tree must be an array"
            )
        for raw_field in raw_tree:
            if not isinstance(raw_field, Mapping):
                raise MechanicsCoverageRegistryError(
                    "simulator Warrior talent field must be an object"
                )
            profile_name = raw_field.get("profile_name")
            if not isinstance(profile_name, str) or not profile_name:
                raise MechanicsCoverageRegistryError(
                    "simulator Warrior talent field lacks profile_name"
                )
            simulator_by_profile[profile_name] = raw_field

    position_maps = translation.get("client_build_position_maps")
    if not isinstance(position_maps, dict):
        raise MechanicsCoverageRegistryError(
            "talent translation client_build_position_maps must be an object"
        )
    merged_sources: list[dict[str, Any]] = []
    for raw_path in paths:
        resolved = Path(raw_path).expanduser().resolve()
        try:
            artifact = load_admitted_position_map(resolved)
        except TurtleTalentPositionMapError as error:
            raise MechanicsCoverageRegistryError(str(error)) from error
        client_build = artifact["client_build"]
        if client_build in position_maps:
            raise MechanicsCoverageRegistryError(
                f"duplicate/conflicting talent position map for client build {client_build}"
            )
        artifact_map = artifact["position_map"]
        output_trees: list[list[dict[str, Any]]] = []
        for tree_index, raw_tree in enumerate(artifact_map["tree_fields"]):
            output_tree: list[dict[str, Any]] = []
            for position, raw_field in enumerate(raw_tree):
                field = dict(raw_field)
                status = field["simulator_mapping_status"]
                profile_name = field.get("profile_name")
                common = {
                    "talent_id": field.get("talent_id"),
                    "profile_name": profile_name,
                    "max_rank": field["max_rank"],
                    "client_semantic": field.get("client_semantic"),
                    "simulator_mapping_status": status,
                    "simulator_target": field.get("simulator_target"),
                    "calibrated_scopes": [],
                }
                if status == "TALENT_FIELD":
                    simulator_field = simulator_by_profile.get(str(profile_name))
                    if simulator_field is None:
                        raise MechanicsCoverageRegistryError(
                            f"admitted client build {client_build} maps unknown simulator "
                            f"talent field {profile_name!r}"
                        )
                    if field.get("talent_id") != simulator_field.get("talent_id"):
                        raise MechanicsCoverageRegistryError(
                            f"admitted client build {client_build} talent ID conflicts with "
                            f"simulator field {profile_name}"
                        )
                    common.update(
                        {
                            "definition_status": simulator_field["definition_status"],
                            "effect_status": simulator_field["effect_status"],
                            "effect_implemented": simulator_field["effect_implemented"],
                            "simulator_field_semantic": {
                                key: simulator_field.get(key)
                                for key in ("tab", "index", "tier", "column", "max_rank")
                            },
                            "simulator_representation": simulator_field[
                                "simulator_representation"
                            ],
                            "turtle_calibration": simulator_field["turtle_calibration"],
                            "comparison_eligibility": simulator_field[
                                "comparison_eligibility"
                            ],
                        }
                    )
                elif status == "OPTION_FIELD":
                    if field.get("simulator_target") != "warrior.options.ravagerRank":
                        raise MechanicsCoverageRegistryError(
                            "admitted Ravager mapping has the wrong simulator target"
                        )
                    common.update(
                        {
                            "definition_status": "KNOWN",
                            "effect_status": "IMPLEMENTED",
                            "effect_implemented": True,
                            **_three_layer_status(
                                representation_complete=True,
                                mechanism="talent:turtle.warrior.ravager",
                            ),
                        }
                    )
                elif status == "UNSUPPORTED_TURTLE_ONLY":
                    common.update(
                        {
                            "definition_status": "KNOWN",
                            "effect_status": "UNSUPPORTED",
                            "effect_implemented": False,
                            **_three_layer_status(
                                representation_complete=False,
                                mechanism=(
                                    f"talent:{field.get('talent_id') or tree_index}:{position}"
                                ),
                            ),
                        }
                    )
                else:  # load_admitted_position_map already rejects this.
                    raise MechanicsCoverageRegistryError(
                        f"unsupported talent map status {status!r}"
                    )
                output_tree.append(common)
            output_trees.append(output_tree)
        position_maps[client_build] = {
            "tree_fields": output_trees,
            "zero_only_unsupported_tail_may_be_trimmed": artifact_map[
                "zero_only_unsupported_tail_may_be_trimmed"
            ],
            "admission_artifact": str(resolved),
            "admission_status": artifact["admission_status"],
        }
        merged_sources.append(
            {
                "path": str(resolved),
                "client_build": client_build,
                "admission": True,
            }
        )
    pinned_builds = sorted(position_maps)
    summary["pinned_client_builds"] = pinned_builds
    if pinned_builds:
        translation["definition_status"] = "PINNED"
        translation["translation_reason"] = None
    return merged_sources


def _load_json_array(path: Path, *, label: str) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MechanicsCoverageRegistryError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise MechanicsCoverageRegistryError(f"{label} must be an array of objects")
    return value


def _catalog_counts(observation: Mapping[str, Any], key: str) -> Counter[int]:
    value = observation.get(key, Counter())
    if not isinstance(value, Counter):
        raise MechanicsCoverageRegistryError(f"catalogue {key} must be a Counter")
    return value


def _three_layer_status(*, representation_complete: bool, mechanism: str) -> dict[str, Any]:
    representation_reason = (
        None if representation_complete else "SIMULATOR_MECHANISM_NOT_FULLY_REPRESENTED"
    )
    comparison_reasons = ["NO_TURTLE_CALIBRATED_SCOPE"]
    if not representation_complete:
        comparison_reasons.insert(0, "SIMULATOR_REPRESENTATION_INCOMPLETE")
    return {
        "simulator_representation": {
            "status": "RUNNABLE" if representation_complete else "BLOCKED",
            "reason": representation_reason,
        },
        "turtle_calibration": {
            "status": "NOT_CALIBRATED",
            "scopes": [],
            "mechanism": mechanism,
        },
        "comparison_eligibility": {
            "eligible": False,
            "reasons": comparison_reasons,
        },
    }


def build_registry(
    *,
    database_path: str | Path = DEFAULT_DATABASE,
    wowsims_root: str | Path = DEFAULT_WOWSIMS_ROOT,
    historical_catalog_path: str | Path | None = DEFAULT_CATALOG,
    talent_position_map_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    root = Path(wowsims_root).expanduser().resolve()
    database_resolved = Path(database_path).expanduser().resolve()
    database = _load_json(database_resolved, label="wowsims database")
    raw_items = database.get("items")
    raw_enchants = database.get("enchants")
    if not isinstance(raw_items, list) or not isinstance(raw_enchants, list):
        raise MechanicsCoverageRegistryError("wowsims database items/enchants must be arrays")
    items_by_id = {
        identifier: row
        for row in raw_items
        if isinstance(row, Mapping) and (identifier := _positive_id(row.get("id"))) is not None
    }
    enchants_by_id = {
        identifier: row
        for row in raw_enchants
        if isinstance(row, Mapping)
        and (identifier := _positive_id(row.get("effectId"))) is not None
    }
    observation = scan_historical_catalog(historical_catalog_path)
    item_counts = _catalog_counts(observation, "item_counts")
    warrior_item_counts = _catalog_counts(observation, "warrior_item_counts")
    enchant_counts = _catalog_counts(observation, "enchant_counts")
    warrior_enchant_counts = _catalog_counts(observation, "warrior_enchant_counts")

    item_files, enchant_files, talent_files = _source_files(root)
    item_sources = _read_masked_sources(item_files)
    enchant_sources = _read_masked_sources(enchant_files)
    talent_sources = _read_masked_sources(talent_files)
    item_registration, unresolved_item_calls = _registration_evidence(
        item_sources, root=root, calls=ITEM_REGISTRATION_CALLS
    )
    enchant_registration, unresolved_enchant_calls = _registration_evidence(
        enchant_sources, root=root, calls=ENCHANT_REGISTRATION_CALLS
    )
    target_item_ids = set(item_counts) | set(item_registration)
    target_enchant_ids = set(enchant_counts) | set(enchant_registration)

    item_output: dict[str, Any] = {}
    item_statuses: Counter[str] = Counter()
    warrior_item_statuses: Counter[str] = Counter()
    item_gaps: list[int] = []
    warrior_item_gaps: list[int] = []
    item_source_db_divergence: list[int] = []
    item_db_implemented_outside_scope: list[int] = []
    for identifier in sorted(target_item_ids):
        raw = items_by_id.get(identifier)
        database_known = raw is not None
        declared_effects = list(raw.get("effects") or []) if raw is not None else []
        effect_declared = bool(declared_effects)
        db_implemented = bool(raw.get("hasImplementedEffects")) if raw is not None else False
        source_evidence = item_registration.get(identifier, [])
        source_registered = bool(source_evidence)

        class_effect_status: dict[str, Any] = {}
        applicability = WARRIOR_INAPPLICABLE_ITEM_EFFECTS.get(identifier)
        if applicability is not None:
            declared_spell_ids = tuple(
                sorted(
                    effect["spellId"]
                    for effect in declared_effects
                    if isinstance(effect, Mapping)
                    and isinstance(effect.get("spellId"), int)
                    and not isinstance(effect.get("spellId"), bool)
                )
            )
            if declared_spell_ids != applicability["expected_spell_ids"]:
                raise MechanicsCoverageRegistryError(
                    f"item {identifier} class applicability pin expected spells "
                    f"{applicability['expected_spell_ids']}, got {declared_spell_ids}"
                )
            class_effect_status["WARRIOR"] = {
                "effective_effect_status": "NO_SPECIAL_EFFECT",
                "applicability_status": applicability["status"],
                "evidence": applicability["evidence"],
            }
        if not database_known:
            effect_status = "UNKNOWN"
        elif effect_declared and db_implemented and source_registered:
            effect_status = "IMPLEMENTED"
        elif not effect_declared and not db_implemented and not source_registered:
            effect_status = "NO_SPECIAL_EFFECT"
        elif effect_declared and not db_implemented and not source_registered:
            effect_status = "UNSUPPORTED"
        elif effect_declared and db_implemented and not source_registered and not warrior_item_counts[identifier]:
            effect_status = "OUTSIDE_WARRIOR_COMMON_SOURCE_SCOPE"
            item_db_implemented_outside_scope.append(identifier)
        else:
            effect_status = "UNKNOWN"
            item_source_db_divergence.append(identifier)
        warrior_effect_status = (
            class_effect_status.get("WARRIOR", {}).get(
                "effective_effect_status", effect_status
            )
        )
        if effect_status not in {"IMPLEMENTED", "NO_SPECIAL_EFFECT"}:
            item_gaps.append(identifier)
            if (
                warrior_item_counts[identifier]
                and warrior_effect_status not in {"IMPLEMENTED", "NO_SPECIAL_EFFECT"}
            ):
                warrior_item_gaps.append(identifier)
        item_statuses[effect_status] += 1
        if warrior_item_counts[identifier]:
            warrior_item_statuses[warrior_effect_status] += 1
        row: dict[str, Any] = {
            "name": raw.get("name") if raw is not None else None,
            "definition_status": "KNOWN" if database_known else "UNKNOWN",
            "effect_status": effect_status,
            "calibrated_scopes": [],
            "database_known": database_known,
            "effect_declared": effect_declared,
            "database_marks_effect_implemented": db_implemented,
            "source_registration_found": source_registered,
            "effect_implemented": effect_status == "IMPLEMENTED",
            "representation_complete": effect_status in {"IMPLEMENTED", "NO_SPECIAL_EFFECT"},
            "calibrated_scope": {
                "status": "NOT_ITEM_SPECIFICALLY_CALIBRATED",
                "scopes": [],
            },
            "historical_segment_occurrences": item_counts[identifier],
            "historical_warrior_segment_occurrences": warrior_item_counts[identifier],
            "declared_effects": declared_effects,
            "source_registration_evidence": source_evidence,
            "class_effect_status": class_effect_status,
            **_three_layer_status(
                representation_complete=effect_status in {"IMPLEMENTED", "NO_SPECIAL_EFFECT"},
                mechanism=f"item:{identifier}",
            ),
        }
        hand_type = raw.get("handType") if raw is not None else None
        if hand_type in HAND_TYPE_TO_MODE:
            row["weapon_mode"] = HAND_TYPE_TO_MODE[hand_type]
            row["hand_type"] = hand_type
        if raw is not None and _positive_id(raw.get("setId")) is not None:
            row["set_id"] = raw["setId"]
            row["set_name"] = raw.get("setName")
        item_output[str(identifier)] = row

    enchant_output: dict[str, Any] = {}
    enchant_statuses: Counter[str] = Counter()
    warrior_enchant_statuses: Counter[str] = Counter()
    enchant_gaps: list[int] = []
    warrior_enchant_gaps: list[int] = []
    for identifier in sorted(target_enchant_ids):
        raw = enchants_by_id.get(identifier)
        database_known = raw is not None
        source_evidence = enchant_registration.get(identifier, [])
        dynamic_registered = bool(source_evidence)
        static_stats = _nonzero_stats(raw.get("stats")) if raw is not None else False
        if database_known and (dynamic_registered or static_stats):
            effect_status = "IMPLEMENTED"
        else:
            effect_status = "UNKNOWN"
            enchant_gaps.append(identifier)
            if warrior_enchant_counts[identifier]:
                warrior_enchant_gaps.append(identifier)
        enchant_statuses[effect_status] += 1
        if warrior_enchant_counts[identifier]:
            warrior_enchant_statuses[effect_status] += 1
        enchant_output[str(identifier)] = {
            "name": raw.get("name") if raw is not None else None,
            "definition_status": "KNOWN" if database_known else "UNKNOWN",
            "effect_status": effect_status,
            "calibrated_scopes": [],
            "database_known": database_known,
            "static_stats_implemented": static_stats,
            "dynamic_source_registration_found": dynamic_registered,
            "effect_implemented": effect_status == "IMPLEMENTED",
            "calibrated_scope": {
                "status": "NOT_ENCHANT_SPECIFICALLY_CALIBRATED",
                "scopes": [],
            },
            "historical_segment_occurrences": enchant_counts[identifier],
            "historical_warrior_segment_occurrences": warrior_enchant_counts[identifier],
            "source_registration_evidence": source_evidence,
            **_three_layer_status(
                representation_complete=effect_status == "IMPLEMENTED",
                mechanism=f"enchant:{identifier}",
            ),
        }

    simulator_talent_fields, talent_translation, talent_summary = _talent_translation(
        root, talent_sources, observation
    )
    talent_position_map_overlays = _merge_talent_position_map_overlays(
        paths=talent_position_map_paths,
        simulator_field_coverage=simulator_talent_fields,
        translation=talent_translation,
        summary=talent_summary,
    )
    return {
        "schema": COVERAGE_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": "wowsims_mechanics_coverage_registry",
        "created_at": _utc_now(),
        "item_dataset": {
            "kind": "LOCAL_WOWSIMS_DATABASE_AND_SOURCE_CLOSURE",
            "database": str(database_resolved),
            "wowsims_root": str(root),
        },
        "coverage_contract": {
            "database_known_is_not_effect_implemented": True,
            "unknown_special_effect_defaults_to_passive": False,
            "class_inapplicable_effect_keeps_unscoped_blocker": True,
            "implemented_item_effect_requires_database_and_current_source_registration": True,
            "static_enchant_stats_are_applied_by_sim_core_database": True,
            "calibrated_scope_is_independent_of_source_implementation": True,
            "simulator_representation_runnable_does_not_imply_comparison_eligible": True,
            "empty_calibrated_scopes_are_comparison_eligible": False,
            "observed_client_build_does_not_pin_talent_position_order": True,
            "historical_talent_translation_requires_exact_client_build_position_map": True,
            "historical_talent_rank_must_not_exceed_pinned_max_rank": True,
            "talent_position_map_overlay_requires_admission_true": True,
            "large_normalized_event_partitions_opened": False,
        },
        "request_encoding_contract": {
            "permanent_enchant_in_item_spec": "SUPPORTED",
            "temporary_enchant_in_item_spec": "NOT_SUPPORTED_BY_BUILD_REQUEST_COMPOSER_V1",
            "gem_enchant_in_item_spec": "NOT_SUPPORTED_BY_BUILD_REQUEST_COMPOSER_V1",
        },
        "items": item_output,
        "enchants": enchant_output,
        "simulator_talent_field_coverage": {"WARRIOR": simulator_talent_fields},
        "talent_translations": {"WARRIOR": talent_translation},
        "summary": {
            "historical_catalog": {
                "status": observation["status"],
                "path": observation["catalog_path"],
                "segment_count": observation["segment_count"],
            },
            "target_item_id_count": len(item_output),
            "target_enchant_id_count": len(enchant_output),
            "target_id_contract": {
                "item_ids": "HISTORICAL_OBSERVED_UNION_CURRENT_SOURCE_REGISTERED",
                "enchant_ids": "HISTORICAL_OBSERVED_UNION_CURRENT_SOURCE_REGISTERED",
                "full_database_is_not_an_output_target": True,
                "historical_observed_item_id_count": len(item_counts),
                "historical_observed_enchant_id_count": len(enchant_counts),
                "source_registered_only_item_id_count": len(
                    set(item_registration) - set(item_counts)
                ),
                "source_registered_only_enchant_id_count": len(
                    set(enchant_registration) - set(enchant_counts)
                ),
            },
            "item_definition_known_count": sum(
                row["definition_status"] == "KNOWN" for row in item_output.values()
            ),
            "enchant_definition_known_count": sum(
                row["definition_status"] == "KNOWN" for row in enchant_output.values()
            ),
            "item_effect_status_counts": dict(sorted(item_statuses.items())),
            "enchant_effect_status_counts": dict(sorted(enchant_statuses.items())),
            "item_gap_ids": item_gaps,
            "enchant_gap_ids": enchant_gaps,
            "item_source_database_divergence_ids": item_source_db_divergence,
            "item_database_implemented_outside_warrior_common_scope_ids": (
                item_db_implemented_outside_scope
            ),
            "warrior_priority": {
                "observed_item_id_count": len(warrior_item_counts),
                "observed_enchant_id_count": len(warrior_enchant_counts),
                "item_effect_status_counts": dict(sorted(warrior_item_statuses.items())),
                "enchant_effect_status_counts": dict(
                    sorted(warrior_enchant_statuses.items())
                ),
                "item_gap_ids": warrior_item_gaps,
                "enchant_gap_ids": warrior_enchant_gaps,
            },
            "source_scan": {
                "current_source_registered_item_id_count": len(item_registration),
                "current_source_registered_enchant_id_count": len(enchant_registration),
                "unresolved_item_registration_calls": unresolved_item_calls,
                "unresolved_enchant_registration_calls": unresolved_enchant_calls,
            },
            "talent_position_map_overlays": talent_position_map_overlays,
            "warrior_talents": talent_summary,
            "weapon_mode_semantics": {
                "1": "ONE_HAND_MAIN_HAND_ONLY",
                "2": "ONE_HAND_EITHER_HAND",
                "3": "ONE_HAND_OFF_HAND_ONLY",
                "4": "TWO_HAND",
                "catalog_projection": {
                    "1": "ONE_HAND",
                    "2": "ONE_HAND",
                    "3": "ONE_HAND",
                    "4": "TWO_HAND",
                },
                "missing_or_nonweapon_hand_type": "NO_WEAPON_MODE_CLAIM",
            },
        },
    }


def write_registry(
    *,
    output_path: str | Path = DEFAULT_OUTPUT,
    database_path: str | Path = DEFAULT_DATABASE,
    wowsims_root: str | Path = DEFAULT_WOWSIMS_ROOT,
    historical_catalog_path: str | Path | None = DEFAULT_CATALOG,
    talent_position_map_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    registry = build_registry(
        database_path=database_path,
        wowsims_root=wowsims_root,
        historical_catalog_path=historical_catalog_path,
        talent_position_map_paths=talent_position_map_paths,
    )
    resolved = Path(output_path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=resolved.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(registry, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(resolved)
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--wowsims-root", default=str(DEFAULT_WOWSIMS_ROOT))
    parser.add_argument("--historical-catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--no-historical-catalog", action="store_true")
    parser.add_argument(
        "--talent-position-map",
        action="append",
        default=[],
        help="admitted turtle_talent_position_map/v1 artifact; repeat per client build",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    registry = write_registry(
        output_path=args.output,
        database_path=args.database,
        wowsims_root=args.wowsims_root,
        historical_catalog_path=(
            None if args.no_historical_catalog else args.historical_catalog
        ),
        talent_position_map_paths=args.talent_position_map,
    )
    print(
        json.dumps(
            {
                "status": "WROTE_CONSERVATIVE_COVERAGE_REGISTRY",
                "output": str(Path(args.output).expanduser().resolve()),
                "summary": registry["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
