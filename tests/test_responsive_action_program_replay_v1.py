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
from o2o_dps.sim_bridge import (
    ActResult,
    ActionRef,
    AvailableAction,
    SetTargetResult,
)
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1
from o2o_dps.upper_kara_wave_target_gate_v1 import (
    REQUIRED_RETARGET_MODE_V1,
    WaveTargetGateDecisionV1,
)


STRIKE = ActionRef(spell_id=23894)
WHIRLWIND = ActionRef(spell_id=1680)


class _DrivenBridge:
    def __init__(self, action=STRIKE, *, target_rows=()) -> None:
        self.load_calls = []
        self.advance_calls = 0
        self.set_target_calls = []
        self.act_calls = []
        self.action = action
        self.target_rows = tuple(deepcopy(target_rows))
        self._time = 0
        self._finished = False
        self._target_index = 0

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
        if self.target_rows:
            state["target_index"] = self._target_index
            state["dynamic_target_semantics"] = {
                "targets": deepcopy(list(self.target_rows))
            }
        return state

    def load_dynamic_v4(self, request, seed, config):
        self.load_calls.append((deepcopy(request), seed, config))
        return SimpleNamespace(state=self._state())

    def advance(self):
        self.advance_calls += 1
        self._time = 100
        return self._state()

    def actions(self):
        return [AvailableAction(0, self.action, "test action", True, 0, True)]

    def set_target(self, target_index):
        self.set_target_calls.append(target_index)
        self._target_index = target_index
        return SetTargetResult(
            changed=True,
            target_index=target_index,
            finished=False,
            needs_input=True,
            state=self._state(),
        )

    def act(self, action, *, attempt_id=None):
        assert action == self.action
        self.act_calls.append(action)
        self._finished = True
        return ActResult(True, True, True, False, self._state())

    def target_event_diagnostic(self):
        return {"schema": "development_responsive_target_event_diagnostic/v1",
                "event_count": 2}


class _TelemetryBridge(_DrivenBridge):
    def dynamic_candidate_damage_receipts(self, *, cursor=0):
        assert cursor == 0

        def row(action, ordinal, damage):
            return SimpleNamespace(
                damage_ordinal=ordinal,
                time_ms=100,
                target_index=0,
                requested_damage=damage,
                applied_damage=damage,
                overkill_damage=0.0,
                killed=False,
                status="APPLIED",
                action=action,
                outcome="HIT",
                execution_id=ordinal,
                execution_index=ordinal,
                landed_execution_index=ordinal,
                resolution_phase="APPLIED_AFTER_OUTCOME",
                outcome_computed=True,
                random_stream_rewound=False,
                attempt_id=f"attempt-{ordinal}",
                retargeted_to=None,
            )

        return SimpleNamespace(
            receipts=(
                row(STRIKE, 1, 12.0),
                row(WHIRLWIND, 2, 7.0),
            )
        )


def _case(_seed):
    return CompiledResponsiveIncantagosCaseV1(
        request={"loadout": "one-identical-build"},
        dynamic_config=SimpleNamespace(
            content_sha256="same-v4-config",
            retarget_mode=REQUIRED_RETARGET_MODE_V1,
        ),
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


class _FakeTargetGate:
    def __init__(self, *, direct=(0,), collateral=(0,)):
        self.direct = tuple(direct)
        self.collateral = tuple(collateral)
        self.validated_cases = []
        self.previous_stage_ids = []

    def validate_case(self, case):
        assert case.dynamic_config.retarget_mode == REQUIRED_RETARGET_MODE_V1
        self.validated_cases.append(case)

    def evaluate_state(self, state, *, previous_stage_id=None):
        self.previous_stage_ids.append(previous_stage_id)
        return WaveTargetGateDecisionV1(
            stage_id="fake-stage",
            direct_target_indexes=self.direct,
            collateral_target_indexes=self.collateral,
        )


def _decision_binding(policy_id, decision):
    return ImportedReactiveProgramBindingV1(
        policy_id,
        policy_id,
        "current-state-v1",
        lambda: lambda observation, available: decision,
    )


class _ReceiptResolver:
    def __init__(self, decision):
        self.decision = decision
        self.execution_receipts = []

    def __call__(self, observation, available):
        return self.decision

    def record_last_execution_receipt_v1(self, decision, execution_receipts):
        self.execution_receipts.append((decision, execution_receipts))


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


def test_responsive_replay_reports_only_current_decision_execution_receipts():
    resolver = _ReceiptResolver(ProgramDecisionV1(gcd_action=STRIKE))
    binding = ImportedReactiveProgramBindingV1(
        "offline",
        "offline",
        "current-state-v1",
        lambda: resolver,
    )
    result = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=_DrivenBridge,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(binding,),
    ).replay(78, _program("offline"))

    assert result.status is ReplayStatusV1.COMPLETE
    assert len(resolver.execution_receipts) == 1
    decision, execution_receipts = resolver.execution_receipts[0]
    assert decision.gcd_action == STRIKE
    assert [row["kind"] for row in execution_receipts] == [
        "QUEUE_KEEP",
        "TERMINAL_GCD",
    ]
    assert "IMPORTED_REACTIVE_INCUMBENT_SELECTED" not in {
        row["kind"] for row in execution_receipts
    }


def test_responsive_replay_retains_requested_terminal_telemetry():
    result = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=_TelemetryBridge,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(
            _decision_binding("offline", ProgramDecisionV1(gcd_action=STRIKE)),
        ),
        terminal_telemetry_action_refs=(STRIKE,),
    ).replay(79, _program("offline"))

    assert result.status is ReplayStatusV1.COMPLETE
    assert tuple(row.action for row in result.available_actions) == (STRIKE,)
    telemetry = result.receipts[-1]
    assert telemetry["kind"] == "NATIVE_TERMINAL_TELEMETRY_V1"
    assert telemetry["state_time_ms"] == 100
    assert telemetry["terminal_action_surface"]["status"] == "OBSERVED"
    damage = telemetry["candidate_damage_surface"]
    assert damage["status"] == "OBSERVED"
    assert damage["source_receipt_count"] == 2
    assert damage["retained_receipt_count"] == 1
    assert damage["retained_actions"] == [STRIKE.to_wire()]
    assert [row["action"] for row in damage["receipts"]] == [STRIKE.to_wire()]


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


def test_runtime_target_gate_receipt_and_outcome_cover_allowed_set_target():
    bridges = []
    gate = _FakeTargetGate(direct=(0,), collateral=(0,))

    def bridge_factory():
        bridge = _DrivenBridge()
        bridges.append(bridge)
        return bridge

    decision = ProgramDecisionV1(target_index=0, gcd_action=STRIKE)
    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=bridge_factory,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(_decision_binding("cat", decision),),
        target_gate=gate,
    )
    result = replay.replay(77, _program("cat"))

    assert result.status is ReplayStatusV1.COMPLETE
    assert result.target_gate == WaveTargetGateDecisionV1(
        stage_id="fake-stage",
        direct_target_indexes=(0,),
        collateral_target_indexes=(0,),
    )
    target_receipt = next(
        row for row in result.receipts if row.get("kind") == "SET_TARGET"
    )
    assert target_receipt["simulator_target_index"] == 0
    assert target_receipt["target_gate"] == result.target_gate.to_dict()
    assert bridges[0].set_target_calls == [0]
    assert bridges[0].act_calls == [STRIKE]
    assert len(gate.validated_cases) == 1


def test_runtime_target_gate_rejects_direct_target_before_bridge_mutation():
    bridges = []
    gate = _FakeTargetGate(direct=(1,), collateral=(0, 1))

    def bridge_factory():
        bridge = _DrivenBridge()
        bridges.append(bridge)
        return bridge

    decision = ProgramDecisionV1(target_index=0, gcd_action=STRIKE)
    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=bridge_factory,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(_decision_binding("cat", decision),),
        target_gate=gate,
    )
    result = replay.replay(77, _program("cat"))

    assert result.status is ReplayStatusV1.INVALID
    assert "outside the current direct-target allowlist" in result.invalid_reason
    assert bridges[0].set_target_calls == []
    assert bridges[0].act_calls == []


def test_runtime_target_gate_fails_closed_before_unmasked_whirlwind():
    bridges = []
    gate = _FakeTargetGate(direct=(0,), collateral=(0,))
    targets = (
        {"target_index": 0, "dead": False, "attackable": True},
        {"target_index": 1, "dead": False, "attackable": True},
    )

    def bridge_factory():
        bridge = _DrivenBridge(WHIRLWIND, target_rows=targets)
        bridges.append(bridge)
        return bridge

    decision = ProgramDecisionV1(gcd_action=WHIRLWIND)
    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=bridge_factory,
        case_factory=_case,
        observation_projector_factory=lambda case: (
            lambda state, available: CausalLiveStateProjectionV1(
                {
                    key: deepcopy(value)
                    for key, value in state.items()
                    if key != "remaining_ms"
                },
                (0, 1),
                state["time_ms"],
            )
        ),
        imported_bindings=(_decision_binding("cat", decision),),
        target_gate=gate,
    )
    result = replay.replay(77, _program("cat"))

    assert result.status is ReplayStatusV1.INVALID
    assert "unmasked collateral action could hit target indexes" in (
        result.invalid_reason
    )
    assert bridges[0].act_calls == []
