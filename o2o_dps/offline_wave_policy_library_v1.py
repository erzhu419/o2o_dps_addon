"""Build a compact library of exact Chronicle player-wave policy modes.

The input is deliberately limited to the manifest-selected compact episode
partitions and, optionally, their compact decision/build mappings.  Every
selected player-wave remains its own mode; this module never averages action
orders, timings, targets, or item/cooldown use across players or waves.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .offline_wave_policy_v1 import (
    KNOWN_BUILD_MAPPING_SCHEMA,
    KNOWN_EPISODE_SCHEMA,
    LIBRARY_SCHEMA,
    OfflineWavePolicyV1Error,
    compile_offline_wave_feedback_policy_v1,
)


JSONMap = dict[str, Any]
EPISODE_MANIFEST_SCHEMA = "historical_fury_expert_episode_manifest/v1"
BUILD_JOIN_MANIFEST_SCHEMA = "historical_fury_decision_build_join/v1"

DEFAULT_EPISODE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "offline_data/derived/historical_fury_expert_episodes/v1/manifest.json"
)
DEFAULT_BUILD_JOIN_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "offline_data/derived/historical_fury_decision_build_join/v1/manifest.json"
)

SELECTED_EXACT_MODE = "EXACT_PLAYER_WAVE_MODE_PRESERVED"
REJECTED_PARTIAL = "PARTIAL_WAVE_COVERAGE"
REJECTED_NO_ACTION = "NO_EXECUTABLE_OBSERVED_START"
REJECTED_NO_GCD = "NO_EXECUTABLE_OBSERVED_GCD"
REJECTED_NO_RUNTIME_BUILD = "NO_EXACT_SINGLE_RUNTIME_EXECUTABLE_BUILD"


class OfflineWavePolicyLibraryV1Error(RuntimeError):
    """The compact manifest set or one of its selected records is invalid."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfflineWavePolicyLibraryV1Error(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OfflineWavePolicyLibraryV1Error(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineWavePolicyLibraryV1Error(f"{label} must be nonempty text")
    return value.strip()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OfflineWavePolicyLibraryV1Error(
            f"cannot read {label} {path}: {error}"
        ) from error
    return _object(value, label)


def _partition_path(
    manifest_path: Path, descriptor: Mapping[str, Any], label: str
) -> Path:
    raw = _text(descriptor.get("path"), f"{label}.path")
    path = Path(raw)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _iter_jsonl_gzip(path: Path, label: str) -> Iterator[Mapping[str, Any]]:
    try:
        handle = gzip.open(path, "rt", encoding="utf-8")
    except OSError as error:
        raise OfflineWavePolicyLibraryV1Error(
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
                    raise OfflineWavePolicyLibraryV1Error(
                        f"invalid JSON in {label} {path}:{line_number}: {error}"
                    ) from error
                yield _object(value, f"{label} row {line_number}")
    except OSError as error:
        raise OfflineWavePolicyLibraryV1Error(
            f"cannot stream {label} {path}: {error}"
        ) from error


def _episode_descriptors(
    manifest_path: Path,
    *,
    instance_ids: frozenset[str] | None,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    manifest = _read_json(manifest_path, "episode manifest")
    if manifest.get("schema") != EPISODE_MANIFEST_SCHEMA:
        raise OfflineWavePolicyLibraryV1Error(
            f"unsupported episode manifest schema {manifest.get('schema')!r}"
        )
    descriptors = [
        _object(row, "episode partition descriptor")
        for row in _array(manifest.get("partitions"), "episode manifest.partitions")
    ]
    by_instance: dict[str, Mapping[str, Any]] = {}
    for descriptor in descriptors:
        instance_id = _text(descriptor.get("instance_id"), "partition instance_id")
        if instance_id in by_instance:
            raise OfflineWavePolicyLibraryV1Error(
                f"duplicate episode partition for instance {instance_id}"
            )
        by_instance[instance_id] = descriptor
    if instance_ids is not None:
        missing = sorted(instance_ids.difference(by_instance))
        if missing:
            raise OfflineWavePolicyLibraryV1Error(
                f"requested instance_id not present in episode manifest: {missing}"
            )
        descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor["instance_id"] in instance_ids
        ]
    return manifest, descriptors


def _mapping_descriptors(
    manifest_path: Path,
) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
    manifest = _read_json(manifest_path, "decision/build join manifest")
    if manifest.get("schema") != BUILD_JOIN_MANIFEST_SCHEMA:
        raise OfflineWavePolicyLibraryV1Error(
            f"unsupported build-join manifest schema {manifest.get('schema')!r}"
        )
    descriptors: dict[str, Mapping[str, Any]] = {}
    for raw in _array(
        manifest.get("mapping_partitions"),
        "decision/build join manifest.mapping_partitions",
    ):
        descriptor = _object(raw, "mapping partition descriptor")
        instance_id = _text(
            descriptor.get("instance_id"), "mapping partition instance_id"
        )
        if instance_id in descriptors:
            raise OfflineWavePolicyLibraryV1Error(
                f"duplicate mapping partition for instance {instance_id}"
            )
        descriptors[instance_id] = descriptor
    return manifest, descriptors


def _load_instance_build_mappings(
    manifest_path: Path,
    descriptor: Mapping[str, Any] | None,
    *,
    instance_id: str,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    if descriptor is None:
        return {}
    if descriptor.get("record_schema") != KNOWN_BUILD_MAPPING_SCHEMA:
        raise OfflineWavePolicyLibraryV1Error(
            f"mapping partition {instance_id} has unsupported record schema"
        )
    path = _partition_path(manifest_path, descriptor, "mapping partition")
    mappings: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in _iter_jsonl_gzip(path, "mapping partition"):
        if row.get("schema") != KNOWN_BUILD_MAPPING_SCHEMA:
            raise OfflineWavePolicyLibraryV1Error(
                f"mapping row in {instance_id} has unsupported schema"
            )
        source = _object(row.get("source"), "mapping source")
        row_instance = _text(source.get("instance_id"), "mapping source.instance_id")
        if row_instance != instance_id:
            raise OfflineWavePolicyLibraryV1Error(
                f"mapping row instance {row_instance} differs from partition {instance_id}"
            )
        key = (
            _text(source.get("episode_id"), "mapping source.episode_id"),
            _text(source.get("wave_id"), "mapping source.wave_id"),
        )
        if key in mappings:
            raise OfflineWavePolicyLibraryV1Error(
                f"duplicate mapping for episode/wave {key!r}"
            )
        mappings[key] = row
    return mappings


def _segment_dictionary(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> tuple[
    frozenset[str],
    dict[tuple[str, str], tuple[tuple[tuple[int, int, int], str], ...]],
]:
    """Load runtime flags and any causal segment timeline carried by the join.

    Older compact fixtures only carried ``segment_ref`` and coverage flags.  A
    row with neither identity nor ``valid_from`` therefore remains a valid
    runtime-only dictionary row; production rows carrying either field must
    carry the complete timeline identity.
    """

    dictionary = _object(
        manifest.get("segment_dictionary"), "build join segment_dictionary"
    )
    descriptor = _object(
        dictionary.get("partition"), "build join segment dictionary partition"
    )
    path = _partition_path(
        manifest_path, descriptor, "build segment dictionary partition"
    )
    runtime_refs: set[str] = set()
    timelines: dict[tuple[str, str], list[tuple[tuple[int, int, int], str]]] = {}
    for row in _iter_jsonl_gzip(path, "build segment dictionary partition"):
        segment_ref = _text(row.get("segment_ref"), "segment_ref")
        flags = _object(row.get("coverage_flags"), "segment coverage_flags")
        if flags.get("runtime_executable") is True:
            runtime_refs.add(segment_ref)

        raw_identity = row.get("identity")
        raw_valid_from = row.get("valid_from")
        if raw_identity is None and raw_valid_from is None:
            continue
        identity = _object(raw_identity, "segment identity")
        valid_from = _object(raw_valid_from, "segment valid_from")
        instance_id = _text(
            identity.get("instance_id"), "segment identity.instance_id"
        )
        player_guid = _text(
            identity.get("player_guid"), "segment identity.player_guid"
        )
        anchor_values = (
            valid_from.get("timestamp_ms"),
            valid_from.get("event_index"),
            valid_from.get("message_ordinal"),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in anchor_values
        ):
            raise OfflineWavePolicyLibraryV1Error(
                "segment valid_from timeline anchor must contain nonnegative integers"
            )
        timelines.setdefault((instance_id, player_guid.casefold()), []).append(
            ((anchor_values[0], anchor_values[1], anchor_values[2]), segment_ref)
        )

    frozen_timelines: dict[
        tuple[str, str], tuple[tuple[tuple[int, int, int], str], ...]
    ] = {}
    for identity, entries in timelines.items():
        entries.sort(key=lambda row: (row[0], row[1]))
        previous_anchor: tuple[int, int, int] | None = None
        previous_ref: str | None = None
        for anchor, segment_ref in entries:
            if previous_anchor == anchor and previous_ref != segment_ref:
                raise OfflineWavePolicyLibraryV1Error(
                    f"segment timeline has conflicting refs at {identity!r} {anchor!r}"
                )
            previous_anchor = anchor
            previous_ref = segment_ref
        frozen_timelines[identity] = tuple(entries)
    return frozenset(runtime_refs), frozen_timelines


def _timeline_segment_at_action(
    timeline: Sequence[tuple[tuple[int, int, int], str]],
    action_anchor: tuple[int, int, int],
) -> str | None:
    selected: str | None = None
    for valid_from, segment_ref in timeline:
        if valid_from > action_anchor:
            break
        selected = segment_ref
    return selected


def _complete_build_mapping_from_timeline(
    episode: Mapping[str, Any],
    *,
    wave_index: int,
    build_mapping: Mapping[str, Any] | None,
    segment_timelines: Mapping[
        tuple[str, str], Sequence[tuple[tuple[int, int, int], str]]
    ],
) -> Mapping[str, Any] | None:
    """Fill ontology-expanded START bindings from the exact causal build timeline."""

    instance_id = _text(episode.get("instance_id"), "episode.instance_id")
    player = _object(episode.get("player"), "episode.player")
    player_guid = _text(player.get("guid"), "episode.player.guid")
    timeline = segment_timelines.get((instance_id, player_guid.casefold()))
    if not timeline:
        return build_mapping

    waves = _array(episode.get("wave_observations"), "episode.wave_observations")
    if wave_index < 0 or wave_index >= len(waves):
        raise OfflineWavePolicyLibraryV1Error("wave_index is outside episode")
    wave = _object(waves[wave_index], "wave observation")

    if build_mapping is None:
        completed: JSONMap = {
            "schema": KNOWN_BUILD_MAPPING_SCHEMA,
            "source": {
                "instance_id": instance_id,
                "episode_id": _text(episode.get("episode_id"), "episode.episode_id"),
                "wave_id": _text(wave.get("wave_id"), "wave.wave_id"),
            },
            "decision_bindings": [],
        }
    else:
        if build_mapping.get("schema") != KNOWN_BUILD_MAPPING_SCHEMA:
            raise OfflineWavePolicyLibraryV1Error("unsupported build mapping schema")
        completed = deepcopy(dict(build_mapping))

    raw_bindings = _array(completed.get("decision_bindings"), "decision_bindings")
    binding_by_order: dict[tuple[int, ...], JSONMap] = {}
    for raw_binding in raw_bindings:
        binding = deepcopy(dict(_object(raw_binding, "decision binding")))
        order = binding.get("order_key")
        if not (
            isinstance(order, list)
            and order
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in order
            )
        ):
            continue
        binding_by_order[tuple(order)] = binding

    changed = False
    for raw_transition in _array(wave.get("prefix_transitions"), "prefix_transitions"):
        transition = _object(raw_transition, "prefix transition")
        observed = _object(transition.get("observed_event"), "observed_event")
        if observed.get("phase") != "START":
            continue
        order = observed.get("order_key")
        if not (
            isinstance(order, list)
            and len(order) == 4
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in order
            )
        ):
            raise OfflineWavePolicyLibraryV1Error(
                "START action timeline completion requires a four-integer order_key"
            )
        order_key = tuple(order)
        action_anchor = (order[0], order[1], order[3])
        selected_ref = _timeline_segment_at_action(timeline, action_anchor)
        if selected_ref is None:
            continue
        existing = binding_by_order.get(order_key)
        if existing is not None:
            existing_ref = existing.get("segment_ref")
            if existing_ref is not None and existing_ref != selected_ref:
                raise OfflineWavePolicyLibraryV1Error(
                    "decision/build binding disagrees with causal segment timeline "
                    f"for order_key {order!r}: {existing_ref!r} != {selected_ref!r}"
                )
            if existing_ref is None:
                existing["segment_ref"] = selected_ref
                changed = True
            continue
        binding_by_order[order_key] = {
            "order_key": list(order),
            "segment_ref": selected_ref,
            "join_status": "JOINED_EXACT_CAUSAL_PREFIX_FROM_SEGMENT_TIMELINE",
        }
        changed = True

    if not changed and build_mapping is not None:
        return build_mapping
    if not binding_by_order:
        return build_mapping
    completed["decision_bindings"] = [
        binding_by_order[order]
        for order in sorted(binding_by_order)
    ]
    return completed


def _complete_coverage(episode: Mapping[str, Any]) -> bool:
    window_join = _object(episode.get("window_join"), "episode.window_join")
    coverage = _object(window_join.get("coverage"), "episode.window_join.coverage")
    partial = coverage.get("partial")
    if not isinstance(partial, bool):
        raise OfflineWavePolicyLibraryV1Error(
            "episode.window_join.coverage.partial must be boolean"
        )
    return not partial


def _identity(
    *,
    instance_id: str,
    episode_id: str,
    wave_id: str,
    wave_index: int,
) -> JSONMap:
    return {
        "instance_id": instance_id,
        "episode_id": episode_id,
        "wave_id": wave_id,
        "wave_index": wave_index,
    }


def _classify_expected_compile_rejection(error: OfflineWavePolicyV1Error) -> str:
    message = str(error)
    if message == "wave has no executable observed START actions":
        return REJECTED_NO_ACTION
    if message == "wave has no source-observed executable GCD for an independent tail":
        return REJECTED_NO_GCD
    raise OfflineWavePolicyLibraryV1Error(
        f"selected episode/wave cannot be compiled: {message}"
    ) from error


def build_offline_wave_policy_library_v1(
    episode_manifest_path: str | Path = DEFAULT_EPISODE_MANIFEST,
    *,
    build_join_manifest_path: str | Path | None = None,
    instance_ids: Iterable[str] | None = None,
    episode_ids: Iterable[str] | None = None,
    complete_only: bool = False,
    runtime_executable_only: bool = False,
    max_modes: int | None = None,
) -> JSONMap:
    """Stream compact partitions and preserve every selected wave as a mode."""

    if not isinstance(complete_only, bool):
        raise TypeError("complete_only must be boolean")
    if not isinstance(runtime_executable_only, bool):
        raise TypeError("runtime_executable_only must be boolean")
    if max_modes is not None and (
        isinstance(max_modes, bool) or not isinstance(max_modes, int) or max_modes <= 0
    ):
        raise ValueError("max_modes must be a positive integer or None")
    selected_instances = None
    if instance_ids is not None:
        selected_instances = frozenset(_text(row, "instance_id") for row in instance_ids)
        if not selected_instances:
            selected_instances = None
    selected_episodes = None
    if episode_ids is not None:
        selected_episodes = frozenset(
            _text(row, "episode_id") for row in episode_ids
        )
        if not selected_episodes:
            selected_episodes = None

    episode_manifest_file = Path(episode_manifest_path).expanduser().resolve()
    episode_manifest, episode_partitions = _episode_descriptors(
        episode_manifest_file, instance_ids=selected_instances
    )

    mapping_manifest: Mapping[str, Any] | None = None
    mapping_manifest_file: Path | None = None
    mapping_partitions: dict[str, Mapping[str, Any]] = {}
    runtime_segment_refs: frozenset[str] = frozenset()
    segment_timelines: dict[
        tuple[str, str], tuple[tuple[tuple[int, int, int], str], ...]
    ] = {}
    if build_join_manifest_path is not None:
        mapping_manifest_file = Path(build_join_manifest_path).expanduser().resolve()
        mapping_manifest, mapping_partitions = _mapping_descriptors(
            mapping_manifest_file
        )
        if mapping_manifest.get("segment_dictionary") is not None:
            runtime_segment_refs, segment_timelines = _segment_dictionary(
                mapping_manifest_file, mapping_manifest
            )
        elif runtime_executable_only:
            raise OfflineWavePolicyLibraryV1Error(
                "build join manifest has no segment dictionary"
            )
        if runtime_executable_only and not runtime_segment_refs:
            raise OfflineWavePolicyLibraryV1Error(
                "build segment dictionary has no runtime-executable segment"
            )
    elif runtime_executable_only:
        raise ValueError(
            "runtime_executable_only requires build_join_manifest_path"
        )

    modes: list[JSONMap] = []
    selected: list[JSONMap] = []
    rejected: list[JSONMap] = []
    rejection_counts: Counter[str] = Counter()
    episode_rows_seen = 0
    waves_seen = 0
    episode_rows_matched = 0
    mapping_matches = 0
    modes_without_mapping = 0
    scan_truncated = False
    seen_mode_ids: set[str] = set()
    action_counts: Counter[str] = Counter()
    evidence_status_counts: Counter[str] = Counter()
    unresolved_spell_counts: Counter[str] = Counter()

    for descriptor in episode_partitions:
        instance_id = _text(descriptor.get("instance_id"), "partition instance_id")
        if descriptor.get("record_schema") != KNOWN_EPISODE_SCHEMA:
            raise OfflineWavePolicyLibraryV1Error(
                f"episode partition {instance_id} has unsupported record schema"
            )
        mappings = _load_instance_build_mappings(
            mapping_manifest_file or episode_manifest_file,
            mapping_partitions.get(instance_id),
            instance_id=instance_id,
        )
        episode_path = _partition_path(
            episode_manifest_file, descriptor, "episode partition"
        )
        for episode in _iter_jsonl_gzip(episode_path, "episode partition"):
            episode_rows_seen += 1
            if episode.get("schema") != KNOWN_EPISODE_SCHEMA:
                raise OfflineWavePolicyLibraryV1Error(
                    f"episode row in {instance_id} has unsupported schema"
                )
            row_instance = _text(episode.get("instance_id"), "episode.instance_id")
            if row_instance != instance_id:
                raise OfflineWavePolicyLibraryV1Error(
                    f"episode instance {row_instance} differs from partition {instance_id}"
                )
            episode_id = _text(episode.get("episode_id"), "episode.episode_id")
            if selected_episodes is not None and episode_id not in selected_episodes:
                continue
            episode_rows_matched += 1
            waves = _array(episode.get("wave_observations"), "episode.wave_observations")
            complete = _complete_coverage(episode)
            for wave_index, raw_wave in enumerate(waves):
                wave = _object(raw_wave, "wave observation")
                wave_id = _text(wave.get("wave_id"), "wave.wave_id")
                waves_seen += 1
                identity = _identity(
                    instance_id=instance_id,
                    episode_id=episode_id,
                    wave_id=wave_id,
                    wave_index=wave_index,
                )
                if complete_only and not complete:
                    reason = REJECTED_PARTIAL
                    rejected.append({**identity, "reason": reason})
                    rejection_counts[reason] += 1
                    continue

                mapping = _complete_build_mapping_from_timeline(
                    episode,
                    wave_index=wave_index,
                    build_mapping=mappings.get((episode_id, wave_id)),
                    segment_timelines=segment_timelines,
                )
                try:
                    policy = compile_offline_wave_feedback_policy_v1(
                        episode,
                        wave_index=wave_index,
                        build_mapping=mapping,
                    )
                except OfflineWavePolicyV1Error as error:
                    reason = _classify_expected_compile_rejection(error)
                    rejected.append({**identity, "reason": reason})
                    rejection_counts[reason] += 1
                    continue

                fully_joined_segment_refs = {
                    row.build_segment_ref for row in policy.actions
                }
                if runtime_executable_only and (
                    len(fully_joined_segment_refs) != 1
                    or None in fully_joined_segment_refs
                    or next(iter(fully_joined_segment_refs))
                    not in runtime_segment_refs
                ):
                    reason = REJECTED_NO_RUNTIME_BUILD
                    rejected.append({**identity, "reason": reason})
                    rejection_counts[reason] += 1
                    continue

                if policy.mode_id in seen_mode_ids:
                    raise OfflineWavePolicyLibraryV1Error(
                        f"duplicate compiled mode_id {policy.mode_id}"
                    )
                seen_mode_ids.add(policy.mode_id)
                if mapping is None:
                    modes_without_mapping += 1
                    mapping_status = (
                        "BUILD_JOIN_NOT_REQUESTED"
                        if mapping_manifest_file is None
                        else "NO_MATCHING_BUILD_MAPPING"
                    )
                else:
                    mapping_matches += 1
                    mapping_status = "EXACT_EPISODE_WAVE_BUILD_MAPPING"
                mode = policy.to_dict()
                modes.append(mode)
                action_counts.update(row.action_key for row in policy.actions)
                evidence_status_counts.update(
                    row.evidence_status for row in policy.actions
                )
                unresolved_spell_counts.update(
                    str(row.get("spell_id"))
                    for row in policy.unresolved_start_evidence
                )
                selected.append(
                    {
                        **identity,
                        "mode_id": policy.mode_id,
                        "reason": SELECTED_EXACT_MODE,
                        "build_mapping_status": mapping_status,
                    }
                )
                if max_modes is not None and len(modes) >= max_modes:
                    scan_truncated = True
                    break
            if scan_truncated:
                break
        if scan_truncated:
            break

    return {
        "schema": LIBRARY_SCHEMA,
        "status": "DEVELOPMENT_OFFLINE_MODES_NOT_YET_COMPARISON",
        "input_scope": {
            "episode_manifest_schema": episode_manifest.get("schema"),
            "build_join_manifest_schema": (
                mapping_manifest.get("schema") if mapping_manifest is not None else None
            ),
            "selected_instance_ids": sorted(
                descriptor["instance_id"] for descriptor in episode_partitions
            ),
            "selected_episode_ids": sorted(selected_episodes or ()),
            "compact_manifest_partitions_only": True,
            "raw_chronicle_opened": False,
            "chronicle_api_called": False,
        },
        "selection": {
            "complete_only": complete_only,
            "runtime_executable_only": runtime_executable_only,
            "max_modes": max_modes,
            "scan_truncated_by_max_modes": scan_truncated,
            "selected": selected,
            "rejected": rejected,
        },
        "counts": {
            "episode_partition_count_selected": len(episode_partitions),
            "episode_rows_seen": episode_rows_seen,
            "episode_rows_matched": episode_rows_matched,
            "player_waves_seen": waves_seen,
            "mode_count": len(modes),
            "rejected_count": len(rejected),
            "rejection_reason_counts": dict(sorted(rejection_counts.items())),
            "mode_with_exact_build_mapping_count": mapping_matches,
            "mode_without_exact_build_mapping_count": modes_without_mapping,
            "runtime_executable_segment_count": len(runtime_segment_refs),
            "selected_action_counts": dict(sorted(action_counts.items())),
            "selected_evidence_status_counts": dict(
                sorted(evidence_status_counts.items())
            ),
            "selected_unresolved_start_spell_counts": dict(
                sorted(unresolved_spell_counts.items())
            ),
        },
        "modes": modes,
        "contract": {
            "unit": "ONE_EXACT_PLAYER_WAVE_DEMONSTRATION_PER_MODE",
            "different_trajectories_merged": False,
            "global_action_priority_averaging": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }


def write_offline_wave_policy_library_v1(
    output_path: str | Path,
    **build_kwargs: Any,
) -> JSONMap:
    """Build and write one compact JSON library and its selection receipt."""

    result = build_offline_wave_policy_library_v1(**build_kwargs)
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
            "Compile exact player-wave Chronicle demonstrations into a compact "
            "non-aggregated development policy library."
        )
    )
    parser.add_argument(
        "--episode-manifest", type=Path, default=DEFAULT_EPISODE_MANIFEST
    )
    parser.add_argument("--build-join-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--episode-id", action="append", default=[])
    parser.add_argument("--complete-only", action="store_true")
    parser.add_argument("--runtime-executable-only", action="store_true")
    parser.add_argument("--max-modes", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = write_offline_wave_policy_library_v1(
        args.output,
        episode_manifest_path=args.episode_manifest,
        build_join_manifest_path=args.build_join_manifest,
        instance_ids=args.instance_id or None,
        episode_ids=args.episode_id or None,
        complete_only=args.complete_only,
        runtime_executable_only=args.runtime_executable_only,
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
    "DEFAULT_EPISODE_MANIFEST",
    "EPISODE_MANIFEST_SCHEMA",
    "OfflineWavePolicyLibraryV1Error",
    "build_offline_wave_policy_library_v1",
    "main",
    "write_offline_wave_policy_library_v1",
]
