"""Read-only quality audit for the local Chronicle All Activity dataset.

Only reports under ``offline_data/reports`` are written.  A CSV can prove that
an event type has rows, but not which Chronicle web streams were selected, so a
zero count is reported as ``empty_or_unobserved`` rather than unavailable.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence
import uuid

from .import_chronicle_csv import OFFICIAL_COLUMNS


AUDIT_SCHEMA_VERSION = 1
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "offline_data"
DEFAULT_REPORT_STEM = "chronicle_dataset_quality"
QUEUE_RELATIVE_PATH = Path("chronicle_raw") / "export_queue.json"
UUID_TEXT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
RAW_FILENAME = re.compile(r"^all-activity-(?P<alias>.+)\.csv$", re.IGNORECASE)

# Current short export names plus actual legacy/fixture spellings.
FAMILY_TYPE_ALIASES: dict[str, frozenset[str]] = {
    "DMG": frozenset(("DMG", "DAMAGE", "SPELL_DAMAGE")),
    "GO": frozenset(("GO", "CAST", "SPELL_GO")),
    "START": frozenset(("START", "SPELL_START")),
    "RES": frozenset(("RES", "RESOURCE", "RESOURCE_CHANGE")),
    "AURA": frozenset(("AURA",)),
    "INFO": frozenset(("INFO", "COMBATANT_INFO")),
    "CONS": frozenset(("CONS", "CONSUME")),
    "CLASS": frozenset(("CLASS", "UNIT_CLASSIFICATION")),
}
KEY_FAMILIES = tuple(FAMILY_TYPE_ALIASES)
GRADE_A_REQUIRED = frozenset(KEY_FAMILIES)
GRADE_B_REQUIRED = frozenset(("DMG", "GO", "START", "RES", "AURA", "CLASS"))


class DatasetAuditError(RuntimeError):
    """The dataset root or queue cannot be audited."""


@dataclass(frozen=True)
class AuditResult:
    report: dict[str, Any]
    json_report: Path
    markdown_report: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DatasetAuditError(f"{label} does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetAuditError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise DatasetAuditError(f"{label} is not a JSON object: {path}")
    return value


def _uuid_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not UUID_TEXT.fullmatch(text):
        return None
    return str(uuid.UUID(text))


def _append_alias(aliases: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in aliases:
        aliases.append(text)


def _alias_from_raw_name(value: Any) -> str | None:
    if not value:
        return None
    match = RAW_FILENAME.match(Path(str(value)).name)
    return match.group("alias") if match is not None else None


def _alias_from_normalized_name(value: Any) -> str | None:
    if not value:
        return None
    name = Path(str(value)).name
    return name.split("__", 1)[0] if "__" in name else None


def _entry_aliases(entry: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    receipt = entry.get("import_receipt")
    if not isinstance(receipt, dict):
        receipt = {}
    for value in (
        entry.get("instance_id"),
        entry.get("slug"),
        entry.get("download_instance_id"),
        receipt.get("instance"),
        _alias_from_raw_name(receipt.get("raw_copy")),
        _alias_from_normalized_name(receipt.get("normalized")),
    ):
        _append_alias(aliases, value)
    return aliases


def _provenance_aliases(value: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for candidate in (
        value.get("instance"),
        _alias_from_raw_name(value.get("raw_copy")),
        _alias_from_normalized_name(value.get("normalized")),
    ):
        _append_alias(aliases, candidate)
    return aliases


def _candidate_paths(
    value: Any,
    *,
    data_root: Path,
    fallback_directory: Path | None = None,
) -> list[Path]:
    text = str(value or "").strip()
    if not text:
        return []
    supplied = Path(text).expanduser()
    candidates = [supplied if supplied.is_absolute() else data_root / supplied]
    if fallback_directory is not None:
        fallback = data_root / fallback_directory / supplied.name
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path.resolve()
    return None


def _display_path(path: Path | None, data_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(data_root).as_posix()
    except ValueError:
        return str(path)


def _load_provenance_records(data_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    issues: list[str] = []
    for path in sorted((data_root / "chronicle_raw").rglob("provenance.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            issues.append(f"unreadable provenance {_display_path(path, data_root)}: {error}")
            continue
        if not isinstance(value, dict):
            issues.append(
                f"provenance is not an object: {_display_path(path, data_root)}"
            )
            continue
        records.append(
            {"path": path.resolve(), "value": value, "aliases": _provenance_aliases(value)}
        )
    return records, issues


def _select_provenance(
    entry: dict[str, Any],
    aliases: list[str],
    records: list[dict[str, Any]],
    data_root: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    issues: list[str] = []
    receipt = entry.get("import_receipt")
    if not isinstance(receipt, dict):
        receipt = {}
    receipt_path = _first_existing(
        _candidate_paths(receipt.get("metadata"), data_root=data_root)
    )
    if receipt_path is not None:
        selected = next(
            (record for record in records if record["path"] == receipt_path), None
        )
        if selected is not None:
            return selected, issues
        try:
            value = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return None, [f"unreadable receipt provenance: {error}"]
        if isinstance(value, dict):
            return {
                "path": receipt_path,
                "value": value,
                "aliases": _provenance_aliases(value),
            }, issues
        return None, ["receipt provenance is not a JSON object"]

    alias_set = set(aliases)
    matches = [record for record in records if alias_set.intersection(record["aliases"])]
    if len(matches) == 1:
        return matches[0], issues
    if len(matches) > 1:
        issues.append(
            "multiple provenance files match queue aliases: "
            + ", ".join(_display_path(record["path"], data_root) or "" for record in matches)
        )
    return None, issues


def _artifact_from_candidates(
    candidates: Iterable[Path], data_root: Path
) -> tuple[Path | None, dict[str, Any]]:
    candidate_list = list(dict.fromkeys(candidates))
    selected = _first_existing(candidate_list)
    reported = selected or (candidate_list[0] if candidate_list else None)
    return selected, {
        "path": _display_path(reported, data_root),
        "exists": selected is not None,
        "size_bytes": selected.stat().st_size if selected is not None else None,
    }


def _glob_alias_artifacts(
    directory: Path, aliases: Iterable[str], pattern_suffix: str
) -> list[Path]:
    matches: list[Path] = []
    for alias in aliases:
        for path in sorted(directory.glob(f"{alias}{pattern_suffix}")):
            resolved = path.resolve()
            if resolved not in matches:
                matches.append(resolved)
    return matches


def _prepare_instance(
    entry: dict[str, Any],
    *,
    queue_index: int,
    records: list[dict[str, Any]],
    data_root: Path,
) -> dict[str, Any]:
    issues: list[str] = []
    aliases = _entry_aliases(entry)
    provenance_record, provenance_issues = _select_provenance(
        entry, aliases, records, data_root
    )
    issues.extend(provenance_issues)
    provenance = provenance_record["value"] if provenance_record is not None else {}
    if provenance_record is not None:
        for alias in provenance_record["aliases"]:
            _append_alias(aliases, alias)
    receipt = entry.get("import_receipt")
    if not isinstance(receipt, dict):
        receipt = {}

    normalized_candidates = _candidate_paths(
        receipt.get("normalized"),
        data_root=data_root,
        fallback_directory=Path("normalized"),
    )
    normalized_candidates.extend(
        _candidate_paths(
            provenance.get("normalized"),
            data_root=data_root,
            fallback_directory=Path("normalized"),
        )
    )
    normalized_candidates.extend(
        _glob_alias_artifacts(data_root / "normalized", aliases, "__*.jsonl")
    )
    normalized_path, normalized_artifact = _artifact_from_candidates(
        normalized_candidates, data_root
    )

    raw_candidates = _candidate_paths(receipt.get("raw_copy"), data_root=data_root)
    raw_candidates.extend(
        _candidate_paths(provenance.get("raw_copy"), data_root=data_root)
    )
    raw_path, raw_artifact = _artifact_from_candidates(raw_candidates, data_root)
    provenance_path = provenance_record["path"] if provenance_record is not None else None
    provenance_artifact = {
        "path": _display_path(provenance_path, data_root),
        "exists": provenance_path is not None,
        "size_bytes": provenance_path.stat().st_size if provenance_path is not None else None,
    }

    canonical_candidates = (
        ("download_instance_id", entry.get("download_instance_id")),
        ("import_receipt.instance", receipt.get("instance")),
        ("provenance.instance", provenance.get("instance")),
        ("raw_filename", _alias_from_raw_name(raw_path)),
        ("normalized_filename", _alias_from_normalized_name(normalized_path)),
        ("queue.instance_id", entry.get("instance_id")),
        ("queue.slug", entry.get("slug")),
    )
    canonical_uuid: str | None = None
    canonical_source: str | None = None
    for source, candidate in canonical_candidates:
        canonical_uuid = _uuid_text(candidate)
        if canonical_uuid is not None:
            canonical_source = source
            _append_alias(aliases, canonical_uuid)
            break

    if canonical_uuid is None:
        issues.append("no canonical download UUID can be resolved from queue or artifacts")
    if normalized_path is None:
        issues.append("normalized JSONL is missing")
    if raw_path is None:
        issues.append("canonical preserved raw CSV is missing")
    if provenance_path is None:
        issues.append("provenance.json is missing")

    raw_header: dict[str, Any] = {
        "valid_official_columns": False,
        "columns": [],
        "error": None,
    }
    if raw_path is not None:
        try:
            with raw_path.open("r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), None)
            raw_header["columns"] = header or []
            raw_header["valid_official_columns"] = bool(
                header is not None and all(column in header for column in OFFICIAL_COLUMNS)
            )
            if not raw_header["valid_official_columns"]:
                issues.append("raw CSV is missing one or more official columns")
        except (OSError, csv.Error, UnicodeError) as error:
            raw_header["error"] = str(error)
            issues.append(f"cannot read raw CSV header: {error}")

    return {
        "queue_index": queue_index,
        "queue_instance_id": str(entry.get("instance_id") or ""),
        "slug": str(entry.get("slug") or "") or None,
        "canonical_instance_uuid": canonical_uuid,
        "canonical_uuid_source": canonical_source,
        "aliases": aliases,
        "queue_status": entry.get("status"),
        "event_stream_selection_claim": entry.get("event_stream_selection"),
        "stream_selection_evidence": "not_machine_verifiable_from_all_activity_csv",
        "artifacts": {
            "raw_csv": raw_artifact,
            "normalized_jsonl": normalized_artifact,
            "provenance_json": provenance_artifact,
        },
        "raw_header": raw_header,
        "receipt_row_count": receipt.get("row_count"),
        "provenance_row_count": provenance.get("row_count"),
        "normalized_path_internal": str(normalized_path) if normalized_path else None,
        "issues": issues,
    }


def _scan_normalized(path_text: str) -> dict[str, Any]:
    """Count every normalized row and exact event type in one worker process."""

    path = Path(path_text)
    counts: Counter[str] = Counter()
    row_count = 0
    invalid_json_rows = 0
    missing_type_rows = 0
    examples: list[str] = []
    try:
        with path.open("r", encoding="utf-8", buffering=1024 * 1024) as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                row_count += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    invalid_json_rows += 1
                    if len(examples) < 5:
                        examples.append(f"line {line_number}: invalid JSON: {error.msg}")
                    continue
                if not isinstance(row, dict):
                    invalid_json_rows += 1
                    if len(examples) < 5:
                        examples.append(f"line {line_number}: row is not an object")
                    continue
                event_type = row.get("type")
                if not isinstance(event_type, str) or not event_type.strip():
                    missing_type_rows += 1
                    if len(examples) < 5:
                        examples.append(f"line {line_number}: missing event type")
                    continue
                counts[event_type] += 1
    except (OSError, UnicodeError) as error:
        return {
            "path": path_text,
            "row_count": row_count,
            "event_type_counts": dict(counts),
            "invalid_json_rows": invalid_json_rows,
            "missing_type_rows": missing_type_rows,
            "examples": examples,
            "scan_error": str(error),
        }
    return {
        "path": path_text,
        "row_count": row_count,
        "event_type_counts": dict(sorted(counts.items())),
        "invalid_json_rows": invalid_json_rows,
        "missing_type_rows": missing_type_rows,
        "examples": examples,
        "scan_error": None,
    }


def _scan_paths(paths: list[str], workers: int) -> dict[str, dict[str, Any]]:
    if workers == 1 or len(paths) <= 1:
        return {path: _scan_normalized(path) for path in paths}
    results: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_scan_normalized, path): path for path in paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                results[path] = future.result()
            except Exception as error:  # process failures belong in the report
                results[path] = {
                    "path": path,
                    "row_count": 0,
                    "event_type_counts": {},
                    "invalid_json_rows": 0,
                    "missing_type_rows": 0,
                    "examples": [],
                    "scan_error": f"worker failed: {error}",
                }
    return results


def _event_family(event_type: str) -> str | None:
    normalized = event_type.strip().upper()
    for family, aliases in FAMILY_TYPE_ALIASES.items():
        if normalized in aliases:
            return family
    return None


def _family_counts(type_counts: dict[str, int]) -> dict[str, int]:
    counts = {family: 0 for family in KEY_FAMILIES}
    for event_type, count in type_counts.items():
        family = _event_family(event_type)
        if family is not None:
            counts[family] += int(count)
    return counts


def _grade(family_counts: dict[str, int]) -> tuple[str, str, str]:
    observed = {family for family, count in family_counts.items() if count > 0}
    if GRADE_A_REQUIRED.issubset(observed):
        return (
            "A",
            "all roadmap Grade A families are observed",
            "event_family_coverage_only",
        )
    if GRADE_B_REQUIRED.issubset(observed):
        missing = sorted(GRADE_A_REQUIRED.difference(observed))
        return (
            "B",
            "rotation families are observed; missing " + ", ".join(missing),
            "rotation_behavior_cloning",
        )
    return (
        "C",
        "insufficient state families for Grade A/B; retain for damage/cast mechanics",
        "mechanics_statistics_only",
    )


def _finalize_instance(instance: dict[str, Any], scan: dict[str, Any] | None) -> None:
    if scan is None:
        scan = {
            "row_count": 0,
            "event_type_counts": {},
            "invalid_json_rows": 0,
            "missing_type_rows": 0,
            "examples": [],
            "scan_error": "normalized JSONL is missing",
        }
    type_counts = {
        str(key): int(value)
        for key, value in sorted(scan.get("event_type_counts", {}).items())
    }
    family_counts = _family_counts(type_counts)
    grade, grade_basis, use = _grade(family_counts)
    row_count = int(scan.get("row_count") or 0)
    instance["normalized_scan"] = {
        "row_count": row_count,
        "event_type_counts": type_counts,
        "invalid_json_rows": int(scan.get("invalid_json_rows") or 0),
        "missing_type_rows": int(scan.get("missing_type_rows") or 0),
        "examples": list(scan.get("examples") or []),
        "scan_error": scan.get("scan_error"),
    }
    instance["families"] = {
        family: {
            "row_count": family_counts[family],
            "status": "observed"
            if family_counts[family] > 0
            else "empty_or_unobserved",
        }
        for family in KEY_FAMILIES
    }
    instance["grade"] = grade
    instance["grade_basis"] = grade_basis
    instance["roadmap_use"] = use

    if scan.get("scan_error"):
        instance["issues"].append(f"normalized scan failed: {scan['scan_error']}")
    if scan.get("invalid_json_rows"):
        instance["issues"].append(
            f"normalized JSONL has {scan['invalid_json_rows']} invalid row(s)"
        )
    if scan.get("missing_type_rows"):
        instance["issues"].append(
            f"normalized JSONL has {scan['missing_type_rows']} row(s) without type"
        )
    for label, expected in (
        ("import receipt", instance.get("receipt_row_count")),
        ("provenance", instance.get("provenance_row_count")),
    ):
        if isinstance(expected, int) and expected != row_count:
            instance["issues"].append(
                f"{label} row_count {expected} != normalized row_count {row_count}"
            )


def _coverage(family_instances: dict[str, int], family: str, total: int) -> str:
    count = family_instances.get(family, 0)
    if count == total and total > 0:
        return "all_instances"
    if count > 0:
        return "partial_instances"
    return "none"


def _field_matrix(
    *, family_instances: dict[str, int], instance_count: int
) -> list[dict[str, Any]]:
    def family_field(
        field: str,
        family: str,
        status: str,
        evidence: str,
        supplement: str,
    ) -> dict[str, Any]:
        observed_instances = family_instances.get(family, 0)
        return {
            "field": field,
            "status": status if observed_instances else "MISSING",
            "evidence": evidence,
            "family": family,
            "instances_with_family": observed_instances,
            "instances_total": instance_count,
            "coverage": _coverage(family_instances, family, instance_count),
            "supplement": supplement,
        }

    rows = [
        family_field(
            "damage_value_and_outcome",
            "DMG",
            "OBSERVED",
            "DMG rows",
            "simulator models counterfactual outcomes",
        ),
        family_field(
            "server_spell_go",
            "GO",
            "OBSERVED",
            "GO rows",
            "O2O Logger links server event to client intent",
        ),
        family_field(
            "server_spell_start",
            "START",
            "OBSERVED",
            "START rows",
            "O2O Logger links server event to client intent",
        ),
        family_field(
            "resource_delta_including_rage_gain_or_loss",
            "RES",
            "OBSERVED",
            "RES rows; trajectory ETL must select player rage",
            "O2O Logger records absolute rage around the action",
        ),
        family_field(
            "absolute_rage",
            "RES",
            "MISSING",
            "RES supplies deltas but the export has no known/reset absolute rage anchor",
            "O2O Logger records absolute rage directly",
        ),
        family_field(
            "aura_application_removal_and_stacks",
            "AURA",
            "OBSERVED",
            "AURA rows",
            "O2O Logger supplies exact local duration snapshots",
        ),
        family_field(
            "combatant_info_partial_gear_and_talent_summary",
            "INFO",
            "OBSERVED",
            "INFO rows; actual exports provide only partial loadout and talent summaries",
            "SavedVariables calibration boundary records exact learned ranks and item links",
        ),
        family_field(
            "consume_record",
            "CONS",
            "OBSERVED",
            "CONS rows; the row is observed but some values are synthetic/projected",
            "O2O Logger client action plus server outcome disambiguates actual item use",
        ),
        family_field(
            "unit_classification",
            "CLASS",
            "OBSERVED",
            "CLASS rows",
            "target registry and live UnitClassification snapshot",
        ),
        family_field(
            "gcd_remaining",
            "GO",
            "MISSING",
            "cast events and GCD duration rules do not provide remaining state without an initial anchor and complete replay",
            "O2O Logger exact client snapshot and calibration task",
        ),
        family_field(
            "mainhand_offhand_swing_remaining",
            "DMG",
            "MISSING",
            "attack events do not identify hand assignment/reset state or remaining timers",
            "O2O Logger exact swing snapshots and calibration task",
        ),
        family_field(
            "cooldown_remaining",
            "GO",
            "MISSING",
            "cast events and cooldown durations do not provide remaining state without an initial anchor and complete replay",
            "O2O Logger exact cooldown snapshot and calibration task",
        ),
        {
            "field": "target_armor_after_debuffs",
            "status": "INFERRED",
            "evidence": "target identity plus AURA stacks and mechanics registry",
            "family": "AURA",
            "instances_with_family": family_instances.get("AURA", 0),
            "instances_total": instance_count,
            "coverage": _coverage(family_instances, "AURA", instance_count),
            "supplement": "non-dummy calibration, including zero-armor condition",
        },
    ]
    for field, supplement in (
        (
            "keypress",
            "raw key-down remains unavailable; O2O Logger SPELL_CAST_EVENT observes initiated client actions",
        ),
        ("queue_intent", "O2O Logger SPELL_QUEUE_EVENT"),
        (
            "queue_cancel",
            "O2O Logger queue popped codes plus the subsequent GO/result chain distinguish execution from cancellation",
        ),
        (
            "exact_talent_ranks_and_full_item_configuration",
            "O2O Logger calibration boundary captures learned talents and equipped item links",
        ),
        ("position", "live player/target position instrumentation where available"),
        ("range", "live range probe/calibration task"),
        ("movement", "O2O Logger movement state"),
        ("latency", "O2O Logger client-to-server event timing"),
        ("hidden_rng_state", "simulator seed; never Chronicle-observed"),
    ):
        rows.append(
            {
                "field": field,
                "status": "MISSING",
                "evidence": "not represented by All Activity CSV rows",
                "family": None,
                "instances_with_family": 0,
                "instances_total": instance_count,
                "coverage": "none",
                "supplement": supplement,
            }
        )
    return rows


def build_audit_report(
    data_root: str | Path = DEFAULT_DATA_ROOT,
    *,
    workers: int | None = None,
) -> dict[str, Any]:
    """Read the queue and all canonical artifacts, then return the audit object."""

    resolved_root = Path(data_root).expanduser().resolve()
    queue = _load_json_object(resolved_root / QUEUE_RELATIVE_PATH, "export queue")
    entries = queue.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise DatasetAuditError("export queue field 'entries' must be a list of objects")
    if workers is None:
        workers = min(8, os.cpu_count() or 1)
    if workers < 1:
        raise DatasetAuditError("workers must be at least 1")

    provenance_records, provenance_issues = _load_provenance_records(resolved_root)
    instances = [
        _prepare_instance(
            entry,
            queue_index=index,
            records=provenance_records,
            data_root=resolved_root,
        )
        for index, entry in enumerate(entries)
    ]
    scan_paths = sorted(
        {
            instance["normalized_path_internal"]
            for instance in instances
            if instance["normalized_path_internal"] is not None
        }
    )
    scans = _scan_paths(scan_paths, workers)
    for instance in instances:
        internal_path = instance.pop("normalized_path_internal")
        _finalize_instance(instance, scans.get(internal_path) if internal_path else None)

    total_type_counts: Counter[str] = Counter()
    total_family_counts: Counter[str] = Counter()
    family_instances: Counter[str] = Counter()
    grade_counts: Counter[str] = Counter()
    total_rows = 0
    invalid_rows = 0
    missing_type_rows = 0
    complete_artifacts = 0
    canonical_uuids = 0
    issue_instances = 0
    associated_provenance: set[str] = set()
    for instance in instances:
        scan = instance["normalized_scan"]
        total_rows += scan["row_count"]
        invalid_rows += scan["invalid_json_rows"]
        missing_type_rows += scan["missing_type_rows"]
        total_type_counts.update(scan["event_type_counts"])
        for family, availability in instance["families"].items():
            total_family_counts[family] += availability["row_count"]
            if availability["row_count"] > 0:
                family_instances[family] += 1
        grade_counts[instance["grade"]] += 1
        if all(artifact["exists"] for artifact in instance["artifacts"].values()):
            complete_artifacts += 1
        if instance["canonical_instance_uuid"] is not None:
            canonical_uuids += 1
        if instance["issues"]:
            issue_instances += 1
        provenance_path = instance["artifacts"]["provenance_json"]["path"]
        if provenance_path:
            associated_provenance.add(provenance_path)

    family_summary = {
        family: {
            "row_count": total_family_counts[family],
            "instances_observed": family_instances[family],
            "instances_empty_or_unobserved": len(instances) - family_instances[family],
            "zero_row_meaning": "empty_or_unobserved",
        }
        for family in KEY_FAMILIES
    }
    normalized_discovered = list((resolved_root / "normalized").glob("*.jsonl"))
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "chronicle_offline_dataset_quality_audit",
        "generated_at": _utc_now(),
        "data_root": str(resolved_root),
        "workers": workers,
        "evidence_boundary": {
            "event_type_counts": "exact full scan of every matched normalized JSONL row",
            "zero_family_rows": "empty_or_unobserved",
            "stream_selection": "not machine-verifiable from an exported All Activity CSV",
            "raw_inputs_modified": False,
        },
        "queue": {
            "path": QUEUE_RELATIVE_PATH.as_posix(),
            "schema_version": queue.get("schema_version"),
            "kind": queue.get("kind"),
            "entry_count": len(entries),
            "status_counts": dict(
                sorted(Counter(str(entry.get("status") or "missing") for entry in entries).items())
            ),
        },
        "artifact_inventory": {
            "queue_entries": len(entries),
            "provenance_files_discovered": len(provenance_records),
            "provenance_files_associated": len(associated_provenance),
            "normalized_files_discovered": len(normalized_discovered),
            "normalized_files_scanned": len(scan_paths),
            "instances_with_complete_canonical_artifacts": complete_artifacts,
            "canonical_uuid_resolved": canonical_uuids,
        },
        "summary": {
            "instance_count": len(instances),
            "normalized_row_count": total_rows,
            "invalid_json_rows": invalid_rows,
            "missing_type_rows": missing_type_rows,
            "instances_with_issues": issue_instances,
            "grade_counts": {grade: grade_counts[grade] for grade in ("A", "B", "C")},
            "event_type_counts": dict(sorted(total_type_counts.items())),
            "family_summary": family_summary,
        },
        "grade_rules": {
            "A": {
                "required_families": list(KEY_FAMILIES),
                "use": "event_family_coverage_only",
            },
            "B": {
                "required_families": [
                    family for family in KEY_FAMILIES if family in GRADE_B_REQUIRED
                ],
                "typical_gap": "INFO and/or CONS",
                "use": "rotation_behavior_cloning",
            },
            "C": {
                "rule": "does not meet Grade A or B",
                "use": "mechanics_statistics_only",
            },
        },
        "family_type_aliases": {
            family: sorted(aliases) for family, aliases in FAMILY_TYPE_ALIASES.items()
        },
        "field_provenance_matrix": _field_matrix(
            family_instances=dict(family_instances), instance_count=len(instances)
        ),
        "provenance_discovery_issues": provenance_issues,
        "instances": instances,
    }


def _markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    inventory = report["artifact_inventory"]
    lines = [
        "# Chronicle Offline Dataset Quality Audit",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Result",
        "",
        f"- Queue instances: {summary['instance_count']}",
        f"- Fully scanned normalized rows: {summary['normalized_row_count']}",
        "- Grades: "
        + ", ".join(f"{grade}={summary['grade_counts'][grade]}" for grade in ("A", "B", "C")),
        "- Complete raw/normalized/provenance triplets: "
        f"{inventory['instances_with_complete_canonical_artifacts']}/{summary['instance_count']}",
        "- Canonical download UUIDs resolved: "
        f"{inventory['canonical_uuid_resolved']}/{summary['instance_count']}",
        f"- Instances with audit issues: {summary['instances_with_issues']}",
        "",
        "## Evidence boundary",
        "",
        "Event-type counts below are exact full-file counts from normalized JSONL. "
        "A family with zero rows is only `empty_or_unobserved`: an All Activity CSV "
        "cannot prove that the corresponding Chronicle stream was selected, available, "
        "or genuinely empty. The audit does not modify queue, raw CSV, normalized JSONL, "
        "or provenance files.",
        "",
        "## Grade distribution",
        "",
        "| Grade | Instances | Roadmap use |",
        "|---|---:|---|",
        f"| A | {summary['grade_counts']['A']} | event-family coverage only; run reconstruction-readiness audit |",
        f"| B | {summary['grade_counts']['B']} | rotation behavior cloning |",
        f"| C | {summary['grade_counts']['C']} | mechanics statistics only |",
        "",
        "## Key event families",
        "",
        "| Family | Rows | Instances observed | Empty or unobserved |",
        "|---|---:|---:|---:|",
    ]
    for family in KEY_FAMILIES:
        item = summary["family_summary"][family]
        lines.append(
            f"| {family} | {item['row_count']} | {item['instances_observed']} | "
            f"{item['instances_empty_or_unobserved']} |"
        )

    lines.extend(
        [
            "",
            "## Field provenance matrix",
            "",
            "| Field | Status | Coverage | Evidence | Gap fill |",
            "|---|---|---|---|---|",
        ]
    )
    for row in report["field_provenance_matrix"]:
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    row["field"],
                    row["status"],
                    row["coverage"],
                    row["evidence"],
                    row["supplement"],
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Instances",
            "",
            "| Canonical UUID | Queue ID / slug aliases | Grade | Rows | Missing families | Artifacts | Issues |",
            "|---|---|---:|---:|---|---|---|",
        ]
    )
    for instance in report["instances"]:
        missing = [
            family
            for family, availability in instance["families"].items()
            if availability["status"] == "empty_or_unobserved"
        ]
        artifacts = instance["artifacts"]
        artifact_text = "/".join(
            "yes" if artifacts[name]["exists"] else "no"
            for name in ("raw_csv", "normalized_jsonl", "provenance_json")
        )
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    instance["canonical_instance_uuid"] or "unresolved",
                    ", ".join(instance["aliases"]),
                    instance["grade"],
                    instance["normalized_scan"]["row_count"],
                    ", ".join(missing) or "none",
                    artifact_text + " (raw/normalized/provenance)",
                    "; ".join(instance["issues"]) or "none",
                )
            )
            + " |"
        )
    if report["provenance_discovery_issues"]:
        lines.extend(("", "## Provenance discovery issues", ""))
        lines.extend(f"- {issue}" for issue in report["provenance_discovery_issues"])
    lines.append("")
    return "\n".join(lines)


def write_reports(
    report: dict[str, Any],
    *,
    data_root: str | Path,
    report_stem: str = DEFAULT_REPORT_STEM,
) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", report_stem):
        raise DatasetAuditError(
            "report stem may contain only letters, digits, dot, dash, underscore"
        )
    output_directory = Path(data_root).expanduser().resolve() / "reports"
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"{report_stem}.json"
    markdown_path = output_directory / f"{report_stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def audit_dataset(
    data_root: str | Path = DEFAULT_DATA_ROOT,
    *,
    workers: int | None = None,
    report_stem: str = DEFAULT_REPORT_STEM,
) -> AuditResult:
    report = build_audit_report(data_root, workers=workers)
    json_report, markdown_report = write_reports(
        report, data_root=data_root, report_stem=report_stem
    )
    return AuditResult(report, json_report, markdown_report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit Chronicle raw/normalized/provenance artifacts without modifying them."
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"offline data root (default: {DEFAULT_DATA_ROOT})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel normalized JSONL scanners (default: up to 8)",
    )
    parser.add_argument(
        "--report-stem",
        default=DEFAULT_REPORT_STEM,
        help=f"JSON/Markdown basename under reports (default: {DEFAULT_REPORT_STEM})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = audit_dataset(
            args.data_root, workers=args.workers, report_stem=args.report_stem
        )
    except DatasetAuditError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    receipt = {
        "status": "ok",
        "instances": result.report["summary"]["instance_count"],
        "rows": result.report["summary"]["normalized_row_count"],
        "grades": result.report["summary"]["grade_counts"],
        "json_report": str(result.json_report),
        "markdown_report": str(result.markdown_report),
    }
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
