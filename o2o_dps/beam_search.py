"""Small deterministic beam search over the interactive simulator bridge.

WoWSims does not expose a tested snapshot/restore contract here, so every
candidate branch is reconstructed from the original RaidSimRequest and fixed
seed, then its complete decision prefix is replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .sim_bridge import ActionRef, ActResult, AvailableAction, CancelQueueResult


JSONMap = dict[str, Any]

# Turtle ranks used by the current Fury simulator.  Queue classification
# additionally requires tag=1 and triggers_gcd=false.  Keep the lower Heroic
# Strike rank used by the original fixture, but include the live max-rank
# Heroic Strike and Cleave actions exposed by o2obridge.
DEFAULT_QUEUE_SPELL_IDS = frozenset({11567, 25286, 20569})


class SearchError(RuntimeError):
    """The simulator could not reproduce a branch advertised as legal."""


class ActionLane(str, Enum):
    QUEUE = "queue"
    GCD = "gcd"
    OFF_GCD = "off_gcd"


class BridgeLike(Protocol):
    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap: ...

    def advance(self) -> JSONMap: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(self, action: ActionRef) -> ActResult: ...

    def cancel_queue(self) -> CancelQueueResult: ...

    def wait(self, wait_ms: int) -> JSONMap: ...


@dataclass(frozen=True)
class PlannedCommand:
    """One flattened simulator command in a selected policy sequence."""

    kind: str
    action: ActionRef | None = None
    wait_ms: int | None = None

    def to_dict(self) -> JSONMap:
        if self.kind == "act" and self.action is not None:
            return {"command": "act", "action": self.action.to_wire()}
        if self.kind == "cancel_queue":
            return {"command": "cancel_queue"}
        if self.kind == "wait" and self.wait_ms is not None:
            return {"command": "wait", "wait_ms": self.wait_ms}
        raise ValueError("planned command is incomplete")


@dataclass(frozen=True)
class FactorizedDecision:
    """One bounded queue operation followed by a GCD or an explicit wait.

    The queue operation is either a known next-swing spell or cancellation of
    an already-active queue.  Off-GCD, stance, and target lanes are deliberately
    outside this bootstrap contract.
    """

    queue: ActionRef | None = None
    gcd: ActionRef | None = None
    wait_ms: int | None = None
    cancel_queue: bool = False

    def commands(self) -> tuple[PlannedCommand, ...]:
        if self.queue is not None and self.cancel_queue:
            raise ValueError("decision cannot queue and cancel in the same lane")
        if self.wait_ms is not None:
            if self.gcd is not None:
                raise ValueError("wait decision cannot also contain a GCD action")
            result: list[PlannedCommand] = []
            if self.cancel_queue:
                result.append(PlannedCommand(kind="cancel_queue"))
            elif self.queue is not None:
                result.append(PlannedCommand(kind="act", action=self.queue))
            result.append(PlannedCommand(kind="wait", wait_ms=self.wait_ms))
            return tuple(result)
        if self.gcd is None:
            raise ValueError("spell decision requires a GCD action")
        result: list[PlannedCommand] = []
        if self.cancel_queue:
            result.append(PlannedCommand(kind="cancel_queue"))
        elif self.queue is not None:
            result.append(PlannedCommand(kind="act", action=self.queue))
        result.append(PlannedCommand(kind="act", action=self.gcd))
        return tuple(result)

    def sort_key(self) -> tuple[int, ActionRef, ActionRef, int, int]:
        return (
            1 if self.wait_ms is not None else 0,
            self.queue or ActionRef(),
            self.gcd or ActionRef(),
            self.wait_ms or 0,
            1 if self.cancel_queue else 0,
        )


@dataclass(frozen=True)
class TeacherState:
    """State/action/next-state record for one decision on the best branch."""

    state: JSONMap
    decision: FactorizedDecision
    next_state: JSONMap

    def to_dict(self) -> JSONMap:
        return {
            "state": self.state,
            "decision": [command.to_dict() for command in self.decision.commands()],
            "next_state": self.next_state,
        }


@dataclass(frozen=True)
class SearchResult:
    best_action_sequence: tuple[PlannedCommand, ...]
    score: float
    frontier_state: JSONMap
    teacher_states: tuple[TeacherState, ...]

    def to_dict(self) -> JSONMap:
        return {
            "best_action_sequence": [
                command.to_dict() for command in self.best_action_sequence
            ],
            "score": self.score,
            "frontier_state": self.frontier_state,
            "teacher_states": [state.to_dict() for state in self.teacher_states],
        }


@dataclass(frozen=True)
class _BeamNode:
    prefix: tuple[FactorizedDecision, ...]
    score: float
    frontier_state: JSONMap
    teacher_states: tuple[TeacherState, ...]

    def sort_key(
        self,
    ) -> tuple[
        float,
        tuple[tuple[int, ActionRef, ActionRef, int, int], ...],
    ]:
        return (-self.score, tuple(decision.sort_key() for decision in self.prefix))


def classify_action(
    available: AvailableAction,
    *,
    queue_spell_ids: frozenset[int] = DEFAULT_QUEUE_SPELL_IDS,
) -> ActionLane:
    """Classify a spellbook entry without treating every non-GCD as a queue."""

    if available.triggers_gcd:
        return ActionLane.GCD
    action = available.action
    if action.tag == 1 and action.spell_id in queue_spell_ids:
        return ActionLane.QUEUE
    return ActionLane.OFF_GCD


def factorized_decisions(
    available_actions: Iterable[AvailableAction],
    *,
    spell_id_allowlist: frozenset[int] | None = None,
    queue_spell_ids: frozenset[int] = DEFAULT_QUEUE_SPELL_IDS,
    fallback_wait_ms: int = 100,
    queue_active: bool = False,
    include_wait_when_gcd_available: bool = False,
) -> tuple[FactorizedDecision, ...]:
    """Build bounded queue+GCD/wait decisions from the legal spellbook.

    Queue cancellation is emitted only when the caller explicitly confirms an
    active next-swing queue.  The legacy behavior remains the default: a wait is
    emitted only when no GCD is usable.
    """

    if fallback_wait_ms <= 0:
        raise ValueError("fallback_wait_ms must be positive")
    if not isinstance(queue_active, bool):
        raise TypeError("queue_active must be boolean")
    if not isinstance(include_wait_when_gcd_available, bool):
        raise TypeError("include_wait_when_gcd_available must be boolean")
    unique: dict[ActionRef, AvailableAction] = {}
    for available in available_actions:
        if not available.legal:
            continue
        action = available.action
        if spell_id_allowlist is not None and action.spell_id not in spell_id_allowlist:
            continue
        unique.setdefault(action, available)

    queues: list[ActionRef] = []
    gcds: list[ActionRef] = []
    for action, available in unique.items():
        lane = classify_action(available, queue_spell_ids=queue_spell_ids)
        if lane is ActionLane.QUEUE:
            queues.append(action)
        elif lane is ActionLane.GCD:
            gcds.append(action)
    queues.sort()
    gcds.sort()

    decisions: list[FactorizedDecision] = []
    if gcds:
        decisions.extend(FactorizedDecision(gcd=gcd) for gcd in gcds)
        decisions.extend(
            FactorizedDecision(queue=queue, gcd=gcd)
            for queue in queues
            for gcd in gcds
        )
        if queue_active:
            decisions.extend(
                FactorizedDecision(gcd=gcd, cancel_queue=True) for gcd in gcds
            )
        if include_wait_when_gcd_available:
            decisions.append(FactorizedDecision(wait_ms=fallback_wait_ms))
            decisions.extend(
                FactorizedDecision(queue=queue, wait_ms=fallback_wait_ms)
                for queue in queues
            )
            if queue_active:
                decisions.append(
                    FactorizedDecision(
                        wait_ms=fallback_wait_ms,
                        cancel_queue=True,
                    )
                )
    else:
        decisions.append(FactorizedDecision(wait_ms=fallback_wait_ms))
        decisions.extend(
            FactorizedDecision(queue=queue, wait_ms=fallback_wait_ms)
            for queue in queues
        )
        if queue_active:
            decisions.append(
                FactorizedDecision(
                    wait_ms=fallback_wait_ms,
                    cancel_queue=True,
                )
            )
    decisions.sort(key=FactorizedDecision.sort_key)
    return tuple(decisions)


def beam_search(
    bridge: BridgeLike,
    raid_sim_request: Mapping[str, Any],
    *,
    seed: int = 1,
    depth: int = 3,
    beam_width: int = 4,
    spell_id_allowlist: Iterable[int] | None = None,
    queue_spell_ids: Iterable[int] = DEFAULT_QUEUE_SPELL_IDS,
    fallback_wait_ms: int = 100,
    score_state: Callable[[Mapping[str, Any]], float] | None = None,
) -> SearchResult:
    """Search a short deterministic policy prefix.

    The default score is cumulative ``damage_done`` at the frontier.  This is
    a simulator integration objective only; it does not imply calibration or
    superiority to any expert policy.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if depth <= 0:
        raise ValueError("depth must be positive")
    if beam_width <= 0:
        raise ValueError("beam_width must be positive")
    if fallback_wait_ms <= 0:
        raise ValueError("fallback_wait_ms must be positive")

    try:
        canonical_request = json.loads(json.dumps(raid_sim_request))
    except (TypeError, ValueError) as error:
        raise TypeError("RaidSimRequest must be JSON serializable") from error
    if not isinstance(canonical_request, dict):
        raise TypeError("RaidSimRequest must be a JSON object")

    allowlist = (
        None
        if spell_id_allowlist is None
        else frozenset(_spell_ids(spell_id_allowlist, "spell_id_allowlist"))
    )
    queue_ids = frozenset(_spell_ids(queue_spell_ids, "queue_spell_ids"))
    scorer = score_state or _damage_score

    root_state, root_teachers = _replay(
        bridge, canonical_request, seed, ()
    )
    beam = [
        _BeamNode(
            prefix=(),
            score=_score(scorer, root_state),
            frontier_state=root_state,
            teacher_states=root_teachers,
        )
    ]

    for _ in range(depth):
        candidates: list[_BeamNode] = []
        for node in beam:
            if bool(node.frontier_state.get("finished")):
                candidates.append(node)
                continue

            # Reconstruct the parent too: the available action set must come
            # from this exact fixed-seed prefix, never another branch's state.
            parent_state, _ = _replay(
                bridge, canonical_request, seed, node.prefix
            )
            if bool(parent_state.get("finished")):
                candidates.append(node)
                continue
            decisions = factorized_decisions(
                bridge.actions(),
                spell_id_allowlist=allowlist,
                queue_spell_ids=queue_ids,
                fallback_wait_ms=fallback_wait_ms,
            )
            for decision in decisions:
                prefix = node.prefix + (decision,)
                frontier_state, teachers = _replay(
                    bridge, canonical_request, seed, prefix
                )
                candidates.append(
                    _BeamNode(
                        prefix=prefix,
                        score=_score(scorer, frontier_state),
                        frontier_state=frontier_state,
                        teacher_states=teachers,
                    )
                )

        if not candidates:
            raise SearchError("beam has no reachable simulator branches")
        candidates.sort(key=_BeamNode.sort_key)
        beam = candidates[:beam_width]
        if all(bool(node.frontier_state.get("finished")) for node in beam):
            break

    best = min(beam, key=_BeamNode.sort_key)
    flattened = tuple(
        command
        for decision in best.prefix
        for command in decision.commands()
    )
    return SearchResult(
        best_action_sequence=flattened,
        score=best.score,
        frontier_state=best.frontier_state,
        teacher_states=best.teacher_states,
    )


def _replay(
    bridge: BridgeLike,
    request: Mapping[str, Any],
    seed: int,
    prefix: Sequence[FactorizedDecision],
) -> tuple[JSONMap, tuple[TeacherState, ...]]:
    state = bridge.load(request, seed)
    teachers: list[TeacherState] = []
    for decision_number, decision in enumerate(prefix, start=1):
        if bool(state.get("finished")):
            raise SearchError(
                f"decision {decision_number} is after encounter completion"
            )
        before = dict(state)
        if decision.queue is not None and decision.cancel_queue:
            raise SearchError(
                f"decision {decision_number} queues and cancels simultaneously"
            )
        if decision.cancel_queue:
            cancel_result = bridge.cancel_queue()
            if not cancel_result.canceled:
                raise SearchError(
                    f"queue cancellation failed at decision {decision_number}"
                )
            if cancel_result.consumes_decision:
                raise SearchError(
                    f"queue cancellation consumed decision {decision_number}"
                )
            state = cancel_result.state
        elif decision.queue is not None:
            queue_result = bridge.act(decision.queue)
            if not queue_result.casted:
                raise SearchError(
                    f"queue action was not cast at decision {decision_number}: "
                    f"{decision.queue}"
                )
            if queue_result.consumes_decision:
                raise SearchError(
                    f"queue action consumed the decision at decision "
                    f"{decision_number}: {decision.queue}"
                )
            state = queue_result.state
        if decision.wait_ms is not None:
            state = bridge.wait(decision.wait_ms)
        else:
            if decision.gcd is None:
                raise SearchError(f"decision {decision_number} has no GCD action")
            gcd_result = bridge.act(decision.gcd)
            if not gcd_result.casted:
                raise SearchError(
                    f"GCD action was not cast at decision {decision_number}: "
                    f"{decision.gcd}"
                )
            if not gcd_result.consumes_decision:
                raise SearchError(
                    f"GCD action did not consume decision {decision_number}: "
                    f"{decision.gcd}"
                )
            state = gcd_result.state

        if not bool(state.get("finished")):
            state = bridge.advance()
        teachers.append(
            TeacherState(
                state=before,
                decision=decision,
                next_state=dict(state),
            )
        )
    return dict(state), tuple(teachers)


def _damage_score(state: Mapping[str, Any]) -> float:
    value = state.get("damage_done")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchError("simulator state is missing numeric damage_done")
    return float(value)


def _score(
    scorer: Callable[[Mapping[str, Any]], float], state: Mapping[str, Any]
) -> float:
    value = scorer(state)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SearchError("score_state must return a number")
    return float(value)


def _spell_ids(values: Iterable[int], label: str) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must contain positive integer spell IDs")
        result.append(value)
    return result


__all__ = (
    "ActionLane",
    "DEFAULT_QUEUE_SPELL_IDS",
    "FactorizedDecision",
    "PlannedCommand",
    "SearchError",
    "SearchResult",
    "TeacherState",
    "beam_search",
    "classify_action",
    "factorized_decisions",
)
