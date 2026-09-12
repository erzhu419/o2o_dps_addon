"""Publish the pinned v11 dynamic bridge and run one smoke per HPC node."""

from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shlex
import sys
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from .hpc_environment_v1 import (
    CommandResult,
    HpcEnvironmentError,
    HpcSite,
    SchedulerNodeTransport,
    _fail_fast,
    _parse_key_values,
    _remote_hash_test,
    inspect_bridge,
    load_site_config,
)
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_MODE_V3,
    DYNAMIC_SAME_TIMESTAMP_ORDER_V3,
    DynamicTargetSemanticsConfigV3,
)


JSONMap = dict[str, Any]
CONTRACT_SCHEMA = "o2o_hpc_dynamic_environment/v5"
RECEIPT_SCHEMA = "o2o_hpc_dynamic_environment_receipt/v5"
PROBE_SCHEMA = "o2o_hpc_dynamic_probe_summary/v5"
EXPECTED_BRIDGE_FILENAME = (
    "o2obridge.seedfix-v11.withdb.goamd64v1.linux-amd64"
)
EXPECTED_BRIDGE_SHA256 = (
    "b9a8cfdebfb715267326c4bf255323e428ec5d49a2e1e5a4c349faa87e186d4f"
)
EXPECTED_NODES = tuple(f"node{index:03d}" for index in range(1, 7))
V5_POINTER_NAME = "current-bridge-dynamic-v5"
V4_POINTER_NAME = "current-bridge-dynamic-v4"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_CONFIG = PROJECT_ROOT / "configs" / "hpc" / "site.local.json"
DEFAULT_CONTRACT = PROJECT_ROOT / "configs" / "hpc" / "dynamic_v5.example.json"
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / EXPECTED_BRIDGE_FILENAME
DEFAULT_RECEIPT_DIRECTORY = PROJECT_ROOT / ".hpc-local" / "receipts"
_MARKER = "O2O_DYNAMIC_V5_PROBE="


class HpcDynamicEnvironmentV5Error(RuntimeError):
    """The pinned v11 release or its six-node smoke is not ready."""


@dataclass(frozen=True)
class DynamicV5Contract:
    source_path: Path
    source_sha256: str
    seed_base: int
    base_request: str
    target_health: float
    initial_armor: float
    restored_armor: float
    blocked_damage_time_ms: int
    restored_at_ms: int
    horizon_ms: int
    minimum_headroom_cpus: int
    maximum_load1_fraction: float
    process_timeout_seconds: int
    node_timeout_seconds: int


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _load_contract(path: Path) -> tuple[JSONMap, bytes]:
    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HpcDynamicEnvironmentV5Error(
            f"could not load v5 contract: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HpcDynamicEnvironmentV5Error("v5 contract must be an object")
    return value, raw


def _fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise HpcDynamicEnvironmentV5Error(f"{label} fields differ from v5 contract")


def _object(value: Any, label: str) -> JSONMap:
    if not isinstance(value, dict):
        raise HpcDynamicEnvironmentV5Error(f"{label} must be an object")
    return value


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HpcDynamicEnvironmentV5Error(f"{label} must be an integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HpcDynamicEnvironmentV5Error(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise HpcDynamicEnvironmentV5Error(f"{label} must be finite")
    return result


def load_dynamic_v5_contract(path: Path = DEFAULT_CONTRACT) -> DynamicV5Contract:
    value, raw = _load_contract(path)
    _fields(
        value,
        {"schema", "bridge", "route", "nodes", "dynamic_probe", "load_guard", "smoke"},
        "contract",
    )
    if value.get("schema") != CONTRACT_SCHEMA:
        raise HpcDynamicEnvironmentV5Error("contract schema is not v5")
    bridge = _object(value.get("bridge"), "bridge")
    route = _object(value.get("route"), "route")
    probe = _object(value.get("dynamic_probe"), "dynamic_probe")
    guard = _object(value.get("load_guard"), "load_guard")
    smoke = _object(value.get("smoke"), "smoke")
    _fields(bridge, {"filename", "sha256"}, "bridge")
    _fields(route, {"control_plane_alias", "node_transport_kind"}, "route")
    _fields(
        probe,
        {
            "base_request", "bridge_command", "seed_base", "target_count",
            "target_health", "initial_armor", "restored_armor",
            "blocked_damage_time_ms", "attackability_restored_at_ms",
            "idle_advance_horizon_ms", "idle_advance_mode",
            "same_timestamp_order", "retarget_mode",
        },
        "dynamic_probe",
    )
    _fields(guard, {"minimum_headroom_logical_cpus", "maximum_load1_fraction"}, "load_guard")
    _fields(
        smoke,
        {"workers_per_node", "total_processes", "gomaxprocs_per_process", "process_timeout_seconds", "node_timeout_seconds"},
        "smoke",
    )
    nodes = value.get("nodes")
    if (
        bridge != {"filename": EXPECTED_BRIDGE_FILENAME, "sha256": EXPECTED_BRIDGE_SHA256}
        or route != {"control_plane_alias": "jtl110gpu2", "node_transport_kind": "scheduler_run_on"}
        or not isinstance(nodes, list)
        or tuple(nodes) != EXPECTED_NODES
        or probe.get("base_request") != "configs/wowsims/fury_warrior_clean_dual.json"
        or probe.get("bridge_command") != "load_dynamic_v3"
        or probe.get("target_count") != 1
        or probe.get("idle_advance_mode") != DYNAMIC_IDLE_ADVANCE_MODE_V3
        or probe.get("same_timestamp_order") != DYNAMIC_SAME_TIMESTAMP_ORDER_V3
        or probe.get("retarget_mode") != "NEXT_ALIVE_CYCLIC"
        or smoke.get("workers_per_node") != 1
        or smoke.get("total_processes") != 6
        or smoke.get("gomaxprocs_per_process") != 1
    ):
        raise HpcDynamicEnvironmentV5Error("contract is not the pinned v11 six-node smoke")
    restored = _int(probe.get("attackability_restored_at_ms"), "restored_at_ms")
    horizon = _int(probe.get("idle_advance_horizon_ms"), "horizon_ms")
    blocked = _int(probe.get("blocked_damage_time_ms"), "blocked_damage_time_ms")
    if not (0 < blocked < restored < horizon):
        raise HpcDynamicEnvironmentV5Error("dynamic smoke timing is not ordered")
    headroom = _int(guard.get("minimum_headroom_logical_cpus"), "minimum headroom")
    fraction = _number(guard.get("maximum_load1_fraction"), "load1 fraction")
    process_timeout = _int(smoke.get("process_timeout_seconds"), "process timeout")
    node_timeout = _int(smoke.get("node_timeout_seconds"), "node timeout")
    if headroom < 1 or not (0 < fraction < 1) or not (5 <= process_timeout <= node_timeout <= 120):
        raise HpcDynamicEnvironmentV5Error("load or timeout bound is invalid")
    return DynamicV5Contract(
        source_path=path.expanduser().resolve(),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        seed_base=_int(probe.get("seed_base"), "seed_base"),
        base_request=str(probe["base_request"]),
        target_health=_number(probe.get("target_health"), "target_health"),
        initial_armor=_number(probe.get("initial_armor"), "initial_armor"),
        restored_armor=_number(probe.get("restored_armor"), "restored_armor"),
        blocked_damage_time_ms=blocked,
        restored_at_ms=restored,
        horizon_ms=horizon,
        minimum_headroom_cpus=headroom,
        maximum_load1_fraction=fraction,
        process_timeout_seconds=process_timeout,
        node_timeout_seconds=node_timeout,
    )


def validate_site_v5(site: HpcSite) -> None:
    if (
        site.control_ssh_alias != "jtl110gpu2"
        or site.node_transport_kind != "scheduler_run_on"
        or tuple(node.name for node in site.nodes) != EXPECTED_NODES
        or tuple(node.transport_name for node in site.nodes) != EXPECTED_NODES
    ):
        raise HpcDynamicEnvironmentV5Error(
            "site is not jtl110gpu2/scheduler_run_on node001-node006"
        )


def verify_local_v11_bridge(path: Path) -> JSONMap:
    resolved = path.expanduser().resolve()
    if resolved.name != EXPECTED_BRIDGE_FILENAME:
        raise HpcDynamicEnvironmentV5Error("bridge filename is not pinned v11")
    try:
        identity = inspect_bridge(resolved)
    except HpcEnvironmentError as error:
        raise HpcDynamicEnvironmentV5Error(str(error)) from error
    if identity.get("sha256") != EXPECTED_BRIDGE_SHA256:
        raise HpcDynamicEnvironmentV5Error("bridge bytes are not pinned v11")
    return identity


def _smoke_request(contract: DynamicV5Contract) -> JSONMap:
    path = PROJECT_ROOT / contract.base_request
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HpcDynamicEnvironmentV5Error("base request must be an object")
    request = copy.deepcopy(value)
    encounter = _object(request.get("encounter"), "request.encounter")
    targets = encounter.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise HpcDynamicEnvironmentV5Error("base request must have one target")
    encounter["duration"] = contract.horizon_ms / 1000
    encounter["durationVariation"] = 0
    encounter["useHealth"] = True
    target = targets[0]
    target["name"] = "HPC dynamic-v5 bridge smoke"
    target["swingSpeed"] = 0
    target["minBaseDamage"] = 0
    target["damageSpread"] = 0
    target["parryHaste"] = False
    stats = target.get("stats")
    if not isinstance(stats, list):
        raise HpcDynamicEnvironmentV5Error("base target stats must be an array")
    while len(stats) <= 34:
        stats.append(0)
    stats[26] = 3000.0
    stats[34] = contract.target_health
    options = _object(request.get("simOptions"), "request.simOptions")
    options["iterations"] = 1
    options["interactive"] = True
    return request


def build_dynamic_probe_payload_v5(contract: DynamicV5Contract) -> tuple[bytes, JSONMap]:
    request = _smoke_request(contract)
    restoration = DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, contract.target_health),),
        idle_advance_horizon_ms=contract.horizon_ms,
        background_damage_events=(BackgroundDamageEventV1(0, contract.blocked_damage_time_ms, 0, "blocked-during-idle", 10.0),),
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 0, False),
            DynamicAttackabilityEventV2(1, contract.restored_at_ms, 0, True),
        ),
        effective_armor_events=(
            DynamicEffectiveArmorEventV2(0, 0, 0, contract.initial_armor),
            DynamicEffectiveArmorEventV2(1, contract.restored_at_ms, 0, contract.restored_armor),
        ),
    )
    horizon = DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, contract.target_health),),
        idle_advance_horizon_ms=contract.horizon_ms,
        effective_armor_events=(DynamicEffectiveArmorEventV2(0, 0, 0, contract.initial_armor),),
    )
    messages = (
        {"command": "load_dynamic_v3", "request": request, "seed": contract.seed_base, "dynamic": restoration.to_wire()},
        {"command": "dynamic_idle_advance_receipts", "cursor": 0},
        {"command": "dynamic_attackability_receipts", "cursor": 0},
        {"command": "dynamic_armor_receipts", "cursor": 0},
        {"command": "dynamic_damage_receipts", "cursor": 0},
        {"command": "dynamic_candidate_damage_receipts", "cursor": 0},
        {"command": "state"},
        {"command": "load_dynamic_v3", "request": request, "seed": contract.seed_base + 1, "dynamic": horizon.to_wire()},
        {"command": "wait", "wait_ms": contract.horizon_ms + contract.restored_at_ms},
        {"command": "advance"},
        {"command": "dynamic_idle_advance_receipts", "cursor": 0},
        {"command": "state"},
        {"command": "close"},
    )
    payload = b"".join(canonical_bytes(message) + b"\n" for message in messages)
    return payload, {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size_bytes": len(payload),
        "processes_per_node": 1,
        "command_count": len(messages),
        "restoration_config_sha256": restoration.content_sha256,
        "horizon_config_sha256": horizon.content_sha256,
        "restored_at_ms": contract.restored_at_ms,
        "horizon_ms": contract.horizon_ms,
        "bridge_command": "load_dynamic_v3",
    }


_REMOTE_PROBE = r'''
import base64,hashlib,json,os,subprocess,sys
MARKER="O2O_DYNAMIC_V5_PROBE="
SCHEMA="o2o_hpc_dynamic_probe_summary/v5"
COMMANDS=["load_dynamic_v3","dynamic_idle_advance_receipts","dynamic_attackability_receipts","dynamic_armor_receipts","dynamic_damage_receipts","dynamic_candidate_damage_receipts","state","load_dynamic_v3","wait","advance","dynamic_idle_advance_receipts","state","close"]
def need(ok,msg):
    if not ok: raise ValueError(msg)
def canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
bridge,payload_b64,bridge_sha,restore_sha,horizon_sha,restore_ms,horizon_ms,timeout=sys.argv[1:]
need(hashlib.sha256(open(bridge,"rb").read()).hexdigest()==bridge_sha,"bridge sha")
payload=base64.b64decode(payload_b64)
env=os.environ.copy(); env["GOMAXPROCS"]="1"
result=subprocess.run([bridge],input=payload,capture_output=True,timeout=int(timeout),env=env)
need(result.returncode==0,"bridge exit")
lines=result.stdout.splitlines(); need(len(lines)==len(COMMANDS),"response count")
rows=[json.loads(line) for line in lines]
for command,row in zip(COMMANDS,rows): need(row.get("ok") is True and row.get("command")==command,"command "+command)
load=rows[0]; receipt=load.get("dynamic_load"); state=load.get("state")
need(receipt.get("schema")=="o2o_dynamic_target_semantics/v3" and receipt.get("config_digest")==restore_sha,"restore load")
need(receipt.get("idle_advance_mode")=="CENTRAL_TO_NEXT_ATTACKABLE_OR_HORIZON" and receipt.get("idle_advance_horizon_ms")==int(horizon_ms),"restore contract")
need(state.get("time_ms")==int(restore_ms) and state.get("needs_input") is True and state.get("num_targets")==1,"restore state")
need(float(state.get("target_armor"))==1234.0,"restored armor")
idle=rows[1]["dynamic_idle_advance_receipts"]; ir=idle["receipts"]
need(idle.get("config_digest")==restore_sha and len(ir)==1 and ir[0].get("status")=="ATTACKABILITY_RESTORED","restore idle")
need(ir[0].get("policy_actions_consumed")==0 and ir[0].get("policy_target_selections_consumed")==0 and ir[0].get("scheduler_random_draws")==0,"idle consumption")
need(ir[0].get("candidate_cursor_start")==0 and ir[0].get("candidate_cursor_end")==0,"candidate cursor")
attack=rows[2]["dynamic_attackability_receipts"]; need(attack.get("next_cursor")==2 and attack.get("schedule_complete") is True,"attack receipts")
armor=rows[3]["dynamic_armor_receipts"]; need(armor.get("next_cursor")==2 and armor.get("schedule_complete") is True,"armor receipts")
damage=rows[4]["dynamic_damage_receipts"]; need(len(damage.get("receipts",[]))==1 and damage["receipts"][0].get("status")=="CANCELED_TARGET_UNATTACKABLE","background cancel")
candidate=rows[5]["dynamic_candidate_damage_receipts"]; need(candidate.get("next_cursor")==0 and candidate.get("receipts")==[],"candidate empty")
need(rows[6].get("state")==state,"restore state roundtrip")
load2=rows[7]; receipt2=load2.get("dynamic_load"); state2=load2.get("state")
need(receipt2.get("config_digest")==horizon_sha and receipt2.get("idle_advance_horizon_ms")==int(horizon_ms),"horizon load")
need(state2.get("time_ms")==0 and state2.get("finished") is False and state2.get("needs_input") is True and state2.get("num_targets")==1,"attackable horizon initial state")
waited=rows[8].get("state"); need(waited.get("time_ms")==0 and waited.get("finished") is False and waited.get("needs_input") is False,"attackable horizon wait scheduled")
terminal=rows[9].get("state"); need(terminal.get("time_ms")==int(horizon_ms) and terminal.get("finished") is True and terminal.get("needs_input") is False and terminal.get("num_targets")==1,"attackable horizon terminal state")
idle2=rows[10]["dynamic_idle_advance_receipts"]; ir2=idle2["receipts"]
need(idle2.get("stream_closed") is True and len(ir2)==0,"attackable horizon must not mint idle interval")
need(rows[11].get("state")==terminal,"attackable horizon state roundtrip")
need(rows[12].get("ok") is True and rows[12].get("command")=="close" and rows[12].get("environment_generation")==2,"close")
summary={"schema":SCHEMA,"status":"READY","hostname":os.uname().nodename,"bridge_sha256":bridge_sha,"restoration_config_sha256":restore_sha,"horizon_config_sha256":horizon_sha,"attackable_horizon_verified":True,"process_count":1,"response_sha256":hashlib.sha256(result.stdout).hexdigest(),"restored_at_ms":int(restore_ms),"horizon_ms":int(horizon_ms),"failures":[]}
print(MARKER+json.dumps(summary,sort_keys=True,separators=(",",":")))
'''.strip()


def remote_probe_command_v5(
    bridge_path: str,
    payload: bytes,
    contract: DynamicV5Contract,
    metadata: Mapping[str, Any],
    *,
    python_executable: str,
) -> str:
    program = base64.b64encode(_REMOTE_PROBE.encode()).decode()
    payload64 = base64.b64encode(payload).decode()
    invocation = " ".join(
        shlex.quote(value)
        for value in (
            python_executable, "-c",
            "import base64,sys;program=base64.b64decode(sys.argv[1]);sys.argv=sys.argv[1:];exec(program)",
            program, bridge_path, payload64, EXPECTED_BRIDGE_SHA256,
            str(metadata["restoration_config_sha256"]), str(metadata["horizon_config_sha256"]),
            str(contract.restored_at_ms), str(contract.horizon_ms), str(contract.process_timeout_seconds),
        )
    )
    return _fail_fast("export LC_ALL=C", "export GOMAXPROCS=1", "cd || exit 90", invocation)


def parse_probe_v5(result: CommandResult, node: str, metadata: Mapping[str, Any]) -> JSONMap:
    lines = [line[len(_MARKER):] for line in result.stdout.splitlines() if line.startswith(_MARKER)]
    summary = None
    if len(lines) == 1:
        try:
            parsed = json.loads(lines[0])
            if isinstance(parsed, dict):
                summary = parsed
        except json.JSONDecodeError:
            pass
    ready = (
        result.returncode == 0
        and summary is not None
        and summary.get("schema") == PROBE_SCHEMA
        and summary.get("status") == "READY"
        and summary.get("hostname") == node
        and summary.get("bridge_sha256") == EXPECTED_BRIDGE_SHA256
        and summary.get("restoration_config_sha256") == metadata["restoration_config_sha256"]
        and summary.get("horizon_config_sha256") == metadata["horizon_config_sha256"]
        and summary.get("attackable_horizon_verified") is True
        and summary.get("process_count") == 1
        and summary.get("restored_at_ms") == metadata["restored_at_ms"]
        and summary.get("horizon_ms") == metadata["horizon_ms"]
        and summary.get("failures") == []
    )
    return {
        "name": node,
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "LOAD_DYNAMIC_V3_IDLE_HORIZON_SMOKE_FAILED",
        "transport_returncode": result.returncode,
        "summary": summary,
        "stderr_tail": "" if ready else result.stderr[-2048:],
    }


def _inventory_command(site: HpcSite) -> str:
    v4 = f"{site.node_shared_root}/{V4_POINTER_NAME}"
    return _fail_fast(
        "export LC_ALL=C", "cd || exit 90",
        "printf 'hostname='; hostname",
        "printf 'logical_cpus='; getconf _NPROCESSORS_ONLN",
        "set -- $(cat /proc/loadavg); printf 'load1=%s\\n' \"$1\"",
        f"if test -L {shlex.quote(v4)}; then printf 'v4_pointer_target='; readlink -- {shlex.quote(v4)}; elif test -e {shlex.quote(v4)}; then printf 'v4_pointer_target=INVALID_NON_SYMLINK\\n'; else printf 'v4_pointer_target=ABSENT\\n'; fi",
    )


def inspect_node_load_v5(site: HpcSite, transport: Any, contract: DynamicV5Contract) -> list[JSONMap]:
    rows = []
    for node in site.nodes:
        result = transport.run(node, _inventory_command(site), timeout=35)
        values = _parse_key_values(result.stdout)
        try:
            cpus = int(values.get("logical_cpus", ""))
            load1 = float(values.get("load1", ""))
        except ValueError:
            cpus, load1 = 0, math.inf
        ready = (
            result.returncode == 0 and values.get("hostname") == node.name
            and cpus > contract.minimum_headroom_cpus
            and load1 <= cpus * contract.maximum_load1_fraction
            and cpus - load1 >= contract.minimum_headroom_cpus
            and values.get("v4_pointer_target") not in {None, "INVALID_NON_SYMLINK"}
        )
        rows.append({
            "name": node.name, "status": "READY" if ready else "BLOCKED",
            "logical_cpus": cpus, "load1": load1,
            "headroom_by_load1": None if not math.isfinite(load1) else cpus - load1,
            "v4_pointer_target": values.get("v4_pointer_target"),
            "transport_returncode": result.returncode,
        })
    return rows


def _release_paths(site: HpcSite) -> tuple[str, str, str, str]:
    release = f"{site.node_shared_root}/releases/dynamic-v5/{EXPECTED_BRIDGE_SHA256}"
    bridge = f"{release}/o2obridge.linux-amd64"
    pointer = f"{site.node_shared_root}/{V5_POINTER_NAME}"
    return release, bridge, pointer, f"releases/dynamic-v5/{EXPECTED_BRIDGE_SHA256}"


def _verify_command(bridge: str, *, pointer: str | None = None, target: str | None = None) -> str:
    commands = ["export LC_ALL=C", "cd || exit 90"]
    if pointer is not None:
        commands += [f"test -L {shlex.quote(pointer)}", f"pointer_target=$(readlink -- {shlex.quote(pointer)})", f"test \"$pointer_target\" = {shlex.quote(str(target))}"]
    commands += [
        f"test -f {shlex.quote(bridge)} && test -x {shlex.quote(bridge)} && test ! -L {shlex.quote(bridge)}",
        _remote_hash_test(bridge, EXPECTED_BRIDGE_SHA256),
        f"magic=$(od -An -t x1 -N 4 {shlex.quote(bridge)} | tr -d '[:space:]')", "test \"$magic\" = 7f454c46",
        f"machine=$(od -An -t u2 -j 18 -N 2 {shlex.quote(bridge)} | tr -d '[:space:]')", "test \"$machine\" = 62",
        f"printf 'verified_sha256={EXPECTED_BRIDGE_SHA256}\\n'",
    ]
    if pointer is not None:
        commands.append("printf 'pointer_target=%s\\n' \"$pointer_target\"")
    return _fail_fast(*commands)


def _verify_all(site: HpcSite, transport: Any, bridge: str, *, pointer: str | None = None, target: str | None = None) -> list[JSONMap]:
    rows = []
    for node in site.nodes:
        result = transport.run(node, _verify_command(bridge, pointer=pointer, target=target), timeout=60)
        values = _parse_key_values(result.stdout)
        ready = result.returncode == 0 and values.get("verified_sha256") == EXPECTED_BRIDGE_SHA256 and (pointer is None or values.get("pointer_target") == target)
        rows.append({"name": node.name, "status": "READY" if ready else "BLOCKED", "verified_sha256": values.get("verified_sha256"), "pointer_target": values.get("pointer_target"), "transport_returncode": result.returncode})
    return rows


def stage_dynamic_v5_bridge(site: HpcSite, transport: Any, local_bridge: Path, payload: bytes, contract: DynamicV5Contract, metadata: Mapping[str, Any], load_rows: Sequence[Mapping[str, Any]]) -> JSONMap:
    if not all(row.get("status") == "READY" for row in load_rows):
        return {"status": "BLOCKED", "reason": "NODE_LOAD_HEADROOM_NOT_READY"}
    release, bridge, pointer, relative_target = _release_paths(site)
    primary = site.nodes[0]
    state = transport.run(primary, _fail_fast("cd || exit 90", f"if test -e {shlex.quote(release)} || test -L {shlex.quote(release)}; then printf 'state=EXISTS\\n'; else printf 'state=ABSENT\\n'; fi"), timeout=35)
    release_state = _parse_key_values(state.stdout).get("state") if state.returncode == 0 else None
    install_status = "REUSED_IDENTICAL"
    if release_state == "ABSENT":
        install_status = "INSTALLED_NEW"
        incoming = f"{site.node_shared_root}/incoming/dynamic-v5-{EXPECTED_BRIDGE_SHA256}-{uuid.uuid4().hex}"
        incoming_bridge = f"{incoming}/o2obridge.linux-amd64"
        prepared = transport.run(primary, _fail_fast("umask 077", "cd || exit 90", f"mkdir -- {shlex.quote(incoming)}"), timeout=35)
        if prepared.returncode != 0:
            return {"status": "BLOCKED", "reason": "STAGING_PREPARE_FAILED"}
        copied = transport.copy(primary, local_bridge, f"~/{incoming_bridge}", timeout=240)
        if copied.returncode != 0:
            return {"status": "BLOCKED", "reason": "BRIDGE_COPY_FAILED", "incoming_relative_path": incoming}
        installed = transport.run(primary, _fail_fast(
            "umask 077", "cd || exit 90", _remote_hash_test(incoming_bridge, EXPECTED_BRIDGE_SHA256),
            f"chmod 700 {shlex.quote(incoming_bridge)}", f"mkdir -p -- {shlex.quote(str(PurePosixPath(release).parent))}",
            f"test ! -e {shlex.quote(release)} && test ! -L {shlex.quote(release)}", f"mv -- {shlex.quote(incoming)} {shlex.quote(release)}",
        ), timeout=60)
        if installed.returncode != 0:
            return {"status": "BLOCKED", "reason": "BRIDGE_INSTALL_FAILED", "incoming_relative_path": incoming}
    elif release_state != "EXISTS":
        return {"status": "BLOCKED", "reason": "RELEASE_STATE_UNKNOWN"}
    release_verify = _verify_all(site, transport, bridge)
    if not all(row["status"] == "READY" for row in release_verify):
        return {"status": "BLOCKED", "reason": "RELEASE_NOT_EXACT_ON_ALL_NODES", "release_verification": release_verify}
    smokes = []
    for node in site.nodes:
        result = transport.run(
            node,
            remote_probe_command_v5(
                bridge,
                payload,
                contract,
                metadata,
                python_executable=site.node_python,
            ),
            timeout=contract.node_timeout_seconds,
        )
        smokes.append(parse_probe_v5(result, node.name, metadata))
    if not all(row["status"] == "READY" for row in smokes):
        return {"status": "BLOCKED", "reason": "SIX_NODE_SMOKE_FAILED_BEFORE_ACTIVATION", "release_verification": release_verify, "smoke": smokes}
    temporary = f"{site.node_shared_root}/.current-bridge-dynamic-v5-{uuid.uuid4().hex}"
    activated = transport.run(primary, _fail_fast(
        "cd || exit 90", f"if test -e {shlex.quote(pointer)} && test ! -L {shlex.quote(pointer)}; then exit 73; fi",
        f"ln -s -- {shlex.quote(relative_target)} {shlex.quote(temporary)}", f"mv -Tf -- {shlex.quote(temporary)} {shlex.quote(pointer)}",
    ), timeout=45)
    if activated.returncode != 0:
        return {"status": "BLOCKED", "reason": "V5_POINTER_ACTIVATION_FAILED", "release_verification": release_verify, "smoke": smokes}
    pointer_verify = _verify_all(site, transport, f"{pointer}/o2obridge.linux-amd64", pointer=pointer, target=relative_target)
    post_load = inspect_node_load_v5(site, transport, contract)
    v4_unchanged = all(before.get("v4_pointer_target") == after.get("v4_pointer_target") for before, after in zip(load_rows, post_load))
    ready = all(row["status"] == "READY" for row in pointer_verify) and v4_unchanged
    return {
        "status": "READY" if ready else "BLOCKED",
        "reason": None if ready else "POINTER_OR_V4_ISOLATION_VERIFICATION_FAILED",
        "install_status": install_status,
        "release_relative_path": release,
        "pointer_relative_path": pointer,
        "pointer_bridge_relative_path": f"{pointer}/o2obridge.linux-amd64",
        "release_verification": release_verify,
        "smoke": smokes,
        "pointer_verification": pointer_verify,
        "v4_pointer_unchanged": v4_unchanged,
        "post_activation_load": post_load,
    }


def publish_receipt_v5(report: Mapping[str, Any], directory: Path = DEFAULT_RECEIPT_DIRECTORY) -> tuple[Path, JSONMap]:
    core = copy.deepcopy(dict(report))
    digest = hashlib.sha256(canonical_bytes(core)).hexdigest()
    receipt = {**core, "content_address": {"algorithm": "sha256", "scope": "canonical JSON excluding content_address", "sha256": digest}}
    rendered = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    destination_dir = directory.expanduser().resolve(); destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"hpc-dynamic-v5.{digest}.receipt.json"
    if destination.exists():
        if destination.read_bytes() != rendered:
            raise HpcDynamicEnvironmentV5Error("receipt content-address collision")
        return destination, receipt
    descriptor, name = tempfile.mkstemp(prefix=".hpc-dynamic-v5-", suffix=".tmp", dir=destination_dir)
    __import__("os").close(descriptor); temporary = Path(name)
    try:
        temporary.write_bytes(rendered); temporary.replace(destination)
    finally:
        if temporary.exists(): temporary.unlink()
    return destination, receipt


def verify_receipt_v5(value: Mapping[str, Any]) -> str:
    address = value.get("content_address")
    if not isinstance(address, Mapping) or address.get("algorithm") != "sha256" or address.get("scope") != "canonical JSON excluding content_address":
        raise HpcDynamicEnvironmentV5Error("receipt content address is invalid")
    digest = hashlib.sha256(canonical_bytes({key: child for key, child in value.items() if key != "content_address"})).hexdigest()
    if address.get("sha256") != digest:
        raise HpcDynamicEnvironmentV5Error("receipt content address differs")
    return digest


def run_dynamic_v5_environment(*, site_path: Path = DEFAULT_SITE_CONFIG, contract_path: Path = DEFAULT_CONTRACT, bridge_path: Path = DEFAULT_BRIDGE, receipt_directory: Path = DEFAULT_RECEIPT_DIRECTORY, apply: bool = False, node_transport: Any | None = None) -> tuple[Path, JSONMap]:
    contract = load_dynamic_v5_contract(contract_path)
    site = load_site_config(site_path); validate_site_v5(site)
    bridge = verify_local_v11_bridge(bridge_path)
    payload, metadata = build_dynamic_probe_payload_v5(contract)
    transport = node_transport or SchedulerNodeTransport(site)
    loads = inspect_node_load_v5(site, transport, contract)
    load_ready = all(row["status"] == "READY" for row in loads)
    release = {"status": "NOT_REQUESTED"}
    if apply and load_ready:
        release = stage_dynamic_v5_bridge(site, transport, bridge_path.expanduser().resolve(), payload, contract, metadata, loads)
    elif apply:
        release = {"status": "BLOCKED", "reason": "NODE_LOAD_HEADROOM_NOT_READY"}
    status = ("DYNAMIC_V5_V11_BRIDGE_READY_NONSCIENTIFIC" if apply and release.get("status") == "READY" else "DRY_RUN_READY_V5_NO_MUTATION" if not apply and load_ready else "BLOCKED")
    report = {
        "schema": RECEIPT_SCHEMA, "generated_at_utc": datetime.now(timezone.utc).isoformat(), "status": status,
        "contract": {"path": str(contract.source_path), "sha256": contract.source_sha256},
        "route": {"control_plane": "jtl110gpu2", "node_transport": "scheduler_run_on", "nodes": list(EXPECTED_NODES)},
        "bridge": bridge, "probe": metadata, "pre_activation_load": loads, "release": release,
        "execution_scope": {"processes_per_node": 1, "total_processes": 6, "multiseed_started": False, "training_started": False, "formal_policy_runner_connected": False, "scientific_experiment_started": False, "dynamic_v4_pointer_mutated": False},
    }
    return publish_receipt_v5(report, receipt_directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", type=Path, default=DEFAULT_SITE_CONFIG)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--receipt-directory", type=Path, default=DEFAULT_RECEIPT_DIRECTORY)
    parser.add_argument("--apply", action="store_true", help="publish v11, run one smoke on each node, and activate only the v5 pointer")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        path, receipt = run_dynamic_v5_environment(site_path=args.site_config, contract_path=args.contract, bridge_path=args.bridge, receipt_directory=args.receipt_directory, apply=args.apply)
    except (HpcDynamicEnvironmentV5Error, HpcEnvironmentError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr); return 2
    print(f"status={receipt['status']}"); print(f"receipt={path}"); print(f"receipt_sha256={receipt['content_address']['sha256']}")
    return 0 if receipt["status"] != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "DEFAULT_BRIDGE", "DEFAULT_CONTRACT", "DynamicV5Contract", "EXPECTED_BRIDGE_SHA256",
    "HpcDynamicEnvironmentV5Error", "build_dynamic_probe_payload_v5", "inspect_node_load_v5",
    "load_dynamic_v5_contract", "parse_probe_v5", "publish_receipt_v5", "remote_probe_command_v5",
    "run_dynamic_v5_environment", "stage_dynamic_v5_bridge", "validate_site_v5", "verify_local_v11_bridge", "verify_receipt_v5",
)
