"""Build a source-bound factual team kill-clock accounting control.

This is deliberately not a teammate response model.  It replays the observed
leave-one-player-out, focal-player, and explicitly unattributed damage events
at their observed times and magnitudes.  When an event's historical target has
already died in the accounting replay, the event is deterministically assigned
to the earliest-introduced currently alive target.  Scaling only the historical
focal lane demonstrates whether that dynamic assignment and the resulting kill
clock react to candidate damage.

The target capacity is a retrospective damage-through-death budget.  It is not
claimed to be initial or maximum health.  Only fully observed, zero-healing,
event-time-attributed targets whose complete union-window damage ledger closes
exactly to that budget are admitted.  The multiplier-one arm is an in-sample
factual bookkeeping diagnostic, not held-out validation.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics
import tempfile
from typing import Any, Mapping, Sequence

from . import chronicle_external_team_timeline_v2 as timeline_v2
from . import historical_fury_source_bound_environment_evidence_v1 as evidence_v1


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_source_bound_team_kill_clock_control/v1"
ROW_SCHEMA = "historical_fury_source_bound_team_kill_clock_control_row/v1"
KIND = "historical_fury_source_bound_team_kill_clock_control_manifest"
STATUS = "FACTUAL_DYNAMIC_RETARGET_CONTROL_COMPLETE_NOT_COUNTERFACTUAL_MODEL"
IMPLEMENTATION_REVISION = "v1.0_closed_union_ledger_dynamic_retarget"
CONTENT_ADDRESS_SCHEMA = (
    "historical_fury_source_bound_team_kill_clock_control_content/v1"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_MANIFEST = evidence_v1.DEFAULT_OUTPUT
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_fury_source_bound_team_kill_clock_control"
    / "v1"
)
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIRECTORY / "manifest.json"

ARM_SPECS = (
    ("focal_damage_x0", 0, 1, "0.0"),
    ("factual_focal_damage_x1", 1, 1, "1.0"),
    ("focal_damage_x1_25", 5, 4, "1.25"),
)
LANE_KEYS = (
    (
        "EXACT_PLAYER_LOO_TEAM",
        "leave_one_out_exact_player_damage_events",
        True,
    ),
    (
        "EXCLUDED_FOCAL_EXACT_PLAYER",
        "excluded_focal_exact_player_damage_events",
        False,
    ),
    (
        "OBSERVED_UNATTRIBUTED",
        "unattributed_voting_damage_evidence_not_runtime_schedule",
        True,
    ),
)
LANE_ORDER = {name: index for index, (name, _, _) in enumerate(LANE_KEYS)}


class HistoricalFurySourceBoundTeamKillClockControlV1Error(RuntimeError):
    """The evidence or deterministic accounting artifact is inconsistent."""


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"value is not strict JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    digest = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    core["content_address"] = {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "sha256": digest,
    }
    return core


def _verify_content_address(value: Mapping[str, Any], label: str) -> str:
    address = _mapping(value.get("content_address"), f"{label} content address")
    digest = address.get("sha256")
    if (
        address.get("schema") != CONTENT_ADDRESS_SCHEMA
        or address.get("algorithm") != "sha256"
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} content address is invalid"
        )
    expected = hashlib.sha256(
        _canonical_bytes({key: value for key, value in value.items() if key != "content_address"})
    ).hexdigest()
    if digest != expected:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} content address differs"
        )
    return digest


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} must be an array"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} must be an integer"
        )
    if minimum is not None and value < minimum:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} must be >= {minimum}"
        )
    return value


def _order_key(value: Any, label: str) -> tuple[int, int, int, int]:
    raw = _array(value, label)
    if len(raw) != 4:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} must have four EventMeta components"
        )
    return tuple(
        _integer(component, f"{label}[{index}]")
        for index, component in enumerate(raw)
    )  # type: ignore[return-value]


def _anchor_order(anchor: Mapping[str, Any], label: str) -> tuple[int, int, int, int]:
    stream_type = anchor.get("stream_type")
    if stream_type not in timeline_v2.STREAM_ORDER:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"{label} has unsupported stream type {stream_type}"
        )
    return (
        _integer(anchor.get("timestamp_ms"), f"{label}.timestamp_ms", minimum=0),
        _integer(anchor.get("event_index"), f"{label}.event_index"),
        timeline_v2.STREAM_ORDER[str(stream_type)],
        _integer(
            anchor.get("frame_message_index"),
            f"{label}.frame_message_index",
            minimum=0,
        ),
    )


def _window_bounds(
    row: Mapping[str, Any],
) -> tuple[
    tuple[int, int, int, int],
    tuple[int, int, int, int],
    int,
]:
    windows = _mapping(row.get("execution_window_contract"), "execution windows")
    support = _mapping(windows.get("bound_decision_support"), "decision support")
    diagnostic = _mapping(
        windows.get("base_request_diagnostic_slice"), "diagnostic slice"
    )
    start = _order_key(support.get("start_order_key"), "union start order")
    last = _order_key(support.get("end_order_key"), "decision support end order")
    diagnostic_end = _integer(
        diagnostic.get("end_timestamp_ms"), "diagnostic end", minimum=0
    )
    if start > last or diagnostic_end < start[0]:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "materialized union bounds are inconsistent"
        )
    return start, last, diagnostic_end


def _inside_union(
    order: tuple[int, int, int, int],
    *,
    start: tuple[int, int, int, int],
    last_decision: tuple[int, int, int, int],
    diagnostic_end_ms: int,
) -> bool:
    return order >= start and (order[0] <= diagnostic_end_ms or order <= last_decision)


def _source_events(row: Mapping[str, Any]) -> dict[str, list[JSONMap]]:
    trace = _mapping(row.get("team_trace"), "team trace")
    result: dict[str, list[JSONMap]] = {}
    for lane, source_key, is_team_lane in LANE_KEYS:
        output = []
        for event_index, raw in enumerate(_array(trace.get(source_key), source_key)):
            event = _mapping(raw, f"{source_key}[{event_index}]")
            membership = _mapping(
                event.get("window_membership"), "event window membership"
            )
            if membership.get("materialized_team_trace_union") is not True:
                raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
                    "source schedule contains an event outside the materialized union"
                )
            damage = _integer(event.get("damage"), "event damage", minimum=1)
            order = _order_key(event.get("order_key"), "event order key")
            output.append(
                {
                    "lane": lane,
                    "is_team_lane": is_team_lane,
                    "source_event_index": event_index,
                    "order_key": list(order),
                    "timestamp_ms": order[0],
                    "original_target_guid": str(event.get("target_guid")),
                    "damage": damage,
                }
            )
        result[lane] = output
    return result


def _target_reasons(
    target: Mapping[str, Any],
    *,
    start: tuple[int, int, int, int],
    last_decision: tuple[int, int, int, int],
    diagnostic_end_ms: int,
    target_events: Sequence[Mapping[str, Any]],
) -> tuple[list[str], JSONMap | None]:
    reasons: list[str] = []
    first = _mapping(target.get("first_observed_activity_anchor"), "first activity")
    last = _mapping(target.get("last_observed_activity_anchor"), "last activity")
    first_order = _anchor_order(first, "first activity")
    last_order = _anchor_order(last, "last activity")
    if first_order < start:
        reasons.append("INTRODUCED_BEFORE_MATERIALIZED_UNION_ORIGIN")
    if not _inside_union(
        first_order,
        start=start,
        last_decision=last_decision,
        diagnostic_end_ms=diagnostic_end_ms,
    ) or not _inside_union(
        last_order,
        start=start,
        last_decision=last_decision,
        diagnostic_end_ms=diagnostic_end_ms,
    ):
        reasons.append("TARGET_ACTIVITY_NOT_FULLY_INSIDE_MATERIALIZED_UNION")
    if last_order < first_order:
        reasons.append("TARGET_ACTIVITY_ORDER_INVALID")

    death = _mapping(target.get("death"), "target death")
    death_anchor = death.get("anchor")
    death_order: tuple[int, int, int, int] | None = None
    if death.get("observed") is not True or not isinstance(death_anchor, Mapping):
        reasons.append("TARGET_DEATH_NOT_OBSERVED")
    else:
        death_order = _anchor_order(death_anchor, "target death")
        if not _inside_union(
            death_order,
            start=start,
            last_decision=last_decision,
            diagnostic_end_ms=diagnostic_end_ms,
        ):
            reasons.append("TARGET_DEATH_OUTSIDE_MATERIALIZED_UNION")

    outcome = _mapping(target.get("historical_outcome"), "historical outcome")
    healing = outcome.get("observed_healing_received")
    if isinstance(healing, bool) or not isinstance(healing, int) or healing != 0:
        reasons.append("TARGET_HAS_HEALING_OR_UNKNOWN_HEALING")

    health = _mapping(target.get("health_evidence"), "health evidence")
    budget = health.get("retrospective_kill_budget_proxy")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        reasons.append("POSITIVE_RETROSPECTIVE_KILL_BUDGET_UNAVAILABLE")

    lifecycle = _mapping(
        target.get("target_lifecycle_evidence"), "target lifecycle evidence"
    )
    nonvoting = _mapping(
        lifecycle.get("event_time_nonvoting_positive_damage"),
        "event-time nonvoting damage",
    )
    if _integer(nonvoting.get("event_count"), "nonvoting event count", minimum=0) != 0:
        reasons.append("EVENT_TIME_TARGET_IDENTITY_INCOMPLETE")

    if any(
        not _inside_union(
            _order_key(event.get("order_key"), "target event order"),
            start=start,
            last_decision=last_decision,
            diagnostic_end_ms=diagnostic_end_ms,
        )
        for event in target_events
    ):
        reasons.append("TARGET_DAMAGE_EVENT_OUTSIDE_MATERIALIZED_UNION")
    if any(
        _order_key(event.get("order_key"), "target event order") < first_order
        or _order_key(event.get("order_key"), "target event order") > last_order
        for event in target_events
    ):
        reasons.append("TARGET_DAMAGE_EVENT_OUTSIDE_ACTIVITY_ENVELOPE")

    ledger_damage = sum(
        _integer(event.get("damage"), "target event damage", minimum=1)
        for event in target_events
    )
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        if ledger_damage != budget:
            reasons.append("COMPLETE_DAMAGE_LEDGER_DOES_NOT_CLOSE_TO_KILL_BUDGET")

    reasons = sorted(set(reasons))
    if reasons:
        return reasons, None
    assert death_order is not None and isinstance(budget, int)
    guid = str(target.get("target_guid"))
    if not guid:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "selected target has no GUID"
        )
    return [], {
        "target_guid": guid,
        "source_target_index": _integer(
            target.get("target_index"), "source target index", minimum=0
        ),
        "introduction_order_key": list(first_order),
        "introduction_offset_ms": first_order[0] - start[0],
        "observed_death_order_key": list(death_order),
        "observed_death_offset_ms": death_order[0] - start[0],
        "retrospective_kill_budget": budget,
        "complete_union_damage_ledger": ledger_damage,
        "exact_initial_or_max_health_claimed": False,
    }


def _scaled_damage(damage: int, numerator: int, denominator: int) -> int:
    # ARM_SPECS use non-negative ratios.  Round-half-up is explicit and stable.
    return (damage * numerator * 2 + denominator) // (2 * denominator)


def _lane_accounting_template() -> JSONMap:
    return {
        "scheduled_event_count": 0,
        "original_damage": 0,
        "scaled_scheduled_damage": 0,
        "applied_damage": 0,
        "overkill_damage": 0,
        "dropped_no_alive_target_damage": 0,
        "retargeted_event_count": 0,
        "retargeted_scaled_damage": 0,
    }


def _run_arm(
    *,
    target_registry: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    arm_id: str,
    numerator: int,
    denominator: int,
    decimal: str,
) -> JSONMap:
    states: dict[str, JSONMap] = {
        str(target["target_guid"]): {
            "remaining": _integer(
                target.get("retrospective_kill_budget"), "target budget", minimum=1
            ),
            "introduction_order": _order_key(
                target.get("introduction_order_key"), "target introduction"
            ),
            "predicted_death_order": None,
        }
        for target in target_registry
    }
    registry_by_guid = {str(target["target_guid"]): target for target in target_registry}
    lane_accounting = {lane: _lane_accounting_template() for lane in LANE_ORDER}
    team_assignments: Counter[tuple[str, str, str]] = Counter()
    team_assignment_damage: Counter[tuple[str, str, str]] = Counter()

    for event in sorted(
        events,
        key=lambda item: (
            tuple(item["order_key"]),
            LANE_ORDER[str(item["lane"])],
            int(item["source_event_index"]),
            str(item["original_target_guid"]),
        ),
    ):
        lane = str(event["lane"])
        accounting = lane_accounting[lane]
        accounting["scheduled_event_count"] += 1
        raw_damage = _integer(event.get("damage"), "event damage", minimum=1)
        accounting["original_damage"] += raw_damage
        if lane == "EXCLUDED_FOCAL_EXACT_PLAYER":
            scaled = _scaled_damage(raw_damage, numerator, denominator)
        else:
            scaled = raw_damage
        accounting["scaled_scheduled_damage"] += scaled
        order = _order_key(event.get("order_key"), "event order")
        original_guid = str(event["original_target_guid"])
        original = states[original_guid]
        applied_guid: str | None = None
        retargeted = False
        if original["predicted_death_order"] is None and original["introduction_order"] <= order:
            applied_guid = original_guid
        elif original["predicted_death_order"] is not None:
            candidates = [
                guid
                for guid, state in states.items()
                if state["predicted_death_order"] is None
                and state["introduction_order"] <= order
            ]
            if candidates:
                applied_guid = min(
                    candidates,
                    key=lambda guid: (
                        states[guid]["introduction_order"],
                        int(registry_by_guid[guid]["control_target_index"]),
                        guid,
                    ),
                )
                retargeted = True
        else:
            raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
                "an event precedes its admitted original target introduction"
            )

        if applied_guid is None:
            accounting["dropped_no_alive_target_damage"] += scaled
            continue
        if event.get("is_team_lane") is True:
            assignment = (lane, original_guid, applied_guid)
            team_assignments[assignment] += 1
            team_assignment_damage[assignment] += scaled
        if retargeted:
            accounting["retargeted_event_count"] += 1
            accounting["retargeted_scaled_damage"] += scaled
        if scaled <= 0:
            continue
        state = states[applied_guid]
        applied = min(int(state["remaining"]), scaled)
        state["remaining"] -= applied
        accounting["applied_damage"] += applied
        accounting["overkill_damage"] += scaled - applied
        if state["remaining"] == 0:
            state["predicted_death_order"] = order

    target_outcomes = []
    for target in target_registry:
        guid = str(target["target_guid"])
        state = states[guid]
        predicted = state["predicted_death_order"]
        observed_offset = _integer(
            target.get("observed_death_offset_ms"), "observed death offset", minimum=0
        )
        predicted_offset = (
            int(predicted[0])
            - int(_order_key(target_registry[0].get("union_origin_order_key"), "origin")[0])
            if predicted is not None
            else None
        )
        target_outcomes.append(
            {
                "control_target_index": target["control_target_index"],
                "target_guid": guid,
                "observed_death_offset_ms": observed_offset,
                "predicted_death_offset_ms": predicted_offset,
                "predicted_minus_observed_ms": (
                    predicted_offset - observed_offset
                    if predicted_offset is not None
                    else None
                ),
                "absolute_death_anchor_error_ms": (
                    abs(predicted_offset - observed_offset)
                    if predicted_offset is not None
                    else None
                ),
                "remaining_budget": state["remaining"],
            }
        )
    predicted_offsets = [
        outcome["predicted_death_offset_ms"]
        for outcome in target_outcomes
        if outcome["predicted_death_offset_ms"] is not None
    ]
    terminal = len(predicted_offsets) == len(target_registry)
    return {
        "arm_id": arm_id,
        "focal_damage_multiplier": {
            "numerator": numerator,
            "denominator": denominator,
            "decimal": decimal,
        },
        "focal_lane_is_observed_schedule_not_candidate_policy": True,
        "team_event_times_and_magnitudes_fixed": True,
        "team_target_assignment_is_dynamic": True,
        "retarget_selection_rule": (
            "EARLIEST_INTRODUCED_ALIVE_THEN_CONTROL_INDEX_THEN_GUID"
        ),
        "target_count": len(target_registry),
        "targets_killed_count": len(predicted_offsets),
        "terminal": terminal,
        "predicted_kill_clock_ms": max(predicted_offsets) if terminal else None,
        "target_outcomes": target_outcomes,
        "lane_accounting": [
            {"lane": lane, **lane_accounting[lane]} for lane in LANE_ORDER
        ],
        "team_dynamic_assignments": [
            {
                "lane": lane,
                "original_target_guid": original,
                "applied_target_guid": applied,
                "event_count": team_assignments[(lane, original, applied)],
                "scaled_damage": team_assignment_damage[(lane, original, applied)],
            }
            for lane, original, applied in sorted(team_assignments)
        ],
        "team_assignment_changed_relative_multiplier_1": None,
    }


def build_control_row(source_row: Mapping[str, Any]) -> JSONMap:
    """Build one deterministic accounting row from a validated evidence row."""

    start, last_decision, diagnostic_end = _window_bounds(source_row)
    by_lane = _source_events(source_row)
    events_by_target: dict[str, list[JSONMap]] = defaultdict(list)
    for events in by_lane.values():
        for event in events:
            events_by_target[str(event["original_target_guid"])].append(event)

    selected = []
    rejected: list[JSONMap] = []
    reason_counts: Counter[str] = Counter()
    for raw_target in _array(source_row.get("targets"), "source targets"):
        target = _mapping(raw_target, "source target")
        guid = str(target.get("target_guid"))
        reasons, projection = _target_reasons(
            target,
            start=start,
            last_decision=last_decision,
            diagnostic_end_ms=diagnostic_end,
            target_events=events_by_target.get(guid, []),
        )
        if reasons:
            reason_counts.update(reasons)
            rejected.append({"target_guid": guid, "reason_codes": reasons})
        else:
            assert projection is not None
            selected.append(projection)

    selected.sort(
        key=lambda target: (
            tuple(target["introduction_order_key"]),
            int(target["source_target_index"]),
            str(target["target_guid"]),
        )
    )
    for index, target in enumerate(selected):
        target["control_target_index"] = index
        target["union_origin_order_key"] = list(start)
    selected_guids = {str(target["target_guid"]) for target in selected}
    selected_events = [
        event
        for lane in LANE_ORDER
        for event in by_lane[lane]
        if str(event["original_target_guid"]) in selected_guids
    ]
    source_identity = deepcopy(
        dict(_mapping(source_row.get("source_identity"), "source identity"))
    )
    base = {
        "schema": ROW_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": (
            "FACTUAL_CONTROL_COMPUTED_NOT_COUNTERFACTUAL_MODEL"
            if selected
            else "BLOCKED_NO_CLOSED_FULLY_OBSERVED_TARGETS"
        ),
        "segment_ref": source_row.get("segment_ref"),
        "source_evidence_row_sha256": _mapping(
            source_row.get("content_address"), "source row content address"
        ).get("sha256"),
        "source_identity": source_identity,
        "control_contract": {
            "execution_window": "MATERIALIZED_TEAM_TRACE_UNION",
            "target_capacity": (
                "RETROSPECTIVE_DAMAGE_THROUGH_FIRST_DEATH_BUDGET_NOT_HP"
            ),
            "target_introduction": "FIRST_OBSERVED_ACTIVITY_ANCHOR",
            "historical_death_is_model_input": False,
            "historical_death_is_calibration_anchor": True,
            "event_times_fixed": True,
            "team_event_magnitudes_fixed": True,
            "team_event_targets_dynamic_after_original_target_death": True,
            "learned_counterfactual_team_response_model": False,
            "candidate_policy_evaluated": False,
            "held_out_validation": False,
        },
        "target_selection": {
            "source_target_count": len(
                _array(source_row.get("targets"), "source targets")
            ),
            "selected_target_count": len(selected),
            "rejected_target_count": len(rejected),
            "required_conditions": [
                "INTRODUCED_AT_OR_AFTER_MATERIALIZED_UNION_ORIGIN",
                "ALL_OBSERVED_ACTIVITY_INSIDE_MATERIALIZED_UNION",
                "OBSERVED_DEAD_INSIDE_MATERIALIZED_UNION",
                "ZERO_OBSERVED_HEALING",
                "POSITIVE_RETROSPECTIVE_KILL_BUDGET",
                "NO_EVENT_TIME_NONVOTING_POSITIVE_DAMAGE",
                "COMPLETE_THREE_LANE_DAMAGE_LEDGER_EQUALS_KILL_BUDGET",
            ],
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
            "rejected_targets": sorted(rejected, key=lambda item: item["target_guid"]),
        },
        "target_registry": selected,
        "source_schedule_accounting": {
            "selected_event_count": len(selected_events),
            "selected_damage": sum(int(event["damage"]) for event in selected_events),
            "exact_player_loo_team_event_count": sum(
                event["lane"] == "EXACT_PLAYER_LOO_TEAM" for event in selected_events
            ),
            "exact_player_loo_team_damage": sum(
                int(event["damage"])
                for event in selected_events
                if event["lane"] == "EXACT_PLAYER_LOO_TEAM"
            ),
            "excluded_focal_event_count": sum(
                event["lane"] == "EXCLUDED_FOCAL_EXACT_PLAYER"
                for event in selected_events
            ),
            "excluded_focal_damage": sum(
                int(event["damage"])
                for event in selected_events
                if event["lane"] == "EXCLUDED_FOCAL_EXACT_PLAYER"
            ),
            "observed_unattributed_event_count": sum(
                event["lane"] == "OBSERVED_UNATTRIBUTED"
                for event in selected_events
            ),
            "observed_unattributed_damage": sum(
                int(event["damage"])
                for event in selected_events
                if event["lane"] == "OBSERVED_UNATTRIBUTED"
            ),
            "unattributed_lane_is_observed_not_exact_player_attributed": True,
        },
        "arms": [],
        "factual_multiplier_one_calibration": None,
        "candidate_sensitivity": None,
        "blockers": (
            []
            if selected
            else [
                {
                    "code": "NO_CLOSED_FULLY_OBSERVED_TARGETS",
                    "detail": (
                        "no target in this request supports the closed factual "
                        "accounting control"
                    ),
                }
            ]
        ),
    }
    if not selected:
        return _content_addressed(base)

    arms = [
        _run_arm(
            target_registry=selected,
            events=selected_events,
            arm_id=arm_id,
            numerator=numerator,
            denominator=denominator,
            decimal=decimal,
        )
        for arm_id, numerator, denominator, decimal in ARM_SPECS
    ]
    factual = next(arm for arm in arms if arm["arm_id"] == "factual_focal_damage_x1")
    factual_assignments = factual["team_dynamic_assignments"]
    for arm in arms:
        arm["team_assignment_changed_relative_multiplier_1"] = (
            arm["team_dynamic_assignments"] != factual_assignments
        )
    target_errors = [
        int(outcome["absolute_death_anchor_error_ms"])
        for outcome in factual["target_outcomes"]
        if outcome["absolute_death_anchor_error_ms"] is not None
    ]
    actual_clock = max(int(target["observed_death_offset_ms"]) for target in selected)
    predicted_clock = factual["predicted_kill_clock_ms"]
    zero = next(arm for arm in arms if arm["arm_id"] == "focal_damage_x0")
    stronger = next(
        arm for arm in arms if arm["arm_id"] == "focal_damage_x1_25"
    )
    base["arms"] = arms
    base["factual_multiplier_one_calibration"] = {
        "role": "IN_SAMPLE_FACTUAL_ACCOUNTING_DIAGNOSTIC_NOT_VALIDATION",
        "same_source_wave_used_for_capacity_schedule_and_anchor": True,
        "target_count": len(selected),
        "predicted_death_count": len(target_errors),
        "all_selected_targets_predicted_dead": factual["terminal"],
        "mean_absolute_death_anchor_error_ms": (
            round(sum(target_errors) / len(target_errors), 6) if target_errors else None
        ),
        "median_absolute_death_anchor_error_ms": (
            statistics.median(target_errors) if target_errors else None
        ),
        "maximum_absolute_death_anchor_error_ms": max(target_errors, default=None),
        "actual_selected_target_kill_clock_ms": actual_clock,
        "predicted_selected_target_kill_clock_ms": predicted_clock,
        "absolute_kill_clock_error_ms": (
            abs(int(predicted_clock) - actual_clock)
            if predicted_clock is not None
            else None
        ),
        "calibration_pass_claimed": False,
    }
    state_or_clock = [
        (bool(arm["terminal"]), arm["predicted_kill_clock_ms"]) for arm in arms
    ]
    base["candidate_sensitivity"] = {
        "arm_state_or_kill_clock_values": [
            {
                "arm_id": arm["arm_id"],
                "terminal": arm["terminal"],
                "predicted_kill_clock_ms": arm["predicted_kill_clock_ms"],
            }
            for arm in arms
        ],
        "terminal_state_or_kill_clock_changes_with_focal_multiplier": (
            len(set(state_or_clock)) > 1
        ),
        "zero_focal_vs_factual_state_or_kill_clock_changed": (
            zero["terminal"],
            zero["predicted_kill_clock_ms"],
        )
        != (factual["terminal"], factual["predicted_kill_clock_ms"]),
        "stronger_focal_vs_factual_state_or_kill_clock_changed": (
            stronger["terminal"],
            stronger["predicted_kill_clock_ms"],
        )
        != (factual["terminal"], factual["predicted_kill_clock_ms"]),
        "team_dynamic_assignment_changes_with_focal_multiplier": any(
            arm["team_assignment_changed_relative_multiplier_1"] for arm in arms
        ),
        "interpretation": (
            "MECHANISM_SENSITIVITY_ONLY_NOT_A_CANDIDATE_POLICY_COMPARISON"
        ),
    }
    return _content_addressed(base)


def _summary(rows: Sequence[Mapping[str, Any]]) -> JSONMap:
    computed = [row for row in rows if row.get("arms")]
    factual_rows = [
        _mapping(row.get("factual_multiplier_one_calibration"), "calibration")
        for row in computed
    ]
    target_errors = [
        int(outcome["absolute_death_anchor_error_ms"])
        for row in computed
        for arm in _array(row.get("arms"), "arms")
        if _mapping(arm, "arm").get("arm_id") == "factual_focal_damage_x1"
        for outcome in _array(_mapping(arm, "arm").get("target_outcomes"), "outcomes")
        if _mapping(outcome, "outcome").get("absolute_death_anchor_error_ms")
        is not None
    ]
    kill_clock_errors = [
        int(calibration["absolute_kill_clock_error_ms"])
        for calibration in factual_rows
        if calibration.get("absolute_kill_clock_error_ms") is not None
    ]
    arm_aggregate = []
    for arm_id, _, _, _ in ARM_SPECS:
        arms = [
            _mapping(arm, "arm")
            for row in computed
            for arm in _array(row.get("arms"), "arms")
            if _mapping(arm, "arm").get("arm_id") == arm_id
        ]
        terminal_clocks = [
            int(arm["predicted_kill_clock_ms"])
            for arm in arms
            if arm.get("predicted_kill_clock_ms") is not None
        ]
        team_lanes = [
            _mapping(lane, "lane")
            for arm in arms
            for lane in _array(arm.get("lane_accounting"), "lane accounting")
            if _mapping(lane, "lane").get("lane")
            in {"EXACT_PLAYER_LOO_TEAM", "OBSERVED_UNATTRIBUTED"}
        ]
        arm_aggregate.append(
            {
                "arm_id": arm_id,
                "request_count": len(arms),
                "terminal_request_count": sum(arm.get("terminal") is True for arm in arms),
                "targets_killed_count": sum(
                    int(arm.get("targets_killed_count", 0)) for arm in arms
                ),
                "target_count": sum(int(arm.get("target_count", 0)) for arm in arms),
                "minimum_terminal_kill_clock_ms": min(terminal_clocks, default=None),
                "median_terminal_kill_clock_ms": (
                    statistics.median(terminal_clocks) if terminal_clocks else None
                ),
                "maximum_terminal_kill_clock_ms": max(terminal_clocks, default=None),
                "team_retargeted_event_count": sum(
                    int(lane.get("retargeted_event_count", 0)) for lane in team_lanes
                ),
                "team_retargeted_scaled_damage": sum(
                    int(lane.get("retargeted_scaled_damage", 0)) for lane in team_lanes
                ),
            }
        )
    schedule_rows = [
        _mapping(row.get("source_schedule_accounting"), "schedule") for row in rows
    ]
    selected_loo_damage = sum(
        int(schedule.get("exact_player_loo_team_damage", 0))
        for schedule in schedule_rows
    )
    selected_focal_damage = sum(
        int(schedule.get("excluded_focal_damage", 0)) for schedule in schedule_rows
    )
    selected_unattributed_damage = sum(
        int(schedule.get("observed_unattributed_damage", 0))
        for schedule in schedule_rows
    )
    return {
        "request_count": len(rows),
        "computed_request_count": len(computed),
        "blocked_request_count": len(rows) - len(computed),
        "source_target_count": sum(
            _mapping(row.get("target_selection"), "selection").get(
                "source_target_count"
            )
            for row in rows
        ),
        "selected_target_count": sum(
            _mapping(row.get("target_selection"), "selection").get(
                "selected_target_count"
            )
            for row in rows
        ),
        "rejected_target_count": sum(
            _mapping(row.get("target_selection"), "selection").get(
                "rejected_target_count"
            )
            for row in rows
        ),
        "computed_arm_count": sum(len(_array(row.get("arms"), "arms")) for row in rows),
        "factual_multiplier_one_terminal_request_count": sum(
            calibration.get("all_selected_targets_predicted_dead") is True
            for calibration in factual_rows
        ),
        "factual_multiplier_one_predicted_death_count": len(target_errors),
        "factual_multiplier_one_mean_absolute_death_anchor_error_ms": (
            round(sum(target_errors) / len(target_errors), 6) if target_errors else None
        ),
        "factual_multiplier_one_median_absolute_death_anchor_error_ms": (
            statistics.median(target_errors) if target_errors else None
        ),
        "factual_multiplier_one_maximum_absolute_death_anchor_error_ms": (
            max(target_errors, default=None)
        ),
        "factual_multiplier_one_kill_clock_error_request_count": len(kill_clock_errors),
        "factual_multiplier_one_mean_absolute_kill_clock_error_ms": (
            round(sum(kill_clock_errors) / len(kill_clock_errors), 6)
            if kill_clock_errors
            else None
        ),
        "candidate_sensitive_request_count": sum(
            _mapping(row.get("candidate_sensitivity"), "sensitivity").get(
                "terminal_state_or_kill_clock_changes_with_focal_multiplier"
            )
            is True
            for row in computed
        ),
        "zero_focal_vs_factual_state_or_kill_clock_changed_request_count": sum(
            _mapping(row.get("candidate_sensitivity"), "sensitivity").get(
                "zero_focal_vs_factual_state_or_kill_clock_changed"
            )
            is True
            for row in computed
        ),
        "stronger_focal_vs_factual_state_or_kill_clock_changed_request_count": sum(
            _mapping(row.get("candidate_sensitivity"), "sensitivity").get(
                "stronger_focal_vs_factual_state_or_kill_clock_changed"
            )
            is True
            for row in computed
        ),
        "team_assignment_sensitive_request_count": sum(
            _mapping(row.get("candidate_sensitivity"), "sensitivity").get(
                "team_dynamic_assignment_changes_with_focal_multiplier"
            )
            is True
            for row in computed
        ),
        "selected_exact_player_loo_team_event_count": sum(
            int(schedule.get("exact_player_loo_team_event_count", 0))
            for schedule in schedule_rows
        ),
        "selected_exact_player_loo_team_damage": selected_loo_damage,
        "selected_excluded_focal_event_count": sum(
            int(schedule.get("excluded_focal_event_count", 0))
            for schedule in schedule_rows
        ),
        "selected_excluded_focal_damage": selected_focal_damage,
        "selected_observed_unattributed_event_count": sum(
            int(schedule.get("observed_unattributed_event_count", 0))
            for schedule in schedule_rows
        ),
        "selected_observed_unattributed_damage": selected_unattributed_damage,
        "selected_total_damage": (
            selected_loo_damage + selected_focal_damage + selected_unattributed_damage
        ),
        "arm_aggregate": arm_aggregate,
    }


def validate_control_row(value: Mapping[str, Any]) -> JSONMap:
    row = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        row.get("schema") != ROW_SCHEMA
        or row.get("implementation_revision") != IMPLEMENTATION_REVISION
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control row identity differs"
        )
    _verify_content_address(row, "control row")
    contract = _mapping(row.get("control_contract"), "control contract")
    if (
        contract.get("learned_counterfactual_team_response_model") is not False
        or contract.get("candidate_policy_evaluated") is not False
        or contract.get("held_out_validation") is not False
        or contract.get("historical_death_is_model_input") is not False
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control row exceeds its factual diagnostic boundary"
        )
    arms = _array(row.get("arms"), "control arms")
    if arms and [arm.get("arm_id") for arm in arms] != [spec[0] for spec in ARM_SPECS]:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control arms differ from the frozen sensitivity family"
        )
    if not arms and row.get("status") != "BLOCKED_NO_CLOSED_FULLY_OBSERVED_TARGETS":
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "empty control row is not fail-closed"
        )
    return row


def build_historical_fury_source_bound_team_kill_clock_control_v1(
    *, evidence_manifest_path: str | Path = DEFAULT_EVIDENCE_MANIFEST
) -> tuple[list[JSONMap], JSONMap]:
    evidence_manifest, evidence_rows = (
        evidence_v1.load_historical_fury_source_bound_environment_evidence_v1(
            evidence_manifest_path
        )
    )
    rows = [validate_control_row(build_control_row(row)) for row in evidence_rows]
    descriptor = _mapping(
        evidence_manifest.get("evidence_partition"), "source evidence partition"
    )
    metadata = {
        "source_evidence_manifest_sha256": _mapping(
            evidence_manifest.get("content_address"), "source manifest content address"
        ).get("sha256"),
        "source_evidence_partition_logical_sha256": descriptor.get(
            "logical_content_sha256"
        ),
        "source_evidence_request_count": _mapping(
            evidence_manifest.get("summary"), "source summary"
        ).get("request_count"),
    }
    return rows, metadata


def _manifest_for_rows(
    *,
    rows: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    partition: Mapping[str, Any],
) -> JSONMap:
    return _content_addressed(
        {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": KIND,
            "status": STATUS,
            "execution_status": "DETERMINISTIC_FACTUAL_ACCOUNTING_COMPLETE",
            "scientific_runs_started": False,
            "simulator_run_count": 0,
            "hpc_job_count": 0,
            "network_request_count": 0,
            "comparison_authorized": False,
            "training_authorized": False,
            "deployment_authorized": False,
            "superiority_claim_authorized": False,
            "calibration_pass_claimed": False,
            "learned_counterfactual_team_response_model": False,
            "candidate_policy_evaluated": False,
            "held_out_validation": False,
            "source_binding": deepcopy(dict(metadata)),
            "control_partition": deepcopy(dict(partition)),
            "summary": _summary(rows),
            "interpretation": {
                "factual_multiplier_one": (
                    "IN_SAMPLE_ACCOUNTING_DIAGNOSTIC_NOT_HELD_OUT_VALIDATION"
                ),
                "sensitivity_arms": (
                    "FIXED_HISTORICAL_FOCAL_SCHEDULE_SCALING_NOT_POLICY_EVALUATION"
                ),
                "dynamic_retarget": (
                    "TARGET_ASSIGNMENT_RESPONSE_ONLY_TIMING_AND_MAGNITUDES_REMAIN_FIXED"
                ),
            },
        }
    )


def validate_historical_fury_source_bound_team_kill_clock_control_v1(
    value: Mapping[str, Any]
) -> JSONMap:
    manifest = json.loads(_canonical_bytes(value).decode("utf-8"))
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
        or manifest.get("kind") != KIND
        or manifest.get("status") != STATUS
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control manifest identity differs"
        )
    _verify_content_address(manifest, "control manifest")
    for key in (
        "comparison_authorized",
        "training_authorized",
        "deployment_authorized",
        "superiority_claim_authorized",
        "calibration_pass_claimed",
        "learned_counterfactual_team_response_model",
        "candidate_policy_evaluated",
        "held_out_validation",
    ):
        if manifest.get(key) is not False:
            raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
                f"control manifest incorrectly sets {key}"
            )
    if (
        manifest.get("execution_status")
        != "DETERMINISTIC_FACTUAL_ACCOUNTING_COMPLETE"
        or manifest.get("scientific_runs_started") is not False
        or manifest.get("simulator_run_count") != 0
        or manifest.get("hpc_job_count") != 0
        or manifest.get("network_request_count") != 0
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control manifest run accounting differs"
        )
    return manifest


def _gzip_bytes(payload: bytes) -> bytes:
    import io

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as handle:
        handle.write(payload)
    return output.getvalue()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def publish_historical_fury_source_bound_team_kill_clock_control_v1(
    *,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    evidence_manifest_path: str | Path = DEFAULT_EVIDENCE_MANIFEST,
) -> tuple[Path, Path, Path, JSONMap]:
    rows, metadata = build_historical_fury_source_bound_team_kill_clock_control_v1(
        evidence_manifest_path=evidence_manifest_path
    )
    logical = b"".join(_canonical_bytes(row, newline=True) for row in rows)
    compressed = _gzip_bytes(logical)
    logical_sha = hashlib.sha256(logical).hexdigest()
    destination = Path(output_directory).expanduser().resolve()
    partition_name = f"team_kill_clock_control.{logical_sha}.jsonl.gz"
    partition_path = destination / partition_name
    _atomic_write(partition_path, compressed)
    partition = {
        "path": partition_name,
        "record_schema": ROW_SCHEMA,
        "record_count": len(rows),
        "logical_size_bytes": len(logical),
        "logical_content_sha256": logical_sha,
        "compressed_size_bytes": len(compressed),
        "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
        "gzip_mtime": 0,
    }
    manifest = validate_historical_fury_source_bound_team_kill_clock_control_v1(
        _manifest_for_rows(rows=rows, metadata=metadata, partition=partition)
    )
    payload = _canonical_bytes(manifest, newline=True)
    stable = destination / "manifest.json"
    addressed = destination / (
        "historical_fury_source_bound_team_kill_clock_control_v1."
        + manifest["content_address"]["sha256"]
        + ".manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return stable, addressed, partition_path, manifest


def load_historical_fury_source_bound_team_kill_clock_control_v1(
    path: str | Path = DEFAULT_OUTPUT,
) -> tuple[JSONMap, list[JSONMap]]:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        resolved = resolved / "manifest.json"
    try:
        manifest = validate_historical_fury_source_bound_team_kill_clock_control_v1(
            json.loads(resolved.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as error:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"cannot read control manifest: {error}"
        ) from error
    descriptor = _mapping(manifest.get("control_partition"), "control partition")
    partition_path = (resolved.parent / str(descriptor.get("path"))).resolve()
    if partition_path.parent != resolved.parent:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control partition escapes its artifact directory"
        )
    try:
        compressed = partition_path.read_bytes()
        logical = gzip.decompress(compressed)
    except (OSError, gzip.BadGzipFile, EOFError) as error:
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            f"cannot read control partition: {error}"
        ) from error
    if (
        len(compressed) != descriptor.get("compressed_size_bytes")
        or hashlib.sha256(compressed).hexdigest()
        != descriptor.get("compressed_file_sha256")
        or len(logical) != descriptor.get("logical_size_bytes")
        or hashlib.sha256(logical).hexdigest()
        != descriptor.get("logical_content_sha256")
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control partition content differs"
        )
    rows = [
        validate_control_row(json.loads(line)) for line in logical.splitlines() if line
    ]
    if (
        len(rows) != descriptor.get("record_count")
        or _summary(rows) != manifest.get("summary")
    ):
        raise HistoricalFurySourceBoundTeamKillClockControlV1Error(
            "control manifest/partition accounting differs"
        )
    return manifest, rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-manifest", type=Path, default=DEFAULT_EVIDENCE_MANIFEST
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    args = parser.parse_args(argv)
    try:
        stable, addressed, partition, manifest = (
            publish_historical_fury_source_bound_team_kill_clock_control_v1(
                output_directory=args.output_dir,
                evidence_manifest_path=args.evidence_manifest,
            )
        )
    except HistoricalFurySourceBoundTeamKillClockControlV1Error as error:
        print(f"ERROR: {error}")
        return 2
    print(
        json.dumps(
            {
                "manifest": str(stable),
                "addressed_manifest": str(addressed),
                "partition": str(partition),
                "content_sha256": manifest["content_address"]["sha256"],
                "summary": manifest["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HistoricalFurySourceBoundTeamKillClockControlV1Error",
    "build_control_row",
    "build_historical_fury_source_bound_team_kill_clock_control_v1",
    "load_historical_fury_source_bound_team_kill_clock_control_v1",
    "publish_historical_fury_source_bound_team_kill_clock_control_v1",
    "validate_control_row",
    "validate_historical_fury_source_bound_team_kill_clock_control_v1",
]
