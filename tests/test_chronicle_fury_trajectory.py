from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_fury_trajectory import (
    FuryTrajectoryError,
    build_fury_partial_trajectory,
    main,
    parse_combatant_info,
    parse_consume_detail,
    parse_type_amount,
)


INSTANCE = "043b4d65-9c58-4643-b601-62f1684af04a"
SLUG = "rxHQPcS85kuLoZx-"
ENCOUNTER = "b2a45869-540e-4a63-8271-dd61903f50e7"
GUID = "0x000000000066ADAE"
NAME = "Narcissly"


def _row(
    event_index: int,
    event_type: str,
    *,
    offset_ms: int,
    source: str | None = NAME,
    source_guid: str | None = GUID,
    target: str | None = "Target",
    target_guid: str | None = "0xF130000000000001",
    spell: str | None = None,
    spell_id: int | None = None,
    value: object = None,
    outcome: str | None = None,
    synthetic: bool = False,
    flags: list[str] | None = None,
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
        "synthetic": synthetic,
        "flags": flags or [],
        "activity": None,
        "provenance": {
            "format": "chronicle_all_activity_csv",
            "source_file": f"all-activity-{INSTANCE}.csv",
            "raw_file": f"chronicle_raw/test/all-activity-{INSTANCE}.csv",
            "csv_line": event_index + 100,
            "export_row": event_index,
            "imported_at": "2026-08-29T00:00:00.000000Z",
        },
    }


def _write_fixture(root: Path) -> Path:
    normalized_dir = root / "normalized"
    normalized_dir.mkdir(parents=True)
    normalized = normalized_dir / f"{INSTANCE}__fixture.jsonl"
    rows = [
        _row(
            1,
            "INFO",
            offset_ms=0,
            target=NAME,
            target_guid=GUID,
            spell="Combatant Info",
            value=19,
            outcome="WARRIOR Human talents=21/30/0 gear=19 slots guild=Example Guild",
        ),
        _row(
            2,
            "CONS",
            offset_ms=0,
            target=None,
            target_guid=None,
            spell="Juju Power",
            spell_id=16323,
            value=0,
            outcome=(
                "kind=Active at Pull · confidence=Effect Derived · item=12451 · projection"
            ),
            synthetic=True,
            flags=["SYNTHETIC", "ITEM", "PROJECTED"],
        ),
        _row(
            3,
            "START",
            offset_ms=100,
            spell="Bloodthirst",
            spell_id=23894,
            value="—",
            outcome="cast=0ms",
        ),
        _row(
            4,
            "GO",
            offset_ms=110,
            spell="Bloodthirst",
            spell_id=23894,
            value=1,
            outcome="hits=1 misses=0",
        ),
        _row(
            5,
            "DMG",
            offset_ms=112,
            spell="Bloodthirst",
            spell_id=23894,
            value="1,234",
            outcome="Crit · Physical",
            flags=["CRIT"],
        ),
        _row(
            6,
            "RES",
            offset_ms=115,
            target=NAME,
            target_guid=GUID,
            spell="Mighty Rage",
            spell_id=17528,
            value="1,000",
            outcome="Gain · Rage",
            flags=["RAGE"],
        ),
        _row(
            7,
            "START",
            offset_ms=200,
            spell="Heroic Strike",
            spell_id=25286,
            value="—",
            outcome="cast=0ms",
        ),
        _row(
            8,
            "START",
            offset_ms=201,
            spell="Heroic Strike",
            spell_id=25286,
            value="—",
            outcome="cast=0ms",
        ),
        _row(
            9,
            "FAIL",
            offset_ms=205,
            spell="Heroic Strike",
            spell_id=25286,
            value=0,
            outcome="failed by server",
        ),
        _row(
            10,
            "START",
            offset_ms=300,
            spell="Whirlwind",
            spell_id=1680,
            value="—",
            outcome="cast=0ms",
        ),
        _row(
            11,
            "GO",
            offset_ms=310,
            spell="Whirlwind",
            spell_id=1680,
            value=1,
            outcome="hits=1 misses=0",
        ),
        _row(
            12,
            "DEAD",
            offset_ms=312,
            spell="Whirlwind",
            spell_id=1680,
            value="2,345",
            outcome="Hit · Physical",
        ),
        _row(
            13,
            "AURA",
            offset_ms=315,
            source=None,
            source_guid=None,
            target=NAME,
            target_guid=GUID,
            spell="Flurry",
            spell_id=12970,
            value=3,
            outcome="Added (stacks=3)",
        ),
        _row(
            14,
            "CLASS",
            offset_ms=316,
            source=None,
            source_guid=None,
            target=NAME,
            target_guid=GUID,
            spell="Classification",
            value="W",
            outcome="Friendly Player",
        ),
    ]
    with normalized.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            json.dump(row, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")

    queue_dir = root / "chronicle_raw"
    queue_dir.mkdir()
    queue = {
        "schema_version": 1,
        "kind": "chronicle_export_queue",
        "entries": [
            {
                "instance_id": INSTANCE,
                "slug": SLUG,
                "leaderboard_rows": [
                    {"character": NAME, "observed_spec": "Fury"}
                ],
                "import_receipt": {"normalized": str(normalized)},
            }
        ],
    }
    (queue_dir / "export_queue.json").write_text(
        json.dumps(queue, ensure_ascii=False), encoding="utf-8"
    )
    return normalized


class ChronicleFuryTrajectoryTests(unittest.TestCase):
    def test_type_specific_amount_parser_handles_thousands(self) -> None:
        self.assertEqual(parse_type_amount("DMG", "1,482"), 1482)
        self.assertEqual(parse_type_amount("DEAD", "-2,345"), -2345)
        self.assertEqual(parse_type_amount("RES", "1,000.5"), 1000.5)
        self.assertIsNone(parse_type_amount("GO", "1,482"))
        self.assertIsNone(parse_type_amount("DMG", "12,34"))
        self.assertIsNone(parse_type_amount("DMG", "—"))

    def test_info_and_cons_parsers_preserve_export_semantics(self) -> None:
        info = parse_combatant_info(
            "WARRIOR High Elf talents=21/30/0 gear=19 slots guild=Example Guild"
        )
        self.assertEqual(info["class"], "WARRIOR")
        self.assertEqual(info["race"], "High Elf")
        self.assertEqual(info["talent_tree"], [21, 30, 0])
        self.assertEqual(info["gear_slot_count"], 19)
        self.assertEqual(info["guild"], "Example Guild")

        attributes, markers = parse_consume_detail(
            "kind=Active at Pull · confidence=Effect Derived · item=12451 · projection"
        )
        self.assertEqual(attributes["confidence"], "Effect Derived")
        self.assertEqual(attributes["item"], "12451")
        self.assertEqual(markers, ["projection"])

    def test_end_to_end_keeps_partial_linkage_and_field_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            normalized = _write_fixture(data_root)
            original = normalized.read_bytes()

            result = build_fury_partial_trajectory(
                instance=SLUG,
                player_guid=GUID,
                player_name=NAME,
                data_root=data_root,
            )

            self.assertEqual(normalized.read_bytes(), original)
            records = [
                json.loads(line)
                for line in result.trajectory.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(result.record_count, len(records))
            self.assertGreater(len(records), 0)
            for record in records:
                self.assertEqual(record["schema_version"], 1)
                self.assertEqual(
                    record["identity"]["canonical_instance_id"], INSTANCE
                )
                self.assertEqual(record["identity"]["source_instance_ref"], INSTANCE)
                self.assertEqual(record["identity"]["public_slug"], SLUG)
                expected_paths = {
                    *(f"identity.{key}" for key in record["identity"]),
                    *(f"event.{key}" for key in record["event"]),
                    *(f"fields.{key}" for key in record["fields"]),
                }
                self.assertEqual(set(record["field_provenance"]), expected_paths)
                for evidence in record["field_provenance"].values():
                    self.assertIn(
                        evidence["kind"],
                        {"OBSERVED", "RECONSTRUCTED", "INFERRED", "MISSING"},
                    )
                    self.assertIsInstance(evidence["event_index"], int)
                    self.assertIsInstance(evidence["csv_line"], int)

            candidates = [
                row for row in records if row["record_kind"] == "candidate_action"
            ]
            self.assertEqual(len(candidates), 4)
            self.assertTrue(all(row["fields"]["candidate_only"] for row in candidates))
            whirlwind = next(
                row for row in candidates if row["fields"]["spell_name"] == "Whirlwind"
            )
            self.assertEqual(
                whirlwind["fields"]["state_observed_rage_gain_chronicle_units"],
                1000,
            )
            self.assertIsNone(whirlwind["fields"]["state_absolute_rage"])
            self.assertEqual(
                whirlwind["field_provenance"]["fields.state_absolute_rage"]["kind"],
                "MISSING",
            )

            results = [row for row in records if row["record_kind"] == "action_result"]
            bloodthirst = next(
                row for row in results if row["fields"]["spell_name"] == "Bloodthirst"
            )
            heroic = next(
                row for row in results if row["fields"]["spell_name"] == "Heroic Strike"
            )
            self.assertEqual(bloodthirst["fields"]["association_status"], "unique")
            self.assertIsNotNone(bloodthirst["fields"]["matched_candidate_action_id"])
            self.assertEqual(heroic["fields"]["association_status"], "ambiguous")
            self.assertEqual(len(heroic["fields"]["candidate_action_ids"]), 2)
            self.assertIsNone(heroic["fields"]["matched_candidate_action_id"])

            rewards = [row for row in records if row["record_kind"] == "reward_event"]
            self.assertEqual(
                [row["fields"]["damage_amount"] for row in rewards], [1234, 2345]
            )
            self.assertFalse(rewards[0]["fields"]["lethal_damage_event"])
            self.assertTrue(rewards[1]["fields"]["lethal_damage_event"])

            resource = next(
                row for row in records if row["record_kind"] == "resource_event"
            )
            self.assertEqual(resource["fields"]["amount_chronicle_units"], 1000)
            self.assertEqual(resource["fields"]["wow_rage_delta_candidate"], 100.0)
            self.assertEqual(
                resource["field_provenance"]["fields.wow_rage_delta_candidate"]["kind"],
                "INFERRED",
            )

            context = next(
                row for row in records if row["record_kind"] == "encounter_context"
            )
            self.assertEqual(context["fields"]["talent_tree"], [21, 30, 0])
            self.assertEqual(context["fields"]["gear_slot_count"], 19)
            self.assertIsNone(context["fields"]["gear_items"])

            consume = next(
                row for row in records if row["record_kind"] == "consume_evidence"
            )
            self.assertTrue(consume["fields"]["synthetic"])
            self.assertTrue(consume["fields"]["projected"])
            self.assertEqual(consume["fields"]["confidence"], "Effect Derived")

            manifest_text = result.manifest.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["rage"]["gain_rows"], 1)
            self.assertEqual(manifest["rage"]["loss_rows"], 0)
            self.assertFalse(manifest["rage"]["loss_rage_observed_in_selected_csv"])
            self.assertEqual(manifest["quality"]["trajectory_status"], "PARTIAL")
            self.assertEqual(manifest["action_association"]["ambiguous_results"], 1)
            self.assertNotIn("checksum", manifest_text.lower())
            self.assertNotIn("fingerprint", manifest_text.lower())

    def test_cli_accepts_instance_and_player_guid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            _write_fixture(data_root)
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = main(
                    [
                        "--instance",
                        INSTANCE,
                        "--player-guid",
                        GUID,
                        "--data-root",
                        str(data_root),
                    ]
                )
            self.assertEqual(return_code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["canonical_instance_id"], INSTANCE)
            self.assertTrue(Path(receipt["trajectory"]).is_file())
            self.assertTrue(Path(receipt["manifest"]).is_file())

    def test_no_player_events_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            _write_fixture(data_root)
            output_dir = Path(temporary_directory) / "derived"
            with self.assertRaisesRegex(FuryTrajectoryError, "no selected events"):
                build_fury_partial_trajectory(
                    instance=INSTANCE,
                    player_guid="0x0000000000000000",
                    data_root=data_root,
                    output_dir=output_dir,
                )
            self.assertEqual(list(output_dir.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
