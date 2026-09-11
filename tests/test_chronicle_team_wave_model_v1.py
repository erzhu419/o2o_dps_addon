from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_team_wave_model_v1 import (
    ChronicleTeamWaveModelError,
    SCHEMA,
    build_chronicle_team_wave_model,
    build_leave_one_player_out_background,
    validate_team_wave_model_manifest,
)
from o2o_dps.chronicle_team_wave_timeline_v1 import (
    IMPLEMENTATION_REVISION as TIMELINE_IMPLEMENTATION_REVISION,
)


TIMELINE_SCHEMA = "chronicle_team_wave_timeline/v1"
INSTANCE = "11111111-1111-4111-8111-111111111111"
ENCOUNTER = "22222222-2222-4222-8222-222222222222"
WAVE_ID = f"{ENCOUNTER}:wave:1"
ALICE = "0x00000000000000A1"
BOB = "0x00000000000000B2"
CAROL = "0x00000000000000C3"
ALICE_PET = "0xF140001111000001"
BOB_PET = "0xF140002222000002"
TARGET_ONE = "0xF13000AAA1000001"
TARGET_TWO = "0xF13000AAA2000002"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wave() -> dict[str, object]:
    return {
        "instance_id": INSTANCE,
        "encounter_id": ENCOUNTER,
        "wave_id": WAVE_ID,
        "wave_ordinal": 1,
    }


def _event(
    event_index: int,
    event_type: str,
    *,
    attribution_kind: str,
    player_guid: str | None,
    source_guid: str,
    target_guid: str,
    amount: int | None = None,
    spell: str = "Attack",
) -> dict[str, object]:
    offset_ms = 100 + event_index * 10
    return {
        "schema": TIMELINE_SCHEMA,
        "record_type": "event",
        "wave": _wave(),
        "order_key": [offset_ms, event_index, event_index + 1000],
        "offset_ms": offset_ms,
        "wave_offset_ms": offset_ms - 100,
        "event_type": event_type,
        "source_event_type": "GO" if event_type == "CAST" else event_type,
        "source": {"guid": source_guid, "name": f"Source-{source_guid[-2:]}"},
        "target": {"guid": target_guid, "name": f"Target-{target_guid[-2:]}"},
        "spell": {"id": event_index, "name": spell},
        "amount": amount,
        "amount_status": (
            "PARSED_NONNEGATIVE_NUMERIC"
            if amount is not None
            else ("NOT_NUMERIC_OR_ABSENT" if event_type in ("DMG", "DEAD") else "NOT_APPLICABLE")
        ),
        "amount_semantics": "OBSERVED_NORMALIZED_VALUE_NOT_CALIBRATED_EFFECTIVE_DAMAGE",
        "outcome": None,
        "flags": ["fixture-retained-by-exact-lane"],
        "synthetic": False,
        "attribution": {
            "kind": attribution_kind,
            "player_guid": player_guid,
            "evidence": "SYNTHETIC_FIXTURE_EXPLICIT",
        },
    }


def _events(*, invalid_owned_owner: bool = False) -> list[dict[str, object]]:
    return [
        _event(
            1,
            "START",
            attribution_kind="DIRECT_PLAYER",
            player_guid=ALICE,
            source_guid=ALICE,
            target_guid=TARGET_ONE,
            spell="Bloodthirst",
        ),
        _event(
            2,
            "DMG",
            attribution_kind="DIRECT_PLAYER",
            player_guid=ALICE,
            source_guid=ALICE,
            target_guid=TARGET_ONE,
            amount=100,
            spell="Bloodthirst",
        ),
        _event(
            3,
            "DMG",
            attribution_kind="OWNED_ENTITY",
            player_guid=None if invalid_owned_owner else ALICE,
            source_guid=ALICE_PET,
            target_guid=TARGET_ONE,
            amount=25,
            spell="Alice Pet",
        ),
        _event(
            4,
            "DMG",
            attribution_kind="OWNED_ENTITY",
            player_guid=BOB,
            source_guid=BOB_PET,
            target_guid=TARGET_ONE,
            amount=50,
            spell="Bob Pet",
        ),
        _event(
            5,
            "DMG",
            attribution_kind="UNATTRIBUTED",
            player_guid=None,
            source_guid="0xF13000EEEE000001",
            target_guid=TARGET_ONE,
            amount=20,
            spell="Unknown Damage",
        ),
        _event(
            6,
            "DMG",
            attribution_kind="UNATTRIBUTED",
            player_guid=None,
            source_guid="0xF13000EEEE000002",
            target_guid=TARGET_ONE,
            amount=None,
            spell="Unparsed Damage",
        ),
        _event(
            7,
            "START",
            attribution_kind="DIRECT_PLAYER",
            player_guid=BOB,
            source_guid=BOB,
            target_guid=TARGET_TWO,
            spell="Mortal Strike",
        ),
        _event(
            8,
            "DEAD",
            attribution_kind="UNATTRIBUTED",
            player_guid=None,
            source_guid="0x0",
            target_guid=TARGET_ONE,
            amount=None,
            spell="Death marker",
        ),
        _event(
            9,
            "DMG",
            attribution_kind="DIRECT_PLAYER",
            player_guid=BOB,
            source_guid=BOB,
            target_guid=TARGET_TWO,
            amount=30,
            spell="Mortal Strike",
        ),
    ]


def _timeline_rows(
    contamination_status: str,
    *,
    invalid_owned_owner: bool = False,
) -> list[dict[str, object]]:
    wave = _wave()
    rows: list[dict[str, object]] = [
        {
            "schema": TIMELINE_SCHEMA,
            "record_type": "wave_header",
            "wave": wave,
            "scenario": {
                "scenario_id": "wave-fixture",
                "capsule_sha256": "a" * 64,
                "main_comparison_only": True,
            },
            "window": {
                "absolute_start_ms": 100,
                "absolute_end_ms": 300,
                "duration_ms": 200,
            },
            "targets": [
                {"target_guid": TARGET_ONE, "target_index": 0, "display_name": "One"},
                {"target_guid": TARGET_TWO, "target_index": 1, "display_name": "Two"},
            ],
            "roster": [
                {
                    "player_guid": ALICE,
                    "player_name": "Alice",
                    "hero_class": "WARRIOR",
                    "guild_name": "南北",
                    "gear": [{"slot_index": 15, "item_id": 1}],
                    "talents": {"summary": [17, 34, 0]},
                    "leaderboard_memberships": [{"specialization": "Fury"}],
                    "leaderboard_specs_recorded_separately": ["Fury"],
                    "fury_arms_rows_merged": False,
                },
                {
                    "player_guid": BOB,
                    "player_name": "Bob",
                    "hero_class": "WARRIOR",
                    # A cross-guild participant must connect both the raid-guild
                    # and player-guild nodes rather than dropping either edge.
                    "guild_name": "GuestGuild",
                    "gear": [],
                    "talents": {"summary": [31, 20, 0]},
                    "leaderboard_memberships": [{"specialization": "Arms"}],
                    "leaderboard_specs_recorded_separately": ["Arms"],
                    "fury_arms_rows_merged": False,
                },
                {
                    "player_guid": CAROL,
                    "player_name": "Carol",
                    "hero_class": None,
                    "guild_name": "南北",
                    "gear": None,
                    "talents": None,
                    "leaderboard_memberships": [],
                    "leaderboard_specs_recorded_separately": [],
                    "fury_arms_rows_merged": False,
                },
            ],
            "instance_leaderboard_memberships": [],
            "specialization_contract": {
                "fury_and_arms_are_recorded_as_distinct_membership_rows": True,
                "fury_arms_rows_merged": False,
                "talent_summary_used_to_infer_board_spec": False,
            },
            "contamination": {
                "status": contamination_status,
                "guild_name": "南北",
                "guild_name_observed": "南北",
                "guild_id": "guild-fixture",
                "guild_match_evidence": "EXACT_NAME",
                "fuzzy_name_match_used": False,
                "rule": {"version": "fixture-v1"},
            },
            "source_hashes": {
                "capsule_file_sha256": "b" * 64,
                "export_queue_file_sha256": "c" * 64,
                "normalized_file_sha256": "d" * 64,
                "combatant_sidecar_manifest_file_sha256": "e" * 64,
                "combatant_sidecar_file_sha256": "f" * 64,
            },
        }
    ]
    rows.extend(_events(invalid_owned_owner=invalid_owned_owner))
    rows.extend(
        [
            {
                "schema": TIMELINE_SCHEMA,
                "record_type": "target_summary",
                "wave": wave,
                "target": {"target_guid": TARGET_ONE, "target_index": 0, "display_name": "One"},
                "observed_incoming_damage": {
                    "value": 195,
                    "event_count": 5,
                    "unparsed_event_count": 1,
                },
                "observed_healing_received": {"value": 0, "event_count": 0},
                "death_clock": {
                    "status": "OBSERVED_DEAD",
                    "observed_death_wave_offset_ms": 80,
                    "censored": False,
                },
            },
            {
                "schema": TIMELINE_SCHEMA,
                "record_type": "target_summary",
                "wave": wave,
                "target": {"target_guid": TARGET_TWO, "target_index": 1, "display_name": "Two"},
                "observed_incoming_damage": {
                    "value": 30,
                    "event_count": 1,
                    "unparsed_event_count": 0,
                },
                "observed_healing_received": {"value": 0, "event_count": 0},
                "death_clock": {
                    "status": "RIGHT_CENSORED_AT_WAVE_END",
                    "observed_death_wave_offset_ms": None,
                    "censored": True,
                },
            },
            {
                "schema": TIMELINE_SCHEMA,
                "record_type": "wave_summary",
                "wave": wave,
                "event_count": 9,
                "event_type_counts": {"DEAD": 1, "DMG": 6, "START": 2},
                "team_damage": {
                    "value": 225,
                    "canonical_dmg_value": 225,
                    "event_count": 5,
                    "event_count_semantics": (
                        "parsed numeric DMG rows only; excludes DEAD rows"
                    ),
                    "dmg_row_count": 6,
                    "numeric_damage_bearing_row_count": 5,
                    "unparsed_event_count": 1,
                    "lethal_dead_damage_event_count": 0,
                    "lethal_dead_damage_value": 0,
                    "duration_seconds": 0.2,
                    "dps": 1125,
                },
                "damage_attribution_conservation": {
                    "direct_player": {
                        "value": 130,
                        "event_count": 2,
                        "numeric_damage_bearing_row_count": 2,
                        "dmg_row_count": 2,
                        "unparsed_dmg_row_count": 0,
                        "lethal_dead_damage_event_count": 0,
                        "lethal_dead_damage_value": 0,
                    },
                    "owned_entity": {
                        "value": 75,
                        "event_count": 2,
                        "numeric_damage_bearing_row_count": 2,
                        "dmg_row_count": 2,
                        "unparsed_dmg_row_count": 0,
                        "lethal_dead_damage_event_count": 0,
                        "lethal_dead_damage_value": 0,
                    },
                    "unattributed": {
                        "value": 20,
                        "event_count": 1,
                        "numeric_damage_bearing_row_count": 1,
                        "dmg_row_count": 2,
                        "unparsed_dmg_row_count": 1,
                        "lethal_dead_damage_event_count": 0,
                        "lethal_dead_damage_value": 0,
                    },
                    "sum_equals_team_damage": True,
                    "owner_policy": "explicit fixture owner only",
                    "name_inference_used": False,
                },
                "per_player_observed_damage": [
                    {"player_guid": ALICE, "value": 125, "event_count": 2},
                    {"player_guid": BOB, "value": 80, "event_count": 2},
                ],
            },
        ]
    )
    return rows


def _write_canonical_gzip(path: Path, rows: list[dict[str, object]]) -> str:
    logical = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
        ) as compressed:
            compressed.write(logical)
    return hashlib.sha256(logical).hexdigest()


def _fixture(
    root: Path,
    contamination_status: str = "NO_KNOWN_RULE_MATCH",
    *,
    invalid_owned_owner: bool = False,
) -> tuple[Path, list[dict[str, object]]]:
    timeline_dir = root / "timeline"
    timeline_dir.mkdir(parents=True)
    rows = _timeline_rows(
        contamination_status,
        invalid_owned_owner=invalid_owned_owner,
    )
    partition = timeline_dir / "fixture.jsonl.gz"
    logical_sha = _write_canonical_gzip(partition, rows)
    core = {
        "schema": TIMELINE_SCHEMA,
        "schema_version": 1,
        "kind": "chronicle_team_wave_timeline_manifest",
        "implementation_revision": TIMELINE_IMPLEMENTATION_REVISION,
        "partitions": [
            {
                "instance_id": INSTANCE,
                "partition": partition.name,
                "logical_content_sha256": logical_sha,
                "compressed_file_sha256": _sha256_file(partition),
                "compressed_size_bytes": partition.stat().st_size,
                "record_count": len(rows),
                "wave_count": 1,
                "event_count": 9,
            }
        ],
    }
    manifest = {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(core),
        },
    }
    manifest_path = timeline_dir / "manifest.json"
    manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
    return manifest_path, rows


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _records(rows: list[dict[str, object]], record_type: str) -> list[dict[str, object]]:
    return [row for row in rows if row.get("record_type") == record_type]


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = set(value)
        for child in value.values():
            result.update(_all_keys(child))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_all_keys(child))
        return result
    return set()


class ChronicleTeamWaveModelV1Tests(unittest.TestCase):
    def test_stale_timeline_revision_is_rejected_before_model_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, _ = _fixture(root)
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["implementation_revision"] = "stale-cutoff-revision"
            core = {
                key: value
                for key, value in manifest.items()
                if key != "content_address"
            }
            manifest["content_address"]["sha256"] = _sha256_json(core)
            manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
            with self.assertRaisesRegex(
                ChronicleTeamWaveModelError, "current.*revision"
            ):
                build_chronicle_team_wave_model(
                    timeline_manifest_path=manifest_path,
                    output_directory=root / "model",
                )

    def test_leave_one_player_out_removes_direct_and_owned_but_retains_unknown(self) -> None:
        projection = build_leave_one_player_out_background(_events(), ALICE)

        self.assertEqual(3, len(projection["excluded_focal_events"]))
        self.assertEqual(6, len(projection["included_events"]))
        self.assertEqual(125, projection["excluded_focal_damage"]["value"])
        self.assertEqual(100, projection["included_damage"]["value"])
        self.assertEqual(
            20,
            projection["unattributed_branch"]["damage"]["value"],
        )
        self.assertEqual(
            2,
            projection["unattributed_branch"]["damage"]["event_count"],
        )
        self.assertEqual(
            1,
            projection["unattributed_branch"]["damage"]["unparsed_event_count"],
        )
        self.assertTrue(projection["filter_contract"]["unattributed_events_retained"])
        self.assertFalse(projection["voting_eligible"])

    def test_numeric_dead_is_retained_but_excluded_from_canonical_prefix_damage(self) -> None:
        events = _events()
        events[7]["amount"] = 777
        events[7]["amount_status"] = "PARSED_NONNEGATIVE_NUMERIC"
        events[7]["damage_accounting"] = {
            "canonical_prefix_lane": "DEAD_MARKER_EXCLUDED_FROM_CANONICAL_DAMAGE",
            "capsule_reconstruction_included": True,
            "relative_to_first_dead": "AT_FIRST_DEAD",
            "derived_from_future_totals": False,
        }
        projection = build_leave_one_player_out_background(events, ALICE)

        dead = next(
            event
            for event in projection["included_events"]
            if event["event_type"] == "DEAD"
        )
        self.assertEqual(777, dead["amount"])
        self.assertEqual(100, projection["included_damage"]["value"])
        self.assertEqual(4, projection["included_damage"]["event_count"])
        self.assertEqual(
            "FULL_NORMALIZED_WAVE_NUMERIC_DMG_ONLY",
            projection["filter_contract"]["damage_value_lane"],
        )
        self.assertTrue(
            projection["filter_contract"][
                "numeric_dead_value_excluded_to_prevent_double_count"
            ]
        )

    def test_build_emits_prefix_only_cross_spec_episodes_and_descriptive_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path, _ = _fixture(root)
            result = build_chronicle_team_wave_model(
                timeline_manifest_path=manifest_path,
                output_directory=root / "model",
            )
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            validate_team_wave_model_manifest(manifest)
            rows = _read_jsonl(result.partitions[0])

            episodes = {
                row["player"]["name"]: row
                for row in _records(rows, "player_wave_episode")
            }
            self.assertEqual(
                "WARRIOR_FURY",
                episodes["Alice"]["player"]["specialization"]["partition_key"],
            )
            self.assertEqual(
                "WARRIOR_ARMS",
                episodes["Bob"]["player"]["specialization"]["partition_key"],
            )
            self.assertNotEqual(
                episodes["Alice"]["player"]["specialization"]["partition_key"],
                episodes["Bob"]["player"]["specialization"]["partition_key"],
            )
            self.assertTrue(
                episodes["Alice"]["eligibility"][
                    "historical_fury_policy_training_eligible"
                ]
            )
            self.assertFalse(
                episodes["Bob"]["eligibility"][
                    "historical_fury_policy_training_eligible"
                ]
            )
            self.assertEqual(
                "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING",
                episodes["Bob"]["eligibility"]["policy_lane_role"],
            )
            self.assertFalse(
                episodes["Bob"]["eligibility"][
                    "historical_arms_policy_voting_eligible"
                ]
            )
            self.assertEqual(
                "OBSERVED_MULTIPLE_EXPLICIT",
                episodes["Bob"]["player"]["guild_identity_status"],
            )
            self.assertEqual(
                2,
                len(episodes["Bob"]["component_membership"]["guild_node_ids"]),
            )
            self.assertEqual(
                "UNKNOWN_EXPLICIT",
                episodes["Carol"]["player"]["specialization"]["status"],
            )
            self.assertEqual("UNKNOWN", episodes["Carol"]["player"]["hero_class"])
            self.assertFalse(episodes["Carol"]["eligibility"]["team_behavior_training_eligible"])

            alice_transitions = episodes["Alice"]["prefix_transitions"]
            self.assertEqual(0, alice_transitions[0]["state_before"]["prefix_event_count"])
            self.assertEqual([], alice_transitions[0]["state_before"]["seen_target_guids"])
            self.assertEqual(
                TARGET_ONE,
                alice_transitions[0]["action_observation"]["target"]["guid"],
            )
            self.assertEqual(
                [TARGET_ONE],
                alice_transitions[1]["state_before"]["seen_target_guids"],
            )
            self.assertEqual(
                0,
                alice_transitions[2]["state_before"][
                    "leave_one_player_out_background_before"
                ]["included_background_damage"],
            )
            bob_transitions = episodes["Bob"]["prefix_transitions"]
            self.assertEqual(
                125,
                bob_transitions[0]["state_before"][
                    "leave_one_player_out_background_before"
                ]["included_background_damage"],
            )
            self.assertEqual(
                145,
                bob_transitions[1]["state_before"][
                    "leave_one_player_out_background_before"
                ]["included_background_damage"],
            )
            target_prefix = {
                row["target_guid"]: row
                for row in bob_transitions[2]["state_before"][
                    "observed_target_prefix"
                ]
            }
            self.assertTrue(target_prefix[TARGET_ONE]["observed_dead"])
            self.assertEqual(195, target_prefix[TARGET_ONE]["prefix_observed_damage"])
            self.assertFalse(target_prefix[TARGET_TWO]["observed_dead"])
            forbidden = {
                "death_clock",
                "target_summaries",
                "wave_summary",
                "team_damage",
                "final_totals",
                "wave_end_ms",
                "wave_duration_ms",
                "remaining_wave_ms",
            }
            for episode in episodes.values():
                for transition in episode["prefix_transitions"]:
                    self.assertTrue(forbidden.isdisjoint(_all_keys(transition["state_before"])))

            exact = _records(rows, "exact_trace_event")
            self.assertEqual(9, len(exact))
            self.assertTrue(all(row["lane"]["status"] == "DESCRIPTIVE_NONVOTING" for row in exact))
            self.assertTrue(all(row["lane"]["voting_eligible"] is False for row in exact))
            self.assertEqual(["fixture-retained-by-exact-lane"], exact[0]["event"]["flags"])
            self.assertEqual(2, len(_records(rows, "descriptive_target_outcome")))
            self.assertEqual(1, len(_records(rows, "descriptive_wave_outcome")))

            unknown_episode = _records(rows, "unattributed_wave_episode")[0]
            self.assertFalse(unknown_episode["eligibility"]["team_behavior_training_eligible"])
            self.assertEqual(3, len(unknown_episode["prefix_transitions"]))
            self.assertFalse(manifest["claim_boundary"]["learned_generator_present"])
            self.assertFalse(manifest["claim_boundary"]["fourth_voting_baseline_present"])
            self.assertTrue(manifest["claim_boundary"]["arms_common_action_diagnostic_only"])
            self.assertFalse(manifest["claim_boundary"]["multiseed_12_cells_ready"])
            self.assertFalse(manifest["claim_boundary"]["comparison_ready"])
            self.assertFalse(manifest["split_graph"]["row_random_split_allowed"])
            self.assertFalse(manifest["split_graph"]["same_player_or_guild_can_cross_folds"])
            instance_components = {
                row["component_id"]
                for row in manifest["split_graph"]["node_to_component"]
                if row["node_id"] == episodes["Alice"]["component_membership"]["instance_node_id"]
            }
            player_components = {
                row["component_id"]
                for row in manifest["split_graph"]["node_to_component"]
                if row["node_id"] in {
                    episodes["Alice"]["component_membership"]["player_node_id"],
                    episodes["Bob"]["component_membership"]["player_node_id"],
                }
            }
            self.assertEqual(instance_components, player_components)

    def test_default_training_contamination_gate_is_exact(self) -> None:
        expected = {
            "POSTFIX_KNOWN_CLEAN": True,
            "NO_KNOWN_RULE_MATCH": True,
            "SUSPECT_36YD_RANGE_BUG": False,
            "UNKNOWN_NONVOTING": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            common = Path(raw)
            for index, (status, eligible) in enumerate(expected.items()):
                with self.subTest(status=status):
                    root = common / str(index)
                    manifest_path, _ = _fixture(root, status)
                    result = build_chronicle_team_wave_model(
                        timeline_manifest_path=manifest_path,
                        output_directory=root / "model",
                    )
                    rows = _read_jsonl(result.partitions[0])
                    alice = next(
                        row
                        for row in _records(rows, "player_wave_episode")
                        if row["player"]["name"] == "Alice"
                    )
                    self.assertEqual(
                        eligible,
                        alice["eligibility"]["team_behavior_training_eligible"],
                    )
                    self.assertEqual(
                        eligible,
                        alice["eligibility"]["historical_fury_policy_training_eligible"],
                    )

    def test_build_is_content_addressed_and_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path, _ = _fixture(root)
            first = build_chronicle_team_wave_model(
                timeline_manifest_path=manifest_path,
                output_directory=root / "first",
            )
            second = build_chronicle_team_wave_model(
                timeline_manifest_path=manifest_path,
                output_directory=root / "second",
            )
            first_manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
            second_manifest = json.loads(second.manifest.read_text(encoding="utf-8"))
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(first.partitions[0].read_bytes(), second.partitions[0].read_bytes())
            partition = first_manifest["partitions"][0]
            self.assertIn(partition["logical_content_sha256"], first.partitions[0].name)
            self.assertEqual(partition["compressed_file_sha256"], _sha256_file(first.partitions[0]))
            addressed = json.loads(first.content_addressed_manifest.read_text(encoding="utf-8"))
            self.assertEqual(first_manifest, addressed)
            core = {key: value for key, value in first_manifest.items() if key != "content_address"}
            self.assertEqual(first_manifest["content_address"]["sha256"], _sha256_json(core))

    def test_missing_owned_entity_owner_fails_closed_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path, _ = _fixture(root, invalid_owned_owner=True)
            output = root / "model"
            with self.assertRaisesRegex(ChronicleTeamWaveModelError, "lacks its explicit owner"):
                build_chronicle_team_wave_model(
                    timeline_manifest_path=manifest_path,
                    output_directory=output,
                )
            self.assertFalse((output / "manifest.json").exists())

    def test_tampered_partition_hash_fails_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path, _ = _fixture(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["partitions"][0]["compressed_file_sha256"] = "0" * 64
            core = {key: value for key, value in manifest.items() if key != "content_address"}
            manifest["content_address"]["sha256"] = _sha256_json(core)
            manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
            output = root / "model"
            with self.assertRaisesRegex(ChronicleTeamWaveModelError, "compressed SHA-256 mismatch"):
                build_chronicle_team_wave_model(
                    timeline_manifest_path=manifest_path,
                    output_directory=output,
                )
            self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
