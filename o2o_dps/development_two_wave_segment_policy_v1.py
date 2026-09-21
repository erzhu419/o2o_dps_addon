"""Development-only wave-conditioned sequence policy with exact Cat fallback.

The searched object is a finite action sequence over one continuous two-wave
segment and one exact build.  Each wave may own a different target, GCD,
next-swing queue, and burst sequence.  A guarded finite resource opportunity
may be skipped on wave one from current HP/remaining-time observations and
retried on wave two without consuming the resource.

This module does not claim an Upper Kara route policy.  It deliberately uses
the existing ``ScheduledActionPlan`` grammar and exposes a runtime binding for
the existing causal program replay.  The binding delegates to exact Cat when
the finite sequence is exhausted, not yet applicable, or mismatches the
current native action surface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    OptionalOffGcdPrefixV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
    ReactiveResolverFactoryV1,
)
from .causal_guard_v1 import (
    GuardObservationError,
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
)
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from .wave_action_schedule_v1 import (
    QueueLaneOp,
    ScheduledActionPlan,
    ScheduledOperationKind,
)


JSONMap = dict[str, Any]
SCHEMA = "development_two_wave_segment_policy/v1"
SCOPE = "DEVELOPMENT_TWO_WAVE_SEGMENT"
_FORBIDDEN_OBSERVATION_FIELDS = frozenset({
    "arrival_ms",
    "dynamic_config_sha256",
    "dynamic_idle_advance",
    "encounter_health_target",
    "future_schedule",
    "remaining_ms",
    "wake_ready",
})


class DevelopmentTwoWaveSegmentPolicyV1Error(RuntimeError):
    """The finite sequence or causal observation violates the v1 contract."""


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


@dataclass(frozen=True)
class SegmentWaveV1:
    wave_id: str
    target_indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _nonempty(self.wave_id, "wave_id"))
        if (
            not isinstance(self.target_indexes, tuple)
            or not self.target_indexes
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in self.target_indexes
            )
            or len(set(self.target_indexes)) != len(self.target_indexes)
        ):
            raise ValueError("target_indexes must be unique nonnegative integers")

    def to_dict(self) -> JSONMap:
        return {
            "wave_id": self.wave_id,
            "target_indexes": list(self.target_indexes),
        }


def _plan_actions(plan: ScheduledActionPlan) -> tuple[ActionRef, ...]:
    values = list(plan.off_gcd_actions)
    if plan.queue_action is not None:
        values.append(plan.queue_action)
    if plan.gcd_action is not None:
        values.append(plan.gcd_action)
    return tuple(values)


@dataclass(frozen=True)
class WaveConditionedSequenceStepV1:
    step_id: str
    wave_id: str
    plan: ScheduledActionPlan
    resource_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _nonempty(self.step_id, "step_id"))
        object.__setattr__(self, "wave_id", _nonempty(self.wave_id, "wave_id"))
        if not isinstance(self.plan, ScheduledActionPlan):
            raise TypeError("plan must be ScheduledActionPlan")
        if self.plan.equipment_action is not None:
            raise ValueError("v1 segment policy does not execute equipment actions")
        if self.resource_id is not None:
            object.__setattr__(
                self,
                "resource_id",
                _nonempty(self.resource_id, "resource_id"),
            )
            guard = self.plan.guard
            if (
                guard is None
                or guard.false_semantics != SKIP_PLAN
                or guard.action_ready is None
                or guard.action_ready not in _plan_actions(self.plan)
            ):
                raise ValueError(
                    "a finite-resource step requires a SKIP_PLAN action-ready guard"
                )
            if self.plan.conditional_prefix_only and self.plan.target_index is not None:
                raise ValueError(
                    "v1 conditional resource retries must be targetless self/AOE actions"
                )

    def to_dict(self) -> JSONMap:
        return {
            "step_id": self.step_id,
            "wave_id": self.wave_id,
            "resource_id": self.resource_id,
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True)
class DevelopmentTwoWaveSegmentPolicyV1:
    policy_id: str
    exact_build_id: str
    waves: tuple[SegmentWaveV1, SegmentWaveV1]
    steps: tuple[WaveConditionedSequenceStepV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _nonempty(self.policy_id, "policy_id"))
        object.__setattr__(
            self,
            "exact_build_id",
            _nonempty(self.exact_build_id, "exact_build_id"),
        )
        if (
            not isinstance(self.waves, tuple)
            or len(self.waves) != 2
            or any(not isinstance(row, SegmentWaveV1) for row in self.waves)
        ):
            raise TypeError("waves must contain exactly two SegmentWaveV1 rows")
        wave_ids = tuple(row.wave_id for row in self.waves)
        if len(set(wave_ids)) != 2:
            raise ValueError("two-wave segment requires distinct wave IDs")
        all_targets = tuple(
            target for wave in self.waves for target in wave.target_indexes
        )
        if len(set(all_targets)) != len(all_targets):
            raise ValueError("wave target registries must be disjoint")
        if not isinstance(self.steps, tuple) or not self.steps or any(
            not isinstance(row, WaveConditionedSequenceStepV1)
            for row in self.steps
        ):
            raise TypeError("steps must be a nonempty tuple of sequence steps")
        step_ids = [row.step_id for row in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step IDs must be unique")
        indexes = {wave_id: index for index, wave_id in enumerate(wave_ids)}
        step_wave_indexes = []
        resource_actions: dict[str, ActionRef] = {}
        for step in self.steps:
            if step.wave_id not in indexes:
                raise ValueError(f"step refers to unknown wave {step.wave_id!r}")
            if step.plan.guide_provenance or step.plan.guide_priority != 0.0:
                raise ValueError("deployment sequence must not retain guide metadata")
            step_wave_indexes.append(indexes[step.wave_id])
            wave = self.waves[indexes[step.wave_id]]
            if (
                step.plan.target_index is not None
                and step.plan.target_index not in wave.target_indexes
            ):
                raise ValueError("step target is outside its declared wave")
            guard_target = step.plan.guard.target_index if step.plan.guard else None
            if guard_target is not None and guard_target not in wave.target_indexes:
                raise ValueError("step guard target is outside its declared wave")
            if step.resource_id is not None:
                assert step.plan.guard is not None
                action = step.plan.guard.action_ready
                assert action is not None
                prior = resource_actions.setdefault(step.resource_id, action)
                if prior != action:
                    raise ValueError("one resource retry chain changes action identity")
        if step_wave_indexes != sorted(step_wave_indexes):
            raise ValueError("all wave-one steps must precede wave-two steps")
        if set(step.wave_id for step in self.steps) != set(wave_ids):
            raise ValueError("both waves require at least one sequence step")

    def to_dict(self) -> JSONMap:
        retry_resources = sorted({
            step.resource_id
            for step in self.steps
            if step.resource_id is not None
            and sum(
                other.resource_id == step.resource_id for other in self.steps
            ) > 1
        })
        return {
            "schema": SCHEMA,
            "scope": SCOPE,
            "policy_id": self.policy_id,
            "exact_build_id": self.exact_build_id,
            "waves": [row.to_dict() for row in self.waves],
            "steps": [row.to_dict() for row in self.steps],
            "resource_retry_ids": retry_resources,
            "contract": {
                "upper_kara_full_route_claim": False,
                "single_exact_build": True,
                "continuous_two_wave_state": True,
                "step_at_or_after_ms_clock": "RELATIVE_TO_FIRST_CAUSAL_WAVE_ACTIVE_OBSERVATION",
                "wave_specific_gcd_queue_target_and_burst_sequences": True,
                "guard_skip_consumes_resource": False,
                "later_wave_resource_retry_supported": True,
                "selector_reads_causal_projection_only": True,
                "arrival_or_future_oracle_fields_used": [],
                "sequence_exhaustion_fallback": CAT_POLICY_ID,
                "sequence_mismatch_fallback": CAT_POLICY_ID,
                "recognized_step_not_ready": "WAIT_WITH_CURSOR_RETAINED",
            },
        }


def freeze_two_wave_segment_policy_v1(
    *,
    policy_id: str,
    exact_build_id: str,
    waves: tuple[SegmentWaveV1, SegmentWaveV1],
    steps: Sequence[WaveConditionedSequenceStepV1],
) -> DevelopmentTwoWaveSegmentPolicyV1:
    """Remove proposal-guide metadata from searched schedules before runtime."""

    frozen = tuple(
        replace(
            step,
            plan=replace(
                step.plan,
                guide_provenance=(),
                guide_priority=0.0,
            ),
        )
        for step in steps
    )
    return DevelopmentTwoWaveSegmentPolicyV1(
        policy_id=policy_id,
        exact_build_id=exact_build_id,
        waves=waves,
        steps=frozen,
    )


def two_wave_segment_policy_digest_v1(
    policy: DevelopmentTwoWaveSegmentPolicyV1,
) -> str:
    """Return the frozen sequence identity carried by the wire program.

    The runtime selector is rebuilt from a deterministic registry after the
    compact manifest is frozen.  Binding only ``policy_id`` would therefore
    let changed steps or guards pass the replay key check.  This digest makes
    that check depend on the complete canonical frozen policy while avoiding
    duplication of the full sequence in every program receipt.
    """

    if not isinstance(policy, DevelopmentTwoWaveSegmentPolicyV1):
        raise TypeError("policy must be DevelopmentTwoWaveSegmentPolicyV1")
    canonical = json.dumps(
        policy.to_dict(),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class DevelopmentTwoWaveSegmentSessionV1:
    """One fresh-seed stateful executor for a frozen finite sequence."""

    def __init__(
        self,
        policy: DevelopmentTwoWaveSegmentPolicyV1,
        cat_resolver: Callable[
            [CausalLiveStateProjectionV1, tuple[AvailableAction, ...]],
            ProgramDecisionV1,
        ],
    ) -> None:
        if not isinstance(policy, DevelopmentTwoWaveSegmentPolicyV1):
            raise TypeError("policy must be DevelopmentTwoWaveSegmentPolicyV1")
        if not callable(cat_resolver):
            raise TypeError("cat_resolver must be callable")
        self.policy = policy
        self._cat_resolver = cat_resolver
        self._next_step = 0
        self._used_resources: set[str] = set()
        self._wave_started_at_ms: dict[str, int] = {}
        self._audit: list[JSONMap] = []

    @property
    def next_step_index(self) -> int:
        return self._next_step

    @property
    def used_resource_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._used_resources))

    @property
    def audit_events(self) -> tuple[JSONMap, ...]:
        return tuple(dict(row) for row in self._audit)

    def _record(self, kind: str, observation: CausalLiveStateProjectionV1, **rows: Any) -> None:
        self._audit.append({
            "kind": kind,
            "state_time_ms": observation.visibility_cutoff_ms,
            **rows,
        })

    def _cat(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
        reason: str,
    ) -> ProgramDecisionV1:
        self._record(
            "EXACT_CAT_FALLBACK",
            observation,
            reason=reason,
            next_step_index=self._next_step,
        )
        decision = self._cat_resolver(observation, available)
        if not isinstance(decision, ProgramDecisionV1):
            raise TypeError("cat_resolver must return ProgramDecisionV1")
        return decision

    def _wait(
        self,
        observation: CausalLiveStateProjectionV1,
        *,
        reason: str,
        wait_ms: int,
        target_index: int,
    ) -> ProgramDecisionV1:
        delay = max(1, min(100, wait_ms))
        self._record(
            "SEQUENCE_WAIT",
            observation,
            reason=reason,
            wait_ms=delay,
            next_step_index=self._next_step,
        )
        # A searched order may deliberately wait for rage, a cooldown, or its
        # scheduled slot.  Keep white swings running while the cursor is held;
        # otherwise a zero-rage opener can wait forever because no rage is
        # generated.  The target is selected from the currently active wave,
        # never from a future registry row.
        return ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            wait_ms=delay,
        )

    def _wave_status(
        self,
        wave: SegmentWaveV1,
        observation: CausalLiveStateProjectionV1,
    ) -> str:
        semantics = observation.state.get("dynamic_target_semantics")
        rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
        if not isinstance(rows, list):
            raise DevelopmentTwoWaveSegmentPolicyV1Error(
                "causal observation lacks visible target semantics"
            )
        precombat = observation.state.get("precombat")
        if (
            wave.wave_id == self.policy.waves[0].wave_id
            and isinstance(precombat, Mapping)
            and precombat.get("active") is True
        ):
            return "PRECOMBAT"
        visible = {
            row.get("target_index"): row
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("target_index"), int)
            and not isinstance(row.get("target_index"), bool)
        }
        wave_rows = [visible[index] for index in wave.target_indexes if index in visible]
        if not wave_rows:
            return "FUTURE_NOT_VISIBLE"
        if any(row.get("attackable") is True and row.get("dead") is not True for row in wave_rows):
            return "ACTIVE"
        all_visible = len(wave_rows) == len(wave.target_indexes)
        if all_visible and all(row.get("dead") is True for row in wave_rows):
            return "PASSED"
        return "FUTURE_NOT_VISIBLE"

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("segment policy requires CausalLiveStateProjectionV1")
        if any(not isinstance(row, AvailableAction) for row in available):
            raise TypeError("available must contain AvailableAction values")
        leaked = sorted(_FORBIDDEN_OBSERVATION_FIELDS & set(observation.state))
        if leaked:
            raise DevelopmentTwoWaveSegmentPolicyV1Error(
                f"causal observation exposes forbidden fields: {leaked}"
            )
        if observation.state.get("time_ms") != observation.visibility_cutoff_ms:
            raise DevelopmentTwoWaveSegmentPolicyV1Error(
                "causal observation time differs from visibility cutoff"
            )
        by_action = {row.action: row for row in available}
        if len(by_action) != len(available):
            raise DevelopmentTwoWaveSegmentPolicyV1Error(
                "native action surface contains duplicate identities"
            )

        while self._next_step < len(self.policy.steps):
            step_index = self._next_step
            step = self.policy.steps[step_index]
            wave = next(row for row in self.policy.waves if row.wave_id == step.wave_id)
            status = self._wave_status(wave, observation)
            if status == "PRECOMBAT":
                # The finite combat prefix starts at the first causal live
                # observation.  Let exact Cat own the pre-pull clock without
                # consuming or skipping any searched step.
                return self._cat(observation, available, "SEQUENCE_NOT_STARTED_PRECOMBAT")
            if status == "PASSED":
                self._record(
                    "SEQUENCE_STEP_WAVE_PASSED",
                    observation,
                    step_id=step.step_id,
                    wave_id=step.wave_id,
                )
                self._next_step += 1
                continue
            if status == "FUTURE_NOT_VISIBLE":
                return self._cat(observation, available, "STEP_WAVE_NOT_VISIBLE")
            wave_started_at = self._wave_started_at_ms.setdefault(
                wave.wave_id, observation.visibility_cutoff_ms
            )
            wave_elapsed_ms = observation.visibility_cutoff_ms - wave_started_at
            if wave_elapsed_ms < step.plan.at_or_after_ms:
                return self._wait(
                    observation,
                    reason="STEP_TIME_NOT_REACHED",
                    wait_ms=step.plan.at_or_after_ms - wave_elapsed_ms,
                    target_index=(
                        step.plan.target_index
                        if step.plan.target_index is not None
                        else wave.target_indexes[0]
                    ),
                )
            if step.resource_id in self._used_resources:
                self._record(
                    "RESOURCE_RETRY_ALREADY_CONSUMED",
                    observation,
                    step_id=step.step_id,
                    resource_id=step.resource_id,
                )
                self._next_step += 1
                continue

            guard = step.plan.guard
            if guard is not None:
                try:
                    evaluation = evaluate_observable_guard_v1(
                        guard, observation.state, available
                    )
                except GuardObservationError as error:
                    return self._cat(
                        observation,
                        available,
                        f"GUARD_OBSERVATION_UNAVAILABLE:{error}",
                    )
                if not evaluation.satisfied:
                    failed = set(evaluation.failed_predicates)
                    remaining_unknown = (
                        "estimated_remaining_attackable_gte_ms" in failed
                        and evaluation.observed.get(
                            "estimated_remaining_attackable_ms"
                        )
                        is None
                    )
                    if remaining_unknown and not (
                        failed
                        - {
                            "estimated_remaining_attackable_gte_ms",
                            "action_ready",
                        }
                    ):
                        if "action_ready" in failed:
                            guarded = by_action.get(guard.action_ready)
                            if guarded is None:
                                return self._cat(
                                    observation,
                                    available,
                                    "SEQUENCE_NATIVE_ACTION_MISMATCH",
                                )
                        return self._wait(
                            observation,
                            reason="CAUSAL_REMAINING_TIME_NOT_YET_ESTIMABLE",
                            wait_ms=guard.check_interval_ms,
                            target_index=(
                                step.plan.target_index
                                if step.plan.target_index is not None
                                else wave.target_indexes[0]
                            ),
                        )
                    # A recognized action whose cooldown has not completed is
                    # a transient sequence wait, not permission for Cat to
                    # issue a different core action.  Other failed resource
                    # predicates (HP/lifetime/attackability) intentionally
                    # skip this wave's opportunity while retaining the finite
                    # resource for a later-wave retry.
                    if evaluation.failed_predicates == ("action_ready",):
                        guarded = by_action.get(guard.action_ready)
                        if guarded is not None:
                            return self._wait(
                                observation,
                                reason="GUARDED_ACTION_NOT_READY",
                                wait_ms=(
                                    guarded.ready_in_ms
                                    if guarded.ready_in_ms > 0
                                    else guard.check_interval_ms
                                ),
                                target_index=(
                                    step.plan.target_index
                                    if step.plan.target_index is not None
                                    else wave.target_indexes[0]
                                ),
                            )
                        return self._cat(
                            observation,
                            available,
                            "SEQUENCE_NATIVE_ACTION_MISMATCH",
                        )
                    self._record(
                        (
                            "GUARD_SKIP_RESOURCE_RETAINED"
                            if step.resource_id is not None
                            else "GUARD_SKIP_SEQUENCE_STEP"
                        ),
                        observation,
                        step_id=step.step_id,
                        wave_id=step.wave_id,
                        resource_id=step.resource_id,
                        failed_predicates=list(evaluation.failed_predicates),
                    )
                    self._next_step += 1
                    continue

            actions = _plan_actions(step.plan)
            unrecognized = tuple(
                action
                for action in actions
                if by_action.get(action) is None
            )
            if unrecognized:
                return self._cat(
                    observation,
                    available,
                    "SEQUENCE_NATIVE_ACTION_MISMATCH",
                )
            not_ready = tuple(
                (
                    by_action[action].ready_in_ms
                    if by_action[action].ready_in_ms > 0
                    else 100
                )
                for action in actions
                if (
                    by_action[action].ready_in_ms > 0
                    or not by_action[action].legal
                )
            )
            if not_ready:
                return self._wait(
                    observation,
                    reason="SEQUENCE_ACTION_NOT_READY",
                    wait_ms=min(not_ready),
                    target_index=(
                        step.plan.target_index
                        if step.plan.target_index is not None
                        else wave.target_indexes[0]
                    ),
                )

            self._next_step += 1
            decision = self._decision_from_plan(
                step, wave, observation, available
            )
            if step.resource_id is not None:
                self._used_resources.add(step.resource_id)
            self._record(
                "WAVE_SEQUENCE_STEP_SELECTED",
                observation,
                step_id=step.step_id,
                wave_id=step.wave_id,
                resource_id=step.resource_id,
                target_index=step.plan.target_index,
                wave_elapsed_ms=wave_elapsed_ms,
            )
            return decision

        return self._cat(observation, available, "SEQUENCE_EXHAUSTED")

    def _decision_from_plan(
        self,
        step: WaveConditionedSequenceStepV1,
        wave: SegmentWaveV1,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        plan = step.plan
        prefixes = tuple(
            OptionalOffGcdPrefixV1(
                action,
                (
                    plan.guard
                    if plan.guard is not None
                    and plan.guard.action_ready == action
                    else ObservableCausalGuardV1(
                        target_index=plan.target_index,
                        target_attackable_is=(
                            True if plan.target_index is not None else None
                        ),
                        action_ready=action,
                        false_semantics=SKIP_PLAN,
                    )
                ),
            )
            for action in plan.off_gcd_actions
        )
        if plan.conditional_prefix_only:
            source = self._cat_resolver(observation, available)
            if not isinstance(source, ProgramDecisionV1):
                raise TypeError("cat_resolver must return ProgramDecisionV1")
            return replace(
                source,
                optional_off_gcd_prefixes=(
                    *prefixes,
                    *source.optional_off_gcd_prefixes,
                ),
                prefix_order=(
                    *(
                        ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
                        for _ in prefixes
                    ),
                    *(source.prefix_order or ()),
                ),
            )

        prefix_kind = {
            ScheduledOperationKind.SET_TARGET: (
                ProgramPrefixOperationKindV1.SET_TARGET
            ),
            ScheduledOperationKind.ACT_OFF_GCD: (
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD
            ),
            ScheduledOperationKind.QUEUE_KEEP: (
                ProgramPrefixOperationKindV1.QUEUE_KEEP
            ),
            ScheduledOperationKind.QUEUE_SET: (
                ProgramPrefixOperationKindV1.QUEUE_SET
            ),
            ScheduledOperationKind.QUEUE_CANCEL: (
                ProgramPrefixOperationKindV1.QUEUE_CANCEL
            ),
        }
        try:
            mapped_prefix = [
                prefix_kind[row] for row in plan.prefix_order or ()
            ]
        except KeyError as error:
            raise DevelopmentTwoWaveSegmentPolicyV1Error(
                f"unsupported scheduled prefix operation {error.args[0].value}"
            ) from error
        if ProgramPrefixOperationKindV1.SET_TARGET in mapped_prefix:
            target_position = mapped_prefix.index(
                ProgramPrefixOperationKindV1.SET_TARGET
            )
            mapped_prefix.insert(
                target_position + 1,
                ProgramPrefixOperationKindV1.START_ATTACK,
            )
        else:
            mapped_prefix[0:0] = [
                ProgramPrefixOperationKindV1.SET_TARGET,
                ProgramPrefixOperationKindV1.START_ATTACK,
            ]
        prefix_order = tuple(mapped_prefix)
        return ProgramDecisionV1(
            target_index=(
                plan.target_index
                if plan.target_index is not None
                else wave.target_indexes[0]
            ),
            start_attack=True,
            optional_off_gcd_prefixes=prefixes,
            queue_op=plan.queue_op,
            queue_action=plan.queue_action,
            gcd_action=plan.gcd_action,
            wait_ms=plan.wait_ms,
            prefix_order=prefix_order,
        )


def build_two_wave_segment_runtime_v1(
    policy: DevelopmentTwoWaveSegmentPolicyV1,
    *,
    cat_resolver_factory: ReactiveResolverFactoryV1,
) -> tuple[CausalActionProgramV1, ImportedReactiveProgramBindingV1]:
    """Bind a frozen development sequence to a fresh exact-Cat session."""

    if not isinstance(policy, DevelopmentTwoWaveSegmentPolicyV1):
        raise TypeError("policy must be DevelopmentTwoWaveSegmentPolicyV1")
    if not callable(cat_resolver_factory):
        raise TypeError("cat_resolver_factory must be callable")
    binding_id = f"development-two-wave-segment::{policy.policy_id}"

    def open_session() -> DevelopmentTwoWaveSegmentSessionV1:
        return DevelopmentTwoWaveSegmentSessionV1(
            policy, cat_resolver_factory()
        )

    binding = ImportedReactiveProgramBindingV1(
        binding_id=binding_id,
        source_policy_id=policy.policy_id,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=open_session,
    )
    program = CausalActionProgramV1(
        program_id=policy.policy_id,
        selector=ImportedReactiveSelectorV1(
            binding_id=binding_id,
            source_policy_id=policy.policy_id,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.SEARCHED_REACTIVE,
        source_refs=(
            SCHEMA,
            CAT_POLICY_ID,
            "frozen-policy-sha256:"
            + two_wave_segment_policy_digest_v1(policy),
        ),
    )
    return program, binding


__all__ = (
    "SCHEMA",
    "SCOPE",
    "DevelopmentTwoWaveSegmentPolicyV1",
    "DevelopmentTwoWaveSegmentPolicyV1Error",
    "DevelopmentTwoWaveSegmentSessionV1",
    "SegmentWaveV1",
    "WaveConditionedSequenceStepV1",
    "build_two_wave_segment_runtime_v1",
    "freeze_two_wave_segment_policy_v1",
    "two_wave_segment_policy_digest_v1",
)
