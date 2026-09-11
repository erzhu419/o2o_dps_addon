"""Exact Windows/WSL dynamic-bridge trace equivalence for v4 binaries.

The harness materializes one two-target, health-bound scenario and executes the
same fixed command stream against independent Windows and WSL/Linux bridge
processes.  It compares every raw JSONL request/response plus every typed
result and state.  Only the capture-level platform/binary launch metadata is
removed before comparison.

A PASS is deliberately narrow: it proves exact behavior for this request,
seed, trace contract, and binary pair.  It is not simulator validation, policy
fidelity, a scientific comparison, or HPC readiness.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .expert_proposals import ACTION_KEY_TO_REF
from .sim_bridge import (
    ActionRef,
    AvailableAction,
    BackgroundDamageEventV1,
    DynamicCandidateDamageReceiptBatchV1,
    DynamicDamageReceiptBatchV1,
    DynamicLoadResultV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
    SimulatorBridge,
)


JSONMap = dict[str, Any]

SCHEMA = "fury_bridge_platform_equivalence/v2"
CAPTURE_SCHEMA = "fury_bridge_platform_equivalence_capture/v2"
INCOMING_RECEIPT_SCHEMA = "fury_bridge_incoming_state_delta_receipt/v1"
EVIDENCE_SCOPE = "FIXED_DYNAMIC_REQUEST_SEED_FULL_JSONL_TRACE_ONLY"
ACTION_RULE = "wait250_then_exact_bloodthirst_on_auto_retargeted_target1"

EXPECTED_WINDOWS_SHA256 = (
    "3a330bf9b48d0032e3881d458943b758a32fc64e8976dfee106d0e5742e7e55e"
)
EXPECTED_LINUX_SHA256 = (
    "93015dd74ce436c171b50b41d9b2059249c6dbbea64614c1a472c9bda2ad838b"
)

TARGET_HEALTH = (1.0, 100_000.0)
SAME_TIMESTAMP_MS = 10
POST_DEATH_EVENT_MS = 20
WAIT_TO_MS = 250
STARTING_RAGE = 50.0
TARGET_ONE_SWING_SECONDS = 0.2
TARGET_ONE_BASE_DAMAGE = 200.0
HEALTH_STAT_INDEX = 34
ATTEMPT_ID = "platform-equivalence-v2:bloodthirst:0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUEST = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_phase1.json"
DEFAULT_WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v4.withdb.goamd64v1.windows-amd64.exe"
)
DEFAULT_LINUX_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v4.withdb.goamd64v1.linux-amd64"
)
DEFAULT_SIMULATOR_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
DEFAULT_RECEIPT_DIRECTORY = PROJECT_ROOT / ".hpc-local" / "receipts"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WIRE_COMMANDS = (
    "load_dynamic_v1",
    "wait",
    "advance",
    "dynamic_damage_receipts",
    "dynamic_candidate_damage_receipts",
    "state",
    "actions",
    "act",
    "server_results",
    "dynamic_candidate_damage_receipts",
    "dynamic_damage_receipts",
    "state",
    "close",
)
_VALIDATED_COMMANDS = (
    "load_dynamic_v1",
    "wait",
    "advance",
    "incoming_damage_receipt",
    "dynamic_damage_receipts",
    "dynamic_candidate_damage_receipts",
    "state_post_lifecycle",
    "actions",
    "act",
    "server_results",
    "dynamic_candidate_damage_receipts",
    "dynamic_damage_receipts",
    "state_final",
    "close",
)


class FuryBridgePlatformEquivalenceV2Error(RuntimeError):
    """A launch, input, dynamic invariant, or comparison contract failed."""


@dataclass(frozen=True)
class FileIdentityV2:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class IncomingStateDeltaReceiptV1:
    schema: str
    start_time_ms: int
    end_time_ms: int
    health_before: float
    health_after: float
    health_delta: float
    rage_before: float
    rage_after: float
    rage_delta: float
    source: str


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"value is not finite canonical JSON: {error}"
        ) from error


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(canonical_bytes(value))
    except FuryBridgePlatformEquivalenceV2Error as error:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} is not strict JSON: {error}"
        ) from error


def snapshot_file(path: Path) -> tuple[FileIdentityV2, bytes]:
    resolved = path.expanduser().resolve()
    try:
        value = resolved.read_bytes()
    except OSError as error:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"could not read {resolved}: {error}"
        ) from error
    return (
        FileIdentityV2(
            path=str(resolved),
            size_bytes=len(value),
            sha256=sha256_bytes(value),
        ),
        value,
    )


def load_request(path: Path) -> tuple[FileIdentityV2, JSONMap]:
    identity, value = snapshot_file(path)
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"request is not valid JSON: {identity.path}: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise FuryBridgePlatformEquivalenceV2Error("request JSON must be an object")
    return identity, decoded


def windows_path_to_wsl(path: Path) -> str:
    resolved = path.expanduser().resolve()
    drive = resolved.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise FuryBridgePlatformEquivalenceV2Error(
            f"WSL launch requires a drive-letter path, got {resolved}"
        )
    posix = resolved.as_posix()
    suffix = posix[3:] if len(posix) > 3 else ""
    return f"/mnt/{drive[0].lower()}/{suffix}"


def linux_wsl_arguments(
    *, distro: str, simulator_root: Path, linux_bridge: Path
) -> tuple[str, ...]:
    if not isinstance(distro, str) or not distro.strip():
        raise FuryBridgePlatformEquivalenceV2Error("WSL distro must be nonempty")
    return (
        "-d",
        distro,
        "--cd",
        windows_path_to_wsl(simulator_root),
        "--",
        windows_path_to_wsl(linux_bridge),
    )


class RecordingSimulatorBridgeV2(SimulatorBridge):
    """SimulatorBridge that retains the exact validated JSONL wire exchange."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._wire_trace_v2: list[JSONMap] = []
        super().__init__(*args, **kwargs)

    def _request(self, command: str, **payload: Any) -> JSONMap:
        request = {"command": command, **payload}
        response = super()._request(command, **payload)
        self._wire_trace_v2.append(
            {
                "request": _strict_json_copy(request, f"{command} wire request"),
                "response": _strict_json_copy(response, f"{command} wire response"),
            }
        )
        return response

    @property
    def wire_trace_v2(self) -> list[JSONMap]:
        return copy.deepcopy(self._wire_trace_v2)


def build_dynamic_fixture_v2(
    base_request: Mapping[str, Any],
) -> tuple[JSONMap, DynamicTeamBackgroundConfigV1]:
    """Materialize the fixed two-target cross-platform trace fixture."""

    if not isinstance(base_request, Mapping):
        raise TypeError("base_request must be a mapping")
    request = _strict_json_copy(base_request, "base request")
    raid = _mapping(request.get("raid"), "request.raid")
    parties = _list(raid.get("parties"), "request.raid.parties")
    if not parties:
        raise FuryBridgePlatformEquivalenceV2Error("request has no party")
    party = _mapping(parties[0], "request.raid.parties[0]")
    players = _list(party.get("players"), "request.raid.parties[0].players")
    if not players:
        raise FuryBridgePlatformEquivalenceV2Error("request has no player")
    player = _mapping(players[0], "request player")

    equipment = _mapping(player.get("equipment"), "request player equipment")
    items = _list(equipment.get("items"), "request player equipment.items")
    if len(items) <= 15:
        raise FuryBridgePlatformEquivalenceV2Error(
            "fixture requires an explicit off-hand equipment slot"
        )
    items[15] = {}
    warrior = _mapping(player.get("warrior"), "request player warrior")
    options = _mapping(warrior.get("options"), "request player warrior.options")
    options["startingRage"] = STARTING_RAGE
    player["healingModel"] = {"hps": 0, "cadenceSeconds": 10}
    player["distanceFromTarget"] = 5
    raid["tanks"] = [{"type": "Player", "index": 0}]

    encounter = _mapping(request.get("encounter"), "request.encounter")
    source_targets = _list(encounter.get("targets"), "request.encounter.targets")
    if not source_targets:
        raise FuryBridgePlatformEquivalenceV2Error("request has no source target")
    source = _mapping(source_targets[0], "request.encounter.targets[0]")
    targets: list[JSONMap] = []
    for index, health in enumerate(TARGET_HEALTH):
        target = copy.deepcopy(source)
        stats = _list(target.get("stats"), f"target {index} stats")
        if len(stats) <= HEALTH_STAT_INDEX:
            stats.extend(0 for _ in range(HEALTH_STAT_INDEX + 1 - len(stats)))
        stats[HEALTH_STAT_INDEX] = health
        target["name"] = f"O2O platform-equivalence V2 target {index}"
        target["stats"] = stats
        target["swingSpeed"] = 0 if index == 0 else TARGET_ONE_SWING_SECONDS
        target["minBaseDamage"] = 0 if index == 0 else TARGET_ONE_BASE_DAMAGE
        target["damageSpread"] = 0
        target["parryHaste"] = False
        targets.append(target)
    encounter["targets"] = targets
    encounter["useHealth"] = True

    sim_options = _mapping(request.get("simOptions"), "request.simOptions")
    sim_options["iterations"] = 1
    sim_options["interactive"] = True

    config = DynamicTeamBackgroundConfigV1(
        target_health=tuple(
            DynamicTargetHealthV1(index, health)
            for index, health in enumerate(TARGET_HEALTH)
        ),
        background_damage_events=(
            BackgroundDamageEventV1(
                0,
                SAME_TIMESTAMP_MS,
                0,
                "same-ms-background-kill",
                TARGET_HEALTH[0],
            ),
            BackgroundDamageEventV1(
                1,
                POST_DEATH_EVENT_MS,
                0,
                "post-death-background-cancel",
                3.0,
            ),
        ),
        retarget_mode="NEXT_ALIVE_CYCLIC",
    )
    request = _strict_json_copy(request, "materialized dynamic request")
    request_targets = request["encounter"]["targets"]
    for index, target_health in enumerate(config.target_health):
        observed = _finite_number(
            request_targets[index]["stats"][HEALTH_STAT_INDEX],
            f"materialized target {index} health",
        )
        if struct.pack(">d", observed) != struct.pack(">d", target_health.health):
            raise FuryBridgePlatformEquivalenceV2Error(
                f"materialized target {index} health bits differ from config"
            )
    return request, config


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _validate_dynamic_state(
    state: Mapping[str, Any],
    config: DynamicTeamBackgroundConfigV1,
    generation: int,
    *,
    label: str,
) -> Mapping[str, Any]:
    row = _mapping(state, label)
    dynamic = _mapping(row.get("dynamic_team_background"), f"{label}.dynamic")
    if (
        dynamic.get("schema") != "o2o_dynamic_team_background/v1"
        or dynamic.get("config_digest") != config.content_sha256
        or dynamic.get("environment_generation") != generation
        or dynamic.get("same_timestamp_order") != "BACKGROUND_BEFORE_CANDIDATE"
        or dynamic.get("retarget_mode") != "NEXT_ALIVE_CYCLIC"
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} lost its exact dynamic load binding"
        )
    targets = _list(dynamic.get("targets"), f"{label}.dynamic.targets")
    if len(targets) != len(config.target_health):
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} dynamic target count differs from config"
        )
    live = 0
    for index, (raw_target, configured) in enumerate(
        zip(targets, config.target_health)
    ):
        target = _mapping(raw_target, f"{label}.dynamic.targets[{index}]")
        if target.get("target_index") != index:
            raise FuryBridgePlatformEquivalenceV2Error(
                f"{label} target identities are not contiguous"
            )
        initial = _finite_number(
            target.get("initial_health"), f"{label} target {index} initial health"
        )
        if struct.pack(">d", initial) != struct.pack(">d", configured.health):
            raise FuryBridgePlatformEquivalenceV2Error(
                f"{label} target {index} initial-health bits differ from config"
            )
        dead = target.get("dead")
        if not isinstance(dead, bool):
            raise FuryBridgePlatformEquivalenceV2Error(
                f"{label} target {index} dead flag is not boolean"
            )
        if not dead:
            live += 1
    if row.get("total_target_count") != len(targets):
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} total_target_count is absent or inexact"
        )
    if row.get("num_targets") != live:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} num_targets differs from live target rows"
        )
    selected = _strict_int(row.get("target_index"), f"{label}.target_index")
    if selected < 0 or selected >= len(targets) or targets[selected]["dead"] is True:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} selected target is absent or dead"
        )
    if row.get("target_health_known") is not True:
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} does not expose exact target health"
        )
    return dynamic


def _incoming_receipt(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> IncomingStateDeltaReceiptV1:
    start = _strict_int(before.get("time_ms"), "incoming start time")
    end = _strict_int(after.get("time_ms"), "incoming end time")
    health_before = _finite_number(
        before.get("health_current"), "incoming health before"
    )
    health_after = _finite_number(
        after.get("health_current"), "incoming health after"
    )
    rage_before = _rage(before, "incoming rage before")
    rage_after = _rage(after, "incoming rage after")
    if end <= start:
        raise FuryBridgePlatformEquivalenceV2Error(
            "incoming observation interval did not advance"
        )
    if health_after >= health_before:
        raise FuryBridgePlatformEquivalenceV2Error(
            "controlled interval did not expose incoming health loss"
        )
    if rage_after <= rage_before:
        raise FuryBridgePlatformEquivalenceV2Error(
            "controlled interval did not expose damage-taken rage"
        )
    return IncomingStateDeltaReceiptV1(
        schema=INCOMING_RECEIPT_SCHEMA,
        start_time_ms=start,
        end_time_ms=end,
        health_before=health_before,
        health_after=health_after,
        health_delta=health_after - health_before,
        rage_before=rage_before,
        rage_after=rage_after,
        rage_delta=rage_after - rage_before,
        source=(
            "HARNESS_DERIVED_FROM_EXACT_HEALTH_AND_RAGE_STATE_DELTA_DURING_"
            "NO_POLICY_ACTION_INTERVAL"
        ),
    )


def _validate_background_batch(
    batch: DynamicDamageReceiptBatchV1,
    config: DynamicTeamBackgroundConfigV1,
    generation: int,
) -> None:
    if not isinstance(batch, DynamicDamageReceiptBatchV1):
        raise FuryBridgePlatformEquivalenceV2Error(
            "background receipt endpoint returned the wrong type"
        )
    if (
        batch.config_digest != config.content_sha256
        or batch.environment_generation != generation
        or batch.cursor != 0
        or batch.next_cursor != 2
        or batch.schedule_complete is not True
        or len(batch.receipts) != 2
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "background receipt batch does not close the fixed schedule"
        )
    killer, canceled = batch.receipts
    if (
        killer.schedule_index != 0
        or killer.event_id != "same-ms-background-kill"
        or killer.time_ms != SAME_TIMESTAMP_MS
        or killer.target_index != 0
        or killer.requested_damage != TARGET_HEALTH[0]
        or killer.applied_damage != TARGET_HEALTH[0]
        or killer.overkill_damage != 0
        or killer.killed is not True
        or killer.status != "APPLIED"
        or killer.retargeted_to != 1
        or killer.damage_ordinal is None
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "same-ms background killer receipt is not exact"
        )
    if (
        canceled.schedule_index != 1
        or canceled.event_id != "post-death-background-cancel"
        or canceled.time_ms != POST_DEATH_EVENT_MS
        or canceled.target_index != 0
        or canceled.requested_damage != 3.0
        or canceled.applied_damage != 0
        or canceled.overkill_damage != 3.0
        or canceled.killed is not False
        or canceled.status != "CANCELED_TARGET_DEAD"
        or canceled.retargeted_to is not None
        or canceled.damage_ordinal is None
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "post-death background cancellation receipt is not exact"
        )


def _validate_initial_candidate_batch(
    batch: DynamicCandidateDamageReceiptBatchV1,
    background: DynamicDamageReceiptBatchV1,
    config: DynamicTeamBackgroundConfigV1,
    generation: int,
) -> None:
    if not isinstance(batch, DynamicCandidateDamageReceiptBatchV1):
        raise FuryBridgePlatformEquivalenceV2Error(
            "candidate receipt endpoint returned the wrong type"
        )
    if (
        batch.config_digest != config.content_sha256
        or batch.environment_generation != generation
        or batch.cursor != 0
        or batch.next_cursor != 1
        or len(batch.receipts) != 1
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "initial candidate receipt batch is not exact"
        )
    receipt = batch.receipts[0]
    first_background, second_background = background.receipts
    if (
        receipt.time_ms != SAME_TIMESTAMP_MS
        or receipt.target_index != 0
        or receipt.applied_damage != 0
        or receipt.overkill_damage != receipt.requested_damage
        or receipt.killed is not False
        or receipt.status != "CANCELED_TARGET_DEAD"
        or receipt.action != ActionRef(other_id=7, tag=1)
        or receipt.outcome != "CANCELED"
        or receipt.resolution_phase != "TARGET_DIED_AFTER_OUTCOME"
        or receipt.outcome_computed is not True
        or receipt.random_stream_rewound is not False
        or receipt.attempt_id is not None
        or receipt.retargeted_to is not None
        or not (
            first_background.damage_ordinal
            < receipt.damage_ordinal
            < second_background.damage_ordinal
        )
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "same-ms candidate cancellation/order receipt is not exact"
        )


def _validate_post_lifecycle_state(
    state: Mapping[str, Any], dynamic: Mapping[str, Any]
) -> None:
    if (
        state.get("time_ms") != WAIT_TO_MS
        or state.get("target_index") != 1
        or state.get("num_targets") != 1
        or state.get("total_target_count") != 2
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "post-lifecycle state lost time, retarget, or live/total counts"
        )
    targets = dynamic["targets"]
    if targets[0].get("dead") is not True or targets[1].get("dead") is not False:
        raise FuryBridgePlatformEquivalenceV2Error(
            "post-lifecycle target death state is wrong"
        )
    if targets[0].get("death_time_ms") != SAME_TIMESTAMP_MS:
        raise FuryBridgePlatformEquivalenceV2Error(
            "target death time differs from the same-ms fixture"
        )
    if (
        dynamic.get("background_events_processed") != 2
        or dynamic.get("background_events_canceled") != 1
        or dynamic.get("candidate_events_processed") != 1
        or dynamic.get("candidate_events_canceled") != 1
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "post-lifecycle event accounting is not exact"
        )


def _select_bloodthirst(actions: Sequence[AvailableAction]) -> AvailableAction:
    if not isinstance(actions, list) or any(
        not isinstance(action, AvailableAction) for action in actions
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "actions endpoint did not return list[AvailableAction]"
        )
    bloodthirst = ACTION_KEY_TO_REF["warrior.bloodthirst"]
    matches = [
        action
        for action in actions
        if action.action == bloodthirst and action.legal and action.triggers_gcd
    ]
    if len(matches) != 1:
        raise FuryBridgePlatformEquivalenceV2Error(
            "exactly one legal GCD Bloodthirst action is required"
        )
    return matches[0]


def _validate_server_and_candidate_result(
    server: Any,
    candidate: DynamicCandidateDamageReceiptBatchV1,
    config: DynamicTeamBackgroundConfigV1,
    generation: int,
) -> None:
    events = getattr(server, "events", None)
    pending = getattr(server, "pending_attempt_ids", None)
    if not isinstance(events, tuple) or len(events) != 1 or pending != ():
        raise FuryBridgePlatformEquivalenceV2Error(
            "Bloodthirst server result did not resolve exactly once"
        )
    event = events[0]
    if (
        event.attempt_id != ATTEMPT_ID
        or event.action != ACTION_KEY_TO_REF["warrior.bloodthirst"]
        or event.time_ms != WAIT_TO_MS
        or event.damage <= 0
        or len(event.target_results) != 1
        or event.target_results[0].target_index != 1
        or event.target_results[0].damage != event.damage
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "Bloodthirst server result identity or target delta is wrong"
        )
    if (
        not isinstance(candidate, DynamicCandidateDamageReceiptBatchV1)
        or candidate.config_digest != config.content_sha256
        or candidate.environment_generation != generation
        or candidate.cursor != 1
        or candidate.next_cursor != 2
        or len(candidate.receipts) != 1
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "Bloodthirst candidate receipt cursor is not exact"
        )
    receipt = candidate.receipts[0]
    if (
        receipt.time_ms != WAIT_TO_MS
        or receipt.target_index != 1
        or receipt.action != ACTION_KEY_TO_REF["warrior.bloodthirst"]
        or receipt.attempt_id != ATTEMPT_ID
        or receipt.status != "APPLIED"
        or receipt.applied_damage != event.damage
        or receipt.outcome != event.outcome
        or receipt.outcome_computed is not True
        or receipt.random_stream_rewound is not False
        or receipt.resolution_phase != "APPLIED_AFTER_OUTCOME"
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "Bloodthirst candidate and server receipts are not exactly bound"
        )


def capture_dynamic_trace_v2(
    bridge: Any,
    request: Mapping[str, Any],
    config: DynamicTeamBackgroundConfigV1,
    *,
    seed: int,
    platform_metadata: Mapping[str, Any],
) -> JSONMap:
    """Capture and validate one complete fixed dynamic JSONL trace."""

    seed = _strict_int(seed, "seed")
    trace: list[JSONMap] = []
    close_succeeded = False
    try:
        loaded = bridge.load_dynamic_v1(request, seed, config)
        if not isinstance(loaded, DynamicLoadResultV1):
            raise FuryBridgePlatformEquivalenceV2Error(
                "load_dynamic_v1 did not return DynamicLoadResultV1"
            )
        receipt = loaded.receipt
        if (
            receipt.schema != "o2o_dynamic_team_background/v1"
            or receipt.config_digest != config.content_sha256
            or receipt.environment_generation <= 0
            or receipt.target_count != 2
            or receipt.background_event_count != 2
            or receipt.same_timestamp_order != "BACKGROUND_BEFORE_CANDIDATE"
            or receipt.retarget_mode != "NEXT_ALIVE_CYCLIC"
        ):
            raise FuryBridgePlatformEquivalenceV2Error(
                "dynamic load receipt differs from the fixed contract"
            )
        generation = receipt.environment_generation
        initial_dynamic = _validate_dynamic_state(
            loaded.state, config, generation, label="load state"
        )
        if (
            loaded.state.get("time_ms") != 0
            or loaded.state.get("target_index") != 0
            or loaded.state.get("num_targets") != 2
            or loaded.state.get("total_target_count") != 2
            or any(target.get("dead") is not False for target in initial_dynamic["targets"])
        ):
            raise FuryBridgePlatformEquivalenceV2Error(
                "dynamic initial state is not the exact two-live-target root"
            )
        trace.append(
            {
                "command": "load_dynamic_v1",
                "result": _jsonable(loaded),
            }
        )

        waited = bridge.wait(WAIT_TO_MS)
        _validate_dynamic_state(waited, config, generation, label="wait state")
        if waited.get("time_ms") != 0 or waited.get("needs_input") is not False:
            raise FuryBridgePlatformEquivalenceV2Error(
                "wait did not open the controlled no-policy-action interval"
            )
        trace.append(
            {"command": "wait", "wait_ms": WAIT_TO_MS, "state": _jsonable(waited)}
        )

        advanced = bridge.advance()
        dynamic_after = _validate_dynamic_state(
            advanced, config, generation, label="post-lifecycle state"
        )
        _validate_post_lifecycle_state(advanced, dynamic_after)
        trace.append({"command": "advance", "state": _jsonable(advanced)})

        incoming = _incoming_receipt(waited, advanced)
        trace.append(
            {"command": "incoming_damage_receipt", "result": asdict(incoming)}
        )

        background = bridge.dynamic_damage_receipts(cursor=0)
        _validate_background_batch(background, config, generation)
        trace.append(
            {
                "command": "dynamic_damage_receipts",
                "cursor": 0,
                "result": _jsonable(background),
            }
        )

        candidate = bridge.dynamic_candidate_damage_receipts(cursor=0)
        _validate_initial_candidate_batch(
            candidate, background, config, generation
        )
        trace.append(
            {
                "command": "dynamic_candidate_damage_receipts",
                "cursor": 0,
                "result": _jsonable(candidate),
            }
        )

        reported = bridge.state()
        _validate_dynamic_state(
            reported, config, generation, label="reported post-lifecycle state"
        )
        if canonical_bytes(reported) != canonical_bytes(advanced):
            raise FuryBridgePlatformEquivalenceV2Error(
                "state() differs from the preceding advance state"
            )
        trace.append(
            {"command": "state_post_lifecycle", "state": _jsonable(reported)}
        )

        actions = bridge.actions()
        selected = _select_bloodthirst(actions)
        trace.append(
            {
                "command": "actions",
                "actions": [_jsonable(action) for action in actions],
                "selected": _jsonable(selected),
            }
        )

        applied = bridge.act(selected.action, attempt_id=ATTEMPT_ID)
        if (
            applied.casted is not True
            or applied.consumes_decision is not True
            or applied.finished is not False
            or applied.needs_input is not False
        ):
            raise FuryBridgePlatformEquivalenceV2Error(
                "Bloodthirst act acknowledgement is not exact"
            )
        _validate_dynamic_state(
            applied.state, config, generation, label="Bloodthirst act state"
        )
        if (
            applied.state.get("target_index") != 1
            or applied.state.get("num_targets") != 1
            or applied.state.get("total_target_count") != 2
        ):
            raise FuryBridgePlatformEquivalenceV2Error(
                "Bloodthirst act state lost target/live/total identity"
            )
        trace.append(
            {
                "command": "act",
                "attempt_id": ATTEMPT_ID,
                "selected": _jsonable(selected.action),
                "result": _jsonable(applied),
            }
        )

        server = bridge.server_results_since_last_decision((ATTEMPT_ID,))
        candidate_tail = bridge.dynamic_candidate_damage_receipts(cursor=1)
        _validate_server_and_candidate_result(
            server, candidate_tail, config, generation
        )
        trace.append(
            {"command": "server_results", "result": _jsonable(server)}
        )
        trace.append(
            {
                "command": "dynamic_candidate_damage_receipts",
                "cursor": 1,
                "result": _jsonable(candidate_tail),
            }
        )

        background_tail = bridge.dynamic_damage_receipts(cursor=2)
        if (
            not isinstance(background_tail, DynamicDamageReceiptBatchV1)
            or background_tail.config_digest != config.content_sha256
            or background_tail.environment_generation != generation
            or background_tail.cursor != 2
            or background_tail.next_cursor != 2
            or background_tail.schedule_complete is not True
            or background_tail.receipts != ()
        ):
            raise FuryBridgePlatformEquivalenceV2Error(
                "background terminal cursor receipt is not exact"
            )
        trace.append(
            {
                "command": "dynamic_damage_receipts",
                "cursor": 2,
                "result": _jsonable(background_tail),
            }
        )

        final_state = bridge.state()
        _validate_dynamic_state(
            final_state, config, generation, label="final state"
        )
        if canonical_bytes(final_state) != canonical_bytes(applied.state):
            raise FuryBridgePlatformEquivalenceV2Error(
                "read-only receipt commands changed final simulator state"
            )
        trace.append({"command": "state_final", "state": _jsonable(final_state)})
    finally:
        bridge.close()
        close_succeeded = True

    if not close_succeeded:
        raise FuryBridgePlatformEquivalenceV2Error("bridge close did not succeed")
    trace.append(
        {
            "command": "close",
            "result": {
                "acknowledged": True,
                "process_exit_verified_by_simulator_bridge": True,
            },
        }
    )
    wire_trace = _validate_wire_trace(getattr(bridge, "wire_trace_v2", None))
    if tuple(row.get("command") for row in trace) != _VALIDATED_COMMANDS:
        raise FuryBridgePlatformEquivalenceV2Error(
            "validated trace command sequence drifted"
        )
    return {
        "schema": CAPTURE_SCHEMA,
        "platform_metadata": _strict_json_copy(
            platform_metadata, "platform metadata"
        ),
        "physical_trace": {
            "wire_jsonl": wire_trace,
            "typed_validated": trace,
        },
        "coverage": {
            "load_dynamic_v1": True,
            "target_count": 2,
            "explicit_health_stat_index": HEALTH_STAT_INDEX,
            "same_timestamp_background_before_candidate": True,
            "same_timestamp_ms": SAME_TIMESTAMP_MS,
            "target_zero_dead": True,
            "post_death_background_canceled": True,
            "automatic_retarget_to": 1,
            "live_target_count": 1,
            "total_target_count": 2,
            "background_receipts": 2,
            "candidate_receipts": 2,
            "incoming_receipt_kind": INCOMING_RECEIPT_SCHEMA,
            "server_result_attempt_id": ATTEMPT_ID,
            "close_acknowledged": True,
        },
    }


def _validate_wire_trace(value: Any) -> list[JSONMap]:
    if not isinstance(value, list):
        raise FuryBridgePlatformEquivalenceV2Error(
            "bridge does not expose the required raw wire_trace_v2"
        )
    trace = _strict_json_copy(value, "wire trace")
    if len(trace) != len(_WIRE_COMMANDS):
        raise FuryBridgePlatformEquivalenceV2Error(
            "raw wire trace command count is not exact"
        )
    for index, (row, command) in enumerate(zip(trace, _WIRE_COMMANDS)):
        if not isinstance(row, dict) or set(row) != {"request", "response"}:
            raise FuryBridgePlatformEquivalenceV2Error(
                f"wire trace row {index} field set is not exact"
            )
        request = _mapping(row.get("request"), f"wire row {index} request")
        response = _mapping(row.get("response"), f"wire row {index} response")
        if request.get("command") != command:
            raise FuryBridgePlatformEquivalenceV2Error(
                f"wire request {index} command differs from the fixed trace"
            )
        if response.get("ok") is not True or response.get("command") != command:
            raise FuryBridgePlatformEquivalenceV2Error(
                f"wire response {index} did not acknowledge the exact command"
            )
    return trace


def strip_platform_metadata_v2(capture: Mapping[str, Any]) -> JSONMap:
    row = _mapping(capture, "platform capture")
    if set(row) != {"schema", "platform_metadata", "physical_trace", "coverage"}:
        raise FuryBridgePlatformEquivalenceV2Error(
            "platform capture field set is not exact"
        )
    if row.get("schema") != CAPTURE_SCHEMA:
        raise FuryBridgePlatformEquivalenceV2Error(
            "platform capture schema is unsupported"
        )
    _mapping(row.get("platform_metadata"), "platform capture metadata")
    return _strict_json_copy(
        {
            "schema": row["schema"],
            "physical_trace": row["physical_trace"],
            "coverage": row["coverage"],
        },
        "normalized platform capture",
    )


def first_difference(left: Any, right: Any, path: str = "$") -> JSONMap | None:
    if type(left) is not type(right):
        return {
            "path": path,
            "reason": "type",
            "windows": left,
            "linux": right,
        }
    if isinstance(left, dict):
        left_keys = sorted(left)
        right_keys = sorted(right)
        if left_keys != right_keys:
            return {
                "path": path,
                "reason": "keys",
                "windows": left_keys,
                "linux": right_keys,
            }
        for key in left_keys:
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {
                "path": path,
                "reason": "length",
                "windows": len(left),
                "linux": len(right),
            }
        for index, (left_value, right_value) in enumerate(zip(left, right)):
            difference = first_difference(
                left_value, right_value, f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if left != right:
        return {
            "path": path,
            "reason": "value",
            "windows": left,
            "linux": right,
        }
    return None


def compare_platform_captures_v2(
    windows_capture: Mapping[str, Any], linux_capture: Mapping[str, Any]
) -> JSONMap:
    windows = strip_platform_metadata_v2(windows_capture)
    linux = strip_platform_metadata_v2(linux_capture)
    windows_bytes = canonical_bytes(windows)
    linux_bytes = canonical_bytes(linux)
    exact = windows_bytes == linux_bytes
    windows_physical = _mapping(
        windows.get("physical_trace"), "windows physical trace"
    )
    linux_physical = _mapping(linux.get("physical_trace"), "linux physical trace")
    return {
        "status": "PASS_EXACT_DYNAMIC_TRACE" if exact else "FAIL_TRACE_DIFFERENCE",
        "exact_canonical_json": exact,
        "windows_normalized_capture_sha256": sha256_bytes(windows_bytes),
        "linux_normalized_capture_sha256": sha256_bytes(linux_bytes),
        "windows_wire_step_count": len(
            _list(windows_physical.get("wire_jsonl"), "windows wire trace")
        ),
        "linux_wire_step_count": len(
            _list(linux_physical.get("wire_jsonl"), "linux wire trace")
        ),
        "windows_validated_step_count": len(
            _list(
                windows_physical.get("typed_validated"),
                "windows validated trace",
            )
        ),
        "linux_validated_step_count": len(
            _list(linux_physical.get("typed_validated"), "linux validated trace")
        ),
        "excluded_platform_metadata_paths": ["$.platform_metadata"],
        "excluded_physical_result_or_state_fields": [],
        "first_difference": None if exact else first_difference(windows, linux),
    }


def run_equivalence_v2(
    *,
    request_path: Path = DEFAULT_REQUEST,
    windows_bridge: Path = DEFAULT_WINDOWS_BRIDGE,
    linux_bridge: Path = DEFAULT_LINUX_BRIDGE,
    simulator_root: Path = DEFAULT_SIMULATOR_ROOT,
    distro: str = "Ubuntu-22.04",
    seed: int = 20260911,
    expected_windows_sha256: str = EXPECTED_WINDOWS_SHA256,
    expected_linux_sha256: str = EXPECTED_LINUX_SHA256,
    bridge_factory: Callable[..., Any] = RecordingSimulatorBridgeV2,
) -> JSONMap:
    request_identity, base_request = load_request(request_path)
    request, config = build_dynamic_fixture_v2(base_request)
    windows_identity, _ = snapshot_file(windows_bridge)
    linux_identity, _ = snapshot_file(linux_bridge)
    _require_sha256(
        expected_windows_sha256, "expected_windows_sha256"
    )
    _require_sha256(expected_linux_sha256, "expected_linux_sha256")
    if windows_identity.sha256 != expected_windows_sha256:
        raise FuryBridgePlatformEquivalenceV2Error(
            "Windows bridge SHA-256 differs from the pinned v4 binary"
        )
    if linux_identity.sha256 != expected_linux_sha256:
        raise FuryBridgePlatformEquivalenceV2Error(
            "Linux bridge SHA-256 differs from the pinned v4 binary"
        )
    simulator_root = simulator_root.expanduser().resolve()
    if not simulator_root.is_dir():
        raise FuryBridgePlatformEquivalenceV2Error(
            f"simulator root is not a directory: {simulator_root}"
        )
    seed = _strict_int(seed, "seed")

    windows_metadata = {
        "platform": "windows-amd64",
        "binary": asdict(windows_identity),
        "launch": {
            "executable": str(windows_bridge.expanduser().resolve()),
            "cwd": str(simulator_root),
            "arguments": [],
        },
    }
    windows_runtime = bridge_factory(windows_bridge, cwd=simulator_root)
    windows_capture = capture_dynamic_trace_v2(
        windows_runtime,
        request,
        config,
        seed=seed,
        platform_metadata=windows_metadata,
    )

    linux_arguments = linux_wsl_arguments(
        distro=distro,
        simulator_root=simulator_root,
        linux_bridge=linux_bridge,
    )
    linux_metadata = {
        "platform": "linux-amd64-via-wsl",
        "binary": asdict(linux_identity),
        "launch": {
            "executable": "wsl.exe",
            "cwd": str(simulator_root),
            "arguments": list(linux_arguments),
        },
    }
    linux_runtime = bridge_factory(
        "wsl.exe", arguments=linux_arguments, cwd=simulator_root
    )
    linux_capture = capture_dynamic_trace_v2(
        linux_runtime,
        request,
        config,
        seed=seed,
        platform_metadata=linux_metadata,
    )

    comparison = compare_platform_captures_v2(
        windows_capture, linux_capture
    )
    materialized_request_bytes = canonical_bytes(request)
    return {
        "schema": SCHEMA,
        "evidence_scope": EVIDENCE_SCOPE,
        "status": comparison["status"],
        "seed": seed,
        "action_rule": ACTION_RULE,
        "base_request": asdict(request_identity),
        "materialized_request": {
            "sha256": sha256_bytes(materialized_request_bytes),
            "size_bytes": len(materialized_request_bytes),
            "target_count": 2,
            "health_stat_index": HEALTH_STAT_INDEX,
            "target_health_ieee754_binary64_hex": [
                struct.pack(">d", health).hex() for health in TARGET_HEALTH
            ],
        },
        "dynamic_config": {
            "schema": "o2o_dynamic_team_background/v1",
            "content_sha256": config.content_sha256,
            "target_count": len(config.target_health),
            "background_event_count": len(config.background_damage_events),
            "same_timestamp_order": config.same_timestamp_order,
            "retarget_mode": config.retarget_mode,
        },
        "binaries": {
            "windows": asdict(windows_identity),
            "linux_wsl": asdict(linux_identity),
        },
        "launch": {
            "windows_cwd": str(simulator_root),
            "linux_distro": distro,
            "linux_arguments": list(linux_arguments),
        },
        "coverage": copy.deepcopy(windows_capture["coverage"]),
        "comparison": comparison,
        "claim_boundary": {
            "supports": [
                "exact raw JSONL and typed dynamic trace equality for this fixed request, seed, command stream, and pinned v4 binary pair"
            ],
            "does_not_support": [
                "scientific simulator equivalence outside the fixed trace",
                "policy fidelity or policy superiority",
                "HPC deployment readiness",
                "real-game correctness",
                "a native bridge incoming-damage receipt endpoint",
            ],
        },
    }


def publish_content_addressed_receipt_v2(
    report: Mapping[str, Any], output_directory: Path = DEFAULT_RECEIPT_DIRECTORY
) -> tuple[Path, JSONMap]:
    core = _strict_json_copy(report, "equivalence report")
    if "content_address" in core:
        raise FuryBridgePlatformEquivalenceV2Error(
            "report already contains content_address"
        )
    digest = sha256_bytes(canonical_bytes(core))
    receipt = {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": digest,
        },
    }
    rendered = (
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    directory = output_directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (
        f"fury-bridge-platform-equivalence-v2.{digest}.receipt.json"
    )
    if destination.exists():
        if destination.read_bytes() != rendered:
            raise FuryBridgePlatformEquivalenceV2Error(
                f"content-address collision at {destination}"
            )
        return destination, receipt
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".fury-bridge-platform-equivalence-v2-",
        suffix=".tmp",
        dir=directory,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_bytes(rendered)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return destination, receipt


def verify_content_addressed_receipt_v2(value: Mapping[str, Any]) -> str:
    row = _mapping(value, "receipt")
    address = _mapping(row.get("content_address"), "receipt.content_address")
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
    ):
        raise FuryBridgePlatformEquivalenceV2Error(
            "receipt content-address contract is invalid"
        )
    declared = _require_sha256(address.get("sha256"), "receipt content SHA-256")
    core = {key: child for key, child in row.items() if key != "content_address"}
    observed = sha256_bytes(canonical_bytes(core))
    if declared != observed:
        raise FuryBridgePlatformEquivalenceV2Error(
            "receipt content-address SHA-256 mismatch"
        )
    return observed


def _mapping(value: Any, label: str) -> JSONMap:
    if not isinstance(value, dict):
        raise FuryBridgePlatformEquivalenceV2Error(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryBridgePlatformEquivalenceV2Error(f"{label} must be an array")
    return value


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryBridgePlatformEquivalenceV2Error(f"{label} must be an integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryBridgePlatformEquivalenceV2Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FuryBridgePlatformEquivalenceV2Error(f"{label} must be finite")
    return result


def _rage(state: Mapping[str, Any], label: str) -> float:
    power = _mapping(state.get("power"), f"{label}.power")
    if power.get("type") != "rage":
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} state power is not rage"
        )
    return _finite_number(power.get("current"), label)


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FuryBridgePlatformEquivalenceV2Error(
            f"{label} must be lowercase SHA-256"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    parser.add_argument("--windows-bridge", type=Path, default=DEFAULT_WINDOWS_BRIDGE)
    parser.add_argument("--linux-bridge", type=Path, default=DEFAULT_LINUX_BRIDGE)
    parser.add_argument("--simulator-root", type=Path, default=DEFAULT_SIMULATOR_ROOT)
    parser.add_argument("--distro", default="Ubuntu-22.04")
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_RECEIPT_DIRECTORY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_equivalence_v2(
        request_path=args.request,
        windows_bridge=args.windows_bridge,
        linux_bridge=args.linux_bridge,
        simulator_root=args.simulator_root,
        distro=args.distro,
        seed=args.seed,
    )
    destination, receipt = publish_content_addressed_receipt_v2(
        report, args.output_directory
    )
    summary = {
        "schema": SCHEMA,
        "status": report["status"],
        "receipt_path": str(destination),
        "receipt_content_sha256": receipt["content_address"]["sha256"],
        "trace_sha256": report["comparison"][
            "windows_normalized_capture_sha256"
        ],
    }
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    )
    return 0 if report["status"] == "PASS_EXACT_DYNAMIC_TRACE" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "ACTION_RULE",
    "CAPTURE_SCHEMA",
    "DEFAULT_LINUX_BRIDGE",
    "DEFAULT_RECEIPT_DIRECTORY",
    "DEFAULT_REQUEST",
    "DEFAULT_SIMULATOR_ROOT",
    "DEFAULT_WINDOWS_BRIDGE",
    "EVIDENCE_SCOPE",
    "EXPECTED_LINUX_SHA256",
    "EXPECTED_WINDOWS_SHA256",
    "FuryBridgePlatformEquivalenceV2Error",
    "INCOMING_RECEIPT_SCHEMA",
    "RecordingSimulatorBridgeV2",
    "SCHEMA",
    "build_dynamic_fixture_v2",
    "canonical_bytes",
    "capture_dynamic_trace_v2",
    "compare_platform_captures_v2",
    "first_difference",
    "linux_wsl_arguments",
    "load_request",
    "publish_content_addressed_receipt_v2",
    "run_equivalence_v2",
    "snapshot_file",
    "strip_platform_metadata_v2",
    "verify_content_addressed_receipt_v2",
    "windows_path_to_wsl",
)
