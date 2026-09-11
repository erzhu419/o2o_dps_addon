"""Deterministic, offline union and cohort binding for Chronicle raw manifests.

The raw union keeps every distinct instance as descriptive evidence.  Training
eligibility is *not* inferred from a raw row's contamination label: it is the
set of unique instance IDs explicitly marked ``training_candidate=true`` by a
strictly loaded character-instance inventory.  A strictly loaded exact-DPS
index must in turn bind that exact inventory.

All inputs are local immutable artifacts.  Source raw manifests and their
object closures are replayed without HTTP, the raw union is published only
after validation, and the content-addressed cohort receipt is published last.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, BinaryIO, Mapping, Sequence, TextIO

from . import chronicle_external_api_ingest_v1 as ingest_v1
from . import chronicle_external_character_dps_index_v1 as dps_v1
from . import chronicle_external_character_history_v1 as history_v1


SCHEMA = "chronicle_external_api_manifest_union/v1"
IMPLEMENTATION_REVISION = "chronicle_external_api_manifest_union_v1.0"
KIND = "chronicle_external_api_manifest_union_cohort_binding_receipt"
OUTPUT_DIRECTORY = "derived/chronicle_external_api_manifest_union/v1/manifests"
MANIFEST_PREFIX = "chronicle_external_api_manifest_union_v1"

RAW_SCHEMA = ingest_v1.SCHEMA
RAW_KIND = "chronicle_external_api_raw_snapshot"
RAW_IMPLEMENTATION_REVISION = ingest_v1.IMPLEMENTATION_REVISION
RAW_PARSER_CONTRACT_REVISION = ingest_v1.PARSER_CONTRACT_REVISION

DEFAULT_DATA_ROOT = ingest_v1.DEFAULT_DATA_ROOT

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RAW_MANIFEST_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_CURRENT_CURSOR_CONTRACT = {
    "query_parameter": "upload_after",
    "source_field": "uploaded_at",
    "inclusive_boundary_deduplication": True,
    "started_at_role": "provenance_and_contamination_only_never_cursor",
    "future_information_allowed": False,
}


class ChronicleExternalManifestUnionError(RuntimeError):
    """A source, binding, cohort, assertion, or publication is invalid."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ChronicleExternalManifestUnionError(
            f"value is not canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _canonical_document_bytes(value: Any) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalManifestUnionError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalManifestUnionError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ChronicleExternalManifestUnionError(
            f"{label} must be a non-empty string without surrounding whitespace"
        )
    return value


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ChronicleExternalManifestUnionError(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ChronicleExternalManifestUnionError(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return value


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalManifestUnionError(
            f"{label} is not UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleExternalManifestUnionError(f"{label} must be a JSON object")
    return value


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(core))
    result.pop("content_address", None)
    digest = _sha256(_canonical_document_bytes(result))
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document excluding content_address",
        "sha256": digest,
    }
    return result


def _verify_content_address(
    value: Mapping[str, Any],
    *,
    label: str,
    expected_scope: str = "canonical JSON document excluding content_address",
) -> str:
    address = _mapping(value.get("content_address"), label=f"{label}.content_address")
    if set(address) != {"algorithm", "scope", "sha256"}:
        raise ChronicleExternalManifestUnionError(
            f"{label} content_address schema is invalid"
        )
    if (
        address.get("algorithm") != "sha256"
        or address.get("scope") != expected_scope
    ):
        raise ChronicleExternalManifestUnionError(
            f"{label} content_address contract is unsupported"
        )
    declared = _digest(address.get("sha256"), label=f"{label} content SHA-256")
    core = {key: child for key, child in value.items() if key != "content_address"}
    if _sha256(_canonical_document_bytes(core)) != declared:
        raise ChronicleExternalManifestUnionError(f"{label} content hash mismatch")
    return declared


def _data_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in resolved.parts}:
        raise ChronicleExternalManifestUnionError(
            f"data root must be an offline_data tree: {resolved}"
        )
    return resolved


def _raw_root(data_root: Path) -> Path:
    return data_root / "chronicle_raw" / "external_api" / "v1"


def _safe_relative(path: Path, data_root: Path, *, label: str) -> str:
    try:
        return path.resolve().relative_to(data_root.resolve()).as_posix()
    except (OSError, ValueError) as error:
        raise ChronicleExternalManifestUnionError(
            f"{label} must stay under data_root"
        ) from error


def _regular_resolved_file(path: str | Path, *, label: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ChronicleExternalManifestUnionError(f"{label} must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot resolve {label} {requested}: {error}"
        ) from error
    if not resolved.is_file():
        raise ChronicleExternalManifestUnionError(f"{label} must be a regular file")
    return resolved


def _load_current_raw_manifest(
    path: str | Path, *, data_root: Path
) -> tuple[dict[str, Any], Path, bytes, str]:
    resolved = _regular_resolved_file(path, label="source raw manifest")
    manifest_directory = (_raw_root(data_root) / "manifests").resolve()
    if resolved.parent != manifest_directory:
        raise ChronicleExternalManifestUnionError(
            "source raw manifest must be a direct child of the configured raw manifest directory"
        )
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot read source raw manifest {resolved}: {error}"
        ) from error
    digest = _sha256(payload)
    match = _RAW_MANIFEST_RE.fullmatch(resolved.name)
    if match is None or match.group(1) != digest:
        raise ChronicleExternalManifestUnionError(
            "source raw manifest filename/content SHA-256 mismatch"
        )
    manifest = _json_object(payload, label="source raw manifest")
    if payload != _canonical_document_bytes(manifest):
        raise ChronicleExternalManifestUnionError(
            "source raw manifest bytes are not canonical JSON"
        )
    if (
        manifest.get("schema") != RAW_SCHEMA
        or manifest.get("kind") != RAW_KIND
        or manifest.get("implementation_revision")
        != RAW_IMPLEMENTATION_REVISION
        or manifest.get("parser_contract_revision")
        != RAW_PARSER_CONTRACT_REVISION
    ):
        raise ChronicleExternalManifestUnionError(
            "source raw manifest is not the current ingest/parser contract"
        )
    if manifest.get("api_base") != ingest_v1.EXTERNAL_API_BASE:
        raise ChronicleExternalManifestUnionError(
            "source raw manifest API base is not the locked Chronicle External API"
        )
    if manifest.get("cursor_contract") != _CURRENT_CURSOR_CONTRACT:
        raise ChronicleExternalManifestUnionError(
            "source raw manifest cursor contract is unsupported"
        )
    if manifest.get("contamination_contract") != ingest_v1._contamination_contract():
        raise ChronicleExternalManifestUnionError(
            "source raw manifest contamination contract is not current"
        )
    _mapping(manifest.get("request"), label="source raw manifest.request")
    _array(manifest.get("recent_pages"), label="source raw manifest.recent_pages")
    _array(
        manifest.get("leaderboard_snapshots"),
        label="source raw manifest.leaderboard_snapshots",
    )
    _array(manifest.get("instances"), label="source raw manifest.instances")

    try:
        replay = ingest_v1.replay_manifest_from_local_raw(
            resolved, data_root=data_root
        )
    except (ingest_v1.ChronicleIngestError, OSError) as error:
        raise ChronicleExternalManifestUnionError(
            f"source raw manifest local replay failed: {error}"
        ) from error
    if (
        replay.get("status") != "ALREADY_CURRENT_LOCAL_RAW"
        or replay.get("manifest_sha256") != digest
        or replay.get("source_manifest_sha256") != digest
        or replay.get("network_requests_made") != 0
        or replay.get("watermark_mutated") is not False
    ):
        raise ChronicleExternalManifestUnionError(
            "source raw manifest did not replay as an immutable current offline fixed point"
        )
    return manifest, resolved, payload, digest


def _dedupe_exact_rows(rows: Sequence[Any], *, label: str) -> list[Any]:
    by_canonical: dict[bytes, Any] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ChronicleExternalManifestUnionError(
                f"{label}[{index}] must be an object"
            )
        canonical = _canonical_bytes(row)
        by_canonical.setdefault(canonical, deepcopy(dict(row)))
    return [by_canonical[key] for key in sorted(by_canonical)]


def _union_instances(
    manifests: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for manifest_index, manifest in enumerate(manifests):
        instances = _array(
            manifest.get("instances"),
            label=f"source_manifests[{manifest_index}].instances",
        )
        for row_index, raw_row in enumerate(instances):
            row = _mapping(
                raw_row,
                label=f"source_manifests[{manifest_index}].instances[{row_index}]",
            )
            instance_id = _text(
                row.get("instance_id"),
                label=(
                    f"source_manifests[{manifest_index}].instances[{row_index}]"
                    ".instance_id"
                ),
            )
            normalized = deepcopy(dict(row))
            previous = by_id.get(instance_id)
            if previous is not None:
                if previous != normalized:
                    raise ChronicleExternalManifestUnionError(
                        f"duplicate instance {instance_id} has conflicting complete rows"
                    )
                duplicate_count += 1
                continue
            by_id[instance_id] = normalized
    return [by_id[key] for key in sorted(by_id)], duplicate_count


def _inventory_training_cohort(
    inventory: Mapping[str, Any],
    *,
    union_instances: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, int]]:
    if inventory.get("implementation_revision") != history_v1.INVENTORY_IMPLEMENTATION_REVISION:
        raise ChronicleExternalManifestUnionError(
            "inventory is not the current boundary-aware implementation revision"
        )
    rows = _array(inventory.get("instances"), label="inventory.instances")
    request = _mapping(
        inventory.get("inventory_request"), label="inventory.inventory_request"
    )
    required_streams_raw = _array(
        request.get("required_streams"),
        label="inventory.inventory_request.required_streams",
    )
    required_streams = [
        _text(value, label=f"inventory required stream[{index}]")
        for index, value in enumerate(required_streams_raw)
    ]
    if (
        not required_streams
        or required_streams != sorted(set(required_streams))
        or any(value not in ingest_v1.EVENT_STREAM_TYPES for value in required_streams)
    ):
        raise ChronicleExternalManifestUnionError(
            "inventory required streams must be non-empty, unique, sorted, and supported"
        )
    by_id: dict[str, dict[str, set[Any]]] = {}
    candidate_membership_count = 0
    exact_training_membership_count = 0
    exact_training_record_count = 0
    censored_training_membership_count = 0
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, label=f"inventory.instances[{index}]")
        instance_id = _text(
            row.get("instance_id"), label=f"inventory.instances[{index}].instance_id"
        )
        label = _text(
            row.get("contamination_label"),
            label=f"inventory.instances[{index}].contamination_label",
        )
        missing_plan = _mapping(
            row.get("missing_plan"), label=f"inventory.instances[{index}].missing_plan"
        )
        training = missing_plan.get("training_candidate")
        if not isinstance(training, bool):
            raise ChronicleExternalManifestUnionError(
                f"inventory.instances[{index}].missing_plan.training_candidate must be boolean"
            )
        expected_training = label in history_v1.TRAINING_CANDIDATE_LABELS
        if training != expected_training:
            raise ChronicleExternalManifestUnionError(
                f"inventory instance {instance_id} training flag disagrees with current contamination contract"
            )
        fetch_planned = missing_plan.get("training_fetch_planned")
        if not isinstance(fetch_planned, bool):
            raise ChronicleExternalManifestUnionError(
                f"inventory.instances[{index}].missing_plan.training_fetch_planned must be boolean"
            )
        if training and fetch_planned:
            raise ChronicleExternalManifestUnionError(
                f"inventory training candidate {instance_id} still has a planned evidence fetch"
            )
        ranking = _mapping(
            _mapping(
                row.get("coverage"), label=f"inventory.instances[{index}].coverage"
            ).get("ranking_exact_dps"),
            label=f"inventory.instances[{index}].coverage.ranking_exact_dps",
        )
        ranking_training = ranking.get("raid_contamination_training_eligible")
        if not isinstance(ranking_training, bool) or ranking_training != training:
            raise ChronicleExternalManifestUnionError(
                f"inventory instance {instance_id} ranking/training eligibility disagrees"
            )
        if training:
            candidate_membership_count += 1
            required_coverage = _mapping(
                _mapping(
                    row.get("coverage"),
                    label=f"inventory.instances[{index}].coverage",
                ).get("required_streams"),
                label=f"inventory.instances[{index}].coverage.required_streams",
            )
            action_coverage = _mapping(
                _mapping(
                    row.get("coverage"),
                    label=f"inventory.instances[{index}].coverage",
                ).get("action_events"),
                label=f"inventory.instances[{index}].coverage.action_events",
            )
            if (
                required_coverage.get("status") != "COMPLETE"
                or required_coverage.get("required") != required_streams
                or sorted(required_coverage.get("available", []))
                != required_streams
                or any(
                    required_coverage.get(field) != []
                    for field in ("fetch_required", "missing", "uncaptured")
                )
                or action_coverage.get("status") != "COMPLETE"
                or action_coverage.get("required")
                != list(history_v1.ACTION_STREAMS)
                or sorted(action_coverage.get("available", []))
                != list(history_v1.ACTION_STREAMS)
                or any(
                    action_coverage.get(field) != []
                    for field in ("fetch_required", "missing", "uncaptured")
                )
                or missing_plan.get("required_streams") != []
                or missing_plan.get("required_streams_fetch_required") != []
            ):
                raise ChronicleExternalManifestUnionError(
                    f"inventory training candidate {instance_id} lacks complete stream/action evidence"
                )
            status = ranking.get("status")
            usable = ranking.get("usable_for_exact_dps_training")
            if not isinstance(usable, bool):
                raise ChronicleExternalManifestUnionError(
                    f"inventory training candidate {instance_id} lacks boolean exact-DPS usability"
                )
            if status == dps_v1.EXACT_AVAILABLE:
                records = _array(
                    ranking.get("records"),
                    label=(
                        f"inventory.instances[{index}].coverage.ranking_exact_dps.records"
                    ),
                )
                if not usable:
                    raise ChronicleExternalManifestUnionError(
                        f"inventory training candidate {instance_id} has exact DPS but is not usable"
                    )
                exact_training_membership_count += 1
                exact_training_record_count += len(records)
            else:
                if usable:
                    raise ChronicleExternalManifestUnionError(
                        f"inventory training candidate {instance_id} marks missing exact DPS usable"
                    )
                if (
                    status
                    not in {
                        dps_v1.CAPTURED_GUID_ABSENT,
                        dps_v1.CAPTURED_GUID_ABSENT_NAME_CONFLICT,
                    }
                    or ranking.get("ranking_endpoint_captured") is not True
                    or ranking.get("ranking_fetch_required") is not False
                ):
                    raise ChronicleExternalManifestUnionError(
                        f"inventory training candidate {instance_id} has unresolved exact-DPS evidence"
                    )
                censored_training_membership_count += 1
        state = by_id.setdefault(
            instance_id,
            {
                "training": set(),
                "labels": set(),
                "fetch_planned": set(),
                "identity": set(),
            },
        )
        state["training"].add(training)
        state["labels"].add(label)
        state["fetch_planned"].add(fetch_planned)
        identity = {
            "name": row.get("name"),
            "slug": row.get("slug"),
            "started_at": row.get("started_at"),
            "guild": row.get("guild"),
        }
        state["identity"].add(_canonical_document_bytes(identity))

    for instance_id, state in by_id.items():
        if (
            len(state["training"]) != 1
            or len(state["labels"]) != 1
            or len(state["fetch_planned"]) != 1
            or len(state["identity"]) != 1
        ):
            raise ChronicleExternalManifestUnionError(
                f"inventory memberships disagree for raid instance {instance_id}"
            )
    training_ids = sorted(
        instance_id
        for instance_id, state in by_id.items()
        if state["training"] == {True}
    )
    union_ids = set(union_instances)
    missing_from_union = sorted(set(training_ids) - union_ids)
    if missing_from_union:
        raise ChronicleExternalManifestUnionError(
            "inventory training cohort is not a subset of the raw union: "
            + ", ".join(missing_from_union)
        )
    for instance_id in training_ids:
        raw = union_instances[instance_id]
        state = by_id[instance_id]
        identity_bytes = next(iter(state["identity"]))
        identity = _json_object(identity_bytes, label=f"inventory identity {instance_id}")
        guild = identity.get("guild")
        guild_name = guild.get("name") if isinstance(guild, Mapping) else None
        comparisons = {
            "instance_name": (raw.get("instance_name"), identity.get("name")),
            "slug": (raw.get("slug"), identity.get("slug")),
            "started_at": (raw.get("started_at"), identity.get("started_at")),
            "instance_contamination_label": (
                raw.get("instance_contamination_label"),
                next(iter(state["labels"])),
            ),
            "contamination_guild_context": (
                raw.get("contamination_guild_context"),
                guild_name,
            ),
        }
        mismatches = [
            field for field, (raw_value, inventory_value) in comparisons.items()
            if raw_value != inventory_value
        ]
        if mismatches:
            raise ChronicleExternalManifestUnionError(
                f"raw/inventory identity mismatch for training instance {instance_id}: {mismatches}"
            )
        streams = _mapping(
            raw.get("streams"), label=f"raw instance {instance_id}.streams"
        )
        for stream_type in required_streams:
            wrapper = _mapping(
                streams.get(stream_type),
                label=f"raw instance {instance_id}.streams.{stream_type}",
            )
            if wrapper.get("status") != "AVAILABLE" or not isinstance(
                wrapper.get("object"), Mapping
            ):
                raise ChronicleExternalManifestUnionError(
                    f"training instance {instance_id} required stream {stream_type} is not AVAILABLE"
                )
    metrics = {
        "candidate_membership_count": candidate_membership_count,
        "exact_training_membership_count": exact_training_membership_count,
        "censored_training_membership_count": censored_training_membership_count,
        "exact_training_record_count": exact_training_record_count,
        "required_stream_count": len(required_streams),
    }
    return training_ids, by_id, metrics


def _binding(
    manifest: Mapping[str, Any],
    resolved: Path,
    data_root: Path,
    *,
    label: str,
    content_address_scope: str,
) -> dict[str, Any]:
    content_sha = _verify_content_address(
        manifest, label=label, expected_scope=content_address_scope
    )
    try:
        size_bytes = resolved.stat().st_size
    except OSError as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot stat {label} {resolved}: {error}"
        ) from error
    return {
        "schema": manifest.get("schema"),
        "implementation_revision": manifest.get("implementation_revision"),
        "content_sha256": content_sha,
        "file_sha256": _sha256_file(resolved),
        "size_bytes": size_bytes,
        "path": _safe_relative(resolved, data_root, label=label),
    }


def _load_inventory(
    path: str | Path, *, data_root: Path
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    try:
        manifest, resolved = history_v1.load_character_instance_inventory_manifest(
            path, data_root=data_root
        )
    except (history_v1.CharacterHistoryError, OSError) as error:
        raise ChronicleExternalManifestUnionError(
            f"inventory strict replay failed: {error}"
        ) from error
    if manifest.get("schema") != history_v1.INVENTORY_SCHEMA:
        raise ChronicleExternalManifestUnionError("inventory schema is unsupported")
    if manifest.get("kind") != history_v1.INVENTORY_KIND:
        raise ChronicleExternalManifestUnionError("inventory kind is unsupported")
    if manifest.get("implementation_revision") != history_v1.INVENTORY_IMPLEMENTATION_REVISION:
        raise ChronicleExternalManifestUnionError(
            "inventory is not the current boundary-aware implementation revision"
        )
    binding = _binding(
        manifest,
        resolved,
        data_root,
        label="inventory manifest",
        content_address_scope="canonical JSON excluding content_address",
    )
    expected_name = (
        f"{history_v1.INVENTORY_MANIFEST_PREFIX}."
        f"{binding['content_sha256']}.manifest.json"
    )
    if resolved.name != expected_name:
        raise ChronicleExternalManifestUnionError(
            "inventory manifest filename/content address mismatch"
        )
    return manifest, resolved, binding


def _load_dps_index(
    path: str | Path,
    *,
    data_root: Path,
    inventory_path: Path,
    inventory_binding: Mapping[str, Any],
    zstd_executable: str | Path | None,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    try:
        manifest, resolved = dps_v1.load_character_dps_index_manifest(
            path, data_root=data_root, zstd_executable=zstd_executable
        )
    except (dps_v1.CharacterDpsIndexError, OSError) as error:
        raise ChronicleExternalManifestUnionError(
            f"DPS index strict replay failed: {error}"
        ) from error
    if (
        manifest.get("schema") != dps_v1.SCHEMA
        or manifest.get("kind") != dps_v1.KIND
        or manifest.get("implementation_revision") != dps_v1.IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalManifestUnionError("DPS index contract is unsupported")
    binding = _binding(
        manifest,
        resolved,
        data_root,
        label="DPS index manifest",
        content_address_scope="canonical JSON document excluding content_address",
    )
    expected_name = f"{dps_v1.MANIFEST_PREFIX}.{binding['content_sha256']}.manifest.json"
    if resolved.name != expected_name:
        raise ChronicleExternalManifestUnionError(
            "DPS index manifest filename/content address mismatch"
        )

    source = _mapping(manifest.get("source_inventory"), label="DPS source_inventory")
    expected_source = dict(inventory_binding)
    if dict(source) != expected_source:
        raise ChronicleExternalManifestUnionError(
            "DPS index does not bind the exact supplied inventory"
        )
    source_path = (data_root / str(source["path"])).resolve()
    if source_path != inventory_path.resolve():
        raise ChronicleExternalManifestUnionError(
            "DPS index inventory path does not resolve to the supplied inventory"
        )
    partition = _mapping(manifest.get("partition"), label="DPS index.partition")
    binding["partition_logical_content_sha256"] = _digest(
        partition.get("logical_content_sha256"),
        label="DPS index partition logical SHA-256",
    )
    binding["partition_compressed_file_sha256"] = _digest(
        partition.get("compressed_file_sha256"),
        label="DPS index partition compressed SHA-256",
    )
    binding["partition_logical_size_bytes"] = _integer(
        partition.get("logical_size_bytes"),
        label="DPS index partition logical_size_bytes",
    )
    binding["partition_compressed_size_bytes"] = _integer(
        partition.get("compressed_size_bytes"),
        label="DPS index partition compressed_size_bytes",
    )
    binding["partition_record_count"] = _integer(
        partition.get("record_count"), label="DPS index partition record_count"
    )
    binding["source_inventory_content_sha256"] = inventory_binding[
        "content_sha256"
    ]
    return manifest, resolved, binding


def _source_binding(
    resolved: Path,
    payload: bytes,
    digest: str,
    manifest: Mapping[str, Any],
    *,
    data_root: Path,
) -> dict[str, Any]:
    return {
        "schema": RAW_SCHEMA,
        "implementation_revision": RAW_IMPLEMENTATION_REVISION,
        "parser_contract_revision": RAW_PARSER_CONTRACT_REVISION,
        "sha256": digest,
        "size_bytes": len(payload),
        "media_type": "application/json",
        "relative_path": _safe_relative(
            resolved, _raw_root(data_root), label="source raw manifest"
        ),
        "instance_count": len(_array(manifest.get("instances"), label="instances")),
    }


def _build_raw_union(
    manifests: Sequence[Mapping[str, Any]],
    source_bindings: Sequence[Mapping[str, Any]],
    *,
    scope_name: str,
    population: str,
) -> tuple[dict[str, Any], int]:
    instances, duplicate_count = _union_instances(manifests)
    instance_ids = [str(row["instance_id"]) for row in instances]
    recent_pages = _dedupe_exact_rows(
        [row for manifest in manifests for row in manifest["recent_pages"]],
        label="recent_pages",
    )
    leaderboard_snapshots = _dedupe_exact_rows(
        [
            row
            for manifest in manifests
            for row in manifest["leaderboard_snapshots"]
        ],
        label="leaderboard_snapshots",
    )
    instance_names = sorted(
        {
            str(row["instance_name"])
            for row in instances
            if isinstance(row.get("instance_name"), str)
            and str(row["instance_name"]).strip()
        }
    )
    scope = ingest_v1._scope_descriptor(
        scope_name=scope_name,
        instance_names=instance_names,
        realm_id=None,
        guild_id=None,
        has_video=None,
    )
    stream_sets = [
        set(row.get("streams", {}))
        for row in instances
        if isinstance(row.get("streams"), Mapping)
    ]
    common_streams = sorted(set.intersection(*stream_sets)) if stream_sets else []
    all_have_ranking = bool(instances) and all(
        isinstance(row.get("ranking_records"), Mapping) for row in instances
    )
    bindings = sorted(
        (deepcopy(dict(binding)) for binding in source_bindings),
        key=lambda row: str(row["sha256"]),
    )
    source_shas = [str(row["sha256"]) for row in bindings]
    return (
        {
            "schema": RAW_SCHEMA,
            "implementation_revision": RAW_IMPLEMENTATION_REVISION,
            "parser_contract_revision": RAW_PARSER_CONTRACT_REVISION,
            "kind": RAW_KIND,
            "api_base": ingest_v1.EXTERNAL_API_BASE,
            "cursor_contract": deepcopy(_CURRENT_CURSOR_CONTRACT),
            "contamination_contract": ingest_v1._contamination_contract(),
            "request": {
                "scope": scope,
                "upload_after": None,
                "page_size": 0,
                "max_pages": 0,
                "max_instances": len(instances),
                "explicit_instance_ids": instance_ids,
                "stream_types": common_streams,
                "include_ranking_records": all_have_ranking,
            },
            "recent_pages": recent_pages,
            "leaderboard_snapshots": leaderboard_snapshots,
            "instances": instances,
            "replay_provenance": {
                "method": "deterministic_union_of_current_locally_replayed_raw_manifests",
                "network_requests_made": 0,
                "source_manifest_sha256": source_shas,
                "source_manifest_adoption_status": "CURRENT_REVISIONS_ONLY_NO_MIGRATION",
            },
            "union_contract": {
                "scope_name": scope_name,
                "population": population,
                "source_manifests": bindings,
                "source_manifest_count": len(bindings),
                "source_instance_row_count": sum(
                    int(row["instance_count"]) for row in bindings
                ),
                "duplicate_instance_row_count": duplicate_count,
                "distinct_instance_count": len(instances),
                "instance_deduplication_key": "instance_id",
                "duplicate_acceptance": "complete_instance_rows_must_be_equal",
                "descriptive_instances_retained": True,
                "training_eligibility_embedded": False,
            },
        },
        duplicate_count,
    )


def _validate_assertions(
    *,
    union_ids: Sequence[str],
    training_ids: Sequence[str],
    expect_union_count: int | None,
    expect_training_count: int | None,
    expect_nontraining_instance_ids: Sequence[str],
) -> dict[str, Any]:
    union_set = set(union_ids)
    training_set = set(training_ids)
    expected_nontraining = [
        _text(value, label=f"expected nontraining instance ID[{index}]")
        for index, value in enumerate(expect_nontraining_instance_ids)
    ]
    if len(expected_nontraining) != len(set(expected_nontraining)):
        raise ChronicleExternalManifestUnionError(
            "expected nontraining instance IDs must be unique"
        )
    if expect_union_count is not None and len(union_ids) != expect_union_count:
        raise ChronicleExternalManifestUnionError(
            f"raw union count assertion failed: {len(union_ids)} != {expect_union_count}"
        )
    if expect_training_count is not None and len(training_ids) != expect_training_count:
        raise ChronicleExternalManifestUnionError(
            "training cohort count assertion failed: "
            f"{len(training_ids)} != {expect_training_count}"
        )
    for instance_id in expected_nontraining:
        if instance_id not in union_set:
            raise ChronicleExternalManifestUnionError(
                f"expected nontraining instance is absent from the raw union: {instance_id}"
            )
        if instance_id in training_set:
            raise ChronicleExternalManifestUnionError(
                f"expected nontraining instance entered the training cohort: {instance_id}"
            )
    return {
        "status": "PASS",
        "expected_union_count": expect_union_count,
        "expected_training_count": expect_training_count,
        "expected_nontraining_instance_ids": sorted(expected_nontraining),
    }


def _nontraining_reasons(
    nontraining_ids: Sequence[str], inventory_state: Mapping[str, Mapping[str, set[Any]]]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for instance_id in nontraining_ids:
        state = inventory_state.get(instance_id)
        if state is None:
            output.append(
                {
                    "instance_id": instance_id,
                    "reason": "ABSENT_FROM_BOUND_INVENTORY_DEFAULT_DESCRIPTIVE_NONTRAINING",
                    "inventory_contamination_labels": [],
                }
            )
        else:
            output.append(
                {
                    "instance_id": instance_id,
                    "reason": "BOUND_INVENTORY_EXPLICITLY_NONTRAINING",
                    "inventory_contamination_labels": sorted(
                        str(value) for value in state["labels"]
                    ),
                }
            )
    return output


def _id_list_block(definition: str, instance_ids: Sequence[str]) -> dict[str, Any]:
    ordered = list(instance_ids)
    if ordered != sorted(set(ordered)):
        raise ChronicleExternalManifestUnionError(
            "cohort instance IDs must be unique and sorted"
        )
    return {
        "definition": definition,
        "instance_count": len(ordered),
        "instance_ids": ordered,
        "instance_ids_sha256": _sha256(_canonical_document_bytes(ordered)),
        "instance_ids_hash_contract": "canonical JSON array plus LF",
    }


def _dps_training_metrics(
    inventory: Mapping[str, Any],
    dps_manifest: Mapping[str, Any],
    *,
    training_ids: set[str],
    inventory_metrics: Mapping[str, int],
) -> dict[str, int]:
    try:
        selected_row_count = sum(
            1
            for row in dps_v1.iter_character_dps_rows(inventory)
            if row.get("instance_id") in training_ids
        )
    except (dps_v1.CharacterDpsIndexError, OSError) as error:
        raise ChronicleExternalManifestUnionError(
            f"DPS training-row projection failed: {error}"
        ) from error
    if selected_row_count != inventory_metrics["exact_training_record_count"]:
        raise ChronicleExternalManifestUnionError(
            "DPS row projection disagrees with inventory exact training records"
        )
    summary = _mapping(dps_manifest.get("summary"), label="DPS index.summary")
    summary_exact_memberships = _integer(
        summary.get("training_eligible_exact_membership_count"),
        label="DPS training-eligible exact membership count",
    )
    if summary_exact_memberships != inventory_metrics["exact_training_membership_count"]:
        raise ChronicleExternalManifestUnionError(
            "DPS summary training membership count disagrees with the bound inventory"
        )
    return {
        "candidate_membership_count": inventory_metrics[
            "candidate_membership_count"
        ],
        "exact_training_membership_count": inventory_metrics[
            "exact_training_membership_count"
        ],
        "censored_or_exact_missing_training_membership_count": inventory_metrics[
            "censored_training_membership_count"
        ],
        "captured_exact_GUID_absent_training_membership_count": inventory_metrics[
            "censored_training_membership_count"
        ],
        "selected_exact_DPS_row_count": selected_row_count,
        "required_stream_count": inventory_metrics["required_stream_count"],
    }


def _receipt_core(
    *,
    union: Mapping[str, Any],
    union_path: Path,
    union_payload: bytes,
    manifests: Sequence[Mapping[str, Any]],
    source_shas: Sequence[str],
    duplicate_count: int,
    inventory_binding: Mapping[str, Any],
    dps_binding: Mapping[str, Any],
    inventory_state: Mapping[str, Mapping[str, set[Any]]],
    training_ids: Sequence[str],
    dps_training_metrics: Mapping[str, int],
    assertions: Mapping[str, Any],
    scope_name: str,
    population: str,
    data_root: Path,
) -> dict[str, Any]:
    union_instances_array = _array(union.get("instances"), label="raw union.instances")
    union_ids = [
        _text(row.get("instance_id"), label=f"raw union.instances[{index}].instance_id")
        for index, row in enumerate(union_instances_array)
        if isinstance(row, Mapping)
    ]
    if len(union_ids) != len(union_instances_array) or union_ids != sorted(set(union_ids)):
        raise ChronicleExternalManifestUnionError(
            "raw union instances must have unique sorted instance IDs"
        )
    training_ids = list(training_ids)
    training_set = set(training_ids)
    nontraining_ids = sorted(set(union_ids) - training_set)
    union_sha = _sha256(union_payload)
    union_binding = {
        "schema": RAW_SCHEMA,
        "implementation_revision": RAW_IMPLEMENTATION_REVISION,
        "parser_contract_revision": RAW_PARSER_CONTRACT_REVISION,
        "kind": RAW_KIND,
        "file_sha256": union_sha,
        "size_bytes": len(union_payload),
        "path": _safe_relative(union_path, data_root, label="raw union manifest"),
        "source_manifest_sha256": sorted(source_shas),
        "source_manifest_count": len(source_shas),
        "source_instance_row_count": sum(
            len(_array(manifest["instances"], label="source instances"))
            for manifest in manifests
        ),
        "duplicate_instance_row_count": duplicate_count,
        "distinct_instance_count": len(union_ids),
        "distinct_instance_ids_sha256": _sha256(_canonical_document_bytes(union_ids)),
        "distinct_instance_ids_hash_contract": "canonical JSON array plus LF",
    }
    raw_sources_by_instance: dict[str, list[str]] = {}
    raw_row_sha_by_instance: dict[str, str] = {}
    for manifest, source_sha in zip(manifests, source_shas, strict=True):
        for row in manifest["instances"]:
            instance_id = str(row["instance_id"])
            raw_sources_by_instance.setdefault(instance_id, []).append(source_sha)
            row_sha = _sha256(_canonical_document_bytes(row))
            prior_row_sha = raw_row_sha_by_instance.setdefault(instance_id, row_sha)
            if prior_row_sha != row_sha:
                raise ChronicleExternalManifestUnionError(
                    f"raw row identity changed for {instance_id}"
                )
    selected_instance_bindings = [
        {
            "instance_id": instance_id,
            "raw_instance_row_sha256": raw_row_sha_by_instance[instance_id],
            "source_manifest_sha256": sorted(raw_sources_by_instance[instance_id]),
        }
        for instance_id in training_ids
    ]
    descriptive_block = _id_list_block(
        "all unique instance IDs retained by the raw union", union_ids
    )
    training_block = _id_list_block(
        (
            "unique instance IDs explicitly marked training_candidate=true "
            "by the bound inventory; raw contamination labels never add IDs"
        ),
        training_ids,
    )
    training_block["selected_instances"] = selected_instance_bindings
    training_block["evidence_counts"] = dict(dps_training_metrics)
    nontraining_block = _id_list_block(
        (
            "raw union IDs absent from the bound inventory training cohort, "
            "including raw-only IDs regardless of their contamination label"
        ),
        nontraining_ids,
    )
    nontraining_block["reasons"] = _nontraining_reasons(
        nontraining_ids, inventory_state
    )
    return {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "scope": {"scope_name": scope_name, "population": population},
        "raw_union": union_binding,
        "source_inventory": dict(inventory_binding),
        "source_dps_index": dict(dps_binding),
        "cohorts": {
            "descriptive": descriptive_block,
            "training": training_block,
            "descriptive_nontraining": nontraining_block,
        },
        "assertions": dict(assertions),
        "evidence_contract": {
            "raw_union_is_descriptive": True,
            "inventory_is_training_cohort_authority": True,
            "raw_label_used_to_expand_training_cohort": False,
            "raw_only_instance_default": "DESCRIPTIVE_NONTRAINING",
            "DPS_index_binds_exact_inventory": True,
            "training_ids_subset_of_raw_union": True,
            "duplicate_instance_acceptance": "complete_rows_equal_only",
            "network_requests_made": 0,
        },
        "publication_contract": {
            "source_objects_preexist": True,
            "raw_union_content_addressed_by_canonical_file_sha256": True,
            "raw_union_published_before_receipt": True,
            "receipt_content_addressed": True,
            "receipt_published_last": True,
            "mutable_stable_pointer_written": False,
        },
        "scientific_status": {
            "descriptive_evidence_ready_for_downstream_rebuild": True,
            "training_or_comparison_authorized": False,
            "superiority_claim": False,
        },
    }


def publish_manifest_union(
    source_manifest_paths: Sequence[str | Path],
    inventory_manifest_path: str | Path,
    dps_index_manifest_path: str | Path,
    *,
    scope_name: str,
    population: str,
    data_root: Path = DEFAULT_DATA_ROOT,
    expect_union_count: int | None = None,
    expect_training_count: int | None = None,
    expect_nontraining_instance_ids: Sequence[str] = (),
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Publish a current raw union, then its independently addressed receipt."""

    root = _data_root(Path(data_root))
    scope_name = _text(scope_name, label="scope_name")
    population = _text(population, label="population")
    if expect_union_count is not None:
        _integer(expect_union_count, label="expect_union_count", minimum=1)
    if expect_training_count is not None:
        _integer(expect_training_count, label="expect_training_count", minimum=0)
    if len(source_manifest_paths) < 2:
        raise ChronicleExternalManifestUnionError(
            "manifest union requires at least two source raw manifests"
        )

    loaded = [
        _load_current_raw_manifest(path, data_root=root)
        for path in source_manifest_paths
    ]
    source_shas = [row[3] for row in loaded]
    if len(source_shas) != len(set(source_shas)):
        raise ChronicleExternalManifestUnionError(
            "source raw manifests must have unique content addresses"
        )
    manifests = [row[0] for row in loaded]
    source_bindings = [
        _source_binding(
            resolved,
            payload,
            digest,
            manifest,
            data_root=root,
        )
        for manifest, resolved, payload, digest in loaded
    ]
    union, duplicate_count = _build_raw_union(
        manifests,
        source_bindings,
        scope_name=scope_name,
        population=population,
    )
    union_ids = [str(row["instance_id"]) for row in union["instances"]]

    inventory, inventory_path, inventory_binding = _load_inventory(
        inventory_manifest_path, data_root=root
    )
    inventory_sources = _mapping(
        inventory.get("source_bindings"), label="inventory.source_bindings"
    )
    inventory_ingest_shas = {
        _digest(value, label=f"inventory ingest binding[{index}]")
        for index, value in enumerate(
            _array(
                inventory_sources.get("ingest_manifest_sha256"),
                label="inventory ingest source bindings",
            )
        )
    }
    missing_source_bindings = sorted(set(source_shas) - inventory_ingest_shas)
    if missing_source_bindings:
        raise ChronicleExternalManifestUnionError(
            "bound inventory does not include every raw union source manifest: "
            + ", ".join(missing_source_bindings)
        )
    union_instances = {
        str(row["instance_id"]): row for row in union["instances"]
    }
    training_ids, inventory_state, inventory_metrics = _inventory_training_cohort(
        inventory, union_instances=union_instances
    )
    training_set = set(training_ids)
    nontraining_ids = sorted(set(union_ids) - training_set)
    dps_manifest, _, dps_binding = _load_dps_index(
        dps_index_manifest_path,
        data_root=root,
        inventory_path=inventory_path,
        inventory_binding=inventory_binding,
        zstd_executable=zstd_executable,
    )
    dps_training_metrics = _dps_training_metrics(
        inventory,
        dps_manifest,
        training_ids=training_set,
        inventory_metrics=inventory_metrics,
    )
    assertions = _validate_assertions(
        union_ids=union_ids,
        training_ids=training_ids,
        expect_union_count=expect_union_count,
        expect_training_count=expect_training_count,
        expect_nontraining_instance_ids=expect_nontraining_instance_ids,
    )

    union_payload = _canonical_document_bytes(union)
    union_sha = _sha256(union_payload)
    union_path = _raw_root(root) / "manifests" / f"{union_sha}.json"
    try:
        ingest_v1._atomic_write_bytes(union_path, union_payload)
        replay = ingest_v1.replay_manifest_from_local_raw(
            union_path, data_root=root
        )
        if (
            replay.get("status") != "ALREADY_CURRENT_LOCAL_RAW"
            or replay.get("manifest_sha256") != union_sha
            or replay.get("network_requests_made") != 0
            or replay.get("watermark_mutated") is not False
        ):
            raise ChronicleExternalManifestUnionError(
                "published raw union did not replay as a current offline fixed point"
            )
    except (
        ingest_v1.ChronicleIngestError,
        ChronicleExternalManifestUnionError,
        OSError,
    ) as error:
        # Never delete a content-addressed final path here: another process may
        # have concurrently committed the same bytes.  Without the receipt the
        # union is an unadopted immutable orphan, not a consumable cohort.
        if isinstance(error, ChronicleExternalManifestUnionError):
            raise
        raise ChronicleExternalManifestUnionError(
            f"raw union local replay failed: {error}"
        ) from error

    receipt_core = _receipt_core(
        union=union,
        union_path=union_path,
        union_payload=union_payload,
        manifests=manifests,
        source_shas=source_shas,
        duplicate_count=duplicate_count,
        inventory_binding=inventory_binding,
        dps_binding=dps_binding,
        inventory_state=inventory_state,
        training_ids=training_ids,
        dps_training_metrics=dps_training_metrics,
        assertions=assertions,
        scope_name=scope_name,
        population=population,
        data_root=root,
    )
    receipt = _content_addressed(receipt_core)
    receipt_sha = _verify_content_address(receipt, label="union cohort receipt")
    receipt_payload = _canonical_document_bytes(receipt)
    receipt_directory = root / Path(OUTPUT_DIRECTORY)
    receipt_path = receipt_directory / (
        f"{MANIFEST_PREFIX}.{receipt_sha}.manifest.json"
    )
    try:
        ingest_v1._atomic_write_bytes(receipt_path, receipt_payload)
    except (ingest_v1.ChronicleIngestError, OSError) as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot publish union cohort receipt: {error}"
        ) from error

    return {
        "schema": SCHEMA,
        "status": "PASS_OFFLINE_UNION_AND_COHORT_BOUND",
        "raw_union_manifest_sha256": union_sha,
        "raw_union_manifest_path": str(union_path),
        "receipt_content_sha256": receipt_sha,
        "receipt_manifest_path": str(receipt_path),
        "source_manifest_count": len(source_shas),
        "source_instance_row_count": receipt_core["raw_union"][
            "source_instance_row_count"
        ],
        "duplicate_instance_row_count": duplicate_count,
        "descriptive_instance_count": len(union_ids),
        "training_instance_count": len(training_ids),
        "descriptive_nontraining_instance_count": len(nontraining_ids),
        "network_requests_made": 0,
        "training_or_comparison_authorized": False,
    }


def _resolve_bound_file(
    data_root: Path,
    relative_value: Any,
    *,
    expected_parent: Path,
    label: str,
) -> Path:
    relative_text = _text(relative_value, label=f"{label}.path")
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalManifestUnionError(f"{label} path escapes data_root")
    requested = data_root / relative
    if requested.is_symlink():
        raise ChronicleExternalManifestUnionError(f"{label} path must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot resolve {label} path {requested}: {error}"
        ) from error
    if not resolved.is_file() or resolved.parent != expected_parent.resolve():
        raise ChronicleExternalManifestUnionError(
            f"{label} path is outside its expected immutable manifest directory"
        )
    return resolved


def audit_manifest_union_receipt(
    receipt_manifest_path: str | Path,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    zstd_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Strictly replay a receipt from every bound local source and cohort rule."""

    root = _data_root(Path(data_root))
    resolved = _regular_resolved_file(
        receipt_manifest_path, label="union cohort receipt"
    )
    expected_directory = (root / Path(OUTPUT_DIRECTORY)).resolve()
    if resolved.parent != expected_directory:
        raise ChronicleExternalManifestUnionError(
            "union cohort receipt is outside its expected manifest directory"
        )
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise ChronicleExternalManifestUnionError(
            f"cannot read union cohort receipt {resolved}: {error}"
        ) from error
    receipt = _json_object(payload, label="union cohort receipt")
    if payload != _canonical_document_bytes(receipt):
        raise ChronicleExternalManifestUnionError(
            "union cohort receipt is not canonical JSON"
        )
    if (
        receipt.get("schema") != SCHEMA
        or receipt.get("implementation_revision") != IMPLEMENTATION_REVISION
        or receipt.get("kind") != KIND
    ):
        raise ChronicleExternalManifestUnionError(
            "union cohort receipt schema/kind/revision is unsupported"
        )
    content_sha = _verify_content_address(receipt, label="union cohort receipt")
    if resolved.name != f"{MANIFEST_PREFIX}.{content_sha}.manifest.json":
        raise ChronicleExternalManifestUnionError(
            "union cohort receipt filename/content address mismatch"
        )

    scope = _mapping(receipt.get("scope"), label="receipt.scope")
    if set(scope) != {"scope_name", "population"}:
        raise ChronicleExternalManifestUnionError("receipt scope schema is invalid")
    scope_name = _text(scope.get("scope_name"), label="receipt.scope.scope_name")
    population = _text(scope.get("population"), label="receipt.scope.population")

    raw_binding = _mapping(receipt.get("raw_union"), label="receipt.raw_union")
    union_path = _resolve_bound_file(
        root,
        raw_binding.get("path"),
        expected_parent=_raw_root(root) / "manifests",
        label="raw union manifest",
    )
    union, loaded_union_path, union_payload, union_sha = _load_current_raw_manifest(
        union_path, data_root=root
    )
    if loaded_union_path != union_path:
        raise ChronicleExternalManifestUnionError("raw union resolved path changed")
    if (
        raw_binding.get("file_sha256") != union_sha
        or raw_binding.get("size_bytes") != len(union_payload)
    ):
        raise ChronicleExternalManifestUnionError(
            "receipt raw union file hash/size binding changed"
        )

    union_contract = _mapping(
        union.get("union_contract"), label="raw union.union_contract"
    )
    raw_source_rows = _array(
        union_contract.get("source_manifests"),
        label="raw union.union_contract.source_manifests",
    )
    if len(raw_source_rows) < 2:
        raise ChronicleExternalManifestUnionError(
            "raw union must bind at least two source manifests"
        )
    loaded_sources: list[tuple[dict[str, Any], Path, bytes, str]] = []
    for index, raw_source in enumerate(raw_source_rows):
        source = _mapping(raw_source, label=f"raw union source[{index}]")
        source_path = _resolve_bound_file(
            _raw_root(root),
            source.get("relative_path"),
            expected_parent=_raw_root(root) / "manifests",
            label=f"raw union source[{index}]",
        )
        loaded = _load_current_raw_manifest(source_path, data_root=root)
        expected_source_binding = _source_binding(
            loaded[1], loaded[2], loaded[3], loaded[0], data_root=root
        )
        if dict(source) != expected_source_binding:
            raise ChronicleExternalManifestUnionError(
                f"raw union source[{index}] binding changed"
            )
        loaded_sources.append(loaded)
    source_shas = [row[3] for row in loaded_sources]
    if source_shas != sorted(set(source_shas)):
        raise ChronicleExternalManifestUnionError(
            "raw union source bindings must be unique and sorted by SHA-256"
        )
    source_manifests = [row[0] for row in loaded_sources]
    source_bindings = [
        _source_binding(row[1], row[2], row[3], row[0], data_root=root)
        for row in loaded_sources
    ]
    rebuilt_union, duplicate_count = _build_raw_union(
        source_manifests,
        source_bindings,
        scope_name=scope_name,
        population=population,
    )
    if union != rebuilt_union:
        raise ChronicleExternalManifestUnionError(
            "raw union differs from deterministic source-manifest replay"
        )
    union_instances = {
        str(row["instance_id"]): row for row in rebuilt_union["instances"]
    }
    union_ids = sorted(union_instances)

    inventory_receipt_binding = _mapping(
        receipt.get("source_inventory"), label="receipt.source_inventory"
    )
    inventory_path = _resolve_bound_file(
        root,
        inventory_receipt_binding.get("path"),
        expected_parent=root / Path(history_v1.INVENTORY_MANIFEST_DIRECTORY),
        label="inventory manifest",
    )
    inventory, loaded_inventory_path, inventory_binding = _load_inventory(
        inventory_path, data_root=root
    )
    if loaded_inventory_path != inventory_path or dict(inventory_receipt_binding) != inventory_binding:
        raise ChronicleExternalManifestUnionError(
            "receipt inventory binding changed"
        )
    inventory_sources = _mapping(
        inventory.get("source_bindings"), label="inventory.source_bindings"
    )
    inventory_ingest_shas = {
        _digest(value, label=f"inventory ingest binding[{index}]")
        for index, value in enumerate(
            _array(
                inventory_sources.get("ingest_manifest_sha256"),
                label="inventory ingest source bindings",
            )
        )
    }
    if not set(source_shas).issubset(inventory_ingest_shas):
        raise ChronicleExternalManifestUnionError(
            "receipt inventory no longer binds every raw union source"
        )
    training_ids, inventory_state, inventory_metrics = _inventory_training_cohort(
        inventory, union_instances=union_instances
    )

    dps_receipt_binding = _mapping(
        receipt.get("source_dps_index"), label="receipt.source_dps_index"
    )
    dps_path = _resolve_bound_file(
        root,
        dps_receipt_binding.get("path"),
        expected_parent=(
            root / dps_v1.OUTPUT_DIRECTORY / dps_v1.MANIFEST_DIRECTORY
        ),
        label="DPS index manifest",
    )
    dps_manifest, loaded_dps_path, dps_binding = _load_dps_index(
        dps_path,
        data_root=root,
        inventory_path=inventory_path,
        inventory_binding=inventory_binding,
        zstd_executable=zstd_executable,
    )
    if loaded_dps_path != dps_path or dict(dps_receipt_binding) != dps_binding:
        raise ChronicleExternalManifestUnionError("receipt DPS index binding changed")
    dps_training_metrics = _dps_training_metrics(
        inventory,
        dps_manifest,
        training_ids=set(training_ids),
        inventory_metrics=inventory_metrics,
    )

    assertions = _mapping(receipt.get("assertions"), label="receipt.assertions")
    if set(assertions) != {
        "status",
        "expected_union_count",
        "expected_training_count",
        "expected_nontraining_instance_ids",
    }:
        raise ChronicleExternalManifestUnionError("receipt assertions schema is invalid")
    expected_union_count = assertions.get("expected_union_count")
    if expected_union_count is not None:
        _integer(expected_union_count, label="expected_union_count", minimum=1)
    expected_training_count = assertions.get("expected_training_count")
    if expected_training_count is not None:
        _integer(expected_training_count, label="expected_training_count", minimum=0)
    expected_nontraining = _array(
        assertions.get("expected_nontraining_instance_ids"),
        label="expected nontraining instance IDs",
    )
    rebuilt_assertions = _validate_assertions(
        union_ids=union_ids,
        training_ids=training_ids,
        expect_union_count=expected_union_count,
        expect_training_count=expected_training_count,
        expect_nontraining_instance_ids=expected_nontraining,
    )
    if dict(assertions) != rebuilt_assertions:
        raise ChronicleExternalManifestUnionError(
            "receipt assertions differ from deterministic replay"
        )

    expected_core = _receipt_core(
        union=rebuilt_union,
        union_path=union_path,
        union_payload=union_payload,
        manifests=source_manifests,
        source_shas=source_shas,
        duplicate_count=duplicate_count,
        inventory_binding=inventory_binding,
        dps_binding=dps_binding,
        inventory_state=inventory_state,
        training_ids=training_ids,
        dps_training_metrics=dps_training_metrics,
        assertions=rebuilt_assertions,
        scope_name=scope_name,
        population=population,
        data_root=root,
    )
    expected_receipt = _content_addressed(expected_core)
    if receipt != expected_receipt:
        raise ChronicleExternalManifestUnionError(
            "union cohort receipt differs from deterministic full-source replay"
        )
    cohorts = _mapping(receipt.get("cohorts"), label="receipt.cohorts")
    descriptive = _mapping(cohorts.get("descriptive"), label="descriptive cohort")
    training = _mapping(cohorts.get("training"), label="training cohort")
    nontraining = _mapping(
        cohorts.get("descriptive_nontraining"), label="nontraining cohort"
    )
    return {
        "schema": SCHEMA,
        "status": "PASS_STRICT_FULL_SOURCE_REPLAY",
        "receipt_content_sha256": content_sha,
        "receipt_manifest_path": str(resolved),
        "raw_union_manifest_sha256": union_sha,
        "raw_union_manifest_path": str(union_path),
        "source_manifest_count": len(source_shas),
        "descriptive_instance_count": descriptive["instance_count"],
        "training_instance_count": training["instance_count"],
        "descriptive_nontraining_instance_count": nontraining["instance_count"],
        "network_requests_made": 0,
        "training_or_comparison_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Union current Chronicle raw manifests and bind an inventory-derived "
            "training cohort without network access"
        )
    )
    parser.add_argument("--audit-receipt", type=Path)
    parser.add_argument("--source-manifest", action="append", type=Path, default=[])
    parser.add_argument("--inventory-manifest", type=Path)
    parser.add_argument("--dps-index-manifest", type=Path)
    parser.add_argument("--scope-name")
    parser.add_argument("--population")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--expect-union-count", type=int)
    parser.add_argument("--expect-training-count", type=int)
    parser.add_argument(
        "--expect-nontraining-instance-id", action="append", default=[]
    )
    parser.add_argument("--zstd")
    return parser


def _write_output(
    value: Mapping[str, Any],
    *,
    stdout: TextIO | None,
    stdout_buffer: BinaryIO | None,
) -> None:
    payload = _canonical_document_bytes(value)
    if stdout is not None:
        if stdout_buffer is not None:
            raise ChronicleExternalManifestUnionError(
                "stdout and stdout_buffer are mutually exclusive"
            )
        stdout.write(payload.decode("utf-8"))
        return
    target = stdout_buffer if stdout_buffer is not None else getattr(sys.stdout, "buffer", None)
    if target is not None:
        target.write(payload)
    else:  # pragma: no cover - embedded text-only hosts
        sys.stdout.write(payload.decode("utf-8"))


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdout_buffer: BinaryIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    build_values = (
        args.source_manifest,
        args.inventory_manifest,
        args.dps_index_manifest,
        args.scope_name,
        args.population,
        args.expect_union_count,
        args.expect_training_count,
        args.expect_nontraining_instance_id,
    )
    if args.audit_receipt is not None:
        if any(
            value not in (None, [], ())
            for value in build_values
        ):
            raise ChronicleExternalManifestUnionError(
                "--audit-receipt cannot be combined with union build arguments"
            )
        result = audit_manifest_union_receipt(
            args.audit_receipt,
            data_root=args.data_root,
            zstd_executable=args.zstd,
        )
    else:
        if (
            len(args.source_manifest) < 2
            or args.inventory_manifest is None
            or args.dps_index_manifest is None
            or args.scope_name is None
            or args.population is None
        ):
            raise ChronicleExternalManifestUnionError(
                "build requires at least two --source-manifest values plus "
                "--inventory-manifest, --dps-index-manifest, --scope-name, and --population"
            )
        result = publish_manifest_union(
            args.source_manifest,
            args.inventory_manifest,
            args.dps_index_manifest,
            scope_name=args.scope_name,
            population=args.population,
            data_root=args.data_root,
            expect_union_count=args.expect_union_count,
            expect_training_count=args.expect_training_count,
            expect_nontraining_instance_ids=args.expect_nontraining_instance_id,
            zstd_executable=args.zstd,
        )
    _write_output(result, stdout=stdout, stdout_buffer=stdout_buffer)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except ChronicleExternalManifestUnionError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)


__all__ = [
    "ChronicleExternalManifestUnionError",
    "IMPLEMENTATION_REVISION",
    "KIND",
    "MANIFEST_PREFIX",
    "OUTPUT_DIRECTORY",
    "SCHEMA",
    "audit_manifest_union_receipt",
    "main",
    "publish_manifest_union",
]
