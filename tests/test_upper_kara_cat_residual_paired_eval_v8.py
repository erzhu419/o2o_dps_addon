from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import threading
from types import SimpleNamespace

import pytest

from o2o_dps.causal_action_program_v1 import (
    ImportedReactiveProgramBindingV1,
    ProgramDecisionV1,
)
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceStepV1,
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
)
from o2o_dps.development_wave_panel_v1 import PROTOCOL_ID
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    SEED_DERIVATION_ALGORITHM,
    derive_simulator_seed,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.upper_kara_cat_residual_paired_eval_v8 import (
    evaluate_upper_kara_cat_residual_sequence_paired_v8,
)
from o2o_dps.upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from o2o_dps.upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from o2o_dps.wave_action_schedule_v1 import QueueLaneOp
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


BLOODTHIRST = ActionRef(spell_id=23_894)
WHIRLWIND = ActionRef(spell_id=1_680)
REQUEST_SHA256 = "a" * 64


def _policy(*, steps: tuple[CatResidualSequenceStepV1, ...] = ()):
    return DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id="paired-eval-test",
        exact_build_id="live_bonereaver",
        waves=(
            CatResidualSequenceWaveV1("wave-1", (0, 1)),
            CatResidualSequenceWaveV1("wave-2", (2,)),
        ),
        steps=steps,
    )


def _one_whirlwind_step() -> CatResidualSequenceStepV1:
    return CatResidualSequenceStepV1(
        step_id="replace-first-ready-gcd",
        wave_id="wave-1",
        guard=ObservableCausalGuardV1(
            target_index=0,
            target_attackable_is=True,
            action_ready=WHIRLWIND,
            false_semantics=SKIP_PLAN,
        ),
        decision=ProgramDecisionV1(
            target_index=0,
            queue_op=QueueLaneOp.KEEP,
            gcd_action=WHIRLWIND,
        ),
    )


class _FakeCat:
    def __call__(self, observation, available):
        del available
        return ProgramDecisionV1(
            target_index=observation.state["target_index"],
            gcd_action=BLOODTHIRST,
        )


class _FakeBridge:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def _install_fake_native(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expose_seed: bool = False,
    candidate_required_targets_dead: bool = True,
    lane_barrier: threading.Barrier | None = None,
    residual_finished: threading.Event | None = None,
    outcome_seed_offset: int = 0,
) -> None:
    import o2o_dps.upper_kara_cat_residual_paired_eval_v8 as module

    seed = 77
    @dataclass(frozen=True)
    class FakeCase:
        dynamic_load: object
        case_spec: dict
        request: dict

    class FakeDynamicRolloutLoad:
        @staticmethod
        def bind(request, rebound_seed, config):
            del request
            return SimpleNamespace(
                seed=rebound_seed,
                request_sha256=REQUEST_SHA256,
                config=config,
            )

    case = FakeCase(
        dynamic_load=SimpleNamespace(
            seed=seed,
            request_sha256=REQUEST_SHA256,
            config=object(),
        ),
        case_spec={
            "build_id": "live_bonereaver",
            "required_target_indices": [0, 1, 2],
        },
        request={"test": True},
    )
    source_binding = ImportedReactiveProgramBindingV1(
        binding_id=CAT_POLICY_ID,
        source_policy_id=CAT_POLICY_ID,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=_FakeCat,
    )
    available = (
        AvailableAction(
            index=0,
            action=BLOODTHIRST,
            label="Bloodthirst",
            legal=True,
            ready_in_ms=0,
            triggers_gcd=True,
        ),
        AvailableAction(
            index=1,
            action=WHIRLWIND,
            label="Whirlwind",
            legal=True,
            ready_in_ms=0,
            triggers_gcd=True,
        ),
    )

    class FakeNativeReplay:
        def __init__(
            self,
            bridge_factory,
            case_factory,
            observation_projector,
            *,
            imported_bindings=(),
            **_,
        ):
            del observation_projector
            self.bridge_factory = bridge_factory
            self.case_factory = case_factory
            self.binding = imported_bindings[0]

        def replay(self, requested_seed, program, *, max_decisions=10_000):
            del max_decisions
            rebound_case = self.case_factory(requested_seed)
            assert rebound_case.dynamic_load.seed == requested_seed
            assert rebound_case.dynamic_load.request_sha256 == REQUEST_SHA256
            if lane_barrier is not None:
                lane_barrier.wait(timeout=2.0)
            with self.bridge_factory():
                pass
            session = self.binding.open_session()
            state = {
                "time_ms": 3_000,
                "target_index": 0,
                "precombat": {"active": False, "relative_time_ms": 0},
                "dynamic_target_semantics": {
                    "targets": [
                        {
                            "target_index": 0,
                            "current_health": 100.0,
                            "maximum_health": 100.0,
                            "attackable": True,
                            "dead": False,
                        },
                        {
                            "target_index": 1,
                            "current_health": 100.0,
                            "maximum_health": 100.0,
                            "attackable": True,
                            "dead": False,
                        },
                    ]
                },
            }
            if expose_seed:
                state["seed"] = requested_seed
            observation = CausalLiveStateProjectionV1(
                state=state,
                policy_to_simulator_target_index=(0, 1),
                visibility_cutoff_ms=3_000,
            )
            try:
                decision = session(observation, available)
                callback = getattr(
                    session, "record_last_executed_decision_v1", None
                )
                if callable(callback):
                    callback(decision)
            except Exception as error:
                return ScheduleReplayOutcomeV1(
                    seed=requested_seed + outcome_seed_offset,
                    status=ReplayStatusV1.INVALID,
                    state={"time_ms": 3_000, "damage_done": 0.0},
                    invalid_reason=f"{type(error).__name__}: {error}",
                )
            candidate = program.program_id == "paired-eval-test"
            target_rows = [
                {"target_index": index, "dead": True}
                for index in range(3)
            ]
            if candidate and not candidate_required_targets_dead:
                target_rows[2]["dead"] = False
            if residual_finished is not None:
                if candidate:
                    residual_finished.set()
                else:
                    assert residual_finished.wait(timeout=2.0)
            return ScheduleReplayOutcomeV1(
                seed=requested_seed + outcome_seed_offset,
                status=ReplayStatusV1.COMPLETE,
                state={
                    "time_ms": 8_000,
                    "dynamic_team_background": {
                        "simulated_damage_applied": 110.0 if candidate else 100.0,
                        "targets": target_rows,
                    },
                },
            )

    monkeypatch.setattr(
        module,
        "build_upper_kara_heterogeneous_two_wave_burst_case_v7",
        lambda *args, **kwargs: case,
    )
    monkeypatch.setattr(
        module,
        "build_imported_incumbent_bindings_v1",
        lambda *args, **kwargs: (source_binding,),
    )
    monkeypatch.setattr(
        module,
        "build_heterogeneous_two_wave_observation_projector_v1",
        lambda _: (lambda state, actions: (state, actions)),
    )
    monkeypatch.setattr(module, "NativeDynamicV3ActionProgramReplayV1", FakeNativeReplay)
    monkeypatch.setattr(module, "DynamicRolloutLoadV3", FakeDynamicRolloutLoad)


def test_paired_native_lanes_overlap_and_keep_named_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_barrier = threading.Barrier(2)
    residual_finished = threading.Event()
    _install_fake_native(
        monkeypatch,
        lane_barrier=lane_barrier,
        residual_finished=residual_finished,
    )

    result = evaluate_upper_kara_cat_residual_sequence_paired_v8(
        _policy(steps=(_one_whirlwind_step(),)),
        seed=77,
        build_id="live_bonereaver",
        loadout_id="contra_turtle_burst__mighty_rage",
        bridge_factory=_FakeBridge,
    )

    assert lane_barrier.broken is False
    assert residual_finished.is_set()
    # Residual is forced to finish before exact Cat.  Named futures must still
    # assign each outcome to its declared lane, not completion order.
    assert result["exact_cat_terminal"]["own_effective_damage"] == 100.0
    assert result["residual_terminal"]["own_effective_damage"] == 110.0
    assert result["paired_residual_minus_cat_own_effective_damage"] == 10.0
    assert result["fresh_native_bridge_instances_verified"] is True
    assert result["fresh_cat_sessions_verified"] is True


def test_paired_evaluator_reports_damage_delta_freshness_and_step_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_native(monkeypatch)
    result = evaluate_upper_kara_cat_residual_sequence_paired_v8(
        _policy(steps=(_one_whirlwind_step(),)),
        seed=77,
        build_id="live_bonereaver",
        loadout_id="contra_turtle_burst__mighty_rage",
        bridge_factory=_FakeBridge,
    )

    assert result["status"] == "COMPLETED_PAIRED_EVALUATION"
    assert result["paired_comparison_valid"] is True
    assert result["exact_cat_terminal"]["own_effective_damage"] == 100.0
    assert result["residual_terminal"]["own_effective_damage"] == 110.0
    assert result["paired_residual_minus_cat_own_effective_damage"] == 10.0
    assert result["fresh_native_bridge_instances_verified"] is True
    assert result["fresh_cat_sessions_verified"] is True
    assert result["policy_input_audit"]["clean"] is True
    assert result["step_audit"]["executed_step_keys"] == [
        ["wave-1", "replace-first-ready-gcd"]
    ]
    assert result["step_audit"]["steps"] == [
        {
            "wave_id": "wave-1",
            "step_id": "replace-first-ready-gcd",
            "executed": True,
        }
    ]


def test_explicit_seed_namespace_reproduces_master_request_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_native(monkeypatch)
    result = evaluate_upper_kara_cat_residual_sequence_paired_v8(
        _policy(),
        seed=77,
        build_id="live_bonereaver",
        loadout_id="contra_turtle_burst__mighty_rage",
        simulator_seed_namespace=PROTOCOL_ID,
        bridge_factory=_FakeBridge,
    )

    assert result["seed"] == 77
    assert result["master_seed"] == 77
    assert result["request_sha256"] == REQUEST_SHA256
    assert result["simulator_seed"] == derive_simulator_seed(
        77, REQUEST_SHA256, namespace=PROTOCOL_ID
    )
    assert result["simulator_seed_namespace"] == PROTOCOL_ID
    assert (
        result["simulator_seed_derivation_algorithm"]
        == SEED_DERIVATION_ALGORITHM
    )


def test_paired_evaluator_rejects_wrong_outcome_simulator_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_native(monkeypatch, outcome_seed_offset=1)
    with pytest.raises(RuntimeError, match="different simulator seed"):
        evaluate_upper_kara_cat_residual_sequence_paired_v8(
            _policy(),
            seed=77,
            build_id="live_bonereaver",
            loadout_id="contra_turtle_burst__mighty_rage",
            simulator_seed_namespace=PROTOCOL_ID,
            bridge_factory=_FakeBridge,
        )


def test_seed_or_future_control_field_invalidates_both_policy_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_native(monkeypatch, expose_seed=True)
    result = evaluate_upper_kara_cat_residual_sequence_paired_v8(
        _policy(),
        seed=77,
        build_id="live_bonereaver",
        loadout_id="contra_turtle_burst__mighty_rage",
        bridge_factory=_FakeBridge,
    )

    assert result["status"] == "INVALID_OR_INCOMPLETE_PAIRED_EVALUATION"
    assert result["paired_comparison_valid"] is False
    assert result["paired_residual_minus_cat_own_effective_damage"] is None
    assert result["policy_input_audit"]["clean"] is False
    assert result["policy_input_audit"]["forbidden_paths_seen"] == ["state.seed"]
    assert result["exact_cat_terminal"]["status"] == "INVALID_REPLAY"
    assert result["residual_terminal"]["status"] == "INVALID_REPLAY"


def test_required_target_terminal_gate_suppresses_partial_candidate_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_native(monkeypatch, candidate_required_targets_dead=False)
    result = evaluate_upper_kara_cat_residual_sequence_paired_v8(
        _policy(),
        seed=77,
        build_id="live_bonereaver",
        loadout_id="contra_turtle_burst__mighty_rage",
        bridge_factory=_FakeBridge,
    )

    assert result["exact_cat_terminal"]["status"] == "COMPLETED"
    assert result["residual_terminal"]["status"] == "INCOMPLETE_REQUIRED_TARGETS"
    assert result["residual_terminal"]["own_effective_damage"] is None
    assert result["paired_residual_minus_cat_own_effective_damage"] is None
    assert result["paired_comparison_valid"] is False


@pytest.mark.skipif(
    not DEFAULT_EXACT_BRIDGE.is_file(),
    reason="the exact native bridge is not built",
)
def test_zero_residual_native_smoke_is_exact_cat() -> None:
    result = evaluate_upper_kara_cat_residual_sequence_paired_v8(
        _policy(),
        seed=1_120_001,
        build_id="live_bonereaver",
        loadout_id="contra_turtle_burst__mighty_rage",
        first_wave_arrival_ms=0,
        max_decisions=512,
    )

    assert result["status"] == "COMPLETED_PAIRED_EVALUATION"
    assert result["paired_residual_minus_cat_own_effective_damage"] == 0.0
    assert result["fresh_native_bridge_instances_verified"] is True
    assert result["fresh_cat_sessions_verified"] is True
    assert result["policy_input_audit"]["clean"] is True
    assert result["step_audit"]["executed_step_keys"] == []
