"""Distributed rebuild of the fixed 84-instance External Stage-5 artifact.

``plan`` performs only small-file reads, partition stat checks, and six
read-only capacity probes. ``init`` hard-links the already staged Stage-4 files
into a content-addressed ``offline_data`` release tree. Each ``worker`` calls
the existing Stage-5 single-instance kernel exactly once. ``reduce`` requires
all receipts and lets the existing public Stage-5 builder author the final
manifest without rebuilding partitions.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import chronicle_external_api_manifest_union_v1 as union_v1
from . import chronicle_external_team_timeline_v2 as timeline_v2
from . import chronicle_external_team_wave_model_v2 as model_v2
from .hpc_environment_v1 import (
    DEFAULT_SITE_CONFIG,
    SchedulerNodeTransport,
    load_site_config,
)


SCHEMA = "chronicle_external_hpc_stage5/v1"
REVISION = "utk_84_instance_thin_v1"
NODES = tuple(f"node{index:03d}" for index in range(1, 7))
INSTANCE_COUNT = 84
MAX_ATTEMPTS = 3
SHARED_ROOT = "scheduleurm_work/o2o-dps-hpc"
STAGING_ROOT = (
    SHARED_ROOT
    + "/incoming/external-v2-stage4-"
    + "9eb360b13919cf59c7cd9f4b8d015ee6348d7c87b04e0330dcd9a0f52775a03e"
)
STAGED_TIMELINE = "timeline/utk_postfix_dev_20260903_noon"
CANONICAL_TIMELINE = (
    "offline_data/derived/chronicle_external_team_timeline/v2/"
    "utk_postfix_dev_20260903_noon"
)
CANONICAL_COHORT = (
    "offline_data/derived/chronicle_external_api_manifest_union/v1/manifests"
)
CANONICAL_STAGE5 = (
    "offline_data/derived/chronicle_external_team_wave_model/v2/"
    "utk_postfix_dev_20260903_noon"
)
STAGE4_CONTENT_SHA = (
    "9eb360b13919cf59c7cd9f4b8d015ee6348d7c87b04e0330dcd9a0f52775a03e"
)
STAGE4_FILE_SHA = (
    "d3fa9d871695727bedc5c228cc77c1d4a64dc3c50a620443d4b02d30921f0a6b"
)


class Stage5HpcError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path_value: str | Path) -> tuple[Path, dict[str, Any], bytes]:
    path = Path(path_value).expanduser().resolve()
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict) or payload != canonical(value) + b"\n":
        raise Stage5HpcError(f"not canonical JSON plus LF: {path}")
    return path, value, payload


def plan_id(plan: Mapping[str, Any]) -> str:
    core = dict(plan)
    observed = core.pop("plan_id", None)
    expected = hashlib.sha256(canonical(core)).hexdigest()
    if observed != expected:
        raise Stage5HpcError("plan identity differs")
    return expected


def write_once(path: Path, payload: bytes) -> str:
    """Resume identical output and reject a divergent duplicate."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise Stage5HpcError(f"divergent duplicate: {path}")
        return "RESUMED"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise Stage5HpcError(f"divergent duplicate: {path}")
            return "RESUMED"
        return "PUBLISHED"
    finally:
        temporary.unlink(missing_ok=True)


def _cohort(receipt: Mapping[str, Any]) -> tuple[list[str], set[str], dict[str, str]]:
    union_v1._verify_content_address(receipt, label="cohort receipt")
    cohorts = receipt["cohorts"]
    descriptive = list(cohorts["descriptive"]["instance_ids"])
    training = set(cohorts["training"]["instance_ids"])
    nontraining = set(cohorts["descriptive_nontraining"]["instance_ids"])
    reasons = {
        row["instance_id"]: row["reason"]
        for row in cohorts["descriptive_nontraining"]["reasons"]
    }
    if (
        descriptive != sorted(set(descriptive))
        or training & nontraining
        or training | nontraining != set(descriptive)
        or set(reasons) != nontraining
    ):
        raise Stage5HpcError("cohort membership differs")
    return descriptive, training, reasons


def _probe_rows(site_path: str | Path) -> list[dict[str, Any]]:
    site = load_site_config(Path(site_path))
    if (
        site.control_ssh_alias != "jtl110gpu2"
        or site.node_transport_kind != "scheduler_run_on"
        or tuple(node.name for node in site.nodes) != NODES
    ):
        raise Stage5HpcError("unexpected HPC route")
    command = "\n".join(
        (
            "set -eu",
            "export LC_ALL=C",
            "printf 'hostname='; hostname -s",
            "printf 'logical_cpus='; nproc",
            "printf 'load1='; cut -d' ' -f1 /proc/loadavg",
            "printf 'mem_total_kib='; awk '/^MemTotal:/ {print $2}' /proc/meminfo",
            "printf 'mem_available_kib='; awk '/^MemAvailable:/ {print $2}' /proc/meminfo",
        )
    )
    transport = SchedulerNodeTransport(site)

    def one(node: Any) -> dict[str, Any]:
        result = transport.run(node, command, timeout=30)
        values = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
        if result.returncode or values.get("hostname") != node.name:
            raise Stage5HpcError(f"capacity probe failed on {node.name}")
        return {
            "node": node.name,
            "logical_cpus": int(values["logical_cpus"]),
            "load1": float(values["load1"]),
            "mem_total_kib": int(values["mem_total_kib"]),
            "mem_available_kib": int(values["mem_available_kib"]),
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, site.nodes))


def capacity_slots(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Use only currently idle CPU/RAM after fixed safety reserves."""

    by_node = {str(row["node"]): row for row in rows}
    if tuple(sorted(by_node)) != NODES:
        raise Stage5HpcError("capacity must cover node001--node006")
    result = {}
    for node in NODES:
        row = by_node[node]
        cpus = int(row["logical_cpus"])
        load = float(row["load1"])
        total_mib = int(row["mem_total_kib"]) // 1024
        available_mib = int(row["mem_available_kib"]) // 1024
        cpu_slots = max(0, math.floor(cpus - load) - max(8, math.ceil(cpus * 0.15)))
        memory_slots = max(
            0, (available_mib - max(32768, math.ceil(total_mib * 0.20))) // 8192
        )
        result[node] = min(cpu_slots, memory_slots, 160)
    if any(value < 1 for value in result.values()) or sum(result.values()) < INSTANCE_COUNT:
        raise Stage5HpcError("current free capacity is insufficient")
    return result


def _allocate(instance_ids: Sequence[str], slots: Mapping[str, int]) -> list[dict[str, Any]]:
    assigned = {node: 0 for node in NODES}
    shards = []
    for instance_id in instance_ids:
        available = [node for node in NODES if assigned[node] < slots[node]]
        primary = min(available, key=lambda node: (assigned[node] / slots[node], node))
        assigned[primary] += 1
        start = NODES.index(primary)
        shards.append(
            {
                "instance_id": instance_id,
                "primary": primary,
                "retry_nodes": [
                    NODES[(start + offset) % len(NODES)]
                    for offset in range(MAX_ATTEMPTS)
                ],
            }
        )
    return shards


def make_plan(
    *,
    timeline_manifest: str | Path,
    cohort_receipt: str | Path,
    cohort_staged_name: str,
    capacity_rows: Sequence[Mapping[str, Any]] | None = None,
    site_path: str | Path = DEFAULT_SITE_CONFIG,
    expected_instances: int = INSTANCE_COUNT,
    expected_content_sha: str = STAGE4_CONTENT_SHA,
    expected_file_sha: str = STAGE4_FILE_SHA,
    staging_root: str = STAGING_ROOT,
    project_root: str | Path = Path(__file__).resolve().parents[1],
) -> dict[str, Any]:
    """Create the plan without opening any Stage-4 gzip partition."""

    timeline_path, timeline, payload = read_json(timeline_manifest)
    content = model_v2._verify_content_address(timeline, label="Stage-4 manifest")
    if (
        timeline.get("schema") != timeline_v2.SCHEMA
        or timeline.get("implementation_revision") != timeline_v2.IMPLEMENTATION_REVISION
        or content != expected_content_sha
        or hashlib.sha256(payload).hexdigest() != expected_file_sha
    ):
        raise Stage5HpcError("Stage-4 manifest identity differs")
    entries = timeline["instances"]
    order = timeline["instance_order"]
    if len(entries) != expected_instances or [row["instance_id"] for row in entries] != order:
        raise Stage5HpcError("Stage-4 instance set/order differs")
    for row in entries:
        path = timeline_path.parent / row["partition"]["path"]
        if not path.is_file() or path.stat().st_size != row["partition"]["compressed_size_bytes"]:
            raise Stage5HpcError(f"missing/truncated Stage-4 partition: {path.name}")
    _, receipt, receipt_payload = read_json(cohort_receipt)
    descriptive, training, reasons = _cohort(receipt)
    if descriptive != order:
        raise Stage5HpcError("cohort descriptive IDs differ from Stage 4")
    slots = capacity_slots(capacity_rows or _probe_rows(site_path))
    shards = _allocate(order, slots)
    by_id = {row["instance_id"]: row for row in entries}
    for shard in shards:
        row = by_id[shard["instance_id"]]
        shard.update(
            {
                "entry_sha": model_v2._verify_content_address(
                    row, label="Stage-4 instance"
                ),
                "source_binding_sha": hashlib.sha256(
                    canonical(row["source_binding"])
                ).hexdigest(),
                "contamination": model_v2._contamination(
                    row["instance_provenance"],
                    instance_id=shard["instance_id"],
                    receipt_content_sha256=receipt["content_address"]["sha256"],
                    training_instance_ids=frozenset(training),
                    nontraining_reason_by_id=reasons,
                ),
            }
        )
    root = Path(project_root).resolve()
    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": "PLANNED_NOT_RUN",
        "shared_root": SHARED_ROOT,
        "staging_root": staging_root,
        "cohort_staged_name": cohort_staged_name,
        "stage4": {
            "content_sha": content,
            "file_sha": expected_file_sha,
            "partition_count": len(entries),
            "compressed_bytes": sum(
                row["partition"]["compressed_size_bytes"] for row in entries
            ),
        },
        "cohort": {
            "content_sha": receipt["content_address"]["sha256"],
            "file_sha": hashlib.sha256(receipt_payload).hexdigest(),
        },
        "implementation_sha": {
            "stage5": file_sha(
                root / "o2o_dps/chronicle_external_team_wave_model_v2.py"
            ),
            "orchestrator": file_sha(Path(__file__).resolve()),
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
            "cohort_receipt": f"cohort/{cohort_staged_name}",
            "stage4_manifest_and_partitions": STAGED_TIMELINE,
            "raw_normalized_or_stage1_to_3": None,
        },
        "comparison_ready": False,
    }
    return {**core, "plan_id": hashlib.sha256(canonical(core)).hexdigest()}


def save_plan(plan: Mapping[str, Any], directory: str | Path) -> Path:
    identity = plan_id(plan)
    path = Path(directory).resolve() / f"stage5_hpc_plan.{identity}.json"
    write_once(path, canonical(plan) + b"\n")
    return path


def load_plan(path_value: str | Path) -> tuple[Path, dict[str, Any]]:
    path, plan, _ = read_json(path_value)
    if plan.get("schema") != SCHEMA or plan.get("revision") != REVISION:
        raise Stage5HpcError("unsupported plan")
    plan_id(plan)
    if plan.get("transfer", {}).get("raw_normalized_or_stage1_to_3") is not None:
        raise Stage5HpcError("plan widens the transfer boundary")
    return path, plan


def _hardlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not os.path.samefile(source, destination):
            raise Stage5HpcError(f"release link already differs: {destination}")
        return
    os.link(source, destination)


def initialize_release(
    plan_path: str | Path, *, shared_root: str | Path, staging_root: str | Path
) -> Path:
    """Create the standard offline_data tree with zero-copy hard links."""

    _, plan = load_plan(plan_path)
    shared = Path(shared_root).expanduser().resolve()
    staging = Path(staging_root).expanduser().resolve()
    release = shared / "releases" / plan["plan_id"]
    release.mkdir(parents=True, exist_ok=True)
    write_once(release / "plan.json", canonical(plan) + b"\n")
    source_timeline = staging / STAGED_TIMELINE
    destination_timeline = shared / CANONICAL_TIMELINE
    for source in source_timeline.iterdir():
        if source.is_file() and (source.name.endswith(".jsonl.gz") or source.name.endswith(".json")):
            _hardlink(source, destination_timeline / source.name)
    source_cohort = staging / "cohort" / plan["cohort_staged_name"]
    _hardlink(source_cohort, shared / CANONICAL_COHORT / source_cohort.name)
    (shared / CANONICAL_STAGE5 / ".hpc" / plan["plan_id"] / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    return release


def _release(plan: Mapping[str, Any], shared_root: str | Path) -> Path:
    return Path(shared_root).expanduser().resolve() / "releases" / plan["plan_id"]


def _shard(plan: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    matches = [row for row in plan["shards"] if row["instance_id"] == instance_id]
    if len(matches) != 1:
        raise Stage5HpcError("instance is absent or duplicated in plan")
    return matches[0]


def _live_code_matches(plan: Mapping[str, Any]) -> None:
    root = Path(__file__).resolve().parents[1]
    observed = {
        "stage5": file_sha(root / "o2o_dps/chronicle_external_team_wave_model_v2.py"),
        "orchestrator": file_sha(Path(__file__).resolve()),
    }
    if observed != plan["implementation_sha"]:
        raise Stage5HpcError("implementation differs from plan")


def run_worker(
    *,
    plan_path: str | Path,
    shared_root: str | Path,
    instance_id: str,
    node: str,
    attempt: int,
) -> dict[str, Any]:
    """Build one instance; the frozen kernel verifies compressed and logical input."""

    _, plan = load_plan(plan_path)
    shard = _shard(plan, instance_id)
    if not 1 <= attempt <= MAX_ATTEMPTS or shard["retry_nodes"][attempt - 1] != node:
        raise Stage5HpcError("worker node/attempt differs from plan")
    _live_code_matches(plan)
    release = _release(plan, shared_root)
    if (release / "plan.json").read_bytes() != canonical(plan) + b"\n":
        raise Stage5HpcError("release plan differs")
    shared = Path(shared_root).expanduser().resolve()
    output = shared / CANONICAL_STAGE5
    receipt_path = output / ".hpc" / plan["plan_id"] / "receipts" / f"{instance_id}.json"
    if receipt_path.exists():
        _, receipt, _ = read_json(receipt_path)
        partition = receipt.get("partition", {})
        final = output / str(partition.get("path", ""))
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != instance_id
            or not final.is_file()
            or file_sha(final) != partition.get("compressed_sha")
        ):
            raise Stage5HpcError("existing receipt/output differs")
        return {"status": "RESUMED", "receipt": str(receipt_path)}
    timeline_path, timeline, _ = read_json(shared / CANONICAL_TIMELINE / "manifest.json")
    _, cohort, _ = read_json(
        shared / CANONICAL_COHORT / plan["cohort_staged_name"]
    )
    _, training, reasons = _cohort(cohort)
    entry = next(row for row in timeline["instances"] if row["instance_id"] == instance_id)
    if model_v2._verify_content_address(entry, label="Stage-4 instance") != shard["entry_sha"]:
        raise Stage5HpcError("Stage-4 instance entry differs from plan")
    context = model_v2.InputInstance(
        index=timeline["instance_order"].index(instance_id),
        instance_id=instance_id,
        entry=deepcopy(entry),
        partition_path=timeline_path.parent / entry["partition"]["path"],
        provenance=deepcopy(entry["instance_provenance"]),
        contamination=deepcopy(shard["contamination"]),
        source_binding_sha256=shard["source_binding_sha"],
    )
    built = model_v2._build_partition(context, output_directory=output)
    if built.final_path.exists():
        if file_sha(built.final_path) != built.compressed_file_sha256:
            built.temporary_path.unlink(missing_ok=True)
            raise Stage5HpcError("divergent duplicate partition")
        built.temporary_path.unlink(missing_ok=True)
        publication = "RESUMED"
    else:
        try:
            os.link(built.temporary_path, built.final_path)
            publication = "PUBLISHED"
        except FileExistsError:
            if file_sha(built.final_path) != built.compressed_file_sha256:
                raise Stage5HpcError("divergent duplicate partition")
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
        "component_nodes": list(built.component_nodes),
        "component_edges": list(built.component_edges),
    }
    status = write_once(receipt_path, canonical(receipt) + b"\n")
    return {"status": status, "receipt": str(receipt_path)}


def _cohort_binding(
    data_root: Path, path: Path, receipt: Mapping[str, Any], payload: bytes
) -> dict[str, Any]:
    descriptive, training, reasons = _cohort(receipt)
    cohorts, raw = receipt["cohorts"], receipt["raw_union"]
    return {
        "content_addressed_path": path.relative_to(data_root).as_posix(),
        "schema": union_v1.SCHEMA,
        "kind": union_v1.KIND,
        "implementation_revision": union_v1.IMPLEMENTATION_REVISION,
        "content_sha256": receipt["content_address"]["sha256"],
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "raw_union_file_sha256": raw["file_sha256"],
        "raw_union_path": raw["path"],
        "raw_union_size_bytes": raw["size_bytes"],
        "descriptive_instance_count": len(descriptive),
        "descriptive_instance_ids_sha256": cohorts["descriptive"]["instance_ids_sha256"],
        "training_instance_count": len(training),
        "training_instance_ids_sha256": cohorts["training"]["instance_ids_sha256"],
        "descriptive_nontraining_instance_count": len(reasons),
        "descriptive_nontraining_instance_ids_sha256": cohorts[
            "descriptive_nontraining"
        ]["instance_ids_sha256"],
        "audit_status": "PASS_STRICT_FULL_SOURCE_REPLAY",
    }


def reduce_stage5(
    *, plan_path: str | Path, shared_root: str | Path
) -> dict[str, Any]:
    """Require 84 exact receipts, then reuse the frozen manifest publisher."""

    _, plan = load_plan(plan_path)
    _live_code_matches(plan)
    release = _release(plan, shared_root)
    shared = Path(shared_root).expanduser().resolve()
    data_root = shared / "offline_data"
    output = shared / CANONICAL_STAGE5
    receipts_directory = output / ".hpc" / plan["plan_id"] / "receipts"
    actual = sorted(path.name for path in receipts_directory.glob("*.json"))
    expected = sorted(f"{row['instance_id']}.json" for row in plan["shards"])
    if actual != expected:
        raise Stage5HpcError("receipt set is incomplete or unexpected")
    receipts = []
    for shard in plan["shards"]:
        _, receipt, _ = read_json(receipts_directory / f"{shard['instance_id']}.json")
        if (
            receipt.get("plan_id") != plan["plan_id"]
            or receipt.get("instance_id") != shard["instance_id"]
            or file_sha(output / receipt["partition"]["path"])
            != receipt["partition"]["compressed_sha"]
        ):
            raise Stage5HpcError("receipt/output identity differs")
        receipts.append(receipt)
    timeline_path, timeline, timeline_payload = read_json(
        shared / CANONICAL_TIMELINE / "manifest.json"
    )
    timeline_content = model_v2._verify_content_address(
        timeline, label="Stage-4 manifest"
    )
    addressed_timeline = timeline_path.with_name(
        f"chronicle_external_team_timeline_v2.{timeline_content}.manifest.json"
    )
    if addressed_timeline.read_bytes() != timeline_payload:
        raise Stage5HpcError("Stage-4 stable/addressed manifests differ")
    cohort_path, cohort, cohort_payload = read_json(
        shared / CANONICAL_COHORT / plan["cohort_staged_name"]
    )
    _, training, reasons = _cohort(cohort)
    contexts = tuple(
        model_v2.InputInstance(
            index=index,
            instance_id=row["instance_id"],
            entry=deepcopy(row),
            partition_path=timeline_path.parent / row["partition"]["path"],
            provenance=deepcopy(row["instance_provenance"]),
            contamination=deepcopy(plan["shards"][index]["contamination"]),
            source_binding_sha256=plan["shards"][index]["source_binding_sha"],
        )
        for index, row in enumerate(timeline["instances"])
    )
    closure = model_v2.InputClosure(
        manifest=timeline,
        manifest_path=timeline_path,
        addressed_path=addressed_timeline,
        data_root=data_root,
        content_sha256=timeline_content,
        file_sha256=hashlib.sha256(timeline_payload).hexdigest(),
        instances=contexts,
        cohort_receipt=cohort,
        cohort_receipt_path=cohort_path,
        cohort_receipt_binding=_cohort_binding(
            data_root, cohort_path, cohort, cohort_payload
        ),
        training_instance_ids=frozenset(training),
        descriptive_nontraining_instance_ids=frozenset(reasons),
    )
    builds = {}
    for receipt in receipts:
        final = output / receipt["partition"]["path"]
        descriptor, name = tempfile.mkstemp(prefix=f".{final.name}.", dir=output)
        os.close(descriptor)
        temporary = Path(name)
        temporary.unlink()
        os.link(final, temporary)
        builds[receipt["instance_id"]] = model_v2.PartitionBuild(
            temporary_path=temporary,
            final_path=final,
            compressed_file_sha256=receipt["partition"]["compressed_sha"],
            manifest_entry=receipt["manifest_entry"],
            component_nodes=tuple(tuple(row) for row in receipt["component_nodes"]),
            component_edges=tuple(tuple(row) for row in receipt["component_edges"]),
        )
    original_load = model_v2._load_input_closure
    original_build = model_v2._build_partition
    try:
        model_v2._load_input_closure = lambda *_args, **_kwargs: closure
        model_v2._build_partition = lambda context, **_kwargs: builds[
            context.instance_id
        ]
        result = model_v2.build_external_team_wave_model(
            timeline_manifest_path=timeline_path,
            cohort_receipt_path=cohort_path,
            output_directory=output,
            workers=1,
        )
    finally:
        model_v2._load_input_closure = original_load
        model_v2._build_partition = original_build
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
    write_once(output / "publication.json", canonical(publication) + b"\n")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--timeline-manifest", required=True)
    plan.add_argument("--cohort-receipt", required=True)
    plan.add_argument("--cohort-staged-name", required=True)
    plan.add_argument("--site", default=str(DEFAULT_SITE_CONFIG))
    plan.add_argument("--output-directory", required=True)
    init = sub.add_parser("init")
    init.add_argument("--plan", required=True)
    init.add_argument("--shared-root", required=True)
    init.add_argument("--staging-root", required=True)
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
            timeline_manifest=args.timeline_manifest,
            cohort_receipt=args.cohort_receipt,
            cohort_staged_name=args.cohort_staged_name,
            site_path=args.site,
        )
        result = {
            "plan": str(save_plan(value, args.output_directory)),
            "plan_id": value["plan_id"],
        }
    elif args.command == "init":
        result = {
            "release": str(
                initialize_release(
                    args.plan,
                    shared_root=args.shared_root,
                    staging_root=args.staging_root,
                )
            )
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
        result = reduce_stage5(plan_path=args.plan, shared_root=args.shared_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
