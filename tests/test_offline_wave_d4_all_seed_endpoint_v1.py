from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.offline_wave_d2_panel_v1 import (
    CAT,
    CONTRA_DEPLOYED,
    CONTRA_NEW,
    PI_D,
)
from o2o_dps.offline_wave_d3_frozen_heldout_v1 import (
    CONTROLLER_IDS,
    PI_STAR,
    build_frozen_heldout_expansion_contract_v1,
)
from o2o_dps.offline_wave_d3_train_eval_v1 import (
    CAMPAIGN_SCHEMA,
    CANDIDATE_SCHEMA,
    HELDOUT,
    PROPOSAL,
    SELECTION,
    SELECTION_SCHEMA,
)
from o2o_dps.offline_wave_d4_all_seed_endpoint_v1 import (
    CONFIRMATORY_PAIR_COUNT,
    DIAGNOSTIC_REUSE_SEEN,
    FRESH_CONFIRMATORY,
    PRIOR_EXPANSION_SCHEMA,
    OfflineWaveD4AllSeedEndpointV1Error,
    adjudicate_all_seed_endpoint_v1,
    build_all_seed_endpoint_contract_v1,
    build_all_seed_endpoint_row_v1,
    extract_terminal_endpoint_v1,
)
from o2o_dps.offline_wave_policy_v1 import LANE_GCD
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
    SearchedWaveTargetV1,
)
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


HORIZON = 15_531
SEEN_PAIRS = ((31, 131), (32, 132))


def _program() -> SearchedWaveProgramV1:
    return SearchedWaveProgramV1(
        program_id="frozen-winner",
        parent_program_id="offline-parent",
        source_refs=("selection-receipt",),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(
            SearchedWaveStepV1(
                step_id="winner-step-0",
                kind=SearchedWaveStepKindV1.ACTION,
                at_or_after_ms=0,
                max_lateness_ms=500,
                proposal_source="frozen-selection",
                action_key="warrior.bloodthirst",
                action_ref=ActionRef(spell_id=23894),
                lane=LANE_GCD,
                target=SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT),
            ),
        ),
        tail_gcd_priority=("warrior.bloodthirst",),
        tail_queue_priority=(),
        tail_off_gcd_once=(),
    )


def _source():
    program = _program()
    frozen = {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": program.program_id,
        "proposal_arm": "combined",
        "behavior_key": program.behavior_key(),
        "program": program.to_dict(),
    }
    return {
        "schema": "development_offline_wave_policy_d900_d3/v1",
        "status": "D3_FROZEN_WINNER_HELDOUT_COMPLETE",
        "frozen_source_binding": {"component_id": "component"},
        "request_sha256": "request",
        "dynamic_config_sha256": "config",
        "evaluation_build_ref": "build",
        "target_rule_id": "target-rule",
        "campaign": {
            "schema": CAMPAIGN_SCHEMA,
            "campaign_id": "original-d3",
            "paired_seed_cohorts": {
                PROPOSAL: [{"simulator_seed": 1, "teammate_seed": 101}],
                SELECTION: [{"simulator_seed": 11, "teammate_seed": 111}],
                HELDOUT: [{"simulator_seed": 21, "teammate_seed": 121}],
            },
        },
        "selection_receipt": {
            "schema": SELECTION_SCHEMA,
            "campaign_id": "original-d3",
            "status": "ONE_CANDIDATE_FROZEN_FOR_HELDOUT",
            "frozen_candidate": frozen,
            "heldout_rows_consumed": 0,
        },
    }


def _prior_expansion():
    source = _source()
    contract = build_frozen_heldout_expansion_contract_v1(
        source=source,
        heldout_seed_pairs=SEEN_PAIRS,
        expansion_id="seen-expansion",
    )
    return {
        "schema": PRIOR_EXPANSION_SCHEMA,
        "status": "FROZEN_WINNER_BLIND_HELDOUT_PARTIAL",
        "contract": contract,
    }


def _outcome(
    *,
    residuals=(0.0, 0.0, 0.0),
    focal=(30.0, 40.0, 50.0),
    elapsed_ms=9_000,
) -> ScheduleReplayOutcomeV1:
    team_targets = []
    semantic_targets = []
    for index, (current, focal_damage) in enumerate(zip(residuals, focal)):
        initial = 100.0
        background = initial - current - focal_damage
        dead = current == 0.0
        death_time = elapsed_ms if dead else None
        team_targets.append(
            {
                "target_index": index,
                "initial_health": initial,
                "current_health": current,
                "simulated_damage_applied": focal_damage,
                "background_damage_applied": background,
                "dead": dead,
                "death_time_ms": death_time,
            }
        )
        semantic_targets.append(
            {
                "target_index": index,
                "maximum_health": initial,
                "current_health": current,
                "dead": dead,
                "attackable": not dead,
                "death_time_ms": death_time,
            }
        )
    focal_total = sum(focal)
    background_total = sum(row["background_damage_applied"] for row in team_targets)
    return ScheduleReplayOutcomeV1(
        seed=77,
        status=ReplayStatusV1.COMPLETE,
        state={
            "finished": True,
            "time_ms": elapsed_ms,
            "dynamic_team_background": {
                "simulated_damage_applied": focal_total,
                "background_damage_applied": background_total,
                "combined_damage_applied": focal_total + background_total,
                "targets": team_targets,
            },
            "dynamic_target_semantics": {"targets": semantic_targets},
        },
    )


def _diagnostic_contract():
    return build_all_seed_endpoint_contract_v1(
        frozen_d3_source=_source(),
        prior_expansion_source=_prior_expansion(),
        seed_pairs=SEEN_PAIRS,
        campaign_id="seen-diagnostic",
        horizon_ms=HORIZON,
        evidence_mode=DIAGNOSTIC_REUSE_SEEN,
    )


def _endpoint_row(controller, pair, outcome):
    terminal = extract_terminal_endpoint_v1(
        outcome, required_target_indexes=(0, 1, 2), horizon_ms=HORIZON
    )
    return build_all_seed_endpoint_row_v1(
        candidate_id="frozen-winner",
        controller_id=controller,
        simulator_seed=pair[0],
        teammate_seed=pair[1],
        replay_status="COMPLETE",
        terminal_endpoint=terminal,
        domain_fallback_calls=(0 if controller in {PI_D, PI_STAR} else None),
    )


def test_terminal_endpoint_retains_clear_and_horizon_censored_evidence():
    cleared = extract_terminal_endpoint_v1(
        _outcome(), required_target_indexes=(0, 1, 2), horizon_ms=HORIZON
    )
    assert cleared["technical_status"] == "VALID_TERMINAL_EVIDENCE"
    assert cleared["cleared_by_horizon"] is True
    assert cleared["terminal_mode"] == "ALL_REQUIRED_TARGETS_DEAD"
    assert cleared["initial_required_health"] == 300.0
    assert cleared["residual_required_health"] == 0.0
    assert cleared["focal_effective_damage_by_horizon_or_clear"] == 120.0
    assert cleared["focal_dps_if_cleared"] == pytest.approx(13.3333333333)

    censored = extract_terminal_endpoint_v1(
        _outcome(
            residuals=(10.0, 20.0, 30.0),
            focal=(20.0, 30.0, 40.0),
            elapsed_ms=HORIZON,
        ),
        required_target_indexes=(0, 1, 2),
        horizon_ms=HORIZON,
    )
    assert censored["technical_status"] == "VALID_TERMINAL_EVIDENCE"
    assert censored["cleared_by_horizon"] is False
    assert censored["terminal_mode"] == "FIXED_HORIZON_REACHED_WITH_SURVIVORS"
    assert censored["residual_required_health"] == 60.0
    assert censored["residual_required_health_fraction"] == 0.2
    assert censored["focal_effective_damage_by_horizon_or_clear"] == 90.0
    assert censored["focal_dps_if_cleared"] is None


def test_survivors_before_horizon_are_invalid_terminal_evidence():
    terminal = extract_terminal_endpoint_v1(
        _outcome(
            residuals=(10.0, 20.0, 30.0),
            focal=(20.0, 30.0, 40.0),
            elapsed_ms=HORIZON - 1,
        ),
        required_target_indexes=(0, 1, 2),
        horizon_ms=HORIZON,
    )
    assert terminal["technical_status"] == "INVALID_TERMINAL_EVIDENCE"
    assert "exact fixed-horizon terminal" in terminal["failure_reason"]


def test_contract_separates_seen_diagnostic_from_fresh_confirmation():
    diagnostic = _diagnostic_contract()
    assert diagnostic["evidence_mode"] == DIAGNOSTIC_REUSE_SEEN
    assert diagnostic["endpoint_spec"]["model_dominance_rule"][
        "weighted_scalar_score"
    ] is False

    fresh_pairs = tuple(
        (1000 + index, 2000 + index)
        for index in range(CONFIRMATORY_PAIR_COUNT)
    )
    fresh = build_all_seed_endpoint_contract_v1(
        frozen_d3_source=_source(),
        prior_expansion_source=_prior_expansion(),
        seed_pairs=fresh_pairs,
        campaign_id="fresh-confirmation",
        horizon_ms=HORIZON,
        evidence_mode=FRESH_CONFIRMATORY,
    )
    assert fresh["evidence_mode"] == FRESH_CONFIRMATORY
    assert len(fresh["seed_pairs"]) == CONFIRMATORY_PAIR_COUNT

    with pytest.raises(OfflineWaveD4AllSeedEndpointV1Error, match="reuses"):
        build_all_seed_endpoint_contract_v1(
            frozen_d3_source=_source(),
            prior_expansion_source=_prior_expansion(),
            seed_pairs=(SEEN_PAIRS[0],)
            + tuple((3000 + index, 4000 + index) for index in range(47)),
            campaign_id="leaking-confirmation",
            horizon_ms=HORIZON,
            evidence_mode=FRESH_CONFIRMATORY,
        )


def test_all_seed_adjudicator_keeps_nonclears_and_uses_pareto_dominance():
    rows = []
    for pair_index, pair in enumerate(SEEN_PAIRS):
        for controller in CONTROLLER_IDS:
            if pair_index == 0:
                outcome = _outcome(
                    focal=(40.0, 40.0, 40.0)
                    if controller == PI_STAR
                    else (30.0, 30.0, 30.0)
                )
            else:
                outcome = _outcome(
                    residuals=(10.0, 10.0, 10.0)
                    if controller == PI_STAR
                    else (20.0, 20.0, 20.0),
                    focal=(30.0, 30.0, 30.0)
                    if controller == PI_STAR
                    else (20.0, 20.0, 20.0),
                    elapsed_ms=HORIZON,
                )
            rows.append(_endpoint_row(controller, pair, outcome))

    receipt = adjudicate_all_seed_endpoint_v1(
        contract=_diagnostic_contract(), rows=rows
    )
    assert receipt["status"] == "SEEN_SEED_ENDPOINT_DIAGNOSTIC_COMPLETE"
    assert receipt["all_five_controllers_valid_seed_count"] == 2
    assert receipt["per_controller"][PI_STAR]["technically_valid_seed_count"] == 2
    assert receipt["per_controller"][PI_STAR]["clear_count"] == 1
    assert receipt["per_controller"][PI_STAR]["completion_rate"] == 0.5
    comparison = receipt["pi_star_pairwise_by_baseline"][CAT]
    assert comparison["technically_valid_paired_seed_count"] == 2
    assert comparison["both_clear_count"] == 1
    assert comparison["neither_clear_count"] == 1
    assert comparison["mean_residual_health_fraction_delta"] == -0.05
    assert comparison["mean_focal_effective_damage_delta"] == 30.0
    assert comparison["model_dominance"] == "PI_STAR_MODEL_DOMINATES"
    assert receipt["confirmatory_endpoint_complete"] is False
    assert receipt["development_model_superiority_status"] == "NOT_ESTABLISHED"


def test_nonzero_fallback_makes_pi_star_row_ineligible_without_zero_imputation():
    endpoint = extract_terminal_endpoint_v1(
        _outcome(), required_target_indexes=(0, 1, 2), horizon_ms=HORIZON
    )
    row = build_all_seed_endpoint_row_v1(
        candidate_id="frozen-winner",
        controller_id=PI_STAR,
        simulator_seed=31,
        teammate_seed=131,
        replay_status="COMPLETE",
        terminal_endpoint=endpoint,
        domain_fallback_calls=1,
    )
    assert row["all_seed_endpoint_eligible"] is False
    assert row["failure_reason"] == "domain fallback calls=1"
    assert row["terminal_endpoint"][
        "focal_effective_damage_by_horizon_or_clear"
    ] == 120.0
    assert row["incomplete_damage_imputed_as_zero"] is False


def test_adjudicator_rejects_terminal_horizon_drift():
    rows = [
        _endpoint_row(controller, pair, _outcome())
        for pair in SEEN_PAIRS
        for controller in CONTROLLER_IDS
    ]
    rows[0] = deepcopy(rows[0])
    rows[0]["terminal_endpoint"]["fixed_horizon_ms"] += 1
    with pytest.raises(OfflineWaveD4AllSeedEndpointV1Error, match="different horizon"):
        adjudicate_all_seed_endpoint_v1(
            contract=_diagnostic_contract(), rows=rows
        )


def _remote_shard(plan, spec, contract):
    endpoint_rows = [
        _endpoint_row(controller, spec.seed_pairs[0], _outcome())
        for controller in CONTROLLER_IDS
    ]
    source_rows = [
        {
            "controller_id": controller,
            "wall_seconds": 10.0 + index,
            "searched_action_attribution": (
                {"accepted_action_count": 3} if controller == PI_STAR else None
            ),
        }
        for index, controller in enumerate(CONTROLLER_IDS)
    ]
    source = _source()
    return {
        "schema": "development_offline_wave_policy_d900_d4_all_seed_shard/v1",
        "status": "ALL_SEED_ENDPOINT_SHARD_COMPLETE",
        "contract": contract,
        "executed_seed_pairs": [
            {
                "simulator_seed": spec.seed_pairs[0][0],
                "teammate_seed": spec.seed_pairs[0][1],
            }
        ],
        "endpoint_rows": endpoint_rows,
        "source_runtime_rows": source_rows,
        "frozen_source_binding": source["frozen_source_binding"],
        "request_sha256": source["request_sha256"],
        "dynamic_config_sha256": source["dynamic_config_sha256"],
        "evaluation_build_ref": source["evaluation_build_ref"],
        "target_rule_id": source["target_rule_id"],
        "required_target_indexes": [0, 1, 2],
        "fixed_horizon_ms": HORIZON,
    }


def test_remote_plan_is_one_seed_per_shard_and_new_directory_per_campaign():
    from scripts.development_offline_wave_policy_d900_d4_all_seed_remote_v1 import (
        CONFIRMATORY_PLAN,
        DIAGNOSTIC_PLAN,
        NODES,
        build_node_batch_command_v1,
        build_seed_job_argv_v1,
        build_seed_job_specs_v1,
    )

    diagnostic = build_seed_job_specs_v1(DIAGNOSTIC_PLAN)
    confirmation = build_seed_job_specs_v1(CONFIRMATORY_PLAN)
    assert len(diagnostic) == 7
    assert len(confirmation) == 48
    assert all(len(spec.seed_pairs) == 1 for spec in confirmation)
    assert {
        node: sum(spec.node == node for spec in confirmation) for node in NODES
    } == {node: 12 for node in NODES}
    assert set(CONFIRMATORY_PLAN.seed_pairs).isdisjoint(set(SEEN_PAIRS))
    assert CONFIRMATORY_PLAN.remote_result_dir != DIAGNOSTIC_PLAN.remote_result_dir

    argv = build_seed_job_argv_v1(CONFIRMATORY_PLAN, confirmation[0])
    assert argv[argv.index("--heldout-seed-count") + 1] == "1"
    assert argv[argv.index("--campaign-seed-count") + 1] == "48"
    assert argv[argv.index("--evidence-mode") + 1] == FRESH_CONFIRMATORY
    node_specs = tuple(spec for spec in confirmation if spec.node == NODES[0])
    command = build_node_batch_command_v1(CONFIRMATORY_PLAN, node_specs)
    assert command.count(" & pids=\"$pids $!\"") == 12
    assert command.count("if test ! -s ") == 12


def test_remote_merge_validates_global_contract_and_keeps_all_48_seed_rows():
    from scripts.development_offline_wave_policy_d900_d4_all_seed_remote_v1 import (
        CONFIRMATORY_PLAN,
        FIXED_HORIZON_MS,
        build_seed_job_specs_v1,
        merge_seed_job_results_v1,
    )

    specs = build_seed_job_specs_v1(CONFIRMATORY_PLAN)
    contract = build_all_seed_endpoint_contract_v1(
        frozen_d3_source=_source(),
        prior_expansion_source=_prior_expansion(),
        seed_pairs=CONFIRMATORY_PLAN.seed_pairs,
        campaign_id=CONFIRMATORY_PLAN.campaign_id,
        horizon_ms=FIXED_HORIZON_MS,
        evidence_mode=CONFIRMATORY_PLAN.evidence_mode,
    )
    shards = {
        spec.job_id: _remote_shard(CONFIRMATORY_PLAN, spec, contract)
        for spec in specs
    }
    merged = merge_seed_job_results_v1(
        plan=CONFIRMATORY_PLAN,
        frozen_source=_source(),
        prior_expansion_source=_prior_expansion(),
        specs=specs,
        seed_job_results=shards,
    )
    assert merged["status"] == "FRESH_ALL_SEED_ENDPOINT_CONFIRMATION_COMPLETE"
    assert merged["receipt"]["requested_seed_count"] == 48
    assert merged["receipt"]["all_five_controllers_valid_seed_count"] == 48
    assert merged["receipt"]["confirmatory_endpoint_complete"] is True
    assert len(merged["endpoint_rows"]) == 240
    assert len(merged["pi_star_action_attribution_by_seed"]) == 48

    bad = deepcopy(shards)
    target = specs[3].job_id
    bad[target] = deepcopy(bad[target])
    bad[target]["contract"]["endpoint_spec"]["implementation_revision"] = "old"
    with pytest.raises(ValueError, match="exact global endpoint contract"):
        merge_seed_job_results_v1(
            plan=CONFIRMATORY_PLAN,
            frozen_source=_source(),
            prior_expansion_source=_prior_expansion(),
            specs=specs,
            seed_job_results=bad,
        )


def test_remote_node_batch_retries_only_transport_failure():
    from scripts.development_offline_wave_policy_d900_d4_all_seed_remote_v1 import (
        CONFIRMATORY_PLAN,
        _run_node_batch_v1,
        build_seed_job_specs_v1,
    )

    class FakeScheduler:
        def __init__(self, return_codes):
            self.return_codes = list(return_codes)
            self.calls = 0

        def run_on(self, *_args, **_kwargs):
            self.calls += 1
            return self.return_codes.pop(0), "", "failed"

    spec = build_seed_job_specs_v1(CONFIRMATORY_PLAN)[0]
    transport = FakeScheduler([255, 255, 0])
    _run_node_batch_v1(
        transport, CONFIRMATORY_PLAN, spec.node, (spec,)
    )
    assert transport.calls == 3

    application = FakeScheduler([1])
    with pytest.raises(RuntimeError, match="rc=1"):
        _run_node_batch_v1(
            application, CONFIRMATORY_PLAN, spec.node, (spec,)
        )
    assert application.calls == 1
