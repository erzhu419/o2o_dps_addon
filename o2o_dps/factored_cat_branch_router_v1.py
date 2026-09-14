"""Route Cat-relative branch search by build mechanics and modeled wave shape.

Teacher outcomes select a rule only for a fresh full-wave test.  Every other
route delegates to the same native Cat controller through the existing
conditional candidate; this module does not publish a gameplay policy.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math
from statistics import mean, stdev
from typing import Any, Callable, Iterable, Mapping

from .conditional_cat_branch_v1 import (
    FrozenRuleV1,
    evaluate_conditional_cat_branch_v1,
    fit_conditional_cat_branch_v1,
)
from .cat_action_branch_search_v1 import run_cat_action_branch_search_v1
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .factored_policy_context_v1 import extract_factored_policy_context_v1


SCHEMA = "factored_cat_branch_router/v1"
_ROUTE_FIELDS = (
    "weapon_mode", "main_hand_speed_band", "bloodthirst_known",
    "wave_topology", "target_count_band", "modeled_hp_budget_band",
    "off_hand_speed_band", "target_armor_band", "team_dps_prior_band",
    "background_team_ttk_band", "team_prior_source",
)
# Evidence is shared across builds and waves before considering a full joint
# mechanism family.  New combinations are not invented by a Cartesian grid.
_PROJECTIONS = (
    (), ("weapon_mode",), ("target_count_band",),
    ("wave_topology",),
    ("weapon_mode", "main_hand_speed_band"),
    ("weapon_mode", "main_hand_speed_band", "off_hand_speed_band"),
    ("weapon_mode", "bloodthirst_known"),
    ("target_count_band", "modeled_hp_budget_band"),
    ("target_count_band", "background_team_ttk_band"),
    ("modeled_hp_budget_band", "team_dps_prior_band"),
    ("target_armor_band", "background_team_ttk_band"),
    ("wave_topology", "target_count_band"),
    ("weapon_mode", "target_count_band"),
    _ROUTE_FIELDS,
)


@dataclass(frozen=True)
class MechanismRouteV1:
    weapon_mode: str
    main_hand_speed_band: str
    bloodthirst_known: str
    target_count_band: str
    modeled_hp_budget_band: str
    wave_topology: str = "SINGLE_WAVE"
    off_hand_speed_band: str = "unknown"
    target_armor_band: str = "unknown"
    team_dps_prior_band: str = "unknown"
    background_team_ttk_band: str = "unknown"
    team_prior_source: str = "UNAVAILABLE"


def _collapse_bands(values: Iterable[str]) -> str:
    unique = sorted(set(values))
    if not unique:
        return "unknown"
    return unique[0] if len(unique) == 1 else "mixed:" + "+".join(unique)


def mechanism_route_v1(
    case: DevelopmentWaveCaseV1, *,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> MechanismRouteV1:
    """Coarsen transferable mechanisms, never a player/raid identifier."""

    context = extract_factored_policy_context_v1(
        case, item_database=item_database, talent_position_map=talent_position_map,
    )
    build, wave = context.build, context.wave
    speed = build.main_hand_speed_s
    hp = wave.target_max_hp_by_index
    hp_total = (
        max((value for value in hp if value is not None), default=0)
        if wave.wave_topology == "SEQUENTIAL_WAVES" else
        sum(value for value in hp if value is not None)
    )
    if speed is None:
        speed_band = "unknown"
    else:
        speed_band = "fast_under_2s" if speed < 2.0 else "slow_2s_plus"
    off_speed = build.off_hand_speed_s
    if off_speed is None:
        off_speed_band = (
            "none" if build.weapon_mode in {"TWO_HAND", "ONE_HAND"}
            else "unknown"
        )
    else:
        off_speed_band = "fast_under_2s" if off_speed < 2.0 else "slow_2s_plus"
    if not hp or any(value is None for value in hp):
        hp_band = "unknown"
    elif hp_total < 50_000:
        hp_band = "under_50k"
    elif hp_total < 200_000:
        hp_band = "50k_to_200k"
    else:
        hp_band = "200k_plus"
    count = 1 if wave.wave_topology == "SEQUENTIAL_WAVES" else wave.route_target_count
    armor_bands = []
    for armor in wave.target_base_armor_by_index:
        armor_bands.append(
            "unknown" if armor is None else
            "under_2k" if armor < 2_000 else
            "2k_to_4k" if armor < 4_000 else "4k_plus"
        )
    return MechanismRouteV1(
        weapon_mode=build.weapon_mode,
        main_hand_speed_band=speed_band,
        bloodthirst_known=(
            "unknown" if build.bloodthirst_known is None
            else "yes" if build.bloodthirst_known else "no"
        ),
        target_count_band="one" if count == 1 else "two" if count == 2 else "three_plus",
        modeled_hp_budget_band=hp_band,
        wave_topology=wave.wave_topology,
        off_hand_speed_band=off_speed_band,
        target_armor_band=_collapse_bands(armor_bands),
        team_dps_prior_band=_collapse_bands(wave.team_dps_prior_band_by_target),
        background_team_ttk_band=_collapse_bands(
            wave.background_team_ttk_band_by_target
        ),
        team_prior_source=wave.team_prior_source,
    )


def _teacher_rows(
    case: DevelopmentWaveCaseV1, teacher: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Bind one completed teacher artifact to its source case, not a build ID."""

    seed = case.dynamic_load.seed
    if (
        teacher.get("seed") != seed
        or teacher.get("source_wave_ref") != case.case_spec["source_wave_ref"]
        or teacher.get("request_sha256") != case.dynamic_load.request_sha256
        or teacher.get("dynamic_load_contract_sha256") != case.dynamic_load.contract_sha256
    ):
        raise ValueError("teacher seed, wave, request, or dynamic load differs from its case")
    terminal = teacher.get("baseline_terminal") or {}
    rows = []
    for branch in teacher.get("branches") or []:
        observation = (branch.get("policy_observation") or {}).get("observation") or {}
        rows.append({
            "seed": seed,
            "baseline_status": terminal.get("status"),
            "status": branch.get("status"),
            "kind": branch.get("kind"),
            "branch_action_accepted": branch.get("branch_action_accepted"),
            "paired_effective_damage_delta": branch.get("paired_effective_damage_delta"),
            "combat": observation.get("combat"),
        })
    return rows


def _teacher_opportunity_rows(
    case: DevelopmentWaveCaseV1, teacher: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Read full-baseline opportunities without treating them as rewards."""

    seed = case.dynamic_load.seed
    if (
        teacher.get("seed") != seed
        or teacher.get("source_wave_ref") != case.case_spec["source_wave_ref"]
        or teacher.get("request_sha256") != case.dynamic_load.request_sha256
        or teacher.get("dynamic_load_contract_sha256") != case.dynamic_load.contract_sha256
    ):
        raise ValueError("teacher seed, wave, request, or dynamic load differs from its case")
    coverage = teacher.get("action_opportunity_coverage")
    if (
        not isinstance(coverage, list)
        or teacher.get("action_opportunity_rule_count") != len(coverage)
    ):
        raise ValueError("teacher lacks complete full-baseline action opportunity coverage")
    rows = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for raw in coverage:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("rule"), Mapping):
            raise ValueError("teacher action opportunity row is malformed")
        rule = FrozenRuleV1(**raw["rule"])
        count = raw.get("opportunity_press_count")
        phases = raw.get("phase_counts")
        first = raw.get("first_decision_index")
        last = raw.get("last_decision_index")
        key = tuple(asdict(rule).items())
        if (
            rule.kind is None
            or type(count) is not int or count < 1
            or type(first) is not int or type(last) is not int or first > last
            or not isinstance(phases, Mapping)
            or any(type(value) is not int or value < 0 for value in phases.values())
            or sum(phases.values()) != count
            or key in seen
        ):
            raise ValueError("teacher action opportunity row violates its contract")
        seen.add(key)
        rows.append({
            "seed": seed,
            "rule": asdict(rule),
            "opportunity_press_count": count,
        })
    if teacher.get("action_opportunity_press_count") != sum(
        row["opportunity_press_count"] for row in rows
    ):
        raise ValueError("teacher action opportunity total differs from its rows")
    return rows


def fit_factored_cat_branch_router_v1(
    teacher_cases: Iterable[tuple[DevelopmentWaveCaseV1, Mapping[str, Any]]], *,
    min_distinct_seeds: int = 6,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Share branch evidence within mechanism families, then keep Cat fallback."""

    grouped: dict[MechanismRouteV1, list[dict[str, Any]]] = {}
    opportunity_by_route: dict[
        MechanismRouteV1, dict[tuple[tuple[str, Any], ...], dict[int, int]]
    ] = {}
    assigned_seeds_by_route: dict[MechanismRouteV1, set[int]] = {}
    all_seeds: set[int] = set()
    for case, teacher in teacher_cases:
        route = mechanism_route_v1(
            case, item_database=item_database, talent_position_map=talent_position_map,
        )
        grouped.setdefault(route, []).extend(_teacher_rows(case, teacher))
        assigned_seeds_by_route.setdefault(route, set()).add(case.dynamic_load.seed)
        for opportunity in _teacher_opportunity_rows(case, teacher):
            key = tuple(opportunity["rule"].items())
            per_seed = opportunity_by_route.setdefault(route, {}).setdefault(key, {})
            per_seed[opportunity["seed"]] = (
                per_seed.get(opportunity["seed"], 0)
                + opportunity["opportunity_press_count"]
            )
        all_seeds.add(case.dynamic_load.seed)
    if not grouped:
        raise ValueError("at least one teacher case is required")
    projected: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = {}
    for route, rows in grouped.items():
        features = asdict(route)
        for fields in _PROJECTIONS:
            key = (fields, tuple(features[field] for field in fields))
            projected.setdefault(key, []).extend(rows)
    cells = []
    for (fields, values), rows in sorted(projected.items()):
        model = fit_conditional_cat_branch_v1(
            rows, training_seeds=all_seeds, min_distinct_seeds=min_distinct_seeds,
        )
        cells.append({
            "fields": list(fields), "values": list(values), "teacher_model": model,
        })
    routes = [asdict(route) for route in sorted(grouped, key=lambda row: tuple(asdict(row).values()))]
    opportunity_coverage = []
    for route in sorted(grouped, key=lambda row: tuple(asdict(row).values())):
        assigned_count = len(assigned_seeds_by_route[route])
        for rule_key, per_seed in sorted(
            opportunity_by_route.get(route, {}).items(), key=lambda item: item[0],
        ):
            opportunity_coverage.append({
                "mechanism_route": asdict(route),
                "rule": dict(rule_key),
                "assigned_teacher_seed_count": assigned_count,
                "opportunity_seed_count": len(per_seed),
                "opportunity_press_count": sum(per_seed.values()),
                "opportunity_seed_fraction": len(per_seed) / assigned_count,
            })
    router = {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "training_seeds": sorted(all_seeds),
        "routes": routes,
        "route_count": len(routes),
        "projected_cells": cells,
        "projected_cell_count": len(cells),
        "exact_route_rule_opportunity_coverage": opportunity_coverage,
        "exact_route_rule_opportunity_row_count": len(opportunity_coverage),
        "min_exact_route_opportunity_seeds": min_distinct_seeds,
        "route_authorizations": [],
        "transfer_gate_status": "NOT_RUN",
        "full_wave_candidate_policy_evaluated": False,
        "deployment_eligible": False,
    }
    proposals = [_proposal_for_route(router, route) for route in grouped]
    router["proposed_transfer_route_count"] = sum(
        rule.kind is not None for rule in proposals
    )
    # A pooled teacher cell is only a proposal.  It cannot activate a rule on
    # the final fresh set until unused full-wave transfers confirm that exact
    # destination route.
    router["eligible_fresh_test_route_count"] = 0
    return router


def _matching_projected_cells(
    router: Mapping[str, Any], route: MechanismRouteV1,
) -> list[Mapping[str, Any]]:
    features = asdict(route)
    if features not in router["routes"] or route.weapon_mode == "UNKNOWN":
        return []
    matching = []
    for cell in router["projected_cells"]:
        model = cell["teacher_model"]
        if (
            model["status"] != "FROZEN_RULE_FOR_FRESH_TEST"
            or not all(
                features[field] == value
                for field, value in zip(cell["fields"], cell["values"])
            )
        ):
            continue
        rule = FrozenRuleV1(**model["policy"])
        if (
            _rule_is_statically_compatible_with_route(rule, route)
            and _rule_has_exact_route_opportunity_support(router, route, rule)
        ):
            matching.append(cell)
    return matching


def _rule_has_exact_route_opportunity_support(
    router: Mapping[str, Any], route: MechanismRouteV1, rule: FrozenRuleV1,
) -> bool:
    """Require observed trigger opportunities on the exact destination route."""

    minimum = router.get("min_exact_route_opportunity_seeds")
    if type(minimum) is not int or minimum < 1:
        raise ValueError("router lacks an exact-route opportunity support threshold")
    target_route = asdict(route)
    target_rule = asdict(rule)
    return any(
        row.get("mechanism_route") == target_route
        and row.get("rule") == target_rule
        and type(row.get("opportunity_seed_count")) is int
        and row["opportunity_seed_count"] >= minimum
        for row in router.get("exact_route_rule_opportunity_coverage") or []
        if isinstance(row, Mapping)
    )


def _rule_is_statically_compatible_with_route(
    rule: FrozenRuleV1, route: MechanismRouteV1,
) -> bool:
    """Reject a projected rule contradicted by its destination route."""

    compatible_target_counts = {
        "one": frozenset({"one"}),
        # FrozenRuleV1 observes live enemies at intervention time.  A route
        # may therefore enter a lower count bucket after earlier targets die,
        # but it can never enter a bucket above its initial route count.
        "two": frozenset({"one", "two"}),
        "three_plus": frozenset({
            "one", "two", "three_to_four", "five_plus",
        }),
    }
    return bool(
        rule.kind is not None
        and rule.weapon_mode == route.weapon_mode
        and (
            rule.kind not in {"BT_TO_WW", "WW_TO_BT"}
            or route.bloodthirst_known == "yes"
        )
        and rule.target_count in compatible_target_counts.get(
            route.target_count_band, frozenset()
        )
    )


def _exact_route_vetoes(
    router: Mapping[str, Any], route: MechanismRouteV1, rule: FrozenRuleV1,
) -> bool:
    """Do not export a pooled proposal contradicted on its destination route."""

    features = asdict(route)
    exact = next((
        cell for cell in router["projected_cells"]
        if tuple(cell["fields"]) == _ROUTE_FIELDS
        and all(features[field] == value for field, value in zip(cell["fields"], cell["values"]))
    ), None)
    if exact is None:
        return False
    target = asdict(rule)
    observed = next((
        cell for cell in exact["teacher_model"].get("cells") or []
        if cell.get("rule") == target and cell.get("distinct_seed_count", 0) > 0
    ), None)
    return bool(
        observed is not None
        and (
            observed.get("mean_paired_label_delta", 0) <= 0
            or observed.get("positive_seed_fraction", 0) < 0.5
        )
    )


def _proposal_for_route(
    router: Mapping[str, Any], route: MechanismRouteV1,
) -> FrozenRuleV1:
    """Return a teacher-derived proposal for held-out transfer, never deployment."""

    matching = _matching_projected_cells(router, route)
    if not matching:
        return FrozenRuleV1()
    selected = max(matching, key=lambda cell: (
        len(cell["fields"]),
        cell["teacher_model"]["selected_training_cell"]["lower_95_normal_label_bound"],
    ))
    rule = FrozenRuleV1(**selected["teacher_model"]["policy"])
    return FrozenRuleV1() if _exact_route_vetoes(router, route, rule) else rule


def proposed_rule_for_case_v1(
    router: Mapping[str, Any], case: DevelopmentWaveCaseV1, *,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> tuple[MechanismRouteV1, FrozenRuleV1]:
    """Select a route proposal solely for the held-out transfer lane."""

    route = mechanism_route_v1(
        case, item_database=item_database, talent_position_map=talent_position_map,
    )
    return route, _proposal_for_route(router, route)


def authorize_factored_routes_v1(
    router: Mapping[str, Any], transfer_pairs: Iterable[Mapping[str, Any]], *,
    min_distinct_seeds: int = 8,
) -> dict[str, Any]:
    """Authorize proposals only after positive unused full-wave route transfers."""

    if min_distinct_seeds < 2:
        raise ValueError("transfer minimum support must be at least two seeds")
    training_seeds = frozenset(int(seed) for seed in router["training_seeds"])
    pair_groups: dict[
        tuple[tuple[str, Any], ...],
        dict[int, list[tuple[bool, float | None]]],
    ] = {}
    assigned: dict[tuple[tuple[str, Any], ...], set[int]] = {}
    attempted: dict[tuple[tuple[str, Any], ...], set[int]] = {}
    for pair in transfer_pairs:
        raw_route = pair.get("mechanism_route")
        if not isinstance(raw_route, Mapping):
            raise ValueError("transfer pair lacks a mechanism route")
        route = MechanismRouteV1(**raw_route)
        route_key = tuple(asdict(route).items())
        proposal = _proposal_for_route(router, route)
        if proposal.kind is None:
            continue
        if pair.get("rule") != asdict(proposal):
            raise ValueError("transfer pair rule differs from the frozen route proposal")
        seed = pair.get("seed")
        if type(seed) is not int or seed in training_seeds:
            raise ValueError("transfer seed is invalid or overlaps teacher training")
        route_assigned = assigned.setdefault(route_key, set())
        route_assigned.add(seed)
        if pair.get("candidate_intervention_count") == 1:
            attempted.setdefault(route_key, set()).add(seed)
        delta = pair.get("paired_effective_damage_delta")
        complete_receipts = (
            pair.get("status") == "COMPLETE_FRESH_PAIR"
            and pair.get("comparison_ready") is True
            and type(delta) in (int, float)
            and math.isfinite(delta)
        )
        strict_intervention = (
            pair.get("candidate_intervention_count") == 1
            and pair.get("candidate_branch_action_accepted") is True
            and pair.get("strict_single_intervention_verified") is True
        )
        active_cat_fallback = (
            pair.get("rule_active") is True
            and pair.get("candidate_intervention_count") == 0
            and pair.get("candidate_branch_action_accepted") is False
            and pair.get("strict_single_intervention_verified") is False
            and pair.get("active_cat_fallback_verified") is True
            and delta == 0.0
        )
        valid = bool(complete_receipts and (strict_intervention or active_cat_fallback))
        pair_groups.setdefault(route_key, {}).setdefault(seed, []).append((
            valid, float(delta) if valid else None,
        ))

    authorizations: list[dict[str, Any]] = []
    for raw_route in router["routes"]:
        route = MechanismRouteV1(**raw_route)
        proposal = _proposal_for_route(router, route)
        if proposal.kind is None:
            continue
        key = tuple(asdict(route).items())
        values = []
        for _, seed_pairs in sorted(pair_groups.get(key, {}).items()):
            if seed_pairs and all(valid for valid, _ in seed_pairs):
                values.append(mean(
                    value for _, value in seed_pairs if value is not None
                ))
        assigned_count = len(assigned.get(key, set()))
        attempted_count = len(attempted.get(key, set()))
        complete_count = len(values)
        avg = mean(values) if values else None
        sem = stdev(values) / math.sqrt(len(values)) if len(values) >= 2 else None
        lower = avg - 1.96 * sem if avg is not None and sem is not None else None
        positive = sum(value > 0 for value in values) / len(values) if values else None
        support_ready = complete_count >= min_distinct_seeds
        authorized = (
            support_ready
            and positive is not None and positive >= 0.75
            and lower is not None and lower > 0
        )
        authorizations.append({
            "mechanism_route": asdict(route),
            "rule": asdict(proposal),
            "status": (
                "AUTHORIZED_FRESH_TEST" if authorized
                else "REJECTED_TRANSFER_GATE" if support_ready
                else "INCOMPLETE_TRANSFER_EVIDENCE"
            ),
            "assigned_transfer_seed_count": assigned_count,
            "attempted_intervention_seed_count": attempted_count,
            "complete_comparison_seed_count": complete_count,
            "incomplete_transfer_seed_count": assigned_count - complete_count,
            "mean_paired_transfer_delta": avg,
            "lower_95_normal_transfer_bound": lower,
            "positive_seed_fraction": positive,
        })
    result = deepcopy(dict(router))
    result["route_authorizations"] = authorizations
    result["transfer_gate_status"] = "COMPLETE"
    result["eligible_fresh_test_route_count"] = sum(
        row["status"] == "AUTHORIZED_FRESH_TEST" for row in authorizations
    )
    result["transfer_comparison_ready_route_count"] = sum(
        row["complete_comparison_seed_count"] >= min_distinct_seeds
        for row in authorizations
    )
    result["transfer_incomplete_evidence_route_count"] = sum(
        row["status"] == "INCOMPLETE_TRANSFER_EVIDENCE"
        for row in authorizations
    )
    result["full_wave_candidate_policy_evaluated"] = any(
        row["complete_comparison_seed_count"] > 0 for row in authorizations
    )
    return result


def _rule_for_route(router: Mapping[str, Any], route: MechanismRouteV1) -> FrozenRuleV1:
    proposal = _proposal_for_route(router, route)
    if proposal.kind is None:
        return proposal
    expected_route = asdict(route)
    expected_rule = asdict(proposal)
    authorized = any(
        row.get("mechanism_route") == expected_route
        and row.get("rule") == expected_rule
        and row.get("status") == "AUTHORIZED_FRESH_TEST"
        for row in router.get("route_authorizations") or []
    )
    return proposal if authorized else FrozenRuleV1()


def rule_for_case_v1(
    router: Mapping[str, Any], case: DevelopmentWaveCaseV1, *,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> tuple[MechanismRouteV1, FrozenRuleV1]:
    route = mechanism_route_v1(
        case, item_database=item_database, talent_position_map=talent_position_map,
    )
    return route, _rule_for_route(router, route)


def evaluate_factored_cat_branch_router_v1(
    router: Mapping[str, Any], case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any], *,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the routed residual against native Cat on an unseen full wave."""

    route, rule = rule_for_case_v1(
        router, case, item_database=item_database, talent_position_map=talent_position_map,
    )
    result = evaluate_conditional_cat_branch_v1(
        case, bridge_factory, rule, training_seeds=router["training_seeds"],
    )
    return {
        **result,
        "schema": "factored_cat_branch_fresh_pair/v1",
        "mechanism_route": asdict(route),
        "route_abstained_to_cat": rule.kind is None,
        "deployment_eligible": False,
    }


def run_factored_cat_branch_development_v1(
    training_cases: Iterable[DevelopmentWaveCaseV1],
    fresh_cases: Iterable[DevelopmentWaveCaseV1],
    bridge_factory: Callable[[], Any], *,
    max_states: int = 1,
    min_distinct_seeds: int = 6,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute teacher search, route fit, and fresh whole-wave Cat pairs."""

    teachers = [
        (case, run_cat_action_branch_search_v1(case, bridge_factory, max_states=max_states))
        for case in training_cases
    ]
    router = fit_factored_cat_branch_router_v1(
        teachers, min_distinct_seeds=min_distinct_seeds,
        item_database=item_database, talent_position_map=talent_position_map,
    )
    pairs = [
        evaluate_factored_cat_branch_router_v1(
            router, case, bridge_factory,
            item_database=item_database, talent_position_map=talent_position_map,
        )
        for case in fresh_cases
    ]
    return {
        "schema": "factored_cat_branch_development_run/v1",
        "router": router,
        "teacher_cases": [{
            "seed": case.dynamic_load.seed,
            "mechanism_route": asdict(mechanism_route_v1(
                case, item_database=item_database, talent_position_map=talent_position_map,
            )),
            "visited_cat_decisions": teacher["visited_cat_decisions"],
            "independent_action_branch_count": teacher["independent_action_branch_count"],
            "completed_teacher_label_count": teacher["completed_teacher_label_count"],
        } for case, teacher in teachers],
        "fresh_pairs": pairs,
        "policy_update_rounds_completed": 0,
        "comparison_ready": False,
        "deployment_eligible": False,
    }


__all__ = (
    "MechanismRouteV1", "mechanism_route_v1", "fit_factored_cat_branch_router_v1",
    "proposed_rule_for_case_v1", "authorize_factored_routes_v1",
    "rule_for_case_v1", "evaluate_factored_cat_branch_router_v1",
    "run_factored_cat_branch_development_v1",
)
