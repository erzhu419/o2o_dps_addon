"""Development-only causal teammate response-model baseline.

This module consumes Chronicle External team-wave model v2 records without
turning their fixed historical teammate schedule into the proposed model.  It
compiles exact-GUID player event streams into two causal views:

* a timing state captured immediately after the actor's previous event; and
* an emission state ending strictly before the current EventMeta row.

The first view trains a semi-Markov inter-event clock.  The second trains the
event mark, target choice, and damage heads at the sampled emission time.  A
simulator can therefore let candidate and other-team events change the live
prefix before choosing a teammate's next mark/damage.  Target deaths truncate
the generated team clock and force live retargeting rather than replaying a
historical completion time.

The artifact is deliberately a development baseline.  Old-50 selected
instances are forced into train, disconnected identity components provide
development validation, and none of the outputs authorize comparison, voting,
deployment, or a scientific superiority claim.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import random
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import chronicle_external_team_wave_model_v2 as wave_model_v2


ROW_SCHEMA = "chronicle_external_teammate_response_transition/v1"
SUFFICIENT_ROW_SCHEMA = "chronicle_external_teammate_response_sufficient_transition/v1"
SPLIT_SCHEMA = "chronicle_external_teammate_response_component_split/v1"
MODEL_SCHEMA = "chronicle_external_teammate_response_model/v1"
REMOTE_PLAN_SCHEMA = "chronicle_external_teammate_response_remote_plan/v1"
STATUS = "DEVELOPMENT_ONLY_NONVOTING_NONCOMPARISON"

ABLATION_A = "A_FIXED_HISTORICAL_EXACT_GUID_SCHEDULE_CONTROL"
ABLATION_B = "B_DYNAMIC_CLASS_SPEC_BACKOFF_NO_GUID"
ABLATION_C = "C_DYNAMIC_HIERARCHICAL_GUID_THEN_CLASS_SPEC_BACKOFF"
ABLATION_D = "D_C_WITHOUT_OTHER_TEAM_ACTION_OR_DAMAGE_INTENSITY"

_ABLATION_VARIANTS = {
    ABLATION_A: {
        "variant_id": ABLATION_A,
        "execution_family": "FIXED_HISTORICAL_EXACT_GUID_SCHEDULE",
        "learned_response_model": False,
        "historical_eventmeta_schedule": "REPLAY_UNCHANGED",
        "context_levels": [],
        "include_other_team_action_and_damage_intensity": False,
    },
    ABLATION_B: {
        "variant_id": ABLATION_B,
        "execution_family": "DYNAMIC_HIERARCHICAL_MARKED_SEMI_MARKOV",
        "learned_response_model": True,
        "historical_eventmeta_schedule": "NOT_REPLAYED",
        "context_levels": ["CLASS_SPEC", "CLASS", "GLOBAL"],
        "include_other_team_action_and_damage_intensity": True,
    },
    ABLATION_C: {
        "variant_id": ABLATION_C,
        "execution_family": "DYNAMIC_HIERARCHICAL_MARKED_SEMI_MARKOV",
        "learned_response_model": True,
        "historical_eventmeta_schedule": "NOT_REPLAYED",
        "context_levels": ["GUID", "CLASS_SPEC", "CLASS", "GLOBAL"],
        "include_other_team_action_and_damage_intensity": True,
    },
    ABLATION_D: {
        "variant_id": ABLATION_D,
        "execution_family": "DYNAMIC_HIERARCHICAL_MARKED_SEMI_MARKOV",
        "learned_response_model": True,
        "historical_eventmeta_schedule": "NOT_REPLAYED",
        "context_levels": ["GUID", "CLASS_SPEC", "CLASS", "GLOBAL"],
        "include_other_team_action_and_damage_intensity": False,
    },
}

EVENT_TYPES = frozenset({"START", "GO", "FAIL", "DMG", "HEAL"})
ACTION_EVENT_TYPES = frozenset({"START", "GO", "FAIL"})
PLAYER_ATTRIBUTION_KINDS = frozenset(
    {
        "DIRECT_FRIENDLY_PLAYER",
        "EXACT_OFFICIAL_OWNER",
        "EXACT_OFFICIAL_CONTROLLER",
    }
)
HOSTILE_LANES = frozenset({"HOSTILE_CREATURE", "HOSTILE_OBJECT", "HOSTILE_PLAYER"})
DELAY_UPPER_BOUNDS_MS = (0, 100, 250, 500, 1_000, 2_000, 4_000, 8_000, 16_000, 32_000)


class ChronicleExternalTeammateResponseModelV1Error(RuntimeError):
    """Raised when a source or causal boundary is inconsistent."""


def ablation_variant_v1(variant_id: str) -> dict[str, Any]:
    """Return one validated executable ablation contract."""

    key = _text(variant_id, "ablation variant_id")
    variant = _ABLATION_VARIANTS.get(key)
    if variant is None:
        raise ChronicleExternalTeammateResponseModelV1Error(
            f"unsupported teammate-response ablation {key}"
        )
    return deepcopy(variant)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalTeammateResponseModelV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalTeammateResponseModelV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalTeammateResponseModelV1Error(f"{label} must be non-empty text")
    return value


def _integer(value: Any, label: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalTeammateResponseModelV1Error(f"{label} must be an integer")
    if nonnegative and value < 0:
        raise ChronicleExternalTeammateResponseModelV1Error(f"{label} must be nonnegative")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _instance_node_id(instance_id: str) -> str:
    """Reproduce the Stage-5 split-graph node identity exactly."""

    digest = hashlib.sha256(
        _canonical_bytes(
            {"kind": "instance", "observed_identity": instance_id.casefold()}
        )
    ).hexdigest()
    return f"instance:{digest}"


def _count(value: Any, label: str) -> int:
    return _integer(value, label, nonnegative=True)


def old50_instance_ids_from_overlay_manifest_v1(
    overlay_manifest: Mapping[str, Any],
    stage5_manifest: Mapping[str, Any],
) -> list[str]:
    """Return the exact old-50 instance closure after checking its Stage-5 pin."""

    overlay = _mapping(overlay_manifest, "overlay manifest")
    if overlay.get("schema") != "chronicle_old50_exact_fury_slot_overlay_manifest/v1":
        raise ChronicleExternalTeammateResponseModelV1Error(
            "unsupported old-50 exact Fury overlay manifest"
        )
    stage5 = _mapping(stage5_manifest, "Stage-5 manifest")
    if stage5.get("schema") != wave_model_v2.SCHEMA:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "unsupported Stage-5 team-wave manifest"
        )
    source = _mapping(
        _mapping(overlay.get("source_bindings"), "overlay source_bindings").get(
            "stage5_manifest"
        ),
        "overlay Stage-5 binding",
    )
    stage5_address = _mapping(stage5.get("content_address"), "Stage-5 content_address")
    if source.get("content_sha256") != stage5_address.get("sha256"):
        raise ChronicleExternalTeammateResponseModelV1Error(
            "old-50 overlay is not bound to this Stage-5 manifest"
        )
    instance_ids = [
        _text(value, "overlay instance id")
        for value in _array(overlay.get("instance_order"), "overlay instance_order")
    ]
    if not instance_ids or len(instance_ids) != len(set(instance_ids)):
        raise ChronicleExternalTeammateResponseModelV1Error(
            "old-50 instance order must be non-empty and unique"
        )
    return instance_ids


_SUMMARY_FIELDS = (
    "wave_count",
    "player_wave_episode_count",
    "prefix_transition_count",
    "exact_event_count",
    "exact_trace_count",
)


def _component_index(stage5: Mapping[str, Any]) -> dict[str, str]:
    graph = _mapping(stage5.get("split_graph"), "Stage-5 split_graph")
    if (
        graph.get("required_split_unit") != "connected component"
        or graph.get("row_random_split_allowed") is not False
        or graph.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ChronicleExternalTeammateResponseModelV1Error(
            "Stage-5 identity-component split boundary was weakened"
        )
    result: dict[str, str] = {}
    for raw in _array(graph.get("node_to_component"), "node_to_component"):
        row = _mapping(raw, "node_to_component row")
        node_id = _text(row.get("node_id"), "split node_id")
        component_id = _text(row.get("component_id"), "split component_id")
        if node_id in result:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "duplicate split node mapping"
            )
        result[node_id] = component_id
    return result


def build_component_split_v1(
    stage5_manifest: Mapping[str, Any],
    *,
    old50_instance_ids: Sequence[str],
    validation_fraction: float = 0.20,
) -> dict[str, Any]:
    """Build a deterministic whole-component development split.

    Components containing an old-50 selected instance are always train.  Large
    remaining components are added to train only if required to reach the
    requested training mass; every remaining candidate component is validation.
    """

    stage5 = _mapping(stage5_manifest, "Stage-5 manifest")
    if stage5.get("schema") != wave_model_v2.SCHEMA:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "unsupported Stage-5 team-wave manifest"
        )
    if not isinstance(validation_fraction, (int, float)) or not 0 < validation_fraction < 0.5:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "validation_fraction must be in (0, 0.5)"
        )
    old50 = [_text(value, "old-50 instance id") for value in old50_instance_ids]
    if not old50 or len(old50) != len(set(old50)):
        raise ChronicleExternalTeammateResponseModelV1Error(
            "old-50 instance ids must be non-empty and unique"
        )

    node_components = _component_index(stage5)
    instances = [
        _mapping(value, "Stage-5 instance")
        for value in _array(stage5.get("instances"), "Stage-5 instances")
    ]
    by_id: dict[str, Mapping[str, Any]] = {}
    component_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    excluded_ids: list[str] = []
    for entry in instances:
        instance_id = _text(entry.get("instance_id"), "Stage-5 instance_id")
        if instance_id in by_id:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "duplicate Stage-5 instance id"
            )
        by_id[instance_id] = entry
        contamination = _mapping(entry.get("contamination_lane"), "contamination_lane")
        if contamination.get("candidate_filter_passed") is not True:
            excluded_ids.append(instance_id)
            continue
        component_id = node_components.get(_instance_node_id(instance_id))
        if component_id is None:
            raise ChronicleExternalTeammateResponseModelV1Error(
                f"candidate instance {instance_id} has no split component"
            )
        component_rows[component_id].append(entry)

    missing = sorted(set(old50) - set(by_id))
    noncandidate = sorted(
        instance_id
        for instance_id in old50
        if instance_id in by_id
        and _mapping(by_id[instance_id].get("contamination_lane"), "contamination_lane").get(
            "candidate_filter_passed"
        )
        is not True
    )
    if missing or noncandidate:
        raise ChronicleExternalTeammateResponseModelV1Error(
            f"old-50 closure is not training-candidate complete: missing={missing}, noncandidate={noncandidate}"
        )

    protected_components = {
        node_components[_instance_node_id(instance_id)] for instance_id in old50
    }
    if not component_rows or len(component_rows) < 2:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "at least two candidate identity components are required"
        )

    def component_stats(component_id: str) -> dict[str, Any]:
        rows = component_rows[component_id]
        totals = {
            field: sum(
                _count(_mapping(row.get("summary"), "instance summary").get(field), field)
                for row in rows
            )
            for field in _SUMMARY_FIELDS
        }
        totals["compressed_partition_bytes"] = sum(
            _count(
                _mapping(row.get("partition"), "instance partition").get(
                    "compressed_size_bytes"
                ),
                "compressed_size_bytes",
            )
            for row in rows
        )
        return {
            "component_id": component_id,
            "instance_ids": sorted(_text(row.get("instance_id"), "instance id") for row in rows),
            "instance_count": len(rows),
            **totals,
        }

    components = {
        component_id: component_stats(component_id)
        for component_id in sorted(component_rows)
    }
    total_events = sum(row["exact_event_count"] for row in components.values())
    train_target = math.ceil(total_events * (1.0 - float(validation_fraction)))
    train_components = set(protected_components)
    train_events = sum(components[value]["exact_event_count"] for value in train_components)
    remaining = sorted(
        (value for value in components if value not in train_components),
        key=lambda value: (-components[value]["exact_event_count"], value),
    )
    while train_events < train_target and len(remaining) > 1:
        component_id = remaining.pop(0)
        train_components.add(component_id)
        train_events += components[component_id]["exact_event_count"]
    validation_components = set(components) - train_components
    if not validation_components:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "no disconnected development-validation component remains"
        )

    split_rows: list[dict[str, Any]] = []
    for component_id, stats in components.items():
        split = "TRAIN" if component_id in train_components else "VALIDATION"
        split_rows.append({**deepcopy(stats), "split": split})

    def totals(split: str) -> dict[str, int]:
        selected = [row for row in split_rows if row["split"] == split]
        return {
            "component_count": len(selected),
            "instance_count": sum(row["instance_count"] for row in selected),
            **{
                field: sum(row[field] for row in selected)
                for field in (*_SUMMARY_FIELDS, "compressed_partition_bytes")
            },
        }

    return {
        "schema": SPLIT_SCHEMA,
        "status": STATUS,
        "source_stage5": {
            "schema": stage5.get("schema"),
            "content_sha256": _mapping(
                stage5.get("content_address"), "Stage-5 content_address"
            ).get("sha256"),
        },
        "split_rule": {
            "unit": "connected component of exact instance, guild, and player identities",
            "requested_validation_fraction": float(validation_fraction),
            "mass_field": "exact_event_count",
            "old50_components_forced_to_train": True,
            "old50_is_heldout_or_comparison": False,
            "row_random_split_allowed": False,
            "same_player_or_guild_can_cross_folds": False,
        },
        "old50_instance_ids": sorted(old50),
        "protected_train_component_ids": sorted(protected_components),
        "components": split_rows,
        "excluded_descriptive_nontraining_instance_ids": sorted(excluded_ids),
        "summary": {
            "train": totals("TRAIN"),
            "validation": totals("VALIDATION"),
            "candidate_total": totals("TRAIN") | {
                field: totals("TRAIN")[field] + totals("VALIDATION")[field]
                for field in (
                    "component_count",
                    "instance_count",
                    *_SUMMARY_FIELDS,
                    "compressed_partition_bytes",
                )
            },
        },
        "scientific_boundary": {
            "development_validation_only": True,
            "old50_nonheldout_training_discovery_only": True,
            "heldout_performance_evidence_eligible": False,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        },
    }


@dataclass
class _TargetPrefix:
    guid: str
    lane: str
    first_seen_ms: int
    last_seen_ms: int
    prefix_damage: int = 0
    dead: bool = False


@dataclass(frozen=True)
class _Activity:
    time_ms: int
    actor_guid: str | None
    event_type: str
    damage: int


@dataclass
class _ActorPrefix:
    last_event_ms: int | None = None
    last_mark_token: str | None = None
    last_spell_id: int | None = None
    last_target_guid: str | None = None


@dataclass
class _PrefixReplay:
    wave_start_offset_ms: int
    targets: dict[str, _TargetPrefix] = field(default_factory=dict)
    actors: dict[str, _ActorPrefix] = field(default_factory=lambda: defaultdict(_ActorPrefix))
    recent: deque[_Activity] = field(default_factory=deque)
    prefix_trace_count: int = 0

    def elapsed(self, anchor: Mapping[str, Any]) -> int:
        return max(
            0,
            _integer(anchor.get("offset_ms"), "anchor.offset_ms")
            - self.wave_start_offset_ms,
        )

    def _prune(self, time_ms: int) -> None:
        while self.recent and time_ms - self.recent[0].time_ms > 3_000:
            self.recent.popleft()

    @staticmethod
    def _activity_summaries(
        values: Iterable[_Activity], time_ms: int, actor_guid: str
    ) -> dict[str, dict[str, int]]:
        """Aggregate all four causal activity views in one pass.

        The prior implementation materialized four filtered copies of the same
        recent-activity deque and then scanned each copy twice.  Keep the exact
        inclusive 1 s/3 s window semantics while accumulating actor, other,
        whole-team, and explicitly unattributed views together.
        """

        # Fixed slots avoid constructing metric names and temporary selected
        # lists inside the hot loop.  The first four slots are the 1 s window;
        # the last four are the 3 s window (count, action, DMG count, damage).
        actor_values = [0] * 8
        other_values = [0] * 8
        whole_values = [0] * 8
        unattributed_values = [0] * 8
        for row in values:
            age_ms = time_ms - row.time_ms
            if age_ms < 0 or age_ms > 3_000:
                continue
            action = int(row.event_type in ACTION_EVENT_TYPES)
            damage_event = int(row.event_type == "DMG")
            selected = actor_values if row.actor_guid == actor_guid else other_values
            for summary in (whole_values, selected):
                summary[4] += 1
                summary[5] += action
                summary[6] += damage_event
                summary[7] += row.damage
                if age_ms <= 1_000:
                    summary[0] += 1
                    summary[1] += action
                    summary[2] += damage_event
                    summary[3] += row.damage
            if row.actor_guid is None:
                unattributed_values[4] += 1
                unattributed_values[5] += action
                unattributed_values[6] += damage_event
                unattributed_values[7] += row.damage
                if age_ms <= 1_000:
                    unattributed_values[0] += 1
                    unattributed_values[1] += action
                    unattributed_values[2] += damage_event
                    unattributed_values[3] += row.damage

        def materialize(summary: Sequence[int]) -> dict[str, int]:
            return {
                "marked_event_count_1000ms": summary[0],
                "action_event_count_1000ms": summary[1],
                "damage_event_count_1000ms": summary[2],
                "damage_amount_1000ms": summary[3],
                "marked_event_count_3000ms": summary[4],
                "action_event_count_3000ms": summary[5],
                "damage_event_count_3000ms": summary[6],
                "damage_amount_3000ms": summary[7],
            }

        return {
            "actor": materialize(actor_values),
            "other_team_including_unattributed": materialize(other_values),
            "whole_team": materialize(whole_values),
            "unattributed_explicit_unknown": materialize(unattributed_values),
        }

    def snapshot(
        self,
        *,
        actor_guid: str,
        time_ms: int,
        cutoff_order_key: Sequence[int] | None,
        cutoff_semantics: str,
    ) -> dict[str, Any]:
        self._prune(time_ms)
        actor = self.actors[actor_guid]
        activity = self._activity_summaries(self.recent, time_ms, actor_guid)
        alive = sorted(
            target.guid
            for target in self.targets.values()
            if target.lane == "HOSTILE_CREATURE" and not target.dead
        )
        dead = sorted(
            target.guid
            for target in self.targets.values()
            if target.lane == "HOSTILE_CREATURE" and target.dead
        )
        return {
            "cutoff_semantics": cutoff_semantics,
            "cutoff_exclusive_order_key": list(cutoff_order_key) if cutoff_order_key else None,
            "prefix_trace_exclusive_index": self.prefix_trace_count,
            "wave_elapsed_ms": time_ms,
            "actor_last_event_ms": actor.last_event_ms,
            "time_since_actor_event_ms": (
                time_ms - actor.last_event_ms if actor.last_event_ms is not None else None
            ),
            "actor_last_mark_token": actor.last_mark_token,
            "actor_last_spell_id": actor.last_spell_id,
            "actor_last_target_guid": actor.last_target_guid,
            "marked_activity": activity,
            "target_state": {
                "alive_target_guids": alive,
                "dead_target_guids": dead,
                "alive_semantics": "PREFIX_OBSERVED_HOSTILE_CREATURE_NOT_YET_DEAD_PROXY",
                "targets": [
                    {
                        "target_guid": target.guid,
                        "last_observed_lane": target.lane,
                        "first_seen_ms": target.first_seen_ms,
                        "last_seen_ms": target.last_seen_ms,
                        "prefix_damage": target.prefix_damage,
                        "observed_dead": target.dead,
                    }
                    for target in sorted(self.targets.values(), key=lambda item: item.guid)
                ],
            },
            "future_event_or_death_visible": False,
        }

    def sufficient_snapshot(
        self,
        *,
        actor_guid: str,
        time_ms: int,
        cutoff_order_key: Sequence[int] | None,
        cutoff_semantics: str,
    ) -> dict[str, Any]:
        """Return only the causal fields consumed by B/C/D statistics."""

        self._prune(time_ms)
        actor = self.actors[actor_guid]
        other_action_count = 0
        other_damage_amount = 0
        for row in self.recent:
            age_ms = time_ms - row.time_ms
            if (
                age_ms < 0
                or age_ms > 3_000
                or row.actor_guid == actor_guid
            ):
                continue
            other_action_count += row.event_type in ACTION_EVENT_TYPES
            other_damage_amount += row.damage
        alive_target_count = sum(
            target.lane == "HOSTILE_CREATURE" and not target.dead
            for target in self.targets.values()
        )
        return {
            "cutoff_semantics": cutoff_semantics,
            "cutoff_exclusive_order_key": (
                list(cutoff_order_key) if cutoff_order_key else None
            ),
            "prefix_trace_exclusive_index": self.prefix_trace_count,
            "actor_last_mark_token": actor.last_mark_token,
            "marked_activity": {
                "other_team_including_unattributed": {
                    "action_event_count_3000ms": other_action_count,
                    "damage_amount_3000ms": other_damage_amount,
                }
            },
            "target_state": {"alive_target_count": alive_target_count},
            "future_event_or_death_visible": False,
        }

    def _observe_target(
        self,
        guid: str | None,
        lane: str,
        time_ms: int,
        *,
        damage: int = 0,
        dead: bool = False,
    ) -> None:
        if guid is None:
            return
        target = self.targets.get(guid)
        if target is None:
            target = _TargetPrefix(guid, lane, time_ms, time_ms)
            self.targets[guid] = target
        else:
            if lane != "UNKNOWN_NONVOTING" or target.lane == "UNKNOWN_NONVOTING":
                target.lane = lane
            target.last_seen_ms = time_ms
        target.prefix_damage += damage
        target.dead = target.dead or dead

    def observe(self, trace: Mapping[str, Any]) -> None:
        kind = _text(trace.get("trace_kind"), "trace_kind")
        anchor = _mapping(trace.get("anchor"), "trace anchor")
        time_ms = self.elapsed(anchor)
        if kind == "CLASSIFICATION_CONTEXT":
            classification = _mapping(trace.get("classification"), "classification")
            self._observe_target(
                _optional_text(classification.get("guid")),
                _text(classification.get("lane"), "classification lane"),
                time_ms,
            )
            self.prefix_trace_count += 1
            return
        event = _mapping(trace.get("event"), "trace event")
        target = _mapping(event.get("target"), "event target")
        target_guid = _optional_text(target.get("guid"))
        target_lane = _text(target.get("lane"), "target lane")
        if kind == "DEATH_MARKER":
            if event.get("event_type") != "DEAD" or "damage" in event:
                raise ChronicleExternalTeammateResponseModelV1Error(
                    "DEAD must remain a zero-damage marker"
                )
            self._observe_target(target_guid, target_lane, time_ms, dead=True)
            self.prefix_trace_count += 1
            return
        if kind == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT":
            self._observe_target(target_guid, target_lane, time_ms)
            self.prefix_trace_count += 1
            return
        if kind not in {"EXACT_PLAYER_EVENT", "UNATTRIBUTED_EVENT"}:
            raise ChronicleExternalTeammateResponseModelV1Error(
                f"unsupported exact trace kind {kind}"
            )
        event_type = _text(event.get("event_type"), "event_type")
        if event_type not in EVENT_TYPES:
            raise ChronicleExternalTeammateResponseModelV1Error(
                f"unsupported teammate event type {event_type}"
            )
        actor_guid = _optional_text(trace.get("player_guid"))
        damage = 0
        if event_type == "DMG":
            damage = _count(
                _mapping(event.get("damage"), "event damage").get("amount"),
                "damage amount",
            )
        self._observe_target(target_guid, target_lane, time_ms, damage=damage)
        self.recent.append(_Activity(time_ms, actor_guid, event_type, damage))
        if actor_guid is not None:
            actor = self.actors[actor_guid]
            actor.last_event_ms = time_ms
            actor.last_mark_token = _mark_token(event)
            spell = _mapping(event.get("spell"), "event spell")
            actor.last_spell_id = (
                spell.get("id") if isinstance(spell.get("id"), int) else None
            )
            actor.last_target_guid = target_guid
        self.prefix_trace_count += 1


def _mark_token(event: Mapping[str, Any]) -> str:
    spell = _mapping(event.get("spell"), "event spell")
    attribution = _mapping(event.get("attribution"), "event attribution")
    return json.dumps(
        [event.get("event_type"), spell.get("id"), attribution.get("attribution_kind")],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _actor_metadata(player_record: Mapping[str, Any]) -> dict[str, Any]:
    player = _mapping(player_record.get("player"), "player metadata")
    guid = _text(player.get("guid"), "player guid")
    hero_class = _text(player.get("class"), "player class").upper()
    lane = _mapping(player_record.get("warrior_spec_lane"), "warrior_spec_lane")
    if hero_class == "WARRIOR":
        spec_key = _text(lane.get("partition_key"), "warrior partition_key")
        observed_spec = lane.get("observed_spec")
        spec_status = _text(lane.get("evidence_status"), "warrior evidence_status")
    else:
        spec_key = f"{hero_class}_SPEC_NOT_AVAILABLE"
        observed_spec = None
        spec_status = "SPEC_NOT_AVAILABLE_FROM_CURRENT_ARTIFACT"
    return {
        "player_guid": guid,
        "class": hero_class,
        "spec_key": spec_key,
        "observed_spec": observed_spec,
        "spec_status": spec_status,
        "race": player.get("race"),
    }


def _event_target_mode(event: Mapping[str, Any], state: _PrefixReplay, actor_guid: str) -> str:
    target = _mapping(event.get("target"), "event target")
    guid = _optional_text(target.get("guid"))
    if guid is None:
        return "NO_TARGET"
    if target.get("voting_enemy_target") is not True or target.get("lane") != "HOSTILE_CREATURE":
        return "NON_HOSTILE_OR_UNKNOWN"
    known = state.targets.get(guid)
    if known is not None and known.dead:
        return "DEAD_TARGET_OBSERVED_DIAGNOSTIC"
    if known is None:
        return "UNSEEN_HOSTILE_CURRENT_LABEL"
    if state.actors[actor_guid].last_target_guid == guid:
        return "STAY_ALIVE"
    return "SWITCH_ALIVE"


def _trace_anchor(trace: Mapping[str, Any]) -> Mapping[str, Any]:
    anchor = trace.get("anchor")
    if not isinstance(anchor, Mapping):
        event = _mapping(trace.get("event"), "trace event")
        anchor = event.get("anchor")
    return _mapping(anchor, "trace anchor")


def _iter_wave_response_rows_v1(
    wave_record: Mapping[str, Any], *, copy_rows: bool, sufficient_only: bool
) -> Iterator[dict[str, Any]]:
    """Shared causal compiler for full and worker-sufficient rows."""

    record = _mapping(wave_record, "Stage-5 wave")
    if record.get("schema") != wave_model_v2.PARTITION_RECORD_SCHEMA:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "unsupported Stage-5 wave record"
        )
    wave = _mapping(record.get("wave"), "wave identity")
    identity = {
        key: wave.get(key)
        for key in (
            "instance_id",
            "encounter_id",
            "encounter_ordinal",
            "wave_id",
            "wave_ordinal",
        )
    }
    for key in ("instance_id", "encounter_id", "wave_id"):
        _text(identity[key], f"wave.{key}")
    for key in ("encounter_ordinal", "wave_ordinal"):
        _count(identity[key], f"wave.{key}")

    player_records = [
        _mapping(value, "player record")
        for value in _array(record.get("players"), "players")
    ]
    actors: dict[str, dict[str, Any]] = {}
    for value in player_records:
        actor_metadata = _actor_metadata(value)
        actors[actor_metadata["player_guid"]] = actor_metadata
    if len(actors) != len(player_records):
        raise ChronicleExternalTeammateResponseModelV1Error("duplicate player GUID")

    reconstruction = _mapping(
        _mapping(record.get("descriptive_outcome"), "descriptive_outcome").get(
            "reconstruction_binding"
        ),
        "reconstruction_binding",
    )
    window = _mapping(reconstruction.get("window"), "reconstruction window")
    start_offset = _integer(window.get("start_offset_ms"), "window.start_offset_ms")
    replay = _PrefixReplay(start_offset)
    snapshot = replay.sufficient_snapshot if sufficient_only else replay.snapshot
    wave_start_state_by_actor = {
        actor_guid: snapshot(
            actor_guid=actor_guid,
            time_ms=0,
            cutoff_order_key=None,
            cutoff_semantics="WAVE_START_BEFORE_ANY_TRACE_ROW",
        )
        for actor_guid in actors
    }
    timing_state_by_actor: dict[str, dict[str, Any]] = {}
    prior_order: tuple[int, ...] | None = None

    trace_rows = [
        _mapping(value, "exact trace row")
        for value in _array(record.get("exact_trace"), "exact_trace")
    ]
    for expected_index, trace in enumerate(trace_rows):
        if _count(trace.get("trace_index"), "trace_index") != expected_index:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "trace indices are not contiguous"
            )
        order_raw = _array(trace.get("order_key"), "trace order_key")
        if len(order_raw) != 5:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "trace order key must contain five integers"
            )
        order = tuple(_count(value, "trace order component") for value in order_raw)
        if prior_order is not None and order <= prior_order:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "exact trace is not in strict EventMeta order"
            )
        prior_order = order
        normalized_trace = dict(trace)
        normalized_trace["anchor"] = _trace_anchor(trace)
        kind = _text(trace.get("trace_kind"), "trace_kind")
        if kind != "EXACT_PLAYER_EVENT":
            replay.observe(normalized_trace)
            continue

        actor_guid = _text(trace.get("player_guid"), "trace player_guid")
        actor = actors.get(actor_guid)
        if actor is None:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "exact player event actor is absent from roster"
            )
        event = _mapping(trace.get("event"), "trace event")
        event_type = _text(event.get("event_type"), "event_type")
        if event_type not in EVENT_TYPES:
            raise ChronicleExternalTeammateResponseModelV1Error(
                f"unsupported player event type {event_type}"
            )
        attribution = _mapping(event.get("attribution"), "event attribution")
        attribution_kind = _text(
            attribution.get("attribution_kind"), "attribution_kind"
        )
        if (
            attribution_kind not in PLAYER_ATTRIBUTION_KINDS
            or attribution.get("player_guid") != actor_guid
        ):
            raise ChronicleExternalTeammateResponseModelV1Error(
                "event lacks exact direct/owner/controller player attribution"
            )
        anchor = _trace_anchor(trace)
        time_ms = replay.elapsed(anchor)
        emission_state = snapshot(
            actor_guid=actor_guid,
            time_ms=time_ms,
            cutoff_order_key=order,
            cutoff_semantics="STRICTLY_BEFORE_CURRENT_EVENTMETA_ROW",
        )
        timing_state = timing_state_by_actor.get(actor_guid)
        if timing_state is None:
            timing_state = wave_start_state_by_actor[actor_guid]
            delay_ms = time_ms
            delay_origin = "WAVE_START"
        else:
            last_ms = replay.actors[actor_guid].last_event_ms
            if last_ms is None or time_ms < last_ms:
                raise ChronicleExternalTeammateResponseModelV1Error(
                    "actor event time regressed"
                )
            delay_ms = time_ms - last_ms
            delay_origin = "PREVIOUS_ACTOR_EVENT"

        target = _mapping(event.get("target"), "event target")
        source = _mapping(event.get("source"), "event source")
        spell = _mapping(event.get("spell"), "event spell")
        damage = (
            _count(_mapping(event.get("damage"), "event damage").get("amount"), "damage")
            if event_type == "DMG"
            else 0
        )
        healing = (
            _count(_mapping(event.get("healing"), "event healing").get("amount"), "healing")
            if event_type == "HEAL"
            else 0
        )
        label = {
            "event_type": event_type,
            "mark_token": _mark_token(event),
            "spell_id": spell.get("id"),
            "spell_name": spell.get("name"),
            "attribution_kind": attribution_kind,
            "attributed_player_guid": actor_guid,
            "exact_source_guid": source.get("guid"),
            "source_lane": source.get("lane"),
            "target_guid": target.get("guid"),
            "target_lane": target.get("lane"),
            "target_mode": _event_target_mode(event, replay, actor_guid),
            "inter_event_delay_ms": delay_ms,
            "delay_origin": delay_origin,
            "damage_amount": damage,
            "healing_amount": healing,
        }
        causal_contract = {
            "timing_state_contains_current_or_later_trace": False,
            "emission_state_contains_current_or_later_trace": False,
            "current_event_is_label_only": True,
            "future_death_visible": False,
            "exact_guid_owner_controller_only": True,
        }
        if sufficient_only:
            row = {
                "schema": SUFFICIENT_ROW_SCHEMA,
                "status": STATUS,
                "trace_index": expected_index,
                "eventmeta_order_key": list(order),
                "actor": actor,
                "timing_state_after_previous_actor_event": timing_state,
                "emission_state_before_current_event": emission_state,
                "label": label,
                "causal_contract": causal_contract,
            }
        else:
            row = {
                "schema": ROW_SCHEMA,
                "status": STATUS,
                "wave": deepcopy(identity) if copy_rows else identity,
                "trace_index": expected_index,
                "eventmeta_order_key": list(order),
                "actor": deepcopy(actor) if copy_rows else actor,
                "timing_state_after_previous_actor_event": (
                    deepcopy(timing_state) if copy_rows else timing_state
                ),
                "emission_state_before_current_event": emission_state,
                "label": label,
                "causal_contract": causal_contract,
                "scientific_boundary": {
                    "development_training_or_validation_only": True,
                    "heldout_performance_evidence_eligible": False,
                    "comparison_eligible": False,
                    "voting_eligible": False,
                    "deployment_eligible": False,
                },
            }
        replay.observe(normalized_trace)
        timing_state_by_actor[actor_guid] = snapshot(
            actor_guid=actor_guid,
            time_ms=time_ms,
            cutoff_order_key=None,
            cutoff_semantics=(
                "IMMEDIATELY_AFTER_PREVIOUS_ACTOR_EVENT_BEFORE_ALL_LATER_TRACE_ROWS"
            ),
        )
        yield row


def iter_wave_response_rows_v1(
    wave_record: Mapping[str, Any], *, copy_rows: bool = True
) -> Iterator[dict[str, Any]]:
    """Yield full exact-player response rows from one Stage-5 v2 wave."""

    yield from _iter_wave_response_rows_v1(
        wave_record, copy_rows=copy_rows, sufficient_only=False
    )


def iter_wave_response_sufficient_rows_v1(
    wave_record: Mapping[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield compact causal rows sufficient for exact B/C/D statistics."""

    yield from _iter_wave_response_rows_v1(
        wave_record, copy_rows=False, sufficient_only=True
    )


def compile_wave_response_rows_v1(
    wave_record: Mapping[str, Any], *, copy_rows: bool = True
) -> list[dict[str, Any]]:
    """Compile exact-player response rows from one Stage-5 v2 wave."""

    return list(iter_wave_response_rows_v1(wave_record, copy_rows=copy_rows))


def fixed_historical_exact_guid_schedule_control_v1(
    wave_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize arm A without changing its historical EventMeta trace."""

    record = _mapping(wave_record, "Stage-5 wave")
    # Reuse the learned arms' schema, ordering, roster, and attribution checks.
    # The returned trace itself is only copied, never rescheduled or relabelled.
    compile_wave_response_rows_v1(record)
    return {
        "schema": "chronicle_external_teammate_fixed_schedule_control/v1",
        "status": STATUS,
        "variant": ablation_variant_v1(ABLATION_A),
        "wave": deepcopy(_mapping(record.get("wave"), "wave identity")),
        "exact_trace": deepcopy(_array(record.get("exact_trace"), "exact_trace")),
        "trace_mutated": False,
        "candidate_feedback_applied": False,
        "scientific_boundary": {
            "development_validation_only": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        },
    }


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value <= 2:
        return "1-2"
    if value <= 5:
        return "3-5"
    return "6+"


def _magnitude_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    return f"2^{value.bit_length() - 1}"


def _delay_bucket(value: int) -> int:
    for index, upper in enumerate(DELAY_UPPER_BOUNDS_MS):
        if value <= upper:
            return index
    return len(DELAY_UPPER_BOUNDS_MS)


def _sample_delay_bucket(bucket: int, rng: random.Random) -> int:
    if bucket == 0:
        return 0
    lower = DELAY_UPPER_BOUNDS_MS[bucket - 1] + 1
    upper = (
        DELAY_UPPER_BOUNDS_MS[bucket]
        if bucket < len(DELAY_UPPER_BOUNDS_MS)
        else DELAY_UPPER_BOUNDS_MS[-1] * 2
    )
    return rng.randint(lower, upper)


def _damage_bucket(value: int) -> int:
    return 0 if value <= 0 else value.bit_length()


def _sample_damage_bucket(bucket: int, rng: random.Random) -> int:
    if bucket <= 0:
        return 0
    return rng.randint(1 << (bucket - 1), (1 << bucket) - 1)


def _weighted_choice(counter: Counter[Any], rng: random.Random) -> Any:
    total = sum(counter.values())
    if total <= 0:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "cannot sample an empty empirical head"
        )
    draw = rng.randrange(total)
    cumulative = 0
    for value, count in sorted(counter.items(), key=lambda item: repr(item[0])):
        cumulative += count
        if draw < cumulative:
            return value
    raise AssertionError("weighted choice fell through")


def _context_keys(
    actor: Mapping[str, Any],
    state: Mapping[str, Any],
    variant_id: str,
) -> list[tuple[str, ...]]:
    key = _text(variant_id, "ablation variant_id")
    variant = _ABLATION_VARIANTS.get(key)
    if variant is None:
        raise ChronicleExternalTeammateResponseModelV1Error(
            f"unsupported teammate-response ablation {key}"
        )
    if not variant["learned_response_model"]:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "fixed historical arm A does not train or sample a response model"
        )
    activity = _mapping(state.get("marked_activity"), "marked_activity")
    other = _mapping(
        activity.get("other_team_including_unattributed"), "other-team activity"
    )
    targets = _mapping(state.get("target_state"), "target_state")
    compact_alive_count = targets.get("alive_target_count")
    alive_count = (
        _count(compact_alive_count, "alive target count")
        if compact_alive_count is not None
        else len(_array(targets.get("alive_target_guids"), "alive targets"))
    )
    last_mark = _optional_text(state.get("actor_last_mark_token")) or "__NONE__"
    action_bucket = _count_bucket(
        _count(other.get("action_event_count_3000ms"), "other action count")
    )
    damage_bucket = _magnitude_bucket(
        _count(other.get("damage_amount_3000ms"), "other damage amount")
    )
    alive_bucket = _count_bucket(alive_count)
    guid = _text(actor.get("player_guid"), "actor player_guid")
    hero_class = _text(actor.get("class"), "actor class")
    spec = _text(actor.get("spec_key"), "actor spec_key")
    intensity = (action_bucket, damage_bucket) if variant[
        "include_other_team_action_and_damage_intensity"
    ] else ()
    available = {
        "GUID": ("GUID", guid, last_mark, alive_bucket, *intensity),
        "CLASS_SPEC": (
            "CLASS_SPEC",
            hero_class,
            spec,
            last_mark,
            alive_bucket,
            *intensity,
        ),
        "CLASS": ("CLASS", hero_class, last_mark, alive_bucket),
        "GLOBAL": ("GLOBAL",),
    }
    return [available[level] for level in variant["context_levels"]]


class HierarchicalMarkedSemiMarkovV1:
    """Mergeable empirical response baseline with exact-GUID backoff."""

    def __init__(
        self,
        *,
        variant_id: str = ABLATION_C,
        min_guid_events: int = 25,
        min_class_spec_events: int = 50,
        min_class_events: int = 100,
    ) -> None:
        self.variant = ablation_variant_v1(variant_id)
        if not self.variant["learned_response_model"]:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "arm A is the fixed-schedule control and cannot instantiate a learned model"
            )
        self.variant_id = self.variant["variant_id"]
        self.minimums = {
            "GUID": min_guid_events,
            "CLASS_SPEC": min_class_spec_events,
            "CLASS": min_class_events,
            "GLOBAL": 1,
        }
        if any(value < 1 for value in self.minimums.values()):
            raise ChronicleExternalTeammateResponseModelV1Error(
                "all empirical backoff minimums must be positive"
            )
        self.mark_counts: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
        self.delay_counts: dict[tuple[str, ...], Counter[int]] = defaultdict(Counter)
        self.target_counts: dict[tuple[tuple[str, ...], str], Counter[str]] = defaultdict(Counter)
        self.damage_counts: dict[tuple[tuple[str, ...], str], Counter[int]] = defaultdict(Counter)
        self.spell_name_counts: dict[str, Counter[str | None]] = defaultdict(Counter)
        self.source_guid_counts: dict[tuple[str, str], Counter[str | None]] = defaultdict(Counter)
        self.row_count = 0

    def update(self, row: Mapping[str, Any]) -> None:
        if row.get("schema") not in {ROW_SCHEMA, SUFFICIENT_ROW_SCHEMA}:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "unsupported response transition row"
            )
        actor = _mapping(row.get("actor"), "row actor")
        emission = _mapping(
            row.get("emission_state_before_current_event"), "emission state"
        )
        timing = _mapping(
            row.get("timing_state_after_previous_actor_event"), "timing state"
        )
        label = _mapping(row.get("label"), "row label")
        token = _text(label.get("mark_token"), "mark token")
        delay = _count(label.get("inter_event_delay_ms"), "inter-event delay")
        damage = _count(label.get("damage_amount"), "damage amount")
        target_mode = _text(label.get("target_mode"), "target mode")
        for context in _context_keys(actor, emission, self.variant_id):
            self.mark_counts[context][token] += 1
            self.target_counts[(context, token)][target_mode] += 1
            self.damage_counts[(context, token)][_damage_bucket(damage)] += 1
        for context in _context_keys(actor, timing, self.variant_id):
            self.delay_counts[context][_delay_bucket(delay)] += 1
        spell_name = label.get("spell_name")
        self.spell_name_counts[token][spell_name if isinstance(spell_name, str) else None] += 1
        if "GUID" in self.variant["context_levels"]:
            self.source_guid_counts[
                (_text(actor.get("player_guid"), "actor guid"), token)
            ][_optional_text(label.get("exact_source_guid"))] += 1
        self.row_count += 1

    def merge(self, other: "HierarchicalMarkedSemiMarkovV1") -> None:
        if self.variant_id != other.variant_id or self.minimums != other.minimums:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "cannot merge models with different variants or backoff minimums"
            )
        for destination, source in (
            (self.mark_counts, other.mark_counts),
            (self.delay_counts, other.delay_counts),
            (self.target_counts, other.target_counts),
            (self.damage_counts, other.damage_counts),
            (self.spell_name_counts, other.spell_name_counts),
            (self.source_guid_counts, other.source_guid_counts),
        ):
            for key, counts in source.items():
                destination[key].update(counts)
        self.row_count += other.row_count

    def _select_context(
        self,
        table: Mapping[tuple[str, ...], Counter[Any]],
        actor: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> tuple[str, ...]:
        for context in _context_keys(actor, state, self.variant_id):
            if sum(table.get(context, Counter()).values()) >= self.minimums[context[0]]:
                return context
        raise ChronicleExternalTeammateResponseModelV1Error(
            "model has no global empirical support"
        )

    def sample_delay(
        self,
        *,
        actor: Mapping[str, Any],
        timing_state: Mapping[str, Any],
        rng: random.Random,
    ) -> dict[str, Any]:
        context = self._select_context(self.delay_counts, actor, timing_state)
        bucket = _weighted_choice(self.delay_counts[context], rng)
        return {
            "delay_ms": _sample_delay_bucket(bucket, rng),
            "delay_bucket": bucket,
            "context_level": context[0],
            "context": list(context),
            "support": sum(self.delay_counts[context].values()),
        }

    def sample_emission(
        self,
        *,
        actor: Mapping[str, Any],
        emission_state: Mapping[str, Any],
        rng: random.Random,
    ) -> dict[str, Any]:
        context = self._select_context(self.mark_counts, actor, emission_state)
        token = _weighted_choice(self.mark_counts[context], rng)
        event_type, spell_id, attribution_kind = json.loads(token)
        target_mode = _weighted_choice(self.target_counts[(context, token)], rng)
        damage_bucket = _weighted_choice(self.damage_counts[(context, token)], rng)
        damage = _sample_damage_bucket(damage_bucket, rng) if event_type == "DMG" else 0
        actor_guid = _text(actor.get("player_guid"), "actor guid")
        source_counts = (
            self.source_guid_counts.get((actor_guid, token), Counter())
            if "GUID" in self.variant["context_levels"]
            else Counter()
        )
        source_guid = _weighted_choice(source_counts, rng) if source_counts else None
        spell_name = _weighted_choice(self.spell_name_counts[token], rng)
        return {
            "event_type": event_type,
            "spell_id": spell_id,
            "spell_name": spell_name,
            "attribution_kind": attribution_kind,
            "attributed_player_guid": actor_guid,
            "exact_source_guid": source_guid,
            "target_mode": target_mode,
            "sampled_damage": damage,
            "damage_bucket": damage_bucket,
            "context_level": context[0],
            "context": list(context),
            "support": sum(self.mark_counts[context].values()),
        }


@dataclass
class _RuntimeActor:
    metadata: dict[str, Any]
    current_target_guid: str | None = None


class DynamicTeamRuntimeV1:
    """Small simulator-facing state machine for dynamic kill clock and retargeting."""

    def __init__(
        self,
        *,
        actors: Sequence[Mapping[str, Any]],
        target_health_by_guid: Mapping[str, int | float],
    ) -> None:
        self.actors: dict[str, _RuntimeActor] = {}
        for raw in actors:
            metadata = dict(_mapping(raw, "runtime actor"))
            guid = _text(metadata.get("player_guid"), "runtime actor guid")
            if guid in self.actors:
                raise ChronicleExternalTeammateResponseModelV1Error(
                    "duplicate runtime actor guid"
                )
            self.actors[guid] = _RuntimeActor(metadata)
        self.health: dict[str, float] = {}
        for guid, raw_health in target_health_by_guid.items():
            value = float(raw_health)
            if not math.isfinite(value) or value <= 0:
                raise ChronicleExternalTeammateResponseModelV1Error(
                    "runtime target health must be finite and positive"
                )
            self.health[_text(guid, "runtime target guid")] = value
        if not self.health:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "runtime requires at least one target"
            )
        self.time_ms = 0
        self.kill_clock_ms: int | None = None
        self._replay = _PrefixReplay(0)
        for guid in sorted(self.health):
            self._replay._observe_target(guid, "HOSTILE_CREATURE", 0)

    def alive_target_guids(self) -> list[str]:
        return sorted(guid for guid, health in self.health.items() if health > 0)

    def snapshot_for_actor(self, actor_guid: str, *, time_ms: int | None = None) -> dict[str, Any]:
        guid = _text(actor_guid, "runtime actor guid")
        if guid not in self.actors:
            raise ChronicleExternalTeammateResponseModelV1Error("unknown runtime actor")
        now = self.time_ms if time_ms is None else _count(time_ms, "runtime time_ms")
        if now != self.time_ms:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "advance runtime to the exact query time before taking a prefix snapshot"
            )
        return self._replay.snapshot(
            actor_guid=guid,
            time_ms=now,
            cutoff_order_key=None,
            cutoff_semantics="LIVE_SIMULATOR_PREFIX_AT_QUERY_TIME",
        )

    def advance_to(self, time_ms: int) -> None:
        """Advance an idle live prefix; intervening events must be applied first."""

        now = _count(time_ms, "runtime advance time_ms")
        if now < self.time_ms:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "runtime advance cannot move backwards"
            )
        self.time_ms = now
        self._replay._prune(now)

    def _resolve_target(
        self, actor_guid: str, target_mode: str, rng: random.Random
    ) -> str | None:
        alive = self.alive_target_guids()
        if not alive or target_mode in {"NO_TARGET", "NON_HOSTILE_OR_UNKNOWN"}:
            return None
        current = self.actors[actor_guid].current_target_guid
        if target_mode == "STAY_ALIVE" and current in alive:
            return current
        alternatives = [guid for guid in alive if guid != current]
        if target_mode == "SWITCH_ALIVE" and alternatives:
            return alternatives[rng.randrange(len(alternatives))]
        if current in alive:
            return current
        return alive[rng.randrange(len(alive))]

    def apply_event(
        self,
        *,
        time_ms: int,
        actor_guid: str,
        event_type: str,
        spell_id: int | None,
        spell_name: str | None,
        attribution_kind: str,
        exact_source_guid: str | None,
        target_mode: str,
        requested_damage: int | float,
        rng: random.Random,
        actor_role: str,
        explicit_target_guid: str | None = None,
    ) -> dict[str, Any]:
        now = _count(time_ms, "runtime event time_ms")
        if now < self.time_ms:
            raise ChronicleExternalTeammateResponseModelV1Error(
                "runtime event time regressed"
            )
        guid = _text(actor_guid, "runtime actor guid")
        if guid not in self.actors:
            raise ChronicleExternalTeammateResponseModelV1Error("unknown runtime actor")
        if event_type not in EVENT_TYPES:
            raise ChronicleExternalTeammateResponseModelV1Error("unsupported runtime event type")
        damage = float(requested_damage)
        if not math.isfinite(damage) or damage < 0 or (event_type != "DMG" and damage != 0):
            raise ChronicleExternalTeammateResponseModelV1Error(
                "runtime damage is invalid for event type"
            )
        requested_target = _optional_text(explicit_target_guid)
        target_guid = (
            requested_target
            if requested_target in self.alive_target_guids()
            else self._resolve_target(guid, target_mode, rng)
        )
        applied = 0.0
        killed = False
        if event_type == "DMG" and target_guid is not None:
            applied = min(damage, self.health[target_guid])
            self.health[target_guid] -= applied
            killed = self.health[target_guid] <= 0
        self.time_ms = now
        source_guid = exact_source_guid or guid
        synthetic_trace = {
            "trace_kind": "EXACT_PLAYER_EVENT",
            "player_guid": guid,
            "anchor": {"offset_ms": now},
            "event": {
                "event_type": event_type,
                "source": {"guid": source_guid, "lane": "FRIENDLY_PLAYER"},
                "target": {
                    "guid": target_guid,
                    "lane": "HOSTILE_CREATURE" if target_guid else "UNKNOWN",
                    "voting_enemy_target": target_guid is not None,
                },
                "spell": {"id": spell_id, "name": spell_name},
                "attribution": {
                    "attribution_kind": attribution_kind,
                    "player_guid": guid,
                },
                **(
                    {"damage": {"amount": int(round(applied)), "amount_source": "DMG_ONLY"}}
                    if event_type == "DMG"
                    else {}
                ),
                **(
                    {"healing": {"amount": 0, "amount_source": "HEAL_ONLY"}}
                    if event_type == "HEAL"
                    else {}
                ),
            },
        }
        self._replay.observe(synthetic_trace)
        self.actors[guid].current_target_guid = target_guid
        retargets: list[dict[str, str | None]] = []
        if killed and target_guid is not None:
            self._replay._observe_target(target_guid, "HOSTILE_CREATURE", now, dead=True)
            alive = self.alive_target_guids()
            for other_guid, actor in sorted(self.actors.items()):
                if actor.current_target_guid == target_guid:
                    # A death invalidates the assignment.  The next live
                    # emission resolves its sampled target mode against the
                    # then-current alive set; GUID sort order is not a policy.
                    actor.current_target_guid = None
                    retargets.append(
                        {
                            "actor_guid": other_guid,
                            "dead_target_guid": target_guid,
                            "retargeted_to": None,
                        }
                    )
            if not alive:
                self.kill_clock_ms = now
        return {
            "time_ms": now,
            "actor_guid": guid,
            "actor_role": actor_role,
            "event_type": event_type,
            "target_guid": target_guid,
            "requested_damage": damage,
            "applied_damage": applied,
            "overkill_damage": damage - applied,
            "killed": killed,
            "retargets": retargets,
            "all_targets_dead": self.kill_clock_ms is not None,
            "kill_clock_ms": self.kill_clock_ms,
        }

    def propose_delay(
        self,
        model: HierarchicalMarkedSemiMarkovV1,
        *,
        actor_guid: str,
        rng: random.Random,
    ) -> dict[str, Any]:
        actor = self.actors[_text(actor_guid, "runtime actor guid")]
        sampled = model.sample_delay(
            actor=actor.metadata,
            timing_state=self.snapshot_for_actor(actor_guid),
            rng=rng,
        )
        return {**sampled, "actor_guid": actor_guid, "proposed_time_ms": self.time_ms + sampled["delay_ms"]}

    def sample_emission(
        self,
        model: HierarchicalMarkedSemiMarkovV1,
        *,
        actor_guid: str,
        time_ms: int,
        rng: random.Random,
    ) -> dict[str, Any]:
        actor = self.actors[_text(actor_guid, "runtime actor guid")]
        self.advance_to(time_ms)
        state = self.snapshot_for_actor(actor_guid)
        return model.sample_emission(actor=actor.metadata, emission_state=state, rng=rng)


def build_remote_training_plan_v1(
    stage5_manifest: Mapping[str, Any],
    split: Mapping[str, Any],
    *,
    nodes: Sequence[str] = ("node001", "node002", "node003", "node004", "node005", "node006"),
    root_protocol_reviewed: bool = False,
    exact_dynamic_adapter_materialized_pass: bool = False,
) -> dict[str, Any]:
    """Prepare, but never launch, the server-side map/reduce job."""

    stage5 = _mapping(stage5_manifest, "Stage-5 manifest")
    if stage5.get("schema") != wave_model_v2.SCHEMA:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "unsupported Stage-5 team-wave manifest"
        )
    split_doc = _mapping(split, "response split")
    if split_doc.get("schema") != SPLIT_SCHEMA:
        raise ChronicleExternalTeammateResponseModelV1Error("unsupported response split")
    split_source = _mapping(split_doc.get("source_stage5"), "split source_stage5")
    stage5_address = _mapping(stage5.get("content_address"), "Stage-5 content_address")
    split_stage5_schema = _text(split_source.get("schema"), "split Stage-5 schema")
    split_stage5_sha = _text(
        split_source.get("content_sha256"), "split Stage-5 content_sha256"
    )
    stage5_sha = _text(stage5_address.get("sha256"), "Stage-5 content sha256")
    if split_stage5_schema != stage5.get("schema") or split_stage5_sha != stage5_sha:
        raise ChronicleExternalTeammateResponseModelV1Error(
            "response split is not bound to this Stage-5 manifest"
        )
    node_names = [_text(value, "scheduler node") for value in nodes]
    if not node_names or len(node_names) != len(set(node_names)):
        raise ChronicleExternalTeammateResponseModelV1Error(
            "scheduler nodes must be non-empty and unique"
        )
    assignment_by_instance: dict[str, str] = {}
    for component in _array(split_doc.get("components"), "split components"):
        component_row = _mapping(component, "split component")
        for instance_id in _array(component_row.get("instance_ids"), "component instance_ids"):
            assignment_by_instance[_text(instance_id, "split instance_id")] = _text(
                component_row.get("split"), "component split"
            )
    candidates: list[dict[str, Any]] = []
    for raw in _array(stage5.get("instances"), "Stage-5 instances"):
        entry = _mapping(raw, "Stage-5 instance")
        instance_id = _text(entry.get("instance_id"), "Stage-5 instance_id")
        fold = assignment_by_instance.get(instance_id)
        if fold is None:
            continue
        partition = _mapping(entry.get("partition"), "instance partition")
        candidates.append(
            {
                "instance_id": instance_id,
                "split": fold,
                "partition_path": _text(partition.get("path"), "partition path"),
                "compressed_size_bytes": _count(
                    partition.get("compressed_size_bytes"), "compressed_size_bytes"
                ),
                "record_count": _count(partition.get("record_count"), "record_count"),
                "exact_event_count": _count(
                    _mapping(entry.get("summary"), "instance summary").get(
                        "exact_event_count"
                    ),
                    "exact_event_count",
                ),
            }
        )
    loads = {node: 0 for node in node_names}
    assignments = {node: [] for node in node_names}
    for item in sorted(
        candidates, key=lambda row: (-row["compressed_size_bytes"], row["instance_id"])
    ):
        node = min(node_names, key=lambda value: (loads[value], value))
        assignments[node].append(item)
        loads[node] += item["compressed_size_bytes"]
    blockers = []
    if not exact_dynamic_adapter_materialized_pass:
        blockers.append("EXACT_DYNAMIC_ADAPTER_MATERIALIZED_ARTIFACT_PASS_PENDING")
    if not root_protocol_reviewed:
        blockers.append("ROOT_PROTOCOL_REVIEW_PENDING")
    return {
        "schema": REMOTE_PLAN_SCHEMA,
        "status": "PREPARED_NOT_EXECUTED" if not blockers else "BLOCKED_NOT_EXECUTED",
        "model_choice": {
            "family": "HIERARCHICAL_MARKED_SEMI_MARKOV_EMPIRICAL_BACKOFF",
            "timing_head": "previous-actor-event causal state to inter-event delay bucket",
            "emission_heads": "live-prefix mark, target-mode, and damage buckets",
            "backoff_order": ["exact GUID", "class plus available spec", "class", "global"],
            "non_warrior_spec_status": "SPEC_NOT_AVAILABLE_FROM_CURRENT_ARTIFACT",
            "fixed_historical_schedule_is_model": False,
        },
        "execution": {
            "launched": False,
            "nodes": node_names,
            "whole_instance_task_count": len(candidates),
            "maximum_useful_concurrent_tasks": len(candidates),
            "threads_per_task": 1,
            "partition_scan_count_per_task": 1,
            "server_side_existing_stage5_partitions_reused": True,
            "stage5_partitions_copied_to_local": False,
            "map_output": "mergeable count tables only; no expanded row corpus",
            "reduce": "deterministic counter merge then development validation",
        },
        "assignments": [
            {
                "node": node,
                "task_count": len(assignments[node]),
                "compressed_input_bytes": loads[node],
                "tasks": assignments[node],
            }
            for node in node_names
        ],
        "cost": {
            "compressed_input_bytes": sum(row["compressed_size_bytes"] for row in candidates),
            "exact_event_rows_scanned_once": sum(row["exact_event_count"] for row in candidates),
            "instance_partitions": len(candidates),
            "local_heavy_compute": False,
        },
        "ablation": [
            ablation_variant_v1(variant_id)
            for variant_id in (ABLATION_A, ABLATION_B, ABLATION_C, ABLATION_D)
        ],
        "development_validation_metrics": [
            "next_mark_negative_log_likelihood",
            "inter_event_log_mae_and_bucket_calibration",
            "target_mode_accuracy_and_generated_dead_target_rate",
            "positive_damage_log_mae",
            "dynamic_rollout_team_kill_clock_calibration",
        ],
        "prerequisite_blockers": blockers,
        "scientific_boundary": {
            "old50_nonheldout": True,
            "development_validation_only": True,
            "comparison_eligible": False,
            "voting_eligible": False,
            "deployment_eligible": False,
            "heavy_training_started": False,
        },
    }


__all__ = [
    "ABLATION_A",
    "ABLATION_B",
    "ABLATION_C",
    "ABLATION_D",
    "ChronicleExternalTeammateResponseModelV1Error",
    "DynamicTeamRuntimeV1",
    "HierarchicalMarkedSemiMarkovV1",
    "MODEL_SCHEMA",
    "REMOTE_PLAN_SCHEMA",
    "ROW_SCHEMA",
    "SPLIT_SCHEMA",
    "STATUS",
    "SUFFICIENT_ROW_SCHEMA",
    "ablation_variant_v1",
    "build_component_split_v1",
    "build_remote_training_plan_v1",
    "compile_wave_response_rows_v1",
    "fixed_historical_exact_guid_schedule_control_v1",
    "iter_wave_response_rows_v1",
    "iter_wave_response_sufficient_rows_v1",
    "old50_instance_ids_from_overlay_manifest_v1",
]
