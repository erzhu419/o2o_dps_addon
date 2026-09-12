"""Compile External Chronicle team timelines into prefix-causal wave evidence.

This is a versioned consumer of ``chronicle_external_team_timeline/v2``.  It
does not reuse or impersonate the manual-CSV V1 model.  Each input wave is
streamed independently, flattened back into exact EventMeta order, and emitted
with per-player strict-prefix transitions and leave-one-player-out background
features.  The exact trace remains descriptive and nonvoting; a later explicit
adapter is required before any historical policy can consume it.

Only DMG contributes damage.  DEAD is a marker which can affect later prefix
state, but it never contributes an amount.  No final wave total, future death,
or later classification is allowed into ``state_before``.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
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
from typing import Any, Iterable, Mapping, Sequence

from . import chronicle_external_api_manifest_union_v1 as union_v1
from .chronicle_external_event_normalizer_v1 import STREAM_ORDER
from .chronicle_external_team_timeline_v2 import (
    CONTAMINATION_CUTOFF,
    CONTAMINATION_LABELS,
    CONTAMINATION_POSTFIX_AT_OR_AFTER,
    CONTAMINATION_SUSPECT_BEFORE,
    IMPLEMENTATION_REVISION as TIMELINE_IMPLEMENTATION_REVISION,
    NEGATIVE_DAMAGE_DIAGNOSTIC_LANE,
    NEGATIVE_DAMAGE_VALUE_POLICY,
    PARTITION_RECORD_SCHEMA as TIMELINE_RECORD_SCHEMA,
    SCHEMA as TIMELINE_SCHEMA,
    STATUS as TIMELINE_STATUS,
    ChronicleExternalTeamTimelineV2Error,
    load_external_team_timeline_manifest,
)


SCHEMA = "chronicle_external_team_wave_model/v2"
PARTITION_RECORD_SCHEMA = "chronicle_external_team_wave_model_wave/v2"
KIND = "chronicle_external_team_wave_model_manifest"
STATUS = "DESCRIPTIVE_NONVOTING_NOT_COMPARISON"
IMPLEMENTATION_REVISION = (
    "v2.5_external_action_prefix_range_bug_boundary_20260903_noon_receipt_cohort_loo"
)
MAX_WORKERS = 32

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMELINE_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_timeline"
    / "v2"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_wave_model"
    / "v2"
)

PLAYER_ATTRIBUTION_KINDS = frozenset(
    {
        "DIRECT_FRIENDLY_PLAYER",
        "EXACT_OFFICIAL_OWNER",
        "EXACT_OFFICIAL_CONTROLLER",
    }
)
ALL_ATTRIBUTION_KINDS = tuple(sorted(PLAYER_ATTRIBUTION_KINDS | {"UNATTRIBUTED"}))
ACTION_EVENT_TYPES = frozenset({"START", "GO", "FAIL"})
AMOUNT_EVENT_TYPES = frozenset({"DMG", "HEAL"})
DEFAULT_CANDIDATE_CONTAMINATION_LABELS = frozenset(
    {"POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BANNED_PREFIX_KEYS = frozenset(
    {
        "death_markers",
        "descriptive_outcome",
        "event_accounting",
        "final_totals",
        "future_events",
        "reconstruction_binding",
        "summary",
        "target_summaries",
        "wave_duration_ms",
        "wave_end_ms",
        "wave_summary",
    }
)


class ChronicleExternalTeamWaveModelV2Error(RuntimeError):
    """An input or output violates the External team-wave V2 contract."""


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
        raise ChronicleExternalTeamWaveModelV2Error(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalTeamWaveModelV2Error(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalTeamWaveModelV2Error(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} must be nonempty text"
        )
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalTeamWaveModelV2Error(f"{label} must be an integer")
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    result = _integer(value, label=label)
    if result < 0:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} must be nonnegative"
        )
    return result


def _sha(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if _SHA256_RE.fullmatch(rendered) is None:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} must be a lowercase SHA-256"
        )
    return rendered


def _safe_component(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if _SAFE_COMPONENT_RE.fullmatch(rendered) is None:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} is unsafe for an artifact filename"
        )
    return rendered


def _content_addressed(value: Mapping[str, Any]) -> dict[str, Any]:
    # Builders pass freshly finalized trees and never mutate them after this
    # call.  A recursive deepcopy would duplicate every prefix transition up
    # to three times (player, wave, partition) without strengthening identity.
    core = dict(value)
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
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} content-address contract is unsupported"
        )
    expected = _sha(address.get("sha256"), label=f"{label}.content_address.sha256")
    core = {key: child for key, child in value.items() if key != "content_address"}
    actual = _sha256_bytes(_canonical_bytes(core))
    if expected != actual:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} content address mismatch"
        )
    return actual


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleExternalTeamWaveModelV2Error(f"{label} must be an object")
    return value


def _data_root(path: Path) -> Path:
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.name == "derived":
            return parent.parent
    raise ChronicleExternalTeamWaveModelV2Error(
        "timeline manifest must be below an offline_data/derived directory"
    )


def _under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} must stay under the timeline offline_data root"
        )
    return resolved


def _relative(path: Path, root: Path, *, label: str) -> str:
    resolved = _under(path, root, label=label)
    return resolved.relative_to(root.resolve()).as_posix()


def _resolve_relative(
    base: Path, relative_value: Any, root: Path, *, label: str
) -> Path:
    relative = Path(_text(relative_value, label=label))
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} must be a safe relative path"
        )
    resolved = _under(base / relative, root, label=label)
    if not resolved.is_file() or resolved.is_symlink():
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} is not a regular file"
        )
    return resolved


def _node_id(kind: str, value: str) -> str:
    digest = _sha256_bytes(
        _canonical_bytes({"kind": kind, "observed_identity": value.casefold()})
    )
    return f"{kind}:{digest}"


def _guild_observed_identity(provenance: Mapping[str, Any]) -> tuple[str | None, str]:
    temporal = _mapping(
        provenance.get("temporal_and_guild_provenance"),
        label="instance temporal_and_guild_provenance",
    )
    guild = temporal.get("guild")
    if isinstance(guild, Mapping):
        guild_id = _optional_text(guild.get("id"))
        if guild_id is not None:
            return f"id:{guild_id}", "OFFICIAL_GUILD_ID"
        guild_name = _optional_text(guild.get("name"))
        if guild_name is not None:
            return f"name:{guild_name}", "OFFICIAL_GUILD_NAME_FALLBACK"
    contamination = _mapping(
        temporal.get("contamination"), label="instance contamination"
    )
    guild_context = _optional_text(contamination.get("guild_context"))
    if guild_context is not None:
        return f"context:{guild_context}", "CONTAMINATION_GUILD_CONTEXT_FALLBACK"
    return None, "UNKNOWN_EXPLICIT"


def _contamination(
    provenance: Mapping[str, Any],
    *,
    instance_id: str,
    receipt_content_sha256: str,
    training_instance_ids: frozenset[str],
    nontraining_reason_by_id: Mapping[str, str],
) -> dict[str, Any]:
    temporal = _mapping(
        provenance.get("temporal_and_guild_provenance"),
        label="instance temporal_and_guild_provenance",
    )
    contamination = deepcopy(
        dict(_mapping(temporal.get("contamination"), label="instance contamination"))
    )
    label = _text(contamination.get("label"), label="contamination.label")
    if label not in CONTAMINATION_LABELS:
        raise ChronicleExternalTeamWaveModelV2Error(
            "contamination label is outside the versioned raid-level contract"
        )
    if contamination.get("time_field") != "started_at":
        raise ChronicleExternalTeamWaveModelV2Error(
            "contamination must remain based on started_at"
        )
    if contamination.get("uploaded_at_used") is not False:
        raise ChronicleExternalTeamWaveModelV2Error(
            "uploaded_at must not enter contamination"
        )
    receipt_training_candidate = instance_id in training_instance_ids
    cohort_assignment = (
        "TRAINING_CANDIDATE"
        if receipt_training_candidate
        else "DESCRIPTIVE_NONTRAINING"
    )
    cohort_reason = (
        "BOUND_INVENTORY_EXPLICIT_TRAINING_CANDIDATE"
        if receipt_training_candidate
        else _text(
            nontraining_reason_by_id.get(instance_id),
            label="descriptive nontraining cohort reason",
        )
    )
    return {
        "label": label,
        "started_at": temporal.get("started_at"),
        "time_field": "started_at",
        "uploaded_at_used": False,
        "guild_context": contamination.get("guild_context"),
        "guild_evidence": contamination.get("guild_evidence"),
        "candidate_filter_passed": receipt_training_candidate,
        "raw_label_candidate_filter_passed": (
            label in DEFAULT_CANDIDATE_CONTAMINATION_LABELS
        ),
        "candidate_labels": sorted(DEFAULT_CANDIDATE_CONTAMINATION_LABELS),
        "candidate_authority": "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP",
        "cohort_assignment": cohort_assignment,
        "cohort_reason": cohort_reason,
        "cohort_receipt_content_sha256": receipt_content_sha256,
        "voting_authorized": False,
        "player_name_used": False,
    }


@dataclass(frozen=True)
class InputInstance:
    index: int
    instance_id: str
    entry: Mapping[str, Any]
    partition_path: Path
    provenance: Mapping[str, Any]
    contamination: Mapping[str, Any]
    source_binding_sha256: str


@dataclass(frozen=True)
class InputClosure:
    manifest: Mapping[str, Any]
    manifest_path: Path
    addressed_path: Path
    data_root: Path
    content_sha256: str
    file_sha256: str
    instances: tuple[InputInstance, ...]
    cohort_receipt: Mapping[str, Any]
    cohort_receipt_path: Path
    cohort_receipt_binding: Mapping[str, Any]
    training_instance_ids: frozenset[str]
    descriptive_nontraining_instance_ids: frozenset[str]


@dataclass(frozen=True)
class _CohortClosure:
    document: Mapping[str, Any]
    path: Path
    binding: Mapping[str, Any]
    descriptive_instance_ids: tuple[str, ...]
    training_instance_ids: frozenset[str]
    descriptive_nontraining_instance_ids: frozenset[str]
    nontraining_reason_by_id: Mapping[str, str]


def _cohort_ids(
    cohort: Mapping[str, Any], *, label: str
) -> tuple[str, ...]:
    raw_ids = _array(cohort.get("instance_ids"), label=f"{label}.instance_ids")
    instance_ids = tuple(
        _safe_component(value, label=f"{label}.instance_ids[{index}]")
        for index, value in enumerate(raw_ids)
    )
    if instance_ids != tuple(sorted(set(instance_ids))):
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} instance IDs must be unique and sorted"
        )
    if cohort.get("instance_count") != len(instance_ids):
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} instance count differs from its IDs"
        )
    expected_ids_sha = _sha256_bytes(_canonical_bytes(list(instance_ids)) + b"\n")
    if (
        cohort.get("instance_ids_hash_contract") != "canonical JSON array plus LF"
        or cohort.get("instance_ids_sha256") != expected_ids_sha
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            f"{label} instance ID hash contract differs"
        )
    return instance_ids


def _load_cohort_closure(
    path_value: str | Path, *, data_root: Path
) -> _CohortClosure:
    requested = Path(path_value).expanduser().resolve()
    expected_parent = (data_root / Path(union_v1.OUTPUT_DIRECTORY)).resolve()
    if (
        not requested.is_file()
        or requested.is_symlink()
        or requested.parent != expected_parent
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "cohort receipt is outside its immutable manifest directory"
        )
    try:
        payload_before = requested.read_bytes()
        audit = union_v1.audit_manifest_union_receipt(
            requested, data_root=data_root
        )
        payload_after = requested.read_bytes()
    except (OSError, union_v1.ChronicleExternalManifestUnionError) as error:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"cohort receipt strict full-source replay failed: {error}"
        ) from error
    if payload_before != payload_after:
        raise ChronicleExternalTeamWaveModelV2Error(
            "cohort receipt changed during strict full-source replay"
        )
    try:
        document = json.loads(payload_before.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"cohort receipt is not JSON: {error}"
        ) from error
    receipt = _mapping(document, label="cohort receipt")
    if payload_before != _canonical_bytes(receipt) + b"\n":
        raise ChronicleExternalTeamWaveModelV2Error(
            "cohort receipt is not canonical JSON"
        )
    if (
        receipt.get("schema") != union_v1.SCHEMA
        or receipt.get("kind") != union_v1.KIND
        or receipt.get("implementation_revision") != union_v1.IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "cohort receipt schema/kind/revision is unsupported"
        )
    try:
        # The union publisher intentionally hashes its canonical JSON document
        # including the terminal LF.  That contract differs from this model's
        # own content-addressed artifacts and must be verified by the producer.
        content_sha = union_v1._verify_content_address(
            receipt, label="cohort receipt"
        )
    except union_v1.ChronicleExternalManifestUnionError as error:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"cohort receipt content address is invalid: {error}"
        ) from error
    if requested.name != f"{union_v1.MANIFEST_PREFIX}.{content_sha}.manifest.json":
        raise ChronicleExternalTeamWaveModelV2Error(
            "cohort receipt filename/content address mismatch"
        )
    cohorts = _mapping(receipt.get("cohorts"), label="cohort receipt.cohorts")
    descriptive = _mapping(cohorts.get("descriptive"), label="descriptive cohort")
    training = _mapping(cohorts.get("training"), label="training cohort")
    nontraining = _mapping(
        cohorts.get("descriptive_nontraining"), label="descriptive nontraining cohort"
    )
    descriptive_ids = _cohort_ids(descriptive, label="descriptive cohort")
    training_ids = frozenset(_cohort_ids(training, label="training cohort"))
    nontraining_ids = frozenset(
        _cohort_ids(nontraining, label="descriptive nontraining cohort")
    )
    if (
        training_ids & nontraining_ids
        or training_ids | nontraining_ids != frozenset(descriptive_ids)
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "receipt training/nontraining cohorts do not partition descriptive IDs"
        )
    reason_by_id: dict[str, str] = {}
    for index, raw_reason in enumerate(
        _array(nontraining.get("reasons"), label="descriptive nontraining reasons")
    ):
        reason = _mapping(raw_reason, label=f"nontraining reason[{index}]")
        instance_id = _safe_component(
            reason.get("instance_id"), label=f"nontraining reason[{index}].instance_id"
        )
        rendered = _text(reason.get("reason"), label="nontraining reason")
        if instance_id in reason_by_id:
            raise ChronicleExternalTeamWaveModelV2Error(
                "receipt repeats a descriptive nontraining reason"
            )
        reason_by_id[instance_id] = rendered
    if set(reason_by_id) != set(nontraining_ids):
        raise ChronicleExternalTeamWaveModelV2Error(
            "receipt reasons do not cover the exact nontraining cohort"
        )
    raw_union = _mapping(receipt.get("raw_union"), label="receipt.raw_union")
    raw_union_sha = _sha(
        raw_union.get("file_sha256"), label="receipt raw union file SHA"
    )
    expected_audit = {
        "status": "PASS_STRICT_FULL_SOURCE_REPLAY",
        "receipt_content_sha256": content_sha,
        "raw_union_manifest_sha256": raw_union_sha,
        "descriptive_instance_count": len(descriptive_ids),
        "training_instance_count": len(training_ids),
        "descriptive_nontraining_instance_count": len(nontraining_ids),
        "network_requests_made": 0,
        "training_or_comparison_authorized": False,
    }
    if any(audit.get(key) != value for key, value in expected_audit.items()):
        raise ChronicleExternalTeamWaveModelV2Error(
            "cohort receipt audit result differs from the loaded receipt"
        )
    file_sha = _sha256_bytes(payload_before)
    binding = {
        "content_addressed_path": _relative(
            requested, data_root, label="cohort receipt"
        ),
        "schema": union_v1.SCHEMA,
        "kind": union_v1.KIND,
        "implementation_revision": union_v1.IMPLEMENTATION_REVISION,
        "content_sha256": content_sha,
        "file_sha256": file_sha,
        "size_bytes": len(payload_before),
        "raw_union_file_sha256": raw_union_sha,
        "raw_union_path": _text(raw_union.get("path"), label="raw union path"),
        "raw_union_size_bytes": _nonnegative_integer(
            raw_union.get("size_bytes"), label="raw union size"
        ),
        "descriptive_instance_count": len(descriptive_ids),
        "descriptive_instance_ids_sha256": descriptive.get("instance_ids_sha256"),
        "training_instance_count": len(training_ids),
        "training_instance_ids_sha256": training.get("instance_ids_sha256"),
        "descriptive_nontraining_instance_count": len(nontraining_ids),
        "descriptive_nontraining_instance_ids_sha256": nontraining.get(
            "instance_ids_sha256"
        ),
        "audit_status": "PASS_STRICT_FULL_SOURCE_REPLAY",
    }
    return _CohortClosure(
        document=deepcopy(dict(receipt)),
        path=requested,
        binding=binding,
        descriptive_instance_ids=descriptive_ids,
        training_instance_ids=training_ids,
        descriptive_nontraining_instance_ids=nontraining_ids,
        nontraining_reason_by_id=reason_by_id,
    )


def _load_input_closure(
    path_value: str | Path, *, cohort_receipt_path: str | Path
) -> InputClosure:
    requested = Path(path_value).expanduser().resolve()
    try:
        manifest, manifest_path = load_external_team_timeline_manifest(requested)
    except ChronicleExternalTeamTimelineV2Error as error:
        raise ChronicleExternalTeamWaveModelV2Error(str(error)) from error
    if (
        manifest.get("schema") != TIMELINE_SCHEMA
        or manifest.get("status") != TIMELINE_STATUS
        or manifest.get("implementation_revision")
        != TIMELINE_IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "input is not the supported External timeline V2 revision"
        )
    content_sha = _verify_content_address(manifest, label="timeline manifest")
    payload = _canonical_bytes(manifest) + b"\n"
    if manifest_path.read_bytes() != payload:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline stable manifest is not canonical or changed after validation"
        )
    file_sha = _sha256_bytes(payload)
    addressed = manifest_path.with_name(
        f"chronicle_external_team_timeline_v2.{content_sha}.manifest.json"
    )
    if (
        not addressed.is_file()
        or addressed.is_symlink()
        or addressed.read_bytes() != payload
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline content-addressed manifest is missing or differs"
        )
    root = _data_root(manifest_path)
    cohort = _load_cohort_closure(cohort_receipt_path, data_root=root)
    timeline_input_closure = _mapping(
        manifest.get("input_closure"), label="timeline input_closure"
    )
    timeline_raw = _mapping(
        timeline_input_closure.get("raw_api_manifest"),
        label="timeline raw_api_manifest",
    )
    receipt_raw = _mapping(cohort.document.get("raw_union"), label="receipt raw_union")
    for key in ("file_sha256", "path", "schema", "size_bytes"):
        if timeline_raw.get(key) != receipt_raw.get(key):
            raise ChronicleExternalTeamWaveModelV2Error(
                f"timeline raw union differs from cohort receipt {key}"
            )
    raw_entries = _array(manifest.get("instances"), label="timeline instances")
    order = _array(manifest.get("instance_order"), label="timeline instance_order")
    if len(raw_entries) != len(order) or not raw_entries:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline instance order and entries differ"
        )
    if tuple(order) != cohort.descriptive_instance_ids:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline instances differ from the receipt descriptive cohort"
        )
    instances: list[InputInstance] = []
    for index, raw_entry in enumerate(raw_entries):
        entry = _mapping(raw_entry, label=f"timeline instances[{index}]")
        instance_id = _safe_component(entry.get("instance_id"), label="instance_id")
        if order[index] != instance_id:
            raise ChronicleExternalTeamWaveModelV2Error(
                "timeline instances are not in declared deterministic order"
            )
        if index and instance_id <= instances[-1].instance_id:
            raise ChronicleExternalTeamWaveModelV2Error(
                "timeline instances must be strictly sorted by instance_id"
            )
        partition = _mapping(entry.get("partition"), label="timeline partition")
        partition_path = _resolve_relative(
            manifest_path.parent,
            partition.get("path"),
            root,
            label="timeline partition path",
        )
        provenance = _mapping(
            entry.get("instance_provenance"), label="instance_provenance"
        )
        source_binding = _mapping(entry.get("source_binding"), label="source_binding")
        instances.append(
            InputInstance(
                index=index,
                instance_id=instance_id,
                entry=deepcopy(dict(entry)),
                partition_path=partition_path,
                provenance=deepcopy(dict(provenance)),
                contamination=_contamination(
                    provenance,
                    instance_id=instance_id,
                    receipt_content_sha256=str(
                        cohort.binding["content_sha256"]
                    ),
                    training_instance_ids=cohort.training_instance_ids,
                    nontraining_reason_by_id=cohort.nontraining_reason_by_id,
                ),
                source_binding_sha256=_sha256_bytes(_canonical_bytes(source_binding)),
            )
        )
    return InputClosure(
        manifest=manifest,
        manifest_path=manifest_path,
        addressed_path=addressed,
        data_root=root,
        content_sha256=content_sha,
        file_sha256=file_sha,
        instances=tuple(instances),
        cohort_receipt=cohort.document,
        cohort_receipt_path=cohort.path,
        cohort_receipt_binding=cohort.binding,
        training_instance_ids=cohort.training_instance_ids,
        descriptive_nontraining_instance_ids=(
            cohort.descriptive_nontraining_instance_ids
        ),
    )


def _anchor_order(anchor_value: Any, *, encounter_ordinal: int) -> tuple[int, ...]:
    anchor = _mapping(anchor_value, label="trace anchor")
    stream_type = _text(anchor.get("stream_type"), label="trace anchor stream_type")
    if stream_type not in STREAM_ORDER:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"unsupported trace stream type {stream_type}"
        )
    return (
        encounter_ordinal,
        _nonnegative_integer(anchor.get("timestamp_ms"), label="anchor.timestamp_ms"),
        _nonnegative_integer(anchor.get("event_index"), label="anchor.event_index"),
        STREAM_ORDER[stream_type],
        _nonnegative_integer(
            anchor.get("frame_message_index"), label="anchor.frame_message_index"
        ),
    )


def _player_guid(player_value: Mapping[str, Any]) -> str:
    player = _mapping(player_value.get("player"), label="timeline player")
    return _text(player.get("guid"), label="timeline player.guid")


def _flatten_exact_trace(wave: Mapping[str, Any]) -> list[dict[str, Any]]:
    encounter_ordinal = _nonnegative_integer(
        wave.get("encounter_ordinal"), label="wave encounter_ordinal"
    )
    flattened: list[dict[str, Any]] = []
    players = _array(wave.get("players"), label="wave players")
    for raw_player in players:
        player = _mapping(raw_player, label="wave player")
        guid = _player_guid(player)
        for raw_event in _array(player.get("timeline"), label="player timeline"):
            event = dict(_mapping(raw_event, label="player timeline event"))
            attribution = _mapping(event.get("attribution"), label="event attribution")
            kind = _text(
                attribution.get("attribution_kind"), label="event attribution_kind"
            )
            if kind not in PLAYER_ATTRIBUTION_KINDS or attribution.get("player_guid") != guid:
                raise ChronicleExternalTeamWaveModelV2Error(
                    "player timeline event lacks exact direct/owner/controller attribution"
                )
            flattened.append(
                {
                    "trace_kind": "EXACT_PLAYER_EVENT",
                    "player_guid": guid,
                    "anchor": event.get("anchor"),
                    "event": event,
                }
            )
    unattributed = _mapping(wave.get("unattributed_lane"), label="unattributed_lane")
    for raw_event in _array(unattributed.get("timeline"), label="unattributed timeline"):
        event = dict(_mapping(raw_event, label="unattributed event"))
        attribution = _mapping(event.get("attribution"), label="event attribution")
        if (
            attribution.get("attribution_kind") != "UNATTRIBUTED"
            or attribution.get("player_guid") is not None
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "unattributed event acquired a player identity"
            )
        flattened.append(
            {
                "trace_kind": "UNATTRIBUTED_EVENT",
                "player_guid": None,
                "anchor": event.get("anchor"),
                "event": event,
            }
        )
    for raw_marker in _array(wave.get("death_markers"), label="death_markers"):
        marker = dict(_mapping(raw_marker, label="death marker"))
        death = _mapping(marker.get("death"), label="death marker death")
        if (
            marker.get("event_type") != "DEAD"
            or death.get("marker_only") is not True
            or death.get("damage_amount_added") != 0
            or "damage" in marker
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "DEAD must remain a marker-only zero-damage trace row"
            )
        flattened.append(
            {
                "trace_kind": "DEATH_MARKER",
                "player_guid": None,
                "anchor": marker.get("anchor"),
                "event": marker,
            }
        )
    for raw_diagnostic in _array(
        wave.get("negative_damage_diagnostics"),
        label="negative_damage_diagnostics",
    ):
        event = dict(_mapping(raw_diagnostic, label="negative damage diagnostic"))
        _negative_damage_signed_amount(event)
        flattened.append(
            {
                "trace_kind": "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT",
                "player_guid": None,
                "anchor": event.get("anchor"),
                "event": event,
            }
        )
    for raw_transition in _array(
        wave.get("classification_transitions_inside_window"),
        label="classification transitions",
    ):
        transition = dict(_mapping(raw_transition, label="classification transition"))
        flattened.append(
            {
                "trace_kind": "CLASSIFICATION_CONTEXT",
                "player_guid": None,
                "anchor": transition.get("anchor"),
                "classification": transition,
            }
        )
    flattened.sort(
        key=lambda item: _anchor_order(
            item.get("anchor"), encounter_ordinal=encounter_ordinal
        )
    )
    prior: tuple[int, ...] | None = None
    for index, item in enumerate(flattened):
        order = _anchor_order(item.get("anchor"), encounter_ordinal=encounter_ordinal)
        if prior is not None and order <= prior:
            raise ChronicleExternalTeamWaveModelV2Error(
                "flattened exact trace is not in strict EventMeta order"
            )
        prior = order
        item["trace_index"] = index
        item["order_key"] = list(order)
    return flattened


def _assert_prefix_contract(value: Any, *, path: str = "state_before") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered = str(key)
            if rendered in _BANNED_PREFIX_KEYS:
                raise ChronicleExternalTeamWaveModelV2Error(
                    f"forbidden future/outcome key in {path}.{rendered}"
                )
            _assert_prefix_contract(child, path=f"{path}.{rendered}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_prefix_contract(child, path=f"{path}[{index}]")


def _event_target(event: Mapping[str, Any]) -> tuple[str | None, str]:
    target = _mapping(event.get("target"), label="event target")
    guid = _optional_text(target.get("guid"))
    lane = _text(target.get("lane"), label="event target lane")
    return guid, lane


def _event_amount(event: Mapping[str, Any], event_type: str) -> int:
    if event_type == "DMG":
        damage = _mapping(event.get("damage"), label="DMG damage")
        if damage.get("amount_source") != "DMG_ONLY":
            raise ChronicleExternalTeamWaveModelV2Error(
                "DMG amount source must remain DMG_ONLY"
            )
        return _nonnegative_integer(damage.get("amount"), label="DMG amount")
    if event_type == "HEAL":
        healing = _mapping(event.get("healing"), label="HEAL healing")
        if healing.get("amount_source") != "HEAL_ONLY":
            raise ChronicleExternalTeamWaveModelV2Error(
                "HEAL amount source must remain HEAL_ONLY"
            )
        return _nonnegative_integer(healing.get("amount"), label="HEAL amount")
    return 0


def _negative_damage_signed_amount(event: Mapping[str, Any]) -> int:
    diagnostic = _mapping(
        event.get("negative_damage_diagnostic"),
        label="negative damage diagnostic",
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
        raise ChronicleExternalTeamWaveModelV2Error(
            "signed negative DMG diagnostic violates its nonvoting contract"
        )
    return signed


@dataclass
class _PrefixTarget:
    guid: str
    lane: str
    first_seen_ms: int
    last_seen_ms: int
    damage_amount: int = 0
    observed_dead: bool = False


@dataclass
class _PrefixState:
    wave_start_offset_ms: int
    trace_event_count: int = 0
    model_event_count: int = 0
    damage_by_attribution: Counter[str] = field(default_factory=Counter)
    damage_by_player: Counter[str] = field(default_factory=Counter)
    healing_by_attribution: Counter[str] = field(default_factory=Counter)
    targets: dict[str, _PrefixTarget] = field(default_factory=dict)
    classification_lane_by_guid: dict[str, str] = field(default_factory=dict)
    actor_recent: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    actor_last_target: dict[str, str | None] = field(default_factory=dict)

    def _elapsed(self, anchor: Mapping[str, Any]) -> int:
        offset = _integer(anchor.get("offset_ms"), label="anchor.offset_ms")
        return max(0, offset - self.wave_start_offset_ms)

    def snapshot(
        self,
        *,
        order_key: Sequence[int],
        anchor: Mapping[str, Any],
        actor_key: str,
    ) -> dict[str, Any]:
        elapsed_ms = self._elapsed(anchor)
        total_damage = sum(self.damage_by_attribution.values())
        focal_damage = self.damage_by_player.get(actor_key, 0)
        explicit_player_damage = sum(self.damage_by_player.values())
        unattributed_damage = self.damage_by_attribution.get("UNATTRIBUTED", 0)
        other_player_damage = explicit_player_damage - focal_damage
        if min(focal_damage, other_player_damage, unattributed_damage) < 0:
            raise ChronicleExternalTeamWaveModelV2Error(
                "prefix leave-one-player-out damage underflow"
            )
        background = None
        if actor_key != "__unattributed__":
            included = other_player_damage + unattributed_damage
            background = {
                "filter_contract": (
                    "exclude strict-prefix direct/official-owner/official-controller "
                    "DMG attributed to the exact focal player GUID"
                ),
                "focal_player_guid": actor_key,
                "excluded_focal_damage": focal_damage,
                "included_explicit_other_player_damage": other_player_damage,
                "included_unattributed_damage": unattributed_damage,
                "included_background_damage": included,
                "prefix_elapsed_average_background_dps": (
                    included / (elapsed_ms / 1000.0) if elapsed_ms > 0 else None
                ),
                "unattributed_retained_as_explicit_unknown": True,
                "name_or_guid_suffix_inference_used": False,
            }
        lane_counts = Counter(self.classification_lane_by_guid.values())
        classification_state = [
            {"guid": guid, "lane": lane}
            for guid, lane in sorted(self.classification_lane_by_guid.items())
        ]
        target_state = [
            {
                "target_guid": target.guid,
                "last_observed_lane": target.lane,
                "first_seen_wave_elapsed_ms": target.first_seen_ms,
                "last_seen_wave_elapsed_ms": target.last_seen_ms,
                "prefix_damage_amount": target.damage_amount,
                "observed_dead": target.observed_dead,
            }
            for target in sorted(self.targets.values(), key=lambda item: item.guid)
        ]
        target_lane_counts = Counter(
            str(row["last_observed_lane"]) for row in target_state
        )
        result = {
            "cutoff_semantics": "strictly before current exact EventMeta order_key",
            "cutoff_exclusive_order_key": list(order_key),
            "wave_elapsed_ms": elapsed_ms,
            "prefix_trace_event_count": self.trace_event_count,
            "prefix_player_or_unattributed_event_count": self.model_event_count,
            "prefix_damage_amount_by_attribution": {
                kind: self.damage_by_attribution.get(kind, 0)
                for kind in ALL_ATTRIBUTION_KINDS
            },
            "prefix_damage_amount_total": total_damage,
            "prefix_healing_amount_by_attribution": {
                kind: self.healing_by_attribution.get(kind, 0)
                for kind in ALL_ATTRIBUTION_KINDS
            },
            "leave_one_player_out_background_before": background,
            "classification_prefix_state_ref": {
                "content_sha256": _sha256_bytes(_canonical_bytes(classification_state)),
                "classified_guid_count": len(classification_state),
                "counts_by_lane": dict(sorted(lane_counts.items())),
                "materialization": (
                    "replay CLASSIFICATION_CONTEXT exact_trace rows with trace_index "
                    "strictly below prefix_trace_exclusive_index"
                ),
            },
            "observed_target_prefix_state_ref": {
                "content_sha256": _sha256_bytes(_canonical_bytes(target_state)),
                "observed_target_count": len(target_state),
                "observed_dead_target_count": sum(
                    1 for row in target_state if row["observed_dead"]
                ),
                "counts_by_last_observed_lane": dict(
                    sorted(target_lane_counts.items())
                ),
                "prefix_damage_amount": sum(
                    int(row["prefix_damage_amount"]) for row in target_state
                ),
                "availability_semantics": (
                    "prefix observed/classified/dead proxy; attackability not guessed"
                ),
                "materialization": (
                    "deterministically replay exact_trace rows strictly below "
                    "prefix_trace_exclusive_index"
                ),
            },
            "prefix_trace_exclusive_index": self.trace_event_count,
            "actor_last_observed_target_guid": self.actor_last_target.get(actor_key),
            "actor_recent_exact_trace_indices": list(
                self.actor_recent.get(actor_key, [])[-8:]
            ),
        }
        return result

    def _observe_target(
        self,
        *,
        guid: str | None,
        lane: str,
        elapsed_ms: int,
        damage: int = 0,
        dead: bool = False,
    ) -> None:
        if guid is None:
            return
        target = self.targets.get(guid)
        if target is None:
            target = _PrefixTarget(
                guid=guid,
                lane=lane,
                first_seen_ms=elapsed_ms,
                last_seen_ms=elapsed_ms,
            )
            self.targets[guid] = target
        else:
            target.lane = lane
            target.last_seen_ms = elapsed_ms
        target.damage_amount += damage
        if dead:
            target.observed_dead = True

    def observe(self, trace: Mapping[str, Any]) -> None:
        trace_kind = _text(trace.get("trace_kind"), label="trace_kind")
        anchor = _mapping(trace.get("anchor"), label="trace anchor")
        elapsed_ms = self._elapsed(anchor)
        if trace_kind == "CLASSIFICATION_CONTEXT":
            classification = _mapping(
                trace.get("classification"), label="classification context"
            )
            guid = _text(classification.get("guid"), label="classification guid")
            lane = _text(classification.get("lane"), label="classification lane")
            self.classification_lane_by_guid[guid] = lane
            if lane in {"HOSTILE_CREATURE", "HOSTILE_OBJECT", "HOSTILE_PLAYER"}:
                self._observe_target(guid=guid, lane=lane, elapsed_ms=elapsed_ms)
            self.trace_event_count += 1
            return
        event = _mapping(trace.get("event"), label="trace event")
        event_type = _text(event.get("event_type"), label="event_type")
        target_guid, target_lane = _event_target(event)
        if trace_kind == "DEATH_MARKER":
            if event_type != "DEAD" or "damage" in event:
                raise ChronicleExternalTeamWaveModelV2Error(
                    "death trace must be a zero-damage DEAD marker"
                )
            self._observe_target(
                guid=target_guid,
                lane=target_lane,
                elapsed_ms=elapsed_ms,
                dead=True,
            )
            self.trace_event_count += 1
            return
        if trace_kind == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT":
            _negative_damage_signed_amount(event)
            self._observe_target(
                guid=target_guid,
                lane=target_lane,
                elapsed_ms=elapsed_ms,
                damage=0,
            )
            self.trace_event_count += 1
            return
        if trace_kind not in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}:
            raise ChronicleExternalTeamWaveModelV2Error(
                f"unsupported exact trace kind {trace_kind}"
            )
        attribution = _mapping(event.get("attribution"), label="event attribution")
        kind = _text(
            attribution.get("attribution_kind"), label="event attribution_kind"
        )
        actor_key = trace.get("player_guid") or "__unattributed__"
        amount = _event_amount(event, event_type)
        if event_type == "DMG":
            self.damage_by_attribution[kind] += amount
            if actor_key != "__unattributed__":
                self.damage_by_player[str(actor_key)] += amount
        elif event_type == "HEAL":
            self.healing_by_attribution[kind] += amount
        self._observe_target(
            guid=target_guid,
            lane=target_lane,
            elapsed_ms=elapsed_ms,
            damage=amount if event_type == "DMG" else 0,
        )
        self.actor_recent[str(actor_key)].append(
            _nonnegative_integer(trace.get("trace_index"), label="trace_index")
        )
        self.actor_last_target[str(actor_key)] = target_guid
        self.trace_event_count += 1
        self.model_event_count += 1


def _transition(
    trace: Mapping[str, Any], state_before: Mapping[str, Any]
) -> dict[str, Any]:
    event = _mapping(trace.get("event"), label="transition event")
    event_type = _text(event.get("event_type"), label="transition event_type")
    if event_type not in ACTION_EVENT_TYPES:
        raise ChronicleExternalTeamWaveModelV2Error(
            "only START/GO/FAIL can become an action transition"
        )
    target = _mapping(event.get("target"), label="transition target")
    attribution = _mapping(event.get("attribution"), label="transition attribution")
    label: dict[str, Any] = {
        "event_type": event_type,
        "learning_role": "OBSERVED_ACTION_LABEL",
        "spell": deepcopy(event.get("spell")),
        "target": {
            "guid": target.get("guid"),
            "lane": target.get("lane"),
            "voting_enemy_target": target.get("voting_enemy_target"),
            "hostile_object_preserved_nonvoting": target.get(
                "hostile_object_preserved_nonvoting"
            ),
            "hostile_player_preserved_nonvoting": target.get(
                "hostile_player_preserved_nonvoting"
            ),
        },
        "source_lane": _mapping(event.get("source"), label="transition source").get(
            "lane"
        ),
        "attribution_kind": attribution.get("attribution_kind"),
    }
    label["action"] = deepcopy(event.get("action"))
    _assert_prefix_contract(state_before)
    return {
        "trace_index": trace.get("trace_index"),
        "order_key": deepcopy(trace.get("order_key")),
        "state_before": dict(state_before),
        "current_event_label": label,
        "feature_cutoff_is_strict_prefix": True,
        "current_event_present_in_state_before": False,
        "future_outcomes_in_state_before": False,
        "instant_cast_or_start_go_dedup_applied": False,
    }


def _spec_lane(player_record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(player_record.get("player"), label="player metadata")
    guid = _text(metadata.get("guid"), label="player guid")
    hero_class = _text(metadata.get("class"), label="player class").upper()
    evidence = deepcopy(
        dict(
            _mapping(
                player_record.get("warrior_spec_evidence"),
                label="warrior_spec_evidence",
            )
        )
    )
    if evidence.get("inference_used") is not False:
        raise ChronicleExternalTeamWaveModelV2Error(
            "spec lane must not use inferred evidence"
        )
    if hero_class != "WARRIOR":
        if evidence.get("status") != "NOT_APPLICABLE_NON_WARRIOR":
            raise ChronicleExternalTeamWaveModelV2Error(
                "non-warrior has an unexpected warrior spec status"
            )
        return {
            "partition_key": f"{hero_class}_UNSPECIFIED",
            "role": "TEAM_BACKGROUND_DESCRIPTIVE_NONVOTING",
            "observed_spec": None,
            "evidence_status": evidence.get("status"),
            "exact_guid_match": evidence.get("exact_player_guid_match"),
            "fury_or_arms_conflict_free_observation": False,
            "voting_authorized": False,
            "source_evidence": evidence,
        }
    observed_spec = evidence.get("player_spec")
    conflicts = evidence.get("field_conflicts")
    conflict_free = isinstance(conflicts, Mapping) and not conflicts
    exact = evidence.get("exact_player_guid_match") is True
    admitted = (
        evidence.get("status") == "OBSERVED"
        and observed_spec in {"Fury", "Arms"}
        and evidence.get("voting_for_fury_or_arms_lane") is True
        and conflict_free
        and exact
    )
    if admitted:
        partition = f"WARRIOR_{str(observed_spec).upper()}"
        role = (
            "FUTURE_EXPLICIT_ADAPTER_FURY_CANDIDATE_DESCRIPTIVE_NONVOTING"
            if observed_spec == "Fury"
            else "ARMS_COMMON_ACTION_DIAGNOSTIC_DESCRIPTIVE_NONVOTING"
        )
    else:
        partition = "WARRIOR_UNKNOWN_OR_CONFLICTING_NONVOTING"
        role = "UNKNOWN_SPEC_DESCRIPTIVE_NONVOTING"
    return {
        "partition_key": partition,
        "role": role,
        "observed_spec": observed_spec,
        "evidence_status": evidence.get("status"),
        "exact_guid_match": exact,
        "fury_or_arms_conflict_free_observation": admitted,
        "voting_authorized": False,
        "source_evidence": evidence,
    }


def _component_membership(
    *, instance_id: str, guild_identity: str | None, player_guid: str | None
) -> tuple[dict[str, Any], dict[str, str], set[tuple[str, str]]]:
    instance_node = _node_id("instance", instance_id)
    nodes = {instance_node: "instance"}
    edges: set[tuple[str, str]] = set()
    guild_node = _node_id("guild", guild_identity) if guild_identity else None
    if guild_node:
        nodes[guild_node] = "guild"
        edges.add(tuple(sorted((instance_node, guild_node))))
    player_node = _node_id("player", player_guid) if player_guid else None
    if player_node:
        nodes[player_node] = "player"
        edges.add(tuple(sorted((instance_node, player_node))))
    return (
        {
            "instance_node_id": instance_node,
            "guild_node_ids": [guild_node] if guild_node else [],
            "player_node_id": player_node,
            "component_edges": [list(edge) for edge in sorted(edges)],
            "required_split_unit": "connected component of instance, guild, and player nodes",
            "row_random_split_allowed": False,
        },
        nodes,
        edges,
    )


def _damage_by_kind(values: Mapping[str, int]) -> dict[str, int]:
    return {kind: int(values.get(kind, 0)) for kind in ALL_ATTRIBUTION_KINDS}


def _loo_projection(
    *,
    focal_guid: str,
    event_count: int,
    player_event_counts: Mapping[str, int],
    damage_by_kind: Mapping[str, int],
    damage_by_player_kind: Mapping[str, Mapping[str, int]],
    unattributed_event_count: int,
) -> dict[str, Any]:
    focal_by_kind = damage_by_player_kind.get(focal_guid, {})
    included_by_kind = {
        kind: int(damage_by_kind.get(kind, 0)) - int(focal_by_kind.get(kind, 0))
        for kind in ALL_ATTRIBUTION_KINDS
    }
    if any(value < 0 for value in included_by_kind.values()):
        raise ChronicleExternalTeamWaveModelV2Error(
            "final leave-one-player-out damage underflow"
        )
    excluded_count = int(player_event_counts.get(focal_guid, 0))
    return {
        "status": STATUS,
        "focal_player_guid": focal_guid,
        "filter_contract": {
            "excluded_attribution_kinds": sorted(PLAYER_ATTRIBUTION_KINDS),
            "exact_player_guid_match_required": True,
            "unattributed_events_retained": True,
            "name_or_guid_suffix_inference_used": False,
            "damage_amount_source": "DMG_ONLY",
            "dead_marker_damage_added": 0,
        },
        "included_event_count": event_count - excluded_count,
        "excluded_focal_event_count": excluded_count,
        "included_damage_amount_by_attribution": included_by_kind,
        "included_damage_amount": sum(included_by_kind.values()),
        "excluded_focal_damage_amount_by_attribution": _damage_by_kind(
            focal_by_kind
        ),
        "excluded_focal_damage_amount": sum(focal_by_kind.values()),
        "unattributed_branch": {
            "status": "EXPLICIT_UNKNOWN_NONVOTING",
            "event_count": unattributed_event_count,
            "damage_amount": int(damage_by_kind.get("UNATTRIBUTED", 0)),
        },
        "event_materialization": "filter exact_trace by exact focal player_guid",
        "voting_authorized": False,
    }


@dataclass(frozen=True)
class WaveOutput:
    record: Mapping[str, Any]
    component_nodes: Mapping[str, str]
    component_edges: tuple[tuple[str, str], ...]
    player_episode_count: int
    fury_episode_count: int
    arms_episode_count: int
    unknown_warrior_episode_count: int
    transition_count: int
    exact_trace_count: int
    exact_event_count: int
    classification_count: int
    death_marker_count: int
    negative_damage_diagnostic_count: int
    negative_damage_signed_amount_excluded: int
    negative_damage_absolute_amount_excluded: int


def _build_wave(
    wave: Mapping[str, Any], *, context: InputInstance
) -> WaveOutput:
    if (
        wave.get("schema") != TIMELINE_RECORD_SCHEMA
        or wave.get("implementation_revision") != TIMELINE_IMPLEMENTATION_REVISION
        or wave.get("status") != TIMELINE_STATUS
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "unsupported External timeline wave record"
        )
    timeline_wave_sha = _verify_content_address(wave, label="timeline wave")
    instance_id = _text(wave.get("instance_id"), label="wave instance_id")
    if instance_id != context.instance_id:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline wave crossed an instance partition"
        )
    encounter_id = _text(wave.get("encounter_id"), label="wave encounter_id")
    encounter_ordinal = _nonnegative_integer(
        wave.get("encounter_ordinal"), label="wave encounter_ordinal"
    )
    wave_id = _text(wave.get("wave_id"), label="wave_id")
    wave_ordinal = _nonnegative_integer(wave.get("wave_ordinal"), label="wave_ordinal")
    if wave.get("source_binding_sha256") != context.source_binding_sha256:
        raise ChronicleExternalTeamWaveModelV2Error(
            "wave source binding differs from timeline instance"
        )
    trace = _flatten_exact_trace(wave)
    accounting = _mapping(wave.get("event_accounting"), label="event_accounting")
    exclusive = _mapping(accounting.get("exclusive_lanes"), label="exclusive_lanes")
    expected_event_count = sum(
        _nonnegative_integer(exclusive.get(key), label=f"exclusive_lanes.{key}")
        for key in (
            "exact_player_event_count",
            "unattributed_event_count",
            "death_marker_count",
            "negative_damage_diagnostic_count",
        )
    )
    expected_class_count = _nonnegative_integer(
        accounting.get("classification_transition_count"),
        label="classification_transition_count",
    )
    if len(trace) != expected_event_count + expected_class_count:
        raise ChronicleExternalTeamWaveModelV2Error(
            "flattened trace count differs from timeline accounting"
        )
    damage_amount = sum(
        _event_amount(_mapping(row.get("event"), label="trace event"), "DMG")
        for row in trace
        if row.get("trace_kind") in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}
        and _mapping(row.get("event"), label="trace event").get("event_type") == "DMG"
    )
    declared_damage = _mapping(accounting.get("damage"), label="accounting damage")
    if (
        declared_damage.get("amount_source") != "DMG_ONLY"
        or declared_damage.get("amount") != damage_amount
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model DMG total differs from timeline accounting"
        )
    if _mapping(accounting.get("slain"), label="accounting slain").get(
        "amount_added_to_damage"
    ) != 0:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline slain accounting attempted to add damage"
        )
    negative_rows = [
        row
        for row in trace
        if row.get("trace_kind") == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT"
    ]
    negative_signed_amount = sum(
        _negative_damage_signed_amount(
            _mapping(row.get("event"), label="negative diagnostic trace event")
        )
        for row in negative_rows
    )
    negative_absolute_amount = sum(
        abs(
            _negative_damage_signed_amount(
                _mapping(row.get("event"), label="negative diagnostic trace event")
            )
        )
        for row in negative_rows
    )
    if (
        declared_damage.get("negative_event_count_excluded") != len(negative_rows)
        or declared_damage.get("negative_signed_amount_excluded")
        != negative_signed_amount
        or declared_damage.get("absolute_amount_excluded")
        != negative_absolute_amount
        or declared_damage.get("policy") != NEGATIVE_DAMAGE_VALUE_POLICY
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model negative-DMG diagnostics differ from timeline accounting"
        )

    window = _mapping(
        _mapping(wave.get("reconstruction_binding"), label="reconstruction_binding").get(
            "window"
        ),
        label="reconstruction window",
    )
    state = _PrefixState(
        wave_start_offset_ms=_integer(
            window.get("start_offset_ms"), label="window.start_offset_ms"
        )
    )
    transitions_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trace_indices_by_player: dict[str, list[int]] = defaultdict(list)
    outcome_indices_by_player: dict[str, list[int]] = defaultdict(list)
    unattributed_transitions: list[dict[str, Any]] = []
    unattributed_trace_indices: list[int] = []
    unattributed_outcome_indices: list[int] = []
    player_event_counts: Counter[str] = Counter()
    damage_by_kind: Counter[str] = Counter()
    damage_by_player_kind: dict[str, Counter[str]] = defaultdict(Counter)
    event_count = 0
    unattributed_event_count = 0
    for trace_row in trace:
        trace_kind = trace_row["trace_kind"]
        if trace_kind in {
            "CLASSIFICATION_CONTEXT",
            "DEATH_MARKER",
            "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT",
        }:
            state.observe(trace_row)
            continue
        event = _mapping(trace_row.get("event"), label="trace event")
        event_type = _text(event.get("event_type"), label="event_type")
        player_guid = _optional_text(trace_row.get("player_guid"))
        actor_key = player_guid or "__unattributed__"
        trace_index = _nonnegative_integer(
            trace_row.get("trace_index"), label="trace_index"
        )
        if player_guid is None:
            unattributed_trace_indices.append(trace_index)
            unattributed_event_count += 1
        else:
            trace_indices_by_player[player_guid].append(trace_index)
            player_event_counts[player_guid] += 1
        if event_type in ACTION_EVENT_TYPES:
            state_before = state.snapshot(
                order_key=_array(trace_row.get("order_key"), label="trace order_key"),
                anchor=_mapping(trace_row.get("anchor"), label="trace anchor"),
                actor_key=actor_key,
            )
            built = _transition(trace_row, state_before)
            if player_guid is None:
                unattributed_transitions.append(built)
            else:
                transitions_by_player[player_guid].append(built)
        elif event_type in AMOUNT_EVENT_TYPES:
            if player_guid is None:
                unattributed_outcome_indices.append(trace_index)
            else:
                outcome_indices_by_player[player_guid].append(trace_index)
        else:
            raise ChronicleExternalTeamWaveModelV2Error(
                f"unsupported non-DEAD exact event type {event_type}"
            )
        if event_type == "DMG":
            attribution = _mapping(event.get("attribution"), label="event attribution")
            kind = _text(
                attribution.get("attribution_kind"), label="event attribution_kind"
            )
            amount = _event_amount(event, "DMG")
            damage_by_kind[kind] += amount
            if player_guid is not None:
                damage_by_player_kind[player_guid][kind] += amount
        event_count += 1
        state.observe(trace_row)

    guild_identity, guild_identity_status = _guild_observed_identity(
        context.provenance
    )
    component_nodes: dict[str, str] = {}
    component_edges: set[tuple[str, str]] = set()
    players_output: list[dict[str, Any]] = []
    fury_count = 0
    arms_count = 0
    unknown_warrior_count = 0
    players = _array(wave.get("players"), label="wave players")
    for raw_player in players:
        player_record = _mapping(raw_player, label="wave player")
        _verify_content_address(player_record, label="timeline player record")
        metadata = deepcopy(
            dict(_mapping(player_record.get("player"), label="player metadata"))
        )
        guid = _text(metadata.get("guid"), label="player guid")
        spec_lane = _spec_lane(player_record)
        if spec_lane["partition_key"] == "WARRIOR_FURY":
            fury_count += 1
        elif spec_lane["partition_key"] == "WARRIOR_ARMS":
            arms_count += 1
        elif str(metadata.get("class") or "").upper() == "WARRIOR":
            unknown_warrior_count += 1
        membership, nodes, edges = _component_membership(
            instance_id=instance_id,
            guild_identity=guild_identity,
            player_guid=guid,
        )
        for node, kind in nodes.items():
            previous = component_nodes.setdefault(node, kind)
            if previous != kind:
                raise ChronicleExternalTeamWaveModelV2Error(
                    "component node kind conflict"
                )
        component_edges.update(edges)
        candidate_contamination = bool(context.contamination["candidate_filter_passed"])
        players_output.append(
            _content_addressed(
                {
                    "player": metadata,
                    "warrior_spec_lane": spec_lane,
                    "contamination_lane": deepcopy(dict(context.contamination)),
                    "component_membership": membership,
                    "eligibility_observation": {
                        "team_behavior_candidate_filter_passed": candidate_contamination,
                        "historical_fury_candidate_filter_passed": (
                            candidate_contamination
                            and spec_lane["partition_key"] == "WARRIOR_FURY"
                        ),
                        "arms_diagnostic_candidate_filter_passed": (
                            candidate_contamination
                            and spec_lane["partition_key"] == "WARRIOR_ARMS"
                        ),
                        "voting_authorized": False,
                        "explicit_historical_policy_adapter_required": True,
                    },
                    "exact_trace_indices": trace_indices_by_player.get(guid, []),
                    "outcome_context_trace_indices": outcome_indices_by_player.get(
                        guid, []
                    ),
                    "prefix_transitions": transitions_by_player.get(guid, []),
                    "leave_one_player_out_background": _loo_projection(
                        focal_guid=guid,
                        event_count=event_count,
                        player_event_counts=player_event_counts,
                        damage_by_kind=damage_by_kind,
                        damage_by_player_kind=damage_by_player_kind,
                        unattributed_event_count=unattributed_event_count,
                    ),
                    "summary": {
                        "event_count": player_event_counts.get(guid, 0),
                        "transition_count": len(transitions_by_player.get(guid, [])),
                        "damage_amount": sum(damage_by_player_kind.get(guid, {}).values()),
                    },
                }
            )
        )
    unattributed_membership, nodes, edges = _component_membership(
        instance_id=instance_id,
        guild_identity=guild_identity,
        player_guid=None,
    )
    component_nodes.update(nodes)
    component_edges.update(edges)
    death_indices = [
        row["trace_index"] for row in trace if row["trace_kind"] == "DEATH_MARKER"
    ]
    class_indices = [
        row["trace_index"]
        for row in trace
        if row["trace_kind"] == "CLASSIFICATION_CONTEXT"
    ]
    negative_diagnostic_indices = [
        row["trace_index"]
        for row in trace
        if row["trace_kind"] == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT"
    ]
    wave_identity = {
        "instance_id": instance_id,
        "encounter_id": encounter_id,
        "encounter_ordinal": encounter_ordinal,
        "wave_id": wave_id,
        "wave_ordinal": wave_ordinal,
    }
    record_core = {
        "schema": PARTITION_RECORD_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "wave": wave_identity,
        "source_timeline": {
            "wave_content_sha256": timeline_wave_sha,
            "instance_source_binding_sha256": context.source_binding_sha256,
        },
        "raid_provenance": {
            "contamination": deepcopy(dict(context.contamination)),
            "guild_observed_identity_status": guild_identity_status,
            "started_at": context.contamination.get("started_at"),
        },
        "exact_trace": trace,
        "players": players_output,
        "unattributed_episode": {
            "actor": {
                "kind": "UNATTRIBUTED_UNKNOWN",
                "player_guid": None,
                "owner_inference_used": False,
            },
            "component_membership": unattributed_membership,
            "exact_trace_indices": unattributed_trace_indices,
            "outcome_context_trace_indices": unattributed_outcome_indices,
            "prefix_transitions": unattributed_transitions,
            "eligibility": {
                "retained_as_uncertainty_branch": True,
                "voting_authorized": False,
            },
            "summary": {
                "event_count": unattributed_event_count,
                "transition_count": len(unattributed_transitions),
                "damage_amount": damage_by_kind.get("UNATTRIBUTED", 0),
            },
        },
        "descriptive_outcome": {
            "allowed_in_state_before": False,
            "timeline_summary": deepcopy(wave.get("summary")),
            "event_accounting": deepcopy(wave.get("event_accounting")),
            "reconstruction_binding": deepcopy(wave.get("reconstruction_binding")),
            "death_marker_trace_indices": death_indices,
            "classification_context_trace_indices": class_indices,
            "negative_damage_diagnostic_trace_indices": (
                negative_diagnostic_indices
            ),
        },
        "scientific_boundaries": {
            "status": STATUS,
            "learned_team_model_built": False,
            "historical_policy_adapter_present": False,
            "comparison_authorized": False,
            "policy_training_authorized": False,
            "exact_trace_can_vote": False,
            "damage_amount_source": "DMG_ONLY",
            "dead_marker_damage_added": 0,
            "negative_damage_value_policy": NEGATIVE_DAMAGE_VALUE_POLICY,
            "negative_damage_abs_or_clamp_used": False,
            "negative_damage_or_reward_added": 0,
            "future_death_or_final_total_in_state_before": False,
            "future_classification_backfill_allowed": False,
            "name_or_guid_suffix_identity_inference_used": False,
        },
        "summary": {
            "player_episode_count": len(players_output),
            "fury_episode_count": fury_count,
            "arms_episode_count": arms_count,
            "unknown_warrior_episode_count": unknown_warrior_count,
            "transition_count": sum(
                len(values) for values in transitions_by_player.values()
            )
            + len(unattributed_transitions),
            "exact_trace_count": len(trace),
            "exact_event_count": expected_event_count,
            "classification_context_count": expected_class_count,
            "death_marker_count": len(death_indices),
            "negative_damage_diagnostic_count": len(negative_rows),
            "negative_damage_signed_amount_excluded": negative_signed_amount,
            "negative_damage_absolute_amount_excluded": negative_absolute_amount,
            "damage_amount": damage_amount,
            "dead_marker_damage_added": 0,
        },
    }
    record = _content_addressed(record_core)
    return WaveOutput(
        record=record,
        component_nodes=component_nodes,
        component_edges=tuple(sorted(component_edges)),
        player_episode_count=len(players_output),
        fury_episode_count=fury_count,
        arms_episode_count=arms_count,
        unknown_warrior_episode_count=unknown_warrior_count,
        transition_count=sum(len(values) for values in transitions_by_player.values())
        + len(unattributed_transitions),
        exact_trace_count=len(trace),
        exact_event_count=expected_event_count,
        classification_count=expected_class_count,
        death_marker_count=len(death_indices),
        negative_damage_diagnostic_count=len(negative_rows),
        negative_damage_signed_amount_excluded=negative_signed_amount,
        negative_damage_absolute_amount_excluded=negative_absolute_amount,
    )


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    compressed_file_sha256: str
    manifest_entry: Mapping[str, Any]
    component_nodes: tuple[tuple[str, str], ...]
    component_edges: tuple[tuple[str, str], ...]


def _roster_evidence_sha(wave: Mapping[str, Any]) -> str:
    rows = []
    for raw_player in _array(wave.get("players"), label="wave players"):
        player = _mapping(raw_player, label="wave player")
        rows.append(
            {
                "player": deepcopy(player.get("player")),
                "warrior_spec_evidence": deepcopy(
                    player.get("warrior_spec_evidence")
                ),
            }
        )
    rows.sort(
        key=lambda row: _text(
            _mapping(row.get("player"), label="roster player").get("guid"),
            label="roster player guid",
        )
    )
    return _sha256_bytes(_canonical_bytes(rows))


def _build_partition(
    context: InputInstance, *, output_directory: Path
) -> PartitionBuild:
    partition = _mapping(context.entry.get("partition"), label="timeline partition")
    expected_compressed_size = _nonnegative_integer(
        partition.get("compressed_size_bytes"), label="timeline compressed_size_bytes"
    )
    if context.partition_path.stat().st_size != expected_compressed_size:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline partition compressed size changed before model build"
        )
    expected_compressed_sha = _sha(
        partition.get("compressed_file_sha256"),
        label="timeline compressed_file_sha256",
    )
    if _sha256_file(context.partition_path) != expected_compressed_sha:
        raise ChronicleExternalTeamWaveModelV2Error(
            "timeline partition compressed hash changed before model build"
        )
    expected_logical_sha = _sha(
        partition.get("logical_content_sha256"),
        label="timeline logical_content_sha256",
    )
    expected_record_count = _nonnegative_integer(
        partition.get("record_count"), label="timeline record_count"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{context.instance_id}.external-team-wave-model.",
        suffix=".jsonl.gz.tmp",
        dir=output_directory,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    input_logical = hashlib.sha256()
    output_logical = hashlib.sha256()
    output_logical_size = 0
    input_count = 0
    output_count = 0
    prior_wave_order: tuple[int, int, str] | None = None
    encounter_ids: set[str] = set()
    roster_sha: str | None = None
    component_nodes: dict[str, str] = {}
    component_edges: set[tuple[str, str]] = set()
    summary: Counter[str] = Counter()
    try:
        try:
            input_handle = gzip.open(context.partition_path, "rb")
        except OSError as error:
            raise ChronicleExternalTeamWaveModelV2Error(
                f"cannot open timeline partition {context.partition_path}: {error}"
            ) from error
        with input_handle:
            with temporary.open("wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_output,
                    compresslevel=9,
                    mtime=0,
                ) as compressed_output:
                    for line_number, raw_line in enumerate(input_handle, 1):
                        input_logical.update(raw_line)
                        if not raw_line.strip():
                            raise ChronicleExternalTeamWaveModelV2Error(
                                "timeline partition contains an empty JSONL row"
                            )
                        try:
                            value = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as error:
                            raise ChronicleExternalTeamWaveModelV2Error(
                                f"invalid timeline JSONL row {line_number}: {error}"
                            ) from error
                        wave = _mapping(value, label=f"timeline row {line_number}")
                        if raw_line != _canonical_bytes(wave) + b"\n":
                            raise ChronicleExternalTeamWaveModelV2Error(
                                f"timeline row {line_number} is not canonical JSONL"
                            )
                        wave_order = (
                            _nonnegative_integer(
                                wave.get("encounter_ordinal"),
                                label="wave encounter_ordinal",
                            ),
                            _nonnegative_integer(
                                wave.get("wave_ordinal"), label="wave wave_ordinal"
                            ),
                            _text(wave.get("wave_id"), label="wave wave_id"),
                        )
                        if prior_wave_order is not None and wave_order <= prior_wave_order:
                            raise ChronicleExternalTeamWaveModelV2Error(
                                "timeline waves are not in deterministic encounter/wave order"
                            )
                        prior_wave_order = wave_order
                        current_roster_sha = _roster_evidence_sha(wave)
                        if roster_sha is None:
                            roster_sha = current_roster_sha
                        elif roster_sha != current_roster_sha:
                            raise ChronicleExternalTeamWaveModelV2Error(
                                "exact player metadata/spec roster changed across instance waves"
                            )
                        built = _build_wave(wave, context=context)
                        payload = _canonical_bytes(built.record) + b"\n"
                        compressed_output.write(payload)
                        output_logical.update(payload)
                        output_logical_size += len(payload)
                        input_count += 1
                        output_count += 1
                        encounter_ids.add(
                            _text(wave.get("encounter_id"), label="wave encounter_id")
                        )
                        for node, kind in built.component_nodes.items():
                            previous = component_nodes.setdefault(node, kind)
                            if previous != kind:
                                raise ChronicleExternalTeamWaveModelV2Error(
                                    "component node kind conflict within partition"
                                )
                        component_edges.update(built.component_edges)
                        summary["player_wave_episode_count"] += built.player_episode_count
                        summary["fury_episode_count"] += built.fury_episode_count
                        summary["arms_episode_count"] += built.arms_episode_count
                        summary[
                            "unknown_warrior_episode_count"
                        ] += built.unknown_warrior_episode_count
                        summary["prefix_transition_count"] += built.transition_count
                        summary["exact_trace_count"] += built.exact_trace_count
                        summary["exact_event_count"] += built.exact_event_count
                        summary[
                            "classification_context_count"
                        ] += built.classification_count
                        summary["death_marker_count"] += built.death_marker_count
                        summary[
                            "negative_damage_diagnostic_count"
                        ] += built.negative_damage_diagnostic_count
                        summary[
                            "negative_damage_signed_amount_excluded"
                        ] += built.negative_damage_signed_amount_excluded
                        summary[
                            "negative_damage_absolute_amount_excluded"
                        ] += built.negative_damage_absolute_amount_excluded
                raw_output.flush()
                os.fsync(raw_output.fileno())
        if input_count != expected_record_count:
            raise ChronicleExternalTeamWaveModelV2Error(
                "timeline partition record count differs from manifest"
            )
        if input_logical.hexdigest() != expected_logical_sha:
            raise ChronicleExternalTeamWaveModelV2Error(
                "timeline partition logical hash changed during model build"
            )
        if _sha256_file(context.partition_path) != expected_compressed_sha:
            raise ChronicleExternalTeamWaveModelV2Error(
                "timeline partition compressed hash changed during model build"
            )
        output_logical_sha = output_logical.hexdigest()
        final = output_directory / (
            f"{context.instance_id}.{output_logical_sha}.jsonl.gz"
        )
        compressed_sha = _sha256_file(temporary)
        provenance = deepcopy(dict(context.provenance))
        spec_evidence = _mapping(
            provenance.get("warrior_spec_evidence"),
            label="instance warrior_spec_evidence",
        )
        entry_core = {
            "instance_id": context.instance_id,
            "status": STATUS,
            "timeline_input": {
                "instance_entry_content_sha256": _verify_content_address(
                    context.entry, label="timeline instance entry"
                ),
                "source_binding_sha256": context.source_binding_sha256,
                "partition_path_name": context.partition_path.name,
                "logical_content_sha256": expected_logical_sha,
                "compressed_file_sha256": expected_compressed_sha,
                "compressed_size_bytes": expected_compressed_size,
                "record_count": expected_record_count,
                "scan_count": 1,
            },
            "instance_provenance": provenance,
            "contamination_lane": deepcopy(dict(context.contamination)),
            "exact_roster_evidence_sha256": roster_sha,
            "partition": {
                "path": final.name,
                "logical_content_sha256": output_logical_sha,
                "logical_size_bytes": output_logical_size,
                "compressed_file_sha256": compressed_sha,
                "compressed_size_bytes": temporary.stat().st_size,
                "record_count": output_count,
                "record_schema": PARTITION_RECORD_SCHEMA,
                "gzip_mtime": 0,
            },
            "summary": {
                "encounter_count": len(encounter_ids),
                "wave_count": output_count,
                "player_wave_episode_count": summary[
                    "player_wave_episode_count"
                ],
                "fury_episode_count": summary["fury_episode_count"],
                "arms_episode_count": summary["arms_episode_count"],
                "unknown_warrior_episode_count": summary[
                    "unknown_warrior_episode_count"
                ],
                "warrior_spec_observation_count": _nonnegative_integer(
                    spec_evidence.get("observation_count"),
                    label="warrior spec observation_count",
                ),
                "prefix_transition_count": summary["prefix_transition_count"],
                "exact_trace_count": summary["exact_trace_count"],
                "exact_event_count": summary["exact_event_count"],
                "classification_context_count": summary[
                    "classification_context_count"
                ],
                "death_marker_count": summary["death_marker_count"],
                "negative_damage_diagnostic_count": summary[
                    "negative_damage_diagnostic_count"
                ],
                "negative_damage_signed_amount_excluded": summary[
                    "negative_damage_signed_amount_excluded"
                ],
                "negative_damage_absolute_amount_excluded": summary[
                    "negative_damage_absolute_amount_excluded"
                ],
                "raw_row_copy_count": 0,
                "normalized_row_copy_count": 0,
                "network_request_count": 0,
            },
        }
        return PartitionBuild(
            temporary_path=temporary,
            final_path=final,
            compressed_file_sha256=compressed_sha,
            manifest_entry=_content_addressed(entry_core),
            component_nodes=tuple(sorted(component_nodes.items())),
            component_edges=tuple(sorted(component_edges)),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, value: str) -> str:
        self.parent.setdefault(value, value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _split_graph(
    nodes: Mapping[str, str], edges: Iterable[tuple[str, str]]
) -> dict[str, Any]:
    disjoint = _DisjointSet()
    for node in nodes:
        disjoint.find(node)
    normalized_edges = sorted({tuple(sorted(edge)) for edge in edges})
    for left, right in normalized_edges:
        if left not in nodes or right not in nodes:
            raise ChronicleExternalTeamWaveModelV2Error(
                "component edge references an unknown node"
            )
        disjoint.union(left, right)
    grouped: dict[str, list[str]] = defaultdict(list)
    for node in sorted(nodes):
        grouped[disjoint.find(node)].append(node)
    components: list[dict[str, Any]] = []
    node_to_component: list[dict[str, str]] = []
    for members in sorted(grouped.values(), key=lambda values: tuple(values)):
        component_id = _sha256_bytes(_canonical_bytes({"nodes": members}))
        components.append({"component_id": component_id, "node_ids": members})
        node_to_component.extend(
            {"node_id": node, "component_id": component_id} for node in members
        )
    return {
        "node_identity": "kind plus SHA-256 of case-folded exact observed identity",
        "nodes": [
            {"node_id": node, "kind": nodes[node]} for node in sorted(nodes)
        ],
        "edges": [list(edge) for edge in normalized_edges],
        "connected_components": components,
        "node_to_component": sorted(node_to_component, key=lambda row: row["node_id"]),
        "required_split_unit": "connected component",
        "row_random_split_allowed": False,
        "same_player_or_guild_can_cross_folds": False,
    }


def _manifest_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = (
        "encounter_count",
        "wave_count",
        "player_wave_episode_count",
        "fury_episode_count",
        "arms_episode_count",
        "unknown_warrior_episode_count",
        "warrior_spec_observation_count",
        "prefix_transition_count",
        "exact_trace_count",
        "exact_event_count",
        "classification_context_count",
        "death_marker_count",
        "negative_damage_diagnostic_count",
        "negative_damage_absolute_amount_excluded",
        "raw_row_copy_count",
        "normalized_row_copy_count",
        "network_request_count",
    )
    result = {
        key: sum(
            _nonnegative_integer(
                _mapping(entry.get("summary"), label="instance summary").get(key),
                label=f"instance summary.{key}",
            )
            for entry in entries
        )
        for key in keys
    }
    result["negative_damage_signed_amount_excluded"] = sum(
        _integer(
            _mapping(entry.get("summary"), label="instance summary").get(
                "negative_damage_signed_amount_excluded"
            ),
            label="instance summary.negative_damage_signed_amount_excluded",
        )
        for entry in entries
    )
    result["instance_count"] = len(entries)
    labels = Counter(
        _text(
            _mapping(entry.get("contamination_lane"), label="contamination lane").get(
                "label"
            ),
            label="contamination label",
        )
        for entry in entries
    )
    result["contamination_label_counts"] = dict(sorted(labels.items()))
    result["training_candidate_instance_count"] = sum(
        _mapping(entry.get("contamination_lane"), label="contamination lane").get(
            "candidate_filter_passed"
        )
        is True
        for entry in entries
    )
    result["descriptive_nontraining_instance_count"] = (
        len(entries) - result["training_candidate_instance_count"]
    )
    result["compressed_partition_bytes"] = sum(
        _nonnegative_integer(
            _mapping(entry.get("partition"), label="instance partition").get(
                "compressed_size_bytes"
            ),
            label="instance compressed_size_bytes",
        )
        for entry in entries
    )
    result["comparison_authorized"] = False
    return result


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


def _publish_immutable(temporary: Path, final: Path, expected_sha: str) -> None:
    if final.exists():
        if not final.is_file() or final.is_symlink() or _sha256_file(final) != expected_sha:
            raise ChronicleExternalTeamWaveModelV2Error(
                f"immutable content-addressed output differs: {final}"
            )
        temporary.unlink()
    else:
        temporary.replace(final)


def _parallel_builds(
    contexts: Sequence[InputInstance], *, output_directory: Path, workers: int
) -> list[PartitionBuild]:
    results: list[PartitionBuild | None] = [None] * len(contexts)
    failure: BaseException | None = None
    with ProcessPoolExecutor(max_workers=min(workers, len(contexts))) as executor:
        futures = {
            executor.submit(
                _build_partition, context, output_directory=output_directory
            ): index
            for index, context in enumerate(contexts)
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
        raise ChronicleExternalTeamWaveModelV2Error(
            "parallel model build ended without every instance result"
        )
    return [result for result in results if result is not None]


def build_external_team_wave_model(
    *,
    timeline_manifest_path: str | Path = DEFAULT_TIMELINE_MANIFEST,
    cohort_receipt_path: str | Path,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
) -> dict[str, Any]:
    """Build deterministic, descriptive External per-player wave evidence."""

    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ChronicleExternalTeamWaveModelV2Error(
            f"workers must be an integer from 1 through {MAX_WORKERS}"
        )
    closure = _load_input_closure(
        timeline_manifest_path, cohort_receipt_path=cohort_receipt_path
    )
    output = _under(
        Path(output_directory), closure.data_root, label="team-wave model output"
    )
    output.mkdir(parents=True, exist_ok=True)
    builds: list[PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        if workers == 1 or len(closure.instances) == 1:
            for context in closure.instances:
                builds.append(_build_partition(context, output_directory=output))
        else:
            builds = _parallel_builds(
                closure.instances, output_directory=output, workers=workers
            )
        entries = [deepcopy(dict(build.manifest_entry)) for build in builds]
        nodes: dict[str, str] = {}
        edges: set[tuple[str, str]] = set()
        for build in builds:
            for node, kind in build.component_nodes:
                previous = nodes.setdefault(node, kind)
                if previous != kind:
                    raise ChronicleExternalTeamWaveModelV2Error(
                        "global component node kind conflict"
                    )
            edges.update(build.component_edges)
        manifest_core = {
            "schema": SCHEMA,
            "kind": KIND,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "status": STATUS,
            "input_closure": {
                "timeline_manifest": {
                    "stable_path": _relative(
                        closure.manifest_path,
                        closure.data_root,
                        label="timeline stable manifest",
                    ),
                    "content_addressed_path": _relative(
                        closure.addressed_path,
                        closure.data_root,
                        label="timeline addressed manifest",
                    ),
                    "content_sha256": closure.content_sha256,
                    "file_sha256": closure.file_sha256,
                    "size_bytes": closure.manifest_path.stat().st_size,
                    "schema": TIMELINE_SCHEMA,
                    "implementation_revision": TIMELINE_IMPLEMENTATION_REVISION,
                    "status": TIMELINE_STATUS,
                },
                "cohort_receipt": deepcopy(
                    dict(closure.cohort_receipt_binding)
                ),
                "instance_ids_sha256": _sha256_bytes(
                    _canonical_bytes(
                        [context.instance_id for context in closure.instances]
                    )
                ),
            },
            "instance_order": [entry["instance_id"] for entry in entries],
            "streaming_and_parallel_contract": {
                "one_timeline_wave_materialized_at_a_time_per_worker": True,
                "full_instance_event_list_materialized": False,
                "workers_min": 1,
                "workers_max": MAX_WORKERS,
                "requested_worker_count_enters_content_identity": False,
                "worker_completion_order_enters_result_order": False,
                "instances_committed_in_sorted_instance_id_order": True,
                "gzip_mtime": 0,
            },
            "trace_contract": {
                "event_order": [
                    "encounter_ordinal",
                    "timestamp_ms",
                    "EventMeta.index",
                    "fixed_stream_tiebreaker",
                    "frame_message_index",
                ],
                "trace_lanes": [
                    "EXACT_PLAYER_EVENT",
                    "UNATTRIBUTED_EVENT",
                    "CLASSIFICATION_CONTEXT",
                    "DEATH_MARKER",
                    "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT",
                ],
                "damage_amount_source": "DMG_ONLY",
                "dead_marker_damage_added": 0,
                "negative_damage_diagnostic_lane": (
                    NEGATIVE_DAMAGE_DIAGNOSTIC_LANE
                ),
                "negative_damage_value_policy": NEGATIVE_DAMAGE_VALUE_POLICY,
                "negative_damage_enters_damage_loo_reward_or_action": False,
                "negative_damage_abs_or_clamp_allowed": False,
                "outcome_context_types": ["DMG", "HEAL"],
                "outcome_context_remains_in_exact_trace": True,
                "signed_negative_dmg_is_outcome_context": False,
                "exact_owner_controller_only": True,
                "hostile_object_and_hostile_player_target_flags_preserved": True,
            },
            "prefix_contract": {
                "state_cutoff": "strictly before current exact EventMeta order_key",
                "future_death_or_final_total_in_state_before": False,
                "future_classification_backfill_allowed": False,
                "current_event_is_label_not_state": True,
                "action_transition_types": sorted(ACTION_EVENT_TYPES),
                "damage_or_heal_becomes_action_transition": False,
                "damage_or_heal_updates_later_action_state": True,
                "negative_damage_updates_later_damage_or_loo_state": False,
                "negative_damage_preserves_temporal_target_touch": True,
                "leave_one_player_out_excludes_exact_focal_direct_owner_controller": True,
                "unattributed_background_retained": True,
                "action_start_go_dedup_deferred_to_explicit_prefix_causal_adapter": True,
                "attackability_not_inferred_from_silence": True,
                "large_classification_and_target_prefix_state": (
                    "content-addressed state reference plus exact-trace exclusive index; "
                    "materialized only by deterministic prefix replay"
                ),
            },
            "spec_and_contamination_contract": {
                "player_class_and_race_source": "timeline exact metadata GUID resolver",
                "warrior_spec_source": "timeline admission exact GUID evidence",
                "fury_and_arms_separate": True,
                "unknown_or_conflicting_warrior_spec_nonvoting": True,
                "contamination_scope": "raid instance",
                "contamination_time_field": "started_at",
                "uploaded_at_used": False,
                "player_name_used_for_contamination_or_spec": False,
                "contamination_rule_recomputed_by_model": False,
                "training_candidate_authority": (
                    "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP"
                ),
                "raw_contamination_label_may_expand_training_cohort": False,
                "cohort_receipt_strict_full_source_replay": True,
                "descriptive_instance_count": len(closure.instances),
                "training_candidate_instance_count": len(
                    closure.training_instance_ids
                ),
                "descriptive_nontraining_instance_count": len(
                    closure.descriptive_nontraining_instance_ids
                ),
                "legacy_single_cutoff_compatibility_alias": (
                    CONTAMINATION_CUTOFF
                ),
                "contamination_suspect_before_local": (
                    CONTAMINATION_SUSPECT_BEFORE
                ),
                "contamination_postfix_at_or_after_local": (
                    CONTAMINATION_POSTFIX_AT_OR_AFTER
                ),
                "contamination_rules": {
                    "guild_is_nanbei_and_started_before_fix_date": (
                        "SUSPECT_36YD_RANGE_BUG"
                    ),
                    "guild_is_nanbei_and_started_in_fix_morning_boundary": (
                        "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING"
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
            "split_graph": _split_graph(nodes, edges),
            "scientific_boundaries": {
                "status": STATUS,
                "learned_team_model_built": False,
                "historical_policy_adapter_present": False,
                "exact_trace_status": "DESCRIPTIVE_NONVOTING",
                "comparison_authorized": False,
                "policy_training_authorized": False,
                "legacy_team_timeline_or_model_modified": False,
                "old_50_capsule_membership_claimed": False,
                "fourth_baseline_present": False,
                "multiseed_comparison_ready": False,
                "superiority_claim": False,
            },
            "publication_contract": {
                "partition_per_instance": True,
                "wave_player_partition_and_manifest_content_addressed": True,
                "content_addressed_manifest_published_before_stable_pointer": True,
                "stable_manifest_committed_last": True,
                "stable_and_addressed_manifest_bytes_equal": True,
                "raw_or_normalized_rows_copied": False,
            },
            "summary": _manifest_summary(entries),
            "instances": entries,
        }
        manifest = _content_addressed(manifest_core)
        content_sha = _verify_content_address(manifest, label="model manifest")
        payload = _canonical_bytes(manifest) + b"\n"
        file_sha = _sha256_bytes(payload)
        addressed = output / (
            f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
        )
        stable = output / "manifest.json"
        addressed_temporary = _write_temporary(addressed, payload)
        stable_temporary = _write_temporary(stable, payload)
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
            "schema": SCHEMA,
            "status": STATUS,
            "manifest_path": str(stable),
            "content_addressed_manifest_path": str(addressed),
            "content_sha256": content_sha,
            "manifest_file_sha256": file_sha,
            "partitions": [str(build.final_path) for build in builds],
            "summary": manifest["summary"],
            "workers_requested": workers,
            "workers_used": min(workers, len(builds)),
            "comparison_authorized": False,
            "network_request_count": 0,
        }
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)


def _validate_transition(
    transition: Mapping[str, Any],
    *,
    trace: Sequence[Mapping[str, Any]],
    expected_trace_index: int,
    expected_player_guid: str | None,
) -> None:
    trace_index = _nonnegative_integer(
        transition.get("trace_index"), label="transition trace_index"
    )
    if trace_index != expected_trace_index or trace_index >= len(trace):
        raise ChronicleExternalTeamWaveModelV2Error(
            "transition trace reference differs from exact trace indices"
        )
    source = trace[trace_index]
    expected_kind = (
        "EXACT_PLAYER_EVENT"
        if expected_player_guid is not None
        else "UNATTRIBUTED_EVENT"
    )
    if (
        source.get("trace_kind") != expected_kind
        or source.get("player_guid") != expected_player_guid
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "transition references a different actor lane"
        )
    if transition.get("order_key") != source.get("order_key"):
        raise ChronicleExternalTeamWaveModelV2Error(
            "transition order differs from exact trace"
        )
    if (
        transition.get("feature_cutoff_is_strict_prefix") is not True
        or transition.get("current_event_present_in_state_before") is not False
        or transition.get("future_outcomes_in_state_before") is not False
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "transition prefix boundary was weakened"
        )
    state = _mapping(transition.get("state_before"), label="state_before")
    _assert_prefix_contract(state)
    if state.get("prefix_trace_event_count") != trace_index:
        raise ChronicleExternalTeamWaveModelV2Error(
            "state_before does not end at the strict trace prefix"
        )
    if state.get("prefix_trace_exclusive_index") != trace_index:
        raise ChronicleExternalTeamWaveModelV2Error(
            "state replay reference is not the strict trace prefix"
        )
    expected_model_prefix = sum(
        1
        for earlier in trace[:trace_index]
        if earlier.get("trace_kind")
        in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}
    )
    if state.get("prefix_player_or_unattributed_event_count") != expected_model_prefix:
        raise ChronicleExternalTeamWaveModelV2Error(
            "state_before model-event count is not a strict prefix"
        )
    for key in (
        "classification_prefix_state_ref",
        "observed_target_prefix_state_ref",
    ):
        state_ref = _mapping(state.get(key), label=f"state_before.{key}")
        _sha(state_ref.get("content_sha256"), label=f"state_before.{key}.sha256")
    recent = _array(
        state.get("actor_recent_exact_trace_indices"),
        label="actor_recent_exact_trace_indices",
    )
    if any(
        _nonnegative_integer(value, label="actor recent trace index") >= trace_index
        for value in recent
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "actor recent trace reference is not a strict prefix"
        )
    event = _mapping(source.get("event"), label="transition source event")
    current = _mapping(
        transition.get("current_event_label"), label="current_event_label"
    )
    if (
        event.get("event_type") not in ACTION_EVENT_TYPES
        or current.get("event_type") != event.get("event_type")
        or current.get("learning_role") != "OBSERVED_ACTION_LABEL"
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "action transition label differs from START/GO/FAIL exact trace event"
        )


def _validate_model_wave(
    record: Mapping[str, Any],
    *,
    instance_id: str,
    expected_contamination: Mapping[str, Any],
) -> dict[str, int]:
    if (
        record.get("schema") != PARTITION_RECORD_SCHEMA
        or record.get("implementation_revision") != IMPLEMENTATION_REVISION
        or record.get("status") != STATUS
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "unsupported team-wave model record"
        )
    _verify_content_address(record, label="team-wave model record")
    wave = _mapping(record.get("wave"), label="model wave identity")
    if wave.get("instance_id") != instance_id:
        raise ChronicleExternalTeamWaveModelV2Error(
            "model wave crossed its instance partition"
        )
    raid_provenance = _mapping(
        record.get("raid_provenance"), label="model raid_provenance"
    )
    if dict(
        _mapping(
            raid_provenance.get("contamination"),
            label="model raid contamination",
        )
    ) != dict(expected_contamination):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model wave cohort assignment differs from the bound receipt"
        )
    trace = [
        _mapping(value, label="exact trace row")
        for value in _array(record.get("exact_trace"), label="exact_trace")
    ]
    prior_order: tuple[int, ...] | None = None
    exact_event_count = 0
    classification_count = 0
    death_count = 0
    negative_damage_diagnostic_count = 0
    negative_damage_signed_amount = 0
    negative_damage_absolute_amount = 0
    damage_amount = 0
    for index, row in enumerate(trace):
        if row.get("trace_index") != index:
            raise ChronicleExternalTeamWaveModelV2Error(
                "exact trace indices are not contiguous"
            )
        order_raw = _array(row.get("order_key"), label="trace order_key")
        if len(order_raw) != 5:
            raise ChronicleExternalTeamWaveModelV2Error(
                "trace order_key must contain five integers"
            )
        order = tuple(
            _nonnegative_integer(value, label="trace order component")
            for value in order_raw
        )
        if prior_order is not None and order <= prior_order:
            raise ChronicleExternalTeamWaveModelV2Error(
                "model exact trace order is not strict"
            )
        prior_order = order
        kind = _text(row.get("trace_kind"), label="trace_kind")
        if kind == "CLASSIFICATION_CONTEXT":
            classification_count += 1
            if "event" in row:
                raise ChronicleExternalTeamWaveModelV2Error(
                    "classification trace cannot impersonate an event"
                )
            continue
        event = _mapping(row.get("event"), label="trace event")
        event_type = _text(event.get("event_type"), label="trace event_type")
        if kind == "DEATH_MARKER":
            death_count += 1
            if event_type != "DEAD" or "damage" in event:
                raise ChronicleExternalTeamWaveModelV2Error(
                    "DEAD marker became damage-bearing"
                )
        elif kind == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT":
            signed = _negative_damage_signed_amount(event)
            negative_damage_diagnostic_count += 1
            negative_damage_signed_amount += signed
            negative_damage_absolute_amount += abs(signed)
        elif kind in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}:
            exact_event_count += 1
            if event_type == "DEAD":
                raise ChronicleExternalTeamWaveModelV2Error(
                    "DEAD entered a player/unattributed transition lane"
                )
            if event_type == "DMG":
                damage_amount += _event_amount(event, "DMG")
        else:
            raise ChronicleExternalTeamWaveModelV2Error(
                f"unknown exact trace kind {kind}"
            )

    referenced: list[int] = []
    player_count = 0
    fury_count = 0
    arms_count = 0
    unknown_warrior_count = 0
    transition_count = 0
    seen_guids: set[str] = set()
    for raw_player in _array(record.get("players"), label="model players"):
        player_record = _mapping(raw_player, label="model player")
        _verify_content_address(player_record, label="model player")
        player = _mapping(player_record.get("player"), label="model player metadata")
        guid = _text(player.get("guid"), label="model player guid")
        if guid in seen_guids:
            raise ChronicleExternalTeamWaveModelV2Error(
                "duplicate model player GUID"
            )
        seen_guids.add(guid)
        _text(player.get("class"), label="model player class")
        # Race is preserved exactly and may be absent in upstream metadata.
        if player.get("race") is not None and not isinstance(player.get("race"), str):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model player race must be text or null"
            )
        spec = _mapping(
            player_record.get("warrior_spec_lane"), label="warrior_spec_lane"
        )
        partition_key = _text(
            spec.get("partition_key"), label="spec partition_key"
        )
        if spec.get("voting_authorized") is not False:
            raise ChronicleExternalTeamWaveModelV2Error(
                "spec lane unexpectedly authorizes voting"
            )
        player_contamination = _mapping(
            player_record.get("contamination_lane"),
            label="model player contamination_lane",
        )
        if dict(player_contamination) != dict(expected_contamination):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model player cohort assignment differs from the bound receipt"
            )
        expected_candidate = expected_contamination.get(
            "candidate_filter_passed"
        ) is True
        eligibility = _mapping(
            player_record.get("eligibility_observation"),
            label="model player eligibility_observation",
        )
        if (
            eligibility.get("team_behavior_candidate_filter_passed")
            is not expected_candidate
            or eligibility.get("historical_fury_candidate_filter_passed")
            is not (
                expected_candidate and partition_key == "WARRIOR_FURY"
            )
            or eligibility.get("arms_diagnostic_candidate_filter_passed")
            is not (
                expected_candidate and partition_key == "WARRIOR_ARMS"
            )
            or eligibility.get("voting_authorized") is not False
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model player eligibility differs from receipt/spec assignment"
            )
        if partition_key == "WARRIOR_FURY":
            fury_count += 1
        elif partition_key == "WARRIOR_ARMS":
            arms_count += 1
        elif str(player.get("class") or "").upper() == "WARRIOR":
            unknown_warrior_count += 1
        indices = [
            _nonnegative_integer(value, label="player exact_trace_index")
            for value in _array(
                player_record.get("exact_trace_indices"),
                label="player exact_trace_indices",
            )
        ]
        if indices != sorted(set(indices)) or any(
            index >= len(trace)
            or trace[index].get("trace_kind") != "EXACT_PLAYER_EVENT"
            or trace[index].get("player_guid") != guid
            for index in indices
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "player exact_trace_indices cross actor lane or are not strict"
            )
        transitions = [
            _mapping(value, label="player transition")
            for value in _array(
                player_record.get("prefix_transitions"),
                label="player prefix_transitions",
            )
        ]
        outcome_indices = [
            _nonnegative_integer(value, label="player outcome trace index")
            for value in _array(
                player_record.get("outcome_context_trace_indices"),
                label="player outcome_context_trace_indices",
            )
        ]
        expected_action_indices = [
            index
            for index in indices
            if _mapping(trace[index].get("event"), label="player trace event").get(
                "event_type"
            )
            in ACTION_EVENT_TYPES
        ]
        expected_outcome_indices = [
            index
            for index in indices
            if _mapping(trace[index].get("event"), label="player trace event").get(
                "event_type"
            )
            in AMOUNT_EVENT_TYPES
        ]
        if outcome_indices != expected_outcome_indices or len(transitions) != len(
            expected_action_indices
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "player action transitions/outcome contexts do not partition exact events"
            )
        for index, transition in zip(
            expected_action_indices, transitions, strict=True
        ):
            _validate_transition(
                transition,
                trace=trace,
                expected_trace_index=index,
                expected_player_guid=guid,
            )
        referenced.extend(indices)
        transition_count += len(transitions)
        player_count += 1
    unattributed = _mapping(
        record.get("unattributed_episode"), label="unattributed_episode"
    )
    indices = [
        _nonnegative_integer(value, label="unattributed exact_trace_index")
        for value in _array(
            unattributed.get("exact_trace_indices"),
            label="unattributed exact_trace_indices",
        )
    ]
    if indices != sorted(set(indices)) or any(
        index >= len(trace)
        or trace[index].get("trace_kind") != "UNATTRIBUTED_EVENT"
        or trace[index].get("player_guid") is not None
        for index in indices
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "unattributed exact_trace_indices cross actor lane or are not strict"
        )
    transitions = [
        _mapping(value, label="unattributed transition")
        for value in _array(
            unattributed.get("prefix_transitions"),
            label="unattributed prefix_transitions",
        )
    ]
    outcome_indices = [
        _nonnegative_integer(value, label="unattributed outcome trace index")
        for value in _array(
            unattributed.get("outcome_context_trace_indices"),
            label="unattributed outcome_context_trace_indices",
        )
    ]
    expected_action_indices = [
        index
        for index in indices
        if _mapping(trace[index].get("event"), label="unattributed trace event").get(
            "event_type"
        )
        in ACTION_EVENT_TYPES
    ]
    expected_outcome_indices = [
        index
        for index in indices
        if _mapping(trace[index].get("event"), label="unattributed trace event").get(
            "event_type"
        )
        in AMOUNT_EVENT_TYPES
    ]
    if outcome_indices != expected_outcome_indices or len(transitions) != len(
        expected_action_indices
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "unattributed action transitions/outcome contexts do not partition exact events"
        )
    for index, transition in zip(expected_action_indices, transitions, strict=True):
        _validate_transition(
            transition,
            trace=trace,
            expected_trace_index=index,
            expected_player_guid=None,
        )
    referenced.extend(indices)
    transition_count += len(transitions)
    expected_references = sorted(
        row["trace_index"]
        for row in trace
        if row.get("trace_kind")
        in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}
    )
    if sorted(referenced) != expected_references or len(set(referenced)) != len(referenced):
        raise ChronicleExternalTeamWaveModelV2Error(
            "player/unattributed episode trace indices do not partition exact events"
        )
    outcome = _mapping(record.get("descriptive_outcome"), label="descriptive_outcome")
    if outcome.get("allowed_in_state_before") is not False:
        raise ChronicleExternalTeamWaveModelV2Error(
            "descriptive outcome was authorized as a prefix feature"
        )
    negative_indices = [
        row["trace_index"]
        for row in trace
        if row.get("trace_kind") == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT"
    ]
    if outcome.get("negative_damage_diagnostic_trace_indices") != negative_indices:
        raise ChronicleExternalTeamWaveModelV2Error(
            "descriptive outcome negative-DMG trace indices differ from exact trace"
        )
    timeline_accounting = _mapping(
        outcome.get("event_accounting"), label="descriptive timeline accounting"
    )
    timeline_damage = _mapping(
        timeline_accounting.get("damage"), label="descriptive timeline damage"
    )
    if (
        timeline_damage.get("amount") != damage_amount
        or timeline_damage.get("negative_event_count_excluded")
        != negative_damage_diagnostic_count
        or timeline_damage.get("negative_signed_amount_excluded")
        != negative_damage_signed_amount
        or timeline_damage.get("absolute_amount_excluded")
        != negative_damage_absolute_amount
        or timeline_damage.get("policy") != NEGATIVE_DAMAGE_VALUE_POLICY
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "descriptive timeline negative-DMG accounting differs from exact trace"
        )
    boundaries = _mapping(
        record.get("scientific_boundaries"), label="scientific_boundaries"
    )
    if (
        boundaries.get("comparison_authorized") is not False
        or boundaries.get("policy_training_authorized") is not False
        or boundaries.get("dead_marker_damage_added") != 0
        or boundaries.get("negative_damage_value_policy")
        != NEGATIVE_DAMAGE_VALUE_POLICY
        or boundaries.get("negative_damage_abs_or_clamp_used") is not False
        or boundaries.get("negative_damage_or_reward_added") != 0
        or boundaries.get("future_death_or_final_total_in_state_before") is not False
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model wave scientific boundary was widened"
        )
    summary = _mapping(record.get("summary"), label="model wave summary")
    observed = {
        "player_episode_count": player_count,
        "fury_episode_count": fury_count,
        "arms_episode_count": arms_count,
        "unknown_warrior_episode_count": unknown_warrior_count,
        "transition_count": transition_count,
        "exact_trace_count": len(trace),
        "exact_event_count": (
            exact_event_count + death_count + negative_damage_diagnostic_count
        ),
        "classification_context_count": classification_count,
        "death_marker_count": death_count,
        "negative_damage_diagnostic_count": negative_damage_diagnostic_count,
        "negative_damage_signed_amount_excluded": (
            negative_damage_signed_amount
        ),
        "negative_damage_absolute_amount_excluded": (
            negative_damage_absolute_amount
        ),
        "damage_amount": damage_amount,
        "dead_marker_damage_added": 0,
    }
    if any(summary.get(key) != value for key, value in observed.items()):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model wave summary differs from validated trace/episodes"
        )
    return observed


def load_external_team_wave_model_manifest(
    path_value: str | Path,
) -> tuple[dict[str, Any], Path]:
    """Load and fully revalidate a committed External team-wave V2 artifact."""

    requested = Path(path_value).expanduser().resolve()
    manifest = _load_json(requested, label="team-wave model manifest")
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("kind") != KIND
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
        or manifest.get("status") != STATUS
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "unsupported team-wave model manifest"
        )
    content_sha = _verify_content_address(manifest, label="model manifest")
    payload = _canonical_bytes(manifest) + b"\n"
    if requested.read_bytes() != payload:
        raise ChronicleExternalTeamWaveModelV2Error(
            "model manifest is not canonical JSON"
        )
    stable = requested.parent / "manifest.json"
    addressed = requested.parent / (
        f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
    )
    for candidate, label in ((stable, "stable"), (addressed, "addressed")):
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or candidate.read_bytes() != payload
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                f"model {label} manifest is missing or differs"
            )
    root = _data_root(stable)
    input_closure = _mapping(manifest.get("input_closure"), label="input_closure")
    timeline_binding = _mapping(
        input_closure.get(
            "timeline_manifest"
        ),
        label="timeline_manifest binding",
    )
    receipt_binding = _mapping(
        input_closure.get("cohort_receipt"), label="cohort_receipt binding"
    )
    timeline_path = _resolve_relative(
        root,
        timeline_binding.get("stable_path"),
        root,
        label="bound timeline stable manifest",
    )
    receipt_path = _resolve_relative(
        root,
        receipt_binding.get("content_addressed_path"),
        root,
        label="bound cohort receipt",
    )
    closure = _load_input_closure(
        timeline_path, cohort_receipt_path=receipt_path
    )
    if (
        closure.content_sha256 != timeline_binding.get("content_sha256")
        or closure.file_sha256 != timeline_binding.get("file_sha256")
        or closure.manifest_path.stat().st_size != timeline_binding.get("size_bytes")
        or _relative(
            closure.addressed_path,
            root,
            label="bound timeline addressed manifest",
        )
        != timeline_binding.get("content_addressed_path")
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "live timeline input differs from model input closure"
        )
    if dict(receipt_binding) != dict(closure.cohort_receipt_binding):
        raise ChronicleExternalTeamWaveModelV2Error(
            "live cohort receipt differs from model input closure"
        )
    entries = [
        _mapping(value, label="model instance entry")
        for value in _array(manifest.get("instances"), label="model instances")
    ]
    order = _array(manifest.get("instance_order"), label="model instance_order")
    if len(entries) != len(order) or not entries:
        raise ChronicleExternalTeamWaveModelV2Error(
            "model instances and order differ"
        )
    timeline_by_id = {context.instance_id: context for context in closure.instances}
    validated_entries: list[Mapping[str, Any]] = []
    for position, entry in enumerate(entries):
        _verify_content_address(entry, label="model instance entry")
        instance_id = _safe_component(entry.get("instance_id"), label="instance_id")
        if order[position] != instance_id or instance_id not in timeline_by_id:
            raise ChronicleExternalTeamWaveModelV2Error(
                "model instance order/set differs from bound timeline"
            )
        timeline_context = timeline_by_id[instance_id]
        entry_contamination = _mapping(
            entry.get("contamination_lane"), label="model instance contamination lane"
        )
        if dict(entry_contamination) != dict(timeline_context.contamination):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model instance cohort assignment differs from bound receipt"
            )
        timeline_input = _mapping(entry.get("timeline_input"), label="timeline_input")
        if (
            timeline_input.get("instance_entry_content_sha256")
            != _verify_content_address(
                timeline_context.entry, label="bound timeline instance entry"
            )
            or timeline_input.get("source_binding_sha256")
            != timeline_context.source_binding_sha256
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model instance binding differs from bound timeline instance"
            )
        partition = _mapping(entry.get("partition"), label="model partition")
        partition_path = _resolve_relative(
            stable.parent,
            partition.get("path"),
            root,
            label="model partition path",
        )
        expected_size = _nonnegative_integer(
            partition.get("compressed_size_bytes"),
            label="model compressed_size_bytes",
        )
        if partition_path.stat().st_size != expected_size:
            raise ChronicleExternalTeamWaveModelV2Error(
                "model partition compressed size mismatch"
            )
        if _sha256_file(partition_path) != partition.get("compressed_file_sha256"):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model partition compressed hash mismatch"
            )
        logical = hashlib.sha256()
        record_count = 0
        encounter_ids: set[str] = set()
        totals: Counter[str] = Counter()
        prior_wave: tuple[int, int, str] | None = None
        try:
            with gzip.open(partition_path, "rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    logical.update(raw_line)
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ChronicleExternalTeamWaveModelV2Error(
                            f"invalid model partition row {line_number}: {error}"
                        ) from error
                    row = _mapping(value, label="model partition row")
                    if raw_line != _canonical_bytes(row) + b"\n":
                        raise ChronicleExternalTeamWaveModelV2Error(
                            "model partition row is not canonical JSONL"
                        )
                    wave_identity = _mapping(row.get("wave"), label="wave identity")
                    wave_order = (
                        _nonnegative_integer(
                            wave_identity.get("encounter_ordinal"),
                            label="encounter_ordinal",
                        ),
                        _nonnegative_integer(
                            wave_identity.get("wave_ordinal"), label="wave_ordinal"
                        ),
                        _text(wave_identity.get("wave_id"), label="wave_id"),
                    )
                    if prior_wave is not None and wave_order <= prior_wave:
                        raise ChronicleExternalTeamWaveModelV2Error(
                            "model partition wave order is not strict"
                        )
                    prior_wave = wave_order
                    observed = _validate_model_wave(
                        row,
                        instance_id=instance_id,
                        expected_contamination=timeline_context.contamination,
                    )
                    for key, amount in observed.items():
                        if key != "damage_amount" and key != "dead_marker_damage_added":
                            totals[key] += amount
                    encounter_ids.add(
                        _text(
                            wave_identity.get("encounter_id"), label="encounter_id"
                        )
                    )
                    record_count += 1
        except ChronicleExternalTeamWaveModelV2Error:
            raise
        except OSError as error:
            raise ChronicleExternalTeamWaveModelV2Error(
                f"cannot stream model partition {partition_path}: {error}"
            ) from error
        if (
            record_count != partition.get("record_count")
            or logical.hexdigest() != partition.get("logical_content_sha256")
        ):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model partition logical identity/count mismatch"
            )
        declared = _mapping(entry.get("summary"), label="model instance summary")
        derived = {
            "encounter_count": len(encounter_ids),
            "wave_count": record_count,
            "player_wave_episode_count": totals["player_episode_count"],
            "fury_episode_count": totals["fury_episode_count"],
            "arms_episode_count": totals["arms_episode_count"],
            "unknown_warrior_episode_count": totals[
                "unknown_warrior_episode_count"
            ],
            "prefix_transition_count": totals["transition_count"],
            "exact_trace_count": totals["exact_trace_count"],
            "exact_event_count": totals["exact_event_count"],
            "classification_context_count": totals[
                "classification_context_count"
            ],
            "death_marker_count": totals["death_marker_count"],
            "negative_damage_diagnostic_count": totals[
                "negative_damage_diagnostic_count"
            ],
            "negative_damage_signed_amount_excluded": totals[
                "negative_damage_signed_amount_excluded"
            ],
            "negative_damage_absolute_amount_excluded": totals[
                "negative_damage_absolute_amount_excluded"
            ],
        }
        if any(declared.get(key) != value for key, value in derived.items()):
            raise ChronicleExternalTeamWaveModelV2Error(
                "model instance summary differs from partition records"
            )
        validated_entries.append(entry)
    if set(order) != set(timeline_by_id) or len(order) != len(timeline_by_id):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model omitted or added a timeline instance"
        )
    if manifest.get("summary") != _manifest_summary(validated_entries):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model manifest summary differs from instance entries"
        )
    summary = _mapping(manifest.get("summary"), label="model summary")
    if (
        summary.get("instance_count") != len(closure.instances)
        or summary.get("training_candidate_instance_count")
        != len(closure.training_instance_ids)
        or summary.get("descriptive_nontraining_instance_count")
        != len(closure.descriptive_nontraining_instance_ids)
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model summary differs from the exact receipt cohort"
        )
    contamination_contract = _mapping(
        manifest.get("spec_and_contamination_contract"),
        label="spec_and_contamination_contract",
    )
    if (
        contamination_contract.get("training_candidate_authority")
        != "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP"
        or contamination_contract.get(
            "raw_contamination_label_may_expand_training_cohort"
        )
        is not False
        or contamination_contract.get("cohort_receipt_strict_full_source_replay")
        is not True
        or contamination_contract.get("descriptive_instance_count")
        != len(closure.instances)
        or contamination_contract.get("training_candidate_instance_count")
        != len(closure.training_instance_ids)
        or contamination_contract.get("descriptive_nontraining_instance_count")
        != len(closure.descriptive_nontraining_instance_ids)
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model receipt cohort contract was weakened"
        )
    graph = _mapping(manifest.get("split_graph"), label="split_graph")
    if (
        graph.get("required_split_unit") != "connected component"
        or graph.get("row_random_split_allowed") is not False
        or graph.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model component split contract was weakened"
        )
    boundaries = _mapping(
        manifest.get("scientific_boundaries"), label="scientific_boundaries"
    )
    if (
        boundaries.get("comparison_authorized") is not False
        or boundaries.get("policy_training_authorized") is not False
        or boundaries.get("historical_policy_adapter_present") is not False
        or boundaries.get("superiority_claim") is not False
    ):
        raise ChronicleExternalTeamWaveModelV2Error(
            "model manifest scientific boundary was widened"
        )
    return manifest, stable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build External API prefix-causal per-player team-wave evidence V2"
    )
    parser.add_argument(
        "--timeline-manifest", type=Path, default=DEFAULT_TIMELINE_MANIFEST
    )
    parser.add_argument("--cohort-receipt", type=Path, required=True)
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
        result = build_external_team_wave_model(
            timeline_manifest_path=args.timeline_manifest,
            cohort_receipt_path=args.cohort_receipt,
            output_directory=args.output_dir,
            workers=args.workers,
        )
    except ChronicleExternalTeamWaveModelV2Error as error:
        print(f"Chronicle External team-wave model V2 failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ChronicleExternalTeamWaveModelV2Error",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_TIMELINE_MANIFEST",
    "IMPLEMENTATION_REVISION",
    "MAX_WORKERS",
    "PARTITION_RECORD_SCHEMA",
    "SCHEMA",
    "STATUS",
    "build_external_team_wave_model",
    "load_external_team_wave_model_manifest",
    "main",
]
