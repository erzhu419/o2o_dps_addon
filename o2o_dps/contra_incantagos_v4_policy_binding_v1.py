"""Development-only Contra policy inputs for a responsive Incantagos v4 load.

This binds *reads*, not a performance comparison or a v3 rollout receipt.  The
compiled simulator request uses target GUIDs as names and a synthetic player
build.  The source policy reads Chronicle's NPC names from a separate policy
request copy; classification and gear names remain explicit hypotheses.  A
source target-switch sink is executable only if its
locator has a separate native target binding; no static nearest-enemy mapping
is inferred here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contra260817_fury_full_policy_rollout_v4 import (
    Contra260817SimulatorInputsV4,
    _contra_state_mapper,
)
from .contra260817_fury_ordered_sink_executor_v4 import (
    Contra260817SimulatorControlFacadeV4,
    Contra260817TargetBindingV4,
)
from .contra260817_fury_full_policy_v3 import (
    SOURCE_DEFAULT_PROFILE_V3,
    Contra260817FuryFullPolicyAdapterV3,
    validate_source_decision_v3,
)
from .deployed_contra_runtime_binding_v1 import (
    validate_deployed_contra_runtime_binding_v1,
)
from .fury_full_policy_rollout_v2 import _combat_state, _proposal
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
)
from .fury_full_policy_rollout_v4 import _resolve_target_semantics_v4
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    RuntimeBoundContraRaidBAdapterV1,
)
from .sim_bridge import AvailableAction
from .upper_kara_incantagos_v4_cat_binding_v1 import (
    build_incantagos_v4_cat_binding_v1,
)
from .upper_kara_resolved_dynamic_v4_adapter_v1 import (
    CompiledResolvedIncantagosDynamicV4CaseV1,
)
from .upper_kara_trash_dynamic_v4_adapter_v1 import (
    CompiledResolvedTrashDynamicV4CaseV1,
)
from .upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)


SCHEMA = "contra_incantagos_v4_policy_binding/v1"


@dataclass(frozen=True)
class ContraIncantagosV4PolicyBindingV1:
    request: Mapping[str, Any]
    target_contexts: Mapping[int, TargetSemanticsContextV3]
    dynamic_config_digest: str
    native_target_guids: tuple[str, ...]
    equipped_item_names: tuple[str, ...]
    receipt: Mapping[str, Any]

    def resolve_target(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Read live v4 HP, armor and attackability at this decision boundary."""

        return _resolve_target_semantics_v4(
            state, self.request, self.target_contexts
        )

    def exact_guid_target_bindings(self) -> tuple[Contra260817TargetBindingV4, ...]:
        """Bind explicit GUID selection only; nearest-enemy remains unresolved."""

        return tuple(
            Contra260817TargetBindingV4("TargetUnit", guid, index)
            for index, guid in enumerate(self.native_target_guids)
        )


def bind_contra_incantagos_v4_policy_v1(
    fixed_case: (
        CompiledResolvedIncantagosDynamicV4CaseV1
        | CompiledResolvedTrashDynamicV4CaseV1
    ),
    case: CompiledResponsiveIncantagosCaseV1,
    source_metadata: Mapping[str, Any],
    *,
    source_metadata_artifact_sha256: str,
    classification_hypotheses_by_occurrence_id: Mapping[str, str],
    equipment_request: Mapping[str, Any],
    equipped_item_names: Sequence[str],
    equipment_request_provenance: Mapping[str, Any] | None = None,
) -> ContraIncantagosV4PolicyBindingV1:
    """Reuse the same frozen metadata/name and gear-slot binding as Cat."""

    named = build_incantagos_v4_cat_binding_v1(
        fixed_case, case, source_metadata,
        equipment_request=equipment_request,
        equipped_item_names=equipped_item_names,
        classification_hypotheses_by_occurrence_id=(
            classification_hypotheses_by_occurrence_id
        ),
        source_metadata_artifact_sha256=source_metadata_artifact_sha256,
        equipment_request_provenance=equipment_request_provenance,
    )
    guids = tuple(case.native_target_guids)
    names = tuple(equipped_item_names)
    evidence_sha = case.dynamic_config.content_sha256
    exact_build = named.receipt["equipment_request_provenance"]["observed_equipment_id_match"]
    trash = isinstance(fixed_case, CompiledResolvedTrashDynamicV4CaseV1)
    return ContraIncantagosV4PolicyBindingV1(
        request=named.request,
        target_contexts=named.target_contexts,
        dynamic_config_digest=evidence_sha,
        native_target_guids=guids,
        equipped_item_names=names,
        receipt={
            "schema": (
                "contra_upper_kara_trash_v4_policy_binding/v1" if trash else SCHEMA
            ),
            "dynamic_config_digest": evidence_sha,
            "source_metadata_artifact_sha256": source_metadata_artifact_sha256,
            "native_target_guids": list(guids),
            "target_classifications": {
                str(index): named.target_contexts[index].target_classification.value
                for index in range(len(guids))
            },
            "target_name_binding": "CHRONICLE_INSTANCE_METADATA_UNIT_NAME",
            "simulator_request_target_labels_remain_guids": True,
            "chronicle_metadata_name_bound": True,
            "game_client_unit_name_observed": False,
            "equipped_item_names": list(names),
            "equipment_request_provenance": named.receipt["equipment_request_provenance"],
            "observed_equipment_id_match": exact_build,
            "simulator_build_is_synthetic": not exact_build,
            "simulator_effect_equivalence_verified": False,
            "same_gear_as_historical_focal_verified": False,
            "nearest_enemy_source_target_switch_bound": False,
            "source_item_actions_bound": False,
            "comparison_authorized": False,
        },
    )


def propose_deployed_contra_incantagos_v4_v1(
    binding: ContraIncantagosV4PolicyBindingV1,
    *,
    runtime_binding: Mapping[str, Any],
    state: Mapping[str, Any],
    available: Sequence[AvailableAction],
    last_gcd_action: str,
) -> Any:
    """Invoke deployed Raid-B policy once on live v4 target semantics."""

    source = RuntimeBoundContraRaidBAdapterV1(
        validate_deployed_contra_runtime_binding_v1(runtime_binding)
    )
    target = binding.resolve_target(state)
    combat = _combat_state(
        state, available, binding.request, target,
        last_gcd_action=last_gcd_action,
    )
    return _proposal(source, combat, target)


def propose_contra260817_incantagos_v4_v1(
    binding: ContraIncantagosV4PolicyBindingV1,
    *,
    controls: Contra260817SimulatorControlFacadeV4,
    inputs: Contra260817SimulatorInputsV4,
    source: Contra260817FuryFullPolicyAdapterV3,
    state: Mapping[str, Any],
    available: Sequence[AvailableAction],
    last_gcd_action: str,
) -> Any:
    """Invoke exact Contra260817 source once; no DynamicRolloutLoadV3 is used."""

    if type(source) is not Contra260817FuryFullPolicyAdapterV3 or source.profile != SOURCE_DEFAULT_PROFILE_V3:
        raise ValueError("Contra260817 v4 binding requires the unmodified source-default profile")
    if (
        inputs.equipped_mainhand_name not in binding.equipped_item_names
        or inputs.equipped_offhand_name not in binding.equipped_item_names
    ):
        raise ValueError("Contra source equipment names differ from the v4 loadout hypothesis")
    target = binding.resolve_target(state)
    mapper = _contra_state_mapper(
        controls, inputs, binding.dynamic_config_digest
    )
    combat = mapper(
        state, available, binding.request, target,
        last_gcd_action=last_gcd_action,
    )
    return validate_source_decision_v3(source.propose(combat))


__all__ = (
    "SCHEMA",
    "ContraIncantagosV4PolicyBindingV1",
    "bind_contra_incantagos_v4_policy_v1",
    "propose_deployed_contra_incantagos_v4_v1",
    "propose_contra260817_incantagos_v4_v1",
)
