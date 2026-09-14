"""Frozen current-observation subset around the first latched HS opportunity.

Cat remains the controller and is called exactly once at every decision.  The
first exact middle-health Heroic Strike opportunity is terminal: active Flurry
or an inactive nonmatching guard permanently falls back to Cat, while an
inactive matching guard queues Heroic Strike once.  The guard sees only the
features computed from that current observation.
"""

from __future__ import annotations

from copy import deepcopy
import re
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
from .cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
    RESOLUTION_UNKNOWN,
    _available_action_to_wire,
    _normalize_action_snapshot,
)
from .cat_sparse_guard_policy_v2 import (
    FEATURE_ORDER,
    _FEATURE_VALUES,
    sparse_guard_features_v2,
)
from .expert_policy import ExpertDecision
from .sim_bridge import AvailableAction, SimBridgeProtocolError


POLICY_ID = "cat_latched_subset_policy/v1"
RESOLUTION_ABSTAINED_SUBSET_NONMATCH = "ABSTAINED_SUBSET_NONMATCH"
RESOLUTION_SCHEMA = "cat_latched_subset_resolution/v1"
PARENT_DECISION_CONTRACT = (
    "FIRST_EXACT_BROAD_OPPORTUNITY_ONCE;SUBSET_ONLY_OF_EXECUTED_"
    "FLURRY_INACTIVE_INTERVENTIONS;NONMATCH_CAT_FALLBACK"
)

_GUARD_KEYS = frozenset({
    "decision_contract",
    "action",
    "predicate_count",
    "predicates",
    "nonmatch_action",
})


def validate_frozen_subset_guard_v1(
    selected_candidate_id: Any,
    frozen_guard: Any,
) -> dict[str, Any]:
    """Return a canonical copy of one learner-produced frozen guard."""

    if not isinstance(selected_candidate_id, str) or re.fullmatch(
        r"subset-[0-9]{4}", selected_candidate_id,
    ) is None:
        raise ValueError("selected_candidate_id must use the subset-NNNN form")
    if not isinstance(frozen_guard, Mapping) or set(frozen_guard) != _GUARD_KEYS:
        raise ValueError("frozen guard fields differ from the subset contract")
    predicates = frozen_guard.get("predicates")
    predicate_count = frozen_guard.get("predicate_count")
    if (
        frozen_guard.get("decision_contract") != PARENT_DECISION_CONTRACT
        or frozen_guard.get("action") != "ADD_HS_QUEUE"
        or frozen_guard.get("nonmatch_action") != "EXACT_CAT_FALLBACK"
        or type(predicate_count) is not int
        or not 0 <= predicate_count <= 2
        or not isinstance(predicates, list)
        or len(predicates) != predicate_count
    ):
        raise ValueError("frozen guard semantics differ from the subset contract")

    normalized_predicates: list[dict[str, str]] = []
    seen: set[str] = set()
    feature_positions: list[int] = []
    for predicate in predicates:
        if not isinstance(predicate, Mapping) or set(predicate) != {
            "feature", "value",
        }:
            raise ValueError("each frozen predicate must contain feature and value")
        feature = predicate.get("feature")
        value = predicate.get("value")
        if not isinstance(feature, str) or feature not in FEATURE_ORDER:
            raise ValueError("frozen predicate feature is unsupported")
        if feature in seen:
            raise ValueError("frozen predicate features must be unique")
        if not isinstance(value, str) or value not in _FEATURE_VALUES[feature]:
            raise ValueError("frozen predicate value is unsupported")
        seen.add(feature)
        feature_positions.append(FEATURE_ORDER.index(feature))
        normalized_predicates.append({"feature": feature, "value": value})
    if feature_positions != sorted(feature_positions):
        raise ValueError("frozen predicates must follow canonical feature order")

    return {
        "decision_contract": PARENT_DECISION_CONTRACT,
        "action": "ADD_HS_QUEUE",
        "predicate_count": predicate_count,
        "predicates": normalized_predicates,
        "nonmatch_action": "EXACT_CAT_FALLBACK",
    }


def _guard_matches(
    features: Mapping[str, str], frozen_guard: Mapping[str, Any],
) -> bool:
    return all(
        features.get(predicate["feature"]) == predicate["value"]
        for predicate in frozen_guard["predicates"]
    )


class CatLatchedSubsetPolicyV1:
    """Cat plus one frozen, terminal first-opportunity subset decision."""

    expert_id = POLICY_ID

    def __init__(
        self,
        *,
        selected_candidate_id: str,
        frozen_guard: Mapping[str, Any],
    ) -> None:
        self.selected_candidate_id = selected_candidate_id
        self.frozen_guard = validate_frozen_subset_guard_v1(
            selected_candidate_id, frozen_guard,
        )
        self.cat = CatFuryFullPolicyAdapterV4()
        self.decision_count = 0
        self.resolution: str | None = None
        self.resolution_receipts: list[dict[str, Any]] = []
        self._current_available_actions: Any = None

    @property
    def intervention_latched(self) -> bool:
        return self.resolution is not None

    def bind_current_available_actions_v2(self, available_actions: Any) -> None:
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
        guard_evaluated: bool,
        guard_matched: bool | None,
        reason_code: str,
    ) -> None:
        self.resolution = resolution
        self.resolution_receipts.append({
            "schema": RESOLUTION_SCHEMA,
            "policy_id": POLICY_ID,
            "selected_candidate_id": self.selected_candidate_id,
            "frozen_guard": deepcopy(self.frozen_guard),
            "decision_index": decision_index,
            "resolution": resolution,
            "reason_code": reason_code,
            "current_features": dict(features),
            "guard_evaluated": guard_evaluated,
            "guard_matched": guard_matched,
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

        # This remains true after terminal resolution: the continuation is the
        # same stateful Cat instance, not an independently implemented policy.
        cat = validate_source_decision_v4(self.cat.propose(state))
        if self.resolution is not None:
            return cat

        features = sparse_guard_features_v2(state)
        offered = available_branches_v1(state, cat)
        if features["hp_phase"] != "MIDDLE" or "ADD_HS_QUEUE" not in offered:
            return cat

        if state.combat.nampower is not True:
            self._resolve(
                RESOLUTION_UNKNOWN,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=None,
                guard_evaluated=False,
                guard_matched=None,
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
                guard_evaluated=False,
                guard_matched=None,
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
                guard_evaluated=False,
                guard_matched=None,
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
                guard_evaluated=False,
                guard_matched=None,
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
                guard_evaluated=False,
                guard_matched=None,
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
                guard_evaluated=False,
                guard_matched=None,
                reason_code="FIRST_EXACT_OPPORTUNITY_FLURRY_UNKNOWN",
            )
            return cat

        matched = _guard_matches(features, self.frozen_guard)
        if not matched:
            self._resolve(
                RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
                decision_index=decision_index,
                features=features,
                cat=cat,
                candidate=cat,
                exact_hs_row=exact_hs_row,
                guard_evaluated=True,
                guard_matched=False,
                reason_code="FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_NONMATCH",
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
            guard_evaluated=True,
            guard_matched=True,
            reason_code="FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_MATCH",
        )
        return candidate

    def terminal_resolution_v1(self) -> dict[str, Any]:
        if self.resolution_receipts:
            return deepcopy(self.resolution_receipts[0])
        return {
            "schema": RESOLUTION_SCHEMA,
            "policy_id": POLICY_ID,
            "selected_candidate_id": self.selected_candidate_id,
            "frozen_guard": deepcopy(self.frozen_guard),
            "decision_index": None,
            "resolution": RESOLUTION_NO_PARENT_OPPORTUNITY,
            "reason_code": "NO_EXACT_PARENT_OPPORTUNITY_BEFORE_TERMINAL",
            "decisions_observed": self.decision_count,
            "guard_evaluated": False,
            "guard_matched": None,
            "exact_hs_available_row": None,
        }


__all__ = (
    "POLICY_ID",
    "RESOLUTION_SCHEMA",
    "PARENT_DECISION_CONTRACT",
    "RESOLUTION_ABSTAINED_SUBSET_NONMATCH",
    "CatLatchedSubsetPolicyV1",
    "validate_frozen_subset_guard_v1",
)
