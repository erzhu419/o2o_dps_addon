"""Cat profile-1 ordered-sink simulator executor and receipts (v5).

This module is intentionally additive.  It consumes the source-derived,
non-voting :mod:`cat_fury_full_policy_readiness_v4` decision and submits every
raw sink in the recorded Lua order.  Simulator submission/acceptance is kept
strictly separate from a WoW client observation and from a game-server
outcome; neither of the latter is available here.

The native bridge has exact spell, queue, target, start-attack, and stop-cast
commands.  It does not expose WoW CVars or bag/equipment slot APIs.  The local
facade below therefore records those calls in a typed simulator sidecar.  A
sidecar acceptance proves ordering and state-machine coverage only; it is
explicitly an omitted combat mechanic and can never authorize comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from .cat_fury_full_policy_readiness_v4 import (
    ADAPTER_CONTRACT_SHA256,
    POLICY_ID,
    CatFuryFullPolicyAdapterV4,
    build_synthetic_differential_receipt_v4,
    validate_source_decision_v4,
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
from .sim_bridge import ActionRef, ActResult, AvailableAction


JSONMap = dict[str, Any]
EXECUTION_SCHEMA_V5 = "cat_fury_ordered_sink_simulator_execution/v5"
MAPPING_RECEIPT_SCHEMA_V5 = "cat_fury_ordered_sink_mapping_coverage/v5"
EXECUTION_COVERAGE_SCHEMA_V5 = "cat_fury_ordered_sink_execution_coverage/v5"
IMPLEMENTATION_REVISION = "v5.0_cat_profile1_ordered_simulator_sidecar"


class CatFuryOrderedSinkExecutorV5Error(RuntimeError):
    """The Cat v5 ordered-sink or receipt contract was violated."""


@dataclass(frozen=True)
class CatSimulatorControlResultV5:
    """A simulator control result, never a WoW-client observation."""

    accepted: bool
    consumes_decision: bool
    state: Mapping[str, Any]
    effect_scope: str
    combat_mechanics_modeled: bool
    omission_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be boolean")
        if not isinstance(self.consumes_decision, bool):
            raise TypeError("consumes_decision must be boolean")
        if not isinstance(self.state, Mapping):
            raise TypeError("state must be a mapping")
        if not isinstance(self.effect_scope, str) or not self.effect_scope:
            raise TypeError("effect_scope must be nonempty")
        if not isinstance(self.combat_mechanics_modeled, bool):
            raise TypeError("combat_mechanics_modeled must be boolean")
        if self.omission_code is not None and (
            not isinstance(self.omission_code, str) or not self.omission_code
        ):
            raise TypeError("omission_code must be nonempty or None")


@dataclass(frozen=True)
class CatSimulatorItemActionBindingV5:
    """Bind one Cat slot locator to an exact simulator item ActionRef."""

    locator: str
    action: ActionRef

    def __post_init__(self) -> None:
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise TypeError("locator must be nonempty")
        if not isinstance(self.action, ActionRef) or self.action.item_id <= 0:
            raise TypeError("item binding action must be an item ActionRef")
        if self.action.spell_id or self.action.other_id:
            raise ValueError("item binding action must contain only item_id/tag")


class CatOrderedSinkBridgeLikeV5(Protocol):
    def actions(self) -> list[AvailableAction]: ...

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult: ...

    def wait(self, wait_ms: int) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _ResolvedSinkV5:
    sink: RawSink
    action_key: str | None = None
    action_ref: ActionRef | None = None
    queue: SwingQueueOp | None = None
    stance: StanceOp | None = None
    arguments: tuple[Any, ...] = ()
    recognized: bool = True


SUPPORTED_OPERATION_COVERAGE_V5 = frozenset(
    {
        ("autoattack", "AttackTarget"),
        ("item", "UseInventoryItem"),
        ("item", "UseContainerItem"),
        ("off_gcd", "CastSpellByName"),
        ("off_gcd", "QueueSpellByName"),
        ("swing_queue", "QueueSpellByName"),
        ("cast_control", "SpellStopCasting"),
        ("stance", "CastSpellByName"),
        ("gcd", "CastSpellByName"),
        ("cvar", "SetCVar"),
    }
)

# Profile1 stores Target=0 and therefore emits no target sink.  The rollout
# still audits the native set_target capability, but no target operation is
# fabricated into the source ledger.
PROFILE1_TARGET_CONTRACT_V5: JSONMap = {
    "profile_target_mode": 0,
    "source_target_sink_required": False,
    "source_target_sink_observed": False,
    "bridge_control": "set_target",
    "bridge_capability_audited": True,
    "reason": "profile1 Target=0; target selection remains simulator lifecycle state",
}

_ACTION_KEY_BY_VALUE: Mapping[str, str] = {
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
    "压制": "warrior.overpower",
}
_ACTION_REFS: Mapping[str, ActionRef] = {
    **ACTION_KEY_TO_REF,
    # Turtle WoWSims registers level-60 Overpower as spell 11585.
    "warrior.overpower": ActionRef(spell_id=11585),
}
_QUEUE_BY_VALUE: Mapping[str, SwingQueueOp] = {
    "英勇打击": SwingQueueOp.HEROIC_STRIKE,
    "顺劈斩": SwingQueueOp.CLEAVE,
}
_STANCE_BY_VALUE: Mapping[str, StanceOp] = {
    "战斗姿态": StanceOp.BATTLE,
    "防御姿态": StanceOp.DEFENSIVE,
    "狂暴姿态": StanceOp.BERSERKER,
}
_CONTAINER_RE = re.compile(r"([0-4]):([1-9][0-9]*):(.+)\Z")
_CVAR_VALUES = {
    "NP_QueueCastTimeSpells=0": ("NP_QueueCastTimeSpells", False),
    "NP_QueueCastTimeSpells=1": ("NP_QueueCastTimeSpells", True),
    "NP_QueueInstantSpells=0": ("NP_QueueInstantSpells", False),
    "NP_QueueInstantSpells=1": ("NP_QueueInstantSpells", True),
}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        "algorithm": "sha256-canonical-json-v1",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _canonical_sha256(core),
    }


class CatSimulatorControlFacadeV5:
    """Add typed CVar/item sidecar controls without altering the bridge.

    Native methods are delegated when present.  Missing item bindings are
    accepted only by the sidecar ledger and carry a mandatory mechanics
    omission.  This is sufficient to test ordering, never DPS equivalence.
    """

    def __init__(
        self,
        bridge: Any,
        *,
        item_bindings: Sequence[CatSimulatorItemActionBindingV5] = (),
        initial_autoattack_active: bool = True,
        initial_cvars: Mapping[str, bool] | None = None,
    ) -> None:
        if not isinstance(initial_autoattack_active, bool):
            raise TypeError("initial_autoattack_active must be boolean")
        bindings: dict[str, ActionRef] = {}
        for binding in item_bindings:
            if not isinstance(binding, CatSimulatorItemActionBindingV5):
                raise TypeError("item_bindings must contain v5 bindings")
            if binding.locator in bindings:
                raise ValueError(f"duplicate item binding {binding.locator!r}")
            bindings[binding.locator] = binding.action
        cvars = {
            "NP_QueueCastTimeSpells": True,
            "NP_QueueInstantSpells": True,
        }
        if initial_cvars is not None:
            if set(initial_cvars) != set(cvars) or any(
                not isinstance(value, bool) for value in initial_cvars.values()
            ):
                raise ValueError("initial_cvars must contain the two typed NP CVars")
            cvars.update(initial_cvars)
        self._bridge = bridge
        self._bindings = bindings
        self._initial_autoattack_active = initial_autoattack_active
        self._initial_cvars = dict(cvars)
        self._autoattack_active = initial_autoattack_active
        self._cvars = dict(cvars)
        self._used_locators: list[str] = []
        self._control_receipts: list[JSONMap] = []

    def reset_sidecar(self) -> None:
        self._autoattack_active = self._initial_autoattack_active
        self._cvars = dict(self._initial_cvars)
        self._used_locators = []
        self._control_receipts = []

    @property
    def autoattack_active(self) -> bool:
        return self._autoattack_active

    @property
    def used_locators(self) -> tuple[str, ...]:
        return tuple(self._used_locators)

    @property
    def cvars(self) -> Mapping[str, bool]:
        return dict(self._cvars)

    def sidecar_receipt(self) -> JSONMap:
        return {
            "schema": "cat_fury_simulator_control_sidecar/v5",
            "autoattack_active": self._autoattack_active,
            "cvars": dict(sorted(self._cvars.items())),
            "used_item_locators": list(self._used_locators),
            "control_receipts": [dict(row) for row in self._control_receipts],
            "game_client_observed": False,
            "game_server_outcome_observed": False,
        }

    def load_dynamic_v2(self, request: Mapping[str, Any], seed: int, config: Any) -> Any:
        self.reset_sidecar()
        return self._bridge.load_dynamic_v2(request, seed, config)

    def start_attack(self) -> CatSimulatorControlResultV5:
        result = self._native_control("start_attack")
        if result.accepted:
            self._autoattack_active = True
        return result

    def stop_cast(self) -> CatSimulatorControlResultV5:
        return self._native_control("stop_cast")

    def set_target(self, target_index: int) -> Any:
        method = getattr(self._bridge, "set_target", None)
        if not callable(method):
            raise CatFuryOrderedSinkExecutorV5Error(
                "native simulator set_target capability is absent"
            )
        return method(target_index)

    def cat_set_cvar(self, name: str, enabled: bool) -> CatSimulatorControlResultV5:
        if name not in self._cvars or not isinstance(enabled, bool):
            raise CatFuryOrderedSinkExecutorV5Error("unsupported Cat CVar control")
        before = self._cvars[name]
        self._cvars[name] = enabled
        state = self._state()
        row = {
            "kind": "CVAR",
            "name": name,
            "before": before,
            "after": enabled,
            "effect_scope": "SIMULATOR_SIDECAR_ONLY",
            "combat_mechanics_modeled": False,
            "omission_code": "CVAR_CLIENT_QUEUE_MECHANICS_NOT_MODELED",
        }
        self._control_receipts.append(row)
        return CatSimulatorControlResultV5(
            True,
            False,
            state,
            "SIMULATOR_SIDECAR_ONLY",
            False,
            "CVAR_CLIENT_QUEUE_MECHANICS_NOT_MODELED",
        )

    def cat_use_inventory_item(self, slot: int) -> CatSimulatorControlResultV5:
        if slot not in {13, 14}:
            raise CatFuryOrderedSinkExecutorV5Error("unsupported inventory slot")
        return self._use_item(f"inventory:{slot}")

    def cat_use_container_item(
        self, bag: int, slot: int, name: str
    ) -> CatSimulatorControlResultV5:
        if bag < 0 or slot <= 0 or not name:
            raise CatFuryOrderedSinkExecutorV5Error("invalid container locator")
        return self._use_item(f"container:{bag}:{slot}:{name}")

    def _native_control(self, method_name: str) -> CatSimulatorControlResultV5:
        method = getattr(self._bridge, method_name, None)
        if not callable(method):
            raise CatFuryOrderedSinkExecutorV5Error(
                f"native simulator {method_name} capability is absent"
            )
        raw = method()
        accepted = getattr(raw, "accepted", None)
        consumed = getattr(raw, "consumes_decision", None)
        state = getattr(raw, "state", None)
        if not isinstance(accepted, bool) or not isinstance(consumed, bool) or not isinstance(state, Mapping):
            raise CatFuryOrderedSinkExecutorV5Error(
                f"native {method_name} returned an untyped control result"
            )
        row = {
            "kind": method_name,
            "accepted": accepted,
            "effect_scope": "NATIVE_SIMULATOR_CONTROL",
            "combat_mechanics_modeled": True,
            "omission_code": None,
        }
        self._control_receipts.append(row)
        return CatSimulatorControlResultV5(
            accepted,
            consumed,
            dict(state),
            "NATIVE_SIMULATOR_CONTROL",
            True,
        )

    def _use_item(self, locator: str) -> CatSimulatorControlResultV5:
        self._used_locators.append(locator)
        action = self._bindings.get(locator)
        if action is None:
            state = self._state()
            row = {
                "kind": "ITEM",
                "locator": locator,
                "accepted": True,
                "effect_scope": "SIMULATOR_SIDECAR_ONLY",
                "combat_mechanics_modeled": False,
                "omission_code": "ITEM_COMBAT_EFFECT_NOT_BOUND_TO_SIMULATOR_ACTION",
            }
            self._control_receipts.append(row)
            return CatSimulatorControlResultV5(
                True,
                False,
                state,
                "SIMULATOR_SIDECAR_ONLY",
                False,
                "ITEM_COMBAT_EFFECT_NOT_BOUND_TO_SIMULATOR_ACTION",
            )
        available = {row.action: row for row in self.actions()}
        row = available.get(action)
        if row is None:
            raise CatFuryOrderedSinkExecutorV5Error(
                f"bound item {locator!r} is absent from simulator actions"
            )
        result = self.act(action)
        if not isinstance(result, ActResult):
            raise CatFuryOrderedSinkExecutorV5Error(
                "native item act returned an untyped result"
            )
        receipt = {
            "kind": "ITEM",
            "locator": locator,
            "action": action.to_wire(),
            "accepted": result.casted,
            "effect_scope": "NATIVE_SIMULATOR_ITEM_ACTION",
            "combat_mechanics_modeled": True,
            "omission_code": None,
        }
        self._control_receipts.append(receipt)
        return CatSimulatorControlResultV5(
            result.casted,
            result.consumes_decision,
            result.state,
            "NATIVE_SIMULATOR_ITEM_ACTION",
            True,
        )

    def _state(self) -> Mapping[str, Any]:
        method = getattr(self._bridge, "state", None)
        if not callable(method):
            raise CatFuryOrderedSinkExecutorV5Error("bridge.state capability is absent")
        value = method()
        if not isinstance(value, Mapping):
            raise CatFuryOrderedSinkExecutorV5Error("bridge.state returned non-mapping")
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def execute_cat_fury_ordered_sinks_v5(
    bridge: CatOrderedSinkBridgeLikeV5,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id_prefix: str | None = None,
    result_bearing_action_keys: Sequence[str] = (),
) -> JSONMap:
    """Submit one exact Cat source invocation and return its typed ledger."""

    if not isinstance(decision, ExpertDecision):
        raise TypeError("decision must be ExpertDecision")
    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    if attempt_id_prefix is not None and (
        not isinstance(attempt_id_prefix, str) or not attempt_id_prefix.strip()
    ):
        raise TypeError("attempt_id_prefix must be nonempty or None")
    if isinstance(result_bearing_action_keys, (str, bytes)) or not isinstance(
        result_bearing_action_keys, Sequence
    ):
        raise TypeError("result_bearing_action_keys must be a sequence")
    result_bearing = frozenset(result_bearing_action_keys)

    resolved, preflight = _preflight(decision)
    current = dict(state)
    blocked = bool(preflight)
    consumed = not bool(current.get("needs_input", True))
    reasons = list(preflight)
    events: list[JSONMap] = []
    omissions: list[JSONMap] = []
    accepted_gcd_actions: list[str] = []

    for index, item in enumerate(resolved, start=1):
        traversal = (
            "CONTINUE_TO_NEXT_RAW_SINK"
            if index < len(resolved)
            else "END_RECORDED_RAW_SINK_SEQUENCE"
        )
        event = _base_event(index, item, current, traversal)
        if blocked:
            _mark_not_submitted(event, "NOT_SUBMITTED_FAIL_CLOSED")
            event["traversal"]["executor_state"] = "FAIL_CLOSED"
            events.append(event)
            continue
        if consumed and item.sink.channel != "cvar":
            _mark_not_submitted(
                event, "NOT_SUBMITTED_DECISION_ALREADY_CONSUMED"
            )
            event["decision_consumption"] = {
                "status": "ALREADY_CONSUMED_BY_EARLIER_SINK",
                "consumes_decision": None,
            }
            event["traversal"]["executor_state"] = (
                "SOURCE_ATTEMPT_RECORDED_WITHOUT_SIMULATOR_SUBMISSION"
            )
            reasons.append(
                f"raw_sink[{index}]:simulator_cannot_submit_after_decision_consumed"
            )
            events.append(event)
            continue
        try:
            attempt_id = None
            if (
                attempt_id_prefix is not None
                and item.sink.channel == "gcd"
                and item.action_key in result_bearing
            ):
                attempt_id = f"{attempt_id_prefix}:sink-{index}"
            event, current, sink_consumed, runtime_reason, runtime_blocked = (
                _execute_resolved(
                    bridge,
                    item,
                    current,
                    event,
                    result_attempt_id=attempt_id,
                )
            )
        except Exception as error:
            event["simulator_submission"] = {
                "status": "BRIDGE_ERROR_FAIL_CLOSED",
                "error_type": type(error).__name__,
            }
            event["simulator_acceptance"] = {
                "status": "UNKNOWN_BRIDGE_ERROR",
                "evidence_scope": "SIMULATOR",
            }
            event["decision_consumption"] = {
                "status": "UNKNOWN_BRIDGE_ERROR",
                "consumes_decision": None,
            }
            event["simulator_outcome"] = {
                "status": "UNKNOWN_BRIDGE_ERROR"
            }
            event["traversal"]["executor_state"] = "FAIL_CLOSED"
            sink_consumed = False
            runtime_reason = f"raw_sink[{index}]:bridge_exception:{type(error).__name__}"
            runtime_blocked = True
        events.append(event)
        if runtime_reason:
            reasons.append(runtime_reason)
        omission = event.get("mechanics_omission")
        if isinstance(omission, Mapping):
            omissions.append(dict(omission))
        acceptance = event.get("simulator_acceptance")
        if (
            item.sink.channel == "gcd"
            and item.action_key
            and isinstance(acceptance, Mapping)
            and acceptance.get("status") == "ACCEPTED"
        ):
            accepted_gcd_actions.append(item.action_key)
        consumed = consumed or sink_consumed
        blocked = blocked or runtime_blocked

    wait_event: JSONMap | None = None
    if decision.gcd == WAIT_ACTION:
        wait_event = _execute_wait(
            bridge, decision, current, blocked=blocked, consumed=consumed
        )
        current = dict(wait_event.pop("_state"))
        reason = wait_event.pop("_reason")
        if reason:
            reasons.append(str(reason))
        blocked = blocked or bool(wait_event.pop("_blocked"))
        if wait_event["decision_consumption"].get("consumes_decision") is True:
            consumed = True

    reasons = list(dict.fromkeys(reasons))
    return {
        "schema": EXECUTION_SCHEMA_V5,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "expert_id": decision.expert_id,
        "source_decision": decision.to_dict(),
        "raw_sink_order": [sink.to_dict() for sink in decision.raw_sink_order],
        "sink_events": events,
        "wait_event": wait_event,
        "fallback": {"used": False, "reason": "V5_NEVER_SYNTHESIZES_FALLBACK"},
        "decision_consumed": consumed,
        "execution_blocked": blocked,
        "accepted_gcd_actions": accepted_gcd_actions,
        "evidence_boundary": {
            "simulator_submission_observed": True,
            "simulator_acceptance_observed": True,
            "wow_client_observed": False,
            "game_server_outcome_observed": False,
            "simulator_acceptance_is_client_acceptance": False,
            "simulator_outcome_is_game_server_outcome": False,
        },
        "mechanics_omissions": _unique_rows(omissions),
        "simulator_mechanics_complete": not omissions,
        "nonfaithful_reasons": reasons,
        "source_to_simulator_order_faithful": not reasons,
        "ordered_projection_faithful": False,
        "comparison_ready": False,
        "final_state": current,
    }


def _preflight(
    decision: ExpertDecision,
) -> tuple[list[_ResolvedSinkV5], list[str]]:
    reasons: list[str] = []
    resolved: list[_ResolvedSinkV5] = []
    try:
        validate_source_decision_v4(decision)
    except Exception as error:
        reasons.append(f"proposal:v4_source_decision_invalid:{type(error).__name__}")
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
    for index, sink in enumerate(decision.raw_sink_order, start=1):
        item, sink_reasons = _resolve_sink(sink, index=index)
        reasons.extend(sink_reasons)
        supported = (sink.channel, sink.operation) in SUPPORTED_OPERATION_COVERAGE_V5
        if not supported:
            reasons.append(
                f"raw_sink[{index}]:unsupported_operation:{sink.channel}/{sink.operation}"
            )
        resolved.append(
            replace(item, recognized=supported and not sink_reasons)
        )
    raw_gcd_keys = [
        item.action_key
        for item in resolved
        if item.sink.channel == "gcd" and item.action_key is not None
    ]
    if decision.metadata.get("raw_gcd_calls") != raw_gcd_keys:
        reasons.append("proposal:metadata_raw_gcd_calls_mismatch")
    if decision.metadata.get("source_derived_diagnostic_executable") is not True:
        reasons.append("proposal:source_diagnostic_not_executable")
    reasons.extend(_normalized_lane_reasons(decision, resolved))
    return resolved, list(dict.fromkeys(reasons))


def _resolve_sink(
    sink: RawSink, *, index: int
) -> tuple[_ResolvedSinkV5, list[str]]:
    reasons: list[str] = []
    if not sink.source_ref:
        reasons.append(f"raw_sink[{index}]:source_ref_missing")
    if sink.channel == "autoattack":
        if sink.operation != "AttackTarget" or sink.value != "START":
            reasons.append(f"raw_sink[{index}]:unsupported_autoattack")
        return _ResolvedSinkV5(sink, arguments=("start_attack",)), reasons
    if sink.channel == "cast_control":
        if sink.operation != "SpellStopCasting" or sink.value is not None:
            reasons.append(f"raw_sink[{index}]:unsupported_cast_control")
        return _ResolvedSinkV5(sink, arguments=("stop_cast",)), reasons
    if sink.channel == "item" and sink.operation == "UseInventoryItem":
        try:
            slot = int(sink.value or "")
        except ValueError:
            slot = -1
        if slot not in {13, 14}:
            reasons.append(f"raw_sink[{index}]:unsupported_inventory_slot")
        return _ResolvedSinkV5(
            sink, arguments=("cat_use_inventory_item", slot)
        ), reasons
    if sink.channel == "item" and sink.operation == "UseContainerItem":
        match = _CONTAINER_RE.fullmatch(sink.value or "")
        if match is None:
            reasons.append(f"raw_sink[{index}]:invalid_container_locator")
            return _ResolvedSinkV5(sink), reasons
        bag, slot, name = int(match.group(1)), int(match.group(2)), match.group(3)
        return _ResolvedSinkV5(
            sink, arguments=("cat_use_container_item", bag, slot, name)
        ), reasons
    if sink.channel == "cvar":
        parsed = _CVAR_VALUES.get(sink.value or "")
        if sink.operation != "SetCVar" or parsed is None:
            reasons.append(f"raw_sink[{index}]:unsupported_cvar")
            return _ResolvedSinkV5(sink), reasons
        return _ResolvedSinkV5(
            sink, arguments=("cat_set_cvar", parsed[0], parsed[1])
        ), reasons
    if sink.channel == "swing_queue":
        queue = _QUEUE_BY_VALUE.get(sink.value or "")
        if queue is None:
            reasons.append(f"raw_sink[{index}]:unsupported_queue_value")
            return _ResolvedSinkV5(sink), reasons
        return _ResolvedSinkV5(
            sink,
            action_ref=QUEUE_REFS[queue],
            queue=queue,
        ), reasons
    if sink.channel in {"gcd", "off_gcd", "stance"}:
        action_key = _ACTION_KEY_BY_VALUE.get(sink.value or "")
        if action_key is None:
            reasons.append(f"raw_sink[{index}]:unsupported_action_value:{sink.value}")
            return _ResolvedSinkV5(sink), reasons
        stance = _STANCE_BY_VALUE.get(sink.value or "") if sink.channel == "stance" else None
        if sink.channel == "stance" and stance is None:
            reasons.append(f"raw_sink[{index}]:unsupported_stance_value")
        return _ResolvedSinkV5(
            sink,
            action_key=action_key,
            action_ref=_ACTION_REFS[action_key],
            stance=stance,
        ), reasons
    reasons.append(f"raw_sink[{index}]:unsupported_channel:{sink.channel}")
    return _ResolvedSinkV5(sink), reasons


def _normalized_lane_reasons(
    decision: ExpertDecision, resolved: Sequence[_ResolvedSinkV5]
) -> list[str]:
    reasons: list[str] = []
    raw_gcd = [row.action_key for row in resolved if row.sink.channel == "gcd"]
    if decision.gcd == WAIT_ACTION:
        if raw_gcd:
            reasons.append("proposal:wait_conflicts_with_raw_gcd")
    elif not raw_gcd or raw_gcd[-1] != decision.gcd:
        reasons.append("proposal:normalized_gcd_mismatch")
    raw_off = tuple(
        row.action_key
        for row in resolved
        if row.sink.channel == "off_gcd" and row.action_key is not None
    )
    if raw_off != decision.off_gcd:
        reasons.append("proposal:normalized_off_gcd_mismatch")
    raw_queue = [row.queue for row in resolved if row.queue is not None]
    if raw_queue:
        if raw_queue[-1] is not decision.swing_queue:
            reasons.append("proposal:normalized_swing_queue_mismatch")
    elif decision.swing_queue is not SwingQueueOp.KEEP:
        reasons.append("proposal:normalized_swing_queue_missing")
    raw_stance = [row.stance for row in resolved if row.stance is not None]
    if raw_stance:
        if raw_stance[-1] is not decision.stance:
            reasons.append("proposal:normalized_stance_mismatch")
    elif decision.stance is not StanceOp.KEEP:
        reasons.append("proposal:normalized_stance_missing")
    has_stop = any(row.sink.channel == "cast_control" for row in resolved)
    if has_stop != (decision.cast_control is CastControl.STOP_CAST):
        reasons.append("proposal:normalized_cast_control_mismatch")
    if decision.target is not TargetOp.KEEP:
        reasons.append("proposal:profile1_target_must_remain_keep")
    return reasons


def _execute_resolved(
    bridge: CatOrderedSinkBridgeLikeV5,
    item: _ResolvedSinkV5,
    current: JSONMap,
    event: JSONMap,
    *,
    result_attempt_id: str | None,
) -> tuple[JSONMap, JSONMap, bool, str | None, bool]:
    sink = item.sink
    if sink.channel in {"autoattack", "cast_control", "item", "cvar"}:
        if sink.channel == "cast_control" and not _is_casting(current):
            event["simulator_submission"] = {
                "status": "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
                "reason": "no_active_cast",
            }
            event["simulator_acceptance"] = {
                "status": "NOOP_STATE_ALREADY_SATISFIED",
                "evidence_scope": "SIMULATOR",
            }
            event["decision_consumption"] = {
                "status": "NOT_CONSUMED",
                "consumes_decision": False,
                "expected_for_lane": False,
            }
            event["simulator_outcome"] = {"status": "NOOP"}
            return event, current, False, None, False
        return _execute_control(bridge, item, current, event)

    if item.action_ref is None:
        raise CatFuryOrderedSinkExecutorV5Error("resolved action is missing")
    available = {row.action: row for row in bridge.actions()}
    row = available.get(item.action_ref)
    if row is None:
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_ACTION_ABSENT_FROM_SPELLBOOK",
            "action": item.action_ref.to_wire(),
        }
        event["simulator_acceptance"] = {
            "status": "REJECTED_ACTION_ABSENT_FROM_SIMULATOR_SPELLBOOK",
            "evidence_scope": "SIMULATOR",
        }
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE_REJECTED"}
        return (
            event,
            current,
            False,
            f"raw_sink[{event['order']}]:action_absent_from_simulator_spellbook",
            True,
        )
    result = (
        bridge.act(item.action_ref, attempt_id=result_attempt_id)
        if result_attempt_id is not None
        else bridge.act(item.action_ref)
    )
    if not isinstance(result, ActResult):
        raise CatFuryOrderedSinkExecutorV5Error("bridge.act must return ActResult")
    after = dict(result.state)
    expected_consumption = sink.channel == "gcd"
    before_queue = _queued_swing(current)
    after_queue = _queued_swing(after)
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "action": item.action_ref.to_wire(),
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
        "expected_for_lane": expected_consumption,
        "available_action_triggers_gcd": row.triggers_gcd,
    }
    event["simulator_state_after_immediate"] = after
    event["simulator_outcome"] = {
        "status": (
            "PENDING_TYPED_SIMULATOR_RESULT"
            if result.casted and sink.channel == "gcd"
            else "ACCEPTED_NO_IMMEDIATE_DAMAGE_RESULT"
            if result.casted
            else "NOT_APPLICABLE_REJECTED"
        ),
        "acceptance_is_not_outcome": True,
    }
    if item.queue is not None:
        event["queue_transition"] = _queue_transition(
            before_queue, item.queue, after_queue, accepted=result.casted
        )
    reason = None
    if row.triggers_gcd is not expected_consumption:
        reason = (
            f"raw_sink[{event['order']}]:triggers_gcd_contract_mismatch:"
            f"bridge={str(row.triggers_gcd).lower()}:"
            f"lane_expected={str(expected_consumption).lower()}"
        )
    if result.casted and result.consumes_decision is not expected_consumption:
        mismatch = (
            f"raw_sink[{event['order']}]:decision_consumption_mismatch:"
            f"bridge={str(result.consumes_decision).lower()}:"
            f"lane_expected={str(expected_consumption).lower()}"
        )
        reason = mismatch if reason is None else f"{reason};{mismatch}"
    return event, after, result.consumes_decision, reason, False


def _execute_control(
    bridge: CatOrderedSinkBridgeLikeV5,
    item: _ResolvedSinkV5,
    current: JSONMap,
    event: JSONMap,
) -> tuple[JSONMap, JSONMap, bool, str | None, bool]:
    if not item.arguments:
        raise CatFuryOrderedSinkExecutorV5Error("resolved control arguments missing")
    method_name = str(item.arguments[0])
    method = getattr(bridge, method_name, None)
    if not callable(method):
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_BRIDGE_CAPABILITY_ABSENT",
            "control": method_name,
        }
        event["simulator_acceptance"] = {
            "status": "UNKNOWN_BRIDGE_CAPABILITY_ABSENT",
            "evidence_scope": "SIMULATOR",
        }
        event["decision_consumption"] = {
            "status": "NOT_APPLICABLE_NOT_SUBMITTED",
            "consumes_decision": None,
        }
        event["simulator_outcome"] = {"status": "UNKNOWN_NOT_SUBMITTED"}
        return (
            event,
            current,
            False,
            f"raw_sink[{event['order']}]:bridge_capability_absent:{method_name}",
            True,
        )
    result = method(*item.arguments[1:])
    if not isinstance(result, CatSimulatorControlResultV5):
        # A direct native bridge may expose v2's structurally typed control.
        accepted = getattr(result, "accepted", None)
        consumes = getattr(result, "consumes_decision", None)
        raw_state = getattr(result, "state", None)
        if not isinstance(accepted, bool) or not isinstance(consumes, bool) or not isinstance(raw_state, Mapping):
            raise CatFuryOrderedSinkExecutorV5Error(
                f"bridge.{method_name} must return a typed control result"
            )
        result = CatSimulatorControlResultV5(
            accepted,
            consumes,
            raw_state,
            "NATIVE_SIMULATOR_CONTROL",
            True,
        )
    after = dict(result.state)
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "control": method_name,
        "arguments": list(item.arguments[1:]),
        "effect_scope": result.effect_scope,
    }
    event["simulator_acceptance"] = {
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "evidence": f"bridge.{method_name}.accepted",
        "evidence_scope": "SIMULATOR",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
        "consumes_decision": result.consumes_decision,
        "expected_for_lane": False,
    }
    event["simulator_state_after_immediate"] = after
    event["simulator_outcome"] = {
        "status": "CONTROL_ACCEPTED" if result.accepted else "CONTROL_REJECTED",
        "combat_mechanics_modeled": result.combat_mechanics_modeled,
        "effect_scope": result.effect_scope,
    }
    if result.omission_code:
        event["mechanics_omission"] = {
            "code": result.omission_code,
            "order": event["order"],
            "channel": item.sink.channel,
            "operation": item.sink.operation,
            "comparison_fatal": True,
        }
    reason = None
    if result.accepted and result.consumes_decision:
        reason = (
            f"raw_sink[{event['order']}]:{method_name}_unexpectedly_consumed_decision"
        )
    return event, after, result.consumes_decision, reason, False


def _execute_wait(
    bridge: CatOrderedSinkBridgeLikeV5,
    decision: ExpertDecision,
    current: JSONMap,
    *,
    blocked: bool,
    consumed: bool,
) -> JSONMap:
    event: JSONMap = {
        "kind": "SOURCE_WAIT_DECISION",
        "requested_wait_ms": decision.wait_ms,
        "source_decision": WAIT_ACTION,
        "fallback": False,
        "client_observation": _client_not_observed(),
        "server_outcome": _server_not_observed(),
        "_state": current,
        "_reason": None,
        "_blocked": False,
    }
    if blocked:
        event["_blocked"] = True
        event["simulator_submission"] = {"status": "NOT_SUBMITTED_FAIL_CLOSED"}
        event["simulator_acceptance"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}
        event["decision_consumption"] = {
            "status": "NOT_APPLICABLE_NOT_SUBMITTED",
            "consumes_decision": None,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE"}
        return event
    if consumed or not bool(current.get("needs_input", True)):
        event["simulator_submission"] = {
            "status": "NOT_SUBMITTED_DECISION_ALREADY_CONSUMED"
        }
        event["simulator_acceptance"] = {
            "status": "NOT_APPLICABLE_DECISION_ALREADY_CONSUMED"
        }
        event["decision_consumption"] = {
            "status": "ALREADY_CONSUMED_BY_EARLIER_SINK",
            "consumes_decision": None,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE"}
        return event
    if decision.wait_ms is None or decision.wait_ms <= 0:
        event["simulator_submission"] = {"status": "INVALID_SOURCE_WAIT"}
        event["simulator_acceptance"] = {"status": "REJECTED"}
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE_REJECTED"}
        event["_reason"] = "gcd:invalid_source_wait"
        event["_blocked"] = True
        return event
    try:
        after = bridge.wait(decision.wait_ms)
        if not isinstance(after, Mapping):
            raise TypeError("bridge.wait returned non-mapping")
    except Exception as error:
        event["simulator_submission"] = {
            "status": "BRIDGE_ERROR_FAIL_CLOSED",
            "error_type": type(error).__name__,
        }
        event["simulator_acceptance"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
        event["decision_consumption"] = {
            "status": "UNKNOWN_BRIDGE_ERROR",
            "consumes_decision": None,
        }
        event["simulator_outcome"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
        event["_reason"] = f"gcd:wait_bridge_exception:{type(error).__name__}"
        event["_blocked"] = True
        return event
    event["simulator_submission"] = {
        "status": "SUBMITTED",
        "wait_ms": decision.wait_ms,
    }
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
    event["_state"] = dict(after)
    return event


def _base_event(
    index: int,
    item: _ResolvedSinkV5,
    current: Mapping[str, Any],
    traversal: str,
) -> JSONMap:
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
            "action_ref": item.action_ref.to_wire() if item.action_ref else None,
        },
        "simulator_state_before": dict(current),
        "simulator_submission": {"status": "NOT_EVALUATED"},
        "simulator_acceptance": {"status": "NOT_EVALUATED"},
        "client_observation": _client_not_observed(),
        "server_outcome": _server_not_observed(),
        "simulator_outcome": {"status": "NOT_EVALUATED"},
        "decision_consumption": {
            "status": "NOT_EVALUATED",
            "consumes_decision": None,
        },
        "queue_transition": {
            "kind": "NOT_A_QUEUE_OPERATION",
            "before": _queued_swing(current).value,
            "requested": item.queue.value if item.queue else None,
            "after": _queued_swing(current).value,
        },
        "traversal": {
            "source_sequence": traversal,
            "simulator_acceptance_does_not_rewrite_source_traversal": True,
            "executor_state": "CONTINUE",
        },
    }


def _mark_not_submitted(event: JSONMap, status: str) -> None:
    event["simulator_submission"] = {"status": status}
    event["simulator_acceptance"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}
    event["decision_consumption"] = {
        "status": "NOT_APPLICABLE_NOT_SUBMITTED",
        "consumes_decision": None,
    }
    event["simulator_outcome"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}


def _client_not_observed() -> JSONMap:
    return {
        "status": "NOT_OBSERVED_NO_WOW_CLIENT",
        "accepted": None,
        "evidence": None,
    }


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


def _queue_transition(
    before: SwingQueueOp,
    requested: SwingQueueOp,
    after: SwingQueueOp,
    *,
    accepted: bool,
) -> JSONMap:
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
        if action.get("spell_id") in {11567, 25286}:
            return SwingQueueOp.HEROIC_STRIKE
        if action.get("spell_id") == 20569:
            return SwingQueueOp.CLEAVE
    return SwingQueueOp.KEEP


def _is_casting(state: Mapping[str, Any]) -> bool:
    current = state.get("current_cast")
    if not isinstance(current, Mapping):
        return False
    remaining = current.get("remaining_ms")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        return remaining > 0
    return bool(current)


def _unique_rows(rows: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    seen: set[str] = set()
    result: list[JSONMap] = []
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(dict(row))
    return result


def build_operation_mapping_receipt_v5() -> JSONMap:
    """Resolve every v4 source-oracle raw sink without bridge mutation."""

    source = build_synthetic_differential_receipt_v4()
    fixture_rows: list[JSONMap] = []
    pairs: set[tuple[str, str]] = set()
    attempt_count = 0
    unresolved: list[JSONMap] = []
    for fixture in source["fixtures"]:
        mapped: list[JSONMap] = []
        for index, raw in enumerate(fixture["expected_source_raw_sink_order"], start=1):
            sink = RawSink(
                raw["channel"], raw["operation"], raw.get("value"), raw["source_ref"]
            )
            resolved, reasons = _resolve_sink(sink, index=index)
            supported = (sink.channel, sink.operation) in SUPPORTED_OPERATION_COVERAGE_V5
            row = {
                "order": index,
                "source_sink": sink.to_dict(),
                "recognized": supported and not reasons,
                "canonical_action": resolved.action_key,
                "action_ref": resolved.action_ref.to_wire() if resolved.action_ref else None,
                "control": resolved.arguments[0] if resolved.arguments else None,
                "reasons": reasons,
            }
            mapped.append(row)
            pairs.add((sink.channel, sink.operation))
            attempt_count += 1
            if not row["recognized"]:
                unresolved.append(
                    {"fixture_id": fixture["fixture_id"], **row}
                )
        fixture_rows.append(
            {
                "fixture_id": fixture["fixture_id"],
                "source_order_sha256": _canonical_sha256(
                    fixture["expected_source_raw_sink_order"]
                ),
                "mapped_attempts": mapped,
                "all_attempts_resolved": all(row["recognized"] for row in mapped),
            }
        )
    missing_pairs = sorted(SUPPORTED_OPERATION_COVERAGE_V5 - pairs)
    extra_pairs = sorted(pairs - SUPPORTED_OPERATION_COVERAGE_V5)
    core: JSONMap = {
        "schema": MAPPING_RECEIPT_SCHEMA_V5,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "policy_id": POLICY_ID,
        "adapter_contract_sha256": ADAPTER_CONTRACT_SHA256,
        "source_oracle_content_sha256": source["content_address"]["sha256"],
        "source_oracle_fixture_count": len(fixture_rows),
        "source_oracle_raw_attempt_count": attempt_count,
        "required_operation_pairs": [
            {"channel": channel, "operation": operation}
            for channel, operation in sorted(SUPPORTED_OPERATION_COVERAGE_V5)
        ],
        "observed_operation_pairs": [
            {"channel": channel, "operation": operation}
            for channel, operation in sorted(pairs)
        ],
        "missing_operation_pairs": [list(row) for row in missing_pairs],
        "unexpected_operation_pairs": [list(row) for row in extra_pairs],
        "unresolved_attempts": unresolved,
        "fixture_rows": fixture_rows,
        "profile1_target_contract": dict(PROFILE1_TARGET_CONTRACT_V5),
        "source_oracle_to_executor_mapping_complete": (
            not missing_pairs and not extra_pairs and not unresolved
        ),
        "bridge_process_mutated": False,
        "native_process_execution_coverage_claimed": False,
        "game_client_observed": False,
        "game_server_outcome_observed": False,
        "comparison_ready": False,
        "runner_registration_authorized": False,
    }
    return {**core, "content_address": _content_address(core)}


def validate_operation_mapping_receipt_v5(value: Mapping[str, Any]) -> JSONMap:
    raw = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if raw.get("schema") != MAPPING_RECEIPT_SCHEMA_V5:
        raise CatFuryOrderedSinkExecutorV5Error("mapping receipt schema mismatch")
    core = {key: item for key, item in raw.items() if key != "content_address"}
    if raw.get("content_address") != _content_address(core):
        raise CatFuryOrderedSinkExecutorV5Error("mapping receipt content mismatch")
    if raw.get("source_oracle_to_executor_mapping_complete") is not True:
        raise CatFuryOrderedSinkExecutorV5Error("operation mapping is incomplete")
    if raw.get("missing_operation_pairs") != [] or raw.get("unexpected_operation_pairs") != [] or raw.get("unresolved_attempts") != []:
        raise CatFuryOrderedSinkExecutorV5Error("operation mapping has gaps")
    for name in (
        "native_process_execution_coverage_claimed",
        "game_client_observed",
        "game_server_outcome_observed",
        "comparison_ready",
        "runner_registration_authorized",
    ):
        if raw.get(name) is not False:
            raise CatFuryOrderedSinkExecutorV5Error(f"{name} must remain false")
    return raw


def audit_execution_coverage_v5(
    executions: Mapping[str, Mapping[str, Any]],
    *,
    evidence_scope: str = "DETERMINISTIC_SIMULATOR_CONTRACT_HARNESS",
) -> JSONMap:
    """Audit caller-produced executions against all 43 source fixtures.

    This accepts deterministic contract-harness or real simulator executions,
    but records the caller's evidence label instead of upgrading it to client
    evidence.  Every raw attempt must have one ordered simulator disposition.
    """

    mapping = validate_operation_mapping_receipt_v5(
        build_operation_mapping_receipt_v5()
    )
    if not isinstance(executions, Mapping):
        raise TypeError("executions must be a mapping")
    if not isinstance(evidence_scope, str) or not evidence_scope.strip():
        raise TypeError("evidence_scope must be nonempty")
    expected = {
        row["fixture_id"]: row for row in mapping["fixture_rows"]
    }
    missing = sorted(set(expected) - set(executions))
    extra = sorted(set(executions) - set(expected))
    rows: list[JSONMap] = []
    covered_pairs: set[tuple[str, str]] = set()
    for fixture_id in sorted(set(expected) & set(executions)):
        execution = executions[fixture_id]
        events = execution.get("sink_events")
        raw_order = execution.get("raw_sink_order")
        expected_raw = [
            row["source_sink"] for row in expected[fixture_id]["mapped_attempts"]
        ]
        cardinality = isinstance(events, list) and len(events) == len(expected_raw)
        order_match = raw_order == expected_raw
        dispositions = bool(cardinality)
        if isinstance(events, list):
            for event in events:
                source = event.get("source_sink") if isinstance(event, Mapping) else None
                submission = event.get("simulator_submission") if isinstance(event, Mapping) else None
                acceptance = event.get("simulator_acceptance") if isinstance(event, Mapping) else None
                if (
                    not isinstance(source, Mapping)
                    or not isinstance(submission, Mapping)
                    or not isinstance(acceptance, Mapping)
                    or submission.get("status")
                    not in {
                        "SUBMITTED",
                        "NOT_SUBMITTED_STATE_ALREADY_SATISFIED",
                    }
                    or acceptance.get("status")
                    not in {
                        "ACCEPTED",
                        "REJECTED",
                        "NOOP_STATE_ALREADY_SATISFIED",
                    }
                    or event.get("client_observation") != _client_not_observed()
                    or event.get("server_outcome") != _server_not_observed()
                ):
                    dispositions = False
                    continue
                covered_pairs.add((str(source.get("channel")), str(source.get("operation"))))
        rows.append(
            {
                "fixture_id": fixture_id,
                "raw_order_matches_source_oracle": order_match,
                "event_cardinality_matches": cardinality,
                "all_simulator_dispositions_typed": dispositions,
                "execution_blocked": execution.get("execution_blocked"),
                "game_client_observed": False,
                "game_server_outcome_observed": False,
            }
        )
    all_complete = (
        not missing
        and not extra
        and all(
            row["raw_order_matches_source_oracle"]
            and row["event_cardinality_matches"]
            and row["all_simulator_dispositions_typed"]
            and row["execution_blocked"] is False
            for row in rows
        )
        and covered_pairs == SUPPORTED_OPERATION_COVERAGE_V5
    )
    core: JSONMap = {
        "schema": EXECUTION_COVERAGE_SCHEMA_V5,
        "policy_id": POLICY_ID,
        "mapping_receipt_sha256": mapping["content_address"]["sha256"],
        "evidence_scope": evidence_scope,
        "native_o2obridge_process_claimed": (
            evidence_scope == "NATIVE_O2OBRIDGE_PROCESS"
        ),
        "fixture_count": len(rows),
        "missing_fixture_ids": missing,
        "unexpected_fixture_ids": extra,
        "covered_operation_pairs": [
            {"channel": channel, "operation": operation}
            for channel, operation in sorted(covered_pairs)
        ],
        "rows": rows,
        "source_oracle_to_simulator_operation_coverage_complete": all_complete,
        "game_client_observed": False,
        "game_server_outcome_observed": False,
        "comparison_ready": False,
        "runner_registration_authorized": False,
    }
    return {**core, "content_address": _content_address(core)}


def validate_execution_coverage_receipt_v5(
    value: Mapping[str, Any],
) -> JSONMap:
    raw = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if raw.get("schema") != EXECUTION_COVERAGE_SCHEMA_V5:
        raise CatFuryOrderedSinkExecutorV5Error(
            "execution coverage receipt schema mismatch"
        )
    core = {key: item for key, item in raw.items() if key != "content_address"}
    if raw.get("content_address") != _content_address(core):
        raise CatFuryOrderedSinkExecutorV5Error(
            "execution coverage receipt content mismatch"
        )
    if raw.get("source_oracle_to_simulator_operation_coverage_complete") is not True:
        raise CatFuryOrderedSinkExecutorV5Error(
            "source-to-simulator execution coverage is incomplete"
        )
    if raw.get("missing_fixture_ids") != [] or raw.get("unexpected_fixture_ids") != []:
        raise CatFuryOrderedSinkExecutorV5Error(
            "execution coverage fixture partition is incomplete"
        )
    if raw.get("fixture_count") != 43:
        raise CatFuryOrderedSinkExecutorV5Error(
            "execution coverage must contain all 43 v4 fixtures"
        )
    expected_pairs = [
        {"channel": channel, "operation": operation}
        for channel, operation in sorted(SUPPORTED_OPERATION_COVERAGE_V5)
    ]
    if raw.get("covered_operation_pairs") != expected_pairs:
        raise CatFuryOrderedSinkExecutorV5Error(
            "execution coverage operation pairs are incomplete"
        )
    rows = raw.get("rows")
    if not isinstance(rows, list) or len(rows) != 43 or any(
        not isinstance(row, Mapping)
        or row.get("raw_order_matches_source_oracle") is not True
        or row.get("event_cardinality_matches") is not True
        or row.get("all_simulator_dispositions_typed") is not True
        or row.get("execution_blocked") is not False
        or row.get("game_client_observed") is not False
        or row.get("game_server_outcome_observed") is not False
        for row in rows
    ):
        raise CatFuryOrderedSinkExecutorV5Error(
            "execution coverage rows do not close"
        )
    for name in (
        "game_client_observed",
        "game_server_outcome_observed",
        "comparison_ready",
        "runner_registration_authorized",
    ):
        if raw.get(name) is not False:
            raise CatFuryOrderedSinkExecutorV5Error(f"{name} must remain false")
    native = raw.get("evidence_scope") == "NATIVE_O2OBRIDGE_PROCESS"
    if raw.get("native_o2obridge_process_claimed") is not native:
        raise CatFuryOrderedSinkExecutorV5Error(
            "native process evidence scope is inconsistent"
        )
    return raw


__all__ = (
    "CatFuryOrderedSinkExecutorV5Error",
    "CatOrderedSinkBridgeLikeV5",
    "CatSimulatorControlFacadeV5",
    "CatSimulatorControlResultV5",
    "CatSimulatorItemActionBindingV5",
    "EXECUTION_COVERAGE_SCHEMA_V5",
    "EXECUTION_SCHEMA_V5",
    "IMPLEMENTATION_REVISION",
    "MAPPING_RECEIPT_SCHEMA_V5",
    "PROFILE1_TARGET_CONTRACT_V5",
    "SUPPORTED_OPERATION_COVERAGE_V5",
    "audit_execution_coverage_v5",
    "build_operation_mapping_receipt_v5",
    "execute_cat_fury_ordered_sinks_v5",
    "validate_execution_coverage_receipt_v5",
    "validate_operation_mapping_receipt_v5",
)
