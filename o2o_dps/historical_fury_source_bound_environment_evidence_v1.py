"""Extract compact source-bound environment evidence for the nine Fury builds.

The exact source-build requests currently describe one generic 20.001 second
duration target.  Their historical decision support, however, can span many
successive hostile targets.  This module binds the requests to three already
published local evidence layers and materializes the mismatch before any
request is promoted to health mode:

* strict-prefix Fury decision states from the expert-episode adapter;
* target outcomes and lifecycle summaries from External-V2 reconstruction;
* exact-player damage events from the External-V2 team timeline, split into
  focal and leave-one-player-out lanes.

The output remains evidence-only.  A death-time damage sum is a retrospective
kill-budget proxy, not maximum health.  START/GO armor-debuff candidates are
not aura state.  Event activity is not an exact attackability schedule, and a
historical leave-one-out trace is not a counterfactual team-response model.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from . import chronicle_external_encounter_reconstruction_v2 as reconstruction_v2
from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_timeline_v2 as timeline_v2
from . import fury_offline_scenario_capsule_v2 as capsule_v2
from . import historical_fury_expert_episode_adapter_v1 as episode_v1
from . import historical_fury_source_bound_dynamic_config_v1 as dynamic_v1
from . import historical_fury_source_bound_prototype_bundle_v1 as source_v1


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_environment_evidence/v1"
ROW_SCHEMA = "historical_fury_source_bound_environment_evidence_row/v1"
KIND = "historical_fury_source_bound_environment_evidence_manifest"
STATUS = "EVIDENCE_AUDIT_COMPLETE_NOT_EXECUTABLE"
ROW_STATUS = "SOURCE_BOUND_EVIDENCE_BLOCKED_NOT_EXECUTABLE"
IMPLEMENTATION_REVISION = "v1.2_eventmeta_prefix_censored_lifecycle_loo_streaming"
CONTENT_ADDRESS_SCHEMA = "historical_fury_source_bound_environment_evidence_content/v1"
EXPECTED_REQUEST_COUNT = 9
GAP_SENSITIVITY_MS = (0, 1500, 3000, 5000)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DYNAMIC_MANIFEST = dynamic_v1.DEFAULT_OUTPUT
DEFAULT_SOURCE_BUNDLE = source_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_RECONSTRUCTION_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_encounter_reconstruction"
    / "v2"
    / "utk_postfix_dev_20260903_noon"
    / "manifest.json"
)
DEFAULT_EPISODE_MANIFEST = episode_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_TIMELINE_MANIFEST = dynamic_v1.DEFAULT_TEAM_TIMELINE
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_environment_evidence"
    / "v1"
)
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIRECTORY / "manifest.json"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_F130_RE = re.compile(r"^0xF130([0-9A-F]{6})", re.IGNORECASE)
_REQUIRED_BLOCKERS = frozenset(
    {
        "SOURCE_BOUND_EXECUTION_WINDOW_NOT_DECLARED",
        "SOURCE_BOUND_TARGET_REGISTRY_NOT_BOUND",
        "EXACT_INITIAL_OR_MAX_HEALTH_NOT_IDENTIFIED",
        "EXACT_BASE_OR_EFFECTIVE_ARMOR_NOT_IDENTIFIED",
        "EXACT_ATTACKABILITY_INTERVALS_NOT_IDENTIFIED",
        "COUNTERFACTUAL_TEAM_KILL_CLOCK_NOT_CALIBRATED",
    }
)


class HistoricalFurySourceBoundEnvironmentEvidenceV1Error(RuntimeError):
    """A selected source artifact or evidence-only output is inconsistent."""


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> JSONMap:
    result: JSONMap = {}
    for key, value in pairs:
        if key in result:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _strict_json(payload: bytes, label: str) -> JSONMap:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON token: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"cannot decode {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be an object"
        )
    return value


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        return _strict_json(path.read_bytes(), label)
    except OSError as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"cannot read {label}: {error}"
        ) from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be an integer"
        )
    if minimum is not None and value < minimum:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be >= {minimum}"
        )
    return value


def _sha(value: Any, label: str) -> str:
    result = _text(value, label)
    if _SHA_RE.fullmatch(result) is None:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must be a lowercase SHA-256"
        )
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _verify_content_address(value: Mapping[str, Any], label: str) -> str:
    address = _mapping(value.get("content_address"), f"{label}.content_address")
    if address.get("algorithm") != "sha256":
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} uses an unsupported content-address algorithm"
        )
    declared = _sha(address.get("sha256"), f"{label}.content_address.sha256")
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    if hashlib.sha256(_canonical_bytes(core)).hexdigest() != declared:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} content address differs"
        )
    return declared


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address; host locators forbidden",
            "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
        },
    }


def _looks_absolute(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        normalized.startswith("/")
        or normalized.startswith("//")
        or (
            len(normalized) > 2
            and normalized[1] == ":"
            and normalized[2] == "/"
        )
    )


def _assert_path_free(value: Any, label: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_path_free(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_path_free(child, f"{label}[{index}]")
    elif isinstance(value, str) and _looks_absolute(value):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} contains an absolute host locator"
        )


def _safe_child(base: Path, relative_value: Any, label: str) -> Path:
    relative = Path(_text(relative_value, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} escapes its artifact directory"
        )
    root = base.resolve()
    result = (root / relative).resolve()
    try:
        result.relative_to(root)
    except ValueError as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} escapes its artifact directory"
        ) from error
    return result


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _gzip_bytes(payload: bytes) -> bytes:
    import io

    destination = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as stream:
        stream.write(payload)
    return destination.getvalue()


def _manifest_identity(
    value: Mapping[str, Any], *, schema: str, revision: str, status: str, label: str
) -> str:
    if (
        value.get("schema") != schema
        or value.get("implementation_revision") != revision
        or value.get("status") != status
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} implementation identity differs"
        )
    return _verify_content_address(value, label)


def _instance_index(manifest: Mapping[str, Any], label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for raw in _array(manifest.get("instances"), f"{label}.instances"):
        row = _mapping(raw, f"{label} instance")
        instance_id = _text(row.get("instance_id"), f"{label}.instance_id")
        if instance_id in result:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"{label} repeats instance {instance_id}"
            )
        result[instance_id] = row
    return result


def _partition_index(manifest: Mapping[str, Any], label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for raw in _array(manifest.get("partitions"), f"{label}.partitions"):
        row = _mapping(raw, f"{label} partition")
        instance_id = _text(row.get("instance_id"), f"{label}.instance_id")
        if instance_id in result:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"{label} repeats instance {instance_id}"
            )
        result[instance_id] = row
    return result


def _stream_selected_jsonl(
    *,
    path: Path,
    descriptor: Mapping[str, Any],
    needles: Sequence[bytes],
    selector,
    label: str,
) -> tuple[list[JSONMap], JSONMap]:
    if path.stat().st_size != _integer(
        descriptor.get("compressed_size_bytes"),
        f"{label}.compressed_size_bytes",
        minimum=0,
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} compressed size differs"
        )
    digest = hashlib.sha256()
    logical_size = 0
    record_count = 0
    decoded_count = 0
    selected: list[JSONMap] = []
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                        f"{label} has a blank JSONL row"
                    )
                digest.update(line)
                logical_size += len(line)
                record_count += 1
                if needles and not any(needle in line for needle in needles):
                    continue
                row = _strict_json(line, f"{label} row {line_number}")
                decoded_count += 1
                if selector(row):
                    selected.append(row)
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"cannot stream {label}: {error}"
        ) from error
    expected = {
        "logical_size_bytes": logical_size,
        "logical_content_sha256": digest.hexdigest(),
        "record_count": record_count,
    }
    for key, observed in expected.items():
        if descriptor.get(key) != observed:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"{label} {key} differs"
            )
    return selected, {
        "instance_id": descriptor.get("instance_id"),
        "record_schema": descriptor.get("record_schema"),
        "record_count": record_count,
        "logical_size_bytes": logical_size,
        "logical_content_sha256": digest.hexdigest(),
        "compressed_size_bytes": descriptor.get("compressed_size_bytes"),
        "physical_gzip_bytes_identity_claimed": False,
        "decoded_candidate_row_count": decoded_count,
        "selected_row_count": len(selected),
        "read_mode": "STREAMING_JSONL_IDENTITY_MATCH_ONLY",
        "full_logical_partition_verified_at_eof": True,
    }


def _anchor_projection(value: Mapping[str, Any] | None) -> JSONMap | None:
    if value is None:
        return None
    return {
        key: deepcopy(value.get(key))
        for key in (
            "timestamp_ms",
            "offset_ms",
            "event_index",
            "stream_type",
            "frame_index",
            "frame_message_index",
            "official_message_sha256",
        )
    }


def _anchor_order_key(anchor: Mapping[str, Any], label: str) -> tuple[int, int, int, int]:
    stream_type = _text(anchor.get("stream_type"), f"{label}.stream_type")
    try:
        stream_order = timeline_v2.STREAM_ORDER[stream_type]
    except KeyError as error:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} has unsupported stream type {stream_type}"
        ) from error
    return (
        _integer(anchor.get("timestamp_ms"), f"{label}.timestamp_ms", minimum=0),
        _integer(anchor.get("event_index"), f"{label}.event_index"),
        stream_order,
        _integer(
            anchor.get("frame_message_index"),
            f"{label}.frame_message_index",
            minimum=0,
        ),
    )


def _decision_order_key(value: Any, label: str) -> tuple[int, int, int, int]:
    raw = _array(value, label)
    if len(raw) != 4:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"{label} must have four EventMeta order components"
        )
    return tuple(
        _integer(component, f"{label}[{index}]")
        for index, component in enumerate(raw)
    )  # type: ignore[return-value]


def _window_membership(
    *,
    order_key: tuple[int, int, int, int],
    first_decision_order: tuple[int, int, int, int],
    last_decision_order: tuple[int, int, int, int],
    diagnostic_end_timestamp_ms: int,
) -> JSONMap:
    diagnostic = (
        order_key >= first_decision_order
        and order_key[0] <= diagnostic_end_timestamp_ms
    )
    decision_support = first_decision_order <= order_key <= last_decision_order
    union = diagnostic or decision_support
    if order_key < first_decision_order:
        relative = "PRE_PREFIX"
    elif union:
        relative = "IN_MATERIALIZED_UNION"
    else:
        relative = "POST_MATERIALIZED_UNION"
    return {
        "base_request_diagnostic_slice": diagnostic,
        "bound_decision_support": decision_support,
        "materialized_team_trace_union": union,
        "relative_to_materialized_union": relative,
    }


def _guid_entry(guid: str) -> tuple[str, int | None]:
    match = _F130_RE.match(guid)
    if match is not None:
        return "CREATURE_GUID_F130", int(match.group(1), 16)
    if guid.upper().startswith("0XF140"):
        return "PET_GUID_F140", None
    return "OTHER_NON_F130_GUID", None


def _load_selected_reconstruction(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    request_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str], JSONMap], list[JSONMap]]:
    instances = _instance_index(manifest, "reconstruction")
    result: dict[tuple[str, str, str], JSONMap] = {}
    closures: list[JSONMap] = []
    wanted_by_artifact: dict[tuple[str, str], set[str]] = defaultdict(set)
    for request in request_rows:
        source = _mapping(request.get("source_identity"), "source_identity")
        instance_id = _text(source.get("instance_id"), "source instance_id")
        encounter_id = _text(source.get("encounter_id"), "source encounter_id")
        wave_id = _text(source.get("wave_id"), "source wave_id")
        wanted_by_artifact[(instance_id, encounter_id)].add(wave_id)
    for artifact_key in sorted(wanted_by_artifact):
        instance_id, encounter_id = artifact_key
        wanted_wave_ids = wanted_by_artifact[artifact_key]
        instance = instances.get(instance_id)
        if instance is None:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"reconstruction lacks instance {instance_id}"
            )
        entries = [
            _mapping(raw, "reconstruction encounter")
            for raw in _array(instance.get("encounters"), "reconstruction encounters")
            if _mapping(raw, "reconstruction encounter").get("encounter_id")
            == encounter_id
        ]
        if len(entries) != 1:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"reconstruction encounter selection is not unique: {artifact_key}"
            )
        entry = entries[0]
        reference = _mapping(entry.get("artifact"), "reconstruction artifact reference")
        path = _safe_child(manifest_path.parent, reference.get("path"), "artifact.path")
        if path.stat().st_size != _integer(
            reference.get("compressed_size_bytes"),
            "artifact.compressed_size_bytes",
            minimum=0,
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected reconstruction compressed size differs"
            )
        if _sha256_file(path) != _sha(
            reference.get("compressed_file_sha256"),
            "artifact.compressed_file_sha256",
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected reconstruction compressed bytes differ"
            )
        try:
            with gzip.open(path, "rb") as handle:
                logical = handle.read()
        except (OSError, EOFError, gzip.BadGzipFile) as error:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"cannot open selected reconstruction: {error}"
            ) from error
        if len(logical) != _integer(
            reference.get("logical_size_bytes"),
            "artifact.logical_size_bytes",
            minimum=0,
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected reconstruction logical size differs"
            )
        artifact = _strict_json(logical, "selected reconstruction artifact")
        if (
            artifact.get("schema") != reconstruction_v2.ARTIFACT_SCHEMA
            or artifact.get("implementation_revision")
            != reconstruction_v2.IMPLEMENTATION_REVISION
            or artifact.get("status") != reconstruction_v2.STATUS
            or artifact.get("instance_id") != instance_id
            or artifact.get("encounter_id") != encounter_id
            or artifact.get("summary") != entry.get("summary")
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected reconstruction identity differs"
            )
        artifact_sha = _verify_content_address(artifact, "selected reconstruction")
        if artifact_sha != reference.get("content_sha256"):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected reconstruction content address differs from manifest"
            )
        matched: dict[str, Mapping[str, Any]] = {}
        for raw in _array(artifact.get("waves"), "reconstruction waves"):
            wave = _mapping(raw, "reconstruction wave")
            wave_id = wave.get("wave_id")
            if wave_id not in wanted_wave_ids:
                continue
            if wave_id in matched:
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    f"reconstruction wave selection is duplicated: {wave_id}"
                )
            matched[str(wave_id)] = wave
        if set(matched) != wanted_wave_ids:
            missing = sorted(wanted_wave_ids - set(matched))
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"reconstruction wave selection is incomplete: {missing}"
            )
        for wave_id in sorted(matched):
            wave = deepcopy(dict(matched[wave_id]))
            _verify_content_address(wave, "selected reconstruction wave")
            for target in _array(wave.get("targets"), "reconstruction wave targets"):
                _verify_content_address(
                    _mapping(target, "reconstruction target"), "target"
                )
            result[(instance_id, encounter_id, wave_id)] = wave
        closures.append(
            {
                "instance_id": instance_id,
                "encounter_id": encounter_id,
                "selected_wave_ids": sorted(wanted_wave_ids),
                "selected_wave_count": len(wanted_wave_ids),
                "artifact_content_sha256": artifact_sha,
                "compressed_size_bytes": reference.get("compressed_size_bytes"),
                "logical_size_bytes": reference.get("logical_size_bytes"),
                "selective_artifact_bytes_verified": True,
            }
        )
    return result, sorted(closures, key=lambda row: (row["instance_id"], row["encounter_id"]))


def _selected_episode_rows(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    request_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str], JSONMap], list[JSONMap]]:
    descriptors = _partition_index(manifest, "episode")
    wanted: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for request in request_rows:
        source = _mapping(request.get("source_identity"), "source_identity")
        key = (
            _text(source.get("instance_id"), "instance_id"),
            _text(source.get("encounter_id"), "encounter_id"),
            _text(source.get("player_guid"), "player_guid").casefold(),
        )
        wanted[key] = request
    selected: dict[tuple[str, str, str], JSONMap] = {}
    closures: list[JSONMap] = []
    for instance_id in sorted({key[0] for key in wanted}):
        descriptor = descriptors.get(instance_id)
        if descriptor is None:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"episode manifest lacks instance {instance_id}"
            )
        keys = {key for key in wanted if key[0] == instance_id}
        needles = sorted(
            {value.encode("utf-8") for key in keys for value in (key[1], key[2])}
        )
        path = _safe_child(manifest_path.parent, descriptor.get("path"), "episode path")
        rows, closure = _stream_selected_jsonl(
            path=path,
            descriptor=descriptor,
            needles=needles,
            selector=lambda row, keys=keys: (
                str(row.get("instance_id")),
                str(row.get("encounter_id")),
                str(_mapping(row.get("player"), "episode player").get("guid")).casefold(),
            )
            in keys,
            label=f"episode partition {instance_id}",
        )
        closure["instance_id"] = instance_id
        for row in rows:
            if (
                row.get("schema") != episode_v1.SCHEMA
                or row.get("implementation_revision") != episode_v1.IMPLEMENTATION_REVISION
                or row.get("status") != episode_v1.STATUS
            ):
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    "selected episode identity differs"
                )
            key = (
                str(row["instance_id"]),
                str(row["encounter_id"]),
                str(_mapping(row.get("player"), "episode player").get("guid")).casefold(),
            )
            if key in selected:
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    f"episode selection is duplicated: {key}"
                )
            selected[key] = row
        closures.append(closure)
    if set(selected) != set(wanted):
        missing = sorted(set(wanted) - set(selected))
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"episode selection is incomplete: {missing}"
        )
    return selected, closures


def _selected_timeline_rows(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    request_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str, str], JSONMap], list[JSONMap]]:
    instances = _instance_index(manifest, "timeline")
    wanted: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for request in request_rows:
        source = _mapping(request.get("source_identity"), "source_identity")
        key = (
            _text(source.get("instance_id"), "instance_id"),
            _text(source.get("encounter_id"), "encounter_id"),
            _text(source.get("wave_id"), "wave_id"),
        )
        wanted[key] = request
    selected: dict[tuple[str, str, str], JSONMap] = {}
    closures: list[JSONMap] = []
    for instance_id in sorted({key[0] for key in wanted}):
        instance = instances.get(instance_id)
        if instance is None:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"timeline manifest lacks instance {instance_id}"
            )
        descriptor = _mapping(instance.get("partition"), "timeline partition")
        keys = {key for key in wanted if key[0] == instance_id}
        needles = sorted({key[1].encode("utf-8") for key in keys})
        path = _safe_child(manifest_path.parent, descriptor.get("path"), "timeline path")
        rows, closure = _stream_selected_jsonl(
            path=path,
            descriptor=descriptor,
            needles=needles,
            selector=lambda row, keys=keys: (
                str(row.get("instance_id")),
                str(row.get("encounter_id")),
                str(row.get("wave_id")),
            )
            in keys,
            label=f"timeline partition {instance_id}",
        )
        closure["instance_id"] = instance_id
        source_binding_sha = hashlib.sha256(
            _canonical_bytes(_mapping(instance.get("source_binding"), "timeline source binding"))
        ).hexdigest()
        for row in rows:
            timeline_v2._verify_wave_record(
                row, source_binding_sha256=source_binding_sha
            )
            key = (
                str(row["instance_id"]),
                str(row["encounter_id"]),
                str(row["wave_id"]),
            )
            request = wanted[key]
            source = _mapping(request.get("source_identity"), "source_identity")
            row_sha = _verify_content_address(row, "selected timeline wave")
            if (
                row_sha != source.get("source_wave_content_sha256")
                or row.get("wave_ordinal") != source.get("wave_ordinal")
            ):
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    "timeline wave differs from exact dynamic source identity"
                )
            if key in selected:
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    f"timeline wave selection is duplicated: {key}"
                )
            selected[key] = row
        closures.append(closure)
    if set(selected) != set(wanted):
        missing = sorted(set(wanted) - set(selected))
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"timeline wave selection is incomplete: {missing}"
        )
    return selected, closures


def _damage_map(value: Any, label: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in _array(value, label):
        row = _mapping(raw, f"{label} row")
        guid = _text(row.get("target_guid"), f"{label}.target_guid")
        amount = _integer(row.get("damage_amount"), f"{label}.damage_amount", minimum=0)
        if guid in result:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"{label} repeats target {guid}"
            )
        result[guid] = amount
    return result


def _decision_evidence(
    *, request: Mapping[str, Any], episode: Mapping[str, Any]
) -> tuple[list[JSONMap], JSONMap]:
    source = _mapping(request.get("source_identity"), "source_identity")
    prefix = _mapping(request.get("decision_prefix"), "decision_prefix")
    waves = [
        _mapping(raw, "episode wave")
        for raw in _array(episode.get("wave_observations"), "wave_observations")
        if _mapping(raw, "episode wave").get("wave_id") == source.get("wave_id")
    ]
    if len(waves) != 1:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "episode exact wave selection is not unique"
        )
    wave = waves[0]
    if (
        wave.get("wave_ordinal") != source.get("wave_ordinal")
        or wave.get("source_wave_content_sha256")
        != source.get("source_wave_content_sha256")
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "episode wave differs from exact source identity"
        )
    first = _mapping(prefix.get("first_decision"), "first_decision")
    last = _mapping(prefix.get("last_bound_decision"), "last_bound_decision")
    first_order = tuple(_array(first.get("order_key"), "first order_key"))
    last_order = tuple(_array(last.get("order_key"), "last order_key"))
    selected = []
    for raw in _array(wave.get("prefix_transitions"), "prefix_transitions"):
        transition = _mapping(raw, "prefix transition")
        observed = _mapping(transition.get("observed_event"), "observed_event")
        if observed.get("policy_decision_label") is not True:
            continue
        order = tuple(_array(observed.get("order_key"), "decision order_key"))
        if first_order <= order <= last_order:
            selected.append(transition)
    expected_count = _integer(prefix.get("decision_count"), "decision_count", minimum=1)
    if len(selected) != expected_count:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"bound decision count differs: expected {expected_count}, observed {len(selected)}"
        )
    focal = _text(source.get("player_guid"), "player_guid").casefold()
    prior_seen: set[str] = set()
    prior_dead: set[str] = set()
    prior_damage: dict[str, int] = {}
    prior_total = 0
    output: list[JSONMap] = []
    for index, transition in enumerate(selected):
        if (
            transition.get("feature_cutoff_is_strict_prefix") is not True
            or transition.get("current_event_present_in_state_before") is not False
            or transition.get("future_outcomes_in_state_before") is not False
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected decision is not a strict causal prefix"
            )
        observed = _mapping(transition.get("observed_event"), "observed_event")
        if str(observed.get("source_guid")).casefold() != focal:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected decision source differs from focal player"
            )
        state = _mapping(transition.get("state_before"), "state_before")
        seen = {_text(value, "seen target") for value in _array(state.get("seen_hostile_target_guids"), "seen targets")}
        alive = {_text(value, "alive target") for value in _array(state.get("alive_seen_hostile_target_guids"), "alive targets")}
        dead = {_text(value, "dead target") for value in _array(state.get("observed_dead_target_guids"), "dead targets")}
        if not alive <= seen or not dead <= seen or alive & dead:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "strict-prefix target sets are inconsistent"
            )
        damage = _damage_map(
            state.get("prefix_direct_damage_by_target"),
            "prefix_direct_damage_by_target",
        )
        total = _integer(
            state.get("prefix_direct_damage_amount"),
            "prefix_direct_damage_amount",
            minimum=0,
        )
        if index and (
            not prior_seen <= seen
            or not prior_dead <= dead
            or total < prior_total
            or any(damage.get(guid, 0) < amount for guid, amount in prior_damage.items())
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "selected strict-prefix state is not monotone"
            )
        exact_target = _mapping(observed.get("exact_target"), "exact_target")
        damage_delta = [
            {"target_guid": guid, "damage_amount": damage.get(guid, 0) - prior_damage.get(guid, 0)}
            for guid in sorted(set(damage) | set(prior_damage))
            if damage.get(guid, 0) != prior_damage.get(guid, 0)
        ]
        row: JSONMap = {
            "bound_decision_index": index,
            "trace_index": transition.get("trace_index"),
            "action_key": observed.get("action_key"),
            "action_lane": observed.get("action_lane"),
            "anchor": _anchor_projection(_mapping(observed.get("anchor"), "decision anchor")),
            "order_key": deepcopy(observed.get("order_key")),
            "resolved_target_guid": exact_target.get("guid")
            if exact_target.get("voting_enemy_target") is True
            else None,
            "strict_prefix_state": {
                "seen_target_count": len(seen),
                "alive_target_guids": sorted(alive),
                "dead_target_count": len(dead),
                "new_seen_target_guids": sorted(seen - prior_seen),
                "new_dead_target_guids": sorted(dead - prior_dead),
                "last_observed_hostile_target_guid": state.get("last_observed_hostile_target_guid"),
                "focal_direct_damage_prefix_amount": total,
                "focal_direct_damage_delta_since_previous_bound_decision": total - prior_total,
                "focal_direct_damage_by_target_delta": damage_delta,
                "future_target_backfill_used": False,
            },
        }
        if index == 0:
            row["strict_prefix_state"]["segment_entry_snapshot"] = {
                "seen_target_guids": sorted(seen),
                "dead_target_guids": sorted(dead),
                "focal_direct_damage_by_target": [
                    {"target_guid": guid, "damage_amount": amount}
                    for guid, amount in sorted(damage.items())
                ],
            }
        output.append(row)
        prior_seen, prior_dead, prior_damage, prior_total = seen, dead, damage, total
    if (
        output[0]["anchor"]["timestamp_ms"] != first.get("timestamp_ms")
        or output[0]["anchor"]["event_index"] != first.get("event_index")
        or output[0]["action_key"] != first.get("action_key")
        or output[-1]["anchor"]["timestamp_ms"] != last.get("timestamp_ms")
        or output[-1]["anchor"]["event_index"] != last.get("event_index")
        or output[-1]["action_key"] != last.get("action_key")
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "episode first/last decisions differ from dynamic binding"
        )
    last_state = output[-1]["strict_prefix_state"]
    return output, {
        "decision_count": len(output),
        "first_decision_timestamp_ms": output[0]["anchor"]["timestamp_ms"],
        "last_decision_timestamp_ms": output[-1]["anchor"]["timestamp_ms"],
        "span_ms": output[-1]["anchor"]["timestamp_ms"] - output[0]["anchor"]["timestamp_ms"],
        "last_prefix_seen_target_count": last_state["seen_target_count"],
        "last_prefix_alive_target_count": len(last_state["alive_target_guids"]),
        "last_prefix_dead_target_count": last_state["dead_target_count"],
    }


def _target_projection(target: Mapping[str, Any]) -> JSONMap:
    if target.get("voting_for_simulator_target_model") is not True:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "nonvoting reconstruction target entered environment evidence"
        )
    guid = _text(target.get("target_guid"), "target_guid")
    kind, entry = _guid_entry(guid)
    damage = _mapping(target.get("damage_received"), "damage_received")
    through = _mapping(damage.get("through_first_death"), "through_first_death")
    death = _mapping(target.get("death"), "death")
    armor = _mapping(target.get("armor"), "armor")
    if (
        damage.get("amount_source") != "DMG_ONLY"
        or through.get("interpretation")
        != "observed DMG sum, not exact maximum or initial health"
        or armor.get("effective_armor") is not None
        or armor.get("aura_transitions") is not None
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "reconstruction target evidence boundary differs"
        )
    by_player = []
    for raw in _array(damage.get("by_exact_player"), "damage by exact player"):
        row = _mapping(raw, "damage player row")
        player = _mapping(row.get("player"), "damage player")
        by_player.append(
            {
                "player_guid": player.get("guid"),
                "damage_amount": row.get("damage_amount"),
                "damage_event_count": row.get("damage_event_count"),
            }
        )
    return {
        "target_guid": guid,
        "guid_kind": kind,
        "creature_entry_id": entry,
        "first_observed_activity_anchor": _anchor_projection(
            _mapping(target.get("first_activity_anchor"), "first activity")
        ),
        "last_observed_activity_anchor": _anchor_projection(
            _mapping(target.get("last_activity_anchor"), "last activity")
        ),
        "death": {
            "observed": death.get("observed"),
            "anchor": _anchor_projection(
                _mapping(death.get("anchor"), "death anchor")
                if death.get("anchor") is not None
                else None
            ),
            "marker_damage_added": death.get("damage_amount_added_from_slain"),
        },
        "historical_outcome": {
            "observed_damage_received": damage.get("amount"),
            "observed_damage_event_count": damage.get("event_count"),
            "observed_damage_through_first_death": through.get("amount"),
            "observed_damage_through_first_death_event_count": through.get("event_count"),
            "observed_unattributed_damage": damage.get("unattributed_amount"),
            "observed_healing_received": _mapping(
                target.get("healing_received"), "healing_received"
            ).get("amount"),
            "damage_by_exact_player": sorted(by_player, key=lambda row: str(row["player_guid"])),
            "kill_budget_is_exact_initial_or_max_health": False,
        },
        "health_evidence": {
            "exact_initial_or_max_health": None,
            "retrospective_kill_budget_proxy": through.get("amount")
            if death.get("observed") is True
            else None,
            "retrospective_only_not_policy_input": True,
            "same_entry_leave_instance_out_prior": None,
        },
        "armor_evidence": {
            "exact_base_armor": None,
            "exact_effective_armor_events": None,
            "source_status": armor.get("status"),
        },
        "attackability_evidence": {
            "exact_intervals": None,
            "observed_activity_envelope": {
                "first_offset_ms": _mapping(target.get("first_activity_anchor"), "first activity").get("offset_ms"),
                "last_offset_ms": _mapping(target.get("last_activity_anchor"), "last activity").get("offset_ms"),
                "not_equal_to_exact_attackability": True,
            },
        },
    }


def _event_sort_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    order_key = event.get("order_key")
    if isinstance(order_key, list) and len(order_key) == 4:
        return (*order_key, str(event.get("actor_player_guid")), str(event.get("target_guid")))
    anchor = _mapping(event.get("anchor"), "schedule anchor")
    return (
        anchor.get("timestamp_ms"),
        anchor.get("event_index"),
        anchor.get("frame_message_index"),
        str(event.get("actor_player_guid")),
        str(event.get("target_guid")),
    )


def _armor_candidate_ids() -> tuple[set[str], set[int]]:
    names: set[str] = set()
    spell_ids: set[int] = set()
    for name, raw in capsule_v2.ARMOR_DEBUFF_REGISTRY.items():
        names.add(name.casefold())
        for value in _array(
            _mapping(raw, "armor registry row").get("simulator_spell_id_hypotheses"),
            "armor spell ids",
        ):
            spell_ids.add(_integer(value, "armor spell id", minimum=1))
    return names, spell_ids


def _timeline_evidence(
    *,
    request: Mapping[str, Any],
    record: Mapping[str, Any],
    targets: list[JSONMap],
    diagnostic_duration_ms: int,
) -> JSONMap:
    source = _mapping(request.get("source_identity"), "source_identity")
    prefix = _mapping(request.get("decision_prefix"), "decision_prefix")
    first_prefix = _mapping(prefix.get("first_decision"), "first decision")
    last_prefix = _mapping(prefix.get("last_bound_decision"), "last decision")
    first_decision = _integer(
        first_prefix.get("timestamp_ms"),
        "first decision timestamp",
    )
    last_decision = _integer(
        last_prefix.get("timestamp_ms"),
        "last decision timestamp",
    )
    first_decision_order = _decision_order_key(
        first_prefix.get("order_key"), "first decision order_key"
    )
    last_decision_order = _decision_order_key(
        last_prefix.get("order_key"), "last decision order_key"
    )
    if (
        first_decision_order[0] != first_decision
        or last_decision_order[0] != last_decision
        or first_decision_order > last_decision_order
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "bound decision timestamps and EventMeta order differ"
        )
    reconstruction = _mapping(record.get("reconstruction_binding"), "reconstruction binding")
    window = _mapping(reconstruction.get("window"), "reconstruction window")
    wave_start_anchor = _mapping(window.get("first_anchor"), "first anchor")
    wave_end_anchor = _mapping(window.get("last_context_anchor"), "last context anchor")
    wave_start = _integer(
        wave_start_anchor.get("timestamp_ms"),
        "wave start",
    )
    wave_end = _integer(
        wave_end_anchor.get("timestamp_ms"),
        "wave end",
    )
    wave_start_order = _anchor_order_key(wave_start_anchor, "wave first anchor")
    wave_end_order = _anchor_order_key(wave_end_anchor, "wave last context anchor")
    if not (
        wave_start_order
        <= first_decision_order
        <= last_decision_order
        <= wave_end_order
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "bound decision interval is outside reconstruction wave"
        )
    diagnostic_end = min(wave_end, first_decision + diagnostic_duration_ms)
    evidence_end = max(last_decision, diagnostic_end)
    focal = _text(source.get("player_guid"), "focal player_guid").casefold()
    target_by_guid = {str(row["target_guid"]): row for row in targets}
    if len(target_by_guid) != len(targets):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "reconstruction repeats a voting target GUID"
        )

    names, spell_ids = _armor_candidate_ids()
    target_runtime: dict[str, JSONMap] = {
        guid: {
            "positive_damage_events": [],
            "event_time_voting_positive_damage_events": [],
            "event_time_nonvoting_positive_damage_events": [],
            "healing_events": [],
        }
        for guid in target_by_guid
    }
    loo_events: list[JSONMap] = []
    focal_events: list[JSONMap] = []
    unattributed_window_damage = 0
    unattributed_voting_damage_evidence: list[JSONMap] = []
    diagnostic_voting_damage_target_guids: set[str] = set()
    union_voting_damage_target_guids: set[str] = set()
    armor_candidates_full_wave: list[JSONMap] = []

    def observe(event: Mapping[str, Any], actor: str | None) -> None:
        nonlocal unattributed_window_damage
        target = _mapping(event.get("target"), "timeline target")
        guid = target.get("guid")
        if guid not in target_by_guid:
            return
        event_time_voting = target.get("voting_enemy_target") is True
        anchor = _mapping(event.get("anchor"), "timeline anchor")
        timestamp = _integer(anchor.get("timestamp_ms"), "event timestamp")
        order_key = _anchor_order_key(anchor, "timeline anchor")
        membership = _window_membership(
            order_key=order_key,
            first_decision_order=first_decision_order,
            last_decision_order=last_decision_order,
            diagnostic_end_timestamp_ms=diagnostic_end,
        )
        event_type = event.get("event_type")
        if event_type == "DMG":
            damage = _mapping(event.get("damage"), "timeline damage")
            amount = _integer(damage.get("amount"), "timeline damage amount", minimum=0)
            if amount <= 0:
                return
            compact = {
                "timestamp_ms": timestamp,
                "amount": amount,
                "actor_player_guid": actor,
                "attribution_kind": _mapping(event.get("attribution"), "attribution").get("attribution_kind"),
                "anchor": _anchor_projection(anchor),
                "order_key": list(order_key),
                "event_time_voting_enemy_target": event_time_voting,
                "window_membership": membership,
            }
            runtime = target_runtime[str(guid)]
            runtime["positive_damage_events"].append(compact)
            if not event_time_voting:
                runtime["event_time_nonvoting_positive_damage_events"].append(compact)
            else:
                runtime["event_time_voting_positive_damage_events"].append(compact)
                if membership["base_request_diagnostic_slice"]:
                    diagnostic_voting_damage_target_guids.add(str(guid))
                if membership["materialized_team_trace_union"]:
                    union_voting_damage_target_guids.add(str(guid))
            if event_time_voting and membership["materialized_team_trace_union"]:
                if actor is None:
                    unattributed_window_damage += amount
                    unattributed_voting_damage_evidence.append(
                        {
                            "offset_ms": timestamp - first_decision,
                            "target_guid": guid,
                            "target_index": target_by_guid[str(guid)]["target_index"],
                            "damage": amount,
                            "anchor": _anchor_projection(anchor),
                            "order_key": list(order_key),
                            "window_membership": membership,
                        }
                    )
                else:
                    schedule = {
                        "offset_ms": timestamp - first_decision,
                        "target_guid": guid,
                        "target_index": target_by_guid[str(guid)]["target_index"],
                        "damage": amount,
                        "actor_player_guid": actor,
                        "attribution_kind": compact["attribution_kind"],
                        "source_guid": _mapping(event.get("source"), "source").get("guid"),
                        "spell_id": _mapping(event.get("spell"), "spell").get("id"),
                        "spell_name": _mapping(event.get("spell"), "spell").get("name"),
                        "anchor": _anchor_projection(anchor),
                        "order_key": list(order_key),
                        "window_membership": membership,
                    }
                    (focal_events if actor.casefold() == focal else loo_events).append(schedule)
        elif event_type == "HEAL":
            amount = _integer(
                _mapping(event.get("healing"), "timeline healing").get("amount"),
                "timeline healing amount",
                minimum=0,
            )
            target_runtime[str(guid)]["healing_events"].append(
                {
                    "timestamp_ms": timestamp,
                    "amount": amount,
                    "actor_player_guid": actor,
                    "order_key": list(order_key),
                    "event_time_voting_enemy_target": event_time_voting,
                    "window_membership": membership,
                }
            )
        if event_time_voting and event_type in {"START", "GO"}:
            spell = _mapping(event.get("spell"), "timeline spell")
            name = spell.get("name")
            spell_id = spell.get("id")
            if (
                isinstance(name, str)
                and name.casefold() in names
                or isinstance(spell_id, int)
                and not isinstance(spell_id, bool)
                and spell_id in spell_ids
            ):
                armor_candidates_full_wave.append(
                    {
                        "offset_ms": timestamp - first_decision,
                        "phase": event_type,
                        "target_guid": guid,
                        "actor_player_guid": actor,
                        "actor_role": "CANDIDATE_ENDOGENOUS"
                        if actor is not None and actor.casefold() == focal
                        else "NONFOCAL_OR_UNATTRIBUTED_CAST_PROXY",
                        "spell_id": spell_id,
                        "spell_name": name,
                        "aura_apply_or_stack_proven": False,
                        "effective_armor_event_emitted": False,
                        "anchor": _anchor_projection(anchor),
                        "order_key": list(order_key),
                        "window_membership": membership,
                    }
                )

    for raw_player in _array(record.get("players"), "timeline players"):
        player = _mapping(raw_player, "timeline player")
        actor = _text(
            _mapping(player.get("player"), "player metadata").get("guid"),
            "player guid",
        ).casefold()
        for raw_event in _array(player.get("timeline"), "player timeline"):
            event = _mapping(raw_event, "player event")
            attribution = _mapping(event.get("attribution"), "event attribution")
            if (
                attribution.get("player_guid") is None
                or str(attribution.get("player_guid")).casefold() != actor
                or attribution.get("attribution_kind")
                not in background_v2.PLAYER_ATTRIBUTION_KINDS
            ):
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    "player event lacks exact player attribution"
                )
            observe(event, actor)
    for raw_event in _array(
        _mapping(record.get("unattributed_lane"), "unattributed lane").get("timeline"),
        "unattributed timeline",
    ):
        observe(_mapping(raw_event, "unattributed event"), None)

    classification_transition_evidence: list[JSONMap] = []
    for raw_transition in _array(
        record.get("classification_transitions_inside_window"),
        "classification transitions",
    ):
        transition = _mapping(raw_transition, "classification transition")
        guid = transition.get("guid")
        if guid not in target_by_guid:
            continue
        anchor = _mapping(transition.get("anchor"), "classification anchor")
        order_key = _anchor_order_key(anchor, "classification anchor")
        classification_transition_evidence.append(
            {
                "target_guid": guid,
                "lane": transition.get("lane"),
                "unit_type": deepcopy(transition.get("unit_type")),
                "affiliation": deepcopy(transition.get("affiliation")),
                "resolution_status": transition.get("resolution_status"),
                "spell_id": transition.get("spell_id"),
                "anchor": _anchor_projection(anchor),
                "order_key": list(order_key),
                "window_membership": _window_membership(
                    order_key=order_key,
                    first_decision_order=first_decision_order,
                    last_decision_order=last_decision_order,
                    diagnostic_end_timestamp_ms=diagnostic_end,
                ),
            }
        )

    loo_events.sort(key=_event_sort_key)
    focal_events.sort(key=_event_sort_key)
    unattributed_voting_damage_evidence.sort(key=_event_sort_key)
    armor_candidates_full_wave.sort(key=_event_sort_key)
    classification_transition_evidence.sort(key=_event_sort_key)
    diagnostic_loo = [
        event
        for event in loo_events
        if _mapping(event.get("window_membership"), "LOO window membership").get(
            "base_request_diagnostic_slice"
        )
        is True
    ]
    diagnostic_focal = [
        event
        for event in focal_events
        if _mapping(event.get("window_membership"), "focal window membership").get(
            "base_request_diagnostic_slice"
        )
        is True
    ]
    union_armor_candidates = [
        event
        for event in armor_candidates_full_wave
        if _mapping(event.get("window_membership"), "armor window membership").get(
            "materialized_team_trace_union"
        )
        is True
    ]

    decision_rows = [
        _mapping(raw, "internal decision row")
        for raw in _array(request.get("_decision_rows"), "internal decision rows")
    ]
    last_alive = set(
        _array(
            _mapping(
                decision_rows[-1].get("strict_prefix_state"), "last strict prefix"
            ).get("alive_target_guids"),
            "last alive targets",
        )
    )
    last_seen = {
        str(guid)
        for decision in decision_rows
        for guid in _array(
            _mapping(
                decision.get("strict_prefix_state"), "decision strict prefix"
            ).get("new_seen_target_guids"),
            "new seen targets",
        )
    }
    last_dead = {
        str(guid)
        for decision in decision_rows
        for guid in _array(
            _mapping(
                decision.get("strict_prefix_state"), "decision strict prefix"
            ).get("new_dead_target_guids"),
            "new dead targets",
        )
    }
    if not last_alive <= last_seen or not last_dead <= last_seen or last_alive & last_dead:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "last strict-prefix target registry is inconsistent"
        )

    for guid, target in target_by_guid.items():
        runtime = target_runtime[guid]
        for key in (
            "positive_damage_events",
            "event_time_voting_positive_damage_events",
            "event_time_nonvoting_positive_damage_events",
            "healing_events",
        ):
            runtime[key].sort(key=_event_sort_key)
        all_damage = runtime["positive_damage_events"]
        voting_damage = runtime["event_time_voting_positive_damage_events"]
        nonvoting_damage = runtime["event_time_nonvoting_positive_damage_events"]
        prefix_voting_damage = [
            event
            for event in voting_damage
            if tuple(event["order_key"]) < last_decision_order
        ]
        right_censored_at_cutoff = guid in last_seen and guid not in last_dead
        target["target_lifecycle_evidence"] = {
            "event_time_voting_positive_damage": {
                "first_anchor": voting_damage[0]["anchor"] if voting_damage else None,
                "last_anchor": voting_damage[-1]["anchor"] if voting_damage else None,
                "event_count": len(voting_damage),
                "amount": sum(event["amount"] for event in voting_damage),
            },
            "retrospective_final_target_identity_positive_damage": {
                "first_anchor": all_damage[0]["anchor"] if all_damage else None,
                "last_anchor": all_damage[-1]["anchor"] if all_damage else None,
                "event_count": len(all_damage),
                "amount": sum(event["amount"] for event in all_damage),
                "uses_final_target_identity_backfill": bool(nonvoting_damage),
            },
            "strict_prefix_event_time_voting_positive_damage": {
                "first_anchor": (
                    prefix_voting_damage[0]["anchor"] if prefix_voting_damage else None
                ),
                "last_anchor": (
                    prefix_voting_damage[-1]["anchor"] if prefix_voting_damage else None
                ),
                "event_count": len(prefix_voting_damage),
                "amount": sum(event["amount"] for event in prefix_voting_damage),
                "cutoff_exclusive_order_key": list(last_decision_order),
                "cutoff_anchor": _anchor_projection(
                    _mapping(decision_rows[-1].get("anchor"), "last decision anchor")
                ),
                "target_seen_at_cutoff": guid in last_seen,
                "target_observed_dead_at_cutoff": guid in last_dead,
                "right_censored_at_cutoff": right_censored_at_cutoff,
                "future_suffix_used": False,
            },
            "positive_damage_is_point_attackability_evidence_only": True,
            "event_time_nonvoting_positive_damage": {
                "event_count": len(nonvoting_damage),
                "amount_excluded_from_runtime_schedule": sum(
                    event["amount"] for event in nonvoting_damage
                ),
            },
            "retrospective_target_identity_backfill_used_only_in_labeled_hypothesis": bool(
                nonvoting_damage
            ),
            "event_time_runtime_lane_backfilled": False,
            "silent_interval_interpretation": "UNIDENTIFIED",
        }
        reconstruction_total = _integer(
            _mapping(target.get("historical_outcome"), "historical outcome").get(
                "observed_damage_received"
            ),
            "reconstruction target damage",
            minimum=0,
        )
        observed_total = sum(event["amount"] for event in runtime["positive_damage_events"])
        if reconstruction_total != observed_total:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "timeline/reconstruction target damage differs for "
                f"{guid}: reconstruction={reconstruction_total}, timeline={observed_total}"
            )

    lower_bounds = []
    for guid in sorted(last_alive):
        runtime = target_runtime.get(guid)
        if runtime is None:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "strict-prefix alive target is absent from reconstruction registry"
            )
        damage = sum(
            event["amount"]
            for event in runtime["positive_damage_events"]
            if tuple(event["order_key"]) < last_decision_order
        )
        healing = sum(
            event["amount"]
            for event in runtime["healing_events"]
            if tuple(event["order_key"]) < last_decision_order
        )
        lower_bounds.append(
            {
                "target_guid": guid,
                "cutoff_exclusive_timestamp_ms": last_decision,
                "cutoff_exclusive_order_key": list(last_decision_order),
                "observed_positive_damage_before_cutoff": damage,
                "observed_healing_before_cutoff": healing,
                "persistent_health_model_lower_bound": max(0, damage - healing),
                "status": "MODEL_CONDITIONAL_STRICT_LOWER_BOUND_NOT_EXACT_HP",
            }
        )

    return {
        "windows": {
            "reconstruction_wave": {
                "start_timestamp_ms": wave_start,
                "end_timestamp_ms": wave_end,
                "start_order_key": list(wave_start_order),
                "end_order_key": list(wave_end_order),
                "duration_ms": wave_end - wave_start,
                "role": "RETROSPECTIVE_OUTCOME_EVIDENCE_ONLY",
            },
            "base_request_diagnostic_slice": {
                "start_timestamp_ms": first_decision,
                "end_timestamp_ms": diagnostic_end,
                "start_order_key": list(first_decision_order),
                "end_boundary": "INCLUSIVE_TIMESTAMP",
                "duration_ms": diagnostic_end - first_decision,
                "truncated_by_wave_end": diagnostic_end < first_decision + diagnostic_duration_ms,
                "role": "DIAGNOSTIC_ONLY_NOT_FULL_WAVE_NOT_KILL_CLOCK",
                "covers_all_bound_decisions": diagnostic_end >= last_decision,
            },
            "bound_decision_support": {
                "start_timestamp_ms": first_decision,
                "end_timestamp_ms": last_decision,
                "start_order_key": list(first_decision_order),
                "end_order_key": list(last_decision_order),
                "end_boundary": "INCLUSIVE_EVENTMETA_ORDER",
                "duration_ms": last_decision - first_decision,
                "role": "OBSERVED_SOURCE_BUILD_DECISIONS_ONLY",
            },
            "materialized_team_trace_union": {
                "start_timestamp_ms": first_decision,
                "end_timestamp_ms": evidence_end,
                "duration_ms": evidence_end - first_decision,
                "membership_rule": "DIAGNOSTIC_TIMESTAMP_OR_BOUND_DECISION_EVENTMETA_ORDER",
            },
            "selected_execution_window": None,
        },
        "target_registry_evidence": {
            "full_reconstruction_voting_target_guids": sorted(target_by_guid),
            "strict_prefix_alive_at_first_bound_decision": deepcopy(
                _mapping(
                    _array(request.get("_decision_rows"), "internal decision rows")[0].get(
                        "strict_prefix_state"
                    ),
                    "first strict prefix",
                ).get("alive_target_guids")
            ),
            "strict_prefix_alive_at_last_bound_decision": sorted(last_alive),
            "strict_prefix_seen_by_last_bound_decision": sorted(
                {
                    str(guid)
                    for decision in _array(
                        request.get("_decision_rows"), "internal decision rows"
                    )
                    for guid in _array(
                        _mapping(
                            _mapping(decision, "decision row").get(
                                "strict_prefix_state"
                            ),
                            "decision strict prefix",
                        ).get("new_seen_target_guids"),
                        "new seen targets",
                    )
                }
            ),
            "diagnostic_slice_event_time_voting_damage_target_guids": sorted(
                diagnostic_voting_damage_target_guids
            ),
            "materialized_union_event_time_voting_damage_target_guids": sorted(
                union_voting_damage_target_guids
            ),
            "future_target_backfill_used": False,
            "selected_simulator_registry": None,
        },
        "team_trace": {
            "focal_player_guid": focal,
            "leave_one_out_exact_player_damage_events": loo_events,
            "excluded_focal_exact_player_damage_events": focal_events,
            "unattributed_voting_damage_evidence_not_runtime_schedule": (
                unattributed_voting_damage_evidence
            ),
            "diagnostic_slice_accounting": {
                "loo_event_count": len(diagnostic_loo),
                "loo_damage": sum(event["damage"] for event in diagnostic_loo),
                "excluded_focal_event_count": len(diagnostic_focal),
                "excluded_focal_damage": sum(event["damage"] for event in diagnostic_focal),
            },
            "materialized_union_accounting": {
                "loo_event_count": len(loo_events),
                "loo_damage": sum(event["damage"] for event in loo_events),
                "excluded_focal_event_count": len(focal_events),
                "excluded_focal_damage": sum(event["damage"] for event in focal_events),
                "unattributed_damage_not_entering_runtime_schedule": unattributed_window_damage,
            },
            "historical_schedule_role": "FACTUAL_CONTROL_AND_CALIBRATION_EVIDENCE_ONLY",
            "counterfactual_team_response_model": None,
            "candidate_damage_double_counted": False,
        },
        "armor_cast_candidate_evidence": {
            "materialized_team_trace_union": union_armor_candidates,
            "full_reconstruction_wave_retrospective": armor_candidates_full_wave,
            "outside_materialized_union_count": len(armor_candidates_full_wave)
            - len(union_armor_candidates),
            "aura_apply_or_stack_proven": False,
            "effective_armor_schedule_proven": False,
        },
        "classification_transition_evidence": classification_transition_evidence,
        "alive_at_last_bound_decision_health_lower_bounds": lower_bounds,
    }


def _pack_hypotheses(
    targets: Sequence[Mapping[str, Any]],
    *,
    target_guids: set[str],
    registry_scope: str,
    lifecycle_anchor_mode: str,
) -> list[JSONMap]:
    intervals = []
    for target in targets:
        guid = str(target["target_guid"])
        if guid not in target_guids:
            continue
        lifecycle = _mapping(target.get("target_lifecycle_evidence"), "target lifecycle")
        if lifecycle_anchor_mode == "EVENT_TIME_VOTING_FULL_WAVE_RETROSPECTIVE":
            anchors = _mapping(
                lifecycle.get("event_time_voting_positive_damage"),
                "event-time voting lifecycle",
            )
            retrospective_backfill = False
            prefix_censored = False
        elif lifecycle_anchor_mode == "RETROSPECTIVE_FINAL_TARGET_IDENTITY":
            anchors = _mapping(
                lifecycle.get("retrospective_final_target_identity_positive_damage"),
                "retrospective lifecycle",
            )
            retrospective_backfill = anchors.get("uses_final_target_identity_backfill") is True
            prefix_censored = False
        elif (
            lifecycle_anchor_mode
            == "STRICT_PREFIX_EVENT_TIME_VOTING_CENSORED_AT_LAST_BOUND_DECISION"
        ):
            anchors = _mapping(
                lifecycle.get("strict_prefix_event_time_voting_positive_damage"),
                "strict-prefix event-time voting lifecycle",
            )
            retrospective_backfill = False
            prefix_censored = True
        else:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"unsupported lifecycle anchor mode {lifecycle_anchor_mode}"
            )
        first = anchors.get("first_anchor")
        last = anchors.get("last_anchor")
        if not isinstance(first, Mapping) or not isinstance(last, Mapping):
            continue
        start = _integer(first.get("timestamp_ms"), "lifecycle start")
        death = _mapping(target.get("death"), "target death").get("anchor")
        last_order = _anchor_order_key(last, "last positive damage anchor")
        end_order = last_order
        end_source = "LAST_POSITIVE_DAMAGE"
        if isinstance(death, Mapping):
            death_order = _anchor_order_key(death, "death anchor")
            death_is_available = not prefix_censored or (
                anchors.get("target_observed_dead_at_cutoff") is True
                and death_order
                < tuple(
                    _array(
                        anchors.get("cutoff_exclusive_order_key"),
                        "pack cutoff order key",
                    )
                )
            )
            if death_is_available and death_order > end_order:
                end_order = death_order
                end_source = "DEATH_MARKER"
        right_censored = prefix_censored and anchors.get("right_censored_at_cutoff") is True
        if right_censored:
            cutoff_anchor = _mapping(anchors.get("cutoff_anchor"), "pack cutoff anchor")
            cutoff_order = _anchor_order_key(cutoff_anchor, "pack cutoff anchor")
            if cutoff_order > end_order:
                end_order = cutoff_order
                end_source = "LAST_BOUND_DECISION_RIGHT_CENSOR"
        end = end_order[0]
        if end < start:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "pack lifecycle interval ends before it starts"
            )
        intervals.append(
            (start, end, guid, end_source, retrospective_backfill, right_censored)
        )
    output = []
    for gap in GAP_SENSITIVITY_MS:
        components: list[JSONMap] = []
        current: JSONMap | None = None
        for (
            start,
            end,
            guid,
            end_source,
            retrospective_backfill,
            right_censored,
        ) in sorted(intervals):
            if current is None or start > current["end_timestamp_ms"] + gap:
                current = {
                    "component_index": len(components),
                    "start_timestamp_ms": start,
                    "end_timestamp_ms": end,
                    "target_guids": [guid],
                    "target_end_anchor_sources": {guid: end_source},
                    "retrospective_identity_backfill_target_guids": (
                        [guid] if retrospective_backfill else []
                    ),
                    "right_censored_target_guids": [guid] if right_censored else [],
                }
                components.append(current)
            else:
                current["end_timestamp_ms"] = max(current["end_timestamp_ms"], end)
                current["target_guids"].append(guid)
                current["target_end_anchor_sources"][guid] = end_source
                if retrospective_backfill:
                    current["retrospective_identity_backfill_target_guids"].append(guid)
                if right_censored:
                    current["right_censored_target_guids"].append(guid)
        for component in components:
            component["target_guids"].sort()
            component["retrospective_identity_backfill_target_guids"].sort()
            component["right_censored_target_guids"].sort()
            component["target_count"] = len(component["target_guids"])
        output.append(
            {
                "registry_scope": registry_scope,
                "source_registry_target_count": len(target_guids),
                "lifecycle_anchor_mode": lifecycle_anchor_mode,
                "retrospective_final_identity_backfill_allowed": (
                    lifecycle_anchor_mode == "RETROSPECTIVE_FINAL_TARGET_IDENTITY"
                ),
                "future_suffix_used": not prefix_censored,
                "gap_ms": gap,
                "status": "LIFECYCLE_OVERLAP_SENSITIVITY_NOT_EXACT_PULL_MEMBERSHIP",
                "interval_target_count": len(intervals),
                "component_count": len(components),
                "components": components,
            }
        )
    return output


def _quantile(values: Sequence[int], fraction: float) -> float:
    if not values:
        raise ValueError("quantile needs values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _attach_leave_instance_out_priors(rows: list[JSONMap]) -> None:
    observations: dict[int, dict[tuple[str, str, str], int]] = defaultdict(dict)
    healed_deaths: dict[int, set[tuple[str, str, str]]] = defaultdict(set)
    for row in rows:
        source = _mapping(row.get("source_identity"), "source identity")
        instance_id = str(source["instance_id"])
        wave_id = str(source["wave_id"])
        for target in _array(row.get("targets"), "row targets"):
            target = _mapping(target, "target")
            entry = target.get("creature_entry_id")
            proxy = _mapping(target.get("health_evidence"), "health evidence").get(
                "retrospective_kill_budget_proxy"
            )
            healing = _mapping(
                target.get("historical_outcome"), "historical outcome"
            ).get("observed_healing_received")
            if isinstance(entry, int) and isinstance(proxy, int) and proxy > 0:
                observation_key = (instance_id, wave_id, str(target.get("target_guid")))
                if isinstance(healing, int) and not isinstance(healing, bool) and healing == 0:
                    prior = observations[entry].setdefault(observation_key, proxy)
                    if prior != proxy:
                        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                            "duplicate target has inconsistent kill-budget evidence"
                        )
                else:
                    healed_deaths[entry].add(observation_key)
    for row in rows:
        instance_id = str(_mapping(row.get("source_identity"), "source identity")["instance_id"])
        for target in _array(row.get("targets"), "row targets"):
            target = _mapping(target, "target")
            entry = target.get("creature_entry_id")
            health = _mapping(target.get("health_evidence"), "health evidence")
            values = [
                value
                for (other_instance, _, _), value in observations.get(entry, {}).items()
                if other_instance != instance_id
            ]
            excluded_healed = sum(
                other_instance != instance_id
                for other_instance, _, _ in healed_deaths.get(entry, set())
            )
            if not values:
                health["same_entry_leave_instance_out_prior"] = {
                    "status": "MISSING_NO_OTHER_SOURCE_INSTANCE_ZERO_HEALING_DEATH_SUPPORT",
                    "support_count": 0,
                    "support_filter": "DEAD_TARGET_WITH_ZERO_OBSERVED_HEALING",
                    "excluded_healed_death_support_count": excluded_healed,
                    "historical_truth": False,
                }
                continue
            health["same_entry_leave_instance_out_prior"] = {
                "status": "HYPOTHESIS_PRIOR_NOT_EXACT_HP",
                "support_count": len(values),
                "support_filter": "DEAD_TARGET_WITH_ZERO_OBSERVED_HEALING",
                "excluded_healed_death_support_count": excluded_healed,
                "source_instance_count": len(
                    {
                        other_instance
                        for other_instance, _, _ in observations[int(entry)]
                        if other_instance != instance_id
                    }
                ),
                "minimum": min(values),
                "q1": _quantile(values, 0.25),
                "median": statistics.median(values),
                "q3": _quantile(values, 0.75),
                "maximum": max(values),
                "historical_truth": False,
            }


def _build_row(
    *,
    request: Mapping[str, Any],
    source_request: Mapping[str, Any],
    reconstruction_wave: Mapping[str, Any],
    episode: Mapping[str, Any],
    timeline_record: Mapping[str, Any],
) -> JSONMap:
    source = deepcopy(dict(_mapping(request.get("source_identity"), "source identity")))
    decision_rows, decision_summary = _decision_evidence(
        request=request, episode=episode
    )
    request_with_internal = deepcopy(dict(request))
    request_with_internal["_decision_rows"] = decision_rows
    composition = _mapping(source_request.get("composition"), "source composition")
    base_request = _mapping(composition.get("request"), "base request")
    encounter = _mapping(base_request.get("encounter"), "base encounter")
    duration = encounter.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "base request duration must be positive"
        )
    duration_ms = int(round(float(duration) * 1000))
    targets = [
        _target_projection(_mapping(raw, "reconstruction target"))
        for raw in _array(reconstruction_wave.get("targets"), "reconstruction targets")
        if _mapping(raw, "reconstruction target").get(
            "voting_for_simulator_target_model"
        )
        is True
    ]
    for target_index, target in enumerate(targets):
        target["target_index"] = target_index
    timeline = _timeline_evidence(
        request=request_with_internal,
        record=timeline_record,
        targets=targets,
        diagnostic_duration_ms=duration_ms,
    )
    reconstruction_sha = _verify_content_address(
        reconstruction_wave, "selected reconstruction wave"
    )
    timeline_binding = _mapping(
        timeline_record.get("reconstruction_binding"), "timeline reconstruction binding"
    )
    if timeline_binding.get("wave_content_sha256") != reconstruction_sha:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "timeline/reconstruction wave content addresses differ"
        )
    base_targets = _array(encounter.get("targets"), "base request targets")
    row = {
        "schema": ROW_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": ROW_STATUS,
        "segment_ref": request.get("segment_ref"),
        "base_request_sha256": request.get("base_request_sha256"),
        "source_identity": source,
        "base_request_shape": {
            "duration_ms": duration_ms,
            "target_count": len(base_targets),
            "encounter_useHealth": encounter.get("useHealth"),
            "all_target_health_stats_positive": _mapping(
                request.get("base_request_dynamic_gate"), "base health gate"
            ).get("all_target_health_stats_positive"),
        },
        "source_wave_shape": {
            "duration_ms": timeline["windows"]["reconstruction_wave"]["duration_ms"],
            "voting_target_count": len(targets),
            "observed_dead_target_count": sum(
                _mapping(target.get("death"), "target death").get("observed") is True
                for target in targets
            ),
            "right_censored_target_count": sum(
                _mapping(target.get("death"), "target death").get("observed") is not True
                for target in targets
            ),
        },
        "structural_mismatch": {
            "base_request_single_generic_target_matches_source_registry": len(base_targets)
            == len(targets),
            "base_request_duration_covers_bound_decision_span": duration_ms
            >= decision_summary["span_ms"],
            "source_build_validity_beyond_bound_decisions_proven": False,
        },
        "execution_window_contract": timeline["windows"],
        "target_registry_evidence": timeline["target_registry_evidence"],
        "decision_summary": decision_summary,
        "bound_decision_prefix_deltas": decision_rows,
        "targets": targets,
        "pack_hypothesis_family": [],
        "team_trace": timeline["team_trace"],
        "armor_cast_candidate_evidence": timeline["armor_cast_candidate_evidence"],
        "classification_transition_evidence": timeline[
            "classification_transition_evidence"
        ],
        "alive_at_last_bound_decision_health_lower_bounds": timeline[
            "alive_at_last_bound_decision_health_lower_bounds"
        ],
        "identifiability": {
            "exact_initial_or_max_health": False,
            "exact_base_or_effective_armor": False,
            "exact_attackability_intervals": False,
            "historical_factual_death_anchors": True,
            "historical_exact_player_loo_damage_schedule": True,
            "counterfactual_team_kill_clock": False,
            "external_core_aura_events_available": False,
        },
        "derived_request_template": None,
        "dynamic_load_config": None,
        "blockers": [
            {
                "code": "SOURCE_BOUND_EXECUTION_WINDOW_NOT_DECLARED",
                "detail": "full historical wave and 20.001s diagnostic slice have distinct roles; neither is silently selected as the training execution window",
            },
            {
                "code": "SOURCE_BOUND_TARGET_REGISTRY_NOT_BOUND",
                "detail": "observed lifecycle and pack-sensitivity evidence is materialized, but no hypothesis branch is selected as simulator truth",
            },
            {
                "code": "EXACT_INITIAL_OR_MAX_HEALTH_NOT_IDENTIFIED",
                "detail": "damage-through-death and leave-instance-out summaries remain proxies/priors",
            },
            {
                "code": "EXACT_BASE_OR_EFFECTIVE_ARMOR_NOT_IDENTIFIED",
                "detail": "the External core stream has no aura lifecycle or numeric armor state",
            },
            {
                "code": "EXACT_ATTACKABILITY_INTERVALS_NOT_IDENTIFIED",
                "detail": "positive damage is point evidence and event activity is only a retrospective envelope",
            },
            {
                "code": "COUNTERFACTUAL_TEAM_KILL_CLOCK_NOT_CALIBRATED",
                "detail": "the exact historical LOO schedule is factual control evidence, not a policy-responsive teammate model",
            },
        ],
    }
    full_registry = {str(target["target_guid"]) for target in targets}
    last_prefix_seen = {
        str(guid)
        for decision in decision_rows
        for guid in _array(
            _mapping(
                decision.get("strict_prefix_state"), "decision strict prefix"
            ).get("new_seen_target_guids"),
            "new strict-prefix seen targets",
        )
    }
    if not last_prefix_seen <= full_registry:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "strict-prefix seen target is absent from reconstruction registry"
        )
    row["pack_hypothesis_family"] = [
        hypothesis
        for registry_scope, registry, lifecycle_mode in (
            (
                "FULL_RECONSTRUCTION_WAVE",
                full_registry,
                "EVENT_TIME_VOTING_FULL_WAVE_RETROSPECTIVE",
            ),
            (
                "FULL_RECONSTRUCTION_WAVE",
                full_registry,
                "RETROSPECTIVE_FINAL_TARGET_IDENTITY",
            ),
            (
                "SEEN_BY_LAST_BOUND_DECISION_STRICT_PREFIX",
                last_prefix_seen,
                "STRICT_PREFIX_EVENT_TIME_VOTING_CENSORED_AT_LAST_BOUND_DECISION",
            ),
        )
        for hypothesis in _pack_hypotheses(
            targets,
            target_guids=registry,
            registry_scope=registry_scope,
            lifecycle_anchor_mode=lifecycle_mode,
        )
    ]
    return _content_addressed(row)


def _validate_row(value: Mapping[str, Any]) -> JSONMap:
    row = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        row.get("schema") != ROW_SCHEMA
        or row.get("implementation_revision") != IMPLEMENTATION_REVISION
        or row.get("status") != ROW_STATUS
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "environment evidence row identity differs"
        )
    _verify_content_address(row, "environment evidence row")
    if row.get("derived_request_template") is not None or row.get("dynamic_load_config") is not None:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "evidence-only row published an executable pair"
        )
    if _mapping(row.get("execution_window_contract"), "execution window").get(
        "selected_execution_window"
    ) is not None:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "evidence-only row selected an execution window"
        )
    identifiability = _mapping(row.get("identifiability"), "identifiability")
    for key in (
        "exact_initial_or_max_health",
        "exact_base_or_effective_armor",
        "exact_attackability_intervals",
        "counterfactual_team_kill_clock",
        "external_core_aura_events_available",
    ):
        if identifiability.get(key) is not False:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"evidence-only row incorrectly identifies {key}"
            )
    codes = {
        _text(_mapping(raw, "blocker").get("code"), "blocker code")
        for raw in _array(row.get("blockers"), "blockers")
    }
    if codes != _REQUIRED_BLOCKERS:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "environment evidence blocker set differs"
        )
    team = _mapping(row.get("team_trace"), "team trace")
    focal = _text(team.get("focal_player_guid"), "focal player").casefold()
    loo = [
        _mapping(raw, "LOO event")
        for raw in _array(
            team.get("leave_one_out_exact_player_damage_events"), "LOO events"
        )
    ]
    excluded = [
        _mapping(raw, "focal event")
        for raw in _array(
            team.get("excluded_focal_exact_player_damage_events"), "focal events"
        )
    ]
    if any(str(event.get("actor_player_guid")).casefold() == focal for event in loo):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "focal damage survived leave-one-player-out"
        )
    if any(str(event.get("actor_player_guid")).casefold() != focal for event in excluded):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "nonfocal damage entered excluded focal lane"
        )
    if any(
        _mapping(event.get("window_membership"), "team event window membership").get(
            "materialized_team_trace_union"
        )
        is not True
        for event in loo + excluded
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "team runtime schedule contains an event outside its materialized union"
        )
    armor = _mapping(
        row.get("armor_cast_candidate_evidence"), "armor candidate evidence"
    )
    armor_union = [
        _mapping(raw, "union armor candidate")
        for raw in _array(
            armor.get("materialized_team_trace_union"), "union armor candidates"
        )
    ]
    armor_full = [
        _mapping(raw, "full-wave armor candidate")
        for raw in _array(
            armor.get("full_reconstruction_wave_retrospective"),
            "full-wave armor candidates",
        )
    ]
    expected_armor_union = [
        event
        for event in armor_full
        if _mapping(event.get("window_membership"), "armor window membership").get(
            "materialized_team_trace_union"
        )
        is True
    ]
    if (
        armor_union != expected_armor_union
        or armor.get("outside_materialized_union_count")
        != len(armor_full) - len(armor_union)
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "armor candidate window accounting differs"
        )
    targets = [_mapping(raw, "target") for raw in _array(row.get("targets"), "targets")]
    if [target.get("target_index") for target in targets] != list(range(len(targets))):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "target indexes are not contiguous"
        )
    for target in targets:
        health = _mapping(target.get("health_evidence"), "health evidence")
        armor = _mapping(target.get("armor_evidence"), "armor evidence")
        attackability = _mapping(target.get("attackability_evidence"), "attackability")
        if (
            health.get("exact_initial_or_max_health") is not None
            or armor.get("exact_base_armor") is not None
            or armor.get("exact_effective_armor_events") is not None
            or attackability.get("exact_intervals") is not None
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "target evidence promotes an unobserved exact state"
            )
        lifecycle = _mapping(
            target.get("target_lifecycle_evidence"), "target lifecycle"
        )
        if lifecycle.get("event_time_runtime_lane_backfilled") is not False:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "event-time target lane was backfilled"
            )
        prior = _mapping(
            health.get("same_entry_leave_instance_out_prior"), "health prior"
        )
        if prior.get("support_filter") != "DEAD_TARGET_WITH_ZERO_OBSERVED_HEALING":
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "health prior does not exclude healed deaths"
            )
    for raw_hypothesis in _array(
        row.get("pack_hypothesis_family"), "pack hypothesis family"
    ):
        hypothesis = _mapping(raw_hypothesis, "pack hypothesis")
        for raw_component in _array(hypothesis.get("components"), "pack components"):
            component = _mapping(raw_component, "pack component")
            if _integer(component.get("end_timestamp_ms"), "pack end") < _integer(
                component.get("start_timestamp_ms"), "pack start"
            ):
                raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                    "pack component ends before it starts"
                )
    _assert_path_free(row)
    return row


def _load_inputs(
    *,
    dynamic_manifest_path: Path,
    source_bundle_path: Path,
    reconstruction_manifest_path: Path,
    episode_manifest_path: Path,
    timeline_manifest_path: Path,
) -> tuple[JSONMap, JSONMap, JSONMap, JSONMap, JSONMap, JSONMap]:
    dynamic = dynamic_v1.load_historical_fury_source_bound_dynamic_config_v1(
        dynamic_manifest_path
    )
    source_bundle = _load_json(source_bundle_path, "source bundle")
    source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
        source_bundle, verify_source_bytes=False
    )
    reconstruction, _ = reconstruction_v2.load_external_reconstruction_manifest(
        reconstruction_manifest_path, verify_artifacts=False
    )
    episodes = _load_json(episode_manifest_path, "episode manifest")
    episode_sha = _manifest_identity(
        episodes,
        schema=episode_v1.MANIFEST_SCHEMA,
        revision=episode_v1.IMPLEMENTATION_REVISION,
        status=episode_v1.STATUS,
        label="episode manifest",
    )
    timeline, _ = timeline_v2.load_external_team_timeline_manifest(
        timeline_manifest_path, verify_inputs=False, verify_partitions=False
    )
    dynamic_closure = _mapping(dynamic.get("input_closure"), "dynamic input closure")
    source_sha = _verify_content_address(source_bundle, "source bundle")
    reconstruction_sha = _verify_content_address(reconstruction, "reconstruction manifest")
    timeline_sha = _verify_content_address(timeline, "timeline manifest")
    if _mapping(
        dynamic_closure.get("source_bound_prototype_bundle"), "dynamic source bundle"
    ).get("content_sha256") != source_sha:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "dynamic manifest does not bind the source bundle"
        )
    if _mapping(
        dynamic_closure.get("chronicle_external_team_timeline"), "dynamic timeline"
    ).get("content_sha256") != timeline_sha:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "dynamic manifest does not bind the team timeline"
        )
    if _mapping(
        _mapping(timeline.get("input_closure"), "timeline input closure").get(
            "reconstruction_manifest"
        ),
        "timeline reconstruction",
    ).get("content_sha256") != reconstruction_sha:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "timeline manifest does not bind the reconstruction manifest"
        )
    if _mapping(
        _mapping(episodes.get("input_closure"), "episode input closure").get(
            "external_v2_timeline"
        ),
        "episode timeline",
    ).get("content_sha256") != timeline_sha:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "episode manifest does not bind the team timeline"
        )
    return dynamic, source_bundle, reconstruction, episodes, timeline, {
        "source_bound_dynamic_preparation": {
            "schema": dynamic.get("schema"),
            "implementation_revision": dynamic.get("implementation_revision"),
            "content_sha256": _verify_content_address(dynamic, "dynamic manifest"),
        },
        "source_bound_prototype_bundle": {
            "schema": source_bundle.get("schema"),
            "implementation_revision": source_bundle.get("implementation_revision"),
            "content_sha256": source_sha,
        },
        "external_reconstruction": {
            "schema": reconstruction.get("schema"),
            "implementation_revision": reconstruction.get("implementation_revision"),
            "content_sha256": reconstruction_sha,
        },
        "fury_expert_episodes": {
            "schema": episodes.get("schema"),
            "implementation_revision": episodes.get("implementation_revision"),
            "content_sha256": episode_sha,
        },
        "external_team_timeline": {
            "schema": timeline.get("schema"),
            "implementation_revision": timeline.get("implementation_revision"),
            "content_sha256": timeline_sha,
        },
    }


def build_historical_fury_source_bound_environment_evidence_v1(
    *,
    dynamic_manifest_path: str | Path = DEFAULT_DYNAMIC_MANIFEST,
    source_bundle_path: str | Path = DEFAULT_SOURCE_BUNDLE,
    reconstruction_manifest_path: str | Path = DEFAULT_RECONSTRUCTION_MANIFEST,
    episode_manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
    timeline_manifest_path: str | Path = DEFAULT_TIMELINE_MANIFEST,
) -> tuple[list[JSONMap], JSONMap]:
    """Build compact rows and manifest metadata without simulator/HPC execution."""

    paths = [
        Path(value).expanduser().resolve()
        for value in (
            dynamic_manifest_path,
            source_bundle_path,
            reconstruction_manifest_path,
            episode_manifest_path,
            timeline_manifest_path,
        )
    ]
    dynamic, source_bundle, reconstruction, episodes, timeline, input_closure = _load_inputs(
        dynamic_manifest_path=paths[0],
        source_bundle_path=paths[1],
        reconstruction_manifest_path=paths[2],
        episode_manifest_path=paths[3],
        timeline_manifest_path=paths[4],
    )
    requests = [
        _mapping(raw, "dynamic request")
        for raw in _array(dynamic.get("requests"), "dynamic requests")
    ]
    if len(requests) != EXPECTED_REQUEST_COUNT:
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            f"expected {EXPECTED_REQUEST_COUNT} dynamic requests"
        )
    source_requests = {
        _text(_mapping(raw, "source request").get("segment_ref"), "segment_ref"): _mapping(
            raw, "source request"
        )
        for raw in _array(source_bundle.get("requests"), "source requests")
    }
    for request in requests:
        segment_ref = _text(request.get("segment_ref"), "segment_ref")
        source_request = source_requests.get(segment_ref)
        if (
            source_request is None
            or source_request.get("request_sha256") != request.get("base_request_sha256")
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "dynamic/source request identity differs"
            )
    reconstruction_rows, reconstruction_closure = _load_selected_reconstruction(
        manifest=reconstruction,
        manifest_path=paths[2],
        request_rows=requests,
    )
    episode_rows, episode_closure = _selected_episode_rows(
        manifest=episodes,
        manifest_path=paths[3],
        request_rows=requests,
    )
    timeline_rows, timeline_closure = _selected_timeline_rows(
        manifest=timeline,
        manifest_path=paths[4],
        request_rows=requests,
    )
    rows = []
    for request in sorted(requests, key=lambda row: str(row["segment_ref"])):
        source = _mapping(request.get("source_identity"), "source identity")
        wave_key = (
            str(source["instance_id"]),
            str(source["encounter_id"]),
            str(source["wave_id"]),
        )
        episode_key = (
            str(source["instance_id"]),
            str(source["encounter_id"]),
            str(source["player_guid"]).casefold(),
        )
        rows.append(
            _build_row(
                request=request,
                source_request=source_requests[str(request["segment_ref"])],
                reconstruction_wave=reconstruction_rows[wave_key],
                episode=episode_rows[episode_key],
                timeline_record=timeline_rows[wave_key],
            )
        )
    _attach_leave_instance_out_priors(rows)
    # Priors alter row content, so refresh each row address after attachment.
    rows = [_content_addressed(row) for row in rows]
    rows = [_validate_row(row) for row in rows]
    metadata = {
        "input_closure": {
            **input_closure,
            "selected_reconstruction_artifacts": reconstruction_closure,
            "relevant_episode_partitions": episode_closure,
            "relevant_timeline_partitions": timeline_closure,
        },
        "extraction_contract": {
            "relevant_instance_count": len({row["source_identity"]["instance_id"] for row in rows}),
            "selected_segment_count": len(rows),
            "selected_source_wave_count": len(
                {
                    (
                        row["source_identity"]["instance_id"],
                        row["source_identity"]["encounter_id"],
                        row["source_identity"]["wave_id"],
                    )
                    for row in rows
                }
            ),
            "timeline_partition_read_mode": "STREAMING_JSONL_IDENTITY_MATCH_ONLY",
            "episode_partition_read_mode": "STREAMING_JSONL_IDENTITY_MATCH_ONLY",
            "all_relevant_partitions_verified_through_eof": True,
            "selected_timeline_wave_record_count_retained_until_row_build": len(
                timeline_rows
            ),
            "maximum_selected_timeline_wave_records_from_one_partition": max(
                (
                    _integer(
                        closure.get("selected_row_count"),
                        "timeline selected row count",
                        minimum=0,
                    )
                    for closure in timeline_closure
                ),
                default=0,
            ),
            "selected_episode_record_count_retained_until_row_build": len(episode_rows),
            "published_raw_timeline_event_arrays": 0,
            "published_timeline_events_are_compact_projections": True,
            "network_request_count": 0,
        },
    }
    return rows, metadata


def _manifest_for_rows(
    *, rows: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any], descriptor: Mapping[str, Any]
) -> JSONMap:
    target_count = sum(len(_array(row.get("targets"), "row targets")) for row in rows)
    dead_count = sum(
        _mapping(target, "target").get("death", {}).get("observed") is True
        for row in rows
        for target in _array(row.get("targets"), "row targets")
    )
    mismatch_count = sum(
        _mapping(row.get("structural_mismatch"), "structural mismatch").get(
            "base_request_single_generic_target_matches_source_registry"
        )
        is not True
        for row in rows
    )
    core = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "status": STATUS,
        "execution_status": "NOT_RUN",
        "scientific_runs_started": False,
        "simulator_run_count": 0,
        "hpc_job_count": 0,
        "network_request_count": 0,
        "comparison_authorized": False,
        "training_authorized": False,
        "deployment_authorized": False,
        "superiority_claim_authorized": False,
        "input_closure": deepcopy(metadata.get("input_closure")),
        "extraction_contract": deepcopy(metadata.get("extraction_contract")),
        "evidence_partition": deepcopy(dict(descriptor)),
        "identifiability_contract": {
            "exact_health_claimed": False,
            "exact_armor_claimed": False,
            "exact_attackability_claimed": False,
            "historical_death_used_as_candidate_independent_termination": False,
            "historical_loo_schedule_used_as_counterfactual_team_response": False,
            "go_or_start_used_as_aura_state": False,
        },
        "summary": {
            "request_count": len(rows),
            "evidence_row_count": len(rows),
            "ready_request_count": 0,
            "blocked_request_count": len(rows),
            "structural_target_count_mismatch_request_count": mismatch_count,
            "source_voting_target_count": target_count,
            "source_observed_dead_target_count": dead_count,
            "source_right_censored_target_count": target_count - dead_count,
            "bound_decision_count": sum(
                _mapping(row.get("decision_summary"), "decision summary").get(
                    "decision_count"
                )
                for row in rows
            ),
            "diagnostic_slice_covers_all_bound_decisions_count": sum(
                _mapping(
                    _mapping(row.get("execution_window_contract"), "execution window").get(
                        "base_request_diagnostic_slice"
                    ),
                    "diagnostic slice",
                ).get("covers_all_bound_decisions")
                is True
                for row in rows
            ),
            "last_prefix_seen_multiple_targets_request_count": sum(
                _mapping(row.get("decision_summary"), "decision summary").get(
                    "last_prefix_seen_target_count"
                )
                > 1
                for row in rows
            ),
            "event_time_nonvoting_positive_damage_target_count": sum(
                _mapping(
                    _mapping(target, "target").get("target_lifecycle_evidence"),
                    "target lifecycle",
                )
                .get("event_time_nonvoting_positive_damage", {})
                .get("event_count", 0)
                > 0
                for row in rows
                for target in _array(row.get("targets"), "row targets")
            ),
            "armor_cast_candidate_outside_materialized_union_count": sum(
                _mapping(
                    row.get("armor_cast_candidate_evidence"),
                    "armor candidate evidence",
                ).get("outside_materialized_union_count")
                for row in rows
            ),
            "classification_transition_evidence_count": sum(
                len(
                    _array(
                        row.get("classification_transition_evidence"),
                        "classification transition evidence",
                    )
                )
                for row in rows
            ),
            "exact_loo_damage_event_count": sum(
                len(
                    _array(
                        _mapping(row.get("team_trace"), "team trace").get(
                            "leave_one_out_exact_player_damage_events"
                        ),
                        "LOO events",
                    )
                )
                for row in rows
            ),
        },
    }
    artifact = _content_addressed(core)
    _assert_path_free(artifact)
    return artifact


def validate_historical_fury_source_bound_environment_evidence_v1(
    value: Mapping[str, Any]
) -> JSONMap:
    manifest = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
        or manifest.get("kind") != KIND
        or manifest.get("status") != STATUS
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "environment evidence manifest identity differs"
        )
    _verify_content_address(manifest, "environment evidence manifest")
    if (
        manifest.get("execution_status") != "NOT_RUN"
        or manifest.get("scientific_runs_started") is not False
        or manifest.get("simulator_run_count") != 0
        or manifest.get("hpc_job_count") != 0
        or manifest.get("network_request_count") != 0
        or manifest.get("comparison_authorized") is not False
        or manifest.get("training_authorized") is not False
        or manifest.get("deployment_authorized") is not False
        or manifest.get("superiority_claim_authorized") is not False
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "environment evidence manifest exceeded its audit boundary"
        )
    summary = _mapping(manifest.get("summary"), "summary")
    if (
        summary.get("request_count") != EXPECTED_REQUEST_COUNT
        or summary.get("evidence_row_count") != EXPECTED_REQUEST_COUNT
        or summary.get("ready_request_count") != 0
        or summary.get("blocked_request_count") != EXPECTED_REQUEST_COUNT
    ):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "environment evidence request accounting differs"
        )
    contract = _mapping(manifest.get("identifiability_contract"), "identifiability contract")
    if any(contract.get(key) is not False for key in contract):
        raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
            "environment evidence manifest promotes an unobserved claim"
        )
    _assert_path_free(manifest)
    return manifest


def publish_historical_fury_source_bound_environment_evidence_v1(
    *,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    dynamic_manifest_path: str | Path = DEFAULT_DYNAMIC_MANIFEST,
    source_bundle_path: str | Path = DEFAULT_SOURCE_BUNDLE,
    reconstruction_manifest_path: str | Path = DEFAULT_RECONSTRUCTION_MANIFEST,
    episode_manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
    timeline_manifest_path: str | Path = DEFAULT_TIMELINE_MANIFEST,
) -> tuple[Path, Path, Path, JSONMap]:
    rows, metadata = build_historical_fury_source_bound_environment_evidence_v1(
        dynamic_manifest_path=dynamic_manifest_path,
        source_bundle_path=source_bundle_path,
        reconstruction_manifest_path=reconstruction_manifest_path,
        episode_manifest_path=episode_manifest_path,
        timeline_manifest_path=timeline_manifest_path,
    )
    logical = b"".join(_canonical_bytes(row, newline=True) for row in rows)
    compressed = _gzip_bytes(logical)
    logical_sha = hashlib.sha256(logical).hexdigest()
    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    partition_name = f"environment_evidence.{logical_sha}.jsonl.gz"
    partition_path = destination / partition_name
    _atomic_write(partition_path, compressed)
    descriptor = {
        "path": partition_name,
        "record_schema": ROW_SCHEMA,
        "record_count": len(rows),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": logical_sha,
        "compressed_size_bytes": len(compressed),
        "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
        "gzip_mtime": 0,
    }
    manifest = _manifest_for_rows(rows=rows, metadata=metadata, descriptor=descriptor)
    manifest = validate_historical_fury_source_bound_environment_evidence_v1(manifest)
    payload = _canonical_bytes(manifest, newline=True)
    stable = destination / "manifest.json"
    addressed = destination / (
        "historical_fury_source_bound_environment_evidence_v1."
        + manifest["content_address"]["sha256"]
        + ".manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return stable, addressed, partition_path, manifest


def load_historical_fury_source_bound_environment_evidence_v1(
    path: str | Path = DEFAULT_OUTPUT, *, verify_partition: bool = True
) -> tuple[JSONMap, list[JSONMap]]:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        resolved = resolved / "manifest.json"
    manifest = validate_historical_fury_source_bound_environment_evidence_v1(
        _load_json(resolved, "environment evidence manifest")
    )
    rows: list[JSONMap] = []
    if verify_partition:
        descriptor = _mapping(manifest.get("evidence_partition"), "evidence partition")
        partition_path = _safe_child(resolved.parent, descriptor.get("path"), "partition path")
        if (
            partition_path.stat().st_size
            != _integer(descriptor.get("compressed_size_bytes"), "compressed size", minimum=0)
            or _sha256_file(partition_path)
            != _sha(descriptor.get("compressed_file_sha256"), "compressed sha")
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "environment evidence compressed partition differs"
            )
        digest = hashlib.sha256()
        logical_size = 0
        try:
            with gzip.open(partition_path, "rb") as handle:
                for line_number, line in enumerate(handle, 1):
                    digest.update(line)
                    logical_size += len(line)
                    rows.append(
                        _validate_row(
                            _strict_json(line, f"environment evidence row {line_number}")
                        )
                    )
        except (OSError, EOFError, gzip.BadGzipFile) as error:
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                f"cannot read environment evidence partition: {error}"
            ) from error
        if (
            len(rows) != descriptor.get("record_count")
            or logical_size != descriptor.get("logical_size_bytes")
            or digest.hexdigest() != descriptor.get("logical_content_sha256")
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "environment evidence logical partition differs"
            )
        summary = _mapping(manifest.get("summary"), "summary")
        if (
            len(rows) != summary.get("evidence_row_count")
            or sum(len(row["targets"]) for row in rows)
            != summary.get("source_voting_target_count")
            or sum(row["decision_summary"]["decision_count"] for row in rows)
            != summary.get("bound_decision_count")
        ):
            raise HistoricalFurySourceBoundEnvironmentEvidenceV1Error(
                "environment evidence manifest/partition accounting differs"
            )
    return manifest, rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dynamic-manifest", type=Path, default=DEFAULT_DYNAMIC_MANIFEST)
    parser.add_argument("--source-bundle", type=Path, default=DEFAULT_SOURCE_BUNDLE)
    parser.add_argument(
        "--reconstruction-manifest", type=Path, default=DEFAULT_RECONSTRUCTION_MANIFEST
    )
    parser.add_argument("--episode-manifest", type=Path, default=DEFAULT_EPISODE_MANIFEST)
    parser.add_argument("--timeline-manifest", type=Path, default=DEFAULT_TIMELINE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    args = parser.parse_args(argv)
    try:
        stable, addressed, partition, artifact = (
            publish_historical_fury_source_bound_environment_evidence_v1(
                output_directory=args.output_dir,
                dynamic_manifest_path=args.dynamic_manifest,
                source_bundle_path=args.source_bundle,
                reconstruction_manifest_path=args.reconstruction_manifest,
                episode_manifest_path=args.episode_manifest,
                timeline_manifest_path=args.timeline_manifest,
            )
        )
    except (OSError, ValueError, HistoricalFurySourceBoundEnvironmentEvidenceV1Error) as error:
        print(f"BLOCKED: {error}")
        return 2
    print(
        json.dumps(
            {
                "status": artifact["status"],
                "request_count": artifact["summary"]["request_count"],
                "ready_request_count": artifact["summary"]["ready_request_count"],
                "source_voting_target_count": artifact["summary"]["source_voting_target_count"],
                "bound_decision_count": artifact["summary"]["bound_decision_count"],
                "exact_loo_damage_event_count": artifact["summary"]["exact_loo_damage_event_count"],
                "content_sha256": artifact["content_address"]["sha256"],
                "stable": str(stable),
                "addressed": str(addressed),
                "partition": str(partition),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


__all__: Sequence[str] = (
    "DEFAULT_OUTPUT",
    "DEFAULT_OUTPUT_DIRECTORY",
    "HistoricalFurySourceBoundEnvironmentEvidenceV1Error",
    "IMPLEMENTATION_REVISION",
    "ROW_SCHEMA",
    "SCHEMA",
    "STATUS",
    "build_historical_fury_source_bound_environment_evidence_v1",
    "load_historical_fury_source_bound_environment_evidence_v1",
    "publish_historical_fury_source_bound_environment_evidence_v1",
    "validate_historical_fury_source_bound_environment_evidence_v1",
)


if __name__ == "__main__":
    raise SystemExit(main())
