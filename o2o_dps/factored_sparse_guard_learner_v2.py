"""Learn route-local sparse Cat-relative guards from branch-teacher artifacts.

This learner produces a development shortlist, not a gameplay policy or an
authorization.  Cat remains the permanent zero-residual arm.  Reward labels
come only from completed, accepted, single-intervention teacher branches;
guard reachability comes independently from each teacher's complete baseline
opportunity inventory.
"""

from __future__ import annotations

from dataclasses import asdict
from itertools import combinations
import math
from statistics import mean, stdev
from typing import Any, Iterable, Mapping

from .cat_action_branch_search_v1 import BRANCH_KINDS
from .cat_sparse_guard_policy_v2 import (
    FEATURE_ORDER,
    SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
    SparseGuardV2,
    sparse_guard_features_v2,
)
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .factored_cat_branch_router_v1 import MechanismRouteV1, mechanism_route_v1


SCHEMA = "factored_sparse_guard_learner/v5"
_FOLD_COUNT = 3


def _route_key(route: MechanismRouteV1) -> tuple[Any, ...]:
    return tuple(asdict(route).values())


def _guard_key(guard: SparseGuardV2) -> tuple[Any, ...]:
    return tuple(asdict(guard).values())


def _guard_sort_key(guard: SparseGuardV2) -> tuple[str, ...]:
    return tuple("" if value is None else str(value) for value in _guard_key(guard))


def _guard_depth(guard: SparseGuardV2) -> int:
    return 0 if guard.kind is None else 1 + int(guard.second_feature is not None)


def _guard_from_items(
    kind: str, items: tuple[tuple[str, str], ...],
) -> SparseGuardV2:
    ordered = tuple(sorted(items, key=lambda item: FEATURE_ORDER.index(item[0])))
    if len(ordered) == 1:
        return SparseGuardV2(kind, ordered[0][0], ordered[0][1])
    if len(ordered) == 2:
        return SparseGuardV2(
            kind, ordered[0][0], ordered[0][1], ordered[1][0], ordered[1][1],
        )
    raise ValueError("sparse guards must have depth one or two")


def _features_match(guard: SparseGuardV2, features: Mapping[str, str]) -> bool:
    return bool(
        guard.kind is not None
        and features.get(guard.first_feature) == guard.first_value
        and (
            guard.second_feature is None
            or features.get(guard.second_feature) == guard.second_value
        )
    )


def _label_statistics(values: Iterable[float]) -> dict[str, Any]:
    materialized = list(values)
    if not materialized:
        return {
            "distinct_seed_count": 0,
            "mean_paired_effective_damage_delta": None,
            "lower_95_normal_effective_damage_delta_bound": None,
            "positive_seed_fraction": None,
        }
    average = mean(materialized)
    lower = (
        average - 1.96 * stdev(materialized) / math.sqrt(len(materialized))
        if len(materialized) >= 2 else None
    )
    return {
        "distinct_seed_count": len(materialized),
        "mean_paired_effective_damage_delta": average,
        "lower_95_normal_effective_damage_delta_bound": lower,
        "positive_seed_fraction": (
            sum(value > 0 for value in materialized) / len(materialized)
        ),
    }


def _teacher_matches_case(
    case: DevelopmentWaveCaseV1, teacher: Mapping[str, Any],
) -> bool:
    return bool(
        teacher.get("seed") == case.dynamic_load.seed
        and teacher.get("source_wave_ref") == case.case_spec["source_wave_ref"]
        and teacher.get("request_sha256") == case.dynamic_load.request_sha256
        and teacher.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
    )


def _accepted_branch_rows(teacher: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (teacher.get("baseline_terminal") or {}).get("status") != "COMPLETED":
        return []
    accepted: list[dict[str, Any]] = []
    for branch in teacher.get("branches") or []:
        delta = branch.get("paired_effective_damage_delta")
        decision_index = branch.get("decision_index")
        kind = branch.get("kind")
        observation = (branch.get("policy_observation") or {}).get("observation")
        if not (
            branch.get("status") == "COMPLETE_BRANCH_SMOKE"
            and branch.get("branch_action_accepted") is True
            and branch.get("strict_single_intervention_verified") is True
            and kind in BRANCH_KINDS
            and type(decision_index) is int and decision_index >= 0
            and type(delta) in (int, float) and not isinstance(delta, bool)
            and math.isfinite(delta)
            and isinstance(observation, Mapping)
        ):
            continue
        features = sparse_guard_features_v2(observation)
        if tuple(features) != tuple(FEATURE_ORDER):
            raise ValueError("sparse feature extractor returned a noncanonical feature order")
        accepted.append({
            "kind": kind,
            "decision_index": decision_index,
            "delta": float(delta),
            "features": features,
        })
    return accepted


def _opportunity_rows(
    teacher: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    rows = teacher.get("sparse_action_opportunities")
    if (
        teacher.get("sparse_action_opportunity_contract")
        != SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2
        or not isinstance(rows, list)
        or teacher.get("sparse_action_opportunity_count") != len(rows)
    ):
        return "MISSING_SPARSE_ACTION_OPPORTUNITY_CONTRACT", []
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return "MALFORMED_SPARSE_ACTION_OPPORTUNITY_CONTRACT", []
        kind = row.get("kind")
        decision_index = row.get("decision_index")
        features = row.get("features")
        if not (
            kind in BRANCH_KINDS
            and type(decision_index) is int and decision_index >= 0
            and isinstance(features, Mapping)
            and tuple(features) == tuple(FEATURE_ORDER)
            and all(isinstance(value, str) for value in features.values())
            and (kind, decision_index) not in seen
        ):
            return "MALFORMED_SPARSE_ACTION_OPPORTUNITY_CONTRACT", []
        seen.add((kind, decision_index))
        parsed.append({
            "kind": kind,
            "decision_index": decision_index,
            "features": dict(features),
        })
    return "COMPLETE", parsed


def _observed_guards(rows: Iterable[Mapping[str, Any]]) -> list[SparseGuardV2]:
    observed: dict[tuple[Any, ...], SparseGuardV2] = {}
    for row in rows:
        items = tuple((name, row["features"][name]) for name in FEATURE_ORDER)
        for depth in (1, 2):
            for conditions in combinations(items, depth):
                # The exact mechanism route already fixes weapon mode.  Keep
                # the depth-one route-wide guard, but do not spend a bounded
                # shortlist slot on X AND that route-implied predicate.
                if depth == 2 and any(
                    feature == "weapon_mode" for feature, _ in conditions
                ):
                    continue
                guard = _guard_from_items(str(row["kind"]), conditions)
                observed[_guard_key(guard)] = guard
    return sorted(observed.values(), key=_guard_sort_key)


def _case_guard_accounting(
    guard: SparseGuardV2, case: Mapping[str, Any],
) -> dict[str, Any]:
    """Join a guard to its first executable opportunity in one whole wave.

    A guard would latch at its first matching opportunity.  A label from a
    later matching press is therefore not a label for the deployed guard.
    No matching opportunity is exact Cat fallback; a matching opportunity
    without an accepted branch at that exact decision remains unknown.
    """

    opportunities = [
        row for row in case["opportunities"]
        if row["kind"] == guard.kind and _features_match(guard, row["features"])
    ]
    opportunity_keys = {
        (row["kind"], row["decision_index"])
        for row in case["opportunities"]
    }
    matching_labels = [
        row for row in case["branches"]
        if row["kind"] == guard.kind and _features_match(guard, row["features"])
    ]
    effect_without_opportunity = any(
        (row["kind"], row["decision_index"]) not in opportunity_keys
        for row in matching_labels
    )
    if not opportunities:
        return {
            "status": "CAT_FALLBACK_NO_MATCHING_OPPORTUNITY",
            "effect": 0.0,
            "direct_label": None,
            "first_opportunity_decision_index": None,
            "opportunity_press_count": 0,
            "effect_without_opportunity": effect_without_opportunity,
        }
    first = min(row["decision_index"] for row in opportunities)
    exact = [
        row["delta"] for row in matching_labels
        if row["decision_index"] == first
    ]
    if not exact:
        return {
            "status": "UNKNOWN_FIRST_OPPORTUNITY_EFFECT",
            "effect": None,
            "direct_label": None,
            "first_opportunity_decision_index": first,
            "opportunity_press_count": len(opportunities),
            "effect_without_opportunity": effect_without_opportunity,
        }
    value = mean(exact)
    return {
        "status": "FIRST_OPPORTUNITY_EFFECT_LABELED",
        "effect": value,
        "direct_label": value,
        "first_opportunity_decision_index": first,
        "opportunity_press_count": len(opportunities),
        "effect_without_opportunity": effect_without_opportunity,
    }


def _per_seed_guard_accounting(
    guard: SparseGuardV2, cases: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(case["seed"], []).append(
            _case_guard_accounting(guard, case)
        )
    complete_policy_effects: dict[int, float] = {}
    direct_branch_effects: dict[int, float] = {}
    opportunity_seeds: set[int] = set()
    fallback_zero_seeds: set[int] = set()
    unknown_seeds: set[int] = set()
    effect_without_opportunity_seeds: set[int] = set()
    opportunity_press_count = 0
    matched_label_case_count = 0
    for seed, rows in sorted(grouped.items()):
        opportunity_press_count += sum(
            row["opportunity_press_count"] for row in rows
        )
        direct = [
            row["direct_label"] for row in rows
            if row["direct_label"] is not None
        ]
        matched_label_case_count += len(direct)
        if direct:
            direct_branch_effects[seed] = mean(direct)
        has_opportunity = any(row["opportunity_press_count"] > 0 for row in rows)
        if has_opportunity:
            opportunity_seeds.add(seed)
        else:
            fallback_zero_seeds.add(seed)
        if any(row["effect_without_opportunity"] for row in rows):
            effect_without_opportunity_seeds.add(seed)
        if any(row["status"] == "UNKNOWN_FIRST_OPPORTUNITY_EFFECT" for row in rows):
            unknown_seeds.add(seed)
            continue
        complete_policy_effects[seed] = mean(row["effect"] for row in rows)
    return {
        "complete_policy_effects": complete_policy_effects,
        "direct_branch_effects": direct_branch_effects,
        "opportunity_seeds": opportunity_seeds,
        "fallback_zero_seeds": fallback_zero_seeds,
        "unknown_seeds": unknown_seeds,
        "effect_without_opportunity_seeds": effect_without_opportunity_seeds,
        "opportunity_press_count": opportunity_press_count,
        "matched_label_case_count": matched_label_case_count,
    }


def _passes_effect_gate(stats: Mapping[str, Any], *, minimum: int) -> bool:
    """Gate expected route-policy value, not per-seed strict wins.

    A sparse policy is exactly Cat on seeds where its guard is not reached, so
    those paired effects are legitimate zeroes in the expected-value estimate.
    Requiring 75% of *all* seeds to be strictly positive would turn abstention
    into a loss and make a useful conditional policy mathematically
    ineligible.  Positive-seed fraction remains a reported diagnostic;
    support and a positive lower confidence bound decide eligibility.
    """

    return bool(
        stats["distinct_seed_count"] >= minimum
        and stats["lower_95_normal_effective_damage_delta_bound"] is not None
        and stats["lower_95_normal_effective_damage_delta_bound"] > 0
    )


def _rank_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    stats = candidate["complete_route_policy_effect_statistics"]
    return (
        stats["lower_95_normal_effective_damage_delta_bound"],
        stats["mean_paired_effective_damage_delta"],
        stats["distinct_seed_count"],
        -candidate["guard_depth"],
        tuple("" if value is None else str(value) for value in candidate["guard"].values()),
    )


def _bounded_selection(candidates: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    per_kind: dict[str, list[Mapping[str, Any]]] = {kind: [] for kind in BRANCH_KINDS}
    for candidate in candidates:
        per_kind[candidate["guard"]["kind"]].append(candidate)
    kind_limited = [
        candidate
        for kind in BRANCH_KINDS
        for candidate in sorted(per_kind[kind], key=_rank_key, reverse=True)[:2]
    ]
    return sorted(kind_limited, key=_rank_key, reverse=True)[:8]


def _route_model(
    route: MechanismRouteV1, cases: list[dict[str, Any]], *,
    global_fold_by_seed: Mapping[int, int], min_distinct_seeds: int,
    min_opportunity_seeds: int,
) -> dict[str, Any]:
    assigned_seeds = sorted({case["seed"] for case in cases})
    contract_failures = sorted({
        case["opportunity_status"] for case in cases
        if case["opportunity_status"] != "COMPLETE"
    })
    branches = [row for case in cases for row in case["branches"]]
    opportunities = [row for case in cases for row in case["opportunities"]]
    # The candidate universe is defined by reachable current observations,
    # never by which branch outcomes happened to be evaluated.
    guards = _observed_guards(opportunities)
    fold_minimum = max(2, math.ceil(min_distinct_seeds * 2 / 3))
    candidates: list[dict[str, Any]] = []
    for guard in guards:
        accounting = _per_seed_guard_accounting(guard, cases)
        per_seed = accounting["complete_policy_effects"]
        direct_per_seed = accounting["direct_branch_effects"]
        opportunity_seeds = accounting["opportunity_seeds"]
        fallback_zero_seeds = accounting["fallback_zero_seeds"]
        unknown_effect_opportunity_seeds = accounting["unknown_seeds"]
        effect_without_opportunity_seeds = accounting[
            "effect_without_opportunity_seeds"
        ]
        full_stats = _label_statistics(direct_per_seed.values())
        policy_stats = _label_statistics(per_seed.values())
        fold_rows: list[dict[str, Any]] = []
        for fold_index in range(_FOLD_COUNT):
            validation = {
                seed for seed in assigned_seeds
                if global_fold_by_seed[seed] == fold_index
            }
            training_values = [
                value for seed, value in per_seed.items() if seed not in validation
            ]
            heldout_values = [
                value for seed, value in per_seed.items() if seed in validation
            ]
            fold_rows.append({
                "fold_index": fold_index,
                "training_seed_count": len(set(assigned_seeds).difference(validation)),
                "validation_seeds": sorted(validation),
                "training_route_policy_effect_statistics": _label_statistics(
                    training_values
                ),
                "heldout_route_policy_effect_statistics": _label_statistics(
                    heldout_values
                ),
                "passed_training_fold_effect_gate": False,
            })
        candidates.append({
            "guard": asdict(guard),
            "guard_depth": _guard_depth(guard),
            "matched_teacher_case_count": accounting["matched_label_case_count"],
            "per_seed_effect_label_count": len(direct_per_seed),
            "branch_effect_statistics": full_stats,
            "complete_route_policy_effect_statistics": policy_stats,
            "complete_route_policy_seed_count": len(per_seed),
            "exact_route_opportunity_seed_count": len(opportunity_seeds),
            "exact_route_opportunity_press_count": accounting[
                "opportunity_press_count"
            ],
            "effect_labeled_opportunity_seed_count": len(direct_per_seed),
            "fallback_zero_seed_count": len(fallback_zero_seeds),
            "unknown_effect_opportunity_seed_count": len(
                unknown_effect_opportunity_seeds
            ),
            "effect_without_recorded_opportunity_seed_count": len(
                effect_without_opportunity_seeds
            ),
            "known_partial_route_policy_statistics": policy_stats,
            "route_policy_effect_accounting_status": (
                "COMPLETE" if not unknown_effect_opportunity_seeds
                and not effect_without_opportunity_seeds
                and len(per_seed) == len(assigned_seeds)
                else "PARTIAL_UNKNOWN_EFFECTS_NOT_IMPUTED"
            ),
            "folds": fold_rows,
            "passed_all_three_training_fold_effect_gates": False,
            "shortlist_gate_status": "FAILED_ONE_OR_MORE_TRAINING_FOLD_EFFECT_GATES",
        })

    if not contract_failures:
        for fold_index in range(_FOLD_COUNT):
            for candidate in candidates:
                train = candidate["folds"][fold_index][
                    "training_route_policy_effect_statistics"
                ]
                if (
                    candidate["exact_route_opportunity_seed_count"]
                    >= min_opportunity_seeds
                    and candidate["unknown_effect_opportunity_seed_count"] == 0
                    and candidate[
                        "effect_without_recorded_opportunity_seed_count"
                    ] == 0
                    and candidate["complete_route_policy_seed_count"]
                    == len(assigned_seeds)
                    and _passes_effect_gate(train, minimum=fold_minimum)
                ):
                    candidate["folds"][fold_index][
                        "passed_training_fold_effect_gate"
                    ] = True

    shortlist_candidates = []
    for candidate in candidates:
        fold_gate_passed = all(
            row["passed_training_fold_effect_gate"]
            for row in candidate["folds"]
        )
        candidate["passed_all_three_training_fold_effect_gates"] = (
            fold_gate_passed
        )
        heldout = [
            row["heldout_route_policy_effect_statistics"]
            for row in candidate["folds"]
        ]
        candidate["each_fold_heldout_direction_nonnegative"] = all(
            row["distinct_seed_count"] > 0
            and row["mean_paired_effective_damage_delta"] >= 0
            for row in heldout
        )
        if contract_failures:
            status = "OPPORTUNITY_CONTRACT_INCOMPLETE_ABSTAIN"
        elif candidate["effect_without_recorded_opportunity_seed_count"]:
            status = "EFFECT_LABEL_WITHOUT_RECORDED_OPPORTUNITY_ABSTAIN"
        elif candidate["unknown_effect_opportunity_seed_count"]:
            status = "UNKNOWN_FIRST_OPPORTUNITY_EFFECT_ABSTAIN"
        elif candidate["exact_route_opportunity_seed_count"] < min_opportunity_seeds:
            status = "INSUFFICIENT_EXACT_ROUTE_ACTION_OPPORTUNITY_SUPPORT"
        elif not fold_gate_passed:
            status = "FAILED_ONE_OR_MORE_TRAINING_FOLD_EFFECT_GATES"
        elif candidate["complete_route_policy_effect_statistics"]["distinct_seed_count"] < min_distinct_seeds:
            status = "INSUFFICIENT_ALL_TRAINING_SUPPORT"
        elif (
            candidate["complete_route_policy_effect_statistics"][
                "lower_95_normal_effective_damage_delta_bound"
            ] is None
            or candidate["complete_route_policy_effect_statistics"][
                "lower_95_normal_effective_damage_delta_bound"
            ] <= 0
        ):
            status = "ALL_TRAINING_LOWER_BOUND_NOT_POSITIVE"
        elif not candidate["each_fold_heldout_direction_nonnegative"]:
            status = "HELDOUT_FOLD_DIRECTION_NEGATIVE_OR_UNOBSERVED"
        else:
            status = "PASSED_DEVELOPMENT_SHORTLIST_GATE"
            shortlist_candidates.append(candidate)
        candidate["shortlist_gate_status"] = status

    shortlist = _bounded_selection(shortlist_candidates)
    shortlist_keys = {tuple(row["guard"].values()) for row in shortlist}
    for candidate in candidates:
        candidate["shortlisted"] = tuple(candidate["guard"].values()) in shortlist_keys
        if (
            candidate["shortlist_gate_status"] == "PASSED_DEVELOPMENT_SHORTLIST_GATE"
            and not candidate["shortlisted"]
        ):
            candidate["shortlist_gate_status"] = "PASSED_GATE_BUT_REMOVED_BY_ROUTE_CAP"

    kind_diagnostics = []
    for kind in BRANCH_KINDS:
        rows = [row for row in candidates if row["guard"]["kind"] == kind]
        kind_diagnostics.append({
            "kind": kind,
            "accepted_complete_branch_label_count": sum(
                1 for row in branches if row["kind"] == kind
            ),
            "observed_guard_candidate_count": len(rows),
            "shortlisted_guard_count": sum(row["shortlisted"] for row in rows),
        })
    opportunity_status = (
        "COMPLETE" if not contract_failures
        else "INCOMPLETE_ABSTAIN_NO_COMPATIBILITY_FALLBACK"
    )
    return {
        "mechanism_route": asdict(route),
        "assigned_teacher_case_count": len(cases),
        "assigned_seed_count": len(assigned_seeds),
        "assigned_seeds": assigned_seeds,
        "sparse_action_opportunity_contract_status": opportunity_status,
        "sparse_action_opportunity_contract_failures": contract_failures,
        "accepted_complete_branch_label_count": len(branches),
        "observed_guard_candidate_count": len(candidates),
        "fold_selection_metric": (
            "FIRST_MATCHING_EXECUTABLE_OPPORTUNITY_EXACT_LABEL_JOIN;"
            "ALL_ROUTE_CASES_AGGREGATED_ONCE_PER_SEED;"
            "EACH_CANDIDATE_MUST_PASS_ALL_THREE_TRAINING_FOLD_EFFECT_GATES;"
            "ROUTE_KIND_CAP_APPLIED_ONLY_AFTER_STABILITY_GATES;"
            "CAT_FALLBACK_ZERO_INCLUDED;EXPECTED_EFFECT_LOWER_BOUND_GATE;"
            "POSITIVE_SEED_FRACTION_DIAGNOSTIC_ONLY;UNKNOWN_EFFECT_SEEDS_REJECTED"
        ),
        "kind_diagnostics": kind_diagnostics,
        "candidate_diagnostics": candidates,
        "shortlist": [row["guard"] for row in shortlist],
        "shortlist_count": len(shortlist),
        "status": (
            "DEVELOPMENT_SHORTLIST_READY_NONVOTING" if shortlist
            else "ABSTAIN_NO_STABLE_REACHABLE_GUARD"
        ),
        "cat_zero_residual_fallback": True,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def fit_factored_sparse_guard_learner_v2(
    teacher_cases: Iterable[tuple[DevelopmentWaveCaseV1, Mapping[str, Any]]], *,
    min_distinct_seeds: int = 6,
    min_opportunity_seeds: int = 6,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return route-local sparse guards that survived seed-blocked fold gates."""

    if min_distinct_seeds < 6 or min_opportunity_seeds < 6:
        raise ValueError("v2 support thresholds may not be lower than six seeds")
    grouped: dict[MechanismRouteV1, list[dict[str, Any]]] = {}
    all_seeds: set[int] = set()
    for case, teacher in teacher_cases:
        if not _teacher_matches_case(case, teacher):
            raise ValueError("teacher seed, wave, request, or dynamic load differs from its case")
        route = mechanism_route_v1(
            case, item_database=item_database,
            talent_position_map=talent_position_map,
        )
        opportunity_status, opportunities = _opportunity_rows(teacher)
        seed = case.dynamic_load.seed
        grouped.setdefault(route, []).append({
            "seed": seed,
            "branches": _accepted_branch_rows(teacher),
            "opportunities": opportunities,
            "opportunity_status": opportunity_status,
        })
        all_seeds.add(seed)
    if not grouped:
        raise ValueError("at least one teacher case is required")
    ordered_seeds = sorted(all_seeds)
    fold_by_seed = {
        seed: index % _FOLD_COUNT for index, seed in enumerate(ordered_seeds)
    }
    routes = [
        _route_model(
            route, grouped[route], global_fold_by_seed=fold_by_seed,
            min_distinct_seeds=min_distinct_seeds,
            min_opportunity_seeds=min_opportunity_seeds,
        )
        for route in sorted(grouped, key=_route_key)
    ]
    shortlist_count = sum(route["shortlist_count"] for route in routes)
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "DEVELOPMENT_SPARSE_GUARD_SHORTLIST_READY_NONVOTING"
            if shortlist_count else "ABSTAIN_NO_STABLE_REACHABLE_SPARSE_GUARD"
        ),
        "selection_protocol": (
            "EXACT_MECHANISM_ROUTE;OBSERVED_DEPTH_1_OR_2_GUARDS_ONLY;"
            "GLOBAL_SEED_BLOCKED_DETERMINISTIC_THREE_FOLD_STABILITY;"
            "INDIVIDUAL_CANDIDATE_FOLD_GATES_BEFORE_GLOBAL_BOUNDED_RANKING;"
            "FIRST_MATCHING_EXECUTABLE_OPPORTUNITY_EXACT_BRANCH_LABEL_PER_CASE;"
            "ALL_ASSIGNED_ROUTE_CASES_AGGREGATED_ONCE_PER_SEED;"
            "CAT_ZERO_RESIDUAL_ARM;EXPECTED_EFFECT_LOWER_BOUND_GATE;"
            "POSITIVE_SEED_FRACTION_DIAGNOSTIC_ONLY;"
            "UNKNOWN_FIRST_OPPORTUNITY_EFFECT_REJECTED"
        ),
        "feature_order": list(FEATURE_ORDER),
        "fold_count": _FOLD_COUNT,
        "seed_fold_assignments": [
            {"seed": seed, "fold_index": fold_by_seed[seed]}
            for seed in ordered_seeds
        ],
        "training_seeds": ordered_seeds,
        "min_distinct_training_seeds": min_distinct_seeds,
        "min_exact_route_action_opportunity_seeds": min_opportunity_seeds,
        "max_guards_per_branch_kind_per_route": 2,
        "max_guards_per_route": 8,
        "cat_arm": {
            "role": "PERMANENT_FALLBACK",
            "residual_effective_damage_delta": 0.0,
        },
        "routes": routes,
        "route_count": len(routes),
        "shortlisted_guard_count": shortlist_count,
        "authorization_claimed": False,
        "full_wave_candidate_policy_evaluated": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


__all__ = [
    "SCHEMA", "SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2",
    "fit_factored_sparse_guard_learner_v2",
]
