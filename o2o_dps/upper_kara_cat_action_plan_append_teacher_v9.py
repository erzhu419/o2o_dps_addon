"""Append-only ActionPlan teacher over one frozen v8 residual parent.

Every lane starts from the same native load and runs the complete frozen
parent.  A branch may replace one parent decision only at a later epoch where
all existing parent steps for the current wave were already executed.  The
same parent/Cat session then continues to the terminal state.  This produces
paired development labels for adding a next residual step; it does not expose
the branch decision index or future suffix to a deployable policy.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    ImportedReactiveProgramBindingV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramDecisionV1,
)
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_two_wave_cat_residual_sequence_v1 import (
    CatResidualSequenceDecisionTraceV1,
    CatResidualSequenceWaveV1,
    DevelopmentTwoWaveCatResidualSequenceSessionV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
    frozen_two_wave_cat_residual_sequence_wire_v1,
    two_wave_cat_residual_sequence_from_dict_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_cat_action_plan_teacher_v8 import (
    CatDecisionPointV8,
    RECKLESSNESS,
    enumerate_cat_relative_action_plans_v8,
)
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_heterogeneous_two_wave_case_v1 import (
    build_heterogeneous_two_wave_observation_projector_v1,
)
from .upper_kara_imported_incumbent_program_v1 import (
    build_imported_incumbent_bindings_v1,
)
from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_action_plan_append_teacher/v9"
LANE_SCHEMA = "upper_kara_cat_action_plan_append_branch_lane/v9"
COMPLETE_STATUS = "COMPLETE_CAT_ACTION_PLAN_APPEND_TEACHER_NONVOTING"
COMPLETE_BRANCH_STATUS = "COMPLETE_APPEND_BRANCH_TEACHER_LABEL"
SCOPE = "MODEL_DEFINED_DEVELOPMENT_ONLY"


class UpperKaraCatActionPlanAppendTeacherV9Error(RuntimeError):
    """A frozen-parent append branch could not be established faithfully."""


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decision_actions(decision: ProgramDecisionV1) -> tuple[ActionRef, ...]:
    actions = [row.action for row in decision.optional_off_gcd_prefixes]
    if decision.queue_action is not None:
        actions.append(decision.queue_action)
    if decision.gcd_action is not None:
        actions.append(decision.gcd_action)
    return tuple(actions)


def _wire_available(row: AvailableAction) -> JSONMap:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
        "result_bearing": row.result_bearing,
        "cooldown_duration_ms": row.cooldown_duration_ms,
    }


def _required_targets_dead(
    case: DevelopmentPrecombatWaveCaseV1,
    state: Mapping[str, Any],
) -> bool:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required = case.case_spec.get("required_target_indices")
    return bool(
        isinstance(targets, list)
        and isinstance(required, list)
        and required
        and all(
            type(index) is int
            and 0 <= index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("dead") is True
            for index in required
        )
    )


def _terminal(
    case: DevelopmentPrecombatWaveCaseV1,
    outcome: ScheduleReplayOutcomeV1,
) -> JSONMap:
    complete = (
        outcome.status is ReplayStatusV1.COMPLETE
        and _required_targets_dead(case, outcome.state)
    )
    return {
        "status": "COMPLETED" if complete else outcome.status.value,
        "own_effective_damage": outcome.effective_damage if complete else None,
        "elapsed_ms": outcome.elapsed_ms if complete else None,
        "invalid_reason": outcome.invalid_reason,
        "required_targets_dead": _required_targets_dead(case, outcome.state),
    }


def _branch_receipts(
    outcome: ScheduleReplayOutcomeV1,
    decision_index: int,
) -> list[JSONMap]:
    return [
        deepcopy(dict(row))
        for row in outcome.receipts
        if row.get("decision_index") == decision_index
    ]


def _current_wave(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    observation: CausalLiveStateProjectionV1,
) -> CatResidualSequenceWaveV1 | None:
    precombat = observation.state.get("precombat")
    if isinstance(precombat, Mapping) and precombat.get("active") is True:
        return None
    semantics = observation.state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list):
        raise UpperKaraCatActionPlanAppendTeacherV9Error(
            "causal observation lacks visible target semantics"
        )
    active: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "visible target semantics contain a malformed row"
            )
        target_index = row.get("target_index")
        if isinstance(target_index, bool) or not isinstance(target_index, int):
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "visible target semantics lack an integer target_index"
            )
        if row.get("attackable") is True and row.get("dead") is not True:
            active.add(target_index)
    selected = observation.state.get("target_index")
    if isinstance(selected, int) and not isinstance(selected, bool) and selected in active:
        for wave in policy.waves:
            if selected in wave.target_indexes:
                return wave
    for wave in policy.waves:
        if active.intersection(wave.target_indexes):
            return wave
    return None


def _wave_step_keys(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    wave_id: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (step.wave_id, step.step_id)
        for step in policy.steps
        if step.wave_id == wave_id
    )


@dataclass(frozen=True)
class ParentAppendDecisionPointV9:
    """One visited parent state plus its already executed residual prefix."""

    trace: CatResidualSequenceDecisionTraceV1
    wave_id: str | None
    required_parent_wave_step_keys: tuple[tuple[str, str], ...]

    @property
    def decision_index(self) -> int:
        return self.trace.decision_index

    @property
    def observation(self) -> CausalLiveStateProjectionV1:
        return self.trace.observation

    @property
    def available_actions(self) -> tuple[AvailableAction, ...]:
        return self.trace.available_actions

    @property
    def cat_decision(self) -> ProgramDecisionV1:
        return self.trace.cat_decision

    @property
    def parent_decision(self) -> ProgramDecisionV1:
        return self.trace.parent_decision

    @property
    def stratum(self) -> tuple[str, int]:
        return self._v8_point().stratum

    @property
    def append_ready(self) -> bool:
        if self.wave_id is None or not self.required_parent_wave_step_keys:
            return False
        executed_before = self.trace.executed_step_keys_before
        executed_in_wave = tuple(
            key for key in executed_before if key[0] == self.wave_id
        )
        return bool(
            executed_in_wave == self.required_parent_wave_step_keys
            and self.trace.executed_step_keys_after == executed_before
        )

    def _v8_point(self) -> CatDecisionPointV8:
        # The v8 enumerator is intentionally reused with the complete parent
        # decision as its source.  This makes the new branch relative to the
        # frozen parent, not relative to an independently restarted Cat lane.
        return CatDecisionPointV8(
            decision_index=self.decision_index,
            observation=self.observation,
            available_actions=self.available_actions,
            cat_decision=self.parent_decision,
        )

    def alternatives(self) -> tuple[ProgramDecisionV1, ...]:
        if not self.append_ready:
            return ()
        rows = enumerate_cat_relative_action_plans_v8(self._v8_point())
        if any(RECKLESSNESS in _decision_actions(row) for row in rows):
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "mechanics-excluded Recklessness entered append alternatives"
            )
        return rows

    def prefix_row(self) -> JSONMap:
        row = self._v8_point().prefix_row()
        row["cat_decision"] = self.cat_decision.to_dict()
        row["parent_decision"] = self.parent_decision.to_dict()
        row["parent_wave_id"] = self.wave_id
        row["parent_executed_step_keys_before"] = [
            list(key) for key in self.trace.executed_step_keys_before
        ]
        row["parent_executed_step_keys_after"] = [
            list(key) for key in self.trace.executed_step_keys_after
        ]
        row["required_parent_wave_step_keys"] = [
            list(key) for key in self.required_parent_wave_step_keys
        ]
        return row


@dataclass(frozen=True)
class AppendActionPlanBranchV9:
    decision_index: int
    decision: ProgramDecisionV1

    def __post_init__(self) -> None:
        if (
            isinstance(self.decision_index, bool)
            or not isinstance(self.decision_index, int)
            or self.decision_index < 0
        ):
            raise ValueError("decision_index must be a nonnegative integer")
        if not isinstance(self.decision, ProgramDecisionV1):
            raise TypeError("decision must be ProgramDecisionV1")
        if RECKLESSNESS in _decision_actions(self.decision):
            raise ValueError("Recklessness is mechanics-excluded from v9 branches")

    @property
    def branch_key(self) -> str:
        return _canonical(
            {
                "decision_index": self.decision_index,
                "decision": self.decision.to_dict(),
            }
        )


class ParentAppendBranchSessionV9:
    """One complete parent session with at most one later append branch."""

    def __init__(
        self,
        policy: DevelopmentTwoWaveCatResidualSequenceV1,
        parent_session: DevelopmentTwoWaveCatResidualSequenceSessionV1,
        branch: AppendActionPlanBranchV9 | None = None,
    ) -> None:
        if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
            raise TypeError("policy must be DevelopmentTwoWaveCatResidualSequenceV1")
        if not isinstance(
            parent_session, DevelopmentTwoWaveCatResidualSequenceSessionV1
        ):
            raise TypeError(
                "parent_session must be DevelopmentTwoWaveCatResidualSequenceSessionV1"
            )
        if branch is not None and not isinstance(branch, AppendActionPlanBranchV9):
            raise TypeError("branch must be AppendActionPlanBranchV9 or None")
        self.policy = policy
        self.parent_session = parent_session
        self.branch = branch
        self.points: list[ParentAppendDecisionPointV9] = []
        self.interventions: list[JSONMap] = []

    def record_last_executed_decision_v1(
        self,
        actual_decision: ProgramDecisionV1,
    ) -> None:
        """Forward executor acceptance to the stateful parent session."""

        self.parent_session.record_last_executed_decision_v1(actual_decision)

    def reject_last_execution_v1(self, reason: str) -> None:
        """Forward executor rejection without consuming a parent residual."""

        self.parent_session.reject_last_execution_v1(reason)

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        parent_decision = self.parent_session(observation, available)
        trace = self.parent_session.last_decision_trace
        if trace is None or trace.parent_decision != parent_decision:
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "parent session did not publish its current decision trace"
            )
        wave = _current_wave(self.policy, observation)
        point = ParentAppendDecisionPointV9(
            trace=trace,
            wave_id=None if wave is None else wave.wave_id,
            required_parent_wave_step_keys=(
                () if wave is None else _wave_step_keys(self.policy, wave.wave_id)
            ),
        )
        self.points.append(point)
        if self.branch is None or point.decision_index != self.branch.decision_index:
            return parent_decision
        if self.interventions:
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "one append branch session cannot intervene more than once"
            )
        if not point.append_ready:
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "append branch precedes completion of its parent wave prefix"
            )
        allowed = {_canonical(row.to_dict()) for row in point.alternatives()}
        if _canonical(self.branch.decision.to_dict()) not in allowed:
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "append branch is not legal and ready at its causal parent state"
            )
        self.interventions.append(
            {
                "decision_index": point.decision_index,
                "branch_key": self.branch.branch_key,
                "parent_executed_step_keys_before": [
                    list(key) for key in trace.executed_step_keys_before
                ],
                "required_parent_wave_step_keys": [
                    list(key) for key in point.required_parent_wave_step_keys
                ],
            }
        )
        return self.branch.decision


def select_parent_append_points_v9(
    points: Sequence[ParentAppendDecisionPointV9],
    *,
    max_states: int,
) -> tuple[ParentAppendDecisionPointV9, ...]:
    """Select append-ready strata deterministically without reward access."""

    if isinstance(max_states, bool) or not isinstance(max_states, int) or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    strata_order = (
        ("WAVE_1", 2),
        ("WAVE_1", 1),
        ("WAVE_2", 1),
        ("MIXED_VISIBLE_WAVES", 1),
    )
    groups: dict[tuple[str, int], list[ParentAppendDecisionPointV9]] = {
        key: [] for key in strata_order
    }
    for point in points:
        if not point.alternatives():
            continue
        groups.setdefault(point.stratum, []).append(point)
    selected: list[ParentAppendDecisionPointV9] = []
    depth = 0
    while len(selected) < max_states:
        added = False
        keys = (*strata_order, *sorted(set(groups).difference(strata_order)))
        for key in keys:
            rows = groups.get(key, ())
            if depth < len(rows) and len(selected) < max_states:
                selected.append(rows[depth])
                added = True
        if not added:
            break
        depth += 1
    return tuple(selected)


def _validate_parent(
    parent_policy: DevelopmentTwoWaveCatResidualSequenceV1,
    *,
    build_id: str,
    case: DevelopmentPrecombatWaveCaseV1,
) -> JSONMap:
    if not isinstance(parent_policy, DevelopmentTwoWaveCatResidualSequenceV1):
        raise TypeError(
            "parent_policy must be DevelopmentTwoWaveCatResidualSequenceV1"
        )
    if not parent_policy.steps:
        raise ValueError("v9 append teacher requires a nonempty frozen parent")
    if parent_policy.exact_build_id != build_id:
        raise ValueError("parent exact build differs from build_id")
    if any(
        RECKLESSNESS in _decision_actions(step.decision)
        for step in parent_policy.steps
    ):
        raise ValueError("parent contains mechanics-excluded Recklessness")
    required = case.case_spec.get("required_target_indices")
    parent_targets = tuple(
        target for wave in parent_policy.waves for target in wave.target_indexes
    )
    if not isinstance(required, list) or set(required) != set(parent_targets):
        raise ValueError("parent wave targets differ from case required targets")
    wire = frozen_two_wave_cat_residual_sequence_wire_v1(parent_policy)
    if two_wave_cat_residual_sequence_from_dict_v1(deepcopy(wire)) != parent_policy:
        raise ValueError("parent differs after its closed-wire round trip")
    return wire


def run_upper_kara_cat_action_plan_append_teacher_v9(
    case: DevelopmentPrecombatWaveCaseV1,
    *,
    parent_policy: DevelopmentTwoWaveCatResidualSequenceV1,
    build_id: str,
    loadout_id: str,
    simulator_seed: int | None = None,
    max_states: int = 3,
    plan_start_index: int = 0,
    max_plans_per_state: int = 16,
    branch_workers: int = 1,
    max_decisions: int = 10_000,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    bridge_factory: Callable[[], Any] | None = None,
) -> JSONMap:
    """Score same-seed single replacements after the frozen parent prefix."""

    if not isinstance(case, DevelopmentPrecombatWaveCaseV1):
        raise TypeError("case must be DevelopmentPrecombatWaveCaseV1")
    if case.case_spec.get("build_id") != build_id:
        raise ValueError("case build differs from build_id")
    if (
        isinstance(plan_start_index, bool)
        or not isinstance(plan_start_index, int)
        or plan_start_index < 0
    ):
        raise ValueError("plan_start_index must be a nonnegative integer")
    if (
        isinstance(max_plans_per_state, bool)
        or not isinstance(max_plans_per_state, int)
        or max_plans_per_state < 1
    ):
        raise ValueError("max_plans_per_state must be a positive integer")
    if (
        isinstance(branch_workers, bool)
        or not isinstance(branch_workers, int)
        or branch_workers < 1
    ):
        raise ValueError("branch_workers must be a positive integer")
    if isinstance(max_decisions, bool) or not isinstance(max_decisions, int) or max_decisions < 1:
        raise ValueError("max_decisions must be a positive integer")
    if simulator_seed is not None and (
        isinstance(simulator_seed, bool)
        or not isinstance(simulator_seed, int)
        or simulator_seed < 0
    ):
        raise ValueError("simulator_seed must be a nonnegative integer or None")

    master_seed = case.dynamic_load.seed
    replay_seed = master_seed if simulator_seed is None else simulator_seed
    replay_case = replace(
        case,
        dynamic_load=DynamicRolloutLoadV3.bind(
            case.request,
            replay_seed,
            case.dynamic_load.config,
        ),
    )
    parent_wire = _validate_parent(
        parent_policy,
        build_id=build_id,
        case=replay_case,
    )
    source_bindings = build_imported_incumbent_bindings_v1(
        build_id,
        replay_case,
        runtime_binding_path=runtime_binding_path,
    )
    cat_bindings = [
        row for row in source_bindings if row.source_policy_id == CAT_POLICY_ID
    ]
    if len(cat_bindings) != 1:
        raise UpperKaraCatActionPlanAppendTeacherV9Error(
            "exactly one Cat source binding is required"
        )
    parent_program, raw_parent_binding = build_two_wave_cat_residual_sequence_runtime_v1(
        parent_policy,
        cat_resolver_factory=cat_bindings[0].open_session,
    )
    resolved_bridge_path = Path(bridge_path).expanduser().resolve()
    resolved_bridge_cwd = Path(bridge_cwd).expanduser().resolve()
    open_bridge = bridge_factory or (
        lambda: SimulatorBridgePrecombatV1(
            resolved_bridge_path,
            cwd=resolved_bridge_cwd,
        )
    )

    def run_lane(
        branch: AppendActionPlanBranchV9 | None,
    ) -> tuple[ScheduleReplayOutcomeV1, ParentAppendBranchSessionV9]:
        sessions: list[ParentAppendBranchSessionV9] = []

        def open_session() -> ParentAppendBranchSessionV9:
            parent_session = raw_parent_binding.open_session()
            if not isinstance(
                parent_session, DevelopmentTwoWaveCatResidualSequenceSessionV1
            ):
                raise TypeError("parent runtime opened an unexpected session type")
            session = ParentAppendBranchSessionV9(
                parent_policy,
                parent_session,
                branch,
            )
            sessions.append(session)
            return session

        binding = ImportedReactiveProgramBindingV1(
            binding_id=raw_parent_binding.binding_id,
            source_policy_id=raw_parent_binding.source_policy_id,
            observation_contract_id=raw_parent_binding.observation_contract_id,
            resolver_factory=open_session,
        )
        replay = NativeDynamicV3ActionProgramReplayV1(
            open_bridge,
            lambda requested_seed: {replay_seed: replay_case}[requested_seed],
            build_heterogeneous_two_wave_observation_projector_v1(replay_case),
            imported_bindings=(binding,),
        )
        outcome = replay.replay(
            replay_seed,
            parent_program,
            max_decisions=max_decisions,
        )
        if len(sessions) != 1:
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "native replay did not open exactly one parent session"
            )
        return outcome, sessions[0]

    baseline_outcome, baseline_session = run_lane(None)
    baseline_terminal = _terminal(replay_case, baseline_outcome)
    common: JSONMap = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "master_seed": master_seed,
        "simulator_seed": replay_seed,
        "build_id": build_id,
        "loadout_id": loadout_id,
        "branch_workers": branch_workers,
        "parent_policy_id": parent_policy.policy_id,
        "parent_policy_wire": parent_wire,
        "parent_step_count": len(parent_policy.steps),
        "baseline_terminal": baseline_terminal,
        "baseline_parent_decision_count": len(baseline_session.points),
        "baseline_executed_step_keys": [
            list(key)
            for key in baseline_session.parent_session.executed_step_keys
        ],
        "policy_input_contract": "CURRENT_CAUSAL_LIVE_STATE_PROJECTION_V1_ONLY",
        "state_selection_contract": (
            "DETERMINISTIC_APPEND_READY_PARENT_VISITED_STATES;"
            "ALL_CURRENT_WAVE_PARENT_STEPS_EXECUTED_BEFORE_BRANCH;"
            "CURRENT_LEGAL_READY_ACTION_SNAPSHOT;NO_REWARD_OR_FUTURE_SUFFIX"
        ),
        "branch_contract": (
            "ONE_COMPLETE_CURRENT_ACTION_PLAN;FRESH_SAME_SEED_NATIVE_LOAD;"
            "EXACT_PARENT_PREFIX;SAME_PARENT_AND_CAT_SESSION_CONTINUATION"
        ),
        "mechanics_exclusions": [
            {
                "action": RECKLESSNESS.to_wire(),
                "status": "MECHANICS_UNCALIBRATED_NONVOTING",
            }
        ],
        "comparison_ready": False,
        "deployment_eligible": False,
        "scientific_run_launched": False,
    }
    if baseline_terminal["status"] != "COMPLETED":
        return {
            **common,
            "status": "PARENT_BASELINE_INCOMPLETE_NO_APPEND_BRANCHES_SCORED",
            "selected_state_count": 0,
            "plan_slice": {
                "start_index": plan_start_index,
                "max_plans_per_state": max_plans_per_state,
            },
            "candidate_action_plan_counts": [],
            "independent_action_plan_branch_count": 0,
            "completed_teacher_label_count": 0,
            "positive_single_seed_label_count": 0,
            "branches": [],
        }

    selected = select_parent_append_points_v9(
        baseline_session.points,
        max_states=max_states,
    )
    branch_jobs: list[
        tuple[ParentAppendDecisionPointV9, int, ProgramDecisionV1]
    ] = []
    candidate_counts: list[JSONMap] = []
    for point in selected:
        alternatives = point.alternatives()
        candidate_counts.append(
            {
                "decision_index": point.decision_index,
                "wave_stratum": point.stratum[0],
                "live_target_count": point.stratum[1],
                "parent_wave_id": point.wave_id,
                "required_parent_wave_step_keys": [
                    list(key) for key in point.required_parent_wave_step_keys
                ],
                "candidate_action_plan_count": len(alternatives),
            }
        )
        sliced = alternatives[
            plan_start_index : plan_start_index + max_plans_per_state
        ]
        for relative_index, alternative in enumerate(sliced):
            branch_jobs.append(
                (point, plan_start_index + relative_index, alternative)
            )

    def score_branch(
        job: tuple[ParentAppendDecisionPointV9, int, ProgramDecisionV1],
    ) -> JSONMap:
        baseline_point, candidate_plan_index, alternative = job
        branch = AppendActionPlanBranchV9(
            baseline_point.decision_index,
            alternative,
        )
        outcome, session = run_lane(branch)
        if len(session.interventions) != 1:
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "targeted append branch did not intervene exactly once"
            )
        index = baseline_point.decision_index
        if index >= len(session.points):
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "append candidate ended before its targeted parent state"
            )
        for prefix_index in range(index + 1):
            if (
                session.points[prefix_index].prefix_row()
                != baseline_session.points[prefix_index].prefix_row()
            ):
                raise UpperKaraCatActionPlanAppendTeacherV9Error(
                    f"candidate parent prefix differs at decision {prefix_index}"
                )
        candidate_point = session.points[index]
        parent_prefix_verified = bool(
            candidate_point.trace.executed_step_keys_before
            == baseline_point.trace.executed_step_keys_before
            and candidate_point.required_parent_wave_step_keys
            == baseline_point.required_parent_wave_step_keys
        )
        append_verified = bool(
            candidate_point.append_ready
            and parent_prefix_verified
            and candidate_point.required_parent_wave_step_keys
        )
        causal_verified = (
            candidate_point.observation.state.get("time_ms")
            == candidate_point.observation.visibility_cutoff_ms
            and candidate_point.prefix_row() == baseline_point.prefix_row()
        )
        if not (parent_prefix_verified and append_verified and causal_verified):
            raise UpperKaraCatActionPlanAppendTeacherV9Error(
                "append branch failed parent-prefix or causal-state verification"
            )
        terminal = _terminal(replay_case, outcome)
        complete = terminal["status"] == "COMPLETED"
        damage_delta = (
            terminal["own_effective_damage"]
            - baseline_terminal["own_effective_damage"]
            if complete
            else None
        )
        return {
            "decision_index": index,
            "candidate_plan_index": candidate_plan_index,
            "wave_stratum": baseline_point.stratum[0],
            "live_target_count": baseline_point.stratum[1],
            "parent_wave_id": baseline_point.wave_id,
            "branch_key": branch.branch_key,
            "policy_observation": deepcopy(baseline_point.observation.state),
            "available_actions": [
                _wire_available(row) for row in baseline_point.available_actions
            ],
            "cat_decision": baseline_point.cat_decision.to_dict(),
            "parent_decision": baseline_point.parent_decision.to_dict(),
            "candidate_decision": alternative.to_dict(),
            "parent_executed_step_keys_before_branch": [
                list(key)
                for key in baseline_point.trace.executed_step_keys_before
            ],
            "parent_executed_step_keys_after_branch": [
                list(key)
                for key in baseline_point.trace.executed_step_keys_after
            ],
            "required_parent_wave_step_keys": [
                list(key) for key in baseline_point.required_parent_wave_step_keys
            ],
            "parent_execution_prefix_verified": parent_prefix_verified,
            "parent_decision_prefix_verified": True,
            "causal_observation_verified": causal_verified,
            "append_after_parent_steps_verified": append_verified,
            "strict_single_intervention_verified": True,
            "parent_proposal_prefix_through_decision_index": index,
            "parent_proposal_prefix_count_verified": index + 1,
            "execution_receipts": _branch_receipts(outcome, index),
            "branch_terminal": terminal,
            "paired_effective_damage_delta": damage_delta,
            "status": (
                COMPLETE_BRANCH_STATUS
                if complete
                else "INVALID_OR_INCOMPLETE_APPEND_BRANCH"
            ),
        }

    if branch_workers == 1 or len(branch_jobs) <= 1:
        branches = [score_branch(job) for job in branch_jobs]
    else:
        with ThreadPoolExecutor(
            max_workers=min(branch_workers, len(branch_jobs))
        ) as executor:
            branches = list(executor.map(score_branch, branch_jobs))

    return {
        **common,
        "status": COMPLETE_STATUS,
        "selected_state_count": len(selected),
        "plan_slice": {
            "start_index": plan_start_index,
            "max_plans_per_state": max_plans_per_state,
        },
        "candidate_action_plan_counts": candidate_counts,
        "selected_state_strata": [
            {
                "decision_index": row.decision_index,
                "wave_stratum": row.stratum[0],
                "live_target_count": row.stratum[1],
                "parent_wave_id": row.wave_id,
                "required_parent_wave_step_keys": [
                    list(key) for key in row.required_parent_wave_step_keys
                ],
            }
            for row in selected
        ],
        "independent_action_plan_branch_count": len(branches),
        "completed_teacher_label_count": sum(
            row["status"] == COMPLETE_BRANCH_STATUS for row in branches
        ),
        "positive_single_seed_label_count": sum(
            row["status"] == COMPLETE_BRANCH_STATUS
            and row["paired_effective_damage_delta"] > 0
            for row in branches
        ),
        "branches": branches,
    }


__all__ = (
    "AppendActionPlanBranchV9",
    "COMPLETE_BRANCH_STATUS",
    "COMPLETE_STATUS",
    "LANE_SCHEMA",
    "ParentAppendBranchSessionV9",
    "ParentAppendDecisionPointV9",
    "SCHEMA",
    "SCOPE",
    "UpperKaraCatActionPlanAppendTeacherV9Error",
    "run_upper_kara_cat_action_plan_append_teacher_v9",
    "select_parent_append_points_v9",
)
