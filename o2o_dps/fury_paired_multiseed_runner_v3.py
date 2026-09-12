"""Four-baseline admission profile over the audited paired runner v2.

The compact rollout, shard, and reduction wire formats stay at v2.  Version 3
adds a content-addressed plan envelope which requires four baseline policies
and a complete External-V2 historical-policy artifact admission receipt before
the immutable v2 transport plan can be built.  The receipt is explicitly
non-promoting by itself; statistical victory still requires the independent
protocol baseline-readiness and corpus gates.
"""

from __future__ import annotations

import copy
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping

from . import fury_paired_multiseed_runner_v2 as _v2


JSONMap = dict[str, Any]
PLAN_KIND = "fury_paired_multiseed_runner_plan_v3"
PLAN_CONTRACT_KIND = "fury_paired_multiseed_runner_contract_v3"
HISTORICAL_ARTIFACT_ADMISSION_SCHEMA = (
    "fury_historical_external_v2_artifact_admission_receipt/v1"
)
HISTORICAL_POLICY_ID = (
    "chronicle.external_v2.fury.historical_player_policy"
)
REQUIRED_BASELINE_IDS = (
    "cat.fury.profile1",
    "contra.deployed.fury.raid_a",
    "contra260817.fury.source_candidate",
    HISTORICAL_POLICY_ID,
)
REQUIRED_STRATA = ("overall", "single_target", "multi_target")
HISTORICAL_REQUIRED_CONDITIONS = (
    "external_v2_candidate_mask_exactly_bound",
    "prefix_causality_verified",
    "policy_model_content_addressed",
    "full_scenario_adapter_verified",
    "ordered_execution_fidelity_closed",
    "paired_rollout_identity_closed",
)
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

FuryPairedRunnerError = _v2.FuryPairedRunnerError
ValidatedShard = _v2.ValidatedShard
COMPARISON_INTENT = _v2.COMPARISON_INTENT
DIAGNOSTIC_INTENT = _v2.DIAGNOSTIC_INTENT
SINGLE_BRIDGE_MODE = _v2.SINGLE_BRIDGE_MODE
SYNTHETIC_MODE = _v2.SYNTHETIC_MODE
ROLLOUT_KIND = _v2.ROLLOUT_KIND
SHARD_MANIFEST_KIND = _v2.SHARD_MANIFEST_KIND
REDUCTION_KIND = _v2.REDUCTION_KIND
REDUCTION_RECEIPT_KIND = _v2.REDUCTION_RECEIPT_KIND
SEED_DERIVATION_ALGORITHM = _v2.SEED_DERIVATION_ALGORITHM
SCENARIO_MODEL_KIND = _v2.SCENARIO_MODEL_KIND
TARGET_CONTEXT_BUNDLE_KIND = _v2.TARGET_CONTEXT_BUNDLE_KIND
EXACT_STATIC_REQUEST_SEMANTICS_SCHEMA = (
    _v2.EXACT_STATIC_REQUEST_SEMANTICS_SCHEMA
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryPairedRunnerError(f"{label} must be an object")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FuryPairedRunnerError(f"{label} must be a lowercase SHA-256")
    return value


def validate_historical_artifact_admission_receipt(
    receipt: Mapping[str, Any] | None,
) -> JSONMap:
    """Validate artifact closure without treating it as comparison authority."""

    if receipt is None:
        raise FuryPairedRunnerError(
            "historical External-V2 artifact admission receipt is required"
        )
    value = copy.deepcopy(dict(_mapping(receipt, "historical artifact receipt")))
    expected_fields = {
        "schema",
        "policy_id",
        "artifact_manifest_sha256",
        "source_bundle_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
        "cohort_receipt_sha256",
        "team_wave_model_manifest_sha256",
        "policy_model_sha256",
        "prefix_causality_receipt_sha256",
        "full_scenario_adapter_sha256",
        "ordered_execution_fidelity_sha256",
        "satisfied_conditions",
        "artifact_ready",
        "readiness_conditions_satisfied",
        "comparison_ready_by_itself",
        "blockers",
        "receipt_sha256",
    }
    if set(value) != expected_fields:
        raise FuryPairedRunnerError(
            "historical External-V2 artifact receipt field set mismatch"
        )
    if value.get("schema") != HISTORICAL_ARTIFACT_ADMISSION_SCHEMA:
        raise FuryPairedRunnerError("historical artifact receipt schema mismatch")
    if value.get("policy_id") != HISTORICAL_POLICY_ID:
        raise FuryPairedRunnerError("historical artifact receipt policy mismatch")
    for field in (
        "artifact_manifest_sha256",
        "source_bundle_sha256",
        "policy_adapter_sha256",
        "policy_profile_sha256",
        "cohort_receipt_sha256",
        "team_wave_model_manifest_sha256",
        "policy_model_sha256",
        "prefix_causality_receipt_sha256",
        "full_scenario_adapter_sha256",
        "ordered_execution_fidelity_sha256",
    ):
        _sha256(value.get(field), f"historical artifact receipt {field}")
    if value.get("satisfied_conditions") != list(HISTORICAL_REQUIRED_CONDITIONS):
        raise FuryPairedRunnerError(
            "historical artifact receipt does not satisfy every fixed condition"
        )
    if (
        value.get("artifact_ready") is not True
        or value.get("readiness_conditions_satisfied") is not True
        or value.get("comparison_ready_by_itself") is not False
        or value.get("blockers") != []
    ):
        raise FuryPairedRunnerError(
            "historical artifact receipt is incomplete or self-promoting"
        )
    supplied = _sha256(value.get("receipt_sha256"), "historical receipt_sha256")
    unsigned = dict(value)
    unsigned.pop("receipt_sha256")
    if supplied != _v2.sha256_json(unsigned):
        raise FuryPairedRunnerError("historical artifact receipt SHA-256 mismatch")
    return value


def _validate_policy_membership(
    policies: Iterable[Mapping[str, Any]],
    receipt: Mapping[str, Any],
) -> tuple[JSONMap, ...]:
    buffered = tuple(copy.deepcopy(dict(_mapping(value, "policy"))) for value in policies)
    if len(buffered) != len(REQUIRED_BASELINE_IDS) + 1:
        raise FuryPairedRunnerError(
            "v3 runner requires exactly four baselines and one candidate"
        )
    observed_baselines = tuple(
        value.get("policy_id") for value in buffered if value.get("role") == "BASELINE"
    )
    if observed_baselines != REQUIRED_BASELINE_IDS:
        raise FuryPairedRunnerError(
            "v3 baseline order must be Cat, deployed Contra, Contra260817, historical External-V2"
        )
    candidates = [value for value in buffered if value.get("role") == "CANDIDATE"]
    if len(candidates) != 1 or buffered[-1].get("role") != "CANDIDATE":
        raise FuryPairedRunnerError("v3 runner requires one final candidate policy")
    historical = buffered[len(REQUIRED_BASELINE_IDS) - 1]
    expected = {
        "source_sha256": receipt["source_bundle_sha256"],
        "adapter_sha256": receipt["policy_adapter_sha256"],
        "profile_sha256": receipt["policy_profile_sha256"],
    }
    for field, digest in expected.items():
        if historical.get(field) != digest:
            raise FuryPairedRunnerError(
                f"historical policy {field} differs from its artifact receipt"
            )
    return buffered


def build_runner_plan(
    *,
    historical_artifact_admission_receipt: Mapping[str, Any] | None,
    **kwargs: Any,
) -> JSONMap:
    """Build a v3 admission envelope around one deterministic v2 runner plan."""

    receipt = validate_historical_artifact_admission_receipt(
        historical_artifact_admission_receipt
    )
    if "policies" not in kwargs:
        raise FuryPairedRunnerError("policies are required")
    normalized_policies = _validate_policy_membership(kwargs["policies"], receipt)
    registered_full_policy_adapters = getattr(
        _v2, "_POLICY_TO_FULL_ROLLOUT_EXPERT_ID", {}
    )
    if (
        kwargs.get("execution_mode") == SINGLE_BRIDGE_MODE
        and HISTORICAL_POLICY_ID not in registered_full_policy_adapters
    ):
        raise FuryPairedRunnerError(
            "historical External-V2 full-policy executor adapter is not registered; "
            "production dispatch remains blocked"
        )
    delegate_kwargs = dict(kwargs)
    delegate_kwargs["policies"] = normalized_policies
    delegate = _v2.build_runner_plan(**delegate_kwargs)
    contract: JSONMap = {
        "schema_version": 3,
        "kind": PLAN_CONTRACT_KIND,
        "profile_revision": 3,
        "required_baseline_ids": list(REQUIRED_BASELINE_IDS),
        "required_strata": list(REQUIRED_STRATA),
        "required_baseline_stratum_cell_count": 12,
        "historical_artifact_admission_receipt": receipt,
        "historical_artifact_admission_receipt_sha256": receipt["receipt_sha256"],
        "delegate_transport_schema_version": 2,
        "delegate_transport_kind": _v2.PLAN_KIND,
        "delegate_plan_sha256": delegate["plan_sha256"],
        "delegate_plan": delegate,
        "comparison_authority_inherited_from_artifact_receipt": False,
        "statistical_and_corpus_gates_still_required": True,
    }
    return {
        "schema_version": 3,
        "kind": PLAN_KIND,
        "generated_at": delegate["generated_at"],
        "plan_sha256": _v2.sha256_json(contract),
        "contract": contract,
        "execution_started": False,
        "victory_claim_allowed": False,
    }


def validate_runner_plan(plan: Mapping[str, Any]) -> JSONMap:
    value = copy.deepcopy(dict(_mapping(plan, "v3 runner plan")))
    if set(value) != {
        "schema_version",
        "kind",
        "generated_at",
        "plan_sha256",
        "contract",
        "execution_started",
        "victory_claim_allowed",
    }:
        raise FuryPairedRunnerError("v3 runner plan field set mismatch")
    if value.get("schema_version") != 3 or value.get("kind") != PLAN_KIND:
        raise FuryPairedRunnerError("invalid v3 runner plan schema or kind")
    contract = _mapping(value.get("contract"), "v3 runner plan contract")
    expected_contract_fields = {
        "schema_version",
        "kind",
        "profile_revision",
        "required_baseline_ids",
        "required_strata",
        "required_baseline_stratum_cell_count",
        "historical_artifact_admission_receipt",
        "historical_artifact_admission_receipt_sha256",
        "delegate_transport_schema_version",
        "delegate_transport_kind",
        "delegate_plan_sha256",
        "delegate_plan",
        "comparison_authority_inherited_from_artifact_receipt",
        "statistical_and_corpus_gates_still_required",
    }
    if set(contract) != expected_contract_fields:
        raise FuryPairedRunnerError("v3 runner plan contract field set mismatch")
    if (
        contract.get("schema_version") != 3
        or contract.get("kind") != PLAN_CONTRACT_KIND
        or contract.get("profile_revision") != 3
        or contract.get("required_baseline_ids") != list(REQUIRED_BASELINE_IDS)
        or contract.get("required_strata") != list(REQUIRED_STRATA)
        or contract.get("required_baseline_stratum_cell_count") != 12
        or contract.get("delegate_transport_schema_version") != 2
        or contract.get("delegate_transport_kind") != _v2.PLAN_KIND
        or contract.get("comparison_authority_inherited_from_artifact_receipt") is not False
        or contract.get("statistical_and_corpus_gates_still_required") is not True
    ):
        raise FuryPairedRunnerError("v3 runner profile contract mismatch")
    receipt = validate_historical_artifact_admission_receipt(
        _mapping(
            contract.get("historical_artifact_admission_receipt"),
            "historical artifact receipt",
        )
    )
    if contract.get("historical_artifact_admission_receipt_sha256") != receipt[
        "receipt_sha256"
    ]:
        raise FuryPairedRunnerError("v3 plan historical receipt binding mismatch")
    delegate = _v2.validate_runner_plan(
        _mapping(contract.get("delegate_plan"), "delegate_plan")
    )
    if (
        contract.get("delegate_plan_sha256") != delegate["plan_sha256"]
        or value.get("plan_sha256") != _v2.sha256_json(dict(contract))
    ):
        raise FuryPairedRunnerError("v3 plan content address mismatch")
    _validate_policy_membership(delegate["contract"]["policies"], receipt)
    if value.get("execution_started") is not False or value.get(
        "victory_claim_allowed"
    ) is not False:
        raise FuryPairedRunnerError("a runner plan cannot claim execution or victory")
    return value


def unwrap_runner_plan(plan: Mapping[str, Any]) -> JSONMap:
    validated = validate_runner_plan(plan)
    return copy.deepcopy(dict(validated["contract"]["delegate_plan"]))


def execute_shard(
    plan: Mapping[str, Any],
    shard_index: int,
    executor: Callable[..., Mapping[str, Any]],
    output_directory: str | Path,
    **kwargs: Any,
) -> Path:
    return _v2.execute_shard(
        unwrap_runner_plan(plan),
        shard_index,
        executor,
        output_directory,
        **kwargs,
    )


def validate_shard(plan: Mapping[str, Any], manifest_path: str | Path) -> ValidatedShard:
    return _v2.validate_shard(unwrap_runner_plan(plan), manifest_path)


def validate_rollout_rows(
    plan: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]
) -> tuple[JSONMap, ...]:
    return _v2.validate_rollout_rows(unwrap_runner_plan(plan), rows)


def build_reduction_receipt(
    plan: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    manifest_paths: Iterable[str | Path] | None = None,
) -> JSONMap:
    return _v2.build_reduction_receipt(
        unwrap_runner_plan(plan), rows, manifest_paths=manifest_paths
    )


def validate_reduction_receipt(
    plan: Mapping[str, Any],
    receipt: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    manifest_paths: Iterable[str | Path] | None = None,
) -> tuple[JSONMap, ...]:
    return _v2.validate_reduction_receipt(
        unwrap_runner_plan(plan),
        receipt,
        rows,
        manifest_paths=manifest_paths,
    )


def reduce_shards(
    plan: Mapping[str, Any], manifest_paths: Iterable[str | Path]
) -> JSONMap:
    return _v2.reduce_shards(unwrap_runner_plan(plan), manifest_paths)


# Transport algorithms remain byte-for-byte v2 and are re-exported explicitly.
canonical_json_bytes = _v2.canonical_json_bytes
sha256_json = _v2.sha256_json
derive_simulator_seed = _v2.derive_simulator_seed
normalize_runner_scenarios = _v2.normalize_runner_scenarios
runner_scenario_bundle_sha256 = _v2.runner_scenario_bundle_sha256
runner_scenario_model_bundle_sha256 = _v2.runner_scenario_model_bundle_sha256
runner_target_context_bundle_set_sha256 = (
    _v2.runner_target_context_bundle_set_sha256
)
build_exact_static_request_semantics_receipt = (
    _v2.build_exact_static_request_semantics_receipt
)


__all__ = (
    "COMPARISON_INTENT",
    "DIAGNOSTIC_INTENT",
    "EXACT_STATIC_REQUEST_SEMANTICS_SCHEMA",
    "FuryPairedRunnerError",
    "HISTORICAL_ARTIFACT_ADMISSION_SCHEMA",
    "HISTORICAL_POLICY_ID",
    "HISTORICAL_REQUIRED_CONDITIONS",
    "PLAN_CONTRACT_KIND",
    "PLAN_KIND",
    "REDUCTION_KIND",
    "REDUCTION_RECEIPT_KIND",
    "REQUIRED_BASELINE_IDS",
    "REQUIRED_STRATA",
    "ROLLOUT_KIND",
    "SCENARIO_MODEL_KIND",
    "SEED_DERIVATION_ALGORITHM",
    "SHARD_MANIFEST_KIND",
    "SINGLE_BRIDGE_MODE",
    "SYNTHETIC_MODE",
    "TARGET_CONTEXT_BUNDLE_KIND",
    "ValidatedShard",
    "build_exact_static_request_semantics_receipt",
    "build_reduction_receipt",
    "build_runner_plan",
    "canonical_json_bytes",
    "derive_simulator_seed",
    "execute_shard",
    "normalize_runner_scenarios",
    "reduce_shards",
    "runner_scenario_bundle_sha256",
    "runner_scenario_model_bundle_sha256",
    "runner_target_context_bundle_set_sha256",
    "sha256_json",
    "unwrap_runner_plan",
    "validate_historical_artifact_admission_receipt",
    "validate_reduction_receipt",
    "validate_rollout_rows",
    "validate_runner_plan",
    "validate_shard",
)
