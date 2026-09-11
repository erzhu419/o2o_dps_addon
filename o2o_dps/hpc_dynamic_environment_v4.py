"""Fail-closed dynamic-v4 bridge installation and bounded HPC pilot.

This module is infrastructure validation only.  It installs one pinned v4
Linux bridge into a v4-specific content-addressed release and activates only
``current-bridge-dynamic-v4``.  The legacy ``current-bridge`` pointer used by
v3 is neither inspected nor modified.

Each configured node must first complete one real ``load_dynamic_v1`` trace.
Only then may the bounded 32-process-per-node startup/round-trip pilot run.
No policy, score, training job, Chronicle archive, or offline corpus enters
this contract.
"""

from __future__ import annotations

import argparse
import base64
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from .fury_bridge_platform_equivalence_v2 import (
    HEALTH_STAT_INDEX,
    TARGET_HEALTH,
    build_dynamic_fixture_v2,
    load_request,
)
from .hpc_environment_v1 import (
    CommandResult,
    HpcEnvironmentError,
    HpcSite,
    SchedulerNodeTransport,
    SiteNode,
    WslControlTransport,
    _fail_fast,
    _parse_key_values,
    _remote_hash_test,
    _sha256_file,
    _site_identity,
    bootstrap_control,
    bootstrap_node,
    inspect_bridge,
    load_site_config,
    probe_control,
    probe_node,
)


JSONMap = dict[str, Any]

CONTRACT_SCHEMA = "o2o_hpc_dynamic_environment/v4"
RECEIPT_SCHEMA = "o2o_hpc_dynamic_environment_receipt/v4"
PROBE_SUMMARY_SCHEMA = "o2o_hpc_dynamic_probe_summary/v4"
EXPECTED_BRIDGE_SHA256 = (
    "93015dd74ce436c171b50b41d9b2059249c6dbbea64614c1a472c9bda2ad838b"
)
EXPECTED_BRIDGE_FILENAME = (
    "o2obridge.seedfix-v4.withdb.goamd64v1.linux-amd64"
)
EXPECTED_NODES = tuple(f"node{index:03d}" for index in range(1, 7))
V4_POINTER_NAME = "current-bridge-dynamic-v4"
V3_POINTER_NAME = "current-bridge"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_CONFIG = PROJECT_ROOT / "configs" / "hpc" / "site.local.json"
DEFAULT_CONTRACT = (
    PROJECT_ROOT / "configs" / "hpc" / "dynamic_v4.example.json"
)
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / EXPECTED_BRIDGE_FILENAME
DEFAULT_RECEIPT_DIRECTORY = PROJECT_ROOT / ".hpc-local" / "receipts"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_FIELDS = frozenset(
    {"schema", "bridge", "route", "nodes", "dynamic_probe", "pilot"}
)
_BRIDGE_FIELDS = frozenset({"filename", "sha256"})
_ROUTE_FIELDS = frozenset({"control_plane_alias", "node_transport_kind"})
_DYNAMIC_FIELDS = frozenset(
    {
        "base_request",
        "seed_base",
        "target_count",
        "health_stat_index",
        "target_health",
        "same_timestamp_order",
        "retarget_mode",
    }
)
_PILOT_FIELDS = frozenset(
    {
        "workers_per_node",
        "total_workers",
        "gomaxprocs_per_worker",
        "worker_timeout_seconds",
        "node_timeout_seconds",
    }
)
_PROBE_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "status",
        "hostname",
        "bridge_sha256",
        "dynamic_config_sha256",
        "worker_count",
        "exit_zero_count",
        "validated_count",
        "deterministic_workload_sha256",
        "max_rss_kib",
        "elapsed_ms",
        "failures",
    }
)
_PROBE_MARKER = "O2O_DYNAMIC_V4_PROBE="
_MAX_DIAGNOSTIC_CHARS = 4096


class HpcDynamicEnvironmentV4Error(RuntimeError):
    """The frozen v4 contract, route, bridge, or pilot failed validation."""


@dataclass(frozen=True)
class DynamicV4Contract:
    schema: str
    bridge_filename: str
    bridge_sha256: str
    control_plane_alias: str
    node_transport_kind: str
    nodes: tuple[str, ...]
    base_request: str
    seed_base: int
    target_count: int
    health_stat_index: int
    target_health: tuple[float, ...]
    same_timestamp_order: str
    retarget_mode: str
    workers_per_node: int
    total_workers: int
    gomaxprocs_per_worker: int
    worker_timeout_seconds: int
    node_timeout_seconds: int
    content_sha256: str
    source_path: Path


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
        raise HpcDynamicEnvironmentV4Error(
            f"value is not finite canonical JSON: {error}"
        ) from error


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise HpcDynamicEnvironmentV4Error(
            f"{label} fields mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _mapping(value: Any, label: str) -> JSONMap:
    if not isinstance(value, dict):
        raise HpcDynamicEnvironmentV4Error(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HpcDynamicEnvironmentV4Error(f"{label} must be an array")
    return value


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HpcDynamicEnvironmentV4Error(f"{label} must be an integer")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise HpcDynamicEnvironmentV4Error(
            f"{label} must be lowercase SHA-256"
        )
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HpcDynamicEnvironmentV4Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise HpcDynamicEnvironmentV4Error(f"{label} must be finite")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_fields(pairs: Sequence[tuple[str, Any]]) -> JSONMap:
    result: JSONMap = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def _strict_json_document(path: Path) -> tuple[JSONMap, bytes]:
    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_fields,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HpcDynamicEnvironmentV4Error(
            f"could not load dynamic-v4 contract {resolved}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HpcDynamicEnvironmentV4Error("dynamic-v4 contract must be an object")
    return value, raw


def load_dynamic_v4_contract(path: Path = DEFAULT_CONTRACT) -> DynamicV4Contract:
    value, raw = _strict_json_document(path)
    _exact_fields(value, _TOP_FIELDS, "dynamic-v4 contract")
    if value.get("schema") != CONTRACT_SCHEMA:
        raise HpcDynamicEnvironmentV4Error(
            f"dynamic-v4 schema must be {CONTRACT_SCHEMA}"
        )

    bridge = _mapping(value.get("bridge"), "bridge")
    route = _mapping(value.get("route"), "route")
    dynamic = _mapping(value.get("dynamic_probe"), "dynamic_probe")
    pilot = _mapping(value.get("pilot"), "pilot")
    _exact_fields(bridge, _BRIDGE_FIELDS, "bridge")
    _exact_fields(route, _ROUTE_FIELDS, "route")
    _exact_fields(dynamic, _DYNAMIC_FIELDS, "dynamic_probe")
    _exact_fields(pilot, _PILOT_FIELDS, "pilot")

    if bridge.get("filename") != EXPECTED_BRIDGE_FILENAME:
        raise HpcDynamicEnvironmentV4Error(
            "bridge filename must identify the pinned seedfix-v4 binary"
        )
    bridge_sha = _sha256(bridge.get("sha256"), "bridge.sha256")
    if bridge_sha != EXPECTED_BRIDGE_SHA256:
        raise HpcDynamicEnvironmentV4Error(
            "bridge SHA-256 must equal the pinned dynamic-v4 binary"
        )
    if (
        route.get("control_plane_alias") != "jtl110gpu2"
        or route.get("node_transport_kind") != "scheduler_run_on"
    ):
        raise HpcDynamicEnvironmentV4Error(
            "dynamic-v4 route must be jtl110gpu2 plus scheduler_run_on"
        )

    nodes_raw = _list(value.get("nodes"), "nodes")
    if tuple(nodes_raw) != EXPECTED_NODES:
        raise HpcDynamicEnvironmentV4Error(
            "dynamic-v4 nodes must be exactly node001 through node006"
        )
    base_request = dynamic.get("base_request")
    if base_request != "configs/wowsims/fury_warrior_phase1.json":
        raise HpcDynamicEnvironmentV4Error(
            "dynamic probe base_request is not the frozen request"
        )
    seed_base = _strict_int(dynamic.get("seed_base"), "seed_base")
    target_count = _strict_int(dynamic.get("target_count"), "target_count")
    health_index = _strict_int(
        dynamic.get("health_stat_index"), "health_stat_index"
    )
    health_raw = _list(dynamic.get("target_health"), "target_health")
    if (
        target_count != 2
        or health_index != HEALTH_STAT_INDEX
        or len(health_raw) != 2
        or tuple(
            _finite_number(value, f"target_health[{index}]")
            for index, value in enumerate(health_raw)
        )
        != TARGET_HEALTH
        or dynamic.get("same_timestamp_order") != "BACKGROUND_BEFORE_CANDIDATE"
        or dynamic.get("retarget_mode") != "NEXT_ALIVE_CYCLIC"
    ):
        raise HpcDynamicEnvironmentV4Error(
            "dynamic probe mechanics differ from the frozen two-target fixture"
        )

    workers = _strict_int(pilot.get("workers_per_node"), "workers_per_node")
    total = _strict_int(pilot.get("total_workers"), "total_workers")
    gomaxprocs = _strict_int(
        pilot.get("gomaxprocs_per_worker"), "gomaxprocs_per_worker"
    )
    worker_timeout = _strict_int(
        pilot.get("worker_timeout_seconds"), "worker_timeout_seconds"
    )
    node_timeout = _strict_int(
        pilot.get("node_timeout_seconds"), "node_timeout_seconds"
    )
    if (
        workers != 32
        or total != 192
        or total != workers * len(EXPECTED_NODES)
        or gomaxprocs != 1
        or worker_timeout < 10
        or worker_timeout > 60
        or node_timeout < worker_timeout
        or node_timeout > 300
    ):
        raise HpcDynamicEnvironmentV4Error(
            "pilot must be exactly 32 workers/node, 192 total, GOMAXPROCS=1"
        )

    return DynamicV4Contract(
        schema=CONTRACT_SCHEMA,
        bridge_filename=EXPECTED_BRIDGE_FILENAME,
        bridge_sha256=bridge_sha,
        control_plane_alias="jtl110gpu2",
        node_transport_kind="scheduler_run_on",
        nodes=EXPECTED_NODES,
        base_request=base_request,
        seed_base=seed_base,
        target_count=target_count,
        health_stat_index=health_index,
        target_health=tuple(
            _finite_number(value, f"target_health[{index}]")
            for index, value in enumerate(health_raw)
        ),
        same_timestamp_order=str(dynamic["same_timestamp_order"]),
        retarget_mode=str(dynamic["retarget_mode"]),
        workers_per_node=workers,
        total_workers=total,
        gomaxprocs_per_worker=gomaxprocs,
        worker_timeout_seconds=worker_timeout,
        node_timeout_seconds=node_timeout,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        source_path=path.expanduser().resolve(),
    )


def validate_site_binding_v4(site: HpcSite, contract: DynamicV4Contract) -> None:
    if not isinstance(site, HpcSite):
        raise TypeError("site must be HpcSite")
    if not isinstance(contract, DynamicV4Contract):
        raise TypeError("contract must be DynamicV4Contract")
    if site.control_ssh_alias != contract.control_plane_alias:
        raise HpcDynamicEnvironmentV4Error(
            "site control plane is not the pinned jtl110gpu2 route"
        )
    if site.node_transport_kind != contract.node_transport_kind:
        raise HpcDynamicEnvironmentV4Error(
            "site node transport is not scheduler_run_on"
        )
    if tuple(node.name for node in site.nodes) != contract.nodes:
        raise HpcDynamicEnvironmentV4Error(
            "site logical nodes differ from node001 through node006"
        )
    if tuple(node.transport_name for node in site.nodes) != contract.nodes:
        raise HpcDynamicEnvironmentV4Error(
            "site scheduler targets differ from node001 through node006"
        )
    if site.pilot_workers_per_node != contract.workers_per_node:
        raise HpcDynamicEnvironmentV4Error(
            "site pilot worker count differs from the v4 contract"
        )
    if site.maximum_workers_per_node_before_benchmark < contract.workers_per_node:
        raise HpcDynamicEnvironmentV4Error(
            "site maximum worker bound is below the v4 pilot"
        )


def verify_local_v4_bridge(
    path: Path, contract: DynamicV4Contract
) -> JSONMap:
    resolved = path.expanduser().resolve()
    if resolved.name != contract.bridge_filename or "seedfix-v4" not in resolved.name:
        raise HpcDynamicEnvironmentV4Error(
            "bridge filename is not the pinned dynamic-v4 artifact"
        )
    try:
        identity = inspect_bridge(resolved)
    except HpcEnvironmentError as error:
        raise HpcDynamicEnvironmentV4Error(str(error)) from error
    if identity.get("sha256") != contract.bridge_sha256:
        raise HpcDynamicEnvironmentV4Error(
            "bridge SHA-256 differs from the pinned dynamic-v4 artifact"
        )
    return identity


def build_dynamic_probe_payload_v4(
    contract: DynamicV4Contract,
) -> tuple[bytes, JSONMap]:
    request_path = PROJECT_ROOT / Path(contract.base_request)
    _, base_request = load_request(request_path)
    request, config = build_dynamic_fixture_v2(base_request)
    if (
        len(config.target_health) != contract.target_count
        or tuple(value.health for value in config.target_health)
        != contract.target_health
        or config.same_timestamp_order != contract.same_timestamp_order
        or config.retarget_mode != contract.retarget_mode
    ):
        raise HpcDynamicEnvironmentV4Error(
            "materialized dynamic fixture differs from the v4 contract"
        )
    messages = (
        {
            "command": "load_dynamic_v1",
            "request": request,
            "seed": contract.seed_base,
            "dynamic": config.to_wire(),
        },
        {"command": "wait", "wait_ms": 250},
        {"command": "advance"},
        {"command": "dynamic_damage_receipts", "cursor": 0},
        {"command": "dynamic_candidate_damage_receipts", "cursor": 0},
        {"command": "state"},
        {"command": "close"},
    )
    payload = b"".join(canonical_bytes(message) + b"\n" for message in messages)
    return payload, {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size_bytes": len(payload),
        "command_count": len(messages),
        "dynamic_config_sha256": config.content_sha256,
        "target_count": contract.target_count,
        "target_health": list(contract.target_health),
        "health_stat_index": contract.health_stat_index,
        "seed_base": contract.seed_base,
        "probe_program_sha256": hashlib.sha256(
            _REMOTE_PROBE_PROGRAM.encode("utf-8")
        ).hexdigest(),
    }


_REMOTE_PROBE_PROGRAM = r'''
import concurrent.futures
import copy
import hashlib
import json
import os
import resource
import subprocess
import sys
import threading
import time

MARKER = "O2O_DYNAMIC_V4_PROBE="
SCHEMA = "o2o_hpc_dynamic_probe_summary/v4"
COMMANDS = [
    "load_dynamic_v1", "wait", "advance", "dynamic_damage_receipts",
    "dynamic_candidate_damage_receipts", "state", "close",
]

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def need(condition, message):
    if not condition:
        raise ValueError(message)

def dynamic(state, digest, live):
    need(state.get("total_target_count") == 2, "total_target_count")
    need(state.get("num_targets") == live, "num_targets")
    need(state.get("target_health_known") is True, "target_health_known")
    value = state.get("dynamic_team_background")
    need(isinstance(value, dict), "dynamic state")
    need(value.get("schema") == "o2o_dynamic_team_background/v1", "dynamic schema")
    need(value.get("config_digest") == digest, "dynamic digest")
    need(value.get("same_timestamp_order") == "BACKGROUND_BEFORE_CANDIDATE", "order")
    need(value.get("retarget_mode") == "NEXT_ALIVE_CYCLIC", "retarget")
    targets = value.get("targets")
    need(isinstance(targets, list) and len(targets) == 2, "targets")
    need([row.get("target_index") for row in targets] == [0, 1], "target identity")
    need([float(row.get("initial_health")) for row in targets] == [1.0, 100000.0], "health")
    return value

def validate(stdout, expected_digest):
    lines = stdout.splitlines()
    need(len(lines) == len(COMMANDS), "response line count")
    rows = [json.loads(line) for line in lines]
    for expected, row in zip(COMMANDS, rows):
        need(isinstance(row, dict), "response type")
        need(row.get("ok") is True and row.get("command") == expected, "command ack")
    load = rows[0]
    receipt = load.get("dynamic_load")
    need(isinstance(receipt, dict), "load receipt")
    need(receipt.get("config_digest") == expected_digest, "load digest")
    need(receipt.get("target_count") == 2 and receipt.get("background_event_count") == 2, "load counts")
    initial = dynamic(load.get("state"), expected_digest, 2)
    need(all(row.get("dead") is False for row in initial["targets"]), "initial dead")
    waited = rows[1].get("state")
    dynamic(waited, expected_digest, 2)
    advanced = rows[2].get("state")
    after = dynamic(advanced, expected_digest, 1)
    need(advanced.get("time_ms") == 250 and advanced.get("target_index") == 1, "retarget state")
    need(after["targets"][0].get("dead") is True and after["targets"][0].get("death_time_ms") == 10, "death")
    need(after["targets"][1].get("dead") is False, "live target")
    background = rows[3].get("dynamic_damage_receipts")
    need(isinstance(background, dict) and background.get("config_digest") == expected_digest, "background batch")
    br = background.get("receipts")
    need(isinstance(br, list) and len(br) == 2, "background receipts")
    need(br[0].get("status") == "APPLIED" and br[0].get("killed") is True and br[0].get("retargeted_to") == 1, "kill receipt")
    need(br[1].get("status") == "CANCELED_TARGET_DEAD", "dead background cancel")
    candidate = rows[4].get("dynamic_candidate_damage_receipts")
    need(isinstance(candidate, dict) and candidate.get("config_digest") == expected_digest, "candidate batch")
    cr = candidate.get("receipts")
    need(isinstance(cr, list) and len(cr) == 1, "candidate receipts")
    need(cr[0].get("status") == "CANCELED_TARGET_DEAD" and cr[0].get("resolution_phase") == "TARGET_DIED_AFTER_OUTCOME", "candidate cancel")
    need(br[0].get("damage_ordinal") < cr[0].get("damage_ordinal") < br[1].get("damage_ordinal"), "same-ms ordering")
    need(rows[5].get("state") == advanced, "state round trip")
    need(rows[6].get("ok") is True and rows[6].get("command") == "close", "close")
    return {
        "response_sha256": hashlib.sha256(stdout).hexdigest(),
        "final_state_sha256": hashlib.sha256(canonical(advanced)).hexdigest(),
        "damage_ordinals": [br[0]["damage_ordinal"], cr[0]["damage_ordinal"], br[1]["damage_ordinal"]],
    }

bridge, mode, worker_text, payload_b64, bridge_sha, dynamic_sha, seed_text, timeout_text, gomax_text = sys.argv[1:]
workers = int(worker_text)
seed_base = int(seed_text)
timeout = int(timeout_text)
gomax = int(gomax_text)
template = [json.loads(line) for line in __import__("base64").b64decode(payload_b64).splitlines()]
barrier = threading.Barrier(workers)

bridge_digest = hashlib.sha256()
with open(bridge, "rb") as bridge_file:
    while True:
        chunk = bridge_file.read(1024 * 1024)
        if not chunk:
            break
        bridge_digest.update(chunk)
need(bridge_digest.hexdigest() == bridge_sha, "bridge sha256")

def one(index):
    messages = copy.deepcopy(template)
    seed = seed_base + index
    messages[0]["seed"] = seed
    payload = b"".join(canonical(message) + b"\n" for message in messages)
    environment = os.environ.copy()
    environment["GOMAXPROCS"] = str(gomax)
    barrier.wait(timeout=timeout)
    try:
        result = subprocess.run([bridge], input=payload, capture_output=True, timeout=timeout, env=environment)
    except Exception as error:
        return {"worker_index": index, "seed": seed, "exit_code": 255, "error": type(error).__name__}
    row = {"worker_index": index, "seed": seed, "exit_code": result.returncode}
    if result.returncode != 0:
        row["error"] = result.stderr.decode("utf-8", "replace")[-512:]
        return row
    try:
        row.update(validate(result.stdout, dynamic_sha))
    except Exception as error:
        row["error"] = type(error).__name__ + ":" + str(error)
    return row

started = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
    rows = list(pool.map(one, range(workers)))
elapsed_ms = max(1, int(round((time.monotonic() - started) * 1000)))
rows.sort(key=lambda row: row["worker_index"])
exit_zero = sum(row.get("exit_code") == 0 for row in rows)
validated = sum("response_sha256" in row and "error" not in row for row in rows)
failures = [row for row in rows if "error" in row or row.get("exit_code") != 0]
deterministic_rows = [
    {key: row[key] for key in ("worker_index", "seed", "exit_code", "response_sha256", "final_state_sha256", "damage_ordinals")}
    for row in rows if "response_sha256" in row and "error" not in row
]
summary = {
    "schema": SCHEMA,
    "mode": mode,
    "status": "READY" if exit_zero == workers and validated == workers and not failures else "BLOCKED",
    "hostname": os.uname().nodename,
    "bridge_sha256": bridge_sha,
    "dynamic_config_sha256": dynamic_sha,
    "worker_count": workers,
    "exit_zero_count": exit_zero,
    "validated_count": validated,
    "deterministic_workload_sha256": hashlib.sha256(canonical(deterministic_rows)).hexdigest(),
    "max_rss_kib": int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss),
    "elapsed_ms": elapsed_ms,
    "failures": failures[:8],
}
print(MARKER + json.dumps(summary, sort_keys=True, separators=(",", ":")))
raise SystemExit(0 if summary["status"] == "READY" else 2)
'''.strip()


def remote_probe_command_v4(
    bridge_path: str,
    *,
    mode: str,
    workers: int,
    payload: bytes,
    contract: DynamicV4Contract,
    payload_metadata: Mapping[str, Any],
    python_executable: str = "python3",
) -> str:
    if mode not in {"smoke", "pilot"}:
        raise HpcDynamicEnvironmentV4Error("probe mode must be smoke or pilot")
    expected_workers = 1 if mode == "smoke" else contract.workers_per_node
    if workers != expected_workers:
        raise HpcDynamicEnvironmentV4Error(
            f"{mode} worker count must be exactly {expected_workers}"
        )
    if not isinstance(payload, bytes) or not payload:
        raise HpcDynamicEnvironmentV4Error("probe payload must be nonempty bytes")
    dynamic_sha = _sha256(
        payload_metadata.get("dynamic_config_sha256"),
        "dynamic config SHA-256",
    )
    encoded_program = base64.b64encode(
        _REMOTE_PROBE_PROGRAM.encode("utf-8")
    ).decode("ascii")
    encoded_payload = base64.b64encode(payload).decode("ascii")
    python_code = (
        "import base64,sys;"
        "program=base64.b64decode(sys.argv[1]);"
        "sys.argv=sys.argv[1:];"
        "exec(program)"
    )
    invocation = " ".join(
        shlex.quote(value)
        for value in (
            python_executable,
            "-c",
            python_code,
            encoded_program,
            bridge_path,
            mode,
            str(workers),
            encoded_payload,
            contract.bridge_sha256,
            dynamic_sha,
            str(contract.seed_base),
            str(contract.worker_timeout_seconds),
            str(contract.gomaxprocs_per_worker),
        )
    )
    return _fail_fast(
        "export LC_ALL=C",
        "cd || exit 90",
        f"probe_mode={shlex.quote(mode)}",
        f"probe_workers={workers}",
        f"probe_gomaxprocs={contract.gomaxprocs_per_worker}",
        "export GOMAXPROCS=$probe_gomaxprocs",
        f"test \"$probe_mode\" = {shlex.quote(mode)}",
        f"test \"$probe_workers\" -eq {workers}",
        invocation,
    )


def _diagnostic_tail(value: str) -> str:
    return value[-_MAX_DIAGNOSTIC_CHARS:]


def parse_probe_summary_v4(
    result: CommandResult,
    *,
    node_name: str,
    mode: str,
    workers: int,
    bridge_sha256: str,
    dynamic_config_sha256: str,
) -> JSONMap:
    marker_lines = [
        row[len(_PROBE_MARKER) :]
        for row in result.stdout.splitlines()
        if row.startswith(_PROBE_MARKER)
    ]
    line = marker_lines[0] if len(marker_lines) == 1 else None
    parsed: JSONMap | None = None
    if line is not None:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                parsed = value
        except json.JSONDecodeError:
            parsed = None
    ready = False
    if parsed is not None:
        try:
            _exact_fields(parsed, _PROBE_FIELDS, "probe summary")
            ready = (
                result.returncode == 0
                and parsed.get("schema") == PROBE_SUMMARY_SCHEMA
                and parsed.get("mode") == mode
                and parsed.get("status") == "READY"
                and parsed.get("bridge_sha256") == bridge_sha256
                and parsed.get("dynamic_config_sha256")
                == dynamic_config_sha256
                and parsed.get("worker_count") == workers
                and parsed.get("exit_zero_count") == workers
                and parsed.get("validated_count") == workers
                and isinstance(parsed.get("hostname"), str)
                and bool(parsed.get("hostname"))
                and _SHA256_RE.fullmatch(
                    str(parsed.get("deterministic_workload_sha256"))
                )
                is not None
                and isinstance(parsed.get("max_rss_kib"), int)
                and not isinstance(parsed.get("max_rss_kib"), bool)
                and parsed.get("max_rss_kib") > 0
                and isinstance(parsed.get("elapsed_ms"), int)
                and not isinstance(parsed.get("elapsed_ms"), bool)
                and parsed.get("elapsed_ms") > 0
                and parsed.get("failures") == []
            )
        except HpcDynamicEnvironmentV4Error:
            ready = False
    response: JSONMap = {
        "name": node_name,
        "mode": mode,
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "PROBE_SUMMARY_OR_WORKER_ACCOUNTING_FAILED",
        "transport_returncode": result.returncode,
        "summary": copy.deepcopy(parsed),
        "summary_sha256": (
            hashlib.sha256(canonical_bytes(parsed)).hexdigest()
            if parsed is not None
            else None
        ),
        "diagnostic_stdout_tail": (
            "" if ready else _diagnostic_tail(result.stdout)
        ),
        "diagnostic_stderr_tail": (
            "" if ready else _diagnostic_tail(result.stderr)
        ),
    }
    return response


def _v4_release_paths(site: HpcSite, digest: str) -> tuple[str, str, str]:
    release_dir = f"{site.node_shared_root}/releases/dynamic-v4/{digest}"
    bridge_path = f"{release_dir}/o2obridge.linux-amd64"
    pointer = f"{site.node_shared_root}/{V4_POINTER_NAME}"
    return release_dir, bridge_path, pointer


def _release_state_command(release_dir: str) -> str:
    quoted = shlex.quote(release_dir)
    return _fail_fast(
        "cd || exit 90",
        (
            f"if test -e {quoted} || test -L {quoted}; "
            "then printf 'release_state=EXISTS\\n'; "
            "else printf 'release_state=ABSENT\\n'; fi"
        ),
    )


def _hash_verify_command(
    bridge_path: str,
    digest: str,
    *,
    pointer: str | None = None,
    expected_pointer_target: str | None = None,
) -> str:
    quoted_bridge = shlex.quote(bridge_path)
    commands = ["export LC_ALL=C", "cd || exit 90"]
    if pointer is not None:
        if expected_pointer_target is None:
            raise HpcDynamicEnvironmentV4Error("pointer target is required")
        commands.extend(
            (
                f"test -L {shlex.quote(pointer)}",
                f"pointer_target=$(readlink -- {shlex.quote(pointer)})",
                f"test \"$pointer_target\" = {shlex.quote(expected_pointer_target)}",
            )
        )
    commands.extend(
        (
            f"test -f {quoted_bridge} && test -x {quoted_bridge} && test ! -L {quoted_bridge}",
            _remote_hash_test(bridge_path, digest),
            f"entry_marks=$(find -H {shlex.quote(str(PurePosixPath(bridge_path).parent))} -mindepth 1 -maxdepth 1 -printf x)",
            "test \"$entry_marks\" = x",
            f"elf_magic=$(od -An -t x1 -N 4 {quoted_bridge} | tr -d '[:space:]')",
            "test \"$elf_magic\" = 7f454c46",
            f"elf_machine=$(od -An -t u2 -j 18 -N 2 {quoted_bridge} | tr -d '[:space:]')",
            "test \"$elf_machine\" = 62",
            f"printf 'verified_sha256={digest}\\n'",
            "printf 'v4_hash_status=EXACT_ELF64_X86_64_READY\\n'",
        )
    )
    if pointer is not None:
        commands.append("printf 'pointer_target=%s\\n' \"$pointer_target\"")
    return _fail_fast(*commands)


def _hash_all_nodes(
    site: HpcSite,
    transport: Any,
    *,
    bridge_path: str,
    digest: str,
    pointer: str | None = None,
    expected_pointer_target: str | None = None,
) -> list[JSONMap]:
    rows: list[JSONMap] = []
    for node in site.nodes:
        result = transport.run(
            node,
            _hash_verify_command(
                bridge_path,
                digest,
                pointer=pointer,
                expected_pointer_target=expected_pointer_target,
            ),
            timeout=60,
        )
        values = _parse_key_values(result.stdout)
        ready = (
            result.returncode == 0
            and values.get("verified_sha256") == digest
            and values.get("v4_hash_status") == "EXACT_ELF64_X86_64_READY"
            and (
                pointer is None
                or values.get("pointer_target") == expected_pointer_target
            )
        )
        rows.append(
            {
                "name": node.name,
                "status": "READY" if ready else "BLOCKED",
                "reason": None if ready else "V4_HASH_OR_POINTER_VERIFICATION_FAILED",
                "verified_sha256": values.get("verified_sha256"),
                "pointer_target": values.get("pointer_target"),
                "transport_returncode": result.returncode,
                "diagnostic_stderr_tail": _diagnostic_tail(result.stderr),
            }
        )
    return rows


def _run_probe_on_node(
    site: HpcSite,
    transport: Any,
    node: SiteNode,
    *,
    bridge_path: str,
    mode: str,
    workers: int,
    payload: bytes,
    contract: DynamicV4Contract,
    payload_metadata: Mapping[str, Any],
) -> JSONMap:
    command = remote_probe_command_v4(
        bridge_path,
        mode=mode,
        workers=workers,
        payload=payload,
        contract=contract,
        payload_metadata=payload_metadata,
        python_executable=site.node_python,
    )
    result = transport.run(
        node,
        command,
        timeout=(
            contract.worker_timeout_seconds + 30
            if mode == "smoke"
            else contract.node_timeout_seconds
        ),
    )
    parsed = parse_probe_summary_v4(
        result,
        node_name=node.name,
        mode=mode,
        workers=workers,
        bridge_sha256=contract.bridge_sha256,
        dynamic_config_sha256=str(
            payload_metadata["dynamic_config_sha256"]
        ),
    )
    parsed["command_sha256"] = hashlib.sha256(
        command.encode("utf-8")
    ).hexdigest()
    return parsed


def stage_dynamic_v4_bridge(
    site: HpcSite,
    transport: Any,
    bridge_path: Path,
    bridge_identity: Mapping[str, Any],
    *,
    payload: bytes,
    contract: DynamicV4Contract,
    payload_metadata: Mapping[str, Any],
) -> JSONMap:
    digest = str(bridge_identity.get("sha256"))
    if digest != contract.bridge_sha256:
        raise HpcDynamicEnvironmentV4Error("staging bridge identity is not v4")
    release_dir, final_bridge, pointer = _v4_release_paths(site, digest)
    primary = site.nodes[0]
    release_state_result = transport.run(
        primary, _release_state_command(release_dir), timeout=35
    )
    release_state = (
        _parse_key_values(release_state_result.stdout).get("release_state")
        if release_state_result.returncode == 0
        else None
    )
    install_status = "REUSED_IDENTICAL"
    if release_state == "EXISTS":
        verification = _hash_all_nodes(
            site,
            transport,
            bridge_path=final_bridge,
            digest=digest,
        )
        if not all(row["status"] == "READY" for row in verification):
            return {
                "status": "BLOCKED",
                "reason": "EXISTING_V4_RELEASE_INVALID",
                "install_status": install_status,
                "release_hash_verification": verification,
                "smoke": [],
                "pointer_verification": [],
            }
    elif release_state == "ABSENT":
        install_status = "INSTALLED_NEW"
        token = uuid.uuid4().hex
        incoming_dir = (
            f"{site.node_shared_root}/incoming/dynamic-v4-{digest}-{token}"
        )
        incoming_bridge = f"{incoming_dir}/o2obridge.linux-amd64"
        prepare = transport.run(
            primary,
            _fail_fast(
                "umask 077",
                "cd || exit 90",
                f"test ! -e {shlex.quote(incoming_dir)} && test ! -L {shlex.quote(incoming_dir)}",
                f"mkdir -- {shlex.quote(incoming_dir)}",
            ),
            timeout=35,
        )
        if prepare.returncode != 0:
            return {
                "status": "BLOCKED",
                "reason": "V4_STAGING_PREPARE_FAILED",
                "install_status": install_status,
                "diagnostic_stderr_tail": _diagnostic_tail(prepare.stderr),
            }
        copied = transport.copy(
            primary,
            bridge_path,
            f"~/{incoming_bridge}",
            timeout=240,
        )
        if copied.returncode != 0:
            return {
                "status": "BLOCKED",
                "reason": "V4_COPY_FAILED_INCOMING_PRESERVED",
                "install_status": install_status,
                "incoming_relative_path": incoming_dir,
                "diagnostic_stderr_tail": _diagnostic_tail(copied.stderr),
            }
        install = transport.run(
            primary,
            _fail_fast(
                "umask 077",
                "cd || exit 90",
                f"test -f {shlex.quote(incoming_bridge)} && test ! -L {shlex.quote(incoming_bridge)}",
                _remote_hash_test(incoming_bridge, digest),
                f"chmod 700 {shlex.quote(incoming_bridge)}",
                f"mkdir -p -- {shlex.quote(str(PurePosixPath(release_dir).parent))}",
                f"test ! -e {shlex.quote(release_dir)} && test ! -L {shlex.quote(release_dir)}",
                f"mv -- {shlex.quote(incoming_dir)} {shlex.quote(release_dir)}",
            ),
            timeout=60,
        )
        if install.returncode != 0:
            return {
                "status": "BLOCKED",
                "reason": "V4_VERIFY_OR_INSTALL_FAILED_INCOMING_PRESERVED",
                "install_status": install_status,
                "incoming_relative_path": incoming_dir,
                "diagnostic_stderr_tail": _diagnostic_tail(install.stderr),
            }
        verification = _hash_all_nodes(
            site,
            transport,
            bridge_path=final_bridge,
            digest=digest,
        )
        if not all(row["status"] == "READY" for row in verification):
            return {
                "status": "BLOCKED",
                "reason": "NEW_V4_RELEASE_NOT_VISIBLE_OR_EXACT_ON_EVERY_NODE",
                "install_status": install_status,
                "release_hash_verification": verification,
                "smoke": [],
                "pointer_verification": [],
            }
    else:
        return {
            "status": "BLOCKED",
            "reason": "V4_RELEASE_STATE_PROBE_FAILED",
            "install_status": "NOT_ATTEMPTED",
            "transport_returncode": release_state_result.returncode,
            "diagnostic_stderr_tail": _diagnostic_tail(
                release_state_result.stderr
            ),
        }

    smoke = [
        _run_probe_on_node(
            site,
            transport,
            node,
            bridge_path=final_bridge,
            mode="smoke",
            workers=1,
            payload=payload,
            contract=contract,
            payload_metadata=payload_metadata,
        )
        for node in site.nodes
    ]
    if not all(row["status"] == "READY" for row in smoke):
        return {
            "status": "BLOCKED",
            "reason": "DYNAMIC_V4_SMOKE_FAILED_BEFORE_ACTIVATION",
            "install_status": install_status,
            "release_relative_path": release_dir,
            "pointer_relative_path": pointer,
            "legacy_v3_pointer_mutated": False,
            "release_hash_verification": verification,
            "smoke": smoke,
            "pointer_verification": [],
        }

    relative_target = f"releases/dynamic-v4/{digest}"
    temporary_pointer = (
        f"{site.node_shared_root}/.current-bridge-dynamic-v4-{uuid.uuid4().hex}"
    )
    activate = transport.run(
        primary,
        _fail_fast(
            "cd || exit 90",
            f"if test -e {shlex.quote(pointer)} && test ! -L {shlex.quote(pointer)}; then exit 73; fi",
            _remote_hash_test(final_bridge, digest),
            f"test ! -e {shlex.quote(temporary_pointer)} && test ! -L {shlex.quote(temporary_pointer)}",
            f"ln -s -- {shlex.quote(relative_target)} {shlex.quote(temporary_pointer)}",
            f"mv -Tf -- {shlex.quote(temporary_pointer)} {shlex.quote(pointer)}",
        ),
        timeout=45,
    )
    if activate.returncode != 0:
        return {
            "status": "BLOCKED",
            "reason": "DYNAMIC_V4_POINTER_ACTIVATION_FAILED",
            "install_status": install_status,
            "release_relative_path": release_dir,
            "pointer_relative_path": pointer,
            "legacy_v3_pointer_mutated": False,
            "release_hash_verification": verification,
            "smoke": smoke,
            "pointer_verification": [],
            "diagnostic_stderr_tail": _diagnostic_tail(activate.stderr),
        }

    pointer_bridge = f"{pointer}/o2obridge.linux-amd64"
    pointer_verification = _hash_all_nodes(
        site,
        transport,
        bridge_path=pointer_bridge,
        digest=digest,
        pointer=pointer,
        expected_pointer_target=relative_target,
    )
    ready = all(row["status"] == "READY" for row in pointer_verification)
    return {
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "DYNAMIC_V4_POINTER_NOT_EXACT_ON_EVERY_NODE",
        "install_status": install_status,
        "release_relative_path": release_dir,
        "pointer_relative_path": pointer,
        "pointer_bridge_relative_path": pointer_bridge,
        "legacy_v3_pointer_relative_path": (
            f"{site.node_shared_root}/{V3_POINTER_NAME}"
        ),
        "legacy_v3_pointer_mutated": False,
        "release_hash_verification": verification,
        "smoke": smoke,
        "pointer_verification": pointer_verification,
    }


def run_dynamic_v4_pilot(
    site: HpcSite,
    transport: Any,
    *,
    bridge_path: str,
    payload: bytes,
    contract: DynamicV4Contract,
    payload_metadata: Mapping[str, Any],
) -> JSONMap:
    rows_by_name: dict[str, JSONMap] = {}
    with ThreadPoolExecutor(max_workers=len(site.nodes)) as executor:
        futures = {
            executor.submit(
                _run_probe_on_node,
                site,
                transport,
                node,
                bridge_path=bridge_path,
                mode="pilot",
                workers=contract.workers_per_node,
                payload=payload,
                contract=contract,
                payload_metadata=payload_metadata,
            ): node.name
            for node in site.nodes
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                rows_by_name[name] = future.result()
            except Exception as error:  # preserve all six-node diagnostics
                rows_by_name[name] = {
                    "name": name,
                    "mode": "pilot",
                    "status": "BLOCKED",
                    "reason": "LOCAL_PILOT_ORCHESTRATION_EXCEPTION",
                    "exception_class": type(error).__name__,
                }
    rows = [rows_by_name[node.name] for node in site.nodes]
    ready_rows = [row for row in rows if row.get("status") == "READY"]
    total_exit_zero = sum(
        int(row.get("summary", {}).get("exit_zero_count", 0))
        for row in ready_rows
    )
    total_validated = sum(
        int(row.get("summary", {}).get("validated_count", 0))
        for row in ready_rows
    )
    workload_hashes = {
        row.get("summary", {}).get("deterministic_workload_sha256")
        for row in ready_rows
    }
    hashes_consistent = (
        len(ready_rows) == len(site.nodes)
        and len(workload_hashes) == 1
        and None not in workload_hashes
    )
    ready = (
        len(ready_rows) == len(site.nodes)
        and total_exit_zero == contract.total_workers
        and total_validated == contract.total_workers
        and hashes_consistent
    )
    return {
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "PILOT_192_WORKER_OR_CROSS_NODE_HASH_FAILED",
        "workers_per_node": contract.workers_per_node,
        "expected_total_workers": contract.total_workers,
        "exit_zero_total": total_exit_zero,
        "validated_total": total_validated,
        "cross_node_workload_hash_consistent": hashes_consistent,
        "deterministic_workload_sha256": (
            next(iter(workload_hashes)) if hashes_consistent else None
        ),
        "nodes": rows,
        "scientific_results_produced": False,
    }


def publish_receipt_v4(
    report: Mapping[str, Any], directory: Path = DEFAULT_RECEIPT_DIRECTORY
) -> tuple[Path, JSONMap]:
    core = json.loads(canonical_bytes(report))
    if "content_address" in core:
        raise HpcDynamicEnvironmentV4Error(
            "report already contains content_address"
        )
    digest = hashlib.sha256(canonical_bytes(core)).hexdigest()
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
    destination_directory = directory.expanduser().resolve()
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / (
        f"hpc-dynamic-v4.{digest}.receipt.json"
    )
    if destination.exists():
        if destination.read_bytes() != rendered:
            raise HpcDynamicEnvironmentV4Error(
                f"content-address collision at {destination}"
            )
        return destination, receipt
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hpc-dynamic-v4-", suffix=".tmp", dir=destination_directory
    )
    __import__("os").close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(rendered)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination, receipt


def verify_receipt_v4(value: Mapping[str, Any]) -> str:
    row = _mapping(value, "receipt")
    address = _mapping(row.get("content_address"), "content_address")
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON excluding content_address"
    ):
        raise HpcDynamicEnvironmentV4Error(
            "receipt content-address contract is invalid"
        )
    declared = _sha256(address.get("sha256"), "content address")
    core = {key: child for key, child in row.items() if key != "content_address"}
    observed = hashlib.sha256(canonical_bytes(core)).hexdigest()
    if declared != observed:
        raise HpcDynamicEnvironmentV4Error(
            "receipt content-address SHA-256 mismatch"
        )
    return observed


def run_dynamic_v4_environment(
    *,
    site_path: Path = DEFAULT_SITE_CONFIG,
    contract_path: Path = DEFAULT_CONTRACT,
    bridge_path: Path = DEFAULT_BRIDGE,
    receipt_directory: Path = DEFAULT_RECEIPT_DIRECTORY,
    apply: bool = False,
    control_transport: Any | None = None,
    node_transport: Any | None = None,
) -> tuple[Path, JSONMap]:
    contract = load_dynamic_v4_contract(contract_path)
    site = load_site_config(site_path)
    validate_site_binding_v4(site, contract)
    bridge = verify_local_v4_bridge(bridge_path, contract)
    payload, payload_metadata = build_dynamic_probe_payload_v4(contract)
    control_transport = control_transport or WslControlTransport(site)
    node_transport = node_transport or SchedulerNodeTransport(site)

    control = probe_control(site, control_transport)
    nodes = [probe_node(site, node_transport, node) for node in site.nodes]
    device_ids = {
        row.get("inventory", {}).get("shared_fs_device")
        for row in nodes
        if row.get("status") == "READY"
    }
    base_ready = (
        control.get("status") == "READY"
        and all(row.get("status") == "READY" for row in nodes)
        and len(device_ids) == 1
        and None not in device_ids
    )
    mutation: JSONMap = {
        "requested": apply,
        "control_bootstrap": {"status": "NOT_REQUESTED"},
        "node_bootstrap": [],
        "dynamic_v4_bridge": {"status": "NOT_REQUESTED"},
    }
    pilot: JSONMap = {
        "status": "NOT_REQUESTED",
        "scientific_results_produced": False,
    }
    if apply:
        if base_ready:
            mutation["control_bootstrap"] = bootstrap_control(
                site, control_transport
            )
            mutation["node_bootstrap"] = [
                bootstrap_node(site, node_transport, node)
                for node in site.nodes
            ]
        else:
            mutation["control_bootstrap"] = {
                "status": "BLOCKED",
                "reason": "CONTROL_OR_NODE_PROBE_NOT_READY",
            }
            mutation["node_bootstrap"] = [
                {
                    "name": node.name,
                    "status": "BLOCKED",
                    "reason": "CONTROL_OR_NODE_PROBE_NOT_READY",
                }
                for node in site.nodes
            ]
        bootstrapped = (
            mutation["control_bootstrap"].get("status") == "READY"
            and len(mutation["node_bootstrap"]) == len(site.nodes)
            and all(
                row.get("status") == "READY"
                for row in mutation["node_bootstrap"]
            )
        )
        if bootstrapped:
            mutation["dynamic_v4_bridge"] = stage_dynamic_v4_bridge(
                site,
                node_transport,
                bridge_path.expanduser().resolve(),
                bridge,
                payload=payload,
                contract=contract,
                payload_metadata=payload_metadata,
            )
        else:
            mutation["dynamic_v4_bridge"] = {
                "status": "BLOCKED",
                "reason": "BOOTSTRAP_NOT_READY",
            }
        staged = mutation["dynamic_v4_bridge"]
        if staged.get("status") == "READY":
            pilot = run_dynamic_v4_pilot(
                site,
                node_transport,
                bridge_path=str(staged["pointer_bridge_relative_path"]),
                payload=payload,
                contract=contract,
                payload_metadata=payload_metadata,
            )
        else:
            pilot = {
                "status": "BLOCKED",
                "reason": "DYNAMIC_V4_SMOKE_OR_ACTIVATION_NOT_READY",
                "scientific_results_produced": False,
            }

    if not apply:
        status = (
            "DRY_RUN_READY_DYNAMIC_V4_NO_EXECUTION"
            if base_ready
            else "BLOCKED"
        )
    else:
        status = (
            "DYNAMIC_V4_ENVIRONMENT_READY_NONSCIENTIFIC"
            if base_ready
            and mutation["dynamic_v4_bridge"].get("status") == "READY"
            and pilot.get("status") == "READY"
            else "BLOCKED"
        )

    report: JSONMap = {
        "schema": RECEIPT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "contract": {
            "path": str(contract.source_path),
            "size_bytes": contract.source_path.stat().st_size,
            "sha256": contract.content_sha256,
        },
        "site_config": _site_identity(site_path),
        "route": {
            "control_plane": "jtl110gpu2",
            "nodes": "scheduler_run_on",
            "configured_nodes": list(contract.nodes),
            "credentials_embedded": False,
        },
        "bridge": bridge,
        "dynamic_probe": payload_metadata,
        "control_plane": control,
        "nodes": nodes,
        "shared_filesystem": {
            "status": "READY" if base_ready else "BLOCKED",
            "device_ids": sorted(value for value in device_ids if value is not None),
        },
        "mutation": mutation,
        "pilot": pilot,
        "pilot_measurement_contract": {
            "elapsed_ms": "CLOCK_MONOTONIC_WALL_TIME_FOR_EACH_NODE_BATCH",
            "max_rss_kib": (
                "LINUX_RESOURCE_RUSAGE_CHILDREN_MAXIMUM_CHILD_HIGH_WATER_KIB_"
                "NOT_AGGREGATE_RSS"
            ),
            "deterministic_workload_sha256": (
                "CANONICAL_PER_WORKER_SEED_EXIT_RESPONSE_STATE_AND_DAMAGE_"
                "ORDINAL_ROWS"
            ),
        },
        "execution_scope": {
            "scientific_experiment_started": False,
            "policy_comparison_started": False,
            "training_started": False,
            "offline_data_uploaded": False,
            "workers_per_node": contract.workers_per_node,
            "maximum_total_workers": contract.total_workers,
            "full_1152_core_load_allowed": False,
            "legacy_v3_pointer_mutated": False,
        },
    }
    return publish_receipt_v4(report, receipt_directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", type=Path, default=DEFAULT_SITE_CONFIG)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument(
        "--receipt-directory", type=Path, default=DEFAULT_RECEIPT_DIRECTORY
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="install isolated v4 bridge, run six smokes, then bounded 192-worker pilot",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        path, receipt = run_dynamic_v4_environment(
            site_path=args.site_config,
            contract_path=args.contract,
            bridge_path=args.bridge,
            receipt_directory=args.receipt_directory,
            apply=args.apply,
        )
    except (HpcDynamicEnvironmentV4Error, HpcEnvironmentError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(f"status={receipt['status']}")
    print(f"receipt={path}")
    print(f"receipt_sha256={receipt['content_address']['sha256']}")
    print("scientific_experiment_started=false")
    return 0 if receipt["status"] != "BLOCKED" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "CONTRACT_SCHEMA",
    "CommandResult",
    "DEFAULT_BRIDGE",
    "DEFAULT_CONTRACT",
    "DEFAULT_RECEIPT_DIRECTORY",
    "DEFAULT_SITE_CONFIG",
    "DynamicV4Contract",
    "EXPECTED_BRIDGE_SHA256",
    "EXPECTED_NODES",
    "HpcDynamicEnvironmentV4Error",
    "PROBE_SUMMARY_SCHEMA",
    "RECEIPT_SCHEMA",
    "V4_POINTER_NAME",
    "build_dynamic_probe_payload_v4",
    "canonical_bytes",
    "load_dynamic_v4_contract",
    "parse_probe_summary_v4",
    "publish_receipt_v4",
    "remote_probe_command_v4",
    "run_dynamic_v4_environment",
    "run_dynamic_v4_pilot",
    "stage_dynamic_v4_bridge",
    "validate_site_binding_v4",
    "verify_local_v4_bridge",
    "verify_receipt_v4",
)
