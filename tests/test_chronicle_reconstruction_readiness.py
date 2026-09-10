from __future__ import annotations

from contextlib import redirect_stdout
from collections import Counter
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_reconstruction_readiness import (
    _parameter_coverage,
    build_readiness_report,
    main,
)


INSTANCE = "043b4d65-9c58-4643-b601-62f1684af04a"
ENCOUNTER = "b2a45869-540e-4a63-8271-dd61903f50e7"
SECOND_ENCOUNTER = "0a1ae051-5f6f-41c3-86a6-fd572e104fba"
PLAYER = "LeaderboardWarrior"
GUID = "0x000000000066ADAE"
SECOND_PLAYER = "SecondLeaderboardWarrior"
SECOND_GUID = "0x0000000000888888"


def _row(index: int, event_type: str, **values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instance": INSTANCE,
        "encounter": ENCOUNTER,
        "event_index": index,
        "offset_ms": index * 10,
        "time": f"12:00:{index / 10:04.1f}",
        "type": event_type,
        "source": PLAYER,
        "source_guid": GUID,
        "target": "Target",
        "target_guid": "0xF130000000000001",
        "spell": None,
        "spell_id": None,
        "value": None,
        "outcome": None,
        "synthetic": False,
        "flags": [],
        "activity": None,
        "provenance": {"csv_line": index + 1},
    }
    row.update(values)
    return row


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    normalized_dir = root / "normalized"
    normalized_dir.mkdir(parents=True)
    normalized = normalized_dir / f"{INSTANCE}__fixture.jsonl"
    rows = [
        _row(
            1,
            "INFO",
            target=PLAYER,
            target_guid=GUID,
            spell="Combatant Info",
            value=19,
            outcome="WARRIOR Human talents=21/30/0 gear=19 slots guild=Example",
        ),
        _row(2, "START", spell="Bloodthirst", spell_id=23894, outcome="cast=0ms"),
        _row(3, "GO", spell="Bloodthirst", spell_id=23894, outcome="hits=1 misses=0"),
        _row(
            4,
            "RES",
            target=PLAYER,
            target_guid=GUID,
            spell="Bloodrage",
            spell_id=2687,
            value=200,
            outcome="Gain · Rage",
        ),
        _row(
            5,
            "RES",
            target=PLAYER,
            target_guid=GUID,
            spell="Bloodthirst",
            spell_id=23894,
            value=300,
            outcome="Loss · Rage",
        ),
        _row(6, "DMG", spell="Auto Attack", spell_id=6603, value=500, outcome="Hit · Physical"),
        _row(
            7,
            "AURA",
            target=PLAYER,
            target_guid=GUID,
            spell="Flurry",
            spell_id=12970,
            outcome="Added (stacks=3)",
            gcd_remaining_ms=900,
            cooldown_remaining_ms=4100,
            queue_intent="Heroic Strike",
            client_keypress=12345,
        ),
        _row(8, "CONS", spell="Juju Power", spell_id=16323, outcome="kind=Active at Pull"),
        _row(9, "CLASS", target=PLAYER, target_guid=GUID, spell="Classification", outcome="Friendly Player"),
        _row(
            10,
            "INFO",
            source="RaidWarrior",
            source_guid="0x0000000000777777",
            target="RaidWarrior",
            target_guid="0x0000000000777777",
            spell="Combatant Info",
            value=19,
            outcome="WARRIOR Orc talents=21/30/0 gear=19 slots guild=Example",
        ),
        _row(
            11,
            "START",
            source="RaidWarrior",
            source_guid="0x0000000000777777",
            spell="Whirlwind",
            spell_id=1680,
        ),
    ]
    with normalized.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    queue_dir = root / "chronicle_raw"
    queue_dir.mkdir()
    queue = {
        "schema_version": 1,
        "kind": "chronicle_export_queue",
        "entries": [
            {
                "instance_id": INSTANCE,
                "leaderboard_rows": [
                    {
                        "character": PLAYER,
                        "board_class": "WARRIOR",
                        "board_spec": "Fury",
                        "rank": 1,
                        "source_file": "Warrior Fury.pdf",
                    },
                    {
                        "character": "MissingWarrior",
                        "board_class": "WARRIOR",
                        "board_spec": "Fury",
                        "rank": 2,
                        "source_file": "Warrior Fury.pdf",
                    },
                    {"character": "Other", "board_class": "MAGE", "rank": 2},
                ],
                "import_receipt": {"normalized": str(normalized)},
            }
        ],
    }
    (queue_dir / "export_queue.json").write_text(
        json.dumps(queue, ensure_ascii=False), encoding="utf-8"
    )

    manifest_dir = root / "derived" / "chronicle_fury_partial_trajectory" / "v1"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "fixture.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "kind": "chronicle_fury_partial_trajectory_manifest",
                "player": {
                    "guid": GUID,
                    "name": PLAYER,
                    "info_classes": ["WARRIOR"],
                    "leaderboard_observed_specs": ["Fury"],
                },
                "selection": {"encounters_emitted": [ENCOUNTER]},
                "source": {"normalized_file": str(normalized)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry_dir = root.parent / "mechanics" / "registry" / "turtle_1_18_1"
    registry_dir.mkdir(parents=True)
    registry = registry_dir / "warrior_fury.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mechanics": [
                    {
                        "key": "warrior.bloodthirst",
                        "implementation": {
                            "spell_id": 23894,
                            "rage_cost": 30,
                            "gcd_seconds": 1.5,
                            "cooldown_seconds": 6,
                        },
                        "calibration": {
                            "parameters": {
                                "rage_cost": {
                                    "observed_value": 30,
                                    "status": "verified_deterministic",
                                    "confidence": "high",
                                },
                                "gcd_seconds": {
                                    "observed_value": 1.5,
                                    "status": "verified_deterministic",
                                    "confidence": "high",
                                },
                                "cooldown_seconds": {
                                    "observed_value": 6,
                                    "status": "verified_deterministic",
                                    "confidence": "high",
                                },
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return normalized, manifest, registry


class ChronicleReconstructionReadinessTests(unittest.TestCase):
    def test_parameter_coverage_uses_the_worst_observed_action_status(self) -> None:
        coverage = _parameter_coverage(
            Counter({(23894, "Bloodthirst"): 2, (99999, "Unknown Action"): 1}),
            {
                23894: {
                    "mechanic": "warrior.bloodthirst",
                    "implementation": {"rage_cost": 30},
                    "calibrated": {
                        "rage_cost": {
                            "observed_value": 30,
                            "status": "verified_deterministic",
                            "confidence": "high",
                        }
                    },
                }
            },
            kind="rage_cost",
        )

        self.assertEqual(coverage["status"], "MISSING")

    def test_current_character_parameter_is_not_applied_to_other_warriors(self) -> None:
        coverage = _parameter_coverage(
            Counter({(1680, "Whirlwind"): 3}),
            {
                1680: {
                    "mechanic": "warrior.whirlwind",
                    "implementation": {"current_character_cooldown_seconds": 8.5},
                    "calibrated": {
                        "current_character_cooldown_seconds": {
                            "observed_value": 8.5,
                            "status": "verified_deterministic",
                            "confidence": "high",
                        }
                    },
                }
            },
            kind="cooldown",
        )

        self.assertEqual(coverage["status"], "INFERRED")
        self.assertTrue(coverage["skills"][0]["current_character_only"])

    def test_field_statuses_are_player_encounter_scoped_and_inputs_stay_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "offline_data"
            normalized, manifest, registry = _fixture(root)
            normalized_before = normalized.read_bytes()
            manifest_before = manifest.read_bytes()

            report = build_readiness_report(root, registry=registry, workers=1)

            self.assertEqual(normalized.read_bytes(), normalized_before)
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertEqual(report["summary"]["leaderboard_player_encounters"], 1)
            self.assertEqual(report["summary"]["leaderboard_targets"], 2)
            self.assertEqual(report["summary"]["name_matched_leaderboard_targets"], 1)
            self.assertEqual(report["summary"]["unmatched_leaderboard_targets"], 1)
            self.assertEqual(
                report["instances"][0]["unmatched_leaderboard_targets"],
                ["MissingWarrior"],
            )
            item = report["player_encounters"][0]
            self.assertEqual(item["player_name"], PLAYER)
            self.assertEqual(item["player_guid"], GUID)
            self.assertTrue(item["identity"]["verified"])
            self.assertEqual(item["identity"]["status"], "VERIFIED")
            self.assertEqual(item["roadmap_stream_grade"], "A")
            self.assertTrue(item["stream_grade_a"])
            self.assertFalse(item["full_state_ready"])
            fields = item["fields"]
            self.assertEqual(fields["rage_gain_delta_chronicle_units"]["status"], "OBSERVED")
            self.assertEqual(fields["rage_loss_delta_chronicle_units"]["status"], "OBSERVED")
            self.assertEqual(fields["rage_unit_conversion_to_wow"]["status"], "INFERRED")
            self.assertEqual(fields["absolute_rage_anchor"]["status"], "MISSING")
            self.assertEqual(fields["skill_rage_cost_coverage"]["status"], "RECONSTRUCTABLE")
            self.assertEqual(fields["rage_spend_outcome_transition_coverage"]["status"], "MISSING")
            self.assertEqual(fields["gcd_duration_parameter_coverage"]["status"], "RECONSTRUCTABLE")
            self.assertEqual(fields["gcd_remaining"]["status"], "MISSING")
            self.assertEqual(fields["cooldown_duration_parameter_coverage"]["status"], "RECONSTRUCTABLE")
            self.assertEqual(fields["cooldown_remaining"]["status"], "MISSING")
            self.assertEqual(fields["swing_event_timestamps"]["status"], "OBSERVED")
            self.assertEqual(fields["mainhand_offhand_swing_remaining"]["status"], "MISSING")
            self.assertEqual(fields["queue_intent_timing"]["status"], "MISSING")
            self.assertEqual(fields["client_keypress"]["status"], "MISSING")
            self.assertEqual(fields["gear_slot_count"]["status"], "RECONSTRUCTABLE")
            self.assertEqual(fields["gear_item_ids"]["status"], "MISSING")
            self.assertNotIn("RaidWarrior", json.dumps(report, ensure_ascii=False))

    def test_event_between_two_leaderboard_warriors_is_attributed_to_both(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "offline_data"
            normalized, _, registry = _fixture(root)
            queue_path = root / "chronicle_raw" / "export_queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["entries"][0]["leaderboard_rows"].append(
                {
                    "character": SECOND_PLAYER,
                    "board_class": "WARRIOR",
                    "board_spec": "Arms",
                    "rank": 3,
                    "source_file": "Warrior Arms.pdf",
                }
            )
            queue_path.write_text(json.dumps(queue), encoding="utf-8")
            extra_rows = [
                _row(
                    12,
                    "INFO",
                    source=SECOND_PLAYER,
                    source_guid=SECOND_GUID,
                    target=SECOND_PLAYER,
                    target_guid=SECOND_GUID,
                    outcome="WARRIOR Orc talents=31/20/0 gear=19 slots guild=Example",
                ),
                _row(
                    13,
                    "AURA",
                    target=SECOND_PLAYER,
                    target_guid=SECOND_GUID,
                    spell="Battle Shout",
                    spell_id=11551,
                    outcome="Added",
                ),
            ]
            with normalized.open("a", encoding="utf-8", newline="\n") as handle:
                for row in extra_rows:
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")

            report = build_readiness_report(root, registry=registry, workers=1)
            by_player = {item["player_name"]: item for item in report["player_encounters"]}

            self.assertEqual(report["summary"]["leaderboard_targets"], 3)
            self.assertEqual(report["summary"]["name_matched_leaderboard_targets"], 2)
            self.assertEqual(report["summary"]["identity_verified_leaderboard_targets"], 2)
            self.assertEqual(
                by_player[SECOND_PLAYER]["fields"]["buffs_duration_stacks"]["player_involved_aura_rows"],
                1,
            )
            self.assertTrue(by_player[SECOND_PLAYER]["identity"]["verified"])

    def test_verified_instance_identity_applies_to_same_guid_in_another_encounter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "offline_data"
            normalized, _, registry = _fixture(root)
            with normalized.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        _row(
                            12,
                            "START",
                            encounter=SECOND_ENCOUNTER,
                            spell="Bloodthirst",
                            spell_id=23894,
                        ),
                        separators=(",", ":"),
                    )
                    + "\n"
                )

            report = build_readiness_report(
                root, registry=registry, manifests=[], workers=1
            )
            by_encounter = {
                item["encounter_id"]: item for item in report["player_encounters"]
            }

            self.assertTrue(by_encounter[SECOND_ENCOUNTER]["identity"]["verified"])
            self.assertEqual(
                by_encounter[SECOND_ENCOUNTER]["identity"]["status"],
                "VERIFIED_INSTANCE_INFO",
            )
            self.assertNotIn(
                "player_identity", by_encounter[SECOND_ENCOUNTER]["full_state_blockers"]
            )

    def test_cli_writes_rerunnable_json_and_markdown_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "offline_data"
            normalized, manifest, registry = _fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = main(
                    [
                        "--data-root",
                        str(root),
                        "--registry",
                        str(registry),
                        "--normalized",
                        str(normalized),
                        "--manifest",
                        str(manifest),
                        "--report-stem",
                        "fixture_readiness",
                    ]
                )
            self.assertEqual(return_code, 0)
            receipt = json.loads(output.getvalue())
            report = json.loads(Path(receipt["json_report"]).read_text(encoding="utf-8"))
            markdown = Path(receipt["markdown_report"]).read_text(encoding="utf-8")
            self.assertEqual(report["schema"], "chronicle_warrior_reconstruction_readiness/v1")
            self.assertTrue(report["evidence_boundary"]["grade_a_not_full_state"])
            self.assertIn("Grade A is stream-family coverage, not full-state readiness", markdown)
            self.assertIn("RECONSTRUCTABLE", markdown)


if __name__ == "__main__":
    unittest.main()
