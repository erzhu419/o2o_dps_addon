"""Deployed-Contra no-op and delayed-queue semantics over frozen executor v2.

V2 remains byte-frozen by the published rollout protocol.  This overlay keeps
its source-order execution and changes only observations that the real bridge
exposed: an already active stance is idempotent, an illegal
``QueueSpellByName`` call is an expected no-op when cooldown queueing is off,
the exact large-nonboss Bloodthirst/Slam source branches retry while below
their resource cost, and an accepted next-swing queue may become visible only
after the simulator's 10 ms realism ICD.  If every GCD call in one Contra
invocation is such a no-op, the existing declared 100 ms retry cadence advances
the simulator explicitly.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Mapping, Sequence

from . import fury_ordered_sink_executor_v2 as _v2
from .expert_policy import ExpertDecision, StanceOp, WAIT_ACTION
from .expert_proposals import ACTION_KEY_TO_REF
from .sim_bridge import ActResult, AvailableAction


EXECUTION_SCHEMA_V3 = "fury_ordered_sink_execution/v3"
_EXPECTED_NOOP_STATUSES = frozenset(
    {
        "REJECTED_SOURCE_DECLARED_NOOP",
        "REJECTED_SOURCE_CONFIGURED_NOOP",
        "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
    }
)
_STANCE_REFS = {
    StanceOp.BATTLE: ACTION_KEY_TO_REF["warrior.battle_stance"],
    StanceOp.DEFENSIVE: ACTION_KEY_TO_REF["warrior.defensive_stance"],
    StanceOp.BERSERKER: ACTION_KEY_TO_REF["warrior.berserker_stance"],
}
_SOURCE_RESOURCE_RETRY_BRANCHES = {
    ("warrior.bloodthirst", "Contra_ALL.lua:31672", "QueueSpellByName"): 30.0,
    ("warrior.slam", "Contra_ALL.lua:31677", "CastSpellByName"): 15.0,
}


class _StateSatisfiedStanceFacade:
    """Suppress only an idempotent deployed-Contra stance submission."""

    def __init__(
        self,
        bridge: Any,
        decision: ExpertDecision,
        state: Mapping[str, Any],
    ) -> None:
        self._bridge = bridge
        self._decision = decision
        self._state = dict(state)
        self.intercepted_stance_refs: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def actions(self) -> list[AvailableAction]:
        return self._bridge.actions()

    def act(self, action: Any, *, attempt_id: str | None = None) -> ActResult:
        active = _current_stance(self._state)
        if (
            self._decision.expert_id in _v2.CONTRA_EXPERT_IDS
            and _STANCE_REFS.get(active) == action
        ):
            self.intercepted_stance_refs.append(action)
            return ActResult(
                casted=False,
                consumes_decision=False,
                finished=bool(self._state.get("finished", False)),
                needs_input=bool(self._state.get("needs_input", True)),
                state=dict(self._state),
            )
        result = (
            self._bridge.act(action, attempt_id=attempt_id)
            if attempt_id is not None
            else self._bridge.act(action)
        )
        if isinstance(result, ActResult):
            self._state = dict(result.state)
        return result

    def wait(self, wait_ms: int) -> Mapping[str, Any]:
        state = self._bridge.wait(wait_ms)
        if isinstance(state, Mapping):
            self._state = dict(state)
        return state

    def start_attack(self) -> Any:
        result = self._bridge.start_attack()
        state = getattr(result, "state", None)
        if isinstance(state, Mapping):
            self._state = dict(state)
        return result

    def stop_cast(self) -> Any:
        result = self._bridge.stop_cast()
        state = getattr(result, "state", None)
        if isinstance(state, Mapping):
            self._state = dict(state)
        return result


def execute_ordered_sinks_v3(
    bridge: Any,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Run frozen v2, then adjudicate the three observed Contra semantics."""

    facade = _StateSatisfiedStanceFacade(bridge, decision, state)
    result = _v2.execute_ordered_sinks_v2(
        facade,
        decision,
        state,
        attempt_id_prefix=attempt_id_prefix,
        result_bearing_action_keys=result_bearing_action_keys,
    )
    result["schema"] = EXECUTION_SCHEMA_V3
    result["base_executor_schema"] = _v2.EXECUTION_SCHEMA
    events = result.get("sink_events")
    if not isinstance(events, list):
        return result

    reasons = list(result.get("nonfaithful_reasons", ()))
    intercepted = list(facade.intercepted_stance_refs)
    for event in events:
        if not isinstance(event, dict):
            continue
        order = event.get("order")
        source = event.get("source_sink")
        operation = event.get("operation_contract")
        action_ref = operation.get("action_ref") if isinstance(operation, Mapping) else None
        if (
            intercepted
            and isinstance(source, Mapping)
            and source.get("channel") == "stance"
            and isinstance(action_ref, Mapping)
            and action_ref == intercepted[0].to_wire()
        ):
            intercepted.pop(0)
            event["simulator_submission"] = {
                "status": "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
                "reason": "requested_stance_is_active",
            }
            event["client_acceptance"] = {
                "status": "NOT_APPLICABLE_STATE_ALREADY_SATISFIED"
            }
            event["decision_consumption"] = {
                "status": "NOT_CONSUMED",
                "consumes_decision": False,
                "expected_for_lane": False,
            }
            event.pop("simulator_state_after_immediate", None)
            event["server_result"] = _v2._server_result(
                "NOT_APPLICABLE_CLIENT_NOOP"
            )
            reasons = _without_reason(
                reasons, f"raw_sink[{order}]:unclassified_client_rejection"
            )

        acceptance = event.get("client_acceptance")
        available = event.get("simulator_submission")
        available_action = (
            available.get("available_action")
            if isinstance(available, Mapping)
            else None
        )
        if (
            isinstance(source, Mapping)
            and source.get("channel") == "gcd"
            and source.get("operation") == "QueueSpellByName"
            and isinstance(acceptance, dict)
            and acceptance.get("status") == "REJECTED_UNCLASSIFIED"
            and isinstance(available_action, Mapping)
            and available_action.get("legal") is False
            and isinstance(available_action.get("ready_in_ms"), int)
            and not isinstance(available_action.get("ready_in_ms"), bool)
            and available_action.get("ready_in_ms") > 0
            and decision.expert_id in _v2.CONTRA_EXPERT_IDS
            and decision.metadata.get("nampower_queue_spells_on_cooldown") is False
        ):
            acceptance["status"] = "REJECTED_SOURCE_CONFIGURED_NOOP"
            acceptance["source_rejection_classification"] = (
                "KNOWN_SOURCE_CONFIGURATION_NOOP"
            )
            acceptance["evidence"] = (
                "AvailableAction.ready_in_ms>0;NP_QueueSpellsOnCooldown=0"
            )
            reasons = _without_reason(
                reasons, f"raw_sink[{order}]:unclassified_client_rejection"
            )

        if _is_source_resource_retry_noop(
            decision,
            source=source,
            acceptance=acceptance,
            available_action=available_action,
            event=event,
        ):
            acceptance["status"] = "REJECTED_SOURCE_RESOURCE_RETRY_NOOP"
            acceptance["source_rejection_classification"] = (
                "EXACT_SOURCE_BRANCH_RESOURCE_RETRY_NOOP"
            )
            acceptance["evidence"] = "exact source/action/config branch;rage below cost"
            reasons = _without_reason(
                reasons, f"raw_sink[{order}]:unclassified_client_rejection"
            )

        transition = event.get("queue_transition")
        if (
            isinstance(transition, dict)
            and transition.get("kind") == "ACCEPTED_QUEUE_STATE_NOT_OBSERVED"
        ):
            transition["kind"] = "ACCEPTED_PENDING_ACTIVATION"
            transition["observation_boundary"] = (
                "IMMEDIATE_STATE_BEFORE_10MS_REALISM_ICD"
            )
            reasons = _without_reason(
                reasons,
                f"raw_sink[{order}]:queue_transition:"
                "ACCEPTED_QUEUE_STATE_NOT_OBSERVED",
            )

    result["nonfaithful_reasons"] = reasons
    result["ordered_projection_faithful"] = not reasons
    if (
        result.get("execution_blocked") is False
        and result.get("decision_consumed") is False
        and _all_gcd_attempts_are_expected_noops(events)
    ):
        _apply_declared_retry_wait(facade, decision, result)
    result["v3_semantics"] = {
        "active_stance_is_idempotent": True,
        "configured_queue_rejection_is_source_noop": True,
        "accepted_queue_activation_observation": "DEFERRED_TO_SUBSEQUENT_ADVANCE",
        "retry_wait_source": "decision.metadata.known_noop_retry_wait_ms",
        "fallback_used": False,
    }
    return result


def _apply_declared_retry_wait(
    bridge: _StateSatisfiedStanceFacade,
    decision: ExpertDecision,
    result: dict[str, Any],
) -> None:
    retry_ms = decision.metadata.get("known_noop_retry_wait_ms")
    current = result.get("final_state")
    if (
        isinstance(retry_ms, bool)
        or not isinstance(retry_ms, int)
        or retry_ms <= 0
        or not isinstance(current, Mapping)
        or not bool(current.get("needs_input", True))
    ):
        return
    retry = replace(decision, gcd=WAIT_ACTION, wait_ms=retry_ms)
    event = _v2._execute_source_wait(
        bridge,
        retry,
        dict(current),
        blocked=False,
        consumed=False,
    )
    final_state = dict(event.pop("_state"))
    reason = event.pop("_nonfaithful_reason")
    blocked = event.pop("_blocked")
    event["kind"] = "SOURCE_NOOP_RETRY_WAIT"
    event["source_decision"] = decision.gcd
    event["retry_reason"] = "all_contra_gcd_sinks_were_expected_noops"
    consumption = event.get("decision_consumption")
    if isinstance(consumption, dict) and consumption.get("consumes_decision") is True:
        consumption["status"] = "CONSUMED_BY_SOURCE_NOOP_RETRY_WAIT"
        result["decision_consumed"] = True
    if reason is not None:
        result["nonfaithful_reasons"].append(reason)
    result["ordered_projection_faithful"] = not result["nonfaithful_reasons"]
    result["execution_blocked"] = bool(result["execution_blocked"] or blocked)
    result["wait_event"] = event
    result["final_state"] = final_state


def _all_gcd_attempts_are_expected_noops(
    events: Sequence[Mapping[str, Any]],
) -> bool:
    statuses: list[str | None] = []
    for event in events:
        source = event.get("source_sink")
        if not isinstance(source, Mapping) or source.get("channel") != "gcd":
            continue
        acceptance = event.get("client_acceptance")
        statuses.append(
            acceptance.get("status") if isinstance(acceptance, Mapping) else None
        )
    return bool(statuses) and all(status in _EXPECTED_NOOP_STATUSES for status in statuses)


def _is_source_resource_retry_noop(
    decision: ExpertDecision,
    *,
    source: Any,
    acceptance: Any,
    available_action: Any,
    event: Mapping[str, Any],
) -> bool:
    if (
        decision.expert_id not in _v2.CONTRA_EXPERT_IDS
        or decision.metadata.get("saved_xuanfeng") is not False
        or not isinstance(source, Mapping)
        or source.get("channel") != "gcd"
        or not isinstance(acceptance, Mapping)
        or acceptance.get("status") != "REJECTED_UNCLASSIFIED"
        or not isinstance(available_action, Mapping)
        or available_action.get("legal") is not False
        or available_action.get("ready_in_ms") != 0
    ):
        return False
    operation = event.get("operation_contract")
    if not isinstance(operation, Mapping):
        return False
    action_key = operation.get("canonical_action")
    branch = (
        action_key,
        source.get("source_ref"),
        source.get("operation"),
    )
    cost = _SOURCE_RESOURCE_RETRY_BRANCHES.get(branch)
    if cost is None:
        return False
    if (
        action_key == "warrior.bloodthirst"
        and decision.metadata.get("nampower_queue_spells_on_cooldown") is not False
    ):
        return False
    before = event.get("simulator_state_before")
    power = before.get("power") if isinstance(before, Mapping) else None
    rage = power.get("current") if isinstance(power, Mapping) else None
    return (
        not isinstance(rage, bool)
        and isinstance(rage, (int, float))
        and 0.0 <= float(rage) < cost
    )


def _without_reason(reasons: list[str], rejected: str) -> list[str]:
    return [reason for reason in reasons if reason != rejected]


def _current_stance(state: Mapping[str, Any]) -> StanceOp:
    auras = state.get("auras")
    if isinstance(auras, list):
        for aura in auras:
            if not isinstance(aura, Mapping):
                continue
            label = str(aura.get("label", "")).casefold()
            action = aura.get("action")
            spell_id = action.get("spell_id") if isinstance(action, Mapping) else None
            if spell_id == 2457 or label in {"battle stance", "战斗姿态"}:
                return StanceOp.BATTLE
            if spell_id == 71 or label in {"defensive stance", "防御姿态"}:
                return StanceOp.DEFENSIVE
            if spell_id == 2458 or label in {"berserker stance", "狂暴姿态"}:
                return StanceOp.BERSERKER
    return StanceOp.KEEP


def audit_ordered_execution_v3(
    execution: Mapping[str, Any],
    proposal: ExpertDecision,
    *,
    decision_index: int,
) -> list[dict[str, Any]]:
    """Reuse the frozen independent audit after a lossless v3 projection."""

    projected = deepcopy(dict(execution))
    for event in projected.get("sink_events", ()):
        if not isinstance(event, dict):
            continue
        acceptance = event.get("client_acceptance")
        if isinstance(acceptance, dict):
            if acceptance.get("status") == "NOT_APPLICABLE_STATE_ALREADY_SATISFIED":
                acceptance["status"] = "NOT_APPLICABLE_NO_ACTIVE_CAST"
            elif acceptance.get("status") in {
                "REJECTED_SOURCE_CONFIGURED_NOOP",
                "REJECTED_SOURCE_RESOURCE_RETRY_NOOP",
            }:
                acceptance["status"] = "REJECTED_SOURCE_DECLARED_NOOP"
        transition = event.get("queue_transition")
        if isinstance(transition, dict) and (
            transition.get("kind") == "ACCEPTED_PENDING_ACTIVATION"
        ):
            transition["kind"] = "QUEUED"
    wait_event = execution.get("wait_event")
    retry_wait = (
        isinstance(wait_event, Mapping)
        and wait_event.get("kind") == "SOURCE_NOOP_RETRY_WAIT"
    )
    if retry_wait and proposal.gcd != WAIT_ACTION:
        projected["wait_event"] = None
    blockers = _rollout_v2()._audit_ordered_execution(
        projected,
        proposal,
        decision_index=decision_index,
    )
    if retry_wait and not _valid_retry_wait(wait_event, proposal):
        blockers.append(
            _rollout_v2()._blocker(
                "ORDERED_TRACE_SOURCE_NOOP_RETRY_WAIT_INVALID",
                "Contra no-op retry wait differs from its declared cadence",
                execution_fatal=True,
                decision_index=decision_index,
            )
        )
    return blockers


def _valid_retry_wait(event: Mapping[str, Any], proposal: ExpertDecision) -> bool:
    retry_ms = proposal.metadata.get("known_noop_retry_wait_ms")
    submission = event.get("simulator_submission")
    acceptance = event.get("client_acceptance")
    consumption = event.get("decision_consumption")
    return (
        isinstance(retry_ms, int)
        and not isinstance(retry_ms, bool)
        and retry_ms > 0
        and event.get("requested_wait_ms") == retry_ms
        and isinstance(submission, Mapping)
        and submission.get("status") == "SUBMITTED"
        and isinstance(acceptance, Mapping)
        and acceptance.get("status") == "ACCEPTED"
        and isinstance(consumption, Mapping)
        and consumption.get("status") == "CONSUMED_BY_SOURCE_NOOP_RETRY_WAIT"
        and consumption.get("consumes_decision") is True
    )


def _rollout_v2() -> Any:
    # Lazy import avoids the executor/rollout import cycle.
    from . import fury_full_policy_rollout_v2

    return fury_full_policy_rollout_v2


__all__ = (
    "EXECUTION_SCHEMA_V3",
    "audit_ordered_execution_v3",
    "execute_ordered_sinks_v3",
)
