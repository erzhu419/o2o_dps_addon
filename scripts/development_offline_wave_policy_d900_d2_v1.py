"""Run the first strict d900 D2 complete-wave controller panel.

Four numeric lanes share one exact historical request, responsive teammate
model, target model, simulator seed, and teammate seed: Cat, deployed Contra,
Contra_new/Contra260817, and the independent Chronicle ``pi_D`` controller.
The frozen V8 policy is emitted as a structured N/A because it is bound to a
different build and two-wave registry; ``pi_star`` remains a D3 deliverable.

This is a development-model comparison.  Target HP, armor, attackability,
route priority, and teammate response are not historical ground truth.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramOriginV1,
)
from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.contra260817_fury_full_policy_rollout_v4 import (
    Contra260817SimulatorInputsV4,
)
from o2o_dps.contra260817_incantagos_v4_reactive_session_v1 import (
    build_contra260817_incantagos_v4_imported_binding_v1,
)
from o2o_dps.contra_incantagos_v4_policy_binding_v1 import (
    bind_contra_incantagos_v4_policy_v1,
)
from o2o_dps.deployed_contra_incantagos_v4_session_v1 import (
    build_deployed_contra_incantagos_v4_reactive_binding_v1,
)
from o2o_dps.deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v2 import sha256_json
from o2o_dps.offline_team_wave_policy_v1 import (
    compile_offline_team_wave_feedback_policy_v1,
    load_offline_team_wave_record_v1,
)
from o2o_dps.offline_wave_d2_panel_v1 import (
    CAT,
    CONTRA_DEPLOYED,
    CONTRA_NEW,
    PI_D,
    build_d2_full_wave_panel_v1,
    summarize_d2_lane_outcome_v1,
)
from o2o_dps.offline_wave_policy_v1 import (
    build_offline_wave_policy_runtime_v1,
    offline_policy_contribution_receipt_v1,
    offline_policy_execution_contribution_receipt_v1,
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
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
)
from o2o_dps.responsive_team_runtime_store_v1 import (
    open_responsive_team_runtime_store_v1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.sim_bridge_dynamic_v4 import SimulatorBridgeDynamicV4
from o2o_dps.upper_kara_development_route_focus_v1 import (
    DoomguardCurrentStateRouteFocusedBridgeV1,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ATTACKABILITY_MODES,
    OBSERVED_ONSET_UNTIL_SIM_DEATH,
)
from scripts.development_offline_wave_policy_d900_v1 import (
    _cat_resolver_bomb_factory,
    _compact_audit,
    _exact_build_segment_ref,
    _runtime_binding,
)
from scripts.development_responsive_upper_kara_trash_full_wave_v1 import (
    _SparseTargetTimelineBridge,
)
from scripts.development_responsive_upper_kara_trash_smoke_v1 import (
    ENCOUNTER_ID,
    FOCAL_GUID,
    INSTANCE_ID,
    WAVE_ID,
    build_case,
)


SCHEMA = "development_offline_wave_policy_d900_d2/v1"
FROZEN_V8_POLICY_ID = (
    "v8::upper-kara-cat-action-plan-residual-v8-256x256::"
    "contra_turtle_burst__rage::proposal-005"
)
FROZEN_V8_BUILD_ID = "live_bonereaver"
FROZEN_V8_WAVE_CONTRACT = "continuous_two_wave:multi_two=[0,1],single_long=[2]"

# Terminal action surface includes every registered action.  This compact set
# only controls which candidate-damage receipts are retained beside it.
TERMINAL_DAMAGE_REFS = (
    ActionRef(spell_id=20_569),
    ActionRef(spell_id=25_286),
    ActionRef(other_id=7, tag=1),
    ActionRef(other_id=7, tag=2),
)


@dataclass(frozen=True)
class _LaneV1:
    lane_id: str
    program: CausalActionProgramV1
    binding_factory: Callable[
        [list[Any]], ImportedReactiveProgramBindingV1
    ]


def _imported_program(binding: ImportedReactiveProgramBindingV1) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id=f"d900-d2::{binding.source_policy_id}",
        selector=ImportedReactiveSelectorV1(
            binding.binding_id,
            binding.source_policy_id,
            OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
        source_refs=(binding.source_policy_id, SCHEMA),
    )


def _constant_binding_factory(
    binding: ImportedReactiveProgramBindingV1,
) -> Callable[[list[Any]], ImportedReactiveProgramBindingV1]:
    def build(_sessions: list[Any]) -> ImportedReactiveProgramBindingV1:
        return binding

    return build


def run(args: argparse.Namespace) -> dict[str, Any]:
    stage5_parts = args.stage5.parts
    try:
        locator = Path(
            *stage5_parts[stage5_parts.index("offline_data") :]
        ).as_posix()
    except ValueError as error:
        raise ValueError("stage5 path must include offline_data") from error
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
    evaluation_build_ref = _exact_build_segment_ref(exact_build)
    request_sha = sha256_json(case.request)
    config_sha = case.dynamic_config.content_sha256
    target_rule_id = (
        f"d900:{args.attackability_mode}:"
        f"route-focus={str(args.route_focus).lower()}"
    )

    team_wave = load_offline_team_wave_record_v1(
        args.stage5,
        wave_id=args.wave_id,
    )
    offline_policy = compile_offline_team_wave_feedback_policy_v1(
        team_wave,
        player_guid=FOCAL_GUID,
        build_segment_ref=evaluation_build_ref,
        encounter_name="Upper Tower of Karazhan d900 trash wave 1",
    )
    runtime_binding = _runtime_binding(offline_policy, fixed)
    offline_program, offline_binding = build_offline_wave_policy_runtime_v1(
        offline_policy,
        runtime_binding,
        domain_fallback_factory=_cat_resolver_bomb_factory,
    )

    names = tuple(exact_build["equipped_item_names"])
    classifications = {
        row.occurrence_id: "elite" for row in fixed.occurrence_index_registry
    }
    shared = dict(
        equipment_request=case.request,
        equipped_item_names=names,
        classification_hypotheses_by_occurrence_id=classifications,
        source_metadata_artifact_sha256=args.metadata.stem,
        equipment_request_provenance=exact_build,
    )
    cat = build_incantagos_v4_cat_binding_v1(
        fixed, case, metadata, **shared
    )
    contra = bind_contra_incantagos_v4_policy_v1(
        fixed, case, metadata, **shared
    )
    build_evidence = cat.receipt["equipment_request_provenance"]
    if not (
        build_evidence.get("observed_equipment_id_match") is True
        and build_evidence.get("observed_talents_string_match") is True
    ):
        raise RuntimeError("D2 exact historical equipment/talents binding failed")

    cat_binding = ImportedReactiveProgramBindingV1(
        binding_id=cat.receipt["source_policy_id"],
        source_policy_id=cat.receipt["source_policy_id"],
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=cat.open_session,
    )
    contra_inputs = replace(
        Contra260817SimulatorInputsV4(),
        equipped_mainhand_name=names[14],
        equipped_offhand_name=names[15],
        target_bindings=contra.exact_guid_target_bindings(),
    )
    contra_new_binding = build_contra260817_incantagos_v4_imported_binding_v1(
        contra, contra_inputs
    )
    deployed_binding = build_deployed_contra_incantagos_v4_reactive_binding_v1(
        contra,
        load_deployed_contra_runtime_binding_v1(args.deployed_runtime_binding),
    )

    def offline_binding_factory(sessions: list[Any]) -> ImportedReactiveProgramBindingV1:
        def open_tracked_session():
            session = offline_binding.open_session()
            sessions.append(session)
            return session

        return replace(offline_binding, resolver_factory=open_tracked_session)

    lanes = (
        _LaneV1(CAT, _imported_program(cat_binding), _constant_binding_factory(cat_binding)),
        _LaneV1(
            CONTRA_DEPLOYED,
            _imported_program(deployed_binding),
            _constant_binding_factory(deployed_binding),
        ),
        _LaneV1(
            CONTRA_NEW,
            _imported_program(contra_new_binding),
            _constant_binding_factory(contra_new_binding),
        ),
        _LaneV1(PI_D, offline_program, offline_binding_factory),
    )

    def run_lane(seed_offset: int, lane: _LaneV1) -> dict[str, Any]:
        simulator_seed = args.simulator_seed + seed_offset
        teammate_seed = args.teammate_seed + seed_offset
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
        session_refs: list[Any] = []
        binding = lane.binding_factory(session_refs)
        focus_refs: list[Any] = []
        timeline_refs: list[Any] = []

        def bridge_factory():
            driven = IncantagosDevelopmentDrivenBridgeV1(
                bridge=SimulatorBridgeDynamicV4(
                    args.bridge, cwd=args.simulator_root
                ),
                case=case,
                loaded_model=loaded,
                teammate_seed=teammate_seed,
            )
            focused = (
                DoomguardCurrentStateRouteFocusedBridgeV1(
                    driven, case.native_target_guids
                )
                if args.route_focus
                else driven
            )
            if args.route_focus:
                focus_refs.append(focused)
            timeline = _SparseTargetTimelineBridge(focused)
            timeline_refs.append(timeline)
            return timeline

        replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
            bridge_factory=bridge_factory,
            case_factory=lambda _seed: case,
            observation_projector_factory=lambda current: (
                build_incantagos_v4_prefix_projector_v1(fixed, current)
            ),
            imported_bindings=(binding,),
            terminal_telemetry_action_refs=TERMINAL_DAMAGE_REFS,
        )
        started = perf_counter()
        try:
            outcome = replay.replay(
                simulator_seed,
                lane.program,
                max_decisions=args.max_decisions,
            )
        finally:
            with closing(loaded.model):
                pass
        wall_seconds = round(perf_counter() - started, 3)

        offline_execution = None
        fallback_calls = None
        offline_audit = None
        if lane.lane_id == PI_D:
            if len(session_refs) > 1:
                raise RuntimeError(
                    f"PI_D opened {len(session_refs)} sessions; expected at most one"
                )
            if session_refs:
                session = session_refs[0]
                contribution = offline_policy_execution_contribution_receipt_v1(
                    session.audit_events
                )
                offline_execution = contribution["offline_accepted_execution"]
                fallback_calls = session.domain_fallback_calls
                offline_audit = _compact_audit(session.audit_events)
            elif outcome.status.value != "INVALID":
                raise RuntimeError("PI_D completed without opening its source session")
            else:
                offline_audit = {
                    "status": "NOT_OBSERVED_PRE_SESSION_INVALID_REPLAY",
                    "event_kind_counts": {},
                    "action_events": [],
                }

        row = summarize_d2_lane_outcome_v1(
            outcome,
            lane_id=lane.lane_id,
            source_policy_id=binding.source_policy_id,
            simulator_seed=simulator_seed,
            teammate_seed=teammate_seed,
            request_sha256=request_sha,
            dynamic_config_sha256=config_sha,
            evaluation_build_ref=evaluation_build_ref,
            target_rule_id=target_rule_id,
            offline_domain_fallback_calls=fallback_calls,
            offline_accepted_execution=offline_execution,
        )
        row.update(
            {
                "wall_seconds": wall_seconds,
                "decision_count": sum(
                    receipt.get("kind")
                    == "IMPORTED_REACTIVE_INCUMBENT_SELECTED"
                    for receipt in outcome.receipts
                ),
                "offline_audit": offline_audit,
                "target_timeline_snapshots": (
                    timeline_refs[-1].snapshots if timeline_refs else []
                ),
                "first_observed_dead_ms": (
                    timeline_refs[-1].first_observed_dead_ms
                    if timeline_refs
                    else {}
                ),
                "route_focus_receipts": (
                    focus_refs[-1].route_focus_receipts
                    if args.route_focus and focus_refs
                    else []
                ),
            }
        )
        return row

    jobs = [
        (seed_offset, lane)
        for seed_offset in range(args.seed_count)
        for lane in lanes
    ]
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(args.lane_workers, len(jobs))) as pool:
        futures = {
            pool.submit(run_lane, seed_offset, lane): (seed_offset, lane.lane_id)
            for seed_offset, lane in jobs
        }
        for future in as_completed(futures):
            rows.append(future.result())
    lane_order = {lane_id: index for index, lane_id in enumerate(
        (CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D)
    )}
    rows.sort(key=lambda row: (row["simulator_seed"], lane_order[row["lane_id"]]))

    panel = build_d2_full_wave_panel_v1(
        rows,
        request_sha256=request_sha,
        dynamic_config_sha256=config_sha,
        evaluation_build_ref=evaluation_build_ref,
        target_rule_id=target_rule_id,
        v8_policy_id=FROZEN_V8_POLICY_ID,
        v8_exact_build_id=FROZEN_V8_BUILD_ID,
        v8_wave_contract=FROZEN_V8_WAVE_CONTRACT,
    )
    panel.update(
        {
            "run_schema": SCHEMA,
            "instance_id": INSTANCE_ID,
            "encounter_id": ENCOUNTER_ID,
            "wave_id": args.wave_id,
            "focal_player_guid": FOCAL_GUID,
            "frozen_source_binding": frozen,
            "historical_build_source_identity": exact_build["source_identity"],
            "historical_build_character": {
                "race": case.request["raid"]["parties"][0]["players"][0]["race"],
                "talents_string": case.request["raid"]["parties"][0]["players"][0]["talentsString"],
                "equipped_item_ids": [
                    item.get("id")
                    for item in case.request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
                    if item.get("id")
                ],
                "equipped_item_names": list(names),
            },
            "offline_compiled_contribution": offline_policy_contribution_receipt_v1(
                offline_policy
            ),
            "offline_runtime_binding": runtime_binding.to_dict(),
            "parallel_lane_workers": min(args.lane_workers, len(jobs)),
            "target_hp_armor_attackability_are_model_hypotheses": True,
            "responsive_team_is_model_not_historical_replay": True,
        }
    )
    return panel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "metadata",
        "stage5",
        "exact-build",
        "frozen-dispatch",
        "runtime-store",
        "bridge",
        "simulator-root",
        "deployed-runtime-binding",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in (
        "component-id",
        "stage5-content-sha256",
        "partition-compressed-file-sha256",
        "result-sha256",
        "model-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--wave-id", default=WAVE_ID)
    parser.add_argument("--simulator-seed", type=int, default=2026092001)
    parser.add_argument("--teammate-seed", type=int, default=2026092002)
    parser.add_argument("--seed-count", type=int, default=3)
    parser.add_argument("--max-decisions", type=int, default=1000)
    parser.add_argument("--lane-workers", type=int, default=4)
    parser.add_argument("--route-focus", action="store_true")
    parser.add_argument(
        "--attackability-mode",
        choices=ATTACKABILITY_MODES,
        default=OBSERVED_ONSET_UNTIL_SIM_DEATH,
    )
    args = parser.parse_args()
    if not 1 <= args.seed_count <= 8:
        parser.error("development D2 accepts 1-8 paired seeds")
    if not 1 <= args.lane_workers <= 8:
        parser.error("lane-workers must be in 1..8")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
