"""Optional current-state main-target focus for the observed d900 trash route.

This development-only wrapper changes the *real* bridge target before a policy
decision.  It does not hide other targets or change cleave/AoE eligibility.
The one-wave observed order is not represented as a raid-leader instruction.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .upper_kara_trash_target_contract_v1 import EXACT_TARGETS


ORDERED_GUIDS = tuple(guid for guid, _ in EXACT_TARGETS)


class DevelopmentRouteFocusV1Error(ValueError):
    """The exact registry or a requested target conflicts with route focus."""


def current_route_focus_index_v1(
    state: Mapping[str, Any], native_target_guids: tuple[str, ...]
) -> int | None:
    """Choose the first *currently* alive and attackable exact GUID."""
    if tuple(native_target_guids) != ORDERED_GUIDS:
        raise DevelopmentRouteFocusV1Error("d900 route focus requires its exact three-GUID registry")
    if state.get("finished") is True:
        return None
    semantics = state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list) or len(rows) != len(ORDERED_GUIDS):
        raise DevelopmentRouteFocusV1Error("current target semantics differ from the exact registry")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or row.get("target_index") != index:
            raise DevelopmentRouteFocusV1Error("current target index differs from the exact registry")
        if row.get("attackable") is True and row.get("dead") is False:
            return index
    return None


class DoomguardCurrentStateRouteFocusedBridgeV1:
    """Focus the native main target at clean inputs, never rewrite source state.

    Pass an existing responsive driven bridge.  The wrapper may be supplied as
    a replay ``bridge_factory`` result; explicit off-route SET_TARGET requests
    fail instead of being silently redirected.  Target damage and collateral
    reachability remain entirely under the native three-target environment.
    """

    def __init__(self, bridge: Any, native_target_guids: tuple[str, ...]) -> None:
        if tuple(native_target_guids) != ORDERED_GUIDS:
            raise DevelopmentRouteFocusV1Error("d900 route focus requires its exact three-GUID registry")
        self._bridge = bridge
        self.native_target_guids = tuple(native_target_guids)
        self.route_focus_receipts: list[dict[str, Any]] = []

    def __enter__(self) -> "DoomguardCurrentStateRouteFocusedBridgeV1":
        self._bridge.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return self._bridge.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)

    def _focus(self, state: Mapping[str, Any]) -> dict[str, Any]:
        current = dict(state)
        if current.get("finished") is True or current.get("needs_input") is not True or current.get("wake_ready") is not None:
            return current
        focus = current_route_focus_index_v1(current, self.native_target_guids)
        if focus is None or current.get("target_index") == focus:
            return current
        result = self._bridge.set_target(focus)
        if result.target_index != focus:
            raise DevelopmentRouteFocusV1Error("native bridge selected a different target")
        updated = dict(result.state)
        if updated.get("target_index") != focus:
            raise DevelopmentRouteFocusV1Error("native state did not report the focused target")
        self.route_focus_receipts.append({
            "kind": "DEVELOPMENT_CURRENT_STATE_ROUTE_FOCUS",
            "time_ms": current["time_ms"],
            "from_target_index": current.get("target_index"),
            "to_target_index": focus,
            "to_target_guid": self.native_target_guids[focus],
            "source": "CURRENT_ALIVE_ATTACKABLE_STATE_NOT_FUTURE_DEATH_TIME",
            "comparison_authorized": False,
        })
        return updated

    def load_dynamic_v4(self, request: Mapping[str, Any], seed: int, config: Any) -> Any:
        loaded = self._bridge.load_dynamic_v4(request, seed, config)
        return replace(loaded, state=self._focus(loaded.state))

    def advance(self) -> dict[str, Any]:
        return self._focus(self._bridge.advance())

    def _focus_result(self, result: Any) -> Any:
        return replace(result, state=self._focus(result.state))

    def act(self, *args: Any, **kwargs: Any) -> Any:
        return self._focus_result(self._bridge.act(*args, **kwargs))

    def wait(self, duration_ms: int) -> dict[str, Any]:
        return self._focus(self._bridge.wait(duration_ms))

    def start_attack(self) -> Any:
        return self._focus_result(self._bridge.start_attack())

    def stop_cast(self) -> Any:
        return self._focus_result(self._bridge.stop_cast())

    def cancel_queue(self) -> Any:
        return self._focus_result(self._bridge.cancel_queue())

    def set_target(self, index: int) -> Any:
        focus = current_route_focus_index_v1(self._bridge.state(), self.native_target_guids)
        if focus is not None and index != focus:
            raise DevelopmentRouteFocusV1Error(
                f"explicit target {index} conflicts with current route focus {focus}"
            )
        return self._bridge.set_target(index)


__all__ = (
    "ORDERED_GUIDS",
    "DevelopmentRouteFocusV1Error",
    "DoomguardCurrentStateRouteFocusedBridgeV1",
    "current_route_focus_index_v1",
)
