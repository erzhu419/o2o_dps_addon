"""Run a bounded three-target Upper Kara trash wire smoke on frozen d900 data.

This is a development hypothesis: target HP/armor/reachability and teammates
are modeled, so its output is not a DPS comparison or historical policy score.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.chronicle_external_compact_target_reducer_v1 import (
    reduce_external_team_wave_record,
)
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
from o2o_dps.deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
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
from o2o_dps.upper_kara_compact_encounter_model_v1 import (
    build_compact_encounter_development_model_v1,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import _ContraControlsV1
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    compile_responsive_trash_case_v1,
)
from o2o_dps.upper_kara_route_wave_registry_v1 import build_route_wave_registry_v1
from o2o_dps.upper_kara_target_universe_audit_v1 import (
    audit_upper_kara_target_universe_v1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ATTACKABILITY_MODES,
    OBSERVED_ACTIVITY_WINDOWS,
    compile_resolved_trash_dynamic_v4_case_v1,
)
from o2o_dps.upper_kara_trash_target_contract_v1 import (
    ENCOUNTER_ID,
    INSTANCE_ID,
    build_upper_kara_trash_actionability_contract_v1,
    resolve_upper_kara_trash_target_universe_v1,
)


FOCAL_GUID = "0x0000000000576754"
WAVE_ID = f"{ENCOUNTER_ID}:external-v2-wave:1"


def _find_wave(path: Path, wave_id: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("wave", {}).get("wave_id") == wave_id:
                return row
    raise RuntimeError(f"Stage-5 wave not found: {wave_id}")


def build_case(args: argparse.Namespace):
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    registry = build_route_wave_registry_v1([metadata])
    instance = next(row for row in registry["instances"] if row["instance_id"] == INSTANCE_ID)
    encounter = next(row for row in instance["encounters"] if row["encounter_id"] == ENCOUNTER_ID)
    reduction = reduce_external_team_wave_record(
        args.stage5,
        instance_id=INSTANCE_ID,
        encounter_id=ENCOUNTER_ID,
        wave_id=args.wave_id,
    )
    audit = audit_upper_kara_target_universe_v1(
        encounter,
        reduction,
        metadata_units=metadata["units"],
        metadata_players=metadata["players"],
    )
    resolution = resolve_upper_kara_trash_target_universe_v1(encounter, audit)
    actionability = build_upper_kara_trash_actionability_contract_v1(resolution)
    compact = build_compact_encounter_development_model_v1(
        encounter,
        reduction,
        resolution,
        model_id="upper-kara-trash-three-target-development-v1",
        focal_player_guid=FOCAL_GUID,
        armor_hypotheses=[0, 1721, 1961, 2861, 3761, 4211],
    )
    armors = {occurrence_id: 1721 for occurrence_id in actionability["full_environment_occurrence_ids"]}
    exact_build = json.loads(args.exact_build.read_text(encoding="utf-8"))
    if exact_build["source_identity"]["player_guid"] != FOCAL_GUID:
        raise RuntimeError("historical Fury build does not bind the selected focal GUID")
    fixed = compile_resolved_trash_dynamic_v4_case_v1(
        compact,
        actionability,
        exact_build["request"],
        selected_armor_by_occurrence_id=armors,
        horizon_ms=encounter["duration_ms"],
        target_level=60,
        attackability_mode=getattr(args, "attackability_mode", OBSERVED_ACTIVITY_WINDOWS),
    )
    responsive = compile_responsive_trash_case_v1(
        fixed,
        _find_wave(args.stage5, args.wave_id),
        source_membership_evidence={
            "component_id": args.component_id,
            "split": "VALIDATION",
            "model_training_held_out": True,
            "stage5_content_sha256": args.stage5_content_sha256,
            "heldout_performance_evidence_eligible": False,
            "comparison_authorized": False,
        },
    )
    return fixed, responsive


def _contra_summary(decision) -> dict:
    return {
        "valid": decision.valid,
        "gcd": decision.gcd,
        "wait_ms": decision.wait_ms,
        "swing_queue": decision.swing_queue.value,
        "ordered_sink_channels": [row.channel for row in decision.raw_sink_order],
        "source_identity_verified": decision.metadata.get("source_identity_verified"),
    }


def run(args: argparse.Namespace) -> dict:
    stage5_parts = args.stage5.parts
    locator = Path(*stage5_parts[stage5_parts.index("offline_data"):]).as_posix()
    frozen = verify_frozen_validation_source_v1(
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
    classification = {
        row.occurrence_id: "elite" for row in fixed.occurrence_index_registry
    }
    item_names = tuple(exact_build["equipped_item_names"])
    cat_binding = build_incantagos_v4_cat_binding_v1(
        fixed,
        case,
        metadata,
        equipment_request=case.request,
        equipped_item_names=item_names,
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256=args.metadata.stem,
        equipment_request_provenance=exact_build,
    )
    contra_binding = bind_contra_incantagos_v4_policy_v1(
        fixed,
        case,
        metadata,
        equipment_request=case.request,
        equipped_item_names=item_names,
        classification_hypotheses_by_occurrence_id=classification,
        source_metadata_artifact_sha256=args.metadata.stem,
        equipment_request_provenance=exact_build,
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
    with closing(loaded.model), IncantagosDevelopmentDrivenBridgeV1(
        bridge=SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root),
        case=case,
        loaded_model=loaded,
        teammate_seed=args.teammate_seed,
    ) as driven:
        state = driven.load_dynamic_v4(
            case.request, args.simulator_seed, case.dynamic_config
        ).state
        initial_state = {
            "time_ms": state["time_ms"],
            "rage_current": state["power"]["current"],
            "observation_phase": "AFTER_READY_TIME_ZERO_TEAMMATE_COHORT",
            "dynamic_target_semantics": state.get("dynamic_target_semantics"),
            "available_action_labels": [row.label for row in driven.actions()],
        }
        actions = tuple(driven.actions())
        projected = build_incantagos_v4_prefix_projector_v1(fixed, case)(state, actions)
        cat_decision = cat_binding.open_session()(projected, actions)
        contra_inputs = replace(
            Contra260817SimulatorInputsV4(),
            equipped_mainhand_name=item_names[14],
            equipped_offhand_name=item_names[15],
            target_bindings=contra_binding.exact_guid_target_bindings(),
        )
        contra_source = Contra260817FuryFullPolicyAdapterV3(SOURCE_DEFAULT_PROFILE_V3)
        contra260817_decision = propose_contra260817_incantagos_v4_v1(
            contra_binding,
            controls=_ContraControlsV1(
                autoattack_active=False,
                mainhand_name=item_names[14],
                offhand_name=item_names[15],
            ),
            inputs=contra_inputs,
            source=contra_source,
            state=state,
            available=actions,
            last_gcd_action="",
        )
        deployed_decision = None
        if args.deployed_runtime_binding is not None:
            deployed_decision = propose_deployed_contra_incantagos_v4_v1(
                contra_binding,
                runtime_binding=load_deployed_contra_runtime_binding_v1(
                    args.deployed_runtime_binding
                ),
                state=state,
                available=actions,
                last_gcd_action="",
            )
        attack = driven.start_attack()
        if not attack.accepted:
            raise RuntimeError("development start_attack was not accepted")
        state = attack.state
        iterations = 0
        while driven.responsive_event_count < args.stop_after_at_least_responsive_events and not state["finished"]:
            iterations += 1
            if iterations > 10_000:
                raise RuntimeError("bounded responsive smoke made no progress")
            if state["needs_input"]:
                state = driven.wait(100)
            if not state["finished"]:
                state = driven.advance()
        return {
            "schema": "development_responsive_upper_kara_trash_smoke/v1",
            "status": "COMPLETE_BOUNDED_WIRE_SMOKE",
            "comparison_authorized": False,
            "frozen_source_binding": frozen,
            "focal_guid": FOCAL_GUID,
            "wave_id": args.wave_id,
            "historical_build_segment_id": "segment-0042",
            "target_level_hypothesis": 60,
            "target_classification_hypothesis": "elite",
            "native_target_guids": list(case.native_target_guids),
            "native_target_count": len(case.native_target_guids),
            "fixed_background_event_count_removed": case.receipt["fixed_background_event_count_removed"],
            "responsive_teammate_count": len(case.teammate_player_guids),
            "initial_state": initial_state,
            "cat_initial_proposal": cat_decision.to_dict(),
            "cat_input_binding": cat_binding.receipt,
            "contra260817_initial_raw_v4_proposal": _contra_summary(contra260817_decision),
            "contra260817_input_binding": contra_binding.receipt,
            "deployed_contra_initial_raw_v4_proposal": (
                _contra_summary(deployed_decision)
                if deployed_decision is not None else None
            ),
            "deployed_contra_input_binding": (
                contra_binding.receipt if deployed_decision is not None else None
            ),
            "historical_equipment_and_talents_bound": cat_binding.receipt[
                "equipment_request_provenance"
            ]["observed_equipment_id_match"]
            and cat_binding.receipt["equipment_request_provenance"][
                "observed_talents_string_match"
            ],
            "responsive_event_count": driven.responsive_event_count,
            "responsive_applied_damage": driven.responsive_applied_damage,
            "first_wire_receipt": driven.first_wire_receipt,
            "last_wire_receipt": driven.last_wire_receipt,
            "final_time_ms": state["time_ms"],
            "final_target_semantics": state.get("dynamic_target_semantics"),
            "target_contract_status": fixed.receipt["status"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "stage5", "exact-build", "frozen-dispatch", "runtime-store", "bridge", "simulator-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("component-id", "stage5-content-sha256", "partition-compressed-file-sha256", "result-sha256", "model-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--wave-id", default=WAVE_ID)
    parser.add_argument("--simulator-seed", type=int, default=2026092001)
    parser.add_argument("--teammate-seed", type=int, default=2026092002)
    parser.add_argument("--stop-after-at-least-responsive-events", type=int, default=40)
    parser.add_argument("--deployed-runtime-binding", type=Path)
    parser.add_argument("--attackability-mode", choices=ATTACKABILITY_MODES, default=OBSERVED_ACTIVITY_WINDOWS)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
