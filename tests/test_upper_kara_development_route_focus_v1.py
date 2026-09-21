from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import unittest

from o2o_dps.upper_kara_development_route_focus_v1 import (
    ORDERED_GUIDS,
    DevelopmentRouteFocusV1Error,
    DoomguardCurrentStateRouteFocusedBridgeV1,
    current_route_focus_index_v1,
)


def state(*, selected: int = 2, dead: tuple[int, ...] = (),
          unattackable: tuple[int, ...] = (), finished: bool = False,
          needs_input: bool = True, wake_ready: object = None) -> dict:
    targets = [
        {
            "target_index": index,
            "dead": index in dead,
            "attackable": index not in dead and index not in unattackable,
            "current_health": 0 if index in dead else 100,
        }
        for index in range(3)
    ]
    return {
        "time_ms": 4300,
        "finished": finished,
        "needs_input": needs_input,
        "wake_ready": wake_ready,
        "target_index": selected,
        "dynamic_target_semantics": {"targets": targets},
        "dynamic_team_background": {"targets": [{"target_index": i} for i in range(3)]},
    }


@dataclass(frozen=True)
class Loaded:
    state: dict


@dataclass(frozen=True)
class SetResult:
    state: dict
    target_index: int
    changed: bool


class StubBridge:
    def __init__(self, initial: dict, future: list[dict] | None = None) -> None:
        self.current = deepcopy(initial)
        self.future = [deepcopy(row) for row in (future or [])]
        self.set_calls: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def state(self):
        return deepcopy(self.current)

    def load_dynamic_v4(self, *_):
        return Loaded(self.state())

    def advance(self):
        self.current = self.future.pop(0)
        return self.state()

    def set_target(self, index: int):
        self.set_calls.append(index)
        changed = self.current["target_index"] != index
        self.current["target_index"] = index
        return SetResult(self.state(), index, changed)

    def wait(self, _duration_ms: int):
        return self.advance()

    def act(self, *_args, **_kwargs):
        return SetResult(self.advance(), self.current["target_index"], False)

    def start_attack(self):
        return SetResult(self.advance(), self.current["target_index"], False)

    def stop_cast(self):
        return SetResult(self.advance(), self.current["target_index"], False)

    def cancel_queue(self):
        return SetResult(self.advance(), self.current["target_index"], False)


class DevelopmentRouteFocusTests(unittest.TestCase):
    def test_current_state_order_and_unattackable_skip(self):
        self.assertEqual(0, current_route_focus_index_v1(state(), ORDERED_GUIDS))
        self.assertEqual(1, current_route_focus_index_v1(state(dead=(0,)), ORDERED_GUIDS))
        self.assertEqual(1, current_route_focus_index_v1(state(unattackable=(0,)), ORDERED_GUIDS))
        self.assertEqual(2, current_route_focus_index_v1(state(dead=(0, 1)), ORDERED_GUIDS))
        self.assertIsNone(current_route_focus_index_v1(state(dead=(0, 1, 2)), ORDERED_GUIDS))

    def test_load_and_advance_change_real_target_without_hiding_collateral(self):
        initial = state(selected=2)
        later = state(selected=0, dead=(0,))
        raw = StubBridge(initial, [later])
        focused = DoomguardCurrentStateRouteFocusedBridgeV1(raw, ORDERED_GUIDS)
        with focused:
            loaded = focused.load_dynamic_v4({}, 1, None)
            self.assertEqual(0, loaded.state["target_index"])
            self.assertEqual(3, len(loaded.state["dynamic_target_semantics"]["targets"]))
            self.assertEqual(initial["dynamic_team_background"], loaded.state["dynamic_team_background"])
            next_state = focused.advance()
            self.assertEqual(1, next_state["target_index"])
        self.assertEqual([0, 1], raw.set_calls)
        self.assertEqual([ORDERED_GUIDS[0], ORDERED_GUIDS[1]],
                         [row["to_target_guid"] for row in focused.route_focus_receipts])

    def test_wake_or_finished_does_not_create_target_action(self):
        for snapshot in (
            state(wake_ready={"time_ms": 4300}),
            state(needs_input=False),
            state(finished=True, dead=(0, 1, 2)),
        ):
            raw = StubBridge(snapshot)
            focused = DoomguardCurrentStateRouteFocusedBridgeV1(raw, ORDERED_GUIDS)
            self.assertEqual(snapshot, focused.load_dynamic_v4({}, 1, None).state)
            self.assertEqual([], raw.set_calls)

    def test_explicit_off_route_request_rejected_not_rewritten(self):
        raw = StubBridge(state(selected=0))
        focused = DoomguardCurrentStateRouteFocusedBridgeV1(raw, ORDERED_GUIDS)
        with self.assertRaisesRegex(DevelopmentRouteFocusV1Error, "conflicts"):
            focused.set_target(2)
        self.assertEqual([], raw.set_calls)
        self.assertEqual(0, focused.set_target(0).target_index)

    def test_decision_return_paths_also_route_before_next_policy_input(self):
        for method, args in (
            ("wait", (100,)),
            ("act", ("some-action",)),
            ("start_attack", ()),
            ("stop_cast", ()),
            ("cancel_queue", ()),
        ):
            raw = StubBridge(state(selected=0), [state(selected=0, dead=(0,))])
            focused = DoomguardCurrentStateRouteFocusedBridgeV1(raw, ORDERED_GUIDS)
            result = getattr(focused, method)(*args)
            returned = result if isinstance(result, dict) else result.state
            self.assertEqual(1, returned["target_index"], method)
            self.assertEqual([1], raw.set_calls, method)

    def test_other_registry_is_not_bound(self):
        with self.assertRaisesRegex(DevelopmentRouteFocusV1Error, "exact three-GUID"):
            DoomguardCurrentStateRouteFocusedBridgeV1(
                StubBridge(state()), tuple(reversed(ORDERED_GUIDS))
            )


if __name__ == "__main__":
    unittest.main()
