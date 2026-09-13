"""Run one prepared source-bound Fury prototype through the local v11 bridge.

The exact-build bundle remains an immutable duration-mode source.  A separate
content-addressed dynamic preparation must name a READY row for that source
request before this module will construct an executable health-mode request.
The only runner mutation is replacing ``simOptions.randomSeed`` with one seed
already declared by the bundle; all target-health/config bytes come from the
preparation row.

Only one local Windows subprocess is started.  The output retains the full
clone-v2 rollout so request, model, seed, candidate damage, and terminal
lifecycle accounting remain independently checkable.  It is development
smoke evidence, never training, deployment, superiority, or comparison
evidence.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import fury_cat_gap_formal_preparation_v1 as v11_contract
from . import historical_behavior_clone_full_rollout_v2 as full_rollout_v2
from . import historical_behavior_clone_v2 as clone_v2
from . import historical_fury_source_bound_dynamic_config_v1 as dynamic_prep_v1
from . import historical_fury_source_bound_prototype_bundle_v1 as bundle_v1
from .fury_dynamic_target_semantics_v5 import (
    DynamicRolloutLoadV3,
    FuryDynamicTargetSemanticsV5Error,
)
from .sim_bridge_dynamic_v3 import (
    DynamicTargetSemanticsConfigV3,
    SimulatorBridgeDynamicV3,
)


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_prototype_smoke_receipt/v1"
IMPLEMENTATION_REVISION = "v1.1_ready_dynamic_preparation_seed_bound_local_v11"
KIND = "historical_fury_source_bound_prototype_local_native_smoke"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_prototype_smoke_receipt_content/v1"
)
EXECUTION_PAIR_SCHEMA = (
    "historical_fury_source_bound_dynamic_execution_pair/v1"
)
EXECUTION_PAIR_CONTENT_SCHEMA = (
    "historical_fury_source_bound_dynamic_execution_pair_content/v1"
)
COMPLETE_STATUS = "COMPLETE_LOCAL_NATIVE_DEVELOPMENT_SMOKE"
INCOMPLETE_STATUS = "INCOMPLETE_LOCAL_NATIVE_DEVELOPMENT_SMOKE"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_MANIFEST = bundle_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_MODEL_MANIFEST = bundle_v1.DEFAULT_MODEL_MANIFEST
DEFAULT_DYNAMIC_PREPARATION = dynamic_prep_v1.DEFAULT_OUTPUT
DEFAULT_WINDOWS_V11_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / (
        "o2obridge.seedfix-v11.dynamicv3horizonround.withdb."
        "goamd64v1.windows-amd64.exe"
    )
)
DEFAULT_SIMULATOR_ROOT = PROJECT_ROOT.parent / "wowsims-turtle"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_prototype_smoke"
    / "v1"
    / "receipt.json"
)

MODEL_BINDING_SCOPE = (
    "PROTOTYPE_MODEL_ROUTED_THROUGH_AN_OBSERVED_MEMBER_SOURCE_BUILD"
)
SOURCE_ROUTE = "EXACT_SOURCE_BUILD_SEGMENT"


class HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(RuntimeError):
    """The source-bound smoke selection or receipt does not close exactly."""


@dataclass(frozen=True)
class PreparedSourceBoundPrototypeSmokeV1:
    bundle: JSONMap
    bundle_file_sha256: str
    bundle_file_size_bytes: int
    request_row: JSONMap
    base_raid_sim_request: JSONMap
    raid_sim_request: JSONMap
    dynamic_preparation: JSONMap
    dynamic_preparation_file_sha256: str
    dynamic_preparation_file_size_bytes: int
    dynamic_preparation_row: JSONMap
    derived_request_template_sha256: str
    derived_pair_template_sha256: str
    execution_pair_binding: JSONMap
    model: JSONMap
    exact_model_receipt: JSONMap
    model_file_sha256: str
    model_file_size_bytes: int
    model_manifest_file_sha256: str
    model_manifest_content_sha256: str
    seed: int
    dynamic_config: DynamicTargetSemanticsConfigV3
    dynamic_load: DynamicRolloutLoadV3


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
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} must be a finite number"
        )
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} must be a finite number"
        )
    return number


def _resolve_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"{label} does not exist: {path}"
        )
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"cannot read {path}: {error}"
        ) from error
    return digest.hexdigest()


def _strict_json_file(path: Path, label: str) -> tuple[JSONMap, bytes]:
    try:
        return bundle_v1._load_json(path, label)
    except bundle_v1.HistoricalFurySourceBoundPrototypeBundleV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            str(error)
        ) from error


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    payload = deepcopy(dict(core))
    payload.pop("content_address", None)
    return {
        **payload,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        },
    }


def _validate_bundle_boundaries(bundle: Mapping[str, Any]) -> None:
    if bundle.get("status") != "PREPARED":
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound bundle is not PREPARED"
        )
    expected_false = (
        "comparison_authorized",
        "deployment_authorized",
        "scientific_runs_started",
        "training_authorized",
        "superiority_claim_authorized",
    )
    if bundle.get("execution_status") != "NOT_RUN" or any(
        bundle.get(field) is not False for field in expected_false
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound bundle exceeds the development-only boundary"
        )
    contract = _mapping(bundle.get("request_contract"), "request contract")
    if (
        contract.get("current_representative_transplant_count") != 0
        or contract.get("current_character_profile_input_count") != 0
        or contract.get("runtime_snapshot_input_count") != 0
        or contract.get("comparison_receipt_count") != 0
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound bundle contains transplant, runtime snapshot, or comparison residue"
        )


def _select_request_and_binding(
    bundle: Mapping[str, Any], *, segment_ref: str, prototype_id: str
) -> tuple[JSONMap, JSONMap]:
    requests = [
        _mapping(row, "source-bound request")
        for row in _array(bundle.get("requests"), "requests")
        if isinstance(row, Mapping) and row.get("segment_ref") == segment_ref
    ]
    if len(requests) != 1:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "segment_ref must select exactly one declared source request"
        )
    request = requests[0]
    if (
        request.get("source_route") != SOURCE_ROUTE
        or request.get("runtime_executable") is not True
        or request.get("development_build_eligible") is not True
        or request.get("comparison_eligible") is not False
        or request.get("current_character_profile_consumed") is not False
        or request.get("representative_selector_consumed") is not False
        or request.get("runtime_snapshot_consumed") is not False
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "selected request is blocked, transplanted, or comparison-authorized"
        )
    composition = _mapping(request.get("composition"), "request composition")
    audit = _mapping(composition.get("audit"), "composition audit")
    admission = _mapping(audit.get("admission"), "composition admission")
    if (
        composition.get("schema") != "build_request_composer/v1"
        or admission.get("status") != "ADMITTED"
        or admission.get("runtime_executable") is not True
        or admission.get("blockers") != []
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "selected composed request is not runtime-admitted"
        )
    bindings = [
        _mapping(row, "historical policy binding")
        for row in _array(
            request.get("historical_policy_bindings"),
            "historical policy bindings",
        )
        if isinstance(row, Mapping) and row.get("prototype_id") == prototype_id
    ]
    if len(bindings) != 1:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "prototype_id must select exactly one declared model binding"
        )
    binding = bindings[0]
    support = _mapping(
        _mapping(request.get("decision_support"), "decision support").get(
            "by_prototype"
        ),
        "prototype decision support",
    )
    if (
        binding.get("binding_scope") != MODEL_BINDING_SCOPE
        or binding.get("model_is_build_conditioned") is not False
        or binding.get("comparison_authorized") is not False
        or binding.get("segment_weighted_decision_support")
        != support.get(prototype_id)
        or _integer(
            binding.get("segment_weighted_decision_support"),
            "segment weighted decision support",
            minimum=1,
        )
        < 1
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "selected model is not the declared non-comparison source-bound prototype binding"
        )
    return deepcopy(dict(request)), deepcopy(dict(binding))


def _validate_declared_seed(bundle: Mapping[str, Any], seed: int) -> int:
    chosen = _integer(seed, "seed", minimum=1)
    seed_block = _mapping(bundle.get("development_seeds"), "development seeds")
    seeds = [
        _integer(value, "declared development seed", minimum=1)
        for value in _array(seed_block.get("master_seeds"), "master seeds")
    ]
    if (
        not seeds
        or len(seeds) != len(set(seeds))
        or seed_block.get("count") != len(seeds)
        or seed_block.get("development_only") is not True
        or seed_block.get("fixed_sample_no_optional_stopping") is not True
        or seed_block.get("seed_list_sha256")
        != hashlib.sha256(_canonical_bytes(seeds)).hexdigest()
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "declared development seed set does not close"
        )
    if chosen not in seeds:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "seed is not declared by the source-bound bundle"
        )
    return chosen


def _select_dynamic_preparation_row(
    artifact: Mapping[str, Any],
    *,
    segment_ref: str,
    base_request_sha256: str,
    seed_list_sha256: str,
) -> JSONMap:
    rows = [
        _mapping(row, "dynamic preparation row")
        for row in _array(artifact.get("requests"), "dynamic preparation requests")
        if isinstance(row, Mapping)
        and row.get("segment_ref") == segment_ref
        and row.get("base_request_sha256") == base_request_sha256
    ]
    if len(rows) != 1:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "dynamic preparation must select exactly one source request"
        )
    row = deepcopy(dict(rows[0]))
    if row.get("status") != "READY":
        blockers = row.get("blockers")
        codes = (
            ",".join(
                str(blocker.get("code"))
                for blocker in blockers
                if isinstance(blocker, Mapping) and blocker.get("code")
            )
            if isinstance(blockers, list)
            else ""
        )
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "dynamic preparation row is not READY"
            + (f": {codes}" if codes else "")
        )
    if row.get("development_seed_list_sha256") != seed_list_sha256:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "dynamic preparation seed-list binding differs from the bundle"
        )
    for field in (
        "derived_request_template",
        "derived_request_template_sha256",
        "dynamic_load_config",
        "derived_pair_content_sha256",
    ):
        if row.get(field) is None:
            raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
                f"READY dynamic preparation lacks {field}"
            )
    return row


def _request_with_declared_seed(template: Mapping[str, Any], seed: int) -> JSONMap:
    request = deepcopy(dict(template))
    sim_options = _mapping(request.get("simOptions"), "derived request simOptions")
    if "randomSeed" not in sim_options:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "derived request template lacks simOptions.randomSeed"
        )
    seeded_options = deepcopy(dict(sim_options))
    seeded_options["randomSeed"] = str(seed)
    request["simOptions"] = seeded_options
    return request


def _execution_pair_binding(
    *,
    dynamic_preparation_content_sha256: str,
    segment_ref: str,
    base_request_sha256: str,
    seed_list_sha256: str,
    template_sha256: str,
    template_pair_sha256: str,
    seed: int,
    request: Mapping[str, Any],
    dynamic_load: DynamicRolloutLoadV3,
) -> JSONMap:
    load_wire = dynamic_load.to_wire()
    core: JSONMap = {
        "schema": EXECUTION_PAIR_SCHEMA,
        "dynamic_preparation_content_sha256": dynamic_preparation_content_sha256,
        "segment_ref": segment_ref,
        "base_request_sha256": base_request_sha256,
        "development_seed_list_sha256": seed_list_sha256,
        "derived_request_template_sha256": template_sha256,
        "derived_pair_template_content_sha256": template_pair_sha256,
        "selected_seed": seed,
        "final_request_sha256": hashlib.sha256(
            _canonical_bytes(request)
        ).hexdigest(),
        "dynamic_rollout_load": load_wire,
        "dynamic_rollout_load_sha256": hashlib.sha256(
            _canonical_bytes(load_wire)
        ).hexdigest(),
    }
    return {
        **core,
        "content_address": {
            "schema": EXECUTION_PAIR_CONTENT_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
        },
    }


def _load_bound_model(
    *,
    bundle: Mapping[str, Any],
    binding: Mapping[str, Any],
    model_manifest_path: Path,
) -> tuple[JSONMap, JSONMap, str, int, str, str]:
    try:
        manifest, manifest_content_sha, descriptors = (
            bundle_v1._model_manifest_index(model_manifest_path)
        )
    except bundle_v1.HistoricalFurySourceBoundPrototypeBundleV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(str(error)) from error
    manifest_file_sha = _sha256_file(model_manifest_path)
    closure = _mapping(bundle.get("input_closure"), "bundle input closure")
    declared_manifest = _mapping(
        closure.get("behavior_clone_model_manifest"),
        "declared model manifest",
    )
    if declared_manifest != {
        "schema": clone_v2.MANIFEST_SCHEMA,
        "implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
        "content_sha256": manifest_content_sha,
        "file_sha256": manifest_file_sha,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "model manifest differs from the source-bound bundle closure"
        )
    prototype_id = _text(binding.get("prototype_id"), "binding prototype_id")
    descriptor = descriptors.get(prototype_id)
    if descriptor is None:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "bound prototype is absent from the supplied model manifest"
        )
    try:
        model_path = bundle_v1._resolve_locator(
            model_manifest_path.parent,
            descriptor.get("path"),
            f"model {prototype_id} path",
        )
        model, loaded = full_rollout_v2.load_behavior_clone_model_v2(model_path)
    except (
        bundle_v1.HistoricalFurySourceBoundPrototypeBundleV1Error,
        full_rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
    ) as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(str(error)) from error
    exact = _mapping(
        binding.get("exact_validation_receipt"), "exact validation receipt"
    )
    expected = {
        "prototype_id": binding.get("prototype_id"),
        "prototype_family": binding.get("prototype_family"),
        "policy_id": binding.get("policy_id"),
        "model_schema": binding.get("model_schema"),
        "model_implementation_revision": binding.get(
            "model_implementation_revision"
        ),
        "model_content_sha256": binding.get("model_content_sha256"),
        "model_file_sha256": binding.get("model_file_sha256"),
        "model_file_size_bytes": binding.get("model_file_size_bytes"),
    }
    observed = {
        "prototype_id": loaded.get("prototype_id"),
        "prototype_family": loaded.get("prototype_family"),
        "policy_id": loaded.get("policy_id"),
        "model_schema": clone_v2.MODEL_SCHEMA,
        "model_implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
        "model_content_sha256": loaded.get("model_sha256"),
        "model_file_sha256": loaded.get("model_file_sha256"),
        "model_file_size_bytes": loaded.get("model_file_size_bytes"),
    }
    if (
        observed != expected
        or loaded.get("external_exact_validation_receipt") != exact
        or descriptor.get("content_sha256") != expected["model_content_sha256"]
        or descriptor.get("file_sha256") != expected["model_file_sha256"]
        or descriptor.get("file_size_bytes") != expected["model_file_size_bytes"]
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "model bytes or exact validation receipt differ from the selected bundle binding"
        )
    return (
        deepcopy(model),
        deepcopy(dict(exact)),
        str(loaded["model_file_sha256"]),
        int(loaded["model_file_size_bytes"]),
        manifest_file_sha,
        manifest_content_sha,
    )


def prepare_source_bound_prototype_smoke_v1(
    *,
    bundle_path: str | Path,
    model_manifest_path: str | Path,
    dynamic_preparation_path: str | Path,
    segment_ref: str,
    prototype_id: str,
    seed: int,
) -> PreparedSourceBoundPrototypeSmokeV1:
    """Close one READY derived request/model/declared-seed selection."""

    segment_ref = _text(segment_ref, "segment_ref")
    prototype_id = _text(prototype_id, "prototype_id")
    source = _resolve_file(bundle_path, "source-bound bundle")
    model_manifest = _resolve_file(model_manifest_path, "model manifest")
    bundle, raw = _strict_json_file(source, "source-bound bundle")
    try:
        validated = bundle_v1.validate_historical_fury_source_bound_prototype_bundle_v1(
            bundle, verify_source_bytes=False
        )
    except bundle_v1.HistoricalFurySourceBoundPrototypeBundleV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(str(error)) from error
    _validate_bundle_boundaries(validated)
    request_row, binding = _select_request_and_binding(
        validated, segment_ref=segment_ref, prototype_id=prototype_id
    )
    selected_seed = _validate_declared_seed(validated, seed)
    base_request = deepcopy(
        dict(
            _mapping(
                _mapping(request_row.get("composition"), "composition").get(
                    "request"
                ),
                "composed RaidSimRequest",
            )
        )
    )
    base_request_sha = hashlib.sha256(_canonical_bytes(base_request)).hexdigest()
    if base_request_sha != request_row.get("request_sha256"):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "selected RaidSimRequest differs from its bundle digest"
        )
    if _mapping(base_request.get("encounter"), "base encounter").get(
        "useHealth"
    ) is True:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound bundle request must remain duration-mode"
        )
    preparation_source = _resolve_file(
        dynamic_preparation_path, "dynamic preparation"
    )
    try:
        dynamic_preparation = (
            dynamic_prep_v1.load_historical_fury_source_bound_dynamic_config_v1(
                preparation_source
            )
        )
    except dynamic_prep_v1.HistoricalFurySourceBoundDynamicConfigV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"dynamic preparation is invalid: {error}"
        ) from error
    preparation_bundle = _mapping(
        _mapping(
            dynamic_preparation.get("input_closure"),
            "dynamic preparation input closure",
        ).get("source_bound_prototype_bundle"),
        "dynamic preparation source bundle closure",
    )
    seed_list_sha = _text(
        _mapping(validated.get("development_seeds"), "development seeds").get(
            "seed_list_sha256"
        ),
        "development seed-list SHA-256",
    )
    expected_preparation_bundle = {
        "schema": validated["schema"],
        "implementation_revision": validated["implementation_revision"],
        "content_sha256": validated["content_address"]["sha256"],
        "request_count": len(validated["requests"]),
        "development_seed_list_sha256": seed_list_sha,
        "development_seed_count": len(
            validated["development_seeds"]["master_seeds"]
        ),
    }
    if dict(preparation_bundle) != expected_preparation_bundle:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "dynamic preparation is bound to a different source bundle"
        )
    dynamic_row = _select_dynamic_preparation_row(
        dynamic_preparation,
        segment_ref=segment_ref,
        base_request_sha256=base_request_sha,
        seed_list_sha256=seed_list_sha,
    )
    try:
        request_template, dynamic_config = (
            dynamic_prep_v1.select_ready_dynamic_config_v1(
                dynamic_preparation,
                segment_ref=segment_ref,
                request_sha256=base_request_sha,
            )
        )
    except dynamic_prep_v1.HistoricalFurySourceBoundDynamicConfigV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"dynamic preparation row cannot execute: {error}"
        ) from error
    if not isinstance(dynamic_config, DynamicTargetSemanticsConfigV3):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "READY preparation returned the wrong dynamic config type"
        )
    template_sha = hashlib.sha256(_canonical_bytes(request_template)).hexdigest()
    if (
        template_sha != dynamic_row.get("derived_request_template_sha256")
        or request_template != dynamic_row.get("derived_request_template")
        or dynamic_config.to_wire() != dynamic_row.get("dynamic_load_config")
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "selected READY template/config differs from its preparation row"
        )
    request = _request_with_declared_seed(request_template, selected_seed)
    try:
        full_rollout_v2.validate_executable_fury_request_v2(request)
        dynamic_load = DynamicRolloutLoadV3.bind(
            request, selected_seed, dynamic_config
        )
    except (
        TypeError,
        ValueError,
        FuryDynamicTargetSemanticsV5Error,
        full_rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
    ) as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"selected request/dynamic config is not executable as declared: {error}"
        ) from error
    preparation_address = _mapping(
        dynamic_preparation.get("content_address"),
        "dynamic preparation content address",
    )
    preparation_content_sha = _text(
        preparation_address.get("sha256"),
        "dynamic preparation content SHA-256",
    )
    pair_binding = _execution_pair_binding(
        dynamic_preparation_content_sha256=preparation_content_sha,
        segment_ref=segment_ref,
        base_request_sha256=base_request_sha,
        seed_list_sha256=seed_list_sha,
        template_sha256=template_sha,
        template_pair_sha256=_text(
            dynamic_row.get("derived_pair_content_sha256"),
            "derived pair template content SHA-256",
        ),
        seed=selected_seed,
        request=request,
        dynamic_load=dynamic_load,
    )
    (
        model,
        exact,
        model_file_sha,
        model_file_size,
        manifest_file_sha,
        manifest_content_sha,
    ) = _load_bound_model(
        bundle=validated,
        binding=binding,
        model_manifest_path=model_manifest,
    )
    return PreparedSourceBoundPrototypeSmokeV1(
        bundle=deepcopy(validated),
        bundle_file_sha256=hashlib.sha256(raw).hexdigest(),
        bundle_file_size_bytes=len(raw),
        request_row=request_row,
        base_raid_sim_request=base_request,
        raid_sim_request=request,
        dynamic_preparation=deepcopy(dynamic_preparation),
        dynamic_preparation_file_sha256=_sha256_file(preparation_source),
        dynamic_preparation_file_size_bytes=preparation_source.stat().st_size,
        dynamic_preparation_row=dynamic_row,
        derived_request_template_sha256=template_sha,
        derived_pair_template_sha256=str(
            dynamic_row["derived_pair_content_sha256"]
        ),
        execution_pair_binding=pair_binding,
        model=model,
        exact_model_receipt=exact,
        model_file_sha256=model_file_sha,
        model_file_size_bytes=model_file_size,
        model_manifest_file_sha256=manifest_file_sha,
        model_manifest_content_sha256=manifest_content_sha,
        seed=selected_seed,
        dynamic_config=dynamic_config,
        dynamic_load=dynamic_load,
    )


def _verify_windows_v11_bridge_v1(path: str | Path) -> JSONMap:
    if os.name != "nt":
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "local native source-bound smoke requires Windows"
        )
    binary = _resolve_file(path, "Windows v11 bridge")
    identity = {
        "filename": binary.name,
        "sha256": _sha256_file(binary),
        "size_bytes": binary.stat().st_size,
        "platform": "windows-amd64",
    }
    if identity != {
        "filename": DEFAULT_WINDOWS_V11_BRIDGE.name,
        "sha256": v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SHA256,
        "size_bytes": v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SIZE,
        "platform": "windows-amd64",
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "local bridge is not the pinned immutable Windows v11 binary"
        )
    return identity


def _damage_lifecycle_receipt(rollout: Mapping[str, Any]) -> JSONMap:
    closure = _mapping(
        rollout.get("dynamic_v3_runtime_receipt_closure"),
        "rollout runtime receipt closure",
    )
    candidate = _mapping(closure.get("candidate_damage"), "candidate damage")
    raw_receipts = candidate.get("receipts")
    if not isinstance(raw_receipts, (list, tuple)):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "candidate damage receipts must be an array"
        )
    # ``dataclasses.asdict`` preserves tuples in the live rollout returned by
    # the native runner.  Persisted JSON naturally reloads the same batch as a
    # list, so both are the one supported receipt-array shape here.
    receipts = list(raw_receipts)
    if not all(isinstance(row, Mapping) for row in receipts):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "candidate damage receipts must be objects"
        )
    candidate_damage = sum(
        _finite_number(row.get("applied_damage"), "candidate applied damage")
        for row in receipts
    )
    lifecycle = _mapping(
        closure.get("terminal_lifecycle"), "terminal lifecycle"
    )
    terminal_damage = _finite_number(
        lifecycle.get("simulated_damage_applied"),
        "terminal simulated damage",
    )
    rollout_damage = _finite_number(rollout.get("damage_delta"), "damage delta")
    damage_closed = (
        abs(candidate_damage - terminal_damage) <= 1e-9
        and abs(rollout_damage - terminal_damage) <= 1e-9
    )
    completion = _mapping(
        rollout.get("configured_completion"), "configured completion"
    )
    return {
        "runtime_receipt_status": closure.get("status"),
        "scenario_complete": rollout.get("scenario_complete") is True,
        "terminal_reason": completion.get("terminal_reason"),
        "elapsed_ms": rollout.get("elapsed_ms"),
        "candidate_damage_receipt_count": len(receipts),
        "candidate_applied_damage": candidate_damage,
        "terminal_simulated_damage_applied": terminal_damage,
        "rollout_damage_delta": rollout_damage,
        "damage_accounting_closed": damage_closed,
    }


def _build_receipt(
    prepared: PreparedSourceBoundPrototypeSmokeV1,
    *,
    bridge_identity: Mapping[str, Any],
    rollout: Mapping[str, Any],
) -> JSONMap:
    row = prepared.request_row
    binding = next(
        binding
        for binding in row["historical_policy_bindings"]
        if binding["prototype_id"] == prepared.exact_model_receipt["prototype_id"]
    )
    lifecycle = _damage_lifecycle_receipt(rollout)
    complete = (
        rollout.get("status") == full_rollout_v2.STATUS
        and lifecycle["runtime_receipt_status"] == "COMPLETE_BOUND"
        and lifecycle["scenario_complete"] is True
        and lifecycle["damage_accounting_closed"] is True
    )
    core: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": KIND,
        "status": COMPLETE_STATUS if complete else INCOMPLETE_STATUS,
        "execution_scope": {
            "local_windows_native": True,
            "single_process": True,
            "hpc_dispatch_performed": False,
            "network_request_count": 0,
        },
        "selection": {
            "source_route": row["source_route"],
            "segment_ref": row["segment_ref"],
            "catalog_line_number": row["catalog_line_number"],
            "prototype_id": binding["prototype_id"],
            "policy_id": binding["policy_id"],
            "seed": prepared.seed,
            "declared_seed": True,
        },
        "input_bindings": {
            "source_bound_bundle": {
                "schema": prepared.bundle["schema"],
                "implementation_revision": prepared.bundle[
                    "implementation_revision"
                ],
                "content_sha256": prepared.bundle["content_address"]["sha256"],
                "file_sha256": prepared.bundle_file_sha256,
                "file_size_bytes": prepared.bundle_file_size_bytes,
            },
            "base_raid_sim_request": {
                "sha256": row["request_sha256"],
                "consumed_from": "composition.request",
                "duration_mode_preserved_in_source_bundle": True,
                "source_bundle_mutated": False,
            },
            "dynamic_preparation": {
                "schema": prepared.dynamic_preparation["schema"],
                "implementation_revision": prepared.dynamic_preparation[
                    "implementation_revision"
                ],
                "content_sha256": prepared.dynamic_preparation[
                    "content_address"
                ]["sha256"],
                "file_sha256": prepared.dynamic_preparation_file_sha256,
                "file_size_bytes": prepared.dynamic_preparation_file_size_bytes,
                "row_status": prepared.dynamic_preparation_row["status"],
                "development_seed_list_sha256": (
                    prepared.dynamic_preparation_row[
                        "development_seed_list_sha256"
                    ]
                ),
                "derived_request_template_sha256": (
                    prepared.derived_request_template_sha256
                ),
                "derived_pair_template_content_sha256": (
                    prepared.derived_pair_template_sha256
                ),
            },
            "derived_raid_sim_request": {
                "sha256": prepared.dynamic_load.request_sha256,
                "consumed_from": "READY dynamic preparation template",
                "template_sha256": prepared.derived_request_template_sha256,
                "only_runner_mutation": "simOptions.randomSeed",
                "selected_seed": prepared.seed,
            },
            "behavior_clone_model_manifest": {
                "schema": clone_v2.MANIFEST_SCHEMA,
                "implementation_revision": clone_v2.IMPLEMENTATION_REVISION,
                "content_sha256": prepared.model_manifest_content_sha256,
                "file_sha256": prepared.model_manifest_file_sha256,
            },
            "behavior_clone_model": {
                "schema": clone_v2.MODEL_SCHEMA,
                "prototype_id": binding["prototype_id"],
                "policy_id": binding["policy_id"],
                "content_sha256": binding["model_content_sha256"],
                "file_sha256": prepared.model_file_sha256,
                "file_size_bytes": prepared.model_file_size_bytes,
                "exact_validation_receipt": deepcopy(
                    prepared.exact_model_receipt
                ),
            },
            "dynamic_rollout_load": prepared.dynamic_load.to_wire(),
            "dynamic_execution_pair": deepcopy(
                prepared.execution_pair_binding
            ),
            "windows_v11_bridge": deepcopy(dict(bridge_identity)),
        },
        "damage_lifecycle_closure": lifecycle,
        "rollout": deepcopy(dict(rollout)),
        "claim_boundary": {
            "development_smoke_only": True,
            "source_request_bound": True,
            "source_model_bound": True,
            "source_seed_bound": True,
            "current_character_transplant": False,
            "comparison_authorized": False,
            "same_equipment_matched_seed_comparison_authorized": False,
            "training_authorized": False,
            "deployment_authorized": False,
            "superiority_claim_authorized": False,
        },
    }
    return _content_addressed(core)


def validate_source_bound_prototype_smoke_receipt_v1(
    value: Mapping[str, Any],
    *,
    prepared: PreparedSourceBoundPrototypeSmokeV1,
) -> JSONMap:
    """Validate one persisted receipt against its independently loaded inputs."""

    if not isinstance(prepared, PreparedSourceBoundPrototypeSmokeV1):
        raise TypeError("prepared must be PreparedSourceBoundPrototypeSmokeV1")
    raw = json.loads(_canonical_bytes(value).decode("utf-8"))
    address = _mapping(raw.get("content_address"), "content address")
    core = deepcopy(raw)
    core.pop("content_address", None)
    expected_address = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != KIND
        or dict(address) != expected_address
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound smoke receipt identity or content address differs"
        )
    scope = _mapping(raw.get("execution_scope"), "execution scope")
    if scope != {
        "local_windows_native": True,
        "single_process": True,
        "hpc_dispatch_performed": False,
        "network_request_count": 0,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound smoke exceeded its local single-process execution scope"
        )
    boundary = _mapping(raw.get("claim_boundary"), "claim boundary")
    if boundary != {
        "development_smoke_only": True,
        "source_request_bound": True,
        "source_model_bound": True,
        "source_seed_bound": True,
        "current_character_transplant": False,
        "comparison_authorized": False,
        "same_equipment_matched_seed_comparison_authorized": False,
        "training_authorized": False,
        "deployment_authorized": False,
        "superiority_claim_authorized": False,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "source-bound smoke claim boundary differs"
        )
    row = prepared.request_row
    binding = next(
        candidate
        for candidate in row["historical_policy_bindings"]
        if candidate["prototype_id"]
        == prepared.exact_model_receipt["prototype_id"]
    )
    selection = _mapping(raw.get("selection"), "selection")
    if selection != {
        "source_route": SOURCE_ROUTE,
        "segment_ref": row["segment_ref"],
        "catalog_line_number": row["catalog_line_number"],
        "prototype_id": binding["prototype_id"],
        "policy_id": binding["policy_id"],
        "seed": prepared.seed,
        "declared_seed": True,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt selection differs from the prepared request/model/seed"
        )
    inputs = _mapping(raw.get("input_bindings"), "input bindings")
    request_binding = _mapping(
        inputs.get("base_raid_sim_request"), "base request binding"
    )
    if request_binding != {
        "sha256": row["request_sha256"],
        "consumed_from": "composition.request",
        "duration_mode_preserved_in_source_bundle": True,
        "source_bundle_mutated": False,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt does not preserve the exact duration-mode source request"
        )
    bundle_binding = _mapping(
        inputs.get("source_bound_bundle"), "bundle binding"
    )
    if bundle_binding != {
        "schema": prepared.bundle["schema"],
        "implementation_revision": prepared.bundle["implementation_revision"],
        "content_sha256": prepared.bundle["content_address"]["sha256"],
        "file_sha256": prepared.bundle_file_sha256,
        "file_size_bytes": prepared.bundle_file_size_bytes,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt bundle binding differs"
        )
    if inputs.get("dynamic_rollout_load") != prepared.dynamic_load.to_wire():
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt dynamic request/seed/config binding differs"
        )
    preparation_binding = _mapping(
        inputs.get("dynamic_preparation"), "dynamic preparation binding"
    )
    if preparation_binding != {
        "schema": prepared.dynamic_preparation["schema"],
        "implementation_revision": prepared.dynamic_preparation[
            "implementation_revision"
        ],
        "content_sha256": prepared.dynamic_preparation["content_address"][
            "sha256"
        ],
        "file_sha256": prepared.dynamic_preparation_file_sha256,
        "file_size_bytes": prepared.dynamic_preparation_file_size_bytes,
        "row_status": "READY",
        "development_seed_list_sha256": prepared.dynamic_preparation_row[
            "development_seed_list_sha256"
        ],
        "derived_request_template_sha256": (
            prepared.derived_request_template_sha256
        ),
        "derived_pair_template_content_sha256": (
            prepared.derived_pair_template_sha256
        ),
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt dynamic preparation binding differs"
        )
    derived_request_binding = _mapping(
        inputs.get("derived_raid_sim_request"), "derived request binding"
    )
    if derived_request_binding != {
        "sha256": prepared.dynamic_load.request_sha256,
        "consumed_from": "READY dynamic preparation template",
        "template_sha256": prepared.derived_request_template_sha256,
        "only_runner_mutation": "simOptions.randomSeed",
        "selected_seed": prepared.seed,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt final derived-request binding differs"
        )
    if inputs.get("dynamic_execution_pair") != prepared.execution_pair_binding:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt content-bound dynamic execution pair differs"
        )
    model_binding = _mapping(
        inputs.get("behavior_clone_model"), "model binding"
    )
    if (
        model_binding.get("prototype_id") != binding["prototype_id"]
        or model_binding.get("policy_id") != binding["policy_id"]
        or model_binding.get("content_sha256")
        != binding["model_content_sha256"]
        or model_binding.get("file_sha256") != prepared.model_file_sha256
        or model_binding.get("file_size_bytes") != prepared.model_file_size_bytes
        or model_binding.get("exact_validation_receipt")
        != prepared.exact_model_receipt
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt model binding differs from the prepared model"
        )
    bridge = _mapping(inputs.get("windows_v11_bridge"), "Windows v11 bridge")
    if bridge != {
        "filename": DEFAULT_WINDOWS_V11_BRIDGE.name,
        "sha256": v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SHA256,
        "size_bytes": v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SIZE,
        "platform": "windows-amd64",
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "receipt bridge binding is not pinned Windows v11"
        )
    rollout = _mapping(raw.get("rollout"), "rollout")
    if (
        rollout.get("prototype_id") != binding["prototype_id"]
        or rollout.get("policy_id") != binding["policy_id"]
        or _mapping(
            rollout.get("executable_request_preflight"),
            "rollout request preflight",
        ).get("schema")
        != "historical_behavior_clone_executable_fury_request/v2"
        or _mapping(
            rollout.get("bridge_runtime_evidence"),
            "rollout bridge runtime evidence",
        ).get("native_subprocess_bridge")
        is not True
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "rollout is not the selected prototype on a native executable request"
        )
    expected_rollout_binding = full_rollout_v2._model_binding(
        prepared.model,
        expected_model_binding=prepared.exact_model_receipt,
    )
    try:
        full_rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
            rollout,
            _mapping(
                rollout.get("dynamic_v3_runtime_receipt_closure"),
                "runtime receipt closure",
            ),
            prepared.dynamic_load,
            expected_model_binding=expected_rollout_binding,
            expected_exact_model_validation_receipt=prepared.exact_model_receipt,
            expected_bridge_runtime_evidence=_mapping(
                rollout.get("bridge_runtime_evidence"),
                "bridge runtime evidence",
            ),
            expected_dynamic_load_binding=_mapping(
                rollout.get("dynamic_load_binding"), "dynamic load binding"
            ),
        )
    except full_rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"nested clone-v2 rollout does not close: {error}"
        ) from error
    lifecycle = _damage_lifecycle_receipt(rollout)
    if raw.get("damage_lifecycle_closure") != lifecycle:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "damage/lifecycle summary differs from the retained rollout"
        )
    complete = (
        rollout.get("status") == full_rollout_v2.STATUS
        and lifecycle["runtime_receipt_status"] == "COMPLETE_BOUND"
        and lifecycle["scenario_complete"] is True
        and lifecycle["damage_accounting_closed"] is True
    )
    expected_status = COMPLETE_STATUS if complete else INCOMPLETE_STATUS
    if raw.get("status") != expected_status:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            "smoke status differs from nested rollout/lifecycle closure"
        )
    return raw


def run_prepared_local_native_source_bound_prototype_smoke_v1(
    prepared: PreparedSourceBoundPrototypeSmokeV1,
    *,
    bridge_path: str | Path = DEFAULT_WINDOWS_V11_BRIDGE,
    simulator_root: str | Path = DEFAULT_SIMULATOR_ROOT,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    """Execute one already-prepared bounded local/native source-bound smoke."""

    if not isinstance(prepared, PreparedSourceBoundPrototypeSmokeV1):
        raise TypeError(
            "prepared must be PreparedSourceBoundPrototypeSmokeV1"
        )
    bridge_identity = _verify_windows_v11_bridge_v1(bridge_path)
    cwd = Path(simulator_root).expanduser().resolve()
    if not cwd.is_dir():
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
            f"simulator root does not exist: {cwd}"
        )
    with SimulatorBridgeDynamicV3(
        Path(bridge_path).expanduser().resolve(),
        cwd=cwd,
        environment={"GOMAXPROCS": "1"},
    ) as bridge:
        if not isinstance(bridge, SimulatorBridgeDynamicV3):
            raise HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error(
                "runner did not create the native dynamic-v3 bridge"
            )
        rollout = full_rollout_v2.run_behavior_clone_dynamic_v5_rollout_v2(
            bridge,
            prepared.raid_sim_request,
            model=prepared.model,
            expected_model_binding=prepared.exact_model_receipt,
            seed=prepared.seed,
            dynamic_load=prepared.dynamic_load,
            max_decisions=_integer(max_decisions, "max_decisions", minimum=1),
            max_advances=_integer(max_advances, "max_advances", minimum=1),
            retain_steps=True,
        )
    receipt = _build_receipt(
        prepared, bridge_identity=bridge_identity, rollout=rollout
    )
    return validate_source_bound_prototype_smoke_receipt_v1(
        receipt, prepared=prepared
    )


def run_local_native_source_bound_prototype_smoke_v1(
    *,
    bundle_path: str | Path,
    model_manifest_path: str | Path,
    dynamic_preparation_path: str | Path,
    segment_ref: str,
    prototype_id: str,
    seed: int,
    bridge_path: str | Path = DEFAULT_WINDOWS_V11_BRIDGE,
    simulator_root: str | Path = DEFAULT_SIMULATOR_ROOT,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    """Prepare and execute exactly one bounded local/native source-bound smoke."""

    prepared = prepare_source_bound_prototype_smoke_v1(
        bundle_path=bundle_path,
        model_manifest_path=model_manifest_path,
        dynamic_preparation_path=dynamic_preparation_path,
        segment_ref=segment_ref,
        prototype_id=prototype_id,
        seed=seed,
    )
    return run_prepared_local_native_source_bound_prototype_smoke_v1(
        prepared,
        bridge_path=bridge_path,
        simulator_root=simulator_root,
        max_decisions=max_decisions,
        max_advances=max_advances,
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


def publish_local_native_source_bound_prototype_smoke_v1(
    *, output_path: str | Path, **kwargs: Any
) -> JSONMap:
    receipt = run_local_native_source_bound_prototype_smoke_v1(**kwargs)
    destination = Path(output_path).expanduser().resolve()
    _atomic_write(destination, _canonical_bytes(receipt, newline=True))
    return {
        "schema": SCHEMA,
        "status": receipt["status"],
        "receipt": str(destination),
        "content_sha256": receipt["content_address"]["sha256"],
        "segment_ref": receipt["selection"]["segment_ref"],
        "prototype_id": receipt["selection"]["prototype_id"],
        "seed": receipt["selection"]["seed"],
        "hpc_dispatch_performed": False,
        "comparison_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one exact source-bound prototype using the pinned local Windows v11 bridge"
        )
    )
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE_MANIFEST))
    parser.add_argument("--model-manifest", default=str(DEFAULT_MODEL_MANIFEST))
    parser.add_argument(
        "--dynamic-preparation", default=str(DEFAULT_DYNAMIC_PREPARATION)
    )
    parser.add_argument("--segment-ref", required=True)
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
    result = publish_local_native_source_bound_prototype_smoke_v1(
        output_path=args.output,
        bundle_path=args.bundle,
        model_manifest_path=args.model_manifest,
        dynamic_preparation_path=args.dynamic_preparation,
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "COMPLETE_STATUS",
    "CONTENT_ADDRESS_SCHEMA",
    "DEFAULT_BUNDLE_MANIFEST",
    "DEFAULT_DYNAMIC_PREPARATION",
    "DEFAULT_MODEL_MANIFEST",
    "DEFAULT_OUTPUT",
    "DEFAULT_SIMULATOR_ROOT",
    "DEFAULT_WINDOWS_V11_BRIDGE",
    "HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error",
    "IMPLEMENTATION_REVISION",
    "INCOMPLETE_STATUS",
    "KIND",
    "PreparedSourceBoundPrototypeSmokeV1",
    "SCHEMA",
    "main",
    "prepare_source_bound_prototype_smoke_v1",
    "publish_local_native_source_bound_prototype_smoke_v1",
    "run_local_native_source_bound_prototype_smoke_v1",
    "run_prepared_local_native_source_bound_prototype_smoke_v1",
    "validate_source_bound_prototype_smoke_receipt_v1",
]
