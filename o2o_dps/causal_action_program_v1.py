"""Causal state-feedback action programs for dynamic-v3 simulator cases.

The finite schedule search is useful for short deterministic windows, but a
fixed list of timestamps cannot express "skip this burst if the pack is nearly
dead, then reconsider it on the next pack".  This module provides the smallest
state-feedback grammar needed for that behavior:

* at every decision epoch, test ordered current-observation alternatives;
* otherwise execute one unconditional fallback;
* independently guard each optional off-GCD prefix; and
* always end the selected decision with one GCD action or a positive wait.

An imported Cat/Contra adapter is represented by the same
``CausalActionProgramV1`` type through ``ImportedReactiveSelectorV1``.  The
source adapter is not modified: a runtime binding receives only the caller's
causal observation projection and returns the same ``ProgramDecisionV1`` used
by declarative programs.

The raw dynamic-v3 state is control-plane data and is never passed to a
selector.  Callers must provide an observation projector, normally backed by
``project_live_state_for_policy_v1`` and explicit prefix registries.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
import math
from typing import Any, Callable, Mapping, Protocol, Sequence

from .causal_guard_v1 import (
    GuardEvaluationV1,
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
    observable_causal_guard_from_dict_v1,
)
from .policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from .sim_bridge import ActionRef, AvailableAction
from .sim_bridge_dynamic_v3 import _press_clock_state_v1
from .wave_action_schedule_v1 import QueueLaneOp
from .wave_action_sequence_search_v1 import (
    FURY_RESULT_BEARING_ACTION_REFS_V1,
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
)


JSONMap = dict[str, Any]
_SCHEMA = "causal_action_program/v1"
_DECISION_SCHEMA = "causal_action_program_decision/v1"
_PREFIX_SCHEMA = "causal_action_program_optional_off_gcd/v1"
_SELECTOR_SCHEMA = "causal_action_program_selector/v1"


class CausalActionProgramError(RuntimeError):
    """A program or imported binding cannot be executed faithfully."""


class ProgramOriginV1(str, Enum):
    SEARCHED = "SEARCHED"
    SEARCHED_REACTIVE = "SEARCHED_REACTIVE"
    IMPORTED_REACTIVE_INCUMBENT = "IMPORTED_REACTIVE_INCUMBENT"
    SOURCE_DERIVED_OFFLINE = "SOURCE_DERIVED_OFFLINE"
    HAND_AUTHORED_DEVELOPMENT = "HAND_AUTHORED_DEVELOPMENT"


class ProgramPrefixOperationKindV1(str, Enum):
    """Ordered, non-terminal sinks within one source-style decision."""

    SET_TARGET = "SET_TARGET"
    START_ATTACK = "START_ATTACK"
    STOP_CAST = "STOP_CAST"
    OPTIONAL_OFF_GCD = "OPTIONAL_OFF_GCD"
    QUEUE_KEEP = "QUEUE_KEEP"
    QUEUE_SET = "QUEUE_SET"
    QUEUE_CANCEL = "QUEUE_CANCEL"


class ProgramInsertionPointV1(str, Enum):
    """Where one searched prefix is spliced into source ordered sinks."""

    BEFORE_SOURCE_PREFIX_ORDER = "BEFORE_SOURCE_PREFIX_ORDER"
    AFTER_SOURCE_SET_TARGET = "AFTER_SOURCE_SET_TARGET"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise ValueError(f"{label} must be positive")
    return result


def _action(value: object, label: str) -> ActionRef:
    if not isinstance(value, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (value.spell_id, value.item_id, value.other_id)
    if (
        any(
            isinstance(field, bool) or not isinstance(field, int) or field < 0
            for field in (*identities, value.tag)
        )
        or sum(field > 0 for field in identities) != 1
    ):
        raise ValueError(f"{label} has an invalid exact action identity")
    return value


def _action_from_wire(value: object, label: str) -> ActionRef:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an action object")
    try:
        return _action(ActionRef.from_wire(value), label)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


def _exact_mapping(
    value: object,
    fields: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if set(value) != fields:
        raise ValueError(
            f"{label} fields differ: expected {sorted(fields)}, "
            f"got {sorted(value)}"
        )
    return value


@dataclass(frozen=True)
class OptionalOffGcdPrefixV1:
    """One burst/item prefix independently reconsidered at every epoch."""

    action: ActionRef
    guard: ObservableCausalGuardV1

    def __post_init__(self) -> None:
        _action(self.action, "optional off-GCD action")
        if not isinstance(self.guard, ObservableCausalGuardV1):
            raise TypeError("optional off-GCD guard must be ObservableCausalGuardV1")
        if self.guard.false_semantics != SKIP_PLAN:
            raise ValueError("optional off-GCD guard must use SKIP_PLAN semantics")
        if self.guard.action_ready != self.action:
            raise ValueError(
                "optional off-GCD guard.action_ready must equal its action"
            )

    def to_dict(self) -> JSONMap:
        return {
            "schema": _PREFIX_SCHEMA,
            "action": self.action.to_wire(),
            "guard": self.guard.to_dict(),
        }


def optional_off_gcd_prefix_from_dict_v1(
    value: object,
) -> OptionalOffGcdPrefixV1:
    raw = _exact_mapping(value, {"schema", "action", "guard"}, "prefix")
    if raw["schema"] != _PREFIX_SCHEMA:
        raise ValueError("prefix schema is unsupported")
    return OptionalOffGcdPrefixV1(
        action=_action_from_wire(raw["action"], "prefix.action"),
        guard=observable_causal_guard_from_dict_v1(
            raw["guard"], label="prefix.guard"
        ),
    )


@dataclass(frozen=True)
class ProgramDecisionV1:
    """One selected epoch body with a mandatory decision-consuming terminal."""

    target_index: int | None = None
    start_attack: bool = False
    stop_cast: bool = False
    optional_off_gcd_prefixes: tuple[OptionalOffGcdPrefixV1, ...] = ()
    queue_op: QueueLaneOp = QueueLaneOp.KEEP
    queue_action: ActionRef | None = None
    gcd_action: ActionRef | None = None
    wait_ms: int | None = None
    prefix_order: tuple[ProgramPrefixOperationKindV1, ...] | None = None

    def __post_init__(self) -> None:
        if self.target_index is not None:
            _nonnegative_int(self.target_index, "target_index")
        if not isinstance(self.start_attack, bool):
            raise TypeError("start_attack must be boolean")
        if not isinstance(self.stop_cast, bool):
            raise TypeError("stop_cast must be boolean")
        if not isinstance(self.optional_off_gcd_prefixes, tuple) or any(
            not isinstance(prefix, OptionalOffGcdPrefixV1)
            for prefix in self.optional_off_gcd_prefixes
        ):
            raise TypeError(
                "optional_off_gcd_prefixes must contain OptionalOffGcdPrefixV1"
            )
        prefix_actions = [row.action for row in self.optional_off_gcd_prefixes]
        if not isinstance(self.queue_op, QueueLaneOp):
            raise TypeError("queue_op must be QueueLaneOp")
        if self.queue_op is QueueLaneOp.SET:
            action = _action(self.queue_action, "queue_action")
            if action.spell_id <= 0 or action.tag != 1:
                raise ValueError("queue_action must be a tagged spell action")
        elif self.queue_action is not None:
            raise ValueError("queue_action is only valid for queue_op SET")

        has_gcd = self.gcd_action is not None
        has_wait = self.wait_ms is not None
        if has_gcd == has_wait:
            raise ValueError("decision requires exactly one terminal GCD or WAIT")
        if has_gcd:
            _action(self.gcd_action, "gcd_action")
        else:
            _positive_int(self.wait_ms, "wait_ms")

        # Source policies can intentionally submit the same off-GCD sink more
        # than once in one macro pass.  Preserve both operations: after the
        # first accepted use, the second operation's action-ready guard will
        # normally skip it.  Queue and terminal lanes remain mutually
        # exclusive with each other.
        actions: list[ActionRef] = []
        if self.queue_action is not None:
            actions.append(self.queue_action)
        if self.gcd_action is not None:
            actions.append(self.gcd_action)
        if len(set(actions)) != len(actions):
            raise ValueError("one action cannot occupy multiple decision lanes")

        required: list[ProgramPrefixOperationKindV1] = []
        if self.target_index is not None:
            required.append(ProgramPrefixOperationKindV1.SET_TARGET)
        if self.start_attack:
            required.append(ProgramPrefixOperationKindV1.START_ATTACK)
        if self.stop_cast:
            required.append(ProgramPrefixOperationKindV1.STOP_CAST)
        required.extend(
            ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
            for _ in self.optional_off_gcd_prefixes
        )
        required.append(
            {
                QueueLaneOp.KEEP: ProgramPrefixOperationKindV1.QUEUE_KEEP,
                QueueLaneOp.SET: ProgramPrefixOperationKindV1.QUEUE_SET,
                QueueLaneOp.CANCEL: ProgramPrefixOperationKindV1.QUEUE_CANCEL,
            }[self.queue_op]
        )
        if self.prefix_order is None:
            object.__setattr__(self, "prefix_order", tuple(required))
        else:
            if not isinstance(self.prefix_order, tuple) or any(
                not isinstance(kind, ProgramPrefixOperationKindV1)
                for kind in self.prefix_order
            ):
                raise TypeError(
                    "prefix_order must contain ProgramPrefixOperationKindV1"
                )
            from collections import Counter

            if Counter(self.prefix_order) != Counter(required):
                raise ValueError(
                    "prefix_order must contain each configured prefix sink exactly once"
                )

    def to_dict(self) -> JSONMap:
        return {
            "schema": _DECISION_SCHEMA,
            "target_index": self.target_index,
            "controls": {
                "start_attack": self.start_attack,
                "stop_cast": self.stop_cast,
            },
            "optional_off_gcd_prefixes": [
                prefix.to_dict() for prefix in self.optional_off_gcd_prefixes
            ],
            "queue": {
                "op": self.queue_op.value,
                "action": (
                    self.queue_action.to_wire()
                    if self.queue_action is not None
                    else None
                ),
            },
            "terminal": {
                "gcd_action": (
                    self.gcd_action.to_wire()
                    if self.gcd_action is not None
                    else None
                ),
                "wait_ms": self.wait_ms,
            },
            "prefix_order": [kind.value for kind in self.prefix_order or ()],
        }


def program_decision_from_dict_v1(value: object) -> ProgramDecisionV1:
    raw = _exact_mapping(
        value,
        {
            "schema",
            "target_index",
            "controls",
            "optional_off_gcd_prefixes",
            "queue",
            "terminal",
            "prefix_order",
        },
        "program decision",
    )
    if raw["schema"] != _DECISION_SCHEMA:
        raise ValueError("program decision schema is unsupported")
    prefixes = raw["optional_off_gcd_prefixes"]
    if not isinstance(prefixes, list):
        raise ValueError("program decision prefixes must be an array")
    queue = _exact_mapping(raw["queue"], {"op", "action"}, "decision.queue")
    controls = _exact_mapping(
        raw["controls"], {"start_attack", "stop_cast"}, "decision.controls"
    )
    if not isinstance(controls["start_attack"], bool) or not isinstance(
        controls["stop_cast"], bool
    ):
        raise ValueError("decision control flags must be boolean")
    terminal = _exact_mapping(
        raw["terminal"], {"gcd_action", "wait_ms"}, "decision.terminal"
    )
    try:
        queue_op = QueueLaneOp(queue["op"])
    except (TypeError, ValueError) as error:
        raise ValueError("decision.queue.op is unsupported") from error
    queue_action = (
        None
        if queue["action"] is None
        else _action_from_wire(queue["action"], "decision.queue.action")
    )
    gcd_action = (
        None
        if terminal["gcd_action"] is None
        else _action_from_wire(
            terminal["gcd_action"], "decision.terminal.gcd_action"
        )
    )
    prefix_order = raw["prefix_order"]
    if not isinstance(prefix_order, list):
        raise ValueError("decision.prefix_order must be an array")
    try:
        parsed_prefix_order = tuple(
            ProgramPrefixOperationKindV1(value) for value in prefix_order
        )
    except (TypeError, ValueError) as error:
        raise ValueError("decision.prefix_order contains an unsupported sink") from error
    return ProgramDecisionV1(
        target_index=raw["target_index"],
        start_attack=controls["start_attack"],
        stop_cast=controls["stop_cast"],
        optional_off_gcd_prefixes=tuple(
            optional_off_gcd_prefix_from_dict_v1(row) for row in prefixes
        ),
        queue_op=queue_op,
        queue_action=queue_action,
        gcd_action=gcd_action,
        wait_ms=terminal["wait_ms"],
        prefix_order=parsed_prefix_order,
    )


@dataclass(frozen=True)
class GuardedAlternativeV1:
    alternative_id: str
    guard: ObservableCausalGuardV1
    decision: ProgramDecisionV1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "alternative_id",
            _text(self.alternative_id, "alternative_id"),
        )
        if not isinstance(self.guard, ObservableCausalGuardV1):
            raise TypeError("alternative guard must be ObservableCausalGuardV1")
        if self.guard.false_semantics != SKIP_PLAN:
            raise ValueError("alternative guard must use SKIP_PLAN fallthrough")
        if not isinstance(self.decision, ProgramDecisionV1):
            raise TypeError("alternative decision must be ProgramDecisionV1")

    def to_dict(self) -> JSONMap:
        return {
            "alternative_id": self.alternative_id,
            "guard": self.guard.to_dict(),
            "decision": self.decision.to_dict(),
        }


@dataclass(frozen=True)
class OrderedGuardSelectorV1:
    alternatives: tuple[GuardedAlternativeV1, ...]
    fallback: ProgramDecisionV1
    kind: str = "ORDERED_OBSERVABLE_ALTERNATIVES"

    def __post_init__(self) -> None:
        if not isinstance(self.alternatives, tuple) or any(
            not isinstance(row, GuardedAlternativeV1)
            for row in self.alternatives
        ):
            raise TypeError("alternatives must contain GuardedAlternativeV1")
        ids = [row.alternative_id for row in self.alternatives]
        if len(set(ids)) != len(ids):
            raise ValueError("alternative_id values must be unique")
        if not isinstance(self.fallback, ProgramDecisionV1):
            raise TypeError("fallback must be ProgramDecisionV1")
        if self.kind != "ORDERED_OBSERVABLE_ALTERNATIVES":
            raise ValueError("ordered selector kind is fixed")

    def to_dict(self) -> JSONMap:
        return {
            "schema": _SELECTOR_SCHEMA,
            "kind": self.kind,
            "alternatives": [row.to_dict() for row in self.alternatives],
            "fallback": self.fallback.to_dict(),
        }


@dataclass(frozen=True)
class ImportedReactiveSelectorV1:
    """Stable identity for an externally bound source-policy resolver."""

    binding_id: str
    source_policy_id: str
    observation_contract_id: str
    kind: str = "IMPORTED_REACTIVE_INCUMBENT"

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "source_policy_id",
            "observation_contract_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.kind != "IMPORTED_REACTIVE_INCUMBENT":
            raise ValueError("imported selector kind is fixed")

    def to_dict(self) -> JSONMap:
        return {
            "schema": _SELECTOR_SCHEMA,
            "kind": self.kind,
            "binding_id": self.binding_id,
            "source_policy_id": self.source_policy_id,
            "observation_contract_id": self.observation_contract_id,
        }


@dataclass(frozen=True)
class SearchedOffGcdInsertionV1:
    """One identified searched prefix inserted before an incumbent decision."""

    insertion_id: str
    prefix: OptionalOffGcdPrefixV1
    insertion_point: ProgramInsertionPointV1 = (
        ProgramInsertionPointV1.BEFORE_SOURCE_PREFIX_ORDER
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "insertion_id",
            _text(self.insertion_id, "insertion_id"),
        )
        if not isinstance(self.prefix, OptionalOffGcdPrefixV1):
            raise TypeError("insertion prefix must be OptionalOffGcdPrefixV1")
        if not isinstance(self.insertion_point, ProgramInsertionPointV1):
            raise TypeError("insertion_point must be ProgramInsertionPointV1")

    def to_dict(self) -> JSONMap:
        return {
            "insertion_id": self.insertion_id,
            "prefix": self.prefix.to_dict(),
            "insertion_point": self.insertion_point.value,
        }


@dataclass(frozen=True)
class ImportedFallbackOverlaySelectorV1:
    """Searched burst choices over an unchanged reactive incumbent fallback.

    Terminal alternatives are checked first.  If none matches, the bound
    incumbent resolver supplies its complete source decision, after which the
    searched optional off-GCD operations are prepended in ``insertion_order``.
    The incumbent's own ``prefix_order`` remains an unchanged suffix.
    """

    terminal_alternatives: tuple[GuardedAlternativeV1, ...]
    imported_fallback: ImportedReactiveSelectorV1
    off_gcd_insertions: tuple[SearchedOffGcdInsertionV1, ...] = ()
    insertion_order: tuple[str, ...] = ()
    insertion_position: str = (
        "PER_INSERTION_BEFORE_SOURCE_OR_AFTER_SOURCE_SET_TARGET"
    )
    kind: str = "SEARCHED_BURST_OVER_IMPORTED_REACTIVE_INCUMBENT"

    def __post_init__(self) -> None:
        if not isinstance(self.terminal_alternatives, tuple) or any(
            not isinstance(row, GuardedAlternativeV1)
            for row in self.terminal_alternatives
        ):
            raise TypeError(
                "terminal_alternatives must contain GuardedAlternativeV1"
            )
        alternative_ids = [
            row.alternative_id for row in self.terminal_alternatives
        ]
        if len(set(alternative_ids)) != len(alternative_ids):
            raise ValueError("terminal alternative IDs must be unique")
        for row in self.terminal_alternatives:
            if row.decision.gcd_action is None:
                raise ValueError("terminal alternatives must end in a GCD action")
            if row.guard.action_ready != row.decision.gcd_action:
                raise ValueError(
                    "terminal alternative guard.action_ready must equal its GCD"
                )
        if not isinstance(self.imported_fallback, ImportedReactiveSelectorV1):
            raise TypeError(
                "imported_fallback must be ImportedReactiveSelectorV1"
            )
        if not isinstance(self.off_gcd_insertions, tuple) or any(
            not isinstance(row, SearchedOffGcdInsertionV1)
            for row in self.off_gcd_insertions
        ):
            raise TypeError(
                "off_gcd_insertions must contain SearchedOffGcdInsertionV1"
            )
        insertion_ids = [row.insertion_id for row in self.off_gcd_insertions]
        if len(set(insertion_ids)) != len(insertion_ids):
            raise ValueError("off-GCD insertion IDs must be unique")
        if (
            not isinstance(self.insertion_order, tuple)
            or any(
                not isinstance(value, str) or not value.strip()
                for value in self.insertion_order
            )
            or len(set(self.insertion_order)) != len(self.insertion_order)
            or set(self.insertion_order) != set(insertion_ids)
        ):
            raise ValueError(
                "insertion_order must contain every insertion ID exactly once"
            )
        insertion_by_id = {
            row.insertion_id: row for row in self.off_gcd_insertions
        }
        points = [
            insertion_by_id[insertion_id].insertion_point
            for insertion_id in self.insertion_order
        ]
        seen_after_target = False
        for point in points:
            if point is ProgramInsertionPointV1.AFTER_SOURCE_SET_TARGET:
                seen_after_target = True
            elif seen_after_target:
                raise ValueError(
                    "BEFORE_SOURCE insertions must precede AFTER_TARGET "
                    "insertions in insertion_order"
                )
        if self.insertion_position != (
            "PER_INSERTION_BEFORE_SOURCE_OR_AFTER_SOURCE_SET_TARGET"
        ):
            raise ValueError("overlay insertion_position differs from v1")
        if self.kind != "SEARCHED_BURST_OVER_IMPORTED_REACTIVE_INCUMBENT":
            raise ValueError("imported-fallback overlay selector kind is fixed")

    def to_dict(self) -> JSONMap:
        return {
            "schema": _SELECTOR_SCHEMA,
            "kind": self.kind,
            "terminal_alternatives": [
                row.to_dict() for row in self.terminal_alternatives
            ],
            "imported_fallback": self.imported_fallback.to_dict(),
            "off_gcd_insertions": [
                row.to_dict() for row in self.off_gcd_insertions
            ],
            "insertion_order": list(self.insertion_order),
            "insertion_position": self.insertion_position,
        }


@dataclass(frozen=True)
class ImportedReactiveQueueGcdBlockSelectorV1:
    """Replace selected Cat queue/GCD lanes while preserving its other sinks.

    The imported source is resolved first.  A matching searched alternative
    replaces the lane it owns: a queue-only alternative inherits the source
    terminal GCD/wait, while a GCD-only alternative using queue ``KEEP``
    inherits the source queue sink.  Target selection, ordered controls, and
    optional off-GCD prefixes remain sourced from the imported decision.
    """

    block_alternatives: tuple[GuardedAlternativeV1, ...]
    imported_fallback: ImportedReactiveSelectorV1
    kind: str = "SEARCHED_QUEUE_GCD_BLOCK_OVER_IMPORTED_REACTIVE_INCUMBENT"

    def __post_init__(self) -> None:
        if not isinstance(self.block_alternatives, tuple) or any(
            not isinstance(row, GuardedAlternativeV1)
            for row in self.block_alternatives
        ):
            raise TypeError(
                "block_alternatives must contain GuardedAlternativeV1"
            )
        ids = [row.alternative_id for row in self.block_alternatives]
        if len(ids) != len(set(ids)):
            raise ValueError("queue/GCD block alternative IDs must be unique")
        if not isinstance(self.imported_fallback, ImportedReactiveSelectorV1):
            raise TypeError("imported_fallback must be ImportedReactiveSelectorV1")
        queue_count = 0
        gcd_count = 0
        cancel_with_gcd_count = 0
        for row in self.block_alternatives:
            decision = row.decision
            if (
                decision.target_index is not None
                or decision.start_attack
                or decision.stop_cast
                or decision.optional_off_gcd_prefixes
            ):
                raise ValueError(
                    "queue/GCD block decisions cannot own target, controls, or "
                    "off-GCD prefixes"
                )
            if decision.queue_op is QueueLaneOp.SET:
                queue_count += 1
                if decision.gcd_action is not None:
                    raise ValueError("queue block alternative cannot also own a GCD")
                expected_action = decision.queue_action
            elif decision.gcd_action is not None:
                gcd_count += 1
                if decision.queue_op is QueueLaneOp.CANCEL:
                    cancel_with_gcd_count += 1
                elif decision.queue_op is not QueueLaneOp.KEEP:
                    raise ValueError(
                        "GCD block alternative must use queue KEEP or CANCEL"
                    )
                expected_action = decision.gcd_action
            else:
                raise ValueError(
                    "queue/GCD block alternative must set a queue action or a GCD"
                )
            if row.guard.action_ready != expected_action:
                raise ValueError(
                    "queue/GCD block guard.action_ready must equal its action"
                )
        if self.block_alternatives and (
            queue_count > 0 and cancel_with_gcd_count > 0
        ):
            raise ValueError(
                "one searched block cannot mix guarded SET lanes with "
                "GCD-coupled CANCEL"
            )
        if self.kind != (
            "SEARCHED_QUEUE_GCD_BLOCK_OVER_IMPORTED_REACTIVE_INCUMBENT"
        ):
            raise ValueError("imported queue/GCD block selector kind is fixed")

    def to_dict(self) -> JSONMap:
        return {
            "schema": _SELECTOR_SCHEMA,
            "kind": self.kind,
            "block_alternatives": [
                row.to_dict() for row in self.block_alternatives
            ],
            "imported_fallback": self.imported_fallback.to_dict(),
            "replacement_contract": (
                "MATCHED_QUEUE_OR_GCD_LANE_REPLACEMENT_WITH_OTHER_SOURCE_LANE_PRESERVED"
            ),
        }


@dataclass(frozen=True)
class ImportedReactiveBurstQueueGcdBlockSelectorV1:
    """Compose searched burst and queue/GCD changes over one source decision.

    Terminal burst alternatives retain first refusal.  Otherwise the imported
    source decision is resolved exactly once, searched off-GCD insertions are
    spliced around its ordered sinks, and only then is its queue/terminal GCD
    block atomically replaced.  The latter step therefore cannot discard a
    searched burst prefix or take ownership of source target/control sinks.
    """

    terminal_alternatives: tuple[GuardedAlternativeV1, ...]
    imported_fallback: ImportedReactiveSelectorV1
    block_alternatives: tuple[GuardedAlternativeV1, ...]
    off_gcd_insertions: tuple[SearchedOffGcdInsertionV1, ...] = ()
    insertion_order: tuple[str, ...] = ()
    insertion_position: str = (
        "PER_INSERTION_BEFORE_SOURCE_OR_AFTER_SOURCE_SET_TARGET"
    )
    kind: str = (
        "SEARCHED_BURST_AND_QUEUE_GCD_BLOCK_OVER_IMPORTED_REACTIVE_INCUMBENT"
    )

    def __post_init__(self) -> None:
        # Reuse the two independently tested component contracts.  This keeps
        # their action-lane and insertion-order invariants identical rather
        # than maintaining a subtly different composite copy.
        ImportedFallbackOverlaySelectorV1(
            terminal_alternatives=self.terminal_alternatives,
            imported_fallback=self.imported_fallback,
            off_gcd_insertions=self.off_gcd_insertions,
            insertion_order=self.insertion_order,
            insertion_position=self.insertion_position,
        )
        ImportedReactiveQueueGcdBlockSelectorV1(
            block_alternatives=self.block_alternatives,
            imported_fallback=self.imported_fallback,
        )
        if self.kind != (
            "SEARCHED_BURST_AND_QUEUE_GCD_BLOCK_OVER_IMPORTED_REACTIVE_INCUMBENT"
        ):
            raise ValueError("composite burst/queue/GCD selector kind is fixed")

    def to_dict(self) -> JSONMap:
        return {
            "schema": _SELECTOR_SCHEMA,
            "kind": self.kind,
            "terminal_alternatives": [
                row.to_dict() for row in self.terminal_alternatives
            ],
            "imported_fallback": self.imported_fallback.to_dict(),
            "off_gcd_insertions": [
                row.to_dict() for row in self.off_gcd_insertions
            ],
            "insertion_order": list(self.insertion_order),
            "insertion_position": self.insertion_position,
            "block_alternatives": [
                row.to_dict() for row in self.block_alternatives
            ],
            "composition_contract": (
                "TERMINAL_BURST_OR_IMPORTED_THEN_OFF_GCD_THEN_QUEUE_GCD_BLOCK"
            ),
            "replacement_contract": (
                "MATCHED_QUEUE_OR_GCD_LANE_REPLACEMENT_WITH_OTHER_SOURCE_LANE_PRESERVED"
            ),
        }


ProgramSelectorV1 = (
    OrderedGuardSelectorV1
    | ImportedReactiveSelectorV1
    | ImportedFallbackOverlaySelectorV1
    | ImportedReactiveQueueGcdBlockSelectorV1
    | ImportedReactiveBurstQueueGcdBlockSelectorV1
)


def _alternative_from_dict_v1(value: object) -> GuardedAlternativeV1:
    raw = _exact_mapping(
        value, {"alternative_id", "guard", "decision"}, "alternative"
    )
    return GuardedAlternativeV1(
        alternative_id=raw["alternative_id"],
        guard=observable_causal_guard_from_dict_v1(
            raw["guard"], label="alternative.guard"
        ),
        decision=program_decision_from_dict_v1(raw["decision"]),
    )


def _insertion_from_dict_v1(value: object) -> SearchedOffGcdInsertionV1:
    raw = _exact_mapping(
        value,
        {"insertion_id", "prefix", "insertion_point"},
        "off-GCD insertion",
    )
    try:
        insertion_point = ProgramInsertionPointV1(raw["insertion_point"])
    except (TypeError, ValueError) as error:
        raise ValueError("off-GCD insertion point is unsupported") from error
    return SearchedOffGcdInsertionV1(
        insertion_id=raw["insertion_id"],
        prefix=optional_off_gcd_prefix_from_dict_v1(raw["prefix"]),
        insertion_point=insertion_point,
    )


def program_selector_from_dict_v1(value: object) -> ProgramSelectorV1:
    if not isinstance(value, Mapping):
        raise ValueError("program selector must be an object")
    kind = value.get("kind")
    if kind == "ORDERED_OBSERVABLE_ALTERNATIVES":
        raw = _exact_mapping(
            value,
            {"schema", "kind", "alternatives", "fallback"},
            "ordered selector",
        )
        if raw["schema"] != _SELECTOR_SCHEMA:
            raise ValueError("selector schema is unsupported")
        alternatives = raw["alternatives"]
        if not isinstance(alternatives, list):
            raise ValueError("selector alternatives must be an array")
        return OrderedGuardSelectorV1(
            alternatives=tuple(
                _alternative_from_dict_v1(row) for row in alternatives
            ),
            fallback=program_decision_from_dict_v1(raw["fallback"]),
        )
    if kind == "IMPORTED_REACTIVE_INCUMBENT":
        raw = _exact_mapping(
            value,
            {
                "schema",
                "kind",
                "binding_id",
                "source_policy_id",
                "observation_contract_id",
            },
            "imported selector",
        )
        if raw["schema"] != _SELECTOR_SCHEMA:
            raise ValueError("selector schema is unsupported")
        return ImportedReactiveSelectorV1(
            binding_id=raw["binding_id"],
            source_policy_id=raw["source_policy_id"],
            observation_contract_id=raw["observation_contract_id"],
        )
    if kind == "SEARCHED_BURST_OVER_IMPORTED_REACTIVE_INCUMBENT":
        raw = _exact_mapping(
            value,
            {
                "schema",
                "kind",
                "terminal_alternatives",
                "imported_fallback",
                "off_gcd_insertions",
                "insertion_order",
                "insertion_position",
            },
            "imported-fallback overlay selector",
        )
        if raw["schema"] != _SELECTOR_SCHEMA:
            raise ValueError("selector schema is unsupported")
        alternatives = raw["terminal_alternatives"]
        insertions = raw["off_gcd_insertions"]
        insertion_order = raw["insertion_order"]
        if not isinstance(alternatives, list):
            raise ValueError("terminal_alternatives must be an array")
        if not isinstance(insertions, list):
            raise ValueError("off_gcd_insertions must be an array")
        if not isinstance(insertion_order, list):
            raise ValueError("insertion_order must be an array")
        imported = program_selector_from_dict_v1(raw["imported_fallback"])
        if not isinstance(imported, ImportedReactiveSelectorV1):
            raise ValueError("overlay imported_fallback is not imported")
        return ImportedFallbackOverlaySelectorV1(
            terminal_alternatives=tuple(
                _alternative_from_dict_v1(row) for row in alternatives
            ),
            imported_fallback=imported,
            off_gcd_insertions=tuple(
                _insertion_from_dict_v1(row) for row in insertions
            ),
            insertion_order=tuple(insertion_order),
            insertion_position=raw["insertion_position"],
        )
    if kind == "SEARCHED_QUEUE_GCD_BLOCK_OVER_IMPORTED_REACTIVE_INCUMBENT":
        raw = _exact_mapping(
            value,
            {
                "schema",
                "kind",
                "block_alternatives",
                "imported_fallback",
                "replacement_contract",
            },
            "imported queue/GCD block selector",
        )
        if raw["schema"] != _SELECTOR_SCHEMA:
            raise ValueError("selector schema is unsupported")
        if raw["replacement_contract"] != (
            "MATCHED_QUEUE_OR_GCD_LANE_REPLACEMENT_WITH_OTHER_SOURCE_LANE_PRESERVED"
        ):
            raise ValueError("queue/GCD block replacement contract differs from v1")
        alternatives = raw["block_alternatives"]
        if not isinstance(alternatives, list):
            raise ValueError("block_alternatives must be an array")
        imported = program_selector_from_dict_v1(raw["imported_fallback"])
        if not isinstance(imported, ImportedReactiveSelectorV1):
            raise ValueError("queue/GCD block imported_fallback is not imported")
        return ImportedReactiveQueueGcdBlockSelectorV1(
            block_alternatives=tuple(
                _alternative_from_dict_v1(row) for row in alternatives
            ),
            imported_fallback=imported,
        )
    if kind == (
        "SEARCHED_BURST_AND_QUEUE_GCD_BLOCK_OVER_IMPORTED_REACTIVE_INCUMBENT"
    ):
        raw = _exact_mapping(
            value,
            {
                "schema",
                "kind",
                "terminal_alternatives",
                "imported_fallback",
                "off_gcd_insertions",
                "insertion_order",
                "insertion_position",
                "block_alternatives",
                "composition_contract",
                "replacement_contract",
            },
            "composite burst/queue/GCD selector",
        )
        if raw["schema"] != _SELECTOR_SCHEMA:
            raise ValueError("selector schema is unsupported")
        if raw["composition_contract"] != (
            "TERMINAL_BURST_OR_IMPORTED_THEN_OFF_GCD_THEN_QUEUE_GCD_BLOCK"
        ):
            raise ValueError("composite selector composition contract differs from v1")
        if raw["replacement_contract"] != (
            "MATCHED_QUEUE_OR_GCD_LANE_REPLACEMENT_WITH_OTHER_SOURCE_LANE_PRESERVED"
        ):
            raise ValueError("queue/GCD block replacement contract differs from v1")
        terminal_alternatives = raw["terminal_alternatives"]
        insertions = raw["off_gcd_insertions"]
        insertion_order = raw["insertion_order"]
        block_alternatives = raw["block_alternatives"]
        if not isinstance(terminal_alternatives, list):
            raise ValueError("terminal_alternatives must be an array")
        if not isinstance(insertions, list):
            raise ValueError("off_gcd_insertions must be an array")
        if not isinstance(insertion_order, list):
            raise ValueError("insertion_order must be an array")
        if not isinstance(block_alternatives, list):
            raise ValueError("block_alternatives must be an array")
        imported = program_selector_from_dict_v1(raw["imported_fallback"])
        if not isinstance(imported, ImportedReactiveSelectorV1):
            raise ValueError("composite imported_fallback is not imported")
        return ImportedReactiveBurstQueueGcdBlockSelectorV1(
            terminal_alternatives=tuple(
                _alternative_from_dict_v1(row)
                for row in terminal_alternatives
            ),
            imported_fallback=imported,
            block_alternatives=tuple(
                _alternative_from_dict_v1(row) for row in block_alternatives
            ),
            off_gcd_insertions=tuple(
                _insertion_from_dict_v1(row) for row in insertions
            ),
            insertion_order=tuple(insertion_order),
            insertion_position=raw["insertion_position"],
        )
    raise ValueError("program selector kind is unsupported")


@dataclass(frozen=True)
class CausalActionProgramV1:
    """One train/eval candidate, whether searched or source-imported."""

    program_id: str
    selector: ProgramSelectorV1
    origin: ProgramOriginV1
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "program_id", _text(self.program_id, "program_id"))
        if not isinstance(
            self.selector,
            (
                OrderedGuardSelectorV1,
                ImportedReactiveSelectorV1,
                ImportedFallbackOverlaySelectorV1,
                ImportedReactiveQueueGcdBlockSelectorV1,
                ImportedReactiveBurstQueueGcdBlockSelectorV1,
            ),
        ):
            raise TypeError("selector has an unsupported program type")
        if not isinstance(self.origin, ProgramOriginV1):
            raise TypeError("origin must be ProgramOriginV1")
        if not isinstance(self.source_refs, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.source_refs
        ):
            raise TypeError("source_refs must contain non-empty strings")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source_refs must be unique")
        if isinstance(self.selector, ImportedReactiveSelectorV1) and (
            self.origin
            not in (
                ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
                ProgramOriginV1.SEARCHED_REACTIVE,
                ProgramOriginV1.SOURCE_DERIVED_OFFLINE,
            )
        ):
            raise ValueError(
                "an imported selector requires imported, searched-reactive, "
                "or source-derived offline origin"
            )

    def to_dict(self) -> JSONMap:
        return {
            "schema": _SCHEMA,
            "program_id": self.program_id,
            "origin": self.origin.value,
            "selector": self.selector.to_dict(),
            "source_refs": list(self.source_refs),
            "causal_contract": {
                "selector_receives_raw_dynamic_state": False,
                "future_target_or_team_events_visible": False,
                "alternatives_evaluated_from_current_observation_only": True,
                "fallback_unconditional": True,
                "optional_off_gcd_prefixes_reconsidered_each_epoch": True,
            },
        }

    def program_key(self) -> str:
        """Canonical frozen semantic/provenance identity."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def causal_action_program_from_dict_v1(value: object) -> CausalActionProgramV1:
    raw = _exact_mapping(
        value,
        {
            "schema",
            "program_id",
            "origin",
            "selector",
            "source_refs",
            "causal_contract",
        },
        "causal action program",
    )
    if raw["schema"] != _SCHEMA:
        raise ValueError("causal action program schema is unsupported")
    expected_contract = {
        "selector_receives_raw_dynamic_state": False,
        "future_target_or_team_events_visible": False,
        "alternatives_evaluated_from_current_observation_only": True,
        "fallback_unconditional": True,
        "optional_off_gcd_prefixes_reconsidered_each_epoch": True,
    }
    if raw["causal_contract"] != expected_contract:
        raise ValueError("causal action program contract differs from v1")
    sources = raw["source_refs"]
    if not isinstance(sources, list):
        raise ValueError("source_refs must be an array")
    try:
        origin = ProgramOriginV1(raw["origin"])
    except (TypeError, ValueError) as error:
        raise ValueError("program origin is unsupported") from error
    return CausalActionProgramV1(
        program_id=raw["program_id"],
        selector=program_selector_from_dict_v1(raw["selector"]),
        origin=origin,
        source_refs=tuple(sources),
    )


class CausalObservationProjectorV1(Protocol):
    def __call__(
        self,
        state: Mapping[str, Any],
        available_actions: tuple[AvailableAction, ...],
    ) -> CausalLiveStateProjectionV1: ...


class CausalActionProgramReplayV1(Protocol):
    def replay(
        self,
        seed: int,
        program: CausalActionProgramV1,
        *,
        max_decisions: int = 10_000,
    ) -> ScheduleReplayOutcomeV1: ...


ReactiveResolverV1 = Callable[
    [CausalLiveStateProjectionV1, tuple[AvailableAction, ...]],
    ProgramDecisionV1,
]
ReactiveResolverFactoryV1 = Callable[[], ReactiveResolverV1]


@dataclass(frozen=True)
class ImportedReactiveProgramBindingV1:
    """Runtime-only factory around an unmodified, possibly stateful adapter.

    A new resolver is opened for every fresh seed replay.  Its internal state
    then persists across all decision epochs of that replay, preserving source
    latches and ordered-sink behavior without leaking state between seeds.
    """

    binding_id: str
    source_policy_id: str
    observation_contract_id: str
    resolver_factory: ReactiveResolverFactoryV1

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "source_policy_id",
            "observation_contract_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not callable(self.resolver_factory):
            raise TypeError("resolver_factory must be callable")

    def open_session(self) -> ReactiveResolverV1:
        resolver = self.resolver_factory()
        if not callable(resolver):
            raise TypeError("resolver_factory must return a callable resolver")
        return resolver


@dataclass
class _ImportedReactiveProgramSessionV1:
    binding_id: str
    source_policy_id: str
    observation_contract_id: str
    resolver: ReactiveResolverV1
    _pending_decision: ProgramDecisionV1 | None = None

    def decide(
        self,
        observation: CausalLiveStateProjectionV1,
        available_actions: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if self._pending_decision is not None:
            raise CausalActionProgramError(
                "imported resolver received a new decision before execution feedback"
            )
        decision = self.resolver(observation, available_actions)
        if not isinstance(decision, ProgramDecisionV1):
            raise TypeError("imported resolver must return ProgramDecisionV1")
        self._pending_decision = decision
        return decision

    def confirm_pending_execution(
        self,
        actual_decision: ProgramDecisionV1,
        execution_receipts: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Commit runtime-only resolver state after the bridge accepted a plan."""

        if self._pending_decision is None:
            return
        receipt_callback = getattr(
            self.resolver, "record_last_execution_receipt_v1", None
        )
        if callable(receipt_callback):
            receipt_callback(
                actual_decision,
                tuple(dict(row) for row in execution_receipts),
            )
            self._pending_decision = None
            return
        callback = getattr(self.resolver, "record_last_executed_decision_v1", None)
        if callable(callback):
            callback(actual_decision)
        self._pending_decision = None

    def reject_pending_execution(self, reason: str) -> None:
        """Release a proposal which never became an accepted bridge action plan."""

        if self._pending_decision is None:
            return
        callback = getattr(self.resolver, "reject_last_execution_v1", None)
        if callable(callback):
            callback(reason)
        self._pending_decision = None


def _state_time(state: Mapping[str, Any]) -> int:
    value = state.get("time_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CausalActionProgramError(
            "bridge state lacks nonnegative integer time_ms"
        )
    return value


def _state_damage(state: Mapping[str, Any]) -> float:
    lifecycle = state.get("dynamic_team_background")
    value = (
        lifecycle.get("simulated_damage_applied")
        if isinstance(lifecycle, Mapping)
        else state.get("damage_done")
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return 0.0
    return float(value)


def _advance_to_input(bridge: Any, state: Mapping[str, Any]) -> JSONMap:
    current = dict(state)
    count = 0
    while not bool(current.get("finished")) and not bool(
        current.get("needs_input")
    ):
        current = dict(bridge.advance())
        count += 1
        if count > 100_000:
            raise CausalActionProgramError(
                "advance loop exceeded 100000 transitions"
            )
    return current


_FORBIDDEN_PROJECTED_ROOT_FIELDS = frozenset(
    {
        "dynamic_config_sha256",
        "dynamic_idle_advance",
        "encounter_health_target",
        "remaining_ms",
        "wake_ready",
    }
)


def _project_observation(
    projector: CausalObservationProjectorV1,
    state: Mapping[str, Any],
    available: tuple[AvailableAction, ...],
) -> CausalLiveStateProjectionV1:
    projected = projector(state, available)
    if not isinstance(projected, CausalLiveStateProjectionV1):
        raise TypeError(
            "observation_projector must return CausalLiveStateProjectionV1"
        )
    leaked = sorted(_FORBIDDEN_PROJECTED_ROOT_FIELDS & set(projected.state))
    if leaked:
        raise CausalActionProgramError(
            f"causal observation contains future/control fields: {leaked}"
        )
    if projected.visibility_cutoff_ms != _state_time(state):
        raise CausalActionProgramError(
            "causal observation cutoff differs from current simulator time"
        )
    return projected


def _evaluate_selector_guard_v1(
    guard: ObservableCausalGuardV1,
    observation: CausalLiveStateProjectionV1,
    available: tuple[AvailableAction, ...],
) -> GuardEvaluationV1:
    """Treat a not-yet-visible target as causal false, not malformed state.

    Policy target indexes are compact indexes into the prefix-visible registry.
    An index beyond that registry denotes a later target that has not entered
    the observation yet.  Once the index is visible, ordinary guard evaluation
    remains strict so missing HP or attackability is still an error.
    """

    if (
        guard.target_index is not None
        and guard.target_index
        >= len(observation.policy_to_simulator_target_index)
    ):
        return GuardEvaluationV1(
            satisfied=False,
            failed_predicates=("target_not_prefix_visible",),
            observed={
                "target_index": guard.target_index,
                "target_visible": False,
                "visible_target_count": len(
                    observation.policy_to_simulator_target_index
                ),
            },
        )
    return evaluate_observable_guard_v1(guard, observation.state, available)


def _resolve_imported_decision_v1(
    selector: ImportedReactiveSelectorV1,
    observation: CausalLiveStateProjectionV1,
    available: tuple[AvailableAction, ...],
    imported_bindings: Mapping[str, _ImportedReactiveProgramSessionV1],
    receipts: list[JSONMap],
    decision_index: int,
) -> ProgramDecisionV1:
    binding = imported_bindings.get(selector.binding_id)
    if binding is None:
        raise CausalActionProgramError(
            f"imported selector binding is absent: {selector.binding_id}"
        )
    expected = (
        selector.binding_id,
        selector.source_policy_id,
        selector.observation_contract_id,
    )
    actual = (
        binding.binding_id,
        binding.source_policy_id,
        binding.observation_contract_id,
    )
    if actual != expected:
        raise CausalActionProgramError(
            "imported selector identity differs from runtime binding"
        )
    decision = binding.decide(observation, available)
    power = observation.state.get("power")
    receipts.append(
        {
            "decision_index": decision_index,
            "kind": "IMPORTED_REACTIVE_INCUMBENT_SELECTED",
            "binding_id": binding.binding_id,
            "source_policy_id": binding.source_policy_id,
            "state_time_ms": observation.visibility_cutoff_ms,
            "power_current": power.get("current") if isinstance(power, Mapping) else None,
            "gcd_action": (
                decision.gcd_action.to_wire() if decision.gcd_action is not None else None
            ),
            "queue_op": decision.queue_op.value,
            "wait_ms": decision.wait_ms,
        }
    )
    return decision


def _evaluate_ordered_alternatives_v1(
    alternatives: Sequence[GuardedAlternativeV1],
    observation: CausalLiveStateProjectionV1,
    available: tuple[AvailableAction, ...],
    receipts: list[JSONMap],
    decision_index: int,
    *,
    receipt_prefix: str,
) -> ProgramDecisionV1 | None:
    for alternative_index, alternative in enumerate(alternatives):
        evaluation = _evaluate_selector_guard_v1(
            alternative.guard, observation, available
        )
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": f"{receipt_prefix}_GUARD",
                "alternative_index": alternative_index,
                "alternative_id": alternative.alternative_id,
                "matched": evaluation.satisfied,
                "evaluation": evaluation.to_dict(),
                "state_time_ms": observation.visibility_cutoff_ms,
            }
        )
        if evaluation.satisfied:
            receipts.append(
                {
                    "decision_index": decision_index,
                    "kind": f"{receipt_prefix}_SELECTED",
                    "alternative_id": alternative.alternative_id,
                    "state_time_ms": observation.visibility_cutoff_ms,
                }
            )
            return alternative.decision
    return None


def _prepend_searched_insertions_v1(
    selector: (
        ImportedFallbackOverlaySelectorV1
        | ImportedReactiveBurstQueueGcdBlockSelectorV1
    ),
    source_decision: ProgramDecisionV1,
    observation: CausalLiveStateProjectionV1,
    *,
    receipts: list[JSONMap],
    decision_index: int,
) -> ProgramDecisionV1:
    state_time_ms = observation.visibility_cutoff_ms
    if not selector.off_gcd_insertions:
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "EMPTY_OVERLAY_SOURCE_DECISION_UNCHANGED",
                "source_prefix_order": [
                    value.value for value in source_decision.prefix_order or ()
                ],
                "state_time_ms": state_time_ms,
            }
        )
        return source_decision
    insertion_by_id = {
        row.insertion_id: row for row in selector.off_gcd_insertions
    }
    ordered = tuple(
        insertion_by_id[insertion_id]
        for insertion_id in selector.insertion_order
    )
    selected_target = source_decision.target_index
    target_source = "SOURCE_DECISION"
    if selected_target is None:
        observed_target = observation.state.get("target_index")
        if (
            isinstance(observed_target, int)
            and not isinstance(observed_target, bool)
            and 0
            <= observed_target
            < len(observation.policy_to_simulator_target_index)
        ):
            selected_target = observed_target
            target_source = "CURRENT_CAUSAL_OBSERVATION"
        else:
            target_source = "NO_PREFIX_VISIBLE_SELECTED_TARGET"
    eligible: list[SearchedOffGcdInsertionV1] = []
    deferred: list[JSONMap] = []
    for insertion in ordered:
        guard_target = insertion.prefix.guard.target_index
        if guard_target is None or guard_target == selected_target:
            eligible.append(insertion)
            continue
        deferred.append(
            {
                "insertion_id": insertion.insertion_id,
                "guard_target_index": guard_target,
                "selected_target_index": selected_target,
                "reason": "TARGET_MISMATCH_RECONSIDER_NEXT_EPOCH",
            }
        )
    for row in deferred:
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "SEARCHED_OFF_GCD_INSERTION_TARGET_DEFERRED",
                **row,
                "target_source": target_source,
                "state_time_ms": state_time_ms,
            }
        )
    if not eligible:
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "NO_TARGET_MATCHING_OVERLAY_SOURCE_DECISION_UNCHANGED",
                "configured_insertion_order": list(selector.insertion_order),
                "selected_target_index": selected_target,
                "target_source": target_source,
                "source_prefix_order": [
                    value.value for value in source_decision.prefix_order or ()
                ],
                "state_time_ms": state_time_ms,
            }
        )
        return source_decision

    before = tuple(
        row
        for row in eligible
        if row.insertion_point
        is ProgramInsertionPointV1.BEFORE_SOURCE_PREFIX_ORDER
    )
    after_target = tuple(
        row
        for row in eligible
        if row.insertion_point
        is ProgramInsertionPointV1.AFTER_SOURCE_SET_TARGET
    )
    source_order = source_decision.prefix_order or ()
    combined_order: list[ProgramPrefixOperationKindV1] = [
        ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD for _ in before
    ]
    combined_prefixes: list[OptionalOffGcdPrefixV1] = [
        row.prefix for row in before
    ]
    source_prefix_index = 0
    after_inserted = False
    if after_target and source_decision.target_index is None:
        # The incumbent explicitly keeps the current target.  The causal
        # projection above binds which target that is, so no synthetic target
        # switch is needed before target-affecting insertions.
        combined_order.extend(
            ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
            for _ in after_target
        )
        combined_prefixes.extend(row.prefix for row in after_target)
        after_inserted = True
    for kind in source_order:
        combined_order.append(kind)
        if kind is ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD:
            combined_prefixes.append(
                source_decision.optional_off_gcd_prefixes[
                    source_prefix_index
                ]
            )
            source_prefix_index += 1
        if (
            kind is ProgramPrefixOperationKindV1.SET_TARGET
            and after_target
            and not after_inserted
        ):
            combined_order.extend(
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
                for _ in after_target
            )
            combined_prefixes.extend(row.prefix for row in after_target)
            after_inserted = True
    if source_prefix_index != len(source_decision.optional_off_gcd_prefixes):
        raise CausalActionProgramError(
            "source prefix order does not consume its optional off-GCD sinks"
        )
    if after_target and not after_inserted:
        raise CausalActionProgramError(
            "target-affecting insertion has no source or current target binding"
        )
    composed = replace(
        source_decision,
        optional_off_gcd_prefixes=tuple(combined_prefixes),
        prefix_order=tuple(combined_order),
    )
    receipts.append(
        {
            "decision_index": decision_index,
            "kind": "SEARCHED_OFF_GCD_PREFIXES_PREPENDED",
            "configured_insertion_order": list(selector.insertion_order),
            "eligible_insertion_order": [
                row.insertion_id for row in eligible
            ],
            "insertion_position": selector.insertion_position,
            "insertion_points": {
                row.insertion_id: row.insertion_point.value for row in eligible
            },
            "selected_target_index": selected_target,
            "target_source": target_source,
            "source_prefix_order": [value.value for value in source_order],
            "composed_prefix_order": [
                value.value for value in composed.prefix_order or ()
            ],
            "state_time_ms": state_time_ms,
        }
    )
    return composed


def _replace_queue_gcd_block_v1(
    selector: (
        ImportedReactiveQueueGcdBlockSelectorV1
        | ImportedReactiveBurstQueueGcdBlockSelectorV1
    ),
    source_decision: ProgramDecisionV1,
    observation: CausalLiveStateProjectionV1,
    available: tuple[AvailableAction, ...],
    *,
    receipts: list[JSONMap],
    decision_index: int,
) -> ProgramDecisionV1:
    """Select one searched lane decision without taking over Cat's target."""

    state_time_ms = observation.visibility_cutoff_ms
    if source_decision.gcd_action is not None:
        source_available = next(
            (
                row
                for row in available
                if row.action == source_decision.gcd_action
            ),
            None,
        )
        if source_available is not None and not source_available.result_bearing:
            receipts.append(
                {
                    "decision_index": decision_index,
                    "kind": "QUEUE_GCD_BLOCK_SOURCE_NON_RESULT_GCD_PRESERVED",
                    "source_gcd_action": source_decision.gcd_action.to_wire(),
                    "state_time_ms": state_time_ms,
                }
            )
            return source_decision
    selected_target = source_decision.target_index
    target_source = "SOURCE_DECISION"
    if selected_target is None:
        observed_target = observation.state.get("target_index")
        if (
            isinstance(observed_target, int)
            and not isinstance(observed_target, bool)
            and 0
            <= observed_target
            < len(observation.policy_to_simulator_target_index)
        ):
            selected_target = observed_target
            target_source = "CURRENT_CAUSAL_OBSERVATION"
        else:
            target_source = "NO_PREFIX_VISIBLE_SELECTED_TARGET"

    selected: GuardedAlternativeV1 | None = None
    for alternative_index, alternative in enumerate(
        selector.block_alternatives
    ):
        guard_target = alternative.guard.target_index
        if guard_target is not None and guard_target != selected_target:
            receipts.append(
                {
                    "decision_index": decision_index,
                    "kind": "QUEUE_GCD_BLOCK_TARGET_DEFERRED",
                    "alternative_index": alternative_index,
                    "alternative_id": alternative.alternative_id,
                    "guard_target_index": guard_target,
                    "selected_target_index": selected_target,
                    "target_source": target_source,
                    "state_time_ms": state_time_ms,
                }
            )
            continue
        evaluation = _evaluate_selector_guard_v1(
            alternative.guard, observation, available
        )
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "QUEUE_GCD_BLOCK_GUARD",
                "alternative_index": alternative_index,
                "alternative_id": alternative.alternative_id,
                "matched": evaluation.satisfied,
                "evaluation": evaluation.to_dict(),
                "selected_target_index": selected_target,
                "target_source": target_source,
                "state_time_ms": state_time_ms,
            }
        )
        if evaluation.satisfied:
            selected = alternative
            break

    if selected is None:
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "QUEUE_GCD_BLOCK_SOURCE_DECISION_UNCHANGED",
                "selected_target_index": selected_target,
                "target_source": target_source,
                "state_time_ms": state_time_ms,
            }
        )
        return source_decision

    replacement = selected.decision
    inherit_source_gcd = replacement.gcd_action is None
    inherit_source_queue = (
        replacement.gcd_action is not None
        and replacement.queue_op is QueueLaneOp.KEEP
    )
    effective_queue_op = (
        source_decision.queue_op
        if inherit_source_queue
        else replacement.queue_op
    )
    effective_queue_action = (
        source_decision.queue_action if inherit_source_queue else replacement.queue_action
    )
    effective_gcd_action = (
        source_decision.gcd_action
        if inherit_source_gcd
        else replacement.gcd_action
    )
    effective_wait_ms = (
        source_decision.wait_ms if inherit_source_gcd else replacement.wait_ms
    )
    replacement_queue_kind = {
        QueueLaneOp.KEEP: ProgramPrefixOperationKindV1.QUEUE_KEEP,
        QueueLaneOp.SET: ProgramPrefixOperationKindV1.QUEUE_SET,
        QueueLaneOp.CANCEL: ProgramPrefixOperationKindV1.QUEUE_CANCEL,
    }[effective_queue_op]
    queue_kinds = {
        ProgramPrefixOperationKindV1.QUEUE_KEEP,
        ProgramPrefixOperationKindV1.QUEUE_SET,
        ProgramPrefixOperationKindV1.QUEUE_CANCEL,
    }
    source_order = source_decision.prefix_order or ()
    source_queue_count = sum(kind in queue_kinds for kind in source_order)
    if source_queue_count != 1:
        raise CausalActionProgramError(
            "imported source decision must contain exactly one queue sink"
        )
    composed_order = tuple(
        replacement_queue_kind if kind in queue_kinds else kind
        for kind in source_order
    )
    composed = replace(
        source_decision,
        queue_op=effective_queue_op,
        queue_action=effective_queue_action,
        gcd_action=effective_gcd_action,
        wait_ms=effective_wait_ms,
        prefix_order=composed_order,
    )
    receipts.append(
        {
            "decision_index": decision_index,
            "kind": "QUEUE_GCD_BLOCK_SELECTED",
            "alternative_id": selected.alternative_id,
            "selected_target_index": selected_target,
            "target_source": target_source,
            "source_queue_op": source_decision.queue_op.value,
            "configured_replacement_queue_op": replacement.queue_op.value,
            "replacement_queue_op": effective_queue_op.value,
            "source_queue_inherited": inherit_source_queue,
            "source_gcd_inherited": inherit_source_gcd,
            "source_gcd_action": (
                source_decision.gcd_action.to_wire()
                if source_decision.gcd_action is not None
                else None
            ),
            "replacement_gcd_action": (
                effective_gcd_action.to_wire()
                if effective_gcd_action is not None
                else None
            ),
            "preserved_source_prefix_order": [
                value.value for value in source_order if value not in queue_kinds
            ],
            "state_time_ms": state_time_ms,
        }
    )
    return composed


def _select_decision(
    program: CausalActionProgramV1,
    observation: CausalLiveStateProjectionV1,
    available: tuple[AvailableAction, ...],
    imported_bindings: Mapping[str, _ImportedReactiveProgramSessionV1],
    receipts: list[JSONMap],
    decision_index: int,
) -> ProgramDecisionV1:
    selector = program.selector
    if isinstance(selector, OrderedGuardSelectorV1):
        selected = _evaluate_ordered_alternatives_v1(
            selector.alternatives,
            observation,
            available,
            receipts,
            decision_index,
            receipt_prefix="ALTERNATIVE",
        )
        if selected is not None:
            return selected
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "UNCONDITIONAL_FALLBACK_SELECTED",
                "state_time_ms": observation.visibility_cutoff_ms,
            }
        )
        return selector.fallback
    if isinstance(selector, ImportedReactiveSelectorV1):
        return _resolve_imported_decision_v1(
            selector,
            observation,
            available,
            imported_bindings,
            receipts,
            decision_index,
        )
    if isinstance(selector, ImportedReactiveQueueGcdBlockSelectorV1):
        source = _resolve_imported_decision_v1(
            selector.imported_fallback,
            observation,
            available,
            imported_bindings,
            receipts,
            decision_index,
        )
        return _replace_queue_gcd_block_v1(
            selector,
            source,
            observation,
            available,
            receipts=receipts,
            decision_index=decision_index,
        )
    if not isinstance(
        selector,
        (
            ImportedFallbackOverlaySelectorV1,
            ImportedReactiveBurstQueueGcdBlockSelectorV1,
        ),
    ):
        raise CausalActionProgramError("program selector type is unsupported")
    terminal = _evaluate_ordered_alternatives_v1(
        selector.terminal_alternatives,
        observation,
        available,
        receipts,
        decision_index,
        receipt_prefix="OVERLAY_TERMINAL_ALTERNATIVE",
    )
    if terminal is not None:
        return terminal
    source = _resolve_imported_decision_v1(
        selector.imported_fallback,
        observation,
        available,
        imported_bindings,
        receipts,
        decision_index,
    )
    composed = _prepend_searched_insertions_v1(
        selector,
        source,
        observation,
        receipts=receipts,
        decision_index=decision_index,
    )
    if isinstance(selector, ImportedReactiveBurstQueueGcdBlockSelectorV1):
        return _replace_queue_gcd_block_v1(
            selector,
            composed,
            observation,
            available,
            receipts=receipts,
            decision_index=decision_index,
        )
    return composed


def _available_by_action(
    bridge: Any,
) -> tuple[tuple[AvailableAction, ...], dict[ActionRef, AvailableAction]]:
    available = tuple(bridge.actions())
    if any(not isinstance(row, AvailableAction) for row in available):
        raise TypeError("bridge actions must contain AvailableAction values")
    by_action = {row.action: row for row in available}
    if len(by_action) != len(available):
        raise CausalActionProgramError("bridge advertised duplicate actions")
    return available, by_action


def _terminal_telemetry_receipt_v1(
    bridge: Any,
    state: Mapping[str, Any],
    retained_damage_actions: frozenset[ActionRef],
) -> tuple[JSONMap, tuple[AvailableAction, ...]]:
    """Read one compact terminal snapshot without changing replay execution.

    The evaluator supplies the small action set it can actually attribute.  In
    particular, this does not retain a state snapshot for every physical press.
    Missing bridge surfaces are evidence gaps, not replay failures.
    """

    terminal_actions: tuple[AvailableAction, ...] = ()
    try:
        terminal_actions, _ = _available_by_action(bridge)
        action_surface: JSONMap = {
            "status": "OBSERVED",
            "actions": [
                {
                    "index": row.index,
                    "action": row.action.to_wire(),
                    "label": row.label,
                    "legal": row.legal,
                    "ready_in_ms": row.ready_in_ms,
                    "triggers_gcd": row.triggers_gcd,
                    "result_bearing": row.result_bearing,
                    "cooldown_duration_ms": row.cooldown_duration_ms,
                }
                for row in terminal_actions
            ],
        }
    except Exception as error:
        action_surface = {
            "status": "NOT_OBSERVED",
            "reason": "TERMINAL_ACTION_SURFACE_READ_FAILED",
            "error_type": type(error).__name__,
        }

    endpoint = getattr(bridge, "dynamic_candidate_damage_receipts", None)
    if not callable(endpoint):
        damage_surface: JSONMap = {
            "status": "NOT_OBSERVED",
            "reason": "CANDIDATE_DAMAGE_RECEIPT_ENDPOINT_UNAVAILABLE",
        }
    else:
        try:
            batch = endpoint(cursor=0)
            raw_rows = getattr(batch, "receipts", None)
            if not isinstance(raw_rows, tuple):
                raise TypeError("candidate damage receipt batch lacks tuple receipts")
            retained: list[JSONMap] = []
            for row in raw_rows:
                action = getattr(row, "action", None)
                if action not in retained_damage_actions:
                    continue
                if not isinstance(action, ActionRef):
                    raise TypeError("candidate damage receipt lacks ActionRef")
                retained.append(
                    {
                        "damage_ordinal": row.damage_ordinal,
                        "time_ms": row.time_ms,
                        "target_index": row.target_index,
                        "requested_damage": row.requested_damage,
                        "applied_damage": row.applied_damage,
                        "overkill_damage": row.overkill_damage,
                        "killed": row.killed,
                        "status": row.status,
                        "action": action.to_wire(),
                        "outcome": row.outcome,
                        "execution_id": row.execution_id,
                        "execution_index": row.execution_index,
                        "landed_execution_index": row.landed_execution_index,
                        "resolution_phase": row.resolution_phase,
                        "outcome_computed": row.outcome_computed,
                        "random_stream_rewound": row.random_stream_rewound,
                        "attempt_id": row.attempt_id,
                        "retargeted_to": row.retargeted_to,
                    }
                )
            damage_surface = {
                "status": "OBSERVED",
                "source_receipt_count": len(raw_rows),
                "retained_receipt_count": len(retained),
                "retained_actions": [
                    action.to_wire()
                    for action in sorted(retained_damage_actions)
                ],
                "receipts": retained,
            }
        except Exception as error:
            damage_surface = {
                "status": "NOT_OBSERVED",
                "reason": "CANDIDATE_DAMAGE_RECEIPT_READ_FAILED",
                "error_type": type(error).__name__,
            }

    return (
        {
            "kind": "NATIVE_TERMINAL_TELEMETRY_V1",
            "state_time_ms": _state_time(state),
            "terminal_action_surface": action_surface,
            "candidate_damage_surface": damage_surface,
        },
        terminal_actions,
    )


def _attempt_id(
    action: ActionRef,
    row: AvailableAction,
    result_bearing_action_refs: frozenset[ActionRef],
    *,
    decision_index: int,
    operation_id: str,
) -> str | None:
    if row.result_bearing or action in result_bearing_action_refs:
        return f"program-decision-{decision_index}:{operation_id}"
    return None


def _execute_decision(
    bridge: Any,
    state: Mapping[str, Any],
    decision: ProgramDecisionV1,
    *,
    decision_index: int,
    projector: CausalObservationProjectorV1,
    receipts: list[JSONMap],
    result_bearing_action_refs: frozenset[ActionRef],
    external_press_clock: bool = False,
    runtime_target_validator: Callable[
        [Mapping[str, Any], int], Mapping[str, Any] | None
    ]
    | None = None,
    runtime_action_validator: Callable[
        [Mapping[str, Any], ActionRef], Mapping[str, Any] | None
    ]
    | None = None,
) -> JSONMap:
    current = dict(state)
    prefix_index = 0
    assert decision.prefix_order is not None
    for operation_index, kind in enumerate(decision.prefix_order):
        if bool(current.get("finished")):
            return current
        if kind is ProgramPrefixOperationKindV1.SET_TARGET:
            assert decision.target_index is not None
            available, _ = _available_by_action(bridge)
            observation = _project_observation(projector, current, available)
            simulator_index = observation.simulator_target_index(
                decision.target_index
            )
            target_gate = (
                runtime_target_validator(current, simulator_index)
                if runtime_target_validator is not None
                else None
            )
            if target_gate is not None and not isinstance(target_gate, Mapping):
                raise TypeError("runtime target validator must return a mapping or None")
            result = bridge.set_target(simulator_index)
            current = dict(result.state)
            if result.target_index != simulator_index:
                raise CausalActionProgramError(
                    "set_target selected a different target"
                )
            receipts.append(
                {
                    "decision_index": decision_index,
                    "operation_index": operation_index,
                    "kind": "SET_TARGET",
                    "policy_target_index": decision.target_index,
                    "simulator_target_index": simulator_index,
                    "changed": bool(result.changed),
                    "state_time_ms": _state_time(current),
                    **(
                        {"target_gate": dict(target_gate)}
                        if target_gate is not None
                        else {}
                    ),
                }
            )
            continue
        if kind in {
            ProgramPrefixOperationKindV1.START_ATTACK,
            ProgramPrefixOperationKindV1.STOP_CAST,
        }:
            method_name = (
                "start_attack"
                if kind is ProgramPrefixOperationKindV1.START_ATTACK
                else "stop_cast"
            )
            method = getattr(bridge, method_name, None)
            if not callable(method):
                raise CausalActionProgramError(
                    f"bridge lacks imported source control {method_name}"
                )
            result = method()
            if not bool(result.accepted) or bool(result.consumes_decision):
                raise CausalActionProgramError(
                    f"{method_name} was rejected or consumed the decision"
                )
            current = dict(result.state)
            resulting_queue = current.get("swing_queue")
            receipts.append(
                {
                    "decision_index": decision_index,
                    "operation_index": operation_index,
                    "kind": kind.value,
                    "accepted": True,
                    "state_time_ms": _state_time(current),
                }
            )
            continue
        if kind is ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD:
            prefix = decision.optional_off_gcd_prefixes[prefix_index]
            current_prefix_index = prefix_index
            prefix_index += 1
            available, by_action = _available_by_action(bridge)
            observation = _project_observation(projector, current, available)
            evaluation = _evaluate_selector_guard_v1(
                prefix.guard, observation, available
            )
            if not evaluation.satisfied:
                receipts.append(
                    {
                        "decision_index": decision_index,
                        "operation_index": operation_index,
                        "kind": "OPTIONAL_OFF_GCD_SKIPPED",
                        "prefix_index": current_prefix_index,
                        "action": prefix.action.to_wire(),
                        "evaluation": evaluation.to_dict(),
                        "state_time_ms": _state_time(current),
                    }
                )
                continue
            row = by_action.get(prefix.action)
            if row is None or not row.legal or row.ready_in_ms != 0:
                raise CausalActionProgramError(
                    "satisfied optional prefix guard disagrees with action legality"
                )
            if row.triggers_gcd:
                raise CausalActionProgramError(
                    "optional off-GCD prefix is advertised as GCD-triggering"
                )
            action_gate = (
                runtime_action_validator(current, prefix.action)
                if runtime_action_validator is not None
                else None
            )
            if action_gate is not None and not isinstance(action_gate, Mapping):
                raise TypeError("runtime action validator must return a mapping or None")
            attempt_id = _attempt_id(
                prefix.action,
                row,
                result_bearing_action_refs,
                decision_index=decision_index,
                operation_id=f"optional-prefix-{current_prefix_index}",
            )
            result = bridge.act(prefix.action, attempt_id=attempt_id)
            if not result.casted or result.consumes_decision:
                raise CausalActionProgramError(
                    "optional off-GCD prefix was rejected or consumed the decision"
                )
            current = dict(result.state)
            receipts.append(
                {
                    "decision_index": decision_index,
                    "operation_index": operation_index,
                    "kind": "OPTIONAL_OFF_GCD_EXECUTED",
                    "prefix_index": current_prefix_index,
                    "action": prefix.action.to_wire(),
                    "attempt_id": attempt_id,
                    "state_time_ms": _state_time(current),
                    **(
                        {"action_gate": dict(action_gate)}
                        if action_gate is not None
                        else {}
                    ),
                }
            )
            continue
        if kind is ProgramPrefixOperationKindV1.QUEUE_KEEP:
            receipts.append(
                {
                    "decision_index": decision_index,
                    "operation_index": operation_index,
                    "kind": "QUEUE_KEEP",
                    "state_time_ms": _state_time(current),
                }
            )
            continue
        if kind is ProgramPrefixOperationKindV1.QUEUE_CANCEL:
            result = bridge.cancel_queue()
            resulting_queue = result.state.get("swing_queue")
            already_clear = bool(
                isinstance(resulting_queue, Mapping)
                and resulting_queue.get("status") == "NONE"
            )
            if result.consumes_decision or (
                not result.canceled and not already_clear
            ):
                raise CausalActionProgramError(
                    "queue cancellation failed or consumed the decision"
                )
            current = dict(result.state)
            receipts.append(
                {
                    "decision_index": decision_index,
                    "operation_index": operation_index,
                    "kind": (
                        "QUEUE_CANCEL"
                        if result.canceled
                        else "QUEUE_CANCEL_ALREADY_CLEAR"
                    ),
                    "state_time_ms": _state_time(current),
                }
            )
            continue
        if kind is ProgramPrefixOperationKindV1.QUEUE_SET:
            available, by_action = _available_by_action(bridge)
            row = by_action.get(decision.queue_action)
            if row is None or not row.legal or row.ready_in_ms != 0:
                raise CausalActionProgramError(
                    "queue action is not currently legal"
                )
            action_gate = (
                runtime_action_validator(current, decision.queue_action)
                if runtime_action_validator is not None
                else None
            )
            if action_gate is not None and not isinstance(action_gate, Mapping):
                raise TypeError("runtime action validator must return a mapping or None")
            attempt_id = _attempt_id(
                decision.queue_action,
                row,
                result_bearing_action_refs,
                decision_index=decision_index,
                operation_id="queue-set",
            )
            result = bridge.act(decision.queue_action, attempt_id=attempt_id)
            if not result.casted or result.consumes_decision:
                raise CausalActionProgramError(
                    "queue action failed or consumed the decision"
                )
            current = dict(result.state)
            resulting_queue = current.get("swing_queue")
            receipts.append(
                {
                    "decision_index": decision_index,
                    "operation_index": operation_index,
                    "kind": "QUEUE_SET",
                    "action": decision.queue_action.to_wire(),
                    "attempt_id": attempt_id,
                    "state_time_ms": _state_time(current),
                    "queue_state_after_acceptance": (
                        dict(resulting_queue)
                        if isinstance(resulting_queue, Mapping)
                        else {
                            "status": "NOT_OBSERVED",
                            "reason": "SWING_QUEUE_STATE_UNAVAILABLE_AFTER_ACCEPTANCE",
                        }
                    ),
                    **(
                        {"action_gate": dict(action_gate)}
                        if action_gate is not None
                        else {}
                    ),
                }
            )
            continue
        raise CausalActionProgramError(f"unsupported prefix operation {kind}")

    if bool(current.get("finished")):
        return current
    if decision.gcd_action is not None:
        available, by_action = _available_by_action(bridge)
        row = by_action.get(decision.gcd_action)
        if row is None or not row.legal or row.ready_in_ms != 0:
            raise CausalActionProgramError("terminal GCD action is not legal")
        if not row.triggers_gcd:
            raise CausalActionProgramError(
                "terminal GCD action is advertised as off-GCD"
            )
        action_gate = (
            runtime_action_validator(current, decision.gcd_action)
            if runtime_action_validator is not None
            else None
        )
        if action_gate is not None and not isinstance(action_gate, Mapping):
            raise TypeError("runtime action validator must return a mapping or None")
        attempt_id = _attempt_id(
            decision.gcd_action,
            row,
            result_bearing_action_refs,
            decision_index=decision_index,
            operation_id="terminal-gcd",
        )
        result = bridge.act(decision.gcd_action, attempt_id=attempt_id)
        if not result.casted or not result.consumes_decision:
            raise CausalActionProgramError(
                "terminal GCD action failed to consume the decision"
            )
        current = dict(result.state)
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "TERMINAL_GCD",
                "action": decision.gcd_action.to_wire(),
                "attempt_id": attempt_id,
                "state_time_ms": _state_time(current),
                **(
                    {"action_gate": dict(action_gate)}
                    if action_gate is not None
                    else {}
                ),
            }
        )
    elif external_press_clock:
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "TERMINAL_WAIT_EXTERNAL_PRESS_ABSTAIN",
                "configured_wait_ms": decision.wait_ms,
                "state_time_ms": _state_time(current),
            }
        )
    else:
        current = dict(bridge.wait(decision.wait_ms))
        receipts.append(
            {
                "decision_index": decision_index,
                "kind": "TERMINAL_WAIT",
                "wait_ms": decision.wait_ms,
                "state_time_ms": _state_time(current),
            }
        )
    return current


class NativeDynamicV3ActionProgramReplayV1:
    """Fresh-load state-feedback replay with schedule-compatible outcomes."""

    def __init__(
        self,
        bridge_factory: Callable[[], Any],
        case_factory: Callable[[int], Any],
        observation_projector: CausalObservationProjectorV1,
        *,
        imported_bindings: Sequence[ImportedReactiveProgramBindingV1] = (),
        result_bearing_action_refs: Sequence[ActionRef] = tuple(
            FURY_RESULT_BEARING_ACTION_REFS_V1
        ),
        terminal_telemetry_action_refs: Sequence[ActionRef] = (),
        external_press_period_ms: int | None = None,
        external_press_phase_ms: int = 0,
    ) -> None:
        if not callable(bridge_factory) or not callable(case_factory):
            raise TypeError("bridge_factory and case_factory must be callable")
        if not callable(observation_projector):
            raise TypeError("observation_projector must be callable")
        if any(
            not isinstance(row, ImportedReactiveProgramBindingV1)
            for row in imported_bindings
        ):
            raise TypeError(
                "imported_bindings must contain ImportedReactiveProgramBindingV1"
            )
        binding_ids = [row.binding_id for row in imported_bindings]
        if len(set(binding_ids)) != len(binding_ids):
            raise ValueError("imported binding IDs must be unique")
        refs = tuple(result_bearing_action_refs)
        for index, action in enumerate(refs):
            _action(action, f"result_bearing_action_refs[{index}]")
        if len(set(refs)) != len(refs):
            raise ValueError("result_bearing_action_refs must be unique")
        telemetry_refs = tuple(terminal_telemetry_action_refs)
        for index, action in enumerate(telemetry_refs):
            _action(action, f"terminal_telemetry_action_refs[{index}]")
        if len(set(telemetry_refs)) != len(telemetry_refs):
            raise ValueError("terminal_telemetry_action_refs must be unique")
        if type(external_press_phase_ms) is not int:
            raise ValueError("external_press_phase_ms must be an integer")
        if external_press_period_ms is None:
            if external_press_phase_ms != 0:
                raise ValueError(
                    "external_press_phase_ms requires external_press_period_ms"
                )
        else:
            if (
                type(external_press_period_ms) is not int
                or not 1 <= external_press_period_ms <= 60_000
            ):
                raise ValueError(
                    "external_press_period_ms must be an integer in 1..60000"
                )
            if (
                type(external_press_phase_ms) is not int
                or not 0 <= external_press_phase_ms < external_press_period_ms
            ):
                raise ValueError(
                    "external_press_phase_ms must be an integer in "
                    "0..external_press_period_ms-1"
                )
        self._bridge_factory = bridge_factory
        self._case_factory = case_factory
        self._observation_projector = observation_projector
        self._imported_bindings = {row.binding_id: row for row in imported_bindings}
        self._result_bearing_action_refs = frozenset(refs)
        self._terminal_telemetry_action_refs = frozenset(telemetry_refs)
        self._external_press_period_ms = external_press_period_ms
        self._external_press_phase_ms = external_press_phase_ms

    def replay(
        self,
        seed: int,
        program: CausalActionProgramV1,
        *,
        max_decisions: int = 10_000,
    ) -> ScheduleReplayOutcomeV1:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an integer")
        if not isinstance(program, CausalActionProgramV1):
            raise TypeError("program must be CausalActionProgramV1")
        _positive_int(max_decisions, "max_decisions")
        receipts: list[JSONMap] = []
        last_state: JSONMap = {"time_ms": 0, "damage_done": 0.0}
        try:
            case = self._case_factory(seed)
            imported_sessions = {
                binding_id: _ImportedReactiveProgramSessionV1(
                    binding_id=binding.binding_id,
                    source_policy_id=binding.source_policy_id,
                    observation_contract_id=binding.observation_contract_id,
                    resolver=binding.open_session(),
                )
                for binding_id, binding in self._imported_bindings.items()
            }
            with self._bridge_factory() as bridge:
                precombat = getattr(case, "precombat", None)
                external_press = self._external_press_period_ms is not None
                if precombat is None and not external_press:
                    loaded = bridge.load_dynamic_v3(
                        case.request, seed, case.dynamic_load.config
                    )
                elif precombat is None:
                    loaded = bridge.load_dynamic_v3_press_clock(
                        case.request,
                        seed,
                        case.dynamic_load.config,
                        self._external_press_period_ms,
                        self._external_press_phase_ms,
                    )
                elif not external_press:
                    loaded = bridge.load_dynamic_v3_precombat(
                        case.request,
                        seed,
                        case.dynamic_load.config,
                        precombat,
                    )
                else:
                    loaded = bridge.load_dynamic_v3_precombat_press_clock(
                        case.request,
                        seed,
                        case.dynamic_load.config,
                        precombat,
                        self._external_press_period_ms,
                        self._external_press_phase_ms,
                    )
                state = _advance_to_input(bridge, dict(loaded.state))
                last_state = dict(state)
                decision_index = 0
                while not bool(state.get("finished")):
                    if decision_index >= max_decisions:
                        raise CausalActionProgramError(
                            f"program exceeded max_decisions={max_decisions}"
                        )
                    if not bool(state.get("needs_input")):
                        state = _advance_to_input(bridge, state)
                        last_state = dict(state)
                        continue
                    press_index: int | None = None
                    if external_press:
                        clock = _press_clock_state_v1(state)
                        if not clock.ready:
                            raise CausalActionProgramError(
                                "policy input was not a ready physical press"
                            )
                        press_index = clock.press_index
                    available, _ = _available_by_action(bridge)
                    observation = _project_observation(
                        self._observation_projector, state, available
                    )
                    try:
                        decision = _select_decision(
                            program,
                            observation,
                            available,
                            imported_sessions,
                            receipts,
                            decision_index,
                        )
                        execution_receipt_start = len(receipts)
                        state = _execute_decision(
                            bridge,
                            state,
                            decision,
                            decision_index=decision_index,
                            projector=self._observation_projector,
                            receipts=receipts,
                            result_bearing_action_refs=(
                                self._result_bearing_action_refs
                            ),
                            external_press_clock=external_press,
                        )
                        if external_press and not bool(state.get("finished")):
                            closed = dict(bridge.finish_press())
                            closed_clock = _press_clock_state_v1(closed)
                            if closed_clock.ready:
                                raise CausalActionProgramError(
                                    "finish_press left the physical opportunity open"
                                )
                            state = closed
                            receipts.append(
                                {
                                    "decision_index": decision_index,
                                    "kind": "EXTERNAL_PRESS_FINISHED",
                                    "press_index": press_index,
                                    "state_time_ms": _state_time(state),
                                }
                            )
                    except Exception as error:
                        for session in imported_sessions.values():
                            session.reject_pending_execution(
                                f"{type(error).__name__}: {error}"
                            )
                        raise
                    for session in imported_sessions.values():
                        session.confirm_pending_execution(
                            decision,
                            receipts[execution_receipt_start:],
                        )
                    state = _advance_to_input(bridge, state)
                    last_state = dict(state)
                    decision_index += 1
                terminal_actions: tuple[AvailableAction, ...] = ()
                if self._terminal_telemetry_action_refs:
                    telemetry, terminal_actions = _terminal_telemetry_receipt_v1(
                        bridge,
                        state,
                        self._terminal_telemetry_action_refs,
                    )
                    receipts.append(telemetry)
                return ScheduleReplayOutcomeV1(
                    seed=seed,
                    status=ReplayStatusV1.COMPLETE,
                    state=state,
                    available_actions=terminal_actions,
                    receipts=tuple(receipts),
                )
        except Exception as error:
            if "time_ms" not in last_state:
                last_state = {"time_ms": 0, "damage_done": 0.0}
            if (
                "damage_done" not in last_state
                and not isinstance(
                    last_state.get("dynamic_team_background"), Mapping
                )
            ):
                last_state["damage_done"] = _state_damage(last_state)
            return ScheduleReplayOutcomeV1(
                seed=seed,
                status=ReplayStatusV1.INVALID,
                state=last_state,
                receipts=tuple(receipts),
                invalid_reason=f"{type(error).__name__}: {error}",
            )


__all__ = (
    "CausalActionProgramError",
    "CausalActionProgramReplayV1",
    "CausalActionProgramV1",
    "CausalObservationProjectorV1",
    "GuardedAlternativeV1",
    "ImportedFallbackOverlaySelectorV1",
    "ImportedReactiveBurstQueueGcdBlockSelectorV1",
    "ImportedReactiveQueueGcdBlockSelectorV1",
    "ImportedReactiveProgramBindingV1",
    "ImportedReactiveSelectorV1",
    "NativeDynamicV3ActionProgramReplayV1",
    "OptionalOffGcdPrefixV1",
    "OrderedGuardSelectorV1",
    "ProgramDecisionV1",
    "ProgramInsertionPointV1",
    "ProgramOriginV1",
    "ProgramPrefixOperationKindV1",
    "SearchedOffGcdInsertionV1",
    "causal_action_program_from_dict_v1",
    "optional_off_gcd_prefix_from_dict_v1",
    "program_decision_from_dict_v1",
    "program_selector_from_dict_v1",
)
