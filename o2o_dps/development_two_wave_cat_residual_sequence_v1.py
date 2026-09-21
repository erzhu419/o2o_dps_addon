"""Development-only variable-length Cat-relative two-wave residuals.

The old two-wave segment candidate forces a scheduled prefix and can hold its
cursor while an action is unavailable.  This module provides the smaller
residual primitive needed by the next search stage: Cat is advanced exactly
once at every decision epoch, and a guarded residual may replace that one
decision only when its complete action body is executable now.  Otherwise the
same Cat decision is returned immediately.

The frozen object contains no simulator schedule, arrival time, future target,
or environment-control input.  Wave identity is inferred only from target
rows visible in the current causal observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ReactiveResolverFactoryV1,
    program_decision_from_dict_v1,
)
from .causal_guard_v1 import (
    GuardObservationError,
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
    observable_causal_guard_from_dict_v1,
)
from .cat_fury_ordered_sink_executor_v5 import (
    _ACTION_REFS as _CAT_ACTION_REFS,
)
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .policy_observation_causal_projection_v1 import (
    CausalLiveStateProjectionV1,
)
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
SCHEMA = "development_two_wave_cat_residual_sequence/v1"
SCOPE = "DEVELOPMENT_TWO_WAVE_CAT_RELATIVE_RESIDUAL_SEQUENCE"
ZERO_RESIDUAL_SOURCE_REF_V1 = "cat-residual-sequence/v1:zero"
NONZERO_RESIDUAL_SOURCE_REF_V1 = "cat-residual-sequence/v1:nonzero"

_FORBIDDEN_OBSERVATION_FIELDS = frozenset(
    {
        "arrival_ms",
        "dynamic_config_sha256",
        "dynamic_idle_advance",
        "encounter_health_target",
        "future_schedule",
        "remaining_ms",
        "wake_ready",
    }
)
_CAT_ACTION_KEY_BY_REF = {value: key for key, value in _CAT_ACTION_REFS.items()}
if len(_CAT_ACTION_KEY_BY_REF) != len(_CAT_ACTION_REFS):
    raise RuntimeError("Cat v5 action references are not one-to-one")
# Recklessness is intentionally outside Cat's ordinary v5 action surface but
# is registered by the exact Turtle loadout and is a searched burst action.
_CAT_ACTION_KEY_BY_REF[ActionRef(spell_id=1_719)] = "warrior.recklessness"


class DevelopmentTwoWaveCatResidualSequenceV1Error(RuntimeError):
    """A frozen residual sequence or its causal observation is invalid."""


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _exact_mapping(
    value: object,
    fields: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if set(value) != fields:
        raise ValueError(
            f"{label} fields differ: expected {sorted(fields)}, "
            f"got {sorted(value)}"
        )
    return value


@dataclass(frozen=True)
class CatResidualSequenceWaveV1:
    """The target identities belonging to one route-local wave."""

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
            raise ValueError(
                "target_indexes must be unique nonnegative integers"
            )

    def to_dict(self) -> JSONMap:
        return {
            "wave_id": self.wave_id,
            "target_indexes": list(self.target_indexes),
        }


@dataclass(frozen=True)
class CatResidualSequenceStepV1:
    """One wave-local, at-most-once guarded replacement of a Cat decision."""

    step_id: str
    wave_id: str
    guard: ObservableCausalGuardV1
    decision: ProgramDecisionV1
    expected_cat_gcd_action: ActionRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _nonempty(self.step_id, "step_id"))
        object.__setattr__(self, "wave_id", _nonempty(self.wave_id, "wave_id"))
        if not isinstance(self.guard, ObservableCausalGuardV1):
            raise TypeError("guard must be ObservableCausalGuardV1")
        if self.guard.false_semantics != SKIP_PLAN:
            raise ValueError(
                "residual step guard must use SKIP_PLAN Cat-fallback semantics"
            )
        if not isinstance(self.decision, ProgramDecisionV1):
            raise TypeError("decision must be ProgramDecisionV1")
        if self.expected_cat_gcd_action is not None and not isinstance(
            self.expected_cat_gcd_action, ActionRef
        ):
            raise TypeError("expected_cat_gcd_action must be ActionRef or None")

    def to_dict(self) -> JSONMap:
        return {
            "step_id": self.step_id,
            "wave_id": self.wave_id,
            "guard": self.guard.to_dict(),
            "decision": self.decision.to_dict(),
            "expected_cat_gcd_action": (
                None
                if self.expected_cat_gcd_action is None
                else self.expected_cat_gcd_action.to_wire()
            ),
        }


_FROZEN_CONTRACT: JSONMap = {
    "upper_kara_full_route_claim": False,
    "single_exact_build": True,
    "continuous_two_wave_state": True,
    "variable_length_per_wave": True,
    "empty_sequence_is_exact_cat": True,
    "wave_detection": "CURRENT_VISIBLE_TARGET_SEMANTICS_ONLY",
    "step_latch": "WAVE_LOCAL_ONCE_AFTER_EXECUTION",
    "source_decision_condition": "CURRENT_CAT_PROPOSED_GCD_ONLY",
    "cat_session_calls_per_epoch": 1,
    "guard_false_fallback": CAT_POLICY_ID,
    "unavailable_action_fallback": CAT_POLICY_ID,
    "fallback_waits_for_residual": False,
    "arrival_or_future_or_environment_control_fields_used": [],
}


@dataclass(frozen=True)
class DevelopmentTwoWaveCatResidualSequenceV1:
    """Frozen variable-length residual sequence over one exact build."""

    policy_id: str
    exact_build_id: str
    waves: tuple[CatResidualSequenceWaveV1, CatResidualSequenceWaveV1]
    steps: tuple[CatResidualSequenceStepV1, ...] = ()

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
            or any(
                not isinstance(row, CatResidualSequenceWaveV1)
                for row in self.waves
            )
        ):
            raise TypeError(
                "waves must contain exactly two CatResidualSequenceWaveV1 rows"
            )
        wave_ids = tuple(row.wave_id for row in self.waves)
        if len(set(wave_ids)) != 2:
            raise ValueError("two-wave residual requires distinct wave IDs")
        all_targets = tuple(
            target for wave in self.waves for target in wave.target_indexes
        )
        if len(set(all_targets)) != len(all_targets):
            raise ValueError("wave target registries must be disjoint")
        if not isinstance(self.steps, tuple) or any(
            not isinstance(row, CatResidualSequenceStepV1)
            for row in self.steps
        ):
            raise TypeError("steps must contain CatResidualSequenceStepV1 rows")

        waves_by_id = {row.wave_id: row for row in self.waves}
        step_keys: set[tuple[str, str]] = set()
        for step in self.steps:
            wave = waves_by_id.get(step.wave_id)
            if wave is None:
                raise ValueError(f"step refers to unknown wave {step.wave_id!r}")
            key = (step.wave_id, step.step_id)
            if key in step_keys:
                raise ValueError("step IDs must be unique within each wave")
            step_keys.add(key)
            self._validate_step_targets(step, wave)

    @staticmethod
    def _validate_step_targets(
        step: CatResidualSequenceStepV1,
        wave: CatResidualSequenceWaveV1,
    ) -> None:
        targets = set(wave.target_indexes)
        guard_targets = [step.guard.target_index]
        guard_targets.extend(
            prefix.guard.target_index
            for prefix in step.decision.optional_off_gcd_prefixes
        )
        if any(
            target is not None and target not in targets
            for target in guard_targets
        ):
            raise ValueError("step guard reads a target outside its wave")
        if (
            step.decision.target_index is not None
            and step.decision.target_index not in targets
        ):
            raise ValueError("step decision targets an index outside its wave")
        if (
            step.decision.gcd_action is not None
            and step.decision.gcd_action not in _CAT_ACTION_KEY_BY_REF
        ):
            raise ValueError(
                "step replacement GCD lacks a canonical Cat v5 action key"
            )

    def to_dict(self) -> JSONMap:
        return {
            "schema": SCHEMA,
            "scope": SCOPE,
            "policy_id": self.policy_id,
            "exact_build_id": self.exact_build_id,
            "waves": [row.to_dict() for row in self.waves],
            "steps": [row.to_dict() for row in self.steps],
            "contract": dict(_FROZEN_CONTRACT),
        }


def freeze_two_wave_cat_residual_sequence_v1(
    *,
    policy_id: str,
    exact_build_id: str,
    waves: tuple[CatResidualSequenceWaveV1, CatResidualSequenceWaveV1],
    steps: Sequence[CatResidualSequenceStepV1] = (),
) -> DevelopmentTwoWaveCatResidualSequenceV1:
    """Freeze a searched sequence into the closed runtime wire contract."""

    return DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id=policy_id,
        exact_build_id=exact_build_id,
        waves=waves,
        steps=tuple(steps),
    )


def two_wave_cat_residual_sequence_from_dict_v1(
    value: object,
) -> DevelopmentTwoWaveCatResidualSequenceV1:
    """Restore only the exact closed wire emitted by :meth:`to_dict`."""

    raw = _exact_mapping(
        value,
        {
            "schema",
            "scope",
            "policy_id",
            "exact_build_id",
            "waves",
            "steps",
            "contract",
        },
        "two-wave Cat residual sequence",
    )
    if raw["schema"] != SCHEMA or raw["scope"] != SCOPE:
        raise ValueError("two-wave Cat residual schema or scope differs")
    if raw["contract"] != _FROZEN_CONTRACT:
        raise ValueError("two-wave Cat residual contract differs from v1")
    wave_rows = raw["waves"]
    step_rows = raw["steps"]
    if not isinstance(wave_rows, list) or len(wave_rows) != 2:
        raise ValueError("waves must be an array of length two")
    if not isinstance(step_rows, list):
        raise ValueError("steps must be an array")

    waves: list[CatResidualSequenceWaveV1] = []
    for index, row in enumerate(wave_rows):
        parsed = _exact_mapping(
            row, {"wave_id", "target_indexes"}, f"waves[{index}]"
        )
        targets = parsed["target_indexes"]
        if not isinstance(targets, list):
            raise ValueError(f"waves[{index}].target_indexes must be an array")
        waves.append(
            CatResidualSequenceWaveV1(
                wave_id=parsed["wave_id"],
                target_indexes=tuple(targets),
            )
        )

    steps: list[CatResidualSequenceStepV1] = []
    for index, row in enumerate(step_rows):
        parsed = _exact_mapping(
            row,
            {
                "step_id",
                "wave_id",
                "guard",
                "decision",
                "expected_cat_gcd_action",
            },
            f"steps[{index}]",
        )
        steps.append(
            CatResidualSequenceStepV1(
                step_id=parsed["step_id"],
                wave_id=parsed["wave_id"],
                guard=observable_causal_guard_from_dict_v1(
                    parsed["guard"], label=f"steps[{index}].guard"
                ),
                decision=program_decision_from_dict_v1(parsed["decision"]),
                expected_cat_gcd_action=(
                    None
                    if parsed["expected_cat_gcd_action"] is None
                    else ActionRef.from_wire(parsed["expected_cat_gcd_action"])
                ),
            )
        )
    return DevelopmentTwoWaveCatResidualSequenceV1(
        policy_id=raw["policy_id"],
        exact_build_id=raw["exact_build_id"],
        waves=(waves[0], waves[1]),
        steps=tuple(steps),
    )


def frozen_two_wave_cat_residual_sequence_wire_v1(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
) -> JSONMap:
    """Return the portable frozen wire for storage in a candidate manifest."""

    if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
        raise TypeError(
            "policy must be DevelopmentTwoWaveCatResidualSequenceV1"
        )
    return policy.to_dict()


def _decision_actions(decision: ProgramDecisionV1) -> tuple[ActionRef, ...]:
    actions = [prefix.action for prefix in decision.optional_off_gcd_prefixes]
    if decision.queue_op is QueueLaneOp.SET:
        assert decision.queue_action is not None
        actions.append(decision.queue_action)
    if decision.gcd_action is not None:
        actions.append(decision.gcd_action)
    return tuple(actions)


@dataclass(frozen=True)
class CatResidualSequenceDecisionTraceV1:
    """Non-wire trace of one parent-policy decision epoch.

    The trace is deliberately excluded from the frozen policy contract.  It
    lets development teachers prove that a later intervention is appended
    after an already executed residual prefix, without exposing a decision
    index, simulator seed, or future suffix to the runtime policy itself.
    """

    decision_index: int
    observation: CausalLiveStateProjectionV1
    available_actions: tuple[AvailableAction, ...]
    cat_decision: ProgramDecisionV1
    parent_decision: ProgramDecisionV1
    executed_step_keys_before: tuple[tuple[str, str], ...]
    executed_step_keys_after: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.decision_index, bool)
            or not isinstance(self.decision_index, int)
            or self.decision_index < 0
        ):
            raise ValueError("decision_index must be a nonnegative integer")
        if not isinstance(self.observation, CausalLiveStateProjectionV1):
            raise TypeError("observation must be CausalLiveStateProjectionV1")
        if any(not isinstance(row, AvailableAction) for row in self.available_actions):
            raise TypeError("available_actions must contain AvailableAction")
        if not isinstance(self.cat_decision, ProgramDecisionV1):
            raise TypeError("cat_decision must be ProgramDecisionV1")
        if not isinstance(self.parent_decision, ProgramDecisionV1):
            raise TypeError("parent_decision must be ProgramDecisionV1")


class DevelopmentTwoWaveCatResidualSequenceSessionV1:
    """One stateful Cat session plus wave-local at-most-once residual latches."""

    def __init__(
        self,
        policy: DevelopmentTwoWaveCatResidualSequenceV1,
        cat_resolver: Callable[
            [CausalLiveStateProjectionV1, tuple[AvailableAction, ...]],
            ProgramDecisionV1,
        ],
    ) -> None:
        if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
            raise TypeError(
                "policy must be DevelopmentTwoWaveCatResidualSequenceV1"
            )
        if not callable(cat_resolver):
            raise TypeError("cat_resolver must be callable")
        self.policy = policy
        self._cat_resolver = cat_resolver
        self._executed: set[tuple[str, str]] = set()
        self._execution_order: list[tuple[str, str]] = []
        self._audit: list[JSONMap] = []
        self._pending_actual_gcd_key: str | None = None
        self._decision_count = 0
        self._last_decision_trace: CatResidualSequenceDecisionTraceV1 | None = None
        self._last_prior_gcd_key: str | None = None
        self._last_tracks_gcd = False
        self._last_execution_feedback_open = False

    @property
    def executed_step_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._execution_order)

    @property
    def audit_events(self) -> tuple[JSONMap, ...]:
        return tuple(dict(row) for row in self._audit)

    @property
    def last_decision_trace(self) -> CatResidualSequenceDecisionTraceV1 | None:
        """Return the most recent development trace, if a decision was made."""

        return self._last_decision_trace

    def record_last_executed_decision_v1(
        self,
        actual_decision: ProgramDecisionV1,
    ) -> None:
        """Correct Cat continuation when an outer teacher replaced the parent.

        Normal residual execution never calls this hook.  A development-only
        append teacher may call it exactly once, immediately after the most
        recent decision, so the next epoch observes the GCD that was actually
        returned to the simulator rather than the unexecuted parent proposal.
        It does not alter the frozen policy, residual latches, or current
        return value.
        """

        if not isinstance(actual_decision, ProgramDecisionV1):
            raise TypeError("actual_decision must be ProgramDecisionV1")
        if self._last_decision_trace is None or not self._last_execution_feedback_open:
            raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                "execution feedback must immediately follow one unresolved decision"
            )
        self._last_execution_feedback_open = False
        if actual_decision == self._last_decision_trace.parent_decision:
            return
        if not self._last_tracks_gcd:
            return
        setattr(self._cat_resolver, "last_gcd_action", self._last_prior_gcd_key or "")
        if actual_decision.gcd_action is None:
            self._pending_actual_gcd_key = None
            return
        try:
            self._pending_actual_gcd_key = _CAT_ACTION_KEY_BY_REF[
                actual_decision.gcd_action
            ]
        except KeyError as error:
            raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                "actual replacement GCD lacks a canonical Cat continuation key"
            ) from error

    def _finish_decision(
        self,
        *,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
        cat_decision: ProgramDecisionV1,
        parent_decision: ProgramDecisionV1,
        executed_before: tuple[tuple[str, str], ...],
        prior_last_gcd: str | None,
        tracks_last_gcd: bool,
    ) -> ProgramDecisionV1:
        self._last_decision_trace = CatResidualSequenceDecisionTraceV1(
            decision_index=self._decision_count,
            observation=observation,
            available_actions=available,
            cat_decision=cat_decision,
            parent_decision=parent_decision,
            executed_step_keys_before=executed_before,
            executed_step_keys_after=self.executed_step_keys,
        )
        self._decision_count += 1
        self._last_prior_gcd_key = prior_last_gcd
        self._last_tracks_gcd = tracks_last_gcd
        self._last_execution_feedback_open = True
        return parent_decision

    def _record(
        self,
        kind: str,
        observation: CausalLiveStateProjectionV1,
        **rows: Any,
    ) -> None:
        self._audit.append(
            {
                "kind": kind,
                "state_time_ms": observation.visibility_cutoff_ms,
                **rows,
            }
        )

    def _current_wave(
        self,
        observation: CausalLiveStateProjectionV1,
    ) -> CatResidualSequenceWaveV1 | None:
        precombat = observation.state.get("precombat")
        if isinstance(precombat, Mapping) and precombat.get("active") is True:
            return None
        semantics = observation.state.get("dynamic_target_semantics")
        rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
        if not isinstance(rows, list):
            raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                "causal observation lacks visible target semantics"
            )
        visible: dict[int, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                    "visible target semantics contain a non-object row"
                )
            target_index = row.get("target_index")
            if isinstance(target_index, bool) or not isinstance(target_index, int):
                raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                    "visible target semantics lack an integer target_index"
                )
            if target_index in visible:
                raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                    "visible target semantics repeat target_index"
                )
            visible[target_index] = row

        active = {
            target_index
            for target_index, row in visible.items()
            if row.get("attackable") is True and row.get("dead") is not True
        }
        selected = observation.state.get("target_index")
        if (
            isinstance(selected, int)
            and not isinstance(selected, bool)
            and selected in active
        ):
            for wave in self.policy.waves:
                if selected in wave.target_indexes:
                    return wave
        for wave in self.policy.waves:
            if active.intersection(wave.target_indexes):
                return wave
        return None

    def _fallback(
        self,
        observation: CausalLiveStateProjectionV1,
        cat_decision: ProgramDecisionV1,
        reason: str,
        *,
        step: CatResidualSequenceStepV1 | None = None,
        **rows: Any,
    ) -> ProgramDecisionV1:
        self._record(
            "EXACT_CAT_FALLBACK",
            observation,
            reason=reason,
            wave_id=(step.wave_id if step is not None else None),
            step_id=(step.step_id if step is not None else None),
            **rows,
        )
        return cat_decision

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError(
                "residual sequence requires CausalLiveStateProjectionV1"
            )
        if not isinstance(available, tuple) or any(
            not isinstance(row, AvailableAction) for row in available
        ):
            raise TypeError("available must be a tuple of AvailableAction values")
        leaked = sorted(_FORBIDDEN_OBSERVATION_FIELDS & set(observation.state))
        if leaked:
            raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                f"causal observation exposes forbidden fields: {leaked}"
            )
        if observation.state.get("time_ms") != observation.visibility_cutoff_ms:
            raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                "causal observation time differs from visibility cutoff"
            )
        by_action = {row.action: row for row in available}
        if len(by_action) != len(available):
            raise DevelopmentTwoWaveCatResidualSequenceV1Error(
                "native action surface contains duplicate identities"
            )

        # Once the next epoch begins, the preceding decision can no longer be
        # corrected by an outer execution-feedback hook.
        self._last_execution_feedback_open = False
        executed_before = self.executed_step_keys

        # Submit the preceding epoch's actual replacement immediately before
        # Cat maps the next observation.  The imported Cat session otherwise
        # uses the GCD it proposed (rather than the one the residual executed)
        # as ``last_gcd_action``.
        tracks_last_gcd = hasattr(self._cat_resolver, "last_gcd_action")
        if tracks_last_gcd and self._pending_actual_gcd_key is not None:
            setattr(
                self._cat_resolver,
                "last_gcd_action",
                self._pending_actual_gcd_key,
            )
            self._pending_actual_gcd_key = None
        prior_last_gcd = (
            getattr(self._cat_resolver, "last_gcd_action")
            if tracks_last_gcd
            else None
        )

        # Advance the one underlying Cat session at every epoch, including an
        # epoch replaced by a residual.  This is what makes zero steps exactly
        # Cat and prevents a matched replacement from restarting or lagging Cat.
        cat_decision = self._cat_resolver(observation, available)
        if not isinstance(cat_decision, ProgramDecisionV1):
            raise TypeError("cat_resolver must return ProgramDecisionV1")

        wave = self._current_wave(observation)
        if wave is None:
            decision = self._fallback(
                observation, cat_decision, "NO_CURRENT_VISIBLE_ACTIVE_WAVE"
            )
            return self._finish_decision(
                observation=observation,
                available=available,
                cat_decision=cat_decision,
                parent_decision=decision,
                executed_before=executed_before,
                prior_last_gcd=prior_last_gcd,
                tracks_last_gcd=tracks_last_gcd,
            )
        pending = tuple(
            step
            for step in self.policy.steps
            if step.wave_id == wave.wave_id
            and (step.wave_id, step.step_id) not in self._executed
        )
        if not pending:
            decision = self._fallback(
                observation,
                cat_decision,
                "WAVE_RESIDUAL_SEQUENCE_EXHAUSTED",
            )
            return self._finish_decision(
                observation=observation,
                available=available,
                cat_decision=cat_decision,
                parent_decision=decision,
                executed_before=executed_before,
                prior_last_gcd=prior_last_gcd,
                tracks_last_gcd=tracks_last_gcd,
            )
        step = pending[0]
        if (
            step.expected_cat_gcd_action is not None
            and cat_decision.gcd_action != step.expected_cat_gcd_action
        ):
            decision = self._fallback(
                observation,
                cat_decision,
                "CAT_SOURCE_GCD_MISMATCH",
                step=step,
                expected_cat_gcd_action=step.expected_cat_gcd_action.to_wire(),
                observed_cat_gcd_action=(
                    None
                    if cat_decision.gcd_action is None
                    else cat_decision.gcd_action.to_wire()
                ),
            )
            return self._finish_decision(
                observation=observation,
                available=available,
                cat_decision=cat_decision,
                parent_decision=decision,
                executed_before=executed_before,
                prior_last_gcd=prior_last_gcd,
                tracks_last_gcd=tracks_last_gcd,
            )
        try:
            evaluation = evaluate_observable_guard_v1(
                step.guard, observation.state, available
            )
        except GuardObservationError as error:
            decision = self._fallback(
                observation,
                cat_decision,
                f"GUARD_OBSERVATION_UNAVAILABLE:{error}",
                step=step,
            )
            return self._finish_decision(
                observation=observation,
                available=available,
                cat_decision=cat_decision,
                parent_decision=decision,
                executed_before=executed_before,
                prior_last_gcd=prior_last_gcd,
                tracks_last_gcd=tracks_last_gcd,
            )
        if not evaluation.satisfied:
            decision = self._fallback(
                observation,
                cat_decision,
                "RESIDUAL_GUARD_FALSE",
                step=step,
                failed_predicates=list(evaluation.failed_predicates),
            )
            return self._finish_decision(
                observation=observation,
                available=available,
                cat_decision=cat_decision,
                parent_decision=decision,
                executed_before=executed_before,
                prior_last_gcd=prior_last_gcd,
                tracks_last_gcd=tracks_last_gcd,
            )

        for action in _decision_actions(step.decision):
            row = by_action.get(action)
            if row is None:
                decision = self._fallback(
                    observation,
                    cat_decision,
                    "RESIDUAL_ACTION_ABSENT",
                    step=step,
                    action=action.to_wire(),
                )
                return self._finish_decision(
                    observation=observation,
                    available=available,
                    cat_decision=cat_decision,
                    parent_decision=decision,
                    executed_before=executed_before,
                    prior_last_gcd=prior_last_gcd,
                    tracks_last_gcd=tracks_last_gcd,
                )
            if not row.legal:
                decision = self._fallback(
                    observation,
                    cat_decision,
                    "RESIDUAL_ACTION_ILLEGAL",
                    step=step,
                    action=action.to_wire(),
                )
                return self._finish_decision(
                    observation=observation,
                    available=available,
                    cat_decision=cat_decision,
                    parent_decision=decision,
                    executed_before=executed_before,
                    prior_last_gcd=prior_last_gcd,
                    tracks_last_gcd=tracks_last_gcd,
                )
            if row.ready_in_ms != 0:
                decision = self._fallback(
                    observation,
                    cat_decision,
                    "RESIDUAL_ACTION_NOT_READY",
                    step=step,
                    action=action.to_wire(),
                    ready_in_ms=row.ready_in_ms,
                )
                return self._finish_decision(
                    observation=observation,
                    available=available,
                    cat_decision=cat_decision,
                    parent_decision=decision,
                    executed_before=executed_before,
                    prior_last_gcd=prior_last_gcd,
                    tracks_last_gcd=tracks_last_gcd,
                )

        key = (step.wave_id, step.step_id)
        self._executed.add(key)
        self._execution_order.append(key)
        if tracks_last_gcd:
            # Cat has already written the GCD it proposed.  Undo only that
            # eager continuation update.  At the next epoch, submit the GCD
            # that actually executed.  A WAIT/no-GCD replacement preserves
            # the prior value and has nothing to submit.
            setattr(self._cat_resolver, "last_gcd_action", prior_last_gcd)
            if step.decision.gcd_action is not None:
                self._pending_actual_gcd_key = _CAT_ACTION_KEY_BY_REF[
                    step.decision.gcd_action
                ]
        self._record(
            "CAT_RELATIVE_RESIDUAL_SELECTED",
            observation,
            wave_id=step.wave_id,
            step_id=step.step_id,
        )
        return self._finish_decision(
            observation=observation,
            available=available,
            cat_decision=cat_decision,
            parent_decision=step.decision,
            executed_before=executed_before,
            prior_last_gcd=prior_last_gcd,
            tracks_last_gcd=tracks_last_gcd,
        )


def build_two_wave_cat_residual_sequence_runtime_v1(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    *,
    cat_resolver_factory: ReactiveResolverFactoryV1,
) -> tuple[CausalActionProgramV1, ImportedReactiveProgramBindingV1]:
    """Bind the frozen residual sequence to one fresh Cat session per replay."""

    if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
        raise TypeError(
            "policy must be DevelopmentTwoWaveCatResidualSequenceV1"
        )
    if not callable(cat_resolver_factory):
        raise TypeError("cat_resolver_factory must be callable")
    binding_id = (
        "development-two-wave-cat-residual-sequence::"
        f"{policy.policy_id}::{policy.exact_build_id}"
    )

    def open_session() -> DevelopmentTwoWaveCatResidualSequenceSessionV1:
        return DevelopmentTwoWaveCatResidualSequenceSessionV1(
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
            f"exact-build:{policy.exact_build_id}",
            (
                ZERO_RESIDUAL_SOURCE_REF_V1
                if not policy.steps
                else NONZERO_RESIDUAL_SOURCE_REF_V1
            ),
        ),
    )
    return program, binding


__all__ = (
    "SCHEMA",
    "SCOPE",
    "NONZERO_RESIDUAL_SOURCE_REF_V1",
    "ZERO_RESIDUAL_SOURCE_REF_V1",
    "CatResidualSequenceDecisionTraceV1",
    "CatResidualSequenceStepV1",
    "CatResidualSequenceWaveV1",
    "DevelopmentTwoWaveCatResidualSequenceSessionV1",
    "DevelopmentTwoWaveCatResidualSequenceV1",
    "DevelopmentTwoWaveCatResidualSequenceV1Error",
    "build_two_wave_cat_residual_sequence_runtime_v1",
    "freeze_two_wave_cat_residual_sequence_v1",
    "frozen_two_wave_cat_residual_sequence_wire_v1",
    "two_wave_cat_residual_sequence_from_dict_v1",
)
