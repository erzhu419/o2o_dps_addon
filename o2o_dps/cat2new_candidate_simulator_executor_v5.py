"""Offline ordered simulator executor for Cat2_new candidate plans (v5).

The v4 contract describes what a learned candidate wants Cat2_new to do.  It
does not execute the request.  This additive module binds one content-addressed
v4 plan to one dynamic simulator generation and submits only operations for
which an exact simulator representation exists.

The boundary is intentionally strict:

* target units, items, and equipment require explicit typed bindings;
* an unrepresentable operation produces a typed omission and never a proxy
  spell;
* BODY order and compiler-generated FINALLY target restoration are preserved;
* every command state is checked against the dynamic generation/config and a
  fixed horizon;
* simulator acceptance/results are not WoW client or game-server evidence.

This closes an offline *interface* gate.  It cannot register a live or formal
comparison runner while Cat2_new is not deployed and client traces are absent.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .cat2new_candidate_executor_v4 import (
    CONTRACT_SHA256 as PLAN_CONTRACT_SHA256_V4,
    DEFAULT_INSTALLED_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_SAVEDVARIABLES,
    DEFAULT_SOURCE_ROOT,
    EXECUTOR_ID as PLAN_EXECUTOR_ID_V4,
    PLAN_SCHEMA as PLAN_SCHEMA_V4,
    build_readiness_report_v4,
    validate_candidate_action_plan_v4,
)
from .expert_policy import SwingQueueOp
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    CancelQueueResult,
    SetTargetResult,
)


JSONMap = dict[str, Any]

# The bridge rejects attempt IDs on state-only actions.  Only these actions can
# produce a later server-style result receipt in the current Warrior model.
_RESULT_BEARING_ACTION_REFS = frozenset(
    ACTION_KEY_TO_REF[action_key]
    for action_key in (
        "warrior.bloodthirst",
        "warrior.whirlwind",
        "warrior.slam",
        "warrior.execute",
        "warrior.hamstring",
        "warrior.pummel",
        "warrior.sunder_armor",
    )
)

EXECUTION_SCHEMA_V5 = "cat2new_candidate_simulator_execution/v5"
READINESS_SCHEMA_V5 = "cat2new_candidate_simulator_readiness/v5"
RUN_BINDING_SCHEMA_V5 = "cat2new_candidate_simulator_run_binding/v5"
BINDINGS_SCHEMA_V5 = "cat2new_candidate_simulator_operation_bindings/v5"
STATE_RECEIPT_SCHEMA_V5 = "cat2new_candidate_dynamic_state_receipt/v5"
EXECUTOR_ID_V5 = "cat2new.candidate.simulator.executor.v5"
IMPLEMENTATION_REVISION = "v5.2_terminal_wait_clipped_to_horizon"
CONTENT_ADDRESS_ALGORITHM = "sha256-canonical-json-v1"

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class Cat2NewCandidateSimulatorExecutorV5Error(RuntimeError):
    """A v5 binding, execution, or receipt invariant failed."""


class Cat2NewDynamicBridgeV5(Protocol):
    """Stable narrow seam implemented by current dynamic bridge versions."""

    def state(self) -> Mapping[str, Any]: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult: ...

    def set_target(self, target_index: int) -> SetTargetResult: ...

    def cancel_queue(self) -> CancelQueueResult: ...

    def wait(self, wait_ms: int) -> Mapping[str, Any]: ...

    def advance(self) -> Mapping[str, Any]: ...


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"value is not finite canonical JSON: {error}"
        ) from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _content_address(core: Mapping[str, Any]) -> JSONMap:
    return {
        "algorithm": CONTENT_ADDRESS_ALGORITHM,
        "scope": "canonical JSON document excluding content_address",
        "sha256": _sha256_json(core),
    }


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} must be lowercase SHA-256"
        )
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} must be a normalized identifier"
        )
    return value


def _strict_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} must be an integer"
        )
    if minimum is not None and value < minimum:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} must be at least {minimum}"
        )
    return value


def _strict_json_copy(value: Any, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} must be an object"
        )
    try:
        copy = json.loads(_canonical_bytes(value).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} could not be normalized: {error}"
        ) from error
    if not isinstance(copy, dict):
        raise AssertionError("canonical object did not remain an object")
    return copy


@dataclass(frozen=True)
class Cat2NewSimulatorRunBindingV5:
    """Immutable identity and horizon for one already-loaded environment."""

    run_id: str
    request_sha256: str
    seed: int
    dynamic_config_sha256: str
    dynamic_load_receipt_sha256: str
    environment_generation: int
    horizon_end_ms: int
    schema: str = RUN_BINDING_SCHEMA_V5
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != RUN_BINDING_SCHEMA_V5:
            raise ValueError("run binding schema mismatch")
        _identifier(self.run_id, "run_id")
        _sha256(self.request_sha256, "request_sha256")
        _sha256(self.dynamic_config_sha256, "dynamic_config_sha256")
        _sha256(
            self.dynamic_load_receipt_sha256,
            "dynamic_load_receipt_sha256",
        )
        _strict_int(self.seed, "seed")
        _strict_int(
            self.environment_generation,
            "environment_generation",
            minimum=1,
        )
        _strict_int(self.horizon_end_ms, "horizon_end_ms", minimum=1)
        object.__setattr__(self, "content_sha256", _sha256_json(self._core()))

    def _core(self) -> JSONMap:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "request_sha256": self.request_sha256,
            "seed": self.seed,
            "dynamic_config_sha256": self.dynamic_config_sha256,
            "dynamic_load_receipt_sha256": self.dynamic_load_receipt_sha256,
            "environment_generation": self.environment_generation,
            "horizon_end_ms": self.horizon_end_ms,
        }

    def to_wire(self) -> JSONMap:
        return {**self._core(), "content_sha256": self.content_sha256}


@dataclass(frozen=True)
class UnitTargetBindingV5:
    unit: str
    target_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise TypeError("unit must be nonempty")
        _strict_int(self.target_index, "target_index", minimum=0)

    def to_wire(self) -> JSONMap:
        return {"unit": self.unit, "target_index": self.target_index}


@dataclass(frozen=True)
class SimulatorItemBindingV5:
    """Bind a Cat2 locator to an exact simulator item action."""

    locator_kind: str
    locator: str
    action: ActionRef

    def __post_init__(self) -> None:
        if self.locator_kind not in {"TRINKET_SLOT", "NAMED_ITEM"}:
            raise ValueError("unsupported item locator_kind")
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise TypeError("item locator must be nonempty")
        if not isinstance(self.action, ActionRef) or self.action.item_id <= 0:
            raise TypeError("item binding requires an item ActionRef")
        if self.action.spell_id or self.action.other_id:
            raise ValueError("item binding may not masquerade as a spell/other action")

    def to_wire(self) -> JSONMap:
        return {
            "locator_kind": self.locator_kind,
            "locator": self.locator,
            "action": self.action.to_wire(),
        }


@dataclass(frozen=True)
class SimulatorEquipmentBindingV5:
    item_name: str
    item_id: int
    slot: int

    def __post_init__(self) -> None:
        if not isinstance(self.item_name, str) or not self.item_name.strip():
            raise TypeError("equipment item_name must be nonempty")
        _strict_int(self.item_id, "equipment item_id", minimum=1)
        if not 1 <= _strict_int(self.slot, "equipment slot") <= 19:
            raise ValueError("equipment slot must be in [1,19]")

    def to_wire(self) -> JSONMap:
        return {
            "item_name": self.item_name,
            "item_id": self.item_id,
            "slot": self.slot,
        }


@dataclass(frozen=True)
class Cat2NewSimulatorOperationBindingsV5:
    """Serializable exact mappings; no callback or heuristic is hidden here."""

    target_units: tuple[UnitTargetBindingV5, ...] = ()
    item_actions: tuple[SimulatorItemBindingV5, ...] = ()
    equipment_items: tuple[SimulatorEquipmentBindingV5, ...] = ()
    eligible_cleave_enemy_count: int | None = None
    schema: str = BINDINGS_SCHEMA_V5
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != BINDINGS_SCHEMA_V5:
            raise ValueError("operation bindings schema mismatch")
        _typed_tuple(self.target_units, UnitTargetBindingV5, "target_units")
        _typed_tuple(self.item_actions, SimulatorItemBindingV5, "item_actions")
        _typed_tuple(
            self.equipment_items,
            SimulatorEquipmentBindingV5,
            "equipment_items",
        )
        _unique([row.unit for row in self.target_units], "target unit")
        _unique(
            [(row.locator_kind, row.locator) for row in self.item_actions],
            "item locator",
        )
        _unique(
            [(row.item_name, row.slot) for row in self.equipment_items],
            "equipment name/slot",
        )
        if self.eligible_cleave_enemy_count is not None:
            _strict_int(
                self.eligible_cleave_enemy_count,
                "eligible_cleave_enemy_count",
                minimum=0,
            )
        object.__setattr__(self, "content_sha256", _sha256_json(self._core()))

    def _core(self) -> JSONMap:
        return {
            "schema": self.schema,
            "target_units": [row.to_wire() for row in self.target_units],
            "item_actions": [row.to_wire() for row in self.item_actions],
            "equipment_items": [row.to_wire() for row in self.equipment_items],
            "eligible_cleave_enemy_count": self.eligible_cleave_enemy_count,
        }

    def to_wire(self) -> JSONMap:
        return {**self._core(), "content_sha256": self.content_sha256}


@dataclass(frozen=True)
class SimulatorControlResultV5:
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


@dataclass(frozen=True)
class SimulatorEquipmentResultV5:
    accepted: bool
    consumes_decision: bool
    item_id: int
    slot: int
    state: Mapping[str, Any]
    mechanics_modeled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be boolean")
        if not isinstance(self.consumes_decision, bool):
            raise TypeError("consumes_decision must be boolean")
        _strict_int(self.item_id, "item_id", minimum=1)
        if not 1 <= _strict_int(self.slot, "slot") <= 19:
            raise ValueError("slot must be in [1,19]")
        if not isinstance(self.state, Mapping):
            raise TypeError("state must be a mapping")
        if not isinstance(self.mechanics_modeled, bool):
            raise TypeError("mechanics_modeled must be boolean")


def _typed_tuple(value: Any, row_type: type, label: str) -> None:
    if not isinstance(value, tuple) or any(not isinstance(x, row_type) for x in value):
        raise TypeError(f"{label} must be a tuple of {row_type.__name__}")


def _unique(values: Sequence[Any], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label} binding")


def _dynamic_identity(state: Mapping[str, Any]) -> tuple[int, str]:
    lifecycle = state.get("dynamic_team_background")
    semantics = state.get("dynamic_target_semantics")
    if not isinstance(lifecycle, Mapping) or not isinstance(semantics, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "dynamic lifecycle and target-semantics state blocks are required"
        )
    generations = {
        lifecycle.get("environment_generation"),
        semantics.get("environment_generation"),
    }
    digests = {lifecycle.get("config_digest"), semantics.get("config_digest")}
    if len(generations) != 1 or len(digests) != 1:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "dynamic state blocks disagree on generation/config"
        )
    generation = _strict_int(next(iter(generations)), "dynamic generation", minimum=1)
    digest = _sha256(next(iter(digests)), "dynamic config digest")
    return generation, digest


def _state_receipt(
    state: Mapping[str, Any], run: Cat2NewSimulatorRunBindingV5
) -> JSONMap:
    copy = _strict_json_copy(state, "simulator state")
    generation, digest = _dynamic_identity(copy)
    if generation != run.environment_generation:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "simulator state environment generation differs from run binding"
        )
    if digest != run.dynamic_config_sha256:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "simulator state dynamic config differs from run binding"
        )
    time_ms = _strict_int(copy.get("time_ms"), "state.time_ms", minimum=0)
    target_index = copy.get("target_index")
    if target_index is not None:
        _strict_int(target_index, "state.target_index", minimum=0)
    if not isinstance(copy.get("needs_input"), bool):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "state.needs_input must be boolean"
        )
    if not isinstance(copy.get("finished"), bool):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "state.finished must be boolean"
        )
    core: JSONMap = {
        "schema": STATE_RECEIPT_SCHEMA_V5,
        "time_ms": time_ms,
        "target_index": target_index,
        "needs_input": copy.get("needs_input"),
        "finished": copy.get("finished"),
        "queued_swing": deepcopy(copy.get("queued_swing")),
        "environment_generation": generation,
        "dynamic_config_sha256": digest,
        "dynamic_team_lifecycle": deepcopy(copy["dynamic_team_background"]),
        "dynamic_target_semantics": deepcopy(copy["dynamic_target_semantics"]),
        "raw_state_sha256": _sha256_json(copy),
    }
    return {**core, "content_address": _content_address(core)}


def _omission(code: str, message: str, operation: Mapping[str, Any]) -> JSONMap:
    return {
        "code": code,
        "scope": "SIMULATOR_OPERATION",
        "lane": operation["lane"],
        "intent": operation["intent"],
        "message": message,
        "comparison_fatal": True,
        "surrogate_action_used": False,
    }


def _event_core(
    *,
    sequence: int,
    region: str,
    operation: Mapping[str, Any],
    before: Mapping[str, Any],
) -> JSONMap:
    return {
        "sequence": sequence,
        "region": region,
        "plan_ordinal": operation["ordinal"],
        "operation_id": operation["operation_id"],
        "lane": operation["lane"],
        "intent": operation["intent"],
        "arguments": deepcopy(operation["arguments"]),
        "source_route": deepcopy(operation["route"]),
        "state_before": deepcopy(dict(before)),
        "dispatch": {"status": "NOT_EVALUATED"},
        "simulator_acceptance": {"status": "NOT_EVALUATED"},
        "decision_consumption": {
            "status": "NOT_EVALUATED",
            "consumes_decision": None,
        },
        "simulator_outcome": {"status": "NOT_EVALUATED"},
        "typed_omission": None,
        "state_after": deepcopy(dict(before)),
        "client_observation": {
            "status": "NOT_OBSERVED",
            "simulator_acceptance_is_client_acceptance": False,
        },
        "game_server_outcome": {
            "status": "NOT_OBSERVED",
            "simulator_result_is_game_server_outcome": False,
        },
        "traversal": {"status": "CONTINUE", "reason": None},
    }


def _finish_event(event: JSONMap, previous_sha256: str | None) -> JSONMap:
    event["chain"] = {"previous_event_sha256": previous_sha256}
    digest = _sha256_json(event)
    event["chain"]["event_sha256"] = digest
    return event


def _not_submitted(event: JSONMap, status: str, reason: str) -> None:
    event["dispatch"] = {"status": status, "reason": reason}
    event["simulator_acceptance"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}
    event["decision_consumption"] = {
        "status": "NOT_APPLICABLE_NOT_SUBMITTED",
        "consumes_decision": None,
    }
    event["simulator_outcome"] = {"status": "NOT_APPLICABLE_NOT_SUBMITTED"}
    event["traversal"] = {"status": "STOP", "reason": reason}


def _available_action(
    bridge: Cat2NewDynamicBridgeV5, action: ActionRef
) -> AvailableAction | None:
    rows = bridge.actions()
    if not isinstance(rows, list) or any(not isinstance(row, AvailableAction) for row in rows):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "bridge.actions must return AvailableAction rows"
        )
    return {row.action: row for row in rows}.get(action)


def _act(
    bridge: Cat2NewDynamicBridgeV5,
    event: JSONMap,
    action: ActionRef,
    attempt_id: str,
    run: Cat2NewSimulatorRunBindingV5,
    *,
    expected_triggers_gcd: bool,
    source_stops: bool = False,
) -> tuple[JSONMap, bool, bool, str | None]:
    available = _available_action(bridge, action)
    if available is None:
        event["dispatch"] = {
            "status": "NOT_SUBMITTED_ACTION_ABSENT",
            "action": action.to_wire(),
        }
        event["simulator_acceptance"] = {"status": "REJECTED_ACTION_ABSENT"}
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE_REJECTED"}
        event["traversal"] = {
            "status": "STOP",
            "reason": "ACTION_ABSENT_FROM_SIMULATOR_SPELLBOOK",
        }
        return event, False, True, None
    if available.triggers_gcd is not expected_triggers_gcd:
        omission = _omission(
            "ACTION_GCD_CLASSIFICATION_MISMATCH",
            "the exact simulator action has a different GCD classification",
            event,
        )
        event["typed_omission"] = omission
        _not_submitted(
            event,
            "NOT_SUBMITTED_GCD_CLASSIFICATION_MISMATCH",
            omission["code"],
        )
        return event, False, True, None
    if not available.legal:
        event["dispatch"] = {
            "status": "NOT_SUBMITTED_ACTION_ILLEGAL",
            "action": action.to_wire(),
            "available_action": _available_to_wire(available),
        }
        event["simulator_acceptance"] = {"status": "REJECTED_ACTION_ILLEGAL"}
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "NOT_APPLICABLE_REJECTED"}
        event["traversal"] = {"status": "STOP", "reason": "ACTION_ILLEGAL"}
        return event, False, True, None
    result_attempt_id = (
        attempt_id if action in _RESULT_BEARING_ACTION_REFS else None
    )
    result = bridge.act(action, attempt_id=result_attempt_id)
    if not isinstance(result, ActResult):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "bridge.act must return ActResult"
        )
    after = _state_receipt(result.state, run)
    event["dispatch"] = {
        "status": "SUBMITTED",
        "command": "act",
        "attempt_id": result_attempt_id,
        "action": action.to_wire(),
        "available_action": _available_to_wire(available),
    }
    event["simulator_acceptance"] = {
        "status": "ACCEPTED" if result.casted else "REJECTED",
        "evidence": "bridge.act.casted",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
        "consumes_decision": result.consumes_decision,
        "expected": expected_triggers_gcd,
    }
    event["simulator_outcome"] = (
        {
            "status": "PENDING_TYPED_SIMULATOR_RESULT",
            "attempt_id": result_attempt_id,
        }
        if result.casted and result_attempt_id is not None
        else {
            "status": (
                "ACCEPTED_NO_RESULT_EXPECTED" if result.casted else "REJECTED"
            ),
            "attempt_id": None,
        }
    )
    event["state_after"] = after
    stop = result.consumes_decision or source_stops
    event["traversal"] = {
        "status": "STOP" if stop else "CONTINUE",
        "reason": "DECISION_CONSUMED" if result.consumes_decision else (
            "SOURCE_STOPPING_ACTION_ATTEMPT" if source_stops else None
        ),
    }
    return event, result.consumes_decision, stop, result_attempt_id


def _available_to_wire(row: AvailableAction) -> JSONMap:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
    }


def _structural_control(value: Any, method_name: str) -> SimulatorControlResultV5:
    if isinstance(value, SimulatorControlResultV5):
        return value
    accepted = getattr(value, "accepted", None)
    consumes = getattr(value, "consumes_decision", None)
    state = getattr(value, "state", None)
    if not isinstance(accepted, bool) or not isinstance(consumes, bool) or not isinstance(state, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"bridge.{method_name} must return a typed control result"
        )
    return SimulatorControlResultV5(accepted, consumes, state)


def _control(
    bridge: Any,
    event: JSONMap,
    method_name: str,
    run: Cat2NewSimulatorRunBindingV5,
    *,
    omission_code: str,
    omission_message: str,
) -> tuple[JSONMap, bool, bool]:
    method = getattr(bridge, method_name, None)
    if not callable(method):
        omission = _omission(omission_code, omission_message, event)
        event["typed_omission"] = omission
        _not_submitted(event, "NOT_SUBMITTED_CAPABILITY_ABSENT", omission_code)
        return event, False, True
    result = _structural_control(method(), method_name)
    event["dispatch"] = {"status": "SUBMITTED", "command": method_name}
    event["simulator_acceptance"] = {
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "evidence": f"bridge.{method_name}.accepted",
    }
    event["decision_consumption"] = {
        "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
        "consumes_decision": result.consumes_decision,
        "expected": False,
    }
    event["simulator_outcome"] = {
        "status": "CONTROL_ACCEPTED" if result.accepted else "CONTROL_REJECTED"
    }
    event["state_after"] = _state_receipt(result.state, run)
    stop = result.consumes_decision
    event["traversal"] = {
        "status": "STOP" if stop else "CONTINUE",
        "reason": "UNEXPECTED_CONTROL_DECISION_CONSUMPTION" if stop else None,
    }
    return event, result.consumes_decision, stop


def _target_binding(
    bindings: Cat2NewSimulatorOperationBindingsV5, unit: str
) -> UnitTargetBindingV5 | None:
    return next((row for row in bindings.target_units if row.unit == unit), None)


def _item_binding(
    bindings: Cat2NewSimulatorOperationBindingsV5,
    kind: str,
    locator: str,
) -> SimulatorItemBindingV5 | None:
    return next(
        (
            row
            for row in bindings.item_actions
            if row.locator_kind == kind and row.locator == locator
        ),
        None,
    )


def _equipment_binding(
    bindings: Cat2NewSimulatorOperationBindingsV5,
    item_name: str,
    slot: int,
) -> SimulatorEquipmentBindingV5 | None:
    return next(
        (
            row
            for row in bindings.equipment_items
            if row.item_name == item_name and row.slot == slot
        ),
        None,
    )


def _set_target(
    bridge: Any,
    event: JSONMap,
    target_index: int,
    run: Cat2NewSimulatorRunBindingV5,
    *,
    command: str,
) -> tuple[JSONMap, bool]:
    method = getattr(bridge, "set_target", None)
    if not callable(method):
        omission = _omission(
            "SET_TARGET_CAPABILITY_ABSENT",
            "dynamic bridge has no exact target-index control",
            event,
        )
        event["typed_omission"] = omission
        _not_submitted(event, "NOT_SUBMITTED_CAPABILITY_ABSENT", omission["code"])
        return event, True
    result = method(target_index)
    if not isinstance(result, SetTargetResult):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "bridge.set_target must return SetTargetResult"
        )
    after = _state_receipt(result.state, run)
    accepted = result.target_index == target_index and after["target_index"] == target_index
    event["dispatch"] = {
        "status": "SUBMITTED",
        "command": command,
        "target_index": target_index,
    }
    event["simulator_acceptance"] = {
        "status": "ACCEPTED" if accepted else "REJECTED_POSTCONDITION",
        "changed": result.changed,
        "observed_target_index": result.target_index,
    }
    event["decision_consumption"] = {
        "status": "NOT_CONSUMED",
        "consumes_decision": False,
    }
    event["simulator_outcome"] = {
        "status": "TARGET_SELECTED" if accepted else "TARGET_POSTCONDITION_FAILED"
    }
    event["state_after"] = after
    event["traversal"] = {
        "status": "CONTINUE" if accepted else "STOP",
        "reason": None if accepted else "TARGET_POSTCONDITION_FAILED",
    }
    return event, not accepted


def _execute_body_operation(
    bridge: Any,
    operation: Mapping[str, Any],
    event: JSONMap,
    run: Cat2NewSimulatorRunBindingV5,
    bindings: Cat2NewSimulatorOperationBindingsV5,
    captures: dict[str, int],
    attempt_id: str,
) -> tuple[JSONMap, bool, bool, str | None]:
    lane = operation["lane"]
    intent = operation["intent"]
    args = operation["arguments"]

    if intent == "KEEP":
        event["dispatch"] = {"status": "NO_SINK_BY_DESIGN"}
        event["simulator_acceptance"] = {"status": "NOOP"}
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "NOOP"}
        event["traversal"] = {"status": "CONTINUE", "reason": None}
        return event, False, False, None

    if lane == "target" and intent == "AUTO_NEAREST_6":
        omission = _omission(
            "AUTO_NEAREST_6_GEOMETRY_UNAVAILABLE",
            "dynamic bridge has no distance/facing/line-of-sight target ordering",
            operation,
        )
        event["typed_omission"] = omission
        _not_submitted(event, "NOT_SUBMITTED_NO_EXACT_TARGET_ORDER", omission["code"])
        return event, False, True, None

    if lane == "target" and intent == "SET_EXACT_UNIT":
        binding = _target_binding(bindings, args["unit"])
        if binding is None:
            omission = _omission(
                "EXACT_UNIT_TARGET_BINDING_MISSING",
                "the Cat2 unit token has no explicit simulator target index",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_BINDING_MISSING", omission["code"])
            return event, False, True, None
        current_index = event["state_before"].get("target_index")
        if not isinstance(current_index, int):
            omission = _omission(
                "CURRENT_TARGET_CAPTURE_UNAVAILABLE",
                "target restoration requires an exact current simulator target index",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_CAPTURE_MISSING", omission["code"])
            return event, False, True, None
        event, blocked = _set_target(
            bridge, event, binding.target_index, run, command="set_target"
        )
        if not blocked:
            captures[operation["operation_id"]] = current_index
        return event, False, blocked, None

    if lane == "autoattack":
        if intent == "START":
            event, consumed, blocked = _control(
                bridge,
                event,
                "start_attack",
                run,
                omission_code="START_ATTACK_CAPABILITY_ABSENT",
                omission_message="dynamic bridge has no start-attack control",
            )
        else:
            event, consumed, blocked = _control(
                bridge,
                event,
                "stop_attack",
                run,
                omission_code="STOP_ATTACK_CAPABILITY_ABSENT",
                omission_message="current dynamic bridge has no stop-attack control",
            )
        return event, consumed, blocked, None

    if lane == "item":
        if intent == "USE_NAMED_ITEM" and args["self_target"]:
            omission = _omission(
                "SELF_TARGET_ITEM_SEMANTICS_UNAVAILABLE",
                "simulator item actions cannot establish Cat2 self-target/save/restore semantics",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_SELF_TARGET_UNMODELED", omission["code"])
            return event, False, True, None
        kind = "TRINKET_SLOT" if intent == "USE_TRINKET_SLOT" else "NAMED_ITEM"
        locator = str(args["slot"]) if kind == "TRINKET_SLOT" else args["item_name"]
        binding = _item_binding(bindings, kind, locator)
        if binding is None:
            omission = _omission(
                "ITEM_ACTION_BINDING_MISSING",
                "item intent has no exact simulator item ActionRef",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_BINDING_MISSING", omission["code"])
            return event, False, True, None
        return _act(
            bridge,
            event,
            binding.action,
            attempt_id,
            run,
            expected_triggers_gcd=False,
        )

    if lane == "equipment":
        binding = _equipment_binding(bindings, args["item_name"], args["slot"])
        if binding is None:
            omission = _omission(
                "EQUIPMENT_BINDING_MISSING",
                "equipment intent has no exact item-id/slot binding",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_BINDING_MISSING", omission["code"])
            return event, False, True, None
        method = getattr(bridge, "equip_item", None)
        if not callable(method):
            omission = _omission(
                "EQUIPMENT_CONTROL_CAPABILITY_ABSENT",
                "current dynamic bridge does not model equipment mutation",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_CAPABILITY_ABSENT", omission["code"])
            return event, False, True, None
        result = method(binding.item_id, binding.slot)
        if not isinstance(result, SimulatorEquipmentResultV5):
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "bridge.equip_item must return SimulatorEquipmentResultV5"
            )
        if result.item_id != binding.item_id or result.slot != binding.slot:
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "bridge.equip_item result differs from exact binding"
            )
        event["dispatch"] = {
            "status": "SUBMITTED",
            "command": "equip_item",
            "item_id": binding.item_id,
            "slot": binding.slot,
        }
        event["simulator_acceptance"] = {
            "status": "ACCEPTED" if result.accepted else "REJECTED",
            "evidence": "typed equipment result",
        }
        event["decision_consumption"] = {
            "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
            "consumes_decision": result.consumes_decision,
            "expected": False,
        }
        event["simulator_outcome"] = {
            "status": "EQUIPPED" if result.accepted else "REJECTED",
            "mechanics_modeled": result.mechanics_modeled,
            "equipment_dependent_state_refresh_observed": result.accepted,
        }
        event["state_after"] = _state_receipt(result.state, run)
        if not result.mechanics_modeled:
            event["typed_omission"] = _omission(
                "EQUIPMENT_COMBAT_MECHANICS_NOT_MODELED",
                "equipment control changed a sidecar but not simulator combat mechanics",
                operation,
            )
        blocked = bool(event["typed_omission"]) or result.consumes_decision
        event["traversal"] = {
            "status": "STOP" if blocked else "CONTINUE",
            "reason": (
                event["typed_omission"]["code"]
                if event["typed_omission"]
                else "UNEXPECTED_CONTROL_DECISION_CONSUMPTION"
                if result.consumes_decision
                else None
            ),
        }
        return event, result.consumes_decision, blocked, None

    if lane == "swing_queue" and intent == "CANCEL":
        method = getattr(bridge, "cancel_queue", None)
        if not callable(method):
            omission = _omission(
                "QUEUE_CANCEL_CAPABILITY_ABSENT",
                "dynamic bridge has no next-swing cancellation control",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_CAPABILITY_ABSENT", omission["code"])
            return event, False, True, None
        result = method()
        if not isinstance(result, CancelQueueResult):
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "bridge.cancel_queue must return CancelQueueResult"
            )
        event["dispatch"] = {"status": "SUBMITTED", "command": "cancel_queue"}
        event["simulator_acceptance"] = {
            "status": "ACCEPTED" if result.canceled else "NO_ACTIVE_QUEUE",
            "evidence": "bridge.cancel_queue.canceled",
        }
        event["decision_consumption"] = {
            "status": "CONSUMED" if result.consumes_decision else "NOT_CONSUMED",
            "consumes_decision": result.consumes_decision,
            "expected": False,
        }
        event["simulator_outcome"] = {
            "status": "QUEUE_CANCELED" if result.canceled else "QUEUE_ALREADY_INACTIVE"
        }
        event["state_after"] = _state_receipt(result.state, run)
        blocked = result.consumes_decision
        event["traversal"] = {
            "status": "STOP" if blocked else "CONTINUE",
            "reason": "UNEXPECTED_CONTROL_DECISION_CONSUMPTION" if blocked else None,
        }
        return event, result.consumes_decision, blocked, None

    if lane == "swing_queue":
        if intent == "AUTO_HS_OR_CLEAVE":
            count = bindings.eligible_cleave_enemy_count
            if count is None:
                omission = _omission(
                    "ELIGIBLE_CLEAVE_ENEMY_COUNT_MISSING",
                    "Cat2 front/range filtered enemy count was not bound",
                    operation,
                )
                event["typed_omission"] = omission
                _not_submitted(event, "NOT_SUBMITTED_BINDING_MISSING", omission["code"])
                return event, False, True, None
            queue = SwingQueueOp.CLEAVE if count >= 2 else SwingQueueOp.HEROIC_STRIKE
            event["resolved_auto_queue"] = {
                "eligible_cleave_enemy_count": count,
                "resolved": queue.value,
            }
        else:
            queue = (
                SwingQueueOp.HEROIC_STRIKE
                if intent == "HEROIC_STRIKE"
                else SwingQueueOp.CLEAVE
            )
        return _act(
            bridge,
            event,
            QUEUE_REFS[queue],
            attempt_id,
            run,
            expected_triggers_gcd=False,
        )

    if lane == "cast_control" and intent == "STOP_CAST":
        event, consumed, blocked = _control(
            bridge,
            event,
            "stop_cast",
            run,
            omission_code="STOP_CAST_CAPABILITY_ABSENT",
            omission_message="dynamic bridge has no hardcast cancellation control",
        )
        return event, consumed, blocked, None

    if intent == "CAST_ACTION":
        action = ACTION_KEY_TO_REF.get(args["action_key"])
        if action is None:
            omission = _omission(
                "CANONICAL_ACTION_MAPPING_MISSING",
                "candidate action has no exact simulator ActionRef",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_MAPPING_MISSING", omission["code"])
            return event, False, True, None
        return _act(
            bridge,
            event,
            action,
            attempt_id,
            run,
            expected_triggers_gcd=lane in {"stance", "gcd"},
            source_stops=str(
                operation["route"].get("traversal_if_gate_matches", "")
            ).startswith("STOP_"),
        )

    if lane == "wait" and intent == "WAIT":
        before_time = event["state_before"]["time_ms"]
        requested_wait_ms = args["wait_ms"]
        remaining_ms = run.horizon_end_ms - before_time
        if remaining_ms <= 0:
            omission = _omission(
                "WAIT_AT_OR_AFTER_BOUND_HORIZON",
                "the simulator requested another decision at or after the immutable horizon",
                operation,
            )
            event["typed_omission"] = omission
            _not_submitted(event, "NOT_SUBMITTED_HORIZON_ALREADY_REACHED", omission["code"])
            return event, False, True, None
        wait_ms = min(requested_wait_ms, remaining_ms)
        horizon_clipped = wait_ms != requested_wait_ms
        after_state = bridge.wait(wait_ms)
        if not isinstance(after_state, Mapping):
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "bridge.wait must return simulator state"
            )
        scheduled = _state_receipt(after_state, run)
        advanced = False
        if scheduled["needs_input"] is False and scheduled["finished"] is not True:
            advance = getattr(bridge, "advance", None)
            if not callable(advance):
                omission = _omission(
                    "WAIT_ADVANCE_CAPABILITY_ABSENT",
                    "bridge accepted WAIT but cannot advance to its next decision",
                    operation,
                )
                event["typed_omission"] = omission
                event["dispatch"] = {
                    "status": "PARTIALLY_SUBMITTED_WAIT_SCHEDULED",
                    "commands": ["wait"],
                    "wait_ms": wait_ms,
                    "requested_wait_ms": requested_wait_ms,
                    "horizon_clipped": horizon_clipped,
                }
                event["simulator_acceptance"] = {
                    "status": "WAIT_SCHEDULED_ADVANCE_UNAVAILABLE"
                }
                event["decision_consumption"] = {
                    "status": "CONSUMED_BY_INTENTIONAL_WAIT",
                    "consumes_decision": True,
                }
                event["simulator_outcome"] = {
                    "status": "WAIT_TERMINAL_STATE_UNOBSERVED"
                }
                event["state_after"] = scheduled
                event["traversal"] = {
                    "status": "STOP",
                    "reason": omission["code"],
                }
                return event, True, True, None
            after_state = advance()
            if not isinstance(after_state, Mapping):
                raise Cat2NewCandidateSimulatorExecutorV5Error(
                    "bridge.advance must return simulator state"
                )
            advanced = True
        after = _state_receipt(after_state, run)
        wait_complete = (
            after["finished"] is True
            or after["time_ms"] >= before_time + wait_ms
        )
        event["dispatch"] = {
            "status": "SUBMITTED",
            "commands": ["wait", "advance"] if advanced else ["wait"],
            "wait_ms": wait_ms,
            "requested_wait_ms": requested_wait_ms,
            "horizon_clipped": horizon_clipped,
            "scheduled_state": scheduled,
        }
        event["simulator_acceptance"] = {
            "status": "ACCEPTED" if wait_complete else "REJECTED_POSTCONDITION",
            "evidence": "bound wait/advance state",
        }
        event["decision_consumption"] = {
            "status": "CONSUMED_BY_INTENTIONAL_WAIT",
            "consumes_decision": True,
        }
        event["simulator_outcome"] = {
            "status": (
                "WAIT_CLIPPED_TO_HORIZON"
                if wait_complete and horizon_clipped
                else "WAIT_APPLIED"
                if wait_complete
                else "WAIT_DURATION_NOT_REACHED"
            )
        }
        event["state_after"] = after
        if not wait_complete:
            event["typed_omission"] = _omission(
                "WAIT_DURATION_POSTCONDITION_FAILED",
                "wait/advance returned before the requested duration without encounter completion",
                operation,
            )
        event["traversal"] = {
            "status": "STOP",
            "reason": (
                "INTENTIONAL_WAIT_CLIPPED_TO_HORIZON"
                if wait_complete and horizon_clipped
                else "INTENTIONAL_WAIT_NO_FALLBACK"
                if wait_complete
                else "WAIT_DURATION_POSTCONDITION_FAILED"
            ),
        }
        return event, True, True, None

    raise Cat2NewCandidateSimulatorExecutorV5Error(
        f"unhandled v4 operation {lane}:{intent}"
    )


def _execute_finally_operation(
    bridge: Any,
    operation: Mapping[str, Any],
    event: JSONMap,
    run: Cat2NewSimulatorRunBindingV5,
    captures: Mapping[str, int],
) -> tuple[JSONMap, bool]:
    if operation["lane"] != "target" or operation["intent"] != "RESTORE_CAPTURED_TARGET":
        omission = _omission(
            "UNKNOWN_FINALLY_OPERATION",
            "v5 recognizes only compiler-generated target restoration",
            operation,
        )
        event["typed_omission"] = omission
        _not_submitted(event, "NOT_SUBMITTED_UNKNOWN_FINALLY", omission["code"])
        return event, True
    source_id = operation["arguments"].get("captured_by_operation_id")
    target_index = captures.get(source_id)
    if target_index is None:
        event["dispatch"] = {
            "status": "NOOP_CAPTURE_NOT_ESTABLISHED",
            "captured_by_operation_id": source_id,
        }
        event["simulator_acceptance"] = {"status": "NOOP"}
        event["decision_consumption"] = {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
        }
        event["simulator_outcome"] = {"status": "RESTORE_NOT_REQUIRED"}
        event["traversal"] = {"status": "CONTINUE", "reason": None}
        return event, False
    return _set_target(
        bridge,
        event,
        target_index,
        run,
        command="set_target_finally_restore",
    )


def _serialize_simulator_results(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize_simulator_results(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _serialize_simulator_results(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serialize_simulator_results(item) for item in value]
    if isinstance(value, ActionRef):
        return value.to_wire()
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "simulator result contains non-finite number"
            )
        return value
    raise Cat2NewCandidateSimulatorExecutorV5Error(
        f"simulator result contains unsupported {type(value).__name__}"
    )


def execute_cat2new_candidate_plan_v5(
    bridge: Cat2NewDynamicBridgeV5,
    plan: Mapping[str, Any],
    *,
    policy_state_snapshot: Mapping[str, Any],
    simulator_request: Mapping[str, Any],
    dynamic_load_receipt: Any,
    run_binding: Cat2NewSimulatorRunBindingV5,
    operation_bindings: Cat2NewSimulatorOperationBindingsV5 = (
        Cat2NewSimulatorOperationBindingsV5()
    ),
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    verify_live_source: bool = True,
) -> JSONMap:
    """Execute one v4 plan in exact BODY/FINALLY order and return receipts."""

    if not isinstance(run_binding, Cat2NewSimulatorRunBindingV5):
        raise TypeError("run_binding must be Cat2NewSimulatorRunBindingV5")
    if not isinstance(operation_bindings, Cat2NewSimulatorOperationBindingsV5):
        raise TypeError(
            "operation_bindings must be Cat2NewSimulatorOperationBindingsV5"
        )
    validated_plan = validate_candidate_action_plan_v4(
        plan,
        source_root=source_root,
        manifest_path=manifest_path,
        verify_live_source=verify_live_source,
    )
    if validated_plan.get("schema") != PLAN_SCHEMA_V4:
        raise Cat2NewCandidateSimulatorExecutorV5Error("v4 plan schema mismatch")
    snapshot = _strict_json_copy(policy_state_snapshot, "policy_state_snapshot")
    snapshot_sha = _sha256_json(snapshot)
    expected_snapshot_sha = validated_plan["request"]["state_snapshot_sha256"]
    if snapshot_sha != expected_snapshot_sha:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "policy state snapshot SHA-256 differs from v4 plan binding"
        )
    request_copy = _strict_json_copy(simulator_request, "simulator_request")
    if _sha256_json(request_copy) != run_binding.request_sha256:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "simulator request SHA-256 differs from run binding"
        )
    load_receipt_copy = _serialize_simulator_results(dynamic_load_receipt)
    if not isinstance(load_receipt_copy, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "dynamic_load_receipt must serialize to an object"
        )
    if _sha256_json(load_receipt_copy) != run_binding.dynamic_load_receipt_sha256:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "dynamic load receipt SHA-256 differs from run binding"
        )
    receipt_generation = load_receipt_copy.get("environment_generation")
    receipt_digest = load_receipt_copy.get("config_digest")
    if (
        receipt_generation != run_binding.environment_generation
        or receipt_digest != run_binding.dynamic_config_sha256
    ):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "dynamic load receipt identity differs from run binding"
        )

    initial_state = bridge.state()
    if not isinstance(initial_state, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "bridge.state must return simulator state"
        )
    initial = _state_receipt(initial_state, run_binding)
    if initial["time_ms"] > run_binding.horizon_end_ms:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "initial simulator time exceeds bound horizon"
        )
    if initial["needs_input"] is not True or initial["finished"] is True:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "candidate plan requires a live simulator decision boundary"
        )

    events: list[JSONMap] = []
    captures: dict[str, int] = {}
    current = initial
    body_stopped = False
    execution_failed = False
    consumed = not bool(initial_state.get("needs_input", True))
    omissions: list[JSONMap] = []
    attempt_ids: list[str] = []
    previous_sha: str | None = None

    for operation in validated_plan["ordered_schedule"]:
        event = _event_core(
            sequence=len(events) + 1,
            region="BODY",
            operation=operation,
            before=current,
        )
        if body_stopped:
            _not_submitted(
                event,
                "NOT_SUBMITTED_PRIOR_TRAVERSAL_BOUNDARY",
                "PRIOR_TRAVERSAL_BOUNDARY",
            )
        elif consumed:
            _not_submitted(
                event,
                "NOT_SUBMITTED_DECISION_ALREADY_CONSUMED",
                "DECISION_ALREADY_CONSUMED",
            )
            body_stopped = True
        else:
            attempt_id = (
                f"{run_binding.run_id}:plan-{validated_plan['request']['plan_id']}:"
                f"body-{operation['ordinal']}"
            )
            try:
                event, operation_consumed, operation_blocked, result_attempt = (
                    _execute_body_operation(
                        bridge,
                        operation,
                        event,
                        run_binding,
                        operation_bindings,
                        captures,
                        attempt_id,
                    )
                )
            except Exception as error:
                event["dispatch"] = {
                    "status": "BRIDGE_ERROR_FAIL_CLOSED",
                    "error_type": type(error).__name__,
                }
                event["simulator_acceptance"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
                event["decision_consumption"] = {
                    "status": "UNKNOWN_BRIDGE_ERROR",
                    "consumes_decision": None,
                }
                event["simulator_outcome"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
                event["traversal"] = {
                    "status": "STOP",
                    "reason": "BRIDGE_ERROR_FAIL_CLOSED",
                }
                operation_consumed = False
                operation_blocked = True
                result_attempt = None
                execution_failed = True
            consumed = consumed or operation_consumed
            body_stopped = body_stopped or operation_blocked
            if result_attempt is not None:
                attempt_ids.append(result_attempt)
        current = event["state_after"]
        omission = event.get("typed_omission")
        if isinstance(omission, Mapping):
            omissions.append(dict(omission))
            execution_failed = True
        if event["dispatch"].get("status") in {
            "NOT_SUBMITTED_ACTION_ABSENT",
            "NOT_SUBMITTED_ACTION_ILLEGAL",
            "NOT_SUBMITTED_GCD_CLASSIFICATION_MISMATCH",
            "NOT_SUBMITTED_BINDING_MISSING",
            "NOT_SUBMITTED_CAPABILITY_ABSENT",
            "NOT_SUBMITTED_CAPTURE_MISSING",
            "NOT_SUBMITTED_NO_EXACT_TARGET_ORDER",
            "NOT_SUBMITTED_SELF_TARGET_UNMODELED",
            "NOT_SUBMITTED_HORIZON_BOUNDARY",
            "NOT_SUBMITTED_MAPPING_MISSING",
            "BRIDGE_ERROR_FAIL_CLOSED",
        }:
            execution_failed = True
        if event["simulator_acceptance"].get("status") == "REJECTED_POSTCONDITION":
            execution_failed = True
        if current["time_ms"] > run_binding.horizon_end_ms:
            body_stopped = True
            execution_failed = True
            event["horizon_violation"] = {
                "code": "COMMAND_ADVANCED_PAST_BOUND_HORIZON",
                "observed_time_ms": current["time_ms"],
                "horizon_end_ms": run_binding.horizon_end_ms,
                "comparison_fatal": True,
            }
        event = _finish_event(event, previous_sha)
        previous_sha = event["chain"]["event_sha256"]
        events.append(event)

    finally_failures = False
    for operation in validated_plan["finally_schedule"]:
        event = _event_core(
            sequence=len(events) + 1,
            region="FINALLY",
            operation=operation,
            before=current,
        )
        try:
            event, blocked = _execute_finally_operation(
                bridge, operation, event, run_binding, captures
            )
        except Exception as error:
            event["dispatch"] = {
                "status": "BRIDGE_ERROR_FAIL_CLOSED",
                "error_type": type(error).__name__,
            }
            event["simulator_acceptance"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
            event["decision_consumption"] = {
                "status": "UNKNOWN_BRIDGE_ERROR",
                "consumes_decision": None,
            }
            event["simulator_outcome"] = {"status": "UNKNOWN_BRIDGE_ERROR"}
            event["traversal"] = {
                "status": "STOP",
                "reason": "BRIDGE_ERROR_FAIL_CLOSED",
            }
            blocked = True
        finally_failures = finally_failures or blocked
        current = event["state_after"]
        omission = event.get("typed_omission")
        if isinstance(omission, Mapping):
            omissions.append(dict(omission))
        event = _finish_event(event, previous_sha)
        previous_sha = event["chain"]["event_sha256"]
        events.append(event)

    result_method = getattr(bridge, "server_results_since_last_decision", None)
    if attempt_ids and callable(result_method):
        try:
            captured_receipt = _serialize_simulator_results(
                result_method(attempt_ids)
            )
            if not isinstance(captured_receipt, Mapping) or not isinstance(
                captured_receipt.get("pending_attempt_ids"), list
            ):
                raise Cat2NewCandidateSimulatorExecutorV5Error(
                    "simulator result batch lacks pending_attempt_ids"
                )
            simulator_results = {
                "status": "CAPTURED",
                "attempt_ids": list(attempt_ids),
                "receipt": captured_receipt,
            }
            if captured_receipt["pending_attempt_ids"]:
                omissions.append(
                    {
                        "code": "SIMULATOR_RESULTS_PENDING_AT_PLAN_END",
                        "scope": "SIMULATOR_RESULT",
                        "pending_attempt_ids": list(
                            captured_receipt["pending_attempt_ids"]
                        ),
                        "comparison_fatal": True,
                        "surrogate_action_used": False,
                    }
                )
        except Exception as error:
            simulator_results = {
                "status": "CAPTURE_FAILED",
                "attempt_ids": list(attempt_ids),
                "error_type": type(error).__name__,
            }
            execution_failed = True
    elif attempt_ids:
        simulator_results = {
            "status": "API_UNAVAILABLE_TYPED_OMISSION",
            "attempt_ids": list(attempt_ids),
            "omission": {
                "code": "SIMULATOR_TERMINAL_RESULT_API_UNAVAILABLE",
                "comparison_fatal": True,
                "surrogate_result_used": False,
            },
        }
        omissions.append(
            {
                "code": "SIMULATOR_TERMINAL_RESULT_API_UNAVAILABLE",
                "scope": "SIMULATOR_RESULT",
                "comparison_fatal": True,
                "surrogate_action_used": False,
            }
        )
    else:
        simulator_results = {
            "status": "NOT_REQUIRED_NO_ACTION_ATTEMPTS",
            "attempt_ids": [],
        }

    final_state = bridge.state()
    if not isinstance(final_state, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "bridge.state final read must return simulator state"
        )
    final = _state_receipt(final_state, run_binding)
    if final != current:
        execution_failed = True
        final_state_consistency = "FINAL_READ_DIFFERS_FROM_LAST_COMMAND_STATE"
    else:
        final_state_consistency = "PASS"
    horizon_ok = final["time_ms"] <= run_binding.horizon_end_ms and not any(
        "horizon_violation" in event for event in events
    )
    lifecycle_core: JSONMap = {
        "scope": "ONE_CONTENT_ADDRESSED_PLAN_INTERVAL",
        "initial_state": initial,
        "final_state": final,
        "state_checkpoint_count": 1 + len(events) + 1,
        "event_chain_head_sha256": previous_sha,
        "dynamic_generation_constant": all(
            event["state_after"]["environment_generation"]
            == run_binding.environment_generation
            for event in events
        ),
        "dynamic_config_constant": all(
            event["state_after"]["dynamic_config_sha256"]
            == run_binding.dynamic_config_sha256
            for event in events
        ),
        "time_nondecreasing": all(
            event["state_after"]["time_ms"] >= event["state_before"]["time_ms"]
            for event in events
        ),
        "final_state_consistency": final_state_consistency,
        "finally_operation_count": len(validated_plan["finally_schedule"]),
        "finally_failures": finally_failures,
    }
    lifecycle_receipt = {
        **lifecycle_core,
        "content_address": _content_address(lifecycle_core),
    }
    horizon_core: JSONMap = {
        "scope": "PLAN_BOUND_NOT_FULL_ENCOUNTER_ROLLOUT",
        "start_time_ms": initial["time_ms"],
        "end_time_ms": final["time_ms"],
        "horizon_end_ms": run_binding.horizon_end_ms,
        "elapsed_ms": final["time_ms"] - initial["time_ms"],
        "remaining_ms": max(0, run_binding.horizon_end_ms - final["time_ms"]),
        "within_bound": horizon_ok,
        "horizon_reached": final["time_ms"] == run_binding.horizon_end_ms,
        "encounter_finished": final["finished"] is True,
        "prechecked_waits": True,
        "postchecked_all_commands": True,
    }
    horizon_receipt = {
        **horizon_core,
        "content_address": _content_address(horizon_core),
    }

    unique_omissions = []
    seen_omissions: set[str] = set()
    for omission in omissions:
        key = _sha256_json(omission)
        if key not in seen_omissions:
            seen_omissions.add(key)
            unique_omissions.append(omission)
    offline_faithful = (
        not unique_omissions
        and not execution_failed
        and not finally_failures
        and horizon_ok
        and lifecycle_core["time_nondecreasing"]
        and lifecycle_core["dynamic_generation_constant"]
        and lifecycle_core["dynamic_config_constant"]
        and final_state_consistency == "PASS"
    )
    plan_fully_traversed = not any(
        event["region"] == "BODY"
        and str(event["dispatch"].get("status", "")).startswith("NOT_SUBMITTED")
        for event in events
    )
    core: JSONMap = {
        "schema": EXECUTION_SCHEMA_V5,
        "executor_id": EXECUTOR_ID_V5,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "identity": {
            "v4_plan_executor_id": PLAN_EXECUTOR_ID_V4,
            "v4_plan_contract_sha256": PLAN_CONTRACT_SHA256_V4,
            "v4_plan_content_sha256": validated_plan["content_address"]["sha256"],
            "policy_state_snapshot_sha256": snapshot_sha,
            "simulator_request_sha256": _sha256_json(request_copy),
            "dynamic_load_receipt_sha256": _sha256_json(load_receipt_copy),
            "run_binding": run_binding.to_wire(),
            "operation_bindings": operation_bindings.to_wire(),
        },
        "role": {
            "policy_role": "CANDIDATE",
            "execution_role": "OFFLINE_SIMULATOR_CANDIDATE",
            "baseline": False,
            "eligible_for_independent_vote": False,
        },
        "ordered_events": events,
        "simulator_result_receipt": simulator_results,
        "horizon_receipt": horizon_receipt,
        "lifecycle_receipt": lifecycle_receipt,
        "typed_omissions": unique_omissions,
        "fallback": {
            "used": False,
            "surrogate_skill_used": False,
            "reason": "V5_NEVER_SYNTHESIZES_AN_UNREPRESENTABLE_OPERATION",
        },
        "decision_consumed": consumed,
        "body_traversal_stopped": body_stopped,
        "plan_fully_traversed": plan_fully_traversed,
        "execution_blocked": execution_failed or finally_failures or not horizon_ok,
        "offline_plan_execution_faithful": offline_faithful,
        "offline_plan_interval_complete": (
            offline_faithful and plan_fully_traversed
        ),
        "offline_score_eligible": False,
        "live_client_execution": False,
        "formal_runner_registration_authorized": False,
        "comparison_ready": False,
        "scientific_run_launched": False,
        "evidence_boundary": {
            "source_plan": "VERIFIED_CONTENT_ADDRESSED_V4",
            "simulator_submission": "OBSERVED_IN_THIS_EXECUTION",
            "simulator_acceptance": "OBSERVED_IN_THIS_EXECUTION",
            "wow_client_trace": "ABSENT",
            "game_server_trace": "ABSENT",
            "simulator_is_not_wow_client": True,
            "simulator_result_is_not_game_server_result": True,
        },
        "claim_boundary": "offline ordered candidate-plan execution only; not Cat2_new deployment, client fidelity, formal comparison, or DPS superiority",
    }
    return validate_cat2new_candidate_execution_v5(
        {**core, "content_address": _content_address(core)}
    )


def validate_cat2new_candidate_execution_v5(value: Mapping[str, Any]) -> JSONMap:
    document = _strict_json_copy(value, "v5 execution receipt")
    if document.get("schema") != EXECUTION_SCHEMA_V5:
        raise Cat2NewCandidateSimulatorExecutorV5Error("execution schema mismatch")
    core = {key: item for key, item in document.items() if key != "content_address"}
    if document.get("content_address") != _content_address(core):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "execution content address mismatch"
        )
    if document.get("formal_runner_registration_authorized") is not False:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "formal runner registration must remain false"
        )
    if document.get("comparison_ready") is not False:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "comparison_ready must remain false"
        )
    if document.get("offline_score_eligible") is not False:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "offline_score_eligible must remain false before formal registration"
        )
    if document.get("scientific_run_launched") is not False:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "scientific_run_launched must remain false"
        )
    identity = document.get("identity")
    if not isinstance(identity, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "execution identity is missing"
        )
    if (
        identity.get("v4_plan_executor_id") != PLAN_EXECUTOR_ID_V4
        or identity.get("v4_plan_contract_sha256")
        != PLAN_CONTRACT_SHA256_V4
    ):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "execution is not bound to the pinned v4 contract"
        )
    _sha256(identity.get("v4_plan_content_sha256"), "v4 plan content SHA-256")
    _sha256(
        identity.get("policy_state_snapshot_sha256"),
        "policy state snapshot SHA-256",
    )
    _validate_flat_content_sha(identity.get("run_binding"), "run_binding")
    _validate_flat_content_sha(
        identity.get("operation_bindings"), "operation_bindings"
    )
    role = document.get("role")
    if not isinstance(role, Mapping) or role.get("baseline") is not False:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "candidate execution cannot self-promote to baseline"
        )
    fallback = document.get("fallback")
    if not isinstance(fallback, Mapping) or fallback.get("surrogate_skill_used") is not False:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "surrogate skill fallback is forbidden"
        )
    previous: str | None = None
    events = document.get("ordered_events")
    if not isinstance(events, list):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "ordered_events must be an array"
        )
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, Mapping) or event.get("sequence") != sequence:
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "ordered event sequence is invalid"
            )
        chain = event.get("chain")
        if not isinstance(chain, Mapping) or chain.get("previous_event_sha256") != previous:
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "ordered event chain predecessor mismatch"
            )
        expected_event = deepcopy(dict(event))
        event_chain = dict(expected_event["chain"])
        digest = event_chain.pop("event_sha256", None)
        expected_event["chain"] = event_chain
        if digest != _sha256_json(expected_event):
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "ordered event content digest mismatch"
            )
        omission = event.get("typed_omission")
        if isinstance(omission, Mapping) and omission.get("surrogate_action_used") is not False:
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                "typed omission used a surrogate action"
            )
        _validate_nested_content_address(event.get("state_before"), "state_before")
        _validate_nested_content_address(event.get("state_after"), "state_after")
        dispatch = event.get("dispatch")
        if isinstance(dispatch, Mapping) and "scheduled_state" in dispatch:
            _validate_nested_content_address(
                dispatch.get("scheduled_state"), "scheduled_state"
            )
        previous = digest
    lifecycle = document.get("lifecycle_receipt")
    horizon = document.get("horizon_receipt")
    _validate_nested_content_address(lifecycle, "lifecycle_receipt")
    _validate_nested_content_address(horizon, "horizon_receipt")
    assert isinstance(lifecycle, Mapping) and isinstance(horizon, Mapping)
    if lifecycle.get("event_chain_head_sha256") != previous:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "lifecycle event-chain head mismatch"
        )
    initial = lifecycle.get("initial_state")
    final = lifecycle.get("final_state")
    _validate_nested_content_address(initial, "lifecycle.initial_state")
    _validate_nested_content_address(final, "lifecycle.final_state")
    assert isinstance(initial, Mapping) and isinstance(final, Mapping)
    if (
        horizon.get("start_time_ms") != initial.get("time_ms")
        or horizon.get("end_time_ms") != final.get("time_ms")
    ):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "horizon and lifecycle endpoints disagree"
        )
    if document.get("offline_plan_execution_faithful") is True and (
        document.get("typed_omissions") or document.get("execution_blocked") is True
    ):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "faithful execution cannot contain omissions or a blocker"
        )
    return document


def _validate_nested_content_address(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(f"{label} must be an object")
    core = {key: item for key, item in value.items() if key != "content_address"}
    if value.get("content_address") != _content_address(core):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} content address mismatch"
        )


def _validate_flat_content_sha(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(f"{label} must be an object")
    core = {key: item for key, item in value.items() if key != "content_sha256"}
    if value.get("content_sha256") != _sha256_json(core):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            f"{label} content SHA-256 mismatch"
        )


V5_READINESS_BLOCKERS: tuple[JSONMap, ...] = (
    {
        "code": "CAT2_NEW_CLIENT_TRACE_MISSING",
        "scope": "LIVE",
        "message": "no ordered Cat2_new client sink/acceptance trace is available",
        "runner_fatal": True,
    },
    {
        "code": "CAT2_NEW_GAME_SERVER_TRACE_MISSING",
        "scope": "LIVE",
        "message": "no Cat2_new client attempts are joined to WoW server outcomes",
        "runner_fatal": True,
    },
    {
        "code": "CAT2_NEW_DEPLOYED_PROFILE_MISSING",
        "scope": "LIVE",
        "message": "no deployed profile is content-bound to the Cat2_new source tree",
        "runner_fatal": True,
    },
    {
        "code": "LIVE_LUA_DISPATCHER_MISSING",
        "scope": "LIVE",
        "message": "the v4 plan has not been distilled into a client Lua dispatcher",
        "runner_fatal": True,
    },
    {
        "code": "FORMAL_RUNNER_REQUIRES_ZERO_TYPED_OMISSIONS",
        "scope": "SIMULATOR",
        "message": "each registered scenario/plan must prove zero typed omissions",
        "runner_fatal": True,
    },
    {
        "code": "FORMAL_HORIZON_ROLLOUT_NOT_IMPLEMENTED",
        "scope": "SIMULATOR",
        "message": "v5 executes one bound plan interval and is not a full encounter policy loop",
        "runner_fatal": True,
    },
)


def build_cat2new_simulator_readiness_v5(
    *,
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    installed_root: str | Path = DEFAULT_INSTALLED_ROOT,
    savedvariables_path: str | Path = DEFAULT_SAVEDVARIABLES,
) -> JSONMap:
    predecessor = build_readiness_report_v4(
        source_root=source_root,
        manifest_path=manifest_path,
        installed_root=installed_root,
        savedvariables_path=savedvariables_path,
    )
    payload = Path(__file__).resolve().read_bytes()
    blockers = [deepcopy(row) for row in V5_READINESS_BLOCKERS]
    if (
        predecessor["deployed_identity"]["installed_tree"].get(
            "matches_cat2new_tree"
        )
        is not True
    ):
        blockers.insert(
            0,
            {
                "code": "CAT2_NEW_NOT_DEPLOYED",
                "scope": "LIVE",
                "message": "the installed Cat2 tree does not match the pinned Cat2_new tree",
                "runner_fatal": True,
            },
        )
    core: JSONMap = {
        "schema": READINESS_SCHEMA_V5,
        "executor_id": EXECUTOR_ID_V5,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "identity": {
            "module_sha256": hashlib.sha256(payload).hexdigest(),
            "v4_contract_sha256": PLAN_CONTRACT_SHA256_V4,
            "v4_readiness_content_sha256": predecessor["content_address"]["sha256"],
            "cat2new_source_tree_sha256": predecessor["identity"]["source_tree_sha256"],
        },
        "offline_capability": {
            "v4_plan_validation": True,
            "ordered_body_execution": True,
            "compiler_generated_finally_execution": True,
            "dynamic_generation_binding": True,
            "dynamic_config_binding": True,
            "policy_state_content_binding": True,
            "horizon_receipt": True,
            "lifecycle_receipt": True,
            "receipt_scope": "ONE_PLAN_INTERVAL_NOT_FULL_ENCOUNTER",
            "event_hash_chain": True,
            "exact_target_with_binding": True,
            "item_action_with_binding": True,
            "equipment_with_optional_exact_control": True,
            "queue_set_and_cancel": True,
            "intentional_wait_no_fallback": True,
            "unrepresentable_operations_typed_not_proxied": True,
        },
        "conditional_typed_omissions": [
            "AUTO_NEAREST_6_GEOMETRY_UNAVAILABLE",
            "STOP_ATTACK_CAPABILITY_ABSENT",
            "SELF_TARGET_ITEM_SEMANTICS_UNAVAILABLE",
            "EQUIPMENT_CONTROL_CAPABILITY_ABSENT",
            "ELIGIBLE_CLEAVE_ENEMY_COUNT_MISSING",
            "WAIT_ADVANCE_CAPABILITY_ABSENT",
            "SIMULATOR_TERMINAL_RESULT_API_UNAVAILABLE",
            "SIMULATOR_RESULTS_PENDING_AT_PLAN_END",
        ],
        "deployed_identity": deepcopy(predecessor["deployed_identity"]),
        "role": {
            "policy_role": "CANDIDATE",
            "baseline": False,
            "eligible_for_independent_vote": False,
        },
        "offline_execution_contract_complete": True,
        "offline_execution_requires_per_run_zero_omissions": True,
        "live_execution_ready": False,
        "formal_runner_registration_authorized": False,
        "comparison_ready": False,
        "scientific_run_launched": False,
        "blockers": blockers,
        "claim_boundary": "versioned offline executor interface only; deployment, client fidelity, formal runner registration, and comparisons remain blocked",
    }
    return validate_cat2new_simulator_readiness_v5(
        {**core, "content_address": _content_address(core)}
    )


def validate_cat2new_simulator_readiness_v5(value: Mapping[str, Any]) -> JSONMap:
    document = _strict_json_copy(value, "v5 readiness")
    if document.get("schema") != READINESS_SCHEMA_V5:
        raise Cat2NewCandidateSimulatorExecutorV5Error("readiness schema mismatch")
    core = {key: item for key, item in document.items() if key != "content_address"}
    if document.get("content_address") != _content_address(core):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "readiness content address mismatch"
        )
    if document.get("offline_execution_contract_complete") is not True:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "offline execution contract must remain complete"
        )
    identity = document.get("identity")
    if not isinstance(identity, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "readiness identity is missing"
        )
    if identity.get("v4_contract_sha256") != PLAN_CONTRACT_SHA256_V4:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "readiness v4 contract identity mismatch"
        )
    current_module_sha = hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    if identity.get("module_sha256") != current_module_sha:
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "readiness module SHA-256 mismatch"
        )
    for field_name in (
        "live_execution_ready",
        "formal_runner_registration_authorized",
        "comparison_ready",
        "scientific_run_launched",
    ):
        if document.get(field_name) is not False:
            raise Cat2NewCandidateSimulatorExecutorV5Error(
                f"readiness {field_name} must remain false"
            )
    codes = {
        row.get("code")
        for row in document.get("blockers", [])
        if isinstance(row, Mapping)
    }
    required = {row["code"] for row in V5_READINESS_BLOCKERS}
    if not required.issubset(codes):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "mandatory v5 readiness blocker missing"
        )
    deployed = document.get("deployed_identity")
    if not isinstance(deployed, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "deployed identity is missing"
        )
    role = document.get("role")
    if (
        not isinstance(role, Mapping)
        or role.get("policy_role") != "CANDIDATE"
        or role.get("baseline") is not False
        or role.get("eligible_for_independent_vote") is not False
    ):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "readiness candidate role cannot be promoted"
        )
    installed = deployed.get("installed_tree")
    if not isinstance(installed, Mapping):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "installed-tree identity is missing"
        )
    if (
        installed.get("matches_cat2new_tree") is not True
        and "CAT2_NEW_NOT_DEPLOYED" not in codes
    ):
        raise Cat2NewCandidateSimulatorExecutorV5Error(
            "installed-tree mismatch requires CAT2_NEW_NOT_DEPLOYED"
        )
    return document


def serialize_cat2new_candidate_execution_v5(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_cat2new_candidate_execution_v5(value)) + b"\n"


def serialize_cat2new_simulator_readiness_v5(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_cat2new_simulator_readiness_v5(value)) + b"\n"


__all__ = [
    "BINDINGS_SCHEMA_V5",
    "Cat2NewCandidateSimulatorExecutorV5Error",
    "Cat2NewDynamicBridgeV5",
    "Cat2NewSimulatorOperationBindingsV5",
    "Cat2NewSimulatorRunBindingV5",
    "EXECUTION_SCHEMA_V5",
    "EXECUTOR_ID_V5",
    "IMPLEMENTATION_REVISION",
    "READINESS_SCHEMA_V5",
    "RUN_BINDING_SCHEMA_V5",
    "SimulatorControlResultV5",
    "SimulatorEquipmentBindingV5",
    "SimulatorEquipmentResultV5",
    "SimulatorItemBindingV5",
    "UnitTargetBindingV5",
    "V5_READINESS_BLOCKERS",
    "build_cat2new_simulator_readiness_v5",
    "execute_cat2new_candidate_plan_v5",
    "serialize_cat2new_candidate_execution_v5",
    "serialize_cat2new_simulator_readiness_v5",
    "validate_cat2new_candidate_execution_v5",
    "validate_cat2new_simulator_readiness_v5",
]
