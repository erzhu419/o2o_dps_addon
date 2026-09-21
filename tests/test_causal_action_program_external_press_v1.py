from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1


@dataclass(frozen=True)
class _Case:
    request: dict
    dynamic_load: object
    precombat: object | None = None


class _WaitingResolver:
    def __init__(self) -> None:
        self.observed_times: list[int] = []

    def __call__(self, observation, available):
        assert available == ()
        self.observed_times.append(observation.visibility_cutoff_ms)
        return ProgramDecisionV1(wait_ms=75)


class _PressBridge:
    """Two physical presses with one invisible internal wake between them."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.time_ms = 0
        self.press_index = 1
        self.ready = True
        self.finished = False
        self.advance_index = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def _state(self) -> dict:
        next_time_ms = self.time_ms if self.ready else 100
        if self.press_index >= 2 and not self.ready:
            next_time_ms = 200
        return {
            "time_ms": self.time_ms,
            "needs_input": self.ready and not self.finished,
            "finished": self.finished,
            "damage_done": 0.0,
            "press_clock": {
                "period_ms": 100,
                "phase_ms": 0,
                "press_index": self.press_index,
                "ready": self.ready,
                "next_time_ms": next_time_ms,
            },
        }

    def load_dynamic_v3_precombat_press_clock(
        self, request, seed, config, precombat, period_ms, phase_ms
    ):
        del request, seed, config, precombat
        assert (period_ms, phase_ms) == (100, 0)
        self.commands.append("load_dynamic_v3_precombat_press_clock")
        return SimpleNamespace(state=self._state())

    def actions(self):
        return ()

    def wait(self, wait_ms):
        raise AssertionError(f"WAIT {wait_ms} must abstain on the current key")

    def finish_press(self):
        assert self.ready and not self.finished
        self.commands.append(f"finish_press:{self.press_index}")
        self.ready = False
        return self._state()

    def advance(self):
        self.advance_index += 1
        self.commands.append(f"advance:{self.advance_index}")
        if self.advance_index == 1:
            # A model/environment wake is observable to the control plane but
            # must not become a policy opportunity.
            self.time_ms = 50
            self.ready = False
        elif self.advance_index == 2:
            self.time_ms = 100
            self.press_index = 2
            self.ready = True
        elif self.advance_index == 3:
            self.time_ms = 150
            self.ready = False
            self.finished = True
        else:
            raise AssertionError("unexpected extra model advance")
        return self._state()


def _project(state, available):
    del available
    return CausalLiveStateProjectionV1(
        state=deepcopy(dict(state)),
        policy_to_simulator_target_index=(),
        visibility_cutoff_ms=state["time_ms"],
    )


def test_external_press_replay_abstains_and_ignores_internal_model_wake() -> None:
    bridge = _PressBridge()
    sessions: list[_WaitingResolver] = []

    def open_session():
        resolver = _WaitingResolver()
        sessions.append(resolver)
        return resolver

    binding = ImportedReactiveProgramBindingV1(
        "waiting-source", "waiting-source", "causal-live-state/v1", open_session
    )
    program = CausalActionProgramV1(
        "waiting-source",
        ImportedReactiveSelectorV1(
            binding.binding_id,
            binding.source_policy_id,
            binding.observation_contract_id,
        ),
        ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
    )
    case = _Case(
        request={"request": "fake"},
        dynamic_load=SimpleNamespace(config={"dynamic": "fake"}),
        precombat=object(),
    )
    replay = NativeDynamicV3ActionProgramReplayV1(
        lambda: bridge,
        lambda seed: case,
        _project,
        imported_bindings=(binding,),
        external_press_period_ms=100,
    )

    result = replay.replay(9, program)

    assert result.status is ReplayStatusV1.COMPLETE
    assert sessions[0].observed_times == [0, 100]
    assert bridge.commands == [
        "load_dynamic_v3_precombat_press_clock",
        "finish_press:1",
        "advance:1",
        "advance:2",
        "finish_press:2",
        "advance:3",
    ]
    kinds = [row["kind"] for row in result.receipts]
    assert kinds.count("TERMINAL_WAIT_EXTERNAL_PRESS_ABSTAIN") == 2
    assert kinds.count("EXTERNAL_PRESS_FINISHED") == 2


def test_external_press_configuration_is_opt_in_and_validated() -> None:
    case = _Case(
        request={"request": "fake"},
        dynamic_load=SimpleNamespace(config={"dynamic": "fake"}),
    )
    binding = ImportedReactiveProgramBindingV1(
        "waiting-source",
        "waiting-source",
        "causal-live-state/v1",
        _WaitingResolver,
    )
    kwargs = dict(
        bridge_factory=_PressBridge,
        case_factory=lambda seed: case,
        observation_projector=_project,
        imported_bindings=(binding,),
    )

    try:
        NativeDynamicV3ActionProgramReplayV1(
            **kwargs, external_press_phase_ms=1
        )
    except ValueError as error:
        assert "requires external_press_period_ms" in str(error)
    else:
        raise AssertionError("phase without period was accepted")

    try:
        NativeDynamicV3ActionProgramReplayV1(
            **kwargs,
            external_press_period_ms=100,
            external_press_phase_ms=100,
        )
    except ValueError as error:
        assert "external_press_phase_ms" in str(error)
    else:
        raise AssertionError("out-of-range phase was accepted")
