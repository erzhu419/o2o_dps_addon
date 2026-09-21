"""Causal Cat-relative ActionPlan teacher for the heterogeneous two-wave case.

The baseline and every branch start from a fresh native load with the same
simulator seed.  At one Cat-visited decision the branch replaces the complete
current decision (target, queue and terminal GCD/WAIT), then the *same* Cat
session continues for the rest of the two-wave segment.  Completed outcomes
are offline teacher labels; neither rewards nor future target rows enter the
runtime resolver.

This is development evidence only.  It deliberately does not freeze a
deployable policy or claim that a single fixed-seed branch is causal in the
real game.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
)
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .cat_fury_ordered_sink_executor_v5 import _ACTION_REFS
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_heterogeneous_two_wave_case_v1 import (
    build_heterogeneous_two_wave_observation_projector_v1,
)
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
    build_imported_incumbent_bindings_v1,
)
from .wave_action_schedule_v1 import QueueLaneOp
from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_action_plan_teacher/v8"
LANE_SCHEMA = "upper_kara_cat_action_plan_branch_lane/v8"
BINDING_ID = "cat.action-plan-teacher.v8"

BLOODTHIRST = ActionRef(spell_id=23_894)
WHIRLWIND = ActionRef(spell_id=1_680)
TURTLE_SLAM = ActionRef(spell_id=45_961)
EXECUTE = ActionRef(spell_id=20_662)
DEATH_WISH = ActionRef(spell_id=12_328)
RECKLESSNESS = ActionRef(spell_id=1_719)
HEROIC_STRIKE = ActionRef(spell_id=25_286, tag=1)
CLEAVE = ActionRef(spell_id=20_569, tag=1)

SEARCHED_GCD_ACTIONS_V8 = (
    BLOODTHIRST,
    WHIRLWIND,
    TURTLE_SLAM,
    EXECUTE,
    DEATH_WISH,
)
MECHANICS_UNCALIBRATED_GCD_ACTIONS_V8 = (RECKLESSNESS,)
SEARCHED_QUEUE_ACTIONS_V8 = (HEROIC_STRIKE, CLEAVE)

_QUEUE_KINDS = frozenset(
    {
        ProgramPrefixOperationKindV1.QUEUE_KEEP,
        ProgramPrefixOperationKindV1.QUEUE_SET,
        ProgramPrefixOperationKindV1.QUEUE_CANCEL,
    }
)
_ACTION_KEY_BY_REF = {
    **{value: key for key, value in _ACTION_REFS.items()},
    RECKLESSNESS: "warrior.recklessness",
}
_FORBIDDEN_POLICY_FIELDS = frozenset(
    {
        "environment_registry",
        "future_events",
        "future_target_rows",
        "remaining_ms",
        "seed",
        "source_instance_id",
        "source_wave_ref",
    }
)


class UpperKaraCatActionPlanTeacherV8Error(RuntimeError):
    """The paired teacher could not establish one faithful branch."""


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


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _visible_live_targets(
    observation: CausalLiveStateProjectionV1,
) -> tuple[tuple[int, int], ...]:
    semantics = observation.state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list):
        raise UpperKaraCatActionPlanTeacherV8Error(
            "causal observation lacks visible target semantics"
        )
    result: list[tuple[int, int]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise UpperKaraCatActionPlanTeacherV8Error(
                "causal target row is malformed"
            )
        policy_index = row.get("target_index")
        if isinstance(policy_index, bool) or not isinstance(policy_index, int):
            raise UpperKaraCatActionPlanTeacherV8Error(
                "causal target index is malformed"
            )
        if row.get("attackable") is True and row.get("dead") is False:
            result.append(
                (
                    policy_index,
                    observation.simulator_target_index(policy_index),
                )
            )
    return tuple(result)


def _wave_stratum(
    observation: CausalLiveStateProjectionV1,
) -> tuple[str, int]:
    live = _visible_live_targets(observation)
    if not live:
        return "NO_LIVE_TARGET", 0
    simulator_indexes = {simulator for _, simulator in live}
    if simulator_indexes <= {0, 1}:
        return "WAVE_1", len(live)
    if simulator_indexes <= {2}:
        return "WAVE_2", len(live)
    return "MIXED_VISIBLE_WAVES", len(live)


@dataclass(frozen=True)
class CatDecisionPointV8:
    decision_index: int
    observation: CausalLiveStateProjectionV1
    available_actions: tuple[AvailableAction, ...]
    cat_decision: ProgramDecisionV1

    def __post_init__(self) -> None:
        if (
            isinstance(self.decision_index, bool)
            or not isinstance(self.decision_index, int)
            or self.decision_index < 0
        ):
            raise ValueError("decision_index must be a nonnegative integer")
        if not isinstance(self.observation, CausalLiveStateProjectionV1):
            raise TypeError("observation must be CausalLiveStateProjectionV1")
        if any(not isinstance(row, AvailableAction) for row in self.available_actions):
            raise TypeError("available_actions must contain AvailableAction")
        if not isinstance(self.cat_decision, ProgramDecisionV1):
            raise TypeError("cat_decision must be ProgramDecisionV1")

    @property
    def stratum(self) -> tuple[str, int]:
        return _wave_stratum(self.observation)

    def prefix_row(self) -> JSONMap:
        leaked = sorted(_FORBIDDEN_POLICY_FIELDS & set(self.observation.state))
        if leaked:
            raise UpperKaraCatActionPlanTeacherV8Error(
                f"causal observation exposes forbidden fields: {leaked}"
            )
        wave, live_count = self.stratum
        return {
            "decision_index": self.decision_index,
            "state_time_ms": self.observation.visibility_cutoff_ms,
            "wave_stratum": wave,
            "live_target_count": live_count,
            "policy_to_simulator_target_index": list(
                self.observation.policy_to_simulator_target_index
            ),
            "policy_observation": deepcopy(self.observation.state),
            "available_actions": [
                _wire_available(row) for row in self.available_actions
            ],
            "cat_decision": self.cat_decision.to_dict(),
        }


@dataclass(frozen=True)
class ActionPlanBranchV8:
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

    @property
    def branch_key(self) -> str:
        return _canonical(
            {
                "decision_index": self.decision_index,
                "decision": self.decision.to_dict(),
            }
        )


def _queue_active(state: Mapping[str, Any]) -> bool:
    raw = state.get("queued_swing")
    status = raw.get("status") if isinstance(raw, Mapping) else raw
    return str(status or "").upper() not in {"", "NONE", "KEEP", "CANCEL"}


def _replace_queue_marker(
    order: tuple[ProgramPrefixOperationKindV1, ...],
    queue_op: QueueLaneOp,
) -> tuple[ProgramPrefixOperationKindV1, ...]:
    matches = [index for index, kind in enumerate(order) if kind in _QUEUE_KINDS]
    if len(matches) != 1:
        raise UpperKaraCatActionPlanTeacherV8Error(
            "Cat decision must contain exactly one queue prefix marker"
        )
    replacement = {
        QueueLaneOp.KEEP: ProgramPrefixOperationKindV1.QUEUE_KEEP,
        QueueLaneOp.SET: ProgramPrefixOperationKindV1.QUEUE_SET,
        QueueLaneOp.CANCEL: ProgramPrefixOperationKindV1.QUEUE_CANCEL,
    }[queue_op]
    result = list(order)
    result[matches[0]] = replacement
    return tuple(result)


def _replace_target_prefix(
    order: tuple[ProgramPrefixOperationKindV1, ...],
) -> tuple[ProgramPrefixOperationKindV1, ...]:
    remainder = tuple(
        kind
        for kind in order
        if kind
        not in {
            ProgramPrefixOperationKindV1.SET_TARGET,
            ProgramPrefixOperationKindV1.START_ATTACK,
        }
    )
    return (
        ProgramPrefixOperationKindV1.SET_TARGET,
        ProgramPrefixOperationKindV1.START_ATTACK,
        *remainder,
    )


def enumerate_cat_relative_action_plans_v8(
    point: CatDecisionPointV8,
) -> tuple[ProgramDecisionV1, ...]:
    """Enumerate current-ready Cat-relative target/queue/GCD alternatives.

    Membership is determined solely by the current causal observation and the
    native action snapshot.  Every alternative preserves Cat's current
    off-GCD/control sinks unless that lane is explicitly varied here.
    """

    if not isinstance(point, CatDecisionPointV8):
        raise TypeError("point must be CatDecisionPointV8")
    live_targets = _visible_live_targets(point.observation)
    if not live_targets:
        return ()
    ready = {
        row.action: row
        for row in point.available_actions
        if row.legal is True and row.ready_in_ms == 0
    }
    gcd_options: list[tuple[ActionRef | None, int | None]] = [
        (point.cat_decision.gcd_action, point.cat_decision.wait_ms)
    ]
    gcd_options.extend(
        (action, None)
        for action in SEARCHED_GCD_ACTIONS_V8
        if action in ready and ready[action].triggers_gcd is True
    )
    gcd_options.append((None, 100))

    queue_options: list[tuple[QueueLaneOp, ActionRef | None]] = [
        (point.cat_decision.queue_op, point.cat_decision.queue_action),
        (QueueLaneOp.KEEP, None),
    ]
    queue_options.extend(
        (QueueLaneOp.SET, action)
        for action in SEARCHED_QUEUE_ACTIONS_V8
        if action in ready and ready[action].triggers_gcd is False
    )
    if _queue_active(point.observation.state):
        queue_options.append((QueueLaneOp.CANCEL, None))

    source = point.cat_decision
    assert source.prefix_order is not None
    candidates: dict[str, tuple[int, ProgramDecisionV1]] = {}
    # Current AvailableAction legality belongs to the currently selected
    # target.  Therefore action and queue alternatives never share an epoch
    # with a retarget.  A target-only branch yields immediately; Cat receives
    # a fresh native action snapshot for the new target on the next epoch.
    for queue_op, queue_action in queue_options:
        for gcd_action, wait_ms in gcd_options:
            order = _replace_queue_marker(source.prefix_order, queue_op)
            candidate = replace(
                source,
                queue_op=queue_op,
                queue_action=queue_action,
                gcd_action=gcd_action,
                wait_ms=wait_ms,
                prefix_order=order,
            )
            if candidate == source:
                continue
            changed_axes = sum(
                (
                    (queue_op, queue_action)
                    != (source.queue_op, source.queue_action),
                    (gcd_action, wait_ms)
                    != (source.gcd_action, source.wait_ms),
                )
            )
            key = _canonical(candidate.to_dict())
            previous = candidates.get(key)
            if previous is None or changed_axes < previous[0]:
                candidates[key] = (changed_axes, candidate)

    selected_target = point.observation.state.get("target_index")
    for target, _ in live_targets:
        if target == selected_target:
            continue
        order = _replace_target_prefix(
            _replace_queue_marker(source.prefix_order, QueueLaneOp.KEEP)
        )
        order = tuple(
            kind
            for kind in order
            if kind is not ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
        )
        candidate = replace(
            source,
            target_index=target,
            start_attack=True,
            optional_off_gcd_prefixes=(),
            queue_op=QueueLaneOp.KEEP,
            queue_action=None,
            gcd_action=None,
            wait_ms=1,
            prefix_order=order,
        )
        key = _canonical(candidate.to_dict())
        candidates[key] = (1, candidate)
    return tuple(
        row[1]
        for _, row in sorted(
            candidates.items(),
            key=lambda item: (item[1][0], item[0]),
        )
    )


class CatActionPlanBranchSessionV8:
    """One fresh Cat session with at most one teacher intervention."""

    def __init__(
        self,
        cat_resolver: Callable[
            [CausalLiveStateProjectionV1, tuple[AvailableAction, ...]],
            ProgramDecisionV1,
        ],
        branch: ActionPlanBranchV8 | None = None,
    ) -> None:
        if not callable(cat_resolver):
            raise TypeError("cat_resolver must be callable")
        if branch is not None and not isinstance(branch, ActionPlanBranchV8):
            raise TypeError("branch must be ActionPlanBranchV8 or None")
        self.cat_resolver = cat_resolver
        self.branch = branch
        self.points: list[CatDecisionPointV8] = []
        self.interventions: list[JSONMap] = []
        self._pending_last_gcd_action: str | None = None

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("observation must be CausalLiveStateProjectionV1")
        if any(not isinstance(row, AvailableAction) for row in available):
            raise TypeError("available must contain AvailableAction")
        # The imported Cat resolver commits its proposed GCD before returning.
        # A residual branch must instead make the action actually returned at
        # the previous epoch visible to Cat now, immediately before Cat reads
        # its continuation state.
        if self._pending_last_gcd_action is not None:
            if hasattr(self.cat_resolver, "last_gcd_action"):
                setattr(
                    self.cat_resolver,
                    "last_gcd_action",
                    self._pending_last_gcd_action,
                )
            self._pending_last_gcd_action = None
        index = len(self.points)
        prior_last_gcd = getattr(self.cat_resolver, "last_gcd_action", None)
        cat_decision = self.cat_resolver(observation, available)
        if not isinstance(cat_decision, ProgramDecisionV1):
            raise TypeError("Cat resolver must return ProgramDecisionV1")
        point = CatDecisionPointV8(
            decision_index=index,
            observation=observation,
            available_actions=tuple(available),
            cat_decision=cat_decision,
        )
        self.points.append(point)
        if self.branch is None or index != self.branch.decision_index:
            return cat_decision
        if self.interventions:
            raise UpperKaraCatActionPlanTeacherV8Error(
                "one branch session cannot intervene more than once"
            )
        allowed = {
            _canonical(row.to_dict())
            for row in enumerate_cat_relative_action_plans_v8(point)
        }
        replacement_key = _canonical(self.branch.decision.to_dict())
        if replacement_key not in allowed:
            raise UpperKaraCatActionPlanTeacherV8Error(
                "frozen branch is not legal and ready at its causal observation"
            )

        # Restore the pre-proposal value now and defer committing the actual
        # returned GCD until the next decision epoch.  This mirrors execution:
        # Cat may propose Bloodthirst, while the branch actually returns
        # Whirlwind (or no GCD at all).
        if hasattr(self.cat_resolver, "last_gcd_action"):
            if self.branch.decision.gcd_action is None:
                continuation_key = prior_last_gcd or ""
            else:
                try:
                    continuation_key = _ACTION_KEY_BY_REF[
                        self.branch.decision.gcd_action
                    ]
                except KeyError as error:
                    raise UpperKaraCatActionPlanTeacherV8Error(
                        "replacement GCD lacks a canonical Cat continuation key"
                    ) from error
            setattr(self.cat_resolver, "last_gcd_action", prior_last_gcd or "")
            self._pending_last_gcd_action = continuation_key
        self.interventions.append(
            {
                "decision_index": index,
                "branch_key": self.branch.branch_key,
                "prefix": point.prefix_row(),
                "candidate_decision": self.branch.decision.to_dict(),
            }
        )
        return self.branch.decision


def select_cat_branch_points_v8(
    points: Sequence[CatDecisionPointV8],
    *,
    max_states: int,
) -> tuple[CatDecisionPointV8, ...]:
    """Select state strata deterministically without consulting rewards."""

    if isinstance(max_states, bool) or not isinstance(max_states, int) or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    strata_order = (
        ("WAVE_1", 2),
        ("WAVE_1", 1),
        ("WAVE_2", 1),
        ("MIXED_VISIBLE_WAVES", 1),
    )
    groups: dict[tuple[str, int], list[CatDecisionPointV8]] = {
        key: [] for key in strata_order
    }
    for point in points:
        if not enumerate_cat_relative_action_plans_v8(point):
            continue
        groups.setdefault(point.stratum, []).append(point)
    selected: list[CatDecisionPointV8] = []
    depth = 0
    while len(selected) < max_states:
        added = False
        for key in (*strata_order, *sorted(set(groups).difference(strata_order))):
            rows = groups.get(key, ())
            if depth < len(rows) and len(selected) < max_states:
                selected.append(rows[depth])
                added = True
        if not added:
            break
        depth += 1
    return tuple(selected)


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


def _make_program() -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id=BINDING_ID,
        selector=ImportedReactiveSelectorV1(
            binding_id=BINDING_ID,
            source_policy_id=CAT_POLICY_ID,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.SEARCHED_REACTIVE,
        source_refs=(CAT_POLICY_ID, SCHEMA),
    )


def run_upper_kara_cat_action_plan_teacher_v8(
    case: DevelopmentPrecombatWaveCaseV1,
    *,
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
    """Run paired fresh-load Cat branches to the end of both waves."""

    if not isinstance(case, DevelopmentPrecombatWaveCaseV1):
        raise TypeError("case must be DevelopmentPrecombatWaveCaseV1")
    if case.case_spec.get("build_id") != build_id:
        raise ValueError("case build differs from build_id")
    if isinstance(max_plans_per_state, bool) or not isinstance(
        max_plans_per_state, int
    ) or max_plans_per_state < 1:
        raise ValueError("max_plans_per_state must be a positive integer")
    if (
        isinstance(plan_start_index, bool)
        or not isinstance(plan_start_index, int)
        or plan_start_index < 0
    ):
        raise ValueError("plan_start_index must be a nonnegative integer")
    if (
        isinstance(branch_workers, bool)
        or not isinstance(branch_workers, int)
        or branch_workers < 1
    ):
        raise ValueError("branch_workers must be a positive integer")
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
    source_bindings = build_imported_incumbent_bindings_v1(
        build_id,
        replay_case,
        runtime_binding_path=runtime_binding_path,
    )
    cat_bindings = [row for row in source_bindings if row.source_policy_id == CAT_POLICY_ID]
    if len(cat_bindings) != 1:
        raise UpperKaraCatActionPlanTeacherV8Error(
            "exactly one Cat source binding is required"
        )
    source_binding = cat_bindings[0]
    resolved_bridge_path = Path(bridge_path).expanduser().resolve()
    resolved_bridge_cwd = Path(bridge_cwd).expanduser().resolve()
    open_bridge = bridge_factory or (
        lambda: SimulatorBridgePrecombatV1(
            resolved_bridge_path,
            cwd=resolved_bridge_cwd,
        )
    )
    program = _make_program()

    def run_lane(
        branch: ActionPlanBranchV8 | None,
    ) -> tuple[ScheduleReplayOutcomeV1, CatActionPlanBranchSessionV8]:
        sessions: list[CatActionPlanBranchSessionV8] = []

        def open_session() -> CatActionPlanBranchSessionV8:
            session = CatActionPlanBranchSessionV8(
                source_binding.open_session(), branch
            )
            sessions.append(session)
            return session

        binding = ImportedReactiveProgramBindingV1(
            binding_id=BINDING_ID,
            source_policy_id=CAT_POLICY_ID,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
            resolver_factory=open_session,
        )
        replay = NativeDynamicV3ActionProgramReplayV1(
            open_bridge,
            lambda requested_seed: {replay_seed: replay_case}[requested_seed],
            build_heterogeneous_two_wave_observation_projector_v1(replay_case),
            imported_bindings=(binding,),
        )
        outcome = replay.replay(replay_seed, program, max_decisions=max_decisions)
        if len(sessions) != 1:
            raise UpperKaraCatActionPlanTeacherV8Error(
                "native replay did not open exactly one Cat session"
            )
        return outcome, sessions[0]

    baseline_outcome, baseline_session = run_lane(None)
    baseline_terminal = _terminal(replay_case, baseline_outcome)
    common: JSONMap = {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "master_seed": master_seed,
        "simulator_seed": replay_seed,
        "build_id": build_id,
        "loadout_id": loadout_id,
        "branch_workers": branch_workers,
        "policy_input_contract": "CURRENT_CAUSAL_LIVE_STATE_PROJECTION_V1_ONLY",
        "state_selection_contract": (
            "DETERMINISTIC_WAVE_AND_LIVE_TARGET_STRATIFIED_CAT_VISITED_STATES;"
            "CURRENT_LEGAL_READY_ACTION_SNAPSHOT;NO_REWARD_OR_FUTURE_SUFFIX"
        ),
        "branch_contract": (
            "ONE_COMPLETE_CURRENT_ACTION_PLAN;FRESH_SAME_SEED_NATIVE_LOAD;"
            "EXACT_PREFIX_AND_CAT_PROPOSAL;SAME_CAT_SESSION_CONTINUATION"
        ),
        "mechanics_exclusions": [
            {
                "action": RECKLESSNESS.to_wire(),
                "status": "MECHANICS_UNCALIBRATED_NONVOTING",
                "reason": (
                    "local Turtle source comment and executable values conflict; "
                    "live evidence does not resolve crit bonus or duration"
                ),
            }
        ],
        "baseline_terminal": baseline_terminal,
        "visited_cat_decision_count": len(baseline_session.points),
        "comparison_ready": False,
        "deployment_eligible": False,
        "scientific_run_launched": False,
    }
    if baseline_terminal["status"] != "COMPLETED":
        return {
            **common,
            "status": "BASELINE_INCOMPLETE_NO_BRANCHES_SCORED",
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

    selected = select_cat_branch_points_v8(
        baseline_session.points,
        max_states=max_states,
    )
    branch_jobs: list[tuple[CatDecisionPointV8, int, ProgramDecisionV1]] = []
    candidate_counts: list[JSONMap] = []
    for point in selected:
        all_alternatives = enumerate_cat_relative_action_plans_v8(point)
        candidate_counts.append(
            {
                "decision_index": point.decision_index,
                "wave_stratum": point.stratum[0],
                "live_target_count": point.stratum[1],
                "candidate_action_plan_count": len(all_alternatives),
            }
        )
        alternatives = all_alternatives[
            plan_start_index : plan_start_index + max_plans_per_state
        ]
        for relative_plan_index, alternative in enumerate(alternatives):
            branch_jobs.append(
                (
                    point,
                    plan_start_index + relative_plan_index,
                    alternative,
                )
            )

    def score_branch(
        job: tuple[CatDecisionPointV8, int, ProgramDecisionV1],
    ) -> JSONMap:
        point, candidate_plan_index, alternative = job
        branch = ActionPlanBranchV8(point.decision_index, alternative)
        outcome, session = run_lane(branch)
        if len(session.interventions) != 1:
            raise UpperKaraCatActionPlanTeacherV8Error(
                "targeted branch did not intervene exactly once: "
                f"decision_index={point.decision_index}; "
                f"branch_key={branch.branch_key}; "
                f"visited={len(session.points)}; "
                f"replay_status={outcome.status.value}"
            )
        for index in range(point.decision_index + 1):
            if index >= len(session.points):
                raise UpperKaraCatActionPlanTeacherV8Error(
                    "candidate ended before the targeted Cat decision"
                )
            if (
                session.points[index].prefix_row()
                != baseline_session.points[index].prefix_row()
            ):
                raise UpperKaraCatActionPlanTeacherV8Error(
                    f"candidate prefix differs before branch at decision {index}"
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
            "decision_index": point.decision_index,
            "candidate_plan_index": candidate_plan_index,
            "wave_stratum": point.stratum[0],
            "live_target_count": point.stratum[1],
            "branch_key": branch.branch_key,
            "policy_observation": deepcopy(point.observation.state),
            "available_actions": [
                _wire_available(row) for row in point.available_actions
            ],
            "cat_decision": point.cat_decision.to_dict(),
            "candidate_decision": alternative.to_dict(),
            "cat_proposal_prefix_through_decision_index": point.decision_index,
            "cat_proposal_prefix_count_verified": point.decision_index + 1,
            "strict_single_intervention_verified": True,
            "execution_receipts": _branch_receipts(
                outcome, point.decision_index
            ),
            "branch_terminal": terminal,
            "paired_effective_damage_delta": damage_delta,
            "status": (
                "COMPLETE_BRANCH_TEACHER_LABEL"
                if complete
                else "INVALID_OR_INCOMPLETE_BRANCH"
            ),
        }

    if branch_workers == 1 or len(branch_jobs) <= 1:
        branches = [score_branch(job) for job in branch_jobs]
    else:
        with ThreadPoolExecutor(
            max_workers=min(branch_workers, len(branch_jobs))
        ) as executor:
            # executor.map preserves the predeclared state/plan order even
            # when independent native branches complete out of order.
            branches = list(executor.map(score_branch, branch_jobs))
    return {
        **common,
        "status": "COMPLETE_CAT_ACTION_PLAN_TEACHER_NONVOTING",
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
            }
            for row in selected
        ],
        "independent_action_plan_branch_count": len(branches),
        "completed_teacher_label_count": sum(
            row["status"] == "COMPLETE_BRANCH_TEACHER_LABEL"
            for row in branches
        ),
        "positive_single_seed_label_count": sum(
            row["status"] == "COMPLETE_BRANCH_TEACHER_LABEL"
            and row["paired_effective_damage_delta"] > 0
            for row in branches
        ),
        "branches": branches,
    }


__all__ = (
    "ActionPlanBranchV8",
    "BLOODTHIRST",
    "BINDING_ID",
    "CLEAVE",
    "CatActionPlanBranchSessionV8",
    "CatDecisionPointV8",
    "DEATH_WISH",
    "EXECUTE",
    "HEROIC_STRIKE",
    "LANE_SCHEMA",
    "RECKLESSNESS",
    "SCHEMA",
    "SEARCHED_GCD_ACTIONS_V8",
    "SEARCHED_QUEUE_ACTIONS_V8",
    "MECHANICS_UNCALIBRATED_GCD_ACTIONS_V8",
    "TURTLE_SLAM",
    "UpperKaraCatActionPlanTeacherV8Error",
    "WHIRLWIND",
    "enumerate_cat_relative_action_plans_v8",
    "run_upper_kara_cat_action_plan_teacher_v8",
    "select_cat_branch_points_v8",
)
