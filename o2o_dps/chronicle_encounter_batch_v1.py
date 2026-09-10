"""Resume-safe, disk-light Chronicle encounter reconstruction batch.

The batch reads local normalized JSONL files one at a time. It writes the
existing V1 reconstruction and Fury scenario-catalog schemas as compact gzip
JSON, so downstream code receives the same evidence contract without another
copy of source rows or a corpus-sized checkpoint.

Resume uses source size/mtime plus the explicit transformation contract. A
content hash would require rereading the large source merely to decide whether
it can be skipped, so it is deliberately not part of this local workflow.
The one supported legacy contract upgrade validates compact feature reports;
it never rescans normalized JSONL merely to decide whether reuse is safe.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import statistics
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence

from .chronicle_encounter_reconstruction_v1 import (
    DEFAULT_ACTIVE_TOLERANCE_MS,
    DEFAULT_COMBAT_GAP_MS,
    KNOWN_PLAYER_CONTROLLED_SUMMON_ENTRY_IDS,
    ReconstructionError,
    reconstruct_file,
)
from .fury_encounter_scenarios_v1 import (
    ScenarioCompileError,
    compile_fury_encounter_scenarios,
)


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "offline_data" / "normalized"
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "offline_data" / "derived" / "chronicle_encounter_batch" / "v1"
)
DEFAULT_BASE_REQUEST = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
MANIFEST_NAME = "manifest.json"
MANIFEST_KIND = "chronicle_encounter_batch_manifest_v1"
IMPLEMENTATION_REVISION = "v1.1_interleaved_encounters_numeric_dead"
KNOWN_SUMMON_CONTRACT_KEY = "known_player_controlled_summon_entry_ids"


class ChronicleEncounterBatchError(RuntimeError):
    """The batch configuration or one of its persisted artifacts is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleEncounterBatchError(f"{label} must be a JSON object")
    return value


def load_json_document(path: Path) -> JSONMap:
    """Load one JSON object from plain JSON or a ``.gz`` artifact."""

    resolved = path.expanduser().resolve()
    try:
        if resolved.suffix.casefold() == ".gz":
            with gzip.open(resolved, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChronicleEncounterBatchError(f"could not read JSON artifact {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise ChronicleEncounterBatchError(f"JSON artifact is not an object: {resolved}")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_write_json_gzip(
    path: Path,
    value: Mapping[str, Any],
    *,
    compresslevel: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{time.time_ns()}.tmp"
    try:
        with gzip.open(
            temporary,
            mode="wt",
            encoding="utf-8",
            newline="\n",
            compresslevel=compresslevel,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_signature(path: Path) -> JSONMap:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _same_source_signature(left: Any, right: Mapping[str, Any]) -> bool:
    if not isinstance(left, Mapping):
        return False
    return all(left.get(key) == right.get(key) for key in ("path", "size_bytes", "mtime_ns"))


def discover_normalized_files(input_dir: Path, pattern: str = "*.jsonl") -> list[Path]:
    resolved = input_dir.expanduser().resolve()
    if not resolved.is_dir():
        raise ChronicleEncounterBatchError(f"normalized input directory does not exist: {resolved}")
    paths = [path.resolve() for path in resolved.glob(pattern) if path.is_file()]
    paths.sort(key=lambda path: (path.stat().st_size, path.name.casefold()))
    if not paths:
        raise ChronicleEncounterBatchError(
            f"no local normalized JSONL files match {pattern!r} in {resolved}"
        )
    return paths


def _resolve_forced_sources(
    sources: Sequence[Path],
    input_dir: Path,
    selectors: Sequence[str | Path],
) -> set[Path]:
    """Resolve exact source names/paths without reading any normalized rows."""

    resolved: set[Path] = set()
    by_name = {source.name: source for source in sources}
    by_path = {source.resolve(): source for source in sources}
    for raw in selectors:
        token = str(raw).strip()
        if not token:
            raise ValueError("force source selectors must not be empty")
        candidate = Path(token).expanduser()
        source: Path | None = None
        if candidate.is_absolute():
            source = by_path.get(candidate.resolve())
        else:
            source = by_name.get(token)
            if source is None:
                source = by_path.get((input_dir / candidate).resolve())
        if source is None:
            raise ChronicleEncounterBatchError(
                f"forced source does not match a discovered normalized file: {token}"
            )
        resolved.add(source)
    return resolved


def _output_paths(output_dir: Path, source: Path) -> tuple[Path, Path]:
    stem = source.stem
    return (
        output_dir / "features" / f"{stem}.reconstruction.json.gz",
        output_dir / "catalogs" / f"{stem}.catalog.json.gz",
    )


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _artifact_contract(
    *,
    base_request_path: Path,
    combat_gap_ms: int,
    active_tolerance_ms: int,
    max_encounters: int | None,
    armor_hypotheses: Sequence[int | float],
    level_hypotheses: Sequence[int],
    gzip_level: int,
) -> JSONMap:
    return {
        "implementation_revision": IMPLEMENTATION_REVISION,
        "reconstruction_kind": "chronicle_encounter_reconstruction_v1",
        "catalog_kind": "fury_encounter_scenario_catalog_v1",
        "base_request": _source_signature(base_request_path),
        "combat_gap_ms": combat_gap_ms,
        "active_tolerance_ms": active_tolerance_ms,
        "max_encounters_per_file": max_encounters,
        "armor_hypotheses": list(armor_hypotheses),
        "level_hypotheses": list(level_hypotheses),
        "gzip_level": gzip_level,
        "raw_rows_copied": False,
        KNOWN_SUMMON_CONTRACT_KEY: sorted(KNOWN_PLAYER_CONTROLLED_SUMMON_ENTRY_IDS),
    }


def _is_legacy_contract_missing_known_summons(
    previous: Any,
    current: Mapping[str, Any],
) -> bool:
    """Accept only the one historical contract shape predating the summon field."""

    if not isinstance(previous, Mapping) or KNOWN_SUMMON_CONTRACT_KEY in previous:
        return False
    upgraded = dict(previous)
    upgraded[KNOWN_SUMMON_CONTRACT_KEY] = current.get(KNOWN_SUMMON_CONTRACT_KEY)
    return upgraded == current


def _legacy_feature_is_safe_for_known_summons(
    feature: Mapping[str, Any],
    excluded_entry_ids: Sequence[int],
) -> bool:
    """Prove an old small feature report contains no newly excluded target."""

    excluded = {
        value
        for value in excluded_entry_ids
        if isinstance(value, int) and not isinstance(value, bool)
    }
    encounters = feature.get("encounters")
    if not isinstance(encounters, list):
        return False
    for encounter in encounters:
        if not isinstance(encounter, Mapping):
            return False
        waves = encounter.get("waves")
        if not isinstance(waves, list):
            return False
        for wave in waves:
            if not isinstance(wave, Mapping):
                return False
            targets = wave.get("targets")
            if not isinstance(targets, list):
                return False
            for target in targets:
                if not isinstance(target, Mapping):
                    return False
                if target.get("creature_entry_id") in excluded:
                    return False
    return True


def _pending_entry(source: Path) -> JSONMap:
    return {
        "source": _source_signature(source),
        "processing_status": "PENDING",
        "attempt_count": 0,
        "outputs": None,
        "reconstruction_summary": None,
        "catalog_summary": None,
        "elapsed_seconds": None,
        "last_error": None,
    }


def _entry_output_paths(entry: Mapping[str, Any], output_dir: Path) -> tuple[Path, Path] | None:
    outputs = entry.get("outputs")
    if not isinstance(outputs, Mapping):
        return None
    feature = outputs.get("feature_report")
    catalog = outputs.get("scenario_catalog")
    if not isinstance(feature, Mapping) or not isinstance(catalog, Mapping):
        return None
    feature_path = feature.get("path")
    catalog_path = catalog.get("path")
    if not isinstance(feature_path, str) or not isinstance(catalog_path, str):
        return None
    return output_dir / feature_path, output_dir / catalog_path


def _load_completed_pair(
    entry: Mapping[str, Any],
    output_dir: Path,
    source: Path,
) -> tuple[JSONMap, JSONMap] | None:
    paths = _entry_output_paths(entry, output_dir)
    if paths is None:
        return None
    feature_path, catalog_path = paths
    if not feature_path.is_file() or not catalog_path.is_file():
        return None
    try:
        feature = load_json_document(feature_path)
        catalog = load_json_document(catalog_path)
    except ChronicleEncounterBatchError:
        return None
    if feature.get("kind") != "chronicle_encounter_reconstruction_v1":
        return None
    if catalog.get("kind") != "fury_encounter_scenario_catalog_v1":
        return None
    feature_source = feature.get("source")
    if not isinstance(feature_source, Mapping):
        return None
    normalized_file = feature_source.get("normalized_file")
    if not isinstance(normalized_file, str) or Path(normalized_file).resolve() != source.resolve():
        return None
    return feature, catalog


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _global_npc_identity_aggregates(
    reports: Iterable[tuple[str, Mapping[str, Any]]],
) -> list[JSONMap]:
    samples: dict[str, list[float]] = defaultdict(list)
    names: dict[str, set[str]] = defaultdict(set)
    source_files: dict[str, set[str]] = defaultdict(set)
    entry_ids: dict[str, int | None] = {}
    for source_name, report in reports:
        encounters = report.get("encounters")
        if not isinstance(encounters, list):
            continue
        for encounter in encounters:
            if not isinstance(encounter, Mapping):
                continue
            waves = encounter.get("waves")
            if not isinstance(waves, list):
                continue
            for wave in waves:
                if not isinstance(wave, Mapping):
                    continue
                targets = wave.get("targets")
                if not isinstance(targets, list):
                    continue
                for target in targets:
                    if not isinstance(target, Mapping):
                        continue
                    proxy = target.get("kill_budget_proxy")
                    if not isinstance(proxy, Mapping):
                        continue
                    value = proxy.get("value")
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) <= 0
                        or proxy.get("completeness")
                        != "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS"
                    ):
                        continue
                    entry_id = target.get("creature_entry_id")
                    target_name = target.get("target_name")
                    name = target_name if isinstance(target_name, str) and target_name else None
                    if isinstance(entry_id, int) and not isinstance(entry_id, bool):
                        key = f"entry:{entry_id}"
                        entry_ids[key] = entry_id
                    elif name and not name.casefold().startswith("0x"):
                        key = f"name:{name.casefold()}"
                        entry_ids[key] = None
                    else:
                        continue
                    samples[key].append(float(value))
                    source_files[key].add(source_name)
                    if name and not name.casefold().startswith("0x"):
                        names[key].add(name)

    result: list[JSONMap] = []
    for key in sorted(samples):
        values = sorted(samples[key])
        stats = {
            "count": len(values),
            "source_file_count": len(source_files[key]),
            "minimum": _render_number(values[0]),
            "median": _render_number(float(statistics.median(values))),
            "q1": _render_number(_percentile(values, 0.25)),
            "q3": _render_number(_percentile(values, 0.75)),
            "maximum": _render_number(values[-1]),
            "status": "RECONSTRUCTED",
            "basis": "complete kill-budget proxies in completed per-file feature reports",
        }
        if len(values) >= 2:
            health: JSONMap = {
                "status": "INFERRED",
                "center": stats["median"],
                "range": [stats["q1"], stats["q3"]],
                "method": "median and IQR of repeated observed kill-budget proxies",
                "not_equal_to": "exact NPC health distribution",
            }
        else:
            health = {
                "status": "MISSING",
                "center": None,
                "range": None,
                "reason": "one observation cannot establish a repeatable health scenario",
            }
        result.append(
            {
                "identity": {
                    "creature_entry_id": entry_ids[key],
                    "target_name": sorted(names[key])[0] if names[key] else None,
                    "target_name_variants": sorted(names[key]),
                    "classification": "Hostile Creature",
                    "status": "RECONSTRUCTED" if entry_ids[key] is not None else "INFERRED",
                },
                "kill_budget_proxy_summary": stats,
                "health_scenario": health,
            }
        )
    return result


def _aggregate_summary(entries: Sequence[Mapping[str, Any]]) -> JSONMap:
    completed = [entry for entry in entries if entry.get("processing_status") == "COMPLETED"]
    failed = [entry for entry in entries if entry.get("processing_status") == "FAILED"]
    pending = [entry for entry in entries if entry.get("processing_status") == "PENDING"]
    reconstruction_keys = (
        "encounter_count",
        "source_rows_included",
        "wave_count",
        "target_observation_count",
    )
    catalog_keys = ("encounter_count", "wave_count", "scenario_count")
    reconstruction_totals = {
        key: sum(
            int(entry.get("reconstruction_summary", {}).get(key, 0))
            for entry in completed
            if isinstance(entry.get("reconstruction_summary"), Mapping)
        )
        for key in reconstruction_keys
    }
    catalog_totals = {
        key: sum(
            int(entry.get("catalog_summary", {}).get(key, 0))
            for entry in completed
            if isinstance(entry.get("catalog_summary"), Mapping)
        )
        for key in catalog_keys
    }
    variants: dict[str, int] = defaultdict(int)
    for entry in completed:
        summary = entry.get("catalog_summary")
        if not isinstance(summary, Mapping):
            continue
        raw = summary.get("variant_counts")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    variants[str(key)] += value
    return {
        "discovered_file_count": len(entries),
        "completed_file_count": len(completed),
        "failed_file_count": len(failed),
        "pending_file_count": len(pending),
        "reconstruction_totals": reconstruction_totals,
        "catalog_totals": {**catalog_totals, "variant_counts": dict(sorted(variants.items()))},
    }


def _storage_projection(
    entries: Sequence[Mapping[str, Any]],
    *,
    discovered_source_bytes: int,
    max_encounters: int | None,
) -> JSONMap:
    completed = [entry for entry in entries if entry.get("processing_status") == "COMPLETED"]
    completed_source_bytes = sum(
        int(entry.get("source", {}).get("size_bytes", 0))
        for entry in completed
        if isinstance(entry.get("source"), Mapping)
    )
    completed_output_bytes = 0
    for entry in completed:
        outputs = entry.get("outputs")
        if not isinstance(outputs, Mapping):
            continue
        for label in ("feature_report", "scenario_catalog"):
            value = outputs.get(label)
            if isinstance(value, Mapping):
                completed_output_bytes += int(value.get("size_bytes", 0))
    common: JSONMap = {
        "completed_source_bytes": completed_source_bytes,
        "completed_compressed_output_bytes": completed_output_bytes,
        "discovered_source_bytes": discovered_source_bytes,
        "manifest_bytes_excluded": True,
    }
    if max_encounters is not None:
        return {
            **common,
            "projected_full_corpus_output_bytes": None,
            "status": "MISSING",
            "reason": "bounded encounter prefixes do not identify complete-file output density",
        }
    if completed_source_bytes <= 0:
        return {
            **common,
            "projected_full_corpus_output_bytes": None,
            "status": "MISSING",
            "reason": "no complete source file has been processed",
        }
    ratio = completed_output_bytes / completed_source_bytes
    return {
        **common,
        "compressed_output_to_source_ratio": ratio,
        "projected_full_corpus_output_bytes": round(discovered_source_bytes * ratio),
        "status": "INFERRED",
        "method": "linear scaling from completed whole-file compressed outputs",
        "limitation": "encounter and target density can differ between raid files",
    }


def _runtime_projection(
    entries: Sequence[Mapping[str, Any]],
    *,
    discovered_source_bytes: int,
    max_encounters: int | None,
) -> JSONMap:
    completed = [entry for entry in entries if entry.get("processing_status") == "COMPLETED"]
    source_bytes = sum(
        int(entry.get("source", {}).get("size_bytes", 0))
        for entry in completed
        if isinstance(entry.get("source"), Mapping)
    )
    elapsed_seconds = sum(
        float(entry.get("elapsed_seconds", 0) or 0) for entry in completed
    )
    common: JSONMap = {
        "completed_source_bytes": source_bytes,
        "completed_elapsed_seconds": elapsed_seconds,
        "discovered_source_bytes": discovered_source_bytes,
    }
    if max_encounters is not None:
        return {
            **common,
            "projected_serial_seconds": None,
            "status": "MISSING",
            "reason": "bounded encounter runs are not whole-file runtime samples",
        }
    if source_bytes <= 0 or elapsed_seconds <= 0:
        return {
            **common,
            "projected_serial_seconds": None,
            "status": "MISSING",
            "reason": "no complete source file has a positive runtime observation",
        }
    return {
        **common,
        "projected_serial_seconds": discovered_source_bytes * elapsed_seconds / source_bytes,
        "status": "INFERRED",
        "method": "linear scaling from completed whole-file serial runtime",
        "limitation": "JSON parsing and encounter density can differ between files",
    }


def _manifest_document(
    *,
    input_dir: Path,
    output_dir: Path,
    pattern: str,
    artifact_contract: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    selected_file_count: int,
    forced_source_names: Sequence[str],
    npc_aggregates: Sequence[Mapping[str, Any]] | None,
) -> JSONMap:
    discovered_source_bytes = sum(
        int(entry.get("source", {}).get("size_bytes", 0))
        for entry in entries
        if isinstance(entry.get("source"), Mapping)
    )
    return {
        "schema_version": 1,
        "kind": MANIFEST_KIND,
        "updated_at": _utc_now(),
        "source": {
            "normalized_directory": str(input_dir),
            "pattern": pattern,
            "mode": "local_read_only_one_file_at_a_time",
            "source_rows_copied": False,
            "source_checkpoint_written": False,
        },
        "output": {
            "directory": str(output_dir),
            "per_file_encoding": "gzip JSON with compact separators",
            "feature_schema": "chronicle_encounter_reconstruction_v1",
            "catalog_schema": "fury_encounter_scenario_catalog_v1",
        },
        "artifact_contract": dict(artifact_contract),
        "provenance_contract": {
            "OBSERVED": "direct normalized Chronicle row or arithmetic sum of those rows",
            "RECONSTRUCTED": "deterministic transform with declared thresholds",
            "INFERRED": "explicit model or sensitivity assumption, never a direct log field",
            "MISSING": "not identifiable from the normalized stream",
            "authorship": "MISSING; Cat/Contra use is not inferred from player behavior",
            "numeric_target_armor": "MISSING; catalog armor values are explicit INFERRED sensitivity hypotheses",
            "exact_health_and_coordinates": "MISSING; kill budget and positive co-hit evidence remain bounded proxies",
            "known_player_controlled_summon_entry_ids": sorted(
                KNOWN_PLAYER_CONTROLLED_SUMMON_ENTRY_IDS
            ),
        },
        "selection": {
            "ordering": "source size ascending, then filename",
            "selected_file_count_this_run": selected_file_count,
            "forced_source_count_this_run": len(forced_source_names),
            "forced_source_names_this_run": list(forced_source_names),
        },
        "summary": _aggregate_summary(entries),
        "storage_projection": _storage_projection(
            entries,
            discovered_source_bytes=discovered_source_bytes,
            max_encounters=artifact_contract.get("max_encounters_per_file"),
        ),
        "runtime_projection": _runtime_projection(
            entries,
            discovered_source_bytes=discovered_source_bytes,
            max_encounters=artifact_contract.get("max_encounters_per_file"),
        ),
        "npc_identity_aggregates": (
            {
                "status": "RECONSTRUCTED",
                "basis": "all currently completed feature reports",
                "items": list(npc_aggregates),
            }
            if npc_aggregates is not None
            else {
                "status": "PENDING",
                "basis": "rebuilt after selected files finish; no source JSONL rescan required",
                "items": [],
            }
        ),
        "entries": list(entries),
    }


def _load_previous_manifest(path: Path) -> JSONMap | None:
    if not path.is_file():
        return None
    try:
        value = load_json_document(path)
    except ChronicleEncounterBatchError:
        return None
    return value if value.get("kind") == MANIFEST_KIND else None


def run_batch(
    *,
    input_dir: Path = DEFAULT_INPUT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    base_request_path: Path = DEFAULT_BASE_REQUEST,
    pattern: str = "*.jsonl",
    max_files: int | None = None,
    max_encounters: int | None = None,
    combat_gap_ms: int = DEFAULT_COMBAT_GAP_MS,
    active_tolerance_ms: int = DEFAULT_ACTIVE_TOLERANCE_MS,
    armor_hypotheses: Sequence[int | float],
    level_hypotheses: Sequence[int],
    gzip_level: int = 6,
    resume: bool = True,
    force_sources: Sequence[str | Path] = (),
    fail_fast: bool = False,
) -> JSONMap:
    """Process selected local normalized files and return the persisted manifest."""

    if max_files is not None and max_files <= 0:
        raise ValueError("max_files must be positive")
    if max_encounters is not None and max_encounters <= 0:
        raise ValueError("max_encounters must be positive")
    if not 1 <= gzip_level <= 9:
        raise ValueError("gzip_level must be between 1 and 9")
    if not armor_hypotheses or not level_hypotheses:
        raise ValueError("armor_hypotheses and level_hypotheses must not be empty")

    resolved_input = input_dir.expanduser().resolve()
    resolved_output = output_dir.expanduser().resolve()
    resolved_base = base_request_path.expanduser().resolve()
    if not resolved_base.is_file():
        raise ChronicleEncounterBatchError(f"base request does not exist: {resolved_base}")
    base_request = load_json_document(resolved_base)
    sources = discover_normalized_files(resolved_input, pattern)
    forced_paths = _resolve_forced_sources(sources, resolved_input, force_sources)
    ordinary_selected = sources[:max_files] if max_files is not None else sources
    selected_set = set(ordinary_selected) | forced_paths
    selected = [source for source in sources if source in selected_set]
    forced_source_names = [source.name for source in sources if source in forced_paths]
    resolved_output.mkdir(parents=True, exist_ok=True)
    manifest_path = resolved_output / MANIFEST_NAME
    contract = _artifact_contract(
        base_request_path=resolved_base,
        combat_gap_ms=combat_gap_ms,
        active_tolerance_ms=active_tolerance_ms,
        max_encounters=max_encounters,
        armor_hypotheses=armor_hypotheses,
        level_hypotheses=level_hypotheses,
        gzip_level=gzip_level,
    )

    previous = _load_previous_manifest(manifest_path) if resume else None
    resume_contract_mode: str | None = None
    previous_entries: dict[str, Mapping[str, Any]] = {}
    if previous is not None:
        previous_contract = previous.get("artifact_contract")
        if previous_contract == contract:
            resume_contract_mode = "exact"
        elif _is_legacy_contract_missing_known_summons(previous_contract, contract):
            resume_contract_mode = "legacy_missing_known_summons"
    if previous is not None and resume_contract_mode is not None:
        raw_entries = previous.get("entries")
        if isinstance(raw_entries, list):
            for raw in raw_entries:
                if not isinstance(raw, Mapping):
                    continue
                source = raw.get("source")
                if isinstance(source, Mapping) and isinstance(source.get("path"), str):
                    previous_entries[str(source["path"])] = raw

    entries: list[JSONMap] = []
    by_path: dict[str, JSONMap] = {}
    for source in sources:
        signature = _source_signature(source)
        old = previous_entries.get(str(source))
        entry = _pending_entry(source)
        completed_pair = None
        if (
            source not in forced_paths
            and old is not None
            and old.get("processing_status") == "COMPLETED"
            and _same_source_signature(old.get("source"), signature)
        ):
            completed_pair = _load_completed_pair(old, resolved_output, source)
        if completed_pair is not None:
            legacy_safe = resume_contract_mode != "legacy_missing_known_summons"
            if resume_contract_mode == "legacy_missing_known_summons":
                legacy_safe = _legacy_feature_is_safe_for_known_summons(
                    completed_pair[0],
                    contract[KNOWN_SUMMON_CONTRACT_KEY],
                )
            if legacy_safe:
                entry = dict(old)
        elif source in forced_paths and old is not None:
            entry["attempt_count"] = int(old.get("attempt_count", 0))
        entries.append(entry)
        by_path[str(source)] = entry

    initial = _manifest_document(
        input_dir=resolved_input,
        output_dir=resolved_output,
        pattern=pattern,
        artifact_contract=contract,
        entries=entries,
        selected_file_count=len(selected),
        forced_source_names=forced_source_names,
        npc_aggregates=None,
    )
    _atomic_write_json(manifest_path, initial)

    failed_selected = 0
    for ordinal, source in enumerate(selected, start=1):
        entry = by_path[str(source)]
        if entry.get("processing_status") == "COMPLETED":
            print(f"[{ordinal}/{len(selected)}] resume {source.name}", flush=True)
            continue
        feature_path, catalog_path = _output_paths(resolved_output, source)
        started = time.perf_counter()
        attempt_count = int(entry.get("attempt_count", 0)) + 1
        print(f"[{ordinal}/{len(selected)}] reconstruct {source.name}", flush=True)
        try:
            report = reconstruct_file(
                source,
                combat_gap_ms=combat_gap_ms,
                active_tolerance_ms=active_tolerance_ms,
                max_encounters=max_encounters,
            )
            catalog = compile_fury_encounter_scenarios(
                report,
                base_request,
                armor_hypotheses=armor_hypotheses,
                level_hypotheses=level_hypotheses,
                base_request_label=str(resolved_base),
            )
            _atomic_write_json_gzip(feature_path, report, compresslevel=gzip_level)
            _atomic_write_json_gzip(catalog_path, catalog, compresslevel=gzip_level)
            entry.update(
                {
                    "source": _source_signature(source),
                    "processing_status": "COMPLETED",
                    "attempt_count": attempt_count,
                    "outputs": {
                        "feature_report": {
                            "path": _relative(feature_path, resolved_output),
                            "size_bytes": feature_path.stat().st_size,
                        },
                        "scenario_catalog": {
                            "path": _relative(catalog_path, resolved_output),
                            "size_bytes": catalog_path.stat().st_size,
                        },
                    },
                    "reconstruction_summary": dict(_mapping(report.get("summary"), "report.summary")),
                    "catalog_summary": dict(_mapping(catalog.get("summary"), "catalog.summary")),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "last_error": None,
                }
            )
            print(
                f"[{ordinal}/{len(selected)}] complete encounters={report['summary']['encounter_count']} "
                f"scenarios={catalog['summary']['scenario_count']} "
                f"output={feature_path.stat().st_size + catalog_path.stat().st_size}B",
                flush=True,
            )
        except (OSError, ReconstructionError, ScenarioCompileError, ChronicleEncounterBatchError, ValueError) as exc:
            failed_selected += 1
            entry.update(
                {
                    "source": _source_signature(source),
                    "processing_status": "FAILED",
                    "attempt_count": attempt_count,
                    "outputs": None,
                    "reconstruction_summary": None,
                    "catalog_summary": None,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "last_error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
            print(f"[{ordinal}/{len(selected)}] failed {type(exc).__name__}: {exc}", flush=True)

        checkpoint = _manifest_document(
            input_dir=resolved_input,
            output_dir=resolved_output,
            pattern=pattern,
            artifact_contract=contract,
            entries=entries,
            selected_file_count=len(selected),
            forced_source_names=forced_source_names,
            npc_aggregates=None,
        )
        _atomic_write_json(manifest_path, checkpoint)
        if failed_selected and fail_fast:
            break

    def completed_reports() -> Iterable[tuple[str, JSONMap]]:
        # This generator is intentionally consumed once: even the final
        # cross-file aggregate holds only one decompressed per-file report.
        for source in sources:
            entry = by_path[str(source)]
            if entry.get("processing_status") != "COMPLETED":
                continue
            pair = _load_completed_pair(entry, resolved_output, source)
            if pair is None:
                entry["processing_status"] = "PENDING"
                entry["last_error"] = {
                    "type": "ArtifactValidationError",
                    "message": "completed output pair is missing, corrupt, or has mismatched provenance",
                }
                continue
            feature, _catalog = pair
            yield source.name, feature

    npc_aggregates = _global_npc_identity_aggregates(completed_reports())
    final = _manifest_document(
        input_dir=resolved_input,
        output_dir=resolved_output,
        pattern=pattern,
        artifact_contract=contract,
        entries=entries,
        selected_file_count=len(selected),
        forced_source_names=forced_source_names,
        npc_aggregates=npc_aggregates,
    )
    final["run_result"] = {
        "failed_selected_file_count": failed_selected,
        "forced_source_count": len(forced_source_names),
        "forced_source_names": forced_source_names,
        "exit_success": failed_selected == 0,
    }
    _atomic_write_json(manifest_path, final)
    return final


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--base-request", type=Path, default=DEFAULT_BASE_REQUEST)
    parser.add_argument("--pattern", default="*.jsonl")
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-encounters", type=int)
    parser.add_argument("--combat-gap-ms", type=int, default=DEFAULT_COMBAT_GAP_MS)
    parser.add_argument(
        "--active-tolerance-ms", type=int, default=DEFAULT_ACTIVE_TOLERANCE_MS
    )
    parser.add_argument("--armor-hypothesis", type=float, action="append", required=True)
    parser.add_argument("--level-hypothesis", type=int, action="append", required=True)
    parser.add_argument("--gzip-level", type=int, default=6)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--force-source",
        action="append",
        default=[],
        help=(
            "exact discovered source filename or path to rebuild even when its "
            "signature and completed artifacts would otherwise resume; repeatable"
        ),
    )
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = run_batch(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        base_request_path=args.base_request,
        pattern=args.pattern,
        max_files=args.max_files,
        max_encounters=args.max_encounters,
        combat_gap_ms=args.combat_gap_ms,
        active_tolerance_ms=args.active_tolerance_ms,
        armor_hypotheses=args.armor_hypothesis,
        level_hypotheses=args.level_hypothesis,
        gzip_level=args.gzip_level,
        resume=not args.no_resume,
        force_sources=args.force_source,
        fail_fast=args.fail_fast,
    )
    print(
        json.dumps(
            {
                "manifest": str((args.output_dir / MANIFEST_NAME).resolve()),
                "summary": manifest["summary"],
                "storage_projection": manifest["storage_projection"],
                "runtime_projection": manifest["runtime_projection"],
                "run_result": manifest["run_result"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if manifest["run_result"]["exit_success"] else 1


__all__ = (
    "ChronicleEncounterBatchError",
    "DEFAULT_BASE_REQUEST",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_OUTPUT_DIR",
    "MANIFEST_KIND",
    "discover_normalized_files",
    "load_json_document",
    "run_batch",
)


if __name__ == "__main__":
    raise SystemExit(main())
