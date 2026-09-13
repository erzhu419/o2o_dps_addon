"""Compile strict-prefix Fury window-start checkpoint evidence.

The Chronicle state streams can identify a useful subset of the player state
at the first bound decision, but they do not expose a canonical simulator
snapshot.  This compiler therefore records every required field separately:
an observed subset may be retained while the exact checkpoint stays blocked.

Only events strictly before the first bound decision are admitted.  In
particular, target kill budgets, later damage, later aura removals, and default
zeroes are never promoted to window-start observations.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from . import chronicle_external_aura_attribution_v2 as aura_v2
from . import chronicle_external_event_normalizer_v1 as core_state_source
from . import chronicle_external_state_event_normalizer_v1 as state_source
from .historical_fury_source_bound_environment_evidence_v1 import (
    load_historical_fury_source_bound_environment_evidence_v1,
)


JSONMap = dict[str, Any]

SCHEMA = "historical_fury_source_bound_prefix_checkpoint/v1"
ROW_SCHEMA = "historical_fury_source_bound_prefix_checkpoint_row/v1"
IMPLEMENTATION_REVISION = (
    "v1.1_strict_prefix_fieldwise_fail_closed_dynamic_v4_interface_audit"
)
STATUS = "PREFIX_STATE_EVIDENCE_COMPILED_EXACT_CHECKPOINT_BLOCKED"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_EVIDENCE_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_source_bound_environment_evidence"
    / "v1"
    / "manifest.json"
)
DEFAULT_RAW_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "chronicle_raw"
    / "external_api"
    / "v1"
    / "manifests"
    / "fcd5388de131ac0cd784b23cb31bc565e3368cdc150129338fc5fbc16a645f99.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_source_bound_prefix_checkpoint"
    / "v1"
)

STATE_STREAMS = ("resource_change", "aura", "aura_cast")
REQUIRED_FIELDS = (
    "target.max_health",
    "target.current_health",
    "player.rage_current",
    "player.stance",
    "timers.gcd_remaining_ms",
    "timers.cooldowns_remaining_ms",
    "timers.main_hand_swing_remaining_ms",
    "timers.off_hand_swing_remaining_ms",
    "queue.next_swing",
    "player.self_auras_and_procs",
    "target.candidate_owned_existing_debuffs",
)
OBSERVATION_CATEGORIES = frozenset({"EXACT", "PARTIAL", "MISSING"})

STANCE_BY_SPELL_ID = {
    71: "DEFENSIVE",
    2457: "BATTLE",
    2458: "BERSERKER",
    45597: "GLADIATOR",
    45598: "GLADIATOR",
}
STANCE_BY_NAME = {
    "battle stance": "BATTLE",
    "berserker stance": "BERSERKER",
    "defensive stance": "DEFENSIVE",
    "gladiator stance": "GLADIATOR",
    "战斗姿态": "BATTLE",
    "狂暴姿态": "BERSERKER",
    "防御姿态": "DEFENSIVE",
    "角斗士姿态": "GLADIATOR",
}


class HistoricalFuryPrefixCheckpointV1Error(RuntimeError):
    """A source binding or checkpoint contract is inconsistent."""


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFuryPrefixCheckpointV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFuryPrefixCheckpointV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFuryPrefixCheckpointV1Error(f"{label} must be nonempty text")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalFuryPrefixCheckpointV1Error(f"{label} must be an integer")
    return value


def _optional_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _same_guid(left: Any, right: str) -> bool:
    return isinstance(left, str) and left.casefold() == right.casefold()


def _field(
    category: str,
    status: str,
    *,
    value: Any,
    source: str,
    reason: str | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> JSONMap:
    if category not in OBSERVATION_CATEGORIES:
        raise HistoricalFuryPrefixCheckpointV1Error(
            f"unsupported observation category {category!r}"
        )
    if category == "MISSING" and value is not None:
        raise HistoricalFuryPrefixCheckpointV1Error(
            "a missing checkpoint field cannot carry a value"
        )
    result: JSONMap = {
        "observation_category": category,
        "observation_status": status,
        "value": value,
        "source": source,
        "reason": reason,
        "default_value_used": False,
        "future_suffix_used": False,
        "exact_checkpoint_equivalent": category == "EXACT",
    }
    if diagnostics is not None:
        result["diagnostics"] = dict(diagnostics)
    return result


def _missing(status: str, *, source: str, reason: str, diagnostics: Mapping[str, Any] | None = None) -> JSONMap:
    return _field(
        "MISSING",
        status,
        value=None,
        source=source,
        reason=reason,
        diagnostics=diagnostics,
    )


def _event_order(row: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    stream = _text(row.get("stream_type"), "state event stream_type")
    if stream not in state_source.STREAM_ORDER:
        raise HistoricalFuryPrefixCheckpointV1Error(
            f"unsupported state stream in checkpoint input: {stream!r}"
        )
    provenance = _mapping(row.get("provenance"), "state event provenance")
    return (
        _integer(row.get("timestamp_ms"), "state event timestamp_ms"),
        _integer(row.get("event_index"), "state event event_index"),
        state_source.STREAM_ORDER[stream],
        _integer(provenance.get("frame_index"), "state event frame_index"),
        _integer(
            provenance.get("frame_message_index"),
            "state event frame_message_index",
        ),
    )


def _anchor(row: Mapping[str, Any]) -> JSONMap:
    provenance = _mapping(row.get("provenance"), "state event provenance")
    official = _mapping(row.get("official"), "state event official")
    return {
        "timestamp_ms": _integer(row.get("timestamp_ms"), "timestamp_ms"),
        "event_index": _integer(row.get("event_index"), "event_index"),
        "stream_type": _text(row.get("stream_type"), "stream_type"),
        "frame_index": _integer(provenance.get("frame_index"), "frame_index"),
        "frame_message_index": _integer(
            provenance.get("frame_message_index"), "frame_message_index"
        ),
        "official_message_sha256": official.get("message_sha256"),
    }


def _spell_projection(event: Mapping[str, Any], stream: str) -> tuple[str | None, int | None]:
    raw_spell = event.get("spell") if stream == "aura_cast" else event.get("spell_data")
    name: str | None = None
    spell_id: int | None = None
    if isinstance(raw_spell, Mapping):
        name = _optional_identifier(raw_spell.get("name"))
        raw_id = raw_spell.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            spell_id = raw_id
    top_name = None
    if stream == "aura":
        top_name = _optional_identifier(event.get("spell_name"))
    elif stream == "resource_change":
        top_name = _optional_identifier(event.get("source_name"))
    if name and top_name and name != top_name:
        name = None
    return name or top_name, spell_id


def _normalized_state_row(
    *,
    instance_id: str,
    encounter_id: str,
    first_timestamp_ms: int,
    stream: str,
    frame_index: int,
    message_index: int,
    message: Mapping[str, Any],
    source_manifest_sha256: str,
    source_object_sha256: str,
) -> JSONMap:
    event = _mapping(message.get("event"), "decoded state event")
    meta = _mapping(event.get("meta"), "decoded state EventMeta")
    event_index = _integer(meta.get("event_index"), "EventMeta.event_index")
    offset_ms = _integer(meta.get("offset_ms"), "EventMeta.offset_ms")
    spell, spell_id = _spell_projection(event, stream)
    if stream == "resource_change":
        source_guid = _optional_identifier(event.get("caster"))
        target_guid = _optional_identifier(event.get("target"))
        value: int | None = _integer(event.get("amount"), "resource amount")
    elif stream == "aura":
        source_guid = None
        target_guid = _optional_identifier(event.get("target"))
        value = _integer(event.get("current_amount"), "aura current_amount")
    elif stream == "aura_cast":
        source_guid = _optional_identifier(event.get("caster"))
        target_guid = _optional_identifier(event.get("target"))
        value = None
    else:  # pragma: no cover - caller is closed over STATE_STREAMS.
        raise HistoricalFuryPrefixCheckpointV1Error(f"unsupported stream {stream!r}")
    return {
        "instance": instance_id,
        "encounter": encounter_id,
        "timestamp_ms": first_timestamp_ms + offset_ms,
        "event_index": event_index,
        "offset_ms": offset_ms,
        "stream_type": stream,
        "source_guid": source_guid,
        "target_guid": target_guid,
        "spell": spell,
        "spell_id": spell_id,
        "value": value,
        "synthetic": bool(meta.get("is_synthetic")),
        "event_meta": dict(meta),
        "state_payload": {key: child for key, child in event.items() if key != "meta"},
        "official": {
            "message": dict(event),
            "message_sha256": message.get("message_sha256"),
        },
        "provenance": {
            "source_manifest_sha256": source_manifest_sha256,
            "source_object_sha256": source_object_sha256,
            "frame_index": frame_index,
            "frame_message_index": message_index,
        },
    }


def _first_decision(evidence_row: Mapping[str, Any]) -> Mapping[str, Any]:
    decisions = _array(
        evidence_row.get("bound_decision_prefix_deltas"),
        "bound_decision_prefix_deltas",
    )
    if not decisions:
        raise HistoricalFuryPrefixCheckpointV1Error(
            "environment evidence row has no bound decision"
        )
    decision = _mapping(decisions[0], "first bound decision")
    order = _array(decision.get("order_key"), "first decision order_key")
    if len(order) < 2:
        raise HistoricalFuryPrefixCheckpointV1Error(
            "first decision order_key lacks timestamp/event index"
        )
    execution = _mapping(
        evidence_row.get("execution_window_contract"), "execution_window_contract"
    )
    support = _mapping(
        execution.get("bound_decision_support"), "bound_decision_support"
    )
    if support.get("start_order_key") != order:
        raise HistoricalFuryPrefixCheckpointV1Error(
            "first decision and execution-window start differ"
        )
    return decision


def _aura_identity(row: Mapping[str, Any]) -> tuple[str, int | None] | None:
    name = _optional_identifier(row.get("spell"))
    raw_id = row.get("spell_id")
    spell_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    if name is None and spell_id is None:
        return None
    return name or "", spell_id


def _aura_state(row: Mapping[str, Any]) -> str | None:
    payload = _mapping(row.get("state_payload"), "aura state payload")
    state = payload.get("state")
    if not isinstance(state, Mapping):
        return None
    return _optional_identifier(state.get("name"))


def _active_self_auras(
    rows: Iterable[Mapping[str, Any]], player_guid: str
) -> tuple[list[JSONMap], list[JSONMap]]:
    active: dict[tuple[str, int | None], JSONMap] = {}
    unresolved: dict[tuple[str, int | None], JSONMap] = {}
    for row in rows:
        if row.get("stream_type") != "aura" or not _same_guid(
            row.get("target_guid"), player_guid
        ):
            continue
        identity = _aura_identity(row)
        if identity is None:
            continue
        state = _aura_state(row)
        current = _integer(row.get("value"), "aura current_amount")
        if state == "StateRemoved" or current <= 0:
            active.pop(identity, None)
            unresolved.pop(identity, None)
            continue
        if state not in {"StateAdded", "StateModified"}:
            active.pop(identity, None)
            unresolved[identity] = {
                "spell": identity[0] or None,
                "spell_id": identity[1],
                "last_transition": _anchor(row),
                "reason": "AURA_STATE_UNKNOWN_NOT_PROMOTED",
            }
            continue
        payload = _mapping(row.get("state_payload"), "aura state payload")
        unresolved.pop(identity, None)
        active[identity] = {
            "spell": identity[0] or None,
            "spell_id": identity[1],
            "stacks": current,
            "is_buff": payload.get("is_buff"),
            "last_transition_synthetic": bool(row.get("synthetic")),
            "last_transition": _anchor(row),
            "remaining_duration_ms": None,
            "remaining_duration_status": "MISSING_NOT_IN_AURA_PROTO",
        }
    key = lambda item: (
        item.get("spell_id") is None,
        item.get("spell_id") if item.get("spell_id") is not None else 0,
        item.get("spell") or "",
    )
    return sorted(active.values(), key=key), sorted(unresolved.values(), key=key)


def _stance_field(active_auras: Sequence[Mapping[str, Any]]) -> JSONMap:
    observed: list[JSONMap] = []
    for aura in active_auras:
        raw_id = aura.get("spell_id")
        name = _optional_identifier(aura.get("spell"))
        stance = STANCE_BY_SPELL_ID.get(raw_id) if isinstance(raw_id, int) else None
        if stance is None and name is not None:
            stance = STANCE_BY_NAME.get(name.casefold())
        if stance is not None:
            observed.append(
                {
                    "stance": stance,
                    "spell": name,
                    "spell_id": raw_id,
                    "last_transition": aura.get("last_transition"),
                }
            )
    unique = sorted({row["stance"] for row in observed})
    if len(unique) == 1:
        return _field(
            "EXACT",
            "EXACT_ACTIVE_STANCE_AURA_IN_STRICT_PREFIX",
            value=unique[0],
            source="chronicle.aura.strict_prefix_active_state",
            diagnostics={"matching_active_aura_count": len(observed), "evidence": observed},
        )
    if not unique:
        return _missing(
            "MISSING_NO_ACTIVE_STANCE_AURA_BEFORE_WINDOW",
            source="chronicle.aura.strict_prefix_active_state",
            reason="No stance is inferred from class, expert policy, or simulator default.",
        )
    return _missing(
        "MISSING_AMBIGUOUS_MULTIPLE_ACTIVE_STANCE_AURAS",
        source="chronicle.aura.strict_prefix_active_state",
        reason="Multiple distinct stance identities are active at the cutoff.",
        diagnostics={"observed": observed},
    )


def _resource_changes(
    rows: Iterable[Mapping[str, Any]], player_guid: str
) -> list[JSONMap]:
    result: list[JSONMap] = []
    for row in rows:
        if row.get("stream_type") != "resource_change":
            continue
        if not _same_guid(row.get("source_guid"), player_guid) and not _same_guid(
            row.get("target_guid"), player_guid
        ):
            continue
        payload = _mapping(row.get("state_payload"), "resource state payload")
        resource_type = _optional_identifier(payload.get("resource_type"))
        if resource_type is not None and resource_type.casefold() != "rage":
            continue
        result.append(
            {
                "anchor": _anchor(row),
                "amount_raw": row.get("value"),
                "direction": payload.get("direction"),
                "over_resource_raw": payload.get("over_resource"),
                "resource_type": resource_type,
                "source_name": payload.get("source_name"),
                "source_guid": row.get("source_guid"),
                "target_guid": row.get("target_guid"),
            }
        )
    return result


def _candidate_debuff_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    player_guid: str,
    alive_target_guids: set[str],
) -> JSONMap:
    attribution_rows, summary = aura_v2.attribute_state_rows(
        (row for row in rows if row.get("stream_type") in {"aura", "aura_cast"})
    )
    latest: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for event in attribution_rows:
        if event.get("aura_role") != "REGISTERED_HOSTILE_ARMOR_AURA_DIAGNOSTIC":
            continue
        target = _optional_identifier(event.get("target_guid"))
        debuff_id = _optional_identifier(event.get("debuff_id"))
        spell_id = event.get("spell_id")
        if target is None or debuff_id is None or not isinstance(spell_id, int):
            continue
        latest[(target, debuff_id, spell_id)] = event
    active_registered: list[JSONMap] = []
    exact_candidate_contributions: list[JSONMap] = []
    alive_folded = {value.casefold() for value in alive_target_guids}
    for (target, debuff_id, spell_id), event in sorted(latest.items()):
        if target.casefold() not in alive_folded:
            continue
        lifecycle = _mapping(event.get("lifecycle"), "armor aura lifecycle")
        current = _integer(lifecycle.get("current_amount"), "armor aura current_amount")
        if current <= 0:
            continue
        exact_sources = _mapping(
            lifecycle.get("active_exact_source_counts"),
            "armor aura active_exact_source_counts",
        )
        candidate_count = 0
        for source_guid, count in exact_sources.items():
            if isinstance(count, bool) or not isinstance(count, int):
                raise HistoricalFuryPrefixCheckpointV1Error(
                    "candidate armor attribution count is not an integer"
                )
            if _same_guid(source_guid, player_guid):
                candidate_count += count
        unresolved_count = _integer(
            lifecycle.get("active_unresolved_stack_count"),
            "active_unresolved_stack_count",
        )
        record = {
            "target_guid": target,
            "debuff_id": debuff_id,
            "spell": event.get("spell"),
            "spell_id": spell_id,
            "observed_active_stacks": current,
            "exact_source_stack_counts": dict(sorted(exact_sources.items())),
            "unresolved_source_stack_count": unresolved_count,
            "remaining_duration_ms": None,
            "remaining_duration_status": "MISSING_NOT_IN_AURA_PROTO",
            "last_transition": _mapping(event.get("aura_anchor"), "aura anchor"),
        }
        active_registered.append(record)
        if candidate_count > 0:
            exact_candidate_contributions.append(
                {
                    "target_guid": target,
                    "debuff_id": debuff_id,
                    "spell_id": spell_id,
                    "exact_candidate_stack_count": candidate_count,
                    "remaining_duration_ms": None,
                }
            )
    counts = _mapping(summary.get("counts"), "armor attribution counts")
    return {
        "active_registered_physical_armor_auras": active_registered,
        "exact_candidate_physical_armor_contributions": exact_candidate_contributions,
        "registered_state_event_count": sum(
            value
            for key, value in counts.items()
            if isinstance(value, int) and key == "registered_aura_state_seen"
        ),
        "coverage_boundary": (
            "Only registered physical-armor aura contributions can receive exact "
            "caster attribution; this is not the complete candidate debuff set."
        ),
    }


def _stream_available(coverage: Mapping[str, Any], stream: str) -> bool:
    value = coverage.get(stream)
    return isinstance(value, Mapping) and value.get("status") == "AVAILABLE_FRAME_VERIFIED"


def compile_checkpoint_row(
    evidence_row: Mapping[str, Any],
    *,
    state_rows: Sequence[Mapping[str, Any]],
    stream_coverage: Mapping[str, Any],
) -> JSONMap:
    """Compile one fieldwise checkpoint row from already prefix-filtered events."""

    source_identity = _mapping(evidence_row.get("source_identity"), "source_identity")
    instance_id = _text(source_identity.get("instance_id"), "instance_id")
    encounter_id = _text(source_identity.get("encounter_id"), "encounter_id")
    player_guid = _text(source_identity.get("player_guid"), "player_guid")
    decision = _first_decision(evidence_row)
    decision_order = _array(decision.get("order_key"), "decision order_key")
    cutoff = (
        _integer(decision_order[0], "decision timestamp"),
        _integer(decision_order[1], "decision event index"),
    )
    ordered_rows = sorted(state_rows, key=_event_order)
    for state_row in ordered_rows:
        if state_row.get("instance") != instance_id or state_row.get("encounter") != encounter_id:
            raise HistoricalFuryPrefixCheckpointV1Error(
                "state event belongs to a different source encounter"
            )
        if _event_order(state_row)[:2] >= cutoff:
            raise HistoricalFuryPrefixCheckpointV1Error(
                "state event is not strictly before the window-start cutoff"
            )

    prefix_state = _mapping(decision.get("strict_prefix_state"), "strict_prefix_state")
    alive_target_guids = sorted(
        {
            _text(value, "alive target GUID")
            for value in _array(
                prefix_state.get("alive_target_guids"), "alive_target_guids"
            )
        }
    )
    active_self, unresolved_self = _active_self_auras(ordered_rows, player_guid)
    rage_changes = _resource_changes(ordered_rows, player_guid)

    fields: JSONMap = {}
    hp_diagnostics = {
        "target_guids_at_cutoff": alive_target_guids,
        "strict_prefix_focal_damage_amount": prefix_state.get(
            "focal_direct_damage_prefix_amount"
        ),
        "retrospective_kill_budget_materialized": False,
        "same_entry_health_prior_materialized": False,
        "post_cutoff_damage_or_healing_materialized": False,
    }
    fields["target.max_health"] = _missing(
        "MISSING_NO_STRICT_PREFIX_ABSOLUTE_MAX_HEALTH",
        source="chronicle.external.core.strict_prefix",
        reason=(
            "The source exposes damage/lifecycle evidence, not an absolute maximum-health "
            "observation at the cutoff."
        ),
        diagnostics=hp_diagnostics,
    )
    fields["target.current_health"] = _missing(
        "MISSING_NO_STRICT_PREFIX_ABSOLUTE_CURRENT_HEALTH",
        source="chronicle.external.core.strict_prefix",
        reason=(
            "Current health cannot be reconstructed from prefix damage without an observed "
            "maximum/initial-health baseline."
        ),
        diagnostics=hp_diagnostics,
    )
    if _stream_available(stream_coverage, "resource_change"):
        rage_reason = (
            "ResourceChange carries changes and over-resource amounts, not an absolute "
            "current-rage baseline; raw units/directions are retained without integration."
        )
        rage_diagnostics: Mapping[str, Any] = {
            "strict_prefix_rage_change_event_count": len(rage_changes),
            "strict_prefix_rage_changes": rage_changes,
            "change_values_integrated": False,
            "unit_scale_inferred": False,
        }
    else:
        rage_reason = "The required resource_change encounter frame is unavailable."
        rage_diagnostics = {"strict_prefix_rage_change_event_count": 0}
    fields["player.rage_current"] = _missing(
        "MISSING_ABSOLUTE_RAGE_BASELINE",
        source="chronicle.resource_change.strict_prefix",
        reason=rage_reason,
        diagnostics=rage_diagnostics,
    )
    fields["player.stance"] = (
        _stance_field(active_self)
        if _stream_available(stream_coverage, "aura")
        else _missing(
            "MISSING_AURA_STREAM_FRAME",
            source="chronicle.aura.strict_prefix_active_state",
            reason="The required aura encounter frame is unavailable.",
        )
    )

    timer_missing = {
        "timers.gcd_remaining_ms": (
            "MISSING_NO_GCD_TIMER_EVENT",
            "Chronicle START/GO events do not expose remaining GCD at the cutoff.",
        ),
        "timers.cooldowns_remaining_ms": (
            "MISSING_NO_COOLDOWN_TIMER_SNAPSHOT",
            "No complete per-action cooldown remainder snapshot exists in the strict prefix.",
        ),
        "timers.main_hand_swing_remaining_ms": (
            "MISSING_NO_MAIN_HAND_SWING_TIMER_SNAPSHOT",
            "Prior white hits do not identify the exact pending main-hand swing timer.",
        ),
        "timers.off_hand_swing_remaining_ms": (
            "MISSING_NO_OFF_HAND_SWING_TIMER_SNAPSHOT",
            "Prior white hits do not identify the exact pending off-hand swing timer.",
        ),
    }
    for field_name, (status, reason) in timer_missing.items():
        fields[field_name] = _missing(
            status,
            source="chronicle.external.strict_prefix",
            reason=reason,
        )
    fields["queue.next_swing"] = _missing(
        "MISSING_CLIENT_NEXT_SWING_QUEUE_STATE",
        source="chronicle.external.server_events.strict_prefix",
        reason=(
            "Server START/GO events identify attempts/outcomes, not the client's queued "
            "Heroic Strike or Cleave state at the earlier cutoff."
        ),
    )

    if _stream_available(stream_coverage, "aura"):
        fields["player.self_auras_and_procs"] = _field(
            "PARTIAL",
            "PARTIAL_ACTIVE_IDENTITIES_AND_STACKS_DURATION_MISSING",
            value={
                "observed_active": active_self,
                "unresolved_state_transitions": unresolved_self,
            },
            source="chronicle.aura.strict_prefix_active_state",
            reason=(
                "Active identity/stack transitions are prefix-observed, but remaining aura "
                "durations and complete proc internals are not exposed."
            ),
            diagnostics={
                "observed_active_count": len(active_self),
                "unresolved_identity_count": len(unresolved_self),
            },
        )
    else:
        fields["player.self_auras_and_procs"] = _missing(
            "MISSING_AURA_STREAM_FRAME",
            source="chronicle.aura.strict_prefix_active_state",
            reason="The required aura encounter frame is unavailable.",
        )

    if _stream_available(stream_coverage, "aura") and _stream_available(
        stream_coverage, "aura_cast"
    ):
        debuff_evidence = _candidate_debuff_evidence(
            ordered_rows,
            player_guid=player_guid,
            alive_target_guids=set(alive_target_guids),
        )
        fields["target.candidate_owned_existing_debuffs"] = _field(
            "PARTIAL",
            "PARTIAL_REGISTERED_PHYSICAL_ARMOR_ATTRIBUTION_ONLY",
            value=debuff_evidence,
            source="chronicle.aura_plus_aura_cast.strict_prefix",
            reason=(
                "Aura lacks a general caster field and remaining duration; only uniquely "
                "joined registered physical-armor contributions are attributable."
            ),
        )
    else:
        fields["target.candidate_owned_existing_debuffs"] = _missing(
            "MISSING_AURA_OR_AURA_CAST_STREAM_FRAME",
            source="chronicle.aura_plus_aura_cast.strict_prefix",
            reason="Both aura and aura_cast encounter frames are required for attribution.",
        )

    categories = Counter(
        _mapping(fields[name], f"field {name}").get("observation_category")
        for name in REQUIRED_FIELDS
    )
    blockers = [
        name
        for name in REQUIRED_FIELDS
        if not bool(_mapping(fields[name], f"field {name}").get("exact_checkpoint_equivalent"))
    ]
    max_prefix_order = list(_event_order(ordered_rows[-1])) if ordered_rows else None
    result = {
        "schema": ROW_SCHEMA,
        "status": STATUS,
        "segment_ref": _text(evidence_row.get("segment_ref"), "segment_ref"),
        "source_identity": {
            "instance_id": instance_id,
            "encounter_id": encounter_id,
            "player_guid": player_guid,
            "wave_id": source_identity.get("wave_id"),
            "wave_ordinal": source_identity.get("wave_ordinal"),
        },
        "window_start": {
            "semantics": "STATE_STRICTLY_BEFORE_FIRST_BOUND_DECISION",
            "cutoff_exclusive_order_key": list(decision_order),
            "first_action_key": decision.get("action_key"),
            "alive_target_guids": alive_target_guids,
            "last_observed_hostile_target_guid": prefix_state.get(
                "last_observed_hostile_target_guid"
            ),
            "strict_prefix_state_source": "bound_decision_prefix_deltas[0].strict_prefix_state",
        },
        "strict_prefix_input": {
            "state_event_count": len(ordered_rows),
            "maximum_included_state_event_order_key": max_prefix_order,
            "stream_coverage": deepcopy(dict(stream_coverage)),
            "future_suffix_used": False,
            "current_decision_event_used_as_state": False,
        },
        "fields": fields,
        "coverage": {
            "exact_field_count": categories["EXACT"],
            "partial_field_count": categories["PARTIAL"],
            "missing_field_count": categories["MISSING"],
            "required_field_count": len(REQUIRED_FIELDS),
        },
        "exact_checkpoint_ready": not blockers,
        "blocking_fields": blockers,
        "authorization": {
            "simulator_checkpoint_seed_authorized": False,
            "training_authorized": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
        },
    }
    return validate_checkpoint_row(result)


def validate_checkpoint_row(value: Mapping[str, Any]) -> JSONMap:
    row = deepcopy(dict(_mapping(value, "checkpoint row")))
    if row.get("schema") != ROW_SCHEMA or row.get("status") != STATUS:
        raise HistoricalFuryPrefixCheckpointV1Error("checkpoint row schema/status mismatch")
    fields = _mapping(row.get("fields"), "checkpoint fields")
    if set(fields) != set(REQUIRED_FIELDS):
        raise HistoricalFuryPrefixCheckpointV1Error(
            "checkpoint row does not contain exactly the required fields"
        )
    blockers: list[str] = []
    categories: Counter[str] = Counter()
    for name in REQUIRED_FIELDS:
        field = _mapping(fields[name], f"checkpoint field {name}")
        category = field.get("observation_category")
        if category not in OBSERVATION_CATEGORIES:
            raise HistoricalFuryPrefixCheckpointV1Error(
                f"checkpoint field {name} has invalid observation category"
            )
        categories[str(category)] += 1
        if field.get("default_value_used") is not False:
            raise HistoricalFuryPrefixCheckpointV1Error(
                f"checkpoint field {name} uses or obscures a default value"
            )
        if field.get("future_suffix_used") is not False:
            raise HistoricalFuryPrefixCheckpointV1Error(
                f"checkpoint field {name} uses future suffix evidence"
            )
        exact = field.get("exact_checkpoint_equivalent")
        if not isinstance(exact, bool) or exact != (category == "EXACT"):
            raise HistoricalFuryPrefixCheckpointV1Error(
                f"checkpoint field {name} exactness/category mismatch"
            )
        if category == "MISSING" and field.get("value") is not None:
            raise HistoricalFuryPrefixCheckpointV1Error(
                f"missing checkpoint field {name} carries a value"
            )
        if not exact:
            blockers.append(name)
    if fields["target.max_health"] is fields["target.current_health"]:
        raise HistoricalFuryPrefixCheckpointV1Error(
            "max_health and current_health must be distinct field records"
        )
    if row.get("blocking_fields") != blockers:
        raise HistoricalFuryPrefixCheckpointV1Error("blocking field accounting differs")
    if row.get("exact_checkpoint_ready") is not (not blockers):
        raise HistoricalFuryPrefixCheckpointV1Error("checkpoint readiness accounting differs")
    expected_coverage = {
        "exact_field_count": categories["EXACT"],
        "partial_field_count": categories["PARTIAL"],
        "missing_field_count": categories["MISSING"],
        "required_field_count": len(REQUIRED_FIELDS),
    }
    if row.get("coverage") != expected_coverage:
        raise HistoricalFuryPrefixCheckpointV1Error("field coverage accounting differs")
    prefix = _mapping(row.get("strict_prefix_input"), "strict_prefix_input")
    if prefix.get("future_suffix_used") is not False:
        raise HistoricalFuryPrefixCheckpointV1Error("checkpoint claims future suffix use")
    return row


def _load_strict_prefix_state_rows(
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    raw_manifest_path: Path,
    data_root: Path,
) -> tuple[
    dict[tuple[str, str], list[JSONMap]],
    dict[tuple[str, str], JSONMap],
    JSONMap,
]:
    raw_manifest, _raw_payload, raw_manifest_sha = core_state_source._load_source_manifest(
        raw_manifest_path.expanduser().resolve()
    )
    raw_root = core_state_source._resolve_raw_root(data_root)
    requirements: dict[tuple[str, str], tuple[int, int]] = {}
    for evidence_row in evidence_rows:
        identity = _mapping(evidence_row.get("source_identity"), "source_identity")
        key = (
            _text(identity.get("instance_id"), "instance_id"),
            _text(identity.get("encounter_id"), "encounter_id"),
        )
        decision = _first_decision(evidence_row)
        order = _array(decision.get("order_key"), "decision order_key")
        cutoff = (_integer(order[0], "cutoff timestamp"), _integer(order[1], "cutoff index"))
        previous = requirements.setdefault(key, cutoff)
        if previous != cutoff:
            raise HistoricalFuryPrefixCheckpointV1Error(
                "one source encounter has conflicting checkpoint cutoffs"
            )

    rows_by_key: dict[tuple[str, str], list[JSONMap]] = {
        key: [] for key in requirements
    }
    coverage_by_key: dict[tuple[str, str], JSONMap] = {
        key: {
            stream: {
                "status": "MISSING_SOURCE_INSTANCE",
                "strict_prefix_event_count": 0,
            }
            for stream in STATE_STREAMS
        }
        for key in requirements
    }
    instances = {
        _optional_identifier(instance.get("instance_id")): instance
        for instance in _array(raw_manifest.get("instances"), "raw manifest instances")
        if isinstance(instance, Mapping)
    }
    selected_instance_count = 0
    decoded_object_count = 0
    for instance_id in sorted({key[0] for key in requirements}):
        instance = instances.get(instance_id)
        if not isinstance(instance, Mapping):
            continue
        selected_instance_count += 1
        streams = _mapping(instance.get("streams"), f"{instance_id}.streams")
        encounter_keys = [key for key in requirements if key[0] == instance_id]
        for stream in STATE_STREAMS:
            wrapper = streams.get(stream)
            if not isinstance(wrapper, Mapping) or wrapper.get("status") != "AVAILABLE":
                for key in encounter_keys:
                    coverage_by_key[key][stream] = {
                        "status": "MISSING_STREAM_OBJECT",
                        "strict_prefix_event_count": 0,
                    }
                continue
            reference = _mapping(wrapper.get("object"), f"{instance_id}.{stream}.object")
            compressed, _ = core_state_source._read_object_reference(
                raw_root,
                reference,
                label=f"{instance_id}.{stream}",
            )
            decoded_object_count += 1
            frames = state_source.decode_event_stream(compressed, stream_type=stream)
            frame_by_encounter: dict[str, tuple[int, int, Sequence[Mapping[str, Any]]]] = {}
            for frame_index, frame in enumerate(frames):
                encounter_id = _text(frame.get("encounter_id"), "frame encounter_id")
                if encounter_id in frame_by_encounter:
                    raise HistoricalFuryPrefixCheckpointV1Error(
                        f"duplicate {stream} frame for encounter {encounter_id}"
                    )
                frame_by_encounter[encounter_id] = (
                    frame_index,
                    _integer(frame.get("first_timestamp_ms"), "first_timestamp_ms"),
                    _array(frame.get("messages"), "frame messages"),
                )
            for key in encounter_keys:
                frame = frame_by_encounter.get(key[1])
                if frame is None:
                    coverage_by_key[key][stream] = {
                        "status": "MISSING_ENCOUNTER_FRAME",
                        "strict_prefix_event_count": 0,
                        "source_object_sha256": reference.get("sha256"),
                    }
                    continue
                frame_index, origin, messages = frame
                prefix_count = 0
                for message_index, message in enumerate(messages):
                    event = _mapping(message.get("event"), "decoded state event")
                    meta = _mapping(event.get("meta"), "decoded EventMeta")
                    event_order = (
                        origin + _integer(meta.get("offset_ms"), "EventMeta.offset_ms"),
                        _integer(meta.get("event_index"), "EventMeta.event_index"),
                    )
                    if event_order >= requirements[key]:
                        continue
                    rows_by_key[key].append(
                        _normalized_state_row(
                            instance_id=instance_id,
                            encounter_id=key[1],
                            first_timestamp_ms=origin,
                            stream=stream,
                            frame_index=frame_index,
                            message_index=message_index,
                            message=message,
                            source_manifest_sha256=raw_manifest_sha,
                            source_object_sha256=_text(
                                reference.get("sha256"), "source object sha256"
                            ),
                        )
                    )
                    prefix_count += 1
                coverage_by_key[key][stream] = {
                    "status": "AVAILABLE_FRAME_VERIFIED",
                    "strict_prefix_event_count": prefix_count,
                    "source_object_sha256": reference.get("sha256"),
                }
    for rows in rows_by_key.values():
        rows.sort(key=_event_order)
    return (
        rows_by_key,
        coverage_by_key,
        {
            "schema": raw_manifest.get("schema"),
            "implementation_revision": raw_manifest.get("implementation_revision"),
            "parser_contract_revision": raw_manifest.get("parser_contract_revision"),
            "manifest_sha256": raw_manifest_sha,
            "selected_instance_count": selected_instance_count,
            "decoded_object_count": decoded_object_count,
            "selected_streams": list(STATE_STREAMS),
            "network_request_count": 0,
        },
    )


def _simulator_interface_audit() -> JSONMap:
    return {
        "audited_interface": "wowsims-turtle load_dynamic_v4 + RaidSimRequest",
        "raid_sim_request_fields": ["raid", "encounter", "sim_options"],
        "fresh_pull_only_fields": {
            "warrior.options.starting_rage": "AVAILABLE_NOT_MID_WINDOW_RESTORE",
            "warrior.options.stance": "AVAILABLE_NOT_MID_WINDOW_RESTORE",
            "warrior.options.stance_snapshot": "AVAILABLE_NOT_MID_WINDOW_RESTORE",
        },
        "dynamic_v4_target_health": {
            "wire_fields": [
                "target_index",
                "maximum_health",
                "current_health",
            ],
            "max_health_separate_from_current_health": True,
            "current_health_seed_supported": True,
            "missing_health_at_checkpoint_counted_as_window_damage": False,
            "source_evidence_still_required_for_both_health_fields": True,
        },
        "mid_window_player_state_seed": {
            "rage_current": False,
            "stance": False,
            "gcd_remaining_ms": False,
            "cooldowns_remaining_ms": False,
            "main_hand_swing_remaining_ms": False,
            "off_hand_swing_remaining_ms": False,
            "queued_next_swing": False,
            "self_auras_and_procs": False,
            "candidate_owned_existing_debuffs": False,
        },
        "canonical_snapshot_restore_supported": False,
        "exact_checkpoint_wire_supported": False,
        "audit_sources": [
            "wowsims-turtle/proto/api.proto:RaidSimRequest",
            "wowsims-turtle/proto/warrior.proto:Warrior.Options",
            "wowsims-turtle/sim/o2o/environment.go:Environment",
            "wowsims-turtle/sim/o2o/dynamic_target_semantics_v4.go:DynamicTargetSemanticsConfigV4",
            "o2o_dps/sim_bridge_dynamic_v4.py:DynamicTargetSemanticsConfigV4",
        ],
    }


def _coverage_summary(rows: Sequence[Mapping[str, Any]]) -> JSONMap:
    per_field: JSONMap = {}
    for name in REQUIRED_FIELDS:
        counts = Counter(
            _mapping(_mapping(row.get("fields"), "fields")[name], name).get(
                "observation_category"
            )
            for row in rows
        )
        per_field[name] = {
            "exact_observed_row_count": counts["EXACT"],
            "partial_evidence_row_count": counts["PARTIAL"],
            "missing_or_ambiguous_row_count": counts["MISSING"],
            "row_count": len(rows),
        }
    blocker_counts = Counter(
        blocker
        for row in rows
        for blocker in _array(row.get("blocking_fields"), "blocking_fields")
    )
    prefix_events: Counter[str] = Counter()
    available_frames: Counter[str] = Counter()
    for row in rows:
        prefix = _mapping(row.get("strict_prefix_input"), "strict_prefix_input")
        coverage = _mapping(prefix.get("stream_coverage"), "stream_coverage")
        for stream in STATE_STREAMS:
            stream_row = _mapping(coverage.get(stream), f"stream coverage {stream}")
            count = _integer(
                stream_row.get("strict_prefix_event_count"),
                f"{stream} strict_prefix_event_count",
            )
            prefix_events[stream] += count
            if stream_row.get("status") == "AVAILABLE_FRAME_VERIFIED":
                available_frames[stream] += 1
    return {
        "checkpoint_row_count": len(rows),
        "exact_checkpoint_ready_row_count": sum(
            bool(row.get("exact_checkpoint_ready")) for row in rows
        ),
        "blocked_checkpoint_row_count": sum(
            not bool(row.get("exact_checkpoint_ready")) for row in rows
        ),
        "coverage_by_field": per_field,
        "blocking_row_counts_by_field": dict(sorted(blocker_counts.items())),
        "strict_prefix_event_counts_by_stream": {
            stream: prefix_events[stream] for stream in STATE_STREAMS
        },
        "available_encounter_frame_counts_by_stream": {
            stream: available_frames[stream] for stream in STATE_STREAMS
        },
    }


def _manifest_without_address(
    *,
    rows: Sequence[Mapping[str, Any]],
    evidence_manifest: Mapping[str, Any],
    raw_input: Mapping[str, Any],
) -> JSONMap:
    evidence_address = _mapping(
        evidence_manifest.get("content_address"), "evidence content_address"
    )
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "input_closure": {
            "environment_evidence": {
                "schema": evidence_manifest.get("schema"),
                "implementation_revision": evidence_manifest.get(
                    "implementation_revision"
                ),
                "content_sha256": evidence_address.get("sha256"),
            },
            "chronicle_raw_state_streams": dict(raw_input),
        },
        "checkpoint_contract": {
            "cutoff": "STRICTLY_BEFORE_FIRST_BOUND_DECISION_ORDER_KEY",
            "required_fields": list(REQUIRED_FIELDS),
            "missing_field_policy": "FIELDWISE_FAIL_CLOSED_NULL_NO_DEFAULT_ZERO",
            "max_health_current_health_conflation_forbidden": True,
            "resource_delta_integration_forbidden_without_absolute_baseline": True,
            "future_suffix_materialization_forbidden": True,
        },
        "simulator_interface_audit": _simulator_interface_audit(),
        "rows": [deepcopy(dict(row)) for row in rows],
        "summary": _coverage_summary(rows),
        "execution_accounting": {
            "network_request_count": 0,
            "simulator_run_count": 0,
            "hpc_job_count": 0,
            "scientific_runs_started": 0,
        },
        "authorization": {
            "simulator_checkpoint_seed_authorized": False,
            "training_authorized": False,
            "comparison_authorized": False,
            "deployment_authorized": False,
            "superiority_claim_authorized": False,
        },
    }


def build_manifest(
    *,
    rows: Sequence[Mapping[str, Any]],
    evidence_manifest: Mapping[str, Any],
    raw_input: Mapping[str, Any],
) -> JSONMap:
    checked_rows = [validate_checkpoint_row(row) for row in rows]
    document = _manifest_without_address(
        rows=checked_rows,
        evidence_manifest=evidence_manifest,
        raw_input=raw_input,
    )
    document["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical_manifest_without_content_address",
        "sha256": hashlib.sha256(_canonical_bytes(document)).hexdigest(),
    }
    return validate_manifest(document)


def validate_manifest(value: Mapping[str, Any]) -> JSONMap:
    manifest = deepcopy(dict(_mapping(value, "checkpoint manifest")))
    if manifest.get("schema") != SCHEMA or manifest.get("status") != STATUS:
        raise HistoricalFuryPrefixCheckpointV1Error("checkpoint manifest schema/status mismatch")
    rows = [validate_checkpoint_row(row) for row in _array(manifest.get("rows"), "rows")]
    if manifest.get("summary") != _coverage_summary(rows):
        raise HistoricalFuryPrefixCheckpointV1Error(
            "checkpoint coverage/blocker summary differs from rows"
        )
    execution = _mapping(manifest.get("execution_accounting"), "execution_accounting")
    if any(
        execution.get(name) != 0
        for name in (
            "network_request_count",
            "simulator_run_count",
            "hpc_job_count",
            "scientific_runs_started",
        )
    ):
        raise HistoricalFuryPrefixCheckpointV1Error(
            "checkpoint compilation cannot claim network/simulator/HPC execution"
        )
    address = _mapping(manifest.pop("content_address", None), "content_address")
    digest = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    if (
        address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical_manifest_without_content_address"
        or address.get("sha256") != digest
    ):
        raise HistoricalFuryPrefixCheckpointV1Error("manifest content address differs")
    manifest["content_address"] = dict(address)
    return manifest


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def compile_manifest(
    *,
    evidence_manifest_path: Path = DEFAULT_EVIDENCE_MANIFEST,
    raw_manifest_path: Path = DEFAULT_RAW_MANIFEST,
    data_root: Path = DEFAULT_DATA_ROOT,
) -> JSONMap:
    evidence_manifest, evidence_rows = (
        load_historical_fury_source_bound_environment_evidence_v1(
            evidence_manifest_path,
            verify_partition=True,
        )
    )
    rows_by_key, coverage_by_key, raw_input = _load_strict_prefix_state_rows(
        evidence_rows,
        raw_manifest_path=raw_manifest_path,
        data_root=data_root,
    )
    rows: list[JSONMap] = []
    for evidence_row in sorted(evidence_rows, key=lambda row: str(row.get("segment_ref"))):
        identity = _mapping(evidence_row.get("source_identity"), "source_identity")
        key = (
            _text(identity.get("instance_id"), "instance_id"),
            _text(identity.get("encounter_id"), "encounter_id"),
        )
        rows.append(
            compile_checkpoint_row(
                evidence_row,
                state_rows=rows_by_key[key],
                stream_coverage=coverage_by_key[key],
            )
        )
    return build_manifest(
        rows=rows,
        evidence_manifest=evidence_manifest,
        raw_input=raw_input,
    )


def publish_manifest(manifest: Mapping[str, Any], output_directory: Path) -> tuple[Path, Path]:
    checked = validate_manifest(manifest)
    payload = _canonical_bytes(checked, newline=True)
    digest = _mapping(checked.get("content_address"), "content_address")["sha256"]
    stable = output_directory / "manifest.json"
    addressed = output_directory / f"historical_fury_source_bound_prefix_checkpoint_v1.{digest}.manifest.json"
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return stable, addressed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-manifest", type=Path, default=DEFAULT_EVIDENCE_MANIFEST)
    parser.add_argument("--raw-manifest", type=Path, default=DEFAULT_RAW_MANIFEST)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)
    manifest = compile_manifest(
        evidence_manifest_path=args.evidence_manifest,
        raw_manifest_path=args.raw_manifest,
        data_root=args.data_root,
    )
    stable, addressed = publish_manifest(manifest, args.output_directory)
    if args.stdout:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(stable)
        print(addressed)
        print(json.dumps(manifest["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
