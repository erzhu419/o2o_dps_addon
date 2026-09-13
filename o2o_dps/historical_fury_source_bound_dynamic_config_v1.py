"""Bind source-bound prototype requests to exact waves, without guessing dynamics.

The source-bound prototype bundle contains exact historical builds but remains a
duration-mode development bundle.  This producer joins each request back to its
exact decision wave and records what is still required before a health-mode
``DynamicRolloutLoadV3`` can be constructed.  It deliberately does not promote
observed damage/death totals to initial health, does not treat the catalogue
``valid_from`` encounter as the decision wave, and does not copy candidate
Sunder/weapon-proc effects into the exogenous armor schedule.

V1 is a fail-closed preparation artifact: every row is BLOCKED and both the
derived request template and dynamic config are absent.  A later revision may
emit those values only as a content-bound pair from explicit prefix evidence.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_timeline_v2 as timeline_v2
from . import chronicle_external_teammate_response_hpc_v1 as response_hpc_v1
from . import historical_fury_decision_build_join_v1 as join_v1
from . import historical_fury_source_bound_prototype_bundle_v1 as source_v1
from . import hpc_dynamic_environment_v5 as dynamic_env_v5
from .sim_bridge_dynamic_v3 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicTargetSemanticsConfigV3,
    dynamic_target_semantics_config_from_wire_v3,
)


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_dynamic_config_preparation/v1"
IMPLEMENTATION_REVISION = "v1.0_exact_decision_wave_fail_closed_evidence"
KIND = "historical_fury_source_bound_dynamic_config_preparation"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_dynamic_config_preparation_content/v1"
)
STATUS = "BLOCKED"
EXPECTED_REQUEST_COUNT = 9
HEALTH_STAT_INDEX = 34

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_BUNDLE = source_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_DECISION_BUILD_JOIN = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_decision_build_join"
    / "v1"
    / "manifest.json"
)
DEFAULT_TEAM_TIMELINE = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_timeline"
    / "v2"
    / "utk_postfix_dev_20260903_noon"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_dynamic_config"
    / "v1"
)
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIRECTORY / "manifest.json"

_REQUIRED_EVIDENCE_BLOCKERS = frozenset(
    {
        "PREFIX_TARGET_HEALTH_EVIDENCE_MISSING",
        "PREFIX_EXOGENOUS_BASE_ARMOR_EVIDENCE_MISSING",
        "PREFIX_ATTACKABILITY_EVIDENCE_MISSING",
        "SOURCE_BOUND_TEAM_KILL_CLOCK_CALIBRATION_MISSING",
    }
)


class HistoricalFurySourceBoundDynamicConfigV1Error(RuntimeError):
    """The exact-wave preparation inputs or fail-closed artifact are invalid."""


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
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> JSONMap:
    result: JSONMap = {}
    for key, value in pairs:
        if key in result:
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> tuple[JSONMap, bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON token: {token}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"cannot load {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} must be an object"
        )
    return value, raw


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    payload = deepcopy(dict(core))
    payload.pop("content_address", None)
    return {
        **payload,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": (
                "canonical JSON excluding content_address; host locators forbidden"
            ),
            "sha256": _sha256_bytes(_canonical_bytes(payload)),
        },
    }


def _verify_content_address(
    value: Mapping[str, Any], label: str, *, expected_scope: str | None = None
) -> str:
    address = _mapping(value.get("content_address"), f"{label}.content_address")
    if address.get("algorithm") != "sha256":
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} content address algorithm differs"
        )
    if expected_scope is not None and address.get("scope") != expected_scope:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} content address scope differs"
        )
    observed = _text(address.get("sha256"), f"{label}.content_address.sha256")
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    if observed != _sha256_bytes(_canonical_bytes(core)):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} content address does not bind canonical content"
        )
    return observed


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
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} contains an absolute host locator"
        )


def _source_bundle_contract(bundle: Mapping[str, Any]) -> JSONMap:
    if (
        bundle.get("schema") != source_v1.SCHEMA
        or bundle.get("implementation_revision") != source_v1.IMPLEMENTATION_REVISION
        or bundle.get("kind") != source_v1.KIND
        or bundle.get("status") != "PREPARED"
        or bundle.get("execution_status") != "NOT_RUN"
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "source-bound bundle is not the prepared v1 duration-mode source"
        )
    bundle_sha = _verify_content_address(
        bundle,
        "source-bound bundle",
        expected_scope=(
            "canonical JSON excluding content_address; host locators forbidden"
        ),
    )
    requests = _array(bundle.get("requests"), "source-bound bundle.requests")
    if len(requests) != EXPECTED_REQUEST_COUNT:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"source-bound bundle must contain {EXPECTED_REQUEST_COUNT} requests"
        )
    seeds = _mapping(bundle.get("development_seeds"), "development_seeds")
    master_seeds = _array(seeds.get("master_seeds"), "development master_seeds")
    seed_list_sha = _text(
        seeds.get("seed_list_sha256"), "development seed_list_sha256"
    )
    if (
        _integer(seeds.get("count"), "development seed count", minimum=1)
        != len(master_seeds)
        or _sha256_bytes(_canonical_bytes(master_seeds)) != seed_list_sha
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "development seed-list binding differs"
        )
    seen_segments: set[str] = set()
    for index, raw in enumerate(requests):
        row = _mapping(raw, f"source request[{index}]")
        segment_ref = _text(row.get("segment_ref"), f"request[{index}].segment_ref")
        request_sha = _text(
            row.get("request_sha256"), f"request[{index}].request_sha256"
        )
        request = _mapping(
            _mapping(row.get("composition"), f"request[{index}].composition").get(
                "request"
            ),
            f"request[{index}].composition.request",
        )
        if _sha256_bytes(_canonical_bytes(request)) != request_sha:
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                f"source request[{index}] content digest differs"
            )
        if segment_ref in seen_segments:
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "source-bound segment identities are not unique"
            )
        seen_segments.add(segment_ref)
    return {
        "schema": bundle["schema"],
        "implementation_revision": bundle["implementation_revision"],
        "content_sha256": bundle_sha,
        "request_count": len(requests),
        "development_seed_list_sha256": seed_list_sha,
        "development_seed_count": len(master_seeds),
    }


def _join_contract(join: Mapping[str, Any], bundle: Mapping[str, Any]) -> JSONMap:
    if (
        join.get("schema") != join_v1.MANIFEST_SCHEMA
        or join.get("implementation_revision") != join_v1.IMPLEMENTATION_REVISION
        or join.get("status") != join_v1.STATUS
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "decision/build join implementation identity differs"
        )
    join_sha = _verify_content_address(
        join,
        "decision/build join",
        expected_scope="canonical JSON excluding content_address",
    )
    bundle_join = _mapping(
        _mapping(bundle.get("input_closure"), "bundle.input_closure").get(
            "decision_build_join"
        ),
        "bundle decision_build_join",
    )
    if (
        bundle_join.get("schema") != join_v1.MANIFEST_SCHEMA
        or bundle_join.get("implementation_revision")
        != join_v1.IMPLEMENTATION_REVISION
        or bundle_join.get("content_sha256") != join_sha
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "source bundle and decision/build join identities differ"
        )
    return {
        "schema": join["schema"],
        "implementation_revision": join["implementation_revision"],
        "content_sha256": join_sha,
        "mapping_partition_count": len(
            _array(join.get("mapping_partitions"), "mapping_partitions")
        ),
    }


def _timeline_contract(timeline: Mapping[str, Any]) -> JSONMap:
    if (
        timeline.get("schema") != timeline_v2.SCHEMA
        or timeline.get("implementation_revision") != timeline_v2.IMPLEMENTATION_REVISION
        or timeline.get("status") != timeline_v2.STATUS
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "Chronicle team timeline is not the current exact-prefix cohort"
        )
    timeline_sha = _verify_content_address(
        timeline,
        "Chronicle team timeline",
        expected_scope="canonical JSON excluding content_address",
    )
    summary = _mapping(timeline.get("summary"), "team timeline.summary")
    return {
        "schema": timeline["schema"],
        "implementation_revision": timeline["implementation_revision"],
        "status": timeline["status"],
        "content_sha256": timeline_sha,
        "wave_count": _integer(summary.get("wave_count"), "timeline wave_count"),
        "role": "AVAILABLE_DESCRIPTIVE_TIMELINE_NOT_DYNAMIC_PARAMETER_EVIDENCE",
    }


def _partition_bytes(
    *, join_path: Path, descriptor: Mapping[str, Any], label: str
) -> bytes:
    raw_path = _text(descriptor.get("path"), f"{label}.path")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label}.path is not a safe relative locator"
        )
    path = (join_path.parent / relative).resolve()
    try:
        compressed = path.read_bytes()
    except OSError as error:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"cannot read {label}: {error}"
        ) from error
    if (
        len(compressed)
        != _integer(descriptor.get("compressed_size_bytes"), f"{label}.compressed_size")
        or _sha256_bytes(compressed)
        != _text(
            descriptor.get("compressed_file_sha256"),
            f"{label}.compressed_file_sha256",
        )
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} compressed bytes differ"
        )
    try:
        logical = gzip.decompress(compressed)
    except (OSError, EOFError) as error:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"cannot decompress {label}: {error}"
        ) from error
    if (
        len(logical)
        != _integer(descriptor.get("logical_size_bytes"), f"{label}.logical_size")
        or _sha256_bytes(logical)
        != _text(
            descriptor.get("logical_content_sha256"),
            f"{label}.logical_content_sha256",
        )
        or logical.count(b"\n")
        != _integer(descriptor.get("record_count"), f"{label}.record_count")
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"{label} logical bytes differ"
        )
    return logical


def _identity_key(source: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        source.get(key)
        for key in (
            "instance_id",
            "encounter_id",
            "wave_id",
            "wave_ordinal",
            "episode_id",
            "player_guid",
            "server",
            "realm",
            "source_wave_content_sha256",
        )
    )


def _exact_wave_candidates(
    *,
    join: Mapping[str, Any],
    join_path: Path,
    requests: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[JSONMap]], list[JSONMap]]:
    by_segment = {str(row["segment_ref"]): [] for row in requests}
    instances = {
        str(_mapping(row["causal_source_identity"], "causal source")["identity"]["instance_id"])
        for row in requests
    }
    relevant_descriptors = [
        _mapping(raw, "mapping partition")
        for raw in _array(join.get("mapping_partitions"), "mapping_partitions")
        if _mapping(raw, "mapping partition").get("instance_id") in instances
    ]
    descriptor_instances = {str(row.get("instance_id")) for row in relevant_descriptors}
    missing = instances - descriptor_instances
    if missing:
        # Missing partitions become row-level NO_EXACT_DECISION_WAVE blockers.
        pass
    closure: list[JSONMap] = []
    for descriptor in sorted(relevant_descriptors, key=lambda row: str(row["instance_id"])):
        if descriptor.get("record_schema") != join_v1.MAPPING_SCHEMA:
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "mapping partition record schema differs"
            )
        logical = _partition_bytes(
            join_path=join_path,
            descriptor=descriptor,
            label=f"mapping partition {descriptor.get('instance_id')}",
        )
        closure.append(
            {
                key: deepcopy(descriptor.get(key))
                for key in (
                    "instance_id",
                    "record_schema",
                    "record_count",
                    "controllable_start_count",
                    "logical_size_bytes",
                    "logical_content_sha256",
                    "compressed_size_bytes",
                    "compressed_file_sha256",
                )
            }
        )
        for line_number, raw_line in enumerate(logical.splitlines(), 1):
            try:
                record = json.loads(
                    raw_line.decode("utf-8"), object_pairs_hook=_strict_pairs
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HistoricalFurySourceBoundDynamicConfigV1Error(
                    f"mapping record {line_number} is invalid: {error}"
                ) from error
            row = _mapping(record, f"mapping record {line_number}")
            if (
                row.get("schema") != join_v1.MAPPING_SCHEMA
                or row.get("implementation_revision") != join_v1.IMPLEMENTATION_REVISION
                or row.get("record_type")
                != "exact_fury_wave_decision_build_mapping"
            ):
                raise HistoricalFurySourceBoundDynamicConfigV1Error(
                    f"mapping record {line_number} identity differs"
                )
            source = _mapping(row.get("source"), "mapping source")
            selected: dict[str, list[JSONMap]] = {}
            for raw_binding in _array(row.get("decision_bindings"), "decision_bindings"):
                binding = _mapping(raw_binding, "decision binding")
                segment_ref = binding.get("segment_ref")
                if segment_ref not in by_segment:
                    continue
                if binding.get("join_status") != "JOINED_EXACT_CAUSAL_PREFIX":
                    raise HistoricalFurySourceBoundDynamicConfigV1Error(
                        "selected decision binding is not exact causal prefix"
                    )
                selected.setdefault(str(segment_ref), []).append(deepcopy(dict(binding)))
            for segment_ref, bindings in selected.items():
                by_segment[segment_ref].append(
                    {"source": deepcopy(dict(source)), "bindings": bindings}
                )
    return by_segment, closure


def _base_health_gate(request: Mapping[str, Any]) -> JSONMap:
    encounter = _mapping(request.get("encounter"), "request.encounter")
    targets = _array(encounter.get("targets"), "request.encounter.targets")
    rows = []
    for index, raw_target in enumerate(targets):
        target = _mapping(raw_target, f"request target[{index}]")
        stats = _array(target.get("stats"), f"request target[{index}].stats")
        value = stats[HEALTH_STAT_INDEX] if len(stats) > HEALTH_STAT_INDEX else None
        positive = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        )
        rows.append(
            {
                "target_index": index,
                "stats_index": HEALTH_STAT_INDEX,
                "value": value,
                "positive": positive,
            }
        )
    return {
        "encounter_useHealth_value": encounter.get("useHealth"),
        "health_mode_declared": encounter.get("useHealth") is True,
        "target_health_stats": rows,
        "all_target_health_stats_positive": bool(rows)
        and all(row["positive"] for row in rows),
    }


def _wave_binding(
    *, request_row: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> tuple[JSONMap | None, JSONMap, list[JSONMap]]:
    identity = _mapping(
        _mapping(request_row.get("causal_source_identity"), "causal source").get(
            "identity"
        ),
        "causal source identity",
    )
    grouped: dict[tuple[Any, ...], JSONMap] = {}
    for candidate in candidates:
        source = _mapping(candidate.get("source"), "wave candidate source")
        for key in ("instance_id", "player_guid", "server", "realm"):
            observed = str(source.get(key, ""))
            expected = str(identity.get(key, ""))
            if observed.casefold() != expected.casefold():
                raise HistoricalFurySourceBoundDynamicConfigV1Error(
                    f"segment mapping source {key} differs from exact build identity"
                )
        key = _identity_key(source)
        bucket = grouped.setdefault(
            key, {"source": deepcopy(dict(source)), "bindings": []}
        )
        bucket["bindings"].extend(deepcopy(candidate["bindings"]))
    blockers: list[JSONMap] = []
    if not grouped:
        blockers.append(
            {
                "code": "NO_EXACT_DECISION_WAVE_MAPPING",
                "detail": "no decision/build mapping contains this exact segment",
            }
        )
        return None, {"mapping_cardinality": 0, "decision_count": 0}, blockers
    if len(grouped) != 1:
        blockers.append(
            {
                "code": "MULTIPLE_INCOMPATIBLE_DECISION_WAVE_MAPPINGS",
                "detail": (
                    "the exact build segment occurs in multiple decision waves; "
                    "v1 will not merge or choose among them"
                ),
            }
        )
        return (
            None,
            {
                "mapping_cardinality": len(grouped),
                "decision_count": sum(len(row["bindings"]) for row in grouped.values()),
                "candidate_source_identities": [
                    deepcopy(row["source"])
                    for _, row in sorted(grouped.items(), key=lambda item: str(item[0]))
                ],
            },
            blockers,
        )
    selected = next(iter(grouped.values()))
    bindings = sorted(
        selected["bindings"], key=lambda row: tuple(row.get("order_key", ()))
    )
    if not bindings:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "exact wave candidate contains no selected decision"
        )
    source_identity = deepcopy(selected["source"])
    prefix = {
        "mapping_cardinality": 1,
        "decision_count": len(bindings),
        "first_decision": {
            key: deepcopy(bindings[0].get(key))
            for key in (
                "timestamp_ms",
                "event_index",
                "decision_ordinal",
                "order_key",
                "action_key",
            )
        },
        "last_bound_decision": {
            key: deepcopy(bindings[-1].get(key))
            for key in (
                "timestamp_ms",
                "event_index",
                "decision_ordinal",
                "order_key",
                "action_key",
            )
        },
        "build_selector": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_EACH_DECISION_ONLY",
        "future_info_backfill_allowed": False,
        "outcome_suffix_used_for_dynamic_parameters": False,
    }
    return source_identity, prefix, blockers


def _request_preparation_row(
    *,
    request_row: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    seed_list_sha256: str,
) -> JSONMap:
    base_request = _mapping(
        _mapping(request_row.get("composition"), "composition").get("request"),
        "base request",
    )
    source_identity, decision_prefix, blockers = _wave_binding(
        request_row=request_row, candidates=candidates
    )
    health_gate = _base_health_gate(base_request)
    if not health_gate["health_mode_declared"]:
        blockers.append(
            {
                "code": "BASE_REQUEST_HEALTH_MODE_NOT_DECLARED",
                "detail": (
                    "encounter.useHealth is not true; the duration-mode base request "
                    "must not be mutated independently of an evidenced config"
                ),
            }
        )
    if not health_gate["all_target_health_stats_positive"]:
        blockers.append(
            {
                "code": "BASE_REQUEST_TARGET_HEALTH_STAT_34_NOT_POSITIVE",
                "detail": (
                    "one or more target stats[34] values are absent/non-positive; "
                    "DynamicRolloutLoadV3 requires exact positive matching health"
                ),
            }
        )
    blockers.extend(
        [
            {
                "code": "PREFIX_TARGET_HEALTH_EVIDENCE_MISSING",
                "detail": (
                    "no prefix-available exact initial/max HP is bound; observed "
                    "damage or a later death is not promoted to initial health"
                ),
            },
            {
                "code": "PREFIX_EXOGENOUS_BASE_ARMOR_EVIDENCE_MISSING",
                "detail": (
                    "no prefix-available exact base/exogenous armor schedule is bound"
                ),
            },
            {
                "code": "PREFIX_ATTACKABILITY_EVIDENCE_MISSING",
                "detail": (
                    "no exact prefix target registry plus attackable/unattackable "
                    "interval schedule is materialized for this wave"
                ),
            },
            {
                "code": "SOURCE_BOUND_TEAM_KILL_CLOCK_CALIBRATION_MISSING",
                "detail": (
                    "no exact generated/historical source-bound pair supplies a "
                    "team kill-clock calibration for this wave"
                ),
            },
        ]
    )
    causal = _mapping(request_row.get("causal_source_identity"), "causal source")
    return {
        "segment_ref": _text(request_row.get("segment_ref"), "segment_ref"),
        "base_request_sha256": _text(
            request_row.get("request_sha256"), "base_request_sha256"
        ),
        "development_seed_list_sha256": seed_list_sha256,
        "status": "BLOCKED",
        "catalog_valid_from": {
            "role": "BUILD_PREFIX_BOUNDARY_NOT_DECISION_WAVE_SELECTOR",
            **deepcopy(dict(_mapping(causal.get("valid_from"), "catalog valid_from"))),
        },
        "source_identity": source_identity,
        "decision_prefix": decision_prefix,
        "base_request_dynamic_gate": health_gate,
        "evidence": {
            "target_health": {
                "status": "MISSING_PREFIX_VALUE",
                "value": None,
                "observed_background_damage_used_as_initial_health": False,
                "final_death_or_wave_total_used": False,
            },
            "exogenous_armor": {
                "status": "MISSING_PREFIX_BASE_AND_SCHEDULE",
                "base_armor": None,
                "effective_armor_events": None,
                "candidate_endogenous_sunder_encoded_as_environment": False,
                "candidate_weapon_armor_ignore_or_proc_encoded_as_environment": False,
                "contract": (
                    "only prefix-known external team/environment armor changes may "
                    "enter the exogenous schedule; candidate effects remain simulator-owned"
                ),
            },
            "attackability": {
                "status": "MISSING_PREFIX_TARGET_REGISTRY_AND_INTERVALS",
                "events": None,
                "future_mechanics_or_death_used": False,
            },
            "team_kill_clock": {
                "status": "MISSING_SOURCE_BOUND_DYNAMIC_ROLLOUT_CALIBRATION",
                "calibration_metric": None,
                "calibration_weight": 0,
                "count_table_response_model_treated_as_calibration": False,
            },
        },
        "blockers": blockers,
        "derived_request_template": None,
        "derived_request_template_sha256": None,
        "dynamic_load_config": None,
        "derived_pair_content_sha256": None,
    }


def _build_document(
    *,
    bundle: Mapping[str, Any],
    join: Mapping[str, Any],
    join_path: Path,
    timeline: Mapping[str, Any],
) -> JSONMap:
    bundle_contract = _source_bundle_contract(bundle)
    join_contract = _join_contract(join, bundle)
    timeline_contract = _timeline_contract(timeline)
    requests = [
        _mapping(raw, "source request")
        for raw in _array(bundle.get("requests"), "bundle.requests")
    ]
    candidates, partition_closure = _exact_wave_candidates(
        join=join, join_path=join_path, requests=requests
    )
    rows = [
        _request_preparation_row(
            request_row=row,
            candidates=candidates[str(row["segment_ref"])],
            seed_list_sha256=bundle_contract["development_seed_list_sha256"],
        )
        for row in sorted(requests, key=lambda item: str(item["segment_ref"]))
    ]
    exact_wave_count = sum(row["source_identity"] is not None for row in rows)
    blocker_counts: dict[str, int] = {}
    for row in rows:
        for blocker in row["blockers"]:
            code = str(blocker["code"])
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
    core = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "status": STATUS,
        "execution_status": "NOT_RUN",
        "scientific_runs_started": False,
        "simulator_run_count": 0,
        "hpc_job_count": 0,
        "network_request_count": 0,
        "comparison_authorized": False,
        "training_authorized": False,
        "deployment_authorized": False,
        "superiority_claim_authorized": False,
        "input_closure": {
            "source_bound_prototype_bundle": bundle_contract,
            "decision_build_join": join_contract,
            "relevant_mapping_partitions": partition_closure,
            "chronicle_external_team_timeline": timeline_contract,
            "team_background_contract": {
                "schema": background_v2.SCHEMA,
                "implementation_revision": background_v2.IMPLEMENTATION_REVISION,
                "initial_target_health_source": "REQUIRED_EXTERNAL_HYPOTHESIS",
                "materialized_source_bound_draw_count": 0,
            },
            "teammate_response_contract": {
                "schema": response_hpc_v1.SCHEMA,
                "implementation_revision": response_hpc_v1.REVISION,
                "dynamic_rollout_team_kill_clock_metric_materialized": False,
                "dynamic_kill_clock_weight": 0,
            },
        },
        "runtime_capability": {
            "schema": dynamic_env_v5.CONTRACT_SCHEMA,
            "bridge_command": "load_dynamic_v3",
            "dynamic_config_schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            "role": "CAPABILITY_CONTRACT_ONLY_NOT_HISTORICAL_PARAMETER_EVIDENCE",
            "dynamic_probe_scenario_values_consumed": False,
            "parallel_environment_created": False,
        },
        "binding_contract": {
            "wave_selector": "DECISION_BUILD_MAPPING_SOURCE_IDENTITY",
            "catalog_valid_from_encounter_is_wave_selector": False,
            "exact_dimensions": [
                "instance_id",
                "encounter_id",
                "wave_id",
                "player_guid",
                "segment_ref",
                "base_request_sha256",
            ],
            "dynamic_parameter_evidence": "AVAILABLE_AT_OR_BEFORE_DECISION_ONLY",
            "future_outcome_suffix_allowed": False,
            "derived_request_and_config_must_be_published_as_one_pair": True,
            "derived_request_requires_useHealth_true": True,
            "derived_target_stats_34_must_be_positive_and_equal_config_health": True,
            "ready_template_seed_independent": True,
            "runner_seed_mutation_allowlist": [
                "derived_request_template.simOptions.randomSeed",
                "DynamicRolloutLoadV3.seed",
            ],
            "seed_source": "source_bound_prototype_bundle.development_seeds.master_seeds",
        },
        "summary": {
            "request_count": len(rows),
            "exact_unique_decision_wave_count": exact_wave_count,
            "ready_request_count": 0,
            "blocked_request_count": len(rows),
            "derived_request_template_count": 0,
            "dynamic_load_config_count": 0,
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "requests": rows,
    }
    artifact = _content_addressed(core)
    _assert_path_free(artifact)
    return artifact


def build_historical_fury_source_bound_dynamic_config_v1(
    *,
    source_bundle_path: str | Path = DEFAULT_SOURCE_BUNDLE,
    decision_build_join_path: str | Path = DEFAULT_DECISION_BUILD_JOIN,
    team_timeline_path: str | Path = DEFAULT_TEAM_TIMELINE,
) -> JSONMap:
    """Build nine exact-wave BLOCKED rows; never run the simulator or HPC."""

    bundle_path = Path(source_bundle_path).expanduser().resolve()
    join_path = Path(decision_build_join_path).expanduser().resolve()
    timeline_path = Path(team_timeline_path).expanduser().resolve()
    bundle, _ = _load_json(bundle_path, "source-bound bundle")
    join, _ = _load_json(join_path, "decision/build join")
    timeline, _ = _load_json(timeline_path, "Chronicle team timeline")
    artifact = _build_document(
        bundle=bundle, join=join, join_path=join_path, timeline=timeline
    )
    return validate_historical_fury_source_bound_dynamic_config_v1(artifact)


def validate_historical_fury_source_bound_dynamic_config_v1(
    value: Mapping[str, Any],
) -> JSONMap:
    """Validate the v1 zero-READY contract without reopening large inputs."""

    raw = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != KIND
        or raw.get("status") != STATUS
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "dynamic preparation implementation identity differs"
        )
    address = _mapping(raw.get("content_address"), "content_address")
    core = deepcopy(raw)
    core.pop("content_address", None)
    expected_address = _content_addressed(core)["content_address"]
    if dict(address) != expected_address:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "dynamic preparation content address differs"
        )
    _assert_path_free(raw)
    if (
        raw.get("execution_status") != "NOT_RUN"
        or raw.get("scientific_runs_started") is not False
        or raw.get("simulator_run_count") != 0
        or raw.get("hpc_job_count") != 0
        or raw.get("network_request_count") != 0
        or raw.get("comparison_authorized") is not False
        or raw.get("training_authorized") is not False
        or raw.get("deployment_authorized") is not False
        or raw.get("superiority_claim_authorized") is not False
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "dynamic preparation exceeded the no-run boundary"
        )
    rows = _array(raw.get("requests"), "requests")
    summary = _mapping(raw.get("summary"), "summary")
    if (
        len(rows) != EXPECTED_REQUEST_COUNT
        or summary.get("request_count") != len(rows)
        or summary.get("ready_request_count") != 0
        or summary.get("blocked_request_count") != len(rows)
        or summary.get("derived_request_template_count") != 0
        or summary.get("dynamic_load_config_count") != 0
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "v1 request/readiness accounting differs"
        )
    seen: set[tuple[str, str]] = set()
    exact_wave_count = 0
    blocker_counts: dict[str, int] = {}
    seed_list_sha = _text(
        _mapping(
            _mapping(raw.get("input_closure"), "input_closure").get(
                "source_bound_prototype_bundle"
            ),
            "source bundle closure",
        ).get("development_seed_list_sha256"),
        "development_seed_list_sha256",
    )
    for index, item in enumerate(rows):
        row = _mapping(item, f"requests[{index}]")
        key = (
            _text(row.get("segment_ref"), f"requests[{index}].segment_ref"),
            _text(
                row.get("base_request_sha256"),
                f"requests[{index}].base_request_sha256",
            ),
        )
        if key in seen:
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "dynamic preparation request identity is not unique"
            )
        seen.add(key)
        if (
            row.get("status") != "BLOCKED"
            or row.get("development_seed_list_sha256") != seed_list_sha
            or row.get("derived_request_template") is not None
            or row.get("derived_request_template_sha256") is not None
            or row.get("dynamic_load_config") is not None
            or row.get("derived_pair_content_sha256") is not None
        ):
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "v1 row must remain BLOCKED with a null derived pair"
            )
        valid_from = _mapping(row.get("catalog_valid_from"), "catalog_valid_from")
        if valid_from.get("role") != "BUILD_PREFIX_BOUNDARY_NOT_DECISION_WAVE_SELECTOR":
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "catalog valid_from role differs"
            )
        prefix = _mapping(row.get("decision_prefix"), "decision_prefix")
        cardinality = _integer(
            prefix.get("mapping_cardinality"), "mapping_cardinality"
        )
        source = row.get("source_identity")
        if cardinality == 1:
            source = _mapping(source, "source_identity")
            _text(source.get("instance_id"), "source instance_id")
            _text(source.get("encounter_id"), "source encounter_id")
            _text(source.get("wave_id"), "source wave_id")
            _integer(prefix.get("decision_count"), "decision_count", minimum=1)
            exact_wave_count += 1
        elif source is not None:
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "ambiguous/missing wave row must not choose a source identity"
            )
        blockers = _array(row.get("blockers"), "blockers")
        codes = {
            _text(_mapping(blocker, "blocker").get("code"), "blocker.code")
            for blocker in blockers
        }
        if not _REQUIRED_EVIDENCE_BLOCKERS.issubset(codes):
            raise HistoricalFurySourceBoundDynamicConfigV1Error(
                "row omits a required dynamic evidence blocker"
            )
        for code in codes:
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
    if (
        summary.get("exact_unique_decision_wave_count") != exact_wave_count
        or summary.get("blocker_counts") != dict(sorted(blocker_counts.items()))
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "exact-wave/blocker summary differs from request rows"
        )
    contract = _mapping(raw.get("binding_contract"), "binding_contract")
    capability = _mapping(raw.get("runtime_capability"), "runtime_capability")
    if (
        contract.get("catalog_valid_from_encounter_is_wave_selector") is not False
        or contract.get("future_outcome_suffix_allowed") is not False
        or contract.get("derived_request_and_config_must_be_published_as_one_pair")
        is not True
        or contract.get("derived_target_stats_34_must_be_positive_and_equal_config_health")
        is not True
        or capability.get("schema") != dynamic_env_v5.CONTRACT_SCHEMA
        or capability.get("bridge_command") != "load_dynamic_v3"
        or capability.get("dynamic_probe_scenario_values_consumed") is not False
        or capability.get("parallel_environment_created") is not False
    ):
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "binding/runtime capability boundary differs"
        )
    return raw


def load_historical_fury_source_bound_dynamic_config_v1(
    path: str | Path = DEFAULT_OUTPUT,
) -> JSONMap:
    """Load and validate a published preparation manifest."""

    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        resolved = resolved / "manifest.json"
    value, _ = _load_json(resolved, "dynamic preparation manifest")
    return validate_historical_fury_source_bound_dynamic_config_v1(value)


def select_ready_dynamic_config_v1(
    artifact: Mapping[str, Any], *, segment_ref: str, request_sha256: str
) -> tuple[JSONMap, DynamicTargetSemanticsConfigV3]:
    """Select one declared READY pair; v1 intentionally has none."""

    checked = validate_historical_fury_source_bound_dynamic_config_v1(artifact)
    matches = [
        row
        for row in checked["requests"]
        if row["segment_ref"] == segment_ref
        and row["base_request_sha256"] == request_sha256
    ]
    if len(matches) != 1:
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            "segment_ref/base request does not select exactly one preparation row"
        )
    row = matches[0]
    if row.get("status") != "READY":
        codes = ",".join(blocker["code"] for blocker in row["blockers"])
        raise HistoricalFurySourceBoundDynamicConfigV1Error(
            f"source-bound dynamic config is BLOCKED: {codes}"
        )
    # This branch is unreachable under the v1 validator.  Keeping the typed
    # return contract here prevents callers from substituting an unbound config.
    template = deepcopy(
        dict(_mapping(row.get("derived_request_template"), "derived request template"))
    )
    config = dynamic_target_semantics_config_from_wire_v3(
        _mapping(row.get("dynamic_load_config"), "dynamic load config")
    )
    return template, config


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


def publish_historical_fury_source_bound_dynamic_config_v1(
    *,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    source_bundle_path: str | Path = DEFAULT_SOURCE_BUNDLE,
    decision_build_join_path: str | Path = DEFAULT_DECISION_BUILD_JOIN,
    team_timeline_path: str | Path = DEFAULT_TEAM_TIMELINE,
) -> tuple[Path, Path, JSONMap]:
    """Publish stable and content-addressed manifests, without execution."""

    artifact = build_historical_fury_source_bound_dynamic_config_v1(
        source_bundle_path=source_bundle_path,
        decision_build_join_path=decision_build_join_path,
        team_timeline_path=team_timeline_path,
    )
    payload = _canonical_bytes(artifact, newline=True)
    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stable = destination / "manifest.json"
    address = artifact["content_address"]["sha256"]
    addressed = destination / (
        f"historical_fury_source_bound_dynamic_config_v1.{address}.manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return stable, addressed, artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, default=DEFAULT_SOURCE_BUNDLE)
    parser.add_argument(
        "--decision-build-join", type=Path, default=DEFAULT_DECISION_BUILD_JOIN
    )
    parser.add_argument("--team-timeline", type=Path, default=DEFAULT_TEAM_TIMELINE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    args = parser.parse_args(argv)
    try:
        stable, addressed, artifact = (
            publish_historical_fury_source_bound_dynamic_config_v1(
                output_directory=args.output_dir,
                source_bundle_path=args.source_bundle,
                decision_build_join_path=args.decision_build_join,
                team_timeline_path=args.team_timeline,
            )
        )
    except (OSError, ValueError, HistoricalFurySourceBoundDynamicConfigV1Error) as error:
        print(f"BLOCKED: {error}")
        return 2
    print(
        json.dumps(
            {
                "status": artifact["status"],
                "request_count": artifact["summary"]["request_count"],
                "ready_request_count": artifact["summary"]["ready_request_count"],
                "blocked_request_count": artifact["summary"]["blocked_request_count"],
                "exact_unique_decision_wave_count": artifact["summary"][
                    "exact_unique_decision_wave_count"
                ],
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
    "CONTENT_ADDRESS_SCHEMA",
    "DEFAULT_OUTPUT",
    "DEFAULT_OUTPUT_DIRECTORY",
    "HistoricalFurySourceBoundDynamicConfigV1Error",
    "IMPLEMENTATION_REVISION",
    "SCHEMA",
    "build_historical_fury_source_bound_dynamic_config_v1",
    "load_historical_fury_source_bound_dynamic_config_v1",
    "publish_historical_fury_source_bound_dynamic_config_v1",
    "select_ready_dynamic_config_v1",
    "validate_historical_fury_source_bound_dynamic_config_v1",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
