"""Audit StateReconstructorV1 coverage over compact Fury partitions."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sys
from typing import Any

from .o2o_observation import OBSERVATION_FIELDS, STATUSES
from .state_reconstructor_v1 import (
    FutureStateEvidenceError,
    StateReconstructionError,
    StateReconstructorV1,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_fury_decision_dataset"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT = (
    DEFAULT_DATA_ROOT / "reports" / "fury_state_reconstruction_v1.json"
)
SCHEMA = "fury_state_reconstruction_audit/v1"


class StateReconstructionAuditError(RuntimeError):
    """The compact manifest or one of its partitions cannot be audited."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StateReconstructionAuditError(
            f"cannot read compact dataset manifest {path}: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise StateReconstructionAuditError("compact dataset manifest must be an object")
    if manifest.get("schema") != "chronicle_fury_decision_dataset/v1":
        raise StateReconstructionAuditError("unsupported compact dataset schema")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise StateReconstructionAuditError("compact dataset manifest has no partitions")
    return manifest


def _partition_path(entry: Mapping[str, Any], manifest_path: Path) -> Path:
    raw = str(entry.get("partition") or "").strip()
    if not raw:
        raise StateReconstructionAuditError("partition manifest entry has no path")
    path = Path(raw)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _empty_field_counts() -> dict[str, dict[str, int]]:
    return {
        name: {status: 0 for status in STATUSES} for name in OBSERVATION_FIELDS
    }


def build_state_reconstruction_audit(
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Stream every compact partition once and return one coverage report."""

    path = Path(manifest_path).expanduser().resolve()
    manifest = _load_manifest(path)
    entries = manifest["partitions"]
    reconstructor = StateReconstructorV1()
    fields = _empty_field_counts()
    status_totals = {status: 0 for status in STATUSES}
    total_rows = 0
    projected_rows = 0
    future_cutoff_failures = 0
    compressed_bytes = 0
    partition_reports: list[dict[str, Any]] = []

    for partition_index, entry_value in enumerate(entries):
        if not isinstance(entry_value, Mapping):
            raise StateReconstructionAuditError(
                f"partition manifest entry {partition_index} is not an object"
            )
        partition = _partition_path(entry_value, path)
        try:
            partition_bytes = partition.stat().st_size
        except OSError as error:
            raise StateReconstructionAuditError(
                f"cannot stat compact partition {partition}: {error}"
            ) from error
        compressed_bytes += partition_bytes
        partition_rows = 0
        partition_projected = 0
        partition_future_failures = 0
        try:
            with gzip.open(partition, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    partition_rows += 1
                    total_rows += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise StateReconstructionAuditError(
                            f"invalid JSON at {partition}:{line_number}: {error}"
                        ) from error
                    if not isinstance(record, dict):
                        raise StateReconstructionAuditError(
                            f"compact row is not an object at {partition}:{line_number}"
                        )
                    try:
                        observation = reconstructor.reconstruct_observation(record)
                    except FutureStateEvidenceError:
                        future_cutoff_failures += 1
                        partition_future_failures += 1
                        continue
                    except StateReconstructionError as error:
                        raise StateReconstructionAuditError(
                            f"cannot reconstruct {partition}:{line_number}: {error}"
                        ) from error

                    projected_rows += 1
                    partition_projected += 1
                    observation_fields = observation["fields"]
                    for name in OBSERVATION_FIELDS:
                        status = observation_fields[name]["status"]
                        fields[name][status] += 1
                        status_totals[status] += 1
        except (OSError, UnicodeError) as error:
            raise StateReconstructionAuditError(
                f"cannot stream compact partition {partition}: {error}"
            ) from error

        partition_reports.append(
            {
                "partition": str(partition),
                "compressed_bytes": partition_bytes,
                "decision_rows": partition_rows,
                "observations_projected": partition_projected,
                "future_cutoff_failures": partition_future_failures,
            }
        )

    output = manifest.get("output")
    output = output if isinstance(output, Mapping) else {}
    declared_rows = output.get("decision_count")
    declared_rows = declared_rows if isinstance(declared_rows, int) else None
    expected_field_cells = projected_rows * len(OBSERVATION_FIELDS)
    counted_field_cells = sum(status_totals.values())
    row_count_matches = declared_rows is None or declared_rows == total_rows
    field_counts_match = counted_field_cells == expected_field_cells
    status = (
        "ok"
        if future_cutoff_failures == 0 and row_count_matches and field_counts_match
        else "failed"
    )
    return {
        "schema": SCHEMA,
        "generated_at": _utc_now(),
        "status": status,
        "input": {
            "manifest": str(path),
            "partition_count": len(partition_reports),
            "compressed_bytes": compressed_bytes,
            "declared_decision_rows": declared_rows,
            "partitions": partition_reports,
        },
        "coverage": {
            "decision_rows": total_rows,
            "observations_projected": projected_rows,
            "field_count": len(OBSERVATION_FIELDS),
            "field_status_counts": fields,
            "status_totals": status_totals,
            "stance": dict(fields["stance"]),
        },
        "quality": {
            "future_cutoff_failures": future_cutoff_failures,
            "row_count_matches_manifest": row_count_matches,
            "field_cell_count": counted_field_cells,
            "expected_field_cell_count": expected_field_cells,
            "field_cell_count_matches": field_counts_match,
            "line_level_output_materialized": False,
        },
    }


def write_state_reconstruction_audit(
    report: Mapping[str, Any], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, UnicodeError) as error:
        raise StateReconstructionAuditError(
            f"cannot write state reconstruction audit {path}: {error}"
        ) from error
    return path


def audit_state_reconstruction(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> tuple[dict[str, Any], Path]:
    report = build_state_reconstruction_audit(manifest_path)
    return report, write_state_reconstruction_audit(report, output_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, output = audit_state_reconstruction(
            args.dataset_manifest,
            output_path=args.output,
        )
    except StateReconstructionAuditError as error:
        print(f"State reconstruction audit failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output),
                "decision_rows": report["coverage"]["decision_rows"],
                "stance": report["coverage"]["stance"],
                "future_cutoff_failures": report["quality"][
                    "future_cutoff_failures"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "ok" else 1


__all__ = [
    "DEFAULT_MANIFEST",
    "DEFAULT_OUTPUT",
    "SCHEMA",
    "StateReconstructionAuditError",
    "audit_state_reconstruction",
    "build_state_reconstruction_audit",
    "main",
    "write_state_reconstruction_audit",
]


if __name__ == "__main__":
    raise SystemExit(main())
