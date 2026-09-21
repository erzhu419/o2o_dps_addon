"""One-seed native d900 wiring smoke against the frozen v6 Cat receipt.

This command only checks replay identity and one legal current-prefix branch.
It cannot authorize a policy-value or real-raid comparison.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.causal_action_program_v1 import ImportedReactiveProgramBindingV1
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.development_d900_cat_relative_v1 import (
    D900CatRelativeCandidateV1,
    enumerate_d900_cat_relative_action_plans_v1,
    replay_d900_cat_relative_v1,
)
from o2o_dps.responsive_incantagos_driven_bridge_v1 import IncantagosDevelopmentDrivenBridgeV1
from o2o_dps.responsive_team_frozen_validation_source_v1 import verify_frozen_validation_source_v1
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import CatDecisionPointV8
from o2o_dps.upper_kara_imported_incumbent_program_v1 import OBSERVATION_CONTRACT_ID_V1
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from scripts.development_responsive_upper_kara_trash_smoke_v1 import (
    INSTANCE_ID, build_case,
)


def _selected_receipts(outcome):
    return [
        row for row in outcome.receipts
        if row.get("kind") == "IMPORTED_REACTIVE_INCUMBENT_SELECTED"
    ]


def _first_decisions(outcome):
    return [
        {
            "time_ms": row.get("state_time_ms"),
            "gcd_action": row.get("gcd_action"),
            "queue_op": row.get("queue_op"),
            "wait_ms": row.get("wait_ms"),
        }
        for row in _selected_receipts(outcome)[:6]
    ]


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
    names = tuple(exact_build["equipped_item_names"])
    cat = build_incantagos_v4_cat_binding_v1(
        fixed, case, metadata,
        equipment_request=case.request,
        equipped_item_names=names,
        classification_hypotheses_by_occurrence_id={
            row.occurrence_id: "elite" for row in fixed.occurrence_index_registry
        },
        source_metadata_artifact_sha256=args.metadata.stem,
        equipment_request_provenance=exact_build,
    )
    points: list[CatDecisionPointV8] = []

    def recorded_cat_session():
        native_cat = cat.open_session()

        def decide(observation, available):
            decision = native_cat(observation, available)
            point = CatDecisionPointV8(len(points), observation, available, decision)
            if not points:
                points.append(point)
            return decision

        return decide

    def cat_binding(open_session):
        return ImportedReactiveProgramBindingV1(
            cat.receipt["source_policy_id"], cat.receipt["source_policy_id"],
            OBSERVATION_CONTRACT_ID_V1, open_session,
        )

    def run_lane(binding, candidate=None):
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
        opened = []
        try:
            outcome = replay_d900_cat_relative_v1(
                seed=args.simulator_seed,
                cat_binding=binding,
                candidate=candidate,
                driven_bridge_factory=lambda: IncantagosDevelopmentDrivenBridgeV1(
                    bridge=SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root),
                    case=case,
                    loaded_model=loaded,
                    teammate_seed=args.teammate_seed,
                ),
                case_factory=lambda _seed: case,
                observation_projector_factory=lambda current: build_incantagos_v4_prefix_projector_v1(
                    fixed, current
                ),
                max_decisions=args.max_decisions,
                opened_sessions=opened,
            )
        finally:
            loaded.model.close()
        return outcome, opened

    exact, _ = run_lane(cat_binding(recorded_cat_session))
    prior = json.loads(args.prior_v56.read_text(encoding="utf-8"))
    expected = prior["paired_replays"][0]
    exact_receipts = _selected_receipts(exact)
    identity_holds = (
        prior["attackability_mode"] == args.attackability_mode
        and prior["current_state_route_focus_applied"] is True
        and (expected["seed"], expected["teammate_seed"]) == (
            args.simulator_seed, args.teammate_seed
        )
        and expected["source_policy_id"] == cat.receipt["source_policy_id"]
        and exact.status.value == expected["status"] == "COMPLETE"
        and exact.elapsed_ms == expected["elapsed_ms"]
        and math.isclose(exact.effective_damage, expected["effective_damage"], abs_tol=1e-6)
        and len(exact_receipts) == expected["decision_count"]
        and _first_decisions(exact) == expected["first_decisions"]
    )
    summary = {
        "schema": "development_d900_cat_relative_native_smoke/v1",
        "status": "EXACT_CAT_MATCH" if identity_holds else "EXACT_CAT_MISMATCH",
        "comparison_authorized": False,
        "model_result_sha": args.result_sha256,
        "model_sha": args.model_sha256,
        "seed": args.simulator_seed,
        "teammate_seed": args.teammate_seed,
        "exact_cat": {
            "status": exact.status.value,
            "invalid_reason": exact.invalid_reason,
            "elapsed_ms": exact.elapsed_ms,
            "effective_damage": exact.effective_damage,
            "decision_count": len(exact_receipts),
            "first_decisions": _first_decisions(exact),
            "matches_frozen_v56": identity_holds,
        },
    }
    if identity_holds and points:
        point = points[0]
        options = enumerate_d900_cat_relative_action_plans_v1(point)
        replacement = next(
            (
                row for row in options
                if row.target_index is None and row.gcd_action is not None
                and row.gcd_action != point.cat_decision.gcd_action
            ),
            next((row for row in options if row.target_index is None), None),
        )
        if replacement is not None:
            candidate = D900CatRelativeCandidateV1(
                candidate_id="first-prefix-ready-legal-action",
                guard=ObservableCausalGuardV1(
                    live_target_count_gte=1,
                    attackable_target_count_gte=1,
                    action_ready=replacement.gcd_action,
                    false_semantics=SKIP_PLAN,
                ),
                replacement=replacement,
                cat_gcd_action_is=point.cat_decision.gcd_action,
            )
            explored, opened = run_lane(cat_binding(cat.open_session), candidate)
            summary["legal_candidate"] = {
                "candidate": candidate.to_dict(),
                "status": explored.status.value,
                "invalid_reason": explored.invalid_reason,
                "elapsed_ms": explored.elapsed_ms,
                "effective_damage": explored.effective_damage,
                "intervention_count": sum(len(row.interventions) for row in opened),
                "interventions": [event for row in opened for event in row.interventions],
            }
            if explored.status.value != "COMPLETE" or summary["legal_candidate"]["intervention_count"] != 1:
                summary["status"] = "LEGAL_CANDIDATE_WIRING_FAILED"
            else:
                summary["status"] = "COMPLETE_NATIVE_WIRING_SMOKE"
        else:
            summary["status"] = "NO_READY_CAT_RELATIVE_ALTERNATIVE_AT_FIRST_PREFIX"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "stage5", "exact-build", "frozen-dispatch", "runtime-store", "bridge", "simulator-root", "prior-v56", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("component-id", "stage5-content-sha256", "partition-compressed-file-sha256", "result-sha256", "model-sha256", "wave-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--simulator-seed", type=int, required=True)
    parser.add_argument("--teammate-seed", type=int, required=True)
    parser.add_argument("--max-decisions", type=int, default=1_000)
    parser.add_argument("--attackability-mode", required=True)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({
        "output": str(args.output),
        "status": result["status"],
        "exact_cat": {key: result["exact_cat"][key] for key in (
            "status", "elapsed_ms", "effective_damage", "decision_count", "matches_frozen_v56"
        )},
        "legal_candidate": {
            key: result["legal_candidate"][key] for key in (
                "status", "elapsed_ms", "effective_damage", "intervention_count"
            )
        } if "legal_candidate" in result else None,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
