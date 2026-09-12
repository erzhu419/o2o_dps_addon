"""Auditable six-node orchestration for the frozen 3 x 256 Fury horizon run.

The local ``prepare`` action validates the already content-addressed source
and confirmation artifacts and creates a new, immutable attempt bundle.  The
mutating actions use the existing ``scheduler.run_on`` transport from
``configs/hpc/site.local.json``.  They deliberately separate:

* one node001 group per arm as the remote Linux smoke;
* the full six-node dispatch at 40 workers per arm per node; and
* strict remote reductions plus the compact three-arm analysis.

Every attempt has a caller-selected fresh ID.  No command resumes, replaces,
or removes a prior attempt.  Worker and reducer failures remain as marker
files and logs under that attempt.  The command does not launch anything
unless the relevant subcommand is passed ``--apply``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
from typing import Any, Mapping, Sequence

from .cat2new_fury_horizon_confirmation_v1 import (
    CONFIRMATION_ARM_IDS,
    CONFIRMATION_HORIZON_MS,
    CONFIRMATION_MASTER_SEEDS,
)
from .cat2new_fury_horizon_confirmation_v2 import (
    CONFIRMATION_ARM_IDS as CONFIRMATION_ARM_IDS_V2,
    CONFIRMATION_HORIZON_MS as CONFIRMATION_HORIZON_MS_V2,
    CONFIRMATION_MASTER_SEEDS as CONFIRMATION_MASTER_SEEDS_V2,
)
from .fury_cat2_horizon_confirmation_preparation_v1 import (
    MANIFEST_SCHEMA as MANIFEST_SCHEMA_V1,
    WORKERS_PER_NODE_PER_ARM,
)
from .fury_cat2_horizon_confirmation_preparation_v2 import (
    MANIFEST_SCHEMA as MANIFEST_SCHEMA_V2,
    WORKERS_PER_NODE_PER_ARM as WORKERS_PER_NODE_PER_ARM_V2,
)
from .fury_cat2_screening_preparation_v1 import EXPECTED_LINUX_BRIDGE_SHA256
from .fury_multiseed_hpc_dispatch_v2 import EXPECTED_NODES
from .fury_multiseed_hpc_dispatch_v3 import (
    node_worker_command_v3,
    validate_dispatch_plan_v3,
)
from .fury_paired_multiseed_runner_v4 import sha256_json, validate_runner_plan
from .hpc_environment_v1 import (
    DEFAULT_SITE_CONFIG,
    CommandResult,
    HpcSite,
    SchedulerNodeTransport,
    load_site_config,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "fury_cat2_horizon_remote_orchestration/v1"
PREPARE_RECEIPT_SCHEMA = "fury_cat2_horizon_remote_prepare_receipt/v1"
ACTION_RECEIPT_SCHEMA = "fury_cat2_horizon_remote_action_receipt/v1"
STATUS_SCHEMA = "fury_cat2_horizon_remote_status/v1"
MANIFEST_SCHEMA = MANIFEST_SCHEMA_V1

DEFAULT_SOURCE_RELEASE_ROOT = Path(".hpc-local/releases/fury-multiseed-source")
DEFAULT_ATTEMPTS_ROOT = Path(".hpc-local/horizon-remote-attempts")

# These are the already staged, immutable runtime contexts used by the current
# v10 screening run.  They are paths relative to site.node_shared_root.  CLI
# overrides remain available without weakening the content-addressed defaults.
DEFAULT_BRIDGE_RELATIVE = (
    "releases/dynamic-v5/"
    f"{EXPECTED_LINUX_BRIDGE_SHA256}/o2obridge.linux-amd64"
)
DEFAULT_CAT2NEW_RELATIVE = (
    "releases/cat2new-runtime/"
    "b58441a64c366cb19215c080086101e37bb177151e7bdacbbc21dcfac96ca700"
)
DEFAULT_CAT2_CONTEXT_RELATIVE = (
    "releases/cat2-runtime-context/"
    "1d03fbb0cae173db88e66cb61b18b692a0fdfd3c0212cc9924cc032cf8ab8e8b"
)
DEFAULT_CONTRA260817_RELATIVE = (
    "releases/contra260817-runtime/"
    "e7586fc95a77e5647fda7407b71ff587353c677a8e044819fef64e10da539fa2"
)
CAT2_CAPABILITY_RELATIVE = (
    "configs/experts/cat2_capabilities_2026_09_10_f7e659f9.json"
)

_SAFE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_CONFIRMATION_PROTOCOLS: dict[str, JSONMap] = {
    MANIFEST_SCHEMA_V1: {
        "manifest_schema": MANIFEST_SCHEMA_V1,
        "arm_ids": CONFIRMATION_ARM_IDS,
        "master_seeds": CONFIRMATION_MASTER_SEEDS,
        "horizon_ms": CONFIRMATION_HORIZON_MS,
        "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM,
        "reduction_mode": "V1_SEPARATE_REDUCERS_THEN_ANALYSIS",
        "strict_reducer": "o2o_dps.fury_multiseed_hpc_reducer_v3",
        "compact_analysis": "o2o_dps.cat2new_fury_horizon_analysis_v1",
        "identity_binds_analysis_contract": False,
    },
    MANIFEST_SCHEMA_V2: {
        "manifest_schema": MANIFEST_SCHEMA_V2,
        "arm_ids": CONFIRMATION_ARM_IDS_V2,
        "master_seeds": CONFIRMATION_MASTER_SEEDS_V2,
        "horizon_ms": CONFIRMATION_HORIZON_MS_V2,
        "workers_per_node_per_arm": WORKERS_PER_NODE_PER_ARM_V2,
        "reduction_mode": "V2_ANALYSIS_OWNS_SINGLE_PASS_REDUCTIONS",
        "strict_reducer": "o2o_dps.fury_multiseed_hpc_reducer_v4",
        "compact_analysis": "o2o_dps.cat2new_fury_horizon_analysis_v2",
        "identity_binds_analysis_contract": True,
    },
}


class FuryCat2HorizonRemoteOrchestratorV1Error(RuntimeError):
    """An immutable input, state transition, or remote action is invalid."""


def _manifest_protocol(manifest: Mapping[str, Any]) -> JSONMap:
    schema = manifest.get("schema")
    protocol = _CONFIRMATION_PROTOCOLS.get(str(schema))
    if protocol is None:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "horizon confirmation manifest schema is unsupported"
        )
    return dict(protocol)


def _plan_arm_ids(plan: Mapping[str, Any]) -> tuple[str, ...]:
    contract = plan.get("confirmation_contract")
    rows = contract.get("arm_ids") if isinstance(contract, Mapping) else None
    if isinstance(rows, list) and rows and all(isinstance(row, str) for row in rows):
        return tuple(rows)
    smoke = plan.get("smoke")
    arms = smoke.get("arms") if isinstance(smoke, Mapping) else None
    if isinstance(arms, list) and arms:
        result = tuple(
            row.get("arm_id") for row in arms if isinstance(row, Mapping)
        )
        if len(result) == len(arms) and all(isinstance(row, str) for row in result):
            return result
    return tuple(CONFIRMATION_ARM_IDS)


def _plan_seed_count(plan: Mapping[str, Any]) -> int:
    contract = plan.get("confirmation_contract")
    value = contract.get("master_seed_count") if isinstance(contract, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    full = plan.get("full")
    value = full.get("groups_per_arm") if isinstance(full, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return len(CONFIRMATION_MASTER_SEEDS)


def _plan_workers_per_arm(plan: Mapping[str, Any]) -> int:
    full = plan.get("full")
    value = (
        full.get("workers_per_arm_per_node") if isinstance(full, Mapping) else None
    )
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return WORKERS_PER_NODE_PER_ARM


def _canonical_bytes(value: Any) -> bytes:
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


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"could not read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(f"{label} must be an object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"could not hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"{label} must be a lowercase safe identifier"
        )
    return value


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"{label} must be a nonempty relative POSIX path"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"{label} must not be absolute or traverse directories"
        )
    if path.as_posix() != value or any(
        re.fullmatch(r"[A-Za-z0-9._-]+", part) is None for part in path.parts
    ):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"{label} contains an unsafe path segment"
        )
    return value


def _home(relative: str) -> str:
    return f'"$HOME/{_safe_relative(relative, "remote path")}"'


def _validate_site(site: HpcSite, *, required_workers_per_node: int = 120) -> None:
    if (
        site.node_transport_kind != "scheduler_run_on"
        or tuple(node.name for node in site.nodes) != EXPECTED_NODES
        or tuple(node.transport_name for node in site.nodes) != EXPECTED_NODES
    ):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "orchestration requires scheduler_run_on on node001--node006"
        )
    if site.maximum_workers_per_node_before_benchmark < required_workers_per_node:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "site maximum worker budget is below the frozen aggregate 120 per node"
        )


def _load_bound_site(plan: Mapping[str, Any], site_config: str | Path) -> HpcSite:
    path = Path(site_config).expanduser().resolve()
    site = load_site_config(path)
    full = plan.get("full")
    required = (
        full.get("aggregate_workers_per_node")
        if isinstance(full, Mapping)
        else 120
    )
    if not isinstance(required, int) or isinstance(required, bool) or required <= 0:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "attempt worker budget is invalid"
        )
    _validate_site(site, required_workers_per_node=required)
    if _file_sha256(path) != plan["site"]["config_sha256"]:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "site config changed after attempt preparation"
        )
    return site


def _deterministic_archive(files: Mapping[str, bytes]) -> bytes:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for relative in sorted(files):
            _safe_relative(relative, "archive member")
            payload = files[relative]
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as stream:
        stream.write(tar_buffer.getvalue())
    return output.getvalue()


def _write_new_directory(destination: Path, files: Mapping[str, bytes]) -> None:
    target = destination.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"attempt already exists and will not be overwritten: {target}"
        )
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for relative, payload in files.items():
            member = temporary.joinpath(*PurePosixPath(_safe_relative(relative, "file")).parts)
            member.parent.mkdir(parents=True, exist_ok=True)
            member.write_bytes(payload)
        try:
            temporary.rename(target)
        except FileExistsError as error:
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                f"attempt was created concurrently: {target}"
            ) from error
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _write_once(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(value)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as error:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"action receipt already exists; use a new attempt: {destination}"
        ) from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _validated_inputs(
    run_root: Path,
    source_release_root: Path,
) -> tuple[JSONMap, list[JSONMap], Path, dict[str, bytes]]:
    root = run_root.expanduser().resolve()
    manifest_path = root / "horizon-confirmation-manifest.json"
    manifest = _read_json(manifest_path, "horizon confirmation manifest")
    protocol = _manifest_protocol(manifest)
    arm_ids = tuple(protocol["arm_ids"])
    master_seeds = tuple(protocol["master_seeds"])
    workers_per_arm = int(protocol["workers_per_node_per_arm"])
    if (
        manifest.get("arm_count") != len(arm_ids)
        or manifest.get("workers_per_node_per_arm") != workers_per_arm
        or manifest.get("aggregate_workers_per_node")
        != len(arm_ids) * workers_per_arm
        or manifest.get("input_lock", {}).get("confirmation_horizon_ms")
        != protocol["horizon_ms"]
        or manifest.get("input_lock", {}).get("master_seeds")
        != list(master_seeds)
    ):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "manifest is not the frozen 3 x 256, 20.001-second confirmation"
        )
    if protocol["identity_binds_analysis_contract"]:
        analysis_contract = manifest.get("analysis_contract")
        if (
            not isinstance(analysis_contract, Mapping)
            or manifest.get("analysis_contract_sha256")
            != sha256_json(analysis_contract)
            or manifest.get("input_lock", {}).get("analysis_contract")
            != analysis_contract
        ):
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                "v2 manifest analysis contract is not exactly bound"
            )
    confirmation_id = manifest.get("confirmation_id")
    source_sha = manifest.get("source_closure_sha256")
    source_archive_sha = manifest.get("source_archive_sha256")
    if not all(
        isinstance(value, str) and _SHA256.fullmatch(value)
        for value in (confirmation_id, source_sha, source_archive_sha)
    ):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "manifest content addresses are invalid"
        )
    if manifest.get("source_release_relative_path") != (
        f"releases/fury-multiseed-source/{source_sha}"
    ):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "manifest source release path is not its content address"
        )
    expected_run_name = str(manifest.get("run_root_name"))
    _safe_id(expected_run_name, "run_root_name")
    if root.name != expected_run_name:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "run directory name differs from the manifest content address"
        )
    release = source_release_root.expanduser().resolve() / source_sha
    archive_path = release / "source-closure.tar.gz"
    identity_path = release / "source-identity.json"
    if not archive_path.is_file() or not identity_path.is_file():
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "content-addressed local source release is incomplete"
        )
    if _file_sha256(archive_path) != source_archive_sha:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "source archive hash differs from the confirmation manifest"
        )
    source_identity = _read_json(identity_path, "source identity")
    if source_identity.get("canonical_bundle", {}).get("sha256") != source_sha:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "source identity differs from the confirmation manifest"
        )

    raw_arms = manifest.get("arms")
    if not isinstance(raw_arms, list) or [
        row.get("arm_spec", {}).get("arm_id")
        for row in raw_arms
        if isinstance(row, Mapping)
    ] != list(arm_ids):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "manifest arm registry or order changed"
        )
    files = {"horizon-confirmation-manifest.json": manifest_path.read_bytes()}
    arms: list[JSONMap] = []
    all_groups_by_arm: dict[str, set[str]] = {}
    bridge_identities: list[JSONMap] = []
    for raw in raw_arms:
        arm_id = _safe_id(raw["arm_spec"]["arm_id"], "arm_id")
        runner_relative = _safe_relative(raw["runner_plan_path"], "runner plan path")
        dispatch_relative = _safe_relative(
            raw["dispatch_plan_path"], "dispatch plan path"
        )
        runner_path = root.joinpath(*PurePosixPath(runner_relative).parts)
        dispatch_path = root.joinpath(*PurePosixPath(dispatch_relative).parts)
        runner = validate_runner_plan(_read_json(runner_path, f"{arm_id} runner plan"))
        dispatch = validate_dispatch_plan_v3(
            _read_json(dispatch_path, f"{arm_id} dispatch plan"), runner
        )
        if (
            runner["plan_sha256"] != raw.get("runner_plan_sha256")
            or sha256_json(dispatch) != raw.get("dispatch_plan_sha256")
            or runner["contract"]["group_count"] != len(master_seeds)
            or runner["contract"]["seed_derivation"]["master_seeds"]
            != list(master_seeds)
            or [node["name"] for node in dispatch["nodes"]] != list(EXPECTED_NODES)
            or [node["workers"] for node in dispatch["nodes"]]
            != [workers_per_arm] * len(EXPECTED_NODES)
            or runner["contract"]["bridge_identity"].get("sha256")
            != EXPECTED_LINUX_BRIDGE_SHA256
        ):
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                f"{arm_id} runner/dispatch differs from the frozen confirmation"
            )
        group_ids = [
            group_id
            for node in dispatch["nodes"]
            for group_id in node["group_ids"]
        ]
        if len(group_ids) != len(master_seeds) or len(set(group_ids)) != len(
            master_seeds
        ):
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                f"{arm_id} dispatch does not assign the frozen unique group family"
            )
        all_groups_by_arm[arm_id] = set(group_ids)
        bridge_identities.append(dict(runner["contract"]["bridge_identity"]))
        source_binding = raw.get("execution_bundle_identity", {}).get(
            "python_source_closure_sha256"
        )
        if source_binding != source_sha:
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                f"{arm_id} is not bound to the staged source closure"
            )
        factory = raw.get("arm_spec", {}).get("factory_path")
        if not isinstance(factory, str) or not re.fullmatch(
            r"o2o_dps\.[A-Za-z0-9_.]+:[A-Za-z0-9_]+", factory
        ):
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                f"{arm_id} factory path is invalid"
            )
        files[runner_relative] = runner_path.read_bytes()
        files[dispatch_relative] = dispatch_path.read_bytes()
        arms.append(
            {
                "arm_id": arm_id,
                "factory_path": factory,
                "runner_relative": runner_relative,
                "dispatch_relative": dispatch_relative,
                "runner": runner,
                "dispatch": dispatch,
                "smoke_group_id": dispatch["nodes"][0]["group_ids"][0],
            }
        )
    # Group IDs need not match between arms, but every arm must be a complete
    # 256-group paired family.  Keeping sets separate prevents cross-arm output
    # directories from being accidentally treated as interchangeable.
    if set(all_groups_by_arm) != set(arm_ids):
        raise FuryCat2HorizonRemoteOrchestratorV1Error("arm group registry differs")
    if any(value != bridge_identities[0] for value in bridge_identities[1:]):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "confirmation arms disagree on the bridge identity"
        )
    confirmation_identity = {
        "schema": manifest["schema"],
        "input_lock_sha256": manifest["input_lock"]["content_address"]["sha256"],
        "source_closure_sha256": source_sha,
        "source_archive_sha256": source_archive_sha,
        "bridge_identity": bridge_identities[0],
        "workers_per_node_per_arm": workers_per_arm,
        "concurrent_arms": len(arm_ids),
        "arms": [
            {
                "arm_id": raw["arm_spec"]["arm_id"],
                "runner_plan_sha256": raw["runner_plan_sha256"],
                "dispatch_plan_sha256": raw["dispatch_plan_sha256"],
            }
            for raw in raw_arms
        ],
    }
    if protocol["identity_binds_analysis_contract"]:
        confirmation_identity["analysis_contract_sha256"] = manifest[
            "analysis_contract_sha256"
        ]
    if sha256_json(confirmation_identity) != confirmation_id:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "confirmation_id differs from the immutable run identity"
        )
    return manifest, arms, archive_path, files


def _runtime_paths(shared_root: str, runtime: Mapping[str, str]) -> JSONMap:
    bridge = f"{shared_root}/{runtime['bridge_relative']}"
    cat2new = f"{shared_root}/{runtime['cat2new_relative']}"
    cat2 = f"{shared_root}/{runtime['cat2_context_relative']}"
    contra = f"{shared_root}/{runtime['contra260817_relative']}"
    return {
        "bridge": bridge,
        "bridge_cwd": str(PurePosixPath(bridge).parent),
        "cat2new_root": f"{cat2new}/Cat2_new",
        "cat2_capability_manifest": f"{cat2new}/{CAT2_CAPABILITY_RELATIVE}",
        "cat2_installed_root": f"{cat2}/Cat2",
        "cat2_savedvariables": f"{cat2}/Cat2.lua",
        "contra260817_root": f"{contra}/Contra_new",
        "contra260817_manifest": f"{contra}/manifest.json",
    }


def _environment_lines(
    plan: Mapping[str, Any], *, failure_state_relative: str | None = None
) -> list[str]:
    remote = plan["remote"]
    runtime = plan["runtime_paths"]
    if failure_state_relative is None:
        change_directory = f"cd {_home(remote['source_release'])} || exit 90"
    else:
        failure_state = _home(failure_state_relative)
        change_directory = (
            f"if ! cd {_home(remote['source_release'])}; then "
            f"mkdir -p {failure_state}; printf '%s\\n' SOURCE_CWD_FAILED > "
            f"{failure_state}/failure-reason; printf '%s\\n' 90 > "
            f"{failure_state}/exit-code; : > {failure_state}/failed; exit 90; fi"
        )
    return [
        change_directory,
        f"export PYTHONPATH={_home(remote['source_release'])}",
        f"export BOC_CAT2NEW_ROOT={_home(runtime['cat2new_root'])}",
        "export BOC_CAT2_CAPABILITY_MANIFEST="
        + _home(runtime["cat2_capability_manifest"]),
        f"export BOC_CAT2_INSTALLED_ROOT={_home(runtime['cat2_installed_root'])}",
        f"export BOC_CAT2_SAVEDVARIABLES={_home(runtime['cat2_savedvariables'])}",
        f"export BOC_CONTRA260817_ROOT={_home(runtime['contra260817_root'])}",
        f"export BOC_CONTRA260817_MANIFEST={_home(runtime['contra260817_manifest'])}",
        "export GOMAXPROCS=1",
    ]


def _worker_command(
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    *,
    node_name: str,
    output_relative: str,
) -> str:
    return node_worker_command_v3(
        arm["dispatch"],
        node_name,
        runner_plan_path=(
            f"$HOME/{plan['remote']['run_release']}/{arm['runner_relative']}"
        ),
        dispatch_plan_path=(
            f"$HOME/{plan['remote']['run_release']}/{arm['dispatch_relative']}"
        ),
        bridge_path=f"$HOME/{plan['runtime_paths']['bridge']}",
        bridge_cwd=f"$HOME/{plan['runtime_paths']['bridge_cwd']}",
        output_directory=f"$HOME/{output_relative}",
        cat2_policy_factory=arm["factory_path"],
        python_executable=f"$HOME/{plan['node_python']}",
    )


def _single_group_worker_command(
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    output_relative: str,
) -> str:
    values = (
        f"$HOME/{plan['node_python']}",
        "-B",
        "-m",
        "o2o_dps.fury_multiseed_hpc_worker_v3",
        "--runner-plan",
        f"$HOME/{plan['remote']['run_release']}/{arm['runner_relative']}",
        "--dispatch-plan",
        f"$HOME/{plan['remote']['run_release']}/{arm['dispatch_relative']}",
        "--node",
        "node001",
        "--group-id",
        arm["smoke_group_id"],
        "--bridge",
        f"$HOME/{plan['runtime_paths']['bridge']}",
        "--bridge-cwd",
        f"$HOME/{plan['runtime_paths']['bridge_cwd']}",
        "--output-directory",
        f"$HOME/{output_relative}",
        "--cat2-policy-factory",
        arm["factory_path"],
    )

    def quote(value: str) -> str:
        if value.startswith("$HOME/"):
            return '"$HOME"/' + shlex.quote(value[6:])
        return shlex.quote(value)

    return " ".join(quote(value) for value in values)


def _tracked_job_block(
    *,
    label: str,
    state_relative: str,
    log_relative: str,
    command: str,
    success_checks: Sequence[str],
) -> tuple[list[str], str]:
    variable = "p_" + re.sub(r"[^A-Za-z0-9_]", "_", label)
    state = _home(state_relative)
    log = _home(log_relative)
    checks = " && ".join(success_checks)
    inner = "; ".join(
        (
            "set +e",
            f"sh -c {shlex.quote(command)}",
            "rc=$?",
            f"if test \"$rc\" -eq 0; then {checks}; rc=$?; fi" if checks else ":",
            f"printf '%s\\n' \"$rc\" > {state}/exit-code",
            f"if test \"$rc\" -eq 0; then : > {state}/complete; "
            f"else printf '%s\\n' WORKER_OR_VALIDATION_FAILED > {state}/failure-reason; "
            f": > {state}/failed; fi",
            "exit \"$rc\"",
        )
    )
    return (
        [
            f"mkdir -p {state}",
            f": > {state}/running",
            f"( {inner} ) > {log} 2>&1 &",
            f"{variable}=$!",
        ],
        variable,
    )


def _supervisor_epilogue(pids: Sequence[tuple[str, str]], overall: str) -> list[str]:
    lines = ["status=0"]
    for label, variable in pids:
        lines.extend(
            (
                f"if wait \"${variable}\"; then rc=0; else rc=$?; status=1; fi",
                f"printf 'unit=%s rc=%s\\n' {shlex.quote(label)} \"$rc\"",
            )
        )
    state = _home(overall)
    lines.extend(
        (
            f"printf '%s\\n' \"$status\" > {state}/exit-code",
            f"if test \"$status\" -eq 0; then : > {state}/complete; "
            f"else printf '%s\\n' ONE_OR_MORE_UNITS_FAILED > {state}/failure-reason; "
            f": > {state}/failed; fi",
            "exit \"$status\"",
        )
    )
    return lines


def _smoke_script(plan: Mapping[str, Any], arms: Sequence[Mapping[str, Any]]) -> str:
    attempt = plan["remote"]["attempt_root"]
    overall = f"{attempt}/state/smoke"
    lines = [
        "#!/bin/sh",
        "set -u",
        "umask 077",
        *_environment_lines(plan, failure_state_relative=overall),
    ]
    pids: list[tuple[str, str]] = []
    for arm in arms:
        arm_id = arm["arm_id"]
        output = f"{attempt}/smoke/{arm_id}/output"
        state = f"{overall}/arms/{arm_id}"
        log = f"{attempt}/logs/smoke-{arm_id}.log"
        group = arm["smoke_group_id"]
        block, variable = _tracked_job_block(
            label=arm_id,
            state_relative=state,
            log_relative=log,
            command=_single_group_worker_command(plan, arm, output),
            success_checks=(
                f"test -s {_home(f'{output}/receipts/{group}.json')}",
                f"test -s {_home(f'{output}/groups/{group}.jsonl')}",
            ),
        )
        lines.extend(block)
        pids.append((arm_id, variable))
    lines.extend(_supervisor_epilogue(pids, overall))
    return "\n".join(lines) + "\n"


def _node_script(
    plan: Mapping[str, Any],
    node_name: str,
    arms: Sequence[Mapping[str, Any]],
) -> str:
    attempt = plan["remote"]["attempt_root"]
    overall = f"{attempt}/state/full/nodes/{node_name}"
    lines = [
        "#!/bin/sh",
        "set -u",
        "umask 077",
        *_environment_lines(plan, failure_state_relative=overall),
    ]
    pids: list[tuple[str, str]] = []
    for arm in arms:
        arm_id = arm["arm_id"]
        output = f"{attempt}/full/{arm_id}/output"
        state = f"{attempt}/state/full/arms/{arm_id}/nodes/{node_name}"
        log = f"{attempt}/logs/full-{arm_id}-{node_name}.log"
        groups = next(
            row["group_ids"] for row in arm["dispatch"]["nodes"] if row["name"] == node_name
        )
        checks = []
        for group in groups:
            checks.extend(
                (
                    f"test -s {_home(f'{output}/receipts/{group}.json')}",
                    f"test -s {_home(f'{output}/groups/{group}.jsonl')}",
                )
            )
        block, variable = _tracked_job_block(
            label=arm_id,
            state_relative=state,
            log_relative=log,
            command=_worker_command(
                plan, arm, node_name=node_name, output_relative=output
            ),
            success_checks=checks,
        )
        lines.extend(block)
        pids.append((arm_id, variable))
    lines.extend(_supervisor_epilogue(pids, overall))
    return "\n".join(lines) + "\n"


def _reduce_script(plan: Mapping[str, Any], arms: Sequence[Mapping[str, Any]]) -> str:
    attempt = plan["remote"]["attempt_root"]
    run = plan["remote"]["run_release"]
    state = f"{attempt}/state/reduce"
    analysis = f"{attempt}/analysis"
    python = _home(plan["node_python"])
    commands: list[str] = [
        "#!/bin/sh",
        "set -u",
        "umask 077",
        *_environment_lines(plan, failure_state_relative=state),
        "status=0",
        "(",
        "set -eu",
    ]
    for node in EXPECTED_NODES:
        commands.append(
            f"test -f {_home(f'{attempt}/state/full/nodes/{node}/complete')}"
        )
        commands.append(
            f"test ! -f {_home(f'{attempt}/state/full/nodes/{node}/failed')}"
        )
    reduction = plan["reduction"]
    mode = reduction["mode"]
    if mode == "V1_SEPARATE_REDUCERS_THEN_ANALYSIS":
        commands.append(f"mkdir -p {_home(f'{analysis}/reductions')}")
        for arm in arms:
            arm_id = arm["arm_id"]
            commands.append(
                " ".join(
                    (
                        python,
                        f"-B -m {reduction['strict_reducer']}",
                        "--runner-plan",
                        _home(f"{run}/{arm['runner_relative']}"),
                        "--dispatch-plan",
                        _home(f"{run}/{arm['dispatch_relative']}"),
                        "--output-directory",
                        _home(f"{attempt}/full/{arm_id}/output"),
                        "--output",
                        _home(f"{analysis}/reductions/{arm_id}.json"),
                    )
                )
            )
    elif mode != "V2_ANALYSIS_OWNS_SINGLE_PASS_REDUCTIONS":
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "attempt reduction mode is unsupported"
        )
    analysis_parts = [
        python,
        f"-B -m {reduction['compact_analysis']}",
        "--manifest",
        _home(f"{run}/horizon-confirmation-manifest.json"),
        "--run-root",
        _home(run),
    ]
    for arm in arms:
        analysis_parts.extend(
            (
                "--arm-output",
                (
                    f'"{arm["arm_id"]}=$HOME/{attempt}/full/'
                    f'{arm["arm_id"]}/output"'
                ),
            )
        )
    analysis_parts.extend(("--output", _home(f"{analysis}/horizon-analysis.json")))
    if mode == "V2_ANALYSIS_OWNS_SINGLE_PASS_REDUCTIONS":
        analysis_parts.extend(
            (
                "--compact-output",
                _home(f"{analysis}/compact-analysis.json"),
            )
        )
        commands.append(" ".join(analysis_parts))
    else:
        commands.append(" ".join(analysis_parts))
        compact_code = (
            "import json,sys; p=json.load(open(sys.argv[1],encoding='utf-8')); "
            "p.pop('paired_trace',None); "
            "open(sys.argv[2],'w',encoding='utf-8').write(json.dumps(p,ensure_ascii=False,"
            "sort_keys=True,separators=(',',':'),allow_nan=False)+'\\n')"
        )
        commands.append(
            " ".join(
                (
                    python,
                    "-c",
                    shlex.quote(compact_code),
                    _home(f"{analysis}/horizon-analysis.json"),
                    _home(f"{analysis}/compact-analysis.json"),
                )
            )
        )
    commands.extend(
        (
            ") > " + _home(f"{attempt}/logs/reduce.log") + " 2>&1",
            "rc=$?",
            f"printf '%s\\n' \"$rc\" > {_home(state)}/exit-code",
            f"if test \"$rc\" -eq 0; then : > {_home(state)}/complete; "
            f"else printf '%s\\n' REDUCER_OR_ANALYSIS_FAILED > {_home(state)}/failure-reason; "
            f": > {_home(state)}/failed; fi",
            "exit \"$rc\"",
        )
    )
    return "\n".join(commands) + "\n"


def prepare_remote_attempt_v1(
    *,
    run_root: str | Path,
    source_release_root: str | Path,
    attempt_id: str,
    attempts_root: str | Path = DEFAULT_ATTEMPTS_ROOT,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    bridge_relative: str = DEFAULT_BRIDGE_RELATIVE,
    cat2new_relative: str = DEFAULT_CAT2NEW_RELATIVE,
    cat2_context_relative: str = DEFAULT_CAT2_CONTEXT_RELATIVE,
    contra260817_relative: str = DEFAULT_CONTRA260817_RELATIVE,
) -> JSONMap:
    """Create a fresh local attempt bundle without contacting the cluster."""

    attempt_id = _safe_id(attempt_id, "attempt_id")
    site_path = Path(site_config).expanduser().resolve()
    site = load_site_config(site_path)
    _validate_site(site)
    runtime = {
        "bridge_relative": _safe_relative(bridge_relative, "bridge relative path"),
        "cat2new_relative": _safe_relative(cat2new_relative, "Cat2_new relative path"),
        "cat2_context_relative": _safe_relative(
            cat2_context_relative, "Cat2 context relative path"
        ),
        "contra260817_relative": _safe_relative(
            contra260817_relative, "Contra260817 relative path"
        ),
    }
    manifest, arms, source_archive, run_files = _validated_inputs(
        Path(run_root), Path(source_release_root)
    )
    protocol = _manifest_protocol(manifest)
    arm_ids = tuple(protocol["arm_ids"])
    master_seed_count = len(protocol["master_seeds"])
    workers_per_arm = int(protocol["workers_per_node_per_arm"])
    run_archive = _deterministic_archive(run_files)
    run_archive_sha = hashlib.sha256(run_archive).hexdigest()
    confirmation_id = manifest["confirmation_id"]
    run_name = manifest["run_root_name"]
    shared = site.node_shared_root
    source_release = f"{shared}/{manifest['source_release_relative_path']}"
    # The analysis validates the manifest-declared run_root_name.  Keep that
    # basename on the remote release as well; a directory named only by the
    # confirmation digest breaks the otherwise identical local/remote input
    # contract before reduction reads any worker result.
    run_release = f"{shared}/releases/fury-horizon-confirmation/{run_name}"
    attempt_remote = f"{shared}/runs/{run_name}/attempts/{attempt_id}"
    plan_core: JSONMap = {
        "schema": SCHEMA,
        "status": "PREPARED",
        "phase": "LOCAL_PREPARED_NOT_STAGED",
        "attempt_id": attempt_id,
        "confirmation_id": confirmation_id,
        "confirmation_contract": {
            "manifest_schema": manifest["schema"],
            "arm_ids": list(arm_ids),
            "master_seed_count": master_seed_count,
            "horizon_ms": protocol["horizon_ms"],
            "analysis_contract_sha256": manifest.get(
                "analysis_contract_sha256"
            ),
        },
        "run_root_name": run_name,
        "source_closure_sha256": manifest["source_closure_sha256"],
        "source_archive_sha256": manifest["source_archive_sha256"],
        "run_archive_sha256": run_archive_sha,
        "site": {
            "config_sha256": _file_sha256(site_path),
            "control_plane_alias": site.control_ssh_alias,
            "node_transport": site.node_transport_kind,
            "nodes": list(EXPECTED_NODES),
        },
        "node_python": site.node_python,
        "remote": {
            "shared_root": shared,
            "source_release": source_release,
            "run_release": run_release,
            "attempt_root": attempt_remote,
        },
        "runtime_release_relative_paths": runtime,
        "runtime_paths": _runtime_paths(shared, runtime),
        "bridge_sha256": EXPECTED_LINUX_BRIDGE_SHA256,
        "smoke": {
            "node": "node001",
            "groups_per_arm": 1,
            "arms": [
                {
                    "arm_id": arm["arm_id"],
                    "group_id": arm["smoke_group_id"],
                }
                for arm in arms
            ],
        },
        "full": {
            "arm_count": len(arms),
            "groups_per_arm": master_seed_count,
            "workers_per_arm_per_node": workers_per_arm,
            "aggregate_workers_per_node": (
                len(arms) * workers_per_arm
            ),
            "nodes": [
                {
                    "name": node,
                    "arms": [
                        {
                            "arm_id": arm["arm_id"],
                            "group_count": next(
                                row["group_count"]
                                for row in arm["dispatch"]["nodes"]
                                if row["name"] == node
                            ),
                            "workers": next(
                                row["workers"]
                                for row in arm["dispatch"]["nodes"]
                                if row["name"] == node
                            ),
                        }
                        for arm in arms
                    ],
                }
                for node in EXPECTED_NODES
            ],
        },
        "reduction": {
            "node": "node001",
            "mode": protocol["reduction_mode"],
            "strict_reducer": protocol["strict_reducer"],
            "compact_analysis": protocol["compact_analysis"],
            "raw_worker_output_download_required": False,
        },
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "live_fidelity": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    plan = {**plan_core, "plan_sha256": sha256_json(plan_core)}
    scripts = {
        "scripts/smoke.sh": _smoke_script(plan, arms).encode("utf-8"),
        **{
            f"scripts/{node}.sh": _node_script(plan, node, arms).encode("utf-8")
            for node in EXPECTED_NODES
        },
        "scripts/reduce.sh": _reduce_script(plan, arms).encode("utf-8"),
    }
    plan_payload = _canonical_bytes(plan)
    attempt_bundle = _deterministic_archive(
        {"orchestration-plan.json": plan_payload, **scripts}
    )
    attempt_bundle_sha = hashlib.sha256(attempt_bundle).hexdigest()
    destination = (
        Path(attempts_root).expanduser().resolve() / confirmation_id / attempt_id
    )
    receipt = {
        "schema": PREPARE_RECEIPT_SCHEMA,
        "status": "PREPARED",
        "phase": "LOCAL_PREPARED_NOT_STAGED",
        "attempt_id": attempt_id,
        "confirmation_id": confirmation_id,
        "plan_sha256": plan["plan_sha256"],
        "source_archive": {
            "path": str(source_archive),
            "sha256": manifest["source_archive_sha256"],
        },
        "run_archive_sha256": run_archive_sha,
        "attempt_bundle_sha256": attempt_bundle_sha,
        "attempt_directory": str(destination),
        "remote_mutation_performed": False,
        "execution_started": False,
        "heavy_execution_started": False,
    }
    _write_new_directory(
        destination,
        {
            "orchestration-plan.json": plan_payload,
            "run-bundle.tar.gz": run_archive,
            "attempt-bundle.tar.gz": attempt_bundle,
            **scripts,
            "prepare-receipt.json": _canonical_bytes(receipt),
        },
    )
    return receipt


def _load_attempt(attempt_directory: str | Path) -> tuple[Path, JSONMap, JSONMap]:
    root = Path(attempt_directory).expanduser().resolve()
    plan = _read_json(root / "orchestration-plan.json", "orchestration plan")
    receipt = _read_json(root / "prepare-receipt.json", "prepare receipt")
    if plan.get("schema") != SCHEMA or receipt.get("schema") != PREPARE_RECEIPT_SCHEMA:
        raise FuryCat2HorizonRemoteOrchestratorV1Error("attempt schema differs")
    core = dict(plan)
    observed = core.pop("plan_sha256", None)
    if observed != sha256_json(core) or observed != receipt.get("plan_sha256"):
        raise FuryCat2HorizonRemoteOrchestratorV1Error("attempt plan identity differs")
    if root != Path(receipt.get("attempt_directory", "")).expanduser().resolve():
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "attempt directory differs from its prepare receipt"
        )
    for name, field in (
        ("run-bundle.tar.gz", "run_archive_sha256"),
        ("attempt-bundle.tar.gz", "attempt_bundle_sha256"),
    ):
        if _file_sha256(root / name) != receipt[field]:
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                f"{name} differs from its prepare receipt"
            )
    return root, plan, receipt


def _action_receipt_path(root: Path, action: str) -> Path:
    return root / f"{action}-receipt.json"


def _failed_action_receipt(
    plan: Mapping[str, Any], action: str, reason: str, details: Any = None
) -> JSONMap:
    return {
        "schema": ACTION_RECEIPT_SCHEMA,
        "status": "FAILED",
        "phase": action.upper(),
        "attempt_id": plan["attempt_id"],
        "confirmation_id": plan["confirmation_id"],
        "plan_sha256": plan["plan_sha256"],
        "reason": reason,
        "details": details,
    }


def _copy_result_row(name: str, result: CommandResult) -> JSONMap:
    return {
        "name": name,
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def _runtime_preflight_command(plan: Mapping[str, Any]) -> str:
    runtime = plan["runtime_paths"]
    bridge = _home(runtime["bridge"])
    checks = [
        "set -eu",
        f"test -x {_home(plan['node_python'])}",
        f"test -x {bridge}",
        f"observed=$(sha256sum {bridge} | cut -d ' ' -f1)",
        f"test \"$observed\" = {shlex.quote(plan['bridge_sha256'])}",
        f"test -d {_home(runtime['cat2new_root'])}",
        f"test -f {_home(runtime['cat2_capability_manifest'])}",
        f"test -d {_home(runtime['cat2_installed_root'])}",
        f"test -f {_home(runtime['cat2_savedvariables'])}",
        f"test -d {_home(runtime['contra260817_root'])}",
        f"test -f {_home(runtime['contra260817_manifest'])}",
    ]
    return "; ".join(checks)


def _installed_preflight_command(plan: Mapping[str, Any]) -> str:
    remote = plan["remote"]
    checks = [
        "set -eu",
        f"test -f {_home(remote['source_release'] + '/o2o_dps/fury_multiseed_hpc_worker_v3.py')}",
        f"test -f {_home(remote['run_release'] + '/horizon-confirmation-manifest.json')}",
        f"test -f {_home(remote['attempt_root'] + '/orchestration-plan.json')}",
        f"test -f {_home(remote['attempt_root'] + '/state/prepared')}",
    ]
    return "; ".join(checks)


def _install_command(plan: Mapping[str, Any], incoming: str) -> str:
    remote = plan["remote"]
    source = remote["source_release"]
    run = remote["run_release"]
    attempt = remote["attempt_root"]
    token = f"{plan['attempt_id']}-{plan['plan_sha256'][:12]}"
    source_tmp = f"{source}.install-{token}"
    run_tmp = f"{run}.install-{token}"
    attempt_tmp = f"{attempt}.install-{token}"
    source_archive = f"{incoming}/source-closure.tar.gz"
    run_archive = f"{incoming}/run-bundle.tar.gz"
    attempt_archive = f"{incoming}/attempt-bundle.tar.gz"
    lines = [
        "set -eu",
        "umask 077",
        f"test \"$(sha256sum {_home(source_archive)} | cut -d ' ' -f1)\" = {shlex.quote(plan['source_archive_sha256'])}",
        f"test \"$(sha256sum {_home(run_archive)} | cut -d ' ' -f1)\" = {shlex.quote(plan['run_archive_sha256'])}",
        f"test \"$(sha256sum {_home(attempt_archive)} | cut -d ' ' -f1)\" = {shlex.quote(plan['attempt_bundle_sha256'])}",
        f"mkdir -p {_home(str(PurePosixPath(source).parent))} {_home(str(PurePosixPath(run).parent))} {_home(str(PurePosixPath(attempt).parent))}",
        f"if test -d {_home(source)}; then test \"$(cat {_home(source + '/.archive-sha256')})\" = {shlex.quote(plan['source_archive_sha256'])}; "
        f"else test ! -e {_home(source_tmp)}; mkdir {_home(source_tmp)}; "
        f"tar -xzf {_home(source_archive)} -C {_home(source_tmp)}; "
        f"printf '%s\\n' {shlex.quote(plan['source_archive_sha256'])} > {_home(source_tmp + '/.archive-sha256')}; "
        f"mv {_home(source_tmp)} {_home(source)}; fi",
        f"if test -d {_home(run)}; then test \"$(cat {_home(run + '/.archive-sha256')})\" = {shlex.quote(plan['run_archive_sha256'])}; "
        f"else test ! -e {_home(run_tmp)}; mkdir {_home(run_tmp)}; "
        f"tar -xzf {_home(run_archive)} -C {_home(run_tmp)}; "
        f"printf '%s\\n' {shlex.quote(plan['run_archive_sha256'])} > {_home(run_tmp + '/.archive-sha256')}; "
        f"mv {_home(run_tmp)} {_home(run)}; fi",
        f"test ! -e {_home(attempt)}",
        f"test ! -e {_home(attempt_tmp)}",
        f"mkdir {_home(attempt_tmp)}",
        f"tar -xzf {_home(attempt_archive)} -C {_home(attempt_tmp)}",
        f"chmod 700 {_home(attempt_tmp + '/scripts/smoke.sh')} {_home(attempt_tmp + '/scripts/reduce.sh')} "
        + " ".join(_home(f"{attempt_tmp}/scripts/{node}.sh") for node in EXPECTED_NODES),
        f"mkdir -p {_home(attempt_tmp + '/state')} {_home(attempt_tmp + '/logs')} {_home(attempt_tmp + '/smoke')} {_home(attempt_tmp + '/full')} {_home(attempt_tmp + '/analysis')}",
        f": > {_home(attempt_tmp + '/state/prepared')}",
        f"mv {_home(attempt_tmp)} {_home(attempt)}",
        f"rm -f {_home(source_archive)} {_home(run_archive)} {_home(attempt_archive)}",
        f"rmdir {_home(incoming)}",
        "printf 'installed=1\\n'",
    ]
    return "; ".join(lines)


def _best_effort_remote_failure(
    transport: Any,
    node: Any,
    plan: Mapping[str, Any],
    phase: str,
    reason: str,
) -> None:
    state = f"{plan['remote']['attempt_root']}/state/{phase}"
    command = "; ".join(
        (
            "set +e",
            f"mkdir -p {_home(state)}",
            f"printf '%s\\n' {shlex.quote(reason)} > {_home(state + '/failure-reason')}",
            f": > {_home(state + '/failed')}",
        )
    )
    transport.run(node, command, timeout=30)


def stage_remote_attempt_v1(
    *,
    attempt_directory: str | Path,
    source_release_root: str | Path = DEFAULT_SOURCE_RELEASE_ROOT,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    transport: Any | None = None,
) -> JSONMap:
    """Stage source/run releases and one fresh attempt; start no worker."""

    root, plan, prepare = _load_attempt(attempt_directory)
    receipt_path = _action_receipt_path(root, "stage")
    if receipt_path.exists():
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "stage was already attempted; use a new attempt ID"
        )
    site = _load_bound_site(plan, site_config)
    runner = transport or SchedulerNodeTransport(site)
    primary = site.nodes[0]
    try:
        with ThreadPoolExecutor(max_workers=len(site.nodes)) as pool:
            preflight = list(
                pool.map(
                    lambda node: runner.run(
                        node, _runtime_preflight_command(plan), timeout=90
                    ),
                    site.nodes,
                )
            )
        if any(result.returncode != 0 for result in preflight):
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                "one or more node runtime preflights failed"
            )
        incoming = (
            f"{site.node_shared_root}/incoming/fury-horizon-"
            f"{plan['confirmation_id'][:16]}-{plan['attempt_id']}-"
            f"{plan['plan_sha256'][:12]}"
        )
        create = runner.run(
            primary,
            "; ".join(
                (
                    "set -eu",
                    f"test ! -e {_home(plan['remote']['attempt_root'])}",
                    f"test ! -e {_home(incoming)}",
                    f"mkdir -p {_home(str(PurePosixPath(incoming).parent))}",
                    f"mkdir {_home(incoming)}",
                )
            ),
            timeout=60,
        )
        if create.returncode != 0:
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                "fresh remote incoming/attempt path could not be reserved"
            )
        source_archive = (
            Path(source_release_root).expanduser().resolve()
            / plan["source_closure_sha256"]
            / "source-closure.tar.gz"
        )
        if _file_sha256(source_archive) != plan["source_archive_sha256"]:
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                "local source archive changed after preparation"
            )
        copy_specs = (
            ("source-closure.tar.gz", source_archive),
            ("run-bundle.tar.gz", root / "run-bundle.tar.gz"),
            ("attempt-bundle.tar.gz", root / "attempt-bundle.tar.gz"),
        )
        copies = []
        for name, path in copy_specs:
            result = runner.copy(
                primary, path, f"~/{incoming}/{name}", timeout=240
            )
            copies.append(_copy_result_row(name, result))
            if result.returncode != 0:
                raise FuryCat2HorizonRemoteOrchestratorV1Error(
                    f"remote copy failed for {name}"
                )
        install_plan = dict(plan)
        install_plan["attempt_bundle_sha256"] = prepare["attempt_bundle_sha256"]
        installed = runner.run(
            primary, _install_command(install_plan, incoming), timeout=240
        )
        if installed.returncode != 0:
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                "content-addressed remote install failed"
            )
        with ThreadPoolExecutor(max_workers=len(site.nodes)) as pool:
            verification = list(
                pool.map(
                    lambda node: runner.run(
                        node, _installed_preflight_command(plan), timeout=60
                    ),
                    site.nodes,
                )
            )
        if any(result.returncode != 0 for result in verification):
            _best_effort_remote_failure(
                runner, primary, plan, "stage", "SIX_NODE_SHARED_RELEASE_VERIFY_FAILED"
            )
            raise FuryCat2HorizonRemoteOrchestratorV1Error(
                "source/run/attempt is not visible on all six nodes"
            )
        receipt: JSONMap = {
            "schema": ACTION_RECEIPT_SCHEMA,
            "status": "PREPARED",
            "phase": "STAGED",
            "attempt_id": plan["attempt_id"],
            "confirmation_id": plan["confirmation_id"],
            "plan_sha256": plan["plan_sha256"],
            "copies": copies,
            "node_preflight": [
                {
                    "name": node.name,
                    "runtime_returncode": before.returncode,
                    "installed_returncode": after.returncode,
                }
                for node, before, after in zip(site.nodes, preflight, verification)
            ],
            "remote_attempt_root": plan["remote"]["attempt_root"],
            "execution_started": False,
            "heavy_execution_started": False,
        }
    except Exception as error:
        receipt = _failed_action_receipt(plan, "stage", str(error))
    _write_once(receipt_path, receipt)
    return receipt


def _read_action_receipt(root: Path, action: str) -> JSONMap | None:
    path = _action_receipt_path(root, action)
    return None if not path.is_file() else _read_json(path, f"{action} receipt")


def _marker_probe_command(plan: Mapping[str, Any]) -> str:
    attempt = plan["remote"]["attempt_root"]
    arms = " ".join(shlex.quote(arm) for arm in _plan_arm_ids(plan))
    nodes = " ".join(shlex.quote(node) for node in EXPECTED_NODES)
    # Keep this probe compact.  The scheduler transport passes the remote shell
    # command through a Windows argv boundary; spelling out every arm/node path
    # made the otherwise read-only status probe exceed that boundary.
    lines = [
        "set -eu",
        f"attempt={_home(attempt)}",
        "mark() { key=$1; relative=$2; "
        "if test -e \"$attempt/$relative\"; then present=1; else present=0; fi; "
        "printf 'M|%s|%s\\n' \"$key\" \"$present\"; }",
        "mark prepared state/prepared",
        "mark stage.failed state/stage/failed",
        "mark smoke.running state/smoke/running",
        "mark smoke.complete state/smoke/complete",
        "mark smoke.failed state/smoke/failed",
        "mark reduce.running state/reduce/running",
        "mark reduce.complete state/reduce/complete",
        "mark reduce.failed state/reduce/failed",
        "mark analysis.compact analysis/compact-analysis.json",
        f"for arm in {arms}; do "
        "mark \"smoke.arm.$arm.complete\" \"state/smoke/arms/$arm/complete\"; "
        "mark \"smoke.arm.$arm.failed\" \"state/smoke/arms/$arm/failed\"; "
        "done",
        f"for node in {nodes}; do "
        "mark \"full.node.$node.running\" \"state/full/nodes/$node/running\"; "
        "mark \"full.node.$node.complete\" \"state/full/nodes/$node/complete\"; "
        "mark \"full.node.$node.failed\" \"state/full/nodes/$node/failed\"; "
        f"for arm in {arms}; do "
        "mark \"full.arm.$arm.$node.complete\" \"state/full/arms/$arm/nodes/$node/complete\"; "
        "mark \"full.arm.$arm.$node.failed\" \"state/full/arms/$arm/nodes/$node/failed\"; "
        "done; done",
        f"for phase in smoke full; do for arm in {arms}; do "
        "output=\"$attempt/$phase/$arm/output/receipts\"; "
        "if test -d \"$output\"; then n=$(find \"$output\" -type f -name '*.json' | wc -l); "
        "else n=0; fi; printf 'C|%s|%s|%s\\n' \"$phase\" \"$arm\" \"$n\"; "
        "done; done",
    ]
    return "; ".join(lines)


def _parse_markers(
    stdout: str, arm_ids: Sequence[str] = CONFIRMATION_ARM_IDS
) -> tuple[dict[str, bool], dict[str, dict[str, int]]]:
    markers: dict[str, bool] = {}
    counts: dict[str, dict[str, int]] = {"smoke": {}, "full": {}}
    for line in stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] == "M" and parts[2] in {"0", "1"}:
            markers[parts[1]] = parts[2] == "1"
        elif (
            len(parts) == 4
            and parts[0] == "C"
            and parts[1] in counts
            and parts[2] in arm_ids
        ):
            try:
                counts[parts[1]][parts[2]] = int(parts[3])
            except ValueError as error:
                raise FuryCat2HorizonRemoteOrchestratorV1Error(
                    "remote status returned an invalid count"
                ) from error
    return markers, counts


def derive_status_v1(
    plan: Mapping[str, Any],
    markers: Mapping[str, bool],
    counts: Mapping[str, Mapping[str, int]],
    *,
    local_failed_actions: Sequence[str] = (),
) -> JSONMap:
    """Derive one of PREPARED/RUNNING/FAILED/COMPLETE from preserved markers."""

    arm_ids = _plan_arm_ids(plan)
    seed_count = _plan_seed_count(plan)
    failure_markers = sorted(
        key for key, present in markers.items() if present and key.endswith(".failed")
    )
    smoke_complete = markers.get("smoke.complete", False) and all(
        markers.get(f"smoke.arm.{arm}.complete", False)
        for arm in arm_ids
    )
    full_complete = all(
        markers.get(f"full.node.{node}.complete", False) for node in EXPECTED_NODES
    ) and all(
        markers.get(f"full.arm.{arm}.{node}.complete", False)
        for arm in arm_ids
        for node in EXPECTED_NODES
    )
    reduction_complete = (
        markers.get("reduce.complete", False)
        and markers.get("analysis.compact", False)
        and full_complete
    )
    if local_failed_actions or failure_markers:
        status, phase = "FAILED", "FAILED"
    elif reduction_complete:
        status, phase = "COMPLETE", "REDUCTION_COMPLETE"
    elif markers.get("reduce.running", False):
        status, phase = "RUNNING", "REDUCTION"
    elif full_complete:
        status, phase = "RUNNING", "FULL_COMPLETE_READY_FOR_REDUCTION"
    elif any(markers.get(f"full.node.{node}.running", False) for node in EXPECTED_NODES):
        status, phase = "RUNNING", "FULL_DISPATCH"
    elif smoke_complete:
        status, phase = "RUNNING", "SMOKE_COMPLETE_READY_FOR_FULL"
    elif markers.get("smoke.running", False):
        status, phase = "RUNNING", "SMOKE"
    else:
        status, phase = "PREPARED", "STAGED"
    return {
        "schema": STATUS_SCHEMA,
        "status": status,
        "phase": phase,
        "attempt_id": plan["attempt_id"],
        "confirmation_id": plan["confirmation_id"],
        "smoke_complete": smoke_complete,
        "full_complete": full_complete,
        "reduction_complete": reduction_complete,
        "failure_markers": failure_markers,
        "local_failed_actions": sorted(local_failed_actions),
        "receipt_counts": {
            phase_name: {
                arm: int(counts.get(phase_name, {}).get(arm, 0))
                for arm in arm_ids
            }
            for phase_name in ("smoke", "full")
        },
        "expected_receipt_counts": {
            "smoke_per_arm": 1,
            "full_per_arm": seed_count,
        },
        "simulator_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }


def status_remote_attempt_v1(
    *,
    attempt_directory: str | Path,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    transport: Any | None = None,
) -> JSONMap:
    root, plan, _ = _load_attempt(attempt_directory)
    failed_actions = [
        action
        for action in ("stage", "smoke-launch", "full-launch", "reduce-launch")
        if (receipt := _read_action_receipt(root, action)) is not None
        and receipt.get("status") == "FAILED"
    ]
    stage = _read_action_receipt(root, "stage")
    if stage is None:
        return {
            "schema": STATUS_SCHEMA,
            "status": "PREPARED",
            "phase": "LOCAL_PREPARED_NOT_STAGED",
            "attempt_id": plan["attempt_id"],
            "confirmation_id": plan["confirmation_id"],
            "failure_markers": [],
            "local_failed_actions": [],
            "remote_probe_performed": False,
            "simulator_only": True,
            "scientific_result_available": False,
            "deployment_allowed": False,
        }
    if stage.get("status") == "FAILED":
        return {
            "schema": STATUS_SCHEMA,
            "status": "FAILED",
            "phase": "FAILED",
            "attempt_id": plan["attempt_id"],
            "confirmation_id": plan["confirmation_id"],
            "failure_markers": [],
            "local_failed_actions": failed_actions,
            "remote_probe_performed": False,
            "simulator_only": True,
            "scientific_result_available": False,
            "deployment_allowed": False,
        }
    site = _load_bound_site(plan, site_config)
    runner = transport or SchedulerNodeTransport(site)
    result = runner.run(site.nodes[0], _marker_probe_command(plan), timeout=90)
    if result.returncode != 0:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "remote attempt status probe failed"
        )
    markers, counts = _parse_markers(result.stdout, _plan_arm_ids(plan))
    if not markers.get("prepared", False):
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "remote attempt lost its prepared marker"
        )
    report = derive_status_v1(
        plan, markers, counts, local_failed_actions=failed_actions
    )
    report["remote_probe_performed"] = True
    return report


def _launch_command(plan: Mapping[str, Any], phase: str, script_name: str, log_name: str) -> str:
    attempt = plan["remote"]["attempt_root"]
    state = f"{attempt}/state/{phase}"
    return "; ".join(
        (
            "set -eu",
            f"test -f {_home(attempt + '/state/prepared')}",
            f"mkdir -p {_home(state)}",
            f"mkdir {_home(state + '/launch-lock')}",
            f": > {_home(state + '/running')}",
            f"nohup sh {_home(attempt + '/scripts/' + script_name)} > {_home(attempt + '/logs/' + log_name)} 2>&1 < /dev/null & pid=$!",
            f"printf '%s\\n' \"$pid\" > {_home(state + '/pid')}",
            "printf 'pid=%s\\n' \"$pid\"",
        )
    )


def _launch_one_action(
    *,
    action: str,
    phase: str,
    script_name: str,
    log_name: str,
    attempt_directory: str | Path,
    site_config: str | Path,
    transport: Any | None,
    required_phase: str,
) -> JSONMap:
    root, plan, _ = _load_attempt(attempt_directory)
    receipt_path = _action_receipt_path(root, action)
    if receipt_path.exists():
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"{action} was already attempted; use a new attempt ID after failure"
        )
    site = _load_bound_site(plan, site_config)
    runner = transport or SchedulerNodeTransport(site)
    status = status_remote_attempt_v1(
        attempt_directory=root, site_config=site_config, transport=runner
    )
    if status["status"] == "FAILED" or status["phase"] != required_phase:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            f"{action} requires phase {required_phase}; observed "
            f"{status['status']}/{status['phase']}"
        )
    else:
        result = runner.run(
            site.nodes[0],
            _launch_command(plan, phase, script_name, log_name),
            timeout=60,
        )
        pid = next(
            (line[4:] for line in result.stdout.splitlines() if line.startswith("pid=")),
            None,
        )
        if result.returncode != 0 or not pid:
            receipt = _failed_action_receipt(
                plan, action, "remote supervisor launch failed", _copy_result_row(action, result)
            )
            _best_effort_remote_failure(
                runner, site.nodes[0], plan, phase, "SUPERVISOR_LAUNCH_FAILED"
            )
        else:
            receipt = {
                "schema": ACTION_RECEIPT_SCHEMA,
                "status": "RUNNING",
                "phase": phase.upper(),
                "attempt_id": plan["attempt_id"],
                "confirmation_id": plan["confirmation_id"],
                "plan_sha256": plan["plan_sha256"],
                "node": "node001",
                "pid": pid,
                "transport_returncode": result.returncode,
            }
    _write_once(receipt_path, receipt)
    return receipt


def launch_smoke_v1(
    *,
    attempt_directory: str | Path,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    transport: Any | None = None,
) -> JSONMap:
    return _launch_one_action(
        action="smoke-launch",
        phase="smoke",
        script_name="smoke.sh",
        log_name="smoke-launch.log",
        attempt_directory=attempt_directory,
        site_config=site_config,
        transport=transport,
        required_phase="STAGED",
    )


def launch_full_v1(
    *,
    attempt_directory: str | Path,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    transport: Any | None = None,
) -> JSONMap:
    """Launch exactly one three-arm supervisor on each of the six nodes."""

    root, plan, _ = _load_attempt(attempt_directory)
    receipt_path = _action_receipt_path(root, "full-launch")
    if receipt_path.exists():
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "full launch was already attempted; use a new attempt ID after failure"
        )
    site = _load_bound_site(plan, site_config)
    runner = transport or SchedulerNodeTransport(site)
    status = status_remote_attempt_v1(
        attempt_directory=root, site_config=site_config, transport=runner
    )
    if status["status"] == "FAILED" or status["phase"] != "SMOKE_COMPLETE_READY_FOR_FULL":
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "full dispatch requires all three node001 smoke groups to complete; "
            f"observed {status['status']}/{status['phase']}"
        )
    else:
        def launch(node: Any) -> tuple[Any, CommandResult]:
            command = _launch_command(
                plan,
                f"full/nodes/{node.name}",
                f"{node.name}.sh",
                f"{node.name}-launch.log",
            )
            return node, runner.run(node, command, timeout=60)

        with ThreadPoolExecutor(max_workers=len(site.nodes)) as pool:
            results = list(pool.map(launch, site.nodes))
        launches = []
        for node, result in results:
            pid = next(
                (line[4:] for line in result.stdout.splitlines() if line.startswith("pid=")),
                None,
            )
            launches.append(
                {
                    "name": node.name,
                    "status": "RUNNING" if result.returncode == 0 and pid else "FAILED",
                    "pid": pid,
                    "transport_returncode": result.returncode,
                    "stderr": result.stderr[-2000:],
                }
            )
        if any(row["status"] == "FAILED" for row in launches):
            receipt = _failed_action_receipt(
                plan, "full-launch", "one or more node supervisors failed to launch", launches
            )
            _best_effort_remote_failure(
                runner, site.nodes[0], plan, "full", "PARTIAL_SIX_NODE_LAUNCH_FAILURE"
            )
        else:
            receipt = {
                "schema": ACTION_RECEIPT_SCHEMA,
                "status": "RUNNING",
                "phase": "FULL_DISPATCH",
                "attempt_id": plan["attempt_id"],
                "confirmation_id": plan["confirmation_id"],
                "plan_sha256": plan["plan_sha256"],
                "workers_per_arm_per_node": _plan_workers_per_arm(plan),
                "aggregate_workers_per_node": plan["full"][
                    "aggregate_workers_per_node"
                ],
                "launches": launches,
            }
    _write_once(receipt_path, receipt)
    return receipt


def launch_reduce_v1(
    *,
    attempt_directory: str | Path,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    transport: Any | None = None,
) -> JSONMap:
    return _launch_one_action(
        action="reduce-launch",
        phase="reduce",
        script_name="reduce.sh",
        log_name="reduce-launch.log",
        attempt_directory=attempt_directory,
        site_config=site_config,
        transport=transport,
        required_phase="FULL_COMPLETE_READY_FOR_REDUCTION",
    )


def fetch_compact_result_v1(
    *,
    attempt_directory: str | Path,
    site_config: str | Path = DEFAULT_SITE_CONFIG,
    transport: Any | None = None,
) -> JSONMap:
    root, plan, _ = _load_attempt(attempt_directory)
    site = _load_bound_site(plan, site_config)
    runner = transport or SchedulerNodeTransport(site)
    status = status_remote_attempt_v1(
        attempt_directory=root, site_config=site_config, transport=runner
    )
    if status["status"] != "COMPLETE":
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "compact result is available only after COMPLETE"
        )
    result = runner.run(
        site.nodes[0],
        f"set -eu; cat {_home(plan['remote']['attempt_root'] + '/analysis/compact-analysis.json')}",
        timeout=60,
    )
    if result.returncode != 0:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "compact remote result pull failed"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "compact remote result is not JSON"
        ) from error
    if not isinstance(value, dict) or "paired_trace" in value:
        raise FuryCat2HorizonRemoteOrchestratorV1Error(
            "remote compact result is malformed or contains raw paired traces"
        )
    return value


def _preview(attempt_directory: Path, action: str) -> JSONMap:
    _, plan, _ = _load_attempt(attempt_directory)
    return {
        "schema": ACTION_RECEIPT_SCHEMA,
        "status": "PREPARED",
        "phase": f"{action.upper()}_NOT_APPLIED",
        "attempt_id": plan["attempt_id"],
        "confirmation_id": plan["confirmation_id"],
        "plan_sha256": plan["plan_sha256"],
        "remote_mutation_performed": False,
        "execution_started": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--run-root", type=Path, required=True)
    prepare.add_argument(
        "--source-release-root", type=Path, default=DEFAULT_SOURCE_RELEASE_ROOT
    )
    prepare.add_argument("--attempt-id", required=True)
    prepare.add_argument("--attempts-root", type=Path, default=DEFAULT_ATTEMPTS_ROOT)
    prepare.add_argument("--site-config", type=Path, default=DEFAULT_SITE_CONFIG)
    prepare.add_argument("--bridge-relative", default=DEFAULT_BRIDGE_RELATIVE)
    prepare.add_argument("--cat2new-relative", default=DEFAULT_CAT2NEW_RELATIVE)
    prepare.add_argument(
        "--cat2-context-relative", default=DEFAULT_CAT2_CONTEXT_RELATIVE
    )
    prepare.add_argument(
        "--contra260817-relative", default=DEFAULT_CONTRA260817_RELATIVE
    )
    for name in ("stage", "smoke", "launch", "reduce"):
        command = commands.add_parser(name)
        command.add_argument("--attempt-directory", type=Path, required=True)
        command.add_argument("--site-config", type=Path, default=DEFAULT_SITE_CONFIG)
        if name == "stage":
            command.add_argument(
                "--source-release-root",
                type=Path,
                default=DEFAULT_SOURCE_RELEASE_ROOT,
            )
        command.add_argument("--apply", action="store_true")
    for name in ("status", "result"):
        command = commands.add_parser(name)
        command.add_argument("--attempt-directory", type=Path, required=True)
        command.add_argument("--site-config", type=Path, default=DEFAULT_SITE_CONFIG)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare_remote_attempt_v1(
                run_root=args.run_root,
                source_release_root=args.source_release_root,
                attempt_id=args.attempt_id,
                attempts_root=args.attempts_root,
                site_config=args.site_config,
                bridge_relative=args.bridge_relative,
                cat2new_relative=args.cat2new_relative,
                cat2_context_relative=args.cat2_context_relative,
                contra260817_relative=args.contra260817_relative,
            )
        elif args.command in {"stage", "smoke", "launch", "reduce"} and not args.apply:
            report = _preview(args.attempt_directory, args.command)
        elif args.command == "stage":
            report = stage_remote_attempt_v1(
                attempt_directory=args.attempt_directory,
                source_release_root=args.source_release_root,
                site_config=args.site_config,
            )
        elif args.command == "smoke":
            report = launch_smoke_v1(
                attempt_directory=args.attempt_directory,
                site_config=args.site_config,
            )
        elif args.command == "launch":
            report = launch_full_v1(
                attempt_directory=args.attempt_directory,
                site_config=args.site_config,
            )
        elif args.command == "reduce":
            report = launch_reduce_v1(
                attempt_directory=args.attempt_directory,
                site_config=args.site_config,
            )
        elif args.command == "status":
            report = status_remote_attempt_v1(
                attempt_directory=args.attempt_directory,
                site_config=args.site_config,
            )
        else:
            report = fetch_compact_result_v1(
                attempt_directory=args.attempt_directory,
                site_config=args.site_config,
            )
        print(_canonical_bytes(report).decode("utf-8"), end="")
        return 0 if report.get("status") != "FAILED" else 2
    except (
        FuryCat2HorizonRemoteOrchestratorV1Error,
        OSError,
        ValueError,
    ) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


__all__: Sequence[str] = (
    "ACTION_RECEIPT_SCHEMA",
    "DEFAULT_ATTEMPTS_ROOT",
    "FuryCat2HorizonRemoteOrchestratorV1Error",
    "SCHEMA",
    "STATUS_SCHEMA",
    "derive_status_v1",
    "fetch_compact_result_v1",
    "launch_full_v1",
    "launch_reduce_v1",
    "launch_smoke_v1",
    "prepare_remote_attempt_v1",
    "stage_remote_attempt_v1",
    "status_remote_attempt_v1",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
