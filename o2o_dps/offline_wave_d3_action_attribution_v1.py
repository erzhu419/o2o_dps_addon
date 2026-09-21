"""Attribute accepted D3 operations to searched-program control sources.

Action identity is deliberately not used for attribution: the same spell may
be proposed by an explicit searched step and by tail fill.  The join is the
bridge ``decision_index`` recorded in the searched runtime's execution audit
and in the accepted execution trace.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from .offline_wave_execution_trace_v1 import SCHEMA as EXECUTION_TRACE_SCHEMA
from .offline_wave_searched_program_v1 import (
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
)


JSONMap = dict[str, Any]
SCHEMA = "offline_wave_d3_action_attribution/v1"

EXPLICIT_ACTION = "EXPLICIT_ACTION"
TAIL_FILL = "TAIL_FILL"
EXPLICIT_WAIT = "EXPLICIT_WAIT"
CONTROLLER_WAIT = "CONTROLLER_WAIT"
OTHER = "OTHER"

_SOURCES = (
    EXPLICIT_ACTION,
    TAIL_FILL,
    EXPLICIT_WAIT,
    CONTROLLER_WAIT,
    OTHER,
)
_SOURCE_BY_PROPOSAL_KIND = {
    "SEARCHED_PROGRAM_STEP": EXPLICIT_ACTION,
    "SEARCHED_TAIL_FILL": TAIL_FILL,
    "EXPLICIT_WAIT_STEP": EXPLICIT_WAIT,
    "CONTROLLER_WAIT": CONTROLLER_WAIT,
}
_OUTCOME_KINDS = frozenset(
    {
        "SEARCHED_PROPOSAL_EXECUTION_CONFIRMED",
        "SEARCHED_PROPOSAL_ACTION_NOT_EXECUTED",
        "SEARCHED_PROPOSAL_REPLACED_BEFORE_EXECUTION",
    }
)
_SKIP_KINDS = frozenset(
    {
        "SEARCHED_STEP_TARGET_NO_LONGER_ACTIVE",
        "SEARCHED_STEP_GUARD_SKIPPED",
        "SEARCHED_STEP_MASKED_BY_EXACT_NATIVE_SURFACE",
        "SEARCHED_STEP_NOT_READY_AT_DEADLINE",
    }
)


class OfflineWaveD3ActionAttributionV1Error(ValueError):
    """Runtime audit and accepted execution trace cannot be joined exactly."""


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWaveD3ActionAttributionV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


def _step_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineWaveD3ActionAttributionV1Error(
            f"{label} must be a nonempty step id"
        )
    return value.strip()


def _action_signature(row: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, Any], ...]]:
    kind = row.get("kind")
    action = row.get("action")
    if not isinstance(kind, str) or not isinstance(action, Mapping):
        raise OfflineWaveD3ActionAttributionV1Error(
            "accepted action lacks kind or exact action identity"
        )
    return kind, tuple(sorted(dict(action).items()))


def _runtime_outcomes(
    audit_events: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, JSONMap], Counter[str], Counter[str], dict[str, str]]:
    outcomes: dict[int, JSONMap] = {}
    selected_actions: Counter[str] = Counter()
    selected_waits: Counter[str] = Counter()
    terminal_step_status: dict[str, str] = {}

    for event_index, raw in enumerate(audit_events):
        if not isinstance(raw, Mapping):
            raise OfflineWaveD3ActionAttributionV1Error(
                f"audit_events[{event_index}] must be an object"
            )
        kind = raw.get("kind")
        if kind == "SEARCHED_STEP_SELECTED":
            selected_actions[
                _step_id(raw.get("step_id"), f"audit_events[{event_index}].step_id")
            ] += 1
        elif kind == "SEARCHED_EXPLICIT_WAIT_SELECTED":
            selected_waits[
                _step_id(raw.get("step_id"), f"audit_events[{event_index}].step_id")
            ] += 1
        elif kind == "SEARCHED_STEP_WINDOW_MISSED":
            step_id = _step_id(
                raw.get("step_id"), f"audit_events[{event_index}].step_id"
            )
            if step_id in terminal_step_status:
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"step {step_id!r} has multiple terminal outcomes"
                )
            terminal_step_status[step_id] = "MISSED_WINDOW"
        elif kind in _SKIP_KINDS:
            step_id = _step_id(
                raw.get("step_id"), f"audit_events[{event_index}].step_id"
            )
            if step_id in terminal_step_status:
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"step {step_id!r} has multiple terminal outcomes"
                )
            terminal_step_status[step_id] = str(kind)

        if kind not in _OUTCOME_KINDS:
            continue
        decision_index = raw.get("execution_decision_index")
        if decision_index is None:
            # Direct unit-level calls may omit bridge receipts.  Such an event
            # is valid runtime evidence but cannot attribute an accepted trace
            # operation, so it is intentionally not joined.
            continue
        decision_index = _nonnegative_int(
            decision_index,
            f"audit_events[{event_index}].execution_decision_index",
        )
        proposal_index = _nonnegative_int(
            raw.get("proposal_index"),
            f"audit_events[{event_index}].proposal_index",
        )
        if proposal_index != decision_index:
            raise OfflineWaveD3ActionAttributionV1Error(
                f"decision {decision_index} differs from runtime proposal ordinal"
            )
        if decision_index in outcomes:
            raise OfflineWaveD3ActionAttributionV1Error(
                f"decision {decision_index} has multiple runtime outcomes"
            )
        outcomes[decision_index] = dict(raw)

    return outcomes, selected_actions, selected_waits, terminal_step_status


def attribute_d3_searched_replay_v1(
    searched_program: SearchedWaveProgramV1,
    runtime_audit_events: Sequence[Mapping[str, Any]],
    accepted_execution_trace: Mapping[str, Any],
) -> JSONMap:
    """Return compact per-replay attribution suitable for a D3 result row."""

    if not isinstance(searched_program, SearchedWaveProgramV1):
        raise TypeError("searched_program must be SearchedWaveProgramV1")
    if not isinstance(runtime_audit_events, Sequence) or isinstance(
        runtime_audit_events, (str, bytes, bytearray)
    ):
        raise TypeError("runtime_audit_events must be a sequence")
    if not isinstance(accepted_execution_trace, Mapping):
        raise TypeError("accepted_execution_trace must be a mapping")
    if accepted_execution_trace.get("schema") != EXECUTION_TRACE_SCHEMA:
        raise OfflineWaveD3ActionAttributionV1Error(
            "accepted execution trace schema differs"
        )
    blocks = accepted_execution_trace.get("blocks")
    if not isinstance(blocks, list):
        raise OfflineWaveD3ActionAttributionV1Error(
            "accepted execution trace blocks must be a list"
        )

    outcomes, selected_actions, selected_waits, terminal_status = _runtime_outcomes(
        runtime_audit_events
    )
    step_by_id = {step.step_id: step for step in searched_program.steps}
    unknown_steps = (
        set(selected_actions) | set(selected_waits) | set(terminal_status)
    ) - set(step_by_id)
    if unknown_steps:
        raise OfflineWaveD3ActionAttributionV1Error(
            f"runtime audit references unknown steps {sorted(unknown_steps)}"
        )

    action_counts = Counter({source: 0 for source in _SOURCES})
    wait_counts = Counter({source: 0 for source in _SOURCES})
    confirmed_step_ids: set[str] = set()
    seen_decisions: set[int] = set()
    joined_decision_count = 0

    for block_index, raw_block in enumerate(blocks):
        if not isinstance(raw_block, Mapping):
            raise OfflineWaveD3ActionAttributionV1Error(
                f"trace blocks[{block_index}] must be an object"
            )
        decision_index = _nonnegative_int(
            raw_block.get("decision_index"),
            f"trace blocks[{block_index}].decision_index",
        )
        if decision_index in seen_decisions:
            raise OfflineWaveD3ActionAttributionV1Error(
                f"trace repeats decision {decision_index}"
            )
        seen_decisions.add(decision_index)
        actions = raw_block.get("guide_actions")
        if not isinstance(actions, list):
            raise OfflineWaveD3ActionAttributionV1Error(
                f"trace blocks[{block_index}].guide_actions must be a list"
            )
        timing = raw_block.get("accepted_timing")
        if timing is not None and not isinstance(timing, Mapping):
            raise OfflineWaveD3ActionAttributionV1Error(
                f"trace blocks[{block_index}].accepted_timing is malformed"
            )
        if not actions and timing is None:
            continue

        outcome = outcomes.get(decision_index)
        if outcome is None:
            raise OfflineWaveD3ActionAttributionV1Error(
                f"accepted decision {decision_index} lacks joined runtime outcome"
            )
        joined_decision_count += 1
        confirmed = outcome.get("kind") == "SEARCHED_PROPOSAL_EXECUTION_CONFIRMED"
        proposal_kind = outcome.get("proposal_kind")
        source = (
            _SOURCE_BY_PROPOSAL_KIND.get(str(proposal_kind), OTHER)
            if confirmed
            else OTHER
        )

        runtime_actions = outcome.get("accepted_actions", [])
        if not isinstance(runtime_actions, list):
            raise OfflineWaveD3ActionAttributionV1Error(
                f"decision {decision_index} runtime accepted_actions is malformed"
            )
        if [_action_signature(row) for row in actions] != [
            _action_signature(row) for row in runtime_actions
        ]:
            raise OfflineWaveD3ActionAttributionV1Error(
                f"decision {decision_index} accepted actions differ across evidence"
            )

        action_counts[source] += len(actions)
        if timing is not None:
            wait_counts[source] += 1

        if confirmed and source in {EXPLICIT_ACTION, EXPLICIT_WAIT}:
            step_id = _step_id(
                outcome.get("step_id"), f"decision {decision_index}.step_id"
            )
            step = step_by_id.get(step_id)
            if step is None:
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"decision {decision_index} confirms unknown step {step_id!r}"
                )
            expected_kind = (
                SearchedWaveStepKindV1.ACTION
                if source == EXPLICIT_ACTION
                else SearchedWaveStepKindV1.WAIT
            )
            if step.kind is not expected_kind:
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"decision {decision_index} proposal kind disagrees with step"
                )
            # Queue and off-GCD action decisions legitimately carry a short
            # terminal wait after the accepted action.  The action identity,
            # not the absence of timing, is what confirms the ACTION step.
            if source == EXPLICIT_ACTION and len(actions) != 1:
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"confirmed ACTION step {step_id!r} lacks one accepted action"
                )
            if source == EXPLICIT_WAIT and (actions or timing is None):
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"confirmed WAIT step {step_id!r} lacks one accepted wait"
                )
            if step_id in confirmed_step_ids:
                raise OfflineWaveD3ActionAttributionV1Error(
                    f"explicit step {step_id!r} was confirmed more than once"
                )
            confirmed_step_ids.add(step_id)

    trace_action_count = accepted_execution_trace.get("accepted_action_count")
    if _nonnegative_int(trace_action_count, "trace accepted_action_count") != sum(
        action_counts.values()
    ):
        raise OfflineWaveD3ActionAttributionV1Error(
            "trace accepted_action_count differs from attributed action count"
        )
    overlap = confirmed_step_ids & set(terminal_status)
    if overlap:
        raise OfflineWaveD3ActionAttributionV1Error(
            f"confirmed steps also have missed/skipped outcomes {sorted(overlap)}"
        )

    action_step_ids = tuple(
        step.step_id
        for step in searched_program.steps
        if step.kind is SearchedWaveStepKindV1.ACTION
    )
    wait_step_ids = tuple(
        step.step_id
        for step in searched_program.steps
        if step.kind is SearchedWaveStepKindV1.WAIT
    )
    confirmed_action_ids = tuple(
        step_id for step_id in action_step_ids if step_id in confirmed_step_ids
    )
    confirmed_wait_ids = tuple(
        step_id for step_id in wait_step_ids if step_id in confirmed_step_ids
    )
    missed_ids = tuple(
        step.step_id
        for step in searched_program.steps
        if terminal_status.get(step.step_id) == "MISSED_WINDOW"
    )
    skipped_ids = tuple(
        step.step_id
        for step in searched_program.steps
        if step.step_id in terminal_status
        and terminal_status[step.step_id] != "MISSED_WINDOW"
    )
    unresolved_ids = tuple(
        step.step_id
        for step in searched_program.steps
        if step.step_id not in confirmed_step_ids
        and step.step_id not in terminal_status
    )
    selected_but_unaccepted_ids = tuple(
        step_id
        for step_id in unresolved_ids
        if selected_actions[step_id] + selected_waits[step_id] > 0
    )
    never_selected_ids = tuple(
        step_id
        for step_id in unresolved_ids
        if selected_actions[step_id] + selected_waits[step_id] == 0
    )
    action_step_count = len(action_step_ids)
    selected_action_proposal_count = sum(selected_actions.values())

    return {
        "schema": SCHEMA,
        "join_key": "execution_decision_index=decision_index",
        "joined_decision_count": joined_decision_count,
        "accepted_action_count": sum(action_counts.values()),
        "accepted_action_count_by_source": dict(action_counts),
        "accepted_wait_count": sum(wait_counts.values()),
        "accepted_wait_count_by_source": dict(wait_counts),
        "explicit_action_step_count": action_step_count,
        "accepted_explicit_action_step_count": len(confirmed_action_ids),
        "accepted_explicit_action_step_ids": list(confirmed_action_ids),
        "explicit_wait_step_count": len(wait_step_ids),
        "accepted_explicit_wait_step_count": len(confirmed_wait_ids),
        "accepted_explicit_wait_step_ids": list(confirmed_wait_ids),
        "missed_explicit_step_ids": list(missed_ids),
        "skipped_explicit_step_ids": list(skipped_ids),
        "selected_but_unaccepted_explicit_step_ids": list(
            selected_but_unaccepted_ids
        ),
        "never_selected_explicit_step_ids": list(never_selected_ids),
        "planned_action_acceptance_rate": (
            len(confirmed_action_ids) / action_step_count
            if action_step_count
            else None
        ),
        "selected_action_proposal_count": selected_action_proposal_count,
        "confirmed_action_proposal_count": len(confirmed_action_ids),
        "selected_action_proposal_acceptance_rate": (
            len(confirmed_action_ids) / selected_action_proposal_count
            if selected_action_proposal_count
            else None
        ),
        "contract": {
            "action_names_used_for_attribution": False,
            "accepted_operations_come_only_from_execution_trace": True,
            "control_source_comes_only_from_joined_runtime_audit": True,
            "nonconfirmed_proposal_operations_are_other": True,
        },
    }


__all__ = (
    "CONTROLLER_WAIT",
    "EXPLICIT_ACTION",
    "EXPLICIT_WAIT",
    "OTHER",
    "OfflineWaveD3ActionAttributionV1Error",
    "SCHEMA",
    "TAIL_FILL",
    "attribute_d3_searched_replay_v1",
)
