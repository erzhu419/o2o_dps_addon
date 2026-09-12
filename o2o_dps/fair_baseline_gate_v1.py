"""Declaration-only preflight for a future fair five-lane evidence gate.

The gate does not run a simulator and it does not score DPS.  It answers the
earlier question that must be settled first: did Cat, deployed Contra,
Contra260817/Contra_new, the reconstructed historical expert, and the candidate
all declare complete controllers under the same semantic contract?

Version 1 does not open or validate the referenced receipts.  Consequently it
never admits a comparison: even a complete declaration remains
``DECLARATION_COMPLETE_NOT_ADMITTED`` until a family-specific evidence reader
is implemented in a later gate.

Readable source is useful identity evidence, but is deliberately distinct from
source/runtime parity.  Likewise, the pooled ``historical_behavior_clone/v1``
fixture is executable wiring evidence, not a reconstructed top-player expert.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


JSONMap = dict[str, Any]
SCHEMA = "fair_baseline_gate/v1"
INPUT_SCHEMA = "fair_baseline_comparison_case/v1"
IMPLEMENTATION_REVISION = "v1.1_declaration_preflight_never_admits_evidence"

REQUIRED_LANE_FAMILIES = (
    "CAT",
    "CONTRA_DEPLOYED",
    "CONTRA_NEW",
    "HISTORICAL_EXPERT",
    "CANDIDATE",
)
MATCHED_CONTRACT_FIELDS = (
    "character_context_id",
    "raid_context_id",
    "encounter_id",
    "execution_model_id",
    "objective_id",
    "seed_set_id",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "offline_data" / "reports" / "fury_fair_baseline_gate_v1.json"
)


class FairBaselineGateV1Error(ValueError):
    """The gate input or one of the current-project artifacts is malformed."""


def _strict_json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise FairBaselineGateV1Error(f"{label} must be strict JSON: {error}") from error


def _mapping(value: Any, label: str) -> JSONMap:
    copied = _strict_json_copy(value, label)
    if not isinstance(copied, dict):
        raise FairBaselineGateV1Error(f"{label} must be an object")
    return copied


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FairBaselineGateV1Error(f"{label} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise FairBaselineGateV1Error(f"{label} must be boolean")
    return value


def _normalize_shared_contract(value: Any) -> JSONMap:
    source = _mapping(value, "shared_contract")
    if set(source) != set(MATCHED_CONTRACT_FIELDS):
        raise FairBaselineGateV1Error(
            "shared_contract must contain exactly: "
            + ", ".join(MATCHED_CONTRACT_FIELDS)
        )
    return {
        field: _text(source.get(field), f"shared_contract.{field}")
        for field in MATCHED_CONTRACT_FIELDS
    }


def _normalize_lane(value: Any, index: int) -> JSONMap:
    lane = _mapping(value, f"lanes[{index}]")
    family = _text(lane.get("lane_family"), f"lanes[{index}].lane_family")
    policy_id = _text(lane.get("policy_id"), f"lanes[{index}].policy_id")
    contract = _mapping(lane.get("contract"), f"lanes[{index}].contract")
    unknown_fields = sorted(set(contract) - set(MATCHED_CONTRACT_FIELDS))
    if unknown_fields:
        raise FairBaselineGateV1Error(
            f"lanes[{index}].contract has unknown fields: {unknown_fields}"
        )
    evidence_refs = lane.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise FairBaselineGateV1Error(
            f"lanes[{index}].evidence_refs must be a non-empty array"
        )
    normalized_refs = [
        _text(item, f"lanes[{index}].evidence_refs[{ref_index}]")
        for ref_index, item in enumerate(evidence_refs)
    ]
    normalized = {
        "lane_family": family,
        "policy_id": policy_id,
        "contract": {
            field: contract.get(field) for field in MATCHED_CONTRACT_FIELDS
        },
        "artifact_complete": _boolean(
            lane.get("artifact_complete"), f"lanes[{index}].artifact_complete"
        ),
        "full_controller": _boolean(
            lane.get("full_controller"), f"lanes[{index}].full_controller"
        ),
        "source_identity_bound": _boolean(
            lane.get("source_identity_bound"),
            f"lanes[{index}].source_identity_bound",
        ),
        "source_runtime_parity_proven": _boolean(
            lane.get("source_runtime_parity_proven"),
            f"lanes[{index}].source_runtime_parity_proven",
        ),
        "comparison_ready": _boolean(
            lane.get("comparison_ready"), f"lanes[{index}].comparison_ready"
        ),
        "seed_coverage_complete": _boolean(
            lane.get("seed_coverage_complete"),
            f"lanes[{index}].seed_coverage_complete",
        ),
        "fixture_only": _boolean(
            lane.get("fixture_only"), f"lanes[{index}].fixture_only"
        ),
        "historical_expert_cohort_proven": _boolean(
            lane.get("historical_expert_cohort_proven"),
            f"lanes[{index}].historical_expert_cohort_proven",
        ),
        "evidence_refs": normalized_refs,
    }
    return normalized


def _lane_blockers(lane: Mapping[str, Any], shared: Mapping[str, str]) -> list[JSONMap]:
    blockers: list[JSONMap] = []
    contract = lane["contract"]
    missing = [
        field
        for field in MATCHED_CONTRACT_FIELDS
        if not isinstance(contract.get(field), str) or not contract[field].strip()
    ]
    if missing:
        blockers.append({"code": "MATCHED_CONTRACT_NOT_BOUND", "fields": missing})
    mismatches = {
        field: {"expected": shared[field], "observed": contract.get(field)}
        for field in MATCHED_CONTRACT_FIELDS
        if field not in missing and contract[field] != shared[field]
    }
    if mismatches:
        blockers.append({"code": "MATCHED_CONTRACT_MISMATCH", "fields": mismatches})

    flag_blockers = (
        ("artifact_complete", "LANE_ARTIFACT_INCOMPLETE"),
        ("full_controller", "FULL_CONTROLLER_NOT_PROVEN"),
        ("source_identity_bound", "SOURCE_IDENTITY_NOT_BOUND"),
        ("source_runtime_parity_proven", "SOURCE_RUNTIME_PARITY_NOT_PROVEN"),
        ("comparison_ready", "LANE_COMPARISON_READY_FALSE"),
        ("seed_coverage_complete", "SEED_COVERAGE_INCOMPLETE"),
    )
    blockers.extend(
        {"code": code} for field, code in flag_blockers if lane[field] is not True
    )
    if lane["fixture_only"] is True:
        blockers.append({"code": "FIXTURE_ONLY_LANE"})
    if (
        lane["lane_family"] == "HISTORICAL_EXPERT"
        and lane["historical_expert_cohort_proven"] is not True
    ):
        blockers.append({"code": "HISTORICAL_EXPERT_COHORT_NOT_PROVEN"})
    return blockers


def evaluate_fair_baseline_gate_v1(case: Mapping[str, Any]) -> JSONMap:
    """Validate declarations without treating them as admitted evidence."""

    raw = _mapping(case, "comparison case")
    if raw.get("schema") != INPUT_SCHEMA:
        raise FairBaselineGateV1Error(
            f"comparison case schema must equal {INPUT_SCHEMA}"
        )
    comparison_id = _text(raw.get("comparison_id"), "comparison_id")
    shared = _normalize_shared_contract(raw.get("shared_contract"))
    raw_lanes = raw.get("lanes")
    if not isinstance(raw_lanes, list):
        raise FairBaselineGateV1Error("lanes must be an array")
    lanes = [_normalize_lane(value, index) for index, value in enumerate(raw_lanes)]

    by_family: dict[str, list[JSONMap]] = {}
    for lane in lanes:
        by_family.setdefault(lane["lane_family"], []).append(lane)

    global_blockers: list[JSONMap] = []
    missing_families = [
        family for family in REQUIRED_LANE_FAMILIES if family not in by_family
    ]
    duplicate_families = [
        family
        for family in REQUIRED_LANE_FAMILIES
        if len(by_family.get(family, ())) > 1
    ]
    unsupported_families = sorted(set(by_family) - set(REQUIRED_LANE_FAMILIES))
    if missing_families:
        global_blockers.append(
            {"code": "MISSING_REQUIRED_LANES", "lane_families": missing_families}
        )
    if duplicate_families:
        global_blockers.append(
            {"code": "DUPLICATE_REQUIRED_LANES", "lane_families": duplicate_families}
        )
    if unsupported_families:
        global_blockers.append(
            {"code": "UNSUPPORTED_EXTRA_LANES", "lane_families": unsupported_families}
        )
    global_blockers.append({"code": "EVIDENCE_ADMISSION_NOT_IMPLEMENTED_V1"})

    lane_results: list[JSONMap] = []
    for family in REQUIRED_LANE_FAMILIES:
        for lane in by_family.get(family, ()):  # duplicate rows remain auditable
            blockers = _lane_blockers(lane, shared)
            lane_results.append(
                {
                    **deepcopy(lane),
                    "status": (
                        "DECLARATION_COMPLETE_NOT_ADMITTED"
                        if not blockers
                        else "REFUSED"
                    ),
                    "blockers": blockers,
                }
            )
    for family in unsupported_families:
        for lane in by_family[family]:
            lane_results.append(
                {
                    **deepcopy(lane),
                    "status": "REFUSED",
                    "blockers": [{"code": "UNSUPPORTED_LANE_FAMILY"}],
                }
            )

    result: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "comparison_id": comparison_id,
        "decision": "REFUSE_COMPARISON",
        "comparison_allowed": False,
        "required_lane_families": list(REQUIRED_LANE_FAMILIES),
        "matched_contract_fields": list(MATCHED_CONTRACT_FIELDS),
        "shared_contract": shared,
        "global_blockers": global_blockers,
        "lanes": lane_results,
        "admitted_lane_count": 0,
        "declaration_complete_lane_count": sum(
            row["status"] == "DECLARATION_COMPLETE_NOT_ADMITTED"
            for row in lane_results
        ),
        "refused_lane_count": sum(
            row["status"] == "REFUSED" for row in lane_results
        ),
        "claim_boundary": (
            "v1 validates declarations only; evidence references and caller booleans "
            "cannot admit a fair five-lane DPS comparison"
        ),
    }
    metadata = raw.get("audit_metadata")
    if metadata is not None:
        result["audit_metadata"] = _mapping(metadata, "audit_metadata")
    return result


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FairBaselineGateV1Error(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise FairBaselineGateV1Error(f"{label} must be a JSON object: {path}")
    return value


def _entry_by(items: Any, key: str, value: str, label: str) -> JSONMap:
    if not isinstance(items, list):
        raise FairBaselineGateV1Error(f"{label} must be an array")
    matches = [row for row in items if isinstance(row, dict) and row.get(key) == value]
    if len(matches) != 1:
        raise FairBaselineGateV1Error(
            f"{label} must contain exactly one {key}={value!r} row"
        )
    return deepcopy(matches[0])


def _blank_contract() -> JSONMap:
    return {field: None for field in MATCHED_CONTRACT_FIELDS}


def _current_lane(
    family: str,
    policy_id: str,
    *,
    artifact_complete: bool,
    full_controller: bool,
    source_identity_bound: bool,
    source_runtime_parity_proven: bool,
    comparison_ready: bool,
    fixture_only: bool,
    historical_expert_cohort_proven: bool,
    evidence_refs: Sequence[str],
) -> JSONMap:
    return {
        "lane_family": family,
        "policy_id": policy_id,
        "contract": _blank_contract(),
        "artifact_complete": artifact_complete,
        "full_controller": full_controller,
        "source_identity_bound": source_identity_bound,
        "source_runtime_parity_proven": source_runtime_parity_proven,
        "comparison_ready": comparison_ready,
        "seed_coverage_complete": False,
        "fixture_only": fixture_only,
        "historical_expert_cohort_proven": historical_expert_cohort_proven,
        "evidence_refs": list(evidence_refs),
    }


def audit_current_project_v1(project_root: str | Path = PROJECT_ROOT) -> JSONMap:
    """Audit the repository's current formal manifests/configs, without simulation."""

    root = Path(project_root).expanduser().resolve()
    paths = {
        "protocol": root / "configs" / "evaluation" / "fury_multiseed_protocol_v3.json",
        "experts": root / "configs" / "experts" / "fury_experts_v1.json",
        "cat": root / "configs" / "experts" / "cat_deployed_source_manifest_c31be2f9.json",
        "contra_new": root / "configs" / "experts" / "contra260817_source_manifest_91baa120.json",
        "candidate_combined": root / "configs" / "policies" / "fury_combined_candidate_v1.json",
        "candidate_level": root / "configs" / "policies" / "fury_level_conditioned_candidate_v1.json",
    }
    documents = {name: _load_json(path, name) for name, path in paths.items()}
    protocol = documents["protocol"]
    if protocol.get("kind") != "fury_multiseed_evaluation_protocol_v2":
        raise FairBaselineGateV1Error("unexpected Fury multiseed protocol kind")
    experts = documents["experts"]
    if experts.get("schema") != "fury_expert_manifest/v1":
        raise FairBaselineGateV1Error("unexpected Fury expert manifest schema")
    if documents["cat"].get("schema") != "cat_deployed_source_manifest/v1":
        raise FairBaselineGateV1Error("unexpected Cat source manifest schema")
    if documents["contra_new"].get("schema") != "contra260817_source_manifest/v1":
        raise FairBaselineGateV1Error("unexpected Contra260817 source manifest schema")
    for name in ("candidate_combined", "candidate_level"):
        if documents[name].get("kind") != "fury_policy_candidate_grid_v1":
            raise FairBaselineGateV1Error(f"unexpected {name} config kind")

    baseline_contract = _mapping(protocol.get("baseline_contract"), "baseline_contract")
    baseline_rows = baseline_contract.get("required_baselines")
    cat_protocol = _entry_by(
        baseline_rows, "policy_id", "cat.fury.profile1", "required_baselines"
    )
    deployed_protocol = _entry_by(
        baseline_rows,
        "policy_id",
        "contra.deployed.fury.raid_a",
        "required_baselines",
    )
    new_protocol = _entry_by(
        baseline_rows,
        "policy_id",
        "contra260817.fury.source_candidate",
        "required_baselines",
    )
    historical_protocol = _entry_by(
        baseline_rows,
        "policy_id",
        "chronicle.external_v2.fury.historical_player_policy",
        "required_baselines",
    )
    expert_rows = experts.get("experts")
    deployed_expert = _entry_by(
        expert_rows,
        "expert_id",
        "contra.deployed.fury.raid_a",
        "experts",
    )
    source_verification = _mapping(
        deployed_expert.get("source_verification"),
        "deployed Contra source verification",
    )
    runtime_binding = _mapping(
        deployed_expert.get("runtime_binding"), "deployed Contra runtime binding"
    )

    runtime = _mapping(protocol.get("runtime_identity_contract"), "runtime identity")
    corpus = _mapping(protocol.get("corpus_contract"), "corpus contract")
    phase_bindings = _mapping(
        corpus.get("phase_corpus_bindings"), "phase corpus bindings"
    )
    development = _mapping(phase_bindings.get("development"), "development corpus")
    execution = _mapping(protocol.get("execution_contract"), "execution contract")
    implementation = _mapping(
        execution.get("implementation_identity"), "implementation identity"
    )
    evaluation = _mapping(protocol.get("evaluation_contract"), "evaluation contract")
    statistics = _mapping(evaluation.get("statistics"), "evaluation statistics")
    seed_contract = _mapping(protocol.get("seed_contract"), "seed contract")
    phases = _mapping(seed_contract.get("phases"), "seed phases")
    development_seeds = _mapping(phases.get("development"), "development seeds")
    protocol_runtime_snapshot = _text(
        runtime.get("snapshot_sha256"), "runtime snapshot identity"
    )
    deployed_runtime_snapshot = _text(
        runtime_binding.get("runtime_snapshot_sha256"),
        "deployed Contra runtime snapshot identity",
    )
    runtime_character_context_matches = (
        protocol_runtime_snapshot == deployed_runtime_snapshot
    )
    shared = {
        "character_context_id": protocol_runtime_snapshot,
        "raid_context_id": _text(
            development.get("corpus_binding_sha256"), "development corpus binding"
        ),
        "encounter_id": _text(
            development.get("runner_scenario_bundle_sha256"),
            "development scenario bundle",
        ),
        "execution_model_id": _text(
            implementation.get("python_source_closure_sha256"),
            "execution implementation identity",
        ),
        "objective_id": _text(statistics.get("primary_metric"), "primary metric"),
        "seed_set_id": _text(
            development_seeds.get("seed_list_sha256"), "development seed set"
        ),
    }

    cat_readiness = _mapping(
        documents["cat"].get("comparison_readiness"), "Cat comparison readiness"
    )
    new_closure = _mapping(
        documents["contra_new"].get("closure"), "Contra260817 closure"
    )
    historical_artifact = _mapping(
        historical_protocol.get("artifact_contract"), "historical artifact contract"
    )
    historical_readiness = _mapping(
        historical_protocol.get("readiness_contract"),
        "historical readiness contract",
    )
    candidate_contract = _mapping(protocol.get("candidate_contract"), "candidate contract")

    lanes = [
        _current_lane(
            "CAT",
            str(cat_protocol["policy_id"]),
            artifact_complete=False,
            full_controller=cat_readiness.get("simulator_adapter_complete") is True,
            source_identity_bound=cat_readiness.get("source_identity_complete") is True,
            source_runtime_parity_proven=False,
            comparison_ready=(
                cat_readiness.get("eligible_for_comparison") is True
                and cat_protocol.get("comparison_eligible") is True
            ),
            fixture_only=False,
            historical_expert_cohort_proven=False,
            evidence_refs=(
                "configs/experts/cat_deployed_source_manifest_c31be2f9.json",
                "configs/evaluation/fury_multiseed_protocol_v3.json",
            ),
        ),
        _current_lane(
            "CONTRA_DEPLOYED",
            str(deployed_protocol["policy_id"]),
            artifact_complete=False,
            full_controller=False,
            source_identity_bound=(
                source_verification.get("source_visibility_blocked") is False
                and runtime_binding.get("schema") == "deployed_contra_runtime_binding/v1"
            ),
            source_runtime_parity_proven=False,
            comparison_ready=deployed_protocol.get("comparison_eligible") is True,
            fixture_only=False,
            historical_expert_cohort_proven=False,
            evidence_refs=(
                "configs/experts/fury_experts_v1.json",
                "configs/evaluation/fury_multiseed_protocol_v3.json",
            ),
        ),
        _current_lane(
            "CONTRA_NEW",
            str(new_protocol["policy_id"]),
            artifact_complete=new_closure.get("package_complete") is True,
            full_controller=False,
            source_identity_bound=(
                new_closure.get("source_identity_verified_by_manifest") is True
            ),
            source_runtime_parity_proven=(
                documents["contra_new"].get("package", {}).get(
                    "exact_runtime_verified"
                )
                is True
            ),
            comparison_ready=(
                new_closure.get("comparison_eligible") is True
                and new_protocol.get("comparison_eligible") is True
            ),
            fixture_only=False,
            historical_expert_cohort_proven=False,
            evidence_refs=(
                "configs/experts/contra260817_source_manifest_91baa120.json",
                "configs/evaluation/fury_multiseed_protocol_v3.json",
            ),
        ),
        _current_lane(
            "HISTORICAL_EXPERT",
            str(historical_protocol["policy_id"]),
            artifact_complete=historical_artifact.get("status") == "COMPLETE",
            full_controller=(
                "full_scenario_adapter_verified"
                in historical_readiness.get("satisfied_conditions", ())
            ),
            source_identity_bound=(
                historical_artifact.get("cohort_receipt_sha256") is not None
                and historical_artifact.get("policy_model_sha256") is not None
            ),
            source_runtime_parity_proven=(
                "ordered_execution_fidelity_closed"
                in historical_readiness.get("satisfied_conditions", ())
            ),
            comparison_ready=historical_readiness.get("comparison_ready") is True,
            fixture_only=True,
            historical_expert_cohort_proven=False,
            evidence_refs=(
                "configs/evaluation/fury_multiseed_protocol_v3.json",
                "o2o_dps/historical_behavior_clone_v1.py",
            ),
        ),
        _current_lane(
            "CANDIDATE",
            str(candidate_contract.get("policy_id") or "UNFROZEN_CANDIDATE"),
            artifact_complete=candidate_contract.get("status") == "FROZEN",
            full_controller=False,
            source_identity_bound=(
                candidate_contract.get("policy_source_sha256") is not None
                and candidate_contract.get("policy_adapter_sha256") is not None
                and candidate_contract.get("policy_profile_sha256") is not None
            ),
            source_runtime_parity_proven=False,
            comparison_ready=False,
            fixture_only=False,
            historical_expert_cohort_proven=False,
            evidence_refs=(
                "configs/policies/fury_combined_candidate_v1.json",
                "configs/policies/fury_level_conditioned_candidate_v1.json",
                "configs/evaluation/fury_multiseed_protocol_v3.json",
            ),
        ),
    ]
    case = {
        "schema": INPUT_SCHEMA,
        "comparison_id": "current.fury.five_lane.formal_artifact.audit.v1",
        "shared_contract": shared,
        "lanes": lanes,
        "audit_metadata": {
            "mode": "CURRENT_FORMAL_ARTIFACTS_READ_ONLY",
            "artifacts": [
                {"name": name, "path": str(path.relative_to(root)).replace("\\", "/")}
                for name, path in paths.items()
            ],
            "matched_five_lane_result_report": "ABSENT",
            "runtime_character_context": {
                "status": (
                    "MATCHED"
                    if runtime_character_context_matches
                    else "STALE_OR_MISMATCHED_CHARACTER_CONTEXT"
                ),
                "protocol_runtime_snapshot_sha256": protocol_runtime_snapshot,
                "deployed_contra_runtime_snapshot_sha256": deployed_runtime_snapshot,
                "matches": runtime_character_context_matches,
            },
            "contra_source_readability_interpretation": (
                "source identity is available; runtime parity and complete controller "
                "coverage remain separate admission requirements"
            ),
            "historical_clone_interpretation": (
                "historical_behavior_clone/v1 is pooled clean-Fury diagnostic wiring, "
                "not a frozen top-player historical expert"
            ),
        },
    }
    result = evaluate_fair_baseline_gate_v1(case)
    if not runtime_character_context_matches:
        result["global_blockers"].insert(
            0,
            {
                "code": "STALE_OR_MISMATCHED_CHARACTER_CONTEXT",
                "protocol_runtime_snapshot_sha256": protocol_runtime_snapshot,
                "deployed_contra_runtime_snapshot_sha256": deployed_runtime_snapshot,
            },
        )
        result["decision"] = "REFUSE_COMPARISON"
        result["comparison_allowed"] = False
    return result


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        help=f"explicit {INPUT_SCHEMA} JSON; omit to audit current project artifacts",
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.input is None:
        result = audit_current_project_v1(args.project_root)
    else:
        result = evaluate_fair_baseline_gate_v1(
            _load_json(args.input.expanduser().resolve(), "comparison case")
        )
    _write_json(args.output.expanduser().resolve(), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "FairBaselineGateV1Error",
    "IMPLEMENTATION_REVISION",
    "INPUT_SCHEMA",
    "MATCHED_CONTRACT_FIELDS",
    "REQUIRED_LANE_FAMILIES",
    "SCHEMA",
    "audit_current_project_v1",
    "evaluate_fair_baseline_gate_v1",
    "main",
)
