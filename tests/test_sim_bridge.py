from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.sim_bridge import (
    ActionRef,
    CancelQueueResult,
    SetTargetResult,
    SimBridgeCommandError,
    SimBridgeProcessError,
    SimBridgeProtocolError,
    SimulatorBridge,
)


class _ResponseStream:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def readline(self) -> str:
        if not self.lines:
            return ""
        return self.lines.pop(0)


class _RequestStream:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self.closed = False

    def write(self, text: str) -> int:
        request = json.loads(text)
        self.process.requests.append(request)
        response = self.process.handler(request)
        if isinstance(response, str):
            line = response
        else:
            line = json.dumps(response)
        self.process.stdout.lines.append(line + "\n")
        if request["command"] == "close":
            self.process.return_code = 0
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, handler, stderr: str = "") -> None:
        self.handler = handler
        self.requests: list[dict[str, object]] = []
        self.return_code: int | None = None
        self.stdout = _ResponseStream()
        self.stdin = _RequestStream(self)
        self.stderr = io.StringIO(stderr)
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if self.return_code is None:
            self.return_code = 0
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def kill(self) -> None:
        self.killed = True
        self.return_code = 1


def _state(**changes: object) -> dict[str, object]:
    state: dict[str, object] = {
        "time_ms": 0,
        "remaining_ms": 60000,
        "finished": False,
        "needs_input": True,
        "damage_done": 0.0,
    }
    state.update(changes)
    return state


class SimulatorBridgeTests(unittest.TestCase):
    def test_persistent_process_round_trip_preserves_action_tag(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            response: dict[str, object] = {"ok": True, "command": command}
            if command == "load":
                response["state"] = _state()
            elif command == "actions":
                response["actions"] = [
                    {
                        "index": 7,
                        "action": {"spell_id": 25286, "tag": 1},
                        "label": "SpellID:25286 (Tag: 1)",
                        "legal": True,
                        "ready_in_ms": 0,
                        "triggers_gcd": False,
                    }
                ]
            elif command == "act":
                response["apply"] = {
                    "casted": True,
                    "consumes_decision": False,
                    "finished": False,
                    "needs_input": True,
                }
                response["state"] = _state()
            elif command == "cancel_queue":
                response["cancel_queue"] = {
                    "canceled": True,
                    "consumes_decision": False,
                    "finished": False,
                    "needs_input": True,
                }
                response["state"] = _state()
            elif command == "set_target":
                response["set_target"] = {
                    "changed": True,
                    "target_index": request["target_index"],
                    "finished": False,
                    "needs_input": True,
                }
                response["state"] = _state(
                    target_index=request["target_index"], num_targets=2
                )
            elif command == "wait":
                response["state"] = _state(time_ms=100, needs_input=False)
            elif command == "advance":
                response["state"] = _state(time_ms=1500)
            elif command == "state":
                response["state"] = _state(time_ms=1500)
            return response

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process) as popen:
            with SimulatorBridge("C:/tools/o2obridge.exe") as bridge:
                loaded = bridge.load({"raid": {}, "encounter": {}}, seed=41)
                actions = bridge.actions()
                applied = bridge.act(actions[0].action)
                canceled = bridge.cancel_queue()
                targeted = bridge.set_target(1)
                bridge.wait(100)
                bridge.advance()
                bridge.state()

        self.assertTrue(loaded["needs_input"])
        self.assertEqual(actions[0].action, ActionRef(spell_id=25286, tag=1))
        self.assertTrue(applied.casted)
        self.assertFalse(applied.consumes_decision)
        self.assertEqual(
            canceled,
            CancelQueueResult(
                canceled=True,
                consumes_decision=False,
                finished=False,
                needs_input=True,
                state=_state(),
            ),
        )
        self.assertEqual(
            targeted,
            SetTargetResult(
                changed=True,
                target_index=1,
                finished=False,
                needs_input=True,
                state=_state(target_index=1, num_targets=2),
            ),
        )
        self.assertEqual(
            process.requests[0],
            {
                "command": "load",
                "request": {"raid": {}, "encounter": {}},
                "seed": 41,
            },
        )
        self.assertEqual(
            process.requests[2]["action"], {"spell_id": 25286, "tag": 1}
        )
        self.assertEqual(process.requests[3], {"command": "cancel_queue"})
        self.assertEqual(
            process.requests[4], {"command": "set_target", "target_index": 1}
        )
        self.assertEqual(process.requests[-1], {"command": "close"})
        self.assertEqual(popen.call_count, 1)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_remote_error_names_command_and_bridge_error(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object]:
            command = str(request["command"])
            if command == "load":
                return {
                    "ok": False,
                    "command": command,
                    "error": "raid, encounter, and sim_options are required",
                }
            return {"ok": True, "command": command}

        process = _FakeProcess(handler, stderr="sim setup failed\n")
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(
                SimBridgeCommandError,
                "rejected load: raid, encounter, and sim_options are required",
            ):
                bridge.load({}, seed=1)
            bridge.close()

    def test_cancel_queue_requires_a_typed_result(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object]:
            return {"ok": True, "command": request["command"], "state": _state()}

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(
                SimBridgeProtocolError,
                "cancel_queue response is missing cancel_queue result",
            ):
                bridge.cancel_queue()
            bridge.close()

    def test_invalid_json_response_is_a_protocol_error(self) -> None:
        def handler(request: dict[str, object]) -> dict[str, object] | str:
            if request["command"] == "state":
                return "not-json"
            return {"ok": True, "command": request["command"]}

        process = _FakeProcess(handler)
        with patch("o2o_dps.sim_bridge.subprocess.Popen", return_value=process):
            bridge = SimulatorBridge("o2obridge.exe")
            with self.assertRaisesRegex(
                SimBridgeProtocolError, "invalid JSON for state"
            ):
                bridge.state()
            bridge.close()

    def test_start_failure_reports_executable(self) -> None:
        with patch(
            "o2o_dps.sim_bridge.subprocess.Popen",
            side_effect=FileNotFoundError("missing"),
        ):
            with self.assertRaisesRegex(
                SimBridgeProcessError, "could not start simulator bridge.*missing.exe"
            ):
                SimulatorBridge("C:/missing.exe")


if __name__ == "__main__":
    unittest.main()
