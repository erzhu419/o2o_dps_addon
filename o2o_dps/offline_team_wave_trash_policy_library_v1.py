"""Compile exact Stage-5 trash player-wave demonstrations into a library.

Unlike the compact historical-episode library, this builder consumes the
``chronicle_external_team_wave_model/v2`` partitions directly.  One selected
Warrior and one exact wave always produce one mode.  Encounter metadata must
explicitly identify the encounter as non-boss, and every executable START in
the compiled mode must fall inside the same runtime-executable exact build
segment.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .offline_team_wave_policy_v1 import (
    KNOWN_TEAM_WAVE_SCHEMA_V2,
    compile_offline_team_wave_feedback_policy_v1,
)
from .offline_wave_policy_v1 import OfflineWavePolicyV1Error


JSONMap = dict[str, Any]
SCHEMA = "offline_team_wave_trash_policy_library/v1"
TEAM_MANIFEST_SCHEMA = "chronicle_external_team_wave_model/v2"
RAW_API_MANIFEST_SCHEMA = "chronicle_external_api_ingest/v1"
BUILD_JOIN_MANIFEST_SCHEMA = "historical_fury_decision_build_join/v1"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_JOIN_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_decision_build_join"
    / "v1"
    / "manifest.json"
)

SELECTED_EXACT_TRASH_MODE = "EXACT_STAGE5_TRASH_PLAYER_WAVE_MODE_PRESERVED"
REJECTED_SPEC = "NO_EXACT_CONFLICT_FREE_FURY_OR_ARMS_SPEC"
REJECTED_METADATA_MISSING = "ENCOUNTER_METADATA_MISSING"
REJECTED_BOSS = "ENCOUNTER_METADATA_BOSS_TRUE"
REJECTED_BOSS_UNRESOLVED = "ENCOUNTER_METADATA_BOSS_NOT_EXPLICIT_BOOLEAN"
REJECTED_NO_ACTION = "NO_EXECUTABLE_OBSERVED_START"
REJECTED_NO_GCD = "NO_EXECUTABLE_OBSERVED_GCD"
REJECTED_NO_TARGET = "NO_OBSERVED_HOSTILE_TARGET_FOR_EXECUTABLE_MODE"
REJECTED_PARTIAL_BOUNDARY = "PARTIAL_TEAM_WAVE_BOUNDARY"
REJECTED_NO_SEGMENT = "NO_EXACT_BUILD_SEGMENT_AT_EXECUTABLE_START"
REJECTED_MULTIPLE_SEGMENTS = "MULTIPLE_EXACT_BUILD_SEGMENTS_WITHIN_MODE"
REJECTED_NOT_RUNTIME = "EXACT_BUILD_SEGMENT_NOT_RUNTIME_EXECUTABLE"

_PROBE_SEGMENT_REF = "__STAGE5_TRASH_BUILD_PROBE__"


class OfflineTeamWaveTrashPolicyLibraryV1Error(RuntimeError):
    """A selected manifest, metadata join, or exact mode is invalid."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"cannot read {label} {path}: {error}"
        ) from error
    return _object(value, label)


def _relative_path(base: Path, value: object, label: str) -> Path:
    path = Path(_text(value, label))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _iter_jsonl(path: Path, label: str) -> Iterator[Mapping[str, Any]]:
    opener = gzip.open if path.suffix.casefold() == ".gz" else open
    try:
        handle = opener(path, "rt", encoding="utf-8")
    except OSError as error:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"cannot open {label} {path}: {error}"
        ) from error
    try:
        with handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                        f"invalid JSON in {label} {path}:{line_number}: {error}"
                    ) from error
                yield _object(value, f"{label} row {line_number}")
    except OSError as error:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"cannot stream {label} {path}: {error}"
        ) from error


def _data_root(path: Path) -> Path:
    for candidate in (path.parent, *path.parents):
        if candidate.name == "offline_data":
            return candidate
    raise OfflineTeamWaveTrashPolicyLibraryV1Error(
        f"{path} is not below an offline_data directory; pass raw_api_manifest_path"
    )


def _team_partitions(
    manifest_path: Path,
    *,
    selected_instance_ids: frozenset[str] | None,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    manifest = _read_json(manifest_path, "team-wave manifest")
    if manifest.get("schema") != TEAM_MANIFEST_SCHEMA:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"unsupported team-wave manifest schema {manifest.get('schema')!r}"
        )
    by_instance: dict[str, Mapping[str, Any]] = {}
    for raw in _array(manifest.get("instances"), "team-wave manifest.instances"):
        entry = _object(raw, "team-wave instance entry")
        instance_id = _text(entry.get("instance_id"), "instance entry.instance_id")
        if instance_id in by_instance:
            raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                f"duplicate team-wave instance {instance_id}"
            )
        partition = _object(entry.get("partition"), "instance entry.partition")
        if partition.get("record_schema") != KNOWN_TEAM_WAVE_SCHEMA_V2:
            raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                f"team-wave partition {instance_id} has unsupported record schema"
            )
        by_instance[instance_id] = entry
    if selected_instance_ids is not None:
        missing = sorted(selected_instance_ids.difference(by_instance))
        if missing:
            raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                f"requested instance_id not present in team-wave manifest: {missing}"
            )
    selected = [
        entry
        for instance_id, entry in by_instance.items()
        if selected_instance_ids is None or instance_id in selected_instance_ids
    ]
    return manifest, selected


def _derive_raw_api_manifest_path(
    team_manifest_path: Path,
    team_manifest: Mapping[str, Any],
) -> Path:
    root = _data_root(team_manifest_path)
    closure = _object(team_manifest.get("input_closure"), "team input_closure")
    timeline_binding = _object(
        closure.get("timeline_manifest"), "team input_closure.timeline_manifest"
    )
    timeline_path = _relative_path(
        root,
        timeline_binding.get("stable_path"),
        "timeline_manifest.stable_path",
    )
    timeline = _read_json(timeline_path, "bound timeline manifest")
    timeline_closure = _object(
        timeline.get("input_closure"), "timeline input_closure"
    )
    raw_binding = _object(
        timeline_closure.get("raw_api_manifest"),
        "timeline input_closure.raw_api_manifest",
    )
    return _relative_path(root, raw_binding.get("path"), "raw_api_manifest.path")


def _encounter_metadata_index(
    raw_manifest_path: Path,
    *,
    selected_instance_ids: frozenset[str],
) -> tuple[Mapping[str, Any], dict[tuple[str, str], JSONMap], set[str]]:
    manifest = _read_json(raw_manifest_path, "raw API ingest manifest")
    if manifest.get("schema") != RAW_API_MANIFEST_SCHEMA:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"unsupported raw API manifest schema {manifest.get('schema')!r}"
        )
    raw_base = raw_manifest_path.parent.parent
    entries: dict[str, Mapping[str, Any]] = {}
    for raw in _array(manifest.get("instances"), "raw API manifest.instances"):
        entry = _object(raw, "raw API instance entry")
        instance_id = _text(entry.get("instance_id"), "raw instance.instance_id")
        if instance_id in entries:
            raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                f"duplicate raw API instance {instance_id}"
            )
        entries[instance_id] = entry

    index: dict[tuple[str, str], JSONMap] = {}
    instances_with_metadata: set[str] = set()
    for instance_id in sorted(selected_instance_ids):
        entry = entries.get(instance_id)
        if entry is None:
            continue
        metadata_binding = _object(entry.get("metadata"), "raw instance.metadata")
        object_binding = _object(
            metadata_binding.get("object"), "raw instance.metadata.object"
        )
        metadata_path = _relative_path(
            raw_base,
            object_binding.get("relative_path"),
            "raw instance.metadata.object.relative_path",
        )
        metadata = _read_json(metadata_path, f"instance metadata {instance_id}")
        if _text(metadata.get("id"), "instance metadata.id") != instance_id:
            raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                f"instance metadata id differs from raw manifest instance {instance_id}"
            )
        instances_with_metadata.add(instance_id)
        for raw_encounter in _array(
            metadata.get("encounters"), "instance metadata.encounters"
        ):
            encounter = _object(raw_encounter, "instance metadata encounter")
            encounter_id = _text(encounter.get("id"), "metadata encounter.id")
            key = (instance_id, encounter_id)
            if key in index:
                raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                    f"duplicate encounter metadata {key!r}"
                )
            name = encounter.get("name")
            index[key] = {
                "boss": encounter.get("boss"),
                "name": name.strip() if isinstance(name, str) and name.strip() else None,
                "metadata_path": str(metadata_path),
            }
    return manifest, index, instances_with_metadata


def _segment_timelines(
    build_join_manifest_path: Path,
) -> tuple[
    Mapping[str, Any],
    frozenset[str],
    dict[tuple[str, str], tuple[tuple[tuple[int, int, int], str], ...]],
]:
    manifest = _read_json(build_join_manifest_path, "build-join manifest")
    if manifest.get("schema") != BUILD_JOIN_MANIFEST_SCHEMA:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            f"unsupported build-join manifest schema {manifest.get('schema')!r}"
        )
    dictionary = _object(
        manifest.get("segment_dictionary"), "build-join segment_dictionary"
    )
    descriptor = _object(
        dictionary.get("partition"), "build-join segment dictionary partition"
    )
    path = _relative_path(
        build_join_manifest_path.parent,
        descriptor.get("path"),
        "segment dictionary partition.path",
    )
    runtime_refs: set[str] = set()
    timelines: dict[tuple[str, str], list[tuple[tuple[int, int, int], str]]] = {}
    for row in _iter_jsonl(path, "build segment dictionary"):
        segment_ref = _text(row.get("segment_ref"), "segment dictionary.segment_ref")
        identity = _object(row.get("identity"), "segment dictionary.identity")
        valid_from = _object(row.get("valid_from"), "segment dictionary.valid_from")
        instance_id = _text(identity.get("instance_id"), "segment identity.instance_id")
        player_guid = _text(identity.get("player_guid"), "segment identity.player_guid")
        anchor_values = (
            valid_from.get("timestamp_ms"),
            valid_from.get("event_index"),
            valid_from.get("message_ordinal"),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in anchor_values
        ):
            raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                "build segment valid_from requires nonnegative timestamp/event/message integers"
            )
        flags = _object(row.get("coverage_flags"), "segment coverage_flags")
        if flags.get("runtime_executable") is True:
            runtime_refs.add(segment_ref)
        timelines.setdefault((instance_id, player_guid.casefold()), []).append(
            ((anchor_values[0], anchor_values[1], anchor_values[2]), segment_ref)
        )

    frozen: dict[
        tuple[str, str], tuple[tuple[tuple[int, int, int], str], ...]
    ] = {}
    for identity, entries in timelines.items():
        entries.sort(key=lambda value: (value[0], value[1]))
        for previous, current in zip(entries, entries[1:]):
            if previous[0] == current[0] and previous[1] != current[1]:
                raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                    f"conflicting build segments at {identity!r} {current[0]!r}"
                )
        frozen[identity] = tuple(entries)
    if not runtime_refs:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            "build segment dictionary has no runtime-executable segment"
        )
    return manifest, frozenset(runtime_refs), frozen


def _segment_at(
    timeline: Sequence[tuple[tuple[int, int, int], str]],
    anchor: tuple[int, int, int],
) -> str | None:
    position = bisect_right([row[0] for row in timeline], anchor) - 1
    return None if position < 0 else timeline[position][1]


def _stage5_action_anchor(order_key: tuple[int, ...]) -> tuple[int, int, int]:
    if len(order_key) != 5:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            "compiled Stage-5 START order_key must contain exactly five integers"
        )
    return order_key[1], order_key[2], order_key[4]


def _player_lane(
    player_row: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str | None]:
    identity = _object(player_row.get("player"), "team-wave player.player")
    if identity.get("class") != "WARRIOR":
        return identity, None
    raw_lane = player_row.get("warrior_spec_lane")
    if not isinstance(raw_lane, Mapping):
        return identity, ""
    if (
        raw_lane.get("evidence_status") != "OBSERVED"
        or raw_lane.get("exact_guid_match") is not True
        or raw_lane.get("fury_or_arms_conflict_free_observation") is not True
        or raw_lane.get("observed_spec") not in {"Fury", "Arms"}
    ):
        return identity, ""
    return identity, str(raw_lane["observed_spec"])


def _compile_rejection(error: BaseException) -> str | None:
    if str(error) == "wave has no executable observed START actions":
        return REJECTED_NO_ACTION
    if str(error) == "wave has no source-observed executable GCD for an independent tail":
        return REJECTED_NO_GCD
    if str(error) == "observed_target_count must be positive":
        return REJECTED_NO_TARGET
    if str(error) in {
        "team wave anchors do not span the complete boundary",
        "team wave boundary duration must be positive",
    }:
        return REJECTED_PARTIAL_BOUNDARY
    return None


def build_offline_team_wave_trash_policy_library_v1(
    team_wave_manifest_path: str | Path,
    *,
    build_join_manifest_path: str | Path = DEFAULT_BUILD_JOIN_MANIFEST,
    raw_api_manifest_path: str | Path | None = None,
    instance_ids: Iterable[str] | None = None,
    specs: Iterable[str] = ("Fury", "Arms"),
    max_modes: int | None = None,
) -> JSONMap:
    """Stream exact Stage-5 partitions and retain executable trash modes."""

    if max_modes is not None and (
        isinstance(max_modes, bool) or not isinstance(max_modes, int) or max_modes <= 0
    ):
        raise ValueError("max_modes must be a positive integer or None")
    selected_instances = None
    if instance_ids is not None:
        selected_instances = frozenset(_text(value, "instance_id") for value in instance_ids)
        if not selected_instances:
            selected_instances = None
    selected_specs = frozenset(_text(value, "spec") for value in specs)
    if not selected_specs or not selected_specs.issubset({"Fury", "Arms"}):
        raise ValueError("specs must be a nonempty subset of Fury and Arms")

    team_path = Path(team_wave_manifest_path).expanduser().resolve()
    team_manifest, partitions = _team_partitions(
        team_path, selected_instance_ids=selected_instances
    )
    selected_instance_set = frozenset(
        _text(row.get("instance_id"), "instance entry.instance_id")
        for row in partitions
    )
    derived_raw_path = _derive_raw_api_manifest_path(team_path, team_manifest)
    raw_derived = raw_api_manifest_path is None
    raw_path = (
        derived_raw_path
        if raw_api_manifest_path is None
        else Path(raw_api_manifest_path).expanduser().resolve()
    )
    if raw_path != derived_raw_path:
        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
            "raw_api_manifest_path differs from the team-wave input closure"
        )
    raw_manifest, encounter_metadata, instances_with_metadata = (
        _encounter_metadata_index(
            raw_path, selected_instance_ids=selected_instance_set
        )
    )
    build_path = Path(build_join_manifest_path).expanduser().resolve()
    build_manifest, runtime_refs, timelines = _segment_timelines(build_path)

    modes: list[JSONMap] = []
    selected: list[JSONMap] = []
    rejected: list[JSONMap] = []
    rejection_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    selected_spec_counts: Counter[str] = Counter()
    wave_rows_seen = 0
    player_rows_seen = 0
    non_warrior_rows_skipped = 0
    warrior_rows_seen = 0
    spec_filtered_rows = 0
    eligible_rows_seen = 0
    explicit_trash_wave_rows_seen = 0
    scan_truncated = False
    seen_mode_ids: set[str] = set()

    def reject(identity: Mapping[str, Any], reason: str) -> None:
        rejected.append({**identity, "reason": reason})
        rejection_counts[reason] += 1

    for entry in partitions:
        partition_instance = _text(entry.get("instance_id"), "instance entry.instance_id")
        descriptor = _object(entry.get("partition"), "instance entry.partition")
        partition_path = _relative_path(
            team_path.parent, descriptor.get("path"), "team-wave partition.path"
        )
        for team_wave in _iter_jsonl(partition_path, "team-wave partition"):
            wave_rows_seen += 1
            if team_wave.get("schema") != KNOWN_TEAM_WAVE_SCHEMA_V2:
                raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                    f"team-wave row in {partition_instance} has unsupported schema"
                )
            wave = _object(team_wave.get("wave"), "team_wave.wave")
            instance_id = _text(wave.get("instance_id"), "wave.instance_id")
            if instance_id != partition_instance:
                raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                    f"wave instance {instance_id} differs from partition {partition_instance}"
                )
            encounter_id = _text(wave.get("encounter_id"), "wave.encounter_id")
            wave_id = _text(wave.get("wave_id"), "wave.wave_id")
            metadata = encounter_metadata.get((instance_id, encounter_id))
            if metadata is not None and metadata.get("boss") is False:
                explicit_trash_wave_rows_seen += 1

            for raw_player in _array(team_wave.get("players"), "team_wave.players"):
                player_rows_seen += 1
                player_row = _object(raw_player, "team-wave player")
                player, observed_spec = _player_lane(player_row)
                if player.get("class") != "WARRIOR":
                    non_warrior_rows_skipped += 1
                    continue
                warrior_rows_seen += 1
                player_guid = _text(player.get("guid"), "player.guid")
                identity = {
                    "instance_id": instance_id,
                    "encounter_id": encounter_id,
                    "wave_id": wave_id,
                    "player_guid": player_guid,
                }
                if observed_spec == "":
                    reject(identity, REJECTED_SPEC)
                    continue
                if observed_spec not in selected_specs:
                    spec_filtered_rows += 1
                    continue
                if observed_spec is None:
                    raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                        "Warrior lane classification unexpectedly returned no spec state"
                    )
                eligible_rows_seen += 1
                identity["warrior_spec"] = observed_spec

                if metadata is None:
                    reject(identity, REJECTED_METADATA_MISSING)
                    continue
                if metadata.get("boss") is True:
                    reject(identity, REJECTED_BOSS)
                    continue
                if metadata.get("boss") is not False:
                    reject(identity, REJECTED_BOSS_UNRESOLVED)
                    continue

                try:
                    probe = compile_offline_team_wave_feedback_policy_v1(
                        team_wave,
                        player_guid=player_guid,
                        build_segment_ref=_PROBE_SEGMENT_REF,
                        encounter_name=metadata.get("name"),
                    )
                except (OfflineWavePolicyV1Error, ValueError) as error:
                    reason = _compile_rejection(error)
                    if reason is None:
                        raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                            f"cannot compile selected Stage-5 player-wave: {error}"
                        ) from error
                    reject(identity, reason)
                    continue

                timeline = timelines.get((instance_id, player_guid.casefold()), ())
                action_segments = [
                    _segment_at(timeline, _stage5_action_anchor(action.source_order_key))
                    for action in probe.actions
                ]
                if any(segment is None for segment in action_segments):
                    reject(identity, REJECTED_NO_SEGMENT)
                    continue
                unique_segments = set(action_segments)
                if len(unique_segments) != 1:
                    reject(identity, REJECTED_MULTIPLE_SEGMENTS)
                    continue
                segment_ref = next(iter(unique_segments))
                if segment_ref not in runtime_refs:
                    reject(identity, REJECTED_NOT_RUNTIME)
                    continue

                policy = compile_offline_team_wave_feedback_policy_v1(
                    team_wave,
                    player_guid=player_guid,
                    build_segment_ref=segment_ref,
                    encounter_name=metadata.get("name"),
                )
                if [row.source_order_key for row in policy.actions] != [
                    row.source_order_key for row in probe.actions
                ]:
                    raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                        "exact-build recompile changed the executable START sequence"
                    )
                if policy.mode_id in seen_mode_ids:
                    raise OfflineTeamWaveTrashPolicyLibraryV1Error(
                        f"duplicate compiled mode_id {policy.mode_id}"
                    )
                seen_mode_ids.add(policy.mode_id)
                mode = policy.to_dict()
                mode["trash_scope"] = {
                    "encounter_id": encounter_id,
                    "encounter_name": metadata.get("name"),
                    "metadata_boss": False,
                    "warrior_spec": observed_spec,
                    "selection_status": "EXPLICIT_METADATA_NON_BOSS",
                }
                modes.append(mode)
                selected.append(
                    {
                        **identity,
                        "mode_id": policy.mode_id,
                        "build_segment_ref": segment_ref,
                        "reason": SELECTED_EXACT_TRASH_MODE,
                    }
                )
                selected_spec_counts[observed_spec] += 1
                action_counts.update(row.action_key for row in policy.actions)
                evidence_counts.update(row.evidence_status for row in policy.actions)
                if max_modes is not None and len(modes) >= max_modes:
                    scan_truncated = True
                    break
            if scan_truncated:
                break
        if scan_truncated:
            break

    return {
        "schema": SCHEMA,
        "status": "DEVELOPMENT_EXACT_TRASH_MODES_NOT_YET_COMPARISON",
        "input_scope": {
            "team_wave_manifest_schema": team_manifest.get("schema"),
            "build_join_manifest_schema": build_manifest.get("schema"),
            "raw_api_manifest_schema": raw_manifest.get("schema"),
            "raw_api_manifest_derived_from_team_closure": raw_derived,
            "selected_instance_ids": sorted(selected_instance_set),
            "selected_specs": sorted(selected_specs),
            "compact_stage5_partitions_only": True,
            "chronicle_api_called": False,
        },
        "selection": {
            "required_metadata_boss_value": False,
            "encounter_boss_evidence_source": "RAW_API_INSTANCE_METADATA",
            "exact_conflict_free_spec_required": True,
            "single_runtime_executable_build_segment_required": True,
            "max_modes": max_modes,
            "scan_truncated_by_max_modes": scan_truncated,
            "selected": selected,
            "rejected": rejected,
        },
        "counts": {
            "team_wave_partition_count_selected": len(partitions),
            "metadata_instance_count_loaded": len(instances_with_metadata),
            "runtime_executable_segment_count": len(runtime_refs),
            "wave_rows_seen": wave_rows_seen,
            "explicit_trash_wave_rows_seen": explicit_trash_wave_rows_seen,
            "player_wave_rows_seen": player_rows_seen,
            "non_warrior_player_wave_rows_skipped": non_warrior_rows_skipped,
            "warrior_player_wave_rows_seen": warrior_rows_seen,
            "spec_filtered_player_wave_rows": spec_filtered_rows,
            "eligible_conflict_free_player_wave_rows_seen": eligible_rows_seen,
            "mode_count": len(modes),
            "rejected_count": len(rejected),
            "rejection_reason_counts": dict(sorted(rejection_counts.items())),
            "selected_spec_counts": dict(sorted(selected_spec_counts.items())),
            "selected_action_counts": dict(sorted(action_counts.items())),
            "selected_evidence_status_counts": dict(sorted(evidence_counts.items())),
        },
        "modes": modes,
        "contract": {
            "unit": "ONE_EXACT_STAGE5_PLAYER_WAVE_DEMONSTRATION_PER_MODE",
            "boss_modes_included": False,
            "different_players_or_waves_merged": False,
            "all_executable_starts_in_one_exact_build_segment": True,
            "global_action_priority_averaging": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


def write_offline_team_wave_trash_policy_library_v1(
    output_path: str | Path,
    **build_kwargs: Any,
) -> JSONMap:
    result = build_offline_team_wave_trash_policy_library_v1(**build_kwargs)
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile exact non-boss Stage-5 Warrior player-wave demonstrations "
            "with exact runtime build bindings."
        )
    )
    parser.add_argument("--team-wave-manifest", type=Path, required=True)
    parser.add_argument(
        "--build-join-manifest", type=Path, default=DEFAULT_BUILD_JOIN_MANIFEST
    )
    parser.add_argument("--raw-api-manifest", type=Path)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--spec", action="append", choices=("Fury", "Arms"), default=[])
    parser.add_argument("--max-modes", type=int)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = write_offline_team_wave_trash_policy_library_v1(
        args.output,
        team_wave_manifest_path=args.team_wave_manifest,
        build_join_manifest_path=args.build_join_manifest,
        raw_api_manifest_path=args.raw_api_manifest,
        instance_ids=args.instance_id or None,
        specs=args.spec or ("Fury", "Arms"),
        max_modes=args.max_modes,
    )
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "output": str(args.output.resolve()),
                "counts": result["counts"],
                "comparison_authorized": False,
                "deployment_authorized": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BUILD_JOIN_MANIFEST_SCHEMA",
    "DEFAULT_BUILD_JOIN_MANIFEST",
    "OfflineTeamWaveTrashPolicyLibraryV1Error",
    "RAW_API_MANIFEST_SCHEMA",
    "SCHEMA",
    "TEAM_MANIFEST_SCHEMA",
    "build_offline_team_wave_trash_policy_library_v1",
    "main",
    "write_offline_team_wave_trash_policy_library_v1",
]
