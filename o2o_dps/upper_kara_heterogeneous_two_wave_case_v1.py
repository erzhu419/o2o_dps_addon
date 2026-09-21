"""One heterogeneous, continuous Upper Kara two-wave development case.

The first wave is the frozen ``multi_two`` source model and the second wave is
the frozen ``single_long`` source model.  They are composed into one native
dynamic-v3 load.  Consequently the simulator, rather than Python summaries,
carries rage, cooldowns, auras, consumables, equipment, and proc state across
the wave boundary.

The source waves came from different raids and are not claimed to be adjacent
historical pulls.  Their HP, armor, attackability hypothesis, and team-damage
receipts are reused without inventing replacement values.  The complete
environment registry is control-plane metadata.  A separate introduction
registry is the only target registry admitted to the causal policy projector;
it contains neither HP nor source identity.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_wave_stratified_v1 import build_stratified_wave_case_v1
from .fury_contra_adapter_v2 import (
    ContraEvidenceKindV2,
    ContraFieldEvidenceV2,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .policy_observation_causal_projection_v1 import (
    CAUSAL_DAMAGE_RATE_SCHEMA_V1,
    CAUSAL_TARGET_VIEW_SCHEMA_V1,
    CAUSAL_TEAM_VIEW_SCHEMA_V1,
    CausalLiveStateProjectionV1,
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
    project_live_state_for_policy_v1,
)
from .sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from .sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from .sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


JSONMap = dict[str, Any]

_NO_TARGET_PASSTHROUGH_FIELDS = frozenset(
    {
        "armor_penetration",
        "auras",
        "autoattack_active",
        "current_cast",
        "damage_done",
        "equipment_slots",
        "finished",
        "gcd_remaining_ms",
        "health_current",
        "health_maximum",
        "melee_attack_power",
        "mh_swing_duration_ms",
        "mh_swing_remaining_ms",
        "moving",
        "needs_input",
        "oh_swing_remaining_ms",
        "power",
        "precombat",
        "queued_swing",
        "stance",
        "strength",
        "swing_queue",
        "time_ms",
    }
)

SCHEMA = "upper_kara_heterogeneous_continuous_two_wave_case/v1"
ENVIRONMENT_REGISTRY_SCHEMA = (
    "upper_kara_heterogeneous_two_wave_environment_registry/v1"
)
FIRST_WAVE_STRATUM = "multi_two"
SECOND_WAVE_STRATUM = "single_long"
COMPOSED_SOURCE_INSTANCE_ID = "upper-kara-composed-two-source-waves-v1"
COMPOSED_SOURCE_WAVE_REF = "multi_two->single_long"
COMPLETE_STATUS = "COMPLETE_DEVELOPMENT_HETEROGENEOUS_TWO_WAVE"
INCOMPLETE_STATUS = "INCOMPLETE_HETEROGENEOUS_TWO_WAVE_BLOCKED"
UNSEEN_UNTIL_ARRIVAL = "UNSEEN_UNTIL_ARRIVAL"
SEEN_OUT_OF_RANGE = "SEEN_OUT_OF_RANGE"
FIRST_WAVE_VISIBILITY_MODES = frozenset(
    {UNSEEN_UNTIL_ARRIVAL, SEEN_OUT_OF_RANGE}
)


class UpperKaraHeterogeneousTwoWaveCaseV1Error(RuntimeError):
    """The heterogeneous case or its policy projection is inconsistent."""


@dataclass(frozen=True)
class UpperKaraHeterogeneousTwoWaveBuildV1:
    """Fail-closed build result for the composed native environment."""

    status: str
    case: DevelopmentWaveCaseV1 | None
    environment_registry: JSONMap
    target_introduction_registry: TargetIntroductionRegistryV1 | None
    blockers: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status == COMPLETE_STATUS and self.case is not None

    def require_case(self) -> DevelopmentWaveCaseV1:
        if not self.complete or self.case is None:
            detail = "; ".join(self.blockers) or self.status
            raise UpperKaraHeterogeneousTwoWaveCaseV1Error(detail)
        return self.case


def _validate_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")


def _validate_arrival(first_wave_arrival_ms: int) -> None:
    if (
        isinstance(first_wave_arrival_ms, bool)
        or not isinstance(first_wave_arrival_ms, int)
        or first_wave_arrival_ms < 0
    ):
        raise ValueError("first_wave_arrival_ms must be a nonnegative integer")


def _shared_request_surface(request: Mapping[str, Any]) -> JSONMap:
    """Return player/sim/encounter fields that must agree before composition."""

    copied = deepcopy(dict(request))
    encounter = copied.get("encounter")
    if isinstance(encounter, dict):
        encounter.pop("targets", None)
        encounter.pop("duration", None)
    return copied


def _incomplete(
    *, first_wave_arrival_ms: int, visibility: str, blockers: tuple[str, ...]
) -> UpperKaraHeterogeneousTwoWaveBuildV1:
    return UpperKaraHeterogeneousTwoWaveBuildV1(
        status=INCOMPLETE_STATUS,
        case=None,
        environment_registry={
            "schema": ENVIRONMENT_REGISTRY_SCHEMA,
            "status": INCOMPLETE_STATUS,
            "control_plane_only": True,
            "policy_visible": False,
            "requested_first_wave_arrival_ms": first_wave_arrival_ms,
            "requested_first_wave_visibility": visibility,
        },
        target_introduction_registry=None,
        blockers=blockers,
    )


def _remap_background_events(
    source: tuple[BackgroundDamageEventV1, ...],
    *,
    wave_number: int,
    target_offset: int,
    time_offset_ms: int,
    catch_up_at_ms: int | None = None,
) -> list[tuple[int, int, str, float]]:
    """Remap one source schedule, optionally aggregating an unseen prefix."""

    caught: dict[int, float] = {}
    remapped: list[tuple[int, int, str, float]] = []
    for event in source:
        target_index = target_offset + event.target_index
        if catch_up_at_ms is not None and event.time_ms < catch_up_at_ms:
            caught[target_index] = caught.get(target_index, 0.0) + event.damage
            continue
        remapped.append(
            (
                time_offset_ms + event.time_ms,
                target_index,
                f"hetero-w{wave_number}:{event.event_id}",
                event.damage,
            )
        )
    if catch_up_at_ms is not None:
        for target_index, damage in sorted(caught.items()):
            remapped.append(
                (
                    time_offset_ms + catch_up_at_ms,
                    target_index,
                    f"hetero-w{wave_number}:unseen-team-catchup-t{target_index}",
                    damage,
                )
            )
    return remapped


def _background_events(
    first: DevelopmentWaveCaseV1,
    second: DevelopmentWaveCaseV1,
    *,
    second_wave_start_ms: int,
    first_wave_arrival_ms: int,
) -> tuple[BackgroundDamageEventV1, ...]:
    raw = _remap_background_events(
        first.dynamic_load.config.background_damage_events,
        wave_number=1,
        target_offset=0,
        time_offset_ms=0,
        catch_up_at_ms=(first_wave_arrival_ms or None),
    )
    raw.extend(
        _remap_background_events(
            second.dynamic_load.config.background_damage_events,
            wave_number=2,
            target_offset=len(first.dynamic_load.config.target_health),
            time_offset_ms=second_wave_start_ms,
        )
    )
    raw.sort(key=lambda row: (row[0], row[1], row[2]))
    return tuple(
        BackgroundDamageEventV1(
            schedule_index=index,
            time_ms=time_ms,
            target_index=target_index,
            event_id=event_id,
            damage=damage,
        )
        for index, (time_ms, target_index, event_id, damage) in enumerate(raw)
    )


def _attackability_events(
    first: DevelopmentWaveCaseV1,
    second: DevelopmentWaveCaseV1,
    *,
    second_wave_start_ms: int,
    first_wave_arrival_ms: int,
) -> tuple[DynamicAttackabilityEventV2, ...]:
    """Make both wave boundaries explicit instead of relying on target 0."""

    if first.dynamic_load.config.attackability_events:
        raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
            "the frozen full-wave multi_two source unexpectedly has "
            "attackability transitions"
        )
    if second.dynamic_load.config.attackability_events:
        raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
            "the frozen full-wave single_long source unexpectedly has "
            "attackability transitions"
        )
    raw: list[tuple[int, int, bool]] = []
    first_count = len(first.dynamic_load.config.target_health)
    for target_index in range(first_count):
        if first_wave_arrival_ms:
            raw.append((0, target_index, False))
            raw.append((first_wave_arrival_ms, target_index, True))
        else:
            # Explicit t=0 true rows let generic causal registries introduce
            # all members of the multi-target first wave, including target 1.
            raw.append((0, target_index, True))
    second_count = len(second.dynamic_load.config.target_health)
    for local_index in range(second_count):
        target_index = first_count + local_index
        raw.append((0, target_index, False))
        raw.append((second_wave_start_ms, target_index, True))
    raw.sort(key=lambda row: (row[0], row[1]))
    return tuple(
        DynamicAttackabilityEventV2(index, time_ms, target_index, attackable)
        for index, (time_ms, target_index, attackable) in enumerate(raw)
    )


def _effective_armor_events(
    first: DevelopmentWaveCaseV1,
    second: DevelopmentWaveCaseV1,
    *,
    second_wave_start_ms: int,
) -> tuple[DynamicEffectiveArmorEventV2, ...]:
    first_count = len(first.dynamic_load.config.target_health)
    raw = [
        (event.time_ms, event.target_index, event.effective_armor)
        for event in first.dynamic_load.config.effective_armor_events
    ]
    raw.extend(
        (
            second_wave_start_ms + event.time_ms,
            first_count + event.target_index,
            event.effective_armor,
        )
        for event in second.dynamic_load.config.effective_armor_events
    )
    raw.sort(key=lambda row: (row[0], row[1]))
    return tuple(
        DynamicEffectiveArmorEventV2(index, time_ms, target_index, armor)
        for index, (time_ms, target_index, armor) in enumerate(raw)
    )


def _introduction_registry(
    *,
    first_target_count: int,
    second_target_count: int,
    first_wave_arrival_ms: int,
    first_wave_visibility: str,
    second_wave_start_ms: int,
) -> TargetIntroductionRegistryV1:
    first_introduction = (
        first_wave_arrival_ms
        if first_wave_visibility == UNSEEN_UNTIL_ARRIVAL
        else 0
    )
    return TargetIntroductionRegistryV1(
        targets=tuple(
            TargetIntroductionV1(index, first_introduction)
            for index in range(first_target_count)
        )
        + tuple(
            TargetIntroductionV1(first_target_count + index, second_wave_start_ms)
            for index in range(second_target_count)
        )
    )


def _wave_environment_row(
    case: DevelopmentWaveCaseV1,
    *,
    wave_number: int,
    stratum: str,
    target_offset: int,
    start_ms: int,
    introductions: Mapping[int, int],
) -> JSONMap:
    hp = [row.health for row in case.dynamic_load.config.target_health]
    armor = list(case.case_spec["initial_state"]["target_base_armor"])
    target_ids = list(case.case_spec["required_target_ids"])
    per_target = case.case_spec["team_background"]["per_target"]
    return {
        "wave_number": wave_number,
        "stratum": stratum,
        "source_instance_id": case.case_spec["source_instance_id"],
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "source_capsule_sha256": case.case_spec["source_evidence"][
            "wave_capsule_sha256"
        ],
        "simulator_start_ms": start_ms,
        "source_horizon_ms": case.dynamic_load.config.idle_advance_horizon_ms,
        "targets": [
            {
                "source_local_target_index": local_index,
                "simulator_target_index": target_offset + local_index,
                "source_target_id": target_ids[local_index],
                "model_max_hp": hp[local_index],
                "model_base_armor": armor[local_index],
                "policy_introduced_at_ms": introductions[
                    target_offset + local_index
                ],
                "source_team_model_receipt": deepcopy(per_target[local_index]),
            }
            for local_index in range(len(hp))
        ],
    }


def build_upper_kara_heterogeneous_two_wave_case_v1(
    seed: int,
    *,
    first_wave_arrival_ms: int = 0,
    first_wave_visibility: str = UNSEEN_UNTIL_ARRIVAL,
) -> UpperKaraHeterogeneousTwoWaveBuildV1:
    """Compose ``multi_two`` then ``single_long`` in one dynamic-v3 load.

    Delayed arrival is complete only under ``UNSEEN_UNTIL_ARRIVAL``.  The
    bridge has one global attackability bit per target; therefore a visible,
    out-of-range player cannot be disabled while teammates continue applying
    each native background event.  That requested branch returns an explicit
    INCOMPLETE result rather than silently changing the observation model.
    """

    _validate_seed(seed)
    _validate_arrival(first_wave_arrival_ms)
    if first_wave_visibility not in FIRST_WAVE_VISIBILITY_MODES:
        raise ValueError(
            "first_wave_visibility must be UNSEEN_UNTIL_ARRIVAL or "
            "SEEN_OUT_OF_RANGE"
        )
    if first_wave_visibility == SEEN_OUT_OF_RANGE and first_wave_arrival_ms:
        return _incomplete(
            first_wave_arrival_ms=first_wave_arrival_ms,
            visibility=first_wave_visibility,
            blockers=(
                "DYNAMIC_V3_HAS_GLOBAL_TARGET_ATTACKABILITY_ONLY: it cannot "
                "keep a visible target taking per-event teammate damage while "
                "preventing only the player from attacking before arrival",
            ),
        )

    first, _ = build_stratified_wave_case_v1(seed, FIRST_WAVE_STRATUM)
    second, _ = build_stratified_wave_case_v1(seed, SECOND_WAVE_STRATUM)
    if _shared_request_surface(first.request) != _shared_request_surface(
        second.request
    ):
        return _incomplete(
            first_wave_arrival_ms=first_wave_arrival_ms,
            visibility=first_wave_visibility,
            blockers=(
                "SOURCE_WAVE_REQUESTS_DIFFER_OUTSIDE_TARGETS_AND_DURATION",
            ),
        )
    first_count = len(first.dynamic_load.config.target_health)
    second_count = len(second.dynamic_load.config.target_health)
    if (first_count, second_count) != (2, 1):
        return _incomplete(
            first_wave_arrival_ms=first_wave_arrival_ms,
            visibility=first_wave_visibility,
            blockers=(
                "FROZEN_SOURCE_CARDINALITY_CHANGED: expected multi_two=2 and "
                "single_long=1",
            ),
        )
    second_wave_start_ms = first.dynamic_load.config.idle_advance_horizon_ms
    if first_wave_arrival_ms >= second_wave_start_ms:
        raise ValueError(
            "first_wave_arrival_ms must precede the frozen first-wave horizon"
        )
    horizon_ms = (
        second_wave_start_ms
        + second.dynamic_load.config.idle_advance_horizon_ms
    )

    request = deepcopy(first.request)
    request["encounter"]["targets"].extend(
        deepcopy(second.request["encounter"]["targets"])
    )
    request["encounter"]["duration"] = horizon_ms / 1000.0

    target_health = tuple(
        DynamicTargetHealthV1(index, row.health)
        for index, row in enumerate(
            first.dynamic_load.config.target_health
            + second.dynamic_load.config.target_health
        )
    )
    config = DynamicTargetSemanticsConfigV3(
        target_health=target_health,
        idle_advance_horizon_ms=horizon_ms,
        background_damage_events=_background_events(
            first,
            second,
            second_wave_start_ms=second_wave_start_ms,
            first_wave_arrival_ms=first_wave_arrival_ms,
        ),
        attackability_events=_attackability_events(
            first,
            second,
            second_wave_start_ms=second_wave_start_ms,
            first_wave_arrival_ms=first_wave_arrival_ms,
        ),
        effective_armor_events=_effective_armor_events(
            first,
            second,
            second_wave_start_ms=second_wave_start_ms,
        ),
    )
    load = DynamicRolloutLoadV3.bind(request, seed, config)

    introductions = _introduction_registry(
        first_target_count=first_count,
        second_target_count=second_count,
        first_wave_arrival_ms=first_wave_arrival_ms,
        first_wave_visibility=first_wave_visibility,
        second_wave_start_ms=second_wave_start_ms,
    )
    introduction_by_index = {
        row.simulator_target_index: row.introduced_at_ms
        for row in introductions.targets
    }
    environment_registry = {
        "schema": ENVIRONMENT_REGISTRY_SCHEMA,
        "status": COMPLETE_STATUS,
        "control_plane_only": True,
        "policy_visible": False,
        "historical_adjacency_claimed": False,
        "composition": "FROZEN_SOURCE_MODEL_MULTI_TWO_THEN_SINGLE_LONG",
        "single_native_dynamic_v3_environment": True,
        "independent_wave_reset": False,
        "second_wave_start_source": "FIRST_WAVE_FROZEN_MODEL_HORIZON",
        "first_wave_arrival": {
            "arrival_ms": first_wave_arrival_ms,
            "visibility_semantics": first_wave_visibility,
            "pre_arrival_team_damage_representation": (
                "NATIVE_AGGREGATE_CATCHUP_AT_FIRST_OBSERVABLE_ARRIVAL"
                if first_wave_arrival_ms
                else "NATIVE_SOURCE_EVENTS"
            ),
        },
        "waves": [
            _wave_environment_row(
                first,
                wave_number=1,
                stratum=FIRST_WAVE_STRATUM,
                target_offset=0,
                start_ms=0,
                introductions=introduction_by_index,
            ),
            _wave_environment_row(
                second,
                wave_number=2,
                stratum=SECOND_WAVE_STRATUM,
                target_offset=first_count,
                start_ms=second_wave_start_ms,
                introductions=introduction_by_index,
            ),
        ],
    }

    equipment_evidence = ContraFieldEvidenceV2(
        ContraEvidenceKindV2.PINNED_STATIC_INPUT,
        source_sha256=load.request_sha256,
    )
    contexts = {
        index: replace(
            context,
            context_id=f"heterogeneous-wave1-{context.context_id}",
            equipment_evidence=equipment_evidence,
        )
        for index, context in first.target_contexts.items()
    }
    for local_index, context in second.target_contexts.items():
        target_index = first_count + local_index
        contexts[target_index] = replace(
            context,
            context_id=f"heterogeneous-wave2-{context.context_id}",
            target_index=target_index,
            target_name=request["encounter"]["targets"][target_index]["name"],
            equipment_evidence=equipment_evidence,
        )

    max_hp = list(first.case_spec["initial_state"]["target_max_hp"]) + list(
        second.case_spec["initial_state"]["target_max_hp"]
    )
    base_armor = list(
        first.case_spec["initial_state"]["target_base_armor"]
    ) + list(second.case_spec["initial_state"]["target_base_armor"])
    required_target_ids = list(first.case_spec["required_target_ids"]) + list(
        second.case_spec["required_target_ids"]
    )
    spec = deepcopy(first.case_spec)
    for key in ("source_evidence", "team_background"):
        spec.pop(key, None)
    spec.update(
        {
            "schema": SCHEMA,
            "scope": "MODEL_DEFINED_DEVELOPMENT_HETEROGENEOUS_TWO_WAVE",
            "historical_exact": False,
            "real_superiority_authorized": False,
            "deployment_authorized": False,
            "source_instance_id": COMPOSED_SOURCE_INSTANCE_ID,
            "source_instance_ids": [
                first.case_spec["source_instance_id"],
                second.case_spec["source_instance_id"],
            ],
            "source_wave_ref": COMPOSED_SOURCE_WAVE_REF,
            "source_wave_refs": [
                first.case_spec["source_wave_ref"],
                second.case_spec["source_wave_ref"],
            ],
            "source_target_ref": required_target_ids,
            "source_wave_model_target_count": len(required_target_ids),
            "required_target_ids": required_target_ids,
            "required_target_indices": list(range(len(required_target_ids))),
            "environment_registry": deepcopy(environment_registry),
            "continuous_route": {
                "wave_count": 2,
                "single_native_environment": True,
                "independent_wave_reset": False,
                "state_carried": [
                    "rage",
                    "cooldowns",
                    "auras",
                    "consumable_inventory",
                    "equipment",
                    "proc_state",
                ],
                "wave_2_start_ms": second_wave_start_ms,
                "wave_2_start_source": "FIRST_WAVE_FROZEN_MODEL_HORIZON",
            },
            "initial_state": {
                **deepcopy(first.case_spec["initial_state"]),
                "target_max_hp": max_hp,
                "target_current_hp": list(max_hp),
                "target_base_armor": base_armor,
                "target_attackable_at_ms": [
                    (
                        first_wave_arrival_ms
                        if target_index < first_count
                        else second_wave_start_ms
                    )
                    for target_index in range(len(required_target_ids))
                ],
                "target_placement": [
                    first.case_spec["initial_state"]["target_placement"],
                    second.case_spec["initial_state"]["target_placement"],
                ],
            },
            "team_background": {
                "model": "TWO_FROZEN_SOURCE_LEAVE_ONE_OUT_RATE_MODELS",
                "future_schedule_policy_visible": False,
                "waves": [
                    deepcopy(first.case_spec["team_background"]),
                    deepcopy(second.case_spec["team_background"]),
                ],
                "first_wave_pre_arrival_damage": (
                    "AGGREGATED_AT_FIRST_OBSERVABLE_ARRIVAL"
                    if first_wave_arrival_ms
                    else "SOURCE_EVENT_TIMES"
                ),
            },
            "policy_observation": {
                "projection": "NATIVE_DYNAMIC_V3_CURRENT_PREFIX_ONLY",
                "environment_registry_passed_to_policy": False,
                "raw_simulator_target_registry_passed_to_policy": False,
                "target_introduction_registry_control_plane_only": (
                    introductions.to_wire()
                ),
                "target_hp_captured": (
                    "FROM_LIVE_PREFIX_AT_OR_AFTER_TARGET_INTRODUCTION"
                ),
                "source_identity_passed_to_policy": False,
                "future_wave_start_passed_to_policy": False,
            },
            "terminal": {
                "success": (
                    "ALL_THREE_REQUIRED_TARGETS_DEAD_IN_ONE_NATIVE_ENVIRONMENT"
                ),
                "watchdog_ms": horizon_ms,
                "watchdog_outcome": "CENSORED_WATCHDOG_NOT_COMPLETE",
            },
            "limitations": [
                "COMPOSED_SOURCE_WAVES_NOT_OBSERVED_AS_ADJACENT_PULLS",
                "KILL_BUDGET_HP_MODELS_NOT_EXACT_MAX_HP",
                "TEAM_RATE_FROM_SOURCE_DIRECT_GUID_LEAVE_ONE_OUT",
                "FIRST_WAVE_PRE_ARRIVAL_DAMAGE_AGGREGATED_WHEN_DELAYED",
            ],
            "seed": seed,
            "request_sha256": load.request_sha256,
            "dynamic_load_contract_sha256": load.contract_sha256,
        }
    )
    case = DevelopmentWaveCaseV1(spec, request, load, contexts)
    return UpperKaraHeterogeneousTwoWaveBuildV1(
        status=COMPLETE_STATUS,
        case=case,
        environment_registry=environment_registry,
        target_introduction_registry=introductions,
    )


def target_introduction_registry_from_heterogeneous_case_v1(
    case: DevelopmentWaveCaseV1 | DevelopmentPrecombatWaveCaseV1,
) -> TargetIntroductionRegistryV1:
    """Read only the control-plane introduction rows, never environment HP."""

    if not isinstance(
        case, (DevelopmentWaveCaseV1, DevelopmentPrecombatWaveCaseV1)
    ):
        raise TypeError(
            "case must be DevelopmentWaveCaseV1 or "
            "DevelopmentPrecombatWaveCaseV1"
        )
    policy = case.case_spec.get("policy_observation")
    raw = (
        policy.get("target_introduction_registry_control_plane_only")
        if isinstance(policy, Mapping)
        else None
    )
    targets = raw.get("targets") if isinstance(raw, Mapping) else None
    if not isinstance(targets, list) or not targets:
        raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
            "case lacks a target introduction registry"
        )
    rows = []
    for row in targets:
        if not isinstance(row, Mapping):
            raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
                "target introduction registry row must be an object"
            )
        rows.append(
            TargetIntroductionV1(
                simulator_target_index=row.get("simulator_target_index"),
                introduced_at_ms=row.get("introduced_at_ms"),
            )
        )
    return TargetIntroductionRegistryV1(targets=tuple(rows))


def visible_simulator_target_indices_v1(
    case: DevelopmentWaveCaseV1, now_ms: int
) -> tuple[int, ...]:
    """Return prefix-visible indexes without reading HP or source identity."""

    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a nonnegative integer")
    registry = target_introduction_registry_from_heterogeneous_case_v1(case)
    return tuple(
        row.simulator_target_index
        for row in registry.targets
        if row.introduced_at_ms <= now_ms
    )


def _raw_target_rows(
    state: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
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
        raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
            "raw dynamic-v3 state lacks aligned target lifecycle rows"
        )
    return list(life), list(semantic)


def _no_visible_target_projection_v1(
    state: Mapping[str, Any], now_ms: int
) -> CausalLiveStateProjectionV1:
    """Return a constant non-target sentinel before the first introduction.

    Imported source sessions inspect current attackability before localizing a
    target.  The false sentinel therefore produces WAIT while exposing no raw
    HP, target count, source identity, or future unlock time.  Its private
    target map is empty so an unexpected target-setting decision fails closed.
    """

    projected = {
        key: deepcopy(state[key])
        for key in _NO_TARGET_PASSTHROUGH_FIELDS
        if key in state
    }
    projected["target_auras"] = {}
    projected.update(
        {
            "target_index": 0,
            "num_targets": 0,
            "total_target_count": 0,
            "target_health_known": False,
            "target_health": 1.0,
            "target_health_max": 1.0,
            "target_health_percent": 100.0,
            "execute_phase_20": False,
            "execute_phase_25": False,
            "execute_phase_35": False,
            "encounter_damage_taken": 0.0,
            "dynamic_team_background": {
                "schema": CAUSAL_TEAM_VIEW_SCHEMA_V1,
                "retarget_required": False,
                "simulated_damage_applied": 0.0,
                "background_damage_applied": 0.0,
                "combined_damage_applied": 0.0,
                "prefix_damage_rate": {
                    "schema": CAUSAL_DAMAGE_RATE_SCHEMA_V1,
                    "observation_start_ms": now_ms,
                    "observation_end_ms": now_ms,
                    "elapsed_ms": 0,
                    "simulated_damage": 0.0,
                    "background_damage": 0.0,
                    "combined_damage": 0.0,
                    "combined_damage_per_second": None,
                    "source_semantics": "NO_PREFIX_VISIBLE_TARGET_SENTINEL",
                },
                "targets": [
                    {
                        "target_index": 0,
                        "initial_health": 1.0,
                        "current_health": 1.0,
                        "dead": False,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 0.0,
                    }
                ],
            },
            "dynamic_target_semantics": {
                "schema": CAUSAL_TARGET_VIEW_SCHEMA_V1,
                "targets": [
                    {
                        "target_index": 0,
                        "attackable": False,
                        "maximum_health": 1.0,
                        "current_health": 1.0,
                        "dead": False,
                    }
                ],
            },
        }
    )
    return CausalLiveStateProjectionV1(
        state=projected,
        policy_to_simulator_target_index=(),
        visibility_cutoff_ms=now_ms,
    )


class HeterogeneousTwoWaveObservationProjectorV1:
    """Capture each target's HP only after its explicit introduction."""

    def __init__(
        self, case: DevelopmentWaveCaseV1 | DevelopmentPrecombatWaveCaseV1
    ) -> None:
        self._introductions = (
            target_introduction_registry_from_heterogeneous_case_v1(case)
        )
        self._baselines: dict[int, TargetHealthPrefixBaselineV1] = {}
        self._last_time_ms: int | None = None
        self._generation: object = None
        self._hidden_selected_target_substitutions = 0
        self._last_hidden_selected_target_index: int | None = None

    @property
    def target_introduction_registry(self) -> TargetIntroductionRegistryV1:
        return self._introductions

    @property
    def captured_target_indexes(self) -> tuple[int, ...]:
        return tuple(sorted(self._baselines))

    @property
    def hidden_selected_target_substitutions(self) -> int:
        return self._hidden_selected_target_substitutions

    @property
    def last_hidden_selected_target_index(self) -> int | None:
        return self._last_hidden_selected_target_index

    def _reset_if_new_load(self, state: Mapping[str, Any], now_ms: int) -> None:
        generation = state.get("environment_generation")
        generation_changed = (
            self._last_time_ms is not None
            and self._generation is not None
            and generation is not None
            and generation != self._generation
        )
        rewound = self._last_time_ms is not None and now_ms < self._last_time_ms
        if generation_changed or rewound:
            self._baselines.clear()
            self._hidden_selected_target_substitutions = 0
            self._last_hidden_selected_target_index = None
        self._generation = generation
        self._last_time_ms = now_ms

    def __call__(
        self, state: Mapping[str, Any], available_actions: tuple[Any, ...] = ()
    ) -> CausalLiveStateProjectionV1:
        del available_actions
        now = state.get("time_ms")
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
                "raw state time_ms must be a nonnegative integer"
            )
        self._reset_if_new_load(state, now)
        life_rows, semantic_rows = _raw_target_rows(state)
        introduction_by_index = {
            row.simulator_target_index: row.introduced_at_ms
            for row in self._introductions.targets
        }
        if set(introduction_by_index) != set(range(len(life_rows))):
            raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
                "target introduction registry differs from the loaded case"
            )
        visible = tuple(
            index
            for index in range(len(life_rows))
            if introduction_by_index[index] <= now
        )
        if not visible:
            if any(row.get("attackable") is not False for row in semantic_rows):
                raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
                    "a not-yet-introduced target must be raw-unattackable"
                )
            selected = state.get("target_index")
            self._hidden_selected_target_substitutions += 1
            self._last_hidden_selected_target_index = (
                selected if type(selected) is int else None
            )
            return _no_visible_target_projection_v1(state, now)
        for target_index in visible:
            if target_index in self._baselines:
                continue
            life = life_rows[target_index]
            semantic = semantic_rows[target_index]
            values = (
                life.get("current_health"),
                semantic.get("maximum_health", life.get("initial_health")),
                life.get("simulated_damage_applied"),
                life.get("background_damage_applied"),
            )
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in values
            ):
                raise UpperKaraHeterogeneousTwoWaveCaseV1Error(
                    f"target {target_index} lacks numeric prefix HP counters"
                )
            self._baselines[target_index] = TargetHealthPrefixBaselineV1(
                simulator_target_index=target_index,
                observed_at_ms=now,
                current_health=float(values[0]),
                maximum_health=float(values[1]),
                simulated_damage_applied_at_observation=float(values[2]),
                background_damage_applied_at_observation=float(values[3]),
            )
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
        selected = state.get("target_index")
        projected_source: Mapping[str, Any] = state
        if selected not in visible:
            projected_source = deepcopy(dict(state))
            projected_source["target_index"] = visible[0]
            self._hidden_selected_target_substitutions += 1
            self._last_hidden_selected_target_index = (
                selected if type(selected) is int else None
            )
        return project_live_state_for_policy_v1(
            projected_source,
            self._introductions,
            TargetHealthPrefixRegistryV1(targets=health_rows),
        )


def build_heterogeneous_two_wave_observation_projector_v1(
    case: DevelopmentWaveCaseV1 | DevelopmentPrecombatWaveCaseV1,
) -> HeterogeneousTwoWaveObservationProjectorV1:
    return HeterogeneousTwoWaveObservationProjectorV1(case)


__all__ = (
    "COMPLETE_STATUS",
    "ENVIRONMENT_REGISTRY_SCHEMA",
    "FIRST_WAVE_STRATUM",
    "FIRST_WAVE_VISIBILITY_MODES",
    "HeterogeneousTwoWaveObservationProjectorV1",
    "INCOMPLETE_STATUS",
    "SCHEMA",
    "SECOND_WAVE_STRATUM",
    "SEEN_OUT_OF_RANGE",
    "UNSEEN_UNTIL_ARRIVAL",
    "UpperKaraHeterogeneousTwoWaveBuildV1",
    "UpperKaraHeterogeneousTwoWaveCaseV1Error",
    "build_heterogeneous_two_wave_observation_projector_v1",
    "build_upper_kara_heterogeneous_two_wave_case_v1",
    "target_introduction_registry_from_heterogeneous_case_v1",
    "visible_simulator_target_indices_v1",
)
