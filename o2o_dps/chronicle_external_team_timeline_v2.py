"""Per-player team timelines for verified Chronicle External API waves.

This adapter is deliberately separate from the manual-CSV team timeline and
team model.  It binds three immutable inputs -- the External core-event
normalization, reconstruction admission, and External encounter reconstruction
V2 -- then streams the admitted rows in official EventMeta order.  Events are
joined to the exact reconstruction wave windows one encounter at a time.

The result is descriptive evidence only.  DMG is the sole damage amount;
DEAD is a marker only.  Player attribution requires an event-time official
FriendlyPlayer state, either directly or at the exact end of an official
controller/owner chain.  Names and GUID suffixes are never used for identity.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from .chronicle_external_api_ingest_v1 import (
    NO_KNOWN_RULE_MATCH,
    POSTFIX_KNOWN_CLEAN,
    RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
    RANGE_BUG_CUTOFF_LOCAL,
    RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL,
    RANGE_BUG_SUSPECT_BEFORE_LOCAL,
    SUSPECT_36YD_RANGE_BUG,
    UNKNOWN_NONVOTING,
    range_bug_boundary_contract,
)
from . import chronicle_external_encounter_reconstruction_v2 as reconstruction_v2
from .chronicle_external_event_normalizer_v1 import (
    IMPLEMENTATION_REVISION as NORMALIZATION_IMPLEMENTATION_REVISION,
    SCHEMA as NORMALIZATION_SCHEMA,
    STREAM_ORDER,
)
from .chronicle_external_reconstruction_admission_v1 import (
    ChronicleExternalAdmissionError,
    IMPLEMENTATION_REVISION as ADMISSION_IMPLEMENTATION_REVISION,
    SCHEMA as ADMISSION_SCHEMA,
    STATUS as ADMISSION_STATUS,
    canonical_guid,
    load_admission_manifest,
)
from .chronicle_unit_classification_semantics_v1 import (
    OFFICIAL_COMMIT,
    OFFICIAL_EVIDENCE_SHA256,
    ROW_FIELD as CLASSIFICATION_ROW_FIELD,
    iter_resolved_reconstruction_rows,
)


SCHEMA = "chronicle_external_team_timeline/v2"
PARTITION_RECORD_SCHEMA = "chronicle_external_team_timeline_wave/v2"
KIND = "chronicle_external_team_timeline_manifest"
STATUS = "NOT_TEAM_MODEL_NOT_COMPARISON"
IMPLEMENTATION_REVISION = (
    "v2.3_external_exact_event_time_range_bug_boundary_20260903_noon_parallel"
)
MAX_WORKERS = 32

NEGATIVE_DAMAGE_DIAGNOSTIC_LANE = (
    "SIGNED_NEGATIVE_DMG_NONVOTING_DIAGNOSTIC"
)
NEGATIVE_DAMAGE_VALUE_POLICY = (
    "PRESERVED_DIAGNOSTIC_NONVOTING_NO_ABS_OR_CLAMP"
)
CONTAMINATION_CUTOFF = RANGE_BUG_CUTOFF_LOCAL
CONTAMINATION_SUSPECT_BEFORE = RANGE_BUG_SUSPECT_BEFORE_LOCAL
CONTAMINATION_POSTFIX_AT_OR_AFTER = RANGE_BUG_POSTFIX_AT_OR_AFTER_LOCAL
CONTAMINATION_LABELS = frozenset(
    {
        SUSPECT_36YD_RANGE_BUG,
        RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING,
        POSTFIX_KNOWN_CLEAN,
        NO_KNOWN_RULE_MATCH,
        UNKNOWN_NONVOTING,
    }
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_NORMALIZATION_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_external_core_events"
    / "v1"
    / "manifest.json"
)
DEFAULT_ADMISSION_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_external_reconstruction_admission"
    / "v1"
    / "manifest.json"
)
DEFAULT_RECONSTRUCTION_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_external_encounter_reconstruction"
    / "v2"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT / "derived" / "chronicle_external_team_timeline" / "v2"
)

TIMELINE_EVENT_TYPES = frozenset({"START", "GO", "FAIL", "DMG", "HEAL", "DEAD"})
ACTION_EVENT_TYPES = frozenset({"START", "GO", "FAIL"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ChronicleExternalTeamTimelineV2Error(RuntimeError):
    """An input or output violates the External team-timeline V2 contract."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalTeamTimelineV2Error(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalTeamTimelineV2Error(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} must be nonempty text"
        )
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalTeamTimelineV2Error(f"{label} must be an integer")
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    result = _integer(value, label=label)
    if result < 0:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} must be nonnegative"
        )
    return result


def _sha(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if _SHA256_RE.fullmatch(rendered) is None:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} must be a lowercase SHA-256"
        )
    return rendered


def _safe_component(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if _SAFE_COMPONENT_RE.fullmatch(rendered) is None:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} is unsafe for an artifact filename"
        )
    return rendered


def _content_addressed(value: Mapping[str, Any]) -> dict[str, Any]:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": _sha256_bytes(_canonical_bytes(core)),
        },
    }


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = _mapping(value.get("content_address"), label=f"{label}.content_address")
    if address.get("algorithm") != "sha256":
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} uses an unsupported content-address algorithm"
        )
    declared = _sha(address.get("sha256"), label=f"{label}.content_address.sha256")
    core = {key: item for key, item in value.items() if key != "content_address"}
    if _sha256_bytes(_canonical_bytes(core)) != declared:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} content-address mismatch"
        )
    return declared


def _data_root_for(path: Path) -> Path:
    for candidate in (path.parent, *path.parents):
        if candidate.name.casefold() == "offline_data":
            return candidate.resolve()
    raise ChronicleExternalTeamTimelineV2Error(
        f"input is not stored beneath offline_data: {path}"
    )


def _under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} must remain beneath the common offline_data root"
        ) from error
    return resolved


def _relative(path: Path, root: Path, *, label: str) -> str:
    return _under(path, root, label=label).relative_to(root.resolve()).as_posix()


def _load_json_bytes(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"cannot load {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleExternalTeamTimelineV2Error(f"{label} is not an object")
    return value, payload


def _stable_and_addressed(
    path: Path,
    *,
    addressed_name: str,
    label: str,
) -> tuple[Path, Path, bytes]:
    stable = path.with_name("manifest.json")
    addressed = path.with_name(addressed_name)
    if path.name not in {stable.name, addressed.name}:
        raise ChronicleExternalTeamTimelineV2Error(
            f"{label} filename/content address mismatch"
        )
    try:
        payload = path.read_bytes()
        if stable.read_bytes() != payload or addressed.read_bytes() != payload:
            raise ChronicleExternalTeamTimelineV2Error(
                f"stable and addressed {label} manifests differ"
            )
    except OSError as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"stable or addressed {label} manifest is absent: {error}"
        ) from error
    return stable, addressed, payload


def _load_normalization_manifest(
    path: str | Path,
) -> tuple[dict[str, Any], Path, Path, bytes]:
    resolved = Path(path).expanduser().resolve()
    document, _ = _load_json_bytes(resolved, label="normalization manifest")
    if (
        document.get("schema") != NORMALIZATION_SCHEMA
        or document.get("implementation_revision")
        != NORMALIZATION_IMPLEMENTATION_REVISION
        or document.get("kind")
        != "chronicle_external_core_event_normalization_manifest"
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "unsupported External normalization manifest"
        )
    content_sha = _verify_content_address(document, label="normalization manifest")
    stable, addressed, payload = _stable_and_addressed(
        resolved,
        addressed_name=(
            f"chronicle_external_core_events_v1.{content_sha}.manifest.json"
        ),
        label="normalization",
    )
    return document, stable, addressed, payload


@dataclass(frozen=True)
class InputContext:
    data_root: Path
    instance_id: str
    normalization: Mapping[str, Any]
    normalization_stable: Path
    normalization_addressed: Path
    normalization_payload: bytes
    admission: Mapping[str, Any]
    admission_stable: Path
    admission_addressed: Path
    admission_payload: bytes
    admission_instance: Mapping[str, Any]
    reconstruction_document: Mapping[str, Any]
    reconstruction_instance: Mapping[str, Any]
    reconstruction_stable: Path
    reconstruction_addressed: Path
    reconstruction_payload: bytes
    reconstruction_source_context: Any
    warrior_spec_by_guid: Mapping[str, Mapping[str, Any]]
    source_binding: Mapping[str, Any]


@dataclass(frozen=True)
class PartitionInputContext:
    """Small, spawn-safe per-instance view used by process workers.

    ``InputContext`` retains the complete validated manifest closure for the
    coordinator.  Sending it to every Windows worker would redundantly pickle
    the same multi-megabyte documents.  Partition construction needs only the
    instance-local fields below.
    """

    instance_id: str
    admission_stable: Path
    admission_instance: Mapping[str, Any]
    reconstruction_instance: Mapping[str, Any]
    reconstruction_stable: Path
    reconstruction_source_context: Any
    warrior_spec_by_guid: Mapping[str, Mapping[str, Any]]
    source_binding: Mapping[str, Any]


def _partition_input_context(context: InputContext) -> PartitionInputContext:
    return PartitionInputContext(
        instance_id=context.instance_id,
        admission_stable=context.admission_stable,
        admission_instance=context.admission_instance,
        reconstruction_instance=context.reconstruction_instance,
        reconstruction_stable=context.reconstruction_stable,
        reconstruction_source_context=context.reconstruction_source_context,
        warrior_spec_by_guid=context.warrior_spec_by_guid,
        source_binding=context.source_binding,
    )


def _index_by_instance(
    values: Sequence[Any], *, label: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(values):
        item = _mapping(raw, label=f"{label}[{index}]")
        instance_id = _text(item.get("instance_id"), label=f"{label}.instance_id")
        if instance_id in result:
            raise ChronicleExternalTeamTimelineV2Error(
                f"duplicate instance_id in {label}: {instance_id}"
            )
        result[instance_id] = item
    return result


def _reconstruction_instances(
    document: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    raw_instances = document.get("instances")
    if isinstance(raw_instances, list):
        indexed = _index_by_instance(
            raw_instances, label="reconstruction.instances"
        )
        common = _mapping(
            document.get("source_binding"),
            label="reconstruction.source_binding",
        )
        common_keys = (
            "raw_api_manifest",
            "normalized_manifest",
            "admission_manifest",
            "classification_resolver",
        )
        result: dict[str, Mapping[str, Any]] = {}
        for instance_id, raw in indexed.items():
            local = _mapping(
                raw.get("source_binding"),
                label="reconstruction instance source_binding",
            )
            view = deepcopy(dict(raw))
            view["source_binding"] = {
                **{
                    key: deepcopy(
                        dict(_mapping(common.get(key), label=f"common {key}"))
                    )
                    for key in common_keys
                },
                "normalized_partition": deepcopy(
                    dict(
                        _mapping(
                            local.get("normalized_partition"),
                            label="local normalized_partition",
                        )
                    )
                ),
                "metadata_player_resolver": deepcopy(
                    dict(
                        _mapping(
                            local.get("metadata_player_resolver"),
                            label="local metadata_player_resolver",
                        )
                    )
                ),
            }
            result[instance_id] = view
        return result
    encounters = document.get("encounters")
    if not isinstance(encounters, list):
        raise ChronicleExternalTeamTimelineV2Error(
            "reconstruction manifest has neither instances nor encounters"
        )
    source_binding = _mapping(
        document.get("source_binding"), label="reconstruction.source_binding"
    )
    partition = _mapping(
        source_binding.get("normalized_partition"),
        label="reconstruction.normalized_partition",
    )
    instance_id = None
    if encounters:
        first = _mapping(encounters[0], label="reconstruction encounter")
        # The encounter entries intentionally omit instance_id.  The admitted
        # partition path begins with the exact instance UUID.
        path_name = Path(_text(partition.get("path"), label="partition.path")).name
        instance_id = path_name.split(".", 1)[0]
        if not instance_id:
            instance_id = None
        del first
    if instance_id is None:
        path_name = Path(_text(partition.get("path"), label="partition.path")).name
        instance_id = path_name.split(".", 1)[0]
    return {instance_id: document}


def _partition_identity(
    partition: Mapping[str, Any], *, path_key: str
) -> dict[str, Any]:
    return {
        "path_name": Path(_text(partition.get(path_key), label=path_key)).name,
        "compressed_file_sha256": _sha(
            partition.get("compressed_file_sha256"),
            label="partition.compressed_file_sha256",
        ),
        "logical_content_sha256": _sha(
            partition.get("logical_content_sha256"),
            label="partition.logical_content_sha256",
        ),
        "record_count": _nonnegative_integer(
            partition.get("record_count"), label="partition.record_count"
        ),
        "encounter_count": _nonnegative_integer(
            partition.get("encounter_count"), label="partition.encounter_count"
        ),
    }


def _validate_inputs(
    *,
    normalization_manifest_path: str | Path,
    admission_manifest_path: str | Path,
    reconstruction_manifest_path: str | Path,
) -> tuple[InputContext, ...]:
    normalization, normalization_stable, normalization_addressed, norm_payload = (
        _load_normalization_manifest(normalization_manifest_path)
    )
    try:
        admission, admission_resolved = load_admission_manifest(
            admission_manifest_path
        )
        reconstruction, reconstruction_resolved = (
            reconstruction_v2.load_external_reconstruction_manifest(
                reconstruction_manifest_path, verify_artifacts=True
            )
        )
    except (
        ChronicleExternalAdmissionError,
        reconstruction_v2.ChronicleExternalReconstructionV2Error,
    ) as error:
        raise ChronicleExternalTeamTimelineV2Error(str(error)) from error

    roots = {
        _data_root_for(normalization_stable),
        _data_root_for(admission_resolved),
        _data_root_for(reconstruction_resolved),
    }
    if len(roots) != 1:
        raise ChronicleExternalTeamTimelineV2Error(
            "normalization, admission, and reconstruction do not share offline_data"
        )
    data_root = roots.pop()

    admission_content = _verify_content_address(admission, label="admission manifest")
    admission_stable = admission_resolved.with_name("manifest.json")
    admission_addressed = admission_resolved.with_name(
        f"chronicle_external_reconstruction_admission_v1.{admission_content}.manifest.json"
    )
    _, _, admission_payload = _stable_and_addressed(
        admission_resolved,
        addressed_name=admission_addressed.name,
        label="admission",
    )
    reconstruction_content = _verify_content_address(
        reconstruction, label="reconstruction manifest"
    )
    reconstruction_stable = reconstruction_resolved.with_name("manifest.json")
    reconstruction_addressed = reconstruction_resolved.with_name(
        "chronicle_external_encounter_reconstruction_v2."
        f"{reconstruction_content}.manifest.json"
    )
    _, _, reconstruction_payload = _stable_and_addressed(
        reconstruction_resolved,
        addressed_name=reconstruction_addressed.name,
        label="reconstruction",
    )

    normalization_content = _verify_content_address(
        normalization, label="normalization manifest"
    )
    normalization_file_sha = _sha256_bytes(norm_payload)
    admission_file_sha = _sha256_bytes(admission_payload)
    reconstruction_file_sha = _sha256_bytes(reconstruction_payload)

    admission_inputs = _mapping(admission.get("inputs"), label="admission.inputs")
    admitted_normalization = _mapping(
        admission_inputs.get("normalization_manifest"),
        label="admission normalization input",
    )
    normalization_expected = {
        "schema": NORMALIZATION_SCHEMA,
        "content_sha256": normalization_content,
        "file_sha256": normalization_file_sha,
        "size_bytes": len(norm_payload),
    }
    for key, value in normalization_expected.items():
        if admitted_normalization.get(key) != value:
            raise ChronicleExternalTeamTimelineV2Error(
                f"admission normalization binding disagrees with explicit normalization {key}"
            )
    admission_expected = {
        "schema": ADMISSION_SCHEMA,
        "implementation_revision": ADMISSION_IMPLEMENTATION_REVISION,
        "content_sha256": admission_content,
        "file_sha256": admission_file_sha,
        "size_bytes": len(admission_payload),
    }
    if reconstruction.get("status") != reconstruction_v2.STATUS:
        raise ChronicleExternalTeamTimelineV2Error(
            "reconstruction status is not External V2 reconstruction-only"
        )
    normalization_partitions = _index_by_instance(
        _array(normalization.get("partitions"), label="normalization.partitions"),
        label="normalization.partitions",
    )
    admission_instances = _index_by_instance(
        _array(admission.get("instances"), label="admission.instances"),
        label="admission.instances",
    )
    reconstruction_instances = _reconstruction_instances(reconstruction)
    instance_sets = (
        set(normalization_partitions),
        set(admission_instances),
        set(reconstruction_instances),
    )
    if not (instance_sets[0] == instance_sets[1] == instance_sets[2]):
        raise ChronicleExternalTeamTimelineV2Error(
            "normalization/admission/reconstruction instance sets differ"
        )
    if not instance_sets[0]:
        raise ChronicleExternalTeamTimelineV2Error("input closure has no instances")

    try:
        if hasattr(reconstruction_v2, "_source_contexts"):
            source_context_values = reconstruction_v2._source_contexts(
                admission_resolved
            )
        else:
            source_context_values = [
                reconstruction_v2._source_context(admission_resolved)
            ]
    except (
        ChronicleExternalAdmissionError,
        reconstruction_v2.ChronicleExternalReconstructionV2Error,
    ) as error:
        raise ChronicleExternalTeamTimelineV2Error(str(error)) from error
    source_contexts = {
        context.instance_id: context for context in source_context_values
    }
    if set(source_contexts) != instance_sets[0]:
        raise ChronicleExternalTeamTimelineV2Error(
            "classification source contexts differ from manifest instance set"
        )

    common_binding = {
        "normalization_manifest": {
            "stable_path": _relative(
                normalization_stable, data_root, label="normalization stable manifest"
            ),
            "content_addressed_path": _relative(
                normalization_addressed,
                data_root,
                label="normalization addressed manifest",
            ),
            "schema": NORMALIZATION_SCHEMA,
            "implementation_revision": NORMALIZATION_IMPLEMENTATION_REVISION,
            "content_sha256": normalization_content,
            "file_sha256": normalization_file_sha,
            "size_bytes": len(norm_payload),
        },
        "admission_manifest": {
            "stable_path": _relative(
                admission_stable, data_root, label="admission stable manifest"
            ),
            "content_addressed_path": _relative(
                admission_addressed, data_root, label="admission addressed manifest"
            ),
            "schema": ADMISSION_SCHEMA,
            "implementation_revision": ADMISSION_IMPLEMENTATION_REVISION,
            "content_sha256": admission_content,
            "file_sha256": admission_file_sha,
            "size_bytes": len(admission_payload),
        },
        "reconstruction_manifest": {
            "stable_path": _relative(
                reconstruction_stable,
                data_root,
                label="reconstruction stable manifest",
            ),
            "content_addressed_path": _relative(
                reconstruction_addressed,
                data_root,
                label="reconstruction addressed manifest",
            ),
            "schema": reconstruction_v2.SCHEMA,
            "implementation_revision": reconstruction_v2.IMPLEMENTATION_REVISION,
            "content_sha256": reconstruction_content,
            "file_sha256": reconstruction_file_sha,
            "size_bytes": len(reconstruction_payload),
        },
        "raw_api_manifest": deepcopy(dict(admission_inputs.get("raw_api_manifest", {}))),
        "classification_resolver": {
            "official_commit": OFFICIAL_COMMIT,
            "official_evidence_sha256": OFFICIAL_EVIDENCE_SHA256,
        },
    }

    contexts: list[InputContext] = []
    for instance_id in sorted(instance_sets[0]):
        admission_instance = admission_instances[instance_id]
        if admission_instance.get("status") != ADMISSION_STATUS:
            raise ChronicleExternalTeamTimelineV2Error(
                f"admission status is unsupported for {instance_id}"
            )
        reconstruction_instance = reconstruction_instances[instance_id]
        reconstruction_binding = _mapping(
            reconstruction_instance.get("source_binding"),
            label="reconstruction instance source_binding",
        )
        reconstructed_normalization = _mapping(
            reconstruction_binding.get("normalized_manifest"),
            label="reconstruction normalized_manifest",
        )
        reconstructed_admission = _mapping(
            reconstruction_binding.get("admission_manifest"),
            label="reconstruction admission_manifest",
        )
        for key, value in normalization_expected.items():
            if reconstructed_normalization.get(key) != value:
                raise ChronicleExternalTeamTimelineV2Error(
                    "reconstruction normalization binding disagrees with "
                    f"explicit normalization {key} for {instance_id}"
                )
        for key, value in admission_expected.items():
            if reconstructed_admission.get(key) != value:
                raise ChronicleExternalTeamTimelineV2Error(
                    "reconstruction admission binding disagrees with "
                    f"explicit admission {key} for {instance_id}"
                )

        admitted_source = _mapping(
            admission_instance.get("source_evidence"),
            label="admission source_evidence",
        )
        admitted_partition = _mapping(
            admitted_source.get("normalized_partition"),
            label="admission normalized_partition",
        )
        reconstructed_partition = _mapping(
            reconstruction_binding.get("normalized_partition"),
            label="reconstruction normalized_partition",
        )
        norm_identity = _partition_identity(
            normalization_partitions[instance_id], path_key="partition"
        )
        admission_identity = _partition_identity(
            admitted_partition, path_key="path"
        )
        reconstruction_identity = _partition_identity(
            reconstructed_partition, path_key="path"
        )
        if not (
            norm_identity == admission_identity == reconstruction_identity
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "normalization partition identity differs across the three "
                f"inputs for {instance_id}"
            )
        summary = _mapping(
            reconstruction_instance.get("summary"),
            label="reconstruction instance summary",
        )
        if (
            summary.get("source_row_count") != norm_identity["record_count"]
            or summary.get("encounter_count") != norm_identity["encounter_count"]
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                f"reconstruction counts disagree for {instance_id}"
            )

        temporal = _mapping(
            admission_instance.get("temporal_and_guild_provenance"),
            label="temporal_and_guild_provenance",
        )
        contamination = _mapping(
            temporal.get("contamination"), label="raid contamination"
        )
        if contamination.get("label") not in CONTAMINATION_LABELS:
            raise ChronicleExternalTeamTimelineV2Error(
                "raid contamination label is outside the versioned admission contract"
            )
        if contamination.get("time_field") != "started_at":
            raise ChronicleExternalTeamTimelineV2Error(
                "raid contamination must be classified by started_at"
            )
        if contamination.get("uploaded_at_used") not in (False, None):
            raise ChronicleExternalTeamTimelineV2Error(
                "uploaded_at may not classify raid contamination"
            )
        reconstructed_provenance = _mapping(
            reconstruction_instance.get("instance_provenance"),
            label="reconstruction instance provenance",
        )
        if (
            reconstructed_provenance.get("temporal_and_guild_provenance")
            != temporal
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "raid temporal/guild provenance changed for "
                f"{instance_id}"
            )
        instance_binding = {
            **deepcopy(common_binding),
            "instance_id": instance_id,
            "normalized_partition": norm_identity,
        }
        warrior_spec = _mapping(
            admission_instance.get("warrior_spec_evidence"),
            label="warrior_spec_evidence",
        )
        warrior_spec_by_guid: dict[str, Mapping[str, Any]] = {}
        for raw_observation in _array(
            warrior_spec.get("observations"),
            label="warrior_spec_evidence.observations",
        ):
            observation = _mapping(
                raw_observation, label="warrior spec observation"
            )
            guid = canonical_guid(
                observation.get("player_guid"),
                label="warrior spec observation player_guid",
            )
            if guid in warrior_spec_by_guid:
                raise ChronicleExternalTeamTimelineV2Error(
                    f"duplicate warrior spec observation for {guid}"
                )
            player = source_contexts[instance_id].player_resolver.get(guid)
            metadata = (
                player.get("metadata") if isinstance(player, Mapping) else None
            )
            if (
                not isinstance(metadata, Mapping)
                or str(metadata.get("class") or "").upper() != "WARRIOR"
                or str(observation.get("player_class") or "").upper()
                != "WARRIOR"
            ):
                raise ChronicleExternalTeamTimelineV2Error(
                    "warrior spec observation does not exact-match a metadata warrior"
                )
            warrior_spec_by_guid[guid] = deepcopy(dict(observation))
        contexts.append(
            InputContext(
                data_root=data_root,
                instance_id=instance_id,
                normalization=normalization,
                normalization_stable=normalization_stable,
                normalization_addressed=normalization_addressed,
                normalization_payload=norm_payload,
                admission=admission,
                admission_stable=admission_stable,
                admission_addressed=admission_addressed,
                admission_payload=admission_payload,
                admission_instance=admission_instance,
                reconstruction_document=reconstruction,
                reconstruction_instance=reconstruction_instance,
                reconstruction_stable=reconstruction_stable,
                reconstruction_addressed=reconstruction_addressed,
                reconstruction_payload=reconstruction_payload,
                reconstruction_source_context=source_contexts[instance_id],
                warrior_spec_by_guid=warrior_spec_by_guid,
                source_binding=instance_binding,
            )
        )
    return tuple(contexts)


def _anchor_order(
    anchor: Mapping[str, Any], *, encounter_ordinal: int
) -> tuple[int, int, int, int, int]:
    stream_type = _text(anchor.get("stream_type"), label="anchor.stream_type")
    try:
        stream_order = STREAM_ORDER[stream_type]
    except KeyError as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"unsupported anchor stream type {stream_type}"
        ) from error
    return (
        encounter_ordinal,
        _nonnegative_integer(anchor.get("timestamp_ms"), label="anchor.timestamp_ms"),
        _integer(anchor.get("event_index"), label="anchor.event_index"),
        stream_order,
        _nonnegative_integer(
            anchor.get("frame_message_index"),
            label="anchor.frame_message_index",
        ),
    )


@dataclass(frozen=True)
class WaveSpec:
    encounter_id: str
    encounter_ordinal: int
    wave_id: str
    wave_ordinal: int
    start_order: tuple[int, int, int, int, int]
    end_order: tuple[int, int, int, int, int]
    window: Mapping[str, Any]
    reconstruction_content_sha256: str
    expected_event_counts: Mapping[str, int]
    voting_target_count: int
    nonvoting_target_count: int


@dataclass(frozen=True)
class EncounterSpec:
    encounter_id: str
    encounter_ordinal: int
    artifact_content_sha256: str
    waves: tuple[WaveSpec, ...]


def _load_encounter_spec(
    context: InputContext, entry: Mapping[str, Any]
) -> EncounterSpec:
    reference = _mapping(entry.get("artifact"), label="encounter artifact reference")
    relative = Path(_text(reference.get("path"), label="encounter artifact path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalTeamTimelineV2Error(
            "encounter artifact path escapes reconstruction directory"
        )
    path = _under(
        context.reconstruction_stable.parent / relative,
        context.reconstruction_stable.parent,
        label="encounter artifact",
    )
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalTeamTimelineV2Error(
            f"cannot read verified encounter artifact {path}: {error}"
        ) from error
    if not isinstance(artifact, dict):
        raise ChronicleExternalTeamTimelineV2Error(
            "verified encounter artifact is not an object"
        )
    content_sha = _verify_content_address(artifact, label="encounter artifact")
    if content_sha != reference.get("content_sha256"):
        raise ChronicleExternalTeamTimelineV2Error(
            "encounter artifact content hash differs from reconstruction manifest"
        )
    encounter_id = _text(entry.get("encounter_id"), label="encounter_id")
    encounter_ordinal = _nonnegative_integer(
        entry.get("encounter_ordinal"), label="encounter_ordinal"
    )
    if (
        artifact.get("encounter_id") != encounter_id
        or artifact.get("encounter_ordinal") != encounter_ordinal
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "encounter artifact identity differs from reconstruction manifest"
        )
    waves: list[WaveSpec] = []
    prior_end: tuple[int, int, int, int, int] | None = None
    for index, raw_wave in enumerate(
        _array(artifact.get("waves"), label="encounter waves"), 1
    ):
        wave = _mapping(raw_wave, label="reconstruction wave")
        wave_content = _verify_content_address(wave, label="reconstruction wave")
        ordinal = _nonnegative_integer(wave.get("ordinal"), label="wave.ordinal")
        if ordinal != index:
            raise ChronicleExternalTeamTimelineV2Error(
                "reconstruction wave ordinals are not contiguous from one"
            )
        window = _mapping(wave.get("window"), label="wave.window")
        first_anchor = _mapping(window.get("first_anchor"), label="wave.first_anchor")
        last_anchor = _mapping(
            window.get("last_context_anchor"), label="wave.last_context_anchor"
        )
        start_order = _anchor_order(
            first_anchor, encounter_ordinal=encounter_ordinal
        )
        end_order = _anchor_order(last_anchor, encounter_ordinal=encounter_ordinal)
        if end_order < start_order:
            raise ChronicleExternalTeamTimelineV2Error(
                "reconstruction wave window ends before it starts"
            )
        if prior_end is not None and start_order <= prior_end:
            raise ChronicleExternalTeamTimelineV2Error(
                "reconstruction wave windows overlap or are out of order"
            )
        prior_end = end_order
        event_counts = {
            str(key): _nonnegative_integer(value, label="wave event count")
            for key, value in _mapping(
                wave.get("event_counts"), label="wave.event_counts"
            ).items()
        }
        waves.append(
            WaveSpec(
                encounter_id=encounter_id,
                encounter_ordinal=encounter_ordinal,
                wave_id=_text(wave.get("wave_id"), label="wave.wave_id"),
                wave_ordinal=ordinal,
                start_order=start_order,
                end_order=end_order,
                window=deepcopy(dict(window)),
                reconstruction_content_sha256=wave_content,
                expected_event_counts=event_counts,
                voting_target_count=_nonnegative_integer(
                    wave.get("voting_hostile_creature_target_count"),
                    label="wave.voting_hostile_creature_target_count",
                ),
                nonvoting_target_count=_nonnegative_integer(
                    wave.get("nonvoting_target_count"),
                    label="wave.nonvoting_target_count",
                ),
            )
        )
    return EncounterSpec(
        encounter_id=encounter_id,
        encounter_ordinal=encounter_ordinal,
        artifact_content_sha256=content_sha,
        waves=tuple(waves),
    )


def _state_evidence(state: Any) -> dict[str, Any]:
    anchor = state.anchor
    return {
        "guid": state.guid,
        "lane": state.lane,
        "unit_type_numeric": state.unit_type_numeric,
        "affiliation_numeric": state.affiliation_numeric,
        "classification_anchor": (
            {
                "timestamp_ms": anchor.get("timestamp_ms"),
                "event_index": anchor.get("event_index"),
                "stream_type": anchor.get("stream_type"),
                "frame_message_index": anchor.get("frame_message_index"),
                "official_message_sha256": anchor.get(
                    "official_message_sha256"
                ),
            }
            if anchor.get("event_index") is not None
            else None
        ),
        "state_status": state.resolution_status,
    }


def _state_evidence_for_guid(
    guid: str | None, states: Mapping[str, Any]
) -> dict[str, Any]:
    if guid is None:
        return {
            "guid": None,
            "lane": reconstruction_v2.LANE_UNKNOWN,
            "unit_type_numeric": 0,
            "affiliation_numeric": 0,
            "classification_anchor": None,
            "state_status": "ABSENT_GUID_UNKNOWN_NONVOTING",
        }
    return _state_evidence(
        states.get(guid) or reconstruction_v2._unknown_state(guid)
    )


def _team_actor(
    source_guid: str | None,
    states: Mapping[str, Any],
    context: InputContext,
) -> dict[str, Any]:
    raw = reconstruction_v2._source_actor(
        source_guid, states, context.reconstruction_source_context
    )
    result = deepcopy(dict(raw))
    source_state = (
        states.get(source_guid or "")
        or reconstruction_v2._unknown_state(source_guid or "0x0")
    )
    player = result.get("player")
    if not isinstance(player, Mapping) or not isinstance(player.get("guid"), str):
        if source_state.lane == reconstruction_v2.LANE_HOSTILE_PLAYER:
            result["pre_team_filter_status"] = result.get("status")
            result["status"] = "SOURCE_IS_OFFICIAL_HOSTILE_PLAYER_AT_EVENT"
        result["attribution_kind"] = "UNATTRIBUTED"
        return result

    player_guid = canonical_guid(player.get("guid"), label="attributed player GUID")
    player_state = states.get(player_guid)
    denial: str | None = None
    if source_state.lane == reconstruction_v2.LANE_HOSTILE_PLAYER:
        denial = "SOURCE_IS_OFFICIAL_HOSTILE_PLAYER_AT_EVENT"
    elif (
        player_state is None
        or player_state.lane != reconstruction_v2.LANE_FRIENDLY_PLAYER
    ):
        denial = "ATTRIBUTED_PLAYER_NOT_OFFICIAL_FRIENDLY_PLAYER_AT_EVENT"
    if denial is not None:
        result["pre_team_filter_status"] = result.get("status")
        result["status"] = denial
        result["player"] = None
        result["attribution_kind"] = "UNATTRIBUTED"
        return result

    status = str(result.get("status"))
    if status == "EXACT_FRIENDLY_PLAYER_SOURCE":
        kind = "DIRECT_FRIENDLY_PLAYER"
    elif "_CONTROLLER_" in status:
        kind = "EXACT_OFFICIAL_CONTROLLER"
    elif "_OWNER_" in status:
        kind = "EXACT_OFFICIAL_OWNER"
    else:
        raise ChronicleExternalTeamTimelineV2Error(
            f"unexpected exact player attribution status {status}"
        )
    result["attribution_kind"] = kind
    result["player_guid"] = player_guid
    result.pop("player", None)
    return result


def _compact_anchor(row: Mapping[str, Any]) -> dict[str, Any]:
    anchor = reconstruction_v2._anchor(row)
    return {
        key: anchor.get(key)
        for key in (
            "timestamp_ms",
            "offset_ms",
            "event_index",
            "stream_type",
            "frame_index",
            "frame_message_index",
            "derived_jsonl_line",
            "official_message_sha256",
        )
    }


def _compact_timeline_event(
    row: Mapping[str, Any],
    states: Mapping[str, Any],
    context: InputContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_type = _text(row.get("type"), label="row.type").upper()
    if event_type not in TIMELINE_EVENT_TYPES:
        raise ChronicleExternalTeamTimelineV2Error(
            f"unsupported timeline event type {event_type}"
        )
    source_guid = reconstruction_v2._guid(
        row.get("source_guid"), label="row.source_guid"
    )
    target_guid = reconstruction_v2._guid(
        row.get("target_guid"), label="row.target_guid"
    )
    source_state = states.get(source_guid or "") or (
        reconstruction_v2._unknown_state(source_guid) if source_guid else None
    )
    target_state = states.get(target_guid or "") or (
        reconstruction_v2._unknown_state(target_guid) if target_guid else None
    )
    attribution = _team_actor(source_guid, states, context)
    official = _mapping(row.get("official"), label="row.official")
    message = _mapping(official.get("message"), label="row.official.message")
    spell_data = message.get("spell_data")
    spell_mapping = spell_data if isinstance(spell_data, Mapping) else {}
    record: dict[str, Any] = {
        "event_type": event_type,
        "anchor": _compact_anchor(row),
        "source": _state_evidence_for_guid(source_guid, states),
        "target": {
            **_state_evidence_for_guid(target_guid, states),
            "voting_enemy_target": (
                target_guid is not None
                and target_state is not None
                and target_state.lane == reconstruction_v2.LANE_HOSTILE_CREATURE
            ),
            "hostile_object_preserved_nonvoting": (
                target_guid is not None
                and target_state is not None
                and target_state.lane == reconstruction_v2.LANE_HOSTILE_OBJECT
            ),
            "hostile_player_preserved_nonvoting": (
                target_guid is not None
                and target_state is not None
                and target_state.lane == reconstruction_v2.LANE_HOSTILE_PLAYER
            ),
        },
        "spell": {
            "id": row.get("spell_id"),
            "name": row.get("spell"),
            "official_attack_outcome": spell_mapping.get("attack_outcome"),
        },
        "attribution": attribution,
    }
    if event_type == "DMG":
        amount = _integer(row.get("value"), label="DMG.value")
        if message.get("amount") != amount:
            raise ChronicleExternalTeamTimelineV2Error(
                "DMG normalized amount differs from official message"
            )
        if amount < 0:
            record["negative_damage_diagnostic"] = {
                "lane": NEGATIVE_DAMAGE_DIAGNOSTIC_LANE,
                "signed_amount": amount,
                "absolute_magnitude": abs(amount),
                "policy": NEGATIVE_DAMAGE_VALUE_POLICY,
                "damage_amount_added": 0,
                "reward_amount_added": 0,
                "hit_type": message.get("hit_type"),
                "overkill": message.get("overkill"),
                "source_name": message.get("source_name"),
                "school": deepcopy(message.get("school")),
            }
        else:
            record["damage"] = {
                "amount": amount,
                "amount_source": "DMG_ONLY",
                "hit_type": message.get("hit_type"),
                "overkill": message.get("overkill"),
                "source_name": message.get("source_name"),
                "school": deepcopy(message.get("school")),
            }
    elif event_type == "HEAL":
        amount = _nonnegative_integer(row.get("value"), label="HEAL.value")
        if message.get("amount") != amount:
            raise ChronicleExternalTeamTimelineV2Error(
                "HEAL normalized amount differs from official message"
            )
        record["healing"] = {
            "amount": amount,
            "amount_source": "HEAL_ONLY",
            "overheal": message.get("overheal"),
            "absorbed": message.get("absorbed"),
            "hit_type": message.get("hit_type"),
        }
    elif event_type == "START":
        record["action"] = {
            "phase": "START",
            "cast_time_ms": message.get("cast_time_ms"),
            "channel_time_ms": message.get("channel_time_ms"),
            "cast_flags": message.get("cast_flags"),
            "item_id": message.get("item_id"),
        }
    elif event_type == "GO":
        record["action"] = {
            "phase": "GO",
            "num_hits": message.get("num_hits"),
            "num_misses": message.get("num_misses"),
            "item_id": message.get("item_id"),
        }
    elif event_type == "FAIL":
        record["action"] = {
            "phase": "FAIL",
            "failed_by_server": message.get("failed_by_server"),
        }
    elif event_type == "DEAD":
        if row.get("value") is not None:
            raise ChronicleExternalTeamTimelineV2Error(
                "DEAD.value must be absent; slain is marker-only"
            )
        record["death"] = {
            "marker_only": True,
            "damage_amount_added": 0,
            "nested_attribution_present": isinstance(
                message.get("attribution"), Mapping
            ),
            "nested_attribution_amount_ignored": (
                isinstance(message.get("attribution"), Mapping)
            ),
        }
    return record, attribution


def _timeline_order(
    event: Mapping[str, Any], *, encounter_ordinal: int
) -> tuple[int, int, int, int, int]:
    return _anchor_order(
        _mapping(event.get("anchor"), label="timeline event anchor"),
        encounter_ordinal=encounter_ordinal,
    )


def _assert_strict_timeline_order(
    events: Sequence[Mapping[str, Any]], *, encounter_ordinal: int, label: str
) -> None:
    prior: tuple[int, int, int, int, int] | None = None
    for event in events:
        order = _timeline_order(event, encounter_ordinal=encounter_ordinal)
        if prior is not None and order <= prior:
            raise ChronicleExternalTeamTimelineV2Error(
                f"{label} is not in strict EventMeta order"
            )
        prior = order


def _player_warrior_spec_evidence(
    context: InputContext, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    guid = canonical_guid(metadata.get("guid"), label="player metadata GUID")
    player_class = str(metadata.get("class") or "").upper()
    if player_class != "WARRIOR":
        return {
            "status": "NOT_APPLICABLE_NON_WARRIOR",
            "player_spec": None,
            "field_conflicts": {},
            "sources": [],
            "exact_player_guid_match": True,
            "voting_for_fury_or_arms_lane": False,
            "inference_used": False,
        }
    observation = context.warrior_spec_by_guid.get(guid)
    if observation is None:
        return {
            "status": "MISSING_ADMISSION_WARRIOR_SPEC_OBSERVATION_NONVOTING",
            "player_spec": "Unknown",
            "field_conflicts": {},
            "sources": [],
            "exact_player_guid_match": False,
            "voting_for_fury_or_arms_lane": False,
            "inference_used": False,
        }
    spec = observation.get("player_spec")
    status = observation.get("spec_evidence_status")
    conflicts = observation.get("field_conflicts")
    has_conflict = isinstance(conflicts, Mapping) and bool(conflicts)
    voting = status == "OBSERVED" and spec in {"Fury", "Arms"} and not has_conflict
    return {
        "status": status,
        "player_spec": spec,
        "field_conflicts": deepcopy(dict(conflicts)) if isinstance(conflicts, Mapping) else {},
        "sources": deepcopy(observation.get("sources"))
        if isinstance(observation.get("sources"), list)
        else [],
        "exact_player_guid_match": observation.get("player_guid") == guid,
        "voting_for_fury_or_arms_lane": voting,
        "inference_used": False,
    }


@dataclass
class WaveBuilder:
    spec: WaveSpec
    context: InputContext
    player_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unattributed_events: list[dict[str, Any]] = field(default_factory=list)
    death_markers: list[dict[str, Any]] = field(default_factory=list)
    negative_damage_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    classification_transitions: list[dict[str, Any]] = field(default_factory=list)
    event_type_counts: Counter[str] = field(default_factory=Counter)
    source_lane_event_counts: Counter[str] = field(default_factory=Counter)
    target_lane_event_counts: Counter[str] = field(default_factory=Counter)
    attribution_kind_counts: Counter[str] = field(default_factory=Counter)
    attributed_status_counts: Counter[str] = field(default_factory=Counter)
    player_event_count: int = 0
    unattributed_event_count: int = 0
    damage_amount: int = 0
    player_damage_amount: int = 0
    unattributed_damage_amount: int = 0
    healing_amount: int = 0
    player_healing_amount: int = 0
    unattributed_healing_amount: int = 0
    negative_damage_signed_amount_excluded: int = 0
    negative_damage_absolute_amount_excluded: int = 0
    hostile_object_source_attributed_event_count: int = 0
    hostile_object_target_event_count: int = 0
    hostile_player_source_event_count: int = 0
    hostile_player_target_event_count: int = 0

    def observe_classification(self, state: Any) -> None:
        compact = state.compact()
        self.classification_transitions.append(compact)

    def observe_event(
        self, row: Mapping[str, Any], states: Mapping[str, Any]
    ) -> None:
        record, attribution = _compact_timeline_event(
            row, states, self.context
        )
        event_type = str(record["event_type"])
        self.event_type_counts[event_type] += 1
        source_lane = str(record["source"]["lane"])
        target_lane = str(record["target"]["lane"])
        self.source_lane_event_counts[source_lane] += 1
        self.target_lane_event_counts[target_lane] += 1
        if source_lane == reconstruction_v2.LANE_HOSTILE_PLAYER:
            self.hostile_player_source_event_count += 1
            if attribution.get("player_guid") is not None:
                raise ChronicleExternalTeamTimelineV2Error(
                    "HostilePlayer source entered a friendly player timeline"
                )
        if target_lane == reconstruction_v2.LANE_HOSTILE_PLAYER:
            self.hostile_player_target_event_count += 1
        if target_lane == reconstruction_v2.LANE_HOSTILE_OBJECT:
            self.hostile_object_target_event_count += 1
            if record["target"]["voting_enemy_target"] is not False:
                raise ChronicleExternalTeamTimelineV2Error(
                    "HostileObject target became a voting enemy target"
                )

        if event_type == "DEAD":
            self.death_markers.append(record)
            self.attribution_kind_counts["DEATH_MARKER"] += 1
            return

        if "negative_damage_diagnostic" in record:
            diagnostic = _mapping(
                record.get("negative_damage_diagnostic"),
                label="negative_damage_diagnostic",
            )
            signed_amount = _integer(
                diagnostic.get("signed_amount"),
                label="negative_damage_diagnostic.signed_amount",
            )
            if signed_amount >= 0:
                raise ChronicleExternalTeamTimelineV2Error(
                    "negative DMG diagnostic is not negative"
                )
            self.negative_damage_diagnostics.append(record)
            self.negative_damage_signed_amount_excluded += signed_amount
            self.negative_damage_absolute_amount_excluded += abs(signed_amount)
            self.attribution_kind_counts[NEGATIVE_DAMAGE_DIAGNOSTIC_LANE] += 1
            if source_lane == reconstruction_v2.LANE_HOSTILE_OBJECT:
                kind = attribution.get("attribution_kind")
                if kind not in {
                    "EXACT_OFFICIAL_OWNER",
                    "EXACT_OFFICIAL_CONTROLLER",
                }:
                    raise ChronicleExternalTeamTimelineV2Error(
                        "diagnostic HostileObject source lacks an official relation"
                    )
                self.hostile_object_source_attributed_event_count += 1
            return

        player_guid = attribution.get("player_guid")
        if isinstance(player_guid, str):
            player_guid = canonical_guid(
                player_guid, label="timeline player attribution GUID"
            )
            self.player_events.setdefault(player_guid, []).append(record)
            self.player_event_count += 1
            kind = _text(
                attribution.get("attribution_kind"), label="attribution_kind"
            )
            self.attribution_kind_counts[kind] += 1
            self.attributed_status_counts[str(attribution.get("status"))] += 1
            if source_lane == reconstruction_v2.LANE_HOSTILE_OBJECT:
                if kind not in {
                    "EXACT_OFFICIAL_OWNER",
                    "EXACT_OFFICIAL_CONTROLLER",
                }:
                    raise ChronicleExternalTeamTimelineV2Error(
                        "HostileObject source was not attributed through an official relation"
                    )
                self.hostile_object_source_attributed_event_count += 1
        else:
            self.unattributed_events.append(record)
            self.unattributed_event_count += 1
            self.attribution_kind_counts["UNATTRIBUTED"] += 1

        if event_type == "DMG":
            amount = int(record["damage"]["amount"])
            self.damage_amount += amount
            if isinstance(player_guid, str):
                self.player_damage_amount += amount
            else:
                self.unattributed_damage_amount += amount
        elif event_type == "HEAL":
            amount = int(record["healing"]["amount"])
            self.healing_amount += amount
            if isinstance(player_guid, str):
                self.player_healing_amount += amount
            else:
                self.unattributed_healing_amount += amount

    def finish(self) -> dict[str, Any]:
        if dict(sorted(self.event_type_counts.items())) != dict(
            sorted(self.spec.expected_event_counts.items())
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                f"wave {self.spec.wave_id} exact event counts differ from reconstruction"
            )
        if self.damage_amount != (
            self.player_damage_amount + self.unattributed_damage_amount
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "wave damage attribution accounting is not conserved"
            )
        if self.healing_amount != (
            self.player_healing_amount + self.unattributed_healing_amount
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "wave healing attribution accounting is not conserved"
            )
        nonclass_count = sum(self.event_type_counts.values())
        if nonclass_count != (
            self.player_event_count
            + self.unattributed_event_count
            + len(self.death_markers)
            + len(self.negative_damage_diagnostics)
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "wave exclusive event lanes are not conserved"
            )

        players: list[dict[str, Any]] = []
        active_player_count = 0
        for guid in sorted(self.context.reconstruction_source_context.player_resolver):
            raw_player = self.context.reconstruction_source_context.player_resolver[
                guid
            ]
            metadata = reconstruction_v2._compact_player(raw_player)
            events = self.player_events.get(guid, [])
            _assert_strict_timeline_order(
                events,
                encounter_ordinal=self.spec.encounter_ordinal,
                label=f"player {guid} timeline",
            )
            counts = Counter(str(event["event_type"]) for event in events)
            damage = sum(
                int(event["damage"]["amount"])
                for event in events
                if event["event_type"] == "DMG"
            )
            healing = sum(
                int(event["healing"]["amount"])
                for event in events
                if event["event_type"] == "HEAL"
            )
            if events:
                active_player_count += 1
            players.append(
                _content_addressed(
                    {
                        "player": metadata,
                        "warrior_spec_evidence": _player_warrior_spec_evidence(
                            self.context, metadata
                        ),
                        "timeline": events,
                        "summary": {
                            "event_count": len(events),
                            "event_type_counts": dict(sorted(counts.items())),
                            "action_event_count": sum(
                                counts.get(kind, 0) for kind in ACTION_EVENT_TYPES
                            ),
                            "damage_event_count": counts.get("DMG", 0),
                            "damage_amount": damage,
                            "healing_event_count": counts.get("HEAL", 0),
                            "healing_amount": healing,
                        },
                    }
                )
            )
        _assert_strict_timeline_order(
            self.unattributed_events,
            encounter_ordinal=self.spec.encounter_ordinal,
            label="unattributed timeline",
        )
        _assert_strict_timeline_order(
            self.death_markers,
            encounter_ordinal=self.spec.encounter_ordinal,
            label="death marker timeline",
        )
        _assert_strict_timeline_order(
            self.negative_damage_diagnostics,
            encounter_ordinal=self.spec.encounter_ordinal,
            label="negative damage diagnostic timeline",
        )

        source_binding_sha = _sha256_bytes(
            _canonical_bytes(self.context.source_binding)
        )
        core = {
            "schema": PARTITION_RECORD_SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "status": STATUS,
            "instance_id": self.context.instance_id,
            "encounter_id": self.spec.encounter_id,
            "encounter_ordinal": self.spec.encounter_ordinal,
            "wave_id": self.spec.wave_id,
            "wave_ordinal": self.spec.wave_ordinal,
            "source_binding_sha256": source_binding_sha,
            "reconstruction_binding": {
                "encounter_artifact_content_sha256": (
                    self.context.reconstruction_instance["encounters"][
                        self.spec.encounter_ordinal
                    ]["artifact"]["content_sha256"]
                ),
                "wave_content_sha256": self.spec.reconstruction_content_sha256,
                "window": deepcopy(dict(self.spec.window)),
                "voting_hostile_creature_target_count": (
                    self.spec.voting_target_count
                ),
                "nonvoting_target_count": self.spec.nonvoting_target_count,
            },
            "players": players,
            "unattributed_lane": {
                "timeline": self.unattributed_events,
                "summary": {
                    "event_count": self.unattributed_event_count,
                    "damage_amount": self.unattributed_damage_amount,
                    "healing_amount": self.unattributed_healing_amount,
                },
            },
            "death_markers": self.death_markers,
            "negative_damage_diagnostics": self.negative_damage_diagnostics,
            "classification_transitions_inside_window": (
                self.classification_transitions
            ),
            "event_accounting": {
                "classification_transition_count": len(
                    self.classification_transitions
                ),
                "nonclassification_event_count": nonclass_count,
                "event_type_counts": dict(sorted(self.event_type_counts.items())),
                "exclusive_lanes": {
                    "exact_player_event_count": self.player_event_count,
                    "unattributed_event_count": self.unattributed_event_count,
                    "death_marker_count": len(self.death_markers),
                    "negative_damage_diagnostic_count": len(
                        self.negative_damage_diagnostics
                    ),
                },
                "source_lane_event_counts": dict(
                    sorted(self.source_lane_event_counts.items())
                ),
                "target_lane_event_counts": dict(
                    sorted(self.target_lane_event_counts.items())
                ),
                "attribution_kind_counts": dict(
                    sorted(self.attribution_kind_counts.items())
                ),
                "attributed_status_counts": dict(
                    sorted(self.attributed_status_counts.items())
                ),
                "damage": {
                    "amount": self.damage_amount,
                    "amount_source": "DMG_ONLY",
                    "exact_player_amount": self.player_damage_amount,
                    "unattributed_amount": self.unattributed_damage_amount,
                    "negative_event_count_excluded": len(
                        self.negative_damage_diagnostics
                    ),
                    "negative_signed_amount_excluded": (
                        self.negative_damage_signed_amount_excluded
                    ),
                    "absolute_amount_excluded": (
                        self.negative_damage_absolute_amount_excluded
                    ),
                    "policy": NEGATIVE_DAMAGE_VALUE_POLICY,
                },
                "healing": {
                    "amount": self.healing_amount,
                    "amount_source": "HEAL_ONLY",
                    "exact_player_amount": self.player_healing_amount,
                    "unattributed_amount": self.unattributed_healing_amount,
                },
                "slain": {
                    "marker_count": len(self.death_markers),
                    "amount_added_to_damage": 0,
                },
            },
            "summary": {
                "roster_player_count": len(players),
                "active_player_count": active_player_count,
                "exact_player_event_count": self.player_event_count,
                "unattributed_event_count": self.unattributed_event_count,
                "death_marker_count": len(self.death_markers),
                "negative_damage_diagnostic_count": len(
                    self.negative_damage_diagnostics
                ),
                "negative_damage_signed_amount_excluded": (
                    self.negative_damage_signed_amount_excluded
                ),
                "negative_damage_absolute_amount_excluded": (
                    self.negative_damage_absolute_amount_excluded
                ),
                "classification_transition_count": len(
                    self.classification_transitions
                ),
                "nonclassification_event_count": nonclass_count,
                "damage_amount": self.damage_amount,
                "player_damage_amount": self.player_damage_amount,
                "unattributed_damage_amount": self.unattributed_damage_amount,
                "healing_amount": self.healing_amount,
                "player_healing_amount": self.player_healing_amount,
                "unattributed_healing_amount": self.unattributed_healing_amount,
                "hostile_object_source_attributed_event_count": (
                    self.hostile_object_source_attributed_event_count
                ),
                "hostile_object_target_event_count": (
                    self.hostile_object_target_event_count
                ),
                "hostile_player_source_event_count": (
                    self.hostile_player_source_event_count
                ),
                "hostile_player_target_event_count": (
                    self.hostile_player_target_event_count
                ),
            },
            "scientific_boundaries": {
                "team_model_built": False,
                "comparison_authorized": False,
                "policy_training_authorized": False,
                "legacy_csv_timeline_modified": False,
                "damage_amount_source": "DMG_ONLY",
                "negative_damage_value_policy": NEGATIVE_DAMAGE_VALUE_POLICY,
                "negative_damage_abs_or_clamp_used": False,
                "negative_damage_reward_amount_added": 0,
                "slain_is_marker_only": True,
                "class_future_backfill_count": 0,
                "name_identity_inference_count": 0,
                "guid_suffix_identity_inference_count": 0,
                "hostile_object_promoted_to_enemy_target": False,
                "hostile_player_promoted_to_friendly_player": False,
            },
        }
        return _content_addressed(core)


@dataclass
class EncounterBuilder:
    spec: EncounterSpec
    context: InputContext
    states: dict[str, Any] = field(default_factory=dict)
    source_row_count: int = 0
    assigned_source_row_count: int = 0
    outside_wave_row_count: int = 0
    outside_wave_event_type_counts: Counter[str] = field(default_factory=Counter)
    current_wave_index: int = 0

    def __post_init__(self) -> None:
        self.wave_builders = [
            WaveBuilder(spec=wave, context=self.context) for wave in self.spec.waves
        ]

    def observe(
        self,
        row: Mapping[str, Any],
        order: tuple[int, int, int, int, int],
    ) -> None:
        if (
            row.get("encounter") != self.spec.encounter_id
            or order[0] != self.spec.encounter_ordinal
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "row does not belong to the active reconstruction encounter"
            )
        self.source_row_count += 1
        while (
            self.current_wave_index < len(self.spec.waves)
            and order > self.spec.waves[self.current_wave_index].end_order
        ):
            self.current_wave_index += 1
        active: WaveBuilder | None = None
        if self.current_wave_index < len(self.spec.waves):
            wave = self.spec.waves[self.current_wave_index]
            if wave.start_order <= order <= wave.end_order:
                active = self.wave_builders[self.current_wave_index]

        event_type = _text(row.get("type"), label="row.type").upper()
        state = None
        if event_type == "CLASS":
            try:
                state = reconstruction_v2._state_from_class_row(
                    row, self.context.reconstruction_source_context
                )
            except reconstruction_v2.ChronicleExternalReconstructionV2Error as error:
                raise ChronicleExternalTeamTimelineV2Error(str(error)) from error
            self.states[state.guid] = state
        elif event_type not in TIMELINE_EVENT_TYPES:
            raise ChronicleExternalTeamTimelineV2Error(
                f"unsupported admitted event type {event_type}"
            )

        if active is None:
            self.outside_wave_row_count += 1
            self.outside_wave_event_type_counts[event_type] += 1
            return
        self.assigned_source_row_count += 1
        if state is not None:
            active.observe_classification(state)
        else:
            active.observe_event(row, self.states)

    def finish(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.source_row_count != (
            self.assigned_source_row_count + self.outside_wave_row_count
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "encounter source-row accounting is not conserved"
            )
        waves = [builder.finish() for builder in self.wave_builders]
        summary_core = {
            "encounter_id": self.spec.encounter_id,
            "encounter_ordinal": self.spec.encounter_ordinal,
            "reconstruction_artifact_content_sha256": (
                self.spec.artifact_content_sha256
            ),
            "source_row_count": self.source_row_count,
            "assigned_source_row_count": self.assigned_source_row_count,
            "outside_wave_row_count": self.outside_wave_row_count,
            "outside_wave_event_type_counts": dict(
                sorted(self.outside_wave_event_type_counts.items())
            ),
            "wave_count": len(waves),
            "wave_content_sha256": [
                wave["content_address"]["sha256"] for wave in waves
            ],
        }
        return waves, _content_addressed(summary_core)


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    compressed_file_sha256: str
    manifest_entry: Mapping[str, Any]


def _summary_totals(waves: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    keys = (
        "exact_player_event_count",
        "unattributed_event_count",
        "death_marker_count",
        "negative_damage_diagnostic_count",
        "negative_damage_absolute_amount_excluded",
        "classification_transition_count",
        "nonclassification_event_count",
        "damage_amount",
        "player_damage_amount",
        "unattributed_damage_amount",
        "healing_amount",
        "player_healing_amount",
        "unattributed_healing_amount",
        "hostile_object_source_attributed_event_count",
        "hostile_object_target_event_count",
        "hostile_player_source_event_count",
        "hostile_player_target_event_count",
    )
    result = {
        key: sum(
            _nonnegative_integer(
                _mapping(wave.get("summary"), label="wave.summary").get(key),
                label=f"wave.summary.{key}",
            )
            for wave in waves
        )
        for key in keys
    }
    result["negative_damage_signed_amount_excluded"] = sum(
        _integer(
            _mapping(wave.get("summary"), label="wave.summary").get(
                "negative_damage_signed_amount_excluded"
            ),
            label="wave.summary.negative_damage_signed_amount_excluded",
        )
        for wave in waves
    )
    return result


def _build_instance_partition(
    context: InputContext | PartitionInputContext, *, output_directory: Path
) -> PartitionBuild:
    encounter_entries = _array(
        context.reconstruction_instance.get("encounters"),
        label="reconstruction instance encounters",
    )
    descriptor, name = tempfile.mkstemp(
        prefix=f".{_safe_component(context.instance_id, label='instance_id')}.timeline.",
        suffix=".jsonl.gz.tmp",
        dir=output_directory,
    )
    os.close(descriptor)
    temporary = Path(name)
    logical_digest = hashlib.sha256()
    logical_size = 0
    wave_record_count = 0
    encounter_summaries: list[dict[str, Any]] = []
    wave_summaries: list[Mapping[str, Any]] = []
    prior_order: tuple[int, int, int, int, int] | None = None
    active: EncounterBuilder | None = None
    next_encounter_index = 0

    def write_finished(builder: EncounterBuilder, compressed: Any) -> None:
        nonlocal logical_size, wave_record_count, next_encounter_index
        waves, encounter_summary = builder.finish()
        encounter_summaries.append(encounter_summary)
        for wave in waves:
            payload = _canonical_bytes(wave) + b"\n"
            compressed.write(payload)
            logical_digest.update(payload)
            logical_size += len(payload)
            wave_record_count += 1
            wave_summaries.append(_mapping(wave.get("summary"), label="wave.summary"))
        next_encounter_index += 1

    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
            ) as compressed:
                for row in iter_resolved_reconstruction_rows(
                    context.admission_stable, instance_id=context.instance_id
                ):
                    try:
                        order = reconstruction_v2._row_order(
                            row, context=context.reconstruction_source_context
                        )
                    except reconstruction_v2.ChronicleExternalReconstructionV2Error as error:
                        raise ChronicleExternalTeamTimelineV2Error(str(error)) from error
                    if prior_order is not None and order <= prior_order:
                        raise ChronicleExternalTeamTimelineV2Error(
                            "admitted EventMeta order is not strictly increasing"
                        )
                    prior_order = order
                    encounter_id = _text(row.get("encounter"), label="row.encounter")
                    if active is None or encounter_id != active.spec.encounter_id:
                        if active is not None:
                            write_finished(active, compressed)
                        if next_encounter_index >= len(encounter_entries):
                            raise ChronicleExternalTeamTimelineV2Error(
                                "admitted stream has more encounters than reconstruction"
                            )
                        entry = _mapping(
                            encounter_entries[next_encounter_index],
                            label="reconstruction encounter entry",
                        )
                        spec = _load_encounter_spec(context, entry)
                        if (
                            spec.encounter_id != encounter_id
                            or spec.encounter_ordinal != order[0]
                            or spec.encounter_ordinal != next_encounter_index
                        ):
                            raise ChronicleExternalTeamTimelineV2Error(
                                "admitted encounter order differs from reconstruction"
                            )
                        active = EncounterBuilder(spec=spec, context=context)
                    active.observe(row, order)
                if active is not None:
                    write_finished(active, compressed)
            raw.flush()
            os.fsync(raw.fileno())
        if next_encounter_index != len(encounter_entries):
            raise ChronicleExternalTeamTimelineV2Error(
                "reconstruction has encounters absent from admitted stream"
            )
        logical_sha = logical_digest.hexdigest()
        final = output_directory / f"{context.instance_id}.{logical_sha}.jsonl.gz"
        compressed_sha = _sha256_file(temporary)
        compressed_size = temporary.stat().st_size

        source_row_count = sum(
            _nonnegative_integer(
                item.get("source_row_count"), label="encounter source_row_count"
            )
            for item in encounter_summaries
        )
        assigned_source_row_count = sum(
            _nonnegative_integer(
                item.get("assigned_source_row_count"),
                label="encounter assigned_source_row_count",
            )
            for item in encounter_summaries
        )
        outside_wave_row_count = sum(
            _nonnegative_integer(
                item.get("outside_wave_row_count"),
                label="encounter outside_wave_row_count",
            )
            for item in encounter_summaries
        )
        reconstruction_summary = _mapping(
            context.reconstruction_instance.get("summary"),
            label="reconstruction instance summary",
        )
        if (
            source_row_count != reconstruction_summary.get("source_row_count")
            or len(encounter_summaries)
            != reconstruction_summary.get("encounter_count")
            or wave_record_count != reconstruction_summary.get("wave_count")
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "timeline partition counts differ from reconstruction"
            )
        if source_row_count != assigned_source_row_count + outside_wave_row_count:
            raise ChronicleExternalTeamTimelineV2Error(
                "instance source-row accounting is not conserved"
            )
        wave_totals = _summary_totals(
            [
                {"summary": summary}
                for summary in wave_summaries
            ]
        )
        if assigned_source_row_count != (
            wave_totals["nonclassification_event_count"]
            + wave_totals["classification_transition_count"]
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "assigned source rows differ from emitted wave rows"
            )
        roster = _mapping(
            context.admission_instance.get("metadata_player_resolver"),
            label="metadata_player_resolver",
        )
        temporal = deepcopy(
            dict(
                _mapping(
                    context.admission_instance.get(
                        "temporal_and_guild_provenance"
                    ),
                    label="temporal_and_guild_provenance",
                )
            )
        )
        spec_evidence = deepcopy(
            dict(
                _mapping(
                    context.admission_instance.get("warrior_spec_evidence"),
                    label="warrior_spec_evidence",
                )
            )
        )
        spec_counts = _mapping(
            spec_evidence.get("recomputed_counts"),
            label="warrior_spec_evidence.recomputed_counts",
        )
        summary = {
            "encounter_count": len(encounter_summaries),
            "wave_count": wave_record_count,
            "source_row_count": source_row_count,
            "assigned_source_row_count": assigned_source_row_count,
            "outside_wave_row_count": outside_wave_row_count,
            "roster_player_count": _nonnegative_integer(
                roster.get("player_count"), label="metadata roster player_count"
            ),
            "warrior_spec_observation_count": _nonnegative_integer(
                spec_evidence.get("observation_count"),
                label="warrior spec observation_count",
            ),
            "warrior_fury_observation_count": _nonnegative_integer(
                spec_counts.get("Fury"), label="warrior Fury count"
            ),
            "warrior_arms_observation_count": _nonnegative_integer(
                spec_counts.get("Arms"), label="warrior Arms count"
            ),
            "warrior_other_unknown_observation_count": _nonnegative_integer(
                spec_counts.get("Other_or_unknown"),
                label="warrior Other_or_unknown count",
            ),
            "warrior_spec_conflict_observation_count": _nonnegative_integer(
                spec_evidence.get("field_conflict_observation_count"),
                label="warrior spec conflict count",
            ),
            **wave_totals,
            "raw_row_copy_count": 0,
            "normalized_row_copy_count": 0,
            "network_request_count": 0,
        }
        entry_core = {
            "instance_id": context.instance_id,
            "status": STATUS,
            "source_binding": deepcopy(dict(context.source_binding)),
            "instance_provenance": {
                "temporal_and_guild_provenance": temporal,
                "warrior_spec_evidence": spec_evidence,
                "metadata_player_resolver": {
                    "kind": roster.get("kind"),
                    "player_count": roster.get("player_count"),
                    "players_sha256": roster.get("players_sha256"),
                    "name_or_class_inference_used": roster.get(
                        "name_or_class_inference_used"
                    ),
                },
            },
            "partition": {
                "path": final.name,
                "logical_content_sha256": logical_sha,
                "logical_size_bytes": logical_size,
                "compressed_file_sha256": compressed_sha,
                "compressed_size_bytes": compressed_size,
                "record_count": wave_record_count,
                "record_schema": PARTITION_RECORD_SCHEMA,
                "gzip_mtime": 0,
            },
            "summary": summary,
            "encounters": encounter_summaries,
        }
        entry = _content_addressed(entry_core)
        return PartitionBuild(
            temporary_path=temporary,
            final_path=final,
            compressed_file_sha256=compressed_sha,
            manifest_entry=entry,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _build_instance_partitions_parallel(
    contexts: Sequence[InputContext],
    *,
    output_directory: Path,
    workers: int,
) -> list[PartitionBuild]:
    """Build independent instance partitions without changing result order.

    Results are placed in the already-validated, instance-id-sorted context
    order.  Worker scheduling and completion order therefore cannot enter a
    partition or manifest content address.
    """

    results: list[PartitionBuild | None] = [None] * len(contexts)
    failure: BaseException | None = None
    effective_workers = min(workers, len(contexts))
    worker_contexts = tuple(_partition_input_context(context) for context in contexts)
    with ProcessPoolExecutor(
        max_workers=effective_workers,
    ) as executor:
        futures = {
            executor.submit(
                _build_instance_partition,
                worker_contexts[index],
                output_directory=output_directory,
            ): index
            for index in range(len(contexts))
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except BaseException as error:
                if failure is None:
                    failure = error
                for pending in futures:
                    if pending is not future:
                        pending.cancel()
    if failure is not None:
        for result in results:
            if result is not None:
                result.temporary_path.unlink(missing_ok=True)
        raise failure
    if any(result is None for result in results):  # pragma: no cover - defensive
        for result in results:
            if result is not None:
                result.temporary_path.unlink(missing_ok=True)
        raise ChronicleExternalTeamTimelineV2Error(
            "parallel partition build ended without every instance result"
        )
    return [result for result in results if result is not None]


def _write_temporary(path: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_immutable(temporary: Path, final: Path, expected_sha256: str) -> None:
    if final.exists():
        if _sha256_file(final) != expected_sha256:
            raise ChronicleExternalTeamTimelineV2Error(
                f"immutable content-addressed output differs: {final}"
            )
        temporary.unlink()
    else:
        temporary.replace(final)


def _manifest_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    sum_keys = (
        "encounter_count",
        "wave_count",
        "source_row_count",
        "assigned_source_row_count",
        "outside_wave_row_count",
        "roster_player_count",
        "warrior_spec_observation_count",
        "warrior_fury_observation_count",
        "warrior_arms_observation_count",
        "warrior_other_unknown_observation_count",
        "warrior_spec_conflict_observation_count",
        "exact_player_event_count",
        "unattributed_event_count",
        "death_marker_count",
        "negative_damage_diagnostic_count",
        "negative_damage_absolute_amount_excluded",
        "classification_transition_count",
        "nonclassification_event_count",
        "damage_amount",
        "player_damage_amount",
        "unattributed_damage_amount",
        "healing_amount",
        "player_healing_amount",
        "unattributed_healing_amount",
        "hostile_object_source_attributed_event_count",
        "hostile_object_target_event_count",
        "hostile_player_source_event_count",
        "hostile_player_target_event_count",
        "raw_row_copy_count",
        "normalized_row_copy_count",
        "network_request_count",
    )
    result = {
        key: sum(
            _nonnegative_integer(
                _mapping(entry.get("summary"), label="instance.summary").get(key),
                label=f"instance.summary.{key}",
            )
            for entry in entries
        )
        for key in sum_keys
    }
    result["negative_damage_signed_amount_excluded"] = sum(
        _integer(
            _mapping(entry.get("summary"), label="instance.summary").get(
                "negative_damage_signed_amount_excluded"
            ),
            label="instance.summary.negative_damage_signed_amount_excluded",
        )
        for entry in entries
    )
    result["instance_count"] = len(entries)
    result["compressed_partition_bytes"] = sum(
        _nonnegative_integer(
            _mapping(entry.get("partition"), label="instance.partition").get(
                "compressed_size_bytes"
            ),
            label="partition.compressed_size_bytes",
        )
        for entry in entries
    )
    return result


def build_external_team_timeline(
    *,
    normalization_manifest_path: str | Path = DEFAULT_NORMALIZATION_MANIFEST,
    admission_manifest_path: str | Path = DEFAULT_ADMISSION_MANIFEST,
    reconstruction_manifest_path: str | Path = DEFAULT_RECONSTRUCTION_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
) -> dict[str, Any]:
    """Build deterministic per-instance External team-timeline partitions."""

    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ChronicleExternalTeamTimelineV2Error(
            f"workers must be an integer from 1 through {MAX_WORKERS}"
        )

    contexts = _validate_inputs(
        normalization_manifest_path=normalization_manifest_path,
        admission_manifest_path=admission_manifest_path,
        reconstruction_manifest_path=reconstruction_manifest_path,
    )
    data_root = contexts[0].data_root
    output = _under(
        Path(output_directory), data_root, label="team timeline output directory"
    )
    output.mkdir(parents=True, exist_ok=True)
    builds: list[PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        if workers == 1 or len(contexts) == 1:
            for context in contexts:
                builds.append(
                    _build_instance_partition(context, output_directory=output)
                )
        else:
            builds = _build_instance_partitions_parallel(
                contexts,
                output_directory=output,
                workers=workers,
            )
        entries = [deepcopy(dict(build.manifest_entry)) for build in builds]
        first_binding = _mapping(
            entries[0].get("source_binding"), label="instance source_binding"
        )
        manifest_core = {
            "schema": SCHEMA,
            "kind": KIND,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "status": STATUS,
            "input_closure": {
                key: deepcopy(first_binding[key])
                for key in (
                    "normalization_manifest",
                    "admission_manifest",
                    "reconstruction_manifest",
                    "raw_api_manifest",
                    "classification_resolver",
                )
            },
            "instance_order": [entry["instance_id"] for entry in entries],
            "streaming_contract": {
                "instances_sorted_by_instance_id": True,
                "one_admitted_partition_verified_then_streamed_per_instance": True,
                "bounded_instance_parallelism_max_workers": MAX_WORKERS,
                "requested_worker_count_enters_content_identity": False,
                "worker_completion_order_enters_result_order": False,
                "memory_scope": "one encounter and its reconstruction waves",
                "normalized_events_materialized_as_full_instance_list": False,
                "event_order": [
                    "encounter_ordinal",
                    "timestamp_ms",
                    "EventMeta.index",
                    "fixed_stream_tiebreaker",
                    "frame_message_index",
                ],
                "wave_join": (
                    "inclusive reconstruction first_anchor through "
                    "last_context_anchor in exact EventMeta order"
                ),
            },
            "attribution_contract": {
                "direct_player": (
                    "exact metadata GUID and current official FriendlyPlayer state"
                ),
                "owned_or_controlled_entity": (
                    "official full GUID chain only; controller before owner; exact "
                    "metadata GUID endpoint currently FriendlyPlayer"
                ),
                "relation_chain_max_depth": 5,
                "future_classification_backfill_allowed": False,
                "name_identity_inference_allowed": False,
                "guid_suffix_identity_inference_allowed": False,
                "hostile_player_is_friendly_player": False,
            },
            "player_metadata_and_spec_contract": {
                "class_and_race_source": "admission exact metadata player GUID resolver",
                "warrior_spec_source": "admission warrior_spec_evidence exact player_guid observation",
                "player_name_used_for_spec": False,
                "spell_sequence_used_for_spec": False,
                "fury_or_arms_voting_requires_observed_conflict_free_label": True,
                "unknown_or_conflicting_spec_is_nonvoting": True,
            },
            "event_contract": {
                "action_types": sorted(ACTION_EVENT_TYPES),
                "damage_amount_source": "DMG_ONLY",
                "negative_damage_diagnostic_lane": (
                    NEGATIVE_DAMAGE_DIAGNOSTIC_LANE
                ),
                "negative_damage_value_policy": NEGATIVE_DAMAGE_VALUE_POLICY,
                "negative_damage_enters_canonical_damage_or_reward": False,
                "negative_damage_abs_or_clamp_allowed": False,
                "healing_amount_source": "HEAL_ONLY",
                "slain_use": "DEATH_MARKER_ONLY",
                "slain_nested_attribution_damage_added": 0,
                "hostile_object_target_lane": "PRESERVED_NONVOTING",
                "hostile_player_target_lane": "PRESERVED_NONVOTING",
                "unknown_target_lane": "PRESERVED_NONVOTING",
            },
            "contamination_contract": {
                "scope": "raid instance",
                "time_field": "started_at",
                "uploaded_at_used": False,
                "player_name_used": False,
                "value_copied_from_verified_admission": True,
                "rules_recomputed_by_timeline": False,
                "legacy_single_cutoff_compatibility_alias": (
                    CONTAMINATION_CUTOFF
                ),
                "boundary_policy": range_bug_boundary_contract(),
                "rules": {
                    "guild_is_nanbei_and_started_before_fix_date": (
                        "SUSPECT_36YD_RANGE_BUG"
                    ),
                    "guild_is_nanbei_and_started_in_fix_morning_boundary": (
                        RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING
                    ),
                    "guild_is_nanbei_and_started_at_or_after_noon_safe_floor": (
                        "POSTFIX_KNOWN_CLEAN"
                    ),
                    "other_guild_with_complete_evidence": (
                        "NO_KNOWN_RULE_MATCH"
                    ),
                    "missing_guild_or_started_at_evidence": "UNKNOWN_NONVOTING",
                },
                "named_player_blacklist_or_weighting_allowed": False,
            },
            "publication_contract": {
                "partition_per_instance": True,
                "partition_and_wave_and_player_records_content_addressed": True,
                "gzip_mtime": 0,
                "content_addressed_manifest_published_before_stable_pointer": True,
                "stable_manifest_committed_last": True,
                "stable_and_addressed_manifest_bytes_equal": True,
                "raw_or_normalized_rows_copied": False,
            },
            "scientific_boundaries": {
                "team_model_built": False,
                "comparison_authorized": False,
                "policy_training_authorized": False,
                "legacy_50_timeline_or_model_modified": False,
                "capsule_membership_claimed": False,
                "status_is_timeline_evidence_only": True,
            },
            "summary": _manifest_summary(entries),
            "instances": entries,
        }
        manifest = _content_addressed(manifest_core)
        content_sha = _verify_content_address(manifest, label="timeline manifest")
        payload = _canonical_bytes(manifest) + b"\n"
        file_sha = _sha256_bytes(payload)
        addressed = output / (
            f"chronicle_external_team_timeline_v2.{content_sha}.manifest.json"
        )
        stable = output / "manifest.json"
        addressed_temporary = _write_temporary(addressed, payload)
        stable_temporary = _write_temporary(stable, payload)

        # Content-addressed partitions may become harmless orphans after a
        # later failure; the stable manifest remains the sole commit mark.
        for build in builds:
            _publish_immutable(
                build.temporary_path,
                build.final_path,
                build.compressed_file_sha256,
            )
        _publish_immutable(addressed_temporary, addressed, file_sha)
        addressed_temporary = None
        stable_temporary.replace(stable)
        stable_temporary = None
        return {
            "status": STATUS,
            "schema": SCHEMA,
            "manifest_path": str(stable),
            "content_addressed_manifest_path": str(addressed),
            "content_sha256": content_sha,
            "manifest_file_sha256": file_sha,
            "summary": manifest["summary"],
            "partitions": [str(build.final_path) for build in builds],
            "team_model_built": False,
            "comparison_authorized": False,
            "network_request_count": 0,
            "workers_requested": workers,
            "workers_used": min(workers, len(contexts)),
        }
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)


def _verify_wave_record(
    record: Mapping[str, Any], *, source_binding_sha256: str
) -> Mapping[str, Any]:
    if (
        record.get("schema") != PARTITION_RECORD_SCHEMA
        or record.get("implementation_revision") != IMPLEMENTATION_REVISION
        or record.get("status") != STATUS
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "unsupported team-timeline wave record"
        )
    _verify_content_address(record, label="team-timeline wave")
    if record.get("source_binding_sha256") != source_binding_sha256:
        raise ChronicleExternalTeamTimelineV2Error(
            "wave source binding differs from instance manifest entry"
        )
    encounter_ordinal = _nonnegative_integer(
        record.get("encounter_ordinal"), label="wave encounter_ordinal"
    )
    player_events = 0
    player_damage = 0
    player_healing = 0
    for raw_player in _array(record.get("players"), label="wave.players"):
        player = _mapping(raw_player, label="wave player")
        _verify_content_address(player, label="wave player")
        player_metadata = _mapping(player.get("player"), label="player metadata")
        player_guid = canonical_guid(
            player_metadata.get("guid"), label="wave player GUID"
        )
        _text(player_metadata.get("class"), label="player metadata class")
        _text(player_metadata.get("race"), label="player metadata race")
        spec_evidence = _mapping(
            player.get("warrior_spec_evidence"),
            label="player warrior_spec_evidence",
        )
        if spec_evidence.get("inference_used") is not False:
            raise ChronicleExternalTeamTimelineV2Error(
                "player warrior spec evidence used inference"
            )
        conflicts = spec_evidence.get("field_conflicts")
        has_conflict = isinstance(conflicts, Mapping) and bool(conflicts)
        observed_vote = (
            spec_evidence.get("status") == "OBSERVED"
            and spec_evidence.get("player_spec") in {"Fury", "Arms"}
            and not has_conflict
        )
        if spec_evidence.get("voting_for_fury_or_arms_lane") is not observed_vote:
            raise ChronicleExternalTeamTimelineV2Error(
                "player warrior spec voting boundary is inconsistent"
            )
        timeline = [
            _mapping(item, label="player timeline event")
            for item in _array(player.get("timeline"), label="player.timeline")
        ]
        _assert_strict_timeline_order(
            timeline,
            encounter_ordinal=encounter_ordinal,
            label=f"loaded player {player_guid} timeline",
        )
        for event in timeline:
            attribution = _mapping(
                event.get("attribution"), label="player event attribution"
            )
            if attribution.get("player_guid") != player_guid:
                raise ChronicleExternalTeamTimelineV2Error(
                    "player timeline event attribution GUID mismatch"
                )
            source = _mapping(event.get("source"), label="player event source")
            if source.get("lane") == reconstruction_v2.LANE_HOSTILE_PLAYER:
                raise ChronicleExternalTeamTimelineV2Error(
                    "HostilePlayer event appears in a friendly player timeline"
                )
            target = _mapping(event.get("target"), label="player event target")
            if (
                target.get("lane") == reconstruction_v2.LANE_HOSTILE_OBJECT
                and target.get("voting_enemy_target") is not False
            ):
                raise ChronicleExternalTeamTimelineV2Error(
                    "loaded HostileObject target is voting"
                )
            if event.get("event_type") == "DMG":
                player_damage += _nonnegative_integer(
                    _mapping(event.get("damage"), label="event.damage").get(
                        "amount"
                    ),
                    label="event.damage.amount",
                )
            elif event.get("event_type") == "HEAL":
                player_healing += _nonnegative_integer(
                    _mapping(event.get("healing"), label="event.healing").get(
                        "amount"
                    ),
                    label="event.healing.amount",
                )
            elif event.get("event_type") == "DEAD":
                raise ChronicleExternalTeamTimelineV2Error(
                    "DEAD marker appears in a player amount timeline"
                )
        player_events += len(timeline)

    unattributed = _mapping(
        record.get("unattributed_lane"), label="unattributed_lane"
    )
    unattributed_timeline = [
        _mapping(item, label="unattributed event")
        for item in _array(
            unattributed.get("timeline"), label="unattributed_lane.timeline"
        )
    ]
    _assert_strict_timeline_order(
        unattributed_timeline,
        encounter_ordinal=encounter_ordinal,
        label="loaded unattributed timeline",
    )
    unattributed_damage = 0
    unattributed_healing = 0
    for event in unattributed_timeline:
        attribution = _mapping(
            event.get("attribution"), label="unattributed event attribution"
        )
        if attribution.get("player_guid") is not None:
            raise ChronicleExternalTeamTimelineV2Error(
                "unattributed event contains a player GUID"
            )
        target = _mapping(event.get("target"), label="unattributed event target")
        if (
            target.get("lane") == reconstruction_v2.LANE_HOSTILE_OBJECT
            and target.get("voting_enemy_target") is not False
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "unattributed HostileObject target is voting"
            )
        if event.get("event_type") == "DMG":
            unattributed_damage += _nonnegative_integer(
                _mapping(event.get("damage"), label="event.damage").get("amount"),
                label="event.damage.amount",
            )
        elif event.get("event_type") == "HEAL":
            unattributed_healing += _nonnegative_integer(
                _mapping(event.get("healing"), label="event.healing").get(
                    "amount"
                ),
                label="event.healing.amount",
            )
        elif event.get("event_type") == "DEAD":
            raise ChronicleExternalTeamTimelineV2Error(
                "DEAD marker appears in the unattributed amount timeline"
            )

    death_markers = [
        _mapping(item, label="death marker")
        for item in _array(record.get("death_markers"), label="death_markers")
    ]
    _assert_strict_timeline_order(
        death_markers,
        encounter_ordinal=encounter_ordinal,
        label="loaded death marker timeline",
    )
    for marker in death_markers:
        death = _mapping(marker.get("death"), label="death marker details")
        if (
            marker.get("event_type") != "DEAD"
            or death.get("marker_only") is not True
            or death.get("damage_amount_added") != 0
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "slain marker violates marker-only accounting"
            )

    negative_damage_diagnostics = [
        _mapping(item, label="negative damage diagnostic")
        for item in _array(
            record.get("negative_damage_diagnostics"),
            label="negative_damage_diagnostics",
        )
    ]
    _assert_strict_timeline_order(
        negative_damage_diagnostics,
        encounter_ordinal=encounter_ordinal,
        label="loaded negative damage diagnostic timeline",
    )
    negative_signed_amount = 0
    negative_absolute_amount = 0
    for event in negative_damage_diagnostics:
        diagnostic = _mapping(
            event.get("negative_damage_diagnostic"),
            label="negative damage diagnostic details",
        )
        signed = _integer(
            diagnostic.get("signed_amount"),
            label="negative damage diagnostic signed_amount",
        )
        if (
            event.get("event_type") != "DMG"
            or "damage" in event
            or signed >= 0
            or diagnostic.get("absolute_magnitude") != abs(signed)
            or diagnostic.get("lane") != NEGATIVE_DAMAGE_DIAGNOSTIC_LANE
            or diagnostic.get("policy") != NEGATIVE_DAMAGE_VALUE_POLICY
            or diagnostic.get("damage_amount_added") != 0
            or diagnostic.get("reward_amount_added") != 0
        ):
            raise ChronicleExternalTeamTimelineV2Error(
                "signed negative DMG diagnostic violates its nonvoting contract"
            )
        negative_signed_amount += signed
        negative_absolute_amount += abs(signed)

    summary = _mapping(record.get("summary"), label="wave.summary")
    expected = {
        "exact_player_event_count": player_events,
        "unattributed_event_count": len(unattributed_timeline),
        "death_marker_count": len(death_markers),
        "negative_damage_diagnostic_count": len(negative_damage_diagnostics),
        "negative_damage_signed_amount_excluded": negative_signed_amount,
        "negative_damage_absolute_amount_excluded": negative_absolute_amount,
        "damage_amount": player_damage + unattributed_damage,
        "player_damage_amount": player_damage,
        "unattributed_damage_amount": unattributed_damage,
        "healing_amount": player_healing + unattributed_healing,
        "player_healing_amount": player_healing,
        "unattributed_healing_amount": unattributed_healing,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise ChronicleExternalTeamTimelineV2Error(
                f"loaded wave summary differs from timelines for {key}"
            )
    if summary.get("classification_transition_count") != len(
        _array(
            record.get("classification_transitions_inside_window"),
            label="classification transitions",
        )
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "wave classification transition count mismatch"
        )
    if summary.get("nonclassification_event_count") != (
        player_events
        + len(unattributed_timeline)
        + len(death_markers)
        + len(negative_damage_diagnostics)
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "wave nonclassification event accounting mismatch"
        )
    accounting = _mapping(record.get("event_accounting"), label="event_accounting")
    exclusive = _mapping(accounting.get("exclusive_lanes"), label="exclusive_lanes")
    expected_exclusive = {
        "exact_player_event_count": player_events,
        "unattributed_event_count": len(unattributed_timeline),
        "death_marker_count": len(death_markers),
        "negative_damage_diagnostic_count": len(negative_damage_diagnostics),
    }
    if any(exclusive.get(key) != value for key, value in expected_exclusive.items()):
        raise ChronicleExternalTeamTimelineV2Error(
            "wave exclusive-lane accounting differs from emitted timelines"
        )
    damage_accounting = _mapping(accounting.get("damage"), label="accounting.damage")
    if (
        damage_accounting.get("amount") != player_damage + unattributed_damage
        or damage_accounting.get("negative_event_count_excluded")
        != len(negative_damage_diagnostics)
        or damage_accounting.get("negative_signed_amount_excluded")
        != negative_signed_amount
        or damage_accounting.get("absolute_amount_excluded")
        != negative_absolute_amount
        or damage_accounting.get("policy") != NEGATIVE_DAMAGE_VALUE_POLICY
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "wave negative-DMG accounting differs from diagnostic lane"
        )
    return summary


def load_external_team_timeline_manifest(
    path: str | Path,
    *,
    verify_inputs: bool = True,
    verify_partitions: bool = True,
) -> tuple[dict[str, Any], Path]:
    """Load a timeline manifest and stream-verify every referenced partition."""

    resolved = Path(path).expanduser().resolve()
    manifest, _ = _load_json_bytes(resolved, label="team timeline manifest")
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("kind") != KIND
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
        or manifest.get("status") != STATUS
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "unsupported team timeline manifest"
        )
    content_sha = _verify_content_address(manifest, label="team timeline manifest")
    _, _, payload = _stable_and_addressed(
        resolved,
        addressed_name=f"chronicle_external_team_timeline_v2.{content_sha}.manifest.json",
        label="team timeline",
    )
    data_root = _data_root_for(resolved)
    contexts_by_instance: dict[str, InputContext] = {}
    if verify_inputs:
        closure = _mapping(manifest.get("input_closure"), label="input_closure")
        resolved_inputs = {}
        for key in (
            "normalization_manifest",
            "admission_manifest",
            "reconstruction_manifest",
        ):
            binding = _mapping(closure.get(key), label=f"input_closure.{key}")
            relative = Path(_text(binding.get("stable_path"), label="stable_path"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ChronicleExternalTeamTimelineV2Error(
                    "input closure path escapes offline_data"
                )
            resolved_inputs[key] = _under(
                data_root / relative, data_root, label=f"input closure {key}"
            )
        contexts = _validate_inputs(
            normalization_manifest_path=resolved_inputs["normalization_manifest"],
            admission_manifest_path=resolved_inputs["admission_manifest"],
            reconstruction_manifest_path=resolved_inputs[
                "reconstruction_manifest"
            ],
        )
        contexts_by_instance = {context.instance_id: context for context in contexts}

    raw_entries = _array(manifest.get("instances"), label="manifest.instances")
    instance_entries = [
        _mapping(raw, label="manifest instance") for raw in raw_entries
    ]
    instance_ids = [
        _text(entry.get("instance_id"), label="manifest instance_id")
        for entry in instance_entries
    ]
    if instance_ids != sorted(instance_ids) or len(instance_ids) != len(
        set(instance_ids)
    ):
        raise ChronicleExternalTeamTimelineV2Error(
            "manifest instances are not unique and sorted"
        )
    if manifest.get("instance_order") != instance_ids:
        raise ChronicleExternalTeamTimelineV2Error(
            "manifest instance_order differs from instance entries"
        )
    if verify_inputs and set(instance_ids) != set(contexts_by_instance):
        raise ChronicleExternalTeamTimelineV2Error(
            "timeline and input closure instance sets differ"
        )

    if verify_partitions:
        output = resolved.parent
        for entry in instance_entries:
            _verify_content_address(entry, label="timeline instance entry")
            instance_id = _text(entry.get("instance_id"), label="instance_id")
            source_binding = _mapping(
                entry.get("source_binding"), label="instance source_binding"
            )
            if verify_inputs and source_binding != contexts_by_instance[
                instance_id
            ].source_binding:
                raise ChronicleExternalTeamTimelineV2Error(
                    f"instance source binding changed for {instance_id}"
                )
            source_binding_sha = _sha256_bytes(_canonical_bytes(source_binding))
            partition = _mapping(entry.get("partition"), label="instance.partition")
            relative = Path(_text(partition.get("path"), label="partition.path"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ChronicleExternalTeamTimelineV2Error(
                    "timeline partition path escapes output directory"
                )
            partition_path = _under(
                output / relative, output, label="timeline partition"
            )
            expected_size = _nonnegative_integer(
                partition.get("compressed_size_bytes"),
                label="partition.compressed_size_bytes",
            )
            expected_sha = _sha(
                partition.get("compressed_file_sha256"),
                label="partition.compressed_file_sha256",
            )
            try:
                if partition_path.stat().st_size != expected_size:
                    raise ChronicleExternalTeamTimelineV2Error(
                        "timeline partition compressed size mismatch"
                    )
                if _sha256_file(partition_path) != expected_sha:
                    raise ChronicleExternalTeamTimelineV2Error(
                        "timeline partition compressed hash mismatch"
                    )
                logical_digest = hashlib.sha256()
                logical_size = 0
                record_count = 0
                totals: Counter[str] = Counter()
                encounter_wave_hashes: dict[int, list[str]] = {}
                prior_identity: tuple[int, int] | None = None
                with gzip.open(partition_path, "rb") as handle:
                    for line_number, line in enumerate(handle, 1):
                        logical_digest.update(line)
                        logical_size += len(line)
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as error:
                            raise ChronicleExternalTeamTimelineV2Error(
                                f"invalid timeline JSONL row {line_number}: {error}"
                            ) from error
                        if not isinstance(record, dict):
                            raise ChronicleExternalTeamTimelineV2Error(
                                "timeline JSONL row is not an object"
                            )
                        if record.get("instance_id") != instance_id:
                            raise ChronicleExternalTeamTimelineV2Error(
                                "timeline wave instance mismatch"
                            )
                        summary = _verify_wave_record(
                            record, source_binding_sha256=source_binding_sha
                        )
                        encounter_ordinal = _nonnegative_integer(
                            record.get("encounter_ordinal"),
                            label="wave encounter_ordinal",
                        )
                        wave_ordinal = _nonnegative_integer(
                            record.get("wave_ordinal"), label="wave_ordinal"
                        )
                        identity = (encounter_ordinal, wave_ordinal)
                        if prior_identity is not None and identity <= prior_identity:
                            raise ChronicleExternalTeamTimelineV2Error(
                                "timeline wave records are not strictly ordered"
                            )
                        prior_identity = identity
                        encounter_wave_hashes.setdefault(
                            encounter_ordinal, []
                        ).append(record["content_address"]["sha256"])
                        for key, value in summary.items():
                            if isinstance(value, int) and not isinstance(value, bool):
                                totals[key] += value
                        record_count += 1
            except (OSError, EOFError, UnicodeDecodeError, gzip.BadGzipFile) as error:
                raise ChronicleExternalTeamTimelineV2Error(
                    f"cannot verify timeline partition {partition_path}: {error}"
                ) from error
            if logical_digest.hexdigest() != _sha(
                partition.get("logical_content_sha256"),
                label="partition.logical_content_sha256",
            ):
                raise ChronicleExternalTeamTimelineV2Error(
                    "timeline partition logical hash mismatch"
                )
            if logical_size != _nonnegative_integer(
                partition.get("logical_size_bytes"),
                label="partition.logical_size_bytes",
            ):
                raise ChronicleExternalTeamTimelineV2Error(
                    "timeline partition logical size mismatch"
                )
            if record_count != _nonnegative_integer(
                partition.get("record_count"), label="partition.record_count"
            ):
                raise ChronicleExternalTeamTimelineV2Error(
                    "timeline partition record count mismatch"
                )

            encounter_entries = [
                _mapping(item, label="timeline encounter summary")
                for item in _array(entry.get("encounters"), label="instance.encounters")
            ]
            for index, encounter in enumerate(encounter_entries):
                _verify_content_address(encounter, label="timeline encounter summary")
                if encounter.get("encounter_ordinal") != index:
                    raise ChronicleExternalTeamTimelineV2Error(
                        "timeline encounter summaries are not contiguous"
                    )
                if encounter.get("wave_content_sha256") != encounter_wave_hashes.get(
                    index, []
                ):
                    raise ChronicleExternalTeamTimelineV2Error(
                        "encounter wave hashes differ from partition rows"
                    )
            declared_summary = _mapping(
                entry.get("summary"), label="instance.summary"
            )
            if declared_summary.get("wave_count") != record_count:
                raise ChronicleExternalTeamTimelineV2Error(
                    "instance wave count differs from partition"
                )
            for key in _summary_totals(
                [{"summary": dict(totals)}]
            ):
                if declared_summary.get(key) != totals.get(key, 0):
                    raise ChronicleExternalTeamTimelineV2Error(
                        f"instance summary differs from wave records for {key}"
                    )
        if manifest.get("summary") != _manifest_summary(instance_entries):
            raise ChronicleExternalTeamTimelineV2Error(
                "timeline manifest summary differs from instance entries"
            )
    if _sha256_bytes(payload) != _sha256_file(resolved):
        raise ChronicleExternalTeamTimelineV2Error(
            "timeline manifest bytes changed while loading"
        )
    return manifest, resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build External API per-player team timelines V2"
    )
    parser.add_argument(
        "--normalization-manifest",
        type=Path,
        default=DEFAULT_NORMALIZATION_MANIFEST,
    )
    parser.add_argument(
        "--admission-manifest", type=Path, default=DEFAULT_ADMISSION_MANIFEST
    )
    parser.add_argument(
        "--reconstruction-manifest",
        type=Path,
        default=DEFAULT_RECONSTRUCTION_MANIFEST,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=f"parallel instance partitions (1-{MAX_WORKERS}; default: 1)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_external_team_timeline(
            normalization_manifest_path=args.normalization_manifest,
            admission_manifest_path=args.admission_manifest,
            reconstruction_manifest_path=args.reconstruction_manifest,
            output_directory=args.output_dir,
            workers=args.workers,
        )
    except ChronicleExternalTeamTimelineV2Error as error:
        print(f"Chronicle External team timeline V2 failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ChronicleExternalTeamTimelineV2Error",
    "DEFAULT_ADMISSION_MANIFEST",
    "DEFAULT_NORMALIZATION_MANIFEST",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_RECONSTRUCTION_MANIFEST",
    "IMPLEMENTATION_REVISION",
    "MAX_WORKERS",
    "PARTITION_RECORD_SCHEMA",
    "SCHEMA",
    "STATUS",
    "build_external_team_timeline",
    "load_external_team_timeline_manifest",
    "main",
]
