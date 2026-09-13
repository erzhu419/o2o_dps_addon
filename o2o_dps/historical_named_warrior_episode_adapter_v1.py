"""Stream exact-GUID named Warrior action episodes from External timeline V2.

The adapter joins the small named-player reference to only the referenced local
External-V2 instance partitions.  Each source wave remains one episode boundary
per named player.  A START is a server-observed action-start proxy, not a client
request or next-swing queue intent.  GO and FAIL are outcomes and never become
action labels.  State attached to an event is built strictly from earlier
direct-player events and earlier global death markers in the same wave.

This is historical observation evidence, not a matched simulator comparison and
not a Fury baseline.  In particular, the three currently configured references
are Arms players in the one post-fix clean raid available locally.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, TextIO

from . import chronicle_external_team_timeline_v2 as timeline_v2
from . import historical_warrior_reference_cohort_v1 as reference_v1


JSONMap = dict[str, Any]
SCHEMA = "historical_named_warrior_wave_episode/v1"
MANIFEST_SCHEMA = "historical_named_warrior_episode_manifest/v1"
IMPLEMENTATION_REVISION = (
    "v1.1_server_start_proxy_no_client_queue_inference_controllable_labels_only"
)
STATUS = "HISTORICAL_OBSERVATION_ONLY_NOT_COMPARISON"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMELINE_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_timeline"
    / "v2"
    / "utk_postfix_dev_20260903_noon"
    / "manifest.json"
)
DEFAULT_REFERENCE = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_warrior_reference_cohort"
    / "v1"
    / "reference.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_named_warrior_episodes"
    / "v1"
)

ACTION_EVENT_TYPES = frozenset({"START", "GO", "FAIL"})


class HistoricalNamedWarriorEpisodeError(RuntimeError):
    """A source binding, episode, or strict-prefix contract is inconsistent."""


@dataclass(frozen=True)
class ActionSpec:
    action_key: str
    spell_ids: frozenset[int]
    aliases: frozenset[str]
    lane: str


def _aliases(*values: str) -> frozenset[str]:
    return frozenset(value.strip().casefold() for value in values)


# Existing Cat2/simulator Warrior commands plus the observed Arms commands that
# the older Fury-only ontology omitted.  IDs are preferred; names are used only
# when the source has no positive spell ID.
ACTION_ONTOLOGY: tuple[ActionSpec, ...] = (
    ActionSpec("warrior.battle_shout", frozenset({25289}), _aliases("Battle Shout", "战斗怒吼"), "gcd"),
    ActionSpec("warrior.battle_stance", frozenset({2457}), _aliases("Battle Stance", "战斗姿态"), "off_gcd"),
    ActionSpec("warrior.berserker_stance", frozenset({2458}), _aliases("Berserker Stance", "狂暴姿态"), "off_gcd"),
    ActionSpec("warrior.defensive_stance", frozenset({71}), _aliases("Defensive Stance", "防御姿态"), "off_gcd"),
    ActionSpec("warrior.bloodrage", frozenset({2687}), _aliases("Bloodrage", "血性狂暴"), "off_gcd"),
    ActionSpec("warrior.bloodthirst", frozenset({23894}), _aliases("Bloodthirst", "嗜血"), "gcd"),
    ActionSpec("warrior.death_wish", frozenset({12328}), _aliases("Death Wish", "死亡之愿"), "gcd"),
    ActionSpec("warrior.execute", frozenset({20662}), _aliases("Execute", "斩杀"), "gcd"),
    ActionSpec("warrior.hamstring", frozenset({7373}), _aliases("Hamstring", "断筋"), "gcd"),
    ActionSpec("warrior.pummel", frozenset({6552}), _aliases("Pummel", "拳击"), "gcd"),
    ActionSpec("warrior.slam", frozenset({45961}), _aliases("Slam", "猛击"), "gcd"),
    ActionSpec("warrior.sunder_armor", frozenset({11597}), _aliases("Sunder Armor", "破甲攻击"), "gcd"),
    ActionSpec("warrior.whirlwind", frozenset({1680}), _aliases("Whirlwind", "旋风斩"), "gcd"),
    ActionSpec("warrior.heroic_strike", frozenset({11567, 25286}), _aliases("Heroic Strike", "英勇打击"), "next_swing"),
    ActionSpec("warrior.cleave", frozenset({20569}), _aliases("Cleave", "顺劈斩"), "next_swing"),
    ActionSpec("warrior.mortal_strike", frozenset({21553}), _aliases("Mortal Strike", "致死打击"), "gcd"),
    ActionSpec("warrior.sweeping_strikes", frozenset({12292}), _aliases("Sweeping Strikes", "横扫攻击"), "gcd"),
    ActionSpec("warrior.overpower", frozenset({11585}), _aliases("Overpower", "压制"), "gcd"),
    ActionSpec("warrior.charge", frozenset({11578}), _aliases("Charge", "冲锋"), "gcd"),
    ActionSpec("warrior.intercept", frozenset({20617}), _aliases("Intercept", "拦截"), "gcd"),
    ActionSpec("warrior.berserker_rage", frozenset({18499}), _aliases("Berserker Rage", "狂暴之怒"), "off_gcd"),
    ActionSpec("warrior.recklessness", frozenset({1719}), _aliases("Recklessness", "鲁莽"), "off_gcd"),
)

_SPEC_BY_ID = {
    spell_id: spec for spec in ACTION_ONTOLOGY for spell_id in spec.spell_ids
}
_SPEC_BY_ALIAS = {
    alias: spec for spec in ACTION_ONTOLOGY for alias in spec.aliases
}

# These GO rows are effects of a controllable action, not fresh decisions.
_TRIGGERED_PARENT_BY_ID = {
    20647: "warrior.execute",
    45960: "warrior.execute",
    45963: "warrior.execute",
    45964: "warrior.execute",
    53214: "warrior.execute",
    12723: "warrior.sweeping_strikes",
    26654: "warrior.sweeping_strikes",
    7922: "warrior.charge",
    20615: "warrior.intercept",
    29131: "warrior.bloodrage",
}


@dataclass(frozen=True)
class EpisodeBuildResult:
    manifest: Path
    content_addressed_manifest: Path
    partition_count: int
    episode_count: int
    transition_count: int
    server_observed_start_count: int
    controllable_policy_label_count: int
    unclassified_start_count: int

    def as_dict(self) -> JSONMap:
        return {
            "schema": MANIFEST_SCHEMA,
            "status": STATUS,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "partition_count": self.partition_count,
            "episode_count": self.episode_count,
            "transition_count": self.transition_count,
            "server_observed_start_count": self.server_observed_start_count,
            "controllable_policy_label_count": self.controllable_policy_label_count,
            "unclassified_start_count": self.unclassified_start_count,
        }


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HistoricalNamedWarriorEpisodeError(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return payload + (b"\n" if newline else b"")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalNamedWarriorEpisodeError(f"cannot read {path}: {error}") from error
    return digest.hexdigest()


def _content_addressed(value: Mapping[str, Any]) -> JSONMap:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": _sha256_bytes(_canonical_bytes(core)),
        },
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalNamedWarriorEpisodeError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalNamedWarriorEpisodeError(f"{label} must be an array")
    return value


def _text(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HistoricalNamedWarriorEpisodeError(f"{label} must be nonempty text")
    return value.strip()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalNamedWarriorEpisodeError(f"{label} must be an integer")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    result = _integer(value, label)
    if result < 0:
        raise HistoricalNamedWarriorEpisodeError(f"{label} must be nonnegative")
    return result


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalNamedWarriorEpisodeError(f"cannot read {label}: {error}") from error
    return deepcopy(dict(_mapping(value, label)))


def _spell_parts(spell: Mapping[str, Any]) -> tuple[int | None, str | None]:
    raw_id = spell.get("id")
    spell_id = (
        raw_id
        if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0
        else None
    )
    raw_name = spell.get("name")
    name = (
        raw_name.strip().casefold()
        if isinstance(raw_name, str) and raw_name.strip()
        else None
    )
    return spell_id, name


def action_spec(spell: Mapping[str, Any]) -> ActionSpec | None:
    """Resolve a supported action without guessing around an unknown positive ID."""

    spell_id, name = _spell_parts(spell)
    if spell_id is not None:
        return _SPEC_BY_ID.get(spell_id)
    return _SPEC_BY_ALIAS.get(name) if name is not None else None


def _fallback_action_key(spell: Mapping[str, Any]) -> str:
    spell_id, name = _spell_parts(spell)
    if spell_id is not None:
        return f"unmapped.spell_id.{spell_id}"
    if name is not None:
        return f"unmapped.spell_name.{name}"
    return "unmapped.no_spell_identity"


def _classify_action(event: Mapping[str, Any]) -> JSONMap:
    phase = str(event.get("event_type") or "").upper()
    if phase not in ACTION_EVENT_TYPES:
        raise HistoricalNamedWarriorEpisodeError(f"unsupported action phase {phase!r}")
    spell = _mapping(event.get("spell"), "event.spell")
    spec = action_spec(spell)
    spell_id, _ = _spell_parts(spell)
    triggered_parent = _TRIGGERED_PARENT_BY_ID.get(spell_id or -1)
    if spec is not None:
        action_key = spec.action_key
        lane = spec.lane
        ontology_status = "KNOWN_CONTROLLABLE_ACTION"
    elif triggered_parent is not None and phase == "GO":
        action_key = triggered_parent
        lane = "triggered_result"
        ontology_status = "KNOWN_TRIGGERED_RESULT_NOT_DECISION"
    else:
        action_key = _fallback_action_key(spell)
        lane = "unmapped"
        ontology_status = "UNMAPPED_DIRECT_EVENT_PRESERVED"
    policy_decision_label = (
        phase == "START" and ontology_status == "KNOWN_CONTROLLABLE_ACTION"
    )
    if phase == "START":
        role = (
            "SERVER_OBSERVED_START_CONTROLLABLE_ACTION_PROXY"
            if policy_decision_label
            else "SERVER_OBSERVED_START_UNCLASSIFIED_OBSERVATION"
        )
    else:
        role = "ACTION_RESULT_GO" if phase == "GO" else "ACTION_RESULT_FAIL"
    return {
        "phase": phase,
        "learning_role": role,
        "action_key": action_key,
        "action_lane": lane,
        "ontology_status": ontology_status,
        "server_observed_start_proxy": phase == "START",
        "client_action_request_observed": False,
        "client_next_swing_queue_intent_observed": False,
        "policy_decision_label": policy_decision_label,
    }


def _anchor_order(event: Mapping[str, Any], stable_index: int) -> tuple[int, int, int, int, int]:
    anchor = _mapping(event.get("anchor"), "event.anchor")
    timestamp = _nonnegative_integer(anchor.get("timestamp_ms"), "anchor.timestamp_ms")
    event_index = _integer(anchor.get("event_index"), "anchor.event_index")
    stream = _text(anchor.get("stream_type"), "anchor.stream_type")
    assert stream is not None
    if stream not in timeline_v2.STREAM_ORDER:
        raise HistoricalNamedWarriorEpisodeError(f"unsupported stream_type {stream!r}")
    frame_message = _nonnegative_integer(
        anchor.get("frame_message_index"), "anchor.frame_message_index"
    )
    return (
        timestamp,
        event_index,
        timeline_v2.STREAM_ORDER[stream],
        frame_message,
        stable_index,
    )


def _public_order(event: Mapping[str, Any]) -> list[int]:
    anchor = _mapping(event.get("anchor"), "event.anchor")
    stream = str(anchor["stream_type"])
    return [
        int(anchor["timestamp_ms"]),
        int(anchor["event_index"]),
        int(timeline_v2.STREAM_ORDER[stream]),
        int(anchor["frame_message_index"]),
    ]


def _target(event: Mapping[str, Any]) -> JSONMap:
    source = _mapping(event.get("target"), "event.target")
    return {
        "guid": source.get("guid"),
        "lane": source.get("lane"),
        "voting_enemy_target": source.get("voting_enemy_target") is True,
        "hostile_object_preserved_nonvoting": (
            source.get("hostile_object_preserved_nonvoting") is True
        ),
        "hostile_player_preserved_nonvoting": (
            source.get("hostile_player_preserved_nonvoting") is True
        ),
    }


def _is_exact_direct_event(event: Mapping[str, Any], player_guid: str) -> bool:
    attribution = _mapping(event.get("attribution"), "event.attribution")
    source = _mapping(event.get("source"), "event.source")
    key = player_guid.casefold()
    return (
        attribution.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER"
        and str(attribution.get("player_guid") or "").casefold() == key
        and str(attribution.get("source_guid") or "").casefold() == key
        and str(source.get("guid") or "").casefold() == key
        and source.get("lane") == "FRIENDLY_PLAYER"
    )


class _PrefixState:
    def __init__(self) -> None:
        self.merged_event_count = 0
        self.direct_player_event_count = 0
        self.action_event_count = 0
        self.direct_damage_event_count = 0
        self.direct_damage_total = 0
        self.damage_by_target: Counter[str] = Counter()
        self.seen_targets: set[str] = set()
        self.dead_targets: set[str] = set()
        self.last_target: str | None = None
        self.recent_actions: list[JSONMap] = []

    def snapshot(self, event: Mapping[str, Any]) -> JSONMap:
        order = _public_order(event)
        anchor = _mapping(event.get("anchor"), "event.anchor")
        return {
            "cutoff_semantics": "strictly before current event order_key",
            "cutoff_exclusive_order_key": order,
            "wave_elapsed_ms": _nonnegative_integer(
                anchor.get("offset_ms"), "anchor.offset_ms"
            ),
            "prefix_merged_event_count": self.merged_event_count,
            "prefix_direct_player_event_count": self.direct_player_event_count,
            "prefix_action_event_count": self.action_event_count,
            "prefix_direct_damage_event_count": self.direct_damage_event_count,
            "prefix_direct_damage_amount": self.direct_damage_total,
            "prefix_direct_damage_by_target": [
                {"target_guid": guid, "damage_amount": self.damage_by_target[guid]}
                for guid in sorted(self.damage_by_target, key=str.casefold)
            ],
            "seen_hostile_target_guids": sorted(
                self.seen_targets, key=str.casefold
            ),
            "observed_dead_target_guids": sorted(
                self.dead_targets, key=str.casefold
            ),
            "alive_seen_hostile_target_guids": sorted(
                self.seen_targets - self.dead_targets, key=str.casefold
            ),
            "last_observed_hostile_target_guid": self.last_target,
            "recent_direct_action_observations": deepcopy(self.recent_actions[-8:]),
        }

    def observe(self, event: Mapping[str, Any], *, direct: bool) -> None:
        self.merged_event_count += 1
        target = _target(event)
        guid = target.get("guid")
        hostile = target.get("voting_enemy_target") is True and isinstance(guid, str)
        if event.get("event_type") == "DEAD":
            if hostile:
                self.seen_targets.add(guid)
                self.dead_targets.add(guid)
            return
        if not direct:
            return
        self.direct_player_event_count += 1
        if hostile:
            self.seen_targets.add(guid)
            self.last_target = guid
        if event.get("event_type") == "DMG":
            damage = event.get("damage")
            if isinstance(damage, Mapping):
                amount = damage.get("amount")
                if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                    self.direct_damage_event_count += 1
                    self.direct_damage_total += amount
                    if hostile:
                        self.damage_by_target[guid] += amount

    def observe_action(
        self,
        event: Mapping[str, Any],
        classification: Mapping[str, Any],
    ) -> None:
        self.action_event_count += 1
        compact = {
            "order_key": _public_order(event),
            "phase": classification["phase"],
            "action_key": classification["action_key"],
            "target_guid": _target(event).get("guid"),
        }
        self.recent_actions.append(compact)


def _compact_observed_action(
    event: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> JSONMap:
    anchor = _mapping(event.get("anchor"), "event.anchor")
    action = event.get("action")
    return {
        **deepcopy(dict(classification)),
        "order_key": _public_order(event),
        "anchor": {
            key: anchor.get(key)
            for key in (
                "timestamp_ms",
                "offset_ms",
                "event_index",
                "stream_type",
                "frame_index",
                "frame_message_index",
                "derived_jsonl_line",
                "official_message_sha256",
            )
        },
        "spell": deepcopy(dict(_mapping(event.get("spell"), "event.spell"))),
        "exact_target": _target(event),
        "source_guid": _mapping(event.get("source"), "event.source").get("guid"),
        "action_payload": deepcopy(dict(action)) if isinstance(action, Mapping) else None,
    }


def build_strict_prefix_trace(
    wave: Mapping[str, Any],
    player_row: Mapping[str, Any],
    *,
    player_guid: str,
    classify_action: Callable[[Mapping[str, Any]], Mapping[str, Any]] = _classify_action,
) -> JSONMap:
    """Build the shared exact-GUID, strict-prefix action trace for one wave."""

    direct_events: list[Mapping[str, Any]] = []
    excluded_attribution = Counter()
    for raw in _array(player_row.get("timeline"), "player.timeline"):
        event = _mapping(raw, "player.timeline event")
        attribution = _mapping(event.get("attribution"), "event.attribution")
        if _is_exact_direct_event(event, player_guid):
            direct_events.append(event)
        else:
            if attribution.get("attribution_kind") == "DIRECT_FRIENDLY_PLAYER":
                raise HistoricalNamedWarriorEpisodeError(
                    "DIRECT_FRIENDLY_PLAYER event does not exact-match named GUID/source"
                )
            excluded_attribution[str(attribution.get("attribution_kind") or "UNKNOWN")] += 1

    merged: list[tuple[Mapping[str, Any], bool, int]] = []
    stable_index = 0
    for event in direct_events:
        merged.append((event, True, stable_index))
        stable_index += 1
    for raw in _array(wave.get("death_markers"), "wave.death_markers"):
        event = _mapping(raw, "death marker")
        if event.get("event_type") != "DEAD":
            raise HistoricalNamedWarriorEpisodeError("death marker is not DEAD")
        merged.append((event, False, stable_index))
        stable_index += 1
    merged.sort(key=lambda row: _anchor_order(row[0], row[2]))

    state = _PrefixState()
    transitions: list[JSONMap] = []
    action_counts: Counter[str] = Counter()
    server_start_action_counts: Counter[str] = Counter()
    controllable_label_action_counts: Counter[str] = Counter()
    unclassified_start_action_counts: Counter[str] = Counter()
    phase_counts: Counter[str] = Counter()
    ontology_counts: Counter[str] = Counter()
    for event, direct, _ in merged:
        event_type = str(event.get("event_type") or "").upper()
        if direct and event_type in ACTION_EVENT_TYPES:
            classification = _mapping(classify_action(event), "action classification")
            before = state.snapshot(event)
            observed = _compact_observed_action(event, classification)
            transitions.append(
                {
                    "trace_index": len(transitions),
                    "state_before": before,
                    "observed_event": observed,
                    "feature_cutoff_is_strict_prefix": True,
                    "current_event_present_in_state_before": False,
                    "future_outcomes_in_state_before": False,
                }
            )
            action_counts[str(classification["action_key"])] += 1
            phase_counts[event_type] += 1
            ontology_counts[str(classification["ontology_status"])] += 1
            if event_type == "START":
                action_key = str(classification["action_key"])
                server_start_action_counts[action_key] += 1
                if classification["policy_decision_label"] is True:
                    controllable_label_action_counts[action_key] += 1
                else:
                    unclassified_start_action_counts[action_key] += 1
            state.observe(event, direct=True)
            state.observe_action(event, classification)
        else:
            state.observe(event, direct=direct)

    return {
        "prefix_transitions": transitions,
        "summary": {
            "direct_source_event_count": len(direct_events),
            "excluded_non_direct_attribution_event_count": sum(
                excluded_attribution.values()
            ),
            "excluded_non_direct_attribution_counts": dict(
                sorted(excluded_attribution.items())
            ),
            "transition_count": len(transitions),
            "server_observed_start_count": phase_counts["START"],
            "controllable_policy_label_count": sum(
                controllable_label_action_counts.values()
            ),
            "unclassified_start_count": sum(
                unclassified_start_action_counts.values()
            ),
            "go_outcome_count": phase_counts["GO"],
            "fail_outcome_count": phase_counts["FAIL"],
            "phase_counts": dict(sorted(phase_counts.items())),
            "action_event_counts": dict(sorted(action_counts.items())),
            "server_start_action_counts": dict(
                sorted(server_start_action_counts.items())
            ),
            "controllable_label_action_counts": dict(
                sorted(controllable_label_action_counts.items())
            ),
            "unclassified_start_action_counts": dict(
                sorted(unclassified_start_action_counts.items())
            ),
            "ontology_status_counts": dict(sorted(ontology_counts.items())),
        },
    }


def _build_episode(
    wave: Mapping[str, Any],
    player_row: Mapping[str, Any],
    configured: Mapping[str, Any],
) -> JSONMap:
    player = _mapping(player_row.get("player"), "player record identity")
    guid = str(configured["character_guid"])
    if str(player.get("guid") or "").casefold() != guid.casefold():
        raise HistoricalNamedWarriorEpisodeError("timeline player GUID differs from reference")
    if str(player.get("class") or "").upper() != "WARRIOR":
        raise HistoricalNamedWarriorEpisodeError("named player is not a timeline Warrior")
    spec = _mapping(player_row.get("warrior_spec_evidence"), "warrior_spec_evidence")
    if (
        spec.get("exact_player_guid_match") is not True
        or spec.get("inference_used") is not False
        or spec.get("status") != "OBSERVED"
        or spec.get("player_spec") != "Arms"
    ):
        raise HistoricalNamedWarriorEpisodeError(
            "named player lacks exact observed Arms spec evidence"
        )

    trace = build_strict_prefix_trace(
        wave,
        player_row,
        player_guid=guid,
    )
    transitions = _array(trace.get("prefix_transitions"), "strict prefix transitions")
    trace_summary = _mapping(trace.get("summary"), "strict prefix summary")

    instance_id = _text(wave.get("instance_id"), "wave.instance_id")
    wave_id = _text(wave.get("wave_id"), "wave.wave_id")
    encounter_id = _text(
        wave.get("encounter_id"), "wave.encounter_id", optional=True
    )
    assert instance_id is not None and wave_id is not None
    episode_identity = {
        "instance_id": instance_id,
        "encounter_id": encounter_id,
        "encounter_ordinal": _nonnegative_integer(
            wave.get("encounter_ordinal"), "wave.encounter_ordinal"
        ),
        "wave_id": wave_id,
        "wave_ordinal": _nonnegative_integer(
            wave.get("wave_ordinal"), "wave.wave_ordinal"
        ),
        "player_guid": guid.casefold(),
    }
    core = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "record_type": "named_warrior_wave_episode",
        "status": STATUS,
        "episode_id": _sha256_bytes(_canonical_bytes(episode_identity)),
        "instance_id": instance_id,
        "encounter_id": encounter_id,
        "encounter_ordinal": episode_identity["encounter_ordinal"],
        "wave_id": wave_id,
        "wave_ordinal": episode_identity["wave_ordinal"],
        "player": {
            "guid": guid,
            "configured_name": configured["configured_name"],
            "timeline_name": player.get("name"),
            "class": "WARRIOR",
            "spec": "Arms",
            "spec_evidence": deepcopy(dict(spec)),
        },
        "source_wave": {
            "timeline_wave_content_sha256": _mapping(
                wave.get("content_address"), "wave.content_address"
            ).get("sha256"),
            "source_binding_sha256": wave.get("source_binding_sha256"),
            "source_record_is_episode_boundary": True,
            "encounter_id_preserved_without_synthetic_replacement": True,
        },
        "prefix_transitions": transitions,
        "summary": deepcopy(dict(trace_summary)),
        "contracts": {
            "identity": "exact configured GUID plus DIRECT_FRIENDLY_PLAYER source",
            "event_order": (
                "timestamp_ms, event_index, official stream order, "
                "frame_message_index, stable input ordinal"
            ),
            "state_cutoff": "strictly before current event order_key",
            "start_semantics": (
                "server-observed action-start proxy; not a client request or "
                "next-swing queue intent"
            ),
            "go_fail_semantics": "outcome only; never a policy decision label",
            "client_request_and_queue_intent": (
                "MISSING; never inferred from START, GO, or FAIL"
            ),
            "death_context": "only DEAD markers earlier in this source wave",
            "target_identity": (
                "exact server-resolved target GUID; no name/suffix join and no "
                "target-switch intent claim"
            ),
            "wave_boundary": "one source wave row is one player episode boundary",
        },
        "scientific_boundaries": {
            "historical_observation_only": True,
            "fury_policy_baseline": False,
            "client_action_request_observed": False,
            "client_next_swing_queue_intent_observed": False,
            "queue_replacement_or_cancel_observed": False,
            "unclassified_start_is_policy_label": False,
            "matched_build_team_counterfactual_present": False,
            "comparison_authorized": False,
            "superiority_claim_authorized": False,
        },
    }
    return core


def _reference_memberships(reference: Mapping[str, Any]) -> dict[str, dict[str, JSONMap]]:
    try:
        reference_v1.validate_reference_document(reference)
    except reference_v1.HistoricalWarriorReferenceError as error:
        raise HistoricalNamedWarriorEpisodeError(str(error)) from error
    result: dict[str, dict[str, JSONMap]] = {}
    for raw_player in _array(reference.get("players"), "reference.players"):
        player = _mapping(raw_player, "reference player")
        guid = _text(player.get("character_guid"), "reference.character_guid")
        name = _text(player.get("requested_name"), "reference.requested_name")
        assert guid is not None and name is not None
        if player.get("selected_specs") != ["Arms"]:
            raise HistoricalNamedWarriorEpisodeError(
                "named episode V1 accepts the observed Arms reference lane only"
            )
        for raw_raid in _array(player.get("raids"), "reference.player.raids"):
            raid = _mapping(raw_raid, "reference raid")
            instance_id = _text(raid.get("instance_id"), "reference.instance_id")
            assert instance_id is not None
            by_guid = result.setdefault(instance_id, {})
            key = guid.casefold()
            if key in by_guid:
                raise HistoricalNamedWarriorEpisodeError(
                    "duplicate named GUID membership in one reference raid"
                )
            by_guid[key] = {
                "character_guid": guid,
                "configured_name": name,
            }
    if not result:
        raise HistoricalNamedWarriorEpisodeError("reference has no selected raid membership")
    return result


def _resolve_partition(
    manifest_path: Path, entry: Mapping[str, Any]
) -> tuple[Path, Mapping[str, Any]]:
    partition = _mapping(entry.get("partition"), "timeline instance partition")
    if partition.get("record_schema") != timeline_v2.PARTITION_RECORD_SCHEMA:
        raise HistoricalNamedWarriorEpisodeError("timeline partition record schema changed")
    relative_text = _text(partition.get("path"), "partition.path")
    assert relative_text is not None
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise HistoricalNamedWarriorEpisodeError("timeline partition path escapes manifest")
    base = manifest_path.parent.resolve()
    resolved = (base / relative).resolve()
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise HistoricalNamedWarriorEpisodeError("timeline partition is missing or escapes")
    expected_size = _nonnegative_integer(
        partition.get("compressed_size_bytes"), "partition.compressed_size_bytes"
    )
    if resolved.stat().st_size != expected_size:
        raise HistoricalNamedWarriorEpisodeError("timeline partition compressed size mismatch")
    expected_sha = _text(
        partition.get("compressed_file_sha256"), "partition.compressed_file_sha256"
    )
    assert expected_sha is not None
    if _sha256_file(resolved) != expected_sha:
        raise HistoricalNamedWarriorEpisodeError("timeline partition compressed hash mismatch")
    return resolved, partition


def _iter_verified_waves(
    path: Path,
    partition: Mapping[str, Any],
    *,
    instance_id: str,
) -> Iterator[JSONMap]:
    logical = hashlib.sha256()
    logical_size = 0
    count = 0
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, line in enumerate(handle, 1):
                logical.update(line)
                logical_size += len(line)
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HistoricalNamedWarriorEpisodeError(
                        f"invalid timeline JSONL row {line_number}: {error}"
                    ) from error
                wave = deepcopy(dict(_mapping(raw, "timeline wave")))
                if wave.get("schema") != timeline_v2.PARTITION_RECORD_SCHEMA:
                    raise HistoricalNamedWarriorEpisodeError("timeline wave schema changed")
                if wave.get("instance_id") != instance_id:
                    raise HistoricalNamedWarriorEpisodeError(
                        "timeline wave instance differs from selected partition"
                    )
                count += 1
                yield wave
    except (OSError, EOFError, UnicodeError, gzip.BadGzipFile) as error:
        raise HistoricalNamedWarriorEpisodeError(
            f"cannot stream timeline partition {path}: {error}"
        ) from error
    if count != _nonnegative_integer(partition.get("record_count"), "partition.record_count"):
        raise HistoricalNamedWarriorEpisodeError("timeline partition record count mismatch")
    if logical_size != _nonnegative_integer(
        partition.get("logical_size_bytes"), "partition.logical_size_bytes"
    ):
        raise HistoricalNamedWarriorEpisodeError("timeline partition logical size mismatch")
    expected = _text(
        partition.get("logical_content_sha256"), "partition.logical_content_sha256"
    )
    if logical.hexdigest() != expected:
        raise HistoricalNamedWarriorEpisodeError("timeline partition logical hash mismatch")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _output_under_offline_data(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if "offline_data" not in {part.casefold() for part in resolved.parts}:
        raise HistoricalNamedWarriorEpisodeError(
            "generated episode output must stay under ignored offline_data"
        )
    return resolved


def _write_instance_partition(
    *,
    output_directory: Path,
    instance_id: str,
    waves: Iterable[Mapping[str, Any]],
    memberships: Mapping[str, Mapping[str, Any]],
) -> JSONMap:
    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_directory, prefix=f".{instance_id}.", suffix=".jsonl.gz", delete=False
    ) as raw_handle:
        temporary = Path(raw_handle.name)
        logical = hashlib.sha256()
        logical_size = 0
        episodes = 0
        transitions = 0
        server_starts = 0
        controllable_labels = 0
        unclassified_starts = 0
        phases: Counter[str] = Counter()
        actions: Counter[str] = Counter()
        server_start_actions: Counter[str] = Counter()
        controllable_label_actions: Counter[str] = Counter()
        unclassified_start_actions: Counter[str] = Counter()
        source_wave_count = 0
        try:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_handle, mtime=0
            ) as compressed:
                for wave in waves:
                    source_wave_count += 1
                    players = {
                        str(_mapping(row, "wave player").get("player", {}).get("guid") or "").casefold():
                        _mapping(row, "wave player")
                        for row in _array(wave.get("players"), "wave.players")
                    }
                    for guid_key, configured in sorted(memberships.items()):
                        if guid_key not in players:
                            raise HistoricalNamedWarriorEpisodeError(
                                f"named player {guid_key} is absent from wave roster"
                            )
                        episode = _build_episode(wave, players[guid_key], configured)
                        payload = _canonical_bytes(episode, newline=True)
                        compressed.write(payload)
                        logical.update(payload)
                        logical_size += len(payload)
                        episodes += 1
                        summary = _mapping(episode.get("summary"), "episode.summary")
                        transitions += int(summary["transition_count"])
                        server_starts += int(summary["server_observed_start_count"])
                        controllable_labels += int(
                            summary["controllable_policy_label_count"]
                        )
                        unclassified_starts += int(
                            summary["unclassified_start_count"]
                        )
                        phases.update(summary["phase_counts"])
                        actions.update(summary["action_event_counts"])
                        server_start_actions.update(
                            summary["server_start_action_counts"]
                        )
                        controllable_label_actions.update(
                            summary["controllable_label_action_counts"]
                        )
                        unclassified_start_actions.update(
                            summary["unclassified_start_action_counts"]
                        )
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        except Exception:
            raw_handle.close()
            if temporary.exists():
                temporary.unlink()
            raise
    logical_sha = logical.hexdigest()
    final = output_directory / f"{instance_id}.{logical_sha}.jsonl.gz"
    os.replace(temporary, final)
    return {
        "instance_id": instance_id,
        "path": final.name,
        "record_schema": SCHEMA,
        "source_wave_count": source_wave_count,
        "named_player_count": len(memberships),
        "record_count": episodes,
        "transition_count": transitions,
        "server_observed_start_count": server_starts,
        "controllable_policy_label_count": controllable_labels,
        "unclassified_start_count": unclassified_starts,
        "phase_counts": dict(sorted(phases.items())),
        "action_event_counts": dict(sorted(actions.items())),
        "server_start_action_counts": dict(sorted(server_start_actions.items())),
        "controllable_label_action_counts": dict(
            sorted(controllable_label_actions.items())
        ),
        "unclassified_start_action_counts": dict(
            sorted(unclassified_start_actions.items())
        ),
        "logical_size_bytes": logical_size,
        "logical_content_sha256": logical_sha,
        "compressed_size_bytes": final.stat().st_size,
        "compressed_file_sha256": _sha256_file(final),
        "gzip_mtime": 0,
    }


def build_historical_named_warrior_episodes(
    *,
    timeline_manifest_path: str | Path = DEFAULT_TIMELINE_MANIFEST,
    reference_path: str | Path = DEFAULT_REFERENCE,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
) -> EpisodeBuildResult:
    """Build small named-player episode partitions from referenced local raids."""

    timeline_path = Path(timeline_manifest_path).expanduser().resolve()
    try:
        timeline, resolved_timeline = timeline_v2.load_external_team_timeline_manifest(
            timeline_path,
            verify_inputs=False,
            verify_partitions=False,
        )
    except timeline_v2.ChronicleExternalTeamTimelineV2Error as error:
        raise HistoricalNamedWarriorEpisodeError(str(error)) from error
    ref_path = Path(reference_path).expanduser().resolve()
    reference = _load_json(ref_path, "named Warrior reference")
    memberships = _reference_memberships(reference)
    destination = _output_under_offline_data(Path(output_directory))

    entries = {
        str(_mapping(raw, "timeline instance").get("instance_id")): _mapping(
            raw, "timeline instance"
        )
        for raw in _array(timeline.get("instances"), "timeline.instances")
    }
    missing = sorted(set(memberships) - set(entries))
    if missing:
        raise HistoricalNamedWarriorEpisodeError(
            f"referenced raids are absent from local timeline: {missing}"
        )

    built_partitions = []
    for instance_id in sorted(memberships):
        entry = entries[instance_id]
        provenance = _mapping(entry.get("instance_provenance"), "instance_provenance")
        temporal = _mapping(
            provenance.get("temporal_and_guild_provenance"),
            "temporal_and_guild_provenance",
        )
        contamination = _mapping(temporal.get("contamination"), "contamination")
        if contamination.get("label") != "POSTFIX_KNOWN_CLEAN":
            raise HistoricalNamedWarriorEpisodeError(
                "named reference instance is not POSTFIX_KNOWN_CLEAN in timeline"
            )
        source_partition_path, source_partition = _resolve_partition(
            resolved_timeline, entry
        )
        built = _write_instance_partition(
            output_directory=destination,
            instance_id=instance_id,
            waves=_iter_verified_waves(
                source_partition_path,
                source_partition,
                instance_id=instance_id,
            ),
            memberships=memberships[instance_id],
        )
        built["source_timeline_partition"] = {
            key: source_partition.get(key)
            for key in (
                "path",
                "record_schema",
                "record_count",
                "compressed_size_bytes",
                "compressed_file_sha256",
                "logical_size_bytes",
                "logical_content_sha256",
            )
        }
        built_partitions.append(built)

    summary = {
        "instance_count": len(built_partitions),
        "named_player_membership_count": sum(
            len(values) for values in memberships.values()
        ),
        "source_wave_count": sum(row["source_wave_count"] for row in built_partitions),
        "episode_count": sum(row["record_count"] for row in built_partitions),
        "transition_count": sum(row["transition_count"] for row in built_partitions),
        "server_observed_start_count": sum(
            row["server_observed_start_count"] for row in built_partitions
        ),
        "controllable_policy_label_count": sum(
            row["controllable_policy_label_count"] for row in built_partitions
        ),
        "unclassified_start_count": sum(
            row["unclassified_start_count"] for row in built_partitions
        ),
        "go_outcome_count": sum(
            row["phase_counts"].get("GO", 0) for row in built_partitions
        ),
        "fail_outcome_count": sum(
            row["phase_counts"].get("FAIL", 0) for row in built_partitions
        ),
    }
    manifest_core = {
        "schema": MANIFEST_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "input_closure": {
            "timeline_manifest": {
                "path": str(resolved_timeline),
                "schema": timeline.get("schema"),
                "content_sha256": _mapping(
                    timeline.get("content_address"), "timeline.content_address"
                ).get("sha256"),
            },
            "named_reference": {
                "path": str(ref_path),
                "schema": reference.get("schema"),
                "file_sha256": _sha256_file(ref_path),
            },
            "network_request_count": 0,
            "only_reference_instance_partitions_read": True,
        },
        "episode_contract": {
            "identity": "exact GUID plus DIRECT_FRIENDLY_PLAYER only",
            "ordering": (
                "timestamp_ms then event_index then official stream order then "
                "frame_message_index with stable input tie-break"
            ),
            "start": (
                "server-observed action-start proxy; only ontology-confirmed "
                "controllable START is a policy label"
            ),
            "go_fail": "outcome only",
            "client_request_and_queue_intent": (
                "MISSING; no queue set/replace/cancel inference"
            ),
            "target": (
                "exact server-resolved target GUID preserved; target-switch "
                "intent not claimed"
            ),
            "state": "strict prefix inside source wave",
            "boundary": "source wave, including null encounter_id if present",
        },
        "partitions": built_partitions,
        "summary": summary,
        "scientific_boundaries": {
            "historical_observation_only": True,
            "arms_lane_preserved": True,
            "fury_policy_baseline": False,
            "client_action_request_observed": False,
            "client_next_swing_queue_intent_observed": False,
            "queue_replacement_or_cancel_observed": False,
            "unclassified_start_is_policy_label": False,
            "comparison_authorized": False,
            "training_or_superiority_claim_made": False,
        },
    }
    manifest = _content_addressed(manifest_core)
    payload = _canonical_bytes(manifest, newline=True)
    stable = destination / "manifest.json"
    address = str(_mapping(manifest["content_address"], "content_address")["sha256"])
    addressed = destination / (
        f"historical_named_warrior_episode_adapter_v1.{address}.manifest.json"
    )
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return EpisodeBuildResult(
        manifest=stable,
        content_addressed_manifest=addressed,
        partition_count=summary["instance_count"],
        episode_count=summary["episode_count"],
        transition_count=summary["transition_count"],
        server_observed_start_count=summary["server_observed_start_count"],
        controllable_policy_label_count=summary[
            "controllable_policy_label_count"
        ],
        unclassified_start_count=summary["unclassified_start_count"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build strict-prefix named Warrior episodes from local External V2"
    )
    parser.add_argument("--timeline-manifest", type=Path, default=DEFAULT_TIMELINE_MANIFEST)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_named_warrior_episodes(
        timeline_manifest_path=args.timeline_manifest,
        reference_path=args.reference,
        output_directory=args.output_directory,
    )
    rendered = _canonical_bytes(result.as_dict(), newline=True).decode("utf-8")
    if stdout is None:
        print(rendered, end="")
    else:
        stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalNamedWarriorEpisodeError as error:
        print(str(error), file=os.sys.stderr)
        raise SystemExit(2)


__all__ = [
    "ACTION_ONTOLOGY",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_REFERENCE",
    "DEFAULT_TIMELINE_MANIFEST",
    "EpisodeBuildResult",
    "HistoricalNamedWarriorEpisodeError",
    "IMPLEMENTATION_REVISION",
    "MANIFEST_SCHEMA",
    "SCHEMA",
    "STATUS",
    "action_spec",
    "build_historical_named_warrior_episodes",
    "build_strict_prefix_trace",
    "main",
]
