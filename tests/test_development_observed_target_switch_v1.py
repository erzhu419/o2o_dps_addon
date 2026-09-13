from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from o2o_dps.development_observed_target_switch_v1 import (
    ObservedTargetSwitchBridgeV1,
    TwoTargetOnlyObservedSwitchBridgeV1,
    select_current_hp_target_v1,
)
from o2o_dps.sim_bridge import SetTargetResult


def _state() -> dict:
    return {
        "time_ms": 100, "finished": False, "needs_input": True,
        "target_index": 0, "mh_swing_remaining_ms": 1500,
        "gcd_remaining_ms": 0,
        "dynamic_team_background": {"targets": [
            {"target_index": 0, "current_health": 1000.0, "dead": False},
            {"target_index": 1, "current_health": 2000.0, "dead": False},
        ]},
        "dynamic_target_semantics": {"targets": [
            {"target_index": 0, "dead": False, "attackable": True},
            {"target_index": 1, "dead": False, "attackable": True},
        ]},
    }


class _NativeControl:
    def __init__(self, state: dict) -> None:
        self.live = deepcopy(state)
        self.calls: list[int] = []

    def set_target(self, index: int) -> SetTargetResult:
        self.calls.append(index)
        old = self.live["target_index"]
        self.live["target_index"] = index
        return SetTargetResult(
            changed=old != index, target_index=index,
            finished=False, needs_input=True, state=deepcopy(self.live),
        )

    def advance(self) -> dict:
        return deepcopy(self.live)


class ObservedTargetSwitchTests(unittest.TestCase):
    def test_selection_uses_only_current_registry(self) -> None:
        state = _state()
        state["future_team_schedule"] = [{"time_ms": 200, "target_index": 0}]
        self.assertEqual(select_current_hp_target_v1(state), 1)
        changed_future = deepcopy(state)
        changed_future["future_team_schedule"] = [{"time_ms": 200, "target_index": 1}]
        self.assertEqual(select_current_hp_target_v1(changed_future), 1)

        state["dynamic_target_semantics"]["targets"][1]["attackable"] = False
        self.assertEqual(select_current_hp_target_v1(state), 0)
        state["dynamic_target_semantics"]["targets"][0]["dead"] = True
        state["dynamic_team_background"]["targets"][0]["dead"] = True
        self.assertIsNone(select_current_hp_target_v1(state))

    def test_initial_switch_has_native_receipt_and_preserves_timers(self) -> None:
        native = _NativeControl(_state())
        controller = ObservedTargetSwitchBridgeV1(native)
        selected = controller._select(deepcopy(native.live), initial=True)
        self.assertEqual(native.calls, [1])
        self.assertEqual(selected["target_index"], 1)
        receipt = controller.target_switch_receipts[0]
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["phase"], "INITIAL")
        self.assertEqual(receipt["observed_registry"][1]["current_health"], 2000.0)
        self.assertEqual(selected["mh_swing_remaining_ms"], 1500)

    def test_invalid_current_target_switches_after_advance(self) -> None:
        state = _state()
        state["dynamic_team_background"]["targets"][0]["dead"] = True
        state["dynamic_target_semantics"]["targets"][0]["dead"] = True
        native = _NativeControl(state)
        controller = ObservedTargetSwitchBridgeV1(native)
        selected = controller.advance()
        self.assertEqual(selected["target_index"], 1)
        self.assertEqual(native.calls, [1])
        self.assertEqual(controller.target_switch_receipts[0]["phase"], "INVALID_CURRENT_TARGET")

    def test_conditional_two_target_branch_is_exact_noop_elsewhere(self) -> None:
        state = _state()
        state["dynamic_team_background"]["targets"].append(
            {"target_index": 2, "current_health": 3000.0, "dead": False}
        )
        state["dynamic_target_semantics"]["targets"].append(
            {"target_index": 2, "dead": False, "attackable": True}
        )
        native = _NativeControl(state)
        controller = TwoTargetOnlyObservedSwitchBridgeV1(native)
        self.assertEqual(controller._select(state, initial=True), state)
        self.assertEqual(native.calls, [])
        self.assertEqual(controller.target_switch_receipts, [])


if __name__ == "__main__":
    unittest.main()
