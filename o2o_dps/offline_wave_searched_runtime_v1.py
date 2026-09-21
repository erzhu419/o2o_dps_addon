"""Causal runtime for independent searched complete-wave programs.

The searched program is intentionally not converted back into an observed
Chronicle policy.  Its steps are proposals with bounded execution windows;
progress is committed only after the responsive bridge reports the matching
accepted action (or the matching explicit wait).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    OptionalOffGcdPrefixV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
)
from .causal_guard_v1 import (
    SKIP_PLAN,
    ObservableCausalGuardV1,
    evaluate_observable_guard_v1,
)
from .offline_wave_policy_v1 import (
    LANE_GCD,
    LANE_OFF_GCD,
    LANE_QUEUE,
    OfflineWaveRuntimeBindingV1,
    _ACTION_REF_BY_KEY,
    _FINITE_RESOURCE_BY_KEY,
)
from .offline_wave_searched_program_v1 import (
    PROGRAM_SCHEMA,
    SearchedWaveGapBehaviorV1,
    SearchedWaveProgramV1,
    SearchedWaveStepKindV1,
    SearchedWaveStepV1,
    SearchedWaveTargetKindV1,
)
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_imported_incumbent_program_v1 import OBSERVATION_CONTRACT_ID_V1
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
RUNTIME_SCHEMA = "offline_wave_searched_runtime/v1"


class SearchedWaveRuntimeV1Error(RuntimeError):
    """The searched controller cannot be executed on the visible wave."""


@dataclass(frozen=True)
class _PendingExecutionV1:
    decision: ProgramDecisionV1
    step_id: str | None
    expected_action: ActionRef | None
    expected_lane: str | None
    resource_actions: tuple[tuple[str, ActionRef], ...]
    kind: str


def _resource_id(action_key: str, action: ActionRef) -> str | None:
    resource = _FINITE_RESOURCE_BY_KEY.get(action_key)
    if resource is not None:
        return resource
    if action.item_id > 0:
        return f"item.{action.item_id}"
    # Every source-derived off-GCD tail entry is a once-per-wave proposal.
    # This also prevents stance tails from oscillating Battle/Berserker on
    # every decision after their explicit source steps were consumed.
    return f"action.{action_key}"


class SearchedWaveProgramSessionV1:
    """One fresh-seed stateful execution of a frozen searched program."""

    def __init__(
        self,
        searched_program: SearchedWaveProgramV1,
        runtime_binding: OfflineWaveRuntimeBindingV1,
        *,
        domain_fallback: (
            Callable[
                [CausalLiveStateProjectionV1, tuple[AvailableAction, ...]],
                ProgramDecisionV1,
            ]
            | None
        ) = None,
    ) -> None:
        if not isinstance(searched_program, SearchedWaveProgramV1):
            raise TypeError("searched_program must be SearchedWaveProgramV1")
        if not isinstance(runtime_binding, OfflineWaveRuntimeBindingV1):
            raise TypeError("runtime_binding must be OfflineWaveRuntimeBindingV1")
        if domain_fallback is not None and not callable(domain_fallback):
            raise TypeError("domain_fallback must be callable or None")
        target_count = len(runtime_binding.target_indexes)
        for step in searched_program.steps:
            if (
                step.kind is SearchedWaveStepKindV1.ACTION
                and step.target.kind is SearchedWaveTargetKindV1.INDEX
                and step.target.index >= target_count
            ):
                raise SearchedWaveRuntimeV1Error(
                    f"step {step.step_id!r} targets source index "
                    f"{step.target.index}, but binding has {target_count} targets"
                )
        unknown_tail = tuple(
            key
            for key in (
                *searched_program.tail_gcd_priority,
                *searched_program.tail_queue_priority,
                *searched_program.tail_off_gcd_once,
            )
            if key not in _ACTION_REF_BY_KEY
        )
        if unknown_tail:
            raise SearchedWaveRuntimeV1Error(
                f"searched tail contains unknown actions: {unknown_tail}"
            )
        self.searched_program = searched_program
        self.runtime_binding = runtime_binding
        self._domain_fallback = domain_fallback
        self._committed: set[str] = set()
        self._skipped: set[str] = set()
        self._used_resources: set[str] = set()
        self._wave_started_at_ms: int | None = None
        self._pending: _PendingExecutionV1 | None = None
        self._audit: list[JSONMap] = []
        self._domain_fallback_calls = 0

    @property
    def committed_step_ids(self) -> tuple[str, ...]:
        return tuple(
            step.step_id
            for step in self.searched_program.steps
            if step.step_id in self._committed
        )

    @property
    def skipped_step_ids(self) -> tuple[str, ...]:
        return tuple(
            step.step_id
            for step in self.searched_program.steps
            if step.step_id in self._skipped
        )

    @property
    def domain_fallback_calls(self) -> int:
        return self._domain_fallback_calls

    @property
    def audit_events(self) -> tuple[JSONMap, ...]:
        return tuple(deepcopy(row) for row in self._audit)

    def _record(
        self,
        kind: str,
        observation: CausalLiveStateProjectionV1,
        **values: Any,
    ) -> None:
        self._audit.append(
            {
                "kind": kind,
                "state_time_ms": observation.visibility_cutoff_ms,
                **values,
            }
        )

    def record_last_executed_decision_v1(
        self, actual_decision: ProgramDecisionV1
    ) -> None:
        self.record_last_execution_receipt_v1(actual_decision, ())

    def record_last_execution_receipt_v1(
        self,
        actual_decision: ProgramDecisionV1,
        execution_receipts: Sequence[Mapping[str, Any]],
    ) -> None:
        if not isinstance(actual_decision, ProgramDecisionV1):
            raise TypeError("actual_decision must be ProgramDecisionV1")
        if not isinstance(execution_receipts, Sequence) or isinstance(
            execution_receipts, (str, bytes, bytearray)
        ):
            raise TypeError("execution_receipts must be a sequence")
        pending = self._pending
        if pending is None:
            raise SearchedWaveRuntimeV1Error(
                "execution feedback has no pending searched-wave proposal"
            )
        self._pending = None
        if actual_decision != pending.decision:
            self._audit.append(
                {
                    "kind": "SEARCHED_PROPOSAL_REPLACED_BEFORE_EXECUTION",
                    "step_id": pending.step_id,
                    "proposal_kind": pending.kind,
                }
            )
            return

        accepted_by_action: dict[ActionRef, list[JSONMap]] = {}
        accepted_actions: list[JSONMap] = []
        terminal_wait = False
        for raw_receipt in execution_receipts:
            if not isinstance(raw_receipt, Mapping):
                raise SearchedWaveRuntimeV1Error(
                    "execution receipt must be an object"
                )
            kind = raw_receipt.get("kind")
            if kind in {
                "TERMINAL_WAIT",
                "TERMINAL_WAIT_EXTERNAL_PRESS_ABSTAIN",
            }:
                terminal_wait = True
                continue
            if kind not in {
                "OPTIONAL_OFF_GCD_EXECUTED",
                "QUEUE_SET",
                "TERMINAL_GCD",
            }:
                continue
            action_wire = raw_receipt.get("action")
            if not isinstance(action_wire, Mapping):
                raise SearchedWaveRuntimeV1Error(
                    f"{kind} receipt lacks an exact action identity"
                )
            action = ActionRef.from_wire(action_wire)
            normalized = {
                "kind": kind,
                "action": action.to_wire(),
                "state_time_ms": raw_receipt.get("state_time_ms"),
                "target_index": actual_decision.target_index,
            }
            accepted_actions.append(normalized)
            accepted_by_action.setdefault(action, []).append(normalized)

        expected_kind = {
            LANE_GCD: "TERMINAL_GCD",
            LANE_QUEUE: "QUEUE_SET",
            LANE_OFF_GCD: "OPTIONAL_OFF_GCD_EXECUTED",
        }.get(pending.expected_lane)
        expected_rows = accepted_by_action.get(pending.expected_action, [])
        expected_executed = (
            pending.expected_action is None
            and terminal_wait
            or pending.expected_action is not None
            and any(row["kind"] == expected_kind for row in expected_rows)
        )
        executed_resources = [
            resource_id
            for resource_id, action in pending.resource_actions
            if action in accepted_by_action
        ]
        self._used_resources.update(executed_resources)
        if not expected_executed:
            self._audit.append(
                {
                    "kind": "SEARCHED_PROPOSAL_ACTION_NOT_EXECUTED",
                    "step_id": pending.step_id,
                    "proposal_kind": pending.kind,
                    "expected_action": (
                        pending.expected_action.to_wire()
                        if pending.expected_action is not None
                        else None
                    ),
                    "expected_lane": pending.expected_lane,
                    "observed_receipt_kinds": [
                        str(row.get("kind")) for row in execution_receipts
                    ],
                    "accepted_actions": accepted_actions,
                }
            )
            return
        if pending.step_id is not None:
            self._committed.add(pending.step_id)
        self._audit.append(
            {
                "kind": "SEARCHED_PROPOSAL_EXECUTION_CONFIRMED",
                "step_id": pending.step_id,
                "proposal_kind": pending.kind,
                "resource_ids": executed_resources,
                "accepted_actions": accepted_actions,
                "accepted_target_index": actual_decision.target_index,
            }
        )

    def reject_last_execution_v1(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("execution rejection reason must be nonempty")
        if self._pending is None:
            return
        pending = self._pending
        self._pending = None
        self._audit.append(
            {
                "kind": "SEARCHED_PROPOSAL_EXECUTION_REJECTED",
                "step_id": pending.step_id,
                "proposal_kind": pending.kind,
                "reason": reason.strip(),
            }
        )

    def _visible_targets(
        self, observation: CausalLiveStateProjectionV1
    ) -> tuple[Mapping[str, Any], ...]:
        semantics = observation.state.get("dynamic_target_semantics")
        rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
        if not isinstance(rows, list):
            raise SearchedWaveRuntimeV1Error(
                "causal observation lacks visible target semantics"
            )
        by_index: dict[int, Mapping[str, Any]] = {}
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise SearchedWaveRuntimeV1Error("visible target must be an object")
            index = raw.get("target_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise SearchedWaveRuntimeV1Error(
                    "visible target lacks a nonnegative target_index"
                )
            if index in by_index:
                raise SearchedWaveRuntimeV1Error(
                    "visible targets repeat target_index"
                )
            by_index[index] = raw
        return tuple(
            by_index[index]
            for index in self.runtime_binding.target_indexes
            if index in by_index
        )

    @staticmethod
    def _active_target_indexes(
        rows: Iterable[Mapping[str, Any]],
    ) -> tuple[int, ...]:
        return tuple(
            int(row["target_index"])
            for row in rows
            if row.get("attackable") is True and row.get("dead") is not True
        )

    def _fallback(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
        reason: str,
    ) -> ProgramDecisionV1:
        self._domain_fallback_calls += 1
        self._record("SEARCHED_DOMAIN_FALLBACK", observation, reason=reason)
        if self._domain_fallback is None:
            raise SearchedWaveRuntimeV1Error(
                f"observation is outside supported wave: {reason}"
            )
        decision = self._domain_fallback(observation, available)
        if not isinstance(decision, ProgramDecisionV1):
            raise TypeError("domain_fallback must return ProgramDecisionV1")
        return decision

    def _current_target(
        self,
        observation: CausalLiveStateProjectionV1,
        active: tuple[int, ...],
    ) -> int:
        current = observation.state.get("target_index")
        if (
            isinstance(current, int)
            and not isinstance(current, bool)
            and current in active
        ):
            return current
        return active[0]

    def _step_target(
        self,
        step: SearchedWaveStepV1,
        observation: CausalLiveStateProjectionV1,
        active: tuple[int, ...],
    ) -> int | None:
        if step.target.kind in {
            SearchedWaveTargetKindV1.CURRENT,
            SearchedWaveTargetKindV1.SELF,
        }:
            return self._current_target(observation, active)
        desired = self.runtime_binding.target_indexes[step.target.index]
        return desired if desired in active else None

    def _wait_decision(
        self,
        *,
        observation: CausalLiveStateProjectionV1,
        target_index: int,
        wait_ms: int,
        reason: str,
        step_id: str | None = None,
        explicit: bool = False,
    ) -> ProgramDecisionV1:
        # Responsive waits stop at simulator/model wake boundaries.  A 500 ms
        # cap preserves bounded re-observation while avoiding five redundant
        # bridge round trips for every quiet half-second of a fixed schedule.
        delay = max(1, min(500 if not explicit else wait_ms, wait_ms))
        decision = ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            wait_ms=delay,
        )
        self._pending = _PendingExecutionV1(
            decision=decision,
            step_id=step_id,
            expected_action=None,
            expected_lane=None,
            resource_actions=(),
            kind="EXPLICIT_WAIT_STEP" if explicit else "CONTROLLER_WAIT",
        )
        self._record(
            "SEARCHED_EXPLICIT_WAIT_SELECTED" if explicit else "SEARCHED_WAIT",
            observation,
            reason=reason,
            wait_ms=delay,
            step_id=step_id,
        )
        return decision

    @staticmethod
    def _decision_for_action(
        action: ActionRef,
        lane: str,
        *,
        target_index: int,
    ) -> ProgramDecisionV1:
        prefix = (
            ProgramPrefixOperationKindV1.SET_TARGET,
            ProgramPrefixOperationKindV1.START_ATTACK,
        )
        if lane == LANE_GCD:
            return ProgramDecisionV1(
                target_index=target_index,
                start_attack=True,
                gcd_action=action,
                prefix_order=(*prefix, ProgramPrefixOperationKindV1.QUEUE_KEEP),
            )
        if lane == LANE_QUEUE:
            return ProgramDecisionV1(
                target_index=target_index,
                start_attack=True,
                queue_op=QueueLaneOp.SET,
                queue_action=action,
                wait_ms=1,
                prefix_order=(*prefix, ProgramPrefixOperationKindV1.QUEUE_SET),
            )
        guard = ObservableCausalGuardV1(
            target_index=target_index,
            target_attackable_is=True,
            action_ready=action,
            false_semantics=SKIP_PLAN,
        )
        return ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            optional_off_gcd_prefixes=(OptionalOffGcdPrefixV1(action, guard),),
            wait_ms=1,
            prefix_order=(
                *prefix,
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD,
                ProgramPrefixOperationKindV1.QUEUE_KEEP,
            ),
        )

    def _tail_decision(
        self,
        *,
        observation: CausalLiveStateProjectionV1,
        available_by_action: Mapping[ActionRef, AvailableAction],
        active: tuple[int, ...],
        reason: str,
    ) -> ProgramDecisionV1:
        target = self._current_target(observation, active)

        def ready_key(keys: Sequence[str], *, once: bool = False) -> str | None:
            for key in keys:
                action = _ACTION_REF_BY_KEY[key]
                resource = _resource_id(key, action)
                if once and resource is not None and resource in self._used_resources:
                    continue
                row = available_by_action.get(action)
                if row is not None and row.legal and row.ready_in_ms == 0:
                    return key
            return None

        gcd_key = ready_key(self.searched_program.tail_gcd_priority)
        off_key = ready_key(
            self.searched_program.tail_off_gcd_once,
            once=True,
        )
        queue_key = ready_key(self.searched_program.tail_queue_priority)
        if gcd_key is None and off_key is None and queue_key is None:
            waits = [
                row.ready_in_ms
                for key in self.searched_program.tail_gcd_priority
                if (row := available_by_action.get(_ACTION_REF_BY_KEY[key]))
                is not None
                and row.ready_in_ms > 0
            ]
            return self._wait_decision(
                observation=observation,
                target_index=target,
                wait_ms=min(waits, default=100),
                reason=f"{reason}:NO_TAIL_ACTION_READY",
            )

        # Re-evaluate after each lane.  Queue reservation or a stance/off-GCD
        # transition can invalidate a GCD which was legal in the pre-prefix
        # action snapshot; composing all three from that stale snapshot caused
        # real D3 replays to fail at the terminal GCD.
        prefixes: tuple[OptionalOffGcdPrefixV1, ...] = ()
        resource_actions: tuple[tuple[str, ActionRef], ...] = ()
        off_ref: ActionRef | None = None
        if off_key is not None:
            off_ref = _ACTION_REF_BY_KEY[off_key]
            prefixes = (
                OptionalOffGcdPrefixV1(
                    off_ref,
                    ObservableCausalGuardV1(
                        target_index=target,
                        target_attackable_is=True,
                        action_ready=off_ref,
                        false_semantics=SKIP_PLAN,
                    ),
                ),
            )
            resource = _resource_id(off_key, off_ref)
            if resource is not None:
                resource_actions = ((resource, off_ref),)
        # Off-GCD once actions run alone, then the next epoch recomputes the
        # legal GCD.  Otherwise prefer a GCD over queueing; a queue runs alone
        # only while no GCD is ready.
        gcd_ref = (
            _ACTION_REF_BY_KEY[gcd_key]
            if off_ref is None and gcd_key is not None
            else None
        )
        queue_ref = (
            _ACTION_REF_BY_KEY[queue_key]
            if off_ref is None and gcd_ref is None and queue_key is not None
            else None
        )
        queue_op = QueueLaneOp.SET if queue_ref is not None else QueueLaneOp.KEEP
        kinds = [
            ProgramPrefixOperationKindV1.SET_TARGET,
            ProgramPrefixOperationKindV1.START_ATTACK,
        ]
        kinds.extend(
            ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD for _ in prefixes
        )
        kinds.append(
            ProgramPrefixOperationKindV1.QUEUE_SET
            if queue_op is QueueLaneOp.SET
            else ProgramPrefixOperationKindV1.QUEUE_KEEP
        )
        decision = ProgramDecisionV1(
            target_index=target,
            start_attack=True,
            optional_off_gcd_prefixes=prefixes,
            queue_op=queue_op,
            queue_action=queue_ref,
            gcd_action=gcd_ref,
            wait_ms=None if gcd_ref is not None else 1,
            prefix_order=tuple(kinds),
        )
        expected_action = off_ref or gcd_ref or queue_ref
        expected_lane = (
            LANE_OFF_GCD
            if off_ref is not None
            else LANE_GCD
            if gcd_ref is not None
            else LANE_QUEUE
            if queue_ref is not None
            else None
        )
        self._pending = _PendingExecutionV1(
            decision=decision,
            step_id=None,
            expected_action=expected_action,
            expected_lane=expected_lane,
            resource_actions=resource_actions,
            kind="SEARCHED_TAIL_FILL",
        )
        self._record(
            "SEARCHED_TAIL_SELECTED",
            observation,
            reason=reason,
            gcd_action=gcd_key if gcd_ref is not None else None,
            queue_action=queue_key if queue_ref is not None else None,
            off_gcd_action=off_key if off_ref is not None else None,
            target_index=target,
        )
        return decision

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("searched program requires CausalLiveStateProjectionV1")
        if not isinstance(available, tuple) or any(
            not isinstance(row, AvailableAction) for row in available
        ):
            raise TypeError("available must contain AvailableAction values")
        if observation.state.get("time_ms") != observation.visibility_cutoff_ms:
            raise SearchedWaveRuntimeV1Error(
                "causal observation time differs from visibility cutoff"
            )
        if self._pending is not None:
            self.reject_last_execution_v1(
                "NEXT_DECISION_WITHOUT_EXECUTION_CONFIRMATION"
            )
        rows = self._visible_targets(observation)
        active = self._active_target_indexes(rows)
        precombat = observation.state.get("precombat")
        if not active:
            if isinstance(precombat, Mapping) and precombat.get("active") is True:
                return self._wait_decision(
                    observation=observation,
                    target_index=self.runtime_binding.target_indexes[0],
                    wait_ms=100,
                    reason="SUPPORTED_WAVE_PRECOMBAT",
                )
            if rows and all(row.get("dead") is True for row in rows):
                return self._wait_decision(
                    observation=observation,
                    target_index=self.runtime_binding.target_indexes[0],
                    wait_ms=1,
                    reason="SUPPORTED_WAVE_COMPLETE",
                )
            return self._fallback(observation, available, "NO_SUPPORTED_ACTIVE_TARGET")

        if self._wave_started_at_ms is None:
            self._wave_started_at_ms = observation.visibility_cutoff_ms
        elapsed = observation.visibility_cutoff_ms - self._wave_started_at_ms
        by_action = {row.action: row for row in available}
        if len(by_action) != len(available):
            raise SearchedWaveRuntimeV1Error(
                "native action surface contains duplicate identities"
            )

        while True:
            remaining = [
                step
                for step in self.searched_program.steps
                if step.step_id not in self._committed
                and step.step_id not in self._skipped
            ]
            if not remaining:
                return self._tail_decision(
                    observation=observation,
                    available_by_action=by_action,
                    active=active,
                    reason="PROGRAM_EXHAUSTED",
                )
            step = remaining[0]
            if elapsed < step.at_or_after_ms:
                if (
                    self.searched_program.gap_behavior
                    is SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
                ):
                    return self._tail_decision(
                        observation=observation,
                        available_by_action=by_action,
                        active=active,
                        reason=f"GAP_BEFORE_STEP:{step.step_id}",
                    )
                return self._wait_decision(
                    observation=observation,
                    target_index=self._current_target(observation, active),
                    wait_ms=step.at_or_after_ms - elapsed,
                    reason="NEXT_SEARCHED_STEP_WINDOW_NOT_REACHED",
                )

            deadline = step.at_or_after_ms + step.max_lateness_ms
            if elapsed > deadline:
                self._skipped.add(step.step_id)
                self._record(
                    "SEARCHED_STEP_WINDOW_MISSED",
                    observation,
                    step_id=step.step_id,
                    runtime_elapsed_ms=elapsed,
                    deadline_ms=deadline,
                )
                continue
            if step.kind is SearchedWaveStepKindV1.WAIT:
                return self._wait_decision(
                    observation=observation,
                    target_index=self._current_target(observation, active),
                    wait_ms=step.wait_ms,
                    reason="EXPLICIT_SEARCHED_WAIT",
                    step_id=step.step_id,
                    explicit=True,
                )

            target = self._step_target(step, observation, active)
            if target is None:
                self._skipped.add(step.step_id)
                self._record(
                    "SEARCHED_STEP_TARGET_NO_LONGER_ACTIVE",
                    observation,
                    step_id=step.step_id,
                    target=step.target.to_dict(),
                )
                continue
            if step.guard is not None:
                evaluation = evaluate_observable_guard_v1(
                    step.guard, observation.state, available
                )
                if not evaluation.satisfied:
                    guard_deadline = min(
                        deadline,
                        step.at_or_after_ms + step.guard.timeout_ms,
                    )
                    if (
                        step.guard.false_semantics == SKIP_PLAN
                        or elapsed >= guard_deadline
                    ):
                        self._skipped.add(step.step_id)
                        self._record(
                            "SEARCHED_STEP_GUARD_SKIPPED",
                            observation,
                            step_id=step.step_id,
                            failed_predicates=list(evaluation.failed_predicates),
                            runtime_elapsed_ms=elapsed,
                            guard_deadline_ms=guard_deadline,
                        )
                        continue
                    return self._wait_decision(
                        observation=observation,
                        target_index=target,
                        wait_ms=min(
                            step.guard.check_interval_ms,
                            guard_deadline - elapsed,
                        ),
                        reason=f"STEP_GUARD_FALSE:{step.step_id}",
                    )

            native = by_action.get(step.action_ref)
            if native is None:
                self._skipped.add(step.step_id)
                self._record(
                    "SEARCHED_STEP_MASKED_BY_EXACT_NATIVE_SURFACE",
                    observation,
                    step_id=step.step_id,
                    action_key=step.action_key,
                )
                continue
            if not native.legal or native.ready_in_ms != 0:
                if elapsed >= deadline:
                    self._skipped.add(step.step_id)
                    self._record(
                        "SEARCHED_STEP_NOT_READY_AT_DEADLINE",
                        observation,
                        step_id=step.step_id,
                        action_key=step.action_key,
                        runtime_elapsed_ms=elapsed,
                        deadline_ms=deadline,
                        native_legal=native.legal,
                        native_ready_in_ms=native.ready_in_ms,
                    )
                    continue
                if (
                    self.searched_program.gap_behavior
                    is SearchedWaveGapBehaviorV1.TAIL_FILL_UNTIL_STEP
                ):
                    return self._tail_decision(
                        observation=observation,
                        available_by_action=by_action,
                        active=active,
                        reason=f"STEP_TRANSIENTLY_NOT_READY:{step.step_id}",
                    )
                return self._wait_decision(
                    observation=observation,
                    target_index=target,
                    wait_ms=min(
                        max(1, native.ready_in_ms or 100),
                        max(1, deadline - elapsed),
                    ),
                    reason=f"STEP_TRANSIENTLY_NOT_READY:{step.step_id}",
                )

            decision = self._decision_for_action(
                step.action_ref,
                step.lane,
                target_index=target,
            )
            resource = _resource_id(step.action_key, step.action_ref)
            self._pending = _PendingExecutionV1(
                decision=decision,
                step_id=step.step_id,
                expected_action=step.action_ref,
                expected_lane=step.lane,
                resource_actions=(
                    ((resource, step.action_ref),) if resource is not None else ()
                ),
                kind="SEARCHED_PROGRAM_STEP",
            )
            self._record(
                "SEARCHED_STEP_SELECTED",
                observation,
                step_id=step.step_id,
                action_key=step.action_key,
                runtime_elapsed_ms=elapsed,
                window_start_ms=step.at_or_after_ms,
                window_deadline_ms=deadline,
                target_index=target,
            )
            return decision


def build_searched_wave_program_runtime_v1(
    searched_program: SearchedWaveProgramV1,
    runtime_binding: OfflineWaveRuntimeBindingV1,
    *,
    domain_fallback_factory: (
        Callable[[], Callable[..., ProgramDecisionV1]] | None
    ) = None,
) -> tuple[CausalActionProgramV1, ImportedReactiveProgramBindingV1]:
    """Bind a frozen searched program as a fresh-session reactive selector."""

    if not isinstance(searched_program, SearchedWaveProgramV1):
        raise TypeError("searched_program must be SearchedWaveProgramV1")
    if not isinstance(runtime_binding, OfflineWaveRuntimeBindingV1):
        raise TypeError("runtime_binding must be OfflineWaveRuntimeBindingV1")
    if domain_fallback_factory is not None and not callable(domain_fallback_factory):
        raise TypeError("domain_fallback_factory must be callable or None")
    binding_id = (
        f"searched-wave::{searched_program.program_id}::"
        f"{runtime_binding.runtime_wave_id}"
    )

    def open_session() -> SearchedWaveProgramSessionV1:
        fallback = (
            domain_fallback_factory() if domain_fallback_factory is not None else None
        )
        return SearchedWaveProgramSessionV1(
            searched_program,
            runtime_binding,
            domain_fallback=fallback,
        )

    binding = ImportedReactiveProgramBindingV1(
        binding_id=binding_id,
        source_policy_id=searched_program.program_id,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=open_session,
    )
    refs = tuple(
        dict.fromkeys(
            (
                RUNTIME_SCHEMA,
                PROGRAM_SCHEMA,
                searched_program.program_id,
                *searched_program.source_refs,
            )
        )
    )
    program = CausalActionProgramV1(
        program_id=f"searched-runtime::{searched_program.program_id}",
        selector=ImportedReactiveSelectorV1(
            binding_id=binding_id,
            source_policy_id=searched_program.program_id,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.SEARCHED_REACTIVE,
        source_refs=refs,
    )
    return program, binding


__all__ = (
    "RUNTIME_SCHEMA",
    "SearchedWaveProgramSessionV1",
    "SearchedWaveRuntimeV1Error",
    "build_searched_wave_program_runtime_v1",
)
