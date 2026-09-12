"""Additive Cat source-reentry clock over the frozen v5 sink executor.

Cat's Lua source is normally re-entered by a repeatedly pressed macro.  The
native simulator instead exposes a decision epoch and requires that epoch to
be consumed before it can advance.  When every reached source sink has a
typed, non-consuming disposition and at least one submitted GCD was rejected,
this wrapper schedules a fixed 100 ms simulator wake-up.  The wake-up is a
runner timing proxy: it is neither a Cat policy action nor evidence of exact
WoW-client input cadence.

The v5 executor and its source-order ledger remain byte-identical.  This
module only appends a typed clock receipt after a complete v5 invocation.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from . import cat_fury_ordered_sink_executor_v5 as _v5
from . import cat_fury_full_policy_rollout_v5 as _cat_rollout_v5
from . import fury_full_policy_rollout_v2 as _v2
from .expert_policy import ExpertDecision, WAIT_ACTION


JSONMap = dict[str, Any]
EXECUTION_SCHEMA_V6 = "cat_fury_ordered_sink_simulator_execution/v6"
IMPLEMENTATION_REVISION = "v6.0_cat_fixed_100ms_source_reentry"
SOURCE_REENTRY_CLOCK_SCHEMA_V6 = "cat_fury_source_reentry_clock/v6"
SOURCE_REENTRY_RETRY_MS_V6 = 100
SOURCE_REENTRY_TIMING_AUTHORITY_V6 = "RUNNER_FIXED_100MS_PROXY"
SOURCE_REENTRY_TRIGGER_V6 = (
    "SOURCE_GCD_TYPED_REJECTED_ALL_REACHED_SINKS_NONCONSUMING"
)


class CatFuryOrderedSinkExecutorV6Error(RuntimeError):
    """The additive Cat source-reentry contract was violated."""


def _all_reached_sinks_typed_nonconsuming_with_rejected_gcd_v6(
    events: Any,
) -> bool:
    """Return whether a source invocation needs the external reentry clock.

    Accepted CVar/control/queue sinks are allowed because those lanes do not
    consume a GCD decision.  At least one *submitted* source GCD must have a
    typed simulator rejection; this prevents turning an empty invocation or a
    control-only invocation into an invented polling loop.
    """

    if not isinstance(events, list) or not events:
        return False
    rejected_gcd = False
    for event in events:
        if not isinstance(event, Mapping):
            return False
        source = event.get("source_sink")
        attempt = event.get("source_attempt")
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        consumption = event.get("decision_consumption")
        if (
            not isinstance(source, Mapping)
            or not isinstance(attempt, Mapping)
            or attempt.get("status") != "ATTEMPTED"
            or not isinstance(submission, Mapping)
            or submission.get("status")
            not in {"SUBMITTED", "NOT_SUBMITTED_STATE_ALREADY_SATISFIED"}
            or not isinstance(acceptance, Mapping)
            or acceptance.get("status")
            not in {"ACCEPTED", "REJECTED", "NOOP_STATE_ALREADY_SATISFIED"}
            or not isinstance(consumption, Mapping)
            or consumption.get("consumes_decision") is not False
        ):
            return False
        if (
            source.get("channel") == "gcd"
            and submission.get("status") == "SUBMITTED"
            and acceptance.get("status") == "REJECTED"
        ):
            rejected_gcd = True
    return rejected_gcd


def _expected_reentry_v6(
    execution: Mapping[str, Any], decision: ExpertDecision
) -> bool:
    return (
        decision.gcd != WAIT_ACTION
        and execution.get("execution_blocked") is False
        and execution.get("decision_consumed") is False
        and execution.get("source_to_simulator_order_faithful") is True
        and execution.get("nonfaithful_reasons") == []
        and _all_reached_sinks_typed_nonconsuming_with_rejected_gcd_v6(
            execution.get("sink_events")
        )
    )


def execute_cat_fury_ordered_sinks_v6(
    bridge: _v5.CatOrderedSinkBridgeLikeV5,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> JSONMap:
    """Run frozen v5 source sinks, then schedule a typed reentry if required."""

    execution = _v5.execute_cat_fury_ordered_sinks_v5(
        bridge,
        decision,
        state,
        attempt_id_prefix=attempt_id_prefix,
        result_bearing_action_keys=result_bearing_action_keys,
    )
    execution["schema"] = EXECUTION_SCHEMA_V6
    execution["implementation_revision"] = IMPLEMENTATION_REVISION
    execution["source_reentry_clock"] = None
    execution["decision_consumption_scope"] = "SOURCE_SINKS_ONLY"
    execution["simulator_epoch_consumed_by_source_reentry_clock"] = False

    if _expected_reentry_v6(execution, decision):
        current = execution["final_state"]
        if (
            not isinstance(current, Mapping)
            or current.get("finished") is True
            or current.get("needs_input") is not True
        ):
            raise CatFuryOrderedSinkExecutorV6Error(
                "typed rejected Cat GCD must leave one open simulator decision"
            )
        scheduled_at = current.get("time_ms")
        if type(scheduled_at) is not int:
            raise CatFuryOrderedSinkExecutorV6Error(
                "source reentry clock requires integer simulator time_ms"
            )
        after = bridge.wait(SOURCE_REENTRY_RETRY_MS_V6)
        if not isinstance(after, Mapping):
            raise CatFuryOrderedSinkExecutorV6Error(
                "source reentry bridge.wait returned non-mapping"
            )
        after = dict(after)
        if (
            after.get("time_ms") != scheduled_at
            or after.get("needs_input") is not False
        ):
            raise CatFuryOrderedSinkExecutorV6Error(
                "source reentry bridge.wait must schedule without advancing time"
            )
        nominal_wake = scheduled_at + SOURCE_REENTRY_RETRY_MS_V6
        execution["source_reentry_clock"] = {
            "schema": SOURCE_REENTRY_CLOCK_SCHEMA_V6,
            "trigger": SOURCE_REENTRY_TRIGGER_V6,
            "timing_authority": SOURCE_REENTRY_TIMING_AUTHORITY_V6,
            "exact_client_cadence": False,
            "policy_action": False,
            "source_sink": False,
            "source_sink_decision_consumed": False,
            "simulator_decision_consumed": True,
            "requested_ms": SOURCE_REENTRY_RETRY_MS_V6,
            "scheduled_at_time_ms": scheduled_at,
            "nominal_wake_time_ms": nominal_wake,
            "actual_next_epoch_time_ms": None,
        }
        execution["final_state"] = after
        execution["simulator_epoch_consumed_by_source_reentry_clock"] = True
    return execution


def _audit_ordered_execution_v6(
    execution: Mapping[str, Any],
    proposal: ExpertDecision,
    *,
    decision_index: int,
) -> list[JSONMap]:
    """Reuse the frozen v5 ledger audit and validate the additive clock."""

    projected = copy.deepcopy(dict(execution))
    projected["schema"] = _v5.EXECUTION_SCHEMA_V5
    projected["implementation_revision"] = _v5.IMPLEMENTATION_REVISION
    projected.pop("source_reentry_clock", None)
    blockers = _cat_rollout_v5._audit_ordered_execution_v5(
        projected,
        proposal,
        decision_index=decision_index,
    )

    def add(code: str, message: str) -> None:
        blockers.append(
            _v2._blocker(
                code,
                message,
                execution_fatal=True,
                decision_index=decision_index,
            )
        )

    if execution.get("schema") != EXECUTION_SCHEMA_V6:
        add("CAT_V6_EXECUTION_SCHEMA_MISMATCH", "ordered executor schema differs")
    if execution.get("decision_consumption_scope") != "SOURCE_SINKS_ONLY":
        add(
            "CAT_V6_DECISION_CONSUMPTION_SCOPE_MISMATCH",
            "legacy decision_consumed must remain explicitly scoped to source sinks",
        )
    expected = _expected_reentry_v6(execution, proposal)
    reentry = execution.get("source_reentry_clock")
    if execution.get("simulator_epoch_consumed_by_source_reentry_clock") is not expected:
        add(
            "CAT_V6_SIMULATOR_EPOCH_CONSUMPTION_MISMATCH",
            "source reentry clock consumption flag differs from its ledger",
        )
    if expected != isinstance(reentry, Mapping):
        add(
            "CAT_V6_SOURCE_REENTRY_CLOCK_MISMATCH",
            "typed rejected Cat GCD did not produce exactly one source reentry clock",
        )
    elif isinstance(reentry, Mapping):
        final_state = execution.get("final_state")
        scheduled_at = reentry.get("scheduled_at_time_ms")
        nominal_wake = (
            scheduled_at + SOURCE_REENTRY_RETRY_MS_V6
            if type(scheduled_at) is int
            else None
        )
        if (
            reentry.get("schema") != SOURCE_REENTRY_CLOCK_SCHEMA_V6
            or reentry.get("trigger") != SOURCE_REENTRY_TRIGGER_V6
            or reentry.get("timing_authority")
            != SOURCE_REENTRY_TIMING_AUTHORITY_V6
            or reentry.get("requested_ms") != SOURCE_REENTRY_RETRY_MS_V6
            or reentry.get("exact_client_cadence") is not False
            or reentry.get("policy_action") is not False
            or reentry.get("source_sink") is not False
            or reentry.get("source_sink_decision_consumed") is not False
            or reentry.get("simulator_decision_consumed") is not True
            or execution.get("wait_event") is not None
            or not isinstance(final_state, Mapping)
            or final_state.get("time_ms") != scheduled_at
            or final_state.get("needs_input") is not False
            or reentry.get("nominal_wake_time_ms") != nominal_wake
            or (
                reentry.get("actual_next_epoch_time_ms") is not None
                and (
                    type(reentry.get("actual_next_epoch_time_ms")) is not int
                    or reentry["actual_next_epoch_time_ms"] < scheduled_at
                )
            )
        ):
            add(
                "CAT_V6_SOURCE_REENTRY_CLOCK_INVALID",
                "Cat source reentry clock or scheduled simulator state is invalid",
            )
    return blockers


__all__ = (
    "CatFuryOrderedSinkExecutorV6Error",
    "EXECUTION_SCHEMA_V6",
    "IMPLEMENTATION_REVISION",
    "SOURCE_REENTRY_CLOCK_SCHEMA_V6",
    "SOURCE_REENTRY_RETRY_MS_V6",
    "SOURCE_REENTRY_TIMING_AUTHORITY_V6",
    "SOURCE_REENTRY_TRIGGER_V6",
    "_all_reached_sinks_typed_nonconsuming_with_rejected_gcd_v6",
    "_audit_ordered_execution_v6",
    "execute_cat_fury_ordered_sinks_v6",
)
