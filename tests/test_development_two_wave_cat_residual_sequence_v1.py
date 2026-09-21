from copy import deepcopy
import json

import pytest

from o2o_dps.causal_action_program_v1 import (
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.causal_guard_v1 import (
    ObservableCausalGuardV1,
    SKIP_PLAN,
)
from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    SCHEMA,
    CatResidualSequenceStepV1,
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceSessionV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    DevelopmentTwoWaveCatResidualSequenceV1Error,
    build_two_wave_cat_residual_sequence_runtime_v1,
    freeze_two_wave_cat_residual_sequence_v1,
    frozen_two_wave_cat_residual_sequence_wire_v1,
    two_wave_cat_residual_sequence_from_dict_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp


BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
EXECUTE = ActionRef(spell_id=20662)
SLAM = ActionRef(spell_id=45961)
OVERPOWER = ActionRef(spell_id=11585)
RECKLESSNESS = ActionRef(spell_id=1719)
HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)

WAVES = (
    CatResidualSequenceWaveV1("wave-1", (0,)),
    CatResidualSequenceWaveV1("wave-2", (1,)),
)


class _StatefulCatResolver:
    def __init__(self) -> None:
        self.calls = 0
        self.decisions: list[ProgramDecisionV1] = []

    def __call__(self, observation, available) -> ProgramDecisionV1:
        del available
        self.calls += 1
        decision = ProgramDecisionV1(
            target_index=observation.state.get("target_index", 0),
            gcd_action=BLOODTHIRST,
        )
        self.decisions.append(decision)
        return decision


class _EagerLastGcdCatResolver(_StatefulCatResolver):
    """Model the imported Cat session's eager proposal-side continuation."""

    def __init__(self, prior: str = "warrior.execute") -> None:
        super().__init__()
        self.last_gcd_action = prior
        self.seen_last_gcd_before_propose: list[str] = []

    def __call__(self, observation, available) -> ProgramDecisionV1:
        self.seen_last_gcd_before_propose.append(self.last_gcd_action)
        decision = super().__call__(observation, available)
        self.last_gcd_action = "warrior.bloodthirst"
        return decision


def _available(
    *,
    include_whirlwind: bool = True,
    whirlwind_ready_in_ms: int = 0,
    whirlwind_legal: bool = True,
) -> tuple[AvailableAction, ...]:
    rows = [
        (BLOODTHIRST, True, 0, True),
        (EXECUTE, True, 0, True),
        (SLAM, True, 0, True),
        (OVERPOWER, True, 0, True),
        (RECKLESSNESS, True, 0, True),
        (HEROIC_STRIKE, True, 0, False),
    ]
    if include_whirlwind:
        rows.append(
            (WHIRLWIND, whirlwind_legal, whirlwind_ready_in_ms, True)
        )
    return tuple(
        AvailableAction(
            index=index,
            action=action,
            label=str(action.spell_id),
            legal=legal,
            ready_in_ms=ready_in_ms,
            triggers_gcd=triggers_gcd,
        )
        for index, (action, legal, ready_in_ms, triggers_gcd) in enumerate(rows)
    )


def _observation(
    time_ms: int,
    *,
    wave: int,
    health: float = 100.0,
    selected_target: int | None = None,
    precombat: bool = False,
    extra_state: dict | None = None,
) -> CausalLiveStateProjectionV1:
    if wave == 1:
        targets = [
            {
                "target_index": 0,
                "maximum_health": 100.0,
                "current_health": health,
                "attackable": not precombat,
                "dead": False,
            }
        ]
        default_target = 0
        private_mapping = (701, 999)
    elif wave == 2:
        targets = [
            {
                "target_index": 0,
                "maximum_health": 100.0,
                "current_health": 0.0,
                "attackable": False,
                "dead": True,
            },
            {
                "target_index": 1,
                "maximum_health": 100.0,
                "current_health": health,
                "attackable": not precombat,
                "dead": False,
            },
        ]
        default_target = 1
        private_mapping = (701, 702, 999)
    else:
        raise ValueError("wave must be one or two")
    state = {
        "time_ms": time_ms,
        "target_index": (
            default_target if selected_target is None else selected_target
        ),
        "precombat": {"active": precombat, "relative_time_ms": -500},
        "dynamic_target_semantics": {"targets": targets},
    }
    if extra_state:
        state.update(extra_state)
    return CausalLiveStateProjectionV1(
        state=state,
        # Deliberately contains non-visible/future control-plane values.  The
        # residual detector must never inspect this private routing tuple.
        policy_to_simulator_target_index=private_mapping,
        visibility_cutoff_ms=time_ms,
    )


def _hp_guard(
    target_index: int,
    *,
    gte: float | None = None,
    lte: float | None = None,
) -> ObservableCausalGuardV1:
    return ObservableCausalGuardV1(
        target_index=target_index,
        target_hp_pct_gte=gte,
        target_hp_pct_lte=lte,
        target_attackable_is=True,
        false_semantics=SKIP_PLAN,
    )


def _step(
    step_id: str,
    wave_id: str,
    guard: ObservableCausalGuardV1,
    action: ActionRef,
    *,
    target_index: int,
) -> CatResidualSequenceStepV1:
    return CatResidualSequenceStepV1(
        step_id=step_id,
        wave_id=wave_id,
        guard=guard,
        decision=ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            gcd_action=action,
        ),
    )


def _policy(
    steps: tuple[CatResidualSequenceStepV1, ...] = (),
    *,
    policy_id: str = "cat-relative-test",
) -> DevelopmentTwoWaveCatResidualSequenceV1:
    return freeze_two_wave_cat_residual_sequence_v1(
        policy_id=policy_id,
        exact_build_id="fury-build-exact",
        waves=WAVES,
        steps=steps,
    )


def test_empty_variable_length_sequence_is_exact_same_cat_session() -> None:
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(_policy(), cat)

    first = session(_observation(0, wave=1), _available())
    second = session(_observation(100, wave=2), _available())

    assert first is cat.decisions[0]
    assert second is cat.decisions[1]
    assert cat.calls == 2
    assert session.executed_step_keys == ()
    assert all(row["kind"] == "EXACT_CAT_FALLBACK" for row in session.audit_events)


def test_variable_length_steps_are_ordered_and_latched_once_per_wave() -> None:
    policy = _policy(
        (
            _step("opener", "wave-1", _hp_guard(0, gte=80), WHIRLWIND, target_index=0),
            _step("finisher", "wave-1", _hp_guard(0, lte=79), EXECUTE, target_index=0),
            _step("opener", "wave-2", _hp_guard(1, gte=1), SLAM, target_index=1),
        )
    )
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    # The second step is eligible by HP, but sequence order retains the first
    # step and returns Cat rather than jumping ahead or waiting.
    false_first = session(_observation(0, wave=1, health=50), _available())
    assert false_first is cat.decisions[0]
    assert false_first.wait_ms is None
    assert session.executed_step_keys == ()

    opener = session(_observation(100, wave=1, health=90), _available())
    assert opener.gcd_action == WHIRLWIND
    assert session.executed_step_keys == (("wave-1", "opener"),)

    finisher = session(_observation(200, wave=1, health=50), _available())
    assert finisher.gcd_action == EXECUTE
    exhausted = session(_observation(300, wave=1, health=40), _available())
    assert exhausted is cat.decisions[3]

    wave_two = session(_observation(1_000, wave=2), _available())
    assert wave_two.gcd_action == SLAM
    assert session.executed_step_keys == (
        ("wave-1", "opener"),
        ("wave-1", "finisher"),
        ("wave-2", "opener"),
    )
    assert cat.calls == 5


@pytest.mark.parametrize(
    ("available", "reason"),
    (
        (_available(include_whirlwind=False), "RESIDUAL_ACTION_ABSENT"),
        (_available(whirlwind_ready_in_ms=600), "RESIDUAL_ACTION_NOT_READY"),
        (_available(whirlwind_legal=False), "RESIDUAL_ACTION_ILLEGAL"),
    ),
)
def test_unexecutable_residual_immediately_returns_cat_without_latching(
    available: tuple[AvailableAction, ...],
    reason: str,
) -> None:
    policy = _policy(
        (
            _step(
                "ww",
                "wave-1",
                _hp_guard(0, gte=1),
                WHIRLWIND,
                target_index=0,
            ),
        )
    )
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    result = session(_observation(0, wave=1), available)

    assert result is cat.decisions[0]
    assert result.wait_ms is None
    assert session.executed_step_keys == ()
    assert session.audit_events[-1]["reason"] == reason


def test_matched_step_advances_cat_once_and_later_epoch_uses_same_session() -> None:
    policy = _policy(
        (
            _step(
                "ww",
                "wave-1",
                _hp_guard(0, gte=1),
                WHIRLWIND,
                target_index=0,
            ),
        )
    )
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    replaced = session(_observation(0, wave=1), _available())
    after_latch = session(_observation(100, wave=1), _available())

    assert replaced.gcd_action == WHIRLWIND
    assert cat.calls == 2
    assert after_latch is cat.decisions[1]
    assert session.executed_step_keys == (("wave-1", "ww"),)


def test_current_visible_selected_target_resolves_overlapping_wave_only() -> None:
    policy = _policy(
        (
            _step("w1", "wave-1", _hp_guard(0, gte=1), WHIRLWIND, target_index=0),
            _step("w2", "wave-2", _hp_guard(1, gte=1), SLAM, target_index=1),
        )
    )
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)
    observation = _observation(1_000, wave=2, selected_target=1)
    state = deepcopy(observation.state)
    state["dynamic_target_semantics"]["targets"][0].update(
        {"current_health": 10.0, "attackable": True, "dead": False}
    )
    overlapping = CausalLiveStateProjectionV1(
        state=state,
        policy_to_simulator_target_index=observation.policy_to_simulator_target_index,
        visibility_cutoff_ms=observation.visibility_cutoff_ms,
    )

    result = session(overlapping, _available())

    assert result.gcd_action == SLAM
    assert session.executed_step_keys == (("wave-2", "w2"),)


def test_missing_guard_observation_and_precombat_both_return_cat() -> None:
    rage_guard = ObservableCausalGuardV1(
        rage_gte=20,
        false_semantics=SKIP_PLAN,
    )
    policy = _policy(
        (_step("rage", "wave-1", rage_guard, WHIRLWIND, target_index=0),)
    )
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    missing = session(_observation(0, wave=1), _available())
    precombat = session(
        _observation(100, wave=1, precombat=True), _available()
    )

    assert missing is cat.decisions[0]
    assert precombat is cat.decisions[1]
    assert session.executed_step_keys == ()
    assert "GUARD_OBSERVATION_UNAVAILABLE" in session.audit_events[0]["reason"]


def test_frozen_wire_round_trips_without_adding_a_content_digest() -> None:
    policy = _policy(
        (
            CatResidualSequenceStepV1(
                step_id="full-body",
                wave_id="wave-1",
                guard=_hp_guard(0, gte=1),
                decision=ProgramDecisionV1(
                    target_index=0,
                    start_attack=True,
                    queue_op=QueueLaneOp.SET,
                    queue_action=HEROIC_STRIKE,
                    gcd_action=WHIRLWIND,
                ),
            ),
        )
    )
    wire = frozen_two_wave_cat_residual_sequence_wire_v1(policy)
    restored = two_wave_cat_residual_sequence_from_dict_v1(
        json.loads(json.dumps(wire))
    )

    assert restored == policy
    assert wire["schema"] == SCHEMA
    assert wire["contract"]["arrival_or_future_or_environment_control_fields_used"] == []
    assert "arrival_ms" not in json.dumps(wire)
    assert "sha256" not in json.dumps(wire).lower()


def test_runtime_binding_is_fresh_and_uses_explicit_policy_and_build_identity() -> None:
    zero = _policy(policy_id="explicit-policy")
    left, left_binding = build_two_wave_cat_residual_sequence_runtime_v1(
        zero, cat_resolver_factory=_StatefulCatResolver
    )

    assert left.origin is ProgramOriginV1.SEARCHED_REACTIVE
    assert left.selector.binding_id == left_binding.binding_id
    assert left_binding.open_session() is not left_binding.open_session()
    assert left.selector.binding_id == (
        "development-two-wave-cat-residual-sequence::"
        "explicit-policy::fury-build-exact"
    )
    assert "exact-build:fury-build-exact" in left.source_refs
    assert "sha256" not in left.program_key().lower()


def test_replacement_repairs_eager_cat_last_gcd_on_the_next_epoch() -> None:
    policy = _policy(
        (
            _step(
                "ww",
                "wave-1",
                _hp_guard(0, gte=1),
                WHIRLWIND,
                target_index=0,
            ),
        )
    )
    cat = _EagerLastGcdCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    replaced = session(_observation(0, wave=1), _available())

    assert replaced.gcd_action == WHIRLWIND
    # Cat proposed Bloodthirst and eagerly wrote it, but the residual restores
    # the value Cat saw at the start of this epoch until the real action is
    # submitted at the next decision boundary.
    assert cat.last_gcd_action == "warrior.execute"

    fallback = session(_observation(100, wave=1), _available())

    assert fallback is cat.decisions[1]
    assert cat.seen_last_gcd_before_propose == [
        "warrior.execute",
        "warrior.whirlwind",
    ]
    assert cat.last_gcd_action == "warrior.bloodthirst"


def test_wait_replacement_restores_prior_without_submitting_a_gcd() -> None:
    policy = _policy(
        (
            CatResidualSequenceStepV1(
                step_id="one-wait",
                wave_id="wave-1",
                guard=_hp_guard(0, gte=1),
                decision=ProgramDecisionV1(wait_ms=25),
            ),
        )
    )
    cat = _EagerLastGcdCatResolver(prior="warrior.execute")
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    waiting = session(_observation(0, wave=1), _available())
    assert waiting.wait_ms == 25
    assert cat.last_gcd_action == "warrior.execute"

    session(_observation(100, wave=1), _available())
    assert cat.seen_last_gcd_before_propose == [
        "warrior.execute",
        "warrior.execute",
    ]


def test_overpower_uses_cat_v5_reverse_mapping_for_continuation() -> None:
    policy = _policy(
        (
            _step(
                "overpower",
                "wave-1",
                _hp_guard(0, gte=1),
                OVERPOWER,
                target_index=0,
            ),
        )
    )
    cat = _EagerLastGcdCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    selected = session(_observation(0, wave=1), _available())
    session(_observation(100, wave=1), _available())

    assert selected.gcd_action == OVERPOWER
    assert cat.seen_last_gcd_before_propose[1] == "warrior.overpower"


def test_recklessness_uses_searched_burst_mapping_for_continuation() -> None:
    policy = _policy(
        (
            _step(
                "recklessness",
                "wave-1",
                _hp_guard(0, gte=1),
                RECKLESSNESS,
                target_index=0,
            ),
        )
    )
    cat = _EagerLastGcdCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    selected = session(_observation(0, wave=1), _available())
    session(_observation(100, wave=1), _available())

    assert selected.gcd_action == RECKLESSNESS
    assert cat.seen_last_gcd_before_propose[1] == "warrior.recklessness"


def test_step_can_require_the_current_cat_proposed_gcd_without_waiting() -> None:
    step = CatResidualSequenceStepV1(
        step_id="replace-battle-shout",
        wave_id="wave-1",
        guard=_hp_guard(0, gte=1),
        decision=ProgramDecisionV1(gcd_action=WHIRLWIND),
        expected_cat_gcd_action=ActionRef(spell_id=25289),
    )
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(
        _policy((step,)), cat
    )

    first = session(_observation(0, wave=1), _available())

    assert first is cat.decisions[0]
    assert session.executed_step_keys == ()
    assert session.audit_events[-1]["reason"] == "CAT_SOURCE_GCD_MISMATCH"


def test_wrong_wave_target_and_wait_semantic_guard_are_rejected() -> None:
    with pytest.raises(ValueError, match="outside its wave"):
        _policy(
            (
                _step(
                    "wrong-target",
                    "wave-1",
                    _hp_guard(0, gte=1),
                    WHIRLWIND,
                    target_index=1,
                ),
            )
        )

    waiting_guard = ObservableCausalGuardV1(target_index=0, target_attackable_is=True)
    with pytest.raises(ValueError, match="SKIP_PLAN"):
        CatResidualSequenceStepV1(
            step_id="waiting",
            wave_id="wave-1",
            guard=waiting_guard,
            decision=ProgramDecisionV1(gcd_action=WHIRLWIND),
        )


def test_future_control_field_is_rejected_before_cat_session_advances() -> None:
    cat = _StatefulCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(_policy(), cat)

    with pytest.raises(
        DevelopmentTwoWaveCatResidualSequenceV1Error,
        match="forbidden fields",
    ):
        session(
            _observation(0, wave=1, extra_state={"arrival_ms": 500}),
            _available(),
        )
    assert cat.calls == 0


def test_step_ids_are_wave_local_and_each_wave_may_have_different_length() -> None:
    policy = DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id="wave-local-ids",
        exact_build_id="build",
        waves=WAVES,
        steps=(
            _step("same", "wave-1", _hp_guard(0, gte=1), WHIRLWIND, target_index=0),
            _step("extra", "wave-1", _hp_guard(0, gte=1), EXECUTE, target_index=0),
            _step("same", "wave-2", _hp_guard(1, gte=1), SLAM, target_index=1),
        ),
    )

    assert [step.wave_id for step in policy.steps] == [
        "wave-1",
        "wave-1",
        "wave-2",
    ]


def test_public_trace_and_execution_feedback_preserve_actual_cat_continuation() -> None:
    policy = _policy(
        (
            _step(
                "parent-ww",
                "wave-1",
                _hp_guard(0, gte=1),
                WHIRLWIND,
                target_index=0,
            ),
        )
    )
    cat = _EagerLastGcdCatResolver(prior="warrior.execute")
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(policy, cat)

    parent = session(_observation(0, wave=1), _available())
    first_trace = session.last_decision_trace
    assert parent.gcd_action == WHIRLWIND
    assert first_trace is not None
    assert first_trace.decision_index == 0
    assert first_trace.cat_decision.gcd_action == BLOODTHIRST
    assert first_trace.parent_decision.gcd_action == WHIRLWIND
    assert first_trace.executed_step_keys_before == ()
    assert first_trace.executed_step_keys_after == (("wave-1", "parent-ww"),)

    fallback = session(_observation(100, wave=1), _available())
    second_trace = session.last_decision_trace
    assert fallback.gcd_action == BLOODTHIRST
    assert second_trace is not None
    assert second_trace.executed_step_keys_before == (("wave-1", "parent-ww"),)
    assert second_trace.executed_step_keys_after == (("wave-1", "parent-ww"),)

    # A development teacher actually returns Slam at this epoch.  Feed back
    # that executed decision so the same Cat session sees Slam, not its
    # unexecuted Bloodthirst proposal, at the following decision boundary.
    session.record_last_executed_decision_v1(
        ProgramDecisionV1(target_index=0, gcd_action=SLAM)
    )
    session(_observation(200, wave=1), _available())
    assert cat.seen_last_gcd_before_propose == [
        "warrior.execute",
        "warrior.whirlwind",
        "warrior.slam",
    ]


def test_execution_feedback_is_immediate_and_single_use() -> None:
    cat = _EagerLastGcdCatResolver()
    session = DevelopmentTwoWaveCatResidualSequenceSessionV1(_policy(), cat)

    with pytest.raises(
        DevelopmentTwoWaveCatResidualSequenceV1Error,
        match="immediately follow",
    ):
        session.record_last_executed_decision_v1(
            ProgramDecisionV1(gcd_action=WHIRLWIND)
        )

    session(_observation(0, wave=1), _available())
    session.record_last_executed_decision_v1(
        ProgramDecisionV1(gcd_action=WHIRLWIND)
    )
    with pytest.raises(
        DevelopmentTwoWaveCatResidualSequenceV1Error,
        match="immediately follow",
    ):
        session.record_last_executed_decision_v1(
            ProgramDecisionV1(gcd_action=EXECUTE)
        )
