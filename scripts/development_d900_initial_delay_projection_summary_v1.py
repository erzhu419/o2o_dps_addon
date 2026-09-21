"""Compact one-seed d900 projection replay without transferring the full trace."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys


def summarize(path: Path, first_wake_path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    first_wake = json.loads(first_wake_path.read_text(encoding="utf-8"))
    module = Path(__file__).resolve().parents[1] / "o2o_dps/chronicle_external_teammate_response_model_v1.py"
    (replay,) = result["paired_replays"]
    drive = replay["responsive_drive"]
    diagnostic = drive["target_event_diagnostic"]
    white = [
        row for row in diagnostic["groups"]
        if row["event_type"] == "DMG" and row["spell_id"] == 6603
    ]
    early = [row for row in white if row["time_bucket"] == "through_9098ms"]
    return {
        "trained_statistical_model_content_sha256": first_wake["model_sha"],
        "formal_training_result_content_sha256": first_wake["model_result_sha"],
        "development_runtime_source_module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "status": replay["status"],
        "effective_damage": replay["effective_damage"],
        "first_decisions": replay["first_decisions"],
        "source_policy_id": replay["source_policy_id"],
        "seed": replay["seed"],
        "teammate_seed": replay["teammate_seed"],
        "elapsed_ms": replay["elapsed_ms"],
        "responsive_event_count": drive["responsive_event_count"],
        "white_total_events": sum(row["event_count"] for row in white),
        "white_total_hits": sum(row["applied_hit_count"] for row in white),
        "white_early_by_target": {
            str(index): {
                "events": sum(row["event_count"] for row in early if row["target_index"] == index),
                "hits": sum(row["applied_hit_count"] for row in early if row["target_index"] == index),
                "applied_damage": round(sum(row["applied_damage"] for row in early if row["target_index"] == index), 2),
            }
            for index in range(3)
        },
        "first_observed_dead_ms": replay["first_observed_dead_ms"],
        "target_timeline_snapshots": replay["target_timeline_snapshots"],
    }


if __name__ == "__main__":
    print(json.dumps(summarize(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, sort_keys=True))
