import json

import pytest

from o2o_dps.causal_action_program_v1 import ProgramDecisionV1, ProgramOriginV1
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.development_two_wave_segment_policy_v1 import (
    SCOPE,
    DevelopmentTwoWaveSegmentPolicyV1,
    DevelopmentTwoWaveSegmentPolicyV1Error,
    DevelopmentTwoWaveSegmentSessionV1,
    SegmentWaveV1,
    WaveConditionedSequenceStepV1,
    build_two_wave_segment_runtime_v1,
    freeze_two_wave_segment_policy_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp, ScheduledActionPlan


BLOODTHIRST = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)
EXECUTE = ActionRef(spell_id=20662)
HEROIC_STRIKE = ActionRef(spell_id=25286, tag=1)
CLEAVE = ActionRef(spell_id=20569, tag=1)
DEATH_WISH = ActionRef(spell_id=12328)


class _CatResolver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, observation, available) -> ProgramDecisionV1:
        del available
        self.calls += 1
        return ProgramDecisionV1(
            target_index=observation.state.get("target_index", 0),
            gcd_action=BLOODTHIRST,
        )


def _available(
    *,
    whirlwind_ready_in_ms: int = 0,
    whirlwind_legal: bool = True,
    include_whirlwind: bool = True,
    death_wish_ready_in_ms: int = 0,
) -> tuple[AvailableAction, ...]:
    actions = [
        (BLOODTHIRST, True, 0, True),
        (EXECUTE, True, 0, True),
        (HEROIC_STRIKE, True, 0, False),
        (CLEAVE, True, 0, False),
        (DEATH_WISH, True, death_wish_ready_in_ms, False),
    ]
    if include_whirlwind:
        actions.append(
            (WHIRLWIND, whirlwind_legal, whirlwind_ready_in_ms, True)
        )
    return tuple(
        AvailableAction(
            index=index,
            action=action,
            label=str(action.spell_id),
            legal=legal,
            ready_in_ms=ready,
            triggers_gcd=triggers_gcd,
        )
        for index, (action, legal, ready, triggers_gcd) in enumerate(actions)
    )


def _observation(
    time_ms: int,
    *,
    wave: int,
    wave_one_health: float = 100.0,
    wave_two_health: float = 100.0,
    attackable: bool = True,
    precombat: bool = False,
    prefix_dps: float | None = 10.0,
    extra_state: dict | None = None,
) -> CausalLiveStateProjectionV1:
    wave_one_dead = wave >= 2
    semantics = [
        {
            "target_index": 0,
            "maximum_health": 100.0,
            "current_health": 0.0 if wave_one_dead else wave_one_health,
            "attackable": attackable if wave == 1 else False,
            "dead": wave_one_dead,
        }
    ]
    life = [
        {
            "target_index": 0,
            "initial_health": 100.0,
            "current_health": 0.0 if wave_one_dead else wave_one_health,
            "dead": wave_one_dead,
        }
    ]
    mapping = (0,)
    target_index = 0
    if wave >= 2:
        semantics.append(
            {
                "target_index": 1,
                "maximum_health": 100.0,
                "current_health": wave_two_health,
                "attackable": attackable,
                "dead": False,
            }
        )
        life.append(
            {
                "target_index": 1,
                "initial_health": 100.0,
                "current_health": wave_two_health,
                "dead": False,
            }
        )
        mapping = (0, 1)
        target_index = 1
    state = {
        "time_ms": time_ms,
        "target_index": target_index,
        "precombat": {"active": precombat, "relative_time_ms": -500},
        "dynamic_target_semantics": {"targets": semantics},
        "dynamic_team_background": {
            "targets": life,
            "prefix_damage_rate": {
                "schema": "o2o_policy_prefix_damage_rate/v1",
                "combined_damage_per_second": prefix_dps,
            },
        },
    }
    if extra_state:
        state.update(extra_state)
    return CausalLiveStateProjectionV1(
        state=state,
        policy_to_simulator_target_index=mapping,
        visibility_cutoff_ms=time_ms,
    )


def _resource_guard(target_index: int) -> ObservableCausalGuardV1:
    return ObservableCausalGuardV1(
        target_index=target_index,
        target_hp_pct_gte=50,
        target_attackable_is=True,
        estimated_remaining_attackable_gte_ms=3_000,
        action_ready=DEATH_WISH,
        false_semantics=SKIP_PLAN,
    )


def _full_policy() -> DevelopmentTwoWaveSegmentPolicyV1:
    return DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="two-wave-test",
        exact_build_id="fury-build-exact",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "w1-rotation",
                "wave-1",
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    target_index=0,
                    queue_op=QueueLaneOp.SET,
                    queue_action=HEROIC_STRIKE,
                    gcd_action=WHIRLWIND,
                ),
            ),
            WaveConditionedSequenceStepV1(
                "w1-burst",
                "wave-1",
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    off_gcd_actions=(DEATH_WISH,),
                    guard=_resource_guard(0),
                ),
                resource_id="spell.death-wish",
            ),
            WaveConditionedSequenceStepV1(
                "w2-burst-retry",
                "wave-2",
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    off_gcd_actions=(DEATH_WISH,),
                    guard=_resource_guard(1),
                ),
                resource_id="spell.death-wish",
            ),
            WaveConditionedSequenceStepV1(
                "w2-rotation",
                "wave-2",
                ScheduledActionPlan(
                    at_or_after_ms=0,
                    target_index=1,
                    queue_op=QueueLaneOp.SET,
                    queue_action=CLEAVE,
                    gcd_action=EXECUTE,
                ),
            ),
        ),
    )


def _wait_policy(*, wave_one_at: int = 0, wave_two_at: int = 0):
    return DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="clock-test",
        exact_build_id="build",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "w1", "wave-1", ScheduledActionPlan(wave_one_at, wait_ms=50)
            ),
            WaveConditionedSequenceStepV1(
                "w2", "wave-2", ScheduledActionPlan(wave_two_at, wait_ms=60)
            ),
        ),
    )


def test_two_wave_sequence_retains_skipped_resource_and_retries_on_wave_two():
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(_full_policy(), cat)
    actions = _available()

    first = session(_observation(1_000, wave=1, wave_one_health=20), actions)
    assert first.target_index == 0
    assert first.queue_op is QueueLaneOp.SET
    assert first.queue_action == HEROIC_STRIKE
    assert first.gcd_action == WHIRLWIND

    skipped = session(_observation(1_100, wave=1, wave_one_health=20), actions)
    assert skipped.gcd_action == BLOODTHIRST
    assert session.used_resource_ids == ()
    skip = next(
        row
        for row in session.audit_events
        if row["kind"] == "GUARD_SKIP_RESOURCE_RETAINED"
    )
    assert set(skip["failed_predicates"]) == {
        "target_hp_pct_gte",
        "estimated_remaining_attackable_gte_ms",
    }

    retried = session(_observation(5_000, wave=2), actions)
    assert retried.gcd_action == BLOODTHIRST
    assert tuple(row.action for row in retried.optional_off_gcd_prefixes) == (
        DEATH_WISH,
    )
    assert session.used_resource_ids == ("spell.death-wish",)

    second = session(_observation(5_100, wave=2), actions)
    assert second.target_index == 1
    assert second.queue_action == CLEAVE
    assert second.gcd_action == EXECUTE
    exhausted = session(_observation(5_200, wave=2), actions)
    assert exhausted.gcd_action == BLOODTHIRST
    assert cat.calls == 3


def test_schedule_clock_is_relative_to_each_waves_first_causal_active_state():
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(
        _wait_policy(wave_one_at=500, wave_two_at=300), cat
    )

    before_one = session(_observation(10_000, wave=1), _available())
    assert before_one.wait_ms == 100
    assert cat.calls == 0
    assert session.next_step_index == 0
    at_one = session(_observation(10_500, wave=1), _available())
    assert at_one.wait_ms == 50
    assert session.next_step_index == 1

    before_two = session(_observation(20_000, wave=2), _available())
    assert before_two.wait_ms == 100
    assert session.next_step_index == 1
    at_two = session(_observation(20_300, wave=2), _available())
    assert at_two.wait_ms == 60
    assert session.next_step_index == 2


def test_precombat_delegates_to_cat_without_consuming_the_combat_sequence():
    policy = _wait_policy()
    precombat_session = DevelopmentTwoWaveSegmentSessionV1(
        policy, _CatResolver()
    )
    selected = precombat_session(
        _observation(0, wave=1, attackable=False, precombat=True), _available()
    )
    assert selected.gcd_action == BLOODTHIRST
    assert precombat_session.next_step_index == 0
    assert any(
        row["kind"] == "EXACT_CAT_FALLBACK"
        and row["reason"] == "SEQUENCE_NOT_STARTED_PRECOMBAT"
        for row in precombat_session.audit_events
    )

    cat = _CatResolver()
    live_session = DevelopmentTwoWaveSegmentSessionV1(policy, cat)
    fallback = live_session(
        _observation(0, wave=1, attackable=False, precombat=False), _available()
    )
    assert fallback.gcd_action == BLOODTHIRST
    assert live_session.next_step_index == 0
    assert cat.calls == 1
    assert not any(
        row["kind"] == "SEQUENCE_STEP_WAVE_PASSED"
        for row in live_session.audit_events
    )


def test_recognized_but_not_ready_action_waits_without_advancing_or_calling_cat():
    policy = DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="ready-test",
        exact_build_id="build",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "ww",
                "wave-1",
                ScheduledActionPlan(0, target_index=0, gcd_action=WHIRLWIND),
            ),
            WaveConditionedSequenceStepV1(
                "later", "wave-2", ScheduledActionPlan(0, wait_ms=50)
            ),
        ),
    )
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(policy, cat)

    waiting = session(
        _observation(0, wave=1),
        _available(whirlwind_ready_in_ms=650),
    )
    assert waiting.wait_ms == 100
    assert waiting.start_attack is True
    assert waiting.target_index == 0
    assert session.next_step_index == 0
    assert cat.calls == 0
    selected = session(_observation(100, wave=1), _available())
    assert selected.gcd_action == WHIRLWIND
    assert session.next_step_index == 1


def test_unrecognized_sequence_action_uses_exact_cat_without_consuming_step():
    policy = DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="mismatch-test",
        exact_build_id="build",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "ww",
                "wave-1",
                ScheduledActionPlan(0, target_index=0, gcd_action=WHIRLWIND),
            ),
            WaveConditionedSequenceStepV1(
                "later", "wave-2", ScheduledActionPlan(0, wait_ms=50)
            ),
        ),
    )
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(policy, cat)

    decision = session(
        _observation(0, wave=1), _available(include_whirlwind=False)
    )
    assert decision.gcd_action == BLOODTHIRST
    assert session.next_step_index == 0
    assert cat.calls == 1


def test_native_present_but_illegal_cooldown_action_waits_without_calling_cat():
    policy = DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="native-legality-test",
        exact_build_id="build",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "ww",
                "wave-1",
                ScheduledActionPlan(0, target_index=0, gcd_action=WHIRLWIND),
            ),
            WaveConditionedSequenceStepV1(
                "later", "wave-2", ScheduledActionPlan(0, wait_ms=50)
            ),
        ),
    )
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(policy, cat)

    waiting = session(
        _observation(0, wave=1),
        _available(whirlwind_legal=False, whirlwind_ready_in_ms=650),
    )
    assert waiting.wait_ms == 100
    assert waiting.start_attack is True
    assert waiting.target_index == 0
    assert session.next_step_index == 0
    assert cat.calls == 0


def test_guarded_resource_cooldown_waits_but_low_lifetime_skips_and_retains():
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(_full_policy(), cat)
    session(_observation(0, wave=1), _available())

    waiting = session(
        _observation(100, wave=1),
        _available(death_wish_ready_in_ms=700),
    )
    assert waiting.wait_ms == 100
    assert session.next_step_index == 1
    assert cat.calls == 0

    low = session(
        _observation(200, wave=1, wave_one_health=20),
        _available(death_wish_ready_in_ms=700),
    )
    assert low.gcd_action == BLOODTHIRST
    assert session.next_step_index == 2
    assert session.used_resource_ids == ()


def test_resource_guard_waits_for_a_causal_prefix_rate_instead_of_skipping():
    cat = _CatResolver()
    session = DevelopmentTwoWaveSegmentSessionV1(_full_policy(), cat)
    session(_observation(0, wave=1), _available())

    unknown = session(
        _observation(100, wave=1, prefix_dps=None), _available()
    )
    assert unknown.wait_ms == 100
    assert session.next_step_index == 1
    assert session.used_resource_ids == ()
    assert cat.calls == 0

    selected = session(
        _observation(200, wave=1, prefix_dps=10.0), _available()
    )
    assert tuple(row.action for row in selected.optional_off_gcd_prefixes) == (
        DEATH_WISH,
    )
    assert session.next_step_index == 2
    assert session.used_resource_ids == ("spell.death-wish",)


def test_freeze_strips_guide_metadata_and_artifact_is_explicitly_non_route():
    raw = (
        WaveConditionedSequenceStepV1(
            "w1",
            "wave-1",
            ScheduledActionPlan(
                0,
                wait_ms=50,
                guide_provenance=("offline-expert:tonyniu",),
                guide_priority=3.5,
            ),
        ),
        WaveConditionedSequenceStepV1(
            "w2", "wave-2", ScheduledActionPlan(0, wait_ms=50)
        ),
    )
    policy = freeze_two_wave_segment_policy_v1(
        policy_id="frozen",
        exact_build_id="build",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=raw,
    )
    artifact = policy.to_dict()

    assert all(not step.plan.guide_provenance for step in policy.steps)
    assert artifact["scope"] == SCOPE
    assert artifact["contract"]["upper_kara_full_route_claim"] is False
    assert artifact["contract"]["step_at_or_after_ms_clock"].startswith(
        "RELATIVE_TO_FIRST_CAUSAL"
    )


def test_forbidden_future_or_remaining_fields_are_rejected_before_selection():
    session = DevelopmentTwoWaveSegmentSessionV1(_wait_policy(), _CatResolver())
    contaminated = _observation(
        0,
        wave=1,
        extra_state={"remaining_ms": 5_000, "arrival_ms": 123},
    )

    with pytest.raises(
        DevelopmentTwoWaveSegmentPolicyV1Error,
        match="forbidden fields",
    ):
        session(contaminated, _available())
    assert "arrival_ms" not in json.dumps(_full_policy().to_dict())


def test_runtime_marks_frozen_stateful_sequence_as_searched_reactive():
    program, binding = build_two_wave_segment_runtime_v1(
        _full_policy(), cat_resolver_factory=_CatResolver
    )

    assert program.origin is ProgramOriginV1.SEARCHED_REACTIVE
    assert program.selector.binding_id == binding.binding_id
    assert binding.open_session() is not binding.open_session()


def test_runtime_identity_binds_the_complete_frozen_sequence_not_only_policy_id():
    waves = (
        SegmentWaveV1("wave-1", (0,)),
        SegmentWaveV1("wave-2", (1,)),
    )

    def policy(wait_ms: int) -> DevelopmentTwoWaveSegmentPolicyV1:
        return DevelopmentTwoWaveSegmentPolicyV1(
            policy_id="same-id",
            exact_build_id="build",
            waves=waves,
            steps=(
                WaveConditionedSequenceStepV1(
                    "w1", "wave-1", ScheduledActionPlan(0, wait_ms=wait_ms)
                ),
                WaveConditionedSequenceStepV1(
                    "w2", "wave-2", ScheduledActionPlan(0, wait_ms=50)
                ),
            ),
        )

    left, _ = build_two_wave_segment_runtime_v1(
        policy(10), cat_resolver_factory=_CatResolver
    )
    right, _ = build_two_wave_segment_runtime_v1(
        policy(20), cat_resolver_factory=_CatResolver
    )

    assert left.program_id == right.program_id
    assert left.selector == right.selector
    assert left.program_key() != right.program_key()
    assert any(
        row.startswith("frozen-policy-sha256:") for row in left.source_refs
    )


def test_illegal_action_on_dead_current_target_retargets_without_advancing_cursor():
    policy = DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="retarget-test",
        exact_build_id="build",
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "w1", "wave-1", ScheduledActionPlan(0, wait_ms=1)
            ),
            WaveConditionedSequenceStepV1(
                "ww", "wave-2", ScheduledActionPlan(0, target_index=1, gcd_action=WHIRLWIND)
            ),
        ),
    )
    session = DevelopmentTwoWaveSegmentSessionV1(policy, _CatResolver())
    # Move the cursor to wave two because wave one is already dead.
    dead_current = _observation(5_000, wave=2)
    dead_state = dict(dead_current.state)
    dead_state["target_index"] = 0
    dead_current = CausalLiveStateProjectionV1(
        state=dead_state,
        policy_to_simulator_target_index=dead_current.policy_to_simulator_target_index,
        visibility_cutoff_ms=dead_current.visibility_cutoff_ms,
    )

    retarget = session(
        dead_current,
        _available(whirlwind_legal=False, whirlwind_ready_in_ms=0),
    )
    assert retarget.wait_ms == 100
    assert retarget.target_index == 1
    assert retarget.start_attack is True
    assert session.next_step_index == 1

    selected = session(_observation(5_100, wave=2), _available())
    assert selected.gcd_action == WHIRLWIND
    assert session.next_step_index == 2
