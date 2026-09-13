"""Prepare exact historical Fury prototype/build requests without running them.

The earlier build-conditioned bundle deliberately used a small set of current
character representatives.  That is the wrong character boundary for an
historical prototype baseline.  This producer instead starts at the portable
decision/build join, selects the prototype-union segments whose *own* build is
runnable and development-eligible, re-opens each exact catalogue row, and
composes the simulator request from that historical row.

The result is a portable preparation artifact.  It never starts a simulator,
does not transplant a prototype onto a current-character representative, and
does not authorize a comparison or deployment.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import fury_build_conditioned_development_bundle_v1 as bundle_v1
from . import historical_behavior_clone_v2 as clone_v2
from . import historical_build_catalog_v1 as catalog_v1
from . import historical_fury_decision_build_join_v1 as join_v1
from . import historical_fury_behavior_prototypes_v1 as prototypes_v1
from . import historical_fury_prototype_build_gap_priority_v1 as gap_v1
from .build_request_composer_v1 import (
    BuildRequestComposerV1Error,
    compose_build_request_v1,
)
from .fury_multiseed_evaluation_v2 import derive_seed_set
from .historical_behavior_clone_simulator_adapter_v2 import (
    HistoricalBehaviorCloneSimulatorAdapterV2Error,
    exact_validation_receipt_v2,
    policy_id_for_prototype_v2,
)


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_prototype_bundle/v1"
IMPLEMENTATION_REVISION = "v1.0_exact_prototype_segment_source_binding"
KIND = "historical_fury_source_bound_prototype_development_bundle"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_prototype_bundle_content/v1"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GAP_MANIFEST = gap_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_JOIN_MANIFEST = gap_v1.DEFAULT_JOIN_MANIFEST
DEFAULT_CATALOG_MANIFEST = gap_v1.DEFAULT_CATALOG_MANIFEST
DEFAULT_MODEL_MANIFEST = clone_v2.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_PROTOTYPE_MANIFEST = prototypes_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_prototype_bundle"
    / "v1"
)

DEVELOPMENT_SEED_COUNT = 32
DEVELOPMENT_SEED_PHASE = "source_bound_historical_prototype_smoke"
SOURCE_SELECTION = (
    "PROTOTYPE_UNION_EXACT_CATALOG_SEGMENT_AND_RUNTIME_EXECUTABLE_AND_"
    "DEVELOPMENT_BUILD_ELIGIBLE"
)


class HistoricalFurySourceBoundPrototypeBundleV1Error(RuntimeError):
    """A source closure or prepared bundle does not close exactly."""


@dataclass(frozen=True)
class _SourcePaths:
    gap_manifest: Path
    join_manifest: Path
    catalog_manifest: Path
    catalog_data: Path
    prototype_manifest: Path
    model_manifest: Path


@dataclass(frozen=True)
class _SourceMaterial:
    input_closure: JSONMap
    selected: tuple[tuple[JSONMap, JSONMap], ...]
    model_by_prototype: Mapping[str, JSONMap]
    gap_sha256: str
    model_manifest_sha256: str


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
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"cannot read {path}: {error}"
        ) from error
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> tuple[JSONMap, bytes]:
    # Reuse the strict duplicate-key/non-finite loader already used by the gap
    # artifact.  Converting its error keeps this module's public boundary clear.
    try:
        return gap_v1._load_json(path, label)
    except gap_v1.HistoricalFuryPrototypeBuildGapPriorityV1Error as error:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(str(error)) from error


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    payload = deepcopy(dict(core))
    payload.pop("content_address", None)
    return {
        **payload,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address; host locators forbidden",
            "sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        },
    }


def _portable_projection(value: Any, *, key: str | None = None) -> Any:
    """Replace source locators while retaining the composed scientific state."""

    locator = key is not None and (
        key == "path"
        or key.endswith("_path")
        or key in {"catalog_path", "database", "wowsims_root"}
    )
    if locator and isinstance(value, str) and _looks_absolute(value):
        return "NON_IDENTITY_SOURCE_LOCATOR"
    if isinstance(value, Mapping):
        return {
            str(child_key): _portable_projection(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_portable_projection(child) for child in value]
    return deepcopy(value)


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


def _assert_path_free(value: Any, *, label: str = "bundle") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_path_free(child, label=f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_path_free(child, label=f"{label}[{index}]")
    elif isinstance(value, str) and _looks_absolute(value):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} contains an absolute host locator"
        )


def _resolve_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} does not exist: {path}"
        )
    return path


def _resolve_locator(base: Path, value: Any, label: str) -> Path:
    raw = _text(value, label)
    normalized = raw.replace("\\", "/")
    basename = normalized.rstrip("/").rsplit("/", 1)[-1]
    native = Path(raw).expanduser()
    candidates: list[Path] = []
    if native.is_absolute():
        candidates.append(native)
    else:
        portable_parts = tuple(
            part for part in normalized.split("/") if part not in ("", ".")
        )
        if ".." not in portable_parts:
            candidates.append(base.joinpath(*portable_parts))
    # A manifest can be copied from Windows to Linux (or the reverse).  The
    # original absolute spelling is then foreign to pathlib, while the bound
    # artifact is intentionally colocated with the copied manifest.
    if basename:
        candidates.append(base / basename)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise HistoricalFurySourceBoundPrototypeBundleV1Error(
        f"{label} cannot be resolved relative to {base}"
    )


def _verify_generic_content_address(
    document: Mapping[str, Any], label: str
) -> str:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    core = deepcopy(dict(document))
    core.pop("content_address", None)
    expected = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    if (
        address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
        or address.get("sha256") != expected
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"{label} content address differs"
        )
    return expected


def _catalog_flags(segment: Mapping[str, Any]) -> JSONMap:
    coverage = _mapping(segment.get("coverage"), "catalog coverage")
    comparison = _mapping(coverage.get("comparison"), "catalog comparison coverage")
    return {
        "runtime_executable": coverage.get("runtime_executable") is True,
        "representative_build_eligible": (
            coverage.get("representative_build_eligible") is True
        ),
        "development_build_eligible": (
            coverage.get("development_build_eligible") is True
        ),
        "comparison_eligible": comparison.get("eligible") is True,
    }


def _catalog_portable_contract(manifest: Mapping[str, Any]) -> JSONMap:
    return {
        "schema": manifest.get("schema"),
        "implementation_revision": manifest.get("implementation_revision"),
        "kind": manifest.get("kind"),
        "causal_contract": deepcopy(manifest.get("causal_contract")),
    }


def _load_selected_catalog_rows(
    *,
    catalog_path: Path,
    selected_dictionary_rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[JSONMap, JSONMap], ...]:
    wanted: dict[int, Mapping[str, Any]] = {}
    for row in selected_dictionary_rows:
        line_number = _integer(
            row.get("catalog_line_number"), "catalog_line_number", minimum=1
        )
        if line_number in wanted:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "selected source segments duplicate a catalogue line"
            )
        wanted[line_number] = row
    if not wanted:
        return ()

    maximum_line = max(wanted)
    found: list[tuple[JSONMap, JSONMap]] = []
    try:
        with gzip.open(catalog_path, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                dictionary_row = wanted.get(line_number)
                if dictionary_row is None:
                    if line_number >= maximum_line:
                        break
                    continue
                segment = gap_v1._strict_json_bytes(
                    raw_line, f"catalog row {line_number}"
                )
                if segment.get("schema") != catalog_v1.RECORD_SCHEMA:
                    raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                        "selected catalogue row schema differs"
                    )
                portable = join_v1._portable_projection(segment)
                source_sha = hashlib.sha256(_canonical_bytes(portable)).hexdigest()
                segment_ref = f"sha256:{source_sha}"
                if (
                    segment_ref != dictionary_row.get("segment_ref")
                    or source_sha
                    != dictionary_row.get(
                        "portable_source_segment_content_sha256"
                    )
                    or segment.get("identity") != dictionary_row.get("identity")
                ):
                    raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                        "selected catalogue row differs from its content-addressed segment_ref"
                    )
                observation = _mapping(
                    segment.get("observation"), "catalog observation"
                )
                valid_from = _mapping(
                    observation.get("valid_from"), "catalog valid_from"
                )
                expected_valid_from = {
                    key: deepcopy(valid_from.get(key))
                    for key in (
                        "timestamp_ms",
                        "encounter_id",
                        "event_index",
                        "message_ordinal",
                    )
                }
                if expected_valid_from != dictionary_row.get("valid_from"):
                    raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                        "selected catalogue causal anchor differs from the join dictionary"
                    )
                equipment = _mapping(segment.get("equipment"), "catalog equipment")
                talents = _mapping(segment.get("talents"), "catalog talents")
                if (
                    hashlib.sha256(
                        _canonical_bytes(join_v1._portable_projection(equipment))
                    ).hexdigest()
                    != dictionary_row.get("equipment_content_sha256")
                    or hashlib.sha256(
                        _canonical_bytes(join_v1._portable_projection(talents))
                    ).hexdigest()
                    != dictionary_row.get("talents_content_sha256")
                    or _catalog_flags(segment)
                    != dictionary_row.get("coverage_flags")
                ):
                    raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                        "selected catalogue build/coverage identity differs from the join"
                    )
                found.append((deepcopy(dict(dictionary_row)), segment))
    except (OSError, EOFError) as error:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"cannot stream selected catalogue rows: {error}"
        ) from error
    if len(found) != len(wanted):
        missing = len(wanted) - len(found)
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"catalogue omitted {missing} selected source-bound segment(s)"
        )
    return tuple(sorted(found, key=lambda item: str(item[0]["segment_ref"])))


def _model_manifest_index(
    path: Path,
) -> tuple[JSONMap, str, dict[str, Mapping[str, Any]]]:
    manifest, raw = _load_json(path, "behavior clone model manifest")
    if (
        manifest.get("schema") != clone_v2.MANIFEST_SCHEMA
        or manifest.get("implementation_revision") != clone_v2.IMPLEMENTATION_REVISION
        or manifest.get("status") != clone_v2.STATUS
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "behavior clone model manifest identity differs"
        )
    content_sha = _verify_generic_content_address(manifest, "model manifest")
    boundaries = _mapping(
        manifest.get("scientific_boundaries"), "model scientific boundaries"
    )
    for key in (
        "comparison_authorized",
        "same_equipment_matched_seed_comparison_authorized",
        "deployment_authorized",
        "superiority_claim_authorized",
    ):
        if boundaries.get(key) is not False:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                f"model manifest must keep {key}=false"
            )
    rows = _array(manifest.get("models"), "model descriptors")
    indexed: dict[str, Mapping[str, Any]] = {}
    for raw_row in rows:
        row = _mapping(raw_row, "model descriptor")
        prototype_id = _text(row.get("prototype_id"), "model prototype_id")
        if prototype_id in indexed:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "model manifest duplicates a prototype identity"
            )
        indexed[prototype_id] = row
    summary = _mapping(manifest.get("summary"), "model manifest summary")
    if _integer(summary.get("model_count"), "model_count") != len(indexed):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "model manifest model count differs"
        )
    return manifest, content_sha, indexed


def _prototype_manifest_binding(
    *, path: Path, join_manifest: Mapping[str, Any]
) -> tuple[JSONMap, JSONMap, dict[str, Mapping[str, Any]]]:
    manifest, raw = _load_json(path, "historical prototype manifest")
    if (
        manifest.get("schema") != prototypes_v1.MANIFEST_SCHEMA
        or manifest.get("implementation_revision")
        != prototypes_v1.IMPLEMENTATION_REVISION
        or manifest.get("status") != prototypes_v1.STATUS
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "historical prototype manifest identity differs"
        )
    content_sha = _verify_generic_content_address(
        manifest, "historical prototype manifest"
    )
    prototype_rows = []
    prototype_ids: set[str] = set()
    prototype_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_row in _array(manifest.get("prototypes"), "historical prototypes"):
        row = _mapping(raw_row, "historical prototype")
        prototype_id = _text(row.get("prototype_id"), "prototype_id")
        if prototype_id in prototype_ids:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "historical prototype manifest duplicates a prototype"
            )
        prototype_ids.add(prototype_id)
        prototype_by_id[prototype_id] = row
        prototype_rows.append(
            {
                key: deepcopy(row.get(key))
                for key in (
                    "prototype_id",
                    "prototype_family",
                    "selection_rule",
                    "member_count",
                    "members",
                )
            }
        )
    join_closure = _mapping(join_manifest.get("input_closure"), "join input closure")
    join_prototype = _mapping(
        join_closure.get("behavior_prototype_manifest"),
        "join prototype manifest binding",
    )
    join_episode = _mapping(
        join_closure.get("fury_episode_manifest"), "join episode manifest binding"
    )
    prototype_closure = _mapping(
        manifest.get("input_closure"), "prototype input closure"
    )
    frozen = _mapping(
        prototype_closure.get("frozen_cohort"), "prototype frozen cohort"
    )
    portable_membership_contract = {
        "schema": manifest["schema"],
        "implementation_revision": manifest["implementation_revision"],
        "status": manifest["status"],
        "selection_contract": deepcopy(manifest.get("selection_contract")),
        "prototypes": prototype_rows,
        "source_audit": deepcopy(manifest.get("source_audit")),
        "scientific_boundaries": deepcopy(manifest.get("scientific_boundaries")),
        "frozen_cohort_file_sha256": frozen.get("file_sha256"),
        "fury_episode_portable_manifest_contract_sha256": join_episode.get(
            "portable_manifest_contract_sha256"
        ),
    }
    portable_sha = hashlib.sha256(
        _canonical_bytes(portable_membership_contract)
    ).hexdigest()
    if (
        join_prototype.get("schema") != prototypes_v1.MANIFEST_SCHEMA
        or join_prototype.get("implementation_revision")
        != prototypes_v1.IMPLEMENTATION_REVISION
        or join_prototype.get("prototype_count") != len(prototype_rows)
        or join_prototype.get("portable_membership_contract_sha256")
        != portable_sha
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "join does not bind the supplied historical prototype membership"
        )
    exact = {
        "schema": prototypes_v1.MANIFEST_SCHEMA,
        "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
        "content_sha256": content_sha,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "portable_membership_contract_sha256": portable_sha,
        "prototype_count": len(prototype_rows),
        "prototype_ids": sorted(prototype_ids),
    }
    return manifest, exact, prototype_by_id


def _load_relevant_models(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    descriptors: Mapping[str, Mapping[str, Any]],
    prototype_by_id: Mapping[str, Mapping[str, Any]],
    relevant_prototypes: set[str],
) -> dict[str, JSONMap]:
    missing = relevant_prototypes - (set(descriptors) & set(prototype_by_id))
    if missing:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "no clone-v2 model exists for prototype(s): " + ", ".join(sorted(missing))
        )
    source_manifest = _mapping(
        _mapping(manifest.get("input_closure"), "model input_closure").get(
            "prototype_manifest"
        ),
        "model prototype manifest binding",
    )
    result: dict[str, JSONMap] = {}
    for prototype_id in sorted(relevant_prototypes):
        descriptor = descriptors[prototype_id]
        model_path = _resolve_locator(
            manifest_path.parent,
            descriptor.get("path"),
            f"model {prototype_id} path",
        )
        raw = model_path.read_bytes()
        if (
            len(raw)
            != _integer(descriptor.get("file_size_bytes"), "model file_size_bytes")
            or hashlib.sha256(raw).hexdigest()
            != _text(descriptor.get("file_sha256"), "model file_sha256")
        ):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                f"model bytes differ for {prototype_id}"
            )
        model, _ = _load_json(model_path, f"model {prototype_id}")
        try:
            receipt = exact_validation_receipt_v2(model)
        except HistoricalBehaviorCloneSimulatorAdapterV2Error as error:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                f"model {prototype_id} exact validation failed: {error}"
            ) from error
        model_identity = _mapping(
            model.get("prototype_identity"), "model prototype identity"
        )
        prototype = prototype_by_id[prototype_id]
        expected_identity = {
            key: deepcopy(prototype.get(key))
            for key in (
                "prototype_id",
                "prototype_family",
                "selection_rule",
                "member_count",
                "members",
            )
        }
        model_address = _mapping(model.get("content_address"), "model address")
        if (
            dict(model_identity) != expected_identity
            or descriptor.get("schema") != clone_v2.MODEL_SCHEMA
            or descriptor.get("content_sha256") != model_address.get("sha256")
            or descriptor.get("content_sha256") != receipt.get("model_sha256")
        ):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                f"model descriptor identity differs for {prototype_id}"
            )
        source_binding = _mapping(model.get("source_binding"), "model source binding")
        model_prototype_manifest = _mapping(
            source_binding.get("prototype_manifest"),
            "model source prototype manifest",
        )
        for key in (
            "schema",
            "implementation_revision",
            "file_sha256",
            "content_sha256",
        ):
            if model_prototype_manifest.get(key) != source_manifest.get(key):
                raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                    f"model {prototype_id} prototype source binding differs"
                )
        source_partition = _mapping(
            source_binding.get("prototype_partition"),
            "model source prototype partition",
        )
        prototype_partition = _mapping(
            prototype.get("partition"), "historical prototype partition"
        )
        expected_partition = {
            "record_schema": prototype_partition.get("record_schema"),
            "implementation_revision": prototypes_v1.IMPLEMENTATION_REVISION,
            "record_count": prototype_partition.get("record_count"),
            "logical_size_bytes": prototype_partition.get("logical_size_bytes"),
            "logical_content_sha256": prototype_partition.get(
                "logical_content_sha256"
            ),
        }
        if dict(source_partition) != expected_partition:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                f"model {prototype_id} prototype partition binding differs"
            )
        result[prototype_id] = {
            "prototype_id": prototype_id,
            "prototype_family": model_identity.get("prototype_family"),
            "policy_id": policy_id_for_prototype_v2(prototype_id),
            "model_schema": clone_v2.MODEL_SCHEMA,
            "model_implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
            "model_content_sha256": receipt["model_sha256"],
            "model_file_sha256": descriptor["file_sha256"],
            "model_file_size_bytes": descriptor["file_size_bytes"],
            "exact_validation_receipt": receipt,
            "prototype_source_binding": {
                key: deepcopy(model_prototype_manifest.get(key))
                for key in (
                    "schema",
                    "implementation_revision",
                    "file_sha256",
                    "content_sha256",
                )
            },
            "comparison_authorized": False,
        }
    return result


def _load_source_material(
    *,
    gap_manifest_path: str | Path,
    join_manifest_path: str | Path,
    catalog_manifest_path: str | Path,
    catalog_path: str | Path | None,
    prototype_manifest_path: str | Path,
    model_manifest_path: str | Path,
) -> tuple[_SourcePaths, _SourceMaterial]:
    gap_path = _resolve_file(gap_manifest_path, "gap manifest")
    join_path = _resolve_file(join_manifest_path, "join manifest")
    catalog_manifest_path = _resolve_file(
        catalog_manifest_path, "catalog manifest"
    )
    prototype_manifest_path = _resolve_file(
        prototype_manifest_path, "prototype manifest"
    )
    model_manifest_path = _resolve_file(model_manifest_path, "model manifest")

    gap, _ = _load_json(gap_path, "gap manifest")
    try:
        gap = gap_v1.validate_historical_fury_prototype_build_gap_priority_v1(gap)
    except gap_v1.HistoricalFuryPrototypeBuildGapPriorityV1Error as error:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(str(error)) from error
    gap_sha = _text(
        _mapping(gap.get("content_address"), "gap content address").get("sha256"),
        "gap sha256",
    )

    join, _ = _load_json(join_path, "join manifest")
    try:
        join_sha = gap_v1._validate_join_manifest(join)
        dictionary, dictionary_binding = gap_v1._read_segment_dictionary(
            join, join_path.parent
        )
    except gap_v1.HistoricalFuryPrototypeBuildGapPriorityV1Error as error:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(str(error)) from error
    gap_join = _mapping(
        _mapping(gap.get("input_closure"), "gap input closure").get(
            "decision_build_join"
        ),
        "gap join binding",
    )
    if gap_join.get("content_sha256") != join_sha:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "gap does not bind the supplied decision/build join"
        )

    summary = _mapping(gap.get("summary"), "gap summary")
    if (
        len(dictionary)
        != _integer(
            summary.get("prototype_union_distinct_segment_count"),
            "prototype union segment count",
        )
        or sum(int(row["_union_support"]) for row in dictionary.values())
        != _integer(
            summary.get("prototype_union_weighted_decision_support"),
            "prototype union decision support",
        )
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "gap and join prototype-union accounting differ"
        )
    runtime_rows = [
        row
        for row in dictionary.values()
        if _mapping(row.get("coverage_flags"), "dictionary coverage flags").get(
            "runtime_executable"
        )
        is True
    ]
    selected_dictionary_rows = [
        row
        for row in runtime_rows
        if _mapping(row.get("coverage_flags"), "dictionary coverage flags").get(
            "development_build_eligible"
        )
        is True
    ]
    ceiling = _mapping(
        _mapping(gap.get("coverage_ceiling"), "coverage ceiling").get(
            "currently_runtime_executable"
        ),
        "currently runtime executable",
    )
    if (
        len(runtime_rows)
        != _integer(ceiling.get("distinct_segment_count"), "runtime segment count")
        or sum(int(row["_union_support"]) for row in runtime_rows)
        != _integer(ceiling.get("weighted_decision_support"), "runtime support")
        or sum(
            1
            for row in dictionary.values()
            if _mapping(row.get("coverage_flags"), "coverage flags").get(
                "development_build_eligible"
            )
            is True
        )
        != _integer(
            summary.get("development_build_eligible_segment_count"),
            "development eligible segment count",
        )
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "gap runtime/development ceiling differs from the exact join dictionary"
        )

    catalog_manifest, _ = _load_json(catalog_manifest_path, "catalog manifest")
    if (
        catalog_manifest.get("schema") != catalog_v1.SCHEMA
        or catalog_manifest.get("implementation_revision")
        != catalog_v1.IMPLEMENTATION_REVISION
        or catalog_manifest.get("kind") != "historical_build_catalog_manifest"
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "historical build catalog manifest identity differs"
        )
    join_catalog = _mapping(
        _mapping(join.get("input_closure"), "join input closure").get(
            "historical_build_catalog"
        ),
        "join catalog binding",
    )
    portable_catalog_contract_sha = hashlib.sha256(
        _canonical_bytes(_catalog_portable_contract(catalog_manifest))
    ).hexdigest()
    if (
        join_catalog.get("schema") != catalog_v1.SCHEMA
        or join_catalog.get("implementation_revision")
        != catalog_v1.IMPLEMENTATION_REVISION
        or join_catalog.get("portable_manifest_contract_sha256")
        != portable_catalog_contract_sha
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "join does not bind the supplied portable catalog contract"
        )
    data_path = (
        _resolve_file(catalog_path, "catalog data")
        if catalog_path is not None
        else _resolve_locator(
            catalog_manifest_path.parent,
            catalog_manifest.get("catalog_path"),
            "catalog data",
        )
    )
    selected = _load_selected_catalog_rows(
        catalog_path=data_path,
        selected_dictionary_rows=selected_dictionary_rows,
    )

    relevant_prototypes = {
        str(prototype_id)
        for dictionary_row, _ in selected
        for prototype_id in _mapping(
            _mapping(dictionary_row.get("decision_support"), "decision support").get(
                "by_prototype"
            ),
            "by prototype support",
        )
    }
    _, prototype_binding, prototype_by_id = _prototype_manifest_binding(
        path=prototype_manifest_path, join_manifest=join
    )
    model_manifest, model_manifest_sha, model_descriptors = _model_manifest_index(
        model_manifest_path
    )
    model_prototype_source = _mapping(
        _mapping(model_manifest.get("input_closure"), "model input closure").get(
            "prototype_manifest"
        ),
        "model prototype manifest binding",
    )
    for key in (
        "schema",
        "implementation_revision",
        "content_sha256",
        "file_sha256",
    ):
        if model_prototype_source.get(key) != prototype_binding.get(key):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "clone-v2 model manifest does not bind the supplied prototype bytes"
            )
    model_by_prototype = _load_relevant_models(
        manifest_path=model_manifest_path,
        manifest=model_manifest,
        descriptors=model_descriptors,
        prototype_by_id=prototype_by_id,
        relevant_prototypes=relevant_prototypes,
    )
    expected_prototypes = {
        _text(value, "gap prototype id")
        for value in _array(summary.get("prototype_ids"), "gap prototype ids")
    }
    if not relevant_prototypes <= expected_prototypes:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "selected source segment names a prototype outside the gap artifact"
        )
    if expected_prototypes != set(prototype_binding["prototype_ids"]):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "gap and historical prototype manifest identities differ"
        )

    selected_refs = [str(row[0]["segment_ref"]) for row in selected]
    selected_support = sum(int(row[0]["_union_support"]) for row in selected)
    closure: JSONMap = {
        "gap_priority": {
            "schema": gap_v1.SCHEMA,
            "implementation_revision": gap_v1.IMPLEMENTATION_REVISION,
            "content_sha256": gap_sha,
        },
        "decision_build_join": {
            "schema": join_v1.MANIFEST_SCHEMA,
            "implementation_revision": join_v1.IMPLEMENTATION_REVISION,
            "content_sha256": join_sha,
        },
        "segment_dictionary": deepcopy(dictionary_binding),
        "historical_build_catalog": {
            "schema": catalog_v1.SCHEMA,
            "implementation_revision": catalog_v1.IMPLEMENTATION_REVISION,
            "portable_manifest_contract_sha256": portable_catalog_contract_sha,
            "selected_rows_reopened_by_segment_ref": True,
        },
        "historical_behavior_prototypes": prototype_binding,
        "behavior_clone_model_manifest": {
            "schema": clone_v2.MANIFEST_SCHEMA,
            "implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
            "content_sha256": model_manifest_sha,
            "file_sha256": _sha256_file(model_manifest_path),
        },
        "selected_source_segments": {
            "selection_rule": SOURCE_SELECTION,
            "segment_count": len(selected),
            "weighted_decision_support": selected_support,
            "segment_ref_set_sha256": hashlib.sha256(
                _canonical_bytes(selected_refs)
            ).hexdigest(),
            "prototype_ids": sorted(relevant_prototypes),
        },
        "network_request_count": 0,
        "simulator_run_count": 0,
    }
    paths = _SourcePaths(
        gap_manifest=gap_path,
        join_manifest=join_path,
        catalog_manifest=catalog_manifest_path,
        catalog_data=data_path,
        prototype_manifest=prototype_manifest_path,
        model_manifest=model_manifest_path,
    )
    return paths, _SourceMaterial(
        input_closure=closure,
        selected=selected,
        model_by_prototype=model_by_prototype,
        gap_sha256=gap_sha,
        model_manifest_sha256=model_manifest_sha,
    )


def _shared_components(first_seed: int):
    raid, encounter, execution, objective = bundle_v1._shared_components(first_seed)
    base = {
        "schema": SCHEMA,
        "scope": "EXACT_HISTORICAL_SOURCE_BUILD_DEVELOPMENT_ONLY",
        "comparison_eligible": False,
        "current_character_profile_consumed": False,
    }
    return (
        replace(raid, provenance={**base, "component": "RaidContext"}),
        replace(encounter, provenance={**base, "component": "EncounterModel"}),
        replace(execution, provenance={**base, "component": "ExecutionModel"}),
        replace(
            objective,
            kind="SOURCE_BOUND_HISTORICAL_PROTOTYPE_DEVELOPMENT",
            provenance={
                **base,
                "component": "Objective",
                "seed_role": "FIRST_DECLARED_SEED_REQUEST_TEMPLATE",
            },
        ),
    )


def _request_for_segment(
    *,
    dictionary_row: Mapping[str, Any],
    segment: Mapping[str, Any],
    catalog_path: Path,
    first_seed: int,
    model_by_prototype: Mapping[str, JSONMap],
) -> JSONMap:
    line_number = _integer(
        dictionary_row.get("catalog_line_number"), "catalog_line_number", minimum=1
    )
    try:
        profile = catalog_v1.historical_segment_to_character_profile(
            segment,
            catalog_path=catalog_path,
            catalog_line_number=line_number,
            consumes={},
            database={},
        )
        raid, encounter, execution, objective = _shared_components(first_seed)
        composition = compose_build_request_v1(
            profile, raid, encounter, execution, objective
        )
    except (catalog_v1.HistoricalBuildCatalogError, BuildRequestComposerV1Error) as error:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"cannot compose exact source segment {dictionary_row.get('segment_ref')}: {error}"
        ) from error
    if not composition.admitted or composition.request is None:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            f"exact source segment {dictionary_row.get('segment_ref')} was not admitted"
        )
    request = composition.request
    player = _mapping(
        _array(
            _mapping(
                _array(_mapping(request.get("raid"), "request raid").get("parties"), "parties")[0],
                "first party",
            ).get("players"),
            "players",
        )[0],
        "historical player request",
    )
    source_player = _mapping(segment.get("player"), "historical player")
    if (
        player.get("name") != source_player.get("name")
        or player.get("class") != "ClassWarrior"
        or player.get("consumes") != {}
        or player.get("database") != {}
        or profile.selection.record.get("event")
        != "HISTORICAL_BUILD_SEGMENT_OBSERVED"
        or profile.selection.selection_mode != "historical_build_segment_prefix"
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "composed request retained non-historical character residue"
        )
    support = _mapping(dictionary_row.get("decision_support"), "decision support")
    by_prototype = _mapping(support.get("by_prototype"), "by prototype support")
    policy_bindings = []
    for prototype_id in sorted(by_prototype):
        model = deepcopy(model_by_prototype[_text(prototype_id, "prototype id")])
        model["segment_weighted_decision_support"] = _integer(
            by_prototype[prototype_id], "prototype segment support", minimum=1
        )
        model["binding_scope"] = (
            "PROTOTYPE_MODEL_ROUTED_THROUGH_AN_OBSERVED_MEMBER_SOURCE_BUILD"
        )
        model["model_is_build_conditioned"] = False
        policy_bindings.append(model)
    if not policy_bindings:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "selected prototype-union segment has no prototype model binding"
        )
    components = bundle_v1._component_payload(
        profile, raid, encounter, execution, objective
    )
    identity = _mapping(segment.get("identity"), "historical identity")
    observation = _mapping(segment.get("observation"), "historical observation")
    return {
        "source_route": "EXACT_SOURCE_BUILD_SEGMENT",
        "segment_ref": dictionary_row["segment_ref"],
        "catalog_line_number": line_number,
        "causal_source_identity": {
            "identity": deepcopy(dict(identity)),
            "valid_from": deepcopy(
                dict(_mapping(observation.get("valid_from"), "valid_from"))
            ),
            "portable_source_segment_content_sha256": dictionary_row[
                "portable_source_segment_content_sha256"
            ],
        },
        "decision_support": {
            "prototype_member_union": _integer(
                support.get("prototype_member_union"), "prototype union support", minimum=1
            ),
            "by_prototype": {
                str(key): _integer(value, "prototype support", minimum=1)
                for key, value in by_prototype.items()
            },
        },
        "historical_policy_bindings": policy_bindings,
        "five_part_components": _portable_projection(components),
        "composition": _portable_projection(composition.as_dict()),
        "request_sha256": hashlib.sha256(_canonical_bytes(request)).hexdigest(),
        "runtime_executable": True,
        "development_build_eligible": True,
        "comparison_eligible": False,
        "current_character_profile_consumed": False,
        "representative_selector_consumed": False,
        "runtime_snapshot_consumed": False,
    }


def _build_document(
    *,
    gap_manifest_path: str | Path,
    join_manifest_path: str | Path,
    catalog_manifest_path: str | Path,
    catalog_path: str | Path | None,
    prototype_manifest_path: str | Path,
    model_manifest_path: str | Path,
) -> JSONMap:
    paths, source = _load_source_material(
        gap_manifest_path=gap_manifest_path,
        join_manifest_path=join_manifest_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_path=catalog_path,
        prototype_manifest_path=prototype_manifest_path,
        model_manifest_path=model_manifest_path,
    )
    seed_namespace = (
        "brainofcat.fury.source-bound-prototype.v1."
        + source.gap_sha256
        + "."
        + source.model_manifest_sha256
    )
    seeds = derive_seed_set(
        seed_namespace, DEVELOPMENT_SEED_PHASE, DEVELOPMENT_SEED_COUNT
    )
    requests = [
        _request_for_segment(
            dictionary_row=dictionary_row,
            segment=segment,
            catalog_path=paths.catalog_data,
            first_seed=seeds[0],
            model_by_prototype=source.model_by_prototype,
        )
        for dictionary_row, segment in source.selected
    ]
    policy_cell_count = sum(
        len(request["historical_policy_bindings"]) for request in requests
    )
    if not requests:
        core: JSONMap = {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": KIND,
            "status": "BLOCKED",
            "execution_status": "NOT_RUN",
            "comparison_authorized": False,
            "deployment_authorized": False,
            "scientific_runs_started": False,
            "training_authorized": False,
            "superiority_claim_authorized": False,
            "blockers": [
                {
                    "code": "NO_RUNTIME_EXECUTABLE_SOURCE_BOUND_SEGMENTS",
                    "detail": (
                        "the prototype union contains no exact catalog segment that "
                        "is both runtime-executable and development-build-eligible"
                    ),
                }
            ],
            "input_closure": source.input_closure,
            "source_selection": SOURCE_SELECTION,
            "requests": [],
            "request_contract": {
                "request_count": 0,
                "policy_binding_count": 0,
                "requested_seed_policy_cell_count": 0,
            },
        }
        return _content_addressed(core)

    selected_support = sum(
        int(request["decision_support"]["prototype_member_union"])
        for request in requests
    )
    core = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "status": "PREPARED",
        "execution_status": "NOT_RUN",
        "comparison_authorized": False,
        "deployment_authorized": False,
        "scientific_runs_started": False,
        "training_authorized": False,
        "superiority_claim_authorized": False,
        "blockers": [],
        "claim_boundary": (
            "exact source-build composition and exact prototype-model binding are "
            "development preparation only; no simulator result or matched comparison exists"
        ),
        "input_closure": source.input_closure,
        "source_selection": SOURCE_SELECTION,
        "development_seeds": {
            "algorithm": "sha256_namespace_phase_counter_u63_v1",
            "namespace": seed_namespace,
            "phase": DEVELOPMENT_SEED_PHASE,
            "count": len(seeds),
            "master_seeds": list(seeds),
            "seed_list_sha256": hashlib.sha256(
                _canonical_bytes(list(seeds))
            ).hexdigest(),
            "development_only": True,
            "fixed_sample_no_optional_stopping": True,
        },
        "request_contract": {
            "source_segment_count": len(requests),
            "request_count": len(requests),
            "policy_binding_count": policy_cell_count,
            "seed_count": len(seeds),
            "requested_seed_policy_cell_count": policy_cell_count * len(seeds),
            "weighted_historical_decision_support": selected_support,
            "one_exact_historical_character_profile_per_request": True,
            "prototype_models_routed_only_through_supported_source_segments": True,
            "prototype_models_are_build_conditioned": False,
            "current_representative_transplant_count": 0,
            "current_character_profile_input_count": 0,
            "runtime_snapshot_input_count": 0,
            "five_component_composition_required": True,
            "comparison_receipt_count": 0,
        },
        "requests": requests,
    }
    artifact = _content_addressed(core)
    _assert_path_free(artifact)
    return artifact


def build_historical_fury_source_bound_prototype_bundle_v1(
    *,
    gap_manifest_path: str | Path = DEFAULT_GAP_MANIFEST,
    join_manifest_path: str | Path = DEFAULT_JOIN_MANIFEST,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_path: str | Path | None = None,
    prototype_manifest_path: str | Path = DEFAULT_PROTOTYPE_MANIFEST,
    model_manifest_path: str | Path = DEFAULT_MODEL_MANIFEST,
) -> JSONMap:
    """Build but do not execute the source-bound development bundle."""

    artifact = _build_document(
        gap_manifest_path=gap_manifest_path,
        join_manifest_path=join_manifest_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_path=catalog_path,
        prototype_manifest_path=prototype_manifest_path,
        model_manifest_path=model_manifest_path,
    )
    validate_historical_fury_source_bound_prototype_bundle_v1(
        artifact, verify_source_bytes=False
    )
    return artifact


def validate_historical_fury_source_bound_prototype_bundle_v1(
    value: Mapping[str, Any],
    *,
    verify_source_bytes: bool = True,
    gap_manifest_path: str | Path = DEFAULT_GAP_MANIFEST,
    join_manifest_path: str | Path = DEFAULT_JOIN_MANIFEST,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_path: str | Path | None = None,
    prototype_manifest_path: str | Path = DEFAULT_PROTOTYPE_MANIFEST,
    model_manifest_path: str | Path = DEFAULT_MODEL_MANIFEST,
) -> JSONMap:
    """Validate the artifact, optionally rebuilding it from relocated inputs."""

    raw = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != KIND
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "source-bound bundle implementation identity differs"
        )
    address = _mapping(raw.get("content_address"), "bundle content address")
    core = deepcopy(raw)
    core.pop("content_address", None)
    if address != {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address; host locators forbidden",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "source-bound bundle content address differs"
        )
    _assert_path_free(raw)
    if (
        raw.get("execution_status") != "NOT_RUN"
        or raw.get("comparison_authorized") is not False
        or raw.get("deployment_authorized") is not False
        or raw.get("scientific_runs_started") is not False
        or raw.get("training_authorized") is not False
        or raw.get("superiority_claim_authorized") is not False
    ):
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "source-bound bundle exceeded its preparation boundary"
        )
    requests = _array(raw.get("requests"), "requests")
    contract = _mapping(raw.get("request_contract"), "request contract")
    if raw.get("status") == "BLOCKED":
        blockers = _array(raw.get("blockers"), "blockers")
        if (
            requests
            or len(blockers) != 1
            or _mapping(blockers[0], "blocker").get("code")
            != "NO_RUNTIME_EXECUTABLE_SOURCE_BOUND_SEGMENTS"
            or contract.get("request_count") != 0
        ):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "zero-source blocked bundle contract differs"
            )
    elif raw.get("status") == "PREPARED":
        if raw.get("blockers") != [] or not requests:
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "prepared source-bound bundle request/blocker closure differs"
            )
        if (
            contract.get("request_count") != len(requests)
            or contract.get("source_segment_count") != len(requests)
            or contract.get("current_representative_transplant_count") != 0
            or contract.get("current_character_profile_input_count") != 0
            or contract.get("runtime_snapshot_input_count") != 0
            or contract.get("comparison_receipt_count") != 0
            or contract.get("prototype_models_are_build_conditioned") is not False
        ):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "prepared source-bound request accounting differs"
            )
        seen: set[str] = set()
        policy_binding_count = 0
        for request in requests:
            row = _mapping(request, "source-bound request")
            segment_ref = _text(row.get("segment_ref"), "request segment_ref")
            if (
                segment_ref in seen
                or row.get("source_route") != "EXACT_SOURCE_BUILD_SEGMENT"
                or row.get("runtime_executable") is not True
                or row.get("development_build_eligible") is not True
                or row.get("comparison_eligible") is not False
                or row.get("current_character_profile_consumed") is not False
                or row.get("representative_selector_consumed") is not False
                or row.get("runtime_snapshot_consumed") is not False
            ):
                raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                    "request source route or residue boundary differs"
                )
            seen.add(segment_ref)
            composition = _mapping(row.get("composition"), "composition")
            request_payload = _mapping(composition.get("request"), "sim request")
            if hashlib.sha256(_canonical_bytes(request_payload)).hexdigest() != row.get(
                "request_sha256"
            ):
                raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                    "composed simulator request digest differs"
                )
            bindings = _array(
                row.get("historical_policy_bindings"), "historical policy bindings"
            )
            if not bindings:
                raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                    "source segment lacks a historical prototype model"
                )
            policy_binding_count += len(bindings)
            by_prototype = _mapping(
                _mapping(row.get("decision_support"), "decision support").get(
                    "by_prototype"
                ),
                "by prototype",
            )
            if {binding["prototype_id"] for binding in bindings} != set(by_prototype):
                raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                    "request prototype support/model identities differ"
                )
        seeds = _array(
            _mapping(raw.get("development_seeds"), "development seeds").get(
                "master_seeds"
            ),
            "master seeds",
        )
        if (
            contract.get("policy_binding_count") != policy_binding_count
            or contract.get("requested_seed_policy_cell_count")
            != policy_binding_count * len(seeds)
        ):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "seed/policy cell accounting differs"
            )
    else:
        raise HistoricalFurySourceBoundPrototypeBundleV1Error(
            "source-bound bundle status must be PREPARED or BLOCKED"
        )

    if verify_source_bytes:
        expected = _build_document(
            gap_manifest_path=gap_manifest_path,
            join_manifest_path=join_manifest_path,
            catalog_manifest_path=catalog_manifest_path,
            catalog_path=catalog_path,
            prototype_manifest_path=prototype_manifest_path,
            model_manifest_path=model_manifest_path,
        )
        if _canonical_bytes(expected) != _canonical_bytes(raw):
            raise HistoricalFurySourceBoundPrototypeBundleV1Error(
                "bundle differs from the supplied source bytes"
            )
    return raw


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


def publish_historical_fury_source_bound_prototype_bundle_v1(
    *,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    gap_manifest_path: str | Path = DEFAULT_GAP_MANIFEST,
    join_manifest_path: str | Path = DEFAULT_JOIN_MANIFEST,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_path: str | Path | None = None,
    prototype_manifest_path: str | Path = DEFAULT_PROTOTYPE_MANIFEST,
    model_manifest_path: str | Path = DEFAULT_MODEL_MANIFEST,
) -> JSONMap:
    artifact = build_historical_fury_source_bound_prototype_bundle_v1(
        gap_manifest_path=gap_manifest_path,
        join_manifest_path=join_manifest_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_path=catalog_path,
        prototype_manifest_path=prototype_manifest_path,
        model_manifest_path=model_manifest_path,
    )
    validate_historical_fury_source_bound_prototype_bundle_v1(
        artifact,
        verify_source_bytes=True,
        gap_manifest_path=gap_manifest_path,
        join_manifest_path=join_manifest_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_path=catalog_path,
        prototype_manifest_path=prototype_manifest_path,
        model_manifest_path=model_manifest_path,
    )
    destination = Path(output_directory).expanduser().resolve()
    content_sha = artifact["content_address"]["sha256"]
    payload = _canonical_bytes(artifact, newline=True)
    stable = destination / "manifest.json"
    addressed = destination / (
        f"historical_fury_source_bound_prototype_bundle_v1.{content_sha}.manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return {
        "schema": SCHEMA,
        "status": artifact["status"],
        "execution_status": "NOT_RUN",
        "manifest": str(stable),
        "content_addressed_manifest": str(addressed),
        "content_sha256": content_sha,
        "request_count": len(artifact["requests"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare exact historical Fury prototype/build requests"
    )
    parser.add_argument("--gap-manifest", default=str(DEFAULT_GAP_MANIFEST))
    parser.add_argument("--join-manifest", default=str(DEFAULT_JOIN_MANIFEST))
    parser.add_argument("--catalog-manifest", default=str(DEFAULT_CATALOG_MANIFEST))
    parser.add_argument("--catalog")
    parser.add_argument(
        "--prototype-manifest", default=str(DEFAULT_PROTOTYPE_MANIFEST)
    )
    parser.add_argument("--model-manifest", default=str(DEFAULT_MODEL_MANIFEST))
    parser.add_argument("--output-directory", default=str(DEFAULT_OUTPUT_DIRECTORY))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = publish_historical_fury_source_bound_prototype_bundle_v1(
        output_directory=args.output_directory,
        gap_manifest_path=args.gap_manifest,
        join_manifest_path=args.join_manifest,
        catalog_manifest_path=args.catalog_manifest,
        catalog_path=args.catalog,
        prototype_manifest_path=args.prototype_manifest,
        model_manifest_path=args.model_manifest,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTENT_ADDRESS_SCHEMA",
    "DEFAULT_CATALOG_MANIFEST",
    "DEFAULT_GAP_MANIFEST",
    "DEFAULT_JOIN_MANIFEST",
    "DEFAULT_MODEL_MANIFEST",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_PROTOTYPE_MANIFEST",
    "HistoricalFurySourceBoundPrototypeBundleV1Error",
    "IMPLEMENTATION_REVISION",
    "KIND",
    "SCHEMA",
    "SOURCE_SELECTION",
    "build_historical_fury_source_bound_prototype_bundle_v1",
    "main",
    "publish_historical_fury_source_bound_prototype_bundle_v1",
    "validate_historical_fury_source_bound_prototype_bundle_v1",
]
