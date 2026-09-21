"""Run the first bounded D3 searched-policy experiment on d900 wave 1.

The experiment uses one frozen searched program across every seed.  Candidate
generation receives only accepted proposal-cohort actions, selection and
held-out paired seeds are disjoint, and incomplete waves retain null metrics.
This remains development-model evidence, not a real-raid deployment result.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

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
    summarize_d2_lane_outcome_v1,
)
from o2o_dps.offline_wave_d3_candidate_panel_v1 import (
    build_d3_candidate_panel_v1,
)
from o2o_dps.offline_wave_d3_train_eval_v1 import (
    HELDOUT,
    SEARCH_ARMS,
    SELECTION,
    build_d3_campaign_contract_v1,
    build_d3_full_wave_row_v1,
    evaluate_d3_frozen_candidate_heldout_v1,
    select_d3_frozen_candidate_v1,
    validate_d3_candidate_manifests_v1,
)
from o2o_dps.offline_wave_execution_trace_v1 import (
    project_offline_wave_execution_trace_v1,
)
from o2o_dps.offline_wave_policy_v1 import (
    build_offline_wave_policy_runtime_v1,
)
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
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1
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


SCHEMA = "development_offline_wave_policy_d900_d3/v1"
PI_STAR = "PI_STAR"
TERMINAL_DAMAGE_REFS = (
    ActionRef(spell_id=20_569),
    ActionRef(spell_id=25_286),
    ActionRef(other_id=7, tag=1),
    ActionRef(other_id=7, tag=2),
)


@dataclass(frozen=True)
class _ControllerV1:
    controller_id: str
    program: CausalActionProgramV1
    binding: ImportedReactiveProgramBindingV1
    session_kind: str


def _imported_program(
    binding: ImportedReactiveProgramBindingV1,
) -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id=f"d900-d3::{binding.source_policy_id}",
        selector=ImportedReactiveSelectorV1(
            binding.binding_id,
            binding.source_policy_id,
            OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
        source_refs=(binding.source_policy_id, SCHEMA),
    )


def _seed_pairs(start: int, count: int) -> tuple[tuple[int, int], ...]:
    return tuple((start + index, start + 100_000 + index) for index in range(count))


def _compact_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for block in trace.get("blocks", []):
        target = block.get("accepted_target")
        for action in block.get("guide_actions", []):
            actions.append(
                {
                    "decision_index": block.get("decision_index"),
                    "source_policy_id": block.get("source_policy_id"),
                    "policy_target_index": (
                        target.get("policy_target_index")
                        if isinstance(target, Mapping)
                        else None
                    ),
                    "lane": action.get("lane"),
                    "action": action.get("action"),
                    "state_time_ms": action.get("state_time_ms"),
                }
            )
    return {
        "schema": trace.get("schema"),
        "seed": trace.get("seed"),
        "replay_status": trace.get("replay_status"),
        "source_policy_ids": trace.get("source_policy_ids"),
        "accepted_action_count": trace.get("accepted_action_count"),
        "guide_actions": actions,
    }


def _compact_audit(session: Any | None) -> dict[str, Any] | None:
    if session is None or not hasattr(session, "audit_events"):
        return None
    rows = session.audit_events
    retained = [
        dict(row)
        for row in rows
        if str(row.get("kind", "")).startswith("SEARCHED_STEP_")
        or row.get("kind")
        in {
            "SEARCHED_PROPOSAL_ACTION_NOT_EXECUTED",
            "SEARCHED_DOMAIN_FALLBACK",
        }
    ]
    return {
        "event_kind_counts": dict(
            sorted(Counter(str(row.get("kind")) for row in rows).items())
        ),
        "step_events": retained,
    }


def _mean_or_none(values: Sequence[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def run(args: argparse.Namespace) -> dict[str, Any]:
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
    contra = bind_contra_incantagos_v4_policy_v1(
        fixed, case, metadata, **shared
    )
    build_evidence = cat.receipt["equipment_request_provenance"]
    if not (
        build_evidence.get("observed_equipment_id_match") is True
        and build_evidence.get("observed_talents_string_match") is True
    ):
        raise RuntimeError("D3 exact historical equipment/talents binding failed")
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
    baselines = (
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
    )

    def replay_controller(
        controller: _ControllerV1,
        simulator_seed: int,
        teammate_seed: int,
        *,
        cohort: str,
        candidate_id: str | None = None,
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

        binding = replace(
            controller.binding,
            resolver_factory=open_tracked_session,
        )

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
            failure_reason = f"searched/offline domain fallback calls={fallback_calls}"
        completed = score_status == "COMPLETED"
        trace = project_offline_wave_execution_trace_v1(outcome)
        compact_trace = _compact_trace(trace)
        row = {
            "controller_id": controller.controller_id,
            "candidate_id": candidate_id,
            "cohort": cohort,
            "simulator_seed": simulator_seed,
            "teammate_seed": teammate_seed,
            "replay_status": (
                outcome.status.value
                if isinstance(outcome.status, ReplayStatusV1)
                else str(outcome.status)
            ),
            "score_status": score_status,
            "failure_reason": failure_reason,
            "required_targets_dead": raw["required_targets_dead"],
            "effective_damage": raw["effective_damage"] if completed else None,
            "dps": raw["dps"] if completed else None,
            "completion_time_ms": (
                raw["completion_time_ms"] if completed else None
            ),
            "terminal_resource": raw["terminal_resource"],
            "terminal_cooldowns": raw["terminal_cooldowns"],
            "accepted_action_receipt_count": raw[
                "accepted_action_receipt_count"
            ],
            "domain_fallback_calls": fallback_calls,
            "wall_seconds": wall_seconds,
            "accepted_execution_trace": compact_trace,
            "runtime_audit": _compact_audit(session),
        }
        if candidate_id is not None:
            row["contract_row"] = build_d3_full_wave_row_v1(
                candidate_id=candidate_id,
                cohort=cohort,
                simulator_seed=simulator_seed,
                teammate_seed=teammate_seed,
                replay_status=row["replay_status"],
                score_status=score_status,
                required_targets_dead=row["required_targets_dead"],
                effective_damage=row["effective_damage"],
                dps=row["dps"],
                completion_time_ms=row["completion_time_ms"],
                failure_reason=failure_reason,
            )
        return row

    proposal_pairs = _seed_pairs(args.proposal_seed, args.proposal_seed_count)
    selection_pairs = _seed_pairs(args.selection_seed, args.selection_seed_count)
    heldout_pairs = _seed_pairs(args.heldout_seed, args.heldout_seed_count)
    campaign = build_d3_campaign_contract_v1(
        campaign_id="d900-d3-bounded-search-v1",
        proposal_seed_pairs=proposal_pairs,
        selection_seed_pairs=selection_pairs,
        heldout_seed_pairs=heldout_pairs,
        proposal_budget_by_arm={
            arm: args.proposal_budget for arm in SEARCH_ARMS
        },
    )

    # The accepted π_D proposal trace is the offline donor.  A source proposal
    # receipt alone is never passed into candidate generation.
    proposal_rows = [
        replay_controller(
            baselines[-1], simulator_seed, teammate_seed, cohort="proposal"
        )
        for simulator_seed, teammate_seed in proposal_pairs
    ]
    eligible_donor_rows = [
        row
        for row in proposal_rows
        if row["score_status"] == "COMPLETED"
        and row["domain_fallback_calls"] in {None, 0}
        and row["accepted_execution_trace"]["accepted_action_count"] > 0
    ]
    if not eligible_donor_rows:
        raise RuntimeError(
            "D3 has no complete, fallback-free proposal trace with accepted actions"
        )
    donor_row = max(
        eligible_donor_rows,
        key=lambda row: row["accepted_execution_trace"][
            "accepted_action_count"
        ],
    )
    # One accepted trace is one ordered guide.  Concatenating multiple fresh
    # seeds would reset state_time_ms and invent a sequence that never ran.
    accepted_offline_guides = list(
        donor_row["accepted_execution_trace"]["guide_actions"]
    )
    manifests = validate_d3_candidate_manifests_v1(
        build_d3_candidate_panel_v1(
            offline_policy,
            proposal_budget_per_arm=args.proposal_budget,
            offline_guide_actions=accepted_offline_guides,
        )
    )
    searched_controllers: dict[str, _ControllerV1] = {}
    for manifest in manifests:
        searched = searched_wave_program_from_dict_v1(manifest["program"])
        program, binding = build_searched_wave_program_runtime_v1(
            searched,
            runtime_binding,
            domain_fallback_factory=_cat_resolver_bomb_factory,
        )
        searched_controllers[manifest["candidate_id"]] = _ControllerV1(
            PI_STAR, program, binding, "searched"
        )

    selection_jobs = [
        (manifest["candidate_id"], simulator_seed, teammate_seed)
        for manifest in manifests
        for simulator_seed, teammate_seed in selection_pairs
    ]
    selection_runtime_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=min(args.lane_workers, len(selection_jobs))
    ) as pool:
        futures = {
            pool.submit(
                replay_controller,
                searched_controllers[candidate_id],
                simulator_seed,
                teammate_seed,
                cohort=SELECTION,
                candidate_id=candidate_id,
            ): (candidate_id, simulator_seed, teammate_seed)
            for candidate_id, simulator_seed, teammate_seed in selection_jobs
        }
        for future in as_completed(futures):
            selection_runtime_rows.append(future.result())
    selection_runtime_rows.sort(
        key=lambda row: (
            str(row["candidate_id"]),
            row["simulator_seed"],
            row["teammate_seed"],
        )
    )
    selection_receipt = select_d3_frozen_candidate_v1(
        campaign=campaign,
        candidates=manifests,
        selection_rows=[row["contract_row"] for row in selection_runtime_rows],
        completed_proposal_trials_by_arm={
            arm: args.proposal_budget for arm in SEARCH_ARMS
        },
    )

    heldout_runtime_rows: list[dict[str, Any]] = []
    heldout_receipt: dict[str, Any] | None = None
    frozen_candidate = selection_receipt.get("frozen_candidate")
    if isinstance(frozen_candidate, Mapping):
        candidate_id = str(frozen_candidate["candidate_id"])
        heldout_jobs = [
            (candidate_id, simulator_seed, teammate_seed)
            for simulator_seed, teammate_seed in heldout_pairs
        ]
        with ThreadPoolExecutor(
            max_workers=min(args.lane_workers, len(heldout_jobs))
        ) as pool:
            futures = [
                pool.submit(
                    replay_controller,
                    searched_controllers[candidate_id],
                    simulator_seed,
                    teammate_seed,
                    cohort=HELDOUT,
                    candidate_id=candidate_id,
                )
                for _, simulator_seed, teammate_seed in heldout_jobs
            ]
            heldout_runtime_rows = [future.result() for future in futures]
        heldout_runtime_rows.sort(
            key=lambda row: (row["simulator_seed"], row["teammate_seed"])
        )
        heldout_receipt = evaluate_d3_frozen_candidate_heldout_v1(
            campaign=campaign,
            selection_receipt=selection_receipt,
            heldout_rows=[row["contract_row"] for row in heldout_runtime_rows],
        )

    # Baselines are descriptive paired comparators on the same new cohorts;
    # they never participate in winner selection.
    comparison_pairs = tuple((*selection_pairs, *heldout_pairs))
    baseline_jobs = [
        (controller, simulator_seed, teammate_seed)
        for controller in baselines
        for simulator_seed, teammate_seed in comparison_pairs
    ]
    baseline_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=min(args.lane_workers, len(baseline_jobs))
    ) as pool:
        futures = [
            pool.submit(
                replay_controller,
                controller,
                simulator_seed,
                teammate_seed,
                cohort=(
                    SELECTION
                    if (simulator_seed, teammate_seed) in selection_pairs
                    else HELDOUT
                ),
            )
            for controller, simulator_seed, teammate_seed in baseline_jobs
        ]
        baseline_rows = [future.result() for future in futures]
    baseline_rows.sort(
        key=lambda row: (
            row["cohort"],
            row["simulator_seed"],
            row["controller_id"],
        )
    )

    selected_rows = [*selection_runtime_rows, *heldout_runtime_rows]
    frozen_id = (
        frozen_candidate.get("candidate_id")
        if isinstance(frozen_candidate, Mapping)
        else None
    )
    selected_rows = [
        row for row in selected_rows if row["candidate_id"] == frozen_id
    ]
    comparable = [
        *baseline_rows,
        *(
            {**row, "controller_id": PI_STAR}
            for row in selected_rows
        ),
    ]
    by_pair: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in comparable:
        by_pair[(row["cohort"], row["simulator_seed"], row["teammate_seed"])].append(row)
    required_ids = {CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D, PI_STAR}
    valid_pairs = [
        (key, rows)
        for key, rows in sorted(by_pair.items())
        if {row["controller_id"] for row in rows} == required_ids
        and all(row["score_status"] == "COMPLETED" for row in rows)
    ]
    aggregates: dict[str, Any] = {}
    for controller_id in sorted(required_ids):
        rows = [
            row
            for _, members in valid_pairs
            for row in members
            if row["controller_id"] == controller_id
        ]
        aggregates[controller_id] = {
            "valid_paired_seed_count": len(rows),
            "mean_dps": _mean_or_none([float(row["dps"]) for row in rows]),
            "mean_effective_damage": _mean_or_none(
                [float(row["effective_damage"]) for row in rows]
            ),
            "mean_completion_time_ms": _mean_or_none(
                [float(row["completion_time_ms"]) for row in rows]
            ),
        }

    status = (
        "D3_FROZEN_WINNER_HELDOUT_COMPLETE"
        if heldout_receipt is not None
        and heldout_receipt.get("status") == "FROZEN_CANDIDATE_HELDOUT_COMPLETE"
        else "D3_NO_HELDOUT_COMPLETE_WINNER"
    )
    return {
        "schema": SCHEMA,
        "status": status,
        "campaign": campaign,
        "instance_id": INSTANCE_ID,
        "encounter_id": ENCOUNTER_ID,
        "wave_id": args.wave_id,
        "focal_player_guid": FOCAL_GUID,
        "frozen_source_binding": frozen_source,
        "request_sha256": request_sha,
        "dynamic_config_sha256": config_sha,
        "evaluation_build_ref": evaluation_build_ref,
        "target_rule_id": target_rule_id,
        "proposal_offline_donor_runs": proposal_rows,
        "selected_offline_donor_seed_pair": {
            "simulator_seed": donor_row["simulator_seed"],
            "teammate_seed": donor_row["teammate_seed"],
        },
        "accepted_offline_guide_action_count": len(accepted_offline_guides),
        "candidate_manifests": manifests,
        "selection_receipt": selection_receipt,
        "selection_runtime_rows": selection_runtime_rows,
        "heldout_receipt": heldout_receipt,
        "heldout_runtime_rows": heldout_runtime_rows,
        "baseline_rows": baseline_rows,
        "paired_valid_pair_count": len(valid_pairs),
        "paired_aggregate_valid_pairs_only": aggregates,
        "parallel_lane_workers": min(
            args.lane_workers,
            max(len(selection_jobs), len(baseline_jobs)),
        ),
        "contracts": {
            "one_program_per_candidate_across_all_seeds": True,
            "proposal_selection_heldout_seed_cohorts_disjoint": True,
            "heldout_can_reselect_winner": False,
            "selection_seed_previously_screened_for_baseline_completion": bool(
                args.development_screened_selection_seed
            ),
            "incomplete_imputed_as_zero": False,
            "domain_fallback_required_zero_for_candidate_score": True,
            "baseline_rows_participate_in_selection": False,
            "full_d3_parent_union_complete": False,
            "implemented_parent_scope": (
                "PI_D_PARENT_PLUS_PLUGIN_MECHANISM_AND_ACCEPTED_OFFLINE_DONORS"
            ),
            "target_hp_armor_attackability_are_model_hypotheses": True,
            "responsive_team_is_model_not_historical_replay": True,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


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
    parser.add_argument("--proposal-seed", type=int, default=2026092001)
    parser.add_argument("--proposal-seed-count", type=int, default=1)
    parser.add_argument("--selection-seed", type=int, default=2026092101)
    parser.add_argument("--selection-seed-count", type=int, default=2)
    parser.add_argument("--heldout-seed", type=int, default=2026092201)
    parser.add_argument("--heldout-seed-count", type=int, default=2)
    parser.add_argument("--proposal-budget", type=int, default=4)
    parser.add_argument("--max-decisions", type=int, default=1000)
    parser.add_argument("--lane-workers", type=int, default=12)
    parser.add_argument("--route-focus", action="store_true")
    parser.add_argument("--development-screened-selection-seed", action="store_true")
    parser.add_argument(
        "--attackability-mode",
        choices=ATTACKABILITY_MODES,
        default=OBSERVED_ONSET_UNTIL_SIM_DEATH,
    )
    args = parser.parse_args()
    for name in (
        "proposal_seed_count",
        "selection_seed_count",
        "heldout_seed_count",
    ):
        if not 1 <= getattr(args, name) <= 8:
            parser.error(f"{name.replace('_', '-')} must be in 1..8")
    if not 1 <= args.proposal_budget <= 32:
        parser.error("proposal-budget must be in 1..32")
    if not 1 <= args.lane_workers <= 64:
        parser.error("lane-workers must be in 1..64")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
