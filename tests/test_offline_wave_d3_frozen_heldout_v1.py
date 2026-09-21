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
    PI_STAR,
    OfflineWaveD3FrozenHeldoutV1Error,
    adjudicate_frozen_heldout_expansion_v1,
    build_frozen_heldout_expansion_contract_v1,
    build_frozen_heldout_row_v1,
)
from o2o_dps.offline_wave_d3_train_eval_v1 import (
    CAMPAIGN_SCHEMA,
    CANDIDATE_SCHEMA,
    HELDOUT,
    PROPOSAL,
    SELECTION,
    SELECTION_SCHEMA,
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


NEW_PAIRS = ((31, 131), (32, 132))
CONTROLLERS = (CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D, PI_STAR)


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


def _contract():
    return build_frozen_heldout_expansion_contract_v1(
        source=_source(),
        heldout_seed_pairs=NEW_PAIRS,
        expansion_id="blind-expansion",
    )


def _row(controller: str, pair, dps: float):
    fallback = 0 if controller in {PI_D, PI_STAR} else None
    return build_frozen_heldout_row_v1(
        candidate_id="frozen-winner",
        controller_id=controller,
        simulator_seed=pair[0],
        teammate_seed=pair[1],
        replay_status="COMPLETE",
        score_status="COMPLETED",
        required_targets_dead=True,
        effective_damage=dps * 10.0,
        dps=dps,
        completion_time_ms=10_000,
        domain_fallback_calls=fallback,
    )


def _complete_panel():
    base = {
        CAT: 700.0,
        CONTRA_DEPLOYED: 500.0,
        CONTRA_NEW: 600.0,
        PI_D: 550.0,
        PI_STAR: 800.0,
    }
    return [
        _row(controller, pair, dps + index)
        for index, pair in enumerate(NEW_PAIRS)
        for controller, dps in base.items()
    ]


@pytest.mark.parametrize(
    "pair,match",
    [
        ((1, 999), "prior simulator"),
        ((999, 111), "prior teammate"),
    ],
)
def test_expansion_rejects_every_prior_seed_component(pair, match):
    with pytest.raises(OfflineWaveD3FrozenHeldoutV1Error, match=match):
        build_frozen_heldout_expansion_contract_v1(
            source=_source(),
            heldout_seed_pairs=(pair,),
            expansion_id="leaking-expansion",
        )


def test_expansion_rejects_winner_behavior_drift_and_does_not_reselect():
    source = _source()
    source["selection_receipt"]["frozen_candidate"]["behavior_key"] = "wrong"
    with pytest.raises(OfflineWaveD3FrozenHeldoutV1Error, match="behavior key"):
        build_frozen_heldout_expansion_contract_v1(
            source=source,
            heldout_seed_pairs=NEW_PAIRS,
            expansion_id="bad-frozen-program",
        )

    contract = _contract()
    assert contract["selection_reopened"] is False
    assert contract["candidate_regenerated"] is False
    assert contract["frozen_candidate"]["candidate_id"] == "frozen-winner"


def test_complete_blind_panel_scores_only_the_exact_frozen_winner():
    receipt = adjudicate_frozen_heldout_expansion_v1(
        contract=_contract(), rows=_complete_panel()
    )
    assert receipt["status"] == "FROZEN_WINNER_BLIND_HELDOUT_COMPLETE"
    assert receipt["valid_paired_seed_count"] == 2
    assert receipt["aggregate_on_valid_pairs_only"][PI_STAR]["mean_dps"] == 800.5
    assert receipt["aggregate_on_valid_pairs_only"][CAT]["mean_dps"] == 700.5
    assert [row["pi_star_minus_cat_dps"] for row in receipt["pair_receipts"]] == [
        100.0,
        100.0,
    ]
    assert receipt["selection_reopened"] is False
    assert receipt["candidate_regenerated"] is False


def test_incomplete_pair_keeps_nulls_and_is_not_zero_imputed():
    rows = _complete_panel()
    target = next(
        index
        for index, row in enumerate(rows)
        if row["simulator_seed"] == NEW_PAIRS[1][0]
        and row["controller_id"] == PI_STAR
    )
    rows[target] = build_frozen_heldout_row_v1(
        candidate_id="frozen-winner",
        controller_id=PI_STAR,
        simulator_seed=NEW_PAIRS[1][0],
        teammate_seed=NEW_PAIRS[1][1],
        replay_status="HORIZON_REACHED",
        score_status="INCOMPLETE_REQUIRED_TARGETS",
        required_targets_dead=False,
        effective_damage=None,
        dps=None,
        completion_time_ms=None,
        domain_fallback_calls=0,
        failure_reason="modeled targets remain alive",
    )
    receipt = adjudicate_frozen_heldout_expansion_v1(
        contract=_contract(), rows=rows
    )
    assert receipt["status"] == "FROZEN_WINNER_BLIND_HELDOUT_PARTIAL"
    assert receipt["valid_paired_seed_count"] == 1
    assert receipt["pair_receipts"][1]["pi_star_minus_cat_dps"] is None
    assert receipt["aggregate_on_valid_pairs_only"][PI_STAR]["mean_dps"] == 800.0
    assert receipt["failed_or_incomplete_row_imputed_as_zero"] is False
    pairwise = receipt["pairwise_pi_star_by_baseline"][CAT]
    assert pairwise["common_completed_pair_count"] == 1
    assert pairwise["pi_star_only_completed_pair_count"] == 0
    assert pairwise["baseline_only_completed_pair_count"] == 1
    assert pairwise["neither_completed_pair_count"] == 0
    assert pairwise["pi_star_win_count_on_common_completed"] == 1
    assert pairwise["mean_delta_dps_on_common_completed"] == 100.0
    assert pairwise["confirmatory_interpretation_authorized"] is False
    assert receipt["pairwise_interpretation_scope"] == "DESCRIPTIVE_POSTHOC"


def test_pairwise_receipt_keeps_cat_pair_when_unrelated_baseline_is_incomplete():
    rows = _complete_panel()
    target = next(
        index
        for index, row in enumerate(rows)
        if row["simulator_seed"] == NEW_PAIRS[1][0]
        and row["controller_id"] == CONTRA_DEPLOYED
    )
    rows[target] = build_frozen_heldout_row_v1(
        candidate_id="frozen-winner",
        controller_id=CONTRA_DEPLOYED,
        simulator_seed=NEW_PAIRS[1][0],
        teammate_seed=NEW_PAIRS[1][1],
        replay_status="COMPLETE",
        score_status="INCOMPLETE_REQUIRED_TARGETS",
        required_targets_dead=False,
        effective_damage=None,
        dps=None,
        completion_time_ms=None,
        domain_fallback_calls=None,
        failure_reason="modeled targets remain alive",
    )

    receipt = adjudicate_frozen_heldout_expansion_v1(
        contract=_contract(), rows=rows
    )
    assert receipt["valid_paired_seed_count"] == 1
    cat_pairwise = receipt["pairwise_pi_star_by_baseline"][CAT]
    contra_pairwise = receipt["pairwise_pi_star_by_baseline"][CONTRA_DEPLOYED]
    assert cat_pairwise["common_completed_pair_count"] == 2
    assert cat_pairwise["pi_star_win_count_on_common_completed"] == 2
    assert contra_pairwise["common_completed_pair_count"] == 1
    assert contra_pairwise["pi_star_only_completed_pair_count"] == 1


@pytest.mark.parametrize("controller", [PI_D, PI_STAR])
def test_fallback_capable_completed_rows_require_explicit_zero(controller):
    with pytest.raises(
        OfflineWaveD3FrozenHeldoutV1Error, match="exactly zero fallback"
    ):
        build_frozen_heldout_row_v1(
            candidate_id="frozen-winner",
            controller_id=controller,
            simulator_seed=31,
            teammate_seed=131,
            replay_status="COMPLETE",
            score_status="COMPLETED",
            required_targets_dead=True,
            effective_damage=8000.0,
            dps=800.0,
            completion_time_ms=10_000,
            domain_fallback_calls=1,
        )


def test_adjudicator_rejects_duplicate_or_nonfrozen_rows():
    rows = _complete_panel()
    rows.append(deepcopy(rows[0]))
    with pytest.raises(OfflineWaveD3FrozenHeldoutV1Error, match="duplicate"):
        adjudicate_frozen_heldout_expansion_v1(contract=_contract(), rows=rows)

    rows = _complete_panel()
    rows[0]["candidate_id"] = "reselected-candidate"
    with pytest.raises(OfflineWaveD3FrozenHeldoutV1Error, match="frozen candidate"):
        adjudicate_frozen_heldout_expansion_v1(contract=_contract(), rows=rows)
