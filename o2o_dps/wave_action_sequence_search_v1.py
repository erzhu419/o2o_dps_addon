"""Multi-seed beam search over independent finite wave action schedules.

The candidate is an ordered schedule for one exact scenario/build cell, not a
Cat residual and not a universal state policy.  Expert sources only rank the
complete native action expansion.  Every evaluated branch is reconstructed by
fresh fixed-seed replay because o2obridge has no general snapshot contract.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
import math
from statistics import mean
from typing import Any, Callable, Mapping, Protocol, Sequence

from .causal_guard_v1 import (
    GuardTimeoutError,
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
    next_observable_guard_check_ms_v1,
)
from .sim_bridge import ActionRef, AvailableAction
from .wave_action_schedule_v1 import (
    EquipmentAction,
    QueueLaneOp,
    ScheduledActionPlan,
    ScheduledOperationKind,
    SearchCellIdentity,
    enumerate_scheduled_action_plans,
)
from .upper_kara_wave_target_gate_v1 import (
    RuntimeTargetGateV1,
    WaveTargetGateDecisionV1,
)


JSONMap = dict[str, Any]


# This is the exact result-bearing set implemented by the current Fury
# o2obridge contract.  These actions must carry an encounter-unique attempt ID;
# attaching one to any other action is itself a protocol error.
FURY_RESULT_BEARING_ACTION_REFS_V1 = frozenset(
    {
        ActionRef(spell_id=23894),  # Bloodthirst
        ActionRef(spell_id=1680),   # Whirlwind
        ActionRef(spell_id=45961),  # Turtle Slam
        ActionRef(spell_id=20662),  # Execute
        ActionRef(spell_id=7373),   # Hamstring
        ActionRef(spell_id=6552),   # Pummel
        ActionRef(spell_id=11597),  # Sunder Armor
        ActionRef(spell_id=11585),  # Overpower
        ActionRef(spell_id=1672),   # Shield Bash
        ActionRef(spell_id=12809),  # Concussion Blow
    }
)

_FURY_SWING_QUEUE_REFS_V1 = frozenset(
    {
        ActionRef(spell_id=25286, tag=1),  # Heroic Strike queue
        ActionRef(spell_id=20569, tag=1),  # Cleave queue
    }
)


# The current native bridge does not expose an action-specific hit mask.  A
# direct Whirlwind can therefore be admitted only when every *currently*
# attackable target it may hit is legal collateral.  Cleave and Sweeping
# Strikes can create damage on a later swing, after the decision that accepted
# the action, so they need the stricter all-living-target check until the
# bridge reports and enforces their eventual hit targets.
FURY_IMMEDIATE_UNMASKED_COLLATERAL_SPELL_IDS_V1 = frozenset({1680})
FURY_DELAYED_UNMASKED_COLLATERAL_SPELL_IDS_V1 = frozenset({20569, 12292})


class ReplayStatusV1(str, Enum):
    FRONTIER = "FRONTIER"
    COMPLETE = "COMPLETE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ScheduleReplayOutcomeV1:
    """Fresh replay result for one schedule and simulator seed."""

    seed: int
    status: ReplayStatusV1
    state: Mapping[str, Any]
    available_actions: tuple[AvailableAction, ...] = ()
    receipts: tuple[Mapping[str, Any], ...] = ()
    invalid_reason: str | None = None
    target_gate: WaveTargetGateDecisionV1 | None = None

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("seed must be an integer")
        if not isinstance(self.status, ReplayStatusV1):
            raise TypeError("status must be ReplayStatusV1")
        if not isinstance(self.state, Mapping):
            raise TypeError("state must be a mapping")
        if any(not isinstance(row, AvailableAction) for row in self.available_actions):
            raise TypeError("available_actions must contain AvailableAction values")
        if self.status is ReplayStatusV1.INVALID:
            if not isinstance(self.invalid_reason, str) or not self.invalid_reason:
                raise ValueError("INVALID replay requires invalid_reason")
        elif self.invalid_reason is not None:
            raise ValueError("only INVALID replay may carry invalid_reason")
        if self.target_gate is not None and not isinstance(
            self.target_gate, WaveTargetGateDecisionV1
        ):
            raise TypeError("target_gate must be WaveTargetGateDecisionV1 or None")
        if self.status is ReplayStatusV1.FRONTIER and not self.available_actions:
            raise ValueError("FRONTIER replay requires available_actions")

    @property
    def elapsed_ms(self) -> int:
        value = self.state.get("time_ms")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("replay state lacks nonnegative integer time_ms")
        return value

    @property
    def effective_damage(self) -> float:
        lifecycle = self.state.get("dynamic_team_background")
        value = (
            lifecycle.get("simulated_damage_applied")
            if isinstance(lifecycle, Mapping)
            else self.state.get("damage_done")
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError("replay state lacks finite nonnegative candidate damage")
        return float(value)

    def compact_dict(self) -> JSONMap:
        result: JSONMap = {
            "seed": self.seed,
            "status": self.status.value,
            "elapsed_ms": self.elapsed_ms,
            "effective_damage": self.effective_damage,
            "invalid_reason": self.invalid_reason,
        }
        precombat = self.state.get("precombat")
        if isinstance(precombat, Mapping):
            pull_time_ms = precombat.get("pull_time_ms")
            relative_to_pull_ms = precombat.get("relative_time_ms")
            if (
                isinstance(pull_time_ms, bool)
                or not isinstance(pull_time_ms, int)
                or pull_time_ms <= 0
                or isinstance(relative_to_pull_ms, bool)
                or not isinstance(relative_to_pull_ms, int)
                or relative_to_pull_ms != self.elapsed_ms - pull_time_ms
            ):
                raise ValueError("replay precombat clock is malformed")
            result["relative_to_pull_ms"] = relative_to_pull_ms
        if self.target_gate is not None:
            result["target_gate"] = self.target_gate.to_dict()
        return result


class ScheduleReplayV1(Protocol):
    def replay(
        self, seed: int, schedule: Sequence[ScheduledActionPlan]
    ) -> ScheduleReplayOutcomeV1: ...


class ActionGuideV1(Protocol):
    """Proposal-only guide; returned weights may not change membership."""

    guide_id: str

    def action_priorities(
        self,
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> Mapping[ActionRef, float]: ...


@dataclass(frozen=True)
class WaveActionSequenceSearchResultV1:
    cell: SearchCellIdentity
    status: str
    schedule: tuple[ScheduledActionPlan, ...]
    outcomes: tuple[ScheduleReplayOutcomeV1, ...]
    mean_dps: float | None
    search_ranking_score: float
    search_ranking_metric: str
    evaluated_schedule_count: int
    completed_depth: int
    guide_ids: tuple[str, ...]
    replay_workers: int
    continuation_max_steps: int = 0
    continuation_seed_replay_count: int = 0
    continuation_scored_prefix_count: int = 0
    continuation_completed_prefix_count: int = 0

    def to_dict(self) -> JSONMap:
        pull_times = {
            precombat["pull_time_ms"]
            for outcome in self.outcomes
            if isinstance(
                precombat := outcome.state.get("precombat"), Mapping
            )
            and type(precombat.get("pull_time_ms")) is int
            and precombat["pull_time_ms"] > 0
        }
        if len(pull_times) > 1:
            raise ValueError("search outcomes disagree on precombat pull_time_ms")
        pull_time_ms = next(iter(pull_times), None)
        schedule: list[JSONMap] = []
        for step in self.schedule:
            row = step.to_dict()
            if pull_time_ms is not None:
                row["simulator_time_ms"] = step.at_or_after_ms
                row["relative_to_pull_ms"] = (
                    step.at_or_after_ms - pull_time_ms
                )
            schedule.append(row)
        return {
            "schema": "wave_action_sequence_search/v1",
            "search_object": "FINITE_SCENARIO_ACTION_SCHEDULE",
            "candidate_is_cat_or_contra_residual": False,
            "cell": self.cell.to_dict(),
            "status": self.status,
            "completed_depth": self.completed_depth,
            "evaluated_schedule_count": self.evaluated_schedule_count,
            "guide_ids": list(self.guide_ids),
            "replay_workers": self.replay_workers,
            "guide_role": "PROPOSAL_ORDER_ONLY_NOT_RUNTIME_FALLBACK",
            "mean_dps": self.mean_dps,
            "search_ranking_score": self.search_ranking_score,
            "search_ranking_metric": self.search_ranking_metric,
            "ranking_continuation": {
                "max_steps": self.continuation_max_steps,
                "seed_replay_count": self.continuation_seed_replay_count,
                "scored_prefix_count": self.continuation_scored_prefix_count,
                "completed_prefix_count": self.continuation_completed_prefix_count,
                "same_action_schedule_shared_across_seeds": True,
                "runtime_fallback": False,
            },
            "pull_time_ms": pull_time_ms,
            "schedule": schedule,
            "outcomes": [outcome.compact_dict() for outcome in self.outcomes],
        }


@dataclass(frozen=True)
class _BeamNodeV1:
    schedule: tuple[ScheduledActionPlan, ...]
    outcomes: tuple[ScheduleReplayOutcomeV1, ...]
    score: float

    @property
    def complete(self) -> bool:
        return all(row.status is ReplayStatusV1.COMPLETE for row in self.outcomes)

    def key(self) -> tuple[str, ...]:
        return tuple(step.plan_key() for step in self.schedule)

    def sort_key(self) -> tuple[float, int, tuple[str, ...]]:
        return (-self.score, 0 if self.complete else 1, self.key())


def search_wave_action_sequences_v1(
    replay: ScheduleReplayV1,
    cell: SearchCellIdentity,
    *,
    seeds: Sequence[int],
    max_steps: int,
    beam_width: int,
    action_guides: Sequence[ActionGuideV1] = (),
    equipment_actions: Sequence[EquipmentAction] = (),
    max_off_gcd_actions: int = 1,
    max_prefix_permutations: int | None = None,
    wait_ms: int = 100,
    guard_options: Sequence[ObservableCausalGuardV1] = (),
    max_expansions_per_node: int | None = None,
    replay_workers: int = 1,
    score_outcomes: Callable[[Sequence[ScheduleReplayOutcomeV1]], float] | None = None,
    continuation_max_steps: int = 0,
    target_gate: RuntimeTargetGateV1 | None = None,
) -> WaveActionSequenceSearchResultV1:
    """Search one exact cell with one shared schedule across paired seeds.

    ``guard_options`` is deliberately empty by default.  Explicit options add
    guarded variants of complete native plans; they are not Cat residuals.
    Every seed receives the same schedule, while a ``SKIP_PLAN`` guard may
    causally execute or skip one shared step from that seed's current
    observation (for example, current pack HP on arrival).

    ``target_gate`` restricts direct SET_TARGET choices to the current
    contract stage.  Native replay also requires explicit engine retargeting.
    """

    if not isinstance(cell, SearchCellIdentity):
        raise TypeError("cell must be SearchCellIdentity")
    normalized_seeds = _seeds(seeds)
    if type(max_steps) is not int or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")
    if type(beam_width) is not int or beam_width <= 0:
        raise ValueError("beam_width must be a positive integer")
    if max_expansions_per_node is not None and (
        type(max_expansions_per_node) is not int or max_expansions_per_node <= 0
    ):
        raise ValueError("max_expansions_per_node must be positive or None")
    if type(replay_workers) is not int or replay_workers <= 0:
        raise ValueError("replay_workers must be a positive integer")
    if type(continuation_max_steps) is not int or continuation_max_steps < 0:
        raise ValueError("continuation_max_steps must be a nonnegative integer")
    if continuation_max_steps and score_outcomes is not None:
        raise ValueError(
            "continuation ranking and caller-supplied prefix scoring are exclusive"
        )
    if continuation_max_steps and not action_guides:
        raise ValueError("continuation ranking requires at least one action guide")
    if any(not isinstance(value, EquipmentAction) for value in equipment_actions):
        raise TypeError("equipment_actions must contain EquipmentAction values")
    if any(
        not isinstance(value, ObservableCausalGuardV1)
        for value in guard_options
    ):
        raise TypeError(
            "guard_options must contain ObservableCausalGuardV1 values"
        )
    replay_target_gate = getattr(replay, "target_gate", None)
    if target_gate is None:
        target_gate = replay_target_gate
    elif replay_target_gate is not None and replay_target_gate is not target_gate:
        raise ValueError("search and replay target_gate values differ")
    if target_gate is not None and not isinstance(
        target_gate, RuntimeTargetGateV1
    ):
        raise TypeError("target_gate must implement RuntimeTargetGateV1 or be None")
    normalized_guards = tuple(guard_options)
    guide_ids = tuple(_guide_ids(action_guides))
    # Frontier DPS is not a valid beam heuristic: a 100 ms WAIT after an early
    # white hit can beat a useful GCD solely through its tiny denominator.
    # Rank incomplete prefixes by accumulated own damage.  The reported winner
    # among completed schedules is still selected by terminal mean DPS below.
    root_outcomes = _apply_target_gate_v1(
        _replay_all(
            replay,
            normalized_seeds,
            (),
            replay_workers=replay_workers,
        ),
        target_gate,
    )
    if any(row.status is ReplayStatusV1.INVALID for row in root_outcomes):
        reasons = sorted({row.invalid_reason for row in root_outcomes})
        raise RuntimeError(f"root schedule is invalid: {reasons}")
    continuation = (
        _GuidedContinuationRankerV1(
            replay=replay,
            seeds=normalized_seeds,
            action_guides=action_guides,
            equipment_actions=equipment_actions,
            max_off_gcd_actions=max_off_gcd_actions,
            max_prefix_permutations=max_prefix_permutations,
            wait_ms=wait_ms,
            guard_options=normalized_guards,
            max_steps=continuation_max_steps,
            replay_workers=replay_workers,
            target_gate=target_gate,
        )
        if continuation_max_steps
        else None
    )
    scorer = score_outcomes or _mean_own_effective_damage
    if continuation is not None:
        ranking_metric = "BEST_GUIDED_FULL_WAVE_CONTINUATION_MEAN_DPS"
        root_score = continuation.score((), root_outcomes)
    else:
        ranking_metric = (
            "CALLER_SUPPLIED_PREFIX_SCORE"
            if score_outcomes is not None
            else "MEAN_OWN_EFFECTIVE_DAMAGE_AT_PREFIX"
        )
        root_score = _score(scorer, root_outcomes)
    beam = [_BeamNodeV1((), root_outcomes, root_score)]
    evaluated = 1
    completed_depth = 0

    for depth in range(1, max_steps + 1):
        candidates: dict[tuple[str, ...], _BeamNodeV1] = {}
        for node in beam:
            if node.complete:
                candidates[node.key()] = node
                continue
            expansions = _common_expansions(
                node,
                action_guides=action_guides,
                equipment_actions=equipment_actions,
                max_off_gcd_actions=max_off_gcd_actions,
                max_prefix_permutations=max_prefix_permutations,
                wait_ms=wait_ms,
                guard_options=normalized_guards,
            )
            if max_expansions_per_node is not None:
                expansions = _bounded_diverse_expansions_v1(
                    expansions,
                    max_expansions_per_node,
                )
            for plan in expansions:
                schedule = (*node.schedule, plan)
                outcomes = _apply_target_gate_v1(
                    _replay_all(
                        replay,
                        normalized_seeds,
                        schedule,
                        replay_workers=replay_workers,
                    ),
                    target_gate,
                    previous_outcomes=node.outcomes,
                )
                evaluated += 1
                if any(row.status is ReplayStatusV1.INVALID for row in outcomes):
                    continue
                if _all_live_seeds_skipped_conditional_prefix_v1(
                    node.outcomes,
                    outcomes,
                    plan=plan,
                    step_index=len(schedule) - 1,
                ):
                    continue
                candidate = _BeamNodeV1(
                    schedule=schedule,
                    outcomes=outcomes,
                    score=(
                        continuation.score(schedule, outcomes)
                        if continuation is not None
                        else _score(scorer, outcomes)
                    ),
                )
                prior = candidates.get(candidate.key())
                if prior is None or candidate.sort_key() < prior.sort_key():
                    candidates[candidate.key()] = candidate
        if not candidates:
            break
        beam = sorted(candidates.values(), key=_BeamNodeV1.sort_key)[:beam_width]
        completed_depth = depth
        if all(node.complete for node in beam):
            break

    completed = [node for node in beam if node.complete]
    if completed:
        best = min(
            completed,
            key=lambda node: (
                -_mean_dps(node.outcomes),
                node.key(),
            ),
        )
    else:
        best = min(beam, key=_BeamNodeV1.sort_key)
    status = (
        "COMPLETE_PAIRED_SEED_SCHEDULE"
        if best.complete
        else "INCOMPLETE_MAX_STEPS_OR_NO_COMMON_EXPANSION"
    )
    return WaveActionSequenceSearchResultV1(
        cell=cell,
        status=status,
        schedule=best.schedule,
        outcomes=best.outcomes,
        mean_dps=_mean_dps(best.outcomes) if best.complete else None,
        search_ranking_score=best.score,
        search_ranking_metric=ranking_metric,
        evaluated_schedule_count=evaluated,
        completed_depth=completed_depth,
        guide_ids=guide_ids,
        replay_workers=replay_workers,
        continuation_max_steps=continuation_max_steps,
        continuation_seed_replay_count=(
            continuation.seed_replay_count if continuation is not None else 0
        ),
        continuation_scored_prefix_count=(
            continuation.scored_prefix_count if continuation is not None else 0
        ),
        continuation_completed_prefix_count=(
            continuation.completed_prefix_count
            if continuation is not None
            else 0
        ),
    )


def _seeds(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("seeds must be a sequence")
    result = tuple(values)
    if not result or any(isinstance(value, bool) or not isinstance(value, int) for value in result):
        raise ValueError("seeds must contain integers")
    if len(set(result)) != len(result):
        raise ValueError("seeds must be unique")
    return result


def _guide_ids(guides: Sequence[ActionGuideV1]) -> list[str]:
    result = []
    for guide in guides:
        guide_id = getattr(guide, "guide_id", None)
        if not isinstance(guide_id, str) or not guide_id.strip():
            raise TypeError("every action guide requires a nonempty guide_id")
        if guide_id in result:
            raise ValueError(f"duplicate guide_id {guide_id!r}")
        result.append(guide_id)
    return result


def _replay_all(
    replay: ScheduleReplayV1,
    seeds: Sequence[int],
    schedule: Sequence[ScheduledActionPlan],
    *,
    replay_workers: int,
) -> tuple[ScheduleReplayOutcomeV1, ...]:
    if replay_workers == 1 or len(seeds) == 1:
        rows = tuple(replay.replay(seed, schedule) for seed in seeds)
    else:
        with ThreadPoolExecutor(
            max_workers=min(replay_workers, len(seeds)),
            thread_name_prefix="wave-replay",
        ) as executor:
            rows = tuple(
                executor.map(
                    lambda seed: replay.replay(seed, schedule),
                    seeds,
                )
            )
    if any(not isinstance(row, ScheduleReplayOutcomeV1) for row in rows):
        raise TypeError("replay must return ScheduleReplayOutcomeV1")
    if tuple(row.seed for row in rows) != tuple(seeds):
        raise ValueError("replay outcome seeds differ from requested seeds")
    return rows


def _apply_target_gate_v1(
    outcomes: Sequence[ScheduleReplayOutcomeV1],
    target_gate: RuntimeTargetGateV1 | None,
    *,
    previous_outcomes: Sequence[ScheduleReplayOutcomeV1] = (),
) -> tuple[ScheduleReplayOutcomeV1, ...]:
    rows = tuple(outcomes)
    if target_gate is None:
        return rows
    previous_by_seed = {row.seed: row for row in previous_outcomes}
    gated: list[ScheduleReplayOutcomeV1] = []
    for outcome in rows:
        if (
            outcome.status is ReplayStatusV1.INVALID
            or outcome.target_gate is not None
        ):
            gated.append(outcome)
            continue
        previous = previous_by_seed.get(outcome.seed)
        previous_stage_id = (
            previous.target_gate.stage_id
            if previous is not None and previous.target_gate is not None
            else None
        )
        gated.append(
            replace(
                outcome,
                target_gate=target_gate.evaluate_state(
                    outcome.state,
                    previous_stage_id=previous_stage_id,
                ),
            )
        )
    return tuple(gated)


def _bounded_diverse_expansions_v1(
    expansions: Sequence[ScheduledActionPlan],
    limit: int,
) -> tuple[ScheduledActionPlan, ...]:
    """Bound replay work without turning an expert guide into an allowlist.

    ``_common_expansions`` returns guide-ranked plans.  Taking a plain prefix
    of that order lets one high-weight action occupy the whole finite budget
    through target, guard, and prefix-order variants.  Select membership from
    semantic plan identity instead, first retaining one representative for
    every GCD action and then for every action advertised in any lane.  The
    remaining budget covers distinct structural families before canonical
    fill.  Guides still order the retained proposals, but cannot change which
    plans the finite cap admits.

    If ``limit`` is smaller than the number of distinct GCD actions, complete
    action coverage is mathematically impossible; the canonical action order
    makes that truncation deterministic and guide-independent.
    """

    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    ranked = tuple(expansions)
    if len(ranked) <= limit:
        return ranked

    canonical = tuple(sorted(ranked, key=ScheduledActionPlan.plan_key))
    selected: dict[str, ScheduledActionPlan] = {}

    def add(plan: ScheduledActionPlan) -> None:
        if len(selected) < limit:
            selected.setdefault(plan.plan_key(), plan)

    # A finite cap must not let many variants of one guide-favoured GCD remove
    # a different legal GCD from the evaluated search space.
    for action in sorted(
        {plan.gcd_action for plan in canonical if plan.gcd_action is not None}
    ):
        add(next(plan for plan in canonical if plan.gcd_action == action))

    # Cover exact ActionRef membership in off-GCD and next-swing lanes as well.
    # A selected bundle can satisfy more than one membership, so recompute the
    # covered set after each addition rather than reserving one slot per token.
    action_universe = sorted(
        {
            operation.action
            for plan in canonical
            for operation in plan.ordered_operations()
            if operation.action is not None
        }
    )
    for action in action_universe:
        covered = {
            operation.action
            for plan in selected.values()
            for operation in plan.ordered_operations()
            if operation.action is not None
        }
        if action in covered:
            continue
        add(
            next(
                plan
                for plan in canonical
                if any(
                    operation.action == action
                    for operation in plan.ordered_operations()
                )
            )
        )

    # Use spare slots for distinct searchable structures: waits/timing,
    # targets, equipment, queue state, guard, and cross-lane operation order.
    # Greedy set cover is deterministic because plan_key is the tie-breaker.
    structural_universe = {
        token
        for plan in canonical
        for token in _expansion_structure_tokens_v1(plan)
    }
    covered_structures = {
        token
        for plan in selected.values()
        for token in _expansion_structure_tokens_v1(plan)
    }
    while len(selected) < limit:
        uncovered = structural_universe - covered_structures
        if not uncovered:
            break
        candidates = [
            plan for plan in canonical if plan.plan_key() not in selected
        ]
        if not candidates:
            break
        best = min(
            candidates,
            key=lambda plan: (
                -len(_expansion_structure_tokens_v1(plan) & uncovered),
                plan.plan_key(),
            ),
        )
        newly_covered = _expansion_structure_tokens_v1(best) & uncovered
        if not newly_covered:
            break
        add(best)
        covered_structures.update(newly_covered)

    for plan in canonical:
        add(plan)
        if len(selected) == limit:
            break

    # Preserve the guide's proposal order among the guide-independent retained
    # membership.  This also keeps the uncapped call's ordering contract.
    return tuple(
        sorted(
            selected.values(),
            key=lambda plan: (-plan.guide_priority, plan.plan_key()),
        )
    )


def _expansion_structure_tokens_v1(
    plan: ScheduledActionPlan,
) -> frozenset[tuple[object, ...]]:
    terminal_kind = (
        "GCD"
        if plan.gcd_action is not None
        else "WAIT"
        if plan.wait_ms is not None
        else "CONDITIONAL_PREFIX"
    )
    guard_key = (
        "NONE"
        if plan.guard is None
        else repr(sorted(plan.guard.to_dict().items()))
    )
    equipment_key: tuple[object, ...] = (
        ("NONE",)
        if plan.equipment_action is None
        else (
            plan.equipment_action.slot,
            plan.equipment_action.item_id,
            plan.equipment_action.is_weapon_swap,
        )
    )
    queue_action = (
        None
        if plan.queue_action is None
        else (
            plan.queue_action.spell_id,
            plan.queue_action.item_id,
            plan.queue_action.other_id,
            plan.queue_action.tag,
        )
    )
    tokens = {
        ("terminal", terminal_kind),
        ("target", plan.target_index),
        ("equipment", *equipment_key),
        ("queue", plan.queue_op.value, queue_action),
        ("off_gcd_count", len(plan.off_gcd_actions)),
        ("guard", guard_key),
        (
            "prefix_order",
            *(kind.value for kind in plan.prefix_order or ()),
        ),
    }
    if plan.wait_ms is not None:
        tokens.add(("wait_ms", plan.wait_ms))
    return frozenset(tokens)


class _GuidedContinuationRankerV1:
    """Rank a prefix by the best complete shared-seed guide continuation.

    A guide continuation is a search heuristic only.  It is never appended to
    the returned candidate schedule.  At every continuation decision all seeds
    share one action plan selected from their common legal expansion, so this
    cannot turn a fixed simulator seed into seed-specific future knowledge.
    """

    def __init__(
        self,
        *,
        replay: ScheduleReplayV1,
        seeds: Sequence[int],
        action_guides: Sequence[ActionGuideV1],
        equipment_actions: Sequence[EquipmentAction],
        max_off_gcd_actions: int,
        max_prefix_permutations: int | None,
        wait_ms: int,
        guard_options: Sequence[ObservableCausalGuardV1],
        max_steps: int,
        replay_workers: int,
        target_gate: RuntimeTargetGateV1 | None,
    ) -> None:
        self._replay = replay
        self._seeds = tuple(seeds)
        self._guide_groups = tuple((guide,) for guide in action_guides)
        self._equipment_actions = tuple(equipment_actions)
        self._max_off_gcd_actions = max_off_gcd_actions
        self._max_prefix_permutations = max_prefix_permutations
        self._wait_ms = wait_ms
        self._guard_options = tuple(guard_options)
        self._max_steps = max_steps
        self._replay_workers = replay_workers
        self._target_gate = target_gate
        self._cache: dict[
            tuple[str, ...], tuple[ScheduleReplayOutcomeV1, ...]
        ] = {}
        self.seed_replay_count = 0
        self.scored_prefix_count = 0
        self.completed_prefix_count = 0

    def score(
        self,
        prefix: Sequence[ScheduledActionPlan],
        outcomes: Sequence[ScheduleReplayOutcomeV1],
    ) -> float:
        schedule = tuple(prefix)
        rows = tuple(outcomes)
        key = tuple(plan.plan_key() for plan in schedule)
        self._cache.setdefault(key, rows)
        self.scored_prefix_count += 1
        if all(row.status is ReplayStatusV1.COMPLETE for row in rows):
            self.completed_prefix_count += 1
            return _mean_dps(rows)

        completed_scores: list[float] = []
        for guide_group in self._guide_groups:
            continuation_schedule = schedule
            continuation_rows = rows
            for _ in range(self._max_steps):
                if all(
                    row.status is ReplayStatusV1.COMPLETE
                    for row in continuation_rows
                ):
                    break
                node = _BeamNodeV1(
                    continuation_schedule,
                    continuation_rows,
                    0.0,
                )
                expansions = _common_expansions(
                    node,
                    action_guides=guide_group,
                    equipment_actions=self._equipment_actions,
                    max_off_gcd_actions=self._max_off_gcd_actions,
                    max_prefix_permutations=self._max_prefix_permutations,
                    wait_ms=self._wait_ms,
                    guard_options=self._guard_options,
                )
                if not expansions:
                    break
                while expansions:
                    chosen = _completion_choice_v1(expansions, self._wait_ms)
                    proposed_schedule = (*continuation_schedule, chosen)
                    proposed_rows = self._cached_replay(
                        proposed_schedule,
                        previous_outcomes=continuation_rows,
                    )
                    if any(
                        row.status is ReplayStatusV1.INVALID
                        for row in proposed_rows
                    ):
                        break
                    if _all_live_seeds_skipped_conditional_prefix_v1(
                        continuation_rows,
                        proposed_rows,
                        plan=chosen,
                        step_index=len(proposed_schedule) - 1,
                    ):
                        chosen_key = chosen.plan_key()
                        expansions = tuple(
                            plan
                            for plan in expansions
                            if plan.plan_key() != chosen_key
                        )
                        continue
                    continuation_schedule = proposed_schedule
                    continuation_rows = proposed_rows
                    break
                else:
                    break
                if any(
                    row.status is ReplayStatusV1.INVALID
                    for row in proposed_rows
                ):
                    break
            if all(
                row.status is ReplayStatusV1.COMPLETE
                for row in continuation_rows
            ):
                completed_scores.append(_mean_dps(continuation_rows))

        if not completed_scores:
            raise RuntimeError(
                "no action guide completed the prefix within "
                f"continuation_max_steps={self._max_steps}"
            )
        self.completed_prefix_count += 1
        return max(completed_scores)

    def _cached_replay(
        self,
        schedule: Sequence[ScheduledActionPlan],
        *,
        previous_outcomes: Sequence[ScheduleReplayOutcomeV1],
    ) -> tuple[ScheduleReplayOutcomeV1, ...]:
        key = tuple(plan.plan_key() for plan in schedule)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rows = _apply_target_gate_v1(
            _replay_all(
                self._replay,
                self._seeds,
                schedule,
                replay_workers=self._replay_workers,
            ),
            self._target_gate,
            previous_outcomes=previous_outcomes,
        )
        self._cache[key] = rows
        self.seed_replay_count += len(self._seeds)
        return rows


def _completion_choice_v1(
    expansions: Sequence[ScheduledActionPlan], fallback_wait_ms: int
) -> ScheduledActionPlan:
    positive = [plan for plan in expansions if plan.guide_priority > 0]
    if positive:
        return min(positive, key=lambda plan: (-plan.guide_priority, plan.plan_key()))
    waits = [
        plan
        for plan in expansions
        if plan.wait_ms is not None
        and not plan.off_gcd_actions
        and plan.equipment_action is None
        and plan.queue_op is QueueLaneOp.KEEP
    ]
    meaningful = [plan for plan in waits if plan.wait_ms > fallback_wait_ms]
    if meaningful:
        return min(meaningful, key=lambda plan: (plan.wait_ms, plan.plan_key()))
    if waits:
        return min(waits, key=lambda plan: (plan.wait_ms, plan.plan_key()))
    return min(expansions, key=lambda plan: plan.plan_key())


def _score(
    scorer: Callable[[Sequence[ScheduleReplayOutcomeV1]], float],
    outcomes: Sequence[ScheduleReplayOutcomeV1],
) -> float:
    value = scorer(outcomes)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("score_outcomes must return a finite number")
    return float(value)


def _all_live_seeds_skipped_conditional_prefix_v1(
    prior_outcomes: Sequence[ScheduleReplayOutcomeV1],
    new_outcomes: Sequence[ScheduleReplayOutcomeV1],
    *,
    plan: ScheduledActionPlan,
    step_index: int,
) -> bool:
    """Reject a zero-time branch that changed no live seed's native state."""

    if not plan.conditional_prefix_only:
        return False
    by_seed = {outcome.seed: outcome for outcome in new_outcomes}
    live_seeds = tuple(
        outcome.seed
        for outcome in prior_outcomes
        if outcome.status is ReplayStatusV1.FRONTIER
    )
    if not live_seeds:
        return False
    for seed in live_seeds:
        outcome = by_seed.get(seed)
        if outcome is None:
            return False
        step_receipts = [
            receipt
            for receipt in outcome.receipts
            if receipt.get("step_index") == step_index
        ]
        if (
            not step_receipts
            or step_receipts[-1].get("kind")
            != "GUARD_FALSE_PLAN_SKIPPED"
        ):
            return False
    return True


def _mean_dps(outcomes: Sequence[ScheduleReplayOutcomeV1]) -> float:
    if not outcomes:
        raise ValueError("cannot score an empty outcome set")
    return mean(
        outcome.effective_damage * 1000.0 / max(1, outcome.elapsed_ms)
        for outcome in outcomes
    )


def _mean_own_effective_damage(
    outcomes: Sequence[ScheduleReplayOutcomeV1],
) -> float:
    if not outcomes:
        raise ValueError("cannot score an empty outcome set")
    return mean(outcome.effective_damage for outcome in outcomes)


def _common_expansions(
    node: _BeamNodeV1,
    *,
    action_guides: Sequence[ActionGuideV1],
    equipment_actions: Sequence[EquipmentAction],
    max_off_gcd_actions: int,
    max_prefix_permutations: int | None,
    wait_ms: int,
    guard_options: Sequence[ObservableCausalGuardV1],
) -> tuple[ScheduledActionPlan, ...]:
    live = [row for row in node.outcomes if row.status is ReplayStatusV1.FRONTIER]
    if not live:
        return ()
    gated = tuple(row.target_gate is not None for row in live)
    if any(gated) and not all(gated):
        raise ValueError("frontier outcomes mix gated and ungated target state")
    at_or_after_ms = max(row.elapsed_ms for row in live)
    additional_wait_ms = _observable_wait_options(live, wait_ms)
    per_seed: list[dict[str, ScheduledActionPlan]] = []
    accumulated_priorities: dict[ActionRef, list[float]] = {}
    for outcome in live:
        for guide in action_guides:
            values = guide.action_priorities(outcome, node.schedule)
            if not isinstance(values, Mapping):
                raise TypeError("guide action_priorities must return a mapping")
            for action, priority in values.items():
                if not isinstance(action, ActionRef):
                    raise TypeError("guide priority keys must be ActionRef")
                if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not math.isfinite(float(priority)):
                    raise ValueError("guide priorities must be finite numbers")
                accumulated_priorities.setdefault(action, []).append(float(priority))
        plans = enumerate_scheduled_action_plans(
            outcome.available_actions,
            _target_states(outcome.state),
            at_or_after_ms=at_or_after_ms,
            queue_active=_queue_active(outcome.state),
            equipment_actions=equipment_actions,
            max_off_gcd_actions=max_off_gcd_actions,
            max_prefix_permutations=max_prefix_permutations,
            fallback_wait_ms=wait_ms,
            additional_wait_ms=additional_wait_ms,
            include_wait=True,
            allow_gcd_without_target=_precombat_active(outcome.state),
            guard_options=guard_options,
            allowed_direct_target_indexes=(
                outcome.target_gate.direct_target_indexes
                if outcome.target_gate is not None
                else None
            ),
        )
        per_seed.append({plan.plan_key(): plan for plan in plans})
    all_plans: dict[str, ScheduledActionPlan] = {}
    for plans in per_seed:
        for key, plan in plans.items():
            all_plans.setdefault(key, plan)

    common = set(per_seed[0])
    for plans in per_seed[1:]:
        common.intersection_update(plans)
    # Once a prior guarded step executes on only some paired seeds, a long-CD
    # resource can be available in one frontier and unavailable in another.
    # A strict legal-action intersection would then make it impossible for the
    # shared schedule to say "use it on the next pack only where it was saved".
    # Admit the narrow safe case: one action, explicitly guarded by that same
    # action's current legality/readiness, with false=>skip semantics.  Runtime
    # still re-evaluates the guard independently from current observations; no
    # seed identity or future death timestamp enters the decision.
    common.update(
        key
        for key, plan in all_plans.items()
        if _is_conditionally_common_skip_plan_v1(plan, live)
    )
    priorities = {
        action: mean(values) for action, values in accumulated_priorities.items()
    }
    provenance = tuple(getattr(guide, "guide_id") for guide in action_guides)
    result = []
    for key in common:
        plan = all_plans[key]
        all_ordered_actions = [
            operation.action
            for operation in plan.ordered_operations()
            if operation.action is not None
        ]
        ordered_actions = [
            action for action in all_ordered_actions
            if float(priorities.get(action, 0.0)) > 0
        ]
        guide_priority = sum(
            priorities.get(action, 0.0) * (len(ordered_actions) - index)
            for index, action in enumerate(ordered_actions)
        ) - 1e-6 * (len(all_ordered_actions) - len(ordered_actions))
        result.append(replace(
            plan,
            guide_priority=guide_priority,
            guide_provenance=provenance,
        ))
    result.sort(key=lambda row: (-row.guide_priority, row.plan_key()))
    return tuple(result)


def _is_conditionally_common_skip_plan_v1(
    plan: ScheduledActionPlan,
    outcomes: Sequence[ScheduleReplayOutcomeV1],
) -> bool:
    """Whether one non-intersecting plan is safe to replay on every seed.

    This deliberately does not generalize to arbitrary conditional bundles.
    Every action except the guarded action would need its own explicit runtime
    branch, so a multi-action plan remains subject to the ordinary intersection.
    """

    guard = plan.guard
    if (
        guard is None
        or guard.false_semantics != SKIP_PLAN
        or guard.action_ready is None
        or not plan.conditional_prefix_only
        or plan.equipment_action is not None
    ):
        return False
    ordered = plan.ordered_operations()
    action_operations = [
        operation
        for operation in ordered
        if operation.action is not None
    ]
    if (
        len(action_operations) != 1
        or action_operations[0].action != guard.action_ready
    ):
        return False
    action_kind = action_operations[0].kind
    expected_triggers_gcd = action_kind is ScheduledOperationKind.ACT_GCD
    if action_kind not in {
        ScheduledOperationKind.ACT_GCD,
        ScheduledOperationKind.ACT_OFF_GCD,
        ScheduledOperationKind.QUEUE_SET,
    }:
        return False
    for outcome in outcomes:
        advertised = next(
            (
                row
                for row in outcome.available_actions
                if row.action == guard.action_ready
            ),
            None,
        )
        if (
            advertised is not None
            and advertised.triggers_gcd is not expected_triggers_gcd
        ):
            return False
    if plan.target_index is not None:
        if (
            guard.target_index != plan.target_index
            or guard.target_attackable_is is not True
        ):
            return False
        if any(
            outcome.target_gate is not None
            and plan.target_index not in outcome.target_gate.direct_target_indexes
            for outcome in outcomes
        ):
            return False
        set_target_position = next(
            (
                index
                for index, operation in enumerate(ordered)
                if operation.kind is ScheduledOperationKind.SET_TARGET
            ),
            None,
        )
        action_position = ordered.index(action_operations[0])
        if (
            set_target_position is None
            or set_target_position > action_position
        ):
            return False
    return True


def _observable_wait_options(
    outcomes: Sequence[ScheduleReplayOutcomeV1],
    fallback_wait_ms: int,
) -> tuple[int, ...]:
    """Derive timing choices only from policy-visible clocks.

    A single fixed 100 ms wait makes a three-second pre-pull window require 30
    search decisions and strongly biases the beam against late pre-pot or
    pre-burst timing.  Swing, GCD, cooldown and pull clocks are already visible
    to the eventual addon, so their boundaries are legitimate schedule choices
    rather than future information.
    """

    values: set[int] = set()
    for outcome in outcomes:
        state = outcome.state
        time_ms = outcome.elapsed_ms
        horizon = state.get("dynamic_idle_advance")
        horizon_ms = (
            horizon.get("horizon_ms") if isinstance(horizon, Mapping) else None
        )
        remaining_horizon = (
            horizon_ms - time_ms
            if type(horizon_ms) is int and horizon_ms > time_ms
            else None
        )

        visible_timers: list[object] = [
            state.get("gcd_remaining_ms"),
            state.get("mh_swing_remaining_ms"),
            state.get("oh_swing_remaining_ms"),
        ]
        visible_timers.extend(
            action.ready_in_ms
            for action in outcome.available_actions
            if not action.legal and action.ready_in_ms > 0
        )
        for raw in visible_timers:
            if type(raw) is not int or raw <= 0:
                continue
            if remaining_horizon is None or raw <= remaining_horizon:
                values.add(raw)

        precombat = state.get("precombat")
        relative = (
            precombat.get("relative_time_ms")
            if isinstance(precombat, Mapping)
            else None
        )
        if type(relative) is int and relative < 0:
            until_pull = -relative
            # GCD-aligned and just-before-pull timing are both reachable without
            # walking the beam through dozens of identical 100 ms decisions.
            values.add(until_pull)
            values.add(min(1500, until_pull))
            if until_pull > fallback_wait_ms:
                values.add(until_pull - fallback_wait_ms)

    values.discard(fallback_wait_ms)
    return tuple(sorted(value for value in values if value > 0))


def _target_states(state: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    semantics = state.get("dynamic_target_semantics")
    values = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if isinstance(values, list):
        return tuple(values)
    lifecycle = state.get("dynamic_team_background")
    values = lifecycle.get("targets") if isinstance(lifecycle, Mapping) else None
    if isinstance(values, list):
        return tuple({**row, "attackable": not bool(row.get("dead"))} for row in values if isinstance(row, Mapping))
    return ()


def _enforce_unmasked_collateral_action_v1(
    action: ActionRef,
    state: Mapping[str, Any],
    tracker: Any | None,
) -> JSONMap | None:
    """Fail closed when an unmasked multi-target action can hit an illegal target.

    The target contract distinguishes direct-target permission from collateral
    permission.  SET_TARGET enforcement alone is insufficient for Whirlwind,
    Cleave, and Sweeping Strikes because the simulator chooses their secondary
    hit set internally.  This check is deliberately narrow and can be removed
    once the bridge accepts an explicit hit mask and returns the realized mask.
    """

    if tracker is None:
        return None
    spell_id = action.spell_id
    if spell_id in FURY_IMMEDIATE_UNMASKED_COLLATERAL_SPELL_IDS_V1:
        mode = "CURRENT_ATTACKABLE_TARGETS_MUST_BE_ALLOWED"
        include = lambda row: not row["dead"] and row["attackable"]
    elif spell_id in FURY_DELAYED_UNMASKED_COLLATERAL_SPELL_IDS_V1:
        mode = "ALL_LIVING_TARGETS_MUST_BE_ALLOWED_UNTIL_NATIVE_HIT_MASK"
        include = lambda row: not row["dead"]
    else:
        return None

    decision = tracker.observe(state)
    raw_targets = _target_states(state)
    if not raw_targets:
        raise RuntimeError(
            "collateral-target action requires complete runtime target states"
        )

    normalized: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for row in raw_targets:
        target_index = row.get("target_index")
        dead = row.get("dead")
        attackable = row.get("attackable")
        if (
            isinstance(target_index, bool)
            or not isinstance(target_index, int)
            or target_index < 0
            or not isinstance(dead, bool)
            or not isinstance(attackable, bool)
        ):
            raise RuntimeError(
                "collateral-target action requires target_index/dead/attackable "
                "for every runtime target"
            )
        if target_index in seen_indexes:
            raise RuntimeError("runtime target indexes are not unique")
        seen_indexes.add(target_index)
        normalized.append(
            {
                "target_index": target_index,
                "dead": dead,
                "attackable": attackable,
            }
        )

    exposed = tuple(
        sorted(row["target_index"] for row in normalized if include(row))
    )
    allowed = set(decision.collateral_target_indexes)
    forbidden = tuple(index for index in exposed if index not in allowed)
    if forbidden:
        raise RuntimeError(
            "unmasked collateral action could hit target indexes outside the "
            f"current stage allowlist: {forbidden}"
        )
    return {
        "mode": mode,
        "stage_id": decision.stage_id,
        "allowed_target_indexes": sorted(allowed),
        "exposed_target_indexes": list(exposed),
    }


def _queue_active(state: Mapping[str, Any]) -> bool:
    queue = state.get("swing_queue")
    if isinstance(queue, Mapping):
        return queue.get("status") in {"PENDING", "ACTIVE"}
    auras = state.get("auras")
    if isinstance(auras, list):
        for row in auras:
            action = row.get("action") if isinstance(row, Mapping) else None
            if isinstance(action, Mapping):
                try:
                    if ActionRef.from_wire(action) in _FURY_SWING_QUEUE_REFS_V1:
                        return True
                except Exception:
                    continue
    return False


def _precombat_active(state: Mapping[str, Any]) -> bool:
    value = state.get("precombat")
    return isinstance(value, Mapping) and value.get("active") is True


class _WaveTargetGateTrackerV1:
    """Replay-local monotone stage cursor; never shared across seeds."""

    def __init__(self, target_gate: RuntimeTargetGateV1) -> None:
        self.target_gate = target_gate
        self.decision: WaveTargetGateDecisionV1 | None = None

    def observe(self, state: Mapping[str, Any]) -> WaveTargetGateDecisionV1:
        self.decision = self.target_gate.evaluate_state(
            state,
            previous_stage_id=(
                self.decision.stage_id if self.decision is not None else None
            ),
        )
        return self.decision


class NativeDynamicV3ScheduleReplayV1:
    """Fresh-load executor for a seed-indexed dynamic-v3 wave case."""

    def __init__(
        self,
        bridge_factory: Callable[[], Any],
        case_factory: Callable[[int], Any],
        *,
        result_bearing_action_refs: Sequence[ActionRef] = tuple(
            FURY_RESULT_BEARING_ACTION_REFS_V1
        ),
        target_gate: RuntimeTargetGateV1 | None = None,
    ) -> None:
        if any(
            not isinstance(action, ActionRef)
            for action in result_bearing_action_refs
        ):
            raise TypeError(
                "result_bearing_action_refs must contain ActionRef values"
            )
        if len(set(result_bearing_action_refs)) != len(
            tuple(result_bearing_action_refs)
        ):
            raise ValueError("result_bearing_action_refs must be unique")
        self._bridge_factory = bridge_factory
        self._case_factory = case_factory
        self._result_bearing_action_refs = frozenset(
            result_bearing_action_refs
        )
        if target_gate is not None and not isinstance(
            target_gate, RuntimeTargetGateV1
        ):
            raise TypeError(
                "target_gate must implement RuntimeTargetGateV1 or be None"
            )
        self.target_gate = target_gate

    def _load_case(self, bridge: Any, case: Any, seed: int) -> Any:
        precombat = getattr(case, "precombat", None)
        if precombat is None:
            return bridge.load_dynamic_v3(
                case.request, seed, case.dynamic_load.config
            )
        return bridge.load_dynamic_v3_precombat(
            case.request,
            seed,
            case.dynamic_load.config,
            precombat,
        )

    def replay(
        self, seed: int, schedule: Sequence[ScheduledActionPlan]
    ) -> ScheduleReplayOutcomeV1:
        receipts: list[JSONMap] = []
        try:
            case = self._case_factory(seed)
            tracker = (
                _WaveTargetGateTrackerV1(self.target_gate)
                if self.target_gate is not None
                else None
            )
            if self.target_gate is not None:
                self.target_gate.validate_case(case)
            with self._bridge_factory() as bridge:
                loaded = self._load_case(bridge, case, seed)
                state = dict(loaded.state)
                root_time_ms = _state_time(state)
                state = _advance_to_input(bridge, state)
                if tracker is not None:
                    tracker.observe(state)
                for step_index, plan in enumerate(schedule):
                    if bool(state.get("finished")):
                        break
                    state = _execute_plan(
                        bridge,
                        state,
                        plan,
                        root_time_ms=root_time_ms,
                        step_index=step_index,
                        receipts=receipts,
                        result_bearing_action_refs=(
                            self._result_bearing_action_refs
                        ),
                        target_gate_tracker=tracker,
                    )
                    state = _advance_to_input(bridge, state)
                    if tracker is not None:
                        tracker.observe(state)
                if bool(state.get("finished")):
                    status = ReplayStatusV1.COMPLETE
                    available: tuple[AvailableAction, ...] = ()
                else:
                    status = ReplayStatusV1.FRONTIER
                    available = tuple(bridge.actions())
                return ScheduleReplayOutcomeV1(
                    seed=seed,
                    status=status,
                    state=state,
                    available_actions=available,
                    receipts=tuple(receipts),
                    target_gate=(tracker.decision if tracker is not None else None),
                )
        except Exception as error:
            return ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.INVALID,
                state={"time_ms": 0, "damage_done": 0.0},
                receipts=tuple(receipts),
                invalid_reason=f"{type(error).__name__}: {error}",
            )


class NativeDynamicV4ScheduleReplayV1(NativeDynamicV3ScheduleReplayV1):
    """Fresh-load executor for a seed-indexed dynamic-v4 wave case."""

    def _load_case(self, bridge: Any, case: Any, seed: int) -> Any:
        precombat = getattr(case, "precombat", None)
        if precombat is None:
            return bridge.load_dynamic_v4(
                case.request, seed, case.dynamic_load.config
            )
        load_precombat = getattr(bridge, "load_dynamic_v4_precombat", None)
        if not callable(load_precombat):
            raise RuntimeError(
                "bridge does not support load_dynamic_v4_precombat"
            )
        return load_precombat(
            case.request,
            seed,
            case.dynamic_load.config,
            precombat,
        )


def _state_time(state: Mapping[str, Any]) -> int:
    value = state.get("time_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("bridge state lacks nonnegative time_ms")
    return value


def _advance_to_input(bridge: Any, state: Mapping[str, Any]) -> JSONMap:
    current = dict(state)
    advances = 0
    while not bool(current.get("finished")) and not bool(current.get("needs_input")):
        current = dict(bridge.advance())
        advances += 1
        if advances > 100_000:
            raise RuntimeError("advance loop exceeded 100000 transitions")
    return current


def _wait_until_plan_guard_v1(
    bridge: Any,
    state: Mapping[str, Any],
    guard: ObservableCausalGuardV1,
    *,
    step_index: int,
    receipts: list[JSONMap],
) -> JSONMap:
    """Wait across changing observable checkpoints until ``guard`` is true."""

    current = dict(state)
    started_at_ms = _state_time(current)
    deadline_ms = started_at_ms + guard.timeout_ms
    previous_checkpoint_ms: int | None = None
    same_time_checkpoint_count = 0

    while True:
        current_time_ms = _state_time(current)
        if bool(current.get("finished")):
            receipts.append(
                {
                    "step_index": step_index,
                    "kind": "GUARD_TERMINAL_BEFORE_SATISFIED",
                    "state_time_ms": current_time_ms,
                    "guard_wait_elapsed_ms": current_time_ms - started_at_ms,
                }
            )
            return current
        if current_time_ms > deadline_ms:
            receipts.append(
                {
                    "step_index": step_index,
                    "kind": "GUARD_TIMEOUT",
                    "state_time_ms": current_time_ms,
                    "deadline_ms": deadline_ms,
                }
            )
            raise GuardTimeoutError(
                f"plan guard exceeded timeout_ms={guard.timeout_ms}"
            )

        available = tuple(bridge.actions())
        evaluation = evaluate_observable_guard_v1(guard, current, available)
        if evaluation.satisfied:
            receipts.append(
                {
                    "step_index": step_index,
                    "kind": "GUARD_SATISFIED",
                    "state_time_ms": current_time_ms,
                    "guard_wait_elapsed_ms": current_time_ms - started_at_ms,
                    "evaluation": evaluation.to_dict(),
                }
            )
            return current
        if current_time_ms >= deadline_ms:
            receipts.append(
                {
                    "step_index": step_index,
                    "kind": "GUARD_TIMEOUT",
                    "state_time_ms": current_time_ms,
                    "deadline_ms": deadline_ms,
                    "evaluation": evaluation.to_dict(),
                }
            )
            raise GuardTimeoutError(
                f"plan guard remained false for timeout_ms={guard.timeout_ms}"
            )

        if current_time_ms == previous_checkpoint_ms:
            same_time_checkpoint_count += 1
        else:
            same_time_checkpoint_count = 0
        previous_checkpoint_ms = current_time_ms
        if same_time_checkpoint_count > 32:
            receipts.append(
                {
                    "step_index": step_index,
                    "kind": "GUARD_STALLED_OBSERVATION",
                    "state_time_ms": current_time_ms,
                    "evaluation": evaluation.to_dict(),
                }
            )
            raise RuntimeError(
                "guard wait did not leave one simulator timestamp after 32 "
                "observable checkpoints"
            )
        requested_wait_ms = min(
            next_observable_guard_check_ms_v1(
                guard, evaluation, available
            ),
            deadline_ms - current_time_ms,
        )
        waited = dict(bridge.wait(requested_wait_ms))
        receipts.append(
            {
                "step_index": step_index,
                "kind": "GUARD_WAIT",
                "state_time_ms": current_time_ms,
                "wait_ms": requested_wait_ms,
                "evaluation": evaluation.to_dict(),
            }
        )
        current = _advance_to_input(bridge, waited)


def _execute_plan(
    bridge: Any,
    state: Mapping[str, Any],
    plan: ScheduledActionPlan,
    *,
    root_time_ms: int,
    step_index: int,
    receipts: list[JSONMap],
    result_bearing_action_refs: frozenset[ActionRef],
    target_gate_tracker: _WaveTargetGateTrackerV1 | None = None,
) -> JSONMap:
    current = dict(state)
    elapsed = _state_time(current) - root_time_ms
    if elapsed < plan.at_or_after_ms:
        wait_for = plan.at_or_after_ms - elapsed
        current = dict(bridge.wait(wait_for))
        receipts.append({
            "step_index": step_index,
            "kind": "WAIT_UNTIL_SCHEDULED_TIME",
            "wait_ms": wait_for,
            "state_time_ms": _state_time(current),
        })
        current = _advance_to_input(bridge, current)
        if bool(current.get("finished")):
            return current

    if plan.guard is not None:
        if plan.guard.false_semantics == SKIP_PLAN:
            evaluation = evaluate_observable_guard_v1(
                plan.guard,
                current,
                tuple(bridge.actions()),
            )
            if not evaluation.satisfied:
                receipts.append(
                    {
                        "step_index": step_index,
                        "kind": "GUARD_FALSE_PLAN_SKIPPED",
                        "state_time_ms": _state_time(current),
                        "guard_wait_elapsed_ms": 0,
                        "evaluation": evaluation.to_dict(),
                        "accepted": False,
                        "consumes_decision": False,
                    }
                )
                return current
            receipts.append(
                {
                    "step_index": step_index,
                    "kind": "GUARD_SATISFIED",
                    "state_time_ms": _state_time(current),
                    "guard_wait_elapsed_ms": 0,
                    "evaluation": evaluation.to_dict(),
                }
            )
        else:
            current = _wait_until_plan_guard_v1(
                bridge,
                current,
                plan.guard,
                step_index=step_index,
                receipts=receipts,
            )
        if bool(current.get("finished")):
            return current

    ordered_operations = plan.ordered_operations()
    if (
        target_gate_tracker is not None
        and plan.target_index is not None
        and (
            not ordered_operations
            or ordered_operations[0].kind is not ScheduledOperationKind.SET_TARGET
        )
    ):
        raise RuntimeError(
            "target-gated plan must set its explicit target first"
        )

    consumed = False
    for operation_index, operation in enumerate(ordered_operations):
        if consumed:
            break
        kind = operation.kind
        before_time = _state_time(current)
        receipt: JSONMap = {
            "step_index": step_index,
            "operation_index": operation_index,
            "kind": kind.value,
            "state_time_before_ms": before_time,
        }
        if kind is ScheduledOperationKind.SET_TARGET:
            if target_gate_tracker is not None:
                decision = target_gate_tracker.observe(current)
                if operation.target_index not in decision.direct_target_indexes:
                    raise RuntimeError(
                        "scheduled target is not legal in the current target stage"
                    )
            result = bridge.set_target(operation.target_index)
            current = dict(result.state)
            if result.target_index != operation.target_index:
                raise RuntimeError("runtime set_target selected a different target")
            receipt.update({"accepted": True, "consumes_decision": False})
        elif kind is ScheduledOperationKind.EQUIP:
            method = getattr(bridge, "equip_item", None)
            if not callable(method):
                raise RuntimeError("bridge has no runtime equipment-change command")
            result = method(operation.equipment_action)
            current = dict(result.state)
            receipt.update({"accepted": bool(result.accepted), "consumes_decision": bool(result.consumes_decision)})
            if not result.accepted:
                raise RuntimeError("equipment action was rejected")
            consumed = bool(result.consumes_decision)
        elif kind in {
            ScheduledOperationKind.ACT_OFF_GCD,
            ScheduledOperationKind.QUEUE_SET,
            ScheduledOperationKind.ACT_GCD,
        }:
            collateral_gate = _enforce_unmasked_collateral_action_v1(
                operation.action,
                current,
                target_gate_tracker,
            )
            available = {row.action: row for row in bridge.actions()}
            row = available.get(operation.action)
            if row is None or not row.legal:
                raise RuntimeError(f"scheduled action is not legal: {operation.action}")
            attempt_id = (
                f"schedule-step-{step_index}:operation-{operation_index}"
                if row.result_bearing
                or operation.action in result_bearing_action_refs
                else None
            )
            result = bridge.act(operation.action, attempt_id=attempt_id)
            if not result.casted:
                raise RuntimeError(f"scheduled action was not cast: {operation.action}")
            current = dict(result.state)
            consumed = bool(result.consumes_decision)
            receipt.update({
                "action": operation.action.to_wire(),
                "advertised_triggers_gcd": row.triggers_gcd,
                "accepted": True,
                "consumes_decision": consumed,
                "attempt_id": attempt_id,
            })
            if collateral_gate is not None:
                receipt["collateral_gate"] = collateral_gate
            if kind is ScheduledOperationKind.ACT_GCD and not consumed:
                raise RuntimeError("terminal GCD action did not consume the decision")
        elif kind is ScheduledOperationKind.QUEUE_CANCEL:
            result = bridge.cancel_queue()
            if not result.canceled:
                raise RuntimeError("scheduled queue cancellation was rejected")
            current = dict(result.state)
            consumed = bool(result.consumes_decision)
            receipt.update({"accepted": True, "consumes_decision": consumed})
        elif kind is ScheduledOperationKind.QUEUE_KEEP:
            receipt.update({"accepted": True, "consumes_decision": False})
        elif kind is ScheduledOperationKind.WAIT:
            current = dict(bridge.wait(operation.wait_ms))
            consumed = True
            receipt.update({"accepted": True, "consumes_decision": True, "wait_ms": operation.wait_ms})
        else:
            raise RuntimeError(f"unsupported scheduled operation {kind}")
        receipt["state_time_after_ms"] = _state_time(current)
        receipts.append(receipt)
        # Exact legality is refreshed before every following cast above.  This
        # matters for stance, proc, queue-delay, and same-epoch item changes.
    return current


__all__ = (
    "ActionGuideV1",
    "FURY_DELAYED_UNMASKED_COLLATERAL_SPELL_IDS_V1",
    "FURY_IMMEDIATE_UNMASKED_COLLATERAL_SPELL_IDS_V1",
    "FURY_RESULT_BEARING_ACTION_REFS_V1",
    "NativeDynamicV3ScheduleReplayV1",
    "NativeDynamicV4ScheduleReplayV1",
    "ReplayStatusV1",
    "ScheduleReplayOutcomeV1",
    "ScheduleReplayV1",
    "WaveActionSequenceSearchResultV1",
    "search_wave_action_sequences_v1",
)
