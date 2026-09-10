from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import o2o_dps.chronicle_encounter_batch_v1 as batch_module
from o2o_dps.chronicle_encounter_batch_v1 import (
    ChronicleEncounterBatchError,
    KNOWN_SUMMON_CONTRACT_KEY,
    MANIFEST_KIND,
    load_json_document,
    run_batch,
)


PLAYER = "0x0000000000000001"
MOB = "0xF130000001000001"


def _row(
    event_index: int,
    offset_ms: int,
    event_type: str,
    *,
    source: str | None = None,
    source_guid: str | None = None,
    target: str | None = None,
    target_guid: str | None = None,
    value: int | None = None,
    outcome: str | None = None,
) -> dict[str, object]:
    return {
        "instance": "instance-1",
        "encounter": "encounter-1",
        "event_index": event_index,
        "offset_ms": offset_ms,
        "time": f"00:00:{offset_ms / 1000:06.3f}",
        "type": event_type,
        "source": source,
        "source_guid": source_guid,
        "target": target,
        "target_guid": target_guid,
        "spell": None,
        "spell_id": None,
        "value": value,
        "outcome": outcome,
        "flags": [],
        "activity": None,
        "provenance": {
            "source_file": "fixture.csv",
            "csv_line": event_index + 2,
            "export_row": event_index,
        },
    }


def _normalized_rows() -> list[dict[str, object]]:
    return [
        _row(0, 0, "CLASS", target="Warrior", target_guid=PLAYER, outcome="Friendly Player"),
        _row(1, 0, "CLASS", target="Mob", target_guid=MOB, outcome="Hostile Creature"),
        _row(
            2,
            1_000,
            "DMG",
            source="Warrior",
            source_guid=PLAYER,
            target="Mob",
            target_guid=MOB,
            value=100,
            outcome="HIT",
        ),
        _row(3, 2_000, "DEAD", target="Mob", target_guid=MOB),
    ]


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _base_request() -> dict[str, object]:
    stats = [0] * 35
    return {
        "raid": {"parties": [{"players": [{"name": "Warrior"}]}]},
        "encounter": {
            "duration": 30,
            "durationVariation": 0,
            "useHealth": False,
            "targets": [{"name": "Template", "level": 60, "stats": stats}],
        },
        "simOptions": {"randomSeed": 1},
    }


class ChronicleEncounterBatchV1Tests(unittest.TestCase):
    def test_writes_gzip_v1_artifacts_manifest_and_resumes_without_source_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            source = input_dir / "one.jsonl"
            _write_jsonl(source, _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")

            first = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            self.assertEqual(first["kind"], MANIFEST_KIND)
            self.assertEqual(first["summary"]["completed_file_count"], 1)
            self.assertEqual(first["artifact_contract"][KNOWN_SUMMON_CONTRACT_KEY], [17252])
            self.assertFalse(first["source"]["source_rows_copied"])
            entry = first["entries"][0]
            self.assertEqual(entry["processing_status"], "COMPLETED")
            feature_path = output_dir / entry["outputs"]["feature_report"]["path"]
            catalog_path = output_dir / entry["outputs"]["scenario_catalog"]["path"]
            self.assertEqual(feature_path.suffix, ".gz")
            self.assertEqual(catalog_path.suffix, ".gz")
            with gzip.open(feature_path, "rt", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["kind"], "chronicle_encounter_reconstruction_v1")
            self.assertEqual(
                load_json_document(catalog_path)["kind"],
                "fury_encounter_scenario_catalog_v1",
            )
            self.assertEqual(first["storage_projection"]["status"], "INFERRED")
            self.assertEqual(first["npc_identity_aggregates"]["items"][0]["health_scenario"]["status"], "MISSING")
            self.assertEqual(list(output_dir.rglob("*.jsonl")), [])

            with patch(
                "o2o_dps.chronicle_encounter_batch_v1.reconstruct_file",
                side_effect=AssertionError("resume unexpectedly rescanned the source"),
            ):
                second = run_batch(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    base_request_path=base,
                    armor_hypotheses=(1721,),
                    level_hypotheses=(60,),
                )
            self.assertEqual(second["entries"][0]["attempt_count"], 1)

    def test_legacy_contract_missing_only_known_summons_reuses_safe_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            source = input_dir / "one.jsonl"
            _write_jsonl(source, _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")

            first = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            feature_path = output_dir / first["entries"][0]["outputs"]["feature_report"]["path"]
            feature_mtime_ns = feature_path.stat().st_mtime_ns
            manifest_path = output_dir / "manifest.json"
            legacy = load_json_document(manifest_path)
            legacy["artifact_contract"].pop(KNOWN_SUMMON_CONTRACT_KEY)
            manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

            with patch(
                "o2o_dps.chronicle_encounter_batch_v1.reconstruct_file",
                side_effect=AssertionError("safe legacy feature unexpectedly rescanned JSONL"),
            ):
                second = run_batch(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    base_request_path=base,
                    armor_hypotheses=(1721,),
                    level_hypotheses=(60,),
                )

            self.assertEqual(second["entries"][0]["attempt_count"], 1)
            self.assertEqual(feature_path.stat().st_mtime_ns, feature_mtime_ns)
            self.assertEqual(second["artifact_contract"][KNOWN_SUMMON_CONTRACT_KEY], [17252])

    def test_legacy_contract_rebuilds_feature_containing_newly_excluded_summon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            source = input_dir / "one.jsonl"
            _write_jsonl(source, _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")

            first = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            entry = first["entries"][0]
            feature_path = output_dir / entry["outputs"]["feature_report"]["path"]
            feature = load_json_document(feature_path)
            feature["encounters"][0]["waves"][0]["targets"][0]["creature_entry_id"] = 17252
            with gzip.open(feature_path, "wt", encoding="utf-8") as handle:
                json.dump(feature, handle)

            manifest_path = output_dir / "manifest.json"
            legacy = load_json_document(manifest_path)
            legacy["artifact_contract"].pop(KNOWN_SUMMON_CONTRACT_KEY)
            manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

            original_reconstruct = batch_module.reconstruct_file
            with patch(
                "o2o_dps.chronicle_encounter_batch_v1.reconstruct_file",
                wraps=original_reconstruct,
            ) as reconstruct:
                second = run_batch(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    base_request_path=base,
                    armor_hypotheses=(1721,),
                    level_hypotheses=(60,),
                )

            self.assertEqual(reconstruct.call_count, 1)
            rebuilt_feature = load_json_document(
                output_dir / second["entries"][0]["outputs"]["feature_report"]["path"]
            )
            rebuilt_targets = rebuilt_feature["encounters"][0]["waves"][0]["targets"]
            self.assertEqual([target["creature_entry_id"] for target in rebuilt_targets], [1])

    def test_legacy_compatibility_rejects_any_additional_contract_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            source = input_dir / "one.jsonl"
            _write_jsonl(source, _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")

            run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            manifest_path = output_dir / "manifest.json"
            legacy = load_json_document(manifest_path)
            legacy["artifact_contract"].pop(KNOWN_SUMMON_CONTRACT_KEY)
            legacy["artifact_contract"]["combat_gap_ms"] += 1
            manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

            original_reconstruct = batch_module.reconstruct_file
            with patch(
                "o2o_dps.chronicle_encounter_batch_v1.reconstruct_file",
                wraps=original_reconstruct,
            ) as reconstruct:
                run_batch(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    base_request_path=base,
                    armor_hypotheses=(1721,),
                    level_hypotheses=(60,),
                )

            self.assertEqual(reconstruct.call_count, 1)

    def test_signature_change_reprocesses_and_bounded_run_has_no_storage_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            source = input_dir / "one.jsonl"
            _write_jsonl(source, _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")
            first = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                max_encounters=1,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            self.assertEqual(first["storage_projection"]["status"], "MISSING")
            source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            second = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                max_encounters=1,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            self.assertEqual(second["entries"][0]["attempt_count"], 1)
            self.assertEqual(second["summary"]["completed_file_count"], 1)

    def test_max_files_selects_smallest_but_manifest_keeps_pending_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            small = input_dir / "small.jsonl"
            large = input_dir / "large.jsonl"
            _write_jsonl(small, _normalized_rows())
            _write_jsonl(large, _normalized_rows() + _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")
            result = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                max_files=1,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            self.assertEqual(result["summary"]["completed_file_count"], 1)
            self.assertEqual(result["summary"]["pending_file_count"], 1)
            completed = [
                entry for entry in result["entries"] if entry["processing_status"] == "COMPLETED"
            ]
            self.assertEqual(completed[0]["source"]["name"], small.name)

    def test_force_source_rebuilds_only_named_completed_source_and_recomputes_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            one = input_dir / "one.jsonl"
            two = input_dir / "two.jsonl"
            _write_jsonl(one, _normalized_rows())
            _write_jsonl(two, _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")

            first = run_batch(
                input_dir=input_dir,
                output_dir=output_dir,
                base_request_path=base,
                armor_hypotheses=(1721,),
                level_hypotheses=(60,),
            )
            initial_identity = first["npc_identity_aggregates"]["items"][0]
            self.assertEqual(initial_identity["kill_budget_proxy_summary"]["count"], 2)
            self.assertEqual(initial_identity["kill_budget_proxy_summary"]["median"], 100)

            original_reconstruct = batch_module.reconstruct_file
            rebuilt_names: list[str] = []

            def changed_reconstruction(source: Path, **kwargs):
                rebuilt_names.append(source.name)
                report = original_reconstruct(source, **kwargs)
                target = report["encounters"][0]["waves"][0]["targets"][0]
                target["kill_budget_proxy"]["value"] = 400
                return report

            with patch(
                "o2o_dps.chronicle_encounter_batch_v1.reconstruct_file",
                side_effect=changed_reconstruction,
            ):
                second = run_batch(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    base_request_path=base,
                    max_files=1,
                    armor_hypotheses=(1721,),
                    level_hypotheses=(60,),
                    force_sources=(two.name,),
                )

            self.assertEqual(rebuilt_names, [two.name])
            by_name = {entry["source"]["name"]: entry for entry in second["entries"]}
            self.assertEqual(by_name[one.name]["attempt_count"], 1)
            self.assertEqual(by_name[two.name]["attempt_count"], 2)
            self.assertEqual(second["selection"]["selected_file_count_this_run"], 2)
            self.assertEqual(second["selection"]["forced_source_count_this_run"], 1)
            self.assertEqual(second["selection"]["forced_source_names_this_run"], [two.name])
            self.assertEqual(second["run_result"]["forced_source_names"], [two.name])
            rebuilt_identity = second["npc_identity_aggregates"]["items"][0]
            rebuilt_stats = rebuilt_identity["kill_budget_proxy_summary"]
            self.assertEqual(rebuilt_stats["count"], 2)
            self.assertEqual(rebuilt_stats["median"], 250)

    def test_force_source_rejects_unknown_selector_before_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "normalized"
            output_dir = root / "derived"
            input_dir.mkdir()
            _write_jsonl(input_dir / "one.jsonl", _normalized_rows())
            base = root / "base.json"
            base.write_text(json.dumps(_base_request()), encoding="utf-8")

            with patch(
                "o2o_dps.chronicle_encounter_batch_v1.reconstruct_file",
                side_effect=AssertionError("unknown selector must fail before reconstruction"),
            ):
                with self.assertRaisesRegex(
                    ChronicleEncounterBatchError,
                    "forced source does not match",
                ):
                    run_batch(
                        input_dir=input_dir,
                        output_dir=output_dir,
                        base_request_path=base,
                        armor_hypotheses=(1721,),
                        level_hypotheses=(60,),
                        force_sources=("missing.jsonl",),
                    )
            self.assertFalse((output_dir / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
