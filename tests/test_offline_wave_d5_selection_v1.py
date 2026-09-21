from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.offline_wave_d2_panel_v1 import (
    CAT,
    CONTRA_DEPLOYED,
    CONTRA_NEW,
    PI_D,
)
from o2o_dps.offline_wave_d3_action_attribution_v1 import (
    CONTROLLER_WAIT,
    EXPLICIT_ACTION,
    EXPLICIT_WAIT,
    OTHER,
    SCHEMA as ATTRIBUTION_SCHEMA,
    TAIL_FILL,
)
from o2o_dps.offline_wave_d3_frozen_heldout_v1 import PI_STAR
from o2o_dps.offline_wave_d4_all_seed_endpoint_v1 import (
    TERMINAL_SCHEMA,
    build_all_seed_endpoint_row_v1,
)
from o2o_dps.offline_wave_d5_selection_v1 import (
    CANDIDATE_SCHEMA,
    CONFIRMATION_CONTROLLER_IDS,
    D4_RETENTION,
    D5_TAIL_ONLY,
    HELDOUT,
    RETENTION,
    ROW_SCHEMA,
    SEARCHED,
    SELECTION,
    TAIL_ONLY,
    OfflineWaveD5SelectionV1Error,
    adjudicate_d5_confirmation_v1,
    build_d5_confirmation_endpoint_row_v1,
    build_d5_selection_contract_v1,
    build_d5_selection_row_v1,
    evaluate_d5_frozen_candidate_heldout_v1,
    select_d5_candidate_v1,
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


HORIZON = 15_531
D4_PAIRS = ((2026101049, 2026201049), (2026101050, 2026201050))
SELECTION_PAIRS = ((3001, 4001), (3002, 4002))
HELDOUT_PAIRS = ((5001, 6001), (5002, 6002))
RETENTION_ID = "d5-parent-retention"
TAIL_ID = "d5-tail-only-ablation"


def _program(program_id: str, *, at_ms: int, lateness_ms: int) -> SearchedWaveProgramV1:
    return SearchedWaveProgramV1(
        program_id=program_id,
        parent_program_id=None,
        source_refs=("test",),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(
            SearchedWaveStepV1(
                step_id=f"{program_id}-step",
                kind=SearchedWaveStepKindV1.ACTION,
                at_or_after_ms=at_ms,
                max_lateness_ms=lateness_ms,
                proposal_source="test",
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


def _manifest(candidate_id: str, role: str, program: SearchedWaveProgramV1):
    return {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "role": role,
        "proposal_arm": role,
        "behavior_key": program.behavior_key(),
        "program": program.to_dict(),
    }


def _candidates(*, include_tie_clone: bool = False):
    retention = _program(RETENTION_ID, at_ms=0, lateness_ms=0)
    tail = _program(TAIL_ID, at_ms=HORIZON + 1, lateness_ms=0)
    searched_a = _program("searched-a", at_ms=0, lateness_ms=250)
    searched_b = _program("searched-b", at_ms=0, lateness_ms=500)
    rows = [
        _manifest(RETENTION_ID, RETENTION, retention),
        _manifest(TAIL_ID, TAIL_ONLY, tail),
        _manifest("searched-a", SEARCHED, searched_a),
        _manifest("searched-b", SEARCHED, searched_b),
    ]
    if include_tie_clone:
        rows.append(_manifest("searched-a-clone", SEARCHED, searched_a))
    return rows


def _contract(*, include_tie_clone: bool = False):
    return build_d5_selection_contract_v1(
        campaign_id="d5-test",
        candidates=_candidates(include_tie_clone=include_tie_clone),
        retention_candidate_id=RETENTION_ID,
        tail_only_candidate_id=TAIL_ID,
        selection_seed_pairs=SELECTION_PAIRS,
        heldout_seed_pairs=HELDOUT_PAIRS,
        d4_confirmation_seed_pairs=D4_PAIRS,
        horizon_ms=HORIZON,
    )


def _terminal(*, residual_fraction: float, focal_damage: float):
    initial = 100.0
    residual = residual_fraction * initial
    background = initial - residual - focal_damage
    assert background >= 0
    cleared = residual == 0.0
    elapsed = 10_000 if cleared else HORIZON
    return {
        "schema": TERMINAL_SCHEMA,
        "technical_status": "VALID_TERMINAL_EVIDENCE",
        "failure_reason": None,
        "fixed_horizon_ms": HORIZON,
        "terminal_mode": (
            "ALL_REQUIRED_TARGETS_DEAD"
            if cleared
            else "FIXED_HORIZON_REACHED_WITH_SURVIVORS"
        ),
        "terminal_elapsed_ms": elapsed,
        "cleared_by_horizon": cleared,
        "initial_required_health": initial,
        "residual_required_health": residual,
        "residual_required_health_fraction": residual_fraction,
        "focal_effective_damage_by_horizon_or_clear": focal_damage,
        "background_effective_damage_by_horizon_or_clear": background,
        "combined_effective_damage_by_horizon_or_clear": focal_damage + background,
        "focal_dps_if_cleared": (
            focal_damage * 1000.0 / elapsed if cleared else None
        ),
        "target_outcomes": [
            {
                "target_index": 0,
                "maximum_health": initial,
                "initial_health": initial,
                "current_health": residual,
                "simulated_damage_applied": focal_damage,
                "background_damage_applied": background,
                "dead": cleared,
                "death_time_ms": elapsed if cleared else None,
            }
        ],
    }


def _attribution(program: SearchedWaveProgramV1, status: str = "NEVER_SELECTED", tail=2):
    sources = (EXPLICIT_ACTION, TAIL_FILL, EXPLICIT_WAIT, CONTROLLER_WAIT, OTHER)
    action_counts = {source: 0 for source in sources}
    action_counts[TAIL_FILL] = tail
    accepted_action_ids = []
    missed_ids = []
    skipped_ids = []
    selected_unaccepted_ids = []
    never_selected_ids = []
    step_id = program.steps[0].step_id
    if status == "ACCEPTED":
        accepted_action_ids.append(step_id)
        action_counts[EXPLICIT_ACTION] = 1
    elif status == "MISSED":
        missed_ids.append(step_id)
    elif status == "SKIPPED":
        skipped_ids.append(step_id)
    elif status == "SELECTED_BUT_UNACCEPTED":
        selected_unaccepted_ids.append(step_id)
    else:
        never_selected_ids.append(step_id)
    return {
        "schema": ATTRIBUTION_SCHEMA,
        "accepted_action_count": sum(action_counts.values()),
        "accepted_action_count_by_source": action_counts,
        "accepted_wait_count": 0,
        "accepted_wait_count_by_source": {source: 0 for source in sources},
        "explicit_action_step_count": 1,
        "accepted_explicit_action_step_count": len(accepted_action_ids),
        "accepted_explicit_action_step_ids": accepted_action_ids,
        "explicit_wait_step_count": 0,
        "accepted_explicit_wait_step_count": 0,
        "accepted_explicit_wait_step_ids": [],
        "missed_explicit_step_ids": missed_ids,
        "skipped_explicit_step_ids": skipped_ids,
        "selected_but_unaccepted_explicit_step_ids": selected_unaccepted_ids,
        "never_selected_explicit_step_ids": never_selected_ids,
    }


def _program_from_manifest(manifest):
    from o2o_dps.offline_wave_searched_program_v1 import (
        searched_wave_program_from_dict_v1,
    )

    return searched_wave_program_from_dict_v1(manifest["program"])


def _row(
    manifest,
    pair,
    *,
    cohort=SELECTION,
    residual_fraction=0.1,
    focal_damage=50.0,
    attribution_status="NEVER_SELECTED",
    tail=2,
):
    endpoint = build_all_seed_endpoint_row_v1(
        candidate_id=manifest["candidate_id"],
        controller_id=PI_STAR,
        simulator_seed=pair[0],
        teammate_seed=pair[1],
        replay_status="COMPLETE",
        terminal_endpoint=_terminal(
            residual_fraction=residual_fraction, focal_damage=focal_damage
        ),
        domain_fallback_calls=0,
    )
    program = _program_from_manifest(manifest)
    return build_d5_selection_row_v1(
        cohort=cohort,
        endpoint_row=endpoint,
        action_attribution=_attribution(
            program, status=attribution_status, tail=tail
        ),
    )


def _selection_rows(contract, metrics):
    result = []
    for manifest in contract["candidate_manifests"]:
        residual, focal, status, tail = metrics[manifest["candidate_id"]]
        for pair in SELECTION_PAIRS:
            result.append(
                _row(
                    manifest,
                    pair,
                    residual_fraction=residual,
                    focal_damage=focal,
                    attribution_status=status,
                    tail=tail,
                )
            )
    return result


@pytest.mark.parametrize(
    ("retention_residual", "candidate_residuals"),
    [
        (0.0, (0.0, 0.1)),  # completion falls from 1.0 to 0.5
        (0.1, (0.2, 0.2)),  # equal completion, worse residual health
    ],
)
def test_higher_focal_damage_is_rejected_when_clear_or_residual_gate_degrades(
    retention_residual, candidate_residuals
):
    contract = _contract()
    manifests = {row["candidate_id"]: row for row in contract["candidate_manifests"]}
    rows = []
    for pair_index, pair in enumerate(SELECTION_PAIRS):
        rows.extend(
            [
                _row(
                    manifests[RETENTION_ID],
                    pair,
                    residual_fraction=retention_residual,
                    focal_damage=40.0,
                ),
                _row(
                    manifests[TAIL_ID],
                    pair,
                    residual_fraction=0.3,
                    focal_damage=20.0,
                ),
                _row(
                    manifests["searched-a"],
                    pair,
                    residual_fraction=candidate_residuals[pair_index],
                    focal_damage=70.0,
                    attribution_status="MISSED",
                ),
                _row(
                    manifests["searched-b"],
                    pair,
                    residual_fraction=0.3,
                    focal_damage=20.0,
                ),
            ]
        )
    receipt = select_d5_candidate_v1(contract=contract, selection_rows=rows)
    assert receipt["frozen_candidate"]["candidate_id"] == RETENTION_ID
    rejected = next(
        row for row in receipt["candidate_summaries"] if row["candidate_id"] == "searched-a"
    )
    assert rejected["mean_focal_effective_damage_by_horizon_or_clear"] == 70.0
    assert rejected["comparison_vs_retention"]["feasible_vs_retention"] is False


def test_selection_uses_focal_objective_then_behavior_and_id_tie_break_and_reports_attribution():
    contract = _contract(include_tie_clone=True)
    manifests = {row["candidate_id"]: row for row in contract["candidate_manifests"]}
    metrics = {
        RETENTION_ID: (0.2, 30.0, "NEVER_SELECTED", 2),
        TAIL_ID: (0.25, 25.0, "NEVER_SELECTED", 4),
        "searched-a": (0.1, 60.0, "MISSED", 3),
        "searched-a-clone": (0.1, 60.0, "MISSED", 3),
        "searched-b": (0.1, 60.0, "ACCEPTED", 1),
    }
    rows = _selection_rows(contract, metrics)
    receipt = select_d5_candidate_v1(contract=contract, selection_rows=rows)
    tied = [manifests[name] for name in ("searched-a", "searched-a-clone", "searched-b")]
    expected = min(tied, key=lambda row: (row["behavior_key"], row["candidate_id"]))
    assert receipt["frozen_candidate"]["candidate_id"] == expected["candidate_id"]
    assert receipt["tail_only_ablation_score"]["action_attribution"][
        "accepted_tail_action_count"
    ] == 8
    searched_a = next(
        row for row in receipt["candidate_summaries"] if row["candidate_id"] == "searched-a"
    )
    assert searched_a["action_attribution"]["accepted_tail_action_count"] == 6
    assert searched_a["action_attribution"]["per_explicit_step"][0][
        "terminal_outcome_seed_counts"
    ]["MISSED"] == 2
    assert receipt["heldout_rows_consumed"] == 0


def test_selection_rejects_missing_seed_and_wrong_controller():
    contract = _contract()
    metrics = {
        RETENTION_ID: (0.2, 30.0, "NEVER_SELECTED", 2),
        TAIL_ID: (0.25, 25.0, "NEVER_SELECTED", 2),
        "searched-a": (0.1, 60.0, "ACCEPTED", 1),
        "searched-b": (0.1, 55.0, "MISSED", 2),
    }
    rows = _selection_rows(contract, metrics)
    with pytest.raises(OfflineWaveD5SelectionV1Error, match="exact paired seed coverage"):
        select_d5_candidate_v1(contract=contract, selection_rows=rows[:-1])

    manifest = contract["candidate_manifests"][0]
    cat_endpoint = build_all_seed_endpoint_row_v1(
        candidate_id=manifest["candidate_id"],
        controller_id=CAT,
        simulator_seed=SELECTION_PAIRS[0][0],
        teammate_seed=SELECTION_PAIRS[0][1],
        replay_status="COMPLETE",
        terminal_endpoint=_terminal(residual_fraction=0.1, focal_damage=50.0),
        domain_fallback_calls=None,
    )
    bad = deepcopy(rows)
    bad[0] = {
        "schema": ROW_SCHEMA,
        "cohort": SELECTION,
        "endpoint_row": cat_endpoint,
        "action_attribution": _attribution(_program_from_manifest(manifest)),
        "complete_case_deleted": False,
        "failed_or_incomplete_imputed_as_zero": False,
    }
    with pytest.raises(OfflineWaveD5SelectionV1Error, match="PI_STAR controller"):
        select_d5_candidate_v1(contract=contract, selection_rows=bad)


def test_heldout_evaluates_only_frozen_candidate_and_retention_without_reselection():
    contract = _contract()
    manifests = {row["candidate_id"]: row for row in contract["candidate_manifests"]}
    metrics = {
        RETENTION_ID: (0.2, 30.0, "NEVER_SELECTED", 2),
        TAIL_ID: (0.25, 25.0, "NEVER_SELECTED", 2),
        "searched-a": (0.1, 60.0, "ACCEPTED", 1),
        "searched-b": (0.1, 55.0, "MISSED", 2),
    }
    selection = select_d5_candidate_v1(
        contract=contract, selection_rows=_selection_rows(contract, metrics)
    )
    frozen_id = selection["frozen_candidate"]["candidate_id"]
    heldout = [
        _row(
            manifests[candidate_id],
            pair,
            cohort=HELDOUT,
            residual_fraction=(0.1 if candidate_id == frozen_id else 0.2),
            focal_damage=(60.0 if candidate_id == frozen_id else 30.0),
        )
        for candidate_id in {RETENTION_ID, frozen_id}
        for pair in HELDOUT_PAIRS
    ]
    receipt = evaluate_d5_frozen_candidate_heldout_v1(
        contract=contract,
        selection_receipt=selection,
        heldout_rows=heldout,
    )
    assert receipt["frozen_candidate"]["candidate_id"] == frozen_id
    assert receipt["selection_reopened"] is False
    assert receipt["heldout_can_reselect"] is False
    assert receipt["frozen_candidate_model_dominates_retention"] is True

    extra = heldout + [
        _row(
            manifests[TAIL_ID],
            HELDOUT_PAIRS[0],
            cohort=HELDOUT,
            residual_fraction=0.0,
            focal_damage=100.0,
        )
    ]
    with pytest.raises(OfflineWaveD5SelectionV1Error, match="unknown candidate"):
        evaluate_d5_frozen_candidate_heldout_v1(
            contract=contract,
            selection_receipt=selection,
            heldout_rows=extra,
        )


def test_selection_and_heldout_seed_components_must_be_new_and_disjoint():
    with pytest.raises(OfflineWaveD5SelectionV1Error, match="D4 confirmation"):
        build_d5_selection_contract_v1(
            campaign_id="leak",
            candidates=_candidates(),
            retention_candidate_id=RETENTION_ID,
            tail_only_candidate_id=TAIL_ID,
            selection_seed_pairs=(D4_PAIRS[0],),
            heldout_seed_pairs=HELDOUT_PAIRS,
            d4_confirmation_seed_pairs=D4_PAIRS,
            horizon_ms=HORIZON,
        )


CONFIRMATION_PAIRS = tuple((7000 + index, 8000 + index) for index in range(48))


def _confirmation_contract():
    return build_d5_selection_contract_v1(
        campaign_id="d5-confirmation-test",
        candidates=_candidates(),
        retention_candidate_id=RETENTION_ID,
        tail_only_candidate_id=TAIL_ID,
        selection_seed_pairs=SELECTION_PAIRS,
        heldout_seed_pairs=CONFIRMATION_PAIRS,
        d4_confirmation_seed_pairs=D4_PAIRS,
        horizon_ms=HORIZON,
    )


def _confirmation_fixture():
    contract = _confirmation_contract()
    manifests = {row["candidate_id"]: row for row in contract["candidate_manifests"]}
    selection_metrics = {
        RETENTION_ID: (0.2, 30.0, "NEVER_SELECTED", 2),
        TAIL_ID: (0.25, 25.0, "NEVER_SELECTED", 4),
        "searched-a": (0.1, 60.0, "ACCEPTED", 1),
        "searched-b": (0.1, 55.0, "MISSED", 2),
    }
    selection = select_d5_candidate_v1(
        contract=contract,
        selection_rows=_selection_rows(contract, selection_metrics),
    )
    winner_id = selection["frozen_candidate"]["candidate_id"]
    candidate_by_controller = {
        PI_STAR: winner_id,
        D4_RETENTION: RETENTION_ID,
        D5_TAIL_ONLY: TAIL_ID,
        CAT: winner_id,
        CONTRA_DEPLOYED: winner_id,
        CONTRA_NEW: winner_id,
        PI_D: winner_id,
    }
    focal_by_controller = {
        PI_STAR: 70.0,
        D4_RETENTION: 40.0,
        D5_TAIL_ONLY: 30.0,
        CAT: 45.0,
        CONTRA_DEPLOYED: 35.0,
        CONTRA_NEW: 50.0,
        PI_D: 55.0,
    }
    endpoint_rows = []
    attributions = []
    for pair in CONFIRMATION_PAIRS:
        for controller_id in CONFIRMATION_CONTROLLER_IDS:
            candidate_id = candidate_by_controller[controller_id]
            inner_controller = (
                PI_STAR
                if controller_id in {PI_STAR, D4_RETENTION, D5_TAIL_ONLY}
                else controller_id
            )
            endpoint = build_all_seed_endpoint_row_v1(
                candidate_id=candidate_id,
                controller_id=inner_controller,
                simulator_seed=pair[0],
                teammate_seed=pair[1],
                replay_status="COMPLETE",
                terminal_endpoint=_terminal(
                    residual_fraction=0.0,
                    focal_damage=focal_by_controller[controller_id],
                ),
                domain_fallback_calls=(
                    0 if inner_controller in {PI_STAR, PI_D} else None
                ),
            )
            endpoint_rows.append(
                build_d5_confirmation_endpoint_row_v1(
                    controller_id=controller_id,
                    endpoint_row=endpoint,
                )
            )
            if controller_id in {PI_STAR, D4_RETENTION, D5_TAIL_ONLY}:
                program = _program_from_manifest(manifests[candidate_id])
                attributions.append(
                    {
                        "controller_id": controller_id,
                        "candidate_id": candidate_id,
                        "simulator_seed": pair[0],
                        "teammate_seed": pair[1],
                        "attribution": _attribution(
                            program,
                            status=(
                                "NEVER_SELECTED"
                                if controller_id == D5_TAIL_ONLY
                                else "ACCEPTED"
                            ),
                            tail=(3 if controller_id == D5_TAIL_ONLY else 1),
                        ),
                    }
                )
    return contract, selection, endpoint_rows, attributions


def test_confirmation_keeps_exact_48_seed_seven_lane_panel_and_pairwise_endpoints():
    contract, selection, endpoint_rows, attributions = _confirmation_fixture()
    receipt = adjudicate_d5_confirmation_v1(
        contract,
        selection,
        endpoint_rows,
        attributions,
    )
    assert receipt["status"] == "FRESH_D5_CONFIRMATION_COMPLETE"
    assert receipt["requested_seed_count"] == 48
    assert receipt["all_seven_controllers_valid_seed_count"] == 48
    assert set(receipt["per_controller"]) == set(CONFIRMATION_CONTROLLER_IDS)
    assert receipt["per_controller"][PI_STAR]["completion_rate"] == 1.0
    assert receipt["per_controller"][PI_STAR][
        "mean_focal_effective_damage_by_horizon_or_clear"
    ] == 70.0
    assert receipt["per_controller"][PI_STAR][
        "mean_background_effective_damage_by_horizon_or_clear"
    ] == 30.0
    versus_cat = receipt["winner_pairwise_by_comparator"][CAT]
    assert versus_cat["technically_valid_paired_seed_count"] == 48
    assert versus_cat["mean_focal_effective_damage_delta"] == 25.0
    assert versus_cat["model_dominance"] == "D5_WINNER_MODEL_DOMINATES"
    assert receipt["development_model_superiority_status"] == (
        "D5_WINNER_PARETO_DOMINATES_ALL_COMPARATORS"
    )
    diagnostic = receipt["action_attribution_diagnostic"]
    assert diagnostic["status"] == "EXACT_SEARCHED_LANE_ATTRIBUTION_COMPLETE"
    assert diagnostic["per_controller"][D5_TAIL_ONLY][
        "accepted_tail_action_count"
    ] == 144
    assert receipt["selection_reopened"] is False
    assert receipt["heldout_can_reselect"] is False


def test_confirmation_rejects_missing_lane_and_attribution_is_optional():
    contract, selection, endpoint_rows, _ = _confirmation_fixture()
    with pytest.raises(OfflineWaveD5SelectionV1Error, match="exact 48-seed"):
        adjudicate_d5_confirmation_v1(
            contract,
            selection,
            endpoint_rows[:-1],
        )

    receipt = adjudicate_d5_confirmation_v1(
        contract,
        selection,
        endpoint_rows,
    )
    assert receipt["action_attribution_diagnostic"] == {
        "status": "NOT_SUPPLIED",
        "required_for_endpoint_adjudication": False,
        "per_controller": None,
    }
