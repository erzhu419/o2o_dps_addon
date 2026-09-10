from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_fury_decision_windows import (
    DecisionWindowError,
    MISSING_STATE_FIELDS,
    build_decision_windows,
)


INSTANCE = "043b4d65-9c58-4643-b601-62f1684af04a"
ENCOUNTER = "6dcc1914-1742-4962-8a8c-c89a1b2539b1"
GUID = "0x000000000066ADAE"
PLAYER = "Narcissly"


def _record(
    event_index: int,
    kind: str,
    *,
    fields: dict[str, object] | None = None,
    offset_ms: int | None = None,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "schema": "chronicle_fury_partial_trajectory/v1",
        "record_kind": kind,
        "identity": {
            "canonical_instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "player_guid": GUID,
            "player_name": PLAYER,
        },
        "event": {
            "event_index": event_index,
            "csv_line": event_index + 1,
            "offset_ms": offset_ms if offset_ms is not None else event_index * 10,
            "time": "12:00:00.000",
            "source_event_type": "START" if kind == "candidate_action" else "TEST",
        },
        "fields": fields or {},
        "field_provenance": provenance or {},
    }


def _candidate(
    event_index: int, candidate_id: str, spell_id: int, spell_name: str, offset_ms: int
) -> dict[str, object]:
    return _record(
        event_index,
        "candidate_action",
        offset_ms=offset_ms,
        fields={
            "candidate_action_id": candidate_id,
            "spell_id": spell_id,
            "spell_name": spell_name,
            "target_guid": "0xF130000000000001",
            "target_name": "Target Dummy",
            "state_talent_tree": [21, 30, 0],
            "state_gear_slot_count": 19,
        },
        provenance={
            "fields.state_talent_tree": {
                "kind": "RECONSTRUCTED",
                "event_index": 1,
            },
            "fields.state_gear_slot_count": {
                "kind": "RECONSTRUCTED",
                "event_index": 1,
            },
        },
    )


def _result(
    event_index: int,
    candidate_id: str,
    spell_id: int,
    spell_name: str,
    status: str,
    offset_ms: int,
) -> dict[str, object]:
    return _record(
        event_index,
        "action_result",
        offset_ms=offset_ms,
        fields={
            "spell_id": spell_id,
            "spell_name": spell_name,
            "association_status": "unique",
            "matched_candidate_action_id": candidate_id,
            "result_status": status,
            "start_to_result_ms": 10,
        },
    )


def _ambiguous_result(
    event_index: int,
    candidate_ids: list[str],
    spell_id: int,
    spell_name: str,
    offset_ms: int,
) -> dict[str, object]:
    return _record(
        event_index,
        "action_result",
        offset_ms=offset_ms,
        fields={
            "spell_id": spell_id,
            "spell_name": spell_name,
            "association_status": "ambiguous",
            "candidate_action_ids": candidate_ids,
            "result_status": "succeeded",
        },
    )


def _fixture(root: Path, *, identity_verified: bool = True) -> tuple[Path, Path, Path, Path]:
    trajectory = root / "fixture.jsonl"
    rows = [
        _record(
            2,
            "resource_event",
            fields={
                "resource": "Rage",
                "direction": "Gain",
                "amount_chronicle_units": 100,
            },
        ),
        _record(
            3,
            "reward_event",
            offset_ms=100,
            fields={
                "direction": "outgoing",
                "spell_id": 6603,
                "spell_name": "Auto Attack",
                "target_guid": "0xF130000000000001",
                "damage_amount": 50,
            },
        ),
        _record(
            4,
            "aura_event",
            fields={
                "target_guid": GUID,
                "source_guid": GUID,
                "spell_id": 12970,
                "spell_name": "Flurry",
                "aura_change": "Added",
                "stacks": 3,
            },
        ),
        _candidate(5, "bt-1", 23894, "Bloodthirst", 200),
        _result(6, "bt-1", 23894, "Bloodthirst", "succeeded", 210),
        _record(
            7,
            "reward_event",
            offset_ms=220,
            fields={
                "direction": "outgoing",
                "spell_id": 23894,
                "spell_name": "Bloodthirst",
                "target_guid": "0xF130000000000001",
                "damage_amount": 100,
            },
        ),
        _record(
            8,
            "resource_event",
            fields={
                "resource": "Rage",
                "direction": "Gain",
                "amount_chronicle_units": 20,
            },
        ),
        _candidate(9, "cleave-1", 20569, "Cleave", 300),
        _result(10, "cleave-1", 20569, "Cleave", "succeeded", 310),
        _candidate(11, "bt-fail", 23894, "Bloodthirst", 400),
        _result(12, "bt-fail", 23894, "Bloodthirst", "failed", 410),
        _candidate(13, "visual-1", 99999, "Corrupted Saber Visual (DND)", 500),
        _result(
            14,
            "visual-1",
            99999,
            "Corrupted Saber Visual (DND)",
            "succeeded",
            510,
        ),
        _candidate(15, "bt-ambiguous-1", 23894, "Bloodthirst", 600),
        _candidate(16, "bt-ambiguous-2", 23894, "Bloodthirst", 605),
        _ambiguous_result(
            17,
            ["bt-ambiguous-1", "bt-ambiguous-2"],
            23894,
            "Bloodthirst",
            610,
        ),
    ]
    with trajectory.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    manifest = root / "fixture.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "kind": "chronicle_fury_partial_trajectory_manifest",
                "identity": {"canonical_instance_id": INSTANCE},
                "player": {
                    "guid": GUID,
                    "name": PLAYER,
                    "info_classes": ["WARRIOR"],
                    "leaderboard_observed_specs": ["Fury"],
                },
                "source": {"normalized_file": str(root / "source.jsonl")},
            }
        ),
        encoding="utf-8",
    )

    readiness = root / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "player_encounters": [
                    {
                        "normalized_file": str(root / "source.jsonl"),
                        "encounter_id": ENCOUNTER,
                        "player_guid": GUID,
                        "player_name": PLAYER,
                        "identity": {
                            "verified": identity_verified,
                            "status": "VERIFIED" if identity_verified else "MISSING_INFO",
                        },
                        "full_state_ready": False,
                        "full_state_blockers": ["absolute_rage_anchor"],
                    }
                ]
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
    return trajectory, manifest, readiness, registry


class ChronicleFuryDecisionWindowTests(unittest.TestCase):
    def test_builds_partial_behavior_windows_without_future_or_reward_overclaim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            trajectory, manifest, readiness, registry = _fixture(root)
            output = root / "output"

            result = build_decision_windows(
                trajectory=trajectory,
                trajectory_manifest=manifest,
                readiness_report=readiness,
                encounter=ENCOUNTER,
                registry=registry,
                output_dir=output,
            )

            records = [
                json.loads(line)
                for line in result.trajectory.read_text(encoding="utf-8").splitlines()
                if line
            ]
            output_manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(result.start_candidate_count, 6)
            self.assertEqual(result.observable_behavior_labels, 1)
            self.assertEqual(records[0]["decision"]["lane"], "gcd")
            self.assertTrue(records[0]["decision"]["observable_behavior_label"])
            self.assertEqual(
                records[0]["decision"]["action"]["catalog_status"],
                "MAPPED_ACTIVE",
            )
            self.assertEqual(
                records[0]["decision"]["action"]["policy_action_key"],
                "warrior.bloodthirst",
            )
            self.assertEqual(
                records[0]["window_until_next_start_candidate"]["outgoing_damage_observed"],
                100,
            )
            self.assertEqual(
                records[0]["window_until_next_start_candidate"]["rage_gain_chronicle_units"],
                20,
            )
            self.assertIsNone(
                records[0]["window_until_next_start_candidate"]["causal_reward"]
            )
            self.assertEqual(
                records[1]["decision"]["lane"], "on_swing_unknown_intent"
            )
            self.assertFalse(records[1]["decision"]["observable_behavior_label"])
            self.assertFalse(records[1]["decision"]["queue_intent_label"])
            self.assertFalse(records[2]["decision"]["observable_behavior_label"])
            self.assertEqual(records[3]["decision"]["action"]["catalog_status"], "UNMAPPED")
            self.assertIsNone(records[3]["decision"]["action"]["policy_action_key"])
            self.assertFalse(records[3]["decision"]["observable_behavior_label"])
            self.assertEqual(records[4]["decision"]["association"], "ambiguous")
            self.assertEqual(records[5]["decision"]["association"], "ambiguous")
            self.assertFalse(records[4]["decision"]["observable_behavior_label"])
            self.assertFalse(records[5]["decision"]["observable_behavior_label"])
            self.assertEqual(records[1]["state_before"]["rage_gain_total_chronicle_units"], 120)
            self.assertIsNone(records[1]["state_before"]["rage_loss_total_chronicle_units"])
            self.assertEqual(
                records[1]["state_before"]["recent_uniquely_linked_server_actions"][0][
                    "candidate_action_id"
                ],
                "bt-1",
            )
            for record in records:
                for name in MISSING_STATE_FIELDS:
                    self.assertIsNone(record["state_before"][name])
                    self.assertFalse(record["state_mask"][name])
                start = record["decision"]["start_event_index"]
                for evidence in record["state_provenance"].values():
                    if evidence and evidence.get("event_index") is not None:
                        self.assertLessEqual(evidence["event_index"], start)
            self.assertEqual(
                output_manifest["quality"]["state_future_leakage_count"], 0
            )
            self.assertEqual(output_manifest["output"]["start_candidate_count"], 6)
            self.assertEqual(output_manifest["output"]["mapped_active_candidate_count"], 5)
            self.assertEqual(output_manifest["output"]["unmapped_candidate_count"], 1)
            self.assertEqual(output_manifest["quality"]["queue_intent_labels"], 0)
            self.assertEqual(output_manifest["quality"]["causal_reward_rows"], 0)
            self.assertFalse(output_manifest["quality"]["full_state_bc_ready"])
            self.assertFalse(output_manifest["quality"]["offline_rl_ready"])

    def test_unverified_readiness_identity_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            trajectory, manifest, readiness, registry = _fixture(
                root, identity_verified=False
            )
            output = root / "output"

            with self.assertRaisesRegex(DecisionWindowError, "identity is not verified"):
                build_decision_windows(
                    trajectory=trajectory,
                    trajectory_manifest=manifest,
                    readiness_report=readiness,
                    encounter=ENCOUNTER,
                    registry=registry,
                    output_dir=output,
                )

            self.assertFalse(output.exists())

    def test_mixed_player_trajectory_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            trajectory, manifest, readiness, registry = _fixture(root)
            rows = [
                json.loads(line)
                for line in trajectory.read_text(encoding="utf-8").splitlines()
                if line
            ]
            rows[0]["identity"]["player_guid"] = "0x0000000000BADBAD"
            trajectory.write_text(
                "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
                encoding="utf-8",
            )
            output = root / "output"

            with self.assertRaisesRegex(
                DecisionWindowError, "player GUID does not match manifest"
            ):
                build_decision_windows(
                    trajectory=trajectory,
                    trajectory_manifest=manifest,
                    readiness_report=readiness,
                    encounter=ENCOUNTER,
                    registry=registry,
                    output_dir=output,
                )

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
