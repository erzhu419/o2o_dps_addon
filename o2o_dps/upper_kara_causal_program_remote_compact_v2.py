"""Compact storage contract for causal-program training shards.

The v1 worker embeds the complete candidate family in every seed-shard
terminal.  This module separates that invariant family from shard-local lane
results without changing the objects consumed by the freeze reducer:

* one candidate manifest retains the full v1 program receipts;
* one compact sidecar retains the v1 lane rows for a seed shard; and
* one small terminal is the commit marker inspected by orchestration.

The conversion and restoration functions are pure.  The write helpers use
create-only atomic publication; a sidecar is published before its terminal.
There is deliberately no digest identity or parent/delta representation.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .causal_action_program_v1 import causal_action_program_from_dict_v1
from .upper_kara_causal_program_remote_contract_v1 import (
    TERMINAL_COMPLETE,
    TERMINAL_FAILED,
    TERMINAL_INVALID,
    terminal_status_for_lanes_v1,
)


JSONMap = dict[str, Any]
LEGACY_TRAIN_TERMINAL_SCHEMA_V1 = (
    "upper_kara_causal_program_remote_train_shard/v1"
)
CANDIDATE_MANIFEST_SCHEMA_V2 = (
    "upper_kara_causal_program_remote_candidate_manifest/v2"
)
TRAIN_LANE_SIDECAR_SCHEMA_V2 = (
    "upper_kara_causal_program_remote_train_lane_sidecar/v2"
)
TRAIN_TERMINAL_COMMIT_SCHEMA_V2 = (
    "upper_kara_causal_program_remote_train_commit/v2"
)

_PROGRAM_RECEIPT_FIELDS = frozenset(
    {
        "program_ref",
        "program_id",
        "program_key",
        "program_origin",
        "proposal_guide_ids",
        "program",
    }
)
_LANE_FIELDS = frozenset(
    {
        "master_seed",
        "simulator_seed",
        "status",
        "own_effective_damage",
        "own_effective_dps",
        "ttk_ms",
        "error",
        "program_ref",
        "first_wave_arrival_ms",
    }
)
_CANDIDATE_STORAGE_CONTRACT = {
    "program_receipts_materialized_once_per_loadout": True,
    "programs_stored_as_full_wire_objects": True,
}
_SIDECAR_STORAGE_CONTRACT = {
    "candidate_programs_embedded": False,
    "v1_lane_rows_preserved": True,
}
_TERMINAL_STORAGE_CONTRACT = {
    "candidate_programs_embedded": False,
    "lane_rows_embedded": False,
    "lane_sidecar_published_before_terminal": True,
}


@dataclass(frozen=True)
class CompactTrainingArtifactsV2:
    candidate_manifest: JSONMap
    lane_sidecar: JSONMap
    terminal: JSONMap


def _mapping(value: object, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _exact_mapping(value: object, fields: frozenset[str], label: str) -> JSONMap:
    result = _mapping(value, label)
    if set(result) != fields:
        raise ValueError(f"{label} fields do not match its schema")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_nonnegative(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} must be finite and non-negative")
    return float(value)


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result = [_text(row, f"{label} row") for row in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _examples(value: object, label: str) -> list[JSONMap]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    rows: list[JSONMap] = []
    seeds: set[int] = set()
    for raw in value:
        row = _exact_mapping(
            raw, frozenset({"seed", "first_wave_arrival_ms"}), f"{label} row"
        )
        seed = _integer(row["seed"], f"{label}.seed")
        arrival = _integer(
            row["first_wave_arrival_ms"], f"{label}.first_wave_arrival_ms"
        )
        if seed in seeds:
            raise ValueError(f"{label} seeds must be unique")
        seeds.add(seed)
        rows.append({"seed": seed, "first_wave_arrival_ms": arrival})
    return rows


def _program_receipts(value: object) -> list[JSONMap]:
    if not isinstance(value, list) or not value:
        raise ValueError("candidate programs must be a non-empty list")
    receipts: list[JSONMap] = []
    refs: list[str] = []
    ids: list[str] = []
    keys: list[str] = []
    for raw in value:
        row = _exact_mapping(raw, _PROGRAM_RECEIPT_FIELDS, "program receipt")
        program = causal_action_program_from_dict_v1(row["program"])
        proposal_guide_ids = _string_list(
            row["proposal_guide_ids"], "program receipt proposal_guide_ids"
        )
        if (
            row["program_ref"] != program.program_id
            or row["program_id"] != program.program_id
            or row["program_key"] != program.program_key()
            or row["program_origin"] != program.origin.value
        ):
            raise ValueError("program receipt identity mismatch")
        parsed = deepcopy(row)
        parsed["proposal_guide_ids"] = proposal_guide_ids
        receipts.append(parsed)
        refs.append(program.program_id)
        ids.append(program.program_id)
        keys.append(program.program_key())
    if (
        len(refs) != len(set(refs))
        or len(ids) != len(set(ids))
        or len(keys) != len(set(keys))
    ):
        raise ValueError("program receipts must have unique identities")
    return receipts


def _lanes(value: object) -> list[JSONMap]:
    if not isinstance(value, list) or not value:
        raise ValueError("lane sidecar lanes must be a non-empty list")
    lanes: list[JSONMap] = []
    for raw in value:
        row = _exact_mapping(raw, _LANE_FIELDS, "lane")
        _integer(row["master_seed"], "lane.master_seed")
        _integer(row["simulator_seed"], "lane.simulator_seed")
        _integer(
            row["first_wave_arrival_ms"], "lane.first_wave_arrival_ms"
        )
        _text(row["program_ref"], "lane.program_ref")
        status = row["status"]
        if status not in {TERMINAL_COMPLETE, TERMINAL_INVALID, TERMINAL_FAILED}:
            raise ValueError("lane has an invalid status")
        metrics = (
            row["own_effective_damage"],
            row["own_effective_dps"],
            row["ttk_ms"],
        )
        if status == TERMINAL_COMPLETE:
            _finite_nonnegative(metrics[0], "lane.own_effective_damage")
            _finite_nonnegative(metrics[1], "lane.own_effective_dps")
            _integer(metrics[2], "lane.ttk_ms", minimum=1)
        elif any(metric is not None for metric in metrics):
            raise ValueError("invalid or failed lane metrics must be null")
        if row["error"] is not None and not isinstance(row["error"], str):
            raise ValueError("lane.error must be a string or null")
        lanes.append(deepcopy(row))
    return lanes


def build_candidate_manifest_v2(
    *,
    campaign_id: str,
    build_id: str,
    campaign_contract: Mapping[str, Any],
    loadout_id: str,
    proposal_guide_ids: Sequence[str],
    programs: Sequence[Mapping[str, Any]],
) -> JSONMap:
    payload = {
        "schema": CANDIDATE_MANIFEST_SCHEMA_V2,
        "campaign_id": campaign_id,
        "build_id": build_id,
        "campaign_contract": deepcopy(dict(campaign_contract)),
        "loadout_id": loadout_id,
        "proposal_guide_ids": list(proposal_guide_ids),
        "programs": [deepcopy(dict(row)) for row in programs],
        "storage_contract": dict(_CANDIDATE_STORAGE_CONTRACT),
    }
    return validate_candidate_manifest_v2(payload)


def validate_candidate_manifest_v2(value: object) -> JSONMap:
    row = _exact_mapping(
        value,
        frozenset(
            {
                "schema",
                "campaign_id",
                "build_id",
                "campaign_contract",
                "loadout_id",
                "proposal_guide_ids",
                "programs",
                "storage_contract",
            }
        ),
        "candidate manifest",
    )
    if row["schema"] != CANDIDATE_MANIFEST_SCHEMA_V2:
        raise ValueError("candidate manifest schema mismatch")
    row["campaign_id"] = _text(row["campaign_id"], "candidate campaign_id")
    row["build_id"] = _text(row["build_id"], "candidate build_id")
    row["loadout_id"] = _text(row["loadout_id"], "candidate loadout_id")
    row["campaign_contract"] = deepcopy(
        _mapping(row["campaign_contract"], "candidate campaign_contract")
    )
    row["proposal_guide_ids"] = _string_list(
        row["proposal_guide_ids"], "candidate proposal_guide_ids"
    )
    row["programs"] = _program_receipts(row["programs"])
    if row["storage_contract"] != _CANDIDATE_STORAGE_CONTRACT:
        raise ValueError("candidate storage contract mismatch")
    row["storage_contract"] = dict(_CANDIDATE_STORAGE_CONTRACT)
    return row


def build_training_lane_sidecar_v2(
    candidate_manifest: Mapping[str, Any],
    *,
    candidate_manifest_ref: str,
    seed_shard_index: int,
    examples: Sequence[Mapping[str, Any]],
    lanes: Sequence[Mapping[str, Any]],
) -> JSONMap:
    manifest = validate_candidate_manifest_v2(candidate_manifest)
    payload = {
        "schema": TRAIN_LANE_SIDECAR_SCHEMA_V2,
        "campaign_id": manifest["campaign_id"],
        "build_id": manifest["build_id"],
        "loadout_id": manifest["loadout_id"],
        "seed_shard_index": seed_shard_index,
        "examples": [deepcopy(dict(row)) for row in examples],
        "candidate_manifest_ref": candidate_manifest_ref,
        "lanes": [deepcopy(dict(row)) for row in lanes],
        "storage_contract": dict(_SIDECAR_STORAGE_CONTRACT),
    }
    return validate_training_lane_sidecar_v2(payload)


def validate_training_lane_sidecar_v2(value: object) -> JSONMap:
    row = _exact_mapping(
        value,
        frozenset(
            {
                "schema",
                "campaign_id",
                "build_id",
                "loadout_id",
                "seed_shard_index",
                "examples",
                "candidate_manifest_ref",
                "lanes",
                "storage_contract",
            }
        ),
        "training lane sidecar",
    )
    if row["schema"] != TRAIN_LANE_SIDECAR_SCHEMA_V2:
        raise ValueError("training lane sidecar schema mismatch")
    row["campaign_id"] = _text(row["campaign_id"], "sidecar campaign_id")
    row["build_id"] = _text(row["build_id"], "sidecar build_id")
    row["loadout_id"] = _text(row["loadout_id"], "sidecar loadout_id")
    row["seed_shard_index"] = _integer(
        row["seed_shard_index"], "sidecar seed_shard_index"
    )
    row["examples"] = _examples(row["examples"], "sidecar examples")
    row["candidate_manifest_ref"] = _text(
        row["candidate_manifest_ref"], "sidecar candidate_manifest_ref"
    )
    row["lanes"] = _lanes(row["lanes"])
    if row["storage_contract"] != _SIDECAR_STORAGE_CONTRACT:
        raise ValueError("sidecar storage contract mismatch")
    row["storage_contract"] = dict(_SIDECAR_STORAGE_CONTRACT)
    return row


def build_training_terminal_commit_v2(
    candidate_manifest: Mapping[str, Any],
    lane_sidecar: Mapping[str, Any],
    *,
    lane_sidecar_ref: str,
    terminal_status: str,
    lane_status_counts: Mapping[str, int],
    metric: Mapping[str, Any] | None,
    completed_at: str,
    training_contract: Mapping[str, Any],
) -> JSONMap:
    manifest = validate_candidate_manifest_v2(candidate_manifest)
    sidecar = validate_training_lane_sidecar_v2(lane_sidecar)
    payload = {
        "schema": TRAIN_TERMINAL_COMMIT_SCHEMA_V2,
        "terminal_status": terminal_status,
        "campaign_id": sidecar["campaign_id"],
        "build_id": sidecar["build_id"],
        "loadout_id": sidecar["loadout_id"],
        "seed_shard_index": sidecar["seed_shard_index"],
        "examples": deepcopy(sidecar["examples"]),
        "candidate_manifest_ref": sidecar["candidate_manifest_ref"],
        "lane_sidecar_ref": lane_sidecar_ref,
        "program_count": len(manifest["programs"]),
        "lane_count": len(sidecar["lanes"]),
        "lane_status_counts": dict(lane_status_counts),
        "metric": deepcopy(dict(metric)) if metric is not None else None,
        "completed_at": completed_at,
        "training_contract": deepcopy(dict(training_contract)),
        "storage_contract": dict(_TERMINAL_STORAGE_CONTRACT),
    }
    return validate_training_terminal_commit_v2(payload)


def validate_training_terminal_commit_v2(value: object) -> JSONMap:
    row = _exact_mapping(
        value,
        frozenset(
            {
                "schema",
                "terminal_status",
                "campaign_id",
                "build_id",
                "loadout_id",
                "seed_shard_index",
                "examples",
                "candidate_manifest_ref",
                "lane_sidecar_ref",
                "program_count",
                "lane_count",
                "lane_status_counts",
                "metric",
                "completed_at",
                "training_contract",
                "storage_contract",
            }
        ),
        "training terminal commit",
    )
    if row["schema"] != TRAIN_TERMINAL_COMMIT_SCHEMA_V2:
        raise ValueError("training terminal commit schema mismatch")
    if row["terminal_status"] not in {
        TERMINAL_COMPLETE,
        TERMINAL_INVALID,
        TERMINAL_FAILED,
    }:
        raise ValueError("training terminal commit has invalid status")
    for name in ("campaign_id", "build_id", "loadout_id"):
        row[name] = _text(row[name], f"terminal {name}")
    row["seed_shard_index"] = _integer(
        row["seed_shard_index"], "terminal seed_shard_index"
    )
    row["examples"] = _examples(row["examples"], "terminal examples")
    row["candidate_manifest_ref"] = _text(
        row["candidate_manifest_ref"], "terminal candidate_manifest_ref"
    )
    row["lane_sidecar_ref"] = _text(
        row["lane_sidecar_ref"], "terminal lane_sidecar_ref"
    )
    row["program_count"] = _integer(
        row["program_count"], "terminal program_count", minimum=1
    )
    row["lane_count"] = _integer(
        row["lane_count"], "terminal lane_count", minimum=1
    )
    counts = _mapping(row["lane_status_counts"], "terminal lane_status_counts")
    if set(counts) - {TERMINAL_COMPLETE, TERMINAL_INVALID, TERMINAL_FAILED}:
        raise ValueError("terminal lane_status_counts contains an invalid status")
    row["lane_status_counts"] = {
        key: _integer(value, f"terminal lane_status_counts.{key}", minimum=1)
        for key, value in sorted(counts.items())
    }
    if sum(row["lane_status_counts"].values()) != row["lane_count"]:
        raise ValueError("terminal lane count does not match status counts")
    if row["metric"] is not None:
        row["metric"] = deepcopy(_mapping(row["metric"], "terminal metric"))
    row["completed_at"] = _text(row["completed_at"], "terminal completed_at")
    row["training_contract"] = deepcopy(
        _mapping(row["training_contract"], "terminal training_contract")
    )
    if row["storage_contract"] != _TERMINAL_STORAGE_CONTRACT:
        raise ValueError("terminal storage contract mismatch")
    row["storage_contract"] = dict(_TERMINAL_STORAGE_CONTRACT)
    return row


def compact_training_artifacts_from_v1_v2(
    value: Mapping[str, Any],
    *,
    candidate_manifest_ref: str,
    lane_sidecar_ref: str,
) -> CompactTrainingArtifactsV2:
    """Split one current v1 train terminal into the three v2 artifacts."""

    legacy = _mapping(value, "v1 training terminal")
    if legacy.get("schema") != LEGACY_TRAIN_TERMINAL_SCHEMA_V1:
        raise ValueError("v1 training terminal schema mismatch")
    required = {
        "terminal_status",
        "campaign_id",
        "build_id",
        "campaign_contract",
        "loadout_id",
        "seed_shard_index",
        "examples",
        "programs",
        "proposal_guide_ids",
        "lanes",
        "lane_status_counts",
        "metric",
        "completed_at",
        "contract",
    }
    if not required.issubset(legacy):
        raise ValueError("v1 training terminal is missing required fields")
    manifest = build_candidate_manifest_v2(
        campaign_id=legacy["campaign_id"],
        build_id=legacy["build_id"],
        campaign_contract=_mapping(
            legacy["campaign_contract"], "v1 campaign_contract"
        ),
        loadout_id=legacy["loadout_id"],
        proposal_guide_ids=legacy["proposal_guide_ids"],
        programs=legacy["programs"],
    )
    sidecar = build_training_lane_sidecar_v2(
        manifest,
        candidate_manifest_ref=candidate_manifest_ref,
        seed_shard_index=legacy["seed_shard_index"],
        examples=legacy["examples"],
        lanes=legacy["lanes"],
    )
    terminal = build_training_terminal_commit_v2(
        manifest,
        sidecar,
        lane_sidecar_ref=lane_sidecar_ref,
        terminal_status=legacy["terminal_status"],
        lane_status_counts=legacy["lane_status_counts"],
        metric=legacy["metric"],
        completed_at=legacy["completed_at"],
        training_contract=_mapping(legacy["contract"], "v1 training contract"),
    )
    restore_freeze_inputs_v2(manifest, sidecar, terminal)
    return CompactTrainingArtifactsV2(manifest, sidecar, terminal)


def restore_freeze_inputs_v2(
    candidate_manifest: Mapping[str, Any],
    lane_sidecar: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> tuple[tuple[JSONMap, ...], tuple[JSONMap, ...]]:
    """Validate a compact bundle and restore v1 freeze ``programs, lanes``."""

    manifest = validate_candidate_manifest_v2(candidate_manifest)
    sidecar = validate_training_lane_sidecar_v2(lane_sidecar)
    commit = validate_training_terminal_commit_v2(terminal)
    identity_fields = ("campaign_id", "build_id", "loadout_id")
    if any(
        manifest[name] != sidecar[name] or sidecar[name] != commit[name]
        for name in identity_fields
    ):
        raise ValueError("compact training artifact identity mismatch")
    if (
        sidecar["seed_shard_index"] != commit["seed_shard_index"]
        or sidecar["examples"] != commit["examples"]
        or sidecar["candidate_manifest_ref"]
        != commit["candidate_manifest_ref"]
    ):
        raise ValueError("sidecar and terminal shard identity mismatch")
    programs = manifest["programs"]
    lanes = sidecar["lanes"]
    if commit["program_count"] != len(programs) or commit["lane_count"] != len(
        lanes
    ):
        raise ValueError("compact training artifact count mismatch")
    status_counts = dict(sorted(Counter(row["status"] for row in lanes).items()))
    if commit["lane_status_counts"] != status_counts:
        raise ValueError("terminal lane status counts do not match sidecar")
    if commit["terminal_status"] != terminal_status_for_lanes_v1(lanes):
        raise ValueError("terminal status does not match sidecar lanes")

    arrivals = {
        row["seed"]: row["first_wave_arrival_ms"] for row in sidecar["examples"]
    }
    expected = {
        (program["program_ref"], seed)
        for program in programs
        for seed in arrivals
    }
    observed: set[tuple[str, int]] = set()
    for lane in lanes:
        key = (lane["program_ref"], lane["master_seed"])
        if key in observed:
            raise ValueError("lane sidecar contains a duplicate program/seed lane")
        observed.add(key)
        if arrivals.get(lane["master_seed"]) != lane["first_wave_arrival_ms"]:
            raise ValueError("lane arrival does not match its declared example")
    if observed != expected:
        raise ValueError("lane sidecar coverage does not match candidate manifest")
    return tuple(deepcopy(programs)), tuple(deepcopy(lanes))


def _compact_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_create_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_compact_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: str | Path, label: str) -> JSONMap:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    return _mapping(value, label)


def artifact_path_ref_v2(path: str | Path) -> str:
    """Return the one absolute-path reference convention used by v2 bundles."""

    return str(Path(path).expanduser().resolve())


def write_candidate_manifest_v2(
    path: str | Path, candidate_manifest: Mapping[str, Any]
) -> None:
    _atomic_create_json(path, validate_candidate_manifest_v2(candidate_manifest))


def read_candidate_manifest_v2(path: str | Path) -> JSONMap:
    return validate_candidate_manifest_v2(_read_json(path, "candidate manifest"))


def _validate_bundle_path_refs_v2(
    *,
    candidate_manifest_path: str | Path,
    lane_sidecar_path: str | Path,
    lane_sidecar: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> None:
    expected_manifest_ref = artifact_path_ref_v2(candidate_manifest_path)
    expected_sidecar_ref = artifact_path_ref_v2(lane_sidecar_path)
    if (
        lane_sidecar.get("candidate_manifest_ref") != expected_manifest_ref
        or terminal.get("candidate_manifest_ref") != expected_manifest_ref
    ):
        raise ValueError(
            "candidate_manifest_ref does not match the actual manifest path"
        )
    if terminal.get("lane_sidecar_ref") != expected_sidecar_ref:
        raise ValueError("lane_sidecar_ref does not match the actual sidecar path")


def write_lane_sidecar_then_terminal_v2(
    *,
    candidate_manifest_path: str | Path,
    lane_sidecar_path: str | Path,
    lane_sidecar: Mapping[str, Any],
    terminal_path: str | Path,
    terminal: Mapping[str, Any],
) -> None:
    manifest = read_candidate_manifest_v2(candidate_manifest_path)
    sidecar = validate_training_lane_sidecar_v2(lane_sidecar)
    commit = validate_training_terminal_commit_v2(terminal)
    _validate_bundle_path_refs_v2(
        candidate_manifest_path=candidate_manifest_path,
        lane_sidecar_path=lane_sidecar_path,
        lane_sidecar=sidecar,
        terminal=commit,
    )
    restore_freeze_inputs_v2(manifest, sidecar, commit)
    _atomic_create_json(lane_sidecar_path, sidecar)
    _atomic_create_json(terminal_path, commit)


def read_training_lane_sidecar_v2(path: str | Path) -> JSONMap:
    return validate_training_lane_sidecar_v2(
        _read_json(path, "training lane sidecar")
    )


def read_training_terminal_commit_v2(path: str | Path) -> JSONMap:
    return validate_training_terminal_commit_v2(
        _read_json(path, "training terminal commit")
    )


def read_training_bundle_v2(
    *,
    candidate_manifest_path: str | Path,
    lane_sidecar_path: str | Path,
    terminal_path: str | Path,
) -> CompactTrainingArtifactsV2:
    """Read one published bundle and bind every reference to its actual path."""

    manifest = read_candidate_manifest_v2(candidate_manifest_path)
    sidecar = read_training_lane_sidecar_v2(lane_sidecar_path)
    terminal = read_training_terminal_commit_v2(terminal_path)
    _validate_bundle_path_refs_v2(
        candidate_manifest_path=candidate_manifest_path,
        lane_sidecar_path=lane_sidecar_path,
        lane_sidecar=sidecar,
        terminal=terminal,
    )
    restore_freeze_inputs_v2(manifest, sidecar, terminal)
    return CompactTrainingArtifactsV2(manifest, sidecar, terminal)


__all__ = (
    "CANDIDATE_MANIFEST_SCHEMA_V2",
    "CompactTrainingArtifactsV2",
    "LEGACY_TRAIN_TERMINAL_SCHEMA_V1",
    "TRAIN_LANE_SIDECAR_SCHEMA_V2",
    "TRAIN_TERMINAL_COMMIT_SCHEMA_V2",
    "artifact_path_ref_v2",
    "build_candidate_manifest_v2",
    "build_training_lane_sidecar_v2",
    "build_training_terminal_commit_v2",
    "compact_training_artifacts_from_v1_v2",
    "read_candidate_manifest_v2",
    "read_training_bundle_v2",
    "read_training_lane_sidecar_v2",
    "read_training_terminal_commit_v2",
    "restore_freeze_inputs_v2",
    "validate_candidate_manifest_v2",
    "validate_training_lane_sidecar_v2",
    "validate_training_terminal_commit_v2",
    "write_candidate_manifest_v2",
    "write_lane_sidecar_then_terminal_v2",
)
