"""Bind the formal old-50 exact-Fury overlay to native dynamic-v3 scenarios.

This adapter is deliberately additive.  It reads the immutable 20-instance / 470-wave
overlay, validates every compressed partition in one sequential pass, and produces new
per-wave artifacts without modifying the overlay, the compact scenario capsule, or any
frozen policy artifact.

The focal player's strict-prefix state and current action label form the expert-training
projection.  Descriptive outcome references are not copied.  The exact-GUID leave-one-out
teammate schedule is bound only to the dynamic-v3 environment and is never exported as a
policy feature.  Every result remains development-only, non-voting, non-held-out,
comparison-ineligible, and deployment-ineligible.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from . import chronicle_old50_dynamic_v3_scenario_compiler_v1 as compiler_v1
from . import chronicle_old50_exact_fury_slot_overlay_v1 as overlay_v1
from . import chronicle_old50_warrior_slot_substitution_v2 as selector_v2
from . import chronicle_stage6_old50_overlap_hpc_v1 as overlap_v1
from . import chronicle_external_team_wave_model_v2 as model_v2
from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .fury_capsule_execution_binding_v2 import enumerate_capsule_scenarios_v2
from .fury_paired_multiseed_runner_v4 import normalize_runner_scenarios, sha256_json
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


JSONMap = dict[str, Any]

SCHEMA = "chronicle_old50_exact_fury_dynamic_v3_adapter/v1"
REVISION = "formal_overlay_exact_guid_loo_strict_prefix_dynamic_v3_v1"
STATUS = "DYNAMIC_V3_EXECUTABLE_EXPERT_TRAINING_DIAGNOSTIC_NONVOTING"
TRAINING_SCHEMA = "chronicle_old50_exact_fury_expert_training_projection/v1"
SCENARIO_MODEL_SCHEMA = "chronicle_old50_exact_fury_dynamic_v3_scenario_model/v1"
MATERIALIZED_MANIFEST_SCHEMA = (
    "chronicle_old50_exact_fury_dynamic_v3_adapter_manifest/v1"
)
MATERIALIZED_MANIFEST_STATUS = (
    "COMPLETE_COMPILE_VALIDATION_NO_SIMULATION_NONVOTING"
)
MATERIALIZED_MANIFEST_PREFIX = (
    "chronicle_old50_exact_fury_dynamic_v3_adapter_v1"
)

FORMAL_INSTANCE_COUNT = 20
FORMAL_WAVE_COUNT = 470
FORMAL_PDF_INSTANCE_COUNT = 6
FORMAL_GENERIC_INSTANCE_COUNT = 14
FORMAL_OVERLAY_MANIFEST_CONTENT_SHA256 = (
    "862d629a99abaf6e7c6a7e2c3426ba64ddf260f5afb018e878f43a4021a14806"
)
FORMAL_OVERLAY_MANIFEST_FILE_SHA256 = (
    "f01998e99666d4e33a3a35f0c2f38c67af9b82ab1f02825937bff58dcf0fc665"
)
HEALTH_BRANCH_FALLBACK_POLICY_V1 = (
    "proxy_center_else_repeat_center_else_legacy_threshold_25000_v1"
)
HEALTH_BRANCH_FALLBACK_ORDER_V1 = (
    "proxy_center",
    "repeat_center",
    "legacy_threshold_25000",
)
HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1 = {
    "policy_id": HEALTH_BRANCH_FALLBACK_POLICY_V1,
    "priority_order": list(HEALTH_BRANCH_FALLBACK_ORDER_V1),
    "historical_outcome_proxy_availability_used": True,
    "candidate_or_simulator_outcome_used_for_selection": False,
    "future_information_used_for_selection": False,
    "all_selected_values_are_sensitivity_hypotheses": True,
    "heldout_performance_evidence_eligible": False,
    "comparison_eligible": False,
    "purpose": "DEVELOPMENT_ONLY_SIMULATOR_HEALTH_SENSITIVITY_MATERIALIZATION",
}
FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1 = {
    "legacy_threshold_25000": 13,
    "proxy_center": 1258,
    "repeat_center": 1,
}
FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1 = {"expose_armor": 1700}
ARMOR_MAGNITUDE_CHOICE_PROJECTION_POLICY_V1 = (
    "corpus_global_choices_intersect_wave_observed_debuff_ids_v1"
)

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_ID_TOKEN_RE = re.compile(r"[^A-Za-z0-9._:-]+")

SCIENTIFIC_BOUNDARY = {
    "expert_training_discovery_only": True,
    "heldout_performance_evidence_eligible": False,
    "comparison_eligible": False,
    "comparison_outcome_eligible": False,
    "historical_policy_voting_eligible": False,
    "deployment_allowed": False,
    "diagnostic_nonvoting": True,
    "future_team_schedule_visible_to_policy": False,
    "teammate_schedule_environment_input_only": True,
    "descriptive_outcomes_exported_to_policy_input": False,
    "health_hypothesis_uses_historical_outcome_proxy_availability": True,
    "health_hypothesis_eligible_for_comparison": False,
    "policy_training_started": False,
    "simulator_run_started": False,
}

LIMITATION_CODES = (
    "OLD50_DEVELOPMENT_OVERLAY_NOT_HELDOUT",
    "PDF_LANE_SELECTION_IS_OUTCOME_CONDITIONED_TRAINING_DISCOVERY",
    "TARGET_HEALTH_IS_CAPSULE_SENSITIVITY_HYPOTHESIS",
    "HEALTH_BRANCH_AVAILABILITY_USES_HISTORICAL_OUTCOME_PROXIES",
    "BASE_AND_EFFECTIVE_ARMOR_ARE_SIMULATOR_HYPOTHESES",
    "ATTACKABILITY_IS_CAPSULE_WINDOW_HYPOTHESIS",
    "TARGET_CLASSIFICATION_IS_CALLER_SELECTED_HYPOTHESIS",
    "TEAM_SCHEDULE_IS_FIXED_OBSERVED_EXACT_GUID_LOO_NOT_ENDOGENOUS",
    "FUTURE_TEAM_EVENTS_ARE_ENVIRONMENT_ONLY",
    "OFFLINE_SCENARIO_NOT_REAL_CLIENT_FIDELITY",
)


class ChronicleOld50ExactFuryDynamicV3AdapterError(RuntimeError):
    """The formal overlay cannot be adapted without widening its claims."""


@dataclass(frozen=True)
class FormalExactFuryOverlayCorpusV1:
    manifest_path: Path
    manifest: JSONMap
    rows: tuple[JSONMap, ...]


@dataclass(frozen=True)
class ValidatedOld50CapsuleIndexV1:
    bundle_content_sha256: str
    scenarios_by_wave_key: Mapping[tuple[str, str, int], tuple[JSONMap, str]]


@dataclass(frozen=True)
class CompiledExactFuryDynamicV3ScenarioV1:
    artifact: JSONMap
    scenario: JSONMap
    dynamic_config: DynamicTargetSemanticsConfigV3


@dataclass(frozen=True)
class MaterializedExactFuryDynamicV3CorpusV1:
    manifest_path: Path
    addressed_manifest_path: Path
    manifest: JSONMap


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be nonempty text"
        )
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be an integer"
        )
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be {qualifier}"
        )
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be numeric"
        )
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or (positive and parsed <= 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be finite and {qualifier}"
        )
    return parsed


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _strict_json(value: Any, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"{label} is not strict JSON: {error}"
        ) from error


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = _strict_json(dict(value), "content-addressed document")
    if "content_address" in core:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "content-addressed core already contains content_address"
        )
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _load_json_path(path: str | Path, label: str) -> JSONMap:
    resolved = Path(path).expanduser().resolve()
    opener = gzip.open if resolved.suffix == ".gz" else open
    try:
        with opener(resolved, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"could not load {label} {resolved}: {error}"
        ) from error
    return dict(_mapping(value, label))


def _atomic_publish_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as error:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"could not inspect existing materialization file {path}: {error}"
            ) from error
        if existing == payload:
            return
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"existing materialization file differs: {path}"
        )
    if temporary.exists():
        temporary.unlink()
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"could not atomically publish {path}: {error}"
        ) from error


def _files_equal(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"could not compare materialized files: {error}"
        ) from error


def _publish_partition_or_reuse(temporary: Path, partition_path: Path) -> bool:
    """Publish a new partition, or reuse byte-identical prior completed work."""

    if partition_path.exists():
        if not _files_equal(temporary, partition_path):
            temporary.unlink()
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"existing materialized partition differs: {partition_path}"
            )
        temporary.unlink()
        return True
    os.replace(temporary, partition_path)
    return False


def _selector_semantics(lane: str) -> JSONMap:
    if lane == selector_v2.PDF_LANE:
        return {
            "leaderboard_membership_used_for_selection": True,
            "leaderboard_dps_rank_used_for_selection": True,
            "outcome_conditioned_discovery": True,
            "same_instance_ranking_dps_values_used": False,
            "generic_selection_outcome_free": False,
            "purpose": "TOP_PLAYER_EXPERT_TRAINING_DISCOVERY_ONLY",
            "heldout_performance_evidence_eligible": False,
            "comparison_outcome_eligible": False,
        }
    if lane == selector_v2.GENERIC_LANE:
        return {
            "leaderboard_membership_used_for_selection": False,
            "leaderboard_dps_rank_used_for_selection": False,
            "outcome_conditioned_discovery": False,
            "same_instance_ranking_dps_values_used": False,
            "generic_selection_outcome_free": True,
            "purpose": "OUTCOME_FREE_EXACT_FURY_IDENTITY_DISCOVERY_ONLY",
            "heldout_performance_evidence_eligible": False,
            "comparison_outcome_eligible": False,
        }
    raise ChronicleOld50ExactFuryDynamicV3AdapterError(
        f"unsupported overlay selection lane {lane!r}"
    )


def _validate_wave_for_adapter(value: Mapping[str, Any]) -> JSONMap:
    try:
        row = overlay_v1.validate_wave_overlay_v1(value)
    except Exception as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"invalid exact-Fury overlay wave: {error}"
        ) from error
    selection = _mapping(row.get("slot_selection"), "slot_selection")
    lane = _text(selection.get("selection_lane"), "selection_lane")
    if dict(_mapping(selection.get("selection_semantics"), "selection_semantics")) != (
        _selector_semantics(lane)
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay selector semantics are not the frozen exact lane contract"
        )
    boundary = _mapping(row.get("scientific_boundary"), "wave scientific boundary")
    required_false = (
        "heldout_performance_evidence_eligible",
        "comparison_outcome_eligible",
        "historical_policy_voting_eligible",
        "future_team_schedule_visible_to_policy",
        "policy_training_started",
        "simulator_run_started",
        "deployment_ready",
    )
    if (
        boundary.get("expert_training_discovery_episode_eligible") is not True
        or boundary.get("teammate_schedule_environment_input_only") is not True
        or any(boundary.get(field) is not False for field in required_false)
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay wave scientific boundary is not training-only and non-voting"
        )
    return row


def _validate_formal_manifest(value: Mapping[str, Any]) -> JSONMap:
    try:
        manifest = overlay_v1.validate_overlay_manifest_v1(value)
    except Exception as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"invalid exact-Fury overlay manifest: {error}"
        ) from error
    summary = _mapping(manifest.get("summary"), "overlay manifest summary")
    required_counts = {
        "instance_count": FORMAL_INSTANCE_COUNT,
        "wave_overlay_count": FORMAL_WAVE_COUNT,
        "pdf_outcome_conditioned_instance_count": FORMAL_PDF_INSTANCE_COUNT,
        "generic_outcome_free_instance_count": FORMAL_GENERIC_INSTANCE_COUNT,
        "selected_guid_missing_wave_count": 0,
    }
    if any(summary.get(key) != expected for key, expected in required_counts.items()):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay manifest is not the complete 20-instance / 470-wave formal corpus"
        )
    if summary.get("selection_lane_instance_counts") != {
        selector_v2.GENERIC_LANE: FORMAL_GENERIC_INSTANCE_COUNT,
        selector_v2.PDF_LANE: FORMAL_PDF_INSTANCE_COUNT,
    }:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay manifest lane counts differ from the frozen 6 PDF / 14 generic split"
        )
    pdf_waves = _integer(
        summary.get("pdf_outcome_conditioned_wave_count"), "PDF wave count"
    )
    generic_waves = _integer(
        summary.get("generic_outcome_free_wave_count"), "generic wave count"
    )
    if pdf_waves + generic_waves != FORMAL_WAVE_COUNT:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay lane wave counts do not close to 470"
        )
    boundary = _mapping(manifest.get("scientific_boundary"), "manifest boundary")
    required_false = (
        "heldout_performance_evidence_eligible",
        "comparison_outcome_eligible",
        "historical_policy_voting_eligible",
        "future_teammate_schedule_visible_to_policy",
        "policy_training_started",
        "simulator_run_started",
        "comparison_started",
        "deployment_started",
    )
    if (
        boundary.get("expert_training_discovery_overlay_complete") is not True
        or boundary.get("pdf_lane_training_only_and_outcome_conditioned") is not True
        or boundary.get("generic_lane_outcome_free") is not True
        or any(boundary.get(field) is not False for field in required_false)
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "formal overlay manifest widened its scientific boundary"
        )
    execution = _mapping(manifest.get("execution_boundary"), "execution boundary")
    if execution != {
        "network_requests_made": 0,
        "heavy_jobs_started": False,
        "policy_training_started": False,
        "simulator_runs_started": False,
        "comparison_started": False,
        "deployment_started": False,
        "frozen_historical_policy_v2_v5_modified": False,
    }:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "formal overlay execution boundary widened"
        )
    return manifest


def _binding_matches_manifest(row: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    bindings = _mapping(row.get("source_bindings"), "wave source bindings")
    sources = _mapping(manifest.get("source_bindings"), "manifest source bindings")
    expected = {
        "selector_v2_content_sha256": _mapping(
            sources.get("selector_v2"), "manifest selector_v2"
        ).get("content_sha256"),
        "stage5_manifest_content_sha256": _mapping(
            sources.get("stage5_manifest"), "manifest stage5"
        ).get("content_sha256"),
        "stage6_manifest_content_sha256": _mapping(
            sources.get("stage6_manifest"), "manifest stage6"
        ).get("content_sha256"),
        "old50_capsule_content_sha256": sources.get(
            "old50_capsule_content_sha256"
        ),
    }
    if any(bindings.get(field) != expected_value for field, expected_value in expected.items()):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay wave source closure differs from its formal manifest"
        )


def load_formal_exact_fury_overlay_corpus_v1(
    manifest_path: str | Path,
) -> FormalExactFuryOverlayCorpusV1:
    """Load and verify all 470 rows with exactly one sequential read per partition."""

    resolved = Path(manifest_path).expanduser().resolve()
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            raw_manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"could not load overlay manifest {resolved}: {error}"
        ) from error
    manifest = _validate_formal_manifest(_mapping(raw_manifest, "overlay manifest"))
    try:
        manifest_file_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"could not bind formal overlay manifest bytes {resolved}: {error}"
        ) from error
    if (
        manifest["content_address"]["sha256"]
        != FORMAL_OVERLAY_MANIFEST_CONTENT_SHA256
        or manifest_file_sha != FORMAL_OVERLAY_MANIFEST_FILE_SHA256
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay manifest is not the pinned published 20/470 formal corpus"
        )
    rows: list[JSONMap] = []
    seen_keys: set[tuple[Any, ...]] = set()
    entries = {
        _text(entry.get("instance_id"), "manifest instance_id"): entry
        for entry in (
            _mapping(value, "overlay instance entry")
            for value in _array(manifest.get("instances"), "manifest instances")
        )
    }
    for raw_instance_id in _array(manifest.get("instance_order"), "instance_order"):
        instance_id = _text(raw_instance_id, "instance_order value")
        entry = _mapping(entries.get(instance_id), f"instance entry {instance_id}")
        descriptor = _mapping(entry.get("partition"), "overlay partition descriptor")
        relative = _text(descriptor.get("path"), "overlay partition path")
        if Path(relative).name != relative:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "overlay partitions must be colocated basenames"
            )
        partition_path = resolved.parent / relative
        logical = hashlib.sha256()
        logical_size = 0
        record_count = 0
        entry_rows: list[JSONMap] = []
        try:
            with partition_path.open("rb") as source:
                hashing_raw = overlap_v1._HashingRaw(source)
                with io.BufferedReader(hashing_raw) as buffered:
                    with gzip.GzipFile(fileobj=buffered, mode="rb") as handle:
                        for line_number, raw_line in enumerate(handle, 1):
                            logical.update(raw_line)
                            logical_size += len(raw_line)
                            record_count += 1
                            try:
                                value = json.loads(raw_line.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    f"invalid overlay row {instance_id}:{line_number}: {error}"
                                ) from error
                            row = _validate_wave_for_adapter(
                                _mapping(value, "overlay partition row")
                            )
                            if raw_line != _canonical(row) + b"\n":
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "overlay partition contains noncanonical JSONL"
                                )
                            identity = _mapping(row.get("identity"), "wave identity")
                            selection = _mapping(
                                row.get("slot_selection"), "wave slot selection"
                            )
                            entry_selector = _mapping(
                                entry.get("selector"), "instance selector"
                            )
                            if (
                                identity.get("instance_id") != instance_id
                                or identity.get("selected_guid")
                                != entry_selector.get("selected_guid")
                                or selection.get("selection_lane")
                                != entry_selector.get("selection_lane")
                                or selection.get("selector_row_content_sha256")
                                != entry_selector.get("selector_row_content_sha256")
                            ):
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "overlay row crossed its instance selector boundary"
                                )
                            _binding_matches_manifest(row, manifest)
                            overlay_key = tuple(
                                _array(identity.get("overlay_key"), "overlay_key")
                            )
                            if overlay_key in seen_keys:
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "formal overlay contains a duplicate exact wave/GUID key"
                                )
                            seen_keys.add(overlay_key)
                            entry_rows.append(row)
        except OSError as error:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"could not read overlay partition {partition_path}: {error}"
            ) from error
        if (
            hashing_raw.bytes_read != descriptor.get("compressed_size_bytes")
            or hashing_raw.digest.hexdigest()
            != descriptor.get("compressed_file_sha256")
            or record_count != descriptor.get("record_count")
            or logical_size != descriptor.get("logical_size_bytes")
            or logical.hexdigest() != descriptor.get("logical_content_sha256")
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "overlay partition count, size, or hash differs after its single scan"
            )
        entry_selector = _mapping(entry.get("selector"), "instance selector")
        if (
            record_count
            != _mapping(entry.get("summary"), "instance summary").get(
                "wave_overlay_count"
            )
            or record_count != entry_selector.get("accepted_wave_count")
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "overlay instance record count differs from its selector receipt"
            )
        rows.extend(entry_rows)
    if len(rows) != FORMAL_WAVE_COUNT or len(seen_keys) != FORMAL_WAVE_COUNT:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "verified overlay rows do not close to exactly 470 unique episodes"
        )
    return FormalExactFuryOverlayCorpusV1(
        manifest_path=resolved,
        manifest=deepcopy(manifest),
        rows=tuple(deepcopy(rows)),
    )


def build_validated_old50_capsule_index_v1(
    capsule_bundle: Mapping[str, Any],
) -> ValidatedOld50CapsuleIndexV1:
    """Validate the compact capsule once before compiling many overlay waves."""

    try:
        scenarios = enumerate_capsule_scenarios_v2(capsule_bundle)
    except Exception as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"invalid old-50 compact capsule: {error}"
        ) from error
    bundle_sha = _sha(
        _mapping(capsule_bundle.get("content_address"), "capsule content address").get(
            "sha256"
        ),
        "capsule content SHA-256",
    )
    catalogs = {
        _sha(source.get("source_bundle_id"), "capsule source_bundle_id"): _sha(
            _mapping(source.get("catalog"), "capsule source catalog").get("sha256"),
            "capsule catalog SHA-256",
        )
        for source in (
            _mapping(value, "capsule source")
            for value in _array(capsule_bundle.get("sources"), "capsule sources")
        )
    }
    indexed: dict[tuple[str, str, int], tuple[JSONMap, str]] = {}
    for scenario in scenarios:
        source = _mapping(scenario.get("source_identity"), "capsule source identity")
        instance_id = _text(source.get("instance_id"), "capsule instance_id")
        encounter_id = _text(source.get("encounter_id"), "capsule encounter_id")
        wave_ordinal = _integer(source.get("wave_ordinal"), "capsule wave_ordinal")
        wave_id = _text(source.get("wave_id"), "capsule wave_id")
        if wave_id != f"{encounter_id}:wave:{wave_ordinal}":
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compact capsule wave ID is not its frozen v1 derived namespace"
            )
        key = (
            instance_id,
            encounter_id,
            wave_ordinal,
        )
        source_id = _sha(scenario.get("source_bundle_id"), "source_bundle_id")
        if source_id not in catalogs:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "capsule scenario source catalog is missing"
            )
        if key in indexed:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compact capsule duplicates an exact wave identity"
            )
        indexed[key] = (deepcopy(scenario), catalogs[source_id])
    return ValidatedOld50CapsuleIndexV1(
        bundle_content_sha256=bundle_sha,
        scenarios_by_wave_key=indexed,
    )


def _find_capsule_scenario(
    *,
    overlay_wave: Mapping[str, Any],
    capsule_bundle: Mapping[str, Any],
    capsule_index: ValidatedOld50CapsuleIndexV1 | None,
) -> tuple[JSONMap, str]:
    index = capsule_index or build_validated_old50_capsule_index_v1(capsule_bundle)
    if not isinstance(index, ValidatedOld50CapsuleIndexV1):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "capsule_index must be a validated old-50 capsule index"
        )
    declared_bundle_sha = _sha(
        _mapping(capsule_bundle.get("content_address"), "capsule content address").get(
            "sha256"
        ),
        "capsule content SHA-256",
    )
    bindings = _mapping(overlay_wave.get("source_bindings"), "wave source bindings")
    if (
        bindings.get("old50_capsule_content_sha256")
        != index.bundle_content_sha256
        or declared_bundle_sha != index.bundle_content_sha256
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay wave is not bound to the validated old-50 capsule"
        )
    identity = _mapping(overlay_wave.get("identity"), "overlay identity")
    instance_id = _text(identity.get("instance_id"), "overlay instance_id")
    encounter_id = _text(identity.get("encounter_id"), "overlay encounter_id")
    wave_ordinal = _integer(identity.get("wave_ordinal"), "overlay wave_ordinal")
    external_wave_id = _text(
        identity.get("external_wave_id"), "overlay external_wave_id"
    )
    if external_wave_id != f"{encounter_id}:external-v2-wave:{wave_ordinal}":
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay wave ID is not its frozen external-v2 derived namespace"
        )
    key = (
        instance_id,
        encounter_id,
        wave_ordinal,
    )
    resolved = index.scenarios_by_wave_key.get(key)
    if resolved is None:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay key does not resolve to exactly one compact capsule scenario"
        )
    scenario, catalog_sha = resolved
    selected_guids = _array(
        _mapping(overlay_wave.get("target_contract"), "target contract").get(
            "selected_target_guids"
        ),
        "selected target GUIDs",
    )
    targets = [
        _mapping(value, "capsule target")
        for value in _array(scenario.get("targets"), "capsule targets")
    ]
    if [target.get("target_guid") for target in targets] != selected_guids:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "overlay target remap does not equal the capsule target order"
        )
    return deepcopy(scenario), catalog_sha


def _compile_overlay_background(
    *, overlay_wave: Mapping[str, Any], target_guids: Sequence[str], horizon_ms: int
) -> tuple[tuple[BackgroundDamageEventV1, ...], JSONMap]:
    identity = _mapping(overlay_wave.get("identity"), "overlay identity")
    focal_guid = _text(identity.get("selected_guid"), "selected focal GUID")
    projection = _mapping(
        overlay_wave.get("exact_guid_loo_teammate_schedule"),
        "exact-GUID LOO teammate schedule",
    )
    schedule = [
        _mapping(value, "projected teammate event")
        for value in _array(projection.get("projected_schedule"), "projected schedule")
    ]
    typed: list[BackgroundDamageEventV1] = []
    for index, event in enumerate(schedule):
        target_index = _integer(event.get("target_index"), "background target index")
        time_ms = _integer(event.get("time_ms"), "background time_ms")
        if target_index >= len(target_guids) or event.get("target_guid") != target_guids[
            target_index
        ]:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "projected teammate event differs from the capsule target order"
            )
        if time_ms > horizon_ms:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "projected teammate event exceeds the capsule horizon"
            )
        if (
            event.get("actor_player_guid") == focal_guid
            or event.get("runtime_eligible") is not True
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "projected environment schedule retained focal or ineligible damage"
            )
        try:
            typed.append(
                BackgroundDamageEventV1(
                    schedule_index=index,
                    time_ms=time_ms,
                    target_index=target_index,
                    event_id=_text(event.get("event_id"), "background event_id"),
                    damage=_number(event.get("damage"), "background damage", positive=True),
                )
            )
        except (TypeError, ValueError) as error:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"invalid dynamic-v3 background event: {error}"
            ) from error
    if (
        len(typed) != projection.get("projected_event_count")
        or sum(row.damage for row in typed) != projection.get("projected_damage")
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "typed background schedule differs from the exact overlay receipt"
        )
    loo = _mapping(projection.get("exact_guid_leave_one_out"), "exact GUID LOO")
    return tuple(typed), {
        "selection_unit": "SAME_EXACT_OVERLAY_WAVE",
        "overlay_leave_one_out_applied_upstream": True,
        "focal_player_guid": focal_guid,
        "exact_guid_leave_one_out": True,
        "kept_background_event_count": len(typed),
        "kept_background_damage": sum(row.damage for row in typed),
        "excluded_focal_event_count": _integer(
            loo.get("excluded_event_count"), "excluded focal event count"
        ),
        "excluded_focal_damage": _number(
            loo.get("excluded_damage"), "excluded focal damage"
        ),
        "component_wide_random_draw_used": False,
        "future_schedule_exported_to_policy_context": False,
        "policy_runtime_receives_bridge_state_only": True,
    }


def _expert_training_projection(overlay_wave: Mapping[str, Any]) -> JSONMap:
    episode = _mapping(
        overlay_wave.get("focal_player_episode"), "focal player episode"
    )
    transitions = [
        _mapping(value, "prefix transition")
        for value in _array(episode.get("prefix_transitions"), "prefix transitions")
    ]
    samples: list[JSONMap] = []
    for sample_index, transition in enumerate(transitions):
        label = _mapping(transition.get("current_event_label"), "current action label")
        if label.get("learning_role") != "OBSERVED_ACTION_LABEL":
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "expert label is not an observed action label"
            )
        state = _mapping(transition.get("state_before"), "strict-prefix state")
        samples.append(
            {
                "sample_index": sample_index,
                "trace_index": _integer(
                    transition.get("trace_index"), "transition trace_index"
                ),
                "order_key": deepcopy(
                    _array(transition.get("order_key"), "transition order_key")
                ),
                "input_state_strict_prefix": deepcopy(dict(state)),
                "observed_action_label": deepcopy(dict(label)),
            }
        )
    selection = _mapping(overlay_wave.get("slot_selection"), "slot selection")
    lane = _text(selection.get("selection_lane"), "selection lane")
    identity = _mapping(overlay_wave.get("identity"), "overlay identity")
    return {
        "schema": TRAINING_SCHEMA,
        "selected_guid": _text(identity.get("selected_guid"), "selected GUID"),
        "selection_lane": lane,
        "selection_is_outcome_conditioned": lane == selector_v2.PDF_LANE,
        "sample_count": len(samples),
        "samples": samples,
        "strict_prefix_features_only": True,
        "current_action_is_label_not_input": True,
        "descriptive_outcome_context_included": False,
        "future_teammate_schedule_included": False,
        "heldout_performance_evidence_eligible": False,
        "comparison_eligible": False,
        "deployment_allowed": False,
    }


def _position_hypothesis_id(scenario: Mapping[str, Any]) -> str:
    layout = _mapping(scenario.get("layout"), "scenario layout")
    spatial = _mapping(layout.get("spatial_assumption"), "spatial assumption")
    value = _text(spatial.get("value"), "spatial assumption value")
    token = _ID_TOKEN_RE.sub("-", value).strip("-")
    if not token:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "spatial hypothesis cannot form a stable identifier"
        )
    return f"old50-position-{token}-v1"


def _select_health_rows(
    targets: Sequence[Mapping[str, Any]], health_branch_selection_policy: str
) -> list[
    tuple[int, Mapping[str, Any], tuple[str, ...], str, tuple[JSONMap, ...]]
]:
    if health_branch_selection_policy != HEALTH_BRANCH_FALLBACK_POLICY_V1:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "unsupported health branch selection policy"
        )
    reasons = {
        "proxy_center": "PRIMARY_PROXY_CENTER_AVAILABLE",
        "repeat_center": "PRIMARY_MISSING_REPEAT_CENTER_FALLBACK",
        "legacy_threshold_25000": (
            "PRIMARY_AND_REPEAT_MISSING_LEGACY_THRESHOLD_25000_FALLBACK"
        ),
    }
    selected: list[
        tuple[int, Mapping[str, Any], tuple[str, ...], str, tuple[JSONMap, ...]]
    ] = []
    for target_index, target in enumerate(targets):
        family = _mapping(
            target.get("max_health_hypothesis_family"),
            f"target[{target_index}] health family",
        )
        branches = [
            _mapping(value, "health branch")
            for value in _array(
                family.get("shared_branch_family"), "shared health branches"
            )
        ]
        available = tuple(
            sorted(
                _text(row.get("branch_id"), "health branch_id")
                for row in branches
                if row.get("use_health") is True and row.get("max_health") is not None
            )
        )
        branch_by_id = {
            _text(row.get("branch_id"), "health branch_id"): row
            for row in branches
            if row.get("use_health") is True and row.get("max_health") is not None
        }
        provenance = tuple(
            {
                "branch_id": branch_id,
                "declared_source": branch_by_id[branch_id].get("source"),
                "declared_status": branch_by_id[branch_id].get("status"),
                "availability_from_historical_capsule": True,
                "historical_truth": False,
            }
            for branch_id in available
        )
        branch = next(
            (candidate for candidate in HEALTH_BRANCH_FALLBACK_ORDER_V1 if candidate in available),
            None,
        )
        if branch is None:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "health fallback policy cannot select a declared target branch"
            )
        value, selected_branch = compiler_v1._select_health(target, branch)
        selected.append(
            (value, selected_branch, available, reasons[branch], provenance)
        )
    return selected


def _project_armor_magnitude_choices(
    targets: Sequence[Mapping[str, Any]],
    global_choices: Mapping[str, int | float] | None,
) -> tuple[JSONMap, JSONMap, tuple[str, ...]]:
    normalized = _strict_json(
        dict(global_choices or {}), "corpus-global armor magnitude choices"
    )
    observed: set[str] = set()
    for target_index, target in enumerate(targets):
        armor = _mapping(target.get("armor"), f"target[{target_index}] armor")
        for transition in _array(
            armor.get("observed_transitions"),
            f"target[{target_index}] armor transitions",
        ):
            observed.add(
                _text(
                    _mapping(transition, "armor transition").get("debuff_id"),
                    "armor transition debuff_id",
                )
            )
    ordered_observed = tuple(sorted(observed))
    projected = {
        debuff_id: normalized[debuff_id]
        for debuff_id in ordered_observed
        if debuff_id in normalized
    }
    return normalized, projected, ordered_observed


def _validate_wave_identity_join(value: Any) -> JSONMap:
    join = dict(_mapping(value, "wave identity join"))
    if set(join) != {
        "join_key",
        "overlay_external_wave_id",
        "capsule_wave_id",
        "namespace_translation",
        "ordinal_only_join_allowed",
    }:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "wave identity join field set differs"
        )
    key = _array(join.get("join_key"), "wave identity join key")
    if len(key) != 3:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "wave identity join key must contain instance, encounter, and ordinal"
        )
    _text(key[0], "wave identity instance_id")
    encounter_id = _text(key[1], "wave identity encounter_id")
    wave_ordinal = _integer(key[2], "wave identity wave_ordinal")
    if (
        join.get("overlay_external_wave_id")
        != f"{encounter_id}:external-v2-wave:{wave_ordinal}"
        or join.get("capsule_wave_id") != f"{encounter_id}:wave:{wave_ordinal}"
        or join.get("namespace_translation") != "DERIVED_ID_NAMESPACE_ONLY"
        or join.get("ordinal_only_join_allowed") is not False
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "wave identity namespaces do not share one exact encounter/ordinal join"
        )
    return join


def compile_exact_fury_overlay_dynamic_v3_v1(
    *,
    overlay_wave: Mapping[str, Any],
    overlay_manifest_content_sha256: str,
    capsule_bundle: Mapping[str, Any],
    base_request: Mapping[str, Any],
    equipped_item_names: Sequence[str],
    target_level: int,
    initial_base_armor: int | float,
    health_branch_selection_policy: str,
    attackability_branch_id: str,
    target_classification: str,
    armor_magnitude_by_debuff: Mapping[str, int | float] | None = None,
    capsule_index: ValidatedOld50CapsuleIndexV1 | None = None,
) -> CompiledExactFuryDynamicV3ScenarioV1:
    """Compile one verified overlay episode into a native dynamic-v3 scenario."""

    row = _validate_wave_for_adapter(overlay_wave)
    manifest_sha = _sha(
        overlay_manifest_content_sha256, "overlay manifest content SHA-256"
    )
    level = _integer(target_level, "target_level", positive=True)
    base_armor = _number(initial_base_armor, "initial_base_armor", positive=True)
    health_policy = _text(
        health_branch_selection_policy, "health_branch_selection_policy"
    )
    attack_branch = _text(attackability_branch_id, "attackability_branch_id")
    scenario_source, catalog_sha = _find_capsule_scenario(
        overlay_wave=row,
        capsule_bundle=capsule_bundle,
        capsule_index=capsule_index,
    )
    targets = [
        _mapping(value, "capsule target")
        for value in _array(scenario_source.get("targets"), "capsule targets")
    ]
    target_guids = [_text(target.get("target_guid"), "target GUID") for target in targets]
    horizon_ms = _integer(
        _mapping(scenario_source.get("horizon"), "scenario horizon").get(
            "milliseconds"
        ),
        "scenario horizon milliseconds",
        positive=True,
    )
    try:
        health_rows = _select_health_rows(targets, health_policy)
        target_health = [row[0] for row in health_rows]
        global_armor_choices, wave_armor_choices, observed_armor_debuff_ids = (
            _project_armor_magnitude_choices(
                targets, armor_magnitude_by_debuff
            )
        )
        request = compiler_v1._compile_request(
            base_request=base_request,
            targets=targets,
            target_health=target_health,
            target_level=level,
            base_armor=base_armor,
            horizon_ms=horizon_ms,
        )
        attackability_events, attackability_receipt = (
            compiler_v1._compile_attackability_events(
                targets, branch_id=attack_branch, horizon_ms=horizon_ms
            )
        )
        armor_events, armor_receipt = compiler_v1._compile_armor_events(
            targets,
            base_armor=base_armor,
            magnitude_choices=wave_armor_choices,
            horizon_ms=horizon_ms,
        )
    except Exception as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"capsule sensitivity hypothesis cannot be compiled: {error}"
        ) from error
    armor_receipt = {
        **armor_receipt,
        "magnitude_choice_projection": {
            "policy_id": ARMOR_MAGNITUDE_CHOICE_PROJECTION_POLICY_V1,
            "corpus_global_magnitude_choices": global_armor_choices,
            "wave_observed_debuff_ids": list(observed_armor_debuff_ids),
            "projected_magnitude_choices": wave_armor_choices,
        },
    }
    background_events, background_receipt = _compile_overlay_background(
        overlay_wave=row, target_guids=target_guids, horizon_ms=horizon_ms
    )
    try:
        dynamic_config = DynamicTargetSemanticsConfigV3(
            target_health=tuple(
                DynamicTargetHealthV1(index, float(value))
                for index, value in enumerate(target_health)
            ),
            idle_advance_horizon_ms=horizon_ms,
            background_damage_events=background_events,
            attackability_events=attackability_events,
            effective_armor_events=armor_events,
        )
    except (TypeError, ValueError) as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"native dynamic-v3 rejected the overlay environment: {error}"
        ) from error
    request_sha = sha256_json(request)
    capsule_sha = _sha(scenario_source.get("capsule_sha256"), "scenario capsule SHA")
    context_bundle = compiler_v1._target_context_bundle(
        targets=targets,
        health=target_health,
        target_classification=target_classification,
        equipped_item_names=equipped_item_names,
        request_sha256=request_sha,
        capsule_scenario_sha256=capsule_sha,
        health_hypothesis_id=f"old50-{health_policy}",
        position_hypothesis_id=_position_hypothesis_id(scenario_source),
    )
    context_bundle.update(
        {
            "heldout_performance_evidence_eligible": False,
            "historical_policy_voting_eligible": False,
            "future_team_schedule_visible_to_policy": False,
            "deployment_allowed": False,
        }
    )
    identity = _mapping(row.get("identity"), "overlay identity")
    selection = _mapping(row.get("slot_selection"), "slot selection")
    row_sha = _sha(
        _mapping(row.get("content_address"), "overlay row content address").get(
            "sha256"
        ),
        "overlay row content SHA-256",
    )
    source = _mapping(scenario_source.get("source_identity"), "scenario source identity")
    projection = _mapping(
        scenario_source.get("runner_projection"), "runner projection"
    )
    provenance = _mapping(
        scenario_source.get("provenance_hashes"), "scenario provenance hashes"
    )
    component = _mapping(
        _mapping(row.get("split_authority"), "split authority").get(
            "overlap_component_join"
        ),
        "overlap component join",
    )
    wave_identity_join = {
        "join_key": [
            _text(identity.get("instance_id"), "instance_id"),
            _text(identity.get("encounter_id"), "encounter_id"),
            _integer(identity.get("wave_ordinal"), "wave_ordinal"),
        ],
        "overlay_external_wave_id": _text(
            identity.get("external_wave_id"), "overlay external_wave_id"
        ),
        "capsule_wave_id": _text(source.get("wave_id"), "capsule wave_id"),
        "namespace_translation": "DERIVED_ID_NAMESPACE_ONLY",
        "ordinal_only_join_allowed": False,
    }
    hypothesis_selection = {
        "target_level": level,
        "target_level_status": "CALLER_SELECTED_SENSITIVITY_HYPOTHESIS",
        "initial_base_armor": base_armor,
        "initial_base_armor_status": "CALLER_SELECTED_SENSITIVITY_HYPOTHESIS",
        "health_branch_selection_policy": deepcopy(
            HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1
        ),
        "health_by_target": [
            {
                "target_index": index,
                "target_guid": target_guids[index],
                "max_health": target_health[index],
                "selected_branch_id": health_rows[index][1].get("branch_id"),
                "available_branch_ids": list(health_rows[index][2]),
                "selection_reason": health_rows[index][3],
                "available_branch_provenance": list(health_rows[index][4]),
                "source": health_rows[index][1].get("source"),
            }
            for index in range(len(targets))
        ],
        "attackability": attackability_receipt,
        "armor": armor_receipt,
        "target_classification": _text(
            target_classification, "target_classification"
        ),
        "classification_is_historical_truth": False,
        "position_is_historical_truth": False,
    }
    compilation_identity_sha = sha256_json(
        {
            "overlay_wave_content_sha256": row_sha,
            "capsule_scenario_sha256": capsule_sha,
            "wave_identity_join": wave_identity_join,
            "selected_guid": identity["selected_guid"],
            "selection_lane": selection["selection_lane"],
            "request_sha256": request_sha,
            "dynamic_config_content_sha256": dynamic_config.content_sha256,
            "target_context_bundle_sha256": sha256_json(context_bundle),
            "hypothesis_selection": hypothesis_selection,
        }
    )
    scenario_model = {
        "schema": SCENARIO_MODEL_SCHEMA,
        "status": STATUS,
        "request_sha256": request_sha,
        "bridge_execution_eligible": True,
        "historical_truth": False,
        "comparison_eligible": False,
        "heldout_performance_evidence_eligible": False,
        "historical_policy_voting_eligible": False,
        "deployment_allowed": False,
        "future_team_schedule_visible_to_policy": False,
        "source": {
            "overlay_manifest_content_sha256": manifest_sha,
            "overlay_wave_content_sha256": row_sha,
            "capsule_bundle_content_sha256": _mapping(
                capsule_bundle.get("content_address"), "capsule content address"
            )["sha256"],
            "capsule_scenario_sha256": capsule_sha,
            "focal_player_guid": identity["selected_guid"],
            "selection_lane": selection["selection_lane"],
            "wave_identity_join": wave_identity_join,
            "compilation_identity_sha256": compilation_identity_sha,
        },
        "hypothesis_selection": hypothesis_selection,
        "causal_background_projection": background_receipt,
        "limitation_codes": list(LIMITATION_CODES),
    }
    scenario_id = (
        f"old50-exact-fury-{row_sha[:16]}-compile-{compilation_identity_sha[:24]}"
    )
    candidate = {
        "instance_id": _text(identity.get("instance_id"), "instance_id"),
        "component_id": _text(
            component.get("stage6_component_id"), "stage6 component_id"
        ),
        "scenario_id": scenario_id,
        "stratum": _text(projection.get("stratum"), "scenario stratum"),
        "scenario_weight": _number(
            projection.get("scenario_weight"), "scenario weight", positive=True
        ),
        "horizon_ms": horizon_ms,
        "estimated_cost_units": max(
            1, horizon_ms * len(targets) + len(background_events)
        ),
        "request": request,
        "dynamic_load_config": dynamic_config.to_wire(),
        "scenario_model": scenario_model,
        "target_context_bundle": context_bundle,
        "corpus_entry_sha256": _sha(
            provenance.get("corpus_entry_sha256"), "corpus entry SHA-256"
        ),
        "source_scenario_sha256": _sha(
            provenance.get("source_scenario_sha256"), "source scenario SHA-256"
        ),
        "catalog_sha256": catalog_sha,
    }
    try:
        normalized = normalize_runner_scenarios([candidate])[0]
    except Exception as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"runner-v4 rejected the compiled overlay scenario: {error}"
        ) from error
    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": STATUS,
        "source_bindings": {
            "overlay_manifest_content_sha256": manifest_sha,
            "overlay_wave_content_sha256": row_sha,
            "capsule_bundle_content_sha256": _mapping(
                capsule_bundle.get("content_address"), "capsule content address"
            )["sha256"],
            "capsule_scenario_sha256": capsule_sha,
            "stage5_wave_content_sha256": _mapping(
                row.get("source_bindings"), "wave source bindings"
            )["stage5_wave_content_sha256"],
            "stage6_block_content_sha256": _mapping(
                row.get("source_bindings"), "wave source bindings"
            )["stage6_block_content_sha256"],
            "wave_identity_join": wave_identity_join,
        },
        "expert_training_projection": _expert_training_projection(row),
        "environment_projection": background_receipt,
        "hypothesis_selection": hypothesis_selection,
        "scenario": normalized,
        "scientific_boundary": deepcopy(SCIENTIFIC_BOUNDARY),
    }
    artifact = deepcopy(core)
    artifact["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_exact_fury_overlay_dynamic_v3_structure_v1(
        artifact, source_overlay_wave=row
    )
    return CompiledExactFuryDynamicV3ScenarioV1(
        artifact=artifact,
        scenario=normalized,
        dynamic_config=dynamic_config,
    )


def validate_exact_fury_overlay_dynamic_v3_structure_v1(
    value: Mapping[str, Any],
    *,
    source_overlay_wave: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Check one record's internal/source-row structure, not full provenance."""
    raw = _strict_json(value, "compiled exact-Fury dynamic-v3 artifact")
    expected_fields = {
        "schema",
        "revision",
        "status",
        "source_bindings",
        "expert_training_projection",
        "environment_projection",
        "hypothesis_selection",
        "scenario",
        "scientific_boundary",
        "content_address",
    }
    if set(raw) != expected_fields or (
        raw.get("schema") != SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != STATUS
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled adapter schema or field set differs"
        )
    address = _mapping(raw.get("content_address"), "content address")
    core = {key: item for key, item in raw.items() if key != "content_address"}
    if address != {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled adapter content address differs"
        )
    if raw.get("scientific_boundary") != SCIENTIFIC_BOUNDARY:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled adapter scientific boundary widened"
        )
    bindings = _mapping(raw.get("source_bindings"), "source bindings")
    for field in (
        "overlay_manifest_content_sha256",
        "overlay_wave_content_sha256",
        "capsule_bundle_content_sha256",
        "capsule_scenario_sha256",
        "stage5_wave_content_sha256",
        "stage6_block_content_sha256",
    ):
        _sha(bindings.get(field), field)
    binding_join = _validate_wave_identity_join(bindings.get("wave_identity_join"))
    training = _mapping(
        raw.get("expert_training_projection"), "expert training projection"
    )
    expected_training_fields = {
        "schema",
        "selected_guid",
        "selection_lane",
        "selection_is_outcome_conditioned",
        "sample_count",
        "samples",
        "strict_prefix_features_only",
        "current_action_is_label_not_input",
        "descriptive_outcome_context_included",
        "future_teammate_schedule_included",
        "heldout_performance_evidence_eligible",
        "comparison_eligible",
        "deployment_allowed",
    }
    lane = training.get("selection_lane")
    samples = _array(training.get("samples"), "training samples")
    if (
        set(training) != expected_training_fields
        or training.get("schema") != TRAINING_SCHEMA
        or lane not in selector_v2.SELECTION_LANES
        or training.get("selection_is_outcome_conditioned")
        is not (lane == selector_v2.PDF_LANE)
        or training.get("sample_count") != len(samples)
        or training.get("strict_prefix_features_only") is not True
        or training.get("current_action_is_label_not_input") is not True
        or any(
            training.get(field) is not False
            for field in (
                "descriptive_outcome_context_included",
                "future_teammate_schedule_included",
                "heldout_performance_evidence_eligible",
                "comparison_eligible",
                "deployment_allowed",
            )
        )
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "expert training projection widened its feature or claim boundary"
        )
    prior: tuple[int, ...] | None = None
    for index, value_sample in enumerate(samples):
        sample = _mapping(value_sample, "training sample")
        if set(sample) != {
            "sample_index",
            "trace_index",
            "order_key",
            "input_state_strict_prefix",
            "observed_action_label",
        } or sample.get("sample_index") != index:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "training sample field set or order differs"
            )
        order = tuple(
            _integer(value, "training order component")
            for value in _array(sample.get("order_key"), "training order_key")
        )
        state = _mapping(
            sample.get("input_state_strict_prefix"), "training prefix state"
        )
        label = _mapping(sample.get("observed_action_label"), "training action label")
        if (
            (prior is not None and order <= prior)
            or state.get("cutoff_exclusive_order_key") != list(order)
            or label.get("learning_role") != "OBSERVED_ACTION_LABEL"
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "training sample is not a causal strict-prefix action example"
            )
        try:
            model_v2._assert_prefix_contract(state)
        except Exception as error:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(str(error)) from error
        prior = order
    environment = _mapping(raw.get("environment_projection"), "environment projection")
    if (
        environment.get("exact_guid_leave_one_out") is not True
        or environment.get("component_wide_random_draw_used") is not False
        or environment.get("future_schedule_exported_to_policy_context") is not False
        or environment.get("policy_runtime_receives_bridge_state_only") is not True
        or environment.get("focal_player_guid") != training.get("selected_guid")
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "dynamic environment projection is not causally isolated"
        )
    scenario = _mapping(raw.get("scenario"), "scenario")
    try:
        normalized = normalize_runner_scenarios([scenario])[0]
    except Exception as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"compiled scenario no longer satisfies runner-v4: {error}"
        ) from error
    if normalized != scenario:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled scenario is not stored in canonical runner form"
        )
    model = _mapping(scenario.get("scenario_model"), "scenario model")
    target_context = _mapping(
        scenario.get("target_context_bundle"), "target context bundle"
    )
    if (
        model.get("schema") != SCENARIO_MODEL_SCHEMA
        or model.get("status") != STATUS
        or model.get("bridge_execution_eligible") is not True
        or model.get("historical_truth") is not False
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "runner scenario model widened its execution or truth status"
        )
    if (
        target_context.get("schema") != compiler_v1.TARGET_CONTEXT_SCHEMA
        or target_context.get("status")
        != "OLD50_EXPLICIT_HYPOTHESES_CONTEXT_BOUND"
        or target_context.get("bridge_execution_eligible") is not True
        or target_context.get("limitation_codes") != list(compiler_v1.LIMITATION_CODES)
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "target context lost its explicit hypothesis boundary"
        )
    for bounded in (model, target_context):
        if (
            bounded.get("comparison_eligible") is not False
            or bounded.get("heldout_performance_evidence_eligible") is not False
            or bounded.get("historical_policy_voting_eligible") is not False
            or bounded.get("deployment_allowed") is not False
            or bounded.get("future_team_schedule_visible_to_policy") is not False
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "runner scenario widened a scientific or deployment gate"
            )
    model_source = _mapping(model.get("source"), "scenario model source")
    model_join = _validate_wave_identity_join(
        model_source.get("wave_identity_join")
    )
    if (
        model_source.get("overlay_manifest_content_sha256")
        != bindings.get("overlay_manifest_content_sha256")
        or model_source.get("overlay_wave_content_sha256")
        != bindings.get("overlay_wave_content_sha256")
        or model_source.get("capsule_bundle_content_sha256")
        != bindings.get("capsule_bundle_content_sha256")
        or model_source.get("capsule_scenario_sha256")
        != bindings.get("capsule_scenario_sha256")
        or model_join != binding_join
        or model_source.get("focal_player_guid") != training.get("selected_guid")
        or model_source.get("selection_lane") != training.get("selection_lane")
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "runner scenario lost the overlay/capsule source binding"
        )
    compilation_identity_sha = _sha(
        model_source.get("compilation_identity_sha256"),
        "compilation identity SHA-256",
    )
    hypotheses = _mapping(raw.get("hypothesis_selection"), "hypothesis selection")
    model_hypotheses = _mapping(
        model.get("hypothesis_selection"), "scenario model hypothesis selection"
    )
    if dict(hypotheses) != dict(model_hypotheses):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "top-level and runner hypothesis selections differ"
        )
    if (
        hypotheses.get("target_level_status")
        != "CALLER_SELECTED_SENSITIVITY_HYPOTHESIS"
        or hypotheses.get("initial_base_armor_status")
        != "CALLER_SELECTED_SENSITIVITY_HYPOTHESIS"
        or hypotheses.get("classification_is_historical_truth") is not False
        or hypotheses.get("position_is_historical_truth") is not False
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled hypotheses widened their historical-truth status"
        )
    health_policy = _mapping(
        hypotheses.get("health_branch_selection_policy"),
        "health branch selection policy",
    )
    if health_policy != HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "health branch selection policy differs from its frozen v1 contract"
        )
    model_environment = _mapping(
        model.get("causal_background_projection"),
        "scenario model causal background projection",
    )
    if dict(environment) != dict(model_environment):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "top-level and runner environment projections differ"
        )
    dynamic = _mapping(scenario.get("dynamic_load_config"), "dynamic load config")
    if dynamic.get("retarget_mode") != "NEXT_ALIVE_CYCLIC":
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled dynamic scenario changed the fixed retarget mode"
        )
    dynamic_health = [
        _mapping(row, "dynamic target health")
        for row in _array(dynamic.get("target_health"), "dynamic target health")
    ]
    health_rows = [
        _mapping(row, "hypothesis target health")
        for row in _array(hypotheses.get("health_by_target"), "health hypotheses")
    ]
    reason_by_branch = {
        "proxy_center": "PRIMARY_PROXY_CENTER_AVAILABLE",
        "repeat_center": "PRIMARY_MISSING_REPEAT_CENTER_FALLBACK",
        "legacy_threshold_25000": (
            "PRIMARY_AND_REPEAT_MISSING_LEGACY_THRESHOLD_25000_FALLBACK"
        ),
    }
    for health_row in health_rows:
        available = _array(
            health_row.get("available_branch_ids"), "available health branch IDs"
        )
        provenance = [
            _mapping(row, "available health branch provenance")
            for row in _array(
                health_row.get("available_branch_provenance"),
                "available health branch provenance",
            )
        ]
        selected_branch = health_row.get("selected_branch_id")
        if (
            available != sorted(set(available))
            or [row.get("branch_id") for row in provenance] != available
            or any(
                set(row)
                != {
                    "branch_id",
                    "declared_source",
                    "declared_status",
                    "availability_from_historical_capsule",
                    "historical_truth",
                }
                or row.get("availability_from_historical_capsule") is not True
                or row.get("historical_truth") is not False
                for row in provenance
            )
            or selected_branch not in available
            or selected_branch not in HEALTH_BRANCH_FALLBACK_ORDER_V1
            or health_row.get("selection_reason")
            != reason_by_branch[selected_branch]
            or selected_branch
            != next(
                branch
                for branch in HEALTH_BRANCH_FALLBACK_ORDER_V1
                if branch in available
            )
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "per-target health branch selection differs from the frozen policy"
            )
    contexts = [
        _mapping(row, "target context")
        for row in _array(target_context.get("contexts"), "target contexts")
    ]
    request = _mapping(scenario.get("request"), "scenario request")
    encounter = _mapping(request.get("encounter"), "scenario request encounter")
    request_targets = [
        _mapping(row, "request target")
        for row in _array(encounter.get("targets"), "request targets")
    ]
    target_count = len(dynamic_health)
    if not (
        target_count
        == len(health_rows)
        == len(contexts)
        == len(request_targets)
        == target_context.get("target_count")
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "hypothesis, dynamic, request, and context target counts differ"
        )
    level = _integer(
        hypotheses.get("target_level"), "hypothesis target level", positive=True
    )
    base_armor = _number(
        hypotheses.get("initial_base_armor"),
        "hypothesis initial base armor",
        positive=True,
    )
    classification = _text(
        hypotheses.get("target_classification"), "target classification"
    )
    equipped_names: list[str] | None = None
    for index, (dynamic_row, health_row, context, request_target) in enumerate(
        zip(dynamic_health, health_rows, contexts, request_targets, strict=True)
    ):
        stats = _array(request_target.get("stats"), "request target stats")
        names = [
            _text(name, "equipped item name")
            for name in _array(
                context.get("equipped_item_names"), "equipped item names"
            )
        ]
        if equipped_names is None:
            equipped_names = names
        if (
            dynamic_row.get("target_index") != index
            or health_row.get("target_index") != index
            or context.get("target_index") != index
            or dynamic_row.get("health") != health_row.get("max_health")
            or context.get("target_max_health") != health_row.get("max_health")
            or request_target.get("level") != level
            or len(stats) <= max(ARMOR_STAT_INDEX, HEALTH_STAT_INDEX)
            or stats[ARMOR_STAT_INDEX] != base_armor
            or stats[HEALTH_STAT_INDEX] != health_row.get("max_health")
            or context.get("target_classification") != classification
            or names != equipped_names
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compiled target hypothesis differs from request/dynamic/context"
            )
    attack = _mapping(hypotheses.get("attackability"), "attackability receipt")
    armor = _mapping(hypotheses.get("armor"), "armor receipt")
    if (
        attack.get("attackability_is_historical_truth") is not False
        or armor.get("effective_armor_is_historical_truth") is not False
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "dynamic target semantics were relabeled as historical truth"
        )
    magnitude_projection = _mapping(
        armor.get("magnitude_choice_projection"),
        "armor magnitude choice projection",
    )
    if set(magnitude_projection) != {
        "policy_id",
        "corpus_global_magnitude_choices",
        "wave_observed_debuff_ids",
        "projected_magnitude_choices",
    } or magnitude_projection.get("policy_id") != (
        ARMOR_MAGNITUDE_CHOICE_PROJECTION_POLICY_V1
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "armor magnitude choice projection contract differs"
        )
    global_magnitudes = _mapping(
        magnitude_projection.get("corpus_global_magnitude_choices"),
        "corpus-global armor magnitude choices",
    )
    observed_debuff_ids = [
        _text(value, "wave observed armor debuff ID")
        for value in _array(
            magnitude_projection.get("wave_observed_debuff_ids"),
            "wave observed armor debuff IDs",
        )
    ]
    projected_magnitudes = _mapping(
        magnitude_projection.get("projected_magnitude_choices"),
        "wave-projected armor magnitude choices",
    )
    selected_magnitudes = _mapping(
        armor.get("selected_magnitudes"), "selected armor magnitudes"
    )
    if (
        observed_debuff_ids != sorted(set(observed_debuff_ids))
        or set(selected_magnitudes) != set(observed_debuff_ids)
        or dict(projected_magnitudes)
        != {
            debuff_id: global_magnitudes[debuff_id]
            for debuff_id in observed_debuff_ids
            if debuff_id in global_magnitudes
        }
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "armor magnitude choices were not projected by observed wave debuffs"
        )
    background = [
        _mapping(row, "dynamic background event")
        for row in _array(
            dynamic.get("background_damage_events"), "dynamic background events"
        )
    ]
    if (
        len(background) != environment.get("kept_background_event_count")
        or sum(_number(row.get("damage"), "background damage") for row in background)
        != environment.get("kept_background_damage")
        or len(_array(dynamic.get("attackability_events"), "attackability events"))
        != attack.get("event_count")
        or len(_array(dynamic.get("effective_armor_events"), "armor events"))
        != armor.get("event_count")
        or armor.get("base_armor") != base_armor
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "dynamic environment differs from its hypothesis/environment receipts"
        )
    expected_compilation_identity_sha = sha256_json(
        {
            "overlay_wave_content_sha256": bindings["overlay_wave_content_sha256"],
            "capsule_scenario_sha256": bindings["capsule_scenario_sha256"],
            "wave_identity_join": binding_join,
            "selected_guid": training["selected_guid"],
            "selection_lane": training["selection_lane"],
            "request_sha256": scenario["request_sha256"],
            "dynamic_config_content_sha256": dynamic["content_sha256"],
            "target_context_bundle_sha256": scenario[
                "target_context_bundle_sha256"
            ],
            "hypothesis_selection": hypotheses,
        }
    )
    if (
        compilation_identity_sha != expected_compilation_identity_sha
        or scenario.get("scenario_id")
        != (
            f"old50-exact-fury-{bindings['overlay_wave_content_sha256'][:16]}"
            f"-compile-{compilation_identity_sha[:24]}"
        )
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "scenario identity does not cover the compiled request and hypotheses"
        )
    if source_overlay_wave is not None:
        source_row = _validate_wave_for_adapter(source_overlay_wave)
        source_identity = _mapping(source_row.get("identity"), "source identity")
        source_bindings = _mapping(
            source_row.get("source_bindings"), "source overlay bindings"
        )
        source_sha = _sha(
            _mapping(
                source_row.get("content_address"), "source overlay content address"
            ).get("sha256"),
            "source overlay row SHA-256",
        )
        expected_join = {
            "join_key": [
                source_identity["instance_id"],
                source_identity["encounter_id"],
                source_identity["wave_ordinal"],
            ],
            "overlay_external_wave_id": source_identity["external_wave_id"],
            "capsule_wave_id": (
                f"{source_identity['encounter_id']}:wave:"
                f"{source_identity['wave_ordinal']}"
            ),
            "namespace_translation": "DERIVED_ID_NAMESPACE_ONLY",
            "ordinal_only_join_allowed": False,
        }
        if (
            bindings.get("overlay_wave_content_sha256") != source_sha
            or binding_join != expected_join
            or bindings.get("stage5_wave_content_sha256")
            != source_bindings.get("stage5_wave_content_sha256")
            or bindings.get("stage6_block_content_sha256")
            != source_bindings.get("stage6_block_content_sha256")
            or training != _expert_training_projection(source_row)
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compiled artifact differs from its exact source overlay episode"
            )
        source_targets = _array(
            _mapping(source_row.get("target_contract"), "source target contract").get(
                "selected_target_guids"
            ),
            "source selected target GUIDs",
        )
        if [row.get("target_guid") for row in health_rows] != source_targets:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compiled target GUID order differs from the source target contract"
            )
        source_component = _mapping(
            _mapping(source_row.get("split_authority"), "source split authority").get(
                "overlap_component_join"
            ),
            "source overlap component join",
        )
        if (
            scenario.get("instance_id") != source_identity.get("instance_id")
            or scenario.get("component_id")
            != source_component.get("stage6_component_id")
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compiled runner grouping differs from the source wave/component"
            )
        expected_background, expected_environment = _compile_overlay_background(
            overlay_wave=source_row,
            target_guids=[row["target_guid"] for row in health_rows],
            horizon_ms=_integer(scenario.get("horizon_ms"), "scenario horizon"),
        )
        if (
            environment != expected_environment
            or background != [row.to_wire() for row in expected_background]
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "compiled dynamic background differs from the exact source schedule"
            )
    return raw


def _validate_formal_overlay_member_record_v1(
    value: Mapping[str, Any],
    *,
    formal_overlay_corpus: FormalExactFuryOverlayCorpusV1,
) -> JSONMap:
    """Check overlay membership only; this is not the simulator promotion gate."""

    if not isinstance(formal_overlay_corpus, FormalExactFuryOverlayCorpusV1):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "formal validation requires a verified 20/470 overlay corpus"
        )
    raw = _mapping(value, "compiled exact-Fury dynamic-v3 artifact")
    bindings = _mapping(raw.get("source_bindings"), "source bindings")
    manifest_sha = formal_overlay_corpus.manifest["content_address"]["sha256"]
    if bindings.get("overlay_manifest_content_sha256") != manifest_sha:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled artifact is not bound to the verified formal manifest"
        )
    row_sha = bindings.get("overlay_wave_content_sha256")
    matches = [
        row
        for row in formal_overlay_corpus.rows
        if row["content_address"]["sha256"] == row_sha
    ]
    if len(matches) != 1:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "compiled artifact is not a unique member of the verified 20/470 corpus"
        )
    return validate_exact_fury_overlay_dynamic_v3_structure_v1(
        raw, source_overlay_wave=matches[0]
    )


MATERIALIZED_EXECUTION_BOUNDARY = {
    "network_requests_made": 0,
    "policy_training_started": False,
    "simulator_runs_started": False,
    "comparison_started": False,
    "deployment_started": False,
    "source_overlay_modified": False,
    "source_capsule_modified": False,
    "frozen_historical_policy_v2_v5_modified": False,
}


def _equipped_names_input(value: Mapping[str, Any]) -> tuple[JSONMap, tuple[str, ...]]:
    receipt = _strict_json(value, "equipped names receipt")
    names = tuple(
        _text(name, f"equipped_item_names[{index}]")
        for index, name in enumerate(
            _array(receipt.get("equipped_item_names"), "equipped_item_names")
        )
    )
    if not names:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "equipped names receipt must retain the pinned loadout"
        )
    return receipt, names


def _materialization_parameters(
    *,
    target_level: int,
    initial_base_armor: int | float,
    health_branch_selection_policy: str,
    attackability_branch_id: str,
    target_classification: str,
    armor_magnitude_by_debuff: Mapping[str, int | float] | None,
) -> JSONMap:
    return {
        "target_level": _integer(target_level, "target_level", positive=True),
        "initial_base_armor": _number(
            initial_base_armor, "initial_base_armor", positive=True
        ),
        "health_branch_selection_policy": _text(
            health_branch_selection_policy, "health_branch_selection_policy"
        ),
        "attackability_branch_id": _text(
            attackability_branch_id, "attackability_branch_id"
        ),
        "target_classification": _text(
            target_classification, "target_classification"
        ),
        "armor_magnitude_by_debuff": _strict_json(
            dict(armor_magnitude_by_debuff or {}), "armor magnitude choices"
        ),
        "loadout_contract": "CALLER_PINNED_STATIC_REQUEST_AND_EQUIPPED_NAMES",
    }


def _materialized_summary(entries: Sequence[Mapping[str, Any]]) -> JSONMap:
    summaries = [
        _mapping(entry.get("summary"), "materialized instance summary")
        for entry in entries
    ]
    lane_counts = {
        selector_v2.PDF_LANE: sum(
            int(summary.get("pdf_outcome_conditioned_wave_count", 0))
            for summary in summaries
        ),
        selector_v2.GENERIC_LANE: sum(
            int(summary.get("generic_outcome_free_wave_count", 0))
            for summary in summaries
        ),
    }
    health_branch_counts: Counter[str] = Counter()
    for summary in summaries:
        for branch_id, count in _mapping(
            summary.get("health_branch_selection_counts"),
            "health branch selection counts",
        ).items():
            health_branch_counts[_text(branch_id, "selected health branch ID")] += (
                _integer(count, "selected health branch count")
            )
    return {
        "instance_count": len(entries),
        "compiled_wave_count": sum(
            int(summary.get("compiled_wave_count", 0)) for summary in summaries
        ),
        "pdf_outcome_conditioned_wave_count": lane_counts[selector_v2.PDF_LANE],
        "generic_outcome_free_wave_count": lane_counts[selector_v2.GENERIC_LANE],
        "expert_training_sample_count": sum(
            int(summary.get("expert_training_sample_count", 0))
            for summary in summaries
        ),
        "background_event_count": sum(
            int(summary.get("background_event_count", 0)) for summary in summaries
        ),
        "background_damage": sum(
            float(summary.get("background_damage", 0)) for summary in summaries
        ),
        "target_count": sum(
            int(summary.get("target_count", 0)) for summary in summaries
        ),
        "health_branch_selection_counts": dict(sorted(health_branch_counts.items())),
    }


def _validate_materialized_manifest_document(value: Mapping[str, Any]) -> JSONMap:
    raw = _strict_json(value, "materialized adapter manifest")
    if set(raw) != {
        "schema",
        "revision",
        "status",
        "source_bindings",
        "materialization_parameters",
        "instance_order",
        "instances",
        "summary",
        "scientific_boundary",
        "execution_boundary",
        "content_address",
    } or (
        raw.get("schema") != MATERIALIZED_MANIFEST_SCHEMA
        or raw.get("revision") != REVISION
        or raw.get("status") != MATERIALIZED_MANIFEST_STATUS
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized adapter manifest schema or field set differs"
        )
    address = _mapping(raw.get("content_address"), "manifest content address")
    core = {key: item for key, item in raw.items() if key != "content_address"}
    if address != {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized adapter manifest content address differs"
        )
    if raw.get("scientific_boundary") != SCIENTIFIC_BOUNDARY or raw.get(
        "execution_boundary"
    ) != MATERIALIZED_EXECUTION_BOUNDARY:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized adapter manifest widened its execution or claim boundary"
        )
    sources = _mapping(raw.get("source_bindings"), "manifest source bindings")
    for field in (
        "overlay_manifest_content_sha256",
        "capsule_bundle_content_sha256",
        "base_request_sha256",
        "equipped_names_receipt_sha256",
    ):
        _sha(sources.get(field), field)
    parameters = _mapping(
        raw.get("materialization_parameters"), "materialization parameters"
    )
    if set(parameters) != {
        "target_level",
        "initial_base_armor",
        "health_branch_selection_policy",
        "attackability_branch_id",
        "target_classification",
        "armor_magnitude_by_debuff",
        "loadout_contract",
    } or parameters.get("loadout_contract") != (
        "CALLER_PINNED_STATIC_REQUEST_AND_EQUIPPED_NAMES"
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialization parameter contract differs"
        )
    _integer(parameters.get("target_level"), "target_level", positive=True)
    _number(parameters.get("initial_base_armor"), "base armor", positive=True)
    if (
        _text(
            parameters.get("health_branch_selection_policy"),
            "health branch selection policy",
        )
        != HEALTH_BRANCH_FALLBACK_POLICY_V1
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialization used an unsupported health branch selection policy"
        )
    _text(parameters.get("attackability_branch_id"), "attackability branch")
    _text(parameters.get("target_classification"), "target classification")
    if parameters.get("armor_magnitude_by_debuff") != (
        FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "formal armor magnitude choices differ from expose_armor=1700"
        )
    order = [
        _text(item, "materialized instance order")
        for item in _array(raw.get("instance_order"), "instance order")
    ]
    instances = [
        _mapping(item, "materialized instance entry")
        for item in _array(raw.get("instances"), "instances")
    ]
    if (
        len(order) != FORMAL_INSTANCE_COUNT
        or len(instances) != FORMAL_INSTANCE_COUNT
        or len(set(order)) != FORMAL_INSTANCE_COUNT
        or [entry.get("instance_id") for entry in instances] != order
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized instance order does not close to 20 unique instances"
        )
    for entry in instances:
        entry_address = _mapping(entry.get("content_address"), "entry content address")
        entry_core = {
            key: item for key, item in entry.items() if key != "content_address"
        }
        if entry_address != {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(entry_core),
        }:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "materialized instance entry content address differs"
            )
        if set(entry_core) != {
            "instance_id",
            "selected_guid",
            "selection_lane",
            "partition",
            "summary",
        }:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "materialized instance entry field set differs"
            )
        if entry.get("selection_lane") not in selector_v2.SELECTION_LANES:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "materialized instance lane is unsupported"
            )
        _text(entry.get("selected_guid"), "materialized selected GUID")
        partition = _mapping(entry.get("partition"), "partition descriptor")
        if set(partition) != {
            "path",
            "record_schema",
            "record_count",
            "logical_content_sha256",
            "logical_size_bytes",
            "compressed_file_sha256",
            "compressed_size_bytes",
            "gzip_mtime",
        } or partition.get("record_schema") != SCHEMA:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "materialized partition descriptor differs"
            )
        relative = _text(partition.get("path"), "partition path")
        if Path(relative).name != relative or partition.get("gzip_mtime") != 0:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "materialized partition must be a deterministic colocated gzip"
            )
        for field in ("logical_content_sha256", "compressed_file_sha256"):
            _sha(partition.get(field), field)
        for field in ("record_count", "logical_size_bytes", "compressed_size_bytes"):
            _integer(partition.get(field), field, positive=True)
        summary = _mapping(entry.get("summary"), "instance summary")
        if partition.get("record_count") != summary.get("compiled_wave_count"):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "partition count differs from its instance summary"
            )
    summary = _mapping(raw.get("summary"), "materialized summary")
    if dict(summary) != _materialized_summary(instances) or (
        summary.get("instance_count") != FORMAL_INSTANCE_COUNT
        or summary.get("compiled_wave_count") != FORMAL_WAVE_COUNT
        or summary.get("pdf_outcome_conditioned_wave_count")
        + summary.get("generic_outcome_free_wave_count")
        != FORMAL_WAVE_COUNT
        or summary.get("health_branch_selection_counts")
        != FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized summary does not close to the formal 20/470 corpus"
        )
    return raw


def materialize_exact_fury_overlay_dynamic_v3_v1(
    *,
    overlay_manifest_path: str | Path,
    capsule_bundle: Mapping[str, Any],
    base_request: Mapping[str, Any],
    equipped_names_receipt: Mapping[str, Any],
    output_directory: str | Path,
    target_level: int,
    initial_base_armor: int | float,
    health_branch_selection_policy: str,
    attackability_branch_id: str,
    target_classification: str,
    armor_magnitude_by_debuff: Mapping[str, int | float] | None = None,
) -> MaterializedExactFuryDynamicV3CorpusV1:
    """Compile all formal waves into deterministic per-instance gzip partitions."""

    corpus = load_formal_exact_fury_overlay_corpus_v1(overlay_manifest_path)
    capsule = _strict_json(capsule_bundle, "old-50 capsule bundle")
    capsule_index = build_validated_old50_capsule_index_v1(capsule)
    request = _strict_json(base_request, "base simulator request")
    equipped_receipt, equipped_names = _equipped_names_input(
        equipped_names_receipt
    )
    parameters = _materialization_parameters(
        target_level=target_level,
        initial_base_armor=initial_base_armor,
        health_branch_selection_policy=health_branch_selection_policy,
        attackability_branch_id=attackability_branch_id,
        target_classification=target_classification,
        armor_magnitude_by_debuff=armor_magnitude_by_debuff,
    )
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    stable_path = output / "manifest.json"
    if stable_path.exists():
        validated = validate_materialized_exact_fury_dynamic_v3_corpus_v1(
            manifest_path=stable_path,
            overlay_manifest_path=corpus.manifest_path,
            capsule_bundle=capsule,
            base_request=request,
            equipped_names_receipt=equipped_receipt,
        )
        if validated["materialization_parameters"] != parameters:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "existing complete materialization used different parameters"
            )
        addressed_path = output / (
            f"{MATERIALIZED_MANIFEST_PREFIX}."
            f"{validated['content_address']['sha256']}.manifest.json"
        )
        return MaterializedExactFuryDynamicV3CorpusV1(
            manifest_path=stable_path,
            addressed_manifest_path=addressed_path,
            manifest=validated,
        )
    for temporary in output.glob(".*.tmp"):
        if temporary.is_file():
            temporary.unlink()
    rows_by_instance: dict[str, list[JSONMap]] = {
        instance_id: [] for instance_id in corpus.manifest["instance_order"]
    }
    for row in corpus.rows:
        rows_by_instance[row["identity"]["instance_id"]].append(row)
    overlay_manifest_sha = corpus.manifest["content_address"]["sha256"]
    manifest_entries_by_instance = {
        entry["instance_id"]: entry for entry in corpus.manifest["instances"]
    }
    entries: list[JSONMap] = []
    for instance_id in corpus.manifest["instance_order"]:
        source_entry = manifest_entries_by_instance[instance_id]
        selector = _mapping(source_entry.get("selector"), "source selector")
        temporary = output / f".{instance_id}.compiled.jsonl.gz.tmp"
        if temporary.exists():
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"materialization temporary already exists: {temporary}"
            )
        logical = hashlib.sha256()
        logical_size = 0
        summary = {
            "compiled_wave_count": 0,
            "pdf_outcome_conditioned_wave_count": 0,
            "generic_outcome_free_wave_count": 0,
            "expert_training_sample_count": 0,
            "background_event_count": 0,
            "background_damage": 0.0,
            "target_count": 0,
            "health_branch_selection_counts": {},
        }
        try:
            with temporary.open("xb") as raw_handle:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw_handle, mtime=0
                ) as gzip_handle:
                    for source_row in rows_by_instance[instance_id]:
                        compiled = compile_exact_fury_overlay_dynamic_v3_v1(
                            overlay_wave=source_row,
                            overlay_manifest_content_sha256=overlay_manifest_sha,
                            capsule_bundle=capsule,
                            capsule_index=capsule_index,
                            base_request=request,
                            equipped_item_names=equipped_names,
                            target_level=parameters["target_level"],
                            initial_base_armor=parameters["initial_base_armor"],
                            health_branch_selection_policy=parameters[
                                "health_branch_selection_policy"
                            ],
                            attackability_branch_id=parameters[
                                "attackability_branch_id"
                            ],
                            target_classification=parameters[
                                "target_classification"
                            ],
                            armor_magnitude_by_debuff=parameters[
                                "armor_magnitude_by_debuff"
                            ],
                        )
                        artifact = _validate_formal_overlay_member_record_v1(
                            compiled.artifact,
                            formal_overlay_corpus=corpus,
                        )
                        line = _canonical(artifact) + b"\n"
                        gzip_handle.write(line)
                        logical.update(line)
                        logical_size += len(line)
                        training = artifact["expert_training_projection"]
                        environment = artifact["environment_projection"]
                        lane = training["selection_lane"]
                        summary["compiled_wave_count"] += 1
                        summary[
                            "pdf_outcome_conditioned_wave_count"
                            if lane == selector_v2.PDF_LANE
                            else "generic_outcome_free_wave_count"
                        ] += 1
                        summary["expert_training_sample_count"] += training[
                            "sample_count"
                        ]
                        summary["background_event_count"] += environment[
                            "kept_background_event_count"
                        ]
                        summary["background_damage"] += environment[
                            "kept_background_damage"
                        ]
                        summary["target_count"] += artifact["scenario"][
                            "target_context_bundle"
                        ]["target_count"]
                        for health_row in artifact["hypothesis_selection"][
                            "health_by_target"
                        ]:
                            branch_id = health_row["selected_branch_id"]
                            summary["health_branch_selection_counts"][branch_id] = (
                                summary["health_branch_selection_counts"].get(
                                    branch_id, 0
                                )
                                + 1
                            )
                raw_handle.flush()
                os.fsync(raw_handle.fileno())
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise
        compressed = hashlib.sha256()
        compressed_size = 0
        try:
            with temporary.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    compressed.update(chunk)
                    compressed_size += len(chunk)
        except OSError as error:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"could not bind materialized partition {temporary}: {error}"
            ) from error
        logical_sha = logical.hexdigest()
        partition_name = f"{instance_id}.{logical_sha}.jsonl.gz"
        partition_path = output / partition_name
        _publish_partition_or_reuse(temporary, partition_path)
        entry_core = {
            "instance_id": instance_id,
            "selected_guid": selector["selected_guid"],
            "selection_lane": selector["selection_lane"],
            "partition": {
                "path": partition_name,
                "record_schema": SCHEMA,
                "record_count": summary["compiled_wave_count"],
                "logical_content_sha256": logical_sha,
                "logical_size_bytes": logical_size,
                "compressed_file_sha256": compressed.hexdigest(),
                "compressed_size_bytes": compressed_size,
                "gzip_mtime": 0,
            },
            "summary": summary,
        }
        entries.append(_content_addressed(entry_core))
    manifest_core = {
        "schema": MATERIALIZED_MANIFEST_SCHEMA,
        "revision": REVISION,
        "status": MATERIALIZED_MANIFEST_STATUS,
        "source_bindings": {
            "overlay_manifest_content_sha256": overlay_manifest_sha,
            "capsule_bundle_content_sha256": capsule_index.bundle_content_sha256,
            "base_request_sha256": sha256_json(request),
            "equipped_names_receipt_sha256": sha256_json(equipped_receipt),
        },
        "materialization_parameters": parameters,
        "instance_order": list(corpus.manifest["instance_order"]),
        "instances": entries,
        "summary": _materialized_summary(entries),
        "scientific_boundary": deepcopy(SCIENTIFIC_BOUNDARY),
        "execution_boundary": deepcopy(MATERIALIZED_EXECUTION_BOUNDARY),
    }
    manifest = _content_addressed(manifest_core)
    _validate_materialized_manifest_document(manifest)
    payload = _canonical(manifest) + b"\n"
    addressed_path = output / (
        f"{MATERIALIZED_MANIFEST_PREFIX}."
        f"{manifest['content_address']['sha256']}.manifest.json"
    )
    expected_files = {
        entry["partition"]["path"] for entry in entries
    } | {addressed_path.name}
    extras = sorted(
        path.name
        for path in output.iterdir()
        if path.is_file() and path.name not in expected_files
    )
    if extras:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"materialization directory contains unrelated or stale files: {extras}"
        )
    _atomic_publish_bytes(addressed_path, payload)
    _atomic_publish_bytes(stable_path, payload)
    validated = validate_materialized_exact_fury_dynamic_v3_corpus_v1(
        manifest_path=stable_path,
        overlay_manifest_path=corpus.manifest_path,
        capsule_bundle=capsule,
        base_request=request,
        equipped_names_receipt=equipped_receipt,
    )
    return MaterializedExactFuryDynamicV3CorpusV1(
        manifest_path=stable_path,
        addressed_manifest_path=addressed_path,
        manifest=validated,
    )


def validate_materialized_exact_fury_dynamic_v3_corpus_v1(
    *,
    manifest_path: str | Path,
    overlay_manifest_path: str | Path,
    capsule_bundle: Mapping[str, Any],
    base_request: Mapping[str, Any],
    equipped_names_receipt: Mapping[str, Any],
) -> JSONMap:
    """The complete gate: recompile all 470 rows against every immutable input."""

    resolved = Path(manifest_path).expanduser().resolve()
    if resolved.name != "manifest.json":
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized validation starts from the stable manifest.json"
        )
    try:
        manifest_bytes = resolved.read_bytes()
        manifest_value = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"could not load materialized manifest {resolved}: {error}"
        ) from error
    manifest = _validate_materialized_manifest_document(
        _mapping(manifest_value, "materialized manifest")
    )
    if manifest_bytes != _canonical(manifest) + b"\n":
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized manifest is not canonical JSON with one LF"
        )
    addressed = resolved.parent / (
        f"{MATERIALIZED_MANIFEST_PREFIX}."
        f"{manifest['content_address']['sha256']}.manifest.json"
    )
    try:
        addressed_bytes = addressed.read_bytes()
    except OSError as error:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            f"addressed materialized manifest is missing: {error}"
        ) from error
    if addressed_bytes != manifest_bytes:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "stable and content-addressed materialized manifests differ"
        )
    corpus = load_formal_exact_fury_overlay_corpus_v1(overlay_manifest_path)
    capsule = _strict_json(capsule_bundle, "old-50 capsule bundle")
    capsule_index = build_validated_old50_capsule_index_v1(capsule)
    request = _strict_json(base_request, "base simulator request")
    equipped_receipt, equipped_names = _equipped_names_input(
        equipped_names_receipt
    )
    sources = manifest["source_bindings"]
    if sources != {
        "overlay_manifest_content_sha256": corpus.manifest["content_address"][
            "sha256"
        ],
        "capsule_bundle_content_sha256": capsule_index.bundle_content_sha256,
        "base_request_sha256": sha256_json(request),
        "equipped_names_receipt_sha256": sha256_json(equipped_receipt),
    }:
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized manifest differs from the supplied immutable sources"
        )
    source_by_sha = {
        row["content_address"]["sha256"]: row for row in corpus.rows
    }
    seen_source_shas: set[str] = set()
    entries = manifest["instances"]
    observed_entries: list[JSONMap] = []
    parameters = manifest["materialization_parameters"]
    for entry in entries:
        descriptor = entry["partition"]
        partition_path = resolved.parent / descriptor["path"]
        logical = hashlib.sha256()
        logical_size = 0
        record_count = 0
        observed_summary = {
            "compiled_wave_count": 0,
            "pdf_outcome_conditioned_wave_count": 0,
            "generic_outcome_free_wave_count": 0,
            "expert_training_sample_count": 0,
            "background_event_count": 0,
            "background_damage": 0.0,
            "target_count": 0,
            "health_branch_selection_counts": {},
        }
        try:
            with partition_path.open("rb") as source_handle:
                hashing_raw = overlap_v1._HashingRaw(source_handle)
                with io.BufferedReader(hashing_raw) as buffered:
                    with gzip.GzipFile(fileobj=buffered, mode="rb") as gzip_handle:
                        for raw_line in gzip_handle:
                            logical.update(raw_line)
                            logical_size += len(raw_line)
                            record_count += 1
                            try:
                                artifact_value = json.loads(raw_line.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    f"invalid materialized JSONL row: {error}"
                                ) from error
                            artifact_map = _mapping(
                                artifact_value, "materialized adapter row"
                            )
                            if raw_line != _canonical(artifact_map) + b"\n":
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "materialized partition contains noncanonical JSONL"
                                )
                            artifact_sources = _mapping(
                                artifact_map.get("source_bindings"),
                                "artifact source bindings",
                            )
                            source_sha = _sha(
                                artifact_sources.get(
                                    "overlay_wave_content_sha256"
                                ),
                                "artifact overlay wave SHA-256",
                            )
                            source_row = source_by_sha.get(source_sha)
                            if source_row is None or source_sha in seen_source_shas:
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "materialized row source is absent or duplicated"
                                )
                            artifact = _validate_formal_overlay_member_record_v1(
                                artifact_map, formal_overlay_corpus=corpus
                            )
                            expected = compile_exact_fury_overlay_dynamic_v3_v1(
                                overlay_wave=source_row,
                                overlay_manifest_content_sha256=sources[
                                    "overlay_manifest_content_sha256"
                                ],
                                capsule_bundle=capsule,
                                capsule_index=capsule_index,
                                base_request=request,
                                equipped_item_names=equipped_names,
                                target_level=parameters["target_level"],
                                initial_base_armor=parameters[
                                    "initial_base_armor"
                                ],
                                health_branch_selection_policy=parameters[
                                    "health_branch_selection_policy"
                                ],
                                attackability_branch_id=parameters[
                                    "attackability_branch_id"
                                ],
                                target_classification=parameters[
                                    "target_classification"
                                ],
                                armor_magnitude_by_debuff=parameters[
                                    "armor_magnitude_by_debuff"
                                ],
                            ).artifact
                            if artifact != expected:
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "materialized row differs from deterministic source recompilation"
                                )
                            seen_source_shas.add(source_sha)
                            training = artifact["expert_training_projection"]
                            environment = artifact["environment_projection"]
                            hypotheses = artifact["hypothesis_selection"]
                            contexts = artifact["scenario"][
                                "target_context_bundle"
                            ]["contexts"]
                            if (
                                artifact_sources[
                                    "overlay_manifest_content_sha256"
                                ]
                                != sources["overlay_manifest_content_sha256"]
                                or artifact_sources[
                                    "capsule_bundle_content_sha256"
                                ]
                                != sources["capsule_bundle_content_sha256"]
                                or training["selected_guid"]
                                != entry["selected_guid"]
                                or training["selection_lane"]
                                != entry["selection_lane"]
                                or artifact_sources["wave_identity_join"][
                                    "join_key"
                                ][0]
                                != entry["instance_id"]
                                or hypotheses["target_level"]
                                != parameters["target_level"]
                                or hypotheses["initial_base_armor"]
                                != parameters["initial_base_armor"]
                                or hypotheses["health_branch_selection_policy"][
                                    "policy_id"
                                ]
                                != parameters["health_branch_selection_policy"]
                                or hypotheses["attackability"]["branch_id"]
                                != parameters["attackability_branch_id"]
                                or hypotheses["target_classification"]
                                != parameters["target_classification"]
                                or any(
                                    context["equipped_item_names"]
                                    != list(equipped_names)
                                    for context in contexts
                                )
                            ):
                                raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                                    "materialized row differs from manifest parameters"
                                )
                            lane = training["selection_lane"]
                            observed_summary["compiled_wave_count"] += 1
                            observed_summary[
                                "pdf_outcome_conditioned_wave_count"
                                if lane == selector_v2.PDF_LANE
                                else "generic_outcome_free_wave_count"
                            ] += 1
                            observed_summary[
                                "expert_training_sample_count"
                            ] += training["sample_count"]
                            observed_summary["background_event_count"] += environment[
                                "kept_background_event_count"
                            ]
                            observed_summary["background_damage"] += environment[
                                "kept_background_damage"
                            ]
                            observed_summary["target_count"] += len(contexts)
                            for health_row in hypotheses["health_by_target"]:
                                branch_id = health_row["selected_branch_id"]
                                observed_summary[
                                    "health_branch_selection_counts"
                                ][branch_id] = (
                                    observed_summary[
                                        "health_branch_selection_counts"
                                    ].get(branch_id, 0)
                                    + 1
                                )
        except OSError as error:
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                f"could not read materialized partition {partition_path}: {error}"
            ) from error
        if (
            hashing_raw.bytes_read != descriptor["compressed_size_bytes"]
            or hashing_raw.digest.hexdigest()
            != descriptor["compressed_file_sha256"]
            or record_count != descriptor["record_count"]
            or logical_size != descriptor["logical_size_bytes"]
            or logical.hexdigest() != descriptor["logical_content_sha256"]
            or observed_summary != entry["summary"]
        ):
            raise ChronicleOld50ExactFuryDynamicV3AdapterError(
                "materialized partition count, size, hash, or summary differs"
            )
        observed_entries.append(entry)
    if (
        len(seen_source_shas) != FORMAL_WAVE_COUNT
        or seen_source_shas != set(source_by_sha)
        or manifest["summary"] != _materialized_summary(observed_entries)
    ):
        raise ChronicleOld50ExactFuryDynamicV3AdapterError(
            "materialized corpus does not cover each of the 470 source waves once"
        )
    return manifest


def _cli_inputs(arguments: argparse.Namespace) -> tuple[JSONMap, JSONMap, JSONMap]:
    return (
        _load_json_path(arguments.capsule, "old-50 capsule bundle"),
        _load_json_path(arguments.base_request, "base simulator request"),
        _load_json_path(arguments.equipped_names, "equipped names receipt"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize or validate the exact-Fury overlay dynamic-v3 corpus"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("materialize", "validate"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--overlay-manifest", required=True)
        sub.add_argument("--capsule", required=True)
        sub.add_argument("--base-request", required=True)
        sub.add_argument("--equipped-names", required=True)
        if command == "validate":
            sub.add_argument("--manifest", required=True)
        else:
            sub.add_argument("--output-directory", required=True)
            sub.add_argument("--target-level", required=True, type=int)
            sub.add_argument("--initial-base-armor", required=True, type=float)
            sub.add_argument("--health-branch-selection-policy", required=True)
            sub.add_argument("--attackability-branch-id", required=True)
            sub.add_argument("--target-classification", required=True)
            sub.add_argument("--armor-magnitudes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        capsule, request, equipped = _cli_inputs(arguments)
        if arguments.command == "validate":
            manifest = validate_materialized_exact_fury_dynamic_v3_corpus_v1(
                manifest_path=arguments.manifest,
                overlay_manifest_path=arguments.overlay_manifest,
                capsule_bundle=capsule,
                base_request=request,
                equipped_names_receipt=equipped,
            )
        else:
            armor = (
                _load_json_path(arguments.armor_magnitudes, "armor magnitudes")
                if arguments.armor_magnitudes
                else {}
            )
            result = materialize_exact_fury_overlay_dynamic_v3_v1(
                overlay_manifest_path=arguments.overlay_manifest,
                capsule_bundle=capsule,
                base_request=request,
                equipped_names_receipt=equipped,
                output_directory=arguments.output_directory,
                target_level=arguments.target_level,
                initial_base_armor=arguments.initial_base_armor,
                health_branch_selection_policy=(
                    arguments.health_branch_selection_policy
                ),
                attackability_branch_id=arguments.attackability_branch_id,
                target_classification=arguments.target_classification,
                armor_magnitude_by_debuff=armor,
            )
            manifest = result.manifest
    except (ChronicleOld50ExactFuryDynamicV3AdapterError, OSError, ValueError) as error:
        print(
            json.dumps(
                {"status": "BLOCKED", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "content_sha256": manifest["content_address"]["sha256"],
                "summary": manifest["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


__all__ = (
    "CompiledExactFuryDynamicV3ScenarioV1",
    "ChronicleOld50ExactFuryDynamicV3AdapterError",
    "ARMOR_MAGNITUDE_CHOICE_PROJECTION_POLICY_V1",
    "FORMAL_ARMOR_MAGNITUDE_BY_DEBUFF_V1",
    "FORMAL_GENERIC_INSTANCE_COUNT",
    "FORMAL_INSTANCE_COUNT",
    "FORMAL_PDF_INSTANCE_COUNT",
    "FORMAL_WAVE_COUNT",
    "FORMAL_OVERLAY_MANIFEST_CONTENT_SHA256",
    "FORMAL_OVERLAY_MANIFEST_FILE_SHA256",
    "FORMAL_HEALTH_BRANCH_SELECTION_COUNTS_V1",
    "HEALTH_BRANCH_FALLBACK_ORDER_V1",
    "HEALTH_BRANCH_FALLBACK_POLICY_V1",
    "HEALTH_BRANCH_SELECTION_POLICY_RECEIPT_V1",
    "FormalExactFuryOverlayCorpusV1",
    "LIMITATION_CODES",
    "MATERIALIZED_MANIFEST_SCHEMA",
    "MATERIALIZED_MANIFEST_STATUS",
    "MaterializedExactFuryDynamicV3CorpusV1",
    "REVISION",
    "SCHEMA",
    "SCENARIO_MODEL_SCHEMA",
    "SCIENTIFIC_BOUNDARY",
    "STATUS",
    "TRAINING_SCHEMA",
    "ValidatedOld50CapsuleIndexV1",
    "build_validated_old50_capsule_index_v1",
    "compile_exact_fury_overlay_dynamic_v3_v1",
    "load_formal_exact_fury_overlay_corpus_v1",
    "main",
    "materialize_exact_fury_overlay_dynamic_v3_v1",
    "validate_exact_fury_overlay_dynamic_v3_structure_v1",
    "validate_materialized_exact_fury_dynamic_v3_corpus_v1",
)


if __name__ == "__main__":
    raise SystemExit(main())
