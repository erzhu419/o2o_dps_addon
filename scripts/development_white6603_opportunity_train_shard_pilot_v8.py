"""Read one frozen TRAIN partition; emit only a compact v8 white-mark audit."""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_teammate_response_model_v1 import (
    iter_wave_response_sufficient_rows_v1,
)
from o2o_dps.development_white6603_joint_training_v8 import JointWhiteTrainingV8
from o2o_dps.development_white6603_opportunity_v8 import White6603OpportunityClockV8


def _formal_counts(path: Path) -> tuple[int, int]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        worker = json.load(handle)
    tables = worker["joint_dynamic_training_counts"]["base_c"]["tables"]
    white = sum(
        choice["count"]
        for row in tables["mark_counts"]
        if row["key"] == ["GLOBAL"]
        for choice in row["counts"]
        if json.loads(choice["value"]) == ["DMG", 6603, "DIRECT_FRIENDLY_PLAYER"]
    )
    return white, worker["single_scan_receipt"]["compiled_exact_player_row_count"]


def run(args: argparse.Namespace) -> dict:
    dispatch = json.loads(args.dispatch.read_text(encoding="utf-8"))
    (task,) = [
        task for task in dispatch["tasks"]
        if task["instance_id"] == args.instance_id
    ]
    if task["split"] != "TRAIN":
        raise ValueError("white opportunity pilot must use a frozen TRAIN task")
    partition = args.data_root / task["partition_locator"]
    phase_labels: Counter[tuple[str, bool]] = Counter()
    class_white: Counter[str] = Counter()
    prior_start_white: Counter[bool] = Counter()
    context_keys: set[tuple] = set()
    time_mismatches = 0
    prefix_clock_mismatches = 0
    direct_6603_without_hostile_lane = 0
    direct_white_target_lanes: Counter[str] = Counter()
    row_count = 0
    wave_count = 0
    trainer = JointWhiteTrainingV8()
    with gzip.open(partition, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            wave_count += 1
            trainer.begin_wave()
            clock = White6603OpportunityClockV8()
            trace = record["exact_trace"]
            start_offset = record["descriptive_outcome"]["reconstruction_binding"]["window"]["start_offset_ms"]
            for row in iter_wave_response_sufficient_rows_v1(record):
                observation = trainer.update(row)
                clock_observation = clock.observe(row)
                if observation != clock_observation:
                    prefix_clock_mismatches += 1
                label = row["label"]
                anchor = trace[row["trace_index"]]["anchor"]
                if observation["elapsed_ms"] != max(0, anchor["offset_ms"] - start_offset):
                    time_mismatches += 1
                direct_6603 = (
                    label["event_type"] == "DMG"
                    and label["spell_id"] == 6603
                    and label["attribution_kind"] == "DIRECT_FRIENDLY_PLAYER"
                )
                direct_6603_without_hostile_lane += direct_6603 and label["target_lane"] != "HOSTILE_CREATURE"
                if direct_6603:
                    direct_white_target_lanes[label["target_lane"]] += 1
                phase_labels[(observation["phase"], observation["white6603"])] += 1
                if observation["white6603"]:
                    class_white[row["actor"]["class"]] += 1
                    prior_start_white[observation["prior_direct_hostile_start"]] += 1
                context_keys.update(observation["contexts"])
                row_count += 1
    training = trainer.serialize()
    args.training_output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.training_output, "wt", encoding="utf-8") as handle:
        json.dump(training, handle, ensure_ascii=False, separators=(",", ":"))
    with gzip.open(args.training_output, "rt", encoding="utf-8") as handle:
        restored = JointWhiteTrainingV8.deserialize(json.load(handle))
    roundtrip_pass = (
        restored.row_count == row_count
        and restored.white_count == trainer.white_count
        and restored.serialize() == training
    )
    formal_white, formal_rows = _formal_counts(args.formal_worker)
    white_count = sum(count for (phase, label), count in phase_labels.items() if label)
    result = {
        "schema": "development_white6603_opportunity_train_shard_pilot/v8",
        "status": "PASS" if (
            time_mismatches == 0
            and prefix_clock_mismatches == 0
            and white_count == formal_white
            and row_count == formal_rows
            and roundtrip_pass
        ) else "DIAGNOSTIC_MISMATCH",
        "instance_id": args.instance_id,
        "split": task["split"],
        "component_id": task["component_id"],
        "wave_count": wave_count,
        "compiled_row_count": row_count,
        "formal_v7_compiled_row_count": formal_rows,
        "time_semantics_mismatch_count": time_mismatches,
        "prefix_clock_mismatch_count": prefix_clock_mismatches,
        "formal_v7_global_direct_white6603_count": formal_white,
        "v8_direct_white6603_mark_count": white_count,
        "direct_white6603_without_hostile_lane_count": direct_6603_without_hostile_lane,
        "direct_white6603_by_target_lane": dict(sorted(direct_white_target_lanes.items())),
        "phase_labels": {
            f"{phase}_{'WHITE' if label else 'NONWHITE'}": count
            for (phase, label), count in sorted(phase_labels.items())
        },
        "white_by_class": dict(sorted(class_white.items())),
        "white_by_prior_direct_hostile_start": {
            str(key).lower(): value for key, value in sorted(prior_start_white.items())
        },
        "unique_context_key_count": len(context_keys),
        "joint_white_training_roundtrip_pass": roundtrip_pass,
        "joint_white_training_context_count": len(restored.white_counts),
        "joint_white_training_output": str(args.training_output),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--formal-worker", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--training-output", type=Path, required=True)
    result = run(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
