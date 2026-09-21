from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from o2o_dps.responsive_action_program_replay_v1 import (
    NativeDynamicV4ResponsiveActionProgramReplayV1,
)
from o2o_dps.sim_bridge import ActResult, ActionRef, AvailableAction
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1


STRIKE = ActionRef(spell_id=23894)


class _DrivenBridge:
    def __init__(self) -> None:
        self.load_calls = []
        self.advance_calls = 0
        self._time = 0
        self._finished = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def _state(self):
        state = {
            "time_ms": self._time,
            "needs_input": not self._finished,
            "finished": self._finished,
            "damage_done": 12.0 if self._finished else 0.0,
            "remaining_ms": 1_000,
        }
        if self._time == 0:
            state["wake_ready"] = {"wake_id": "teammate-first"}
        return state

    def load_dynamic_v4(self, request, seed, config):
        self.load_calls.append((deepcopy(request), seed, config))
        return SimpleNamespace(state=self._state())

    def advance(self):
        self.advance_calls += 1
        self._time = 100
        return self._state()

    def actions(self):
        return [AvailableAction(0, STRIKE, "Bloodthirst", True, 0, True)]

    def act(self, action, *, attempt_id=None):
        assert action == STRIKE
        self._finished = True
        return ActResult(True, True, True, False, self._state())

    def target_event_diagnostic(self):
        return {"schema": "development_responsive_target_event_diagnostic/v1",
                "event_count": 2}


def _case(_seed):
    return CompiledResponsiveIncantagosCaseV1(
        request={"loadout": "one-identical-build"},
        dynamic_config=SimpleNamespace(content_sha256="same-v4-config"),
        runtime=None,
        native_target_guids=(),
        target_introduced_at_ms_by_guid={},
        actors=(),
        candidate_player_guid="candidate",
        teammate_player_guids=(),
        team_only_sidecar=(),
        receipt={},
    )


def _projector(_case):
    def project(state, available):
        assert available[0].action == STRIKE
        safe = {key: deepcopy(value) for key, value in state.items()
                if key != "remaining_ms"}
        return CausalLiveStateProjectionV1(safe, (0,), state["time_ms"])

    return project


def _program(policy_id):
    return CausalActionProgramV1(
        program_id=policy_id,
        selector=ImportedReactiveSelectorV1(policy_id, policy_id, "current-state-v1"),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
    )


def test_two_imported_sessions_use_same_loadout_seed_and_post_wake_state():
    bridges = []
    observations = []
    session_opened = []

    def bridge_factory():
        bridge = _DrivenBridge()
        bridges.append(bridge)
        return bridge

    def binding(policy_id):
        def open_session():
            session_opened.append(policy_id)

            def resolve(observation, available):
                observations.append((policy_id, observation.visibility_cutoff_ms,
                                     "remaining_ms" in observation.state,
                                     available[0].action))
                return ProgramDecisionV1(gcd_action=STRIKE)

            return resolve

        return ImportedReactiveProgramBindingV1(
            policy_id, policy_id, "current-state-v1", open_session
        )

    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=bridge_factory,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(binding("cat"), binding("contra")),
    )
    for policy_id in ("cat", "contra"):
        result = replay.replay(77, _program(policy_id))
        assert result.status is ReplayStatusV1.COMPLETE
        assert result.effective_damage == 12.0
        assert result.receipts[0]["comparison_authorized"] is False
        assert result.receipts[-1]["target_event_diagnostic"]["event_count"] == 2
    assert [bridge.load_calls[0] for bridge in bridges] == [
        ({"loadout": "one-identical-build"}, 77, bridges[0].load_calls[0][2]),
        ({"loadout": "one-identical-build"}, 77, bridges[0].load_calls[0][2]),
    ]
    assert [bridge.advance_calls for bridge in bridges] == [1, 1]
    assert observations == [
        ("cat", 100, False, STRIKE),
        ("contra", 100, False, STRIKE),
    ]
    assert session_opened == ["cat", "contra"]


def test_raw_future_field_reaches_no_imported_resolver():
    calls = []
    binding = ImportedReactiveProgramBindingV1(
        "cat", "cat", "current-state-v1",
        lambda: lambda observation, available: calls.append(observation),
    )
    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=_DrivenBridge,
        case_factory=_case,
        observation_projector_factory=lambda case: (
            lambda state, actions: CausalLiveStateProjectionV1(
                dict(state), (0,), state["time_ms"]
            )
        ),
        imported_bindings=(binding,),
    )
    result = replay.replay(77, _program("cat"))
    # The projector omitted no data; a real bound projector must fail before
    # the imported policy can consume a future/control field.
    assert result.status is ReplayStatusV1.INVALID
    assert "remaining_ms" in result.invalid_reason
    assert calls == []


def test_bounded_frontier_stops_after_wake_before_policy_input():
    calls = []
    binding = ImportedReactiveProgramBindingV1(
        "cat", "cat", "current-state-v1",
        lambda: lambda observation, available: calls.append(observation),
    )
    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=_DrivenBridge,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(binding,),
    )
    result = replay.replay(77, _program("cat"), stop_at_or_after_ms=100)
    assert result.status is ReplayStatusV1.FRONTIER
    assert result.elapsed_ms == 100
    assert result.receipts[-1]["kind"] == (
        "DEVELOPMENT_RESPONSIVE_V4_BOUNDED_FRONTIER"
    )
    assert calls == []
