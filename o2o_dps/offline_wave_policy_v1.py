"""Chronicle-demonstration-driven complete-wave policy, independent of Cat.

The historical behavior-clone lane intentionally pools observations.  This
module preserves a different object: one complete player-wave demonstration
with its observed action order, timing, target changes, and build-segment
bindings.  The runtime replays that mode as a causal feedback controller and
uses only source-derived action priorities after the finite demonstration is
exhausted.  Cat is not part of the supported-wave control path.

Chronicle observes server START events, not the original client request.  In
particular, next-swing queue request/cancel timing and idle intent remain
missing.  Observed inter-START gaps are therefore schedule evidence, never an
``intentional WAIT`` label.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .causal_action_program_v1 import (
    CausalActionProgramV1,
    ImportedReactiveProgramBindingV1,
    ImportedReactiveSelectorV1,
    OptionalOffGcdPrefixV1,
    ProgramDecisionV1,
    ProgramOriginV1,
    ProgramPrefixOperationKindV1,
)
from .causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from .expert_policy import SwingQueueOp
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .policy_observation_causal_projection_v1 import CausalLiveStateProjectionV1
from .sim_bridge import ActionRef, AvailableAction
from .upper_kara_imported_incumbent_program_v1 import (
    OBSERVATION_CONTRACT_ID_V1,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
MODE_SCHEMA = "offline_wave_feedback_policy/v1"
LIBRARY_SCHEMA = "offline_wave_feedback_policy_library/v1"
CONTRIBUTION_SCHEMA = "offline_wave_policy_contribution_receipt/v1"
EXECUTION_CONTRIBUTION_SCHEMA = "offline_wave_policy_execution_contribution_receipt/v1"
RUNTIME_BINDING_SCHEMA = "offline_wave_policy_runtime_binding/v1"
SCOPE = "CHRONICLE_COMPLETE_PLAYER_WAVE_DEMONSTRATION"

KNOWN_EPISODE_SCHEMA = "historical_fury_expert_observation_episode/v1"
KNOWN_WAVE_RECORD_SCHEMA = "historical_named_warrior_wave_episode/v1"
KNOWN_BUILD_MAPPING_SCHEMA = "historical_fury_decision_build_mapping/v1"

TARGET_NO_EXPLICIT = "NO_EXPLICIT_TARGET"
TARGET_CURRENT = "CURRENT_ENEMY"
TARGET_OTHER = "OTHER_OR_NEW_ENEMY"
TARGET_SELF = "SELF"
TARGET_ROLES = frozenset(
    {TARGET_NO_EXPLICIT, TARGET_CURRENT, TARGET_OTHER, TARGET_SELF}
)

LANE_GCD = "gcd"
LANE_OFF_GCD = "off_gcd"
LANE_QUEUE = "queue"
LANES = frozenset({LANE_GCD, LANE_OFF_GCD, LANE_QUEUE})

EVIDENCE_KNOWN_ONTOLOGY_START = "KNOWN_ONTOLOGY_SERVER_START"
EVIDENCE_EXTENDED_ONTOLOGY_START = "EXTENDED_ONTOLOGY_SERVER_START"
EVIDENCE_EXACT_ITEM_START = "EXACT_ITEM_SERVER_START"
EVIDENCE_STATIC_ACTUATOR_START = "STATIC_NATIVE_ACTUATOR_SERVER_START"

MISSING_CLIENT_FIELDS = (
    "client_request_time",
    "client_next_swing_queue_set_replace_cancel",
    "exact_rage_at_request",
    "exact_swing_deadlines_at_request",
)


class OfflineWavePolicyV1Error(RuntimeError):
    """A demonstration, runtime observation, or execution receipt is invalid."""


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfflineWavePolicyV1Error(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OfflineWavePolicyV1Error(f"{label} must be an array")
    return value


def _action_ref(value: object, label: str) -> ActionRef:
    if not isinstance(value, ActionRef):
        raise TypeError(f"{label} must be ActionRef")
    identities = (value.spell_id, value.item_id, value.other_id)
    if (
        any(
            isinstance(row, bool) or not isinstance(row, int) or row < 0
            for row in (*identities, value.tag)
        )
        or sum(row > 0 for row in identities) != 1
    ):
        raise ValueError(f"{label} has an invalid exact identity")
    return value


# The original 15-action ontology remains the core.  The expanded registry is
# deliberately expressed in native simulator identities: Chronicle supplies
# the observed START spell while the exact build action surface decides at
# runtime whether that source action is executable.  This recovers decisions
# the old Fury-only ontology retained as ``unmapped`` without inventing an
# action that the selected build does not have.
_ACTION_REF_BY_KEY: dict[str, ActionRef] = dict(ACTION_KEY_TO_REF)
_ACTION_REF_BY_KEY.update(
    {
        "warrior.heroic_strike": QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE],
        "warrior.cleave": QUEUE_REFS[SwingQueueOp.CLEAVE],
        "warrior.recklessness": ActionRef(spell_id=1719),
        "warrior.sweeping_strikes": ActionRef(spell_id=12292),
        "warrior.overpower": ActionRef(spell_id=11585),
        "warrior.charge": ActionRef(spell_id=11578),
        "warrior.intercept": ActionRef(spell_id=20617),
        "warrior.berserker_rage": ActionRef(spell_id=18499),
        "racial.perception": ActionRef(spell_id=20600),
        "racial.blood_fury": ActionRef(spell_id=20572),
        "racial.berserking": ActionRef(spell_id=26297),
        "consume.juju_flurry": ActionRef(spell_id=16322),
        "item.mighty_rage_potion": ActionRef(item_id=13442),
        "item.slayers_crest": ActionRef(item_id=23041),
        "item.kiss_of_the_spider": ActionRef(item_id=22954),
        "item.goblin_sapper_charge": ActionRef(item_id=10646),
        "item.quickness_potion": ActionRef(item_id=61181),
        "item.elixir_of_rapid_growth": ActionRef(item_id=56113),
    }
)

_LANE_BY_KEY: dict[str, str] = {
    **{key: LANE_GCD for key in ACTION_KEY_TO_REF},
    "warrior.battle_stance": LANE_OFF_GCD,
    "warrior.berserker_stance": LANE_OFF_GCD,
    "warrior.bloodrage": LANE_OFF_GCD,
    "warrior.defensive_stance": LANE_OFF_GCD,
    "warrior.heroic_strike": LANE_QUEUE,
    "warrior.cleave": LANE_QUEUE,
    "warrior.recklessness": LANE_GCD,
    "warrior.sweeping_strikes": LANE_GCD,
    "warrior.overpower": LANE_GCD,
    "warrior.charge": LANE_GCD,
    "warrior.intercept": LANE_GCD,
    "warrior.berserker_rage": LANE_OFF_GCD,
    "racial.perception": LANE_OFF_GCD,
    "racial.blood_fury": LANE_OFF_GCD,
    "racial.berserking": LANE_OFF_GCD,
    "consume.juju_flurry": LANE_OFF_GCD,
    "item.mighty_rage_potion": LANE_OFF_GCD,
    "item.slayers_crest": LANE_OFF_GCD,
    "item.kiss_of_the_spider": LANE_OFF_GCD,
    "item.goblin_sapper_charge": LANE_OFF_GCD,
    "item.quickness_potion": LANE_OFF_GCD,
    "item.elixir_of_rapid_growth": LANE_OFF_GCD,
}

# Direct spell actions.  Rank-one Heroic Strike is normalized to the native
# max-rank queue identity because the simulator queue lane is rank-agnostic.
_EXTENDED_START_BY_SPELL_ID: Mapping[int, str] = {
    78: "warrior.heroic_strike",
    1719: "warrior.recklessness",
    11578: "warrior.charge",
    11585: "warrior.overpower",
    12292: "warrior.sweeping_strikes",
    16322: "consume.juju_flurry",
    18499: "warrior.berserker_rage",
    20572: "racial.blood_fury",
    20600: "racial.perception",
    20617: "warrior.intercept",
    26297: "racial.berserking",
}

# These START spell IDs are activation effects whose concrete native actuator
# is already established elsewhere in this repository.  The conversion is
# still build-conditioned: if that item/action is absent from actions(), the
# source ordinal is masked and never executed.  Unknown effects remain in
# unresolved_start_evidence.
_STATIC_ACTUATOR_BY_EFFECT_SPELL_ID: Mapping[int, str] = {
    13241: "item.goblin_sapper_charge",
    17528: "item.mighty_rage_potion",
    28777: "item.slayers_crest",
    28866: "item.kiss_of_the_spider",
    45425: "item.quickness_potion",
    46102: "item.elixir_of_rapid_growth",
}

_FINITE_RESOURCE_BY_KEY = {
    "warrior.death_wish": "cooldown.death_wish",
    "warrior.recklessness": "cooldown.recklessness",
    "warrior.sweeping_strikes": "cooldown.sweeping_strikes",
    "consume.juju_flurry": "consume.juju_flurry",
    "item.mighty_rage_potion": "potion.combat",
    "item.slayers_crest": "trinket.slayers_crest",
    "item.kiss_of_the_spider": "trinket.kiss_of_the_spider",
    "item.goblin_sapper_charge": "engineering.sapper",
    "item.quickness_potion": "potion.combat",
    "item.elixir_of_rapid_growth": "consume.elixir_of_rapid_growth",
}


@dataclass(frozen=True)
class OfflineWaveActionV1:
    source_ordinal: int
    at_or_after_ms: int
    action_key: str
    action_ref: ActionRef
    lane: str
    target_role: str
    source_target_ordinal: int | None
    source_order_key: tuple[int, ...]
    evidence_status: str
    build_segment_ref: str | None = None
    resource_id: str | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.source_ordinal, "source_ordinal")
        _nonnegative_int(self.at_or_after_ms, "at_or_after_ms")
        object.__setattr__(self, "action_key", _nonempty(self.action_key, "action_key"))
        _action_ref(self.action_ref, "action_ref")
        if self.lane not in LANES:
            raise ValueError(f"unsupported action lane {self.lane!r}")
        if self.target_role not in TARGET_ROLES:
            raise ValueError(f"unsupported target role {self.target_role!r}")
        if self.source_target_ordinal is not None:
            _nonnegative_int(self.source_target_ordinal, "source_target_ordinal")
        if (
            not isinstance(self.source_order_key, tuple)
            or not self.source_order_key
            or any(
                isinstance(row, bool) or not isinstance(row, int) or row < 0
                for row in self.source_order_key
            )
        ):
            raise ValueError("source_order_key must contain nonnegative integers")
        if self.evidence_status not in {
            EVIDENCE_KNOWN_ONTOLOGY_START,
            EVIDENCE_EXTENDED_ONTOLOGY_START,
            EVIDENCE_EXACT_ITEM_START,
            EVIDENCE_STATIC_ACTUATOR_START,
        }:
            raise ValueError("unsupported evidence_status")
        if self.build_segment_ref is not None:
            object.__setattr__(
                self,
                "build_segment_ref",
                _nonempty(self.build_segment_ref, "build_segment_ref"),
            )
        if self.resource_id is not None:
            object.__setattr__(
                self, "resource_id", _nonempty(self.resource_id, "resource_id")
            )

    def to_dict(self) -> JSONMap:
        return {
            "source_ordinal": self.source_ordinal,
            "at_or_after_ms": self.at_or_after_ms,
            "action_key": self.action_key,
            "action_ref": self.action_ref.to_wire(),
            "lane": self.lane,
            "target_role": self.target_role,
            "source_target_ordinal": self.source_target_ordinal,
            "source_order_key": list(self.source_order_key),
            "evidence_status": self.evidence_status,
            "build_segment_ref": self.build_segment_ref,
            "resource_id": self.resource_id,
            "observed_channels": ["server_start", "timing", "target"],
            "missing_channels": list(MISSING_CLIENT_FIELDS),
            "timing_semantics": (
                "OBSERVED_SERVER_START_NOT_BEFORE_SCHEDULE;_NOT_AN_"
                "INTENTIONAL_WAIT_LABEL"
            ),
        }


@dataclass(frozen=True)
class OfflineWaveFeedbackPolicyV1:
    policy_id: str
    mode_id: str
    encounter_name: str
    source_instance_id: str
    source_episode_id: str
    source_wave_id: str
    source_player_guid: str
    source_player_name: str | None
    source_wave_ordinal: int
    observed_duration_ms: int
    complete_wave_coverage: bool
    observed_target_count: int
    source_target_guids: tuple[str, ...]
    build_segment_refs: tuple[str, ...]
    actions: tuple[OfflineWaveActionV1, ...]
    unresolved_start_evidence: tuple[JSONMap, ...]
    tail_gcd_priority: tuple[str, ...]
    tail_queue_priority: tuple[str, ...]
    tail_off_gcd_once: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "policy_id",
            "mode_id",
            "encounter_name",
            "source_instance_id",
            "source_episode_id",
            "source_wave_id",
            "source_player_guid",
        ):
            object.__setattr__(self, field, _nonempty(getattr(self, field), field))
        if self.source_player_name is not None:
            object.__setattr__(
                self,
                "source_player_name",
                _nonempty(self.source_player_name, "source_player_name"),
            )
        _nonnegative_int(self.source_wave_ordinal, "source_wave_ordinal")
        _nonnegative_int(self.observed_duration_ms, "observed_duration_ms")
        if self.observed_duration_ms <= 0:
            raise ValueError("observed_duration_ms must be positive")
        if not isinstance(self.complete_wave_coverage, bool):
            raise TypeError("complete_wave_coverage must be boolean")
        _nonnegative_int(self.observed_target_count, "observed_target_count")
        if self.observed_target_count <= 0:
            raise ValueError("observed_target_count must be positive")
        if (
            not isinstance(self.source_target_guids, tuple)
            or not self.source_target_guids
        ):
            raise TypeError("source_target_guids must be a nonempty tuple")
        normalized_target_guids = tuple(
            _nonempty(value, "source_target_guid") for value in self.source_target_guids
        )
        if len({value.casefold() for value in normalized_target_guids}) != len(
            normalized_target_guids
        ):
            raise ValueError("source_target_guids must be unique")
        if len(normalized_target_guids) != self.observed_target_count:
            raise ValueError("source_target_guids must identify every observed target")
        object.__setattr__(self, "source_target_guids", normalized_target_guids)
        if (
            not isinstance(self.actions, tuple)
            or not self.actions
            or any(not isinstance(row, OfflineWaveActionV1) for row in self.actions)
        ):
            raise TypeError("actions must contain OfflineWaveActionV1 values")
        ordinals = [row.source_ordinal for row in self.actions]
        if ordinals != list(range(len(self.actions))):
            raise ValueError("source action ordinals must be contiguous")
        schedule = [(row.at_or_after_ms, row.source_order_key) for row in self.actions]
        if schedule != sorted(schedule):
            raise ValueError("source actions must preserve chronological order")
        for label, values, lane in (
            ("tail_gcd_priority", self.tail_gcd_priority, LANE_GCD),
            ("tail_queue_priority", self.tail_queue_priority, LANE_QUEUE),
            ("tail_off_gcd_once", self.tail_off_gcd_once, LANE_OFF_GCD),
        ):
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise TypeError(f"{label} must be a unique tuple")
            for key in values:
                if key not in _ACTION_REF_BY_KEY or _LANE_BY_KEY.get(key) != lane:
                    raise ValueError(f"{label} contains unsupported action {key!r}")
        if not self.tail_gcd_priority:
            raise ValueError("an independent complete-wave policy needs a GCD tail")

    def to_dict(self) -> JSONMap:
        all_actions_joined = all(
            row.build_segment_ref is not None for row in self.actions
        )
        one_segment_for_all_actions = (
            all_actions_joined and len(self.build_segment_refs) == 1
        )
        return {
            "schema": MODE_SCHEMA,
            "scope": SCOPE,
            "policy_id": self.policy_id,
            "mode_id": self.mode_id,
            "source": {
                "instance_id": self.source_instance_id,
                "episode_id": self.source_episode_id,
                "wave_id": self.source_wave_id,
                "player_guid": self.source_player_guid,
                "player_name": self.source_player_name,
                "wave_ordinal": self.source_wave_ordinal,
                "encounter_name": self.encounter_name,
            },
            "wave_evidence": {
                "observed_duration_ms": self.observed_duration_ms,
                "complete_wave_coverage": self.complete_wave_coverage,
                "observed_target_count": self.observed_target_count,
                "source_target_guids": list(self.source_target_guids),
            },
            "build_binding": {
                "segment_refs": list(self.build_segment_refs),
                "status": (
                    "NOT_JOINED"
                    if not self.build_segment_refs
                    else (
                        "SINGLE_CAUSAL_SEGMENT"
                        if one_segment_for_all_actions
                        else (
                            "PARTIALLY_JOINED"
                            if not all_actions_joined
                            else "MULTIPLE_CAUSAL_SEGMENTS"
                        )
                    )
                ),
                "all_executable_actions_joined_to_segment": all_actions_joined,
                "single_exact_segment_for_all_actions": one_segment_for_all_actions,
                "same_build_comparison_authorized": False,
            },
            "actions": [row.to_dict() for row in self.actions],
            "unresolved_start_evidence": [
                deepcopy(row) for row in self.unresolved_start_evidence
            ],
            "source_derived_tail": {
                "gcd_priority": list(self.tail_gcd_priority),
                "queue_priority": list(self.tail_queue_priority),
                "off_gcd_once": list(self.tail_off_gcd_once),
            },
            "contract": {
                "cat_required_for_supported_wave": False,
                "complete_supported_wave_control": True,
                "source_modes_preserved_not_globally_averaged": True,
                "action_repeat_skip_delay_supported": True,
                "target_death_retarget_supported": True,
                "native_legality_feedback_supported": True,
                "client_queue_intent_recovered": False,
                "idle_gap_labeled_as_intentional_wait": False,
                "comparison_authorized": False,
                "deployment_authorized": False,
            },
        }


def _canonical_action(
    observed: Mapping[str, Any],
) -> tuple[str, ActionRef, str, str] | None:
    phase = observed.get("phase")
    if phase != "START":
        return None
    key = observed.get("action_key")
    if isinstance(key, str) and key in _ACTION_REF_BY_KEY:
        return (
            key,
            _ACTION_REF_BY_KEY[key],
            _LANE_BY_KEY[key],
            EVIDENCE_KNOWN_ONTOLOGY_START,
        )
    spell = observed.get("spell")
    spell_id = spell.get("id") if isinstance(spell, Mapping) else None
    if isinstance(spell_id, int) and not isinstance(spell_id, bool):
        extended = _EXTENDED_START_BY_SPELL_ID.get(spell_id)
        if extended is not None:
            return (
                extended,
                _ACTION_REF_BY_KEY[extended],
                _LANE_BY_KEY[extended],
                EVIDENCE_EXTENDED_ONTOLOGY_START,
            )
    payload = observed.get("action_payload")
    item_id = payload.get("item_id") if isinstance(payload, Mapping) else None
    if isinstance(item_id, int) and not isinstance(item_id, bool) and item_id > 0:
        return (
            f"item.{item_id}",
            ActionRef(item_id=item_id),
            LANE_OFF_GCD,
            EVIDENCE_EXACT_ITEM_START,
        )
    if isinstance(spell_id, int) and not isinstance(spell_id, bool):
        actuator = _STATIC_ACTUATOR_BY_EFFECT_SPELL_ID.get(spell_id)
        if actuator is not None:
            return (
                actuator,
                _ACTION_REF_BY_KEY[actuator],
                _LANE_BY_KEY[actuator],
                EVIDENCE_STATIC_ACTUATOR_START,
            )
    return None


def _target_role(
    observed: Mapping[str, Any],
    *,
    player_guid: str,
    last_hostile_guid: str | None,
    target_ordinals: dict[str, int],
    target_guids: list[str],
) -> tuple[str, int | None, str | None]:
    target = observed.get("exact_target")
    if not isinstance(target, Mapping):
        return TARGET_NO_EXPLICIT, None, last_hostile_guid
    guid = target.get("guid")
    lane = target.get("lane")
    if not isinstance(guid, str) or not guid:
        return TARGET_NO_EXPLICIT, None, last_hostile_guid
    if guid.casefold() == player_guid.casefold() or lane == "FRIENDLY_PLAYER":
        return TARGET_SELF, None, last_hostile_guid
    if target.get("voting_enemy_target") is not True or lane != "HOSTILE_CREATURE":
        return TARGET_NO_EXPLICIT, None, last_hostile_guid
    normalized_guid = guid.casefold()
    ordinal = target_ordinals.get(normalized_guid)
    if ordinal is None:
        ordinal = len(target_ordinals)
        target_ordinals[normalized_guid] = ordinal
        target_guids.append(guid)
    role = (
        TARGET_CURRENT
        if last_hostile_guid is None or last_hostile_guid.casefold() == guid.casefold()
        else TARGET_OTHER
    )
    return role, ordinal, guid


def _build_ref_by_order_key(
    build_mapping: Mapping[str, Any] | None,
) -> dict[tuple[int, ...], str]:
    if build_mapping is None:
        return {}
    if build_mapping.get("schema") != KNOWN_BUILD_MAPPING_SCHEMA:
        raise OfflineWavePolicyV1Error("unsupported build mapping schema")
    result: dict[tuple[int, ...], str] = {}
    for row in _array(build_mapping.get("decision_bindings"), "decision_bindings"):
        binding = _mapping(row, "decision binding")
        order = binding.get("order_key")
        segment = binding.get("segment_ref")
        if (
            isinstance(order, list)
            and order
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in order
            )
            and isinstance(segment, str)
            and segment
        ):
            result[tuple(order)] = segment
    return result


def _priority_from_source(
    actions: Sequence[OfflineWaveActionV1], lane: str
) -> tuple[str, ...]:
    selected = [row.action_key for row in actions if row.lane == lane]
    counts = Counter(selected)
    first = {key: selected.index(key) for key in counts}
    return tuple(sorted(counts, key=lambda key: (-counts[key], first[key], key)))


def compile_offline_wave_feedback_policy_v1(
    episode: Mapping[str, Any],
    *,
    wave_index: int = 0,
    build_mapping: Mapping[str, Any] | None = None,
    policy_id: str | None = None,
) -> OfflineWaveFeedbackPolicyV1:
    """Compile one exact player-wave observation into an independent mode."""

    schema = episode.get("schema")
    if schema not in {KNOWN_EPISODE_SCHEMA, KNOWN_WAVE_RECORD_SCHEMA}:
        raise OfflineWavePolicyV1Error(f"unsupported episode schema {schema!r}")
    player = _mapping(episode.get("player"), "episode.player")
    player_guid = _nonempty(player.get("guid"), "player.guid")
    player_name = player.get("name") or player.get("timeline_name")
    if player_name is not None and not isinstance(player_name, str):
        raise OfflineWavePolicyV1Error("player name must be text or null")

    if schema == KNOWN_EPISODE_SCHEMA:
        waves = _array(episode.get("wave_observations"), "wave_observations")
        if wave_index < 0 or wave_index >= len(waves):
            raise OfflineWavePolicyV1Error("wave_index is outside episode")
        wave = _mapping(waves[wave_index], "wave observation")
        dps_window = _mapping(episode.get("exact_dps_window"), "exact_dps_window")
        encounter_name = _nonempty(dps_window.get("encounter_name"), "encounter_name")
        coverage = _mapping(
            _mapping(episode.get("window_join"), "window_join").get("coverage"),
            "window_join.coverage",
        )
        complete_coverage = coverage.get("partial") is False
        episode_id = _nonempty(episode.get("episode_id"), "episode_id")
    else:
        if wave_index != 0:
            raise OfflineWavePolicyV1Error(
                "single-wave record only supports wave_index=0"
            )
        wave = episode
        encounter_name = _nonempty(
            episode.get("encounter_name")
            or str(episode.get("encounter_id") or "Trash"),
            "encounter_name",
        )
        complete_coverage = True
        episode_id = _nonempty(episode.get("episode_id"), "episode_id")

    transitions = _array(wave.get("prefix_transitions"), "prefix_transitions")
    source_wave_id = _nonempty(wave.get("wave_id"), "wave_id")
    source_wave_ordinal = _nonnegative_int(wave.get("wave_ordinal", 0), "wave_ordinal")
    window = _mapping(wave.get("window"), "wave.window")
    duration = window.get("boundary_duration_ms")
    if duration is None:
        duration = window.get("context_duration_ms")
    duration = _nonnegative_int(duration, "wave duration")
    if duration <= 0:
        raise OfflineWavePolicyV1Error("wave duration must be positive")

    build_refs = _build_ref_by_order_key(build_mapping)
    target_ordinals: dict[str, int] = {}
    target_guids: list[str] = []
    last_hostile_guid: str | None = None
    actions: list[OfflineWaveActionV1] = []
    unresolved: list[JSONMap] = []

    for transition in transitions:
        row = _mapping(transition, "prefix transition")
        observed = _mapping(row.get("observed_event"), "observed_event")
        if observed.get("phase") != "START":
            continue
        canonical = _canonical_action(observed)
        if canonical is None:
            spell = observed.get("spell")
            payload = observed.get("action_payload")
            unresolved.append(
                {
                    "source_order_key": deepcopy(observed.get("order_key")),
                    "source_action_key": observed.get("action_key"),
                    "spell_id": spell.get("id") if isinstance(spell, Mapping) else None,
                    "spell_name": (
                        spell.get("name") if isinstance(spell, Mapping) else None
                    ),
                    "item_id": (
                        payload.get("item_id") if isinstance(payload, Mapping) else None
                    ),
                    "status": "OBSERVED_START_NOT_EXECUTABLE_WITH_CURRENT_REGISTRY",
                }
            )
            continue
        action_key, ref, lane, evidence = canonical
        order_raw = observed.get("order_key")
        if (
            not isinstance(order_raw, list)
            or not order_raw
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in order_raw
            )
        ):
            raise OfflineWavePolicyV1Error("START action lacks a valid order_key")
        order = tuple(order_raw)
        state_before = _mapping(row.get("state_before"), "state_before")
        elapsed = _nonnegative_int(
            state_before.get("wave_elapsed_ms"), "wave_elapsed_ms"
        )
        role, target_ordinal, last_hostile_guid = _target_role(
            observed,
            player_guid=player_guid,
            last_hostile_guid=last_hostile_guid,
            target_ordinals=target_ordinals,
            target_guids=target_guids,
        )
        resource = _FINITE_RESOURCE_BY_KEY.get(action_key)
        if ref.item_id > 0:
            resource = f"item.{ref.item_id}"
            _ACTION_REF_BY_KEY.setdefault(action_key, ref)
            _LANE_BY_KEY.setdefault(action_key, lane)
        actions.append(
            OfflineWaveActionV1(
                source_ordinal=len(actions),
                at_or_after_ms=elapsed,
                action_key=action_key,
                action_ref=ref,
                lane=lane,
                target_role=role,
                source_target_ordinal=target_ordinal,
                source_order_key=order,
                evidence_status=evidence,
                build_segment_ref=build_refs.get(order),
                resource_id=resource,
            )
        )

    if not actions:
        raise OfflineWavePolicyV1Error("wave has no executable observed START actions")
    gcd_priority = _priority_from_source(actions, LANE_GCD)
    if not gcd_priority:
        raise OfflineWavePolicyV1Error(
            "wave has no source-observed executable GCD for an independent tail"
        )
    segment_refs = tuple(
        dict.fromkeys(
            row.build_segment_ref
            for row in actions
            if row.build_segment_ref is not None
        )
    )
    instance_id = _nonempty(episode.get("instance_id"), "instance_id")
    mode_id = f"{instance_id}:{episode_id}:{source_wave_id}"
    return OfflineWaveFeedbackPolicyV1(
        policy_id=(policy_id or f"chronicle.pi_d.{mode_id}"),
        mode_id=mode_id,
        encounter_name=encounter_name,
        source_instance_id=instance_id,
        source_episode_id=episode_id,
        source_wave_id=source_wave_id,
        source_player_guid=player_guid,
        source_player_name=player_name,
        source_wave_ordinal=source_wave_ordinal,
        observed_duration_ms=duration,
        complete_wave_coverage=complete_coverage,
        observed_target_count=len(target_ordinals),
        source_target_guids=tuple(target_guids),
        build_segment_refs=segment_refs,
        actions=tuple(actions),
        unresolved_start_evidence=tuple(unresolved),
        tail_gcd_priority=gcd_priority,
        tail_queue_priority=_priority_from_source(actions, LANE_QUEUE),
        tail_off_gcd_once=_priority_from_source(actions, LANE_OFF_GCD),
    )


@dataclass(frozen=True)
class OfflineWaveRuntimeBindingV1:
    runtime_wave_id: str
    source_target_guids: tuple[str, ...]
    target_indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "runtime_wave_id",
            _nonempty(self.runtime_wave_id, "runtime_wave_id"),
        )
        if (
            not isinstance(self.source_target_guids, tuple)
            or not self.source_target_guids
        ):
            raise ValueError("source_target_guids must be a nonempty tuple")
        source_guids = tuple(
            _nonempty(value, "source_target_guid") for value in self.source_target_guids
        )
        if len({value.casefold() for value in source_guids}) != len(source_guids):
            raise ValueError("source_target_guids must be unique")
        object.__setattr__(self, "source_target_guids", source_guids)
        if (
            not isinstance(self.target_indexes, tuple)
            or not self.target_indexes
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.target_indexes
            )
            or len(set(self.target_indexes)) != len(self.target_indexes)
        ):
            raise ValueError("target_indexes must be unique nonnegative integers")
        if len(self.source_target_guids) != len(self.target_indexes):
            raise ValueError(
                "source_target_guids and target_indexes must be one-to-one"
            )

    def to_dict(self) -> JSONMap:
        return {
            "schema": RUNTIME_BINDING_SCHEMA,
            "runtime_wave_id": self.runtime_wave_id,
            "source_target_guids": list(self.source_target_guids),
            "target_indexes": list(self.target_indexes),
            "target_bindings": [
                {"source_guid": guid, "runtime_target_index": index}
                for guid, index in zip(
                    self.source_target_guids, self.target_indexes, strict=True
                )
            ],
        }


@dataclass(frozen=True)
class _PendingExecutionV1:
    decision: ProgramDecisionV1
    source_ordinal: int | None
    expected_action: ActionRef | None
    expected_lane: str | None
    resource_actions: tuple[tuple[str, ActionRef], ...]
    kind: str


class OfflineWaveFeedbackSessionV1:
    """One fresh-seed causal controller for a complete supported wave."""

    def __init__(
        self,
        policy: OfflineWaveFeedbackPolicyV1,
        runtime_binding: OfflineWaveRuntimeBindingV1,
        *,
        domain_fallback: (
            Callable[
                [CausalLiveStateProjectionV1, tuple[AvailableAction, ...]],
                ProgramDecisionV1,
            ]
            | None
        ) = None,
    ) -> None:
        if not isinstance(policy, OfflineWaveFeedbackPolicyV1):
            raise TypeError("policy must be OfflineWaveFeedbackPolicyV1")
        if not isinstance(runtime_binding, OfflineWaveRuntimeBindingV1):
            raise TypeError("runtime_binding must be OfflineWaveRuntimeBindingV1")
        if domain_fallback is not None and not callable(domain_fallback):
            raise TypeError("domain_fallback must be callable or None")
        if tuple(
            value.casefold() for value in runtime_binding.source_target_guids
        ) != tuple(value.casefold() for value in policy.source_target_guids):
            raise OfflineWavePolicyV1Error(
                "runtime target binding does not match source target GUID order"
            )
        self.policy = policy
        self.runtime_binding = runtime_binding
        self._domain_fallback = domain_fallback
        self._committed: set[int] = set()
        self._permanently_masked: set[int] = set()
        self._used_resources: set[str] = set()
        self._wave_started_at_ms: int | None = None
        self._pending: _PendingExecutionV1 | None = None
        self._audit: list[JSONMap] = []
        self._domain_fallback_calls = 0

    @property
    def committed_source_ordinals(self) -> tuple[int, ...]:
        return tuple(sorted(self._committed))

    @property
    def domain_fallback_calls(self) -> int:
        return self._domain_fallback_calls

    @property
    def audit_events(self) -> tuple[JSONMap, ...]:
        return tuple(deepcopy(row) for row in self._audit)

    def _record(
        self,
        kind: str,
        observation: CausalLiveStateProjectionV1,
        **values: Any,
    ) -> None:
        self._audit.append(
            {
                "kind": kind,
                "state_time_ms": observation.visibility_cutoff_ms,
                **values,
            }
        )

    def record_last_executed_decision_v1(
        self, actual_decision: ProgramDecisionV1
    ) -> None:
        """Legacy feedback cannot prove that a proposed action actually ran."""

        self.record_last_execution_receipt_v1(actual_decision, ())

    def record_last_execution_receipt_v1(
        self,
        actual_decision: ProgramDecisionV1,
        execution_receipts: Sequence[Mapping[str, Any]],
    ) -> None:
        """Commit source progress only for an action-level bridge receipt."""

        if not isinstance(actual_decision, ProgramDecisionV1):
            raise TypeError("actual_decision must be ProgramDecisionV1")
        if not isinstance(execution_receipts, Sequence) or isinstance(
            execution_receipts, (str, bytes, bytearray)
        ):
            raise TypeError("execution_receipts must be a sequence")
        pending = self._pending
        if pending is None:
            raise OfflineWavePolicyV1Error(
                "execution feedback has no pending offline-wave proposal"
            )
        self._pending = None
        if actual_decision != pending.decision:
            self._audit.append(
                {
                    "kind": "OFFLINE_PROPOSAL_REPLACED_BEFORE_EXECUTION",
                    "source_ordinal": pending.source_ordinal,
                    "proposal_kind": pending.kind,
                }
            )
            return

        accepted_by_action: dict[ActionRef, list[JSONMap]] = {}
        accepted_actions: list[JSONMap] = []
        accepted_kinds = {
            "OPTIONAL_OFF_GCD_EXECUTED",
            "QUEUE_SET",
            "TERMINAL_GCD",
        }
        terminal_wait = False
        for raw_receipt in execution_receipts:
            receipt = _mapping(raw_receipt, "execution receipt")
            kind = receipt.get("kind")
            if kind in {
                "TERMINAL_WAIT",
                "TERMINAL_WAIT_EXTERNAL_PRESS_ABSTAIN",
            }:
                terminal_wait = True
                continue
            if kind not in accepted_kinds:
                continue
            action_wire = receipt.get("action")
            if not isinstance(action_wire, Mapping):
                raise OfflineWavePolicyV1Error(
                    f"{kind} receipt lacks an exact action identity"
                )
            action = ActionRef.from_wire(action_wire)
            normalized = {
                "kind": kind,
                "action": action.to_wire(),
                "state_time_ms": receipt.get("state_time_ms"),
                "target_index": actual_decision.target_index,
            }
            accepted_actions.append(normalized)
            accepted_by_action.setdefault(action, []).append(normalized)

        expected_kind = {
            LANE_GCD: "TERMINAL_GCD",
            LANE_QUEUE: "QUEUE_SET",
            LANE_OFF_GCD: "OPTIONAL_OFF_GCD_EXECUTED",
        }.get(pending.expected_lane)
        expected_receipts = accepted_by_action.get(pending.expected_action, [])
        expected_executed = (
            pending.expected_action is None
            and terminal_wait
            or pending.expected_action is not None
            and any(row["kind"] == expected_kind for row in expected_receipts)
        )
        executed_resources = [
            resource_id
            for resource_id, action in pending.resource_actions
            if action in accepted_by_action
        ]
        self._used_resources.update(executed_resources)
        if not expected_executed:
            self._audit.append(
                {
                    "kind": "OFFLINE_PROPOSAL_ACTION_NOT_EXECUTED",
                    "source_ordinal": pending.source_ordinal,
                    "proposal_kind": pending.kind,
                    "expected_action": (
                        pending.expected_action.to_wire()
                        if pending.expected_action is not None
                        else None
                    ),
                    "expected_lane": pending.expected_lane,
                    "observed_receipt_kinds": [
                        str(row.get("kind")) for row in execution_receipts
                    ],
                    "resource_ids": executed_resources,
                    "accepted_actions": accepted_actions,
                    "accepted_target_index": actual_decision.target_index,
                }
            )
            return

        if pending.source_ordinal is not None:
            self._committed.add(pending.source_ordinal)
        self._audit.append(
            {
                "kind": "OFFLINE_PROPOSAL_EXECUTION_CONFIRMED",
                "source_ordinal": pending.source_ordinal,
                "proposal_kind": pending.kind,
                "resource_ids": executed_resources,
                "accepted_actions": accepted_actions,
                "accepted_target_index": actual_decision.target_index,
            }
        )

    def reject_last_execution_v1(self, reason: str) -> None:
        reason = _nonempty(reason, "execution rejection reason")
        if self._pending is None:
            return
        pending = self._pending
        self._pending = None
        self._audit.append(
            {
                "kind": "OFFLINE_PROPOSAL_EXECUTION_REJECTED",
                "source_ordinal": pending.source_ordinal,
                "proposal_kind": pending.kind,
                "reason": reason,
            }
        )

    def _visible_targets(
        self, observation: CausalLiveStateProjectionV1
    ) -> tuple[Mapping[str, Any], ...]:
        semantics = observation.state.get("dynamic_target_semantics")
        rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
        if not isinstance(rows, list):
            raise OfflineWavePolicyV1Error(
                "causal observation lacks visible target semantics"
            )
        by_index: dict[int, Mapping[str, Any]] = {}
        for row in rows:
            target = _mapping(row, "visible target")
            index = target.get("target_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise OfflineWavePolicyV1Error(
                    "visible target lacks a nonnegative target_index"
                )
            if index in by_index:
                raise OfflineWavePolicyV1Error("visible targets repeat target_index")
            by_index[index] = target
        return tuple(
            by_index[index]
            for index in self.runtime_binding.target_indexes
            if index in by_index
        )

    @staticmethod
    def _active_target_indexes(
        rows: Iterable[Mapping[str, Any]],
    ) -> tuple[int, ...]:
        return tuple(
            int(row["target_index"])
            for row in rows
            if row.get("attackable") is True and row.get("dead") is not True
        )

    def _fallback(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
        reason: str,
    ) -> ProgramDecisionV1:
        self._domain_fallback_calls += 1
        self._record("DOMAIN_FALLBACK", observation, reason=reason)
        if self._domain_fallback is None:
            raise OfflineWavePolicyV1Error(
                f"observation is outside supported wave: {reason}"
            )
        decision = self._domain_fallback(observation, available)
        if not isinstance(decision, ProgramDecisionV1):
            raise TypeError("domain_fallback must return ProgramDecisionV1")
        return decision

    def _target_index(
        self,
        action: OfflineWaveActionV1 | None,
        observation: CausalLiveStateProjectionV1,
        active: tuple[int, ...],
    ) -> int:
        if not active:
            raise OfflineWavePolicyV1Error("supported wave has no active target")
        current = observation.state.get("target_index")
        current = (
            current
            if isinstance(current, int)
            and not isinstance(current, bool)
            and current in active
            else None
        )
        if action is None or action.target_role in {
            TARGET_NO_EXPLICIT,
            TARGET_SELF,
            TARGET_CURRENT,
        }:
            if action is not None and action.source_target_ordinal is not None:
                if action.source_target_ordinal >= len(
                    self.runtime_binding.target_indexes
                ):
                    raise OfflineWavePolicyV1Error(
                        "source target ordinal has no explicit runtime binding"
                    )
                desired = self.runtime_binding.target_indexes[
                    action.source_target_ordinal
                ]
                if desired in active:
                    return desired
            return current if current is not None else active[0]
        if action.source_target_ordinal is not None:
            if action.source_target_ordinal >= len(self.runtime_binding.target_indexes):
                raise OfflineWavePolicyV1Error(
                    "source target ordinal has no explicit runtime binding"
                )
            desired = self.runtime_binding.target_indexes[action.source_target_ordinal]
            if desired in active:
                return desired
        for index in active:
            if index != current:
                return index
        return active[0]

    def _wait_decision(
        self,
        *,
        observation: CausalLiveStateProjectionV1,
        target_index: int,
        wait_ms: int,
        reason: str,
    ) -> ProgramDecisionV1:
        delay = max(1, min(100, wait_ms))
        decision = ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            wait_ms=delay,
        )
        self._pending = _PendingExecutionV1(
            decision=decision,
            source_ordinal=None,
            expected_action=None,
            expected_lane=None,
            resource_actions=(),
            kind="WAIT",
        )
        self._record("OFFLINE_FEEDBACK_WAIT", observation, reason=reason, wait_ms=delay)
        return decision

    def _decision_for_action(
        self,
        action: OfflineWaveActionV1,
        *,
        target_index: int,
    ) -> ProgramDecisionV1:
        prefix = (
            ProgramPrefixOperationKindV1.SET_TARGET,
            ProgramPrefixOperationKindV1.START_ATTACK,
        )
        if action.lane == LANE_GCD:
            return ProgramDecisionV1(
                target_index=target_index,
                start_attack=True,
                gcd_action=action.action_ref,
                prefix_order=(
                    *prefix,
                    ProgramPrefixOperationKindV1.QUEUE_KEEP,
                ),
            )
        if action.lane == LANE_QUEUE:
            return ProgramDecisionV1(
                target_index=target_index,
                start_attack=True,
                queue_op=QueueLaneOp.SET,
                queue_action=action.action_ref,
                wait_ms=1,
                prefix_order=(
                    *prefix,
                    ProgramPrefixOperationKindV1.QUEUE_SET,
                ),
            )
        guard = ObservableCausalGuardV1(
            target_index=target_index,
            target_attackable_is=True,
            action_ready=action.action_ref,
            false_semantics=SKIP_PLAN,
        )
        return ProgramDecisionV1(
            target_index=target_index,
            start_attack=True,
            optional_off_gcd_prefixes=(
                OptionalOffGcdPrefixV1(action.action_ref, guard),
            ),
            wait_ms=1,
            prefix_order=(
                *prefix,
                ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD,
                ProgramPrefixOperationKindV1.QUEUE_KEEP,
            ),
        )

    def _tail_decision(
        self,
        *,
        observation: CausalLiveStateProjectionV1,
        available_by_action: Mapping[ActionRef, AvailableAction],
        active: tuple[int, ...],
    ) -> ProgramDecisionV1:
        target = self._target_index(None, observation, active)
        gcd_key = next(
            (
                key
                for key in self.policy.tail_gcd_priority
                if (
                    (row := available_by_action.get(_ACTION_REF_BY_KEY[key]))
                    is not None
                    and row.legal
                    and row.ready_in_ms == 0
                )
            ),
            None,
        )
        off_key = next(
            (
                key
                for key in self.policy.tail_off_gcd_once
                if _FINITE_RESOURCE_BY_KEY.get(key, f"action.{key}")
                not in self._used_resources
                and (
                    (row := available_by_action.get(_ACTION_REF_BY_KEY[key]))
                    is not None
                    and row.legal
                    and row.ready_in_ms == 0
                )
            ),
            None,
        )
        queue_key = next(
            (
                key
                for key in self.policy.tail_queue_priority
                if (
                    (row := available_by_action.get(_ACTION_REF_BY_KEY[key]))
                    is not None
                    and row.legal
                    and row.ready_in_ms == 0
                )
            ),
            None,
        )
        if gcd_key is None and off_key is None:
            waits = [
                row.ready_in_ms
                for key in self.policy.tail_gcd_priority
                if (row := available_by_action.get(_ACTION_REF_BY_KEY[key])) is not None
                and row.ready_in_ms > 0
            ]
            return self._wait_decision(
                observation=observation,
                target_index=target,
                wait_ms=min(waits, default=100),
                reason="NO_SOURCE_DERIVED_TAIL_ACTION_READY",
            )

        prefixes: tuple[OptionalOffGcdPrefixV1, ...] = ()
        resources: tuple[str, ...] = ()
        if off_key is not None:
            off_ref = _ACTION_REF_BY_KEY[off_key]
            prefixes = (
                OptionalOffGcdPrefixV1(
                    off_ref,
                    ObservableCausalGuardV1(
                        target_index=target,
                        target_attackable_is=True,
                        action_ready=off_ref,
                        false_semantics=SKIP_PLAN,
                    ),
                ),
            )
            resources = (_FINITE_RESOURCE_BY_KEY.get(off_key, f"action.{off_key}"),)
        queue_op = QueueLaneOp.SET if queue_key is not None else QueueLaneOp.KEEP
        queue_ref = _ACTION_REF_BY_KEY[queue_key] if queue_key is not None else None
        gcd_ref = _ACTION_REF_BY_KEY[gcd_key] if gcd_key is not None else None
        kinds = [
            ProgramPrefixOperationKindV1.SET_TARGET,
            ProgramPrefixOperationKindV1.START_ATTACK,
        ]
        kinds.extend(ProgramPrefixOperationKindV1.OPTIONAL_OFF_GCD for _ in prefixes)
        kinds.append(
            ProgramPrefixOperationKindV1.QUEUE_SET
            if queue_op is QueueLaneOp.SET
            else ProgramPrefixOperationKindV1.QUEUE_KEEP
        )
        decision = ProgramDecisionV1(
            target_index=target,
            start_attack=True,
            optional_off_gcd_prefixes=prefixes,
            queue_op=queue_op,
            queue_action=queue_ref,
            gcd_action=gcd_ref,
            wait_ms=None if gcd_ref is not None else 1,
            prefix_order=tuple(kinds),
        )
        self._pending = _PendingExecutionV1(
            decision=decision,
            source_ordinal=None,
            expected_action=(gcd_ref or queue_ref or (off_ref if off_key else None)),
            expected_lane=(
                LANE_GCD
                if gcd_ref is not None
                else (
                    LANE_QUEUE
                    if queue_ref is not None
                    else LANE_OFF_GCD if off_key is not None else None
                )
            ),
            resource_actions=(
                ((resources[0], off_ref),) if off_key is not None else ()
            ),
            kind="SOURCE_DERIVED_TAIL",
        )
        self._record(
            "SOURCE_DERIVED_TAIL_SELECTED",
            observation,
            gcd_action=gcd_key,
            queue_action=queue_key,
            off_gcd_action=off_key,
            target_index=target,
        )
        return decision

    def _source_action_deadline_ms(self, action: OfflineWaveActionV1) -> int:
        """Bound retries without allowing a later source action to bypass it."""

        deadline = min(
            self.policy.observed_duration_ms,
            action.at_or_after_ms + 2_000,
        )
        next_ordinal = action.source_ordinal + 1
        if next_ordinal < len(self.policy.actions):
            deadline = min(
                deadline,
                self.policy.actions[next_ordinal].at_or_after_ms,
            )
        return max(action.at_or_after_ms, deadline)

    def __call__(
        self,
        observation: CausalLiveStateProjectionV1,
        available: tuple[AvailableAction, ...],
    ) -> ProgramDecisionV1:
        if not isinstance(observation, CausalLiveStateProjectionV1):
            raise TypeError("offline policy requires CausalLiveStateProjectionV1")
        if not isinstance(available, tuple) or any(
            not isinstance(row, AvailableAction) for row in available
        ):
            raise TypeError("available must contain AvailableAction values")
        if observation.state.get("time_ms") != observation.visibility_cutoff_ms:
            raise OfflineWavePolicyV1Error(
                "causal observation time differs from visibility cutoff"
            )
        if self._pending is not None:
            self.reject_last_execution_v1(
                "NEXT_DECISION_WITHOUT_EXECUTION_CONFIRMATION"
            )
        rows = self._visible_targets(observation)
        active = self._active_target_indexes(rows)
        precombat = observation.state.get("precombat")
        if not active:
            if isinstance(precombat, Mapping) and precombat.get("active") is True:
                target = self.runtime_binding.target_indexes[0]
                return self._wait_decision(
                    observation=observation,
                    target_index=target,
                    wait_ms=100,
                    reason="SUPPORTED_WAVE_PRECOMBAT",
                )
            if rows and all(row.get("dead") is True for row in rows):
                # Normal replays terminate before asking for another decision.
                return self._wait_decision(
                    observation=observation,
                    target_index=self.runtime_binding.target_indexes[0],
                    wait_ms=1,
                    reason="SUPPORTED_WAVE_COMPLETE",
                )
            return self._fallback(observation, available, "NO_SUPPORTED_ACTIVE_TARGET")

        if self._wave_started_at_ms is None:
            self._wave_started_at_ms = observation.visibility_cutoff_ms
        elapsed = observation.visibility_cutoff_ms - self._wave_started_at_ms
        by_action = {row.action: row for row in available}
        if len(by_action) != len(available):
            raise OfflineWavePolicyV1Error(
                "native action surface contains duplicate identities"
            )

        while True:
            remaining = [
                row
                for row in self.policy.actions
                if row.source_ordinal not in self._committed
                and row.source_ordinal not in self._permanently_masked
            ]
            if not remaining:
                return self._tail_decision(
                    observation=observation,
                    available_by_action=by_action,
                    active=active,
                )
            action = remaining[0]
            if action.at_or_after_ms > elapsed:
                target = self._target_index(action, observation, active)
                return self._wait_decision(
                    observation=observation,
                    target_index=target,
                    wait_ms=action.at_or_after_ms - elapsed,
                    reason="NEXT_OBSERVED_START_SCHEDULE_NOT_REACHED",
                )

            native = by_action.get(action.action_ref)
            if native is None:
                self._permanently_masked.add(action.source_ordinal)
                self._record(
                    "SOURCE_ACTION_MASKED_BY_EXACT_NATIVE_SURFACE",
                    observation,
                    source_ordinal=action.source_ordinal,
                    action_key=action.action_key,
                )
                continue
            if not native.legal or native.ready_in_ms != 0:
                deadline = self._source_action_deadline_ms(action)
                if elapsed >= deadline:
                    self._permanently_masked.add(action.source_ordinal)
                    self._record(
                        "SOURCE_ACTION_MASKED_AFTER_EXECUTION_WINDOW",
                        observation,
                        source_ordinal=action.source_ordinal,
                        action_key=action.action_key,
                        source_at_or_after_ms=action.at_or_after_ms,
                        source_deadline_ms=deadline,
                        runtime_elapsed_ms=elapsed,
                        native_legal=native.legal,
                        native_ready_in_ms=native.ready_in_ms,
                    )
                    continue
                target = self._target_index(action, observation, active)
                ready_wait = max(1, native.ready_in_ms or 100)
                return self._wait_decision(
                    observation=observation,
                    target_index=target,
                    wait_ms=min(ready_wait, deadline - elapsed),
                    reason="EARLIEST_SOURCE_ACTION_TRANSIENTLY_NOT_READY",
                )

            selected = action
            target = self._target_index(selected, observation, active)
            decision = self._decision_for_action(selected, target_index=target)
            resources = (
                (selected.resource_id,) if selected.resource_id is not None else ()
            )
            self._pending = _PendingExecutionV1(
                decision=decision,
                source_ordinal=selected.source_ordinal,
                expected_action=selected.action_ref,
                expected_lane=selected.lane,
                resource_actions=(
                    ((resources[0], selected.action_ref),) if resources else ()
                ),
                kind="SOURCE_DEMONSTRATION_ACTION",
            )
            self._record(
                "SOURCE_DEMONSTRATION_ACTION_SELECTED",
                observation,
                source_ordinal=selected.source_ordinal,
                action_key=selected.action_key,
                source_at_or_after_ms=selected.at_or_after_ms,
                runtime_elapsed_ms=elapsed,
                target_index=target,
            )
            return decision


def build_offline_wave_policy_runtime_v1(
    policy: OfflineWaveFeedbackPolicyV1,
    runtime_binding: OfflineWaveRuntimeBindingV1,
    *,
    domain_fallback_factory: (
        Callable[[], Callable[..., ProgramDecisionV1]] | None
    ) = None,
) -> tuple[CausalActionProgramV1, ImportedReactiveProgramBindingV1]:
    """Bind one source mode without making Cat a structural dependency."""

    if not isinstance(policy, OfflineWaveFeedbackPolicyV1):
        raise TypeError("policy must be OfflineWaveFeedbackPolicyV1")
    if not isinstance(runtime_binding, OfflineWaveRuntimeBindingV1):
        raise TypeError("runtime_binding must be OfflineWaveRuntimeBindingV1")
    if domain_fallback_factory is not None and not callable(domain_fallback_factory):
        raise TypeError("domain_fallback_factory must be callable or None")
    if tuple(
        value.casefold() for value in runtime_binding.source_target_guids
    ) != tuple(value.casefold() for value in policy.source_target_guids):
        raise OfflineWavePolicyV1Error(
            "runtime target binding does not match source target GUID order"
        )
    binding_id = f"offline-wave::{policy.policy_id}::{runtime_binding.runtime_wave_id}"

    def open_session() -> OfflineWaveFeedbackSessionV1:
        fallback = (
            domain_fallback_factory() if domain_fallback_factory is not None else None
        )
        return OfflineWaveFeedbackSessionV1(
            policy, runtime_binding, domain_fallback=fallback
        )

    binding = ImportedReactiveProgramBindingV1(
        binding_id=binding_id,
        source_policy_id=policy.policy_id,
        observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        resolver_factory=open_session,
    )
    program = CausalActionProgramV1(
        program_id=policy.policy_id,
        selector=ImportedReactiveSelectorV1(
            binding_id=binding_id,
            source_policy_id=policy.policy_id,
            observation_contract_id=OBSERVATION_CONTRACT_ID_V1,
        ),
        origin=ProgramOriginV1.SOURCE_DERIVED_OFFLINE,
        source_refs=(
            MODE_SCHEMA,
            policy.source_instance_id,
            policy.source_episode_id,
            policy.source_wave_id,
        ),
    )
    return program, binding


def offline_wave_feedback_policy_from_dict_v1(
    value: Mapping[str, Any],
) -> OfflineWaveFeedbackPolicyV1:
    """Load a compact library mode back into the executable policy object."""

    raw = _mapping(value, "offline wave policy")
    if raw.get("schema") != MODE_SCHEMA:
        raise OfflineWavePolicyV1Error("unsupported offline wave policy schema")
    source = _mapping(raw.get("source"), "offline wave policy source")
    wave = _mapping(raw.get("wave_evidence"), "wave_evidence")
    build = _mapping(raw.get("build_binding"), "build_binding")
    tail = _mapping(raw.get("source_derived_tail"), "source_derived_tail")
    actions: list[OfflineWaveActionV1] = []
    for index, value in enumerate(_array(raw.get("actions"), "actions")):
        row = _mapping(value, f"actions[{index}]")
        order = _array(row.get("source_order_key"), "source_order_key")
        actions.append(
            OfflineWaveActionV1(
                source_ordinal=_nonnegative_int(
                    row.get("source_ordinal"), "source_ordinal"
                ),
                at_or_after_ms=_nonnegative_int(
                    row.get("at_or_after_ms"), "at_or_after_ms"
                ),
                action_key=_nonempty(row.get("action_key"), "action_key"),
                action_ref=ActionRef.from_wire(
                    _mapping(row.get("action_ref"), "action_ref")
                ),
                lane=_nonempty(row.get("lane"), "lane"),
                target_role=_nonempty(row.get("target_role"), "target_role"),
                source_target_ordinal=row.get("source_target_ordinal"),
                source_order_key=tuple(order),
                evidence_status=_nonempty(
                    row.get("evidence_status"), "evidence_status"
                ),
                build_segment_ref=row.get("build_segment_ref"),
                resource_id=row.get("resource_id"),
            )
        )
    # Exact item IDs can be source-specific, so restore them into the runtime
    # registry before the policy validates its source-derived tail.
    for action in actions:
        _ACTION_REF_BY_KEY.setdefault(action.action_key, action.action_ref)
        _LANE_BY_KEY.setdefault(action.action_key, action.lane)
    unresolved = tuple(
        dict(_mapping(row, "unresolved_start_evidence row"))
        for row in _array(
            raw.get("unresolved_start_evidence"),
            "unresolved_start_evidence",
        )
    )
    player_name = source.get("player_name")
    if player_name is not None and not isinstance(player_name, str):
        raise OfflineWavePolicyV1Error("source.player_name must be text or null")
    complete = wave.get("complete_wave_coverage")
    if not isinstance(complete, bool):
        raise OfflineWavePolicyV1Error(
            "wave_evidence.complete_wave_coverage must be boolean"
        )
    return OfflineWaveFeedbackPolicyV1(
        policy_id=_nonempty(raw.get("policy_id"), "policy_id"),
        mode_id=_nonempty(raw.get("mode_id"), "mode_id"),
        encounter_name=_nonempty(source.get("encounter_name"), "encounter_name"),
        source_instance_id=_nonempty(source.get("instance_id"), "instance_id"),
        source_episode_id=_nonempty(source.get("episode_id"), "episode_id"),
        source_wave_id=_nonempty(source.get("wave_id"), "wave_id"),
        source_player_guid=_nonempty(source.get("player_guid"), "player_guid"),
        source_player_name=player_name,
        source_wave_ordinal=_nonnegative_int(
            source.get("wave_ordinal"), "wave_ordinal"
        ),
        observed_duration_ms=_nonnegative_int(
            wave.get("observed_duration_ms"), "observed_duration_ms"
        ),
        complete_wave_coverage=complete,
        observed_target_count=_nonnegative_int(
            wave.get("observed_target_count"), "observed_target_count"
        ),
        source_target_guids=tuple(
            _nonempty(row, "source_target_guid")
            for row in _array(wave.get("source_target_guids"), "source_target_guids")
        ),
        build_segment_refs=tuple(
            _nonempty(row, "segment_ref")
            for row in _array(build.get("segment_refs"), "segment_refs")
        ),
        actions=tuple(actions),
        unresolved_start_evidence=unresolved,
        tail_gcd_priority=tuple(
            _nonempty(row, "tail gcd action")
            for row in _array(tail.get("gcd_priority"), "gcd_priority")
        ),
        tail_queue_priority=tuple(
            _nonempty(row, "tail queue action")
            for row in _array(tail.get("queue_priority"), "queue_priority")
        ),
        tail_off_gcd_once=tuple(
            _nonempty(row, "tail off-gcd action")
            for row in _array(tail.get("off_gcd_once"), "off_gcd_once")
        ),
    )


def offline_policy_contribution_receipt_v1(
    offline_policy: OfflineWaveFeedbackPolicyV1,
    comparison_policy: OfflineWaveFeedbackPolicyV1 | None = None,
) -> JSONMap:
    """Report behavioral content changed by offline data, not provenance IDs."""

    if not isinstance(offline_policy, OfflineWaveFeedbackPolicyV1):
        raise TypeError("offline_policy must be OfflineWaveFeedbackPolicyV1")
    if comparison_policy is not None and not isinstance(
        comparison_policy, OfflineWaveFeedbackPolicyV1
    ):
        raise TypeError("comparison_policy must be OfflineWaveFeedbackPolicyV1 or None")

    def projection(policy: OfflineWaveFeedbackPolicyV1 | None) -> JSONMap:
        if policy is None:
            return {
                "action_sequence": [],
                "action_counts": {},
                "timing_ms": [],
                "target_sequence": [],
                "resource_sequence": [],
                "tail_gcd_priority": [],
                "tail_queue_priority": [],
            }
        return {
            "action_sequence": [row.action_key for row in policy.actions],
            "action_counts": dict(
                sorted(Counter(row.action_key for row in policy.actions).items())
            ),
            "timing_ms": [row.at_or_after_ms for row in policy.actions],
            "target_sequence": [
                {
                    "role": row.target_role,
                    "source_target_ordinal": row.source_target_ordinal,
                }
                for row in policy.actions
            ],
            "resource_sequence": [
                row.resource_id for row in policy.actions if row.resource_id is not None
            ],
            "tail_gcd_priority": list(policy.tail_gcd_priority),
            "tail_queue_priority": list(policy.tail_queue_priority),
        }

    offline = projection(offline_policy)
    comparison = projection(comparison_policy)
    changed = {field: offline[field] != comparison[field] for field in offline}
    return {
        "schema": CONTRIBUTION_SCHEMA,
        "receipt_level": "COMPILED_POLICY_DEFINITION",
        "accepted_execution_compared": False,
        "offline_policy_id": offline_policy.policy_id,
        "comparison_policy_id": (
            comparison_policy.policy_id
            if comparison_policy is not None
            else "NO_OFFLINE_SOURCE_ABLATION"
        ),
        "offline_behavior": offline,
        "comparison_behavior": comparison,
        "changed_behavior_fields": [
            field for field, differs in changed.items() if differs
        ],
        "offline_data_changed_compiled_policy": any(changed.values()),
        "provenance_only_change": not any(changed.values()),
    }


def offline_policy_execution_contribution_receipt_v1(
    offline_audit_events: Sequence[Mapping[str, Any]],
    comparison_audit_events: Sequence[Mapping[str, Any]] = (),
) -> JSONMap:
    """Compare accepted bridge actions, excluding proposals that never executed."""

    def projection(events: Sequence[Mapping[str, Any]]) -> JSONMap:
        if not isinstance(events, Sequence) or isinstance(
            events, (str, bytes, bytearray)
        ):
            raise TypeError("audit events must be a sequence")
        accepted: list[JSONMap] = []
        for raw_event in events:
            event = _mapping(raw_event, "offline audit event")
            if event.get("kind") not in {
                "OFFLINE_PROPOSAL_EXECUTION_CONFIRMED",
                "OFFLINE_PROPOSAL_ACTION_NOT_EXECUTED",
            }:
                continue
            raw_actions = event.get("accepted_actions")
            if not isinstance(raw_actions, list):
                raise OfflineWavePolicyV1Error(
                    "confirmed execution audit lacks accepted_actions"
                )
            for raw_action in raw_actions:
                action = _mapping(raw_action, "accepted action")
                wire = _mapping(action.get("action"), "accepted action identity")
                ref = ActionRef.from_wire(wire)
                accepted.append(
                    {
                        "action": ref.to_wire(),
                        "receipt_kind": _nonempty(
                            action.get("kind"), "accepted receipt kind"
                        ),
                        "accepted_at_ms": action.get("state_time_ms"),
                        "target_index": action.get(
                            "target_index", event.get("accepted_target_index")
                        ),
                        "proposal_kind": event.get("proposal_kind"),
                        "source_ordinal": event.get("source_ordinal"),
                    }
                )

        def identity(row: Mapping[str, Any]) -> str:
            action = _mapping(row.get("action"), "accepted action identity")
            for key in ("spell_id", "item_id", "other_id"):
                value = action.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return f"{key}:{value}:tag:{int(action.get('tag', 0))}"
            raise OfflineWavePolicyV1Error("accepted action has no exact identity")

        action_sequence = [identity(row) for row in accepted]
        return {
            "accepted_action_sequence": action_sequence,
            "accepted_action_counts": dict(sorted(Counter(action_sequence).items())),
            "accepted_timing_ms": [row["accepted_at_ms"] for row in accepted],
            "accepted_target_sequence": [row["target_index"] for row in accepted],
            "receipt_kind_sequence": [row["receipt_kind"] for row in accepted],
            "proposal_kind_sequence": [row["proposal_kind"] for row in accepted],
            "source_ordinal_sequence": [row["source_ordinal"] for row in accepted],
            "tail_action_sequence": [
                identity(row)
                for row in accepted
                if row["proposal_kind"] == "SOURCE_DERIVED_TAIL"
            ],
        }

    offline = projection(offline_audit_events)
    comparison = projection(comparison_audit_events)
    changed = {field: offline[field] != comparison[field] for field in offline}
    return {
        "schema": EXECUTION_CONTRIBUTION_SCHEMA,
        "receipt_level": "ACCEPTED_BRIDGE_EXECUTION",
        "accepted_execution_compared": True,
        "offline_accepted_execution": offline,
        "comparison_accepted_execution": comparison,
        "changed_execution_fields": [
            field for field, differs in changed.items() if differs
        ],
        "offline_data_changed_accepted_execution": any(changed.values()),
        "provenance_only_change": not any(changed.values()),
    }


__all__ = (
    "CONTRIBUTION_SCHEMA",
    "EXECUTION_CONTRIBUTION_SCHEMA",
    "LIBRARY_SCHEMA",
    "MODE_SCHEMA",
    "OfflineWaveActionV1",
    "OfflineWaveFeedbackPolicyV1",
    "OfflineWaveFeedbackSessionV1",
    "OfflineWavePolicyV1Error",
    "OfflineWaveRuntimeBindingV1",
    "build_offline_wave_policy_runtime_v1",
    "compile_offline_wave_feedback_policy_v1",
    "offline_wave_feedback_policy_from_dict_v1",
    "offline_policy_contribution_receipt_v1",
    "offline_policy_execution_contribution_receipt_v1",
)
