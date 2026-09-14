"""Bounded factored Cat-relative search on the physical press clock.

The phases are intentionally separate:

1. training cases create one-press ActionPlan outcome labels;
2. the factored router may only *propose* a rule;
3. unused transfer cases decide whether an exact route is authorized; and
4. another untouched set evaluates only authorized rules, with Cat fallback.

Every fresh pair first proves that an abstaining conditional controller is
exactly identical to Cat on an independent same-seed, same-period full wave.
The result remains model-defined and non-voting even when its technical
comparison receipts close.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Iterable, Mapping

from .branch_teacher_v1 import BranchReplayMismatchV1, _run_fresh
from .cat_external_press_action_teacher_v1 import (
    _branch_action_accepted,
    _normalized_paired_delta,
    _run_press_lane,
    _same_press_prefix,
    _terminal_receipt,
    _wire,
    run_cat_external_press_action_teacher_v1,
)
from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from .cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from .conditional_cat_branch_v1 import ConditionalCatBranchCandidateV1, FrozenRuleV1
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .factored_cat_branch_router_v1 import (
    authorize_factored_routes_v1,
    fit_factored_cat_branch_router_v1,
    proposed_rule_for_case_v1,
    rule_for_case_v1,
)
from .fury_full_policy_rollout_v3 import target_semantics_context_receipt_v3
from .cat_external_press_pilot_v1 import _no_live_target_press_reason


SCHEMA = "factored_external_press_search/v1"


def _case_tracking(case: DevelopmentWaveCaseV1) -> dict[str, Any]:
    transplant = case.case_spec.get("build_transplant")
    return {
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "build_ref": case.case_spec.get("build_ref"),
        "representative_rank": (
            transplant.get("representative_rank")
            if isinstance(transplant, Mapping) else None
        ),
        "build_identity_is_policy_feature": False,
    }


def _semantic_receipt(
    case: DevelopmentWaveCaseV1, artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Close current-target semantics without admitting future schedules."""

    reasons: list[str] = []
    target_count = len(case.request["encounter"]["targets"])
    config = case.dynamic_load.config
    load = artifact.get("dynamic_load_receipt")
    if not isinstance(load, Mapping):
        reasons.append("DYNAMIC_LOAD_RECEIPT_MISSING")
    else:
        expected_load = {
            "schema": "o2o_dynamic_target_semantics/v3",
            "config_digest": config.content_sha256,
            "target_count": target_count,
            "background_event_count": len(config.background_damage_events),
            "attackability_event_count": len(config.attackability_events),
            "effective_armor_event_count": len(config.effective_armor_events),
        }
        if any(load.get(key) != value for key, value in expected_load.items()):
            reasons.append("DYNAMIC_LOAD_RECEIPT_DIFFERS_FROM_CASE")
    if (
        artifact.get("seed") != case.dynamic_load.seed
        or artifact.get("mode") != "DYNAMIC_V3_WHOLE_WAVE"
    ):
        reasons.append("LANE_CASE_BINDING_INVALID")

    context_receipts = {
        index: target_semantics_context_receipt_v3(context)
        for index, context in case.target_contexts.items()
    }
    presses = artifact.get("presses")
    if not isinstance(presses, list) or not presses:
        reasons.append("POLICY_PRESS_RECEIPTS_MISSING")
        presses = []
    for press in presses:
        if press.get("source_invocation_count") == 0:
            before = press.get("simulator_state_before")
            reason = (
                _no_live_target_press_reason(before, target_count=target_count)
                if isinstance(before, Mapping) else None
            )
            if (
                reason is None
                or press.get("policy_disposition") != "NO_LIVE_TARGET_ENVIRONMENT_NOOP"
                or press.get("no_live_target_reason") != reason
                or any(press.get(key) is not None for key in (
                    "target_semantics", "expert_state", "proposal",
                ))
            ):
                reasons.append("NO_LIVE_TARGET_PRESS_RECEIPT_INVALID")
            continue
        index = press.get("target_index")
        semantics = press.get("target_semantics")
        expert = press.get("expert_state")
        combat = expert.get("combat") if isinstance(expert, Mapping) else None
        expected = context_receipts.get(index)
        if not isinstance(semantics, Mapping) or expected is None or not isinstance(combat, Mapping):
            reasons.append("PRESS_TARGET_SEMANTICS_MISSING_OR_UNBOUND")
            continue
        static_fields = (
            "context_id", "mode", "target_index", "target_classification", "target_name",
        )
        if any(semantics.get(key) != expected.get(key) for key in static_fields):
            reasons.append("PRESS_TARGET_SEMANTICS_CONTEXT_MISMATCH")
        if (
            expected.get("target_max_health") is not None
            and semantics.get("target_max_health") != expected.get("target_max_health")
        ):
            reasons.append("PRESS_TARGET_MAX_HEALTH_MISMATCH")
        expected_evidence = {
            key: {
                field: value for field, value in receipt.items() if field != "schema"
            }
            for key, receipt in expected["field_evidence"].items()
        }
        # The live resolver carries typed evidence dataclasses.  Its structural
        # projection omits the redundant per-field schema tag but must preserve
        # every evidence value.
        if semantics.get("field_evidence") != expected_evidence:
            reasons.append("PRESS_FIELD_EVIDENCE_MISMATCH")
        if semantics.get("dynamic_state_time_ms") != press.get("time_ms"):
            reasons.append("PRESS_SEMANTICS_TIME_MISMATCH")
        if (
            combat.get("target_name") != semantics.get("target_name")
            or combat.get("target_health_pct") != semantics.get("target_health_pct")
        ):
            reasons.append("POLICY_OBSERVATION_TARGET_MISMATCH")

    final = artifact.get("final_state")
    dynamics = final.get("dynamic_target_semantics") if isinstance(final, Mapping) else None
    team = final.get("dynamic_team_background") if isinstance(final, Mapping) else None
    for name, block in (("TARGET", dynamics), ("TEAM", team)):
        if not isinstance(block, Mapping):
            reasons.append(f"FINAL_{name}_SEMANTICS_MISSING")
        elif (
            block.get("schema") != "o2o_dynamic_target_semantics/v3"
            or block.get("config_digest") != config.content_sha256
            or not isinstance(block.get("targets"), list)
            or len(block["targets"]) != target_count
            or any(
                not isinstance(row, Mapping) or row.get("target_index") != index
                for index, row in enumerate(block["targets"])
            )
        ):
            reasons.append(f"FINAL_{name}_SEMANTICS_INVALID")
    if isinstance(load, Mapping) and isinstance(dynamics, Mapping) and isinstance(team, Mapping):
        shared_fields = ("schema", "config_digest", "environment_generation", "same_timestamp_order")
        if any(
            dynamics.get(field) != load.get(field) or team.get(field) != load.get(field)
            for field in shared_fields
        ):
            reasons.append("FINAL_SEMANTICS_LOAD_IDENTITY_MISMATCH")
        if (
            dynamics.get("attackability_events_total") != len(config.attackability_events)
            or dynamics.get("effective_armor_events_total") != len(config.effective_armor_events)
            or team.get("background_events_total") != len(config.background_damage_events)
            or team.get("retarget_mode") != config.retarget_mode
        ):
            reasons.append("FINAL_SEMANTICS_EVENT_TOTAL_MISMATCH")
    unique = list(dict.fromkeys(reasons))
    return {
        "status": "VALID_CURRENT_SEMANTIC_RECEIPTS" if not unique else "INVALID_SEMANTIC_RECEIPTS",
        "valid": not unique,
        "reason_codes": unique,
        "policy_press_count": len(presses),
        "target_context_count": len(context_receipts),
        "future_team_schedule_visible_to_policy": False,
    }


def _exact_noop_identity(
    cat: Mapping[str, Any], no_op: Mapping[str, Any], *, no_op_interventions: int,
) -> dict[str, Any]:
    """Require byte-structure-equivalent Cat/no-op lane artifacts."""

    keys = (
        "status", "terminal", "mode", "seed", "period_ms", "press_count",
        "press_phase_ms", "press_clock_configured_at_ms", "first_scheduled_press_ms",
        "press_clock_configuration_mode",
        "presses", "final_state", "dynamic_load_receipt",
    )
    differences = [key for key in keys if cat.get(key) != no_op.get(key)]
    exact = not differences and no_op_interventions == 0
    return {
        "status": "EXACT_CAT_NOOP_IDENTITY" if exact else "CAT_NOOP_IDENTITY_FAILED",
        "exact": exact,
        "no_op_intervention_count": no_op_interventions,
        "differing_fields": differences,
        "cat_press_count": cat.get("press_count"),
        "no_op_press_count": no_op.get("press_count"),
    }


def _exact_active_fallback_identity(
    cat: Mapping[str, Any], candidate: Mapping[str, Any], *,
    candidate_interventions: int,
) -> dict[str, Any]:
    """Prove that an active rule which did not trigger remained exact Cat."""

    keys = (
        "status", "terminal", "mode", "seed", "period_ms", "press_count",
        "press_phase_ms", "press_clock_configured_at_ms", "first_scheduled_press_ms",
        "press_clock_configuration_mode",
        "presses", "final_state", "dynamic_load_receipt",
    )
    differences = [key for key in keys if cat.get(key) != candidate.get(key)]
    exact = not differences and candidate_interventions == 0
    return {
        "status": (
            "EXACT_ACTIVE_CAT_FALLBACK_IDENTITY"
            if exact else "ACTIVE_CAT_FALLBACK_IDENTITY_FAILED"
        ),
        "exact": exact,
        "candidate_intervention_count": candidate_interventions,
        "differing_fields": differences,
        "cat_press_count": cat.get("press_count"),
        "candidate_press_count": candidate.get("press_count"),
    }


def _evaluate_pair(
    case: DevelopmentWaveCaseV1,
    rule: FrozenRuleV1,
    bridge_factory: Callable[[], Any],
    *,
    period_ms: int,
    max_presses: int,
    simulator_inputs: CatFurySimulatorInputsV5 | None,
) -> dict[str, Any]:
    """Run Cat, exact no-op, and one proposed/authorized conditional lane."""

    if not isinstance(rule, FrozenRuleV1):
        raise TypeError("rule must be FrozenRuleV1")

    def lane(adapter: Any) -> dict[str, Any]:
        return _run_fresh(
            bridge_factory,
            lambda bridge: _run_press_lane(
                bridge, case, adapter, period_ms=period_ms,
                max_presses=max_presses, simulator_inputs=simulator_inputs,
            ),
        )

    cat = lane(CatFuryFullPolicyAdapterV4())
    no_op_adapter = ConditionalCatBranchCandidateV1(FrozenRuleV1())
    no_op = lane(no_op_adapter)
    no_op_identity = _exact_noop_identity(
        cat, no_op, no_op_interventions=len(no_op_adapter.interventions),
    )
    if rule.kind is None:
        candidate_adapter = no_op_adapter
        candidate = no_op
    else:
        candidate_adapter = ConditionalCatBranchCandidateV1(
            FrozenRuleV1(**asdict(rule))
        )
        candidate = lane(candidate_adapter)

    cat_terminal = _terminal_receipt(case, cat)
    no_op_terminal = _terminal_receipt(case, no_op)
    candidate_terminal = _terminal_receipt(case, candidate)
    semantics = {
        "cat": _semantic_receipt(case, cat),
        "no_op": _semantic_receipt(case, no_op),
        "candidate": _semantic_receipt(case, candidate),
    }
    interventions = candidate_adapter.interventions
    active = rule.kind is not None
    active_fallback_identity = _exact_active_fallback_identity(
        cat, candidate, candidate_interventions=len(interventions),
    )
    prefix_verified = False
    prefix_press_count: int | None = None
    action_accepted = False
    intervention: dict[str, Any] | None = None
    if active and len(interventions) == 1:
        intervention = interventions[0]
        index = intervention.get("decision_index")
        if type(index) is int:
            try:
                prefix_press_count = _same_press_prefix(cat, candidate, index)
                prefix_verified = True
                cat_press = next(
                    press for press in cat["presses"]
                    if press.get("decision_index") == index
                )
                candidate_press = next(
                    press for press in candidate["presses"]
                    if press.get("decision_index") == index
                )
                action_accepted, _ = _branch_action_accepted(
                    str(rule.kind), cat_press, candidate_press,
                )
            except (BranchReplayMismatchV1, IndexError, StopIteration):
                prefix_verified = False
    strict_intervention = active and len(interventions) == 1 and prefix_verified and action_accepted
    all_terminals = all(
        terminal.get("status") == "COMPLETED"
        for terminal in (cat_terminal, no_op_terminal, candidate_terminal)
    )
    semantics_valid = all(row["valid"] for row in semantics.values())
    technical_ready = bool(
        no_op_identity["exact"] and all_terminals and semantics_valid and strict_intervention
    )
    active_fallback_ready = bool(
        active and len(interventions) == 0
        and no_op_identity["exact"] and active_fallback_identity["exact"]
        and all_terminals and semantics_valid
    )
    fallback_ready = bool(
        no_op_identity["exact"] and all_terminals and semantics_valid
        and not active and len(interventions) == 0
    )
    delta = (
        _normalized_paired_delta(
            candidate_terminal["own_effective_damage"], cat_terminal["own_effective_damage"],
        )
        if technical_ready else 0.0 if active_fallback_ready else None
    )
    return {
        "schema": "factored_external_press_fresh_pair/v1",
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        **_case_tracking(case),
        "period_ms": period_ms,
        "rule": asdict(rule),
        "rule_active": active,
        "status": (
            "COMPLETE_FRESH_PAIR" if technical_ready or active_fallback_ready
            else "EXACT_CAT_NOOP_FALLBACK" if fallback_ready
            else "INCOMPLETE_FRESH_PAIR"
        ),
        "no_op_identity_gate": no_op_identity,
        "active_cat_fallback_identity_gate": active_fallback_identity,
        "press_clock_configuration_modes": {
            "cat": cat.get("press_clock_configuration_mode"),
            "no_op": no_op.get("press_clock_configuration_mode"),
            "candidate": candidate.get("press_clock_configuration_mode"),
        },
        "semantic_receipts": semantics,
        "cat_terminal": cat_terminal,
        "no_op_terminal": no_op_terminal,
        "candidate_terminal": candidate_terminal,
        "strict_single_intervention_verified": strict_intervention,
        "active_cat_fallback_verified": active_fallback_ready,
        "candidate_intervention_count": len(interventions),
        "candidate_intervention": _wire(intervention),
        "candidate_branch_action_accepted": action_accepted,
        "accepted_prefix_presses_verified": (
            prefix_press_count if strict_intervention else None
        ),
        "paired_effective_damage_delta": delta,
        "technical_receipts_ready": technical_ready or active_fallback_ready or fallback_ready,
        "comparison_ready": technical_ready or active_fallback_ready,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _phase_cases(
    training_cases: Iterable[DevelopmentWaveCaseV1],
    transfer_cases: Iterable[DevelopmentWaveCaseV1],
    untouched_cases: Iterable[DevelopmentWaveCaseV1],
) -> tuple[tuple[DevelopmentWaveCaseV1, ...], ...]:
    phases = tuple(tuple(rows) for rows in (training_cases, transfer_cases, untouched_cases))
    if any(not rows for rows in phases):
        raise ValueError("training, transfer, and untouched phases must be nonempty")
    for rows in phases:
        if any(not isinstance(case, DevelopmentWaveCaseV1) for case in rows):
            raise TypeError("all phase rows must be DevelopmentWaveCaseV1")
        identities = [
            (case.dynamic_load.seed, case.dynamic_load.request_sha256,
             case.dynamic_load.contract_sha256)
            for case in rows
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("a phase contains a duplicate case identity")
    seed_sets = [{case.dynamic_load.seed for case in rows} for rows in phases]
    if any(seed_sets[i] & seed_sets[j] for i in range(3) for j in range(i + 1, 3)):
        raise ValueError("training, transfer, and untouched seeds must be disjoint")
    return phases


def run_factored_external_press_search_v1(
    training_cases: Iterable[DevelopmentWaveCaseV1],
    transfer_cases: Iterable[DevelopmentWaveCaseV1],
    untouched_cases: Iterable[DevelopmentWaveCaseV1],
    bridge_factory: Callable[[], Any],
    *,
    period_ms: int = 100,
    max_states: int = 1,
    max_presses: int = 400,
    teacher_min_distinct_seeds: int = 6,
    transfer_min_distinct_seeds: int = 8,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    item_database: Mapping[str, Any] | None = None,
    talent_position_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run proposal, transfer authorization, then untouched fresh evaluation."""

    training, transfer, untouched = _phase_cases(
        training_cases, transfer_cases, untouched_cases,
    )
    teachers = [
        (
            case,
            run_cat_external_press_action_teacher_v1(
                case, bridge_factory, period_ms=period_ms, max_states=max_states,
                max_presses=max_presses, simulator_inputs=simulator_inputs,
            ),
        )
        for case in training
    ]
    proposal_router = fit_factored_cat_branch_router_v1(
        teachers,
        min_distinct_seeds=teacher_min_distinct_seeds,
        item_database=item_database,
        talent_position_map=talent_position_map,
    )
    transfer_pairs = []
    for case in transfer:
        route, proposal = proposed_rule_for_case_v1(
            proposal_router, case,
            item_database=item_database,
            talent_position_map=talent_position_map,
        )
        pair = _evaluate_pair(
            case, proposal, bridge_factory, period_ms=period_ms,
            max_presses=max_presses, simulator_inputs=simulator_inputs,
        )
        pair["mechanism_route"] = asdict(route)
        pair["phase"] = "HELD_OUT_TRANSFER"
        transfer_pairs.append(pair)
    authorized_router = authorize_factored_routes_v1(
        proposal_router, transfer_pairs,
        min_distinct_seeds=transfer_min_distinct_seeds,
    )
    untouched_pairs = []
    for case in untouched:
        route, authorized_rule = rule_for_case_v1(
            authorized_router, case,
            item_database=item_database,
            talent_position_map=talent_position_map,
        )
        pair = _evaluate_pair(
            case, authorized_rule, bridge_factory, period_ms=period_ms,
            max_presses=max_presses, simulator_inputs=simulator_inputs,
        )
        pair["mechanism_route"] = asdict(route)
        pair["phase"] = "UNTOUCHED_FRESH"
        untouched_pairs.append(pair)

    all_pairs = transfer_pairs + untouched_pairs
    receipts_ready = all(pair["technical_receipts_ready"] for pair in all_pairs)
    active_untouched = [pair for pair in untouched_pairs if pair["rule_active"]]
    comparison_ready = bool(
        receipts_ready and active_untouched
        and all(pair["comparison_ready"] for pair in active_untouched)
    )
    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_BOUNDED_FACTORED_SEARCH_NONVOTING"
            if receipts_ready else "INCOMPLETE_BOUNDED_FACTORED_SEARCH_NONVOTING"
        ),
        "period_ms": period_ms,
        "phase_seed_contract": "TRAIN_TRANSFER_UNTOUCHED_NUMERIC_SEEDS_DISJOINT",
        "training_cases": [_case_tracking(case) for case in training],
        "transfer_cases": [_case_tracking(case) for case in transfer],
        "untouched_cases": [_case_tracking(case) for case in untouched],
        "teacher_artifacts": [teacher for _, teacher in teachers],
        "proposal_router": proposal_router,
        "transfer_pairs": transfer_pairs,
        "authorized_router": authorized_router,
        "untouched_fresh_pairs": untouched_pairs,
        "authorized_route_count": authorized_router["eligible_fresh_test_route_count"],
        "active_untouched_pair_count": len(active_untouched),
        "all_semantic_terminal_clock_receipts_valid": receipts_ready,
        "comparison_ready": comparison_ready,
        "voting_eligible": False,
        "deployment_eligible": False,
        "policy_update_rounds_completed": 0,
        "scientific_run_launched": False,
    }


__all__ = (
    "run_factored_external_press_search_v1",
    "_evaluate_pair",
    "_exact_noop_identity",
    "_semantic_receipt",
)
