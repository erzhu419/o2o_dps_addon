from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps.o2o_observation import OBSERVATION_FIELDS
from o2o_dps.state_reconstruction_audit import (
    DEFAULT_OUTPUT,
    audit_state_reconstruction,
    build_state_reconstruction_audit,
)


def _missing() -> dict[str, object]:
    return {
        "kind": "MISSING",
        "event_index": None,
        "csv_line": None,
        "offset_ms": None,
        "note": "fixture missing",
    }


def _record(
    start_event_index: int,
    *,
    player_guid: str = "Player-A",
    encounter_id: str = "Encounter-1",
    spell_id: int = 23894,
    result_event_index: int | None = None,
) -> dict[str, object]:
    values = {name: None for name in OBSERVATION_FIELDS}
    masks = {name: False for name in OBSERVATION_FIELDS}
    provenance = {name: _missing() for name in OBSERVATION_FIELDS}
    values["combat_time_ms"] = start_event_index * 10
    masks["combat_time_ms"] = True
    provenance["combat_time_ms"] = {
        "kind": "OBSERVED",
        "event_index": start_event_index,
        "csv_line": start_event_index + 100,
        "offset_ms": start_event_index * 10,
        "note": "fixture START",
    }
    succeeded = result_event_index is not None
    return {
        "schema": "chronicle_fury_decision/v1",
        "identity": {
            "player_guid": player_guid,
            "encounter_id": encounter_id,
        },
        "source": {
            "start_anchor": {
                "kind": "OBSERVED",
                "event_index": start_event_index,
                "csv_line": start_event_index + 100,
                "offset_ms": start_event_index * 10,
            }
        },
        "action": {
            "decision_id": f"{player_guid}:{encounter_id}:{start_event_index}",
            "spell_id": spell_id,
            "spell_name": "Battle Stance" if spell_id == 2457 else "Bloodthirst",
        },
        "result": {
            "association": "unique" if succeeded else "unlinked",
            "status": "succeeded" if succeeded else "missing",
            "anchor": (
                {
                    "kind": "OBSERVED",
                    "event_index": result_event_index,
                    "csv_line": result_event_index + 100,
                    "offset_ms": result_event_index * 10,
                    "note": "fixture GO",
                }
                if succeeded
                else None
            ),
            "candidate_action_ids": [],
            "start_to_result_ms": None,
        },
        "state_before": values,
        "state_mask": masks,
        "state_provenance": provenance,
        "window_until_next_start_candidate": {},
        "eligibility": {},
    }


def _write_partition(path: Path, records: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for record in records:
            json.dump(record, handle, separators=(",", ":"))
            handle.write("\n")


def _manifest(
    root: Path, partitions: list[tuple[Path, bool]], decision_count: int
) -> Path:
    path = root / "manifest.json"
    entries = []
    for partition, relative in partitions:
        entries.append(
            {"partition": partition.name if relative else str(partition.resolve())}
        )
    path.write_text(
        json.dumps(
            {
                "schema": "chronicle_fury_decision_dataset/v1",
                "output": {"decision_count": decision_count},
                "partitions": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


class StateReconstructionAuditTests(unittest.TestCase):
    def test_streams_each_partition_once_and_counts_all_field_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.jsonl.gz"
            second = root / "second.jsonl.gz"
            _write_partition(
                first,
                [
                    _record(10, spell_id=2457, result_event_index=11),
                    _record(12),
                ],
            )
            _write_partition(second, [_record(5, player_guid="Player-B")])
            manifest = _manifest(root, [(first, False), (second, True)], 3)
            real_gzip_open = gzip.open

            with mock.patch(
                "o2o_dps.state_reconstruction_audit.gzip.open",
                side_effect=real_gzip_open,
            ) as opened:
                report = build_state_reconstruction_audit(manifest)

            self.assertEqual(opened.call_count, 2)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["coverage"]["decision_rows"], 3)
            self.assertEqual(report["coverage"]["observations_projected"], 3)
            self.assertEqual(report["coverage"]["field_count"], 29)
            self.assertEqual(
                report["coverage"]["field_status_counts"]["combat_time_ms"]
                ["OBSERVED"],
                3,
            )
            self.assertEqual(report["coverage"]["stance"]["RECONSTRUCTED"], 1)
            self.assertEqual(report["coverage"]["stance"]["MISSING"], 2)
            self.assertEqual(
                report["coverage"]["status_totals"],
                {
                    "OBSERVED": 3,
                    "RECONSTRUCTED": 1,
                    "INFERRED": 0,
                    "MISSING": 83,
                },
            )
            self.assertEqual(report["quality"]["future_cutoff_failures"], 0)
            self.assertEqual(report["quality"]["field_cell_count"], 87)
            self.assertTrue(report["quality"]["field_cell_count_matches"])
            self.assertFalse(report["quality"]["line_level_output_materialized"])
            self.assertEqual(report["input"]["partition_count"], 2)
            self.assertEqual(
                report["input"]["compressed_bytes"],
                first.stat().st_size + second.stat().st_size,
            )

    def test_future_cutoff_failure_is_counted_without_a_row_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "future.jsonl.gz"
            future = _record(10)
            future["state_before"]["recent_uniquely_linked_server_actions"] = [
                {"result_event_index": 11}
            ]
            future["state_mask"]["recent_uniquely_linked_server_actions"] = True
            future["state_provenance"][
                "recent_uniquely_linked_server_actions"
            ] = {
                "kind": "RECONSTRUCTED",
                "event_index": 9,
                "csv_line": 109,
                "offset_ms": 90,
                "note": "fixture ledger",
            }
            _write_partition(partition, [future, _record(12)])
            manifest = _manifest(root, [(partition, False)], 2)

            report = build_state_reconstruction_audit(manifest)

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["coverage"]["decision_rows"], 2)
            self.assertEqual(report["coverage"]["observations_projected"], 1)
            self.assertEqual(report["quality"]["future_cutoff_failures"], 1)
            self.assertEqual(report["coverage"]["stance"]["MISSING"], 1)
            self.assertEqual(report["quality"]["field_cell_count"], 29)

    def test_audit_writes_only_the_requested_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            partition = root / "input.jsonl.gz"
            _write_partition(partition, [_record(10)])
            manifest = _manifest(root, [(partition, False)], 1)
            reports = root / "reports"
            output = reports / "coverage.json"

            report, written = audit_state_reconstruction(
                manifest, output_path=output
            )

            self.assertEqual(written, output.resolve())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["coverage"],
                report["coverage"],
            )
            self.assertEqual([path.name for path in reports.iterdir()], ["coverage.json"])
            self.assertEqual(
                DEFAULT_OUTPUT.name, "fury_state_reconstruction_v1.json"
            )


if __name__ == "__main__":
    unittest.main()
