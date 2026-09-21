"""Adapt one authoritative registry phase into one wave-local search cell.

The route registry is observational.  This adapter therefore refuses to make
a search contract until the selected phase has an authoritative target
overlay and the caller supplies every target-model binding.  In particular,
metadata activity and list order are never promoted to target permission or
priority.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping, Sequence

from .upper_kara_route_wave_registry_v1 import (
    TARGET_CONTRACT_AUTHORITATIVE,
    validate_route_wave_registry_v1,
)
from .upper_kara_wave_local_search_contract_v1 import (
    BaselineRefV1,
    FrozenPlayerIdentityV1,
    ResourceAvailabilityCaseV1,
    ResourceRefV1,
    SeedNamespaceV1,
    TargetStageV1,
    UpperKaraWaveLocalSearchContractV1,
    WaveSearchUnitV1,
    WaveTargetBindingV1,
)


JSONMap = dict[str, Any]


class UpperKaraRegistryLocalContractAdapterError(ValueError):
    """The selected registry phase cannot be represented without guessing."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraRegistryLocalContractAdapterError(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _find_phase_v1(
    registry: Mapping[str, Any],
    *,
    instance_id: str,
    pull_ref: str,
    phase_ref: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    wanted_instance = _text(instance_id, "instance_id")
    wanted_pull = _text(pull_ref, "pull_ref")
    wanted_phase = _text(phase_ref, "phase_ref")
    for instance in registry["instances"]:
        if instance["instance_id"] != wanted_instance:
            continue
        for encounter in instance["encounters"]:
            if encounter["pull_ref"] != wanted_pull:
                continue
            for phase in encounter["phase_candidates"]:
                if phase["phase_ref"] == wanted_phase:
                    return instance, encounter, phase
    raise UpperKaraRegistryLocalContractAdapterError(
        "selected instance/pull/phase was not found"
    )


def _total_priority_order_v1(
    allowed: Sequence[str], edges: Sequence[Sequence[str]]
) -> tuple[str, ...] | None:
    """Return a lossless total order, or None for an unconstrained target set.

    A non-empty partial order with multiple currently minimal targets cannot be
    represented by the v1 linear stage machine without adding an ordering that
    the authority did not provide, so it is rejected.
    """

    if not edges:
        return None
    allowed_set = set(allowed)
    successors = {target_id: set() for target_id in allowed}
    indegree = {target_id: 0 for target_id in allowed}
    for edge in edges:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise UpperKaraRegistryLocalContractAdapterError(
                "priority edge must contain exactly two target IDs"
            )
        before, after = edge
        if before not in allowed_set or after not in allowed_set or before == after:
            raise UpperKaraRegistryLocalContractAdapterError(
                "priority edge does not order two distinct allowed targets"
            )
        if after not in successors[before]:
            successors[before].add(after)
            indegree[after] += 1

    ordered: list[str] = []
    while len(ordered) < len(allowed):
        ready = [
            target_id
            for target_id in allowed
            if indegree[target_id] == 0 and target_id not in ordered
        ]
        if len(ready) != 1:
            reason = "cyclic" if not ready else "not a total order"
            raise UpperKaraRegistryLocalContractAdapterError(
                f"authoritative priority partial order is {reason} and cannot be "
                "represented losslessly by a v1 target stage"
            )
        current = ready[0]
        ordered.append(current)
        for successor in successors[current]:
            indegree[successor] -= 1
    return tuple(ordered)


def resource_availability_cases_v1(
    resources: Sequence[ResourceRefV1],
) -> tuple[ResourceAvailabilityCaseV1, ...]:
    """Enumerate every local ready/not-ready state for route-level valuation."""

    resource_rows = tuple(resources)
    if any(not isinstance(row, ResourceRefV1) for row in resource_rows):
        raise TypeError("resources must contain ResourceRefV1 values")
    resource_ids = tuple(sorted(row.resource_id for row in resource_rows))
    if len(resource_ids) != len(set(resource_ids)):
        raise UpperKaraRegistryLocalContractAdapterError(
            "resource identities must be unique"
        )
    cases: list[ResourceAvailabilityCaseV1] = []
    for ready_count in range(len(resource_ids) + 1):
        for ready in combinations(resource_ids, ready_count):
            case_id = "none-ready" if not ready else "ready:" + "+".join(ready)
            cases.append(ResourceAvailabilityCaseV1(case_id, ready))
    return tuple(cases)


def registry_phase_to_local_search_contract_v1(
    registry: Mapping[str, Any],
    *,
    instance_id: str,
    pull_ref: str,
    phase_ref: str,
    campaign_id: str,
    frozen_player: FrozenPlayerIdentityV1,
    target_bindings: Mapping[str, WaveTargetBindingV1],
    variant_budget: int,
    baselines: Sequence[BaselineRefV1],
    train_seeds: SeedNamespaceV1,
    selection_seeds: SeedNamespaceV1,
    heldout_seeds: SeedNamespaceV1,
    resources: Sequence[ResourceRefV1],
) -> UpperKaraWaveLocalSearchContractV1:
    """Build one local-search cell from one exact authoritative phase.

    The caller must provide model bindings for every target in the encounter.
    This is intentional: metadata supplies identity and timing observations,
    not calibrated HP, armor, team-clock, or attackability models.
    """

    validate_route_wave_registry_v1(registry)
    instance, encounter, phase = _find_phase_v1(
        registry,
        instance_id=instance_id,
        pull_ref=pull_ref,
        phase_ref=phase_ref,
    )
    target_contract = phase["target_contract"]
    if target_contract["status"] != TARGET_CONTRACT_AUTHORITATIVE:
        raise UpperKaraRegistryLocalContractAdapterError(
            "selected phase target rules are unresolved; apply an authoritative "
            "target overlay before creating a search contract"
        )
    if target_contract["observed_activity_grants_target_permission"] is not False:
        raise UpperKaraRegistryLocalContractAdapterError(
            "observed activity must not grant target permission"
        )

    encounter_targets = tuple(encounter["targets"])
    occurrence_ids = tuple(row["occurrence_id"] for row in encounter_targets)
    if not isinstance(target_bindings, Mapping):
        raise TypeError("target_bindings must be a mapping")
    if set(target_bindings) != set(occurrence_ids):
        raise UpperKaraRegistryLocalContractAdapterError(
            "target_bindings must cover exactly every encounter target"
        )
    ordered_bindings: list[WaveTargetBindingV1] = []
    for target in encounter_targets:
        occurrence_id = target["occurrence_id"]
        binding = target_bindings[occurrence_id]
        if not isinstance(binding, WaveTargetBindingV1):
            raise TypeError("target_bindings values must be WaveTargetBindingV1")
        if binding.occurrence_id != occurrence_id:
            raise UpperKaraRegistryLocalContractAdapterError(
                "target binding key and occurrence_id differ"
            )
        if binding.target_guid != target["target_guid"]:
            raise UpperKaraRegistryLocalContractAdapterError(
                "target binding GUID differs from registry metadata"
            )
        if binding.identity_kind != "REGISTRY_CREATURE_OCCURRENCE":
            raise UpperKaraRegistryLocalContractAdapterError(
                "registry target binding has a nonregistry identity kind"
            )
        if binding.creature_entry_id != target["creature_entry_id"]:
            raise UpperKaraRegistryLocalContractAdapterError(
                "target binding creature entry differs from registry metadata"
            )
        ordered_bindings.append(binding)

    allowed = tuple(target_contract["allowed_primary_occurrence_ids"])
    collateral = tuple(target_contract["collateral_occurrence_ids"])
    ordered = _total_priority_order_v1(
        allowed, target_contract["priority_partial_order"]
    )
    direct_mode = "ANY_LEGAL" if ordered is None else "FIXED_SEQUENCE"
    direct_targets = allowed if ordered is None else ordered

    is_boss_encounter = encounter["metadata_boss_flag"] is True
    boss_target_ids = {
        row["occurrence_id"]
        for row in encounter_targets
        if row["metadata_boss_flag"] is True
    }
    stage_role = (
        "PULL"
        if not is_boss_encounter
        else "BOSS"
        if set(direct_targets) & boss_target_ids
        else "ADDS"
    )
    unit_kind = "BOSS_PHASE" if is_boss_encounter else "ROUTE_PULL"
    resources_tuple = tuple(resources)
    stage = TargetStageV1(
        stage_id=phase["phase_ref"],
        stage_role=stage_role,
        direct_target_mode=direct_mode,
        direct_target_occurrence_ids=direct_targets,
        collateral_target_occurrence_ids=collateral,
    )
    return UpperKaraWaveLocalSearchContractV1(
        campaign_id=campaign_id,
        search_unit=WaveSearchUnitV1(
            unit_id=f"{encounter['pull_slot_id']}:{phase['phase_ordinal']:03d}",
            unit_kind=unit_kind,
            route_ref=instance["route_variant_id"],
            pull_or_phase_ref=phase["phase_ref"],
        ),
        frozen_player=frozen_player,
        targets=tuple(ordered_bindings),
        target_stages=(stage,),
        variant_budget=variant_budget,
        baselines=tuple(baselines),
        train_seeds=train_seeds,
        selection_seeds=selection_seeds,
        heldout_seeds=heldout_seeds,
        resources=resources_tuple,
        resource_availability_cases=resource_availability_cases_v1(resources_tuple),
    )


__all__ = [
    "UpperKaraRegistryLocalContractAdapterError",
    "registry_phase_to_local_search_contract_v1",
    "resource_availability_cases_v1",
]
