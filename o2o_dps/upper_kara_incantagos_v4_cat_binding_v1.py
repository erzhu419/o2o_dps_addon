"""Development-only Cat source binding for a compiled responsive Incantagos v4 case.

The v4 environment is not a v3 ``DevelopmentPrecombatWaveCaseV1``.  In
particular its causal ``num_targets`` counts currently attackable targets,
not every living target.  Keep that difference explicit while reusing Cat's
actual state mapper, source adapter, and ordered-sink projection.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import cat_fury_full_policy_rollout_v5 as _cat_rollout
from . import cat_fury_ordered_sink_executor_v5 as _cat_sinks
from . import upper_kara_imported_incumbent_program_v1 as _incumbent
from .cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
    ContraTargetClassificationV2,
)
from .fury_full_policy_rollout_v3 import (
    TargetSemanticsContextV3,
    TargetSemanticsModeV3,
)
from . import fury_full_policy_rollout_v2 as _fury_v2
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
    project_live_state_for_policy_v1,
)
from .sim_bridge import AvailableAction
from .upper_kara_resolved_dynamic_v4_adapter_v1 import (
    CompiledResolvedIncantagosDynamicV4CaseV1,
)
from .upper_kara_trash_dynamic_v4_adapter_v1 import (
    CompiledResolvedTrashDynamicV4CaseV1,
)
from .upper_kara_responsive_incantagos_case_v1 import (
    CompiledResponsiveIncantagosCaseV1,
)


class IncantagosV4CatBindingV1Error(ValueError):
    """The source input cannot be represented honestly for the v4 case."""


FixedV4Case = (
    CompiledResolvedIncantagosDynamicV4CaseV1 | CompiledResolvedTrashDynamicV4CaseV1
)


def _focal_player(request: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        player = request["raid"]["parties"][0]["players"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise IncantagosV4CatBindingV1Error("request lacks focal player") from error
    if not isinstance(player, Mapping):
        raise IncantagosV4CatBindingV1Error("focal player must be an object")
    return player


def _historical_request_provenance(
    artifact: Mapping[str, Any] | None,
    *,
    fixed_case: FixedV4Case,
    equipment_request: Mapping[str, Any],
    responsive_request: Mapping[str, Any],
    equipped_item_names: tuple[str, ...],
) -> dict[str, Any]:
    if artifact is None:
        return {
            "kind": "CONTROLLED_SIMULATOR_REQUEST_HISTORICAL_BUILD_NOT_BOUND",
            "observed_equipment_id_match": False,
            "observed_talents_string_match": False,
            "simulator_effect_equivalence_verified": False,
        }
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("schema") != "exact_historical_fury_wowsims_request/v1"
        or artifact.get("status") != "DEVELOPMENT_CHARACTER_BUILD_ONLY"
        or artifact.get("comparison_authorized") is not False
    ):
        raise IncantagosV4CatBindingV1Error("exact historical build artifact is invalid")
    identity = artifact.get("source_identity")
    source = fixed_case.receipt.get("source")
    if not isinstance(identity, Mapping) or not isinstance(source, Mapping):
        raise IncantagosV4CatBindingV1Error("exact historical source identity is missing")
    if (
        identity.get("instance_id") != source.get("instance_id")
        or identity.get("player_guid") != source.get("focal_player_guid")
        or not isinstance(identity.get("build_segment_id"), str)
        or not identity["build_segment_id"]
    ):
        raise IncantagosV4CatBindingV1Error("exact historical build source differs from focal case")
    observed_request = artifact.get("request")
    if not isinstance(observed_request, Mapping):
        raise IncantagosV4CatBindingV1Error("exact historical request is missing")
    observed = _focal_player(observed_request)
    named = _focal_player(equipment_request)
    simulated = _focal_player(responsive_request)
    equipment = observed.get("equipment")
    talents = observed.get("talentsString")
    if (
        not isinstance(equipment, Mapping)
        or not isinstance(talents, str)
        or not talents
        or any(
            player.get("equipment") != equipment
            or player.get("talentsString") != talents
            for player in (named, simulated)
        )
    ):
        raise IncantagosV4CatBindingV1Error(
            "exact historical equipment or talents differ from simulator request"
        )
    if tuple(artifact.get("equipped_item_names", ())) != equipped_item_names:
        raise IncantagosV4CatBindingV1Error("historical item names differ from source binding")
    name_source = artifact.get("equipped_item_name_source")
    if not isinstance(name_source, str) or not name_source:
        raise IncantagosV4CatBindingV1Error("historical item name source is missing")
    return {
        "kind": "EXACT_HISTORICAL_FURY_REQUEST_CHARACTER_BUILD",
        "source_identity": deepcopy(dict(identity)),
        "equipped_item_name_source": name_source,
        "observed_equipment_id_match": True,
        "observed_talents_string_match": True,
        "simulator_effect_equivalence_verified": False,
    }


def _evidence(kind: ContraEvidenceKindV2, digest: str) -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(kind=kind, source_sha256=digest)


def _hypothesis(label: str, digest: str) -> ContraFieldEvidenceV2:
    return ContraFieldEvidenceV2(
        kind=ContraEvidenceKindV2.SENSITIVITY_HYPOTHESIS,
        corpus_sha256=digest,
        hypothesis_id=label,
    )


def _request_item_ids(request: Mapping[str, Any]) -> tuple[int, ...]:
    try:
        items = request["raid"]["parties"][0]["players"][0]["equipment"]["items"]
    except (KeyError, IndexError, TypeError) as error:
        raise IncantagosV4CatBindingV1Error("request lacks focal equipment") from error
    if not isinstance(items, list):
        raise IncantagosV4CatBindingV1Error("equipment.items must be a list")
    result = []
    for item in items:
        if not isinstance(item, Mapping):
            raise IncantagosV4CatBindingV1Error("equipment item must be an object")
        item_id = item.get("id")
        if item_id is None:
            continue
        if type(item_id) is not int or item_id <= 0:
            raise IncantagosV4CatBindingV1Error("equipment item ID is invalid")
        result.append(item_id)
    return tuple(result)


@dataclass(frozen=True)
class IncantagosV4CatBindingV1:
    """Static source inputs plus a fresh Cat session factory for each replay."""

    request: dict[str, Any]
    target_contexts: dict[int, TargetSemanticsContextV3]
    native_target_count: int
    receipt: dict[str, Any]

    def open_session(self) -> "IncantagosV4CatSessionV1":
        return IncantagosV4CatSessionV1(self)


class IncantagosV4PrefixProjectorV1:
    """Capture each target's HP baseline only after its first visible prefix."""

    def __init__(
        self,
        fixed_case: FixedV4Case,
        responsive_case: CompiledResponsiveIncantagosCaseV1,
    ) -> None:
        registry = fixed_case.occurrence_index_registry
        if tuple(row.target_guid for row in registry) != responsive_case.native_target_guids:
            raise IncantagosV4CatBindingV1Error("prefix target registries differ")
        introductions = tuple(
            TargetIntroductionV1(
                row.target_index,
                responsive_case.target_introduced_at_ms_by_guid[row.target_guid],
            )
            for row in registry
        )
        if not introductions or not any(row.introduced_at_ms == 0 for row in introductions):
            raise IncantagosV4CatBindingV1Error("no target is visible at the initial prefix")
        self.introductions = TargetIntroductionRegistryV1(targets=introductions)
        self.config_digest = responsive_case.dynamic_config.content_sha256
        self._baselines: dict[int, TargetHealthPrefixBaselineV1] = {}
        self._last_time_ms: int | None = None
        self._generation: int | None = None

    def __call__(
        self,
        state: Mapping[str, Any],
        available_actions: tuple[AvailableAction, ...],
    ) -> CausalLiveStateProjectionV1:
        del available_actions
        now = state.get("time_ms")
        if type(now) is not int or now < 0:
            raise IncantagosV4CatBindingV1Error("raw v4 time_ms is invalid")
        team = state.get("dynamic_team_background")
        semantics = state.get("dynamic_target_semantics")
        if not isinstance(team, Mapping) or not isinstance(semantics, Mapping):
            raise IncantagosV4CatBindingV1Error("raw v4 target state is missing")
        if (
            team.get("config_digest") != self.config_digest
            or semantics.get("config_digest") != self.config_digest
        ):
            raise IncantagosV4CatBindingV1Error("raw v4 target config differs")
        generation = team.get("environment_generation")
        if type(generation) is not int or generation != semantics.get("environment_generation"):
            raise IncantagosV4CatBindingV1Error("raw v4 environment generation differs")
        if (
            self._last_time_ms is not None
            and (now < self._last_time_ms or generation != self._generation)
        ):
            self._baselines.clear()
        self._last_time_ms = now
        self._generation = generation
        life = team.get("targets")
        semantic = semantics.get("targets")
        count = len(self.introductions.targets)
        if not isinstance(life, list) or not isinstance(semantic, list) or len(life) != count or len(semantic) != count:
            raise IncantagosV4CatBindingV1Error("raw v4 target count differs")
        intro_by_index = {
            row.simulator_target_index: row.introduced_at_ms
            for row in self.introductions.targets
        }
        for index, introduced_at in intro_by_index.items():
            if now < introduced_at or index in self._baselines:
                continue
            lifecycle = life[index]
            target = semantic[index]
            if not isinstance(lifecycle, Mapping) or not isinstance(target, Mapping):
                raise IncantagosV4CatBindingV1Error("raw v4 target row is invalid")
            self._baselines[index] = TargetHealthPrefixBaselineV1(
                simulator_target_index=index,
                observed_at_ms=now,
                current_health=lifecycle["current_health"],
                maximum_health=target["maximum_health"],
                simulated_damage_applied_at_observation=lifecycle["simulated_damage_applied"],
                background_damage_applied_at_observation=lifecycle["background_damage_applied"],
            )
        health_registry = TargetHealthPrefixRegistryV1(
            targets=tuple(
                self._baselines.get(
                    index,
                    TargetHealthPrefixBaselineV1(
                        simulator_target_index=index,
                        observed_at_ms=intro_by_index[index],
                        current_health=1.0,
                        maximum_health=1.0,
                        simulated_damage_applied_at_observation=0.0,
                        background_damage_applied_at_observation=0.0,
                    ),
                )
                for index in range(count)
            )
        )
        visible = tuple(index for index in range(count) if intro_by_index[index] <= now)
        selected = state.get("target_index")
        raw = state
        if selected not in visible:
            raw = deepcopy(state)
            raw["target_index"] = visible[0]
        return project_live_state_for_policy_v1(raw, self.introductions, health_registry)


def build_incantagos_v4_prefix_projector_v1(
    fixed_case: FixedV4Case,
    responsive_case: CompiledResponsiveIncantagosCaseV1,
) -> IncantagosV4PrefixProjectorV1:
    return IncantagosV4PrefixProjectorV1(fixed_case, responsive_case)


def build_incantagos_v4_cat_binding_v1(
    fixed_case: FixedV4Case,
    responsive_case: CompiledResponsiveIncantagosCaseV1,
    source_metadata: Mapping[str, Any],
    *,
    equipment_request: Mapping[str, Any],
    equipped_item_names: Sequence[str],
    classification_hypotheses_by_occurrence_id: Mapping[str, str],
    source_metadata_artifact_sha256: str,
    equipment_request_provenance: Mapping[str, Any] | None = None,
) -> IncantagosV4CatBindingV1:
    """Bind observed NPC names and explicit controlled-loadout hypotheses.

    Chronicle metadata does not supply ``UnitClassification`` or historical
    equipped gear.  Callers must provide those inputs explicitly.  Item IDs
    must match the simulator request exactly; names are paired in slot order.
    This does not establish historical same-gear or comparison eligibility.
    """

    if not isinstance(
        fixed_case,
        (CompiledResolvedIncantagosDynamicV4CaseV1, CompiledResolvedTrashDynamicV4CaseV1),
    ):
        raise TypeError("fixed_case must be a supported resolved dynamic-v4 case")
    trash = isinstance(fixed_case, CompiledResolvedTrashDynamicV4CaseV1)
    if not isinstance(responsive_case, CompiledResponsiveIncantagosCaseV1):
        raise TypeError("responsive_case must be CompiledResponsiveIncantagosCaseV1")
    if not isinstance(source_metadata, Mapping):
        raise TypeError("source_metadata must be a mapping")
    if (
        not isinstance(source_metadata_artifact_sha256, str)
        or len(source_metadata_artifact_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_metadata_artifact_sha256)
    ):
        raise ValueError("source metadata artifact identity must be a SHA-256 hex string")
    if not isinstance(equipment_request, Mapping):
        raise TypeError("equipment_request must be a mapping")
    if not isinstance(classification_hypotheses_by_occurrence_id, Mapping):
        raise TypeError("classification hypotheses must be a mapping")
    source = fixed_case.receipt.get("source")
    responsive_source = responsive_case.receipt.get("source")
    if not isinstance(source, Mapping) or not isinstance(responsive_source, Mapping):
        raise IncantagosV4CatBindingV1Error("compiled source receipts are missing")
    if (
        source_metadata.get("id") != source.get("instance_id")
        or responsive_source.get("instance_id") != source.get("instance_id")
        or responsive_source.get("encounter_id") != source.get("encounter_id")
    ):
        raise IncantagosV4CatBindingV1Error("metadata and compiled case source differ")
    if fixed_case.request != responsive_case.request:
        raise IncantagosV4CatBindingV1Error("fixed and responsive requests differ")
    registry = fixed_case.occurrence_index_registry
    count = len(registry)
    if count == 0 or tuple(row.target_index for row in registry) != tuple(range(count)):
        raise IncantagosV4CatBindingV1Error("native target registry is not contiguous")
    if tuple(row.target_guid for row in registry) != responsive_case.native_target_guids:
        raise IncantagosV4CatBindingV1Error("native target GUID registries differ")
    if set(classification_hypotheses_by_occurrence_id) != {
        row.occurrence_id for row in registry
    }:
        raise IncantagosV4CatBindingV1Error("classification hypotheses must cover native targets")
    if _request_item_ids(equipment_request) != _request_item_ids(responsive_case.request):
        raise IncantagosV4CatBindingV1Error("named equipment IDs differ from simulator request")
    names = tuple(equipped_item_names)
    if len(names) != len(_request_item_ids(equipment_request)) or any(
        not isinstance(name, str) or not name.strip() for name in names
    ):
        raise IncantagosV4CatBindingV1Error("equipped names must cover every nonempty item slot")
    build_provenance = _historical_request_provenance(
        equipment_request_provenance,
        fixed_case=fixed_case,
        equipment_request=equipment_request,
        responsive_request=responsive_case.request,
        equipped_item_names=names,
    )
    units = source_metadata.get("units")
    if not isinstance(units, Mapping):
        raise IncantagosV4CatBindingV1Error("metadata lacks unit names")
    request = deepcopy(responsive_case.request)
    request_targets = request.get("encounter", {}).get("targets")
    if not isinstance(request_targets, list) or len(request_targets) != count:
        raise IncantagosV4CatBindingV1Error("request target registry differs from native registry")
    config_digest = responsive_case.dynamic_config.content_sha256
    contexts: dict[int, TargetSemanticsContextV3] = {}
    for row in registry:
        index = row.target_index
        unit = units.get(row.target_guid)
        if not isinstance(unit, Mapping):
            raise IncantagosV4CatBindingV1Error(f"metadata lacks unit {row.target_guid}")
        name = unit.get("name")
        if not isinstance(name, str) or not name.strip():
            raise IncantagosV4CatBindingV1Error(f"metadata unit {row.target_guid} lacks a name")
        if (
            row.creature_entry_id is not None
            and unit.get("entry") != row.creature_entry_id
        ):
            raise IncantagosV4CatBindingV1Error(
                f"metadata unit {row.target_guid} entry differs: "
                f"metadata={unit.get('entry')!r}, registry={row.creature_entry_id!r}"
            )
        if request_targets[index].get("name") != row.target_guid:
            raise IncantagosV4CatBindingV1Error("compiled request target GUID differs")
        request_targets[index]["name"] = name
        try:
            classification = ContraTargetClassificationV2(
                classification_hypotheses_by_occurrence_id[row.occurrence_id]
            )
        except (ValueError, TypeError) as error:
            raise IncantagosV4CatBindingV1Error("invalid classification hypothesis") from error
        health = responsive_case.dynamic_config.target_health[index].maximum_health
        if type(health) not in (int, float) or not float(health).is_integer() or health <= 0:
            raise IncantagosV4CatBindingV1Error("v4 health hypothesis is not integral")
        contexts[index] = TargetSemanticsContextV3(
            context_id=(
                f"{'upper-kara-trash' if trash else 'incantagos'}-v4-development-{row.occurrence_id}"
            ),
            mode=TargetSemanticsModeV3.SIMULATOR_HYPOTHESIS,
            target_index=index,
            target_classification=classification,
            target_name=name,
            equipped_item_names=names,
            target_classification_evidence=_hypothesis(
                f"v4-classification-{index}", config_digest
            ),
            target_name_evidence=_evidence(
                ContraEvidenceKindV2.OBSERVED_SOURCE,
                source_metadata_artifact_sha256,
            ),
            equipment_evidence=_hypothesis("v4-controlled-loadout", config_digest),
            target_position_evidence=_hypothesis("v4-request-position", config_digest),
            target_health_pct_evidence=_hypothesis(f"v4-current-health-{index}", config_digest),
            target_max_health_evidence=_hypothesis(f"v4-max-health-{index}", config_digest),
            target_max_health=int(health),
        )
    return IncantagosV4CatBindingV1(
        request=request,
        target_contexts=contexts,
        native_target_count=count,
        receipt={
            "schema": (
                "upper_kara_trash_v4_cat_source_binding/v1"
                if trash else "incantagos_v4_cat_source_binding/v1"
            ),
            "status": "DEVELOPMENT_ONLY_NOT_COMPARISON_AUTHORIZED",
            "instance_id": source["instance_id"],
            "encounter_id": source["encounter_id"],
            "source_policy_id": CAT_POLICY_ID,
            "native_target_count": count,
            "equipment_binding": build_provenance["kind"],
            "equipment_request_provenance": build_provenance,
            "classification_binding": "EXPLICIT_SENSITIVITY_HYPOTHESES_NOT_OBSERVED_UNITCLASSIFICATION",
            "name_binding": "CHRONICLE_INSTANCE_METADATA_UNIT_NAME",
            "comparison_authorized": False,
        },
    )


class IncantagosV4CatSessionV1:
    def __init__(self, binding: IncantagosV4CatBindingV1) -> None:
        self.binding = binding
        self.last_gcd_action = ""
        self._controls = _incumbent._CatControlsV1()
        self._adapter = CatFuryFullPolicyAdapterV4()
        self._inputs = _cat_rollout.CatFurySimulatorInputsV5()
        self._mapper = _cat_rollout._cat_state_mapper(self._controls, self._inputs)

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available_actions: tuple[AvailableAction, ...],
    ) -> Any:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("Cat v4 resolver requires a causal live projection")
        if not _incumbent._current_target_attackable(observation):
            return _incumbent.ProgramDecisionV1(wait_ms=_incumbent.SOURCE_REENTRY_WAIT_MS_V1)
        indexes = observation.policy_to_simulator_target_index
        if not indexes or any(index < 0 or index >= self.binding.native_target_count for index in indexes):
            raise IncantagosV4CatBindingV1Error("causal target mapping is outside v4 registry")
        request = deepcopy(self.binding.request)
        targets = request["encounter"]["targets"]
        request["encounter"]["targets"] = [deepcopy(targets[index]) for index in indexes]
        context = self.binding.target_contexts[indexes[observation.state["target_index"]]]
        semantics = observation.state["dynamic_target_semantics"]["targets"]
        selected = semantics[observation.state["target_index"]]
        maximum = selected.get("maximum_health")
        current = selected.get("current_health")
        if (
            type(maximum) not in (int, float)
            or type(current) not in (int, float)
            or maximum != context.target_max_health
            or not 0 <= current <= maximum
            or observation.state.get("num_targets")
            != sum(row.get("attackable") is True and row.get("dead") is False for row in semantics)
        ):
            raise IncantagosV4CatBindingV1Error("causal v4 health or attackable count differs")
        target = {
            "target_index": observation.state["target_index"],
            "target_health_pct": 100.0 * current / maximum,
            "target_max_health": int(maximum),
            "target_classification": context.target_classification.value,
            "target_name": context.target_name,
            "target_distance_yards": _fury_v2._player_distance(request),
            "equipped_item_names": list(context.equipped_item_names),
        }
        state = self._mapper(
            observation.state,
            available_actions,
            request,
            target,
            last_gcd_action=self.last_gcd_action,
        )
        proposal = _cat_rollout._proposal_v5(self._adapter, state, target)
        resolved, reasons = _cat_sinks._preflight(proposal)
        if reasons:
            raise IncantagosV4CatBindingV1Error(
                "Cat ordered-sink preflight failed: " + "; ".join(reasons)
            )
        decision, chosen_key = _incumbent._program_decision_from_resolved_v1(
            proposal,
            resolved,
            observation,
            available_actions,
            current_stance=state.combat.current_stance,
            allow_cat_cvar_sidecar_omission=True,
        )
        if decision.start_attack:
            self._controls.autoattack_active = True
        if chosen_key:
            self.last_gcd_action = chosen_key
        return decision


__all__ = (
    "IncantagosV4CatBindingV1",
    "IncantagosV4CatBindingV1Error",
    "IncantagosV4CatSessionV1",
    "IncantagosV4PrefixProjectorV1",
    "build_incantagos_v4_cat_binding_v1",
    "build_incantagos_v4_prefix_projector_v1",
)
