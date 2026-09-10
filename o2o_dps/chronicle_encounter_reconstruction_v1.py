"""Bounded Chronicle encounter and target-group reconstruction.

This module deliberately reconstructs only properties supported by the
normalized Chronicle event stream.  It never invents coordinates, exact target
health, effective armor, threat ownership, or Cat/Contra authorship.

The input is streamed once while retaining one small accumulator per selected
encounter. Normalized exports may interleave encounter IDs, so contiguity is
not required. The output contains aggregate records and source anchors; no
row-level copy is materialized.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable, Iterator, Mapping, Sequence


JSONMap = dict[str, Any]

DEFAULT_COMBAT_GAP_MS = 8_000
DEFAULT_ACTIVE_TOLERANCE_MS = 1_500

CORE_ACTIVITY_TYPES = frozenset({"DMG", "START", "GO", "FAIL", "DEAD"})
SUPPLEMENTAL_TYPES = frozenset({"AURA", "HEAL", "ABS", "DSP"})

# Creature GUIDs in the current normalized Turtle logs use the classic
# HighGuid creature prefix.  CLASS rows remain the primary semantic evidence;
# the prefix is retained as explicit inferred fallback, never as player/NPC
# authorship evidence.
CREATURE_GUID_RE = re.compile(r"^0xF130", re.IGNORECASE)
CREATURE_ENTRY_RE = re.compile(r"^0xF130([0-9A-F]{6})", re.IGNORECASE)
PLAYER_GUID_RE = re.compile(r"^0x0{4}", re.IGNORECASE)
STACK_RE = re.compile(r"stacks\s*=\s*(\d+)", re.IGNORECASE)
OWNER_RE = re.compile(r"owner=([0-9A-F]+)", re.IGNORECASE)

# Chronicle CLASS rows can omit owner= for player summons while still labeling
# the creature as hostile.  Entry 17252 is the classic/Turtle Felguard summon
# template and occurs in the supported raid corpus as a player-controlled pet.
# Keep this deliberately finite: a name match is not sufficient evidence and
# unknown summon templates remain unclassified rather than being guessed.
KNOWN_PLAYER_CONTROLLED_SUMMON_ENTRY_IDS = frozenset({17252})

ARMOR_AURA_NAMES = frozenset(
    {
        "sunder armor",
        "expose armor",
        "exposearmor",
        "faerie fire",
        "faerie fire (feral)",
        "curse of recklessness",
        "crystal yield",
        "bonereaver's edge",
        "annihilator",
    }
)

# Only same-batch melee/cleave effects support a relative-position assumption.
# Generic AoE is retained as temporal co-hit evidence but cannot establish that
# targets form one melee pile.
MELEE_STACK_EVIDENCE_NAMES = frozenset(
    {
        "whirlwind",
        "cleave",
        "blade flurry",
        "sweeping strikes",
        "thunder clap",
    }
)


class ReconstructionError(ValueError):
    """The normalized stream violates the V1 reconstruction contract."""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ReconstructionError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ReconstructionError(f"{field_name} must be an integer") from exc


def _nonnegative_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        rendered = str(value).strip().replace(",", "")
        if not rendered or rendered in {"-", "—"}:
            return None
        try:
            parsed = float(rendered)
        except ValueError:
            return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _anchor(row: Mapping[str, Any]) -> JSONMap:
    result: JSONMap = {
        "encounter": _text(row.get("encounter")),
        "event_index": _integer(row.get("event_index"), field_name="event_index"),
        "offset_ms": _integer(row.get("offset_ms"), field_name="offset_ms"),
        "type": _text(row.get("type")),
    }
    provenance = row.get("provenance")
    if isinstance(provenance, Mapping):
        for key in ("source_file", "raw_file", "csv_line", "export_row"):
            if provenance.get(key) is not None:
                result[key] = provenance[key]
    return result


def _guid(value: Any) -> str | None:
    rendered = _text(value)
    if not rendered:
        return None
    if rendered[:2].casefold() == "0x":
        return "0x" + rendered[2:].upper()
    return rendered.upper()


def _is_creature_guid(value: str | None) -> bool:
    return bool(value and CREATURE_GUID_RE.match(value))


def _is_player_guid(value: str | None) -> bool:
    return bool(value and PLAYER_GUID_RE.match(value))


def _creature_entry_id(value: str | None) -> int | None:
    if not value:
        return None
    match = CREATURE_ENTRY_RE.match(value)
    return int(match.group(1), 16) if match else None


def _is_known_player_controlled_summon(value: str | None) -> bool:
    return _creature_entry_id(value) in KNOWN_PLAYER_CONTROLLED_SUMMON_ENTRY_IDS


def _row_guid_names(row: Mapping[str, Any]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for side in ("source", "target"):
        guid = _guid(row.get(f"{side}_guid"))
        if guid:
            result[guid] = _text(row.get(side))
    return result


def _classification_kind(row: Mapping[str, Any]) -> str | None:
    if str(row.get("type") or "").upper() != "CLASS":
        return None
    searchable = " ".join(
        str(row.get(key) or "")
        for key in ("source", "target", "spell", "outcome", "value")
    ).casefold()
    if "friendly player" in searchable:
        return "FRIENDLY_PLAYER"
    if "friendly creature" in searchable or "friendly object" in searchable:
        return "FRIENDLY_ENTITY"
    if "hostile creature" in searchable:
        return "HOSTILE_CREATURE"
    return None


def _classification_owner_suffix(row: Mapping[str, Any]) -> str | None:
    if str(row.get("type") or "").upper() != "CLASS":
        return None
    match = OWNER_RE.search(_text(row.get("outcome")) or "")
    return match.group(1).upper() if match else None


def _armor_aura_key(row: Mapping[str, Any]) -> str | None:
    spell = _text(row.get("spell"))
    if not spell:
        return None
    normalized = spell.casefold()
    if normalized in ARMOR_AURA_NAMES:
        return spell
    return None


def _aura_transition(row: Mapping[str, Any]) -> tuple[str, int] | None:
    outcome = (_text(row.get("outcome")) or "").casefold()
    if any(token in outcome for token in ("removed", "faded", "dispelled")):
        return ("remove", 0)
    stack_match = STACK_RE.search(outcome)
    stack = int(stack_match.group(1)) if stack_match else None
    if stack is None:
        numeric = _nonnegative_number(row.get("value"))
        if numeric is not None and numeric.is_integer():
            stack = int(numeric)
    if stack == 0:
        return ("remove", 0)
    if any(
        token in outcome
        for token in ("added", "applied", "increased", "refreshed", "stack")
    ):
        return ("set", stack or 1)
    return None


@dataclass
class ArmorTracker:
    active: dict[str, int] = field(default_factory=dict)
    interval_start_ms: int | None = None
    interval_anchor: JSONMap | None = None
    strata: list[JSONMap] = field(default_factory=list)
    transitions: list[JSONMap] = field(default_factory=list)

    def observe(self, row: Mapping[str, Any]) -> None:
        aura = _armor_aura_key(row)
        transition = _aura_transition(row) if aura else None
        if aura is None or transition is None:
            return
        offset_ms = _integer(row.get("offset_ms"), field_name="offset_ms")
        if self.interval_start_ms is not None and offset_ms >= self.interval_start_ms:
            self._close(offset_ms)
        action, stacks = transition
        if action == "remove":
            self.active.pop(aura, None)
        else:
            self.active[aura] = stacks
        anchor = _anchor(row)
        self.interval_start_ms = offset_ms
        self.interval_anchor = anchor
        self.transitions.append(
            {
                "aura": aura,
                "operation": action,
                "stacks": stacks,
                "status": "OBSERVED",
                "anchor": anchor,
            }
        )

    def _close(self, end_ms: int) -> None:
        assert self.interval_start_ms is not None
        if end_ms < self.interval_start_ms:
            raise ReconstructionError("armor stratum end precedes start")
        self.strata.append(
            {
                "start_offset_ms": self.interval_start_ms,
                "end_offset_ms": end_ms,
                "observed_active_armor_auras": [
                    {"name": name, "stacks": stacks}
                    for name, stacks in sorted(self.active.items())
                ],
                "status": "RECONSTRUCTED",
                "completeness": "PARTIAL_EVENT_PREFIX",
                "effective_armor": {
                    "value": None,
                    "status": "MISSING",
                    "reason": "Chronicle aura events do not expose numeric post-debuff armor",
                },
                "start_transition_anchor": self.interval_anchor,
            }
        )

    def finish(self, end_ms: int) -> None:
        if self.interval_start_ms is not None:
            self._close(max(end_ms, self.interval_start_ms))
            self.interval_start_ms = None
            self.interval_anchor = None


@dataclass
class TargetAccumulator:
    guid: str
    name: str | None = None
    classification_evidence: str = "UNCLASSIFIED"
    classification_anchor: JSONMap | None = None
    first_anchor: JSONMap | None = None
    last_anchor: JSONMap | None = None
    first_offset_ms: int | None = None
    last_offset_ms: int | None = None
    incoming_damage_sum: float = 0.0
    incoming_damage_count: int = 0
    incoming_damage_unparsed_count: int = 0
    outgoing_damage_sum: float = 0.0
    outgoing_damage_count: int = 0
    observed_healing_sum: float = 0.0
    observed_healing_count: int = 0
    death_anchor: JSONMap | None = None
    damage_sum_at_death: float | None = None
    unparsed_damage_count_at_death: int | None = None
    healing_sum_at_death: float | None = None
    largest_prior_incoming_hit: float = 0.0
    largest_survival_witness_hit: float = 0.0
    survival_witness_anchor: JSONMap | None = None
    overkill_or_value_semantics_warning: bool = False
    armor: ArmorTracker = field(default_factory=ArmorTracker)

    def touch(self, row: Mapping[str, Any], name: str | None = None) -> None:
        anchor = _anchor(row)
        offset_ms = anchor["offset_ms"]
        if self.first_anchor is None:
            self.first_anchor = anchor
            self.first_offset_ms = offset_ms
        self.last_anchor = anchor
        self.last_offset_ms = offset_ms
        if name:
            self.name = name

    def record_incoming_damage(self, row: Mapping[str, Any]) -> None:
        value = _nonnegative_number(row.get("value"))
        self.touch(row, _text(row.get("target")))
        if value is None:
            self.incoming_damage_unparsed_count += 1
            return
        self.incoming_damage_sum += value
        self.incoming_damage_count += 1
        self.largest_prior_incoming_hit = max(self.largest_prior_incoming_hit, value)
        outcome = (_text(row.get("outcome")) or "").casefold()
        flags = {str(value).casefold() for value in row.get("flags", []) if value}
        if "overkill" in outcome or "overkill" in flags:
            self.overkill_or_value_semantics_warning = True

    def record_outgoing_damage(self, row: Mapping[str, Any]) -> None:
        value = _nonnegative_number(row.get("value"))
        self.touch(row, _text(row.get("source")))
        if value is not None:
            self.outgoing_damage_sum += value
            self.outgoing_damage_count += 1
        offset_ms = _integer(row.get("offset_ms"), field_name="offset_ms")
        if (
            self.largest_prior_incoming_hit > self.largest_survival_witness_hit
            and self.last_offset_ms is not None
            and offset_ms >= self.last_offset_ms
        ):
            self.largest_survival_witness_hit = self.largest_prior_incoming_hit
            self.survival_witness_anchor = _anchor(row)

    def record_source_action_survival_witness(self, row: Mapping[str, Any]) -> None:
        self.touch(row, _text(row.get("source")))
        if self.largest_prior_incoming_hit > self.largest_survival_witness_hit:
            self.largest_survival_witness_hit = self.largest_prior_incoming_hit
            self.survival_witness_anchor = _anchor(row)

    def record_healing(self, row: Mapping[str, Any]) -> None:
        value = _nonnegative_number(row.get("value"))
        self.touch(row, _text(row.get("target")))
        if value is not None:
            self.observed_healing_sum += value
            self.observed_healing_count += 1

    def record_death(self, row: Mapping[str, Any]) -> None:
        if self.death_anchor is not None:
            return
        self.touch(row, _text(row.get("target")) or _text(row.get("source")))
        self.death_anchor = _anchor(row)
        self.damage_sum_at_death = self.incoming_damage_sum
        self.unparsed_damage_count_at_death = self.incoming_damage_unparsed_count
        self.healing_sum_at_death = self.observed_healing_sum

    def finish(self, wave_end_ms: int) -> JSONMap:
        self.armor.finish(wave_end_ms)
        if self.largest_survival_witness_hit > 0:
            strict_bound: JSONMap = {
                "value": _render_number(self.largest_survival_witness_hit),
                "status": "RECONSTRUCTED",
                "method": "largest incoming DMG followed by a later observed action from the same creature",
                "survival_witness_anchor": self.survival_witness_anchor,
                "limitation": "normalized DMG effective/overkill semantics are not independently calibrated",
            }
        else:
            strict_bound = {
                "value": None,
                "status": "MISSING",
                "reason": "no nonterminal incoming hit has a later creature-action survival witness",
            }
        kill_proxy: JSONMap
        if self.death_anchor is not None and self.damage_sum_at_death is not None:
            complete = (self.unparsed_damage_count_at_death or 0) == 0
            kill_proxy = {
                "value": _render_number(self.damage_sum_at_death),
                "status": "OBSERVED",
                "completeness": (
                    "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS"
                    if complete
                    else "PARTIAL_UNPARSED_DAMAGE_VALUES"
                ),
                "unparsed_damage_event_count": self.unparsed_damage_count_at_death or 0,
                "observed_healing_to_death": _render_number(self.healing_sum_at_death or 0.0),
                "death_anchor": self.death_anchor,
                "interpretation": "observed incoming damage sum through first DEAD",
                "not_equal_to": "exact maximum or initial health",
            }
        else:
            kill_proxy = {
                "value": None,
                "status": "MISSING",
                "reason": "no DEAD event for this target in the reconstructed wave",
            }
        armor_status = "RECONSTRUCTED" if self.armor.transitions else "MISSING"
        return {
            "target_guid": self.guid,
            "creature_entry_id": _creature_entry_id(self.guid),
            "target_name": self.name,
            "classification": {
                "value": "Hostile Creature",
                "status": (
                    "OBSERVED"
                    if self.classification_evidence == "CLASS_HOSTILE_CREATURE"
                    else "INFERRED"
                ),
                "evidence": self.classification_evidence,
                "anchor": self.classification_anchor,
            },
            "activity_interval": {
                "first_offset_ms": self.first_offset_ms,
                "last_offset_ms": self.last_offset_ms,
                "status": "OBSERVED",
                "first_anchor": self.first_anchor,
                "last_anchor": self.last_anchor,
            },
            "observed_incoming_damage_sum": {
                "value": _render_number(self.incoming_damage_sum),
                "event_count": self.incoming_damage_count,
                "unparsed_event_count": self.incoming_damage_unparsed_count,
                "status": "OBSERVED",
                "completeness": (
                    "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS"
                    if self.incoming_damage_unparsed_count == 0
                    else "PARTIAL_UNPARSED_DAMAGE_VALUES"
                ),
            },
            "observed_outgoing_damage_sum": {
                "value": _render_number(self.outgoing_damage_sum),
                "event_count": self.outgoing_damage_count,
                "status": "OBSERVED",
            },
            "observed_healing_received_sum": {
                "value": _render_number(self.observed_healing_sum),
                "event_count": self.observed_healing_count,
                "status": "OBSERVED",
            },
            "confirmed_single_hit_health_lower_bound": strict_bound,
            "kill_budget_proxy": kill_proxy,
            "armor_debuff_evidence": {
                "status": armor_status,
                "transitions": self.armor.transitions,
                "strata": self.armor.strata,
                "effective_armor": {
                    "value": None,
                    "status": "MISSING",
                },
            },
            "warnings": (
                ["observed overkill marker; damage sums must not be treated as health"]
                if self.overkill_or_value_semantics_warning
                else []
            ),
        }


def _pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _components(nodes: Sequence[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for left, right in edges:
        if left not in adjacency or right not in adjacency:
            continue
        adjacency[left].add(right)
        adjacency[right].add(left)
    result: list[list[str]] = []
    seen: set[str] = set()
    for root in sorted(nodes):
        if root in seen:
            continue
        queue = [root]
        seen.add(root)
        component: list[str] = []
        while queue:
            current = queue.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        result.append(sorted(component))
    return result


@dataclass
class WaveAccumulator:
    encounter_id: str
    ordinal: int
    start_offset_ms: int
    first_anchor: JSONMap
    hostile_evidence: dict[str, JSONMap]
    friendly_entities: set[str]
    classification_conflicts: set[str]
    hostile_class_names: dict[str, str | None]
    active_tolerance_ms: int
    end_offset_ms: int = 0
    last_core_activity_ms: int = 0
    last_anchor: JSONMap | None = None
    event_count: int = 0
    targets: dict[str, TargetAccumulator] = field(default_factory=dict)
    cohit_buckets: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict)
    recent_target_activity: deque[tuple[int, str]] = field(default_factory=deque)
    concurrent_edges: dict[tuple[str, str], JSONMap] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.end_offset_ms = self.start_offset_ms
        self.last_core_activity_ms = self.start_offset_ms
        self.last_anchor = self.first_anchor

    def _target(self, guid: str, row: Mapping[str, Any]) -> TargetAccumulator:
        evidence = self.hostile_evidence.get(guid)
        if evidence is None or guid in self.friendly_entities or guid in self.classification_conflicts:
            raise ReconstructionError(f"attempted to materialize non-hostile target {guid}")
        if guid not in self.targets:
            names = _row_guid_names(row)
            self.targets[guid] = TargetAccumulator(
                guid=guid,
                name=names.get(guid) or self.hostile_class_names.get(guid),
                classification_evidence=str(evidence["kind"]),
                classification_anchor=evidence.get("anchor"),
            )
        target = self.targets[guid]
        target.classification_evidence = str(evidence["kind"])
        target.classification_anchor = evidence.get("anchor")
        return target

    def _hostile_guids(self, row: Mapping[str, Any]) -> list[str]:
        result: list[str] = []
        for guid in _row_guid_names(row):
            if (
                guid in self.hostile_evidence
                and guid not in self.friendly_entities
                and guid not in self.classification_conflicts
            ):
                result.append(guid)
        return sorted(set(result))

    def _record_temporal_activity(self, offset_ms: int, guids: Sequence[str], row: Mapping[str, Any]) -> None:
        cutoff = offset_ms - self.active_tolerance_ms
        while self.recent_target_activity and self.recent_target_activity[0][0] < cutoff:
            self.recent_target_activity.popleft()
        for guid in guids:
            for prior_offset, prior_guid in self.recent_target_activity:
                if prior_guid == guid:
                    continue
                key = _pair(guid, prior_guid)
                evidence = self.concurrent_edges.get(key)
                if evidence is None:
                    self.concurrent_edges[key] = {
                        "count": 1,
                        "first_anchor": _anchor(row),
                        "max_observed_separation_ms": offset_ms - prior_offset,
                    }
                else:
                    evidence["count"] += 1
                    evidence["max_observed_separation_ms"] = max(
                        evidence["max_observed_separation_ms"],
                        offset_ms - prior_offset,
                    )
            self.recent_target_activity.append((offset_ms, guid))

    def observe(self, row: Mapping[str, Any], *, core_activity: bool) -> None:
        event_type = str(row.get("type") or "").upper()
        offset_ms = _integer(row.get("offset_ms"), field_name="offset_ms")
        if offset_ms < self.end_offset_ms:
            raise ReconstructionError("offset_ms moved backward inside encounter wave")
        self.end_offset_ms = offset_ms
        self.last_anchor = _anchor(row)
        self.event_count += 1
        hostile_guids = self._hostile_guids(row)
        if core_activity:
            self.last_core_activity_ms = offset_ms
            self._record_temporal_activity(offset_ms, hostile_guids, row)

        source_guid = _guid(row.get("source_guid"))
        target_guid = _guid(row.get("target_guid"))
        for guid in hostile_guids:
            target = self._target(guid, row)
            name = _row_guid_names(row).get(guid)
            target.touch(row, name)

        # Chronicle can serialize a killing blow as DEAD with a numeric value,
        # but many DEAD rows are terminal markers with no damage value. Retain
        # the former and do not count the latter as unparsed damage/co-hit.
        damage_bearing = event_type == "DMG" or (
            event_type == "DEAD"
            and _nonnegative_number(row.get("value")) is not None
        )
        if damage_bearing:
            if target_guid in hostile_guids:
                self._target(target_guid, row).record_incoming_damage(row)
                spell_name = (_text(row.get("spell")) or "unknown").casefold()
                spell_key = row.get("spell_id") if row.get("spell_id") is not None else spell_name
                source_key = source_guid or _text(row.get("source")) or "unknown"
                key = (offset_ms, source_key, spell_key)
                bucket = self.cohit_buckets.setdefault(
                    key,
                    {
                        "offset_ms": offset_ms,
                        "source_guid": source_guid,
                        "source_name": _text(row.get("source")),
                        "spell_id": row.get("spell_id"),
                        "spell_name": _text(row.get("spell")),
                        "targets": set(),
                        "first_anchor": _anchor(row),
                    },
                )
                bucket["targets"].add(target_guid)
            if source_guid in hostile_guids:
                self._target(source_guid, row).record_outgoing_damage(row)
        elif event_type in {"START", "GO"} and source_guid in hostile_guids:
            self._target(source_guid, row).record_source_action_survival_witness(row)
        elif event_type == "HEAL" and target_guid in hostile_guids:
            self._target(target_guid, row).record_healing(row)
        elif event_type == "AURA" and target_guid in hostile_guids:
            self._target(target_guid, row).armor.observe(row)
        if event_type == "DEAD":
            death_guid: str | None = None
            if target_guid in hostile_guids:
                death_guid = target_guid
            elif target_guid is None and source_guid in hostile_guids:
                death_guid = source_guid
            if death_guid:
                self._target(death_guid, row).record_death(row)

    def finish(self) -> JSONMap:
        eligible_targets = {
            guid
            for guid in self.targets
            if guid in self.hostile_evidence
            and guid not in self.friendly_entities
            and guid not in self.classification_conflicts
        }
        rendered_targets = [
            self.targets[guid].finish(self.end_offset_ms) for guid in sorted(eligible_targets)
        ]
        temporal_groups: list[JSONMap] = []
        melee_edges: set[tuple[str, str]] = set()
        for bucket in sorted(
            self.cohit_buckets.values(),
            key=lambda value: (
                value["offset_ms"],
                str(value.get("source_guid") or value.get("source_name") or ""),
                str(value.get("spell_id") or value.get("spell_name") or ""),
            ),
        ):
            targets = sorted(set(bucket["targets"]) & eligible_targets)
            if len(targets) < 2:
                continue
            spell_name = (_text(bucket.get("spell_name")) or "").casefold()
            melee_reach = spell_name in MELEE_STACK_EVIDENCE_NAMES
            if melee_reach:
                for left_index, left in enumerate(targets):
                    for right in targets[left_index + 1 :]:
                        melee_edges.add(_pair(left, right))
            temporal_groups.append(
                {
                    "offset_ms": bucket["offset_ms"],
                    "source_guid": bucket.get("source_guid"),
                    "source_name": bucket.get("source_name"),
                    "spell_id": bucket.get("spell_id"),
                    "spell_name": bucket.get("spell_name"),
                    "target_guids": targets,
                    "status": "OBSERVED",
                    "relation": (
                        "MELEE_REACH_COHIT"
                        if melee_reach
                        else "TEMPORAL_COHIT_ONLY"
                    ),
                    "first_anchor": bucket["first_anchor"],
                }
            )

        target_guids = sorted(eligible_targets)
        components = _components(target_guids, melee_edges)
        rendered_groups: list[JSONMap] = []
        for index, component in enumerate(components, start=1):
            component_edges = [
                edge for edge in sorted(melee_edges) if set(edge).issubset(component)
            ]
            stacked = len(component) >= 2 and bool(component_edges)
            rendered_groups.append(
                {
                    "group_id": f"wave-{self.ordinal}-group-{index}",
                    "target_guids": component,
                    "position_assumption": {
                        "value": "stacked" if stacked else "unknown",
                        "status": "INFERRED" if stacked else "MISSING",
                        "basis": (
                            "connected same-batch melee/cleave co-hit evidence"
                            if stacked
                            else "no positive relative-position evidence"
                        ),
                        "coordinates": None,
                    },
                }
            )
        if len(target_guids) > 1 and len(components) == 1 and melee_edges:
            wave_position = {
                "value": "stacked",
                "status": "INFERRED",
                "basis": "all observed targets connected by same-batch melee/cleave co-hit evidence",
                "coordinates": None,
            }
        else:
            wave_position = {
                "value": "unknown",
                "status": "MISSING",
                "basis": "absence of cross-target melee co-hit cannot prove spatial separation",
                "coordinates": None,
            }
        concurrent = [
            {
                "target_guids": list(edge),
                "status": "RECONSTRUCTED",
                **evidence,
            }
            for edge, evidence in sorted(self.concurrent_edges.items())
            if set(edge).issubset(eligible_targets)
        ]
        return {
            "wave_id": f"{self.encounter_id}:wave:{self.ordinal}",
            "ordinal": self.ordinal,
            "start_offset_ms": self.start_offset_ms,
            "end_offset_ms": self.end_offset_ms,
            "duration_ms": self.end_offset_ms - self.start_offset_ms,
            "status": "RECONSTRUCTED",
            "boundary_method": "hostile core-activity gap",
            "event_count": self.event_count,
            "target_count": len(rendered_targets),
            "first_anchor": self.first_anchor,
            "last_anchor": self.last_anchor,
            "targets": rendered_targets,
            "simultaneously_active_pairs": concurrent,
            "same_batch_damage_groups": temporal_groups,
            "target_groups": rendered_groups,
            "position_assumption": wave_position,
            "separate_position": {
                "value": None,
                "status": "MISSING",
                "reason": "normalized Chronicle schema has no coordinates, facing, or range; no V1 event is positive separation evidence",
            },
        }


@dataclass
class EncounterAccumulator:
    instance: str
    encounter_id: str
    combat_gap_ms: int
    active_tolerance_ms: int
    hostile_evidence: dict[str, JSONMap] = field(default_factory=dict)
    friendly_players: set[str] = field(default_factory=set)
    friendly_entities: set[str] = field(default_factory=set)
    friendly_owned_entities: set[str] = field(default_factory=set)
    known_player_controlled_summons: set[str] = field(default_factory=set)
    hostile_label_owner_overrides: set[str] = field(default_factory=set)
    entity_owner_suffixes: dict[str, str] = field(default_factory=dict)
    classification_conflicts: set[str] = field(default_factory=set)
    hostile_class_names: dict[str, str | None] = field(default_factory=dict)
    waves: list[JSONMap] = field(default_factory=list)
    current_wave: WaveAccumulator | None = None
    rows_seen: int = 0
    first_anchor: JSONMap | None = None
    last_anchor: JSONMap | None = None

    def _learn_classification(self, row: Mapping[str, Any]) -> None:
        kind = _classification_kind(row)
        if kind is None:
            return
        names = _row_guid_names(row)
        anchor = _anchor(row)
        owner_suffix = _classification_owner_suffix(row)
        for guid, name in names.items():
            if kind == "FRIENDLY_PLAYER":
                self.friendly_players.add(guid)
                player_suffix = guid[-6:].upper()
                for entity_guid, entity_owner in self.entity_owner_suffixes.items():
                    if entity_owner != player_suffix:
                        continue
                    prior = self.hostile_evidence.get(entity_guid)
                    if prior and prior.get("kind") == "CLASS_HOSTILE_CREATURE":
                        self.hostile_label_owner_overrides.add(entity_guid)
                    self.hostile_evidence.pop(entity_guid, None)
                    self.friendly_entities.add(entity_guid)
                    self.friendly_owned_entities.add(entity_guid)
                continue
            if _is_known_player_controlled_summon(guid):
                self.hostile_evidence.pop(guid, None)
                self.friendly_entities.add(guid)
                self.known_player_controlled_summons.add(guid)
                continue
            if owner_suffix:
                self.entity_owner_suffixes[guid] = owner_suffix
                if any(
                    player_guid[-6:].upper() == owner_suffix
                    for player_guid in self.friendly_players
                ):
                    if kind == "HOSTILE_CREATURE":
                        self.hostile_label_owner_overrides.add(guid)
                    self.hostile_evidence.pop(guid, None)
                    self.friendly_entities.add(guid)
                    self.friendly_owned_entities.add(guid)
                    continue
            if kind == "FRIENDLY_ENTITY":
                prior = self.hostile_evidence.get(guid)
                if prior and prior.get("kind") == "CLASS_HOSTILE_CREATURE":
                    self.classification_conflicts.add(guid)
                else:
                    self.hostile_evidence.pop(guid, None)
                self.friendly_entities.add(guid)
                continue
            if kind == "HOSTILE_CREATURE" and _is_creature_guid(guid):
                if guid in self.friendly_entities:
                    self.classification_conflicts.add(guid)
                    continue
                self.hostile_evidence[guid] = {
                    "kind": "CLASS_HOSTILE_CREATURE",
                    "status": "OBSERVED",
                    "anchor": anchor,
                }
                self.hostile_class_names[guid] = name

    def _infer_hostility_from_direct_damage(self, row: Mapping[str, Any]) -> None:
        event_type = str(row.get("type") or "").upper()
        if event_type not in {"DMG", "DEAD"}:
            return
        source_guid = _guid(row.get("source_guid"))
        target_guid = _guid(row.get("target_guid"))
        candidate: str | None = None
        direction: str | None = None
        if (
            source_guid in self.friendly_players
            and _is_creature_guid(target_guid)
        ):
            candidate = target_guid
            direction = "DIRECT_DAMAGE_FROM_CLASS_FRIENDLY_PLAYER"
        elif (
            target_guid in self.friendly_players
            and _is_creature_guid(source_guid)
        ):
            candidate = source_guid
            direction = "DIRECT_DAMAGE_TO_CLASS_FRIENDLY_PLAYER"
        if (
            candidate is None
            or candidate in self.friendly_entities
            or candidate in self.classification_conflicts
            or candidate in self.hostile_evidence
        ):
            return
        if _is_known_player_controlled_summon(candidate):
            self.hostile_evidence.pop(candidate, None)
            self.friendly_entities.add(candidate)
            self.known_player_controlled_summons.add(candidate)
            return
        self.hostile_evidence[candidate] = {
            "kind": direction,
            "status": "INFERRED",
            "anchor": _anchor(row),
        }
        self.hostile_class_names[candidate] = _row_guid_names(row).get(candidate)

    def _relevant_hostile_guids(self, row: Mapping[str, Any]) -> list[str]:
        result: list[str] = []
        names = _row_guid_names(row)
        for guid in names:
            if (
                guid in self.hostile_evidence
                and guid not in self.friendly_entities
                and guid not in self.classification_conflicts
            ):
                result.append(guid)
        return sorted(set(result))

    def observe(self, row: Mapping[str, Any]) -> None:
        self.rows_seen += 1
        anchor = _anchor(row)
        if self.first_anchor is None:
            self.first_anchor = anchor
        self.last_anchor = anchor
        self._learn_classification(row)
        self._infer_hostility_from_direct_damage(row)
        event_type = str(row.get("type") or "").upper()
        hostile_guids = self._relevant_hostile_guids(row)
        core = event_type in CORE_ACTIVITY_TYPES and bool(hostile_guids)
        supplemental = event_type in SUPPLEMENTAL_TYPES and bool(hostile_guids)
        if not core and not supplemental:
            return
        offset_ms = anchor["offset_ms"]
        if core:
            if (
                self.current_wave is not None
                and offset_ms - self.current_wave.last_core_activity_ms > self.combat_gap_ms
            ):
                self.waves.append(self.current_wave.finish())
                self.current_wave = None
            if self.current_wave is None:
                self.current_wave = WaveAccumulator(
                    encounter_id=self.encounter_id,
                    ordinal=len(self.waves) + 1,
                    start_offset_ms=offset_ms,
                    first_anchor=anchor,
                    hostile_evidence=self.hostile_evidence,
                    friendly_entities=self.friendly_entities,
                    classification_conflicts=self.classification_conflicts,
                    hostile_class_names=self.hostile_class_names,
                    active_tolerance_ms=self.active_tolerance_ms,
                )
        if self.current_wave is not None:
            self.current_wave.observe(row, core_activity=core)

    def finish(self) -> JSONMap:
        if self.current_wave is not None:
            self.waves.append(self.current_wave.finish())
            self.current_wave = None
        return {
            "instance": self.instance,
            "encounter": self.encounter_id,
            "status": "RECONSTRUCTED",
            "row_count": self.rows_seen,
            "wave_count": len(self.waves),
            "first_anchor": self.first_anchor,
            "last_anchor": self.last_anchor,
            "classification_summary": {
                "friendly_player_count": len(self.friendly_players),
                "friendly_entity_count": len(self.friendly_entities),
                "friendly_owned_entity_count": len(self.friendly_owned_entities),
                "known_player_controlled_summon_exclusion_count": len(
                    self.known_player_controlled_summons
                ),
                "hostile_label_overridden_by_friendly_owner_count": len(
                    self.hostile_label_owner_overrides
                ),
                "explicit_hostile_creature_count": sum(
                    evidence.get("kind") == "CLASS_HOSTILE_CREATURE"
                    for guid, evidence in self.hostile_evidence.items()
                    if guid not in self.friendly_entities
                    and guid not in self.classification_conflicts
                ),
                "inferred_hostile_creature_count": sum(
                    evidence.get("kind") != "CLASS_HOSTILE_CREATURE"
                    for guid, evidence in self.hostile_evidence.items()
                    if guid not in self.friendly_entities
                    and guid not in self.classification_conflicts
                ),
                "classification_conflict_count": len(self.classification_conflicts),
                "creature_guid_only_hostile_count": 0,
            },
            "waves": self.waves,
        }


def iter_normalized_rows(path: Path) -> Iterator[JSONMap]:
    with path.open("r", encoding="utf-8", buffering=1024 * 1024) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReconstructionError(
                    f"invalid JSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise ReconstructionError(f"normalized row {line_number} is not an object")
            yield value


def iter_reconstructed_encounters(
    rows: Iterable[Mapping[str, Any]],
    *,
    combat_gap_ms: int = DEFAULT_COMBAT_GAP_MS,
    active_tolerance_ms: int = DEFAULT_ACTIVE_TOLERANCE_MS,
    encounter_filter: str | None = None,
    max_encounters: int | None = None,
) -> Iterator[JSONMap]:
    if combat_gap_ms <= 0:
        raise ReconstructionError("combat_gap_ms must be positive")
    if active_tolerance_ms < 0:
        raise ReconstructionError("active_tolerance_ms must be nonnegative")
    if max_encounters is not None and max_encounters <= 0:
        raise ReconstructionError("max_encounters must be positive")
    accumulators: dict[tuple[str, str], EncounterAccumulator] = {}
    encounter_order: list[tuple[str, str]] = []
    for row in rows:
        instance = _text(row.get("instance"))
        encounter = _text(row.get("encounter"))
        if not instance or not encounter:
            raise ReconstructionError("normalized row is missing instance or encounter")
        key = (instance, encounter)
        if encounter_filter is not None and encounter != encounter_filter:
            continue
        current = accumulators.get(key)
        if current is None:
            if max_encounters is not None and len(encounter_order) >= max_encounters:
                continue
            current = EncounterAccumulator(
                instance=instance,
                encounter_id=encounter,
                combat_gap_ms=combat_gap_ms,
                active_tolerance_ms=active_tolerance_ms,
            )
            accumulators[key] = current
            encounter_order.append(key)
        current.observe(row)
    for key in encounter_order:
        yield accumulators[key].finish()


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ReconstructionError("cannot compute percentile of empty values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def build_npc_identity_aggregates(encounters: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    samples: dict[str, list[float]] = defaultdict(list)
    names: dict[str, set[str]] = defaultdict(set)
    entry_ids: dict[str, int | None] = {}
    for encounter in encounters:
        for wave in encounter.get("waves", []):
            if not isinstance(wave, Mapping):
                continue
            for target in wave.get("targets", []):
                if not isinstance(target, Mapping):
                    continue
                name = _text(target.get("target_name"))
                entry_id = target.get("creature_entry_id")
                kill_proxy = target.get("kill_budget_proxy")
                if not isinstance(kill_proxy, Mapping):
                    continue
                value = _nonnegative_number(kill_proxy.get("value"))
                if (
                    value is None
                    or value <= 0
                    or kill_proxy.get("completeness")
                    != "COMPLETE_FOR_NORMALIZED_DAMAGE_ROWS"
                ):
                    continue
                if isinstance(entry_id, int) and not isinstance(entry_id, bool):
                    key = f"entry:{entry_id}"
                    entry_ids[key] = entry_id
                elif name and not name.casefold().startswith("0x"):
                    key = f"name:{name.casefold()}"
                    entry_ids[key] = None
                else:
                    continue
                if name and not name.casefold().startswith("0x"):
                    names[key].add(name)
                samples[key].append(value)
    result: list[JSONMap] = []
    for key in sorted(samples):
        values = sorted(samples[key])
        count = len(values)
        stats = {
            "count": count,
            "minimum": _render_number(values[0]),
            "median": _render_number(float(statistics.median(values))),
            "q1": _render_number(_percentile(values, 0.25)),
            "q3": _render_number(_percentile(values, 0.75)),
            "maximum": _render_number(values[-1]),
        }
        if count >= 2:
            scenario: JSONMap = {
                "status": "INFERRED",
                "center": stats["median"],
                "range": [stats["q1"], stats["q3"]],
                "method": "median and IQR of repeated observed kill-budget proxies",
                "not_equal_to": "exact NPC health distribution",
            }
        else:
            scenario = {
                "status": "MISSING",
                "center": None,
                "range": None,
                "reason": "one observation cannot establish a repeatable health scenario",
            }
        result.append(
            {
                "identity": {
                    "creature_entry_id": entry_ids.get(key),
                    "target_name": sorted(names[key])[0] if names[key] else None,
                    "target_name_variants": sorted(names[key]),
                    "classification": "Hostile Creature",
                    "status": (
                        "RECONSTRUCTED" if entry_ids.get(key) is not None else "INFERRED"
                    ),
                    "limitation": (
                        "creature entry is decoded from the classic creature GUID; spawn identity is not retained"
                        if entry_ids.get(key) is not None
                        else "exact display name is not a globally unique NPC identity"
                    ),
                },
                "kill_budget_proxy_summary": stats,
                "health_scenario": scenario,
            }
        )
    return result


def reconstruct_file(
    normalized_path: Path,
    *,
    combat_gap_ms: int = DEFAULT_COMBAT_GAP_MS,
    active_tolerance_ms: int = DEFAULT_ACTIVE_TOLERANCE_MS,
    encounter_filter: str | None = None,
    max_encounters: int | None = None,
) -> JSONMap:
    resolved = normalized_path.expanduser().resolve()
    if not resolved.is_file():
        raise ReconstructionError(f"normalized input does not exist: {resolved}")
    encounters = list(
        iter_reconstructed_encounters(
            iter_normalized_rows(resolved),
            combat_gap_ms=combat_gap_ms,
            active_tolerance_ms=active_tolerance_ms,
            encounter_filter=encounter_filter,
            max_encounters=max_encounters,
        )
    )
    if not encounters:
        raise ReconstructionError("no matching encounter was reconstructed")
    selected = encounter_filter is not None or max_encounters is not None
    return {
        "schema_version": 1,
        "kind": "chronicle_encounter_reconstruction_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "normalized_file": str(resolved),
            "mode": "read_only_stream",
            "scope": "selected_encounters" if selected else "complete_file",
            "complete_file_scanned": True,
            "encounter_filter": encounter_filter,
            "max_encounters": max_encounters,
            "row_level_copy_written": False,
        },
        "parameters": {
            "combat_gap_ms": combat_gap_ms,
            "active_tolerance_ms": active_tolerance_ms,
            "encounter_contiguity_required": False,
            "interleaved_encounter_accumulators": True,
            "known_player_controlled_summon_entry_ids": sorted(
                KNOWN_PLAYER_CONTROLLED_SUMMON_ENTRY_IDS
            ),
        },
        "provenance_contract": {
            "OBSERVED": "direct normalized Chronicle row or arithmetic sum of those rows",
            "RECONSTRUCTED": "deterministic prefix transform with declared thresholds",
            "INFERRED": "explicit scenario assumption, never a direct log field",
            "MISSING": "not identifiable from the normalized stream",
            "authorship": "MISSING; Cat/Contra use is not inferred from player behavior",
            "hostility": "OBSERVED only from CLASS Hostile Creature without a CLASS Friendly Player owner; otherwise INFERRED only from direct DMG/DEAD interaction with a CLASS Friendly Player",
            "ownership_override": "a CLASS owner suffix resolving to a CLASS Friendly Player excludes the owned creature from hostile targets",
            "known_player_controlled_summons": "creature entry 17252 (Felguard summon) is excluded even when Chronicle omits owner=; the finite entry allowlist is corpus-supported and names alone never trigger exclusion",
            "creature_guid": "identity/entry decoding only; never sufficient hostility evidence",
        },
        "summary": {
            "encounter_count": len(encounters),
            "source_rows_included": sum(
                int(value.get("row_count", 0)) for value in encounters
            ),
            "wave_count": sum(int(value.get("wave_count", 0)) for value in encounters),
            "target_observation_count": sum(
                int(wave.get("target_count", 0))
                for encounter in encounters
                for wave in encounter.get("waves", [])
                if isinstance(wave, Mapping)
            ),
        },
        "encounters": encounters,
        "npc_identity_aggregates": build_npc_identity_aggregates(encounters),
        "simulation_contract": {
            "request_grain": "one simulator request per reconstructed wave and target group",
            "target_health": "use repeated kill-budget proxy median/IQR only as an INFERRED scenario; never as exact health",
            "target_armor": "MISSING until aura signatures are joined to calibrated base armor/effect magnitudes",
            "position": "stacked only for positive same-batch melee/cleave co-hit evidence; otherwise unknown",
            "separate": "not identified in V1; enumerate a separate scenario only as a downstream sensitivity analysis",
            "wowsims_limitation": "aggregate target-damage evaluation does not model per-target death; evaluate each wave/group separately",
        },
        "known_limitations": [
            "combat-gap waves are reconstructed boundaries, not Chronicle-observed pull labels",
            "kill-budget proxy can differ from health because of healing, regeneration, overkill semantics, or a truncated segment start",
            "same-batch generic AoE establishes temporal co-hit only, not exact distance",
            "absence of co-hit does not establish spatial separation",
            "threat, tank ownership, target switching intent, and addon authorship are not identified",
            "unknown creatures seen only through pets, totems, or other creatures are conservatively excluded until player-hostility evidence appears",
        ],
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--encounter")
    parser.add_argument("--max-encounters", type=int)
    parser.add_argument("--combat-gap-ms", type=int, default=DEFAULT_COMBAT_GAP_MS)
    parser.add_argument(
        "--active-tolerance-ms", type=int, default=DEFAULT_ACTIVE_TOLERANCE_MS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = reconstruct_file(
        args.normalized,
        combat_gap_ms=args.combat_gap_ms,
        active_tolerance_ms=args.active_tolerance_ms,
        encounter_filter=args.encounter,
        max_encounters=args.max_encounters,
    )
    _write_json(args.output.expanduser().resolve(), report)
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
