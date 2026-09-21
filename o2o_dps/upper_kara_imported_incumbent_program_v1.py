"""Execute the three existing Fury incumbents as causal action programs.

This module is an integration layer, not another policy translation.  It
reuses each incumbent's existing state mapper, source adapter, proposal
validator, and ordered-sink resolver.  The only new operation is a bounded
projection of the *currently executable* source prefix into
``ProgramDecisionV1`` so imported programs and searched programs can share the
same native dynamic-v3 replay.

The action-program grammar has no CVar lane.  Cat's four Slam CVar calls are
therefore omitted here because the existing native Cat executor records them
as a simulator sidecar with no combat mechanic; the Slam action itself and
all combat-affecting source sinks retain their source order.  Any reached
target-GUID, item, equipment, attack-toggle/stop, or multiple-queue path that
cannot be represented exactly fails closed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import cat_fury_full_policy_rollout_v5 as _cat_rollout
from . import cat_fury_ordered_sink_executor_v5 as _cat_sinks
from . import contra260817_fury_full_policy_rollout_v4 as _contra_rollout
from . import contra260817_fury_ordered_sink_executor_v4 as _contra_sinks
from . import fury_full_policy_rollout_v2 as _deployed_rollout
from . import fury_full_policy_rollout_v3 as _target_semantics
from . import fury_ordered_sink_executor_v2 as _deployed_sinks
from .causal_action_program_v1 import (
    CausalActionProgramV1,
    CausalLiveStateProjectionV1,
    ImportedFallbackOverlaySelectorV1,
    ImportedReactiveBurstQueueGcdBlockSelectorV1,
    ImportedReactiveQueueGcdBlockSelectorV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    OptionalOffGcdPrefixV1,
    ProgramDecisionV1,
    ProgramPrefixOperationKindV1,
)
from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from .contra260817_fury_full_policy_v3 import (
    SOURCE_DEFAULT_PROFILE_V3,
    Contra260817FuryFullPolicyAdapterV3,
)
from .deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_two_wave_build_panel_v1 import BUILD_IDS
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .expert_policy import ExpertDecision, StanceOp, SwingQueueOp, WAIT_ACTION
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_ordered_sink_executor_raid_b_v1 import _project as _raid_b_identity
from .fury_ordered_sink_executor_v4 import _v2_identity as _raid_a_identity
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
)
from .fury_runtime_bound_deployed_contra_adapter_v7 import (
    RAID_A_CONTROLLER,
    RuntimeBoundContraDeployedFuryAdapterV7,
)
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    POLICY_ID as DEPLOYED_RAID_B_POLICY_ID,
    RuntimeBoundContraRaidBAdapterV1,
)
from .policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
    project_live_state_for_policy_v1,
)
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_two_wave_baseline_panel_v1 import (
    baseline_policy_ids_for_build_v1,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
OBSERVATION_CONTRACT_ID_V1 = "policy_observation_causal_projection/v1"
SOURCE_REENTRY_WAIT_MS_V1 = 100


class UpperKaraImportedIncumbentProgramV1Error(RuntimeError):
    """An imported source path cannot be represented without changing it."""


def deployed_controller_for_build_v1(build_id: str) -> str:
    """Return the installed-Contra controller matching the exact weapon mode."""

    if build_id == "live_bonereaver":
        return "raid_a"
    if build_id == "clean_dual_weapon_probe":
        return "raid_b"
    raise ValueError(f"unknown continuous two-wave build {build_id!r}")


def _target_rows(state: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    team = state.get("dynamic_team_background")
    semantics = state.get("dynamic_target_semantics")
    life = team.get("targets") if isinstance(team, Mapping) else None
    semantic = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if (
        not isinstance(life, list)
        or not life
        or any(not isinstance(row, Mapping) for row in life)
        or not isinstance(semantic, list)
        or len(semantic) != len(life)
        or any(not isinstance(row, Mapping) for row in semantic)
    ):
        raise UpperKaraImportedIncumbentProgramV1Error(
            "raw dynamic-v3 state lacks aligned target lifecycle rows"
        )
    return list(life), list(semantic)


def _introduction_registry_v1(
    case: DevelopmentPrecombatWaveCaseV1,
) -> TargetIntroductionRegistryV1:
    target_count = len(case.dynamic_load.config.target_health)
    if target_count <= 0:
        raise UpperKaraImportedIncumbentProgramV1Error(
            "continuous case has no target-health rows"
        )
    first_attackable: dict[int, int] = {}
    for event in case.dynamic_load.config.attackability_events:
        if event.attackable:
            first_attackable.setdefault(event.target_index, event.time_ms)
    rows = [TargetIntroductionV1(0, 0)]
    for target_index in range(1, target_count):
        introduced_at = first_attackable.get(target_index)
        if introduced_at is None:
            raise UpperKaraImportedIncumbentProgramV1Error(
                f"target {target_index} has no prefix-observable introduction"
            )
        rows.append(TargetIntroductionV1(target_index, introduced_at))
    return TargetIntroductionRegistryV1(targets=tuple(rows))


class ContinuousTwoWaveObservationProjectorV1:
    """Capture current/max HP only when each target becomes prefix-visible."""

    def __init__(self, case: DevelopmentPrecombatWaveCaseV1) -> None:
        if not isinstance(case, DevelopmentPrecombatWaveCaseV1):
            raise TypeError("case must be DevelopmentPrecombatWaveCaseV1")
        self._introductions = _introduction_registry_v1(case)
        self._baselines: dict[int, TargetHealthPrefixBaselineV1] = {}
        self._last_time_ms: int | None = None
        self._generation: object = None
        self._hidden_selected_target_substitutions = 0
        self._last_hidden_selected_target_index: int | None = None

    @property
    def target_introduction_registry(self) -> TargetIntroductionRegistryV1:
        return self._introductions

    @property
    def hidden_selected_target_substitutions(self) -> int:
        """Number of raw hidden-target selections causally replaced so far."""

        return self._hidden_selected_target_substitutions

    @property
    def last_hidden_selected_target_index(self) -> int | None:
        return self._last_hidden_selected_target_index

    def _reset_if_new_load(self, state: Mapping[str, Any], now_ms: int) -> None:
        generation = state.get("environment_generation")
        new_generation = (
            self._last_time_ms is not None
            and generation is not None
            and self._generation is not None
            and generation != self._generation
        )
        rewound = self._last_time_ms is not None and now_ms < self._last_time_ms
        if new_generation or rewound:
            self._baselines.clear()
            self._hidden_selected_target_substitutions = 0
            self._last_hidden_selected_target_index = None
        self._generation = generation
        self._last_time_ms = now_ms

    def __call__(
        self,
        state: Mapping[str, Any],
        available_actions: tuple[AvailableAction, ...],
    ) -> CausalLiveStateProjectionV1:
        del available_actions
        now = state.get("time_ms")
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise UpperKaraImportedIncumbentProgramV1Error(
                "raw state time_ms must be a nonnegative integer"
            )
        self._reset_if_new_load(state, now)
        life_rows, semantic_rows = _target_rows(state)
        introduction_by_index = {
            row.simulator_target_index: row.introduced_at_ms
            for row in self._introductions.targets
        }
        if set(introduction_by_index) != set(range(len(life_rows))):
            raise UpperKaraImportedIncumbentProgramV1Error(
                "target introduction registry differs from the loaded case"
            )

        for target_index, introduced_at in introduction_by_index.items():
            if target_index in self._baselines or introduced_at > now:
                continue
            life = life_rows[target_index]
            semantic = semantic_rows[target_index]
            current = life.get("current_health")
            # Dynamic-v3 exposes the selected target's max at the root and
            # every target's bound maximum as lifecycle.initial_health.  Read
            # the latter only after this target is prefix-visible.
            maximum = semantic.get("maximum_health", life.get("initial_health"))
            simulated = life.get("simulated_damage_applied")
            background = life.get("background_damage_applied")
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (current, maximum, simulated, background)
            ):
                raise UpperKaraImportedIncumbentProgramV1Error(
                    f"target {target_index} lacks numeric prefix HP counters"
                )
            self._baselines[target_index] = TargetHealthPrefixBaselineV1(
                simulator_target_index=target_index,
                observed_at_ms=now,
                current_health=float(current),
                maximum_health=float(maximum),
                simulated_damage_applied_at_observation=float(simulated),
                background_damage_applied_at_observation=float(background),
            )

        # The causal projector requires exact index coverage.  Invisible rows
        # receive inert placeholders that are replaced from the live prefix
        # before their introduction time is reached; their values are never
        # exposed to the policy.
        health_rows = tuple(
            self._baselines.get(
                target_index,
                TargetHealthPrefixBaselineV1(
                    simulator_target_index=target_index,
                    observed_at_ms=introduction_by_index[target_index],
                    current_health=1.0,
                    maximum_health=1.0,
                    simulated_damage_applied_at_observation=0.0,
                    background_damage_applied_at_observation=0.0,
                ),
            )
            for target_index in range(len(life_rows))
        )
        visible_indexes = tuple(
            target_index
            for target_index, introduced_at in introduction_by_index.items()
            if introduced_at <= now
        )
        selected = state.get("target_index")
        projected_source: Mapping[str, Any] = state
        if selected not in visible_indexes:
            # Dynamic-v3 may point at a future target while every target is
            # unattackable during precombat.  That simulator-internal choice is
            # not policy-observable, so bind the policy view to the first
            # prefix-visible target without copying any hidden target values.
            projected_source = deepcopy(state)
            projected_source["target_index"] = visible_indexes[0]
            self._hidden_selected_target_substitutions += 1
            self._last_hidden_selected_target_index = (
                selected if isinstance(selected, int) and not isinstance(selected, bool) else None
            )
        return project_live_state_for_policy_v1(
            projected_source,
            self._introductions,
            TargetHealthPrefixRegistryV1(targets=health_rows),
        )


def build_continuous_two_wave_observation_projector_v1(
    case: DevelopmentPrecombatWaveCaseV1,
) -> ContinuousTwoWaveObservationProjectorV1:
    """Build a fresh, per-replay projector for one exact continuous case."""

    return ContinuousTwoWaveObservationProjectorV1(case)


def continuous_two_wave_observation_projector_factory_v1(
    loadout_id: str,
    cases_by_simulator: Mapping[int, DevelopmentPrecombatWaveCaseV1],
) -> ContinuousTwoWaveObservationProjectorV1:
    """Driver-compatible projector factory for structurally identical cases.

    Native integrated replay below creates a fresh projector for every replay.
    This factory remains useful to callers of the generic native factory, but
    it deliberately rejects a mixed introduction schedule.
    """

    if not isinstance(loadout_id, str) or not loadout_id:
        raise TypeError("loadout_id must be nonempty text")
    cases = tuple(cases_by_simulator.values())
    if not cases:
        raise ValueError("cases_by_simulator must not be empty")
    signatures = {
        tuple(
            (row.simulator_target_index, row.introduced_at_ms)
            for row in _introduction_registry_v1(case).targets
        )
        for case in cases
    }
    if len(signatures) != 1:
        raise UpperKaraImportedIncumbentProgramV1Error(
            "one shared observation projector cannot mix introduction schedules"
        )
    return ContinuousTwoWaveObservationProjectorV1(cases[0])


def _current_target_attackable(observation: CausalLiveStateProjectionV1) -> bool:
    state = observation.state
    semantics = state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    index = state.get("target_index")
    if (
        not isinstance(rows, list)
        or isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= len(rows)
        or not isinstance(rows[index], Mapping)
    ):
        raise UpperKaraImportedIncumbentProgramV1Error(
            "causal observation lacks a selected target row"
        )
    row = rows[index]
    return row.get("attackable") is True and row.get("dead") is False


def _localized_policy_inputs_v1(
    case: DevelopmentPrecombatWaveCaseV1,
    observation: CausalLiveStateProjectionV1,
) -> tuple[JSONMap, dict[int, Any], JSONMap]:
    indexes = observation.policy_to_simulator_target_index
    request = deepcopy(case.request)
    encounter = request.get("encounter")
    if not isinstance(encounter, dict) or not isinstance(
        encounter.get("targets"), list
    ):
        raise UpperKaraImportedIncumbentProgramV1Error(
            "case request lacks encounter targets"
        )
    raw_targets = encounter["targets"]
    encounter["targets"] = [deepcopy(raw_targets[index]) for index in indexes]
    contexts: dict[int, Any] = {}
    for policy_index, simulator_index in enumerate(indexes):
        try:
            context = case.target_contexts[simulator_index]
        except KeyError as error:
            raise UpperKaraImportedIncumbentProgramV1Error(
                f"case lacks target context {simulator_index}"
            ) from error
        contexts[policy_index] = replace(context, target_index=policy_index)
    target = _target_semantics._resolve_target_semantics_v3(
        observation.state, request, contexts
    )
    return request, contexts, target


@dataclass
class _CatControlsV1:
    autoattack_active: bool = True

    @property
    def used_locators(self) -> tuple[str, ...]:
        return ()


@dataclass
class _ContraControlsV1:
    autoattack_active: bool
    mainhand_name: str | None
    offhand_name: str | None

    def equipped_name(self, slot: int) -> str | None:
        return {16: self.mainhand_name, 17: self.offhand_name}.get(slot)


class _ContraPreflightSurfaceV1:
    def __init__(self, available: Sequence[AvailableAction]) -> None:
        self._available = list(available)

    def actions(self) -> list[AvailableAction]:
        return list(self._available)

    @staticmethod
    def has_native_capability(name: str) -> bool:
        return name in {"start_attack", "stop_cast"}

    @staticmethod
    def target_index(operation: str, value: str) -> None:
        del operation, value
        return None

    @staticmethod
    def item_action(locator: str) -> None:
        del locator
        return None


def _prefix_for_action(action: ActionRef) -> OptionalOffGcdPrefixV1:
    return OptionalOffGcdPrefixV1(
        action=action,
        guard=ObservableCausalGuardV1(
            action_ready=action,
            false_semantics=SKIP_PLAN,
        ),
    )


def _is_casting(state: Mapping[str, Any]) -> bool:
    current = state.get("current_cast")
    return isinstance(current, Mapping) and current.get("action") is not None


def _deferred_contra_queue_identities(
    decision: ExpertDecision,
) -> frozenset[tuple[str, str | None, str | None]]:
    rows = decision.metadata.get("deferred_queue_guard_checks", ())
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return frozenset()
    return frozenset(
        (
            str(row.get("operation")),
            row.get("value") if isinstance(row.get("value"), str) else None,
            row.get("source_ref")
            if isinstance(row.get("source_ref"), str)
            else None,
        )
        for row in rows
        if isinstance(row, Mapping)
        and row.get("reason")
        == "IsCurrentAction_deferred_until_after_whirlwind"
    )


def _program_decision_from_resolved_v1(
    decision: ExpertDecision,
    resolved: Sequence[Any],
    observation: CausalLiveStateProjectionV1,
    available_actions: tuple[AvailableAction, ...],
    *,
    current_stance: StanceOp,
    allow_cat_cvar_sidecar_omission: bool,
) -> tuple[ProgramDecisionV1, str | None]:
    if not decision.valid:
        raise UpperKaraImportedIncumbentProgramV1Error(
            f"source policy returned invalid: {decision.reason}"
        )
    by_action = {row.action: row for row in available_actions}
    if len(by_action) != len(available_actions):
        raise UpperKaraImportedIncumbentProgramV1Error(
            "available action identities are not unique"
        )
    prefixes: list[OptionalOffGcdPrefixV1] = []
    prefix_order: list[ProgramPrefixOperationKindV1] = []
    start_attack = False
    stop_cast = False
    queue_op = QueueLaneOp.KEEP
    queue_action: ActionRef | None = None
    queue_marker_added = False
    chosen_gcd: ActionRef | None = None
    chosen_key: str | None = None
    deferred_queues = _deferred_contra_queue_identities(decision)

    for item in resolved:
        sink = item.sink
        channel = sink.channel
        if channel == "cvar":
            if not allow_cat_cvar_sidecar_omission:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "reached CVar sink has no action-program representation"
                )
            continue
        if bool(getattr(item, "known_noop", False)):
            continue
        if channel == "target":
            raise UpperKaraImportedIncumbentProgramV1Error(
                "reached source target-selection sink lacks an exact local target binding"
            )
        if channel == "equipment":
            raise UpperKaraImportedIncumbentProgramV1Error(
                "reached source equipment sink has no action-program lane"
            )
        if channel == "item":
            raise UpperKaraImportedIncumbentProgramV1Error(
                "reached source item sink lacks an exact simulator ActionRef binding"
            )
        if channel == "autoattack":
            value = sink.value
            method = (
                str(item.arguments[0])
                if getattr(item, "arguments", ())
                else "start_attack" if value == "START" else ""
            )
            if method != "start_attack":
                raise UpperKaraImportedIncumbentProgramV1Error(
                    f"reached autoattack control {method or value!r} is not representable"
                )
            if start_attack:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "one source decision emitted start_attack more than once"
                )
            start_attack = True
            prefix_order.append(ProgramPrefixOperationKindV1.START_ATTACK)
            continue
        if channel == "cast_control":
            if _is_casting(observation.state):
                if stop_cast:
                    raise UpperKaraImportedIncumbentProgramV1Error(
                        "one source decision emitted stop_cast more than once"
                    )
                stop_cast = True
                prefix_order.append(ProgramPrefixOperationKindV1.STOP_CAST)
            continue

        action = getattr(item, "action_ref", None)
        if not isinstance(action, ActionRef):
            raise UpperKaraImportedIncumbentProgramV1Error(
                f"resolved {channel} sink lacks an exact ActionRef"
            )
        row = by_action.get(action)
        if row is None:
            raise UpperKaraImportedIncumbentProgramV1Error(
                f"source action is absent from the simulator spellbook: {action.to_wire()}"
            )

        if channel == "stance":
            stance = getattr(item, "stance", None)
            if stance is current_stance:
                continue
            if row.triggers_gcd:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "source stance is unexpectedly advertised as GCD-triggering"
                )
            prefixes.append(_prefix_for_action(action))
            prefix_order.append(ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD)
            continue
        if channel == "off_gcd":
            if row.triggers_gcd:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "source off-GCD action is advertised as GCD-triggering"
                )
            prefixes.append(_prefix_for_action(action))
            prefix_order.append(ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD)
            continue
        if channel == "swing_queue":
            if queue_marker_added:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "multiple source queue sinks cannot be represented by one queue lane"
                )
            queue_marker_added = True
            identity = (sink.operation, sink.value, sink.source_ref)
            if identity in deferred_queues:
                prefix_order.append(ProgramPrefixOperationKindV1.QUEUE_KEEP)
                continue
            if row.triggers_gcd:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "source next-swing action is advertised as GCD-triggering"
                )
            if row.legal and row.ready_in_ms == 0:
                queue_op = QueueLaneOp.SET
                queue_action = action
                prefix_order.append(ProgramPrefixOperationKindV1.QUEUE_SET)
            else:
                prefix_order.append(ProgramPrefixOperationKindV1.QUEUE_KEEP)
            continue
        if channel == "gcd":
            if not row.triggers_gcd:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "source GCD action is advertised as non-GCD"
                )
            if row.legal and row.ready_in_ms == 0:
                chosen_gcd = action
                key = getattr(item, "action_key", None)
                chosen_key = key if isinstance(key, str) and key else decision.gcd
                break
            continue
        raise UpperKaraImportedIncumbentProgramV1Error(
            f"unsupported resolved source channel {channel!r}"
        )

    if not queue_marker_added:
        prefix_order.append(ProgramPrefixOperationKindV1.QUEUE_KEEP)
    if chosen_gcd is not None:
        result = ProgramDecisionV1(
            start_attack=start_attack,
            stop_cast=stop_cast,
            optional_off_gcd_prefixes=tuple(prefixes),
            queue_op=queue_op,
            queue_action=queue_action,
            gcd_action=chosen_gcd,
            prefix_order=tuple(prefix_order),
        )
    else:
        wait_ms = (
            decision.wait_ms
            if decision.gcd == WAIT_ACTION and decision.wait_ms is not None
            else decision.metadata.get("known_noop_retry_wait_ms")
        )
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or wait_ms <= 0:
            wait_ms = SOURCE_REENTRY_WAIT_MS_V1
        result = ProgramDecisionV1(
            start_attack=start_attack,
            stop_cast=stop_cast,
            optional_off_gcd_prefixes=tuple(prefixes),
            queue_op=queue_op,
            queue_action=queue_action,
            wait_ms=wait_ms,
            prefix_order=tuple(prefix_order),
        )
    return result, chosen_key


class _SourceSessionV1:
    def __init__(
        self,
        *,
        policy_id: str,
        case: DevelopmentPrecombatWaveCaseV1,
        runtime_binding: Mapping[str, Any] | None,
    ) -> None:
        self.policy_id = policy_id
        self.case = case
        self.last_gcd_action = ""
        self._runtime_binding = runtime_binding
        if policy_id == CAT_POLICY_ID:
            self._controls: Any = _CatControlsV1()
            self._adapter: Any = CatFuryFullPolicyAdapterV4()
            self._inputs: Any = _cat_rollout.CatFurySimulatorInputsV5()
            self._mapper = _cat_rollout._cat_state_mapper(
                self._controls, self._inputs
            )
        elif policy_id == CONTRA260817_POLICY_ID:
            self._inputs = _contra_rollout.Contra260817SimulatorInputsV4()
            self._controls = _ContraControlsV1(
                autoattack_active=self._inputs.initial_autoattack_active,
                mainhand_name=self._inputs.equipped_mainhand_name,
                offhand_name=self._inputs.equipped_offhand_name,
            )
            self._adapter = Contra260817FuryFullPolicyAdapterV3(
                SOURCE_DEFAULT_PROFILE_V3
            )
            self._mapper = _contra_rollout._contra_state_mapper(
                self._controls,
                self._inputs,
                case.dynamic_load.contract_sha256,
            )
        elif policy_id == CONTRA_DEPLOYED_POLICY_ID:
            if runtime_binding is None:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "deployed Contra Raid-A requires a runtime binding"
                )
            self._adapter = RuntimeBoundContraDeployedFuryAdapterV7(
                runtime_binding, controller=RAID_A_CONTROLLER
            )
            self._controls = None
            self._inputs = None
            self._mapper = None
        elif policy_id == DEPLOYED_RAID_B_POLICY_ID:
            if runtime_binding is None:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    "deployed Contra Raid-B requires a runtime binding"
                )
            self._adapter = RuntimeBoundContraRaidBAdapterV1(runtime_binding)
            self._controls = None
            self._inputs = None
            self._mapper = None
        else:
            raise ValueError(f"unknown imported incumbent {policy_id!r}")

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available_actions: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not _current_target_attackable(observation):
            return ProgramDecisionV1(wait_ms=SOURCE_REENTRY_WAIT_MS_V1)
        request, _, target = _localized_policy_inputs_v1(
            self.case, observation
        )
        if self.policy_id == CAT_POLICY_ID:
            source_state = self._mapper(
                observation.state,
                available_actions,
                request,
                target,
                last_gcd_action=self.last_gcd_action,
            )
            proposal = _cat_rollout._proposal_v5(
                self._adapter, source_state, target
            )
            resolved, reasons = _cat_sinks._preflight(proposal)
            stance = source_state.combat.current_stance
            allow_cat_cvar = True
        elif self.policy_id == CONTRA260817_POLICY_ID:
            source_state = self._mapper(
                observation.state,
                available_actions,
                request,
                target,
                last_gcd_action=self.last_gcd_action,
            )
            proposal = _contra_rollout._proposal_v4(
                self._adapter, source_state, target
            )
            resolved, reasons = _contra_sinks._preflight(
                _ContraPreflightSurfaceV1(available_actions), proposal
            )
            stance = source_state.entry_combat.current_stance
            allow_cat_cvar = False
        else:
            combat = _deployed_rollout._combat_state(
                observation.state,
                available_actions,
                request,
                target,
                last_gcd_action=self.last_gcd_action,
            )
            proposal = _deployed_rollout._proposal(
                self._adapter, combat, target
            )
            projected = (
                _raid_a_identity(proposal)
                if self.policy_id == CONTRA_DEPLOYED_POLICY_ID
                else _raid_b_identity(proposal)
            )
            resolved, reasons = _deployed_sinks._preflight(projected)
            stance = combat.current_stance
            allow_cat_cvar = False
        if reasons:
            raise UpperKaraImportedIncumbentProgramV1Error(
                f"{self.policy_id} ordered-sink preflight failed: "
                + "; ".join(reasons)
            )
        result, chosen_key = _program_decision_from_resolved_v1(
            proposal,
            resolved,
            observation,
            available_actions,
            current_stance=stance,
            allow_cat_cvar_sidecar_omission=allow_cat_cvar,
        )
        if result.start_attack and isinstance(
            self._controls, (_CatControlsV1, _ContraControlsV1)
        ):
            self._controls.autoattack_active = True
        if chosen_key:
            self.last_gcd_action = chosen_key
        return result


def build_imported_incumbent_bindings_v1(
    build_id: str,
    case: DevelopmentPrecombatWaveCaseV1,
    *,
    runtime_binding_path: str | Path = DEFAULT_BINDING,
) -> tuple[ImportedReactiveProgramBindingV1, ...]:
    """Bind all three public baseline identities to their existing adapters."""

    if build_id not in BUILD_IDS:
        raise ValueError(f"build_id must be one of {BUILD_IDS!r}")
    if not isinstance(case, DevelopmentPrecombatWaveCaseV1):
        raise TypeError("case must be DevelopmentPrecombatWaveCaseV1")
    expected_build = case.case_spec.get("build_id")
    if expected_build != build_id:
        raise ValueError(
            f"case build {expected_build!r} differs from requested {build_id!r}"
        )
    runtime = load_deployed_contra_runtime_binding_v1(runtime_binding_path)
    policy_ids = baseline_policy_ids_for_build_v1(build_id)
    return tuple(
        ImportedReactiveProgramBindingV1(
            binding_id=policy_id,
            source_policy_id=policy_id,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
            resolver_factory=lambda policy_id=policy_id: _SourceSessionV1(
                policy_id=policy_id,
                case=case,
                runtime_binding=runtime,
            ),
        )
        for policy_id in policy_ids
    )


def _required_imported_binding_ids_v1(
    program: CausalActionProgramV1,
) -> tuple[str, ...]:
    """Return only the reactive bindings reachable from ``program``."""

    if not isinstance(program, CausalActionProgramV1):
        raise TypeError("program must be CausalActionProgramV1")
    selector = program.selector
    if isinstance(selector, ImportedReactiveSelectorV1):
        return (selector.binding_id,)
    if isinstance(selector, ImportedFallbackOverlaySelectorV1):
        return (selector.imported_fallback.binding_id,)
    if isinstance(
        selector,
        (
            ImportedReactiveQueueGcdBlockSelectorV1,
            ImportedReactiveBurstQueueGcdBlockSelectorV1,
        ),
    ):
        return (selector.imported_fallback.binding_id,)
    return ()


class _FreshIntegratedReplayV1:
    def __init__(
        self,
        *,
        build_id: str,
        cases_by_simulator: Mapping[int, DevelopmentPrecombatWaveCaseV1],
        bridge_path: Path,
        bridge_cwd: Path,
        runtime_binding_path: Path,
    ) -> None:
        self._build_id = build_id
        self._cases = dict(cases_by_simulator)
        self._bridge_path = bridge_path
        self._bridge_cwd = bridge_cwd
        self._runtime_binding_path = runtime_binding_path
        self._last_observation_audit: JSONMap | None = None

    @property
    def last_observation_audit(self) -> JSONMap | None:
        return (
            None
            if self._last_observation_audit is None
            else deepcopy(self._last_observation_audit)
        )

    def replay(self, seed: int, program: Any, *, max_decisions: int = 10_000) -> Any:
        try:
            source_case = self._cases[seed]
        except KeyError as error:
            raise KeyError(f"no continuous case is bound to simulator seed {seed}") from error
        # The train/eval driver keys master-seed cases by a separately derived
        # simulator seed.  Rebind both the bridge input and the source-policy
        # evidence contract to the seed that is actually executed; otherwise
        # Contra's state mapper would attest the master-seed load while the
        # bridge runs the derived seed.
        case = replace(
            source_case,
            dynamic_load=DynamicRolloutLoadV3.bind(
                source_case.request,
                seed,
                source_case.dynamic_load.config,
            ),
        )
        # All mutable components are new here.  Adapter latches and prefix-HP
        # observations persist across epochs of this replay, then are discarded
        # before the next seed/program replay.
        projector = build_continuous_two_wave_observation_projector_v1(case)
        required_binding_ids = _required_imported_binding_ids_v1(program)
        if required_binding_ids:
            available_bindings = build_imported_incumbent_bindings_v1(
                self._build_id,
                case,
                runtime_binding_path=self._runtime_binding_path,
            )
            by_id = {row.binding_id: row for row in available_bindings}
            missing = tuple(
                binding_id
                for binding_id in required_binding_ids
                if binding_id not in by_id
            )
            if missing:
                raise UpperKaraImportedIncumbentProgramV1Error(
                    f"program requires unavailable imported bindings {missing!r}"
                )
            bindings = tuple(by_id[binding_id] for binding_id in required_binding_ids)
        else:
            # Native searched selectors cannot reach a source-policy resolver.
            # Avoid loading Contra's runtime binding and opening three unused
            # stateful adapter sessions for every searched replay.
            bindings = ()
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


def build_imported_incumbent_program_replay_factory_v1(
    build_id: str,
    *,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
) -> Callable[[str, Mapping[int, Any]], _FreshIntegratedReplayV1]:
    """Return the train/eval driver's fresh-per-replay native factory."""

    deployed_controller_for_build_v1(build_id)
    resolved_bridge = Path(bridge_path).expanduser().resolve()
    resolved_cwd = Path(bridge_cwd).expanduser().resolve()
    resolved_binding = Path(runtime_binding_path).expanduser().resolve()

    def factory(
        loadout_id: str, cases_by_simulator: Mapping[int, Any]
    ) -> _FreshIntegratedReplayV1:
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
        return _FreshIntegratedReplayV1(
            build_id=build_id,
            cases_by_simulator=cases,
            bridge_path=resolved_bridge,
            bridge_cwd=resolved_cwd,
            runtime_binding_path=resolved_binding,
        )

    return factory


__all__ = (
    "ContinuousTwoWaveObservationProjectorV1",
    "OBSERVATION_CONTRACT_ID_V1",
    "SOURCE_REENTRY_WAIT_MS_V1",
    "UpperKaraImportedIncumbentProgramV1Error",
    "build_continuous_two_wave_observation_projector_v1",
    "build_imported_incumbent_bindings_v1",
    "build_imported_incumbent_program_replay_factory_v1",
    "continuous_two_wave_observation_projector_factory_v1",
    "deployed_controller_for_build_v1",
)
