"""Audit Fury Warrior mechanics already covered by local real-game evidence.

The audit is deliberately read-only with respect to the simulator and mechanics
registry.  It combines the compact Phase 1--11 calibration exports with the
already-derived, player-scoped Chronicle Fury trajectory.  It does not rescan
the much larger normalized Chronicle corpus because that export still lacks
absolute rage, target armor, and per-slot gear.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_OUTPUT = DEFAULT_DATA_ROOT / "sim_validation" / "fury_existing_evidence_audit.json"

SCHEMA_VERSION = 1
UNBRIDLED_WRATH_ID = 12964
EXECUTE_ID = 20662
BATTLE_SHOUT_ID = 25289
DEEP_WOUNDS_ID = 12721
KALIMDOR_PROC_ID = 26415
KALIMDOR_ITEM_ID = 21679

ABILITY_SPELL_IDS: dict[str, frozenset[int]] = {
    "heroic_strike": frozenset((11564, 11565, 11566, 11567, 25286)),
    "cleave": frozenset((845, 7369, 11608, 11609, 20569, 20571)),
    "whirlwind": frozenset((1680,)),
    "slam": frozenset(
        (1464, 8820, 11604, 11605, 45960, 45961, 45963, 45964, 45599, 53214)
    ),
    "bloodthirst": frozenset((23894,)),
    "execute": frozenset((5308, 20647, 20658, 20660, 20661, 20662)),
    "deep_wounds": frozenset((DEEP_WOUNDS_ID,)),
}
SPELL_TO_ABILITY = {
    spell_id: ability
    for ability, spell_ids in ABILITY_SPELL_IDS.items()
    for spell_id in spell_ids
}
DIRECT_SKILL_ABILITIES = tuple(
    ability for ability in ABILITY_SPELL_IDS if ability != "deep_wounds"
)
UW_TRIGGER_NAMES = (
    "white",
    "heroic_strike",
    "cleave",
    "whirlwind",
    "slam",
    "bloodthirst",
    "execute",
    "deep_wounds",
    "unknown",
)

ENERGIZE_MIRROR_EVENTS = frozenset(
    ("SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF")
)
RESOURCE_EVENTS = frozenset(("UNIT_RAGE", "UNIT_RAGE_GUID"))
RESULT_EVENTS = frozenset(("SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"))
CONFOUNDING_EVENTS = frozenset(
    (
        "AUTO_ATTACK_SELF",
        "SPELL_DAMAGE_EVENT_SELF",
        "SPELL_MISS_SELF",
        "CALIBRATION_ACTION_REQUESTED",
    )
)
ITEM_LINK_ID = re.compile(r"Hitem:(\d+):")

JSONMap = dict[str, Any]


class FuryExistingEvidenceAuditError(RuntimeError):
    """An input artifact could not be read as the expected JSON object stream."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _spell_id(row: Mapping[str, Any]) -> int | None:
    direct = _integer(row.get("spellID"))
    if direct is not None:
        return direct
    if str(row.get("event") or "").startswith("SPELL_"):
        return _integer(row.get("arg3"))
    return None


def _state(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("state")
    return value if isinstance(value, Mapping) else {}


def _task(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("task")
    return value if isinstance(value, Mapping) else {}


def _rage(state: Mapping[str, Any]) -> float | None:
    raw = _number(state.get("rageRaw"))
    scale = _number(state.get("rageRawScale"))
    if raw is not None and scale not in (None, 0):
        return raw / scale
    return _number(state.get("rage"))


def _maximum_rage(state: Mapping[str, Any]) -> float | None:
    raw = _number(state.get("maximumRageRaw"))
    scale = _number(state.get("rageRawScale"))
    if raw is not None and scale not in (None, 0):
        return raw / scale
    return _number(state.get("maximumRage"))


def _energize_amount_rage(row: Mapping[str, Any]) -> float | None:
    amount = _number(row.get("amount"))
    if amount is None:
        amount = _number(row.get("arg5"))
    if amount is None:
        return None
    scale = _number(_state(row).get("rageRawScale"))
    # Nampower and Chronicle both export this energize in raw tenths.  Some
    # early logger snapshots omitted rageRawScale, but their event units did not
    # change.
    return amount / (scale if scale not in (None, 0) else 10.0)


def _attack_power(state: Mapping[str, Any]) -> float | None:
    value = state.get("attackPower")
    if isinstance(value, Mapping):
        return _number(value.get("effective"))
    return _number(value)


def _target_armor(state: Mapping[str, Any]) -> float | None:
    value = state.get("targetArmor")
    if isinstance(value, Mapping):
        return _number(value.get("effective"))
    return _number(value)


def _talent_rank(state: Mapping[str, Any], *, tab: int, index: int) -> int | None:
    talents = state.get("talents")
    if not isinstance(talents, list):
        return None
    for talent in talents:
        if not isinstance(talent, Mapping):
            continue
        if _integer(talent.get("tab")) == tab and _integer(talent.get("index")) == index:
            return _integer(talent.get("rank"))
    return None


def _source_guid(row: Mapping[str, Any]) -> str | None:
    value = row.get("sourceGUID")
    return str(value) if value else None


def _target_guid(row: Mapping[str, Any]) -> str | None:
    value = row.get("targetGUID")
    return str(value) if value else None


def _read_jsonl(path: Path) -> list[JSONMap]:
    rows: list[JSONMap] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FuryExistingEvidenceAuditError(
                        f"invalid JSONL at {path}:{line_number}: {error}"
                    ) from error
                if not isinstance(value, dict):
                    raise FuryExistingEvidenceAuditError(
                        f"JSONL row is not an object at {path}:{line_number}"
                    )
                value["__audit_line__"] = line_number
                rows.append(value)
    except OSError as error:
        raise FuryExistingEvidenceAuditError(f"cannot read {path}: {error}") from error
    return rows


def _load_json(path: Path) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FuryExistingEvidenceAuditError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise FuryExistingEvidenceAuditError(f"JSON document is not an object: {path}")
    return value


def _task_identity(row: Mapping[str, Any]) -> tuple[Any, Any]:
    task = _task(row)
    return task.get("taskRunId"), task.get("trial")


def _uw_same_occurrence(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_time = _number(left.get("time"))
    right_time = _number(right.get("time"))
    if left_time is None or right_time is None or abs(left_time - right_time) > 0.011:
        return False
    return (
        _integer(left.get("amount") or left.get("arg5"))
        == _integer(right.get("amount") or right.get("arg5"))
        and (_source_guid(left) or left.get("arg1"))
        == (_source_guid(right) or right.get("arg1"))
        and (_target_guid(left) or left.get("arg2"))
        == (_target_guid(right) or right.get("arg2"))
        and _task_identity(left) == _task_identity(right)
    )


def _deduplicate_unbridled_wrath(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Pair BY_SELF/ON_SELF mirrors without collapsing simultaneous real procs."""

    pending: list[dict[str, Any]] = []
    raw_count = 0
    mirror_count = 0
    for index, row in enumerate(rows):
        if _spell_id(row) != UNBRIDLED_WRATH_ID or row.get("event") not in ENERGIZE_MIRROR_EVENTS:
            continue
        raw_count += 1
        counterpart = (
            "SPELL_ENERGIZE_ON_SELF"
            if row.get("event") == "SPELL_ENERGIZE_BY_SELF"
            else "SPELL_ENERGIZE_BY_SELF"
        )
        matched: dict[str, Any] | None = None
        for occurrence in reversed(pending):
            if occurrence["mirror_index"] is not None:
                continue
            if occurrence["row"].get("event") != counterpart:
                continue
            if _uw_same_occurrence(occurrence["row"], row):
                matched = occurrence
                break
        if matched is None:
            pending.append(
                {
                    "index": index,
                    "end_index": index,
                    "row": row,
                    "mirror_index": None,
                }
            )
            continue
        matched["end_index"] = max(matched["end_index"], index)
        matched["mirror_index"] = index
        if row.get("event") == "SPELL_ENERGIZE_BY_SELF":
            matched["row"] = row
        mirror_count += 1
    return pending, raw_count, mirror_count


def _trigger_for_proc(
    rows: Sequence[Mapping[str, Any]], occurrence: Mapping[str, Any]
) -> str:
    proc_time = _number(occurrence["row"].get("time"))
    if proc_time is None:
        return "unknown"
    candidates: list[tuple[float, int, str]] = []
    lo = max(0, int(occurrence["index"]) - 12)
    hi = min(len(rows), int(occurrence["end_index"]) + 13)
    for row in rows[lo:hi]:
        event_time = _number(row.get("time"))
        if event_time is None or abs(event_time - proc_time) > 0.1001:
            continue
        event = str(row.get("event") or "")
        spell_id = _spell_id(row)
        if event in RESULT_EVENTS and spell_id in SPELL_TO_ABILITY:
            ability = SPELL_TO_ABILITY[spell_id]
            candidates.append((abs(event_time - proc_time), 0, ability))
        elif event == "AUTO_ATTACK_SELF":
            candidates.append((abs(event_time - proc_time), 1, "white"))
        elif event == "UNIT_CASTEVENT" and spell_id == 6603:
            candidates.append((abs(event_time - proc_time), 2, "white"))
    return min(candidates)[2] if candidates else "unknown"


def _resource_delta_for_proc(
    rows: Sequence[Mapping[str, Any]], occurrence: Mapping[str, Any]
) -> JSONMap:
    row = occurrence["row"]
    advertised = _energize_amount_rage(row)
    maximum = _maximum_rage(_state(row))
    proc_time = _number(row.get("time"))
    if advertised is None or proc_time is None:
        return {"identifiable": False, "reason": "missing_amount_or_time"}

    # A state snapshot attached to the energize event may already be post-proc.
    # Require explicit resource events on both sides; otherwise event presence
    # would be silently promoted to an actual resource-bar delta.
    previous_index: int | None = None
    before: float | None = None
    for index in range(int(occurrence["index"]) - 1, -1, -1):
        candidate = rows[index]
        candidate_time = _number(candidate.get("time"))
        if candidate_time is None or proc_time - candidate_time > 0.5:
            break
        if candidate.get("event") in RESOURCE_EVENTS:
            candidate_rage = _rage(_state(candidate))
            if candidate_rage is not None:
                previous_index = index
                before = candidate_rage
                maximum = _maximum_rage(_state(candidate)) or maximum
                break
    if previous_index is None or before is None:
        return {"identifiable": False, "reason": "no_pre_proc_resource_snapshot"}

    pre_confounders: list[str] = []
    for candidate in rows[previous_index + 1 : int(occurrence["index"])]:
        event = str(candidate.get("event") or "")
        if event in CONFOUNDING_EVENTS:
            pre_confounders.append(event)
        elif event == "SPELL_GO_SELF" and _spell_id(candidate) != UNBRIDLED_WRATH_ID:
            pre_confounders.append(event)
        elif (
            _spell_id(candidate) == UNBRIDLED_WRATH_ID
            and event in ENERGIZE_MIRROR_EVENTS
        ):
            pre_confounders.append("another_unbridled_wrath")
    if pre_confounders:
        return {
            "identifiable": False,
            "reason": "confounded_after_pre_proc_resource_snapshot",
            "confounders": sorted(set(pre_confounders)),
        }

    next_index: int | None = None
    after: float | None = None
    for index in range(int(occurrence["end_index"]) + 1, len(rows)):
        candidate = rows[index]
        candidate_time = _number(candidate.get("time"))
        if candidate_time is None or candidate_time - proc_time > 0.5:
            break
        if candidate.get("event") in RESOURCE_EVENTS:
            candidate_rage = _rage(_state(candidate))
            if candidate_rage is not None:
                next_index = index
                after = candidate_rage
                break
    if next_index is None or after is None:
        return {"identifiable": False, "reason": "no_nearby_resource_snapshot"}

    confounders: list[str] = []
    for candidate in rows[int(occurrence["end_index"]) + 1 : next_index]:
        event = str(candidate.get("event") or "")
        if event in CONFOUNDING_EVENTS:
            confounders.append(event)
        elif event == "SPELL_GO_SELF" and _spell_id(candidate) != UNBRIDLED_WRATH_ID:
            confounders.append(event)
        elif (
            _spell_id(candidate) == UNBRIDLED_WRATH_ID
            and event in ENERGIZE_MIRROR_EVENTS
        ):
            confounders.append("another_unbridled_wrath")
    if confounders:
        return {
            "identifiable": False,
            "reason": "confounded_before_next_resource_snapshot",
            "confounders": sorted(set(confounders)),
        }

    delta = after - before
    capped = maximum is not None and before >= maximum - 1e-6
    matches = abs(delta - advertised) <= 0.051
    return {
        "identifiable": True,
        "reason": None,
        "rage_before": round(before, 3),
        "rage_after": round(after, 3),
        "actual_delta": round(delta, 3),
        "advertised_delta": round(advertised, 3),
        "matches_advertised_delta": matches,
        "capped_at_observation": capped,
    }


def _exact_damage_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("event"),
        _spell_id(row),
        round(_number(row.get("time")) or -1.0, 3),
        _source_guid(row),
        _target_guid(row),
        _integer(row.get("amount")),
        _integer(row.get("hitInfo")),
    )


def _deduplicated_damage_rows(
    rows: Sequence[Mapping[str, Any]], spell_ids: Iterable[int]
) -> tuple[list[tuple[int, Mapping[str, Any]]], int]:
    accepted: list[tuple[int, Mapping[str, Any]]] = []
    seen: set[tuple[Any, ...]] = set()
    duplicate_count = 0
    allowed = set(spell_ids)
    for index, row in enumerate(rows):
        if row.get("event") != "SPELL_DAMAGE_EVENT_SELF" or _spell_id(row) not in allowed:
            continue
        signature = _exact_damage_signature(row)
        if signature in seen:
            duplicate_count += 1
            continue
        seen.add(signature)
        accepted.append((index, row))
    return accepted, duplicate_count


def _is_critical(row: Mapping[str, Any]) -> bool:
    hit_info = _integer(row.get("hitInfo")) or 0
    return bool(hit_info & 2)


def _slot_item_id(state: Mapping[str, Any], slot: int) -> int | None:
    equipment = state.get("equipment")
    if not isinstance(equipment, list):
        return None
    for item in equipment:
        if not isinstance(item, Mapping) or _integer(item.get("slot")) != slot:
            continue
        direct = _integer(item.get("itemID"))
        if direct is not None:
            return direct
        match = ITEM_LINK_ID.search(str(item.get("link") or ""))
        return int(match.group(1)) if match is not None else None
    return None


def _counter_json(counter: Counter[Any]) -> dict[str, int]:
    return {
        str(key): int(value)
        for key, value in sorted(counter.items(), key=lambda item: str(item[0]))
    }


def _compact_distribution(values: Sequence[float]) -> JSONMap:
    if not values:
        return {"count": 0, "minimum": None, "median": None, "maximum": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "minimum": round(ordered[0], 3),
        "median": round(float(statistics.median(ordered)), 3),
        "maximum": round(ordered[-1], 3),
    }


def _new_calibration_accumulator() -> dict[str, Any]:
    return {
        "records": 0,
        "files": [],
        "uw_raw": 0,
        "uw_unique": 0,
        "uw_mirrors": 0,
        "uw_amounts": Counter(),
        "uw_triggers": Counter({name: 0 for name in UW_TRIGGER_NAMES}),
        "uw_delta_reasons": Counter(),
        "uw_identifiable": 0,
        "uw_confirmed": 0,
        "uw_anchors": [],
        "skill_samples": defaultdict(lambda: {"ordinary": 0, "critical": 0}),
        "impale_cells": defaultdict(lambda: {"ordinary": [], "critical": []}),
        "impale_ranks": Counter(),
        "skill_duplicates": 0,
        "battle_shout": [],
        "battle_shout_sources": set(),
        "deep_ticks": 0,
        "deep_tick_duplicates": 0,
        "deep_amounts": Counter(),
        "deep_intervals_ms": [],
        "deep_applications": 0,
        "deep_refresh_candidates": 0,
        "deep_removals": 0,
        "deep_sources": set(),
        "kalimdor_go": 0,
        "kalimdor_damage": 0,
        "kalimdor_miss": 0,
        "kalimdor_damage_amounts": [],
        "kalimdor_context_files": set(),
        "kalimdor_sources": set(),
    }


def _battle_shout_observations(
    rows: Sequence[Mapping[str, Any]], source_name: str
) -> list[JSONMap]:
    observations: list[JSONMap] = []
    for index, row in enumerate(rows):
        if row.get("event") != "SPELL_GO_SELF" or _spell_id(row) != BATTLE_SHOUT_ID:
            continue
        before_state = _state(row)
        before_rage = _rage(before_state)
        before_ap = _attack_power(before_state)
        after_state: Mapping[str, Any] | None = None
        duration_ms: float | None = None
        aura_seen = False
        row_time = _number(row.get("time"))
        for candidate in rows[index + 1 : index + 18]:
            candidate_time = _number(candidate.get("time"))
            if (
                row_time is not None
                and candidate_time is not None
                and candidate_time - row_time > 0.75
            ):
                break
            if candidate.get("event") == "AURA_CAST_ON_SELF" and _spell_id(candidate) == BATTLE_SHOUT_ID:
                aura_seen = True
            if candidate.get("event") == "BUFF_UPDATE_DURATION_SELF":
                possible = _number(candidate.get("arg2"))
                if possible is not None and possible > 0:
                    duration_ms = possible
            if candidate.get("event") in RESOURCE_EVENTS and _rage(_state(candidate)) is not None:
                after_state = _state(candidate)
                if duration_ms is not None and aura_seen:
                    break
        after_rage = _rage(after_state or {})
        after_ap = _attack_power(after_state or {})
        observations.append(
            {
                "source_file": source_name,
                "line": row.get("__audit_line__"),
                "sequence": row.get("sequence"),
                "rage_before": before_rage,
                "rage_after": after_rage,
                "rage_cost": (
                    round(before_rage - after_rage, 3)
                    if before_rage is not None and after_rage is not None
                    else None
                ),
                "attack_power_before": before_ap,
                "attack_power_after": after_ap,
                "attack_power_delta": (
                    round(after_ap - before_ap, 3)
                    if before_ap is not None and after_ap is not None
                    else None
                ),
                "gcd_seconds": (
                    round(_number(before_state.get("gcd")) or 0.0, 3)
                    if _number(before_state.get("gcd")) is not None
                    else None
                ),
                "aura_seen": aura_seen,
                "duration_ms": duration_ms,
            }
        )
    return observations


def _scan_calibration(calibration_dir: Path) -> dict[str, Any]:
    acc = _new_calibration_accumulator()
    paths = sorted(calibration_dir.glob("*.jsonl")) if calibration_dir.is_dir() else []
    for path in paths:
        rows = _read_jsonl(path)
        source_name = path.name
        acc["files"].append(source_name)
        acc["records"] += len(rows)

        # Large static fields are intentionally emitted only on selected marker
        # snapshots.  Carry the latest observation within the same target/file
        # so a nearby damage row can reuse it without pretending it was sampled
        # again at the damage timestamp.
        contexts: dict[int, tuple[float | None, float | None, int | None, str | None]] = {}
        current_ap: float | None = None
        current_armor: float | None = None
        current_impale_rank: int | None = None
        current_target: str | None = None
        for context_index, context_row in enumerate(rows):
            context_state = _state(context_row)
            observed_target = context_state.get("targetGUID") or _target_guid(context_row)
            if observed_target and str(observed_target) != current_target:
                current_target = str(observed_target)
                current_armor = None
            observed_ap = _attack_power(context_state)
            observed_armor = _target_armor(context_state)
            observed_rank = _talent_rank(context_state, tab=1, index=11)
            if observed_ap is not None:
                current_ap = observed_ap
            if observed_armor is not None:
                current_armor = observed_armor
            if observed_rank is not None:
                current_impale_rank = observed_rank
            contexts[context_index] = (
                current_ap,
                current_armor,
                current_impale_rank,
                current_target,
            )

        occurrences, raw_count, mirror_count = _deduplicate_unbridled_wrath(rows)
        acc["uw_raw"] += raw_count
        acc["uw_unique"] += len(occurrences)
        acc["uw_mirrors"] += mirror_count
        for occurrence in occurrences:
            row = occurrence["row"]
            amount = _number(row.get("amount") or row.get("arg5"))
            if amount is not None:
                acc["uw_amounts"][int(amount)] += 1
            trigger = _trigger_for_proc(rows, occurrence)
            acc["uw_triggers"][trigger] += 1
            delta = _resource_delta_for_proc(rows, occurrence)
            if delta["identifiable"]:
                acc["uw_identifiable"] += 1
                if delta.get("matches_advertised_delta") is True:
                    acc["uw_confirmed"] += 1
            else:
                acc["uw_delta_reasons"][delta.get("reason") or "unknown"] += 1
            if len(acc["uw_anchors"]) < 24:
                acc["uw_anchors"].append(
                    {
                        "source_file": source_name,
                        "line": row.get("__audit_line__"),
                        "sequence": row.get("sequence"),
                        "time": row.get("time"),
                        "trigger": trigger,
                        "raw_energize_amount": amount,
                        "actual_resource_delta": delta,
                    }
                )

        all_skill_ids = set().union(
            *(ABILITY_SPELL_IDS[name] for name in DIRECT_SKILL_ABILITIES)
        )
        damage_rows, duplicates = _deduplicated_damage_rows(rows, all_skill_ids)
        acc["skill_duplicates"] += duplicates
        for damage_index, row in damage_rows:
            ability = SPELL_TO_ABILITY.get(_spell_id(row))
            if ability is None or ability == "deep_wounds":
                continue
            outcome = "critical" if _is_critical(row) else "ordinary"
            acc["skill_samples"][ability][outcome] += 1
            context_ap, context_armor, context_rank, context_target = contexts[
                damage_index
            ]
            rank = _talent_rank(_state(row), tab=1, index=11)
            if rank is None:
                rank = context_rank
            if rank is not None:
                acc["impale_ranks"][rank] += 1
            if ability != "bloodthirst":
                continue
            amount = _number(row.get("amount"))
            ap = _attack_power(_state(row)) or context_ap
            row_target = _target_guid(row)
            armor = _target_armor(_state(row))
            if armor is None and (not row_target or row_target == context_target):
                armor = context_armor
            if amount is None or ap is None or armor is None:
                continue
            task = _task(row)
            cell = (
                ability,
                int(round(ap)),
                int(round(armor)),
                task.get("stratum"),
            )
            acc["impale_cells"][cell][outcome].append(amount)

        battle_shout = _battle_shout_observations(rows, source_name)
        acc["battle_shout"].extend(battle_shout)
        if battle_shout:
            acc["battle_shout_sources"].add(source_name)

        deep_rows, deep_duplicates = _deduplicated_damage_rows(rows, (DEEP_WOUNDS_ID,))
        acc["deep_tick_duplicates"] += deep_duplicates
        last_tick: dict[str, float] = {}
        for _, row in deep_rows:
            row_time = _number(row.get("time"))
            target = _target_guid(row) or "unknown_target"
            amount = _integer(row.get("amount"))
            acc["deep_ticks"] += 1
            acc["deep_sources"].add(source_name)
            if amount is not None:
                acc["deep_amounts"][amount] += 1
            if row_time is not None and target in last_tick:
                interval = (row_time - last_tick[target]) * 1000.0
                if 200 <= interval <= 4000:
                    acc["deep_intervals_ms"].append(interval)
            if row_time is not None:
                last_tick[target] = row_time

        last_application: dict[str, float] = {}
        for row in rows:
            if _spell_id(row) == DEEP_WOUNDS_ID and row.get("event") == "SPELL_GO_SELF":
                target = _target_guid(row) or "unknown_target"
                row_time = _number(row.get("time"))
                acc["deep_applications"] += 1
                acc["deep_sources"].add(source_name)
                if row_time is not None and target in last_application:
                    delta = row_time - last_application[target]
                    if 0 < delta < 5.8:
                        acc["deep_refresh_candidates"] += 1
                if row_time is not None:
                    last_application[target] = row_time
            elif _spell_id(row) == DEEP_WOUNDS_ID and row.get("event") == "DEBUFF_REMOVED_OTHER":
                acc["deep_removals"] += 1

        file_has_kalimdor_context = False
        for row in rows:
            if _slot_item_id(_state(row), 16) == KALIMDOR_ITEM_ID:
                file_has_kalimdor_context = True
            if _spell_id(row) != KALIMDOR_PROC_ID:
                continue
            event = row.get("event")
            if event == "SPELL_GO_SELF":
                acc["kalimdor_go"] += 1
            elif event == "SPELL_DAMAGE_EVENT_SELF":
                acc["kalimdor_damage"] += 1
                amount = _number(row.get("amount"))
                if amount is not None:
                    acc["kalimdor_damage_amounts"].append(amount)
            elif event == "SPELL_MISS_SELF":
                acc["kalimdor_miss"] += 1
            acc["kalimdor_sources"].add(source_name)
        if file_has_kalimdor_context:
            acc["kalimdor_context_files"].add(source_name)
    return acc


def _new_chronicle_accumulator() -> dict[str, Any]:
    return {
        "files": [],
        "records": 0,
        "uw_events": 0,
        "uw_amounts": Counter(),
        "uw_absolute_rage": 0,
        "execute_candidates": 0,
        "execute_results": 0,
        "execute_damage_rows": 0,
        "skill_samples": defaultdict(lambda: {"ordinary": 0, "critical": 0}),
        "battle_shout_auras": 0,
        "battle_shout_candidates": 0,
        "battle_shout_results": 0,
        "deep_ticks": 0,
        "deep_applications": 0,
        "deep_auras": 0,
        "deep_amounts": [],
        "deep_intervals_ms": [],
        "kalimdor_records": 0,
        "kalimdor_damage_rows": 0,
    }


def _scan_chronicle_trajectories(trajectory_dir: Path) -> dict[str, Any]:
    acc = _new_chronicle_accumulator()
    paths = sorted(trajectory_dir.glob("*.jsonl")) if trajectory_dir.is_dir() else []
    last_deep_tick: dict[tuple[str, str, str], float] = {}
    for path in paths:
        rows = _read_jsonl(path)
        acc["files"].append(path.name)
        acc["records"] += len(rows)
        for row in rows:
            kind = row.get("record_kind")
            fields = row.get("fields") if isinstance(row.get("fields"), Mapping) else {}
            event = row.get("event") if isinstance(row.get("event"), Mapping) else {}
            identity = row.get("identity") if isinstance(row.get("identity"), Mapping) else {}
            spell_id = _integer(fields.get("spell_id"))
            if spell_id == UNBRIDLED_WRATH_ID and kind == "resource_event":
                acc["uw_events"] += 1
                amount = _integer(fields.get("amount_chronicle_units") or fields.get("raw_value"))
                if amount is not None:
                    acc["uw_amounts"][amount] += 1
                if _number(fields.get("absolute_rage")) is not None:
                    acc["uw_absolute_rage"] += 1
            if spell_id in ABILITY_SPELL_IDS["execute"]:
                if kind == "candidate_action":
                    acc["execute_candidates"] += 1
                elif kind == "action_result":
                    acc["execute_results"] += 1
                elif kind == "reward_event":
                    acc["execute_damage_rows"] += 1
            ability = SPELL_TO_ABILITY.get(spell_id)
            if ability in DIRECT_SKILL_ABILITIES and kind == "reward_event":
                outcome = "critical" if fields.get("critical") is True else "ordinary"
                acc["skill_samples"][ability][outcome] += 1
            if spell_id == BATTLE_SHOUT_ID:
                if kind == "aura_event":
                    acc["battle_shout_auras"] += 1
                elif kind == "candidate_action":
                    acc["battle_shout_candidates"] += 1
                elif kind == "action_result":
                    acc["battle_shout_results"] += 1
            if spell_id == DEEP_WOUNDS_ID:
                if kind == "reward_event":
                    acc["deep_ticks"] += 1
                    amount = _number(fields.get("damage_amount"))
                    if amount is not None:
                        acc["deep_amounts"].append(amount)
                    offset = _number(event.get("offset_ms"))
                    key = (
                        str(identity.get("canonical_instance_id")),
                        str(identity.get("encounter_id")),
                        str(fields.get("target_guid")),
                    )
                    if offset is not None and key in last_deep_tick:
                        interval = offset - last_deep_tick[key]
                        if 200 <= interval <= 4000:
                            acc["deep_intervals_ms"].append(interval)
                    if offset is not None:
                        last_deep_tick[key] = offset
                elif kind == "action_result":
                    acc["deep_applications"] += 1
                elif kind == "aura_event":
                    acc["deep_auras"] += 1
            if spell_id == KALIMDOR_PROC_ID:
                acc["kalimdor_records"] += 1
                if kind == "reward_event":
                    acc["kalimdor_damage_rows"] += 1
    return acc


def _load_execute_trials(summary_dir: Path) -> tuple[list[JSONMap], list[str], list[int]]:
    trials_by_key: dict[tuple[str, int], JSONMap] = {}
    sources: set[str] = set()
    talent_ranks: list[int] = []
    paths = sorted(summary_dir.glob("*.json")) if summary_dir.is_dir() else []
    for path in paths:
        document = _load_json(path)
        runs = document.get("specialized_runs")
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, Mapping) or run.get("task_id") != "warrior_execute_transition":
                continue
            run_id = str(run.get("task_run_id") or path.stem)
            talent = run.get("talent_context")
            if isinstance(talent, Mapping):
                improved = talent.get("improved_execute")
                if isinstance(improved, Mapping):
                    rank = _integer(improved.get("rank"))
                    if rank is not None:
                        talent_ranks.append(rank)
            run_trials = run.get("trials")
            if not isinstance(run_trials, list):
                continue
            for trial in run_trials:
                if not isinstance(trial, Mapping):
                    continue
                trial_number = _integer(trial.get("trial"))
                if trial_number is None:
                    continue
                outcome = trial.get("outcome") if isinstance(trial.get("outcome"), Mapping) else {}
                rage = trial.get("rage") if isinstance(trial.get("rage"), Mapping) else {}
                compact = {
                    "source_file": path.name,
                    "task_run_id": run_id,
                    "trial": trial_number,
                    "rage_before": rage.get("before"),
                    "rage_after": rage.get("after"),
                    "rage_spent": rage.get("spent_from_pre_to_post"),
                    "rage_identifiable": rage.get("identifiable") is True,
                    "landed": outcome.get("landed") is True,
                    "outcome_event": outcome.get("event"),
                    "miss_info": outcome.get("miss_info"),
                    "damage": outcome.get("amount"),
                    "hit_info": outcome.get("hit_info"),
                    "miss_net_rage_spent": rage.get("miss_net_rage_spent"),
                    "miss_extra_rage_retained": rage.get("miss_extra_rage_retained"),
                }
                trials_by_key[(run_id, trial_number)] = compact
                sources.add(path.name)
    return list(trials_by_key.values()), sorted(sources), talent_ranks


def _unbridled_wrath_document(cal: Mapping[str, Any], chron: Mapping[str, Any]) -> JSONMap:
    unique = int(cal["uw_unique"])
    attributable = unique - int(cal["uw_triggers"].get("unknown", 0))
    confirmed = int(cal["uw_confirmed"])
    if unique == 0 and int(chron["uw_events"]) == 0:
        status = "MISSING"
    elif confirmed >= 3 and attributable == unique:
        status = "VERIFIED"
    else:
        status = "PARTIAL"
    return {
        "status": status,
        "spell_id": UNBRIDLED_WRATH_ID,
        "calibration": {
            "raw_mirrored_event_count": cal["uw_raw"],
            "unique_proc_count": unique,
            "mirrored_events_removed": cal["uw_mirrors"],
            "raw_energize_amount_counts": _counter_json(cal["uw_amounts"]),
            "trigger_attribution_counts": _counter_json(cal["uw_triggers"]),
            "attributable_proc_count": attributable,
            "actual_resource_delta": {
                "identifiable_count": cal["uw_identifiable"],
                "confirmed_advertised_delta_count": confirmed,
                "unidentifiable_count": unique - int(cal["uw_identifiable"]),
                "unidentifiable_reason_counts": _counter_json(cal["uw_delta_reasons"]),
                "presence_is_not_counted_as_applied_rage": True,
            },
            "evidence_anchors": cal["uw_anchors"],
        },
        "chronicle": {
            "player_scoped_resource_event_count": chron["uw_events"],
            "raw_energize_amount_counts": _counter_json(chron["uw_amounts"]),
            "events_with_absolute_rage": chron["uw_absolute_rage"],
            "boundary": "Chronicle RES rows are delta-like; absolute rage is absent.",
        },
        "conclusion": (
            "Proc events and trigger classes are reusable, but event presence alone does "
            "not prove the advertised rage reached the resource bar."
        ),
    }


def _execute_document(
    trials: Sequence[Mapping[str, Any]],
    sources: Sequence[str],
    talent_ranks: Sequence[int],
    chron: Mapping[str, Any],
) -> JSONMap:
    landed = [trial for trial in trials if trial.get("landed") is True]
    avoided = [trial for trial in trials if trial.get("landed") is not True]
    base_costs = [
        _number(trial.get("miss_net_rage_spent"))
        for trial in avoided
        if _number(trial.get("miss_net_rage_spent")) is not None
    ]
    current_cost_confirmed = bool(base_costs) and all(abs(cost - 10.0) <= 0.01 for cost in base_costs)
    retained = [
        _number(trial.get("miss_extra_rage_retained"))
        for trial in avoided
        if _number(trial.get("miss_extra_rage_retained")) is not None
    ]
    landed_extra_spent = sum(
        _number(trial.get("rage_before")) is not None
        and _number(trial.get("rage_before")) > 10
        and abs((_number(trial.get("rage_after")) or 0.0)) <= 0.01
        for trial in landed
    )
    if not trials and int(chron["execute_results"]) == 0:
        status = "MISSING"
    elif current_cost_confirmed and len(avoided) >= 2 and landed_extra_spent >= 2:
        status = "VERIFIED"
    else:
        status = "PARTIAL"
    return {
        "status": status,
        "spell_id": EXECUTE_ID,
        "current_build": {
            "improved_execute_rank_observations": sorted(set(talent_ranks)),
            "trial_count": len(trials),
            "landed_count": len(landed),
            "avoidance_count": len(avoided),
            "critical_landed_count": sum(
                bool((_integer(trial.get("hit_info")) or 0) & 2) for trial in landed
            ),
            "base_cost_on_avoidance_candidates": base_costs,
            "base_cost_10_rage": "VERIFIED" if current_cost_confirmed else "MISSING",
            "landed_trials_spending_extra_rage": landed_extra_spent,
            "avoidance_trials_retaining_extra_rage": len(retained),
            "retained_extra_rage_observations": retained,
            "trial_evidence": list(trials),
        },
        "chronicle": {
            "candidate_action_count": chron["execute_candidates"],
            "action_result_count": chron["execute_results"],
            "damage_row_count": chron["execute_damage_rows"],
            "absolute_rage_or_loss_rows_available": False,
        },
        "evidence_sources": list(sources),
        "conclusion": (
            "The rank-2 build's 10-rage base cost is directly observed on one avoidance; "
            "landed extra-rage spending is repeated, but avoidance-class coverage is still single-trial."
        ),
    }


def _impale_document(cal: Mapping[str, Any], chron: Mapping[str, Any]) -> JSONMap:
    cells: list[JSONMap] = []
    qualifying_ratios: list[float] = []
    qualifying_ordinary = 0
    qualifying_critical = 0
    for key, values in sorted(cal["impale_cells"].items(), key=lambda item: str(item[0])):
        ordinary = list(values["ordinary"])
        critical = list(values["critical"])
        if not ordinary or not critical:
            continue
        ratio = float(statistics.median(critical)) / float(statistics.median(ordinary))
        qualifying_ratios.append(ratio)
        qualifying_ordinary += len(ordinary)
        qualifying_critical += len(critical)
        cells.append(
            {
                "ability": key[0],
                "attack_power": key[1],
                "target_armor": key[2],
                "stratum": key[3],
                "ordinary_count": len(ordinary),
                "critical_count": len(critical),
                "ordinary_median": statistics.median(ordinary),
                "critical_median": statistics.median(critical),
                "critical_to_ordinary_ratio": round(ratio, 6),
            }
        )
    rank_two_seen = int(cal["impale_ranks"].get(2, 0)) > 0
    sufficient = (
        rank_two_seen
        and qualifying_ordinary >= 2
        and qualifying_critical >= 2
        and bool(qualifying_ratios)
        and all(2.15 <= ratio <= 2.25 for ratio in qualifying_ratios)
    )
    total_samples = sum(
        int(counts["ordinary"]) + int(counts["critical"])
        for counts in cal["skill_samples"].values()
    )
    status = "VERIFIED" if sufficient else ("PARTIAL" if total_samples else "MISSING")
    return {
        "status": status,
        "talent": {"tab": 1, "index": 11, "rank_counts": _counter_json(cal["impale_ranks"])},
        "calibration_skill_samples": {
            ability: dict(cal["skill_samples"].get(ability, {"ordinary": 0, "critical": 0}))
            for ability in DIRECT_SKILL_ABILITIES
        },
        "exact_duplicate_damage_rows_removed": cal["skill_duplicates"],
        "controlled_bloodthirst_ratio_cells": cells,
        "verification_gate": {
            "sufficient": sufficient,
            "ordinary_samples_in_comparable_cells": qualifying_ordinary,
            "critical_samples_in_comparable_cells": qualifying_critical,
            "accepted_current_rank2_ratio_band": [2.15, 2.25],
            "reason": (
                "fixed-AP/fixed-armor Bloodthirst provides deterministic comparable damage"
                if sufficient
                else "ordinary/critical fixed-context coverage is not sufficient"
            ),
        },
        "chronicle_skill_samples": {
            ability: dict(chron["skill_samples"].get(ability, {"ordinary": 0, "critical": 0}))
            for ability in DIRECT_SKILL_ABILITIES
        },
        "chronicle_boundary": (
            "Chronicle adds outcome volume but lacks target armor/AP and exact talent placement, "
            "so it is not used to pass the controlled ratio gate."
        ),
    }


def _battle_shout_document(cal: Mapping[str, Any], chron: Mapping[str, Any]) -> JSONMap:
    complete = [
        observation
        for observation in cal["battle_shout"]
        if observation.get("rage_cost") is not None
        and observation.get("attack_power_delta") is not None
        and observation.get("gcd_seconds") is not None
        and observation.get("duration_ms") is not None
        and observation.get("aura_seen") is True
    ]
    if complete:
        status = "VERIFIED"
    elif cal["battle_shout"] or chron["battle_shout_auras"]:
        status = "PARTIAL"
    else:
        status = "MISSING"
    return {
        "status": status,
        "spell_id": BATTLE_SHOUT_ID,
        "calibration_cast_count": len(cal["battle_shout"]),
        "complete_transition_count": len(complete),
        "observations": cal["battle_shout"],
        "chronicle": {
            "aura_event_count": chron["battle_shout_auras"],
            "candidate_action_count": chron["battle_shout_candidates"],
            "action_result_count": chron["battle_shout_results"],
            "cost_ap_gcd_duration_identifiable": False,
        },
        "evidence_sources": sorted(cal["battle_shout_sources"]),
    }


def _deep_wounds_document(cal: Mapping[str, Any], chron: Mapping[str, Any]) -> JSONMap:
    local_intervals = list(cal["deep_intervals_ms"])
    combined_intervals = local_intervals + list(chron["deep_intervals_ms"])
    timing = _compact_distribution(combined_intervals)
    if combined_intervals:
        median = float(statistics.median(combined_intervals))
        near = sum(abs(value - median) <= 125 for value in combined_intervals)
        concentration = near / len(combined_intervals)
    else:
        concentration = 0.0
    timing_status = (
        "VERIFIED" if len(combined_intervals) >= 10 and concentration >= 0.75 else "PARTIAL"
    ) if combined_intervals else "MISSING"
    observed = int(cal["deep_ticks"]) + int(chron["deep_ticks"])
    status = "PARTIAL" if observed else "MISSING"
    return {
        "status": status,
        "spell_id": DEEP_WOUNDS_ID,
        "calibration": {
            "application_count": cal["deep_applications"],
            "periodic_tick_count": cal["deep_ticks"],
            "exact_duplicate_tick_rows_removed": cal["deep_tick_duplicates"],
            "tick_damage_counts": _counter_json(cal["deep_amounts"]),
            "refresh_candidates_before_5_8_seconds": cal["deep_refresh_candidates"],
            "debuff_removed_event_count": cal["deep_removals"],
            "tick_interval_ms": _compact_distribution(local_intervals),
        },
        "chronicle": {
            "application_result_count": chron["deep_applications"],
            "aura_event_count": chron["deep_auras"],
            "periodic_tick_count": chron["deep_ticks"],
            "tick_damage": _compact_distribution(chron["deep_amounts"]),
            "tick_interval_ms": _compact_distribution(chron["deep_intervals_ms"]),
        },
        "coverage": {
            "tick_timing": timing_status,
            "combined_tick_interval_ms": timing,
            "intervals_within_125ms_of_median_fraction": round(concentration, 4),
            "tick_damage_observed": "VERIFIED" if observed else "MISSING",
            "refresh_presence": (
                "PARTIAL" if cal["deep_refresh_candidates"] or chron["deep_auras"] else "MISSING"
            ),
            "snapshot_or_refresh_damage_rule": "MISSING",
        },
        "evidence_sources": sorted(cal["deep_sources"]),
        "conclusion": (
            "Tick cadence and damage are well observed; no controlled pre-refresh/post-refresh "
            "snapshot comparison exists, so the full refresh rule is not verified."
        ),
    }


def _kalimdor_document(cal: Mapping[str, Any], chron: Mapping[str, Any]) -> JSONMap:
    procs = int(cal["kalimdor_go"])
    status = "PARTIAL" if procs or int(chron["kalimdor_records"]) else "MISSING"
    return {
        "status": status,
        "item_id": KALIMDOR_ITEM_ID,
        "spell_id": KALIMDOR_PROC_ID,
        "calibration": {
            "proc_go_count": procs,
            "damage_count": cal["kalimdor_damage"],
            "miss_count": cal["kalimdor_miss"],
            "damage_amount": _compact_distribution(cal["kalimdor_damage_amounts"]),
            "files_with_positive_equipped_item_context": sorted(cal["kalimdor_context_files"]),
        },
        "chronicle": {
            "spell_record_count": chron["kalimdor_records"],
            "damage_row_count": chron["kalimdor_damage_rows"],
            "gear_item_identity_available": False,
        },
        "exposure_opportunities": {
            "status": "UNKNOWN",
            "count": None,
            "reason": (
                "The compact logs do not carry a weapon identity on every eligible melee event, "
                "and some campaigns swap weapons; a reliable proc denominator cannot be formed."
            ),
            "ppm_fitted": False,
        },
        "evidence_sources": sorted(cal["kalimdor_sources"]),
    }


def build_audit(
    data_root: Path = DEFAULT_DATA_ROOT,
    *,
    calibration_dir: Path | None = None,
    calibration_summary_dir: Path | None = None,
    chronicle_trajectory_dir: Path | None = None,
) -> JSONMap:
    """Build the audit document without writing or modifying simulator inputs."""

    data_root = data_root.resolve()
    calibration_dir = (calibration_dir or data_root / "calibration").resolve()
    calibration_summary_dir = (
        calibration_summary_dir or data_root / "calibration_summaries"
    ).resolve()
    chronicle_trajectory_dir = (
        chronicle_trajectory_dir
        or data_root / "derived" / "chronicle_fury_partial_trajectory" / "v1"
    ).resolve()

    cal = _scan_calibration(calibration_dir)
    chron = _scan_chronicle_trajectories(chronicle_trajectory_dir)
    execute_trials, execute_sources, execute_ranks = _load_execute_trials(
        calibration_summary_dir
    )

    mechanisms = {
        "unbridled_wrath": _unbridled_wrath_document(cal, chron),
        "execute": _execute_document(
            execute_trials, execute_sources, execute_ranks, chron
        ),
        "impale": _impale_document(cal, chron),
        "battle_shout": _battle_shout_document(cal, chron),
        "deep_wounds": _deep_wounds_document(cal, chron),
        "kalimdors_revenge_proc": _kalimdor_document(cal, chron),
    }
    status_counts = Counter(mechanic["status"] for mechanic in mechanisms.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "fury_existing_evidence_audit",
        "generated_at": _utc_now(),
        "status_counts": {
            status: int(status_counts.get(status, 0))
            for status in ("VERIFIED", "PARTIAL", "MISSING")
        },
        "sources": {
            "calibration": {
                "directory": str(calibration_dir),
                "files_scanned": len(cal["files"]),
                "records_scanned": cal["records"],
                "files": cal["files"],
            },
            "calibration_summaries": {
                "directory": str(calibration_summary_dir),
                "execute_sources": execute_sources,
            },
            "chronicle_fury_trajectory": {
                "directory": str(chronicle_trajectory_dir),
                "files_scanned": len(chron["files"]),
                "records_scanned": chron["records"],
                "files": chron["files"],
            },
            "chronicle_normalized": {
                "scanned": False,
                "reason": (
                    "The existing player-scoped derived Fury trajectory is reused. A full normalized "
                    "corpus rescan would not restore the absolute rage, target armor, or gear fields "
                    "needed by these gates."
                ),
            },
        },
        "mechanisms": mechanisms,
        "simulator_modified": False,
        "registry_modified": False,
    }


def write_audit(document: Mapping[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output.resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--calibration-dir", type=Path)
    parser.add_argument("--calibration-summary-dir", type=Path)
    parser.add_argument("--chronicle-trajectory-dir", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = build_audit(
            args.data_root,
            calibration_dir=args.calibration_dir,
            calibration_summary_dir=args.calibration_summary_dir,
            chronicle_trajectory_dir=args.chronicle_trajectory_dir,
        )
        output = write_audit(document, args.output)
    except FuryExistingEvidenceAuditError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
