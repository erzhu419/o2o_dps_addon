"""Development-only causal Contra260817 session for the responsive v4 bridge.

The source adapter is invoked afresh at each input boundary.  Only the
prefix-visible target registry reaches its mapper; the existing Contra
ordered-sink preflight and action-program conversion preserve reached sink
order.  Source sinks without an action-program lane fail closed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

from . import contra260817_fury_full_policy_rollout_v4 as _rollout
from . import contra260817_fury_ordered_sink_executor_v4 as _sinks
from . import upper_kara_imported_incumbent_program_v1 as _incumbent
from .causal_action_program_v1 import ImportedReactiveProgramBindingV1, ProgramDecisionV1
from .contra260817_fury_full_policy_v3 import (
    SOURCE_DEFAULT_PROFILE_V3,
    Contra260817FuryFullPolicyAdapterV3,
)
from .contra260817_fury_full_policy_rollout_v4 import Contra260817SimulatorInputsV4
from .contra_incantagos_v4_policy_binding_v1 import ContraIncantagosV4PolicyBindingV1
from . import fury_full_policy_rollout_v2 as _fury_v2
from .fury_paired_multiseed_runner_v4 import CONTRA260817_POLICY_ID
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .sim_bridge import AvailableAction


class Contra260817IncantagosV4SessionError(RuntimeError):
    """The live v4 source prefix cannot be executed as an action program."""


class Contra260817IncantagosV4ReactiveSessionV1:
    """One replay's stateful Contra260817 source, without a v3 load contract."""

    def __init__(
        self,
        binding: ContraIncantagosV4PolicyBindingV1,
        inputs: Contra260817SimulatorInputsV4,
    ) -> None:
        if not isinstance(binding, ContraIncantagosV4PolicyBindingV1):
            raise TypeError("binding must be a Contra Incantagos v4 policy binding")
        if not isinstance(inputs, Contra260817SimulatorInputsV4):
            raise TypeError("inputs must be Contra260817SimulatorInputsV4")
        if (
            inputs.equipped_mainhand_name not in binding.equipped_item_names
            or inputs.equipped_offhand_name not in binding.equipped_item_names
        ):
            raise Contra260817IncantagosV4SessionError(
                "Contra weapon-name hypotheses differ from the v4 loadout"
            )
        self.binding = binding
        self.inputs = inputs
        self.last_gcd_action = ""
        self._controls = _incumbent._ContraControlsV1(
            autoattack_active=inputs.initial_autoattack_active,
            mainhand_name=inputs.equipped_mainhand_name,
            offhand_name=inputs.equipped_offhand_name,
        )
        self._adapter = Contra260817FuryFullPolicyAdapterV3(
            SOURCE_DEFAULT_PROFILE_V3
        )
        if not self._adapter.source_identity_verified:
            raise Contra260817IncantagosV4SessionError(
                f"Contra260817 source identity: {self._adapter.source_verification}"
            )
        self._mapper = _rollout._contra_state_mapper(
            self._controls, inputs, binding.dynamic_config_digest
        )

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available_actions: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("Contra260817 v4 resolver requires a causal live projection")
        if not _incumbent._current_target_attackable(observation):
            return ProgramDecisionV1(wait_ms=_incumbent.SOURCE_REENTRY_WAIT_MS_V1)

        indexes = observation.policy_to_simulator_target_index
        if (
            not indexes
            or len(set(indexes)) != len(indexes)
            or any(
                type(index) is not int
                or index < 0
                or index >= len(self.binding.native_target_guids)
                for index in indexes
            )
        ):
            raise Contra260817IncantagosV4SessionError(
                "causal target map differs from the native v4 registry"
            )
        request = deepcopy(self.binding.request)
        encounter = request.get("encounter")
        targets = encounter.get("targets") if isinstance(encounter, dict) else None
        if not isinstance(targets, list) or len(targets) != len(self.binding.native_target_guids):
            raise Contra260817IncantagosV4SessionError(
                "named source request differs from the native v4 registry"
            )
        encounter["targets"] = [deepcopy(targets[index]) for index in indexes]
        selected_index = observation.state["target_index"]
        context = replace(
            self.binding.target_contexts[indexes[selected_index]],
            target_index=selected_index,
        )
        semantic = observation.state["dynamic_target_semantics"]["targets"][
            selected_index
        ]
        current = semantic.get("current_health")
        maximum = semantic.get("maximum_health")
        if (
            type(current) not in (int, float)
            or type(maximum) not in (int, float)
            or maximum != context.target_max_health
            or not 0 <= current <= maximum
            or observation.state.get("num_targets")
            != sum(
                row.get("attackable") is True and row.get("dead") is False
                for row in observation.state["dynamic_target_semantics"]["targets"]
            )
        ):
            raise Contra260817IncantagosV4SessionError(
                "causal v4 health or attackable count differs"
            )
        # The causal projection deliberately does not expose simulator armor.
        # Contra260817 reads the named context, HP and target count here, not
        # effective armor.  Do not reintroduce hidden simulator internals.
        target = {
            "target_index": selected_index,
            "target_health_pct": 100.0 * current / maximum,
            "target_max_health": int(maximum),
            "target_classification": context.target_classification.value,
            "target_name": context.target_name,
            "target_distance_yards": _fury_v2._player_distance(request),
            "equipped_item_names": list(context.equipped_item_names),
            "field_evidence": {
                "target_health_pct": context.target_health_pct_evidence,
                "target_max_health": context.target_max_health_evidence,
                "target_classification": context.target_classification_evidence,
                "target_name": context.target_name_evidence,
                "equipped_item_names": context.equipment_evidence,
                "target_position": context.target_position_evidence,
            },
        }
        source_state = self._mapper(
            observation.state,
            available_actions,
            request,
            target,
            last_gcd_action=self.last_gcd_action,
        )
        proposal = _rollout._proposal_v4(self._adapter, source_state, target)
        resolved, reasons = _sinks._preflight(
            _incumbent._ContraPreflightSurfaceV1(available_actions), proposal
        )
        if reasons:
            raise Contra260817IncantagosV4SessionError(
                "Contra260817 ordered-sink preflight failed: "
                + "; ".join(reasons)
            )
        decision, chosen_key = _incumbent._program_decision_from_resolved_v1(
            proposal,
            resolved,
            observation,
            available_actions,
            current_stance=source_state.entry_combat.current_stance,
            allow_cat_cvar_sidecar_omission=False,
        )
        if decision.start_attack:
            self._controls.autoattack_active = True
        if chosen_key:
            self.last_gcd_action = chosen_key
        return decision


def build_contra260817_incantagos_v4_imported_binding_v1(
    binding: ContraIncantagosV4PolicyBindingV1,
    inputs: Contra260817SimulatorInputsV4,
) -> ImportedReactiveProgramBindingV1:
    """Give native v4 replay a new source session on every replay."""

    return ImportedReactiveProgramBindingV1(
        binding_id=CONTRA260817_POLICY_ID,
        source_policy_id=CONTRA260817_POLICY_ID,
        observation_contract_id=_incumbent.OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=lambda: Contra260817IncantagosV4ReactiveSessionV1(
            binding, inputs
        ),
    )


__all__ = (
    "Contra260817IncantagosV4ReactiveSessionV1",
    "Contra260817IncantagosV4SessionError",
    "build_contra260817_incantagos_v4_imported_binding_v1",
)
