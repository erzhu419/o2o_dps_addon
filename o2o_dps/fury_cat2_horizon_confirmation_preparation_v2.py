"""Prepare immutable repaired Cat2 Fury horizon artifacts locally.

The source closure includes the v6.2 terminal-hardcast accounting repair, the
single-pass v4 reducer, and the dual-baseline v2 analysis.  Preparation is
local and content addressed; it performs no cluster copy and launches no
worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_horizon_confirmation_v2 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_MASTER_SEEDS,
    EXECUTION_ENTRYPOINTS_V2,
    build_horizon_confirmation_v2,
)
from .fury_cat2_screening_preparation_v1 import (
    EXPECTED_LINUX_BRIDGE_SHA256,
    FuryCat2ScreeningPreparationV1Error,
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
SCHEMA = "fury_cat2_horizon_confirmation_preparation/v2"
MANIFEST_SCHEMA = "fury_cat2_horizon_confirmation_manifest/v2"
RUN_LABEL = "cat2new-fury-horizon-confirmation-v2-right-censor-3x256-w40"
WORKERS_PER_NODE_PER_ARM = 40
CONCURRENT_ARMS = len(CONFIRMATION_ARM_IDS)
AGGREGATE_WORKERS_PER_NODE = WORKERS_PER_NODE_PER_ARM * CONCURRENT_ARMS
_SAFE_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


class FuryCat2HorizonConfirmationPreparationV2Error(RuntimeError):
    """A repaired confirmation input or immutable identity is invalid."""


def _confirmation_manifest_v2(
    bundle: Mapping[str, Any],
    *,
    source_closure_sha256: str,
    source_archive_sha256: str,
    run_label: str,
) -> JSONMap:
    arms = bundle.get("arms")
    if not isinstance(arms, list) or len(arms) != len(CONFIRMATION_ARM_IDS):
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            "confirmation builder did not return the frozen three arms"
        )
    analysis_contract = bundle.get("analysis_contract")
    analysis_contract_sha = bundle.get("analysis_contract_sha256")
    if (
        not isinstance(analysis_contract, Mapping)
        or analysis_contract_sha != sha256_json(analysis_contract)
    ):
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            "analysis contract content address is invalid"
        )

    rows: list[JSONMap] = []
    identity_rows: list[JSONMap] = []
    for expected_id, raw in zip(CONFIRMATION_ARM_IDS, arms):
        if not isinstance(raw, Mapping):
            raise FuryCat2HorizonConfirmationPreparationV2Error(
                f"confirmation arm {expected_id} is malformed"
            )
        spec = raw.get("arm_spec")
        runner = raw.get("runner_plan")
        dispatch = raw.get("dispatch_plan")
        if not all(isinstance(value, Mapping) for value in (spec, runner, dispatch)):
            raise FuryCat2HorizonConfirmationPreparationV2Error(
                f"confirmation arm {expected_id} lacks its plan"
            )
        if spec.get("arm_id") != expected_id:
            raise FuryCat2HorizonConfirmationPreparationV2Error(
                "confirmation arm order differs from the frozen registry"
            )
        checked_runner = validate_runner_plan(runner)
        checked_dispatch = validate_dispatch_plan_v3(dispatch, checked_runner)
        if checked_runner["contract"]["seed_derivation"]["master_seeds"] != list(
            CONFIRMATION_MASTER_SEEDS
        ):
            raise FuryCat2HorizonConfirmationPreparationV2Error(
                f"confirmation arm {expected_id} changed the fresh seed family"
            )
        if [node["workers"] for node in checked_dispatch["nodes"]] != [
            WORKERS_PER_NODE_PER_ARM
        ] * 6:
            raise FuryCat2HorizonConfirmationPreparationV2Error(
                f"confirmation arm {expected_id} changed the worker budget"
            )
        execution = raw.get("execution_bundle_identity")
        if not isinstance(execution, Mapping) or execution.get(
            "python_source_closure_sha256"
        ) != source_closure_sha256:
            raise FuryCat2HorizonConfirmationPreparationV2Error(
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
        "analysis_contract_sha256": analysis_contract_sha,
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
        "status": (
            "PREPARED_REPAIRED_SYNTHETIC_DUAL_BASELINE_DIAGNOSTIC_NOT_EXECUTED"
        ),
        "confirmation_id": confirmation_id,
        "run_root_name": f"{run_label}-{confirmation_id}",
        "input_lock": bundle["input_lock"],
        "analysis_contract": dict(analysis_contract),
        "analysis_contract_sha256": analysis_contract_sha,
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
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


def prepare_fury_cat2_horizon_confirmation_v2(
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
    """Build the repaired content-addressed source and run artifacts."""

    if not _SAFE_LABEL.fullmatch(run_label):
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            "run label must contain only lowercase letters, digits, and hyphens"
        )
    root = Path(project_root).expanduser().resolve()
    bridge = Path(linux_bridge_path).expanduser().resolve()
    if not bridge.is_file():
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            f"Linux bridge is missing: {bridge}"
        )
    if hashlib.sha256(bridge.read_bytes()).hexdigest() != expected_linux_bridge_sha256:
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            "Linux bridge differs from the expected v10 binary"
        )
    template = _load_json(Path(template_runner_plan_path), "template runner plan")
    source_identity = build_fury_execution_source_identity_v2(
        project_root=root,
        required_relative_paths=EXECUTION_ENTRYPOINTS_V2,
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
    try:
        bundle = build_horizon_confirmation_v2(
            template,
            bridge_path=bridge,
            bridge_platform="linux-amd64",
            bridge_build_id=bridge_build_id,
            workers_per_node=WORKERS_PER_NODE_PER_ARM,
            project_root=root,
            execution_entrypoints=EXECUTION_ENTRYPOINTS_V2,
        )
    except Exception as error:
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            f"repaired confirmation build failed: {error}"
        ) from error
    final_identity = build_fury_execution_source_identity_v2(
        project_root=root,
        required_relative_paths=EXECUTION_ENTRYPOINTS_V2,
    )
    if final_identity["canonical_bundle"]["sha256"] != source_sha:
        raise FuryCat2HorizonConfirmationPreparationV2Error(
            "execution source changed while confirmation artifacts were prepared"
        )
    manifest = _confirmation_manifest_v2(
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
            "existing repaired horizon confirmation manifest",
        )
        if existing_manifest != manifest:
            raise FuryCat2HorizonConfirmationPreparationV2Error(
                "existing confirmation manifest differs and will not be overwritten"
            )
        for arm in manifest["arms"]:
            existing_runner = validate_runner_plan(
                _load_json(run_root / arm["runner_plan_path"], "existing runner plan")
            )
            if existing_runner["plan_sha256"] != arm["runner_plan_sha256"]:
                raise FuryCat2HorizonConfirmationPreparationV2Error(
                    f"existing runner plan differs for {arm['arm_spec']['arm_id']}"
                )
            checked_dispatch = validate_dispatch_plan_v3(
                _load_json(
                    run_root / arm["dispatch_plan_path"],
                    "existing dispatch plan",
                ),
                existing_runner,
            )
            if sha256_json(checked_dispatch) != arm["dispatch_plan_sha256"]:
                raise FuryCat2HorizonConfirmationPreparationV2Error(
                    f"existing dispatch plan differs for {arm['arm_spec']['arm_id']}"
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
            "analysis_contract_sha256": manifest["analysis_contract_sha256"],
            "arm_count": manifest["arm_count"],
            "master_seed_count": len(CONFIRMATION_MASTER_SEEDS),
            "aggregate_workers_per_node": AGGREGATE_WORKERS_PER_NODE,
        },
        "remote_copy_performed": False,
        "execution_started": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
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
        receipt = prepare_fury_cat2_horizon_confirmation_v2(
            template_runner_plan_path=args.template_runner_plan,
            linux_bridge_path=args.linux_bridge,
            bridge_build_id=args.bridge_build_id,
            source_release_root=args.source_release_root,
            runs_root=args.runs_root,
            project_root=args.project_root,
            run_label=args.run_label,
        )
    except (
        OSError,
        ValueError,
        FuryCat2ScreeningPreparationV1Error,
        FuryCat2HorizonConfirmationPreparationV2Error,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


__all__: Sequence[str] = (
    "AGGREGATE_WORKERS_PER_NODE",
    "CONCURRENT_ARMS",
    "FuryCat2HorizonConfirmationPreparationV2Error",
    "MANIFEST_SCHEMA",
    "RUN_LABEL",
    "SCHEMA",
    "WORKERS_PER_NODE_PER_ARM",
    "prepare_fury_cat2_horizon_confirmation_v2",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
