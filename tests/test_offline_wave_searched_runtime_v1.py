from __future__ import annotations

from dataclasses import replace

import pytest

from o2o_dps.causal_action_program_v1 import ProgramOriginV1
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.offline_wave_policy_v1 import (
    LANE_GCD,
    OfflineWaveRuntimeBindingV1,
)
from o2o_dps.offline_wave_searched_program_v1 import (
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
    SearchedWaveTargetV1,
)
from o2o_dps.offline_wave_searched_runtime_v1 import (
    SearchedWaveProgramSessionV1,
    SearchedWaveRuntimeV1Error,
    build_searched_wave_program_runtime_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction


BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)
KISS_OF_THE_SPIDER = ActionRef(item_id=22954)


def _action_step(
    step_id: str,
    action: ActionRef,
    action_key: str,
    *,
    at_ms: int,
    lateness_ms: int = 500,
    target: SearchedWaveTargetV1 | None = None,
    guard: ObservableCausalGuardV1 | None = None,
) -> SearchedWaveStepV1:
    return SearchedWaveStepV1(
        step_id=step_id,
        kind=SearchedWaveStepKindV1.ACTION,
        at_or_after_ms=at_ms,
        max_lateness_ms=lateness_ms,
        proposal_source="TEST",
        action_key=action_key,
        action_ref=action,
        lane=LANE_GCD,
        target=target
        or SearchedWaveTargetV1(SearchedWaveTargetKindV1.CURRENT),
        guard=guard,
    )


def _program(
    *steps: SearchedWaveStepV1,
    gap: SearchedWaveGapBehaviorV1 = SearchedWaveGapBehaviorV1.WAIT_UNTIL_STEP,
) -> SearchedWaveProgramV1:
    return SearchedWaveProgramV1(
        program_id="searched-test",
        parent_program_id=None,
        source_refs=("test-source",),
        applied_edit_ids=(),
        gap_behavior=gap,
        steps=steps,
        tail_gcd_priority=("warrior.bloodthirst", "warrior.whirlwind"),
        tail_queue_priority=(),
        tail_off_gcd_once=(),
    )


def _binding() -> OfflineWaveRuntimeBindingV1:
    return OfflineWaveRuntimeBindingV1(
        runtime_wave_id="wave-test",
        source_target_guids=("Creature-A", "Creature-B"),
        target_indexes=(10, 20),
    )


def _observation(
    time_ms: int,
    *,
    active_indexes: tuple[int, ...] = (10, 20),
    current_index: int = 10,
    rage: float = 50.0,
) -> CausalLiveStateProjectionV1:
    return CausalLiveStateProjectionV1(
        state={
            "time_ms": time_ms,
            "target_index": current_index,
            "power": {"type": "rage", "current": rage, "maximum": 100.0},
            "precombat": {"active": False},
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": index,
                        "maximum_health": 100.0,
                        "current_health": 100.0,
                        "attackable": True,
                        "dead": False,
                    }
                    for index in active_indexes
                ]
            },
        },
        policy_to_simulator_target_index=active_indexes,
        visibility_cutoff_ms=time_ms,
    )


def _available(
    *actions: ActionRef,
    illegal: frozenset[ActionRef] = frozenset(),
) -> tuple[AvailableAction, ...]:
    return tuple(
        AvailableAction(
            index=index,
            action=action,
            label=str(action.to_wire()),
            legal=action not in illegal,
            ready_in_ms=0,
            triggers_gcd=True,
        )
        for index, action in enumerate(actions)
    )


def _gcd_receipt(action: ActionRef) -> tuple[dict[str, object], ...]:
    return (
        {
            "kind": "TERMINAL_GCD",
            "action": action.to_wire(),
            "state_time_ms": 1,
        },
    )


def test_runtime_binding_is_searched_and_opens_fresh_sessions() -> None:
    searched = _program(
        _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0)
    )
    program, binding = build_searched_wave_program_runtime_v1(
        searched, _binding()
    )

    assert program.origin is ProgramOriginV1.SEARCHED_REACTIVE
    assert binding.source_policy_id == searched.program_id
    assert binding.open_session() is not binding.open_session()
    assert "expected_cat_gcd_action" not in str(searched.to_dict())


def test_wait_gap_does_not_claim_step_progress() -> None:
    session = SearchedWaveProgramSessionV1(
        _program(
            _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=500)
        ),
        _binding(),
    )
    decision = session(_observation(0), _available(BLOODTHIRST))

    assert decision.gcd_action is None
    assert decision.wait_ms == 500
    session.record_last_execution_receipt_v1(
        decision, ({"kind": "TERMINAL_WAIT", "state_time_ms": 100},)
    )
    assert session.committed_step_ids == ()


def test_tail_fill_uses_searched_tail_before_future_step() -> None:
    session = SearchedWaveProgramSessionV1(
        _program(
            _action_step("ww", WHIRLWIND, "warrior.whirlwind", at_ms=500),
            gap=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        ),
        _binding(),
    )
    decision = session(_observation(0), _available(BLOODTHIRST, WHIRLWIND))

    assert decision.gcd_action == BLOODTHIRST
    session.record_last_execution_receipt_v1(decision, _gcd_receipt(BLOODTHIRST))
    assert session.committed_step_ids == ()
    assert any(
        row["kind"] == "SEARCHED_TAIL_SELECTED" for row in session.audit_events
    )


def test_tail_recomputes_after_gcd_before_queueing() -> None:
    searched = replace(
        _program(
            _action_step("future", WHIRLWIND, "warrior.whirlwind", at_ms=500),
            gap=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        ),
        tail_queue_priority=("warrior.heroic_strike",),
    )
    session = SearchedWaveProgramSessionV1(searched, _binding())
    decision = session(
        _observation(0), _available(BLOODTHIRST, HEROIC_STRIKE, WHIRLWIND)
    )

    assert decision.gcd_action == BLOODTHIRST
    assert decision.queue_action is None


def test_tail_runs_off_gcd_alone_before_recomputing_other_lanes() -> None:
    searched = replace(
        _program(
            _action_step("future", WHIRLWIND, "warrior.whirlwind", at_ms=500),
            gap=SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP,
        ),
        tail_queue_priority=("warrior.heroic_strike",),
        tail_off_gcd_once=("item.kiss_of_the_spider",),
    )
    session = SearchedWaveProgramSessionV1(searched, _binding())
    decision = session(
        _observation(0),
        _available(
            BLOODTHIRST, HEROIC_STRIKE, KISS_OF_THE_SPIDER, WHIRLWIND
        ),
    )

    assert [row.action for row in decision.optional_off_gcd_prefixes] == [
        KISS_OF_THE_SPIDER
    ]
    assert decision.gcd_action is None
    assert decision.queue_action is None


def test_step_commits_only_after_exact_accepted_action() -> None:
    searched = _program(
        _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0)
    )
    session = SearchedWaveProgramSessionV1(searched, _binding())
    decision = session(_observation(0), _available(BLOODTHIRST))
    session.record_last_execution_receipt_v1(decision, _gcd_receipt(WHIRLWIND))
    assert session.committed_step_ids == ()

    retry = session(_observation(1), _available(BLOODTHIRST))
    session.record_last_execution_receipt_v1(retry, _gcd_receipt(BLOODTHIRST))
    assert session.committed_step_ids == ("bt",)


def test_runtime_audit_joins_proposal_ordinal_to_bridge_decision_index() -> None:
    searched = _program(
        _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0)
    )
    session = SearchedWaveProgramSessionV1(searched, _binding())
    decision = session(_observation(0), _available(BLOODTHIRST))
    session.record_last_execution_receipt_v1(
        decision,
        (
            {
                **_gcd_receipt(BLOODTHIRST)[0],
                "decision_index": 0,
            },
        ),
    )

    selected, confirmed = session.audit_events
    assert selected["proposal_index"] == 0
    assert confirmed["proposal_index"] == 0
    assert confirmed["execution_decision_index"] == 0


def test_runtime_rejects_bridge_decision_index_different_from_proposal_ordinal() -> None:
    searched = _program(
        _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0)
    )
    session = SearchedWaveProgramSessionV1(searched, _binding())
    decision = session(_observation(0), _available(BLOODTHIRST))

    with pytest.raises(
        SearchedWaveRuntimeV1Error,
        match="differs from bridge decision_index",
    ):
        session.record_last_execution_receipt_v1(
            decision,
            (
                {
                    **_gcd_receipt(BLOODTHIRST)[0],
                    "decision_index": 9,
                },
            ),
        )


def test_dead_explicit_target_is_skipped_without_silent_retarget() -> None:
    indexed = SearchedWaveTargetV1(SearchedWaveTargetKindV1.INDEX, 1)
    session = SearchedWaveProgramSessionV1(
        _program(
            _action_step(
                "dead-target",
                WHIRLWIND,
                "warrior.whirlwind",
                at_ms=0,
                target=indexed,
            ),
            _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0),
        ),
        _binding(),
    )
    decision = session(
        _observation(0, active_indexes=(10,)),
        _available(BLOODTHIRST, WHIRLWIND),
    )

    assert session.skipped_step_ids == ("dead-target",)
    assert decision.gcd_action == BLOODTHIRST


def test_skip_guard_advances_to_next_step() -> None:
    guard = ObservableCausalGuardV1(rage_gte=90, false_semantics=SKIP_PLAN)
    session = SearchedWaveProgramSessionV1(
        _program(
            _action_step(
                "guarded-ww",
                WHIRLWIND,
                "warrior.whirlwind",
                at_ms=0,
                guard=guard,
            ),
            _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0),
        ),
        _binding(),
    )
    decision = session(
        _observation(0, rage=20), _available(BLOODTHIRST, WHIRLWIND)
    )

    assert session.skipped_step_ids == ("guarded-ww",)
    assert decision.gcd_action == BLOODTHIRST


def test_missed_window_is_nullified_then_next_step_runs() -> None:
    session = SearchedWaveProgramSessionV1(
        _program(
            _action_step(
                "expired-ww",
                WHIRLWIND,
                "warrior.whirlwind",
                at_ms=0,
                lateness_ms=10,
            ),
            _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=20),
        ),
        _binding(),
    )
    # The first call defines wave-relative t=0.  At absolute 25, elapsed=25.
    initial = session(_observation(100), _available(BLOODTHIRST, WHIRLWIND))
    session.record_last_execution_receipt_v1(initial, _gcd_receipt(WHIRLWIND))
    # Rebuild to observe an actually missed first window without committing it.
    session = SearchedWaveProgramSessionV1(session.searched_program, _binding())
    wait = session(_observation(100), _available(BLOODTHIRST, illegal=frozenset({WHIRLWIND})))
    session.record_last_execution_receipt_v1(
        wait, ({"kind": "TERMINAL_WAIT", "state_time_ms": 110},)
    )
    wait = session(_observation(110), _available(BLOODTHIRST, illegal=frozenset({WHIRLWIND})))
    session.record_last_execution_receipt_v1(
        wait, ({"kind": "TERMINAL_WAIT", "state_time_ms": 120},)
    )
    decision = session(
        _observation(120), _available(BLOODTHIRST, illegal=frozenset({WHIRLWIND}))
    )

    assert session.skipped_step_ids == ("expired-ww",)
    assert decision.gcd_action == BLOODTHIRST


def test_explicit_wait_step_commits_from_terminal_wait_receipt() -> None:
    action = _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=50)
    wait_step = SearchedWaveStepV1(
        step_id="pause",
        kind=SearchedWaveStepKindV1.WAIT,
        at_or_after_ms=0,
        max_lateness_ms=0,
        proposal_source="TEST",
        wait_ms=25,
    )
    session = SearchedWaveProgramSessionV1(_program(wait_step, action), _binding())
    decision = session(_observation(0), _available(BLOODTHIRST))
    assert decision.wait_ms == 25
    session.record_last_execution_receipt_v1(
        decision, ({"kind": "TERMINAL_WAIT", "state_time_ms": 25},)
    )
    assert session.committed_step_ids == ("pause",)


def test_runtime_rejects_out_of_binding_target_index() -> None:
    searched = _program(
        _action_step(
            "bad-target",
            BLOODTHIRST,
            "warrior.bloodthirst",
            at_ms=0,
            target=SearchedWaveTargetV1(SearchedWaveTargetKindV1.INDEX, 2),
        )
    )
    with pytest.raises(Exception, match="binding has 2 targets"):
        SearchedWaveProgramSessionV1(searched, _binding())


def test_program_object_is_immutable_across_session_progress() -> None:
    searched = _program(
        _action_step("bt", BLOODTHIRST, "warrior.bloodthirst", at_ms=0)
    )
    before = searched.to_dict()
    session = SearchedWaveProgramSessionV1(searched, _binding())
    decision = session(_observation(0), _available(BLOODTHIRST))
    session.record_last_execution_receipt_v1(decision, _gcd_receipt(BLOODTHIRST))

    assert searched.to_dict() == before
    assert replace(searched, program_id="other").behavior_key() == searched.behavior_key()
