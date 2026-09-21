"""Expand the frozen d900 D3 winner on new blind paired seeds.

The winner is loaded verbatim from a completed D3 artifact.  This runner has
no candidate generator and no selector: it executes that one program and the
four fixed comparators on the same new seeds, retaining null metrics for every
incomplete replay.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.causal_action_program_v1 import ImportedReactiveProgramBindingV1
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
    summarize_d2_lane_outcome_v1,
)
from o2o_dps.offline_wave_d3_action_attribution_v1 import (
    attribute_d3_searched_replay_v1,
)
from o2o_dps.offline_wave_d3_frozen_heldout_v1 import (
    CONTROLLER_IDS,
    PI_STAR,
    adjudicate_frozen_heldout_expansion_v1,
    build_frozen_heldout_expansion_contract_v1,
    build_frozen_heldout_row_v1,
)
from o2o_dps.offline_wave_execution_trace_v1 import (
    project_offline_wave_execution_trace_v1,
)
from o2o_dps.offline_wave_policy_v1 import build_offline_wave_policy_runtime_v1
from o2o_dps.offline_wave_searched_program_v1 import (
    searched_wave_program_from_dict_v1,
)
from o2o_dps.offline_wave_searched_runtime_v1 import (
    build_searched_wave_program_runtime_v1,
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
from o2o_dps.upper_kara_development_route_focus_v1 import (
    DoomguardCurrentStateRouteFocusedBridgeV1,
)
from o2o_dps.upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
    build_incantagos_v4_prefix_projector_v1,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from o2o_dps.upper_kara_trash_dynamic_v4_adapter_v1 import (
    ATTACKABILITY_MODES,
    OBSERVED_ONSET_UNTIL_SIM_DEATH,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1
from scripts.development_offline_wave_policy_d900_d3_v1 import (
    TERMINAL_DAMAGE_REFS,
    _ControllerV1,
    _compact_audit,
    _compact_trace,
    _imported_program,
)
from scripts.development_offline_wave_policy_d900_v1 import (
    _cat_resolver_bomb_factory,
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


OUTPUT_SCHEMA = "development_offline_wave_policy_d900_d3_frozen_heldout/v1"


def _seed_pairs(start: int, count: int) -> tuple[tuple[int, int], ...]:
    return tuple((start + index, start + 100_000 + index) for index in range(count))


def _require_equal(label: str, current: object, frozen: object) -> None:
    if current != frozen:
        raise RuntimeError(
            f"current {label} differs from frozen D3 source: {current!r} != {frozen!r}"
        )


def run(
    args: argparse.Namespace,
    *,
    runtime_row_enricher: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    source = json.loads(args.frozen_d3_result.read_text(encoding="utf-8"))
    expansion_pairs = _seed_pairs(args.heldout_seed, args.heldout_seed_count)
    contract = build_frozen_heldout_expansion_contract_v1(
        source=source,
        heldout_seed_pairs=expansion_pairs,
        expansion_id=args.expansion_id,
    )
    frozen_manifest = contract["frozen_candidate"]

    stage5_parts = args.stage5.parts
    try:
        locator = Path(
            *stage5_parts[stage5_parts.index("offline_data") :]
        ).as_posix()
    except ValueError as error:
        raise ValueError("stage5 path must include offline_data") from error
    frozen_source = verify_frozen_validation_source_v1(
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

    _require_equal("instance_id", INSTANCE_ID, source.get("instance_id"))
    _require_equal("encounter_id", ENCOUNTER_ID, source.get("encounter_id"))
    _require_equal("wave_id", args.wave_id, source.get("wave_id"))
    _require_equal("focal_player_guid", FOCAL_GUID, source.get("focal_player_guid"))
    _require_equal("frozen source binding", frozen_source, source.get("frozen_source_binding"))
    _require_equal("request_sha256", request_sha, source.get("request_sha256"))
    _require_equal("dynamic_config_sha256", config_sha, source.get("dynamic_config_sha256"))
    _require_equal("evaluation_build_ref", evaluation_build_ref, source.get("evaluation_build_ref"))
    _require_equal("target_rule_id", target_rule_id, source.get("target_rule_id"))

    team_wave = load_offline_team_wave_record_v1(args.stage5, wave_id=args.wave_id)
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
    cat = build_incantagos_v4_cat_binding_v1(fixed, case, metadata, **shared)
    contra = bind_contra_incantagos_v4_policy_v1(fixed, case, metadata, **shared)
    build_evidence = cat.receipt["equipment_request_provenance"]
    if not (
        build_evidence.get("observed_equipment_id_match") is True
        and build_evidence.get("observed_talents_string_match") is True
    ):
        raise RuntimeError("frozen held-out exact equipment/talents binding failed")
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
    searched = searched_wave_program_from_dict_v1(frozen_manifest["program"])
    searched_program, searched_binding = build_searched_wave_program_runtime_v1(
        searched,
        runtime_binding,
        domain_fallback_factory=_cat_resolver_bomb_factory,
    )
    controllers = (
        _ControllerV1(CAT, _imported_program(cat_binding), cat_binding, "baseline"),
        _ControllerV1(
            CONTRA_DEPLOYED,
            _imported_program(deployed_binding),
            deployed_binding,
            "baseline",
        ),
        _ControllerV1(
            CONTRA_NEW,
            _imported_program(contra_new_binding),
            contra_new_binding,
            "baseline",
        ),
        _ControllerV1(PI_D, offline_program, offline_binding, "offline"),
        _ControllerV1(PI_STAR, searched_program, searched_binding, "searched"),
    )
    if tuple(row.controller_id for row in controllers) != CONTROLLER_IDS:
        raise RuntimeError("five-controller frozen held-out panel drifted")

    def replay_controller(
        controller: _ControllerV1,
        simulator_seed: int,
        teammate_seed: int,
    ) -> dict[str, Any]:
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
        sessions: list[Any] = []

        def open_tracked_session():
            session = controller.binding.open_session()
            sessions.append(session)
            return session

        binding = replace(controller.binding, resolver_factory=open_tracked_session)

        def bridge_factory():
            driven = IncantagosDevelopmentDrivenBridgeV1(
                bridge=SimulatorBridgeDynamicV4(args.bridge, cwd=args.simulator_root),
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
            return _SparseTargetTimelineBridge(focused)

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
                controller.program,
                max_decisions=args.max_decisions,
            )
        finally:
            with closing(loaded.model):
                pass
        wall_seconds = round(perf_counter() - started, 3)
        if len(sessions) > 1:
            raise RuntimeError(
                f"{controller.controller_id} opened {len(sessions)} sessions"
            )
        session = sessions[0] if sessions else None
        fallback_calls = (
            getattr(session, "domain_fallback_calls", None)
            if session is not None
            else None
        )
        # Cat/Contra do not own a domain-fallback path.  PI_D and PI_STAR must
        # expose an explicit counter so zero is an observed gate, not absence.
        if controller.controller_id in {PI_D, PI_STAR} and fallback_calls is None:
            raise RuntimeError(
                f"{controller.controller_id} did not report domain fallback calls"
            )
        raw = summarize_d2_lane_outcome_v1(
            outcome,
            lane_id=CAT,
            source_policy_id=controller.binding.source_policy_id,
            simulator_seed=simulator_seed,
            teammate_seed=teammate_seed,
            request_sha256=request_sha,
            dynamic_config_sha256=config_sha,
            evaluation_build_ref=evaluation_build_ref,
            target_rule_id=target_rule_id,
        )
        score_status = raw["score_status"]
        failure_reason = raw["failure_reason"]
        if fallback_calls not in {None, 0}:
            score_status = "DOMAIN_FALLBACK_USED"
            failure_reason = f"domain fallback calls={fallback_calls}"
        completed = score_status == "COMPLETED"
        execution_trace = project_offline_wave_execution_trace_v1(outcome)
        searched_action_attribution = (
            attribute_d3_searched_replay_v1(
                searched,
                session.audit_events,
                execution_trace,
            )
            if controller.controller_id == PI_STAR
            else None
        )
        contract_row = build_frozen_heldout_row_v1(
            candidate_id=frozen_manifest["candidate_id"],
            controller_id=controller.controller_id,
            simulator_seed=simulator_seed,
            teammate_seed=teammate_seed,
            replay_status=(
                outcome.status.value
                if isinstance(outcome.status, ReplayStatusV1)
                else str(outcome.status)
            ),
            score_status=score_status,
            required_targets_dead=raw["required_targets_dead"],
            effective_damage=raw["effective_damage"] if completed else None,
            dps=raw["dps"] if completed else None,
            completion_time_ms=(raw["completion_time_ms"] if completed else None),
            domain_fallback_calls=fallback_calls,
            failure_reason=failure_reason,
        )
        runtime_row = {
            **contract_row,
            "source_policy_id": controller.binding.source_policy_id,
            "terminal_resource": raw["terminal_resource"],
            "terminal_cooldowns": raw["terminal_cooldowns"],
            "accepted_action_receipt_count": raw[
                "accepted_action_receipt_count"
            ],
            "wall_seconds": wall_seconds,
            "accepted_execution_trace": _compact_trace(execution_trace),
            "runtime_audit": _compact_audit(session),
            "searched_action_attribution": searched_action_attribution,
        }
        if runtime_row_enricher is not None:
            enrichment = runtime_row_enricher(
                outcome=outcome,
                raw_summary=raw,
                controller_id=controller.controller_id,
                simulator_seed=simulator_seed,
                teammate_seed=teammate_seed,
                domain_fallback_calls=fallback_calls,
            )
            if not isinstance(enrichment, Mapping):
                raise TypeError("runtime_row_enricher must return a mapping")
            overlap = sorted(set(runtime_row) & set(enrichment))
            if overlap:
                raise RuntimeError(
                    "runtime row enrichment overlaps existing fields: "
                    + ", ".join(overlap)
                )
            runtime_row.update(dict(enrichment))
        return runtime_row

    jobs = [
        (controller, simulator_seed, teammate_seed)
        for simulator_seed, teammate_seed in expansion_pairs
        for controller in controllers
    ]
    runtime_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(args.lane_workers, len(jobs))) as pool:
        futures = {
            pool.submit(
                replay_controller, controller, simulator_seed, teammate_seed
            ): (controller.controller_id, simulator_seed, teammate_seed)
            for controller, simulator_seed, teammate_seed in jobs
        }
        for future in as_completed(futures):
            runtime_rows.append(future.result())
    runtime_rows.sort(
        key=lambda row: (
            row["simulator_seed"],
            row["teammate_seed"],
            CONTROLLER_IDS.index(row["controller_id"]),
        )
    )
    receipt = adjudicate_frozen_heldout_expansion_v1(
        contract=contract, rows=runtime_rows
    )
    return {
        "schema": OUTPUT_SCHEMA,
        "status": receipt["status"],
        "frozen_d3_source_schema": source["schema"],
        "contract": contract,
        "receipt": receipt,
        "runtime_rows": runtime_rows,
        "pi_star_action_attribution_by_seed": [
            {
                "simulator_seed": row["simulator_seed"],
                "teammate_seed": row["teammate_seed"],
                "attribution": row["searched_action_attribution"],
            }
            for row in runtime_rows
            if row["controller_id"] == PI_STAR
        ],
        "frozen_source_binding": frozen_source,
        "request_sha256": request_sha,
        "dynamic_config_sha256": config_sha,
        "evaluation_build_ref": evaluation_build_ref,
        "target_rule_id": target_rule_id,
        "parallel_lane_workers": min(args.lane_workers, len(jobs)),
        "contracts": {
            "winner_loaded_verbatim_from_prior_d3_result": True,
            "candidate_generation_performed": False,
            "selection_performed": False,
            "all_seed_components_new_vs_prior_d3": True,
            "same_seed_pair_for_all_five_controllers": True,
            "incomplete_imputed_as_zero": False,
            "pi_d_and_pi_star_fallback_required_zero": True,
            "target_hp_armor_attackability_are_model_hypotheses": True,
            "responsive_team_is_model_not_historical_replay": True,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


def build_argument_parser_v1(
    *, description: str | None = None
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description or __doc__)
    for name in (
        "metadata",
        "stage5",
        "exact-build",
        "frozen-dispatch",
        "runtime-store",
        "bridge",
        "simulator-root",
        "deployed-runtime-binding",
        "frozen-d3-result",
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
    parser.add_argument("--expansion-id", default="d900-d3-frozen-blind-v1")
    parser.add_argument("--heldout-seed", type=int, default=2026092401)
    parser.add_argument("--heldout-seed-count", type=int, default=8)
    parser.add_argument("--max-decisions", type=int, default=300)
    parser.add_argument("--lane-workers", type=int, default=32)
    parser.add_argument("--route-focus", action="store_true")
    parser.add_argument(
        "--attackability-mode",
        choices=ATTACKABILITY_MODES,
        default=OBSERVED_ONSET_UNTIL_SIM_DEATH,
    )
    return parser


def main() -> int:
    parser = build_argument_parser_v1()
    args = parser.parse_args()
    if not 1 <= args.heldout_seed_count <= 64:
        parser.error("heldout-seed-count must be in 1..64")
    if not 1 <= args.lane_workers <= 64:
        parser.error("lane-workers must be in 1..64")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
