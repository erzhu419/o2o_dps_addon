"""Score d900 heldout choices with one TRAIN worker in memory, not a reducer model."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc
from o2o_dps import chronicle_external_teammate_response_model_v1 as response
from o2o_dps.upper_kara_target_head_heldout_eval_v1 import evaluate_heldout_stage5_gzip_v1


PILOT_INSTANCE = "9a61ace6-0292-4a97-bafa-76cead47cf38"
D900_INSTANCE = "d900a97b-b53e-4444-943b-3e0f2be8d477"
D900_COMPONENT = "114175c6c5af0b03978551442d51dcbbf325f8ba2af8b7e3bef468bc7da8fa68"
EXPECTED_WHITE_TEAMMATE_DENOMINATORS = {"FIRST_ACQUISITION": 247, "RETARGET": 425}


def _compact_view(view: dict) -> dict:
    return {
        "white_head_status": view["white_head_status"],
        "start": {
            phase: {
                key: view["metrics"][phase][key]
                for key in (
                    "eligible_choices", "uniform_expected_accuracy",
                    "learned_expected_accuracy", "learned_top1_accuracy",
                    "learned_supported_choices", "learned_fallback_choices",
                )
            } for phase in ("FIRST_ACQUISITION", "RETARGET")
        },
        "white_6603": {
            phase: {
                key: view["white_metrics"][phase][key]
                for key in (
                    "eligible_choices", "uniform_expected_accuracy",
                    "learned_expected_accuracy", "learned_top1_accuracy",
                    "learned_supported_choices", "learned_fallback_choices",
                )
            } for phase in ("FIRST_ACQUISITION", "RETARGET")
        },
    }


def run(worker_path: Path, dispatch_path: Path, stage5_path: Path) -> dict:
    with gzip.open(worker_path, "rt", encoding="utf-8") as handle:
        worker = json.load(handle)
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    if worker.get("revision") != hpc.REVISION or worker.get("split") != "TRAIN":
        raise ValueError("worker is not a current TRAIN pilot")
    if worker.get("instance_id") != PILOT_INSTANCE or worker.get("node") != "node006":
        raise ValueError("representative pilot worker identity differs")
    if worker.get("dispatch_content_sha256") != dispatch["content_address"]["sha256"]:
        raise ValueError("worker/dispatch identity differs")
    tasks = {task["instance_id"]: task for task in dispatch["tasks"]}
    if tasks[PILOT_INSTANCE]["split"] != "TRAIN" or tasks[D900_INSTANCE]["split"] != "VALIDATION":
        raise ValueError("pilot/d900 split differs")
    if tasks[D900_INSTANCE]["component_id"] != D900_COMPONENT:
        raise ValueError("d900 validation component differs")
    joint = hpc.deserialize_joint_training_v4(worker["joint_dynamic_training_counts"])
    model = hpc.materialize_joint_variant_v4(joint, response.ABLATION_D)
    evaluated = evaluate_heldout_stage5_gzip_v1(stage5_path, model=model)
    teammate = evaluated["cohort_teammates_only"]["white_metrics"]
    for phase, expected in EXPECTED_WHITE_TEAMMATE_DENOMINATORS.items():
        if teammate[phase]["eligible_choices"] != expected:
            raise ValueError(f"heldout white {phase} denominator differs from v58")
    views = {
        name: _compact_view(evaluated[name])
        for name in ("cohort", "cohort_teammates_only", "selected_d900", "selected_d900_teammates_only")
    }
    return {
        "schema": "development_d900_v7_single_train_worker_pilot_eval/v1",
        "status": "SINGLE_TRAIN_PARTITION_PILOT_NOT_FORMAL_MODEL",
        "pilot_worker": {
            "instance_id": worker["instance_id"], "node": worker["node"],
            "split": worker["split"],
            "compiled_row_count": worker["single_scan_receipt"]["compiled_exact_player_row_count"],
            "joint_row_count": joint.row_count,
        },
        "heldout": {
            "instance_id": D900_INSTANCE,
            "split": tasks[D900_INSTANCE]["split"],
            "component_id": D900_COMPONENT,
            "cohort_wave_count": evaluated["cohort_wave_count"],
            "teammate_white_denominator_matches_v58": True,
        },
        "views": views,
        "boundary": (
            "Only one real TRAIN worker was deserialized and materialized in memory; "
            "no 68-worker reducer, formal D-store, heldout source gate, DPS evaluation, "
            "or model adoption is asserted"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--dispatch", type=Path, required=True)
    parser.add_argument("--stage5", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.worker, args.dispatch, args.stage5), ensure_ascii=False))


if __name__ == "__main__":
    main()
