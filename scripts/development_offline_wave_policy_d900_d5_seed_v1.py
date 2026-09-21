"""Evaluate one paired d900 seed for D5 selection or confirmation."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
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
    summarize_d2_lane_outcome_v1,
)
from o2o_dps.offline_wave_d3_action_attribution_v1 import (
    attribute_d3_searched_replay_v1,
)
from o2o_dps.offline_wave_d3_frozen_heldout_v1 import PI_STAR
from o2o_dps.offline_wave_d4_all_seed_endpoint_v1 import (
    build_all_seed_endpoint_row_v1,
    extract_terminal_endpoint_v1,
)
from o2o_dps.offline_wave_execution_trace_v1 import (
    project_offline_wave_execution_trace_v1,
)
from o2o_dps.offline_wave_d5_selection_v1 import (
    CONFIRMATION_CONTROLLER_IDS,
    D4_RETENTION,
    D5_TAIL_ONLY,
)
from o2o_dps.offline_wave_policy_v1 import build_offline_wave_policy_runtime_v1
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveProgramV1,
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
from o2o_dps.responsive_team_hpc_result_loader_v1 import (
    CurrentSourceDeclarationV1,
)
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
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1
from scripts.development_offline_wave_policy_d900_d3_frozen_heldout_v1 import (
    build_argument_parser_v1,
)
from scripts.development_offline_wave_policy_d900_d3_v1 import (
    TERMINAL_DAMAGE_REFS,
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
    build_case,
)


JSONMap = dict[str, Any]
OUTPUT_SCHEMA = "development_offline_wave_policy_d900_d5_seed/v1"
IMPLEMENTATION_REVISION = "d900-d5-one-seed-shard-v2"
@dataclass(frozen=True)
class _ControllerV1:
    controller_id: str
    candidate_id: str
    program: CausalActionProgramV1
    binding: ImportedReactiveProgramBindingV1
    searched_program: SearchedWaveProgramV1 | None
    requires_zero_fallback: bool


@dataclass(frozen=True)
class _EnvironmentV1:
    fixed: Any
    case: Any
    runtime_binding: Any
    request_sha256: str
    dynamic_config_sha256: str
    evaluation_build_ref: str
    target_rule_id: str
    baseline_controllers: Mapping[str, _ControllerV1]


def _seed_pairs(start: int, count: int) -> tuple[tuple[int, int], ...]:
    return tuple((seed, seed + 100_000) for seed in range(start, start + count))


def _require_equal(label: str, current: object, frozen: object) -> None:
    if current != frozen:
        raise RuntimeError(f"{label} differs from the frozen D4 environment")


def _load_candidate_panel_v1(path: Path, horizon_ms: int) -> JSONMap:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("candidate panel must be an object")
    if value.get("candidate_count") != 256:
        raise ValueError("D5 selection panel must contain exactly 256 candidates")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 256:
        raise ValueError("D5 candidate panel has the wrong manifest count")
    if value.get("fixed_horizon_ms") != horizon_ms:
        raise ValueError("D5 candidate panel has a different fixed horizon")
    if value.get("candidates_frozen_before_seed_execution") is not True:
        raise ValueError("D5 candidate panel was not frozen before execution")
    if value.get("per_seed_candidate_generation") is not False:
        raise ValueError("D5 candidate panel permits per-seed generation")
    ids = [row.get("candidate_id") for row in candidates if isinstance(row, Mapping)]
    if len(ids) != 256 or any(not isinstance(value, str) for value in ids):
        raise ValueError("D5 candidate manifests lack candidate IDs")
    if len(set(ids)) != len(ids):
        raise ValueError("D5 candidate IDs are not unique")
    return value


def _selection_payload(
    path: Path, *, candidate_panel: Mapping[str, Any]
) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("selection receipt must be an object")
    if value.get("candidate_panel") != candidate_panel:
        raise ValueError(
            "selection receipt candidate panel differs from the staged authority"
        )
    receipt = value.get("selection_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("selection artifact lacks selection_receipt")
    frozen = receipt.get("frozen_candidate")
    if not isinstance(frozen, Mapping):
        raise ValueError("selection receipt lacks frozen_candidate")
    return receipt


def _manifest_role(manifest: Mapping[str, Any]) -> str | None:
    raw = manifest.get("candidate_role") or manifest.get("proposal_arm")
    if isinstance(raw, str):
        normalized = raw.strip().upper().replace("-", "_")
        if normalized in {D4_RETENTION, "RETENTION", "PARENT_RETENTION"}:
            return D4_RETENTION
        if normalized in {D5_TAIL_ONLY, "TAIL_ONLY", "TAIL"}:
            return D5_TAIL_ONLY
    candidate_id = str(manifest.get("candidate_id") or "").lower()
    if "retention" in candidate_id:
        return D4_RETENTION
    if "tail-only" in candidate_id or "tail_only" in candidate_id:
        return D5_TAIL_ONLY
    return None


def _find_anchor_v1(candidates: list[Any], role: str) -> Mapping[str, Any]:
    matches = [
        row
        for row in candidates
        if isinstance(row, Mapping) and _manifest_role(row) == role
    ]
    if len(matches) != 1:
        raise ValueError(f"candidate panel must contain exactly one {role} anchor")
    return matches[0]


def _build_environment_v1(
    args: argparse.Namespace, d4_source: Mapping[str, Any]
) -> _EnvironmentV1:
    stage5_parts = args.stage5.parts
    try:
        locator = Path(
            *stage5_parts[stage5_parts.index("offline_data") :]
        ).as_posix()
    except ValueError as error:
        raise ValueError("stage5 path must include offline_data") from error
    source_binding = verify_frozen_validation_source_v1(
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
    build_ref = _exact_build_segment_ref(exact_build)
    request_sha = sha256_json(case.request)
    config_sha = case.dynamic_config.content_sha256
    target_rule_id = (
        f"d900:{args.attackability_mode}:"
        f"route-focus={str(args.route_focus).lower()}"
    )
    for label, current, frozen in (
        ("frozen source binding", source_binding, d4_source.get("frozen_source_binding")),
        ("request_sha256", request_sha, d4_source.get("request_sha256")),
        ("dynamic_config_sha256", config_sha, d4_source.get("dynamic_config_sha256")),
        ("evaluation_build_ref", build_ref, d4_source.get("evaluation_build_ref")),
        ("target_rule_id", target_rule_id, d4_source.get("target_rule_id")),
    ):
        _require_equal(label, current, frozen)

    team_wave = load_offline_team_wave_record_v1(args.stage5, wave_id=args.wave_id)
    offline_policy = compile_offline_team_wave_feedback_policy_v1(
        team_wave,
        player_guid=FOCAL_GUID,
        build_segment_ref=build_ref,
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
        raise RuntimeError("D5 exact equipment/talents binding failed")
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
    baselines = {
        CAT: _ControllerV1(
            CAT, CAT, _imported_program(cat_binding), cat_binding, None, False
        ),
        CONTRA_DEPLOYED: _ControllerV1(
            CONTRA_DEPLOYED,
            CONTRA_DEPLOYED,
            _imported_program(deployed_binding),
            deployed_binding,
            None,
            False,
        ),
        CONTRA_NEW: _ControllerV1(
            CONTRA_NEW,
            CONTRA_NEW,
            _imported_program(contra_new_binding),
            contra_new_binding,
            None,
            False,
        ),
        PI_D: _ControllerV1(
            PI_D, PI_D, offline_program, offline_binding, None, True
        ),
    }
    return _EnvironmentV1(
        fixed=fixed,
        case=case,
        runtime_binding=runtime_binding,
        request_sha256=request_sha,
        dynamic_config_sha256=config_sha,
        evaluation_build_ref=build_ref,
        target_rule_id=target_rule_id,
        baseline_controllers=baselines,
    )


def _searched_controller_v1(
    *,
    controller_id: str,
    manifest: Mapping[str, Any],
    runtime_binding: Any,
) -> _ControllerV1:
    candidate_id = manifest.get("candidate_id")
    program_wire = manifest.get("program")
    if not isinstance(candidate_id, str) or not isinstance(program_wire, Mapping):
        raise ValueError("candidate manifest lacks identity or program")
    searched = searched_wave_program_from_dict_v1(program_wire)
    program, binding = build_searched_wave_program_runtime_v1(
        searched,
        runtime_binding,
        domain_fallback_factory=_cat_resolver_bomb_factory,
    )
    return _ControllerV1(
        controller_id=controller_id,
        candidate_id=candidate_id,
        program=program,
        binding=binding,
        searched_program=searched,
        requires_zero_fallback=True,
    )


def _build_endpoint_row_v1(
    *,
    phase: str,
    candidate_id: str,
    controller_id: str,
    simulator_seed: int,
    teammate_seed: int,
    replay_status: str,
    terminal_endpoint: Mapping[str, Any],
    domain_fallback_calls: int | None,
    requires_zero_fallback: bool,
    action_attribution: Mapping[str, Any] | None,
) -> JSONMap:
    if phase == "selection":
        from o2o_dps.offline_wave_d5_selection_v1 import (
            SELECTION,
            build_d5_selection_row_v1,
        )

        endpoint = build_all_seed_endpoint_row_v1(
            candidate_id=candidate_id,
            controller_id=PI_STAR,
            simulator_seed=simulator_seed,
            teammate_seed=teammate_seed,
            replay_status=replay_status,
            terminal_endpoint=terminal_endpoint,
            domain_fallback_calls=domain_fallback_calls,
        )
        if action_attribution is None:
            raise RuntimeError("selection candidate lacks action attribution")
        return build_d5_selection_row_v1(
            cohort=SELECTION,
            endpoint_row=endpoint,
            action_attribution=action_attribution,
        )

    from o2o_dps.offline_wave_d5_selection_v1 import (
        build_d5_confirmation_endpoint_row_v1,
    )

    raw_controller_id = (
        PI_STAR
        if controller_id in {PI_STAR, D4_RETENTION, D5_TAIL_ONLY}
        else controller_id
    )
    endpoint = build_all_seed_endpoint_row_v1(
        candidate_id=candidate_id,
        controller_id=raw_controller_id,
        simulator_seed=simulator_seed,
        teammate_seed=teammate_seed,
        replay_status=replay_status,
        terminal_endpoint=terminal_endpoint,
        domain_fallback_calls=domain_fallback_calls,
    )
    return build_d5_confirmation_endpoint_row_v1(
        controller_id=controller_id,
        endpoint_row=endpoint,
    )


def _replay_controller_v1(
    *,
    args: argparse.Namespace,
    environment: _EnvironmentV1,
    controller: _ControllerV1,
    simulator_seed: int,
    teammate_seed: int,
) -> JSONMap:
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
            case=environment.case,
            loaded_model=loaded,
            teammate_seed=teammate_seed,
        )
        focused = (
            DoomguardCurrentStateRouteFocusedBridgeV1(
                driven, environment.case.native_target_guids
            )
            if args.route_focus
            else driven
        )
        return _SparseTargetTimelineBridge(focused)

    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=bridge_factory,
        case_factory=lambda _seed: environment.case,
        observation_projector_factory=lambda current: (
            build_incantagos_v4_prefix_projector_v1(environment.fixed, current)
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
    if controller.requires_zero_fallback and fallback_calls is None:
        raise RuntimeError(
            f"{controller.controller_id} did not report domain fallback calls"
        )
    raw = summarize_d2_lane_outcome_v1(
        outcome,
        lane_id=CAT,
        source_policy_id=controller.binding.source_policy_id,
        simulator_seed=simulator_seed,
        teammate_seed=teammate_seed,
        request_sha256=environment.request_sha256,
        dynamic_config_sha256=environment.dynamic_config_sha256,
        evaluation_build_ref=environment.evaluation_build_ref,
        target_rule_id=environment.target_rule_id,
    )
    terminal = extract_terminal_endpoint_v1(
        outcome,
        required_target_indexes=(0, 1, 2),
        horizon_ms=args.fixed_horizon_ms,
    )
    replay_status = (
        outcome.status.value
        if isinstance(outcome.status, ReplayStatusV1)
        else str(outcome.status)
    )
    trace = project_offline_wave_execution_trace_v1(outcome)
    attribution = (
        attribute_d3_searched_replay_v1(
            controller.searched_program,
            session.audit_events,
            trace,
        )
        if controller.searched_program is not None and session is not None
        else None
    )
    endpoint_row = _build_endpoint_row_v1(
        phase=args.phase,
        candidate_id=controller.candidate_id,
        controller_id=controller.controller_id,
        simulator_seed=simulator_seed,
        teammate_seed=teammate_seed,
        replay_status=replay_status,
        terminal_endpoint=terminal,
        domain_fallback_calls=fallback_calls,
        requires_zero_fallback=controller.requires_zero_fallback,
        action_attribution=attribution,
    )
    return {
        "endpoint_row": endpoint_row,
        "action_attribution": (
            {
                "controller_id": controller.controller_id,
                "candidate_id": controller.candidate_id,
                "simulator_seed": simulator_seed,
                "teammate_seed": teammate_seed,
                "attribution": attribution,
            }
            if attribution is not None
            else None
        ),
        "runtime_summary": {
            "controller_id": controller.controller_id,
            "candidate_id": controller.candidate_id,
            "source_policy_id": controller.binding.source_policy_id,
            "simulator_seed": simulator_seed,
            "teammate_seed": teammate_seed,
            "wall_seconds": wall_seconds,
            "required_targets_dead": raw["required_targets_dead"],
            "accepted_action_receipt_count": raw[
                "accepted_action_receipt_count"
            ],
            "accepted_action_count": _compact_trace(trace)[
                "accepted_action_count"
            ],
        },
    }


def run(args: argparse.Namespace) -> JSONMap:
    if args.heldout_seed_count != 1:
        raise ValueError("D5 shards require exactly one paired seed")
    if args.implementation_revision != IMPLEMENTATION_REVISION:
        raise ValueError("D5 shard implementation revision differs")
    panel = _load_candidate_panel_v1(
        args.candidate_panel, args.fixed_horizon_ms
    )
    if sha256_json(panel) != args.candidate_panel_sha256:
        raise ValueError("D5 candidate panel digest differs")
    if args.phase == "selection" and args.selection_receipt_sha256 is not None:
        raise ValueError("selection must not bind a confirmation receipt digest")
    expected_pairs = panel[
        "selection_seed_pairs"
        if args.phase == "selection"
        else "confirmation_seed_pairs"
    ]
    campaign_pairs = [
        {"simulator_seed": a, "teammate_seed": b}
        for a, b in _seed_pairs(
            args.campaign_first_seed, args.campaign_seed_count
        )
    ]
    if expected_pairs != campaign_pairs:
        raise ValueError("candidate panel and campaign seed contract differ")
    executed_pair = _seed_pairs(args.heldout_seed, 1)[0]
    if {
        "simulator_seed": executed_pair[0],
        "teammate_seed": executed_pair[1],
    } not in campaign_pairs:
        raise ValueError("executed seed is outside the frozen campaign")

    d4 = json.loads(args.source_d4_result.read_text(encoding="utf-8"))
    environment = _build_environment_v1(args, d4)
    manifests = panel["candidates"]
    if args.phase == "selection":
        controllers = tuple(
            _searched_controller_v1(
                controller_id=PI_STAR,
                manifest=manifest,
                runtime_binding=environment.runtime_binding,
            )
            for manifest in manifests
        )
    else:
        if args.selection_receipt is None:
            raise ValueError("confirmation requires --selection-receipt")
        receipt = _selection_payload(
            args.selection_receipt, candidate_panel=panel
        )
        if sha256_json(receipt) != args.selection_receipt_sha256:
            raise ValueError("D5 selection receipt digest differs")
        winner = receipt["frozen_candidate"]
        winner_id = str(winner["candidate_id"])
        retention = _find_anchor_v1(manifests, D4_RETENTION)
        tail = _find_anchor_v1(manifests, D5_TAIL_ONLY)
        searched_controllers = (
            _searched_controller_v1(
                controller_id=PI_STAR,
                manifest=winner,
                runtime_binding=environment.runtime_binding,
            ),
            _searched_controller_v1(
                controller_id=D4_RETENTION,
                manifest=retention,
                runtime_binding=environment.runtime_binding,
            ),
            _searched_controller_v1(
                controller_id=D5_TAIL_ONLY,
                manifest=tail,
                runtime_binding=environment.runtime_binding,
            ),
        )
        controllers = (
            *searched_controllers,
            *(
                replace(
                    environment.baseline_controllers[key],
                    candidate_id=winner_id,
                )
                for key in (CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D)
            ),
        )
        if tuple(row.controller_id for row in controllers) != (
            CONFIRMATION_CONTROLLER_IDS
        ):
            raise RuntimeError("D5 confirmation controller panel drifted")

    workers = min(args.lane_workers, len(controllers))
    jobs: list[JSONMap] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _replay_controller_v1,
                args=args,
                environment=environment,
                controller=controller,
                simulator_seed=executed_pair[0],
                teammate_seed=executed_pair[1],
            )
            for controller in controllers
        ]
        for future in as_completed(futures):
            jobs.append(future.result())
    jobs.sort(
        key=lambda row: (
            str(row["runtime_summary"]["controller_id"]),
            str(row["runtime_summary"]["candidate_id"]),
        )
    )
    return {
        "schema": OUTPUT_SCHEMA,
        "status": "D5_ONE_SEED_SHARD_COMPLETE",
        "implementation_revision": IMPLEMENTATION_REVISION,
        "phase": args.phase,
        "campaign_id": args.campaign_id,
        "candidate_panel_sha256": args.candidate_panel_sha256,
        "selection_receipt_sha256": args.selection_receipt_sha256,
        "executed_seed_pairs": [
            {
                "simulator_seed": executed_pair[0],
                "teammate_seed": executed_pair[1],
            }
        ],
        "candidate_panel_size": len(manifests),
        "fixed_horizon_ms": args.fixed_horizon_ms,
        "parallel_lane_workers": workers,
        "endpoint_rows": [row["endpoint_row"] for row in jobs],
        "runtime_summaries": [row["runtime_summary"] for row in jobs],
        "action_attributions": [
            row["action_attribution"]
            for row in jobs
            if args.phase == "confirmation"
            and row["action_attribution"] is not None
        ],
        "frozen_source_binding": d4.get("frozen_source_binding"),
        "request_sha256": environment.request_sha256,
        "dynamic_config_sha256": environment.dynamic_config_sha256,
        "evaluation_build_ref": environment.evaluation_build_ref,
        "target_rule_id": environment.target_rule_id,
        "contracts": {
            "one_seed_per_process": True,
            "candidate_generation_performed": False,
            "all_seed_terminal_endpoint": True,
            "incomplete_waves_retained": True,
            "confirmation_selection_reopened": False,
            "compact_runtime_summaries": True,
        },
    }


def main() -> int:
    parser = build_argument_parser_v1(description=__doc__)
    parser.add_argument("--phase", choices=("selection", "confirmation"), required=True)
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--candidate-panel-sha256", required=True)
    parser.add_argument("--candidate-panel", type=Path, required=True)
    parser.add_argument("--source-d4-result", type=Path, required=True)
    parser.add_argument("--selection-receipt", type=Path)
    parser.add_argument("--selection-receipt-sha256")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--campaign-first-seed", type=int, required=True)
    parser.add_argument("--campaign-seed-count", type=int, required=True)
    parser.add_argument("--fixed-horizon-ms", type=int, required=True)
    args = parser.parse_args()
    if args.heldout_seed_count != 1:
        parser.error("heldout-seed-count must be exactly one")
    if not 1 <= args.lane_workers <= 64:
        parser.error("lane-workers must be in 1..64")
    if args.fixed_horizon_ms <= 0:
        parser.error("fixed-horizon-ms must be positive")
    if args.phase == "confirmation" and args.selection_receipt is None:
        parser.error("confirmation requires --selection-receipt")
    if args.phase == "confirmation" and args.selection_receipt_sha256 is None:
        parser.error("confirmation requires --selection-receipt-sha256")
    if args.phase == "selection" and args.selection_receipt_sha256 is not None:
        parser.error("selection cannot use --selection-receipt-sha256")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
