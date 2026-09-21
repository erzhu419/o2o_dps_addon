"""Freeze route-planner burst choices for held-out exact-cell replay.

Four request-level burst loadouts are searched independently for each exact
Upper Kara build x wave cell.  This module joins their paired-measured package
alternatives into one encounter cell, lets the raid cooldown allocator choose
between them, and maps that choice back to the complete searched action table.

The route allocator never sees Contra conditions or expert scores.  They may
have ordered inner-search proposals, but all guide metadata is removed before
held-out replay.  An encounter for which the allocator chooses no package uses
the no-resource schedule from the explicit no-potion loadout.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Mapping, Sequence

from .raid_cooldown_schedule_v1 import (
    CooldownPackageV1,
    EncounterCooldownCellV1,
    RaidCooldownScheduleV1,
    allocate_raid_cooldown_packages_v1,
)
from .route_timing_calibration_v1 import RouteTimingShiftProfileV1
from .upper_kara_burst_package_search_v1 import (
    BURST_LOADOUT_IDS_V1,
    UpperKaraBurstPackageSearchResultV1,
)
from .wave_action_schedule_v1 import ScheduledActionPlan, SearchCellIdentity
from .wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    ScheduleReplayV1,
)


JSONMap = dict[str, Any]
NO_POTION_LOADOUT_ID_V1 = "contra_turtle_burst__no_potion"
COMPLETE_SEARCH_V1 = "COMPLETE_PAIRED_SEED_SCHEDULE"
RAPID_GROWTH_RESOURCE_ID_V1 = "item.elixir_of_rapid_growth"
RAPID_GROWTH_POSITIVE_DURATION_MS_V1 = 120_000
RAPID_GROWTH_WITHDRAWAL_DURATION_MS_V1 = 120_000


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _json_object(value: object, label: str) -> JSONMap:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a JSON object string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must encode a JSON object")
    return parsed


def _core_cell_payload_v1(cell: SearchCellIdentity) -> JSONMap:
    """Remove only the declared request-level loadout differences.

    Upper Kara's exact cell identity embeds request consumes in two JSON-valued
    mechanics and embeds the precombat allowlist in its environment branch.
    Everything else -- including target HP/armor, attackability, build,
    talents, equipment and team model -- remains equality-constrained.
    """

    if not isinstance(cell, SearchCellIdentity):
        raise TypeError("cell must be SearchCellIdentity")
    mechanics: list[tuple[str, object]] = []
    for name, value in cell.derived_mechanics:
        if name in {"initial_state_json", "exact_build_context_json"}:
            normalized = _json_object(value, name)
            normalized.pop("consumes", None)
            mechanics.append((name, normalized))
        else:
            mechanics.append((name, value))
    environment = _json_object(
        cell.environment_branch_id, "environment_branch_id"
    )
    environment.pop("precombat", None)
    return {
        "scenario_id": cell.scenario_id,
        "wave_or_boss_id": cell.wave_or_boss_id,
        "exact_build_id": cell.exact_build_id,
        "talents": list(cell.talents),
        "equipment": list(cell.equipment),
        "derived_mechanics_without_consumes": mechanics,
        "environment_without_precombat": environment,
    }


def core_upper_kara_search_cell_key_v1(cell: SearchCellIdentity) -> str:
    """Stable identity shared only by allowed loadout/precombat branches."""

    return _canonical_json(_core_cell_payload_v1(cell))


def _pull_time_ms_v1(cell: SearchCellIdentity) -> int:
    environment = _json_object(
        cell.environment_branch_id, "environment_branch_id"
    )
    precombat = environment.get("precombat")
    if not isinstance(precombat, Mapping):
        raise ValueError("Upper Kara burst cell lacks precombat metadata")
    value = precombat.get("pull_time_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Upper Kara burst cell has invalid precombat pull_time_ms")
    return value


def _training_seeds_v1(result: UpperKaraBurstPackageSearchResultV1) -> tuple[int, ...]:
    search = result.package_search.baseline_search
    if search.status != COMPLETE_SEARCH_V1:
        raise ValueError(
            f"{result.loadout.loadout_id} no-resource baseline is incomplete"
        )
    seeds = tuple(row.seed for row in search.outcomes)
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError("no-resource baseline must contain unique training seeds")
    if result.first_seed != seeds[0]:
        raise ValueError("first_seed differs from the paired training panel")
    if any(row.status is not ReplayStatusV1.COMPLETE for row in search.outcomes):
        raise ValueError("no-resource baseline has a nonterminal training lane")
    return seeds


def _freeze_schedule_v1(
    schedule: Sequence[ScheduledActionPlan],
) -> tuple[ScheduledActionPlan, ...]:
    rows = tuple(schedule)
    if any(not isinstance(row, ScheduledActionPlan) for row in rows):
        raise TypeError("schedule must contain ScheduledActionPlan values")
    return tuple(
        replace(row, guide_provenance=(), guide_priority=0.0) for row in rows
    )


@dataclass(frozen=True)
class FrozenEncounterActionTableV1:
    encounter_id: str
    encounter_kind: str
    raid_start_ms: int
    loadout_id: str
    source_kind: str
    namespaced_package_id: str | None
    source_package_id: str | None
    pull_time_ms: int
    starting_equipment: tuple[tuple[str, int], ...]
    talents: tuple[tuple[str, int], ...]
    training_seeds: tuple[int, ...]
    schedule: tuple[ScheduledActionPlan, ...]

    def __post_init__(self) -> None:
        if not self.encounter_id or not self.loadout_id:
            raise ValueError("encounter_id and loadout_id must be nonempty")
        if self.source_kind not in {
            "PLANNER_SELECTED_RESOURCE_ARM",
            "NO_RESOURCE_NO_POTION_BASELINE",
        }:
            raise ValueError("unknown frozen action-table source_kind")
        paired_ids = self.namespaced_package_id is not None
        if paired_ids != (self.source_package_id is not None):
            raise ValueError("package identities must be both present or both absent")
        if self.source_kind == "PLANNER_SELECTED_RESOURCE_ARM" and not paired_ids:
            raise ValueError("selected resource arm requires package identities")
        if self.source_kind == "NO_RESOURCE_NO_POTION_BASELINE" and paired_ids:
            raise ValueError("no-resource baseline cannot carry a package identity")
        if type(self.pull_time_ms) is not int or self.pull_time_ms <= 0:
            raise ValueError("pull_time_ms must be positive")
        if not self.training_seeds or len(self.training_seeds) != len(
            set(self.training_seeds)
        ):
            raise ValueError("training_seeds must be nonempty and unique")
        if any(not isinstance(row, ScheduledActionPlan) for row in self.schedule):
            raise TypeError("schedule must contain ScheduledActionPlan values")
        if any(row.guide_provenance or row.guide_priority != 0.0 for row in self.schedule):
            raise ValueError("frozen schedule retains guide metadata")

    def to_dict(self) -> JSONMap:
        steps: list[JSONMap] = []
        for step in self.schedule:
            row = step.to_dict()
            row.pop("guide", None)
            relative = step.at_or_after_ms - self.pull_time_ms
            row["simulator_time_ms"] = step.at_or_after_ms
            row["relative_to_pull_ms"] = relative
            row["precombat"] = relative < 0
            steps.append(row)
        return {
            "encounter_id": self.encounter_id,
            "encounter_kind": self.encounter_kind,
            "raid_start_ms": self.raid_start_ms,
            "loadout_id": self.loadout_id,
            "source_kind": self.source_kind,
            "namespaced_package_id": self.namespaced_package_id,
            "source_package_id": self.source_package_id,
            "pull_time_ms": self.pull_time_ms,
            "starting_equipment": [
                {"slot": slot, "item_id": item_id}
                for slot, item_id in self.starting_equipment
            ],
            "talents": [
                {"talent": talent, "rank": rank}
                for talent, rank in self.talents
            ],
            "training_seeds": list(self.training_seeds),
            "steps": steps,
            "guide_metadata_removed": True,
        }


@dataclass(frozen=True)
class NamespacedBurstPackageScheduleV1:
    loadout_id: str
    source_package_id: str
    package: CooldownPackageV1
    action_table: FrozenEncounterActionTableV1


@dataclass(frozen=True)
class AggregatedBurstEncounterV1:
    core_cell_key: str
    planner_cell: EncounterCooldownCellV1
    training_seeds: tuple[int, ...]
    no_potion_baseline: FrozenEncounterActionTableV1
    package_schedules: tuple[NamespacedBurstPackageScheduleV1, ...]

    def package_schedule_by_id(self) -> dict[str, NamespacedBurstPackageScheduleV1]:
        return {row.package.package_id: row for row in self.package_schedules}


def aggregate_upper_kara_burst_loadouts_v1(
    results: Sequence[UpperKaraBurstPackageSearchResultV1],
) -> AggregatedBurstEncounterV1:
    """Aggregate exactly four loadout searches for one exact encounter cell."""

    rows = tuple(results)
    if any(not isinstance(row, UpperKaraBurstPackageSearchResultV1) for row in rows):
        raise TypeError(
            "results must contain UpperKaraBurstPackageSearchResultV1 values"
        )
    by_loadout = {row.loadout.loadout_id: row for row in rows}
    if len(by_loadout) != len(rows):
        raise ValueError("burst loadout IDs must be unique")
    if set(by_loadout) != set(BURST_LOADOUT_IDS_V1):
        raise ValueError("one result is required for each of the four burst loadouts")

    first = rows[0]
    core_key = core_upper_kara_search_cell_key_v1(first.search_cell)
    encounter_meta = (
        first.planner_cell.encounter_id,
        first.planner_cell.encounter_kind,
        first.planner_cell.raid_start_ms,
    )
    training_seeds = _training_seeds_v1(first)
    pull_time_ms = _pull_time_ms_v1(first.search_cell)
    for row in rows:
        if row.package_search.cell != row.search_cell:
            raise ValueError("package search returned a different search cell")
        if core_upper_kara_search_cell_key_v1(row.search_cell) != core_key:
            raise ValueError(
                "loadout searches differ outside consumes/precombat/loadout branch"
            )
        if (
            row.planner_cell.encounter_id,
            row.planner_cell.encounter_kind,
            row.planner_cell.raid_start_ms,
        ) != encounter_meta:
            raise ValueError("loadout searches have different encounter metadata")
        if _training_seeds_v1(row) != training_seeds:
            raise ValueError("loadout searches use different training seed panels")
        if _pull_time_ms_v1(row.search_cell) != pull_time_ms:
            raise ValueError("loadout searches use different precombat pull times")

    no_potion = by_loadout[NO_POTION_LOADOUT_ID_V1]
    baseline_table = FrozenEncounterActionTableV1(
        encounter_id=encounter_meta[0],
        encounter_kind=encounter_meta[1],
        raid_start_ms=encounter_meta[2],
        loadout_id=NO_POTION_LOADOUT_ID_V1,
        source_kind="NO_RESOURCE_NO_POTION_BASELINE",
        namespaced_package_id=None,
        source_package_id=None,
        pull_time_ms=pull_time_ms,
        starting_equipment=no_potion.search_cell.equipment,
        talents=no_potion.search_cell.talents,
        training_seeds=training_seeds,
        schedule=_freeze_schedule_v1(
            no_potion.package_search.baseline_search.schedule
        ),
    )

    namespaced_packages: list[CooldownPackageV1] = []
    bindings: list[NamespacedBurstPackageScheduleV1] = []
    for loadout_id in BURST_LOADOUT_IDS_V1:
        row = by_loadout[loadout_id]
        eligible_by_source_id = {}
        for arm in row.package_search.arms:
            if arm.status != "PAIRED_MEASURED_PLANNER_ELIGIBLE":
                continue
            if arm.measurement is None or arm.search.status != COMPLETE_SEARCH_V1:
                raise ValueError("planner-eligible arm lacks complete measured search")
            source_id = arm.measurement.package.package_id
            if source_id in eligible_by_source_id:
                raise ValueError("eligible arm package IDs must be unique per loadout")
            if tuple(outcome.seed for outcome in arm.search.outcomes) != training_seeds:
                raise ValueError("eligible arm uses a different training seed panel")
            if tuple(
                paired.seed for paired in arm.measurement.paired_rows
            ) != training_seeds:
                raise ValueError(
                    "eligible arm measurement uses a different training seed panel"
                )
            if any(
                outcome.status is not ReplayStatusV1.COMPLETE
                for outcome in arm.search.outcomes
            ):
                raise ValueError("planner-eligible arm has a nonterminal lane")
            eligible_by_source_id[source_id] = arm

        planner_by_source_id = {
            package.package_id: package for package in row.planner_cell.packages
        }
        if set(planner_by_source_id) != set(eligible_by_source_id):
            raise ValueError("planner packages do not match eligible measured arms")
        for source_id, source_package in planner_by_source_id.items():
            arm = eligible_by_source_id[source_id]
            if arm.measurement is None or arm.measurement.package != source_package:
                raise ValueError("planner package differs from its measured arm")
            namespaced_id = f"{loadout_id}::{source_id}"
            namespaced = replace(source_package, package_id=namespaced_id)
            table = FrozenEncounterActionTableV1(
                encounter_id=encounter_meta[0],
                encounter_kind=encounter_meta[1],
                raid_start_ms=encounter_meta[2],
                loadout_id=loadout_id,
                source_kind="PLANNER_SELECTED_RESOURCE_ARM",
                namespaced_package_id=namespaced_id,
                source_package_id=source_id,
                pull_time_ms=pull_time_ms,
                starting_equipment=row.search_cell.equipment,
                talents=row.search_cell.talents,
                training_seeds=training_seeds,
                schedule=_freeze_schedule_v1(arm.search.schedule),
            )
            namespaced_packages.append(namespaced)
            bindings.append(NamespacedBurstPackageScheduleV1(
                loadout_id=loadout_id,
                source_package_id=source_id,
                package=namespaced,
                action_table=table,
            ))
    if len({row.package_id for row in namespaced_packages}) != len(
        namespaced_packages
    ):
        raise AssertionError("namespaced burst package IDs still collide")

    return AggregatedBurstEncounterV1(
        core_cell_key=core_key,
        planner_cell=EncounterCooldownCellV1(
            encounter_id=encounter_meta[0],
            encounter_kind=encounter_meta[1],
            raid_start_ms=encounter_meta[2],
            packages=tuple(namespaced_packages),
        ),
        training_seeds=training_seeds,
        no_potion_baseline=baseline_table,
        package_schedules=tuple(bindings),
    )


@dataclass(frozen=True)
class RoutePersistentEffectWindowV1:
    resource_id: str
    source_encounter_id: str
    namespaced_package_id: str
    effect_id: str
    raid_start_ms: int
    raid_end_ms: int
    stat_deltas: tuple[tuple[str, int], ...]
    affected_later_encounter_ids: tuple[str, ...]

    def to_dict(self) -> JSONMap:
        return {
            "resource_id": self.resource_id,
            "source_encounter_id": self.source_encounter_id,
            "namespaced_package_id": self.namespaced_package_id,
            "effect_id": self.effect_id,
            "raid_start_ms": self.raid_start_ms,
            "raid_end_ms": self.raid_end_ms,
            "stat_deltas": dict(self.stat_deltas),
            "affected_later_encounter_ids": list(
                self.affected_later_encounter_ids
            ),
        }


@dataclass(frozen=True)
class FrozenBurstRoutePlanV1:
    planner_schedule: RaidCooldownScheduleV1
    encounters: tuple[FrozenEncounterActionTableV1, ...]
    baselines: tuple[FrozenEncounterActionTableV1, ...]
    persistent_effect_windows: tuple[RoutePersistentEffectWindowV1, ...]
    route_persistent_effects_resolved: bool

    def to_dict(self) -> JSONMap:
        return {
            "schema": "upper_kara_frozen_burst_route/v1",
            "planner_schedule": self.planner_schedule.to_dict(),
            "encounters": [row.to_dict() for row in self.encounters],
            "held_out_baselines": [row.to_dict() for row in self.baselines],
            "persistent_effect_windows": [
                row.to_dict() for row in self.persistent_effect_windows
            ],
            "route_persistent_effects_resolved": (
                self.route_persistent_effects_resolved
            ),
            "scientific_status": (
                "FROZEN_ROUTE_READY_FOR_HELD_OUT_REPLAY"
                if self.route_persistent_effects_resolved
                else "INCOMPLETE_ROUTE_PERSISTENT_EFFECT_CARRY_UNMODELED"
            ),
            "contract": {
                "every_route_encounter_has_a_frozen_action_table": True,
                "unassigned_encounter_uses_no_resource_no_potion_baseline": True,
                "selected_package_maps_to_complete_inner_search_schedule": True,
                "guide_metadata_removed_before_evaluation": True,
                "isolated_wave_marginal_treated_as_full_route_value": False,
            },
        }


def _rapid_growth_effect_windows_v1(
    planner: RaidCooldownScheduleV1,
    route_cells: Sequence[EncounterCooldownCellV1],
) -> tuple[RoutePersistentEffectWindowV1, ...]:
    """Expose the cross-wave Rapid Growth aura and withdrawal intervals.

    The current bridge replays each wave from a fresh request and therefore
    cannot inject these carried stats into the next exact wave.  Keeping the
    precise route intervals here lets the evaluation gate refuse a scientific
    route result instead of silently treating isolated-wave marginals as
    additive.
    """

    ordered = tuple(
        sorted(route_cells, key=lambda row: (row.raid_start_ms, row.encounter_id))
    )
    result: list[RoutePersistentEffectWindowV1] = []
    for assignment in planner.assignments:
        for use in assignment.package.uses:
            if use.resource_id != RAPID_GROWTH_RESOURCE_ID_V1:
                continue
            used_at = assignment.raid_start_ms + use.use_at_ms
            intervals = (
                (
                    "RAPID_GROWTH_POSITIVE",
                    used_at,
                    used_at + RAPID_GROWTH_POSITIVE_DURATION_MS_V1,
                    (("strength", 30),),
                ),
                (
                    "RAPID_GROWTH_WITHDRAWAL",
                    used_at + RAPID_GROWTH_POSITIVE_DURATION_MS_V1,
                    used_at
                    + RAPID_GROWTH_POSITIVE_DURATION_MS_V1
                    + RAPID_GROWTH_WITHDRAWAL_DURATION_MS_V1,
                    (("strength", -25), ("stamina", -25)),
                ),
            )
            for effect_id, start_ms, end_ms, stat_deltas in intervals:
                affected = tuple(
                    cell.encounter_id
                    for cell in ordered
                    if cell.encounter_id != assignment.encounter_id
                    and cell.raid_start_ms > assignment.raid_start_ms
                    and start_ms <= cell.raid_start_ms < end_ms
                )
                result.append(RoutePersistentEffectWindowV1(
                    resource_id=use.resource_id,
                    source_encounter_id=assignment.encounter_id,
                    namespaced_package_id=assignment.package.package_id,
                    effect_id=effect_id,
                    raid_start_ms=start_ms,
                    raid_end_ms=end_ms,
                    stat_deltas=stat_deltas,
                    affected_later_encounter_ids=affected,
                ))
    return tuple(result)


def allocate_and_freeze_upper_kara_burst_route_v1(
    encounters: Sequence[AggregatedBurstEncounterV1],
    *,
    boss_independent_groups: Sequence[str] = ("combat_potion",),
    initial_next_available_ms: Mapping[str, int] | None = None,
    route_time_shift_bounds: Mapping[
        tuple[str, str], tuple[int, int]
    ] | None = None,
    route_timing_profiles: Sequence[RouteTimingShiftProfileV1] | None = None,
) -> FrozenBurstRoutePlanV1:
    """Allocate route resources and freeze one full table for every encounter."""

    rows = tuple(encounters)
    if not rows or any(not isinstance(row, AggregatedBurstEncounterV1) for row in rows):
        raise TypeError("encounters must contain AggregatedBurstEncounterV1 values")
    encounter_ids = [row.planner_cell.encounter_id for row in rows]
    if len(encounter_ids) != len(set(encounter_ids)):
        raise ValueError("route encounter IDs must be unique")
    planner = allocate_raid_cooldown_packages_v1(
        (row.planner_cell for row in rows),
        boss_independent_groups=boss_independent_groups,
        initial_next_available_ms=initial_next_available_ms,
        route_time_shift_bounds=route_time_shift_bounds,
        route_timing_profiles=route_timing_profiles,
    )
    assignments = {row.encounter_id: row for row in planner.assignments}
    selected_tables: list[FrozenEncounterActionTableV1] = []
    baseline_tables: list[FrozenEncounterActionTableV1] = []
    for encounter in sorted(
        rows,
        key=lambda row: (
            row.planner_cell.raid_start_ms,
            row.planner_cell.encounter_id,
        ),
    ):
        baseline_tables.append(encounter.no_potion_baseline)
        assignment = assignments.get(encounter.planner_cell.encounter_id)
        if assignment is None:
            selected_tables.append(encounter.no_potion_baseline)
            continue
        binding = encounter.package_schedule_by_id().get(
            assignment.package.package_id
        )
        if binding is None or binding.package != assignment.package:
            raise RuntimeError("allocator selection lacks its inner-search schedule")
        selected_tables.append(binding.action_table)
    persistent_effects = _rapid_growth_effect_windows_v1(
        planner, tuple(row.planner_cell for row in rows)
    )
    return FrozenBurstRoutePlanV1(
        planner_schedule=planner,
        encounters=tuple(selected_tables),
        baselines=tuple(baseline_tables),
        persistent_effect_windows=persistent_effects,
        route_persistent_effects_resolved=not any(
            row.affected_later_encounter_ids for row in persistent_effects
        ),
    )


def _seed_panel_v1(values: Sequence[int]) -> tuple[int, ...]:
    seeds = tuple(values)
    if not seeds or any(
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
        for seed in seeds
    ):
        raise ValueError("evaluation_seeds must contain nonnegative integers")
    if len(seeds) != len(set(seeds)):
        raise ValueError("evaluation_seeds must be unique")
    return seeds


def _replay_lane_v1(
    replay: ScheduleReplayV1 | None,
    *,
    seed: int,
    table: FrozenEncounterActionTableV1,
) -> JSONMap:
    if replay is None:
        return {
            "status": "FAILED",
            "reason": "MISSING_EXACT_LOADOUT_REPLAY",
            "effective_damage": None,
            "elapsed_ms": None,
            "effective_dps": None,
        }
    try:
        outcome = replay.replay(seed, table.schedule)
        if not isinstance(outcome, ScheduleReplayOutcomeV1):
            raise TypeError("replay returned a non-ScheduleReplayOutcomeV1 value")
        if outcome.seed != seed:
            raise ValueError("replay returned a different seed")
        if outcome.status is not ReplayStatusV1.COMPLETE:
            return {
                "status": outcome.status.value,
                "reason": outcome.invalid_reason or "NONTERMINAL_HELD_OUT_REPLAY",
                "effective_damage": None,
                "elapsed_ms": None,
                "effective_dps": None,
            }
        damage = outcome.effective_damage
        elapsed = outcome.elapsed_ms
        if elapsed <= 0:
            raise ValueError("complete replay has nonpositive elapsed_ms")
        return {
            "status": "COMPLETE",
            "reason": None,
            "effective_damage": damage,
            "elapsed_ms": elapsed,
            "effective_dps": damage * 1000.0 / elapsed,
        }
    except Exception as error:
        return {
            "status": "FAILED",
            "reason": f"{type(error).__name__}: {error}",
            "effective_damage": None,
            "elapsed_ms": None,
            "effective_dps": None,
        }


def evaluate_frozen_upper_kara_burst_route_v1(
    plan: FrozenBurstRoutePlanV1,
    *,
    evaluation_seeds: Sequence[int],
    replay_by_encounter_loadout: Mapping[tuple[str, str], ScheduleReplayV1],
) -> JSONMap:
    """Replay selected and no-resource schedules on a disjoint held-out panel.

    A failed/missing/nonterminal lane makes the paired row incomplete.  Its
    metrics and delta stay ``None``; failure is never converted to zero damage.
    """

    if not isinstance(plan, FrozenBurstRoutePlanV1):
        raise TypeError("plan must be FrozenBurstRoutePlanV1")
    if not isinstance(replay_by_encounter_loadout, Mapping):
        raise TypeError("replay_by_encounter_loadout must be a mapping")
    seeds = _seed_panel_v1(evaluation_seeds)
    training = {
        seed
        for table in plan.encounters
        for seed in table.training_seeds
    }
    overlap = sorted(training & set(seeds))
    if overlap:
        raise ValueError(f"training and evaluation seeds overlap: {overlap}")
    if not plan.route_persistent_effects_resolved:
        return {
            "schema": "upper_kara_frozen_burst_route_held_out_eval/v1",
            "status": "INCOMPLETE_ROUTE_PERSISTENT_EFFECT_CARRY_UNMODELED",
            "evaluation_seeds": list(seeds),
            "training_evaluation_seeds_disjoint": True,
            "seed_rows": [],
            "summary": {
                "paired_seed_count": 0,
                "evaluation_seed_count": len(seeds),
                "mean_candidate_minus_baseline_route_effective_damage": None,
            },
            "persistent_effect_windows": [
                row.to_dict() for row in plan.persistent_effect_windows
            ],
            "contract": {
                "search_or_guides_invoked_on_evaluation_seeds": False,
                "candidate_action_tables_frozen_before_evaluation": True,
                "failed_lane_scored_as_zero": False,
                "failed_lane_metrics_are_null": True,
                "isolated_wave_rapid_growth_marginal_accepted_as_route_result": False,
            },
        }
    baseline_by_encounter = {
        row.encounter_id: row for row in plan.baselines
    }
    seed_rows: list[JSONMap] = []
    for seed in seeds:
        encounter_rows: list[JSONMap] = []
        for selected in plan.encounters:
            baseline = baseline_by_encounter[selected.encounter_id]
            candidate_lane = _replay_lane_v1(
                replay_by_encounter_loadout.get(
                    (selected.encounter_id, selected.loadout_id)
                ),
                seed=seed,
                table=selected,
            )
            baseline_lane = _replay_lane_v1(
                replay_by_encounter_loadout.get(
                    (baseline.encounter_id, baseline.loadout_id)
                ),
                seed=seed,
                table=baseline,
            )
            paired = bool(
                candidate_lane["status"] == "COMPLETE"
                and baseline_lane["status"] == "COMPLETE"
            )
            encounter_rows.append({
                "encounter_id": selected.encounter_id,
                "candidate_loadout_id": selected.loadout_id,
                "candidate_package_id": selected.namespaced_package_id,
                "candidate": candidate_lane,
                "no_resource_no_potion_baseline": baseline_lane,
                "paired_complete": paired,
                "candidate_minus_baseline_effective_damage": (
                    float(candidate_lane["effective_damage"])
                    - float(baseline_lane["effective_damage"])
                    if paired
                    else None
                ),
                "candidate_minus_baseline_elapsed_ms": (
                    int(candidate_lane["elapsed_ms"])
                    - int(baseline_lane["elapsed_ms"])
                    if paired
                    else None
                ),
            })
        seed_rows.append({
            "seed": seed,
            "encounters": encounter_rows,
            "route_paired_complete": all(
                row["paired_complete"] for row in encounter_rows
            ),
            "candidate_minus_baseline_route_effective_damage": (
                sum(
                    float(row["candidate_minus_baseline_effective_damage"])
                    for row in encounter_rows
                )
                if all(row["paired_complete"] for row in encounter_rows)
                else None
            ),
        })
    complete = all(row["route_paired_complete"] for row in seed_rows)
    route_deltas = [
        float(row["candidate_minus_baseline_route_effective_damage"])
        for row in seed_rows
        if row["candidate_minus_baseline_route_effective_damage"] is not None
    ]
    return {
        "schema": "upper_kara_frozen_burst_route_held_out_eval/v1",
        "status": (
            "COMPLETE_HELD_OUT_ROUTE_PAIRED_REPLAY"
            if complete
            else "INCOMPLETE_HELD_OUT_ROUTE_PAIRED_REPLAY"
        ),
        "evaluation_seeds": list(seeds),
        "training_evaluation_seeds_disjoint": True,
        "seed_rows": seed_rows,
        "summary": {
            "paired_seed_count": len(route_deltas),
            "evaluation_seed_count": len(seeds),
            "mean_candidate_minus_baseline_route_effective_damage": (
                sum(route_deltas) / len(route_deltas) if complete else None
            ),
        },
        "contract": {
            "search_or_guides_invoked_on_evaluation_seeds": False,
            "candidate_action_tables_frozen_before_evaluation": True,
            "failed_lane_scored_as_zero": False,
            "failed_lane_metrics_are_null": True,
        },
    }


__all__ = (
    "NO_POTION_LOADOUT_ID_V1",
    "AggregatedBurstEncounterV1",
    "FrozenBurstRoutePlanV1",
    "FrozenEncounterActionTableV1",
    "NamespacedBurstPackageScheduleV1",
    "RAPID_GROWTH_RESOURCE_ID_V1",
    "RoutePersistentEffectWindowV1",
    "aggregate_upper_kara_burst_loadouts_v1",
    "allocate_and_freeze_upper_kara_burst_route_v1",
    "core_upper_kara_search_cell_key_v1",
    "evaluate_frozen_upper_kara_burst_route_v1",
)
