from __future__ import annotations

from copy import deepcopy

import pytest

from o2o_dps.offline_wave_d5_candidate_panel_v1 import (
    OfflineWaveD5CandidatePanelV1Error,
    PARENT_CANDIDATE_ID,
    RETENTION,
    SEARCHED,
    TAIL_ONLY,
    TAIL_ONLY_CANDIDATE_ID,
    _coverage_receipt,
    build_d5_candidate_panel_v1,
    validate_d5_candidate_panel_v1,
)
from o2o_dps.offline_wave_policy_v1 import LANE_GCD, LANE_OFF_GCD, LANE_QUEUE
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
    SearchedWaveTargetV1,
    searched_wave_program_from_dict_v1,
)
from o2o_dps.sim_bridge import ActionRef


HORIZON_MS = 15_531


def _step(
    ordinal: int,
    at_ms: int,
    lateness_ms: int,
    key: str,
    action: ActionRef,
    lane: str,
) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=f"combined-step-{ordinal:04d}",
        kind=SearchedWaveStepKindV1.ACTION,
        at_or_after_ms=at_ms,
        max_lateness_ms=lateness_ms,
        proposal_source="D3_COMBINED_PLUGIN_AND_OFFLINE_DONORS_V1",
        action_key=key,
        action_ref=action,
        lane=lane,
        target=(
            SearchedWaveTargetV1(SearchedWaveTargetKindV1.SELF)
            if key == "warrior.bloodrage"
            else SearchedWaveTargetV1(SearchedWaveTargetKindV1.INDEX, 0)
        ),
    )


def _parent() -> SearchedWaveProgramV1:
    rows = (
        (0, 1_000, "warrior.bloodthirst", ActionRef(spell_id=23894), LANE_GCD),
        (5_015, 0, "item.kiss_of_the_spider", ActionRef(item_id=22954), LANE_OFF_GCD),
        (5_016, 0, "warrior.cleave", ActionRef(spell_id=20569, tag=1), LANE_QUEUE),
        (5_500, 500, "warrior.bloodrage", ActionRef(spell_id=2687), LANE_OFF_GCD),
        (7_500, 3_000, "warrior.execute", ActionRef(spell_id=20662), LANE_GCD),
        (11_968, 0, "warrior.heroic_strike", ActionRef(spell_id=25286, tag=1), LANE_QUEUE),
        (12_125, 0, "warrior.bloodthirst", ActionRef(spell_id=23894), LANE_GCD),
        (13_625, 0, "warrior.heroic_strike", ActionRef(spell_id=25286, tag=1), LANE_QUEUE),
        (14_412, 0, "warrior.heroic_strike", ActionRef(spell_id=25286, tag=1), LANE_QUEUE),
    )
    return SearchedWaveProgramV1(
        program_id="d3-combined-001",
        parent_program_id="offline-donor-0000",
        source_refs=(
            "chronicle.pi_d.d900",
            "D3_ACCEPTED_OFFLINE_GUIDE_DONOR_V1",
        ),
        applied_edit_ids=(),
        gap_behavior=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        steps=tuple(
            _step(index, at_ms, lateness, key, action, lane)
            for index, (at_ms, lateness, key, action, lane) in enumerate(rows)
        ),
        tail_gcd_priority=(
            "warrior.execute",
            "warrior.charge",
            "warrior.whirlwind",
            "warrior.bloodthirst",
        ),
        tail_queue_priority=("warrior.heroic_strike", "warrior.cleave"),
        tail_off_gcd_once=("item.kiss_of_the_spider",),
    )


def _guides() -> tuple[dict[str, object], ...]:
    rows = (
        (0, ActionRef(spell_id=23894), LANE_GCD),
        (5_015, ActionRef(item_id=22954), LANE_OFF_GCD),
        (5_016, ActionRef(spell_id=20569, tag=1), LANE_QUEUE),
        (5_500, ActionRef(spell_id=2687), LANE_OFF_GCD),
        (7_500, ActionRef(spell_id=20662), LANE_GCD),
        (11_968, ActionRef(spell_id=25286, tag=1), LANE_QUEUE),
    )
    return tuple(
        {"state_time_ms": at_ms, "action": action.to_wire(), "lane": lane}
        for at_ms, action, lane in rows
    )


def _build(budget: int = 4):
    return build_d5_candidate_panel_v1(
        _parent(),
        offline_guide_actions=_guides(),
        horizon_ms=HORIZON_MS,
        searched_candidate_budget=budget,
    )


def test_budget_four_is_stratified_and_not_a_zero_lateness_prefix() -> None:
    panel = _build(4)
    receipt = panel["coverage_receipt"]

    assert len(panel["manifests"]) == 6
    assert [row["candidate_id"] for row in panel["manifests"]] == [
        PARENT_CANDIDATE_ID,
        TAIL_ONLY_CANDIDATE_ID,
        "d5-searched-001",
        "d5-searched-002",
        "d5-searched-003",
        "d5-searched-004",
    ]
    assert [row["role"] for row in panel["manifests"]] == [
        RETENTION,
        TAIL_ONLY,
        SEARCHED,
        SEARCHED,
        SEARCHED,
        SEARCHED,
    ]
    assert set(receipt["covered_lateness_profiles"]) == {
        "UNIFORM_250",
        "UNIFORM_500",
        "UNIFORM_1000",
        "MIXED_1000_250_500",
    }
    assert set(receipt["covered_gap_behaviors"]) == {
        "TAIL_FILL_UNTIL_STEP",
        "WAIT_UNTIL_STEP",
    }
    assert receipt["all_search_lateness_profiles_nonzero"] is True
    assert receipt["distinct_tail_order_count"] == 4
    assert receipt["profile_shift_gap_cross_coverage_required"] is False
    assert receipt["profile_shift_gap_cross_coverage_satisfied"] is False
    assert receipt["coverage_satisfied"] is True
    for row in receipt["candidate_coverage"]:
        assert min(row["lateness_by_lane_ms"].values()) > 0
        program = searched_wave_program_from_dict_v1(
            next(
                manifest["program"]
                for manifest in panel["manifests"]
                if manifest["candidate_id"] == row["candidate_id"]
            )
        )
        by_id = {step.step_id: step for step in program.steps}
        assert all(by_id[step_id].max_lateness_ms > 0 for step_id in row["retimed_step_ids"])


def test_exact_parent_and_horizon_safe_tail_only_ablation_are_retained() -> None:
    parent = _parent()
    panel = build_d5_candidate_panel_v1(
        parent,
        offline_guide_actions=_guides(),
        horizon_ms=HORIZON_MS,
        searched_candidate_budget=4,
    )

    assert panel["manifests"][0]["program"] == parent.to_dict()
    tail = searched_wave_program_from_dict_v1(panel["manifests"][1]["program"])
    assert tail.gap_behavior is SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
    assert len(tail.steps) == 1
    assert tail.steps[0].at_or_after_ms == HORIZON_MS + 1
    assert tail.steps[0].kind is SearchedWaveStepKindV1.ACTION
    assert tail.tail_gcd_priority == parent.tail_gcd_priority
    assert tail.tail_queue_priority == parent.tail_queue_priority
    assert set(parent.source_refs).issubset(tail.source_refs)
    assert panel["coverage_receipt"]["tail_only_ablation"] == {
        "candidate_id": TAIL_ONLY_CANDIDATE_ID,
        "status": "MATERIALIZED_WITH_POST_HORIZON_SENTINEL",
        "sentinel_at_ms": HORIZON_MS + 1,
        "explicit_action_executed_within_horizon": False,
    }


def test_production_budget_has_254_unique_seed_invariant_bounded_programs() -> None:
    panel = _build(254)
    searched = panel["manifests"][2:]
    receipt = panel["coverage_receipt"]

    assert len(searched) == 254
    assert len({row["behavior_key"] for row in panel["manifests"]}) == 256
    assert max(
        len(searched_wave_program_from_dict_v1(row["program"]).applied_edit_ids)
        for row in searched
    ) <= 8
    assert all(
        set(_parent().source_refs).issubset(
            searched_wave_program_from_dict_v1(row["program"]).source_refs
        )
        for row in searched
    )

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key).casefold().replace("-", "_")
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    forbidden = {
        "seed",
        "seed_id",
        "simulator_seed",
        "teammate_seed",
        "per_seed",
        "per_seed_program",
        "per_seed_programs",
    }
    assert not (set(keys(panel["manifests"])) & forbidden)
    assert receipt["required_timing_shifts_ms"] == [0, -250, 250, -500, 500]
    assert receipt["profile_shift_gap_cross_coverage_required"] is True
    assert receipt["profile_shift_gap_cross_coverage_satisfied"] is True
    assert all(
        row == {
            "covered_timing_shifts": [0, -250, 250, -500, 500],
            "covered_gap_behaviors": [
                "TAIL_FILL_UNTIL_STEP",
                "WAIT_UNTIL_STEP",
            ],
            "covered_shift_gap_pair_count": 10,
            "required_shift_gap_pair_count": 10,
        }
        for row in receipt["profile_cross_coverage"].values()
    )
    assert panel == _build(254)


def test_production_coverage_detects_profile_shift_gap_confounding() -> None:
    panel = _build(254)
    receipt = panel["coverage_receipt"]
    coverage = deepcopy(receipt["candidate_coverage"])
    profile_shift = {
        "UNIFORM_250": 0,
        "UNIFORM_500": -250,
        "UNIFORM_1000": 250,
        "MIXED_1000_250_500": -500,
    }
    for row in coverage:
        row["timing_shift_ms"] = profile_shift[row["lateness_profile"]]
    confounded = _coverage_receipt(
        budget=254,
        supported_guide_action_count=receipt["supported_guide_action_count"],
        ignored_guide_actions=receipt["ignored_guide_actions"],
        common_missed_step_ids=receipt["common_missed_step_ids"],
        rows=coverage,
        horizon_ms=HORIZON_MS,
    )
    assert confounded["profile_shift_gap_cross_coverage_required"] is True
    assert confounded["profile_shift_gap_cross_coverage_satisfied"] is False
    assert confounded["coverage_satisfied"] is False


def test_validator_rejects_duplicate_behavior_and_unfulfilled_budget() -> None:
    duplicate = _build(4)
    duplicate_program = deepcopy(duplicate["manifests"][2]["program"])
    duplicate_program["program_id"] = "d5-searched-002"
    duplicate["manifests"][3]["program"] = duplicate_program
    duplicate["manifests"][3]["behavior_key"] = duplicate["manifests"][2][
        "behavior_key"
    ]
    with pytest.raises(OfflineWaveD5CandidatePanelV1Error, match="behaviors must be unique"):
        validate_d5_candidate_panel_v1(duplicate)

    incomplete = _build(4)
    incomplete["manifests"].pop()
    with pytest.raises(OfflineWaveD5CandidatePanelV1Error, match="IDs/order"):
        validate_d5_candidate_panel_v1(incomplete)


def test_validator_rejects_false_coverage_and_builder_rejects_unstratified_budget() -> None:
    panel = _build(4)
    panel["coverage_receipt"]["both_gap_behaviors_covered"] = False
    with pytest.raises(OfflineWaveD5CandidatePanelV1Error, match="inconsistent"):
        validate_d5_candidate_panel_v1(panel)

    with pytest.raises(OfflineWaveD5CandidatePanelV1Error, match="at least 4"):
        build_d5_candidate_panel_v1(
            _parent(),
            offline_guide_actions=_guides(),
            horizon_ms=HORIZON_MS,
            searched_candidate_budget=3,
        )


def test_builder_rejects_unknown_or_unordered_guide_actions() -> None:
    unsupported = {
        "state_time_ms": 0,
        "action": ActionRef(spell_id=2457).to_wire(),
        "lane": LANE_OFF_GCD,
    }
    guides = (unsupported, *_guides())
    panel = build_d5_candidate_panel_v1(
        _parent(),
        offline_guide_actions=guides,
        horizon_ms=HORIZON_MS,
        searched_candidate_budget=4,
    )
    receipt = panel["coverage_receipt"]
    assert receipt["guide_action_count"] == len(guides)
    assert receipt["supported_guide_action_count"] == len(_guides())
    assert receipt["ignored_guide_action_count"] == 1
    assert receipt["ignored_guide_actions"] == [
        {
            "guide_index": 0,
            "state_time_ms": 0,
            "action": {"spell_id": 2457},
            "lane": LANE_OFF_GCD,
            "reason": "NOT_IN_FROZEN_PARENT_ACTION_VOCABULARY",
        }
    ]

    with pytest.raises(OfflineWaveD5CandidatePanelV1Error, match="no action supported"):
        build_d5_candidate_panel_v1(
            _parent(),
            offline_guide_actions=(unsupported,),
            horizon_ms=HORIZON_MS,
            searched_candidate_budget=4,
        )

    unordered = list(_guides())
    unordered[1]["state_time_ms"] = -1
    with pytest.raises(OfflineWaveD5CandidatePanelV1Error, match="nonnegative"):
        build_d5_candidate_panel_v1(
            _parent(),
            offline_guide_actions=unordered,
            horizon_ms=HORIZON_MS,
            searched_candidate_budget=4,
        )
