"""Development-only deployed Contra Raid-B resolver for causal v4 replay.

The source sees only the targets introduced by the current observation.  Its
ordered sinks are translated through the existing action-program vocabulary;
any reached sink without an exact representation remains a hard failure.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Mapping

from . import fury_full_policy_rollout_v2 as _rollout
from . import fury_ordered_sink_executor_v2 as _sinks
from .causal_action_program_v1 import (
    ImportedReactiveProgramBindingV1,
    ProgramDecisionV1,
)
from .contra_incantagos_v4_policy_binding_v1 import (
    ContraIncantagosV4PolicyBindingV1,
)
from .deployed_contra_runtime_binding_v1 import (
    validate_deployed_contra_runtime_binding_v1,
)
from .fury_ordered_sink_executor_raid_b_v1 import _project as _raid_b_identity
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    POLICY_ID,
    RuntimeBoundContraRaidBAdapterV1,
)
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .sim_bridge import AvailableAction
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
    SOURCE_REENTRY_WAIT_MS_V1,
    _current_target_attackable,
    _program_decision_from_resolved_v1,
)


class DeployedContraIncantagosV4SessionV1Error(RuntimeError):
    """The prefix-visible Raid-B source cannot be represented exactly."""


class DeployedContraIncantagosV4SessionV1:
    """Maintain Raid-B source state across one fresh responsive replay."""

    def __init__(
        self,
        binding: ContraIncantagosV4PolicyBindingV1,
        runtime_binding: Mapping[str, Any],
    ) -> None:
        if not isinstance(binding, ContraIncantagosV4PolicyBindingV1):
            raise TypeError("binding must be ContraIncantagosV4PolicyBindingV1")
        self.binding = binding
        self.last_gcd_action = ""
        self._source = RuntimeBoundContraRaidBAdapterV1(
            validate_deployed_contra_runtime_binding_v1(runtime_binding)
        )

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available_actions: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("Raid-B v4 resolver requires a causal live projection")
        if not _current_target_attackable(observation):
            return ProgramDecisionV1(wait_ms=SOURCE_REENTRY_WAIT_MS_V1)

        indexes = observation.policy_to_simulator_target_index
        if not indexes or len(set(indexes)) != len(indexes) or any(
            type(index) is not int or index < 0 or index >= len(self.binding.native_target_guids)
            for index in indexes
        ):
            raise DeployedContraIncantagosV4SessionV1Error(
                "causal target mapping is outside the native v4 registry"
            )
        request = deepcopy(self.binding.request)
        encounter = request.get("encounter")
        targets = encounter.get("targets") if isinstance(encounter, dict) else None
        if not isinstance(targets, list) or len(targets) != len(self.binding.native_target_guids):
            raise DeployedContraIncantagosV4SessionV1Error(
                "source request and native target registry differ"
            )
        encounter["targets"] = [deepcopy(targets[index]) for index in indexes]
        contexts = {
            policy_index: replace(
                self.binding.target_contexts[simulator_index],
                target_index=policy_index,
            )
            for policy_index, simulator_index in enumerate(indexes)
        }
        state = observation.state
        selected_index = state.get("target_index")
        semantics = state.get("dynamic_target_semantics")
        rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
        if (
            type(selected_index) is not int or not isinstance(rows, list)
            or selected_index < 0 or selected_index >= len(rows)
            or state.get("total_target_count") != len(indexes)
            or set(contexts) != set(range(len(indexes)))
        ):
            raise DeployedContraIncantagosV4SessionV1Error(
                "causal target semantics differ from the prefix registry"
            )
        selected = rows[selected_index]
        context = contexts[selected_index]
        maximum = selected.get("maximum_health")
        current = selected.get("current_health")
        if (
            type(maximum) not in (int, float)
            or type(current) not in (int, float)
            or maximum != context.target_max_health
            or not 0 <= current <= maximum
            or selected.get("attackable") is not True
            or selected.get("dead") is not False
            or state.get("num_targets") != sum(
                row.get("attackable") is True and row.get("dead") is False
                for row in rows
            )
            or encounter["targets"][selected_index].get("name") != context.target_name
        ):
            raise DeployedContraIncantagosV4SessionV1Error(
                "causal v4 selected target HP, name or attackability differs"
            )
        target = {
            "target_index": selected_index,
            "target_health_pct": 100.0 * current / maximum,
            "target_max_health": int(maximum),
            "target_classification": context.target_classification.value,
            "target_name": context.target_name,
            "target_distance_yards": _rollout._player_distance(request),
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
        combat = _rollout._combat_state(
            observation.state, available_actions, request, target,
            last_gcd_action=self.last_gcd_action,
        )
        proposal = _rollout._proposal(self._source, combat, target)
        resolved, reasons = _sinks._preflight(_raid_b_identity(proposal))
        if reasons:
            raise DeployedContraIncantagosV4SessionV1Error(
                "Raid-B ordered-sink preflight failed: " + "; ".join(reasons)
            )
        decision, chosen_key = _program_decision_from_resolved_v1(
            proposal, resolved, observation, available_actions,
            current_stance=combat.current_stance,
            allow_cat_cvar_sidecar_omission=False,
        )
        if chosen_key:
            self.last_gcd_action = chosen_key
        return decision


def build_deployed_contra_incantagos_v4_reactive_binding_v1(
    binding: ContraIncantagosV4PolicyBindingV1,
    runtime_binding: Mapping[str, Any],
) -> ImportedReactiveProgramBindingV1:
    """Open a new deployed Raid-B resolver for each replay seed."""

    validated = validate_deployed_contra_runtime_binding_v1(runtime_binding)
    return ImportedReactiveProgramBindingV1(
        binding_id=POLICY_ID,
        source_policy_id=POLICY_ID,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=lambda: DeployedContraIncantagosV4SessionV1(
            binding, validated
        ),
    )


__all__ = (
    "DeployedContraIncantagosV4SessionV1",
    "DeployedContraIncantagosV4SessionV1Error",
    "build_deployed_contra_incantagos_v4_reactive_binding_v1",
)
