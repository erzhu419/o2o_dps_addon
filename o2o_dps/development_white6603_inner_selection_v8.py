"""Freeze an instance-block inner selection within the existing TRAIN component.

This selection is for white-6603 model/smoothing choices, not an independent
generalization estimate: the existing 65 TRAIN raids share one player/guild
connected component.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_inner_selection_v8(dispatch: dict[str, Any], source_dispatch: str) -> dict[str, Any]:
    tasks = dispatch["tasks"]
    train = sorted((task for task in tasks if task["split"] == "TRAIN"), key=lambda task: task["instance_id"])
    validation = sorted((task for task in tasks if task["split"] == "VALIDATION"), key=lambda task: task["instance_id"])
    if len(train) != 65 or len(validation) != 3 or len(tasks) != 68:
        raise ValueError("expected frozen 65 TRAIN + 3 VALIDATION raid tasks")
    instance_ids = [task["instance_id"] for task in tasks]
    if len(set(instance_ids)) != len(instance_ids):
        raise ValueError("duplicate raid instance in frozen dispatch")
    train_components = {task["component_id"] for task in train}
    validation_components = {task["component_id"] for task in validation}
    if len(train_components) != 1 or train_components & validation_components:
        raise ValueError("frozen component boundary changed")

    # UUID order and every fifth complete raid were fixed before reading labels.
    selection = train[4::5]
    selection_ids = {task["instance_id"] for task in selection}
    fit = [task for task in train if task["instance_id"] not in selection_ids]
    return {
        "schema": "development_white6603_v8_inner_selection/v1",
        "status": "INTRA_COMPONENT_SELECTION_NOT_INDEPENDENT",
        "source_dispatch": source_dispatch,
        "unit": "complete raid instance task",
        "rule": "sort TRAIN instance_id ascending; 1-based ranks divisible by 5 enter selection",
        "allowed_use": "white6603 v8 model and smoothing choice within TRAIN only",
        "train_component_id": next(iter(train_components)),
        "fit_component_id": next(iter(train_components)),
        "selection_component_id": next(iter(train_components)),
        "train_task_count": len(train),
        "fit_task_count": len(fit),
        "selection_task_count": len(selection),
        "fit_instance_ids": [task["instance_id"] for task in fit],
        "selection_instance_ids": [task["instance_id"] for task in selection],
        "external_validation_tasks": [
            {"instance_id": task["instance_id"], "component_id": task["component_id"]}
            for task in validation
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dispatch = json.loads(args.dispatch.read_text(encoding="utf-8"))
    result = build_inner_selection_v8(dispatch, str(args.dispatch))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "train_task_count", "fit_task_count", "selection_task_count")}))


if __name__ == "__main__":
    main()
