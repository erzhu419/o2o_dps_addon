"""Independent finite-wave action schedule grammar.

This module describes the search space for a complete per-wave or per-boss
action schedule.  Cat, Contra, and historical/offline experts may rank plans,
but they do not define an allowlist: every action advertised as legal by the
current simulator state remains in the enumerated space.

The grammar is deliberately separate from execution.  In particular it does
not assume that every off-GCD action is a next-swing queue and it does not use
the experimental post-GCD queue command.  A runner must execute the ordered
operations and stop after the first accepted action whose ``consumes_decision``
result is true.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections import Counter
from itertools import permutations, product
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .sim_bridge import ActionRef, AvailableAction


JSONScalar = str | int | float | bool
JSONMap = dict[str, Any]


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_action_ref(action: object, label: str) -> ActionRef:
    if not isinstance(action, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (action.spell_id, action.item_id, action.other_id)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in identities):
        raise ValueError(f"{label} has an invalid identity")
    if sum(value > 0 for value in identities) != 1:
        raise ValueError(f"{label} must have exactly one positive identity")
    if isinstance(action.tag, bool) or not isinstance(action.tag, int) or action.tag < 0:
        raise ValueError(f"{label}.tag must be a non-negative integer")
    return action


def _canonical_named_ints(
    values: object,
    label: str,
    *,
    positive_values: bool,
) -> tuple[tuple[str, int], ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    canonical: list[tuple[str, int]] = []
    for index, row in enumerate(values):
        if not isinstance(row, tuple) or len(row) != 2:
            raise TypeError(f"{label}[{index}] must be a (name, value) tuple")
        name = _nonempty_text(row[0], f"{label}[{index}].name")
        value = (
            _positive_int(row[1], f"{label}[{index}].value")
            if positive_values
            else _nonnegative_int(row[1], f"{label}[{index}].value")
        )
        canonical.append((name, value))
    names = [name for name, _ in canonical]
    if len(set(names)) != len(names):
        raise ValueError(f"{label} names must be unique")
    return tuple(sorted(canonical))


def _canonical_mechanics(values: object) -> tuple[tuple[str, JSONScalar], ...]:
    if not isinstance(values, tuple):
        raise TypeError("derived_mechanics must be a tuple")
    canonical: list[tuple[str, JSONScalar]] = []
    for index, row in enumerate(values):
        if not isinstance(row, tuple) or len(row) != 2:
            raise TypeError(
                f"derived_mechanics[{index}] must be a (name, value) tuple"
            )
        name = _nonempty_text(row[0], f"derived_mechanics[{index}].name")
        value = row[1]
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(
                f"derived_mechanics[{index}].value must be a JSON scalar"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"derived_mechanics[{index}].value must be finite"
            )
        canonical.append((name, value))
    names = [name for name, _ in canonical]
    if len(set(names)) != len(names):
        raise ValueError("derived_mechanics names must be unique")
    return tuple(sorted(canonical, key=lambda row: row[0]))


@dataclass(frozen=True)
class SearchCellIdentity:
    """Exact condition under which one finite schedule is optimized."""

    scenario_id: str
    wave_or_boss_id: str
    exact_build_id: str
    talents: tuple[tuple[str, int], ...] = ()
    equipment: tuple[tuple[str, int], ...] = ()
    derived_mechanics: tuple[tuple[str, JSONScalar], ...] = ()
    environment_branch_id: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scenario_id", _nonempty_text(self.scenario_id, "scenario_id")
        )
        object.__setattr__(
            self,
            "wave_or_boss_id",
            _nonempty_text(self.wave_or_boss_id, "wave_or_boss_id"),
        )
        object.__setattr__(
            self,
            "exact_build_id",
            _nonempty_text(self.exact_build_id, "exact_build_id"),
        )
        object.__setattr__(
            self,
            "environment_branch_id",
            _nonempty_text(self.environment_branch_id, "environment_branch_id"),
        )
        object.__setattr__(
            self,
            "talents",
            _canonical_named_ints(self.talents, "talents", positive_values=False),
        )
        object.__setattr__(
            self,
            "equipment",
            _canonical_named_ints(self.equipment, "equipment", positive_values=True),
        )
        object.__setattr__(
            self, "derived_mechanics", _canonical_mechanics(self.derived_mechanics)
        )

    def to_dict(self) -> JSONMap:
        return {
            "scenario_id": self.scenario_id,
            "wave_or_boss_id": self.wave_or_boss_id,
            "exact_build_id": self.exact_build_id,
            "talents": [
                {"talent": talent, "rank": rank} for talent, rank in self.talents
            ],
            "equipment": [
                {"slot": slot, "item_id": item_id}
                for slot, item_id in self.equipment
            ],
            "derived_mechanics": {
                name: value for name, value in self.derived_mechanics
            },
            "environment_branch_id": self.environment_branch_id,
        }

    def cell_key(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )


@dataclass(frozen=True)
class EquipmentAction:
    """Declarative equipment change; execution support is negotiated later."""

    slot: str
    item_id: int
    is_weapon_swap: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "slot", _nonempty_text(self.slot, "slot"))
        _positive_int(self.item_id, "item_id")
        if not isinstance(self.is_weapon_swap, bool):
            raise TypeError("is_weapon_swap must be boolean")

    def to_dict(self) -> JSONMap:
        return {
            "slot": self.slot,
            "item_id": self.item_id,
            "is_weapon_swap": self.is_weapon_swap,
        }


class QueueLaneOp(str, Enum):
    KEEP = "KEEP"
    SET = "SET"
    CANCEL = "CANCEL"


class ScheduledOperationKind(str, Enum):
    SET_TARGET = "SET_TARGET"
    EQUIP = "EQUIP"
    ACT_OFF_GCD = "ACT_OFF_GCD"
    QUEUE_KEEP = "QUEUE_KEEP"
    QUEUE_SET = "QUEUE_SET"
    QUEUE_CANCEL = "QUEUE_CANCEL"
    ACT_GCD = "ACT_GCD"
    WAIT = "WAIT"


@dataclass(frozen=True)
class ScheduledOperation:
    """One typed operation in the cross-lane execution order."""

    kind: ScheduledOperationKind
    action: ActionRef | None = None
    target_index: int | None = None
    equipment_action: EquipmentAction | None = None
    wait_ms: int | None = None

    def to_dict(self) -> JSONMap:
        result: JSONMap = {"kind": self.kind.value}
        if self.action is not None:
            result["action"] = self.action.to_wire()
        if self.target_index is not None:
            result["target_index"] = self.target_index
        if self.equipment_action is not None:
            result["equipment_action"] = self.equipment_action.to_dict()
        if self.wait_ms is not None:
            result["wait_ms"] = self.wait_ms
        return result


@dataclass(frozen=True)
class ScheduledActionPlan:
    """One action opportunity with all independently searchable lanes."""

    at_or_after_ms: int
    target_index: int | None = None
    equipment_action: EquipmentAction | None = None
    off_gcd_actions: tuple[ActionRef, ...] = ()
    queue_op: QueueLaneOp = QueueLaneOp.KEEP
    queue_action: ActionRef | None = None
    gcd_action: ActionRef | None = None
    wait_ms: int | None = None
    prefix_order: tuple[ScheduledOperationKind, ...] | None = None
    guard: ObservableCausalGuardV1 | None = None
    guide_provenance: tuple[str, ...] = ()
    guide_priority: float = 0.0

    def __post_init__(self) -> None:
        _nonnegative_int(self.at_or_after_ms, "at_or_after_ms")
        if self.target_index is not None:
            _nonnegative_int(self.target_index, "target_index")
        if self.equipment_action is not None and not isinstance(
            self.equipment_action, EquipmentAction
        ):
            raise TypeError("equipment_action must be EquipmentAction")
        if not isinstance(self.off_gcd_actions, tuple):
            raise TypeError("off_gcd_actions must be a tuple")
        for index, action in enumerate(self.off_gcd_actions):
            _validate_action_ref(action, f"off_gcd_actions[{index}]")
        if len(set(self.off_gcd_actions)) != len(self.off_gcd_actions):
            raise ValueError("off_gcd_actions cannot repeat an action")

        if not isinstance(self.queue_op, QueueLaneOp):
            raise TypeError("queue_op must be QueueLaneOp")
        if self.queue_op is QueueLaneOp.SET:
            action = _validate_action_ref(self.queue_action, "queue_action")
            if action.spell_id <= 0 or action.tag != 1:
                raise ValueError("queue_action must be a tagged spell action")
        elif self.queue_action is not None:
            raise ValueError("queue_action is only valid when queue_op is SET")

        if self.guard is not None and not isinstance(
            self.guard, ObservableCausalGuardV1
        ):
            raise TypeError("guard must be ObservableCausalGuardV1 or None")

        has_gcd = self.gcd_action is not None
        has_wait = self.wait_ms is not None
        conditional_prefix_only = not has_gcd and not has_wait
        if has_gcd and has_wait:
            raise ValueError("plan must contain exactly one GCD action or WAIT")
        if conditional_prefix_only:
            if (
                self.guard is None
                or self.guard.false_semantics != SKIP_PLAN
                or self.guard.action_ready is None
                or self.off_gcd_actions != (self.guard.action_ready,)
                or self.equipment_action is not None
                or self.queue_op is not QueueLaneOp.KEEP
            ):
                raise ValueError(
                    "a plan without a GCD action or WAIT must be one guarded "
                    "off-GCD action with SKIP_PLAN semantics"
                )
            if self.target_index is not None and (
                self.guard.target_index != self.target_index
                or self.guard.target_attackable_is is not True
            ):
                raise ValueError(
                    "a targeted conditional prefix requires the same guarded "
                    "attackable target"
                )
        if has_gcd:
            _validate_action_ref(self.gcd_action, "gcd_action")
        elif has_wait:
            _positive_int(self.wait_ms, "wait_ms")

        lane_actions = list(self.off_gcd_actions)
        if self.queue_action is not None:
            lane_actions.append(self.queue_action)
        if self.gcd_action is not None:
            lane_actions.append(self.gcd_action)
        if len(set(lane_actions)) != len(lane_actions):
            raise ValueError("one ActionRef cannot occupy multiple lanes")

        required_prefix = self._required_prefix_order()
        if self.prefix_order is None:
            object.__setattr__(self, "prefix_order", required_prefix)
        else:
            if not isinstance(self.prefix_order, tuple) or any(
                not isinstance(kind, ScheduledOperationKind)
                for kind in self.prefix_order
            ):
                raise TypeError(
                    "prefix_order must be a tuple of ScheduledOperationKind"
                )
            if Counter(self.prefix_order) != Counter(required_prefix):
                raise ValueError(
                    "prefix_order must contain every prefix lane exactly once"
                )
        if conditional_prefix_only and self.target_index is not None:
            assert self.prefix_order is not None
            if self.prefix_order.index(ScheduledOperationKind.SET_TARGET) > (
                self.prefix_order.index(ScheduledOperationKind.ACT_OFF_GCD)
            ):
                raise ValueError(
                    "a targeted conditional prefix must set target before acting"
                )

        if not isinstance(self.guide_provenance, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.guide_provenance
        ):
            raise TypeError("guide_provenance must be a tuple of non-empty strings")
        if isinstance(self.guide_priority, bool) or not isinstance(
            self.guide_priority, (int, float)
        ):
            raise TypeError("guide_priority must be numeric")
        if not math.isfinite(float(self.guide_priority)):
            raise ValueError("guide_priority must be finite")

    @property
    def conditional_prefix_only(self) -> bool:
        """Whether the plan is a zero-time guarded off-GCD prefix."""

        return self.gcd_action is None and self.wait_ms is None

    def _required_prefix_order(self) -> tuple[ScheduledOperationKind, ...]:
        required: list[ScheduledOperationKind] = []
        if self.target_index is not None:
            required.append(ScheduledOperationKind.SET_TARGET)
        if self.equipment_action is not None:
            required.append(ScheduledOperationKind.EQUIP)
        required.extend(
            ScheduledOperationKind.ACT_OFF_GCD for _ in self.off_gcd_actions
        )
        required.append(
            {
                QueueLaneOp.KEEP: ScheduledOperationKind.QUEUE_KEEP,
                QueueLaneOp.SET: ScheduledOperationKind.QUEUE_SET,
                QueueLaneOp.CANCEL: ScheduledOperationKind.QUEUE_CANCEL,
            }[self.queue_op]
        )
        return tuple(required)

    def ordered_operations(self) -> tuple[ScheduledOperation, ...]:
        """Return the searched pre-GCD order followed by its terminal action."""

        assert self.prefix_order is not None
        operations: list[ScheduledOperation] = []
        off_gcd_index = 0
        for kind in self.prefix_order:
            if kind is ScheduledOperationKind.SET_TARGET:
                operations.append(
                    ScheduledOperation(kind=kind, target_index=self.target_index)
                )
            elif kind is ScheduledOperationKind.EQUIP:
                operations.append(
                    ScheduledOperation(
                        kind=kind, equipment_action=self.equipment_action
                    )
                )
            elif kind is ScheduledOperationKind.ACT_OFF_GCD:
                operations.append(
                    ScheduledOperation(
                        kind=kind, action=self.off_gcd_actions[off_gcd_index]
                    )
                )
                off_gcd_index += 1
            elif kind is ScheduledOperationKind.QUEUE_SET:
                operations.append(
                    ScheduledOperation(kind=kind, action=self.queue_action)
                )
            else:
                operations.append(ScheduledOperation(kind=kind))
        if self.gcd_action is not None:
            operations.append(
                ScheduledOperation(
                    kind=ScheduledOperationKind.ACT_GCD,
                    action=self.gcd_action,
                )
            )
        elif self.wait_ms is not None:
            operations.append(
                ScheduledOperation(
                    kind=ScheduledOperationKind.WAIT,
                    wait_ms=self.wait_ms,
                )
            )
        return tuple(operations)

    def _identity_dict(self) -> JSONMap:
        result: JSONMap = {
            "at_or_after_ms": self.at_or_after_ms,
            "target_index": self.target_index,
            "equipment_action": (
                self.equipment_action.to_dict()
                if self.equipment_action is not None
                else None
            ),
            "off_gcd_actions": [action.to_wire() for action in self.off_gcd_actions],
            "queue": {
                "op": self.queue_op.value,
                "action": (
                    self.queue_action.to_wire()
                    if self.queue_action is not None
                    else None
                ),
            },
            "gcd_action": (
                self.gcd_action.to_wire() if self.gcd_action is not None else None
            ),
            "wait_ms": self.wait_ms,
            "conditional_prefix_only": self.conditional_prefix_only,
            "prefix_order": [kind.value for kind in self.prefix_order or ()],
        }
        if self.guard is not None:
            result["guard"] = self.guard.to_dict()
        return result

    def plan_key(self) -> str:
        """Stable semantic identity; guide metadata does not change the plan."""

        return json.dumps(
            self._identity_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_dict(self) -> JSONMap:
        result = self._identity_dict()
        result["ordered_operations"] = [
            operation.to_dict() for operation in self.ordered_operations()
        ]
        result["guide"] = {
            "provenance": list(self.guide_provenance),
            "priority": float(self.guide_priority),
        }
        return result


def _target_value(target: object, name: str) -> object:
    if isinstance(target, Mapping):
        if name not in target:
            raise ValueError(f"target state is missing {name}")
        return target[name]
    if not hasattr(target, name):
        raise ValueError(f"target state is missing {name}")
    return getattr(target, name)


def _eligible_target_indexes(
    target_states: Iterable[object],
    allowed_direct_target_indexes: Iterable[int] | None = None,
) -> tuple[int, ...]:
    allowed: frozenset[int] | None = None
    if allowed_direct_target_indexes is not None:
        normalized = tuple(
            _nonnegative_int(value, f"allowed_direct_target_indexes[{position}]")
            for position, value in enumerate(allowed_direct_target_indexes)
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed direct target indexes must be unique")
        allowed = frozenset(normalized)

    indexes: list[int] = []
    observed_indexes: list[int] = []
    for position, target in enumerate(target_states):
        index = _nonnegative_int(
            _target_value(target, "target_index"),
            f"target_states[{position}].target_index",
        )
        observed_indexes.append(index)
        dead = _target_value(target, "dead")
        attackable = _target_value(target, "attackable")
        if not isinstance(dead, bool) or not isinstance(attackable, bool):
            raise TypeError("target dead and attackable fields must be boolean")
        if not dead and attackable and (allowed is None or index in allowed):
            indexes.append(index)
    if len(set(observed_indexes)) != len(observed_indexes):
        raise ValueError("target indexes must be unique")
    if allowed is not None and not allowed <= set(observed_indexes):
        raise ValueError("allowed direct target index is absent from target states")
    return tuple(sorted(indexes))


def _guide_score(
    plan: ScheduledActionPlan,
    guide_priorities: Mapping[ActionRef, float],
) -> float:
    all_ordered = [
        operation.action
        for operation in plan.ordered_operations()
        if operation.action is not None
    ]
    ordered = [
        action for action in all_ordered
        if float(guide_priorities.get(action, 0.0)) > 0
    ]
    # Earlier guided actions receive greater weight, so an expert sequence can
    # rank permutations without removing any alternative from exploration.
    # Unproposed zero-weight actions are deliberately excluded from the rank
    # length: inserting one must not increase an expert guide score.
    guided_score = sum(
        float(guide_priorities.get(action, 0.0)) * (len(ordered) - index)
        for index, action in enumerate(ordered)
    )
    return guided_score - 1e-6 * (len(all_ordered) - len(ordered))


def enumerate_scheduled_action_plans(
    available_actions: Iterable[AvailableAction],
    target_states: Iterable[object],
    *,
    at_or_after_ms: int = 0,
    queue_active: bool = False,
    equipment_actions: Iterable[EquipmentAction] = (),
    max_off_gcd_actions: int = 1,
    fallback_wait_ms: int = 100,
    additional_wait_ms: Iterable[int] = (),
    include_wait: bool = True,
    allow_gcd_without_target: bool = False,
    queue_spell_ids: frozenset[int] | None = None,
    max_prefix_permutations: int | None = None,
    guide_priorities: Mapping[ActionRef, float] | None = None,
    guide_provenance: tuple[str, ...] = (),
    guard_options: Iterable[ObservableCausalGuardV1] = (),
    allowed_direct_target_indexes: Iterable[int] | None = None,
) -> tuple[ScheduledActionPlan, ...]:
    """Enumerate the complete legal one-opportunity schedule space.

    ``guide_priorities`` is applied only after the full cross-product is built.
    It therefore changes ordering but never membership.  ``triggers_gcd`` from
    the current native action snapshot is the only GCD/off-GCD classifier.
    Guards are absent by default; callers must explicitly supply
    ``guard_options`` before guarded variants enter the cross-product.
    When ``allowed_direct_target_indexes`` is supplied, only that stage-local
    subset may be selected and SET_TARGET is the first prefix operation.
    """

    _nonnegative_int(at_or_after_ms, "at_or_after_ms")
    _nonnegative_int(max_off_gcd_actions, "max_off_gcd_actions")
    _positive_int(fallback_wait_ms, "fallback_wait_ms")
    wait_values = {fallback_wait_ms}
    for index, value in enumerate(additional_wait_ms):
        wait_values.add(_positive_int(value, f"additional_wait_ms[{index}]"))
    if not isinstance(queue_active, bool):
        raise TypeError("queue_active must be boolean")
    if not isinstance(include_wait, bool):
        raise TypeError("include_wait must be boolean")
    if not isinstance(allow_gcd_without_target, bool):
        raise TypeError("allow_gcd_without_target must be boolean")
    if queue_spell_ids is not None and (
        not isinstance(queue_spell_ids, frozenset)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in queue_spell_ids
        )
    ):
        raise TypeError(
            "queue_spell_ids must be None or a frozenset of positive integers"
        )
    if max_prefix_permutations is not None:
        _positive_int(max_prefix_permutations, "max_prefix_permutations")

    guards: list[ObservableCausalGuardV1 | None] = [None]
    for index, guard in enumerate(guard_options):
        if not isinstance(guard, ObservableCausalGuardV1):
            raise TypeError(
                f"guard_options[{index}] must be ObservableCausalGuardV1"
            )
        if guard not in guards:
            guards.append(guard)

    equipment_options: list[EquipmentAction | None] = [None]
    for index, action in enumerate(equipment_actions):
        if not isinstance(action, EquipmentAction):
            raise TypeError(f"equipment_actions[{index}] must be EquipmentAction")
        if action not in equipment_options:
            equipment_options.append(action)

    unique: dict[ActionRef, AvailableAction] = {}
    for index, available in enumerate(available_actions):
        if not isinstance(available, AvailableAction):
            raise TypeError(f"available_actions[{index}] must be AvailableAction")
        _validate_action_ref(available.action, f"available_actions[{index}].action")
        if not isinstance(available.legal, bool):
            raise TypeError(f"available_actions[{index}].legal must be boolean")
        if not isinstance(available.triggers_gcd, bool):
            raise TypeError(
                f"available_actions[{index}].triggers_gcd must be boolean"
            )
        _nonnegative_int(
            available.ready_in_ms, f"available_actions[{index}].ready_in_ms"
        )
        if not available.legal:
            continue
        prior = unique.get(available.action)
        if prior is not None and prior.triggers_gcd != available.triggers_gcd:
            raise ValueError("one legal ActionRef has conflicting GCD classification")
        unique.setdefault(available.action, available)

    queue_actions: list[ActionRef] = []
    gcd_actions: list[ActionRef] = []
    off_gcd_actions: list[ActionRef] = []
    for action, available in unique.items():
        if available.triggers_gcd:
            gcd_actions.append(action)
        elif action.tag == 1 and (
            queue_spell_ids is None or action.spell_id in queue_spell_ids
        ):
            queue_actions.append(action)
        else:
            off_gcd_actions.append(action)
    queue_actions.sort()
    gcd_actions.sort()
    off_gcd_actions.sort()

    off_gcd_sequences: list[tuple[ActionRef, ...]] = [()]
    for length in range(1, min(max_off_gcd_actions, len(off_gcd_actions)) + 1):
        off_gcd_sequences.extend(permutations(off_gcd_actions, length))

    queue_options: list[tuple[QueueLaneOp, ActionRef | None]] = [
        (QueueLaneOp.KEEP, None)
    ]
    queue_options.extend((QueueLaneOp.SET, action) for action in queue_actions)
    if queue_active:
        queue_options.append((QueueLaneOp.CANCEL, None))

    targets = tuple(target_states)
    eligible_targets = _eligible_target_indexes(
        targets,
        allowed_direct_target_indexes=allowed_direct_target_indexes,
    )
    known_no_target = (
        bool(targets) or allowed_direct_target_indexes is not None
    ) and not eligible_targets
    target_options: tuple[int | None, ...] = (
        eligible_targets if eligible_targets else (None,)
    )

    endpoint_options: list[tuple[ActionRef | None, int | None]] = []
    if not known_no_target or allow_gcd_without_target:
        endpoint_options.extend((action, None) for action in gcd_actions)
    if include_wait or not endpoint_options:
        endpoint_options.extend((None, value) for value in sorted(wait_values))

    if known_no_target:
        # No-target windows may still use self-buffs, equipment, cancellation,
        # and WAIT, but cannot set a fresh next-swing attack.
        queue_options = [
            option for option in queue_options if option[0] is not QueueLaneOp.SET
        ]

    priorities = guide_priorities or {}
    if not isinstance(priorities, Mapping):
        raise TypeError("guide_priorities must be a mapping")
    for action, priority in priorities.items():
        _validate_action_ref(action, "guide_priorities key")
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            raise TypeError("guide priority values must be numeric")
        if not math.isfinite(float(priority)):
            raise ValueError("guide priority values must be finite")

    plans: list[ScheduledActionPlan] = []

    def append_ordered_variants(draft: ScheduledActionPlan) -> None:
        assert draft.prefix_order is not None
        unique_orders = set(permutations(draft.prefix_order))
        remaining_orders = sorted(
            unique_orders - {draft.prefix_order},
            key=lambda order: tuple(kind.value for kind in order),
        )
        prefix_orders = [draft.prefix_order, *remaining_orders]
        if max_prefix_permutations is not None:
            prefix_orders = prefix_orders[:max_prefix_permutations]
        for prefix_order in prefix_orders:
            if (
                allowed_direct_target_indexes is not None
                and draft.target_index is not None
                and prefix_order[0] is not ScheduledOperationKind.SET_TARGET
            ):
                continue
            if (
                draft.conditional_prefix_only
                and draft.target_index is not None
                and prefix_order.index(ScheduledOperationKind.SET_TARGET)
                > prefix_order.index(ScheduledOperationKind.ACT_OFF_GCD)
            ):
                continue
            ordered_draft = ScheduledActionPlan(
                at_or_after_ms=draft.at_or_after_ms,
                target_index=draft.target_index,
                equipment_action=draft.equipment_action,
                off_gcd_actions=draft.off_gcd_actions,
                queue_op=draft.queue_op,
                queue_action=draft.queue_action,
                gcd_action=draft.gcd_action,
                wait_ms=draft.wait_ms,
                prefix_order=prefix_order,
                guard=draft.guard,
                guide_provenance=draft.guide_provenance,
            )
            plans.append(
                ScheduledActionPlan(
                    at_or_after_ms=ordered_draft.at_or_after_ms,
                    target_index=ordered_draft.target_index,
                    equipment_action=ordered_draft.equipment_action,
                    off_gcd_actions=ordered_draft.off_gcd_actions,
                    queue_op=ordered_draft.queue_op,
                    queue_action=ordered_draft.queue_action,
                    gcd_action=ordered_draft.gcd_action,
                    wait_ms=ordered_draft.wait_ms,
                    prefix_order=ordered_draft.prefix_order,
                    guard=ordered_draft.guard,
                    guide_provenance=ordered_draft.guide_provenance,
                    guide_priority=_guide_score(ordered_draft, priorities),
                )
            )

    for (
        target_index,
        equipment_action,
        off_gcd_sequence,
        (queue_op, queue_action),
        (gcd_action, wait_ms),
        guard,
    ) in product(
        target_options,
        equipment_options,
        off_gcd_sequences,
        queue_options,
        endpoint_options,
        guards,
    ):
        draft = ScheduledActionPlan(
            at_or_after_ms=at_or_after_ms,
            target_index=target_index,
            equipment_action=equipment_action,
            off_gcd_actions=off_gcd_sequence,
            queue_op=queue_op,
            queue_action=queue_action,
            gcd_action=gcd_action,
            wait_ms=wait_ms,
            guard=guard,
            guide_provenance=guide_provenance,
        )
        append_ordered_variants(draft)

    # A guarded off-GCD resource must be representable without an artificial
    # WAIT.  This lets the next ordinary plan use the same decision epoch on
    # both branches: one seed casts the saved burst, another skips it because
    # the resource is already on cooldown.
    for guard in guards:
        if (
            guard is None
            or guard.false_semantics != SKIP_PLAN
            or guard.action_ready not in off_gcd_actions
        ):
            continue
        for target_index in target_options:
            if target_index is not None and (
                guard.target_index != target_index
                or guard.target_attackable_is is not True
            ):
                continue
            append_ordered_variants(
                ScheduledActionPlan(
                    at_or_after_ms=at_or_after_ms,
                    target_index=target_index,
                    off_gcd_actions=(guard.action_ready,),
                    guard=guard,
                    guide_provenance=guide_provenance,
                )
            )

    plans.sort(key=lambda plan: (-float(plan.guide_priority), plan.plan_key()))
    return tuple(plans)


__all__ = [
    "EquipmentAction",
    "QueueLaneOp",
    "ObservableCausalGuardV1",
    "ScheduledActionPlan",
    "ScheduledOperation",
    "ScheduledOperationKind",
    "SearchCellIdentity",
    "enumerate_scheduled_action_plans",
]
