from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from o2o_dps.offline_wave_d3_train_eval_v1 import (
    CANDIDATE_SCHEMA,
    COMBINED,
    HELDOUT,
    OFFLINE_ONLY,
    PARENT_RETENTION,
    PLUGIN_ONLY,
    SELECTION,
    OfflineWaveD3ContractV1Error,
    build_d3_campaign_contract_v1,
    build_d3_full_wave_row_v1,
    evaluate_d3_frozen_candidate_heldout_v1,
    select_d3_frozen_candidate_v1,
    validate_d3_candidate_manifests_v1,
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


PROPOSAL_PAIRS = ((1, 101), (2, 102))
SELECTION_PAIRS = ((11, 111), (12, 112))
HELDOUT_PAIRS = ((21, 121), (22, 122))
BUDGETS = {PLUGIN_ONLY: 4, OFFLINE_ONLY: 4, COMBINED: 4}


def _campaign():
    return build_d3_campaign_contract_v1(
        campaign_id="d3-wave-0042",
        proposal_seed_pairs=PROPOSAL_PAIRS,
        selection_seed_pairs=SELECTION_PAIRS,
        heldout_seed_pairs=HELDOUT_PAIRS,
        proposal_budget_by_arm=BUDGETS,
    )


def _candidate(candidate_id: str, arm: str, behavior_key: str, action: str):
    return {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "proposal_arm": arm,
        "behavior_key": behavior_key,
        "program": {"rules": [{"when": "READY", "action": action}]},
        "provenance": {"source": arm},
    }


def _candidates():
    return [
        _candidate("parent", PARENT_RETENTION, "behavior/parent", "BT"),
        _candidate("plugin", PLUGIN_ONLY, "behavior/plugin", "WW"),
        _candidate("offline", OFFLINE_ONLY, "behavior/offline", "SLAM"),
        _candidate("combined", COMBINED, "behavior/combined", "EXECUTE"),
    ]


def _searched_program(program_id: str, source: str) -> SearchedWaveProgramV1:
    return SearchedWaveProgramV1(
        program_id=program_id,
        parent_program_id=None,
        source_refs=(source,),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=(
            SearchedWaveStepV1(
                step_id=f"{program_id}-step",
                kind=SearchedWaveStepKindV1.ACTION,
                at_or_after_ms=0,
                max_lateness_ms=250,
                proposal_source=source,
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


def _complete_row(candidate_id: str, cohort: str, pair, dps: float):
    return build_d3_full_wave_row_v1(
        candidate_id=candidate_id,
        cohort=cohort,
        simulator_seed=pair[0],
        teammate_seed=pair[1],
        replay_status="COMPLETE",
        score_status="COMPLETED",
        required_targets_dead=True,
        effective_damage=dps * 10.0,
        dps=dps,
        completion_time_ms=10_000,
    )


def _incomplete_row(candidate_id: str, cohort: str, pair):
    return build_d3_full_wave_row_v1(
        candidate_id=candidate_id,
        cohort=cohort,
        simulator_seed=pair[0],
        teammate_seed=pair[1],
        replay_status="HORIZON_REACHED",
        score_status="INCOMPLETE_REQUIRED_TARGETS",
        required_targets_dead=False,
        effective_damage=None,
        dps=None,
        completion_time_ms=None,
        failure_reason="modeled targets remain alive",
    )


def _selection_rows(scores: dict[str, tuple[float, float]]):
    return [
        _complete_row(candidate_id, SELECTION, pair, scores[candidate_id][index])
        for candidate_id in scores
        for index, pair in enumerate(SELECTION_PAIRS)
    ]


def _select(candidates, rows):
    return select_d3_frozen_candidate_v1(
        campaign=_campaign(),
        candidates=candidates,
        selection_rows=rows,
        completed_proposal_trials_by_arm=BUDGETS,
    )


def test_campaign_requires_disjoint_pair_components_and_equal_arm_budgets():
    campaign = _campaign()
    assert campaign["equal_non_parent_proposal_budgets"] is True
    assert campaign["heldout_can_affect_selection"] is False

    with pytest.raises(OfflineWaveD3ContractV1Error, match="simulator seeds overlap"):
        build_d3_campaign_contract_v1(
            campaign_id="leak",
            proposal_seed_pairs=((1, 101),),
            selection_seed_pairs=((1, 201),),
            heldout_seed_pairs=((3, 301),),
            proposal_budget_by_arm=BUDGETS,
        )
    with pytest.raises(OfflineWaveD3ContractV1Error, match="must be equal"):
        build_d3_campaign_contract_v1(
            campaign_id="unequal",
            proposal_seed_pairs=((1, 101),),
            selection_seed_pairs=((2, 102),),
            heldout_seed_pairs=((3, 103),),
            proposal_budget_by_arm={
                PLUGIN_ONLY: 4,
                OFFLINE_ONLY: 3,
                COMBINED: 4,
            },
        )


@pytest.mark.parametrize("forbidden", ["program_by_seed", "seed_action_sequence"])
def test_candidate_manifest_forbids_seed_specific_programs_recursively(forbidden):
    candidates = _candidates()
    candidates[2]["program"]["nested"] = {forbidden: {"11": ["BT"]}}

    with pytest.raises(OfflineWaveD3ContractV1Error, match="seed-specific"):
        validate_d3_candidate_manifests_v1(candidates)


def test_searched_behavior_key_is_recomputed_without_provenance_fields():
    plugin = _searched_program("plugin-program", "PLUGIN_GENERATOR")
    offline = replace(
        plugin,
        program_id="offline-program",
        source_refs=("chronicle-player-a",),
        steps=(
            replace(
                plugin.steps[0],
                step_id="offline-step",
                proposal_source="OFFLINE_GUIDE",
            ),
        ),
    )
    candidates = _candidates()
    candidates[1]["program"] = plugin.to_dict()
    candidates[1]["behavior_key"] = plugin.behavior_key()
    candidates[2]["program"] = offline.to_dict()
    candidates[2]["behavior_key"] = offline.behavior_key()

    validated = validate_d3_candidate_manifests_v1(candidates)
    assert len(validated) == 4
    assert validated[1]["behavior_key"] == validated[2]["behavior_key"]

    candidates[2]["behavior_key"] = "provenance-derived-wrong-key"
    with pytest.raises(OfflineWaveD3ContractV1Error, match="behavior projection"):
        validate_d3_candidate_manifests_v1(candidates)


def test_full_wave_rows_keep_failed_metrics_null_instead_of_zero():
    row = _incomplete_row("offline", SELECTION, SELECTION_PAIRS[0])
    assert row["dps"] is None
    assert row["effective_damage"] is None
    assert row["selection_eligible"] is False
    assert row["failed_or_incomplete_row_imputed_as_zero"] is False

    with pytest.raises(OfflineWaveD3ContractV1Error, match="must keep.*null"):
        build_d3_full_wave_row_v1(
            candidate_id="offline",
            cohort=SELECTION,
            simulator_seed=11,
            teammate_seed=111,
            replay_status="INVALID",
            score_status="INVALID_REPLAY",
            required_targets_dead=False,
            effective_damage=0.0,
            dps=0.0,
            completion_time_ms=0,
            failure_reason="runtime failure",
        )


def test_selection_uses_mean_paired_dps_then_behavior_key_and_keeps_parent_arm():
    candidates = _candidates()
    candidates[1]["behavior_key"] = "behavior/z-plugin"
    candidates[2]["behavior_key"] = "behavior/a-offline"
    receipt = _select(
        candidates,
        _selection_rows(
            {
                "parent": (100.0, 100.0),
                "plugin": (120.0, 120.0),
                "offline": (119.0, 121.0),
                "combined": (90.0, 90.0),
            }
        ),
    )

    assert receipt["status"] == "ONE_CANDIDATE_FROZEN_FOR_HELDOUT"
    assert receipt["frozen_candidate"]["candidate_id"] == "offline"
    assert receipt["selection_score"]["mean_paired_dps"] == 120.0
    assert receipt["parent_retention_score"]["mean_paired_dps"] == 100.0
    assert receipt["heldout_rows_consumed"] == 0


def test_incomplete_candidate_is_excluded_without_zero_imputation():
    scores = {
        "parent": (100.0, 100.0),
        "plugin": (90.0, 90.0),
        "offline": (200.0, 200.0),
        "combined": (80.0, 80.0),
    }
    rows = _selection_rows(scores)
    rows = [
        _incomplete_row("offline", SELECTION, SELECTION_PAIRS[1])
        if row["candidate_id"] == "offline"
        and row["simulator_seed"] == SELECTION_PAIRS[1][0]
        else row
        for row in rows
    ]
    receipt = _select(_candidates(), rows)

    assert receipt["frozen_candidate"]["candidate_id"] == "parent"
    offline = next(
        row for row in receipt["candidate_summaries"] if row["candidate_id"] == "offline"
    )
    assert offline["selection_eligible"] is False
    assert offline["mean_paired_dps"] is None
    assert offline["failed_or_incomplete_row_imputed_as_zero"] is False


def test_selection_rejects_heldout_rows_and_incomplete_budget_accounting():
    candidates = _candidates()
    rows = _selection_rows(
        {candidate["candidate_id"]: (100.0, 100.0) for candidate in candidates}
    )
    heldout = _complete_row("parent", HELDOUT, HELDOUT_PAIRS[0], 999.0)
    with pytest.raises(OfflineWaveD3ContractV1Error, match="held-out data"):
        _select(candidates, rows + [heldout])

    with pytest.raises(OfflineWaveD3ContractV1Error, match="every equal proposal budget"):
        select_d3_frozen_candidate_v1(
            campaign=_campaign(),
            candidates=candidates,
            selection_rows=rows,
            completed_proposal_trials_by_arm={
                PLUGIN_ONLY: 4,
                OFFLINE_ONLY: 3,
                COMBINED: 4,
            },
        )


def test_heldout_evaluates_only_frozen_candidate_and_never_reselects():
    candidates = _candidates()
    receipt = _select(
        candidates,
        _selection_rows(
            {
                "parent": (100.0, 100.0),
                "plugin": (110.0, 110.0),
                "offline": (90.0, 90.0),
                "combined": (80.0, 80.0),
            }
        ),
    )
    assert receipt["frozen_candidate"]["candidate_id"] == "plugin"
    heldout_rows = [
        _complete_row("plugin", HELDOUT, pair, dps)
        for pair, dps in zip(HELDOUT_PAIRS, (1.0, 2.0))
    ]
    result = evaluate_d3_frozen_candidate_heldout_v1(
        campaign=_campaign(),
        selection_receipt=receipt,
        heldout_rows=heldout_rows,
    )

    assert result["status"] == "FROZEN_CANDIDATE_HELDOUT_COMPLETE"
    assert result["frozen_candidate"]["candidate_id"] == "plugin"
    assert result["heldout_score"]["mean_paired_dps"] == 1.5
    assert result["selection_reopened"] is False

    wrong_candidate = deepcopy(heldout_rows)
    wrong_candidate[0]["candidate_id"] = "offline"
    with pytest.raises(OfflineWaveD3ContractV1Error, match="already frozen"):
        evaluate_d3_frozen_candidate_heldout_v1(
            campaign=_campaign(),
            selection_receipt=receipt,
            heldout_rows=wrong_candidate,
        )


def test_provenance_only_duplicate_behavior_is_not_offline_contribution():
    candidates = _candidates()
    candidates[1]["candidate_id"] = "z-plugin"
    candidates[1]["behavior_key"] = "behavior/shared"
    candidates[1]["program"] = {
        "rules": [{"when": "READY", "action": "CLEAVE"}]
    }
    candidates[2]["candidate_id"] = "a-offline"
    candidates[2]["behavior_key"] = "behavior/shared"
    candidates[2]["program"] = deepcopy(candidates[1]["program"])
    receipt = _select(
        candidates,
        _selection_rows(
            {
                "parent": (100.0, 100.0),
                "z-plugin": (120.0, 120.0),
                "a-offline": (120.0, 120.0),
                "combined": (90.0, 90.0),
            }
        ),
    )

    assert receipt["frozen_candidate"]["candidate_id"] == "z-plugin"
    shared = next(
        row
        for row in receipt["behavior_groups"]
        if row["behavior_key"] == "behavior/shared"
    )
    assert shared["candidate_ids"] == ["a-offline", "z-plugin"]
    assert shared["ranking_entries_after_deduplication"] == 1
    contribution = receipt["offline_contribution_on_selection"]
    assert contribution["status"] == "NOT_COUNTED_PROVENANCE_ONLY_BEHAVIOR"
    assert contribution["counted"] is False
    assert contribution["provenance_alone_counts_as_contribution"] is False


def test_distinct_offline_behavior_with_strict_gain_is_counted():
    receipt = _select(
        _candidates(),
        _selection_rows(
            {
                "parent": (100.0, 100.0),
                "plugin": (110.0, 110.0),
                "offline": (130.0, 130.0),
                "combined": (120.0, 120.0),
            }
        ),
    )

    contribution = receipt["offline_contribution_on_selection"]
    assert contribution["status"] == (
        "COUNTED_DISTINCT_BEHAVIOR_WITH_STRICT_SELECTION_GAIN"
    )
    assert contribution["counted"] is True
