"""Deterministic time-zero prefix reconstruction gate for the O2O bridge.

This is deliberately narrower than simulator snapshot/restore.  Each reference
and replay starts a fresh bridge process, loads the same content-addressed
RaidSimRequest with the same absolute seed, and replays the full prefix from
time zero.  The gate compares every command response, exported state, and
available-action table, then compares the terminal state and damage hashes.

The resulting evidence is simulator-only.  It cannot seed a historical WoW
mid-state, backfill a Shadow row, vote in policy selection, train a policy, or
authorize deployment.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .sim_bridge import ActionRef, AvailableAction, SimulatorBridge


JSONMap = dict[str, Any]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.seedfix-v1.exe"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
DEFAULT_INPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "prefix_reconstruction_inputs_v1"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_prefix_reconstruction_gate_v1.json"
)
DEFAULT_RECEIPT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_prefix_reconstruction_gate_v1.receipt.json"
)
DEFAULT_SEEDS = (177013, 177019)

EXPECTED_BRIDGE_SHA256 = (
    "3f455eada0cf962f10294cc0a0db1f5e698d9211cffe5a6b715028be6ed53a9d"
)
EXPECTED_CONFIG_SHA256 = (
    "b9922bc5c14c3f88b2138251e18b743c42938ef74e6ccb4e632493dcc7f1d3cf"
)

HEROIC_STRIKE_QUEUE = {"spell_id": 25286, "tag": 1}
CLEAVE_QUEUE = {"spell_id": 20569, "tag": 1}
BATTLE_STANCE = {"spell_id": 2457, "tag": 0}
BLOODRAGE = {"spell_id": 2687, "tag": 0}
BLOODTHIRST = {"spell_id": 23894, "tag": 0}
WHIRLWIND = {"spell_id": 1680, "tag": 0}


LANES: tuple[JSONMap, ...] = (
    {
        "lane_id": "queue_cancel_wait_advance",
        "request_id": "single_target",
        "coverage": ["queue", "cancel_queue", "wait", "advance"],
        "steps": [
            {
                "command": "load",
                "expect": {"needs_input": True, "finished": False, "num_targets": 1},
            },
            {
                "command": "act",
                "selector": HEROIC_STRIKE_QUEUE,
                "expect": {
                    "casted": True,
                    "consumes_decision": False,
                    "needs_input": True,
                    "finished": False,
                    "aura_absent": "HS/Cleave Queue Aura",
                },
            },
            {
                "command": "wait",
                "wait_ms": 500,
                "expect": {"needs_input": False, "finished": False},
            },
            {
                "command": "advance",
                "expect": {
                    "needs_input": True,
                    "finished": False,
                    "time_increased": True,
                    "aura_contains": "HS/Cleave Queue Aura",
                },
            },
            {
                "command": "cancel_queue",
                "expect": {
                    "canceled": True,
                    "consumes_decision": False,
                    "needs_input": True,
                    "finished": False,
                    "aura_absent": "HS/Cleave Queue Aura",
                },
            },
            {
                "command": "wait",
                "wait_ms": 100,
                "expect": {"needs_input": False, "finished": False},
            },
            {
                "command": "advance",
                "expect": {
                    "needs_input": True,
                    "finished": False,
                    "time_increased": True,
                },
            },
        ],
    },
    {
        "lane_id": "stance_offgcd_gcd_swing",
        "request_id": "single_target",
        "coverage": ["stance", "off_gcd", "gcd", "cast", "swing_during_advance"],
        "steps": [
            {
                "command": "load",
                "expect": {"needs_input": True, "finished": False, "num_targets": 1},
            },
            {
                "command": "act",
                "selector": BATTLE_STANCE,
                "expect": {
                    "casted": True,
                    "consumes_decision": False,
                    "needs_input": True,
                    "finished": False,
                    "aura_contains": "Battle Stance",
                },
            },
            {
                "command": "act",
                "selector": BLOODRAGE,
                "expect": {
                    "casted": True,
                    "consumes_decision": False,
                    "needs_input": True,
                    "finished": False,
                    "aura_contains": "Bloodrage",
                },
            },
            {
                "command": "act",
                "selector": BLOODTHIRST,
                "expect": {
                    "casted": True,
                    "consumes_decision": True,
                    "needs_input": False,
                    "finished": False,
                },
            },
            {
                "command": "advance",
                "expect": {
                    "needs_input": True,
                    "finished": False,
                    "time_increased": True,
                },
            },
            {
                "command": "wait",
                "wait_ms": 2000,
                "expect": {"needs_input": False, "finished": False},
            },
            {
                "command": "advance",
                "expect": {
                    "needs_input": True,
                    "finished": False,
                    "time_increased": True,
                    "oh_swing_deadline_crossed": True,
                },
            },
        ],
    },
    {
        "lane_id": "multitarget_settarget_queue_gcd",
        "request_id": "three_target",
        "coverage": [
            "multi_target",
            "set_target",
            "queue",
            "gcd",
            "queued_mainhand_swing",
        ],
        "steps": [
            {
                "command": "load",
                "expect": {"needs_input": True, "finished": False, "num_targets": 3},
            },
            {
                "command": "set_target",
                "target_index": 2,
                "expect": {
                    "changed": True,
                    "target_index": 2,
                    "needs_input": True,
                    "finished": False,
                },
            },
            {
                "command": "act",
                "selector": CLEAVE_QUEUE,
                "expect": {
                    "casted": True,
                    "consumes_decision": False,
                    "needs_input": True,
                    "finished": False,
                    "aura_absent": "HS/Cleave Queue Aura",
                },
            },
            {
                "command": "act",
                "selector": WHIRLWIND,
                "expect": {
                    "casted": True,
                    "consumes_decision": True,
                    "needs_input": False,
                    "finished": False,
                },
            },
            {
                "command": "advance",
                "expect": {
                    "needs_input": True,
                    "finished": False,
                    "time_increased": True,
                    "target_index": 2,
                },
            },
            {
                "command": "wait",
                "wait_ms": 2000,
                "expect": {"needs_input": False, "finished": False, "target_index": 2},
            },
            {
                "command": "advance",
                "expect": {
                    "needs_input": True,
                    "finished": False,
                    "time_increased": True,
                    "mh_swing_deadline_crossed": True,
                    "target_index": 2,
                    "aura_absent": "HS/Cleave Queue Aura",
                },
            },
        ],
    },
)


class PrefixReconstructionGateError(RuntimeError):
    """The gate input or one deterministic transcript is invalid."""


def canonical_bytes(value: Any) -> bytes:
    """Serialize a JSON value in the exact form used by hashes/comparisons."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PrefixReconstructionGateError(f"could not read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise PrefixReconstructionGateError(f"{label} must be a JSON object: {path}")
    return value


def derive_requests(config: Mapping[str, Any], *, config_sha256: str) -> dict[str, JSONMap]:
    """Derive fixed 12-second one- and three-target requests without mutation."""

    if not isinstance(config, Mapping):
        raise PrefixReconstructionGateError("base configuration must be an object")
    if not isinstance(config_sha256, str) or len(config_sha256) != 64:
        raise PrefixReconstructionGateError("config_sha256 must be a SHA-256 hex digest")

    single = deepcopy(dict(config))
    try:
        encounter = single["encounter"]
        sim_options = single["simOptions"]
        targets = encounter["targets"]
        player = single["raid"]["parties"][0]["players"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise PrefixReconstructionGateError(
            "configuration must contain raid.parties[0].players[0], "
            "encounter.targets, and simOptions"
        ) from error
    if not isinstance(encounter, dict) or not isinstance(sim_options, dict):
        raise PrefixReconstructionGateError("encounter and simOptions must be objects")
    if not isinstance(targets, list) or not targets or not isinstance(targets[0], dict):
        raise PrefixReconstructionGateError("encounter.targets must contain an object")
    if not isinstance(player, dict):
        raise PrefixReconstructionGateError("the interactive player must be an object")

    encounter["duration"] = 12
    encounter.pop("useHealth", None)
    encounter["targets"] = [deepcopy(targets[0])]
    sim_options["iterations"] = 1
    sim_options["interactive"] = True

    three = deepcopy(single)
    source_target = three["encounter"]["targets"][0]
    three["encounter"]["targets"] = [deepcopy(source_target) for _ in range(3)]

    return {"single_target": single, "three_target": three}


def build_request_bundle(
    requests: Mapping[str, Mapping[str, Any]], *, config_sha256: str
) -> JSONMap:
    return {
        "schema_version": 1,
        "kind": "fury_prefix_reconstruction_request_bundle_v1",
        "base_config_sha256": config_sha256,
        "derivation": {
            "duration_seconds": 12,
            "iterations": 1,
            "interactive": True,
            "target_counts": {"single_target": 1, "three_target": 3},
            "seed_source": "nonzero load-command absolute seed override",
        },
        "requests": deepcopy(dict(requests)),
    }


def _write_content_addressed(
    directory: Path, *, prefix: str, suffix: str, payload: bytes
) -> tuple[Path, str]:
    digest = sha256_bytes(payload)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}-{digest}{suffix}"
    if path.exists():
        if path.read_bytes() != payload:
            raise PrefixReconstructionGateError(
                f"content-addressed path contains different bytes: {path}"
            )
    else:
        path.write_bytes(payload)
    if sha256_file(path) != digest:
        raise PrefixReconstructionGateError(f"staged input hash mismatch: {path}")
    return path, digest


def stage_inputs(
    *,
    config_path: Path,
    config_sha256: str,
    requests: Mapping[str, Mapping[str, Any]],
    input_directory: Path,
) -> JSONMap:
    config_payload = config_path.read_bytes()
    if sha256_bytes(config_payload) != config_sha256:
        raise PrefixReconstructionGateError("configuration changed while staging")
    config_snapshot, staged_config_sha = _write_content_addressed(
        input_directory,
        prefix="config",
        suffix=".json",
        payload=config_payload,
    )
    request_bundle = build_request_bundle(requests, config_sha256=config_sha256)
    request_payload = canonical_bytes(request_bundle)
    request_snapshot, request_bundle_sha = _write_content_addressed(
        input_directory,
        prefix="request-bundle",
        suffix=".json",
        payload=request_payload,
    )
    request_hashes = {
        request_id: sha256_bytes(canonical_bytes(request))
        for request_id, request in sorted(requests.items())
    }
    return {
        "config_snapshot_path": str(config_snapshot.resolve()),
        "config_snapshot_sha256": staged_config_sha,
        "request_bundle_path": str(request_snapshot.resolve()),
        "request_bundle_sha256": request_bundle_sha,
        "request_sha256_by_id": request_hashes,
    }


def _action_wire(action: AvailableAction) -> JSONMap:
    return {
        "index": action.index,
        "action": action.action.to_wire(),
        "label": action.label,
        "legal": action.legal,
        "ready_in_ms": action.ready_in_ms,
        "triggers_gcd": action.triggers_gcd,
    }


def _actions_wire(actions: Sequence[AvailableAction]) -> list[JSONMap]:
    return [_action_wire(action) for action in actions]


def _selector_ref(selector: Mapping[str, Any]) -> ActionRef:
    return ActionRef(
        spell_id=int(selector.get("spell_id", 0)),
        item_id=int(selector.get("item_id", 0)),
        other_id=int(selector.get("other_id", 0)),
        tag=int(selector.get("tag", 0)),
    )


def _resolve_legal_action(
    actions: Sequence[AvailableAction], selector: Mapping[str, Any]
) -> AvailableAction:
    wanted = _selector_ref(selector)
    matches = [action for action in actions if action.action == wanted]
    if len(matches) != 1:
        raise PrefixReconstructionGateError(
            f"selector {dict(selector)} resolved to {len(matches)} available actions"
        )
    selected = matches[0]
    if not selected.legal:
        raise PrefixReconstructionGateError(
            f"selected action is not legal: {dict(selector)} ready_in_ms={selected.ready_in_ms}"
        )
    return selected


def _aura_labels(state: Mapping[str, Any]) -> list[str]:
    values = state.get("auras")
    if not isinstance(values, list):
        return []
    return [
        str(value.get("label", ""))
        for value in values
        if isinstance(value, Mapping)
    ]


def _check_step(
    *,
    definition: Mapping[str, Any],
    response: Mapping[str, Any],
    state: Mapping[str, Any],
    previous_state: Mapping[str, Any] | None,
) -> list[JSONMap]:
    expect = definition.get("expect")
    if not isinstance(expect, Mapping):
        raise PrefixReconstructionGateError("every transcript step needs an expectation")
    checks: list[JSONMap] = []

    def check(name: str, actual: Any, expected: Any) -> None:
        checks.append(
            {
                "name": name,
                "passed": actual == expected,
                "actual": actual,
                "expected": expected,
            }
        )

    response_state = response.get("state")
    check("response_state_equals_exported_state", response_state, dict(state))
    for field in ("needs_input", "finished", "num_targets", "target_index"):
        if field in expect:
            check(f"state.{field}", state.get(field), expect[field])

    result = response.get("apply")
    if isinstance(result, Mapping):
        for field in ("casted", "consumes_decision", "needs_input", "finished"):
            if field in expect:
                check(f"apply.{field}", result.get(field), expect[field])
    canceled = response.get("cancel_queue")
    if isinstance(canceled, Mapping):
        for field in ("canceled", "consumes_decision", "needs_input", "finished"):
            if field in expect:
                check(f"cancel_queue.{field}", canceled.get(field), expect[field])
    targeted = response.get("set_target")
    if isinstance(targeted, Mapping):
        for field in ("changed", "target_index", "needs_input", "finished"):
            if field in expect:
                check(f"set_target.{field}", targeted.get(field), expect[field])

    labels = _aura_labels(state)
    if "aura_contains" in expect:
        needle = str(expect["aura_contains"])
        check("aura_contains", any(needle in label for label in labels), True)
    if "aura_absent" in expect:
        needle = str(expect["aura_absent"])
        check("aura_absent", any(needle in label for label in labels), False)

    if expect.get("time_increased") is True:
        previous_time = None if previous_state is None else previous_state.get("time_ms")
        current_time = state.get("time_ms")
        passed = (
            isinstance(previous_time, int)
            and isinstance(current_time, int)
            and current_time > previous_time
        )
        checks.append(
            {
                "name": "time_increased",
                "passed": passed,
                "actual": current_time,
                "expected": f"> {previous_time}",
            }
        )
    for hand, field in (
        ("mh", "mh_swing_remaining_ms"),
        ("oh", "oh_swing_remaining_ms"),
    ):
        expectation = f"{hand}_swing_deadline_crossed"
        if expect.get(expectation) is not True:
            continue
        previous_remaining = None if previous_state is None else previous_state.get(field)
        previous_time = None if previous_state is None else previous_state.get("time_ms")
        current_time = state.get("time_ms")
        passed = (
            isinstance(previous_remaining, int)
            and isinstance(previous_time, int)
            and isinstance(current_time, int)
            and current_time - previous_time >= previous_remaining
        )
        checks.append(
            {
                "name": expectation,
                "passed": passed,
                "actual": {
                    "advanced_ms": (
                        None
                        if not isinstance(previous_time, int) or not isinstance(current_time, int)
                        else current_time - previous_time
                    ),
                    "prior_remaining_ms": previous_remaining,
                },
                "expected": "advanced_ms >= prior_remaining_ms",
            }
        )
    return checks


def _execute_step(
    bridge: SimulatorBridge,
    definition: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    seed: int,
) -> tuple[JSONMap, JSONMap, list[JSONMap]]:
    command = definition.get("command")
    if command == "load":
        returned_state = bridge.load(request, seed)
        command_record: JSONMap = {
            "command": "load",
            "request_sha256": request_sha256,
            "absolute_seed": seed,
        }
        response: JSONMap = {"state": returned_state}
    elif command == "act":
        selector = definition.get("selector")
        if not isinstance(selector, Mapping):
            raise PrefixReconstructionGateError("act step is missing selector")
        selected = _resolve_legal_action(bridge.actions(), selector)
        applied = bridge.act(selected.action)
        command_record = {
            "command": "act",
            "selector": dict(selector),
            "resolved_action": selected.action.to_wire(),
            "resolved_action_index": selected.index,
            "resolved_action_triggers_gcd": selected.triggers_gcd,
        }
        response = {
            "apply": {
                "casted": applied.casted,
                "consumes_decision": applied.consumes_decision,
                "finished": applied.finished,
                "needs_input": applied.needs_input,
            },
            "state": applied.state,
        }
    elif command == "cancel_queue":
        canceled = bridge.cancel_queue()
        command_record = {"command": "cancel_queue"}
        response = {
            "cancel_queue": {
                "canceled": canceled.canceled,
                "consumes_decision": canceled.consumes_decision,
                "finished": canceled.finished,
                "needs_input": canceled.needs_input,
            },
            "state": canceled.state,
        }
    elif command == "set_target":
        target_index = definition.get("target_index")
        if isinstance(target_index, bool) or not isinstance(target_index, int):
            raise PrefixReconstructionGateError("set_target needs an integer target_index")
        targeted = bridge.set_target(target_index)
        command_record = {"command": "set_target", "target_index": target_index}
        response = {
            "set_target": {
                "changed": targeted.changed,
                "target_index": targeted.target_index,
                "finished": targeted.finished,
                "needs_input": targeted.needs_input,
            },
            "state": targeted.state,
        }
    elif command == "wait":
        wait_ms = definition.get("wait_ms")
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or wait_ms <= 0:
            raise PrefixReconstructionGateError("wait needs a positive integer wait_ms")
        returned_state = bridge.wait(wait_ms)
        command_record = {"command": "wait", "wait_ms": wait_ms}
        response = {"state": returned_state}
    elif command == "advance":
        returned_state = bridge.advance()
        command_record = {"command": "advance"}
        response = {"state": returned_state}
    else:
        raise PrefixReconstructionGateError(f"unsupported transcript command: {command!r}")

    state = bridge.state()
    actions = _actions_wire(bridge.actions())
    return command_record, {"response": response, "state": state}, actions


def run_episode(
    *,
    bridge_path: Path,
    request: Mapping[str, Any],
    request_sha256: str,
    lane: Mapping[str, Any],
    seed: int,
) -> JSONMap:
    steps = lane.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PrefixReconstructionGateError("lane has no steps")
    rows: list[JSONMap] = []
    invariant_failures: list[JSONMap] = []
    previous_state: Mapping[str, Any] | None = None

    with SimulatorBridge(bridge_path) as bridge:
        for ordinal, definition in enumerate(steps):
            if not isinstance(definition, Mapping):
                raise PrefixReconstructionGateError("lane step must be an object")
            command, values, actions = _execute_step(
                bridge,
                definition,
                request=request,
                request_sha256=request_sha256,
                seed=seed,
            )
            state = values["state"]
            checks = _check_step(
                definition=definition,
                response=values["response"],
                state=state,
                previous_state=previous_state,
            )
            failed = [check for check in checks if check.get("passed") is not True]
            for check in failed:
                invariant_failures.append(
                    {
                        "step_ordinal": ordinal,
                        "command": command["command"],
                        **check,
                    }
                )
            rows.append(
                {
                    "ordinal": ordinal,
                    "command": command,
                    "response": values["response"],
                    "state": state,
                    "actions": actions,
                    "invariant_checks": checks,
                }
            )
            previous_state = state

    final_state = rows[-1]["state"]
    final_damage = final_state.get("damage_done")
    if not isinstance(final_damage, (int, float)) or isinstance(final_damage, bool):
        invariant_failures.append(
            {
                "step_ordinal": len(rows) - 1,
                "command": rows[-1]["command"]["command"],
                "name": "final_damage_is_numeric",
                "passed": False,
                "actual": final_damage,
                "expected": "number",
            }
        )
    payload: JSONMap = {
        "schema_version": 1,
        "lane_id": lane["lane_id"],
        "request_id": lane["request_id"],
        "request_sha256": request_sha256,
        "absolute_seed": seed,
        "reconstruction_mode": "REPLAY_FROM_TIME_ZERO",
        "state_source": "SIMULATED_PREFIX_ONLY",
        "steps": rows,
        "final": {
            "damage_done": final_damage,
            "state_sha256": sha256_bytes(canonical_bytes(final_state)),
            "state_and_damage_sha256": sha256_bytes(
                canonical_bytes({"state": final_state, "damage_done": final_damage})
            ),
        },
        "invariants_passed": not invariant_failures,
        "invariant_failures": invariant_failures,
    }
    payload["episode_sha256"] = sha256_bytes(canonical_bytes(payload))
    return payload


def first_difference(reference: Any, replay: Any, path: str = "$") -> JSONMap | None:
    """Return a compact path/value record for the first canonical mismatch."""

    if type(reference) is not type(replay):
        return {
            "path": path,
            "reference": reference,
            "replay": replay,
            "reason": "type_mismatch",
        }
    if isinstance(reference, Mapping):
        reference_keys = sorted(reference)
        replay_keys = sorted(replay)
        if reference_keys != replay_keys:
            return {
                "path": path,
                "reference": reference_keys,
                "replay": replay_keys,
                "reason": "key_mismatch",
            }
        for key in reference_keys:
            found = first_difference(reference[key], replay[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(reference, list):
        if len(reference) != len(replay):
            return {
                "path": path,
                "reference": len(reference),
                "replay": len(replay),
                "reason": "length_mismatch",
            }
        for index, (left, right) in enumerate(zip(reference, replay)):
            found = first_difference(left, right, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if reference != replay:
        return {
            "path": path,
            "reference": reference,
            "replay": replay,
            "reason": "value_mismatch",
        }
    return None


def _source_locks() -> JSONMap:
    module_path = Path(__file__).resolve()
    test_path = PROJECT_ROOT / "tests" / "test_fury_prefix_reconstruction_gate_v1.py"
    result: JSONMap = {
        "module_path": str(module_path),
        "module_sha256": sha256_file(module_path),
    }
    if test_path.exists():
        result["test_path"] = str(test_path.resolve())
        result["test_sha256"] = sha256_file(test_path)
    return result


def _input_lock_snapshot(
    *, bridge_path: Path, config_path: Path, staged: Mapping[str, Any]
) -> JSONMap:
    return {
        "bridge_path": str(bridge_path.resolve()),
        "bridge_sha256": sha256_file(bridge_path),
        "config_path": str(config_path.resolve()),
        "config_sha256": sha256_file(config_path),
        "staged_config_path": staged["config_snapshot_path"],
        "staged_config_sha256": sha256_file(Path(str(staged["config_snapshot_path"]))),
        "request_bundle_path": staged["request_bundle_path"],
        "request_bundle_sha256": sha256_file(Path(str(staged["request_bundle_path"]))),
        "source": _source_locks(),
    }


def _validated_seeds(seeds: Sequence[int]) -> tuple[int, ...]:
    values: list[int] = []
    for value in seeds:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PrefixReconstructionGateError("absolute seeds must be positive integers")
        if value in values:
            raise PrefixReconstructionGateError(f"duplicate seed: {value}")
        values.append(value)
    if not values:
        raise PrefixReconstructionGateError("at least one absolute seed is required")
    return tuple(values)


def run_gate(
    *,
    bridge_path: Path = DEFAULT_BRIDGE,
    config_path: Path = DEFAULT_CONFIG,
    input_directory: Path = DEFAULT_INPUT_DIRECTORY,
    seeds: Sequence[int] = DEFAULT_SEEDS,
) -> JSONMap:
    """Execute reference + two fresh-process replays for every lane and seed."""

    bridge_path = bridge_path.resolve()
    config_path = config_path.resolve()
    input_directory = input_directory.resolve()
    normalized_seeds = _validated_seeds(seeds)
    if not bridge_path.is_file():
        raise PrefixReconstructionGateError(f"bridge not found: {bridge_path}")
    if not config_path.is_file():
        raise PrefixReconstructionGateError(f"configuration not found: {config_path}")

    bridge_sha = sha256_file(bridge_path)
    config_sha = sha256_file(config_path)
    if bridge_sha != EXPECTED_BRIDGE_SHA256:
        raise PrefixReconstructionGateError(
            f"bridge drift: got {bridge_sha}, expected {EXPECTED_BRIDGE_SHA256}"
        )
    if config_sha != EXPECTED_CONFIG_SHA256:
        raise PrefixReconstructionGateError(
            f"configuration drift: got {config_sha}, expected {EXPECTED_CONFIG_SHA256}"
        )

    config = load_json_object(config_path, "base configuration")
    requests = derive_requests(config, config_sha256=config_sha)
    staged = stage_inputs(
        config_path=config_path,
        config_sha256=config_sha,
        requests=requests,
        input_directory=input_directory,
    )
    input_lock_before = _input_lock_snapshot(
        bridge_path=bridge_path, config_path=config_path, staged=staged
    )

    groups: list[JSONMap] = []
    execution_failures: list[JSONMap] = []
    completed_episodes = 0
    matched_replays = 0
    mismatches = 0
    all_invariants_passed = True

    for lane in LANES:
        request_id = str(lane["request_id"])
        request = requests[request_id]
        request_sha = staged["request_sha256_by_id"][request_id]
        for seed in normalized_seeds:
            episodes: list[JSONMap | None] = []
            roles = ("reference", "replay_1", "replay_2")
            for role in roles:
                try:
                    episode = run_episode(
                        bridge_path=bridge_path,
                        request=request,
                        request_sha256=request_sha,
                        lane=lane,
                        seed=seed,
                    )
                    completed_episodes += 1
                    all_invariants_passed = (
                        all_invariants_passed and episode["invariants_passed"] is True
                    )
                    episodes.append(episode)
                except Exception as error:  # Preserve a formal FAIL artifact.
                    episodes.append(None)
                    all_invariants_passed = False
                    execution_failures.append(
                        {
                            "lane_id": lane["lane_id"],
                            "absolute_seed": seed,
                            "role": role,
                            "error_type": type(error).__name__,
                            "error": str(error),
                        }
                    )

            reference = episodes[0]
            replay_rows: list[JSONMap] = []
            for replay_index, replay in enumerate(episodes[1:], start=1):
                matched = reference is not None and replay is not None and reference == replay
                difference = (
                    None
                    if matched or reference is None or replay is None
                    else first_difference(reference, replay)
                )
                if matched:
                    matched_replays += 1
                else:
                    mismatches += 1
                replay_rows.append(
                    {
                        "role": f"replay_{replay_index}",
                        "completed": replay is not None,
                        "matched_reference": matched,
                        "episode_sha256": (
                            None if replay is None else replay["episode_sha256"]
                        ),
                        "final_state_sha256": (
                            None if replay is None else replay["final"]["state_sha256"]
                        ),
                        "final_damage_done": (
                            None if replay is None else replay["final"]["damage_done"]
                        ),
                        "first_difference": difference,
                    }
                )
            groups.append(
                {
                    "lane_id": lane["lane_id"],
                    "coverage": deepcopy(lane["coverage"]),
                    "request_id": request_id,
                    "request_sha256": request_sha,
                    "absolute_seed": seed,
                    "reference": reference,
                    "replays": replay_rows,
                }
            )

    input_lock_after = _input_lock_snapshot(
        bridge_path=bridge_path, config_path=config_path, staged=staged
    )
    input_lock_stable = input_lock_before == input_lock_after
    expected_episode_count = len(LANES) * len(normalized_seeds) * 3
    expected_replay_count = len(LANES) * len(normalized_seeds) * 2
    passed = (
        input_lock_stable
        and completed_episodes == expected_episode_count
        and matched_replays == expected_replay_count
        and mismatches == 0
        and all_invariants_passed
        and not execution_failures
    )

    return {
        "schema_version": 1,
        "kind": "fury_prefix_reconstruction_gate_v1",
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "status": "PASS" if passed else "FAIL",
        "prefix_reconstruction_gate_passed": passed,
        "scope": {
            "reconstruction_mode": "REPLAY_FROM_TIME_ZERO",
            "state_source": "SIMULATED_PREFIX_ONLY",
            "reference_definition": (
                "a fresh seed-fixed bridge process loaded from the same content-addressed "
                "request and replayed from time zero"
            ),
            "replay_definition": (
                "two additional fresh bridge processes per lane and absolute seed"
            ),
            "comparison_unit": (
                "every command response, post-command state, available-action table, "
                "terminal damage, and terminal state hash"
            ),
        },
        "capabilities": {
            "canonical_time_zero_prefix_replay": passed,
            "snapshot_restore": False,
            "rng_state_snapshot_restore": False,
            "pending_event_or_closure_snapshot_restore": False,
            "historical_midstate_seeding": False,
            "shadow_backfill": False,
        },
        "authorization": {
            "training_allowed": False,
            "voting_allowed": False,
            "deployment_allowed": False,
            "real_game_superiority_claimed": False,
        },
        "claims_excluded": [
            "snapshot or restore support",
            "seeding from a real or historical mid-encounter state",
            "backfilling omitted Shadow state",
            "exact Cat, Cat2, Contra, or WoW client execution",
            "training evidence or policy-selection voting evidence",
            "deployment authorization or real-game DPS superiority",
        ],
        "input_contract": {
            "expected_bridge_sha256": EXPECTED_BRIDGE_SHA256,
            "expected_config_sha256": EXPECTED_CONFIG_SHA256,
            "absolute_seeds": list(normalized_seeds),
            "content_addressed_inputs": staged,
            "input_lock_before": input_lock_before,
            "input_lock_after": input_lock_after,
            "input_lock_stable": input_lock_stable,
        },
        "coverage": {
            "requested_lane_count": 3,
            "implemented_lane_count": len(LANES),
            "lanes": [
                {
                    "lane_id": lane["lane_id"],
                    "request_id": lane["request_id"],
                    "coverage": deepcopy(lane["coverage"]),
                    "commands": [step["command"] for step in lane["steps"]],
                }
                for lane in LANES
            ],
            "api_coverage_substitution": {
                "requested_shape": "off-GCD+GCD/stance+cast/swing",
                "implemented_legal_shape": (
                    "Battle Stance (non-consuming action) -> Bloodrage (off-GCD) -> "
                    "Bloodthirst (GCD) -> advance through an auto-attack swing"
                ),
                "gap": (
                    "the bridge exposes no mid-state snapshot command; swing coverage is "
                    "event advancement from time zero, not an injected swing state"
                ),
            },
            "queue_activation_boundary": {
                "observed_contract": (
                    "a queue cast is accepted immediately but its queue aura is installed by "
                    "a delayed pending action; the cancel lane therefore waits 500 ms and "
                    "advances before issuing cancel_queue"
                ),
                "unexported_state": (
                    "the accepted-but-not-yet-active pending queue is not represented in the "
                    "exported state or available-action rows"
                ),
                "bridge_modified": False,
            },
        },
        "execution": {
            "process_isolation": "ONE_FRESH_BRIDGE_PROCESS_PER_EPISODE",
            "reference_processes_per_lane_seed": 1,
            "replay_processes_per_lane_seed": 2,
            "expected_episode_count": expected_episode_count,
            "completed_episode_count": completed_episodes,
            "expected_replay_count": expected_replay_count,
            "matched_replay_count": matched_replays,
            "mismatch_count": mismatches,
            "all_invariants_passed": all_invariants_passed,
            "execution_failures": execution_failures,
            "groups": groups,
        },
        "limitations": [
            "Only deterministic simulator prefixes reconstructed from time zero were tested.",
            "The bridge has no snapshot, restore, RNG-state export, pending-event export, or historical-state import command.",
            "The fixed requests are synthetic 12-second one- and three-target simulator encounters derived from one content-addressed configuration.",
            "A PASS does not establish that sparse Shadow observations contain enough state to reconstruct their real mid-encounter decision points.",
        ],
    }


def write_artifacts(report: Mapping[str, Any], *, output: Path, receipt: Path) -> JSONMap:
    output.parent.mkdir(parents=True, exist_ok=True)
    report_payload = json.dumps(
        report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    output.write_bytes(report_payload)
    report_sha = sha256_bytes(report_payload)
    receipt_document: JSONMap = {
        "schema_version": 1,
        "kind": "fury_prefix_reconstruction_gate_v1_receipt",
        "created_at": report.get("created_at"),
        "artifact_path": str(output.resolve()),
        "artifact_sha256": report_sha,
        "artifact_bytes": len(report_payload),
        "status": report.get("status"),
        "prefix_reconstruction_gate_passed": report.get(
            "prefix_reconstruction_gate_passed"
        ),
        "bridge_sha256": report.get("input_contract", {}).get(
            "expected_bridge_sha256"
        ),
        "config_sha256": report.get("input_contract", {}).get(
            "expected_config_sha256"
        ),
        "request_bundle_sha256": report.get("input_contract", {})
        .get("content_addressed_inputs", {})
        .get("request_bundle_sha256"),
        "module_sha256": sha256_file(Path(__file__).resolve()),
        "test_sha256": (
            sha256_file(PROJECT_ROOT / "tests" / "test_fury_prefix_reconstruction_gate_v1.py")
            if (PROJECT_ROOT / "tests" / "test_fury_prefix_reconstruction_gate_v1.py").exists()
            else None
        ),
        "authorization": {
            "training_allowed": False,
            "voting_allowed": False,
            "deployment_allowed": False,
            "real_game_superiority_claimed": False,
        },
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt_payload = json.dumps(
        receipt_document, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    receipt.write_bytes(receipt_payload)
    return {
        "report_path": str(output.resolve()),
        "report_sha256": report_sha,
        "receipt_path": str(receipt.resolve()),
        "receipt_sha256": sha256_bytes(receipt_payload),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-directory", type=Path, default=DEFAULT_INPUT_DIRECTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_gate(
            bridge_path=args.bridge,
            config_path=args.config,
            input_directory=args.input_directory,
            seeds=args.seeds,
        )
        receipt = write_artifacts(report, output=args.output, receipt=args.receipt)
    except Exception as error:
        print(f"prefix reconstruction gate failed before artifact completion: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], **receipt}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
