"""Development-only manual target control from the current native registry.

This controller is separate from Cat's source policy: profile 1 has no target
sink.  It reads only the state returned at the current decision, never the
future team schedule or target-death times.  The initial rule chooses the
alive, attackable target with most remaining HP; subsequent control is used
only when the selected target is no longer eligible.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .sim_bridge import SetTargetResult
from .sim_bridge_dynamic_v3 import DynamicLoadResultV3


SCHEMA = "development_observed_target_switch/v1"


def current_target_registry_v1(state: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Project only decision-time HP and eligibility from aligned native rows."""

    life = state["dynamic_team_background"]["targets"]
    semantic = state["dynamic_target_semantics"]["targets"]
    if len(life) != len(semantic):
        raise ValueError("native target registry rows are not aligned")
    rows = []
    for index, (health, status) in enumerate(zip(life, semantic)):
        if health["target_index"] != index or status["target_index"] != index:
            raise ValueError("native target registry index mismatch")
        if health["dead"] != status["dead"]:
            raise ValueError("native target registry death state mismatch")
        rows.append({
            "target_index": index,
            "current_health": health["current_health"],
            "dead": status["dead"],
            "attackable": status["attackable"],
        })
    return tuple(rows)


def select_current_hp_target_v1(state: Mapping[str, Any]) -> int | None:
    """A deterministic observable-state rule; a tie preserves index order."""

    eligible = [row for row in current_target_registry_v1(state)
                if not row["dead"] and row["attackable"]]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["current_health"], -row["target_index"]))[
        "target_index"
    ]


class ObservedTargetSwitchBridgeV1:
    """Expose explicit native set_target receipts while Cat continues unchanged."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self.target_switch_receipts: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def _select(self, state: Mapping[str, Any], *, initial: bool) -> dict[str, Any]:
        before = dict(state)
        rows = current_target_registry_v1(before)
        current = before["target_index"]
        if type(current) is not int or current < 0 or current >= len(rows):
            raise ValueError("selected native target is outside observed registry")
        if initial:
            requested = select_current_hp_target_v1(before)
        else:
            selected = rows[current]
            if not selected["dead"] and selected["attackable"]:
                return before
            requested = select_current_hp_target_v1(before)
        if requested is None or requested == current:
            return before
        if before["finished"] or not before["needs_input"]:
            return before
        result = self._bridge.set_target(requested)
        if not isinstance(result, SetTargetResult):
            raise TypeError("native set_target must return SetTargetResult")
        after = result.state
        accepted = (
            result.changed and result.target_index == requested
            and after["target_index"] == requested
            and result.needs_input == before["needs_input"]
            and after["time_ms"] == before["time_ms"]
            and after["mh_swing_remaining_ms"] == before["mh_swing_remaining_ms"]
            and after["gcd_remaining_ms"] == before["gcd_remaining_ms"]
        )
        receipt = {
            "schema": SCHEMA,
            "phase": "INITIAL" if initial else "INVALID_CURRENT_TARGET",
            "observation_time_ms": before["time_ms"],
            "observed_registry": rows,
            "prior_target_index": current,
            "requested_target_index": requested,
            "native_changed": result.changed,
            "native_target_index": result.target_index,
            "accepted": accepted,
        }
        self.target_switch_receipts.append(receipt)
        if not accepted:
            raise RuntimeError("native set_target postcondition failed")
        return dict(after)

    def load_dynamic_v3(self, request: Mapping[str, Any], seed: int, config: Any) -> DynamicLoadResultV3:
        self.target_switch_receipts.clear()
        result = self._bridge.load_dynamic_v3(request, seed, config)
        if not isinstance(result, DynamicLoadResultV3):
            raise TypeError("native load_dynamic_v3 must return DynamicLoadResultV3")
        return replace(result, state=self._select(result.state, initial=True))

    def advance(self) -> dict[str, Any]:
        return self._select(self._bridge.advance(), initial=False)

    def wait(self, wait_ms: int) -> dict[str, Any]:
        return self._select(self._bridge.wait(wait_ms), initial=False)


class TwoTargetOnlyObservedSwitchBridgeV1(ObservedTargetSwitchBridgeV1):
    """Prespecified conditional branch; all other counts exactly keep Cat."""

    def _select(self, state: Mapping[str, Any], *, initial: bool) -> dict[str, Any]:
        if len(current_target_registry_v1(state)) != 2:
            return dict(state)
        return super()._select(state, initial=initial)


__all__ = [
    "SCHEMA", "ObservedTargetSwitchBridgeV1", "TwoTargetOnlyObservedSwitchBridgeV1",
    "current_target_registry_v1", "select_current_hp_target_v1",
]
