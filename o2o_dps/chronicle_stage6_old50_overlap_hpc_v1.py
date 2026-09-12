"""Exact-key Stage-6 to old-50 target-capsule overlap audit.

The audit is deliberately descriptive and non-voting.  A worker opens exactly
one Stage-6 gzip partition once, validates every row while streaming it, and
only retains compact join evidence for exact instance/encounter/wave candidates.
The old CSV and External-v2 builders use different, validated ``wave_id``
namespaces, so their common semantic key is the exact instance ID, exact
encounter ID, and wave ordinal encoded by both wave IDs.  An ordinal by itself
is never a join key.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gzip
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Mapping, Sequence

from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_wave_model_v2 as model_v2
from . import fury_offline_scenario_capsule_v2 as capsule_v2


SCHEMA = "chronicle_stage6_old50_overlap_hpc/v1"
REVISION = "exact_wave_guid_time_component_audit_v1_1"
PLAN_SCHEMA = f"{SCHEMA}/plan"
RECEIPT_SCHEMA = f"{SCHEMA}/worker_receipt"
AUDIT_SCHEMA = f"{SCHEMA}/compact_audit"
COMPILER_INPUT_SCHEMA = "chronicle_stage6_old50_dynamic_v3_compiler_input/v1"
CLASSIFICATIONS = ("EXACT", "SUBSET", "REJECT")
NODES = tuple(f"node{index:03d}" for index in range(1, 7))


class Stage6Old50OverlapError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Stage6Old50OverlapError(f"value is not canonical JSON: {error}") from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(core))
    value["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _sha256_bytes(_canonical(core)),
    }
    return value


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = value.get("content_address")
    if not isinstance(address, Mapping):
        raise Stage6Old50OverlapError(f"{label} lacks a content address")
    observed = address.get("sha256")
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    expected = _sha256_bytes(_canonical(core))
    if observed != expected:
        raise Stage6Old50OverlapError(f"{label} content address differs")
    return expected


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage6Old50OverlapError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise Stage6Old50OverlapError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Stage6Old50OverlapError(f"{label} must be nonempty text")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Stage6Old50OverlapError(f"{label} must be an integer")
    return value


def _relative(path: Path, root: Path, *, label: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(root.expanduser().resolve()).as_posix()
    except ValueError as error:
        raise Stage6Old50OverlapError(f"{label} is outside shared root") from error


def _resolve(root: Path, locator: Any, *, label: str) -> Path:
    text = _text(locator, label=label)
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise Stage6Old50OverlapError(f"{label} must be a safe relative path")
    candidate = root.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise Stage6Old50OverlapError(f"{label} escapes shared root") from error
    return candidate


def _read_canonical_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Stage6Old50OverlapError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict) or payload != _canonical(value) + b"\n":
        raise Stage6Old50OverlapError(f"{label} is not canonical JSON plus LF")
    return value, payload


def _load_capsule(path: Path) -> tuple[dict[str, Any], str, str]:
    file_sha = _sha256_file(path)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Stage6Old50OverlapError(f"could not load old-50 capsule: {error}") from error
    if not isinstance(value, dict):
        raise Stage6Old50OverlapError("old-50 capsule must be a JSON object")
    capsule_v2.validate_scenario_capsule_bundle_v2(value)
    content_sha = _verify_content_address(value, label="old-50 capsule")
    return value, content_sha, file_sha


def _load_stage6_manifest(path: Path) -> tuple[dict[str, Any], str, str, bytes]:
    manifest, payload = _read_canonical_json(path, label="Stage-6 manifest")
    content_sha = background_v2._verify_content_address(
        manifest, label="Stage-6 manifest"
    )
    if (
        manifest.get("schema") != background_v2.SCHEMA
        or manifest.get("implementation_revision")
        != background_v2.IMPLEMENTATION_REVISION
        or manifest.get("status") != background_v2.STATUS
    ):
        raise Stage6Old50OverlapError("input is not the current Stage-6 artifact")
    addressed = path.with_name(
        f"chronicle_external_team_background_generator_v2.{content_sha}.manifest.json"
    )
    if not addressed.is_file() or addressed.read_bytes() != payload:
        raise Stage6Old50OverlapError("Stage-6 stable/addressed manifests differ")
    instances = _array(manifest.get("instances"), label="Stage-6 instances")
    order = _array(manifest.get("instance_order"), label="Stage-6 instance_order")
    if (
        not instances
        or len(instances) != len(order)
        or [row.get("instance_id") for row in instances] != order
        or order != sorted(set(order))
    ):
        raise Stage6Old50OverlapError("Stage-6 instance order/set differs")
    for raw in instances:
        background_v2._verify_content_address(
            _mapping(raw, label="Stage-6 instance entry"),
            label="Stage-6 instance entry",
        )
    return manifest, content_sha, _sha256_bytes(payload), payload


def _load_stage5_manifest(path: Path) -> tuple[dict[str, Any], str, str, bytes]:
    manifest, payload = _read_canonical_json(path, label="Stage-5 manifest")
    content_sha = model_v2._verify_content_address(manifest, label="Stage-5 manifest")
    if (
        manifest.get("schema") != model_v2.SCHEMA
        or manifest.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or manifest.get("status") != model_v2.STATUS
    ):
        raise Stage6Old50OverlapError("input is not the current Stage-5 artifact")
    addressed = path.with_name(
        f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
    )
    if not addressed.is_file() or addressed.read_bytes() != payload:
        raise Stage6Old50OverlapError("Stage-5 stable/addressed manifests differ")
    instances = _array(manifest.get("instances"), label="Stage-5 instances")
    order = _array(manifest.get("instance_order"), label="Stage-5 instance_order")
    if (
        not instances
        or len(instances) != len(order)
        or [row.get("instance_id") for row in instances] != order
        or order != sorted(set(order))
    ):
        raise Stage6Old50OverlapError("Stage-5 instance order/set differs")
    for raw in instances:
        model_v2._verify_content_address(
            _mapping(raw, label="Stage-5 instance entry"),
            label="Stage-5 instance entry",
        )
    return manifest, content_sha, _sha256_bytes(payload), payload


def _wave_key(identity: Mapping[str, Any]) -> tuple[str, str, int]:
    instance_id = _text(identity.get("instance_id"), label="wave instance_id")
    encounter_id = _text(identity.get("encounter_id"), label="wave encounter_id")
    ordinal = _integer(identity.get("wave_ordinal"), label="wave wave_ordinal")
    if ordinal < 0:
        raise Stage6Old50OverlapError("wave ordinal must be nonnegative")
    wave_id = identity.get("wave_id")
    if wave_id is not None and wave_id not in {
        f"{encounter_id}:wave:{ordinal}",
        f"{encounter_id}:external-v2-wave:{ordinal}",
    }:
        raise Stage6Old50OverlapError(
            "wave_id does not encode its exact encounter and wave ordinal"
        )
    return instance_id, encounter_id, ordinal


def _key_object(key: tuple[str, str, int]) -> dict[str, Any]:
    return {
        "instance_id": key[0],
        "encounter_id": key[1],
        "wave_ordinal": key[2],
    }


def _capsule_indexes(
    capsule: Mapping[str, Any],
) -> tuple[
    dict[tuple[str, str, int], Mapping[str, Any]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    by_key: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    instances_by_component: dict[str, set[str]] = defaultdict(set)
    components_by_instance: dict[str, set[str]] = defaultdict(set)
    for raw in _array(capsule.get("scenarios"), label="capsule scenarios"):
        scenario = _mapping(raw, label="capsule scenario")
        identity = _mapping(scenario.get("source_identity"), label="source_identity")
        key = _wave_key(identity)
        if key in by_key:
            raise Stage6Old50OverlapError("capsule has duplicate exact wave key")
        by_key[key] = scenario
        projection = _mapping(
            scenario.get("runner_projection"), label="runner_projection"
        )
        component = _text(
            projection.get("component_id"), label="capsule component_id"
        )
        instances_by_component[component].add(key[0])
        components_by_instance[key[0]].add(component)
    return by_key, instances_by_component, components_by_instance


def _stage6_descriptor_indexes(
    manifest: Mapping[str, Any],
) -> tuple[
    dict[tuple[str, str, int], Mapping[str, Any]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    by_key: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    instances_by_component: dict[str, set[str]] = defaultdict(set)
    components_by_instance: dict[str, set[str]] = defaultdict(set)
    for raw_pool in _array(manifest.get("component_pools"), label="component pools"):
        pool = _mapping(raw_pool, label="component pool")
        component = _text(pool.get("component_id"), label="Stage-6 component_id")
        blocks = _array(pool.get("blocks"), label="component pool blocks")
        if pool.get("block_count") != len(blocks):
            raise Stage6Old50OverlapError("Stage-6 component block count differs")
        for raw in blocks:
            descriptor = _mapping(raw, label="Stage-6 block descriptor")
            identity = _mapping(descriptor.get("wave"), label="descriptor wave")
            key = _wave_key(identity)
            if key in by_key or descriptor.get("component_id") != component:
                raise Stage6Old50OverlapError(
                    "Stage-6 descriptors duplicate a wave or cross components"
                )
            by_key[key] = descriptor
            instances_by_component[component].add(key[0])
            components_by_instance[key[0]].add(component)
    expected_blocks = _mapping(
        manifest.get("summary"), label="Stage-6 summary"
    ).get("block_count")
    if expected_blocks != len(by_key):
        raise Stage6Old50OverlapError("Stage-6 descriptor coverage differs")
    return by_key, instances_by_component, components_by_instance


def _component_relation(
    *,
    old_component: str,
    stage6_component: str,
    common_instances: set[str],
    old_instances_by_component: Mapping[str, set[str]],
    stage6_instances_by_component: Mapping[str, set[str]],
) -> dict[str, Any]:
    old_projection = sorted(old_instances_by_component[old_component] & common_instances)
    stage6_projection = sorted(
        stage6_instances_by_component[stage6_component] & common_instances
    )
    if old_projection == stage6_projection:
        relation = "OVERLAP_PROJECTION_EXACT"
        split_authority = "EQUIVALENT_ON_OVERLAP"
    elif set(old_projection) < set(stage6_projection):
        # Stage-6 was built over the wider 84-instance corpus.  Extra identity
        # edges can conservatively merge an old-50 leakage component; adopting
        # that coarser component keeps the split safe.  It must not be treated
        # as evidence that the exact historical wave changed.
        relation = "OLD50_PROJECTION_STRICT_SUBSET_STAGE6_AUTHORITY"
        split_authority = "STAGE6_COARSER_COMPONENT"
    else:
        relation = "OVERLAP_PROJECTION_CONFLICT"
        split_authority = "NONE_REJECT"
    return {
        "old50_component_id": old_component,
        "stage6_component_id": stage6_component,
        "comparison_universe": "20_INSTANCE_INTERSECTION",
        "old50_equivalence_class": old_projection,
        "stage6_equivalence_class": stage6_projection,
        "relation": relation,
        "split_authority": split_authority,
        "raw_component_id_equality_required": False,
    }


def _lpt_assign(
    rows: Sequence[Mapping[str, Any]], capacities: Mapping[str, int]
) -> dict[str, str]:
    if tuple(sorted(capacities)) != NODES or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in capacities.values()
    ):
        raise Stage6Old50OverlapError("capacities must cover six nodes positively")
    loads = {node: 0 for node in NODES}
    counts = {node: 0 for node in NODES}
    result: dict[str, str] = {}
    ordered = sorted(
        rows,
        key=lambda row: (-int(row["weight_bytes"]), str(row["instance_id"])),
    )
    for row in ordered:
        node = min(
            NODES,
            key=lambda candidate: (
                loads[candidate] / capacities[candidate],
                counts[candidate],
                candidate,
            ),
        )
        instance_id = str(row["instance_id"])
        result[instance_id] = node
        loads[node] += int(row["weight_bytes"])
        counts[node] += 1
    return result


def make_plan(
    *,
    stage6_manifest: str | Path,
    capsule: str | Path,
    shared_root: str | Path,
    stage5_manifest: str | Path | None = None,
    capacities: Mapping[str, int] | None = None,
    expected_common_instances: int | None = None,
    expected_candidate_keys: int | None = None,
    stage6_locator: str | None = None,
    capsule_locator: str | None = None,
) -> dict[str, Any]:
    """Bind both compact inputs and create a six-node LPT map plan."""

    root = Path(shared_root).expanduser().resolve()
    stage6_path = Path(stage6_manifest).expanduser().resolve()
    capsule_path = Path(capsule).expanduser().resolve()
    stage6, stage6_content_sha, stage6_file_sha, stage6_payload = (
        _load_stage6_manifest(stage6_path)
    )
    stage5_binding = _mapping(
        _mapping(stage6.get("input_closure"), label="Stage-6 input_closure").get(
            "team_model_manifest"
        ),
        label="Stage-6 Stage-5 binding",
    )
    inferred_stage5 = root / "offline_data" / PurePosixPath(
        _text(stage5_binding.get("stable_path"), label="Stage-5 stable_path")
    )
    stage5_path = (
        Path(stage5_manifest).expanduser().resolve()
        if stage5_manifest is not None
        else inferred_stage5.resolve()
    )
    stage5, stage5_content_sha, stage5_file_sha, stage5_payload = (
        _load_stage5_manifest(stage5_path)
    )
    if (
        stage5_content_sha != stage5_binding.get("content_sha256")
        or stage5_file_sha != stage5_binding.get("file_sha256")
        or len(stage5_payload) != stage5_binding.get("size_bytes")
    ):
        raise Stage6Old50OverlapError("Stage-5 manifest differs from Stage-6 closure")
    bundle, capsule_content_sha, capsule_file_sha = _load_capsule(capsule_path)
    capsule_by_key, old_by_component, old_components_by_instance = _capsule_indexes(
        bundle
    )
    stage6_by_key, stage6_by_component, stage6_components_by_instance = (
        _stage6_descriptor_indexes(stage6)
    )
    old_instances = {key[0] for key in capsule_by_key}
    stage6_instances = set(stage6["instance_order"])
    common_instances = old_instances & stage6_instances
    candidate_keys = set(capsule_by_key) & set(stage6_by_key)
    if expected_common_instances is not None and len(common_instances) != expected_common_instances:
        raise Stage6Old50OverlapError(
            f"common instance count differs: {len(common_instances)}"
        )
    if expected_candidate_keys is not None and len(candidate_keys) != expected_candidate_keys:
        raise Stage6Old50OverlapError(
            f"candidate exact-key count differs: {len(candidate_keys)}"
        )
    entries = {str(row["instance_id"]): row for row in stage6["instances"]}
    stage5_entries = {str(row["instance_id"]): row for row in stage5["instances"]}
    if set(entries) != set(stage5_entries):
        raise Stage6Old50OverlapError("Stage-5 and Stage-6 instance sets differ")
    stage6_base_locator = (
        _text(stage6_locator, label="Stage-6 locator")
        if stage6_locator is not None
        else _relative(stage6_path, root, label="Stage-6 manifest")
    )
    capsule_input_locator = (
        _text(capsule_locator, label="capsule locator")
        if capsule_locator is not None
        else _relative(capsule_path, root, label="capsule")
    )
    stage5_input_locator = _relative(stage5_path, root, label="Stage-5 manifest")
    stage6_parent = PurePosixPath(stage6_base_locator).parent
    stage5_parent = PurePosixPath(stage5_input_locator).parent
    work: list[dict[str, Any]] = []
    for instance_id in sorted(common_instances):
        entry = _mapping(entries[instance_id], label="Stage-6 instance entry")
        stage5_entry = _mapping(
            stage5_entries[instance_id], label="Stage-5 instance entry"
        )
        partition = _mapping(entry.get("partition"), label="Stage-6 partition")
        stage5_partition = _mapping(
            stage5_entry.get("partition"), label="Stage-5 partition"
        )
        partition_name = _text(partition.get("path"), label="partition path")
        if PurePosixPath(partition_name).name != partition_name:
            raise Stage6Old50OverlapError("Stage-6 partition must be colocated with manifest")
        stage5_partition_name = _text(
            stage5_partition.get("path"), label="Stage-5 partition path"
        )
        if PurePosixPath(stage5_partition_name).name != stage5_partition_name:
            raise Stage6Old50OverlapError("Stage-5 partition must be colocated with manifest")
        keys = sorted(key for key in candidate_keys if key[0] == instance_id)
        candidates = []
        for key in keys:
            scenario = capsule_by_key[key]
            descriptor = stage6_by_key[key]
            old_component = _text(
                _mapping(
                    scenario.get("runner_projection"), label="runner_projection"
                ).get("component_id"),
                label="old-50 component",
            )
            stage6_component = _text(
                descriptor.get("component_id"), label="Stage-6 component"
            )
            candidates.append(
                {
                    "key": _key_object(key),
                    "scenario_id": scenario.get("scenario_id"),
                    "capsule_wave_id": _mapping(
                        scenario.get("source_identity"), label="source_identity"
                    ).get("wave_id"),
                    "stage6_wave_id": _mapping(
                        descriptor.get("wave"), label="descriptor wave"
                    ).get("wave_id"),
                    "capsule_sha256": scenario.get("capsule_sha256"),
                    "capsule_wave_ordinal": _mapping(
                        scenario.get("source_identity"), label="source_identity"
                    ).get("wave_ordinal"),
                    "stage6_wave_ordinal": _mapping(
                        descriptor.get("wave"), label="descriptor wave"
                    ).get("wave_ordinal"),
                    "stage6_block_content_sha256": descriptor.get(
                        "block_content_sha256"
                    ),
                    "component_relation": _component_relation(
                        old_component=old_component,
                        stage6_component=stage6_component,
                        common_instances=common_instances,
                        old_instances_by_component=old_by_component,
                        stage6_instances_by_component=stage6_by_component,
                    ),
                }
            )
        work.append(
            {
                "instance_id": instance_id,
                "weight_bytes": (
                    _integer(
                        partition.get("compressed_size_bytes"),
                        label="partition compressed_size_bytes",
                    )
                    + _integer(
                        stage5_partition.get("compressed_size_bytes"),
                        label="Stage-5 partition compressed_size_bytes",
                    )
                ),
                "instance_entry_content_sha256": background_v2._verify_content_address(
                    entry, label="Stage-6 instance entry"
                ),
                "stage5_instance_entry_content_sha256": model_v2._verify_content_address(
                    stage5_entry, label="Stage-5 instance entry"
                ),
                "partition": {
                    "locator": (stage6_parent / partition_name).as_posix(),
                    "compressed_file_sha256": partition.get(
                        "compressed_file_sha256"
                    ),
                    "compressed_size_bytes": partition.get("compressed_size_bytes"),
                    "logical_content_sha256": partition.get(
                        "logical_content_sha256"
                    ),
                    "logical_size_bytes": partition.get("logical_size_bytes"),
                    "record_count": partition.get("record_count"),
                },
                "stage6_contamination_lane": deepcopy(
                    entry.get("model_contamination_lane")
                ),
                "stage5_partition": {
                    "locator": (stage5_parent / stage5_partition_name).as_posix(),
                    "compressed_file_sha256": stage5_partition.get(
                        "compressed_file_sha256"
                    ),
                    "compressed_size_bytes": stage5_partition.get(
                        "compressed_size_bytes"
                    ),
                    "logical_content_sha256": stage5_partition.get(
                        "logical_content_sha256"
                    ),
                    "logical_size_bytes": stage5_partition.get("logical_size_bytes"),
                    "record_count": stage5_partition.get("record_count"),
                },
                "stage5_contamination_lane": deepcopy(
                    stage5_entry.get("contamination_lane")
                ),
                "candidate_count": len(candidates),
                "candidates": candidates,
                "old50_wave_count": sum(key[0] == instance_id for key in capsule_by_key),
                "stage6_wave_count": sum(key[0] == instance_id for key in stage6_by_key),
            }
        )
    node_capacities = dict(capacities or {node: 1 for node in NODES})
    assignment = _lpt_assign(work, node_capacities)
    shards = []
    for row in sorted(work, key=lambda value: str(value["instance_id"])):
        copy = deepcopy(row)
        copy["assigned_node"] = assignment[str(row["instance_id"])]
        shards.append(copy)
    core = {
        "schema": PLAN_SCHEMA,
        "revision": REVISION,
        "status": "PLANNED_NONVOTING_AUDIT",
        "inputs": {
            "stage6_manifest": {
                "locator": stage6_base_locator,
                "schema": stage6.get("schema"),
                "implementation_revision": stage6.get("implementation_revision"),
                "content_sha256": stage6_content_sha,
                "file_sha256": stage6_file_sha,
                "size_bytes": len(stage6_payload),
            },
            "old50_capsule": {
                "locator": capsule_input_locator,
                "schema": bundle.get("schema"),
                "implementation_revision": bundle.get("implementation_revision"),
                "content_sha256": capsule_content_sha,
                "file_sha256": capsule_file_sha,
                "compressed_size_bytes": capsule_path.stat().st_size,
            },
            "stage5_manifest": {
                "locator": stage5_input_locator,
                "schema": stage5.get("schema"),
                "implementation_revision": stage5.get("implementation_revision"),
                "content_sha256": stage5_content_sha,
                "file_sha256": stage5_file_sha,
                "size_bytes": len(stage5_payload),
                "bound_by_stage6_input_closure": True,
            },
        },
        "overlap": {
            "old50_instance_count": len(old_instances),
            "stage6_instance_count": len(stage6_instances),
            "common_instance_count": len(common_instances),
            "common_instance_ids": sorted(common_instances),
            "candidate_exact_key_count": len(candidate_keys),
            "key_contract": ["instance_id", "encounter_id", "wave_ordinal"],
            "ordinal_only_join_allowed": False,
            "wave_id_namespaces_validated_before_join": [
                "<encounter_id>:wave:<wave_ordinal>",
                "<encounter_id>:external-v2-wave:<wave_ordinal>",
            ],
        },
        "lpt": {
            "algorithm": (
                "LPT_DESCENDING_STAGE5_PLUS_STAGE6_COMPRESSED_BYTES_"
                "NORMALIZED_BY_CAPACITY"
            ),
            "nodes": list(NODES),
            "capacity_units": node_capacities,
            "assigned_bytes": {
                node: sum(
                    int(row["weight_bytes"])
                    for row in shards
                    if row["assigned_node"] == node
                )
                for node in NODES
            },
            "assigned_instances": {
                node: sum(row["assigned_node"] == node for row in shards)
                for node in NODES
            },
        },
        "execution_contract": {
            "one_stage6_partition_per_worker": True,
            "stage6_partition_sequential_scan_count": 1,
            "same_instance_stage5_partition_sequential_scan_count": 1,
            "stage5_used_only_for_exact_wave_fury_focal_guid_join": True,
            "raw_csv_or_normalized_input_required": False,
            "formal_comparison_started": False,
            "multiseed_started": False,
            "voting_eligible": False,
            "comparison_ready": False,
        },
        "shards": shards,
    }
    return _content_addressed(core)


def save_plan(plan: Mapping[str, Any], directory: str | Path) -> Path:
    digest = _verify_content_address(plan, label="overlap plan")
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"stage6_old50_overlap_plan.{digest}.json"
    _write_once(path, _canonical(plan) + b"\n")
    return path


def load_plan(path_value: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    value, _ = _read_canonical_json(path, label="overlap plan")
    if value.get("schema") != PLAN_SCHEMA or value.get("revision") != REVISION:
        raise Stage6Old50OverlapError("unsupported overlap plan")
    _verify_content_address(value, label="overlap plan")
    return path, value


class _HashingRaw(io.RawIOBase):
    """Hash compressed bytes while gzip consumes the source exactly once."""

    def __init__(self, source: io.BufferedReader) -> None:
        self._source = source
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        count = self._source.readinto(buffer)
        if count:
            chunk = memoryview(buffer)[:count]
            self.digest.update(chunk)
            self.bytes_read += count
        return count


def _layout_errors(scenario: Mapping[str, Any]) -> list[str]:
    layout = _mapping(scenario.get("layout"), label="capsule layout")
    spatial = _mapping(layout.get("spatial_assumption"), label="spatial_assumption")
    exact = _mapping(layout.get("coordinates_exact"), label="coordinates_exact")
    targets = _array(scenario.get("targets"), label="capsule targets")
    variant = layout.get("variant")
    common = (
        layout.get("side") == "evidence_bounded"
        and layout.get("sensitivity_family")
        == _mapping(scenario.get("source_identity"), label="source_identity").get(
            "wave_id"
        )
        and spatial.get("coordinates") is None
        and spatial.get("not_observed") is True
        and exact == {"value": None, "status": "MISSING"}
    )
    if not common:
        return ["CAPSULE_LAYOUT_EVIDENCE_BOUNDARY_MISMATCH"]
    if variant == "single_target":
        if len(targets) != 1 or spatial.get("status") != "RECONSTRUCTED" or spatial.get("value") != "single_target":
            return ["CAPSULE_SINGLE_TARGET_LAYOUT_MISMATCH"]
    elif variant == "cohit_stacked":
        if len(targets) < 2 or spatial.get("status") != "INFERRED" or spatial.get("value") != "stacked":
            return ["CAPSULE_STACKED_PILE_LAYOUT_MISMATCH"]
    else:
        return ["CAPSULE_LAYOUT_VARIANT_UNSUPPORTED"]
    return []


def _event_arrays(block: Mapping[str, Any]) -> Sequence[tuple[str, list[Any]]]:
    return tuple(
        (
            field,
            _array(block.get(field), label=f"Stage-6 {field}"),
        )
        for field in (
            "runtime_candidate_schedule",
            "nonruntime_damage_diagnostics",
            "dead_marker_diagnostics",
            "negative_damage_diagnostics",
        )
    )


def audit_pair(
    *,
    scenario: Mapping[str, Any],
    block: Mapping[str, Any],
    candidate: Mapping[str, Any],
    expected_contamination: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify one exact-key candidate after all semantic checks."""

    identity = _mapping(scenario.get("source_identity"), label="source_identity")
    block_wave = _mapping(block.get("wave"), label="Stage-6 block wave")
    key = _wave_key(identity)
    if _wave_key(block_wave) != key:
        raise Stage6Old50OverlapError("audit_pair received unequal exact wave keys")
    errors: list[str] = []
    notes = [
        "EXACT_INSTANCE_AND_ENCOUNTER_WITH_VALIDATED_WAVE_NAMESPACE_AND_ORDINAL"
    ]
    if identity.get("wave_ordinal") != block_wave.get("wave_ordinal"):
        errors.append("WAVE_ORDINAL_MISMATCH_AFTER_EXACT_KEY")
    errors.extend(_layout_errors(scenario))
    relation = _mapping(candidate.get("component_relation"), label="component_relation")
    component_relation = relation.get("relation")
    if component_relation not in {
        "OVERLAP_PROJECTION_EXACT",
        "OLD50_PROJECTION_STRICT_SUBSET_STAGE6_AUTHORITY",
    }:
        errors.append("COMPONENT_EQUIVALENCE_CLASS_MISMATCH_ON_OVERLAP")
    elif component_relation == "OLD50_PROJECTION_STRICT_SUBSET_STAGE6_AUTHORITY":
        notes.append("STAGE6_COARSER_LEAKAGE_COMPONENT_RETAINED_AS_SPLIT_AUTHORITY")
    if block.get("component_id") != relation.get("stage6_component_id"):
        errors.append("STAGE6_BLOCK_COMPONENT_DIFFERS_FROM_MANIFEST_DESCRIPTOR")
    block_contamination = _mapping(
        block.get("contamination_lane"), label="Stage-6 block contamination"
    )
    contamination_equal = block_contamination == expected_contamination
    if not contamination_equal:
        errors.append(
            "STAGE6_BLOCK_CONTAMINATION_DIFFERS_FROM_EXACT_STAGE5_WAVE"
        )
    targets = [
        _mapping(row, label="capsule target")
        for row in _array(scenario.get("targets"), label="capsule targets")
    ]
    capsule_guids = [
        _text(row.get("target_guid"), label="capsule target_guid") for row in targets
    ]
    registry = [
        _mapping(row, label="Stage-6 target")
        for row in _array(block.get("target_registry"), label="target_registry")
    ]
    stage6_guids = [
        _text(row.get("target_guid"), label="Stage-6 target_guid") for row in registry
    ]
    if len(set(capsule_guids)) != len(capsule_guids):
        errors.append("CAPSULE_TARGET_GUID_DUPLICATE")
    if len(set(stage6_guids)) != len(stage6_guids):
        errors.append("STAGE6_TARGET_GUID_DUPLICATE")
    capsule_set = set(capsule_guids)
    stage6_set = set(stage6_guids)
    if not capsule_set <= stage6_set:
        errors.append("CAPSULE_TARGET_GUID_OUTSIDE_STAGE6_WAVE")
    horizon = _integer(
        _mapping(scenario.get("horizon"), label="capsule horizon").get(
            "milliseconds"
        ),
        label="capsule horizon milliseconds",
    )
    activity = {
        str(target["target_guid"]): _mapping(
            target.get("observed_hostile_activity_proxy"),
            label="target activity proxy",
        )
        for target in targets
    }
    selected_counts: Counter[str] = Counter()
    selected_min: int | None = None
    selected_max: int | None = None
    out_of_horizon: list[dict[str, Any]] = []
    runtime_out_of_activity: list[dict[str, Any]] = []
    excluded_counts: Counter[str] = Counter()
    for field, rows in _event_arrays(block):
        for raw in rows:
            event = _mapping(raw, label=f"{field} event")
            guid = event.get("target_guid")
            if guid not in capsule_set:
                excluded_counts[field] += 1
                continue
            time_ms = _integer(event.get("time_ms"), label=f"{field} time_ms")
            selected_counts[field] += 1
            selected_min = time_ms if selected_min is None else min(selected_min, time_ms)
            selected_max = time_ms if selected_max is None else max(selected_max, time_ms)
            if not 0 <= time_ms <= horizon:
                out_of_horizon.append(
                    {"lane": field, "target_guid": guid, "time_ms": time_ms}
                )
            if field == "runtime_candidate_schedule" and guid in activity:
                proxy = activity[str(guid)]
                start = _integer(proxy.get("start_ms"), label="activity start_ms")
                end = _integer(proxy.get("end_ms"), label="activity end_ms")
                if not start <= time_ms <= end:
                    runtime_out_of_activity.append(
                        {
                            "target_guid": guid,
                            "time_ms": time_ms,
                            "proxy": [start, end],
                        }
                    )
    if out_of_horizon:
        errors.append("SELECTED_STAGE6_EVENT_OUTSIDE_CAPSULE_HORIZON")
    if runtime_out_of_activity:
        errors.append("STAGE6_RUNTIME_EVENT_OUTSIDE_TARGET_ACTIVITY_PROXY")
    if errors:
        classification = "REJECT"
    elif capsule_set == stage6_set:
        classification = "EXACT"
        notes.append("TARGET_GUID_SET_EQUAL")
    else:
        classification = "SUBSET"
        notes.append("CAPSULE_PILE_STRICT_TARGET_SUBSET")
    notes.extend(
        (
            "SELECTED_EVENTS_WITHIN_CAPSULE_HORIZON",
            "STAGE6_CONTAMINATION_LANE_RETAINED_AS_AUTHORITY",
            "OBSERVED_DAMAGE_NOT_PROMOTED_TO_INITIAL_HEALTH",
        )
    )
    stage6_index = {str(row["target_guid"]): int(row["target_index"]) for row in registry}
    capsule_index = {str(row["target_guid"]): int(row["target_index"]) for row in targets}
    return {
        "classification": classification,
        "reason_codes": sorted(set(errors if errors else notes)),
        "scenario_id": scenario.get("scenario_id"),
        "key": _key_object(key),
        "wave_ordinal": {
            "capsule": identity.get("wave_ordinal"),
            "stage6": block_wave.get("wave_ordinal"),
            "equal": identity.get("wave_ordinal") == block_wave.get("wave_ordinal"),
            "ordinal_only_join_allowed": False,
            "capsule_wave_id": identity.get("wave_id"),
            "stage6_wave_id": block_wave.get("wave_id"),
            "both_wave_ids_encode_same_encounter_and_ordinal": True,
        },
        "target_join": {
            "capsule_target_guids": capsule_guids,
            "stage6_target_guids": stage6_guids,
            "guid_relation": (
                "EQUAL"
                if capsule_set == stage6_set
                else "STRICT_SUBSET"
                if capsule_set < stage6_set
                else "NOT_SUBSET"
            ),
            "index_remap": [
                {
                    "target_guid": guid,
                    "stage6_target_index": stage6_index.get(guid),
                    "capsule_target_index": capsule_index[guid],
                }
                for guid in capsule_guids
            ],
        },
        "time_join": {
            "capsule_horizon_ms": horizon,
            "selected_stage6_event_count_by_lane": dict(sorted(selected_counts.items())),
            "selected_stage6_event_span_ms": (
                None if selected_min is None else [selected_min, selected_max]
            ),
            "excluded_nonpile_event_count_by_lane": dict(
                sorted(excluded_counts.items())
            ),
            "out_of_horizon_events": out_of_horizon,
            "runtime_outside_activity_proxy": runtime_out_of_activity,
            "relative_origin_claim": "SAME_EXACT_WAVE_ID_RECONSTRUCTION_ONLY",
            "horizon_is_exact_pull_or_attackability": False,
        },
        "layout": deepcopy(scenario.get("layout")),
        "component_join": deepcopy(dict(relation)),
        "contamination": {
            "stage6_label": block_contamination.get("label"),
            "stage6_training_candidate": block_contamination.get(
                "training_candidate"
            ),
            "exact_stage5_wave_normalized_lane_equal": contamination_equal,
            "authority": "EXACT_STAGE5_WAVE_BOUND_COHORT_RECEIPT_VIA_STAGE6",
            "raw_instance_lane_compared_directly": False,
            "old50_capsule_field": "ABSENT",
            "player_name_used": False,
        },
        "health_boundary": {
            "stage6_initial_health": "REQUIRES_EXTERNAL_HYPOTHESIS",
            "capsule_exact_max_health": "MISSING",
            "observed_kill_or_damage_budget_used_as_exact_hp": False,
        },
        "source_bindings": {
            "capsule_sha256": scenario.get("capsule_sha256"),
            "capsule_wave_id": identity.get("wave_id"),
            "stage6_wave_id": block_wave.get("wave_id"),
            "stage6_block_content_sha256": _mapping(
                block.get("content_address"), label="block content_address"
            ).get("sha256"),
        },
    }


def _write_once(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise Stage6Old50OverlapError(f"divergent existing output: {path}")
        return "RESUMED"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with io.FileIO(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            import os

            os.fsync(handle.fileno())
        try:
            temporary.replace(path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise Stage6Old50OverlapError(f"divergent existing output: {path}")
        return "PUBLISHED"
    finally:
        temporary.unlink(missing_ok=True)


def _shard(plan: Mapping[str, Any], instance_id: str) -> Mapping[str, Any]:
    matches = [row for row in plan["shards"] if row.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise Stage6Old50OverlapError("instance is absent or duplicated in plan")
    return _mapping(matches[0], label="plan shard")


def _scan_stage5_fury_partition_once(
    *,
    path: Path,
    shard: Mapping[str, Any],
    candidate_keys: set[tuple[str, str, int]],
) -> tuple[dict[tuple[str, str, int], dict[str, Any]], dict[str, Any]]:
    partition = _mapping(shard.get("stage5_partition"), label="Stage-5 partition")
    expected_contamination = _mapping(
        shard.get("stage5_contamination_lane"),
        label="Stage-5 contamination lane",
    )
    logical_digest = hashlib.sha256()
    logical_bytes = 0
    row_count = 0
    selected: dict[tuple[str, str, int], dict[str, Any]] = {}
    with path.open("rb") as source:
        hashing_raw = _HashingRaw(source)
        with io.BufferedReader(hashing_raw) as buffered:
            with gzip.GzipFile(fileobj=buffered, mode="rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    logical_digest.update(raw_line)
                    logical_bytes += len(raw_line)
                    row_count += 1
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise Stage6Old50OverlapError(
                            f"invalid Stage-5 row {line_number}: {error}"
                        ) from error
                    wave = _mapping(value, label="Stage-5 wave")
                    if raw_line != _canonical(wave) + b"\n":
                        raise Stage6Old50OverlapError(
                            "Stage-5 partition contains noncanonical JSONL"
                        )
                    model_v2._validate_model_wave(
                        wave,
                        instance_id=str(shard["instance_id"]),
                        expected_contamination=expected_contamination,
                    )
                    key = _wave_key(
                        _mapping(wave.get("wave"), label="Stage-5 wave identity")
                    )
                    if key not in candidate_keys:
                        continue
                    if key in selected:
                        raise Stage6Old50OverlapError(
                            "Stage-5 partition duplicates a candidate exact wave key"
                        )
                    fury_rows: list[dict[str, Any]] = []
                    for raw_player in _array(
                        wave.get("players"), label="Stage-5 wave players"
                    ):
                        player = _mapping(raw_player, label="Stage-5 player record")
                        metadata = _mapping(
                            player.get("player"), label="Stage-5 player metadata"
                        )
                        spec = _mapping(
                            player.get("warrior_spec_lane"),
                            label="Stage-5 warrior spec lane",
                        )
                        if spec.get("partition_key") != "WARRIOR_FURY":
                            continue
                        eligibility = _mapping(
                            player.get("eligibility_observation"),
                            label="Stage-5 player eligibility",
                        )
                        loo = _mapping(
                            player.get("leave_one_player_out_background"),
                            label="Stage-5 focal leave-one-out",
                        )
                        fury_rows.append(
                            {
                                "focal_player_guid": _text(
                                    metadata.get("guid"), label="Fury player GUID"
                                ),
                                "class": metadata.get("class"),
                                "observed_spec": spec.get("observed_spec"),
                                "spec_evidence_status": spec.get("evidence_status"),
                                "exact_guid_match": spec.get("exact_guid_match"),
                                "training_candidate": eligibility.get(
                                    "historical_fury_candidate_filter_passed"
                                ),
                                "excluded_focal_event_count": loo.get(
                                    "excluded_focal_event_count"
                                ),
                                "excluded_focal_damage_amount": loo.get(
                                    "excluded_focal_damage_amount"
                                ),
                            }
                        )
                    selected[key] = {
                        "stage5_wave_content_sha256": _mapping(
                            wave.get("content_address"),
                            label="Stage-5 wave content address",
                        ).get("sha256"),
                        "normalized_contamination_lane": background_v2._contamination(
                            wave
                        ),
                        "fury_focal_rows": sorted(
                            fury_rows, key=lambda row: row["focal_player_guid"]
                        ),
                    }
    if (
        hashing_raw.bytes_read != partition.get("compressed_size_bytes")
        or hashing_raw.digest.hexdigest() != partition.get("compressed_file_sha256")
        or row_count != partition.get("record_count")
        or logical_bytes != partition.get("logical_size_bytes")
        or logical_digest.hexdigest() != partition.get("logical_content_sha256")
    ):
        raise Stage6Old50OverlapError(
            "Stage-5 partition count/size/hash differs after its single scan"
        )
    if set(selected) != candidate_keys:
        raise Stage6Old50OverlapError(
            "Stage-5 partition is missing a candidate exact wave key"
        )
    return selected, {
        "stage5_partition_open_count": 1,
        "stage5_partition_sequential_scan_count": 1,
        "stage5_compressed_bytes": hashing_raw.bytes_read,
        "stage5_logical_bytes": logical_bytes,
        "stage5_record_count": row_count,
    }


def _attach_fury_focal_join(
    *,
    row: dict[str, Any],
    block: Mapping[str, Any],
    stage5: Mapping[str, Any],
) -> None:
    source = _mapping(block.get("source_model"), label="Stage-6 source_model")
    stage5_sha = stage5.get("stage5_wave_content_sha256")
    source_match = source.get("wave_content_sha256") == stage5_sha
    roster = set(
        _text(value, label="Stage-6 roster GUID")
        for value in _array(block.get("roster_player_guids"), label="Stage-6 roster")
    )
    fury_rows = [
        deepcopy(value)
        for value in _array(stage5.get("fury_focal_rows"), label="Fury focal rows")
    ]
    outside = sorted(
        str(value.get("focal_player_guid"))
        for value in fury_rows
        if value.get("focal_player_guid") not in roster
    )
    exact = source_match and not outside
    if not source_match:
        status = "STAGE5_WAVE_CONTENT_BINDING_MISMATCH"
    elif outside:
        status = "FURY_FOCAL_GUID_OUTSIDE_STAGE6_ROSTER"
    elif fury_rows:
        status = "EXACT_FURY_FOCAL_AVAILABLE"
    else:
        status = "NO_EXACT_FURY_FOCAL_IN_WAVE"
    row["focal_fury_join"] = {
        "status": status,
        "same_exact_wave_key": True,
        "stage5_wave_content_sha256": stage5_sha,
        "stage6_source_model_wave_content_sha256": source.get(
            "wave_content_sha256"
        ),
        "source_binding_equal": source_match,
        "fury_focal_rows": fury_rows if exact else [],
        "fury_focal_count": len(fury_rows) if exact else 0,
        "training_candidate_fury_focal_count": (
            sum(value.get("training_candidate") is True for value in fury_rows)
            if exact
            else 0
        ),
        "exact_guid_leave_one_out_possible": exact and bool(fury_rows),
        "player_name_used": False,
    }


def _scan_stage6_partition_once(
    *,
    path: Path,
    shard: Mapping[str, Any],
    candidates_by_key: Mapping[tuple[str, str, int], Mapping[str, Any]],
    scenarios_by_id: Mapping[str, Mapping[str, Any]],
    stage5_by_key: Mapping[tuple[str, str, int], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    partition = _mapping(shard.get("partition"), label="partition binding")
    logical_digest = hashlib.sha256()
    logical_bytes = 0
    row_count = 0
    seen_candidates: set[tuple[str, str, int]] = set()
    rows: list[dict[str, Any]] = []
    with path.open("rb") as source:
        hashing_raw = _HashingRaw(source)
        with io.BufferedReader(hashing_raw) as buffered:
            with gzip.GzipFile(fileobj=buffered, mode="rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    logical_digest.update(raw_line)
                    logical_bytes += len(raw_line)
                    row_count += 1
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise Stage6Old50OverlapError(
                            f"invalid Stage-6 row {line_number}: {error}"
                        ) from error
                    block = _mapping(value, label="Stage-6 block")
                    if raw_line != _canonical(block) + b"\n":
                        raise Stage6Old50OverlapError(
                            "Stage-6 partition contains noncanonical JSONL"
                        )
                    background_v2._validate_block(
                        block, expected_instance_id=str(shard["instance_id"])
                    )
                    key = _wave_key(
                        _mapping(block.get("wave"), label="Stage-6 block wave")
                    )
                    candidate = candidates_by_key.get(key)
                    if candidate is None:
                        continue
                    if key in seen_candidates:
                        raise Stage6Old50OverlapError(
                            "Stage-6 partition duplicates a candidate exact wave key"
                        )
                    seen_candidates.add(key)
                    if (
                        _mapping(
                            block.get("content_address"), label="block content address"
                        ).get("sha256")
                        != candidate.get("stage6_block_content_sha256")
                    ):
                        raise Stage6Old50OverlapError(
                            "Stage-6 candidate block differs from manifest descriptor"
                        )
                    scenario_id = _text(
                        candidate.get("scenario_id"), label="candidate scenario_id"
                    )
                    scenario = scenarios_by_id.get(scenario_id)
                    if scenario is None or scenario.get("capsule_sha256") != candidate.get(
                        "capsule_sha256"
                    ):
                        raise Stage6Old50OverlapError(
                            "candidate capsule scenario differs from plan"
                        )
                    audit_row = audit_pair(
                        scenario=scenario,
                        block=block,
                        candidate=candidate,
                        expected_contamination=_mapping(
                            _mapping(
                                stage5_by_key.get(key),
                                label="Stage-5 candidate wave",
                            ).get("normalized_contamination_lane"),
                            label="exact Stage-5 wave normalized contamination",
                        ),
                    )
                    _attach_fury_focal_join(
                        row=audit_row,
                        block=block,
                        stage5=_mapping(
                            stage5_by_key.get(key), label="Stage-5 candidate wave"
                        ),
                    )
                    rows.append(audit_row)
    if (
        hashing_raw.bytes_read != partition.get("compressed_size_bytes")
        or hashing_raw.digest.hexdigest() != partition.get("compressed_file_sha256")
        or row_count != partition.get("record_count")
        or logical_bytes != partition.get("logical_size_bytes")
        or logical_digest.hexdigest() != partition.get("logical_content_sha256")
    ):
        raise Stage6Old50OverlapError(
            "Stage-6 partition count/size/hash differs after its single scan"
        )
    if seen_candidates != set(candidates_by_key):
        raise Stage6Old50OverlapError(
            "Stage-6 partition is missing a manifest-planned candidate key"
        )
    return sorted(rows, key=lambda row: tuple(row["key"].values())), {
        "stage6_partition_open_count": 1,
        "stage6_partition_sequential_scan_count": 1,
        "stage6_compressed_bytes": hashing_raw.bytes_read,
        "stage6_logical_bytes": logical_bytes,
        "stage6_record_count": row_count,
    }


def run_worker(
    *,
    plan_path: str | Path,
    shared_root: str | Path,
    output_directory: str | Path,
    instance_id: str,
    node: str,
) -> dict[str, Any]:
    _, plan = load_plan(plan_path)
    shard = _shard(plan, instance_id)
    if node != shard.get("assigned_node"):
        raise Stage6Old50OverlapError("worker node differs from LPT assignment")
    root = Path(shared_root).expanduser().resolve()
    stage6_manifest_path = _resolve(
        root,
        _mapping(plan["inputs"]["stage6_manifest"], label="Stage-6 input").get(
            "locator"
        ),
        label="Stage-6 manifest locator",
    )
    stage6_manifest, stage6_payload = _read_canonical_json(
        stage6_manifest_path, label="Stage-6 manifest"
    )
    stage6_binding = _mapping(
        plan["inputs"]["stage6_manifest"], label="Stage-6 input"
    )
    if (
        _sha256_bytes(stage6_payload) != stage6_binding.get("file_sha256")
        or background_v2._verify_content_address(
            stage6_manifest, label="Stage-6 manifest"
        )
        != stage6_binding.get("content_sha256")
    ):
        raise Stage6Old50OverlapError("live Stage-6 manifest differs from plan")
    entries = {
        str(row["instance_id"]): row
        for row in _array(stage6_manifest.get("instances"), label="Stage-6 instances")
    }
    entry = _mapping(entries.get(instance_id), label="Stage-6 instance entry")
    if background_v2._verify_content_address(
        entry, label="Stage-6 instance entry"
    ) != shard.get("instance_entry_content_sha256"):
        raise Stage6Old50OverlapError("live Stage-6 instance entry differs from plan")
    stage5_manifest_path = _resolve(
        root,
        _mapping(plan["inputs"]["stage5_manifest"], label="Stage-5 input").get(
            "locator"
        ),
        label="Stage-5 manifest locator",
    )
    stage5_manifest, stage5_payload = _read_canonical_json(
        stage5_manifest_path, label="Stage-5 manifest"
    )
    stage5_binding = _mapping(
        plan["inputs"]["stage5_manifest"], label="Stage-5 input"
    )
    if (
        _sha256_bytes(stage5_payload) != stage5_binding.get("file_sha256")
        or model_v2._verify_content_address(
            stage5_manifest, label="Stage-5 manifest"
        )
        != stage5_binding.get("content_sha256")
    ):
        raise Stage6Old50OverlapError("live Stage-5 manifest differs from plan")
    stage5_entries = {
        str(row["instance_id"]): row
        for row in _array(stage5_manifest.get("instances"), label="Stage-5 instances")
    }
    stage5_entry = _mapping(
        stage5_entries.get(instance_id), label="Stage-5 instance entry"
    )
    if model_v2._verify_content_address(
        stage5_entry, label="Stage-5 instance entry"
    ) != shard.get("stage5_instance_entry_content_sha256"):
        raise Stage6Old50OverlapError("live Stage-5 instance entry differs from plan")
    capsule_path = _resolve(
        root,
        _mapping(plan["inputs"]["old50_capsule"], label="capsule input").get(
            "locator"
        ),
        label="capsule locator",
    )
    bundle, capsule_content_sha, capsule_file_sha = _load_capsule(capsule_path)
    capsule_binding = _mapping(
        plan["inputs"]["old50_capsule"], label="capsule input"
    )
    if (
        capsule_content_sha != capsule_binding.get("content_sha256")
        or capsule_file_sha != capsule_binding.get("file_sha256")
    ):
        raise Stage6Old50OverlapError("live old-50 capsule differs from plan")
    scenarios_by_id = {
        str(row["scenario_id"]): _mapping(row, label="capsule scenario")
        for row in _array(bundle.get("scenarios"), label="capsule scenarios")
        if _mapping(row, label="capsule scenario")
        .get("source_identity", {})
        .get("instance_id")
        == instance_id
    }
    candidates_by_key = {
        _wave_key(_mapping(row.get("key"), label="candidate key")): _mapping(
            row, label="candidate"
        )
        for row in _array(shard.get("candidates"), label="shard candidates")
    }
    stage5_by_key, stage5_scan = _scan_stage5_fury_partition_once(
        path=_resolve(
            root,
            _mapping(
                shard.get("stage5_partition"), label="Stage-5 partition"
            ).get("locator"),
            label="Stage-5 partition locator",
        ),
        shard=shard,
        candidate_keys=set(candidates_by_key),
    )
    rows, scan = _scan_stage6_partition_once(
        path=_resolve(
            root,
            _mapping(shard.get("partition"), label="partition").get("locator"),
            label="partition locator",
        ),
        shard=shard,
        candidates_by_key=candidates_by_key,
        scenarios_by_id=scenarios_by_id,
        stage5_by_key=stage5_by_key,
    )
    counts = Counter(row["classification"] for row in rows)
    core = {
        "schema": RECEIPT_SCHEMA,
        "revision": REVISION,
        "plan_sha256": _mapping(
            plan.get("content_address"), label="plan content address"
        ).get("sha256"),
        "instance_id": instance_id,
        "worker_node": node,
        "stage6_partition": deepcopy(shard.get("partition")),
        "single_scan_receipt": {**scan, **stage5_scan},
        "candidate_count": len(rows),
        "classification_counts": {
            classification: counts[classification]
            for classification in CLASSIFICATIONS
        },
        "rows": rows,
        "scientific_boundary": {
            "descriptive_join_audit_only": True,
            "voting_eligible": False,
            "comparison_ready": False,
            "formal_comparison_started": False,
            "observed_damage_used_as_exact_hp": False,
        },
    }
    receipt = _content_addressed(core)
    digest = receipt["content_address"]["sha256"]
    directory = Path(output_directory).expanduser().resolve() / "receipts"
    payload = _canonical(receipt) + b"\n"
    addressed = directory / f"{instance_id}.{digest}.json"
    stable = directory / f"{instance_id}.json"
    _write_once(addressed, payload)
    status = _write_once(stable, payload)
    return {
        "status": status,
        "instance_id": instance_id,
        "receipt": str(stable),
        "receipt_sha256": digest,
        "classification_counts": core["classification_counts"],
    }


def _compiler_input(
    *,
    row: Mapping[str, Any],
    shard: Mapping[str, Any],
    plan: Mapping[str, Any],
    focal: Mapping[str, Any],
) -> dict[str, Any]:
    if row.get("classification") not in {"EXACT", "SUBSET"}:
        raise Stage6Old50OverlapError("rejected row cannot become a compiler input")
    focal_join = _mapping(row.get("focal_fury_join"), label="focal Fury join")
    if (
        focal_join.get("status") != "EXACT_FURY_FOCAL_AVAILABLE"
        or focal_join.get("source_binding_equal") is not True
    ):
        raise Stage6Old50OverlapError("compiler input lacks exact Fury focal evidence")
    focal_guid = _text(
        focal.get("focal_player_guid"), label="compiler focal_player_guid"
    )
    target_join = _mapping(row.get("target_join"), label="target join")
    selected_guids = list(
        _array(
            target_join.get("capsule_target_guids"),
            label="compiler selected target GUIDs",
        )
    )
    core = {
        "schema": COMPILER_INPUT_SCHEMA,
        "revision": REVISION,
        "status": "READY_FOR_HYPOTHESIS_SELECTION_NONVOTING",
        "identity": {
            **deepcopy(dict(_mapping(row.get("key"), label="join key"))),
            "scenario_id": row.get("scenario_id"),
            "focal_player_guid": focal_guid,
        },
        "source_bindings": {
            "old50_capsule": {
                "locator": plan["inputs"]["old50_capsule"]["locator"],
                "bundle_content_sha256": plan["inputs"]["old50_capsule"][
                    "content_sha256"
                ],
                "scenario_id": row.get("scenario_id"),
                "scenario_capsule_sha256": row["source_bindings"][
                    "capsule_sha256"
                ],
                "wave_id": row["source_bindings"]["capsule_wave_id"],
            },
            "stage5_exact_wave": {
                "manifest_locator": plan["inputs"]["stage5_manifest"]["locator"],
                "manifest_content_sha256": plan["inputs"]["stage5_manifest"][
                    "content_sha256"
                ],
                "partition_locator": shard["stage5_partition"]["locator"],
                "wave_content_sha256": focal_join["stage5_wave_content_sha256"],
                "purpose": "exact Fury focal GUID evidence only",
            },
            "stage6_exact_block": {
                "manifest_locator": plan["inputs"]["stage6_manifest"]["locator"],
                "manifest_content_sha256": plan["inputs"]["stage6_manifest"][
                    "content_sha256"
                ],
                "partition_locator": shard["partition"]["locator"],
                "block_content_sha256": row["source_bindings"][
                    "stage6_block_content_sha256"
                ],
                "wave_id": row["source_bindings"]["stage6_wave_id"],
            },
        },
        "join": {
            "classification": row["classification"],
            "target_guid_relation": target_join["guid_relation"],
            "selected_target_guids": selected_guids,
            "target_index_remap": deepcopy(target_join["index_remap"]),
            "component_relation": deepcopy(row["component_join"]),
            "contamination": deepcopy(row["contamination"]),
        },
        "causal_schedule_projection": {
            "selection_unit": "EXACT_CONTENT_ADDRESSED_STAGE6_BLOCK",
            "component_wide_random_draw_allowed": False,
            "reason_component_draw_forbidden": (
                "another wave can have a different target registry"
            ),
            "focal_leave_one_out": {
                "predicate": "actor_player_guid != focal_player_guid",
                "focal_player_guid": focal_guid,
                "exact_guid_only": True,
                "excluded_focal_event_count": focal.get(
                    "excluded_focal_event_count"
                ),
                "excluded_focal_damage_amount": focal.get(
                    "excluded_focal_damage_amount"
                ),
            },
            "target_projection": {
                "predicate": "target_guid in selected_target_guids",
                "selected_target_guids": selected_guids,
                "unselected_target_events_enter_selected_targets": False,
                "stage6_to_capsule_target_index_remap": deepcopy(
                    target_join["index_remap"]
                ),
            },
            "time_ms_preserved": True,
            "source_eventmeta_order_preserved": True,
            "same_timestamp_order_preserved": True,
            "capsule_horizon_ms": row["time_join"]["capsule_horizon_ms"],
            "selected_events_outside_horizon": 0,
        },
        "dynamic_v3_compile_requirements": {
            "command": "load_dynamic_v3",
            "dynamic_config_schema": "o2o_dynamic_target_semantics/v3",
            "target_health_hypothesis_required_for_every_selected_guid": True,
            "target_armor_hypothesis_required_for_every_selected_guid": True,
            "attackability_hypothesis_required_for_every_selected_guid": True,
            "observed_kill_or_damage_budget_may_be_exact_hp": False,
            "observed_activity_may_be_exact_attackability": False,
            "hypothesis_selection_completed": False,
            "load_dynamic_v3_wire_ready": False,
        },
        "scientific_boundary": {
            "historical_truth": False,
            "diagnostic_nonvoting": True,
            "comparison_ready": False,
            "formal_runner_registered": False,
        },
    }
    value = _content_addressed(core)
    validate_compiler_input(value)
    return value


def validate_compiler_input(value: Mapping[str, Any]) -> None:
    if (
        value.get("schema") != COMPILER_INPUT_SCHEMA
        or value.get("revision") != REVISION
        or value.get("status") != "READY_FOR_HYPOTHESIS_SELECTION_NONVOTING"
    ):
        raise Stage6Old50OverlapError("unsupported dynamic-v3 compiler input")
    _verify_content_address(value, label="dynamic-v3 compiler input")
    join = _mapping(value.get("join"), label="compiler join")
    if join.get("classification") not in {"EXACT", "SUBSET"}:
        raise Stage6Old50OverlapError("compiler input carries a rejected join")
    component = _mapping(
        join.get("component_relation"), label="compiler component relation"
    )
    contamination = _mapping(
        join.get("contamination"), label="compiler contamination"
    )
    if (
        component.get("split_authority")
        not in {"EQUIVALENT_ON_OVERLAP", "STAGE6_COARSER_COMPONENT"}
        or contamination.get("exact_stage5_wave_normalized_lane_equal") is not True
    ):
        raise Stage6Old50OverlapError(
            "compiler input weakens component or contamination authority"
        )
    projection = _mapping(
        value.get("causal_schedule_projection"), label="schedule projection"
    )
    loo = _mapping(projection.get("focal_leave_one_out"), label="focal LOO")
    targets = _mapping(projection.get("target_projection"), label="target projection")
    selected = _array(targets.get("selected_target_guids"), label="selected GUIDs")
    if (
        projection.get("selection_unit")
        != "EXACT_CONTENT_ADDRESSED_STAGE6_BLOCK"
        or projection.get("component_wide_random_draw_allowed") is not False
        or loo.get("exact_guid_only") is not True
        or not selected
        or selected != join.get("selected_target_guids")
        or projection.get("selected_events_outside_horizon") != 0
    ):
        raise Stage6Old50OverlapError("compiler causal projection was widened")
    requirements = _mapping(
        value.get("dynamic_v3_compile_requirements"),
        label="dynamic-v3 requirements",
    )
    if (
        requirements.get("command") != "load_dynamic_v3"
        or requirements.get("dynamic_config_schema")
        != "o2o_dynamic_target_semantics/v3"
        or requirements.get("observed_kill_or_damage_budget_may_be_exact_hp")
        is not False
        or requirements.get("hypothesis_selection_completed") is not False
        or requirements.get("load_dynamic_v3_wire_ready") is not False
    ):
        raise Stage6Old50OverlapError("compiler hypothesis boundary was widened")
    boundary = _mapping(
        value.get("scientific_boundary"), label="compiler scientific boundary"
    )
    if (
        boundary.get("diagnostic_nonvoting") is not True
        or boundary.get("comparison_ready") is not False
        or boundary.get("formal_runner_registered") is not False
    ):
        raise Stage6Old50OverlapError("compiler input became comparison eligible")


def reduce_audit(
    *, plan_path: str | Path, output_directory: str | Path
) -> dict[str, Any]:
    _, plan = load_plan(plan_path)
    directory = Path(output_directory).expanduser().resolve()
    receipts: list[Mapping[str, Any]] = []
    expected = {str(row["instance_id"]) for row in plan["shards"]}
    actual = {path.stem for path in (directory / "receipts").glob("*.json") if "." not in path.stem}
    if actual != expected:
        raise Stage6Old50OverlapError("worker receipt set is incomplete or unexpected")
    for instance_id in sorted(expected):
        receipt, payload = _read_canonical_json(
            directory / "receipts" / f"{instance_id}.json",
            label="worker receipt",
        )
        if (
            receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("revision") != REVISION
            or receipt.get("instance_id") != instance_id
            or receipt.get("plan_sha256")
            != _mapping(
                plan.get("content_address"), label="plan content address"
            ).get("sha256")
        ):
            raise Stage6Old50OverlapError("worker receipt identity differs")
        digest = _verify_content_address(receipt, label="worker receipt")
        addressed = directory / "receipts" / f"{instance_id}.{digest}.json"
        if not addressed.is_file() or addressed.read_bytes() != payload:
            raise Stage6Old50OverlapError("worker stable/addressed receipts differ")
        scan = _mapping(receipt.get("single_scan_receipt"), label="single scan receipt")
        if (
            scan.get("stage6_partition_open_count") != 1
            or scan.get("stage6_partition_sequential_scan_count") != 1
            or scan.get("stage5_partition_open_count") != 1
            or scan.get("stage5_partition_sequential_scan_count") != 1
        ):
            raise Stage6Old50OverlapError(
                "worker did not report one Stage-5 and one Stage-6 partition scan"
            )
        receipts.append(receipt)
    rows = sorted(
        [deepcopy(row) for receipt in receipts for row in receipt["rows"]],
        key=lambda row: (
            row["key"]["instance_id"],
            row["key"]["encounter_id"],
            row["key"]["wave_ordinal"],
        ),
    )
    if len(rows) != plan["overlap"]["candidate_exact_key_count"]:
        raise Stage6Old50OverlapError("reduced candidate count differs from plan")
    counts = Counter(row["classification"] for row in rows)
    reasons = Counter(
        reason for row in rows for reason in row.get("reason_codes", [])
    )
    accepted = counts["EXACT"] + counts["SUBSET"]
    accepted_rows = [
        row for row in rows if row["classification"] in {"EXACT", "SUBSET"}
    ]
    accepted_with_fury = [
        row
        for row in accepted_rows
        if row["focal_fury_join"]["exact_guid_leave_one_out_possible"] is True
    ]
    fury_focal_associations = sum(
        int(row["focal_fury_join"]["fury_focal_count"])
        for row in accepted_with_fury
    )
    training_fury_focal_associations = sum(
        int(row["focal_fury_join"]["training_candidate_fury_focal_count"])
        for row in accepted_with_fury
    )
    shard_by_instance = {
        str(shard["instance_id"]): _mapping(shard, label="plan shard")
        for shard in plan["shards"]
    }
    compiler_inputs = []
    for row in accepted_with_fury:
        shard = shard_by_instance[row["key"]["instance_id"]]
        for focal in row["focal_fury_join"]["fury_focal_rows"]:
            compiler_inputs.append(
                _compiler_input(row=row, shard=shard, plan=plan, focal=focal)
            )
    compiler_inputs.sort(
        key=lambda value: (
            value["identity"]["instance_id"],
            value["identity"]["encounter_id"],
            value["identity"]["wave_ordinal"],
            value["identity"]["focal_player_guid"],
        )
    )
    old_only = sum(int(row["old50_wave_count"]) for row in plan["shards"]) - len(rows)
    stage6_only = sum(int(row["stage6_wave_count"]) for row in plan["shards"]) - len(rows)
    core = {
        "schema": AUDIT_SCHEMA,
        "revision": REVISION,
        "status": "COMPLETE_DESCRIPTIVE_NONVOTING",
        "plan_sha256": plan["content_address"]["sha256"],
        "input_bindings": deepcopy(plan["inputs"]),
        "join_contract": {
            "semantic_exact_key": [
                "instance_id",
                "encounter_id",
                "wave_ordinal_encoded_by_validated_wave_id",
            ],
            "ordinal_only_join_allowed": False,
            "wave_id_namespace_difference_is_explicit": True,
            "exact_means_equal_target_guid_sets_after_all_integrity_checks": True,
            "subset_means_capsule_pile_is_strict_guid_subset_after_all_checks": True,
            "stage6_coarser_component_is_conservative_split_authority": True,
            "reject_means_a_join_integrity_or_semantic_check_failed": True,
            "historical_truth_claimed": False,
        },
        "coverage": {
            "common_instance_count": plan["overlap"]["common_instance_count"],
            "common_instance_ids": deepcopy(plan["overlap"]["common_instance_ids"]),
            "candidate_exact_key_count": len(rows),
            "old50_only_wave_count_within_common_instances": old_only,
            "stage6_only_wave_count_within_common_instances": stage6_only,
        },
        "summary": {
            "EXACT": counts["EXACT"],
            "SUBSET": counts["SUBSET"],
            "REJECT": counts["REJECT"],
            "accepted_for_future_nonvoting_scenario_compilation": accepted,
            "accepted_with_exact_fury_focal_guid": len(accepted_with_fury),
            "accepted_without_exact_fury_focal_guid": accepted
            - len(accepted_with_fury),
            "exact_fury_focal_wave_associations": fury_focal_associations,
            "training_candidate_fury_focal_wave_associations": (
                training_fury_focal_associations
            ),
            "dynamic_v3_compiler_input_count": len(compiler_inputs),
            "reason_counts": dict(sorted(reasons.items())),
            "worker_count": len(receipts),
            "stage6_partition_scan_count": sum(
                receipt["single_scan_receipt"][
                    "stage6_partition_sequential_scan_count"
                ]
                for receipt in receipts
            ),
            "stage5_partition_scan_count": sum(
                receipt["single_scan_receipt"][
                    "stage5_partition_sequential_scan_count"
                ]
                for receipt in receipts
            ),
        },
        "health_and_outcome_boundary": {
            "stage6_observed_background_damage_is_initial_health": False,
            "old50_observed_kill_budget_is_exact_hp": False,
            "exact_hp_compiled": False,
            "exact_attackability_compiled": False,
            "comparison_or_training_started": False,
        },
        "compiler_design_gate": {
            "eligible_accepted_row_count": accepted,
            "accepted_row_count_with_exact_fury_focal_guid": len(
                accepted_with_fury
            ),
            "systemic_join_integrity_failure": accepted_with_fury == [],
            "minimal_native_load_dynamic_v3_contract_may_be_designed": bool(
                accepted_with_fury
            ),
            "compiler_implemented_this_round": False,
            "required_selection_unit": (
                "exact accepted Stage-6 block plus exact same-wave Fury GUID leave-one-out"
            ),
            "component_wide_random_draw_for_old50_target_hypothesis_allowed": False,
            "reason": (
                "a component-wide draw may select a different wave and target registry"
            ),
        },
        "compiler_input_schema": {
            "schema": COMPILER_INPUT_SCHEMA,
            "consumer_action": (
                "select typed health/armor/attackability hypotheses, then compile "
                "the exact block projection to load_dynamic_v3"
            ),
            "inputs": compiler_inputs,
        },
        "rows": rows,
        "scientific_boundary": {
            "nonvoting": True,
            "comparison_ready": False,
            "formal_runner_registered": False,
            "multiseed_comparison_started": False,
        },
    }
    audit = _content_addressed(core)
    digest = audit["content_address"]["sha256"]
    payload = _canonical(audit) + b"\n"
    addressed = directory / f"stage6_old50_overlap_audit.{digest}.json"
    stable = directory / "audit.json"
    _write_once(addressed, payload)
    status = _write_once(stable, payload)
    return {
        "status": status,
        "audit": str(stable),
        "content_addressed_audit": str(addressed),
        "content_sha256": digest,
        "summary": audit["summary"],
        "compiler_design_gate": audit["compiler_design_gate"],
    }


def _parse_capacities(values: Sequence[str]) -> dict[str, int]:
    if not values:
        return {node: 1 for node in NODES}
    result: dict[str, int] = {}
    for value in values:
        node, separator, raw_capacity = value.partition("=")
        if not separator:
            raise Stage6Old50OverlapError("capacity must be NODE=INTEGER")
        result[node] = int(raw_capacity)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--stage6-manifest", required=True)
    plan.add_argument("--stage5-manifest")
    plan.add_argument("--capsule", required=True)
    plan.add_argument("--shared-root", required=True)
    plan.add_argument("--output-directory", required=True)
    plan.add_argument("--capacity", action="append", default=[])
    plan.add_argument("--expect-common-instances", type=int)
    plan.add_argument("--expect-candidate-keys", type=int)
    worker = sub.add_parser("worker")
    worker.add_argument("--plan", required=True)
    worker.add_argument("--shared-root", required=True)
    worker.add_argument("--output-directory", required=True)
    worker.add_argument("--instance-id", required=True)
    worker.add_argument("--node", choices=NODES, required=True)
    reducer = sub.add_parser("reduce")
    reducer.add_argument("--plan", required=True)
    reducer.add_argument("--output-directory", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        plan = make_plan(
            stage6_manifest=args.stage6_manifest,
            stage5_manifest=args.stage5_manifest,
            capsule=args.capsule,
            shared_root=args.shared_root,
            capacities=_parse_capacities(args.capacity),
            expected_common_instances=args.expect_common_instances,
            expected_candidate_keys=args.expect_candidate_keys,
        )
        path = save_plan(plan, args.output_directory)
        result = {
            "plan": str(path),
            "plan_sha256": plan["content_address"]["sha256"],
            "overlap": plan["overlap"],
            "lpt": plan["lpt"],
        }
    elif args.command == "worker":
        result = run_worker(
            plan_path=args.plan,
            shared_root=args.shared_root,
            output_directory=args.output_directory,
            instance_id=args.instance_id,
            node=args.node,
        )
    else:
        result = reduce_audit(
            plan_path=args.plan, output_directory=args.output_directory
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AUDIT_SCHEMA",
    "CLASSIFICATIONS",
    "COMPILER_INPUT_SCHEMA",
    "NODES",
    "PLAN_SCHEMA",
    "RECEIPT_SCHEMA",
    "REVISION",
    "SCHEMA",
    "Stage6Old50OverlapError",
    "audit_pair",
    "load_plan",
    "make_plan",
    "reduce_audit",
    "run_worker",
    "save_plan",
    "validate_compiler_input",
]
