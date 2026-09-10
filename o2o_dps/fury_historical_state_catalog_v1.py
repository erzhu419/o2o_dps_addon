"""Build a capture-bound catalog from the imported Fury calibration journal.

The calibration logger stores a state snapshot on every row, but the snapshot
schema became richer over time.  It is therefore unsafe to carry equipment,
talents, auras, armor, or any other field from a nearby row into an older or a
newer capture.  This builder projects each row independently and represents
every absent field as ``null`` plus an explicit missing mask.

Calibration summary reports are used only to attach task-completion metadata
when the source JSONL, taskRunId, and sequence range all agree.  They never
supply state.  The resulting catalog is a historical prior and is deliberately
ineligible for Shadow enrichment, training, expert voting, or deployment.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALIBRATION_DIR = PROJECT_ROOT / "offline_data" / "calibration"
DEFAULT_SUMMARY_DIR = PROJECT_ROOT / "offline_data" / "calibration_summaries"
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "fury_historical_state_catalog"
    / "v1"
)
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "states.jsonl"
DEFAULT_MANIFEST = DEFAULT_OUTPUT_DIR / "manifest.json"
DEFAULT_REPORT = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_historical_state_catalog_v1.json"
)

RECORD_SCHEMA = "fury_historical_state_capture/v1"
MANIFEST_SCHEMA = "fury_historical_state_catalog_manifest/v1"
REPORT_SCHEMA = "fury_historical_state_catalog_report/v1"
SCOPE = "CALIBRATION_CAPTURE_BOUND"
USE_CLASS = "HISTORICAL_PRIOR_ONLY"
MISSING_REASON = "NOT_CAPTURED_IN_SAME_ROW_NO_BACKFILL"
DECLARED_ARMOR_SOURCE_WITHOUT_TARGET = (
    "DECLARED_SOURCE_BUT_VALUE_UNAVAILABLE_TARGET_ABSENT"
)
DECLARED_ARMOR_SOURCE_WITHOUT_VALUE = "DECLARED_SOURCE_BUT_VALUE_NOT_CAPTURED"
ITEM_LINK_RE = re.compile(r"Hitem:(\d+):(\d+):")

# These paths are the complete public presence mask.  A value is present only
# when its source key exists and is non-null on the same JSONL row.
FIELD_SOURCES: tuple[tuple[str, str], ...] = (
    ("actor.player_guid", "playerGUID"),
    ("actor.character_identity", "characterIdentity"),
    ("actor.class_file", "classFile"),
    ("actor.level", "playerLevel"),
    ("actor.health", "health"),
    ("actor.maximum_health", "maximumHealth"),
    ("actor.power", "power"),
    ("actor.rage", "rage"),
    ("actor.rage_raw", "rageRaw"),
    ("actor.attack_power", "attackPower"),
    ("target.target_guid", "targetGUID"),
    ("target.name", "targetName"),
    ("target.classification", "targetClassification"),
    ("target.level", "targetLevel"),
    ("target.health", "targetHealth"),
    ("target.maximum_health", "targetMaximumHealth"),
    ("target.armor", "targetArmor"),
    ("equipment", "equipment"),
    ("talents", "talents"),
    ("skills.skill_lines", "skillLines"),
    ("skills.spellbook", "spellbook"),
    ("combat.main_hand_speed_seconds", "mainHandSpeed"),
    ("combat.off_hand_speed_seconds", "offHandSpeed"),
    ("combat.main_hand_remaining_seconds", "cat2MainHandRemaining"),
    ("combat.gcd_remaining_seconds", "gcd"),
    ("combat.cooldowns", "cooldowns"),
    ("combat.in_combat", "inCombat"),
    ("combat.moving", "moving"),
    ("combat.target_melee_distance", "targetMeleeDistance"),
    ("combat.player_aura_names", "playerAuras"),
    ("combat.target_aura_names", "targetAuras"),
)

PROVENANCE_SOURCE_KEYS = {
    "actor.character_identity": "characterIdentity",
    "actor.level": "playerLevel",
    "actor.attack_power": "attackPower",
    "target.armor": "targetArmor",
    "equipment": "equipment",
    "talents": "talents",
    "skills.skill_lines": "skillLines",
    "skills.spellbook": "spellbook",
    "combat.main_hand_speed_seconds": "mainHandSpeed",
    "combat.off_hand_speed_seconds": "offHandSpeed",
    "combat.player_aura_names": "playerAuras",
    "combat.target_aura_names": "targetAuras",
}
CORE_CONTEXT_FIELDS = frozenset(
    {
        "actor.player_guid",
        "target.target_guid",
        "actor.attack_power",
        "equipment",
        "talents",
        "combat.main_hand_speed_seconds",
        "combat.player_aura_names",
        "combat.target_aura_names",
        "target.armor",
    }
)
SKILL_CONTEXT_FIELDS = CORE_CONTEXT_FIELDS | {
    "skills.skill_lines",
    "skills.spellbook",
}


class FuryHistoricalStateCatalogError(RuntimeError):
    """An input or output violates the capture-bound v1 contract."""


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_nonfinite_json,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise FuryHistoricalStateCatalogError(f"cannot decode {label}: {error}") from error


def _decode_json_line(line: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            line,
            parse_constant=_reject_nonfinite_json,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except ValueError as error:
        raise FuryHistoricalStateCatalogError(f"cannot decode {label}: {error}") from error
    if not isinstance(value, dict):
        raise FuryHistoricalStateCatalogError(f"{label} must contain a JSON object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FuryHistoricalStateCatalogError(
            f"cannot serialize catalog value: {error}"
        ) from error


def _pretty_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FuryHistoricalStateCatalogError(
            f"cannot serialize catalog document: {error}"
        ) from error


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest_relative_path(path: Path, manifest_path: Path) -> str:
    """Return a portable path resolved from the manifest's parent directory."""

    try:
        relative = os.path.relpath(path, start=manifest_path.parent)
    except ValueError as error:
        raise FuryHistoricalStateCatalogError(
            "catalog outputs must share a filesystem volume so manifest paths "
            "remain portable"
        ) from error
    return Path(relative).as_posix()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryHistoricalStateCatalogError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryHistoricalStateCatalogError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FuryHistoricalStateCatalogError(f"{label} must be a non-empty string")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise FuryHistoricalStateCatalogError(f"{label} must be a string")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FuryHistoricalStateCatalogError(f"{label} must be an integer")
    if positive and value <= 0:
        raise FuryHistoricalStateCatalogError(f"{label} must be positive")
    return value


def _finite(value: Any, label: str) -> int | float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise FuryHistoricalStateCatalogError(f"{label} must be a finite number")
    return value


def _optional_finite(value: Any, label: str) -> int | float | None:
    if value is None:
        return None
    return _finite(value, label)


def _optional_boolean(value: Any, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise FuryHistoricalStateCatalogError(f"{label} must be a boolean")
    return value


def _present(state: Mapping[str, Any], key: str) -> bool:
    return key in state and state[key] is not None


def _project_numeric_mapping(
    value: Any,
    label: str,
    keys: Sequence[str],
) -> dict[str, int | float] | None:
    if value is None:
        return None
    source = _mapping(value, label)
    result: dict[str, int | float] = {}
    for key in keys:
        if key in source and source[key] is not None:
            result[key] = _finite(source[key], f"{label}.{key}")
    return result


def _project_character_identity(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    source = _mapping(value, label)
    result: dict[str, Any] = {}
    for key in (
        "name",
        "className",
        "classFile",
        "raceName",
        "raceFile",
        "factionName",
        "factionFile",
        "level",
        "sex",
    ):
        if key in source and source[key] is not None:
            item = source[key]
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                raise FuryHistoricalStateCatalogError(
                    f"{label}.{key} must be a string or integer"
                )
            result[key] = item
    return result


def _project_equipment(value: Any, label: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    result: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    for index, item_value in enumerate(_list(value, label), start=1):
        item = _mapping(item_value, f"{label}[{index}]")
        slot = _integer(item.get("slot"), f"{label}[{index}].slot", positive=True)
        if slot in seen_slots:
            raise FuryHistoricalStateCatalogError(f"{label} repeats equipment slot {slot}")
        seen_slots.add(slot)
        link = _text(item.get("link"), f"{label}[{index}].link")
        match = ITEM_LINK_RE.search(link)
        if match is None:
            raise FuryHistoricalStateCatalogError(
                f"{label}[{index}].link has no parseable item/enchant identity"
            )
        result.append(
            {
                "slot": slot,
                "item_id": int(match.group(1)),
                "enchant_id": int(match.group(2)),
                "link": link,
                "identity_source": "SAME_CAPTURE_INVENTORY_LINK",
            }
        )
    return result


def _project_talents(value: Any, label: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    result: list[dict[str, Any]] = []
    for item_index, item_value in enumerate(_list(value, label), start=1):
        item = _mapping(item_value, f"{label}[{item_index}]")
        projected: dict[str, Any] = {}
        for key in ("tab", "index", "rank", "maxRank", "tier", "column"):
            if key in item and item[key] is not None:
                projected[key] = _integer(item[key], f"{label}[{item_index}].{key}")
        if "name" in item and item["name"] is not None:
            projected["name"] = _text(item["name"], f"{label}[{item_index}].name")
        result.append(projected)
    return result


def _project_skill_lines(value: Any, label: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    result: list[dict[str, Any]] = []
    for item_index, item_value in enumerate(_list(value, label), start=1):
        item = _mapping(item_value, f"{label}[{item_index}]")
        projected: dict[str, Any] = {}
        for key in ("index", "rank", "maximum", "modifier", "temporary"):
            if key in item and item[key] is not None:
                projected[key] = _finite(item[key], f"{label}[{item_index}].{key}")
        for key in ("isHeader", "isExpanded"):
            if key in item and item[key] is not None:
                projected[key] = _optional_boolean(
                    item[key], f"{label}[{item_index}].{key}"
                )
        if "name" in item and item["name"] is not None:
            projected["name"] = _text(item["name"], f"{label}[{item_index}].name")
        result.append(projected)
    return result


def _project_spellbook(value: Any, label: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    result: list[dict[str, Any]] = []
    for item_index, item_value in enumerate(_list(value, label), start=1):
        item = _mapping(item_value, f"{label}[{item_index}]")
        projected: dict[str, Any] = {}
        if "spellbookIndex" in item and item["spellbookIndex"] is not None:
            projected["spellbook_index"] = _integer(
                item["spellbookIndex"],
                f"{label}[{item_index}].spellbookIndex",
                positive=True,
            )
        for source_key, output_key in (
            ("name", "name"),
            ("rank", "rank"),
            ("texture", "texture"),
            ("tooltipText", "tooltip_text"),
        ):
            if source_key in item and item[source_key] is not None:
                converter = _string if source_key == "rank" else _text
                projected[output_key] = converter(
                    item[source_key], f"{label}[{item_index}].{source_key}"
                )
        result.append(projected)
    return result


def _project_aura_names(value: Any, label: str) -> list[str] | None:
    if value is None:
        return None
    result: list[str] = []
    for index, item in enumerate(_list(value, label), start=1):
        result.append(_text(item, f"{label}[{index}]"))
    return result


def _project_cooldowns(value: Any, label: str) -> dict[str, int | float] | None:
    if value is None:
        return None
    source = _mapping(value, label)
    return {
        str(key): _finite(item, f"{label}.{key}")
        for key, item in sorted(source.items(), key=lambda pair: str(pair[0]))
    }


def _coalesced_task_value(
    task: Mapping[str, Any],
    marker: Mapping[str, Any],
    key: str,
    label: str,
) -> tuple[Any, str | None]:
    task_value = task.get(key)
    marker_value = marker.get(key)
    if task_value is not None and marker_value is not None and task_value != marker_value:
        raise FuryHistoricalStateCatalogError(
            f"{label} has conflicting task.{key} and marker.{key}"
        )
    if task_value is not None:
        return task_value, f"task.{key}"
    if marker_value is not None:
        return marker_value, f"marker.{key}"
    return None, None


def _task_context(row: Mapping[str, Any], label: str) -> dict[str, Any]:
    task_value = row.get("task")
    marker_value = row.get("marker")
    task = _mapping(task_value, f"{label}.task") if task_value is not None else {}
    marker = (
        _mapping(marker_value, f"{label}.marker") if marker_value is not None else {}
    )
    result: dict[str, Any] = {}
    task_run_id_source: str | None = None
    for source_key, output_key in (
        ("taskRunId", "task_run_id"),
        ("taskId", "task_id"),
        ("campaignRunId", "campaign_run_id"),
        ("campaignId", "campaign_id"),
        ("trial", "trial"),
    ):
        value, source = _coalesced_task_value(task, marker, source_key, label)
        if value is None:
            result[output_key] = None
        elif source_key == "trial":
            result[output_key] = _integer(value, f"{label}.{source}", positive=True)
        else:
            result[output_key] = _text(value, f"{label}.{source}")
        if output_key == "task_run_id":
            task_run_id_source = source
    result["task_run_id_source"] = task_run_id_source
    return result


def _field_provenance(state: Mapping[str, Any], label: str) -> dict[str, str | None]:
    raw = state.get("fieldProvenance")
    source = _mapping(raw, f"{label}.fieldProvenance") if raw is not None else {}
    result: dict[str, str | None] = {}
    for path, source_key in PROVENANCE_SOURCE_KEYS.items():
        value = source.get(source_key)
        if value is not None:
            declared_source = _optional_text(
                value, f"{label}.fieldProvenance.{source_key}"
            )
            # The logger declares targetArmor's conditional API source even
            # when no target exists.  That declaration is not an observation.
            # Keep the distinction row-local instead of counting it under the
            # raw OBSERVED_* source label.
            if path == "target.armor" and not _present(state, "targetArmor"):
                result[path] = (
                    DECLARED_ARMOR_SOURCE_WITHOUT_TARGET
                    if state.get("targetExists") is False
                    else DECLARED_ARMOR_SOURCE_WITHOUT_VALUE
                )
            else:
                result[path] = declared_source
    return result


def _summary_completion_binding(
    *,
    source_name: str,
    task_run_id: str | None,
    sequence: int,
    summaries_by_source: Mapping[str, str],
    completion_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    if task_run_id is None:
        return "NO_TASK_RUN_ID_ON_CAPTURE", None
    if source_name not in summaries_by_source:
        return "NO_SUMMARY_FOR_SOURCE", None
    completion = completion_index.get((source_name, task_run_id))
    if completion is None:
        return "NO_MATCHING_TASK_COMPLETION", None
    sequence_range = _mapping(
        completion.get("sequence_range"), "task completion sequence_range"
    )
    start = _integer(sequence_range.get("start"), "task completion sequence start")
    end = _integer(sequence_range.get("end"), "task completion sequence end")
    if start > end:
        raise FuryHistoricalStateCatalogError("task completion sequence range is reversed")
    if not start <= sequence <= end:
        return "SEQUENCE_OUTSIDE_TASK_COMPLETION_RANGE", None
    return (
        "EXACT_SOURCE_TASK_RUN_AND_SEQUENCE_RANGE",
        {
            "status": _optional_text(completion.get("status"), "completion.status"),
            "completion_source": _optional_text(
                completion.get("completion_source"), "completion.completion_source"
            ),
        },
    )


def project_capture_row(
    row: Mapping[str, Any],
    *,
    source_name: str,
    source_sha256: str,
    source_line: int,
    summaries_by_source: Mapping[str, str] | None = None,
    completion_index: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project one row without consulting any other capture row."""

    label = f"{source_name}:{source_line}"
    sequence = _integer(row.get("sequence"), f"{label}.sequence", positive=True)
    event = _text(row.get("event"), f"{label}.event")
    state = _mapping(row.get("state"), f"{label}.state")
    captured_at = _finite(state.get("capturedAt"), f"{label}.state.capturedAt")
    task = _task_context(row, label)

    equipment = _project_equipment(state.get("equipment"), f"{label}.state.equipment")
    talents = _project_talents(state.get("talents"), f"{label}.state.talents")
    skill_lines = _project_skill_lines(
        state.get("skillLines"), f"{label}.state.skillLines"
    )
    spellbook = _project_spellbook(state.get("spellbook"), f"{label}.state.spellbook")
    field_presence = {
        output_path: _present(state, source_key)
        for output_path, source_key in FIELD_SOURCES
    }
    missing_fields = [
        output_path for output_path, present in field_presence.items() if not present
    ]

    summary_status, summary_completion = _summary_completion_binding(
        source_name=source_name,
        task_run_id=task["task_run_id"],
        sequence=sequence,
        summaries_by_source=summaries_by_source or {},
        completion_index=completion_index or {},
    )

    projected: dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "binding": {
            "scope": SCOPE,
            "source_jsonl": source_name,
            "source_jsonl_sha256": source_sha256,
            "source_line": source_line,
            "sequence": sequence,
            "event": event,
            "captured_at": captured_at,
            "task": task,
            "summary_binding_status": summary_status,
            "summary_completion": summary_completion,
        },
        "state": {
            "actor": {
                "player_guid": _optional_text(
                    state.get("playerGUID"), f"{label}.state.playerGUID"
                ),
                "character_identity": _project_character_identity(
                    state.get("characterIdentity"),
                    f"{label}.state.characterIdentity",
                ),
                "class_file": _optional_text(
                    state.get("classFile"), f"{label}.state.classFile"
                ),
                "level": (
                    _integer(state["playerLevel"], f"{label}.state.playerLevel")
                    if _present(state, "playerLevel")
                    else None
                ),
                "health": _optional_finite(
                    state.get("health"), f"{label}.state.health"
                ),
                "maximum_health": _optional_finite(
                    state.get("maximumHealth"), f"{label}.state.maximumHealth"
                ),
                "power": _optional_finite(
                    state.get("power"), f"{label}.state.power"
                ),
                "rage": _optional_finite(state.get("rage"), f"{label}.state.rage"),
                "rage_raw": _optional_finite(
                    state.get("rageRaw"), f"{label}.state.rageRaw"
                ),
                "attack_power": _project_numeric_mapping(
                    state.get("attackPower"),
                    f"{label}.state.attackPower",
                    ("base", "positive", "negative", "effective"),
                ),
            },
            "target": {
                "target_guid": _optional_text(
                    state.get("targetGUID"), f"{label}.state.targetGUID"
                ),
                "name": _optional_text(
                    state.get("targetName"), f"{label}.state.targetName"
                ),
                "classification": _optional_text(
                    state.get("targetClassification"),
                    f"{label}.state.targetClassification",
                ),
                "level": (
                    _integer(state["targetLevel"], f"{label}.state.targetLevel")
                    if _present(state, "targetLevel")
                    else None
                ),
                "health": _optional_finite(
                    state.get("targetHealth"), f"{label}.state.targetHealth"
                ),
                "maximum_health": _optional_finite(
                    state.get("targetMaximumHealth"),
                    f"{label}.state.targetMaximumHealth",
                ),
                "armor": _project_numeric_mapping(
                    state.get("targetArmor"),
                    f"{label}.state.targetArmor",
                    ("base", "positive", "negative", "armor", "effective"),
                ),
            },
            "equipment": equipment,
            "talents": talents,
            "skills": {
                "skill_lines": skill_lines,
                "spellbook": spellbook,
            },
            "combat": {
                "main_hand_speed_seconds": _optional_finite(
                    state.get("mainHandSpeed"), f"{label}.state.mainHandSpeed"
                ),
                "off_hand_speed_seconds": _optional_finite(
                    state.get("offHandSpeed"), f"{label}.state.offHandSpeed"
                ),
                "main_hand_remaining_seconds": _optional_finite(
                    state.get("cat2MainHandRemaining"),
                    f"{label}.state.cat2MainHandRemaining",
                ),
                "gcd_remaining_seconds": _optional_finite(
                    state.get("gcd"), f"{label}.state.gcd"
                ),
                "cooldowns": _project_cooldowns(
                    state.get("cooldowns"), f"{label}.state.cooldowns"
                ),
                "in_combat": _optional_boolean(
                    state.get("inCombat"), f"{label}.state.inCombat"
                ),
                "moving": _optional_boolean(
                    state.get("moving"), f"{label}.state.moving"
                ),
                "target_melee_distance": _optional_finite(
                    state.get("targetMeleeDistance"),
                    f"{label}.state.targetMeleeDistance",
                ),
                "player_aura_names": _project_aura_names(
                    state.get("playerAuras"), f"{label}.state.playerAuras"
                ),
                "target_aura_names": _project_aura_names(
                    state.get("targetAuras"), f"{label}.state.targetAuras"
                ),
            },
        },
        "field_provenance": _field_provenance(state, f"{label}.state"),
        "missing_mask": {
            "missing_fields": missing_fields,
            "reason": MISSING_REASON,
        },
        "evidence_scope": SCOPE,
        "use_class": USE_CLASS,
        "gates": {
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
        },
    }
    capture_identity = {
        "source_jsonl_sha256": source_sha256,
        "source_line": source_line,
        "sequence": sequence,
        "task_run_id": task["task_run_id"],
        "captured_at": captured_at,
        "projected_state": projected["state"],
    }
    projected["capture_id"] = _sha256(_canonical_bytes(capture_identity))
    return projected


def _summary_inputs(
    summary_dir: Path,
    source_names: set[str],
) -> tuple[
    list[dict[str, Any]],
    dict[str, str],
    dict[tuple[str, str], Mapping[str, Any]],
]:
    inventory: list[dict[str, Any]] = []
    summaries_by_source: dict[str, str] = {}
    completion_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for path in sorted(summary_dir.glob("BrainOfCat__*.json"), key=lambda item: item.name):
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise FuryHistoricalStateCatalogError(
                f"cannot read calibration summary {path}: {error}"
            ) from error
        document = _decode_json(raw, f"calibration summary {path.name}")
        summary = _mapping(document, f"calibration summary {path.name}")
        if summary.get("kind") != "brainofcat_calibration_summary":
            raise FuryHistoricalStateCatalogError(
                f"{path.name} has unexpected summary kind {summary.get('kind')!r}"
            )
        source = _mapping(summary.get("source"), f"{path.name}.source")
        source_value = _text(
            source.get("calibration_jsonl"), f"{path.name}.source.calibration_jsonl"
        )
        source_name = Path(source_value).name
        if source_name not in source_names:
            raise FuryHistoricalStateCatalogError(
                f"{path.name} refers to calibration JSONL outside the selected set: "
                f"{source_name}"
            )
        if source_name in summaries_by_source:
            raise FuryHistoricalStateCatalogError(
                f"multiple summaries refer to {source_name}"
            )
        summaries_by_source[source_name] = path.name
        task_completions = summary.get("task_completions", [])
        for index, completion_value in enumerate(
            _list(task_completions, f"{path.name}.task_completions"), start=1
        ):
            completion = _mapping(
                completion_value, f"{path.name}.task_completions[{index}]"
            )
            task_run_id = _text(
                completion.get("task_run_id"),
                f"{path.name}.task_completions[{index}].task_run_id",
            )
            key = (source_name, task_run_id)
            if key in completion_index:
                raise FuryHistoricalStateCatalogError(
                    f"duplicate task completion for {source_name}/{task_run_id}"
                )
            completion_index[key] = completion
        inventory.append(
            {
                "kind": "calibration_summary",
                "path": path.name,
                "size_bytes": len(raw),
                "sha256": _sha256(raw),
                "source_jsonl": source_name,
                "task_completion_count": len(task_completions),
            }
        )
    return inventory, summaries_by_source, completion_index


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
        os.replace(temporary, path)
    except OSError as error:
        raise FuryHistoricalStateCatalogError(f"cannot write {path}: {error}") from error
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _builder_identity() -> dict[str, Any]:
    path = Path(__file__).resolve()
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise FuryHistoricalStateCatalogError(
            f"cannot read builder module {path}: {error}"
        ) from error
    return {
        "module": path.name,
        "size_bytes": len(raw),
        "sha256": _sha256(raw),
        "record_schema": RECORD_SCHEMA,
    }


def _verify_source_inventory(
    calibration_root: Path,
    summary_root: Path,
    jsonl_inventory: Sequence[Mapping[str, Any]],
    summary_inventory: Sequence[Mapping[str, Any]],
) -> None:
    expected_jsonl_names = [str(item["path"]) for item in jsonl_inventory]
    expected_summary_names = [str(item["path"]) for item in summary_inventory]
    current_jsonl_names = [
        path.name
        for path in sorted(
            calibration_root.glob("BrainOfCat__*.jsonl"), key=lambda item: item.name
        )
    ]
    current_summary_names = [
        path.name
        for path in sorted(
            summary_root.glob("BrainOfCat__*.json"), key=lambda item: item.name
        )
    ]
    if current_jsonl_names != expected_jsonl_names:
        raise FuryHistoricalStateCatalogError(
            "calibration JSONL selection changed during materialization"
        )
    if current_summary_names != expected_summary_names:
        raise FuryHistoricalStateCatalogError(
            "calibration summary selection changed during materialization"
        )
    for root, inventory, label in (
        (calibration_root, jsonl_inventory, "calibration JSONL"),
        (summary_root, summary_inventory, "calibration summary"),
    ):
        for item in inventory:
            path = root / str(item["path"])
            try:
                raw = path.read_bytes()
            except OSError as error:
                raise FuryHistoricalStateCatalogError(
                    f"cannot revalidate {label} {path}: {error}"
                ) from error
            if len(raw) != item["size_bytes"] or _sha256(raw) != item["sha256"]:
                raise FuryHistoricalStateCatalogError(
                    f"{label} {path.name} changed during materialization"
                )


def materialize_fury_historical_state_catalog(
    calibration_dir: str | Path = DEFAULT_CALIBRATION_DIR,
    summary_dir: str | Path = DEFAULT_SUMMARY_DIR,
    *,
    output_path: str | Path = DEFAULT_OUTPUT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    report_path: str | Path = DEFAULT_REPORT,
) -> dict[str, Any]:
    """Build the deterministic catalog and write its manifest last."""

    calibration_root = Path(calibration_dir).expanduser().resolve()
    summary_root = Path(summary_dir).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    manifest_output = Path(manifest_path).expanduser().resolve()
    report_output = Path(report_path).expanduser().resolve()
    output_set = {output, manifest_output, report_output}
    if len(output_set) != 3:
        raise FuryHistoricalStateCatalogError("output, manifest, and report must differ")
    source_paths = sorted(
        calibration_root.glob("BrainOfCat__*.jsonl"), key=lambda item: item.name
    )
    if not source_paths:
        raise FuryHistoricalStateCatalogError(
            f"no calibration JSONL files found in {calibration_root}"
        )
    if output_set & {path.resolve() for path in source_paths}:
        raise FuryHistoricalStateCatalogError("outputs must not overwrite source JSONL")
    summary_paths = sorted(
        summary_root.glob("BrainOfCat__*.json"), key=lambda item: item.name
    )
    if output_set & {path.resolve() for path in summary_paths}:
        raise FuryHistoricalStateCatalogError("outputs must not overwrite a source summary")

    summary_inventory, summaries_by_source, completion_index = _summary_inputs(
        summary_root, {path.name for path in source_paths}
    )
    builder = _builder_identity()
    jsonl_inventory: list[dict[str, Any]] = []
    coverage = {path: Counter() for path, _ in FIELD_SOURCES}
    provenance_counts: dict[str, Counter[str]] = defaultdict(Counter)
    summary_binding_counts: Counter[str] = Counter()
    coverage_group_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    player_guids: set[str] = set()
    target_guids: set[str] = set()
    task_run_ids: set[str] = set()
    capture_ids: set[str] = set()
    row_count = 0
    output_hash = hashlib.sha256()
    output_size = 0
    temporary_output: Path | None = None

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_output = Path(handle.name)
            for source_path in source_paths:
                try:
                    raw = source_path.read_bytes()
                except OSError as error:
                    raise FuryHistoricalStateCatalogError(
                        f"cannot read calibration JSONL {source_path}: {error}"
                    ) from error
                source_sha256 = _sha256(raw)
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    raise FuryHistoricalStateCatalogError(
                        f"cannot decode calibration JSONL {source_path}: {error}"
                    ) from error
                source_rows = 0
                for source_line, line in enumerate(io.StringIO(text), start=1):
                    if not line.strip():
                        continue
                    row = _decode_json_line(
                        line, f"calibration JSONL {source_path.name}:{source_line}"
                    )
                    projected = project_capture_row(
                        row,
                        source_name=source_path.name,
                        source_sha256=source_sha256,
                        source_line=source_line,
                        summaries_by_source=summaries_by_source,
                        completion_index=completion_index,
                    )
                    capture_id = projected["capture_id"]
                    if capture_id in capture_ids:
                        raise FuryHistoricalStateCatalogError(
                            f"duplicate capture identity {capture_id}"
                        )
                    capture_ids.add(capture_id)
                    serialized = _canonical_bytes(projected)
                    handle.write(serialized)
                    output_hash.update(serialized)
                    output_size += len(serialized)
                    row_count += 1
                    source_rows += 1
                    missing = set(projected["missing_mask"]["missing_fields"])
                    for field_path, _source_key in FIELD_SOURCES:
                        present = field_path not in missing
                        coverage[field_path]["present" if present else "missing"] += 1
                    if not missing.intersection(CORE_CONTEXT_FIELDS):
                        coverage_group_counts["requested_core_same_capture"] += 1
                    if not missing.intersection(SKILL_CONTEXT_FIELDS):
                        coverage_group_counts[
                            "requested_core_plus_skills_same_capture"
                        ] += 1
                    for field_path in PROVENANCE_SOURCE_KEYS:
                        value = projected["field_provenance"].get(field_path)
                        provenance_counts[field_path][value or "NOT_DECLARED_ON_ROW"] += 1
                    binding_status = projected["binding"]["summary_binding_status"]
                    summary_binding_counts[binding_status] += 1
                    event_counts[projected["binding"]["event"]] += 1
                    actor = projected["state"]["actor"]
                    target = projected["state"]["target"]
                    if actor["class_file"] is not None:
                        class_counts[actor["class_file"]] += 1
                    if actor["player_guid"] is not None:
                        player_guids.add(actor["player_guid"])
                    if target["target_guid"] is not None:
                        target_guids.add(target["target_guid"])
                    task_run_id = projected["binding"]["task"]["task_run_id"]
                    if task_run_id is not None:
                        task_run_ids.add(task_run_id)
                jsonl_inventory.append(
                    {
                        "kind": "calibration_jsonl",
                        "path": source_path.name,
                        "size_bytes": len(raw),
                        "sha256": source_sha256,
                        "row_count": source_rows,
                        "summary": summaries_by_source.get(source_path.name),
                    }
                )
            handle.flush()
        os.replace(temporary_output, output)
        temporary_output = None
    except OSError as error:
        raise FuryHistoricalStateCatalogError(f"cannot write catalog {output}: {error}") from error
    finally:
        if temporary_output is not None and temporary_output.exists():
            try:
                temporary_output.unlink()
            except OSError:
                pass

    committed = output.read_bytes()
    dataset_sha256 = output_hash.hexdigest()
    if len(committed) != output_size or _sha256(committed) != dataset_sha256:
        raise FuryHistoricalStateCatalogError("committed catalog bytes failed verification")

    _verify_source_inventory(
        calibration_root,
        summary_root,
        jsonl_inventory,
        summary_inventory,
    )
    input_identities = jsonl_inventory + summary_inventory
    input_bundle_sha256 = _sha256(_canonical_bytes(input_identities))
    build_bundle_sha256 = _sha256(
        _canonical_bytes(
            {
                "builder": builder,
                "input_bundle_sha256": input_bundle_sha256,
                "field_sources": FIELD_SOURCES,
                "missing_reason": MISSING_REASON,
            }
        )
    )
    coverage_document = {
        field_path: {
            "present": counter["present"],
            "missing": counter["missing"],
            "coverage_fraction": counter["present"] / row_count,
        }
        for field_path, counter in coverage.items()
    }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "COMPLETE_HISTORICAL_PRIOR_ONLY",
        "scope": SCOPE,
        "use_class": USE_CLASS,
        "builder": builder,
        "inputs": {
            "calibration_jsonl_count": len(jsonl_inventory),
            "calibration_summary_count": len(summary_inventory),
            "jsonl_without_summary_count": sum(
                item["summary"] is None for item in jsonl_inventory
            ),
            "source_row_count": row_count,
            "input_bundle_sha256": input_bundle_sha256,
            "build_bundle_sha256": build_bundle_sha256,
            "same_bytes_hashed_and_decoded": True,
            "post_build_source_identity_validation": True,
        },
        "output": {
            "dataset": output.name,
            "row_count": row_count,
            "size_bytes": output_size,
            "sha256": dataset_sha256,
            "one_output_per_source_capture": True,
            "duplicate_capture_ids": 0,
        },
        "binding_contract": {
            "state_source": "ONLY_THE_SAME_JSONL_CAPTURE_ROW",
            "identity": [
                "source_jsonl_sha256",
                "source_line",
                "sequence",
                "task_run_id",
                "captured_at",
                "projected_state",
            ],
            "summary_metadata_join": [
                "same source JSONL",
                "exact taskRunId",
                "capture sequence inside declared completion range",
            ],
            "summary_can_supply_state": False,
            "carry_forward": False,
            "carry_backward": False,
            "nearest_capture_join": False,
            "cross_task_join": False,
            "shadow_transition_join": False,
            "missing_reason": MISSING_REASON,
        },
        "coverage": {
            "fields": coverage_document,
            "field_provenance": {
                field_path: dict(sorted(counter.items()))
                for field_path, counter in sorted(provenance_counts.items())
            },
            "summary_binding_status": dict(sorted(summary_binding_counts.items())),
            "events": dict(sorted(event_counts.items())),
            "classes": dict(sorted(class_counts.items())),
            "distinct_player_guids": len(player_guids),
            "distinct_target_guids": len(target_guids),
            "distinct_task_run_ids": len(task_run_ids),
            "groups": {
                "requested_core_same_capture": coverage_group_counts[
                    "requested_core_same_capture"
                ],
                "requested_core_plus_skills_same_capture": coverage_group_counts[
                    "requested_core_plus_skills_same_capture"
                ],
                "core_definition": sorted(CORE_CONTEXT_FIELDS),
                "core_plus_skills_definition": sorted(SKILL_CONTEXT_FIELDS),
            },
        },
        "semantic_limits": {
            "equipment_enchant": (
                "item_id and enchant_id are parsed only from the same capture's "
                "inventory link; enchant names/effects are not inferred"
            ),
            "auras": "names only; no spell ID, stack count, or remaining duration",
            "armor": (
                "the row-local game API snapshot only; no proc attribution, base-armor "
                "reconstruction, or decontamination; a declared conditional API source "
                "without a targetArmor value is classified as unavailable rather than "
                "observed"
            ),
            "skills": (
                "row-local skillLines/spellbook snapshots only; absent snapshots remain "
                "missing"
            ),
            "simulator_seed": (
                "catalog rows omit canonical RNG, pending-event scheduler, continuous "
                "command prefix, and exact restore proof"
            ),
        },
        "shadow_exclusion": {
            "shadow_dataset_consumed": False,
            "shadow_rows_enriched": 0,
            "shadow_rows_backfilled": 0,
            "reason": (
                "historical calibration captures are not the same capture/sequence/"
                "taskRunId as later Shadow transitions"
            ),
        },
        "eligibility": {
            "historical_prior_usable": True,
            "exact_simulator_seed_eligible": False,
            "shadow_backfill_eligible": False,
            "training_eligible": False,
            "expert_vote_eligible": False,
            "deployment_eligible": False,
            "real_game_superiority_claimed": False,
        },
    }
    report_raw = _pretty_bytes(report)
    _atomic_write(report_output, report_raw)
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "kind": "fury_historical_state_catalog",
        "record_schema": RECORD_SCHEMA,
        "scope": SCOPE,
        "use_class": USE_CLASS,
        "builder": builder,
        "inputs": {
            "calibration_jsonl_root": _manifest_relative_path(
                calibration_root, manifest_output
            ),
            "calibration_jsonl_path_base": "manifest_parent",
            "calibration_jsonl": jsonl_inventory,
            "calibration_summaries_root": _manifest_relative_path(
                summary_root, manifest_output
            ),
            "calibration_summaries_path_base": "manifest_parent",
            "calibration_summaries": summary_inventory,
            "input_bundle_sha256": input_bundle_sha256,
            "build_bundle_sha256": build_bundle_sha256,
        },
        "dataset": {
            "path": _manifest_relative_path(output, manifest_output),
            "path_base": "manifest_parent",
            "size_bytes": output_size,
            "row_count": row_count,
            "sha256": dataset_sha256,
        },
        "report": {
            "path": _manifest_relative_path(report_output, manifest_output),
            "path_base": "manifest_parent",
            "size_bytes": len(report_raw),
            "sha256": _sha256(report_raw),
        },
        "eligibility": report["eligibility"],
        "commit": {
            "state": "complete",
            "manifest_written_last": True,
            "content_addressed": True,
            "source_files_opened_read_only": True,
        },
    }
    manifest_raw = _pretty_bytes(manifest)
    _atomic_write(manifest_output, manifest_raw)
    if _sha256(output.read_bytes()) != manifest["dataset"]["sha256"]:
        raise FuryHistoricalStateCatalogError("dataset changed after manifest commit")
    if _sha256(report_output.read_bytes()) != manifest["report"]["sha256"]:
        raise FuryHistoricalStateCatalogError("report changed after manifest commit")
    if _decode_json(manifest_output.read_bytes(), "committed manifest") != manifest:
        raise FuryHistoricalStateCatalogError("manifest commit verification failed")
    return {
        "status": "ok",
        "row_count": row_count,
        "dataset": str(output),
        "dataset_sha256": dataset_sha256,
        "manifest": str(manifest_output),
        "manifest_sha256": _sha256(manifest_raw),
        "report": str(report_output),
        "report_sha256": _sha256(report_raw),
        "input_bundle_sha256": input_bundle_sha256,
        "build_bundle_sha256": build_bundle_sha256,
        "scope": SCOPE,
        "use_class": USE_CLASS,
        "training_eligible": False,
        "expert_vote_eligible": False,
        "deployment_eligible": False,
        "shadow_rows_backfilled": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--summary-dir", type=Path, default=DEFAULT_SUMMARY_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = materialize_fury_historical_state_catalog(
        args.calibration_dir,
        args.summary_dir,
        output_path=args.output,
        manifest_path=args.manifest,
        report_path=args.report,
    )
    json.dump(receipt, sys.stdout, ensure_ascii=False, allow_nan=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
