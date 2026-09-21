"""Fresh native replay for frozen Cat-relative two-wave residual sequences.

The frozen candidate stores only a searched-reactive resolver identity.  This
module reconstructs that identity from the frozen residual registry for every
``(program, seed)`` replay, using a fresh heterogeneous observation projector
and a fresh exact-Cat source session.  Stateful residual latches and Cat
continuation state therefore cannot leak between seeds or candidates.
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
from .development_two_wave_cat_residual_sequence_v1 import (
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
    frozen_two_wave_cat_residual_sequence_wire_v1,
    two_wave_cat_residual_sequence_from_dict_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_heterogeneous_two_wave_case_v1 import (
    build_heterogeneous_two_wave_observation_projector_v1,
)
from .upper_kara_imported_incumbent_program_v1 import (
    _required_imported_binding_ids_v1,
    build_imported_incumbent_bindings_v1,
)


JSONMap = dict[str, Any]


class UpperKaraTwoWaveCatResidualSequenceReplayV1Error(RuntimeError):
    """A frozen residual identity cannot be rebuilt exactly."""


def _canonical_policy_wire_v1(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
) -> JSONMap:
    """Validate that the in-memory policy is its own closed-wire identity."""

    wire = frozen_two_wave_cat_residual_sequence_wire_v1(policy)
    try:
        restored = two_wave_cat_residual_sequence_from_dict_v1(deepcopy(wire))
    except (TypeError, ValueError) as error:
        raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
            "frozen residual policy does not satisfy its wire contract"
        ) from error
    if restored != policy or restored.to_dict() != wire:
        raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
            "frozen residual policy differs after canonical wire round-trip"
        )
    return wire


class _FreshTwoWaveCatResidualSequenceReplayV1:
    def __init__(
        self,
        *,
        build_id: str,
        policies_by_program_id: Mapping[
            str, DevelopmentTwoWaveCatResidualSequenceV1
        ],
        policy_wires_by_program_id: Mapping[str, Mapping[str, Any]],
        cases_by_simulator: Mapping[int, DevelopmentPrecombatWaveCaseV1],
        bridge_path: Path,
        bridge_cwd: Path,
        runtime_binding_path: Path,
        observation_projector_factory: Callable[[Any], Any],
    ) -> None:
        self._build_id = build_id
        self._policies = dict(policies_by_program_id)
        self._policy_wires = {
            key: deepcopy(dict(value))
            for key, value in policy_wires_by_program_id.items()
        }
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

    def _residual_policy(
        self, program: CausalActionProgramV1
    ) -> DevelopmentTwoWaveCatResidualSequenceV1:
        if not isinstance(program.selector, ImportedReactiveSelectorV1):
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "searched-reactive residual replay requires an "
                "ImportedReactiveSelectorV1"
            )
        try:
            policy = self._policies[program.program_id]
            expected_wire = self._policy_wires[program.program_id]
        except KeyError as error:
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                f"frozen residual policy is absent for {program.program_id!r}"
            ) from error
        if policy.policy_id != program.program_id:
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "frozen residual policy ID differs from program ID"
            )
        if policy.exact_build_id != self._build_id:
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "frozen residual policy build differs from replay build"
            )
        if frozen_two_wave_cat_residual_sequence_wire_v1(policy) != expected_wire:
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "frozen residual wire identity changed after registration"
            )
        return policy

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
                raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                    f"program requires unavailable imported bindings {missing!r}"
                )
            return tuple(by_id[binding_id] for binding_id in required_imported_ids)

        policy = self._residual_policy(program)
        cat_candidates = tuple(
            row for row in imported if row.source_policy_id == CAT_POLICY_ID
        )
        if len(cat_candidates) != 1:
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "exactly one Cat binding is required for residual fallback"
            )
        rebuilt_program, residual_binding = (
            build_two_wave_cat_residual_sequence_runtime_v1(
                policy,
                cat_resolver_factory=cat_candidates[0].open_session,
            )
        )
        if rebuilt_program.program_key() != program.program_key():
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "frozen residual program identity differs from rebuilt policy"
            )
        return (residual_binding,)

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
                f"no heterogeneous two-wave case is bound to simulator seed {seed}"
            ) from error
        if source_case.case_spec.get("build_id") != self._build_id:
            raise UpperKaraTwoWaveCatResidualSequenceReplayV1Error(
                "case build identity differs from replay build"
            )
        case = replace(
            source_case,
            dynamic_load=DynamicRolloutLoadV3.bind(
                source_case.request,
                seed,
                source_case.dynamic_load.config,
            ),
        )
        projector = self._observation_projector_factory(case)
        if not callable(projector):
            raise TypeError("observation_projector_factory returned a non-callable")
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
                "projector": "HETEROGENEOUS_TWO_WAVE",
                "captured_target_indexes": list(
                    getattr(projector, "captured_target_indexes", ())
                ),
                "hidden_selected_target_substitutions": getattr(
                    projector, "hidden_selected_target_substitutions", None
                ),
                "last_hidden_selected_target_index": getattr(
                    projector, "last_hidden_selected_target_index", None
                ),
            }


def build_two_wave_cat_residual_sequence_program_replay_factory_v1(
    build_id: str,
    policies_by_program_id: Mapping[
        str, DevelopmentTwoWaveCatResidualSequenceV1
    ],
    *,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    observation_projector_factory: Callable[[Any], Any] = (
        build_heterogeneous_two_wave_observation_projector_v1
    ),
) -> Callable[[str, Mapping[int, Any]], _FreshTwoWaveCatResidualSequenceReplayV1]:
    """Build a train/eval factory for one frozen residual policy registry."""

    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    policies = dict(policies_by_program_id)
    if not policies:
        raise ValueError("policies_by_program_id must be nonempty")
    policy_wires: dict[str, JSONMap] = {}
    for program_id, policy in policies.items():
        if (
            not isinstance(program_id, str)
            or not program_id
            or not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1)
            or policy.policy_id != program_id
            or policy.exact_build_id != build_id
        ):
            raise ValueError(
                "policies_by_program_id must be a nonempty exact-build residual "
                "policy registry keyed by policy_id"
            )
        policy_wires[program_id] = _canonical_policy_wire_v1(policy)

    resolved_bridge = Path(bridge_path).expanduser().resolve()
    resolved_cwd = Path(bridge_cwd).expanduser().resolve()
    resolved_binding = Path(runtime_binding_path).expanduser().resolve()
    if not callable(observation_projector_factory):
        raise TypeError("observation_projector_factory must be callable")

    def factory(
        loadout_id: str,
        cases_by_simulator: Mapping[int, Any],
    ) -> _FreshTwoWaveCatResidualSequenceReplayV1:
        if not isinstance(loadout_id, str) or not loadout_id:
            raise TypeError("loadout_id must be nonempty text")
        cases = dict(cases_by_simulator)
        if not cases or any(
            not isinstance(case, DevelopmentPrecombatWaveCaseV1)
            for case in cases.values()
        ):
            raise TypeError(
                "cases_by_simulator must contain "
                "DevelopmentPrecombatWaveCaseV1 values"
            )
        return _FreshTwoWaveCatResidualSequenceReplayV1(
            build_id=build_id,
            policies_by_program_id=policies,
            policy_wires_by_program_id=policy_wires,
            cases_by_simulator=cases,
            bridge_path=resolved_bridge,
            bridge_cwd=resolved_cwd,
            runtime_binding_path=resolved_binding,
            observation_projector_factory=observation_projector_factory,
        )

    return factory


__all__ = (
    "UpperKaraTwoWaveCatResidualSequenceReplayV1Error",
    "build_two_wave_cat_residual_sequence_program_replay_factory_v1",
)
