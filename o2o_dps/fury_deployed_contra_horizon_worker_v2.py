"""Runtime-bound deployed-Contra v7 worker over runner-v4 matched groups.

The frozen runner-v4/dispatch-v3 pair remains the authority for scenario and
seed selection.  This additive worker replaces only the deployed-Contra lane
producer with the explicit v7 runtime binding.  Its cache namespace is derived
from v7's request/seed/load/runtime identity and cannot collide with v6 output.
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

from .deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    load_deployed_contra_runtime_binding_v1,
    validate_deployed_contra_runtime_binding_v1,
)
from .fury_dynamic_v5_deployed_contra_adapter_v7 import (
    DEPLOYED_CONTRA_V7_PRODUCER,
    FuryDynamicV5DeployedContraAdapterV7Error,
    execute_deployed_contra_v7_lane_v7,
    validate_deployed_contra_v7_artifact_v7,
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
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    RAID_A_CONTROLLER,
    RAID_B_BLOCKER,
    RAID_B_CONTROLLER,
)
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


JSONMap = dict[str, Any]
ROW_SCHEMA_V2 = "fury_deployed_contra_horizon_diagnostic_row/v2"
RECEIPT_SCHEMA_V2 = "fury_deployed_contra_horizon_group_receipt/v2"
CACHE_NAMESPACE_V2 = "deployed-contra-v7"


class FuryDeployedContraHorizonWorkerV2Error(RuntimeError):
    """The runtime-bound v7 worker input, route, or output is invalid."""


def execute_deployed_contra_horizon_group_v2(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
    bridge: Any,
    bridge_binding: Mapping[str, Any],
    runtime_binding: Mapping[str, Any],
    controller: str = RAID_A_CONTROLLER,
) -> tuple[JSONMap, JSONMap]:
    """Execute v7 on the exact deployed-Contra group from the source plan."""

    if os.environ.get("GOMAXPROCS") != "1":
        raise FuryDeployedContraHorizonWorkerV2Error(
            "worker requires GOMAXPROCS=1"
        )
    if controller == RAID_B_CONTROLLER:
        raise FuryDeployedContraHorizonWorkerV2Error(
            f"{RAID_B_BLOCKER}: v7 worker cannot execute Raid-B"
        )
    if controller != RAID_A_CONTROLLER:
        raise FuryDeployedContraHorizonWorkerV2Error(
            f"unsupported deployed-Contra controller {controller!r}"
        )
    try:
        binding = validate_deployed_contra_runtime_binding_v1(runtime_binding)
        plan, checked_dispatch, group, scenario, policies = _group_context(
            runner_plan,
            dispatch,
            node_name=node_name,
            group_id=group_id,
        )
    except (
        DeployedContraRuntimeBindingError,
        FuryMultiseedHpcWorkerV3Error,
    ) as error:
        raise FuryDeployedContraHorizonWorkerV2Error(str(error)) from error
    if checked_dispatch.get("producer_by_policy_id", {}).get(
        CONTRA_DEPLOYED_POLICY_ID
    ) != FURY_V5_PRODUCER:
        raise FuryDeployedContraHorizonWorkerV2Error(
            "source dispatch is not the frozen deployed-Contra v5 route"
        )
    policy = policies[CONTRA_DEPLOYED_POLICY_ID]
    try:
        envelope = execute_deployed_contra_v7_lane_v7(
            bridge,
            group=group,
            scenario=scenario,
            policy=policy,
            runtime_binding=binding,
            controller=controller,
        )
        if set(envelope) != {"lane_result", "lane_cache_identity"}:
            raise FuryDeployedContraHorizonWorkerV2Error(
                "v7 lane envelope field set mismatch"
            )
        lane = validate_lane_result_v4(
            _mapping(envelope["lane_result"], "v7 lane_result"),
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validate_deployed_contra_v7_artifact_v7,
        )
    except (FuryPairedRunnerV4Error, KeyError) as error:
        raise FuryDeployedContraHorizonWorkerV2Error(str(error)) from error

    cache_identity = _strict_json(
        _mapping(envelope["lane_cache_identity"], "v7 lane_cache_identity"),
        "v7 lane cache identity",
    )
    artifact = _mapping(lane.get("artifact"), "v7 lane artifact")
    if cache_identity != artifact.get("lane_cache_identity"):
        raise FuryDeployedContraHorizonWorkerV2Error(
            "v7 envelope cache identity differs from the validated artifact"
        )
    if (
        cache_identity.get("runtime_binding_sha256")
        != binding["binding_sha256"]
        or cache_identity.get("request_sha256") != scenario["request_sha256"]
        or cache_identity.get("simulator_seed") != group["simulator_seed"]
        or cache_identity.get("dynamic_load_contract_sha256")
        != group["dynamic_load_contract_sha256"]
    ):
        raise FuryDeployedContraHorizonWorkerV2Error(
            "v7 cache identity is not bound to the source matched group"
        )
    cache_sha = _lower_sha256(cache_identity.get("sha256"), "v7 cache sha256")
    bridge_receipt = _strict_json(bridge_binding, "bridge binding")
    binding_identity = {
        "schema": binding["schema"],
        "binding_sha256": binding["binding_sha256"],
        "source_manifest_sha256": binding["source_manifest_sha256"],
        "loaded_source_closure_sha256": binding[
            "loaded_source_closure_sha256"
        ],
        "runtime_snapshot_sha256": binding["runtime_snapshot_sha256"],
        "contra_savedvariables_sha256": binding[
            "contra_savedvariables_sha256"
        ],
    }
    result_file = f"{CACHE_NAMESPACE_V2}/cache/{cache_sha}.json"
    receipt_file = (
        f"{CACHE_NAMESPACE_V2}/receipts/{binding['binding_sha256']}/"
        f"{plan['plan_sha256']}/{group_id}.json"
    )
    controller_coverage = _strict_json(
        _mapping(artifact.get("controller_coverage"), "controller coverage"),
        "controller coverage",
    )
    row = {
        "schema": ROW_SCHEMA_V2,
        "source_runner_plan_sha256": plan["plan_sha256"],
        "source_dispatch_plan_sha256": sha256_json(checked_dispatch),
        "source_group_id": group_id,
        "source_policy_identity": dict(policy),
        "source_planned_producer": FURY_V5_PRODUCER,
        "selected_producer": DEPLOYED_CONTRA_V7_PRODUCER,
        "bridge_binding": bridge_receipt,
        "runtime_binding_identity": binding_identity,
        "controller_coverage": controller_coverage,
        "lane_cache_identity": cache_identity,
        "lane_result": lane,
        "source_plan_execution_claimed": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }
    receipt = {
        "schema": RECEIPT_SCHEMA_V2,
        "status": "COMPLETE_SIMULATOR_ONLY_RUNTIME_BOUND_NONVOTING",
        "source_runner_plan_sha256": plan["plan_sha256"],
        "source_dispatch_plan_sha256": sha256_json(checked_dispatch),
        "source_execution_kind": checked_dispatch["execution_kind"],
        "node": node_name,
        "group_id": group_id,
        "master_seed": group["master_seed"],
        "simulator_seed": group["simulator_seed"],
        "source_policy_id": CONTRA_DEPLOYED_POLICY_ID,
        "source_planned_producer": FURY_V5_PRODUCER,
        "selected_producer": DEPLOYED_CONTRA_V7_PRODUCER,
        "artifact_schema": lane["artifact_schema"],
        "artifact_sha256": lane["artifact_sha256"],
        "runtime_binding_identity": binding_identity,
        "lane_cache_identity": cache_identity,
        "result_file": result_file,
        "receipt_file": receipt_file,
        "gomaxprocs": 1,
        "source_plan_execution_claimed": False,
        "savedvariables_snapshot_consumed": True,
        "same_character_runtime_parity": False,
        "controller_coverage": controller_coverage,
        "raid_b_blocker": RAID_B_BLOCKER,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }
    return receipt, row


def run_deployed_contra_horizon_worker_v2(
    runner_plan: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    *,
    node_name: str,
    group_id: str,
    bridge: Any,
    bridge_binding: Mapping[str, Any],
    runtime_binding: Mapping[str, Any],
    output_directory: str | Path,
    controller: str = RAID_A_CONTROLLER,
) -> JSONMap:
    receipt, row = execute_deployed_contra_horizon_group_v2(
        runner_plan,
        dispatch,
        node_name=node_name,
        group_id=group_id,
        bridge=bridge,
        bridge_binding=bridge_binding,
        runtime_binding=runtime_binding,
        controller=controller,
    )
    root = Path(output_directory).expanduser().resolve()
    result_path = root / receipt["result_file"]
    receipt_path = root / receipt["receipt_file"]
    if result_path.exists() or receipt_path.exists():
        raise FuryDeployedContraHorizonWorkerV2Error(
            "v7 cache or receipt already exists; inspect it before retrying"
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
        raise FuryDeployedContraHorizonWorkerV2Error(
            f"could not read bridge binary: {error}"
        ) from error
    observed_sha = hashlib.sha256(payload).hexdigest()
    expected = runner_plan.get("contract", {}).get("bridge_identity")
    expected_sha = expected.get("sha256") if isinstance(expected, Mapping) else None
    if observed_sha == expected_sha:
        mode = "SOURCE_PLAN_EXACT"
        build_id = expected.get("build_id") if isinstance(expected, Mapping) else None
    else:
        if not isinstance(diagnostic_rebind_build_id, str) or not (
            diagnostic_rebind_build_id.strip()
        ):
            raise FuryDeployedContraHorizonWorkerV2Error(
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


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryDeployedContraHorizonWorkerV2Error(f"{label} must be an object")
    return value


def _lower_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FuryDeployedContraHorizonWorkerV2Error(
            f"{label} must be a lowercase SHA-256"
        )
    return value


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
        raise FuryDeployedContraHorizonWorkerV2Error(
            f"{label} is not strict JSON: {error}"
        ) from error
    if not isinstance(result, dict):
        raise FuryDeployedContraHorizonWorkerV2Error(f"{label} must be an object")
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
        raise FuryDeployedContraHorizonWorkerV2Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryDeployedContraHorizonWorkerV2Error(f"{label} must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-plan", type=Path, required=True)
    parser.add_argument("--dispatch-plan", type=Path, required=True)
    parser.add_argument("--runtime-binding", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--bridge-cwd", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--controller", default=RAID_A_CONTROLLER)
    parser.add_argument("--diagnostic-bridge-rebind-build-id")
    args = parser.parse_args(argv)
    try:
        if os.environ.get("GOMAXPROCS") != "1":
            raise FuryDeployedContraHorizonWorkerV2Error(
                "worker requires GOMAXPROCS=1"
            )
        runner_plan = _load_json(args.runner_plan, "runner plan")
        dispatch = _load_json(args.dispatch_plan, "dispatch plan")
        runtime_binding = load_deployed_contra_runtime_binding_v1(
            args.runtime_binding
        )
        bridge_path = args.bridge.expanduser().resolve()
        bridge_binding = _bridge_binding(
            bridge_path,
            runner_plan,
            diagnostic_rebind_build_id=args.diagnostic_bridge_rebind_build_id,
        )
        with SimulatorBridgeDynamicV3(
            bridge_path, cwd=args.bridge_cwd.expanduser().resolve()
        ) as bridge:
            receipt = run_deployed_contra_horizon_worker_v2(
                runner_plan,
                dispatch,
                node_name=args.node,
                group_id=args.group_id,
                bridge=bridge,
                bridge_binding=bridge_binding,
                runtime_binding=runtime_binding,
                output_directory=args.output_directory,
                controller=args.controller,
            )
        print(_canonical_json_bytes(receipt).decode("utf-8"), end="")
        return 0
    except (
        DeployedContraRuntimeBindingError,
        FuryDeployedContraHorizonWorkerV2Error,
        FuryDynamicV5DeployedContraAdapterV7Error,
        FuryPairedRunnerV4Error,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "CACHE_NAMESPACE_V2",
    "FuryDeployedContraHorizonWorkerV2Error",
    "RECEIPT_SCHEMA_V2",
    "ROW_SCHEMA_V2",
    "execute_deployed_contra_horizon_group_v2",
    "run_deployed_contra_horizon_worker_v2",
)
