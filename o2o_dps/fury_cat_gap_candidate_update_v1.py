"""Pure candidate-population update for the Cat-gap development search.

The frozen v1 successive-halving plan can only retain members of its initial
64-point design.  This module adds the missing outcome-to-candidate step without
starting work or changing that historical plan: a complete stage reduction
selects incumbents and deterministically creates adjacent 13-axis children for
the next development batch.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import math
from typing import Any, Mapping, Sequence

from .cat2new_fury_cat_gap_policy_v1 import (
    PARAMETER_AXES,
    Cat2NewFuryCatGapPolicyV1Error,
    FuryCatGapPolicyParametersV1,
)
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    sha256_json,
)


JSONMap = dict[str, Any]
SCHEMA = "fury_cat_gap_candidate_update/v1"
REVISION = "v1_elite_frequency_adjacent_lattice_update"
STATUS = "COMPLETE_NEXT_DEVELOPMENT_CANDIDATE_REGISTRY_READY"
REDUCTION_SCHEMA_V1 = "fury_cat_gap_variable_lane_reduction/v1"
REAL_STAGE_EXECUTION_KIND_V1 = "ADMITTED_OLD50_DEVELOPMENT_STAGE"
RANKING_METRIC = "dual_baseline_maximin_equal_instance_weighted_paired_mean_dps"
DEVELOPMENT_REFERENCE_AUDIT_SCHEMA_V1 = (
    "fury_cat_gap_development_optimization_reference_audit/v1"
)
DEVELOPMENT_REFERENCE_AUDIT_READY_V1 = (
    "DEVELOPMENT_OPTIMIZATION_REFERENCES_ELIGIBLE"
)

_CANDIDATE_FIELDS = {
    "candidate_id",
    "parameter_sha256",
    "origin",
    "parameters",
    "legacy_horizon_arm_id",
    "horizon_v2_ranking_used",
}
_TRANSITIONS = {
    "successive_halving_1": (64, "successive_halving_2", 16),
    "successive_halving_2": (16, "successive_halving_3", 4),
    "successive_halving_3": (4, "selection_validation", 2),
}


class FuryCatGapCandidateUpdateV1Error(RuntimeError):
    """A reduction or registry cannot authorize a deterministic update."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapCandidateUpdateV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise FuryCatGapCandidateUpdateV1Error(f"{label} must be an array")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _addressed(core: Mapping[str, Any]) -> JSONMap:
    result = deepcopy(dict(core))
    result["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": sha256_json(core),
    }
    return result


def _validate_content_address(value: Mapping[str, Any], label: str) -> JSONMap:
    document = deepcopy(dict(value))
    address = _mapping(document.pop("content_address", None), f"{label} address")
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON document without content_address"
        or address.get("sha256") != sha256_json(document)
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            f"{label} content address is not canonical"
        )
    return {**document, "content_address": dict(address)}


def _normalize_candidate(value: Mapping[str, Any]) -> JSONMap:
    row = deepcopy(dict(_mapping(value, "candidate")))
    if set(row) != _CANDIDATE_FIELDS:
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate field set differs from the Cat-gap registry contract"
        )
    try:
        parameters = FuryCatGapPolicyParametersV1.from_mapping(
            _mapping(row.get("parameters"), "candidate parameters")
        )
    except Cat2NewFuryCatGapPolicyV1Error as error:
        raise FuryCatGapCandidateUpdateV1Error(str(error)) from error
    normalized = asdict(parameters)
    origin = row.get("origin")
    if (
        row.get("candidate_id") != parameters.candidate_id
        or row.get("parameter_sha256") != parameters.parameter_sha256
        or not isinstance(origin, str)
        or not origin
        or row.get("legacy_horizon_arm_id") is not None
        or row.get("horizon_v2_ranking_used") is not False
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate identity, origin, or no-Horizon boundary is invalid"
        )
    return {
        "candidate_id": parameters.candidate_id,
        "parameter_sha256": parameters.parameter_sha256,
        "origin": origin,
        "parameters": normalized,
        "legacy_horizon_arm_id": None,
        "horizon_v2_ranking_used": False,
    }


def _normalize_registry(value: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate registry must be a sequence"
        )
    rows = [_normalize_candidate(row) for row in value]
    ids = [row["candidate_id"] for row in rows]
    parameters = [row["parameter_sha256"] for row in rows]
    if not rows or len(ids) != len(set(ids)) or len(parameters) != len(set(parameters)):
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate registry must contain unique identities and parameters"
        )
    return rows


def _validate_development_reference_audit(value: Any) -> JSONMap:
    audit = dict(_mapping(value, "development reference audit"))
    expected_fields = {
        "schema",
        "status",
        "reference_policy_ids",
        "references",
        "optimization_reference_eligible",
        "scientific_comparison_eligible",
        "authority",
        "live_or_scientific_promotion_performed",
    }
    if (
        set(audit) != expected_fields
        or audit.get("schema") != DEVELOPMENT_REFERENCE_AUDIT_SCHEMA_V1
        or audit.get("status") != DEVELOPMENT_REFERENCE_AUDIT_READY_V1
        or audit.get("reference_policy_ids")
        != [CAT_POLICY_ID, CONTRA260817_POLICY_ID]
        or audit.get("optimization_reference_eligible") is not True
        or audit.get("scientific_comparison_eligible") is not False
        or audit.get("authority")
        != "EXACT_SOURCE_SIMULATOR_TRANSLATIONS_FOR_DEVELOPMENT_SEARCH_ONLY"
        or audit.get("live_or_scientific_promotion_performed") is not False
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            "development optimization references are absent or ineligible"
        )
    references = _array(audit.get("references"), "development references")
    if (
        len(references) != 2
        or [
            row.get("policy_id") if isinstance(row, Mapping) else None
            for row in references
        ]
        != [CAT_POLICY_ID, CONTRA260817_POLICY_ID]
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            "development optimization reference identities are invalid"
        )
    for raw in references:
        row = _mapping(raw, "development reference")
        if (
            row.get("exact_policy_identity_matches") is not True
            or row.get("exact_lane_contract_matches") is not True
            or row.get("dynamic_v5_executable") is not True
            or row.get("blocker_codes") != []
            or row.get("runtime_artifact_validation_required") is not True
            or row.get("optimization_reference_eligible") is not True
            or row.get("scientific_comparison_eligible") is not False
        ):
            raise FuryCatGapCandidateUpdateV1Error(
                "development optimization reference failed its source-simulator gate"
            )
    return audit


def _prior_registry_state(
    prior_registry: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    current_stage_id: str,
) -> tuple[list[JSONMap], set[str], JSONMap]:
    if isinstance(prior_registry, Mapping):
        receipt = _validate_content_address(prior_registry, "prior update receipt")
        if (
            receipt.get("schema") != SCHEMA
            or receipt.get("status") != STATUS
            or receipt.get("next_stage_id") != current_stage_id
            or receipt.get("complete_accounting") is not True
            or receipt.get("development_only") is not True
            or receipt.get("scientific_result_available") is not False
            or receipt.get("deployment_allowed") is not False
        ):
            raise FuryCatGapCandidateUpdateV1Error(
                "prior update receipt does not bind the current development stage"
            )
        rows = _normalize_registry(
            _array(receipt.get("candidate_registry"), "prior candidate registry")
        )
        if (
            receipt.get("candidate_count") != len(rows)
            or receipt.get("candidate_registry_sha256") != sha256_json(rows)
        ):
            raise FuryCatGapCandidateUpdateV1Error(
                "prior update receipt candidate registry is inconsistent"
            )
        evaluated = _array(
            receipt.get("evaluated_parameter_sha256s"),
            "evaluated parameter identities",
        )
        if (
            evaluated != sorted(evaluated)
            or len(evaluated) != len(set(evaluated))
            or any(not _is_sha256(identity) for identity in evaluated)
        ):
            raise FuryCatGapCandidateUpdateV1Error(
                "prior evaluated parameter history is invalid"
            )
        binding = {
            "kind": "CANDIDATE_UPDATE_RECEIPT",
            "sha256": receipt["content_address"]["sha256"],
            "candidate_registry_sha256": receipt["candidate_registry_sha256"],
        }
        return rows, set(evaluated), binding

    if current_stage_id != "successive_halving_1":
        raise FuryCatGapCandidateUpdateV1Error(
            "later updates require the preceding content-addressed update receipt"
        )
    rows = _normalize_registry(prior_registry)
    registry_sha = sha256_json(rows)
    return rows, set(), {
        "kind": "INITIAL_CANDIDATE_SEQUENCE",
        "sha256": registry_sha,
        "candidate_registry_sha256": registry_sha,
    }


def _validated_ranking(
    reduction: Mapping[str, Any], registry: Sequence[Mapping[str, Any]]
) -> tuple[JSONMap, list[JSONMap]]:
    checked = _validate_content_address(reduction, "stage reduction")
    _validate_development_reference_audit(
        checked.get("development_reference_audit")
    )
    candidate_ids = {str(row["candidate_id"]) for row in registry}
    if (
        checked.get("schema") != REDUCTION_SCHEMA_V1
        or checked.get("status") != "STAGE_COMPLETE_RETENTION_READY"
        or checked.get("execution_kind") != REAL_STAGE_EXECUTION_KIND_V1
        or checked.get("candidate_count") != len(registry)
        or checked.get("baseline_policy_ids")
        != [CAT_POLICY_ID, CONTRA260817_POLICY_ID]
        or checked.get("ranking_metric") != RANKING_METRIC
        or checked.get("ranking_tie_break") != "candidate_id_ascending"
        or checked.get("retention_allowed") is not True
        or checked.get("selection") is not None
        or checked.get("complete_accounting") is not True
        or checked.get("expected_task_count")
        != checked.get("observed_unique_task_count")
        or not isinstance(checked.get("group_count"), int)
        or isinstance(checked.get("group_count"), bool)
        or checked.get("group_count") <= 0
        or checked.get("contrast_count") != 2 * len(registry)
        or checked.get("heavy_execution_started") is not True
        or checked.get("simulator_only") is not True
        or checked.get("development_only") is not True
        or checked.get("old50_heldout_evidence") is not False
        or checked.get("scientific_result_available") is not False
        or checked.get("deployment_allowed") is not False
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            "stage reduction is incomplete or outside the development update boundary"
        )
    raw_ranking = _array(checked.get("candidate_ranking"), "candidate ranking")
    ranking: list[JSONMap] = []
    for value in raw_ranking:
        row = dict(_mapping(value, "candidate ranking row"))
        candidate_id = row.get("candidate_id")
        means = _mapping(
            row.get("equal_instance_weighted_mean_by_baseline"),
            "candidate baseline means",
        )
        score = row.get("dual_baseline_maximin_mean_dps")
        if (
            set(row)
            != {
                "candidate_id",
                "equal_instance_weighted_mean_by_baseline",
                "dual_baseline_maximin_mean_dps",
            }
            or candidate_id not in candidate_ids
            or set(means) != {CAT_POLICY_ID, CONTRA260817_POLICY_ID}
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            raise FuryCatGapCandidateUpdateV1Error(
                "candidate ranking row is noncanonical"
            )
        numeric_means = {key: float(value) for key, value in means.items()}
        if (
            any(not math.isfinite(value) for value in numeric_means.values())
            or float(score) != min(numeric_means.values())
        ):
            raise FuryCatGapCandidateUpdateV1Error(
                "candidate maximin score differs from its paired baseline means"
            )
        ranking.append(
            {
                "candidate_id": candidate_id,
                "equal_instance_weighted_mean_by_baseline": numeric_means,
                "dual_baseline_maximin_mean_dps": float(score),
            }
        )
    if (
        len(ranking) != len(registry)
        or {row["candidate_id"] for row in ranking} != candidate_ids
        or ranking
        != sorted(
            ranking,
            key=lambda row: (
                -row["dual_baseline_maximin_mean_dps"],
                row["candidate_id"],
            ),
        )
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate ranking is incomplete, duplicated, or out of order"
        )
    return checked, ranking


def _new_child(
    parent: Mapping[str, Any],
    *,
    parent_rank: int,
    elite_counts: Mapping[str, Counter[Any]],
    seen_parameters: set[str],
    prior_stage_id: str,
) -> tuple[JSONMap, JSONMap]:
    source = dict(_mapping(parent.get("parameters"), "parent parameters"))
    options: list[tuple[tuple[int, int, int, int], JSONMap, str, Any, Any, int]] = []
    axis_count = len(PARAMETER_AXES)
    for axis_index, (name, values) in enumerate(PARAMETER_AXES):
        current = source[name]
        current_index = values.index(current)
        current_support = elite_counts[name][current]
        for next_index in (current_index - 1, current_index + 1):
            if next_index < 0 or next_index >= len(values):
                continue
            destination = values[next_index]
            parameters = {**source, name: destination}
            parsed = FuryCatGapPolicyParametersV1.from_mapping(parameters)
            parameter_sha = parsed.parameter_sha256
            if parameter_sha in seen_parameters:
                continue
            destination_support = elite_counts[name][destination]
            improvement = destination_support - current_support
            rotated_axis = (axis_index - parent_rank) % axis_count
            preference = (
                -improvement,
                -destination_support,
                rotated_axis,
                next_index,
            )
            options.append(
                (
                    preference,
                    asdict(parsed),
                    name,
                    current,
                    destination,
                    destination_support,
                )
            )
    if not options:
        raise FuryCatGapCandidateUpdateV1Error(
            f"no unseen adjacent lattice child is available for {parent['candidate_id']}"
        )
    _, parameters, axis, source_value, destination_value, support = min(
        options, key=lambda row: row[0]
    )
    parsed = FuryCatGapPolicyParametersV1.from_mapping(parameters)
    seen_parameters.add(parsed.parameter_sha256)
    child = {
        "candidate_id": parsed.candidate_id,
        "parameter_sha256": parsed.parameter_sha256,
        "origin": "adaptive_elite_frequency_adjacent_neighbor_v1",
        "parameters": parameters,
        "legacy_horizon_arm_id": None,
        "horizon_v2_ranking_used": False,
    }
    lineage = {
        "candidate_id": parsed.candidate_id,
        "parent_candidate_id": parent["candidate_id"],
        "source_stage_id": prior_stage_id,
        "mutated_axis": axis,
        "source_value": source_value,
        "destination_value": destination_value,
        "elite_destination_count": support,
        "single_axis_adjacent_lattice_step": True,
    }
    return child, lineage


def build_next_candidate_registry_v1(
    prior_reduction: Mapping[str, Any],
    prior_registry: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    next_stage_id: str,
    next_candidate_count: int,
) -> JSONMap:
    """Build one deterministic update receipt; no rollout or dispatch is started.

    The initial call accepts the frozen candidate sequence.  Later calls must
    pass the preceding receipt so candidates discarded in earlier stages remain
    in the all-time evaluated-parameter exclusion set.
    """

    stage_id = prior_reduction.get("stage_id")
    transition = _TRANSITIONS.get(stage_id)
    if (
        transition is None
        or isinstance(next_candidate_count, bool)
        or transition[1:] != (next_stage_id, next_candidate_count)
    ):
        raise FuryCatGapCandidateUpdateV1Error(
            "requested next stage/count is not the registered Cat-gap transition"
        )
    registry, prior_evaluated, registry_binding = _prior_registry_state(
        prior_registry,
        current_stage_id=str(stage_id),
    )
    if len(registry) != transition[0]:
        raise FuryCatGapCandidateUpdateV1Error(
            "prior registry count differs from the registered Cat-gap stage"
        )
    reduction, ranking = _validated_ranking(prior_reduction, registry)
    retained = _array(
        reduction.get("retained_candidate_ids"), "retained candidate IDs"
    )
    expected_elites = [row["candidate_id"] for row in ranking[:next_candidate_count]]
    if retained != expected_elites:
        raise FuryCatGapCandidateUpdateV1Error(
            "reduction retained IDs are not the exact registered elite prefix"
        )

    by_id = {row["candidate_id"]: row for row in registry}
    evaluated = prior_evaluated | {
        str(row["parameter_sha256"]) for row in registry
    }
    elite_rows = [by_id[candidate_id] for candidate_id in expected_elites]
    elite_counts = {
        name: Counter(row["parameters"][name] for row in elite_rows)
        for name, _ in PARAMETER_AXES
    }
    selection_freeze = next_stage_id == "selection_validation"
    incumbent_count = (
        next_candidate_count if selection_freeze else next_candidate_count // 2
    )
    novel_child_count = next_candidate_count - incumbent_count
    incumbents = elite_rows[:incumbent_count]
    seen = set(evaluated)
    children: list[JSONMap] = []
    lineage: list[JSONMap] = []
    for parent_rank, parent in enumerate(incumbents[:novel_child_count]):
        child, child_lineage = _new_child(
            parent,
            parent_rank=parent_rank,
            elite_counts=elite_counts,
            seen_parameters=seen,
            prior_stage_id=str(stage_id),
        )
        children.append(child)
        lineage.append(child_lineage)
    next_registry = [*deepcopy(incumbents), *children]
    if len(next_registry) != next_candidate_count:
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate update did not produce the exact next-stage population"
        )

    core = {
        "schema": SCHEMA,
        "revision": REVISION,
        "status": STATUS,
        "source_stage_id": stage_id,
        "next_stage_id": next_stage_id,
        "prior_reduction_sha256": reduction["content_address"]["sha256"],
        "prior_registry_binding": registry_binding,
        "update_rule": {
            "ranking_metric": RANKING_METRIC,
            "elite_pool_count": next_candidate_count,
            "incumbent_count": incumbent_count,
            "novel_child_count": novel_child_count,
            "incumbents": (
                "exact evaluated top two; no new candidate enters selection"
                if selection_freeze
                else "top half of next-stage count by frozen ranking"
            ),
            "mutation": (
                "disabled at the selection boundary"
                if selection_freeze
                else (
                    "one unseen adjacent 13-axis lattice step per incumbent; prefer "
                    "largest elite-frequency improvement, then destination frequency, "
                    "parent-rank-rotated axis order, and lower axis value index"
                )
            ),
            "source_reduction_outcome_drives_parent_and_elite_selection": True,
            "additional_outcome_lookup": False,
        },
        "candidate_count": len(next_registry),
        "candidate_registry": next_registry,
        "candidate_registry_sha256": sha256_json(next_registry),
        "incumbent_candidate_ids": [row["candidate_id"] for row in incumbents],
        "novel_candidate_ids": [row["candidate_id"] for row in children],
        "candidate_lineage": lineage,
        "evaluated_parameter_sha256s": sorted(evaluated),
        "next_stage_evaluation_required_before_further_update": True,
        "complete_accounting": True,
        "development_only": True,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _addressed(core)


def validate_candidate_update_receipt_v1(
    value: Mapping[str, Any],
    prior_reduction: Mapping[str, Any],
    prior_registry: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    next_stage_id: str,
    next_candidate_count: int,
) -> JSONMap:
    """Recompute and return the exact pure update receipt."""

    expected = build_next_candidate_registry_v1(
        prior_reduction,
        prior_registry,
        next_stage_id=next_stage_id,
        next_candidate_count=next_candidate_count,
    )
    observed = deepcopy(dict(_mapping(value, "candidate update receipt")))
    if observed != expected:
        raise FuryCatGapCandidateUpdateV1Error(
            "candidate update receipt differs from the deterministic update"
        )
    return expected


__all__ = (
    "FuryCatGapCandidateUpdateV1Error",
    "REVISION",
    "SCHEMA",
    "STATUS",
    "build_next_candidate_registry_v1",
    "validate_candidate_update_receipt_v1",
)
