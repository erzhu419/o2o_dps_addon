"""Stream one External Chronicle wave into compact hostile-target evidence.

The team-wave model contains an exact trace for replay and audit.  A real
wave-model binding, however, only needs a small target-level outcome sketch:
when a hostile was relevant, observed incoming damage/healing, death or
censoring, a first direct player action, and a compact team-damage curve.
This reducer deliberately emits that sketch only.  It does not copy a trace,
infer target maximum health/effective armor, or turn observed outcome fields
into policy state.
"""

from __future__ import annotations

from collections import defaultdict
import gzip
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "chronicle_external_compact_target_reducer/v1"
ACTION_EVENT_TYPES = frozenset({"START", "GO", "FAIL"})
_HOSTILE_LANE = "HOSTILE_CREATURE"
_DIRECT_PLAYER_KIND = "DIRECT_FRIENDLY_PLAYER"


class ChronicleExternalCompactTargetReducerV1Error(ValueError):
    """The selected compact-reduction input is malformed or absent."""


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalCompactTargetReducerV1Error(f"{label} must be an object")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalCompactTargetReducerV1Error(f"{label} must be nonempty text")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalCompactTargetReducerV1Error(f"{label} must be an integer")
    return value


def _anchor_offset(trace: Mapping[str, Any]) -> int:
    return _integer(_mapping(trace.get("anchor"), label="trace.anchor").get("offset_ms"), label="trace.anchor.offset_ms")


def _guid(side: Mapping[str, Any]) -> str | None:
    value = side.get("guid")
    return value if isinstance(value, str) and value else None


def _compact_event_reference(trace: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    spell = event.get("spell")
    spell_ref: dict[str, Any] | None = None
    if isinstance(spell, Mapping):
        spell_ref = {"id": spell.get("id"), "name": spell.get("name")}
    return {
        "offset_ms": _anchor_offset(trace),
        "trace_index": _integer(trace.get("trace_index"), label="trace.trace_index"),
        "event_type": _text(event.get("event_type"), label="event.event_type"),
        "player_guid": trace.get("player_guid"),
        "source_guid": _guid(_mapping(event.get("source"), label="event.source")),
        "spell": spell_ref,
    }


def _is_direct_friendly_player(trace: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    if trace.get("trace_kind") != "EXACT_PLAYER_EVENT" or not isinstance(trace.get("player_guid"), str):
        return False
    attribution = _mapping(event.get("attribution"), label="event.attribution")
    source = _mapping(event.get("source"), label="event.source")
    return (
        attribution.get("attribution_kind") == _DIRECT_PLAYER_KIND
        and attribution.get("player_guid") == trace.get("player_guid")
        and source.get("lane") == "FRIENDLY_PLAYER"
    )


def _event_amount(event: Mapping[str, Any], *, kind: str) -> int | None:
    payload = event.get("damage" if kind == "DMG" else "healing")
    if not isinstance(payload, Mapping):
        return None
    amount = payload.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return None
    return amount


def _make_target(guid: str) -> dict[str, Any]:
    return {
        "target_guid": guid,
        "first_relevant_offset_ms": None,
        "last_relevant_offset_ms": None,
        "positive_incoming_damage_sum": 0,
        "positive_incoming_damage_event_count": 0,
        "incoming_damage_amount_unavailable_count": 0,
        "incoming_damage_by_attribution_kind": defaultdict(int),
        "incoming_damage_event_count_by_attribution_kind": defaultdict(int),
        "incoming_damage_by_player_guid": defaultdict(int),
        "incoming_damage_event_count_by_player_guid": defaultdict(int),
        "unattributed_positive_incoming_damage_sum": 0,
        "positive_healing_received_sum": 0,
        "positive_healing_received_event_count": 0,
        "healing_amount_unavailable_count": 0,
        "death": None,
        "first_direct_friendly_player_action": None,
        "first_direct_friendly_player_damage": None,
        "first_direct_friendly_player_positive_damage": None,
        "damage_bins": defaultdict(
            lambda: {
                "positive_damage_sum": 0,
                "event_count": 0,
                "positive_damage_by_player_guid": defaultdict(int),
            }
        ),
        "overkill_adjusted_sum": 0,
        "overkill_adjustment_complete": True,
        "overkill_adjustment_reason": None,
        "post_death_positive_incoming_damage_sum": 0,
        "post_death_positive_incoming_damage_event_count": 0,
        "post_death_incoming_damage_amount_unavailable_count": 0,
        "post_death_positive_healing_received_sum": 0,
        "post_death_positive_healing_received_event_count": 0,
        "post_death_healing_amount_unavailable_count": 0,
    }


def _touch(target: dict[str, Any], offset_ms: int) -> None:
    if target["first_relevant_offset_ms"] is None:
        target["first_relevant_offset_ms"] = offset_ms
    target["last_relevant_offset_ms"] = offset_ms


def _record_damage(
    target: dict[str, Any],
    *,
    trace: Mapping[str, Any],
    event: Mapping[str, Any],
    bin_width_ms: int,
) -> None:
    amount = _event_amount(event, kind="DMG")
    if target["death"] is not None:
        if amount is None:
            target["post_death_incoming_damage_amount_unavailable_count"] += 1
        elif amount > 0:
            target["post_death_positive_incoming_damage_sum"] += amount
            target["post_death_positive_incoming_damage_event_count"] += 1
        return
    if amount is None:
        target["incoming_damage_amount_unavailable_count"] += 1
        target["overkill_adjustment_complete"] = False
        target["overkill_adjustment_reason"] = "a DMG row has no integer amount"
        return
    if amount <= 0:
        return
    attribution = _mapping(event.get("attribution"), label="event.attribution")
    kind = attribution.get("attribution_kind")
    kind_text = kind if isinstance(kind, str) and kind else "UNKNOWN"
    target["positive_incoming_damage_sum"] += amount
    target["positive_incoming_damage_event_count"] += 1
    target["incoming_damage_by_attribution_kind"][kind_text] += amount
    target["incoming_damage_event_count_by_attribution_kind"][kind_text] += 1
    player_guid = trace.get("player_guid")
    if isinstance(player_guid, str) and player_guid:
        target["incoming_damage_by_player_guid"][player_guid] += amount
        target["incoming_damage_event_count_by_player_guid"][player_guid] += 1
    if trace.get("trace_kind") == "UNATTRIBUTED_EVENT":
        target["unattributed_positive_incoming_damage_sum"] += amount
    offset_ms = _anchor_offset(trace)
    bucket_start = (offset_ms // bin_width_ms) * bin_width_ms
    bucket = target["damage_bins"][bucket_start]
    bucket["positive_damage_sum"] += amount
    bucket["event_count"] += 1
    if isinstance(player_guid, str) and player_guid:
        bucket["positive_damage_by_player_guid"][player_guid] += amount

    damage = _mapping(event.get("damage"), label="event.damage")
    overkill = damage.get("overkill")
    if (
        isinstance(overkill, bool)
        or not isinstance(overkill, int)
        or overkill < 0
        or overkill > amount
    ):
        target["overkill_adjustment_complete"] = False
        target["overkill_adjustment_reason"] = (
            "DMG overkill is not an integer in [0, amount]"
        )
    else:
        target["overkill_adjusted_sum"] += amount - overkill


def _record_healing(target: dict[str, Any], *, event: Mapping[str, Any]) -> None:
    amount = _event_amount(event, kind="HEAL")
    if target["death"] is not None:
        if amount is None:
            target["post_death_healing_amount_unavailable_count"] += 1
        elif amount > 0:
            target["post_death_positive_healing_received_sum"] += amount
            target["post_death_positive_healing_received_event_count"] += 1
        return
    if amount is None:
        target["healing_amount_unavailable_count"] += 1
    elif amount > 0:
        target["positive_healing_received_sum"] += amount
        target["positive_healing_received_event_count"] += 1


def _render_target(
    target: Mapping[str, Any], *, record_end_offset_ms: int, bin_width_ms: int
) -> dict[str, Any]:
    death = target["death"]
    if death is None:
        death_summary: dict[str, Any] = {
            "status": "CENSORED_AT_RECORD_WAVE_END",
            "offset_ms": record_end_offset_ms,
            "reason": "no DEAD marker for this hostile target inside selected wave",
        }
    else:
        death_summary = {"status": "OBSERVED", **death}
    if target["overkill_adjustment_complete"]:
        adjusted: dict[str, Any] = {
            "value": target["overkill_adjusted_sum"],
            "status": "FIELD_DERIVATION",
            "method": (
                "sum(max(0, DMG.amount - DMG.overkill)) for positive incoming "
                "DMG strictly before the first DEAD marker"
            ),
            "limitation": "a field derivation, not target initial/max health or an armor estimate",
        }
    else:
        adjusted = {
            "value": None,
            "status": "UNAVAILABLE",
            "reason": target["overkill_adjustment_reason"],
        }
    bins = []
    for start, bucket in sorted(target["damage_bins"].items()):
        bins.append(
            {
                "start_offset_ms": start,
                "end_offset_ms_exclusive": start + bin_width_ms,
                "positive_damage_sum": bucket["positive_damage_sum"],
                "event_count": bucket["event_count"],
                "positive_damage_by_player_guid": dict(
                    sorted(bucket["positive_damage_by_player_guid"].items())
                ),
            }
        )
    return {
        "target_guid": target["target_guid"],
        "activity": {
            "first_relevant_offset_ms": target["first_relevant_offset_ms"],
            "last_relevant_offset_ms": target["last_relevant_offset_ms"],
            "status": "OBSERVED",
        },
        "incoming_damage": {
            "positive_sum": target["positive_incoming_damage_sum"],
            "positive_event_count": target["positive_incoming_damage_event_count"],
            "scope": "STRICTLY_BEFORE_FIRST_DEAD_MARKER",
            "amount_unavailable_event_count": target["incoming_damage_amount_unavailable_count"],
            "by_attribution_kind": dict(sorted(target["incoming_damage_by_attribution_kind"].items())),
            "event_count_by_attribution_kind": dict(sorted(target["incoming_damage_event_count_by_attribution_kind"].items())),
            "by_player_guid": dict(
                sorted(target["incoming_damage_by_player_guid"].items())
            ),
            "event_count_by_player_guid": dict(
                sorted(target["incoming_damage_event_count_by_player_guid"].items())
            ),
            "unattributed_positive_sum": target["unattributed_positive_incoming_damage_sum"],
            "overkill_adjusted_effective_damage": adjusted,
        },
        "healing_received": {
            "positive_sum": target["positive_healing_received_sum"],
            "positive_event_count": target["positive_healing_received_event_count"],
            "amount_unavailable_event_count": target["healing_amount_unavailable_count"],
            "scope": "STRICTLY_BEFORE_FIRST_DEAD_MARKER",
        },
        "post_first_death_activity": {
            "incoming_damage": {
                "positive_sum": target["post_death_positive_incoming_damage_sum"],
                "positive_event_count": target[
                    "post_death_positive_incoming_damage_event_count"
                ],
                "amount_unavailable_event_count": target[
                    "post_death_incoming_damage_amount_unavailable_count"
                ],
            },
            "healing_received": {
                "positive_sum": target[
                    "post_death_positive_healing_received_sum"
                ],
                "positive_event_count": target[
                    "post_death_positive_healing_received_event_count"
                ],
                "amount_unavailable_event_count": target[
                    "post_death_healing_amount_unavailable_count"
                ],
            },
            "excluded_from_hp_balance_and_team_kill_clock": True,
        },
        "death": death_summary,
        "first_direct_friendly_player_action": target["first_direct_friendly_player_action"],
        "first_direct_friendly_player_damage": target["first_direct_friendly_player_damage"],
        "first_direct_friendly_player_positive_damage": target["first_direct_friendly_player_positive_damage"],
        "team_damage_timing": {
            "bin_width_ms": bin_width_ms,
            "positive_incoming_damage_bins": bins,
            "semantics": (
                "all positive DMG targeting this hostile; exact player/owner "
                "attribution is retained per bin so a named focal player can "
                "be removed without rereading the exact trace"
            ),
        },
        "binding_boundaries": {
            "target_max_health": {"value": None, "status": "NOT_INFERRED"},
            "effective_armor": {"value": None, "status": "NOT_INFERRED"},
            "observed_damage_is_not_exact_health": True,
        },
    }


def _fury_focal_candidates(
    record: Mapping[str, Any], targets: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Retain the small exact-GUID Fury selector evidence from this record.

    Player summaries and hostile-target reduction have deliberately different
    scopes.  The former is the source record's whole-wave player summary; the
    latter contains only positive damage whose target is a retained hostile.
    Both numbers are kept, and any residual is exposed instead of being
    silently called target damage.
    """

    raw_players = record.get("players")
    if raw_players is None:
        return []
    if not isinstance(raw_players, list):
        raise ChronicleExternalCompactTargetReducerV1Error(
            "record.players must be an array"
        )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_player in raw_players:
        row = _mapping(raw_player, label="record player")
        lane = row.get("warrior_spec_lane")
        if not isinstance(lane, Mapping):
            continue
        if (
            lane.get("partition_key") != "WARRIOR_FURY"
            or lane.get("observed_spec") != "Fury"
            or lane.get("evidence_status") != "OBSERVED"
            or lane.get("exact_guid_match") is not True
        ):
            continue
        player = _mapping(row.get("player"), label="record player identity")
        guid = _text(player.get("guid"), label="record player GUID")
        if guid in seen:
            raise ChronicleExternalCompactTargetReducerV1Error(
                f"record.players repeats exact Fury GUID {guid}"
            )
        seen.add(guid)
        summary = _mapping(row.get("summary"), label="record player summary")
        summary_damage = _integer(
            summary.get("damage_amount"), label="record player summary.damage_amount"
        )
        if summary_damage < 0:
            raise ChronicleExternalCompactTargetReducerV1Error(
                "record player summary.damage_amount must be nonnegative"
            )
        leave_one_out = _mapping(
            row.get("leave_one_player_out_background"),
            label="record player leave-one-out background",
        )
        if leave_one_out.get("focal_player_guid") != guid:
            raise ChronicleExternalCompactTargetReducerV1Error(
                "Fury leave-one-out focal GUID differs from player GUID"
            )
        excluded_damage = _integer(
            leave_one_out.get("excluded_focal_damage_amount"),
            label="excluded focal damage amount",
        )
        retained_predeath_hostile_damage = sum(
            int(target["incoming_damage_by_player_guid"].get(guid, 0))
            for target in targets.values()
        )
        candidates.append(
            {
                "player_guid": guid,
                "player_class": player.get("class"),
                "observed_spec": lane.get("observed_spec"),
                "source_summary_damage_amount": summary_damage,
                "exact_leave_one_out_excluded_damage_amount": excluded_damage,
                "source_summary_closes_to_leave_one_out": (
                    summary_damage == excluded_damage
                ),
                "retained_predeath_positive_hostile_target_damage_amount": (
                    retained_predeath_hostile_damage
                ),
                "source_summary_minus_retained_predeath_target_damage_amount": (
                    summary_damage - retained_predeath_hostile_damage
                ),
                "retained_predeath_target_damage_closes_to_source_summary": (
                    summary_damage == retained_predeath_hostile_damage
                ),
                "source_lane_role": lane.get("role"),
                "source_voting_authorized": lane.get("voting_authorized"),
                "comparison_authorized": False,
            }
        )
    return sorted(candidates, key=lambda row: str(row["player_guid"]))


def reduce_external_team_wave_record(
    path: str | Path,
    *,
    instance_id: str,
    encounter_id: str,
    wave_id: str | None = None,
    wave_ordinal: int | None = None,
    bin_width_ms: int = 1_000,
) -> dict[str, Any]:
    """Return compact target evidence for exactly one selected gzip JSONL wave.

    The partition is read line-by-line and reduction stops immediately after
    the requested record.  ``wave_id`` and ``wave_ordinal`` are mutually
    exclusive so a caller cannot accidentally bind a similarly named wave.
    """

    if not isinstance(instance_id, str) or not instance_id:
        raise ChronicleExternalCompactTargetReducerV1Error("instance_id must be nonempty text")
    if not isinstance(encounter_id, str) or not encounter_id:
        raise ChronicleExternalCompactTargetReducerV1Error("encounter_id must be nonempty text")
    if (wave_id is None) == (wave_ordinal is None):
        raise ChronicleExternalCompactTargetReducerV1Error(
            "provide exactly one of wave_id or wave_ordinal"
        )
    if wave_id is not None and (not isinstance(wave_id, str) or not wave_id):
        raise ChronicleExternalCompactTargetReducerV1Error("wave_id must be nonempty text")
    if wave_ordinal is not None and (
        isinstance(wave_ordinal, bool) or not isinstance(wave_ordinal, int) or wave_ordinal < 0
    ):
        raise ChronicleExternalCompactTargetReducerV1Error("wave_ordinal must be a nonnegative integer")
    if isinstance(bin_width_ms, bool) or not isinstance(bin_width_ms, int) or bin_width_ms <= 0:
        raise ChronicleExternalCompactTargetReducerV1Error("bin_width_ms must be a positive integer")

    source = Path(path)
    try:
        handle = gzip.open(source, "rt", encoding="utf-8")
    except OSError as error:
        raise ChronicleExternalCompactTargetReducerV1Error(f"cannot open gzip JSONL {source}: {error}") from error
    with handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ChronicleExternalCompactTargetReducerV1Error(
                    f"invalid JSON at {source}:{line_number}: {error}"
                ) from error
            if not isinstance(record, Mapping):
                raise ChronicleExternalCompactTargetReducerV1Error(
                    f"record at {source}:{line_number} must be an object"
                )
            wave = record.get("wave")
            if not isinstance(wave, Mapping):
                continue
            if wave.get("instance_id") != instance_id or wave.get("encounter_id") != encounter_id:
                continue
            matched = wave.get("wave_id") == wave_id if wave_id is not None else wave.get("wave_ordinal") == wave_ordinal
            if not matched:
                continue
            return _reduce_record(record, line_number=line_number, source=source, bin_width_ms=bin_width_ms)
    selector = f"wave_id={wave_id!r}" if wave_id is not None else f"wave_ordinal={wave_ordinal!r}"
    raise ChronicleExternalCompactTargetReducerV1Error(
        f"selected wave not found for instance_id={instance_id!r}, encounter_id={encounter_id!r}, {selector}"
    )


def _reduce_record(
    record: Mapping[str, Any], *, line_number: int, source: Path, bin_width_ms: int
) -> dict[str, Any]:
    wave = _mapping(record.get("wave"), label="record.wave")
    trace = record.get("exact_trace")
    if not isinstance(trace, list):
        raise ChronicleExternalCompactTargetReducerV1Error("record.exact_trace must be an array")
    targets: dict[str, dict[str, Any]] = {}
    record_end_offset_ms = 0
    for raw_trace in trace:
        row = _mapping(raw_trace, label="exact_trace row")
        offset_ms = _anchor_offset(row)
        record_end_offset_ms = max(record_end_offset_ms, offset_ms)
        if row.get("trace_kind") in {
            "CLASSIFICATION_CONTEXT",
            "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT",
        }:
            continue
        event = _mapping(row.get("event"), label="trace event")
        event_type = _text(event.get("event_type"), label="event.event_type")
        source_side = _mapping(event.get("source"), label="event.source")
        target_side = _mapping(event.get("target"), label="event.target")
        hostile_sides = [side for side in (source_side, target_side) if side.get("lane") == _HOSTILE_LANE and _guid(side)]
        for side in hostile_sides:
            guid = _guid(side)
            assert guid is not None
            _touch(targets.setdefault(guid, _make_target(guid)), offset_ms)
        target_guid = _guid(target_side) if target_side.get("lane") == _HOSTILE_LANE else None
        if target_guid is None:
            continue
        target = targets[target_guid]
        if event_type == "DMG":
            _record_damage(target, trace=row, event=event, bin_width_ms=bin_width_ms)
            if _is_direct_friendly_player(row, event):
                reference = _compact_event_reference(row, event)
                if target["first_direct_friendly_player_damage"] is None:
                    target["first_direct_friendly_player_damage"] = reference
                amount = _event_amount(event, kind="DMG")
                if amount is not None and amount > 0 and target["first_direct_friendly_player_positive_damage"] is None:
                    target["first_direct_friendly_player_positive_damage"] = reference
        elif event_type == "HEAL":
            _record_healing(target, event=event)
        elif event_type == "DEAD":
            if target["death"] is None:
                target["death"] = {
                    "offset_ms": offset_ms,
                    "trace_index": _integer(row.get("trace_index"), label="trace.trace_index"),
                }
        elif event_type in ACTION_EVENT_TYPES and _is_direct_friendly_player(row, event):
            if target["first_direct_friendly_player_action"] is None:
                target["first_direct_friendly_player_action"] = _compact_event_reference(row, event)
    fury_focal_candidates = _fury_focal_candidates(record, targets)
    return {
        "schema": SCHEMA,
        "status": "DESCRIPTIVE_OUTCOME_ONLY",
        "source_selection": {
            "path": str(source),
            "jsonl_line": line_number,
            "instance_id": wave.get("instance_id"),
            "encounter_id": wave.get("encounter_id"),
            "encounter_ordinal": wave.get("encounter_ordinal"),
            "wave_id": wave.get("wave_id"),
            "wave_ordinal": wave.get("wave_ordinal"),
        },
        "record_end_offset_ms": record_end_offset_ms,
        "target_count": len(targets),
        "fury_focal_candidates": fury_focal_candidates,
        "hostile_targets": [
            _render_target(targets[guid], record_end_offset_ms=record_end_offset_ms, bin_width_ms=bin_width_ms)
            for guid in sorted(targets)
        ],
        "scientific_boundaries": {
            "exact_trace_copied": False,
            "policy_training_or_comparison_authorized": False,
            "target_health_or_armor_inferred": False,
            "future_outcomes_allowed_in_policy_state": False,
            "fury_candidates_selected_by_outcome": False,
            "hp_and_team_damage_truncated_at_first_dead_marker": True,
        },
    }


__all__ = [
    "ChronicleExternalCompactTargetReducerV1Error",
    "SCHEMA",
    "reduce_external_team_wave_record",
]
