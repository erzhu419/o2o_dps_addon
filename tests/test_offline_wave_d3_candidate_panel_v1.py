from __future__ import annotations

from collections import Counter

import pytest

from o2o_dps.offline_wave_d3_candidate_panel_v1 import (
    D900_ACTION_SEQUENCE,
    OfflineWaveD3CandidatePanelV1Error,
    build_d3_candidate_panel_v1,
)
from o2o_dps.offline_wave_d3_train_eval_v1 import (
    COMBINED,
    OFFLINE_ONLY,
    PARENT_RETENTION,
    PLUGIN_ONLY,
    validate_d3_candidate_manifests_v1,
)
from o2o_dps.offline_wave_policy_v1 import (
    EVIDENCE_KNOWN_ONTOLOGY_START,
    LANE_GCD,
    LANE_OFF_GCD,
    LANE_QUEUE,
    TARGET_CURRENT,
    TARGET_NO_EXPLICIT,
    TARGET_OTHER,
    OfflineWaveActionV1,
    OfflineWaveFeedbackPolicyV1,
)
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveGapBehaviorV1,
    searched_wave_program_from_dict_v1,
)
from o2o_dps.sim_bridge import ActionRef


_ACTION_ROWS = (
    (890, "warrior.battle_stance", ActionRef(spell_id=2457), LANE_OFF_GCD, TARGET_NO_EXPLICIT, None),
    (1_093, "warrior.charge", ActionRef(spell_id=11578), LANE_GCD, TARGET_CURRENT, 0),
    (2_234, "warrior.berserker_stance", ActionRef(spell_id=2458), LANE_OFF_GCD, TARGET_NO_EXPLICIT, None),
    (3_515, "warrior.bloodthirst", ActionRef(spell_id=23894), LANE_GCD, TARGET_CURRENT, 0),
    (4_281, "item.kiss_of_the_spider", ActionRef(item_id=22954), LANE_OFF_GCD, TARGET_NO_EXPLICIT, None),
    (4_687, "warrior.cleave", ActionRef(spell_id=20569, tag=1), LANE_QUEUE, TARGET_CURRENT, 0),
    (5_609, "warrior.whirlwind", ActionRef(spell_id=1680), LANE_GCD, TARGET_NO_EXPLICIT, None),
    (8_078, "warrior.execute", ActionRef(spell_id=20662), LANE_GCD, TARGET_CURRENT, 0),
    (11_968, "warrior.heroic_strike", ActionRef(spell_id=25286, tag=1), LANE_QUEUE, TARGET_OTHER, 1),
    (12_125, "warrior.bloodthirst", ActionRef(spell_id=23894), LANE_GCD, TARGET_CURRENT, 1),
    (13_234, "warrior.heroic_strike", ActionRef(spell_id=25286, tag=1), LANE_QUEUE, TARGET_OTHER, 2),
    (14_312, "warrior.heroic_strike", ActionRef(spell_id=25286, tag=1), LANE_QUEUE, TARGET_CURRENT, 2),
    (15_187, "warrior.execute", ActionRef(spell_id=20662), LANE_GCD, TARGET_CURRENT, 2),
)


def _policy() -> OfflineWaveFeedbackPolicyV1:
    actions = tuple(
        OfflineWaveActionV1(
            source_ordinal=index,
            at_or_after_ms=at_ms,
            action_key=key,
            action_ref=action,
            lane=lane,
            target_role=role,
            source_target_ordinal=target,
            source_order_key=(at_ms, index),
            evidence_status=EVIDENCE_KNOWN_ONTOLOGY_START,
            build_segment_ref="segment-d900",
        )
        for index, (at_ms, key, action, lane, role, target) in enumerate(_ACTION_ROWS)
    )
    return OfflineWaveFeedbackPolicyV1(
        policy_id="chronicle.pi_d.d900",
        mode_id="d900-mode",
        encounter_name="Upper Kara d900",
        source_instance_id="d900-instance",
        source_episode_id="d900-episode",
        source_wave_id="d900-wave",
        source_player_guid="player-one",
        source_player_name="warrior",
        source_wave_ordinal=0,
        observed_duration_ms=15_531,
        complete_wave_coverage=True,
        observed_target_count=3,
        source_target_guids=("target-a", "target-b", "target-c"),
        build_segment_refs=("segment-d900",),
        actions=actions,
        unresolved_start_evidence=(),
        tail_gcd_priority=(
            "warrior.bloodthirst",
            "warrior.execute",
            "warrior.charge",
            "warrior.whirlwind",
        ),
        tail_queue_priority=("warrior.heroic_strike", "warrior.cleave"),
        tail_off_gcd_once=(
            "warrior.battle_stance",
            "warrior.berserker_stance",
            "item.kiss_of_the_spider",
        ),
    )


def _guide_actions() -> tuple[dict[str, object], ...]:
    rows = (
        ("TERMINAL_GCD", LANE_GCD, ActionRef(spell_id=23894), 0),
        ("OPTIONAL_OFF_GCD_EXECUTED", LANE_OFF_GCD, ActionRef(spell_id=2687), 5_500),
        ("TERMINAL_GCD", LANE_GCD, ActionRef(spell_id=20662), 6_000),
        ("TERMINAL_GCD", LANE_GCD, ActionRef(spell_id=1680), 7_000),
        ("QUEUE_SET", LANE_QUEUE, ActionRef(spell_id=25286, tag=1), 9_000),
        ("TERMINAL_GCD", LANE_GCD, ActionRef(spell_id=23894), 10_000),
    )
    projected = tuple(
        {
            "kind": kind,
            "lane": lane,
            "action": action.to_wire(),
            "state_time_ms": at_ms,
            "receipt_index": index,
        }
        for index, (kind, lane, action, at_ms) in enumerate(rows)
    )
    projected[4]["policy_target_index"] = 2
    return projected


def _programs(panel, arm):
    return [
        searched_wave_program_from_dict_v1(row["program"])
        for row in panel
        if row["proposal_arm"] == arm
    ]


def test_panel_has_one_parent_equal_arms_global_dedup_and_valid_manifests() -> None:
    panel = build_d3_candidate_panel_v1(
        _policy(), offline_guide_actions=_guide_actions()
    )

    assert Counter(row["proposal_arm"] for row in panel) == {
        PARENT_RETENTION: 1,
        PLUGIN_ONLY: 8,
        OFFLINE_ONLY: 8,
        COMBINED: 8,
    }
    assert len({row["behavior_key"] for row in panel}) == len(panel)
    assert validate_d3_candidate_manifests_v1(panel) == panel
    assert panel == build_d3_candidate_panel_v1(
        _policy(), offline_guide_actions=_guide_actions()
    )


def test_parent_is_retained_and_candidates_have_no_seed_conditioned_shape() -> None:
    policy = _policy()
    panel = build_d3_candidate_panel_v1(
        policy, proposal_budget_per_arm=4, offline_guide_actions=_guide_actions()
    )
    parent = _programs(panel, PARENT_RETENTION)[0]

    assert parent.gap_behavior is SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP
    assert tuple(step.action_key for step in parent.steps) == D900_ACTION_SEQUENCE
    assert tuple(step.at_or_after_ms for step in parent.steps) == tuple(
        action.at_or_after_ms for action in policy.actions
    )
    assert tuple(step.max_lateness_ms for step in parent.steps) == (
        203, 1_141, 1_281, 766, 406, 922, 2_000,
        2_000, 157, 1_109, 1_078, 875, 344,
    )
    assert parent.tail_gcd_priority == policy.tail_gcd_priority
    assert parent.tail_queue_priority == policy.tail_queue_priority
    assert parent.tail_off_gcd_once == policy.tail_off_gcd_once

    forbidden = {
        "seed", "seed_id", "simulator_seed", "teammate_seed", "per_seed",
        "per_seed_program", "per_seed_programs", "seed_override",
        "seed_overrides", "seed_program", "seed_programs",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key.casefold().replace("-", "_")
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert not (set(keys(panel)) & forbidden)


def test_plugin_donors_cover_joint_opener_bloodrage_windows_tail_and_gap_edits() -> None:
    panel = build_d3_candidate_panel_v1(
        _policy(), proposal_budget_per_arm=4, offline_guide_actions=_guide_actions()
    )
    parent = _programs(panel, PARENT_RETENTION)[0]
    plugins = _programs(panel, PLUGIN_ONLY)
    opener = {"warrior.battle_stance", "warrior.charge", "warrior.berserker_stance"}

    assert all(not (opener & {step.action_key for step in program.steps}) for program in plugins)
    assert {
        next(step.at_or_after_ms for step in program.steps if step.action_key == "warrior.bloodthirst")
        for program in plugins
    } == {0, 500, 1_000}
    assert {
        step.at_or_after_ms
        for program in plugins
        for step in program.steps
        if step.action_ref == ActionRef(spell_id=2687) and step.lane == LANE_OFF_GCD
    } == {5_500, 6_000}
    assert any(
        step.action_key == "warrior.whirlwind" and step.max_lateness_ms > 500
        for program in plugins for step in program.steps
    )
    assert any(
        step.action_key == "warrior.execute" and step.max_lateness_ms > 500
        for program in plugins for step in program.steps
    )
    assert all(program.tail_gcd_priority != parent.tail_gcd_priority for program in plugins)
    assert all(
        not (opener & set(program.tail_off_gcd_once)) for program in plugins
    )
    assert {program.gap_behavior for program in plugins} == {
        SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
        SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
    }


def test_offline_order_comes_from_accepted_rows_and_combined_names_both_donors() -> None:
    panel = build_d3_candidate_panel_v1(
        _policy(), proposal_budget_per_arm=4, offline_guide_actions=_guide_actions()
    )
    expected_order = (
        "warrior.bloodthirst",
        "warrior.bloodrage",
        "warrior.execute",
        "warrior.whirlwind",
        "warrior.heroic_strike",
        "warrior.bloodthirst",
    )
    assert all(
        tuple(step.action_key for step in program.steps) == expected_order
        for program in _programs(panel, OFFLINE_ONLY)
    )
    assert all(
        program.steps[4].target.index == 2
        for program in _programs(panel, OFFLINE_ONLY)
    )
    for program in _programs(panel, COMBINED):
        assert any("PLUGIN_MECHANISM" in ref for ref in program.source_refs)
        assert any("ACCEPTED_OFFLINE_GUIDE" in ref for ref in program.source_refs)
        assert not {
            "warrior.battle_stance", "warrior.berserker_stance"
        } & set(program.tail_off_gcd_once)


def test_missing_offline_donor_and_excess_budget_raise_instead_of_unbalancing() -> None:
    with pytest.raises(OfflineWaveD3CandidatePanelV1Error, match="no accepted"):
        build_d3_candidate_panel_v1(_policy(), proposal_budget_per_arm=4)

    with pytest.raises(OfflineWaveD3CandidatePanelV1Error, match="donor space"):
        build_d3_candidate_panel_v1(
            _policy(),
            proposal_budget_per_arm=10_000,
            offline_guide_actions=_guide_actions(),
        )
