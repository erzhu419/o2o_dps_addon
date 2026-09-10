"""Field-level reconstruction-readiness audit for leaderboard Warriors.

The audit streams immutable Chronicle normalized JSONL.  Leaderboard scope is
resolved from ``chronicle_raw/export_queue.json`` and, when available, the
existing Fury partial-trajectory manifests.  Reports are evidence inventories;
they do not create trajectories or launch training/simulation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .chronicle_dataset_audit import (
    FAMILY_TYPE_ALIASES,
    GRADE_A_REQUIRED,
    GRADE_B_REQUIRED,
    KEY_FAMILIES,
)
from .chronicle_fury_trajectory import parse_combatant_info


SCHEMA_VERSION = 1
SCHEMA_NAME = "chronicle_warrior_reconstruction_readiness/v1"
STATUSES = ("OBSERVED", "RECONSTRUCTABLE", "INFERRED", "MISSING")
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
DEFAULT_REGISTRY = (
    Path(__file__).resolve().parents[1]
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
DEFAULT_REPORT_STEM = "chronicle_warrior_reconstruction_readiness"
QUEUE_PATH = Path("chronicle_raw") / "export_queue.json"
MANIFEST_GLOB = "derived/chronicle_fury_partial_trajectory/v1/*.manifest.json"
FULL_STATE_REQUIRED_FIELDS = (
    "combat_time",
    "boss_phase",
    "estimated_remaining_time",
    "target_health_remaining",
    "player_health_remaining",
    "target_count",
    "target_selection",
    "rage_gain_delta_chronicle_units",
    "rage_loss_delta_chronicle_units",
    "rage_unit_conversion_to_wow",
    "absolute_rage_anchor",
    "skill_rage_cost_coverage",
    "rage_spend_outcome_transition_coverage",
    "gcd_remaining",
    "cooldown_remaining",
    "casting_remaining",
    "mainhand_offhand_swing_remaining",
    "buffs_duration_stacks",
    "target_debuffs_duration_stacks",
    "stance_form",
    "queue_intent_timing",
    "range_and_behind",
    "recent_actions",
    "client_keypress",
    "exact_talent_ranks",
    "gear_item_ids",
    "player_stats",
    "raid_buffs_debuffs_initial_state",
    "latency_and_keyspam_interval",
)


class ReconstructionAuditError(RuntimeError):
    """An input required for a trustworthy readiness audit is invalid."""


@dataclass(frozen=True)
class ReconstructionAuditResult:
    report: dict[str, Any]
    json_report: Path
    markdown_report: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReconstructionAuditError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReconstructionAuditError(f"{label} is not a JSON object: {path}")
    return value


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def _normalized_name(value: Any) -> str | None:
    text = str(value or "").strip()
    return Path(text).name if text else None


def _merge_target(
    targets: dict[str, dict[str, dict[str, Any]]],
    normalized_name: str,
    player_name: str,
    evidence: dict[str, Any],
) -> None:
    name = player_name.strip()
    if not name:
        return
    by_name = targets.setdefault(normalized_name, {})
    target = by_name.setdefault(
        name.casefold(), {"name": name, "leaderboard_rows": [], "manifests": []}
    )
    if evidence["kind"] == "leaderboard_row":
        target["leaderboard_rows"].append(evidence)
    else:
        target["manifests"].append(evidence)


def _load_targets(
    data_root: Path, manifest_paths: Iterable[Path]
) -> tuple[dict[str, dict[str, dict[str, Any]]], Path]:
    queue_path = data_root / QUEUE_PATH
    queue = _load_object(queue_path, "Chronicle export queue")
    entries = queue.get("entries")
    if not isinstance(entries, list):
        raise ReconstructionAuditError(f"queue has no entries list: {queue_path}")

    targets: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        receipt = entry.get("import_receipt")
        receipt = receipt if isinstance(receipt, dict) else {}
        normalized_name = _normalized_name(receipt.get("normalized"))
        if normalized_name is None:
            instance = str(entry.get("instance_id") or "").strip()
            matches = sorted((data_root / "normalized").glob(f"{instance}__*.jsonl"))
            normalized_name = matches[0].name if len(matches) == 1 else None
        if normalized_name is None:
            continue
        for row in entry.get("leaderboard_rows") or []:
            if not isinstance(row, dict) or str(row.get("board_class") or "").upper() != "WARRIOR":
                continue
            _merge_target(
                targets,
                normalized_name,
                str(row.get("character") or ""),
                {
                    "kind": "leaderboard_row",
                    "board_spec": row.get("board_spec") or row.get("observed_spec"),
                    "rank": row.get("rank"),
                    "source_file": row.get("source_file"),
                },
            )

    for path in manifest_paths:
        manifest = _load_object(path, "Fury trajectory manifest")
        if manifest.get("kind") != "chronicle_fury_partial_trajectory_manifest":
            continue
        player = manifest.get("player")
        source = manifest.get("source")
        if not isinstance(player, dict) or not isinstance(source, dict):
            continue
        classes = {str(value).upper() for value in player.get("info_classes") or []}
        specs = list(player.get("leaderboard_observed_specs") or [])
        if "WARRIOR" not in classes or not specs:
            continue
        normalized_name = _normalized_name(source.get("normalized_file"))
        if normalized_name is None:
            continue
        _merge_target(
            targets,
            normalized_name,
            str(player.get("name") or ""),
            {
                "kind": "trajectory_manifest",
                "path": _display(path, data_root),
                "player_guid": player.get("guid"),
                "encounters": list(
                    (manifest.get("selection") or {}).get("encounters_emitted") or []
                ),
            },
        )
    return targets, queue_path


def _calibrated_registry(path: Path) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    registry = _load_object(path, "Warrior mechanics registry")
    by_spell: dict[int, dict[str, Any]] = {}
    observed_parameters = 0
    verified_transferable_parameters = 0
    for mechanic in registry.get("mechanics") or []:
        if not isinstance(mechanic, dict):
            continue
        implementation = mechanic.get("implementation")
        implementation = implementation if isinstance(implementation, dict) else {}
        spell_ids: set[int] = set()
        for key in ("spell_id", "wrapper_spell_id", "result_spell_id"):
            value = implementation.get(key)
            if isinstance(value, int):
                spell_ids.add(value)
        for value in implementation.get("spell_ids") or []:
            if isinstance(value, int):
                spell_ids.add(value)
        calibration = mechanic.get("calibration")
        calibration = calibration if isinstance(calibration, dict) else {}
        parameters = calibration.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        calibrated = {
            str(key): value
            for key, value in parameters.items()
            if isinstance(value, dict) and value.get("observed_value") is not None
        }
        observed_parameters += len(calibrated)
        verified_transferable_parameters += sum(
            1
            for key, value in calibrated.items()
            if value.get("status") == "verified_deterministic"
            and value.get("confidence") == "high"
            and "current_character" not in key
        )
        facts = {
            "mechanic": mechanic.get("key"),
            "implementation": implementation,
            "calibrated": calibrated,
            "unresolved": [
                value
                for value in calibration.get("unresolved") or []
                if isinstance(value, dict)
            ],
        }
        for spell_id in spell_ids:
            by_spell[spell_id] = facts
    return by_spell, {
        "path": str(path.resolve()),
        "schema_version": registry.get("schema_version"),
        "observed_parameter_count": observed_parameters,
        "verified_transferable_parameter_count": verified_transferable_parameters,
    }


def _empty_group(name: str, target: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "endpoint_guids": set(),
        "info_guids": set(),
        "info_rows": 0,
        "parsed_info_rows": 0,
        "info_classes": set(),
        "talent_trees": set(),
        "gear_slot_counts": set(),
        "gear_items_observed": False,
        "start_spells": Counter(),
        "start_rows": 0,
        "action_time_rows": 0,
        "go_spells": Counter(),
        "go_rows": 0,
        "rage_gain_rows": 0,
        "rage_loss_rows": 0,
        "rage_gain_units": 0.0,
        "rage_loss_units": 0.0,
        "absolute_rage_rows": 0,
        "gcd_rows": 0,
        "cooldown_rows": 0,
        "swing_timer_rows": 0,
        "auto_attack_rows": 0,
        "queue_intent_rows": 0,
        "keypress_rows": 0,
        "aura_rows": 0,
        "target_debuff_rows": 0,
        "combat_target_guids": set(),
        "leaderboard_rows": target["leaderboard_rows"],
        "manifests": target["manifests"],
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _field(status: str, evidence: str, **detail: Any) -> dict[str, Any]:
    if status not in STATUSES:
        raise AssertionError(status)
    return {"status": status, "evidence": evidence, **detail}


def _parameter_coverage(
    observed_spells: Counter[tuple[int | None, str]],
    by_spell: dict[int, dict[str, Any]],
    *,
    kind: str,
) -> dict[str, Any]:
    aliases = {
        "rage_cost": ("rage_cost",),
        "gcd": ("gcd_seconds",),
        "cooldown": ("cooldown_seconds",),
    }[kind]
    observed: list[dict[str, Any]] = []
    calibrated = 0
    inferred = 0
    unmapped = 0
    for (spell_id, spell_name), rows in sorted(
        observed_spells.items(), key=lambda item: (item[0][0] or -1, item[0][1])
    ):
        mechanic = by_spell.get(spell_id) if spell_id is not None else None
        if mechanic is None:
            unmapped += 1
            observed.append({"spell_id": spell_id, "spell_name": spell_name, "rows": rows, "status": "MISSING"})
            continue
        implementation = mechanic["implementation"]
        parameter_entry = next(
            (
                (key, value)
                for key, value in mechanic["calibrated"].items()
                if any(alias in key for alias in aliases)
            ),
            None,
        )
        parameter_key = parameter_entry[0] if parameter_entry else None
        parameter = parameter_entry[1] if parameter_entry else None
        implementation_keys = [
            key
            for key in implementation
            if any(alias in key for alias in aliases)
        ]
        if kind == "gcd" and "consumes_gcd" in implementation:
            implementation_keys.append("consumes_gcd")
        parameter_verified = (
            parameter is not None
            and parameter.get("status") == "verified_deterministic"
            and parameter.get("confidence") == "high"
            and "current_character" not in str(parameter_key)
        )
        if parameter_verified:
            status = "RECONSTRUCTABLE"
            calibrated += 1
        elif parameter is not None or implementation_keys:
            status = "INFERRED"
            inferred += 1
        else:
            status = "MISSING"
            unmapped += 1
        observed.append(
            {
                "spell_id": spell_id,
                "spell_name": spell_name,
                "rows": rows,
                "mechanic": mechanic["mechanic"],
                "status": status,
                "calibration_parameter": parameter_key,
                "calibrated_value": parameter.get("observed_value") if parameter else None,
                "calibration_status": parameter.get("status") if parameter else None,
                "calibration_confidence": parameter.get("confidence") if parameter else None,
                "current_character_only": "current_character" in str(parameter_key),
                "implementation_fields": sorted(set(implementation_keys)),
            }
        )
    if not observed:
        status = "MISSING"
    elif calibrated == len(observed):
        status = "RECONSTRUCTABLE"
    elif unmapped:
        status = "MISSING"
    elif inferred:
        status = "INFERRED"
    else:
        status = "MISSING"
    return _field(
        status,
        "Chronicle START action candidates joined to live-calibrated registry parameters",
        observed_action_spells=len(observed),
        calibrated_spells=calibrated,
        inferred_spells=inferred,
        missing_or_unmapped_spells=unmapped,
        skills=observed,
    )


def _rage_spend_transition_coverage(
    observed_spells: Counter[tuple[int | None, str]],
    by_spell: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    observed: list[dict[str, Any]] = []
    for (spell_id, spell_name), rows in sorted(
        observed_spells.items(), key=lambda item: (item[0][0] or -1, item[0][1])
    ):
        mechanic = by_spell.get(spell_id) if spell_id is not None else None
        if mechanic is None:
            observed.append(
                {
                    "spell_id": spell_id,
                    "spell_name": spell_name,
                    "rows": rows,
                    "status": "MISSING",
                    "reason": "action candidate is not mapped to the mechanics registry",
                }
            )
            continue
        implementation = mechanic["implementation"]
        calibrated = mechanic["calibrated"]
        cost_keys = sorted(
            {
                key
                for key in (*implementation.keys(), *calibrated.keys())
                if "rage_cost" in key
            }
        )
        if not cost_keys:
            observed.append(
                {
                    "spell_id": spell_id,
                    "spell_name": spell_name,
                    "rows": rows,
                    "mechanic": mechanic["mechanic"],
                    "status": "MISSING",
                    "reason": "registry does not identify the action's rage-cost transition",
                }
            )
            continue
        required = {"miss_refund_fraction"}
        required.update(
            key
            for key in implementation
            if key in ("extra_rage_spent_only_when_landed", "extra_rage_retained_on_miss")
        )
        missing: list[str] = []
        inferred: list[str] = []
        verified: list[str] = []
        for field_name in sorted(required):
            parameter = calibrated.get(field_name)
            parameter_verified = (
                isinstance(parameter, dict)
                and parameter.get("status") == "verified_deterministic"
                and parameter.get("confidence") == "high"
                and "current_character" not in field_name
            )
            unresolved = [
                value
                for value in mechanic["unresolved"]
                if value.get("field") == field_name
            ]
            if parameter_verified:
                verified.append(field_name)
            elif parameter is not None or field_name in implementation or unresolved:
                inferred.append(field_name)
            else:
                missing.append(field_name)
        status = "MISSING" if missing else "INFERRED" if inferred else "RECONSTRUCTABLE"
        observed.append(
            {
                "spell_id": spell_id,
                "spell_name": spell_name,
                "rows": rows,
                "mechanic": mechanic["mechanic"],
                "status": status,
                "cost_fields": cost_keys,
                "verified_outcome_fields": verified,
                "inferred_outcome_fields": inferred,
                "missing_outcome_fields": missing,
            }
        )
    statuses = {value["status"] for value in observed}
    if not observed or "MISSING" in statuses:
        status = "MISSING"
    elif "INFERRED" in statuses:
        status = "INFERRED"
    else:
        status = "RECONSTRUCTABLE"
    return _field(
        status,
        "base costs alone are insufficient; miss refunds and landed-dependent spending must also be verified",
        skills=observed,
    )


def _action_snapshot_field(rows: int, action_rows: int, evidence: str) -> dict[str, Any]:
    if rows == 0:
        status = "MISSING"
    elif action_rows and rows < action_rows:
        status = "INFERRED"
    else:
        status = "OBSERVED"
    return _field(
        status,
        evidence,
        rows=rows,
        action_candidate_rows=action_rows,
        action_point_coverage=(rows / action_rows if action_rows else None),
    )


def _identity(group: dict[str, Any]) -> dict[str, Any]:
    endpoint_guids = sorted(group["endpoint_guids"])
    info_guids = sorted(group["info_guids"])
    manifest_guids = sorted(
        {
            str(manifest.get("player_guid"))
            for manifest in group["manifests"]
            if manifest.get("player_guid")
        }
    )
    info_classes = sorted(group["info_classes"])
    guid = endpoint_guids[0] if len(endpoint_guids) == 1 else None

    if len(endpoint_guids) > 1 or len(info_guids) > 1 or len(manifest_guids) > 1:
        status = "AMBIGUOUS_GUID"
    elif any(value != "WARRIOR" for value in info_classes):
        status = "CLASS_MISMATCH"
    elif guid is None:
        status = "MISSING_GUID"
    elif info_guids and info_guids[0] != guid:
        status = "GUID_MISMATCH"
    elif manifest_guids and manifest_guids[0] != guid:
        status = "GUID_MISMATCH"
    else:
        info_verified = (
            group["parsed_info_rows"] > 0
            and info_classes == ["WARRIOR"]
            and info_guids == [guid]
        )
        manifest_verified = manifest_guids == [guid]
        if info_verified or manifest_verified:
            status = "VERIFIED"
        elif group["info_rows"]:
            status = "UNPARSEABLE_INFO"
        else:
            status = "MISSING_INFO_OR_MANIFEST"

    return {
        "verified": status == "VERIFIED",
        "status": status,
        "player_guid": guid,
        "endpoint_guids": endpoint_guids,
        "info_guids": info_guids,
        "manifest_guids": manifest_guids,
        "info_rows": group["info_rows"],
        "parsed_info_rows": group["parsed_info_rows"],
        "info_classes": info_classes,
    }


def _grade(type_counts: Counter[str]) -> str:
    families = {
        family
        for family, aliases in FAMILY_TYPE_ALIASES.items()
        if any(type_counts.get(alias, 0) for alias in aliases)
    }
    if GRADE_A_REQUIRED.issubset(families):
        return "A"
    if GRADE_B_REQUIRED.issubset(families):
        return "B"
    return "C"


def _finalize_group(
    group: dict[str, Any], by_spell: dict[int, dict[str, Any]], stream_grade: str
) -> dict[str, Any]:
    gain = group["rage_gain_rows"]
    loss = group["rage_loss_rows"]
    auto = group["auto_attack_rows"]
    identity = _identity(group)
    gcd_duration_coverage = _parameter_coverage(
        group["start_spells"], by_spell, kind="gcd"
    )
    cooldown_duration_coverage = _parameter_coverage(
        group["start_spells"], by_spell, kind="cooldown"
    )
    fields = {
        "combat_time": _action_snapshot_field(
            group["action_time_rows"],
            group["start_rows"],
            "normalized offset/time at each Chronicle START action-candidate point",
        ),
        "boss_phase": _field(
            "MISSING",
            "encounter identity is present, but no exact phase snapshot is exported",
        ),
        "estimated_remaining_time": _field(
            "MISSING",
            "requires remaining boss health and recent raid DPS state",
        ),
        "target_health_remaining": _field(
            "MISSING",
            "damage/heal deltas do not provide an absolute target-health anchor",
        ),
        "player_health_remaining": _field(
            "MISSING",
            "resource deltas do not provide an absolute player-health anchor",
        ),
        "target_count": _field(
            "INFERRED" if group["combat_target_guids"] else "MISSING",
            "distinct damaged target GUIDs over an encounter are not the simultaneous target count at an action point",
            encounter_distinct_damaged_targets=len(group["combat_target_guids"]),
        ),
        "target_selection": _field(
            "MISSING",
            "event targets do not identify the client's selected target at every action point",
        ),
        "rage_gain_delta_chronicle_units": _field(
            "OBSERVED" if gain else "MISSING",
            "RES rows whose target/source is the leaderboard player",
            rows=gain,
            total=group["rage_gain_units"],
            unit="Chronicle export units",
        ),
        "rage_loss_delta_chronicle_units": _field(
            "OBSERVED" if loss else "MISSING",
            "RES Loss Rage rows; zero rows means absent, not zero rage cost",
            rows=loss,
            total=group["rage_loss_units"],
            unit="Chronicle export units",
        ),
        "rage_unit_conversion_to_wow": _field(
            "INFERRED" if gain or loss else "MISSING",
            "trajectory v1 candidate Chronicle units / 10; not globally calibrated",
            candidate_divisor=10 if gain or loss else None,
        ),
        "absolute_rage_anchor": _field(
            "OBSERVED" if group["absolute_rage_rows"] else "MISSING",
            "direct absolute rage snapshot; delta rows and skill costs are not anchors",
            rows=group["absolute_rage_rows"],
        ),
        "skill_rage_cost_coverage": _parameter_coverage(
            group["start_spells"], by_spell, kind="rage_cost"
        ),
        "rage_spend_outcome_transition_coverage": _rage_spend_transition_coverage(
            group["start_spells"], by_spell
        ),
        "gcd_duration_parameter_coverage": gcd_duration_coverage,
        "gcd_remaining": (
            _action_snapshot_field(
                group["gcd_rows"],
                group["start_rows"],
                "direct normalized GCD remaining field at action-candidate points",
            )
            if group["gcd_rows"]
            else _field(
                "MISSING",
                "no direct remaining snapshot; duration parameters do not identify remaining without an initial state and complete transition replay",
                rows=0,
            )
        ),
        "cooldown_duration_parameter_coverage": cooldown_duration_coverage,
        "cooldown_remaining": (
            _action_snapshot_field(
                group["cooldown_rows"],
                group["start_rows"],
                "direct normalized cooldown remaining field at action-candidate points",
            )
            if group["cooldown_rows"]
            else _field(
                "MISSING",
                "no direct remaining snapshot; duration parameters do not identify remaining without an initial state and complete transition replay",
                rows=0,
            )
        ),
        "casting_remaining": _field(
            "MISSING",
            "START/GO intervals are server observations, not a complete cast-remaining snapshot at every action point",
            start_rows=group["start_rows"],
            go_rows=group["go_rows"],
        ),
        "swing_event_timestamps": _field(
            "OBSERVED" if auto else "MISSING",
            "Auto Attack event timestamps",
            rows=auto,
        ),
        "mainhand_offhand_swing_remaining": _field(
            _action_snapshot_field(
                group["swing_timer_rows"],
                group["start_rows"],
                "direct main/offhand remaining fields at action-candidate points; Auto Attack spacing alone cannot identify hand/reset state",
            )["status"],
            "direct main/offhand remaining fields at action-candidate points; Auto Attack spacing alone cannot identify hand/reset state",
            direct_rows=group["swing_timer_rows"],
            action_candidate_rows=group["start_rows"],
            action_point_coverage=(
                group["swing_timer_rows"] / group["start_rows"]
                if group["start_rows"]
                else None
            ),
            auto_attack_rows=auto,
        ),
        "queue_intent_timing": _action_snapshot_field(
            group["queue_intent_rows"],
            group["start_rows"],
            "client queue intent at action-candidate points; START/GO only prove server-side events",
        ),
        "client_keypress": _action_snapshot_field(
            group["keypress_rows"],
            group["start_rows"],
            "client hardware action timestamp at action-candidate points; START is not a keypress",
        ),
        "buffs_duration_stacks": _field(
            "MISSING",
            "AURA changes do not provide a complete initial snapshot and exact remaining durations",
            player_involved_aura_rows=group["aura_rows"],
        ),
        "target_debuffs_duration_stacks": _field(
            "MISSING",
            "target AURA changes do not provide complete debuff state and exact remaining durations",
            target_aura_rows=group["target_debuff_rows"],
        ),
        "stance_form": _field(
            "MISSING",
            "no complete stance/form snapshot at each action point",
        ),
        "range_and_behind": _field(
            "MISSING",
            "combat results do not identify exact range or behind-target state",
        ),
        "recent_actions": _field(
            "INFERRED" if group["start_rows"] or group["go_rows"] else "MISSING",
            "START candidates and GO observations require causal association and still omit the client keypress",
            start_rows=group["start_rows"],
            source_go_rows=group["go_rows"],
        ),
        "talent_tree_point_totals": _field(
            "RECONSTRUCTABLE" if group["talent_trees"] else "MISSING",
            "INFO detail exposes only three tree totals",
            values=[list(value) for value in sorted(group["talent_trees"])],
        ),
        "exact_talent_ranks": _field(
            "MISSING",
            "tree totals cannot identify individual talent ranks",
        ),
        "gear_slot_count": _field(
            "RECONSTRUCTABLE" if group["gear_slot_counts"] else "MISSING",
            "parsed deterministically from INFO detail",
            values=sorted(group["gear_slot_counts"]),
        ),
        "gear_item_ids": _field(
            "OBSERVED" if group["gear_items_observed"] else "MISSING",
            "per-slot item IDs are not present in Chronicle INFO detail",
        ),
        "player_stats": _field(
            "MISSING",
            "INFO detail has no exact derived stat snapshot",
        ),
        "raid_buffs_debuffs_initial_state": _field(
            "MISSING",
            "event changes do not provide a complete pull-time raid buff/debuff state",
        ),
        "latency_and_keyspam_interval": _field(
            "MISSING",
            "server event times do not expose client latency or repeated keypress cadence",
        ),
    }
    blockers = [
        name
        for name in FULL_STATE_REQUIRED_FIELDS
        if fields[name]["status"] in ("INFERRED", "MISSING")
    ]
    if not identity["verified"]:
        blockers.insert(0, "player_identity")
    return {
        "player_name": group["name"],
        "player_guid": identity["player_guid"],
        "identity": identity,
        "info_classes": sorted(group["info_classes"]),
        "talent_trees": [list(value) for value in sorted(group["talent_trees"])],
        "leaderboard_rows": group["leaderboard_rows"],
        "trajectory_manifests": group["manifests"],
        "roadmap_stream_grade": stream_grade,
        "stream_grade_a": stream_grade == "A",
        "full_state_ready": not blockers,
        "full_state_blockers": blockers,
        "fields": fields,
    }


def _scan_normalized(
    path: Path,
    targets: dict[str, dict[str, Any]],
    by_spell: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    type_counts: Counter[str] = Counter()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    invalid_rows = 0
    row_count = 0
    instance_ids: set[str] = set()
    matched_targets: set[str] = set()
    with path.open("r", encoding="utf-8", buffering=1024 * 1024) as handle:
        for line in handle:
            if not line.strip():
                continue
            row_count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_rows += 1
                continue
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            event_type = str(row.get("type") or "").upper()
            type_counts[event_type] += 1
            instance_ids.add(str(row.get("instance") or ""))
            encounter = str(row.get("encounter") or "")
            source_name = str(row.get("source") or "")
            target_name = str(row.get("target") or "")
            source_key = source_name.casefold()
            target_key = target_name.casefold()
            row_targets: dict[str, dict[str, Any]] = {}
            if source_key in targets:
                row_targets[source_key] = targets[source_key]
            if target_key in targets:
                row_targets[target_key] = targets[target_key]
            if not row_targets or not encounter:
                continue
            for player_key, target in row_targets.items():
                name = target["name"]
                matched_targets.add(player_key)
                group = groups.setdefault(
                    (player_key, encounter), _empty_group(name, target)
                )
                source_matches = source_key == player_key
                target_matches = target_key == player_key
                row_endpoint_guids: set[str] = set()
                if source_matches and row.get("source_guid"):
                    row_endpoint_guids.add(str(row["source_guid"]))
                if target_matches and row.get("target_guid"):
                    row_endpoint_guids.add(str(row["target_guid"]))
                group["endpoint_guids"].update(row_endpoint_guids)

                if event_type == "INFO":
                    group["info_rows"] += 1
                    group["info_guids"].update(row_endpoint_guids)
                    parsed = parse_combatant_info(row.get("outcome"))
                    if parsed:
                        group["parsed_info_rows"] += 1
                        group["info_classes"].add(str(parsed["class"]).upper())
                        group["talent_trees"].add(tuple(parsed["talent_tree"]))
                        group["gear_slot_counts"].add(parsed["gear_slot_count"])
                    if row.get("gear_items"):
                        group["gear_items_observed"] = True
                if source_matches and event_type == "START":
                    group["start_rows"] += 1
                    if row.get("offset_ms") is not None or row.get("time") is not None:
                        group["action_time_rows"] += 1
                    spell_id = row.get("spell_id") if isinstance(row.get("spell_id"), int) else None
                    group["start_spells"][(spell_id, str(row.get("spell") or ""))] += 1
                    if row.get("gcd_remaining_ms") is not None:
                        group["gcd_rows"] += 1
                    if row.get("cooldown_remaining_ms") is not None:
                        group["cooldown_rows"] += 1
                    if row.get("mainhand_swing_remaining_ms") is not None or row.get("offhand_swing_remaining_ms") is not None:
                        group["swing_timer_rows"] += 1
                    if row.get("queue_intent") is not None:
                        group["queue_intent_rows"] += 1
                    if row.get("client_keypress") is not None or row.get("keypress_at") is not None:
                        group["keypress_rows"] += 1
                if source_matches and event_type == "GO":
                    group["go_rows"] += 1
                    spell_id = row.get("spell_id") if isinstance(row.get("spell_id"), int) else None
                    group["go_spells"][(spell_id, str(row.get("spell") or ""))] += 1
                if event_type == "RES" and (target_matches or source_matches):
                    outcome = str(row.get("outcome") or "").casefold()
                    if "rage" in outcome:
                        amount = _number(row.get("value")) or 0.0
                        if "gain" in outcome:
                            group["rage_gain_rows"] += 1
                            group["rage_gain_units"] += amount
                        elif "loss" in outcome:
                            group["rage_loss_rows"] += 1
                            group["rage_loss_units"] += amount
                if source_matches and event_type in ("DMG", "DEAD") and (
                    row.get("spell_id") == 6603 or str(row.get("spell") or "").casefold() == "auto attack"
                ):
                    group["auto_attack_rows"] += 1
                if source_matches and event_type in ("DMG", "DEAD") and row.get("target_guid"):
                    if str(row["target_guid"]) not in group["endpoint_guids"]:
                        group["combat_target_guids"].add(str(row["target_guid"]))
                if event_type == "AURA":
                    group["aura_rows"] += 1
                    if source_matches and not target_matches:
                        group["target_debuff_rows"] += 1
                for key in ("absolute_rage", "rage_absolute"):
                    if row.get(key) is not None:
                        group["absolute_rage_rows"] += 1
                        break

    stream_grade = _grade(type_counts)
    keyed_player_encounters: list[tuple[str, dict[str, Any]]] = []
    endpoint_guids_by_target: dict[str, set[str]] = {}
    locally_verified_guids_by_target: dict[str, set[str]] = {}
    for (player_key, encounter), group in sorted(groups.items()):
        finalized = _finalize_group(group, by_spell, stream_grade)
        finalized["encounter_id"] = encounter
        keyed_player_encounters.append((player_key, finalized))
        endpoint_guids_by_target.setdefault(player_key, set()).update(
            finalized["identity"]["endpoint_guids"]
        )
        if finalized["identity"]["verified"] and finalized["player_guid"]:
            locally_verified_guids_by_target.setdefault(player_key, set()).add(
                finalized["player_guid"]
            )

    for player_key, finalized in keyed_player_encounters:
        identity = finalized["identity"]
        instance_guids = endpoint_guids_by_target[player_key]
        verified_guids = locally_verified_guids_by_target.get(player_key, set())
        if len(instance_guids) > 1:
            identity["verified"] = False
            identity["status"] = "AMBIGUOUS_INSTANCE_GUID"
            identity["instance_endpoint_guids"] = sorted(instance_guids)
            if "player_identity" not in finalized["full_state_blockers"]:
                finalized["full_state_blockers"].insert(0, "player_identity")
            finalized["full_state_ready"] = False
        elif (
            not identity["verified"]
            and identity["status"] in ("MISSING_INFO_OR_MANIFEST", "UNPARSEABLE_INFO")
            and len(verified_guids) == 1
            and finalized["player_guid"] in verified_guids
        ):
            identity["verified"] = True
            identity["status"] = "VERIFIED_INSTANCE_INFO"
            identity["verification_evidence"] = (
                "same normalized instance has parseable WARRIOR INFO for this name/GUID"
            )
            finalized["full_state_blockers"] = [
                value
                for value in finalized["full_state_blockers"]
                if value != "player_identity"
            ]
            finalized["full_state_ready"] = not finalized["full_state_blockers"]

    player_encounters = [value for _, value in keyed_player_encounters]
    identity_verified_targets: set[str] = set()
    identity_status_counts: Counter[str] = Counter()
    for player_key, finalized in keyed_player_encounters:
        identity_status_counts[finalized["identity"]["status"]] += 1
        if finalized["identity"]["verified"]:
            identity_verified_targets.add(player_key)
    return {
        "normalized_file": str(path.resolve()),
        "instance_ids": sorted(value for value in instance_ids if value),
        "row_count": row_count,
        "invalid_rows": invalid_rows,
        "event_type_counts": dict(sorted(type_counts.items())),
        "roadmap_stream_grade": stream_grade,
        "grade_a_is_stream_coverage_only": True,
        "leaderboard_target_count": len(targets),
        "name_matched_leaderboard_target_count": len(matched_targets),
        "identity_verified_leaderboard_target_count": len(identity_verified_targets),
        "identity_unverified_name_matched_target_count": len(
            matched_targets.difference(identity_verified_targets)
        ),
        "identity_status_counts": dict(sorted(identity_status_counts.items())),
        "unmatched_leaderboard_targets": sorted(
            target["name"]
            for key, target in targets.items()
            if key not in matched_targets
        ),
        "player_encounters": player_encounters,
    }


def _scan_jobs(
    jobs: list[tuple[Path, dict[str, dict[str, Any]]]],
    by_spell: dict[int, dict[str, Any]],
    workers: int,
) -> list[dict[str, Any]]:
    if workers < 1:
        raise ReconstructionAuditError("workers must be at least 1")
    if workers == 1 or len(jobs) <= 1:
        return [_scan_normalized(path, targets, by_spell) for path, targets in jobs]
    results: dict[int, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
        futures = {
            executor.submit(_scan_normalized, path, targets, by_spell): (index, path)
            for index, (path, targets) in enumerate(jobs)
        }
        for future in as_completed(futures):
            index, path = futures[future]
            try:
                results[index] = future.result()
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ReconstructionAuditError(
                    f"cannot scan normalized JSONL {path}: {error}"
                ) from error
    return [results[index] for index in range(len(jobs))]


def build_readiness_report(
    data_root: str | Path = DEFAULT_DATA_ROOT,
    *,
    registry: str | Path = DEFAULT_REGISTRY,
    normalized: Iterable[str | Path] | None = None,
    manifests: Iterable[str | Path] | None = None,
    workers: int = min(8, os.cpu_count() or 1),
) -> dict[str, Any]:
    root = Path(data_root).expanduser().resolve()
    registry_path = Path(registry).expanduser().resolve()
    manifest_paths = (
        [Path(value).expanduser().resolve() for value in manifests]
        if manifests is not None
        else sorted(root.glob(MANIFEST_GLOB))
    )
    all_targets, queue_path = _load_targets(root, manifest_paths)
    if normalized is not None:
        normalized_paths = [Path(value).expanduser().resolve() for value in normalized]
        requested_names = {path.name for path in normalized_paths}
        targets = {
            name: value for name, value in all_targets.items() if name in requested_names
        }
    else:
        normalized_paths = sorted((root / "normalized").glob("*.jsonl"))
        targets = all_targets
    missing = [path for path in normalized_paths if not path.is_file()]
    if missing:
        raise ReconstructionAuditError(f"normalized JSONL does not exist: {missing[0]}")
    available_names = {path.name for path in normalized_paths}
    missing_target_files = [
        {
            "normalized_file": name,
            "leaderboard_targets": sorted(
                target["name"] for target in targets[name].values()
            ),
        }
        for name in sorted(set(targets).difference(available_names))
    ]
    by_spell, registry_info = _calibrated_registry(registry_path)
    jobs = [
        (path, targets[path.name])
        for path in normalized_paths
        if targets.get(path.name)
    ]
    scanned_instances = _scan_jobs(jobs, by_spell, workers)
    player_encounters = [
        {"normalized_file": instance["normalized_file"], **group}
        for instance in scanned_instances
        for group in instance["player_encounters"]
    ]
    instances = [
        {key: value for key, value in instance.items() if key != "player_encounters"}
        for instance in scanned_instances
    ]
    status_counts = Counter(
        field["status"]
        for group in player_encounters
        for field in group["fields"].values()
    )
    field_names = sorted(
        {
            name
            for group in player_encounters
            for name in group["fields"]
        }
    )
    field_status_by_field = {
        name: {
            status: sum(
                1
                for group in player_encounters
                if group["fields"][name]["status"] == status
            )
            for status in STATUSES
        }
        for name in field_names
    }
    blocker_counts = Counter(
        blocker
        for group in player_encounters
        for blocker in group["full_state_blockers"]
    )
    expected_target_count = sum(len(value) for value in targets.values())
    name_matched_target_count = sum(
        item["name_matched_leaderboard_target_count"] for item in instances
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": SCHEMA_NAME,
        "generated_at": _utc_now(),
        "scope": {
            "class": "WARRIOR",
            "players": "leaderboard rows only; raid members are excluded",
            "grain": "normalized instance x leaderboard player x encounter",
        },
        "evidence_boundary": {
            "roadmap_stream_grade": "event-family presence at normalized-instance scope",
            "grade_a_not_full_state": True,
            "full_state_ready": "strict field-level result; INFERRED or MISSING required state blocks readiness",
            "inputs_opened_read_only": True,
            "raw_or_normalized_modified": False,
            "experiments_started": False,
        },
        "inputs": {
            "data_root": str(root),
            "queue": str(queue_path.resolve()),
            "trajectory_manifests": [str(path) for path in manifest_paths],
            "registry": registry_info,
            "workers": workers,
            "normalized_files_requested": len(normalized_paths),
            "normalized_files_with_leaderboard_warriors": len(instances),
            "missing_normalized_files_for_leaderboard_targets": missing_target_files,
        },
        "summary": {
            "leaderboard_player_encounters": len(player_encounters),
            "identity_verified_player_encounters": sum(
                1 for item in player_encounters if item["identity"]["verified"]
            ),
            "identity_unverified_player_encounters": sum(
                1 for item in player_encounters if not item["identity"]["verified"]
            ),
            "leaderboard_targets": expected_target_count,
            "name_matched_leaderboard_targets": name_matched_target_count,
            "identity_verified_leaderboard_targets": sum(
                item["identity_verified_leaderboard_target_count"] for item in instances
            ),
            "identity_unverified_name_matched_targets": sum(
                item["identity_unverified_name_matched_target_count"]
                for item in instances
            ),
            "unmatched_leaderboard_targets": expected_target_count
            - name_matched_target_count,
            "stream_grade_counts": dict(sorted(Counter(item["roadmap_stream_grade"] for item in instances).items())),
            "full_state_ready": sum(1 for item in player_encounters if item["full_state_ready"]),
            "field_status_counts": {status: status_counts.get(status, 0) for status in STATUSES},
            "field_status_by_field": field_status_by_field,
            "full_state_blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "instances": instances,
        "player_encounters": player_encounters,
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Warrior Chronicle Reconstruction Readiness",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Result",
        "",
        f"- Leaderboard targets: {summary['leaderboard_targets']}",
        f"- Name matched: {summary['name_matched_leaderboard_targets']}",
        f"- Identity verified: {summary['identity_verified_leaderboard_targets']}",
        f"- Name matched but identity unverified: {summary['identity_unverified_name_matched_targets']}",
        f"- Unmatched: {summary['unmatched_leaderboard_targets']}",
        f"- Leaderboard Warrior player x encounters: {summary['leaderboard_player_encounters']}",
        f"- Identity-verified player x encounters: {summary['identity_verified_player_encounters']}",
        f"- Identity-unverified player x encounters: {summary['identity_unverified_player_encounters']}",
        f"- Full-state ready: {summary['full_state_ready']}",
        "- Grade A is stream-family coverage, not full-state readiness.",
        "- Raw and normalized inputs were opened read-only; no experiment was started.",
        "",
        "## Field status summary",
        "",
        "| Field | Observed | Reconstructable | Inferred | Missing |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, counts in summary["field_status_by_field"].items():
        lines.append(
            f"| {name} | {counts['OBSERVED']} | {counts['RECONSTRUCTABLE']} | "
            f"{counts['INFERRED']} | {counts['MISSING']} |"
        )
    lines.extend(
        (
            "",
            "## Full-state blockers",
            "",
            "| Field | Player x encounters blocked |",
            "|---|---:|",
        )
    )
    for name, count in summary["full_state_blocker_counts"].items():
        lines.append(f"| {name} | {count} |")
    lines.extend(
        (
        "",
        "## Player x encounter",
        "",
        "| Player | Encounter | Identity | Stream grade | Full state | Rage gain/loss | Rage scale | Rage anchor | Costs | GCD | CD | Swing | Queue | Keypress | Gear |",
        "|---|---|---|---:|---:|---|---|---|---|---|---|---|---|---|---|",
        )
    )
    for item in report["player_encounters"]:
        fields = item["fields"]
        lines.append(
            "| "
            + " | ".join(
                str(value).replace("|", "\\|")
                for value in (
                    item["player_name"],
                    item["encounter_id"],
                    item["identity"]["status"],
                    item["roadmap_stream_grade"],
                    "READY" if item["full_state_ready"] else "NOT READY",
                    fields["rage_gain_delta_chronicle_units"]["status"] + "/" + fields["rage_loss_delta_chronicle_units"]["status"],
                    fields["rage_unit_conversion_to_wow"]["status"],
                    fields["absolute_rage_anchor"]["status"],
                    fields["skill_rage_cost_coverage"]["status"],
                    fields["gcd_remaining"]["status"],
                    fields["cooldown_remaining"]["status"],
                    fields["mainhand_offhand_swing_remaining"]["status"],
                    fields["queue_intent_timing"]["status"],
                    fields["client_keypress"]["status"],
                    fields["gear_item_ids"]["status"],
                )
            )
            + " |"
        )
    issues = []
    unverified_by_file: dict[str, list[dict[str, Any]]] = {}
    for item in report["player_encounters"]:
        if not item["identity"]["verified"]:
            unverified_by_file.setdefault(
                Path(item["normalized_file"]).name, []
            ).append(item)
    for instance in report["instances"]:
        normalized_name = Path(instance["normalized_file"]).name
        if instance["unmatched_leaderboard_targets"]:
            issues.append(
                f"- `{normalized_name}` unmatched: "
                + ", ".join(instance["unmatched_leaderboard_targets"])
            )
        unverified = unverified_by_file.get(normalized_name, [])
        if unverified:
            rendered = ", ".join(
                f"{item['player_name']}@{item['encounter_id']} ({item['identity']['status']})"
                for item in unverified
            )
            issues.append(f"- `{normalized_name}` identity: {rendered}")
    for missing in report["inputs"]["missing_normalized_files_for_leaderboard_targets"]:
        issues.append(
            f"- `{missing['normalized_file']}` missing normalized file for: "
            + ", ".join(missing["leaderboard_targets"])
        )
    lines.extend(("", "## Target and identity issues", ""))
    lines.extend(issues or ("- None.",))
    lines.extend(("", "## Status meanings", ""))
    lines.extend(
        (
            "- `OBSERVED`: exported directly.",
            "- `RECONSTRUCTABLE`: deterministic join/parse using observed events and live-calibrated registry facts.",
            "- `INFERRED`: model or uncalibrated candidate; not ground truth.",
            "- `MISSING`: no evidence sufficient for the field.",
            "",
        )
    )
    return "\n".join(lines)


def write_reports(
    report: dict[str, Any], *, data_root: str | Path, report_stem: str = DEFAULT_REPORT_STEM
) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", report_stem):
        raise ReconstructionAuditError("report stem contains unsupported characters")
    output = Path(data_root).expanduser().resolve() / "reports"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{report_stem}.json"
    markdown_path = output / f"{report_stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def audit_reconstruction_readiness(
    data_root: str | Path = DEFAULT_DATA_ROOT,
    *,
    registry: str | Path = DEFAULT_REGISTRY,
    normalized: Iterable[str | Path] | None = None,
    manifests: Iterable[str | Path] | None = None,
    report_stem: str = DEFAULT_REPORT_STEM,
    workers: int = min(8, os.cpu_count() or 1),
) -> ReconstructionAuditResult:
    report = build_readiness_report(
        data_root,
        registry=registry,
        normalized=normalized,
        manifests=manifests,
        workers=workers,
    )
    json_report, markdown_report = write_reports(
        report, data_root=data_root, report_stem=report_stem
    )
    return ReconstructionAuditResult(report, json_report, markdown_report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit field-level Chronicle reconstruction readiness for leaderboard Warriors."
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--normalized", type=Path, action="append")
    parser.add_argument("--manifest", type=Path, action="append", dest="manifests")
    parser.add_argument("--report-stem", default=DEFAULT_REPORT_STEM)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel normalized JSONL scanners (default: up to 8)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = audit_reconstruction_readiness(
            args.data_root,
            registry=args.registry,
            normalized=args.normalized,
            manifests=args.manifests,
            report_stem=args.report_stem,
            workers=args.workers,
        )
    except ReconstructionAuditError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "player_encounters": result.report["summary"]["leaderboard_player_encounters"],
                "full_state_ready": result.report["summary"]["full_state_ready"],
                "unmatched_leaderboard_targets": result.report["summary"]["unmatched_leaderboard_targets"],
                "identity_unverified_name_matched_targets": result.report["summary"]["identity_unverified_name_matched_targets"],
                "identity_unverified_player_encounters": result.report["summary"]["identity_unverified_player_encounters"],
                "json_report": str(result.json_report),
                "markdown_report": str(result.markdown_report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
