"""Six-node dispatcher for the frozen Fury development multiseed run.

This module owns only the node/shard launch boundary.  It accepts an already
validated v3 paired-runner plan, keeps each paired group indivisible, and maps
the runner's shards across node001--node006.  Policy adapters and simulator
rollouts remain owned by the paired runner's production worker.

The current repository does not yet contain that five-lane production worker,
so inspecting the checked-in protocol returns a compact BLOCKED receipt.  No
synthetic result can make this dispatcher ready for a scientific run.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import shlex
import sys
from typing import Any, Mapping, Sequence

from . import fury_multiseed_evaluation_v3 as evaluation_v3
from . import fury_paired_multiseed_runner_v3 as runner_v3
from .hpc_dynamic_environment_v5 import (
    EXPECTED_BRIDGE_SHA256,
    RECEIPT_SCHEMA as DYNAMIC_V5_RECEIPT_SCHEMA,
    verify_receipt_v5,
)
from .hpc_environment_v1 import (
    CommandResult,
    HpcEnvironmentError,
    HpcSite,
    SchedulerNodeTransport,
    load_site_config,
)


JSONMap = dict[str, Any]
DISPATCH_SCHEMA = "fury_multiseed_hpc_dispatch/v1"
READINESS_SCHEMA = "fury_multiseed_hpc_dispatch_readiness/v1"
PHASE = "development"
MASTER_SEED_COUNT = 256
EXPECTED_NODES = tuple(f"node{index:03d}" for index in range(1, 7))
MAX_WORKERS_PER_NODE = 160
PRODUCTION_WORKER_MODULE = "o2o_dps.fury_multiseed_hpc_worker_v1"
REMOTE_SOURCE_POINTER = "current-fury-multiseed-source"
REMOTE_BRIDGE_POINTER = "current-bridge-dynamic-v5/o2obridge.linux-amd64"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = PROJECT_ROOT / "configs/evaluation/fury_multiseed_protocol_v3.json"
DEFAULT_SITE = PROJECT_ROOT / "configs/hpc/site.local.json"
DEFAULT_RECEIPT_DIRECTORY = PROJECT_ROOT / ".hpc-local/receipts"


class FuryMultiseedHpcDispatchV1Error(RuntimeError):
    """The fixed development dispatch contract is not satisfied."""


@dataclass(frozen=True)
class ValidatedDevelopmentPlan:
    plan: JSONMap
    delegate: JSONMap


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcDispatchV1Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryMultiseedHpcDispatchV1Error(f"{label} must be an object")
    return value


def _validate_site(site: HpcSite) -> None:
    if (
        site.node_transport_kind != "scheduler_run_on"
        or tuple(node.name for node in site.nodes) != EXPECTED_NODES
        or tuple(node.transport_name for node in site.nodes) != EXPECTED_NODES
    ):
        raise FuryMultiseedHpcDispatchV1Error(
            "dispatch requires scheduler_run_on for node001--node006"
        )


def validate_development_runner_plan(
    value: Mapping[str, Any],
) -> ValidatedDevelopmentPlan:
    """Accept only the real five-policy, 256-seed development plan."""

    try:
        plan = runner_v3.validate_runner_plan(value)
        delegate = runner_v3.unwrap_runner_plan(plan)
    except runner_v3.FuryPairedRunnerError as error:
        raise FuryMultiseedHpcDispatchV1Error(str(error)) from error
    contract = delegate["contract"]
    policies = contract["policies"]
    baseline_ids = tuple(
        row["policy_id"] for row in policies if row["role"] == "BASELINE"
    )
    candidates = [row for row in policies if row["role"] == "CANDIDATE"]
    seeds = contract["seed_derivation"]["master_seeds"]
    bridge = contract["bridge_identity"]
    if baseline_ids != runner_v3.REQUIRED_BASELINE_IDS:
        raise FuryMultiseedHpcDispatchV1Error(
            "runner baseline order is not Cat, deployed Contra, Contra260817, External-V2"
        )
    if len(policies) != 5 or len(candidates) != 1 or policies[-1] != candidates[0]:
        raise FuryMultiseedHpcDispatchV1Error(
            "runner must end with exactly one Cat2_new-delivered candidate"
        )
    if (
        contract["phase"] != PHASE
        or contract["execution_mode"] != runner_v3.SINGLE_BRIDGE_MODE
        or contract["plan_intent"] != runner_v3.COMPARISON_INTENT
    ):
        raise FuryMultiseedHpcDispatchV1Error(
            "runner is not the production development comparison plan"
        )
    if len(seeds) != MASTER_SEED_COUNT or len(set(seeds)) != MASTER_SEED_COUNT:
        raise FuryMultiseedHpcDispatchV1Error(
            "development runner must contain the fixed 256 unique master seeds"
        )
    if bridge.get("sha256") != EXPECTED_BRIDGE_SHA256:
        raise FuryMultiseedHpcDispatchV1Error(
            "runner is not bound to the dynamic-v5 v7 Linux bridge"
        )
    if contract["group_count"] != len(contract["scenarios"]) * MASTER_SEED_COUNT:
        raise FuryMultiseedHpcDispatchV1Error(
            "runner group count is not scenarios x 256 paired seeds"
        )
    if contract["shard_count"] < len(EXPECTED_NODES):
        raise FuryMultiseedHpcDispatchV1Error(
            "runner needs at least six shards to use all six nodes"
        )
    return ValidatedDevelopmentPlan(plan=plan, delegate=delegate)


def _assign_shards(delegate: Mapping[str, Any]) -> dict[str, list[int]]:
    """Balance the runner's immutable shards without splitting any shard."""

    shards = delegate["contract"]["shards"]
    assigned = {node: [] for node in EXPECTED_NODES}
    loads = {node: 0 for node in EXPECTED_NODES}
    for shard in sorted(
        shards,
        key=lambda row: (-int(row["estimated_cost_units"]), int(row["shard_index"])),
    ):
        node = min(
            EXPECTED_NODES,
            key=lambda name: (loads[name], len(assigned[name]), name),
        )
        index = int(shard["shard_index"])
        assigned[node].append(index)
        loads[node] += int(shard["estimated_cost_units"])
    for values in assigned.values():
        values.sort()
    return assigned


def build_dispatch_plan(
    runner_plan: Mapping[str, Any],
    *,
    workers_per_node: int,
) -> JSONMap:
    validated = validate_development_runner_plan(runner_plan)
    if (
        isinstance(workers_per_node, bool)
        or not isinstance(workers_per_node, int)
        or not 1 <= workers_per_node <= MAX_WORKERS_PER_NODE
    ):
        raise FuryMultiseedHpcDispatchV1Error(
            f"workers_per_node must be 1..{MAX_WORKERS_PER_NODE}"
        )
    delegate = validated.delegate
    assignment = _assign_shards(delegate)
    shard_count = int(delegate["contract"]["shard_count"])
    flattened = sorted(index for rows in assignment.values() for index in rows)
    if flattened != list(range(shard_count)):
        raise FuryMultiseedHpcDispatchV1Error("dispatch assignment lost a shard")
    plan_sha = str(validated.plan["plan_sha256"])
    remote_run_root = (
        f"scheduleurm_work/o2o-dps-hpc/runs/fury-development-{plan_sha}"
    )
    nodes = []
    by_index = {
        int(row["shard_index"]): row
        for row in delegate["contract"]["shards"]
    }
    for node in EXPECTED_NODES:
        shard_indices = assignment[node]
        nodes.append(
            {
                "name": node,
                "workers": min(workers_per_node, len(shard_indices)),
                "shard_indices": shard_indices,
                "estimated_cost_units": sum(
                    int(by_index[index]["estimated_cost_units"])
                    for index in shard_indices
                ),
            }
        )
    return {
        "schema": DISPATCH_SCHEMA,
        "status": "READY_TO_LAUNCH",
        "phase": PHASE,
        "runner_plan_sha256": plan_sha,
        "bridge_sha256": EXPECTED_BRIDGE_SHA256,
        "required_baseline_ids": list(runner_v3.REQUIRED_BASELINE_IDS),
        "candidate_policy_id": delegate["contract"]["policies"][-1]["policy_id"],
        "master_seed_count": MASTER_SEED_COUNT,
        "scenario_count": len(delegate["contract"]["scenarios"]),
        "paired_group_count": delegate["contract"]["group_count"],
        "expected_rollout_count": delegate["contract"]["expected_rollout_count"],
        "shard_count": shard_count,
        "workers_per_node_limit": workers_per_node,
        "nodes": nodes,
        "remote_source_pointer": REMOTE_SOURCE_POINTER,
        "remote_bridge_pointer": REMOTE_BRIDGE_POINTER,
        "remote_run_root": remote_run_root,
        "production_worker_module": PRODUCTION_WORKER_MODULE,
        "execution_started": False,
        "scientific_result_available": False,
    }


def node_launch_command(
    dispatch: Mapping[str, Any],
    node_name: str,
    *,
    node_python: str,
) -> str:
    if dispatch.get("schema") != DISPATCH_SCHEMA:
        raise FuryMultiseedHpcDispatchV1Error("dispatch plan schema mismatch")
    row = next(
        (item for item in dispatch["nodes"] if item.get("name") == node_name),
        None,
    )
    if row is None or node_name not in EXPECTED_NODES:
        raise FuryMultiseedHpcDispatchV1Error("unknown dispatch node")
    shards = row["shard_indices"]
    workers = int(row["workers"])
    if not shards or workers < 1:
        raise FuryMultiseedHpcDispatchV1Error("dispatch node has no work")

    shared = "scheduleurm_work/o2o-dps-hpc"
    def home_path(relative: str) -> str:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise FuryMultiseedHpcDispatchV1Error("remote path is not home-relative")
        return f'"$HOME/{path.as_posix()}"'

    source = home_path(f"{shared}/{REMOTE_SOURCE_POINTER}")
    bridge = home_path(f"{shared}/{REMOTE_BRIDGE_POINTER}")
    runner_plan = home_path(f"{dispatch['remote_run_root']}/runner-plan.json")
    output = home_path(f"{dispatch['remote_run_root']}/output")
    node_state_relative = f"{dispatch['remote_run_root']}/nodes/{node_name}"
    node_state = home_path(node_state_relative)
    python = home_path(node_python)
    shard_lines = "\\n".join(str(value) for value in shards) + "\\n"
    worker = " ".join(
        (
            python,
            "-B -m",
            shlex.quote(PRODUCTION_WORKER_MODULE),
            "--runner-plan",
            runner_plan,
            "--bridge",
            bridge,
            "--output-directory",
            output,
            "--shard-index",
            '"$1"',
        )
    )
    inner = "; ".join(
        (
            "set +e",
            f"cd {source} || exit 90",
            "export PYTHONPATH=.",
            "export GOMAXPROCS=1",
            f"printf {shlex.quote(shard_lines)} | xargs -r -n1 -P {workers} sh -c {shlex.quote(worker)} sh",
            "rc=$?",
            f"printf '%s\\n' \"$rc\" > {home_path(node_state_relative + '/exit-code')}",
            f"if test \"$rc\" -eq 0; then touch {home_path(node_state_relative + '/complete')}; else touch {home_path(node_state_relative + '/failed')}; fi",
            "exit \"$rc\"",
        )
    )
    prefix = "; ".join(
        (
            "set -eu",
            f"test -f {home_path(f'{shared}/{REMOTE_SOURCE_POINTER}/o2o_dps/fury_multiseed_hpc_worker_v1.py')}",
            f"test -x {bridge}",
            f"test -f {runner_plan}",
            f"mkdir -p {output} {node_state}",
            f"test ! -e {home_path(node_state_relative + '/pid')}",
        )
    )
    suffix = "; ".join(
        (
            "pid=$!",
            f"printf '%s\\n' \"$pid\" > {home_path(node_state_relative + '/pid')}",
            "printf 'pid=%s\\n' \"$pid\"",
        )
    )
    return (
        f"{prefix}; nohup sh -c {shlex.quote(inner)} > "
        f"{home_path(node_state_relative + '/worker.log')} 2>&1 < /dev/null "
        f"& {suffix}"
    )


def _dynamic_v5_ready(receipt: Mapping[str, Any]) -> bool:
    try:
        verify_receipt_v5(receipt)
    except Exception:
        return False
    release = receipt.get("release")
    if not isinstance(release, Mapping):
        return False
    rows = release.get("pointer_verification")
    return (
        receipt.get("schema") == DYNAMIC_V5_RECEIPT_SCHEMA
        and receipt.get("status") == "DYNAMIC_V5_V7_BRIDGE_READY_NONSCIENTIFIC"
        and isinstance(rows, list)
        and [row.get("name") for row in rows] == list(EXPECTED_NODES)
        and all(
            row.get("status") == "READY"
            and row.get("verified_sha256") == EXPECTED_BRIDGE_SHA256
            for row in rows
        )
    )


def inspect_current_readiness(
    protocol: Mapping[str, Any],
    dynamic_v5_receipt: Mapping[str, Any] | None,
    *,
    production_worker_exists: bool,
) -> JSONMap:
    materialized = evaluation_v3.materialize_protocol(protocol)
    evaluation_plan = evaluation_v3.build_plan(materialized)
    baseline_rows = protocol["baseline_contract"]["required_baselines"]
    baseline_blocked = [
        row["policy_id"]
        for row in baseline_rows
        if row.get("comparison_eligible") is not True
    ]
    development = protocol["corpus_contract"]["phase_corpus_bindings"][PHASE]
    blockers = []
    if not _dynamic_v5_ready(dynamic_v5_receipt or {}):
        blockers.append("DYNAMIC_V5_SIX_NODE_RELEASE_NOT_READY")
    if baseline_blocked:
        blockers.append("FOUR_BASELINE_EXECUTORS_NOT_COMPARISON_READY")
    if development.get("comparison_eligible") is not True:
        blockers.append("DEVELOPMENT_CORPUS_NOT_DYNAMIC_COMPARISON_BOUND")
    registry = protocol.get("development_candidate_registry", {})
    if registry.get("status") != "FROZEN" or not registry.get("candidates"):
        blockers.append("CAT2NEW_LEARNED_DEVELOPMENT_CANDIDATE_NOT_FROZEN")
    blockers.append("CAT2NEW_V6_NOT_ADAPTED_TO_RUNNER_FULL_POLICY_V2")
    if not production_worker_exists:
        blockers.append("FIVE_LANE_PRODUCTION_WORKER_NOT_IMPLEMENTED")
    return {
        "schema": READINESS_SCHEMA,
        "status": "READY" if not blockers else "BLOCKED",
        "phase": PHASE,
        "required_master_seed_count": MASTER_SEED_COUNT,
        "required_nodes": list(EXPECTED_NODES),
        "required_baseline_ids": list(runner_v3.REQUIRED_BASELINE_IDS),
        "candidate_role": "Cat2_new action outlet for one learned candidate",
        "cat2new_v6": {
            "offline_feedback_loop_implemented": True,
            "runner_full_policy_v2_adapter_registered": False,
        },
        "dynamic_v5_ready": _dynamic_v5_ready(dynamic_v5_receipt or {}),
        "baseline_ids_not_comparison_ready": baseline_blocked,
        "evaluation_plan_blocked_reason_count": len(
            evaluation_plan["blocked_reasons"]
        ),
        "minimal_blockers": blockers,
        "runner_plan_available": False,
        "execution_started": False,
        "rollout_count": 0,
        "scientific_result_available": False,
    }


def launch_dispatch(
    dispatch: Mapping[str, Any],
    *,
    runner_plan_path: Path,
    site: HpcSite,
    transport: Any,
) -> JSONMap:
    """Stage the compact runner plan and start one supervisor per node."""

    _validate_site(site)
    runner_plan = _read_json(runner_plan_path, "runner plan")
    rebuilt = build_dispatch_plan(
        runner_plan,
        workers_per_node=int(dispatch["workers_per_node_limit"]),
    )
    if rebuilt != dispatch:
        raise FuryMultiseedHpcDispatchV1Error(
            "dispatch plan differs from its runner plan"
        )
    run_root = str(dispatch["remote_run_root"])
    primary = site.nodes[0]
    source_worker = (
        f'$HOME/scheduleurm_work/o2o-dps-hpc/{REMOTE_SOURCE_POINTER}/'
        "o2o_dps/fury_multiseed_hpc_worker_v1.py"
    )
    bridge = (
        f"$HOME/scheduleurm_work/o2o-dps-hpc/{REMOTE_BRIDGE_POINTER}"
    )
    node_python = f"$HOME/{site.node_python}"
    preflight = []
    for node in site.nodes:
        result = transport.run(
            node,
            "; ".join(
                (
                    "set -eu",
                    f'test -f "{source_worker}"',
                    f'test -x "{bridge}"',
                    f'test -x "{node_python}"',
                    f'observed=$(sha256sum "{bridge}" | cut -d " " -f1)',
                    f'test "$observed" = {shlex.quote(EXPECTED_BRIDGE_SHA256)}',
                )
            ),
            timeout=60,
        )
        preflight.append(result.returncode == 0)
    if not all(preflight):
        raise FuryMultiseedHpcDispatchV1Error(
            "remote production worker, Python, or exact dynamic-v5 bridge is missing"
        )
    prepared = transport.run(
        primary,
        f"set -eu; cd || exit 90; test ! -e {shlex.quote(run_root)}; "
        f"mkdir -p {shlex.quote(run_root + '/nodes')}",
        timeout=45,
    )
    if prepared.returncode != 0:
        raise FuryMultiseedHpcDispatchV1Error("remote run directory is not fresh")
    copied = transport.copy(
        primary,
        runner_plan_path.expanduser().resolve(),
        f"~/{run_root}/runner-plan.json",
        timeout=180,
    )
    if copied.returncode != 0:
        raise FuryMultiseedHpcDispatchV1Error("runner plan transfer failed")
    launches = []
    for node in site.nodes:
        result: CommandResult = transport.run(
            node,
            node_launch_command(
                dispatch,
                node.name,
                node_python=site.node_python,
            ),
            timeout=45,
        )
        pid_line = next(
            (line for line in result.stdout.splitlines() if line.startswith("pid=")),
            None,
        )
        launches.append(
            {
                "name": node.name,
                "status": "STARTED" if result.returncode == 0 and pid_line else "FAILED",
                "pid": None if pid_line is None else pid_line[4:],
                "transport_returncode": result.returncode,
            }
        )
        if launches[-1]["status"] != "STARTED":
            break
    return {
        "schema": "fury_multiseed_hpc_launch/v1",
        "status": (
            "RUNNING" if len(launches) == 6 and all(row["status"] == "STARTED" for row in launches)
            else "PARTIAL_LAUNCH_FAILURE"
        ),
        "runner_plan_sha256": dispatch["runner_plan_sha256"],
        "launches": launches,
        "execution_started": any(row["status"] == "STARTED" for row in launches),
        "scientific_result_available": False,
    }


def _latest_dynamic_receipt(directory: Path) -> Path | None:
    rows = sorted(
        directory.expanduser().resolve().glob("hpc-dynamic-v5.*.receipt.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    return rows[-1] if rows else None


def _write_json(path: Path | None, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path is None:
        print(payload, end="")
    else:
        destination = path.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--dynamic-v5-receipt", type=Path)
    parser.add_argument("--runner-plan", type=Path)
    parser.add_argument("--workers-per-node", type=int, default=MAX_WORKERS_PER_NODE)
    parser.add_argument("--site-config", type=Path, default=DEFAULT_SITE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.runner_plan is None:
            protocol = _read_json(args.protocol, "protocol")
            receipt_path = args.dynamic_v5_receipt or _latest_dynamic_receipt(
                DEFAULT_RECEIPT_DIRECTORY
            )
            receipt = None if receipt_path is None else _read_json(
                receipt_path, "dynamic-v5 receipt"
            )
            report = inspect_current_readiness(
                protocol,
                receipt,
                production_worker_exists=(
                    PROJECT_ROOT
                    / "o2o_dps/fury_multiseed_hpc_worker_v1.py"
                ).is_file(),
            )
            _write_json(args.output, report)
            return 0 if report["status"] == "READY" else 2
        runner_plan = _read_json(args.runner_plan, "runner plan")
        dispatch = build_dispatch_plan(
            runner_plan, workers_per_node=args.workers_per_node
        )
        if args.apply:
            site = load_site_config(args.site_config)
            report = launch_dispatch(
                dispatch,
                runner_plan_path=args.runner_plan,
                site=site,
                transport=SchedulerNodeTransport(site),
            )
        else:
            report = dispatch
        _write_json(args.output, report)
        return 0 if report["status"] in {"READY_TO_LAUNCH", "RUNNING"} else 2
    except (
        FuryMultiseedHpcDispatchV1Error,
        HpcEnvironmentError,
        evaluation_v3.FuryMultiseedProtocolError,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "DISPATCH_SCHEMA",
    "EXPECTED_NODES",
    "FuryMultiseedHpcDispatchV1Error",
    "MASTER_SEED_COUNT",
    "MAX_WORKERS_PER_NODE",
    "PHASE",
    "READINESS_SCHEMA",
    "ValidatedDevelopmentPlan",
    "build_dispatch_plan",
    "inspect_current_readiness",
    "launch_dispatch",
    "node_launch_command",
    "validate_development_runner_plan",
)
