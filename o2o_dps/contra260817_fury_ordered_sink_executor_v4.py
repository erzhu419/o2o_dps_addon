"""Native ordered-sink executor for the Contra260817 Fury source oracle.

The v3 source oracle records the Lua call sequence, including calls which the
client would reject after an earlier GCD.  This additive executor submits that
sequence in order to one simulator bridge.  It never substitutes the deployed
Contra adapter and it never collapses the sequence to a scored action.

Target GUIDs, item names/slots, and equipment names are not simulator action
identities.  They therefore require explicit bindings.  The complete source
invocation is preflighted before the first mutating bridge call; a missing
binding, spell, or native equipment/control operation fails the invocation
closed instead of executing a prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contra260817_fury_full_policy_v3 import (
    BERSERKER_RAGE,
    BERSERKING,
    BLOOD_FURY,
    CONCUSSION_BLOW,
    DEMORALIZING_SHOUT,
    LAST_STAND,
    OVERPOWER,
    PERCEPTION,
    POLICY_ID,
    RECKLESSNESS,
    SHIELD_BASH,
    SHIELD_BLOCK,
    SHIELD_WALL,
    validate_source_decision_v3,
)
from .expert_policy import (
    CastControl,
    ExpertDecision,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    TargetOp,
    WAIT_ACTION,
)
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .fury_expert_adapters import (
    BATTLE_SHOUT,
    BLOODRAGE,
    BLOODTHIRST,
    DEATH_WISH,
    EXECUTE,
    HAMSTRING,
    PUMMEL,
    SLAM,
    SUNDER_ARMOR,
    WHIRLWIND,
)
from .sim_bridge import ActionRef, ActResult, AvailableAction


JSONMap = dict[str, Any]
EXECUTION_SCHEMA_V4 = "contra260817_fury_ordered_sink_simulator_execution/v4"
IMPLEMENTATION_REVISION = "v4.4_contra260817_dynamic_cleave_guard"
SOURCE_REENTRY_CLOCK_SCHEMA_V4 = "contra260817_source_reentry_clock/v4"
SOURCE_REENTRY_RETRY_MS_V4 = 100
SOURCE_REENTRY_TIMING_AUTHORITY_V4 = "RUNNER_FIXED_100MS_PROXY"
POST_GCD_REJECTION_SUBMISSION_STATUS_V4 = (
    "NOT_SUBMITTED_SIMULATOR_DECISION_ALREADY_CONSUMED"
)
POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4 = (
    "REJECTED_SIMULATOR_DECISION_ALREADY_CONSUMED"
)
POST_GCD_REJECTION_REASON_V4 = "EARLIER_ACCEPTED_GCD_CONSUMED_SIMULATOR_DECISION"


class Contra260817OrderedSinkExecutorV4Error(RuntimeError):
    """The Contra260817 ordered source/simulator contract was violated."""


@dataclass(frozen=True)
class Contra260817TargetBindingV4:
    operation: str
    value: str
    target_index: int

    def __post_init__(self) -> None:
        if self.operation not in {"TargetUnit", "TargetNearestEnemy"}:
            raise ValueError("unsupported target binding operation")
        if not isinstance(self.value, str) or not self.value:
            raise TypeError("target binding value must be nonempty")
        if type(self.target_index) is not int or self.target_index < 0:
            raise ValueError("target_index must be a nonnegative integer")


@dataclass(frozen=True)
class Contra260817ItemActionBindingV4:
    locator: str
    action: ActionRef

    def __post_init__(self) -> None:
        if not isinstance(self.locator, str) or not self.locator:
            raise TypeError("item locator must be nonempty")
        if not isinstance(self.action, ActionRef) or self.action.item_id <= 0:
            raise TypeError("item binding must use an item ActionRef")
        if self.action.spell_id or self.action.other_id:
            raise ValueError("item binding ActionRef may contain only item_id/tag")


@dataclass(frozen=True)
class _ResolvedSinkV4:
    sink: RawSink
    kind: str
    action_key: str | None = None
    action_ref: ActionRef | None = None
    queue: SwingQueueOp | None = None
    stance: StanceOp | None = None
    arguments: tuple[Any, ...] = ()
    known_noop: bool = False


_ACTION_KEY_BY_VALUE: Mapping[str, str] = {
    "战斗怒吼": BATTLE_SHOUT,
    "战斗姿态": "warrior.battle_stance",
    "防御姿态": "warrior.defensive_stance",
    "狂暴姿态": "warrior.berserker_stance",
    "血性狂暴": BLOODRAGE,
    "嗜血": BLOODTHIRST,
    "斩杀": EXECUTE,
    "断筋": HAMSTRING,
    "拳击": PUMMEL,
    "盾击": SHIELD_BASH,
    "猛击": SLAM,
    "破甲攻击": SUNDER_ARMOR,
    "旋风斩": WHIRLWIND,
    "压制": OVERPOWER,
    "狂暴之怒": BERSERKER_RAGE,
    "震荡猛击": CONCUSSION_BLOW,
    "挫志怒吼": DEMORALIZING_SHOUT,
    "鲁莽": RECKLESSNESS,
    "死亡之愿": DEATH_WISH,
    "盾墙": SHIELD_WALL,
    "破釜沉舟": LAST_STAND,
    "盾牌格挡": SHIELD_BLOCK,
    "感知": PERCEPTION,
    "血性狂怒": BLOOD_FURY,
    "狂暴": BERSERKING,
}

# IDs are taken from the local wowsims-turtle implementation/database.  An ID
# does not imply that a particular race/talent/load has the action: preflight
# also requires it in bridge.actions().  Human Perception is not registered by
# the current simulator, so a reached source attempt fails closed as intended.
_ACTION_REFS: Mapping[str, ActionRef] = {
    **ACTION_KEY_TO_REF,
    OVERPOWER: ActionRef(spell_id=11585),
    BERSERKER_RAGE: ActionRef(spell_id=18499),
    CONCUSSION_BLOW: ActionRef(spell_id=12809),
    DEMORALIZING_SHOUT: ActionRef(spell_id=11556),
    RECKLESSNESS: ActionRef(spell_id=1719),
    SHIELD_BASH: ActionRef(spell_id=1672),
    SHIELD_WALL: ActionRef(spell_id=871),
    LAST_STAND: ActionRef(spell_id=12975),
    SHIELD_BLOCK: ActionRef(spell_id=2565),
    PERCEPTION: ActionRef(spell_id=20600),
    BLOOD_FURY: ActionRef(spell_id=20572),
    BERSERKING: ActionRef(spell_id=26297),
}
_QUEUE_BY_VALUE = {
    "英勇打击": SwingQueueOp.HEROIC_STRIKE,
    "顺劈斩": SwingQueueOp.CLEAVE,
}
_STANCE_BY_VALUE = {
    "战斗姿态": StanceOp.BATTLE,
    "防御姿态": StanceOp.DEFENSIVE,
    "狂暴姿态": StanceOp.BERSERKER,
}
SUPPORTED_OPERATION_COVERAGE_V4 = frozenset(
    {
        ("target", "TargetUnit"),
        ("target", "TargetNearestEnemy"),
        ("autoattack", "UseAction"),
        ("autoattack", "AttackTarget"),
        ("cast_control", "SpellStopCasting"),
        ("gcd", "CastSpellByName"),
        ("off_gcd", "CastSpellByName"),
        ("swing_queue", "CastSpellByName"),
        ("item", "UseInventoryItem"),
        ("item", "Contra.UseItemByName"),
        ("equipment", "Contra.EquipItemByName"),
        ("stance", "ContraZSCast"),
        ("stance", "CastSpellByName"),
    }
)


class Contra260817SimulatorControlFacadeV4:
    """Bind non-spell Contra source locators to exact native operations."""

    def __init__(
        self,
        bridge: Any,
        *,
        target_bindings: Sequence[Contra260817TargetBindingV4] = (),
        item_bindings: Sequence[Contra260817ItemActionBindingV4] = (),
        initial_autoattack_active: bool = False,
        initial_equipment: Mapping[int, str | None] | None = None,
    ) -> None:
        if not isinstance(initial_autoattack_active, bool):
            raise TypeError("initial_autoattack_active must be boolean")
        targets: dict[tuple[str, str], int] = {}
        for binding in target_bindings:
            if not isinstance(binding, Contra260817TargetBindingV4):
                raise TypeError("target_bindings must contain v4 target bindings")
            key = (binding.operation, binding.value)
            if key in targets:
                raise ValueError(f"duplicate target binding {key!r}")
            targets[key] = binding.target_index
        items: dict[str, ActionRef] = {}
        for binding in item_bindings:
            if not isinstance(binding, Contra260817ItemActionBindingV4):
                raise TypeError("item_bindings must contain v4 item bindings")
            if binding.locator in items:
                raise ValueError(f"duplicate item binding {binding.locator!r}")
            items[binding.locator] = binding.action
        self._bridge = bridge
        self._targets = targets
        self._items = items
        self._initial_autoattack_active = initial_autoattack_active
        self._autoattack_active = initial_autoattack_active
        equipment = {16: None, 17: None}
        if initial_equipment is not None:
            if set(initial_equipment) - {16, 17} or any(
                value is not None and not isinstance(value, str)
                for value in initial_equipment.values()
            ):
                raise ValueError("initial_equipment accepts only string slots 16/17")
            equipment.update(initial_equipment)
        self._initial_equipment = equipment
        self._equipment = dict(equipment)
        self._receipts: list[JSONMap] = []

    def reset_receipts(self) -> None:
        self._receipts = []
        self._autoattack_active = self._initial_autoattack_active
        self._equipment = dict(self._initial_equipment)

    def control_receipt(self) -> JSONMap:
        return {
            "schema": "contra260817_simulator_control_receipt/v4",
            "events": [dict(row) for row in self._receipts],
            "target_binding_count": len(self._targets),
            "item_binding_count": len(self._items),
            "autoattack_active": self._autoattack_active,
            "equipment": {str(slot): name for slot, name in sorted(self._equipment.items())},
            "game_client_observed": False,
            "game_server_outcome_observed": False,
        }

    def load_dynamic_v3(self, request: Mapping[str, Any], seed: int, config: Any) -> Any:
        self.reset_receipts()
        return self._bridge.load_dynamic_v3(request, seed, config)

    def target_index(self, operation: str, value: str) -> int | None:
        return self._targets.get((operation, value))

    def item_action(self, locator: str) -> ActionRef | None:
        return self._items.get(locator)

    def has_native_capability(self, name: str) -> bool:
        return callable(getattr(self._bridge, name, None))

    @property
    def autoattack_active(self) -> bool:
        return self._autoattack_active

    def equipped_name(self, slot: int) -> str | None:
        return self._equipment.get(slot)

    def start_attack(self) -> Any:
        result = self._bridge.start_attack()
        if getattr(result, "accepted", None) is True:
            self._autoattack_active = True
        return result

    def stop_attack(self) -> Any:
        result = self._bridge.stop_attack()
        if getattr(result, "accepted", None) is True:
            self._autoattack_active = False
        return result

    def toggle_attack(self) -> Any:
        result = self._bridge.toggle_attack()
        if getattr(result, "accepted", None) is True:
            self._autoattack_active = not self._autoattack_active
        return result

    def equip_item_by_name(self, name: str, slot: int) -> Any:
        result = self._bridge.equip_item_by_name(name, slot)
        if getattr(result, "accepted", None) is True:
            self._equipment[slot] = name
        return result

    def record_control(self, row: Mapping[str, Any]) -> None:
        self._receipts.append(dict(row))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def execute_contra260817_ordered_sinks_v4(
    bridge: Any,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
    external_press_clock: bool = False,
) -> JSONMap:
    """Preflight and submit one complete Contra source invocation in order."""

    if not isinstance(decision, ExpertDecision):
        raise TypeError("decision must be ExpertDecision")
    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    if attempt_id_prefix is not None and (
        not isinstance(attempt_id_prefix, str) or not attempt_id_prefix
    ):
        raise TypeError("attempt_id_prefix must be nonempty or None")
    if not isinstance(external_press_clock, bool):
        raise TypeError("external_press_clock must be boolean")
    resolved, reasons = _preflight(bridge, decision)
    current = dict(state)
    blocked = bool(reasons)
    consumed = not bool(current.get("needs_input", True))
    events: list[JSONMap] = []
    accepted_gcd: list[str] = []
    accepted_consuming_gcd: tuple[int, str] | None = None
    result_bearing = frozenset(result_bearing_action_keys)
    deferred_cleave_refs = {
        row.get("source_ref")
        for row in decision.metadata.get("deferred_queue_guard_checks", ())
        if isinstance(row, Mapping)
        and row.get("operation") == "Contra.IsHeroicStrikActive"
        and row.get("value") == "顺劈斩"
        and row.get("reason") == "IsCurrentAction_deferred_until_after_whirlwind"
    }

    for order, item in enumerate(resolved, start=1):
        event = _base_event(order, item, current, len(resolved))
        if blocked:
            _not_submitted(event, "NOT_SUBMITTED_FAIL_CLOSED")
            events.append(event)
            continue
        if (
            item.sink.channel == "swing_queue"
            and item.sink.value == "顺劈斩"
            and item.sink.source_ref in deferred_cleave_refs
        ):
            accepted_ww = any(
                row.get("source_sink", {}).get("channel") == "gcd"
                and row.get("source_sink", {}).get("value") == "旋风斩"
                and row.get("simulator_acceptance", {}).get("status") == "ACCEPTED"
                for row in events
            )
            event["source_attempt"]["queue_guard"] = (
                "ISCURRENTACTION_CLEARED_AFTER_ACCEPTED_WHIRLWIND"
                if accepted_ww
                else "ISCURRENTACTION_STILL_TRUE_AFTER_REJECTED_WHIRLWIND"
            )
            if not accepted_ww:
                _record_current_action_guard_return(event)
                events.append(event)
                continue
        if (
            accepted_consuming_gcd is not None
            and (
                item.sink.channel == "gcd"
                or (item.sink.channel == "swing_queue" and item.sink.value == "顺劈斩")
            )
            and item.sink.operation == "CastSpellByName"
            and not item.known_noop
        ):
            _record_post_gcd_rejection(
                event,
                consumed_by_order=accepted_consuming_gcd[0],
                consumed_by_action=accepted_consuming_gcd[1],
            )
            events.append(event)
            continue
        try:
            attempt_id = (
                f"{attempt_id_prefix}:sink-{order}"
                if attempt_id_prefix is not None
                and item.sink.channel == "gcd"
                and item.action_key in result_bearing
                and not item.known_noop
                else None
            )
            event, current, sink_consumed = _execute_one(
                bridge, item, current, event, attempt_id=attempt_id
            )
        except Exception as error:
            event["simulator_submission"] = {
                "status": "BRIDGE_ERROR_FAIL_CLOSED",
                "error_type": type(error).__name__,
                "error_message": str(error),
            }
            event["simulator_acceptance"] = {
                "status": "UNKNOWN_BRIDGE_ERROR",
                "evidence_scope": "SIMULATOR",
            }
            event["decision_consumption"] = {
                "status": "UNKNOWN_BRIDGE_ERROR",
                "consumes_decision": None,
            }
            event["simulator_outcome"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
            event["traversal"]["executor_state"] = "FAIL_CLOSED"
            reasons.append(f"raw_sink[{order}]:bridge_exception:{type(error).__name__}")
            sink_consumed = False
            blocked = True
        events.append(event)
        consumed = consumed or sink_consumed
        acceptance = event.get("simulator_acceptance")
        if (
            item.sink.channel == "gcd"
            and item.action_key
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
        ):
            accepted_gcd.append(item.action_key)
            if sink_consumed:
                accepted_consuming_gcd = (order, item.action_key)

    wait_event: JSONMap | None = None
    accepted_queue_reentry = _accepted_swing_queue_nonconsuming(events)
    if decision.gcd == WAIT_ACTION and not accepted_queue_reentry:
        wait_event, current, wait_consumed, wait_reason = _execute_wait(
            bridge, decision, current, blocked=blocked, consumed=consumed,
            external_press_clock=external_press_clock,
        )
        consumed = consumed or wait_consumed
        if wait_reason:
            reasons.append(wait_reason)
            blocked = True

    source_reentry_clock: JSONMap | None = None
    reentry_trigger = _source_reentry_trigger_v4(events)
    if (
        not external_press_clock
        and not blocked
        and not consumed
        and (decision.gcd != WAIT_ACTION or accepted_queue_reentry)
        and bool(current.get("needs_input", True))
        and reentry_trigger is not None
    ):
        scheduled_at = current.get("time_ms")
        if type(scheduled_at) is not int:
            raise Contra260817OrderedSinkExecutorV4Error(
                "source reentry clock requires integer simulator time_ms"
            )
        after = bridge.wait(SOURCE_REENTRY_RETRY_MS_V4)
        if not isinstance(after, Mapping):
            raise Contra260817OrderedSinkExecutorV4Error(
                "source reentry bridge.wait returned non-mapping"
            )
        after = dict(after)
        if after.get("time_ms") != scheduled_at or after.get("needs_input") is not False:
            raise Contra260817OrderedSinkExecutorV4Error(
                "source reentry bridge.wait must schedule without advancing time"
            )
        horizon_ms = None
        idle = current.get("dynamic_idle_advance")
        if isinstance(idle, Mapping) and type(idle.get("horizon_ms")) is int:
            horizon_ms = idle["horizon_ms"]
        nominal_wake = scheduled_at + SOURCE_REENTRY_RETRY_MS_V4
        source_reentry_clock = {
            "schema": SOURCE_REENTRY_CLOCK_SCHEMA_V4,
            "trigger": reentry_trigger,
            "timing_authority": SOURCE_REENTRY_TIMING_AUTHORITY_V4,
            "exact_client_cadence": False,
            "policy_action": False,
            "source_sink": False,
            "requested_ms": SOURCE_REENTRY_RETRY_MS_V4,
            "scheduled_at_time_ms": scheduled_at,
            "nominal_wake_time_ms": nominal_wake,
            "expected_next_boundary_time_ms": (
                min(nominal_wake, horizon_ms)
                if horizon_ms is not None
                else nominal_wake
            ),
        }
        current = after

    reasons = list(dict.fromkeys(reasons))
    all_disposed = all(
        isinstance(event.get("simulator_submission"), Mapping)
        and event["simulator_submission"].get("status")
        not in {"NOT_EVALUATED", "NOT_SUBMITTED_FAIL_CLOSED", "BRIDGE_ERROR_FAIL_CLOSED"}
        for event in events
    )
    return {
        "schema": EXECUTION_SCHEMA_V4,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "expert_id": decision.expert_id,
        "source_decision": decision.to_dict(),
        "raw_sink_order": [sink.to_dict() for sink in decision.raw_sink_order],
        "sink_events": events,
        "wait_event": wait_event,
        "source_reentry_clock": source_reentry_clock,
        "fallback": {"used": False, "reason": "CONTRA_V4_NEVER_SYNTHESIZES_FALLBACK"},
        "decision_consumed": consumed,
        "execution_blocked": blocked,
        "accepted_gcd_actions": accepted_gcd,
        "nonfaithful_reasons": reasons,
        "source_to_simulator_order_faithful": all_disposed and not reasons,
        "ordered_projection_faithful": False,
        "comparison_ready": False,
        "evidence_boundary": {
            "simulator_submission_observed": True,
            "simulator_acceptance_observed": True,
            "wow_client_observed": False,
            "game_server_outcome_observed": False,
        },
        "final_state": current,
    }


def _all_source_sinks_typed_rejected_nonconsuming(
    events: Sequence[Mapping[str, Any]],
) -> bool:
    if not events:
        return False
    seen_submitted = False
    for event in events:
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        consumption = event.get("decision_consumption")
        if (
            not isinstance(submission, Mapping)
            or submission.get("status")
            not in {"SUBMITTED", "NOT_SUBMITTED_SOURCE_DECLARED_NOOP"}
            or not isinstance(acceptance, Mapping)
            or acceptance.get("status")
            not in {"REJECTED", "REJECTED_SOURCE_DECLARED_NOOP"}
            or not isinstance(consumption, Mapping)
            or consumption.get("consumes_decision") is not False
        ):
            return False
        seen_submitted = seen_submitted or submission.get("status") == "SUBMITTED"
    return seen_submitted


def _accepted_swing_queue_nonconsuming(
    events: Sequence[Mapping[str, Any]],
) -> bool:
    """An accepted on-swing call leaves the source invocation input-open."""
    seen_queue = False
    for event in events:
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        consumption = event.get("decision_consumption")
        if (
            not isinstance(submission, Mapping)
            or submission.get("status")
            not in {"SUBMITTED", "NOT_SUBMITTED_SOURCE_DECLARED_NOOP"}
            or not isinstance(acceptance, Mapping)
            or acceptance.get("status")
            not in {"ACCEPTED", "REJECTED", "REJECTED_SOURCE_DECLARED_NOOP"}
            or not isinstance(consumption, Mapping)
            or consumption.get("consumes_decision") is not False
        ):
            return False
        source = event.get("source_sink")
        seen_queue = seen_queue or (
            isinstance(source, Mapping)
            and source.get("channel") == "swing_queue"
            and submission.get("status") == "SUBMITTED"
            and acceptance.get("status") == "ACCEPTED"
        )
    return seen_queue


def _source_reentry_trigger_v4(
    events: Sequence[Mapping[str, Any]],
) -> str | None:
    if _accepted_swing_queue_nonconsuming(events):
        return "ACCEPTED_SWING_QUEUE_NONCONSUMING"
    if _all_source_sinks_typed_rejected_nonconsuming(events):
        return "ALL_SOURCE_SINKS_TYPED_REJECTED_NONCONSUMING"
    return None


def _preflight(
    bridge: Any, decision: ExpertDecision
) -> tuple[list[_ResolvedSinkV4], list[str]]:
    reasons: list[str] = []
    try:
        validate_source_decision_v3(decision)
    except Exception as error:
        reasons.append(f"proposal:source_decision_invalid:{type(error).__name__}")
    if decision.expert_id != POLICY_ID:
        reasons.append(f"proposal:unsupported_policy_id:{decision.expert_id}")
    if not decision.valid:
        reasons.append("proposal:source_decision_invalid")
    if decision.provenance.role is not ExpertRole.CANDIDATE:
        reasons.append("proposal:expert_role_must_remain_candidate")
    if decision.provenance.kind is not ProvenanceKind.SOURCE_DERIVED:
        reasons.append("proposal:provenance_must_be_source_derived")
    if decision.eligible_for_independent_vote:
        reasons.append("proposal:independent_vote_forbidden")

    noop_rows = decision.metadata.get("known_noop_source_gcd_attempts")
    noop_keys = {
        (row.get("action"), row.get("source_ref"))
        for row in noop_rows
        if isinstance(row, Mapping)
    } if isinstance(noop_rows, list) else set()
    resolved: list[_ResolvedSinkV4] = []
    for index, sink in enumerate(decision.raw_sink_order, start=1):
        item, item_reasons = _resolve_sink(sink, index, noop_keys)
        resolved.append(item)
        reasons.extend(item_reasons)

    try:
        available_raw = bridge.actions()
        if not isinstance(available_raw, list) or any(
            not isinstance(row, AvailableAction) for row in available_raw
        ):
            raise TypeError("bridge.actions must return list[AvailableAction]")
        available = {row.action: row for row in available_raw}
    except Exception as error:
        available = {}
        reasons.append(f"preflight:actions_unavailable:{type(error).__name__}")

    for index, item in enumerate(resolved, start=1):
        sink = item.sink
        if item.kind == "action" and not item.known_noop:
            row = available.get(item.action_ref)
            if row is None:
                reasons.append(
                    f"raw_sink[{index}]:action_absent_from_simulator_spellbook:{item.action_key}"
                )
            else:
                expected_gcd = sink.channel == "gcd"
                if row.triggers_gcd is not expected_gcd:
                    reasons.append(
                        f"raw_sink[{index}]:gcd_contract_mismatch:{item.action_key}:"
                        f"simulator={str(row.triggers_gcd).lower()}:source_lane={str(expected_gcd).lower()}"
                    )
        elif item.kind == "target":
            if not callable(getattr(bridge, "set_target", None)):
                reasons.append(f"raw_sink[{index}]:native_set_target_absent")
            elif not callable(getattr(bridge, "target_index", None)) or (
                bridge.target_index(str(sink.operation), str(sink.value)) is None
            ):
                reasons.append(
                    f"raw_sink[{index}]:target_binding_missing:{sink.operation}:{sink.value}"
                )
        elif item.kind == "item":
            locator = str(item.arguments[0])
            action = (
                bridge.item_action(locator)
                if callable(getattr(bridge, "item_action", None))
                else None
            )
            if action is None:
                reasons.append(f"raw_sink[{index}]:item_binding_missing:{locator}")
            elif available.get(action) is None:
                reasons.append(f"raw_sink[{index}]:bound_item_action_absent:{locator}")
        elif item.kind == "equipment":
            has_native = getattr(bridge, "has_native_capability", None)
            if not callable(has_native) or not has_native("equip_item_by_name"):
                reasons.append(
                    f"raw_sink[{index}]:native_equipment_api_absent:"
                    f"equip_item_by_name:{sink.value}"
                )
        elif item.kind == "control":
            method = str(item.arguments[0])
            has_native = getattr(bridge, "has_native_capability", None)
            if (
                not method
                or not callable(has_native)
                or not has_native(method)
            ):
                reasons.append(f"raw_sink[{index}]:native_control_absent:{method}")
    reasons.extend(_normalized_lane_reasons(decision, resolved))
    return resolved, list(dict.fromkeys(reasons))


def _resolve_sink(
    sink: RawSink,
    index: int,
    noop_keys: set[tuple[Any, Any]],
) -> tuple[_ResolvedSinkV4, list[str]]:
    reasons: list[str] = []
    if not sink.source_ref:
        reasons.append(f"raw_sink[{index}]:source_ref_missing")
    if (sink.channel, sink.operation) not in SUPPORTED_OPERATION_COVERAGE_V4:
        reasons.append(
            f"raw_sink[{index}]:unsupported_operation:{sink.channel}/{sink.operation}"
        )
    if sink.channel == "target":
        return _ResolvedSinkV4(sink, "target"), reasons
    if sink.channel == "autoattack":
        if sink.operation == "UseAction" and sink.value == "START":
            method = "start_attack"
        elif sink.operation == "UseAction" and sink.value == "STOP":
            method = "stop_attack"
        elif sink.operation == "AttackTarget" and sink.value == "TOGGLE":
            method = "toggle_attack"
        else:
            method = ""
            reasons.append(f"raw_sink[{index}]:unsupported_autoattack_value")
        return _ResolvedSinkV4(sink, "control", arguments=(method,)), reasons
    if sink.channel == "cast_control":
        if sink.value is not None:
            reasons.append(f"raw_sink[{index}]:cast_control_value_must_be_null")
        return _ResolvedSinkV4(
            sink, "control", arguments=("stop_cast",)
        ), reasons
    if sink.channel == "item":
        locator = (
            f"inventory:{sink.value}"
            if sink.operation == "UseInventoryItem"
            else f"name:{sink.value}"
        )
        if sink.operation == "UseInventoryItem" and sink.value not in {"13", "14"}:
            reasons.append(f"raw_sink[{index}]:unsupported_inventory_slot")
        if not sink.value:
            reasons.append(f"raw_sink[{index}]:empty_item_locator")
        return _ResolvedSinkV4(sink, "item", arguments=(locator,)), reasons
    if sink.channel == "equipment":
        value = str(sink.value or "")
        name, separator, raw_slot = value.rpartition("@")
        try:
            slot = int(raw_slot)
        except ValueError:
            slot = -1
        if not separator or not name or slot not in {16, 17}:
            reasons.append(f"raw_sink[{index}]:invalid_equipment_locator:{value}")
        return _ResolvedSinkV4(
            sink, "equipment", arguments=(name, slot)
        ), reasons
    if sink.channel == "swing_queue":
        queue = _QUEUE_BY_VALUE.get(str(sink.value or ""))
        if queue is None:
            reasons.append(f"raw_sink[{index}]:unsupported_queue_value:{sink.value}")
            return _ResolvedSinkV4(sink, "action"), reasons
        return _ResolvedSinkV4(
            sink, "action", action_ref=QUEUE_REFS[queue], queue=queue
        ), reasons
    if sink.channel in {"gcd", "off_gcd", "stance"}:
        action_key = _ACTION_KEY_BY_VALUE.get(str(sink.value or ""))
        if action_key is None:
            reasons.append(f"raw_sink[{index}]:unsupported_action_value:{sink.value}")
            return _ResolvedSinkV4(sink, "action"), reasons
        stance = _STANCE_BY_VALUE.get(str(sink.value)) if sink.channel == "stance" else None
        known_noop = (action_key, sink.source_ref) in noop_keys
        return _ResolvedSinkV4(
            sink,
            "action",
            action_key=action_key,
            action_ref=_ACTION_REFS[action_key],
            stance=stance,
            known_noop=known_noop,
        ), reasons
    return _ResolvedSinkV4(sink, "unsupported"), reasons


def _normalized_lane_reasons(
    decision: ExpertDecision, resolved: Sequence[_ResolvedSinkV4]
) -> list[str]:
    reasons: list[str] = []
    raw_gcd = [row.action_key for row in resolved if row.sink.channel == "gcd"]
    state_effecting_gcd = [
        row.action_key
        for row in resolved
        if row.sink.channel == "gcd" and not row.known_noop
    ]
    if decision.metadata.get("raw_gcd_calls") != raw_gcd:
        reasons.append("proposal:metadata_raw_gcd_calls_mismatch")
    if decision.gcd == WAIT_ACTION:
        if state_effecting_gcd:
            reasons.append("proposal:wait_conflicts_with_state_effecting_raw_gcd")
    elif not state_effecting_gcd or state_effecting_gcd[-1] != decision.gcd:
        reasons.append("proposal:normalized_gcd_mismatch")
    raw_off = tuple(
        row.action_key for row in resolved if row.sink.channel == "off_gcd"
    )
    if raw_off != decision.off_gcd:
        reasons.append("proposal:normalized_off_gcd_mismatch")
    raw_queue = [row.queue for row in resolved if row.queue is not None]
    if raw_queue and raw_queue[-1] is not decision.swing_queue:
        reasons.append("proposal:normalized_swing_queue_mismatch")
    if not raw_queue and decision.swing_queue is not SwingQueueOp.KEEP:
        reasons.append("proposal:normalized_swing_queue_missing")
    raw_stance = [row.stance for row in resolved if row.stance is not None]
    if raw_stance and raw_stance[-1] is not decision.stance:
        reasons.append("proposal:normalized_stance_mismatch")
    if not raw_stance and decision.stance is not StanceOp.KEEP:
        reasons.append("proposal:normalized_stance_missing")
    has_stop = any(row.sink.channel == "cast_control" for row in resolved)
    if has_stop != (decision.cast_control is CastControl.STOP_CAST):
        reasons.append("proposal:normalized_cast_control_mismatch")
    has_target = any(row.sink.channel == "target" for row in resolved)
    if has_target != (decision.target is not TargetOp.KEEP):
        reasons.append("proposal:normalized_target_mismatch")
    return reasons


def _execute_one(
    bridge: Any,
    item: _ResolvedSinkV4,
    current: JSONMap,
    event: JSONMap,
    *,
    attempt_id: str | None,
) -> tuple[JSONMap, JSONMap, bool]:
    if item.known_noop:
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_SOURCE_DECLARED_NOOP",
            "reason": "level60_bloodthirst_fury_cannot_know_concussion_blow",
        }
        event["simulator_acceptance"] = {
            "status": "REJECTED_SOURCE_DECLARED_NOOP",
            "evidence_scope": "SOURCE_STATIC_CONFIGURATION",
        }
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
            "expected_for_lane": True,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE_REJECTED"}
        return event, current, False
    if item.kind == "action":
        return _execute_action(bridge, item, current, event, attempt_id=attempt_id)
    if item.kind == "target":
        target_index = bridge.target_index(item.sink.operation, item.sink.value)
        result = bridge.set_target(target_index)
        return _execute_structural_control(
            bridge, item, current, event, result, "set_target", [target_index]
        )
    if item.kind == "item":
        locator = str(item.arguments[0])
        action = bridge.item_action(locator)
        row = next((value for value in bridge.actions() if value.action == action), None)
        if row is None:
            raise Contra260817OrderedSinkExecutorV4Error("preflighted item disappeared")
        result = bridge.act(action)
        if not isinstance(result, ActResult):
            raise Contra260817OrderedSinkExecutorV4Error("item act returned untyped result")
        event["operation_contract"]["action_ref"] = action.to_wire()
        event["operation_contract"]["canonical_action"] = locator
        return _finish_action(event, current, result, row, expected_gcd=row.triggers_gcd)
    if item.kind == "equipment":
        name, slot = item.arguments
        result = bridge.equip_item_by_name(name, slot)
        return _execute_structural_control(
            bridge, item, current, event, result, "equip_item_by_name", [name, slot]
        )
    if item.kind == "control":
        method_name = str(item.arguments[0])
        result = getattr(bridge, method_name)()
        return _execute_structural_control(
            bridge, item, current, event, result, method_name, []
        )
    raise Contra260817OrderedSinkExecutorV4Error("unsupported resolved sink")


def _execute_action(
    bridge: Any,
    item: _ResolvedSinkV4,
    current: JSONMap,
    event: JSONMap,
    *,
    attempt_id: str | None,
) -> tuple[JSONMap, JSONMap, bool]:
    if item.action_ref is None:
        raise Contra260817OrderedSinkExecutorV4Error("resolved action missing")
    row = next((value for value in bridge.actions() if value.action == item.action_ref), None)
    if row is None:
        raise Contra260817OrderedSinkExecutorV4Error("preflighted action disappeared")
    result = (
        bridge.act(item.action_ref, attempt_id=attempt_id)
        if attempt_id is not None
        else bridge.act(item.action_ref)
    )
    if not isinstance(result, ActResult):
        raise Contra260817OrderedSinkExecutorV4Error("bridge.act returned untyped result")
    return _finish_action(
        event,
        current,
        result,
        row,
        expected_gcd=item.sink.channel == "gcd",
    )


def _finish_action(
    event: JSONMap,
    current: JSONMap,
    result: ActResult,
    row: AvailableAction,
    *,
    expected_gcd: bool,
) -> tuple[JSONMap, JSONMap, bool]:
    after = dict(result.state)
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "action": row.action.to_wire(),
        "available_action": _available_action(row),
    }
    event["simulator_acceptance"] = {
        "status": "ACCEPTED" if result.casted else "REJECTED",
        "evidence": "bridge.act.casted",
        "evidence_scope": "SIMULATOR",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
        "consumes_decision": result.consumes_decision,
        "expected_for_lane": expected_gcd,
        "available_action_triggers_gcd": row.triggers_gcd,
    }
    event["simulator_state_after_immediate"] = after
    event["simulator_outcome"] = {
        "status": (
            "PENDING_TYPED_SIMULATOR_RESULT"
            if result.casted and expected_gcd
            else "ACCEPTED_NO_IMMEDIATE_DAMAGE_RESULT"
            if result.casted
            else "NOT_APPLICABLE_REJECTED"
        ),
        "acceptance_is_not_outcome": True,
    }
    return event, after, result.consumes_decision


def _execute_structural_control(
    bridge: Any,
    item: _ResolvedSinkV4,
    current: JSONMap,
    event: JSONMap,
    result: Any,
    method_name: str,
    arguments: list[Any],
) -> tuple[JSONMap, JSONMap, bool]:
    accepted = getattr(result, "accepted", None)
    if accepted is None and method_name == "set_target":
        accepted = isinstance(getattr(result, "changed", None), bool)
    consumed = getattr(result, "consumes_decision", None)
    if consumed is None and method_name == "set_target":
        consumed = False
    raw_state = getattr(result, "state", None)
    if not isinstance(accepted, bool) or not isinstance(consumed, bool) or not isinstance(raw_state, Mapping):
        raise Contra260817OrderedSinkExecutorV4Error(
            f"bridge.{method_name} returned an untyped control result"
        )
    after = dict(raw_state)
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "control": method_name,
        "arguments": arguments,
        "effect_scope": "NATIVE_SIMULATOR_CONTROL",
    }
    event["simulator_acceptance"] = {
        "status": "ACCEPTED" if accepted else "REJECTED",
        "evidence": f"bridge.{method_name}",
        "evidence_scope": "SIMULATOR",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if consumed else "NOT_CONSUMED",
        "consumes_decision": consumed,
        "expected_for_lane": False,
    }
    event["simulator_state_after_immediate"] = after
    event["simulator_outcome"] = {
        "status": "CONTROL_ACCEPTED" if accepted else "CONTROL_REJECTED",
        "combat_mechanics_modeled": True,
        "effect_scope": "NATIVE_SIMULATOR_CONTROL",
    }
    record = getattr(bridge, "record_control", None)
    if callable(record):
        record(
            {
                "order": event["order"],
                "channel": item.sink.channel,
                "operation": item.sink.operation,
                "method": method_name,
                "arguments": arguments,
                "accepted": accepted,
            }
        )
    return event, after, consumed


def _execute_wait(
    bridge: Any,
    decision: ExpertDecision,
    current: JSONMap,
    *,
    blocked: bool,
    consumed: bool,
    external_press_clock: bool = False,
) -> tuple[JSONMap, JSONMap, bool, str | None]:
    event: JSONMap = {
        "kind": "SOURCE_WAIT_DECISION",
        "requested_wait_ms": decision.wait_ms,
        "source_decision": WAIT_ACTION,
        "fallback": False,
        "client_observation": _client_not_observed(),
        "server_outcome": _server_not_observed(),
    }
    if blocked:
        _not_submitted(event, "NOT_SUBMITTED_FAIL_CLOSED")
        return event, current, False, "gcd:wait_blocked_by_preflight"
    if consumed or not bool(current.get("needs_input", True)):
        _not_submitted(event, "NOT_SUBMITTED_DECISION_ALREADY_CONSUMED")
        return event, current, False, None
    if decision.wait_ms is None or decision.wait_ms <= 0:
        _not_submitted(event, "INVALID_SOURCE_WAIT")
        return event, current, False, "gcd:invalid_source_wait"
    if external_press_clock:
        event["external_press_disposition"] = "ABSTAINED_NO_EXTRA_KEY"
        event["simulator_submission"] = {"status": "NOT_SUBMITTED_EXTERNAL_PRESS_CLOCK"}
        event["simulator_acceptance"] = {"status": "NOT_APPLICABLE"}
        event["decision_consumption"] = {
            "status": "PHYSICAL_KEY_CLOSED_BY_FINISH_PRESS",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "NO_POLICY_WAIT_SCHEDULED"}
        return event, current, False, None
    after = bridge.wait(decision.wait_ms)
    if not isinstance(after, Mapping):
        raise Contra260817OrderedSinkExecutorV4Error("bridge.wait returned non-mapping")
    event["simulator_submission"] = {"status": "SUBMITTED", "wait_ms": decision.wait_ms}
    event["simulator_acceptance"] = {
        "status": "ACCEPTED",
        "evidence": "bridge.wait_returned_state",
        "evidence_scope": "SIMULATOR",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED_BY_EXPLICIT_SOURCE_WAIT",
        "consumes_decision": True,
    }
    event["simulator_outcome"] = {"status": "WAIT_ACCEPTED"}
    event["simulator_state_after_immediate"] = dict(after)
    return event, dict(after), True, None


def _base_event(
    order: int,
    item: _ResolvedSinkV4,
    current: Mapping[str, Any],
    count: int,
) -> JSONMap:
    return {
        "order": order,
        "source_sink": item.sink.to_dict(),
        "source_attempt": {
            "status": "ATTEMPTED",
            "evidence": "ExpertDecision.raw_sink_order",
        },
        "operation_contract": {
            "recognized": item.kind != "unsupported",
            "canonical_action": item.action_key,
            "action_ref": item.action_ref.to_wire() if item.action_ref else None,
            "known_source_noop": item.known_noop,
        },
        "simulator_state_before": dict(current),
        "simulator_submission": {"status": "NOT_EVALUATED"},
        "simulator_acceptance": {"status": "NOT_EVALUATED"},
        "client_observation": _client_not_observed(),
        "server_outcome": _server_not_observed(),
        "simulator_outcome": {"status": "NOT_EVALUATED"},
        "decision_consumption": {"status": "NOT_EVALUATED", "consumes_decision": None},
        "traversal": {
            "source_sequence": (
                "CONTINUE_TO_NEXT_RAW_SINK"
                if order < count
                else "END_RECORDED_RAW_SINK_SEQUENCE"
            ),
            "simulator_acceptance_does_not_rewrite_source_traversal": True,
            "executor_state": "CONTINUE",
        },
    }


def _not_submitted(event: JSONMap, status: str) -> None:
    event["simulator_submission"] = {"status": status}
    event["simulator_acceptance"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}
    event["decision_consumption"] = {
        "status": "NOT_APPLICABLE_NOT_SUBMITTED",
        "consumes_decision": None,
    }
    event["simulator_outcome"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}


def _record_current_action_guard_return(event: JSONMap) -> None:
    """The source helper returns before CastSpellByName when Cleave stays current."""
    event["simulator_submission"] = {
        "status": "NOT_SUBMITTED_SOURCE_DECLARED_NOOP",
        "reason": "IsCurrentAction_returned_true_after_rejected_whirlwind",
        "bridge_call_made": False,
    }
    event["simulator_acceptance"] = {
        "status": "REJECTED_SOURCE_DECLARED_NOOP",
        "evidence_scope": "SOURCE_GUARD_SIMULATOR_PROXY",
    }
    event["decision_consumption"] = {
        "status": "NOT_CONSUMED",
        "consumes_decision": False,
        "expected_for_lane": False,
    }
    event["simulator_outcome"] = {"status": "NOT_APPLICABLE_REJECTED"}
    event["traversal"]["executor_state"] = "SOURCE_HELPER_RETURNED_BEFORE_CAST"


def _record_post_gcd_rejection(
    event: JSONMap,
    *,
    consumed_by_order: int,
    consumed_by_action: str,
) -> None:
    """Dispose a reached source GCD after this simulator decision was consumed.

    The source call is retained in the ordered attempt ledger, but issuing a
    second ``bridge.act`` would violate the bridge lifecycle. The observed
    same-key Whirlwind -> Cleave client probe also rejected the Cleave attempt;
    this ledger still describes only the simulator execution, not a new live
    client receipt for this rollout.
    """

    event["simulator_submission"] = {
        "status": POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
        "reason": POST_GCD_REJECTION_REASON_V4,
        "bridge_call_made": False,
    }
    event["simulator_acceptance"] = {
        "status": POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4,
        "reason": POST_GCD_REJECTION_REASON_V4,
        "evidence": "earlier_sink_simulator_acceptance_and_decision_consumption",
        "evidence_scope": "SIMULATOR_EXECUTION_LEDGER",
    }
    event["decision_consumption"] = {
        "status": "ALREADY_CONSUMED_BY_EARLIER_GCD",
        "consumes_decision": False,
        "expected_for_lane": True,
        "consumed_by_sink_order": consumed_by_order,
        "consumed_by_action": consumed_by_action,
    }
    event["simulator_outcome"] = {
        "status": "NOT_APPLICABLE_REJECTED",
        "acceptance_is_not_outcome": True,
    }
    event["traversal"]["executor_state"] = (
        "SOURCE_ATTEMPT_RECORDED_AS_POST_GCD_SIMULATOR_REJECTION"
    )


def _client_not_observed() -> JSONMap:
    return {"status": "NOT_OBSERVED_NO_WOW_CLIENT", "accepted": None, "evidence": None}


def _server_not_observed() -> JSONMap:
    return {
        "status": "NOT_OBSERVED_NO_GAME_SERVER_LOG",
        "outcome": None,
        "damage": None,
        "evidence": None,
    }


def _available_action(row: AvailableAction) -> JSONMap:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
    }


__all__ = (
    "Contra260817ItemActionBindingV4",
    "Contra260817OrderedSinkExecutorV4Error",
    "Contra260817SimulatorControlFacadeV4",
    "Contra260817TargetBindingV4",
    "EXECUTION_SCHEMA_V4",
    "IMPLEMENTATION_REVISION",
    "POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4",
    "POST_GCD_REJECTION_REASON_V4",
    "POST_GCD_REJECTION_SUBMISSION_STATUS_V4",
    "SOURCE_REENTRY_CLOCK_SCHEMA_V4",
    "SOURCE_REENTRY_RETRY_MS_V4",
    "SOURCE_REENTRY_TIMING_AUTHORITY_V4",
    "SUPPORTED_OPERATION_COVERAGE_V4",
    "execute_contra260817_ordered_sinks_v4",
)
