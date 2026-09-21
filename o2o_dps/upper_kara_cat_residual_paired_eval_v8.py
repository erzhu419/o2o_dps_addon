"""Paired native evaluator for a frozen v8 Cat-relative residual sequence.

Both lanes use the same heterogeneous two-wave case and simulator seed.  The
exact-Cat and residual lanes nevertheless open independent native bridges,
causal observation projectors and Cat resolver sessions.  A lane is scoreable
only after every required target is dead; partial or invalid runs never yield
a damage delta.

The evaluator is development-only.  It does not select a policy or aggregate
across seeds.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from .development_two_wave_cat_residual_sequence_v1 import (
    DevelopmentTwoWaveCatResidualSequenceSessionV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import AvailableAction
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_heterogeneous_two_wave_remote_v7 import (
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from .upper_kara_heterogeneous_two_wave_case_v1 import (
    build_heterogeneous_two_wave_observation_projector_v1,
)
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
    build_imported_incumbent_bindings_v1,
)
from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_residual_paired_eval/v8"

# These are control-plane or suffix fields, not current policy observations.
# Exact key matching permits ordinary causal fields such as current visible
# target semantics and pull-relative time.
_FORBIDDEN_POLICY_KEYS = frozenset(
    {
        "arrival_ms",
        "dynamic_load",
        "environment_registry",
        "future_events",
        "future_schedule",
        "future_target_rows",
        "required_target_indices",
        "seed",
        "simulator_seed",
        "source_instance_id",
        "source_wave_ref",
        "target_introduction_registry_control_plane_only",
    }
)


class UpperKaraCatResidualPairedEvalV8Error(RuntimeError):
    """The paired evaluator could not establish its comparison contract."""


def _forbidden_paths(value: object, prefix: str = "state") -> tuple[str, ...]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            label = str(key)
            path = f"{prefix}.{label}"
            if label in _FORBIDDEN_POLICY_KEYS:
                result.append(path)
            result.extend(_forbidden_paths(child, path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            result.extend(_forbidden_paths(child, f"{prefix}[{index}]"))
    return tuple(result)


class _PolicyInputAuditResolverV8:
    """Reject suffix/control-plane fields before a policy resolver sees them."""

    def __init__(self, resolver: Callable[..., ProgramDecisionV1]) -> None:
        self._resolver = resolver
        self.decision_count = 0
        self.forbidden_paths_seen: list[str] = []

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        leaked = _forbidden_paths(observation.state)
        if leaked:
            self.forbidden_paths_seen.extend(leaked)
            raise UpperKaraCatResidualPairedEvalV8Error(
                "policy observation exposes forbidden control-plane fields: "
                + ", ".join(leaked)
            )
        self.decision_count += 1
        return self._resolver(observation, available)


def _required_targets_dead(case: Any, state: Mapping[str, Any]) -> bool:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required = case.case_spec.get("required_target_indices")
    return bool(
        isinstance(targets, list)
        and isinstance(required, list)
        and required
        and all(
            type(index) is int
            and 0 <= index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("target_index") == index
            and targets[index].get("dead") is True
            for index in required
        )
    )


def _terminal(
    case: Any,
    outcome: ScheduleReplayOutcomeV1,
) -> JSONMap:
    required_dead = _required_targets_dead(case, outcome.state)
    if outcome.status is ReplayStatusV1.INVALID:
        return {
            "status": "INVALID_REPLAY",
            "replay_status": outcome.status.value,
            "own_effective_damage": None,
            "elapsed_ms": None,
            "required_targets_dead": required_dead,
            "invalid_reason": outcome.invalid_reason,
        }
    if outcome.status is not ReplayStatusV1.COMPLETE or not required_dead:
        return {
            "status": "INCOMPLETE_REQUIRED_TARGETS",
            "replay_status": outcome.status.value,
            "own_effective_damage": None,
            "elapsed_ms": None,
            "required_targets_dead": required_dead,
            "invalid_reason": (
                outcome.invalid_reason
                or "native replay did not kill every required target"
            ),
        }
    try:
        damage = outcome.effective_damage
        elapsed_ms = outcome.elapsed_ms
    except (TypeError, ValueError) as error:
        return {
            "status": "INVALID_TERMINAL_METRICS",
            "replay_status": outcome.status.value,
            "own_effective_damage": None,
            "elapsed_ms": None,
            "required_targets_dead": True,
            "invalid_reason": f"{type(error).__name__}: {error}",
        }
    return {
        "status": "COMPLETED",
        "replay_status": outcome.status.value,
        "own_effective_damage": damage,
        "elapsed_ms": elapsed_ms,
        "required_targets_dead": True,
        "invalid_reason": None,
    }


def _exact_cat_program() -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id="exact-cat::paired-residual-eval-v8",
        selector=ImportedReactiveSelectorV1(
            binding_id=CAT_POLICY_ID,
            source_policy_id=CAT_POLICY_ID,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
        source_refs=(CAT_POLICY_ID, SCHEMA),
    )


def _step_audit(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    sessions: list[DevelopmentTwoWaveCatResidualSequenceSessionV1],
) -> JSONMap:
    if len(sessions) != 1:
        return {
            "session_open_count": len(sessions),
            "executed_step_keys": [],
            "steps": [
                {
                    "wave_id": step.wave_id,
                    "step_id": step.step_id,
                    "executed": False,
                }
                for step in policy.steps
            ],
            "runtime_event_count": 0,
            "runtime_events": [],
            "fallback_reason_counts": {},
        }
    session = sessions[0]
    executed = set(session.executed_step_keys)
    events = [deepcopy(dict(row)) for row in session.audit_events]
    fallback_counts = Counter(
        str(row.get("reason"))
        for row in events
        if row.get("kind") == "EXACT_CAT_FALLBACK"
    )
    return {
        "session_open_count": 1,
        "executed_step_keys": [list(row) for row in session.executed_step_keys],
        "steps": [
            {
                "wave_id": step.wave_id,
                "step_id": step.step_id,
                "executed": (step.wave_id, step.step_id) in executed,
            }
            for step in policy.steps
        ],
        "runtime_event_count": len(events),
        "runtime_events": events,
        "fallback_reason_counts": dict(sorted(fallback_counts.items())),
    }


def evaluate_upper_kara_cat_residual_sequence_paired_v8(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    *,
    seed: int,
    build_id: str,
    loadout_id: str,
    first_wave_arrival_ms: int = 0,
    pull_time_ms: int = 3_000,
    max_decisions: int = 10_000,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    bridge_factory: Callable[[], Any] | None = None,
) -> JSONMap:
    """Evaluate exact Cat and one frozen residual on a paired native case."""

    if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
        raise TypeError(
            "policy must be DevelopmentTwoWaveCatResidualSequenceV1"
        )
    if policy.exact_build_id != build_id:
        raise ValueError(
            "policy exact_build_id differs from the requested exact build"
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if isinstance(max_decisions, bool) or not isinstance(max_decisions, int):
        raise TypeError("max_decisions must be an integer")
    if max_decisions < 1:
        raise ValueError("max_decisions must be positive")

    case = build_upper_kara_heterogeneous_two_wave_burst_case_v7(
        seed,
        build_id=build_id,
        loadout_id=loadout_id,
        pull_time_ms=pull_time_ms,
        first_wave_arrival_ms=first_wave_arrival_ms,
    )
    if case.dynamic_load.seed != seed:
        raise UpperKaraCatResidualPairedEvalV8Error(
            "case builder returned a different simulator seed"
        )
    required = case.case_spec.get("required_target_indices")
    residual_targets = tuple(
        target for wave in policy.waves for target in wave.target_indexes
    )
    if not isinstance(required, list) or set(residual_targets) != set(required):
        raise ValueError(
            "residual wave targets differ from case required target indices"
        )

    source_bindings = build_imported_incumbent_bindings_v1(
        build_id,
        case,
        runtime_binding_path=runtime_binding_path,
    )
    cat_sources = [
        row for row in source_bindings if row.source_policy_id == CAT_POLICY_ID
    ]
    if len(cat_sources) != 1:
        raise UpperKaraCatResidualPairedEvalV8Error(
            "exactly one Cat source binding is required"
        )
    cat_source = cat_sources[0]

    baseline_cat_sessions: list[Callable[..., ProgramDecisionV1]] = []
    candidate_cat_sessions: list[Callable[..., ProgramDecisionV1]] = []
    baseline_input_audits: list[_PolicyInputAuditResolverV8] = []
    candidate_input_audits: list[_PolicyInputAuditResolverV8] = []
    residual_sessions: list[DevelopmentTwoWaveCatResidualSequenceSessionV1] = []

    def open_baseline_cat() -> _PolicyInputAuditResolverV8:
        raw = cat_source.open_session()
        baseline_cat_sessions.append(raw)
        audited = _PolicyInputAuditResolverV8(raw)
        baseline_input_audits.append(audited)
        return audited

    baseline_binding = ImportedReactiveProgramBindingV1(
        binding_id=cat_source.binding_id,
        source_policy_id=cat_source.source_policy_id,
        observation_contract_id=cat_source.observation_contract_id,
        resolver_factory=open_baseline_cat,
    )

    def open_candidate_cat() -> Callable[..., ProgramDecisionV1]:
        raw = cat_source.open_session()
        candidate_cat_sessions.append(raw)
        return raw

    candidate_program, raw_candidate_binding = (
        build_two_wave_cat_residual_sequence_runtime_v1(
            policy,
            cat_resolver_factory=open_candidate_cat,
        )
    )

    def open_candidate_residual() -> _PolicyInputAuditResolverV8:
        raw = raw_candidate_binding.open_session()
        if not isinstance(raw, DevelopmentTwoWaveCatResidualSequenceSessionV1):
            raise TypeError("residual runtime opened an unexpected session type")
        residual_sessions.append(raw)
        audited = _PolicyInputAuditResolverV8(raw)
        candidate_input_audits.append(audited)
        return audited

    candidate_binding = ImportedReactiveProgramBindingV1(
        binding_id=raw_candidate_binding.binding_id,
        source_policy_id=raw_candidate_binding.source_policy_id,
        observation_contract_id=raw_candidate_binding.observation_contract_id,
        resolver_factory=open_candidate_residual,
    )

    resolved_bridge_path = Path(bridge_path).expanduser().resolve()
    resolved_bridge_cwd = Path(bridge_cwd).expanduser().resolve()
    raw_open_bridge = bridge_factory or (
        lambda: SimulatorBridgePrecombatV1(
            resolved_bridge_path,
            cwd=resolved_bridge_cwd,
        )
    )
    opened_bridges: list[Any] = []

    def open_bridge() -> Any:
        bridge = raw_open_bridge()
        opened_bridges.append(bridge)
        return bridge

    def run_lane(
        program: CausalActionProgramV1,
        binding: ImportedReactiveProgramBindingV1,
    ) -> ScheduleReplayOutcomeV1:
        # Projector state is prefix-dependent, so every lane gets a fresh one.
        replay = NativeDynamicV3ActionProgramReplayV1(
            open_bridge,
            lambda requested_seed: {seed: case}[requested_seed],
            build_heterogeneous_two_wave_observation_projector_v1(case),
            imported_bindings=(binding,),
        )
        return replay.replay(seed, program, max_decisions=max_decisions)

    exact_outcome = run_lane(_exact_cat_program(), baseline_binding)
    residual_outcome = run_lane(candidate_program, candidate_binding)
    exact_terminal = _terminal(case, exact_outcome)
    residual_terminal = _terminal(case, residual_outcome)

    fresh_bridges = (
        len(opened_bridges) == 2 and opened_bridges[0] is not opened_bridges[1]
    )
    fresh_cat_sessions = (
        len(baseline_cat_sessions) == 1
        and len(candidate_cat_sessions) == 1
        and baseline_cat_sessions[0] is not candidate_cat_sessions[0]
    )
    input_audits = (*baseline_input_audits, *candidate_input_audits)
    input_clean = bool(input_audits) and all(
        not row.forbidden_paths_seen for row in input_audits
    )
    both_complete = (
        exact_terminal["status"] == "COMPLETED"
        and residual_terminal["status"] == "COMPLETED"
    )
    comparison_valid = bool(
        both_complete and fresh_bridges and fresh_cat_sessions and input_clean
    )
    delta = (
        residual_terminal["own_effective_damage"]
        - exact_terminal["own_effective_damage"]
        if comparison_valid
        else None
    )

    return {
        "schema": SCHEMA,
        "status": (
            "COMPLETED_PAIRED_EVALUATION"
            if comparison_valid
            else "INVALID_OR_INCOMPLETE_PAIRED_EVALUATION"
        ),
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": seed,
        "build_id": build_id,
        "loadout_id": loadout_id,
        "first_wave_arrival_ms": first_wave_arrival_ms,
        "policy_id": policy.policy_id,
        "same_case_and_seed": True,
        "fresh_native_bridge_instances_verified": fresh_bridges,
        "fresh_cat_sessions_verified": fresh_cat_sessions,
        "policy_input_contract": (
            "CURRENT_CAUSAL_LIVE_STATE_PROJECTION_V1_ONLY;"
            "NO_SEED_OR_FUTURE_ENVIRONMENT_REGISTRY"
        ),
        "policy_input_audit": {
            "clean": input_clean,
            "exact_cat_session_count": len(baseline_input_audits),
            "residual_session_count": len(candidate_input_audits),
            "exact_cat_decision_count": sum(
                row.decision_count for row in baseline_input_audits
            ),
            "residual_decision_count": sum(
                row.decision_count for row in candidate_input_audits
            ),
            "forbidden_paths_seen": sorted(
                {
                    path
                    for row in input_audits
                    for path in row.forbidden_paths_seen
                }
            ),
        },
        "exact_cat_terminal": exact_terminal,
        "residual_terminal": residual_terminal,
        "paired_residual_minus_cat_own_effective_damage": delta,
        "paired_comparison_valid": comparison_valid,
        "step_audit": _step_audit(policy, residual_sessions),
    }


__all__ = (
    "SCHEMA",
    "UpperKaraCatResidualPairedEvalV8Error",
    "evaluate_upper_kara_cat_residual_sequence_paired_v8",
)
