from __future__ import annotations

import csv
from pathlib import Path
import json
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_dataset_audit import (
    KEY_FAMILIES,
    audit_dataset,
    build_audit_report,
    main,
)
from o2o_dps.import_chronicle_csv import OFFICIAL_COLUMNS


UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"


def _write_instance(
    data_root: Path,
    *,
    queue_id: str,
    slug: str,
    download_uuid: str,
    event_types: list[str],
    import_number: int,
) -> dict[str, object]:
    import_id = f"20260829T00000{import_number}000000Z"
    raw_directory = data_root / "chronicle_raw" / import_id
    raw_directory.mkdir(parents=True, exist_ok=True)
    normalized_directory = data_root / "normalized"
    normalized_directory.mkdir(parents=True, exist_ok=True)
    raw_path = raw_directory / f"all-activity-{download_uuid}.csv"
    with raw_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(OFFICIAL_COLUMNS)

    normalized_path = normalized_directory / f"{download_uuid}__{import_id}.jsonl"
    with normalized_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, event_type in enumerate(event_types):
            handle.write(
                json.dumps(
                    {
                        "instance": download_uuid,
                        "event_index": index,
                        "type": event_type,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    provenance_path = raw_directory / "provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance": download_uuid,
                "row_count": len(event_types),
                "raw_copy": raw_path.relative_to(data_root).as_posix(),
                "normalized": normalized_path.relative_to(data_root).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    return {
        "instance_id": queue_id,
        "slug": slug,
        "status": "imported",
        "download_instance_id": download_uuid,
        "event_stream_selection": (
            "requested_by_parallel_official_ui_workflow; "
            "not machine-verifiable_from_csv"
        ),
        "import_receipt": {
            "instance": download_uuid,
            "row_count": len(event_types),
            "raw_copy": str(raw_path),
            "normalized": str(normalized_path),
            "metadata": str(provenance_path),
        },
    }


class ChronicleDatasetAuditTests(unittest.TestCase):
    def _dataset(self, root: Path) -> Path:
        data_root = root / "offline_data"
        entries = [
            _write_instance(
                data_root,
                queue_id="page-slug-as-id",
                slug="page-slug-as-id",
                download_uuid=UUID_A,
                event_types=list(KEY_FAMILIES) + ["DMG", "OTHER"],
                import_number=1,
            ),
            _write_instance(
                data_root,
                queue_id=UUID_B,
                slug="second-page-slug",
                download_uuid=UUID_B,
                event_types=["DMG", "GO", "START", "RES", "AURA", "CLASS"],
                import_number=2,
            ),
            _write_instance(
                data_root,
                queue_id="third-page-slug",
                slug="third-page-slug",
                download_uuid=UUID_C,
                event_types=["damage", "cast"],
                import_number=3,
            ),
        ]
        queue_path = data_root / "chronicle_raw" / "export_queue.json"
        queue_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "chronicle_manual_export_queue",
                    "entries": entries,
                }
            ),
            encoding="utf-8",
        )
        return data_root

    def test_full_scan_grades_and_resolves_slug_to_download_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = build_audit_report(
                self._dataset(Path(temporary_directory)), workers=1
            )

            self.assertEqual(report["summary"]["normalized_row_count"], 18)
            self.assertEqual(
                report["summary"]["grade_counts"], {"A": 1, "B": 1, "C": 1}
            )
            self.assertEqual(report["summary"]["event_type_counts"]["DMG"], 3)
            first = report["instances"][0]
            self.assertEqual(first["canonical_instance_uuid"], UUID_A)
            self.assertEqual(first["canonical_uuid_source"], "download_instance_id")
            self.assertIn("page-slug-as-id", first["aliases"])
            self.assertIn(UUID_A, first["aliases"])
            self.assertTrue(first["raw_header"]["valid_official_columns"])
            self.assertEqual(first["normalized_scan"]["event_type_counts"]["DMG"], 2)
            self.assertEqual(first["families"]["INFO"]["status"], "observed")
            self.assertEqual(
                report["instances"][2]["families"]["RES"]["status"],
                "empty_or_unobserved",
            )
            self.assertEqual(
                report["evidence_boundary"]["stream_selection"],
                "not machine-verifiable from an exported All Activity CSV",
            )

    def test_field_matrix_has_all_four_provenance_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = build_audit_report(
                self._dataset(Path(temporary_directory)), workers=1
            )
            matrix = {row["field"]: row for row in report["field_provenance_matrix"]}
            self.assertEqual(
                matrix["resource_delta_including_rage_gain_or_loss"]["status"],
                "OBSERVED",
            )
            self.assertEqual(matrix["absolute_rage"]["status"], "MISSING")
            self.assertEqual(
                matrix["target_armor_after_debuffs"]["status"], "INFERRED"
            )
            for field in (
                "keypress",
                "queue_intent",
                "queue_cancel",
                "position",
                "range",
                "movement",
            ):
                self.assertEqual(matrix[field]["status"], "MISSING")

    def test_cli_writes_only_json_and_markdown_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = self._dataset(Path(temporary_directory))
            return_code = main(
                [
                    "--data-root",
                    str(data_root),
                    "--workers",
                    "1",
                    "--report-stem",
                    "quality-test",
                ]
            )

            self.assertEqual(return_code, 0)
            json_path = data_root / "reports" / "quality-test.json"
            markdown_path = data_root / "reports" / "quality-test.md"
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            report_text = json_path.read_text(encoding="utf-8")
            self.assertNotIn("checksum", report_text.lower())
            self.assertNotIn("fingerprint", report_text.lower())
            self.assertIn("empty_or_unobserved", report_text)
            self.assertIn("Evidence boundary", markdown_path.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(path.name for path in (data_root / "reports").iterdir()),
                ["quality-test.json", "quality-test.md"],
            )

    def test_audit_result_exposes_written_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = self._dataset(Path(temporary_directory))
            result = audit_dataset(data_root, workers=1, report_stem="audit")
            self.assertEqual(result.json_report.name, "audit.json")
            self.assertEqual(result.markdown_report.name, "audit.md")


if __name__ == "__main__":
    unittest.main()
