"""Synchronous client for the Windows-native ``o2obridge`` JSONL process.

The Go bridge owns one simulator environment at a time.  This client keeps the
process alive, but callers may issue ``load`` repeatedly to reconstruct a
canonical branch from the same RaidSimRequest and random seed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any, Mapping, Sequence


JSONMap = dict[str, Any]


class SimBridgeError(RuntimeError):
    """Base class for bridge startup, transport, and remote-command errors."""


class SimBridgeProcessError(SimBridgeError):
    """The bridge could not start or exited before returning a response."""


class SimBridgeProtocolError(SimBridgeError):
    """The bridge returned a response that does not satisfy its JSONL protocol."""


class SimBridgeCommandError(SimBridgeError):
    """The bridge understood a command but could not execute it."""


@dataclass(frozen=True, order=True)
class ActionRef:
    """Exact simulator spellbook identity, including the action tag."""

    spell_id: int = 0
    item_id: int = 0
    other_id: int = 0
    tag: int = 0

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "ActionRef":
        if not isinstance(value, Mapping):
            raise SimBridgeProtocolError("action must be a JSON object")
        return cls(
            spell_id=_wire_int(value, "spell_id"),
            item_id=_wire_int(value, "item_id"),
            other_id=_wire_int(value, "other_id"),
            tag=_wire_int(value, "tag"),
        )

    def to_wire(self) -> JSONMap:
        result: JSONMap = {}
        if self.spell_id:
            result["spell_id"] = self.spell_id
        if self.item_id:
            result["item_id"] = self.item_id
        if self.other_id:
            result["other_id"] = self.other_id
        if self.tag:
            result["tag"] = self.tag
        return result


@dataclass(frozen=True)
class AvailableAction:
    """One spellbook entry and its legality at the current decision point."""

    index: int
    action: ActionRef
    label: str
    legal: bool
    ready_in_ms: int
    triggers_gcd: bool

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "AvailableAction":
        if not isinstance(value, Mapping):
            raise SimBridgeProtocolError("each available action must be an object")
        action = value.get("action")
        if not isinstance(action, Mapping):
            raise SimBridgeProtocolError("available action is missing action identity")
        return cls(
            index=_wire_int(value, "index"),
            action=ActionRef.from_wire(action),
            label=str(value.get("label", "")),
            legal=_wire_bool(value, "legal"),
            ready_in_ms=_wire_int(value, "ready_in_ms"),
            triggers_gcd=_wire_bool(value, "triggers_gcd"),
        )


@dataclass(frozen=True)
class ActResult:
    """Result of an ``act`` command plus the immediate simulator state."""

    casted: bool
    consumes_decision: bool
    finished: bool
    needs_input: bool
    state: JSONMap


@dataclass(frozen=True)
class CancelQueueResult:
    """Result of the explicit, non-spell ``cancel_queue`` control command."""

    canceled: bool
    consumes_decision: bool
    finished: bool
    needs_input: bool
    state: JSONMap


@dataclass(frozen=True)
class SetTargetResult:
    """Result of changing the interactive player's current target."""

    changed: bool
    target_index: int
    finished: bool
    needs_input: bool
    state: JSONMap


class SimulatorBridge:
    """Own a persistent ``o2obridge.exe`` subprocess.

    The protocol is deliberately synchronous: one command is written and one
    JSON response is consumed while holding a lock.  Stderr is drained on a
    daemon thread so a verbose Go failure cannot fill the pipe and deadlock the
    simulator process.
    """

    def __init__(
        self,
        executable: str | os.PathLike[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        executable_path = Path(executable)
        child_environment = None
        if environment is not None:
            child_environment = os.environ.copy()
            child_environment.update(environment)

        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._process = subprocess.Popen(
                [str(executable_path)],
                cwd=None if cwd is None else str(cwd),
                env=child_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=creation_flags,
            )
        except OSError as error:
            raise SimBridgeProcessError(
                f"could not start simulator bridge {executable_path}: {error}"
            ) from error

        if self._process.stdin is None or self._process.stdout is None:
            self._process.kill()
            raise SimBridgeProcessError("simulator bridge was started without pipes")

        self._lock = threading.Lock()
        self._closed = False
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stderr_thread: threading.Thread | None = None
        if self._process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                name="o2obridge-stderr",
                daemon=True,
            )
            self._stderr_thread.start()

    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap:
        """Replace the environment and advance it to its first decision."""

        if not isinstance(request, Mapping):
            raise TypeError("RaidSimRequest must be a mapping")
        response = self._request(
            "load", request=dict(request), seed=_strict_int(seed, "seed")
        )
        return _response_state(response, "load")

    def advance(self) -> JSONMap:
        """Advance events until the next decision or encounter completion."""

        return _response_state(self._request("advance"), "advance")

    def state(self) -> JSONMap:
        """Return the current exported simulator state."""

        return _response_state(self._request("state"), "state")

    def actions(self) -> list[AvailableAction]:
        """Return exact spellbook actions and their current legality."""

        response = self._request("actions")
        values = response.get("actions")
        if not isinstance(values, list):
            raise SimBridgeProtocolError(
                "actions response is missing the actions array"
            )
        return [AvailableAction.from_wire(value) for value in values]

    def act(self, action: ActionRef | Mapping[str, Any]) -> ActResult:
        """Apply one exact action, preserving its tag."""

        action_ref = (
            action if isinstance(action, ActionRef) else ActionRef.from_wire(action)
        )
        response = self._request("act", action=action_ref.to_wire())
        apply = response.get("apply")
        if not isinstance(apply, Mapping):
            raise SimBridgeProtocolError("act response is missing apply result")
        return ActResult(
            casted=_wire_bool(apply, "casted"),
            consumes_decision=_wire_bool(apply, "consumes_decision"),
            finished=_wire_bool(apply, "finished"),
            needs_input=_wire_bool(apply, "needs_input"),
            state=_response_state(response, "act"),
        )

    def cancel_queue(self) -> CancelQueueResult:
        """Cancel an accepted Heroic Strike/Cleave queue without casting."""

        response = self._request("cancel_queue")
        result = response.get("cancel_queue")
        if not isinstance(result, Mapping):
            raise SimBridgeProtocolError(
                "cancel_queue response is missing cancel_queue result"
            )
        return CancelQueueResult(
            canceled=_wire_bool(result, "canceled"),
            consumes_decision=_wire_bool(result, "consumes_decision"),
            finished=_wire_bool(result, "finished"),
            needs_input=_wire_bool(result, "needs_input"),
            state=_response_state(response, "cancel_queue"),
        )

    def set_target(self, target_index: int) -> SetTargetResult:
        """Change target without consuming the current decision or swing timer."""

        index = _strict_int(target_index, "target_index")
        if index < 0:
            raise ValueError("target_index must be non-negative")
        response = self._request("set_target", target_index=index)
        result = response.get("set_target")
        if not isinstance(result, Mapping):
            raise SimBridgeProtocolError(
                "set_target response is missing set_target result"
            )
        return SetTargetResult(
            changed=_wire_bool(result, "changed"),
            target_index=_wire_int(result, "target_index"),
            finished=_wire_bool(result, "finished"),
            needs_input=_wire_bool(result, "needs_input"),
            state=_response_state(response, "set_target"),
        )

    def wait(self, wait_ms: int) -> JSONMap:
        """Advance with the bridge's explicit wait action."""

        duration = _strict_int(wait_ms, "wait_ms")
        if duration <= 0:
            raise ValueError("wait_ms must be positive")
        return _response_state(
            self._request("wait", wait_ms=duration), "wait"
        )

    def close(self) -> None:
        """Ask the bridge to clean up, then ensure the child has exited."""

        if self._closed:
            return
        close_error: SimBridgeError | None = None
        try:
            if self._process.poll() is None:
                try:
                    self._request("close")
                except SimBridgeError as error:
                    close_error = error
        finally:
            self._closed = True
            try:
                self._process.stdin.close()
            except OSError:
                pass
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
        if close_error is not None:
            raise close_error

    def __enter__(self) -> "SimulatorBridge":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            return

    def _request(self, command: str, **payload: Any) -> JSONMap:
        if self._closed:
            raise SimBridgeProcessError("simulator bridge is closed")
        message = {"command": command, **payload}
        try:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise SimBridgeProtocolError(
                f"{command} command is not JSON serializable: {error}"
            ) from error

        with self._lock:
            exit_code = self._process.poll()
            if exit_code is not None:
                raise self._exited_error(command, exit_code)
            try:
                self._process.stdin.write(encoded + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                exit_code = self._process.poll()
                detail = "unknown" if exit_code is None else str(exit_code)
                raise SimBridgeProcessError(
                    f"simulator bridge failed while sending {command} "
                    f"(exit code {detail}){self._stderr_suffix()}"
                ) from error

            line = self._process.stdout.readline()

        if line == "":
            exit_code = self._process.poll()
            detail = "unknown" if exit_code is None else str(exit_code)
            raise SimBridgeProcessError(
                f"simulator bridge ended before replying to {command} "
                f"(exit code {detail}){self._stderr_suffix()}"
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            excerpt = line.rstrip()[:240]
            raise SimBridgeProtocolError(
                f"simulator bridge returned invalid JSON for {command}: {excerpt!r}"
            ) from error
        if not isinstance(response, dict):
            raise SimBridgeProtocolError(
                f"simulator bridge returned non-object JSON for {command}"
            )
        if response.get("ok") is not True:
            remote_error = response.get("error")
            detail = remote_error if isinstance(remote_error, str) else "unknown error"
            raise SimBridgeCommandError(
                f"simulator bridge rejected {command}: {detail}{self._stderr_suffix()}"
            )
        response_command = response.get("command")
        if response_command != command:
            raise SimBridgeProtocolError(
                f"simulator bridge replied to {command} with command "
                f"{response_command!r}"
            )
        return response

    def _exited_error(self, command: str, exit_code: int) -> SimBridgeProcessError:
        return SimBridgeProcessError(
            f"simulator bridge exited before {command} (exit code {exit_code})"
            f"{self._stderr_suffix()}"
        )

    def _stderr_suffix(self) -> str:
        if not self._stderr_tail:
            return ""
        return "; stderr: " + " | ".join(self._stderr_tail)


def _response_state(response: Mapping[str, Any], command: str) -> JSONMap:
    state = response.get("state")
    if not isinstance(state, dict):
        raise SimBridgeProtocolError(
            f"{command} response is missing the simulator state object"
        )
    return dict(state)


def _wire_int(value: Mapping[str, Any], field: str) -> int:
    raw = value.get(field, 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SimBridgeProtocolError(f"{field} must be an integer")
    return raw


def _wire_bool(value: Mapping[str, Any], field: str) -> bool:
    raw = value.get(field)
    if not isinstance(raw, bool):
        raise SimBridgeProtocolError(f"{field} must be a boolean")
    return raw


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


__all__: Sequence[str] = (
    "ActionRef",
    "ActResult",
    "AvailableAction",
    "CancelQueueResult",
    "SetTargetResult",
    "SimBridgeCommandError",
    "SimBridgeError",
    "SimBridgeProcessError",
    "SimBridgeProtocolError",
    "SimulatorBridge",
)
