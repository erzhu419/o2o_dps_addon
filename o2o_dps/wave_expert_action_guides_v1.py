"""Cat and Contra proposal adapters for finite wave schedule search.

Each named factory wraps the existing source adapter itself.  State factories
remain explicit because Cat full-policy, runtime-bound deployed Contra, and
Contra260817 consume deliberately different evidence-bearing state types.
Their proposals only rank the simulator's complete current legal action set;
an invalid or partly unmapped expert proposal becomes zero weights and never
removes an exploration candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from .contra260817_fury_full_policy_v3 import (
    Contra260817FuryFullPolicyAdapterV3,
)
from .contra260817_fury_ordered_sink_executor_v4 import (
    _ACTION_KEY_BY_VALUE as _CONTRA260817_ACTION_KEY_BY_VALUE,
    _ACTION_REFS as _CONTRA260817_ACTION_REFS,
)
from .expert_policy import (
    ExpertDecision,
    RawSink,
    StanceOp,
    SwingQueueOp,
    WAIT_ACTION,
)
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .fury_expert_adapters import FuryExpertState
from .fury_expert_guided_search_v1 import fury_state_from_simulator
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    RAID_A_CONTROLLER,
    RuntimeBoundContraDeployedFuryAdapterV7,
)
from .sim_bridge import ActionRef
from .wave_action_guides_v1 import complete_legal_action_priorities_v1
from .wave_action_schedule_v1 import ScheduledActionPlan
from .wave_action_sequence_search_v1 import ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
ExpertStateFactoryV1 = Callable[
    [ScheduleReplayOutcomeV1, Sequence[ScheduledActionPlan]], Any
]
RawSinkActionResolverV1 = Callable[
    [RawSink, ScheduleReplayOutcomeV1, Sequence[ScheduledActionPlan]],
    ActionRef | None,
]


_STANCE_ACTIONS: Mapping[StanceOp, ActionRef] = {
    StanceOp.BATTLE: ACTION_KEY_TO_REF["warrior.battle_stance"],
    StanceOp.DEFENSIVE: ACTION_KEY_TO_REF["warrior.defensive_stance"],
    StanceOp.BERSERKER: ACTION_KEY_TO_REF["warrior.berserker_stance"],
}
_LOCALIZED_ACTION_KEYS: Mapping[str, str] = {
    **_CONTRA260817_ACTION_KEY_BY_VALUE,
    "英勇打击": "warrior.heroic_strike",
    "顺劈斩": "warrior.cleave",
}
_QUEUE_ACTION_BY_KEY: Mapping[str, ActionRef] = {
    "warrior.heroic_strike": QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
    "warrior.cleave": QUEUE_REFS[SwingQueueOp.CLEAVE],
}
_DEFAULT_ACTION_REFS: Mapping[str, ActionRef] = {
    **_CONTRA260817_ACTION_REFS,
    **_QUEUE_ACTION_BY_KEY,
}
_ACTION_KEY_BY_REF: Mapping[ActionRef, str] = {
    action: key for key, action in _DEFAULT_ACTION_REFS.items()
}


class WaveExpertActionGuideV1Error(ValueError):
    """An expert adapter, state factory, or proposal mapping is invalid."""


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WaveExpertActionGuideV1Error(f"{label} must be nonempty text")
    return value.strip()


def _state_factory(value: ExpertStateFactoryV1) -> ExpertStateFactoryV1:
    if not callable(value):
        raise TypeError("state_factory must be callable")
    return value


def _action_ref_for_key(
    action_key: str,
    action_ref_by_key: Mapping[str, ActionRef],
) -> ActionRef | None:
    action = action_ref_by_key.get(action_key)
    return action if isinstance(action, ActionRef) else None


def _raw_sink_action(
    sink: RawSink,
    action_ref_by_key: Mapping[str, ActionRef],
) -> ActionRef | None:
    if sink.channel == "swing_queue":
        key = _LOCALIZED_ACTION_KEYS.get(str(sink.value), str(sink.value))
        return _QUEUE_ACTION_BY_KEY.get(key)
    if sink.channel == "stance":
        key = _LOCALIZED_ACTION_KEYS.get(str(sink.value), str(sink.value))
        return _action_ref_for_key(key, action_ref_by_key)
    if sink.channel not in {"gcd", "off_gcd"} or sink.value is None:
        return None
    key = _LOCALIZED_ACTION_KEYS.get(sink.value, sink.value)
    return _action_ref_for_key(key, action_ref_by_key)


def expert_decision_action_priorities_v1(
    decision: ExpertDecision,
    *,
    action_ref_by_key: Mapping[str, ActionRef] = _DEFAULT_ACTION_REFS,
    outcome: ScheduleReplayOutcomeV1 | None = None,
    prefix: Sequence[ScheduledActionPlan] = (),
    raw_sink_action_resolver: RawSinkActionResolverV1 | None = None,
) -> dict[ActionRef, float]:
    """Map one valid ordered expert proposal to descending positive weights."""

    if not isinstance(decision, ExpertDecision):
        raise TypeError("expert adapter must return ExpertDecision")
    if not decision.valid:
        return {}
    if not isinstance(action_ref_by_key, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, ActionRef)
        for key, value in action_ref_by_key.items()
    ):
        raise TypeError("action_ref_by_key must map text keys to ActionRef")
    if raw_sink_action_resolver is not None and not callable(
        raw_sink_action_resolver
    ):
        raise TypeError("raw_sink_action_resolver must be callable or None")

    ordered: list[ActionRef] = []

    def add(action: ActionRef | None) -> None:
        if action is not None and action not in ordered:
            ordered.append(action)

    for sink in decision.raw_sink_order:
        action = _raw_sink_action(sink, action_ref_by_key)
        if (
            action is None
            and raw_sink_action_resolver is not None
            and outcome is not None
        ):
            action = raw_sink_action_resolver(sink, outcome, prefix)
            if action is not None and not isinstance(action, ActionRef):
                raise TypeError("raw_sink_action_resolver must return ActionRef or None")
        add(action)

    # Retain normalized lanes even when a source translator has no raw ledger
    # for one of them.  Existing raw order remains authoritative where present.
    for key in decision.off_gcd:
        add(_action_ref_for_key(key, action_ref_by_key))
    if decision.stance is not StanceOp.KEEP:
        add(_STANCE_ACTIONS.get(decision.stance))
    if decision.swing_queue is not SwingQueueOp.KEEP:
        add(QUEUE_REFS.get(decision.swing_queue))
    if decision.gcd != WAIT_ACTION:
        add(_action_ref_for_key(decision.gcd, action_ref_by_key))

    count = len(ordered)
    return {
        action: float(count - index)
        for index, action in enumerate(ordered)
    }


@dataclass(frozen=True)
class ExpertSearchGuideAuditV1:
    guide_id: str
    invocation_count: int
    valid_proposal_count: int
    invalid_proposal_count: int
    observed_expert_ids: tuple[str, ...]
    precombat_skipped_count: int = 0

    def to_dict(self) -> JSONMap:
        return {
            "schema": "expert_sequence_search_guide_audit/v1",
            "guide_id": self.guide_id,
            "invocation_count": self.invocation_count,
            "valid_proposal_count": self.valid_proposal_count,
            "invalid_proposal_count": self.invalid_proposal_count,
            "precombat_skipped_count": self.precombat_skipped_count,
            "observed_expert_ids": list(self.observed_expert_ids),
            "contract": {
                "expert_is_proposal_order_only": True,
                "invalid_expert_proposal_filters_actions": False,
                "unmapped_expert_action_filters_actions": False,
                "all_legal_simulator_actions_receive_a_weight": True,
            },
        }


class ExpertPolicyWaveActionGuideV1:
    """Wrap one existing ``propose(state)`` adapter as ``ActionGuideV1``."""

    def __init__(
        self,
        guide_id: str,
        expert_adapter: Any,
        state_factory: ExpertStateFactoryV1,
        *,
        action_ref_by_key: Mapping[str, ActionRef] = _DEFAULT_ACTION_REFS,
        raw_sink_action_resolver: RawSinkActionResolverV1 | None = None,
    ) -> None:
        self.guide_id = _nonempty(guide_id, "guide_id")
        if not callable(getattr(expert_adapter, "propose", None)):
            raise TypeError("expert_adapter must expose propose(state)")
        self._expert_adapter = expert_adapter
        self._state_factory = _state_factory(state_factory)
        self._action_ref_by_key = dict(action_ref_by_key)
        self._raw_sink_action_resolver = raw_sink_action_resolver
        self._invocation_count = 0
        self._valid_proposal_count = 0
        self._invalid_proposal_count = 0
        self._precombat_skipped_count = 0
        self._observed_expert_ids: list[str] = []
        self._last_decision: ExpertDecision | None = None

    @property
    def last_decision(self) -> ExpertDecision | None:
        return self._last_decision

    def action_priorities(
        self,
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> Mapping[ActionRef, float]:
        precombat = outcome.state.get("precombat")
        if (
            isinstance(precombat, Mapping)
            and precombat.get("active") is True
            and outcome.state.get("num_targets") == 0
        ):
            # Cat/Contra combat state projectors require a selected hostile
            # target and have no authoritative pre-pull controller.  Skipping
            # them here is explicit; a separate burst/offline guide may rank
            # the complete legal self-action set without granting authority.
            self._invocation_count += 1
            self._precombat_skipped_count += 1
            return complete_legal_action_priorities_v1(
                outcome.available_actions,
                {},
            )
        state = self._state_factory(outcome, prefix)
        decision = self._expert_adapter.propose(state)
        if not isinstance(decision, ExpertDecision):
            raise TypeError("expert adapter must return ExpertDecision")
        self._last_decision = decision
        self._invocation_count += 1
        if decision.valid:
            self._valid_proposal_count += 1
        else:
            self._invalid_proposal_count += 1
        if decision.expert_id not in self._observed_expert_ids:
            self._observed_expert_ids.append(decision.expert_id)
        proposed = expert_decision_action_priorities_v1(
            decision,
            action_ref_by_key=self._action_ref_by_key,
            outcome=outcome,
            prefix=prefix,
            raw_sink_action_resolver=self._raw_sink_action_resolver,
        )
        return complete_legal_action_priorities_v1(
            outcome.available_actions,
            proposed,
        )

    def audit_snapshot(self) -> ExpertSearchGuideAuditV1:
        return ExpertSearchGuideAuditV1(
            guide_id=self.guide_id,
            invocation_count=self._invocation_count,
            valid_proposal_count=self._valid_proposal_count,
            invalid_proposal_count=self._invalid_proposal_count,
            observed_expert_ids=tuple(self._observed_expert_ids),
            precombat_skipped_count=self._precombat_skipped_count,
        )


def make_fury_state_from_simulator_factory_v1(
    raid_sim_request: Mapping[str, Any],
) -> ExpertStateFactoryV1:
    """Build the shared bounded Fury state directly from a replay outcome."""

    if not isinstance(raid_sim_request, Mapping):
        raise TypeError("raid_sim_request must be a mapping")

    def factory(
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> FuryExpertState:
        state = fury_state_from_simulator(
            outcome.state,
            outcome.available_actions,
            raid_sim_request,
        )
        last_key = _last_gcd_name(prefix, _ACTION_KEY_BY_REF)
        queue = outcome.state.get("swing_queue")
        queued = state.queued_swing
        if isinstance(queue, Mapping) and queue.get("status") in {"PENDING", "ACTIVE"}:
            kind = queue.get("kind")
            if kind in {"HEROIC_STRIKE", "CLEAVE"}:
                queued = SwingQueueOp(kind)
        return replace(state, last_cast_name=last_key, queued_swing=queued)

    return factory


def _last_gcd_name(
    prefix: Sequence[ScheduledActionPlan],
    name_by_action: Mapping[ActionRef, str],
) -> str:
    for plan in reversed(prefix):
        if plan.gcd_action is not None:
            return name_by_action.get(plan.gcd_action, "")
    return ""


def make_contextual_fury_state_factory_v1(
    raid_sim_request: Mapping[str, Any],
    target_contexts: Mapping[int, Any],
    *,
    last_gcd_name_by_action: Mapping[ActionRef, str] = _ACTION_KEY_BY_REF,
) -> ExpertStateFactoryV1:
    """Use the existing live target-context resolver and Fury state projector."""

    from . import fury_full_policy_rollout_v2 as rollout_v2
    from . import fury_full_policy_rollout_v3 as rollout_v3

    contexts = rollout_v3._validate_context_map_v3(target_contexts)

    def factory(
        outcome: ScheduleReplayOutcomeV1,
        prefix: Sequence[ScheduledActionPlan],
    ) -> FuryExpertState:
        target = rollout_v3._resolve_target_semantics_v3(
            outcome.state,
            raid_sim_request,
            contexts,
        )
        return rollout_v2._combat_state(
            outcome.state,
            outcome.available_actions,
            raid_sim_request,
            target,
            last_gcd_action=_last_gcd_name(prefix, last_gcd_name_by_action),
        )

    return factory


def cat_wave_action_guide_v1(
    state_factory: ExpertStateFactoryV1,
    *,
    adapter: Any | None = None,
    raw_sink_action_resolver: RawSinkActionResolverV1 | None = None,
) -> ExpertPolicyWaveActionGuideV1:
    """Wrap the Cat profile-1 full-policy source adapter."""

    resolved = CatFuryFullPolicyAdapterV4() if adapter is None else adapter
    return ExpertPolicyWaveActionGuideV1(
        f"cat:{getattr(resolved, 'expert_id', 'unknown')}",
        resolved,
        state_factory,
        action_ref_by_key=_DEFAULT_ACTION_REFS,
        raw_sink_action_resolver=raw_sink_action_resolver,
    )


def deployed_contra_wave_action_guide_v1(
    state_factory: ExpertStateFactoryV1,
    *,
    runtime_binding: Mapping[str, Any] | None = None,
    adapter: Any | None = None,
    controller: str = RAID_A_CONTROLLER,
    raw_sink_action_resolver: RawSinkActionResolverV1 | None = None,
) -> ExpertPolicyWaveActionGuideV1:
    """Wrap the runtime-bound deployed Contra adapter, never a stand-in."""

    if adapter is None:
        if runtime_binding is None:
            raise WaveExpertActionGuideV1Error(
                "deployed Contra requires runtime_binding or an explicit adapter"
            )
        adapter = RuntimeBoundContraDeployedFuryAdapterV7(
            runtime_binding,
            controller=controller,
        )
    return ExpertPolicyWaveActionGuideV1(
        f"deployed_contra:{getattr(adapter, 'expert_id', 'unknown')}",
        adapter,
        state_factory,
        action_ref_by_key=_DEFAULT_ACTION_REFS,
        raw_sink_action_resolver=raw_sink_action_resolver,
    )


def contra260817_wave_action_guide_v1(
    state_factory: ExpertStateFactoryV1,
    *,
    adapter: Any | None = None,
    raw_sink_action_resolver: RawSinkActionResolverV1 | None = None,
) -> ExpertPolicyWaveActionGuideV1:
    """Wrap the readable Contra260817 full-policy source adapter."""

    resolved = (
        Contra260817FuryFullPolicyAdapterV3()
        if adapter is None
        else adapter
    )
    return ExpertPolicyWaveActionGuideV1(
        f"contra260817:{getattr(resolved, 'expert_id', 'unknown')}",
        resolved,
        state_factory,
        action_ref_by_key=_DEFAULT_ACTION_REFS,
        raw_sink_action_resolver=raw_sink_action_resolver,
    )


__all__ = (
    "ExpertPolicyWaveActionGuideV1",
    "ExpertSearchGuideAuditV1",
    "ExpertStateFactoryV1",
    "RawSinkActionResolverV1",
    "WaveExpertActionGuideV1Error",
    "cat_wave_action_guide_v1",
    "contra260817_wave_action_guide_v1",
    "deployed_contra_wave_action_guide_v1",
    "expert_decision_action_priorities_v1",
    "make_contextual_fury_state_factory_v1",
    "make_fury_state_from_simulator_factory_v1",
)
