"""Write a frozen development-only d900 Cat-relative one-wave search plan.

This performs one exact Cat proposal-prefix replay, not the 512-seed search.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.causal_action_program_v1 import ImportedReactiveProgramBindingV1
from o2o_dps.development_d900_cat_relative_v1 import replay_d900_cat_relative_v1
from o2o_dps.development_d900_cat_relative_search_plan_v1 import (
    build_d900_cat_relative_search_plan_v1,
    freeze_d900_search_case_v1,
    record_d900_cat_prefix_v1,
)
from o2o_dps.responsive_incantagos_driven_bridge_v1 import IncantagosDevelopmentDrivenBridgeV1
from o2o_dps.responsive_team_frozen_validation_source_v1 import verify_frozen_validation_source_v1
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_imported_incumbent_program_v1 import OBSERVATION_CONTRACT_ID_V1
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from scripts.development_responsive_upper_kara_trash_smoke_v1 import INSTANCE_ID, build_case


def run(args: argparse.Namespace) -> dict:
    stage5_parts = args.stage5.parts
    locator = Path(*stage5_parts[stage5_parts.index("offline_data"):]).as_posix()
    verify_frozen_validation_source_v1(
        json.loads(args.frozen_dispatch.read_text(encoding="utf-8")),
        instance_id=INSTANCE_ID,
        component_id=args.component_id,
        partition_locator=locator,
        stage5_content_sha256=args.stage5_content_sha256,
        partition_compressed_file_sha256=args.partition_compressed_file_sha256,
    )
    fixed, case = build_case(args)
    exact_build = json.loads(args.exact_build.read_text(encoding="utf-8"))
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    cat = build_incantagos_v4_cat_binding_v1(
        fixed, case, metadata,
        equipment_request=case.request,
        equipped_item_names=tuple(exact_build["equipped_item_names"]),
        classification_hypotheses_by_occurrence_id={
            row.occurrence_id: "elite" for row in fixed.occurrence_index_registry
        },
        source_metadata_artifact_sha256=args.metadata.stem,
        equipment_request_provenance=exact_build,
    )
    source_policy_id = cat.receipt["source_policy_id"]
    cat_binding = ImportedReactiveProgramBindingV1(
        source_policy_id, source_policy_id, OBSERVATION_CONTRACT_ID_V1, cat.open_session
    )
    freeze = freeze_d900_search_case_v1(
        case, cat_binding,
        model_result_sha=args.result_sha256,
        model_sha=args.model_sha256,
        exact_build_source=exact_build["source_identity"],
    )
    points = []
    recorded_cat = record_d900_cat_prefix_v1(cat_binding, points)
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=args.result_sha256,
        expected_model_content_sha256=args.model_sha256,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=args.stage5_content_sha256,
            component_id=args.component_id,
            declared_held_out=True,
        ),
    )
    try:
        outcome = replay_d900_cat_relative_v1(
            seed=args.simulator_seed_start,
            cat_binding=recorded_cat,
            candidate=None,
            driven_bridge_factory=lambda: IncantagosDevelopmentDrivenBridgeV1(
                bridge=SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root),
                case=case,
                loaded_model=loaded,
                teammate_seed=args.teammate_seed_start,
            ),
            case_factory=lambda _seed: case,
            observation_projector_factory=lambda current: build_incantagos_v4_prefix_projector_v1(
                fixed, current
            ),
            max_decisions=args.max_decisions,
        )
    finally:
        loaded.model.close()
    if outcome.status.value != "COMPLETE":
        raise RuntimeError(f"proposal exact Cat replay failed: {outcome.invalid_reason}")
    plan = build_d900_cat_relative_search_plan_v1(
        proposal_points=points,
        freeze=freeze,
        simulator_seed_start=args.simulator_seed_start,
        teammate_seed_start=args.teammate_seed_start,
    )
    result = plan.to_dict()
    result["proposal_exact_cat"] = {
        "status": outcome.status.value,
        "effective_damage": outcome.effective_damage,
        "decision_count": len(points),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "stage5", "exact-build", "frozen-dispatch", "runtime-store",
                 "bridge", "simulator-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("component-id", "stage5-content-sha256", "partition-compressed-file-sha256",
                 "result-sha256", "model-sha256", "wave-id", "attackability-mode"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--simulator-seed-start", type=int, required=True)
    parser.add_argument("--teammate-seed-start", type=int, required=True)
    parser.add_argument("--max-decisions", type=int, default=1_000)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({
        "output": str(args.output),
        "status": result["status"],
        "nonzero_candidates": len(result["candidates"]),
        "proposal_exact_cat": result["proposal_exact_cat"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
