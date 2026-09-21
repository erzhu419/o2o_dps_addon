from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
import unittest

from o2o_dps.causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.development_d900_cat_relative_v1 import (
    D900CatRelativeCandidateV1,
    D900CatRelativeError,
    D900CatRelativeSessionV1,
    bind_d900_cat_relative_v1,
    enumerate_d900_cat_relative_action_plans_v1,
    replay_d900_cat_relative_v1,
)
from o2o_dps.development_d900_cat_relative_search_plan_v1 import (
    build_d900_cat_relative_search_plan_v1,
    freeze_d900_search_case_v1,
    paired_seed_cohorts_v1,
    record_d900_cat_prefix_v1,
    verify_d900_search_case_v1,
)
from o2o_dps.policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from o2o_dps.responsive_action_program_replay_v1 import NativeDynamicV4ResponsiveActionProgramReplayV1
from o2o_dps.sim_bridge import ActResult, AvailableAction
from o2o_dps.upper_kara_cat_action_plan_teacher_v8 import (
    BLOODTHIRST,
    WHIRLWIND,
    CatDecisionPointV8,
    enumerate_cat_relative_action_plans_v8,
)
from o2o_dps.upper_kara_development_route_focus_v1 import (
    DoomguardCurrentStateRouteFocusedBridgeV1,
    ORDERED_GUIDS,
)
from o2o_dps.upper_kara_imported_incumbent_program_v1 import OBSERVATION_CONTRACT_ID_V1
from o2o_dps.upper_kara_responsive_incantagos_case_v1 import CompiledResponsiveIncantagosCaseV1
from o2o_dps.wave_action_sequence_search_v1 import ReplayStatusV1
from scripts.development_d900_cat_relative_plan_summary_v1 import summarize


def _observation(*, time_ms: int = 0, selected: int = 0,
                 attackable: tuple[bool, bool, bool] = (True, True, True)) -> CausalLiveStateProjectionV1:
    return CausalLiveStateProjectionV1(
        state={
            "time_ms": time_ms,
            "target_index": selected,
            "dynamic_target_semantics": {
                "targets": [
                    {"target_index": index, "attackable": attackable[index],
                     "dead": False, "current_health": 1_000.0,
                     "maximum_health": 1_000.0}
                    for index in range(3)
                ]
            },
        },
        policy_to_simulator_target_index=(0, 1, 2),
        visibility_cutoff_ms=time_ms,
    )


def _actions() -> tuple[AvailableAction, ...]:
    return (
        AvailableAction(0, BLOODTHIRST, "Bloodthirst", True, 0, True, True),
        AvailableAction(1, WHIRLWIND, "Whirlwind", True, 0, True, True),
    )


class _CatSession:
    def __init__(self) -> None:
        self.calls = 0
        self.last_gcd_action = ""
        self.seen_last_gcd = []

    def __call__(self, observation, available):
        del observation, available
        self.calls += 1
        self.seen_last_gcd.append(self.last_gcd_action)
        self.last_gcd_action = "warrior.bloodthirst"
        return ProgramDecisionV1(gcd_action=BLOODTHIRST)


def _candidate(replacement: ProgramDecisionV1) -> D900CatRelativeCandidateV1:
    return D900CatRelativeCandidateV1(
        "one-ready-branch",
        ObservableCausalGuardV1(live_target_count_gte=1, false_semantics=SKIP_PLAN),
        replacement,
        cat_gcd_action_is=BLOODTHIRST,
    )


def test_one_guarded_action_replacement_then_same_cat_session_resumes() -> None:
    observation = _observation()
    point = CatDecisionPointV8(0, observation, _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST))
    replacement = next(
        decision for decision in enumerate_d900_cat_relative_action_plans_v1(point)
        if decision.gcd_action == WHIRLWIND and decision.target_index is None
    )
    cat = _CatSession()
    session = D900CatRelativeSessionV1(cat, _candidate(replacement))
    assert session(observation, _actions()) == replacement
    assert session(_observation(time_ms=1_500), _actions()).gcd_action == BLOODTHIRST
    assert cat.calls == 2
    assert cat.seen_last_gcd == ["", "warrior.whirlwind"]
    assert len(session.interventions) == 1


def test_off_route_target_is_rejected_even_when_v8_lists_it() -> None:
    observation = _observation()
    point = CatDecisionPointV8(0, observation, _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST))
    off_route = next(
        decision for decision in enumerate_cat_relative_action_plans_v8(point)
        if decision.target_index == 1
    )
    assert off_route not in enumerate_d900_cat_relative_action_plans_v1(point)
    with unittest.TestCase().assertRaisesRegex(D900CatRelativeError, "outside current route focus"):
        D900CatRelativeSessionV1(_CatSession(), _candidate(off_route))(
            observation, _actions()
        )
    # Target 0 being unattackable makes target 1 the current route focus.
    advanced = _observation(selected=1, attackable=(False, True, True))
    advanced_point = CatDecisionPointV8(0, advanced, _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST))
    assert all(
        decision.target_index in (None, 1)
        for decision in enumerate_d900_cat_relative_action_plans_v1(advanced_point)
    )


class _OneDecisionBridge:
    def __init__(self) -> None:
        self.finished = False
        self.actions_used = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def _state(self):
        return {
            "time_ms": 0,
            "needs_input": not self.finished,
            "finished": self.finished,
            "target_index": 0,
            "damage_done": 100.0 if self.finished else 0.0,
            "dynamic_target_semantics": _observation().state["dynamic_target_semantics"],
        }

    def load_dynamic_v4(self, request, seed, config):
        assert request == {"build": "segment-0042"}
        assert seed == 77
        assert config.content_sha256 == "same-config"
        return _Loaded(self._state())

    def actions(self):
        return _actions()

    def act(self, action, *, attempt_id=None):
        self.actions_used.append((action, attempt_id))
        self.finished = True
        return ActResult(True, True, True, False, self._state())


@dataclass(frozen=True)
class _Loaded:
    state: dict


def _case(_seed):
    return CompiledResponsiveIncantagosCaseV1(
        request={"build": "segment-0042"},
        dynamic_config=SimpleNamespace(content_sha256="same-config"),
        runtime=None,
        native_target_guids=ORDERED_GUIDS,
        target_introduced_at_ms_by_guid={},
        actors=(),
        candidate_player_guid="focal",
        teammate_player_guids=(),
        team_only_sidecar=(),
        receipt={},
    )


def _projector(_case):
    return lambda state, available: CausalLiveStateProjectionV1(
        state=dict(state),
        policy_to_simulator_target_index=(0, 1, 2),
        visibility_cutoff_ms=state["time_ms"],
    )


def test_exact_cat_entry_matches_existing_d900_imported_replay_same_seed() -> None:
    opened = []

    def open_cat():
        session = _CatSession()
        opened.append(session)
        return session

    cat_binding = ImportedReactiveProgramBindingV1(
        "cat.fury.profile1", "cat.fury.profile1", OBSERVATION_CONTRACT_ID_V1,
        open_cat,
    )
    exact_binding, exact_program = bind_d900_cat_relative_v1(cat_binding, None)
    assert exact_binding is cat_binding
    assert exact_program.program_id == "trash-v4-cat.fury.profile1-development-probe"
    existing_program = CausalActionProgramV1(
        program_id="trash-v4-cat.fury.profile1-development-probe",
        selector=ImportedReactiveSelectorV1(
            "cat.fury.profile1", "cat.fury.profile1", OBSERVATION_CONTRACT_ID_V1
        ),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
    )
    assert exact_program.program_key() == existing_program.program_key()

    old_bridges, new_bridges = [], []

    def old_factory():
        bridge = _OneDecisionBridge()
        old_bridges.append(bridge)
        return DoomguardCurrentStateRouteFocusedBridgeV1(bridge, ORDERED_GUIDS)

    def new_factory():
        bridge = _OneDecisionBridge()
        new_bridges.append(bridge)
        return bridge

    old = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=old_factory,
        case_factory=_case,
        observation_projector_factory=_projector,
        imported_bindings=(cat_binding,),
    ).replay(77, existing_program)
    new = replay_d900_cat_relative_v1(
        seed=77,
        cat_binding=cat_binding,
        candidate=None,
        driven_bridge_factory=new_factory,
        case_factory=_case,
        observation_projector_factory=_projector,
    )
    assert old.status is new.status is ReplayStatusV1.COMPLETE, (old.invalid_reason, new.invalid_reason)
    assert old.state == new.state
    assert old.effective_damage == new.effective_damage == 100.0
    assert old.receipts == new.receipts
    assert old_bridges[0].actions_used == new_bridges[0].actions_used
    assert len(opened) == 2 and all(session.calls == 1 for session in opened)


def test_d900_replay_entry_applies_ready_branch_and_rejects_off_route() -> None:
    cat_binding = ImportedReactiveProgramBindingV1(
        "cat.fury.profile1", "cat.fury.profile1", OBSERVATION_CONTRACT_ID_V1,
        _CatSession,
    )
    observation = _observation()
    point = CatDecisionPointV8(0, observation, _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST))
    ready = next(
        row for row in enumerate_d900_cat_relative_action_plans_v1(point)
        if row.gcd_action == WHIRLWIND and row.target_index is None
    )
    off_route = next(
        row for row in enumerate_cat_relative_action_plans_v8(point)
        if row.target_index == 1
    )
    bridges = []

    def bridge_factory():
        bridge = _OneDecisionBridge()
        bridges.append(bridge)
        return bridge

    common = dict(
        seed=77, cat_binding=cat_binding, driven_bridge_factory=bridge_factory,
        case_factory=_case, observation_projector_factory=_projector,
    )
    opened_sessions = []
    accepted = replay_d900_cat_relative_v1(
        candidate=_candidate(ready), opened_sessions=opened_sessions, **common
    )
    rejected = replay_d900_cat_relative_v1(candidate=_candidate(off_route), **common)
    assert accepted.status is ReplayStatusV1.COMPLETE
    assert bridges[0].actions_used[0][0] == WHIRLWIND
    assert len(opened_sessions) == 1 and len(opened_sessions[0].interventions) == 1
    assert rejected.status is ReplayStatusV1.INVALID
    assert "outside current route focus" in rejected.invalid_reason
    assert bridges[1].actions_used == []


def test_search_plan_freezes_case_splits_seeds_and_round_robins_ready_prefixes() -> None:
    cat_binding = ImportedReactiveProgramBindingV1(
        "cat.fury.profile1", "cat.fury.profile1", OBSERVATION_CONTRACT_ID_V1,
        _CatSession,
    )
    case = replace(_case(77), receipt={
        "fixed_attackability_mode": "OBSERVED_ONSET_UNTIL_SIM_DEATH",
        "source": {"wave_id": "d900"},
    })
    freeze = freeze_d900_search_case_v1(
        case, cat_binding, model_result_sha="result-v6", model_sha="model-v6",
        exact_build_source={"player_guid": "focal"},
    )
    verify_d900_search_case_v1(case, cat_binding, freeze)
    with unittest.TestCase().assertRaisesRegex(D900CatRelativeError, "frozen case"):
        verify_d900_search_case_v1(replace(case, request={"changed": True}), cat_binding, freeze)
    points = [
        CatDecisionPointV8(index, _observation(time_ms=index * 1_500), _actions(),
                           ProgramDecisionV1(gcd_action=BLOODTHIRST))
        for index in range(3)
    ]
    plan = build_d900_cat_relative_search_plan_v1(
        proposal_points=points, freeze=freeze,
        simulator_seed_start=1000, teammate_seed_start=100_000,
    )
    seeds = plan.seed_cohorts
    assert [len(seeds[name]) for name in ("PROPOSAL", "SELECTION", "HELDOUT")] == [128, 128, 256]
    assert len({pair for rows in seeds.values() for pair in rows}) == 512
    assert len({seed for rows in seeds.values() for seed, _ in rows}) == 512
    assert len(plan.candidates) <= 256
    assert all(candidate.replacement != points[candidate.decision_index_is].cat_decision
               for candidate in plan.candidates)
    assert [candidate.decision_index_is for candidate in plan.candidates[:3]] == [0, 1, 2]
    assert plan.to_dict()["selection_contract"]["minimum_complete_selection_pairs"] == 128
    assert paired_seed_cohorts_v1(1000, 1001)["PROPOSAL"][0] == (1000, 1001)
    many_points = [
        CatDecisionPointV8(index, _observation(time_ms=index * 100), _actions(),
                           ProgramDecisionV1(gcd_action=BLOODTHIRST))
        for index in range(130)
    ]
    capped = build_d900_cat_relative_search_plan_v1(
        proposal_points=many_points, freeze=freeze,
        simulator_seed_start=1000, teammate_seed_start=100_000,
    )
    assert len(capped.candidates) == 256
    assert [row.decision_index_is for row in capped.candidates[:130]] == list(range(130))


def test_plan_candidate_only_fires_at_matching_cat_prefix_and_skips_unready() -> None:
    point = CatDecisionPointV8(
        1, _observation(time_ms=1_500), _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST)
    )
    plan = build_d900_cat_relative_search_plan_v1(
        proposal_points=[CatDecisionPointV8(
            0, _observation(), _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST)
        ), point], freeze={"case": "fixed"},
        simulator_seed_start=1000, teammate_seed_start=100_000,
    )
    candidate = next(row for row in plan.candidates
                     if row.decision_index_is == 1 and row.replacement.gcd_action == WHIRLWIND)
    cat = _CatSession()
    session = D900CatRelativeSessionV1(cat, candidate)
    assert session(_observation(), _actions()).gcd_action == BLOODTHIRST
    assert session(_observation(time_ms=1_500), _actions()) == candidate.replacement
    assert len(session.interventions) == 1
    unready = D900CatRelativeSessionV1(_CatSession(), candidate)
    assert unready(_observation(), _actions()).gcd_action == BLOODTHIRST
    assert unready(_observation(time_ms=1_500), _actions()[:1]).gcd_action == BLOODTHIRST
    assert not unready.interventions and unready.ineligible_count == 1
    assert unready.matched_baseline_count == 1


def test_prefix_recorder_and_paired_row_require_same_frozen_cohort() -> None:
    cat_binding = ImportedReactiveProgramBindingV1(
        "cat.fury.profile1", "cat.fury.profile1", OBSERVATION_CONTRACT_ID_V1,
        _CatSession,
    )
    points = []
    recording = record_d900_cat_prefix_v1(cat_binding, points)
    assert recording.open_session()(_observation(), _actions()).gcd_action == BLOODTHIRST
    assert len(points) == 1 and points[0].decision_index == 0
    plan = build_d900_cat_relative_search_plan_v1(
        proposal_points=points, freeze={"case": "fixed"},
        simulator_seed_start=1000, teammate_seed_start=100_000,
    )
    candidate = plan.candidates[0]
    session = D900CatRelativeSessionV1(_CatSession(), candidate)
    session(_observation(), _actions())
    outcome = SimpleNamespace(status=ReplayStatusV1.COMPLETE,
                              effective_damage=105.0, invalid_reason=None)
    cat_outcome = SimpleNamespace(status=ReplayStatusV1.COMPLETE,
                                  effective_damage=100.0, invalid_reason=None)
    row = plan.paired_row(
        cohort="SELECTION", candidate_id=candidate.candidate_id,
        seed=1128, teammate_seed=100_128,
        candidate_outcome=outcome, cat_outcome=cat_outcome,
        opened_sessions=[session],
    )
    assert row["paired_delta"] == 5.0 and row["intervention_count"] <= 1
    with unittest.TestCase().assertRaisesRegex(ValueError, "outside its frozen cohort"):
        plan.paired_row(
            cohort="HELDOUT", candidate_id=candidate.candidate_id,
            seed=1128, teammate_seed=100_128,
            candidate_outcome=outcome, cat_outcome=cat_outcome,
            opened_sessions=[session],
        )


def test_serialized_plan_summary_checks_route_legality() -> None:
    point = CatDecisionPointV8(
        0, _observation(), _actions(), ProgramDecisionV1(gcd_action=BLOODTHIRST)
    )
    plan = build_d900_cat_relative_search_plan_v1(
        proposal_points=[point], freeze={
            "case_source": {"wave_id": "d900"},
            "native_target_guids": list(ORDERED_GUIDS),
            "attackability_mode": "OBSERVED_ONSET_UNTIL_SIM_DEATH",
            "dynamic_config_content_sha256": "same-config",
            "cat_source_policy_id": "cat.fury.profile1",
            "model_result_sha": "result-v6", "model_sha": "model-v6",
        }, simulator_seed_start=1000, teammate_seed_start=1001,
    ).to_dict()
    plan["proposal_exact_cat"] = {"status": "COMPLETE", "decision_count": 1}
    summary = summarize(plan, plan_path=Path("/remote/plan.json"), plan_bytes=100)
    assert summary["status"] == "PROPOSAL_PLAN_VALID"
    assert summary["source_focus_target_counts"] == {"0": len(plan["candidates"])}
    plan["candidates"][0]["replacement"]["target_index"] = 2
    assert summarize(plan, plan_path=Path("/remote/plan.json"), plan_bytes=100)["status"] == "PROPOSAL_PLAN_INVALID"


def load_tests(loader, tests, pattern):
    del loader, tests, pattern
    return unittest.TestSuite(
        unittest.FunctionTestCase(test)
        for test in (
            test_one_guarded_action_replacement_then_same_cat_session_resumes,
            test_off_route_target_is_rejected_even_when_v8_lists_it,
            test_exact_cat_entry_matches_existing_d900_imported_replay_same_seed,
            test_d900_replay_entry_applies_ready_branch_and_rejects_off_route,
            test_search_plan_freezes_case_splits_seeds_and_round_robins_ready_prefixes,
            test_plan_candidate_only_fires_at_matching_cat_prefix_and_skips_unready,
            test_prefix_recorder_and_paired_row_require_same_frozen_cohort,
            test_serialized_plan_summary_checks_route_legality,
        )
    )
