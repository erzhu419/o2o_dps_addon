"""Development-only deployed Contra reentry for low-HP Raid-A resource no-ops.

The rejected cast remains rejected.  A separate fixed 100 ms runner clock
represents a later macro invocation; it is not a policy action, a source sink,
or an observation of the player's actual key cadence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from .expert_policy import ExpertDecision
from .fury_ordered_sink_executor_v3 import (
    _all_gcd_attempts_are_expected_noops,
    _apply_declared_retry_wait,
)
from .fury_ordered_sink_executor_v4 import (
    EXECUTION_SCHEMA_V4,
    audit_ordered_execution_v4,
    execute_ordered_sinks_v4,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    RUNTIME_BOUND_EXPERT_ID_V7,
)


EXECUTION_SCHEMA_V5 = "fury_ordered_sink_execution/v5"
SOURCE_REENTRY_CLOCK_SCHEMA_V5 = "deployed_contra_source_reentry_clock/v5"
SOURCE_REENTRY_TIMING_AUTHORITY_V5 = "RUNNER_FIXED_100MS_PROXY"
LOW_HP_BT_SOURCE_REF_V5 = "Contra_ALL.lua:31683"
LOW_HP_BT_COST_V5 = 30.0
LOW_HP_EXECUTE_SOURCE_REF_V5 = "Contra_ALL.lua:31684"
# Controlled live profile has Improved Execute 2/2: native cost 15 - 5 = 10 rage.
LOW_HP_EXECUTE_COST_V5 = 10.0
_LOW_HP_RESOURCE_BRANCHES_V5 = {
    (LOW_HP_BT_SOURCE_REF_V5, "warrior.bloodthirst"): (
        LOW_HP_BT_COST_V5, "EXACT_LOW_HP_BT_SOURCE_BRANCH_RESOURCE_RETRY_NOOP",
    ),
    (LOW_HP_EXECUTE_SOURCE_REF_V5, "warrior.execute"): (
        LOW_HP_EXECUTE_COST_V5, "EXACT_LOW_HP_EXECUTE_SOURCE_BRANCH_RESOURCE_RETRY_NOOP",
    ),
}


class FuryOrderedSinkExecutorV5Error(RuntimeError):
    """The bounded deployed Contra reentry contract was violated."""


def _low_hp_resource_rejection(
    decision: ExpertDecision, event: Mapping[str, Any]
) -> tuple[str, float] | None:
    if decision.expert_id != RUNTIME_BOUND_EXPERT_ID_V7:
        return None
    source = event.get("source_sink")
    operation = event.get("operation_contract")
    acceptance = event.get("client_acceptance")
    submission = event.get("simulator_submission")
    consumption = event.get("decision_consumption")
    available = submission.get("available_action") if isinstance(submission, Mapping) else None
    before = event.get("simulator_state_before")
    power = before.get("power") if isinstance(before, Mapping) else None
    rage = power.get("current") if isinstance(power, Mapping) else None
    branch = (
        (source.get("source_ref"), operation.get("canonical_action"))
        if isinstance(source, Mapping) and isinstance(operation, Mapping) else None
    )
    resource = _LOW_HP_RESOURCE_BRANCHES_V5.get(branch)
    if resource is None:
        return None
    cost, classification = resource
    if (
        isinstance(source, Mapping)
        and source.get("channel") == "gcd"
        and source.get("operation") == "QueueSpellByName"
        and isinstance(operation, Mapping)
        and decision.metadata.get("nampower_queue_spells_on_cooldown") is False
        and isinstance(acceptance, Mapping)
        and acceptance.get("status") == "REJECTED_UNCLASSIFIED"
        and isinstance(available, Mapping)
        and available.get("legal") is False
        and type(available.get("ready_in_ms")) is int
        and available["ready_in_ms"] == 0
        and isinstance(consumption, Mapping)
        and consumption.get("consumes_decision") is False
        and type(rage) in (int, float)
        and 0 <= rage < cost
    ):
        return classification, cost
    return None


def execute_ordered_sinks_v5(
    bridge: Any,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Classify only source-matched low-HP rage rejections, then reenter."""

    result = execute_ordered_sinks_v4(
        bridge,
        decision,
        state,
        attempt_id_prefix=attempt_id_prefix,
        result_bearing_action_keys=result_bearing_action_keys,
    )
    events = result.get("sink_events")
    if not isinstance(events, list):
        raise FuryOrderedSinkExecutorV5Error("ordered sink events are missing")
    reasons = list(result.get("nonfaithful_reasons", ()))
    reclassified_orders: list[int] = []
    reclassified_source_refs: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        resource_rejection = _low_hp_resource_rejection(decision, event)
        if resource_rejection is None:
            continue
        classification, cost = resource_rejection
        order = event.get("order")
        if type(order) is not int:
            raise FuryOrderedSinkExecutorV5Error("rejected sink order is invalid")
        acceptance = event["client_acceptance"]
        acceptance["status"] = "REJECTED_SOURCE_RESOURCE_RETRY_NOOP"
        acceptance["source_rejection_classification"] = classification
        acceptance["evidence"] = (
            f"{event['source_sink']['source_ref']};"
            f"QueueSpellByName({event['operation_contract']['canonical_action']});"
            f"ready_in_ms=0;legal=false;0<=rage<{cost:g}"
        )
        reasons = [
            reason for reason in reasons
            if reason != f"raw_sink[{order}]:unclassified_client_rejection"
        ]
        reclassified_orders.append(order)
        reclassified_source_refs.append(event["source_sink"]["source_ref"])
    result["nonfaithful_reasons"] = reasons
    result["ordered_projection_faithful"] = not reasons

    clock = None
    current = result.get("final_state")
    if (
        reclassified_orders
        and result.get("execution_blocked") is False
        and result.get("decision_consumed") is False
        and not reasons
        and isinstance(current, Mapping)
        and current.get("needs_input") is True
        and _all_gcd_attempts_are_expected_noops(events)
    ):
        scheduled_at = current.get("time_ms")
        retry_ms = decision.metadata.get("known_noop_retry_wait_ms")
        if type(scheduled_at) is not int or retry_ms != 100:
            raise FuryOrderedSinkExecutorV5Error(
                "deployed Contra reentry requires a 100 ms runner clock"
            )
        _apply_declared_retry_wait(bridge, decision, result)
        after = result.get("final_state")
        wait_event = result.get("wait_event")
        if (
            not isinstance(after, Mapping)
            or after.get("time_ms") != scheduled_at
            or after.get("needs_input") is not False
            or not isinstance(wait_event, Mapping)
            or result.get("decision_consumed") is not True
        ):
            raise FuryOrderedSinkExecutorV5Error(
                "deployed Contra source reentry was not scheduled"
            )
        clock = {
            "schema": SOURCE_REENTRY_CLOCK_SCHEMA_V5,
            "trigger": (
                "LOW_HP_EXECUTE_31684_TYPED_RAGE_REJECTION_NONCONSUMING"
                if LOW_HP_EXECUTE_SOURCE_REF_V5 in reclassified_source_refs else
                "LOW_HP_BT_31683_TYPED_RAGE_REJECTION_NONCONSUMING"
            ),
            "timing_authority": SOURCE_REENTRY_TIMING_AUTHORITY_V5,
            "requested_ms": 100,
            "scheduled_at_time_ms": scheduled_at,
            "nominal_wake_time_ms": scheduled_at + 100,
            "exact_client_cadence": False,
            "policy_action": False,
            "source_sink": False,
            "source_sink_decision_consumed": False,
            "simulator_decision_consumed": True,
            "reclassified_sink_orders": reclassified_orders,
            "reclassified_source_refs": reclassified_source_refs,
        }

    result["schema"] = EXECUTION_SCHEMA_V5
    result["base_executor_schema"] = EXECUTION_SCHEMA_V4
    result["source_reentry_clock"] = clock
    result["v5_semantics"] = {
        "low_hp_bt_resource_rejection_source_ref": LOW_HP_BT_SOURCE_REF_V5,
        "low_hp_execute_resource_rejection_source_ref": LOW_HP_EXECUTE_SOURCE_REF_V5,
        "reclassified_sink_orders": reclassified_orders,
        "reclassified_source_refs": reclassified_source_refs,
        "runner_proxy_used": clock is not None,
        "deployed_contra_source_changed": False,
        "contra260817_substitution_used": False,
    }
    return result


def audit_ordered_execution_v5(
    execution: Mapping[str, Any],
    proposal: ExpertDecision,
    *,
    decision_index: int,
) -> list[dict[str, Any]]:
    projected = deepcopy(dict(execution))
    projected["schema"] = EXECUTION_SCHEMA_V4
    projected["base_executor_schema"] = "fury_ordered_sink_execution/v3"
    clock = projected.pop("source_reentry_clock", None)
    semantics = projected.pop("v5_semantics", None)
    if not isinstance(semantics, Mapping):
        raise FuryOrderedSinkExecutorV5Error("v5 semantics receipt is missing")
    if (clock is not None) != (semantics.get("runner_proxy_used") is True):
        raise FuryOrderedSinkExecutorV5Error("source reentry clock receipt mismatch")
    if isinstance(clock, Mapping):
        wait_event = execution.get("wait_event")
        current = execution.get("final_state")
        if (
            clock.get("schema") != SOURCE_REENTRY_CLOCK_SCHEMA_V5
            or clock.get("timing_authority") != SOURCE_REENTRY_TIMING_AUTHORITY_V5
            or clock.get("requested_ms") != 100
            or clock.get("exact_client_cadence") is not False
            or clock.get("policy_action") is not False
            or clock.get("source_sink") is not False
            or clock.get("source_sink_decision_consumed") is not False
            or clock.get("simulator_decision_consumed") is not True
            or clock.get("nominal_wake_time_ms")
            != clock.get("scheduled_at_time_ms", -100) + 100
            or not isinstance(wait_event, Mapping)
            or not isinstance(current, Mapping)
            or current.get("time_ms") != clock.get("scheduled_at_time_ms")
            or current.get("needs_input") is not False
        ):
            raise FuryOrderedSinkExecutorV5Error("source reentry clock is invalid")
    return audit_ordered_execution_v4(
        projected, proposal, decision_index=decision_index
    )


__all__ = (
    "EXECUTION_SCHEMA_V5",
    "SOURCE_REENTRY_CLOCK_SCHEMA_V5",
    "audit_ordered_execution_v5",
    "execute_ordered_sinks_v5",
)
