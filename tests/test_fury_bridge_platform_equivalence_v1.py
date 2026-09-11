from __future__ import annotations

import unittest

from o2o_dps.fury_bridge_platform_equivalence_v1 import (
    FuryBridgePlatformEquivalenceError,
    canonical_bytes,
    capture_trace,
    compare_traces,
    first_difference,
    linux_wsl_arguments,
)
from o2o_dps.fury_full_policy_rollout_v2 import (
    ServerResultBatchV2,
    ServerResultEventV2,
    ServerTargetResultV2,
)
from o2o_dps.sim_bridge import ActionRef, ActResult, AvailableAction


def _action(
    spell_id: int, *, index: int, legal: bool = True, triggers_gcd: bool = True
) -> AvailableAction:
    return AvailableAction(
        index=index,
        action=ActionRef(spell_id=spell_id),
        label=f"spell-{spell_id}",
        legal=legal,
        ready_in_ms=0,
        triggers_gcd=triggers_gcd,
    )


class FakeBridge:
    def __init__(self) -> None:
        self.epoch = 0
        self.selected: list[ActionRef] = []
        self.pending: dict[str, ActionRef] = {}

    def load(self, request, seed):
        return {"time_ms": 0, "damage_done": 0.0, "seed_echo": seed}

    def actions(self):
        return [
            _action(900, index=4, triggers_gcd=False),
            _action(30000, index=3),
            _action(1680, index=8),
            _action(45961, index=9),
            _action(100, index=2, legal=False),
        ]

    def act(self, action, *, attempt_id=None):
        self.selected.append(action)
        if attempt_id is not None:
            self.pending[attempt_id] = action
        return ActResult(
            casted=True,
            consumes_decision=True,
            finished=False,
            needs_input=False,
            state={"time_ms": self.epoch * 1500, "selected": action.spell_id},
        )

    def server_results_since_last_decision(self, attempt_ids=()):
        events = ()
        pending = tuple(attempt_ids)
        if self.epoch > 0 and pending:
            events = tuple(
                ServerResultEventV2(
                    time_ms=self.epoch * 1500,
                    outcome="HIT",
                    attempt_id=attempt_id,
                    damage=17.0,
                    action=self.pending.pop(attempt_id),
                    target_results=(
                        ServerTargetResultV2(
                            target_index=0,
                            outcome="HIT",
                            damage=17.0,
                        ),
                    ),
                )
                for attempt_id in pending
            )
            pending = ()
        return ServerResultBatchV2(
            complete_through_time_ms=self.epoch * 1500,
            events=events,
            pending_attempt_ids=pending,
        )

    def advance(self):
        self.epoch += 1
        return {
            "time_ms": self.epoch * 1500,
            "damage_done": float(self.epoch * 17),
            "needs_input": True,
        }


class FuryBridgePlatformEquivalenceV1Tests(unittest.TestCase):
    def test_canonical_bytes_are_order_independent_and_reject_nan(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        with self.assertRaisesRegex(
            FuryBridgePlatformEquivalenceError, "not finite canonical JSON"
        ):
            canonical_bytes({"bad": float("nan")})

    def test_capture_trace_uses_first_legal_gcd_action(self):
        bridge = FakeBridge()
        trace = capture_trace(bridge, {"raid": {}}, seed=41, decision_count=3)
        self.assertEqual(len(trace), 16)
        self.assertEqual([value.spell_id for value in bridge.selected], [45961, 1680, 1680])
        advances = [row for row in trace if row["command"] == "advance"]
        self.assertEqual(advances[-1]["state"]["damage_done"], 51.0)
        result_rows = [row for row in trace if row["command"] == "server_results"]
        observed_attempt_ids = [
            event["attempt_id"]
            for row in result_rows
            for event in row["result"]["events"]
        ]
        self.assertEqual(
            observed_attempt_ids,
            [
                "platform-equivalence:0",
                "platform-equivalence:1",
                "platform-equivalence:2",
            ],
        )
        observed_target_results = [
            event["target_results"]
            for row in result_rows
            for event in row["result"]["events"]
        ]
        self.assertEqual(
            observed_target_results,
            [
                ({"target_index": 0, "outcome": "HIT", "damage": 17.0},),
                ({"target_index": 0, "outcome": "HIT", "damage": 17.0},),
                ({"target_index": 0, "outcome": "HIT", "damage": 17.0},),
            ],
        )
        self.assertEqual(
            result_rows[0]["result"]["pending_attempt_ids"],
            ("platform-equivalence:0",),
        )

    def test_capture_trace_fails_without_a_legal_gcd_action(self):
        bridge = FakeBridge()
        bridge.actions = lambda: [_action(1, index=1, triggers_gcd=False)]
        with self.assertRaisesRegex(
            FuryBridgePlatformEquivalenceError, "no legal GCD action"
        ):
            capture_trace(bridge, {}, seed=1, decision_count=1)

    def test_compare_traces_is_exact_and_content_addressed(self):
        trace = [{"command": "load", "state": {"damage_done": 1.25}}]
        result = compare_traces(trace, [{"state": {"damage_done": 1.25}, "command": "load"}])
        self.assertEqual(result["status"], "PASS_EXACT_TRACE")
        self.assertTrue(result["exact_canonical_json"])
        self.assertEqual(
            result["windows_trace_sha256"], result["linux_trace_sha256"]
        )
        self.assertIsNone(result["first_difference"])

    def test_compare_traces_reports_first_value_difference(self):
        windows = [{"state": {"time_ms": 1500, "damage_done": 10.0}}]
        linux = [{"state": {"time_ms": 1500, "damage_done": 10.5}}]
        result = compare_traces(windows, linux)
        self.assertEqual(result["status"], "FAIL_TRACE_DIFFERENCE")
        self.assertFalse(result["exact_canonical_json"])
        self.assertEqual(
            result["first_difference"],
            {
                "path": "$[0].state.damage_done",
                "reason": "value",
                "windows": 10.0,
                "linux": 10.5,
            },
        )

    def test_first_difference_does_not_coerce_bool_to_integer(self):
        difference = first_difference({"x": True}, {"x": 1})
        self.assertEqual(difference["path"], "$.x")
        self.assertEqual(difference["reason"], "type")

    @unittest.skipUnless(__import__("os").name == "nt", "Windows path contract")
    def test_wsl_arguments_preserve_each_path_as_one_argument(self):
        arguments = linux_wsl_arguments(
            distro="Ubuntu-22.04",
            simulator_root=__import__("pathlib").Path("D:/WOW Tree/wowsims-turtle"),
            linux_bridge=__import__("pathlib").Path("D:/WOW Tree/bin/bridge"),
        )
        self.assertEqual(arguments[:4], ("-d", "Ubuntu-22.04", "--cd", "/mnt/d/WOW Tree/wowsims-turtle"))
        self.assertEqual(arguments[-2:], ("--", "/mnt/d/WOW Tree/bin/bridge"))


if __name__ == "__main__":
    unittest.main()
