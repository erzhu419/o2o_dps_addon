"""Check a real v7 TRAIN worker's white-6603 head in a temporary SQLite store.

This is deliberately not a formal reducer result or loader provenance.  The
temporary store is deleted on exit and cannot be used for evaluation.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import random
import tempfile

from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc
from o2o_dps import chronicle_external_teammate_response_model_v1 as response
from o2o_dps.responsive_team_bridge_adapter_v1 import (
    LoadedResponsiveTeammateModelV1,
    TeammateModelProvenanceV1,
)
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import (
    build_responsive_team_runtime_store_v1,
    open_responsive_team_runtime_store_v1,
)


def _example(model: response.HierarchicalMarkedSemiMarkovV1) -> tuple[dict, dict, dict]:
    actor = {"player_guid": "0x00000000000000AA", "class": "Warrior", "spec_key": "Fury"}
    for last in (None,):
        for first_a, first_b in ((0, 0), (0, 100), (100, 0)):
            for actions_a in (0, 1, 2):
                for actions_b in (0, 1, 2):
                    for damage_a, damage_b in ((0, 0), (100, 0), (0, 100), (100, 200)):
                        candidates = [
                            {"target_guid": "0xA", "first_activity_ms": first_a,
                             "recent_direct_start_count_3000ms": actions_a,
                             "recent_damage_amount_3000ms": damage_a},
                            {"target_guid": "0xB", "first_activity_ms": first_b,
                             "recent_direct_start_count_3000ms": actions_b,
                             "recent_damage_amount_3000ms": damage_b},
                        ]
                        state = {
                            "actor_last_mark_token": None,
                            "actor_last_target_guid": last,
                            "marked_activity": {"other_team_including_unattributed": {
                                "action_event_count_3000ms": 0,
                                "damage_amount_3000ms": 0,
                            }},
                            "target_state": {"alive_target_count": 2,
                                             "target_choice_candidates": candidates},
                        }
                        outcome = model.sample_target_choice(
                            actor=actor, emission_state=state,
                            target_mode="SWITCH_ALIVE", intent_kind="WHITE6603",
                            eligible_target_guids=("0xA", "0xB"),
                            current_target_guid=None, rng=random.Random(7),
                        )
                        if outcome is not None:
                            return actor, state, dict(outcome)
    raise RuntimeError("real pilot WHITE6603 head has no selectable two-target example")


def smoke(worker_path: Path, dispatch_path: Path) -> dict:
    with gzip.open(worker_path, "rt", encoding="utf-8") as handle:
        worker = json.load(handle)
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    hpc._verify_content_address(worker, "pilot worker")
    if worker["revision"] != hpc.REVISION or worker["split"] != "TRAIN":
        raise RuntimeError("pilot worker is not current v7 TRAIN")
    if worker["dispatch_content_sha256"] != dispatch["content_address"]["sha256"]:
        raise RuntimeError("pilot worker and dispatch differ")
    joint = hpc.deserialize_joint_training_v4(worker["joint_dynamic_training_counts"])
    model = hpc.materialize_joint_variant_v4(joint, response.ABLATION_D)
    serialized = hpc.serialize_model_v1(model)
    model_sha = hpc._canonical_sha256(serialized)
    model.model_content_sha256 = model_sha
    actor, state, in_memory = _example(model)
    stage5_sha = dispatch["source_bindings"]["stage5"]["content_sha256"]
    validation = dispatch["split_contract"]["validation_component_ids"]
    d900_component = "114175c6c5af0b03978551442d51dcbbf325f8ba2af8b7e3bef468bc7da8fa68"
    if d900_component not in validation:
        raise RuntimeError("d900 validation component is absent")
    # Test-only provenance is intentionally constructed from a single real worker.
    # It never passes through the formal result loader and never leaves this scope.
    test_source_sha = worker["content_address"]["sha256"]
    loaded = LoadedResponsiveTeammateModelV1(
        model=model,
        provenance=TeammateModelProvenanceV1(
            source_artifact_schema=hpc.RESULT_SCHEMA,
            source_artifact_content_sha256=test_source_sha,
            model_content_sha256=model_sha,
            variant_id=response.ABLATION_D,
            training_scope="SOURCE_BOUND_STAGE5_TRAIN_COMPONENTS_DEVELOPMENT_ONLY",
            current_source_held_out=True,
        ),
        result_content_sha256=test_source_sha,
        current_source_evidence={
            "result_source_stage5_content_sha256": stage5_sha,
            "current_source_stage5_content_sha256": stage5_sha,
            "current_source_component_id": d900_component,
            "validation_component_ids": validation,
            "current_source_held_out": True,
        },
    )
    with tempfile.TemporaryDirectory(prefix="boc-v7-white6603-pilot-only-") as directory:
        store_path = Path(directory) / "pilot-only.sqlite3"
        manifest = build_responsive_team_runtime_store_v1(store_path, loaded)
        size_bytes = store_path.stat().st_size
        opened = open_responsive_team_runtime_store_v1(
            store_path,
            expected_result_content_sha256=test_source_sha,
            expected_model_content_sha256=model_sha,
            variant_id=response.ABLATION_D,
            current_source=CurrentSourceDeclarationV1(
                stage5_content_sha256=stage5_sha,
                component_id=d900_component,
                declared_held_out=True,
            ),
        )
        try:
            from_sqlite = opened.model.sample_target_choice(
                actor=actor, emission_state=state,
                target_mode="SWITCH_ALIVE", intent_kind="WHITE6603",
                eligible_target_guids=("0xA", "0xB"),
                current_target_guid=None, rng=random.Random(7),
            )
            if from_sqlite != in_memory:
                raise RuntimeError("pilot D target-choice changed across SQLite")
        finally:
            opened.model.close()
    return {
        "status": "V7_WHITE6603_PILOT_ONLY_TEMP_STORE_DELETED_NOT_FORMAL_REDUCE",
        "worker_instance_id": worker["instance_id"],
        "worker_revision": worker["revision"],
        "worker_row_count": joint.row_count,
        "model_variant": response.ABLATION_D,
        "model_content_sha256": model_sha,
        "store_revision": manifest["revision"],
        "temporary_store_size_bytes": size_bytes,
        "target_choice_distribution_count": manifest["head_counts"]["target_choice_counts"]["distribution_count"],
        "in_memory_and_sqlite_sample": in_memory,
        "pilot_intent_kind": "WHITE6603",
        "formal_result_loader_used": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--dispatch", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.worker, args.dispatch), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
