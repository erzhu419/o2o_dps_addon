"""Run one source-bound development-hypothesis wire smoke on native v11.

The v2 hypothesis compiler is the only owner of the derived request and
dynamic-v3 configuration.  This module selects its sole wire-smoke-ready row,
binds one prototype and one seed already declared by the source bundle, and
then delegates execution to the v1 prepared local-native tail.  The complete
v1 receipt is retained; no policy-value, comparison, training, HPC, deployment,
or superiority authority is introduced here.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import historical_behavior_clone_full_rollout_v2 as full_rollout_v2
from . import historical_fury_source_bound_dynamic_hypothesis_v2 as hypothesis_v2
from . import historical_fury_source_bound_prototype_bundle_v1 as bundle_v1
from . import historical_fury_source_bound_prototype_smoke_runner_v1 as smoke_v1
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    FuryDynamicTargetSemanticsV5Error,
)
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_prototype_wire_smoke_receipt/v2"
IMPLEMENTATION_REVISION = "v2.0_hypothesis_ready_reused_v1_native_tail"
KIND = "historical_fury_source_bound_prototype_wire_smoke_receipt"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_prototype_wire_smoke_receipt_content/v2"
)
COMPLETE_STATUS = (
    "COMPLETE_LOCAL_NATIVE_DEVELOPMENT_HYPOTHESIS_WIRE_SMOKE_NONVOTING"
)
INCOMPLETE_STATUS = (
    "INCOMPLETE_LOCAL_NATIVE_DEVELOPMENT_HYPOTHESIS_WIRE_SMOKE_NONVOTING"
)
EXPECTED_HORIZON_MS = 20_001

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_MANIFEST = smoke_v1.DEFAULT_BUNDLE_MANIFEST
DEFAULT_MODEL_MANIFEST = smoke_v1.DEFAULT_MODEL_MANIFEST
DEFAULT_DYNAMIC_HYPOTHESIS = hypothesis_v2.DEFAULT_OUTPUT
DEFAULT_WINDOWS_V11_BRIDGE = smoke_v1.DEFAULT_WINDOWS_V11_BRIDGE
DEFAULT_SIMULATOR_ROOT = smoke_v1.DEFAULT_SIMULATOR_ROOT
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_prototype_wire_smoke"
    / "v2"
    / "receipt.json"
)

_FALSE_AUTHORITY_FIELDS = (
    "value_ready",
    "policy_value_authorized",
    "comparison_authorized",
    "training_authorized",
    "hpc_authorized",
    "deployment_authorized",
    "superiority_claim_authorized",
)
_HYPOTHESIS_FIELDS = {
    "execution_window",
    "target_registry",
    "target_health",
    "exogenous_armor",
    "attackability",
    "team_kill_clock",
}


class HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(RuntimeError):
    """The v2 hypothesis selection or outer wire receipt does not close."""


@dataclass(frozen=True)
class PreparedSourceBoundPrototypeWireSmokeV2:
    hypothesis_manifest: JSONMap
    hypothesis_manifest_file_sha256: str
    hypothesis_manifest_file_size_bytes: int
    hypothesis_row: JSONMap
    v1_execution_projection: JSONMap
    prepared_v1: smoke_v1.PreparedSourceBoundPrototypeSmokeV1


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
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} must be a finite number"
        )
    result = float(value)
    if not math.isfinite(result):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} must be a finite number"
        )
    return result


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _resolve_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"{label} does not exist: {path}"
        )
    return path


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    payload = deepcopy(dict(core))
    payload.pop("content_address", None)
    return {
        **payload,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": _sha256_json(payload),
        },
    }


def _claim_boundary() -> JSONMap:
    return {
        "development_hypothesis_wire_smoke_only": True,
        "inner_v1_receipt_retained": True,
        "horizon_target_death_required": False,
        "observation_leak_present": True,
        "value_ready": False,
        "policy_value_authorized": False,
        "comparison_authorized": False,
        "training_authorized": False,
        "hpc_authorized": False,
        "deployment_authorized": False,
        "superiority_claim_authorized": False,
    }


def _select_unique_ready_row(
    artifact: Mapping[str, Any],
    *,
    segment_ref: str,
    base_request_sha256: str,
    prototype_id: str,
) -> JSONMap:
    if (
        artifact.get("schema") != hypothesis_v2.SCHEMA
        or artifact.get("execution_status") != "NOT_RUN"
        or artifact.get("local_native_wire_smoke_authorized") is not True
        or any(artifact.get(field) is not False for field in _FALSE_AUTHORITY_FIELDS)
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "dynamic hypothesis manifest exceeds its local wire-smoke boundary"
        )
    rows = [
        _mapping(raw, "hypothesis row")
        for raw in _array(artifact.get("requests"), "hypothesis requests")
        if isinstance(raw, Mapping)
        and raw.get("status") == hypothesis_v2.READY_STATUS
    ]
    if len(rows) != 1:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "dynamic hypothesis manifest must contain exactly one wire-smoke-ready row"
        )
    row = rows[0]
    if (
        row.get("segment_ref") != segment_ref
        or row.get("base_request_sha256") != base_request_sha256
        or row.get("local_native_wire_smoke_authorized") is not True
        or row.get("blockers") != []
        or any(row.get(field) is not False for field in _FALSE_AUTHORITY_FIELDS)
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "selected hypothesis row is not the requested non-valuing READY row"
        )
    hypotheses = _mapping(row.get("hypotheses"), "row hypotheses")
    if set(hypotheses) != _HYPOTHESIS_FIELDS:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "selected row does not declare all six environment hypotheses"
        )
    leak = _mapping(row.get("observation_leak"), "observation leak")
    if (
        leak.get("present_in_health_hypothesis") is not True
        or leak.get("present_in_materialized_pair") is not True
        or leak.get("blocks_policy_value") is not True
        or leak.get("allowed_role")
        != "LOCAL_NATIVE_WIRE_COMPATIBILITY_SMOKE_ONLY"
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "READY hypothesis row does not retain its value-blocking observation leak"
        )
    prototype = _mapping(row.get("prototype_contract"), "prototype contract")
    bindings = [
        _mapping(raw, "prototype binding")
        for raw in _array(prototype.get("bindings"), "prototype bindings")
        if isinstance(raw, Mapping) and raw.get("prototype_id") == prototype_id
    ]
    pair = _mapping(row.get("pair_binding"), "pair binding")
    if (
        len(bindings) != 1
        or prototype.get("policy_value_authorized") is not False
        or prototype.get("comparison_authorized") is not False
        or prototype_id not in _array(pair.get("prototype_ids"), "pair prototype ids")
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "prototype is not bound to the selected non-valuing hypothesis pair"
        )
    return deepcopy(dict(row))


def _validate_source_bundle_closure(
    artifact: Mapping[str, Any], bundle: Mapping[str, Any]
) -> str:
    seeds = _mapping(bundle.get("development_seeds"), "development seeds")
    seed_list_sha = _text(
        seeds.get("seed_list_sha256"), "development seed-list SHA-256"
    )
    expected = {
        "schema": bundle.get("schema"),
        "implementation_revision": bundle.get("implementation_revision"),
        "content_sha256": _mapping(
            bundle.get("content_address"), "bundle content address"
        ).get("sha256"),
        "request_count": len(_array(bundle.get("requests"), "bundle requests")),
        "development_seed_list_sha256": seed_list_sha,
    }
    observed = _mapping(
        _mapping(artifact.get("input_closure"), "hypothesis input closure").get(
            "source_bound_prototype_bundle_v1"
        ),
        "hypothesis source-bundle closure",
    )
    if dict(observed) != expected:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "dynamic hypothesis manifest is bound to a different source bundle"
        )
    return seed_list_sha


def _v1_projection(
    *,
    artifact: Mapping[str, Any],
    row: Mapping[str, Any],
    seed_list_sha256: str,
) -> tuple[JSONMap, JSONMap]:
    pair = _mapping(row.get("pair_binding"), "pair binding")
    pair_sha = _text(pair.get("content_sha256"), "pair content SHA-256")
    projected_row = {
        "segment_ref": row.get("segment_ref"),
        "base_request_sha256": row.get("base_request_sha256"),
        "status": "READY",
        "development_seed_list_sha256": seed_list_sha256,
        "derived_request_template": deepcopy(row.get("derived_request_template")),
        "derived_request_template_sha256": row.get(
            "derived_request_template_sha256"
        ),
        "dynamic_load_config": deepcopy(row.get("dynamic_load_config")),
        "derived_pair_content_sha256": pair_sha,
    }
    projection = {
        "schema": "historical_fury_source_bound_v1_execution_projection/v2",
        "role": "IN_MEMORY_ADAPTER_TO_REUSE_V1_PREPARED_NATIVE_EXECUTION_TAIL",
        "source_v2_manifest_content_sha256": _mapping(
            artifact.get("content_address"), "hypothesis content address"
        ).get("sha256"),
        "source_v2_row_status": row.get("status"),
        "projected_v1_row_status": "READY",
        "derived_pair_content_sha256": pair_sha,
        "on_disk_v1_artifact_created": False,
        "immutable_v1_predecessor_mutated": False,
        "comparison_authorized": False,
        "training_authorized": False,
        "hpc_authorized": False,
        "deployment_authorized": False,
    }
    return projected_row, projection


def prepare_source_bound_prototype_wire_smoke_v2(
    *,
    bundle_path: str | Path,
    model_manifest_path: str | Path,
    dynamic_hypothesis_path: str | Path,
    segment_ref: str,
    prototype_id: str,
    seed: int,
) -> PreparedSourceBoundPrototypeWireSmokeV2:
    """Bind the sole READY v2 hypothesis to one declared model and seed."""

    segment_ref = _text(segment_ref, "segment_ref")
    prototype_id = _text(prototype_id, "prototype_id")
    bundle_source = _resolve_file(bundle_path, "source-bound bundle")
    model_manifest = _resolve_file(model_manifest_path, "model manifest")
    hypothesis_source = _resolve_file(
        dynamic_hypothesis_path, "dynamic hypothesis manifest"
    )
    try:
        bundle, bundle_bytes = smoke_v1._strict_json_file(
            bundle_source, "source-bound bundle"
        )
        checked_bundle = (
            bundle_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
                bundle, verify_source_bytes=False
            )
        )
        smoke_v1._validate_bundle_boundaries(checked_bundle)
        request_row, binding = smoke_v1._select_request_and_binding(
            checked_bundle,
            segment_ref=segment_ref,
            prototype_id=prototype_id,
        )
        selected_seed = smoke_v1._validate_declared_seed(checked_bundle, seed)
    except (
        bundle_v1.HistoricalFurySourceBoundPrototypeBundleV1Error,
        smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error,
    ) as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(str(error)) from error

    base_request = deepcopy(
        dict(
            _mapping(
                _mapping(request_row.get("composition"), "request composition").get(
                    "request"
                ),
                "base RaidSimRequest",
            )
        )
    )
    base_request_sha = _sha256_json(base_request)
    if (
        base_request_sha != request_row.get("request_sha256")
        or _mapping(base_request.get("encounter"), "base encounter").get(
            "useHealth"
        )
        is True
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "source bundle request is not the declared duration-mode base request"
        )

    try:
        hypothesis = (
            hypothesis_v2.load_historical_fury_source_bound_dynamic_hypothesis_v2(
                hypothesis_source
            )
        )
    except hypothesis_v2.HistoricalFurySourceBoundDynamicHypothesisV2Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"dynamic hypothesis manifest is invalid: {error}"
        ) from error
    seed_list_sha = _validate_source_bundle_closure(hypothesis, checked_bundle)
    row = _select_unique_ready_row(
        hypothesis,
        segment_ref=segment_ref,
        base_request_sha256=base_request_sha,
        prototype_id=prototype_id,
    )

    source_binding = next(
        raw
        for raw in row["prototype_contract"]["bindings"]
        if raw["prototype_id"] == prototype_id
    )
    for field in (
        "prototype_id",
        "policy_id",
        "model_schema",
        "model_implementation_revision",
        "model_content_sha256",
        "model_file_sha256",
        "model_file_size_bytes",
        "segment_weighted_decision_support",
    ):
        if source_binding.get(field) != binding.get(field):
            raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
                f"hypothesis prototype binding differs from source bundle at {field}"
            )

    try:
        request_template, dynamic_config = (
            hypothesis_v2.select_local_native_wire_smoke_pair_v2(
                hypothesis,
                segment_ref=segment_ref,
                base_request_sha256=base_request_sha,
            )
        )
    except hypothesis_v2.HistoricalFurySourceBoundDynamicHypothesisV2Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"dynamic hypothesis pair cannot execute: {error}"
        ) from error
    if not isinstance(dynamic_config, DynamicTargetSemanticsConfigV3):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 selector returned the wrong dynamic config type"
        )
    template_sha = _sha256_json(request_template)
    pair = _mapping(row.get("pair_binding"), "pair binding")
    pair_core = deepcopy(dict(pair))
    pair_sha = pair_core.pop("content_sha256", None)
    if (
        request_template != row.get("derived_request_template")
        or template_sha != row.get("derived_request_template_sha256")
        or dynamic_config.to_wire() != row.get("dynamic_load_config")
        or pair_core.get("derived_request_template_sha256") != template_sha
        or pair_core.get("dynamic_load_config_content_sha256")
        != dynamic_config.content_sha256
        or pair_sha != _sha256_json(pair_core)
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "selected request/config differs from the content-bound v2 pair"
        )

    try:
        request = smoke_v1._request_with_declared_seed(
            request_template, selected_seed
        )
        full_rollout_v2.validate_executable_fury_request_v2(request)
        dynamic_load = DynamicRolloutLoadV3.bind(
            request, selected_seed, dynamic_config
        )
    except (
        TypeError,
        ValueError,
        FuryDynamicTargetSemanticsV5Error,
        full_rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
        smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error,
    ) as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"seeded v2 request/config is not executable as declared: {error}"
        ) from error

    projected_row, projection = _v1_projection(
        artifact=hypothesis, row=row, seed_list_sha256=seed_list_sha
    )
    hypothesis_content_sha = _text(
        _mapping(hypothesis.get("content_address"), "hypothesis content address").get(
            "sha256"
        ),
        "hypothesis content SHA-256",
    )
    execution_pair = smoke_v1._execution_pair_binding(
        dynamic_preparation_content_sha256=hypothesis_content_sha,
        segment_ref=segment_ref,
        base_request_sha256=base_request_sha,
        seed_list_sha256=seed_list_sha,
        template_sha256=template_sha,
        template_pair_sha256=_text(pair_sha, "pair content SHA-256"),
        seed=selected_seed,
        request=request,
        dynamic_load=dynamic_load,
    )
    try:
        (
            model,
            exact_model_receipt,
            model_file_sha,
            model_file_size,
            model_manifest_file_sha,
            model_manifest_content_sha,
        ) = smoke_v1._load_bound_model(
            bundle=checked_bundle,
            binding=binding,
            model_manifest_path=model_manifest,
        )
    except smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(str(error)) from error

    hypothesis_file_sha = hashlib.sha256(hypothesis_source.read_bytes()).hexdigest()
    prepared_v1 = smoke_v1.PreparedSourceBoundPrototypeSmokeV1(
        bundle=deepcopy(checked_bundle),
        bundle_file_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
        bundle_file_size_bytes=len(bundle_bytes),
        request_row=deepcopy(request_row),
        base_raid_sim_request=base_request,
        raid_sim_request=deepcopy(request),
        dynamic_preparation=deepcopy(hypothesis),
        dynamic_preparation_file_sha256=hypothesis_file_sha,
        dynamic_preparation_file_size_bytes=hypothesis_source.stat().st_size,
        dynamic_preparation_row=projected_row,
        derived_request_template_sha256=template_sha,
        derived_pair_template_sha256=_text(pair_sha, "pair content SHA-256"),
        execution_pair_binding=execution_pair,
        model=model,
        exact_model_receipt=exact_model_receipt,
        model_file_sha256=model_file_sha,
        model_file_size_bytes=model_file_size,
        model_manifest_file_sha256=model_manifest_file_sha,
        model_manifest_content_sha256=model_manifest_content_sha,
        seed=selected_seed,
        dynamic_config=dynamic_config,
        dynamic_load=dynamic_load,
    )
    return PreparedSourceBoundPrototypeWireSmokeV2(
        hypothesis_manifest=deepcopy(hypothesis),
        hypothesis_manifest_file_sha256=hypothesis_file_sha,
        hypothesis_manifest_file_size_bytes=hypothesis_source.stat().st_size,
        hypothesis_row=row,
        v1_execution_projection=projection,
        prepared_v1=prepared_v1,
    )


def _wire_smoke_closure(
    prepared: PreparedSourceBoundPrototypeWireSmokeV2,
    inner: Mapping[str, Any],
) -> JSONMap:
    rollout = _mapping(inner.get("rollout"), "inner rollout")
    bridge = _mapping(
        rollout.get("bridge_runtime_evidence"), "bridge runtime evidence"
    )
    inner_scope = _mapping(inner.get("execution_scope"), "inner execution scope")
    lifecycle = _mapping(
        inner.get("damage_lifecycle_closure"), "inner damage lifecycle closure"
    )
    completion = _mapping(
        rollout.get("configured_completion"), "configured completion"
    )
    request = prepared.prepared_v1.raid_sim_request
    encounter = _mapping(request.get("encounter"), "derived encounter")
    execution = _mapping(
        _mapping(prepared.hypothesis_row.get("hypotheses"), "hypotheses").get(
            "execution_window"
        ),
        "execution-window hypothesis",
    )
    request_duration_ms = _finite_number(
        encounter.get("duration"), "derived encounter duration"
    ) * 1000.0
    horizon_configured = (
        request_duration_ms == float(EXPECTED_HORIZON_MS)
        and execution.get("selected_horizon_ms") == EXPECTED_HORIZON_MS
        and prepared.prepared_v1.dynamic_config.idle_advance_horizon_ms
        == EXPECTED_HORIZON_MS
    )
    terminal_reason = completion.get("terminal_reason")
    completion_closed = (
        completion.get("criterion_met") is True
        and rollout.get("scenario_complete") is True
        and terminal_reason in {"ALL_TARGETS_DEAD", "SCENARIO_HORIZON_REACHED"}
    )
    native = (
        inner_scope.get("local_windows_native") is True
        and inner_scope.get("hpc_dispatch_performed") is False
        and bridge.get("runtime_kind") == "NATIVE_SUBPROCESS_BRIDGE"
        and bridge.get("native_subprocess_bridge") is True
        and bridge.get("python_simulated_bridge") is False
        and bridge.get("load_method_invoked") == "load_dynamic_v3"
    )
    runtime_receipts_closed = lifecycle.get("runtime_receipt_status") == "COMPLETE_BOUND"
    damage_closed = lifecycle.get("damage_accounting_closed") is True
    complete = (
        native
        and runtime_receipts_closed
        and horizon_configured
        and completion_closed
        and damage_closed
    )
    return {
        "native_v11_dynamic_v3_bridge": native,
        "runtime_receipt_status": lifecycle.get("runtime_receipt_status"),
        "runtime_receipts_closed": runtime_receipts_closed,
        "expected_horizon_ms": EXPECTED_HORIZON_MS,
        "request_duration_ms": request_duration_ms,
        "dynamic_idle_advance_horizon_ms": (
            prepared.prepared_v1.dynamic_config.idle_advance_horizon_ms
        ),
        "hypothesis_selected_horizon_ms": execution.get("selected_horizon_ms"),
        "horizon_configuration_closed": horizon_configured,
        "configured_completion_criterion_met": completion.get("criterion_met"),
        "scenario_complete": rollout.get("scenario_complete"),
        "terminal_reason": terminal_reason,
        "horizon_reached_without_all_targets_dead": (
            terminal_reason == "SCENARIO_HORIZON_REACHED"
        ),
        "all_targets_dead_required": False,
        "completion_closed": completion_closed,
        "damage_accounting_closed": damage_closed,
        "wire_smoke_complete": complete,
    }


def _outer_input_bindings(
    prepared: PreparedSourceBoundPrototypeWireSmokeV2,
) -> JSONMap:
    manifest = prepared.hypothesis_manifest
    row = prepared.hypothesis_row
    return {
        "dynamic_hypothesis_manifest_v2": {
            "schema": manifest.get("schema"),
            "implementation_revision": manifest.get("implementation_revision"),
            "content_sha256": _mapping(
                manifest.get("content_address"), "hypothesis content address"
            ).get("sha256"),
            "file_sha256": prepared.hypothesis_manifest_file_sha256,
            "file_size_bytes": prepared.hypothesis_manifest_file_size_bytes,
            "execution_status_before_smoke": manifest.get("execution_status"),
            "value_ready": manifest.get("value_ready"),
        },
        "dynamic_hypothesis_row_v2": {
            "segment_ref": row.get("segment_ref"),
            "base_request_sha256": row.get("base_request_sha256"),
            "status": row.get("status"),
            "local_native_wire_smoke_authorized": row.get(
                "local_native_wire_smoke_authorized"
            ),
            "value_ready": row.get("value_ready"),
            "evidence_row_content_sha256": row.get(
                "evidence_row_content_sha256"
            ),
            "derived_request_template_sha256": row.get(
                "derived_request_template_sha256"
            ),
            "pair_binding": deepcopy(row.get("pair_binding")),
        },
        "v1_prepared_execution_projection": deepcopy(
            prepared.v1_execution_projection
        ),
    }


def _build_receipt(
    prepared: PreparedSourceBoundPrototypeWireSmokeV2,
    *,
    inner_receipt: Mapping[str, Any],
) -> JSONMap:
    row = prepared.hypothesis_row
    inner = deepcopy(dict(inner_receipt))
    closure = _wire_smoke_closure(prepared, inner)
    inner_selection = _mapping(inner.get("selection"), "inner selection")
    core: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "status": COMPLETE_STATUS if closure["wire_smoke_complete"] else INCOMPLETE_STATUS,
        "execution_scope": {
            "local_windows_native": True,
            "single_process": True,
            "hpc_dispatch_performed": False,
            "network_request_count": 0,
        },
        "selection": {
            "segment_ref": row.get("segment_ref"),
            "prototype_id": inner_selection.get("prototype_id"),
            "policy_id": inner_selection.get("policy_id"),
            "seed": inner_selection.get("seed"),
            "declared_seed": inner_selection.get("declared_seed"),
            "source_v2_row_status": row.get("status"),
        },
        "input_bindings": _outer_input_bindings(prepared),
        "hypothesis_contract": {
            "prototype_contract": deepcopy(row.get("prototype_contract")),
            "environment_hypotheses": deepcopy(row.get("hypotheses")),
            "observation_leak": deepcopy(row.get("observation_leak")),
            "value_ready": row.get("value_ready"),
        },
        "wire_smoke_closure": closure,
        "inner_v1_receipt": inner,
        "claim_boundary": _claim_boundary(),
    }
    return _content_addressed(core)


def validate_source_bound_prototype_wire_smoke_receipt_v2(
    value: Mapping[str, Any],
    *,
    prepared: PreparedSourceBoundPrototypeWireSmokeV2,
) -> JSONMap:
    """Validate the outer receipt and the retained complete v1 receipt."""

    if not isinstance(prepared, PreparedSourceBoundPrototypeWireSmokeV2):
        raise TypeError("prepared must be PreparedSourceBoundPrototypeWireSmokeV2")
    raw = json.loads(_canonical_bytes(value).decode("utf-8"))
    core = deepcopy(raw)
    address = core.pop("content_address", None)
    expected_address = _content_addressed(core)["content_address"]
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != KIND
        or address != expected_address
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 wire-smoke receipt identity or content address differs"
        )
    if raw.get("execution_scope") != {
        "local_windows_native": True,
        "single_process": True,
        "hpc_dispatch_performed": False,
        "network_request_count": 0,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 wire smoke exceeded its local single-process execution scope"
        )
    if raw.get("claim_boundary") != _claim_boundary():
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 wire-smoke claim boundary differs"
        )
    if raw.get("input_bindings") != _outer_input_bindings(prepared):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 hypothesis manifest, row, or v1 projection binding differs"
        )
    row = prepared.hypothesis_row
    expected_hypotheses = {
        "prototype_contract": deepcopy(row.get("prototype_contract")),
        "environment_hypotheses": deepcopy(row.get("hypotheses")),
        "observation_leak": deepcopy(row.get("observation_leak")),
        "value_ready": False,
    }
    if raw.get("hypothesis_contract") != expected_hypotheses:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 receipt changed an environment or observation-leak hypothesis"
        )
    inner = _mapping(raw.get("inner_v1_receipt"), "inner v1 receipt")
    try:
        checked_inner = smoke_v1.validate_source_bound_prototype_smoke_receipt_v1(
            inner, prepared=prepared.prepared_v1
        )
    except smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            f"retained v1 receipt is invalid: {error}"
        ) from error
    selection = _mapping(checked_inner.get("selection"), "inner selection")
    expected_selection = {
        "segment_ref": row.get("segment_ref"),
        "prototype_id": selection.get("prototype_id"),
        "policy_id": selection.get("policy_id"),
        "seed": prepared.prepared_v1.seed,
        "declared_seed": True,
        "source_v2_row_status": hypothesis_v2.READY_STATUS,
    }
    if (
        raw.get("selection") != expected_selection
        or selection.get("segment_ref") != row.get("segment_ref")
        or selection.get("seed") != prepared.prepared_v1.seed
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "outer selection differs from the retained v1 execution"
        )
    closure = _wire_smoke_closure(prepared, checked_inner)
    if raw.get("wire_smoke_closure") != closure:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "outer wire-smoke closure differs from the retained v1 receipt"
        )
    expected_status = (
        COMPLETE_STATUS if closure["wire_smoke_complete"] else INCOMPLETE_STATUS
    )
    if raw.get("status") != expected_status:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error(
            "v2 status differs from native/horizon/damage receipt closure"
        )
    return raw


def run_local_native_source_bound_prototype_wire_smoke_v2(
    *,
    bundle_path: str | Path,
    model_manifest_path: str | Path,
    dynamic_hypothesis_path: str | Path,
    segment_ref: str,
    prototype_id: str,
    seed: int,
    bridge_path: str | Path = DEFAULT_WINDOWS_V11_BRIDGE,
    simulator_root: str | Path = DEFAULT_SIMULATOR_ROOT,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    """Execute one local/native v2 hypothesis wire smoke, never a value run."""

    prepared = prepare_source_bound_prototype_wire_smoke_v2(
        bundle_path=bundle_path,
        model_manifest_path=model_manifest_path,
        dynamic_hypothesis_path=dynamic_hypothesis_path,
        segment_ref=segment_ref,
        prototype_id=prototype_id,
        seed=seed,
    )
    inner = smoke_v1.run_prepared_local_native_source_bound_prototype_smoke_v1(
        prepared.prepared_v1,
        bridge_path=bridge_path,
        simulator_root=simulator_root,
        max_decisions=max_decisions,
        max_advances=max_advances,
    )
    receipt = _build_receipt(prepared, inner_receipt=inner)
    return validate_source_bound_prototype_wire_smoke_receipt_v2(
        receipt, prepared=prepared
    )


def publish_local_native_source_bound_prototype_wire_smoke_v2(
    *, output_path: str | Path, **kwargs: Any
) -> JSONMap:
    receipt = run_local_native_source_bound_prototype_wire_smoke_v2(**kwargs)
    destination = Path(output_path).expanduser().resolve()
    smoke_v1._atomic_write(destination, _canonical_bytes(receipt, newline=True))
    return {
        "schema": SCHEMA,
        "status": receipt["status"],
        "receipt": str(destination),
        "content_sha256": receipt["content_address"]["sha256"],
        "segment_ref": receipt["selection"]["segment_ref"],
        "prototype_id": receipt["selection"]["prototype_id"],
        "seed": receipt["selection"]["seed"],
        "value_ready": False,
        "hpc_dispatch_performed": False,
        "comparison_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the sole source-bound v2 development-hypothesis pair through "
            "the pinned local Windows v11 bridge"
        )
    )
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE_MANIFEST))
    parser.add_argument("--model-manifest", default=str(DEFAULT_MODEL_MANIFEST))
    parser.add_argument(
        "--dynamic-hypothesis", default=str(DEFAULT_DYNAMIC_HYPOTHESIS)
    )
    parser.add_argument(
        "--segment-ref", default=hypothesis_v2.READY_SEGMENT_REF
    )
    parser.add_argument("--prototype-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--bridge", default=str(DEFAULT_WINDOWS_V11_BRIDGE))
    parser.add_argument("--simulator-root", default=str(DEFAULT_SIMULATOR_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--max-decisions", type=int, default=10_000)
    parser.add_argument("--max-advances", type=int, default=100_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = publish_local_native_source_bound_prototype_wire_smoke_v2(
        output_path=args.output,
        bundle_path=args.bundle,
        model_manifest_path=args.model_manifest,
        dynamic_hypothesis_path=args.dynamic_hypothesis,
        segment_ref=args.segment_ref,
        prototype_id=args.prototype_id,
        seed=args.seed,
        bridge_path=args.bridge,
        simulator_root=args.simulator_root,
        max_decisions=args.max_decisions,
        max_advances=args.max_advances,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__: Sequence[str] = (
    "COMPLETE_STATUS",
    "DEFAULT_BUNDLE_MANIFEST",
    "DEFAULT_DYNAMIC_HYPOTHESIS",
    "DEFAULT_MODEL_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_SIMULATOR_ROOT",
    "DEFAULT_WINDOWS_V11_BRIDGE",
    "HistoricalFurySourceBoundPrototypeSmokeRunnerV2Error",
    "IMPLEMENTATION_REVISION",
    "INCOMPLETE_STATUS",
    "KIND",
    "PreparedSourceBoundPrototypeWireSmokeV2",
    "SCHEMA",
    "main",
    "prepare_source_bound_prototype_wire_smoke_v2",
    "publish_local_native_source_bound_prototype_wire_smoke_v2",
    "run_local_native_source_bound_prototype_wire_smoke_v2",
    "validate_source_bound_prototype_wire_smoke_receipt_v2",
)


if __name__ == "__main__":
    raise SystemExit(main())
