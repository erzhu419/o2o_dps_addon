"""Fresh native replay for frozen searched-reactive two-wave programs.

The wire program records only the stable searched resolver identity.  This
module reconstructs that resolver from the frozen policy registry for each
``(program, seed)`` replay.  The finite-sequence cursor and its exact-Cat
fallback session therefore live for one replay only and cannot leak into the
next seed or candidate.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramOriginV1,
)
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_two_wave_build_panel_v1 import BUILD_IDS
from .development_two_wave_segment_policy_v1 import (
    DevelopmentTwoWaveSegmentPolicyV1,
    build_two_wave_segment_runtime_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_imported_incumbent_program_v1 import (
    _required_imported_binding_ids_v1,
    build_continuous_two_wave_observation_projector_v1,
    build_imported_incumbent_bindings_v1,
)


JSONMap = dict[str, Any]


class UpperKaraTwoWaveSegmentReplayV1Error(RuntimeError):
    """A frozen searched-reactive identity cannot be rebuilt exactly."""


class _FreshTwoWaveSegmentReplayV1:
    def __init__(
        self,
        *,
        build_id: str,
        policies_by_program_id: Mapping[str, DevelopmentTwoWaveSegmentPolicyV1],
        cases_by_simulator: Mapping[int, DevelopmentPrecombatWaveCaseV1],
        bridge_path: Path,
        bridge_cwd: Path,
        runtime_binding_path: Path,
        observation_projector_factory: Callable[[Any], Any],
    ) -> None:
        self._build_id = build_id
        self._policies = dict(policies_by_program_id)
        self._cases = dict(cases_by_simulator)
        self._bridge_path = bridge_path
        self._bridge_cwd = bridge_cwd
        self._runtime_binding_path = runtime_binding_path
        self._observation_projector_factory = observation_projector_factory
        self._last_observation_audit: JSONMap | None = None

    @property
    def last_observation_audit(self) -> JSONMap | None:
        return (
            None
            if self._last_observation_audit is None
            else deepcopy(self._last_observation_audit)
        )

    def _runtime_bindings(
        self,
        program: CausalActionProgramV1,
        case: DevelopmentPrecombatWaveCaseV1,
    ) -> tuple[ImportedReactiveProgramBindingV1, ...]:
        required_imported_ids = _required_imported_binding_ids_v1(program)
        if (
            program.origin is not ProgramOriginV1.SEARCHED_REACTIVE
            and not required_imported_ids
        ):
            return ()
        imported = build_imported_incumbent_bindings_v1(
            self._build_id,
            case,
            runtime_binding_path=self._runtime_binding_path,
        )
        if program.origin is not ProgramOriginV1.SEARCHED_REACTIVE:
            by_id = {row.binding_id: row for row in imported}
            missing = tuple(
                binding_id
                for binding_id in required_imported_ids
                if binding_id not in by_id
            )
            if missing:
                raise UpperKaraTwoWaveSegmentReplayV1Error(
                    f"program requires unavailable imported bindings {missing!r}"
                )
            return tuple(by_id[binding_id] for binding_id in required_imported_ids)

        if not isinstance(program.selector, ImportedReactiveSelectorV1):
            raise UpperKaraTwoWaveSegmentReplayV1Error(
                "searched-reactive replay requires an ImportedReactiveSelectorV1"
            )

        try:
            policy = self._policies[program.program_id]
        except KeyError as error:
            raise UpperKaraTwoWaveSegmentReplayV1Error(
                f"searched-reactive policy is absent for {program.program_id!r}"
            ) from error
        if policy.exact_build_id != self._build_id:
            raise UpperKaraTwoWaveSegmentReplayV1Error(
                "searched-reactive policy build differs from replay build"
            )
        cat_candidates = tuple(
            row for row in imported if row.source_policy_id == CAT_POLICY_ID
        )
        if len(cat_candidates) != 1:
            raise UpperKaraTwoWaveSegmentReplayV1Error(
                "exactly one Cat binding is required for searched fallback"
            )
        rebuilt_program, sequence_binding = build_two_wave_segment_runtime_v1(
            policy,
            cat_resolver_factory=cat_candidates[0].open_session,
        )
        if rebuilt_program.program_key() != program.program_key():
            raise UpperKaraTwoWaveSegmentReplayV1Error(
                "frozen searched-reactive identity differs from rebuilt policy"
            )
        return (sequence_binding,)

    def replay(
        self,
        seed: int,
        program: CausalActionProgramV1,
        *,
        max_decisions: int = 10_000,
    ) -> Any:
        if not isinstance(program, CausalActionProgramV1):
            raise TypeError("program must be CausalActionProgramV1")
        try:
            source_case = self._cases[seed]
        except KeyError as error:
            raise KeyError(
                f"no continuous case is bound to simulator seed {seed}"
            ) from error
        case = replace(
            source_case,
            dynamic_load=DynamicRolloutLoadV3.bind(
                source_case.request,
                seed,
                source_case.dynamic_load.config,
            ),
        )
        projector = self._observation_projector_factory(case)
        bindings = self._runtime_bindings(program, case)
        replay = NativeDynamicV3ActionProgramReplayV1(
            lambda: SimulatorBridgePrecombatV1(
                self._bridge_path, cwd=self._bridge_cwd
            ),
            lambda requested_seed: {seed: case}[requested_seed],
            projector,
            imported_bindings=bindings,
        )
        try:
            return replay.replay(seed, program, max_decisions=max_decisions)
        finally:
            self._last_observation_audit = {
                "simulator_seed": seed,
                "hidden_selected_target_substitutions": (
                    projector.hidden_selected_target_substitutions
                ),
                "last_hidden_selected_target_index": (
                    projector.last_hidden_selected_target_index
                ),
            }


def build_two_wave_segment_program_replay_factory_v1(
    build_id: str,
    policies_by_program_id: Mapping[str, DevelopmentTwoWaveSegmentPolicyV1],
    *,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    observation_projector_factory: Callable[[Any], Any] = (
        build_continuous_two_wave_observation_projector_v1
    ),
) -> Callable[[str, Mapping[int, Any]], _FreshTwoWaveSegmentReplayV1]:
    """Build the train/eval factory for a frozen searched policy registry."""

    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    policies = dict(policies_by_program_id)
    if not policies or any(
        not isinstance(program_id, str)
        or not program_id
        or not isinstance(policy, DevelopmentTwoWaveSegmentPolicyV1)
        or policy.policy_id != program_id
        or policy.exact_build_id != build_id
        for program_id, policy in policies.items()
    ):
        raise ValueError(
            "policies_by_program_id must be a nonempty exact-build policy registry"
        )
    resolved_bridge = Path(bridge_path).expanduser().resolve()
    resolved_cwd = Path(bridge_cwd).expanduser().resolve()
    resolved_binding = Path(runtime_binding_path).expanduser().resolve()
    if not callable(observation_projector_factory):
        raise TypeError("observation_projector_factory must be callable")

    def factory(
        loadout_id: str,
        cases_by_simulator: Mapping[int, Any],
    ) -> _FreshTwoWaveSegmentReplayV1:
        if not isinstance(loadout_id, str) or not loadout_id:
            raise TypeError("loadout_id must be nonempty text")
        cases = dict(cases_by_simulator)
        if not cases or any(
            not isinstance(case, DevelopmentPrecombatWaveCaseV1)
            for case in cases.values()
        ):
            raise TypeError(
                "cases_by_simulator must contain DevelopmentPrecombatWaveCaseV1 values"
            )
        return _FreshTwoWaveSegmentReplayV1(
            build_id=build_id,
            policies_by_program_id=policies,
            cases_by_simulator=cases,
            bridge_path=resolved_bridge,
            bridge_cwd=resolved_cwd,
            runtime_binding_path=resolved_binding,
            observation_projector_factory=observation_projector_factory,
        )

    return factory


__all__ = (
    "UpperKaraTwoWaveSegmentReplayV1Error",
    "build_two_wave_segment_program_replay_factory_v1",
)
