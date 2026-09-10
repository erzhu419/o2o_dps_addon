"""Import a manually exported Chronicle All Activity CSV into canonical JSONL."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Sequence


OFFICIAL_COLUMNS = (
    "#",
    "Type",
    "Time",
    "Source",
    "Source GUID",
    "Action / Ability",
    "Spell ID",
    "Target",
    "Target GUID",
    "Value",
    "Outcome / Detail",
    "Flags",
    "Activity",
    "Encounter",
    "Event Index",
    "Offset (ms)",
    "Synthetic",
)

SOURCE_FORMAT = "chronicle_all_activity_csv"
SCHEMA_VERSION = 1
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
OFFICIAL_FILENAME = re.compile(r"^all-activity-(?P<instance>.+)\.csv$", re.IGNORECASE)
INTEGER = re.compile(r"^[+-]?\d+$")
NUMBER = re.compile(r"^[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?$")


class ChronicleCSVError(ValueError):
    """The input is not a valid Chronicle All Activity CSV export."""


@dataclass(frozen=True)
class ImportResult:
    instance: str
    row_count: int
    raw_copy: Path
    normalized: Path
    metadata: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "instance": self.instance,
            "row_count": self.row_count,
            "raw_copy": str(self.raw_copy),
            "normalized": str(self.normalized),
            "metadata": str(self.metadata),
        }


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _infer_instance(source: Path, explicit: str | None) -> tuple[str, str]:
    if explicit is not None:
        instance = explicit.strip()
        if not instance:
            raise ChronicleCSVError("--instance must not be empty")
        return instance, "cli"

    match = OFFICIAL_FILENAME.match(source.name)
    if match is None or match.group("instance").lower() == "export":
        raise ChronicleCSVError(
            "cannot infer instance from filename; use the official "
            "all-activity-<instance>.csv name or pass --instance"
        )
    return match.group("instance"), "official_filename"


def _safe_filename_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not safe:
        raise ChronicleCSVError("instance does not contain a usable filename component")
    return safe


def _required_text(
    row: dict[str | None, Any], column: str, problems: list[str]
) -> str:
    value = row.get(column)
    if value is None:
        problems.append(f"{column!r} has no value")
        return ""
    text = str(value).strip()
    if not text:
        problems.append(f"{column!r} must not be empty")
    return text


def _optional_text(row: dict[str | None, Any], column: str) -> str | None:
    value = row.get(column)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer_field(
    row: dict[str | None, Any], column: str, problems: list[str], *, optional: bool = False
) -> int | None:
    value = row.get(column)
    if value is None:
        problems.append(f"{column!r} has no value")
        return None
    text = str(value).strip()
    if optional and not text:
        return None
    if not INTEGER.fullmatch(text):
        problems.append(f"{column!r} must be an integer, got {text!r}")
        return None
    return int(text)


def _boolean_field(
    row: dict[str | None, Any], column: str, problems: list[str]
) -> bool | None:
    value = row.get(column)
    if value is None:
        problems.append(f"{column!r} has no value")
        return None
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    problems.append(f"{column!r} must be true or false, got {value!r}")
    return None


def _normalized_value(value: str | None) -> str | int | float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if INTEGER.fullmatch(text):
        return int(text)
    if NUMBER.fullmatch(text):
        return float(text)
    return text


def _normalize_row(
    row: dict[str | None, Any],
    *,
    csv_line: int,
    instance: str,
    source_name: str,
    raw_relative_path: str,
    imported_at: str,
) -> dict[str, Any]:
    problems: list[str] = []

    extra_cells = row.get(None)
    if extra_cells is not None:
        problems.append(f"unexpected extra CSV cells: {extra_cells!r}")

    export_row = _integer_field(row, "#", problems)
    event_type = _required_text(row, "Type", problems)
    event_time = _required_text(row, "Time", problems)
    encounter = _required_text(row, "Encounter", problems)
    event_index = _integer_field(row, "Event Index", problems)
    offset_ms = _integer_field(row, "Offset (ms)", problems)
    spell_id = _integer_field(row, "Spell ID", problems, optional=True)
    synthetic = _boolean_field(row, "Synthetic", problems)

    if problems:
        raise ChronicleCSVError(f"CSV line {csv_line}: " + "; ".join(problems))

    flags = _optional_text(row, "Flags")
    return {
        "instance": instance,
        "encounter": encounter,
        "event_index": event_index,
        "offset_ms": offset_ms,
        "time": event_time,
        "type": event_type,
        "source": _optional_text(row, "Source"),
        "source_guid": _optional_text(row, "Source GUID"),
        "target": _optional_text(row, "Target"),
        "target_guid": _optional_text(row, "Target GUID"),
        "spell": _optional_text(row, "Action / Ability"),
        "spell_id": spell_id,
        "value": _normalized_value(_optional_text(row, "Value")),
        "outcome": _optional_text(row, "Outcome / Detail"),
        "synthetic": synthetic,
        "flags": [part.strip() for part in flags.split("|") if part.strip()]
        if flags
        else [],
        "activity": _optional_text(row, "Activity"),
        "provenance": {
            "format": SOURCE_FORMAT,
            "source_file": source_name,
            "raw_file": raw_relative_path,
            "csv_line": csv_line,
            "export_row": export_row,
            "imported_at": imported_at,
        },
    }


def _validate_header(fieldnames: list[str] | None) -> list[str]:
    if fieldnames is None:
        raise ChronicleCSVError("CSV is empty and has no header")

    duplicates = sorted({name for name in fieldnames if fieldnames.count(name) > 1})
    if duplicates:
        raise ChronicleCSVError(
            "duplicate CSV columns: " + ", ".join(repr(name) for name in duplicates)
        )

    missing = [name for name in OFFICIAL_COLUMNS if name not in fieldnames]
    if missing:
        raise ChronicleCSVError(
            "missing required Chronicle columns: "
            + ", ".join(repr(name) for name in missing)
        )

    return [name for name in fieldnames if name not in OFFICIAL_COLUMNS]


def import_chronicle_csv(
    source_csv: str | Path,
    *,
    instance: str | None = None,
    data_root: str | Path = DEFAULT_DATA_ROOT,
) -> ImportResult:
    """Validate, preserve, and normalize one manual Chronicle CSV export."""

    source = Path(source_csv).expanduser().resolve()
    if not source.is_file():
        raise ChronicleCSVError(f"input CSV does not exist or is not a file: {source}")

    resolved_instance, instance_source = _infer_instance(source, instance)
    safe_instance = _safe_filename_component(resolved_instance)
    resolved_data_root = Path(data_root).expanduser().resolve()

    now = datetime.now(timezone.utc)
    imported_at = _utc_iso(now)
    import_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    raw_dir = resolved_data_root / "chronicle_raw" / import_id
    raw_copy = raw_dir / source.name
    metadata_path = raw_dir / "provenance.json"
    normalized_dir = resolved_data_root / "normalized"
    normalized_path = normalized_dir / f"{safe_instance}__{import_id}.jsonl"
    raw_relative_path = raw_copy.relative_to(resolved_data_root).as_posix()

    temporary_path: Path | None = None
    row_count = 0
    bad_rows: list[str] = []

    try:
        with source.open("r", encoding="utf-8-sig", newline="") as source_handle:
            reader = csv.DictReader(source_handle)
            extra_columns = _validate_header(reader.fieldnames)

            normalized_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{safe_instance}__",
                suffix=".jsonl.tmp",
                dir=normalized_dir,
                delete=False,
            ) as temporary_handle:
                temporary_path = Path(temporary_handle.name)
                try:
                    for row in reader:
                        csv_line = reader.line_num
                        try:
                            normalized = _normalize_row(
                                row,
                                csv_line=csv_line,
                                instance=resolved_instance,
                                source_name=source.name,
                                raw_relative_path=raw_relative_path,
                                imported_at=imported_at,
                            )
                        except ChronicleCSVError as error:
                            bad_rows.append(str(error))
                            continue

                        json.dump(
                            normalized,
                            temporary_handle,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        temporary_handle.write("\n")
                        row_count += 1
                except csv.Error as error:
                    bad_rows.append(f"CSV line {reader.line_num}: malformed CSV: {error}")

        if bad_rows:
            raise ChronicleCSVError(
                f"{len(bad_rows)} bad row(s) in {source.name}:\n" + "\n".join(bad_rows)
            )
        if row_count == 0:
            raise ChronicleCSVError(f"{source.name} contains no event rows")

        raw_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, raw_copy)
        temporary_path.replace(normalized_path)
        temporary_path = None

        source_stat = source.stat()
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "source_format": SOURCE_FORMAT,
            "instance": resolved_instance,
            "instance_source": instance_source,
            "source": {
                "absolute_path": str(source),
                "filename": source.name,
                "size_bytes": source_stat.st_size,
                "modified_at": _utc_iso(
                    datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc)
                ),
            },
            "imported_at": imported_at,
            "row_count": row_count,
            "official_columns": list(OFFICIAL_COLUMNS),
            "extra_columns": extra_columns,
            "raw_copy": raw_relative_path,
            "normalized": normalized_path.relative_to(resolved_data_root).as_posix(),
            "acquisition": "manual Chronicle All Activity CSV export",
        }
        with metadata_path.open("w", encoding="utf-8", newline="\n") as metadata_handle:
            json.dump(metadata, metadata_handle, ensure_ascii=False, indent=2)
            metadata_handle.write("\n")

    except (OSError, UnicodeError) as error:
        raise ChronicleCSVError(f"failed to import {source}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return ImportResult(
        instance=resolved_instance,
        row_count=row_count,
        raw_copy=raw_copy,
        normalized=normalized_path,
        metadata=metadata_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_chronicle_csv",
        description=(
            "Import one manually exported Chronicle All Activity CSV. "
            "No network requests are made."
        ),
    )
    parser.add_argument("csv", type=Path, help="path to the official All Activity CSV")
    parser.add_argument(
        "--instance",
        help="instance ID/slug; inferred from all-activity-<instance>.csv by default",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"offline data root (default: {DEFAULT_DATA_ROOT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = import_chronicle_csv(
            args.csv,
            instance=args.instance,
            data_root=args.data_root,
        )
    except ChronicleCSVError as error:
        print(f"Chronicle CSV import failed: {error}", file=sys.stderr)
        return 2

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
