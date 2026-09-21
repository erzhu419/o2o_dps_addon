from __future__ import annotations

from itertools import product

import pytest

from o2o_dps.causal_guard_v1 import observable_causal_guard_from_dict_v1
from o2o_dps.development_two_wave_segment_policy_v1 import SegmentWaveV1
from o2o_dps.development_two_wave_sequence_candidates_v1 import (
    BLOODTHIRST_V1,
    CLEAVE_QUEUE_V1,
    GCD_ACTIONS_BY_KEY_V1,
    GCD_ORDER_KEYS_V1,
    HEROIC_STRIKE_QUEUE_V1,
    PAIRED_CANDIDATE_COUNT_V1,
    PAIRED_ZERO_ROLE,
    SEARCHED_ROLE,
    TURTLE_SLAM_V1,
    WHIRLWIND_V1,
    build_development_two_wave_sequence_candidate_set_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp


WAVES = (
    SegmentWaveV1("registry-upper-kara-pack-a", (7, 11)),
    SegmentWaveV1("registry-upper-kara-boss-b", (19,)),
)
LONG_COOLDOWN = ActionRef(spell_id=1_719)
GUIDES = (
    "offline:upper-kara-warrior-sequences-v1",
    "cat:profile-1-source",
    "deployed-contra:runtime-source",
    "contra260817:readable-source",
)


def _candidate_set():
    return build_development_two_wave_sequence_candidate_set_v1(
        exact_build_id="fury-exact-build-a",
        waves=WAVES,
        long_cooldown_action=LONG_COOLDOWN,
        resource_id="recklessness",
        proposal_guide_ids=GUIDES,
        wave_one_min_target_hp_pct=55,
        wave_one_min_estimated_remaining_ms=5_000,
    )


def test_exact_cat_zero_and_complete_independent_order_product() -> None:
    result = _candidate_set()

    assert len(result.candidates) == PAIRED_CANDIDATE_COUNT_V1 == 37
    zero = result.candidates[0]
    assert zero.role == PAIRED_ZERO_ROLE
    assert zero.policy is None
    assert zero.wave_one_gcd_order == ()
    assert zero.wave_two_gcd_order == ()
    assert zero.to_dict()["execution"] == {
        "policy_id": CAT_POLICY_ID,
        "sequence": None,
        "paired_zero": True,
    }

    searched = result.candidates[1:]
    assert all(row.role == SEARCHED_ROLE for row in searched)
    assert {
        (row.wave_one_gcd_order, row.wave_two_gcd_order) for row in searched
    } == set(product(GCD_ORDER_KEYS_V1, repeat=2))
    assert len({row.semantic_key() for row in result.candidates}) == 37
    assert len({row.candidate_id for row in result.candidates}) == 37


def test_each_wave_has_its_own_gcd_order_queue_and_declared_target_route() -> None:
    result = _candidate_set()
    candidate = next(
        row
        for row in result.candidates[1:]
        if row.wave_one_gcd_order == ("bloodthirst", "whirlwind", "slam")
        and row.wave_two_gcd_order == ("slam", "bloodthirst", "whirlwind")
    )
    assert candidate.policy is not None
    wave_one_steps = [
        row for row in candidate.policy.steps if row.wave_id == WAVES[0].wave_id
    ]
    wave_two_steps = [
        row for row in candidate.policy.steps if row.wave_id == WAVES[1].wave_id
    ]
    assert len(wave_one_steps) == len(wave_two_steps) == 4

    wave_one_gcd = [
        row.plan
        for row in wave_one_steps
        if row.resource_id is None and row.plan.gcd_action
    ]
    wave_two_gcd = [
        row.plan
        for row in wave_two_steps
        if row.resource_id is None and row.plan.gcd_action
    ]
    assert [row.gcd_action for row in wave_one_gcd] == [
        BLOODTHIRST_V1,
        WHIRLWIND_V1,
        TURTLE_SLAM_V1,
    ]
    assert [row.gcd_action for row in wave_two_gcd] == [
        TURTLE_SLAM_V1,
        BLOODTHIRST_V1,
        WHIRLWIND_V1,
    ]
    assert [row.target_index for row in wave_one_gcd] == [7, 11, 7]
    assert [row.target_index for row in wave_two_gcd] == [19, 19, 19]
    assert all(row.queue_op is QueueLaneOp.SET for row in wave_one_gcd)
    assert all(row.queue_action == CLEAVE_QUEUE_V1 for row in wave_one_gcd)
    assert all(row.queue_op is QueueLaneOp.SET for row in wave_two_gcd)
    assert all(row.queue_action == HEROIC_STRIKE_QUEUE_V1 for row in wave_two_gcd)
    for plan in (*wave_one_gcd, *wave_two_gcd):
        assert plan.guard is not None
        assert plan.guard.target_index == plan.target_index
        assert plan.guard.target_attackable_is is True
        assert plan.guard.action_ready == plan.gcd_action
        assert (
            observable_causal_guard_from_dict_v1(plan.guard.to_dict())
            == plan.guard
        )

    # Even a diagonal GCD-order candidate remains wave-specific through the
    # declared target registry and its multi/single-target queue choice.
    diagonal = next(
        row
        for row in result.candidates[1:]
        if row.wave_one_gcd_order == row.wave_two_gcd_order
    )
    assert diagonal.policy is not None
    first_regular = [
        row.plan
        for row in diagonal.policy.steps
        if row.wave_id == WAVES[0].wave_id and row.plan.gcd_action
    ]
    second_regular = [
        row.plan
        for row in diagonal.policy.steps
        if row.wave_id == WAVES[1].wave_id and row.plan.gcd_action
    ]
    assert [row.queue_action for row in first_regular] != [
        row.queue_action for row in second_regular
    ]
    assert [row.target_index for row in first_regular] != [
        row.target_index for row in second_regular
    ]


def test_long_cooldown_guard_is_causal_and_retries_same_resource() -> None:
    result = _candidate_set()
    policy = result.candidates[1].policy
    assert policy is not None
    resource_steps = [row for row in policy.steps if row.resource_id is not None]
    assert len(resource_steps) == 2
    assert [row.resource_id for row in resource_steps] == [
        "recklessness",
        "recklessness",
    ]
    assert all(row.plan.gcd_action == LONG_COOLDOWN for row in resource_steps)
    assert all(not row.plan.off_gcd_actions for row in resource_steps)

    first_guard = resource_steps[0].plan.guard
    assert first_guard is not None
    assert first_guard.target_index == 7
    assert first_guard.target_attackable_is is True
    assert first_guard.target_hp_pct_gte == 55.0
    assert first_guard.attackable_target_count_gte == 2
    assert first_guard.estimated_remaining_attackable_gte_ms == 5_000
    assert first_guard.action_ready == LONG_COOLDOWN
    first_wire = first_guard.to_dict()
    assert "arrival_ms" not in str(first_wire)
    assert "wave_id" not in str(first_wire)
    assert "remaining_ms" not in first_wire["all_of"]

    retry_guard = resource_steps[1].plan.guard
    assert retry_guard is not None
    assert retry_guard.target_index == 19
    assert retry_guard.target_attackable_is is True
    assert retry_guard.attackable_target_count_gte == 1
    assert retry_guard.attackable_target_count_lte == 1
    assert retry_guard.action_ready == LONG_COOLDOWN
    assert policy.to_dict()["resource_retry_ids"] == ["recklessness"]


def test_freeze_removes_guides_but_candidate_receipt_keeps_provenance() -> None:
    result = _candidate_set()
    assert result.proposal_guide_ids == GUIDES
    assert all(
        not step.plan.guide_provenance and step.plan.guide_priority == 0.0
        for candidate in result.candidates[1:]
        for step in candidate.policy.steps  # type: ignore[union-attr]
    )
    wire = result.to_dict()
    assert wire["proposal_guide_ids"] == list(GUIDES)
    assert wire["contract"]["proposal_guides_removed_from_frozen_policies"] is True
    for candidate in wire["candidates"][1:]:
        for step in candidate["execution"]["sequence"]["steps"]:
            assert step["plan"]["guide"] == {"provenance": [], "priority": 0.0}


def test_fresh_seed_metadata_is_predeclared_and_disjoint() -> None:
    result = _candidate_set()
    assert result.seed_protocol.train_seeds == tuple(range(920_001, 920_257))
    assert result.seed_protocol.evaluation_seeds == tuple(
        range(1_020_001, 1_020_257)
    )
    wire = result.to_dict()["fresh_seed_protocol"]
    assert wire["train"] == {"first": 920_001, "last": 920_256, "count": 256}
    assert wire["evaluation"] == {
        "first": 1_020_001,
        "last": 1_020_256,
        "count": 256,
    }
    assert wire["disjoint"] is True
    assert wire["frozen_before_search"] is True
    assert wire["environment_arrival_nuisance_schedule_ms"] == [
        0,
        1_000,
        3_000,
        5_000,
        7_000,
        9_000,
    ]
    assert wire["arrival_nuisance_is_policy_input"] is False


def test_registry_shape_and_long_cooldown_identity_are_not_hardcoded() -> None:
    other_waves = (
        SegmentWaveV1("other-pack", (101, 303, 707)),
        SegmentWaveV1("other-boss", (909,)),
    )
    result = build_development_two_wave_sequence_candidate_set_v1(
        exact_build_id="other-build",
        waves=other_waves,
        long_cooldown_action=ActionRef(item_id=21_140),
        long_cooldown_triggers_gcd=False,
        resource_id="item-burst",
        proposal_guide_ids=("offline", "cat", "contra"),
    )
    policy = result.candidates[1].policy
    assert policy is not None
    first_targets = [
            row.plan.target_index
            for row in policy.steps
            if row.wave_id == "other-pack"
            and row.resource_id is None
            and row.plan.gcd_action is not None
    ]
    assert first_targets == [101, 303, 707]
    assert policy.steps[0].plan.off_gcd_actions == (ActionRef(item_id=21_140),)

    with pytest.raises(ValueError, match="at least two"):
        build_development_two_wave_sequence_candidate_set_v1(
            exact_build_id="bad-build",
            waves=(
                SegmentWaveV1("not-multitarget", (0,)),
                SegmentWaveV1("single", (1,)),
            ),
            long_cooldown_action=LONG_COOLDOWN,
            resource_id="recklessness",
            proposal_guide_ids=GUIDES,
        )

    with pytest.raises(ValueError, match="distinct"):
        build_development_two_wave_sequence_candidate_set_v1(
            exact_build_id="bad-action",
            waves=WAVES,
            long_cooldown_action=BLOODTHIRST_V1,
            resource_id="not-a-long-cooldown",
            proposal_guide_ids=GUIDES,
        )


def test_action_catalog_is_exact_and_not_a_threshold_only_family() -> None:
    assert GCD_ACTIONS_BY_KEY_V1 == {
        "bloodthirst": ActionRef(spell_id=23_894),
        "whirlwind": ActionRef(spell_id=1_680),
        "slam": ActionRef(spell_id=45_961),
    }
    result = _candidate_set()
    orders = {
        tuple(
            step.plan.gcd_action
            for step in candidate.policy.steps  # type: ignore[union-attr]
            if step.wave_id == WAVES[0].wave_id and step.plan.gcd_action is not None
        )
        for candidate in result.candidates[1:]
    }
    assert len(orders) == 6
