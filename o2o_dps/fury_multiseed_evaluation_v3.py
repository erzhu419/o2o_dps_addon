"""Four-baseline Fury multiseed protocol admission and evaluation profile.

Version 3 deliberately reuses the audited v2 statistical engine and compact
rollout wire format.  Its new authority is narrower: it fixes the required
baseline set to Cat, deployed Contra, Contra260817, and a prefix-causal
Chronicle External-V2 historical Fury policy.  The fourth lane is mandatory;
an absent artifact or readiness receipt blocks dispatch and analysis.

The published v3 protocol is preparatory.  It does not claim that the
historical lane, any addon baseline, a candidate, or a future confirmation
corpus is ready, and this module never launches simulator work.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import types
from typing import Any, Iterable, Mapping, Sequence

from . import fury_multiseed_evaluation_v2 as _v2
from . import fury_paired_multiseed_runner_v3 as _runner_v3


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = (
    PROJECT_ROOT / "configs" / "evaluation" / "fury_multiseed_protocol_v3.json"
)
DEFAULT_PLAN = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_multiseed_protocol_v3.plan.json"
)
DEFAULT_ANALYSIS = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_multiseed_evaluation_v3.json"
)

PROTOCOL_WIRE_KIND = _v2.PROTOCOL_KIND
PROTOCOL_REVISION = 3
EXECUTION_SOURCE_IDENTITY_SCHEMA = "fury_execution_source_identity/v3"
V3_REQUIRED_PRODUCTION_PATHS = (
    "o2o_dps/fury_dynamic_target_semantics_v3.py",
    "o2o_dps/fury_full_policy_rollout_v3.py",
    "o2o_dps/fury_multiseed_evaluation_v3.py",
    "o2o_dps/fury_paired_multiseed_runner_v3.py",
)
PLAN_KIND = "fury_multiseed_evaluation_plan_v3"
ANALYSIS_KIND = "fury_multiseed_evaluation_v3"
HISTORICAL_ARTIFACT_SCHEMA = (
    "chronicle_external_v2_historical_fury_baseline_artifact/v1"
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
REQUIRED_BASELINE_STRATUM_CELL_COUNT = 12
MISSING_HISTORICAL_SOURCE_SENTINEL_SHA256 = (
    "5a086da6b72de003bea363cf1295df5bff63654f01065eb98623da49ca85b219"
)
HISTORICAL_REQUIRED_CONDITIONS = (
    "external_v2_candidate_mask_exactly_bound",
    "prefix_causality_verified",
    "policy_model_content_addressed",
    "full_scenario_adapter_verified",
    "ordered_execution_fidelity_closed",
    "paired_rollout_identity_closed",
)
_PHASE_CONTRACT = {
    "development": (256, 0),
    "selection_validation": (256, 10_000),
    "final_confirmation": (1_000, 20_000),
}
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

FuryMultiseedProtocolError = _v2.FuryMultiseedProtocolError
REQUIRED_PHASES = _v2.REQUIRED_PHASES
ROLLOUT_REQUIRED_FIELDS = _v2.ROLLOUT_REQUIRED_FIELDS
SEED_ALGORITHM = _v2.SEED_ALGORITHM
BASELINE_READINESS_FIELDS = _v2.BASELINE_READINESS_FIELDS


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryMultiseedProtocolError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FuryMultiseedProtocolError(f"{label} must be a list")
    return value


def _sha256_or_none(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise FuryMultiseedProtocolError(f"{label} must be null or lowercase SHA-256")
    return value


def build_fury_execution_source_identity_v3(
    *, project_root: str | Path = PROJECT_ROOT
) -> JSONMap:
    """Recursively identify the isolated v3 execution entrypoint closure."""

    result = _v2.build_fury_execution_source_identity_v2(
        project_root=project_root,
        required_relative_paths=V3_REQUIRED_PRODUCTION_PATHS,
    )
    result["schema"] = EXECUTION_SOURCE_IDENTITY_SCHEMA
    result["scope"] = "production_fury_v3_python_static_local_import_closure"
    return result


def _clone_function(
    function: Any,
    namespace: dict[str, Any],
    *,
    name: str,
) -> Any:
    """Clone one function into a private namespace without module mutation."""

    clone = types.FunctionType(
        function.__code__,
        namespace,
        name=name,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = copy.deepcopy(function.__kwdefaults__)
    clone.__annotations__ = dict(function.__annotations__)
    clone.__doc__ = function.__doc__
    clone.__module__ = __name__
    return clone


_V3_ENGINE_FUNCTIONS: dict[str, Any] | None = None


def _v3_engine_functions() -> dict[str, Any]:
    """Return v2 logic isolated in globals bound to the v3 source contract."""

    global _V3_ENGINE_FUNCTIONS
    if _V3_ENGINE_FUNCTIONS is not None:
        return _V3_ENGINE_FUNCTIONS

    namespace = dict(_v2.__dict__)
    namespace.update(
        {
            "EXECUTION_SOURCE_IDENTITY_SCHEMA": EXECUTION_SOURCE_IDENTITY_SCHEMA,
            "REQUIRED_PRODUCTION_PATHS": V3_REQUIRED_PRODUCTION_PATHS,
            "build_fury_execution_source_identity_v2": (
                build_fury_execution_source_identity_v3
            ),
        }
    )
    materialize = _clone_function(
        _v2.materialize_protocol,
        namespace,
        name="_materialize_protocol_engine_v3",
    )
    namespace["materialize_protocol"] = materialize
    selection_validator = _clone_function(
        _v2._validate_selection_inputs_and_evidence_against_protocol,
        namespace,
        name="_validate_selection_inputs_and_evidence_against_protocol_v3",
    )
    namespace[
        "_validate_selection_inputs_and_evidence_against_protocol"
    ] = selection_validator
    functions = {
        "materialize": materialize,
        "load": _clone_function(
            _v2.load_protocol, namespace, name="_load_protocol_engine_v3"
        ),
        "analyze": _clone_function(
            _v2.analyze_rollouts, namespace, name="_analyze_rollouts_engine_v3"
        ),
    }
    _V3_ENGINE_FUNCTIONS = functions
    return functions


def _historical_entry(document: Mapping[str, Any]) -> Mapping[str, Any]:
    baseline_contract = _mapping(
        document.get("baseline_contract"), "baseline_contract"
    )
    entries = _sequence(
        baseline_contract.get("required_baselines"),
        "baseline_contract.required_baselines",
    )
    observed_ids = tuple(
        str(_mapping(value, f"required_baselines[{index}]").get("policy_id"))
        for index, value in enumerate(entries)
    )
    if observed_ids != REQUIRED_BASELINE_IDS:
        raise FuryMultiseedProtocolError(
            "v3 required baseline order must be exactly Cat, deployed Contra, "
            "Contra260817, and Chronicle External-V2 historical Fury"
        )
    return _mapping(entries[-1], "historical required baseline")


def _historical_status(entry: Mapping[str, Any]) -> JSONMap:
    artifact = _mapping(entry.get("artifact_contract"), "historical artifact_contract")
    readiness = _mapping(
        entry.get("readiness_contract"), "historical readiness_contract"
    )
    status = artifact.get("status")
    artifact_ready = status == "READY"
    satisfied = readiness.get("satisfied_conditions")
    comparison_ready = readiness.get("comparison_ready") is True
    return {
        "policy_id": HISTORICAL_POLICY_ID,
        "artifact_status": status,
        "artifact_ready": artifact_ready,
        "readiness_complete": (
            comparison_ready
            and isinstance(satisfied, list)
            and tuple(satisfied) == HISTORICAL_REQUIRED_CONDITIONS
        ),
        "comparison_eligible": entry.get("comparison_eligible") is True,
        "blocker": entry.get("blocker"),
    }


def _validate_v3_execution_identity(document: Mapping[str, Any]) -> JSONMap:
    execution = _mapping(document.get("execution_contract"), "execution_contract")
    source_contract = _mapping(
        execution.get("python_source_identity_contract"),
        "execution_contract.python_source_identity_contract",
    )
    if set(source_contract) != {
        "schema",
        "verifier",
        "verify_at_materialization",
        "required_entrypoints",
        "file_count",
        "canonical_bundle_sha256",
    }:
        raise FuryMultiseedProtocolError("v3 source identity field set mismatch")
    if (
        source_contract.get("schema") != EXECUTION_SOURCE_IDENTITY_SCHEMA
        or source_contract.get("verifier") != "python_ast_recursive_local_imports"
        or source_contract.get("verify_at_materialization") is not True
        or source_contract.get("required_entrypoints")
        != sorted(V3_REQUIRED_PRODUCTION_PATHS)
    ):
        raise FuryMultiseedProtocolError(
            "v3 source identity must use the isolated v3 production entrypoints"
        )
    observed = build_fury_execution_source_identity_v3()
    if (
        source_contract.get("file_count") != observed["file_count"]
        or source_contract.get("canonical_bundle_sha256")
        != observed["canonical_bundle"]["sha256"]
    ):
        raise FuryMultiseedProtocolError(
            "live v3 Python execution closure differs from the protocol identity"
        )
    files = {
        str(row["relative_path"]): str(row["sha256"])
        for row in observed["files"]
    }
    for required in V3_REQUIRED_PRODUCTION_PATHS:
        if required not in files:
            raise FuryMultiseedProtocolError(
                f"live v3 Python execution closure lacks {required}"
            )

    implementation = _mapping(
        execution.get("implementation_identity"),
        "execution_contract.implementation_identity",
    )
    expected_implementation = {
        "python_source_closure_sha256": observed["canonical_bundle"]["sha256"],
        "ordered_sink_executor_sha256": files.get(
            "o2o_dps/fury_ordered_sink_executor_v2.py"
        ),
        "full_policy_rollout_executor_sha256": files[
            "o2o_dps/fury_full_policy_rollout_v3.py"
        ],
        "paired_runner_source_sha256": files[
            "o2o_dps/fury_paired_multiseed_runner_v3.py"
        ],
        "evaluation_source_sha256": files[
            "o2o_dps/fury_multiseed_evaluation_v3.py"
        ],
        "runtime_snapshot_sha256": document["runtime_identity_contract"][
            "snapshot_sha256"
        ],
    }
    if dict(implementation) != expected_implementation:
        raise FuryMultiseedProtocolError(
            "v3 implementation identity does not match the live isolated closure"
        )
    return observed


def validate_protocol_revision_v3(document: Mapping[str, Any]) -> JSONMap:
    """Validate the v3 overlay before invoking the unchanged v2 engine."""

    value = copy.deepcopy(dict(document))
    if value.get("schema_version") != 2 or value.get("kind") != PROTOCOL_WIRE_KIND:
        raise FuryMultiseedProtocolError(
            "v3 uses the audited v2 wire schema and must retain its schema/kind"
        )
    protocol_id = value.get("protocol_id")
    if not isinstance(protocol_id, str) or ".multiseed.v3." not in protocol_id:
        raise FuryMultiseedProtocolError("v3 protocol_id must carry the v3 revision")

    revision = _mapping(
        value.get("protocol_revision_contract"), "protocol_revision_contract"
    )
    expected_revision = {
        "revision": PROTOCOL_REVISION,
        "wire_schema_and_statistical_engine": PROTOCOL_WIRE_KIND,
        "runner_admission_profile": "fury_paired_multiseed_runner_v3",
        "required_baseline_count": len(REQUIRED_BASELINE_IDS),
        "required_stratum_count": len(REQUIRED_STRATA),
        "required_baseline_stratum_cell_count": (
            REQUIRED_BASELINE_STRATUM_CELL_COUNT
        ),
        "paired_seed_contract_preserved_from_v2": True,
        "scientific_runs_started": False,
    }
    for field, expected in expected_revision.items():
        if revision.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"protocol_revision_contract.{field} must be {expected!r}"
            )
    claim_boundary = revision.get("claim_boundary")
    if not isinstance(claim_boundary, str) or not claim_boundary.strip():
        raise FuryMultiseedProtocolError(
            "protocol_revision_contract.claim_boundary must be nonempty"
        )

    seed_contract = _mapping(value.get("seed_contract"), "seed_contract")
    if seed_contract.get("algorithm") != SEED_ALGORITHM:
        raise FuryMultiseedProtocolError("v3 seed algorithm differs from v2")
    namespace = seed_contract.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise FuryMultiseedProtocolError("seed namespace must be nonempty")
    phases = _mapping(seed_contract.get("phases"), "seed_contract.phases")
    if tuple(phases) != REQUIRED_PHASES:
        raise FuryMultiseedProtocolError("seed phases or their order changed")
    occupied: set[int] = set()
    for phase, (required_count, required_start) in _PHASE_CONTRACT.items():
        contract = _mapping(phases.get(phase), f"seed_contract.phases.{phase}")
        if (
            contract.get("count") != required_count
            or contract.get("counter_start") != required_start
            or contract.get("fixed_sample_no_optional_stopping") is not True
        ):
            raise FuryMultiseedProtocolError(
                f"{phase} must retain its fixed {required_count}-seed no-stopping contract"
            )
        seeds = _v2.derive_seed_set(
            namespace, phase, required_count, counter_start=required_start
        )
        if contract.get("seed_list_sha256") != _v2.sha256_json(list(seeds)):
            raise FuryMultiseedProtocolError(f"{phase} seed-list identity changed")
        overlap = occupied.intersection(seeds)
        if overlap:
            raise FuryMultiseedProtocolError("phase seed sets are not disjoint")
        occupied.update(seeds)
    if (
        phases["selection_validation"].get(
            "can_inform_one_predeclared_shortlist_selection"
        )
        is not True
    ):
        raise FuryMultiseedProtocolError(
            "selection-validation may inform exactly one predeclared shortlist selection"
        )
    if phases["final_confirmation"].get("can_inform_policy_changes") is not False:
        raise FuryMultiseedProtocolError(
            "final-confirmation seeds must never inform policy changes"
        )

    evaluation = _mapping(
        value.get("evaluation_contract"), "evaluation_contract"
    )
    if tuple(_sequence(evaluation.get("required_strata"), "required_strata")) != REQUIRED_STRATA:
        raise FuryMultiseedProtocolError(
            "v3 strata must be overall, single_target, and multi_target"
        )
    if (
        evaluation.get("same_seed_label_for_every_policy") is not True
        or evaluation.get("same_request_for_every_policy") is not True
    ):
        raise FuryMultiseedProtocolError(
            "every baseline and candidate must retain paired requests and seed labels"
        )
    statistics = _mapping(evaluation.get("statistics"), "statistics")
    expected_statistics = {
        "required_baseline_count": 4,
        "required_stratum_count": 3,
        "crossed_voting_bound_count_per_cell": 1,
        "bonferroni_cell_count": 12,
        "simultaneous_test_count": 12,
    }
    for field, expected in expected_statistics.items():
        if statistics.get(field) != expected:
            raise FuryMultiseedProtocolError(
                f"evaluation statistics {field} must be {expected}"
            )
    if statistics.get("bonferroni_family") != (
        "4 required baselines x 3 required strata x 1 crossed "
        "seed-by-component voting lower bound = 12"
    ):
        raise FuryMultiseedProtocolError("v3 Bonferroni family must name all 12 cells")

    historical = _historical_entry(value)
    artifact = _mapping(
        historical.get("artifact_contract"), "historical artifact_contract"
    )
    if artifact.get("schema") != HISTORICAL_ARTIFACT_SCHEMA:
        raise FuryMultiseedProtocolError("historical artifact schema mismatch")
    status = artifact.get("status")
    if status not in {"NOT_BUILT", "READY"}:
        raise FuryMultiseedProtocolError("historical artifact status is invalid")
    readiness = _mapping(
        historical.get("readiness_contract"), "historical readiness_contract"
    )
    required_conditions = tuple(
        _sequence(readiness.get("required_conditions"), "historical required_conditions")
    )
    if required_conditions != HISTORICAL_REQUIRED_CONDITIONS:
        raise FuryMultiseedProtocolError(
            "historical readiness conditions changed or were reordered"
        )
    satisfied_conditions = tuple(
        _sequence(
            readiness.get("satisfied_conditions"),
            "historical satisfied_conditions",
        )
    )
    if any(value not in HISTORICAL_REQUIRED_CONDITIONS for value in satisfied_conditions):
        raise FuryMultiseedProtocolError("historical readiness names an unknown condition")
    if len(set(satisfied_conditions)) != len(satisfied_conditions):
        raise FuryMultiseedProtocolError("historical readiness conditions contain duplicates")

    artifact_hash_fields = (
        "manifest_sha256",
        "cohort_receipt_sha256",
        "team_wave_model_manifest_sha256",
        "policy_model_sha256",
        "prefix_causality_receipt_sha256",
        "full_scenario_adapter_sha256",
    )
    artifact_hashes = {
        field: _sha256_or_none(artifact.get(field), f"artifact_contract.{field}")
        for field in artifact_hash_fields
    }
    source_digest = _sha256_or_none(
        historical.get("source_bundle_sha256"),
        "historical source_bundle_sha256",
    )
    adapter_digest = _sha256_or_none(
        historical.get("policy_adapter_sha256"),
        "historical policy_adapter_sha256",
    )
    profile_digest = _sha256_or_none(
        historical.get("policy_profile_sha256"),
        "historical policy_profile_sha256",
    )
    if status == "NOT_BUILT":
        if (
            historical.get("source_bundle_identity_status")
            != "MISSING_SENTINEL_NOT_AN_ARTIFACT"
            or source_digest != MISSING_HISTORICAL_SOURCE_SENTINEL_SHA256
            or any(value is not None for value in artifact_hashes.values())
            or artifact.get("manifest_path") is not None
            or adapter_digest is not None
            or profile_digest is not None
            or satisfied_conditions
            or readiness.get("comparison_ready") is not False
            or historical.get("comparison_eligible") is not False
            or historical.get("comparison_eligibility_receipt") is not None
        ):
            raise FuryMultiseedProtocolError(
                "missing historical artifact must remain an explicit non-artifact sentinel"
            )
    else:
        if (
            not isinstance(artifact.get("manifest_path"), str)
            or not artifact.get("manifest_path")
            or any(value is None for value in artifact_hashes.values())
            or source_digest in {None, MISSING_HISTORICAL_SOURCE_SENTINEL_SHA256}
            or adapter_digest is None
            or profile_digest is None
            or historical.get("source_bundle_identity_status") != "CONTENT_ADDRESSED_ARTIFACT"
        ):
            raise FuryMultiseedProtocolError(
                "READY historical baseline lacks a complete content-addressed artifact"
            )
        if readiness.get("comparison_ready") is True and satisfied_conditions != HISTORICAL_REQUIRED_CONDITIONS:
            raise FuryMultiseedProtocolError(
                "historical comparison readiness requires every condition in fixed order"
            )
    if historical.get("comparison_eligible") is True and (
        status != "READY"
        or readiness.get("comparison_ready") is not True
        or satisfied_conditions != HISTORICAL_REQUIRED_CONDITIONS
        or not isinstance(historical.get("comparison_eligibility_receipt"), Mapping)
    ):
        raise FuryMultiseedProtocolError(
            "historical baseline cannot be eligible without artifact and readiness closure"
        )

    corpus = _mapping(value.get("corpus_contract"), "corpus_contract")
    final_corpus = _mapping(
        corpus.get("final_confirmation_corpus"), "final_confirmation_corpus"
    )
    for field in (
        "instance_disjoint_from_current_snapshot",
        "guild_player_component_disjoint_from_current_snapshot",
        "policy_must_be_frozen_before_ingestion",
    ):
        if final_corpus.get(field) is not True:
            raise FuryMultiseedProtocolError(
                f"future final corpus must retain {field}=true"
            )
    if value["candidate_contract"].get(
        "selected_without_final_confirmation_seeds"
    ) is not True:
        raise FuryMultiseedProtocolError(
            "candidate selection must not use final-confirmation seeds"
        )

    _validate_v3_execution_identity(value)

    return value


def _attach_v3_envelope(materialized: Mapping[str, Any]) -> JSONMap:
    result = copy.deepcopy(dict(materialized))
    historical = _historical_entry(result["protocol"])
    result["protocol_revision"] = PROTOCOL_REVISION
    result["runner_admission_profile"] = "fury_paired_multiseed_runner_v3"
    result["required_baseline_stratum_cell_count"] = (
        REQUIRED_BASELINE_STRATUM_CELL_COUNT
    )
    result["historical_baseline_readiness"] = _historical_status(historical)
    return result


def _v2_materialized(materialized: Mapping[str, Any]) -> JSONMap:
    result = copy.deepcopy(dict(materialized))
    for field in (
        "protocol_revision",
        "runner_admission_profile",
        "required_baseline_stratum_cell_count",
        "historical_baseline_readiness",
    ):
        result.pop(field, None)
    return result


def materialize_protocol(
    document: Mapping[str, Any],
    *,
    trusted_external_anchor_seal_sha256s: Iterable[str] = (),
    selection_physical_replays: Iterable[Mapping[str, Any]] = (),
    final_discovery_capture_manifest_path: str | Path | None = None,
    _allow_nonpromoting_test_baseline_receipts: bool = False,
) -> JSONMap:
    value = validate_protocol_revision_v3(document)
    materialized = _v3_engine_functions()["materialize"](
        value,
        trusted_external_anchor_seal_sha256s=trusted_external_anchor_seal_sha256s,
        selection_physical_replays=selection_physical_replays,
        final_discovery_capture_manifest_path=final_discovery_capture_manifest_path,
        _allow_nonpromoting_test_baseline_receipts=(
            _allow_nonpromoting_test_baseline_receipts
        ),
    )
    return _attach_v3_envelope(materialized)


def load_protocol(
    path: Path,
    *,
    trusted_external_anchor_seal_sha256s: Iterable[str] = (),
    selection_physical_replay_package_paths: Iterable[str | Path] = (),
    final_discovery_capture_manifest_path: str | Path | None = None,
) -> JSONMap:
    try:
        document = json.loads(path.expanduser().resolve().read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise FuryMultiseedProtocolError(f"could not read v3 protocol: {error}") from error
    if not isinstance(document, Mapping):
        raise FuryMultiseedProtocolError("v3 protocol root must be an object")
    validate_protocol_revision_v3(document)
    materialized = _v3_engine_functions()["load"](
        path,
        trusted_external_anchor_seal_sha256s=trusted_external_anchor_seal_sha256s,
        selection_physical_replay_package_paths=(
            selection_physical_replay_package_paths
        ),
        final_discovery_capture_manifest_path=final_discovery_capture_manifest_path,
    )
    return _attach_v3_envelope(materialized)


def build_plan(materialized: Mapping[str, Any]) -> JSONMap:
    protocol = _mapping(materialized.get("protocol"), "materialized.protocol")
    validate_protocol_revision_v3(protocol)
    plan = _v2.build_plan(_v2_materialized(materialized))
    history = _historical_status(_historical_entry(protocol))
    blockers = list(plan["blocked_reasons"])
    if not history["artifact_ready"]:
        blockers.append("historical_external_v2_artifact_not_ready")
    if not history["readiness_complete"]:
        blockers.append("historical_external_v2_readiness_not_closed")
    plan.update(
        {
            "schema_version": 3,
            "kind": PLAN_KIND,
            "protocol_revision": PROTOCOL_REVISION,
            "required_baseline_ids": list(REQUIRED_BASELINE_IDS),
            "required_baseline_stratum_cell_count": (
                REQUIRED_BASELINE_STRATUM_CELL_COUNT
            ),
            "historical_baseline_readiness": history,
            "execution_started": False,
            "rollout_count": 0,
            "victory_claim_allowed": False,
            "blocked_reasons": list(dict.fromkeys(blockers)),
            "next_gate": (
                "build and physically verify the prefix-causal External-V2 "
                "historical Fury artifact before any four-baseline dispatch"
            ),
        }
    )
    history_blockers = (
        "historical_external_v2_artifact_not_ready",
        "historical_external_v2_readiness_not_closed",
    )
    for phase in plan["readiness"]["phases"].values():
        for blocker in history_blockers:
            if blocker not in phase["blockers"] and blocker in plan["blocked_reasons"]:
                phase["blockers"].append(blocker)
        if any(blocker in plan["blocked_reasons"] for blocker in history_blockers):
            phase["dispatch_allowed"] = False
    return plan


def analyze_rollouts(
    materialized: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    *,
    runner_plan: Mapping[str, Any],
    reduction_receipt: Mapping[str, Any],
    expected_protocol_sha256: str | None,
    phase: str,
    candidate_id: str,
    manifest_paths: Iterable[str | Path] | None = None,
    _allow_nonpromoting_test_selection_replay_authority: bool = False,
) -> JSONMap:
    protocol = _mapping(materialized.get("protocol"), "materialized.protocol")
    validate_protocol_revision_v3(protocol)
    history = _historical_status(_historical_entry(protocol))
    if not (
        history["artifact_ready"]
        and history["readiness_complete"]
        and history["comparison_eligible"]
    ):
        raise FuryMultiseedProtocolError(
            "four-baseline analysis is blocked: historical External-V2 baseline "
            "artifact/readiness is absent or non-comparison-eligible"
        )
    v2_plan = _runner_v3.unwrap_runner_plan(runner_plan)
    artifact = _v3_engine_functions()["analyze"](
        _v2_materialized(materialized),
        rows,
        runner_plan=v2_plan,
        reduction_receipt=reduction_receipt,
        expected_protocol_sha256=expected_protocol_sha256,
        phase=phase,
        candidate_id=candidate_id,
        manifest_paths=manifest_paths,
        _allow_nonpromoting_test_selection_replay_authority=(
            _allow_nonpromoting_test_selection_replay_authority
        ),
    )
    artifact["schema_version"] = 3
    artifact["kind"] = ANALYSIS_KIND
    artifact["protocol_revision"] = PROTOCOL_REVISION
    artifact["required_baseline_stratum_cell_count"] = (
        REQUIRED_BASELINE_STRATUM_CELL_COUNT
    )
    return artifact


def _read_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.expanduser().resolve().read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise FuryMultiseedProtocolError(f"could not read {label}: {error}") from error
    if not isinstance(value, dict):
        raise FuryMultiseedProtocolError(f"{label} root must be an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        + b"\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--phase", choices=REQUIRED_PHASES)
    parser.add_argument("--reduction", type=Path)
    parser.add_argument("--runner-plan", type=Path)
    parser.add_argument("--shard-manifest", type=Path, action="append", default=[])
    parser.add_argument("--trusted-external-anchor-seal-sha256", action="append", default=[])
    parser.add_argument("--selection-replay-package", type=Path, action="append", default=[])
    parser.add_argument("--final-discovery-capture-manifest", type=Path)
    parser.add_argument("--expected-protocol-sha256")
    parser.add_argument("--candidate-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    materialized = load_protocol(
        args.protocol,
        trusted_external_anchor_seal_sha256s=(
            args.trusted_external_anchor_seal_sha256
        ),
        selection_physical_replay_package_paths=args.selection_replay_package,
        final_discovery_capture_manifest_path=args.final_discovery_capture_manifest,
    )
    if args.plan_only:
        if any(
            (
                args.phase,
                args.reduction,
                args.runner_plan,
                args.shard_manifest,
                args.expected_protocol_sha256,
                args.candidate_id,
            )
        ):
            parser.error("--plan-only cannot be combined with analysis arguments")
        output = args.output or DEFAULT_PLAN
        artifact = build_plan(materialized)
    else:
        if (
            not args.phase
            or args.reduction is None
            or args.runner_plan is None
            or not args.shard_manifest
            or not args.expected_protocol_sha256
            or not args.candidate_id
        ):
            parser.error(
                "analysis requires phase, runner plan, reduction, all shard "
                "manifests, expected protocol SHA-256, and candidate ID"
            )
        reduction = _read_json(args.reduction, "reduction")
        artifact = analyze_rollouts(
            materialized,
            _sequence(reduction.get("rollout_rows"), "reduction.rollout_rows"),
            runner_plan=_read_json(args.runner_plan, "runner plan"),
            reduction_receipt=_mapping(
                reduction.get("reduction_receipt"), "reduction_receipt"
            ),
            expected_protocol_sha256=args.expected_protocol_sha256,
            phase=args.phase,
            candidate_id=args.candidate_id,
            manifest_paths=args.shard_manifest,
        )
        output = args.output or DEFAULT_ANALYSIS
    _write_json(output, artifact)
    print(
        json.dumps(
            {
                "kind": artifact["kind"],
                "protocol_sha256": artifact["protocol_sha256"],
                "output": str(output.expanduser().resolve()),
                "execution_started": artifact.get("execution_started"),
                "victory_claim_allowed": artifact.get("victory_claim_allowed"),
                "blocked_reasons": artifact.get("blocked_reasons"),
            },
            ensure_ascii=False,
        )
    )
    return 0


# Stable helpers intentionally retain their v2 algorithms and schemas.
canonical_json_bytes = _v2.canonical_json_bytes
sha256_json = _v2.sha256_json
derive_seed_set = _v2.derive_seed_set
derive_simulator_seed = _v2.derive_simulator_seed
corpus_binding_sha256 = _v2.corpus_binding_sha256
selection_design_sha256 = _v2.selection_design_sha256
build_selection_evidence_bundle = _v2.build_selection_evidence_bundle
build_baseline_comparison_readiness_receipt = (
    _v2.build_baseline_comparison_readiness_receipt
)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ANALYSIS_KIND",
    "BASELINE_READINESS_FIELDS",
    "DEFAULT_PROTOCOL",
    "FuryMultiseedProtocolError",
    "HISTORICAL_ARTIFACT_SCHEMA",
    "HISTORICAL_POLICY_ID",
    "HISTORICAL_REQUIRED_CONDITIONS",
    "PLAN_KIND",
    "PROTOCOL_REVISION",
    "PROTOCOL_WIRE_KIND",
    "REQUIRED_BASELINE_IDS",
    "REQUIRED_BASELINE_STRATUM_CELL_COUNT",
    "REQUIRED_PHASES",
    "REQUIRED_STRATA",
    "ROLLOUT_REQUIRED_FIELDS",
    "SEED_ALGORITHM",
    "analyze_rollouts",
    "build_baseline_comparison_readiness_receipt",
    "build_plan",
    "build_selection_evidence_bundle",
    "canonical_json_bytes",
    "corpus_binding_sha256",
    "derive_seed_set",
    "derive_simulator_seed",
    "load_protocol",
    "main",
    "materialize_protocol",
    "selection_design_sha256",
    "sha256_json",
    "validate_protocol_revision_v3",
]
