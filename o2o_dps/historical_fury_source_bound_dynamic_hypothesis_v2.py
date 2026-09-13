"""Compile one explicit source-bound dynamic-v3 hypothesis pair.

The v1 environment artifact intentionally identifies no exact HP, armor, or
attackability schedule.  This module does not rewrite that evidence.  It
selects a narrowly labelled *development sensitivity hypothesis* for every
source-bound build and materializes a wire pair only where a single origin
target is already present at the first decision and remains the complete
strict-prefix registry through the last bound decision.

The one materialized pair is suitable only for a local native wire smoke.  Its
HP is the full-source, zero-healing kill-budget proxy, its armor is the generic
source-request value, its attackability is an observed-activity envelope, and
its team trace is the fixed historical leave-one-player-out schedule.  None of
those assumptions authorizes policy valuation, comparison, training, HPC, or
deployment.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import historical_fury_source_bound_dynamic_config_v1 as dynamic_v1
from . import historical_fury_source_bound_environment_evidence_v1 as evidence_v1
from . import historical_fury_source_bound_prototype_bundle_v1 as source_v1
from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import DynamicAttackabilityEventV2
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicTargetSemanticsConfigV3,
    dynamic_target_semantics_config_from_wire_v3,
)


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_dynamic_hypothesis/v2"
IMPLEMENTATION_REVISION = "v2.0_single_origin_diagnostic_wire_smoke_only"
KIND = "historical_fury_source_bound_dynamic_hypothesis_manifest"
STATUS = "DEVELOPMENT_HYPOTHESIS_PREPARED_NOT_EXECUTED"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_dynamic_hypothesis_content/v2"
)
PAIR_SCHEMA = "historical_fury_source_bound_dynamic_hypothesis_pair/v2"
READY_STATUS = "READY_FOR_LOCAL_NATIVE_WIRE_SMOKE_ONLY"
BLOCKED_STATUS = "BLOCKED_UNSAFE_TO_MATERIALIZE"
EXPECTED_REQUEST_COUNT = 9
READY_SEGMENT_REF = (
    "sha256:a4508c00b4481dadc0f7b4fbcc078ee5b5cdcbc9ecdda312ca13ed998eef313a"
)
HYPOTHESIS_IDS = {
    "execution": "diagnostic-20.001s-v1",
    "registry": "strict-prefix-stable-single-origin-v1",
    "health": "full-source-zero-heal-kill-budget-v1",
    "armor": "source-request-stats26-static-v1",
    "attackability": "observed-activity-envelope-v1",
    "team": "fixed-exact-player-loo-diagnostic-v1",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_MANIFEST = evidence_v1.DEFAULT_OUTPUT
DEFAULT_SOURCE_BUNDLE = source_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_DYNAMIC_V1_MANIFEST = dynamic_v1.DEFAULT_OUTPUT
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_dynamic_hypothesis"
    / "v2"
)
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIRECTORY / "manifest.json"

_BOUNDARY_FIELDS = (
    "value_ready",
    "policy_value_authorized",
    "comparison_authorized",
    "training_authorized",
    "hpc_authorized",
    "deployment_authorized",
    "superiority_claim_authorized",
)


class HistoricalFurySourceBoundDynamicHypothesisV2Error(RuntimeError):
    """A v2 hypothesis input, pair, or manifest is inconsistent."""


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> JSONMap:
    result: JSONMap = {}
    for key, value in pairs:
        if key in result:
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON token: {token}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"cannot load {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be an object"
        )
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be a finite number"
        )
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive " if positive else ""
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} must be a {qualifier}finite number"
        )
    return number


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address; host locators forbidden",
            "sha256": _sha256(core),
        },
    }


def _looks_absolute(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        normalized.startswith("/")
        or normalized.startswith("//")
        or (
            len(normalized) > 2
            and normalized[1] == ":"
            and normalized[2] == "/"
        )
    )


def _assert_path_free(value: Any, *, label: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_path_free(child, label=f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_path_free(child, label=f"{label}[{index}]")
    elif isinstance(value, str) and _looks_absolute(value):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"{label} contains an absolute host locator"
        )


def _input_material(
    *,
    evidence_manifest_path: Path,
    source_bundle_path: Path,
    dynamic_v1_manifest_path: Path,
) -> tuple[JSONMap, list[JSONMap], JSONMap, JSONMap, JSONMap]:
    try:
        evidence_manifest, rows = (
            evidence_v1.load_historical_fury_source_bound_environment_evidence_v1(
                evidence_manifest_path, verify_partition=True
            )
        )
        source = source_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
            _load_json(source_bundle_path, "source-bound prototype bundle"),
            verify_source_bytes=False,
        )
        preparation = dynamic_v1.load_historical_fury_source_bound_dynamic_config_v1(
            dynamic_v1_manifest_path
        )
    except (
        evidence_v1.HistoricalFurySourceBoundEnvironmentEvidenceV1Error,
        source_v1.HistoricalFurySourceBoundPrototypeBundleV1Error,
        dynamic_v1.HistoricalFurySourceBoundDynamicConfigV1Error,
    ) as error:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(str(error)) from error

    if len(rows) != EXPECTED_REQUEST_COUNT:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"environment evidence must contain {EXPECTED_REQUEST_COUNT} rows"
        )
    if (
        preparation.get("status") != dynamic_v1.STATUS
        or _mapping(preparation.get("summary"), "v1 summary").get(
            "ready_request_count"
        )
        != 0
        or any(
            row.get("derived_request_template") is not None
            or row.get("dynamic_load_config") is not None
            for row in _array(preparation.get("requests"), "v1 requests")
        )
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "v1 preparation is not the immutable zero-READY input"
        )

    closure = _mapping(evidence_manifest.get("input_closure"), "evidence closure")
    source_sha = _text(
        _mapping(source.get("content_address"), "source content address").get(
            "sha256"
        ),
        "source content sha256",
    )
    evidence_source_sha = _mapping(
        closure.get("source_bound_prototype_bundle"), "evidence source closure"
    ).get("content_sha256")
    preparation_sha = _text(
        _mapping(preparation.get("content_address"), "v1 content address").get(
            "sha256"
        ),
        "v1 content sha256",
    )
    evidence_preparation_sha = _mapping(
        closure.get("source_bound_dynamic_preparation"),
        "evidence preparation closure",
    ).get("content_sha256")
    if source_sha != evidence_source_sha or preparation_sha != evidence_preparation_sha:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "environment evidence does not bind the supplied source/v1 inputs"
        )

    source_rows = {
        _text(_mapping(raw, "source request").get("segment_ref"), "segment_ref"):
        _mapping(raw, "source request")
        for raw in _array(source.get("requests"), "source requests")
    }
    if set(source_rows) != {str(row["segment_ref"]) for row in rows}:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "source bundle and evidence segment sets differ"
        )
    for row in rows:
        source_row = source_rows[str(row["segment_ref"])]
        if source_row.get("request_sha256") != row.get("base_request_sha256"):
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "source bundle and evidence request digests differ"
            )

    descriptor = _mapping(
        evidence_manifest.get("evidence_partition"), "evidence partition"
    )
    seed_contract = _mapping(source.get("development_seeds"), "development seeds")
    input_closure = {
        "environment_evidence_v1": {
            "schema": evidence_manifest.get("schema"),
            "implementation_revision": evidence_manifest.get(
                "implementation_revision"
            ),
            "content_sha256": _mapping(
                evidence_manifest.get("content_address"), "evidence address"
            ).get("sha256"),
            "partition_logical_content_sha256": descriptor.get(
                "logical_content_sha256"
            ),
            "partition_record_count": descriptor.get("record_count"),
        },
        "source_bound_prototype_bundle_v1": {
            "schema": source.get("schema"),
            "implementation_revision": source.get("implementation_revision"),
            "content_sha256": source_sha,
            "request_count": len(source_rows),
            "development_seed_list_sha256": seed_contract.get(
                "seed_list_sha256"
            ),
        },
        "dynamic_preparation_v1": {
            "schema": preparation.get("schema"),
            "implementation_revision": preparation.get("implementation_revision"),
            "content_sha256": preparation_sha,
            "ready_request_count": 0,
            "role": "IMMUTABLE_FAIL_CLOSED_PREDECESSOR",
        },
    }
    return evidence_manifest, rows, source, preparation, input_closure


def _casefold_set(values: Sequence[Any]) -> set[str]:
    return {str(value).casefold() for value in values}


def _source_target_template(source_row: Mapping[str, Any]) -> tuple[JSONMap, float]:
    request = _mapping(
        _mapping(source_row.get("composition"), "source composition").get("request"),
        "source request",
    )
    encounter = _mapping(request.get("encounter"), "source encounter")
    targets = _array(encounter.get("targets"), "source targets")
    if len(targets) != 1:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "source-bound duration request must contain one target template"
        )
    target = deepcopy(dict(_mapping(targets[0], "source target template")))
    stats = _array(target.get("stats"), "source target stats")
    if len(stats) <= max(ARMOR_STAT_INDEX, HEALTH_STAT_INDEX):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "source target stats omit armor or health index"
        )
    armor = _number(stats[ARMOR_STAT_INDEX], "source target stats[26]")
    if armor < 0:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "source target armor hypothesis cannot be negative"
        )
    return target, armor


def _prototype_contract(source_row: Mapping[str, Any]) -> JSONMap:
    bindings = []
    for raw in _array(
        source_row.get("historical_policy_bindings"),
        "historical policy bindings",
    ):
        binding = _mapping(raw, "historical policy binding")
        bindings.append(
            {
                "prototype_id": _text(
                    binding.get("prototype_id"), "prototype_id"
                ),
                "policy_id": _text(binding.get("policy_id"), "policy_id"),
                "model_schema": binding.get("model_schema"),
                "model_implementation_revision": binding.get(
                    "model_implementation_revision"
                ),
                "model_content_sha256": binding.get("model_content_sha256"),
                "model_file_sha256": binding.get("model_file_sha256"),
                "model_file_size_bytes": binding.get("model_file_size_bytes"),
                "segment_weighted_decision_support": binding.get(
                    "segment_weighted_decision_support"
                ),
            }
        )
    if not bindings:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "source request has no historical prototype binding"
        )
    return {
        "binding_scope": (
            "PROTOTYPE_MODEL_ROUTED_THROUGH_AN_OBSERVED_MEMBER_SOURCE_BUILD"
        ),
        "bindings": bindings,
        "model_is_build_conditioned": False,
        "policy_value_authorized": False,
        "comparison_authorized": False,
    }


def _origin_contract(evidence_row: Mapping[str, Any]) -> tuple[JSONMap | None, JSONMap]:
    registry = _mapping(
        evidence_row.get("target_registry_evidence"), "target registry evidence"
    )
    initial = _array(
        registry.get("strict_prefix_alive_at_first_bound_decision"),
        "initial alive registry",
    )
    seen_last = _array(
        registry.get("strict_prefix_seen_by_last_bound_decision"),
        "last strict-prefix registry",
    )
    stable_single = (
        len(initial) == 1
        and len(seen_last) == 1
        and _casefold_set(initial) == _casefold_set(seen_last)
    )
    diagnostic_targets = _array(
        registry.get("diagnostic_slice_event_time_voting_damage_target_guids"),
        "diagnostic event-time target registry",
    )
    target = None
    if len(initial) == 1:
        matches = [
            _mapping(raw, "evidence target")
            for raw in _array(evidence_row.get("targets"), "evidence targets")
            if str(_mapping(raw, "evidence target").get("target_guid")).casefold()
            == str(initial[0]).casefold()
        ]
        if len(matches) != 1:
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "initial origin target does not resolve exactly once"
            )
        target = matches[0]
    return target, {
        "selection_mode": (
            "STRICT_PREFIX_SINGLE_ORIGIN_STABLE_THROUGH_LAST_BOUND_DECISION"
        ),
        "initial_alive_target_guids": deepcopy(initial),
        "last_prefix_seen_target_guids": deepcopy(seen_last),
        "diagnostic_event_time_voting_damage_target_guids": deepcopy(
            diagnostic_targets
        ),
        "candidate_origin_target_guid": (
            target.get("target_guid") if target is not None else None
        ),
        "selected_simulator_target_guids": (
            [target.get("target_guid")] if stable_single and target is not None else []
        ),
        "excluded_diagnostic_event_time_target_guids": sorted(
            str(guid)
            for guid in diagnostic_targets
            if target is None
            or str(guid).casefold() != str(target.get("target_guid")).casefold()
        ),
        "selected_target_count": 1 if stable_single and target is not None else 0,
        "future_target_backfill_used": False,
        "future_registry_leak_or_unstable_origin": not stable_single,
        "historical_truth": False,
        "role": "CAUSAL_REGISTRY_HYPOTHESIS_FOR_WIRE_SMOKE_ONLY",
    }


def _health_contract(target: Mapping[str, Any] | None) -> tuple[float | None, JSONMap]:
    if target is None:
        return None, {
            "status": "MISSING_NO_UNIQUE_PREFIX_ORIGIN",
            "value": None,
            "source": "FULL_SOURCE_ZERO_HEALING_KILL_BUDGET_PROXY",
            "observed_healing_received": None,
            "uses_outcome_suffix": True,
            "exact_initial_or_max_health": False,
            "historical_truth": False,
            "policy_input_authorized": False,
        }
    health = _mapping(target.get("health_evidence"), "target health evidence")
    outcome = _mapping(target.get("historical_outcome"), "historical outcome")
    proxy = health.get("retrospective_kill_budget_proxy")
    healing = outcome.get("observed_healing_received")
    death = _mapping(target.get("death"), "target death").get("observed") is True
    valid = (
        isinstance(proxy, (int, float))
        and not isinstance(proxy, bool)
        and float(proxy) > 0
        and healing == 0
        and death
    )
    return (float(proxy) if valid else None), {
        "status": (
            "SENSITIVITY_HYPOTHESIS_FROM_ZERO_HEALING_KILL_BUDGET"
            if valid
            else "MISSING_VALID_ZERO_HEALING_KILL_BUDGET"
        ),
        "value": float(proxy) if valid else None,
        "source": "FULL_SOURCE_ZERO_HEALING_KILL_BUDGET_PROXY",
        "source_proxy_value": proxy,
        "observed_healing_received": healing,
        "source_observed_dead": death,
        "uses_outcome_suffix": True,
        "exact_initial_or_max_health": False,
        "historical_truth": False,
        "policy_input_authorized": False,
    }


def _attackability_events(
    *, target: Mapping[str, Any], start_ms: int, horizon_ms: int
) -> tuple[tuple[DynamicAttackabilityEventV2, ...], JSONMap]:
    first = _integer(
        _mapping(target.get("first_observed_activity_anchor"), "first activity").get(
            "timestamp_ms"
        ),
        "first activity timestamp",
    )
    last = _integer(
        _mapping(target.get("last_observed_activity_anchor"), "last activity").get(
            "timestamp_ms"
        ),
        "last activity timestamp",
    )
    if last < first:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "target activity envelope ends before it starts"
        )
    end_ms = start_ms + horizon_ms
    raw: list[tuple[int, bool]] = [(0, first <= start_ms <= last)]
    if start_ms < first <= end_ms:
        raw.append((first - start_ms, True))
    if start_ms <= last < end_ms and last - start_ms + 1 <= horizon_ms:
        raw.append((last - start_ms + 1, False))
    # A transition at the same timestamp supersedes the initial state.
    collapsed: dict[int, bool] = {}
    for time_ms, attackable in raw:
        collapsed[time_ms] = attackable
    ordered = sorted(collapsed.items())
    events = tuple(
        DynamicAttackabilityEventV2(index, time_ms, 0, attackable)
        for index, (time_ms, attackable) in enumerate(ordered)
    )
    return events, {
        "status": "OBSERVED_ACTIVITY_ENVELOPE_SENSITIVITY_HYPOTHESIS",
        "source_first_activity_timestamp_ms": first,
        "source_last_activity_timestamp_ms": last,
        "compiled_events": [event.to_wire() for event in events],
        "exact_intervals": False,
        "silent_intervals_are_unattackable_assumption": True,
        "historical_truth": False,
        "policy_input_authorized": False,
    }


def _selected_loo_events(
    *, evidence_row: Mapping[str, Any], target_guid: str, horizon_ms: int
) -> tuple[tuple[BackgroundDamageEventV1, ...], JSONMap]:
    team = _mapping(evidence_row.get("team_trace"), "team trace")
    focal = _text(team.get("focal_player_guid"), "focal player guid")
    selected: list[Mapping[str, Any]] = []
    for raw in _array(
        team.get("leave_one_out_exact_player_damage_events"), "LOO events"
    ):
        event = _mapping(raw, "LOO event")
        membership = _mapping(event.get("window_membership"), "event membership")
        time_ms = _integer(event.get("offset_ms"), "LOO event offset")
        if (
            str(event.get("target_guid")).casefold() == target_guid.casefold()
            and membership.get("base_request_diagnostic_slice") is True
            and time_ms <= horizon_ms
        ):
            if str(event.get("actor_player_guid")).casefold() == focal.casefold():
                raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                    "focal player damage survived the evidence LOO lane"
                )
            selected.append(event)
    selected.sort(
        key=lambda event: (
            int(event["offset_ms"]),
            tuple(event.get("order_key", ())),
        )
    )
    source_projection = []
    typed = []
    for index, event in enumerate(selected):
        anchor = _mapping(event.get("anchor"), "LOO anchor")
        message_sha = _text(
            anchor.get("official_message_sha256"), "LOO official message sha256"
        )
        event_id = (
            f"loo:{index}:{message_sha[:16]}:"
            f"{_integer(anchor.get('event_index'), 'LOO event index')}"
        )
        damage = _number(event.get("damage"), "LOO damage", positive=True)
        time_ms = _integer(event.get("offset_ms"), "LOO event offset")
        typed.append(
            BackgroundDamageEventV1(index, time_ms, 0, event_id, damage)
        )
        source_projection.append(
            {
                "official_message_sha256": message_sha,
                "order_key": deepcopy(event.get("order_key")),
                "offset_ms": time_ms,
                "source_guid": event.get("source_guid"),
                "target_guid": event.get("target_guid"),
                "spell_id": event.get("spell_id"),
                "damage": damage,
            }
        )
    return tuple(typed), {
        "status": "FIXED_HISTORICAL_EXACT_PLAYER_LOO_DIAGNOSTIC_SCHEDULE",
        "source": "ENVIRONMENT_EVIDENCE_V1_LEAVE_ONE_OUT_EXACT_PLAYER_DAMAGE",
        "selected_origin_target_guid": target_guid,
        "selected_source_event_count": len(selected),
        "selected_source_damage": sum(event.damage for event in typed),
        "selected_source_events_content_sha256": _sha256(source_projection),
        "compiled_background_event_count": len(typed),
        "compiled_background_damage": sum(event.damage for event in typed),
        "focal_player_guid": focal,
        "focal_damage_in_background": False,
        "unattributed_damage_in_background": False,
        "candidate_responsive": False,
        "counterfactual_kill_clock_calibrated": False,
        "future_schedule_exported_to_policy_context": False,
        "historical_truth_under_candidate_policy": False,
        "policy_value_authorized": False,
    }


def _compile_request(
    *,
    source_row: Mapping[str, Any],
    target: Mapping[str, Any],
    health: float,
    armor: float,
    horizon_ms: int,
) -> JSONMap:
    base = deepcopy(
        dict(
            _mapping(
                _mapping(source_row.get("composition"), "source composition").get(
                    "request"
                ),
                "base request",
            )
        )
    )
    encounter = deepcopy(dict(_mapping(base.get("encounter"), "base encounter")))
    base["encounter"] = encounter
    target_template, _ = _source_target_template(source_row)
    stats = list(_array(target_template.get("stats"), "target template stats"))
    stats[ARMOR_STAT_INDEX] = armor
    stats[HEALTH_STAT_INDEX] = health
    target_template["stats"] = stats
    entry = target.get("creature_entry_id")
    target_template["name"] = f"Source-bound origin target {entry} hypothesis"
    encounter["duration"] = horizon_ms / 1000.0
    encounter["durationVariation"] = 0
    encounter["useHealth"] = True
    encounter["targets"] = [target_template]
    return json.loads(_canonical_bytes(base).decode("utf-8"))


def _build_row(
    *, evidence_row: Mapping[str, Any], source_row: Mapping[str, Any]
) -> JSONMap:
    segment_ref = _text(evidence_row.get("segment_ref"), "segment_ref")
    base_request_sha = _text(
        evidence_row.get("base_request_sha256"), "base request sha256"
    )
    source_request = _mapping(
        _mapping(source_row.get("composition"), "source composition").get("request"),
        "source request",
    )
    if _sha256(source_request) != base_request_sha:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "base request digest differs from source bundle"
        )
    _, armor = _source_target_template(source_row)
    prototype_contract = _prototype_contract(source_row)
    target, registry = _origin_contract(evidence_row)
    health_value, health = _health_contract(target)
    diagnostic = _mapping(
        _mapping(
            evidence_row.get("execution_window_contract"), "execution window"
        ).get("base_request_diagnostic_slice"),
        "diagnostic slice",
    )
    diagnostic_duration = _integer(
        diagnostic.get("duration_ms"), "diagnostic duration", minimum=1
    )
    stable_registry = registry["future_registry_leak_or_unstable_origin"] is False
    safe_horizon = diagnostic_duration if stable_registry else 0
    execution = {
        "selection_mode": "SOURCE_REQUEST_20_001_SECOND_DIAGNOSTIC_SLICE",
        "role": "LOCAL_NATIVE_WIRE_SMOKE_ONLY_NOT_WAVE_OR_KILL_CLOCK",
        "start_timestamp_ms": diagnostic.get("start_timestamp_ms"),
        "end_timestamp_ms": diagnostic.get("end_timestamp_ms"),
        "candidate_duration_ms": diagnostic_duration,
        "selected_horizon_ms": safe_horizon,
        "covers_all_bound_decisions": diagnostic.get("covers_all_bound_decisions"),
        "future_outcome_used_to_extend_window": False,
        "historical_truth": False,
    }
    armor_contract = {
        "status": "STATIC_SOURCE_REQUEST_ARMOR_SENSITIVITY_HYPOTHESIS",
        "base_armor": armor,
        "source": "BASE_SOURCE_REQUEST_TARGET_STATS_26",
        "source_stats_index": ARMOR_STAT_INDEX,
        "exact_base_or_effective_armor": False,
        "dynamic_effective_armor_events": [],
        "dynamic_t_positive_event_count": 0,
        "start_or_go_cast_candidates_promoted_to_aura_state": False,
        "candidate_endogenous_armor_effects_encoded_as_environment": False,
        "historical_truth": False,
        "policy_input_authorized": False,
    }
    blockers: list[JSONMap] = []
    if not stable_registry:
        blockers.extend(
            [
                {
                    "code": "FUTURE_TARGET_REGISTRY_LEAK",
                    "detail": (
                        "a single origin target is not the complete strict-prefix "
                        "registry through the last bound decision"
                    ),
                },
                {
                    "code": "ZERO_SAFE_EXECUTION_HORIZON",
                    "detail": (
                        "the conservative single-origin contract assigns no "
                        "executable horizon when the registry is unstable"
                    ),
                },
            ]
        )
    if health_value is None:
        blockers.append(
            {
                "code": "MISSING_ZERO_HEALING_ORIGIN_KILL_BUDGET_HP",
                "detail": (
                    "no unique dead origin target has a positive full-source "
                    "kill-budget proxy with zero observed healing"
                ),
            }
        )

    attackability: JSONMap
    team: JSONMap
    request_template: JSONMap | None = None
    request_sha: str | None = None
    config_wire: JSONMap | None = None
    pair_binding: JSONMap | None = None
    if not blockers and target is not None and health_value is not None:
        attack_events, attackability = _attackability_events(
            target=target,
            start_ms=_integer(
                diagnostic.get("start_timestamp_ms"), "diagnostic start"
            ),
            horizon_ms=safe_horizon,
        )
        background_events, team = _selected_loo_events(
            evidence_row=evidence_row,
            target_guid=_text(target.get("target_guid"), "origin target guid"),
            horizon_ms=safe_horizon,
        )
        config = DynamicTargetSemanticsConfigV3(
            target_health=(DynamicTargetHealthV1(0, health_value),),
            idle_advance_horizon_ms=safe_horizon,
            background_damage_events=background_events,
            attackability_events=attack_events,
            effective_armor_events=(),
        )
        config_wire = config.to_wire()
        request_template = _compile_request(
            source_row=source_row,
            target=target,
            health=health_value,
            armor=armor,
            horizon_ms=safe_horizon,
        )
        request_sha = _sha256(request_template)
        base_seed = _mapping(source_request.get("simOptions"), "base simOptions").get(
            "randomSeed"
        )
        derived_seed = _mapping(
            request_template.get("simOptions"), "derived simOptions"
        ).get("randomSeed")
        if base_seed != derived_seed:
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "hypothesis compilation changed the source request seed"
            )
        pair_core = {
            "schema": PAIR_SCHEMA,
            "segment_ref": segment_ref,
            "base_request_sha256": base_request_sha,
            "derived_request_template_sha256": request_sha,
            "dynamic_load_config_content_sha256": config.content_sha256,
            "prototype_ids": [
                binding["prototype_id"]
                for binding in prototype_contract["bindings"]
            ],
            "hypothesis_ids": deepcopy(HYPOTHESIS_IDS),
        }
        pair_binding = {**pair_core, "content_sha256": _sha256(pair_core)}
        status = READY_STATUS
    else:
        attackability = {
            "status": "BLOCKED_NO_SAFE_SINGLE_ORIGIN_WINDOW",
            "compiled_events": None,
            "exact_intervals": False,
            "silent_intervals_are_unattackable_assumption": True,
            "historical_truth": False,
            "policy_input_authorized": False,
        }
        team = {
            "status": "BLOCKED_NO_MATERIALIZED_BACKGROUND_SCHEDULE",
            "source": "ENVIRONMENT_EVIDENCE_V1_LEAVE_ONE_OUT_EXACT_PLAYER_DAMAGE",
            "selected_origin_target_guid": (
                target.get("target_guid") if target is not None else None
            ),
            "selected_source_event_count": 0,
            "selected_source_damage": 0.0,
            "compiled_background_event_count": 0,
            "compiled_background_damage": 0.0,
            "focal_damage_in_background": False,
            "unattributed_damage_in_background": False,
            "candidate_responsive": False,
            "counterfactual_kill_clock_calibrated": False,
            "future_schedule_exported_to_policy_context": False,
            "historical_truth_under_candidate_policy": False,
            "policy_value_authorized": False,
        }
        status = BLOCKED_STATUS

    base_seed = _mapping(source_request.get("simOptions"), "base simOptions").get(
        "randomSeed"
    )
    row = {
        "segment_ref": segment_ref,
        "base_request_sha256": base_request_sha,
        "source_identity": deepcopy(evidence_row.get("source_identity")),
        "prototype_contract": prototype_contract,
        "evidence_row_content_sha256": _mapping(
            evidence_row.get("content_address"), "evidence row content address"
        ).get("sha256"),
        "status": status,
        "local_native_wire_smoke_authorized": status == READY_STATUS,
        **{field: False for field in _BOUNDARY_FIELDS},
        "observation_leak": {
            "present_in_health_hypothesis": health.get("uses_outcome_suffix") is True,
            "present_in_materialized_pair": (
                status == READY_STATUS and health.get("uses_outcome_suffix") is True
            ),
            "source": "FULL_SOURCE_OUTCOME_SUFFIX_KILL_BUDGET",
            "allowed_role": "LOCAL_NATIVE_WIRE_COMPATIBILITY_SMOKE_ONLY",
            "blocks_policy_value": True,
        },
        "hypotheses": {
            "execution_window": execution,
            "target_registry": registry,
            "target_health": health,
            "exogenous_armor": armor_contract,
            "attackability": attackability,
            "team_kill_clock": team,
        },
        "seed_contract": {
            "source_request_random_seed": base_seed,
            "derived_request_random_seed": (
                _mapping(request_template.get("simOptions"), "derived simOptions").get(
                    "randomSeed"
                )
                if request_template is not None
                else None
            ),
            "compiler_mutated_seed": False,
        },
        "blockers": blockers,
        "derived_request_template": request_template,
        "derived_request_template_sha256": request_sha,
        "dynamic_load_config": config_wire,
        "pair_binding": pair_binding,
    }
    return row


def build_historical_fury_source_bound_dynamic_hypothesis_v2(
    *,
    evidence_manifest_path: str | Path = DEFAULT_EVIDENCE_MANIFEST,
    source_bundle_path: str | Path = DEFAULT_SOURCE_BUNDLE,
    dynamic_v1_manifest_path: str | Path = DEFAULT_DYNAMIC_V1_MANIFEST,
) -> JSONMap:
    """Build the frozen nine-row development hypothesis manifest."""

    paths = [
        Path(value).expanduser().resolve()
        for value in (
            evidence_manifest_path,
            source_bundle_path,
            dynamic_v1_manifest_path,
        )
    ]
    _, evidence_rows, source, _, input_closure = _input_material(
        evidence_manifest_path=paths[0],
        source_bundle_path=paths[1],
        dynamic_v1_manifest_path=paths[2],
    )
    source_by_segment = {
        str(_mapping(raw, "source request")["segment_ref"]): _mapping(
            raw, "source request"
        )
        for raw in _array(source.get("requests"), "source requests")
    }
    rows = [
        _build_row(
            evidence_row=row,
            source_row=source_by_segment[str(row["segment_ref"])],
        )
        for row in sorted(evidence_rows, key=lambda item: str(item["segment_ref"]))
    ]
    blocker_counts: dict[str, int] = {}
    for row in rows:
        for blocker in row["blockers"]:
            code = str(blocker["code"])
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
    ready = [row for row in rows if row["status"] == READY_STATUS]
    core = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "status": STATUS,
        "execution_status": "NOT_RUN",
        "scientific_runs_started": False,
        "simulator_run_count": 0,
        "network_request_count": 0,
        "local_native_wire_smoke_authorized": len(ready) == 1,
        **{field: False for field in _BOUNDARY_FIELDS},
        "input_closure": input_closure,
        "hypothesis_boundary": {
            "historical_evidence_v1_mutated": False,
            "health_is_exact_historical_state": False,
            "armor_is_exact_historical_state": False,
            "attackability_is_exact_historical_state": False,
            "fixed_loo_schedule_is_counterfactual_team_response": False,
            "candidate_policy_changes_team_kill_clock": False,
            "observation_leak_present_in_ready_pair": True,
            "observation_leak_source": "FULL_SOURCE_OUTCOME_SUFFIX_KILL_BUDGET",
            "value_ready": False,
            "ready_pair_role": "LOCAL_NATIVE_WIRE_COMPATIBILITY_SMOKE_ONLY",
            "next_required_gate": (
                "CANDIDATE_RESPONSIVE_TEAM_KILL_CLOCK_CALIBRATION"
            ),
        },
        "summary": {
            "request_count": len(rows),
            "ready_for_local_native_wire_smoke_only_count": len(ready),
            "blocked_request_count": len(rows) - len(ready),
            "materialized_pair_count": len(ready),
            "future_target_registry_leak_blocked_count": sum(
                any(
                    blocker["code"] == "FUTURE_TARGET_REGISTRY_LEAK"
                    for blocker in row["blockers"]
                )
                for row in rows
            ),
            "zero_safe_execution_horizon_blocked_count": sum(
                any(
                    blocker["code"] == "ZERO_SAFE_EXECUTION_HORIZON"
                    for blocker in row["blockers"]
                )
                for row in rows
            ),
            "missing_valid_health_proxy_blocked_count": sum(
                any(
                    blocker["code"]
                    == "MISSING_ZERO_HEALING_ORIGIN_KILL_BUDGET_HP"
                    for blocker in row["blockers"]
                )
                for row in rows
            ),
            "compiled_background_event_count": sum(
                row["hypotheses"]["team_kill_clock"][
                    "compiled_background_event_count"
                ]
                for row in rows
            ),
            "blocker_counts": dict(sorted(blocker_counts.items())),
            "ready_segment_refs": [row["segment_ref"] for row in ready],
        },
        "requests": rows,
    }
    return validate_historical_fury_source_bound_dynamic_hypothesis_v2(
        _content_addressed(core)
    )


def _validate_pair(row: Mapping[str, Any]) -> None:
    request = _mapping(row.get("derived_request_template"), "derived request")
    request_sha = _sha256(request)
    if request_sha != row.get("derived_request_template_sha256"):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "derived request template digest differs"
        )
    try:
        config = dynamic_target_semantics_config_from_wire_v3(
            _mapping(row.get("dynamic_load_config"), "dynamic load config")
        )
    except (TypeError, ValueError) as error:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"dynamic-v3 config differs: {error}"
        ) from error
    hypotheses = _mapping(row.get("hypotheses"), "hypotheses")
    prototype = _mapping(row.get("prototype_contract"), "prototype contract")
    execution = _mapping(hypotheses.get("execution_window"), "execution hypothesis")
    registry = _mapping(hypotheses.get("target_registry"), "registry hypothesis")
    health = _mapping(hypotheses.get("target_health"), "health hypothesis")
    armor = _mapping(hypotheses.get("exogenous_armor"), "armor hypothesis")
    attackability = _mapping(
        hypotheses.get("attackability"), "attackability hypothesis"
    )
    team = _mapping(hypotheses.get("team_kill_clock"), "team hypothesis")
    encounter = _mapping(request.get("encounter"), "derived encounter")
    targets = _array(encounter.get("targets"), "derived targets")
    if (
        encounter.get("useHealth") is not True
        or len(targets) != 1
        or execution.get("selected_horizon_ms") != 20001
        or _number(encounter.get("duration"), "derived duration", positive=True)
        != 20.001
        or config.idle_advance_horizon_ms != 20001
        or registry.get("future_target_backfill_used") is not False
        or registry.get("future_registry_leak_or_unstable_origin") is not False
        or registry.get("selected_target_count") != 1
        or health.get("uses_outcome_suffix") is not True
        or health.get("exact_initial_or_max_health") is not False
        or health.get("observed_healing_received") != 0
        or armor.get("source_stats_index") != ARMOR_STAT_INDEX
        or armor.get("exact_base_or_effective_armor") is not False
        or armor.get("dynamic_effective_armor_events") != []
        or armor.get("dynamic_t_positive_event_count") != 0
        or config.effective_armor_events
        or attackability.get("exact_intervals") is not False
        or not config.attackability_events
        or team.get("candidate_responsive") is not False
        or team.get("counterfactual_kill_clock_calibrated") is not False
        or team.get("focal_damage_in_background") is not False
        or team.get("future_schedule_exported_to_policy_context") is not False
        or row.get("value_ready") is not False
        or _mapping(row.get("observation_leak"), "observation leak").get(
            "present_in_materialized_pair"
        )
        is not True
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "ready pair exceeds its explicit hypothesis boundary"
        )
    target = _mapping(targets[0], "derived target")
    stats = _array(target.get("stats"), "derived target stats")
    health_value = _number(health.get("value"), "health hypothesis", positive=True)
    armor_value = _number(armor.get("base_armor"), "armor hypothesis")
    if (
        len(stats) <= max(ARMOR_STAT_INDEX, HEALTH_STAT_INDEX)
        or stats[HEALTH_STAT_INDEX] != health_value
        or stats[ARMOR_STAT_INDEX] != armor_value
        or len(config.target_health) != 1
        or config.target_health[0].target_index != 0
        or config.target_health[0].health != health_value
        or len(config.background_damage_events)
        != team.get("compiled_background_event_count")
        or sum(event.damage for event in config.background_damage_events)
        != team.get("compiled_background_damage")
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "request/config target or background binding differs"
        )
    seed = _mapping(row.get("seed_contract"), "seed contract")
    if (
        seed.get("compiler_mutated_seed") is not False
        or seed.get("source_request_random_seed")
        != seed.get("derived_request_random_seed")
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "compiler changed the source request seed"
        )
    binding = _mapping(row.get("pair_binding"), "pair binding")
    pair_core = deepcopy(dict(binding))
    pair_sha = pair_core.pop("content_sha256", None)
    if (
        pair_core.get("schema") != PAIR_SCHEMA
        or pair_core.get("segment_ref") != row.get("segment_ref")
        or pair_core.get("base_request_sha256") != row.get("base_request_sha256")
        or pair_core.get("derived_request_template_sha256") != request_sha
        or pair_core.get("dynamic_load_config_content_sha256")
        != config.content_sha256
        or pair_core.get("prototype_ids")
        != [binding["prototype_id"] for binding in prototype["bindings"]]
        or pair_core.get("hypothesis_ids") != HYPOTHESIS_IDS
        or pair_sha != _sha256(pair_core)
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "derived request/config pair binding differs"
        )


def validate_historical_fury_source_bound_dynamic_hypothesis_v2(
    value: Mapping[str, Any],
) -> JSONMap:
    """Validate a published v2 manifest without reopening source partitions."""

    raw = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != KIND
        or raw.get("status") != STATUS
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "dynamic hypothesis implementation identity differs"
        )
    address = _mapping(raw.get("content_address"), "content address")
    core = deepcopy(raw)
    core.pop("content_address", None)
    expected_address = _content_addressed(core)["content_address"]
    if dict(address) != expected_address:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "dynamic hypothesis content address differs"
        )
    _assert_path_free(raw)
    if (
        raw.get("execution_status") != "NOT_RUN"
        or raw.get("scientific_runs_started") is not False
        or raw.get("simulator_run_count") != 0
        or raw.get("network_request_count") != 0
        or any(raw.get(field) is not False for field in _BOUNDARY_FIELDS)
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "dynamic hypothesis exceeded the local-smoke-only boundary"
        )
    predecessor = _mapping(
        _mapping(raw.get("input_closure"), "input closure").get(
            "dynamic_preparation_v1"
        ),
        "v1 predecessor",
    )
    if (
        predecessor.get("ready_request_count") != 0
        or predecessor.get("role") != "IMMUTABLE_FAIL_CLOSED_PREDECESSOR"
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "v1 zero-READY predecessor boundary differs"
        )
    rows = [
        _mapping(raw_row, f"requests[{index}]")
        for index, raw_row in enumerate(_array(raw.get("requests"), "requests"))
    ]
    if len(rows) != EXPECTED_REQUEST_COUNT:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "dynamic hypothesis request count differs"
        )
    seen: set[tuple[str, str]] = set()
    blocker_counts: dict[str, int] = {}
    ready: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        key = (
            _text(row.get("segment_ref"), f"requests[{index}].segment_ref"),
            _text(
                row.get("base_request_sha256"),
                f"requests[{index}].base_request_sha256",
            ),
        )
        if key in seen:
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "request identity is not unique"
            )
        seen.add(key)
        if any(row.get(field) is not False for field in _BOUNDARY_FIELDS):
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "request row exceeded the local-smoke-only boundary"
            )
        hypotheses = _mapping(row.get("hypotheses"), "row hypotheses")
        prototype = _mapping(row.get("prototype_contract"), "prototype contract")
        prototype_bindings = _array(
            prototype.get("bindings"), "prototype bindings"
        )
        if (
            not prototype_bindings
            or prototype.get("policy_value_authorized") is not False
            or prototype.get("comparison_authorized") is not False
        ):
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "request prototype binding boundary differs"
            )
        if set(hypotheses) != {
            "execution_window",
            "target_registry",
            "target_health",
            "exogenous_armor",
            "attackability",
            "team_kill_clock",
        }:
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "request does not declare all six environment hypotheses"
            )
        codes = {
            _text(_mapping(blocker, "blocker").get("code"), "blocker code")
            for blocker in _array(row.get("blockers"), "blockers")
        }
        for code in codes:
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
        if row.get("status") == READY_STATUS:
            if (
                row.get("local_native_wire_smoke_authorized") is not True
                or codes
                or row.get("segment_ref") != READY_SEGMENT_REF
            ):
                raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                    "unexpected or blocked row was marked wire-smoke ready"
                )
            _validate_pair(row)
            ready.append(row)
        elif row.get("status") == BLOCKED_STATUS:
            if (
                row.get("local_native_wire_smoke_authorized") is not False
                or row.get("derived_request_template") is not None
                or row.get("derived_request_template_sha256") is not None
                or row.get("dynamic_load_config") is not None
                or row.get("pair_binding") is not None
                or "FUTURE_TARGET_REGISTRY_LEAK" not in codes
                or "ZERO_SAFE_EXECUTION_HORIZON" not in codes
                or _mapping(
                    hypotheses.get("execution_window"), "blocked execution"
                ).get("selected_horizon_ms")
                != 0
            ):
                raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                    "blocked request materialized a pair or omitted its causal blocker"
                )
        else:
            raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
                "request has unsupported hypothesis status"
            )
    if [str(row["segment_ref"]) for row in ready] != [READY_SEGMENT_REF]:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "the frozen evidence must admit only the a4508c00 wire-smoke pair"
        )
    summary = _mapping(raw.get("summary"), "summary")
    expected = {
        "request_count": len(rows),
        "ready_for_local_native_wire_smoke_only_count": len(ready),
        "blocked_request_count": len(rows) - len(ready),
        "materialized_pair_count": len(ready),
        "future_target_registry_leak_blocked_count": sum(
            "FUTURE_TARGET_REGISTRY_LEAK"
            in {
                str(_mapping(blocker, "blocker")["code"])
                for blocker in row["blockers"]
            }
            for row in rows
        ),
        "zero_safe_execution_horizon_blocked_count": sum(
            "ZERO_SAFE_EXECUTION_HORIZON"
            in {
                str(_mapping(blocker, "blocker")["code"])
                for blocker in row["blockers"]
            }
            for row in rows
        ),
        "missing_valid_health_proxy_blocked_count": sum(
            "MISSING_ZERO_HEALING_ORIGIN_KILL_BUDGET_HP"
            in {
                str(_mapping(blocker, "blocker")["code"])
                for blocker in row["blockers"]
            }
            for row in rows
        ),
        "compiled_background_event_count": sum(
            int(row["hypotheses"]["team_kill_clock"][
                "compiled_background_event_count"
            ])
            for row in rows
        ),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "ready_segment_refs": [str(row["segment_ref"]) for row in ready],
    }
    if dict(summary) != expected or raw.get("local_native_wire_smoke_authorized") is not True:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "dynamic hypothesis readiness summary differs"
        )
    boundary = _mapping(raw.get("hypothesis_boundary"), "hypothesis boundary")
    if (
        boundary.get("historical_evidence_v1_mutated") is not False
        or boundary.get("health_is_exact_historical_state") is not False
        or boundary.get("armor_is_exact_historical_state") is not False
        or boundary.get("attackability_is_exact_historical_state") is not False
        or boundary.get("fixed_loo_schedule_is_counterfactual_team_response")
        is not False
        or boundary.get("candidate_policy_changes_team_kill_clock") is not False
        or boundary.get("observation_leak_present_in_ready_pair") is not True
        or boundary.get("observation_leak_source")
        != "FULL_SOURCE_OUTCOME_SUFFIX_KILL_BUDGET"
        or boundary.get("value_ready") is not False
    ):
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "manifest promoted a sensitivity hypothesis to historical truth"
        )
    return raw


def load_historical_fury_source_bound_dynamic_hypothesis_v2(
    path: str | Path = DEFAULT_OUTPUT,
) -> JSONMap:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        resolved = resolved / "manifest.json"
    return validate_historical_fury_source_bound_dynamic_hypothesis_v2(
        _load_json(resolved, "dynamic hypothesis manifest")
    )


def select_local_native_wire_smoke_pair_v2(
    artifact: Mapping[str, Any], *, segment_ref: str, base_request_sha256: str
) -> tuple[JSONMap, DynamicTargetSemanticsConfigV3]:
    """Select the sole pair without broadening its local wire-smoke role."""

    checked = validate_historical_fury_source_bound_dynamic_hypothesis_v2(artifact)
    matches = [
        row
        for row in checked["requests"]
        if row["segment_ref"] == segment_ref
        and row["base_request_sha256"] == base_request_sha256
    ]
    if len(matches) != 1:
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            "segment_ref/base request does not select exactly one hypothesis row"
        )
    row = matches[0]
    if row.get("status") != READY_STATUS:
        codes = ",".join(blocker["code"] for blocker in row["blockers"])
        raise HistoricalFurySourceBoundDynamicHypothesisV2Error(
            f"source-bound hypothesis is not wire-smoke ready: {codes}"
        )
    return (
        deepcopy(dict(row["derived_request_template"])),
        dynamic_target_semantics_config_from_wire_v3(row["dynamic_load_config"]),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def publish_historical_fury_source_bound_dynamic_hypothesis_v2(
    *,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    evidence_manifest_path: str | Path = DEFAULT_EVIDENCE_MANIFEST,
    source_bundle_path: str | Path = DEFAULT_SOURCE_BUNDLE,
    dynamic_v1_manifest_path: str | Path = DEFAULT_DYNAMIC_V1_MANIFEST,
) -> tuple[Path, Path, JSONMap]:
    artifact = build_historical_fury_source_bound_dynamic_hypothesis_v2(
        evidence_manifest_path=evidence_manifest_path,
        source_bundle_path=source_bundle_path,
        dynamic_v1_manifest_path=dynamic_v1_manifest_path,
    )
    payload = _canonical_bytes(artifact, newline=True)
    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stable = destination / "manifest.json"
    addressed = destination / (
        "historical_fury_source_bound_dynamic_hypothesis_v2."
        + artifact["content_address"]["sha256"]
        + ".manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return stable, addressed, artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_MANIFEST)
    parser.add_argument("--source-bundle", type=Path, default=DEFAULT_SOURCE_BUNDLE)
    parser.add_argument("--dynamic-v1", type=Path, default=DEFAULT_DYNAMIC_V1_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    args = parser.parse_args(argv)
    try:
        stable, addressed, artifact = (
            publish_historical_fury_source_bound_dynamic_hypothesis_v2(
                output_directory=args.output_dir,
                evidence_manifest_path=args.evidence,
                source_bundle_path=args.source_bundle,
                dynamic_v1_manifest_path=args.dynamic_v1,
            )
        )
    except (OSError, ValueError, HistoricalFurySourceBoundDynamicHypothesisV2Error) as error:
        print(f"BLOCKED: {error}")
        return 2
    print(
        json.dumps(
            {
                "status": artifact["status"],
                **artifact["summary"],
                "content_sha256": artifact["content_address"]["sha256"],
                "stable": str(stable),
                "addressed": str(addressed),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


__all__: Sequence[str] = (
    "BLOCKED_STATUS",
    "DEFAULT_EVIDENCE_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_OUTPUT_DIRECTORY",
    "HistoricalFurySourceBoundDynamicHypothesisV2Error",
    "IMPLEMENTATION_REVISION",
    "READY_SEGMENT_REF",
    "READY_STATUS",
    "SCHEMA",
    "build_historical_fury_source_bound_dynamic_hypothesis_v2",
    "load_historical_fury_source_bound_dynamic_hypothesis_v2",
    "publish_historical_fury_source_bound_dynamic_hypothesis_v2",
    "select_local_native_wire_smoke_pair_v2",
    "validate_historical_fury_source_bound_dynamic_hypothesis_v2",
)


if __name__ == "__main__":
    raise SystemExit(main())
