from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import chronicle_external_api_ingest_v1 as ingest_v1
from o2o_dps import chronicle_external_team_timeline_v2 as timeline_v2
from o2o_dps import historical_named_warrior_episode_adapter_v1 as adapter
from o2o_dps import historical_warrior_reference_cohort_v1 as reference_v1


INSTANCE = "18ec2749-1c46-47b2-bd0b-73524733fde4"
PLAYER = "0x000000000056DEB3"
OTHER = "0x0000000000000002"
TARGET_A = "0xF130000000000001"
TARGET_B = "0xF130000000000002"


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
        "sha256": hashlib.sha256(_canonical({
            key: value for key, value in core.items() if key != "content_address"
        })).hexdigest(),
    }
    return core


def _event(
    phase: str,
    spell_id: int,
    spell_name: str,
    timestamp: int,
    event_index: int,
    target: str = TARGET_A,
    *,
    attribution_kind: str = "DIRECT_FRIENDLY_PLAYER",
    attribution_guid: str = PLAYER,
    source_guid: str = PLAYER,
) -> dict:
    streams = {
        "START": "spell_start",
        "GO": "spell_go",
        "FAIL": "spell_fail",
        "DMG": "damage",
    }
    event = {
        "event_type": phase,
        "anchor": {
            "timestamp_ms": timestamp,
            "offset_ms": timestamp - 1000,
            "event_index": event_index,
            "stream_type": streams[phase],
            "frame_index": 0,
            "frame_message_index": event_index,
            "derived_jsonl_line": event_index,
            "official_message_sha256": "a" * 64,
        },
        "attribution": {
            "attribution_kind": attribution_kind,
            "player_guid": attribution_guid,
            "source_guid": source_guid,
            "status": "EXACT_FRIENDLY_PLAYER_SOURCE",
        },
        "source": {
            "guid": source_guid,
            "lane": "FRIENDLY_PLAYER",
            "unit_type_numeric": 1,
            "affiliation_numeric": 1,
        },
        "target": {
            "guid": target,
            "lane": "HOSTILE_CREATURE",
            "voting_enemy_target": True,
            "hostile_object_preserved_nonvoting": False,
            "hostile_player_preserved_nonvoting": False,
        },
        "spell": {"id": spell_id, "name": spell_name},
    }
    if phase == "START":
        event["action"] = {"phase": "START", "cast_time_ms": 0}
    elif phase == "GO":
        event["action"] = {"phase": "GO", "num_hits": 1, "num_misses": 0}
    elif phase == "FAIL":
        event["action"] = {"phase": "FAIL", "failed_by_server": True}
    else:
        event["damage"] = {"amount": 50, "amount_source": "DMG_ONLY"}
    return event


def _death(timestamp: int = 1003, event_index: int = 13) -> dict:
    event = _event("DMG", 0, "", timestamp, event_index, TARGET_A)
    event["event_type"] = "DEAD"
    event["anchor"]["stream_type"] = "slain"
    event.pop("damage")
    event["death"] = {"marker_only": True, "damage_amount_added": 0}
    return event


def _player_timeline(*, wrong_direct_guid: bool = False) -> list[dict]:
    result = [
        _event("GO", 25286, "Heroic Strike", 1002, 12),
        _event("START", 25286, "Heroic Strike", 1000, 10),
        _event("DMG", 6603, "Auto Attack", 1001, 11),
        _event("START", 21553, "Mortal Strike", 1004, 14, TARGET_B),
        _event("GO", 21553, "Mortal Strike", 1005, 15, TARGET_B),
        _event("START", 20569, "Cleave", 1006, 16, TARGET_B),
        _event("FAIL", 20569, "Cleave", 1007, 17, TARGET_B),
        _event("START", 12292, "Sweeping Strikes", 1008, 18, TARGET_B),
        _event("START", 11585, "Overpower", 1009, 19, TARGET_B),
        _event("START", 11578, "Charge", 1010, 20, TARGET_B),
        _event("START", 20617, "Intercept", 1011, 21, TARGET_B),
        _event("START", 18499, "Berserker Rage", 1012, 22, PLAYER),
        _event("START", 1719, "Recklessness", 1013, 23, PLAYER),
        _event("START", 45961, "Slam", 1014, 24, TARGET_B),
        _event(
            "START",
            28777,
            "Slayer's Crest",
            1015,
            25,
            PLAYER,
            attribution_kind="EXACT_OFFICIAL_OWNER",
        ),
        _event("START", 12566, "Plainsrunning", 1016, 26, PLAYER),
    ]
    if wrong_direct_guid:
        result[1]["attribution"]["player_guid"] = OTHER
    return result


def _wave(*, encounter_id: str | None, wrong_direct_guid: bool = False) -> dict:
    core = {
        "schema": timeline_v2.PARTITION_RECORD_SCHEMA,
        "implementation_revision": timeline_v2.IMPLEMENTATION_REVISION,
        "status": timeline_v2.STATUS,
        "instance_id": INSTANCE,
        "encounter_id": encounter_id,
        "encounter_ordinal": 7,
        "wave_id": f"{encounter_id or 'trash'}:wave:1",
        "wave_ordinal": 1,
        "source_binding_sha256": "b" * 64,
        "reconstruction_binding": {"window": {}},
        "players": [
            {
                "player": {
                    "guid": PLAYER,
                    "name": "托尼牛",
                    "class": "WARRIOR",
                    "race": "Tauren",
                    "level": 60,
                },
                "warrior_spec_evidence": {
                    "exact_player_guid_match": True,
                    "inference_used": False,
                    "status": "OBSERVED",
                    "player_spec": "Arms",
                    "sources": ["instance_metadata", "instance_ranking_records"],
                    "field_conflicts": {},
                },
                "timeline": _player_timeline(
                    wrong_direct_guid=wrong_direct_guid
                ),
            }
        ],
        "death_markers": [_death()],
    }
    return _address(core)


def _reference() -> dict:
    encounter = {
        "encounter_id": "encounter-1",
        "encounter_name": "Boss",
        "killed_at": "2026-09-09T13:47:59.021Z",
        "spec": "Arms",
        "role": "dps",
        "damage_done": 1000.0,
        "duration_secs": 2.0,
        "dps": 500.0,
        "ranking_record_id": "rank-1",
    }
    return {
        "schema": reference_v1.SCHEMA,
        "implementation_revision": reference_v1.IMPLEMENTATION_REVISION,
        "selection_contract": {
            "started_at_not_before_local": ingest_v1.range_bug_boundary_contract()[
                "postfix_known_clean_at_or_after_local"
            ],
            "accepted_contamination_labels": [ingest_v1.POSTFIX_KNOWN_CLEAN],
            "spec_policy": "PRESERVE_SOURCE_SPEC_NEVER_COERCE_TO_FURY",
        },
        "players": [
            {
                "reference_id": f"warrior:{PLAYER.casefold()}",
                "requested_name": "托尼牛",
                "character_guid": PLAYER,
                "selected_specs": ["Arms"],
                "selected_postfix_known_clean_raid_count": 1,
                "raids": [
                    {
                        "instance_id": INSTANCE,
                        "contamination_label": ingest_v1.POSTFIX_KNOWN_CLEAN,
                        "encounter_observation_count": 1,
                        "encounters": [encounter],
                    }
                ],
                "reference_boundaries": {
                    "fury_policy_baseline": False,
                    "direct_same_spec_dps_win_loss_eligible": False,
                    "controllable_action_trace_status": reference_v1.ACTION_TRACE_STATUS,
                    "next_swing_queue_intent_status": reference_v1.QUEUE_INTENT_STATUS,
                    "target_switch_intent_status": reference_v1.TARGET_INTENT_STATUS,
                },
            }
        ],
        "summary": {
            "configured_player_count": 1,
            "selected_player_count": 1,
            "selected_raid_membership_count": 1,
            "selected_encounter_observation_count": 1,
        },
        "scientific_use_boundaries": {
            "historical_identity_performance_and_raid_reference": True,
            "fury_policy_baseline": False,
            "action_trace_available": False,
            "queue_intent_available": False,
            "target_intent_available": False,
            "direct_same_spec_dps_win_loss_eligible": False,
            "training_eligible": False,
            "superiority_claim_eligible": False,
        },
    }


def _fixture(
    root: Path,
    *,
    encounter_id: str | None = "encounter-1",
    wrong_direct_guid: bool = False,
) -> tuple[Path, Path, Path]:
    data = root / "offline_data"
    timeline_dir = data / "derived" / "timeline"
    timeline_dir.mkdir(parents=True)
    wave = _wave(
        encounter_id=encounter_id,
        wrong_direct_guid=wrong_direct_guid,
    )
    logical = _canonical(wave, newline=True)
    partition_path = timeline_dir / "instance.jsonl.gz"
    with partition_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
            handle.write(logical)
    partition = {
        "path": partition_path.name,
        "record_schema": timeline_v2.PARTITION_RECORD_SCHEMA,
        "record_count": 1,
        "compressed_size_bytes": partition_path.stat().st_size,
        "compressed_file_sha256": hashlib.sha256(partition_path.read_bytes()).hexdigest(),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": hashlib.sha256(logical).hexdigest(),
    }
    manifest_core = {
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
                        "contamination": {"label": ingest_v1.POSTFIX_KNOWN_CLEAN}
                    }
                },
                "partition": partition,
            }
        ],
    }
    manifest = _address(manifest_core)
    manifest_payload = _canonical(manifest, newline=True)
    manifest_path = timeline_dir / "manifest.json"
    manifest_path.write_bytes(manifest_payload)
    manifest_sha = manifest["content_address"]["sha256"]
    addressed = timeline_dir / (
        f"chronicle_external_team_timeline_v2.{manifest_sha}.manifest.json"
    )
    addressed.write_bytes(manifest_payload)
    reference_path = data / "derived" / "reference.json"
    reference_path.write_bytes(_canonical(_reference(), newline=True))
    return manifest_path, reference_path, data / "derived" / "episodes"


def _read_episodes(manifest_path: Path) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    partition = manifest["partitions"][0]
    path = manifest_path.parent / partition["path"]
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


class HistoricalNamedWarriorEpisodeAdapterV1Tests(unittest.TestCase):
    def test_ontology_covers_required_arms_and_existing_common_actions(self) -> None:
        expected = {
            21553: "warrior.mortal_strike",
            12292: "warrior.sweeping_strikes",
            11585: "warrior.overpower",
            11578: "warrior.charge",
            20617: "warrior.intercept",
            18499: "warrior.berserker_rage",
            1719: "warrior.recklessness",
            45961: "warrior.slam",
            1680: "warrior.whirlwind",
            25286: "warrior.heroic_strike",
            20569: "warrior.cleave",
        }
        self.assertEqual(
            expected,
            {
                spell_id: adapter.action_spec({"id": spell_id, "name": "ignored"}).action_key
                for spell_id in expected
            },
        )
        self.assertEqual(
            "gcd",
            adapter.action_spec(
                {"id": 1719, "name": "Recklessness"}
            ).lane,
        )

    def test_exact_direct_prefix_and_no_client_queue_inference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="named_episode_") as raw:
            manifest, reference, output = _fixture(Path(raw))
            result = adapter.build_historical_named_warrior_episodes(
                timeline_manifest_path=manifest,
                reference_path=reference,
                output_directory=output,
            )
            self.assertEqual(1, result.episode_count)
            self.assertEqual(11, result.server_observed_start_count)
            self.assertEqual(10, result.controllable_policy_label_count)
            self.assertEqual(1, result.unclassified_start_count)
            episodes = _read_episodes(result.manifest)
            episode = episodes[0]
            transitions = episode["prefix_transitions"]
            indices = [row["observed_event"]["anchor"]["event_index"] for row in transitions]
            self.assertEqual(sorted(indices), indices)
            self.assertNotIn(25, indices)

            heroic_start = transitions[0]
            self.assertEqual(
                "SERVER_OBSERVED_START_CONTROLLABLE_ACTION_PROXY",
                heroic_start["observed_event"]["learning_role"],
            )
            self.assertTrue(heroic_start["observed_event"]["policy_decision_label"])
            self.assertTrue(heroic_start["observed_event"]["server_observed_start_proxy"])
            self.assertFalse(
                heroic_start["observed_event"][
                    "client_next_swing_queue_intent_observed"
                ]
            )
            self.assertNotIn("queue_effect", heroic_start["observed_event"])
            self.assertNotIn(
                "pending_next_swing_queue", heroic_start["state_before"]
            )
            self.assertEqual([], heroic_start["state_before"]["seen_hostile_target_guids"])

            heroic_go = transitions[1]
            self.assertEqual("ACTION_RESULT_GO", heroic_go["observed_event"]["learning_role"])
            self.assertFalse(heroic_go["observed_event"]["policy_decision_label"])
            self.assertFalse(heroic_go["observed_event"]["server_observed_start_proxy"])
            self.assertNotIn("queue_effect", heroic_go["observed_event"])

            mortal = transitions[2]
            self.assertEqual("warrior.mortal_strike", mortal["observed_event"]["action_key"])
            self.assertEqual(TARGET_B, mortal["observed_event"]["exact_target"]["guid"])
            self.assertEqual([TARGET_A], mortal["state_before"]["observed_dead_target_guids"])
            self.assertEqual(TARGET_A, mortal["state_before"]["last_observed_hostile_target_guid"])
            self.assertNotIn(TARGET_B, mortal["state_before"]["seen_hostile_target_guids"])

            cleave_fail = next(
                row for row in transitions
                if row["observed_event"]["action_key"] == "warrior.cleave"
                and row["observed_event"]["phase"] == "FAIL"
            )
            self.assertFalse(cleave_fail["observed_event"]["policy_decision_label"])
            self.assertNotIn("queue_effect", cleave_fail["observed_event"])

            plainsrunning = next(
                row
                for row in transitions
                if row["observed_event"]["action_key"]
                == "unmapped.spell_id.12566"
            )
            self.assertEqual(
                "SERVER_OBSERVED_START_UNCLASSIFIED_OBSERVATION",
                plainsrunning["observed_event"]["learning_role"],
            )
            self.assertFalse(
                plainsrunning["observed_event"]["policy_decision_label"]
            )
            self.assertEqual(
                "UNMAPPED_DIRECT_EVENT_PRESERVED",
                plainsrunning["observed_event"]["ontology_status"],
            )
            self.assertEqual(1, episode["summary"]["excluded_non_direct_attribution_event_count"])
            self.assertTrue(all(row["feature_cutoff_is_strict_prefix"] for row in transitions))
            self.assertTrue(all("transition_sha256" not in row for row in transitions))
            self.assertEqual(
                1,
                episode["summary"]["controllable_label_action_counts"][
                    "warrior.mortal_strike"
                ],
            )
            self.assertEqual(1, episode["summary"]["unclassified_start_count"])
            self.assertEqual(
                {"unmapped.spell_id.12566": 1},
                episode["summary"]["unclassified_start_action_counts"],
            )
            self.assertEqual(
                "MISSING; never inferred from START, GO, or FAIL",
                episode["contracts"]["client_request_and_queue_intent"],
            )
            self.assertFalse(
                episode["scientific_boundaries"][
                    "client_next_swing_queue_intent_observed"
                ]
            )

    def test_null_trash_encounter_id_is_preserved_without_substitution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="named_episode_null_") as raw:
            manifest, reference, output = _fixture(Path(raw), encounter_id=None)
            result = adapter.build_historical_named_warrior_episodes(
                timeline_manifest_path=manifest,
                reference_path=reference,
                output_directory=output,
            )
            episode = _read_episodes(result.manifest)[0]
            self.assertIsNone(episode["encounter_id"])
            self.assertTrue(
                episode["source_wave"][
                    "encounter_id_preserved_without_synthetic_replacement"
                ]
            )

    def test_wrong_direct_guid_fails_instead_of_name_or_container_join(self) -> None:
        with tempfile.TemporaryDirectory(prefix="named_episode_guid_") as raw:
            manifest, reference, output = _fixture(
                Path(raw), wrong_direct_guid=True
            )
            with self.assertRaisesRegex(
                adapter.HistoricalNamedWarriorEpisodeError,
                "does not exact-match named GUID",
            ):
                adapter.build_historical_named_warrior_episodes(
                    timeline_manifest_path=manifest,
                    reference_path=reference,
                    output_directory=output,
                )

    def test_selected_partition_hash_is_verified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="named_episode_hash_") as raw:
            manifest, reference, output = _fixture(Path(raw))
            timeline = json.loads(manifest.read_text(encoding="utf-8"))
            partition = manifest.parent / timeline["instances"][0]["partition"]["path"]
            partition.write_bytes(partition.read_bytes() + b"x")
            with self.assertRaisesRegex(
                adapter.HistoricalNamedWarriorEpisodeError,
                "compressed size mismatch",
            ):
                adapter.build_historical_named_warrior_episodes(
                    timeline_manifest_path=manifest,
                    reference_path=reference,
                    output_directory=output,
                )


if __name__ == "__main__":
    unittest.main()
