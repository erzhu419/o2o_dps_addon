"""Thin distributed runner for the fixed 84-instance External Stage-6 build.

The plan reads only the committed Stage-5 manifest and partition metadata.
Each worker hands one instance to the existing Stage-6 partition kernel once;
the reducer hands the completed partition receipts to the existing publisher.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import chronicle_external_hpc_stage5_v1 as stage5_hpc
from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_wave_model_v2 as model_v2
from .hpc_environment_v1 import DEFAULT_SITE_CONFIG


SCHEMA = "chronicle_external_hpc_stage6/v1"
REVISION = "utk_84_instance_thin_v1"
NODES = stage5_hpc.NODES
INSTANCE_COUNT = stage5_hpc.INSTANCE_COUNT
MAX_ATTEMPTS = stage5_hpc.MAX_ATTEMPTS
SHARED_ROOT = stage5_hpc.SHARED_ROOT
STAGING_ROOT = stage5_hpc.STAGING_ROOT
CANONICAL_STAGE5 = stage5_hpc.CANONICAL_STAGE5
CANONICAL_STAGE6 = (
    "offline_data/derived/chronicle_external_team_background_generator/v2/"
    "utk_postfix_dev_20260903_noon"
)


class Stage6HpcError(RuntimeError):
    pass


def _manifest(
    path_value: str | Path,
    *,
    expected_instances: int,
    require_partitions: bool = True,
) -> tuple[Path, dict[str, Any], bytes, str]:
    path, manifest, payload = stage5_hpc.read_json(path_value)
    content_sha = background_v2._verify_content_address(
        manifest, label="Stage-5 manifest"
    )
    if (
        manifest.get("schema") != model_v2.SCHEMA
        or manifest.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or manifest.get("status") != model_v2.STATUS
    ):
        raise Stage6HpcError("input is not the frozen Stage-5 artifact")
    addressed = path.with_name(
        f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
    )
    if not addressed.is_file() or addressed.read_bytes() != payload:
        raise Stage6HpcError("Stage-5 stable/addressed manifests differ")
    entries = manifest.get("instances")
    order = manifest.get("instance_order")
    if (
        not isinstance(entries, list)
        or not isinstance(order, list)
        or len(entries) != expected_instances
        or [row.get("instance_id") for row in entries] != order
        or order != sorted(set(order))
    ):
        raise Stage6HpcError("Stage-5 instance set/order differs")
    background_v2._component_index(manifest)
    for row in entries:
        background_v2._verify_content_address(row, label="Stage-5 instance")
        partition = row.get("partition", {})
        source = path.parent / str(partition.get("path", ""))
        if require_partitions and (
            not source.is_file()
            or source.stat().st_size != partition.get("compressed_size_bytes")
        ):
            raise Stage6HpcError(f"missing/truncated Stage-5 partition: {source.name}")
    return path, manifest, payload, content_sha


def _plan_id(plan: Mapping[str, Any]) -> str:
    core = dict(plan)
    observed = core.pop("plan_id", None)
    expected = hashlib.sha256(stage5_hpc.canonical(core)).hexdigest()
    if observed != expected:
        raise Stage6HpcError("plan identity differs")
    return expected


def make_plan(
    *,
    team_model_manifest: str | Path,
    capacity_rows: Sequence[Mapping[str, Any]] | None = None,
    site_path: str | Path = DEFAULT_SITE_CONFIG,
    expected_instances: int = INSTANCE_COUNT,
    project_root: str | Path = Path(__file__).resolve().parents[1],
) -> dict[str, Any]:
    """Capture Stage-5 identity and deterministic node assignments."""

    _, manifest, payload, content_sha = _manifest(
        team_model_manifest,
        expected_instances=expected_instances,
        require_partitions=False,
    )
    slots = stage5_hpc.capacity_slots(
        capacity_rows or stage5_hpc._probe_rows(site_path)
    )
    shards = stage5_hpc._allocate(manifest["instance_order"], slots)
    by_id = {row["instance_id"]: row for row in manifest["instances"]}
    for shard in shards:
        shard["entry_sha"] = background_v2._verify_content_address(
            by_id[shard["instance_id"]], label="Stage-5 instance"
        )
    root = Path(project_root).resolve()
    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": "PLANNED_NOT_RUN",
        "shared_root": SHARED_ROOT,
        "staging_root": STAGING_ROOT,
        "stage5": {
            "content_sha": content_sha,
            "file_sha": hashlib.sha256(payload).hexdigest(),
            "partition_count": len(manifest["instances"]),
            "compressed_bytes": sum(
                row["partition"]["compressed_size_bytes"]
                for row in manifest["instances"]
            ),
        },
        "implementation_sha": {
            "stage5_hpc": stage5_hpc.file_sha(
                root / "o2o_dps/chronicle_external_hpc_stage5_v1.py"
            ),
            "stage6": stage5_hpc.file_sha(
                root
                / "o2o_dps/chronicle_external_team_background_generator_v2.py"
            ),
            "orchestrator": stage5_hpc.file_sha(Path(__file__).resolve()),
        },
        "capacity": {
            "captured_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "worker_slots": slots,
        },
        "shards": shards,
        "transfer": {
            "source": "source/o2o_dps",
            "stage5_already_in_shared_canonical_tree": True,
            "raw_normalized_or_stage1_to_4": None,
        },
        "comparison_ready": False,
    }
    return {**core, "plan_id": hashlib.sha256(stage5_hpc.canonical(core)).hexdigest()}


def save_plan(plan: Mapping[str, Any], directory: str | Path) -> Path:
    identity = _plan_id(plan)
    path = Path(directory).resolve() / f"stage6_hpc_plan.{identity}.json"
    stage5_hpc.write_once(path, stage5_hpc.canonical(plan) + b"\n")
    return path


def load_plan(path_value: str | Path) -> tuple[Path, dict[str, Any]]:
    path, plan, _ = stage5_hpc.read_json(path_value)
    if plan.get("schema") != SCHEMA or plan.get("revision") != REVISION:
        raise Stage6HpcError("unsupported plan")
    _plan_id(plan)
    if plan.get("transfer", {}).get("raw_normalized_or_stage1_to_4") is not None:
        raise Stage6HpcError("plan widens the transfer boundary")
    return path, plan


def _live_code_matches(plan: Mapping[str, Any]) -> None:
    root = Path(__file__).resolve().parents[1]
    observed = {
        "stage5_hpc": stage5_hpc.file_sha(
            root / "o2o_dps/chronicle_external_hpc_stage5_v1.py"
        ),
        "stage6": stage5_hpc.file_sha(
            root / "o2o_dps/chronicle_external_team_background_generator_v2.py"
        ),
        "orchestrator": stage5_hpc.file_sha(Path(__file__).resolve()),
    }
    if observed != plan["implementation_sha"]:
        raise Stage6HpcError("implementation differs from plan")


def _release(plan: Mapping[str, Any], shared_root: str | Path) -> Path:
    return Path(shared_root).expanduser().resolve() / "releases" / plan["plan_id"]


def _input(
    shared: Path, plan: Mapping[str, Any]
) -> tuple[Path, dict[str, Any], bytes, str]:
    result = _manifest(
        shared / CANONICAL_STAGE5 / "manifest.json",
        expected_instances=len(plan["shards"]),
    )
    if (
        result[3] != plan["stage5"]["content_sha"]
        or hashlib.sha256(result[2]).hexdigest() != plan["stage5"]["file_sha"]
    ):
        raise Stage6HpcError("live Stage-5 artifact differs from plan")
    return result


def initialize_release(plan_path: str | Path, *, shared_root: str | Path) -> Path:
    """Admit the already-canonical Stage-5 artifact and create receipt space."""

    _, plan = load_plan(plan_path)
    _live_code_matches(plan)
    shared = Path(shared_root).expanduser().resolve()
    _input(shared, plan)
    release = _release(plan, shared)
    release.mkdir(parents=True, exist_ok=True)
    stage5_hpc.write_once(release / "plan.json", stage5_hpc.canonical(plan) + b"\n")
    (shared / CANONICAL_STAGE6 / ".hpc" / plan["plan_id"] / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    return release


def _shard(plan: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    matches = [row for row in plan["shards"] if row["instance_id"] == instance_id]
    if len(matches) != 1:
        raise Stage6HpcError("instance is absent or duplicated in plan")
    return matches[0]


def _context(
    *,
    shared: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    instance_id: str,
) -> background_v2._InputInstance:
    index = manifest["instance_order"].index(instance_id)
    entry = manifest["instances"][index]
    partition = entry["partition"]
    return background_v2._InputInstance(
        index=index,
        instance_id=instance_id,
        entry=deepcopy(entry),
        partition_path=background_v2._resolve_relative(
            manifest_path.parent,
            partition["path"],
            shared / "offline_data",
            label="Stage-5 partition",
        ),
        component_by_node=background_v2._component_index(manifest),
        contamination=deepcopy(entry["contamination_lane"]),
    )


def run_worker(
    *,
    plan_path: str | Path,
    shared_root: str | Path,
    instance_id: str,
    node: str,
    attempt: int,
) -> dict[str, Any]:
    """Build one Stage-6 partition with the existing streaming kernel."""

    _, plan = load_plan(plan_path)
    shard = _shard(plan, instance_id)
    if not 1 <= attempt <= MAX_ATTEMPTS or shard["retry_nodes"][attempt - 1] != node:
        raise Stage6HpcError("worker node/attempt differs from plan")
    _live_code_matches(plan)
    release = _release(plan, shared_root)
    if (release / "plan.json").read_bytes() != stage5_hpc.canonical(plan) + b"\n":
        raise Stage6HpcError("release plan differs")
    shared = Path(shared_root).expanduser().resolve()
    output = shared / CANONICAL_STAGE6
    receipt_path = output / ".hpc" / plan["plan_id"] / "receipts" / f"{instance_id}.json"
    if receipt_path.exists():
        _, receipt, _ = stage5_hpc.read_json(receipt_path)
        partition = receipt.get("partition", {})
        final = output / str(partition.get("path", ""))
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != instance_id
            or not final.is_file()
            or stage5_hpc.file_sha(final) != partition.get("compressed_sha")
        ):
            raise Stage6HpcError("existing receipt/output differs")
        return {"status": "RESUMED", "receipt": str(receipt_path)}
    manifest_path, manifest, _, _ = _input(shared, plan)
    context = _context(
        shared=shared,
        manifest_path=manifest_path,
        manifest=manifest,
        instance_id=instance_id,
    )
    if (
        background_v2._verify_content_address(
            context.entry, label="Stage-5 instance"
        )
        != shard["entry_sha"]
    ):
        raise Stage6HpcError("Stage-5 instance entry differs from plan")
    output.mkdir(parents=True, exist_ok=True)
    built = background_v2._build_partition(context, output_directory=output)
    if built.final_path.exists():
        if stage5_hpc.file_sha(built.final_path) != built.compressed_file_sha256:
            built.temporary_path.unlink(missing_ok=True)
            raise Stage6HpcError("divergent duplicate partition")
        built.temporary_path.unlink(missing_ok=True)
        publication = "RESUMED"
    else:
        try:
            os.link(built.temporary_path, built.final_path)
            publication = "PUBLISHED"
        except FileExistsError:
            if stage5_hpc.file_sha(built.final_path) != built.compressed_file_sha256:
                raise Stage6HpcError("divergent duplicate partition")
            publication = "RESUMED"
        finally:
            built.temporary_path.unlink(missing_ok=True)
    receipt = {
        "schema": SCHEMA + "/receipt",
        "revision": REVISION,
        "plan_id": plan["plan_id"],
        "instance_id": instance_id,
        "partition": {
            "path": built.final_path.name,
            "compressed_sha": built.compressed_file_sha256,
            "publication": publication,
        },
        "manifest_entry": built.manifest_entry,
        "block_descriptors": list(built.block_descriptors),
    }
    status = stage5_hpc.write_once(
        receipt_path, stage5_hpc.canonical(receipt) + b"\n"
    )
    return {"status": status, "receipt": str(receipt_path)}


def reduce_stage6(
    *, plan_path: str | Path, shared_root: str | Path
) -> dict[str, Any]:
    """Require every receipt, then reuse the frozen Stage-6 publisher."""

    _, plan = load_plan(plan_path)
    _live_code_matches(plan)
    shared = Path(shared_root).expanduser().resolve()
    release = _release(plan, shared)
    if (release / "plan.json").read_bytes() != stage5_hpc.canonical(plan) + b"\n":
        raise Stage6HpcError("release plan differs")
    output = shared / CANONICAL_STAGE6
    receipts_directory = output / ".hpc" / plan["plan_id"] / "receipts"
    actual = sorted(path.name for path in receipts_directory.glob("*.json"))
    expected = sorted(f"{row['instance_id']}.json" for row in plan["shards"])
    if actual != expected:
        raise Stage6HpcError("receipt set is incomplete or unexpected")
    receipts = []
    for shard in plan["shards"]:
        _, receipt, _ = stage5_hpc.read_json(
            receipts_directory / f"{shard['instance_id']}.json"
        )
        final = output / receipt.get("partition", {}).get("path", "")
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != shard["instance_id"]
            or not final.is_file()
            or stage5_hpc.file_sha(final)
            != receipt.get("partition", {}).get("compressed_sha")
        ):
            raise Stage6HpcError("receipt/output identity differs")
        receipts.append(receipt)
    manifest_path, manifest, payload, content_sha = _input(shared, plan)
    addressed = manifest_path.with_name(
        f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
    )
    contexts = tuple(
        _context(
            shared=shared,
            manifest_path=manifest_path,
            manifest=manifest,
            instance_id=instance_id,
        )
        for instance_id in manifest["instance_order"]
    )
    closure = background_v2._InputClosure(
        manifest=manifest,
        manifest_path=manifest_path,
        addressed_path=addressed,
        data_root=shared / "offline_data",
        content_sha256=content_sha,
        file_sha256=hashlib.sha256(payload).hexdigest(),
        component_by_node=background_v2._component_index(manifest),
        instances=contexts,
        cohort_receipt_binding=deepcopy(
            manifest["input_closure"]["cohort_receipt"]
        ),
    )
    builds: dict[str, background_v2._PartitionBuild] = {}
    for receipt in receipts:
        final = output / receipt["partition"]["path"]
        descriptor, name = tempfile.mkstemp(prefix=f".{final.name}.", dir=output)
        os.close(descriptor)
        temporary = Path(name)
        temporary.unlink()
        os.link(final, temporary)
        builds[receipt["instance_id"]] = background_v2._PartitionBuild(
            temporary_path=temporary,
            final_path=final,
            compressed_file_sha256=receipt["partition"]["compressed_sha"],
            manifest_entry=receipt["manifest_entry"],
            block_descriptors=tuple(receipt["block_descriptors"]),
        )
    original_load = background_v2._load_input_closure
    original_build = background_v2._build_partition
    try:
        background_v2._load_input_closure = lambda *_args, **_kwargs: closure
        background_v2._build_partition = lambda context, **_kwargs: builds[
            context.instance_id
        ]
        result = background_v2.build_external_team_background_generator(
            team_model_manifest_path=manifest_path,
            output_directory=output,
            workers=1,
        )
    finally:
        background_v2._load_input_closure = original_load
        background_v2._build_partition = original_build
        for build in builds.values():
            build.temporary_path.unlink(missing_ok=True)
    publication = {
        "schema": SCHEMA + "/publication",
        "revision": REVISION,
        "plan_id": plan["plan_id"],
        "instance_count": len(receipts),
        "manifest_content_sha": result["content_sha256"],
        "manifest_file_sha": result["manifest_file_sha256"],
        "comparison_ready": False,
    }
    stage5_hpc.write_once(
        output / "publication.json", stage5_hpc.canonical(publication) + b"\n"
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--team-model-manifest", required=True)
    plan.add_argument("--site", default=str(DEFAULT_SITE_CONFIG))
    plan.add_argument("--output-directory", required=True)
    init = sub.add_parser("init")
    init.add_argument("--plan", required=True)
    init.add_argument("--shared-root", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--plan", required=True)
    worker.add_argument("--shared-root", required=True)
    worker.add_argument("--instance-id", required=True)
    worker.add_argument("--node", choices=NODES, required=True)
    worker.add_argument("--attempt", type=int, required=True)
    reduce_parser = sub.add_parser("reduce")
    reduce_parser.add_argument("--plan", required=True)
    reduce_parser.add_argument("--shared-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        value = make_plan(
            team_model_manifest=args.team_model_manifest,
            site_path=args.site,
        )
        result = {
            "plan": str(save_plan(value, args.output_directory)),
            "plan_id": value["plan_id"],
        }
    elif args.command == "init":
        result = {
            "release": str(initialize_release(args.plan, shared_root=args.shared_root))
        }
    elif args.command == "worker":
        result = run_worker(
            plan_path=args.plan,
            shared_root=args.shared_root,
            instance_id=args.instance_id,
            node=args.node,
            attempt=args.attempt,
        )
    else:
        result = reduce_stage6(plan_path=args.plan, shared_root=args.shared_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
