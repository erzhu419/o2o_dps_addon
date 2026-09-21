"""Parallel remote worker for the frozen V8 A0--A3 attribution panel."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from .upper_kara_cat_action_plan_distiller_v8 import (
    load_upper_kara_cat_action_plan_distillation_v8,
)
from .upper_kara_causal_program_remote_worker_v1 import _atomic_create_json
from .upper_kara_v8_attribution_panel_v1 import (
    EXECUTION_MODES,
    SCHEMA as ATTRIBUTION_SCHEMA,
    evaluate_v8_attribution_seed_v1,
    load_v8_attribution_contract_v1,
    summarize_v8_attribution_rows_v1,
)


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_v8_attribution_remote_worker/v1"
DEFAULT_SHARD_COUNT = 6


def _read_json(path: str | Path, label: str) -> JSONMap:
    try:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def seed_output_name_v1(execution_mode: str, seed_index: int) -> str:
    if execution_mode not in EXECUTION_MODES:
        raise ValueError("unsupported attribution execution mode")
    if type(seed_index) is not int or seed_index < 0:
        raise ValueError("seed_index must be a nonnegative integer")
    mode = "e0" if execution_mode == EXECUTION_MODES[0] else "e1"
    return f"attribution--{mode}--seed-{seed_index:03d}.json"


def assigned_seed_indices_v1(
    seed_count: int,
    *,
    shard_index: int,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> tuple[int, ...]:
    """Return the deterministic ``shard_index::shard_count`` assignment."""

    if type(seed_count) is not int or seed_count < 1:
        raise ValueError("seed_count must be a positive integer")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("shard_count must be a positive integer")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    assigned = tuple(range(shard_index, seed_count, shard_count))
    if not assigned:
        raise ValueError("shard has no assigned seeds")
    return assigned


def _parent_policy(contract: Any, bundle_path: str | Path) -> Any:
    bundle = _read_json(bundle_path, "V8 residual policy bundle")
    if bundle.get("loadout_id") != contract.loadout_id:
        raise ValueError("V8 bundle loadout identity differs from attribution contract")
    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    try:
        policy = policies[contract.parent_policy_id]
    except KeyError as error:
        raise ValueError("frozen attribution parent is absent from policy bundle") from error
    if (
        policy.exact_build_id != contract.build_id
        or len(policy.steps) != 1
        or policy.to_dict() != contract.parent_policy_wire
    ):
        raise ValueError("attribution parent frozen wire identity differs")
    return policy


def _validated_v8_attribution_inputs(
    contract: Any,
    *,
    policy_bundle_path: str | Path,
    bridge_path: str | Path,
    runtime_binding_path: str | Path,
) -> tuple[Any, JSONMap]:
    policy = _parent_policy(contract, policy_bundle_path)
    bridge_name = Path(bridge_path).expanduser().name
    if bridge_name != contract.bridge_artifact_name:
        raise ValueError("attribution bridge artifact differs from frozen contract")
    binding = load_deployed_contra_runtime_binding_v1(runtime_binding_path)
    if binding.get("binding_sha256") != contract.runtime_binding_id:
        raise ValueError("attribution runtime binding differs from frozen contract")
    return policy, {
        "parent_policy_id": policy.policy_id,
        "loadout_id": contract.loadout_id,
        "bridge_artifact_name": bridge_name,
        "runtime_binding_id": binding["binding_sha256"],
    }


def validate_v8_attribution_inputs_v1(
    contract: Any,
    *,
    policy_bundle_path: str | Path,
    bridge_path: str | Path,
    runtime_binding_path: str | Path,
) -> JSONMap:
    """Validate the compact frozen execution closure before remote staging."""

    _, identity = _validated_v8_attribution_inputs(
        contract,
        policy_bundle_path=policy_bundle_path,
        bridge_path=bridge_path,
        runtime_binding_path=runtime_binding_path,
    )
    return identity


def run_v8_attribution_seed_remote_v1(
    *,
    contract_path: str | Path,
    policy_bundle_path: str | Path,
    seed_index: int,
    execution_mode: str,
    output_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    arm_workers: int = 3,
) -> JSONMap:
    contract = load_v8_attribution_contract_v1(contract_path)
    if execution_mode not in contract.execution_modes:
        raise ValueError("execution mode is outside the frozen contract")
    examples = contract.examples()
    if type(seed_index) is not int or not 0 <= seed_index < len(examples):
        raise ValueError("seed_index is outside the frozen attribution panel")
    seed, arrival_ms = examples[seed_index]
    policy, identity = _validated_v8_attribution_inputs(
        contract,
        policy_bundle_path=policy_bundle_path,
        bridge_path=bridge_path,
        runtime_binding_path=runtime_binding_path,
    )
    result = evaluate_v8_attribution_seed_v1(
        policy,
        seed=seed,
        build_id=contract.build_id,
        loadout_id=contract.loadout_id,
        first_wave_arrival_ms=arrival_ms,
        max_decisions=contract.max_decisions,
        arm_workers=arm_workers,
        execution_mode=execution_mode,
        press_period_ms=contract.press_period_ms,
        press_phase_ms=contract.press_phase_ms,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
    )
    result["remote_task"] = {
        "schema": SCHEMA,
        "experiment_id": contract.experiment_id,
        "seed_index": seed_index,
        "execution_mode": execution_mode,
        "external_press_clock": (
            {
                "period_ms": contract.press_period_ms,
                "phase_ms": contract.press_phase_ms,
            }
            if execution_mode == "E1_EXTERNAL_PRESS_CLOCK"
            else None
        ),
        "bridge_artifact_name": identity["bridge_artifact_name"],
        "runtime_binding_artifact_name": Path(runtime_binding_path).name,
        "runtime_binding_id": identity["runtime_binding_id"],
    }
    _atomic_create_json(output_path, result)
    return result


def run_v8_attribution_shard_remote_v1(
    *,
    contract_path: str | Path,
    policy_bundle_path: str | Path,
    shard_index: int,
    shard_count: int,
    execution_mode: str,
    output_dir: str | Path,
    summary_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    seed_workers: int = 32,
    arm_workers: int = 3,
    seed_runner: Callable[..., JSONMap] = run_v8_attribution_seed_remote_v1,
) -> JSONMap:
    """Run one resumable seed shard with bounded nested concurrency.

    A seed block owns four attribution arms.  The default evaluator executes
    the three nontrivial paired arms concurrently, and every paired evaluator
    runs its exact-Cat and residual native lanes concurrently.  With one CPU
    per native bridge, a scheduler task must therefore reserve
    ``seed_workers * arm_workers * 2`` CPUs.  Existing immutable seed files are
    accepted only after their frozen identities validate; malformed or foreign
    files fail the shard instead of being overwritten.
    """

    if type(seed_workers) is not int or seed_workers < 1:
        raise ValueError("seed_workers must be a positive integer")
    if type(arm_workers) is not int or arm_workers < 1:
        raise ValueError("arm_workers must be a positive integer")
    contract = load_v8_attribution_contract_v1(contract_path)
    if execution_mode not in contract.execution_modes:
        raise ValueError("execution mode is outside the frozen contract")
    assigned = assigned_seed_indices_v1(
        contract.seed_count,
        shard_index=shard_index,
        shard_count=shard_count,
    )
    root = Path(output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    def run_one(seed_index: int) -> tuple[JSONMap, bool]:
        output = root / seed_output_name_v1(execution_mode, seed_index)
        if output.exists():
            return (
                _validate_seed_result(
                    _read_json(output, "existing attribution seed result"),
                    contract=contract,
                    seed_index=seed_index,
                    execution_mode=execution_mode,
                    expected_bridge_artifact_name=Path(bridge_path).name,
                    expected_runtime_binding_artifact_name=(
                        Path(runtime_binding_path).name
                    ),
                ),
                True,
            )
        value = seed_runner(
            contract_path=contract_path,
            policy_bundle_path=policy_bundle_path,
            seed_index=seed_index,
            execution_mode=execution_mode,
            output_path=output,
            bridge_path=bridge_path,
            bridge_cwd=bridge_cwd,
            runtime_binding_path=runtime_binding_path,
            arm_workers=arm_workers,
        )
        return (
            _validate_seed_result(
                value,
                contract=contract,
                seed_index=seed_index,
                execution_mode=execution_mode,
                expected_bridge_artifact_name=Path(bridge_path).name,
                expected_runtime_binding_artifact_name=(
                    Path(runtime_binding_path).name
                ),
            ),
            False,
        )

    worker_count = min(seed_workers, len(assigned))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        completed = list(executor.map(run_one, assigned))
    rows = [row for row, _ in completed]
    reused_count = sum(1 for _, reused in completed if reused)
    partial = summarize_v8_attribution_rows_v1(rows)
    summary: JSONMap = {
        "schema": f"{SCHEMA}/shard",
        "status": "COMPLETED_ATTRIBUTION_SHARD",
        "experiment_id": contract.experiment_id,
        "parent_policy_id": contract.parent_policy_id,
        "build_id": contract.build_id,
        "loadout_id": contract.loadout_id,
        "execution_mode": execution_mode,
        "external_press_clock": (
            {
                "period_ms": contract.press_period_ms,
                "phase_ms": contract.press_phase_ms,
            }
            if execution_mode == "E1_EXTERNAL_PRESS_CLOCK"
            else None
        ),
        "bridge_artifact_name": Path(bridge_path).name,
        "runtime_binding_artifact_name": Path(runtime_binding_path).name,
        "runtime_binding_id": contract.runtime_binding_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "assigned_seed_indices": list(assigned),
        "assigned_seed_count": len(assigned),
        "completed_seed_count": len(rows),
        "new_seed_count": len(rows) - reused_count,
        "reused_valid_seed_count": reused_count,
        "seed_workers": worker_count,
        "arm_workers_per_seed": arm_workers,
        "native_lanes_per_paired_arm": 2,
        "maximum_concurrent_arm_lanes": worker_count * arm_workers * 2,
        "seed_output_dir": str(root),
        "partial_attribution_summary": partial,
    }
    _atomic_create_json(summary_path, summary)
    return summary


def _validate_seed_result(
    value: Mapping[str, Any],
    *,
    contract: Any,
    seed_index: int,
    execution_mode: str,
    expected_bridge_artifact_name: str | None = None,
    expected_runtime_binding_artifact_name: str | None = None,
) -> JSONMap:
    seed, arrival_ms = contract.examples()[seed_index]
    task = value.get("remote_task")
    expected_clock = (
        {
            "period_ms": contract.press_period_ms,
            "phase_ms": contract.press_phase_ms,
        }
        if execution_mode == "E1_EXTERNAL_PRESS_CLOCK"
        else None
    )
    if (
        value.get("schema") != f"{ATTRIBUTION_SCHEMA}/seed"
        or value.get("status") != "COMPLETED_ATTRIBUTION_BLOCK"
        or value.get("seed") != seed
        or value.get("first_wave_arrival_ms") != arrival_ms
        or value.get("parent_policy_id") != contract.parent_policy_id
        or value.get("build_id") != contract.build_id
        or value.get("loadout_id") != contract.loadout_id
        or value.get("execution_mode") != execution_mode
        or not isinstance(task, Mapping)
        or task.get("schema") != SCHEMA
        or task.get("experiment_id") != contract.experiment_id
        or task.get("seed_index") != seed_index
        or task.get("execution_mode") != execution_mode
        or task.get("external_press_clock") != expected_clock
        or task.get("bridge_artifact_name") != contract.bridge_artifact_name
        or not isinstance(task.get("runtime_binding_artifact_name"), str)
        or not task.get("runtime_binding_artifact_name")
        or task.get("runtime_binding_id") != contract.runtime_binding_id
        or (
            expected_bridge_artifact_name is not None
            and task.get("bridge_artifact_name")
            != expected_bridge_artifact_name
        )
        or (
            expected_runtime_binding_artifact_name is not None
            and task.get("runtime_binding_artifact_name")
            != expected_runtime_binding_artifact_name
        )
    ):
        raise ValueError(f"attribution seed result identity differs at {seed_index}")
    return dict(value)


def summarize_v8_attribution_remote_v1(
    *,
    contract_path: str | Path,
    input_root: str | Path,
    execution_mode: str,
    output_path: str | Path,
) -> JSONMap:
    contract = load_v8_attribution_contract_v1(contract_path)
    if execution_mode not in contract.execution_modes:
        raise ValueError("execution mode is outside the frozen contract")
    root = Path(input_root).expanduser()
    rows = [
        _validate_seed_result(
            _read_json(
                root / seed_output_name_v1(execution_mode, seed_index),
                "attribution seed result",
            ),
            contract=contract,
            seed_index=seed_index,
            execution_mode=execution_mode,
        )
        for seed_index in range(contract.seed_count)
    ]
    runtime_identities = {
        (
            row["remote_task"]["bridge_artifact_name"],
            row["remote_task"]["runtime_binding_artifact_name"],
            row["remote_task"]["runtime_binding_id"],
        )
        for row in rows
    }
    if len(runtime_identities) != 1:
        raise ValueError("attribution seed results mix runtime identities")
    bridge_name, binding_name, binding_id = next(iter(runtime_identities))
    summary = summarize_v8_attribution_rows_v1(rows)
    summary.update(
        {
            "schema": f"{SCHEMA}/summary",
            "experiment_id": contract.experiment_id,
            "parent_policy_id": contract.parent_policy_id,
            "build_id": contract.build_id,
            "loadout_id": contract.loadout_id,
            "execution_mode": execution_mode,
            "external_press_clock": (
                {
                    "period_ms": contract.press_period_ms,
                    "phase_ms": contract.press_phase_ms,
                }
                if execution_mode == "E1_EXTERNAL_PRESS_CLOCK"
                else None
            ),
            "bridge_artifact_name": bridge_name,
            "runtime_binding_artifact_name": binding_name,
            "runtime_binding_id": binding_id,
            "fresh_seed_start": contract.seed_start,
            "fresh_seed_count": contract.seed_count,
            "arrival_schedule_ms": list(contract.arrival_schedule_ms),
        }
    )
    _atomic_create_json(output_path, summary)
    return summary


def _print_receipt(phase: str, output: Path, payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            {
                "schema": f"{SCHEMA}/stdout",
                "phase": phase,
                "status": payload.get("status", "COMPLETE"),
                "output": str(output),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    run = sub.add_parser("run")
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--policy-bundle", type=Path, required=True)
    run.add_argument("--seed-index", type=int, required=True)
    run.add_argument("--execution-mode", choices=EXECUTION_MODES, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--bridge", type=Path, required=True)
    run.add_argument("--bridge-cwd", type=Path, required=True)
    run.add_argument("--runtime-binding", type=Path, required=True)
    run.add_argument("--arm-workers", type=int, default=3)
    shard = sub.add_parser("run-shard")
    shard.add_argument("--contract", type=Path, required=True)
    shard.add_argument("--policy-bundle", type=Path, required=True)
    shard.add_argument("--shard-index", type=int, required=True)
    shard.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    shard.add_argument("--execution-mode", choices=EXECUTION_MODES, required=True)
    shard.add_argument("--output-dir", type=Path, required=True)
    shard.add_argument("--summary", type=Path, required=True)
    shard.add_argument("--bridge", type=Path, required=True)
    shard.add_argument("--bridge-cwd", type=Path, required=True)
    shard.add_argument("--runtime-binding", type=Path, required=True)
    shard.add_argument("--seed-workers", type=int, default=32)
    shard.add_argument("--arm-workers", type=int, default=3)
    summary = sub.add_parser("summarize")
    summary.add_argument("--contract", type=Path, required=True)
    summary.add_argument("--input-root", type=Path, required=True)
    summary.add_argument("--execution-mode", choices=EXECUTION_MODES, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "run":
        payload = run_v8_attribution_seed_remote_v1(
            contract_path=args.contract,
            policy_bundle_path=args.policy_bundle,
            seed_index=args.seed_index,
            execution_mode=args.execution_mode,
            output_path=args.output,
            bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
            arm_workers=args.arm_workers,
        )
    elif args.phase == "run-shard":
        payload = run_v8_attribution_shard_remote_v1(
            contract_path=args.contract,
            policy_bundle_path=args.policy_bundle,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            execution_mode=args.execution_mode,
            output_dir=args.output_dir,
            summary_path=args.summary,
            bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
            seed_workers=args.seed_workers,
            arm_workers=args.arm_workers,
        )
        args.output = args.summary
    else:
        payload = summarize_v8_attribution_remote_v1(
            contract_path=args.contract,
            input_root=args.input_root,
            execution_mode=args.execution_mode,
            output_path=args.output,
        )
    _print_receipt(args.phase, args.output, payload)


if __name__ == "__main__":
    main()


__all__ = (
    "SCHEMA",
    "assigned_seed_indices_v1",
    "run_v8_attribution_seed_remote_v1",
    "run_v8_attribution_shard_remote_v1",
    "seed_output_name_v1",
    "summarize_v8_attribution_remote_v1",
    "validate_v8_attribution_inputs_v1",
)
