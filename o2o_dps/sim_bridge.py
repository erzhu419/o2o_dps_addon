"""Synchronous client for an ``o2obridge`` JSONL process.

The Go bridge owns one simulator environment at a time.  This client keeps the
process alive, but callers may issue ``load`` repeatedly to reconstruct a
canonical branch from the same RaidSimRequest and random seed.  A paired seed
reconstructs a branch start; it does not promise draw-by-draw common random
numbers after policies select different actions or target paths.  Dynamic
candidate/background damage is conserved through the bridge accounting path,
while the simulator's public Unit health APIs remain available to mechanics
outside that bridge contract.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import threading
from typing import Any, Mapping, Sequence


JSONMap = dict[str, Any]

SERVER_RESULT_OUTCOMES_V2 = frozenset(
    {
        "HIT",
        "CRIT",
        "MISS",
        "DODGE",
        "PARRY",
        "BLOCK",
        "BLOCK_CRIT",
        "GLANCE",
        "CRUSH",
        "MIXED",
        "CANCELED",
    }
)
SERVER_TARGET_RESULT_OUTCOMES_V2 = SERVER_RESULT_OUTCOMES_V2 - {"CANCELED"}

DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1 = "o2o_dynamic_team_background/v1"
DYNAMIC_SAME_TIMESTAMP_ORDER_V1 = "BACKGROUND_BEFORE_CANDIDATE"
DYNAMIC_RETARGET_MODES_V1 = frozenset(
    {"NEXT_ALIVE_CYCLIC", "REQUIRE_EXPLICIT"}
)
DYNAMIC_DAMAGE_RECEIPT_STATUSES_V1 = frozenset(
    {"APPLIED", "CANCELED_TARGET_DEAD"}
)
DYNAMIC_CANDIDATE_DAMAGE_RECEIPT_STATUSES_V1 = frozenset(
    {"APPLIED", "NO_DAMAGE", "CANCELED_TARGET_DEAD"}
)
DYNAMIC_CANDIDATE_DAMAGE_OUTCOMES_V1 = SERVER_RESULT_OUTCOMES_V2 | {"EMPTY"}
DYNAMIC_CANDIDATE_RESOLUTION_PHASES_V1 = frozenset(
    {
        "APPLIED_AFTER_OUTCOME",
        "NO_DAMAGE_AFTER_OUTCOME",
        "TARGET_DEAD_BEFORE_OUTCOME",
        "TARGET_DIED_AFTER_OUTCOME",
    }
)
_DYNAMIC_EVENT_ID_PATTERN_V1 = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_DYNAMIC_TIME_MS_V1 = ((1 << 63) - 1) // 1_000_000


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


@dataclass(frozen=True)
class DynamicTargetHealthV1:
    """One content-bound target index and its positive initial HP."""

    target_index: int
    health: float

    def __post_init__(self) -> None:
        index = _strict_int(self.target_index, "target_index")
        if index < 0:
            raise ValueError("target_index must be non-negative")
        health = _strict_positive_finite_number(self.health, "health")
        object.__setattr__(self, "target_index", index)
        object.__setattr__(self, "health", health)

    def to_wire(self) -> JSONMap:
        return {"target_index": self.target_index, "health": self.health}


@dataclass(frozen=True)
class BackgroundDamageEventV1:
    """One immutable team-background damage event in causal schedule order."""

    schedule_index: int
    time_ms: int
    target_index: int
    event_id: str
    damage: float

    def __post_init__(self) -> None:
        schedule_index = _strict_int(self.schedule_index, "schedule_index")
        time_ms = _strict_int(self.time_ms, "time_ms")
        target_index = _strict_int(self.target_index, "target_index")
        if schedule_index < 0:
            raise ValueError("schedule_index must be non-negative")
        if time_ms < 0:
            raise ValueError("time_ms must be non-negative")
        if time_ms > _MAX_DYNAMIC_TIME_MS_V1:
            raise ValueError("time_ms exceeds the Go duration range")
        if target_index < 0:
            raise ValueError("target_index must be non-negative")
        if not isinstance(self.event_id, str) or not _DYNAMIC_EVENT_ID_PATTERN_V1.fullmatch(
            self.event_id
        ):
            raise ValueError("event_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
        damage = _strict_positive_finite_number(self.damage, "damage")
        object.__setattr__(self, "schedule_index", schedule_index)
        object.__setattr__(self, "time_ms", time_ms)
        object.__setattr__(self, "target_index", target_index)
        object.__setattr__(self, "damage", damage)

    def to_wire(self) -> JSONMap:
        return {
            "schedule_index": self.schedule_index,
            "time_ms": self.time_ms,
            "target_index": self.target_index,
            "event_id": self.event_id,
            "damage": self.damage,
        }


@dataclass(frozen=True)
class DynamicTeamBackgroundConfigV1:
    """Immutable dynamic target lifecycle configuration for ``load_dynamic_v1``."""

    target_health: tuple[DynamicTargetHealthV1, ...]
    background_damage_events: tuple[BackgroundDamageEventV1, ...] = ()
    same_timestamp_order: str = DYNAMIC_SAME_TIMESTAMP_ORDER_V1
    retarget_mode: str = "NEXT_ALIVE_CYCLIC"
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target_health, tuple) or any(
            not isinstance(value, DynamicTargetHealthV1)
            for value in self.target_health
        ):
            raise TypeError("target_health must be a tuple of DynamicTargetHealthV1")
        if not self.target_health or tuple(
            value.target_index for value in self.target_health
        ) != tuple(range(len(self.target_health))):
            raise ValueError(
                "target_health must cover target indexes 0..N-1 exactly in ascending order"
            )
        if not isinstance(self.background_damage_events, tuple) or any(
            not isinstance(value, BackgroundDamageEventV1)
            for value in self.background_damage_events
        ):
            raise TypeError(
                "background_damage_events must be a tuple of BackgroundDamageEventV1"
            )
        if self.same_timestamp_order != DYNAMIC_SAME_TIMESTAMP_ORDER_V1:
            raise ValueError(
                f"same_timestamp_order must be {DYNAMIC_SAME_TIMESTAMP_ORDER_V1!r}"
            )
        if self.retarget_mode not in DYNAMIC_RETARGET_MODES_V1:
            raise ValueError(f"unsupported retarget_mode {self.retarget_mode!r}")

        seen_event_ids: set[str] = set()
        previous_time_ms = -1
        for index, event in enumerate(self.background_damage_events):
            if event.schedule_index != index:
                raise ValueError(
                    "background_damage_events schedule_index must cover "
                    "0..N-1 exactly in list order"
                )
            if event.time_ms < previous_time_ms:
                raise ValueError(
                    "background_damage_events must be strictly sorted by (time_ms,schedule_index)"
                )
            previous_time_ms = event.time_ms
            if event.target_index >= len(self.target_health):
                raise ValueError(
                    f"background_damage_events[{index}].target_index out of range"
                )
            if event.event_id in seen_event_ids:
                raise ValueError(
                    f"duplicate background event_id {event.event_id!r}"
                )
            seen_event_ids.add(event.event_id)

        object.__setattr__(self, "content_sha256", _dynamic_config_digest_v1(self))

    def to_wire(self) -> JSONMap:
        return {
            "schema": DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1,
            "content_sha256": self.content_sha256,
            "target_health": [value.to_wire() for value in self.target_health],
            "background_damage_events": [
                value.to_wire() for value in self.background_damage_events
            ],
            "same_timestamp_order": self.same_timestamp_order,
            "retarget_mode": self.retarget_mode,
        }


@dataclass(frozen=True)
class DynamicLoadReceiptV1:
    schema: str
    config_digest: str
    environment_generation: int
    target_count: int
    background_event_count: int
    same_timestamp_order: str
    retarget_mode: str


@dataclass(frozen=True)
class DynamicLoadResultV1:
    receipt: DynamicLoadReceiptV1
    state: JSONMap


@dataclass(frozen=True)
class DynamicDamageReceiptV1:
    schedule_index: int
    event_id: str
    time_ms: int
    target_index: int
    requested_damage: float
    applied_damage: float
    overkill_damage: float
    killed: bool
    status: str
    damage_ordinal: int | None = None
    retargeted_to: int | None = None


@dataclass(frozen=True)
class DynamicDamageReceiptBatchV1:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    schedule_complete: bool
    receipts: tuple[DynamicDamageReceiptV1, ...]


@dataclass(frozen=True)
class DynamicCandidateDamageReceiptV1:
    damage_ordinal: int
    time_ms: int
    target_index: int
    requested_damage: float
    applied_damage: float
    overkill_damage: float
    killed: bool
    status: str
    action: ActionRef
    outcome: str
    execution_id: int
    execution_index: int
    landed_execution_index: int
    resolution_phase: str
    outcome_computed: bool
    random_stream_rewound: bool
    attempt_id: str | None = None
    retargeted_to: int | None = None


@dataclass(frozen=True)
class DynamicCandidateDamageReceiptBatchV1:
    schema: str
    config_digest: str
    environment_generation: int
    cursor: int
    next_cursor: int
    receipts: tuple[DynamicCandidateDamageReceiptV1, ...]


def _control_result_from_response(
    response: Mapping[str, Any], command: str
) -> Any:
    """Decode a control result without importing the rollout types eagerly.

    ``fury_full_policy_rollout_v2`` imports this module, so the versioned
    control type is imported only when the method is called.  This keeps the
    public result identity required by the ordered sink executor without a
    module-import cycle.
    """

    result = response.get("control")
    if not isinstance(result, Mapping):
        raise SimBridgeProtocolError(
            f"{command} response is missing control result"
        )
    from .fury_ordered_sink_executor_v2 import ControlSinkResultV2

    return ControlSinkResultV2(
        accepted=_wire_bool(result, "accepted"),
        consumes_decision=_wire_bool(result, "consumes_decision"),
        state=_response_state(response, command),
    )


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
        arguments: Sequence[str] = (),
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        executable_path = Path(executable)
        if isinstance(arguments, (str, bytes)) or not isinstance(arguments, Sequence):
            raise TypeError("arguments must be a sequence of strings")
        if any(not isinstance(value, str) or not value.strip() for value in arguments):
            raise TypeError("each bridge argument must be a nonempty string")
        command = [str(executable_path), *arguments]
        child_environment = None
        if environment is not None:
            child_environment = os.environ.copy()
            child_environment.update(environment)

        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._process = subprocess.Popen(
                command,
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
        self._dynamic_binding: tuple[
            int, str, DynamicTeamBackgroundConfigV1
        ] | None = None
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
        # A load command atomically replaces (or invalidates on failure) the
        # prior environment, so no previous generation may bind its response.
        self._dynamic_binding = None
        response = self._request(
            "load", request=dict(request), seed=_strict_int(seed, "seed")
        )
        return _response_state(response, "load")

    def load_dynamic_v1(
        self,
        request: Mapping[str, Any],
        seed: int,
        config: DynamicTeamBackgroundConfigV1,
    ) -> DynamicLoadResultV1:
        """Atomically load an HP-bound target set and immutable team schedule."""

        if not isinstance(request, Mapping):
            raise TypeError("RaidSimRequest must be a mapping")
        if not isinstance(config, DynamicTeamBackgroundConfigV1):
            raise TypeError("config must be a DynamicTeamBackgroundConfigV1")
        self._dynamic_binding = None
        response = self._request(
            "load_dynamic_v1",
            request=dict(request),
            seed=_strict_int(seed, "seed"),
            dynamic=config.to_wire(),
        )
        raw_receipt = response.get("dynamic_load")
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError(
                "load_dynamic_v1 response is missing dynamic_load receipt"
            )
        receipt = _dynamic_load_receipt_v1(raw_receipt, config)
        response_generation = _required_positive_wire_int(
            response, "environment_generation"
        )
        if response_generation != receipt.environment_generation:
            raise SimBridgeProtocolError(
                "load_dynamic_v1 response environment generation differs from receipt"
            )
        state = _response_state(response, "load_dynamic_v1")
        _validate_dynamic_state_binding_v1(
            state,
            generation=receipt.environment_generation,
            config=config,
        )
        self._dynamic_binding = (
            receipt.environment_generation,
            receipt.config_digest,
            config,
        )
        return DynamicLoadResultV1(receipt=receipt, state=state)

    def advance(self) -> JSONMap:
        """Advance events until the next decision or encounter completion."""

        return self._bound_state(self._request("advance"), "advance")

    def state(self) -> JSONMap:
        """Return the current exported simulator state."""

        return self._bound_state(self._request("state"), "state")

    def actions(self) -> list[AvailableAction]:
        """Return exact spellbook actions and their current legality."""

        response = self._request("actions")
        values = response.get("actions")
        if not isinstance(values, list):
            raise SimBridgeProtocolError(
                "actions response is missing the actions array"
            )
        return [AvailableAction.from_wire(value) for value in values]

    def dynamic_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicDamageReceiptBatchV1:
        """Read completed background receipts without draining earlier rows."""

        normalized_cursor = _strict_int(cursor, "cursor")
        if normalized_cursor < 0:
            raise ValueError("cursor must be non-negative")
        if self._dynamic_binding is None:
            raise SimBridgeProtocolError(
                "dynamic_damage_receipts requires a successful load_dynamic_v1"
            )
        response = self._request(
            "dynamic_damage_receipts", cursor=normalized_cursor
        )
        raw_batch = response.get("dynamic_damage_receipts")
        if not isinstance(raw_batch, Mapping):
            raise SimBridgeProtocolError(
                "dynamic_damage_receipts response is missing its receipt batch"
            )
        generation, config_digest, config = self._dynamic_binding
        return _dynamic_damage_receipt_batch_v1(
            raw_batch,
            requested_cursor=normalized_cursor,
            generation=generation,
            config_digest=config_digest,
            config=config,
        )

    def dynamic_candidate_damage_receipts(
        self, *, cursor: int = 0
    ) -> DynamicCandidateDamageReceiptBatchV1:
        """Read candidate applications without draining prior audit rows."""

        normalized_cursor = _strict_int(cursor, "cursor")
        if normalized_cursor < 0:
            raise ValueError("cursor must be non-negative")
        if self._dynamic_binding is None:
            raise SimBridgeProtocolError(
                "dynamic_candidate_damage_receipts requires a successful load_dynamic_v1"
            )
        response = self._request(
            "dynamic_candidate_damage_receipts", cursor=normalized_cursor
        )
        raw_batch = response.get("dynamic_candidate_damage_receipts")
        if not isinstance(raw_batch, Mapping):
            raise SimBridgeProtocolError(
                "dynamic_candidate_damage_receipts response is missing its receipt batch"
            )
        generation, config_digest, config = self._dynamic_binding
        return _dynamic_candidate_damage_receipt_batch_v1(
            raw_batch,
            requested_cursor=normalized_cursor,
            generation=generation,
            config_digest=config_digest,
            target_count=len(config.target_health),
        )

    def act(
        self,
        action: ActionRef | Mapping[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> ActResult:
        """Apply one exact action, preserving its tag."""

        action_ref = (
            action if isinstance(action, ActionRef) else ActionRef.from_wire(action)
        )
        payload: JSONMap = {"action": action_ref.to_wire()}
        if attempt_id is not None:
            if not isinstance(attempt_id, str) or not attempt_id.strip():
                raise TypeError("attempt_id must be a nonempty string or None")
            payload["attempt_id"] = attempt_id
        response = self._request("act", **payload)
        apply = response.get("apply")
        if not isinstance(apply, Mapping):
            raise SimBridgeProtocolError("act response is missing apply result")
        state = self._bound_state(response, "act")
        return ActResult(
            casted=_wire_bool(apply, "casted"),
            consumes_decision=_wire_bool(apply, "consumes_decision"),
            finished=_wire_bool(apply, "finished"),
            needs_input=_wire_bool(apply, "needs_input"),
            state=state,
        )

    def cancel_queue(self) -> CancelQueueResult:
        """Cancel an accepted Heroic Strike/Cleave queue without casting."""

        response = self._request("cancel_queue")
        result = response.get("cancel_queue")
        if not isinstance(result, Mapping):
            raise SimBridgeProtocolError(
                "cancel_queue response is missing cancel_queue result"
            )
        state = self._bound_state(response, "cancel_queue")
        return CancelQueueResult(
            canceled=_wire_bool(result, "canceled"),
            consumes_decision=_wire_bool(result, "consumes_decision"),
            finished=_wire_bool(result, "finished"),
            needs_input=_wire_bool(result, "needs_input"),
            state=state,
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
        state = self._bound_state(response, "set_target")
        return SetTargetResult(
            changed=_wire_bool(result, "changed"),
            target_index=_wire_int(result, "target_index"),
            finished=_wire_bool(result, "finished"),
            needs_input=_wire_bool(result, "needs_input"),
            state=state,
        )

    def start_attack(self) -> Any:
        """Submit the source policy's idempotent auto-attack control."""

        result = _control_result_from_response(
            self._request("start_attack"), "start_attack"
        )
        self._validate_bound_state(result.state)
        return result

    def stop_cast(self) -> Any:
        """Cancel the current hardcast without treating it as a spell result."""

        result = _control_result_from_response(
            self._request("stop_cast"), "stop_cast"
        )
        self._validate_bound_state(result.state)
        return result

    def server_results_since_last_decision(
        self, attempt_ids: Sequence[str] = ()
    ) -> Any:
        """Return typed post-acceptance result evidence for accepted actions.

        Result-bearing attempts receive their immutable identifiers on the
        synchronous ``act`` submission.  Callers then send the complete ordered
        outstanding ledger here; the response partitions it into resolved
        events and still-pending identifiers without rebinding by position.
        """

        if isinstance(attempt_ids, (str, bytes)) or not isinstance(
            attempt_ids, Sequence
        ):
            raise TypeError("attempt_ids must be a sequence of strings")
        normalized = list(attempt_ids)
        if any(
            not isinstance(value, str) or not value.strip() for value in normalized
        ):
            raise TypeError("each attempt_id must be a nonempty string")
        if len(set(normalized)) != len(normalized):
            raise ValueError("attempt_ids must be unique")
        response = self._request("server_results", attempt_ids=normalized)
        result = response.get("server_results")
        if not isinstance(result, Mapping):
            raise SimBridgeProtocolError(
                "server_results response is missing server_results result"
            )
        if self._dynamic_binding is not None:
            generation, _, _ = self._dynamic_binding
            if _required_positive_wire_int(
                result, "environment_generation"
            ) != generation:
                raise SimBridgeProtocolError(
                    "server_results environment generation mismatch"
                )
        values = result.get("events")
        if not isinstance(values, list):
            raise SimBridgeProtocolError(
                "server_results result is missing the events array"
            )
        complete_through_time_ms = _required_nonnegative_wire_int(
            result, "complete_through_time_ms"
        )
        pending_values = result.get("pending_attempt_ids")
        if not isinstance(pending_values, list):
            raise SimBridgeProtocolError(
                "server_results result is missing the pending_attempt_ids array"
            )

        from .fury_full_policy_rollout_v2 import (
            ServerResultBatchV2,
            ServerResultEventV2,
            ServerTargetResultV2,
        )

        events = []
        for value in values:
            if not isinstance(value, Mapping):
                raise SimBridgeProtocolError(
                    "each server result event must be an object"
                )
            action_value = value.get("action")
            if not isinstance(action_value, Mapping):
                raise SimBridgeProtocolError(
                    "server result action must be an object"
                )
            action = _required_action_ref(action_value, "server result action")
            attempt_id = value.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id.strip():
                raise SimBridgeProtocolError(
                    "server result attempt_id must be a nonempty string"
                )
            damage = _required_nonnegative_wire_number(value, "damage")
            outcome = value.get("outcome")
            if not isinstance(outcome, str) or not outcome.strip():
                raise SimBridgeProtocolError(
                    "server result outcome must be a nonempty string"
                )
            if outcome not in SERVER_RESULT_OUTCOMES_V2:
                raise SimBridgeProtocolError(
                    f"unsupported server result outcome {outcome!r}"
                )
            target_values = value.get("target_results")
            if not isinstance(target_values, list):
                raise SimBridgeProtocolError(
                    "server result target_results must be an array"
                )
            target_results = []
            for target_value in target_values:
                if not isinstance(target_value, Mapping):
                    raise SimBridgeProtocolError(
                        "each server target result must be an object"
                    )
                target_index = _required_nonnegative_wire_int(
                    target_value, "target_index"
                )
                target_outcome = target_value.get("outcome")
                if not isinstance(target_outcome, str) or not target_outcome.strip():
                    raise SimBridgeProtocolError(
                        "server target result outcome must be a nonempty string"
                    )
                if target_outcome not in SERVER_TARGET_RESULT_OUTCOMES_V2:
                    raise SimBridgeProtocolError(
                        f"unsupported server target result outcome {target_outcome!r}"
                    )
                target_damage = _required_nonnegative_wire_number(
                    target_value, "damage"
                )
                target_results.append(
                    ServerTargetResultV2(
                        target_index=target_index,
                        outcome=target_outcome,
                        damage=target_damage,
                    )
                )
            time_ms = _required_nonnegative_wire_int(value, "time_ms")
            if time_ms > complete_through_time_ms:
                raise SimBridgeProtocolError(
                    "server result time_ms exceeds complete_through_time_ms"
                )
            try:
                event = ServerResultEventV2(
                    time_ms=time_ms,
                    outcome=outcome,
                    attempt_id=attempt_id,
                    damage=damage,
                    action=action,
                    target_results=tuple(target_results),
                )
            except (TypeError, ValueError) as error:
                raise SimBridgeProtocolError(str(error)) from error
            events.append(event)
        pending_attempt_ids: list[str] = []
        for attempt_id in pending_values:
            if not isinstance(attempt_id, str) or not attempt_id.strip():
                raise SimBridgeProtocolError(
                    "pending attempt_id must be a nonempty string"
                )
            pending_attempt_ids.append(attempt_id)
        _validate_attempt_partition(
            requested=normalized,
            resolved=[event.attempt_id for event in events],
            pending=pending_attempt_ids,
        )
        return ServerResultBatchV2(
            complete_through_time_ms=complete_through_time_ms,
            events=tuple(events),
            pending_attempt_ids=tuple(pending_attempt_ids),
        )

    def wait(self, wait_ms: int) -> JSONMap:
        """Advance with the bridge's explicit wait action."""

        duration = _strict_int(wait_ms, "wait_ms")
        if duration <= 0:
            raise ValueError("wait_ms must be positive")
        return self._bound_state(
            self._request("wait", wait_ms=duration), "wait"
        )

    def _bound_state(self, response: Mapping[str, Any], command: str) -> JSONMap:
        state = _response_state(response, command)
        self._validate_bound_state(state)
        return state

    def _validate_bound_state(self, state: Mapping[str, Any]) -> None:
        if self._dynamic_binding is None:
            return
        generation, _, config = self._dynamic_binding
        _validate_dynamic_state_binding_v1(
            state,
            generation=generation,
            config=config,
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
            # Repeated fresh-process replays otherwise retain the text pipe
            # objects until garbage collection even after the child exits.
            self._process.stdout.close()
            if self._process.stderr is not None:
                self._process.stderr.close()
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
        if (
            self._dynamic_binding is not None
            and command not in {"load", "load_dynamic_v1", "close"}
        ):
            expected_generation, _, _ = self._dynamic_binding
            observed_generation = _required_positive_wire_int(
                response, "environment_generation"
            )
            if observed_generation != expected_generation:
                raise SimBridgeProtocolError(
                    f"{command} response environment generation mismatch"
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


def _required_nonnegative_wire_int(
    value: Mapping[str, Any], field: str
) -> int:
    if field not in value:
        raise SimBridgeProtocolError(f"{field} is required")
    raw = value[field]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SimBridgeProtocolError(f"{field} must be an integer")
    if raw < 0:
        raise SimBridgeProtocolError(f"{field} must be non-negative")
    return raw


def _required_nonnegative_wire_number(
    value: Mapping[str, Any], field: str
) -> float:
    if field not in value:
        raise SimBridgeProtocolError(f"{field} is required")
    raw = value[field]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SimBridgeProtocolError(f"{field} must be numeric")
    try:
        result = float(raw)
    except (OverflowError, ValueError) as error:
        raise SimBridgeProtocolError(f"{field} must be finite") from error
    if not math.isfinite(result):
        raise SimBridgeProtocolError(f"{field} must be finite")
    if result < 0:
        raise SimBridgeProtocolError(f"{field} must be non-negative")
    return result


def _required_action_ref(value: Mapping[str, Any], label: str) -> ActionRef:
    allowed = ("spell_id", "item_id", "other_id", "tag")
    parsed: dict[str, int] = {}
    for field in allowed:
        raw = value.get(field, 0)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SimBridgeProtocolError(f"{label} {field} must be an integer")
        if raw < 0:
            raise SimBridgeProtocolError(f"{label} {field} must be non-negative")
        parsed[field] = raw
    primary_ids = sum(
        parsed[field] > 0 for field in ("spell_id", "item_id", "other_id")
    )
    if primary_ids != 1:
        raise SimBridgeProtocolError(
            f"{label} must contain exactly one nonzero primary ID"
        )
    return ActionRef(**parsed)


def _validate_attempt_partition(
    *, requested: Sequence[str], resolved: Sequence[str], pending: Sequence[str]
) -> None:
    resolved_set = set(resolved)
    pending_set = set(pending)
    if len(resolved_set) != len(resolved) or len(pending_set) != len(pending):
        raise SimBridgeProtocolError(
            "resolved and pending attempt IDs must each be unique"
        )
    if resolved_set & pending_set:
        raise SimBridgeProtocolError(
            "resolved and pending attempt IDs must not overlap"
        )
    requested_set = set(requested)
    if resolved_set | pending_set != requested_set:
        raise SimBridgeProtocolError(
            "resolved and pending attempt IDs must partition the requested ledger"
        )
    expected_resolved = [value for value in requested if value in resolved_set]
    expected_pending = [value for value in requested if value in pending_set]
    if list(resolved) != expected_resolved or list(pending) != expected_pending:
        raise SimBridgeProtocolError(
            "resolved and pending attempt IDs must preserve requested ledger order"
        )


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _strict_positive_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must be finite and positive") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


def _required_positive_wire_int(value: Mapping[str, Any], field: str) -> int:
    result = _required_nonnegative_wire_int(value, field)
    if result == 0:
        raise SimBridgeProtocolError(f"{field} must be positive")
    return result


def _required_wire_text(value: Mapping[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise SimBridgeProtocolError(f"{field} must be a nonempty string")
    return raw


def _dynamic_config_digest_v1(config: DynamicTeamBackgroundConfigV1) -> str:
    document = {
        "background_damage_events": [
            {
                "damage_ieee754": struct.pack(">d", event.damage).hex(),
                "event_id": event.event_id,
                "schedule_index": event.schedule_index,
                "target_index": event.target_index,
                "time_ms": event.time_ms,
            }
            for event in config.background_damage_events
        ],
        "retarget_mode": config.retarget_mode,
        "same_timestamp_order": config.same_timestamp_order,
        "schema": DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1,
        "target_health": [
            {
                "health_ieee754": struct.pack(">d", target.health).hex(),
                "target_index": target.target_index,
            }
            for target in config.target_health
        ],
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dynamic_load_receipt_v1(
    value: Mapping[str, Any], config: DynamicTeamBackgroundConfigV1
) -> DynamicLoadReceiptV1:
    schema = _required_wire_text(value, "schema")
    if schema != DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1:
        raise SimBridgeProtocolError("dynamic_load schema is unsupported")
    config_digest = _required_wire_text(value, "config_digest")
    if config_digest != config.content_sha256:
        raise SimBridgeProtocolError("dynamic_load config_digest mismatch")
    generation = _required_positive_wire_int(value, "environment_generation")
    target_count = _required_nonnegative_wire_int(value, "target_count")
    if target_count != len(config.target_health):
        raise SimBridgeProtocolError("dynamic_load target_count mismatch")
    event_count = _required_nonnegative_wire_int(value, "background_event_count")
    if event_count != len(config.background_damage_events):
        raise SimBridgeProtocolError("dynamic_load background_event_count mismatch")
    same_timestamp_order = _required_wire_text(value, "same_timestamp_order")
    if same_timestamp_order != config.same_timestamp_order:
        raise SimBridgeProtocolError("dynamic_load same_timestamp_order mismatch")
    retarget_mode = _required_wire_text(value, "retarget_mode")
    if retarget_mode != config.retarget_mode:
        raise SimBridgeProtocolError("dynamic_load retarget_mode mismatch")
    return DynamicLoadReceiptV1(
        schema=schema,
        config_digest=config_digest,
        environment_generation=generation,
        target_count=target_count,
        background_event_count=event_count,
        same_timestamp_order=same_timestamp_order,
        retarget_mode=retarget_mode,
    )


def _validate_dynamic_state_binding_v1(
    state: Mapping[str, Any],
    *,
    generation: int,
    config: DynamicTeamBackgroundConfigV1,
) -> None:
    config_digest = config.content_sha256
    expected_target_count = len(config.target_health)
    raw = state.get("dynamic_team_background")
    if not isinstance(raw, Mapping):
        raise SimBridgeProtocolError(
            "dynamic state is missing dynamic_team_background"
        )
    if _required_wire_text(raw, "schema") != DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1:
        raise SimBridgeProtocolError("dynamic state schema is unsupported")
    if _required_wire_text(raw, "config_digest") != config_digest:
        raise SimBridgeProtocolError("dynamic state config_digest mismatch")
    if _required_positive_wire_int(raw, "environment_generation") != generation:
        raise SimBridgeProtocolError("dynamic state environment generation mismatch")

    accounting_fields = {
        "same_timestamp_order",
        "retarget_mode",
        "retarget_required",
        "simulated_damage_applied",
        "background_damage_applied",
        "combined_damage_applied",
        "background_events_processed",
        "background_events_total",
        "background_events_canceled",
        "candidate_events_processed",
        "candidate_events_canceled",
        "damage_applications_total",
        "targets",
    }
    missing = accounting_fields - set(raw)
    if missing:
        raise SimBridgeProtocolError(
            "dynamic state is missing complete lifecycle accounting fields: "
            + ", ".join(sorted(missing))
        )
    if _required_wire_text(raw, "same_timestamp_order") != DYNAMIC_SAME_TIMESTAMP_ORDER_V1:
        raise SimBridgeProtocolError("dynamic state same_timestamp_order is unsupported")
    if _required_wire_text(raw, "retarget_mode") != config.retarget_mode:
        raise SimBridgeProtocolError("dynamic state retarget_mode differs from config")
    if not isinstance(raw.get("retarget_required"), bool):
        raise SimBridgeProtocolError("dynamic state retarget_required must be boolean")
    background_processed = _required_nonnegative_wire_int(
        raw, "background_events_processed"
    )
    background_total = _required_nonnegative_wire_int(raw, "background_events_total")
    if background_total != len(config.background_damage_events):
        raise SimBridgeProtocolError(
            "dynamic state background event count differs from config"
        )
    background_canceled = _required_nonnegative_wire_int(
        raw, "background_events_canceled"
    )
    candidate_processed = _required_nonnegative_wire_int(
        raw, "candidate_events_processed"
    )
    candidate_canceled = _required_nonnegative_wire_int(
        raw, "candidate_events_canceled"
    )
    damage_applications = _required_nonnegative_wire_int(
        raw, "damage_applications_total"
    )
    if (
        background_processed > background_total
        or background_canceled > background_processed
    ):
        raise SimBridgeProtocolError("dynamic background event counters are inconsistent")
    if candidate_canceled > candidate_processed:
        raise SimBridgeProtocolError("dynamic candidate event counters are inconsistent")
    if (
        damage_applications < candidate_processed
        or damage_applications > candidate_processed + background_processed
    ):
        raise SimBridgeProtocolError("dynamic damage application counter is inconsistent")
    simulated = _required_nonnegative_wire_number(
        raw, "simulated_damage_applied"
    )
    background = _required_nonnegative_wire_number(
        raw, "background_damage_applied"
    )
    combined = _required_nonnegative_wire_number(raw, "combined_damage_applied")
    if not _numbers_conserve_v1(combined, simulated + background):
        raise SimBridgeProtocolError(
            "dynamic state combined damage does not equal simulated plus background"
        )
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise SimBridgeProtocolError("dynamic state targets must be a nonempty array")
    if len(targets) != expected_target_count:
        raise SimBridgeProtocolError("dynamic state target count differs from config")
    total_target_count = _required_nonnegative_wire_int(state, "total_target_count")
    if total_target_count != expected_target_count:
        raise SimBridgeProtocolError(
            "dynamic state total_target_count differs from config"
        )
    selected_target_index = _required_nonnegative_wire_int(state, "target_index")
    if selected_target_index >= expected_target_count:
        raise SimBridgeProtocolError("dynamic state target_index is out of range")
    state_time_ms = _required_nonnegative_wire_int(state, "time_ms")
    target_simulated = 0.0
    target_background = 0.0
    target_remaining = 0.0
    target_initial = 0.0
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            raise SimBridgeProtocolError("each dynamic target state must be an object")
        if _required_nonnegative_wire_int(target, "target_index") != index:
            raise SimBridgeProtocolError(
                "dynamic target states must cover indexes in ascending order"
            )
        initial = _required_nonnegative_wire_number(target, "initial_health")
        if struct.pack(">d", initial) != struct.pack(
            ">d", config.target_health[index].health
        ):
            raise SimBridgeProtocolError(
                "dynamic target initial_health IEEE-754 bits differ from config"
            )
        current = _required_nonnegative_wire_number(target, "current_health")
        simulated_target = _required_nonnegative_wire_number(
            target, "simulated_damage_applied"
        )
        background_target = _required_nonnegative_wire_number(
            target, "background_damage_applied"
        )
        if initial <= 0:
            raise SimBridgeProtocolError(
                "dynamic target initial_health must be positive"
            )
        if not _numbers_conserve_v1(
            initial, current + simulated_target + background_target
        ):
            raise SimBridgeProtocolError(
                "dynamic target health does not conserve applied damage"
            )
        dead = target.get("dead")
        if not isinstance(dead, bool) or dead != (current == 0):
            raise SimBridgeProtocolError(
                "dynamic target dead flag differs from current_health"
            )
        if dead:
            death_time_ms = _required_nonnegative_wire_int(target, "death_time_ms")
            if death_time_ms > state_time_ms:
                raise SimBridgeProtocolError(
                    "dynamic target death_time_ms exceeds current state time"
                )
        elif "death_time_ms" in target:
            raise SimBridgeProtocolError(
                "alive dynamic target must not expose death_time_ms"
            )
        target_initial += initial
        target_remaining += current
        target_simulated += simulated_target
        target_background += background_target
    live_target_count = sum(
        1 for target in targets if isinstance(target, Mapping) and not target["dead"]
    )
    if _required_nonnegative_wire_int(state, "num_targets") != live_target_count:
        raise SimBridgeProtocolError(
            "dynamic state num_targets differs from live target count"
        )
    if not _numbers_conserve_v1(simulated, target_simulated):
        raise SimBridgeProtocolError(
            "dynamic state simulated total differs from target rows"
        )
    if not _numbers_conserve_v1(background, target_background):
        raise SimBridgeProtocolError(
            "dynamic state background total differs from target rows"
        )
    if "encounter_damage_taken" in state and not _numbers_conserve_v1(
        _required_nonnegative_wire_number(state, "encounter_damage_taken"), combined
    ):
        raise SimBridgeProtocolError(
            "dynamic state encounter damage differs from combined applied damage"
        )
    if "encounter_health_target" in state and not _numbers_conserve_v1(
        _required_nonnegative_wire_number(state, "encounter_health_target"),
        target_initial,
    ):
        raise SimBridgeProtocolError(
            "dynamic state encounter health differs from target initial health"
        )
    if not _numbers_conserve_v1(target_initial, target_remaining + combined):
        raise SimBridgeProtocolError(
            "dynamic state global target health does not conserve"
        )


def _dynamic_damage_receipt_v1(
    value: Mapping[str, Any], *, target_count: int
) -> DynamicDamageReceiptV1:
    schedule_index = _required_nonnegative_wire_int(value, "schedule_index")
    event_id = _required_wire_text(value, "event_id")
    if not _DYNAMIC_EVENT_ID_PATTERN_V1.fullmatch(event_id):
        raise SimBridgeProtocolError("dynamic receipt event_id is invalid")
    time_ms = _required_nonnegative_wire_int(value, "time_ms")
    target_index = _required_nonnegative_wire_int(value, "target_index")
    if target_index >= target_count:
        raise SimBridgeProtocolError("dynamic receipt target_index is out of range")
    requested = _required_nonnegative_wire_number(value, "requested_damage")
    applied = _required_nonnegative_wire_number(value, "applied_damage")
    overkill = _required_nonnegative_wire_number(value, "overkill_damage")
    killed = _wire_bool(value, "killed")
    status = _required_wire_text(value, "status")
    if status not in DYNAMIC_DAMAGE_RECEIPT_STATUSES_V1:
        raise SimBridgeProtocolError(
            f"unsupported dynamic damage receipt status {status!r}"
        )
    if not _numbers_conserve_v1(requested, applied + overkill):
        raise SimBridgeProtocolError(
            "dynamic receipt requested damage does not equal applied plus overkill"
        )
    if status == "CANCELED_TARGET_DEAD" and (applied != 0 or killed):
        raise SimBridgeProtocolError(
            "canceled dynamic receipt must have zero applied damage and killed=false"
        )
    raw_ordinal = value.get("damage_ordinal")
    damage_ordinal = None
    if raw_ordinal is not None:
        if isinstance(raw_ordinal, bool) or not isinstance(raw_ordinal, int) or raw_ordinal <= 0:
            raise SimBridgeProtocolError("damage_ordinal must be a positive integer")
        damage_ordinal = raw_ordinal
    if status == "APPLIED" and (applied <= 0 or damage_ordinal is None):
        raise SimBridgeProtocolError(
            "applied dynamic receipt requires positive damage and damage_ordinal"
        )
    raw_retargeted = value.get("retargeted_to")
    retargeted_to = None
    if raw_retargeted is not None:
        if isinstance(raw_retargeted, bool) or not isinstance(raw_retargeted, int):
            raise SimBridgeProtocolError("retargeted_to must be an integer")
        if raw_retargeted < 0:
            raise SimBridgeProtocolError("retargeted_to must be non-negative")
        if raw_retargeted >= target_count:
            raise SimBridgeProtocolError("retargeted_to is out of range")
        if not killed:
            raise SimBridgeProtocolError("only a killing dynamic receipt may retarget")
        retargeted_to = raw_retargeted
    return DynamicDamageReceiptV1(
        schedule_index=schedule_index,
        event_id=event_id,
        time_ms=time_ms,
        target_index=target_index,
        requested_damage=requested,
        applied_damage=applied,
        overkill_damage=overkill,
        killed=killed,
        status=status,
        damage_ordinal=damage_ordinal,
        retargeted_to=retargeted_to,
    )


def _dynamic_damage_receipt_batch_v1(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config_digest: str,
    config: DynamicTeamBackgroundConfigV1,
) -> DynamicDamageReceiptBatchV1:
    target_count = len(config.target_health)
    event_count = len(config.background_damage_events)
    schema = _required_wire_text(value, "schema")
    if schema != DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1:
        raise SimBridgeProtocolError("dynamic receipt batch schema is unsupported")
    if _required_wire_text(value, "config_digest") != config_digest:
        raise SimBridgeProtocolError("dynamic receipt batch config_digest mismatch")
    if _required_positive_wire_int(value, "environment_generation") != generation:
        raise SimBridgeProtocolError(
            "dynamic receipt batch environment generation mismatch"
        )
    cursor = _required_nonnegative_wire_int(value, "cursor")
    if cursor != requested_cursor:
        raise SimBridgeProtocolError("dynamic receipt batch cursor mismatch")
    if cursor > event_count:
        raise SimBridgeProtocolError("dynamic receipt batch cursor exceeds config")
    next_cursor = _required_nonnegative_wire_int(value, "next_cursor")
    if next_cursor < cursor:
        raise SimBridgeProtocolError(
            "dynamic receipt batch next_cursor precedes cursor"
        )
    if next_cursor > event_count:
        raise SimBridgeProtocolError("dynamic receipt batch next_cursor exceeds config")
    schedule_complete = _wire_bool(value, "schedule_complete")
    raw_receipts = value.get("receipts")
    if not isinstance(raw_receipts, list):
        raise SimBridgeProtocolError("dynamic receipt batch receipts must be an array")
    receipts: list[DynamicDamageReceiptV1] = []
    seen_ids: set[str] = set()
    previous_time_ms = -1
    previous_ordinal = 0
    for offset, raw_receipt in enumerate(raw_receipts):
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError("each dynamic receipt must be an object")
        receipt = _dynamic_damage_receipt_v1(
            raw_receipt, target_count=target_count
        )
        if receipt.schedule_index != cursor + offset:
            raise SimBridgeProtocolError(
                "dynamic receipts do not preserve contiguous schedule order"
            )
        if receipt.schedule_index >= event_count:
            raise SimBridgeProtocolError(
                "dynamic receipt schedule_index exceeds config"
            )
        expected_event = config.background_damage_events[receipt.schedule_index]
        if (
            receipt.event_id != expected_event.event_id
            or receipt.time_ms != expected_event.time_ms
            or receipt.target_index != expected_event.target_index
            or struct.pack(">d", receipt.requested_damage)
            != struct.pack(">d", expected_event.damage)
        ):
            raise SimBridgeProtocolError(
                "dynamic receipt does not match its config schedule row"
            )
        if receipt.time_ms < previous_time_ms:
            raise SimBridgeProtocolError(
                "dynamic receipts do not preserve (time_ms,schedule_index) order"
            )
        previous_time_ms = receipt.time_ms
        if receipt.damage_ordinal is not None:
            if receipt.damage_ordinal <= previous_ordinal:
                raise SimBridgeProtocolError(
                    "dynamic receipts do not preserve global damage ordinal"
                )
            previous_ordinal = receipt.damage_ordinal
        if receipt.event_id in seen_ids:
            raise SimBridgeProtocolError(
                "dynamic receipt batch contains duplicate event_id"
            )
        seen_ids.add(receipt.event_id)
        receipts.append(receipt)
    if len(receipts) != next_cursor - cursor:
        raise SimBridgeProtocolError(
            "dynamic receipt count does not close cursor interval"
        )
    return DynamicDamageReceiptBatchV1(
        schema=schema,
        config_digest=config_digest,
        environment_generation=generation,
        cursor=cursor,
        next_cursor=next_cursor,
        schedule_complete=schedule_complete,
        receipts=tuple(receipts),
    )


def _dynamic_candidate_damage_receipt_v1(
    value: Mapping[str, Any],
    *,
    target_count: int,
) -> DynamicCandidateDamageReceiptV1:
    damage_ordinal = _required_positive_wire_int(value, "damage_ordinal")
    time_ms = _required_nonnegative_wire_int(value, "time_ms")
    target_index = _required_nonnegative_wire_int(value, "target_index")
    if target_index >= target_count:
        raise SimBridgeProtocolError(
            "candidate receipt target_index is out of range"
        )
    requested = _required_nonnegative_wire_number(value, "requested_damage")
    applied = _required_nonnegative_wire_number(value, "applied_damage")
    overkill = _required_nonnegative_wire_number(value, "overkill_damage")
    if not _numbers_conserve_v1(requested, applied + overkill):
        raise SimBridgeProtocolError(
            "candidate receipt requested damage does not equal applied plus overkill"
        )
    killed = _wire_bool(value, "killed")
    status = _required_wire_text(value, "status")
    if status not in DYNAMIC_CANDIDATE_DAMAGE_RECEIPT_STATUSES_V1:
        raise SimBridgeProtocolError(
            f"unsupported candidate damage receipt status {status!r}"
        )
    if status == "APPLIED" and applied <= 0:
        raise SimBridgeProtocolError(
            "applied candidate receipt must apply positive damage"
        )
    if status == "NO_DAMAGE" and (applied != 0 or requested != 0 or killed):
        raise SimBridgeProtocolError(
            "no-damage candidate receipt must be a zero application"
        )
    if status == "CANCELED_TARGET_DEAD" and (applied != 0 or killed):
        raise SimBridgeProtocolError(
            "canceled candidate receipt must have zero applied damage and killed=false"
        )
    if killed and applied <= 0:
        raise SimBridgeProtocolError(
            "killing candidate receipt must apply positive damage"
        )
    raw_action = value.get("action")
    if not isinstance(raw_action, Mapping):
        raise SimBridgeProtocolError("candidate receipt action must be an object")
    action = ActionRef.from_wire(raw_action)
    outcome = _required_wire_text(value, "outcome")
    if outcome not in DYNAMIC_CANDIDATE_DAMAGE_OUTCOMES_V1:
        raise SimBridgeProtocolError(
            f"unsupported candidate damage outcome {outcome!r}"
        )
    execution_id = _required_nonnegative_wire_int(value, "execution_id")
    execution_index = _required_nonnegative_wire_int(value, "execution_index")
    landed_execution_index = _required_nonnegative_wire_int(
        value, "landed_execution_index"
    )
    resolution_phase = _required_wire_text(value, "resolution_phase")
    if resolution_phase not in DYNAMIC_CANDIDATE_RESOLUTION_PHASES_V1:
        raise SimBridgeProtocolError(
            f"unsupported candidate resolution_phase {resolution_phase!r}"
        )
    outcome_computed = _wire_bool(value, "outcome_computed")
    random_stream_rewound = _wire_bool(value, "random_stream_rewound")
    if random_stream_rewound:
        raise SimBridgeProtocolError(
            "dynamic candidate random streams must never claim rollback"
        )
    expected_phase_by_status = {
        "APPLIED": "APPLIED_AFTER_OUTCOME",
        "NO_DAMAGE": "NO_DAMAGE_AFTER_OUTCOME",
    }
    if status in expected_phase_by_status and (
        resolution_phase != expected_phase_by_status[status] or not outcome_computed
    ):
        raise SimBridgeProtocolError(
            "candidate receipt phase/outcome boundary differs from status"
        )
    if status == "CANCELED_TARGET_DEAD":
        expected_canceled_phase = (
            "TARGET_DIED_AFTER_OUTCOME"
            if outcome_computed
            else "TARGET_DEAD_BEFORE_OUTCOME"
        )
        if resolution_phase != expected_canceled_phase:
            raise SimBridgeProtocolError(
                "canceled candidate receipt phase differs from outcome boundary"
            )
    if status == "CANCELED_TARGET_DEAD" and (
        outcome != "CANCELED" or landed_execution_index != 0
    ):
        raise SimBridgeProtocolError(
            "canceled candidate receipt must have CANCELED outcome and no landed index"
        )
    raw_attempt_id = value.get("attempt_id")
    attempt_id = None
    if raw_attempt_id is not None:
        if not isinstance(raw_attempt_id, str) or not raw_attempt_id:
            raise SimBridgeProtocolError("candidate receipt attempt_id must be nonempty")
        attempt_id = raw_attempt_id
    raw_retargeted = value.get("retargeted_to")
    retargeted_to = None
    if raw_retargeted is not None:
        if (
            isinstance(raw_retargeted, bool)
            or not isinstance(raw_retargeted, int)
            or raw_retargeted < 0
        ):
            raise SimBridgeProtocolError(
                "retargeted_to must be a non-negative integer"
            )
        if not killed:
            raise SimBridgeProtocolError("only a killing candidate receipt may retarget")
        if raw_retargeted >= target_count:
            raise SimBridgeProtocolError("retargeted_to is out of range")
        retargeted_to = raw_retargeted
    return DynamicCandidateDamageReceiptV1(
        damage_ordinal=damage_ordinal,
        time_ms=time_ms,
        target_index=target_index,
        requested_damage=requested,
        applied_damage=applied,
        overkill_damage=overkill,
        killed=killed,
        status=status,
        action=action,
        outcome=outcome,
        execution_id=execution_id,
        execution_index=execution_index,
        landed_execution_index=landed_execution_index,
        resolution_phase=resolution_phase,
        outcome_computed=outcome_computed,
        random_stream_rewound=random_stream_rewound,
        attempt_id=attempt_id,
        retargeted_to=retargeted_to,
    )


def _dynamic_candidate_damage_receipt_batch_v1(
    value: Mapping[str, Any],
    *,
    requested_cursor: int,
    generation: int,
    config_digest: str,
    target_count: int,
) -> DynamicCandidateDamageReceiptBatchV1:
    schema = _required_wire_text(value, "schema")
    if schema != DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1:
        raise SimBridgeProtocolError("candidate receipt batch schema is unsupported")
    if _required_wire_text(value, "config_digest") != config_digest:
        raise SimBridgeProtocolError("candidate receipt batch config_digest mismatch")
    if (
        _required_positive_wire_int(value, "environment_generation")
        != generation
    ):
        raise SimBridgeProtocolError(
            "candidate receipt batch environment generation mismatch"
        )
    cursor = _required_nonnegative_wire_int(value, "cursor")
    if cursor != requested_cursor:
        raise SimBridgeProtocolError("candidate receipt batch cursor mismatch")
    next_cursor = _required_nonnegative_wire_int(value, "next_cursor")
    if next_cursor < cursor:
        raise SimBridgeProtocolError(
            "candidate receipt batch next_cursor precedes cursor"
        )
    raw_receipts = value.get("receipts")
    if not isinstance(raw_receipts, list):
        raise SimBridgeProtocolError(
            "candidate receipt batch receipts must be an array"
        )
    receipts: list[DynamicCandidateDamageReceiptV1] = []
    previous_ordinal = 0
    previous_time_ms = -1
    for raw_receipt in raw_receipts:
        if not isinstance(raw_receipt, Mapping):
            raise SimBridgeProtocolError("each candidate receipt must be an object")
        receipt = _dynamic_candidate_damage_receipt_v1(
            raw_receipt, target_count=target_count
        )
        if receipt.damage_ordinal <= previous_ordinal:
            raise SimBridgeProtocolError(
                "candidate receipts do not preserve global damage ordinal"
            )
        if receipt.time_ms < previous_time_ms:
            raise SimBridgeProtocolError(
                "candidate receipts do not preserve time order"
            )
        previous_ordinal = receipt.damage_ordinal
        previous_time_ms = receipt.time_ms
        receipts.append(receipt)
    if len(receipts) != next_cursor - cursor:
        raise SimBridgeProtocolError(
            "candidate receipt count does not close cursor interval"
        )
    return DynamicCandidateDamageReceiptBatchV1(
        schema=schema,
        config_digest=config_digest,
        environment_generation=generation,
        cursor=cursor,
        next_cursor=next_cursor,
        receipts=tuple(receipts),
    )


def _numbers_conserve_v1(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9)


__all__: Sequence[str] = (
    "ActionRef",
    "ActResult",
    "AvailableAction",
    "BackgroundDamageEventV1",
    "CancelQueueResult",
    "DYNAMIC_CANDIDATE_DAMAGE_OUTCOMES_V1",
    "DYNAMIC_CANDIDATE_DAMAGE_RECEIPT_STATUSES_V1",
    "DYNAMIC_CANDIDATE_RESOLUTION_PHASES_V1",
    "DYNAMIC_DAMAGE_RECEIPT_STATUSES_V1",
    "DYNAMIC_RETARGET_MODES_V1",
    "DYNAMIC_SAME_TIMESTAMP_ORDER_V1",
    "DYNAMIC_TEAM_BACKGROUND_SCHEMA_V1",
    "DynamicDamageReceiptBatchV1",
    "DynamicDamageReceiptV1",
    "DynamicCandidateDamageReceiptBatchV1",
    "DynamicCandidateDamageReceiptV1",
    "DynamicLoadReceiptV1",
    "DynamicLoadResultV1",
    "DynamicTargetHealthV1",
    "DynamicTeamBackgroundConfigV1",
    "SetTargetResult",
    "SERVER_RESULT_OUTCOMES_V2",
    "SERVER_TARGET_RESULT_OUTCOMES_V2",
    "SimBridgeCommandError",
    "SimBridgeError",
    "SimBridgeProcessError",
    "SimBridgeProtocolError",
    "SimulatorBridge",
)
