"""Build a compact Upper Kara route/pull/phase registry from API metadata.

The registry records what the metadata observed.  It deliberately leaves
target permissions unresolved: damage or activity in a log is not evidence
that a raid leader allowed the player to select that target.  Permissions are
added only through an explicit authoritative overlay.

This builder reads the small metadata objects referenced by the external API
admission manifest.  It does not read raw combat rows, normalized partitions,
or the much larger exact team timelines.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_route_wave_registry/v1"
STATUS = "ROUTE_SKELETON_TARGET_RULES_UNRESOLVED"
TARGET_CONTRACT_UNRESOLVED = "UNRESOLVED_REQUIRES_AUTHORITATIVE_OVERLAY"
TARGET_CONTRACT_AUTHORITATIVE = "AUTHORITATIVE"
OVERLAY_SOURCE_KINDS = frozenset({"USER_AUTHORITATIVE", "MECHANIC_AUTHORITATIVE"})
SUPPORTED_OUTCOMES = frozenset({"KILL", "WIPE", "RESET", "PARTIAL"})
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


class UpperKaraRouteRegistryError(ValueError):
    """The route registry cannot be built without changing its meaning."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraRouteRegistryError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise UpperKaraRouteRegistryError(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraRouteRegistryError(f"{label} must be nonempty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpperKaraRouteRegistryError(f"{label} must be a nonnegative integer")
    return value


def _safe_component(value: str) -> str:
    result = _SAFE_COMPONENT.sub("-", value.strip()).strip("-")
    return result or "unnamed"


def _instant(value: object, label: str) -> datetime:
    text = _text(value, label)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    # Python 3.10 accepts only three- or six-digit fractional seconds here,
    # while Chronicle emits valid values such as ``.57Z``.  Right-padding the
    # fraction preserves the instant and also covers explicit UTC offsets.
    normalized = re.sub(
        r"\.(\d{1,5})(?=[+-]\d{2}:\d{2}$)",
        lambda match: "." + match.group(1).ljust(6, "0"),
        normalized,
    )
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as error:
        raise UpperKaraRouteRegistryError(f"{label} is not an ISO timestamp") from error


def _offset_ms(value: object, origin: datetime, label: str) -> int:
    result = round((_instant(value, label) - origin).total_seconds() * 1000.0)
    if result < 0:
        raise UpperKaraRouteRegistryError(f"{label} precedes encounter start")
    return result


def _outcome(value: object) -> str:
    source = _text(value, "encounter.kill_type").lower()
    mapping = {
        "clean": "KILL",
        "wipe": "WIPE",
        "reset": "RESET",
        "partial": "PARTIAL",
    }
    try:
        return mapping[source]
    except KeyError as error:
        raise UpperKaraRouteRegistryError(
            f"unsupported encounter.kill_type {source!r}"
        ) from error


def _target_contract_unresolved() -> JSONMap:
    return {
        "status": TARGET_CONTRACT_UNRESOLVED,
        "observed_activity_grants_target_permission": False,
        "allowed_primary_occurrence_ids": [],
        "forbidden_primary_occurrence_ids": [],
        "priority_partial_order": [],
        "collateral_occurrence_ids": [],
        "source": None,
    }


def _entry_multiset(targets: Sequence[Mapping[str, Any]]) -> JSONMap:
    counts = Counter(int(row["creature_entry_id"]) for row in targets)
    return {str(entry): counts[entry] for entry in sorted(counts)}


def _signature(multiset: Mapping[str, Any], *, boss: bool) -> str:
    body = "_".join(
        f"{_nonnegative_int(int(entry), 'entry')}x{_nonnegative_int(count, 'count')}"
        for entry, count in sorted(multiset.items(), key=lambda row: int(row[0]))
    )
    return f"{'boss' if boss else 'trash'}-{body or 'empty'}"


def _boss_anchor(encounter: Mapping[str, Any]) -> str | None:
    if encounter.get("metadata_boss_flag") is not True:
        return None
    entries = sorted(
        {
            int(row["creature_entry_id"])
            for row in encounter["targets"]
            if row["metadata_boss_flag"] is True
        }
    )
    if entries:
        return "entry-" + "-".join(str(entry) for entry in entries)
    return "name-" + _safe_component(str(encounter["encounter_name"]).lower())


def _target_periods(
    hostile: Mapping[str, Any],
    *,
    encounter_start: datetime,
    encounter_end_ms: int,
) -> tuple[list[JSONMap], int, int, int | None, bool]:
    raw_periods = _array(hostile.get("periods"), "hostile.periods")
    if not raw_periods:
        raise UpperKaraRouteRegistryError("hostile.periods must not be empty")
    periods: list[JSONMap] = []
    for period_ordinal, raw in enumerate(raw_periods):
        row = _mapping(raw, f"hostile.periods[{period_ordinal}]")
        start_ms = _offset_ms(
            row.get("start"), encounter_start, "hostile.period.start"
        )
        end_ms = _offset_ms(row.get("end"), encounter_start, "hostile.period.end")
        last_active_ms = _offset_ms(
            row.get("last_active"), encounter_start, "hostile.period.last_active"
        )
        if not (start_ms <= last_active_ms <= end_ms <= encounter_end_ms):
            raise UpperKaraRouteRegistryError("hostile period offsets are not ordered")
        end_state = _text(row.get("end_state"), "hostile.period.end_state")
        periods.append(
            {
                "period_ordinal": period_ordinal,
                "start_offset_ms": start_ms,
                "end_offset_ms": end_ms,
                "last_active_offset_ms": last_active_ms,
                "end_state": end_state,
            }
        )
    first_ms = min(row["start_offset_ms"] for row in periods)
    last_ms = max(row["last_active_offset_ms"] for row in periods)
    slain = [row["end_offset_ms"] for row in periods if row["end_state"] == "slain"]
    death_ms = min(slain) if slain else None
    return periods, first_ms, last_ms, death_ms, death_ms is None


def _spawn_groups(targets: list[JSONMap], *, tolerance_ms: int = 1000) -> None:
    ordered = sorted(
        targets,
        key=lambda row: (row["first_activity_offset_ms"], row["occurrence_id"]),
    )
    group = -1
    group_start: int | None = None
    for target in ordered:
        start = target["first_activity_offset_ms"]
        if group_start is None or start - group_start > tolerance_ms:
            group += 1
            group_start = start
        target["spawn_group_id"] = f"spawn-{group:03d}"


def _phase_candidates(
    pull_ref: str,
    targets: Sequence[Mapping[str, Any]],
    duration_ms: int,
) -> list[JSONMap]:
    change_points = {0, duration_ms}
    starts: dict[int, list[str]] = defaultdict(list)
    deaths: dict[int, list[str]] = defaultdict(list)
    for target in targets:
        occurrence_id = str(target["occurrence_id"])
        start_ms = int(target["first_activity_offset_ms"])
        change_points.add(start_ms)
        starts[start_ms].append(occurrence_id)
        death_ms = target.get("death_offset_ms")
        if isinstance(death_ms, int):
            change_points.add(death_ms)
            deaths[death_ms].append(occurrence_id)
    points = sorted(change_points)
    phases: list[JSONMap] = []
    for phase_ordinal, (start_ms, end_ms) in enumerate(zip(points, points[1:])):
        if end_ms <= start_ms:
            continue
        active: list[str] = []
        for target in targets:
            occurrence_id = str(target["occurrence_id"])
            for period in target["periods"]:
                if (
                    period["start_offset_ms"] < end_ms
                    and period["end_offset_ms"] > start_ms
                ):
                    active.append(occurrence_id)
                    break
        start_events: list[JSONMap] = []
        if start_ms == 0:
            start_events.append({"kind": "PULL_START", "occurrence_ids": []})
        if starts.get(start_ms):
            start_events.append(
                {
                    "kind": "TARGET_ACTIVITY_BEGAN",
                    "occurrence_ids": sorted(starts[start_ms]),
                }
            )
        if deaths.get(start_ms):
            start_events.append(
                {
                    "kind": "TARGET_DEAD_OBSERVED",
                    "occurrence_ids": sorted(deaths[start_ms]),
                }
            )
        phases.append(
            {
                "phase_ref": f"{pull_ref}:phase-{phase_ordinal:03d}",
                "phase_ordinal": phase_ordinal,
                "start_offset_ms": start_ms,
                "end_offset_ms": end_ms,
                "start_observations": start_events,
                "active_occurrence_ids": sorted(set(active)),
                "semantic_role": "UNRESOLVED",
                "target_contract": _target_contract_unresolved(),
            }
        )
    return phases


def metadata_document_to_route_observation_v1(
    document: Mapping[str, Any],
) -> JSONMap:
    """Convert one Chronicle metadata document without reading event streams."""

    raw = _mapping(document, "metadata document")
    instance_id = _text(raw.get("id"), "metadata.id")
    instance_name = _text(raw.get("name"), "metadata.name")
    if instance_name != "Upper Tower of Karazhan":
        raise UpperKaraRouteRegistryError("metadata is not Upper Tower of Karazhan")
    units = _mapping(raw.get("units"), "metadata.units")
    raw_encounters = _array(raw.get("encounters"), "metadata.encounters")
    ordered_encounters = sorted(
        enumerate(raw_encounters),
        key=lambda item: (
            _instant(
                _mapping(item[1], f"encounters[{item[0]}]").get("start_time"),
                f"encounters[{item[0]}].start_time",
            ),
            _text(
                _mapping(item[1], f"encounters[{item[0]}]").get("id"),
                f"encounters[{item[0]}].id",
            ),
        ),
    )
    route_origin = (
        _instant(
            _mapping(ordered_encounters[0][1], "encounters[0]").get("start_time"),
            "encounters[0].start_time",
        )
        if ordered_encounters
        else None
    )
    if route_origin is None:
        raise UpperKaraRouteRegistryError("metadata.encounters must not be empty")
    encounters: list[JSONMap] = []
    boss_attempts: Counter[str] = Counter()
    distinct_boss_sequence: list[str] = []

    for encounter_ordinal, (metadata_array_ordinal, raw_encounter) in enumerate(
        ordered_encounters
    ):
        row = _mapping(raw_encounter, f"encounters[{metadata_array_ordinal}]")
        if _text(row.get("instance_id"), "encounter.instance_id") != instance_id:
            raise UpperKaraRouteRegistryError("encounter instance_id mismatch")
        encounter_id = _text(row.get("id"), "encounter.id")
        start = _instant(row.get("start_time"), "encounter.start_time")
        end = _instant(row.get("end_time"), "encounter.end_time")
        if end < start:
            raise UpperKaraRouteRegistryError("encounter end precedes start")
        duration_ms = round((end - start).total_seconds() * 1000.0)
        targets: list[JSONMap] = []
        for hostile_ordinal, raw_hostile in enumerate(
            _array(row.get("hostiles"), "encounter.hostiles")
        ):
            hostile = _mapping(raw_hostile, f"hostiles[{hostile_ordinal}]")
            guid = _text(hostile.get("id"), "hostile.id")
            unit = _mapping(units.get(guid), f"units[{guid}]")
            entry = unit.get("entry")
            if isinstance(entry, bool) or not isinstance(entry, int) or entry <= 0:
                raise UpperKaraRouteRegistryError(f"units[{guid}].entry is invalid")
            raw_name = unit.get("name")
            display_name = (
                raw_name.strip()
                if isinstance(raw_name, str) and raw_name.strip()
                else f"entry-{entry}"
            )
            display_name_status = (
                "OBSERVED_METADATA"
                if isinstance(raw_name, str) and raw_name.strip()
                else "MISSING_METADATA_ENTRY_FALLBACK"
            )
            periods, first_ms, last_ms, death_ms, censored = _target_periods(
                hostile,
                encounter_start=start,
                encounter_end_ms=duration_ms,
            )
            targets.append(
                {
                    "occurrence_id": f"{instance_id}:{encounter_id}:{guid}",
                    "target_guid": guid,
                    "hostile_ordinal": hostile_ordinal,
                    "creature_entry_id": entry,
                    "display_name": display_name,
                    "display_name_status": display_name_status,
                    "metadata_boss_flag": hostile.get("boss") is True,
                    "target_role": (
                        "BOSS" if hostile.get("boss") is True else "UNRESOLVED"
                    ),
                    "first_activity_offset_ms": first_ms,
                    "last_activity_offset_ms": last_ms,
                    "death_offset_ms": death_ms,
                    "death_right_censored": censored,
                    "periods": periods,
                    "first_direct_player_action_ms": None,
                    "first_player_damage_ms": None,
                    "focus_reduction_status": "NOT_REDUCED_FROM_EXACT_TIMELINE",
                }
            )
        if not targets:
            raise UpperKaraRouteRegistryError("encounter has no hostile targets")
        _spawn_groups(targets)
        target_multiset = _entry_multiset(targets)
        encounter: JSONMap = {
            "instance_id": instance_id,
            "encounter_id": encounter_id,
            "encounter_ordinal": encounter_ordinal,
            "metadata_encounter_array_ordinal": metadata_array_ordinal,
            "encounter_name": _text(row.get("name"), "encounter.name"),
            "metadata_boss_flag": row.get("boss") is True,
            "source_kill_type": _text(row.get("kill_type"), "encounter.kill_type"),
            "pull_outcome": _outcome(row.get("kill_type")),
            "start_time": _text(row.get("start_time"), "encounter.start_time"),
            "end_time": _text(row.get("end_time"), "encounter.end_time"),
            "route_start_offset_ms": round(
                (start - route_origin).total_seconds() * 1000.0
            ),
            "route_end_offset_ms": round(
                (end - route_origin).total_seconds() * 1000.0
            ),
            "duration_ms": duration_ms,
            "target_entry_multiset": target_multiset,
            "pull_signature": _signature(
                target_multiset, boss=row.get("boss") is True
            ),
            "targets": targets,
        }
        anchor = _boss_anchor(encounter)
        encounter["boss_anchor"] = anchor
        if anchor is not None:
            if not distinct_boss_sequence or distinct_boss_sequence[-1] != anchor:
                distinct_boss_sequence.append(anchor)
            boss_attempts[anchor] += 1
            encounter["attempt_ordinal"] = boss_attempts[anchor]
            encounter["attempt_identity_status"] = "BOSS_ANCHOR_EXACT"
        else:
            encounter["attempt_ordinal"] = 1
            encounter["attempt_identity_status"] = "NOT_IDENTIFIED_FOR_TRASH"
        pull_ref = f"{instance_id}:{encounter_id}"
        encounter["pull_ref"] = pull_ref
        encounter["pull_slot_id"] = None
        encounter["phase_candidates"] = _phase_candidates(
            pull_ref, targets, duration_ms
        )
        encounters.append(encounter)

    sequence = tuple(distinct_boss_sequence)
    variant_id = "route-" + (
        "__".join(_safe_component(value) for value in sequence) if sequence else "no-boss"
    )
    return {
        "instance_id": instance_id,
        "instance_name": instance_name,
        "route_variant_id": variant_id,
        "distinct_boss_sequence": list(sequence),
        "route_coverage": "OBSERVED_METADATA_SEQUENCE",
        "contains_partial_encounter": any(
            row["pull_outcome"] == "PARTIAL" for row in encounters
        ),
        "encounters": encounters,
    }


def _lcs_alignment(reference: Sequence[str], observed: Sequence[str]) -> dict[int, int]:
    """Return observed-index -> reference-index exact-signature LCS matches."""

    rows = len(reference) + 1
    cols = len(observed) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(len(reference) - 1, -1, -1):
        for j in range(len(observed) - 1, -1, -1):
            table[i][j] = (
                1 + table[i + 1][j + 1]
                if reference[i] == observed[j]
                else max(table[i + 1][j], table[i][j + 1])
            )
    result: dict[int, int] = {}
    i = j = 0
    while i < len(reference) and j < len(observed):
        if reference[i] == observed[j]:
            result[j] = i
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return result


def _segment_rows(instance: Mapping[str, Any]) -> dict[int, list[JSONMap]]:
    """Split trash by adjacent distinct-boss anchors; retries stay anchored."""

    segments: dict[int, list[JSONMap]] = defaultdict(list)
    boss_position = -1
    last_anchor: str | None = None
    for encounter in instance["encounters"]:
        anchor = encounter["boss_anchor"]
        if anchor is not None:
            if anchor != last_anchor:
                boss_position += 1
                last_anchor = anchor
            encounter["_boss_position"] = boss_position
        else:
            encounter["_trash_segment"] = boss_position + 1
            segments[boss_position + 1].append(encounter)
    return segments


def _assign_pull_slots(instances: Sequence[JSONMap]) -> list[JSONMap]:
    by_variant: dict[str, list[JSONMap]] = defaultdict(list)
    for instance in instances:
        by_variant[instance["route_variant_id"]].append(instance)

    variants: list[JSONMap] = []
    for variant_id, rows in sorted(by_variant.items()):
        rows.sort(key=lambda row: row["instance_id"])
        segmented = {row["instance_id"]: _segment_rows(row) for row in rows}
        all_segments = sorted(
            {segment for value in segmented.values() for segment in value}
        )
        reference_by_segment: dict[int, list[JSONMap]] = {}
        for segment in all_segments:
            candidates = [
                (segmented[row["instance_id"]].get(segment, []), row["instance_id"])
                for row in rows
            ]
            reference, _ = min(
                candidates,
                key=lambda item: (-len(item[0]), item[1]),
            )
            reference_by_segment[segment] = reference

        for row in rows:
            boss_sequence = row["distinct_boss_sequence"]
            for encounter in row["encounters"]:
                anchor = encounter["boss_anchor"]
                if anchor is not None:
                    position = encounter.pop("_boss_position")
                    encounter["pull_slot_id"] = (
                        f"{variant_id}:boss-{position:02d}:{_safe_component(anchor)}"
                    )
            for segment in all_segments:
                observed = segmented[row["instance_id"]].get(segment, [])
                reference = reference_by_segment[segment]
                matches = _lcs_alignment(
                    [value["pull_signature"] for value in reference],
                    [value["pull_signature"] for value in observed],
                )
                insertion_counts: Counter[tuple[int, int, str]] = Counter()
                for observed_index, encounter in enumerate(observed):
                    if observed_index in matches:
                        reference_index = matches[observed_index]
                        slot = f"pull-{reference_index:03d}"
                    else:
                        left = max(
                            (
                                reference_index
                                for index, reference_index in matches.items()
                                if index < observed_index
                            ),
                            default=-1,
                        )
                        right = min(
                            (
                                reference_index
                                for index, reference_index in matches.items()
                                if index > observed_index
                            ),
                            default=len(reference),
                        )
                        identity = (left, right, encounter["pull_signature"])
                        insertion_ordinal = insertion_counts[identity]
                        insertion_counts[identity] += 1
                        slot = (
                            f"insert-{left + 1:03d}-{right:03d}-"
                            f"{_safe_component(encounter['pull_signature'])}-"
                            f"{insertion_ordinal:02d}"
                        )
                    encounter["pull_slot_id"] = (
                        f"{variant_id}:segment-{segment:02d}:{slot}"
                    )
                    encounter.pop("_trash_segment", None)
            if any(encounter["pull_slot_id"] is None for encounter in row["encounters"]):
                raise UpperKaraRouteRegistryError("pull slot assignment is incomplete")

        slot_support = Counter(
            encounter["pull_slot_id"]
            for row in rows
            for encounter in row["encounters"]
        )
        variants.append(
            {
                "route_variant_id": variant_id,
                "distinct_boss_sequence": list(rows[0]["distinct_boss_sequence"]),
                "instance_ids": [row["instance_id"] for row in rows],
                "instance_count": len(rows),
                "pull_slot_support": dict(sorted(slot_support.items())),
                "trash_alignment": "BOSS_ANCHORED_SEGMENT_LOCAL_EXACT_SIGNATURE_LCS",
            }
        )
    return variants


def build_route_wave_registry_v1(
    metadata_documents: Iterable[Mapping[str, Any]],
    *,
    source: Mapping[str, Any] | None = None,
) -> JSONMap:
    instances = [
        metadata_document_to_route_observation_v1(document)
        for document in metadata_documents
    ]
    if not instances:
        raise UpperKaraRouteRegistryError("metadata_documents must not be empty")
    instances.sort(key=lambda row: row["instance_id"])
    if len({row["instance_id"] for row in instances}) != len(instances):
        raise UpperKaraRouteRegistryError("metadata instance IDs must be unique")
    variants = _assign_pull_slots(instances)
    encounters = [row for instance in instances for row in instance["encounters"]]
    targets = [row for encounter in encounters for row in encounter["targets"]]
    registry: JSONMap = {
        "schema": SCHEMA,
        "status": STATUS,
        "source": deepcopy(dict(source or {"kind": "IN_MEMORY_METADATA_DOCUMENTS"})),
        "permission_boundary": {
            "observed_activity_is_not_target_authorization": True,
            "authoritative_overlay_required": True,
        },
        "route_variants": variants,
        "instances": instances,
        "summary": {
            "instance_count": len(instances),
            "route_variant_count": len(variants),
            "encounter_count": len(encounters),
            "boss_attempt_count": sum(
                row["metadata_boss_flag"] is True for row in encounters
            ),
            "target_occurrence_count": len(targets),
            "death_right_censored_count": sum(
                row["death_right_censored"] is True for row in targets
            ),
            "authoritative_target_contract_count": 0,
            "exact_timeline_rows_read": 0,
            "raw_or_normalized_rows_read": 0,
        },
    }
    validate_route_wave_registry_v1(registry)
    return registry


def load_metadata_documents_from_admission_v1(
    admission_manifest_path: str | Path,
    *,
    offline_data_root: str | Path,
) -> tuple[list[JSONMap], JSONMap]:
    """Read only the metadata objects named by one admission manifest."""

    manifest_path = Path(admission_manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "chronicle_external_reconstruction_admission/v1":
        raise UpperKaraRouteRegistryError("unexpected admission manifest schema")
    raw_manifest = _mapping(
        _mapping(manifest.get("inputs"), "admission.inputs").get("raw_api_manifest"),
        "admission.inputs.raw_api_manifest",
    )
    raw_manifest_path = Path(offline_data_root).resolve() / _text(
        raw_manifest.get("path"), "raw_api_manifest.path"
    )
    object_root = raw_manifest_path.parent.parent
    documents: list[JSONMap] = []
    total_bytes = 0
    for index, raw_instance in enumerate(
        _array(manifest.get("instances"), "admission.instances")
    ):
        instance = _mapping(raw_instance, f"admission.instances[{index}]")
        evidence = _mapping(instance.get("source_evidence"), "source_evidence")
        metadata = _mapping(evidence.get("metadata_object"), "metadata_object")
        path = object_root / _text(metadata.get("relative_path"), "metadata path")
        declared_size = _nonnegative_int(metadata.get("size_bytes"), "metadata size")
        if not path.is_file() or path.stat().st_size != declared_size:
            raise UpperKaraRouteRegistryError(f"metadata object missing or wrong size: {path}")
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("id") != instance.get("instance_id"):
            raise UpperKaraRouteRegistryError("metadata/admission instance mismatch")
        documents.append(document)
        total_bytes += declared_size
    return documents, {
        "kind": "CHRONICLE_EXTERNAL_API_METADATA_ONLY",
        "admission_manifest": str(manifest_path),
        "metadata_object_count": len(documents),
        "metadata_bytes_read": total_bytes,
        "event_streams_read": 0,
    }


def build_route_wave_registry_from_admission_v1(
    admission_manifest_path: str | Path,
    *,
    offline_data_root: str | Path,
) -> JSONMap:
    documents, source = load_metadata_documents_from_admission_v1(
        admission_manifest_path,
        offline_data_root=offline_data_root,
    )
    return build_route_wave_registry_v1(documents, source=source)


def apply_authoritative_target_overlay_v1(
    registry: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> JSONMap:
    """Resolve one exact phase's target permissions from an explicit source."""

    result = deepcopy(dict(registry))
    validate_route_wave_registry_v1(result)
    raw = _mapping(overlay, "overlay")
    expected = {
        "pull_ref",
        "phase_ref",
        "source_kind",
        "source_ref",
        "allowed_primary_occurrence_ids",
        "forbidden_primary_occurrence_ids",
        "priority_partial_order",
        "collateral_occurrence_ids",
    }
    if set(raw) != expected:
        raise UpperKaraRouteRegistryError("overlay fields differ from v1")
    source_kind = _text(raw.get("source_kind"), "overlay.source_kind")
    if source_kind not in OVERLAY_SOURCE_KINDS:
        raise UpperKaraRouteRegistryError("overlay source is not authoritative")
    pull_ref = _text(raw.get("pull_ref"), "overlay.pull_ref")
    phase_ref = _text(raw.get("phase_ref"), "overlay.phase_ref")
    found: tuple[JSONMap, JSONMap] | None = None
    for instance in result["instances"]:
        for encounter in instance["encounters"]:
            if encounter["pull_ref"] != pull_ref:
                continue
            for phase in encounter["phase_candidates"]:
                if phase["phase_ref"] == phase_ref:
                    found = encounter, phase
                    break
    if found is None:
        raise UpperKaraRouteRegistryError("overlay pull/phase was not found")
    encounter, phase = found
    occurrence_ids = {row["occurrence_id"] for row in encounter["targets"]}

    def ids(field: str) -> list[str]:
        values = [_text(value, f"overlay.{field}") for value in _array(raw[field], field)]
        if len(values) != len(set(values)) or not set(values) <= occurrence_ids:
            raise UpperKaraRouteRegistryError(f"overlay.{field} is invalid")
        return values

    allowed = ids("allowed_primary_occurrence_ids")
    forbidden = ids("forbidden_primary_occurrence_ids")
    collateral = ids("collateral_occurrence_ids")
    if not allowed or set(allowed) & set(forbidden):
        raise UpperKaraRouteRegistryError("overlay primary target sets are invalid")
    edges: list[list[str]] = []
    for value in _array(raw["priority_partial_order"], "priority_partial_order"):
        edge = _array(value, "priority edge")
        if len(edge) != 2:
            raise UpperKaraRouteRegistryError("priority edge must contain two IDs")
        before, after = (_text(edge[0], "priority before"), _text(edge[1], "priority after"))
        if before == after or {before, after} - set(allowed):
            raise UpperKaraRouteRegistryError("priority edge must order allowed targets")
        edges.append([before, after])
    phase["target_contract"] = {
        "status": TARGET_CONTRACT_AUTHORITATIVE,
        "observed_activity_grants_target_permission": False,
        "allowed_primary_occurrence_ids": allowed,
        "forbidden_primary_occurrence_ids": forbidden,
        "priority_partial_order": edges,
        "collateral_occurrence_ids": collateral,
        "source": {
            "kind": source_kind,
            "ref": _text(raw.get("source_ref"), "overlay.source_ref"),
        },
    }
    result["summary"]["authoritative_target_contract_count"] += 1
    result["status"] = "ROUTE_SKELETON_WITH_PARTIAL_AUTHORITATIVE_TARGET_RULES"
    validate_route_wave_registry_v1(result)
    return result


def validate_route_wave_registry_v1(registry: Mapping[str, Any]) -> None:
    raw = _mapping(registry, "registry")
    expected = {
        "schema",
        "status",
        "source",
        "permission_boundary",
        "route_variants",
        "instances",
        "summary",
    }
    if set(raw) != expected or raw.get("schema") != SCHEMA:
        raise UpperKaraRouteRegistryError("registry fields or schema differ from v1")
    boundary = _mapping(raw.get("permission_boundary"), "permission_boundary")
    if boundary != {
        "observed_activity_is_not_target_authorization": True,
        "authoritative_overlay_required": True,
    }:
        raise UpperKaraRouteRegistryError("permission boundary differs from v1")
    instances = _array(raw.get("instances"), "instances")
    variants = _array(raw.get("route_variants"), "route_variants")
    instance_ids: set[str] = set()
    authoritative = 0
    total_encounters = 0
    total_targets = 0
    censored = 0
    for instance in instances:
        row = _mapping(instance, "instance")
        instance_id = _text(row.get("instance_id"), "instance_id")
        if instance_id in instance_ids:
            raise UpperKaraRouteRegistryError("duplicate instance_id")
        instance_ids.add(instance_id)
        encounters = _array(row.get("encounters"), "encounters")
        if [entry.get("encounter_ordinal") for entry in encounters] != list(
            range(len(encounters))
        ):
            raise UpperKaraRouteRegistryError("encounter ordinals are not contiguous")
        for encounter in encounters:
            total_encounters += 1
            _text(encounter.get("pull_slot_id"), "pull_slot_id")
            if encounter.get("pull_outcome") not in SUPPORTED_OUTCOMES:
                raise UpperKaraRouteRegistryError("unsupported pull outcome")
            targets = _array(encounter.get("targets"), "targets")
            target_ids = {_text(target.get("occurrence_id"), "occurrence_id") for target in targets}
            if len(target_ids) != len(targets):
                raise UpperKaraRouteRegistryError("duplicate target occurrence")
            total_targets += len(targets)
            censored += sum(target.get("death_right_censored") is True for target in targets)
            for phase in _array(encounter.get("phase_candidates"), "phase_candidates"):
                active = set(_array(phase.get("active_occurrence_ids"), "active targets"))
                if not active <= target_ids:
                    raise UpperKaraRouteRegistryError("phase has unknown active target")
                contract = _mapping(phase.get("target_contract"), "target_contract")
                if contract.get("observed_activity_grants_target_permission") is not False:
                    raise UpperKaraRouteRegistryError("observed activity granted permission")
                permission_fields = (
                    "allowed_primary_occurrence_ids",
                    "forbidden_primary_occurrence_ids",
                    "collateral_occurrence_ids",
                )
                for field in permission_fields:
                    if not set(_array(contract.get(field), field)) <= target_ids:
                        raise UpperKaraRouteRegistryError("contract references unknown target")
                status = contract.get("status")
                if status == TARGET_CONTRACT_UNRESOLVED:
                    if contract.get("source") is not None or any(contract.get(field) for field in permission_fields):
                        raise UpperKaraRouteRegistryError("unresolved target contract has permissions")
                elif status == TARGET_CONTRACT_AUTHORITATIVE:
                    source = _mapping(contract.get("source"), "contract.source")
                    if source.get("kind") not in OVERLAY_SOURCE_KINDS:
                        raise UpperKaraRouteRegistryError("target contract source is not authoritative")
                    authoritative += 1
                else:
                    raise UpperKaraRouteRegistryError("unknown target contract status")
    variant_instance_ids: set[str] = set()
    for variant in variants:
        row = _mapping(variant, "route variant")
        ids = set(_array(row.get("instance_ids"), "route variant instance_ids"))
        if variant_instance_ids & ids:
            raise UpperKaraRouteRegistryError("instance appears in multiple route variants")
        variant_instance_ids.update(ids)
    if variant_instance_ids != instance_ids:
        raise UpperKaraRouteRegistryError("route variant membership is incomplete")
    summary = _mapping(raw.get("summary"), "summary")
    expected_counts = {
        "instance_count": len(instances),
        "route_variant_count": len(variants),
        "encounter_count": total_encounters,
        "target_occurrence_count": total_targets,
        "death_right_censored_count": censored,
        "authoritative_target_contract_count": authoritative,
    }
    for field, value in expected_counts.items():
        if summary.get(field) != value:
            raise UpperKaraRouteRegistryError(f"summary.{field} mismatch")
    if summary.get("raw_or_normalized_rows_read") != 0 or summary.get("exact_timeline_rows_read") != 0:
        raise UpperKaraRouteRegistryError("metadata-only registry reports event rows")


def route_wave_registry_from_dict_v1(value: Mapping[str, Any]) -> JSONMap:
    result = deepcopy(dict(value))
    validate_route_wave_registry_v1(result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-manifest", type=Path, required=True)
    parser.add_argument("--offline-data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = build_route_wave_registry_from_admission_v1(
        args.admission_manifest,
        offline_data_root=args.offline_data_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "SCHEMA",
    "STATUS",
    "TARGET_CONTRACT_AUTHORITATIVE",
    "TARGET_CONTRACT_UNRESOLVED",
    "UpperKaraRouteRegistryError",
    "apply_authoritative_target_overlay_v1",
    "build_route_wave_registry_from_admission_v1",
    "build_route_wave_registry_v1",
    "load_metadata_documents_from_admission_v1",
    "metadata_document_to_route_observation_v1",
    "route_wave_registry_from_dict_v1",
    "validate_route_wave_registry_v1",
)
