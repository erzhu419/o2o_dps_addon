from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_character_dps_index_v1 as dps_index_v1
from o2o_dps import chronicle_external_team_timeline_v2 as timeline_v2
from o2o_dps import historical_fury_expert_cohort_v2 as cohort_v2
from o2o_dps import historical_fury_expert_episode_adapter_v1 as adapter


INSTANCE = "11111111-1111-1111-1111-111111111111"
ENCOUNTER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAYER = "0x00000000000000A1"
TARGET = "0xF130000000000001"
KILLED_AT = "2026-09-03T12:00:02.000Z"
KILLED_MS = adapter._epoch_milliseconds(KILLED_AT, "fixture killed_at")


def _canonical(value: object, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _address(value: dict) -> dict:
    core = deepcopy(value)
    core.pop("content_address", None)
    core["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }
    return core


def _source_manifest(row_count: int) -> dict:
    return {
        "schema": dps_index_v1.SCHEMA,
        "implementation_revision": dps_index_v1.IMPLEMENTATION_REVISION,
        "content_address": {"sha256": "a" * 64},
        "partition": {"path": "derived/index/fixture.jsonl.zst", "record_count": row_count},
        "summary": {
            "exact_ranking_record_count": row_count,
            "training_eligible_exact_membership_count": row_count,
            "censored_membership_count": 0,
        },
    }


def _dps_row(*, encounter_id: str | None, ranking_id: str) -> dict:
    started_at = "2026-09-03T20:00:00+08:00"
    guild_name = "其他公会"
    return {
        "character_guid": PLAYER,
        "character_name": "狂暴战甲",
        "server": "Capybara",
        "realm": "Basin of Stars",
        "instance_id": INSTANCE,
        "instance_name": cohort_v2.FROZEN_INSTANCE_NAME,
        "started_at": started_at,
        "uploaded_at": "2026-09-04T00:00:00Z",
        "guild": {"id": "guild-1", "name": guild_name},
        "contamination_label": ingest_v1.classify_range_bug(guild_name, started_at),
        "encounter_id": encounter_id,
        "encounter_name": "Boss" if encounter_id else "Trash",
        "killed_at": KILLED_AT,
        "damage_done": 2000.0,
        "duration_secs": 2.0,
        "dps": 1000.0,
        "spec": "Fury",
        "role": "dps",
        "ranking_record_id": ranking_id,
    }


def _event(phase: str, spell_id: int, name: str, offset_ms: int) -> dict:
    stream = {"START": "spell_start", "GO": "spell_go", "FAIL": "spell_fail"}[phase]
    event = {
        "event_type": phase,
        "anchor": {
            "timestamp_ms": KILLED_MS - 2000 + offset_ms,
            "offset_ms": offset_ms,
            "event_index": offset_ms,
            "stream_type": stream,
            "frame_index": 0,
            "frame_message_index": offset_ms,
            "derived_jsonl_line": offset_ms,
            "official_message_sha256": "b" * 64,
        },
        "attribution": {
            "attribution_kind": "DIRECT_FRIENDLY_PLAYER",
            "player_guid": PLAYER,
            "source_guid": PLAYER,
            "status": "EXACT_FRIENDLY_PLAYER_SOURCE",
        },
        "source": {"guid": PLAYER, "lane": "FRIENDLY_PLAYER"},
        "target": {
            "guid": TARGET,
            "lane": "HOSTILE_CREATURE",
            "voting_enemy_target": True,
            "hostile_object_preserved_nonvoting": False,
            "hostile_player_preserved_nonvoting": False,
        },
        "spell": {"id": spell_id, "name": name},
        "action": {"phase": phase},
    }
    return event


def _wave(
    *,
    timeline_spec: str = "Unknown",
    conflict: bool = True,
    first_shift: int = 0,
    last_shift: int = 0,
) -> dict:
    evidence = {
        "status": "UNKNOWN_NONVOTING" if timeline_spec == "Unknown" else "OBSERVED",
        "player_spec": timeline_spec,
        "field_conflicts": {"player_spec": ["Arms", "Fury"]} if conflict else {},
        "sources": ["instance_metadata"],
        "exact_player_guid_match": True,
        "voting_for_fury_or_arms_lane": timeline_spec in {"Arms", "Fury"} and not conflict,
        "inference_used": False,
    }
    core = {
        "schema": timeline_v2.PARTITION_RECORD_SCHEMA,
        "implementation_revision": timeline_v2.IMPLEMENTATION_REVISION,
        "status": timeline_v2.STATUS,
        "instance_id": INSTANCE,
        "encounter_id": ENCOUNTER,
        "encounter_ordinal": 1,
        "wave_id": f"{ENCOUNTER}:wave:1",
        "wave_ordinal": 1,
        "source_binding_sha256": "c" * 64,
        "reconstruction_binding": {
            "window": {
                "first_anchor": {"timestamp_ms": KILLED_MS - 2000 + first_shift},
                "last_context_anchor": {"timestamp_ms": KILLED_MS + last_shift},
            }
        },
        "players": [
            {
                "player": {
                    "guid": PLAYER,
                    "name": "狂暴战甲",
                    "class": "WARRIOR",
                    "race": "Human",
                    "level": 60,
                },
                "warrior_spec_evidence": evidence,
                "timeline": [
                    _event("START", 23894, "Bloodthirst", 500),
                    _event("GO", 23894, "Bloodthirst", 600),
                    _event("START", 21553, "Mortal Strike", 700),
                    _event("FAIL", 23894, "Bloodthirst", 800),
                ],
            }
        ],
        "death_markers": [],
    }
    return _address(core)


def _timestamp(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write_raw_metadata(
    root: Path,
    *,
    metadata_start_shift: int,
    metadata_end_shift: int,
    kill_type: str,
) -> dict:
    raw_root = root / "chronicle_raw" / "external_api" / "v1"
    metadata = {
        "id": INSTANCE,
        "encounters": [
            {
                "id": ENCOUNTER,
                "instance_id": INSTANCE,
                "name": "Boss",
                "boss": True,
                "kill_type": kill_type,
                "remaining": [] if kill_type == "clean" else [TARGET],
                "start_time": _timestamp(KILLED_MS - 2000 + metadata_start_shift),
                "end_time": _timestamp(KILLED_MS + metadata_end_shift),
            }
        ],
    }
    metadata_payload = _canonical(metadata)
    metadata_sha = hashlib.sha256(metadata_payload).hexdigest()
    metadata_relative = Path("objects") / "sha256" / metadata_sha[:2] / (
        f"{metadata_sha}.json"
    )
    metadata_path = raw_root / metadata_relative
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_bytes(metadata_payload)
    reference = {
        "relative_path": metadata_relative.as_posix(),
        "sha256": metadata_sha,
        "size_bytes": len(metadata_payload),
        "media_type": "application/json",
    }
    manifest = {
        "schema": ingest_v1.SCHEMA,
        "kind": "chronicle_external_api_raw_snapshot",
        "instances": [
            {"instance_id": INSTANCE, "metadata": {"object": reference}}
        ],
    }
    manifest_payload = _canonical(manifest)
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    manifest_path = raw_root / "manifests" / f"{manifest_sha}.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(manifest_payload)
    return {
        "path": manifest_path.relative_to(root).as_posix(),
        "schema": ingest_v1.SCHEMA,
        "file_sha256": manifest_sha,
        "size_bytes": len(manifest_payload),
    }


def _write_timeline(
    root: Path,
    wave: dict | list[dict],
    contamination_label: str,
    *,
    metadata_start_shift: int,
    metadata_end_shift: int,
    kill_type: str,
) -> Path:
    directory = root / "derived" / "timeline"
    directory.mkdir(parents=True)
    waves = wave if isinstance(wave, list) else [wave]
    logical = b"".join(_canonical(row, newline=True) for row in waves)
    partition_path = directory / "instance.jsonl.gz"
    with partition_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(logical)
    partition = {
        "path": partition_path.name,
        "record_schema": timeline_v2.PARTITION_RECORD_SCHEMA,
        "record_count": len(waves),
        "compressed_size_bytes": partition_path.stat().st_size,
        "compressed_file_sha256": hashlib.sha256(partition_path.read_bytes()).hexdigest(),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
    }
    raw_binding = _write_raw_metadata(
        root,
        metadata_start_shift=metadata_start_shift,
        metadata_end_shift=metadata_end_shift,
        kill_type=kill_type,
    )
    manifest = _address(
        {
            "schema": timeline_v2.SCHEMA,
            "kind": timeline_v2.KIND,
            "implementation_revision": timeline_v2.IMPLEMENTATION_REVISION,
            "status": timeline_v2.STATUS,
            "instance_order": [INSTANCE],
            "instances": [
                {
                    "instance_id": INSTANCE,
                    "instance_provenance": {
                        "temporal_and_guild_provenance": {
                            "contamination": {"label": contamination_label}
                        }
                    },
                    "source_binding": {"raw_api_manifest": raw_binding},
                    "partition": partition,
                }
            ],
        }
    )
    payload = _canonical(manifest, newline=True)
    stable = directory / "manifest.json"
    stable.write_bytes(payload)
    (directory / f"chronicle_external_team_timeline_v2.{manifest['content_address']['sha256']}.manifest.json").write_bytes(payload)
    return stable


def _fixture(
    root: Path,
    *,
    timeline_spec: str = "Unknown",
    conflict: bool = True,
    first_shift: int = 0,
    last_shift: int = 0,
    metadata_start_shift: int = 0,
    metadata_end_shift: int = 0,
    kill_type: str = "clean",
    interior_gap_ms: int = 0,
    duplicate_episode_join_key: bool = False,
    duplicate_ranking_id: bool = False,
) -> tuple[Path, Path, Path, cohort_v2.FrozenCohortSelection, Path]:
    data_root = root / "offline_data"
    source_path = data_root / "derived" / "index" / "manifest.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("{}", encoding="utf-8")
    rows = [
        _dps_row(encounter_id=ENCOUNTER, ranking_id="rank-1"),
        _dps_row(encounter_id=None, ranking_id="rank-null"),
    ]
    if duplicate_episode_join_key:
        rows.append(_dps_row(encounter_id=ENCOUNTER, ranking_id="rank-2"))
    if duplicate_ranking_id:
        duplicate = _dps_row(encounter_id=ENCOUNTER, ranking_id="rank-1")
        duplicate["character_guid"] = "0x00000000000000A2"
        duplicate["character_name"] = "狂暴战乙"
        rows.append(duplicate)
    source_manifest = _source_manifest(len(rows))
    selection = cohort_v2.select_frozen_cohort_rows(
        source_manifest,
        rows,
        source_manifest_path="derived/index/manifest.json",
    )
    cohort = cohort_v2.build_cohort_document(
        source_manifest,
        rows,
        source_manifest_path="derived/index/manifest.json",
    )
    cohort_path = data_root / "derived" / "cohort.json"
    cohort_path.write_bytes(_canonical(cohort, newline=True))
    wave: dict | list[dict] = _wave(
        timeline_spec=timeline_spec,
        conflict=conflict,
        first_shift=first_shift,
        last_shift=last_shift,
    )
    if interior_gap_ms:
        first = deepcopy(wave)
        first_window = first["reconstruction_binding"]["window"]
        first_window["last_context_anchor"]["timestamp_ms"] = KILLED_MS - 1200
        first = _address(first)

        second = deepcopy(wave)
        second["wave_id"] = f"{ENCOUNTER}:wave:2"
        second["wave_ordinal"] = 2
        second_window = second["reconstruction_binding"]["window"]
        second_window["first_anchor"]["timestamp_ms"] = (
            KILLED_MS - 1200 + interior_gap_ms
        )
        second["players"][0]["timeline"] = [
            _event("START", 23894, "Bloodthirst", 1500),
            _event("GO", 23894, "Bloodthirst", 1600),
        ]
        second = _address(second)
        wave = [first, second]
    timeline_path = _write_timeline(
        data_root,
        wave,
        rows[0]["contamination_label"],
        metadata_start_shift=metadata_start_shift,
        metadata_end_shift=metadata_end_shift,
        kill_type=kill_type,
    )
    return cohort_path, timeline_path, data_root / "derived" / "episodes", selection, source_path


def _read_episode(manifest_path: Path) -> tuple[dict, dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    partition = manifest["partitions"][0]
    with gzip.open(manifest_path.parent / partition["path"], "rt", encoding="utf-8") as handle:
        return manifest, json.loads(next(handle))


class HistoricalFuryExpertEpisodeAdapterV1Tests(unittest.TestCase):
    def _build(self, root: Path, **fixture_args: object) -> tuple[adapter.EpisodeBuildResult, dict, dict]:
        cohort, timeline, output, selection, source = _fixture(root, **fixture_args)
        with mock.patch.object(
            cohort_v2,
            "audit_historical_fury_expert_cohort",
            return_value={},
        ), mock.patch.object(
            cohort_v2,
            "load_frozen_cohort_selection",
            return_value=(selection, source),
        ):
            result = adapter.build_historical_fury_expert_episodes(
                cohort_path=cohort,
                timeline_manifest_path=timeline,
                data_root=root / "offline_data",
                output_directory=output,
            )
        manifest, episode = _read_episode(result.manifest)
        return result, manifest, episode

    def test_exact_window_join_null_unresolved_and_start_only_labels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_") as raw:
            result, manifest, episode = self._build(Path(raw))

        self.assertEqual(2, result.selected_observation_count)
        self.assertEqual(1, result.candidate_episode_count)
        self.assertEqual(1, result.unresolved_null_encounter_count)
        self.assertEqual(adapter.NULL_ENCOUNTER_STATUS, manifest["unresolved_observations"][0]["status"])
        self.assertEqual(15, manifest["action_contract"]["ontology_size"])
        self.assertFalse(manifest["scientific_boundaries"]["comparison_authorized"])
        self.assertFalse(manifest["scientific_boundaries"]["training_authorized"])
        self.assertTrue(
            episode["window_join"]["metadata_exact_millisecond_window_match"]
        )
        self.assertTrue(episode["window_join"]["timeline_wave_envelope_is_subset"])
        self.assertFalse(episode["window_join"]["coverage"]["partial"])
        self.assertEqual(
            1, manifest["summary"]["complete_wave_coverage_episode_count"]
        )
        self.assertEqual(
            0, manifest["summary"]["partial_wave_coverage_episode_count"]
        )
        metadata_binding = manifest["input_closure"]["selected_raw_metadata"][0]
        self.assertEqual(64, len(metadata_binding["raw_api_manifest"]["file_sha256"]))
        self.assertEqual(64, len(metadata_binding["metadata_object"]["sha256"]))
        self.assertEqual(
            ENCOUNTER,
            metadata_binding["selected_encounter_ids"][0],
        )
        self.assertEqual("Fury", episode["player"]["window_level_spec"])
        self.assertEqual(
            1,
            episode["player"]["timeline_spec_assessment"][
                "unknown_or_conflicting_timeline_wave_count"
            ],
        )
        transitions = episode["wave_observations"][0]["prefix_transitions"]
        labels = [row for row in transitions if row["observed_event"]["policy_decision_label"]]
        self.assertEqual(["warrior.bloodthirst"], [row["observed_event"]["action_key"] for row in labels])
        self.assertTrue(all(row["observed_event"]["phase"] == "START" for row in labels))
        self.assertFalse(
            next(row for row in transitions if row["observed_event"]["phase"] == "GO")[
                "observed_event"
            ]["policy_decision_label"]
        )
        mortal = next(
            row for row in transitions if row["observed_event"]["action_key"] == "unmapped.spell_id.21553"
        )
        self.assertFalse(mortal["observed_event"]["policy_decision_label"])
        self.assertFalse(mortal["observed_event"]["client_next_swing_queue_intent_observed"])
        self.assertEqual(0, transitions[0]["state_before"]["prefix_merged_event_count"])

    def test_partial_wave_envelope_is_recorded_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_window_") as raw:
            _, manifest, episode = self._build(
                Path(raw), first_shift=641, last_shift=-44093, kill_type="partial"
            )

        coverage = episode["window_join"]["coverage"]
        self.assertTrue(coverage["partial"])
        self.assertEqual(641, coverage["left_unobserved_ms"])
        self.assertEqual(44093, coverage["right_unobserved_ms"])
        self.assertFalse(coverage["wave_envelope_exactly_covers_metadata_window"])
        self.assertFalse(
            coverage["wave_windows_contiguously_cover_metadata_window"]
        )
        self.assertEqual(
            "partial",
            episode["window_join"]["authoritative_metadata_encounter_window"][
                "kill_type"
            ],
        )
        self.assertEqual(
            1, manifest["summary"]["partial_wave_coverage_episode_count"]
        )
        self.assertEqual(641, manifest["summary"]["maximum_left_unobserved_ms"])
        self.assertEqual(44093, manifest["summary"]["maximum_right_unobserved_ms"])

    def test_positive_interior_wave_gap_marks_episode_partial(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_interior_gap_") as raw:
            _, manifest, episode = self._build(Path(raw), interior_gap_ms=200)

        coverage = episode["window_join"]["coverage"]
        self.assertTrue(coverage["partial"])
        self.assertEqual(0, coverage["left_unobserved_ms"])
        self.assertEqual(0, coverage["right_unobserved_ms"])
        self.assertEqual(1, coverage["interior_gap_count"])
        self.assertEqual(200, coverage["interior_unobserved_ms"])
        self.assertEqual(
            [
                {
                    "after_wave_id": f"{ENCOUNTER}:wave:1",
                    "before_wave_id": f"{ENCOUNTER}:wave:2",
                    "previous_coverage_end_ms": KILLED_MS - 1200,
                    "next_coverage_start_ms": KILLED_MS - 1000,
                    "unobserved_ms": 200,
                }
            ],
            coverage["interior_gaps"],
        )
        self.assertTrue(coverage["wave_envelope_exactly_covers_metadata_window"])
        self.assertFalse(
            coverage["wave_windows_contiguously_cover_metadata_window"]
        )
        self.assertEqual(
            0, manifest["summary"]["complete_wave_coverage_episode_count"]
        )
        self.assertEqual(
            1, manifest["summary"]["partial_wave_coverage_episode_count"]
        )
        self.assertEqual(
            1, manifest["summary"]["interior_gap_count_across_encounter_groups"]
        )
        self.assertEqual(200, manifest["summary"]["maximum_interior_gap_ms"])

    def test_metadata_window_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_metadata_") as raw:
            root = Path(raw)
            cohort, timeline, output, selection, source = _fixture(
                root, metadata_end_shift=1
            )
            with mock.patch.object(
                cohort_v2,
                "audit_historical_fury_expert_cohort",
                return_value={},
            ), mock.patch.object(
                cohort_v2,
                "load_frozen_cohort_selection",
                return_value=(selection, source),
            ):
                with self.assertRaisesRegex(
                    adapter.HistoricalFuryExpertEpisodeError,
                    "metadata encounter window differs",
                ):
                    adapter.build_historical_fury_expert_episodes(
                        cohort_path=cohort,
                        timeline_manifest_path=timeline,
                        data_root=root / "offline_data",
                        output_directory=output,
                    )

    def test_wave_envelope_outside_metadata_window_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_outside_") as raw:
            root = Path(raw)
            cohort, timeline, output, selection, source = _fixture(
                root, first_shift=-1
            )
            with mock.patch.object(
                cohort_v2,
                "audit_historical_fury_expert_cohort",
                return_value={},
            ), mock.patch.object(
                cohort_v2,
                "load_frozen_cohort_selection",
                return_value=(selection, source),
            ):
                with self.assertRaisesRegex(
                    adapter.HistoricalFuryExpertEpisodeError,
                    "extends outside",
                ):
                    adapter.build_historical_fury_expert_episodes(
                        cohort_path=cohort,
                        timeline_manifest_path=timeline,
                        data_root=root / "offline_data",
                        output_directory=output,
                    )

    def test_duplicate_episode_join_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_duplicate_join_") as raw:
            root = Path(raw)
            cohort, timeline, output, selection, source = _fixture(
                root, duplicate_episode_join_key=True
            )
            with mock.patch.object(
                cohort_v2,
                "audit_historical_fury_expert_cohort",
                return_value={},
            ), mock.patch.object(
                cohort_v2,
                "load_frozen_cohort_selection",
                return_value=(selection, source),
            ):
                with self.assertRaisesRegex(
                    adapter.HistoricalFuryExpertEpisodeError,
                    "episode join key is duplicated",
                ):
                    adapter.build_historical_fury_expert_episodes(
                        cohort_path=cohort,
                        timeline_manifest_path=timeline,
                        data_root=root / "offline_data",
                        output_directory=output,
                    )

    def test_duplicate_ranking_record_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_duplicate_rank_") as raw:
            root = Path(raw)
            cohort, timeline, output, selection, source = _fixture(
                root, duplicate_ranking_id=True
            )
            with mock.patch.object(
                cohort_v2,
                "audit_historical_fury_expert_cohort",
                return_value={},
            ), mock.patch.object(
                cohort_v2,
                "load_frozen_cohort_selection",
                return_value=(selection, source),
            ):
                with self.assertRaisesRegex(
                    adapter.HistoricalFuryExpertEpisodeError,
                    "ranking_record_id is duplicated",
                ):
                    adapter.build_historical_fury_expert_episodes(
                        cohort_path=cohort,
                        timeline_manifest_path=timeline,
                        data_root=root / "offline_data",
                        output_directory=output,
                    )

    def test_explicit_conflict_free_nonfury_spec_is_rejected(self) -> None:
        for timeline_spec in ("Arms", "Protection"):
            with self.subTest(timeline_spec=timeline_spec), tempfile.TemporaryDirectory(
                prefix="fury_episode_spec_"
            ) as raw:
                root = Path(raw)
                cohort, timeline, output, selection, source = _fixture(
                    root, timeline_spec=timeline_spec, conflict=False
                )
                with mock.patch.object(
                    cohort_v2,
                    "audit_historical_fury_expert_cohort",
                    return_value={},
                ), mock.patch.object(
                    cohort_v2,
                    "load_frozen_cohort_selection",
                    return_value=(selection, source),
                ):
                    with self.assertRaisesRegex(
                        adapter.HistoricalFuryExpertEpisodeError,
                        "non-Fury spec",
                    ):
                        adapter.build_historical_fury_expert_episodes(
                            cohort_path=cohort,
                            timeline_manifest_path=timeline,
                            data_root=root / "offline_data",
                            output_directory=output,
                        )

    def test_ratio_swap_with_unchanged_membership_fails_source_replay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fury_episode_cohort_replay_") as raw:
            root = Path(raw)
            _, timeline, output, _, source_path = _fixture(root)
            first = _dps_row(encounter_id=ENCOUNTER, ranking_id="rank-a")
            second = deepcopy(first)
            second.update(
                {
                    "character_guid": "0x00000000000000A2",
                    "character_name": "狂暴战乙",
                    "ranking_record_id": "rank-b",
                    "damage_done": 3000.0,
                    "dps": 1500.0,
                }
            )
            source_manifest = _source_manifest(2)
            expected = cohort_v2.build_cohort_document(
                source_manifest,
                [first, second],
                source_manifest_path="derived/index/manifest.json",
            )
            tampered = deepcopy(expected)
            candidates = tampered["player_candidates"]
            before_membership = [
                (row["character_guid"], [raid["instance_id"] for raid in row["raids"]])
                for row in candidates
            ]
            candidate_ratios = [
                row["median_equal_raid_local_fury_dps_ratio"] for row in candidates
            ]
            raid_ratios = [row["raids"][0]["median_local_fury_dps_ratio"] for row in candidates]
            for index, row in enumerate(candidates):
                row["median_equal_raid_local_fury_dps_ratio"] = candidate_ratios[1 - index]
                row["raids"][0]["median_local_fury_dps_ratio"] = raid_ratios[1 - index]
            self.assertEqual(
                before_membership,
                [
                    (
                        row["character_guid"],
                        [raid["instance_id"] for raid in row["raids"]],
                    )
                    for row in candidates
                ],
            )
            cohort_path = root / "offline_data" / "derived" / "cohort.json"
            cohort_path.write_bytes(_canonical(tampered, newline=True))
            with mock.patch.object(
                cohort_v2,
                "build_cohort_from_index",
                return_value=expected,
            ), self.assertRaisesRegex(
                adapter.HistoricalFuryExpertEpisodeError,
                "differs from deterministic source-index replay",
            ):
                adapter.build_historical_fury_expert_episodes(
                    cohort_path=cohort_path,
                    timeline_manifest_path=timeline,
                    data_root=root / "offline_data",
                    output_directory=output,
                )

    def test_positive_spell_id_never_falls_back_to_alias(self) -> None:
        classification = adapter.classify_fury_action(
            {"event_type": "START", "spell": {"id": 21553, "name": "Bloodthirst"}}
        )
        self.assertEqual("unmapped.spell_id.21553", classification["action_key"])
        self.assertFalse(classification["policy_decision_label"])


if __name__ == "__main__":
    unittest.main()
