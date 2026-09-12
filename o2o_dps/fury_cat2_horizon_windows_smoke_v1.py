"""Prepare one-seed Windows real-bridge smoke plans for each horizon arm."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_ARM_IDS,
    build_horizon_confirmation_v1,
)
from .fury_multiseed_hpc_dispatch_v3 import (
    build_single_node_fixture_dispatch_v3,
)
from .fury_paired_multiseed_runner_v4 import build_runner_plan, validate_runner_plan


JSONMap = dict[str, Any]
SCHEMA = "fury_cat2_horizon_windows_smoke/v1"
STATUS = "PREPARED_WINDOWS_SINGLE_PROCESS_SMOKE_NOT_EXECUTED"
DEFAULT_MASTER_SEED = 257


class FuryCat2HorizonWindowsSmokeV1Error(RuntimeError):
    """The bounded Windows smoke contract could not be materialized."""


def _single_seed_plan(plan: Mapping[str, Any], master_seed: int) -> JSONMap:
    checked = validate_runner_plan(plan)
    contract = checked["contract"]
    return build_runner_plan(
        protocol_id=contract["protocol_id"],
        protocol_sha256=contract["protocol_sha256"],
        phase=contract["phase"],
        corpus_manifest_sha256=contract["corpus_manifest_sha256"],
        runner_inputs_sha256=contract["runner_inputs_sha256"],
        runner_scenario_bundle_sha256=contract["runner_scenario_bundle_sha256"],
        corpus_binding_sha256=contract["corpus_binding_sha256"],
        master_seeds=(master_seed,),
        scenarios=contract["scenarios"],
        policies=contract["policies"],
        shard_count=1,
        bridge_identity=contract["bridge_identity"],
        execution_bundle_identity=contract["execution_bundle_identity"],
        execution_mode=contract["execution_mode"],
        seed_namespace=contract["seed_derivation"]["namespace"],
        plan_intent=contract["plan_intent"],
        lane_contracts=contract["lane_contracts"],
    )


def build_windows_smoke_bundle_v1(
    template_runner_plan: Mapping[str, Any],
    *,
    windows_bridge_path: str | Path,
    bridge_build_id: str,
    master_seed: int = DEFAULT_MASTER_SEED,
    project_root: str | Path | None = None,
) -> JSONMap:
    if isinstance(master_seed, bool) or not isinstance(master_seed, int) or master_seed < 0:
        raise FuryCat2HorizonWindowsSmokeV1Error(
            "master seed must be a non-negative integer"
        )
    kwargs: JSONMap = {
        "bridge_path": windows_bridge_path,
        "bridge_platform": "windows-amd64",
        "bridge_build_id": bridge_build_id,
        "workers_per_node": 1,
    }
    if project_root is not None:
        kwargs["project_root"] = project_root
    try:
        prepared = build_horizon_confirmation_v1(template_runner_plan, **kwargs)
    except Exception as error:
        raise FuryCat2HorizonWindowsSmokeV1Error(
            f"could not build Windows horizon plans: {error}"
        ) from error
    arms: list[JSONMap] = []
    for expected_id, raw in zip(CONFIRMATION_ARM_IDS, prepared["arms"]):
        if raw["arm_spec"]["arm_id"] != expected_id:
            raise FuryCat2HorizonWindowsSmokeV1Error(
                "horizon arm order differs from the frozen registry"
            )
        plan = _single_seed_plan(raw["runner_plan"], master_seed)
        dispatch = build_single_node_fixture_dispatch_v3(plan, node_name="node001")
        arms.append(
            {
                "arm_id": expected_id,
                "factory_path": raw["arm_spec"]["factory_path"],
                "runner_plan": plan,
                "dispatch_plan": dispatch,
                "group_id": plan["contract"]["groups"][0]["group_id"],
            }
        )
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "master_seed": master_seed,
        "arm_count": len(arms),
        "arms": arms,
        "sequential_single_process_required": True,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


def _write_bundle(root: Path, bundle: Mapping[str, Any]) -> None:
    destination = root.expanduser().resolve()
    if destination.exists():
        raise FuryCat2HorizonWindowsSmokeV1Error(
            f"smoke directory already exists: {destination}"
        )
    destination.mkdir(parents=True)
    manifest = {key: value for key, value in bundle.items() if key != "arms"}
    manifest["arms"] = []
    for arm in bundle["arms"]:
        arm_root = destination / arm["arm_id"]
        arm_root.mkdir()
        (arm_root / "runner-plan.json").write_text(
            json.dumps(arm["runner_plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (arm_root / "dispatch-plan.json").write_text(
            json.dumps(arm["dispatch_plan"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest["arms"].append(
            {
                "arm_id": arm["arm_id"],
                "factory_path": arm["factory_path"],
                "group_id": arm["group_id"],
                "runner_plan_path": f"{arm['arm_id']}/runner-plan.json",
                "dispatch_plan_path": f"{arm['arm_id']}/dispatch-plan.json",
                "output_directory": f"{arm['arm_id']}/output",
            }
        )
    (destination / "smoke-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-runner-plan", type=Path, required=True)
    parser.add_argument("--windows-bridge", type=Path, required=True)
    parser.add_argument("--bridge-build-id", required=True)
    parser.add_argument("--master-seed", type=int, default=DEFAULT_MASTER_SEED)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        template = json.loads(args.template_runner_plan.read_text(encoding="utf-8"))
        bundle = build_windows_smoke_bundle_v1(
            template,
            windows_bridge_path=args.windows_bridge,
            bridge_build_id=args.bridge_build_id,
            master_seed=args.master_seed,
        )
        _write_bundle(args.output_directory, bundle)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        FuryCat2HorizonWindowsSmokeV1Error,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": STATUS,
                "output_directory": str(args.output_directory.resolve()),
                "arm_count": bundle["arm_count"],
                "master_seed": bundle["master_seed"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "DEFAULT_MASTER_SEED",
    "FuryCat2HorizonWindowsSmokeV1Error",
    "SCHEMA",
    "STATUS",
    "build_windows_smoke_bundle_v1",
)
