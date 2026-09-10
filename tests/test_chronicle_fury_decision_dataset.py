from __future__ import annotations

import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import o2o_dps.chronicle_fury_decision_dataset as dataset_module
import o2o_dps.chronicle_fury_decision_windows as windows_module
from o2o_dps.chronicle_fury_decision_dataset import (
    FuryDecisionDatasetError,
    MISSING_STATE_FIELDS,
    build_fury_decision_dataset,
)


INSTANCE = "043b4d65-9c58-4643-b601-62f1684af04a"
ENCOUNTER = "6dcc1914-1742-4962-8a8c-c89a1b2539b1"
FURY_A = "0x00000000000000A1"
FURY_B = "0x00000000000000B2"
ARMS = "0x00000000000000C3"
UNVERIFIED = "0x00000000000000D4"
TARGET = "0xF130000000000001"


def _row(
    event_index: int,
    event_type: str,
    *,
    offset_ms: int,
    source: str | None = None,
    source_guid: str | None = None,
    target: str | None = "Target Dummy",
    target_guid: str | None = TARGET,
    spell: str | None = None,
    spell_id: int | None = None,
    value: object = None,
    outcome: str | None = None,
) -> dict[str, object]:
    return {
        "instance": INSTANCE,
        "encounter": ENCOUNTER,
        "event_index": event_index,
        "offset_ms": offset_ms,
        "time": f"12:00:{offset_ms / 1000:06.3f}",
        "type": event_type,
        "source": source,
        "source_guid": source_guid,
        "target": target,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "outcome": outcome,
        "synthetic": False,
        "flags": [],
        "activity": None,
        "provenance": {
            "format": "chronicle_all_activity_csv",
            "source_file": f"all-activity-{INSTANCE}.csv",
            "raw_file": f"chronicle_raw/test/all-activity-{INSTANCE}.csv",
            "csv_line": event_index + 100,
            "export_row": event_index,
            "imported_at": "2026-09-01T00:00:00.000000Z",
        },
    }


def _info(event_index: int, guid: str, name: str) -> dict[str, object]:
    return _row(
        event_index,
        "INFO",
        offset_ms=0,
        target=name,
        target_guid=guid,
        spell="Combatant Info",
        value=19,
        outcome="WARRIOR Human talents=21/30/0 gear=19 slots guild=Fixture",
    )


def _start(
    event_index: int,
    guid: str,
    name: str,
    spell_id: int,
    spell: str,
    offset_ms: int,
) -> dict[str, object]:
    return _row(
        event_index,
        "START",
        offset_ms=offset_ms,
        source=name,
        source_guid=guid,
        spell=spell,
        spell_id=spell_id,
        value="—",
        outcome="cast=0ms",
    )


def _go(
    event_index: int,
    guid: str,
    name: str,
    spell_id: int,
    spell: str,
    offset_ms: int,
) -> dict[str, object]:
    return _row(
        event_index,
        "GO",
        offset_ms=offset_ms,
        source=name,
        source_guid=guid,
        spell=spell,
        spell_id=spell_id,
        value=1,
        outcome="hits=1 misses=0",
    )


def _readiness_row(
    normalized: Path,
    *,
    guid: str,
    name: str,
    board_spec: str,
    verified: bool,
) -> dict[str, object]:
    return {
        "normalized_file": str(normalized),
        "encounter_id": ENCOUNTER,
        "player_guid": guid,
        "player_name": name,
        "identity": {
            "verified": verified,
            "status": "VERIFIED" if verified else "AMBIGUOUS_GUID",
            "player_guid": guid,
        },
        "leaderboard_rows": [
            {
                "kind": "leaderboard_row",
                "board_spec": board_spec,
                "rank": 1,
                "source_file": f"Warrior {board_spec}.pdf",
            }
        ],
        "full_state_ready": False,
        "full_state_blockers": ["absolute_rage_anchor", "queue_intent_timing"],
    }


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    normalized_dir = root / "normalized"
    normalized_dir.mkdir()
    normalized = normalized_dir / f"{INSTANCE}__fixture.jsonl"
    rows = [
        _info(1, FURY_A, "Fury A"),
        _info(2, FURY_B, "Fury B"),
        _info(3, ARMS, "Arms Player"),
        _info(4, UNVERIFIED, "Unverified Fury"),
        _row(
            5,
            "RES",
            offset_ms=50,
            target="Fury A",
            target_guid=FURY_A,
            spell="Mighty Rage",
            spell_id=17528,
            value=100,
            outcome="Gain · Rage",
        ),
        _row(
            6,
            "DMG",
            offset_ms=100,
            source="Fury A",
            source_guid=FURY_A,
            spell="Auto Attack",
            spell_id=6603,
            value=50,
            outcome="Hit · Physical",
        ),
        _start(7, FURY_A, "Fury A", 23894, "Bloodthirst", 200),
        _go(8, FURY_A, "Fury A", 23894, "Bloodthirst", 210),
        _row(
            9,
            "DMG",
            offset_ms=220,
            source="Fury A",
            source_guid=FURY_A,
            spell="Bloodthirst",
            spell_id=23894,
            value=100,
            outcome="Hit · Physical",
        ),
        _row(
            10,
            "RES",
            offset_ms=225,
            target="Fury A",
            target_guid=FURY_A,
            spell="Bloodthirst",
            spell_id=23894,
            value=20,
            outcome="Gain · Rage",
        ),
        _start(11, FURY_B, "Fury B", 23894, "Bloodthirst", 230),
        _go(12, FURY_B, "Fury B", 23894, "Bloodthirst", 240),
        _start(13, FURY_A, "Fury A", 20569, "Cleave", 300),
        _go(14, FURY_A, "Fury A", 20569, "Cleave", 310),
        _start(15, FURY_A, "Fury A", 23894, "Bloodthirst", 400),
        _start(16, FURY_A, "Fury A", 23894, "Bloodthirst", 405),
        _go(17, FURY_A, "Fury A", 23894, "Bloodthirst", 410),
        _start(18, FURY_B, "Fury B", 23894, "Bloodthirst", 500),
        _go(19, FURY_B, "Fury B", 23894, "Bloodthirst", 510),
        _start(20, ARMS, "Arms Player", 23894, "Bloodthirst", 600),
        _go(21, ARMS, "Arms Player", 23894, "Bloodthirst", 610),
        _start(22, UNVERIFIED, "Unverified Fury", 23894, "Bloodthirst", 700),
        _go(23, UNVERIFIED, "Unverified Fury", 23894, "Bloodthirst", 710),
    ]
    normalized.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    readiness = root / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "schema": "chronicle_warrior_reconstruction_readiness/v1",
                "player_encounters": [
                    _readiness_row(
                        normalized,
                        guid=FURY_A,
                        name="Fury A",
                        board_spec="Fury",
                        verified=True,
                    ),
                    _readiness_row(
                        normalized,
                        guid=FURY_B,
                        name="Fury B",
                        board_spec="Fury",
                        verified=True,
                    ),
                    _readiness_row(
                        normalized,
                        guid=ARMS,
                        name="Arms Player",
                        board_spec="Arms",
                        verified=True,
                    ),
                    _readiness_row(
                        normalized,
                        guid=UNVERIFIED,
                        name="Unverified Fury",
                        board_spec="Fury",
                        verified=False,
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = root / "warrior_fury.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mechanics": [
                    {
                        "key": "warrior.bloodthirst",
                        "implementation": {
                            "spell_id": 23894,
                            "gcd_seconds": 1.5,
                        },
                    },
                    {
                        "key": "warrior.cleave.queue",
                        "implementation": {
                            "spell_id": 20569,
                            "queue_tag": 1,
                            "replaces_next_main_hand_swing": True,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return normalized, readiness, registry


class ChronicleFuryDecisionDatasetTests(unittest.TestCase):
    def test_registry_lane_contract_accepts_base_gcd_and_explicit_sunder_gcd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = Path(temporary_directory) / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mechanics": [
                            {
                                "key": "warrior.slam",
                                "implementation": {
                                    "wrapper_spell_id": 45961,
                                    "base_gcd_seconds": 1.5,
                                },
                            },
                            {
                                "key": "warrior.sunder_armor",
                                "implementation": {
                                    "spell_id": 11597,
                                    "gcd_seconds": 1.5,
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            for module in (dataset_module, windows_module):
                actions, _ = module._registry_actions(registry)
                self.assertEqual(actions[45961]["lane"], "gcd")
                self.assertEqual(actions[11597]["lane"], "gcd")

        for module in (dataset_module, windows_module):
            actions, _ = module._registry_actions(module.DEFAULT_REGISTRY)
            self.assertEqual(actions[45961]["lane"], "gcd")
            self.assertEqual(actions[11597]["lane"], "gcd")

    def test_streams_one_source_once_for_multiple_verified_fury_players(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            normalized, readiness, registry = _fixture(root)
            output = root / "output"

            original_iterator = dataset_module._iter_normalized_rows
            with patch.object(
                dataset_module,
                "_iter_normalized_rows",
                wraps=original_iterator,
            ) as iterator:
                result = build_fury_decision_dataset(
                    readiness,
                    registry=registry,
                    output_dir=output,
                    workers=1,
                )

            self.assertEqual(iterator.call_count, 1)
            self.assertEqual(iterator.call_args.args[0], normalized.resolve())
            self.assertEqual(result.partition_count, 1)
            self.assertEqual(result.decision_count, 6)
            self.assertTrue(result.partitions[0].name.endswith(".jsonl.gz"))
            with gzip.open(result.partitions[0], "rt", encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))

            self.assertEqual({row["identity"]["player_guid"] for row in records}, {FURY_A, FURY_B})
            self.assertTrue(all(row["identity"]["board_spec"] == "Fury" for row in records))
            self.assertNotIn(ARMS, {row["identity"]["player_guid"] for row in records})
            self.assertNotIn(
                UNVERIFIED, {row["identity"]["player_guid"] for row in records}
            )
            self.assertEqual(manifest["selection"]["readiness_player_encounters"], 4)
            self.assertEqual(manifest["selection"]["eligible_fury_player_encounters"], 2)
            self.assertEqual(manifest["selection"]["non_fury_excluded"], 1)
            self.assertEqual(manifest["selection"]["identity_unverified_excluded"], 1)
            self.assertEqual(manifest["partitions"][0]["source_scan_count"], 1)
            self.assertEqual(manifest["partitions"][0]["source_lines_scanned"], 23)
            self.assertEqual(
                manifest["partitions"][0]["mapped_unknown_lane_decisions"], 0
            )
            self.assertEqual(manifest["quality"]["mapped_unknown_lane_decisions"], 0)
            self.assertNotIn("unknown", manifest["output"]["action_lane_counts"])
            self.assertIsNone(manifest["inputs"]["partial_trajectory_intermediate"])
            self.assertEqual(list(output.glob("*partial*")), [])

    def test_preserves_partial_state_provenance_without_queue_or_reward_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, readiness, registry = _fixture(root)
            result = build_fury_decision_dataset(
                readiness,
                registry=registry,
                output_dir=root / "output",
            )
            with gzip.open(result.partitions[0], "rt", encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]

            first = next(
                row
                for row in records
                if row["identity"]["player_guid"] == FURY_A
                and row["action"]["spell_name"] == "Bloodthirst"
                and row["source"]["start_anchor"]["event_index"] == 7
            )
            self.assertEqual(first["result"]["association"], "unique")
            self.assertEqual(first["result"]["status"], "succeeded")
            self.assertTrue(first["eligibility"]["observable_behavior_label"])
            self.assertTrue(first["eligibility"]["partial_observation_bc"])
            self.assertFalse(first["eligibility"]["full_state_bc"])
            self.assertFalse(first["eligibility"]["offline_rl"])
            self.assertEqual(first["state_before"]["rage_gain_total_chronicle_units"], 100)
            self.assertEqual(first["state_before"]["last_auto_attack_elapsed_ms"], 100)
            self.assertEqual(first["state_before"]["talent_tree_point_totals"], [21, 30, 0])
            self.assertEqual(first["state_before"]["gear_slot_count"], 19)
            self.assertEqual(
                first["window_until_next_start_candidate"]["outgoing_damage_observed"],
                100,
            )
            self.assertEqual(
                first["window_until_next_start_candidate"]["rage_gain_chronicle_units"],
                20,
            )
            self.assertIsNone(
                first["window_until_next_start_candidate"]["causal_reward"]
            )
            self.assertEqual(
                first["window_until_next_start_candidate"]["causal_attribution"],
                "MISSING",
            )

            cleave = next(row for row in records if row["action"]["spell_name"] == "Cleave")
            self.assertEqual(cleave["action"]["lane"], "on_swing_unknown_intent")
            self.assertIsNone(cleave["state_before"]["queue_intent"])
            self.assertFalse(cleave["state_mask"]["queue_intent"])
            self.assertFalse(cleave["eligibility"]["queue_intent_supervision"])
            self.assertFalse(cleave["eligibility"]["observable_behavior_label"])

            ambiguous = [
                row
                for row in records
                if row["identity"]["player_guid"] == FURY_A
                and row["result"]["association"] == "ambiguous"
            ]
            self.assertEqual(len(ambiguous), 2)
            self.assertTrue(
                all(not row["eligibility"]["observable_behavior_label"] for row in ambiguous)
            )
            for record in records:
                self.assertEqual(set(record["state_before"]), set(record["state_mask"]))
                self.assertEqual(set(record["state_before"]), set(record["state_provenance"]))
                for name in MISSING_STATE_FIELDS:
                    self.assertIsNone(record["state_before"][name])
                    self.assertFalse(record["state_mask"][name])
                    self.assertEqual(record["state_provenance"][name]["kind"], "MISSING")
                start = record["source"]["start_anchor"]["event_index"]
                for evidence in record["state_provenance"].values():
                    if evidence["event_index"] is not None:
                        self.assertLessEqual(evidence["event_index"], start)
                    if evidence["csv_line"] is not None:
                        self.assertIsInstance(evidence["csv_line"], int)

            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["quality"]["queue_intent_labels"], 0)
            self.assertEqual(manifest["quality"]["causal_reward_rows"], 0)
            self.assertEqual(manifest["quality"]["state_future_leakage_count"], 0)
            self.assertFalse(manifest["quality"]["offline_rl_ready"])

    def test_multiple_normalized_sources_support_partition_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            normalized, readiness, registry = _fixture(root)
            second = normalized.with_name(f"{INSTANCE}__fixture_second.jsonl")
            second.write_bytes(normalized.read_bytes())
            report = json.loads(readiness.read_text(encoding="utf-8"))
            report["player_encounters"][1]["normalized_file"] = str(second)
            readiness.write_text(json.dumps(report), encoding="utf-8")

            result = build_fury_decision_dataset(
                readiness,
                registry=registry,
                output_dir=root / "output",
                workers=2,
            )

            self.assertEqual(result.partition_count, 2)
            self.assertEqual(result.decision_count, 6)
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["inputs"]["workers"], 2)
            self.assertTrue(
                all(value["source_scan_count"] == 1 for value in manifest["partitions"])
            )

    def test_refuses_when_readiness_has_no_verified_fury_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            normalized, _, registry = _fixture(root)
            readiness = root / "arms_only.json"
            readiness.write_text(
                json.dumps(
                    {
                        "player_encounters": [
                            _readiness_row(
                                normalized,
                                guid=ARMS,
                                name="Arms Player",
                                board_spec="Arms",
                                verified=True,
                            ),
                            _readiness_row(
                                normalized,
                                guid=UNVERIFIED,
                                name="Unverified Fury",
                                board_spec="Fury",
                                verified=False,
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = root / "output"

            with self.assertRaisesRegex(
                FuryDecisionDatasetError, "no identity-verified board_spec=Fury"
            ):
                build_fury_decision_dataset(
                    readiness,
                    registry=registry,
                    output_dir=output,
                )

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
