"""Native ordered sinks for the distinct deployed Contra dual-wield Raid-B body."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Mapping, Sequence

from .expert_policy import ExpertDecision
from .fury_contra_adapter_v2 import SEMANTIC_ID as CONTRA_V2_EXPERT_ID
from .fury_ordered_sink_executor_v3 import (
    EXECUTION_SCHEMA_V3, _all_gcd_attempts_are_expected_noops,
    _apply_declared_retry_wait, audit_ordered_execution_v3,
    execute_ordered_sinks_v3,
)
from .fury_runtime_bound_deployed_contra_raid_b_v1 import EXPERT_ID


SCHEMA = "fury_ordered_sink_execution/raid_b_v1"
BT_REF = "Contra_ALL.lua:32089"


class FuryOrderedSinkRaidBError(RuntimeError):
    """A Raid-B sink cannot be executed by the audited native vocabulary."""


def _project(decision: ExpertDecision) -> ExpertDecision:
    if decision.expert_id != EXPERT_ID:
        raise FuryOrderedSinkRaidBError("Raid-B executor requires Raid-B source identity")
    return replace(
        decision,
        provenance=replace(decision.provenance, expert_id=CONTRA_V2_EXPERT_ID),
    )


def execute_ordered_sinks_raid_b_v1(
    bridge: Any,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> dict[str, Any]:
    projected = _project(decision)
    result = execute_ordered_sinks_v3(
        bridge, projected, state,
        attempt_id_prefix=attempt_id_prefix,
        result_bearing_action_keys=result_bearing_action_keys,
    )
    events = result.get("sink_events")
    if not isinstance(events, list):
        raise FuryOrderedSinkRaidBError("Raid-B sink events missing")
    reasons = list(result.get("nonfaithful_reasons", ()))
    reclassified_orders = []
    for event in events:
        source = event.get("source_sink") if isinstance(event, Mapping) else None
        operation = event.get("operation_contract") if isinstance(event, Mapping) else None
        acceptance = event.get("client_acceptance") if isinstance(event, Mapping) else None
        submission = event.get("simulator_submission") if isinstance(event, Mapping) else None
        available = submission.get("available_action") if isinstance(submission, Mapping) else None
        before = event.get("simulator_state_before") if isinstance(event, Mapping) else None
        power = before.get("power") if isinstance(before, Mapping) else None
        rage = power.get("current") if isinstance(power, Mapping) else None
        if (
            isinstance(source, Mapping) and source.get("source_ref") == BT_REF
            and source.get("channel") == "gcd"
            and source.get("operation") == "QueueSpellByName"
            and isinstance(operation, Mapping)
            and operation.get("canonical_action") == "warrior.bloodthirst"
            and isinstance(acceptance, dict)
            and acceptance.get("status") == "REJECTED_UNCLASSIFIED"
            and isinstance(available, Mapping)
            and available.get("legal") is False
            and available.get("ready_in_ms") == 0
            and type(rage) in (int, float) and 0 <= rage < 30
            and decision.metadata.get("nampower_queue_spells_on_cooldown") is False
        ):
            order = event["order"]
            acceptance["status"] = "REJECTED_SOURCE_RESOURCE_RETRY_NOOP"
            acceptance["source_rejection_classification"] = (
                "RAID_B_LOW_HP_BT_RESOURCE_RETRY_NOOP"
            )
            reasons = [reason for reason in reasons if reason !=
                       f"raw_sink[{order}]:unclassified_client_rejection"]
            reclassified_orders.append(order)
    result["nonfaithful_reasons"] = reasons
    result["ordered_projection_faithful"] = not reasons
    clock = None
    current = result.get("final_state")
    if (reclassified_orders and result.get("execution_blocked") is False
            and result.get("decision_consumed") is False and not reasons
            and isinstance(current, Mapping) and current.get("needs_input") is True
            and _all_gcd_attempts_are_expected_noops(events)):
        scheduled_at = current.get("time_ms")
        _apply_declared_retry_wait(bridge, projected, result)
        after = result.get("final_state")
        if (type(scheduled_at) is not int or not isinstance(after, Mapping)
                or after.get("time_ms") != scheduled_at
                or after.get("needs_input") is not False):
            raise FuryOrderedSinkRaidBError("Raid-B retry clock did not schedule")
        clock = {
            "schema": "deployed_contra_raid_b_source_reentry_clock/v1",
            "trigger": "LOW_HP_BT_32089_TYPED_RAGE_REJECTION_NONCONSUMING",
            "timing_authority": "RUNNER_FIXED_100MS_PROXY",
            "requested_ms": 100,
            "scheduled_at_time_ms": scheduled_at,
            "nominal_wake_time_ms": scheduled_at + 100,
            "exact_client_cadence": False,
            "policy_action": False,
            "source_sink": False,
            "reclassified_sink_orders": reclassified_orders,
        }
    result["schema"] = SCHEMA
    result["base_executor_schema"] = EXECUTION_SCHEMA_V3
    result["expert_id"] = decision.expert_id
    result["source_reentry_clock"] = clock
    result["raid_b_semantics"] = {
        "identity_projection_to_audited_v2_vocabulary": True,
        "controller": "raid_b",
        "runtime_binding_sha256": decision.metadata.get("runtime_binding_sha256"),
        "reclassified_sink_orders": reclassified_orders,
        "runner_proxy_used": clock is not None,
    }
    return result


def audit_ordered_execution_raid_b_v1(
    execution: Mapping[str, Any], proposal: ExpertDecision, *, decision_index: int,
) -> list[dict[str, Any]]:
    projected = deepcopy(dict(execution))
    projected["schema"] = EXECUTION_SCHEMA_V3
    projected["base_executor_schema"] = "fury_ordered_sink_execution/v2"
    projected["expert_id"] = CONTRA_V2_EXPERT_ID
    projected.pop("source_reentry_clock", None)
    projected.pop("raid_b_semantics", None)
    return audit_ordered_execution_v3(
        projected, _project(proposal), decision_index=decision_index,
    )


__all__ = (
    "SCHEMA", "FuryOrderedSinkRaidBError", "execute_ordered_sinks_raid_b_v1",
    "audit_ordered_execution_raid_b_v1",
)
