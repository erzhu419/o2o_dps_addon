"""Proposal-only adapters for finite wave action-sequence search.

The search owns action membership: every expansion starts from the simulator's
current ``AvailableAction`` rows.  Guides in this module assign finite weights
to that complete legal set.  Missing guide support therefore becomes weight
zero instead of removing a candidate.

The offline adapter intentionally requires a caller-supplied context factory.
That boundary prevents this module from inventing Chronicle observations from
simulator state fields that do not have the same semantics.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping, Sequence

from .offline_action_sequence_guide_v1 import (
    BuildMatchReceiptV1,
    OfflineActionGuideResultV1,
    OfflineActionSequenceGuideV1,
    OfflineGuideContextV1,
)
from .sim_bridge import ActionRef, AvailableAction
from .wave_action_schedule_v1 import ScheduledActionPlan
from .wave_action_sequence_search_v1 import ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
PriorityFactoryV1 = Callable[
    [ScheduleReplayOutcomeV1, Sequence[ScheduledActionPlan]],
    Mapping[ActionRef, float],
]
OfflineGuideContextFactoryV1 = Callable[
    [ScheduleReplayOutcomeV1, Sequence[ScheduledActionPlan]],
    OfflineGuideContextV1,
]


class WaveActionGuideV1Error(ValueError):
    """A guide identifier, context, or proposed priority is invalid."""


def _guide_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WaveActionGuideV1Error("guide_id must be nonempty text")
    return value.strip()


def _legal_action_refs(
    available_actions: Sequence[AvailableAction],
) -> tuple[ActionRef, ...]:
    result: list[ActionRef] = []
    seen: set[ActionRef] = set()
    for row in available_actions:
        if not isinstance(row, AvailableAction):
            raise WaveActionGuideV1Error(
                "outcome.available_actions must contain AvailableAction rows"
            )
        if row.legal and row.action not in seen:
            seen.add(row.action)
            result.append(row.action)
    return tuple(result)


def _complete_legal_priorities(
    available_actions: Sequence[AvailableAction],
    proposed: Mapping[ActionRef, float],
) -> dict[ActionRef, float]:
    if not isinstance(proposed, Mapping):
        raise WaveActionGuideV1Error("priority factory must return a mapping")
    normalized: dict[ActionRef, float] = {}
    for action, value in proposed.items():
        if not isinstance(action, ActionRef):
            raise WaveActionGuideV1Error("priority keys must be ActionRef")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise WaveActionGuideV1Error("priority values must be finite numbers")
        normalized[action] = float(value)
    return {
        action: normalized.get(action, 0.0)
        for action in _legal_action_refs(available_actions)
    }


def complete_legal_action_priorities_v1(
    available_actions: Sequence[AvailableAction],
    proposed: Mapping[ActionRef, float],
) -> dict[ActionRef, float]:
    """Project arbitrary proposal weights onto the full current legal set."""

    return _complete_legal_priorities(available_actions, proposed)


@dataclass(frozen=True)
class StaticActionGuideV1:
    """A global priority table projected onto each current legal action set."""

    guide_id: str
    priorities: Mapping[ActionRef, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "guide_id", _guide_id(self.guide_id))
        normalized = _complete_legal_priorities(
            tuple(
                AvailableAction(index, action, "", True, 0, False)
                for index, action in enumerate(self.priorities)
            ),
            self.priorities,
        )
        object.__setattr__(self, "priorities", normalized)

    def action_priorities(
        self,
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> Mapping[ActionRef, float]:
        del prefix
        return _complete_legal_priorities(
            outcome.available_actions,
            self.priorities,
        )


class CallableActionGuideV1:
    """Adapt a Cat/Contra-style callable without granting it action authority."""

    def __init__(self, guide_id: str, priority_factory: PriorityFactoryV1) -> None:
        self.guide_id = _guide_id(guide_id)
        if not callable(priority_factory):
            raise TypeError("priority_factory must be callable")
        self._priority_factory = priority_factory

    def action_priorities(
        self,
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> Mapping[ActionRef, float]:
        return _complete_legal_priorities(
            outcome.available_actions,
            self._priority_factory(outcome, prefix),
        )


@dataclass(frozen=True)
class OfflineSearchGuideAuditV1:
    """Bounded audit summary accumulated while an offline guide ranks nodes."""

    guide_id: str
    invocation_count: int
    build_match_receipts: tuple[BuildMatchReceiptV1, ...]
    source_build_scope: str
    backend: str
    model_path: str
    context_contract: Mapping[str, Any]

    def to_dict(self) -> JSONMap:
        if not self.build_match_receipts:
            eligibility = "NOT_EVALUATED"
        elif all(
            receipt.same_build_comparison_eligible
            for receipt in self.build_match_receipts
        ):
            eligibility = "ALL_EXACT_BUILD_MATCH"
        else:
            eligibility = "INELIGIBLE_RECEIPT_PRESENT"
        return {
            "schema": "offline_sequence_search_guide_audit/v1",
            "guide_id": self.guide_id,
            "invocation_count": self.invocation_count,
            "source_build_scope": self.source_build_scope,
            "backend": self.backend,
            "model_path": self.model_path,
            "source_role": "OFFLINE_GUIDE_NOT_BASELINE",
            "context_contract": deepcopy(dict(self.context_contract)),
            "unique_build_match_receipt_count": len(self.build_match_receipts),
            "same_build_comparison_eligibility": eligibility,
            "build_match_receipts": [
                receipt.to_dict() for receipt in self.build_match_receipts
            ],
            "contract": {
                "guide_only_orders_legal_simulator_actions": True,
                "unsupported_legal_actions_receive_zero_weight": True,
                "guide_can_filter_search_action_universe": False,
                "guide_is_same_equipment_baseline": False,
            },
        }


class OfflineActionSequenceSearchGuideV1:
    """Expose ``OfflineActionSequenceGuideV1`` through ``ActionGuideV1``."""

    def __init__(
        self,
        guide_id: str,
        offline_guide: OfflineActionSequenceGuideV1,
        context_factory: OfflineGuideContextFactoryV1,
        *,
        context_contract: Mapping[str, Any] | None = None,
    ) -> None:
        self.guide_id = _guide_id(guide_id)
        if not isinstance(offline_guide, OfflineActionSequenceGuideV1):
            raise TypeError("offline_guide must be OfflineActionSequenceGuideV1")
        if not callable(context_factory):
            raise TypeError("context_factory must be callable")
        if context_contract is not None and not isinstance(
            context_contract, Mapping
        ):
            raise TypeError("context_contract must be a mapping or None")
        self._offline_guide = offline_guide
        self._context_factory = context_factory
        self._context_contract = deepcopy(dict(context_contract or {
            "history_source": "CALLER_SUPPLIED_CONTEXT_FACTORY",
            "policy_observable_only": "NOT_ASSERTED_BY_GENERIC_ADAPTER",
            "future_suffix_used": "NOT_ASSERTED_BY_GENERIC_ADAPTER",
        }))
        self._invocation_count = 0
        self._build_match_receipts: list[BuildMatchReceiptV1] = []
        self._last_result: OfflineActionGuideResultV1 | None = None

    @property
    def last_result(self) -> OfflineActionGuideResultV1 | None:
        return self._last_result

    @property
    def build_match_receipts(self) -> tuple[BuildMatchReceiptV1, ...]:
        return tuple(self._build_match_receipts)

    def action_priorities(
        self,
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> Mapping[ActionRef, float]:
        context = self._context_factory(outcome, prefix)
        if not isinstance(context, OfflineGuideContextV1):
            raise WaveActionGuideV1Error(
                "context_factory must return OfflineGuideContextV1"
            )
        result = self._offline_guide.rank_available_actions(
            context,
            outcome.available_actions,
        )
        self._invocation_count += 1
        self._last_result = result
        receipt = result.build_match_receipt
        if receipt not in self._build_match_receipts:
            self._build_match_receipts.append(receipt)
        return _complete_legal_priorities(
            outcome.available_actions,
            {
                proposal.action_ref: proposal.probability
                for proposal in result.proposals
            },
        )

    def audit_snapshot(self) -> OfflineSearchGuideAuditV1:
        return OfflineSearchGuideAuditV1(
            guide_id=self.guide_id,
            invocation_count=self._invocation_count,
            build_match_receipts=self.build_match_receipts,
            source_build_scope=self._offline_guide.source_build_scope,
            backend=self._offline_guide.backend,
            model_path=str(self._offline_guide.model_path),
            context_contract=self._context_contract,
        )


def action_guide_audit_payload_v1(guides: Sequence[Any]) -> tuple[JSONMap, ...]:
    """Collect opt-in guide audit payloads for storage beside a search result."""

    payloads: list[JSONMap] = []
    for guide in guides:
        audit_snapshot = getattr(guide, "audit_snapshot", None)
        if callable(audit_snapshot):
            snapshot = audit_snapshot()
            to_dict = getattr(snapshot, "to_dict", None)
            if not callable(to_dict):
                raise TypeError("guide audit snapshot must expose to_dict()")
            payload = to_dict()
            if not isinstance(payload, dict):
                raise TypeError("guide audit snapshot to_dict() must return dict")
            payloads.append(payload)
    return tuple(payloads)


__all__ = (
    "CallableActionGuideV1",
    "OfflineActionSequenceSearchGuideV1",
    "OfflineGuideContextFactoryV1",
    "OfflineSearchGuideAuditV1",
    "PriorityFactoryV1",
    "StaticActionGuideV1",
    "WaveActionGuideV1Error",
    "action_guide_audit_payload_v1",
    "complete_legal_action_priorities_v1",
)
