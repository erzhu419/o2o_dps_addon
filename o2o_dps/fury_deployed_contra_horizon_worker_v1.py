"""Single-lane horizon worker for the deployed-Contra v6 execution path.

An existing four-lane runner/dispatch pair supplies the immutable scenario,
seed, and group selection.  The output is deliberately a distinct diagnostic
schema and producer, so it cannot be consumed as the frozen four-lane result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence

from .fury_dynamic_v5_deployed_contra_adapter_v5 import (
    DEPLOYED_CONTRA_V6_PRODUCER,
    FuryDynamicV5DeployedContraAdapterV5Error,
    execute_deployed_contra_v6_lane_v5,
    validate_deployed_contra_v6_artifact_v5,
)
from .fury_multiseed_hpc_worker_v3 import (
    FuryMultiseedHpcWorkerV3Error,
    _group_context,
)
from .fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    FURY_V5_PRODUCER,
    FuryPairedRunnerV4Error,
    sha256_json,
    validate_lane_result_v4,
)
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


JSONMap = dict[str, Any]
ROW_SCHEMA_V1 = "fury_deployed_contra_horizon_diagnostic_row/v1"
RECEIPT_SCHEMA_V1 = "fury_deployed_contra_horizon_group_receipt/v1"


class FuryDeployedContraHorizonWorkerV1Error(RuntimeError):
    """The versioned deployed-Contra diagnostic route is invalid."""


def execute_deployed_contra_horizon_group_v1(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
    bridge: Any,
    bridge_binding: Mapping[str, Any],
) -> tuple[JSONMap, JSONMap]:
    """Execute only deployed Contra while preserving the source group inputs."""

    if os.environ.get("GOMAXPROCS") != "1":
        raise FuryDeployedContraHorizonWorkerV1Error(
            "worker requires GOMAXPROCS=1"
        )
    try:
        plan, checked_dispatch, group, scenario, policies = _group_context(
            runner_plan,
            dispatch,
            node_name=node_name,
            group_id=group_id,
        )
    except FuryMultiseedHpcWorkerV3Error as error:
        raise FuryDeployedContraHorizonWorkerV1Error(str(error)) from error
    if checked_dispatch.get("producer_by_policy_id", {}).get(
        CONTRA_DEPLOYED_POLICY_ID
    ) != FURY_V5_PRODUCER:
        raise FuryDeployedContraHorizonWorkerV1Error(
            "source dispatch is not the frozen deployed-Contra v5 route"
        )
    policy = policies[CONTRA_DEPLOYED_POLICY_ID]
    try:
        envelope = execute_deployed_contra_v6_lane_v5(
            bridge,
            group=group,
            scenario=scenario,
            policy=policy,
        )
        lane = validate_lane_result_v4(
            envelope["lane_result"],
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validate_deployed_contra_v6_artifact_v5,
        )
    except (FuryPairedRunnerV4Error, KeyError) as error:
        raise FuryDeployedContraHorizonWorkerV1Error(str(error)) from error

    binding = _strict_json(bridge_binding, "bridge binding")
    row = {
        "schema": ROW_SCHEMA_V1,
        "source_runner_plan_sha256": plan["plan_sha256"],
        "source_dispatch_plan_sha256": sha256_json(checked_dispatch),
        "source_group_id": group_id,
        "source_policy_identity": dict(policy),
        "source_planned_producer": FURY_V5_PRODUCER,
        "selected_producer": DEPLOYED_CONTRA_V6_PRODUCER,
        "bridge_binding": binding,
        "contra_runtime_binding": {
            "mode": "SOURCE_DERIVED_DEFAULTS",
            "savedvariables_snapshot_consumed": False,
            "same_character_runtime_parity": False,
        },
        "lane_result": lane,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }
    receipt = {
        "schema": RECEIPT_SCHEMA_V1,
        "status": "COMPLETE_SIMULATOR_ONLY_SOURCE_DERIVED_NONVOTING",
        "source_runner_plan_sha256": plan["plan_sha256"],
        "source_dispatch_plan_sha256": sha256_json(checked_dispatch),
        "source_execution_kind": checked_dispatch["execution_kind"],
        "node": node_name,
        "group_id": group_id,
        "master_seed": group["master_seed"],
        "simulator_seed": group["simulator_seed"],
        "source_policy_id": CONTRA_DEPLOYED_POLICY_ID,
        "source_planned_producer": FURY_V5_PRODUCER,
        "selected_producer": DEPLOYED_CONTRA_V6_PRODUCER,
        "artifact_schema": lane["artifact_schema"],
        "artifact_sha256": lane["artifact_sha256"],
        "result_file": f"deployed-contra-v6/groups/{group_id}.json",
        "gomaxprocs": 1,
        "source_plan_execution_claimed": False,
        "savedvariables_snapshot_consumed": False,
        "same_character_runtime_parity": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }
    return receipt, row


def run_deployed_contra_horizon_worker_v1(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
    bridge: Any,
    bridge_binding: Mapping[str, Any],
    output_directory: str | Path,
) -> JSONMap:
    receipt, row = execute_deployed_contra_horizon_group_v1(
        runner_plan,
        dispatch,
        node_name=node_name,
        group_id=group_id,
        bridge=bridge,
        bridge_binding=bridge_binding,
    )
    root = Path(output_directory).expanduser().resolve()
    result_path = root / receipt["result_file"]
    receipt_path = root / "deployed-contra-v6" / "receipts" / f"{group_id}.json"
    if result_path.exists() or receipt_path.exists():
        raise FuryDeployedContraHorizonWorkerV1Error(
            "diagnostic group output already exists; inspect it before retrying"
        )
    _atomic_write(result_path, _canonical_json_bytes(row))
    try:
        _atomic_write(receipt_path, _canonical_json_bytes(receipt))
    except Exception:
        result_path.unlink(missing_ok=True)
        raise
    return receipt


def _bridge_binding(
    bridge_path: Path,
    runner_plan: Mapping[str, Any],
    *,
    diagnostic_rebind_build_id: str | None,
) -> JSONMap:
    try:
        payload = bridge_path.read_bytes()
    except OSError as error:
        raise FuryDeployedContraHorizonWorkerV1Error(
            f"could not read bridge binary: {error}"
        ) from error
    observed_sha = hashlib.sha256(payload).hexdigest()
    expected = runner_plan.get("contract", {}).get("bridge_identity")
    expected_sha = expected.get("sha256") if isinstance(expected, Mapping) else None
    if observed_sha == expected_sha:
        mode = "SOURCE_PLAN_EXACT"
        build_id = (
            expected.get("build_id")
            if isinstance(expected, Mapping)
            else None
        )
    else:
        if not isinstance(diagnostic_rebind_build_id, str) or not (
            diagnostic_rebind_build_id.strip()
        ):
            raise FuryDeployedContraHorizonWorkerV1Error(
                "bridge differs from source plan; --diagnostic-bridge-rebind-build-id is required"
            )
        mode = "DIAGNOSTIC_LOCAL_BRIDGE_REBIND"
        build_id = diagnostic_rebind_build_id.strip()
    return {
        "mode": mode,
        "source_plan_bridge_sha256": expected_sha,
        "observed_bridge_sha256": observed_sha,
        "observed_bridge_size_bytes": len(payload),
        "observed_bridge_build_id": build_id,
    }


def _strict_json(value: Mapping[str, Any], label: str) -> JSONMap:
    try:
        result = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FuryDeployedContraHorizonWorkerV1Error(
            f"{label} is not strict JSON: {error}"
        ) from error
    if not isinstance(result, dict):
        raise FuryDeployedContraHorizonWorkerV1Error(f"{label} must be an object")
    return result


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryDeployedContraHorizonWorkerV1Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryDeployedContraHorizonWorkerV1Error(f"{label} must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--bridge-cwd", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--diagnostic-bridge-rebind-build-id")
    args = parser.parse_args(argv)
    try:
        if os.environ.get("GOMAXPROCS") != "1":
            raise FuryDeployedContraHorizonWorkerV1Error(
                "worker requires GOMAXPROCS=1"
            )
        runner_plan = _load_json(args.runner_plan, "runner plan")
        dispatch = _load_json(args.dispatch_plan, "dispatch plan")
        bridge_path = args.bridge.expanduser().resolve()
        binding = _bridge_binding(
            bridge_path,
            runner_plan,
            diagnostic_rebind_build_id=args.diagnostic_bridge_rebind_build_id,
        )
        with SimulatorBridgeDynamicV3(
            bridge_path, cwd=args.bridge_cwd.expanduser().resolve()
        ) as bridge:
            receipt = run_deployed_contra_horizon_worker_v1(
                runner_plan,
                dispatch,
                node_name=args.node,
                group_id=args.group_id,
                bridge=bridge,
                bridge_binding=binding,
                output_directory=args.output_directory,
            )
        print(_canonical_json_bytes(receipt).decode("utf-8"), end="")
        return 0
    except (
        FuryDeployedContraHorizonWorkerV1Error,
        FuryDynamicV5DeployedContraAdapterV5Error,
        FuryPairedRunnerV4Error,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "FuryDeployedContraHorizonWorkerV1Error",
    "RECEIPT_SCHEMA_V1",
    "ROW_SCHEMA_V1",
    "execute_deployed_contra_horizon_group_v1",
    "run_deployed_contra_horizon_worker_v1",
)
