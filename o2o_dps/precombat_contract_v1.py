"""Wire contract for a policy-visible pre-pull self-action window."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .sim_bridge import ActionRef, SimBridgeProtocolError


JSONMap = dict[str, Any]
PRECOMBAT_ACTIONS_SCHEMA_V1 = "o2o_precombat_actions/v1"


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _validate_action(action: object, label: str) -> ActionRef:
    if not isinstance(action, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (action.spell_id, action.item_id, action.other_id)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (*identities, action.tag)
    ):
        raise ValueError(f"{label} identity fields must be non-negative integers")
    if sum(value > 0 for value in identities) != 1:
        raise ValueError(f"{label} must have exactly one positive identity")
    return action


@dataclass(frozen=True)
class PrecombatActionsConfigV1:
    """Exact actions admitted before the modeled pull origin."""

    pull_time_ms: int
    self_actions: tuple[ActionRef, ...]

    def __post_init__(self) -> None:
        if _strict_int(self.pull_time_ms, "pull_time_ms") <= 0:
            raise ValueError("pull_time_ms must be positive")
        if not isinstance(self.self_actions, tuple) or not self.self_actions:
            raise TypeError("self_actions must be a non-empty tuple")
        for index, action in enumerate(self.self_actions):
            _validate_action(action, f"self_actions[{index}]")
        if len(set(self.self_actions)) != len(self.self_actions):
            raise ValueError("self_actions must be unique")

    def to_wire(self) -> JSONMap:
        return {
            "schema": PRECOMBAT_ACTIONS_SCHEMA_V1,
            "pull_time_ms": self.pull_time_ms,
            "self_actions": [action.to_wire() for action in self.self_actions],
        }


@dataclass(frozen=True)
class PrecombatActionsStateV1:
    pull_time_ms: int
    relative_time_ms: int
    active: bool
    self_action_count: int


def precombat_state_from_wire_v1(
    state: Mapping[str, Any],
    *,
    config: PrecombatActionsConfigV1 | None = None,
) -> PrecombatActionsStateV1:
    """Validate the precombat block embedded in a bridge state."""

    if not isinstance(state, Mapping):
        raise SimBridgeProtocolError("state must be an object")
    value = state.get("precombat")
    if not isinstance(value, Mapping):
        raise SimBridgeProtocolError("state is missing precombat")
    raw = dict(value)
    expected = {
        "schema",
        "pull_time_ms",
        "relative_time_ms",
        "active",
        "self_action_count",
    }
    if set(raw) != expected:
        raise SimBridgeProtocolError("precombat state field set mismatch")
    if raw["schema"] != PRECOMBAT_ACTIONS_SCHEMA_V1:
        raise SimBridgeProtocolError("precombat state schema is unsupported")
    try:
        pull_time_ms = _strict_int(raw["pull_time_ms"], "pull_time_ms")
        relative_time_ms = _strict_int(
            raw["relative_time_ms"], "relative_time_ms"
        )
        self_action_count = _strict_int(
            raw["self_action_count"], "self_action_count"
        )
        time_ms = _strict_int(state.get("time_ms"), "state.time_ms")
    except TypeError as error:
        raise SimBridgeProtocolError(str(error)) from error
    active = raw["active"]
    if pull_time_ms <= 0 or self_action_count <= 0:
        raise SimBridgeProtocolError("precombat state counts/times are invalid")
    if not isinstance(active, bool):
        raise SimBridgeProtocolError("precombat state active must be boolean")
    if relative_time_ms != time_ms - pull_time_ms:
        raise SimBridgeProtocolError("precombat relative time differs from bridge time")
    finished = state.get("finished")
    if not isinstance(finished, bool):
        raise SimBridgeProtocolError("state.finished must be boolean")
    if active != (not finished and time_ms < pull_time_ms):
        raise SimBridgeProtocolError("precombat active flag differs from pull origin")
    if config is not None and (
        pull_time_ms != config.pull_time_ms
        or self_action_count != len(config.self_actions)
    ):
        raise SimBridgeProtocolError("precombat state differs from requested config")
    return PrecombatActionsStateV1(
        pull_time_ms=pull_time_ms,
        relative_time_ms=relative_time_ms,
        active=active,
        self_action_count=self_action_count,
    )


def precombat_input_ready_v1(state: Mapping[str, Any]) -> bool:
    """Whether no-target NeedsInput is explained by a valid pre-pull window."""

    try:
        parsed = precombat_state_from_wire_v1(state)
    except SimBridgeProtocolError:
        return False
    return parsed.active


__all__ = (
    "PRECOMBAT_ACTIONS_SCHEMA_V1",
    "PrecombatActionsConfigV1",
    "PrecombatActionsStateV1",
    "precombat_input_ready_v1",
    "precombat_state_from_wire_v1",
)
