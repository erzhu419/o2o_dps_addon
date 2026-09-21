from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path

import pytest

from o2o_dps.offline_team_wave_trash_policy_library_v1 import (
    BUILD_JOIN_MANIFEST_SCHEMA,
    RAW_API_MANIFEST_SCHEMA,
    REJECTED_BOSS,
    REJECTED_BOSS_UNRESOLVED,
    REJECTED_MULTIPLE_SEGMENTS,
    REJECTED_NOT_RUNTIME,
    REJECTED_NO_SEGMENT,
    REJECTED_NO_TARGET,
    REJECTED_PARTIAL_BOUNDARY,
    REJECTED_SPEC,
    SCHEMA,
    TEAM_MANIFEST_SCHEMA,
    OfflineTeamWaveTrashPolicyLibraryV1Error,
    build_offline_team_wave_trash_policy_library_v1,
    main,
)
from tests.test_offline_team_wave_policy_v1 import _team_wave


FURY_GUID = "0x0000000000576754"
ARMS_GUID = "0x0000000000A00002"
CONFLICT_GUID = "0x0000000000C00003"
ROGUE_GUID = "0x0000000000D00004"
BASE_TIMESTAMP = 1_788_532_098_282


def _player(
    guid: str,
    spec: str,
    *,
    conflict_free: bool = True,
    hero_class: str = "WARRIOR",
) -> dict[str, object]:
    row = deepcopy(_team_wave()["players"][0])
    row["player"] = dict(
        row["player"], guid=guid, name=f"player-{guid}", **{"class": hero_class}
    )
    row["warrior_spec_lane"] = {
        "evidence_status": "OBSERVED",
        "exact_guid_match": True,
        "fury_or_arms_conflict_free_observation": conflict_free,
        "observed_spec": spec,
    }
    return row


def _wave(
    *,
    instance_id: str,
    encounter_id: str,
    wave_ordinal: int,
    players: list[dict[str, object]],
) -> dict[str, object]:
    row = _team_wave()
    row["wave"] = dict(
        row["wave"],
        instance_id=instance_id,
        encounter_id=encounter_id,
        wave_id=f"{encounter_id}:external-v2-wave:{wave_ordinal}",
        wave_ordinal=wave_ordinal,
    )
    row["players"] = players
    return row


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _segment_row(
    instance_id: str,
    player_guid: str,
    segment_ref: str,
    *,
    timestamp_ms: int = BASE_TIMESTAMP,
    event_index: int = 0,
    message_ordinal: int = 0,
    runtime: bool = True,
) -> dict[str, object]:
    return {
        "segment_ref": segment_ref,
        "identity": {
            "instance_id": instance_id,
            "player_guid": player_guid,
        },
        "valid_from": {
            "timestamp_ms": timestamp_ms,
            "event_index": event_index,
            "message_ordinal": message_ordinal,
        },
        "coverage_flags": {"runtime_executable": runtime},
    }


def _fixture(
    root: Path,
    *,
    waves_by_instance: dict[str, list[dict[str, object]]],
    encounters_by_instance: dict[str, list[dict[str, object]]],
    segment_rows: list[dict[str, object]],
) -> tuple[Path, Path, Path]:
    data_root = root / "offline_data"
    team_dir = data_root / "derived" / "team"
    team_dir.mkdir(parents=True)
    instance_entries = []
    for instance_id, waves in waves_by_instance.items():
        partition = team_dir / f"{instance_id}.jsonl.gz"
        _write_rows(partition, waves)
        instance_entries.append(
            {
                "instance_id": instance_id,
                "partition": {
                    "path": partition.name,
                    "record_schema": "chronicle_external_team_wave_model_wave/v2",
                },
            }
        )

    raw_base = data_root / "chronicle_raw" / "external_api" / "v1"
    raw_manifest_path = raw_base / "manifests" / "raw.json"
    raw_manifest_path.parent.mkdir(parents=True)
    raw_instances = []
    for instance_id, encounters in encounters_by_instance.items():
        metadata_path = raw_base / "objects" / f"{instance_id}.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps({"id": instance_id, "encounters": encounters}),
            encoding="utf-8",
        )
        raw_instances.append(
            {
                "instance_id": instance_id,
                "metadata": {
                    "object": {
                        "relative_path": f"objects/{instance_id}.json",
                    }
                },
            }
        )
    raw_manifest_path.write_text(
        json.dumps(
            {"schema": RAW_API_MANIFEST_SCHEMA, "instances": raw_instances}
        ),
        encoding="utf-8",
    )

    timeline_path = data_root / "derived" / "timeline" / "manifest.json"
    timeline_path.parent.mkdir(parents=True)
    timeline_path.write_text(
        json.dumps(
            {
                "input_closure": {
                    "raw_api_manifest": {
                        "path": (
                            "chronicle_raw/external_api/v1/manifests/raw.json"
                        )
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    team_manifest_path = team_dir / "manifest.json"
    team_manifest_path.write_text(
        json.dumps(
            {
                "schema": TEAM_MANIFEST_SCHEMA,
                "input_closure": {
                    "timeline_manifest": {
                        "stable_path": "derived/timeline/manifest.json"
                    }
                },
                "instances": instance_entries,
            }
        ),
        encoding="utf-8",
    )

    build_dir = data_root / "derived" / "build"
    build_dir.mkdir(parents=True)
    segment_path = build_dir / "segments.jsonl.gz"
    _write_rows(segment_path, segment_rows)
    build_manifest_path = build_dir / "manifest.json"
    build_manifest_path.write_text(
        json.dumps(
            {
                "schema": BUILD_JOIN_MANIFEST_SCHEMA,
                "segment_dictionary": {
                    "partition": {"path": segment_path.name}
                },
            }
        ),
        encoding="utf-8",
    )
    return team_manifest_path, raw_manifest_path, build_manifest_path


def test_selects_only_explicit_trash_and_preserves_each_player_wave(
    tmp_path: Path,
) -> None:
    instance_id = "raid-a"
    trash = _wave(
        instance_id=instance_id,
        encounter_id="trash-encounter",
        wave_ordinal=1,
        players=[
            _player(FURY_GUID, "Fury"),
            _player(ARMS_GUID, "Arms"),
            _player(CONFLICT_GUID, "Fury", conflict_free=False),
            _player(ROGUE_GUID, "Unknown", hero_class="ROGUE"),
        ],
    )
    boss = _wave(
        instance_id=instance_id,
        encounter_id="boss-encounter",
        wave_ordinal=2,
        players=[_player(FURY_GUID, "Fury")],
    )
    unresolved = _wave(
        instance_id=instance_id,
        encounter_id="unknown-encounter",
        wave_ordinal=3,
        players=[_player(FURY_GUID, "Fury")],
    )
    team, _, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [trash, boss, unresolved]},
        encounters_by_instance={
            instance_id: [
                {"id": "trash-encounter", "name": "Trash Pull", "boss": False},
                {"id": "boss-encounter", "name": "Boss", "boss": True},
                {"id": "unknown-encounter", "name": "Unknown"},
            ]
        },
        segment_rows=[
            _segment_row(instance_id, FURY_GUID, "segment:fury"),
            _segment_row(instance_id, ARMS_GUID, "segment:arms"),
        ],
    )

    library = build_offline_team_wave_trash_policy_library_v1(
        team,
        build_join_manifest_path=build,
    )

    assert library["schema"] == SCHEMA
    assert library["counts"]["mode_count"] == 2
    assert library["counts"]["selected_spec_counts"] == {"Arms": 1, "Fury": 1}
    assert library["counts"]["non_warrior_player_wave_rows_skipped"] == 1
    assert library["counts"]["rejection_reason_counts"] == {
        REJECTED_BOSS: 1,
        REJECTED_BOSS_UNRESOLVED: 1,
        REJECTED_SPEC: 1,
    }
    assert {row["source"]["player_guid"] for row in library["modes"]} == {
        FURY_GUID,
        ARMS_GUID,
    }
    assert {row["build_binding"]["segment_refs"][0] for row in library["modes"]} == {
        "segment:fury",
        "segment:arms",
    }
    assert all(row["trash_scope"]["metadata_boss"] is False for row in library["modes"])
    assert library["contract"]["different_players_or_waves_merged"] is False
    assert library["input_scope"]["raw_api_manifest_derived_from_team_closure"] is True


def test_rejects_cross_segment_missing_and_nonruntime_builds(tmp_path: Path) -> None:
    instance_id = "raid-build"
    nonruntime_guid = "0x0000000000B00002"
    missing_guid = "0x0000000000B00003"
    wave = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=1,
        players=[
            _player(FURY_GUID, "Fury"),
            _player(nonruntime_guid, "Fury"),
            _player(missing_guid, "Arms"),
        ],
    )
    team, raw, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [wave]},
        encounters_by_instance={
            instance_id: [{"id": "trash", "name": "Trash", "boss": False}]
        },
        segment_rows=[
            _segment_row(instance_id, FURY_GUID, "segment:first"),
            _segment_row(
                instance_id,
                FURY_GUID,
                "segment:second",
                timestamp_ms=BASE_TIMESTAMP + 1_000,
            ),
            _segment_row(
                instance_id,
                nonruntime_guid,
                "segment:nonruntime",
                runtime=False,
            ),
        ],
    )

    library = build_offline_team_wave_trash_policy_library_v1(
        team,
        build_join_manifest_path=build,
        raw_api_manifest_path=raw,
    )

    assert library["counts"]["mode_count"] == 0
    assert library["counts"]["rejection_reason_counts"] == {
        REJECTED_MULTIPLE_SEGMENTS: 1,
        REJECTED_NOT_RUNTIME: 1,
        REJECTED_NO_SEGMENT: 1,
    }


def test_instance_filter_and_max_modes_bound_streaming_scan(tmp_path: Path) -> None:
    instance_a = "raid-a"
    instance_b = "raid-b"
    waves = {
        instance_a: [
            _wave(
                instance_id=instance_a,
                encounter_id="trash-a",
                wave_ordinal=1,
                players=[_player(FURY_GUID, "Fury")],
            )
        ],
        instance_b: [
            _wave(
                instance_id=instance_b,
                encounter_id="trash-b",
                wave_ordinal=1,
                players=[_player(ARMS_GUID, "Arms")],
            ),
            _wave(
                instance_id=instance_b,
                encounter_id="trash-b",
                wave_ordinal=2,
                players=[_player(ARMS_GUID, "Arms")],
            ),
        ],
    }
    team, _, build = _fixture(
        tmp_path,
        waves_by_instance=waves,
        encounters_by_instance={
            instance_a: [{"id": "trash-a", "boss": False}],
            instance_b: [{"id": "trash-b", "boss": False}],
        },
        segment_rows=[
            _segment_row(instance_a, FURY_GUID, "segment:a"),
            _segment_row(instance_b, ARMS_GUID, "segment:b"),
        ],
    )

    library = build_offline_team_wave_trash_policy_library_v1(
        team,
        build_join_manifest_path=build,
        instance_ids=[instance_b],
        max_modes=1,
    )

    assert library["input_scope"]["selected_instance_ids"] == [instance_b]
    assert library["counts"]["team_wave_partition_count_selected"] == 1
    assert library["counts"]["wave_rows_seen"] == 1
    assert library["counts"]["mode_count"] == 1
    assert library["modes"][0]["source"]["instance_id"] == instance_b
    assert library["selection"]["scan_truncated_by_max_modes"] is True


def test_cli_writes_a_bounded_trash_library(tmp_path: Path) -> None:
    instance_id = "raid-cli"
    wave = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=1,
        players=[_player(FURY_GUID, "Fury")],
    )
    team, raw, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [wave]},
        encounters_by_instance={
            instance_id: [{"id": "trash", "boss": False}]
        },
        segment_rows=[_segment_row(instance_id, FURY_GUID, "segment:cli")],
    )
    output = tmp_path / "trash-library.json"

    assert main(
        [
            "--team-wave-manifest",
            str(team),
            "--build-join-manifest",
            str(build),
            "--raw-api-manifest",
            str(raw),
            "--instance-id",
            instance_id,
            "--spec",
            "Fury",
            "--max-modes",
            "1",
            "--output",
            str(output),
        ]
    ) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["counts"]["mode_count"] == 1
    assert document["input_scope"]["chronicle_api_called"] is False
    assert document["contract"]["comparison_authorized"] is False


def test_records_targetless_executable_wave_as_rejection(tmp_path: Path) -> None:
    instance_id = "raid-targetless"
    player = _player(FURY_GUID, "Fury")
    for transition in player["prefix_transitions"]:
        target = transition["current_event_label"]["target"]
        target.update(
            {
                "guid": None,
                "lane": "UNKNOWN_NONVOTING",
                "voting_enemy_target": False,
            }
        )
    wave = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=1,
        players=[player],
    )
    team, _, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [wave]},
        encounters_by_instance={
            instance_id: [{"id": "trash", "boss": False}]
        },
        segment_rows=[_segment_row(instance_id, FURY_GUID, "segment:targetless")],
    )

    library = build_offline_team_wave_trash_policy_library_v1(
        team,
        build_join_manifest_path=build,
    )

    assert library["counts"]["mode_count"] == 0
    assert library["counts"]["rejection_reason_counts"] == {
        REJECTED_NO_TARGET: 1
    }


def test_partial_boundary_is_rejected_without_aborting_following_wave(
    tmp_path: Path,
) -> None:
    instance_id = "raid-partial"
    partial = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=1,
        players=[_player(FURY_GUID, "Fury")],
    )
    partial["descriptive_outcome"]["reconstruction_binding"]["window"][
        "last_boundary_anchor"
    ]["offset_ms"] = 15_000
    zero_duration = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=2,
        players=[_player(FURY_GUID, "Fury")],
    )
    zero_duration["descriptive_outcome"]["reconstruction_binding"]["window"][
        "boundary_duration_ms"
    ] = 0
    complete = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=3,
        players=[_player(FURY_GUID, "Fury")],
    )
    team, _, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [partial, zero_duration, complete]},
        encounters_by_instance={
            instance_id: [{"id": "trash", "boss": False}]
        },
        segment_rows=[_segment_row(instance_id, FURY_GUID, "segment:complete")],
    )

    library = build_offline_team_wave_trash_policy_library_v1(
        team,
        build_join_manifest_path=build,
    )

    assert library["counts"]["wave_rows_seen"] == 3
    assert library["counts"]["mode_count"] == 1
    assert library["counts"]["rejection_reason_counts"] == {
        REJECTED_PARTIAL_BOUNDARY: 2
    }
    assert library["modes"][0]["source"]["wave_id"].endswith(":3")


def test_rejects_unknown_requested_instance(tmp_path: Path) -> None:
    instance_id = "raid-known"
    wave = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=1,
        players=[_player(FURY_GUID, "Fury")],
    )
    team, _, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [wave]},
        encounters_by_instance={
            instance_id: [{"id": "trash", "boss": False}]
        },
        segment_rows=[_segment_row(instance_id, FURY_GUID, "segment:known")],
    )

    with pytest.raises(
        OfflineTeamWaveTrashPolicyLibraryV1Error,
        match="requested instance_id not present",
    ):
        build_offline_team_wave_trash_policy_library_v1(
            team,
            build_join_manifest_path=build,
            instance_ids=["raid-missing"],
        )


def test_rejects_metadata_manifest_outside_team_input_closure(tmp_path: Path) -> None:
    instance_id = "raid-closure"
    wave = _wave(
        instance_id=instance_id,
        encounter_id="trash",
        wave_ordinal=1,
        players=[_player(FURY_GUID, "Fury")],
    )
    team, raw, build = _fixture(
        tmp_path,
        waves_by_instance={instance_id: [wave]},
        encounters_by_instance={
            instance_id: [{"id": "trash", "boss": False}]
        },
        segment_rows=[_segment_row(instance_id, FURY_GUID, "segment:closure")],
    )
    other = raw.with_name("other.json")
    other.write_text(raw.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(
        OfflineTeamWaveTrashPolicyLibraryV1Error,
        match="differs from the team-wave input closure",
    ):
        build_offline_team_wave_trash_policy_library_v1(
            team,
            build_join_manifest_path=build,
            raw_api_manifest_path=other,
        )
