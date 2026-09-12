"""Prepare one-seed Windows smoke plans for the repaired horizon run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_horizon_confirmation_v2 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_MASTER_SEEDS,
    build_horizon_confirmation_v2,
)
from .fury_cat2_horizon_windows_smoke_v1 import (
    FuryCat2HorizonWindowsSmokeV1Error,
    _single_seed_plan,
    _write_bundle,
)
from .fury_multiseed_hpc_dispatch_v3 import build_single_node_fixture_dispatch_v3


JSONMap = dict[str, Any]
SCHEMA = "fury_cat2_horizon_windows_smoke/v2"
STATUS = "PREPARED_REPAIRED_WINDOWS_SINGLE_PROCESS_SMOKE_NOT_EXECUTED"
DEFAULT_MASTER_SEED = CONFIRMATION_MASTER_SEEDS[0]


class FuryCat2HorizonWindowsSmokeV2Error(RuntimeError):
    """The repaired bounded Windows smoke could not be materialized."""


def build_windows_smoke_bundle_v2(
    template_runner_plan: Mapping[str, Any],
    *,
    windows_bridge_path: str | Path,
    bridge_build_id: str,
    master_seed: int = DEFAULT_MASTER_SEED,
    project_root: str | Path,
) -> JSONMap:
    if master_seed not in CONFIRMATION_MASTER_SEEDS:
        raise FuryCat2HorizonWindowsSmokeV2Error(
            "master seed must belong to the frozen 513--768 family"
        )
    try:
        prepared = build_horizon_confirmation_v2(
            template_runner_plan,
            bridge_path=windows_bridge_path,
            bridge_platform="windows-amd64",
            bridge_build_id=bridge_build_id,
            workers_per_node=1,
            project_root=project_root,
        )
    except Exception as error:
        raise FuryCat2HorizonWindowsSmokeV2Error(
            f"could not build repaired Windows horizon plans: {error}"
        ) from error
    arms: list[JSONMap] = []
    for expected_id, raw in zip(CONFIRMATION_ARM_IDS, prepared["arms"]):
        if raw["arm_spec"]["arm_id"] != expected_id:
            raise FuryCat2HorizonWindowsSmokeV2Error(
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-runner-plan", type=Path, required=True)
    parser.add_argument("--windows-bridge", type=Path, required=True)
    parser.add_argument("--bridge-build-id", required=True)
    parser.add_argument("--master-seed", type=int, default=DEFAULT_MASTER_SEED)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        template = json.loads(args.template_runner_plan.read_text(encoding="utf-8"))
        bundle = build_windows_smoke_bundle_v2(
            template,
            windows_bridge_path=args.windows_bridge,
            bridge_build_id=args.bridge_build_id,
            master_seed=args.master_seed,
            project_root=args.project_root,
        )
        _write_bundle(args.output_directory, bundle)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        FuryCat2HorizonWindowsSmokeV1Error,
        FuryCat2HorizonWindowsSmokeV2Error,
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
    "FuryCat2HorizonWindowsSmokeV2Error",
    "SCHEMA",
    "STATUS",
    "build_windows_smoke_bundle_v2",
)
