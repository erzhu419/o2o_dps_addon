"""Small client cast-to-GO diagnostic from the existing Shadow checkpoint journal.

The two events are client observations, not a synchronized server clock.  A
repeated cast before a GO is ambiguous and is not converted into a latency.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from .brainofcat_shadow_checkpoint_v1 import import_jsonl


SCHEMA = "shadow_trace_diagnostic/v1"
WATCHED_SPELLS = {
    1680: "WHIRLWIND", 23894: "BLOODTHIRST", 25286: "HEROIC_STRIKE",
    1464: "SLAM", 8820: "SLAM", 11604: "SLAM", 11605: "SLAM", 45961: "SLAM",
}


def summarize_shadow_trace_v1(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    pending: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    checkpoint_count = 0
    partial_target_health_count = 0
    counts: dict[int, dict[str, Any]] = {
        spell: {"spell_id": spell, "name": name, "cast_time_included": name == "SLAM",
                "client_casts": 0,
                "observed_go": 0, "ambiguous_go": 0, "unpaired_go": 0,
                "paired_cast_to_go_ms": []}
        for spell, name in WATCHED_SPELLS.items()
    }
    for record in records:
        if record.get("kind") == "checkpoint":
            checkpoint_count += 1
            fields = record.get("fields")
            if isinstance(fields, Mapping) and all(
                isinstance(fields.get(name), Mapping)
                and fields[name].get("quality") == "PARTIAL"
                for name in ("target.current_health", "target.max_health")
            ):
                partial_target_health_count += 1
        if record.get("kind") != "event_delta":
            continue
        event = record.get("event")
        stamp = record.get("at")
        if not isinstance(event, Mapping) or not isinstance(stamp, Mapping):
            continue
        spell = event.get("spellId")
        time_s = stamp.get("getTimeSeconds")
        if spell not in counts or type(time_s) not in (int, float):
            continue
        key = (str(record.get("sessionId")), str(record.get("pullId")), spell)
        row = counts[spell]
        name = event.get("name")
        if name == "SPELL_CAST_EVENT":
            row["client_casts"] += 1
            pending[key].append(float(time_s))
        elif name == "SPELL_FAILED_SELF":
            pending[key].clear()
        elif name == "SPELL_GO_SELF" and event.get("sourceGuid") == record.get("playerGuid"):
            row["observed_go"] += 1
            casts = pending[key]
            if len(casts) == 1 and 0 <= float(time_s) - casts[0] <= 5:
                row["paired_cast_to_go_ms"].append(round((float(time_s) - casts[0]) * 1000))
            elif casts:
                row["ambiguous_go"] += 1
            else:
                row["unpaired_go"] += 1
            casts.clear()
    for row in counts.values():
        samples = row["paired_cast_to_go_ms"]
        row["median_cast_to_go_ms"] = median(samples) if samples else None
    return {
        "schema": SCHEMA,
        "scope": "CLIENT_OBSERVED_TIMING_DIAGNOSTIC_NOT_SERVER_LATENCY",
        "checkpoint_count": checkpoint_count,
        "partial_target_health_checkpoint_count": partial_target_health_count,
        "spells": list(counts.values()),
        "calibration_priority": "CLIENT_ACTION_TO_GO_TIMING" if any(
            row["paired_cast_to_go_ms"] for row in counts.values()
        ) else "NO_UNAMBIGUOUS_TIMING_PAIRS",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-jsonl", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = summarize_shadow_trace_v1(import_jsonl(args.checkpoint_jsonl)["records"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
