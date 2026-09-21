"""Exercise the real v4 Incantagos policy bridge with learned teammate wakes.

This is a bounded wire test of the development hypothesis, not a policy score.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.contra260817_fury_full_policy_rollout_v4 import (
    Contra260817SimulatorInputsV4,
)
from o2o_dps.contra260817_fury_full_policy_v3 import (
    SOURCE_DEFAULT_PROFILE_V3,
    Contra260817FuryFullPolicyAdapterV3,
)
from o2o_dps.contra_incantagos_v4_policy_binding_v1 import (
    bind_contra_incantagos_v4_policy_v1,
    propose_contra260817_incantagos_v4_v1,
    propose_deployed_contra_incantagos_v4_v1,
)
from o2o_dps.contra260817_incantagos_v4_reactive_session_v1 import (
    build_contra260817_incantagos_v4_imported_binding_v1,
)
from o2o_dps.deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from o2o_dps.deployed_contra_incantagos_v4_session_v1 import (
    build_deployed_contra_incantagos_v4_reactive_binding_v1,
)
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramOriginV1,
)
from o2o_dps.responsive_action_program_replay_v1 import (
    NativeDynamicV4ResponsiveActionProgramReplayV1,
)
from o2o_dps.responsive_incantagos_driven_bridge_v1 import (
    IncantagosDevelopmentDrivenBridgeV1,
)
from o2o_dps.responsive_team_frozen_validation_source_v1 import (
    verify_frozen_validation_source_v1,
)
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import (
    open_responsive_team_runtime_store_v1,
)
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
    _ContraControlsV1,
)
from scripts.development_responsive_incantagos_smoke_v1 import _build_case


def _proposal_summary(decision):
    return {
        "valid": decision.valid,
        "gcd": decision.gcd,
        "wait_ms": decision.wait_ms,
        "swing_queue": decision.swing_queue.value,
        "ordered_sink_channels": [row.channel for row in decision.raw_sink_order],
        "source_identity_verified": decision.metadata.get("source_identity_verified"),
        "comparison_ready": decision.metadata.get("comparison_ready"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "bridge", "simulator-root", "metadata", "stage5", "frozen-dispatch",
        "runtime-store",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in (
        "stage5-content-sha256", "partition-compressed-file-sha256",
        "component-id", "instance-id", "encounter-id", "wave-id",
        "focal-player-guid", "result-sha256", "model-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--simulator-seed", type=int, default=2026092001)
    parser.add_argument("--teammate-seed", type=int, default=2026092002)
    parser.add_argument("--stop-after-at-least-responsive-events", type=int, default=40)
    parser.add_argument("--replay-stop-ms", type=int, default=100)
    parser.add_argument("--deployed-runtime-binding", type=Path)
    args = parser.parse_args()

    stage5_parts = args.stage5.parts
    locator = Path(*stage5_parts[stage5_parts.index("offline_data"):]).as_posix()
    frozen = verify_frozen_validation_source_v1(
        json.loads(args.frozen_dispatch.read_text(encoding="utf-8")),
        instance_id=args.instance_id,
        component_id=args.component_id,
        partition_locator=locator,
        stage5_content_sha256=args.stage5_content_sha256,
        partition_compressed_file_sha256=args.partition_compressed_file_sha256,
    )
    fixed_case, case = _build_case(
        metadata_path=args.metadata,
        stage5_path=args.stage5,
        stage5_content_sha256=args.stage5_content_sha256,
        component_id=args.component_id,
        instance_id=args.instance_id,
        encounter_id=args.encounter_id,
        wave_id=args.wave_id,
        focal_player_guid=args.focal_player_guid,
        source_split="VALIDATION",
    )
    metadata_units = json.loads(args.metadata.read_text(encoding="utf-8"))["units"]
    missing_native_names = [
        guid for guid in case.native_target_guids
        if not metadata_units.get(guid, {}).get("name")
    ]
    controlled_names = json.loads(
        (ROOT / "configs/evaluation/incantagos_clean_dual_item_names_v1.json")
        .read_text(encoding="utf-8")
    )
    equipped_ids = tuple(
        item["id"] for item in case.request["raid"]["parties"][0]["players"][0]
        ["equipment"]["items"] if item.get("id")
    )
    controlled_player = case.request["raid"]["parties"][0]["players"][0]
    if equipped_ids != tuple(row["id"] for row in controlled_names["items"]):
        raise RuntimeError("controlled item-name input differs from simulator equipment")
    classification = {
        row.occurrence_id: (
            "worldboss" if row.target_index == fixed_case.boss_index else "elite"
        )
        for row in fixed_case.occurrence_index_registry
    }
    cat_binding = build_incantagos_v4_cat_binding_v1(
        fixed_case,
        case,
        json.loads(args.metadata.read_text(encoding="utf-8")),
        equipment_request=case.request,
        equipped_item_names=tuple(row["name"] for row in controlled_names["items"]),
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256=args.metadata.stem,
    )
    contra_binding = bind_contra_incantagos_v4_policy_v1(
        fixed_case,
        case,
        json.loads(args.metadata.read_text(encoding="utf-8")),
        equipment_request=case.request,
        equipped_item_names=tuple(row["name"] for row in controlled_names["items"]),
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256=args.metadata.stem,
    )
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
        raw = SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root)
        with IncantagosDevelopmentDrivenBridgeV1(
            bridge=raw,
            case=case,
            loaded_model=loaded,
            teammate_seed=args.teammate_seed,
        ) as driven:
            state = driven.load_dynamic_v4(
                case.request, args.simulator_seed, case.dynamic_config
            ).state
            actions = tuple(driven.actions())
            projected = build_incantagos_v4_prefix_projector_v1(
                fixed_case, case
            )(state, actions)
            cat_decision = cat_binding.open_session()(projected, actions)
            cat_action_label = next(
                (
                    row.label for row in actions
                    if row.action == cat_decision.gcd_action
                ),
                None,
            )
            cat_action_key = next(
                (
                    key for key, action_ref in ACTION_KEY_TO_REF.items()
                    if action_ref == cat_decision.gcd_action
                ),
                None,
            )
            contra_inputs = replace(
                Contra260817SimulatorInputsV4(),
                equipped_mainhand_name=controlled_names["items"][-2]["name"],
                equipped_offhand_name=controlled_names["items"][-1]["name"],
                target_bindings=contra_binding.exact_guid_target_bindings(),
            )
            contra_source = Contra260817FuryFullPolicyAdapterV3(
                SOURCE_DEFAULT_PROFILE_V3
            )
            if not contra_source.source_identity_verified:
                raise RuntimeError(
                    f"Contra260817 source identity: {contra_source.source_verification}"
                )
            contra_decision = propose_contra260817_incantagos_v4_v1(
                contra_binding,
                controls=_ContraControlsV1(
                    autoattack_active=False,
                    mainhand_name=contra_inputs.equipped_mainhand_name,
                    offhand_name=contra_inputs.equipped_offhand_name,
                ),
                inputs=contra_inputs,
                source=contra_source,
                state=state,
                available=actions,
                last_gcd_action="",
            )
            deployed_decision = None
            deployed_runtime_binding = None
            if args.deployed_runtime_binding is not None:
                deployed_runtime_binding = load_deployed_contra_runtime_binding_v1(
                    args.deployed_runtime_binding
                )
                deployed_decision = propose_deployed_contra_incantagos_v4_v1(
                    contra_binding,
                    runtime_binding=deployed_runtime_binding,
                    state=state,
                    available=actions,
                    last_gcd_action="",
                )
            candidate_action_count = 0
            if state["needs_input"] and not state["finished"]:
                attack = driven.start_attack()
                if not attack.accepted or attack.consumes_decision:
                    raise RuntimeError("candidate start_attack was not accepted")
                state = attack.state
                candidate_action_count = 1
            iterations = 0
            while (
                driven.responsive_event_count
                < args.stop_after_at_least_responsive_events
                and not state["finished"]
            ):
                iterations += 1
                if iterations > 10_000:
                    raise RuntimeError("bounded driven smoke made no progress")
                if state["needs_input"]:
                    state = driven.wait(100)
                if not state["finished"]:
                    state = driven.advance()
            result = {
                "schema": "development_responsive_incantagos_driven_smoke/v1",
                "status": "COMPLETE_BOUNDED_WIRE_SMOKE",
                "frozen_source_binding": frozen,
                "model_training_held_out": True,
                "source_performance_held_out": False,
                "responsive_event_count": driven.responsive_event_count,
                "requested_stop_after_at_least_responsive_events": (
                    args.stop_after_at_least_responsive_events
                ),
                "responsive_applied_damage": driven.responsive_applied_damage,
                "first_wire_receipt": driven.first_wire_receipt,
                "last_wire_receipt": driven.last_wire_receipt,
                "simulator_time_ms": state["time_ms"],
                "candidate_action_count": candidate_action_count,
                "native_target_count": len(case.native_target_guids),
                "native_target_names_missing": missing_native_names,
                "controlled_player_race": controlled_player["race"],
                "controlled_equipped_item_ids": equipped_ids,
                "cat_initial_proposal": cat_decision.to_dict(),
                "cat_initial_action_label": cat_action_label,
                "cat_initial_action_key": cat_action_key,
                "cat_input_binding": cat_binding.receipt,
                "contra260817_initial_raw_v4_proposal": _proposal_summary(
                    contra_decision
                ),
                "deployed_contra_initial_raw_v4_proposal": (
                    _proposal_summary(deployed_decision)
                    if deployed_decision is not None else None
                ),
                "deployed_contra_saved_xuanfeng": (
                    deployed_runtime_binding["adapter_inputs"]["saved_xuanfeng"]
                    if deployed_runtime_binding is not None else None
                ),
                "deployed_contra_saved_burst": (
                    deployed_runtime_binding["adapter_inputs"]["burst_runtime_gate"]
                    if deployed_runtime_binding is not None else None
                ),
                "deployed_contra_character_context_id": (
                    deployed_runtime_binding["character_context_id"]
                    if deployed_runtime_binding is not None else None
                ),
                "contra260817_input_binding": contra_binding.receipt,
                "comparison_authorized": False,
            }
        cat_policy_id = cat_binding.receipt["source_policy_id"]
        imported_bindings = [
            ImportedReactiveProgramBindingV1(
                binding_id=cat_policy_id,
                source_policy_id=cat_policy_id,
                observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
                resolver_factory=cat_binding.open_session,
            ),
            build_contra260817_incantagos_v4_imported_binding_v1(
                contra_binding, contra_inputs
            ),
        ]
        if deployed_runtime_binding is not None:
            imported_bindings.append(
                build_deployed_contra_incantagos_v4_reactive_binding_v1(
                    contra_binding,
                    deployed_runtime_binding,
                )
            )
        replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
            bridge_factory=lambda: IncantagosDevelopmentDrivenBridgeV1(
                bridge=SimulatorBridgeDynamicV4(
                    args.bridge, cwd=args.simulator_root
                ),
                case=case,
                loaded_model=loaded,
                teammate_seed=args.teammate_seed,
            ),
            case_factory=lambda seed: case,
            observation_projector_factory=lambda current_case: (
                build_incantagos_v4_prefix_projector_v1(fixed_case, current_case)
            ),
            imported_bindings=tuple(imported_bindings),
        )
        for imported in imported_bindings:
            policy_id = imported.source_policy_id
            program = CausalActionProgramV1(
                program_id=f"incantagos-v4-{policy_id}-development-probe",
                selector=ImportedReactiveSelectorV1(
                    imported.binding_id,
                    policy_id,
                    OBSERVATION_CONTRACT_ID_V1,
                ),
                origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
            )
            outcome = replay.replay(
                args.simulator_seed, program, stop_at_or_after_ms=args.replay_stop_ms,
                max_decisions=50,
            )
            result["cat_bounded_replay" if policy_id == cat_policy_id else (
                "contra260817_bounded_replay"
                if "260817" in policy_id else "deployed_contra_bounded_replay"
            )] = {
                "source_policy_id": policy_id,
                "requested_stop_ms": args.replay_stop_ms,
                "status": outcome.status.value,
                "invalid_reason": outcome.invalid_reason,
                "actual_stop_ms": outcome.elapsed_ms,
                "candidate_effective_damage": outcome.effective_damage,
                "terminal_gcd_count": sum(
                    row.get("kind") == "TERMINAL_GCD" for row in outcome.receipts
                ),
                "executed_ops": [
                    {
                        "kind": row["kind"],
                        "action": row.get("action"),
                        "state_time_ms": row.get("state_time_ms"),
                    }
                    for row in outcome.receipts
                    if row.get("kind") in {
                        "START_ATTACK", "STOP_CAST", "QUEUE_SET",
                        "QUEUE_CANCEL", "TERMINAL_GCD", "TERMINAL_WAIT",
                    }
                ],
                "decision_trace": [
                    {
                        "time_ms": row["state_time_ms"],
                        "power_current": row["power_current"],
                        "gcd_action": row["gcd_action"],
                        "queue_op": row["queue_op"],
                        "wait_ms": row["wait_ms"],
                    }
                    for row in outcome.receipts
                    if row.get("kind") == "IMPORTED_REACTIVE_INCUMBENT_SELECTED"
                ],
                "responsive_drive_receipt": outcome.receipts[-1],
                "comparison_authorized": False,
            }
    finally:
        loaded.model.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
