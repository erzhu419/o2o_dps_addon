"""Paired native evaluator for a frozen v8 Cat-relative residual sequence.

Both lanes use the same heterogeneous two-wave case and simulator seed.  The
exact-Cat and residual lanes nevertheless open independent native bridges,
causal observation projectors and Cat resolver sessions.  A lane is scoreable
only after every required target is dead; partial or invalid runs never yield
a damage delta.

The evaluator is development-only.  It does not select a policy or aggregate
across seeds.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    NativeDynamicV3ActionProgramReplayV1,
    ProgramDecisionV1,
    ProgramOriginV1,
)
from .development_two_wave_cat_residual_sequence_v1 import (
    DevelopmentTwoWaveCatResidualSequenceSessionV1,
    DevelopmentTwoWaveCatResidualSequenceV1,
    build_two_wave_cat_residual_sequence_runtime_v1,
)
from .development_wave_panel_v1 import DEFAULT_BINDING, WORKSPACE_ROOT
from .fury_paired_multiseed_runner_v4 import CAT_POLICY_ID
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_exact_baseline_panel_v1 import DEFAULT_EXACT_BRIDGE
from .upper_kara_heterogeneous_two_wave_remote_v7 import (
    build_upper_kara_heterogeneous_two_wave_burst_case_v7,
)
from .upper_kara_heterogeneous_two_wave_case_v1 import (
    build_heterogeneous_two_wave_observation_projector_v1,
)
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
    build_imported_incumbent_bindings_v1,
)
from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_cat_residual_paired_eval/v8"
COMPACT_TELEMETRY_SCHEMA = "upper_kara_v8_compact_telemetry/v1"

_DEATH_WISH = ActionRef(spell_id=12_328)
_BATTLE_SHOUT = ActionRef(spell_id=25_289)
_CLEAVE_QUEUE = ActionRef(spell_id=20_569, tag=1)
_CLEAVE_DAMAGE = ActionRef(spell_id=20_569)
_HEROIC_STRIKE_DAMAGE = ActionRef(spell_id=25_286)
_MAIN_HAND_WHITE = ActionRef(other_id=7, tag=1)
_OFF_HAND_WHITE = ActionRef(other_id=7, tag=2)
_TELEMETRY_DAMAGE_ACTIONS = (
    _CLEAVE_DAMAGE,
    _HEROIC_STRIKE_DAMAGE,
    _MAIN_HAND_WHITE,
    _OFF_HAND_WHITE,
)
_ACCEPTED_ACTION_RECEIPT_KINDS = frozenset(
    {"OPTIONAL_OFF_GCD_EXECUTED", "QUEUE_SET", "TERMINAL_GCD"}
)

# These are control-plane or suffix fields, not current policy observations.
# Exact key matching permits ordinary causal fields such as current visible
# target semantics and pull-relative time.
_FORBIDDEN_POLICY_KEYS = frozenset(
    {
        "arrival_ms",
        "dynamic_load",
        "environment_registry",
        "future_events",
        "future_schedule",
        "future_target_rows",
        "required_target_indices",
        "seed",
        "simulator_seed",
        "source_instance_id",
        "source_wave_ref",
        "target_introduction_registry_control_plane_only",
    }
)


class UpperKaraCatResidualPairedEvalV8Error(RuntimeError):
    """The paired evaluator could not establish its comparison contract."""


def _forbidden_paths(value: object, prefix: str = "state") -> tuple[str, ...]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            label = str(key)
            path = f"{prefix}.{label}"
            if label in _FORBIDDEN_POLICY_KEYS:
                result.append(path)
            result.extend(_forbidden_paths(child, path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            result.extend(_forbidden_paths(child, f"{prefix}[{index}]"))
    return tuple(result)


class _PolicyInputAuditResolverV8:
    """Reject suffix/control-plane fields before a policy resolver sees them."""

    def __init__(self, resolver: Callable[..., ProgramDecisionV1]) -> None:
        self._resolver = resolver
        self.decision_count = 0
        self.forbidden_paths_seen: list[str] = []

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        leaked = _forbidden_paths(observation.state)
        if leaked:
            self.forbidden_paths_seen.extend(leaked)
            raise UpperKaraCatResidualPairedEvalV8Error(
                "policy observation exposes forbidden control-plane fields: "
                + ", ".join(leaked)
            )
        self.decision_count += 1
        return self._resolver(observation, available)

    def record_last_executed_decision_v1(
        self,
        actual_decision: ProgramDecisionV1,
    ) -> None:
        callback = getattr(
            self._resolver, "record_last_executed_decision_v1", None
        )
        if callable(callback):
            callback(actual_decision)

    def reject_last_execution_v1(self, reason: str) -> None:
        callback = getattr(self._resolver, "reject_last_execution_v1", None)
        if callable(callback):
            callback(reason)


def _required_targets_dead(case: Any, state: Mapping[str, Any]) -> bool:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required = case.case_spec.get("required_target_indices")
    return bool(
        isinstance(targets, list)
        and isinstance(required, list)
        and required
        and all(
            type(index) is int
            and 0 <= index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("target_index") == index
            and targets[index].get("dead") is True
            for index in required
        )
    )


def _not_observed(reason: str, **evidence: Any) -> JSONMap:
    return {"status": "NOT_OBSERVED", "reason": reason, **evidence}


def _action_tuple(value: object) -> tuple[int, int, int, int] | None:
    if not isinstance(value, Mapping):
        return None
    fields = tuple(value.get(key, 0) for key in ("spell_id", "item_id", "other_id", "tag"))
    if any(isinstance(row, bool) or not isinstance(row, int) for row in fields):
        return None
    return fields  # type: ignore[return-value]


def _is_action(value: object, action: ActionRef) -> bool:
    return _action_tuple(value) == (
        action.spell_id,
        action.item_id,
        action.other_id,
        action.tag,
    )


def _accepted_action_times(
    outcome: ScheduleReplayOutcomeV1,
    action: ActionRef,
    *,
    receipt_kinds: frozenset[str] = _ACCEPTED_ACTION_RECEIPT_KINDS,
) -> list[int]:
    values: list[int] = []
    for row in outcome.receipts:
        if (
            row.get("kind") in receipt_kinds
            and _is_action(row.get("action"), action)
            and type(row.get("state_time_ms")) is int
        ):
            values.append(row["state_time_ms"])
    return sorted(values)


def _terminal_capture(outcome: ScheduleReplayOutcomeV1) -> JSONMap:
    rows = [
        row
        for row in outcome.receipts
        if row.get("kind") == "NATIVE_TERMINAL_TELEMETRY_V1"
    ]
    if not rows:
        return _not_observed("NATIVE_TERMINAL_TELEMETRY_RECEIPT_ABSENT")
    if len(rows) != 1:
        return _not_observed(
            "NATIVE_TERMINAL_TELEMETRY_RECEIPT_COUNT_INVALID",
            receipt_count=len(rows),
        )
    return deepcopy(dict(rows[0]))


def _target_death_times(state: Mapping[str, Any]) -> dict[int, int]:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    result: dict[int, int] = {}
    if not isinstance(targets, list):
        return result
    for row in targets:
        if not isinstance(row, Mapping):
            continue
        index = row.get("target_index")
        death = row.get("death_time_ms")
        if type(index) is int and type(death) is int and death >= 0:
            result[index] = death
    return result


def _target_attackable_start_times(case: Any) -> dict[int, int]:
    dynamic_load = getattr(case, "dynamic_load", None)
    config = getattr(dynamic_load, "config", None)
    events = getattr(config, "attackability_events", ())
    result: dict[int, int] = {}
    for event in events:
        index = getattr(event, "target_index", None)
        time_ms = getattr(event, "time_ms", None)
        if (
            getattr(event, "attackable", None) is True
            and type(index) is int
            and type(time_ms) is int
            and time_ms >= 0
        ):
            result[index] = min(result.get(index, time_ms), time_ms)
    return result


def _wave_timing(
    case: Any,
    state: Mapping[str, Any],
    waves: Sequence[Any],
) -> JSONMap:
    starts = _target_attackable_start_times(case)
    deaths = _target_death_times(state)
    rows: list[JSONMap] = []
    missing: list[str] = []
    for wave in waves:
        wave_id = getattr(wave, "wave_id", None)
        targets = tuple(getattr(wave, "target_indexes", ()))
        if not isinstance(wave_id, str) or not targets:
            missing.append("WAVE_REGISTRY_INVALID")
            continue
        missing_starts = [index for index in targets if index not in starts]
        missing_deaths = [index for index in targets if index not in deaths]
        if missing_starts or missing_deaths:
            reason_parts = []
            if missing_starts:
                reason_parts.append("ATTACKABLE_START_MISSING")
            if missing_deaths:
                reason_parts.append("TARGET_DEATH_TIME_MISSING")
            reason = "+".join(reason_parts)
            missing.append(f"{wave_id}:{reason}")
            rows.append(
                {
                    "wave_id": wave_id,
                    "target_indexes": list(targets),
                    **_not_observed(
                        reason,
                        missing_start_target_indexes=missing_starts,
                        missing_death_target_indexes=missing_deaths,
                    ),
                }
            )
            continue
        start_ms = max(starts[index] for index in targets)
        completion_ms = max(deaths[index] for index in targets)
        rows.append(
            {
                "status": "OBSERVED",
                "wave_id": wave_id,
                "target_indexes": list(targets),
                "attackable_start_time_ms": start_ms,
                "completion_time_ms": completion_ms,
                "elapsed_ms": completion_ms - start_ms,
                "start_evidence": (
                    "DYNAMIC_ATTACKABILITY_CONFIG_SIMULATOR_HYPOTHESIS"
                ),
                "completion_evidence": (
                    "DYNAMIC_TEAM_BACKGROUND_TARGET_DEATH_TIME"
                ),
            }
        )
    observed = [row for row in rows if row.get("status") == "OBSERVED"]
    if len(observed) == len(waves) and observed:
        route_start = min(row["attackable_start_time_ms"] for row in observed)
        route_end = max(row["completion_time_ms"] for row in observed)
        route: JSONMap = {
            "status": "OBSERVED",
            "start_time_ms": route_start,
            "completion_time_ms": route_end,
            "elapsed_ms": route_end - route_start,
        }
    else:
        route = _not_observed(
            "ONE_OR_MORE_WAVE_TIMINGS_UNOBSERVED",
            missing=missing,
        )
    return {
        "status": "OBSERVED" if not missing else "PARTIALLY_OBSERVED",
        "waves": rows,
        "full_route": route,
        "timing_contract": (
            "WAVE_START_IS_LATEST_TARGET_ATTACKABLE_TRUE_EVENT;"
            "WAVE_COMPLETION_IS_LATEST_TARGET_DEATH_TIME"
        ),
    }


def _union_intervals(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _intersection_duration(
    left: Sequence[tuple[int, int]],
    right: Sequence[tuple[int, int]],
) -> int:
    return sum(
        max(0, min(left_end, right_end) - max(left_start, right_start))
        for left_start, left_end in _union_intervals(left)
        for right_start, right_end in _union_intervals(right)
    )


def _assumption_derived_uptime(
    acceptance_times: Sequence[int],
    timing: Mapping[str, Any],
    *,
    duration_ms: int,
    assumption_id: str,
    source: str,
) -> JSONMap:
    waves = timing.get("waves")
    route = timing.get("full_route")
    if not isinstance(waves, list) or not isinstance(route, Mapping) or route.get("status") != "OBSERVED":
        return _not_observed("ROUTE_OR_WAVE_TIMING_UNAVAILABLE_FOR_UPTIME")
    wave_windows = [
        (row["attackable_start_time_ms"], row["completion_time_ms"])
        for row in waves
        if isinstance(row, Mapping) and row.get("status") == "OBSERVED"
    ]
    if len(wave_windows) != len(waves):
        return _not_observed("ONE_OR_MORE_WAVE_WINDOWS_UNAVAILABLE_FOR_UPTIME")
    aura_windows = [
        (time_ms, time_ms + duration_ms) for time_ms in acceptance_times
    ]
    route_window = [(route["start_time_ms"], route["completion_time_ms"])]
    return {
        "status": "ASSUMPTION_DERIVED",
        "duration_assumption_ms": duration_ms,
        "duration_assumption_id": assumption_id,
        "duration_assumption_source": source,
        "route_covered_uptime_ms": _intersection_duration(
            aura_windows, route_window
        ),
        "attackable_wave_useful_uptime_ms": _intersection_duration(
            aura_windows, wave_windows
        ),
    }


def _terminal_aura(state: Mapping[str, Any], action: ActionRef) -> JSONMap:
    auras = state.get("auras")
    if not isinstance(auras, list):
        return _not_observed("TERMINAL_AURA_SURFACE_UNAVAILABLE")
    for row in auras:
        if isinstance(row, Mapping) and _is_action(row.get("action"), action):
            remaining = row.get("remaining_ms")
            if type(remaining) is not int:
                return _not_observed("TERMINAL_AURA_REMAINING_TIME_INVALID")
            return {
                "status": "OBSERVED_ACTIVE" if remaining >= 0 else "OBSERVED_INACTIVE",
                "remaining_ms": remaining,
                "label": row.get("label"),
                "stacks": row.get("stacks"),
            }
    return {"status": "OBSERVED_INACTIVE", "remaining_ms": 0}


def _candidate_damage_surface(outcome: ScheduleReplayOutcomeV1) -> JSONMap:
    capture = _terminal_capture(outcome)
    if capture.get("status") == "NOT_OBSERVED":
        return capture
    surface = capture.get("candidate_damage_surface")
    if not isinstance(surface, Mapping):
        return _not_observed("CANDIDATE_DAMAGE_SURFACE_INVALID")
    return deepcopy(dict(surface))


def _group_damage_executions(rows: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    grouped: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        execution_id = row.get("execution_id")
        time_ms = row.get("time_ms")
        if type(execution_id) is not int or type(time_ms) is not int:
            continue
        grouped.setdefault((execution_id, time_ms), []).append(row)
    result: list[JSONMap] = []
    for (execution_id, time_ms), members in sorted(grouped.items(), key=lambda item: item[0][1]):
        outcomes = Counter(str(row.get("outcome")) for row in members)
        statuses = Counter(str(row.get("status")) for row in members)
        applied = sum(
            float(row.get("applied_damage", 0.0))
            for row in members
            if isinstance(row.get("applied_damage"), (int, float))
            and not isinstance(row.get("applied_damage"), bool)
        )
        attempt_ids = sorted(
            {
                str(row.get("attempt_id"))
                for row in members
                if isinstance(row.get("attempt_id"), str)
            }
        )
        result.append(
            {
                "execution_id": execution_id,
                "time_ms": time_ms,
                "target_receipt_count": len(members),
                "target_indexes": sorted(
                    {
                        row["target_index"]
                        for row in members
                        if type(row.get("target_index")) is int
                    }
                ),
                "outcome_counts": dict(sorted(outcomes.items())),
                "status_counts": dict(sorted(statuses.items())),
                "applied_damage": applied,
                "attempt_ids": attempt_ids,
            }
        )
    return result


def _candidate_rows_for(
    surface: Mapping[str, Any],
    *actions: ActionRef,
) -> list[Mapping[str, Any]] | None:
    if surface.get("status") != "OBSERVED":
        return None
    rows = surface.get("receipts")
    if not isinstance(rows, list):
        return None
    return [
        row
        for row in rows
        if isinstance(row, Mapping)
        and any(_is_action(row.get("action"), action) for action in actions)
    ]


def _terminal_resource(state: Mapping[str, Any]) -> JSONMap:
    power = state.get("power")
    if not isinstance(power, Mapping):
        return _not_observed("TERMINAL_POWER_SURFACE_UNAVAILABLE")
    current = power.get("current")
    maximum = power.get("maximum")
    if (
        isinstance(current, bool)
        or not isinstance(current, (int, float))
        or isinstance(maximum, bool)
        or not isinstance(maximum, (int, float))
    ):
        return _not_observed("TERMINAL_POWER_VALUES_INVALID")
    return {
        "status": "OBSERVED",
        "type": power.get("type"),
        "current": current,
        "maximum": maximum,
    }


def _terminal_cooldowns(outcome: ScheduleReplayOutcomeV1) -> JSONMap:
    capture = _terminal_capture(outcome)
    if capture.get("status") == "NOT_OBSERVED":
        return capture
    surface = capture.get("terminal_action_surface")
    if not isinstance(surface, Mapping) or surface.get("status") != "OBSERVED":
        return (
            deepcopy(dict(surface))
            if isinstance(surface, Mapping)
            else _not_observed("TERMINAL_ACTION_SURFACE_INVALID")
        )
    rows = surface.get("actions")
    if not isinstance(rows, list):
        return _not_observed("TERMINAL_ACTION_ROWS_INVALID")
    cooldowns = [
        {
            "action": deepcopy(dict(row["action"])),
            "label": row.get("label"),
            "ready_in_ms": row.get("ready_in_ms"),
            "cooldown_duration_ms": row.get("cooldown_duration_ms"),
        }
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("action"), Mapping)
        and type(row.get("cooldown_duration_ms")) is int
        and row["cooldown_duration_ms"] > 0
    ]
    return {"status": "OBSERVED", "actions": cooldowns}


def _execution_telemetry(
    outcome: ScheduleReplayOutcomeV1,
    fallback_reason_counts: Mapping[str, int] | None,
) -> JSONMap:
    kind_counts = Counter(
        str(row.get("kind"))
        for row in outcome.receipts
        if row.get("kind") != "NATIVE_TERMINAL_TELEMETRY_V1"
    )
    if fallback_reason_counts is None:
        fallback: JSONMap = {
            "status": "NOT_APPLICABLE",
            "reason": "EXACT_CAT_HAS_NO_RESIDUAL_FALLBACK_LAYER",
            "counts": {},
        }
    else:
        fallback = {
            "status": "OBSERVED",
            "counts": dict(sorted(fallback_reason_counts.items())),
        }
    return {
        "program_receipt_kind_counts": dict(sorted(kind_counts.items())),
        "accepted_action_receipt_count": sum(
            kind_counts.get(kind, 0) for kind in _ACCEPTED_ACTION_RECEIPT_KINDS
        ),
        "replay_failure": (
            {"status": "OBSERVED", "reason": outcome.invalid_reason}
            if outcome.invalid_reason is not None
            else {"status": "OBSERVED_NONE", "reason": None}
        ),
        "fallback_reasons": fallback,
    }


def _compact_telemetry(
    case: Any,
    outcome: ScheduleReplayOutcomeV1,
    waves: Sequence[Any],
    fallback_reason_counts: Mapping[str, int] | None,
) -> JSONMap:
    timing = _wave_timing(case, outcome.state, waves)
    death_wish_times = _accepted_action_times(outcome, _DEATH_WISH)
    battle_shout_times = _accepted_action_times(outcome, _BATTLE_SHOUT)
    cleave_accept_times = _accepted_action_times(
        outcome,
        _CLEAVE_QUEUE,
        receipt_kinds=frozenset({"QUEUE_SET"}),
    )
    cleave_queue_receipts = [
        row
        for row in outcome.receipts
        if row.get("kind") == "QUEUE_SET"
        and _is_action(row.get("action"), _CLEAVE_QUEUE)
    ]
    cleave_queue_events: list[JSONMap] = []
    confirmed_queued_count = 0
    queue_confirmation_missing = False
    for row in cleave_queue_receipts:
        queue_state = row.get("queue_state_after_acceptance")
        if not isinstance(queue_state, Mapping) or queue_state.get("status") == (
            "NOT_OBSERVED"
        ):
            queue_confirmation_missing = True
            confirmation: JSONMap = _not_observed(
                "SWING_QUEUE_STATE_UNAVAILABLE_AFTER_ACCEPTANCE"
            )
        else:
            confirmed = queue_state.get("status") != "NONE"
            confirmed_queued_count += int(confirmed)
            confirmation = {
                "status": "OBSERVED",
                "confirmed_queued": confirmed,
                "value": deepcopy(dict(queue_state)),
            }
        cleave_queue_events.append(
            {
                "accepted_time_ms": row.get("state_time_ms"),
                "attempt_id": row.get("attempt_id"),
                "queue_confirmation": confirmation,
            }
        )
    candidate = _candidate_damage_surface(outcome)
    cleave_rows = _candidate_rows_for(candidate, _CLEAVE_DAMAGE)
    mh_rows = _candidate_rows_for(
        candidate,
        _MAIN_HAND_WHITE,
        _CLEAVE_DAMAGE,
        _HEROIC_STRIKE_DAMAGE,
    )
    oh_rows = _candidate_rows_for(candidate, _OFF_HAND_WHITE)
    if cleave_rows is None:
        cleave_resolution = _not_observed(
            candidate.get("reason", "CANDIDATE_DAMAGE_RECEIPTS_UNAVAILABLE")
        )
    else:
        cleave_executions = _group_damage_executions(cleave_rows)
        outcome_counts = Counter(
            str(row.get("outcome")) for row in cleave_rows
        )
        cleave_resolution = {
            "status": "OBSERVED",
            "execution_count": len(cleave_executions),
            "target_receipt_count": len(cleave_rows),
            "outcome_counts": dict(sorted(outcome_counts.items())),
            "executions": cleave_executions,
        }
    queue_state = outcome.state.get("swing_queue")
    cleave = {
        "queue_acceptance": {
            "status": (
                "PARTIALLY_OBSERVED"
                if queue_confirmation_missing
                else "OBSERVED"
            ),
            "accepted_count": len(cleave_accept_times),
            "confirmed_queued_count": confirmed_queued_count,
            "accepted_times_ms": cleave_accept_times,
            "events": cleave_queue_events,
            "evidence": "QUEUE_SET_EXECUTION_RECEIPT",
        },
        "terminal_queue_state": (
            {"status": "OBSERVED", "value": deepcopy(dict(queue_state))}
            if isinstance(queue_state, Mapping)
            else _not_observed("TERMINAL_SWING_QUEUE_STATE_UNAVAILABLE")
        ),
        "resolution": cleave_resolution,
        "accepted_to_resolution_join": _not_observed(
            "QUEUE_ACCEPTANCE_AND_SWING_RESOLUTION_LACK_SHARED_ATTEMPT_ID"
        ),
    }

    def swing_surface(
        rows: list[Mapping[str, Any]] | None,
        hand: str,
    ) -> JSONMap:
        if rows is None:
            return _not_observed(
                candidate.get("reason", "CANDIDATE_DAMAGE_RECEIPTS_UNAVAILABLE")
            )
        events = _group_damage_executions(rows)
        for event in events:
            matching = [
                row
                for row in rows
                if row.get("execution_id") == event["execution_id"]
                and row.get("time_ms") == event["time_ms"]
            ]
            raw_action = matching[0].get("action") if matching else None
            if _is_action(raw_action, _MAIN_HAND_WHITE):
                event["swing_kind"] = "MAIN_HAND_WHITE"
            elif _is_action(raw_action, _OFF_HAND_WHITE):
                event["swing_kind"] = "OFF_HAND_WHITE"
            elif _is_action(raw_action, _CLEAVE_DAMAGE):
                event["swing_kind"] = "MAIN_HAND_CLEAVE_REPLACEMENT"
            elif _is_action(raw_action, _HEROIC_STRIKE_DAMAGE):
                event["swing_kind"] = "MAIN_HAND_HEROIC_STRIKE_REPLACEMENT"
            else:
                event["swing_kind"] = "UNKNOWN"
        return {
            "status": "OBSERVED",
            "hand": hand,
            "event_count": len(events),
            "event_times_ms": [row["time_ms"] for row in events],
            "events": events,
            "evidence": "DYNAMIC_CANDIDATE_DAMAGE_RECEIPTS",
        }

    result: JSONMap = {
        "schema": COMPACT_TELEMETRY_SCHEMA,
        "status": "OBSERVED_WITH_LABELED_ASSUMPTIONS",
        "timing": timing,
        "death_wish": {
            "acceptance": {
                "status": "OBSERVED",
                "accepted_count": len(death_wish_times),
                "accepted_times_ms": death_wish_times,
            },
            "useful_uptime": _assumption_derived_uptime(
                death_wish_times,
                timing,
                duration_ms=30_000,
                assumption_id="WOWSIMS_STATIC_DEATH_WISH_DURATION_30000MS",
                source=(
                    "wowsims-turtle/assets/database/db.json spell_id=12328"
                ),
            ),
            "terminal_aura": _terminal_aura(outcome.state, _DEATH_WISH),
        },
        "cleave": cleave,
        "battle_shout": {
            "acceptance": {
                "status": "OBSERVED",
                "accepted_count": len(battle_shout_times),
                "accepted_times_ms": battle_shout_times,
            },
            "uptime": _assumption_derived_uptime(
                battle_shout_times,
                timing,
                duration_ms=120_000,
                assumption_id="WOWSIMS_STATIC_BATTLE_SHOUT_DURATION_120000MS",
                source=(
                    "wowsims-turtle/assets/database/db.json spell_id=25289"
                ),
            ),
            "terminal_aura": _terminal_aura(outcome.state, _BATTLE_SHOUT),
        },
        "swing_timing": {
            "main_hand": swing_surface(mh_rows, "MAIN_HAND"),
            "off_hand": swing_surface(oh_rows, "OFF_HAND"),
        },
        "terminal": {
            "rage": _terminal_resource(outcome.state),
            "cooldowns": _terminal_cooldowns(outcome),
            "mh_swing_remaining_ms": outcome.state.get("mh_swing_remaining_ms"),
            "oh_swing_remaining_ms": outcome.state.get("oh_swing_remaining_ms"),
        },
        "execution": _execution_telemetry(
            outcome, fallback_reason_counts
        ),
        "assumptions": [
            {
                "id": "DYNAMIC_ATTACKABILITY_CONFIG_SIMULATOR_HYPOTHESIS",
                "scope": "WAVE_START_TIME_ONLY",
            },
            {
                "id": "WOWSIMS_STATIC_DEATH_WISH_DURATION_30000MS",
                "scope": "DEATH_WISH_UPTIME_ONLY",
            },
            {
                "id": "WOWSIMS_STATIC_BATTLE_SHOUT_DURATION_120000MS",
                "scope": "BATTLE_SHOUT_UPTIME_ONLY",
            },
        ],
    }

    def reasons(value: object) -> list[str]:
        found: list[str] = []
        if isinstance(value, Mapping):
            if value.get("status") == "NOT_OBSERVED" and isinstance(
                value.get("reason"), str
            ):
                found.append(value["reason"])
            for child in value.values():
                found.extend(reasons(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(reasons(child))
        return found

    gaps = sorted(set(reasons(result)))
    result["not_observed_reasons"] = gaps
    if gaps:
        result["status"] = "PARTIALLY_OBSERVED_WITH_LABELED_ASSUMPTIONS"
    return result


def _terminal(
    case: Any,
    outcome: ScheduleReplayOutcomeV1,
    waves: Sequence[Any],
    fallback_reason_counts: Mapping[str, int] | None,
) -> JSONMap:
    required_dead = _required_targets_dead(case, outcome.state)
    telemetry = _compact_telemetry(
        case,
        outcome,
        waves,
        fallback_reason_counts,
    )
    if outcome.status is ReplayStatusV1.INVALID:
        return {
            "status": "INVALID_REPLAY",
            "replay_status": outcome.status.value,
            "own_effective_damage": None,
            "elapsed_ms": None,
            "required_targets_dead": required_dead,
            "invalid_reason": outcome.invalid_reason,
            "compact_telemetry": telemetry,
        }
    if outcome.status is not ReplayStatusV1.COMPLETE or not required_dead:
        return {
            "status": "INCOMPLETE_REQUIRED_TARGETS",
            "replay_status": outcome.status.value,
            "own_effective_damage": None,
            "elapsed_ms": None,
            "required_targets_dead": required_dead,
            "invalid_reason": (
                outcome.invalid_reason
                or "native replay did not kill every required target"
            ),
            "compact_telemetry": telemetry,
        }
    try:
        damage = outcome.effective_damage
        elapsed_ms = outcome.elapsed_ms
    except (TypeError, ValueError) as error:
        return {
            "status": "INVALID_TERMINAL_METRICS",
            "replay_status": outcome.status.value,
            "own_effective_damage": None,
            "elapsed_ms": None,
            "required_targets_dead": True,
            "invalid_reason": f"{type(error).__name__}: {error}",
            "compact_telemetry": telemetry,
        }
    return {
        "status": "COMPLETED",
        "replay_status": outcome.status.value,
        "own_effective_damage": damage,
        "elapsed_ms": elapsed_ms,
        "required_targets_dead": True,
        "invalid_reason": None,
        "compact_telemetry": telemetry,
    }


def _exact_cat_program() -> CausalActionProgramV1:
    return CausalActionProgramV1(
        program_id="exact-cat::paired-residual-eval-v8",
        selector=ImportedReactiveSelectorV1(
            binding_id=CAT_POLICY_ID,
            source_policy_id=CAT_POLICY_ID,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.IMPORTED_REACTIVE_INCUMBENT,
        source_refs=(CAT_POLICY_ID, SCHEMA),
    )


def _step_audit(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    sessions: list[DevelopmentTwoWaveCatResidualSequenceSessionV1],
) -> JSONMap:
    if len(sessions) != 1:
        return {
            "session_open_count": len(sessions),
            "executed_step_keys": [],
            "steps": [
                {
                    "wave_id": step.wave_id,
                    "step_id": step.step_id,
                    "executed": False,
                }
                for step in policy.steps
            ],
            "runtime_event_count": 0,
            "runtime_events": [],
            "fallback_reason_counts": {},
        }
    session = sessions[0]
    executed = set(session.executed_step_keys)
    events = [deepcopy(dict(row)) for row in session.audit_events]
    fallback_counts = Counter(
        str(row.get("reason"))
        for row in events
        if row.get("kind") == "EXACT_CAT_FALLBACK"
    )
    return {
        "session_open_count": 1,
        "executed_step_keys": [list(row) for row in session.executed_step_keys],
        "steps": [
            {
                "wave_id": step.wave_id,
                "step_id": step.step_id,
                "executed": (step.wave_id, step.step_id) in executed,
            }
            for step in policy.steps
        ],
        "runtime_event_count": len(events),
        "runtime_events": events,
        "fallback_reason_counts": dict(sorted(fallback_counts.items())),
    }


def evaluate_upper_kara_cat_residual_sequence_paired_v8(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    *,
    seed: int,
    build_id: str,
    loadout_id: str,
    first_wave_arrival_ms: int = 0,
    pull_time_ms: int = 3_000,
    max_decisions: int = 10_000,
    bridge_path: str | Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: str | Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: str | Path = DEFAULT_BINDING,
    bridge_factory: Callable[[], Any] | None = None,
    selected_decision_transform: Callable[
        [ProgramDecisionV1, ProgramDecisionV1], ProgramDecisionV1
    ]
    | None = None,
    external_press_period_ms: int | None = None,
    external_press_phase_ms: int = 0,
) -> JSONMap:
    """Evaluate exact Cat and one frozen residual on a paired native case."""

    if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
        raise TypeError(
            "policy must be DevelopmentTwoWaveCatResidualSequenceV1"
        )
    if policy.exact_build_id != build_id:
        raise ValueError(
            "policy exact_build_id differs from the requested exact build"
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if isinstance(max_decisions, bool) or not isinstance(max_decisions, int):
        raise TypeError("max_decisions must be an integer")
    if max_decisions < 1:
        raise ValueError("max_decisions must be positive")

    case = build_upper_kara_heterogeneous_two_wave_burst_case_v7(
        seed,
        build_id=build_id,
        loadout_id=loadout_id,
        pull_time_ms=pull_time_ms,
        first_wave_arrival_ms=first_wave_arrival_ms,
    )
    if case.dynamic_load.seed != seed:
        raise UpperKaraCatResidualPairedEvalV8Error(
            "case builder returned a different simulator seed"
        )
    required = case.case_spec.get("required_target_indices")
    residual_targets = tuple(
        target for wave in policy.waves for target in wave.target_indexes
    )
    if not isinstance(required, list) or set(residual_targets) != set(required):
        raise ValueError(
            "residual wave targets differ from case required target indices"
        )

    source_bindings = build_imported_incumbent_bindings_v1(
        build_id,
        case,
        runtime_binding_path=runtime_binding_path,
    )
    cat_sources = [
        row for row in source_bindings if row.source_policy_id == CAT_POLICY_ID
    ]
    if len(cat_sources) != 1:
        raise UpperKaraCatResidualPairedEvalV8Error(
            "exactly one Cat source binding is required"
        )
    cat_source = cat_sources[0]

    baseline_cat_sessions: list[Callable[..., ProgramDecisionV1]] = []
    candidate_cat_sessions: list[Callable[..., ProgramDecisionV1]] = []
    baseline_input_audits: list[_PolicyInputAuditResolverV8] = []
    candidate_input_audits: list[_PolicyInputAuditResolverV8] = []
    residual_sessions: list[DevelopmentTwoWaveCatResidualSequenceSessionV1] = []

    def open_baseline_cat() -> _PolicyInputAuditResolverV8:
        raw = cat_source.open_session()
        baseline_cat_sessions.append(raw)
        audited = _PolicyInputAuditResolverV8(raw)
        baseline_input_audits.append(audited)
        return audited

    baseline_binding = ImportedReactiveProgramBindingV1(
        binding_id=cat_source.binding_id,
        source_policy_id=cat_source.source_policy_id,
        observation_contract_id=cat_source.observation_contract_id,
        resolver_factory=open_baseline_cat,
    )

    def open_candidate_cat() -> Callable[..., ProgramDecisionV1]:
        raw = cat_source.open_session()
        candidate_cat_sessions.append(raw)
        return raw

    candidate_program, raw_candidate_binding = (
        build_two_wave_cat_residual_sequence_runtime_v1(
            policy,
            cat_resolver_factory=open_candidate_cat,
            selected_decision_transform=selected_decision_transform,
        )
    )

    def open_candidate_residual() -> _PolicyInputAuditResolverV8:
        raw = raw_candidate_binding.open_session()
        if not isinstance(raw, DevelopmentTwoWaveCatResidualSequenceSessionV1):
            raise TypeError("residual runtime opened an unexpected session type")
        residual_sessions.append(raw)
        audited = _PolicyInputAuditResolverV8(raw)
        candidate_input_audits.append(audited)
        return audited

    candidate_binding = ImportedReactiveProgramBindingV1(
        binding_id=raw_candidate_binding.binding_id,
        source_policy_id=raw_candidate_binding.source_policy_id,
        observation_contract_id=raw_candidate_binding.observation_contract_id,
        resolver_factory=open_candidate_residual,
    )

    resolved_bridge_path = Path(bridge_path).expanduser().resolve()
    resolved_bridge_cwd = Path(bridge_cwd).expanduser().resolve()
    raw_open_bridge = bridge_factory or (
        lambda: SimulatorBridgePrecombatV1(
            resolved_bridge_path,
            cwd=resolved_bridge_cwd,
        )
    )
    opened_bridges: dict[str, list[Any]] = {
        "exact_cat": [],
        "residual": [],
    }

    def run_lane(
        lane_id: str,
        program: CausalActionProgramV1,
        binding: ImportedReactiveProgramBindingV1,
    ) -> ScheduleReplayOutcomeV1:
        def open_lane_bridge() -> Any:
            bridge = raw_open_bridge()
            opened_bridges[lane_id].append(bridge)
            return bridge

        # Projector state is prefix-dependent, so every lane gets a fresh one.
        replay = NativeDynamicV3ActionProgramReplayV1(
            open_lane_bridge,
            lambda requested_seed: {seed: case}[requested_seed],
            build_heterogeneous_two_wave_observation_projector_v1(case),
            imported_bindings=(binding,),
            terminal_telemetry_action_refs=_TELEMETRY_DAMAGE_ACTIONS,
            external_press_period_ms=external_press_period_ms,
            external_press_phase_ms=external_press_phase_ms,
        )
        return replay.replay(seed, program, max_decisions=max_decisions)

    # The lanes own independent bridge/projector/policy sessions.  Retain
    # named futures so completion order cannot swap exact-Cat and residual
    # outputs in the deterministic paired record.
    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="upper-kara-v8-paired-lane",
    ) as executor:
        exact_future = executor.submit(
            run_lane,
            "exact_cat",
            _exact_cat_program(),
            baseline_binding,
        )
        residual_future = executor.submit(
            run_lane,
            "residual",
            candidate_program,
            candidate_binding,
        )
        exact_outcome = exact_future.result()
        residual_outcome = residual_future.result()
    step_audit = _step_audit(policy, residual_sessions)
    fallback_counts = step_audit.get("fallback_reason_counts")
    exact_terminal = _terminal(case, exact_outcome, policy.waves, None)
    residual_terminal = _terminal(
        case,
        residual_outcome,
        policy.waves,
        fallback_counts if isinstance(fallback_counts, Mapping) else {},
    )

    fresh_bridges = (
        len(opened_bridges["exact_cat"]) == 1
        and len(opened_bridges["residual"]) == 1
        and opened_bridges["exact_cat"][0]
        is not opened_bridges["residual"][0]
    )
    fresh_cat_sessions = (
        len(baseline_cat_sessions) == 1
        and len(candidate_cat_sessions) == 1
        and baseline_cat_sessions[0] is not candidate_cat_sessions[0]
    )
    input_audits = (*baseline_input_audits, *candidate_input_audits)
    input_clean = bool(input_audits) and all(
        not row.forbidden_paths_seen for row in input_audits
    )
    both_complete = (
        exact_terminal["status"] == "COMPLETED"
        and residual_terminal["status"] == "COMPLETED"
    )
    comparison_valid = bool(
        both_complete and fresh_bridges and fresh_cat_sessions and input_clean
    )
    delta = (
        residual_terminal["own_effective_damage"]
        - exact_terminal["own_effective_damage"]
        if comparison_valid
        else None
    )

    return {
        "schema": SCHEMA,
        "status": (
            "COMPLETED_PAIRED_EVALUATION"
            if comparison_valid
            else "INVALID_OR_INCOMPLETE_PAIRED_EVALUATION"
        ),
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "seed": seed,
        "build_id": build_id,
        "loadout_id": loadout_id,
        "first_wave_arrival_ms": first_wave_arrival_ms,
        "policy_id": policy.policy_id,
        "same_case_and_seed": True,
        "fresh_native_bridge_instances_verified": fresh_bridges,
        "fresh_cat_sessions_verified": fresh_cat_sessions,
        "policy_input_contract": (
            "CURRENT_CAUSAL_LIVE_STATE_PROJECTION_V1_ONLY;"
            "NO_SEED_OR_FUTURE_ENVIRONMENT_REGISTRY"
        ),
        "policy_input_audit": {
            "clean": input_clean,
            "exact_cat_session_count": len(baseline_input_audits),
            "residual_session_count": len(candidate_input_audits),
            "exact_cat_decision_count": sum(
                row.decision_count for row in baseline_input_audits
            ),
            "residual_decision_count": sum(
                row.decision_count for row in candidate_input_audits
            ),
            "forbidden_paths_seen": sorted(
                {
                    path
                    for row in input_audits
                    for path in row.forbidden_paths_seen
                }
            ),
        },
        "exact_cat_terminal": exact_terminal,
        "residual_terminal": residual_terminal,
        "paired_residual_minus_cat_own_effective_damage": delta,
        "paired_comparison_valid": comparison_valid,
        "step_audit": step_audit,
    }


__all__ = (
    "COMPACT_TELEMETRY_SCHEMA",
    "SCHEMA",
    "UpperKaraCatResidualPairedEvalV8Error",
    "evaluate_upper_kara_cat_residual_sequence_paired_v8",
)
