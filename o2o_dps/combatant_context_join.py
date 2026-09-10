"""Causally join Chronicle CombatantInfo to compact Fury decisions in memory.

Decision partitions are streamed one row at a time.  The only observation
fields this layer can promote are ``gear_item_ids`` and
``exact_talent_ranks``.  No enriched row dataset is written.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import tempfile
from typing import Any

from .chronicle_combatant_sidecar import (
    DEFAULT_DATASET_MANIFEST,
    DEFAULT_OUTPUT_DIR as DEFAULT_SIDECAR_DIR,
    MANIFEST_SCHEMA as SIDECAR_MANIFEST_SCHEMA,
    RECORD_SCHEMA as SIDECAR_RECORD_SCHEMA,
)
from .o2o_observation import project_decision_record, validate_observation


REPORT_SCHEMA = "fury_combatant_context_coverage/v1"
JOIN_OUTCOMES = (
    "instance_unavailable",
    "encounter_missing",
    "guid_mismatch",
    "late_info_only",
    "unanchored_info_only",
    "matched",
)
DEFAULT_SIDECAR_MANIFEST = DEFAULT_SIDECAR_DIR / "manifest.json"
DEFAULT_REPORT = (
    Path(__file__).resolve().parents[1]
    / "offline_data"
    / "reports"
    / "fury_combatant_context_coverage_v1.json"
)


class CombatantContextJoinError(RuntimeError):
    """A decision, sidecar, or manifest cannot be joined causally."""


@dataclass(frozen=True)
class CombatantContext:
    event_index: int
    message_ordinal: int
    offset_ms: int | None
    timestamp_ms: int | None
    gear_item_ids: tuple[int, ...] | None
    exact_talent_ranks: tuple[str, ...] | None
    source_artifact: str


@dataclass(frozen=True)
class CombatantJoinResult:
    observation: dict[str, Any]
    instance_ref: str
    join_outcome: str
    causal_info_matched: bool
    gear_item_ids_available: bool
    exact_talent_ranks_available: bool
    gear_item_ids_promoted: bool
    exact_talent_ranks_promoted: bool
    late_info_rejected: bool


class CombatantContextIndex:
    """Compact per-instance lookup keyed only by encounter and player GUID."""

    def __init__(
        self,
        contexts: Mapping[tuple[str, str], Iterable[CombatantContext]],
        *,
        instance_available: bool = True,
        encounter_ids: Iterable[str] = (),
        identity_keys: Iterable[tuple[str, str]] = (),
    ) -> None:
        self._instance_available = instance_available
        self._encounter_ids = set(encounter_ids)
        self._identity_keys = set(identity_keys)
        self._contexts: dict[
            tuple[str, str], tuple[tuple[tuple[int, int], ...], tuple[CombatantContext, ...]]
        ] = {}
        for key, raw_contexts in contexts.items():
            ordered = tuple(
                sorted(
                    raw_contexts,
                    key=lambda item: (item.event_index, item.message_ordinal),
                )
            )
            anchors = tuple(
                (item.event_index, item.message_ordinal) for item in ordered
            )
            self._contexts[key] = anchors, ordered
            self._encounter_ids.add(key[0])
            self._identity_keys.add(key)

    @classmethod
    def empty(cls, *, instance_available: bool) -> "CombatantContextIndex":
        return cls({}, instance_available=instance_available)

    def select(
        self,
        *,
        encounter_id: str,
        player_guid: str,
        start_event_index: int,
    ) -> tuple[CombatantContext | None, str]:
        """Select latest INFO <= START and return one exclusive join outcome."""

        key = (encounter_id, player_guid.strip().lower())
        if not self._instance_available:
            return None, "instance_unavailable"
        if encounter_id not in self._encounter_ids:
            return None, "encounter_missing"
        if key not in self._identity_keys:
            return None, "guid_mismatch"
        found = self._contexts.get(key)
        if found is None:
            return None, "unanchored_info_only"
        anchors, contexts = found
        position = bisect_right(anchors, (start_event_index, 2**63 - 1))
        if position == 0:
            return None, "late_info_only"
        return contexts[position - 1], "matched"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CombatantContextJoinError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise CombatantContextJoinError(f"{label} is not a JSON object: {path}")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CombatantContextJoinError(f"{label} must be an integer")
    return value


def _resolve_artifact(path_value: Any, *, manifest_path: Path, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise CombatantContextJoinError(f"{label} is not a non-empty path")
    declared = Path(path_value).expanduser()
    candidates = [declared]
    if not declared.is_absolute():
        candidates.append(manifest_path.parent / declared)
    candidates.append(manifest_path.parent / declared.name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CombatantContextJoinError(f"{label} does not exist: {declared}")


def _iter_gzip_jsonl(path: Path, *, label: str) -> Iterator[dict[str, Any]]:
    try:
        with gzip.open(path, mode="rt", encoding="utf-8", newline="") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise CombatantContextJoinError(
                        f"invalid JSON in {label} {path}:{line_number}: {error}"
                    ) from error
                if not isinstance(value, dict):
                    raise CombatantContextJoinError(
                        f"{label} row is not an object: {path}:{line_number}"
                    )
                yield value
    except (OSError, EOFError, UnicodeError) as error:
        raise CombatantContextJoinError(f"cannot read {label} {path}: {error}") from error


def _gear_item_ids(record: Mapping[str, Any]) -> tuple[int, ...] | None:
    gear = record.get("gear")
    if not isinstance(gear, list) or not gear:
        return None
    slots: list[tuple[int, int]] = []
    for slot in gear:
        if not isinstance(slot, Mapping):
            raise CombatantContextJoinError("sidecar gear slot is not an object")
        slot_index = _integer(
            slot.get("slot_index"), label="sidecar gear slot_index"
        )
        item_id = _integer(slot.get("item_id"), label="sidecar gear item_id")
        slots.append((slot_index, item_id))
    slots.sort()
    if len({slot_index for slot_index, _ in slots}) != len(slots):
        raise CombatantContextJoinError("sidecar contains duplicate gear slot_index")
    return tuple(item_id for _, item_id in slots)


def _exact_talent_ranks(record: Mapping[str, Any]) -> tuple[str, ...] | None:
    talents = record.get("talents")
    if not isinstance(talents, Mapping):
        return None
    trees = talents.get("trees")
    if not isinstance(trees, list) or len(trees) != 3:
        return None
    if not all(isinstance(tree, str) for tree in trees):
        raise CombatantContextJoinError("sidecar talent trees must be strings")
    return tuple(trees)


def build_context_index(
    records: Iterable[Mapping[str, Any]],
    *,
    instance_ref: str,
    source_artifact: str,
) -> CombatantContextIndex:
    """Build one compact instance index from streamed sidecar rows."""

    contexts: dict[tuple[str, str], list[CombatantContext]] = {}
    encounter_ids: set[str] = set()
    identity_keys: set[tuple[str, str]] = set()
    for record in records:
        if record.get("schema") != SIDECAR_RECORD_SCHEMA:
            raise CombatantContextJoinError(
                f"unsupported sidecar record schema: {record.get('schema')!r}"
            )
        if record.get("instance_ref") != instance_ref:
            raise CombatantContextJoinError(
                "sidecar record instance_ref disagrees with its manifest entry"
            )
        encounter_id = record.get("encounter_id")
        player = record.get("player")
        if not isinstance(encounter_id, str) or not encounter_id:
            raise CombatantContextJoinError("sidecar encounter_id is missing")
        if not isinstance(player, Mapping):
            raise CombatantContextJoinError("sidecar player is not an object")
        player_guid = player.get("guid")
        if not isinstance(player_guid, str) or not player_guid.strip():
            raise CombatantContextJoinError("sidecar player GUID is missing")
        key = (encounter_id, player_guid.strip().lower())
        encounter_ids.add(encounter_id)
        identity_keys.add(key)
        anchor = record.get("anchor")
        if anchor is None:
            continue
        if not isinstance(anchor, Mapping):
            raise CombatantContextJoinError("sidecar anchor is not an object")
        event_index = _integer(
            anchor.get("event_index"), label="sidecar anchor event_index"
        )
        message_ordinal = _integer(
            record.get("message_ordinal"), label="sidecar message_ordinal"
        )
        offset_value = anchor.get("offset_ms")
        timestamp_value = anchor.get("timestamp_ms")
        offset_ms = (
            None
            if offset_value is None
            else _integer(offset_value, label="sidecar anchor offset_ms")
        )
        timestamp_ms = (
            None
            if timestamp_value is None
            else _integer(timestamp_value, label="sidecar anchor timestamp_ms")
        )
        contexts.setdefault(key, []).append(
            CombatantContext(
                event_index=event_index,
                message_ordinal=message_ordinal,
                offset_ms=offset_ms,
                timestamp_ms=timestamp_ms,
                gear_item_ids=_gear_item_ids(record),
                exact_talent_ranks=_exact_talent_ranks(record),
                source_artifact=source_artifact,
            )
        )
    return CombatantContextIndex(
        contexts,
        encounter_ids=encounter_ids,
        identity_keys=identity_keys,
    )


def load_context_index(
    sidecar_path: str | Path,
    *,
    instance_ref: str,
) -> CombatantContextIndex:
    """Stream one ``jsonl.gz`` sidecar into a compact per-instance index."""

    path = Path(sidecar_path).expanduser().resolve()
    if path.suffixes[-2:] != [".jsonl", ".gz"]:
        raise CombatantContextJoinError(
            f"combatant sidecar must be jsonl.gz, got {path}"
        )
    return build_context_index(
        _iter_gzip_jsonl(path, label="combatant sidecar"),
        instance_ref=instance_ref,
        source_artifact=str(path),
    )


def _observed_provenance(context: CombatantContext) -> dict[str, Any]:
    return {
        "kind": "OBSERVED",
        "event_index": context.event_index,
        "csv_line": None,
        "offset_ms": context.offset_ms,
        "timestamp_ms": context.timestamp_ms,
        "message_ordinal": context.message_ordinal,
        "source_artifact": context.source_artifact,
        "note": "Chronicle combatant_info at or before decision START",
    }


def join_decision_record(
    record: Mapping[str, Any],
    context_index: CombatantContextIndex,
) -> CombatantJoinResult:
    """Project and enrich one decision without mutating the source record."""

    identity = record.get("identity")
    source = record.get("source")
    if not isinstance(identity, Mapping):
        raise CombatantContextJoinError("decision identity is not an object")
    if not isinstance(source, Mapping):
        raise CombatantContextJoinError("decision source is not an object")
    start_anchor = source.get("start_anchor")
    if not isinstance(start_anchor, Mapping):
        raise CombatantContextJoinError("decision START anchor is not an object")

    instance_ref = identity.get("source_instance_ref")
    encounter_id = identity.get("encounter_id")
    player_guid = identity.get("player_guid")
    if not isinstance(instance_ref, str) or not instance_ref:
        raise CombatantContextJoinError("decision source_instance_ref is missing")
    if not isinstance(encounter_id, str) or not encounter_id:
        raise CombatantContextJoinError("decision encounter_id is missing")
    if not isinstance(player_guid, str) or not player_guid.strip():
        raise CombatantContextJoinError("decision player_guid is missing")
    start_event_index = _integer(
        start_anchor.get("event_index"), label="decision START event_index"
    )

    observation = project_decision_record(record)
    context, join_outcome = context_index.select(
        encounter_id=encounter_id,
        player_guid=player_guid,
        start_event_index=start_event_index,
    )
    if context is None:
        return CombatantJoinResult(
            observation=observation,
            instance_ref=instance_ref,
            join_outcome=join_outcome,
            causal_info_matched=False,
            gear_item_ids_available=False,
            exact_talent_ranks_available=False,
            gear_item_ids_promoted=False,
            exact_talent_ranks_promoted=False,
            late_info_rejected=join_outcome == "late_info_only",
        )

    provenance = _observed_provenance(context)
    gear_promoted = False
    talents_promoted = False
    if (
        context.gear_item_ids is not None
        and observation["fields"]["gear_item_ids"]["status"] == "MISSING"
    ):
        observation["fields"]["gear_item_ids"] = {
            "value": list(context.gear_item_ids),
            "status": "OBSERVED",
            "provenance": deepcopy(provenance),
        }
        gear_promoted = True
    if (
        context.exact_talent_ranks is not None
        and observation["fields"]["exact_talent_ranks"]["status"] == "MISSING"
    ):
        observation["fields"]["exact_talent_ranks"] = {
            "value": list(context.exact_talent_ranks),
            "status": "OBSERVED",
            "provenance": deepcopy(provenance),
        }
        talents_promoted = True
    validate_observation(observation)
    return CombatantJoinResult(
        observation=observation,
        instance_ref=instance_ref,
        join_outcome="matched",
        causal_info_matched=True,
        gear_item_ids_available=context.gear_item_ids is not None,
        exact_talent_ranks_available=context.exact_talent_ranks is not None,
        gear_item_ids_promoted=gear_promoted,
        exact_talent_ranks_promoted=talents_promoted,
        late_info_rejected=False,
    )


def _sidecar_entries(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if manifest.get("schema") != SIDECAR_MANIFEST_SCHEMA:
        raise CombatantContextJoinError(
            f"unsupported sidecar manifest schema: {manifest.get('schema')!r}"
        )
    entries = manifest.get("instances")
    if not isinstance(entries, list):
        raise CombatantContextJoinError("sidecar manifest has no instances list")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CombatantContextJoinError("sidecar manifest instance is not an object")
        instance_ref = entry.get("instance_ref")
        if not isinstance(instance_ref, str) or not instance_ref:
            raise CombatantContextJoinError("sidecar manifest instance_ref is missing")
        if instance_ref in result:
            raise CombatantContextJoinError(
                f"duplicate sidecar manifest instance_ref: {instance_ref}"
            )
        result[instance_ref] = entry
    return result


def _dataset_partitions(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("schema") != "chronicle_fury_decision_dataset/v1":
        raise CombatantContextJoinError(
            f"unsupported decision manifest schema: {manifest.get('schema')!r}"
        )
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list):
        raise CombatantContextJoinError("decision manifest has no partitions list")
    if not all(isinstance(partition, dict) for partition in partitions):
        raise CombatantContextJoinError("decision manifest partition is not an object")
    return partitions


def iter_joined_observations(
    *,
    dataset_manifest: str | Path = DEFAULT_DATASET_MANIFEST,
    sidecar_manifest: str | Path = DEFAULT_SIDECAR_MANIFEST,
) -> Iterator[CombatantJoinResult]:
    """Stream compact decisions and yield only in-memory joined observations."""

    dataset_manifest_path = Path(dataset_manifest).expanduser().resolve()
    sidecar_manifest_path = Path(sidecar_manifest).expanduser().resolve()
    dataset = _load_json_object(dataset_manifest_path, label="decision manifest")
    sidecars = _load_json_object(sidecar_manifest_path, label="sidecar manifest")
    entries = _sidecar_entries(sidecars)

    for partition in _dataset_partitions(dataset):
        source_refs = partition.get("source_instance_refs")
        if not isinstance(source_refs, list) or not source_refs:
            raise CombatantContextJoinError(
                "decision partition has no source_instance_refs"
            )
        indices: dict[str, CombatantContextIndex] = {}
        for instance_ref_value in source_refs:
            if not isinstance(instance_ref_value, str) or not instance_ref_value:
                raise CombatantContextJoinError(
                    "decision source_instance_ref is not a non-empty string"
                )
            entry = entries.get(instance_ref_value)
            if entry is None:
                indices[instance_ref_value] = CombatantContextIndex.empty(
                    instance_available=False
                )
                continue
            if entry.get("availability") != "available":
                indices[instance_ref_value] = CombatantContextIndex.empty(
                    instance_available=False
                )
                continue
            sidecar_path = _resolve_artifact(
                entry.get("sidecar_path"),
                manifest_path=sidecar_manifest_path,
                label=f"sidecar for {instance_ref_value}",
            )
            indices[instance_ref_value] = load_context_index(
                sidecar_path, instance_ref=instance_ref_value
            )

        partition_path = _resolve_artifact(
            partition.get("partition"),
            manifest_path=dataset_manifest_path,
            label="decision partition",
        )
        for record in _iter_gzip_jsonl(partition_path, label="decision partition"):
            identity = record.get("identity")
            if not isinstance(identity, Mapping):
                raise CombatantContextJoinError("decision identity is not an object")
            instance_ref = identity.get("source_instance_ref")
            if not isinstance(instance_ref, str) or instance_ref not in indices:
                raise CombatantContextJoinError(
                    "decision instance_ref is outside its partition source refs"
                )
            yield join_decision_record(record, indices[instance_ref])


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def audit_combatant_context_coverage(
    *,
    dataset_manifest: str | Path = DEFAULT_DATASET_MANIFEST,
    sidecar_manifest: str | Path = DEFAULT_SIDECAR_MANIFEST,
    output: str | Path = DEFAULT_REPORT,
) -> dict[str, Any]:
    """Run the streaming join and write only an aggregate coverage report."""

    dataset_manifest_path = Path(dataset_manifest).expanduser().resolve()
    sidecar_manifest_path = Path(sidecar_manifest).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    dataset = _load_json_object(dataset_manifest_path, label="decision manifest")
    sidecars = _load_json_object(sidecar_manifest_path, label="sidecar manifest")
    partitions = _dataset_partitions(dataset)
    entries = _sidecar_entries(sidecars)

    referenced: list[str] = []
    seen_refs: set[str] = set()
    for partition in partitions:
        source_refs = partition.get("source_instance_refs")
        if not isinstance(source_refs, list):
            raise CombatantContextJoinError(
                "decision partition has no source_instance_refs list"
            )
        for instance_ref in source_refs:
            if not isinstance(instance_ref, str) or not instance_ref:
                raise CombatantContextJoinError(
                    "decision source_instance_ref is not a non-empty string"
                )
            if instance_ref not in seen_refs:
                referenced.append(instance_ref)
                seen_refs.add(instance_ref)

    per_instance: dict[str, dict[str, Any]] = {}
    for instance_ref in referenced:
        entry = entries.get(instance_ref)
        availability = (
            entry.get("availability") if entry is not None else "missing_sidecar_entry"
        )
        http_status = entry.get("http_status") if entry is not None else None
        per_instance[instance_ref] = {
            "instance_ref": instance_ref,
            "availability": availability,
            "http_status": http_status,
            "decision_rows": 0,
            "causal_info_matched": 0,
            "gear_item_ids_available": 0,
            "exact_talent_ranks_available": 0,
            "late_info_rejected": 0,
            "join_outcomes": {outcome: 0 for outcome in JOIN_OUTCOMES},
        }

    totals = {
        "decision_rows": 0,
        "causal_info_matched": 0,
        "gear_item_ids_available": 0,
        "exact_talent_ranks_available": 0,
        "gear_item_ids_promoted": 0,
        "exact_talent_ranks_promoted": 0,
        "late_info_rejected": 0,
        "join_outcomes": {outcome: 0 for outcome in JOIN_OUTCOMES},
    }
    for joined in iter_joined_observations(
        dataset_manifest=dataset_manifest_path,
        sidecar_manifest=sidecar_manifest_path,
    ):
        totals["decision_rows"] += 1
        totals["causal_info_matched"] += int(joined.causal_info_matched)
        totals["gear_item_ids_available"] += int(joined.gear_item_ids_available)
        totals["exact_talent_ranks_available"] += int(
            joined.exact_talent_ranks_available
        )
        totals["gear_item_ids_promoted"] += int(joined.gear_item_ids_promoted)
        totals["exact_talent_ranks_promoted"] += int(
            joined.exact_talent_ranks_promoted
        )
        totals["late_info_rejected"] += int(joined.late_info_rejected)
        totals["join_outcomes"][joined.join_outcome] += 1
        instance = per_instance[joined.instance_ref]
        instance["decision_rows"] += 1
        instance["causal_info_matched"] += int(joined.causal_info_matched)
        instance["gear_item_ids_available"] += int(
            joined.gear_item_ids_available
        )
        instance["exact_talent_ranks_available"] += int(
            joined.exact_talent_ranks_available
        )
        instance["late_info_rejected"] += int(joined.late_info_rejected)
        instance["join_outcomes"][joined.join_outcome] += 1

    output_block = dataset.get("output")
    declared_count = (
        output_block.get("decision_count") if isinstance(output_block, Mapping) else None
    )
    if declared_count is None:
        raise CombatantContextJoinError(
            "decision manifest has no output.decision_count"
        )
    declared_count = _integer(
        declared_count, label="decision manifest output.decision_count"
    )
    if totals["decision_rows"] != declared_count:
        raise CombatantContextJoinError(
            f"streamed {totals['decision_rows']} decisions, expected {declared_count}"
        )
    join_outcome_rows = sum(totals["join_outcomes"].values())
    if join_outcome_rows != totals["decision_rows"]:
        raise CombatantContextJoinError(
            f"join outcomes total {join_outcome_rows}, expected {totals['decision_rows']}"
        )

    available_instances = sum(
        per_instance[ref]["availability"] == "available" for ref in referenced
    )
    unavailable_404_instances = sum(
        per_instance[ref]["availability"] == "unavailable"
        and per_instance[ref]["http_status"] == 404
        for ref in referenced
    )
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": _utc_now(),
        "inputs": {
            "decision_manifest": str(dataset_manifest_path),
            "combatant_sidecar_manifest": str(sidecar_manifest_path),
            "decision_partitions_opened_read_only": True,
            "live_profile_used": False,
        },
        "join_contract": {
            "key": ["instance_ref", "encounter_id", "player_guid"],
            "causal_cutoff": "CombatantInfo.event_index <= decision START.event_index",
            "promoted_fields": ["gear_item_ids", "exact_talent_ranks"],
            "status": "OBSERVED",
            "materialization": "in_memory_projection_only",
            "late_info_rejected_definition": (
                "decision has matching instance/encounter/GUID INFO, but all INFO "
                "anchors are after START"
            ),
        },
        "instances": {
            "referenced": len(referenced),
            "available": available_instances,
            "unavailable_404": unavailable_404_instances,
            "other_missing_or_unavailable": (
                len(referenced) - available_instances - unavailable_404_instances
            ),
        },
        "coverage": totals,
        "quality": {
            "declared_decision_rows": declared_count,
            "streamed_decision_rows": totals["decision_rows"],
            "row_count_matches_manifest": True,
            "join_outcome_rows": join_outcome_rows,
            "join_outcome_sum_matches_decision_rows": True,
            "future_cutoff_failures": 0,
            "line_level_output_materialized": False,
        },
        "sidecar_compressed_bytes_total": sidecars.get(
            "sidecar_compressed_bytes_total"
        ),
        "per_instance": [per_instance[ref] for ref in referenced],
    }
    _atomic_write_json(output_path, report)
    return {**report, "report_path": str(output_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit causal CombatantInfo coverage over compact Fury decisions"
    )
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_DATASET_MANIFEST)
    parser.add_argument(
        "--sidecar-manifest", type=Path, default=DEFAULT_SIDECAR_MANIFEST
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = audit_combatant_context_coverage(
            dataset_manifest=args.dataset_manifest,
            sidecar_manifest=args.sidecar_manifest,
            output=args.output,
        )
    except CombatantContextJoinError as error:
        print(f"error: {error}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CombatantContextIndex",
    "CombatantContextJoinError",
    "CombatantJoinResult",
    "DEFAULT_REPORT",
    "REPORT_SCHEMA",
    "audit_combatant_context_coverage",
    "build_context_index",
    "iter_joined_observations",
    "join_decision_record",
    "load_context_index",
]
