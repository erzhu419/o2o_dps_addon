"""Ordered source-sink execution for the audited Cat/Contra Fury adapters.

This is a versioned execution layer, not a replacement for the frozen v1
closed loop.  The v1 loop projects an :class:`ExpertDecision` into normalized
lanes.  That is insufficient for deployed Contra because one Lua invocation
can call several GCD sinks in sequence.  V2 instead treats ``raw_sink_order``
as the source-attempt ledger and records client/simulator acceptance,
decision consumption, queue state, traversal, and server outcome evidence as
separate facts.

Only the operations emitted by the audited deployed Cat profile and deployed
Contra adapters are accepted.  The preflight is deliberately fail closed: an
unknown expert, operation, value, or inconsistent normalized lane prevents
*all* bridge mutation while still returning a complete ordered trace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence

from .expert_policy import (
    CastControl,
    ExpertDecision,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    WAIT_ACTION,
)
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .sim_bridge import ActionRef, ActResult, AvailableAction


EXECUTION_SCHEMA = "fury_ordered_sink_execution/v2"

CAT_EXPERT_ID = "cat.fury.profile1"
CONTRA_EXPERT_IDS = frozenset(
    {
        "contra.deployed.fury.raid_a",
        "contra.deployed.fury.raid_a.v2",
    }
)

# Exact (channel, operation) pairs observed in the audited source adapters.
# Values and source refs are checked separately during preflight.
SUPPORTED_OPERATION_COVERAGE_V2: Mapping[str, frozenset[tuple[str, str]]] = {
    CAT_EXPERT_ID: frozenset(
        {
            ("autoattack", "MPStartAttack"),
            ("cast_control", "SpellStopCasting"),
            ("gcd", "CastSpellByName"),
            ("gcd", "MPCastWithoutNampower"),
            ("off_gcd", "MPCastWithNampower"),
            ("stance", "CastSpellByName"),
            ("swing_queue", "MPCastWithNampower"),
        }
    ),
    "contra.deployed.fury.raid_a": frozenset(
        {
            ("autoattack", "Contra.StartAttack"),
            ("cast_control", "SpellStopCasting"),
            ("gcd", "CastSpellByName"),
            ("gcd", "QueueSpellByName"),
            ("off_gcd", "Contra prelude helper"),
            ("stance", "ContraZSCast"),
            ("swing_queue", "CastSpellByName"),
            ("swing_queue", "QueueSpellByName"),
        }
    ),
    "contra.deployed.fury.raid_a.v2": frozenset(
        {
            ("autoattack", "Contra.StartAttack"),
            ("cast_control", "SpellStopCasting"),
            ("gcd", "CastSpellByName"),
            ("gcd", "QueueSpellByName"),
            ("off_gcd", "Contra prelude helper"),
            ("stance", "ContraZSCast"),
            ("swing_queue", "CastSpellByName"),
            ("swing_queue", "QueueSpellByName"),
        }
    ),
}

UNSUPPORTED_OPERATION_COVERAGE_V2 = (
    "target-selection sinks (not emitted by the audited Cat/Contra Fury adapters)",
    "next-swing cancellation (Cat's deployed cancel helper is commented out and "
    "Contra emits no cancel sink)",
    "Cat2 operations and Contra260817/contra_new operations",
    "arbitrary Contra prelude helper values without an exact simulator action map",
)


_VALUE_TO_ACTION_KEY: Mapping[str, str] = {
    **{key: key for key in ACTION_KEY_TO_REF},
    "战斗怒吼": "warrior.battle_shout",
    "战斗姿态": "warrior.battle_stance",
    "防御姿态": "warrior.defensive_stance",
    "狂暴姿态": "warrior.berserker_stance",
    "血性狂暴": "warrior.bloodrage",
    "嗜血": "warrior.bloodthirst",
    "斩杀": "warrior.execute",
    "断筋": "warrior.hamstring",
    "拳击": "warrior.pummel",
    "猛击": "warrior.slam",
    "破甲攻击": "warrior.sunder_armor",
    "旋风斩": "warrior.whirlwind",
}

_VALUE_TO_QUEUE: Mapping[str, SwingQueueOp] = {
    "英勇打击": SwingQueueOp.HEROIC_STRIKE,
    "warrior.heroic_strike": SwingQueueOp.HEROIC_STRIKE,
    "HEROIC_STRIKE": SwingQueueOp.HEROIC_STRIKE,
    "顺劈斩": SwingQueueOp.CLEAVE,
    "warrior.cleave": SwingQueueOp.CLEAVE,
    "CLEAVE": SwingQueueOp.CLEAVE,
}

_VALUE_TO_STANCE: Mapping[str, StanceOp] = {
    "战斗姿态": StanceOp.BATTLE,
    "warrior.battle_stance": StanceOp.BATTLE,
    "防御姿态": StanceOp.DEFENSIVE,
    "warrior.defensive_stance": StanceOp.DEFENSIVE,
    "狂暴姿态": StanceOp.BERSERKER,
    "warrior.berserker_stance": StanceOp.BERSERKER,
}


class OrderedSinkExecutionError(RuntimeError):
    """The bridge violated the typed ordered-sink execution contract."""


@dataclass(frozen=True)
class ControlSinkResultV2:
    """Typed result for optional ``start_attack`` / ``stop_cast`` controls.

    Bridges without these controls remain explicitly capability-incomplete;
    implementations that expose them share this result identity without
    changing the execution trace schema.
    """

    accepted: bool
    consumes_decision: bool
    state: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be boolean")
        if not isinstance(self.consumes_decision, bool):
            raise TypeError("consumes_decision must be boolean")
        if not isinstance(self.state, Mapping):
            raise TypeError("state must be a mapping")


class OrderedSinkBridgeLikeV2(Protocol):
    """Minimum bridge surface; control methods are optional capabilities."""

    def actions(self) -> list[AvailableAction]: ...

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult: ...

    def wait(self, wait_ms: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _ResolvedSink:
    sink: RawSink
    action_key: str | None = None
    action_ref: ActionRef | None = None
    queue: SwingQueueOp | None = None
    stance: StanceOp | None = None
    recognized: bool = True


def execute_ordered_sinks_v2(
    bridge: OrderedSinkBridgeLikeV2,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Execute one decision from its exact raw source-sink order.

    No fallback action or fallback wait is ever synthesized.  If the source
    decision is ``WAIT``, its explicit duration is submitted after all raw
    source attempts unless a prior operation consumed the decision or a
    fail-closed preflight/runtime boundary prevents further mutation.
    """

    if not isinstance(decision, ExpertDecision):
        raise TypeError("decision must be ExpertDecision")
    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    if attempt_id_prefix is not None and (
        not isinstance(attempt_id_prefix, str) or not attempt_id_prefix.strip()
    ):
        raise TypeError("attempt_id_prefix must be a nonempty string or None")
    if isinstance(result_bearing_action_keys, (str, bytes)) or not isinstance(
        result_bearing_action_keys, Sequence
    ):
        raise TypeError("result_bearing_action_keys must be a sequence of strings")
    if any(
        not isinstance(action_key, str) or not action_key.strip()
        for action_key in result_bearing_action_keys
    ):
        raise TypeError("result_bearing_action_keys must contain nonempty strings")
    result_bearing = frozenset(result_bearing_action_keys)

    current = dict(state)
    resolved, preflight_reasons = _preflight(decision)
    blocked = bool(preflight_reasons)
    consumed = not bool(current.get("needs_input", True))
    nonfaithful = list(preflight_reasons)
    events: list[dict[str, Any]] = []
    accepted_gcd_actions: list[str] = []

    for index, item in enumerate(resolved, start=1):
        source_traversal = (
            "CONTINUE_TO_NEXT_RAW_SINK"
            if index < len(resolved)
            else "END_RECORDED_RAW_SINK_SEQUENCE"
        )
        if blocked:
            event = _base_event(index, item, current, source_traversal)
            event["simulator_submission"] = {
                "status": "NOT_SUBMITTED_FAIL_CLOSED",
                "reason": "preflight_or_prior_runtime_boundary",
            }
            event["client_acceptance"] = {
                "status": "NOT_APPLICABLE_NOT_SUBMITTED"
            }
            event["decision_consumption"] = {
                "status": "NOT_APPLICABLE_NOT_SUBMITTED",
                "consumes_decision": None,
            }
            event["server_result"] = _server_result("NOT_APPLICABLE_NOT_SUBMITTED")
            event["traversal"]["executor_state"] = "FAIL_CLOSED"
            events.append(event)
            continue

        if consumed:
            event = _base_event(index, item, current, source_traversal)
            event["simulator_submission"] = {
                "status": "NOT_SUBMITTED_DECISION_ALREADY_CONSUMED"
            }
            event["client_acceptance"] = {
                "status": "NOT_APPLICABLE_DECISION_ALREADY_CONSUMED"
            }
            event["decision_consumption"] = {
                "status": "ALREADY_CONSUMED_BY_EARLIER_SINK",
                "consumes_decision": None,
            }
            event["server_result"] = _server_result("NOT_APPLICABLE_NOT_SUBMITTED")
            event["traversal"]["executor_state"] = (
                "TRACE_SOURCE_ATTEMPT_WITHOUT_SIMULATOR_SUBMISSION"
            )
            events.append(event)
            nonfaithful.append(
                f"raw_sink[{index}]:simulator_cannot_submit_after_decision_consumed"
            )
            continue

        try:
            result_attempt_id = None
            if (
                attempt_id_prefix is not None
                and item.sink.channel == "gcd"
                and item.action_key in result_bearing
            ):
                result_attempt_id = f"{attempt_id_prefix}:sink-{index}"
            event, current, sink_consumed, runtime_reason, runtime_blocked = (
                _execute_resolved_sink(
                    bridge,
                    decision,
                    item,
                    current,
                    index=index,
                    source_traversal=source_traversal,
                    result_attempt_id=result_attempt_id,
                )
            )
        except Exception as error:  # bridge mutation boundaries must fail closed
            event = _base_event(index, item, current, source_traversal)
            reason = f"raw_sink[{index}]:bridge_exception:{type(error).__name__}"
            event["simulator_submission"] = {
                "status": "BRIDGE_ERROR_FAIL_CLOSED",
                "error_type": type(error).__name__,
            }
            event["client_acceptance"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
            event["decision_consumption"] = {
                "status": "UNKNOWN_BRIDGE_ERROR",
                "consumes_decision": None,
            }
            event["server_result"] = _server_result("UNKNOWN_BRIDGE_ERROR")
            event["traversal"]["executor_state"] = "FAIL_CLOSED"
            runtime_reason = reason
            runtime_blocked = True
            sink_consumed = False
        events.append(event)
        if runtime_reason is not None:
            nonfaithful.append(runtime_reason)
        if event["client_acceptance"]["status"] == "ACCEPTED" and item.sink.channel == "gcd":
            if item.action_key is not None:
                accepted_gcd_actions.append(item.action_key)
        consumed = consumed or sink_consumed
        blocked = blocked or runtime_blocked

    wait_event: dict[str, Any] | None = None
    if decision.gcd == WAIT_ACTION:
        wait_event = _execute_source_wait(
            bridge,
            decision,
            current,
            blocked=blocked,
            consumed=consumed,
        )
        current = dict(wait_event.pop("_state"))
        wait_reason = wait_event.pop("_nonfaithful_reason")
        wait_blocked = wait_event.pop("_blocked")
        if wait_reason is not None:
            nonfaithful.append(wait_reason)
        blocked = blocked or wait_blocked
        if wait_event["decision_consumption"]["consumes_decision"] is True:
            consumed = True

    return {
        "schema": EXECUTION_SCHEMA,
        "expert_id": decision.expert_id,
        "source_decision": decision.to_dict(),
        "raw_sink_order": [sink.to_dict() for sink in decision.raw_sink_order],
        "sink_events": events,
        "wait_event": wait_event,
        "fallback": {
            "used": False,
            "reason": "V2_NEVER_SYNTHESIZES_FALLBACK",
        },
        "decision_consumed": consumed,
        "execution_blocked": blocked,
        "accepted_gcd_actions": accepted_gcd_actions,
        "server_outcome_contract": (
            "IMMEDIATE_BRIDGE_STATE_IS_NOT_A_SERVER_OR_DAMAGE_OUTCOME"
        ),
        "nonfaithful_reasons": _deduplicate(nonfaithful),
        "ordered_projection_faithful": not nonfaithful,
        "final_state": current,
    }


def _preflight(
    decision: ExpertDecision,
) -> tuple[list[_ResolvedSink], list[str]]:
    reasons: list[str] = []
    resolved: list[_ResolvedSink] = []
    allowed = SUPPORTED_OPERATION_COVERAGE_V2.get(decision.expert_id)

    if not decision.valid:
        reasons.append(f"proposal:invalid:{decision.reason or 'unspecified'}")
    if allowed is None:
        reasons.append(f"proposal:unsupported_expert_id:{decision.expert_id}")
    if decision.provenance.role is not ExpertRole.DEPLOYED:
        reasons.append("proposal:expert_role_is_not_deployed")
    if decision.provenance.kind is not ProvenanceKind.SOURCE_DERIVED:
        reasons.append("proposal:provenance_is_not_source_derived")

    for index, sink in enumerate(decision.raw_sink_order, start=1):
        item, sink_reasons = _resolve_sink(sink, index=index)
        reasons.extend(sink_reasons)
        pair_supported = allowed is not None and (sink.channel, sink.operation) in allowed
        if not pair_supported:
            reasons.append(
                f"raw_sink[{index}]:unsupported_operation:"
                f"{sink.channel}/{sink.operation}"
            )
        item = replace(
            item,
            recognized=(
                pair_supported
                and not any(":unsupported_" in reason for reason in sink_reasons)
            ),
        )
        resolved.append(item)

    reasons.extend(_normalized_lane_reasons(decision, resolved))
    return resolved, _deduplicate(reasons)


def _resolve_sink(
    sink: RawSink,
    *,
    index: int,
) -> tuple[_ResolvedSink, list[str]]:
    reasons: list[str] = []
    if not isinstance(sink.source_ref, str) or not sink.source_ref.strip():
        reasons.append(f"raw_sink[{index}]:source_ref_missing")

    if sink.channel == "autoattack":
        if sink.value != "START":
            reasons.append(f"raw_sink[{index}]:unsupported_autoattack_value:{sink.value}")
        return _ResolvedSink(sink), reasons
    if sink.channel == "cast_control":
        if sink.operation != "SpellStopCasting" or sink.value is not None:
            reasons.append(f"raw_sink[{index}]:unsupported_cast_control_value")
        return _ResolvedSink(sink), reasons
    if sink.channel == "swing_queue":
        queue = _VALUE_TO_QUEUE.get(sink.value or "")
        if queue is None:
            reasons.append(f"raw_sink[{index}]:unsupported_queue_value:{sink.value}")
            return _ResolvedSink(sink), reasons
        return _ResolvedSink(sink, action_ref=QUEUE_REFS[queue], queue=queue), reasons
    if sink.channel in {"gcd", "off_gcd", "stance"}:
        action_key = _VALUE_TO_ACTION_KEY.get(sink.value or "")
        if action_key is None:
            reasons.append(f"raw_sink[{index}]:unsupported_action_value:{sink.value}")
            return _ResolvedSink(sink), reasons
        stance = _VALUE_TO_STANCE.get(sink.value or "") if sink.channel == "stance" else None
        if sink.channel == "stance" and stance is None:
            reasons.append(f"raw_sink[{index}]:unsupported_stance_value:{sink.value}")
        return (
            _ResolvedSink(
                sink,
                action_key=action_key,
                action_ref=ACTION_KEY_TO_REF[action_key],
                stance=stance,
            ),
            reasons,
        )
    reasons.append(f"raw_sink[{index}]:unsupported_channel:{sink.channel}")
    return _ResolvedSink(sink), reasons


def _normalized_lane_reasons(
    decision: ExpertDecision,
    resolved: list[_ResolvedSink],
) -> list[str]:
    reasons: list[str] = []
    raw_gcd = [item for item in resolved if item.sink.channel == "gcd"]
    raw_gcd_keys = [item.action_key for item in raw_gcd if item.action_key is not None]
    declared_calls = decision.metadata.get("raw_gcd_calls")
    if declared_calls is not None and list(declared_calls) != raw_gcd_keys:
        reasons.append("proposal:metadata_raw_gcd_calls_mismatch")

    known_noops = decision.metadata.get("known_noop_source_gcd_attempts", [])
    noop_identities: list[tuple[str, str, str]] = []
    if isinstance(known_noops, list):
        for item in known_noops:
            if isinstance(item, Mapping):
                action = item.get("action")
                operation = item.get("operation")
                source_ref = item.get("source_ref")
                if all(isinstance(value, str) for value in (action, operation, source_ref)):
                    noop_identities.append((action, operation, source_ref))
    elif known_noops:
        reasons.append("proposal:known_noop_metadata_is_not_array")

    state_effecting_gcd: list[str] = []
    remaining_noops = list(noop_identities)
    for item in raw_gcd:
        if item.action_key is None:
            continue
        identity = (item.action_key, item.sink.operation, item.sink.source_ref or "")
        if identity in remaining_noops:
            remaining_noops.remove(identity)
        else:
            state_effecting_gcd.append(item.action_key)
    if remaining_noops:
        reasons.append("proposal:known_noop_metadata_has_no_matching_raw_sink")

    if decision.gcd == WAIT_ACTION:
        if state_effecting_gcd:
            reasons.append("proposal:wait_conflicts_with_state_effecting_raw_gcd")
    elif not state_effecting_gcd:
        reasons.append("proposal:normalized_gcd_has_no_raw_source_sink")
    elif state_effecting_gcd[-1] != decision.gcd:
        reasons.append("proposal:normalized_gcd_is_not_last_state_effecting_raw_sink")

    raw_off_gcd = [
        item.action_key
        for item in resolved
        if item.sink.channel == "off_gcd" and item.action_key is not None
    ]
    if tuple(raw_off_gcd) != decision.off_gcd:
        reasons.append("proposal:normalized_off_gcd_mismatch")

    raw_queues = [
        item.queue
        for item in resolved
        if item.sink.channel == "swing_queue" and item.queue is not None
    ]
    if decision.swing_queue is SwingQueueOp.CANCEL:
        reasons.append("proposal:queue_cancel_has_no_audited_raw_sink_operation")
    elif raw_queues:
        if raw_queues[-1] is not decision.swing_queue:
            reasons.append("proposal:normalized_swing_queue_mismatch")
    elif decision.swing_queue is not SwingQueueOp.KEEP:
        reasons.append("proposal:normalized_swing_queue_has_no_raw_source_sink")

    raw_stances = [
        item.stance
        for item in resolved
        if item.sink.channel == "stance" and item.stance is not None
    ]
    if raw_stances:
        if raw_stances[-1] is not decision.stance:
            reasons.append("proposal:normalized_stance_mismatch")
    elif decision.stance is not StanceOp.KEEP:
        reasons.append("proposal:normalized_stance_has_no_raw_source_sink")

    has_stop = any(item.sink.channel == "cast_control" for item in resolved)
    if has_stop != (decision.cast_control is CastControl.STOP_CAST):
        reasons.append("proposal:normalized_cast_control_mismatch")
    return reasons


def _execute_resolved_sink(
    bridge: OrderedSinkBridgeLikeV2,
    decision: ExpertDecision,
    item: _ResolvedSink,
    current: dict[str, Any],
    *,
    index: int,
    source_traversal: str,
    result_attempt_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], bool, str | None, bool]:
    event = _base_event(index, item, current, source_traversal)
    if result_attempt_id is not None:
        event["source_attempt"]["attempt_id"] = result_attempt_id
    sink = item.sink
    if sink.channel == "autoattack":
        return _execute_control(
            bridge,
            "start_attack",
            item,
            current,
            event,
            absent_reason="bridge_has_no_start_attack_control",
            block_if_absent=False,
        )
    if sink.channel == "cast_control":
        casting = _is_casting(current)
        if not casting:
            event["simulator_submission"] = {
                "status": "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
                "reason": "no_active_cast",
            }
            event["client_acceptance"] = {
                "status": "NOT_APPLICABLE_NO_ACTIVE_CAST"
            }
            event["decision_consumption"] = {
                "status": "NOT_CONSUMED",
                "consumes_decision": False,
            }
            event["server_result"] = _server_result("NOT_APPLICABLE_CLIENT_NOOP")
            return event, current, False, None, False
        return _execute_control(
            bridge,
            "stop_cast",
            item,
            current,
            event,
            absent_reason="bridge_has_no_stop_cast_control_while_casting",
            block_if_absent=True,
        )

    assert item.action_ref is not None
    available = {row.action: row for row in bridge.actions()}
    row = available.get(item.action_ref)
    if row is None:
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_ACTION_ABSENT_FROM_SPELLBOOK",
            "action": item.action_ref.to_wire(),
        }
        event["client_acceptance"] = {
            "status": "REJECTED_ACTION_ABSENT_FROM_SIMULATOR_SPELLBOOK"
        }
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["server_result"] = _server_result("NOT_APPLICABLE_CLIENT_REJECTED")
        return (
            event,
            current,
            False,
            f"raw_sink[{index}]:action_absent_from_simulator_spellbook",
            True,
        )

    expected_consumption = sink.channel == "gcd"
    before_queue = _queued_swing(current)
    result = (
        bridge.act(item.action_ref, attempt_id=result_attempt_id)
        if result_attempt_id is not None
        else bridge.act(item.action_ref)
    )
    if not isinstance(result, ActResult):
        raise OrderedSinkExecutionError("bridge.act must return ActResult")
    after = dict(result.state)
    after_queue = _queued_swing(after)
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "action": item.action_ref.to_wire(),
        "available_action": _available_action(row),
    }
    declared_noop = (not result.casted) and _source_declares_known_noop(
        decision, item
    )
    acceptance_status = "ACCEPTED"
    if not result.casted:
        acceptance_status = (
            "REJECTED_SOURCE_DECLARED_NOOP"
            if declared_noop
            else "REJECTED_UNCLASSIFIED"
        )
    event["client_acceptance"] = {
        "status": acceptance_status,
        "evidence": "bridge.act.casted",
        "source_rejection_classification": (
            "NOT_APPLICABLE_ACCEPTED"
            if result.casted
            else (
                "KNOWN_SOURCE_NOOP" if declared_noop else "UNCLASSIFIED"
            )
        ),
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
        "consumes_decision": result.consumes_decision,
        "expected_for_lane": expected_consumption,
        "available_action_triggers_gcd": row.triggers_gcd,
    }
    event["simulator_state_after_immediate"] = after
    event["server_result"] = _server_result(
        "PENDING_OR_NOT_EXPOSED" if result.casted else "NOT_APPLICABLE_CLIENT_REJECTED"
    )
    if item.queue is not None:
        event["queue_transition"] = _queue_transition(
            before_queue,
            item.queue,
            after_queue,
            accepted=result.casted,
        )

    reason: str | None = None
    if not result.casted and not declared_noop:
        reason = f"raw_sink[{index}]:unclassified_client_rejection"
    if row.triggers_gcd is not expected_consumption:
        reason = (
            f"raw_sink[{index}]:triggers_gcd_contract_mismatch:"
            f"bridge={str(row.triggers_gcd).lower()}:"
            f"lane_expected={str(expected_consumption).lower()}"
        )
    if result.casted and result.consumes_decision is not expected_consumption:
        mismatch = (
            f"raw_sink[{index}]:decision_consumption_mismatch:"
            f"bridge={str(result.consumes_decision).lower()}:"
            f"lane_expected={str(expected_consumption).lower()}"
        )
        reason = mismatch if reason is None else f"{reason};{mismatch}"
    queue_kind = event["queue_transition"]["kind"]
    if item.queue is not None and queue_kind in {
        "ACCEPTED_QUEUE_STATE_NOT_OBSERVED",
        "REJECTED_STATE_CHANGED",
    }:
        mismatch = f"raw_sink[{index}]:queue_transition:{queue_kind}"
        reason = mismatch if reason is None else f"{reason};{mismatch}"
    return event, after, result.consumes_decision, reason, False


def _source_declares_known_noop(
    decision: ExpertDecision,
    item: _ResolvedSink,
) -> bool:
    attempts = decision.metadata.get("known_noop_source_gcd_attempts", ())
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        return False
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        if (
            attempt.get("action") == item.action_key
            and attempt.get("operation") == item.sink.operation
            and attempt.get("source_ref") == item.sink.source_ref
        ):
            return True
    return False


def _execute_control(
    bridge: OrderedSinkBridgeLikeV2,
    method_name: str,
    item: _ResolvedSink,
    current: dict[str, Any],
    event: dict[str, Any],
    *,
    absent_reason: str,
    block_if_absent: bool,
) -> tuple[dict[str, Any], dict[str, Any], bool, str | None, bool]:
    method = getattr(bridge, method_name, None)
    if not callable(method):
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_BRIDGE_CAPABILITY_ABSENT",
            "control": method_name,
        }
        event["client_acceptance"] = {
            "status": "UNKNOWN_BRIDGE_CAPABILITY_ABSENT"
        }
        event["decision_consumption"] = {
            "status": "NOT_APPLICABLE_NOT_SUBMITTED",
            "consumes_decision": None,
        }
        event["server_result"] = _server_result("UNKNOWN_NOT_EXPOSED")
        return (
            event,
            current,
            False,
            f"{item.sink.channel}:{absent_reason}",
            block_if_absent,
        )
    result = method()
    if not isinstance(result, ControlSinkResultV2):
        raise OrderedSinkExecutionError(
            f"bridge.{method_name} must return ControlSinkResultV2"
        )
    after = dict(result.state)
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "control": method_name,
    }
    event["client_acceptance"] = {
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "evidence": f"bridge.{method_name}.accepted",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
        "consumes_decision": result.consumes_decision,
        "expected_for_lane": False,
    }
    event["simulator_state_after_immediate"] = after
    event["server_result"] = _server_result(
        "PENDING_OR_NOT_EXPOSED" if result.accepted else "NOT_APPLICABLE_CLIENT_REJECTED"
    )
    reason = None
    if result.accepted and result.consumes_decision:
        reason = f"{item.sink.channel}:{method_name}_unexpectedly_consumed_decision"
    return event, after, result.consumes_decision, reason, False


def _execute_source_wait(
    bridge: OrderedSinkBridgeLikeV2,
    decision: ExpertDecision,
    current: dict[str, Any],
    *,
    blocked: bool,
    consumed: bool,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "SOURCE_WAIT_DECISION",
        "requested_wait_ms": decision.wait_ms,
        "source_decision": WAIT_ACTION,
        "fallback": False,
        "server_result": _server_result("NOT_APPLICABLE_WAIT_HAS_NO_SERVER_RESULT"),
        "_state": current,
        "_nonfaithful_reason": None,
        "_blocked": False,
    }
    if blocked:
        event["_blocked"] = True
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_FAIL_CLOSED"
        }
        event["client_acceptance"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}
        event["decision_consumption"] = {
            "status": "NOT_APPLICABLE_NOT_SUBMITTED",
            "consumes_decision": None,
        }
        return event
    if consumed or not bool(current.get("needs_input", True)):
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_DECISION_ALREADY_CONSUMED"
        }
        event["client_acceptance"] = {
            "status": "NOT_APPLICABLE_DECISION_ALREADY_CONSUMED"
        }
        event["decision_consumption"] = {
            "status": "ALREADY_CONSUMED_BY_EARLIER_SINK",
            "consumes_decision": None,
        }
        return event
    assert decision.wait_ms is not None
    try:
        after = bridge.wait(decision.wait_ms)
    except Exception as error:
        event["simulator_submission"] = {
            "status": "BRIDGE_ERROR_FAIL_CLOSED",
            "wait_ms": decision.wait_ms,
            "error_type": type(error).__name__,
        }
        event["client_acceptance"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
        event["decision_consumption"] = {
            "status": "UNKNOWN_BRIDGE_ERROR",
            "consumes_decision": None,
        }
        event["server_result"] = _server_result("UNKNOWN_BRIDGE_ERROR")
        event["_nonfaithful_reason"] = (
            f"gcd:source_wait_bridge_exception:{type(error).__name__}"
        )
        event["_blocked"] = True
        return event
    if not isinstance(after, Mapping):
        event["simulator_submission"] = {
            "status": "BRIDGE_ERROR_FAIL_CLOSED",
            "wait_ms": decision.wait_ms,
            "error_type": "UNTYPED_WAIT_RESULT",
        }
        event["client_acceptance"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
        event["decision_consumption"] = {
            "status": "UNKNOWN_BRIDGE_ERROR",
            "consumes_decision": None,
        }
        event["server_result"] = _server_result("UNKNOWN_BRIDGE_ERROR")
        event["_nonfaithful_reason"] = "gcd:source_wait_untyped_bridge_result"
        event["_blocked"] = True
        return event
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "wait_ms": decision.wait_ms,
    }
    event["client_acceptance"] = {
        "status": "ACCEPTED",
        "evidence": "bridge.wait_returned_state",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED_BY_EXPLICIT_SOURCE_WAIT",
        "consumes_decision": True,
    }
    event["simulator_state_after_immediate"] = dict(after)
    event["_state"] = dict(after)
    return event


def _base_event(
    index: int,
    item: _ResolvedSink,
    current: Mapping[str, Any],
    source_traversal: str,
) -> dict[str, Any]:
    return {
        "order": index,
        "source_sink": item.sink.to_dict(),
        "source_attempt": {
            "status": "ATTEMPTED",
            "evidence": "ExpertDecision.raw_sink_order",
        },
        "operation_contract": {
            "recognized": item.recognized,
            "canonical_action": item.action_key,
            "action_ref": (
                item.action_ref.to_wire() if item.action_ref is not None else None
            ),
        },
        "simulator_state_before": dict(current),
        "simulator_submission": {"status": "NOT_EVALUATED"},
        "client_acceptance": {"status": "NOT_EVALUATED"},
        "decision_consumption": {
            "status": "NOT_EVALUATED",
            "consumes_decision": None,
        },
        "queue_transition": {
            "kind": "NOT_A_QUEUE_OPERATION",
            "before": _queued_swing(current).value,
            "requested": item.queue.value if item.queue is not None else None,
            "after": _queued_swing(current).value,
        },
        "traversal": {
            "source_sequence": source_traversal,
            "acceptance_does_not_rewrite_source_traversal": True,
            "executor_state": "CONTINUE",
        },
        "server_result": _server_result("NOT_EVALUATED"),
    }


def _available_action(row: AvailableAction) -> dict[str, Any]:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
    }


def _server_result(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "damage_or_miss_result": None,
        "evidence": "NOT_EXPOSED_BY_IMMEDIATE_O2OBRIDGE_COMMAND",
    }


def _queue_transition(
    before: SwingQueueOp,
    requested: SwingQueueOp,
    after: SwingQueueOp,
    *,
    accepted: bool,
) -> dict[str, Any]:
    if not accepted:
        kind = "REJECTED_UNCHANGED" if after is before else "REJECTED_STATE_CHANGED"
    elif before is requested and after is requested:
        kind = "ACCEPTED_ALREADY_ACTIVE"
    elif before is SwingQueueOp.KEEP and after is requested:
        kind = "QUEUED"
    elif before is not requested and after is requested:
        kind = "REPLACED"
    else:
        kind = "ACCEPTED_QUEUE_STATE_NOT_OBSERVED"
    return {
        "kind": kind,
        "before": before.value,
        "requested": requested.value,
        "after": after.value,
        "cancel_requested": False,
    }


def _queued_swing(state: Mapping[str, Any]) -> SwingQueueOp:
    auras = state.get("auras")
    if not isinstance(auras, list):
        return SwingQueueOp.KEEP
    for aura in auras:
        if not isinstance(aura, Mapping):
            continue
        action = aura.get("action")
        if not isinstance(action, Mapping) or action.get("tag") != 1:
            continue
        spell_id = action.get("spell_id")
        if spell_id in {11567, 25286}:
            return SwingQueueOp.HEROIC_STRIKE
        if spell_id == 20569:
            return SwingQueueOp.CLEAVE
    return SwingQueueOp.KEEP


def _is_casting(state: Mapping[str, Any]) -> bool:
    current_cast = state.get("current_cast")
    if not isinstance(current_cast, Mapping):
        return False
    remaining = current_cast.get("remaining_ms")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        return remaining > 0
    return bool(current_cast)


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = (
    "CAT_EXPERT_ID",
    "CONTRA_EXPERT_IDS",
    "ControlSinkResultV2",
    "EXECUTION_SCHEMA",
    "OrderedSinkBridgeLikeV2",
    "OrderedSinkExecutionError",
    "SUPPORTED_OPERATION_COVERAGE_V2",
    "UNSUPPORTED_OPERATION_COVERAGE_V2",
    "execute_ordered_sinks_v2",
)
