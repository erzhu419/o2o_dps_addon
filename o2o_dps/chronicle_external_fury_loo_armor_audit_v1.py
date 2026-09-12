"""Audit focal-Fury leave-one-out hostile armor-aura trajectories.

This Stage-8 module is deliberately an audit, not a simulator adapter.  It
intersects the exact Fury GUID population and the Stage-5/Stage-6 wave/target
closure with Stage-7 physical-effect Aura lifecycles.  The emitted values are
observed aura amounts and anchors only.  They do not authorize an armor value,
policy input, comparison, training, or a simulator action.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import chronicle_external_aura_attribution_v1 as aura_v1
from . import chronicle_external_aura_attribution_v2 as aura_v2
from . import chronicle_external_reconstruction_admission_v1 as admission_v1
from . import chronicle_external_state_event_normalizer_v1 as state_v1
from . import chronicle_external_team_background_generator_v2 as background_v2
from . import chronicle_external_team_wave_model_v2 as wave_model_v2


SCHEMA = "chronicle_external_fury_loo_armor_audit/v1"
WAVE_AUDIT_SCHEMA = "chronicle_external_fury_loo_armor_wave_audit/v1"
STATUS = "STAGE8_FURY_LOO_ARMOR_EXECUTABILITY_AUDIT_NONVOTING"
IMPLEMENTATION_REVISION = "exact_guid_hostile_lifecycle_prefix_factual_loo_split_v2"

EXPECTED_INSTANCE_COUNT = 84
EXPECTED_OBSERVED_FURY_INSTANCE_COUNT = 46
EXPECTED_OBSERVED_FURY_INSTANCE_FOCAL_COUNT = 176

BONEREAVER_DEBUFF_ID = "bonereavers_edge"
BONEREAVER_SPELL_ID = aura_v1.BONEREAVER_SPELL_ID


class ChronicleExternalFuryLooArmorAuditV1Error(RuntimeError):
    """The exact Stage-8 audit closure cannot be established."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalFuryLooArmorAuditV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalFuryLooArmorAuditV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            f"{label} must be nonempty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalFuryLooArmorAuditV1Error(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            f"{label} must be at least {minimum}"
        )
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _guid(value: Any, label: str) -> str:
    try:
        return admission_v1.canonical_guid(value, label=label)
    except admission_v1.ChronicleExternalAdmissionError as error:
        raise ChronicleExternalFuryLooArmorAuditV1Error(str(error)) from error


def _optional_guid(value: Any, label: str) -> str | None:
    return None if value is None else _guid(value, label)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _anchor(value: Any, label: str) -> dict[str, Any]:
    raw = _mapping(value, label)
    result = {
        "timestamp_ms": _integer(raw.get("timestamp_ms"), f"{label}.timestamp_ms"),
        "event_index": _integer(raw.get("event_index"), f"{label}.event_index"),
    }
    for field in ("offset_ms", "stream_type", "frame_index", "frame_message_index"):
        if field in raw:
            result[field] = raw[field]
    return result


def _boundary_order(anchor: Mapping[str, Any]) -> tuple[int, int]:
    return (
        _integer(anchor.get("timestamp_ms"), "anchor.timestamp_ms"),
        _integer(anchor.get("event_index"), "anchor.event_index"),
    )


def _detail_order(anchor: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    stream = str(anchor.get("stream_type") or "")
    stream_order = {"aura_cast": 0, "aura": 1}.get(stream, 2)
    return (
        _integer(anchor.get("timestamp_ms"), "anchor.timestamp_ms"),
        _integer(anchor.get("event_index"), "anchor.event_index"),
        stream_order,
        int(anchor.get("frame_index") or 0),
        int(anchor.get("frame_message_index") or 0),
    )


def _in_closed_window(
    anchor: Mapping[str, Any],
    start: Mapping[str, Any],
    end: Mapping[str, Any],
) -> bool:
    order = _boundary_order(anchor)
    return _boundary_order(start) <= order <= _boundary_order(end)


def _before(anchor: Mapping[str, Any], boundary: Mapping[str, Any]) -> bool:
    return _boundary_order(anchor) < _boundary_order(boundary)


def _same_anchor(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _detail_order(left) == _detail_order(right)


def _wave_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": _text(value.get("instance_id"), "wave.instance_id"),
        "encounter_id": _text(value.get("encounter_id"), "wave.encounter_id"),
        "encounter_ordinal": _integer(
            value.get("encounter_ordinal"), "wave.encounter_ordinal", minimum=0
        ),
        "wave_id": _text(value.get("wave_id"), "wave.wave_id"),
        "wave_ordinal": _integer(
            value.get("wave_ordinal"), "wave.wave_ordinal", minimum=0
        ),
    }


def _wave_key(value: Mapping[str, Any]) -> tuple[str, str, int, str, int]:
    identity = _wave_identity(value)
    return (
        identity["instance_id"],
        identity["encounter_id"],
        identity["encounter_ordinal"],
        identity["wave_id"],
        identity["wave_ordinal"],
    )


def _event_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        _text(row.get("instance"), "event.instance"),
        _text(row.get("encounter"), "event.encounter"),
        _guid(row.get("target_guid"), "event.target_guid"),
        _text(row.get("debuff_id"), "event.debuff_id"),
        _integer(row.get("spell_id"), "event.spell_id"),
    )


def _is_bonereaver(row: Mapping[str, Any]) -> bool:
    return (
        row.get("debuff_id") == BONEREAVER_DEBUFF_ID
        or row.get("spell_id") == BONEREAVER_SPELL_ID
        or row.get("aura_role") == "BONEREAVER_PLAYER_SELF_BUFF_ARMOR_IGNORE"
    )


def _is_supported_hostile_contract(row: Mapping[str, Any]) -> bool:
    identity = (row.get("debuff_id"), row.get("spell_id"))
    return identity in aura_v2.HOSTILE_ARMOR_SPELL_EFFECT_CONTRACTS


def _state_transition(row: Mapping[str, Any]) -> dict[str, Any]:
    transition = _mapping(row.get("factual_transition"), "factual_transition")
    attribution = _mapping(row.get("attribution"), "attribution")
    anchor = _anchor(row.get("aura_anchor"), "aura_anchor")
    return {
        "anchor": anchor,
        "state": _text(row.get("aura_state"), "aura_state"),
        "prior_amount": _integer(
            transition.get("prior_amount"), "factual_transition.prior_amount", minimum=0
        ),
        "current_amount": _integer(
            transition.get("current_amount"),
            "factual_transition.current_amount",
            minimum=0,
        ),
        "stack_delta": _integer(
            transition.get("stack_delta"), "factual_transition.stack_delta"
        ),
        "attribution_status": _text(
            attribution.get("status"), "attribution.status"
        ),
        "source_guid": _optional_guid(
            attribution.get("source_guid"), "attribution.source_guid"
        ),
        "unique_physical_cast_supported": (
            transition.get("unique_physical_cast_supported") is True
        ),
    }


def _action_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    anchor = _anchor(row.get("anchor"), "cast action anchor")
    loo = _mapping(row.get("loo_counterfactual"), "cast action loo_counterfactual")
    delta = row.get("factual_stack_delta")
    if delta is not None:
        delta = _integer(delta, "cast action factual_stack_delta")
    return {
        "anchor": anchor,
        "source_guid": _optional_guid(row.get("source_guid"), "cast source_guid"),
        "factual_amount_before_cast": _integer(
            row.get("factual_amount_before_cast"),
            "cast action factual_amount_before_cast",
            minimum=0,
        ),
        "factual_stack_delta": delta,
        "factual_lifecycle_status": _text(
            row.get("factual_lifecycle_status"),
            "cast action factual_lifecycle_status",
        ),
        "factual_result_event_observed": (
            row.get("factual_result_event_observed") is True
        ),
        "action_status": _text(row.get("action_status"), "cast action status"),
        "counterfactual_nonexecutable": (
            loo.get("counterfactual_nonexecutable") is True
        ),
        "counterfactual_reason": _optional_text(loo.get("reason")),
        "matched_aura_state_anchor": (
            _anchor(row.get("matched_aura_state_anchor"), "matched aura anchor")
            if row.get("matched_aura_state_anchor") is not None
            else None
        ),
    }


def _new_lifecycle(
    *,
    key: tuple[str, str, str, str, int],
    ordinal: int,
    transition: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "key": key,
        "ordinal": ordinal,
        "start_anchor": deepcopy(transition["anchor"]),
        "close_anchor": None,
        "closed_by_state_removed": False,
        "right_censored": False,
        "unknown_predecessor_before_first_observation": False,
        "transitions": [],
        "exact_sources": Counter(),
        "unresolved_stack_count": 0,
        "factual_blockers": [],
        "actions": [],
    }


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _apply_transition(lifecycle: dict[str, Any], transition: Mapping[str, Any]) -> None:
    state = transition["state"]
    prior = transition["prior_amount"]
    current = transition["current_amount"]
    delta = transition["stack_delta"]
    if current - prior != delta:
        _append_once(lifecycle["factual_blockers"], "FACTUAL_STACK_DELTA_MISMATCH")
    lifecycle["transitions"].append(deepcopy(dict(transition)))

    if state == "StateAdded":
        if current < prior:
            _append_once(lifecycle["factual_blockers"], "NONMONOTONIC_STATE_ADDED")
            lifecycle["exact_sources"].clear()
            lifecycle["unresolved_stack_count"] = current
            return
        if delta > 0:
            if (
                transition["unique_physical_cast_supported"]
                and transition["attribution_status"]
                == "UNIQUE_PHYSICAL_EFFECT_CAST_ATTRIBUTED"
                and transition["source_guid"] is not None
            ):
                lifecycle["exact_sources"][transition["source_guid"]] += 1
                if delta > 1:
                    lifecycle["unresolved_stack_count"] += delta - 1
                    _append_once(
                        lifecycle["factual_blockers"],
                        "MULTISTACK_DELTA_HAS_UNRESOLVED_CONTRIBUTION",
                    )
            else:
                lifecycle["unresolved_stack_count"] += delta
                _append_once(
                    lifecycle["factual_blockers"], "UNRESOLVED_STACK_SOURCE"
                )
        elif delta == 0:
            # A factual refresh has no numerical transition.  Its action-side
            # evidence is handled separately and never manufactures a stack.
            pass
        else:
            _append_once(lifecycle["factual_blockers"], "NEGATIVE_STATE_ADDED_DELTA")
    elif state in {"StateModified", "StateUnknown"}:
        lifecycle["exact_sources"].clear()
        lifecycle["unresolved_stack_count"] = current
        _append_once(lifecycle["factual_blockers"], "UNATTRIBUTED_STATE_TRANSITION")
    elif state != "StateRemoved":
        _append_once(lifecycle["factual_blockers"], "UNSUPPORTED_AURA_STATE")


def _build_lifecycles(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[Any, ...], dict[str, Any]]]:
    by_key: dict[tuple[str, str, str, str, int], list[dict[str, Any]]] = defaultdict(
        list
    )
    for raw in rows:
        row = _mapping(raw, "attributed aura event")
        by_key[_event_key(row)].append(_state_transition(row))

    completed: list[dict[str, Any]] = []
    matched_anchor_to_lifecycle: dict[tuple[Any, ...], dict[str, Any]] = {}
    for key in sorted(by_key):
        transitions = sorted(by_key[key], key=lambda row: _detail_order(row["anchor"]))
        active: dict[str, Any] | None = None
        ordinal = 0
        prior_order: tuple[int, int, int, int, int] | None = None
        for transition in transitions:
            order = _detail_order(transition["anchor"])
            if prior_order is not None and order <= prior_order:
                raise ChronicleExternalFuryLooArmorAuditV1Error(
                    "attributed aura transitions are not in strict order"
                )
            prior_order = order
            state = transition["state"]
            current = transition["current_amount"]

            if active is None and state != "StateRemoved":
                ordinal += 1
                active = _new_lifecycle(key=key, ordinal=ordinal, transition=transition)
                if transition["prior_amount"] != 0:
                    _append_once(
                        active["factual_blockers"], "UNKNOWN_ACTIVE_STATE_AT_LIFECYCLE_START"
                    )
            if active is None:
                # A first observed removal closes an aura whose preceding
                # state/source is absent from this encounter stream.  It is a
                # valid reset after its anchor, but waves before that anchor
                # must not be called empty-zero.
                ordinal += 1
                active = _new_lifecycle(key=key, ordinal=ordinal, transition=transition)
                active["unknown_predecessor_before_first_observation"] = True
                _append_once(
                    active["factual_blockers"],
                    "ORPHAN_STATE_REMOVED_HAS_UNKNOWN_PREDECESSOR",
                )

            _apply_transition(active, transition)
            matched_anchor_to_lifecycle[_detail_order(transition["anchor"])] = active
            if state == "StateRemoved":
                if current != 0:
                    _append_once(
                        active["factual_blockers"], "STATE_REMOVED_NONZERO_AMOUNT"
                    )
                active["close_anchor"] = deepcopy(transition["anchor"])
                active["closed_by_state_removed"] = True
                completed.append(active)
                active = None
            elif current == 0:
                _append_once(
                    active["factual_blockers"], "ZERO_WITHOUT_STATE_REMOVED_CLOSURE"
                )

        if active is not None:
            active["right_censored"] = True
            _append_once(active["factual_blockers"], "RIGHT_CENSORED_WITHOUT_STATE_REMOVED")
            completed.append(active)
    return completed, matched_anchor_to_lifecycle


def _assign_actions(
    lifecycles: Sequence[dict[str, Any]],
    matched_anchor_to_lifecycle: Mapping[tuple[Any, ...], dict[str, Any]],
    actions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str, str, int], list[dict[str, Any]]] = defaultdict(
        list
    )
    for lifecycle in lifecycles:
        by_key[lifecycle["key"]].append(lifecycle)
    unassigned: list[dict[str, Any]] = []
    for raw in actions:
        row = _mapping(raw, "cast action")
        key = _event_key(row)
        action = _action_projection(row)
        selected: dict[str, Any] | None = None
        matched = action["matched_aura_state_anchor"]
        if matched is not None:
            selected = matched_anchor_to_lifecycle.get(_detail_order(matched))
            if selected is not None and selected["key"] != key:
                selected = None
        if selected is None and action["factual_amount_before_cast"] > 0:
            action_order = _boundary_order(action["anchor"])
            for lifecycle in by_key.get(key, []):
                start = _boundary_order(lifecycle["start_anchor"])
                close = (
                    _boundary_order(lifecycle["close_anchor"])
                    if lifecycle["close_anchor"] is not None
                    else None
                )
                if start <= action_order and (close is None or action_order <= close):
                    selected = lifecycle
                    break
        if selected is None:
            unassigned.append({"key": key, **action})
        else:
            selected["actions"].append(action)
    for lifecycle in lifecycles:
        lifecycle["actions"].sort(key=lambda row: _detail_order(row["anchor"]))
    return unassigned


def _lifecycle_intersects(
    lifecycle: Mapping[str, Any],
    start: Mapping[str, Any],
    end: Mapping[str, Any],
) -> bool:
    life_start = _boundary_order(_mapping(lifecycle["start_anchor"], "start anchor"))
    life_close = (
        _boundary_order(_mapping(lifecycle["close_anchor"], "close anchor"))
        if lifecycle.get("close_anchor") is not None
        else None
    )
    if lifecycle.get("unknown_predecessor_before_first_observation"):
        return life_close is None or _boundary_order(start) <= life_close
    return life_start <= _boundary_order(end) and (
        life_close is None or life_close >= _boundary_order(start)
    )


def _amount_at_wave_start(
    lifecycle: Mapping[str, Any], start: Mapping[str, Any]
) -> int | None:
    if lifecycle.get("unknown_predecessor_before_first_observation"):
        close = lifecycle.get("close_anchor")
        if close is None or _boundary_order(start) <= _boundary_order(close):
            return None
    amount = 0
    for transition in lifecycle["transitions"]:
        if not _before(transition["anchor"], start):
            break
        amount = transition["current_amount"]
    return amount


def _first_blocker_anchor(
    lifecycle: Mapping[str, Any],
    focal_guid: str,
    start: Mapping[str, Any],
    end: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    for transition in lifecycle["transitions"]:
        if (
            transition["source_guid"] == focal_guid
            or transition["source_guid"] is None
            and transition["stack_delta"] > 0
            or transition["state"] in {"StateModified", "StateUnknown"}
        ):
            candidates.append(transition["anchor"])
    for action in lifecycle["actions"]:
        if (
            action["source_guid"] in {None, focal_guid}
            and action["counterfactual_nonexecutable"]
            and action["factual_amount_before_cast"] > 0
        ):
            candidates.append(action["anchor"])
    within = [
        anchor for anchor in candidates if _in_closed_window(anchor, start, end)
    ]
    if not within:
        return None
    return deepcopy(min(within, key=_detail_order))


def _render_lifecycle(
    lifecycle: Mapping[str, Any],
    *,
    focal_guid: str,
    start: Mapping[str, Any],
    end: Mapping[str, Any],
) -> dict[str, Any]:
    key = lifecycle["key"]
    amount_at_start = _amount_at_wave_start(lifecycle, start)
    # LOO causality is prefix-bounded.  A focal stack or refresh first seen
    # after the wave cannot invalidate an earlier wave merely because both
    # observations belong to the same eventual factual lifecycle.
    relevant_transitions = [
        transition
        for transition in lifecycle["transitions"]
        if _boundary_order(transition["anchor"]) <= _boundary_order(end)
    ]
    exact_sources: Counter[str] = Counter()
    for transition in relevant_transitions:
        if (
            transition["state"] == "StateAdded"
            and transition["stack_delta"] > 0
            and transition["unique_physical_cast_supported"]
            and transition["attribution_status"]
            == "UNIQUE_PHYSICAL_EFFECT_CAST_ATTRIBUTED"
            and transition["source_guid"] is not None
        ):
            exact_sources[transition["source_guid"]] += 1
    complete_lifecycle_exact_sources = Counter(lifecycle["exact_sources"])
    blockers = list(lifecycle["factual_blockers"])
    if lifecycle["unresolved_stack_count"]:
        _append_once(blockers, "UNRESOLVED_STACK_SOURCE")
    if exact_sources.get(focal_guid, 0):
        _append_once(blockers, "FOCAL_PREDECESSOR_CONTRIBUTION")

    refreshes: list[dict[str, Any]] = []
    action_projection_executable = True
    relevant_actions = [
        action
        for action in lifecycle["actions"]
        if _boundary_order(action["anchor"]) <= _boundary_order(end)
    ]
    for action in relevant_actions:
        if action["factual_stack_delta"] == 0 or not action[
            "factual_result_event_observed"
        ]:
            refreshes.append(
                {
                    "anchor": deepcopy(action["anchor"]),
                    "source_guid": action["source_guid"],
                    "factual_amount_before_cast": action[
                        "factual_amount_before_cast"
                    ],
                    "factual_stack_delta": action["factual_stack_delta"],
                    "factual_lifecycle_status": action["factual_lifecycle_status"],
                    "counterfactual_nonexecutable": action[
                        "counterfactual_nonexecutable"
                    ],
                    "counterfactual_reason": action["counterfactual_reason"],
                }
            )
        if action["counterfactual_nonexecutable"]:
            action_projection_executable = False
            if (
                action["factual_amount_before_cast"] > 0
                and action["source_guid"] == focal_guid
            ):
                _append_once(
                    blockers, "FOCAL_ACTIVE_AURA_REFRESH_COUNTERFACTUAL_UNKNOWN"
                )
            elif (
                action["factual_amount_before_cast"] > 0
                and action["source_guid"] is None
            ):
                _append_once(
                    blockers, "UNKNOWN_CASTER_ACTIVE_AURA_REFRESH_COUNTERFACTUAL_UNKNOWN"
                )

    factual_supported = not lifecycle["factual_blockers"]
    state_trajectory_executable = not blockers
    blocker_anchor = _first_blocker_anchor(lifecycle, focal_guid, start, end)
    if blockers and blocker_anchor is None:
        # The blocking contribution predates the inclusive wave window (or is
        # a lifecycle-level closure blocker), so no prefix inside this wave is
        # executable.
        blocker_anchor = deepcopy(dict(start))
    in_window_transitions = [
        {
            "anchor": deepcopy(transition["anchor"]),
            "state": transition["state"],
            "prior_amount": transition["prior_amount"],
            "current_amount": transition["current_amount"],
            "stack_delta": transition["stack_delta"],
            "source_guid": transition["source_guid"],
            "attribution_status": transition["attribution_status"],
        }
        for transition in lifecycle["transitions"]
        if _in_closed_window(transition["anchor"], start, end)
    ]
    return {
        "target_guid": key[2],
        "debuff_id": key[3],
        "spell_id": key[4],
        "lifecycle_ordinal": lifecycle["ordinal"],
        "factual_lifecycle": {
            "start_anchor": deepcopy(lifecycle["start_anchor"]),
            "initial_amount_at_wave_start": amount_at_start,
            "transitions_inside_wave": in_window_transitions,
            "transition_count_inside_wave": len(in_window_transitions),
            "exact_source_counts": dict(sorted(exact_sources.items())),
            "exact_source_counts_through_wave_end": dict(
                sorted(exact_sources.items())
            ),
            "complete_lifecycle_exact_source_counts": dict(
                sorted(complete_lifecycle_exact_sources.items())
            ),
            "unresolved_stack_count": lifecycle["unresolved_stack_count"],
            "refresh_candidates": refreshes,
            "refresh_candidate_count": len(refreshes),
            "closed_by_state_removed": lifecycle["closed_by_state_removed"],
            "state_removed_closure_anchor": deepcopy(lifecycle["close_anchor"]),
            "state_removed_caster_inferred": False,
            "right_censored": lifecycle["right_censored"],
            "unknown_predecessor_before_first_observation": lifecycle[
                "unknown_predecessor_before_first_observation"
            ],
            "supported_by_exact_aura_lifecycle": factual_supported,
            "blocker_codes": list(lifecycle["factual_blockers"]),
        },
        "loo_counterfactual": {
            "focal_guid": focal_guid,
            "focal_stack_subtraction_used": False,
            "state_trajectory_executable": state_trajectory_executable,
            "action_projection_executable": (
                state_trajectory_executable and action_projection_executable
            ),
            "counterfactual_nonexecutable": not state_trajectory_executable,
            "blocker_codes": blockers,
            "executable_prefix_end_anchor_exclusive": blocker_anchor,
        },
    }


def audit_focal_wave_armor_lifecycles(
    *,
    focal_guid: str,
    wave: Mapping[str, Any],
    first_anchor: Mapping[str, Any],
    last_context_anchor: Mapping[str, Any],
    target_registry: Sequence[Mapping[str, Any]],
    attributed_state_events: Sequence[Mapping[str, Any]],
    cast_actions: Sequence[Mapping[str, Any]],
    encounter_scan_complete: bool,
) -> dict[str, Any]:
    """Audit one exact focal GUID against one complete historical wave.

    ``attributed_state_events`` and ``cast_actions`` must cover the complete
    encounter, not merely the wave.  This is what makes active state at the
    wave boundary and explicit StateRemoved reset auditable.
    """

    focal = _guid(focal_guid, "focal_guid")
    identity = _wave_identity(_mapping(wave, "wave"))
    start = _anchor(first_anchor, "first_anchor")
    end = _anchor(last_context_anchor, "last_context_anchor")
    if _boundary_order(end) < _boundary_order(start):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "wave last_context_anchor precedes first_anchor"
        )

    targets: list[dict[str, Any]] = []
    target_guids: set[str] = set()
    for expected_index, raw in enumerate(target_registry):
        target = _mapping(raw, "target_registry row")
        target_index = _integer(target.get("target_index"), "target_index", minimum=0)
        target_guid = _guid(target.get("target_guid"), "target_guid")
        if (
            target_index != expected_index
            or target_guid in target_guids
            or target.get("lane") != "HOSTILE_CREATURE"
        ):
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                "target_registry is not a strict HOSTILE_CREATURE registry"
            )
        target_guids.add(target_guid)
        targets.append({"target_index": target_index, "target_guid": target_guid})

    encounter_events: list[Mapping[str, Any]] = []
    bonereaver_inside = 0
    nonhostile_inside = 0
    unsupported_inside = 0
    unsupported_encounter = 0
    for raw in attributed_state_events:
        row = _mapping(raw, "attributed_state_event")
        if row.get("schema") != aura_v2.EVENT_SCHEMA or row.get("status") != aura_v2.STATUS:
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                "attributed state event is not the supported Stage-7 v2 record"
            )
        if row.get("instance") != identity["instance_id"] or row.get(
            "encounter"
        ) != identity["encounter_id"]:
            continue
        row_anchor = _mapping(row.get("aura_anchor"), "aura_anchor")
        inside = _in_closed_window(row_anchor, start, end)
        if _is_bonereaver(row):
            if inside:
                bonereaver_inside += 1
            continue
        event_target = _guid(row.get("target_guid"), "event.target_guid")
        if not _is_supported_hostile_contract(row):
            if (
                event_target in target_guids
                and _boundary_order(row_anchor) <= _boundary_order(end)
            ):
                unsupported_encounter += 1
            if inside:
                unsupported_inside += 1
            continue
        if event_target not in target_guids:
            if inside:
                nonhostile_inside += 1
            continue
        if row.get("is_buff") is True:
            if _boundary_order(row_anchor) <= _boundary_order(end):
                unsupported_encounter += 1
            if inside:
                unsupported_inside += 1
            continue
        encounter_events.append(row)

    encounter_actions: list[Mapping[str, Any]] = []
    for raw in cast_actions:
        action = _mapping(raw, "cast_action")
        if (
            action.get("instance") == identity["instance_id"]
            and action.get("encounter") == identity["encounter_id"]
            and _guid(action.get("target_guid"), "cast target_guid") in target_guids
            and _is_supported_hostile_contract(action)
        ):
            encounter_actions.append(action)
    lifecycles, matched = _build_lifecycles(encounter_events)
    unassigned_actions = _assign_actions(lifecycles, matched, encounter_actions)
    intersecting = [
        lifecycle
        for lifecycle in lifecycles
        if _lifecycle_intersects(lifecycle, start, end)
    ]
    rendered = [
        _render_lifecycle(
            lifecycle,
            focal_guid=focal,
            start=start,
            end=end,
        )
        for lifecycle in intersecting
    ]
    rendered.sort(
        key=lambda row: (
            row["target_guid"],
            row["debuff_id"],
            row["spell_id"],
            row["lifecycle_ordinal"],
        )
    )

    state_executable = all(
        row["loo_counterfactual"]["state_trajectory_executable"] for row in rendered
    )
    zero_blockers: list[str] = []
    if not encounter_scan_complete:
        zero_blockers.append("ENCOUNTER_AURA_SCAN_INCOMPLETE")
    if unsupported_encounter:
        zero_blockers.append(
            "UNSUPPORTED_HOSTILE_ARMOR_AURA_IN_ENCOUNTER_MAY_CROSS_WAVE"
        )
    relevant_unassigned_active_cast_count = sum(
        action["factual_amount_before_cast"] > 0
        and _boundary_order(action["anchor"]) <= _boundary_order(end)
        for action in unassigned_actions
    )
    if relevant_unassigned_active_cast_count:
        zero_blockers.append("UNASSIGNED_ACTIVE_AURA_CAST_PREVENTS_ZERO_PROOF")
    empty_zero_proven = (
        encounter_scan_complete
        and not rendered
        and not unsupported_encounter
        and not relevant_unassigned_active_cast_count
    )
    if zero_blockers or (not rendered and not empty_zero_proven):
        state_executable = False

    blocker_counts: Counter[str] = Counter(zero_blockers)
    for row in rendered:
        blocker_counts.update(row["loo_counterfactual"]["blocker_codes"])
    factual_transition_count = sum(
        row["factual_lifecycle"]["transition_count_inside_wave"] for row in rendered
    )
    refresh_count = sum(
        row["factual_lifecycle"]["refresh_candidate_count"] for row in rendered
    )
    action_projection_executable = state_executable and all(
        row["loo_counterfactual"]["action_projection_executable"]
        for row in rendered
    )
    return {
        "schema": WAVE_AUDIT_SCHEMA,
        "status": STATUS,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "wave": identity,
        "focal_guid": focal,
        "window": {
            "first_anchor": start,
            "last_context_anchor": end,
            "inclusive_eventmeta_boundary": True,
        },
        "hostile_target_registry": targets,
        "hostile_target_count": len(targets),
        "factual_lifecycles": rendered,
        "summary": {
            "intersecting_lifecycle_count": len(rendered),
            "factual_transition_count_inside_wave": factual_transition_count,
            "factual_refresh_candidate_count": refresh_count,
            "unassigned_cast_action_count": len(unassigned_actions),
            "unassigned_active_cast_count_through_wave_end": (
                relevant_unassigned_active_cast_count
            ),
            "bonereaver_self_ignore_event_count_excluded": bonereaver_inside,
            "nonhostile_or_unregistered_target_event_count_excluded": nonhostile_inside,
            "unsupported_hostile_armor_event_count": unsupported_inside,
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "empty_zero_proof": {
            "status": "EMPTY_ZERO_PROVEN" if empty_zero_proven else "NOT_EMPTY_OR_NOT_PROVEN",
            "proven": empty_zero_proven,
            "encounter_scan_complete": encounter_scan_complete,
            "wave_only_absence_used_as_zero_proof": False,
            "proof_basis": "COMPLETE_ENCOUNTER_LIFECYCLE_REPLAY",
            "complete_encounter_supported_target_state_event_count": len(
                encounter_events
            ),
            "unsupported_target_event_count_in_complete_encounter": (
                unsupported_encounter
            ),
            "unsupported_target_event_count_through_wave_end": (
                unsupported_encounter
            ),
            "unassigned_active_aura_cast_count": (
                relevant_unassigned_active_cast_count
            ),
            "blocker_codes": zero_blockers,
        },
        "loo_counterfactual": {
            "state_trajectory_executable": state_executable,
            "action_projection_executable": action_projection_executable,
            "counterfactual_nonexecutable": not state_executable,
            "focal_stack_subtraction_used": False,
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "claim_boundary": {
            "policy_input_authorized": False,
            "comparison_input_authorized": False,
            "training_authorized": False,
            "dynamic_armor_projection_authorized": False,
            "numeric_armor_value_authorized": False,
            "simulator_action_authorized": False,
            "bonereaver_projected_as_hostile_reduction": False,
        },
    }


def _load_manifest_shell(
    path_value: str | Path,
    *,
    module: Any,
    expected_schema: str,
    expected_kind: str,
    expected_revision: str,
    expected_status: str,
    addressed_prefix: str,
) -> tuple[dict[str, Any], Path, Path, str]:
    requested = Path(path_value).expanduser().resolve()
    manifest = module._load_json(requested, label="manifest")
    if (
        manifest.get("schema") != expected_schema
        or manifest.get("kind") != expected_kind
        or manifest.get("implementation_revision") != expected_revision
        or manifest.get("status") != expected_status
    ):
        raise ChronicleExternalFuryLooArmorAuditV1Error("unsupported manifest shell")
    content_sha = module._verify_content_address(manifest, label="manifest")
    payload = module._canonical_bytes(manifest) + b"\n"
    if requested.read_bytes() != payload:
        raise ChronicleExternalFuryLooArmorAuditV1Error("manifest is not canonical JSON")
    stable = requested.parent / "manifest.json"
    addressed = requested.parent / f"{addressed_prefix}.{content_sha}.manifest.json"
    for candidate in (stable, addressed):
        if not candidate.is_file() or candidate.is_symlink() or candidate.read_bytes() != payload:
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                "stable/addressed manifest twin is missing or differs"
            )
    root = module._data_root(stable)
    return manifest, stable, root, content_sha


def _manifest_entries(
    manifest: Mapping[str, Any], *, module: Any, label: str
) -> dict[str, Mapping[str, Any]]:
    entries = [_mapping(raw, f"{label} instance") for raw in _array(manifest.get("instances"), f"{label} instances")]
    order = _array(manifest.get("instance_order"), f"{label} instance_order")
    if len(entries) != len(order):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            f"{label} instance/order counts differ"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(entries):
        module._verify_content_address(entry, label=f"{label} instance entry")
        instance_id = _text(entry.get("instance_id"), f"{label} instance_id")
        if order[index] != instance_id or instance_id in result:
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                f"{label} instance order/set is not exact"
            )
        result[instance_id] = entry
    return result


def _partition_path(
    *, stable: Path, root: Path, entry: Mapping[str, Any], module: Any, label: str
) -> tuple[Path, Mapping[str, Any]]:
    partition = _mapping(entry.get("partition"), f"{label} partition")
    path = module._resolve_relative(
        stable.parent,
        partition.get("path"),
        root,
        label=f"{label} partition path",
    )
    if (
        path.stat().st_size
        != _integer(
            partition.get("compressed_size_bytes"),
            f"{label} compressed_size_bytes",
            minimum=0,
        )
        or module._sha256_file(path) != partition.get("compressed_file_sha256")
    ):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            f"{label} partition size/hash mismatch"
        )
    return path, partition


def _read_stage5_waves(
    *, stable: Path, root: Path, entry: Mapping[str, Any]
) -> dict[tuple[str, str, int, str, int], dict[str, Any]]:
    path, partition = _partition_path(
        stable=stable,
        root=root,
        entry=entry,
        module=wave_model_v2,
        label="Stage5",
    )
    contamination = _mapping(entry.get("contamination_lane"), "Stage5 contamination")
    rows: dict[tuple[str, str, int, str, int], dict[str, Any]] = {}
    logical = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            logical.update(raw_line)
            value = json.loads(raw_line.decode("utf-8"))
            row = dict(_mapping(value, f"Stage5 row {line_number}"))
            if raw_line != wave_model_v2._canonical_bytes(row) + b"\n":
                raise ChronicleExternalFuryLooArmorAuditV1Error(
                    "Stage5 partition row is not canonical JSONL"
                )
            wave_model_v2._validate_model_wave(
                row,
                instance_id=_text(entry.get("instance_id"), "Stage5 instance_id"),
                expected_contamination=contamination,
            )
            key = _wave_key(_mapping(row.get("wave"), "Stage5 wave"))
            if key in rows:
                raise ChronicleExternalFuryLooArmorAuditV1Error("duplicate Stage5 wave")
            rows[key] = row
    if (
        len(rows) != partition.get("record_count")
        or logical.hexdigest() != partition.get("logical_content_sha256")
    ):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "Stage5 partition logical identity/count mismatch"
        )
    return rows


def _read_stage6_blocks(
    *, stable: Path, root: Path, entry: Mapping[str, Any]
) -> dict[tuple[str, str, int, str, int], dict[str, Any]]:
    path, partition = _partition_path(
        stable=stable,
        root=root,
        entry=entry,
        module=background_v2,
        label="Stage6",
    )
    rows: dict[tuple[str, str, int, str, int], dict[str, Any]] = {}
    logical = hashlib.sha256()
    instance_id = _text(entry.get("instance_id"), "Stage6 instance_id")
    with gzip.open(path, "rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            logical.update(raw_line)
            value = json.loads(raw_line.decode("utf-8"))
            row = dict(_mapping(value, f"Stage6 row {line_number}"))
            if raw_line != background_v2._canonical_bytes(row) + b"\n":
                raise ChronicleExternalFuryLooArmorAuditV1Error(
                    "Stage6 partition row is not canonical JSONL"
                )
            background_v2._validate_block(row, expected_instance_id=instance_id)
            key = _wave_key(_mapping(row.get("wave"), "Stage6 wave"))
            if key in rows:
                raise ChronicleExternalFuryLooArmorAuditV1Error("duplicate Stage6 wave")
            rows[key] = row
    if (
        len(rows) != partition.get("record_count")
        or logical.hexdigest() != partition.get("logical_content_sha256")
    ):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "Stage6 partition logical identity/count mismatch"
        )
    return rows


def _fury_focals(instance: Mapping[str, Any]) -> list[dict[str, Any]]:
    instance_id = _text(instance.get("instance_id"), "raw instance_id")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _array(instance.get("warrior_observations"), "warrior_observations"):
        row = _mapping(raw, "warrior observation")
        if row.get("player_spec") != "Fury" or row.get("spec_evidence_status") != "OBSERVED":
            continue
        guid = _guid(row.get("player_guid"), "observed Fury player_guid")
        if guid in seen:
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                f"duplicate observed Fury GUID in {instance_id}"
            )
        seen.add(guid)
        result.append(
            {
                "instance_id": instance_id,
                "focal_guid": guid,
                "player_spec": "Fury",
                "spec_evidence_status": "OBSERVED",
                "contamination_label": row.get("contamination_label"),
            }
        )
    return sorted(result, key=lambda row: row["focal_guid"])


def _assert_stage5_focal(wave: Mapping[str, Any], focal_guid: str) -> None:
    matched = []
    for raw in _array(wave.get("players"), "Stage5 players"):
        player = _mapping(raw, "Stage5 player")
        metadata = _mapping(player.get("player"), "Stage5 player metadata")
        if _guid(metadata.get("guid"), "Stage5 player GUID") == focal_guid:
            matched.append(player)
    if len(matched) != 1:
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "raw observed Fury focal is not an exact unique Stage5 player"
        )
    lane = _mapping(matched[0].get("warrior_spec_lane"), "Stage5 warrior_spec_lane")
    if (
        lane.get("partition_key") != "WARRIOR_FURY"
        or lane.get("observed_spec") != "Fury"
        or lane.get("evidence_status") != "OBSERVED"
        or lane.get("exact_guid_match") is not True
        or lane.get("fury_or_arms_conflict_free_observation") is not True
    ):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "raw observed Fury focal differs from the exact Stage5 Fury lane"
        )


def scan_manifest_shard(
    *,
    raw_manifest_path: str | Path,
    raw_root: str | Path,
    stage5_manifest_path: str | Path,
    stage6_manifest_path: str | Path,
    shard_index: int = 0,
    shard_count: int = 1,
) -> dict[str, Any]:
    """Scan one deterministic instance shard without copying raw objects."""

    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ChronicleExternalFuryLooArmorAuditV1Error("invalid shard index/count")
    raw_path = Path(raw_manifest_path).expanduser().resolve()
    raw_manifest, _raw_bytes, raw_sha = state_v1._load_source_manifest(raw_path)
    resolved_raw_root = Path(raw_root).expanduser().resolve()

    stage5, stage5_stable, stage5_root, stage5_sha = _load_manifest_shell(
        stage5_manifest_path,
        module=wave_model_v2,
        expected_schema=wave_model_v2.SCHEMA,
        expected_kind=wave_model_v2.KIND,
        expected_revision=wave_model_v2.IMPLEMENTATION_REVISION,
        expected_status=wave_model_v2.STATUS,
        addressed_prefix="chronicle_external_team_wave_model_v2",
    )
    stage6, stage6_stable, stage6_root, stage6_sha = _load_manifest_shell(
        stage6_manifest_path,
        module=background_v2,
        expected_schema=background_v2.SCHEMA,
        expected_kind=background_v2.KIND,
        expected_revision=background_v2.IMPLEMENTATION_REVISION,
        expected_status=background_v2.STATUS,
        addressed_prefix="chronicle_external_team_background_generator_v2",
    )
    stage6_binding = _mapping(
        _mapping(stage6.get("input_closure"), "Stage6 input_closure").get(
            "team_model_manifest"
        ),
        "Stage6 team_model_manifest binding",
    )
    if stage6_binding.get("content_sha256") != stage5_sha:
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "Stage6 is not bound to the supplied Stage5 manifest"
        )

    raw_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in _array(raw_manifest.get("instances"), "raw instances"):
        instance = _mapping(raw, "raw instance")
        instance_id = _text(instance.get("instance_id"), "raw instance_id")
        if instance_id in raw_by_id:
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                f"duplicate raw instance {instance_id}"
            )
        raw_by_id[instance_id] = instance
    stage5_by_id = _manifest_entries(stage5, module=wave_model_v2, label="Stage5")
    stage6_by_id = _manifest_entries(stage6, module=background_v2, label="Stage6")
    if set(raw_by_id) != set(stage5_by_id) or set(raw_by_id) != set(stage6_by_id):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "raw/Stage5/Stage6 instance sets differ"
        )

    all_focals = [
        focal
        for instance_id in sorted(raw_by_id)
        for focal in _fury_focals(raw_by_id[instance_id])
    ]
    all_focal_instances = {row["instance_id"] for row in all_focals}
    if (
        len(raw_by_id) != EXPECTED_INSTANCE_COUNT
        or len(all_focal_instances) != EXPECTED_OBSERVED_FURY_INSTANCE_COUNT
        or len(all_focals) != EXPECTED_OBSERVED_FURY_INSTANCE_FOCAL_COUNT
    ):
        raise ChronicleExternalFuryLooArmorAuditV1Error(
            "raw exact-GUID Fury population differs from the frozen "
            "84-instance / 46-instance / 176-instance-focal closure"
        )

    selected_ids = sorted(raw_by_id)[shard_index::shard_count]
    wave_audits: list[dict[str, Any]] = []
    focal_rows: list[dict[str, Any]] = []
    aggregate = Counter()
    for instance_id in selected_ids:
        raw_instance = raw_by_id[instance_id]
        focals = _fury_focals(raw_instance)
        focal_rows.extend(focals)
        stage5_waves = _read_stage5_waves(
            stable=stage5_stable,
            root=stage5_root,
            entry=stage5_by_id[instance_id],
        )
        stage6_blocks = _read_stage6_blocks(
            stable=stage6_stable,
            root=stage6_root,
            entry=stage6_by_id[instance_id],
        )
        if set(stage5_waves) != set(stage6_blocks):
            raise ChronicleExternalFuryLooArmorAuditV1Error(
                f"Stage5/Stage6 wave sets differ for {instance_id}"
            )
        for key, block in stage6_blocks.items():
            model_wave = stage5_waves[key]
            source_model = _mapping(block.get("source_model"), "Stage6 source_model")
            if source_model.get("wave_content_sha256") != _mapping(
                model_wave.get("content_address"), "Stage5 wave content_address"
            ).get("sha256"):
                raise ChronicleExternalFuryLooArmorAuditV1Error(
                    "Stage6 block is not bound to its exact Stage5 wave"
                )

        rows = aura_v1._raw_rows(raw_instance, raw_root=resolved_raw_root)
        state_events, attribution_summary = aura_v2.attribute_state_rows(
            rows, include_cast_actions=True
        )
        cast_actions = _array(
            attribution_summary.get("cast_actions"), "Stage7 cast_actions"
        )
        events_by_encounter: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        actions_by_encounter: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for event in state_events:
            events_by_encounter[_text(event.get("encounter"), "event encounter")].append(event)
        for action in cast_actions:
            actions_by_encounter[_text(action.get("encounter"), "action encounter")].append(action)

        for key in sorted(stage5_waves, key=lambda item: (item[2], item[4], item[3])):
            model_wave = stage5_waves[key]
            block = stage6_blocks[key]
            descriptive = _mapping(model_wave.get("descriptive_outcome"), "descriptive_outcome")
            reconstruction = _mapping(
                descriptive.get("reconstruction_binding"), "reconstruction_binding"
            )
            window = _mapping(reconstruction.get("window"), "reconstruction window")
            identity = _mapping(model_wave.get("wave"), "Stage5 wave identity")
            encounter_id = _text(identity.get("encounter_id"), "encounter_id")
            for focal in focals:
                _assert_stage5_focal(model_wave, focal["focal_guid"])
                stage6_roster = {
                    _guid(value, "Stage6 roster player_guid")
                    for value in _array(
                        block.get("roster_player_guids"),
                        "Stage6 roster_player_guids",
                    )
                }
                if focal["focal_guid"] not in stage6_roster:
                    raise ChronicleExternalFuryLooArmorAuditV1Error(
                        "exact Fury focal is absent from Stage6 roster"
                    )
                audit = audit_focal_wave_armor_lifecycles(
                    focal_guid=focal["focal_guid"],
                    wave=identity,
                    first_anchor=_mapping(window.get("first_anchor"), "first_anchor"),
                    last_context_anchor=_mapping(
                        window.get("last_context_anchor"), "last_context_anchor"
                    ),
                    target_registry=[
                        _mapping(raw, "target_registry row")
                        for raw in _array(block.get("target_registry"), "target_registry")
                    ],
                    attributed_state_events=events_by_encounter.get(encounter_id, []),
                    cast_actions=actions_by_encounter.get(encounter_id, []),
                    encounter_scan_complete=True,
                )
                wave_audits.append(audit)
                aggregate["focal_wave_count"] += 1
                aggregate["state_trajectory_executable_count"] += bool(
                    audit["loo_counterfactual"]["state_trajectory_executable"]
                )
                aggregate["empty_zero_proven_count"] += bool(
                    audit["empty_zero_proof"]["proven"]
                )
                aggregate["intersecting_lifecycle_count"] += audit["summary"][
                    "intersecting_lifecycle_count"
                ]
                aggregate["factual_transition_count_inside_wave"] += audit[
                    "summary"
                ]["factual_transition_count_inside_wave"]
                for blocker, count in audit["summary"]["blocker_counts"].items():
                    aggregate[f"blocker::{blocker}"] += count

    distinct_focal_instances = {row["instance_id"] for row in focal_rows}
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "input_bindings": {
            "raw_manifest_sha256": raw_sha,
            "stage5_manifest_content_sha256": stage5_sha,
            "stage6_manifest_content_sha256": stage6_sha,
            "stage7_schema": aura_v2.SCHEMA,
            "stage7_implementation_revision": aura_v2.IMPLEMENTATION_REVISION,
            "physical_armor_effect_tuple": list(aura_v2.PHYSICAL_ARMOR_EFFECT_TUPLE),
        },
        "shard": {
            "index": shard_index,
            "count": shard_count,
            "instance_ids": selected_ids,
            "instance_count": len(selected_ids),
        },
        "population": {
            "global_instance_count": len(raw_by_id),
            "global_observed_fury_instance_count": len(all_focal_instances),
            "global_observed_fury_instance_focal_count": len(all_focals),
            "global_exact_population_verified": True,
            "shard_observed_fury_instance_count": len(distinct_focal_instances),
            "shard_observed_fury_instance_focal_count": len(focal_rows),
            "shard_focals": focal_rows,
        },
        "summary": dict(sorted(aggregate.items())),
        "focal_wave_audits": wave_audits,
        "claim_boundary": {
            "policy_input_authorized": False,
            "comparison_input_authorized": False,
            "training_authorized": False,
            "dynamic_armor_projection_authorized": False,
            "numeric_armor_value_authorized": False,
            "simulator_action_authorized": False,
            "magnitude_hypothesis_applied": False,
            "bonereaver_projected_as_hostile_reduction": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser(
        "scan-manifest-shard",
        help="audit one deterministic raw/Stage5/Stage6 instance shard",
    )
    scan.add_argument("--raw-manifest", required=True)
    scan.add_argument(
        "--raw-root",
        required=True,
        help="exact offline_data/chronicle_raw/external_api/v1 directory",
    )
    scan.add_argument("--stage5-manifest", required=True)
    scan.add_argument("--stage6-manifest", required=True)
    scan.add_argument("--shard-index", type=int, default=0)
    scan.add_argument("--shard-count", type=int, default=1)
    scan.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "scan-manifest-shard":  # pragma: no cover
        raise ChronicleExternalFuryLooArmorAuditV1Error("unsupported command")
    result = scan_manifest_shard(
        raw_manifest_path=args.raw_manifest,
        raw_root=args.raw_root,
        stage5_manifest_path=args.stage5_manifest,
        stage6_manifest_path=args.stage6_manifest,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )
    payload = _canonical_bytes(result) + b"\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    else:
        print(payload.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EXPECTED_INSTANCE_COUNT",
    "EXPECTED_OBSERVED_FURY_INSTANCE_COUNT",
    "EXPECTED_OBSERVED_FURY_INSTANCE_FOCAL_COUNT",
    "IMPLEMENTATION_REVISION",
    "SCHEMA",
    "STATUS",
    "WAVE_AUDIT_SCHEMA",
    "ChronicleExternalFuryLooArmorAuditV1Error",
    "audit_focal_wave_armor_lifecycles",
    "scan_manifest_shard",
]
