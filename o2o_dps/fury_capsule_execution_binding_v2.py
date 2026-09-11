"""Bind a compact Fury scenario capsule to static runner-v2 requests.

The offline capsule deliberately does not contain the 30 GB raw/normalized
Chronicle corpus.  This module is the narrow transport boundary: it validates
the content-addressed capsule by itself, reconstructs duration-mode simulator
requests from a small caller-pinned base request, and passes the result through
the paired runner's canonical scenario validator.

Only the static control model is implemented here.  It keeps every target
present for the complete reconstructed hostile-activity horizon, applies one
static base-armor sensitivity hypothesis, and disables target-health
termination.  Observed armor transitions and non-full-wave attackability
branches remain provenance only.  Asking this version to execute either
dynamic schedule, or endogenous health/TTK, fails closed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .fury_offline_scenario_capsule_v2 import (
    FuryOfflineScenarioCapsuleError,
    validate_scenario_capsule_bundle_v2,
)
from .fury_paired_multiseed_runner_v2 import (
    FuryPairedRunnerError,
    SCENARIO_MODEL_KIND,
    TARGET_CONTEXT_BUNDLE_KIND,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
    runner_scenario_model_bundle_sha256,
    runner_target_context_bundle_set_sha256,
    sha256_json,
)


JSONMap = dict[str, Any]

SCHEMA_VERSION = 2
SCHEMA = "fury_capsule_static_execution_binding/v2"
KIND = "fury_capsule_static_execution_binding_v2"
IMPLEMENTATION_REVISION = "v2.1_content_addressed_nonvoting_dynamic_receipts"

STATIC_BASE_ARMOR_MODE = "STATIC_BASE_ARMOR_HYPOTHESIS"
FULL_WAVE_ATTACKABILITY_MODE = "FULL_WAVE_STATIC_HYPOTHESIS"
DURATION_ONLY_HEALTH_MODE = "DURATION_ONLY_NO_TARGET_DEATH"

_CAPSULE_SCHEMA = "fury_offline_scenario_capsules/v2"
_CAPSULE_KIND = "fury_offline_scenario_capsule_bundle_v2"
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SCENARIO_LEVEL = re.compile(r"__level-([1-9][0-9]*)\Z")
_MIN_TARGET_STATS_LENGTH = HEALTH_STAT_INDEX + 1


class FuryCapsuleExecutionBindingV2Error(RuntimeError):
    """The compact capsule cannot be bound without widening its claims."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be nonempty text")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be an integer")
    return value


def _positive_integer(value: Any, label: str) -> int:
    parsed = _integer(value, label)
    if parsed <= 0:
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be positive")
    return parsed


def _nonnegative_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label} must be finite and nonnegative"
        )
    return int(parsed) if parsed.is_integer() else parsed


def _positive_number(value: Any, label: str) -> float:
    parsed = _nonnegative_number(value, label)
    if float(parsed) <= 0:
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be positive")
    return float(parsed)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _strict_json_copy(value: Mapping[str, Any], label: str) -> JSONMap:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        parsed = json.loads(rendered)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label} is not strict JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise FuryCapsuleExecutionBindingV2Error(f"{label} must be a JSON object")
    return parsed


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def load_capsule_bundle_v2(path: Path) -> JSONMap:
    """Load and independently validate a JSON or deterministic JSON.GZ capsule.

    Source paths named by the capsule are intentionally not opened.  That is
    what makes this loader usable on an HPC worker carrying only the capsule.
    """

    resolved = path.expanduser().resolve()
    try:
        if resolved.suffix.casefold() == ".gz":
            with gzip.open(resolved, mode="rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            with resolved.open(mode="r", encoding="utf-8") as handle:
                value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FuryCapsuleExecutionBindingV2Error(
            f"could not load compact capsule {resolved}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise FuryCapsuleExecutionBindingV2Error(
            "compact capsule must contain one JSON object"
        )
    enumerate_capsule_scenarios_v2(value)
    return value


def enumerate_capsule_scenarios_v2(
    bundle: Mapping[str, Any],
) -> tuple[JSONMap, ...]:
    """Validate and deterministically enumerate all portable capsule scenarios."""

    try:
        validate_scenario_capsule_bundle_v2(bundle)
    except FuryOfflineScenarioCapsuleError as exc:
        raise FuryCapsuleExecutionBindingV2Error(str(exc)) from exc
    if bundle.get("schema") != _CAPSULE_SCHEMA or bundle.get("kind") != _CAPSULE_KIND:
        raise FuryCapsuleExecutionBindingV2Error("unsupported compact capsule schema")

    claim = _mapping(bundle.get("claim_boundary"), "capsule.claim_boundary")
    if claim.get("portable_simulator_scenario_model") is not True:
        raise FuryCapsuleExecutionBindingV2Error(
            "capsule does not declare a portable simulator scenario model"
        )
    for field in (
        "exact_historical_replay",
        "exact_target_health",
        "exact_base_or_effective_armor",
        "exact_attackability",
        "exact_wow_unit_classification",
        "exact_coordinates",
        "simulator_execution_started",
        "superiority_result",
    ):
        if claim.get(field) is not False:
            raise FuryCapsuleExecutionBindingV2Error(
                f"capsule claim boundary widened at {field}"
            )

    source_contract = _mapping(bundle.get("source_contract"), "capsule.source_contract")
    transport = _mapping(bundle.get("transport_contract"), "capsule.transport_contract")
    if (
        source_contract.get("raw_csv_or_normalized_rows_embedded") is not False
        or source_contract.get("raw_upload_required") is not False
        or transport.get("upload_capsule_only") is not True
        or transport.get("raw_upload_required") is not False
        or transport.get("raw_csv_uploaded") is not False
        or transport.get("normalized_jsonl_uploaded") is not False
    ):
        raise FuryCapsuleExecutionBindingV2Error(
            "capsule transport contract does not permit capsule-only execution binding"
        )

    sources = _array(bundle.get("sources"), "capsule.sources")
    source_catalogs: dict[str, str] = {}
    for index, raw_source in enumerate(sources):
        source = _mapping(raw_source, f"capsule.sources[{index}]")
        source_id = _sha(source.get("source_bundle_id"), f"capsule.sources[{index}].source_bundle_id")
        if source_id in source_catalogs:
            raise FuryCapsuleExecutionBindingV2Error(
                f"duplicate source bundle ID: {source_id}"
            )
        catalog = _mapping(source.get("catalog"), f"capsule.sources[{index}].catalog")
        source_catalogs[source_id] = _sha(
            catalog.get("sha256"), f"capsule.sources[{index}].catalog.sha256"
        )

    raw_scenarios = _array(bundle.get("scenarios"), "capsule.scenarios")
    validated: list[JSONMap] = []
    seen_keys: set[tuple[str, str]] = set()
    target_count = 0
    strata: Counter[str] = Counter()
    for index, raw_scenario in enumerate(raw_scenarios):
        scenario = _strict_json_copy(
            _mapping(raw_scenario, f"capsule.scenarios[{index}]"),
            f"capsule.scenarios[{index}]",
        )
        _validate_capsule_scenario(scenario, source_catalogs)
        projection = _mapping(scenario["runner_projection"], "scenario.runner_projection")
        key = (
            _text(projection.get("instance_id"), "runner_projection.instance_id"),
            _text(scenario.get("scenario_id"), "scenario.scenario_id"),
        )
        if key in seen_keys:
            raise FuryCapsuleExecutionBindingV2Error(
                f"duplicate capsule runner key: {key}"
            )
        seen_keys.add(key)
        targets = _array(scenario.get("targets"), "scenario.targets")
        target_count += len(targets)
        strata[_text(projection.get("stratum"), "runner_projection.stratum")] += 1
        validated.append(scenario)

    expected_order = sorted(
        validated,
        key=lambda row: (
            str(_mapping(row["runner_projection"], "runner_projection")["instance_id"]),
            str(row["scenario_id"]),
        ),
    )
    if validated != expected_order:
        raise FuryCapsuleExecutionBindingV2Error(
            "capsule scenarios are not in deterministic runner order"
        )

    summary = _mapping(bundle.get("summary"), "capsule.summary")
    if _integer(summary.get("scenario_count"), "summary.scenario_count") != len(validated):
        raise FuryCapsuleExecutionBindingV2Error("capsule scenario count mismatch")
    if _integer(summary.get("target_count"), "summary.target_count") != target_count:
        raise FuryCapsuleExecutionBindingV2Error("capsule target count mismatch")
    declared_strata = _mapping(summary.get("stratum_counts"), "summary.stratum_counts")
    if dict(sorted(strata.items())) != dict(declared_strata):
        raise FuryCapsuleExecutionBindingV2Error("capsule stratum counts mismatch")
    if _integer(summary.get("source_bundle_count"), "summary.source_bundle_count") != len(sources):
        raise FuryCapsuleExecutionBindingV2Error("capsule source bundle count mismatch")
    if summary.get("raw_csv_or_normalized_byte_count_uploaded") != 0:
        raise FuryCapsuleExecutionBindingV2Error(
            "capsule summary unexpectedly reports raw/normalized uploaded bytes"
        )
    return tuple(validated)


def _validate_capsule_scenario(
    scenario: Mapping[str, Any], source_catalogs: Mapping[str, str]
) -> None:
    scenario_id = _text(scenario.get("scenario_id"), "scenario.scenario_id")
    if scenario.get("historical_truth") is not False:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: historical_truth must remain false"
        )
    if scenario.get("claim_scope") != "PREREGISTERED_SIMULATOR_SCENARIO_MODEL_ONLY":
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: unsupported claim scope"
        )
    source_id = _sha(scenario.get("source_bundle_id"), f"{scenario_id}.source_bundle_id")
    if source_id not in source_catalogs:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: source bundle is absent from capsule sources"
        )

    horizon = _mapping(scenario.get("horizon"), f"{scenario_id}.horizon")
    horizon_ms = _positive_integer(horizon.get("milliseconds"), f"{scenario_id}.horizon.ms")
    if (
        horizon.get("status") != "RECONSTRUCTED_HOSTILE_ACTIVITY_SPAN"
        or horizon.get("not_equal_to") != "exact pull duration or exact attackability duration"
    ):
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: horizon uncertainty boundary changed"
        )
    eligibility = _mapping(
        scenario.get("execution_mode_eligibility"), f"{scenario_id}.execution_mode_eligibility"
    )
    if (
        eligibility.get("fixed_observed_horizon_duration_model") is not True
        or eligibility.get("health_enabled_endogenous_ttk") is not False
    ):
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: unsupported execution eligibility"
        )

    armor_family = _array(
        scenario.get("base_armor_hypothesis_family"),
        f"{scenario_id}.base_armor_hypothesis_family",
    )
    if len(armor_family) != 1:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: static binding requires exactly one base-armor branch"
        )
    armor_branch = _mapping(armor_family[0], f"{scenario_id}.base_armor_branch")
    _nonnegative_number(armor_branch.get("base_armor"), f"{scenario_id}.base_armor")
    if (
        armor_branch.get("status") != "SENSITIVITY_HYPOTHESIS"
        or armor_branch.get("not_identified_by_chronicle") is not True
    ):
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: base armor is not a declared sensitivity hypothesis"
        )

    projection = _mapping(scenario.get("runner_projection"), f"{scenario_id}.runner_projection")
    provenance = _mapping(scenario.get("provenance_hashes"), f"{scenario_id}.provenance_hashes")
    for field in (
        "corpus_entry_sha256",
        "source_scenario_sha256",
        "source_sha256",
        "request_sha256",
        "pile_sha256",
        "target_hypotheses_sha256",
        "kill_budget_proxies_sha256",
    ):
        _sha(provenance.get(field), f"{scenario_id}.provenance_hashes.{field}")
    if projection.get("request_sha256") != provenance.get("request_sha256"):
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: runner/original request provenance mismatch"
        )
    if _text(projection.get("instance_id"), f"{scenario_id}.instance_id") != _text(
        _mapping(scenario.get("source_identity"), f"{scenario_id}.source_identity").get("instance_id"),
        f"{scenario_id}.source_identity.instance_id",
    ):
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: source and runner instance IDs differ"
        )
    stratum = _text(projection.get("stratum"), f"{scenario_id}.stratum")
    if stratum not in {"single_target", "multi_target"}:
        raise FuryCapsuleExecutionBindingV2Error(f"{scenario_id}: invalid stratum")
    _positive_number(projection.get("scenario_weight"), f"{scenario_id}.scenario_weight")

    targets = _array(scenario.get("targets"), f"{scenario_id}.targets")
    if not targets:
        raise FuryCapsuleExecutionBindingV2Error(f"{scenario_id}: no targets")
    if (stratum == "single_target") != (len(targets) == 1):
        raise FuryCapsuleExecutionBindingV2Error(
            f"{scenario_id}: target count conflicts with stratum"
        )
    seen_guids: set[str] = set()
    for target_index, raw_target in enumerate(targets):
        target = _mapping(raw_target, f"{scenario_id}.targets[{target_index}]")
        if _integer(target.get("target_index"), "target.target_index") != target_index:
            raise FuryCapsuleExecutionBindingV2Error(
                f"{scenario_id}: target indices are not contiguous"
            )
        guid = _text(target.get("target_guid"), "target.target_guid")
        if guid in seen_guids:
            raise FuryCapsuleExecutionBindingV2Error(
                f"{scenario_id}: duplicate target GUID"
            )
        seen_guids.add(guid)
        display = target.get("display_name")
        if display is not None and (not isinstance(display, str) or not display.strip()):
            raise FuryCapsuleExecutionBindingV2Error(
                f"{scenario_id}: target display_name must be nonempty text or null"
            )
        _validate_target_uncertainty(target, horizon_ms, scenario_id, target_index)


def _validate_target_uncertainty(
    target: Mapping[str, Any], horizon_ms: int, scenario_id: str, target_index: int
) -> None:
    label = f"{scenario_id}.targets[{target_index}]"
    exact_attackability = _mapping(
        target.get("attackability_cause_exact"), f"{label}.attackability_cause_exact"
    )
    if exact_attackability != {"status": "MISSING", "value": None}:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: exact attackability must remain missing"
        )
    branches = _array(
        target.get("attackable_window_hypothesis_family"),
        f"{label}.attackable_window_hypothesis_family",
    )
    full_wave = [
        _mapping(branch, f"{label}.attackable_window_branch")
        for branch in branches
        if isinstance(branch, Mapping) and branch.get("branch_id") == "full_wave"
    ]
    if len(full_wave) != 1 or full_wave[0].get("windows") != [[0, horizon_ms]]:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: one exact full-wave static hypothesis is required"
        )
    if full_wave[0].get("status") != "SENSITIVITY_HYPOTHESIS":
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: full-wave attackability must remain a sensitivity hypothesis"
        )

    health = _mapping(
        target.get("max_health_hypothesis_family"), f"{label}.max_health_hypothesis_family"
    )
    if health.get("historical_truth") is not False:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: health family cannot claim historical truth"
        )
    if health.get("health_enabled_endogenous_ttk_execution_eligible") is not False:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: endogenous TTK unexpectedly became eligible"
        )
    exact_health = _mapping(health.get("exact_max_health"), f"{label}.exact_max_health")
    if exact_health != {"status": "MISSING", "value": None}:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: exact max health must remain missing"
        )
    duration_branches = [
        _mapping(branch, f"{label}.health_branch")
        for branch in _array(health.get("shared_branch_family"), f"{label}.shared_branch_family")
        if isinstance(branch, Mapping) and branch.get("branch_id") == "duration_only"
    ]
    if len(duration_branches) != 1 or duration_branches[0] != {
        "branch_id": "duration_only",
        "max_health": None,
        "status": "CONTROL_BRANCH",
        "use_health": False,
    }:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: duration-only health control branch changed"
        )

    classification = _mapping(target.get("classification"), f"{label}.classification")
    if _mapping(
        classification.get("wow_unit_classification_exact"),
        f"{label}.wow_unit_classification_exact",
    ) != {"status": "MISSING", "value": None}:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: exact UnitClassification must remain missing"
        )
    if set(_array(classification.get("simulator_hypothesis_family"), f"{label}.classification_family")) != {
        "non_worldboss",
        "worldboss",
    }:
        raise FuryCapsuleExecutionBindingV2Error(
            f"{label}: classification sensitivity family changed"
        )

    armor = _mapping(target.get("armor"), f"{label}.armor")
    for exact_field in ("base_armor_exact", "effective_armor_exact"):
        if _mapping(armor.get(exact_field), f"{label}.{exact_field}") != {
            "status": "MISSING",
            "value": None,
        }:
            raise FuryCapsuleExecutionBindingV2Error(
                f"{label}: exact armor must remain missing"
            )
    for transition_index, raw_transition in enumerate(
        _array(armor.get("observed_transitions"), f"{label}.observed_transitions")
    ):
        transition = _mapping(raw_transition, f"{label}.transition[{transition_index}]")
        offset = _integer(transition.get("offset_ms"), "armor transition offset_ms")
        if offset < 0 or offset > horizon_ms:
            raise FuryCapsuleExecutionBindingV2Error(
                f"{label}: armor transition lies outside the fixed horizon"
            )
        if (
            transition.get("operation") not in {"set", "remove"}
            or transition.get("status") != "OBSERVED_AURA_TRANSITION"
            or transition.get("observed_spell_id") is not None
            or transition.get("spell_id_status") != "MISSING_IN_COMPACT_FEATURE_V1"
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                f"{label}: armor transition cannot be bound to simulator mechanics"
            )


def compile_capsule_static_execution_binding_v2(
    bundle: Mapping[str, Any],
    base_request: Mapping[str, Any],
    *,
    target_level: int,
    armor_mode: str = STATIC_BASE_ARMOR_MODE,
    attackability_mode: str = FULL_WAVE_ATTACKABILITY_MODE,
    health_mode: str = DURATION_ONLY_HEALTH_MODE,
) -> JSONMap:
    """Compile the only currently supported static capsule execution branch.

    The returned ``runner_projection.scenarios`` values are canonical objects
    accepted by :func:`normalize_runner_scenarios`.  The target-semantic
    receipts intentionally remain non-historical and do not claim to be the
    exact contexts required for a definitive full-policy comparison.
    """

    if armor_mode != STATIC_BASE_ARMOR_MODE:
        raise FuryCapsuleExecutionBindingV2Error(
            "DYNAMIC_ARMOR_SCHEDULE_UNSUPPORTED: this binding can execute only "
            "the static base-armor sensitivity branch"
        )
    if attackability_mode != FULL_WAVE_ATTACKABILITY_MODE:
        raise FuryCapsuleExecutionBindingV2Error(
            "DYNAMIC_ATTACKABILITY_SCHEDULE_UNSUPPORTED: this binding can execute "
            "only the full-wave static sensitivity branch"
        )
    if health_mode != DURATION_ONLY_HEALTH_MODE:
        raise FuryCapsuleExecutionBindingV2Error(
            "HEALTH_ENABLED_ENDOGENOUS_TTK_UNSUPPORTED: background team damage is "
            "not identified by the compact capsule"
        )
    level = _positive_integer(target_level, "target_level")
    capsules = enumerate_capsule_scenarios_v2(bundle)
    base = _validate_base_request(base_request)
    base_request_sha = sha256_json(base)

    source_catalogs = {
        _sha(_mapping(raw, "capsule source").get("source_bundle_id"), "source_bundle_id"):
        _sha(
            _mapping(_mapping(raw, "capsule source").get("catalog"), "source catalog").get("sha256"),
            "source catalog SHA",
        )
        for raw in _array(bundle.get("sources"), "capsule.sources")
    }
    runner_candidates: list[JSONMap] = []
    semantic_receipts: list[JSONMap] = []
    for capsule in capsules:
        scenario_id = str(capsule["scenario_id"])
        level_match = _SCENARIO_LEVEL.search(scenario_id)
        if level_match is None:
            raise FuryCapsuleExecutionBindingV2Error(
                f"{scenario_id}: capsule v2 lacks an explicit target-level field and "
                "the versioned scenario ID has no level suffix"
            )
        if int(level_match.group(1)) != level:
            raise FuryCapsuleExecutionBindingV2Error(
                f"{scenario_id}: caller target_level={level} conflicts with the "
                f"versioned scenario level={level_match.group(1)}"
            )
        horizon_ms = int(_mapping(capsule["horizon"], "scenario.horizon")["milliseconds"])
        armor_branch = _mapping(
            _array(capsule["base_armor_hypothesis_family"], "base armor family")[0],
            "base armor branch",
        )
        base_armor = _nonnegative_number(armor_branch.get("base_armor"), "base armor")
        request, targets_receipt = _compile_request(
            base,
            capsule,
            horizon_ms=horizon_ms,
            target_level=level,
            base_armor=base_armor,
        )
        request_sha = sha256_json(request)
        projection = _mapping(capsule["runner_projection"], "runner_projection")
        provenance = _mapping(capsule["provenance_hashes"], "provenance_hashes")
        targets = _array(capsule["targets"], "scenario.targets")
        limitation_codes = [
            "DYNAMIC_ARMOR_SCHEDULE_NOT_EXECUTED",
            "DYNAMIC_ATTACKABILITY_SCHEDULE_NOT_EXECUTED",
            "ENDOGENOUS_TTK_BACKGROUND_DAMAGE_UNBOUND",
            "EXACT_TARGET_CONTEXT_UNBOUND",
            "WOW_UNIT_CLASSIFICATION_UNBOUND",
        ]
        target_context_bundle: JSONMap = {
            "schema_version": 2,
            "kind": TARGET_CONTEXT_BUNDLE_KIND,
            "binding_status": "STATIC_CAPSULE_SENSITIVITY_NONVOTING",
            "request_sha256": request_sha,
            "target_count": len(targets),
            "contexts": deepcopy(targets_receipt),
            "comparison_eligible": False,
            "bridge_execution_eligible": False,
            "limitation_codes": limitation_codes,
        }
        target_context_bundle_sha = sha256_json(target_context_bundle)
        dynamic_target_models = []
        for raw_target in targets:
            target = _mapping(raw_target, "scenario target")
            dynamic_target_models.append(
                {
                    "target_index": target["target_index"],
                    "max_health_hypothesis_family": deepcopy(
                        target["max_health_hypothesis_family"]
                    ),
                    "background_team_damage_model": deepcopy(
                        target["background_team_damage_model"]
                    ),
                    "classification": deepcopy(target["classification"]),
                    "armor": deepcopy(target["armor"]),
                    "attackable_window_hypothesis_family": deepcopy(
                        target["attackable_window_hypothesis_family"]
                    ),
                    "attackability_cause_exact": deepcopy(
                        target["attackability_cause_exact"]
                    ),
                }
            )
        scenario_model: JSONMap = {
            "schema_version": 2,
            "kind": SCENARIO_MODEL_KIND,
            "model_status": "STATIC_CAPSULE_SENSITIVITY_NONVOTING",
            "request_sha256": request_sha,
            "target_context_bundle_sha256": target_context_bundle_sha,
            "historical_truth": False,
            "comparison_eligible": False,
            "bridge_execution_eligible": False,
            "dynamic_armor_schedule_status": "OBSERVED_RECEIPTS_RETAINED_NOT_EXECUTED",
            "dynamic_attackability_schedule_status": (
                "PROXY_AND_HYPOTHESIS_RECEIPTS_RETAINED_NOT_EXECUTED"
            ),
            "health_or_horizon_status": (
                "FIXED_OBSERVED_ACTIVITY_SPAN_NO_TARGET_DEATH"
            ),
            "dynamic_semantics_receipt": {
                "status": "RETAINED_NONVOTING_NOT_EXECUTED",
                "target_models": dynamic_target_models,
            },
            "limitation_codes": limitation_codes,
        }
        runner_candidates.append(
            {
                "instance_id": projection["instance_id"],
                "component_id": projection["component_id"],
                "scenario_id": scenario_id,
                "stratum": projection["stratum"],
                "scenario_weight": projection["scenario_weight"],
                "horizon_ms": horizon_ms,
                "estimated_cost_units": horizon_ms * len(targets),
                "request": request,
                "scenario_model": scenario_model,
                "scenario_model_sha256": sha256_json(scenario_model),
                "target_context_bundle": target_context_bundle,
                "target_context_bundle_sha256": target_context_bundle_sha,
                "corpus_entry_sha256": provenance["corpus_entry_sha256"],
                "source_scenario_sha256": provenance["source_scenario_sha256"],
                "catalog_sha256": source_catalogs[str(capsule["source_bundle_id"])],
            }
        )
        dynamic_armor_count = sum(
            len(_array(_mapping(target["armor"], "target.armor")["observed_transitions"], "armor transitions"))
            for target in targets
        )
        non_full_attackability_count = sum(
            sum(
                1
                for branch in _array(
                    _mapping(target, "target")["attackable_window_hypothesis_family"],
                    "attackability branches",
                )
                if _mapping(branch, "attackability branch").get("branch_id") != "full_wave"
            )
            for target in targets
        )
        semantic_receipts.append(
            {
                "scenario_id": scenario_id,
                "capsule_sha256": capsule["capsule_sha256"],
                "historical_truth": False,
                "original_catalog_request_sha256": provenance["request_sha256"],
                "compiled_request_sha256": request_sha,
                "scenario_model_sha256": sha256_json(scenario_model),
                "target_context_bundle_sha256": target_context_bundle_sha,
                "compiled_request_reproduces_original_catalog_request": (
                    request_sha == provenance["request_sha256"]
                ),
                "target_semantics": targets_receipt,
                "excluded_dynamic_evidence": {
                    "armor_transition_count": dynamic_armor_count,
                    "non_full_wave_attackability_branch_count": non_full_attackability_count,
                    "dynamic_armor_schedule_executed": False,
                    "dynamic_attackability_schedule_executed": False,
                },
                "execution_eligibility": {
                    "paired_runner_scenario_contract": True,
                    "static_duration_control_request": True,
                    "dynamic_armor_replay": False,
                    "dynamic_attackability_replay": False,
                    "health_enabled_endogenous_ttk": False,
                    "exact_full_policy_target_context": False,
                    "comparison_eligible": False,
                    "historical_replay_claim": False,
                    "superiority_claim_from_this_binding_alone": False,
                },
            }
        )

    try:
        normalized = list(normalize_runner_scenarios(runner_candidates))
    except FuryPairedRunnerError as exc:
        raise FuryCapsuleExecutionBindingV2Error(
            f"paired runner rejected capsule-bound scenarios: {exc}"
        ) from exc
    scenario_bundle_sha = runner_scenario_bundle_sha256(normalized)
    capsule_address = _sha(
        _mapping(bundle.get("content_address"), "capsule.content_address").get("sha256"),
        "capsule content address",
    )
    runner_core: JSONMap = {
        "kind": "fury_capsule_bound_runner_inputs_v2",
        "capsule_content_sha256": capsule_address,
        "base_request_sha256": base_request_sha,
        "target_level": level,
        "scenarios": normalized,
    }
    runner_projection: JSONMap = {
        **runner_core,
        "runner_inputs_sha256": sha256_json(runner_core),
        "runner_scenario_bundle_sha256": scenario_bundle_sha,
        "scenario_model_bundle_sha256": runner_scenario_model_bundle_sha256(
            normalized
        ),
        "target_context_bundle_set_sha256": runner_target_context_bundle_set_sha256(
            normalized
        ),
    }
    core: JSONMap = {
        "schema_version": SCHEMA_VERSION,
        "schema": SCHEMA,
        "kind": KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "historical_truth": False,
        "capsule_reference": {
            "schema": bundle.get("schema"),
            "kind": bundle.get("kind"),
            "content_sha256": capsule_address,
            "scenario_count": len(capsules),
            "raw_or_normalized_sources_opened": False,
        },
        "static_selection": {
            "target_level": level,
            "target_level_status": "CALLER_PINNED_SENSITIVITY_HYPOTHESIS",
            "target_level_scenario_id_suffix_verified": True,
            "armor_mode": armor_mode,
            "attackability_mode": attackability_mode,
            "health_mode": health_mode,
            "historical_truth": False,
        },
        "capability_boundary": {
            "capsule_only_offline_transport": True,
            "raw_or_normalized_upload_required": False,
            "fixed_observed_horizon_duration_requests_compiled": True,
            "static_base_armor_hypothesis_compiled": True,
            "static_full_wave_attackability_hypothesis_compiled": True,
            "dynamic_armor_schedule_supported": False,
            "dynamic_attackability_schedule_supported": False,
            "health_enabled_endogenous_ttk_supported": False,
            "exact_full_policy_target_context_compiled": False,
            "comparison_eligible": False,
            "exact_historical_replay": False,
            "simulator_execution_started": False,
            "superiority_result": False,
        },
        "runner_projection": runner_projection,
        "scenario_semantics": semantic_receipts,
        "summary": {
            "scenario_count": len(normalized),
            "target_count": sum(len(row["request"]["encounter"]["targets"]) for row in normalized),
            "stratum_counts": dict(sorted(Counter(row["stratum"] for row in normalized).items())),
            "original_catalog_request_match_count": sum(
                int(row["compiled_request_reproduces_original_catalog_request"])
                for row in semantic_receipts
            ),
            "dynamic_armor_transition_count_excluded": sum(
                row["excluded_dynamic_evidence"]["armor_transition_count"]
                for row in semantic_receipts
            ),
            "non_full_wave_attackability_branch_count_excluded": sum(
                row["excluded_dynamic_evidence"]["non_full_wave_attackability_branch_count"]
                for row in semantic_receipts
            ),
            "simulator_execution_started": False,
        },
    }
    result = deepcopy(core)
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    validate_capsule_static_execution_binding_v2(result)
    return result


def _validate_base_request(base_request: Mapping[str, Any]) -> JSONMap:
    base = _strict_json_copy(_mapping(base_request, "base_request"), "base_request")
    _mapping(base.get("raid"), "base_request.raid")
    encounter = _mapping(base.get("encounter"), "base_request.encounter")
    targets = _array(encounter.get("targets"), "base_request.encounter.targets")
    if not targets:
        raise FuryCapsuleExecutionBindingV2Error(
            "base_request.encounter.targets needs one static target template"
        )
    _mapping(targets[0], "base_request.encounter.targets[0]")
    sim_options = _mapping(base.get("simOptions"), "base_request.simOptions")
    if sim_options.get("iterations") != 1:
        raise FuryCapsuleExecutionBindingV2Error(
            "base_request.simOptions.iterations must be exactly 1 for paired seeds"
        )
    return base


def _compile_request(
    base_request: Mapping[str, Any],
    capsule: Mapping[str, Any],
    *,
    horizon_ms: int,
    target_level: int,
    base_armor: int | float,
) -> tuple[JSONMap, list[JSONMap]]:
    request = deepcopy(dict(base_request))
    encounter = dict(_mapping(request.get("encounter"), "base_request.encounter"))
    request["encounter"] = encounter
    template = _mapping(
        _array(encounter.get("targets"), "base_request.encounter.targets")[0],
        "base target template",
    )
    request_targets: list[JSONMap] = []
    receipts: list[JSONMap] = []
    for raw_target in _array(capsule.get("targets"), "capsule scenario targets"):
        target = _mapping(raw_target, "capsule scenario target")
        request_target = _static_request_target(
            template,
            target,
            target_level=target_level,
            base_armor=base_armor,
        )
        request_targets.append(request_target)
        target_index = int(target["target_index"])
        receipts.append(
            {
                "target_index": target_index,
                "target_guid_sha256": hashlib.sha256(
                    str(target["target_guid"]).encode("utf-8")
                ).hexdigest(),
                "request_name": request_target["name"],
                "name_status": "OBSERVED_OR_GUID_FALLBACK_FROM_CAPSULE",
                "target_level": target_level,
                "target_level_status": "CALLER_PINNED_SENSITIVITY_HYPOTHESIS",
                "mob_type": "MobTypeUnknown",
                "wow_unit_classification_exact": False,
                "base_armor": base_armor,
                "base_armor_status": "SENSITIVITY_HYPOTHESIS",
                "effective_armor_exact": False,
                "request_health_stat": 0,
                "exact_max_health": False,
                "attackability_mode": FULL_WAVE_ATTACKABILITY_MODE,
                "attackable_windows": [[0, horizon_ms]],
                "exact_attackability": False,
                "historical_truth": False,
                "field_status": {
                    "target_name": "OBSERVED_OR_GUID_FALLBACK",
                    "target_level": "CALLER_PINNED_HYPOTHESIS",
                    "wow_unit_classification": "MISSING",
                    "base_armor": "SENSITIVITY_HYPOTHESIS",
                    "effective_armor": "MISSING_DYNAMIC_NOT_EXECUTED",
                    "max_health": "MISSING",
                    "attackability": "FULL_WAVE_SENSITIVITY_HYPOTHESIS",
                },
            }
        )
    encounter["duration"] = _render_number(horizon_ms / 1000.0)
    encounter["durationVariation"] = 0
    encounter["useHealth"] = False
    encounter["targets"] = request_targets
    return _strict_json_copy(request, "compiled request"), receipts


def _static_request_target(
    template: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    target_level: int,
    base_armor: int | float,
) -> JSONMap:
    result = deepcopy(dict(template))
    stats_source = result.get("stats")
    if stats_source is not None and not isinstance(stats_source, list):
        raise FuryCapsuleExecutionBindingV2Error("base target stats must be an array")
    stats_length = max(
        _MIN_TARGET_STATS_LENGTH,
        len(stats_source) if isinstance(stats_source, list) else 0,
    )
    stats: list[int | float] = [0] * stats_length
    stats[ARMOR_STAT_INDEX] = base_armor
    stats[HEALTH_STAT_INDEX] = 0
    result.pop("id", None)
    display_name = target.get("display_name")
    result["name"] = str(display_name or target["target_guid"] or "Chronicle target")
    result["level"] = target_level
    result["mobType"] = "MobTypeUnknown"
    result["stats"] = stats
    for field in (
        "minBaseDamage",
        "damageSpread",
        "swingSpeed",
        "dualWield",
        "dualWieldPenalty",
        "parryHaste",
        "spellSchool",
        "targetInputs",
    ):
        result.pop(field, None)
    result["tankIndex"] = -1
    return result


def validate_capsule_static_execution_binding_v2(binding: Mapping[str, Any]) -> None:
    """Validate the binding and re-run the paired runner's scenario normalizer."""

    if set(binding) != {
        "schema_version",
        "schema",
        "kind",
        "implementation_revision",
        "historical_truth",
        "capsule_reference",
        "static_selection",
        "capability_boundary",
        "runner_projection",
        "scenario_semantics",
        "summary",
        "content_address",
    }:
        raise FuryCapsuleExecutionBindingV2Error("execution binding field set mismatch")
    if (
        binding.get("schema_version") != SCHEMA_VERSION
        or binding.get("schema") != SCHEMA
        or binding.get("kind") != KIND
        or binding.get("implementation_revision") != IMPLEMENTATION_REVISION
        or binding.get("historical_truth") is not False
    ):
        raise FuryCapsuleExecutionBindingV2Error("invalid execution binding schema")
    address = _mapping(binding.get("content_address"), "binding.content_address")
    declared = _sha(address.get("sha256"), "binding content address")
    core = deepcopy(dict(binding))
    core.pop("content_address", None)
    if sha256_json(core) != declared:
        raise FuryCapsuleExecutionBindingV2Error("execution binding content address mismatch")

    capability = _mapping(binding.get("capability_boundary"), "binding.capability_boundary")
    expected_true = (
        "capsule_only_offline_transport",
        "fixed_observed_horizon_duration_requests_compiled",
        "static_base_armor_hypothesis_compiled",
        "static_full_wave_attackability_hypothesis_compiled",
    )
    expected_false = (
        "raw_or_normalized_upload_required",
        "dynamic_armor_schedule_supported",
        "dynamic_attackability_schedule_supported",
        "health_enabled_endogenous_ttk_supported",
        "exact_full_policy_target_context_compiled",
        "comparison_eligible",
        "exact_historical_replay",
        "simulator_execution_started",
        "superiority_result",
    )
    if set(capability) != set(expected_true).union(expected_false):
        raise FuryCapsuleExecutionBindingV2Error(
            "execution capability field set mismatch"
        )
    if any(capability.get(field) is not True for field in expected_true) or any(
        capability.get(field) is not False for field in expected_false
    ):
        raise FuryCapsuleExecutionBindingV2Error("execution capability boundary widened")

    capsule_reference = _mapping(
        binding.get("capsule_reference"), "binding.capsule_reference"
    )
    if set(capsule_reference) != {
        "schema",
        "kind",
        "content_sha256",
        "scenario_count",
        "raw_or_normalized_sources_opened",
    }:
        raise FuryCapsuleExecutionBindingV2Error("capsule reference field set mismatch")
    if (
        capsule_reference.get("schema") != _CAPSULE_SCHEMA
        or capsule_reference.get("kind") != _CAPSULE_KIND
        or capsule_reference.get("raw_or_normalized_sources_opened") is not False
    ):
        raise FuryCapsuleExecutionBindingV2Error("invalid compact capsule reference")
    capsule_sha = _sha(
        capsule_reference.get("content_sha256"), "capsule reference content SHA"
    )

    selection = _mapping(binding.get("static_selection"), "binding.static_selection")
    if set(selection) != {
        "target_level",
        "target_level_status",
        "target_level_scenario_id_suffix_verified",
        "armor_mode",
        "attackability_mode",
        "health_mode",
        "historical_truth",
    }:
        raise FuryCapsuleExecutionBindingV2Error("static selection field set mismatch")
    if (
        selection.get("armor_mode") != STATIC_BASE_ARMOR_MODE
        or selection.get("attackability_mode") != FULL_WAVE_ATTACKABILITY_MODE
        or selection.get("health_mode") != DURATION_ONLY_HEALTH_MODE
        or selection.get("historical_truth") is not False
        or selection.get("target_level_status")
        != "CALLER_PINNED_SENSITIVITY_HYPOTHESIS"
        or selection.get("target_level_scenario_id_suffix_verified") is not True
    ):
        raise FuryCapsuleExecutionBindingV2Error("unsupported static selection")
    level = _positive_integer(selection.get("target_level"), "selection.target_level")

    runner = _mapping(binding.get("runner_projection"), "binding.runner_projection")
    if set(runner) != {
        "kind",
        "capsule_content_sha256",
        "base_request_sha256",
        "target_level",
        "scenarios",
        "runner_inputs_sha256",
        "runner_scenario_bundle_sha256",
        "scenario_model_bundle_sha256",
        "target_context_bundle_set_sha256",
    }:
        raise FuryCapsuleExecutionBindingV2Error("runner projection field set mismatch")
    if (
        runner.get("kind") != "fury_capsule_bound_runner_inputs_v2"
        or runner.get("capsule_content_sha256") != capsule_sha
    ):
        raise FuryCapsuleExecutionBindingV2Error("runner/capsule reference mismatch")
    raw_scenarios = _array(runner.get("scenarios"), "runner_projection.scenarios")
    _sha(runner.get("base_request_sha256"), "runner base-request SHA")
    try:
        normalized = list(normalize_runner_scenarios(raw_scenarios))
    except FuryPairedRunnerError as exc:
        raise FuryCapsuleExecutionBindingV2Error(
            f"paired runner rejected bound scenarios: {exc}"
        ) from exc
    if normalized != raw_scenarios:
        raise FuryCapsuleExecutionBindingV2Error(
            "runner scenarios are not stored in canonical normalized form"
        )
    expected_bundle_sha = runner_scenario_bundle_sha256(normalized)
    if runner.get("runner_scenario_bundle_sha256") != expected_bundle_sha:
        raise FuryCapsuleExecutionBindingV2Error("runner scenario bundle SHA mismatch")
    if runner.get("scenario_model_bundle_sha256") != runner_scenario_model_bundle_sha256(
        normalized
    ):
        raise FuryCapsuleExecutionBindingV2Error("runner scenario-model bundle SHA mismatch")
    if runner.get(
        "target_context_bundle_set_sha256"
    ) != runner_target_context_bundle_set_sha256(normalized):
        raise FuryCapsuleExecutionBindingV2Error(
            "runner target-context bundle-set SHA mismatch"
        )
    runner_core = {
        "kind": runner.get("kind"),
        "capsule_content_sha256": runner.get("capsule_content_sha256"),
        "base_request_sha256": runner.get("base_request_sha256"),
        "target_level": runner.get("target_level"),
        "scenarios": raw_scenarios,
    }
    if runner.get("runner_inputs_sha256") != sha256_json(runner_core):
        raise FuryCapsuleExecutionBindingV2Error("runner input SHA mismatch")
    if runner.get("target_level") != level:
        raise FuryCapsuleExecutionBindingV2Error("runner target level mismatch")

    semantics = _array(binding.get("scenario_semantics"), "binding.scenario_semantics")
    if len(semantics) != len(normalized):
        raise FuryCapsuleExecutionBindingV2Error("scenario semantic receipt count mismatch")
    if capsule_reference.get("scenario_count") != len(normalized):
        raise FuryCapsuleExecutionBindingV2Error("capsule reference scenario count mismatch")
    for index, (runner_row, raw_receipt) in enumerate(zip(normalized, semantics)):
        receipt = _mapping(raw_receipt, f"scenario_semantics[{index}]")
        if set(receipt) != {
            "scenario_id",
            "capsule_sha256",
            "historical_truth",
            "original_catalog_request_sha256",
            "compiled_request_sha256",
            "scenario_model_sha256",
            "target_context_bundle_sha256",
            "compiled_request_reproduces_original_catalog_request",
            "target_semantics",
            "excluded_dynamic_evidence",
            "execution_eligibility",
        }:
            raise FuryCapsuleExecutionBindingV2Error(
                "scenario semantic receipt field set mismatch"
            )
        _sha(receipt.get("capsule_sha256"), "scenario capsule SHA")
        original_request_sha = _sha(
            receipt.get("original_catalog_request_sha256"),
            "scenario original request SHA",
        )
        compiled_request_sha = _sha(
            receipt.get("compiled_request_sha256"),
            "scenario compiled request SHA",
        )
        if receipt.get("compiled_request_reproduces_original_catalog_request") is not (
            compiled_request_sha == original_request_sha
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "scenario original-request match flag is inconsistent"
            )
        if (
            receipt.get("scenario_id") != runner_row["scenario_id"]
            or receipt.get("historical_truth") is not False
            or receipt.get("compiled_request_sha256") != runner_row["request_sha256"]
            or receipt.get("scenario_model_sha256")
            != runner_row["scenario_model_sha256"]
            or receipt.get("target_context_bundle_sha256")
            != runner_row["target_context_bundle_sha256"]
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "scenario semantic receipt differs from runner projection"
            )
        excluded = _mapping(
            receipt.get("excluded_dynamic_evidence"),
            f"scenario_semantics[{index}].excluded_dynamic_evidence",
        )
        if set(excluded) != {
            "armor_transition_count",
            "non_full_wave_attackability_branch_count",
            "dynamic_armor_schedule_executed",
            "dynamic_attackability_schedule_executed",
        }:
            raise FuryCapsuleExecutionBindingV2Error(
                "excluded dynamic-evidence field set mismatch"
            )
        if (
            excluded.get("dynamic_armor_schedule_executed") is not False
            or excluded.get("dynamic_attackability_schedule_executed") is not False
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "a dynamic schedule was relabelled as executed"
            )
        eligibility = _mapping(
            receipt.get("execution_eligibility"),
            f"scenario_semantics[{index}].execution_eligibility",
        )
        if set(eligibility) != {
            "paired_runner_scenario_contract",
            "static_duration_control_request",
            "dynamic_armor_replay",
            "dynamic_attackability_replay",
            "health_enabled_endogenous_ttk",
            "exact_full_policy_target_context",
            "comparison_eligible",
            "historical_replay_claim",
            "superiority_claim_from_this_binding_alone",
        }:
            raise FuryCapsuleExecutionBindingV2Error(
                "scenario execution-eligibility field set mismatch"
            )
        if (
            eligibility.get("paired_runner_scenario_contract") is not True
            or eligibility.get("static_duration_control_request") is not True
            or eligibility.get("dynamic_armor_replay") is not False
            or eligibility.get("dynamic_attackability_replay") is not False
            or eligibility.get("health_enabled_endogenous_ttk") is not False
            or eligibility.get("exact_full_policy_target_context") is not False
            or eligibility.get("comparison_eligible") is not False
            or eligibility.get("historical_replay_claim") is not False
            or eligibility.get("superiority_claim_from_this_binding_alone") is not False
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "scenario execution eligibility boundary widened"
            )
        request_targets = _array(
            _mapping(runner_row["request"]["encounter"], "request.encounter").get("targets"),
            "request targets",
        )
        target_receipts = _array(receipt.get("target_semantics"), "target semantics")
        target_bundle = _mapping(
            runner_row.get("target_context_bundle"), "runner target-context bundle"
        )
        scenario_model = _mapping(
            runner_row.get("scenario_model"), "runner scenario model"
        )
        if (
            target_bundle.get("binding_status")
            != "STATIC_CAPSULE_SENSITIVITY_NONVOTING"
            or target_bundle.get("comparison_eligible") is not False
            or target_bundle.get("bridge_execution_eligible") is not False
            or target_bundle.get("contexts") != target_receipts
            or scenario_model.get("model_status")
            != "STATIC_CAPSULE_SENSITIVITY_NONVOTING"
            or scenario_model.get("comparison_eligible") is not False
            or scenario_model.get("bridge_execution_eligible") is not False
            or scenario_model.get("dynamic_armor_schedule_status")
            != "OBSERVED_RECEIPTS_RETAINED_NOT_EXECUTED"
            or scenario_model.get("dynamic_attackability_schedule_status")
            != "PROXY_AND_HYPOTHESIS_RECEIPTS_RETAINED_NOT_EXECUTED"
            or scenario_model.get("health_or_horizon_status")
            != "FIXED_OBSERVED_ACTIVITY_SPAN_NO_TARGET_DEATH"
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "static capsule nonvoting scenario semantics were widened or lost"
            )
        dynamic_receipt = _mapping(
            scenario_model.get("dynamic_semantics_receipt"),
            "runner scenario dynamic-semantics receipt",
        )
        dynamic_targets = _array(
            dynamic_receipt.get("target_models"), "dynamic target models"
        )
        if (
            dynamic_receipt.get("status") != "RETAINED_NONVOTING_NOT_EXECUTED"
            or len(dynamic_targets) != len(request_targets)
            or [
                _mapping(value, "dynamic target model").get("target_index")
                for value in dynamic_targets
            ]
            != list(range(len(request_targets)))
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "dynamic target semantics are not retained in target order"
            )
        retained_transition_count = sum(
            len(
                _array(
                    _mapping(
                        _mapping(value, "dynamic target model").get("armor"),
                        "dynamic target armor",
                    ).get("observed_transitions"),
                    "dynamic target armor transitions",
                )
            )
            for value in dynamic_targets
        )
        if retained_transition_count != excluded.get("armor_transition_count"):
            raise FuryCapsuleExecutionBindingV2Error(
                "retained dynamic armor transition count mismatch"
            )
        retained_non_full_attackability_count = sum(
            sum(
                1
                for branch in _array(
                    _mapping(value, "dynamic target model").get(
                        "attackable_window_hypothesis_family"
                    ),
                    "dynamic attackability family",
                )
                if _mapping(branch, "dynamic attackability branch").get(
                    "branch_id"
                )
                != "full_wave"
            )
            for value in dynamic_targets
        )
        if retained_non_full_attackability_count != excluded.get(
            "non_full_wave_attackability_branch_count"
        ):
            raise FuryCapsuleExecutionBindingV2Error(
                "retained non-full-wave attackability count mismatch"
            )
        if len(target_receipts) != len(request_targets):
            raise FuryCapsuleExecutionBindingV2Error("target semantic receipt count mismatch")
        for target_index, (request_target, raw_target_receipt) in enumerate(
            zip(request_targets, target_receipts)
        ):
            target_receipt = _mapping(raw_target_receipt, "target semantic receipt")
            target_request = _mapping(request_target, "request target")
            stats = _array(target_request.get("stats"), "request target stats")
            if (
                target_receipt.get("target_index") != target_index
                or target_receipt.get("historical_truth") is not False
                or target_receipt.get("exact_max_health") is not False
                or target_receipt.get("effective_armor_exact") is not False
                or target_receipt.get("exact_attackability") is not False
                or target_receipt.get("wow_unit_classification_exact") is not False
                or target_receipt.get("attackability_mode") != FULL_WAVE_ATTACKABILITY_MODE
                or target_receipt.get("target_level") != level
                or target_request.get("level") != level
                or target_request.get("mobType") != "MobTypeUnknown"
                or target_request.get("tankIndex") != -1
                or stats[ARMOR_STAT_INDEX] != target_receipt.get("base_armor")
                or stats[HEALTH_STAT_INDEX] != 0
            ):
                raise FuryCapsuleExecutionBindingV2Error(
                    "request target differs from its static semantic receipt"
                )

    summary = _mapping(binding.get("summary"), "binding.summary")
    if set(summary) != {
        "scenario_count",
        "target_count",
        "stratum_counts",
        "original_catalog_request_match_count",
        "dynamic_armor_transition_count_excluded",
        "non_full_wave_attackability_branch_count_excluded",
        "simulator_execution_started",
    }:
        raise FuryCapsuleExecutionBindingV2Error("binding summary field set mismatch")
    expected_strata = dict(sorted(Counter(row["stratum"] for row in normalized).items()))
    expected_original_matches = sum(
        int(row["compiled_request_reproduces_original_catalog_request"])
        for row in semantics
    )
    expected_transition_count = sum(
        int(_mapping(row["excluded_dynamic_evidence"], "excluded")["armor_transition_count"])
        for row in semantics
    )
    expected_attackability_count = sum(
        int(
            _mapping(row["excluded_dynamic_evidence"], "excluded")[
                "non_full_wave_attackability_branch_count"
            ]
        )
        for row in semantics
    )
    if (
        summary.get("scenario_count") != len(normalized)
        or summary.get("target_count")
        != sum(len(row["request"]["encounter"]["targets"]) for row in normalized)
        or summary.get("stratum_counts") != expected_strata
        or summary.get("original_catalog_request_match_count")
        != expected_original_matches
        or summary.get("dynamic_armor_transition_count_excluded")
        != expected_transition_count
        or summary.get("non_full_wave_attackability_branch_count_excluded")
        != expected_attackability_count
        or summary.get("simulator_execution_started") is not False
    ):
        raise FuryCapsuleExecutionBindingV2Error("binding summary mismatch")


def extract_runner_scenarios_v2(binding: Mapping[str, Any]) -> tuple[JSONMap, ...]:
    """Return canonical deep copies ready for ``build_runner_plan``."""

    validate_capsule_static_execution_binding_v2(binding)
    runner = _mapping(binding.get("runner_projection"), "binding.runner_projection")
    return tuple(
        deepcopy(row)
        for row in _array(runner.get("scenarios"), "runner_projection.scenarios")
    )


def write_capsule_static_execution_binding_v2(
    binding: Mapping[str, Any], output_path: Path
) -> Path:
    """Write a deterministic JSON or JSON.GZ binding artifact."""

    validate_capsule_static_execution_binding_v2(binding)
    path = output_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        binding,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if path.suffix.casefold() == ".gz":
        with path.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                zipped.write(rendered)
    else:
        path.write_bytes(rendered)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capsule", type=Path, required=True)
    parser.add_argument("--base-request", type=Path, required=True)
    parser.add_argument("--target-level", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FuryCapsuleExecutionBindingV2Error(
            f"could not load {label} {path}: {exc}"
        ) from exc
    return _strict_json_copy(_mapping(value, label), label)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    capsule = load_capsule_bundle_v2(args.capsule)
    base_request = _load_json_object(args.base_request, "base request")
    binding = compile_capsule_static_execution_binding_v2(
        capsule,
        base_request,
        target_level=args.target_level,
    )
    output = write_capsule_static_execution_binding_v2(binding, args.output)
    print(
        json.dumps(
            {
                "kind": "fury_capsule_static_execution_binding_receipt_v2",
                "output": output.as_posix(),
                "output_size_bytes": output.stat().st_size,
                "output_file_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "binding_content_sha256": binding["content_address"]["sha256"],
                "capsule_content_sha256": binding["capsule_reference"]["content_sha256"],
                "runner_scenario_bundle_sha256": binding["runner_projection"][
                    "runner_scenario_bundle_sha256"
                ],
                "summary": binding["summary"],
                "historical_truth": False,
                "simulator_execution_started": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


__all__ = (
    "DURATION_ONLY_HEALTH_MODE",
    "FULL_WAVE_ATTACKABILITY_MODE",
    "FuryCapsuleExecutionBindingV2Error",
    "STATIC_BASE_ARMOR_MODE",
    "compile_capsule_static_execution_binding_v2",
    "enumerate_capsule_scenarios_v2",
    "extract_runner_scenarios_v2",
    "load_capsule_bundle_v2",
    "validate_capsule_static_execution_binding_v2",
    "write_capsule_static_execution_binding_v2",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
