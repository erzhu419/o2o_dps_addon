"""Summarize direct teammate white-swing opportunities in one d900 Stage5 wave."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path


WAVE = "02829cd0-85c3-4b6f-adba-059398e6ae14:external-v2-wave:1"
FOCAL = "0x0000000000576754"
TARGETS = {
    "0xF13000F240276CB6", "0xF13000F244276CB4", "0xF13000F245276CB3",
}
CUTOFF_MS = 9098
FOCUS_ACTORS = (
    "0x0000000000757D23", "0x000000000078D2A6", "0x00000000005F5FFA",
)


def analyze(record: dict) -> dict:
    if record.get("wave", {}).get("wave_id") != WAVE:
        raise ValueError("selected wave differs")
    events: dict[str, list[dict]] = defaultdict(list)
    field_examples: dict[str, object] = {}
    first_start_ms: dict[str, int] = {}
    for row in record["exact_trace"]:
        if row.get("trace_kind") != "EXACT_PLAYER_EVENT":
            continue
        actor = row.get("player_guid")
        event = row.get("event")
        anchor = row.get("anchor")
        if not isinstance(actor, str) or actor == FOCAL or not isinstance(event, dict) or not isinstance(anchor, dict):
            continue
        time_ms = anchor.get("offset_ms")
        if type(time_ms) is not int or not 0 <= time_ms <= CUTOFF_MS:
            continue
        if actor in FOCUS_ACTORS and event.get("event_type") == "START":
            first_start_ms[actor] = min(time_ms, first_start_ms.get(actor, time_ms))
        spell = event.get("spell")
        attribution = event.get("attribution")
        source = event.get("source")
        if (not isinstance(spell, dict) or spell.get("id") != 6603
                or not isinstance(attribution, dict)
                or attribution.get("attribution_kind") != "DIRECT_FRIENDLY_PLAYER"
                or attribution.get("player_guid") != actor
                or not isinstance(source, dict) or source.get("lane") != "FRIENDLY_PLAYER"):
            continue
        target = event.get("target")
        guid = target.get("guid") if isinstance(target, dict) else None
        damage = event.get("damage")
        amount = damage.get("amount") if isinstance(damage, dict) else None
        positive = event.get("event_type") == "DMG" and type(amount) is int and amount > 0
        events[actor].append({
            "time_ms": time_ms, "event_type": event.get("event_type"),
            "target_guid": guid, "positive_damage": positive,
            "in_three_target_registry": guid in TARGETS,
            "hit_type": damage.get("hit_type") if isinstance(damage, dict) else None,
        })
        if not field_examples:
            field_examples = {
                "event_keys": sorted(event),
                "source_keys": sorted(source),
                "damage_keys": sorted(damage) if isinstance(damage, dict) else [],
            }

    by_type = Counter()
    by_target = Counter()
    by_hit_type = Counter()
    per_actor = []
    positive_in_registry = 0
    positive_all = 0
    nonpositive_in_registry = 0
    for actor, rows in sorted(events.items()):
        rows.sort(key=lambda item: item["time_ms"])
        by_type.update(str(row["event_type"]) for row in rows)
        by_target.update(str(row["target_guid"]) for row in rows)
        by_hit_type.update(str(row["hit_type"]) for row in rows)
        positive_all += sum(row["positive_damage"] for row in rows)
        nonpositive_in_registry += sum(
            row["in_three_target_registry"] and not row["positive_damage"]
            for row in rows
        )
        positive_times = [
            row["time_ms"] for row in rows
            if row["positive_damage"] and row["in_three_target_registry"]
        ]
        positive_in_registry += len(positive_times)
        per_actor.append({
            "actor_guid": actor,
            "all_6603_event_count": len(rows),
            "all_6603_times_ms": [row["time_ms"] for row in rows],
            "positive_three_target_count": len(positive_times),
            "positive_three_target_times_ms": positive_times,
            "positive_intervals_ms": [b - a for a, b in zip(positive_times, positive_times[1:])],
        })
    focus_roster = {}
    for row in record["players"]:
        player = row["player"]
        actor = player.get("guid")
        if actor not in FOCUS_ACTORS:
            continue
        lane = row.get("warrior_spec_lane") or {}
        focus_roster[actor] = {
            "class": player.get("class"),
            "spec_key": lane.get("partition_key") if player.get("class") == "WARRIOR" else None,
            "first_start_ms": first_start_ms.get(actor),
            "exact_trace_event_count": len(row.get("exact_trace_indices", ())),
        }
    return {
        "schema": "development_d900_white6603_timing_audit/v1",
        "wave_id": WAVE, "cutoff_ms_inclusive": CUTOFF_MS,
        "direct_teammate_actor_count": len(per_actor),
        "all_6603_event_count": sum(by_type.values()),
        "event_type_counts": dict(sorted(by_type.items())),
        "target_guid_counts": dict(sorted(by_target.items())),
        "hit_type_counts": dict(sorted(by_hit_type.items())),
        "positive_all_target_count": positive_all,
        "positive_three_target_count": positive_in_registry,
        "nonpositive_three_target_count": nonpositive_in_registry,
        "field_examples": field_examples,
        "per_actor": per_actor,
        "focus_actor_roster": focus_roster,
        "scope": "observed Stage5 direct teammate events; no hand assignment inferred",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    args = parser.parse_args()
    with gzip.open(args.stage5, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("wave", {}).get("wave_id") == WAVE:
                print(json.dumps(analyze(record), ensure_ascii=False, sort_keys=True))
                return
    raise ValueError("exact wave not found")


if __name__ == "__main__":
    main()
