"""Thin six-node dispatcher for native dynamic-v5 runner-v4 diagnostics.

This module extends the frozen runner-v4 plan with an explicit group-to-node
execution plan.  It does not launch SSH jobs.  The only admitted lanes are Cat
v6, deployed Contra, and the Cat2_new v6 diagnostic candidate. Contra260817
and the external historical policy are never substituted by a generic
executor.

The development plan keeps the preregistered 256 master seeds and assigns
whole paired groups to node001--node006 with deterministic LPT.  A separate
single-group constructor exists solely for the requested local/node fixture.
Neither surface is a formal or live-fidelity comparison.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys
from typing import Any, Iterable, Mapping, Sequence

from .cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER as CAT2NEW_PRODUCER,
    cat2new_lane_contract_v3,
)
from .cat_fury_paired_lane_adapter_v6 import (
    CAT_V6_PRODUCER,
    cat_runner_v4_lane_contract_v6,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    FURY_V5_PRODUCER,
    HISTORICAL_POLICY_ID,
    SINGLE_BRIDGE_MODE,
    FuryPairedRunnerV4Error,
    builtin_lane_contracts_v4,
    validate_runner_plan,
)


JSONMap = dict[str, Any]

DISPATCH_SCHEMA_V2 = "fury_multiseed_dynamic_v5_hpc_dispatch/v2"
READINESS_SCHEMA_V2 = "fury_multiseed_dynamic_v5_hpc_readiness/v2"
DEVELOPMENT_EXECUTION_KIND = "DEVELOPMENT_DIAGNOSTIC_256"
FIXTURE_EXECUTION_KIND = "SINGLE_NODE_DIAGNOSTIC_FIXTURE"
PHASE = "development"
MASTER_SEED_COUNT = 256
EXPECTED_NODES = tuple(f"node{index:03d}" for index in range(1, 7))
ALLOWED_POLICY_IDS = (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CAT2NEW_POLICY_ID,
)
EXPECTED_PRODUCERS = {
    CAT_POLICY_ID: CAT_V6_PRODUCER,
    CONTRA_DEPLOYED_POLICY_ID: FURY_V5_PRODUCER,
    CAT2NEW_POLICY_ID: CAT2NEW_PRODUCER,
}
FORMAL_REQUIRED_POLICY_IDS = (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CONTRA260817_POLICY_ID,
    HISTORICAL_POLICY_ID,
    CAT2NEW_POLICY_ID,
)
FORMAL_MISSING_POLICY_IDS = (
    CONTRA260817_POLICY_ID,
    HISTORICAL_POLICY_ID,
)
FORMAL_BLOCKERS = (
    "CAT2NEW_LEARNED_FEEDBACK_POLICY_FACTORY_NOT_FROZEN",
    "CONTRA260817_DYNAMIC_V3_FULL_POLICY_EXECUTOR_MISSING",
    "CONTRA260817_TARGET_ITEM_EQUIPMENT_ORDERED_SINK_MISSING",
    "DYNAMIC_V5_SIMULATOR_HYPOTHESIS_NONVOTING",
    "HISTORICAL_EXTERNAL_V2_DYNAMIC_V5_LANE_ADAPTER_MISSING",
)
WORKER_MODULE = "o2o_dps.fury_multiseed_hpc_worker_v2"


class FuryMultiseedHpcDispatchV2Error(RuntimeError):
    """The native runner-v4 diagnostic dispatch contract was violated."""


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcDispatchV2Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryMultiseedHpcDispatchV2Error(f"{label} must be an object")
    return value


def _write_json(path: Path | None, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if path is None:
        print(payload, end="")
        return
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")


def _validated_contract(runner_plan: Mapping[str, Any]) -> tuple[JSONMap, JSONMap]:
    try:
        plan = validate_runner_plan(runner_plan)
    except FuryPairedRunnerV4Error as error:
        raise FuryMultiseedHpcDispatchV2Error(str(error)) from error
    contract = plan["contract"]
    if (
        contract.get("execution_mode") != SINGLE_BRIDGE_MODE
        or contract.get("plan_intent") != DIAGNOSTIC_INTENT
        or contract.get("required_bridge_command") != "load_dynamic_v3"
    ):
        raise FuryMultiseedHpcDispatchV2Error(
            "HPC execution requires a single-bridge native dynamic-v5 diagnostic plan"
        )
    return plan, contract


def _validate_admitted_lanes(contract: Mapping[str, Any]) -> None:
    if tuple(contract.get("policy_ids", ())) != ALLOWED_POLICY_IDS:
        raise FuryMultiseedHpcDispatchV2Error(
            "only Cat v6, deployed Contra, and Cat2_new diagnostic lanes are admitted"
        )
    policies = {row.get("policy_id"): row for row in contract.get("policies", [])}
    if (
        policies.get(CAT_POLICY_ID, {}).get("role") != "BASELINE"
        or policies.get(CONTRA_DEPLOYED_POLICY_ID, {}).get("role") != "BASELINE"
        or policies.get(CAT2NEW_POLICY_ID, {}).get("role") != "CANDIDATE"
    ):
        raise FuryMultiseedHpcDispatchV2Error(
            "diagnostic policy roles must be Cat and deployed Contra baselines then Cat2_new candidate"
        )
    lanes = {row.get("policy_id"): row for row in contract.get("lane_contracts", [])}
    if set(lanes) != set(ALLOWED_POLICY_IDS):
        raise FuryMultiseedHpcDispatchV2Error("diagnostic lane contracts are incomplete")
    contra_lane = next(
        row
        for row in builtin_lane_contracts_v4()
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    expected_lanes = {
        CAT_POLICY_ID: cat_runner_v4_lane_contract_v6(),
        CONTRA_DEPLOYED_POLICY_ID: contra_lane,
        CAT2NEW_POLICY_ID: cat2new_lane_contract_v3(),
    }
    for policy_id in ALLOWED_POLICY_IDS:
        lane = lanes[policy_id]
        if (
            lane != expected_lanes[policy_id]
            or
            lane.get("dynamic_v5_executable") is not True
            or lane.get("blocker_codes") != []
            or lane.get("producer") != EXPECTED_PRODUCERS[policy_id]
            or lane.get("simulator_only") is not True
            or lane.get("live_fidelity") is not False
            or lane.get("comparison_ready") is not False
        ):
            raise FuryMultiseedHpcDispatchV2Error(
                f"lane {policy_id} is not its exact admitted native diagnostic contract"
            )
    if contract.get("status") != "READY_FOR_SMALL_FIXTURE" or contract.get(
        "blocker_codes"
    ) != []:
        raise FuryMultiseedHpcDispatchV2Error(
            "runner-v4 diagnostic plan still has lane blockers"
        )


def inspect_formal_comparison_readiness_v2(
    runner_plan: Mapping[str, Any],
) -> JSONMap:
    """Report why the full five-policy comparison remains fail-closed."""

    try:
        plan = validate_runner_plan(runner_plan)
        contract = plan["contract"]
        present = tuple(contract.get("policy_ids", ()))
        runner_plan_sha256 = plan["plan_sha256"]
    except FuryPairedRunnerV4Error as error:
        return {
            "schema": READINESS_SCHEMA_V2,
            "status": "BLOCKED",
            "runner_plan_sha256": None,
            "present_policy_ids": [],
            "required_policy_ids": list(FORMAL_REQUIRED_POLICY_IDS),
            "missing_policy_ids": list(FORMAL_REQUIRED_POLICY_IDS),
            "blocker_codes": ["RUNNER_V4_PLAN_INVALID"],
            "detail": str(error),
            "execution_started": False,
            "comparison_ready": False,
            "scientific_result_available": False,
        }
    lanes = {
        row.get("policy_id"): row for row in contract.get("lane_contracts", [])
    }
    missing = [
        policy_id
        for policy_id in FORMAL_REQUIRED_POLICY_IDS
        if policy_id not in present
        or lanes.get(policy_id, {}).get("dynamic_v5_executable") is not True
    ]
    blockers = list(FORMAL_BLOCKERS)
    for code in contract.get("blocker_codes", []):
        if code not in blockers:
            blockers.append(code)
    return {
        "schema": READINESS_SCHEMA_V2,
        "status": "BLOCKED",
        "runner_plan_sha256": runner_plan_sha256,
        "present_policy_ids": list(present),
        "required_policy_ids": list(FORMAL_REQUIRED_POLICY_IDS),
        "missing_policy_ids": missing,
        "blocker_codes": sorted(blockers),
        "execution_started": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


def _lpt_groups(
    groups: Iterable[Mapping[str, Any]], nodes: Sequence[str]
) -> tuple[list[JSONMap], ...]:
    assignment: dict[str, list[JSONMap]] = {node: [] for node in nodes}
    loads = {node: 0 for node in nodes}
    for raw in sorted(
        groups,
        key=lambda row: (-int(row["estimated_cost_units"]), str(row["group_id"])),
    ):
        node = min(nodes, key=lambda name: (loads[name], len(assignment[name]), name))
        row = dict(raw)
        assignment[node].append(row)
        loads[node] += int(row["estimated_cost_units"])
    return tuple(assignment[node] for node in nodes)


def _build_dispatch(
    plan: JSONMap,
    contract: JSONMap,
    *,
    nodes: Sequence[str],
    workers_per_node: int,
    execution_kind: str,
) -> JSONMap:
    if (
        isinstance(workers_per_node, bool)
        or not isinstance(workers_per_node, int)
        or workers_per_node < 1
    ):
        raise FuryMultiseedHpcDispatchV2Error(
            "workers_per_node must be a positive integer"
        )
    assigned = _lpt_groups(contract["groups"], nodes)
    node_rows = []
    for node, groups in zip(nodes, assigned, strict=True):
        node_rows.append(
            {
                "name": node,
                "workers": min(workers_per_node, len(groups)),
                "group_ids": sorted(str(row["group_id"]) for row in groups),
                "group_count": len(groups),
                "estimated_cost_units": sum(
                    int(row["estimated_cost_units"]) for row in groups
                ),
            }
        )
    if sorted(
        group_id for row in node_rows for group_id in row["group_ids"]
    ) != sorted(str(row["group_id"]) for row in contract["groups"]):
        raise FuryMultiseedHpcDispatchV2Error("LPT assignment lost a paired group")
    return {
        "schema": DISPATCH_SCHEMA_V2,
        "status": "PREPARED_DIAGNOSTIC_NOT_EXECUTED",
        "execution_kind": execution_kind,
        "phase": contract["phase"],
        "runner_plan_sha256": plan["plan_sha256"],
        "master_seed_count": len(contract["seed_derivation"]["master_seeds"]),
        "scenario_count": len(contract["scenarios"]),
        "paired_group_count": contract["group_count"],
        "expected_rollout_count": contract["expected_rollout_count"],
        "policy_ids": list(ALLOWED_POLICY_IDS),
        "producer_by_policy_id": dict(EXPECTED_PRODUCERS),
        "assignment_algorithm": "LPT_WHOLE_PAIRED_GROUP_V2",
        "gomaxprocs": 1,
        "nodes": node_rows,
        "worker_module": WORKER_MODULE,
        "runtime_requirement": "CAT2NEW_FEEDBACK_POLICY_FACTORY_REQUIRED",
        "formal_comparison_status": "BLOCKED",
        "formal_missing_policy_ids": list(FORMAL_MISSING_POLICY_IDS),
        "formal_blocker_codes": list(FORMAL_BLOCKERS),
        "execution_started": False,
        "heavy_execution_started": False,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


def build_development_dispatch_plan_v2(
    runner_plan: Mapping[str, Any], *, workers_per_node: int
) -> JSONMap:
    """Prepare, but do not launch, the fixed 256-seed six-node diagnostic."""

    plan, contract = _validated_contract(runner_plan)
    _validate_admitted_lanes(contract)
    seeds = contract["seed_derivation"]["master_seeds"]
    if (
        contract.get("phase") != PHASE
        or len(seeds) != MASTER_SEED_COUNT
        or len(set(seeds)) != MASTER_SEED_COUNT
        or contract.get("group_count") != len(contract["scenarios"]) * MASTER_SEED_COUNT
    ):
        raise FuryMultiseedHpcDispatchV2Error(
            "development diagnostic must retain 256 unique master seeds"
        )
    if contract["group_count"] < len(EXPECTED_NODES):
        raise FuryMultiseedHpcDispatchV2Error(
            "development diagnostic cannot populate all six nodes"
        )
    return _build_dispatch(
        plan,
        contract,
        nodes=EXPECTED_NODES,
        workers_per_node=workers_per_node,
        execution_kind=DEVELOPMENT_EXECUTION_KIND,
    )


def build_single_node_fixture_dispatch_v2(
    runner_plan: Mapping[str, Any], *, node_name: str = "node001"
) -> JSONMap:
    """Prepare one two-lane group for bounded local or single-node smoke."""

    plan, contract = _validated_contract(runner_plan)
    _validate_admitted_lanes(contract)
    if (
        contract.get("group_count") != 1
        or contract.get("shard_count") != 1
        or len(contract["seed_derivation"]["master_seeds"]) != 1
        or node_name not in EXPECTED_NODES
    ):
        raise FuryMultiseedHpcDispatchV2Error(
            "fixture dispatch requires one seed, one group, one shard, and node001--node006"
        )
    return _build_dispatch(
        plan,
        contract,
        nodes=(node_name,),
        workers_per_node=1,
        execution_kind=FIXTURE_EXECUTION_KIND,
    )


def validate_dispatch_plan_v2(
    value: Mapping[str, Any], runner_plan: Mapping[str, Any]
) -> JSONMap:
    plan, contract = _validated_contract(runner_plan)
    _validate_admitted_lanes(contract)
    if value.get("schema") != DISPATCH_SCHEMA_V2:
        raise FuryMultiseedHpcDispatchV2Error("dispatch schema mismatch")
    kind = value.get("execution_kind")
    if kind == DEVELOPMENT_EXECUTION_KIND:
        nodes = value.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(EXPECTED_NODES):
            raise FuryMultiseedHpcDispatchV2Error(
                "development dispatch node set mismatch"
            )
        worker_counts = [row.get("workers") for row in nodes if isinstance(row, Mapping)]
        if (
            len(worker_counts) != len(nodes)
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 1
                for count in worker_counts
            )
        ):
            raise FuryMultiseedHpcDispatchV2Error(
                "development dispatch worker counts are invalid"
            )
        rebuilt = build_development_dispatch_plan_v2(
            plan,
            workers_per_node=max(worker_counts),
        )
    elif kind == FIXTURE_EXECUTION_KIND:
        nodes = value.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 1:
            raise FuryMultiseedHpcDispatchV2Error("fixture dispatch node set mismatch")
        rebuilt = build_single_node_fixture_dispatch_v2(
            plan, node_name=str(nodes[0].get("name"))
        )
    else:
        raise FuryMultiseedHpcDispatchV2Error("unknown dispatch execution kind")
    if dict(value) != rebuilt:
        raise FuryMultiseedHpcDispatchV2Error(
            "dispatch plan is not the deterministic plan for runner-v4"
        )
    return rebuilt


def node_worker_command_v2(
    dispatch: Mapping[str, Any],
    node_name: str,
    *,
    runner_plan_path: str,
    dispatch_plan_path: str,
    bridge_path: str,
    bridge_cwd: str,
    output_directory: str,
    cat2_policy_factory: str,
    python_executable: str = "python3",
) -> str:
    """Return the foreground worker command; transport/launch stays external."""

    rows = [row for row in dispatch.get("nodes", []) if row.get("name") == node_name]
    if dispatch.get("schema") != DISPATCH_SCHEMA_V2 or len(rows) != 1:
        raise FuryMultiseedHpcDispatchV2Error("unknown dispatch node")
    row = rows[0]
    group_ids = row.get("group_ids")
    workers = row.get("workers")
    if not isinstance(group_ids, list) or not group_ids or not isinstance(workers, int):
        raise FuryMultiseedHpcDispatchV2Error("dispatch node has no work")
    def shell_argument(value: str) -> str:
        if value == "$HOME":
            return '"$HOME"'
        if value.startswith("$HOME/"):
            return '"$HOME"/' + shlex.quote(value[6:])
        return shlex.quote(value)

    worker = " ".join(
        (
            shell_argument(python_executable),
            "-B -m",
            shlex.quote(WORKER_MODULE),
            "--runner-plan",
            shell_argument(runner_plan_path),
            "--dispatch-plan",
            shell_argument(dispatch_plan_path),
            "--node",
            shlex.quote(node_name),
            "--group-id",
            '"$1"',
            "--bridge",
            shell_argument(bridge_path),
            "--bridge-cwd",
            shell_argument(bridge_cwd),
            "--output-directory",
            shell_argument(output_directory),
            "--cat2-policy-factory",
            shlex.quote(cat2_policy_factory),
        )
    )
    lines = "\n".join(group_ids) + "\n"
    return "; ".join(
        (
            "set -eu",
            "export GOMAXPROCS=1",
            f"printf {shlex.quote(lines)} | xargs -r -n1 -P {workers} sh -c {shlex.quote(worker)} sh",
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    development = commands.add_parser("plan")
    development.add_argument("--runner-plan", type=Path, required=True)
    development.add_argument("--workers-per-node", type=int, default=32)
    development.add_argument("--output", type=Path)
    fixture = commands.add_parser("fixture-plan")
    fixture.add_argument("--runner-plan", type=Path, required=True)
    fixture.add_argument("--node", default="node001")
    fixture.add_argument("--output", type=Path)
    readiness = commands.add_parser("readiness")
    readiness.add_argument("--runner-plan", type=Path, required=True)
    readiness.add_argument("--output", type=Path)
    command = commands.add_parser("command")
    command.add_argument("--dispatch-plan", type=Path, required=True)
    command.add_argument("--node", required=True)
    command.add_argument("--runner-plan-path", required=True)
    command.add_argument("--remote-dispatch-plan-path", required=True)
    command.add_argument("--bridge", required=True)
    command.add_argument("--bridge-cwd", required=True)
    command.add_argument("--output-directory", required=True)
    command.add_argument("--cat2-policy-factory", required=True)
    command.add_argument("--python", default="python3")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            value = build_development_dispatch_plan_v2(
                _read_json(args.runner_plan, "runner plan"),
                workers_per_node=args.workers_per_node,
            )
            _write_json(args.output, value)
        elif args.command == "fixture-plan":
            value = build_single_node_fixture_dispatch_v2(
                _read_json(args.runner_plan, "runner plan"), node_name=args.node
            )
            _write_json(args.output, value)
        elif args.command == "readiness":
            value = inspect_formal_comparison_readiness_v2(
                _read_json(args.runner_plan, "runner plan")
            )
            _write_json(args.output, value)
            return 2
        else:
            dispatch = _read_json(args.dispatch_plan, "dispatch plan")
            print(
                node_worker_command_v2(
                    dispatch,
                    args.node,
                    runner_plan_path=args.runner_plan_path,
                    dispatch_plan_path=args.remote_dispatch_plan_path,
                    bridge_path=args.bridge,
                    bridge_cwd=args.bridge_cwd,
                    output_directory=args.output_directory,
                    cat2_policy_factory=args.cat2_policy_factory,
                    python_executable=args.python,
                )
            )
        return 0
    except FuryMultiseedHpcDispatchV2Error as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "ALLOWED_POLICY_IDS",
    "DEVELOPMENT_EXECUTION_KIND",
    "DISPATCH_SCHEMA_V2",
    "EXPECTED_NODES",
    "EXPECTED_PRODUCERS",
    "FIXTURE_EXECUTION_KIND",
    "FORMAL_BLOCKERS",
    "FORMAL_MISSING_POLICY_IDS",
    "FORMAL_REQUIRED_POLICY_IDS",
    "FuryMultiseedHpcDispatchV2Error",
    "MASTER_SEED_COUNT",
    "PHASE",
    "READINESS_SCHEMA_V2",
    "WORKER_MODULE",
    "build_development_dispatch_plan_v2",
    "build_single_node_fixture_dispatch_v2",
    "inspect_formal_comparison_readiness_v2",
    "node_worker_command_v2",
    "validate_dispatch_plan_v2",
)
