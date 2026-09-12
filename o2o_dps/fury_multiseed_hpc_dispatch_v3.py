"""Four-lane development dispatcher for native dynamic-v3 runner-v4.

This additive surface admits Cat, deployed Contra, Contra260817's source
oracle, and the Cat2_new candidate.  It is deliberately simulator-only and
does not alter the frozen formal-readiness gate exposed by the v2 dispatcher.
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
from .contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    contra260817_runner_v4_lane_contract_v4,
)
from .fury_multiseed_hpc_dispatch_v2 import (
    EXPECTED_NODES,
    FORMAL_BLOCKERS,
    FORMAL_MISSING_POLICY_IDS,
    FORMAL_REQUIRED_POLICY_IDS,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    FURY_V5_PRODUCER,
    SINGLE_BRIDGE_MODE,
    FuryPairedRunnerV4Error,
    builtin_lane_contracts_v4,
    validate_runner_plan,
)


JSONMap = dict[str, Any]

DISPATCH_SCHEMA_V3 = "fury_multiseed_dynamic_v5_hpc_dispatch/v3"
DEVELOPMENT_EXECUTION_KIND_V3 = "DEVELOPMENT_DIAGNOSTIC_256_FOUR_LANE"
FIXTURE_EXECUTION_KIND_V3 = "SINGLE_NODE_DIAGNOSTIC_FIXTURE_FOUR_LANE"
PHASE = "development"
MASTER_SEED_COUNT = 256
WORKER_MODULE_V3 = "o2o_dps.fury_multiseed_hpc_worker_v3"
ALLOWED_POLICY_IDS_V3 = (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CAT2NEW_POLICY_ID,
)
EXPECTED_PRODUCERS_V3 = {
    CAT_POLICY_ID: CAT_V6_PRODUCER,
    CONTRA_DEPLOYED_POLICY_ID: FURY_V5_PRODUCER,
    CONTRA260817_POLICY_ID: CONTRA260817_V4_PRODUCER,
    CAT2NEW_POLICY_ID: CAT2NEW_PRODUCER,
}


class FuryMultiseedHpcDispatchV3Error(RuntimeError):
    """The four-lane development dispatch contract was violated."""


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryMultiseedHpcDispatchV3Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryMultiseedHpcDispatchV3Error(f"{label} must be an object")
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
        raise FuryMultiseedHpcDispatchV3Error(str(error)) from error
    contract = plan["contract"]
    if (
        contract.get("execution_mode") != SINGLE_BRIDGE_MODE
        or contract.get("plan_intent") != DIAGNOSTIC_INTENT
        or contract.get("required_bridge_command") != "load_dynamic_v3"
    ):
        raise FuryMultiseedHpcDispatchV3Error(
            "four-lane execution requires a single-bridge native dynamic-v3 diagnostic plan"
        )
    return plan, contract


def _validate_admitted_lanes(contract: Mapping[str, Any]) -> None:
    if tuple(contract.get("policy_ids", ())) != ALLOWED_POLICY_IDS_V3:
        raise FuryMultiseedHpcDispatchV3Error(
            "development lanes must be ordered Cat, deployed Contra, Contra260817, Cat2_new"
        )
    policies = {row.get("policy_id"): row for row in contract.get("policies", [])}
    expected_roles = {
        CAT_POLICY_ID: "BASELINE",
        CONTRA_DEPLOYED_POLICY_ID: "BASELINE",
        CONTRA260817_POLICY_ID: "BASELINE",
        CAT2NEW_POLICY_ID: "CANDIDATE",
    }
    if set(policies) != set(expected_roles) or any(
        policies[policy_id].get("role") != role
        for policy_id, role in expected_roles.items()
    ):
        raise FuryMultiseedHpcDispatchV3Error(
            "four-lane development policy roles are invalid"
        )
    lanes = {row.get("policy_id"): row for row in contract.get("lane_contracts", [])}
    deployed_contra_lane = next(
        row
        for row in builtin_lane_contracts_v4()
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    expected_lanes = {
        CAT_POLICY_ID: cat_runner_v4_lane_contract_v6(),
        CONTRA_DEPLOYED_POLICY_ID: deployed_contra_lane,
        CONTRA260817_POLICY_ID: contra260817_runner_v4_lane_contract_v4(),
        CAT2NEW_POLICY_ID: cat2new_lane_contract_v3(),
    }
    if set(lanes) != set(expected_lanes):
        raise FuryMultiseedHpcDispatchV3Error(
            "four-lane development contracts are incomplete"
        )
    for policy_id in ALLOWED_POLICY_IDS_V3:
        lane = lanes[policy_id]
        if (
            lane != expected_lanes[policy_id]
            or lane.get("dynamic_v5_executable") is not True
            or lane.get("blocker_codes") != []
            or lane.get("producer") != EXPECTED_PRODUCERS_V3[policy_id]
            or lane.get("simulator_only") is not True
            or lane.get("live_fidelity") is not False
            or lane.get("comparison_ready") is not False
        ):
            raise FuryMultiseedHpcDispatchV3Error(
                f"lane {policy_id} is not its exact admitted development contract"
            )
    if contract.get("status") != "READY_FOR_SMALL_FIXTURE" or contract.get(
        "blocker_codes"
    ) != []:
        raise FuryMultiseedHpcDispatchV3Error(
            "runner-v4 development plan still has lane blockers"
        )


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
        raise FuryMultiseedHpcDispatchV3Error(
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
        raise FuryMultiseedHpcDispatchV3Error("LPT assignment lost a paired group")
    return {
        "schema": DISPATCH_SCHEMA_V3,
        "status": "PREPARED_DIAGNOSTIC_NOT_EXECUTED",
        "execution_kind": execution_kind,
        "phase": contract["phase"],
        "runner_plan_sha256": plan["plan_sha256"],
        "master_seed_count": len(contract["seed_derivation"]["master_seeds"]),
        "scenario_count": len(contract["scenarios"]),
        "paired_group_count": contract["group_count"],
        "expected_rollout_count": contract["expected_rollout_count"],
        "policy_ids": list(ALLOWED_POLICY_IDS_V3),
        "producer_by_policy_id": dict(EXPECTED_PRODUCERS_V3),
        "assignment_algorithm": "LPT_WHOLE_PAIRED_GROUP_V3",
        "gomaxprocs": 1,
        "nodes": node_rows,
        "worker_module": WORKER_MODULE_V3,
        "runtime_requirement": "CAT2NEW_FEEDBACK_POLICY_FACTORY_REQUIRED",
        "formal_comparison_status": "BLOCKED",
        "formal_gate_source": "fury_multiseed_hpc_dispatch_v2",
        "formal_missing_policy_ids": list(FORMAL_MISSING_POLICY_IDS),
        "formal_blocker_codes": list(FORMAL_BLOCKERS),
        "execution_started": False,
        "heavy_execution_started": False,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


def build_development_dispatch_plan_v3(
    runner_plan: Mapping[str, Any], *, workers_per_node: int
) -> JSONMap:
    """Prepare, without launching, the 256-seed six-node four-lane diagnostic."""

    plan, contract = _validated_contract(runner_plan)
    _validate_admitted_lanes(contract)
    seeds = contract["seed_derivation"]["master_seeds"]
    if (
        contract.get("phase") != PHASE
        or len(seeds) != MASTER_SEED_COUNT
        or len(set(seeds)) != MASTER_SEED_COUNT
        or contract.get("group_count")
        != len(contract["scenarios"]) * MASTER_SEED_COUNT
    ):
        raise FuryMultiseedHpcDispatchV3Error(
            "development diagnostic must retain 256 unique master seeds"
        )
    if contract["group_count"] < len(EXPECTED_NODES):
        raise FuryMultiseedHpcDispatchV3Error(
            "development diagnostic cannot populate all six nodes"
        )
    return _build_dispatch(
        plan,
        contract,
        nodes=EXPECTED_NODES,
        workers_per_node=workers_per_node,
        execution_kind=DEVELOPMENT_EXECUTION_KIND_V3,
    )


def build_single_node_fixture_dispatch_v3(
    runner_plan: Mapping[str, Any], *, node_name: str = "node001"
) -> JSONMap:
    """Prepare one paired four-lane group for a bounded real-bridge smoke."""

    plan, contract = _validated_contract(runner_plan)
    _validate_admitted_lanes(contract)
    if (
        contract.get("group_count") != 1
        or contract.get("shard_count") != 1
        or len(contract["seed_derivation"]["master_seeds"]) != 1
        or node_name not in EXPECTED_NODES
    ):
        raise FuryMultiseedHpcDispatchV3Error(
            "fixture dispatch requires one seed, one group, one shard, and node001--node006"
        )
    return _build_dispatch(
        plan,
        contract,
        nodes=(node_name,),
        workers_per_node=1,
        execution_kind=FIXTURE_EXECUTION_KIND_V3,
    )


def validate_dispatch_plan_v3(
    value: Mapping[str, Any], runner_plan: Mapping[str, Any]
) -> JSONMap:
    plan, contract = _validated_contract(runner_plan)
    _validate_admitted_lanes(contract)
    if value.get("schema") != DISPATCH_SCHEMA_V3:
        raise FuryMultiseedHpcDispatchV3Error("dispatch schema mismatch")
    kind = value.get("execution_kind")
    if kind == DEVELOPMENT_EXECUTION_KIND_V3:
        nodes = value.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(EXPECTED_NODES):
            raise FuryMultiseedHpcDispatchV3Error(
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
            raise FuryMultiseedHpcDispatchV3Error(
                "development dispatch worker counts are invalid"
            )
        rebuilt = build_development_dispatch_plan_v3(
            plan, workers_per_node=max(worker_counts)
        )
    elif kind == FIXTURE_EXECUTION_KIND_V3:
        nodes = value.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 1:
            raise FuryMultiseedHpcDispatchV3Error("fixture dispatch node set mismatch")
        rebuilt = build_single_node_fixture_dispatch_v3(
            plan, node_name=str(nodes[0].get("name"))
        )
    else:
        raise FuryMultiseedHpcDispatchV3Error("unknown dispatch execution kind")
    if dict(value) != rebuilt:
        raise FuryMultiseedHpcDispatchV3Error(
            "dispatch plan is not deterministic for the runner-v4 plan"
        )
    return rebuilt


def node_worker_command_v3(
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
    """Return a foreground per-node worker command; launch remains external."""

    rows = [row for row in dispatch.get("nodes", []) if row.get("name") == node_name]
    if dispatch.get("schema") != DISPATCH_SCHEMA_V3 or len(rows) != 1:
        raise FuryMultiseedHpcDispatchV3Error("unknown dispatch node")
    row = rows[0]
    group_ids = row.get("group_ids")
    workers = row.get("workers")
    if not isinstance(group_ids, list) or not group_ids or not isinstance(workers, int):
        raise FuryMultiseedHpcDispatchV3Error("dispatch node has no work")

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
            shlex.quote(WORKER_MODULE_V3),
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
            value = build_development_dispatch_plan_v3(
                _read_json(args.runner_plan, "runner plan"),
                workers_per_node=args.workers_per_node,
            )
            _write_json(args.output, value)
        elif args.command == "fixture-plan":
            value = build_single_node_fixture_dispatch_v3(
                _read_json(args.runner_plan, "runner plan"), node_name=args.node
            )
            _write_json(args.output, value)
        else:
            dispatch = _read_json(args.dispatch_plan, "dispatch plan")
            print(
                node_worker_command_v3(
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
    except FuryMultiseedHpcDispatchV3Error as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "ALLOWED_POLICY_IDS_V3",
    "DEVELOPMENT_EXECUTION_KIND_V3",
    "DISPATCH_SCHEMA_V3",
    "EXPECTED_PRODUCERS_V3",
    "FIXTURE_EXECUTION_KIND_V3",
    "FuryMultiseedHpcDispatchV3Error",
    "WORKER_MODULE_V3",
    "build_development_dispatch_plan_v3",
    "build_single_node_fixture_dispatch_v3",
    "node_worker_command_v3",
    "validate_dispatch_plan_v3",
)
