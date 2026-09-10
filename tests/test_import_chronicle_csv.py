from __future__ import annotations

import csv
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

from o2o_dps.import_chronicle_csv import (
    ChronicleCSVError,
    OFFICIAL_COLUMNS,
    import_chronicle_csv,
    main,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "all-activity-instance-fixture.csv"
)


class ChronicleCSVImportTests(unittest.TestCase):
    def test_cli_prints_machine_readable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = main(
                    [str(FIXTURE), "--data-root", str(data_root)]
                )

            self.assertEqual(return_code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "ok")
            self.assertEqual(receipt["instance"], "instance-fixture")
            self.assertEqual(receipt["row_count"], 3)
            self.assertTrue(Path(receipt["raw_copy"]).is_file())
            self.assertTrue(Path(receipt["normalized"]).is_file())

    def test_import_preserves_raw_file_and_normalizes_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "offline_data"
            result = import_chronicle_csv(FIXTURE, data_root=data_root)

            self.assertEqual(result.instance, "instance-fixture")
            self.assertEqual(result.row_count, 3)
            self.assertEqual(result.raw_copy.read_bytes(), FIXTURE.read_bytes())

            rows = [
                json.loads(line)
                for line in result.normalized.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 3)
            self.assertEqual(
                set(
                    (
                        "instance",
                        "encounter",
                        "event_index",
                        "offset_ms",
                        "time",
                        "type",
                        "source",
                        "target",
                        "spell",
                        "value",
                        "outcome",
                        "synthetic",
                        "provenance",
                    )
                ).difference(rows[0]),
                set(),
            )
            self.assertEqual(rows[0]["instance"], "instance-fixture")
            self.assertEqual(rows[1]["event_index"], 11)
            self.assertEqual(rows[1]["value"], 642)
            self.assertEqual(rows[1]["flags"], ["critical", "spell"])
            self.assertEqual(rows[2]["offset_ms"], -500)
            self.assertTrue(rows[2]["synthetic"])
            self.assertEqual(rows[0]["provenance"]["csv_line"], 2)
            self.assertEqual(
                rows[0]["provenance"]["raw_file"],
                result.raw_copy.relative_to(data_root).as_posix(),
            )

            metadata_text = result.metadata.read_text(encoding="utf-8")
            metadata = json.loads(metadata_text)
            self.assertEqual(metadata["row_count"], 3)
            self.assertEqual(metadata["instance_source"], "official_filename")
            self.assertNotIn("checksum", metadata_text.lower())
            self.assertNotIn("fingerprint", metadata_text.lower())
            self.assertNotIn('"hash', metadata_text.lower())

    def test_missing_official_column_fails_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "all-activity-missing-column.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(OFFICIAL_COLUMNS[:-1])

            data_root = root / "offline_data"
            with self.assertRaisesRegex(
                ChronicleCSVError, "missing required Chronicle columns.*Synthetic"
            ):
                import_chronicle_csv(source, data_root=data_root)

            self.assertFalse(data_root.exists())

    def test_bad_rows_report_line_and_all_invalid_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "all-activity-invalid-row.csv"
            invalid = {name: "" for name in OFFICIAL_COLUMNS}
            invalid.update(
                {
                    "#": "0",
                    "Type": "damage",
                    "Time": "12:00:00.000",
                    "Encounter": "encounter-1",
                    "Event Index": "not-an-index",
                    "Offset (ms)": "1.5",
                    "Synthetic": "maybe",
                }
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=OFFICIAL_COLUMNS)
                writer.writeheader()
                writer.writerow(invalid)

            data_root = root / "offline_data"
            with self.assertRaises(ChronicleCSVError) as raised:
                import_chronicle_csv(source, data_root=data_root)

            message = str(raised.exception)
            self.assertIn("CSV line 2", message)
            self.assertIn("Event Index", message)
            self.assertIn("Offset (ms)", message)
            self.assertIn("Synthetic", message)
            normalized = data_root / "normalized"
            self.assertEqual(list(normalized.glob("*.jsonl")), [])
            self.assertEqual(list(normalized.glob("*.tmp")), [])
            self.assertFalse((data_root / "chronicle_raw").exists())


if __name__ == "__main__":
    unittest.main()
