from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import o2o_dps.chronicle_team_wave_timeline_v1 as timeline_module
from o2o_dps.chronicle_team_wave_timeline_v1 import (
    ChronicleTeamWaveTimelineError,
    SCHEMA,
    build_chronicle_team_wave_timeline,
    contamination_classification,
)


INSTANCE = "11111111-1111-4111-8111-111111111111"
ENCOUNTER = "22222222-2222-4222-8222-222222222222"
WAVE = f"{ENCOUNTER}:wave:1"
ALICE = "0x00000000000000A1"
BOB = "0x00000000000000B2"
PET = "0xF140001111000001"
NAME_ONLY_SUMMON = "0xF140001112000002"
BOSS = "0xF13000ABC1000001"
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


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)


def _write_gzip_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def _row(
    event_index: int,
    event_type: str,
    offset_ms: int,
    *,
    source: str | None = None,
    source_guid: str | None = None,
    target: str | None = None,
    target_guid: str | None = None,
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
        "time": "00:00:00.000",
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
            "raw_file": "chronicle_raw/not-opened.csv",
            "csv_line": event_index + 100,
            "export_row": event_index,
        },
    }


def _class(event_index: int, guid: str, name: str, outcome: str) -> dict[str, object]:
    return _row(
        event_index,
        "CLASS",
        0,
        target=name,
        target_guid=guid,
        spell="Classification",
        outcome=outcome,
    )


def _sidecar_record(guid: str, name: str, talents: list[int]) -> dict[str, object]:
    return {
        "schema": "chronicle_combatant_info/v1",
        "instance_ref": INSTANCE,
        "encounter_id": ENCOUNTER,
        "slug": "fixture",
        "frame_index": 0,
        "frame_message_index": 0,
        "message_ordinal": 0,
        "first_timestamp_ms": 0,
        "anchor": {
            "offset_ms": 0,
            "event_index": 1,
            "timestamp_ms": 0,
            "is_synthetic": False,
        },
        "player": {
            "guid": guid,
            "name": name,
            "guild_name": "南北",
            "hero_class": "WARRIOR",
            "race": "Human",
            "gender": 2,
        },
        "gear": [{"slot_index": 15, "item_id": 12345}],
        "talents": {"summary": talents, "trees": ["0", "0", "0"]},
    }


def _target(
    guid: str,
    index: int,
    *,
    first_relative: int,
    first_absolute: int,
    damage: int,
    damage_events: int,
    unparsed_damage: int,
    healing: int,
    healing_events: int,
    kill_budget_value: int | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "target_guid": guid,
        "target_index": index,
        "display_name": f"Target {index}",
        "observed_hostile_activity_proxy": {
            "start_ms": first_relative,
            "end_ms": 1000,
            "first_anchor": {
                "offset_ms": first_absolute,
                "event_index": 30,
                "csv_line": 130,
            },
            "last_anchor": {
                "offset_ms": 2000,
                "event_index": 90,
                "csv_line": 190,
            },
        },
        "observed_combat_summaries": {
            "observed_incoming_damage_sum": {
                "status": "OBSERVED",
                "value": damage,
                "event_count": damage_events,
                "unparsed_event_count": unparsed_damage,
            },
            "observed_healing_received_sum": {
                "status": "OBSERVED",
                "value": healing,
                "event_count": healing_events,
            },
            "observed_outgoing_damage_sum": {
                "status": "OBSERVED",
                "value": 0,
                "event_count": 0,
            },
        },
    }
    if kill_budget_value is not None:
        result["max_health_hypothesis_family"] = {
            "observed_kill_budget_proxy": {
                "status": "OBSERVED",
                "value": kill_budget_value,
                "unparsed_damage_event_count": 0,
                "death_anchor": {
                    "offset_ms": 2000,
                    "event_index": 80,
                    "csv_line": 180,
                },
                "interpretation": "observed incoming damage sum through first DEAD",
            }
        }
    return result


def _fixture(
    root: Path,
    *,
    wrong_normalized_hash: bool = False,
    lethal_dead_value: int | None = None,
    capsule_counts_lethal_dead: bool = False,
    capsule_includes_lethal_dead_value: bool = True,
    pre_activity_damage: int | None = None,
    post_death_damage: int | None = None,
) -> dict[str, Path]:
    normalized_dir = root / "normalized"
    normalized = normalized_dir / f"{INSTANCE}__fixture.jsonl"
    normalized_dir.mkdir(parents=True)
    rows = [
        _class(1, ALICE, "Alice", "Friendly Player"),
        _class(2, BOB, "Bob", "Friendly Player"),
        _class(3, PET, "Wolf", "Friendly Creature owner=000000B2"),
        _class(4, NAME_ONLY_SUMMON, "Wolf", "Unknown Creature"),
        _class(5, BOSS, "Boss", "Hostile Creature"),
        _row(
            6,
            "INFO",
            0,
            target="Alice",
            target_guid=ALICE,
            spell="Combatant Info",
            outcome="WARRIOR Human talents=20/31/0 gear=19 slots guild=南北",
        ),
        # Deliberately not chronological: SQLite must emit the strict key order.
        _row(
            70,
            "DMG",
            1300,
            source="Wolf",
            source_guid=NAME_ONLY_SUMMON,
            target="Target 0",
            target_guid=TARGET_ONE,
            spell="Bite",
            value="50",
        ),
        _row(
            30,
            "DMG",
            1100,
            source="Alice",
            source_guid=ALICE,
            target="Target 0",
            target_guid=TARGET_ONE,
            spell="Bloodthirst",
            value="1,200",
        ),
        _row(
            20,
            "START",
            1000,
            source="Alice",
            source_guid=ALICE,
            spell="Bloodthirst",
            spell_id=23894,
        ),
        _row(
            21,
            "START",
            1001,
            source="Boss",
            source_guid=BOSS,
            spell="Hostile Cast",
        ),
        _row(
            22,
            "GO",
            1010,
            source="Alice",
            source_guid=ALICE,
            spell="Bloodthirst",
            spell_id=23894,
        ),
        _row(
            23,
            "FAIL",
            1020,
            source="Alice",
            source_guid=ALICE,
            spell="Execute",
            outcome="Not enough rage",
        ),
        *(
            [
                _row(
                    25,
                    "DMG",
                    1050,
                    source="Alice",
                    source_guid=ALICE,
                    target="Target 0",
                    target_guid=TARGET_ONE,
                    spell="Pre-admission periodic",
                    value=pre_activity_damage,
                )
            ]
            if pre_activity_damage is not None
            else []
        ),
        _row(
            40,
            "DMG",
            1200,
            source="Wolf",
            source_guid=PET,
            target="Target 0",
            target_guid=TARGET_ONE,
            spell="Bite",
            value=300,
        ),
        _row(
            45,
            "DMG",
            1220,
            source="Wolf",
            source_guid=NAME_ONLY_SUMMON,
            target="Target 0",
            target_guid=TARGET_ONE,
            spell="Bite",
            value=20,
        ),
        _row(
            60,
            "CLASS",
            1250,
            target="Wolf",
            target_guid=NAME_ONLY_SUMMON,
            spell="Classification",
            outcome="Friendly Creature owner=000000B2",
        ),
        _row(
            75,
            "HEAL",
            1350,
            source="Alice",
            source_guid=ALICE,
            target="Target 1",
            target_guid=TARGET_TWO,
            spell="Fixture Heal",
            value="25",
        ),
        _row(
            80,
            "DEAD",
            2000,
            source="Alice" if lethal_dead_value is not None else None,
            source_guid=ALICE if lethal_dead_value is not None else None,
            target="Target 0",
            target_guid=TARGET_ONE,
            value=lethal_dead_value if lethal_dead_value is not None else "—",
        ),
        *(
            [
                _row(
                    82,
                    "DMG",
                    2100,
                    source="Alice",
                    source_guid=ALICE,
                    target="Target 0",
                    target_guid=TARGET_ONE,
                    spell="Post-death batched hit",
                    value=post_death_damage,
                )
            ]
            if post_death_damage is not None
            else []
        ),
        _row(
            81,
            "DMG",
            2001,
            source="Alice",
            source_guid=ALICE,
            target="Unselected target",
            target_guid="0xF13000FFFF000001",
            value=99999,
        ),
    ]
    with normalized.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    normalized_hash = hashlib.sha256(normalized.read_bytes()).hexdigest()

    scenario_core: dict[str, object] = {
        "scenario_id": "fixture-main-scenario",
        "source_identity": {
            "instance_id": INSTANCE,
            "encounter_id": ENCOUNTER,
            "wave_id": WAVE,
            "wave_ordinal": 1,
        },
        "horizon": {
            "milliseconds": 2000,
            "status": "RECONSTRUCTED_HOSTILE_ACTIVITY_SPAN",
        },
        "targets": [
            _target(
                TARGET_ONE,
                0,
                first_relative=100,
                first_absolute=1100,
                damage=(
                    1570
                    + (
                        (lethal_dead_value or 0)
                        if capsule_includes_lethal_dead_value
                        else 0
                    )
                    + (post_death_damage or 0)
                ),
                damage_events=4
                + (1 if post_death_damage is not None else 0)
                + (
                    1
                    if lethal_dead_value is not None
                    and capsule_counts_lethal_dead
                    else 0
                ),
                unparsed_damage=0,
                healing=0,
                healing_events=0,
                kill_budget_value=(
                    1570
                    + (
                        (lethal_dead_value or 0)
                        if capsule_includes_lethal_dead_value
                        else 0
                    )
                    if lethal_dead_value is not None
                    else None
                ),
            ),
            _target(
                TARGET_TWO,
                1,
                first_relative=200,
                first_absolute=1200,
                damage=0,
                damage_events=0,
                unparsed_damage=0,
                healing=25,
                healing_events=1,
            ),
        ],
    }
    scenario = {
        **scenario_core,
        "capsule_sha256": _sha256_json(scenario_core),
    }
    capsule_core: dict[str, object] = {
        "schema": "fury_offline_scenario_capsules/v2",
        "schema_version": 2,
        "kind": "fixture",
        "bucket": "main_comparison",
        "scenarios": [scenario],
        "source_instance_provenance": {
            "entries": [
                {
                    "instance_id": INSTANCE,
                    "normalized_byte_sha256": (
                        "0" * 64 if wrong_normalized_hash else normalized_hash
                    ),
                }
            ]
        },
    }
    capsule = {
        **capsule_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": _sha256_json(capsule_core),
        },
    }
    capsule_path = root / "capsule.json.gz"
    _write_gzip_json(capsule_path, capsule)

    queue = {
        "kind": "chronicle_manual_export_queue",
        "entries": [
            {
                "instance_id": INSTANCE,
                "slug": "fixture",
                "status": "imported",
                "guild": {"name": "南北"},
                "started_at": "2026-08-31T15:59:59Z",
                "uploaded_at": "2026-09-03T00:00:00Z",
                "leaderboard_rows": [
                    {
                        "character": "Alice",
                        "board_class": "WARRIOR",
                        "board_spec": "Fury",
                        "observed_spec": "Fury",
                        "rank": 1,
                        "dps": 1234,
                        "raid_date": "2026-08-31",
                        "source_file": "Fury.pdf",
                    },
                    {
                        "character": "Bob",
                        "board_class": "WARRIOR",
                        "board_spec": "Arms",
                        "observed_spec": "Arms",
                        "rank": 2,
                        "dps": 1200,
                        "raid_date": "2026-08-31",
                        "source_file": "Arms.pdf",
                    },
                ],
                "import_receipt": {"normalized": str(normalized)},
            }
        ],
    }
    queue_path = root / "export_queue.json"
    _write_json(queue_path, queue)

    sidecar = root / f"{INSTANCE}.combatant_info.jsonl.gz"
    _write_gzip_jsonl(
        sidecar,
        [
            _sidecar_record(ALICE, "Alice", [20, 31, 0]),
            _sidecar_record(BOB, "Bob", [36, 15, 0]),
        ],
    )
    sidecar_manifest = {
        "schema": "chronicle_combatant_info_sidecar/v1",
        "instances": [
            {
                "instance_ref": INSTANCE,
                "availability": "available",
                "sidecar_path": str(sidecar),
            }
        ],
    }
    sidecar_manifest_path = root / "sidecar_manifest.json"
    _write_json(sidecar_manifest_path, sidecar_manifest)
    return {
        "capsule": capsule_path,
        "queue": queue_path,
        "sidecar_manifest": sidecar_manifest_path,
        "normalized_dir": normalized_dir,
    }


def _read_partition(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class ChronicleTeamWaveTimelineV1Tests(unittest.TestCase):
    def test_builds_ordered_conserved_timeline_with_explicit_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root)
            output = root / "output"
            result = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=output,
                workers=4,
            )
            self.assertEqual(SCHEMA, result.as_dict()["schema"])
            self.assertEqual(1, result.instance_count)
            self.assertEqual(1, result.wave_count)
            self.assertTrue(result.manifest.is_file())
            self.assertTrue(result.content_addressed_manifest.is_file())
            self.assertEqual(1, len(result.partitions))

            records = _read_partition(result.partitions[0])
            self.assertEqual("wave_header", records[0]["record_type"])
            events = [row for row in records if row["record_type"] == "event"]
            self.assertEqual(
                [
                    "START",
                    "CAST",
                    "FAIL",
                    "DMG",
                    "DMG",
                    "DMG",
                    "DMG",
                    "HEAL",
                    "DEAD",
                ],
                [row["event_type"] for row in events],
            )
            keys = [tuple(row["order_key"]) for row in events]
            self.assertEqual(sorted(keys), keys)
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(1200, events[3]["amount"])
            self.assertEqual("GO", events[1]["source_event_type"])

            damage = [row for row in events if row["source_event_type"] in ("DMG", "DEAD")]
            self.assertEqual(
                [
                    "DIRECT_PLAYER",
                    "OWNED_ENTITY",
                    "UNATTRIBUTED",
                    "OWNED_ENTITY",
                    "UNATTRIBUTED",
                ],
                [row["attribution"]["kind"] for row in damage],
            )
            self.assertFalse(
                any(row["attribution"]["name_inference_used"] for row in damage)
            )
            owned = [
                row for row in damage if row["attribution"]["kind"] == "OWNED_ENTITY"
            ]
            self.assertTrue(all(row["attribution"]["player_guid"] == BOB for row in owned))
            transitioned = [
                row for row in damage if row["source"]["guid"] == NAME_ONLY_SUMMON
            ]
            self.assertEqual(
                ["UNATTRIBUTED", "OWNED_ENTITY"],
                [row["attribution"]["kind"] for row in transitioned],
            )
            self.assertEqual(
                [0, 4, 104],
                transitioned[0]["attribution"]["classification_as_of_order_key"],
            )
            self.assertEqual(
                [1250, 60, 160],
                transitioned[1]["attribution"]["classification_as_of_order_key"],
            )
            self.assertTrue(
                all(row["attribution"]["temporal_cutoff_enforced"] for row in events)
            )

            header = records[0]
            self.assertEqual("SUSPECT_36YD_RANGE_BUG", header["contamination"]["status"])
            self.assertEqual(
                "EXACT_NAME", header["contamination"]["guild_match_evidence"]
            )
            self.assertFalse(header["contamination"]["fuzzy_name_match_used"])
            self.assertEqual(
                1, header["classification_contract"]["classification_transition_count"]
            )
            roster = {row["player_name"]: row for row in header["roster"]}
            self.assertEqual(["Fury"], roster["Alice"]["leaderboard_specs_recorded_separately"])
            self.assertEqual(["Arms"], roster["Bob"]["leaderboard_specs_recorded_separately"])
            self.assertFalse(roster["Alice"]["fury_arms_rows_merged"])

            summaries = {
                row["target_guid"]: row
                for row in records
                if row["record_type"] == "target_summary"
            }
            self.assertEqual("OBSERVED_DEAD", summaries[TARGET_ONE]["death_clock"]["status"])
            self.assertFalse(summaries[TARGET_ONE]["death_clock"]["censored"])
            self.assertEqual(
                "RIGHT_CENSORED_AT_WAVE_END",
                summaries[TARGET_TWO]["death_clock"]["status"],
            )
            self.assertTrue(summaries[TARGET_TWO]["death_clock"]["censored"])
            self.assertEqual(4, summaries[TARGET_ONE]["observed_damage"]["event_count"])
            self.assertEqual(4, summaries[TARGET_ONE]["observed_damage"]["dmg_row_count"])
            self.assertEqual(
                4,
                summaries[TARGET_ONE]["observed_damage"][
                    "numeric_damage_bearing_row_count"
                ],
            )
            self.assertEqual(
                0,
                summaries[TARGET_ONE]["observed_damage"][
                    "lethal_dead_damage_event_count"
                ],
            )

            wave_summary = next(
                row for row in records if row["record_type"] == "wave_summary"
            )
            conservation = wave_summary["damage_attribution_conservation"]
            self.assertEqual(1200, conservation["direct_player"]["value"])
            self.assertEqual(350, conservation["owned_entity"]["value"])
            self.assertEqual(20, conservation["unattributed"]["value"])
            self.assertEqual(1570, wave_summary["team_damage"]["value"])
            self.assertTrue(conservation["sum_equals_team_damage"])
            self.assertEqual(
                1, wave_summary["classification_provenance"]["transition_count"]
            )
            self.assertEqual(
                0,
                wave_summary["classification_provenance"][
                    "future_classification_backfill_count"
                ],
            )

            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            core = {key: value for key, value in manifest.items() if key != "content_address"}
            self.assertEqual(_sha256_json(core), manifest["content_address"]["sha256"])
            self.assertEqual(0, manifest["summary"]["raw_file_open_count"])
            self.assertFalse(manifest["output_contract"]["raw_30gb_upload_required"])
            self.assertEqual(1, manifest["partitions"][0]["normalized_input"]["scan_count"])
            self.assertEqual(
                f"$EXTERNAL/{fixture['capsule'].name}",
                manifest["inputs"]["capsule"]["path"],
            )
            self.assertNotIn(str(root), result.manifest.read_text(encoding="utf-8"))

    def test_numeric_dead_is_lethal_damage_but_not_capsule_dmg_event_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root, lethal_dead_value=745)
            result = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=root / "output",
            )
            records = _read_partition(result.partitions[0])
            lethal = next(
                row
                for row in records
                if row.get("record_type") == "event"
                and row.get("source_event_type") == "DEAD"
            )
            self.assertEqual(745, lethal["amount"])

            target = next(
                row
                for row in records
                if row.get("record_type") == "target_summary"
                and row.get("target_guid") == TARGET_ONE
            )
            damage = target["observed_damage"]
            self.assertEqual(2315, damage["value"])
            self.assertEqual(4, damage["event_count"])
            self.assertEqual(4, damage["dmg_row_count"])
            self.assertEqual(5, damage["numeric_damage_bearing_row_count"])
            self.assertEqual(1, damage["lethal_dead_damage_event_count"])
            self.assertEqual(745, damage["lethal_dead_damage_value"])
            self.assertEqual(4, damage["capsule_declared_event_count"])
            self.assertEqual(
                "TARGET_ACTIVITY_NUMERIC_DMG_ONLY",
                damage["capsule_event_count_match_semantics"],
            )
            self.assertEqual("OBSERVED_DEAD", target["death_clock"]["status"])
            self.assertEqual(2000, target["death_clock"]["observed"]["offset_ms"])

            wave = next(
                row for row in records if row.get("record_type") == "wave_summary"
            )
            team = wave["team_damage"]
            self.assertEqual(2315, team["value"])
            self.assertEqual(4, team["event_count"])
            self.assertEqual(4, team["dmg_row_count"])
            self.assertEqual(5, team["numeric_damage_bearing_row_count"])
            self.assertEqual(1, team["lethal_dead_damage_event_count"])
            self.assertEqual(745, team["lethal_dead_damage_value"])
            direct = wave["damage_attribution_conservation"]["direct_player"]
            self.assertEqual(1945, direct["value"])
            self.assertEqual(2, direct["numeric_damage_bearing_row_count"])
            self.assertEqual(1, direct["dmg_row_count"])
            self.assertEqual(1, direct["lethal_dead_damage_event_count"])
            self.assertEqual(745, direct["lethal_dead_damage_value"])
            self.assertTrue(
                wave["damage_attribution_conservation"]["sum_equals_team_damage"]
            )
            alice = next(
                row
                for row in wave["per_player_observed_damage"]
                if row["player_guid"] == ALICE
            )
            self.assertEqual(1945, alice["value"])
            self.assertEqual(1, alice["lethal_dead_damage_event_count"])
            self.assertEqual(745, alice["lethal_dead_damage_value"])

            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            partition = manifest["partitions"][0]
            self.assertEqual(4, partition["team_damage_event_count"])
            self.assertEqual(5, partition["team_numeric_damage_bearing_row_count"])
            self.assertEqual(1, partition["team_lethal_dead_damage_event_count"])
            self.assertEqual(745, partition["team_lethal_dead_damage_value"])

    def test_legacy_capsule_count_including_numeric_dead_is_labeled_not_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(
                root,
                lethal_dead_value=51,
                capsule_counts_lethal_dead=True,
            )
            result = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=root / "output",
            )
            records = _read_partition(result.partitions[0])
            target = next(
                row
                for row in records
                if row.get("record_type") == "target_summary"
                and row.get("target_guid") == TARGET_ONE
            )
            damage = target["observed_damage"]
            self.assertEqual(5, damage["capsule_declared_event_count"])
            self.assertEqual(4, damage["event_count"])
            self.assertEqual(5, damage["numeric_damage_bearing_row_count"])
            self.assertEqual(
                "TARGET_ACTIVITY_NUMERIC_DMG_PLUS_NUMERIC_DEAD",
                damage["capsule_event_count_match_semantics"],
            )
            self.assertEqual(1621, damage["value"])
            self.assertEqual(51, damage["lethal_dead_damage_value"])

    def test_activity_admission_and_post_death_damage_are_separate_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(
                root,
                lethal_dead_value=3,
                pre_activity_damage=138,
                post_death_damage=1162,
            )
            result = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=root / "output",
            )
            records = _read_partition(result.partitions[0])
            pre = next(
                row
                for row in records
                if row.get("record_type") == "event"
                and row.get("spell", {}).get("name") == "Pre-admission periodic"
            )
            post = next(
                row
                for row in records
                if row.get("record_type") == "event"
                and row.get("spell", {}).get("name") == "Post-death batched hit"
            )
            self.assertFalse(
                pre["damage_accounting"]["capsule_reconstruction_included"]
            )
            self.assertEqual(
                "BEFORE_TARGET_HOSTILITY_ADMISSION",
                pre["damage_accounting"]["capsule_reconstruction_exclusion_reason"],
            )
            self.assertEqual(
                "AFTER_FIRST_DEAD",
                post["damage_accounting"]["relative_to_first_dead"],
            )

            target = next(
                row
                for row in records
                if row.get("record_type") == "target_summary"
                and row.get("target_guid") == TARGET_ONE
            )
            damage = target["observed_damage"]
            self.assertEqual(2735, damage["value"])
            self.assertEqual(2870, damage["canonical_dmg_value"])
            self.assertEqual(3, damage["numeric_dead_value"])
            self.assertEqual(2873, damage["combined_value"])
            self.assertEqual(2732, damage["capsule_activity_dmg_value"])
            self.assertEqual(2735, damage["capsule_activity_combined_value"])
            self.assertEqual(
                "TARGET_ACTIVITY_NUMERIC_DMG_PLUS_NUMERIC_DEAD",
                damage["capsule_value_match_semantics"],
            )
            self.assertEqual(6, damage["event_count"])
            self.assertEqual(5, damage["capsule_declared_event_count"])
            self.assertEqual(
                138,
                target["pre_activity_damage_diagnostic"]["canonical_dmg_value"],
            )
            self.assertEqual(
                1162,
                target["post_first_dead_damage_diagnostic"]["canonical_dmg_value"],
            )
            self.assertEqual(1570, target["kill_budget_damage"]["canonical_dmg_value"])
            self.assertEqual(1573, target["kill_budget_damage"]["combined_value"])
            self.assertEqual(
                "TARGET_ACTIVITY_THROUGH_FIRST_DEAD_NUMERIC_DMG_PLUS_NUMERIC_DEAD",
                target["kill_budget_damage"]["capsule_value_match_semantics"],
            )
            wave = next(
                row for row in records if row.get("record_type") == "wave_summary"
            )
            self.assertEqual(2870, wave["team_damage"]["canonical_dmg_value"])
            self.assertEqual(2873, wave["team_damage"]["value"])

    def test_capsule_value_can_select_dmg_only_without_dropping_dead_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(
                root,
                lethal_dead_value=51,
                capsule_includes_lethal_dead_value=False,
            )
            result = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=root / "output",
            )
            records = _read_partition(result.partitions[0])
            self.assertEqual(
                1,
                sum(
                    row.get("record_type") == "event"
                    and row.get("source_event_type") == "DEAD"
                    for row in records
                ),
            )
            target = next(
                row
                for row in records
                if row.get("record_type") == "target_summary"
                and row.get("target_guid") == TARGET_ONE
            )
            damage = target["observed_damage"]
            self.assertEqual(1570, damage["value"])
            self.assertEqual(1570, damage["canonical_dmg_value"])
            self.assertEqual(51, damage["numeric_dead_value"])
            self.assertEqual(1621, damage["combined_value"])
            self.assertEqual(
                "TARGET_ACTIVITY_NUMERIC_DMG_ONLY",
                damage["capsule_value_match_semantics"],
            )
            self.assertEqual(
                "TARGET_ACTIVITY_THROUGH_FIRST_DEAD_NUMERIC_DMG_ONLY",
                target["kill_budget_damage"]["capsule_value_match_semantics"],
            )

    def test_content_addresses_are_deterministic_across_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root)
            first = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=root / "one",
            )
            second = build_chronicle_team_wave_timeline(
                capsule_path=fixture["capsule"],
                export_queue_path=fixture["queue"],
                sidecar_manifest_path=fixture["sidecar_manifest"],
                normalized_directory=fixture["normalized_dir"],
                output_directory=root / "two",
            )
            first_manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
            second_manifest = json.loads(second.manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                first_manifest["content_address"]["sha256"],
                second_manifest["content_address"]["sha256"],
            )
            self.assertEqual(
                first_manifest["partitions"][0]["logical_content_sha256"],
                second_manifest["partitions"][0]["logical_content_sha256"],
            )
            self.assertEqual(
                first.partitions[0].read_bytes(), second.partitions[0].read_bytes()
            )

    def test_declared_normalized_hash_mismatch_fails_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root, wrong_normalized_hash=True)
            output = root / "output"
            with self.assertRaisesRegex(
                ChronicleTeamWaveTimelineError, "normalized SHA-256 mismatch"
            ):
                build_chronicle_team_wave_timeline(
                    capsule_path=fixture["capsule"],
                    export_queue_path=fixture["queue"],
                    sidecar_manifest_path=fixture["sidecar_manifest"],
                    normalized_directory=fixture["normalized_dir"],
                    output_directory=output,
                )
            self.assertFalse((output / "manifest.json").exists())
            self.assertEqual([], list(output.glob("*.jsonl.gz")))
            self.assertEqual([], list(output.glob("*.tmp")))

    def test_contamination_cutoff_uses_started_at_in_asia_shanghai(self) -> None:
        cases = [
            (
                {"guild": {"name": "南北"}, "started_at": "2026-08-31T15:59:59Z"},
                "SUSPECT_36YD_RANGE_BUG",
                False,
                "EXACT_NAME",
            ),
            (
                {"guild": {"name": "南北"}, "started_at": "2026-09-02T16:00:00Z"},
                "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
                False,
                "EXACT_NAME",
            ),
            (
                {"guild": {"name": "南北"}, "started_at": "2026-09-03T04:00:00Z"},
                "POSTFIX_KNOWN_CLEAN",
                True,
                "EXACT_NAME",
            ),
            (
                {
                    "guild": {
                        "name": "�ϱ�",
                        "id": "65a8fe4c-8023-4ed2-bcef-d14d0feacb6b",
                    },
                    "started_at": "2026-08-20T12:33:29.189Z",
                },
                "SUSPECT_36YD_RANGE_BUG",
                False,
                "KNOWN_GUILD_ID",
            ),
            (
                {"guild": {"name": "Elsewhere"}, "started_at": "2026-08-01T00:00:00Z"},
                "NO_KNOWN_RULE_MATCH",
                True,
                None,
            ),
            (
                {"guild": {"name": "南北"}, "uploaded_at": "2026-09-02T00:00:00Z"},
                "UNKNOWN_NONVOTING",
                False,
                "EXACT_NAME",
            ),
        ]
        for entry, status, eligible, match_evidence in cases:
            with self.subTest(status=status):
                result = contamination_classification(entry)
                self.assertEqual(status, result["status"])
                self.assertEqual(eligible, result["historical_policy_voting_eligible"])
                self.assertEqual(match_evidence, result["guild_match_evidence"])
                self.assertFalse(result["rule"]["uploaded_at_used"])
                self.assertFalse(result["fuzzy_name_match_used"])

    def test_disagreeing_target_wave_anchors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root)
            with gzip.open(fixture["capsule"], "rt", encoding="utf-8") as handle:
                capsule = json.load(handle)
            scenario = capsule["scenarios"][0]
            scenario["targets"][1]["observed_hostile_activity_proxy"]["first_anchor"][
                "offset_ms"
            ] += 1
            scenario_core = {
                key: value for key, value in scenario.items() if key != "capsule_sha256"
            }
            scenario["capsule_sha256"] = _sha256_json(scenario_core)
            capsule_core = {
                key: value for key, value in capsule.items() if key != "content_address"
            }
            capsule["content_address"]["sha256"] = _sha256_json(capsule_core)
            _write_gzip_json(fixture["capsule"], capsule)
            with self.assertRaisesRegex(
                ChronicleTeamWaveTimelineError,
                "do not agree on one absolute wave start",
            ):
                build_chronicle_team_wave_timeline(
                    capsule_path=fixture["capsule"],
                    export_queue_path=fixture["queue"],
                    sidecar_manifest_path=fixture["sidecar_manifest"],
                    normalized_directory=fixture["normalized_dir"],
                    output_directory=root / "output",
                )

    def test_only_conflicting_class_at_identical_order_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root)
            normalized = next(fixture["normalized_dir"].glob("*.jsonl"))
            conflict = _row(
                4,
                "CLASS",
                0,
                target="Wolf",
                target_guid=NAME_ONLY_SUMMON,
                spell="Classification",
                outcome="Hostile Creature",
            )
            with normalized.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(conflict, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            with gzip.open(fixture["capsule"], "rt", encoding="utf-8") as handle:
                capsule = json.load(handle)
            capsule["source_instance_provenance"]["entries"][0][
                "normalized_byte_sha256"
            ] = hashlib.sha256(normalized.read_bytes()).hexdigest()
            capsule_core = {
                key: value for key, value in capsule.items() if key != "content_address"
            }
            capsule["content_address"]["sha256"] = _sha256_json(capsule_core)
            _write_gzip_json(fixture["capsule"], capsule)
            with self.assertRaisesRegex(
                ChronicleTeamWaveTimelineError,
                "conflicting CLASS evidence at identical order key",
            ):
                build_chronicle_team_wave_timeline(
                    capsule_path=fixture["capsule"],
                    export_queue_path=fixture["queue"],
                    sidecar_manifest_path=fixture["sidecar_manifest"],
                    normalized_directory=fixture["normalized_dir"],
                    output_directory=root / "output",
                )

    def test_parallel_failure_drains_and_removes_late_success_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _fixture(root)
            output = root / "output"
            slow_started = Event()

            failing_b = SimpleNamespace(instance_id="instance-b")
            slow = SimpleNamespace(instance_id="instance-slow")
            failing_a = SimpleNamespace(instance_id="instance-a")

            def fake_partition(job: object, output_directory: Path):
                if job is failing_b:
                    slow_started.wait(timeout=2)
                    raise ChronicleTeamWaveTimelineError("fixture worker failure B")
                if job is failing_a:
                    slow_started.wait(timeout=2)
                    raise ChronicleTeamWaveTimelineError("fixture worker failure A")
                slow_started.set()
                time.sleep(0.05)
                temporary_path = output_directory / ".late-success.jsonl.gz.tmp"
                temporary_path.write_bytes(b"late")
                return timeline_module.PartitionBuild(
                    temporary_path=temporary_path,
                    final_path=output_directory / "late.jsonl.gz",
                    manifest_entry={},
                )

            with patch.object(
                timeline_module,
                "_jobs",
                return_value=(failing_b, slow, failing_a),
            ), patch.object(timeline_module, "_write_partition", side_effect=fake_partition):
                with self.assertRaises(ChronicleTeamWaveTimelineError) as raised:
                    build_chronicle_team_wave_timeline(
                        capsule_path=fixture["capsule"],
                        export_queue_path=fixture["queue"],
                        sidecar_manifest_path=fixture["sidecar_manifest"],
                        normalized_directory=fixture["normalized_dir"],
                        output_directory=output,
                        workers=2,
                    )
            message = str(raised.exception)
            first = message.index(
                "index=0 instance_id=instance-b ChronicleTeamWaveTimelineError: "
                "fixture worker failure B"
            )
            second = message.index(
                "index=2 instance_id=instance-a ChronicleTeamWaveTimelineError: "
                "fixture worker failure A"
            )
            self.assertLess(first, second)
            self.assertIn("parallel partition failures (2)", message)
            self.assertFalse((output / ".late-success.jsonl.gz.tmp").exists())
            self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
