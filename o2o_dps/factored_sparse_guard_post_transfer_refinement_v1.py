"""Development-only refinement after a negative sparse-guard transfer wave.

The transfer run evaluated each parent guard as an actual full-wave policy.
This module uses the current-observation features captured at that parent's
single intervention to rank one extra predicate.  Its projected effect is the
parent's measured delta when that extra predicate matched and exact zero
otherwise.  That projection is deliberately *not* called the refined policy's
effect: a normal refined guard can skip the parent's first match and first
match later in the wave.

Only the predeclared slow-DW, single-target, 50k-to-200k hypothesis is
considered, and at most one depth-two ``SparseGuardV2`` is frozen globally.
The frozen artifact is only a policy source for a disjoint untouched-fresh
full-wave run.  The fresh reducer below reports the actual policy effect from
``CatSparseGuardPolicyV2`` lanes and never retroactively labels the refinement
as transfer-authorized.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .cat_sparse_guard_policy_v2 import FEATURE_ORDER, SparseGuardV2
from .factored_cat_branch_router_v1 import MechanismRouteV1
from .factored_external_press_matrix_v1 import (
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
    SPARSE_SHORTLIST_SCHEMA,
    _load_item_database,
)
from .factored_sparse_guard_full_wave_v1 import (
    AUTHORIZATION_SCHEMA,
    FRESH_PHASE,
    TRANSFER_PHASE,
    _authorized_route_guards,
    _effect_statistics,
    _guard_from_wire,
    _guard_key,
    _pair_valid_effect,
    _phase_reductions,
    _route_from_wire,
    _route_key,
    _route_relative_guard_key,
    _seed_in_phase,
    _shared_receipts_valid,
    _validated_phase_artifacts,
    validate_sparse_shortlist_v5,
)
from .factored_sparse_guard_learner_v2 import SCHEMA as SPARSE_LEARNER_SCHEMA


SCHEMA = "factored_sparse_guard_post_transfer_refinement/v1"
FRESH_RESULT_SCHEMA = "factored_sparse_guard_post_transfer_refinement_fresh/v1"
MIN_SUPPORT_SEEDS = 6
TARGET_PARENT_GUARD = SparseGuardV2("ADD_HS_QUEUE", "hp_phase", "MIDDLE")
TARGET_REFINED_GUARD = SparseGuardV2(
    "ADD_HS_QUEUE", "hp_phase", "MIDDLE", "flurry_state", "INACTIVE",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _depth(guard: SparseGuardV2) -> int:
    return 1 + int(guard.second_feature is not None)


def _target_route(route: MechanismRouteV1) -> bool:
    return bool(
        route.weapon_mode == "DUAL_WIELD"
        and route.main_hand_speed_band == "slow_2s_plus"
        and route.target_count_band == "one"
        and route.modeled_hp_budget_band == "50k_to_200k"
    )


def _refined_guard(
    parent: SparseGuardV2, feature: str, value: str,
) -> SparseGuardV2:
    predicates = [(parent.first_feature, parent.first_value), (feature, value)]
    predicates.sort(key=lambda row: FEATURE_ORDER.index(str(row[0])))
    return SparseGuardV2(
        parent.kind,
        str(predicates[0][0]), str(predicates[0][1]),
        str(predicates[1][0]), str(predicates[1][1]),
    )


def _complete_current_features(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or tuple(value) != tuple(FEATURE_ORDER):
        return None
    if not all(isinstance(value[name], str) for name in FEATURE_ORDER):
        return None
    return dict(value)


def _shortlist_authorization_decisions(
    shortlist_routes: Mapping[MechanismRouteV1, tuple[SparseGuardV2, ...]],
    authorization: Mapping[str, Any],
) -> dict[tuple[tuple[Any, ...], tuple[Any, ...]], Mapping[str, Any]]:
    """Bind every transfer decision back to exactly one shortlisted parent."""

    _authorized_route_guards(authorization)
    expected = {
        (_route_key(route), _guard_key(guard))
        for route, guards in shortlist_routes.items() for guard in guards
    }
    result: dict[tuple[tuple[Any, ...], tuple[Any, ...]], Mapping[str, Any]] = {}
    for raw in authorization.get("guard_authorizations") or []:
        if not isinstance(raw, Mapping):
            raise ValueError("transfer authorization decision is malformed")
        route = _route_from_wire(raw.get("mechanism_route"))
        guard = _guard_from_wire(raw.get("guard"))
        key = (_route_key(route), _guard_key(guard))
        if key in result:
            raise ValueError("transfer authorization repeats a parent guard decision")
        result[key] = raw
    if set(result) != expected:
        raise ValueError("transfer decisions differ from the v5 shortlist")
    return result


def _candidate_projection(
    artifacts: Iterable[Mapping[str, Any]],
    *,
    route: MechanismRouteV1,
    parent: SparseGuardV2,
    child: SparseGuardV2,
    added_feature: str,
    added_value: str,
) -> dict[str, Any]:
    """Project a child from its parent's observed intervention receipts.

    Unknown parent case effects stay unknown.  A complete parent intervention
    contributes its actual delta only when its current matched feature equals
    the added predicate; all other complete cases contribute exact zero to the
    exploratory projection.
    """

    by_seed: dict[int, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        if artifact.get("mechanism_route") != asdict(route):
            continue
        pair = next(
            row for row in artifact["result"]["guard_pairs"]
            if row.get("guard") == asdict(parent)
        )
        effect = _pair_valid_effect(
            pair,
            shared_receipts_valid=_shared_receipts_valid(artifact["result"]),
        )
        features = None
        if pair.get("effect_class") == "TRIGGERED_INTERVENTION":
            intervention = pair.get("candidate_intervention")
            if isinstance(intervention, Mapping):
                features = _complete_current_features(
                    intervention.get("matched_features")
                )
        matches = bool(
            effect is not None
            and features is not None
            and features.get(added_feature) == added_value
        )
        # A triggered row without a complete current feature receipt cannot be
        # safely assigned to either side of the predicate.
        invalid_features = bool(
            effect is not None
            and pair.get("effect_class") == "TRIGGERED_INTERVENTION"
            and features is None
        )
        by_seed.setdefault(artifact["matrix"]["seed"], []).append({
            "effect": None if invalid_features else effect,
            "matches": matches,
            "projected": float(effect) if matches else 0.0,
        })

    complete: dict[int, float] = {}
    matched_seeds: set[int] = set()
    per_seed = []
    for seed, rows in sorted(by_seed.items()):
        unknown = sum(row["effect"] is None for row in rows)
        matched = sum(row["matches"] for row in rows)
        value = (
            sum(row["projected"] for row in rows) / len(rows)
            if rows and unknown == 0 else None
        )
        if value is not None:
            complete[seed] = value
        if matched:
            matched_seeds.add(seed)
        per_seed.append({
            "seed": seed,
            "status": (
                "COMPLETE_EXPLORATORY_PARENT_DELTA_PROJECTION"
                if value is not None else "UNKNOWN_PARENT_CASE_NOT_IMPUTED"
            ),
            "assigned_case_count": len(rows),
            "matching_parent_intervention_case_count": matched,
            "unknown_case_count": unknown,
            "exploratory_projected_effective_damage_delta": value,
        })
    return {
        "mechanism_route": asdict(route),
        "parent_guard": asdict(parent),
        "refined_guard": asdict(child),
        "added_predicate": {"feature": added_feature, "value": added_value},
        "assigned_seed_count": len(by_seed),
        "complete_seed_count": len(complete),
        "unknown_seed_count": sum(
            row["unknown_case_count"] > 0 for row in per_seed
        ),
        "matching_parent_intervention_seed_count": len(matched_seeds),
        "per_seed_exploratory_projections": per_seed,
        "exploratory_projected_effect_statistics": _effect_statistics(
            complete.values()
        ),
        "exploratory_projection_contract": (
            "PARENT_ACTUAL_FULL_WAVE_DELTA_IF_ADDED_CURRENT_OBSERVATION_"
            "PREDICATE_MATCHED_AT_PARENT_INTERVENTION_ELSE_EXACT_ZERO;"
            "ANY_UNKNOWN_PARENT_CASE_MAKES_SEED_UNKNOWN;NO_UNKNOWN_ZERO_IMPUTATION"
        ),
        "exploratory_projection_is_exact_refined_policy_effect": False,
        "reason_not_exact": (
            "A_STANDARD_REFINED_GUARD_MAY_SKIP_THE_PARENT_FIRST_MATCH_AND_"
            "FIRST_MATCH_LATER_IN_THE_WAVE"
        ),
    }


def _projection_passes(row: Mapping[str, Any], minimum: int) -> bool:
    stats = row["exploratory_projected_effect_statistics"]
    return bool(
        row["matching_parent_intervention_seed_count"] >= minimum
        and row["complete_seed_count"] == row["assigned_seed_count"]
        and row["unknown_seed_count"] == 0
        and stats["lower_95_normal_effective_damage_delta_bound"] is not None
        and stats["lower_95_normal_effective_damage_delta_bound"] > 0
    )


def _candidate_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    stats = row["exploratory_projected_effect_statistics"]
    guard = _guard_from_wire(row["refined_guard"])
    return (
        -stats["lower_95_normal_effective_damage_delta_bound"],
        -stats["mean_paired_effective_damage_delta"],
        -row["matching_parent_intervention_seed_count"],
        tuple("" if value is None else str(value) for value in _guard_key(guard)),
    )


def build_post_transfer_refinement_v1(
    shortlist_artifact: Mapping[str, Any],
    authorization_artifact: Mapping[str, Any],
    transfer_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
    min_support_seeds: int = MIN_SUPPORT_SEEDS,
) -> dict[str, Any]:
    """Freeze the single predeclared exploratory refinement, if supported."""

    if type(min_support_seeds) is not int or min_support_seeds < MIN_SUPPORT_SEEDS:
        raise ValueError("refinement support may not be lower than six seeds")
    routes = validate_sparse_shortlist_v5(shortlist_artifact)
    decisions = _shortlist_authorization_decisions(routes, authorization_artifact)
    if sorted(shortlist_artifact.get("training_seeds") or []) != sorted(
        authorization_artifact.get("training_seeds") or []
    ):
        raise ValueError("refinement shortlist and authorization training seeds differ")
    rows = _validated_phase_artifacts(
        transfer_artifacts,
        phase=TRANSFER_PHASE,
        policy_artifact=shortlist_artifact,
        item_database=item_database,
    )
    transfer_seeds = sorted({row["matrix"]["seed"] for row in rows})
    if transfer_seeds != sorted(authorization_artifact.get("transfer_seeds") or []):
        raise ValueError("refinement transfer cases differ from authorization seeds")
    if sorted({row["matrix"]["sample_index"] for row in rows}) != sorted(
        authorization_artifact.get("sample_indices") or []
    ):
        raise ValueError("refinement transfer sample indices differ from authorization")
    if len(rows) != authorization_artifact.get("transfer_case_count"):
        raise ValueError("refinement transfer case count differs from authorization")

    diagnostics: list[dict[str, Any]] = []
    candidate_keys: set[tuple[tuple[Any, ...], tuple[Any, ...]]] = set()
    for route in sorted(routes, key=_route_key):
        # ``validate_sparse_shortlist_v5`` already rejects route-relative
        # duplicates; keep this local set so later producer schemas cannot
        # accidentally reintroduce equivalent parents.
        parent_keys: set[tuple[Any, ...]] = set()
        for parent in routes[route]:
            parent_relative = _route_relative_guard_key(route, parent)
            if parent_relative in parent_keys:
                continue
            parent_keys.add(parent_relative)
            decision = decisions[(_route_key(route), _guard_key(parent))]
            if (
                decision.get("authorized_for_fresh") is True
                or _depth(parent) != 1
                or parent != TARGET_PARENT_GUARD
                or not _target_route(route)
            ):
                continue
            observed_values: dict[str, set[str]] = {}
            for artifact in rows:
                if artifact.get("mechanism_route") != asdict(route):
                    continue
                pair = next(
                    candidate for candidate in artifact["result"]["guard_pairs"]
                    if candidate.get("guard") == asdict(parent)
                )
                if _pair_valid_effect(
                    pair,
                    shared_receipts_valid=_shared_receipts_valid(artifact["result"]),
                ) is None or pair.get("effect_class") != "TRIGGERED_INTERVENTION":
                    continue
                intervention = pair.get("candidate_intervention")
                features = _complete_current_features(
                    intervention.get("matched_features")
                    if isinstance(intervention, Mapping) else None
                )
                if features is None:
                    continue
                observed_values.setdefault("flurry_state", set()).add(
                    features["flurry_state"]
                )
            for feature in ("flurry_state",):
                for value in ("INACTIVE",):
                    if value not in observed_values.get(feature, ()):
                        continue
                    child = _refined_guard(parent, feature, value)
                    key = (_route_key(route), _guard_key(child))
                    if key in candidate_keys:
                        continue
                    candidate_keys.add(key)
                    diagnostics.append(_candidate_projection(
                        rows, route=route, parent=parent, child=child,
                        added_feature=feature, added_value=value,
                    ))

    selected: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for row in diagnostics:
        row["passed_exploratory_refinement_gate"] = _projection_passes(
            row, min_support_seeds,
        )
        if row["unknown_seed_count"]:
            status = "REJECTED_UNKNOWN_PARENT_EFFECTS_NOT_IMPUTED"
        elif row["matching_parent_intervention_seed_count"] < min_support_seeds:
            status = "REJECTED_INSUFFICIENT_MATCHED_SUPPORT"
        elif row["exploratory_projected_effect_statistics"][
            "lower_95_normal_effective_damage_delta_bound"
        ] is None:
            status = "REJECTED_EXPLORATORY_LOWER_BOUND_UNDEFINED"
        elif not row["passed_exploratory_refinement_gate"]:
            status = "REJECTED_EXPLORATORY_LOWER_BOUND_NOT_POSITIVE"
        else:
            status = "PASSED_EXPLORATORY_GATE_PENDING_GLOBAL_CAP"
            passed.append(row)
        row["refinement_gate_status"] = status
        row["selected_for_untouched_fresh_evaluation"] = False

    if passed:
        winner = sorted(passed, key=_candidate_sort_key)[0]
        winner["refinement_gate_status"] = (
            "FROZEN_FOR_UNTOUCHED_FRESH_ACTUAL_POLICY_EVALUATION_NONVOTING"
        )
        winner["selected_for_untouched_fresh_evaluation"] = True
        selected.append(winner)
        for row in passed:
            if row is not winner:
                row["refinement_gate_status"] = "PASSED_GATE_BUT_REMOVED_BY_GLOBAL_CAP"

    selected.sort(key=lambda row: _route_key(_route_from_wire(row["mechanism_route"])))
    refined_routes = [{
        "mechanism_route": deepcopy(row["mechanism_route"]),
        "guards": [deepcopy(row["refined_guard"])],
    } for row in selected]
    semantic = deepcopy(authorization_artifact.get("execution_semantic_contract"))
    if (
        not isinstance(semantic, Mapping)
        or semantic.get("required_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    ):
        raise ValueError("transfer authorization lacks its execution semantics")
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "DEVELOPMENT_REFINEMENT_FROZEN_FOR_UNTOUCHED_FRESH_NONVOTING"
            if selected else "NO_EXPLORATORY_REFINEMENT_PASSED_NONVOTING"
        ),
        "source_shortlist_schema": SPARSE_SHORTLIST_SCHEMA,
        "source_learner_schema": SPARSE_LEARNER_SCHEMA,
        "source_transfer_authorization_schema": AUTHORIZATION_SCHEMA,
        "training_seeds": sorted(shortlist_artifact.get("training_seeds") or []),
        "transfer_seeds": transfer_seeds,
        "transfer_sample_indices": sorted({
            row["matrix"]["sample_index"] for row in rows
        }),
        "transfer_case_count": len(rows),
        "execution_semantic_contract": semantic,
        "candidate_diagnostics": diagnostics,
        "candidate_count": len(diagnostics),
        "refined_routes": refined_routes,
        "frozen_refined_guard_count": len(selected),
        "minimum_matching_parent_intervention_seed_support": min_support_seeds,
        "selection_contract": (
            "SINGLE_PREDECLARED_POST_TRANSFER_HYPOTHESIS_ONLY;SLOW_DUAL_WIELD_"
            "SINGLE_TARGET_50K_TO_200K;ADD_HS_QUEUE;HP_PHASE_MIDDLE_AND_"
            "FLURRY_STATE_INACTIVE;CURRENT_OBSERVABLE_MATCHED_FEATURES_ONLY;"
            "SUPPORT_AT_LEAST_SIX_SEEDS;POSITIVE_EXPLORATORY_LOWER_95_NORMAL_"
            "BOUND;AT_MOST_ONE_GUARD_GLOBALLY"
        ),
        "exploratory_projection_is_exact_refined_policy_effect": False,
        "standard_refined_guard_actual_full_wave_evaluated": False,
        "prior_full_wave_authorization_claimed": False,
        "fresh_evaluation_eligible": bool(selected),
        "unknown_effect_imputed": False,
        "positive_seed_fraction_role": "DIAGNOSTIC_ONLY_NOT_A_SELECTION_GATE",
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def validate_post_transfer_refinement_v1(
    artifact: Mapping[str, Any],
) -> dict[MechanismRouteV1, tuple[SparseGuardV2, ...]]:
    """Validate a frozen refinement as a fresh-evaluation policy source."""

    if (
        not isinstance(artifact, Mapping)
        or artifact.get("schema") != SCHEMA
        or artifact.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or artifact.get("source_shortlist_schema") != SPARSE_SHORTLIST_SCHEMA
        or artifact.get("source_learner_schema") != SPARSE_LEARNER_SCHEMA
        or artifact.get("source_transfer_authorization_schema") != AUTHORIZATION_SCHEMA
        or artifact.get("standard_refined_guard_actual_full_wave_evaluated") is not False
        or artifact.get("prior_full_wave_authorization_claimed") is not False
        or artifact.get("unknown_effect_imputed") is not False
        or artifact.get("raw_chronicle_rows_loaded") is not False
        or artifact.get("voting_eligible") is not False
        or artifact.get("deployment_eligible") is not False
    ):
        raise ValueError("post-transfer refinement schema or scope differs")
    training = artifact.get("training_seeds")
    transfer = artifact.get("transfer_seeds")
    semantic = artifact.get("execution_semantic_contract")
    if (
        not isinstance(training, list) or len(training) != len(set(training))
        or any(type(seed) is not int for seed in training)
        or not isinstance(transfer, list) or len(transfer) != len(set(transfer))
        or any(not _seed_in_phase(seed, TRANSFER_PHASE) for seed in transfer)
        or set(training) & set(transfer)
        or not isinstance(semantic, Mapping)
        or semantic.get("required_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    ):
        raise ValueError("post-transfer refinement provenance is malformed")
    routes: dict[MechanismRouteV1, tuple[SparseGuardV2, ...]] = {}
    selected_keys: set[tuple[tuple[Any, ...], tuple[Any, ...]]] = set()
    for row in artifact.get("refined_routes") or []:
        if not isinstance(row, Mapping):
            raise ValueError("refined route row is malformed")
        route = _route_from_wire(row.get("mechanism_route"))
        raw_guards = row.get("guards")
        if route in routes or not isinstance(raw_guards, list) or len(raw_guards) != 1:
            raise ValueError("refinement must freeze at most one guard per route")
        guard = _guard_from_wire(raw_guards[0])
        if (
            not _target_route(route)
            or guard != TARGET_REFINED_GUARD
            or _depth(guard) != 2
            or "weapon_mode" in {
            guard.first_feature, guard.second_feature,
            }
        ):
            raise ValueError("refined guard must add one non-route-implied predicate")
        routes[route] = (guard,)
        selected_keys.add((_route_key(route), _guard_key(guard)))
    diagnostics = artifact.get("candidate_diagnostics")
    minimum = artifact.get("minimum_matching_parent_intervention_seed_support")
    if (
        not isinstance(diagnostics, list)
        or artifact.get("candidate_count") != len(diagnostics)
        or type(minimum) is not int
        or minimum < MIN_SUPPORT_SEEDS
    ):
        raise ValueError("refinement candidate diagnostics are incomplete")
    recorded = set()
    recorded_count = 0
    for row in diagnostics:
        if not isinstance(row, Mapping):
            raise ValueError("refinement candidate diagnostic is malformed")
        if row.get("exploratory_projection_is_exact_refined_policy_effect") is not False:
            raise ValueError("refinement incorrectly claims an exact projected policy effect")
        if row.get("selected_for_untouched_fresh_evaluation") is True:
            recorded_count += 1
            route = _route_from_wire(row.get("mechanism_route"))
            guard = _guard_from_wire(row.get("refined_guard"))
            if not _projection_passes(
                row, minimum
            ):
                raise ValueError("selected refinement does not pass its exploratory gate")
            recorded.add((_route_key(route), _guard_key(guard)))
    if (
        len(selected_keys) > 1
        or
        recorded != selected_keys
        or recorded_count != len(recorded)
        or artifact.get("frozen_refined_guard_count") != len(selected_keys)
        or artifact.get("fresh_evaluation_eligible") is not bool(selected_keys)
    ):
        raise ValueError("refinement frozen route inventory differs from diagnostics")
    expected_status = (
        "DEVELOPMENT_REFINEMENT_FROZEN_FOR_UNTOUCHED_FRESH_NONVOTING"
        if selected_keys else "NO_EXPLORATORY_REFINEMENT_PASSED_NONVOTING"
    )
    if artifact.get("status") != expected_status:
        raise ValueError("refinement status differs from its frozen inventory")
    return routes


def reduce_refined_sparse_guard_fresh_v1(
    refinement_artifact: Mapping[str, Any],
    fresh_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
    min_distinct_seeds: int = 64,
) -> dict[str, Any]:
    """Reduce actual refined-policy lanes on the untouched 265e9 namespace."""

    if type(min_distinct_seeds) is not int or min_distinct_seeds < 64:
        raise ValueError("refined fresh support may not be lower than 64 seeds")
    routes = validate_post_transfer_refinement_v1(refinement_artifact)
    if not routes:
        raise ValueError("fresh refinement evaluation requires a frozen guard")
    rows = _validated_phase_artifacts(
        fresh_artifacts,
        phase=FRESH_PHASE,
        policy_artifact=refinement_artifact,
        item_database=item_database,
    )
    fresh_seeds = sorted({row["matrix"]["seed"] for row in rows})
    prior = set(refinement_artifact.get("training_seeds") or []) | set(
        refinement_artifact.get("transfer_seeds") or []
    )
    if prior & set(fresh_seeds):
        raise ValueError("untouched fresh seeds overlap refinement development phases")
    results = _phase_reductions(rows, routes)
    for row in results:
        stats = row["expected_route_policy_effect_statistics"]
        passed = bool(
            row["assigned_seed_count"] >= min_distinct_seeds
            and row["complete_seed_count"] == row["assigned_seed_count"]
            and row["unknown_seed_count"] == 0
            and stats["lower_95_normal_effective_damage_delta_bound"] is not None
            and stats["lower_95_normal_effective_damage_delta_bound"] > 0
        )
        if row["unknown_seed_count"]:
            status = "UNKNOWN_FRESH_ACTUAL_POLICY_EFFECTS_NOT_IMPUTED"
        elif row["assigned_seed_count"] < min_distinct_seeds:
            status = "INSUFFICIENT_UNTOUCHED_FRESH_ACTUAL_POLICY_SEEDS"
        elif stats["lower_95_normal_effective_damage_delta_bound"] is None:
            status = "UNTOUCHED_FRESH_ACTUAL_POLICY_LOWER_BOUND_UNDEFINED"
        elif not passed:
            status = "UNTOUCHED_FRESH_ACTUAL_POLICY_LOWER_BOUND_NOT_POSITIVE"
        else:
            status = "PASSED_UNTOUCHED_FRESH_ACTUAL_POLICY_GATE_NONVOTING"
        row["fresh_actual_policy_gate_status"] = status
        row["passed_untouched_fresh_actual_policy_gate"] = passed
    all_complete = all(row["unknown_seed_count"] == 0 for row in results)
    return {
        "schema": FRESH_RESULT_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_REFINED_GUARD_UNTOUCHED_FRESH_NONVOTING"
            if all_complete else "INCOMPLETE_REFINED_GUARD_UNTOUCHED_FRESH_NONVOTING"
        ),
        "source_refinement_schema": SCHEMA,
        "training_seeds": sorted(refinement_artifact.get("training_seeds") or []),
        "transfer_seeds": sorted(refinement_artifact.get("transfer_seeds") or []),
        "fresh_seeds": fresh_seeds,
        "fresh_sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "fresh_case_count": len(rows),
        "execution_semantic_contract": deepcopy(rows[0]["execution_contract"]["semantic"]),
        "guard_results": results,
        "evaluated_guard_count": len(results),
        "passed_fresh_guard_count": sum(
            row["passed_untouched_fresh_actual_policy_gate"] for row in results
        ),
        "standard_refined_guard_actual_full_wave_evaluated": True,
        "prior_full_wave_authorization_claimed": False,
        "fresh_gate": (
            "COMPLETE_BALANCED_SAME_SEED_ACTUAL_POLICY_PAIRED_EFFECT;"
            "POSITIVE_LOWER_95_NORMAL_BOUND;POSITIVE_SEED_FRACTION_DIAGNOSTIC_ONLY"
        ),
        "all_semantic_terminal_clock_receipts_valid": all_complete,
        "comparison_ready": bool(results and all_complete),
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    refine = commands.add_parser("refine")
    refine.add_argument("--shortlist", type=Path, required=True)
    refine.add_argument("--authorization", type=Path, required=True)
    refine.add_argument("--input", type=Path, nargs="+", required=True)
    refine.add_argument("--item-db", type=Path, required=True)
    refine.add_argument("--min-support-seeds", type=int, default=MIN_SUPPORT_SEEDS)
    refine.add_argument("--output", type=Path, required=True)

    fresh = commands.add_parser("fresh")
    fresh.add_argument("--refinement", type=Path, required=True)
    fresh.add_argument("--input", type=Path, nargs="+", required=True)
    fresh.add_argument("--item-db", type=Path, required=True)
    fresh.add_argument("--min-distinct-seeds", type=int, default=64)
    fresh.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    item_database = _load_item_database(args.item_db)
    if args.command == "refine":
        result = build_post_transfer_refinement_v1(
            _read_json(args.shortlist), _read_json(args.authorization),
            [_read_json(path) for path in args.input],
            item_database=item_database,
            min_support_seeds=args.min_support_seeds,
        )
    else:
        result = reduce_refined_sparse_guard_fresh_v1(
            _read_json(args.refinement),
            [_read_json(path) for path in args.input],
            item_database=item_database,
            min_distinct_seeds=args.min_distinct_seeds,
        )
    _write_json(args.output, result)


__all__ = (
    "SCHEMA", "FRESH_RESULT_SCHEMA", "MIN_SUPPORT_SEEDS",
    "TARGET_PARENT_GUARD", "TARGET_REFINED_GUARD",
    "build_post_transfer_refinement_v1",
    "validate_post_transfer_refinement_v1",
    "reduce_refined_sparse_guard_fresh_v1",
)


if __name__ == "__main__":
    main()
