"""Causal source-bound clone wire smoke built on the frozen v2 preparation.

This runner adds only a development-only execution lane.  It reuses the v2
hypothesis preparation, derives target introduction and current/maximum-health
registries only from its validated strict-prefix checkpoint artifact, and then
executes clone rollout v3.  A dynamic health hypothesis is never accepted as
checkpoint evidence.  No existing production caller or frozen predecessor is
changed.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import historical_behavior_clone_full_rollout_v2 as clone_v2
from . import historical_behavior_clone_full_rollout_v3 as clone_v3
from . import historical_fury_source_bound_prefix_checkpoint_v1 as checkpoint_v1
from . import historical_fury_source_bound_prototype_smoke_runner_v1 as smoke_v1
from . import historical_fury_source_bound_prototype_smoke_runner_v2 as smoke_v2
from .policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
)
from .sim_bridge_dynamic_v3 import DynamicLoadReceiptV3, SimulatorBridgeDynamicV3


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_prototype_wire_smoke_receipt/v3"
IMPLEMENTATION_REVISION = "v3.0_causal_observation_clone_v3"
KIND = "historical_fury_source_bound_prototype_causal_wire_smoke_receipt"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_prototype_wire_smoke_receipt_content/v3"
)
COMPLETE_STATUS = "COMPLETE_LOCAL_NATIVE_CAUSAL_WIRE_SMOKE_NONVOTING"
INCOMPLETE_STATUS = "INCOMPLETE_LOCAL_NATIVE_CAUSAL_WIRE_SMOKE_NONVOTING"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_MANIFEST = smoke_v2.DEFAULT_BUNDLE_MANIFEST
DEFAULT_MODEL_MANIFEST = smoke_v2.DEFAULT_MODEL_MANIFEST
DEFAULT_DYNAMIC_HYPOTHESIS = smoke_v2.DEFAULT_DYNAMIC_HYPOTHESIS
DEFAULT_PREFIX_CHECKPOINT = checkpoint_v1.DEFAULT_OUTPUT_DIRECTORY / "manifest.json"
DEFAULT_WINDOWS_V11_BRIDGE = smoke_v2.DEFAULT_WINDOWS_V11_BRIDGE
DEFAULT_SIMULATOR_ROOT = smoke_v2.DEFAULT_SIMULATOR_ROOT
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_prototype_wire_smoke"
    / "v3"
    / "receipt.json"
)


CLAIM_BOUNDARY_V3: JSONMap = {
    "development_causal_wire_smoke_only": True,
    "source_bound_hypothesis_preparation_reused": True,
    "exact_prefix_health_registry_required": True,
    "dynamic_health_hypothesis_accepted_as_exact_prefix_health": False,
    "value_ready": False,
    "policy_value_authorized": False,
    "comparison_authorized": False,
    "training_authorized": False,
    "hpc_authorized": False,
    "deployment_authorized": False,
    "superiority_claim_authorized": False,
}


class HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(RuntimeError):
    """The explicit prefix contract or causal native smoke did not close."""


@dataclass(frozen=True)
class PreparedPrefixCheckpointBindingV3:
    manifest: JSONMap
    row: JSONMap
    target_introduction_registry: TargetIntroductionRegistryV1
    target_health_prefix_registry: TargetHealthPrefixRegistryV1
    binding: JSONMap


def _native_bridge_evidence() -> JSONMap:
    return {
        "schema": clone_v2.BRIDGE_RUNTIME_EVIDENCE_SCHEMA,
        "runtime_kind": "NATIVE_SUBPROCESS_BRIDGE",
        "load_method_invoked": "load_dynamic_v3",
        "native_subprocess_bridge": True,
        "python_simulated_bridge": False,
        "evidence_scope": "NATIVE_SUBPROCESS_SMOKE_ONLY",
        "comparison_authorized": False,
    }


def _exact_health_value(field: Mapping[str, Any], label: str) -> float:
    value = field.get("value")
    diagnostics = field.get("diagnostics")
    if (
        field.get("observation_category") != "EXACT"
        or field.get("exact_checkpoint_equivalent") is not True
        or field.get("future_suffix_used") is not False
        or field.get("default_value_used") is not False
        or field.get("source") != "chronicle.external.core.strict_prefix"
        or not isinstance(diagnostics, Mapping)
        or diagnostics.get("retrospective_kill_budget_materialized") is not False
        or diagnostics.get("same_entry_health_prior_materialized") is not False
        or diagnostics.get("post_cutoff_damage_or_healing_materialized") is not False
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            f"{label} is not exact strict-prefix checkpoint evidence"
        )
    result = float(value)
    if (
        not math.isfinite(result)
        or result < 0
        or (label == "target.max_health" and result <= 0)
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            f"{label} has an invalid exact checkpoint value"
        )
    return result


def _derive_prefix_checkpoint_binding_v3(
    prepared: smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2,
    manifest: Mapping[str, Any],
) -> PreparedPrefixCheckpointBindingV3:
    try:
        checked_manifest = checkpoint_v1.validate_manifest(manifest)
    except checkpoint_v1.HistoricalFuryPrefixCheckpointV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            f"prefix checkpoint manifest is invalid: {error}"
        ) from error
    if checked_manifest.get("implementation_revision") != (
        checkpoint_v1.IMPLEMENTATION_REVISION
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prefix checkpoint implementation revision is not the prepared v1 contract"
        )
    rows = [
        row
        for row in checked_manifest.get("rows", [])
        if isinstance(row, Mapping)
        and row.get("segment_ref") == prepared.hypothesis_row.get("segment_ref")
    ]
    if len(rows) != 1:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prefix checkpoint does not contain exactly one prepared segment row"
        )
    row = deepcopy(dict(rows[0]))
    target_count = len(prepared.prepared_v1.dynamic_config.target_health)
    if target_count != 1:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "checkpoint v1 has no per-target exact-health vector for this prepared wave"
        )
    fields = row.get("fields")
    if not isinstance(fields, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prefix checkpoint row has no field evidence"
        )
    maximum = _exact_health_value(
        fields.get("target.max_health", {}), "target.max_health"
    )
    current = _exact_health_value(
        fields.get("target.current_health", {}), "target.current_health"
    )
    if current > maximum:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "exact current health exceeds exact maximum health"
        )
    hypotheses = smoke_v2._mapping(
        prepared.hypothesis_row.get("hypotheses"), "row hypotheses"
    )
    registry = smoke_v2._mapping(
        hypotheses.get("target_registry"), "target registry hypothesis"
    )
    selected = smoke_v2._array(
        registry.get("selected_simulator_target_guids"),
        "selected simulator target GUIDs",
    )
    initial = smoke_v2._array(
        registry.get("initial_alive_target_guids"),
        "initial alive target GUIDs",
    )
    window = row.get("window_start")
    if not isinstance(window, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prefix checkpoint row has no window-start evidence"
        )
    alive = window.get("alive_target_guids")
    if not isinstance(alive, list):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prefix checkpoint has no alive-target registry"
        )
    selected_folded = {
        guid.casefold() for guid in selected if isinstance(guid, str)
    }
    alive_folded = {
        guid.casefold() for guid in alive if isinstance(guid, str)
    }
    diagnostics_match = True
    for field_name in ("target.max_health", "target.current_health"):
        diagnostics = fields[field_name].get("diagnostics")
        diagnostic_guids = (
            diagnostics.get("target_guids_at_cutoff")
            if isinstance(diagnostics, Mapping)
            else None
        )
        diagnostics_match = diagnostics_match and isinstance(
            diagnostic_guids, list
        ) and {
            guid.casefold()
            for guid in diagnostic_guids
            if isinstance(guid, str)
        } == selected_folded
    if (
        len(selected) != 1
        or not selected
        or any(
            not isinstance(guid, str)
            or guid.casefold() not in {str(value).casefold() for value in initial}
            for guid in selected
        )
        or selected_folded != alive_folded
        or not diagnostics_match
        or registry.get("future_target_backfill_used") is not False
        or registry.get("future_registry_leak_or_unstable_origin") is not False
        or window.get("semantics") != "STATE_STRICTLY_BEFORE_FIRST_BOUND_DECISION"
        or row.get("strict_prefix_input", {}).get("future_suffix_used") is not False
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prepared target registry and checkpoint prefix do not identify the same target"
        )
    introductions = TargetIntroductionRegistryV1(
        targets=(TargetIntroductionV1(0, 0),)
    )
    health = TargetHealthPrefixRegistryV1(
        targets=(
            TargetHealthPrefixBaselineV1(
                0,
                0,
                current,
                maximum,
                0.0,
                0.0,
            ),
        )
    )
    address = checked_manifest.get("content_address")
    if not isinstance(address, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prefix checkpoint manifest has no content address"
        )
    binding = {
        "schema": "historical_fury_prefix_checkpoint_binding/v3",
        "manifest": {
            "schema": checked_manifest.get("schema"),
            "implementation_revision": checked_manifest.get(
                "implementation_revision"
            ),
            "content_sha256": address.get("sha256"),
        },
        "selected_row": {
            "segment_ref": row.get("segment_ref"),
            "source_identity": deepcopy(row.get("source_identity")),
            "window_start": deepcopy(window),
            "strict_prefix_input": deepcopy(row.get("strict_prefix_input")),
            "target_max_health": deepcopy(fields["target.max_health"]),
            "target_current_health": deepcopy(fields["target.current_health"]),
        },
        "dynamic_health_hypothesis_consumed_as_prefix_observation": False,
        "target_current_and_maximum_health_separate": True,
    }
    return PreparedPrefixCheckpointBindingV3(
        manifest=deepcopy(checked_manifest),
        row=row,
        target_introduction_registry=introductions,
        target_health_prefix_registry=health,
        binding=binding,
    )


def prepare_prefix_checkpoint_binding_v3(
    prepared: smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2,
    checkpoint_manifest_path: str | Path = DEFAULT_PREFIX_CHECKPOINT,
) -> PreparedPrefixCheckpointBindingV3:
    if not isinstance(prepared, smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2):
        raise TypeError("prepared must be PreparedSourceBoundPrototypeWireSmokeV2")
    try:
        source = smoke_v1._resolve_file(
            checkpoint_manifest_path, "prefix checkpoint manifest"
        )
        document, _ = smoke_v1._strict_json_file(
            source, "prefix checkpoint manifest"
        )
    except smoke_v1.HistoricalFurySourceBoundPrototypeSmokeRunnerV1Error as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(str(error)) from error
    return _derive_prefix_checkpoint_binding_v3(prepared, document)


def _validate_prepared_checkpoint(
    prepared: smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2,
    checkpoint: PreparedPrefixCheckpointBindingV3,
) -> PreparedPrefixCheckpointBindingV3:
    if not isinstance(checkpoint, PreparedPrefixCheckpointBindingV3):
        raise TypeError("checkpoint must be PreparedPrefixCheckpointBindingV3")
    expected = _derive_prefix_checkpoint_binding_v3(prepared, checkpoint.manifest)
    if checkpoint != expected:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "prepared checkpoint binding differs from its validated manifest"
        )
    return expected


def _registry_binding(
    checkpoint: PreparedPrefixCheckpointBindingV3,
) -> JSONMap:
    return {
        "checkpoint_evidence": deepcopy(checkpoint.binding),
        "target_introduction_registry": (
            checkpoint.target_introduction_registry.to_wire()
        ),
        "target_health_prefix_registry": (
            checkpoint.target_health_prefix_registry.to_wire()
        ),
        "target_current_and_maximum_health_separated": True,
        "future_target_rows_visible_to_policy": False,
        "raw_simulator_health_visible_to_policy": False,
        "dynamic_health_hypothesis_consumed_as_prefix_observation": False,
    }


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    payload = deepcopy(dict(core))
    payload.pop("content_address", None)
    return {
        **payload,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": smoke_v2._sha256_json(payload),
        },
    }


def _build_receipt(
    prepared: smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2,
    *,
    bridge_identity: Mapping[str, Any],
    rollout: Mapping[str, Any],
    checkpoint: PreparedPrefixCheckpointBindingV3,
) -> JSONMap:
    inner = rollout.get("inner_rollout_v2")
    complete = (
        rollout.get("status") == clone_v2.STATUS
        and isinstance(inner, Mapping)
        and inner.get("scenario_complete") is True
        and smoke_v2._mapping(
            inner.get("dynamic_v3_runtime_receipt_closure"),
            "runtime receipt closure",
        ).get("status")
        == "COMPLETE_BOUND"
    )
    row = prepared.hypothesis_row
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
            "segment_ref": row.get("segment_ref"),
            "prototype_id": rollout.get("prototype_id"),
            "policy_id": rollout.get("policy_id"),
            "seed": prepared.prepared_v1.seed,
            "declared_seed": True,
            "source_v2_row_status": row.get("status"),
        },
        "input_bindings": smoke_v2._outer_input_bindings(prepared),
        "prefix_registry_binding": _registry_binding(checkpoint),
        "bridge_identity": deepcopy(dict(bridge_identity)),
        "rollout_v3": deepcopy(dict(rollout)),
        "claim_boundary": deepcopy(CLAIM_BOUNDARY_V3),
    }
    return _content_addressed(core)


def validate_source_bound_prototype_wire_smoke_receipt_v3(
    value: Mapping[str, Any],
    *,
    prepared: smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2,
    checkpoint: PreparedPrefixCheckpointBindingV3,
) -> JSONMap:
    """Validate the causal outer receipt and its retained rollout-v3."""

    if not isinstance(prepared, smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2):
        raise TypeError("prepared must be PreparedSourceBoundPrototypeWireSmokeV2")
    checked_checkpoint = _validate_prepared_checkpoint(prepared, checkpoint)
    try:
        raw = json.loads(smoke_v2._canonical_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            f"v3 receipt is not strict JSON: {error}"
        ) from error
    core = deepcopy(raw)
    address = core.pop("content_address", None)
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != KIND
        or address != _content_addressed(core)["content_address"]
    ):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 wire-smoke receipt identity or content address differs"
        )
    if raw.get("claim_boundary") != CLAIM_BOUNDARY_V3:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 wire-smoke claim boundary differs"
        )
    if raw.get("execution_scope") != {
        "local_windows_native": True,
        "single_process": True,
        "hpc_dispatch_performed": False,
        "network_request_count": 0,
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 wire smoke exceeded its local single-process scope"
        )
    if raw.get("input_bindings") != smoke_v2._outer_input_bindings(prepared):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 hypothesis preparation binding differs"
        )
    expected_registry = _registry_binding(checked_checkpoint)
    if raw.get("prefix_registry_binding") != expected_registry:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 explicit prefix registry binding differs"
        )
    bridge_identity = raw.get("bridge_identity")
    if not isinstance(bridge_identity, Mapping) or dict(bridge_identity) != {
        "filename": smoke_v1.DEFAULT_WINDOWS_V11_BRIDGE.name,
        "sha256": smoke_v1.v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SHA256,
        "size_bytes": smoke_v1.v11_contract.EXPECTED_WINDOWS_V11_BRIDGE_SIZE,
        "platform": "windows-amd64",
    }:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 bridge identity is not the pinned Windows v11 binary"
        )
    rollout = raw.get("rollout_v3")
    if not isinstance(rollout, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 retained rollout is missing"
        )
    inner = rollout.get("inner_rollout_v2")
    if not isinstance(inner, Mapping):
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 retained rollout lacks clone-v2 evidence"
        )
    expected_model = clone_v2._model_binding(
        prepared.prepared_v1.model,
        expected_model_binding=prepared.prepared_v1.exact_model_receipt,
    )
    try:
        checked = clone_v3.validate_behavior_clone_dynamic_v5_rollout_v3(
            rollout,
            prepared.prepared_v1.dynamic_load,
            target_introduction_registry=(
                checked_checkpoint.target_introduction_registry
            ),
            target_health_prefix_registry=(
                checked_checkpoint.target_health_prefix_registry
            ),
            expected_model_binding=expected_model,
            expected_exact_model_validation_receipt=(
                prepared.prepared_v1.exact_model_receipt
            ),
            expected_bridge_runtime_evidence=_native_bridge_evidence(),
            loaded_receipt=DynamicLoadReceiptV3(
                **inner["dynamic_load_binding"]["bridge_receipt"]
            ),
        )
    except (
        KeyError,
        TypeError,
        clone_v3.HistoricalBehaviorCloneFullRolloutV3Error,
    ) as error:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            f"retained causal rollout is invalid: {error}"
        ) from error
    row = prepared.hypothesis_row
    expected_selection = {
        "segment_ref": row.get("segment_ref"),
        "prototype_id": checked.get("prototype_id"),
        "policy_id": checked.get("policy_id"),
        "seed": prepared.prepared_v1.seed,
        "declared_seed": True,
        "source_v2_row_status": row.get("status"),
    }
    if raw.get("selection") != expected_selection:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 selection differs from its retained causal rollout"
        )
    complete = (
        checked.get("status") == clone_v2.STATUS
        and inner.get("scenario_complete") is True
        and inner["dynamic_v3_runtime_receipt_closure"].get("status")
        == "COMPLETE_BOUND"
    )
    expected_status = COMPLETE_STATUS if complete else INCOMPLETE_STATUS
    if raw.get("status") != expected_status:
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            "v3 status differs from retained causal rollout closure"
        )
    return raw


def run_prepared_local_native_source_bound_prototype_wire_smoke_v3(
    prepared: smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2,
    *,
    checkpoint: PreparedPrefixCheckpointBindingV3,
    bridge_path: str | Path = DEFAULT_WINDOWS_V11_BRIDGE,
    simulator_root: str | Path = DEFAULT_SIMULATOR_ROOT,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    """Execute one prepared causal smoke through the local native bridge."""

    if not isinstance(prepared, smoke_v2.PreparedSourceBoundPrototypeWireSmokeV2):
        raise TypeError("prepared must be PreparedSourceBoundPrototypeWireSmokeV2")
    checked_checkpoint = _validate_prepared_checkpoint(prepared, checkpoint)
    bridge_identity = smoke_v1._verify_windows_v11_bridge_v1(bridge_path)
    cwd = Path(simulator_root).expanduser().resolve()
    if not cwd.is_dir():
        raise HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error(
            f"simulator root does not exist: {cwd}"
        )
    with SimulatorBridgeDynamicV3(
        Path(bridge_path).expanduser().resolve(),
        cwd=cwd,
        environment={"GOMAXPROCS": "1"},
    ) as bridge:
        rollout = clone_v3.run_behavior_clone_dynamic_v5_rollout_v3(
            bridge,
            prepared.prepared_v1.raid_sim_request,
            model=prepared.prepared_v1.model,
            expected_model_binding=prepared.prepared_v1.exact_model_receipt,
            seed=prepared.prepared_v1.seed,
            dynamic_load=prepared.prepared_v1.dynamic_load,
            target_introduction_registry=(
                checked_checkpoint.target_introduction_registry
            ),
            target_health_prefix_registry=(
                checked_checkpoint.target_health_prefix_registry
            ),
            max_decisions=smoke_v2._integer(
                max_decisions, "max_decisions", minimum=1
            ),
            max_advances=smoke_v2._integer(
                max_advances, "max_advances", minimum=1
            ),
            retain_steps=True,
        )
    receipt = _build_receipt(
        prepared,
        bridge_identity=bridge_identity,
        rollout=rollout,
        checkpoint=checked_checkpoint,
    )
    return validate_source_bound_prototype_wire_smoke_receipt_v3(
        receipt,
        prepared=prepared,
        checkpoint=checked_checkpoint,
    )


def run_local_native_source_bound_prototype_wire_smoke_v3(
    *,
    bundle_path: str | Path,
    model_manifest_path: str | Path,
    dynamic_hypothesis_path: str | Path,
    segment_ref: str,
    prototype_id: str,
    seed: int,
    checkpoint_manifest_path: str | Path = DEFAULT_PREFIX_CHECKPOINT,
    bridge_path: str | Path = DEFAULT_WINDOWS_V11_BRIDGE,
    simulator_root: str | Path = DEFAULT_SIMULATOR_ROOT,
    max_decisions: int = 10_000,
    max_advances: int = 100_000,
) -> JSONMap:
    prepared = smoke_v2.prepare_source_bound_prototype_wire_smoke_v2(
        bundle_path=bundle_path,
        model_manifest_path=model_manifest_path,
        dynamic_hypothesis_path=dynamic_hypothesis_path,
        segment_ref=segment_ref,
        prototype_id=prototype_id,
        seed=seed,
    )
    checkpoint = prepare_prefix_checkpoint_binding_v3(
        prepared, checkpoint_manifest_path
    )
    return run_prepared_local_native_source_bound_prototype_wire_smoke_v3(
        prepared,
        checkpoint=checkpoint,
        bridge_path=bridge_path,
        simulator_root=simulator_root,
        max_decisions=max_decisions,
        max_advances=max_advances,
    )


def publish_local_native_source_bound_prototype_wire_smoke_v3(
    *, output_path: str | Path, **kwargs: Any
) -> JSONMap:
    receipt = run_local_native_source_bound_prototype_wire_smoke_v3(**kwargs)
    destination = Path(output_path).expanduser().resolve()
    smoke_v1._atomic_write(
        destination, smoke_v2._canonical_bytes(receipt, newline=True)
    )
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
        description="Run one causal source-bound development wire smoke"
    )
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE_MANIFEST))
    parser.add_argument("--model-manifest", default=str(DEFAULT_MODEL_MANIFEST))
    parser.add_argument(
        "--dynamic-hypothesis", default=str(DEFAULT_DYNAMIC_HYPOTHESIS)
    )
    parser.add_argument("--segment-ref", required=True)
    parser.add_argument("--prototype-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--prefix-checkpoint", default=str(DEFAULT_PREFIX_CHECKPOINT)
    )
    parser.add_argument("--bridge", default=str(DEFAULT_WINDOWS_V11_BRIDGE))
    parser.add_argument("--simulator-root", default=str(DEFAULT_SIMULATOR_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--max-decisions", type=int, default=10_000)
    parser.add_argument("--max-advances", type=int, default=100_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = publish_local_native_source_bound_prototype_wire_smoke_v3(
        output_path=args.output,
        bundle_path=args.bundle,
        model_manifest_path=args.model_manifest,
        dynamic_hypothesis_path=args.dynamic_hypothesis,
        segment_ref=args.segment_ref,
        prototype_id=args.prototype_id,
        seed=args.seed,
        checkpoint_manifest_path=args.prefix_checkpoint,
        bridge_path=args.bridge,
        simulator_root=args.simulator_root,
        max_decisions=args.max_decisions,
        max_advances=args.max_advances,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__: Sequence[str] = (
    "CLAIM_BOUNDARY_V3",
    "COMPLETE_STATUS",
    "DEFAULT_PREFIX_CHECKPOINT",
    "DEFAULT_OUTPUT",
    "HistoricalFurySourceBoundPrototypeSmokeRunnerV3Error",
    "IMPLEMENTATION_REVISION",
    "INCOMPLETE_STATUS",
    "KIND",
    "PreparedPrefixCheckpointBindingV3",
    "SCHEMA",
    "main",
    "publish_local_native_source_bound_prototype_wire_smoke_v3",
    "prepare_prefix_checkpoint_binding_v3",
    "run_local_native_source_bound_prototype_wire_smoke_v3",
    "run_prepared_local_native_source_bound_prototype_wire_smoke_v3",
    "validate_source_bound_prototype_wire_smoke_receipt_v3",
)


if __name__ == "__main__":
    raise SystemExit(main())
