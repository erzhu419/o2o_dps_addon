"""One-shot Cat Heroic Strike queue opportunity with a terminal latch.

The Cat source adapter remains the sole baseline controller and is called
exactly once at every decision. On the first *current* legal and ready Heroic
Strike queue opportunity in the middle-health phase, inactive Flurry queues
Heroic Strike once; active Flurry permanently abstains. Missing or malformed
current-action evidence at a broadly offered opportunity resolves to UNKNOWN
instead of allowing a later observation to change the adjudication.
"""

from __future__ import annotations

from typing import Any, Mapping

from .cat_action_branch_search_v1 import (
    apply_action_branch_v1,
    available_branches_v1,
)
from .cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    validate_source_decision_v4,
)
from .cat_sparse_guard_policy_v2 import sparse_guard_features_v2
from .expert_policy import ExpertDecision, SwingQueueOp
from .expert_proposals import QUEUE_REFS
from .sim_bridge import ActionRef, AvailableAction, SimBridgeProtocolError


POLICY_ID = "cat_latched_first_opportunity_policy/v1"
HS_ACTION_REF = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]

RESOLUTION_INTERVENED = "INTERVENED"
RESOLUTION_ABSTAINED_ACTIVE = "ABSTAINED_ACTIVE"
RESOLUTION_NO_PARENT_OPPORTUNITY = "NO_PARENT_OPPORTUNITY"
RESOLUTION_UNKNOWN = "UNKNOWN"


def _available_action_to_wire(row: AvailableAction) -> dict[str, Any]:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
    }


def _normalize_action_snapshot(rows: Any) -> tuple[AvailableAction, ...]:
    if not isinstance(rows, (list, tuple)):
        raise TypeError("current available actions must be a list or tuple")
    normalized: list[AvailableAction] = []
    for row in rows:
        if isinstance(row, AvailableAction):
            normalized.append(row)
        elif isinstance(row, Mapping):
            normalized.append(AvailableAction.from_wire(row))
        else:
            raise TypeError("each current available action must be typed or mapped")
    return tuple(normalized)


def _parse_action_ref(value: Any) -> ActionRef | None:
    if isinstance(value, ActionRef):
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        return ActionRef.from_wire(value)
    except (TypeError, ValueError, SimBridgeProtocolError):
        return None


def candidate_runner_press_validation_receipt_v1(press: Any) -> dict[str, Any]:
    """Audit one actual v5 ordered-sink event for the intervention.

    Any rejection, missing field, wrong action tag, GCD consumption, or queue
    transition mismatch is an UNKNOWN intervention outcome for paired
    evaluation.
    """

    reasons: list[str] = []
    if not isinstance(press, Mapping):
        reasons.append("PRESS_NOT_MAPPING")
        return {
            "schema": "cat_latched_candidate_runner_press_validation/v1",
            "valid": False,
            "resolution": RESOLUTION_UNKNOWN,
            "reason_codes": reasons,
        }

    source = press.get("source_sink")
    if not isinstance(source, Mapping):
        reasons.append("SOURCE_SINK_MISSING")
    else:
        if source.get("channel") != "swing_queue":
            reasons.append("SOURCE_CHANNEL_MISMATCH")
        if source.get("operation") != "QueueSpellByName":
            reasons.append("SOURCE_OPERATION_MISMATCH")
        if source.get("value") != "英勇打击":
            reasons.append("SOURCE_VALUE_MISMATCH")
        source_ref = source.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.endswith(
            ":ADD_HS_QUEUE"
        ):
            reasons.append("SOURCE_REF_MISMATCH")

    operation = press.get("operation_contract")
    if not isinstance(operation, Mapping):
        reasons.append("OPERATION_CONTRACT_MISSING")
    else:
        if operation.get("recognized") is not True:
            reasons.append("OPERATION_NOT_RECOGNIZED")
        if _parse_action_ref(operation.get("action_ref")) != HS_ACTION_REF:
            reasons.append("OPERATION_ACTION_MISMATCH")

    submission = press.get("simulator_submission")
    if not isinstance(submission, Mapping):
        reasons.append("SUBMISSION_MISSING")
    else:
        if submission.get("status") != "SUBMITTED":
            reasons.append("SUBMISSION_NOT_SUBMITTED")
        if _parse_action_ref(submission.get("action")) != HS_ACTION_REF:
            reasons.append("SUBMISSION_ACTION_MISMATCH")
        available = submission.get("available_action")
        if not isinstance(available, Mapping):
            reasons.append("SUBMISSION_AVAILABLE_ACTION_MISSING")
        else:
            if _parse_action_ref(available.get("action")) != HS_ACTION_REF:
                reasons.append("AVAILABLE_ACTION_MISMATCH")
            if available.get("legal") is not True:
                reasons.append("AVAILABLE_ACTION_NOT_LEGAL")
            if type(available.get("ready_in_ms")) is not int or available.get(
                "ready_in_ms"
            ) != 0:
                reasons.append("AVAILABLE_ACTION_NOT_READY")
            if available.get("triggers_gcd") is not False:
                reasons.append("AVAILABLE_ACTION_GCD_MISMATCH")

    acceptance = press.get("simulator_acceptance")
    if not isinstance(acceptance, Mapping) or acceptance.get("status") != "ACCEPTED":
        reasons.append("SIMULATOR_NOT_ACCEPTED")

    consumption = press.get("decision_consumption")
    if not isinstance(consumption, Mapping):
        reasons.append("DECISION_CONSUMPTION_MISSING")
    else:
        if consumption.get("consumes_decision") is not False:
            reasons.append("DECISION_WAS_CONSUMED")
        if consumption.get("expected_for_lane") is not False:
            reasons.append("EXPECTED_LANE_CONSUMPTION_MISMATCH")
        if consumption.get("available_action_triggers_gcd") is not False:
            reasons.append("DECISION_AVAILABLE_ACTION_GCD_MISMATCH")

    transition = press.get("queue_transition")
    deferred_confirmation_required = False
    if not isinstance(transition, Mapping):
        reasons.append("QUEUE_TRANSITION_MISSING")
    else:
        if transition.get("before") != SwingQueueOp.KEEP.value:
            reasons.append("QUEUE_BEFORE_MISMATCH")
        if transition.get("requested") != SwingQueueOp.HEROIC_STRIKE.value:
            reasons.append("QUEUE_REQUEST_MISMATCH")
        if transition.get("cancel_requested") is not False:
            reasons.append("QUEUE_CANCEL_MISMATCH")
        if (
            transition.get("kind") == "ACCEPTED_QUEUE_STATE_NOT_OBSERVED"
            and transition.get("after") == SwingQueueOp.KEEP.value
        ):
            # Native wowsims applies QueueDelay through a pending action.  Its
            # immediate Apply result can therefore prove submission but not
            # the resulting aura; the full-wave evaluator must bind a later
            # state observation before this becomes a valid intervention.
            deferred_confirmation_required = True
        else:
            if transition.get("kind") != "QUEUED":
                reasons.append("QUEUE_NOT_QUEUED")
            if transition.get("after") != SwingQueueOp.HEROIC_STRIKE.value:
                reasons.append("QUEUE_AFTER_MISMATCH")

    submission_contract_valid = not reasons
    valid = submission_contract_valid and not deferred_confirmation_required
    reason_codes = list(reasons)
    if deferred_confirmation_required:
        reason_codes.append("QUEUE_CONFIRMATION_DEFERRED_REQUIRED")
    return {
        "schema": "cat_latched_candidate_runner_press_validation/v1",
        "valid": valid,
        "resolution": RESOLUTION_INTERVENED if valid else RESOLUTION_UNKNOWN,
        "reason_codes": reason_codes,
        "submission_contract_valid": submission_contract_valid,
        "immediate_queue_confirmed": valid,
        "deferred_confirmation_required": deferred_confirmation_required,
    }


def validate_candidate_runner_press_v1(press: Any) -> bool:
    """Return whether an actual executor event proves the HS intervention."""

    return candidate_runner_press_validation_receipt_v1(press)["valid"] is True


validate_candidate_runner_press = validate_candidate_runner_press_v1


class CatLatchedFirstOpportunityPolicyV1:
    """Cat plus one terminal first-opportunity resolution."""

    expert_id = POLICY_ID

    def __init__(self) -> None:
        self.cat = CatFuryFullPolicyAdapterV4()
        self.decision_count = 0
        self.resolution: str | None = None
        self.resolution_receipts: list[dict[str, Any]] = []
        self._current_available_actions: Any = None

    @property
    def intervention_latched(self) -> bool:
        return self.resolution is not None

    def bind_current_available_actions_v2(self, available_actions: Any) -> None:
        # Normalize after Cat proposes so malformed evidence becomes a terminal
        # UNKNOWN receipt rather than an untyped runner exception.
        self._current_available_actions = available_actions

    bind_current_available_actions_v1 = bind_current_available_actions_v2
    bind_current_available_actions = bind_current_available_actions_v2

    def _resolve(
        self,
        resolution: str,
        *,
        decision_index: int,
        features: Mapping[str, str],
        cat: ExpertDecision,
        candidate: ExpertDecision,
        exact_hs_row: AvailableAction | None,
        reason_code: str,
    ) -> None:
        self.resolution = resolution
        self.resolution_receipts.append({
            "schema": "cat_latched_first_opportunity_resolution/v1",
            "policy_id": POLICY_ID,
            "decision_index": decision_index,
            "resolution": resolution,
            "reason_code": reason_code,
            "current_features": dict(features),
            "exact_hs_available_row": (
                _available_action_to_wire(exact_hs_row)
                if exact_hs_row is not None else None
            ),
            "cat_proposal": cat.to_dict(),
            "candidate_proposal": candidate.to_dict(),
        })

    def propose(self, state: CatFuryFullPolicyStateV4) -> ExpertDecision:
        if not isinstance(state, CatFuryFullPolicyStateV4):
            raise TypeError("state must be CatFuryFullPolicyStateV4")
        decision_index = self.decision_count
        self.decision_count += 1
        raw_actions = self._current_available_actions
        self._current_available_actions = None

        # Exactly one Cat call per decision, including after terminal latch.
        cat = validate_source_decision_v4(self.cat.propose(state))
        if self.resolution is not None:
            return cat

        features = sparse_guard_features_v2(state)
        offered = available_branches_v1(state, cat)
        if features["hp_phase"] != "MIDDLE" or "ADD_HS_QUEUE" not in offered:
            return cat

        # The v5 simulator executor supports this queue sink only through the
        # Nampower QueueSpellByName contract.  Without it the branch builder
        # would emit CastSpellByName, so the first broad parent opportunity is
        # unsupported evidence rather than an opportunity to retry later.
        if state.combat.nampower is not True:
            self._resolve(
                RESOLUTION_UNKNOWN,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=None,
                reason_code="NAMPOWER_QUEUE_OPERATION_UNSUPPORTED",
            )
            return cat

        try:
            actions = _normalize_action_snapshot(raw_actions)
        except (TypeError, ValueError, SimBridgeProtocolError):
            self._resolve(
                RESOLUTION_UNKNOWN,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=None,
                reason_code="CURRENT_ACTION_SNAPSHOT_MISSING_OR_MALFORMED",
            )
            return cat

        hs_rows = tuple(row for row in actions if row.action == HS_ACTION_REF)
        if len(hs_rows) > 1:
            self._resolve(
                RESOLUTION_UNKNOWN,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=None,
                reason_code="AMBIGUOUS_HS_ACTION_ROWS",
            )
            return cat
        if not hs_rows:
            return cat
        exact_hs_row = hs_rows[0]
        if not exact_hs_row.legal or exact_hs_row.ready_in_ms != 0:
            return cat
        if exact_hs_row.triggers_gcd is not False:
            self._resolve(
                RESOLUTION_UNKNOWN,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=exact_hs_row,
                reason_code="HS_ACTION_GCD_CONTRACT_MISMATCH",
            )
            return cat

        flurry = features["flurry_state"]
        if flurry == "ACTIVE":
            self._resolve(
                RESOLUTION_ABSTAINED_ACTIVE,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=exact_hs_row,
                reason_code="FIRST_EXACT_OPPORTUNITY_FLURRY_ACTIVE",
            )
            return cat
        if flurry != "INACTIVE":
            self._resolve(
                RESOLUTION_UNKNOWN,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=exact_hs_row,
                reason_code="FIRST_EXACT_OPPORTUNITY_FLURRY_UNKNOWN",
            )
            return cat

        candidate = apply_action_branch_v1(state, cat, "ADD_HS_QUEUE")
        self._resolve(
            RESOLUTION_INTERVENED,
            decision_index=decision_index,
            features=features,
            cat=cat,
            candidate=candidate,
            exact_hs_row=exact_hs_row,
            reason_code="FIRST_EXACT_OPPORTUNITY_FLURRY_INACTIVE",
        )
        return candidate

    def terminal_resolution_v1(self) -> dict[str, Any]:
        """Return the latched receipt or an explicit no-opportunity terminal."""

        if self.resolution_receipts:
            return dict(self.resolution_receipts[0])
        return {
            "schema": "cat_latched_first_opportunity_resolution/v1",
            "policy_id": POLICY_ID,
            "decision_index": None,
            "resolution": RESOLUTION_NO_PARENT_OPPORTUNITY,
            "reason_code": "NO_EXACT_PARENT_OPPORTUNITY_BEFORE_TERMINAL",
            "decisions_observed": self.decision_count,
            "exact_hs_available_row": None,
        }


CatLatchedFirstOpportunityPolicy = CatLatchedFirstOpportunityPolicyV1

__all__ = (
    "POLICY_ID",
    "HS_ACTION_REF",
    "RESOLUTION_INTERVENED",
    "RESOLUTION_ABSTAINED_ACTIVE",
    "RESOLUTION_NO_PARENT_OPPORTUNITY",
    "RESOLUTION_UNKNOWN",
    "CatLatchedFirstOpportunityPolicyV1",
    "CatLatchedFirstOpportunityPolicy",
    "candidate_runner_press_validation_receipt_v1",
    "validate_candidate_runner_press_v1",
    "validate_candidate_runner_press",
)
