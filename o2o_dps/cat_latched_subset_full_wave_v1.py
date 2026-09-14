"""Strict full-wave adjudication for one frozen latched subset policy.

The selected subset policy owns its identity even when its predicate list is
empty.  Interventions require an actual immediate queue transition or a tag-1
Heroic Strike aura observed before the original main-hand swing deadline.
Terminal abstentions are usable only when the entire candidate lane is exactly
the shared Cat lane; UNKNOWN outcomes are never imputed as zero.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any, Callable, Mapping

from .branch_teacher_v1 import BranchReplayMismatchV1
from .cat_external_press_action_teacher_v1 import (
    _normalized_paired_delta,
    _same_press_prefix,
    _terminal_receipt,
)
from .cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from .cat_latched_first_opportunity_full_wave_v1 import (
    TARGET_ROUTE,
    _candidate_sink_event,
    _compact_action_event,
    _deferred_queue_confirmation,
    _press_for_decision,
)
from .cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
)
from .cat_latched_subset_policy_v1 import (
    POLICY_ID,
    RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
    RESOLUTION_SCHEMA,
    CatLatchedSubsetPolicyV1,
    validate_frozen_subset_guard_v1,
)
from .cat_sparse_guard_policy_v2 import FEATURE_ORDER, _FEATURE_VALUES
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .factored_cat_branch_router_v1 import mechanism_route_v1
from .factored_external_press_matrix_v1 import (
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
)
from .factored_external_press_search_v1 import (
    _exact_active_fallback_identity,
    _semantic_receipt,
)
from .factored_sparse_guard_full_wave_v1 import _lane, _shared_baseline
from .sim_bridge import ActionRef


SCHEMA = "cat_latched_subset_full_wave/v1"


def _action_ref_from_wire(raw: Any) -> ActionRef | None:
    try:
        return raw if isinstance(raw, ActionRef) else ActionRef.from_wire(raw)
    except Exception:
        return None


def _features_valid(raw: Any) -> bool:
    if not isinstance(raw, Mapping) or set(raw) != set(FEATURE_ORDER):
        return False
    return all(
        isinstance(raw.get(name), str)
        and raw.get(name) in _FEATURE_VALUES[name]
        for name in FEATURE_ORDER
    )


def _guard_matches(features: Mapping[str, str], guard: Mapping[str, Any]) -> bool:
    return all(
        features.get(predicate["feature"]) == predicate["value"]
        for predicate in guard["predicates"]
    )


def subset_resolution_receipt_valid_v1(
    receipt: Mapping[str, Any],
    *,
    resolution: str,
    resolution_count: int,
    selected_candidate_id: str,
    frozen_guard: Mapping[str, Any],
) -> bool:
    """Validate policy identity and the exact terminal subset decision."""

    try:
        canonical_guard = validate_frozen_subset_guard_v1(
            selected_candidate_id, frozen_guard,
        )
    except (TypeError, ValueError):
        return False
    if not isinstance(receipt, Mapping) or (
        receipt.get("schema") != RESOLUTION_SCHEMA
        or receipt.get("policy_id") != POLICY_ID
        or receipt.get("selected_candidate_id") != selected_candidate_id
        or receipt.get("frozen_guard") != canonical_guard
        or receipt.get("resolution") != resolution
    ):
        return False

    if resolution == RESOLUTION_NO_PARENT_OPPORTUNITY:
        decisions = receipt.get("decisions_observed")
        return bool(
            resolution_count == 0
            and receipt.get("decision_index") is None
            and type(decisions) is int
            and decisions >= 0
            and receipt.get("reason_code")
            == "NO_EXACT_PARENT_OPPORTUNITY_BEFORE_TERMINAL"
            and receipt.get("guard_evaluated") is False
            and receipt.get("guard_matched") is None
            and receipt.get("exact_hs_available_row") is None
        )

    if (
        resolution_count != 1
        or type(receipt.get("decision_index")) is not int
        or receipt["decision_index"] < 0
        or not _features_valid(receipt.get("current_features"))
    ):
        return False
    features = receipt["current_features"]
    if features.get("hp_phase") != "MIDDLE":
        return False
    available = receipt.get("exact_hs_available_row")
    if not isinstance(available, Mapping) or (
        available.get("legal") is not True
        or available.get("ready_in_ms") != 0
        or available.get("triggers_gcd") is not False
        or _action_ref_from_wire(available.get("action")) != HS_ACTION_REF
    ):
        return False

    if resolution == RESOLUTION_INTERVENED:
        return bool(
            features.get("flurry_state") == "INACTIVE"
            and receipt.get("reason_code")
            == "FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_MATCH"
            and receipt.get("guard_evaluated") is True
            and receipt.get("guard_matched") is True
            and _guard_matches(features, canonical_guard)
        )
    if resolution == RESOLUTION_ABSTAINED_SUBSET_NONMATCH:
        return bool(
            features.get("flurry_state") == "INACTIVE"
            and receipt.get("reason_code")
            == "FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_NONMATCH"
            and receipt.get("guard_evaluated") is True
            and receipt.get("guard_matched") is False
            and not _guard_matches(features, canonical_guard)
            and receipt.get("candidate_proposal") == receipt.get("cat_proposal")
        )
    if resolution == RESOLUTION_ABSTAINED_ACTIVE:
        return bool(
            features.get("flurry_state") == "ACTIVE"
            and receipt.get("reason_code")
            == "FIRST_EXACT_OPPORTUNITY_FLURRY_ACTIVE"
            and receipt.get("guard_evaluated") is False
            and receipt.get("guard_matched") is None
            and receipt.get("candidate_proposal") == receipt.get("cat_proposal")
        )
    return False


def _compact_resolution_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy({
        name: receipt.get(name)
        for name in (
            "schema",
            "policy_id",
            "selected_candidate_id",
            "frozen_guard",
            "decision_index",
            "resolution",
            "reason_code",
            "current_features",
            "guard_evaluated",
            "guard_matched",
            "exact_hs_available_row",
            "cat_proposal",
            "candidate_proposal",
            "decisions_observed",
        )
        if name in receipt
    })


def evaluate_latched_subset_full_wave_case_v1(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    selected_candidate_id: str,
    frozen_guard: Mapping[str, Any],
    *,
    period_ms: int = 100,
    max_presses: int = 400,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    item_database: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run and strictly adjudicate one actual frozen subset policy."""

    if not isinstance(case, DevelopmentWaveCaseV1):
        raise TypeError("case must be DevelopmentWaveCaseV1")
    canonical_guard = validate_frozen_subset_guard_v1(
        selected_candidate_id, frozen_guard,
    )
    route = mechanism_route_v1(case, item_database=item_database)
    if route != TARGET_ROUTE:
        raise ValueError("latched subset is valid only on its exact mechanism route")

    cat, _, shared = _shared_baseline(
        case,
        bridge_factory,
        period_ms=period_ms,
        max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    adapter = CatLatchedSubsetPolicyV1(
        selected_candidate_id=selected_candidate_id,
        frozen_guard=canonical_guard,
    )
    candidate = _lane(
        case,
        adapter,
        bridge_factory,
        period_ms=period_ms,
        max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    terminal = _terminal_receipt(case, candidate)
    semantics = _semantic_receipt(case, candidate)
    resolution_receipt = adapter.terminal_resolution_v1()
    resolution = resolution_receipt.get("resolution")
    resolution_count = len(adapter.resolution_receipts)
    resolution_valid = bool(
        isinstance(resolution, str)
        and subset_resolution_receipt_valid_v1(
            resolution_receipt,
            resolution=resolution,
            resolution_count=resolution_count,
            selected_candidate_id=selected_candidate_id,
            frozen_guard=canonical_guard,
        )
    )
    candidate_ready = bool(
        terminal.get("status") == "COMPLETED"
        and semantics.get("valid") is True
        and candidate.get("press_clock_configuration_mode")
        == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    )
    shared_ready = shared.get("technical_receipts_ready") is True

    fallback_resolution = resolution in {
        RESOLUTION_ABSTAINED_ACTIVE,
        RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
        RESOLUTION_NO_PARENT_OPPORTUNITY,
    }
    fallback_identity = _exact_active_fallback_identity(
        cat,
        candidate,
        candidate_interventions=(1 if resolution == RESOLUTION_INTERVENED else 0),
    )
    prefix_count: int | None = None
    action_event: Mapping[str, Any] | None = None
    action_validation: dict[str, Any] | None = None
    deferred_confirmation: dict[str, Any] | None = None
    proposal_binding_valid = False
    strict_intervention = False
    if resolution == RESOLUTION_INTERVENED and resolution_valid:
        decision_index = resolution_receipt["decision_index"]
        try:
            prefix_count = _same_press_prefix(cat, candidate, decision_index)
            cat_press = _press_for_decision(cat, decision_index)
            candidate_press = _press_for_decision(candidate, decision_index)
            action_event, action_validation = _candidate_sink_event(candidate_press)
            if action_validation.get("deferred_confirmation_required") is True:
                deferred_confirmation = _deferred_queue_confirmation(
                    candidate,
                    decision_index=decision_index,
                    action_event=action_event,
                )
            proposal_binding_valid = bool(
                cat_press.get("proposal") == resolution_receipt.get("cat_proposal")
                and candidate_press.get("proposal")
                == resolution_receipt.get("candidate_proposal")
            )
            strict_intervention = bool(
                (
                    action_validation.get("valid") is True
                    or (
                        action_validation.get("submission_contract_valid") is True
                        and action_validation.get("deferred_confirmation_required") is True
                        and deferred_confirmation is not None
                        and deferred_confirmation.get("valid") is True
                    )
                )
                and proposal_binding_valid
            )
        except (BranchReplayMismatchV1, KeyError):
            strict_intervention = False

    triggered_ready = bool(
        shared_ready
        and candidate_ready
        and resolution_valid
        and resolution == RESOLUTION_INTERVENED
        and strict_intervention
    )
    fallback_ready = bool(
        shared_ready
        and candidate_ready
        and resolution_valid
        and fallback_resolution
        and fallback_identity.get("exact") is True
    )
    comparison_ready = triggered_ready or fallback_ready
    delta = (
        _normalized_paired_delta(
            terminal["own_effective_damage"],
            shared["cat_terminal"]["own_effective_damage"],
        )
        if triggered_ready
        else 0.0 if fallback_ready else None
    )
    if triggered_ready:
        status = "COMPLETE_LATCHED_SUBSET_INTERVENTION_PAIR"
        effect_class = "TRIGGERED_INTERVENTION"
    elif fallback_ready:
        status = "EXACT_CAT_LATCHED_SUBSET_ABSTENTION_FALLBACK"
        effect_class = "EXACT_CAT_FALLBACK_ZERO"
    else:
        status = "UNKNOWN_LATCHED_SUBSET_POLICY_EFFECT_NOT_IMPUTED"
        effect_class = "UNKNOWN_NOT_IMPUTED"

    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "mechanism_route": asdict(route),
        "policy_id": POLICY_ID,
        "selected_candidate_id": selected_candidate_id,
        "frozen_guard": deepcopy(canonical_guard),
        "status": status,
        "effect_class": effect_class,
        "resolution": resolution,
        "resolution_receipt": _compact_resolution_receipt(resolution_receipt),
        "resolution_receipt_valid": resolution_valid,
        "candidate_intervention_count": (
            1 if resolution == RESOLUTION_INTERVENED else 0
        ),
        "candidate_prefix_press_count": prefix_count,
        "candidate_prefix_verified": prefix_count is not None,
        "accepted_prefix_presses_verified": (
            prefix_count if strict_intervention else None
        ),
        "candidate_proposal_binding_valid": proposal_binding_valid,
        "candidate_action_event": _compact_action_event(action_event),
        "candidate_action_validation": deepcopy(action_validation),
        "candidate_deferred_queue_confirmation": deepcopy(deferred_confirmation),
        "strict_single_intervention_verified": strict_intervention,
        "cat_fallback_identity_gate": fallback_identity,
        "cat_fallback_verified": fallback_ready,
        "shared_cat_no_op": deepcopy(shared),
        "candidate_terminal": terminal,
        "candidate_semantic_receipt": semantics,
        "candidate_press_clock_configuration_mode": candidate.get(
            "press_clock_configuration_mode"
        ),
        "paired_effective_damage_delta": delta,
        "technical_receipts_ready": comparison_ready,
        "comparison_ready": comparison_ready,
        "full_press_lanes_retained": False,
        "unknown_effect_imputed": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


__all__ = (
    "SCHEMA",
    "TARGET_ROUTE",
    "subset_resolution_receipt_valid_v1",
    "evaluate_latched_subset_full_wave_case_v1",
)
