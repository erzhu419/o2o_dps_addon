"""Compile one resolved Incantagos encounter into a dynamic-v4 dev case.

This adapter is intentionally narrow.  It accepts only the full encounter
checkpoint (offset zero), keeps observed leave-one-out bins fixed, and omits
school-incompatible team-only targets from the native target arrays.  The
result is executable development evidence, never comparison evidence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Any, Callable, Mapping, Sequence

from .fury_encounter_scenarios_v1 import ARMOR_STAT_INDEX, HEALTH_STAT_INDEX
from .sim_bridge import BackgroundDamageEventV1
from .sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from .sim_bridge_dynamic_v4 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
)
from .upper_kara_compact_encounter_model_v1 import (
    SCHEMA as COMPACT_SCHEMA,
    STATUS as COMPACT_STATUS,
)
from .upper_kara_incantagos_actionability_contract_v1 import (
    BOSS,
    COLLATERAL_ONLY_CANDIDATE,
    DIRECT_AND_COLLATERAL_CANDIDATE,
    SCHEMA as ACTIONABILITY_SCHEMA,
    STATUS as ACTIONABILITY_STATUS,
    TEAM_ONLY_SCHOOL_INCOMPATIBLE,
)
from .upper_kara_wave_local_search_contract_v1 import ObservedTargetStateV1
from .upper_kara_wave_target_gate_v1 import (
    REQUIRED_RETARGET_MODE_V1,
    ReactiveBossAddsTargetGateV1,
)


JSONMap = dict[str, Any]

SCHEMA = "upper_kara_resolved_incantagos_dynamic_v4_case/v1"
STATUS = "DEVELOPMENT_ONLY_FIXED_LOO_NOT_COMPARISON_AUTHORIZED"
FULL_ENCOUNTER_START_OFFSET_MS = 0


class UpperKaraResolvedDynamicV4AdapterV1Error(ValueError):
    """The resolved development inputs cannot be compiled without widening claims."""


@dataclass(frozen=True)
class ResolvedDynamicTargetIndexV1:
    target_index: int
    occurrence_id: str
    target_guid: str
    identity_kind: str
    creature_entry_id: int | None

    def to_dict(self) -> JSONMap:
        return {
            "target_index": self.target_index,
            "occurrence_id": self.occurrence_id,
            "target_guid": self.target_guid,
            "identity_kind": self.identity_kind,
            "creature_entry_id": self.creature_entry_id,
        }


@dataclass(frozen=True)
class ResolvedDynamicLoadV4:
    """Minimal load binding required by native v4 replay and target gates."""

    config: DynamicTargetSemanticsConfigV4

    def __post_init__(self) -> None:
        if not isinstance(self.config, DynamicTargetSemanticsConfigV4):
            raise TypeError("config must be DynamicTargetSemanticsConfigV4")


@dataclass(frozen=True)
class CompiledResolvedIncantagosDynamicV4CaseV1:
    request: JSONMap
    dynamic_config: DynamicTargetSemanticsConfigV4
    dynamic_load: ResolvedDynamicLoadV4
    occurrence_index_registry: tuple[ResolvedDynamicTargetIndexV1, ...]
    boss_index: int
    priority_add_indexes: tuple[int, ...]
    collateral_only_indexes: tuple[int, ...]
    optional_actionable_indexes: tuple[int, ...]
    team_only_sidecar: tuple[JSONMap, ...]
    observation_provider: Callable[
        [Mapping[str, Any]], Mapping[int, ObservedTargetStateV1]
    ]
    target_gate: ReactiveBossAddsTargetGateV1
    receipt: JSONMap


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be a list"
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be nonempty text"
        )
    return value.strip()


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be an integer"
        )
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be {qualifier}"
        )
    return value


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be numeric"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        qualifier = "positive finite" if positive else "nonnegative finite"
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be {qualifier}"
        )
    return result


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label, positive=True)


def _strict_json_copy(value: Mapping[str, Any], label: str) -> JSONMap:
    try:
        result = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} is not strict JSON: {error}"
        ) from error
    if not isinstance(result, dict):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must be a JSON object"
        )
    return result


def _unique_text_list(value: object, label: str) -> tuple[str, ...]:
    rows = _array(value, label)
    result = tuple(_text(row, f"{label} item") for row in rows)
    if len(result) != len(set(result)):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must contain unique values"
        )
    return result


def _comparison_false(value: Mapping[str, Any], label: str) -> None:
    boundaries = _mapping(value.get("scientific_boundaries"), f"{label}.scientific_boundaries")
    if boundaries.get("comparison_authorized") is not False:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            f"{label} must remain comparison-ineligible"
        )


def _merged_activity_windows(
    target: Mapping[str, Any], *, horizon_ms: int
) -> tuple[tuple[int, int], ...]:
    attackability = _mapping(
        target.get("attackability_model"), "target.attackability_model"
    )
    if attackability.get("descriptive_outcome_proxy") is not True:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "attackability must remain an explicit descriptive-outcome proxy"
        )
    raw_windows = _array(
        attackability.get("activity_windows"), "target activity windows"
    )
    if not raw_windows:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "target activity windows must not be empty"
        )
    windows: list[tuple[int, int]] = []
    for raw in raw_windows:
        row = _mapping(raw, "target activity window")
        start = _integer(row.get("start_offset_ms"), "activity start")
        end = _integer(row.get("end_offset_ms"), "activity end")
        if start > end or end > horizon_ms:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "activity window is reversed or exceeds the full encounter horizon"
            )
        windows.append((start, end))
    windows.sort()
    merged: list[tuple[int, int]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _latest_bin_window_intersection_ms(
    *, start_ms: int, end_ms_exclusive: int, windows: Sequence[tuple[int, int]]
) -> int | None:
    latest: int | None = None
    for window_start, window_end in windows:
        intersection_start = max(start_ms, window_start)
        intersection_end = min(end_ms_exclusive - 1, window_end)
        if intersection_start <= intersection_end:
            latest = intersection_end if latest is None else max(latest, intersection_end)
    return latest


def _compile_attackability_events(
    targets: Sequence[Mapping[str, Any]], *, horizon_ms: int
) -> tuple[tuple[DynamicAttackabilityEventV2, ...], tuple[tuple[tuple[int, int], ...], ...]]:
    raw: list[tuple[int, int, bool]] = []
    all_windows: list[tuple[tuple[int, int], ...]] = []
    for target_index, target in enumerate(targets):
        windows = _merged_activity_windows(target, horizon_ms=horizon_ms)
        all_windows.append(windows)
        raw.append((0, target_index, windows[0][0] == 0))
        for window_index, (start, end) in enumerate(windows):
            if start > 0:
                raw.append((start, target_index, True))
            if end < horizon_ms:
                raw.append((end + 1, target_index, False))
            if window_index + 1 < len(windows):
                next_start = windows[window_index + 1][0]
                if next_start <= end + 1:
                    raise AssertionError("activity windows were not merged")
    raw.sort(key=lambda row: (row[0], row[1]))
    return (
        tuple(
            DynamicAttackabilityEventV2(index, time_ms, target_index, attackable)
            for index, (time_ms, target_index, attackable) in enumerate(raw)
        ),
        tuple(all_windows),
    )


def _compile_background_events(
    targets: Sequence[Mapping[str, Any]],
    *,
    activity_windows: Sequence[Sequence[tuple[int, int]]],
    horizon_ms: int,
) -> tuple[tuple[BackgroundDamageEventV1, ...], list[JSONMap]]:
    raw_events: list[tuple[int, int, int, float]] = []
    target_receipts: list[JSONMap] = []
    for target_index, target in enumerate(targets):
        occurrence_id = _text(target.get("occurrence_id"), "target occurrence_id")
        team = _mapping(
            target.get("team_kill_clock_model"), "target team_kill_clock_model"
        )
        if team.get("kind") != "EXACT_ATTEMPT_BINNED_FOCAL_LEAVE_ONE_OUT":
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "native team schedule is not the exact-attempt focal leave-one-out model"
            )
        bins = _array(team.get("damage_bins"), "target damage bins")
        declared_total = _number(
            team.get("leave_one_out_positive_damage"),
            "target leave-one-out positive damage",
        )
        observed_total = 0.0
        compiled_total = 0.0
        event_count = 0
        for bin_index, raw_bin in enumerate(bins):
            row = _mapping(raw_bin, "target damage bin")
            start = _integer(row.get("start_offset_ms"), "damage bin start")
            end = _integer(
                row.get("end_offset_ms_exclusive"), "damage bin end"
            )
            if end <= start or start > horizon_ms:
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    "damage bin is empty, reversed, or begins after the encounter horizon"
                )
            # Reducer bins have a fixed width.  The final bin may therefore end
            # after the exact encounter horizon even though every contributing
            # event is in-range.  Clip only its placement interval; its full
            # leave-one-out total remains unchanged.
            placement_end_ms_exclusive = min(end, horizon_ms + 1)
            damage = _number(
                row.get("leave_one_out_positive_damage"),
                "bin leave-one-out positive damage",
            )
            observed_total += damage
            event_time = _latest_bin_window_intersection_ms(
                start_ms=start,
                end_ms_exclusive=placement_end_ms_exclusive,
                windows=activity_windows[target_index],
            )
            if event_time is None:
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    f"damage bin for {occurrence_id} does not intersect "
                    "an activity window"
                )
            if damage == 0:
                continue
            raw_events.append((event_time, target_index, bin_index, damage))
            compiled_total += damage
            event_count += 1
        if observed_total != declared_total or compiled_total != declared_total:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                f"leave-one-out bins do not close exactly for {occurrence_id}"
            )
        target_receipts.append(
            {
                "target_index": target_index,
                "occurrence_id": occurrence_id,
                "declared_leave_one_out_positive_damage": declared_total,
                "compiled_leave_one_out_positive_damage": compiled_total,
                "compiled_event_count": event_count,
            }
        )
    raw_events.sort(key=lambda row: (row[0], row[1], row[2]))
    events = tuple(
        BackgroundDamageEventV1(
            schedule_index=index,
            time_ms=time_ms,
            target_index=target_index,
            event_id=f"resolved-loo:{target_index}:{bin_index}",
            damage=damage,
        )
        for index, (time_ms, target_index, bin_index, damage) in enumerate(raw_events)
    )
    return events, target_receipts


def _validate_base_request(base_request: Mapping[str, Any]) -> JSONMap:
    request = _strict_json_copy(_mapping(base_request, "base_request"), "base_request")
    _mapping(request.get("raid"), "base_request.raid")
    encounter = _mapping(request.get("encounter"), "base_request.encounter")
    targets = _array(encounter.get("targets"), "base_request.encounter.targets")
    if not targets:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "base request needs one target template"
        )
    _mapping(targets[0], "base request target template")
    sim_options = _mapping(request.get("simOptions"), "base_request.simOptions")
    if sim_options.get("iterations") != 1:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "base_request.simOptions.iterations must equal 1"
        )
    return request


def _compile_request(
    base_request: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    *,
    selected_armors: Sequence[float],
    point_health: Sequence[float],
    horizon_ms: int,
    target_level: int,
) -> JSONMap:
    request = _validate_base_request(base_request)
    encounter = dict(_mapping(request.get("encounter"), "base_request.encounter"))
    request["encounter"] = encounter
    template = _mapping(
        _array(encounter.get("targets"), "base request targets")[0],
        "base request target template",
    )
    request_targets: list[JSONMap] = []
    for index, target in enumerate(targets):
        compiled = deepcopy(dict(template))
        compiled.pop("id", None)
        compiled["name"] = _text(target.get("target_guid"), "target_guid")
        compiled["level"] = target_level
        compiled["mobType"] = "MobTypeUnknown"
        raw_stats = compiled.get("stats")
        if raw_stats is None:
            stats: list[int | float] = []
        elif isinstance(raw_stats, list):
            stats = list(raw_stats)
        else:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "base target stats must be a list"
            )
        while len(stats) <= HEALTH_STAT_INDEX:
            stats.append(0)
        stats[ARMOR_STAT_INDEX] = selected_armors[index]
        stats[HEALTH_STAT_INDEX] = point_health[index]
        compiled["stats"] = stats
        request_targets.append(compiled)
    encounter["duration"] = horizon_ms / 1000.0
    encounter["durationVariation"] = 0
    encounter["useHealth"] = True
    encounter["targets"] = request_targets
    return _strict_json_copy(request, "compiled request")


class _ResolvedDynamicV4ObservationProviderV1:
    """Strict raw-state cross-check used by the causal runtime target gate."""

    def __init__(self, config: DynamicTargetSemanticsConfigV4) -> None:
        self._config = config

    def __call__(
        self, state: Mapping[str, Any]
    ) -> Mapping[int, ObservedTargetStateV1]:
        if not isinstance(state, Mapping):
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "simulator state must be an object"
            )
        count = len(self._config.target_health)
        if _integer(state.get("total_target_count"), "state.total_target_count") != count:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "state total_target_count differs from the compiled registry"
            )
        team = _mapping(
            state.get("dynamic_team_background"), "state.dynamic_team_background"
        )
        semantics = _mapping(
            state.get("dynamic_target_semantics"), "state.dynamic_target_semantics"
        )
        for label, block in (("team", team), ("semantics", semantics)):
            if block.get("schema") != DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4:
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    f"{label} state schema is not dynamic-v4"
                )
            if block.get("config_digest") != self._config.content_sha256:
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    f"{label} state config digest differs from the compiled config"
                )
        team_generation = _integer(
            team.get("environment_generation"), "team environment_generation", positive=True
        )
        semantics_generation = _integer(
            semantics.get("environment_generation"),
            "semantics environment_generation",
            positive=True,
        )
        if team_generation != semantics_generation:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "dynamic-v4 state blocks have different environment generations"
            )
        team_targets = _array(team.get("targets"), "team lifecycle targets")
        semantics_targets = _array(
            semantics.get("targets"), "target semantics targets"
        )
        if len(team_targets) != count or len(semantics_targets) != count:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "dynamic-v4 state block target count differs from the registry"
            )

        observations: dict[int, ObservedTargetStateV1] = {}
        active_count = 0
        for index, checkpoint in enumerate(self._config.target_health):
            lifecycle = _mapping(team_targets[index], f"team target[{index}]")
            target = _mapping(semantics_targets[index], f"semantics target[{index}]")
            if (
                _integer(lifecycle.get("target_index"), "team target_index") != index
                or _integer(target.get("target_index"), "semantics target_index") != index
            ):
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    "dynamic-v4 target indexes are not contiguous and aligned"
                )
            initial = _number(lifecycle.get("initial_health"), "initial_health", positive=True)
            team_current = _number(lifecycle.get("current_health"), "team current_health")
            maximum = _number(target.get("maximum_health"), "maximum_health", positive=True)
            semantics_current = _number(
                target.get("current_health"), "semantics current_health"
            )
            team_dead = lifecycle.get("dead")
            semantics_dead = target.get("dead")
            attackable = target.get("attackable")
            if not all(
                isinstance(value, bool)
                for value in (team_dead, semantics_dead, attackable)
            ):
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    "dynamic-v4 dead/attackable fields must be booleans"
                )
            if (
                initial != checkpoint.current_health
                or maximum != checkpoint.maximum_health
                or team_current != semantics_current
                or team_current > initial
                or semantics_current > maximum
                or team_dead != semantics_dead
                or team_dead != (team_current <= 0)
                or (team_dead and attackable)
            ):
                raise UpperKaraResolvedDynamicV4AdapterV1Error(
                    "dynamic-v4 lifecycle and semantics target states disagree"
                )
            if attackable and not team_dead:
                active_count += 1
            observations[index] = ObservedTargetStateV1(
                visible=attackable,
                attackable=attackable,
                dead=team_dead,
            )
        if _integer(state.get("num_targets"), "state.num_targets") != active_count:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "state num_targets differs from live attackable target rows"
            )
        return observations


def compile_resolved_incantagos_dynamic_v4_case_v1(
    compact_model: Mapping[str, Any],
    actionability_contract: Mapping[str, Any],
    base_request: Mapping[str, Any],
    *,
    selected_armor_by_occurrence_id: Mapping[str, int | float],
    horizon_ms: int,
    target_level: int,
) -> CompiledResolvedIncantagosDynamicV4CaseV1:
    """Compile a full-offset-zero, fixed-LOO Incantagos development case."""

    compact = _mapping(compact_model, "compact_model")
    actionability = _mapping(actionability_contract, "actionability_contract")
    if compact.get("schema") != COMPACT_SCHEMA or compact.get("status") != COMPACT_STATUS:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "unexpected compact encounter model schema or status"
        )
    if (
        actionability.get("schema") != ACTIONABILITY_SCHEMA
        or actionability.get("status") != ACTIONABILITY_STATUS
    ):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "unexpected Incantagos actionability schema or status"
        )
    _comparison_false(compact, "compact_model")
    _comparison_false(actionability, "actionability_contract")
    if actionability.get("comparison_authorized") is not False:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "actionability contract must remain comparison-ineligible"
        )
    action_gate = _mapping(actionability.get("gate"), "actionability gate")
    if action_gate.get("development_target_actionability_authorized") is not True:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "Incantagos target actionability is not development-authorized"
        )
    horizon = _integer(horizon_ms, "horizon_ms", positive=True)
    level = _integer(target_level, "target_level", positive=True)
    if not isinstance(selected_armor_by_occurrence_id, Mapping):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "selected_armor_by_occurrence_id must be an object"
        )

    compact_source = _mapping(compact.get("source"), "compact source")
    action_source = _mapping(actionability.get("source"), "actionability source")
    for field in ("instance_id", "encounter_id", "pull_ref"):
        if compact_source.get(field) != action_source.get(field):
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                f"compact/actionability source mismatch at {field}"
            )

    compact_rows = [_mapping(row, "compact target") for row in _array(compact.get("targets"), "compact targets")]
    compact_by_id: dict[str, Mapping[str, Any]] = {}
    for row in compact_rows:
        occurrence_id = _text(row.get("occurrence_id"), "compact occurrence_id")
        if occurrence_id in compact_by_id:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "compact model repeats an occurrence_id"
            )
        compact_by_id[occurrence_id] = row

    full_ids = _unique_text_list(
        actionability.get("full_environment_occurrence_ids"),
        "full_environment_occurrence_ids",
    )
    direct_ids = _unique_text_list(
        actionability.get("direct_candidate_occurrence_ids"),
        "direct_candidate_occurrence_ids",
    )
    collateral_ids = _unique_text_list(
        actionability.get("collateral_candidate_occurrence_ids"),
        "collateral_candidate_occurrence_ids",
    )
    priority_ids = _unique_text_list(
        actionability.get("combat_priority_add_occurrence_ids"),
        "combat_priority_add_occurrence_ids",
    )
    collateral_only_ids = _unique_text_list(
        actionability.get("collateral_only_occurrence_ids", []),
        "collateral_only_occurrence_ids",
    )
    optional_actionable_ids = _unique_text_list(
        actionability.get("optional_actionable_occurrence_ids", []),
        "optional_actionable_occurrence_ids",
    )
    team_only_ids = _unique_text_list(
        actionability.get("team_only_occurrence_ids"),
        "team_only_occurrence_ids",
    )
    boss_id = _text(actionability.get("boss_occurrence_id"), "boss_occurrence_id")
    full_set = set(full_ids)
    native_set = set(collateral_ids)
    if set(compact_by_id) != full_set:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "compact target universe differs from actionability full environment"
        )
    if (
        boss_id not in direct_ids
        or boss_id not in native_set
        or not set(direct_ids) <= native_set
        or not set(priority_ids) <= set(direct_ids)
        or not set(optional_actionable_ids) <= set(direct_ids)
        or not set(collateral_only_ids) <= native_set
        or set(collateral_only_ids) & set(direct_ids)
        or set(priority_ids) & set(optional_actionable_ids)
        or native_set & set(team_only_ids)
        or native_set | set(team_only_ids) != full_set
    ):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "actionability direct/collateral/priority/team-only partitions are inconsistent"
        )
    if native_set - set(direct_ids) != set(collateral_only_ids):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "collateral-only list does not exactly explain nondirect native targets"
        )
    if set(direct_ids) != {boss_id, *priority_ids, *optional_actionable_ids}:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "direct target list is not boss plus priority and optional-actionable adds"
        )
    if (
        boss_id in priority_ids
        or boss_id in collateral_only_ids
        or boss_id in optional_actionable_ids
        or boss_id in team_only_ids
    ):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "boss occurrence cannot be an add or team-only target"
        )

    action_rows = [_mapping(row, "actionability target") for row in _array(actionability.get("targets"), "actionability targets")]
    action_by_id: dict[str, Mapping[str, Any]] = {}
    for row in action_rows:
        occurrence_id = _text(row.get("resolved_occurrence_id"), "resolved occurrence_id")
        if occurrence_id in action_by_id:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "actionability contract repeats an occurrence_id"
            )
        action_by_id[occurrence_id] = row
    if not full_set <= set(action_by_id):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "actionability rows omit a full-environment occurrence"
        )
    for occurrence_id in full_ids:
        compact_row = compact_by_id[occurrence_id]
        action_row = action_by_id[occurrence_id]
        if (
            compact_row.get("target_guid") != action_row.get("target_guid")
            or compact_row.get("identity_kind") != action_row.get("identity_resolution")
            or compact_row.get("creature_entry_id")
            != action_row.get("stable_template_creature_entry_id")
        ):
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                f"compact/actionability identity differs for {occurrence_id}"
            )
        if occurrence_id in team_only_ids:
            expected_actionability = TEAM_ONLY_SCHOOL_INCOMPATIBLE
        elif occurrence_id in collateral_only_ids:
            expected_actionability = COLLATERAL_ONLY_CANDIDATE
        else:
            expected_actionability = DIRECT_AND_COLLATERAL_CANDIDATE
        if action_row.get("candidate_actionability") != expected_actionability:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                f"actionability row/list mismatch for {occurrence_id}"
            )
    if action_by_id[boss_id].get("mechanic_classification") != BOSS:
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "boss occurrence is not classified as the boss"
        )

    native_ids = (boss_id,) + tuple(
        occurrence_id
        for occurrence_id in full_ids
        if occurrence_id != boss_id and occurrence_id in native_set
    )
    native_targets = tuple(compact_by_id[occurrence_id] for occurrence_id in native_ids)
    index_by_id = {occurrence_id: index for index, occurrence_id in enumerate(native_ids)}
    if set(selected_armor_by_occurrence_id) != set(native_ids):
        raise UpperKaraResolvedDynamicV4AdapterV1Error(
            "selected armor keys must exactly equal native occurrence IDs"
        )

    selected_armors: list[float] = []
    point_health: list[float] = []
    registry: list[ResolvedDynamicTargetIndexV1] = []
    for target_index, (occurrence_id, target) in enumerate(zip(native_ids, native_targets)):
        armor_model = _mapping(target.get("armor_model"), "target armor_model")
        if armor_model.get("kind") != "EXPLICIT_SENSITIVITY_GRID":
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "native target armor is not an explicit sensitivity grid"
            )
        grid = tuple(
            _number(value, "armor grid value")
            for value in _array(
                armor_model.get("base_armor_hypotheses"), "base armor hypotheses"
            )
        )
        selected = _number(
            selected_armor_by_occurrence_id[occurrence_id], "selected armor"
        )
        if selected not in grid:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                f"selected armor for {occurrence_id} is outside its explicit grid"
            )
        hp_model = _mapping(target.get("hp_model"), "target hp_model")
        if hp_model.get("kind") != "OBSERVED_KILL_DAMAGE_BALANCE_POINT_HYPOTHESIS":
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "native target health is not the supported point hypothesis"
            )
        healing = _number(
            hp_model.get("positive_healing_received"), "positive healing received"
        )
        if healing != 0:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "native eligible target healing is unsupported by fixed dynamic-v4 damage"
            )
        health = _number(hp_model.get("point_health"), "point health", positive=True)
        selected_armors.append(selected)
        point_health.append(health)
        registry.append(
            ResolvedDynamicTargetIndexV1(
                target_index=target_index,
                occurrence_id=occurrence_id,
                target_guid=_text(target.get("target_guid"), "target_guid"),
                identity_kind=_text(target.get("identity_kind"), "identity_kind"),
                creature_entry_id=_optional_positive_int(
                    target.get("creature_entry_id"), "creature_entry_id"
                ),
            )
        )

    team_only_sidecar: list[JSONMap] = []
    for occurrence_id in team_only_ids:
        target = compact_by_id[occurrence_id]
        team = _mapping(target.get("team_kill_clock_model"), "team-only kill clock")
        focal_damage = _number(
            team.get("focal_positive_damage_removed"),
            "team-only focal positive damage",
        )
        if focal_damage != 0:
            raise UpperKaraResolvedDynamicV4AdapterV1Error(
                "team-only target has focal damage and cannot be omitted safely"
            )
        team_only_sidecar.append(
            {
                "occurrence_id": occurrence_id,
                "target_guid": target.get("target_guid"),
                "identity_kind": target.get("identity_kind"),
                "creature_entry_id": target.get("creature_entry_id"),
                "native_target_index": None,
                "omission_reason": "TEAM_ONLY_SCHOOL_INCOMPATIBLE",
                "hp_model": deepcopy(dict(_mapping(target.get("hp_model"), "team-only hp model"))),
                "team_kill_clock_model": deepcopy(dict(team)),
                "attackability_model": deepcopy(
                    dict(_mapping(target.get("attackability_model"), "team-only attackability"))
                ),
                "schedule_reallocated_to_native_targets": False,
            }
        )

    attackability_events, windows = _compile_attackability_events(
        native_targets, horizon_ms=horizon
    )
    background_events, background_receipts = _compile_background_events(
        native_targets,
        activity_windows=windows,
        horizon_ms=horizon,
    )
    armor_events = tuple(
        DynamicEffectiveArmorEventV2(index, 0, index, armor)
        for index, armor in enumerate(selected_armors)
    )
    dynamic_config = DynamicTargetSemanticsConfigV4(
        target_health=tuple(
            DynamicTargetHealthV4(index, health, health)
            for index, health in enumerate(point_health)
        ),
        idle_advance_horizon_ms=horizon,
        background_damage_events=background_events,
        attackability_events=attackability_events,
        effective_armor_events=armor_events,
        retarget_mode=REQUIRED_RETARGET_MODE_V1,
    )
    request = _compile_request(
        base_request,
        native_targets,
        selected_armors=selected_armors,
        point_health=point_health,
        horizon_ms=horizon,
        target_level=level,
    )
    observation_provider = _ResolvedDynamicV4ObservationProviderV1(dynamic_config)
    priority_indexes = tuple(index_by_id[value] for value in priority_ids)
    collateral_only_indexes = tuple(index_by_id[value] for value in collateral_only_ids)
    optional_actionable_indexes = tuple(
        index_by_id[value] for value in optional_actionable_ids
    )
    # The revised gate distinguishes mandatory focus/blocking adds from targets
    # that are legal only as collateral (for example Incantagos whelps).
    target_gate = ReactiveBossAddsTargetGateV1(
        boss_target_index=index_by_id[boss_id],
        add_target_indexes=priority_indexes,
        collateral_only_target_indexes=collateral_only_indexes,
        optional_actionable_target_indexes=optional_actionable_indexes,
        boss_collateral_during_priority_adds=True,
        observation_provider=observation_provider,
    )

    receipt: JSONMap = {
        "schema": SCHEMA,
        "status": STATUS,
        "source": deepcopy(dict(compact_source)),
        "full_encounter_start_offset_ms": FULL_ENCOUNTER_START_OFFSET_MS,
        "horizon_ms": horizon,
        "target_level": level,
        "dynamic_config_digest": dynamic_config.content_sha256,
        "native_target_count": len(native_ids),
        "native_occurrence_ids": list(native_ids),
        "occurrence_index_registry": [row.to_dict() for row in registry],
        "boss_index": index_by_id[boss_id],
        "priority_add_indexes": list(priority_indexes),
        "collateral_only_indexes": list(collateral_only_indexes),
        "optional_actionable_indexes": list(optional_actionable_indexes),
        "team_only_occurrence_ids": list(team_only_ids),
        "team_only_schedule_reallocated": False,
        "selected_armor_by_occurrence_id": {
            occurrence_id: selected_armors[index]
            for index, occurrence_id in enumerate(native_ids)
        },
        "activity_windows_inclusive_ms": [
            {
                "target_index": index,
                "occurrence_id": native_ids[index],
                "windows": [list(window) for window in target_windows],
            }
            for index, target_windows in enumerate(windows)
        ],
        "fixed_leave_one_out_schedule": {
            "bin_total_timestamp_proxy": "LATEST_MILLISECOND_INTERSECTING_ACTIVITY_WINDOW",
            "final_bin_placement_clipped_to_horizon_without_clipping_damage": True,
            "target_receipts": background_receipts,
            "event_count": len(background_events),
            "responsive_to_candidate_policy": False,
        },
        "scientific_boundaries": {
            "development_only": True,
            "comparison_authorized": False,
            "maximum_and_current_health_equal_point_hypothesis": True,
            "full_encounter_checkpoint_only": True,
            "target_armor_observed": False,
            "attackability_is_descriptive_outcome_proxy": True,
            "policy_observes_only_current_attackability_and_death": True,
            "fixed_team_schedule_is_not_candidate_responsive": True,
            "team_only_school_incompatible_targets_omitted_from_native_arrays": True,
            "team_only_damage_not_reallocated": True,
        },
    }
    return CompiledResolvedIncantagosDynamicV4CaseV1(
        request=request,
        dynamic_config=dynamic_config,
        dynamic_load=ResolvedDynamicLoadV4(dynamic_config),
        occurrence_index_registry=tuple(registry),
        boss_index=index_by_id[boss_id],
        priority_add_indexes=priority_indexes,
        collateral_only_indexes=collateral_only_indexes,
        optional_actionable_indexes=optional_actionable_indexes,
        team_only_sidecar=tuple(team_only_sidecar),
        observation_provider=observation_provider,
        target_gate=target_gate,
        receipt=receipt,
    )


__all__ = (
    "CompiledResolvedIncantagosDynamicV4CaseV1",
    "FULL_ENCOUNTER_START_OFFSET_MS",
    "ResolvedDynamicTargetIndexV1",
    "ResolvedDynamicLoadV4",
    "SCHEMA",
    "STATUS",
    "UpperKaraResolvedDynamicV4AdapterV1Error",
    "compile_resolved_incantagos_dynamic_v4_case_v1",
)
