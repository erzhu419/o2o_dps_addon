"""Search route-level burst packages for one exact Upper Kara cell.

The selected loadout owns consumable availability, the exact native action
snapshot owns action identities and build-modified cooldown durations, and the
finite inner search owns the complete action schedule.  Contra contributes an
inventory and proposal order only: neither its manual trigger aura nor its
Boss-HP condition is an execution constraint.

Only long cooldowns and explicitly finite consumables become route resources.
Short cooldowns and ordinary actions are deliberately omitted from the
resource mask, so they remain available in the no-resource baseline and every
resource arm.  Mutually exclusive or unsupported loadout actions are masked in
every arm if a native runtime unexpectedly exposes them.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .causal_guard_v1 import ObservableCausalGuardV1
from .contra_fury_burst_inventory_v1 import (
    ContraFuryBurstInventoryV1,
    build_contra_fury_burst_inventory_v1,
    resolve_contra_fury_burst_guide_v1,
)
from .contra_turtle_burst_loadout_v1 import (
    ContraTurtleBurstLoadoutV1,
    build_contra_turtle_burst_loadouts_v1,
    build_upper_kara_contra_burst_loadout_case_v1,
)
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_wave_panel_v1 import DEFAULT_BINDING
from .fury_chronicle_prior import DEFAULT_MODEL as DEFAULT_OFFLINE_GUIDE
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .raid_cooldown_schedule_v1 import EncounterCooldownCellV1
from .sim_bridge import ActionRef, AvailableAction
from .simulator_cooldown_binding_v1 import (
    ContraSimulatorResourceSpecProjectionV1,
    SimulatorCooldownBindingResolutionV1,
    TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1,
    exact_trinket_item_ids_from_request_v1,
    project_contra_turtle_resource_specs_for_request_v1,
    resolve_simulator_cooldown_bindings_v1,
)
from .upper_kara_exact_cell_case_v1 import (
    UPPER_KARA_HISTORICAL_FURY_RANKS,
    UPPER_KARA_WAVE_STRATA,
    search_cell_from_upper_kara_case_v1,
)
from .wave_action_guides_v1 import CallableActionGuideV1
from .wave_action_schedule_v1 import EquipmentAction, SearchCellIdentity
from .wave_action_sequence_pilot_v1 import (
    DEFAULT_BRIDGE,
    DEFAULT_BRIDGE_CWD,
    DEFAULT_EXPERT_GUIDES,
    build_expert_action_guides_v1,
)
from .wave_action_sequence_search_v1 import (
    ActionGuideV1,
    NativeDynamicV3ScheduleReplayV1,
    ScheduleReplayV1,
)
from .wave_cooldown_package_measurement_v1 import CooldownActionBindingV1
from .wave_cooldown_package_search_v1 import (
    WaveCooldownPackageSearchResultV1,
    enumerate_cooldown_resource_arms_v1,
    search_wave_cooldown_packages_v1,
)
from .wave_action_sequence_remote_contract_v1 import (
    MANIFEST_SCHEMA as REMOTE_MANIFEST_SCHEMA,
    MIN_SEEDS_PER_CELL,
    UPPER_KARA_CASE_BUILDER,
)


JSONMap = dict[str, Any]
LONG_COOLDOWN_MIN_MS_V1 = 90_000
FINITE_CONSUMABLE_CATEGORIES_V1 = frozenset(
    {"POTION", "CONSUMABLE", "ENGINEERING_EXPLOSIVE"}
)
POTION_RESOURCE_IDS_V1 = frozenset(
    {
        "item.mighty_rage_potion",
        "item.rage_potion",
        "item.quickness_potion",
    }
)
BURST_LOADOUT_IDS_V1 = (
    "contra_turtle_burst__no_potion",
    "contra_turtle_burst__mighty_rage",
    "contra_turtle_burst__rage",
    "contra_turtle_burst__quickness",
)


class UpperKaraBurstPackageSearchV1Error(RuntimeError):
    """The driver could not produce an allocator-eligible encounter cell."""


@dataclass(frozen=True)
class BurstRouteResourceDecisionV1:
    resource_id: str
    source_category: str
    action: ActionRef | None
    native_label: str | None
    native_legal_at_snapshot: bool | None
    native_ready_in_ms: int | None
    native_cooldown_ms: int | None
    route_resource_included: bool
    disposition: str
    reason: str
    remains_available_in_every_arm: bool

    def to_dict(self) -> JSONMap:
        return {
            "resource_id": self.resource_id,
            "source_category": self.source_category,
            "action": self.action.to_wire() if self.action is not None else None,
            "native_label": self.native_label,
            "native_legal_at_snapshot": self.native_legal_at_snapshot,
            "native_ready_in_ms": self.native_ready_in_ms,
            "native_cooldown_ms": self.native_cooldown_ms,
            "route_resource_included": self.route_resource_included,
            "disposition": self.disposition,
            "reason": self.reason,
            "remains_available_in_every_arm": (
                self.remains_available_in_every_arm
            ),
        }


@dataclass(frozen=True)
class BurstRouteResourceSelectionV1:
    decisions: tuple[BurstRouteResourceDecisionV1, ...]
    route_bindings: tuple[CooldownActionBindingV1, ...]
    always_disabled_bindings: tuple[CooldownActionBindingV1, ...]
    native_snapshot_action_count: int
    long_cooldown_min_ms: int

    @property
    def search_constraint_bindings(self) -> tuple[CooldownActionBindingV1, ...]:
        """Actions masked by resource arms, including loadout exclusions."""

        return (*self.route_bindings, *self.always_disabled_bindings)

    def to_dict(self) -> JSONMap:
        return {
            "schema": "upper_kara_burst_route_resource_selection/v1",
            "long_cooldown_min_ms": self.long_cooldown_min_ms,
            "native_snapshot_action_count": self.native_snapshot_action_count,
            "route_resource_ids": [
                row.resource_id for row in self.route_bindings
            ],
            "always_disabled_resource_ids": [
                row.resource_id for row in self.always_disabled_bindings
            ],
            "decisions": [row.to_dict() for row in self.decisions],
            "contract": {
                "route_resources_are_long_cd_or_explicitly_finite": True,
                "short_cd_and_ordinary_actions_remain_in_every_arm": True,
                "loadout_exclusions_are_never_enabled_by_an_arm": True,
                "native_snapshot_owns_availability_and_cooldown": True,
            },
        }


@dataclass(frozen=True)
class UpperKaraBurstPackageSearchResultV1:
    representative_rank: int
    stratum: str
    loadout: ContraTurtleBurstLoadoutV1
    first_seed: int
    search_cell: SearchCellIdentity
    native_snapshot: tuple[AvailableAction, ...]
    projection: ContraSimulatorResourceSpecProjectionV1
    native_resolution: SimulatorCooldownBindingResolutionV1
    resource_selection: BurstRouteResourceSelectionV1
    package_search: WaveCooldownPackageSearchResultV1
    guide_ids: tuple[str, ...]
    snapshot_authority: str
    replay_authority: str

    @property
    def planner_cell(self) -> EncounterCooldownCellV1:
        return self.package_search.planner_cell

    def to_dict(self) -> JSONMap:
        route_state_required = any(
            row.status == "PAIRED_MEASURED_ROUTE_STATE_REQUIRED"
            for row in self.package_search.arms
        )
        return {
            "schema": "upper_kara_burst_package_search/v1",
            "status": "COMPLETE_PAIRED_PACKAGE_SEARCH",
            "route_allocation_status": (
                "REQUIRES_CROSS_ENCOUNTER_STATE_AWARE_REPLAY"
                if route_state_required
                else "ELIGIBLE_FOR_ADDITIVE_COOLDOWN_ALLOCATION"
            ),
            "selector": {
                "representative_rank": self.representative_rank,
                "stratum": self.stratum,
                "loadout_id": self.loadout.loadout_id,
                "first_seed": self.first_seed,
            },
            "search_cell": self.search_cell.to_dict(),
            "loadout": self.loadout.to_dict(),
            "native_snapshot": [
                {
                    "index": row.index,
                    "action": row.action.to_wire(),
                    "label": row.label,
                    "legal": row.legal,
                    "ready_in_ms": row.ready_in_ms,
                    "triggers_gcd": row.triggers_gcd,
                    "cooldown_duration_ms": row.cooldown_duration_ms,
                }
                for row in self.native_snapshot
            ],
            "snapshot_authority": self.snapshot_authority,
            "replay_authority": self.replay_authority,
            "contra_projection": self.projection.to_dict(),
            "native_resolution": self.native_resolution.to_dict(),
            "resource_selection": self.resource_selection.to_dict(),
            "guide_ids": list(self.guide_ids),
            "package_search": self.package_search.to_dict(),
            "planner_cell": {
                "encounter_id": self.planner_cell.encounter_id,
                "encounter_kind": self.planner_cell.encounter_kind,
                "raid_start_ms": self.planner_cell.raid_start_ms,
                "packages": [
                    row.to_dict() for row in self.planner_cell.packages
                ],
            },
            "contract": {
                "contra_manual_trigger_enforced": False,
                "contra_boss_hp_threshold_enforced": False,
                "contra_role": "GUIDE_AND_ACTION_IDENTITY_ONLY",
                "inner_search_owns_sequence_target_guard_and_timing": True,
                "no_resource_baseline_is_paired_on_same_seeds": True,
                "failure_emits_no_encounter_cooldown_cell": True,
                "persistent_effect_isolated_marginal_used_as_route_value": False,
            },
            "coverage": {
                "status": "EXACT_LOADOUT_NATIVE_SNAPSHOT_SCOPED",
                "not_claimed_complete_all_burst": True,
                "known_blockers": [
                    "Goblin Sapper remains absent when the exact native build does not register action item 10646",
                    *(
                        [
                            "Rapid Growth packages require cross-encounter state-aware replay before route allocation"
                        ]
                        if route_state_required
                        else []
                    ),
                ],
            },
        }


def _find_loadout_v1(
    request: Mapping[str, Any], loadout_id: str
) -> ContraTurtleBurstLoadoutV1:
    rows = {
        row.loadout_id: row
        for row in build_contra_turtle_burst_loadouts_v1(request)
    }
    try:
        return rows[loadout_id]
    except KeyError as error:
        raise ValueError(f"unknown Contra Turtle burst loadout {loadout_id!r}") from error


def select_burst_route_resources_v1(
    loadout: ContraTurtleBurstLoadoutV1,
    inventory: ContraFuryBurstInventoryV1,
    projection: ContraSimulatorResourceSpecProjectionV1,
    native_resolution: SimulatorCooldownBindingResolutionV1,
    native_snapshot: Sequence[AvailableAction],
    *,
    long_cooldown_min_ms: int = LONG_COOLDOWN_MIN_MS_V1,
) -> BurstRouteResourceSelectionV1:
    """Classify every Contra-derived action for route planning.

    Positive native bindings for a non-selected potion or an explicitly
    unsupported loadout action are kept only as permanent masks.  This makes
    potion mutual exclusion true even if a bridge exposes a broader inventory
    than the exact request intended.
    """

    if not isinstance(loadout, ContraTurtleBurstLoadoutV1):
        raise TypeError("loadout must be ContraTurtleBurstLoadoutV1")
    if not isinstance(inventory, ContraFuryBurstInventoryV1):
        raise TypeError("inventory must be ContraFuryBurstInventoryV1")
    if not isinstance(projection, ContraSimulatorResourceSpecProjectionV1):
        raise TypeError("projection must be ContraSimulatorResourceSpecProjectionV1")
    if not isinstance(native_resolution, SimulatorCooldownBindingResolutionV1):
        raise TypeError("native_resolution must be SimulatorCooldownBindingResolutionV1")
    if type(long_cooldown_min_ms) is not int or long_cooldown_min_ms <= 0:
        raise ValueError("long_cooldown_min_ms must be a positive integer")
    snapshot = tuple(native_snapshot)
    if any(not isinstance(row, AvailableAction) for row in snapshot):
        raise TypeError("native_snapshot must contain AvailableAction values")

    source_by_id = {row.action_id: row for row in inventory.actions}
    spec_by_id = {row.resource_id: row for row in projection.resource_specs}
    binding_by_id = {
        row.resource_id: row for row in native_resolution.bindings
    }
    snapshot_by_action: dict[ActionRef, AvailableAction] = {}
    for row in snapshot:
        snapshot_by_action.setdefault(row.action, row)

    decisions: list[BurstRouteResourceDecisionV1] = []
    route_bindings: list[CooldownActionBindingV1] = []
    disabled_bindings: list[CooldownActionBindingV1] = []
    unsupported = frozenset(loadout.unsupported_source_action_ids)
    for source in inventory.actions:
        resource_id = source.action_id
        spec = spec_by_id.get(resource_id)
        action = spec.action if spec is not None else None
        native = snapshot_by_action.get(action) if action is not None else None
        binding = binding_by_id.get(resource_id)

        loadout_excluded = (
            resource_id in POTION_RESOURCE_IDS_V1
            and resource_id != loadout.potion_resource_id
        )
        explicitly_unsupported = resource_id in unsupported
        if loadout_excluded:
            disposition = "EXCLUDED_MUTUALLY_EXCLUSIVE_LOADOUT"
            reason = (
                "the exact request selected a different default potion; this "
                "action is disabled in the baseline and every resource arm"
            )
            include = False
            remains = False
            if binding is not None:
                disabled_bindings.append(binding)
        elif explicitly_unsupported:
            disposition = "EXCLUDED_LOADOUT_UNSUPPORTED"
            reason = (
                "the current executable loadout does not implement this "
                "Contra-derived action"
            )
            include = False
            remains = False
            if binding is not None:
                disabled_bindings.append(binding)
        elif spec is None:
            disposition = "EXCLUDED_IDENTITY_UNRESOLVED"
            reason = "the exact request/runtime did not resolve an ActionRef"
            include = False
            remains = False
        elif native is None:
            disposition = "EXCLUDED_NATIVE_ACTION_ABSENT"
            reason = "the action is absent from the first exact native snapshot"
            include = False
            remains = False
        elif binding is None:
            if source.category in FINITE_CONSUMABLE_CATEGORIES_V1:
                raise ValueError(
                    f"finite consumable {resource_id} is present but lacks a "
                    "positive native cooldown; route use cannot be bounded"
                )
            disposition = "EXCLUDED_NATIVE_COOLDOWN_NOT_POSITIVE"
            reason = (
                "native cooldown metadata is zero, so this ordinary action "
                "does not consume a route-level clock"
            )
            include = False
            remains = True
        elif source.category in FINITE_CONSUMABLE_CATEGORIES_V1:
            disposition = "INCLUDED_EXPLICITLY_FINITE_CONSUMABLE"
            reason = (
                "the selected loadout exposes a finite consumable and native "
                f"cooldown metadata is {binding.cooldown_ms} ms"
            )
            include = True
            remains = False
            route_bindings.append(binding)
        elif binding.cooldown_ms >= long_cooldown_min_ms:
            disposition = "INCLUDED_LONG_COOLDOWN"
            reason = (
                f"native cooldown {binding.cooldown_ms} ms meets the route "
                f"threshold {long_cooldown_min_ms} ms"
            )
            include = True
            remains = False
            route_bindings.append(binding)
        else:
            disposition = "EXCLUDED_SHORT_COOLDOWN_REMAINS_ORDINARY"
            reason = (
                f"native cooldown {binding.cooldown_ms} ms is below the route "
                f"threshold {long_cooldown_min_ms} ms; inner search retains it"
            )
            include = False
            remains = True

        decisions.append(BurstRouteResourceDecisionV1(
            resource_id=resource_id,
            source_category=source.category,
            action=action,
            native_label=native.label if native is not None else None,
            native_legal_at_snapshot=(native.legal if native is not None else None),
            native_ready_in_ms=(native.ready_in_ms if native is not None else None),
            native_cooldown_ms=(
                native.cooldown_duration_ms if native is not None else None
            ),
            route_resource_included=include,
            disposition=disposition,
            reason=reason,
            remains_available_in_every_arm=remains,
        ))

    unknown_projection = sorted(set(spec_by_id) - set(source_by_id))
    if unknown_projection:
        raise ValueError(
            "projection contains resources absent from Contra inventory: "
            + ", ".join(unknown_projection)
        )
    constrained = (*route_bindings, *disabled_bindings)
    if len({row.resource_id for row in constrained}) != len(constrained):
        raise ValueError("route selection produced duplicate resource IDs")
    if len({row.action for row in constrained}) != len(constrained):
        raise ValueError("route selection produced duplicate native actions")
    return BurstRouteResourceSelectionV1(
        decisions=tuple(decisions),
        route_bindings=tuple(route_bindings),
        always_disabled_bindings=tuple(disabled_bindings),
        native_snapshot_action_count=len(snapshot),
        long_cooldown_min_ms=long_cooldown_min_ms,
    )


def _load_first_native_snapshot_v1(
    case: DevelopmentPrecombatWaveCaseV1,
    bridge_factory: Callable[[], Any],
) -> tuple[AvailableAction, ...]:
    with bridge_factory() as bridge:
        bridge.load_dynamic_v3_precombat(
            case.request,
            case.dynamic_load.seed,
            case.dynamic_load.config,
            case.precombat,
        )
        snapshot = tuple(bridge.actions())
    if not snapshot:
        raise UpperKaraBurstPackageSearchV1Error(
            "NATIVE_SNAPSHOT_FAILED_CLOSED: actions snapshot is empty"
        )
    if any(not isinstance(row, AvailableAction) for row in snapshot):
        raise UpperKaraBurstPackageSearchV1Error(
            "NATIVE_SNAPSHOT_FAILED_CLOSED: invalid action row"
        )
    return snapshot


def _contra_burst_order_guide_v1(
    loadout: ContraTurtleBurstLoadoutV1,
    request: Mapping[str, Any],
    inventory: ContraFuryBurstInventoryV1,
) -> CallableActionGuideV1:
    named_refs = dict(TURTLE_NATIVE_ACTION_BY_CONTRA_ID_V1)
    for resource_id in POTION_RESOURCE_IDS_V1:
        if resource_id != loadout.potion_resource_id:
            named_refs.pop(resource_id, None)
    for resource_id in loadout.unsupported_source_action_ids:
        named_refs.pop(resource_id, None)
    trinkets = exact_trinket_item_ids_from_request_v1(request)

    def priorities(outcome: Any, prefix: Any) -> Mapping[ActionRef, float]:
        del prefix
        return resolve_contra_fury_burst_guide_v1(
            outcome.available_actions,
            named_action_refs=named_refs,
            trinket_item_ids=trinkets,
            inventory=inventory,
        ).action_priorities

    return CallableActionGuideV1(
        "contra_burst_inventory:source_order_conditions_not_enforced",
        priorities,
    )


def run_upper_kara_burst_package_search_v1(
    *,
    seeds: Sequence[int],
    representative_rank: int,
    stratum: str,
    loadout_id: str,
    encounter_id: str,
    encounter_kind: str,
    raid_start_ms: int,
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    pull_time_ms: int = 3_000,
    max_steps: int,
    beam_width: int,
    max_off_gcd_actions: int = 1,
    max_prefix_permutations: int | None = None,
    wait_ms: int = 100,
    guard_options: Sequence[ObservableCausalGuardV1] = (),
    max_expansions_per_node: int | None = None,
    replay_workers: int = 1,
    continuation_max_steps: int = 0,
    max_resources_per_arm: int | None = None,
    expert_guide_names: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    runtime_binding_path: Path = DEFAULT_BINDING,
    offline_guide_artifact_path: Path = DEFAULT_OFFLINE_GUIDE,
    equipment_actions: Sequence[EquipmentAction] = (),
    long_cooldown_min_ms: int = LONG_COOLDOWN_MIN_MS_V1,
    case_options: Mapping[str, Any] | None = None,
    case_factory: Callable[[int], DevelopmentPrecombatWaveCaseV1] | None = None,
    search_cell_factory: Callable[[Any], SearchCellIdentity] = (
        search_cell_from_upper_kara_case_v1
    ),
    bridge_factory: Callable[[], Any] | None = None,
    snapshot_loader: Callable[[DevelopmentPrecombatWaveCaseV1], Sequence[AvailableAction]] | None = None,
    replay: ScheduleReplayV1 | None = None,
    action_guides: Sequence[ActionGuideV1] | None = None,
) -> UpperKaraBurstPackageSearchResultV1:
    """Run one bounded exact-cell package search or raise without a cell."""

    normalized_seeds = tuple(seeds)
    if not normalized_seeds or any(
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        for seed in normalized_seeds
    ):
        raise ValueError("seeds must contain nonnegative integers")
    if len(set(normalized_seeds)) != len(normalized_seeds):
        raise ValueError("seeds must be unique")
    options = dict(case_options or {})
    if case_factory is None:
        def resolved_case_factory(seed: int) -> DevelopmentPrecombatWaveCaseV1:
            return build_upper_kara_contra_burst_loadout_case_v1(
                seed,
                representative_rank=representative_rank,
                stratum=stratum,
                loadout_id=loadout_id,
                pull_time_ms=pull_time_ms,
                **options,
            )
    else:
        resolved_case_factory = case_factory

    try:
        first_case = resolved_case_factory(normalized_seeds[0])
        if not isinstance(first_case, DevelopmentPrecombatWaveCaseV1):
            raise TypeError("case_factory must return DevelopmentPrecombatWaveCaseV1")
        selected = first_case.case_spec.get("burst_loadout")
        if not isinstance(selected, Mapping) or selected.get("loadout_id") != loadout_id:
            raise ValueError("first case is not bound to the requested loadout")
        loadout = _find_loadout_v1(first_case.request, loadout_id)
        cell = search_cell_factory(first_case)
        if not isinstance(cell, SearchCellIdentity):
            raise TypeError("search_cell_factory must return SearchCellIdentity")
    except Exception as error:
        raise UpperKaraBurstPackageSearchV1Error(
            f"CASE_BINDING_FAILED_CLOSED: {type(error).__name__}: {error}"
        ) from error

    resolved_bridge_factory = bridge_factory or (
        lambda: SimulatorBridgePrecombatV1(bridge_path, cwd=bridge_cwd)
    )
    try:
        snapshot = tuple(
            snapshot_loader(first_case)
            if snapshot_loader is not None
            else _load_first_native_snapshot_v1(
                first_case, resolved_bridge_factory
            )
        )
        if not snapshot or any(
            not isinstance(row, AvailableAction) for row in snapshot
        ):
            raise ValueError("native snapshot must contain AvailableAction rows")
        inventory = build_contra_fury_burst_inventory_v1()
        projection = project_contra_turtle_resource_specs_for_request_v1(
            first_case.request,
            inventory=inventory,
        )
        resolution = resolve_simulator_cooldown_bindings_v1(
            snapshot,
            resource_specs=projection.resource_specs,
        )
        selection = select_burst_route_resources_v1(
            loadout,
            inventory,
            projection,
            resolution,
            snapshot,
            long_cooldown_min_ms=long_cooldown_min_ms,
        )
    except Exception as error:
        raise UpperKaraBurstPackageSearchV1Error(
            f"NATIVE_RESOURCE_BINDING_FAILED_CLOSED: {type(error).__name__}: {error}"
        ) from error

    if action_guides is None:
        guides = (
            *build_expert_action_guides_v1(
                first_case,
                guide_names=expert_guide_names,
                runtime_binding_path=runtime_binding_path,
                offline_guide_artifact_path=offline_guide_artifact_path,
            ),
            _contra_burst_order_guide_v1(loadout, first_case.request, inventory),
        )
    else:
        guides = tuple(action_guides)
    resolved_replay = replay or NativeDynamicV3ScheduleReplayV1(
        resolved_bridge_factory,
        resolved_case_factory,
    )
    arms = enumerate_cooldown_resource_arms_v1(
        selection.route_bindings,
        max_resources_per_arm=max_resources_per_arm,
    )
    try:
        searched = search_wave_cooldown_packages_v1(
            resolved_replay,
            cell,
            encounter_id=encounter_id,
            encounter_kind=encounter_kind,
            raid_start_ms=raid_start_ms,
            seeds=normalized_seeds,
            bindings=selection.search_constraint_bindings,
            arms=arms,
            max_steps=max_steps,
            beam_width=beam_width,
            action_guides=guides,
            equipment_actions=equipment_actions,
            max_off_gcd_actions=max_off_gcd_actions,
            max_prefix_permutations=max_prefix_permutations,
            wait_ms=wait_ms,
            guard_options=guard_options,
            max_expansions_per_node=max_expansions_per_node,
            replay_workers=replay_workers,
            continuation_max_steps=continuation_max_steps,
        )
    except Exception as error:
        raise UpperKaraBurstPackageSearchV1Error(
            f"PACKAGE_SEARCH_FAILED_CLOSED: {type(error).__name__}: {error}"
        ) from error
    return UpperKaraBurstPackageSearchResultV1(
        representative_rank=representative_rank,
        stratum=stratum,
        loadout=loadout,
        first_seed=normalized_seeds[0],
        search_cell=cell,
        native_snapshot=snapshot,
        projection=projection,
        native_resolution=resolution,
        resource_selection=selection,
        package_search=searched,
        guide_ids=tuple(guide.guide_id for guide in guides),
        snapshot_authority=(
            "EXACT_NATIVE_BRIDGE_FIRST_SNAPSHOT"
            if snapshot_loader is None
            else "CALLER_SUPPLIED_SNAPSHOT"
        ),
        replay_authority=(
            "FRESH_EXACT_NATIVE_BRIDGE_REPLAY"
            if replay is None
            else "CALLER_SUPPLIED_REPLAY"
        ),
    )


def _seed_panel_v1(values: Sequence[int], label: str) -> tuple[int, ...]:
    panel = tuple(values)
    if len(panel) < MIN_SEEDS_PER_CELL or any(
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        for seed in panel
    ):
        raise ValueError(
            f"{label} must contain at least {MIN_SEEDS_PER_CELL} "
            "nonnegative integer seeds"
        )
    if len(set(panel)) != len(panel):
        raise ValueError(f"{label} seeds must be unique")
    return panel


def build_upper_kara_burst_package_campaign_manifest_v1(
    *,
    train_seeds: Sequence[int],
    evaluation_seeds: Sequence[int],
    pull_time_ms: int = 3_000,
    case_options: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Prepare 12 exact cells x four loadouts without running a search.

    ``seeds`` is the training/search panel consumed by the remote worker;
    ``evaluation_seeds`` is a disjoint held-out panel.  The exact cell identity
    is read from the fully loadout-bound first training seed.
    """

    train = _seed_panel_v1(train_seeds, "train_seeds")
    evaluation = _seed_panel_v1(evaluation_seeds, "evaluation_seeds")
    overlap = sorted(set(train) & set(evaluation))
    if overlap:
        raise ValueError("train_seeds and evaluation_seeds must be disjoint")
    if type(pull_time_ms) is not int or pull_time_ms <= 0:
        raise ValueError("pull_time_ms must be a positive integer")
    options = dict(case_options or {})
    unsupported_options = set(options) - {"attackability_branch"}
    if unsupported_options:
        raise ValueError(
            "campaign case_options cannot be represented by the remote "
            "Upper Kara builder: " + ", ".join(sorted(unsupported_options))
        )

    rows: list[JSONMap] = []
    for rank in UPPER_KARA_HISTORICAL_FURY_RANKS:
        for stratum in UPPER_KARA_WAVE_STRATA:
            for loadout_id in BURST_LOADOUT_IDS_V1:
                case = build_upper_kara_contra_burst_loadout_case_v1(
                    train[0],
                    representative_rank=rank,
                    stratum=stratum,
                    loadout_id=loadout_id,
                    pull_time_ms=pull_time_ms,
                    **options,
                )
                loadout = _find_loadout_v1(case.request, loadout_id)
                identity = search_cell_from_upper_kara_case_v1(case)
                suffix = loadout_id.split("__", 1)[1].replace("_", "-")
                rows.append({
                    "cell_id": (
                        f"upper-kara-rank-{rank:02d}-{stratum}-{suffix}"
                    ),
                    "status": "PREPARED_NOT_RUN",
                    "representative_rank": rank,
                    "stratum": stratum,
                    "burst_loadout_id": loadout_id,
                    "case_builder": UPPER_KARA_CASE_BUILDER,
                    "case_params": {
                        "representative_rank": rank,
                        "stratum": stratum,
                        "attackability_branch": options.get(
                            "attackability_branch", "full_wave"
                        ),
                        "precombat_self_actions": [
                            action.to_wire()
                            for action in loadout.precombat_self_actions
                        ],
                        "pull_time_ms": pull_time_ms,
                        "player_consumes": deepcopy(
                            dict(loadout.player_consumes)
                        ),
                    },
                    "search_cell": identity.to_dict(),
                    "seeds": list(train),
                    "evaluation_seeds": list(evaluation),
                })
    expected_count = (
        len(UPPER_KARA_HISTORICAL_FURY_RANKS)
        * len(UPPER_KARA_WAVE_STRATA)
        * len(BURST_LOADOUT_IDS_V1)
    )
    if (
        len(rows) != expected_count
        or len({row["cell_id"] for row in rows}) != expected_count
    ):
        raise AssertionError("burst campaign is not 12 exact cells x 4 loadouts")
    return {
        "schema": REMOTE_MANIFEST_SCHEMA,
        "campaign_schema": "upper_kara_burst_package_campaign_manifest/v1",
        "status": "PREPARED_NOT_RUN",
        "execution_evidence": False,
        "train_seed_count_per_cell": len(train),
        "evaluation_seed_count_per_cell": len(evaluation),
        "train_evaluation_seeds_disjoint": True,
        "exact_cell_count": (
            len(UPPER_KARA_HISTORICAL_FURY_RANKS)
            * len(UPPER_KARA_WAVE_STRATA)
        ),
        "loadout_count_per_exact_cell": len(BURST_LOADOUT_IDS_V1),
        "coverage_status": "EXACT_LOADOUT_NATIVE_SNAPSHOT_SCOPED",
        "known_blockers": [
            "Goblin Sapper action item 10646 may be absent from an exact native build",
        ],
        "cells": rows,
        "contract": {
            "row_seeds_are_train_search_seeds": True,
            "row_evaluation_seeds_are_held_out": True,
            "package_search_executed": False,
            "contra_manual_and_boss_hp_conditions_are_constraints": False,
        },
    }


def write_upper_kara_burst_package_campaign_manifest_v1(
    output_path: str | Path,
    *,
    train_seeds: Sequence[int],
    evaluation_seeds: Sequence[int],
    pull_time_ms: int = 3_000,
    case_options: Mapping[str, Any] | None = None,
) -> Path:
    output = Path(output_path).expanduser().resolve()
    payload = build_upper_kara_burst_package_campaign_manifest_v1(
        train_seeds=train_seeds,
        evaluation_seeds=evaluation_seeds,
        pull_time_ms=pull_time_ms,
        case_options=case_options,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


__all__ = (
    "BURST_LOADOUT_IDS_V1",
    "FINITE_CONSUMABLE_CATEGORIES_V1",
    "LONG_COOLDOWN_MIN_MS_V1",
    "POTION_RESOURCE_IDS_V1",
    "BurstRouteResourceDecisionV1",
    "BurstRouteResourceSelectionV1",
    "UpperKaraBurstPackageSearchResultV1",
    "UpperKaraBurstPackageSearchV1Error",
    "build_upper_kara_burst_package_campaign_manifest_v1",
    "run_upper_kara_burst_package_search_v1",
    "select_burst_route_resources_v1",
    "write_upper_kara_burst_package_campaign_manifest_v1",
)
