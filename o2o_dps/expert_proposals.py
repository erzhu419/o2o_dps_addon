"""Project unified Fury expert outputs into the simulator's current lanes.

The present interactive bridge can replay a GCD/wait and one next-swing queue
operation.  Other expert lanes remain in the projection report instead of
being silently treated as executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .beam_search import FactorizedDecision
from .expert_policy import (
    CastControl,
    ExpertDecision,
    StanceOp,
    SwingQueueOp,
    TargetOp,
    WAIT_ACTION,
)
from .sim_bridge import ActionRef, AvailableAction


ACTION_KEY_TO_REF: Mapping[str, ActionRef] = {
    "warrior.battle_shout": ActionRef(spell_id=25289),
    "warrior.battle_stance": ActionRef(spell_id=2457),
    "warrior.berserker_stance": ActionRef(spell_id=2458),
    "warrior.bloodrage": ActionRef(spell_id=2687),
    "warrior.bloodthirst": ActionRef(spell_id=23894),
    "warrior.death_wish": ActionRef(spell_id=12328),
    "warrior.defensive_stance": ActionRef(spell_id=71),
    "warrior.execute": ActionRef(spell_id=20662),
    "warrior.hamstring": ActionRef(spell_id=7373),
    "warrior.pummel": ActionRef(spell_id=6552),
    "warrior.slam": ActionRef(spell_id=45961),
    "warrior.sunder_armor": ActionRef(spell_id=11597),
    "warrior.whirlwind": ActionRef(spell_id=1680),
}

QUEUE_REFS: Mapping[SwingQueueOp, ActionRef] = {
    SwingQueueOp.HEROIC_STRIKE: ActionRef(spell_id=25286, tag=1),
    SwingQueueOp.CLEAVE: ActionRef(spell_id=20569, tag=1),
}


@dataclass(frozen=True)
class ProjectedProposal:
    expert_id: str
    accepted: bool
    candidate_id: str | None
    decision: FactorizedDecision | None
    faithful_replay: bool
    omitted_lanes: tuple[str, ...]
    reason: str
    source: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "accepted": self.accepted,
            "candidate_id": self.candidate_id,
            "decision": (
                [command.to_dict() for command in self.decision.commands()]
                if self.decision is not None
                else None
            ),
            "faithful_replay": self.faithful_replay,
            "omitted_lanes": list(self.omitted_lanes),
            "reason": self.reason,
            "source": self.source,
        }


def project_expert_decision(
    decision: ExpertDecision,
    available_actions: Iterable[AvailableAction],
    *,
    queue_active: bool = False,
    current_stance: StanceOp = StanceOp.BERSERKER,
    target_is_usable: bool = True,
    casting: bool = False,
) -> ProjectedProposal:
    """Mask an expert proposal against exact bridge actions.

    Unsupported lanes are reported and make the projection non-faithful, but
    the supported GCD/queue core can still enter a candidate *union*.  It must
    not be reported as an expert rollout or used as an independent vote.
    """

    if not isinstance(queue_active, bool):
        raise TypeError("queue_active must be boolean")
    if not decision.valid:
        return ProjectedProposal(
            expert_id=decision.expert_id,
            accepted=False,
            candidate_id=None,
            decision=None,
            faithful_replay=False,
            omitted_lanes=(),
            reason=decision.reason or "expert returned invalid",
            source=decision.to_dict(),
        )

    legal = {
        available.action: available
        for available in available_actions
        if available.legal
    }
    omitted: list[str] = []

    queue_ref: ActionRef | None = None
    cancel_queue = False
    if decision.swing_queue is SwingQueueOp.CANCEL:
        if not queue_active:
            return _rejected(decision, "queue cancellation requested without an active queue")
        cancel_queue = True
    elif decision.swing_queue is not SwingQueueOp.KEEP:
        queue_ref = QUEUE_REFS[decision.swing_queue]
        available = legal.get(queue_ref)
        if available is None or available.triggers_gcd:
            return _rejected(decision, "requested next-swing action is not legal")

    gcd_ref: ActionRef | None = None
    wait_ms: int | None = None
    if decision.gcd == WAIT_ACTION:
        wait_ms = decision.wait_ms
    else:
        gcd_ref = ACTION_KEY_TO_REF.get(decision.gcd)
        if gcd_ref is None:
            return _rejected(decision, f"unknown canonical GCD action {decision.gcd}")
        available = legal.get(gcd_ref)
        if available is None or not available.triggers_gcd:
            return _rejected(decision, "requested GCD action is not legal")

    if decision.off_gcd:
        omitted.append("off_gcd")
    if decision.stance is not StanceOp.KEEP and decision.stance is not current_stance:
        omitted.append("stance")
    # This bounded projector has no target-selection command.  A usable current
    # target does not make a requested target change faithfully replayed.
    if decision.target is not TargetOp.KEEP:
        omitted.append("target")
    if decision.cast_control is CastControl.STOP_CAST and casting:
        omitted.append("cast_control")

    projected = FactorizedDecision(
        queue=queue_ref,
        gcd=gcd_ref,
        wait_ms=wait_ms,
        cancel_queue=cancel_queue,
    )
    candidate_id = _candidate_id(decision, projected)
    return ProjectedProposal(
        expert_id=decision.expert_id,
        accepted=True,
        candidate_id=candidate_id,
        decision=projected,
        faithful_replay=not omitted,
        omitted_lanes=tuple(omitted),
        reason=(
            "supported lanes projected exactly"
            if not omitted
            else "GCD/queue candidate retained; unsupported lanes were not replayed"
        ),
        source=decision.to_dict(),
    )


def _candidate_id(
    source: ExpertDecision, projected: FactorizedDecision
) -> str:
    if projected.cancel_queue:
        queue = "cancel"
    elif projected.queue is None:
        queue = "keep"
    else:
        queue = str(projected.queue.spell_id)
    gcd = source.gcd if projected.gcd is not None else f"wait_{projected.wait_ms}ms"
    return f"queue_{queue}__gcd_{gcd}"


def _rejected(decision: ExpertDecision, reason: str) -> ProjectedProposal:
    return ProjectedProposal(
        expert_id=decision.expert_id,
        accepted=False,
        candidate_id=None,
        decision=None,
        faithful_replay=False,
        omitted_lanes=(),
        reason=reason,
        source=decision.to_dict(),
    )


__all__ = (
    "ACTION_KEY_TO_REF",
    "QUEUE_REFS",
    "ProjectedProposal",
    "project_expert_decision",
)
