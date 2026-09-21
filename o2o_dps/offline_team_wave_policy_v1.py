"""Adapt a Chronicle external team-wave record into one executable mode.

Stage-5 team-wave rows use ``current_event_label`` rather than the compact
historical episode adapter's ``observed_event``.  This module performs that
schema boundary explicitly and then delegates policy construction to
``offline_wave_policy_v1``.  It does not pool players or waves: one call binds
one exact player, one complete wave, and one exact build segment.

The source contains server START times.  It does not reveal the player's
client request time, next-swing queue intent, or intentional idle decisions.
Those missing channels remain missing in the resulting policy contract.
"""

from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from .expert_policy import SwingQueueOp
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .offline_wave_policy_v1 import (
    KNOWN_BUILD_MAPPING_SCHEMA,
    KNOWN_WAVE_RECORD_SCHEMA,
    OfflineWaveFeedbackPolicyV1,
    OfflineWavePolicyV1Error,
    compile_offline_wave_feedback_policy_v1,
)


JSONMap = dict[str, Any]
KNOWN_TEAM_WAVE_SCHEMA_V2 = "chronicle_external_team_wave_model_wave/v2"
BOUNDARY_METADATA_SCHEMA_V1 = "offline_team_wave_boundary_metadata/v1"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfflineWavePolicyV1Error(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OfflineWavePolicyV1Error(f"{label} must be an array")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfflineWavePolicyV1Error(f"{label} must be nonempty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfflineWavePolicyV1Error(
            f"{label} must be a nonnegative integer"
        )
    return value


_ACTION_KEY_BY_SPELL_ID = {
    ref.spell_id: key
    for key, ref in ACTION_KEY_TO_REF.items()
    if ref.spell_id > 0
}
_ACTION_KEY_BY_SPELL_ID.update(
    {
        QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE].spell_id: (
            "warrior.heroic_strike"
        ),
        QUEUE_REFS[SwingQueueOp.CLEAVE].spell_id: "warrior.cleave",
    }
)


def _wave_identity(team_wave: Mapping[str, Any]) -> Mapping[str, Any]:
    if team_wave.get("schema") != KNOWN_TEAM_WAVE_SCHEMA_V2:
        raise OfflineWavePolicyV1Error(
            f"unsupported team-wave schema {team_wave.get('schema')!r}"
        )
    return _mapping(team_wave.get("wave"), "team_wave.wave")


def offline_team_wave_boundary_metadata_v1(
    team_wave: Mapping[str, Any],
) -> JSONMap:
    """Return the exact wave boundary retained by this policy adapter."""

    wave = _wave_identity(team_wave)
    outcome = _mapping(
        team_wave.get("descriptive_outcome"),
        "team_wave.descriptive_outcome",
    )
    reconstruction = _mapping(
        outcome.get("reconstruction_binding"),
        "descriptive_outcome.reconstruction_binding",
    )
    window = _mapping(
        reconstruction.get("window"),
        "descriptive_outcome.reconstruction_binding.window",
    )
    duration = _nonnegative_int(
        window.get("boundary_duration_ms"), "window.boundary_duration_ms"
    )
    if duration <= 0:
        raise OfflineWavePolicyV1Error(
            "team wave boundary duration must be positive"
        )
    first = _mapping(window.get("first_anchor"), "window.first_anchor")
    last = _mapping(
        window.get("last_boundary_anchor"), "window.last_boundary_anchor"
    )
    first_offset = _nonnegative_int(
        first.get("offset_ms"), "window.first_anchor.offset_ms"
    )
    last_offset = _nonnegative_int(
        last.get("offset_ms"), "window.last_boundary_anchor.offset_ms"
    )
    if first_offset != 0 or last_offset != duration:
        raise OfflineWavePolicyV1Error(
            "team wave anchors do not span the complete boundary"
        )
    return {
        "schema": BOUNDARY_METADATA_SCHEMA_V1,
        "instance_id": _nonempty(wave.get("instance_id"), "wave.instance_id"),
        "encounter_id": _nonempty(
            wave.get("encounter_id"), "wave.encounter_id"
        ),
        "encounter_ordinal": _nonnegative_int(
            wave.get("encounter_ordinal"), "wave.encounter_ordinal"
        ),
        "wave_id": _nonempty(wave.get("wave_id"), "wave.wave_id"),
        "wave_ordinal": _nonnegative_int(
            wave.get("wave_ordinal"), "wave.wave_ordinal"
        ),
        "boundary_duration_ms": duration,
        "first_anchor": deepcopy(dict(first)),
        "last_boundary_anchor": deepcopy(dict(last)),
        "complete_wave_coverage": True,
        "source_status": team_wave.get("status"),
        "comparison_authorized": False,
    }


def _find_exact_player(
    team_wave: Mapping[str, Any], player_guid: str
) -> Mapping[str, Any]:
    wanted = _nonempty(player_guid, "player_guid")
    matches: list[Mapping[str, Any]] = []
    for raw in _array(team_wave.get("players"), "team_wave.players"):
        row = _mapping(raw, "team_wave player")
        identity = _mapping(row.get("player"), "team_wave player.player")
        guid = identity.get("guid")
        if isinstance(guid, str) and guid.casefold() == wanted.casefold():
            matches.append(row)
    if len(matches) != 1:
        raise OfflineWavePolicyV1Error(
            f"expected exactly one player {wanted!r}, found {len(matches)}"
        )
    row = matches[0]
    identity = _mapping(row.get("player"), "team_wave player.player")
    if identity.get("class") != "WARRIOR":
        raise OfflineWavePolicyV1Error(
            "offline team-wave policy currently supports warriors only"
        )
    lane = _mapping(
        row.get("warrior_spec_lane"), "team_wave player.warrior_spec_lane"
    )
    if (
        lane.get("evidence_status") != "OBSERVED"
        or lane.get("exact_guid_match") is not True
        or lane.get("fury_or_arms_conflict_free_observation") is not True
        or lane.get("observed_spec") not in {"Fury", "Arms"}
    ):
        raise OfflineWavePolicyV1Error(
            "player lacks an exact conflict-free observed warrior spec"
        )
    return row


def _normalize_event_label(
    transition: Mapping[str, Any],
) -> JSONMap:
    label = _mapping(
        transition.get("current_event_label"),
        "prefix transition.current_event_label",
    )
    action = _mapping(label.get("action"), "current_event_label.action")
    event_type = label.get("event_type")
    phase = action.get("phase")
    if event_type == "START" and phase != "START":
        raise OfflineWavePolicyV1Error(
            "START event label has a conflicting action phase"
        )
    if phase is None:
        phase = event_type
    observed: JSONMap = {
        "phase": phase,
        "order_key": deepcopy(transition.get("order_key")),
        "spell": deepcopy(label.get("spell")),
        "action_payload": {"item_id": action.get("item_id")},
        "exact_target": deepcopy(label.get("target")),
    }
    source_key = label.get("action_key")
    if isinstance(source_key, str) and source_key:
        observed["action_key"] = source_key
    else:
        spell = label.get("spell")
        spell_id = spell.get("id") if isinstance(spell, Mapping) else None
        if isinstance(spell_id, int) and not isinstance(spell_id, bool):
            known_key = _ACTION_KEY_BY_SPELL_ID.get(spell_id)
            if known_key is not None:
                observed["action_key"] = known_key
    return observed


def compile_offline_team_wave_feedback_policy_v1(
    team_wave: Mapping[str, Any],
    *,
    player_guid: str,
    build_segment_ref: str,
    policy_id: str | None = None,
    encounter_name: str | None = None,
) -> OfflineWaveFeedbackPolicyV1:
    """Compile one exact Stage-5 player-wave mode without Cat resolution."""

    if not isinstance(team_wave, Mapping):
        raise TypeError("team_wave must be a mapping")
    segment_ref = _nonempty(build_segment_ref, "build_segment_ref")
    boundary = offline_team_wave_boundary_metadata_v1(team_wave)
    selected = _find_exact_player(team_wave, player_guid)
    identity = _mapping(selected.get("player"), "team_wave player.player")

    transitions: list[JSONMap] = []
    build_bindings: list[JSONMap] = []
    for raw in _array(
        selected.get("prefix_transitions"), "player.prefix_transitions"
    ):
        transition = _mapping(raw, "player prefix transition")
        observed = _normalize_event_label(transition)
        state_before = _mapping(
            transition.get("state_before"), "prefix transition.state_before"
        )
        transitions.append(
            {
                "observed_event": observed,
                "state_before": {
                    "wave_elapsed_ms": _nonnegative_int(
                        state_before.get("wave_elapsed_ms"),
                        "state_before.wave_elapsed_ms",
                    )
                },
            }
        )
        if observed.get("phase") == "START":
            order = observed.get("order_key")
            if (
                not isinstance(order, list)
                or not order
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in order
                )
            ):
                raise OfflineWavePolicyV1Error(
                    "START action lacks a valid transition order_key"
                )
            build_bindings.append(
                {"order_key": deepcopy(order), "segment_ref": segment_ref}
            )

    episode_id = (
        f"external-team-wave:{boundary['encounter_id']}:"
        f"{identity['guid']}"
    )
    normalized: JSONMap = {
        "schema": KNOWN_WAVE_RECORD_SCHEMA,
        "episode_id": episode_id,
        "instance_id": boundary["instance_id"],
        "encounter_id": boundary["encounter_id"],
        "encounter_name": encounter_name
        or f"Trash:{boundary['encounter_id']}",
        "wave_id": boundary["wave_id"],
        "wave_ordinal": boundary["wave_ordinal"],
        "window": {
            "boundary_duration_ms": boundary["boundary_duration_ms"],
            "first_anchor": deepcopy(boundary["first_anchor"]),
            "last_boundary_anchor": deepcopy(
                boundary["last_boundary_anchor"]
            ),
        },
        "player": deepcopy(dict(identity)),
        "prefix_transitions": transitions,
        "source_boundary": boundary,
    }
    build_mapping: JSONMap = {
        "schema": KNOWN_BUILD_MAPPING_SCHEMA,
        "decision_bindings": build_bindings,
    }
    policy = compile_offline_wave_feedback_policy_v1(
        normalized,
        build_mapping=build_mapping,
        policy_id=policy_id,
    )
    if not policy.complete_wave_coverage:
        raise OfflineWavePolicyV1Error(
            "team-wave adapter did not retain complete wave coverage"
        )
    if any(row.build_segment_ref != segment_ref for row in policy.actions):
        raise OfflineWavePolicyV1Error(
            "not every executable action joined to the exact build segment"
        )
    if policy.build_segment_refs != (segment_ref,):
        raise OfflineWavePolicyV1Error(
            "team-wave policy must bind one exact build segment"
        )
    return policy


def load_offline_team_wave_record_v1(
    path: str | Path,
    *,
    wave_id: str,
) -> JSONMap:
    """Stream a JSONL(.gz) artifact and return one exact wave record."""

    source = Path(path)
    wanted = _nonempty(wave_id, "wave_id")
    opener = gzip.open if source.suffix.casefold() == ".gz" else open
    try:
        handle = opener(source, "rt", encoding="utf-8")
    except OSError as exc:
        raise OfflineWavePolicyV1Error(
            f"cannot open team-wave artifact {source}: {exc}"
        ) from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OfflineWavePolicyV1Error(
                    f"invalid JSON on team-wave line {line_number}: {exc}"
                ) from exc
            row = _mapping(value, f"team-wave line {line_number}")
            wave = row.get("wave")
            if isinstance(wave, Mapping) and wave.get("wave_id") == wanted:
                if row.get("schema") != KNOWN_TEAM_WAVE_SCHEMA_V2:
                    raise OfflineWavePolicyV1Error(
                        "matched wave has an unsupported schema"
                    )
                return dict(row)
    raise OfflineWavePolicyV1Error(
        f"wave_id {wanted!r} not found in {source}"
    )


__all__ = (
    "BOUNDARY_METADATA_SCHEMA_V1",
    "KNOWN_TEAM_WAVE_SCHEMA_V2",
    "compile_offline_team_wave_feedback_policy_v1",
    "load_offline_team_wave_record_v1",
    "offline_team_wave_boundary_metadata_v1",
)
