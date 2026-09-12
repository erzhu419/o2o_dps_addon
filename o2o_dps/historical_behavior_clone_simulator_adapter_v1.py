"""Typed simulator surface for the pooled-clean-Fury behavior clone.

This version is additive: it does not alter the frozen External-V2 historical
runner or the shared Cat/Contra ordered executor.  It translates each sampled
V1 behavior-clone mark into one existing simulator action lane, records bridge
acceptance separately, and feeds that acceptance back to the semi-Markov
clock.  A small epoch runner permits zero-delay off-GCD/stance/queue chains and
stops only after a GCD action or an explicit model WAIT consumes the epoch.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from . import historical_behavior_clone_v1 as clone_v1
from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    RawSink,
    StanceOp,
    SwingQueueOp,
    WAIT_ACTION,
)
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .sim_bridge import ActionRef, ActResult, AvailableAction


POLICY_ID = "chronicle.pooled_clean_fury.behavior_clone_v1"
ADAPTER_SCHEMA = "historical_behavior_clone_simulator_adapter/v1"
EXECUTION_SCHEMA = "historical_behavior_clone_typed_sink_execution/v1"
EPOCH_SCHEMA = "historical_behavior_clone_decision_epoch/v1"
SOURCE_REF = "historical_behavior_clone_v1.mark_head"

STANCE_BY_ACTION = {
    "warrior.battle_stance": StanceOp.BATTLE,
    "warrior.defensive_stance": StanceOp.DEFENSIVE,
    "warrior.berserker_stance": StanceOp.BERSERKER,
}
QUEUE_BY_ACTION = {
    "warrior.heroic_strike": SwingQueueOp.HEROIC_STRIKE,
    "warrior.cleave": SwingQueueOp.CLEAVE,
}


class HistoricalBehaviorCloneSimulatorAdapterV1Error(RuntimeError):
    """The typed adapter, bridge, or pending-proposal contract was violated."""


class BehaviorCloneBridgeV1(Protocol):
    def actions(self) -> list[AvailableAction]: ...

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult: ...

    def wait(self, wait_ms: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ActionSinkBindingV1:
    action_key: str
    policy_lane: str
    sink_channel: str
    sink_operation: str
    action_ref: ActionRef
    stance: StanceOp = StanceOp.KEEP
    queue: SwingQueueOp = SwingQueueOp.KEEP

    @property
    def triggers_gcd(self) -> bool:
        return self.policy_lane == "gcd"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "policy_lane": self.policy_lane,
            "sink_channel": self.sink_channel,
            "sink_operation": self.sink_operation,
            "action_ref": self.action_ref.to_wire(),
            "triggers_gcd": self.triggers_gcd,
            "stance": self.stance.value,
            "swing_queue": self.queue.value,
            "mapping_status": "MAPPED",
            "target_sink_status": (
                "NOT_APPLICABLE_ACTION_REF_HAS_NO_TARGET_ARGUMENT"
            ),
        }


def _binding(action_key: str) -> ActionSinkBindingV1:
    spec = clone_v1.ACTION_SPEC_BY_KEY[action_key]
    if spec.lane == "queue":
        queue = QUEUE_BY_ACTION[action_key]
        return ActionSinkBindingV1(
            action_key=action_key,
            policy_lane=spec.lane,
            sink_channel="swing_queue",
            sink_operation="BehaviorClone.NextSwingQueueV1",
            action_ref=QUEUE_REFS[queue],
            queue=queue,
        )
    if spec.lane == "off_gcd" and action_key in STANCE_BY_ACTION:
        return ActionSinkBindingV1(
            action_key=action_key,
            policy_lane=spec.lane,
            sink_channel="stance",
            sink_operation="BehaviorClone.StanceV1",
            action_ref=ACTION_KEY_TO_REF[action_key],
            stance=STANCE_BY_ACTION[action_key],
        )
    if spec.lane == "off_gcd":
        return ActionSinkBindingV1(
            action_key=action_key,
            policy_lane=spec.lane,
            sink_channel="off_gcd",
            sink_operation="BehaviorClone.OffGCDV1",
            action_ref=ACTION_KEY_TO_REF[action_key],
        )
    if spec.lane == "gcd":
        return ActionSinkBindingV1(
            action_key=action_key,
            policy_lane=spec.lane,
            sink_channel="gcd",
            sink_operation="BehaviorClone.GCDV1",
            action_ref=ACTION_KEY_TO_REF[action_key],
        )
    raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
        f"action {action_key} has no typed sink mapping"
    )


ACTION_SINK_BINDINGS_V1 = {
    action_key: _binding(action_key) for action_key in clone_v1.ACTION_KEYS
}
if set(ACTION_SINK_BINDINGS_V1) != set(clone_v1.ACTION_KEYS):
    raise RuntimeError("behavior-clone sink coverage does not close to 15 actions")


@dataclass(frozen=True)
class BehaviorCloneFrameV1:
    state: Mapping[str, Any]
    observation: Mapping[str, Any]
    available_actions: tuple[AvailableAction, ...]

    def __post_init__(self) -> None:
        time_ms = self.state.get("time_ms")
        if isinstance(time_ms, bool) or not isinstance(time_ms, int) or time_ms < 0:
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "frame state requires nonnegative time_ms"
            )
        if self.observation.get("schema") != clone_v1.OBSERVATION_SCHEMA:
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "frame has unsupported behavior-clone observation"
            )
        if self.observation.get("wave_elapsed_ms") != time_ms:
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "observation time differs from simulator state"
            )
        if any(not isinstance(row, AvailableAction) for row in self.available_actions):
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "available_actions must contain AvailableAction values"
            )

    @property
    def now_ms(self) -> int:
        return int(self.state["time_ms"])


def action_sink_coverage_v1() -> dict[str, Any]:
    rows = [ACTION_SINK_BINDINGS_V1[key].to_dict() for key in clone_v1.ACTION_KEYS]
    return {
        "schema": "historical_behavior_clone_action_sink_coverage/v1",
        "action_count": len(rows),
        "ontology_action_count": len(clone_v1.ACTION_KEYS),
        "all_actions_mapped_or_not_applicable": all(
            row["mapping_status"] in {"MAPPED", "NOT_APPLICABLE"} for row in rows
        ),
        "rows": rows,
    }


def _available_by_ref(
    values: Iterable[AvailableAction],
) -> dict[ActionRef, AvailableAction]:
    result: dict[ActionRef, AvailableAction] = {}
    for row in values:
        if not isinstance(row, AvailableAction):
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "bridge actions must be AvailableAction values"
            )
        if row.action in result:
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "bridge returned a duplicate action identity"
            )
        result[row.action] = row
    return result


def legal_action_mask_v1(
    available_actions: Iterable[AvailableAction],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return exact legal ontology keys plus one status for every action."""

    available = _available_by_ref(available_actions)
    legal: list[str] = []
    status: dict[str, str] = {}
    for action_key in clone_v1.ACTION_KEYS:
        binding = ACTION_SINK_BINDINGS_V1[action_key]
        row = available.get(binding.action_ref)
        if row is None:
            status[action_key] = "NOT_APPLICABLE_ABSENT_FROM_SPELLBOOK"
            continue
        if row.triggers_gcd is not binding.triggers_gcd:
            status[action_key] = "INVALID_TRIGGER_LANE_MISMATCH"
            continue
        if not row.legal:
            status[action_key] = "NOT_APPLICABLE_CURRENTLY_ILLEGAL"
            continue
        legal.append(action_key)
        status[action_key] = "LEGAL"
    return tuple(legal), status


class HistoricalBehaviorCloneSimulatorAdapterV1:
    """Convert clocked clone proposals into factorized typed sink decisions."""

    expert_id = POLICY_ID

    def __init__(self, model: Mapping[str, Any], *, simulator_seed: int) -> None:
        clone_v1.validate_model_v1(model)
        self.model = deepcopy(dict(model))
        self.runtime = clone_v1.HistoricalBehaviorCloneRuntimeV1(
            self.model, seed=simulator_seed
        )
        self._pending_decision: ExpertDecision | None = None

    @property
    def provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=POLICY_ID,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(
                "o2o_dps/historical_behavior_clone_v1.py",
                "pooled-clean-Fury model artifact",
            ),
            source_refs=(clone_v1.MODEL_SCHEMA, ADAPTER_SCHEMA),
        )

    def propose(self, frame: BehaviorCloneFrameV1) -> ExpertDecision:
        if not isinstance(frame, BehaviorCloneFrameV1):
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "adapter requires BehaviorCloneFrameV1"
            )
        legal, legal_status = legal_action_mask_v1(frame.available_actions)
        proposal = self.runtime.propose(
            now_ms=frame.now_ms,
            observation=frame.observation,
            legal_actions=legal,
        )
        if proposal["kind"] == "WAIT":
            if self._pending_decision is not None:
                raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                    "runtime returned WAIT while an action proposal is pending"
                )
            return ExpertDecision(
                provenance=self.provenance,
                valid=True,
                gcd=WAIT_ACTION,
                wait_ms=int(proposal["wait_ms"]),
                eligible_for_independent_vote=False,
                reason=str(proposal["reason"]),
                metadata={
                    "schema": ADAPTER_SCHEMA,
                    "runtime_proposal": deepcopy(proposal),
                    "legal_action_status": legal_status,
                    "raw_gcd_calls": [],
                    "pooled_clean_fury": True,
                    "top_player_policy": False,
                },
            )

        proposal_id = int(proposal["proposal_id"])
        if self._pending_decision is not None:
            pending = self._pending_decision.metadata["runtime_proposal"]
            if (
                pending.get("proposal_id") != proposal_id
                or pending.get("action_key") != proposal.get("action_key")
            ):
                raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                    "runtime changed an unsubmitted pending proposal"
                )
            return self._pending_decision

        action_key = str(proposal["action_key"])
        binding = ACTION_SINK_BINDINGS_V1[action_key]
        raw = RawSink(
            binding.sink_channel,
            binding.sink_operation,
            action_key,
            SOURCE_REF,
        )
        kwargs: dict[str, Any] = {
            "gcd": WAIT_ACTION,
            # ExpertDecision requires a positive wait for a non-GCD normalized
            # lane.  The V1 clone executor never submits this placeholder for
            # an ACTION proposal; the next delay comes from the runtime clock.
            "wait_ms": 1,
            "swing_queue": binding.queue,
            "stance": binding.stance,
            "off_gcd": (
                (action_key,) if binding.sink_channel == "off_gcd" else ()
            ),
        }
        if binding.sink_channel == "gcd":
            kwargs["gcd"] = action_key
            kwargs["wait_ms"] = None
        decision = ExpertDecision(
            provenance=self.provenance,
            valid=True,
            raw_sink_order=(raw,),
            eligible_for_independent_vote=False,
            reason="pooled clean Fury behavior-clone mark",
            metadata={
                "schema": ADAPTER_SCHEMA,
                "runtime_proposal": deepcopy(proposal),
                "legal_action_status": legal_status,
                "raw_gcd_calls": (
                    [action_key] if binding.sink_channel == "gcd" else []
                ),
                "normalized_wait_placeholder_submitted": False,
                "target_routing": (
                    "NOT_APPLICABLE_ACTION_REF_HAS_NO_TARGET_ARGUMENT"
                ),
                "pooled_clean_fury": True,
                "top_player_policy": False,
            },
            **kwargs,
        )
        self._pending_decision = decision
        return decision

    def _preflight_pending(self, decision: ExpertDecision) -> Mapping[str, Any]:
        if self._pending_decision is None or decision is not self._pending_decision:
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "decision is not the adapter's current pending proposal"
            )
        proposal = decision.metadata.get("runtime_proposal")
        if not isinstance(proposal, Mapping) or proposal.get("kind") != "ACTION":
            raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
                "pending decision lacks an ACTION runtime proposal"
            )
        return proposal

    def record_action_result(
        self, decision: ExpertDecision, *, accepted: bool, now_ms: int
    ) -> None:
        proposal = self._preflight_pending(decision)
        self.runtime.record_submission(
            proposal_id=int(proposal["proposal_id"]),
            accepted=accepted,
            now_ms=now_ms,
        )
        self._pending_decision = None


def _decision_binding(decision: ExpertDecision) -> ActionSinkBindingV1:
    if decision.expert_id != POLICY_ID or decision.provenance.role is not ExpertRole.CANDIDATE:
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "decision is not a behavior-clone candidate"
        )
    proposal = decision.metadata.get("runtime_proposal")
    if not isinstance(proposal, Mapping) or proposal.get("kind") != "ACTION":
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "decision does not carry an ACTION proposal"
        )
    action_key = proposal.get("action_key")
    if action_key not in ACTION_SINK_BINDINGS_V1:
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "decision action is outside sink coverage"
        )
    binding = ACTION_SINK_BINDINGS_V1[str(action_key)]
    if len(decision.raw_sink_order) != 1:
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "behavior-clone action must have exactly one raw sink"
        )
    sink = decision.raw_sink_order[0]
    if (
        sink.channel != binding.sink_channel
        or sink.operation != binding.sink_operation
        or sink.value != binding.action_key
        or sink.source_ref != SOURCE_REF
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "decision raw sink differs from its typed binding"
        )
    return binding


def execute_historical_behavior_clone_decision_v1(
    adapter: HistoricalBehaviorCloneSimulatorAdapterV1,
    bridge: BehaviorCloneBridgeV1,
    decision: ExpertDecision,
    state: Mapping[str, Any],
    *,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Execute one WAIT or one pending typed action without synthesizing spam."""

    if not isinstance(adapter, HistoricalBehaviorCloneSimulatorAdapterV1):
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "executor requires the matching V1 adapter"
        )
    now_ms = state.get("time_ms")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "executor state requires nonnegative time_ms"
        )
    proposal = decision.metadata.get("runtime_proposal")
    if decision.gcd == WAIT_ACTION and isinstance(proposal, Mapping) and proposal.get("kind") == "WAIT":
        after = bridge.wait(int(decision.wait_ms))
        return {
            "schema": EXECUTION_SCHEMA,
            "kind": "WAIT",
            "proposal": decision.to_dict(),
            "wait_ms": decision.wait_ms,
            "bridge_submission": "SUBMITTED",
            "client_acceptance": "ACCEPTED",
            "decision_consumed": True,
            "final_state": deepcopy(dict(after)),
            "normalized_wait_placeholder_submitted": False,
        }

    binding = _decision_binding(decision)
    # Check pending identity before the first bridge mutation.  Re-executing an
    # already accepted proposal therefore cannot spam the simulator.
    adapter._preflight_pending(decision)
    available = _available_by_ref(bridge.actions())
    row = available.get(binding.action_ref)
    if (
        row is None
        or not row.legal
        or row.triggers_gcd is not binding.triggers_gcd
    ):
        adapter.record_action_result(decision, accepted=False, now_ms=now_ms)
        return {
            "schema": EXECUTION_SCHEMA,
            "kind": "ACTION",
            "proposal": decision.to_dict(),
            "typed_sink_binding": binding.to_dict(),
            "bridge_submission": "NOT_SUBMITTED_NO_LONGER_LEGAL",
            "client_acceptance": "REJECTED_PRE_SUBMISSION",
            "decision_consumed": False,
            "final_state": deepcopy(dict(state)),
            "normalized_wait_placeholder_submitted": False,
        }
    result = (
        bridge.act(binding.action_ref)
        if attempt_id is None
        else bridge.act(binding.action_ref, attempt_id=attempt_id)
    )
    if not isinstance(result, ActResult):
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "bridge.act must return ActResult"
        )
    adapter.record_action_result(
        decision, accepted=result.casted, now_ms=now_ms
    )
    expected_consumption = binding.triggers_gcd and result.casted
    return {
        "schema": EXECUTION_SCHEMA,
        "kind": "ACTION",
        "proposal": decision.to_dict(),
        "typed_sink_binding": binding.to_dict(),
        "bridge_submission": "SUBMITTED",
        "client_acceptance": "ACCEPTED" if result.casted else "REJECTED",
        "decision_consumed": result.consumes_decision,
        "consumption_matches_lane": result.consumes_decision is expected_consumption,
        "final_state": deepcopy(dict(result.state)),
        "normalized_wait_placeholder_submitted": False,
        "attempt_id": attempt_id,
    }


ObservationFactoryV1 = Callable[
    [Mapping[str, Any], tuple[AvailableAction, ...], HistoricalBehaviorCloneSimulatorAdapterV1],
    Mapping[str, Any],
]


def run_historical_behavior_clone_epoch_v1(
    adapter: HistoricalBehaviorCloneSimulatorAdapterV1,
    bridge: BehaviorCloneBridgeV1,
    initial_state: Mapping[str, Any],
    observation_factory: ObservationFactoryV1,
    *,
    max_substeps: int = 32,
    attempt_id_prefix: str | None = None,
) -> dict[str, Any]:
    """Run zero-delay non-GCD marks until a GCD or explicit WAIT consumes input."""

    if isinstance(max_substeps, bool) or not isinstance(max_substeps, int) or max_substeps <= 0:
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "max_substeps must be positive"
        )
    state = deepcopy(dict(initial_state))
    steps: list[dict[str, Any]] = []
    if attempt_id_prefix is not None and (
        not isinstance(attempt_id_prefix, str) or not attempt_id_prefix.strip()
    ):
        raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
            "attempt_id_prefix must be nonempty or None"
        )
    for substep_index in range(max_substeps):
        available = tuple(bridge.actions())
        observation = observation_factory(state, available, adapter)
        frame = BehaviorCloneFrameV1(
            state=state,
            observation=observation,
            available_actions=available,
        )
        decision = adapter.propose(frame)
        binding = (
            None
            if decision.metadata.get("runtime_proposal", {}).get("kind") != "ACTION"
            else _decision_binding(decision)
        )
        attempt_id = (
            f"{attempt_id_prefix}:substep-{substep_index}"
            if attempt_id_prefix is not None
            and binding is not None
            and binding.triggers_gcd
            else None
        )
        execution = execute_historical_behavior_clone_decision_v1(
            adapter, bridge, decision, state, attempt_id=attempt_id
        )
        steps.append(execution)
        state = deepcopy(dict(execution["final_state"]))
        if execution["decision_consumed"] is True:
            return {
                "schema": EPOCH_SCHEMA,
                "status": "COMPLETE",
                "substep_count": len(steps),
                "steps": steps,
                "final_state": state,
                "comparison_ready": False,
                "voting_eligible": False,
            }
    raise HistoricalBehaviorCloneSimulatorAdapterV1Error(
        "behavior-clone epoch exceeded max_substeps without consuming input"
    )


__all__ = [
    "ACTION_SINK_BINDINGS_V1",
    "ADAPTER_SCHEMA",
    "ActionSinkBindingV1",
    "BehaviorCloneFrameV1",
    "EPOCH_SCHEMA",
    "EXECUTION_SCHEMA",
    "HistoricalBehaviorCloneSimulatorAdapterV1",
    "HistoricalBehaviorCloneSimulatorAdapterV1Error",
    "POLICY_ID",
    "action_sink_coverage_v1",
    "execute_historical_behavior_clone_decision_v1",
    "legal_action_mask_v1",
    "run_historical_behavior_clone_epoch_v1",
]
