"""Small descriptive target-order evidence from one exact Chronicle wave trace.

The trace records what players hit, not a raid leader's instructions or which
targets every melee player could reach.  No future trace field is policy input.
"""

from __future__ import annotations

from collections import defaultdict
import gzip
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "upper_kara_observed_route_evidence/v1"
DIRECT_PLAYER = "DIRECT_FRIENDLY_PLAYER"
HOSTILE = "HOSTILE_CREATURE"


def _guid(side: object) -> str | None:
    if isinstance(side, Mapping) and side.get("lane") == HOSTILE:
        value = side.get("guid")
        return value if isinstance(value, str) else None
    return None


def _positive_damage(event: Mapping[str, Any]) -> int:
    value = event.get("damage")
    amount = value.get("amount") if isinstance(value, Mapping) else None
    return amount if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0 else 0


def _direct_player(row: Mapping[str, Any], event: Mapping[str, Any]) -> str | None:
    attribution = event.get("attribution")
    source = event.get("source")
    player = row.get("player_guid")
    if (
        row.get("trace_kind") == "EXACT_PLAYER_EVENT"
        and isinstance(player, str)
        and isinstance(attribution, Mapping)
        and attribution.get("attribution_kind") == DIRECT_PLAYER
        and attribution.get("player_guid") == player
        and isinstance(source, Mapping)
        and source.get("lane") == "FRIENDLY_PLAYER"
    ):
        return player
    return None


def extract_observed_route_evidence_v1(
    record: Mapping[str, Any], *, target_guids: tuple[str, ...], focal_guid: str,
    bin_width_ms: int = 1_000,
) -> dict[str, Any]:
    """Summarize exact target damage/action timing without inferring permission."""
    if not target_guids or len(target_guids) != len(set(target_guids)):
        raise ValueError("target_guids must be nonempty and unique")
    if bin_width_ms <= 0:
        raise ValueError("bin_width_ms must be positive")
    wave = record.get("wave")
    trace = record.get("exact_trace")
    if not isinstance(wave, Mapping) or not isinstance(trace, list):
        raise ValueError("record needs wave and exact_trace")
    targets = {
        guid: {
            "first_direct_player_action": None,
            "first_direct_player_positive_damage": None,
            "first_positive_damage": None,
            "first_death": None,
            "positive_damage": 0,
            "direct_player_positive_damage": 0,
            "direct_player_guids": set(),
            "post_death_positive_damage": 0,
        }
        for guid in target_guids
    }
    bins: dict[int, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {"positive_damage": 0, "direct_player_positive_damage": 0, "direct_player_guids": set(), "focal_positive_damage": 0})
    )
    focal_event_target_changes: list[dict[str, Any]] = []
    focal_targeted_action_events: list[dict[str, Any]] = []
    focal_start_target_transitions: list[dict[str, Any]] = []
    focal_last_target: str | None = None
    focal_last_start_target: str | None = None
    end_ms = 0
    for row in trace:
        if not isinstance(row, Mapping):
            continue
        event = row.get("event")
        anchor = row.get("anchor")
        if not isinstance(event, Mapping) or not isinstance(anchor, Mapping):
            continue
        offset = anchor.get("offset_ms")
        if not isinstance(offset, int) or offset < 0:
            continue
        end_ms = max(end_ms, offset)
        target = _guid(event.get("target"))
        if target not in targets:
            continue
        event_type = event.get("event_type")
        actor = _direct_player(row, event)
        reference = {"offset_ms": offset, "trace_index": row.get("trace_index")}
        summary = targets[target]
        if event_type == "DEAD" and summary["first_death"] is None:
            summary["first_death"] = reference
        if actor and event_type in {"START", "GO", "FAIL"} and summary["first_direct_player_action"] is None:
            summary["first_direct_player_action"] = {**reference, "player_guid": actor, "event_type": event_type}
        amount = _positive_damage(event) if event_type == "DMG" else 0
        if amount and summary["first_death"] is not None:
            summary["post_death_positive_damage"] += amount
            continue
        if amount:
            if summary["first_positive_damage"] is None:
                summary["first_positive_damage"] = reference
            summary["positive_damage"] += amount
            bucket = bins[(offset // bin_width_ms) * bin_width_ms][target]
            bucket["positive_damage"] += amount
            if actor:
                summary["direct_player_positive_damage"] += amount
                summary["direct_player_guids"].add(actor)
                bucket["direct_player_positive_damage"] += amount
                bucket["direct_player_guids"].add(actor)
                if summary["first_direct_player_positive_damage"] is None:
                    summary["first_direct_player_positive_damage"] = {**reference, "player_guid": actor}
                if actor == focal_guid:
                    bucket["focal_positive_damage"] += amount
        if actor == focal_guid and event_type in {"START", "GO", "DMG"}:
            spell = event.get("spell")
            action = {
                **reference,
                "target_guid": target,
                "event_type": event_type,
                "spell": {"id": spell.get("id"), "name": spell.get("name")} if isinstance(spell, Mapping) else None,
            }
            if event_type in {"START", "GO"}:
                focal_targeted_action_events.append(action)
            if event_type == "START" and target != focal_last_start_target:
                focal_start_target_transitions.append(action)
                focal_last_start_target = target
            if target != focal_last_target:
                focal_event_target_changes.append(action)
                focal_last_target = target
    rendered_bins = []
    for start, target_bins in sorted(bins.items()):
        totals = {guid: int(target_bins[guid]["positive_damage"]) for guid in target_guids}
        largest = max(totals.values())
        leaders = [guid for guid, amount in totals.items() if amount == largest and amount > 0]
        rendered_bins.append({
            "start_ms": start,
            "end_ms_exclusive": start + bin_width_ms,
            "positive_damage_by_target": totals,
            "direct_player_damage_by_target": {guid: int(target_bins[guid]["direct_player_positive_damage"]) for guid in target_guids},
            "direct_player_count_by_target": {guid: len(target_bins[guid]["direct_player_guids"]) for guid in target_guids},
            "focal_positive_damage_by_target": {guid: int(target_bins[guid]["focal_positive_damage"]) for guid in target_guids},
            "largest_damage_target_guids": leaders,
            "more_than_one_target_damaged": sum(amount > 0 for amount in totals.values()) > 1,
        })
    return {
        "schema": SCHEMA,
        "status": "DESCRIPTIVE_TRACE_EVIDENCE_ONLY",
        "source": {key: wave.get(key) for key in ("instance_id", "encounter_id", "wave_id", "wave_ordinal")},
        "focal_player_guid": focal_guid,
        "target_guids": list(target_guids),
        "trace_rows": len(trace),
        "last_trace_offset_ms": end_ms,
        "targets": [
            {**{key: value for key, value in targets[guid].items() if key != "direct_player_guids"},
             "target_guid": guid,
             "direct_player_count": len(targets[guid]["direct_player_guids"])}
            for guid in target_guids
        ],
        "bin_width_ms": bin_width_ms,
        "damage_timeline": rendered_bins,
        "focal_event_target_changes": focal_event_target_changes,
        "focal_targeted_action_events": focal_targeted_action_events,
        "focal_start_target_transitions": focal_start_target_transitions,
        "interpretation": {
            "observed_damaged_target_order_only": True,
            "focal_event_target_changes_can_include_cleave_or_aoe_hits": True,
            "focal_targeted_action_events_can_include_passive_procs": True,
            "raid_leader_mandated_order_known": False,
            "melee_reachability_known": False,
            "future_trace_as_policy_observation": False,
        },
    }


def extract_selected_wave_gzip_v1(
    path: str | Path, *, instance_id: str, encounter_id: str, wave_id: str,
    target_guids: tuple[str, ...], focal_guid: str, bin_width_ms: int = 1_000,
) -> dict[str, Any]:
    """Stream to one selected JSONL record; do not copy or emit the raw trace."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            record = json.loads(line)
            wave = record.get("wave") if isinstance(record, Mapping) else None
            if not isinstance(wave, Mapping) or (
                wave.get("instance_id"), wave.get("encounter_id"), wave.get("wave_id")
            ) != (instance_id, encounter_id, wave_id):
                continue
            result = extract_observed_route_evidence_v1(
                record, target_guids=target_guids, focal_guid=focal_guid,
                bin_width_ms=bin_width_ms,
            )
            result["source"]["jsonl_line"] = line_number
            return result
    raise ValueError("selected exact wave not found")


__all__ = ("SCHEMA", "extract_observed_route_evidence_v1", "extract_selected_wave_gzip_v1")
