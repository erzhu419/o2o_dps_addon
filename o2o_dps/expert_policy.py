"""Shared contract for source-derived and runtime-observed expert proposals.

The deployed addons do not expose a pure function over simulator state.  This
contract therefore keeps *how* a proposal was obtained separate from whether
it is a deployed expert or a research candidate.  In particular,
``SOURCE_DERIVED`` never means that the original Lua was executed.

One decision keeps the action lanes independent.  A next-swing cancellation is
an operation in the mutually-exclusive swing-queue lane; it is not a second
action that can be emitted beside Heroic Strike or Cleave.  ``raw_sink_order``
preserves the source call order which the normalized lanes intentionally lose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


WAIT_ACTION = "WAIT"


class ProvenanceKind(str, Enum):
    """How the proposal was obtained."""

    SOURCE_DERIVED = "SOURCE_DERIVED"
    LIVE_BLACKBOX = "LIVE_BLACKBOX"
    EXACT_RUNTIME = "EXACT_RUNTIME"


class ExpertRole(str, Enum):
    """Whether the source is an installed expert, a candidate, or unavailable."""

    DEPLOYED = "DEPLOYED"
    CANDIDATE = "CANDIDATE"
    UNAVAILABLE = "UNAVAILABLE"


class SwingQueueOp(str, Enum):
    KEEP = "KEEP"
    HEROIC_STRIKE = "HEROIC_STRIKE"
    CLEAVE = "CLEAVE"
    CANCEL = "CANCEL"


class StanceOp(str, Enum):
    KEEP = "KEEP"
    BATTLE = "BATTLE"
    DEFENSIVE = "DEFENSIVE"
    BERSERKER = "BERSERKER"


class TargetOp(str, Enum):
    KEEP = "KEEP"
    AUTO_SWITCH = "AUTO_SWITCH"
    NEAREST_ENEMY = "NEAREST_ENEMY"


class CastControl(str, Enum):
    KEEP = "KEEP"
    STOP_CAST = "STOP_CAST"


@dataclass(frozen=True)
class RawSink:
    """One expert action sink in the order emitted by the source policy."""

    channel: str
    operation: str
    value: str | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.operation.strip():
            raise ValueError("raw sink channel and operation must be nonempty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "operation": self.operation,
            "value": self.value,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class ExpertProvenance:
    """Identity and evidence boundary for one adapter result."""

    expert_id: str
    kind: ProvenanceKind
    role: ExpertRole
    authority_files: tuple[str, ...]
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.expert_id.strip():
            raise ValueError("expert_id must be nonempty")
        if not self.authority_files:
            raise ValueError("authority_files must identify the policy authority")

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "authority_files": list(self.authority_files),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class ExpertDecision:
    """One factorized expert proposal.

    ``gcd`` is either a canonical action key or ``WAIT``.  A wait carries a
    positive duration; a spell does not.  ``off_gcd`` and ``raw_sink_order``
    are tuples because their order is semantically meaningful.
    """

    provenance: ExpertProvenance
    valid: bool
    gcd: str = WAIT_ACTION
    wait_ms: int | None = 100
    swing_queue: SwingQueueOp = SwingQueueOp.KEEP
    off_gcd: tuple[str, ...] = ()
    stance: StanceOp = StanceOp.KEEP
    target: TargetOp = TargetOp.KEEP
    cast_control: CastControl = CastControl.KEEP
    raw_sink_order: tuple[RawSink, ...] = ()
    eligible_for_independent_vote: bool = False
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.gcd == WAIT_ACTION:
            if (
                isinstance(self.wait_ms, bool)
                or not isinstance(self.wait_ms, int)
                or self.wait_ms <= 0
            ):
                raise ValueError("WAIT requires a positive wait_ms")
        elif not self.gcd.strip() or self.wait_ms is not None:
            raise ValueError("a GCD spell must be nonempty and have wait_ms=None")
        if not self.valid and self.eligible_for_independent_vote:
            raise ValueError("an invalid proposal cannot vote")
        if (
            self.provenance.role is not ExpertRole.DEPLOYED
            and self.eligible_for_independent_vote
        ):
            raise ValueError("only a deployed expert may be an independent vote")
        object.__setattr__(self, "off_gcd", tuple(self.off_gcd))
        object.__setattr__(self, "raw_sink_order", tuple(self.raw_sink_order))

    @property
    def expert_id(self) -> str:
        return self.provenance.expert_id

    @property
    def is_wait(self) -> bool:
        return self.gcd == WAIT_ACTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "valid": self.valid,
            "gcd": {
                "action": self.gcd,
                "wait_ms": self.wait_ms,
            },
            "swing_queue": self.swing_queue.value,
            "off_gcd": list(self.off_gcd),
            "stance": self.stance.value,
            "target": self.target.value,
            "cast_control": self.cast_control.value,
            "raw_sink_order": [sink.to_dict() for sink in self.raw_sink_order],
            "eligible_for_independent_vote": self.eligible_for_independent_vote,
            "reason": self.reason,
            "metadata": dict(self.metadata),
            "provenance": self.provenance.to_dict(),
        }


def invalid_decision(
    provenance: ExpertProvenance,
    reason: str,
    *,
    wait_ms: int = 100,
    metadata: Mapping[str, Any] | None = None,
) -> ExpertDecision:
    """Return an explicit non-voting result instead of inventing a proposal."""

    return ExpertDecision(
        provenance=provenance,
        valid=False,
        wait_ms=wait_ms,
        eligible_for_independent_vote=False,
        reason=reason,
        metadata=metadata or {},
    )
