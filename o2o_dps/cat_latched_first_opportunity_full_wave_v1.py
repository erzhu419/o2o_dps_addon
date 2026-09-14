"""Strict full-wave adjudication for the latched Heroic Strike candidate.

The adapter is evaluated only on one exact slow-dual-wield mechanism route.
Cat and an explicit Cat no-op are shared once per case. An intervention is
counted only when the observed prefix and the actual v5 ordered-sink receipt
prove the requested queue transition; terminal abstention must be byte-level
Cat fallback. UNKNOWN outcomes are never converted to zero reward.
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
    _wire,
)
from .cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from .cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    POLICY_ID,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
    RESOLUTION_UNKNOWN,
    CatLatchedFirstOpportunityPolicyV1,
    candidate_runner_press_validation_receipt_v1,
)
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .factored_cat_branch_router_v1 import MechanismRouteV1, mechanism_route_v1
from .factored_external_press_matrix_v1 import (
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
)
from .factored_external_press_search_v1 import (
    _exact_active_fallback_identity,
    _semantic_receipt,
)
from .factored_sparse_guard_full_wave_v1 import _lane, _shared_baseline
from .sim_bridge import ActionRef


SCHEMA = "cat_latched_first_opportunity_full_wave/v1"
TARGET_ROUTE = MechanismRouteV1(
    weapon_mode="DUAL_WIELD",
    main_hand_speed_band="slow_2s_plus",
    bloodthirst_known="yes",
    target_count_band="one",
    modeled_hp_budget_band="50k_to_200k",
    wave_topology="SINGLE_WAVE",
    off_hand_speed_band="fast_under_2s",
    target_armor_band="under_2k",
    team_dps_prior_band="16k_plus",
    background_team_ttk_band="under_8s",
    team_prior_source="OFFLINE_REGISTRY_LEAVE_ONE_OUT_RATE_PRIOR",
)


def _action_ref_from_wire(raw: Any) -> ActionRef | None:
    try:
        return raw if isinstance(raw, ActionRef) else ActionRef.from_wire(raw)
    except Exception:
        return None


def _resolution_receipt_valid(
    receipt: Mapping[str, Any], *, resolution: str, resolution_count: int,
) -> bool:
    if (
        receipt.get("schema") != "cat_latched_first_opportunity_resolution/v1"
        or receipt.get("policy_id") != POLICY_ID
        or receipt.get("resolution") != resolution
    ):
        return False
    if resolution == RESOLUTION_NO_PARENT_OPPORTUNITY:
        return bool(
            resolution_count == 0
            and receipt.get("decision_index") is None
            and receipt.get("reason_code")
            == "NO_EXACT_PARENT_OPPORTUNITY_BEFORE_TERMINAL"
        )
    if resolution_count != 1 or type(receipt.get("decision_index")) is not int:
        return False
    features = receipt.get("current_features")
    available = receipt.get("exact_hs_available_row")
    if not isinstance(features, Mapping) or not isinstance(available, Mapping):
        return False
    if (
        features.get("hp_phase") != "MIDDLE"
        or available.get("legal") is not True
        or available.get("ready_in_ms") != 0
        or available.get("triggers_gcd") is not False
        or _action_ref_from_wire(available.get("action")) != HS_ACTION_REF
    ):
        return False
    expected = {
        RESOLUTION_INTERVENED: (
            "INACTIVE", "FIRST_EXACT_OPPORTUNITY_FLURRY_INACTIVE",
        ),
        RESOLUTION_ABSTAINED_ACTIVE: (
            "ACTIVE", "FIRST_EXACT_OPPORTUNITY_FLURRY_ACTIVE",
        ),
    }.get(resolution)
    return bool(
        expected is not None
        and features.get("flurry_state") == expected[0]
        and receipt.get("reason_code") == expected[1]
    )


def _press_for_decision(lane: Mapping[str, Any], decision_index: int) -> Mapping[str, Any]:
    rows = [
        row for row in lane.get("presses") or []
        if isinstance(row, Mapping) and row.get("decision_index") == decision_index
    ]
    if len(rows) != 1:
        raise BranchReplayMismatchV1("latched decision does not bind one press")
    return rows[0]


def _candidate_sink_event(
    candidate_press: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    execution = candidate_press.get("ordered_execution")
    events = execution.get("sink_events") if isinstance(execution, Mapping) else None
    matching = []
    for event in events or []:
        source = event.get("source_sink") if isinstance(event, Mapping) else None
        source_ref = source.get("source_ref") if isinstance(source, Mapping) else None
        if isinstance(source_ref, str) and source_ref.endswith(":ADD_HS_QUEUE"):
            matching.append(event)
    if len(matching) != 1:
        return None, {
            "schema": "cat_latched_candidate_runner_press_validation/v1",
            "valid": False,
            "resolution": RESOLUTION_UNKNOWN,
            "reason_codes": ["EXPECTED_ONE_CANDIDATE_SINK_EVENT"],
        }
    event = matching[0]
    return event, candidate_runner_press_validation_receipt_v1(event)


def _heroic_queue_aura_active(state: Mapping[str, Any]) -> bool:
    for aura in state.get("auras") or []:
        action = aura.get("action") if isinstance(aura, Mapping) else None
        if (
            isinstance(action, Mapping)
            and action.get("tag") == HS_ACTION_REF.tag
            and action.get("spell_id") == HS_ACTION_REF.spell_id
        ):
            return True
    return False


def _press_accepts_heroic_queue(press: Mapping[str, Any]) -> bool:
    execution = press.get("ordered_execution")
    events = execution.get("sink_events") if isinstance(execution, Mapping) else None
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        submission = event.get("simulator_submission")
        acceptance = event.get("simulator_acceptance")
        if (
            isinstance(submission, Mapping)
            and isinstance(acceptance, Mapping)
            and submission.get("status") == "SUBMITTED"
            and _action_ref_from_wire(submission.get("action")) == HS_ACTION_REF
            and acceptance.get("status") == "ACCEPTED"
        ):
            return True
    return False


def _deferred_queue_confirmation(
    candidate: Mapping[str, Any], *, decision_index: int,
    action_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind native pending QueueDelay acceptance to a later visible queue aura."""

    before = action_event.get("simulator_state_before") if isinstance(action_event, Mapping) else None
    submitted_at = before.get("time_ms") if isinstance(before, Mapping) else None
    mh_remaining = before.get("mh_swing_remaining_ms") if isinstance(before, Mapping) else None
    if type(submitted_at) is not int or type(mh_remaining) is not int:
        return {
            "status": "UNKNOWN_QUEUE_CONFIRMATION_BOUNDARY_MISSING",
            "valid": False,
            "observed_at_ms": None,
            "reason_code": "SUBMISSION_TIME_OR_MH_DEADLINE_MISSING",
        }
    mh_deadline = submitted_at + mh_remaining
    presses = candidate.get("presses") or []
    start = next((
        index for index, press in enumerate(presses)
        if isinstance(press, Mapping)
        and press.get("decision_index") == decision_index
    ), None)
    if start is None:
        return {
            "status": "UNKNOWN_QUEUE_CONFIRMATION_PRESS_MISSING",
            "valid": False,
            "observed_at_ms": None,
            "reason_code": "RESOLUTION_PRESS_NOT_FOUND",
        }
    observations = []

    def observe(
        key: str,
        state: Any,
        *,
        press_list_index: int,
        press_decision_index: Any,
    ) -> dict[str, Any] | None:
        observed_at = state.get("time_ms") if isinstance(state, Mapping) else None
        if (
            type(observed_at) is not int
            or observed_at < submitted_at
            or observed_at >= mh_deadline
        ):
            return None
        observations.append((key, observed_at, state))
        if not _heroic_queue_aura_active(state):
            return None
        return {
            "status": "DEFERRED_QUEUE_AURA_CONFIRMED_BEFORE_MH_SWING",
            "valid": True,
            "submitted_at_ms": submitted_at,
            "mh_swing_remaining_ms": mh_remaining,
            "observed_at_ms": observed_at,
            "delay_ms": observed_at - submitted_at,
            "mh_swing_deadline_ms": mh_deadline,
            "observation_source": key,
            "observation_press_list_index": press_list_index,
            "observation_decision_index": press_decision_index,
            "no_later_accepted_hs_before_confirmation": True,
            "action": HS_ACTION_REF.to_wire(),
            "reason_code": None,
        }

    for index, press in enumerate(presses[start:], start=start):
        if not isinstance(press, Mapping):
            continue
        if index == start:
            confirmed = observe(
                "simulator_state_after_press",
                press.get("simulator_state_after_press"),
                press_list_index=index,
                press_decision_index=press.get("decision_index"),
            )
            if confirmed is not None:
                return confirmed
            continue

        before = press.get("simulator_state_before")
        before_at = before.get("time_ms") if isinstance(before, Mapping) else None
        if type(before_at) is not int or before_at < submitted_at:
            return {
                "status": "UNKNOWN_QUEUE_CONFIRMATION_BOUNDARY_MISSING",
                "valid": False,
                "observed_at_ms": None,
                "mh_swing_deadline_ms": mh_deadline,
                "observation_count": len(observations),
                "reason_code": "LATER_PRESS_BEFORE_STATE_MISSING",
            }
        if before_at >= mh_deadline:
            break
        confirmed = observe(
            "simulator_state_before",
            before,
            press_list_index=index,
            press_decision_index=press.get("decision_index"),
        )
        if confirmed is not None:
            return confirmed
        if _press_accepts_heroic_queue(press):
            # A later Cat decision can independently queue the same action.
            # Once that happens, its after-state cannot identify which
            # submission caused the aura, so the original intervention is
            # deliberately left UNKNOWN.
            return {
                "status": "UNKNOWN_AMBIGUOUS_LATER_HS_SUBMISSION",
                "valid": False,
                "observed_at_ms": before_at,
                "mh_swing_deadline_ms": mh_deadline,
                "observation_count": len(observations),
                "reason_code": "LATER_HS_SUBMISSION_PRECEDES_QUEUE_AURA_PROOF",
            }
        confirmed = observe(
            "simulator_state_after_press",
            press.get("simulator_state_after_press"),
            press_list_index=index,
            press_decision_index=press.get("decision_index"),
        )
        if confirmed is not None:
            return confirmed
    return {
        "status": "UNKNOWN_DEFERRED_QUEUE_AURA_NOT_OBSERVED",
        "valid": False,
        "observed_at_ms": None,
        "mh_swing_deadline_ms": mh_deadline,
        "observation_count": len(observations),
        "reason_code": "QUEUE_AURA_NOT_OBSERVED_BEFORE_MH_SWING",
    }


def _compact_resolution_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Retain the causal latch proof without retaining a full lane snapshot."""

    return deepcopy({
        name: receipt.get(name)
        for name in (
            "schema",
            "policy_id",
            "decision_index",
            "resolution",
            "reason_code",
            "current_features",
            "exact_hs_available_row",
            "cat_proposal",
            "candidate_proposal",
            "decisions_observed",
        )
        if name in receipt
    })


def _compact_action_event(event: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Retain the ordered-sink receipt, not its duplicate before/after states."""

    if not isinstance(event, Mapping):
        return None
    return deepcopy({
        name: event.get(name)
        for name in (
            "source_sink",
            "operation_contract",
            "simulator_submission",
            "simulator_acceptance",
            "decision_consumption",
            "queue_transition",
        )
        if name in event
    })


def evaluate_latched_full_wave_case_v1(
    case: DevelopmentWaveCaseV1,
    bridge_factory: Callable[[], Any],
    *,
    period_ms: int = 100,
    max_presses: int = 400,
    simulator_inputs: CatFurySimulatorInputsV5 | None = None,
    item_database: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run and strictly adjudicate one actual route-local candidate policy."""

    if not isinstance(case, DevelopmentWaveCaseV1):
        raise TypeError("case must be DevelopmentWaveCaseV1")
    route = mechanism_route_v1(case, item_database=item_database)
    if route != TARGET_ROUTE:
        raise ValueError("latched candidate is valid only on its exact mechanism route")

    cat, _, shared = _shared_baseline(
        case,
        bridge_factory,
        period_ms=period_ms,
        max_presses=max_presses,
        simulator_inputs=simulator_inputs,
    )
    adapter = CatLatchedFirstOpportunityPolicyV1()
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
        and _resolution_receipt_valid(
            resolution_receipt,
            resolution=resolution,
            resolution_count=resolution_count,
        )
    )
    candidate_ready = bool(
        terminal.get("status") == "COMPLETED"
        and semantics.get("valid") is True
        and candidate.get("press_clock_configuration_mode")
        == REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    )
    shared_ready = shared.get("technical_receipts_ready") is True

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

    fallback_resolution = resolution in {
        RESOLUTION_ABSTAINED_ACTIVE,
        RESOLUTION_NO_PARENT_OPPORTUNITY,
    }
    triggered_ready = bool(
        shared_ready and candidate_ready and resolution_valid
        and resolution == RESOLUTION_INTERVENED and strict_intervention
    )
    fallback_ready = bool(
        shared_ready and candidate_ready and resolution_valid
        and fallback_resolution and fallback_identity.get("exact") is True
    )
    comparison_ready = triggered_ready or fallback_ready
    delta = (
        _normalized_paired_delta(
            terminal["own_effective_damage"],
            shared["cat_terminal"]["own_effective_damage"],
        )
        if triggered_ready else 0.0 if fallback_ready else None
    )
    if triggered_ready:
        status = "COMPLETE_LATCHED_INTERVENTION_PAIR"
        effect_class = "TRIGGERED_INTERVENTION"
    elif fallback_ready:
        status = "EXACT_CAT_LATCHED_ABSTENTION_FALLBACK"
        effect_class = "EXACT_CAT_FALLBACK_ZERO"
    else:
        status = "UNKNOWN_LATCHED_POLICY_EFFECT_NOT_IMPUTED"
        effect_class = "UNKNOWN_NOT_IMPUTED"

    return {
        "schema": SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "mechanism_route": asdict(route),
        "status": status,
        "effect_class": effect_class,
        "resolution": resolution,
        "resolution_receipt": _compact_resolution_receipt(resolution_receipt),
        "resolution_receipt_valid": resolution_valid,
        "candidate_intervention_count": 1 if resolution == RESOLUTION_INTERVENED else 0,
        "candidate_prefix_press_count": prefix_count,
        "candidate_prefix_verified": prefix_count is not None,
        "accepted_prefix_presses_verified": prefix_count if strict_intervention else None,
        "candidate_proposal_binding_valid": proposal_binding_valid,
        "candidate_action_event": _compact_action_event(action_event),
        "candidate_action_validation": deepcopy(action_validation),
        "candidate_deferred_queue_confirmation": deepcopy(deferred_confirmation),
        "strict_single_intervention_verified": strict_intervention,
        "active_cat_fallback_identity_gate": fallback_identity,
        "active_cat_fallback_verified": fallback_ready,
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
    "evaluate_latched_full_wave_case_v1",
)
