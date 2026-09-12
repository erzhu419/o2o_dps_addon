"""Prepare the immutable three-arm Cat2 Fury horizon confirmation locally.

This command packages the exact runner execution closure and materializes the
three 20.001-second, 256-seed development plans.  It performs no remote copy
and launches no worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_MASTER_SEEDS,
    build_horizon_confirmation_v1,
)
from .cat2new_fury_screening_v1 import EXECUTION_ENTRYPOINTS
from .fury_cat2_screening_preparation_v1 import (
    EXPECTED_LINUX_BRIDGE_SHA256,
    _canonical_json_bytes,
    _load_json,
    _source_archive_bytes,
    _write_new_directory,
)
from .fury_execution_source_identity_v2 import (
    build_fury_execution_source_identity_v2,
)
from .fury_multiseed_hpc_dispatch_v3 import validate_dispatch_plan_v3
from .fury_paired_multiseed_runner_v4 import sha256_json, validate_runner_plan


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "fury_cat2_horizon_confirmation_preparation/v1"
MANIFEST_SCHEMA = "fury_cat2_horizon_confirmation_manifest/v1"
RUN_LABEL = "cat2new-fury-horizon-confirmation-v1-v10-3x256-w40"
WORKERS_PER_NODE_PER_ARM = 40
CONCURRENT_ARMS = 3
AGGREGATE_WORKERS_PER_NODE = 120
_SAFE_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


class FuryCat2HorizonConfirmationPreparationV1Error(RuntimeError):
    """A confirmation input or immutable artifact identity is invalid."""


def _confirmation_manifest(
    bundle: Mapping[str, Any],
    *,
    source_closure_sha256: str,
    source_archive_sha256: str,
    run_label: str,
) -> JSONMap:
    arms = bundle.get("arms")
    if not isinstance(arms, list) or len(arms) != len(CONFIRMATION_ARM_IDS):
        raise FuryCat2HorizonConfirmationPreparationV1Error(
            "confirmation builder did not return the frozen three arms"
        )
    rows: list[JSONMap] = []
    identity_rows: list[JSONMap] = []
    for expected_id, raw in zip(CONFIRMATION_ARM_IDS, arms):
        if not isinstance(raw, Mapping):
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                f"confirmation arm {expected_id} is malformed"
            )
        spec = raw.get("arm_spec")
        runner = raw.get("runner_plan")
        dispatch = raw.get("dispatch_plan")
        if not all(isinstance(value, Mapping) for value in (spec, runner, dispatch)):
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                f"confirmation arm {expected_id} lacks its plan"
            )
        if spec.get("arm_id") != expected_id:
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                "confirmation arm order differs from the frozen registry"
            )
        checked_runner = validate_runner_plan(runner)
        checked_dispatch = validate_dispatch_plan_v3(dispatch, checked_runner)
        if checked_runner["contract"]["seed_derivation"]["master_seeds"] != list(
            CONFIRMATION_MASTER_SEEDS
        ):
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                f"confirmation arm {expected_id} changed the fresh seed family"
            )
        if [node["workers"] for node in checked_dispatch["nodes"]] != [
            WORKERS_PER_NODE_PER_ARM
        ] * 6:
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                f"confirmation arm {expected_id} changed the worker budget"
            )
        execution = raw.get("execution_bundle_identity")
        if not isinstance(execution, Mapping) or execution.get(
            "python_source_closure_sha256"
        ) != source_closure_sha256:
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                f"confirmation arm {expected_id} is not bound to the source release"
            )
        dispatch_sha = sha256_json(checked_dispatch)
        identity_rows.append(
            {
                "arm_id": expected_id,
                "runner_plan_sha256": checked_runner["plan_sha256"],
                "dispatch_plan_sha256": dispatch_sha,
            }
        )
        rows.append(
            {
                "arm_spec": dict(spec),
                "factory_config": dict(raw["factory_config"]),
                "candidate_policy_identity": dict(raw["candidate_policy_identity"]),
                "execution_bundle_identity": dict(execution),
                "runner_plan_sha256": checked_runner["plan_sha256"],
                "dispatch_plan_sha256": dispatch_sha,
                "runner_plan_path": f"{expected_id}/runner-plan.json",
                "dispatch_plan_path": f"{expected_id}/dispatch-plan.json",
                "attempt_root_path": f"{expected_id}/attempts",
            }
        )
    identity = {
        "schema": MANIFEST_SCHEMA,
        "input_lock_sha256": bundle["input_lock"]["content_address"]["sha256"],
        "source_closure_sha256": source_closure_sha256,
        "source_archive_sha256": source_archive_sha256,
        "bridge_identity": dict(arms[0]["runner_plan"]["contract"]["bridge_identity"]),
        "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM,
        "concurrent_arms": CONCURRENT_ARMS,
        "arms": identity_rows,
    }
    confirmation_id = sha256_json(identity)
    return {
        "schema": MANIFEST_SCHEMA,
        "status": "PREPARED_SYNTHETIC_MECHANISM_DIAGNOSTIC_NOT_EXECUTED",
        "confirmation_id": confirmation_id,
        "run_root_name": f"{run_label}-{confirmation_id}",
        "input_lock": bundle["input_lock"],
        "source_closure_sha256": source_closure_sha256,
        "source_archive_sha256": source_archive_sha256,
        "source_release_relative_path": (
            f"releases/fury-multiseed-source/{source_closure_sha256}"
        ),
        "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM,
        "concurrent_arms": CONCURRENT_ARMS,
        "aggregate_workers_per_node": AGGREGATE_WORKERS_PER_NODE,
        "arm_count": len(rows),
        "arms": rows,
        "analysis_contract": bundle["analysis_contract"],
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


def prepare_fury_cat2_horizon_confirmation_v1(
    *,
    template_runner_plan_path: str | Path,
    linux_bridge_path: str | Path,
    bridge_build_id: str,
    source_release_root: str | Path,
    runs_root: str | Path,
    project_root: str | Path = PROJECT_ROOT,
    run_label: str = RUN_LABEL,
    expected_linux_bridge_sha256: str = EXPECTED_LINUX_BRIDGE_SHA256,
) -> JSONMap:
    """Build content-addressed local source and confirmation artifacts."""

    if not _SAFE_LABEL.fullmatch(run_label):
        raise FuryCat2HorizonConfirmationPreparationV1Error(
            "run label must contain only lowercase letters, digits, and hyphens"
        )
    root = Path(project_root).expanduser().resolve()
    bridge = Path(linux_bridge_path).expanduser().resolve()
    if not bridge.is_file():
        raise FuryCat2HorizonConfirmationPreparationV1Error(
            f"Linux bridge is missing: {bridge}"
        )
    if hashlib.sha256(bridge.read_bytes()).hexdigest() != expected_linux_bridge_sha256:
        raise FuryCat2HorizonConfirmationPreparationV1Error(
            "Linux bridge differs from the expected v10 binary"
        )
    template = _load_json(Path(template_runner_plan_path), "template runner plan")
    source_identity = build_fury_execution_source_identity_v2(
        project_root=root,
        required_relative_paths=EXECUTION_ENTRYPOINTS,
    )
    source_sha = source_identity["canonical_bundle"]["sha256"]
    archive = _source_archive_bytes(root, source_identity)
    archive_sha = hashlib.sha256(archive).hexdigest()
    release = Path(source_release_root).expanduser().resolve() / source_sha
    release_state = _write_new_directory(
        release,
        {
            "source-identity.json": _canonical_json_bytes(source_identity),
            "source-closure.tar.gz": archive,
        },
    )
    bundle = build_horizon_confirmation_v1(
        template,
        bridge_path=bridge,
        bridge_platform="linux-amd64",
        bridge_build_id=bridge_build_id,
        workers_per_node=WORKERS_PER_NODE_PER_ARM,
        project_root=root,
    )
    final_identity = build_fury_execution_source_identity_v2(
        project_root=root,
        required_relative_paths=EXECUTION_ENTRYPOINTS,
    )
    if final_identity["canonical_bundle"]["sha256"] != source_sha:
        raise FuryCat2HorizonConfirmationPreparationV1Error(
            "execution source changed while confirmation artifacts were prepared"
        )
    manifest = _confirmation_manifest(
        bundle,
        source_closure_sha256=source_sha,
        source_archive_sha256=archive_sha,
        run_label=run_label,
    )
    run_root = Path(runs_root).expanduser().resolve() / manifest["run_root_name"]
    files: dict[str, bytes] = {
        "horizon-confirmation-manifest.json": _canonical_json_bytes(manifest)
    }
    for raw in bundle["arms"]:
        arm_id = raw["arm_spec"]["arm_id"]
        files[f"{arm_id}/runner-plan.json"] = _canonical_json_bytes(
            raw["runner_plan"]
        )
        files[f"{arm_id}/dispatch-plan.json"] = _canonical_json_bytes(
            raw["dispatch_plan"]
        )
    if run_root.exists():
        existing_manifest = _load_json(
            run_root / "horizon-confirmation-manifest.json",
            "existing horizon confirmation manifest",
        )
        if existing_manifest != manifest:
            raise FuryCat2HorizonConfirmationPreparationV1Error(
                "existing confirmation manifest differs and will not be overwritten"
            )
        for arm in manifest["arms"]:
            existing_runner = validate_runner_plan(
                _load_json(
                    run_root / arm["runner_plan_path"], "existing runner plan"
                )
            )
            if existing_runner["plan_sha256"] != arm["runner_plan_sha256"]:
                raise FuryCat2HorizonConfirmationPreparationV1Error(
                    f"existing runner plan differs for {arm['arm_spec']['arm_id']}"
                )
            validate_dispatch_plan_v3(
                _load_json(
                    run_root / arm["dispatch_plan_path"],
                    "existing dispatch plan",
                ),
                existing_runner,
            )
        run_state = "REUSED_EXACT_IDENTITIES"
    else:
        run_state = _write_new_directory(run_root, files)
    return {
        "schema": SCHEMA,
        "status": "PREPARED_LOCAL_ONLY_NO_REMOTE_MUTATION",
        "source_release": {
            "path": str(release),
            "state": release_state,
            "source_closure_sha256": source_sha,
            "archive_sha256": archive_sha,
        },
        "confirmation_run": {
            "path": str(run_root),
            "state": run_state,
            "confirmation_id": manifest["confirmation_id"],
            "arm_count": manifest["arm_count"],
            "aggregate_workers_per_node": AGGREGATE_WORKERS_PER_NODE,
        },
        "remote_copy_performed": False,
        "execution_started": False,
        "scientific_result_available": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-runner-plan", type=Path, required=True)
    parser.add_argument("--linux-bridge", type=Path, required=True)
    parser.add_argument("--bridge-build-id", required=True)
    parser.add_argument(
        "--source-release-root",
        type=Path,
        default=Path(".hpc-local/releases/fury-multiseed-source"),
    )
    parser.add_argument("--runs-root", type=Path, default=Path(".hpc-local/runs"))
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--run-label", default=RUN_LABEL)
    args = parser.parse_args(argv)
    try:
        receipt = prepare_fury_cat2_horizon_confirmation_v1(
            template_runner_plan_path=args.template_runner_plan,
            linux_bridge_path=args.linux_bridge,
            bridge_build_id=args.bridge_build_id,
            source_release_root=args.source_release_root,
            runs_root=args.runs_root,
            project_root=args.project_root,
            run_label=args.run_label,
        )
    except (OSError, ValueError, FuryCat2HorizonConfirmationPreparationV1Error) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


__all__: Sequence[str] = (
    "AGGREGATE_WORKERS_PER_NODE",
    "FuryCat2HorizonConfirmationPreparationV1Error",
    "RUN_LABEL",
    "SCHEMA",
    "WORKERS_PER_NODE_PER_ARM",
    "prepare_fury_cat2_horizon_confirmation_v1",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
