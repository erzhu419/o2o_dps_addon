import gzip
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.offline_wave_policy_library_v1 import (
    BUILD_JOIN_MANIFEST_SCHEMA,
    EPISODE_MANIFEST_SCHEMA,
    OfflineWavePolicyLibraryV1Error,
    REJECTED_PARTIAL,
    REJECTED_NO_RUNTIME_BUILD,
    build_offline_wave_policy_library_v1,
    main,
)
from o2o_dps.offline_wave_policy_v1 import (
    KNOWN_BUILD_MAPPING_SCHEMA,
    KNOWN_EPISODE_SCHEMA,
    LIBRARY_SCHEMA,
)


def _transition(
    action_key: str,
    *,
    elapsed_ms: int,
    event_index: int,
    target_guid: str = "CREATURE-1",
) -> dict[str, object]:
    order_key = [1_000_000 + elapsed_ms, event_index, 3, event_index]
    return {
        "state_before": {"wave_elapsed_ms": elapsed_ms},
        "observed_event": {
            "phase": "START",
            "action_key": action_key,
            "order_key": order_key,
            "exact_target": {
                "guid": target_guid,
                "lane": "HOSTILE_CREATURE",
                "voting_enemy_target": True,
            },
        },
    }


def _episode(
    episode_id: str,
    action_keys: list[str],
    *,
    instance_id: str = "INSTANCE-A",
    partial: bool = False,
    player_guid: str = "PLAYER-A",
) -> dict[str, object]:
    transitions = [
        _transition(
            action_key,
            elapsed_ms=index * 700,
            event_index=index + 1,
            target_guid="CREATURE-1" if index == 0 else "CREATURE-2",
        )
        for index, action_key in enumerate(action_keys)
    ]
    return {
        "schema": KNOWN_EPISODE_SCHEMA,
        "instance_id": instance_id,
        "episode_id": episode_id,
        "player": {"guid": player_guid, "name": f"name-{player_guid}"},
        "exact_dps_window": {"encounter_name": "Fixture encounter"},
        "window_join": {"coverage": {"partial": partial}},
        "wave_observations": [
            {
                "wave_id": f"wave-{episode_id}",
                "wave_ordinal": 1,
                "window": {"boundary_duration_ms": 5_000},
                "prefix_transitions": transitions,
            }
        ],
    }


def _mapping(episode: dict[str, object], prefix: str) -> dict[str, object]:
    wave = episode["wave_observations"][0]
    bindings = []
    for index, transition in enumerate(wave["prefix_transitions"]):
        bindings.append(
            {
                "order_key": transition["observed_event"]["order_key"],
                "segment_ref": f"segment:{prefix}:{index}",
            }
        )
    return {
        "schema": KNOWN_BUILD_MAPPING_SCHEMA,
        "source": {
            "instance_id": episode["instance_id"],
            "episode_id": episode["episode_id"],
            "wave_id": wave["wave_id"],
        },
        "decision_bindings": bindings,
    }


def _write_gzip_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _fixture_manifests(
    root: Path,
    episodes: list[dict[str, object]],
    *,
    mappings: list[dict[str, object]] | None = None,
    runtime_segments: set[str] | None = None,
    segment_rows: list[dict[str, object]] | None = None,
) -> tuple[Path, Path | None]:
    instance_id = str(episodes[0]["instance_id"])
    episode_partition = root / "episodes.jsonl.gz"
    _write_gzip_rows(episode_partition, episodes)
    episode_manifest = root / "episode-manifest.json"
    episode_manifest.write_text(
        json.dumps(
            {
                "schema": EPISODE_MANIFEST_SCHEMA,
                "partitions": [
                    {
                        "instance_id": instance_id,
                        "path": episode_partition.name,
                        "record_schema": KNOWN_EPISODE_SCHEMA,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if mappings is None:
        return episode_manifest, None

    mapping_partition = root / "mappings.jsonl.gz"
    _write_gzip_rows(mapping_partition, mappings)
    mapping_manifest = root / "mapping-manifest.json"
    manifest: dict[str, object] = {
        "schema": BUILD_JOIN_MANIFEST_SCHEMA,
        "mapping_partitions": [
            {
                "instance_id": instance_id,
                "path": mapping_partition.name,
                "record_schema": KNOWN_BUILD_MAPPING_SCHEMA,
            }
        ],
    }
    if runtime_segments is not None or segment_rows is not None:
        segment_partition = root / "segments.jsonl.gz"
        if segment_rows is None:
            all_segments = {
                binding["segment_ref"]
                for mapping in mappings
                for binding in mapping["decision_bindings"]
            }
            segment_rows = [
                {
                    "segment_ref": segment_ref,
                    "coverage_flags": {
                        "runtime_executable": segment_ref in (runtime_segments or set())
                    },
                }
                for segment_ref in sorted(all_segments)
            ]
        _write_gzip_rows(
            segment_partition,
            segment_rows,
        )
        manifest["segment_dictionary"] = {
            "partition": {"path": segment_partition.name}
        }
    mapping_manifest.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return episode_manifest, mapping_manifest


def _check_preserves_distinct_trajectory_order_and_exact_build_mapping(
    tmp_path: Path,
) -> None:
    first = _episode(
        "episode-1", ["warrior.bloodthirst", "warrior.whirlwind"]
    )
    second = _episode(
        "episode-2",
        ["warrior.whirlwind", "warrior.bloodthirst"],
        player_guid="PLAYER-B",
    )
    episode_manifest, mapping_manifest = _fixture_manifests(
        tmp_path,
        [first, second],
        mappings=[_mapping(first, "one"), _mapping(second, "two")],
    )

    library = build_offline_wave_policy_library_v1(
        episode_manifest,
        build_join_manifest_path=mapping_manifest,
    )

    assert library["schema"] == LIBRARY_SCHEMA
    assert library["counts"]["mode_count"] == 2
    assert library["counts"]["mode_with_exact_build_mapping_count"] == 2
    assert len({row["mode_id"] for row in library["modes"]}) == 2
    assert [
        action["action_key"] for action in library["modes"][0]["actions"]
    ] == ["warrior.bloodthirst", "warrior.whirlwind"]
    assert [
        action["action_key"] for action in library["modes"][1]["actions"]
    ] == ["warrior.whirlwind", "warrior.bloodthirst"]
    assert library["modes"][0]["build_binding"]["segment_refs"] == [
        "segment:one:0",
        "segment:one:1",
    ]
    assert library["selection"]["selected"][0]["build_mapping_status"] == (
        "EXACT_EPISODE_WAVE_BUILD_MAPPING"
    )
    assert library["contract"]["different_trajectories_merged"] is False
    assert library["contract"]["comparison_authorized"] is False
    assert library["contract"]["deployment_authorized"] is False


def _check_complete_only_records_partial_wave_rejection(tmp_path: Path) -> None:
    partial = _episode("episode-partial", ["warrior.bloodthirst"], partial=True)
    complete = _episode("episode-complete", ["warrior.whirlwind"])
    episode_manifest, _ = _fixture_manifests(tmp_path, [partial, complete])

    library = build_offline_wave_policy_library_v1(
        episode_manifest,
        complete_only=True,
    )

    assert library["counts"]["mode_count"] == 1
    assert library["counts"]["rejected_count"] == 1
    assert library["counts"]["rejection_reason_counts"] == {
        REJECTED_PARTIAL: 1
    }
    assert library["selection"]["rejected"][0]["episode_id"] == (
        "episode-partial"
    )
    assert library["selection"]["rejected"][0]["reason"] == REJECTED_PARTIAL
    assert library["modes"][0]["source"]["episode_id"] == "episode-complete"


def _check_max_modes_stops_after_exact_modes_without_pooling(tmp_path: Path) -> None:
    episodes = [
        _episode(f"episode-{index}", ["warrior.bloodthirst"])
        for index in range(3)
    ]
    episode_manifest, _ = _fixture_manifests(tmp_path, episodes)

    library = build_offline_wave_policy_library_v1(
        episode_manifest,
        max_modes=2,
    )

    assert library["counts"]["mode_count"] == 2
    assert library["counts"]["episode_rows_seen"] == 2
    assert library["selection"]["scan_truncated_by_max_modes"] is True
    assert [row["source"]["episode_id"] for row in library["modes"]] == [
        "episode-0",
        "episode-1",
    ]


def _check_cli_writes_compact_receipt_without_raw_or_api_access(
    tmp_path: Path,
) -> None:
    episode_manifest, _ = _fixture_manifests(
        tmp_path,
        [_episode("episode-1", ["warrior.bloodthirst"])],
    )
    output = tmp_path / "library.json"

    assert (
        main(
            [
                "--episode-manifest",
                str(episode_manifest),
                "--output",
                str(output),
                "--instance-id",
                "INSTANCE-A",
                "--max-modes",
                "1",
            ]
        )
        == 0
    )
    library = json.loads(output.read_text(encoding="utf-8"))
    assert library["counts"]["mode_count"] == 1
    assert library["input_scope"]["raw_chronicle_opened"] is False
    assert library["input_scope"]["chronicle_api_called"] is False
    assert library["selection"]["selected"][0]["reason"] == (
        "EXACT_PLAYER_WAVE_MODE_PRESERVED"
    )


def _check_episode_and_runtime_build_filter(tmp_path: Path) -> None:
    accepted = _episode("episode-accepted", ["warrior.bloodthirst"])
    rejected = _episode(
        "episode-rejected",
        ["warrior.whirlwind"],
        player_guid="PLAYER-B",
    )
    accepted_mapping = _mapping(accepted, "accepted")
    rejected_mapping = _mapping(rejected, "rejected")
    runtime_ref = accepted_mapping["decision_bindings"][0]["segment_ref"]
    episode_manifest, mapping_manifest = _fixture_manifests(
        tmp_path,
        [accepted, rejected],
        mappings=[accepted_mapping, rejected_mapping],
        runtime_segments={runtime_ref},
    )

    library = build_offline_wave_policy_library_v1(
        episode_manifest,
        build_join_manifest_path=mapping_manifest,
        episode_ids={"episode-accepted"},
        runtime_executable_only=True,
    )
    assert library["counts"]["mode_count"] == 1
    assert library["counts"]["episode_rows_seen"] == 2
    assert library["counts"]["episode_rows_matched"] == 1
    assert library["counts"]["runtime_executable_segment_count"] == 1
    assert library["counts"]["selected_action_counts"] == {
        "warrior.bloodthirst": 1
    }
    assert library["input_scope"]["selected_episode_ids"] == [
        "episode-accepted"
    ]
    assert library["selection"]["runtime_executable_only"] is True

    only_rejected = build_offline_wave_policy_library_v1(
        episode_manifest,
        build_join_manifest_path=mapping_manifest,
        episode_ids={"episode-rejected"},
        runtime_executable_only=True,
    )
    assert only_rejected["counts"]["mode_count"] == 0
    assert only_rejected["counts"]["rejection_reason_counts"] == {
        REJECTED_NO_RUNTIME_BUILD: 1
    }


def _check_runtime_filter_rejects_partially_joined_action_sequence(
    tmp_path: Path,
) -> None:
    episode = _episode(
        "episode-partial-build-join",
        ["warrior.bloodthirst", "warrior.whirlwind"],
    )
    mapping = _mapping(episode, "shared")
    joined_ref = mapping["decision_bindings"][0]["segment_ref"]
    mapping["decision_bindings"] = mapping["decision_bindings"][:1]
    episode_manifest, mapping_manifest = _fixture_manifests(
        tmp_path,
        [episode],
        mappings=[mapping],
        runtime_segments={joined_ref},
    )

    library = build_offline_wave_policy_library_v1(
        episode_manifest,
        build_join_manifest_path=mapping_manifest,
        runtime_executable_only=True,
    )

    assert library["counts"]["mode_count"] == 0
    assert library["counts"]["rejection_reason_counts"] == {
        REJECTED_NO_RUNTIME_BUILD: 1
    }


def _timeline_segment_row(
    episode: dict[str, object],
    *,
    segment_ref: str,
    order_key: list[int],
    runtime_executable: bool = True,
) -> dict[str, object]:
    return {
        "segment_ref": segment_ref,
        "identity": {
            "instance_id": episode["instance_id"],
            "player_guid": episode["player"]["guid"],
        },
        "valid_from": {
            "timestamp_ms": order_key[0],
            "event_index": order_key[1],
            "message_ordinal": order_key[3],
        },
        "coverage_flags": {"runtime_executable": runtime_executable},
    }


def _check_timeline_completes_missing_action_binding(tmp_path: Path) -> None:
    episode = _episode(
        "episode-timeline-completion",
        ["warrior.bloodthirst", "warrior.whirlwind"],
    )
    mapping = _mapping(episode, "shared")
    shared_ref = mapping["decision_bindings"][0]["segment_ref"]
    mapping["decision_bindings"] = mapping["decision_bindings"][:1]
    first_order = episode["wave_observations"][0]["prefix_transitions"][0][
        "observed_event"
    ]["order_key"]
    episode_manifest, mapping_manifest = _fixture_manifests(
        tmp_path,
        [episode],
        mappings=[mapping],
        runtime_segments={shared_ref},
        segment_rows=[
            _timeline_segment_row(
                episode,
                segment_ref=shared_ref,
                order_key=first_order,
            )
        ],
    )

    library = build_offline_wave_policy_library_v1(
        episode_manifest,
        build_join_manifest_path=mapping_manifest,
        runtime_executable_only=True,
    )

    assert library["counts"]["mode_count"] == 1
    assert library["modes"][0]["build_binding"][
        "all_executable_actions_joined_to_segment"
    ] is True
    assert [
        row["build_segment_ref"] for row in library["modes"][0]["actions"]
    ] == [shared_ref, shared_ref]


def _check_timeline_disagreement_fails_closed(tmp_path: Path) -> None:
    episode = _episode(
        "episode-timeline-disagreement",
        ["warrior.bloodthirst"],
    )
    mapping = _mapping(episode, "mapping")
    first_order = episode["wave_observations"][0]["prefix_transitions"][0][
        "observed_event"
    ]["order_key"]
    episode_manifest, mapping_manifest = _fixture_manifests(
        tmp_path,
        [episode],
        mappings=[mapping],
        segment_rows=[
            _timeline_segment_row(
                episode,
                segment_ref="segment:timeline:other",
                order_key=first_order,
            )
        ],
    )

    with unittest.TestCase().assertRaisesRegex(
        OfflineWavePolicyLibraryV1Error,
        "disagrees with causal segment timeline",
    ):
        build_offline_wave_policy_library_v1(
            episode_manifest,
            build_join_manifest_path=mapping_manifest,
        )


class OfflineWavePolicyLibraryV1Test(unittest.TestCase):
    def _temporary_path(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory(
            prefix="offline_wave_policy_library_v1_"
        )
        return temporary, Path(temporary.name)

    def test_preserves_distinct_trajectory_order_and_exact_build_mapping(
        self,
    ) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_preserves_distinct_trajectory_order_and_exact_build_mapping(path)

    def test_complete_only_records_partial_wave_rejection(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_complete_only_records_partial_wave_rejection(path)

    def test_max_modes_stops_after_exact_modes_without_pooling(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_max_modes_stops_after_exact_modes_without_pooling(path)

    def test_cli_writes_compact_receipt_without_raw_or_api_access(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_cli_writes_compact_receipt_without_raw_or_api_access(path)

    def test_episode_and_runtime_build_filter(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_episode_and_runtime_build_filter(path)

    def test_runtime_filter_rejects_partially_joined_action_sequence(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_runtime_filter_rejects_partially_joined_action_sequence(path)

    def test_timeline_completes_missing_action_binding(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_timeline_completes_missing_action_binding(path)

    def test_timeline_disagreement_fails_closed(self) -> None:
        temporary, path = self._temporary_path()
        with temporary:
            _check_timeline_disagreement_fails_closed(path)
