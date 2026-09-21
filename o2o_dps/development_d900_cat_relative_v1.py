"""One-wave Cat-relative development replay for the exact d900 trash pull.

The candidate sees only the current policy projection and native ready actions.
The existing route-focused bridge remains the final authority for target writes.
This is a smoke/search entry, not a comparison-authorized campaign.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from .causal_guard_v1 import (
    ObservableCausalGuardV1,
    SKIP_PLAN,
    evaluate_observable_guard_v1,
)
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .responsive_action_program_replay_v1 import NativeDynamicV4ResponsiveActionProgramReplayV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_cat_action_plan_teacher_v8 import (
    CatDecisionPointV8,
    _ACTION_KEY_BY_REF,
    enumerate_cat_relative_action_plans_v8,
)
from .upper_kara_development_route_focus_v1 import (
    DoomguardCurrentStateRouteFocusedBridgeV1,
    ORDERED_GUIDS,
)
from .upper_kara_imported_incumbent_program_v1 import OBSERVATION_CONTRACT_ID_V1


class D900CatRelativeError(ValueError):
    """A candidate conflicts with the current d900 target/action contract."""


@dataclass(frozen=True)
class D900CatRelativeCandidateV1:
    candidate_id: str
    guard: ObservableCausalGuardV1
    replacement: ProgramDecisionV1
    cat_gcd_action_is: ActionRef | None = None
    decision_index_is: int | None = None
    cat_decision_is: ProgramDecisionV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id must be nonempty")
        if not isinstance(self.guard, ObservableCausalGuardV1) or self.guard.false_semantics != SKIP_PLAN:
            raise ValueError("candidate guard must use causal SKIP_PLAN semantics")
        if not isinstance(self.replacement, ProgramDecisionV1):
            raise TypeError("replacement must be ProgramDecisionV1")
        if self.cat_gcd_action_is is not None and not isinstance(self.cat_gcd_action_is, ActionRef):
            raise TypeError("cat_gcd_action_is must be ActionRef")
        if self.decision_index_is is not None and (
            isinstance(self.decision_index_is, bool)
            or not isinstance(self.decision_index_is, int)
            or self.decision_index_is < 0
        ):
            raise ValueError("decision_index_is must be nonnegative")
        if self.cat_decision_is is not None and not isinstance(self.cat_decision_is, ProgramDecisionV1):
            raise TypeError("cat_decision_is must be ProgramDecisionV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "guard": self.guard.to_dict(),
            "replacement": self.replacement.to_dict(),
            "cat_gcd_action_is": (
                self.cat_gcd_action_is.to_wire() if self.cat_gcd_action_is is not None else None
            ),
            "decision_index_is": self.decision_index_is,
            "cat_decision_is": self.cat_decision_is.to_dict() if self.cat_decision_is is not None else None,
        }


def _route_focus_policy_index(observation: CausalLiveStateProjectionV1) -> int | None:
    mapping = observation.policy_to_simulator_target_index
    if len(set(mapping)) != len(mapping) or any(index not in (0, 1, 2) for index in mapping):
        raise D900CatRelativeError("policy target mapping differs from d900's three-target registry")
    semantics = observation.state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, dict) else None
    if not isinstance(rows, list) or len(rows) != len(mapping):
        raise D900CatRelativeError("current prefix lacks the visible d900 target rows")
    attackable = []
    for policy_index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("target_index") != policy_index:
            raise D900CatRelativeError("current prefix target indexes differ")
        if row.get("attackable") is True and row.get("dead") is False:
            attackable.append((mapping[policy_index], policy_index))
    return min(attackable)[1] if attackable else None


def enumerate_d900_cat_relative_action_plans_v1(
    point: CatDecisionPointV8,
) -> tuple[ProgramDecisionV1, ...]:
    """Reuse V8 ready-action expansion, then apply the single-wave route lock."""

    focus = _route_focus_policy_index(point.observation)
    if focus is None or point.observation.state.get("target_index") != focus:
        return ()
    return tuple(
        decision
        for decision in enumerate_cat_relative_action_plans_v8(point)
        if decision.target_index is None or decision.target_index == focus
    )


class D900CatRelativeSessionV1:
    """At most one guarded replacement, then the same Cat session resumes."""

    def __init__(self, cat_session: Callable[..., ProgramDecisionV1], candidate: D900CatRelativeCandidateV1) -> None:
        if not callable(cat_session) or not isinstance(candidate, D900CatRelativeCandidateV1):
            raise TypeError("Cat session and candidate are required")
        self.cat_session = cat_session
        self.candidate = candidate
        self.interventions: list[dict[str, Any]] = []
        self.decision_count = 0
        self.matched_baseline_count = 0
        self.ineligible_count = 0
        self._pending_last_gcd_action: str | None = None

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if self._pending_last_gcd_action is not None:
            self.cat_session.last_gcd_action = self._pending_last_gcd_action
            self._pending_last_gcd_action = None
        prior_last_gcd = getattr(self.cat_session, "last_gcd_action", "")
        cat_decision = self.cat_session(observation, available)
        decision_index = self.decision_count
        self.decision_count += 1
        if self.interventions or (
            self.candidate.decision_index_is is not None
            and decision_index != self.candidate.decision_index_is
        ) or (
            self.candidate.cat_decision_is is not None
            and cat_decision != self.candidate.cat_decision_is
        ) or (
            self.candidate.cat_gcd_action_is is not None
            and cat_decision.gcd_action != self.candidate.cat_gcd_action_is
        ):
            return cat_decision
        self.matched_baseline_count += 1
        if not evaluate_observable_guard_v1(
            self.candidate.guard, observation.state, available
        ).satisfied:
            self.ineligible_count += 1
            return cat_decision
        focus = _route_focus_policy_index(observation)
        replacement = self.candidate.replacement
        if replacement.target_index is not None and replacement.target_index != focus:
            raise D900CatRelativeError("candidate explicitly targets outside current route focus")
        if focus is None or observation.state.get("target_index") != focus:
            self.ineligible_count += 1
            return cat_decision
        point = CatDecisionPointV8(decision_index, observation, available, cat_decision)
        if replacement not in enumerate_d900_cat_relative_action_plans_v1(point):
            self.ineligible_count += 1
            return cat_decision
        if hasattr(self.cat_session, "last_gcd_action"):
            if replacement.gcd_action is None:
                continuation_key = prior_last_gcd
            else:
                continuation_key = _ACTION_KEY_BY_REF.get(replacement.gcd_action)
                if continuation_key is None:
                    raise D900CatRelativeError("replacement GCD lacks Cat continuation mapping")
            self.cat_session.last_gcd_action = prior_last_gcd
            self._pending_last_gcd_action = continuation_key
        self.interventions.append({
            "candidate_id": self.candidate.candidate_id,
            "decision_index": decision_index,
            "time_ms": observation.visibility_cutoff_ms,
            "replacement": replacement.to_dict(),
        })
        return replacement


def bind_d900_cat_relative_v1(
    cat_binding: ImportedReactiveProgramBindingV1,
    candidate: D900CatRelativeCandidateV1 | None,
    *,
    opened_sessions: list[D900CatRelativeSessionV1] | None = None,
) -> tuple[ImportedReactiveProgramBindingV1, CausalActionProgramV1]:
    """None is the identical imported Cat lane used by the d900 full-wave runner."""

    if cat_binding.observation_contract_id != OBSERVATION_CONTRACT_ID_V1:
        raise D900CatRelativeError("Cat observation contract differs from d900")
    if candidate is None:
        binding = cat_binding
        program_id = f"trash-v4-{cat_binding.source_policy_id}-development-probe"
        origin = ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT
    else:
        def open_candidate_session() -> D900CatRelativeSessionV1:
            session = D900CatRelativeSessionV1(cat_binding.open_session(), candidate)
            if opened_sessions is not None:
                opened_sessions.append(session)
            return session

        binding = ImportedReactiveProgramBindingV1(
            binding_id=f"d900-cat-relative::{candidate.candidate_id}",
            source_policy_id=f"d900.cat-relative::{candidate.candidate_id}",
            observation_contract_id=cat_binding.observation_contract_id,
            resolver_factory=open_candidate_session,
        )
        program_id = binding.binding_id
        origin = ProgramOriginV1.SEARCHED_REACTIVE
    program = CausalActionProgramV1(
        program_id=program_id,
        selector=ImportedReactiveSelectorV1(
            binding.binding_id, binding.source_policy_id, binding.observation_contract_id
        ),
        origin=origin,
        source_refs=(cat_binding.source_policy_id,) if candidate is not None else (),
    )
    return binding, program


def replay_d900_cat_relative_v1(
    *,
    seed: int,
    cat_binding: ImportedReactiveProgramBindingV1,
    candidate: D900CatRelativeCandidateV1 | None,
    driven_bridge_factory: Callable[[], Any],
    case_factory: Callable[[int], Any],
    observation_projector_factory: Callable[[Any], Any],
    max_decisions: int = 10_000,
    opened_sessions: list[D900CatRelativeSessionV1] | None = None,
) -> Any:
    """Run one local smoke lane; caller supplies the frozen responsive model."""

    binding, program = bind_d900_cat_relative_v1(
        cat_binding, candidate, opened_sessions=opened_sessions
    )

    def d900_case(current_seed: int) -> Any:
        case = case_factory(current_seed)
        if tuple(case.native_target_guids) != ORDERED_GUIDS:
            raise D900CatRelativeError("case differs from exact d900 target registry")
        return case

    replay = NativeDynamicV4ResponsiveActionProgramReplayV1(
        bridge_factory=lambda: DoomguardCurrentStateRouteFocusedBridgeV1(
            driven_bridge_factory(), ORDERED_GUIDS
        ),
        case_factory=d900_case,
        observation_projector_factory=observation_projector_factory,
        imported_bindings=(binding,),
    )
    return replay.replay(seed, program, max_decisions=max_decisions)


__all__ = [
    "D900CatRelativeCandidateV1",
    "D900CatRelativeError",
    "D900CatRelativeSessionV1",
    "bind_d900_cat_relative_v1",
    "enumerate_d900_cat_relative_action_plans_v1",
    "replay_d900_cat_relative_v1",
]
