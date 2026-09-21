from __future__ import annotations

import pytest

from o2o_dps.causal_action_program_v1 import ProgramDecisionV1
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceStepV1,
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceSessionV1,
    freeze_two_wave_cat_residual_sequence_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_action_plan_append_teacher_v9 import (
    AppendActionPlanBranchV9,
    ParentAppendBranchSessionV9,
    UpperKaraCatActionPlanAppendTeacherV9Error,
    select_parent_append_points_v9,
)
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import (
    BLOODTHIRST,
    CLEAVE,
    DEATH_WISH,
    HEROIC_STRIKE,
    RECKLESSNESS,
    TURTLE_SLAM,
    WHIRLWIND,
)


class _EagerCat:
    def __init__(self) -> None:
        self.last_gcd_action = "warrior.execute"
        self.seen: list[str] = []

    def __call__(self, observation, available) -> ProgramDecisionV1:
        del available
        self.seen.append(self.last_gcd_action)
        self.last_gcd_action = "warrior.bloodthirst"
        return ProgramDecisionV1(
            target_index=observation.state["target_index"],
            start_attack=True,
            gcd_action=BLOODTHIRST,
        )


def _policy():
    return freeze_two_wave_cat_residual_sequence_v1(
        policy_id="frozen-v8-parent",
        exact_build_id="live_bonereaver",
        waves=(
            CatResidualSequenceWaveV1("wave-1", (0,)),
            CatResidualSequenceWaveV1("wave-2", (1,)),
        ),
        steps=(
            CatResidualSequenceStepV1(
                step_id="existing-ww",
                wave_id="wave-1",
                guard=ObservableCausalGuardV1(
                    target_index=0,
                    target_attackable_is=True,
                    action_ready=WHIRLWIND,
                    false_semantics=SKIP_PLAN,
                ),
                decision=ProgramDecisionV1(
                    target_index=0,
                    start_attack=True,
                    gcd_action=WHIRLWIND,
                ),
                expected_cat_gcd_action=BLOODTHIRST,
            ),
        ),
    )


def _observation(time_ms: int, *, future_field: bool = False):
    state = {
        "time_ms": time_ms,
        "target_index": 0,
        "queued_swing": "KEEP",
        "dynamic_target_semantics": {
            "targets": [
                {
                    "target_index": 0,
                    "attackable": True,
                    "dead": False,
                    "current_health": 1_000.0,
                    "maximum_health": 1_000.0,
                }
            ]
        },
    }
    if future_field:
        state["future_events"] = []
    return CausalLiveStateProjectionV1(
        state=state,
        policy_to_simulator_target_index=(0, 2),
        visibility_cutoff_ms=time_ms,
    )


def _available():
    rows = (
        (BLOODTHIRST, True),
        (WHIRLWIND, True),
        (TURTLE_SLAM, True),
        (DEATH_WISH, True),
        (RECKLESSNESS, True),
        (HEROIC_STRIKE, False),
        (CLEAVE, False),
    )
    return tuple(
        AvailableAction(
            index=index,
            action=action,
            label=str(action.to_wire()),
            legal=True,
            ready_in_ms=0,
            triggers_gcd=triggers_gcd,
            result_bearing=triggers_gcd,
        )
        for index, (action, triggers_gcd) in enumerate(rows)
    )


def _slam_alternative(point):
    return next(
        row for row in point.alternatives() if row.gcd_action == TURTLE_SLAM
    )


def _execute(session, observation):
    decision = session(observation, _available())
    session.record_last_executed_decision_v1(decision)
    return decision


def test_append_point_exists_only_after_parent_step_executed_in_prior_epoch() -> None:
    policy = _policy()
    session = ParentAppendBranchSessionV9(
        policy,
        DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, _EagerCat()),
    )

    first = _execute(session, _observation(0))
    second = _execute(session, _observation(100))

    assert first.gcd_action == WHIRLWIND
    assert second.gcd_action == BLOODTHIRST
    assert not session.points[0].append_ready
    assert session.points[0].alternatives() == ()
    assert session.points[1].append_ready
    assert session.points[1].required_parent_wave_step_keys == (
        ("wave-1", "existing-ww"),
    )
    assert session.points[1].trace.executed_step_keys_before == (
        ("wave-1", "existing-ww"),
    )
    assert RECKLESSNESS not in {
        row.gcd_action for row in session.points[1].alternatives()
    }


def test_single_append_replacement_continues_same_parent_and_cat_session() -> None:
    policy = _policy()
    probe = ParentAppendBranchSessionV9(
        policy,
        DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, _EagerCat()),
    )
    _execute(probe, _observation(0))
    _execute(probe, _observation(100))
    replacement = _slam_alternative(probe.points[1])

    cat = _EagerCat()
    parent = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)
    branch = ParentAppendBranchSessionV9(
        policy,
        parent,
        AppendActionPlanBranchV9(1, replacement),
    )
    assert _execute(branch, _observation(0)).gcd_action == WHIRLWIND
    assert _execute(branch, _observation(100)).gcd_action == TURTLE_SLAM
    assert len(branch.interventions) == 1
    assert branch.interventions[0]["required_parent_wave_step_keys"] == [
        ["wave-1", "existing-ww"]
    ]

    _execute(branch, _observation(200))
    assert cat.seen == [
        "warrior.execute",
        "warrior.whirlwind",
        "warrior.slam",
    ]
    assert parent.executed_step_keys == (("wave-1", "existing-ww"),)


def test_branch_at_parent_execution_epoch_is_rejected_as_not_append_only() -> None:
    policy = _policy()
    probe = ParentAppendBranchSessionV9(
        policy,
        DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, _EagerCat()),
    )
    _execute(probe, _observation(0))
    _execute(probe, _observation(100))
    replacement = _slam_alternative(probe.points[1])
    branch = ParentAppendBranchSessionV9(
        policy,
        DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, _EagerCat()),
        AppendActionPlanBranchV9(0, replacement),
    )

    with pytest.raises(
        UpperKaraCatActionPlanAppendTeacherV9Error,
        match="precedes completion",
    ):
        branch(_observation(0), _available())


def test_selection_uses_only_append_ready_states_and_not_rewards() -> None:
    policy = _policy()
    session = ParentAppendBranchSessionV9(
        policy,
        DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, _EagerCat()),
    )
    for time_ms in (0, 100, 200):
        _execute(session, _observation(time_ms))

    selected = select_parent_append_points_v9(session.points, max_states=2)

    assert tuple(row.decision_index for row in selected) == (1, 2)
    assert all(row.append_ready for row in selected)


def test_prefix_row_rejects_future_control_fields() -> None:
    policy = _policy()
    session = ParentAppendBranchSessionV9(
        policy,
        DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, _EagerCat()),
    )
    _execute(session, _observation(0))
    _execute(session, _observation(100, future_field=True))

    with pytest.raises(
        RuntimeError,
        match="forbidden fields",
    ):
        session.points[1].prefix_row()


def test_recklessness_branch_is_rejected_even_if_native_action_is_ready() -> None:
    with pytest.raises(ValueError, match="Recklessness"):
        AppendActionPlanBranchV9(
            1,
            ProgramDecisionV1(gcd_action=RECKLESSNESS),
        )
