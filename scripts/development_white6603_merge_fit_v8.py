"""Merge only the frozen 52 FIT shard counts; leave selection untouched."""

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

from o2o_dps.development_white6603_joint_training_v8 import JointWhiteTrainingV8
from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc
from o2o_dps.development_white6603_selection_eval_v8 import (
    FIT_SCORING_SCHEMA, load_fit_scoring_v8, project_fit_scoring_v8,
)


def _fit_ids(split: dict) -> list[str]:
    fit_ids = sorted(split["fit_instance_ids"])
    if len(fit_ids) != 52 or set(fit_ids) & set(split["selection_instance_ids"]):
        raise ValueError("frozen 52 FIT identifiers differ")
    return fit_ids


def _passed_summary(run_root: Path, instance_id: str) -> dict:
    path = run_root / "fit" / f"{instance_id}.summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    if summary["status"] != "PASS" or summary["instance_id"] != instance_id:
        raise ValueError(f"FIT shard not passed: {instance_id}")
    return summary


def project_fit_shard(run_root: Path, split: dict, instance_id: str) -> dict:
    """Extract only the scoring tables from one passed FIT shard."""

    if instance_id not in _fit_ids(split):
        raise ValueError("projection task is not a frozen FIT raid")
    summary = _passed_summary(run_root, instance_id)
    source = run_root / "fit" / f"{instance_id}.joint-white.json.gz"
    with gzip.open(source, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    if (
        document["row_count"] != summary["compiled_row_count"]
        or document["white_count"] != summary["v8_direct_white6603_mark_count"]
    ):
        raise ValueError(f"FIT shard count differs: {instance_id}")
    projection = project_fit_scoring_v8({**document, "fit_instance_ids": [instance_id]})
    # The compact projection itself is sufficient to check both global totals.
    load_fit_scoring_v8(projection)
    output = run_root / "fit" / f"{instance_id}.score-projection.json"
    output.write_text(json.dumps(projection, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {
        "status": "PASS", "instance_id": instance_id,
        "row_count": projection["row_count"],
        "white_count": projection["white_count"],
        "output": str(output),
    }


def merge_fit_projections(run_root: Path, split: dict) -> dict:
    """Merge 52 compact scoring projections, never target/damage/delay tables."""

    fit_ids = _fit_ids(split)
    merged_shared: dict[tuple, Counter] = {}
    merged_specific: dict[tuple, Counter] = {}
    merged_white: dict[tuple, Counter] = {}
    minimums = None
    row_count = white_count = 0
    for instance_id in fit_ids:
        summary = _passed_summary(run_root, instance_id)
        source = run_root / "fit" / f"{instance_id}.score-projection.json"
        projection = json.loads(source.read_text(encoding="utf-8"))
        if (
            projection["schema"] != FIT_SCORING_SCHEMA
            or projection["fit_instance_ids"] != [instance_id]
            or projection["row_count"] != summary["compiled_row_count"]
            or projection["white_count"] != summary["v8_direct_white6603_mark_count"]
        ):
            raise ValueError(f"FIT scoring projection differs: {instance_id}")
        if minimums is None:
            minimums = projection["minimums"]
        elif minimums != projection["minimums"]:
            raise ValueError("FIT scoring projection backoff minimums differ")
        for source_key, destination in (
            ("v7_shared_mark_counts", merged_shared),
            ("v7_d_specific_mark_counts", merged_specific),
        ):
            table = hpc._load_counter_table(projection[source_key], source_key)
            for context, counts in table.items():
                destination.setdefault(context, Counter()).update(counts)
        for row in projection["v8_white_counts"]:
            merged_white.setdefault(tuple(row["context"]), Counter()).update(
                {True: row["white"], False: row["nonwhite"]}
            )
        row_count += projection["row_count"]
        white_count += projection["white_count"]
    white_rows = [
        {"context": list(context), "white": counts[True], "nonwhite": counts[False]}
        for context, counts in merged_white.items()
    ]
    white_rows.sort(key=lambda row: json.dumps(row["context"], separators=(",", ":")))
    projection = {
        "schema": FIT_SCORING_SCHEMA,
        "fit_instance_ids": fit_ids,
        "row_count": row_count,
        "white_count": white_count,
        "minimums": minimums,
        "v7_shared_mark_counts": hpc._serialize_counter_table(merged_shared),
        "v7_d_specific_mark_counts": hpc._serialize_counter_table(merged_specific),
        "v8_white_counts": white_rows,
    }
    load_fit_scoring_v8(projection)
    output = run_root / "fit-only.score-projection.json"
    output.write_text(json.dumps(projection, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {
        "schema": "development_white6603_v8_fit_score_projection_receipt/v1",
        "status": "PASS", "fit_task_count": len(fit_ids),
        "row_count": row_count, "white_count": white_count,
        "mark_context_count": len(merged_shared) + len(merged_specific),
        "white_context_count": len(merged_white), "output": str(output),
    }


def merge_fit(run_root: Path, split: dict) -> dict:
    fit_ids = _fit_ids(split)
    merged = JointWhiteTrainingV8()
    for instance_id in fit_ids:
        prefix = run_root / "fit" / instance_id
        summary = json.loads(prefix.with_suffix(".summary.json").read_text(encoding="utf-8"))
        if summary["status"] != "PASS" or summary["instance_id"] != instance_id:
            raise ValueError(f"FIT shard not passed: {instance_id}")
        with gzip.open(prefix.with_suffix(".joint-white.json.gz"), "rt", encoding="utf-8") as handle:
            shard = JointWhiteTrainingV8.deserialize(json.load(handle))
        if (
            shard.row_count != summary["compiled_row_count"]
            or shard.white_count != summary["v8_direct_white6603_mark_count"]
        ):
            raise ValueError(f"FIT shard count differs: {instance_id}")
        merged.joint.merge(shard.joint)
        merged.wave_count += shard.wave_count
        for context, counts in shard.white_counts.items():
            merged.white_counts.setdefault(context, Counter()).update(counts)
    document = merged.serialize()
    document["fit_instance_ids"] = fit_ids
    return document


def write_fit_merge(run_root: Path, document: dict) -> dict:
    output = run_root / "fit-only.joint-white.json.gz"
    with gzip.open(output, "wt", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
    result = {
        "schema": "development_white6603_v8_fit_only_merge_receipt/v1",
        "status": "PASS",
        "fit_task_count": len(document["fit_instance_ids"]),
        "wave_count": document["wave_count"],
        "row_count": document["row_count"],
        "white_count": document["white_count"],
        "context_count": len(document["white_opportunity_counts"]),
        "output": str(output),
    }
    receipt = run_root / "fit-only-merge.summary.json"
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--mode", choices=("full-merge", "project-shard", "merge-projections"), default="full-merge")
    parser.add_argument("--instance-id")
    args = parser.parse_args()
    split = json.loads(args.split.read_text(encoding="utf-8"))
    if args.mode == "project-shard":
        if args.instance_id is None:
            parser.error("project-shard requires --instance-id")
        result = project_fit_shard(args.run_root, split, args.instance_id)
    elif args.mode == "merge-projections":
        result = merge_fit_projections(args.run_root, split)
    else:
        document = merge_fit(args.run_root, split)
        result = write_fit_merge(args.run_root, document)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
