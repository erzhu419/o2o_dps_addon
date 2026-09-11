"""Build portable V2 simulator scenarios from compact Chronicle artifacts.

The compiler reads the raw CSV and normalized JSONL only as opaque byte streams
to compute stable SHA-256 identities; it does not parse, copy, or embed their
rows.  It also verifies the content-addressed V2 corpus, its compact catalog,
the sealed batch-manifest bytes, and the already materialized reconstruction
feature file.  The resulting capsule is therefore small enough to send to a
compute node while the roughly 30 GB source corpus remains on Windows, and all
claims about unobserved game state remain explicitly hypothetical.

In particular, hostile activity intervals are not WoW attackability flags,
incoming damage through death is not exact maximum health, and Chronicle aura
events do not identify base or post-debuff armor.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from o2o_dps.fury_offline_corpus_v2 import sha256_json, verify_content_address
from o2o_dps.fury_offline_runner_inputs_v2 import materialize_runner_inputs_v2


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2
SCHEMA = "fury_offline_scenario_capsules/v2"
KIND = "fury_offline_scenario_capsule_bundle_v2"
IMPLEMENTATION_REVISION = "v2.1_local_source_bytes_hashed_compact_model"
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "fury_offline_scenario_capsules"
    / "v2"
)
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


# Feature-v1 retained aura names but not spell IDs.  These IDs and magnitudes
# are simulator hypotheses, not observations from Chronicle.  Additions to the
# feature compiler must be registered here before a capsule can be emitted.
ARMOR_DEBUFF_REGISTRY: dict[str, JSONMap] = {
    "Sunder Armor": {
        "debuff_id": "sunder_armor",
        "simulator_spell_id_hypotheses": [11597],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor_per_stack",
                "value": 450,
                "max_stacks": 5,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
    "Curse of Recklessness": {
        "debuff_id": "curse_of_recklessness",
        "simulator_spell_id_hypotheses": [11717],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor",
                "value": 640,
                "max_stacks": 1,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
    "Faerie Fire": {
        "debuff_id": "faerie_fire",
        "simulator_spell_id_hypotheses": [9907],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor",
                "value": 505,
                "max_stacks": 1,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
    "Faerie Fire (Feral)": {
        "debuff_id": "faerie_fire_feral",
        "simulator_spell_id_hypotheses": [17392],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor",
                "value": 505,
                "max_stacks": 1,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
    "Expose Armor": {
        "debuff_id": "expose_armor",
        "simulator_spell_id_hypotheses": [11198],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor_at_five_combo_points",
                "value": value,
                "improved_expose_armor_points": points,
                "max_stacks": 1,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
            for points, value in ((0, 1700), (1, 2125), (2, 2550))
        ],
    },
    "ExposeArmor": {
        "debuff_id": "expose_armor",
        "simulator_spell_id_hypotheses": [11198],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor_at_five_combo_points",
                "value": value,
                "improved_expose_armor_points": points,
                "max_stacks": 1,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
            for points, value in ((0, 1700), (1, 2125), (2, 2550))
        ],
    },
    "Crystal Yield": {
        "debuff_id": "crystal_yield",
        "simulator_spell_id_hypotheses": [15235],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor",
                "value": 200,
                "max_stacks": 1,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
    "Bonereaver's Edge": {
        "debuff_id": "bonereavers_edge",
        "simulator_spell_id_hypotheses": [21153],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor_per_stack",
                "value": 700,
                "max_stacks": 3,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
    "Annihilator": {
        "debuff_id": "annihilator",
        "simulator_spell_id_hypotheses": [16928],
        "magnitude_hypotheses": [
            {
                "kind": "flat_armor_per_stack",
                "value": 100,
                "max_stacks": 3,
                "status": "SIMULATOR_MECHANIC_HYPOTHESIS",
            }
        ],
    },
}


class FuryOfflineScenarioCapsuleError(RuntimeError):
    """Compact source evidence cannot satisfy the V2 capsule contract."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryOfflineScenarioCapsuleError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryOfflineScenarioCapsuleError(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryOfflineScenarioCapsuleError(f"{label} must be a non-empty string")
    return value.strip()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryOfflineScenarioCapsuleError(f"{label} must be an integer")
    return value


def _sha(value: Any, label: str) -> str:
    rendered = _text(value, label)
    if not _SHA256.fullmatch(rendered):
        raise FuryOfflineScenarioCapsuleError(f"{label} must be a lowercase SHA-256")
    return rendered


def _load_json(path: Path) -> JSONMap:
    try:
        if path.suffix.casefold() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                result = json.load(handle)
        else:
            result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryOfflineScenarioCapsuleError(f"cannot read {path}: {error}") from error
    if not isinstance(result, dict):
        raise FuryOfflineScenarioCapsuleError(f"JSON artifact is not an object: {path}")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FuryOfflineScenarioCapsuleError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _stable_file_identity(path: Path, label: str) -> tuple[int, str]:
    """Hash one local file and reject replacement or mutation during the read."""

    try:
        before = path.stat()
    except OSError as error:
        raise FuryOfflineScenarioCapsuleError(
            f"cannot stat {label} before hashing: {error}"
        ) from error
    digest = _sha256_file(path)
    try:
        after = path.stat()
    except OSError as error:
        raise FuryOfflineScenarioCapsuleError(
            f"cannot stat {label} after hashing: {error}"
        ) from error
    before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key:
        raise FuryOfflineScenarioCapsuleError(
            f"{label} changed while its byte identity was computed"
        )
    return after.st_size, digest


def _resolve_under_root(project_root: Path, raw: Any, label: str) -> Path:
    root = project_root.expanduser().resolve()
    relative = Path(_text(raw, label))
    candidate = relative if relative.is_absolute() else root / relative
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise FuryOfflineScenarioCapsuleError(f"{label} escapes project root") from error
    if not resolved.is_file():
        raise FuryOfflineScenarioCapsuleError(f"{label} does not exist: {resolved}")
    return resolved


def _portable_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as error:
        raise FuryOfflineScenarioCapsuleError(
            f"portable capsule source escapes project root: {path}"
        ) from error


def _resolve_batch_output(batch_path: Path, raw: Any, label: str) -> Path:
    candidate = Path(_text(raw, label))
    if candidate.is_absolute():
        raise FuryOfflineScenarioCapsuleError(f"{label} must be batch-relative")
    resolved = (batch_path.parent / candidate).resolve()
    try:
        resolved.relative_to(batch_path.parent.resolve())
    except ValueError as error:
        raise FuryOfflineScenarioCapsuleError(f"{label} escapes batch directory") from error
    if not resolved.is_file():
        raise FuryOfflineScenarioCapsuleError(f"{label} does not exist: {resolved}")
    return resolved


def _verified_batch_manifest(
    corpus: Mapping[str, Any], project_root: Path
) -> tuple[Path, JSONMap, str]:
    inputs = _mapping(corpus.get("inputs"), "corpus.inputs")
    declared = _mapping(inputs.get("batch_manifest"), "corpus.inputs.batch_manifest")
    path = _resolve_under_root(project_root, declared.get("path"), "batch_manifest.path")
    expected_size = _integer(declared.get("size_bytes"), "batch_manifest.size_bytes")
    if path.stat().st_size != expected_size:
        raise FuryOfflineScenarioCapsuleError("batch manifest byte size mismatch")
    expected_sha = _sha(declared.get("sha256"), "batch_manifest.sha256")
    if _sha256_file(path) != expected_sha:
        raise FuryOfflineScenarioCapsuleError("batch manifest SHA-256 mismatch")
    document = _load_json(path)
    if document.get("kind") != "chronicle_encounter_batch_manifest_v1":
        raise FuryOfflineScenarioCapsuleError("invalid compact batch manifest kind")
    return path, document, expected_sha


def _batch_registry(
    batch_path: Path,
    batch: Mapping[str, Any],
    project_root: Path,
) -> dict[str, JSONMap]:
    registry: dict[str, JSONMap] = {}
    for index, raw_entry in enumerate(_array(batch.get("entries"), "batch.entries")):
        entry = _mapping(raw_entry, f"batch.entries[{index}]")
        if entry.get("processing_status") != "COMPLETED":
            raise FuryOfflineScenarioCapsuleError(
                "batch contains an incomplete source entry; capsule build is fail-closed"
            )
        outputs = _mapping(entry.get("outputs"), f"batch.entries[{index}].outputs")
        catalog_ref = _mapping(
            outputs.get("scenario_catalog"),
            f"batch.entries[{index}].outputs.scenario_catalog",
        )
        feature_ref = _mapping(
            outputs.get("feature_report"),
            f"batch.entries[{index}].outputs.feature_report",
        )
        catalog_path = _resolve_batch_output(
            batch_path, catalog_ref.get("path"), "scenario_catalog.path"
        )
        feature_path = _resolve_batch_output(
            batch_path, feature_ref.get("path"), "feature_report.path"
        )
        for path, ref, label in (
            (catalog_path, catalog_ref, "scenario_catalog"),
            (feature_path, feature_ref, "feature_report"),
        ):
            expected_size = _integer(ref.get("size_bytes"), f"{label}.size_bytes")
            if path.stat().st_size != expected_size:
                raise FuryOfflineScenarioCapsuleError(f"{label} byte size mismatch: {path}")
        key = _portable_path(catalog_path, project_root)
        if key in registry:
            raise FuryOfflineScenarioCapsuleError(f"duplicate batch catalog: {key}")
        registry[key] = {
            "entry": entry,
            "catalog_path": catalog_path,
            "feature_path": feature_path,
        }
    return registry


def _anchor(value: Any, label: str) -> JSONMap:
    source = _mapping(value, label)
    result = {
        "encounter": _text(source.get("encounter"), f"{label}.encounter"),
        "event_index": _integer(source.get("event_index"), f"{label}.event_index"),
        "offset_ms": _integer(source.get("offset_ms"), f"{label}.offset_ms"),
        "type": _text(source.get("type"), f"{label}.type"),
        "source_file": _text(source.get("source_file"), f"{label}.source_file"),
        "raw_file": _text(source.get("raw_file"), f"{label}.raw_file"),
        "csv_line": _integer(source.get("csv_line"), f"{label}.csv_line"),
        "export_row": _integer(source.get("export_row"), f"{label}.export_row"),
    }
    return result


def _feature_index(feature: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    if feature.get("schema_version") != 1 or feature.get("kind") != (
        "chronicle_encounter_reconstruction_v1"
    ):
        raise FuryOfflineScenarioCapsuleError("invalid feature reconstruction schema")
    source = _mapping(feature.get("source"), "feature.source")
    if source.get("complete_file_scanned") is not True or source.get("row_level_copy_written") is not False:
        raise FuryOfflineScenarioCapsuleError(
            "feature reconstruction must cover the complete source without row copies"
        )
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for encounter_index, raw_encounter in enumerate(
        _array(feature.get("encounters"), "feature.encounters")
    ):
        encounter = _mapping(raw_encounter, f"feature.encounters[{encounter_index}]")
        encounter_id = _text(encounter.get("encounter"), "feature encounter ID")
        for wave_index, raw_wave in enumerate(
            _array(encounter.get("waves"), "feature.encounter.waves")
        ):
            wave = _mapping(raw_wave, f"feature encounter wave[{wave_index}]")
            wave_id = _text(wave.get("wave_id"), "feature wave ID")
            key = (encounter_id, wave_id)
            if key in result:
                raise FuryOfflineScenarioCapsuleError(f"duplicate feature wave: {key}")
            result[key] = wave
    return result


def _feature_target_index(wave: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    targets: dict[str, Mapping[str, Any]] = {}
    for index, raw_target in enumerate(_array(wave.get("targets"), "feature.wave.targets")):
        target = _mapping(raw_target, f"feature.wave.targets[{index}]")
        guid = _text(target.get("target_guid"), "feature target GUID")
        if guid in targets:
            raise FuryOfflineScenarioCapsuleError(f"duplicate target GUID in feature wave: {guid}")
        targets[guid] = target
    return targets


def _health_family(
    target: Mapping[str, Any],
    aggregate: Mapping[str, Any] | None,
) -> JSONMap:
    kill_proxy = _mapping(target.get("kill_budget_proxy"), "target.kill_budget_proxy")
    proxy_value = kill_proxy.get("value")
    if proxy_value is not None and (
        isinstance(proxy_value, bool) or not isinstance(proxy_value, (int, float))
    ):
        raise FuryOfflineScenarioCapsuleError("kill-budget proxy must be numeric or null")
    lower = _mapping(
        target.get("confirmed_single_hit_health_lower_bound"),
        "target.confirmed_single_hit_health_lower_bound",
    )
    lower_value = lower.get("value")
    if lower_value is not None and (
        isinstance(lower_value, bool) or not isinstance(lower_value, (int, float))
    ):
        raise FuryOfflineScenarioCapsuleError("health lower bound must be numeric or null")

    branches: list[JSONMap] = [
        {
            "branch_id": "duration_only",
            "use_health": False,
            "max_health": None,
            "status": "CONTROL_BRANCH",
        }
    ]
    candidates: dict[str, tuple[int, str]] = {}
    if isinstance(proxy_value, (int, float)) and proxy_value > 0:
        floor = max(1, int(round(float(lower_value or 1))))
        for label, scale in (("proxy_low", 0.75), ("proxy_center", 1.0), ("proxy_high", 1.25)):
            candidates[label] = (
                max(floor, int(round(float(proxy_value) * scale))),
                "target_observed_kill_budget_proxy_scaled",
            )
    repeat_prior: JSONMap = {"value": None, "status": "MISSING"}
    if aggregate is not None:
        scenario = _mapping(aggregate.get("health_scenario"), "aggregate.health_scenario")
        repeat_prior = {
            "health_scenario": deepcopy(dict(scenario)),
            "kill_budget_proxy_summary": deepcopy(aggregate.get("kill_budget_proxy_summary")),
            "creature_entry_identity": deepcopy(aggregate.get("identity")),
            "historical_truth": False,
        }
        if scenario.get("status") == "INFERRED":
            center = scenario.get("center")
            span = scenario.get("range")
            if isinstance(center, (int, float)) and not isinstance(center, bool):
                candidates["repeat_center"] = (
                    max(1, int(round(float(center)))),
                    "same_creature_entry_repeated_kill_budget_median",
                )
            if isinstance(span, list) and len(span) == 2:
                for label, value in zip(("repeat_iqr_low", "repeat_iqr_high"), span):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        candidates[label] = (
                            max(1, int(round(float(value)))),
                            "same_creature_entry_repeated_kill_budget_iqr",
                        )
    seen: set[int] = set()
    for label, (value, source) in candidates.items():
        if value in seen:
            continue
        seen.add(value)
        branches.append(
            {
                "branch_id": label,
                "use_health": True,
                "max_health": value,
                "status": "SENSITIVITY_HYPOTHESIS",
                "source": source,
            }
        )
    if len(branches) == 1:
        # These values bracket the two health thresholds used by legacy expert
        # branches.  They are generic sensitivity probes, not inferred health.
        for value in (24_999, 25_000, 50_999, 51_000):
            branches.append(
                {
                    "branch_id": f"legacy_threshold_{value}",
                    "use_health": True,
                    "max_health": value,
                    "status": "GENERIC_THRESHOLD_SENSITIVITY_HYPOTHESIS",
                    "source": "legacy_expert_health_threshold_boundary_probe",
                }
            )
    return {
        "exact_max_health": {"value": None, "status": "MISSING"},
        "confirmed_single_hit_lower_bound": deepcopy(lower),
        "observed_kill_budget_proxy": deepcopy(kill_proxy),
        "repeated_creature_entry_health_prior": repeat_prior,
        "shared_branch_family": branches,
        "health_enabled_endogenous_ttk_execution_eligible": False,
        "endogenous_ttk_blocker": "background_team_damage_model_missing",
        "historical_truth": False,
    }


def _observed_combat_summaries(target: Mapping[str, Any]) -> JSONMap:
    result: JSONMap = {}
    for field in (
        "observed_incoming_damage_sum",
        "observed_outgoing_damage_sum",
        "observed_healing_received_sum",
    ):
        raw = target.get(field)
        if raw is None:
            result[field] = {"value": None, "status": "MISSING_IN_FEATURE"}
        else:
            result[field] = deepcopy(dict(_mapping(raw, f"target.{field}")))
    return result


def _observed_armor_timeline(target: Mapping[str, Any], wave_start: int, wave_end: int) -> JSONMap:
    evidence = _mapping(target.get("armor_debuff_evidence"), "target.armor_debuff_evidence")
    transitions: list[JSONMap] = []
    for index, raw_transition in enumerate(
        _array(evidence.get("transitions"), "target.armor_debuff_evidence.transitions")
    ):
        transition = _mapping(raw_transition, f"armor transition[{index}]")
        aura = _text(transition.get("aura"), f"armor transition[{index}].aura")
        registered = ARMOR_DEBUFF_REGISTRY.get(aura)
        if registered is None:
            raise FuryOfflineScenarioCapsuleError(
                f"unregistered armor-debuff aura in compact feature: {aura}"
            )
        operation = _text(transition.get("operation"), "armor transition operation")
        if operation not in {"set", "remove"}:
            raise FuryOfflineScenarioCapsuleError(
                f"unsupported armor-debuff operation: {operation}"
            )
        stacks = _integer(transition.get("stacks"), "armor transition stacks")
        if stacks < 0:
            raise FuryOfflineScenarioCapsuleError("armor-debuff stacks cannot be negative")
        if transition.get("status") != "OBSERVED":
            raise FuryOfflineScenarioCapsuleError("armor transition must be OBSERVED")
        anchor = _anchor(transition.get("anchor"), "armor transition anchor")
        if anchor["type"] != "AURA" or not wave_start <= anchor["offset_ms"] <= wave_end:
            raise FuryOfflineScenarioCapsuleError("armor transition anchor is outside its wave")
        transitions.append(
            {
                "debuff_id": registered["debuff_id"],
                "observed_aura_name": aura,
                "observed_spell_id": None,
                "spell_id_status": "MISSING_IN_COMPACT_FEATURE_V1",
                "operation": operation,
                "stacks": stacks,
                "offset_ms": anchor["offset_ms"] - wave_start,
                "status": "OBSERVED_AURA_TRANSITION",
                "anchor": anchor,
            }
        )
    transitions.sort(key=lambda row: (int(row["offset_ms"]), str(row["debuff_id"])))

    strata: list[JSONMap] = []
    for index, raw_stratum in enumerate(
        _array(evidence.get("strata"), "target.armor_debuff_evidence.strata")
    ):
        stratum = _mapping(raw_stratum, f"armor stratum[{index}]")
        active: list[JSONMap] = []
        for raw_aura in _array(
            stratum.get("observed_active_armor_auras"), "armor stratum active auras"
        ):
            aura = _mapping(raw_aura, "armor stratum active aura")
            name = _text(aura.get("name"), "armor stratum aura name")
            registered = ARMOR_DEBUFF_REGISTRY.get(name)
            if registered is None:
                raise FuryOfflineScenarioCapsuleError(
                    f"unregistered active armor-debuff aura: {name}"
                )
            active.append(
                {
                    "debuff_id": registered["debuff_id"],
                    "observed_aura_name": name,
                    "stacks": _integer(aura.get("stacks"), "active aura stacks"),
                }
            )
        effective = _mapping(stratum.get("effective_armor"), "armor stratum effective_armor")
        if effective.get("value") is not None or effective.get("status") != "MISSING":
            raise FuryOfflineScenarioCapsuleError(
                "compact feature unexpectedly claims numeric effective armor"
            )
        start = _integer(stratum.get("start_offset_ms"), "armor stratum start")
        end = _integer(stratum.get("end_offset_ms"), "armor stratum end")
        if start < wave_start or end > wave_end or end < start:
            raise FuryOfflineScenarioCapsuleError("armor stratum is outside its wave")
        strata.append(
            {
                "start_ms": start - wave_start,
                "end_ms": end - wave_start,
                "observed_active_debuffs": sorted(active, key=lambda row: row["debuff_id"]),
                "status": "RECONSTRUCTED_FROM_OBSERVED_AURA_PREFIX",
                "effective_armor": {"value": None, "status": "MISSING"},
            }
        )
    return {
        "observed_transitions": transitions,
        "reconstructed_partial_prefix_strata": strata,
        "base_armor_exact": {"value": None, "status": "MISSING"},
        "effective_armor_exact": {"value": None, "status": "MISSING"},
    }


def _target_capsule(
    target: Mapping[str, Any],
    *,
    target_index: int,
    wave_start: int,
    wave_end: int,
    aggregate: Mapping[str, Any] | None,
) -> JSONMap:
    activity = _mapping(target.get("activity_interval"), "target.activity_interval")
    if activity.get("status") != "OBSERVED":
        raise FuryOfflineScenarioCapsuleError("target activity interval must be OBSERVED")
    first = _integer(activity.get("first_offset_ms"), "target activity first offset")
    last = _integer(activity.get("last_offset_ms"), "target activity last offset")
    if first < wave_start or last > wave_end or last < first:
        raise FuryOfflineScenarioCapsuleError("target activity interval is outside wave")
    classification = _mapping(target.get("classification"), "target.classification")
    if classification.get("value") != "Hostile Creature":
        raise FuryOfflineScenarioCapsuleError("feature target is not a hostile creature")
    if classification.get("status") not in {"OBSERVED", "INFERRED"}:
        raise FuryOfflineScenarioCapsuleError("unsupported feature classification status")
    return {
        "target_index": target_index,
        "target_guid": _text(target.get("target_guid"), "target.target_guid"),
        "creature_entry_id": _integer(
            target.get("creature_entry_id"), "target.creature_entry_id"
        ),
        "display_name": target.get("target_name"),
        "observed_hostile_activity_proxy": {
            "start_ms": first - wave_start,
            "end_ms": last - wave_start,
            "status": "OBSERVED_ACTIVITY_INTERVAL",
            "not_equal_to": "exact WoW attackability window",
            "first_anchor": _anchor(activity.get("first_anchor"), "activity.first_anchor"),
            "last_anchor": _anchor(activity.get("last_anchor"), "activity.last_anchor"),
        },
        "attackable_window_hypothesis_family": [
            {
                "branch_id": "full_wave",
                "windows": [[0, wave_end - wave_start]],
                "status": "SENSITIVITY_HYPOTHESIS",
                "unattackable_cause": "none_assumed",
            },
            {
                "branch_id": "observed_hostile_activity_proxy",
                "windows": [[first - wave_start, last - wave_start]],
                "status": "EVIDENCE_BOUNDED_SENSITIVITY_HYPOTHESIS",
                "unattackable_cause": "UNIDENTIFIED",
            },
            {
                "branch_id": "tactical_delay_until_activity_proxy",
                "windows": [[first - wave_start, last - wave_start]],
                "status": "SENSITIVITY_HYPOTHESIS",
                "unattackable_cause": "raid_tactic_delay",
                "cause_identified_by_chronicle": False,
            },
            {
                "branch_id": "mechanic_gate_until_activity_proxy",
                "windows": [[first - wave_start, last - wave_start]],
                "status": "SENSITIVITY_HYPOTHESIS",
                "unattackable_cause": "encounter_mechanic",
                "cause_identified_by_chronicle": False,
            },
        ],
        "attackability_cause_exact": {"value": None, "status": "MISSING"},
        "max_health_hypothesis_family": _health_family(target, aggregate),
        "observed_combat_summaries": _observed_combat_summaries(target),
        "background_team_damage_model": {
            "value": None,
            "status": "MISSING_REQUIRES_PLAYER_CONDITIONING",
            "reason": (
                "observed incoming raid damage is not attributable to the simulated "
                "player and cannot be assigned to that player"
            ),
        },
        "classification": {
            "observed_coarse_value": classification.get("value"),
            "observed_coarse_status": classification.get("status"),
            "wow_unit_classification_exact": {"value": None, "status": "MISSING"},
            "simulator_hypothesis_family": ["non_worldboss", "worldboss"],
        },
        "armor": _observed_armor_timeline(target, wave_start, wave_end),
    }


def _aggregate_index(feature: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for index, raw in enumerate(
        _array(feature.get("npc_identity_aggregates"), "feature.npc_identity_aggregates")
    ):
        aggregate = _mapping(raw, f"npc_identity_aggregates[{index}]")
        identity = _mapping(aggregate.get("identity"), "aggregate.identity")
        entry = _integer(identity.get("creature_entry_id"), "aggregate creature entry")
        if entry in result:
            raise FuryOfflineScenarioCapsuleError(
                f"duplicate feature NPC identity aggregate: {entry}"
            )
        result[entry] = aggregate
    return result


def _source_record(
    *,
    project_root: Path,
    batch_entry: Mapping[str, Any],
    catalog_path: Path,
    catalog_sha: str,
    feature_path: Path,
    feature: Mapping[str, Any],
) -> JSONMap:
    source = _mapping(batch_entry.get("source"), "batch entry source")
    normalized_path = _resolve_under_root(
        project_root, source.get("path"), "batch entry normalized source"
    )
    normalized_size = _integer(source.get("size_bytes"), "normalized source size")
    observed_normalized_size, normalized_sha = _stable_file_identity(
        normalized_path, "normalized source"
    )
    if observed_normalized_size != normalized_size:
        raise FuryOfflineScenarioCapsuleError("normalized source byte size mismatch")
    feature_source = _mapping(feature.get("source"), "feature.source")
    if Path(_text(feature_source.get("normalized_file"), "feature normalized file")).name != normalized_path.name:
        raise FuryOfflineScenarioCapsuleError("feature and batch normalized source disagree")

    raw_refs: set[str] = set()
    for encounter in _array(feature.get("encounters"), "feature.encounters"):
        first_anchor = _mapping(_mapping(encounter, "feature encounter").get("first_anchor"), "encounter first anchor")
        raw_refs.add(_text(first_anchor.get("raw_file"), "anchor raw_file"))
    if len(raw_refs) != 1:
        raise FuryOfflineScenarioCapsuleError("feature must resolve to one raw CSV reference")
    raw_ref = next(iter(raw_refs))
    raw_path = _resolve_under_root(
        project_root, str(Path("offline_data") / raw_ref), "raw CSV reference"
    )
    provenance_path = raw_path.parent / "provenance.json"
    if not provenance_path.is_file():
        raise FuryOfflineScenarioCapsuleError(f"missing raw provenance sidecar: {provenance_path}")
    provenance = _load_json(provenance_path)
    if provenance.get("raw_copy") != raw_ref:
        raise FuryOfflineScenarioCapsuleError("raw provenance locator mismatch")
    if Path(_text(provenance.get("normalized"), "raw provenance normalized")).name != normalized_path.name:
        raise FuryOfflineScenarioCapsuleError("raw provenance normalized locator mismatch")
    declared_raw = _mapping(provenance.get("source"), "raw provenance source")
    raw_size = _integer(declared_raw.get("size_bytes"), "raw provenance source.size_bytes")
    observed_raw_size, raw_sha = _stable_file_identity(raw_path, "raw CSV source")
    if observed_raw_size != raw_size:
        raise FuryOfflineScenarioCapsuleError("raw CSV byte size mismatch")
    provenance_sha = _sha256_file(provenance_path)
    feature_sha = _sha256_file(feature_path)
    normalized_identity: JSONMap = {
        "path": _portable_path(normalized_path, project_root),
        "size_bytes": normalized_size,
        "byte_sha256": normalized_sha,
        "byte_hash_status": "COMPUTED_LOCALLY_BY_COMPACT_COMPILER",
        "uploaded_with_capsule": False,
    }
    raw_identity: JSONMap = {
        "path": _portable_path(raw_path, project_root),
        "size_bytes": raw_size,
        "row_count": _integer(provenance.get("row_count"), "raw provenance row_count"),
        "byte_sha256": raw_sha,
        "byte_hash_status": "COMPUTED_LOCALLY_BY_COMPACT_COMPILER",
        "uploaded_with_capsule": False,
    }
    raw_source_reference_sha256 = sha256_json(
        {
            "raw_csv_source": raw_identity,
            "normalized_source": normalized_identity,
            "raw_provenance_sha256": provenance_sha,
        }
    )
    record: JSONMap = {
        "source_bundle_id": sha256_json(
            {
                "catalog_sha256": catalog_sha,
                "feature_sha256": feature_sha,
                "provenance_sha256": provenance_sha,
                "raw_source_reference_sha256": raw_source_reference_sha256,
            }
        ),
        "catalog": {
            "path": _portable_path(catalog_path, project_root),
            "size_bytes": catalog_path.stat().st_size,
            "sha256": catalog_sha,
        },
        "reconstruction_feature": {
            "path": _portable_path(feature_path, project_root),
            "size_bytes": feature_path.stat().st_size,
            "sha256": feature_sha,
            "complete_normalized_file_scanned": True,
            "row_level_copy_written": False,
        },
        "raw_provenance": {
            "path": _portable_path(provenance_path, project_root),
            "size_bytes": provenance_path.stat().st_size,
            "sha256": provenance_sha,
        },
        "normalized_source": normalized_identity,
        "raw_csv_source": raw_identity,
        "raw_source_reference_sha256": raw_source_reference_sha256,
    }
    return record


def _scenario_capsule(
    *,
    entry: Mapping[str, Any],
    source_scenario: Mapping[str, Any],
    runner_row: Mapping[str, Any],
    feature_wave: Mapping[str, Any],
    feature: Mapping[str, Any],
    source_bundle_id: str,
) -> JSONMap:
    scenario_id = _text(source_scenario.get("scenario_id"), "source scenario ID")
    if sha256_json(source_scenario) != runner_row.get("source_scenario_sha256"):
        raise FuryOfflineScenarioCapsuleError("runner/source scenario hash mismatch")
    source = _mapping(source_scenario.get("source"), "source scenario source")
    pile = _mapping(source_scenario.get("pile"), "source scenario pile")
    duration = _mapping(source_scenario.get("duration"), "source scenario duration")
    horizon = _integer(duration.get("observed_span_ms"), "scenario horizon")
    wave_start = _integer(feature_wave.get("start_offset_ms"), "feature wave start")
    wave_end = _integer(feature_wave.get("end_offset_ms"), "feature wave end")
    if wave_end - wave_start != horizon:
        raise FuryOfflineScenarioCapsuleError("feature and catalog wave horizons disagree")
    target_index = _feature_target_index(feature_wave)
    aggregates = _aggregate_index(feature)
    guids = [
        _text(value, "pile target GUID")
        for value in _array(pile.get("target_guids"), "pile.target_guids")
    ]
    if len(guids) != _integer(pile.get("target_count"), "pile.target_count"):
        raise FuryOfflineScenarioCapsuleError("pile target count mismatch")
    targets: list[JSONMap] = []
    for index, guid in enumerate(guids):
        feature_target = target_index.get(guid)
        if feature_target is None:
            raise FuryOfflineScenarioCapsuleError(
                f"catalog target is absent from feature wave: {guid}"
            )
        creature_entry = _integer(
            feature_target.get("creature_entry_id"), "feature target creature entry"
        )
        targets.append(
            _target_capsule(
                feature_target,
                target_index=index,
                wave_start=wave_start,
                wave_end=wave_end,
                aggregate=aggregates.get(creature_entry),
            )
        )
    spatial = _mapping(pile.get("spatial_assumption"), "pile.spatial_assumption")
    if spatial.get("coordinates") is not None:
        raise FuryOfflineScenarioCapsuleError(
            "compact V1 input unexpectedly claims exact target coordinates"
        )
    target_hypotheses = _mapping(
        source_scenario.get("target_hypotheses"), "source target hypotheses"
    )
    armor = _mapping(target_hypotheses.get("armor"), "target hypotheses armor")
    if armor.get("status") != "INFERRED" or armor.get("not_identified_by_chronicle") is not True:
        raise FuryOfflineScenarioCapsuleError("catalog armor hypothesis lost its uncertainty flag")
    capsule_core: JSONMap = {
        "scenario_id": scenario_id,
        "source_bundle_id": source_bundle_id,
        "historical_truth": False,
        "claim_scope": "PREREGISTERED_SIMULATOR_SCENARIO_MODEL_ONLY",
        "source_identity": {
            "instance_id": _text(source.get("instance"), "source instance"),
            "encounter_id": _text(source.get("encounter"), "source encounter"),
            "wave_id": _text(source.get("wave_id"), "source wave ID"),
            "wave_ordinal": _integer(source.get("wave_ordinal"), "source wave ordinal"),
        },
        "horizon": {
            "milliseconds": horizon,
            "status": "RECONSTRUCTED_HOSTILE_ACTIVITY_SPAN",
            "not_equal_to": "exact pull duration or exact attackability duration",
        },
        "wave_role": {
            "exact": {"value": None, "status": "MISSING"},
            "simulator_hypothesis_family": ["trash_pack", "boss_or_worldboss"],
            "identified_from_compact_chronicle": False,
        },
        "layout": {
            "variant": _text(pile.get("layout_variant"), "pile layout variant"),
            "side": _text(pile.get("layout_side"), "pile layout side"),
            "sensitivity_family": _text(
                pile.get("sensitivity_family"), "pile sensitivity family"
            ),
            "spatial_assumption": deepcopy(spatial),
            "coordinates_exact": {"value": None, "status": "MISSING"},
        },
        "targets": targets,
        "base_armor_hypothesis_family": [
            {
                "branch_id": f"catalog_inferred_{armor.get('value')}",
                "base_armor": armor.get("value"),
                "status": "SENSITIVITY_HYPOTHESIS",
                "not_identified_by_chronicle": True,
            }
        ],
        "provenance_hashes": {
            "corpus_entry_sha256": runner_row.get("corpus_entry_sha256"),
            "source_scenario_sha256": runner_row.get("source_scenario_sha256"),
            "source_sha256": runner_row.get("source_sha256"),
            "request_sha256": runner_row.get("request_sha256"),
            "pile_sha256": runner_row.get("pile_sha256"),
            "target_hypotheses_sha256": runner_row.get("target_hypotheses_sha256"),
            "kill_budget_proxies_sha256": runner_row.get("kill_budget_proxies_sha256"),
        },
        "runner_projection": {
            "instance_id": runner_row.get("instance_id"),
            "component_id": runner_row.get("component_id"),
            "family_id": runner_row.get("family_id"),
            "stratum": runner_row.get("stratum"),
            "scenario_weight": runner_row.get("scenario_weight"),
            "request_sha256": runner_row.get("request_sha256"),
        },
        "execution_mode_eligibility": {
            "fixed_observed_horizon_duration_model": True,
            "health_enabled_endogenous_ttk": False,
            "health_enabled_endogenous_ttk_blocker": (
                "per-target background team damage requires player-conditioned estimation"
            ),
        },
    }
    return {**capsule_core, "capsule_sha256": sha256_json(capsule_core)}


def _source_instance_provenance(
    sources: Sequence[Mapping[str, Any]],
    scenarios: Sequence[Mapping[str, Any]],
) -> JSONMap:
    instance_ids_by_source: dict[str, set[str]] = {}
    for scenario in scenarios:
        source_id = _sha(scenario.get("source_bundle_id"), "scenario source_bundle_id")
        identity = _mapping(scenario.get("source_identity"), "scenario source_identity")
        instance_ids_by_source.setdefault(source_id, set()).add(
            _text(identity.get("instance_id"), "scenario source instance_id")
        )
    entries: list[JSONMap] = []
    for source in sources:
        source_id = _sha(source.get("source_bundle_id"), "source_bundle_id")
        instance_ids = instance_ids_by_source.get(source_id, set())
        if len(instance_ids) != 1:
            raise FuryOfflineScenarioCapsuleError(
                "each compact source must map to exactly one source instance"
            )
        entries.append(
            {
                "instance_id": next(iter(instance_ids)),
                "source_bundle_id": source_id,
                "raw_source_reference_sha256": _sha(
                    source.get("raw_source_reference_sha256"),
                    "raw source reference SHA",
                ),
                "raw_provenance_sha256": _sha(
                    _mapping(source.get("raw_provenance"), "raw provenance").get(
                        "sha256"
                    ),
                    "raw provenance SHA",
                ),
                "raw_csv_byte_sha256": _sha(
                    _mapping(source.get("raw_csv_source"), "raw CSV source").get(
                        "byte_sha256"
                    ),
                    "raw CSV byte SHA",
                ),
                "normalized_byte_sha256": _sha(
                    _mapping(
                        source.get("normalized_source"), "normalized source"
                    ).get("byte_sha256"),
                    "normalized byte SHA",
                ),
                "catalog_sha256": _sha(
                    _mapping(source.get("catalog"), "source catalog").get("sha256"),
                    "source catalog SHA",
                ),
                "reconstruction_feature_sha256": _sha(
                    _mapping(
                        source.get("reconstruction_feature"),
                        "source reconstruction feature",
                    ).get("sha256"),
                    "source reconstruction feature SHA",
                ),
            }
        )
    entries.sort(key=lambda row: str(row["instance_id"]))
    return {
        "schema": "fury_development_source_instance_provenance/v2",
        "entry_count": len(entries),
        "entries": entries,
        "source_instance_provenance_sha256": sha256_json(entries),
        "raw_and_normalized_byte_identity_claimed": True,
        "raw_or_normalized_bytes_embedded": False,
    }


def build_scenario_capsule_bundle_v2(
    manifest_path: Path,
    *,
    bucket: str = "main_comparison",
    project_root: Path = PROJECT_ROOT,
) -> JSONMap:
    """Build a compact, content-addressed scenario-model bundle."""

    root = project_root.expanduser().resolve()
    manifest_resolved = manifest_path.expanduser().resolve()
    corpus = _load_json(manifest_resolved)
    if (
        corpus.get("schema_version") != 2
        or corpus.get("schema") != "fury_offline_corpus/v2"
        or corpus.get("kind") != "fury_offline_corpus_manifest_v2"
        or not verify_content_address(corpus)
    ):
        raise FuryOfflineScenarioCapsuleError("invalid corpus content address or schema")
    runner = materialize_runner_inputs_v2(
        manifest_resolved, bucket=bucket, project_root=root
    )
    runner_rows = {
        _text(row.get("scenario_id"), "runner scenario ID"): row
        for row in _array(runner.get("scenarios"), "runner.scenarios")
    }
    batch_path, batch, batch_sha = _verified_batch_manifest(corpus, root)
    batch_registry = _batch_registry(batch_path, batch, root)

    source_cache: dict[str, JSONMap] = {}
    catalog_cache: dict[str, JSONMap] = {}
    feature_cache: dict[str, JSONMap] = {}
    feature_indexes: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = {}
    capsules: list[JSONMap] = []
    selected_entries = [
        _mapping(value, "corpus scenario")
        for value in _array(corpus.get("scenarios"), "corpus.scenarios")
        if _mapping(value, "corpus scenario").get("bucket") == bucket
    ]
    for entry in selected_entries:
        scenario_id = _text(entry.get("scenario_id"), "corpus scenario ID")
        runner_row = runner_rows.get(scenario_id)
        if runner_row is None:
            raise FuryOfflineScenarioCapsuleError("corpus and runner selections disagree")
        locator = _mapping(entry.get("catalog_locator"), "entry.catalog_locator")
        locator_path = _text(locator.get("path"), "catalog locator path")
        batch_item = batch_registry.get(locator_path)
        if batch_item is None:
            raise FuryOfflineScenarioCapsuleError(
                f"corpus catalog is absent from verified batch: {locator_path}"
            )
        catalog_path = batch_item["catalog_path"]
        feature_path = batch_item["feature_path"]
        catalog_sha = _sha(locator.get("catalog_sha256"), "catalog locator SHA")
        if locator_path not in catalog_cache:
            if _sha256_file(catalog_path) != catalog_sha:
                raise FuryOfflineScenarioCapsuleError("catalog SHA-256 mismatch")
            catalog = _load_json(catalog_path)
            feature = _load_json(feature_path)
            catalog_cache[locator_path] = catalog
            feature_cache[locator_path] = feature
            feature_indexes[locator_path] = _feature_index(feature)
            record = _source_record(
                project_root=root,
                batch_entry=batch_item["entry"],
                catalog_path=catalog_path,
                catalog_sha=catalog_sha,
                feature_path=feature_path,
                feature=feature,
            )
            source_cache[locator_path] = record
        catalog = catalog_cache[locator_path]
        feature = feature_cache[locator_path]
        scenarios = _array(catalog.get("scenarios"), "catalog.scenarios")
        source_index = _integer(locator.get("scenario_index"), "catalog scenario index")
        if source_index < 0 or source_index >= len(scenarios):
            raise FuryOfflineScenarioCapsuleError("catalog scenario index is out of range")
        source_scenario = _mapping(scenarios[source_index], "catalog source scenario")
        source = _mapping(source_scenario.get("source"), "source scenario source")
        wave_key = (
            _text(source.get("encounter"), "source encounter"),
            _text(source.get("wave_id"), "source wave ID"),
        )
        feature_wave = feature_indexes[locator_path].get(wave_key)
        if feature_wave is None:
            raise FuryOfflineScenarioCapsuleError(
                f"source scenario wave is absent from reconstruction feature: {wave_key}"
            )
        capsules.append(
            _scenario_capsule(
                entry=entry,
                source_scenario=source_scenario,
                runner_row=runner_row,
                feature_wave=feature_wave,
                feature=feature,
                source_bundle_id=source_cache[locator_path]["source_bundle_id"],
            )
        )
    if len(capsules) != len(runner_rows):
        raise FuryOfflineScenarioCapsuleError("not every verified runner row produced a capsule")
    capsules.sort(key=lambda row: (row["source_identity"]["instance_id"], row["scenario_id"]))
    sources = sorted(source_cache.values(), key=lambda row: row["catalog"]["path"])
    source_instance_provenance = _source_instance_provenance(sources, capsules)
    compact_source_bytes = sum(
        int(source[artifact]["size_bytes"])
        for source in sources
        for artifact in ("catalog", "reconstruction_feature")
    )
    normalized_bytes_previously_scanned = sum(
        int(source["normalized_source"]["size_bytes"]) for source in sources
    )
    transition_counts = Counter(
        transition["debuff_id"]
        for capsule in capsules
        for target in capsule["targets"]
        for transition in target["armor"]["observed_transitions"]
    )
    core: JSONMap = {
        "schema_version": SCHEMA_VERSION,
        "schema": SCHEMA,
        "kind": KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "historical_truth": False,
        "corpus_role": corpus.get("corpus_role"),
        "bucket": bucket,
        "claim_boundary": {
            "portable_simulator_scenario_model": True,
            "exact_historical_replay": False,
            "exact_target_health": False,
            "exact_base_or_effective_armor": False,
            "exact_attackability": False,
            "exact_wow_unit_classification": False,
            "exact_coordinates": False,
            "simulator_execution_started": False,
            "superiority_result": False,
        },
        "source_contract": {
            "corpus_manifest_path": _portable_path(manifest_resolved, root),
            "corpus_manifest_sha256": _sha(
                _mapping(corpus.get("content_address"), "corpus.content_address").get("sha256"),
                "corpus content address",
            ),
            "batch_manifest_path": _portable_path(batch_path, root),
            "batch_manifest_sha256": batch_sha,
            "raw_csv_or_normalized_rows_opened": True,
            "raw_csv_or_normalized_rows_parsed": False,
            "raw_csv_or_normalized_rows_embedded": False,
            "raw_upload_required": False,
            "feature_files_content_hashed": True,
            "raw_csv_and_normalized_bytes_content_hashed": True,
        },
        "transport_contract": {
            "upload_capsule_only": True,
            "raw_upload_required": False,
            "raw_csv_uploaded": False,
            "normalized_jsonl_uploaded": False,
        },
        "model_registry": {
            "armor_debuffs": deepcopy(ARMOR_DEBUFF_REGISTRY),
            "armor_magnitude_values_are_chronicle_observations": False,
            "armor_magnitude_values_bound_to_execution_binary": False,
            "armor_magnitude_execution_gate": (
                "downstream runner must bind these simulator-mechanic hypotheses "
                "to a content-addressed simulator source/binary before voting"
            ),
            "classification_axis": ["non_worldboss", "worldboss"],
            "classification_axis_is_historical_truth": False,
            "attackable_window_axes": [
                "full_wave",
                "observed_hostile_activity_proxy",
                "tactical_delay_until_activity_proxy",
                "mechanic_gate_until_activity_proxy",
            ],
            "attackable_window_axes_are_historical_truth": False,
            "health_branch_axes_are_historical_truth": False,
            "extension_rule": (
                "an unseen armor-debuff aura name or operation blocks compilation until "
                "the versioned registry and tests are extended"
            ),
        },
        "sources": sources,
        "source_instance_provenance": source_instance_provenance,
        "summary": {
            "source_bundle_count": len(sources),
            "scenario_count": len(capsules),
            "target_count": sum(len(row["targets"]) for row in capsules),
            "stratum_counts": dict(
                sorted(Counter(row["runner_projection"]["stratum"] for row in capsules).items())
            ),
            "observed_armor_debuff_transition_counts": dict(sorted(transition_counts.items())),
            "raw_csv_or_normalized_byte_count_uploaded": 0,
            "compact_feature_and_catalog_source_bytes": compact_source_bytes,
            "normalized_bytes_previously_scanned": normalized_bytes_previously_scanned,
            "compact_to_previously_scanned_byte_ratio": (
                compact_source_bytes / normalized_bytes_previously_scanned
                if normalized_bytes_previously_scanned
                else None
            ),
            "simulator_execution_started": False,
        },
        "scenarios": capsules,
    }
    bundle = deepcopy(core)
    bundle["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_scenario_capsule_bundle_v2(bundle)
    return bundle


def _validate_capsule_source(value: Any, label: str) -> str:
    source = _mapping(value, label)
    expected_source_fields = {
        "source_bundle_id",
        "catalog",
        "reconstruction_feature",
        "raw_provenance",
        "normalized_source",
        "raw_csv_source",
        "raw_source_reference_sha256",
    }
    if set(source) != expected_source_fields:
        raise FuryOfflineScenarioCapsuleError(f"{label} fields mismatch")

    catalog = _mapping(source.get("catalog"), f"{label}.catalog")
    feature = _mapping(
        source.get("reconstruction_feature"), f"{label}.reconstruction_feature"
    )
    provenance = _mapping(source.get("raw_provenance"), f"{label}.raw_provenance")
    normalized = _mapping(source.get("normalized_source"), f"{label}.normalized_source")
    raw = _mapping(source.get("raw_csv_source"), f"{label}.raw_csv_source")
    if set(catalog) != {"path", "size_bytes", "sha256"}:
        raise FuryOfflineScenarioCapsuleError(f"{label}.catalog fields mismatch")
    if set(feature) != {
        "path",
        "size_bytes",
        "sha256",
        "complete_normalized_file_scanned",
        "row_level_copy_written",
    }:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.reconstruction_feature fields mismatch"
        )
    if set(provenance) != {"path", "size_bytes", "sha256"}:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.raw_provenance fields mismatch"
        )
    if set(normalized) != {
        "path",
        "size_bytes",
        "byte_sha256",
        "byte_hash_status",
        "uploaded_with_capsule",
    }:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.normalized_source fields mismatch"
        )
    if set(raw) != {
        "path",
        "size_bytes",
        "row_count",
        "byte_sha256",
        "byte_hash_status",
        "uploaded_with_capsule",
    }:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.raw_csv_source fields mismatch"
        )

    for artifact_name, artifact in (
        ("catalog", catalog),
        ("reconstruction_feature", feature),
        ("raw_provenance", provenance),
    ):
        _text(artifact.get("path"), f"{label}.{artifact_name}.path")
        _integer(artifact.get("size_bytes"), f"{label}.{artifact_name}.size_bytes")
        _sha(artifact.get("sha256"), f"{label}.{artifact_name}.sha256")
    if feature.get("complete_normalized_file_scanned") is not True:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.reconstruction_feature must cover the complete normalized file"
        )
    if feature.get("row_level_copy_written") is not False:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.reconstruction_feature must not embed source rows"
        )
    for artifact_name, artifact in (
        ("normalized_source", normalized),
        ("raw_csv_source", raw),
    ):
        _text(artifact.get("path"), f"{label}.{artifact_name}.path")
        _integer(artifact.get("size_bytes"), f"{label}.{artifact_name}.size_bytes")
        _sha(artifact.get("byte_sha256"), f"{label}.{artifact_name}.byte_sha256")
        if artifact.get("byte_hash_status") != "COMPUTED_LOCALLY_BY_COMPACT_COMPILER":
            raise FuryOfflineScenarioCapsuleError(
                f"{label}.{artifact_name} must carry a locally computed byte hash"
            )
        if artifact.get("uploaded_with_capsule") is not False:
            raise FuryOfflineScenarioCapsuleError(
                f"{label}.{artifact_name} must remain outside the compact capsule"
            )
    _integer(raw.get("row_count"), f"{label}.raw_csv_source.row_count")

    expected_reference = sha256_json(
        {
            "raw_csv_source": dict(raw),
            "normalized_source": dict(normalized),
            "raw_provenance_sha256": provenance["sha256"],
        }
    )
    if source.get("raw_source_reference_sha256") != expected_reference:
        raise FuryOfflineScenarioCapsuleError(
            f"{label}.raw_source_reference_sha256 mismatch"
        )
    expected_bundle_id = sha256_json(
        {
            "catalog_sha256": catalog["sha256"],
            "feature_sha256": feature["sha256"],
            "provenance_sha256": provenance["sha256"],
            "raw_source_reference_sha256": expected_reference,
        }
    )
    if source.get("source_bundle_id") != expected_bundle_id:
        raise FuryOfflineScenarioCapsuleError(f"{label}.source_bundle_id mismatch")
    return expected_bundle_id


def validate_scenario_capsule_bundle_v2(bundle: Mapping[str, Any]) -> None:
    """Fail closed on claim drift, malformed hashes, or content changes."""

    expected_top_level_fields = {
        "schema_version",
        "schema",
        "kind",
        "implementation_revision",
        "historical_truth",
        "corpus_role",
        "bucket",
        "claim_boundary",
        "source_contract",
        "transport_contract",
        "model_registry",
        "sources",
        "source_instance_provenance",
        "summary",
        "scenarios",
        "content_address",
    }
    if set(bundle) != expected_top_level_fields:
        raise FuryOfflineScenarioCapsuleError("scenario capsule top-level fields mismatch")
    if (
        bundle.get("schema_version") != SCHEMA_VERSION
        or bundle.get("schema") != SCHEMA
        or bundle.get("kind") != KIND
        or bundle.get("implementation_revision") != IMPLEMENTATION_REVISION
    ):
        raise FuryOfflineScenarioCapsuleError("invalid scenario capsule schema")
    if bundle.get("historical_truth") is not False:
        raise FuryOfflineScenarioCapsuleError("scenario capsule must set historical_truth=false")
    claim = _mapping(bundle.get("claim_boundary"), "bundle.claim_boundary")
    if set(claim) != {
        "portable_simulator_scenario_model",
        "exact_historical_replay",
        "exact_target_health",
        "exact_base_or_effective_armor",
        "exact_attackability",
        "exact_wow_unit_classification",
        "exact_coordinates",
        "simulator_execution_started",
        "superiority_result",
    }:
        raise FuryOfflineScenarioCapsuleError("capsule claim boundary fields mismatch")
    if claim.get("portable_simulator_scenario_model") is not True:
        raise FuryOfflineScenarioCapsuleError(
            "capsule must declare a portable simulator scenario model"
        )
    forbidden_true = (
        "exact_historical_replay",
        "exact_target_health",
        "exact_base_or_effective_armor",
        "exact_attackability",
        "exact_wow_unit_classification",
        "exact_coordinates",
        "simulator_execution_started",
        "superiority_result",
    )
    if any(claim.get(field) is not False for field in forbidden_true):
        raise FuryOfflineScenarioCapsuleError("capsule claim boundary was widened")
    source_contract = _mapping(bundle.get("source_contract"), "bundle.source_contract")
    if set(source_contract) != {
        "corpus_manifest_path",
        "corpus_manifest_sha256",
        "batch_manifest_path",
        "batch_manifest_sha256",
        "raw_csv_or_normalized_rows_opened",
        "raw_csv_or_normalized_rows_parsed",
        "raw_csv_or_normalized_rows_embedded",
        "raw_upload_required",
        "feature_files_content_hashed",
        "raw_csv_and_normalized_bytes_content_hashed",
    }:
        raise FuryOfflineScenarioCapsuleError("capsule source contract fields mismatch")
    _text(source_contract.get("corpus_manifest_path"), "corpus manifest path")
    _sha(source_contract.get("corpus_manifest_sha256"), "corpus manifest SHA")
    _text(source_contract.get("batch_manifest_path"), "batch manifest path")
    _sha(source_contract.get("batch_manifest_sha256"), "batch manifest SHA")
    if (
        source_contract.get("raw_csv_or_normalized_rows_opened") is not True
        or source_contract.get("raw_csv_or_normalized_rows_parsed") is not False
        or source_contract.get("raw_csv_or_normalized_rows_embedded") is not False
        or source_contract.get("raw_upload_required") is not False
        or source_contract.get("feature_files_content_hashed") is not True
        or source_contract.get("raw_csv_and_normalized_bytes_content_hashed") is not True
    ):
        raise FuryOfflineScenarioCapsuleError(
            "capsule source contract must hash local source bytes without parsing, "
            "embedding, or uploading them"
        )
    transport = _mapping(bundle.get("transport_contract"), "bundle.transport_contract")
    if set(transport) != {
        "upload_capsule_only",
        "raw_upload_required",
        "raw_csv_uploaded",
        "normalized_jsonl_uploaded",
    }:
        raise FuryOfflineScenarioCapsuleError("capsule transport contract fields mismatch")
    if (
        transport.get("upload_capsule_only") is not True
        or transport.get("raw_upload_required") is not False
        or transport.get("raw_csv_uploaded") is not False
        or transport.get("normalized_jsonl_uploaded") is not False
    ):
        raise FuryOfflineScenarioCapsuleError("capsule transport contract was widened")
    address = _mapping(bundle.get("content_address"), "bundle.content_address")
    declared = _sha(address.get("sha256"), "bundle content address")
    core = deepcopy(dict(bundle))
    core.pop("content_address", None)
    if sha256_json(core) != declared:
        raise FuryOfflineScenarioCapsuleError("capsule content address mismatch")
    source_ids: set[str] = set()
    for index, source in enumerate(_array(bundle.get("sources"), "bundle.sources")):
        source_id = _validate_capsule_source(source, f"bundle.sources[{index}]")
        if source_id in source_ids:
            raise FuryOfflineScenarioCapsuleError(
                f"duplicate capsule source bundle ID: {source_id}"
            )
        source_ids.add(source_id)
    scenarios = _array(bundle.get("scenarios"), "bundle.scenarios")
    provenance = _mapping(
        bundle.get("source_instance_provenance"),
        "bundle.source_instance_provenance",
    )
    expected_provenance = _source_instance_provenance(
        [
            _mapping(source, f"bundle.sources[{index}]")
            for index, source in enumerate(
                _array(bundle.get("sources"), "bundle.sources")
            )
        ],
        [_mapping(scenario, "bundle scenario") for scenario in scenarios],
    )
    if dict(provenance) != expected_provenance:
        raise FuryOfflineScenarioCapsuleError(
            "capsule source-instance provenance is inconsistent"
        )
    ids: set[str] = set()
    for index, raw_scenario in enumerate(scenarios):
        scenario = _mapping(raw_scenario, f"bundle.scenarios[{index}]")
        scenario_id = _text(scenario.get("scenario_id"), "capsule scenario ID")
        if scenario_id in ids:
            raise FuryOfflineScenarioCapsuleError(f"duplicate capsule scenario ID: {scenario_id}")
        ids.add(scenario_id)
        if scenario.get("historical_truth") is not False:
            raise FuryOfflineScenarioCapsuleError("scenario historical_truth must be false")
        capsule_sha = _sha(scenario.get("capsule_sha256"), "scenario capsule SHA")
        scenario_core = deepcopy(dict(scenario))
        scenario_core.pop("capsule_sha256", None)
        if sha256_json(scenario_core) != capsule_sha:
            raise FuryOfflineScenarioCapsuleError("scenario capsule SHA mismatch")
        for target in _array(scenario.get("targets"), "scenario.targets"):
            health = _mapping(
                _mapping(target, "scenario target").get("max_health_hypothesis_family"),
                "target max health family",
            )
            if health.get("historical_truth") is not False:
                raise FuryOfflineScenarioCapsuleError("health family claims historical truth")


def verify_capsule_bundle_sources_v2(
    bundle: Mapping[str, Any], *, project_root: Path = PROJECT_ROOT
) -> None:
    """Rebuild from physical sources and require exact capsule equivalence."""

    validate_scenario_capsule_bundle_v2(bundle)
    root = project_root.expanduser().resolve()
    for index, raw_source in enumerate(_array(bundle.get("sources"), "bundle.sources")):
        source = _mapping(raw_source, f"bundle.sources[{index}]")
        for field in ("catalog", "reconstruction_feature", "raw_provenance"):
            artifact = _mapping(source.get(field), f"bundle.sources[{index}].{field}")
            path = _resolve_under_root(root, artifact.get("path"), f"{field}.path")
            if path.stat().st_size != _integer(artifact.get("size_bytes"), f"{field}.size"):
                raise FuryOfflineScenarioCapsuleError(f"sealed {field} byte size mismatch")
            if _sha256_file(path) != _sha(artifact.get("sha256"), f"{field}.sha256"):
                raise FuryOfflineScenarioCapsuleError(f"sealed {field} SHA-256 mismatch")
        for field in ("normalized_source", "raw_csv_source"):
            artifact = _mapping(source.get(field), f"bundle.sources[{index}].{field}")
            path = _resolve_under_root(root, artifact.get("path"), f"{field}.path")
            observed_size, observed_sha = _stable_file_identity(path, field)
            if observed_size != _integer(artifact.get("size_bytes"), f"{field}.size"):
                raise FuryOfflineScenarioCapsuleError(f"sealed {field} byte size mismatch")
            if observed_sha != _sha(
                artifact.get("byte_sha256"), f"{field}.byte_sha256"
            ):
                raise FuryOfflineScenarioCapsuleError(f"sealed {field} SHA-256 mismatch")
    source_contract = _mapping(bundle.get("source_contract"), "bundle.source_contract")
    manifest_path = _resolve_under_root(
        root,
        source_contract.get("corpus_manifest_path"),
        "bundle.source_contract.corpus_manifest_path",
    )
    rebuilt = build_scenario_capsule_bundle_v2(
        manifest_path,
        bucket=_text(bundle.get("bucket"), "bundle.bucket"),
        project_root=root,
    )
    if dict(bundle) != rebuilt:
        raise FuryOfflineScenarioCapsuleError(
            "sealed capsule differs from a complete rebuild of its physical sources"
        )


def write_scenario_capsule_bundle_v2(
    bundle: Mapping[str, Any],
    output_directory: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """Write only a capsule reproduced exactly from its retained local sources."""

    verify_capsule_bundle_sources_v2(bundle, project_root=project_root)
    digest = _mapping(bundle.get("content_address"), "bundle.content_address")["sha256"]
    directory = output_directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"fury_offline_scenario_capsules_v2.{digest}.json.gz"
    rendered = json.dumps(
        bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            zipped.write(rendered)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--bucket",
        choices=("main_comparison", "unknown_layout_sensitivity", "short_auxiliary"),
        default="main_comparison",
    )
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bundle = build_scenario_capsule_bundle_v2(
        args.manifest, bucket=args.bucket, project_root=args.project_root
    )
    output = write_scenario_capsule_bundle_v2(
        bundle,
        args.output_directory,
        project_root=args.project_root,
    )
    print(
        json.dumps(
            {
                "kind": "fury_offline_scenario_capsule_build_receipt_v2",
                "output": output.as_posix(),
                "output_size_bytes": output.stat().st_size,
                "output_file_sha256": _sha256_file(output),
                "content_address": bundle["content_address"],
                "summary": bundle["summary"],
                "historical_truth": False,
                "simulator_execution_started": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
