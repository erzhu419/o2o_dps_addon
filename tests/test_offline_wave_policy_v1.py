from __future__ import annotations

from collections.abc import Iterable

import pytest

from o2o_dps.causal_action_program_v1 import ProgramDecisionV1
from o2o_dps.offline_wave_policy_v1 import (
    OfflineWaveFeedbackSessionV1,
    OfflineWavePolicyV1Error,
    OfflineWaveRuntimeBindingV1,
    build_offline_wave_policy_runtime_v1,
    compile_offline_wave_feedback_policy_v1,
    offline_policy_contribution_receipt_v1,
    offline_policy_execution_contribution_receipt_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction


BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
DEATH_WISH = ActionRef(spell_id=12328)
RECKLESSNESS = ActionRef(spell_id=1719)
SWEEPING_STRIKES = ActionRef(spell_id=12292)
EXACT_ITEM = ActionRef(item_id=99901)


def _start(
    action_key: str,
    *,
    at_ms: int,
    index: int,
    target_guid: str = "Creature-A",
    spell_id: int | None = None,
    item_id: int | None = None,
) -> dict[str, object]:
    observed: dict[str, object] = {
        "phase": "START",
        "action_key": action_key,
        "order_key": [at_ms, index, 3, index],
        "exact_target": {
            "guid": target_guid,
            "lane": "HOSTILE_CREATURE",
            "voting_enemy_target": True,
        },
    }
    if spell_id is not None:
        observed["spell"] = {"id": spell_id, "name": f"spell-{spell_id}"}
    if item_id is not None:
        observed["action_payload"] = {"item_id": item_id}
    return {
        "observed_event": observed,
        "state_before": {"wave_elapsed_ms": at_ms},
    }


def _episode(
    transitions: list[dict[str, object]],
    *,
    episode_id: str = "episode-1",
) -> dict[str, object]:
    return {
        "schema": "historical_fury_expert_observation_episode/v1",
        "episode_id": episode_id,
        "instance_id": "upper-kara-raid-1",
        "player": {
            "guid": "Player-1",
            "name": "Expert One",
        },
        "exact_dps_window": {"encounter_name": "Upper Kara wave 1"},
        "window_join": {"coverage": {"partial": False}},
        "wave_observations": [
            {
                "wave_id": "wave-1",
                "wave_ordinal": 0,
                "window": {"boundary_duration_ms": 15_000},
                "prefix_transitions": transitions,
            }
        ],
    }


def _build_mapping(
    transitions: Iterable[dict[str, object]],
) -> dict[str, object]:
    bindings = []
    for index, transition in enumerate(transitions):
        observed = transition["observed_event"]
        assert isinstance(observed, dict)
        bindings.append(
            {
                "order_key": observed["order_key"],
                "segment_ref": "build-a" if index < 3 else "build-b",
            }
        )
    return {
        "schema": "historical_fury_decision_build_mapping/v1",
        "decision_bindings": bindings,
    }


def _observation(
    time_ms: int,
    *,
    active_indexes: tuple[int, ...] = (10, 20),
    current_index: int = 10,
) -> CausalLiveStateProjectionV1:
    targets = [
        {
            "target_index": index,
            "maximum_health": 100.0,
            "current_health": 100.0,
            "attackable": True,
            "dead": False,
        }
        for index in active_indexes
    ]
    return CausalLiveStateProjectionV1(
        state={
            "time_ms": time_ms,
            "target_index": current_index,
            "precombat": {"active": False},
            "dynamic_target_semantics": {"targets": targets},
        },
        policy_to_simulator_target_index=active_indexes,
        visibility_cutoff_ms=time_ms,
    )


def _available(*actions: ActionRef) -> tuple[AvailableAction, ...]:
    return tuple(
        AvailableAction(
            index=index,
            action=action,
            label=str(action.to_wire()),
            legal=True,
            ready_in_ms=0,
            triggers_gcd=action.item_id == 0,
        )
        for index, action in enumerate(actions)
    )


def _binding(
    policy: object, target_indexes: tuple[int, ...] | None = None
) -> OfflineWaveRuntimeBindingV1:
    source_target_guids = getattr(policy, "source_target_guids")
    assert isinstance(source_target_guids, tuple)
    if target_indexes is None:
        target_indexes = (10, 20)[: len(source_target_guids)]
    return OfflineWaveRuntimeBindingV1(
        "runtime-wave", source_target_guids, target_indexes
    )


def _execution_receipt(
    decision: ProgramDecisionV1, *, state_time_ms: int = 5_000
) -> tuple[dict[str, object], ...]:
    if decision.gcd_action is not None:
        return (
            {
                "kind": "TERMINAL_GCD",
                "action": decision.gcd_action.to_wire(),
                "state_time_ms": state_time_ms,
            },
        )
    if decision.queue_action is not None:
        return (
            {
                "kind": "QUEUE_SET",
                "action": decision.queue_action.to_wire(),
                "state_time_ms": state_time_ms,
            },
            {"kind": "TERMINAL_WAIT", "state_time_ms": state_time_ms + 1},
        )
    if decision.optional_off_gcd_prefixes:
        action = decision.optional_off_gcd_prefixes[0].action
        return (
            {
                "kind": "OPTIONAL_OFF_GCD_EXECUTED",
                "action": action.to_wire(),
                "state_time_ms": state_time_ms,
            },
            {"kind": "TERMINAL_WAIT", "state_time_ms": state_time_ms + 1},
        )
    return ({"kind": "TERMINAL_WAIT", "state_time_ms": state_time_ms},)


def test_compile_preserves_complete_source_mode_and_extended_actions() -> None:
    transitions = [
        _start("warrior.bloodthirst", at_ms=0, index=0),
        _start("warrior.whirlwind", at_ms=900, index=1),
        _start(
            "warrior.bloodthirst",
            at_ms=1_800,
            index=2,
            target_guid="Creature-B",
        ),
        _start(
            "unmapped.spell_id.1719",
            at_ms=1_900,
            index=3,
            target_guid="Creature-B",
            spell_id=1719,
        ),
        _start(
            "unmapped.spell_id.12292",
            at_ms=2_000,
            index=4,
            target_guid="Creature-B",
            spell_id=12292,
        ),
        _start(
            "unmapped.item",
            at_ms=2_050,
            index=5,
            target_guid="Creature-B",
            spell_id=77777,
            item_id=EXACT_ITEM.item_id,
        ),
        _start(
            "unmapped.spell_id.99999",
            at_ms=2_100,
            index=6,
            target_guid="Creature-B",
            spell_id=99999,
        ),
    ]
    policy = compile_offline_wave_feedback_policy_v1(
        _episode(transitions),
        build_mapping=_build_mapping(transitions),
    )

    assert [row.action_key for row in policy.actions] == [
        "warrior.bloodthirst",
        "warrior.whirlwind",
        "warrior.bloodthirst",
        "warrior.recklessness",
        "warrior.sweeping_strikes",
        f"item.{EXACT_ITEM.item_id}",
    ]
    assert [row.at_or_after_ms for row in policy.actions] == [
        0,
        900,
        1_800,
        1_900,
        2_000,
        2_050,
    ]
    assert [row.action_ref for row in policy.actions[-3:]] == [
        RECKLESSNESS,
        SWEEPING_STRIKES,
        EXACT_ITEM,
    ]
    assert policy.actions[0].source_target_ordinal == 0
    assert policy.actions[2].source_target_ordinal == 1
    assert policy.actions[2].target_role == "OTHER_OR_NEW_ENEMY"
    assert [row.build_segment_ref for row in policy.actions] == [
        "build-a",
        "build-a",
        "build-a",
        "build-b",
        "build-b",
        "build-b",
    ]
    assert policy.build_segment_refs == ("build-a", "build-b")
    assert policy.observed_target_count == 2
    assert policy.source_target_guids == ("Creature-A", "Creature-B")
    assert policy.unresolved_start_evidence == (
        {
            "source_order_key": [2_100, 6, 3, 6],
            "source_action_key": "unmapped.spell_id.99999",
            "spell_id": 99999,
            "spell_name": "spell-99999",
            "item_id": None,
            "status": "OBSERVED_START_NOT_EXECUTABLE_WITH_CURRENT_REGISTRY",
        },
    )
    serialized = policy.to_dict()
    assert (
        serialized["build_binding"]["all_executable_actions_joined_to_segment"] is True
    )
    assert serialized["build_binding"]["single_exact_segment_for_all_actions"] is False
    assert serialized["contract"]["idle_gap_labeled_as_intentional_wait"] is False
    assert all(
        row["timing_semantics"].endswith("NOT_AN_INTENTIONAL_WAIT_LABEL")
        for row in serialized["actions"]
    )


def test_same_counts_with_different_order_remain_distinct_and_receipted() -> None:
    left = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.bloodthirst", at_ms=0, index=0),
                _start("warrior.whirlwind", at_ms=500, index=1),
                _start("warrior.bloodthirst", at_ms=1_000, index=2),
            ],
            episode_id="left",
        )
    )
    right = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.bloodthirst", at_ms=0, index=0),
                _start("warrior.bloodthirst", at_ms=500, index=1),
                _start("warrior.whirlwind", at_ms=1_000, index=2),
            ],
            episode_id="right",
        )
    )

    assert left.mode_id != right.mode_id
    assert left.actions != right.actions
    receipt = offline_policy_contribution_receipt_v1(left, right)
    assert receipt["receipt_level"] == "COMPILED_POLICY_DEFINITION"
    assert receipt["accepted_execution_compared"] is False
    assert receipt["offline_data_changed_compiled_policy"] is True
    assert "action_sequence" in receipt["changed_behavior_fields"]
    assert "action_counts" not in receipt["changed_behavior_fields"]
    assert (
        receipt["offline_behavior"]["action_counts"]
        == receipt["comparison_behavior"]["action_counts"]
    )


def test_execution_feedback_rejects_without_advancing_and_accepts_with_target_switch() -> (
    None
):
    policy = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.bloodthirst", at_ms=0, index=0),
                _start(
                    "warrior.whirlwind",
                    at_ms=0,
                    index=1,
                    target_guid="Creature-B",
                ),
            ]
        )
    )
    session = OfflineWaveFeedbackSessionV1(
        policy,
        _binding(policy),
    )
    observation = _observation(5_000)
    available = _available(BLOODTHIRST, WHIRLWIND)

    first = session(observation, available)
    assert first.gcd_action == BLOODTHIRST
    session.reject_last_execution_v1("simulator rejected action")
    assert session.committed_source_ordinals == ()
    retry = session(observation, available)
    assert retry == first
    session.record_last_execution_receipt_v1(retry, _execution_receipt(retry))
    assert session.committed_source_ordinals == (0,)

    switched = session(observation, available)
    assert switched.gcd_action == WHIRLWIND
    assert switched.target_index == 20
    session.record_last_execution_receipt_v1(
        switched, _execution_receipt(switched, state_time_ms=5_100)
    )
    assert session.committed_source_ordinals == (0, 1)


def test_missing_native_action_is_masked_and_supported_wave_never_calls_fallback() -> (
    None
):
    policy = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.bloodthirst", at_ms=0, index=0),
                _start("warrior.whirlwind", at_ms=0, index=1),
            ]
        )
    )
    fallback_calls = 0

    def cat_bomb(
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        del observation, available
        nonlocal fallback_calls
        fallback_calls += 1
        raise AssertionError("Cat/domain fallback must not run on a supported wave")

    session = OfflineWaveFeedbackSessionV1(
        policy,
        _binding(policy),
        domain_fallback=cat_bomb,
    )
    decision = session(_observation(5_000), _available(WHIRLWIND))

    assert decision.gcd_action == WHIRLWIND
    assert fallback_calls == 0
    assert session.domain_fallback_calls == 0
    assert session.committed_source_ordinals == ()
    assert any(
        row["kind"] == "SOURCE_ACTION_MASKED_BY_EXACT_NATIVE_SURFACE"
        and row["source_ordinal"] == 0
        for row in session.audit_events
    )
    session.record_last_execution_receipt_v1(decision, _execution_receipt(decision))
    assert session.committed_source_ordinals == (1,)

    tail = session(_observation(5_100), _available(WHIRLWIND))
    assert tail.gcd_action == WHIRLWIND
    assert fallback_calls == 0
    assert session.domain_fallback_calls == 0


def test_repeated_source_finite_resource_actions_are_not_deduplicated() -> None:
    policy = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.death_wish", at_ms=0, index=0),
                _start("warrior.death_wish", at_ms=0, index=1),
                _start("warrior.bloodthirst", at_ms=0, index=2),
            ]
        )
    )
    session = OfflineWaveFeedbackSessionV1(
        policy,
        _binding(policy),
    )
    observation = _observation(5_000)
    available = _available(DEATH_WISH, BLOODTHIRST)

    first = session(observation, available)
    assert first.gcd_action == DEATH_WISH
    session.record_last_execution_receipt_v1(first, _execution_receipt(first))
    second = session(observation, available)
    assert second.gcd_action == DEATH_WISH
    session.record_last_execution_receipt_v1(second, _execution_receipt(second))
    third = session(observation, available)
    assert third.gcd_action == BLOODTHIRST
    assert session.committed_source_ordinals == (0, 1)


def test_runtime_program_has_no_cat_dependency_in_source_refs() -> None:
    policy = compile_offline_wave_feedback_policy_v1(
        _episode([_start("warrior.bloodthirst", at_ms=0, index=0)])
    )
    program, binding = build_offline_wave_policy_runtime_v1(
        policy,
        _binding(policy),
        domain_fallback_factory=None,
    )

    assert all("cat" not in reference.casefold() for reference in program.source_refs)
    assert binding.open_session() is not binding.open_session()


def test_earliest_source_action_blocks_later_action_until_source_deadline() -> None:
    policy = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.bloodthirst", at_ms=0, index=0),
                _start("warrior.whirlwind", at_ms=1_000, index=1),
            ]
        )
    )
    session = OfflineWaveFeedbackSessionV1(policy, _binding(policy))
    blocked_bloodthirst = AvailableAction(
        index=0,
        action=BLOODTHIRST,
        label="bloodthirst-blocked",
        legal=False,
        ready_in_ms=0,
        triggers_gcd=True,
    )
    ready_whirlwind = _available(WHIRLWIND)[0]

    wait = session(_observation(5_000), (blocked_bloodthirst, ready_whirlwind))
    assert wait.gcd_action is None
    assert wait.wait_ms == 100
    session.record_last_execution_receipt_v1(wait, _execution_receipt(wait))

    selected = session(_observation(6_000), (blocked_bloodthirst, ready_whirlwind))
    assert selected.gcd_action == WHIRLWIND
    assert session.committed_source_ordinals == ()
    assert any(
        row["kind"] == "SOURCE_ACTION_MASKED_AFTER_EXECUTION_WINDOW"
        and row["source_ordinal"] == 0
        for row in session.audit_events
    )


def test_decision_equality_without_action_receipt_does_not_commit() -> None:
    transitions = [
        _start(
            "unmapped.item",
            at_ms=0,
            index=0,
            item_id=EXACT_ITEM.item_id,
        ),
        _start("warrior.bloodthirst", at_ms=1_000, index=1),
    ]
    policy = compile_offline_wave_feedback_policy_v1(_episode(transitions))
    session = OfflineWaveFeedbackSessionV1(policy, _binding(policy))
    observation = _observation(5_000)
    decision = session(observation, _available(EXACT_ITEM, BLOODTHIRST))
    assert decision.optional_off_gcd_prefixes[0].action == EXACT_ITEM

    session.record_last_execution_receipt_v1(
        decision,
        ({"kind": "TERMINAL_WAIT", "state_time_ms": 5_001},),
    )
    assert session.committed_source_ordinals == ()
    assert any(
        row["kind"] == "OFFLINE_PROPOSAL_ACTION_NOT_EXECUTED"
        for row in session.audit_events
    )

    retry = session(observation, _available(EXACT_ITEM, BLOODTHIRST))
    assert retry == decision
    session.record_last_execution_receipt_v1(retry, _execution_receipt(retry))
    assert session.committed_source_ordinals == (0,)


def test_runtime_target_binding_requires_exact_source_guid_order() -> None:
    policy = compile_offline_wave_feedback_policy_v1(
        _episode(
            [
                _start("warrior.bloodthirst", at_ms=0, index=0),
                _start(
                    "warrior.whirlwind",
                    at_ms=1_000,
                    index=1,
                    target_guid="Creature-B",
                ),
            ]
        )
    )
    reversed_binding = OfflineWaveRuntimeBindingV1(
        "runtime-wave",
        tuple(reversed(policy.source_target_guids)),
        (10, 20),
    )
    with pytest.raises(OfflineWavePolicyV1Error, match="source target GUID order"):
        OfflineWaveFeedbackSessionV1(policy, reversed_binding)


def test_contribution_receipt_distinguishes_compiled_from_accepted_execution() -> None:
    left = compile_offline_wave_feedback_policy_v1(
        _episode(
            [_start("warrior.bloodthirst", at_ms=0, index=0)],
            episode_id="left-execution",
        )
    )
    right = compile_offline_wave_feedback_policy_v1(
        _episode(
            [_start("warrior.whirlwind", at_ms=0, index=0)],
            episode_id="right-execution",
        )
    )
    left_session = OfflineWaveFeedbackSessionV1(left, _binding(left))
    right_session = OfflineWaveFeedbackSessionV1(right, _binding(right))
    observation = _observation(5_000)
    left_decision = left_session(observation, _available(BLOODTHIRST))
    right_decision = right_session(observation, _available(WHIRLWIND))
    left_session.record_last_execution_receipt_v1(
        left_decision, _execution_receipt(left_decision)
    )
    right_session.record_last_execution_receipt_v1(
        right_decision, _execution_receipt(right_decision)
    )

    receipt = offline_policy_execution_contribution_receipt_v1(
        left_session.audit_events, right_session.audit_events
    )
    assert receipt["receipt_level"] == "ACCEPTED_BRIDGE_EXECUTION"
    assert receipt["offline_data_changed_accepted_execution"] is True
    assert "accepted_action_sequence" in receipt["changed_execution_fields"]
    assert receipt["offline_accepted_execution"]["accepted_action_sequence"] == [
        "spell_id:23894:tag:0"
    ]


def test_build_binding_reports_unjoined_actions_without_same_build_implication() -> (
    None
):
    policy = compile_offline_wave_feedback_policy_v1(
        _episode([_start("warrior.bloodthirst", at_ms=0, index=0)])
    )
    build = policy.to_dict()["build_binding"]
    assert build["status"] == "NOT_JOINED"
    assert build["all_executable_actions_joined_to_segment"] is False
    assert build["single_exact_segment_for_all_actions"] is False
