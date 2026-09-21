"""Causal, current-observation guards for independent finite schedules.

The contract is deliberately closed: every predicate below is computed from
the current simulator state or the current ``AvailableAction`` snapshot.  It
has no callback or free-form state path through which a schedule could inspect
future team events, a recorded death time, or a terminal rollout suffix.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence

from .sim_bridge import ActionRef, AvailableAction, SimBridgeProtocolError


JSONMap = dict[str, Any]
_QUEUE_STATUSES = frozenset({"NONE", "PENDING", "ACTIVE"})
WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT = "WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT"
SKIP_PLAN = "SKIP_PLAN"
_FALSE_SEMANTICS = frozenset(
    {WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT, SKIP_PLAN}
)


class GuardObservationError(ValueError):
    """The current bridge snapshot cannot expose a requested guard value."""


class GuardTimeoutError(RuntimeError):
    """A finite guard remained false through its observable deadline."""


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _number(value: object, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{label} must be finite and in [{minimum}, {maximum}]")
    return result


def _action(value: object, label: str) -> ActionRef:
    if not isinstance(value, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (value.spell_id, value.item_id, value.other_id)
    if any(
        isinstance(field, bool) or not isinstance(field, int) or field < 0
        for field in (*identities, value.tag)
    ):
        raise ValueError(f"{label} has an invalid identity")
    if sum(field > 0 for field in identities) != 1:
        raise ValueError(f"{label} must have exactly one positive identity")
    return value


@dataclass(frozen=True)
class ObservableCausalGuardV1:
    """Conjunction of explicitly selected, policy-visible predicates."""

    pull_relative_time_gte_ms: int | None = None
    pull_relative_time_lte_ms: int | None = None
    rage_gte: float | None = None
    rage_lte: float | None = None
    target_index: int | None = None
    target_hp_pct_gte: float | None = None
    target_hp_pct_lte: float | None = None
    target_attackable_is: bool | None = None
    target_aura_action: ActionRef | None = None
    target_aura_stacks_lte: int | None = None
    estimated_remaining_attackable_gte_ms: int | None = None
    live_target_count_gte: int | None = None
    live_target_count_lte: int | None = None
    attackable_target_count_gte: int | None = None
    attackable_target_count_lte: int | None = None
    mh_swing_remaining_lte_ms: int | None = None
    queue_status_is: str | None = None
    aura_action: ActionRef | None = None
    aura_remaining_gte_ms: int | None = None
    aura_remaining_lte_ms: int | None = None
    action_ready: ActionRef | None = None
    timeout_ms: int = 10_000
    check_interval_ms: int = 100
    false_semantics: str = WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT

    def __post_init__(self) -> None:
        if self.pull_relative_time_gte_ms is not None:
            _integer(
                self.pull_relative_time_gte_ms,
                "pull_relative_time_gte_ms",
            )
        if self.pull_relative_time_lte_ms is not None:
            _integer(
                self.pull_relative_time_lte_ms,
                "pull_relative_time_lte_ms",
            )
        if (
            self.pull_relative_time_gte_ms is not None
            and self.pull_relative_time_lte_ms is not None
            and self.pull_relative_time_gte_ms
            > self.pull_relative_time_lte_ms
        ):
            raise ValueError(
                "pull_relative_time_gte_ms cannot exceed "
                "pull_relative_time_lte_ms"
            )
        if self.rage_gte is not None:
            object.__setattr__(
                self,
                "rage_gte",
                _number(self.rage_gte, "rage_gte", minimum=0, maximum=100),
            )
        if self.rage_lte is not None:
            object.__setattr__(
                self,
                "rage_lte",
                _number(self.rage_lte, "rage_lte", minimum=0, maximum=100),
            )
        if (
            self.rage_gte is not None
            and self.rage_lte is not None
            and self.rage_gte > self.rage_lte
        ):
            raise ValueError("rage_gte cannot exceed rage_lte")
        has_target_predicate = any(
            value is not None
            for value in (
                self.target_hp_pct_gte,
                self.target_hp_pct_lte,
                self.target_attackable_is,
                self.target_aura_action,
            )
        )
        if (self.target_index is not None) != has_target_predicate:
            raise ValueError(
                "target_index and at least one target predicate must be "
                "supplied together"
            )
        if self.target_index is not None:
            _integer(self.target_index, "target_index", minimum=0)
            for name in ("target_hp_pct_gte", "target_hp_pct_lte"):
                value = getattr(self, name)
                if value is not None:
                    object.__setattr__(
                        self,
                        name,
                        _number(value, name, minimum=0, maximum=100),
                    )
            if (
                self.target_hp_pct_gte is not None
                and self.target_hp_pct_lte is not None
                and self.target_hp_pct_gte > self.target_hp_pct_lte
            ):
                raise ValueError(
                    "target_hp_pct_gte cannot exceed target_hp_pct_lte"
                )
            if self.target_attackable_is is not None and not isinstance(
                self.target_attackable_is, bool
            ):
                raise TypeError("target_attackable_is must be boolean")
        target_aura_fields = (
            self.target_aura_action is not None,
            self.target_aura_stacks_lte is not None,
        )
        if target_aura_fields[0] != target_aura_fields[1]:
            raise ValueError(
                "target_aura_action and target_aura_stacks_lte must be "
                "supplied together"
            )
        if self.target_aura_action is not None:
            _action(self.target_aura_action, "target_aura_action")
            _integer(
                self.target_aura_stacks_lte,
                "target_aura_stacks_lte",
                minimum=0,
            )
        if self.estimated_remaining_attackable_gte_ms is not None:
            _integer(
                self.estimated_remaining_attackable_gte_ms,
                "estimated_remaining_attackable_gte_ms",
                minimum=1,
            )
        for name in ("live_target_count_gte", "live_target_count_lte"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name, minimum=0)
        if (
            self.live_target_count_gte is not None
            and self.live_target_count_lte is not None
            and self.live_target_count_gte > self.live_target_count_lte
        ):
            raise ValueError(
                "live_target_count_gte cannot exceed live_target_count_lte"
            )
        for name in (
            "attackable_target_count_gte",
            "attackable_target_count_lte",
        ):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name, minimum=0)
        if (
            self.attackable_target_count_gte is not None
            and self.attackable_target_count_lte is not None
            and self.attackable_target_count_gte
            > self.attackable_target_count_lte
        ):
            raise ValueError(
                "attackable_target_count_gte cannot exceed "
                "attackable_target_count_lte"
            )
        if self.mh_swing_remaining_lte_ms is not None:
            _integer(
                self.mh_swing_remaining_lte_ms,
                "mh_swing_remaining_lte_ms",
                minimum=0,
            )
        if self.queue_status_is is not None:
            if self.queue_status_is not in _QUEUE_STATUSES:
                raise ValueError(
                    "queue_status_is must be NONE, PENDING, or ACTIVE"
                )
        aura_fields = (
            self.aura_action is not None,
            self.aura_remaining_gte_ms is not None,
            self.aura_remaining_lte_ms is not None,
        )
        if aura_fields[0] != (aura_fields[1] or aura_fields[2]):
            raise ValueError(
                "aura_action requires at least one aura remaining threshold, "
                "and thresholds require aura_action"
            )
        if self.aura_action is not None:
            _action(self.aura_action, "aura_action")
        for name in ("aura_remaining_gte_ms", "aura_remaining_lte_ms"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, name, minimum=0)
        if (
            self.aura_remaining_gte_ms is not None
            and self.aura_remaining_lte_ms is not None
            and self.aura_remaining_gte_ms > self.aura_remaining_lte_ms
        ):
            raise ValueError(
                "aura_remaining_gte_ms cannot exceed aura_remaining_lte_ms"
            )
        if self.action_ready is not None:
            _action(self.action_ready, "action_ready")
        _integer(self.timeout_ms, "timeout_ms", minimum=1)
        _integer(self.check_interval_ms, "check_interval_ms", minimum=1)
        if self.false_semantics not in _FALSE_SEMANTICS:
            raise ValueError(
                "false_semantics must be WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT "
                "or SKIP_PLAN"
            )

        predicates = (
            self.pull_relative_time_gte_ms,
            self.pull_relative_time_lte_ms,
            self.rage_gte,
            self.rage_lte,
            self.target_hp_pct_gte,
            self.target_hp_pct_lte,
            self.target_attackable_is,
            self.target_aura_action,
            self.estimated_remaining_attackable_gte_ms,
            self.live_target_count_gte,
            self.live_target_count_lte,
            self.attackable_target_count_gte,
            self.attackable_target_count_lte,
            self.mh_swing_remaining_lte_ms,
            self.queue_status_is,
            self.aura_action,
            self.action_ready,
        )
        if all(value is None for value in predicates):
            raise ValueError("guard must contain at least one predicate")

    def to_dict(self) -> JSONMap:
        predicates: JSONMap = {}
        for name in (
            "pull_relative_time_gte_ms",
            "pull_relative_time_lte_ms",
            "rage_gte",
            "rage_lte",
            "estimated_remaining_attackable_gte_ms",
            "live_target_count_gte",
            "live_target_count_lte",
            "attackable_target_count_gte",
            "attackable_target_count_lte",
            "mh_swing_remaining_lte_ms",
            "queue_status_is",
        ):
            value = getattr(self, name)
            if value is not None:
                predicates[name] = value
        if self.target_hp_pct_gte is not None:
            predicates["target_hp_pct_gte"] = {
                "target_index": self.target_index,
                "percent": self.target_hp_pct_gte,
            }
        if self.target_hp_pct_lte is not None:
            predicates["target_hp_pct_lte"] = {
                "target_index": self.target_index,
                "percent": self.target_hp_pct_lte,
            }
        if self.target_attackable_is is not None:
            predicates["target_attackable_is"] = {
                "target_index": self.target_index,
                "value": self.target_attackable_is,
            }
        if self.target_aura_action is not None:
            predicates["target_aura_stacks_lte"] = {
                "target_index": self.target_index,
                "action": self.target_aura_action.to_wire(),
                "stacks": self.target_aura_stacks_lte,
            }
        if self.aura_action is not None:
            predicates["aura_remaining_ms"] = {
                "action": self.aura_action.to_wire(),
                "gte": self.aura_remaining_gte_ms,
                "lte": self.aura_remaining_lte_ms,
            }
        if self.action_ready is not None:
            predicates["action_ready"] = self.action_ready.to_wire()
        return {
            "schema": "observable_causal_guard/v1",
            "all_of": predicates,
            "timeout_ms": self.timeout_ms,
            "check_interval_ms": self.check_interval_ms,
            "false_semantics": self.false_semantics,
            "terminal_semantics": "STOP_WITHOUT_EXECUTING_PLAN",
            "timeout_semantics": "INVALID_REPLAY",
        }


def observable_causal_guard_from_dict_v1(
    value: object,
    *,
    label: str = "guard",
) -> ObservableCausalGuardV1:
    """Parse the closed wire shape emitted by :meth:`to_dict`.

    Remote manifests can therefore carry guards without gaining a free-form
    expression language or access to anything beyond the existing observable
    predicate contract.
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    allowed_top = {
        "schema",
        "all_of",
        "timeout_ms",
        "check_interval_ms",
        "false_semantics",
        "terminal_semantics",
        "timeout_semantics",
    }
    unknown_top = sorted(set(value) - allowed_top)
    if unknown_top:
        raise ValueError(f"{label} has unsupported fields: {unknown_top}")
    if value.get("schema") != "observable_causal_guard/v1":
        raise ValueError(
            f"{label}.schema must be 'observable_causal_guard/v1'"
        )
    semantic_contract = {
        "terminal_semantics": "STOP_WITHOUT_EXECUTING_PLAN",
        "timeout_semantics": "INVALID_REPLAY",
    }
    for field, expected in semantic_contract.items():
        if field in value and value[field] != expected:
            raise ValueError(f"{label}.{field} differs from the v1 contract")
    false_semantics = value.get(
        "false_semantics", WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT
    )
    if false_semantics not in _FALSE_SEMANTICS:
        raise ValueError(f"{label}.false_semantics is unsupported")

    predicates = value.get("all_of")
    if not isinstance(predicates, Mapping):
        raise ValueError(f"{label}.all_of must be an object")
    allowed_predicates = {
        "pull_relative_time_gte_ms",
        "pull_relative_time_lte_ms",
        "rage_gte",
        "rage_lte",
        "target_hp_pct_lte",
        "target_hp_pct_gte",
        "target_attackable_is",
        "target_aura_stacks_lte",
        "estimated_remaining_attackable_gte_ms",
        "live_target_count_gte",
        "live_target_count_lte",
        "attackable_target_count_gte",
        "attackable_target_count_lte",
        "mh_swing_remaining_lte_ms",
        "queue_status_is",
        "aura_remaining_ms",
        "action_ready",
    }
    unknown_predicates = sorted(set(predicates) - allowed_predicates)
    if unknown_predicates:
        raise ValueError(
            f"{label}.all_of has unsupported predicates: "
            f"{unknown_predicates}"
        )

    kwargs: JSONMap = {
        key: predicates[key]
        for key in (
            "pull_relative_time_gte_ms",
            "pull_relative_time_lte_ms",
            "rage_gte",
            "rage_lte",
            "estimated_remaining_attackable_gte_ms",
            "live_target_count_gte",
            "live_target_count_lte",
            "attackable_target_count_gte",
            "attackable_target_count_lte",
            "mh_swing_remaining_lte_ms",
            "queue_status_is",
        )
        if key in predicates
    }
    target_index = None
    for name in ("target_hp_pct_gte", "target_hp_pct_lte"):
        target = predicates.get(name)
        if target is None:
            continue
        if not isinstance(target, Mapping) or set(target) != {
            "target_index",
            "percent",
        }:
            raise ValueError(
                f"{label}.all_of.{name} must contain exactly "
                "target_index and percent"
            )
        if target_index is not None and target["target_index"] != target_index:
            raise ValueError(f"{label} target predicates disagree on target_index")
        target_index = target["target_index"]
        kwargs[name] = target["percent"]
    attackable = predicates.get("target_attackable_is")
    if attackable is not None:
        if not isinstance(attackable, Mapping) or set(attackable) != {
            "target_index",
            "value",
        }:
            raise ValueError(
                f"{label}.all_of.target_attackable_is must contain exactly "
                "target_index and value"
            )
        if target_index is not None and attackable["target_index"] != target_index:
            raise ValueError(f"{label} target predicates disagree on target_index")
        target_index = attackable["target_index"]
        kwargs["target_attackable_is"] = attackable["value"]
    target_aura = predicates.get("target_aura_stacks_lte")
    if target_aura is not None:
        if not isinstance(target_aura, Mapping) or set(target_aura) != {
            "target_index",
            "action",
            "stacks",
        }:
            raise ValueError(
                f"{label}.all_of.target_aura_stacks_lte must contain "
                "exactly target_index, action, and stacks"
            )
        if (
            target_index is not None
            and target_aura["target_index"] != target_index
        ):
            raise ValueError(f"{label} target predicates disagree on target_index")
        target_index = target_aura["target_index"]
        try:
            kwargs["target_aura_action"] = ActionRef.from_wire(
                target_aura["action"]
            )
        except SimBridgeProtocolError as error:
            raise ValueError(
                f"invalid {label}.all_of.target_aura_stacks_lte.action: "
                f"{error}"
            ) from error
        kwargs["target_aura_stacks_lte"] = target_aura["stacks"]
    if target_index is not None:
        kwargs["target_index"] = target_index
    aura = predicates.get("aura_remaining_ms")
    if aura is not None:
        if not isinstance(aura, Mapping) or not set(aura) <= {
            "action",
            "gte",
            "lte",
        } or "action" not in aura:
            raise ValueError(
                f"{label}.all_of.aura_remaining_ms has invalid fields"
            )
        try:
            kwargs["aura_action"] = ActionRef.from_wire(aura["action"])
        except SimBridgeProtocolError as error:
            raise ValueError(
                f"invalid {label}.all_of.aura_remaining_ms.action: {error}"
            ) from error
        kwargs["aura_remaining_gte_ms"] = aura.get("gte")
        kwargs["aura_remaining_lte_ms"] = aura.get("lte")
    ready = predicates.get("action_ready")
    if ready is not None:
        try:
            kwargs["action_ready"] = ActionRef.from_wire(ready)
        except SimBridgeProtocolError as error:
            raise ValueError(
                f"invalid {label}.all_of.action_ready: {error}"
            ) from error
    kwargs["timeout_ms"] = value.get("timeout_ms", 10_000)
    kwargs["check_interval_ms"] = value.get("check_interval_ms", 100)
    kwargs["false_semantics"] = false_semantics
    try:
        return ObservableCausalGuardV1(**kwargs)
    except (TypeError, ValueError, SimBridgeProtocolError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


@dataclass(frozen=True)
class GuardEvaluationV1:
    satisfied: bool
    failed_predicates: tuple[str, ...]
    observed: Mapping[str, Any]

    def to_dict(self) -> JSONMap:
        return {
            "satisfied": self.satisfied,
            "failed_predicates": list(self.failed_predicates),
            "observed": dict(self.observed),
        }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GuardObservationError(f"current state lacks observable {label}")
    return value


def _finite_current_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardObservationError(f"current {label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GuardObservationError(f"current {label} must be finite")
    return result


def _target_rows(state: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    for block_name in ("dynamic_target_semantics", "dynamic_team_background"):
        block = state.get(block_name)
        rows = block.get("targets") if isinstance(block, Mapping) else None
        if isinstance(rows, list):
            return tuple(row for row in rows if isinstance(row, Mapping))
    raise GuardObservationError("current state lacks observable target rows")


def _target_health_percent(
    state: Mapping[str, Any], target_index: int
) -> float:
    sources: list[tuple[Mapping[str, Any], str]] = []
    for block_name, maximum_name in (
        ("dynamic_target_semantics", "maximum_health"),
        ("dynamic_team_background", "initial_health"),
    ):
        block = state.get(block_name)
        rows = block.get("targets") if isinstance(block, Mapping) else None
        if isinstance(rows, list):
            sources.extend(
                (row, maximum_name) for row in rows if isinstance(row, Mapping)
            )
    for row, maximum_name in sources:
        if row.get("target_index") != target_index:
            continue
        # Native dynamic_target_semantics exposes the current-health lifecycle
        # but can omit its maximum; dynamic_team_background then carries the
        # complete current/initial-health pair.  An incomplete source is not a
        # malformed observation and must not mask the later complete source.
        if "current_health" not in row or maximum_name not in row:
            continue
        current = _finite_current_number(
            row.get("current_health"), "target current_health"
        )
        maximum = _finite_current_number(
            row.get(maximum_name), f"target {maximum_name}"
        )
        if maximum > 0 and 0 <= current <= maximum:
            return current / maximum * 100.0

    if state.get("target_index") == target_index and state.get(
        "target_health_known"
    ) is True:
        percent = _finite_current_number(
            state.get("target_health_percent"), "target_health_percent"
        )
        if 0 <= percent <= 100:
            return percent
    raise GuardObservationError(
        f"current state lacks observable HP percent for target {target_index}"
    )


def _target_attackable(state: Mapping[str, Any], target_index: int) -> bool:
    for row in _target_rows(state):
        if row.get("target_index") != target_index:
            continue
        value = row.get("attackable")
        if not isinstance(value, bool):
            raise GuardObservationError(
                "current target attackable flag must be boolean"
            )
        return value
    raise GuardObservationError(
        f"current state lacks observable attackability for target {target_index}"
    )


def _estimated_remaining_attackable_ms(
    state: Mapping[str, Any],
) -> tuple[float | None, float, float | None]:
    """Estimate the current pack's remaining lifetime from its causal prefix.

    The numerator is the current HP of targets that are attackable now.  The
    denominator is the combined player-plus-team damage rate already observed
    since the earliest visible target-health baseline.  A zero-length or
    zero-damage prefix yields no estimate instead of consulting configured
    team DPS, future events, or a terminal death time.
    """

    team = _mapping(state.get("dynamic_team_background"), "dynamic team state")
    rate = _mapping(
        team.get("prefix_damage_rate"), "prefix damage-rate estimate"
    )
    if rate.get("schema") != "o2o_policy_prefix_damage_rate/v1":
        raise GuardObservationError(
            "current prefix damage-rate estimate has an unsupported schema"
        )
    raw_dps = rate.get("combined_damage_per_second")
    if raw_dps is None:
        combined_dps = None
    else:
        combined_dps = _finite_current_number(
            raw_dps, "prefix combined_damage_per_second"
        )
        if combined_dps <= 0:
            raise GuardObservationError(
                "current prefix combined_damage_per_second must be positive"
            )

    semantics = _mapping(
        state.get("dynamic_target_semantics"), "dynamic target semantics"
    )
    semantic_rows = semantics.get("targets")
    life_rows = team.get("targets")
    if not isinstance(semantic_rows, list) or not isinstance(life_rows, list):
        raise GuardObservationError(
            "current state lacks aligned observable target rows"
        )
    attackable_by_index: dict[int, bool] = {}
    for row in semantic_rows:
        if not isinstance(row, Mapping):
            raise GuardObservationError("current target semantics row is invalid")
        index = row.get("target_index")
        attackable = row.get("attackable")
        dead = row.get("dead")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not isinstance(attackable, bool)
            or not isinstance(dead, bool)
        ):
            raise GuardObservationError(
                "current target semantics identity/lifecycle is invalid"
            )
        attackable_by_index[index] = attackable and not dead

    current_attackable_health = 0.0
    for row in life_rows:
        if not isinstance(row, Mapping):
            raise GuardObservationError("current target lifecycle row is invalid")
        index = row.get("target_index")
        if index not in attackable_by_index:
            raise GuardObservationError(
                "current target lifecycle and semantics rows are not aligned"
            )
        if not attackable_by_index[index]:
            continue
        current = _finite_current_number(
            row.get("current_health"), "attackable target current_health"
        )
        if current < 0:
            raise GuardObservationError(
                "current attackable target health must be nonnegative"
            )
        current_attackable_health += current

    estimate = (
        current_attackable_health * 1000.0 / combined_dps
        if combined_dps is not None and current_attackable_health > 0
        else None
    )
    return estimate, current_attackable_health, combined_dps


def _selected_target_aura_stacks(
    state: Mapping[str, Any], target_index: int, action: ActionRef
) -> tuple[int | None, int]:
    """Return the selected target and its current stack count for one aura.

    ``target_auras`` belongs only to the bridge's currently selected target.
    A different selected target is therefore an observable non-match, not
    evidence that the requested target has zero stacks.  Once the requested
    target is selected, an absent aura is the real zero-stack state.
    """

    selected = _integer(
        state.get("target_index"), "current target_index", minimum=0
    )
    if selected != target_index:
        return None, selected
    rows = state.get("target_auras")
    if not isinstance(rows, list):
        raise GuardObservationError("current state lacks observable target_auras")
    matched: int | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            raise GuardObservationError("current target aura row must be an object")
        raw_action = row.get("action")
        if not isinstance(raw_action, Mapping):
            raise GuardObservationError(
                "current target aura action identity is invalid"
            )
        try:
            observed_action = ActionRef.from_wire(raw_action)
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise GuardObservationError(
                "current target aura action identity is invalid"
            ) from error
        if observed_action != action:
            continue
        stacks = row.get("stacks")
        try:
            parsed = _integer(stacks, "current target aura stacks", minimum=0)
        except (TypeError, ValueError) as error:
            raise GuardObservationError(str(error)) from error
        if matched is not None:
            raise GuardObservationError(
                "current target_auras repeats one action identity"
            )
        matched = parsed
    return (0 if matched is None else matched), selected


def _live_target_count(state: Mapping[str, Any]) -> int:
    rows = _target_rows(state)
    count = 0
    for row in rows:
        dead = row.get("dead")
        if not isinstance(dead, bool):
            raise GuardObservationError("current target dead flag must be boolean")
        if not dead:
            count += 1
    return count


def _attackable_target_count(state: Mapping[str, Any]) -> int:
    semantics = _mapping(
        state.get("dynamic_target_semantics"), "dynamic target semantics"
    )
    rows = semantics.get("targets")
    if not isinstance(rows, list):
        raise GuardObservationError(
            "current state lacks observable target semantics rows"
        )
    count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise GuardObservationError(
                "current target semantics row must be an object"
            )
        dead = row.get("dead")
        attackable = row.get("attackable")
        if not isinstance(dead, bool) or not isinstance(attackable, bool):
            raise GuardObservationError(
                "current target dead/attackable flags must be boolean"
            )
        if attackable and not dead:
            count += 1
    return count


def _aura_remaining_ms(
    state: Mapping[str, Any], action: ActionRef
) -> int | None:
    rows = state.get("auras")
    if not isinstance(rows, list):
        raise GuardObservationError("current state lacks observable auras")
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("action"), Mapping):
            continue
        try:
            observed_action = ActionRef.from_wire(row["action"])
        except (TypeError, ValueError, SimBridgeProtocolError) as error:
            raise GuardObservationError(
                "current aura action identity is invalid"
            ) from error
        if observed_action != action:
            continue
        remaining = row.get("remaining_ms")
        if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < -1:
            raise GuardObservationError("current aura remaining_ms is invalid")
        return remaining
    return None


def _action_ready_in_ms(
    available_actions: Sequence[AvailableAction], action: ActionRef
) -> int | None:
    for row in available_actions:
        if not isinstance(row, AvailableAction):
            raise TypeError("available_actions must contain AvailableAction values")
        if row.action == action:
            if row.ready_in_ms < 0:
                raise GuardObservationError("current action ready_in_ms is negative")
            return row.ready_in_ms
    return None


def _action_legal(
    available_actions: Sequence[AvailableAction], action: ActionRef
) -> bool | None:
    for row in available_actions:
        if not isinstance(row, AvailableAction):
            raise TypeError("available_actions must contain AvailableAction values")
        if row.action == action:
            if not isinstance(row.legal, bool):
                raise GuardObservationError("current action legal flag is invalid")
            return row.legal
    return None


def evaluate_observable_guard_v1(
    guard: ObservableCausalGuardV1,
    state: Mapping[str, Any],
    available_actions: Sequence[AvailableAction],
) -> GuardEvaluationV1:
    """Evaluate one guard from a closed set of current-observation fields."""

    if not isinstance(guard, ObservableCausalGuardV1):
        raise TypeError("guard must be ObservableCausalGuardV1")
    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    failed: list[str] = []
    observed: JSONMap = {}

    if (
        guard.pull_relative_time_gte_ms is not None
        or guard.pull_relative_time_lte_ms is not None
    ):
        precombat = _mapping(state.get("precombat"), "precombat clock")
        relative = _integer(
            precombat.get("relative_time_ms"), "current pull-relative time"
        )
        observed["pull_relative_time_ms"] = relative
        if (
            guard.pull_relative_time_gte_ms is not None
            and relative < guard.pull_relative_time_gte_ms
        ):
            failed.append("pull_relative_time_gte_ms")
        if (
            guard.pull_relative_time_lte_ms is not None
            and relative > guard.pull_relative_time_lte_ms
        ):
            failed.append("pull_relative_time_lte_ms")

    if guard.rage_gte is not None or guard.rage_lte is not None:
        power = _mapping(state.get("power"), "power")
        if power.get("type") != "rage":
            raise GuardObservationError("current power type is not rage")
        rage = _finite_current_number(power.get("current"), "rage")
        observed["rage"] = rage
        if guard.rage_gte is not None and rage < guard.rage_gte:
            failed.append("rage_gte")
        if guard.rage_lte is not None and rage > guard.rage_lte:
            failed.append("rage_lte")

    if (
        guard.target_hp_pct_gte is not None
        or guard.target_hp_pct_lte is not None
    ):
        assert guard.target_index is not None
        percent = _target_health_percent(state, guard.target_index)
        observed["target_hp_pct"] = percent
        if (
            guard.target_hp_pct_gte is not None
            and percent < guard.target_hp_pct_gte
        ):
            failed.append("target_hp_pct_gte")
        if (
            guard.target_hp_pct_lte is not None
            and percent > guard.target_hp_pct_lte
        ):
            failed.append("target_hp_pct_lte")

    if guard.target_attackable_is is not None:
        assert guard.target_index is not None
        attackable = _target_attackable(state, guard.target_index)
        observed["target_attackable"] = attackable
        if attackable is not guard.target_attackable_is:
            failed.append("target_attackable_is")

    if guard.target_aura_action is not None:
        assert guard.target_index is not None
        stacks, selected = _selected_target_aura_stacks(
            state, guard.target_index, guard.target_aura_action
        )
        observed["selected_target_index"] = selected
        observed["target_aura_stacks"] = stacks
        if stacks is None:
            failed.append("target_aura_target_is_selected")
        elif stacks > guard.target_aura_stacks_lte:
            failed.append("target_aura_stacks_lte")

    if guard.estimated_remaining_attackable_gte_ms is not None:
        estimate, current_health, combined_dps = (
            _estimated_remaining_attackable_ms(state)
        )
        observed["current_attackable_health"] = current_health
        observed["prefix_combined_damage_per_second"] = combined_dps
        observed["estimated_remaining_attackable_ms"] = estimate
        if (
            estimate is None
            or estimate < guard.estimated_remaining_attackable_gte_ms
        ):
            failed.append("estimated_remaining_attackable_gte_ms")

    if (
        guard.live_target_count_gte is not None
        or guard.live_target_count_lte is not None
    ):
        live = _live_target_count(state)
        observed["live_target_count"] = live
        if (
            guard.live_target_count_gte is not None
            and live < guard.live_target_count_gte
        ):
            failed.append("live_target_count_gte")
        if (
            guard.live_target_count_lte is not None
            and live > guard.live_target_count_lte
        ):
            failed.append("live_target_count_lte")

    if (
        guard.attackable_target_count_gte is not None
        or guard.attackable_target_count_lte is not None
    ):
        attackable = _attackable_target_count(state)
        observed["attackable_target_count"] = attackable
        if (
            guard.attackable_target_count_gte is not None
            and attackable < guard.attackable_target_count_gte
        ):
            failed.append("attackable_target_count_gte")
        if (
            guard.attackable_target_count_lte is not None
            and attackable > guard.attackable_target_count_lte
        ):
            failed.append("attackable_target_count_lte")

    if guard.mh_swing_remaining_lte_ms is not None:
        remaining = _integer(
            state.get("mh_swing_remaining_ms"),
            "current mh_swing_remaining_ms",
            minimum=0,
        )
        observed["mh_swing_remaining_ms"] = remaining
        if remaining > guard.mh_swing_remaining_lte_ms:
            failed.append("mh_swing_remaining_lte_ms")

    if guard.queue_status_is is not None:
        queue = _mapping(state.get("swing_queue"), "swing_queue")
        status = queue.get("status")
        if status not in _QUEUE_STATUSES:
            raise GuardObservationError("current swing_queue status is invalid")
        observed["queue_status"] = status
        if status != guard.queue_status_is:
            failed.append("queue_status_is")

    if guard.aura_action is not None:
        remaining = _aura_remaining_ms(state, guard.aura_action)
        observed["aura_remaining_ms"] = remaining
        if remaining is None:
            failed.append("aura_remaining_ms")
        else:
            if (
                guard.aura_remaining_gte_ms is not None
                and remaining != -1
                and remaining < guard.aura_remaining_gte_ms
            ):
                failed.append("aura_remaining_gte_ms")
            if (
                guard.aura_remaining_lte_ms is not None
                and (remaining == -1 or remaining > guard.aura_remaining_lte_ms)
            ):
                failed.append("aura_remaining_lte_ms")

    if guard.action_ready is not None:
        ready_in = _action_ready_in_ms(available_actions, guard.action_ready)
        legal = _action_legal(available_actions, guard.action_ready)
        observed["action_ready_in_ms"] = ready_in
        observed["action_legal"] = legal
        if legal is not True or ready_in != 0:
            failed.append("action_ready")

    return GuardEvaluationV1(
        satisfied=not failed,
        failed_predicates=tuple(failed),
        observed=observed,
    )


def next_observable_guard_check_ms_v1(
    guard: ObservableCausalGuardV1,
    evaluation: GuardEvaluationV1,
    available_actions: Sequence[AvailableAction],
) -> int:
    """Choose a positive simulator wait from current visible timer values."""

    if evaluation.satisfied:
        raise ValueError("satisfied guard does not need another checkpoint")
    delays: list[int] = []
    event_driven = False
    failed = set(evaluation.failed_predicates)
    observed = evaluation.observed

    if "pull_relative_time_gte_ms" in failed:
        delays.append(
            guard.pull_relative_time_gte_ms
            - int(observed["pull_relative_time_ms"])
        )
    if "mh_swing_remaining_lte_ms" in failed:
        delays.append(
            int(observed["mh_swing_remaining_ms"])
            - guard.mh_swing_remaining_lte_ms
        )
    if "aura_remaining_lte_ms" in failed:
        remaining = observed.get("aura_remaining_ms")
        if type(remaining) is int and remaining >= 0:
            delays.append(remaining - guard.aura_remaining_lte_ms)
        else:
            event_driven = True
    if "action_ready" in failed:
        ready_in = _action_ready_in_ms(available_actions, guard.action_ready)
        if type(ready_in) is int and ready_in > 0:
            delays.append(ready_in)
        else:
            event_driven = True

    deterministic = {
        "pull_relative_time_gte_ms",
        "mh_swing_remaining_lte_ms",
        "aura_remaining_lte_ms",
        "action_ready",
    }
    if failed - deterministic or "aura_remaining_gte_ms" in failed:
        event_driven = True
    if event_driven or not delays:
        delays.append(guard.check_interval_ms)
    return max(1, min(delays))


__all__ = [
    "GuardEvaluationV1",
    "GuardObservationError",
    "GuardTimeoutError",
    "ObservableCausalGuardV1",
    "SKIP_PLAN",
    "WAIT_FOR_NEXT_OBSERVABLE_CHECKPOINT",
    "evaluate_observable_guard_v1",
    "next_observable_guard_check_ms_v1",
    "observable_causal_guard_from_dict_v1",
]
