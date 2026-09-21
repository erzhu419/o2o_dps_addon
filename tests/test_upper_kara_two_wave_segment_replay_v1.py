from __future__ import annotations

from o2o_dps.causal_action_program_v1 import (
    ImportedReactiveProgramBindingV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    causal_action_program_from_dict_v1,
)
from o2o_dps.development_two_wave_segment_policy_v1 import (
    DevelopmentTwoWaveSegmentPolicyV1,
    SegmentWaveV1,
    WaveConditionedSequenceStepV1,
    build_two_wave_segment_runtime_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.upper_kara_two_wave_baseline_panel_v1 import (
    baseline_policy_ids_for_build_v1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)
from o2o_dps.upper_kara_two_wave_segment_replay_v1 import (
    build_two_wave_segment_program_replay_factory_v1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import (
    imported_incumbent_programs_v1,
)
from o2o_dps.wave_action_schedule_v1 import ScheduledActionPlan


BUILD_ID = "live_bonereaver"
LOADOUT_ID = "contra_turtle_burst__no_potion"
OBSERVATION_CONTRACT = "policy_observation_causal_projection/v1"


def _policy() -> DevelopmentTwoWaveSegmentPolicyV1:
    return DevelopmentTwoWaveSegmentPolicyV1(
        policy_id="searched-reactive-two-wave",
        exact_build_id=BUILD_ID,
        waves=(SegmentWaveV1("wave-1", (0,)), SegmentWaveV1("wave-2", (1,))),
        steps=(
            WaveConditionedSequenceStepV1(
                "wave-1-first",
                "wave-1",
                ScheduledActionPlan(0, wait_ms=11),
            ),
            WaveConditionedSequenceStepV1(
                "wave-2-first",
                "wave-2",
                ScheduledActionPlan(0, wait_ms=22),
            ),
        ),
    )


def _observation() -> CausalLiveStateProjectionV1:
    return CausalLiveStateProjectionV1(
        state={
            "time_ms": 1_000,
            "target_index": 0,
            "precombat": {"active": False},
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "maximum_health": 100.0,
                        "current_health": 100.0,
                        "attackable": True,
                        "dead": False,
                    }
                ]
            },
            "dynamic_team_background": {
                "targets": [
                    {
                        "target_index": 0,
                        "initial_health": 100.0,
                        "current_health": 100.0,
                        "dead": False,
                    }
                ]
            },
        },
        policy_to_simulator_target_index=(0,),
        visibility_cutoff_ms=1_000,
    )


class _Projector:
    hidden_selected_target_substitutions = 0
    last_hidden_selected_target_index = None

    def __call__(self, state, available):
        del state, available
        return _observation()


class _BindingOpeningReplay:
    opened_source_policy_ids: list[str] = []

    def __init__(
        self,
        bridge_factory,
        case_factory,
        projector,
        *,
        imported_bindings=(),
        **kwargs,
    ):
        del bridge_factory, case_factory, projector, kwargs
        self.bindings = {
            binding.binding_id: binding for binding in imported_bindings
        }

    def replay(self, seed, program, *, max_decisions):
        del seed, max_decisions
        selector = program.selector
        binding_id = getattr(selector, "binding_id", None)
        if binding_id is None:
            binding_id = selector.imported_fallback.binding_id
        binding = self.bindings[binding_id]
        self.opened_source_policy_ids.append(binding.source_policy_id)
        return binding.open_session()(_observation(), ())


def _fake_imported_bindings(opened: list[str]):
    def resolver(policy_id: str):
        def open_resolver():
            opened.append(policy_id)
            return lambda observation, available: ProgramDecisionV1(wait_ms=77)

        return open_resolver

    return tuple(
        ImportedReactiveProgramBindingV1(
            binding_id=policy_id,
            source_policy_id=policy_id,
            observation_contract_id=OBSERVATION_CONTRACT,
            resolver_factory=resolver(policy_id),
        )
        for policy_id in baseline_policy_ids_for_build_v1(BUILD_ID)
    )


def test_frozen_searched_reactive_replay_opens_a_fresh_cursor_for_every_seed(
    monkeypatch,
):
    from o2o_dps import upper_kara_two_wave_segment_replay_v1 as replay_module

    policy = _policy()
    program, _ = build_two_wave_segment_runtime_v1(
        policy,
        cat_resolver_factory=lambda: (
            lambda observation, available: ProgramDecisionV1(wait_ms=99)
        ),
    )
    frozen = causal_action_program_from_dict_v1(program.to_dict())
    cases = {
        seed: build_upper_kara_continuous_two_wave_burst_case_v1(
            seed,
            build_id=BUILD_ID,
            loadout_id=LOADOUT_ID,
            first_wave_arrival_ms=0,
        )
        for seed in (101, 103)
    }
    opened: list[str] = []
    monkeypatch.setattr(
        replay_module,
        "build_imported_incumbent_bindings_v1",
        lambda *args, **kwargs: _fake_imported_bindings(opened),
    )
    monkeypatch.setattr(
        replay_module,
        "build_continuous_two_wave_observation_projector_v1",
        lambda case: _Projector(),
    )
    monkeypatch.setattr(
        replay_module,
        "NativeDynamicV3ActionProgramReplayV1",
        _BindingOpeningReplay,
    )
    replay = build_two_wave_segment_program_replay_factory_v1(
        BUILD_ID,
        {policy.policy_id: policy},
    )(LOADOUT_ID, cases)

    first = replay.replay(101, frozen)
    second = replay.replay(103, frozen)

    assert frozen.origin is ProgramOriginV1.SEARCHED_REACTIVE
    assert first.wait_ms == 11
    assert second.wait_ms == 11
    # Opening each sequence session also opens a fresh exact-Cat fallback
    # session, even though the first searched step does not call it.
    assert opened == [CAT_POLICY_ID, CAT_POLICY_ID]
    assert replay.last_observation_audit["simulator_seed"] == 103


def test_imported_baseline_uses_its_source_binding_not_searched_registry(
    monkeypatch,
):
    from o2o_dps import upper_kara_two_wave_segment_replay_v1 as replay_module

    policy = _policy()
    case = build_upper_kara_continuous_two_wave_burst_case_v1(
        107,
        build_id=BUILD_ID,
        loadout_id=LOADOUT_ID,
        first_wave_arrival_ms=0,
    )
    opened: list[str] = []
    monkeypatch.setattr(
        replay_module,
        "build_imported_incumbent_bindings_v1",
        lambda *args, **kwargs: _fake_imported_bindings(opened),
    )
    monkeypatch.setattr(
        replay_module,
        "build_continuous_two_wave_observation_projector_v1",
        lambda current_case: _Projector(),
    )
    monkeypatch.setattr(
        replay_module,
        "NativeDynamicV3ActionProgramReplayV1",
        _BindingOpeningReplay,
    )
    replay = build_two_wave_segment_program_replay_factory_v1(
        BUILD_ID,
        {policy.policy_id: policy},
    )(LOADOUT_ID, {107: case})
    baseline = imported_incumbent_programs_v1(BUILD_ID)[1]

    decision = replay.replay(107, baseline)

    assert baseline.origin is ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT
    assert decision.wait_ms == 77
    assert opened == [baseline.selector.source_policy_id]


def test_plain_searched_exact_cat_zero_receives_its_imported_fallback_binding(
    monkeypatch,
):
    from o2o_dps import upper_kara_two_wave_segment_replay_v1 as replay_module

    policy = _policy()
    case = build_upper_kara_continuous_two_wave_burst_case_v1(
        109,
        build_id=BUILD_ID,
        loadout_id=LOADOUT_ID,
        first_wave_arrival_ms=0,
    )
    opened: list[str] = []
    monkeypatch.setattr(
        replay_module,
        "build_imported_incumbent_bindings_v1",
        lambda *args, **kwargs: _fake_imported_bindings(opened),
    )
    monkeypatch.setattr(
        replay_module,
        "build_continuous_two_wave_observation_projector_v1",
        lambda current_case: _Projector(),
    )
    monkeypatch.setattr(
        replay_module,
        "NativeDynamicV3ActionProgramReplayV1",
        _BindingOpeningReplay,
    )
    replay = build_two_wave_segment_program_replay_factory_v1(
        BUILD_ID,
        {policy.policy_id: policy},
    )(LOADOUT_ID, {109: case})
    cat_zero = cat_zero_residual_program_v1(LOADOUT_ID)

    decision = replay.replay(109, cat_zero)

    assert cat_zero.origin is ProgramOriginV1.SEARCHED
    assert decision.wait_ms == 77
    assert opened == [CAT_POLICY_ID]
