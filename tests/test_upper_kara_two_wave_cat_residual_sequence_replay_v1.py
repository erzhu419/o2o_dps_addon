from __future__ import annotations

from dataclasses import replace

import pytest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.upper_kara_heterogeneous_two_wave_case_v1 import (
    HeterogeneousTwoWaveObservationProjectorV1,
)
from o2o_dps.upper_kara_heterogeneous_two_wave_remote_v7 import (
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from o2o_dps.upper_kara_two_wave_cat_residual_sequence_replay_v1 import (
    UpperKaraTwoWaveCatResidualSequenceReplayV1Error,
    build_two_wave_cat_residual_sequence_program_replay_factory_v1,
)


BUILD_ID = "live_bonereaver"
LOADOUT_ID = "contra_turtle_burst__mighty_rage"


def _zero_policy(
    *,
    policy_id: str = "cat-residual-zero-test",
    build_id: str = BUILD_ID,
) -> DevelopmentTwoWaveCatResidualSequenceV1:
    return DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id=policy_id,
        exact_build_id=build_id,
        waves=(
            CatResidualSequenceWaveV1("wave-1", (0, 1)),
            CatResidualSequenceWaveV1("wave-2", (2,)),
        ),
        steps=(),
    )


def _case(seed: int):
    return build_upper_kara_heterogeneous_two_wave_burst_case_v7(
        seed,
        build_id=BUILD_ID,
        loadout_id=LOADOUT_ID,
        first_wave_arrival_ms=0,
    )


def _observation(now_ms: int) -> CausalLiveStateProjectionV1:
    return CausalLiveStateProjectionV1(
        state={
            "time_ms": now_ms,
            "target_index": 0,
            "precombat": {"active": False, "relative_time_ms": now_ms - 3_000},
            "dynamic_target_semantics": {
                "targets": [
                    {
                        "target_index": 0,
                        "maximum_health": 100.0,
                        "current_health": 100.0,
                        "attackable": True,
                        "dead": False,
                    },
                    {
                        "target_index": 1,
                        "maximum_health": 100.0,
                        "current_health": 100.0,
                        "attackable": True,
                        "dead": False,
                    },
                ]
            },
        },
        policy_to_simulator_target_index=(0, 1),
        visibility_cutoff_ms=now_ms,
    )


class _StatefulCat:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, observation, available):
        del available
        self.calls += 1
        return ProgramDecisionV1(
            target_index=observation.state["target_index"],
            wait_ms=self.calls,
        )


def _cat_binding(opened: list[_StatefulCat]):
    def open_cat() -> _StatefulCat:
        session = _StatefulCat()
        opened.append(session)
        return session

    return ImportedReactiveProgramBindingV1(
        binding_id=CAT_POLICY_ID,
        source_policy_id=CAT_POLICY_ID,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=open_cat,
    )


def _exact_cat_program() -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id="exact-cat-replay-test",
        selector=ImportedReactiveSelectorV1(
            binding_id=CAT_POLICY_ID,
            source_policy_id=CAT_POLICY_ID,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
        source_refs=(CAT_POLICY_ID,),
    )


def _install_fake_native(monkeypatch, opened, projectors):
    from o2o_dps import upper_kara_two_wave_cat_residual_sequence_replay_v1 as module

    monkeypatch.setattr(
        module,
        "build_imported_incumbent_bindings_v1",
        lambda *args, **kwargs: (_cat_binding(opened),),
    )

    class FakeNativeReplay:
        def __init__(
            self,
            bridge_factory,
            case_factory,
            projector,
            *,
            imported_bindings=(),
            **kwargs,
        ):
            del bridge_factory, kwargs
            self.case_factory = case_factory
            self.projector = projector
            projectors.append(projector)
            self.bindings = {
                binding.binding_id: binding for binding in imported_bindings
            }

        def replay(self, seed, program, *, max_decisions):
            del max_decisions
            self.case_factory(seed)
            binding = self.bindings[program.selector.binding_id]
            session = binding.open_session()
            return tuple(
                session(_observation(now_ms), ())
                for now_ms in (3_000, 3_001)
            )

    monkeypatch.setattr(
        module, "NativeDynamicV3ActionProgramReplayV1", FakeNativeReplay
    )


def test_empty_residual_is_exact_cat_with_fresh_session_and_projector(
    monkeypatch,
) -> None:
    policy = _zero_policy()
    program, _ = build_two_wave_cat_residual_sequence_runtime_v1(
        policy, cat_resolver_factory=_StatefulCat
    )
    cases = {seed: _case(seed) for seed in (101, 103)}
    opened: list[_StatefulCat] = []
    projectors: list[object] = []
    _install_fake_native(monkeypatch, opened, projectors)
    replay = build_two_wave_cat_residual_sequence_program_replay_factory_v1(
        BUILD_ID,
        {policy.policy_id: policy},
    )(LOADOUT_ID, cases)

    first = replay.replay(101, program)
    exact = replay.replay(101, _exact_cat_program())
    second = replay.replay(103, program)

    assert first == exact == second
    assert [row.wait_ms for row in first] == [1, 2]
    assert len(opened) == 3
    assert len({id(row) for row in opened}) == 3
    assert len(projectors) == 3
    assert len({id(row) for row in projectors}) == 3
    assert all(
        isinstance(row, HeterogeneousTwoWaveObservationProjectorV1)
        for row in projectors
    )
    assert replay.last_observation_audit == {
        "simulator_seed": 103,
        "projector": "HETEROGENEOUS_TWO_WAVE",
        "captured_target_indexes": [],
        "hidden_selected_target_substitutions": 0,
        "last_hidden_selected_target_index": None,
    }


def test_replay_rejects_program_identity_that_differs_from_frozen_policy(
    monkeypatch,
) -> None:
    policy = _zero_policy()
    program, _ = build_two_wave_cat_residual_sequence_runtime_v1(
        policy, cat_resolver_factory=_StatefulCat
    )
    changed = replace(program, source_refs=program.source_refs + ("changed",))
    opened: list[_StatefulCat] = []
    _install_fake_native(monkeypatch, opened, [])
    replay = build_two_wave_cat_residual_sequence_program_replay_factory_v1(
        BUILD_ID,
        {policy.policy_id: policy},
    )(LOADOUT_ID, {107: _case(107)})

    with pytest.raises(
        UpperKaraTwoWaveCatResidualSequenceReplayV1Error,
        match="program identity differs",
    ):
        replay.replay(107, changed)

    assert opened == []


def test_factory_rejects_noncanonical_wire_identity(monkeypatch) -> None:
    from o2o_dps import upper_kara_two_wave_cat_residual_sequence_replay_v1 as module

    policy = _zero_policy()
    different = _zero_policy(policy_id="different")
    monkeypatch.setattr(
        module,
        "two_wave_cat_residual_sequence_from_dict_v1",
        lambda wire: different,
    )

    with pytest.raises(
        UpperKaraTwoWaveCatResidualSequenceReplayV1Error,
        match="canonical wire round-trip",
    ):
        build_two_wave_cat_residual_sequence_program_replay_factory_v1(
            BUILD_ID,
            {policy.policy_id: policy},
        )


@pytest.mark.parametrize(
    ("registry", "message"),
    [
        ({"wrong-key": _zero_policy()}, "keyed by policy_id"),
        (
            {
                "cat-residual-zero-test": _zero_policy(
                    build_id="clean_dual_weapon_probe"
                )
            },
            "exact-build residual policy registry",
        ),
    ],
)
def test_factory_rejects_registry_identity_mismatch(registry, message) -> None:
    with pytest.raises(ValueError, match=message):
        build_two_wave_cat_residual_sequence_program_replay_factory_v1(
            BUILD_ID, registry
        )


def test_replay_rejects_case_build_identity_mismatch(monkeypatch) -> None:
    policy = _zero_policy()
    program, _ = build_two_wave_cat_residual_sequence_runtime_v1(
        policy, cat_resolver_factory=_StatefulCat
    )
    case = _case(109)
    changed_spec = dict(case.case_spec)
    changed_spec["build_id"] = "clean_dual_weapon_probe"
    changed_case = replace(case, case_spec=changed_spec)
    _install_fake_native(monkeypatch, [], [])
    replay = build_two_wave_cat_residual_sequence_program_replay_factory_v1(
        BUILD_ID,
        {policy.policy_id: policy},
    )(LOADOUT_ID, {109: changed_case})

    with pytest.raises(
        UpperKaraTwoWaveCatResidualSequenceReplayV1Error,
        match="case build identity differs",
    ):
        replay.replay(109, program)
