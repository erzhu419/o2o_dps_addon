"""Prepare immutable source and eight-arm Fury screening artifacts locally.

The command in this module performs no remote copy and starts no worker.  It
materializes one content-addressed Python source release and one
content-addressed screening run directory, while refusing to overwrite a
different artifact already present at either address.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
from typing import Any, Mapping, Sequence

from .cat2new_fury_screening_v1 import (
    EXECUTION_ENTRYPOINTS,
    FROZEN_SCREENING_ARMS_V1,
    build_screening_plans_v1,
)
from .fury_execution_source_identity_v2 import (
    build_fury_execution_source_identity_v2,
)
from .fury_multiseed_hpc_dispatch_v3 import validate_dispatch_plan_v3
from .fury_paired_multiseed_runner_v4 import sha256_json, validate_runner_plan


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "fury_cat2_screening_preparation/v1"
SCREENING_IDENTITY_SCHEMA = "fury_cat2_screening_identity/v1"
WORKERS_PER_NODE_PER_ARM = 40
CONCURRENT_ARMS_PER_BATCH = 4
AGGREGATE_WORKERS_PER_NODE = 160
RUN_LABEL = "fury-cat2-screening-v1-v10-8x256-w40"
EXPECTED_LINUX_BRIDGE_SHA256 = (
    "fb95049a1def8e8873b344f2d109b1ed71e48d7433c35f136fa8ffdafb574c01"
)
_SAFE_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


class FuryCat2ScreeningPreparationV1Error(RuntimeError):
    """A local artifact would be ambiguous, mutable, or internally stale."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryCat2ScreeningPreparationV1Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryCat2ScreeningPreparationV1Error(f"{label} must be an object")
    return value


def _source_archive_bytes(project_root: Path, source_identity: Mapping[str, Any]) -> bytes:
    rows = source_identity.get("files")
    if not isinstance(rows, list) or not rows:
        raise FuryCat2ScreeningPreparationV1Error(
            "source identity contains no file closure"
        )
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                raise FuryCat2ScreeningPreparationV1Error(
                    f"source identity row {index} is not an object"
                )
            relative = str(raw.get("relative_path", ""))
            pure = PurePosixPath(relative)
            if (
                not relative
                or pure.is_absolute()
                or ".." in pure.parts
                or pure.as_posix() != relative
            ):
                raise FuryCat2ScreeningPreparationV1Error(
                    f"source identity row {index} has an unsafe path"
                )
            path = project_root.joinpath(*pure.parts)
            try:
                payload = path.read_bytes()
            except OSError as error:
                raise FuryCat2ScreeningPreparationV1Error(
                    f"could not package {relative}: {error}"
                ) from error
            digest = hashlib.sha256(payload).hexdigest()
            if len(payload) != raw.get("size_bytes") or digest != raw.get("sha256"):
                raise FuryCat2ScreeningPreparationV1Error(
                    f"source changed after identity capture: {relative}"
                )
            member = tarfile.TarInfo(relative)
            member.size = len(payload)
            member.mode = 0o644
            member.mtime = 0
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            archive.addfile(member, io.BytesIO(payload))
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=compressed, mtime=0
    ) as stream:
        stream.write(tar_buffer.getvalue())
    return compressed.getvalue()


def _write_new_directory(final: Path, files: Mapping[str, bytes]) -> str:
    """Create ``final`` atomically, or verify and reuse its required files."""

    if final.exists():
        if not final.is_dir():
            raise FuryCat2ScreeningPreparationV1Error(
                f"artifact address is not a directory: {final}"
            )
        for relative, expected in files.items():
            path = final / relative
            try:
                observed = path.read_bytes()
            except OSError as error:
                raise FuryCat2ScreeningPreparationV1Error(
                    f"existing artifact is incomplete at {path}: {error}"
                ) from error
            if observed != expected:
                raise FuryCat2ScreeningPreparationV1Error(
                    f"existing artifact differs and will not be overwritten: {path}"
                )
        return "REUSED_EXACT"

    final.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".incoming-{final.name[:16]}-", dir=final.parent)
    )
    try:
        for relative, payload in files.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        if final.exists():
            return _write_new_directory(final, files)
        stage.rename(final)
        return "CREATED"
    finally:
        if stage.exists() and stage.parent == final.parent:
            shutil.rmtree(stage)


def _screening_manifest(
    bundle: Mapping[str, Any],
    *,
    source_closure_sha256: str,
    source_archive_sha256: str,
    source_release_relative_path: str,
    run_label: str,
) -> JSONMap:
    arms = bundle.get("arms")
    if not isinstance(arms, list) or len(arms) != len(FROZEN_SCREENING_ARMS_V1):
        raise FuryCat2ScreeningPreparationV1Error(
            "screening builder did not return the frozen eight arms"
        )
    arm_rows: list[JSONMap] = []
    identity_arms: list[JSONMap] = []
    for index, raw in enumerate(arms):
        if not isinstance(raw, Mapping):
            raise FuryCat2ScreeningPreparationV1Error(
                f"screening arm {index} is not an object"
            )
        arm_spec = raw.get("arm_spec")
        runner = raw.get("runner_plan")
        dispatch = raw.get("dispatch_plan")
        if not all(isinstance(value, Mapping) for value in (arm_spec, runner, dispatch)):
            raise FuryCat2ScreeningPreparationV1Error(
                f"screening arm {index} lacks its spec, runner, or dispatch"
            )
        arm_id = str(arm_spec.get("arm_id", ""))
        expected_arm_id = FROZEN_SCREENING_ARMS_V1[index].arm_id
        if arm_id != expected_arm_id:
            raise FuryCat2ScreeningPreparationV1Error(
                "screening arm order differs from the frozen registry"
            )
        checked_runner = validate_runner_plan(runner)
        checked_dispatch = validate_dispatch_plan_v3(dispatch, checked_runner)
        node_workers = [int(row["workers"]) for row in checked_dispatch["nodes"]]
        if node_workers != [WORKERS_PER_NODE_PER_ARM] * 6:
            raise FuryCat2ScreeningPreparationV1Error(
                f"arm {arm_id} is not capped at 40 workers on every node"
            )
        dispatch_sha256 = sha256_json(checked_dispatch)
        identity_arms.append(
            {
                "arm_id": arm_id,
                "runner_plan_sha256": checked_runner["plan_sha256"],
                "dispatch_plan_sha256": dispatch_sha256,
            }
        )
        arm_rows.append(
            {
                "arm_spec": dict(arm_spec),
                "factory_config": dict(raw["factory_config"]),
                "candidate_policy_identity": dict(raw["candidate_policy_identity"]),
                "execution_bundle_identity": dict(raw["execution_bundle_identity"]),
                "runner_plan_sha256": checked_runner["plan_sha256"],
                "dispatch_plan_sha256": dispatch_sha256,
                "runner_plan_path": f"{arm_id}/runner-plan.json",
                "dispatch_plan_path": f"{arm_id}/dispatch-plan.json",
                "attempt_root_path": f"{arm_id}/attempts",
            }
        )
    batches = [
        [row.arm_id for row in FROZEN_SCREENING_ARMS_V1[offset : offset + 4]]
        for offset in range(0, len(FROZEN_SCREENING_ARMS_V1), 4)
    ]
    first_execution_bundle = arms[0]["execution_bundle_identity"]
    if any(row["execution_bundle_identity"] != first_execution_bundle for row in arms):
        raise FuryCat2ScreeningPreparationV1Error(
            "screening arms do not share one execution source closure"
        )
    observed_closure = first_execution_bundle.get("python_source_closure_sha256")
    if observed_closure != source_closure_sha256:
        raise FuryCat2ScreeningPreparationV1Error(
            "screening plans and source release were built from different closures"
        )
    identity = {
        "schema": SCREENING_IDENTITY_SCHEMA,
        "registry_sha256": bundle.get("registry_sha256"),
        "source_closure_sha256": source_closure_sha256,
        "source_archive_sha256": source_archive_sha256,
        "bridge_identity": dict(arms[0]["runner_plan"]["contract"]["bridge_identity"]),
        "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM,
        "concurrent_arm_batches": batches,
        "arms": identity_arms,
    }
    screening_id = sha256_json(identity)
    return {
        "schema": SCHEMA,
        "status": "PREPARED_DIAGNOSTIC_NOT_EXECUTED",
        "screening_id": screening_id,
        "run_root_name": f"{run_label}-{screening_id}",
        "registry_sha256": bundle.get("registry_sha256"),
        "source_closure_sha256": source_closure_sha256,
        "source_archive_sha256": source_archive_sha256,
        "source_release_relative_path": source_release_relative_path,
        "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM,
        "concurrent_arms_per_batch": CONCURRENT_ARMS_PER_BATCH,
        "aggregate_workers_per_node_per_batch": AGGREGATE_WORKERS_PER_NODE,
        "concurrent_arm_batches": batches,
        "arm_count": len(arm_rows),
        "arms": arm_rows,
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "comparison_ready": False,
        "scientific_result_available": False,
    }


def prepare_fury_cat2_screening_v1(
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
    """Build both local artifacts without publishing or starting execution."""

    if not _SAFE_LABEL.fullmatch(run_label):
        raise FuryCat2ScreeningPreparationV1Error(
            "run label must contain only lowercase letters, digits, and hyphens"
        )
    root = Path(project_root).expanduser().resolve()
    bridge = Path(linux_bridge_path).expanduser().resolve()
    if not bridge.is_file():
        raise FuryCat2ScreeningPreparationV1Error(
            f"Linux bridge is missing: {bridge}"
        )
    observed_bridge_sha256 = hashlib.sha256(bridge.read_bytes()).hexdigest()
    if observed_bridge_sha256 != expected_linux_bridge_sha256:
        raise FuryCat2ScreeningPreparationV1Error(
            "Linux bridge differs from the expected v10 binary"
        )
    template = _load_json(Path(template_runner_plan_path), "template runner plan")
    source_identity = build_fury_execution_source_identity_v2(
        project_root=root,
        required_relative_paths=EXECUTION_ENTRYPOINTS,
    )
    source_closure_sha256 = source_identity["canonical_bundle"]["sha256"]
    archive = _source_archive_bytes(root, source_identity)
    archive_sha256 = hashlib.sha256(archive).hexdigest()
    release_root = Path(source_release_root).expanduser().resolve()
    release = release_root / source_closure_sha256
    release_state = _write_new_directory(
        release,
        {
            "source-identity.json": _canonical_json_bytes(source_identity),
            "source-closure.tar.gz": archive,
        },
    )

    bundle = build_screening_plans_v1(
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
    if final_identity["canonical_bundle"]["sha256"] != source_closure_sha256:
        raise FuryCat2ScreeningPreparationV1Error(
            "execution source changed while screening artifacts were being prepared"
        )
    release_relative = f"releases/fury-multiseed-source/{source_closure_sha256}"
    manifest = _screening_manifest(
        bundle,
        source_closure_sha256=source_closure_sha256,
        source_archive_sha256=archive_sha256,
        source_release_relative_path=release_relative,
        run_label=run_label,
    )
    run_root = Path(runs_root).expanduser().resolve() / manifest["run_root_name"]
    run_files: dict[str, bytes] = {
        "screening-manifest.json": _canonical_json_bytes(manifest)
    }
    for row in bundle["arms"]:
        arm_id = row["arm_spec"]["arm_id"]
        run_files[f"{arm_id}/runner-plan.json"] = _canonical_json_bytes(
            row["runner_plan"]
        )
        run_files[f"{arm_id}/dispatch-plan.json"] = _canonical_json_bytes(
            row["dispatch_plan"]
        )
    if run_root.exists():
        # ``generated_at`` is deliberately outside the plan content address, so
        # an idempotent rerun compares validated identities rather than bytes.
        existing_manifest = _load_json(
            run_root / "screening-manifest.json", "existing screening manifest"
        )
        if existing_manifest != manifest:
            raise FuryCat2ScreeningPreparationV1Error(
                "existing screening manifest differs and will not be overwritten"
            )
        for arm in manifest["arms"]:
            existing_runner = validate_runner_plan(
                _load_json(run_root / arm["runner_plan_path"], "existing runner plan")
            )
            if existing_runner["plan_sha256"] != arm["runner_plan_sha256"]:
                raise FuryCat2ScreeningPreparationV1Error(
                    f"existing runner plan differs for {arm['arm_spec']['arm_id']}"
                )
            validate_dispatch_plan_v3(
                _load_json(run_root / arm["dispatch_plan_path"], "existing dispatch plan"),
                existing_runner,
            )
        run_state = "REUSED_EXACT_IDENTITIES"
    else:
        run_state = _write_new_directory(run_root, run_files)
    return {
        "schema": SCHEMA,
        "status": "PREPARED_LOCAL_ONLY_NO_REMOTE_MUTATION",
        "source_release": {
            "path": str(release),
            "state": release_state,
            "source_closure_sha256": source_closure_sha256,
            "archive_sha256": archive_sha256,
        },
        "screening_run": {
            "path": str(run_root),
            "state": run_state,
            "screening_id": manifest["screening_id"],
            "arm_count": manifest["arm_count"],
            "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM,
            "concurrent_arm_batches": manifest["concurrent_arm_batches"],
            "aggregate_workers_per_node_per_batch": AGGREGATE_WORKERS_PER_NODE,
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
    parser.add_argument(
        "--runs-root", type=Path, default=Path(".hpc-local/runs")
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--run-label", default=RUN_LABEL)
    parser.add_argument(
        "--expected-linux-bridge-sha256", default=EXPECTED_LINUX_BRIDGE_SHA256
    )
    args = parser.parse_args(argv)
    try:
        receipt = prepare_fury_cat2_screening_v1(
            template_runner_plan_path=args.template_runner_plan,
            linux_bridge_path=args.linux_bridge,
            bridge_build_id=args.bridge_build_id,
            source_release_root=args.source_release_root,
            runs_root=args.runs_root,
            project_root=args.project_root,
            run_label=args.run_label,
            expected_linux_bridge_sha256=args.expected_linux_bridge_sha256,
        )
    except (FuryCat2ScreeningPreparationV1Error, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(_canonical_json_bytes(receipt).decode("utf-8"), end="")
    return 0


__all__: Sequence[str] = (
    "AGGREGATE_WORKERS_PER_NODE",
    "CONCURRENT_ARMS_PER_BATCH",
    "EXPECTED_LINUX_BRIDGE_SHA256",
    "FuryCat2ScreeningPreparationV1Error",
    "RUN_LABEL",
    "SCHEMA",
    "WORKERS_PER_NODE_PER_ARM",
    "prepare_fury_cat2_screening_v1",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
