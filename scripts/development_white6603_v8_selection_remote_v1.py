"""Score 13 frozen TRAIN selection raids against fit-only v7/v8 mark heads.

Run only on the remote shared filesystem after the 52-raid fit merge exists.
Reads original Stage-5 selection partitions one wave at a time; never merges
selection counts into the fit model or downloads partitions to Windows.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import sys
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc
from o2o_dps import chronicle_external_teammate_response_model_v1 as response
from o2o_dps.development_white6603_inner_selection_v8 import build_inner_selection_v8
from o2o_dps.development_white6603_joint_training_v8 import JointWhiteTrainingV8
from o2o_dps.development_white6603_selection_eval_v8 import (
    evaluate_selection_v8, evaluate_selection_partials_v8,
    load_fit_scoring_v8, score_opportunity_v8, score_selection_partial_v8,
)
from o2o_dps.development_white6603_runtime_head_artifact_v8 import (
    build_selected_white6603_head_artifact_v8,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _frozen_selection(split_path: Path, dispatch_path: Path) -> tuple[dict, list[dict]]:
    split = _load_json(split_path)
    dispatch = _load_json(dispatch_path)
    if split != build_inner_selection_v8(dispatch, split["source_dispatch"]):
        raise ValueError("frozen 52/13 raid membership differs from dispatch")
    selection_ids = set(split["selection_instance_ids"])
    tasks = sorted(
        (task for task in dispatch["tasks"] if task["instance_id"] in selection_ids),
        key=lambda task: task["instance_id"],
    )
    if len(tasks) != 13 or any(task["split"] != "TRAIN" for task in tasks):
        raise ValueError("selection tasks differ from frozen TRAIN split")
    return split, tasks


def _selection_rows(
    *, tasks: list[dict[str, Any]], data_root: Path, v7_fit_model: Any,
    v8_fit_white_counts: dict,
) -> Iterator[dict[str, Any]]:
    for task in tasks:
        instance_id = task["instance_id"]
        partition = data_root / task["partition_locator"]
        with gzip.open(partition, "rt", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                wave = record["wave"]
                if wave["instance_id"] != instance_id:
                    raise ValueError("Stage-5 partition contains a different raid")
                for row in response.iter_wave_response_sufficient_rows_v1(record):
                    yield score_opportunity_v8(
                        row=row, v7_fit_model=v7_fit_model,
                        v8_fit_white_counts=v8_fit_white_counts,
                        instance_id=instance_id, wave_id=wave["wave_id"],
                    )


def run(
    *, split_path: Path, dispatch_path: Path, fit_merged_path: Path,
    data_root: Path, output_path: Path, head_output_path: Path | None = None,
) -> dict[str, Any]:
    split, selection_tasks = _frozen_selection(split_path, dispatch_path)
    with gzip.open(fit_merged_path, "rt", encoding="utf-8") as handle:
        fit_document = json.load(handle)
    if sorted(fit_document["fit_instance_ids"]) != split["fit_instance_ids"]:
        raise ValueError("fit-only merged counts do not cover exactly the 52 fit raids")
    fit = JointWhiteTrainingV8.deserialize(fit_document)
    v7_fit_model = hpc._JointEvaluationModelViewV4(fit.joint, response.ABLATION_D)
    selection_ids = set(split["selection_instance_ids"])
    result = evaluate_selection_v8(
        _selection_rows(
            tasks=selection_tasks, data_root=data_root, v7_fit_model=v7_fit_model,
            v8_fit_white_counts=fit.white_counts,
        ),
        selection_ids,
    )
    result["fit_task_count"] = len(split["fit_instance_ids"])
    result["fit_count_rows"] = fit.row_count
    result["fit_count_white"] = fit.white_count
    result["fit_merged_source"] = str(fit_merged_path)
    result["source_dispatch"] = str(dispatch_path)
    head_document = None
    if (
        head_output_path is not None
        and result["status"] == "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE"
    ):
        head_document = build_selected_white6603_head_artifact_v8(
            fit_document, result
        )
    _write_json(output_path, result)
    if head_document is not None:
        head_output_path.parent.mkdir(parents=True, exist_ok=True)
        head_output_path.write_text(
            json.dumps(head_document, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return result


def run_shard(
    *, split_path: Path, dispatch_path: Path, fit_projection_path: Path,
    data_root: Path, output_path: Path, shard_index: int, shard_count: int,
) -> dict[str, Any]:
    """One worker loads the compact fit projection once and scores whole raids."""

    split, tasks = _frozen_selection(split_path, dispatch_path)
    if not (1 <= shard_count <= 13 and 0 <= shard_index < shard_count):
        raise ValueError("selection shard index/count differs")
    selected = [task for index, task in enumerate(tasks) if index % shard_count == shard_index]
    projection = _load_json(fit_projection_path)
    if sorted(projection["fit_instance_ids"]) != split["fit_instance_ids"]:
        raise ValueError("fit-only projection does not cover exactly the 52 fit raids")
    v7_fit_model, white_counts = load_fit_scoring_v8(projection)
    partial = score_selection_partial_v8(
        _selection_rows(
            tasks=selected, data_root=data_root, v7_fit_model=v7_fit_model,
            v8_fit_white_counts=white_counts,
        ),
        [task["instance_id"] for task in selected],
    )
    partial.update({
        "shard_index": shard_index,
        "shard_count": shard_count,
        "fit_instance_ids": split["fit_instance_ids"],
        "fit_count_rows": projection["row_count"],
        "fit_count_white": projection["white_count"],
        "source_dispatch": str(dispatch_path),
        "fit_projection_source": str(fit_projection_path),
    })
    _write_json(output_path, partial)
    return partial


def run_reduce(
    *, split_path: Path, dispatch_path: Path, fit_projection_path: Path,
    partial_paths: list[Path], output_path: Path, head_output_path: Path | None = None,
) -> dict[str, Any]:
    """Reduce disjoint shard receipts in frozen raid order, independent of finish order."""

    split, tasks = _frozen_selection(split_path, dispatch_path)
    projection = _load_json(fit_projection_path)
    if sorted(projection["fit_instance_ids"]) != split["fit_instance_ids"]:
        raise ValueError("fit-only projection does not cover exactly the 52 fit raids")
    partials = [_load_json(path) for path in partial_paths]
    shard_count = len(partials)
    if not (1 <= shard_count <= 13):
        raise ValueError("selection shard receipt count differs")
    indexes = set()
    for partial in partials:
        index = partial["shard_index"]
        if (
            partial["shard_count"] != shard_count
            or index in indexes or not (0 <= index < shard_count)
            or partial["fit_instance_ids"] != split["fit_instance_ids"]
            or partial["fit_count_rows"] != projection["row_count"]
            or partial["fit_count_white"] != projection["white_count"]
            or partial["source_dispatch"] != str(dispatch_path)
            or partial["fit_projection_source"] != str(fit_projection_path)
        ):
            raise ValueError("selection shard receipt source or partition differs")
        expected = {
            task["instance_id"] for position, task in enumerate(tasks)
            if position % shard_count == index
        }
        if set(partial["raids"]) != expected:
            raise ValueError("selection shard receipt raid assignment differs")
        indexes.add(index)
    if indexes != set(range(shard_count)):
        raise ValueError("selection shard receipt coverage differs")
    result = evaluate_selection_partials_v8(partials, split["selection_instance_ids"])
    result.update({
        "fit_task_count": len(split["fit_instance_ids"]),
        "fit_count_rows": projection["row_count"],
        "fit_count_white": projection["white_count"],
        "fit_projection_source": str(fit_projection_path),
        "source_dispatch": str(dispatch_path),
    })
    _write_json(output_path, result)
    if head_output_path is not None and result["status"] == "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE":
        fit_head_source = {
            "schema": "development_white6603_joint_training/v8",
            "fit_instance_ids": projection["fit_instance_ids"],
            "row_count": projection["row_count"],
            "white_count": projection["white_count"],
            "white_opportunity_counts": projection["v8_white_counts"],
        }
        _write_json(
            head_output_path,
            build_selected_white6603_head_artifact_v8(fit_head_source, result),
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("serial", "shard", "reduce"), default="serial")
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--dispatch", type=Path, required=True)
    parser.add_argument("--fit-merged", type=Path)
    parser.add_argument("--fit-projection", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head-output", type=Path)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int)
    parser.add_argument("--part", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.mode == "serial":
        if args.fit_merged is None or args.data_root is None:
            parser.error("serial requires --fit-merged and --data-root")
        result = run(
            split_path=args.split, dispatch_path=args.dispatch,
            fit_merged_path=args.fit_merged, data_root=args.data_root,
            output_path=args.output, head_output_path=args.head_output,
        )
    elif args.mode == "shard":
        if args.fit_projection is None or args.data_root is None or args.shard_index is None or args.shard_count is None:
            parser.error("shard requires --fit-projection, --data-root, --shard-index and --shard-count")
        result = run_shard(
            split_path=args.split, dispatch_path=args.dispatch,
            fit_projection_path=args.fit_projection, data_root=args.data_root,
            output_path=args.output, shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
    else:
        if args.fit_projection is None or not args.part:
            parser.error("reduce requires --fit-projection and all --part receipts")
        result = run_reduce(
            split_path=args.split, dispatch_path=args.dispatch,
            fit_projection_path=args.fit_projection, partial_paths=args.part,
            output_path=args.output, head_output_path=args.head_output,
        )
    if args.mode == "shard":
        receipt = {
            "shard_index": result["shard_index"],
            "shard_count": result["shard_count"],
            "raid_count": len(result["raids"]),
            "opportunity_count": sum(raid["opportunities"] for raid in result["raids"].values()),
        }
    else:
        receipt = {key: result[key] for key in (
            "status", "selected_candidate", "opportunity_count", "wave_count", "gates"
        )}
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
