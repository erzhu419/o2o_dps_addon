"""Leakage-safe L2 temporal context for compact Fury decisions.

The join consumes the already compact Fury decision partitions and the small
Chronicle encounter reconstruction reports.  It never reopens normalized or
raw Chronicle rows.  One feature report is held in memory while its decision
partition is streamed; output is a compact sidecar keyed by ``decision_id`` so
the large decision row is not copied.

Only evidence available at or before the decision START is exposed.  Final
wave target counts, final current-wave duration, kill budgets, and final target
groups are deliberately not copied from the reconstruction report.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import tempfile
from typing import Any

from .state_reconstructor_v1 import validate_state_evidence_cutoff


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DECISION_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_fury_decision_dataset"
    / "v1"
    / "manifest.json"
)
DEFAULT_FEATURE_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_encounter_batch"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_fury_l2_temporal_join"
    / "v1"
)

ROW_SCHEMA = "chronicle_fury_l2_temporal_context/v1"
MANIFEST_KIND = "chronicle_fury_l2_temporal_join_manifest_v1"
MASK_VALUES = ("OBSERVED", "RECONSTRUCTED", "MISSING")

STRUCTURAL_FIELDS = (
    "wave",
    "wave_elapsed_ms",
    "previous_wave_duration_ms",
    "current_wave_final_duration_ms",
    "target_count",
    "alive_target_proxy",
    "pile_cohit_components",
)
HISTORY_FIELDS = (
    "recent_action_history",
    "player_aura_history",
    "outgoing_target_aura_history",
)
PROTECTED_MISSING_FIELDS = (
    "absolute_rage",
    "queue_intent",
    "gcd_remaining_ms",
    "cooldown_remaining_ms",
    "mainhand_swing_remaining_ms",
    "offhand_swing_remaining_ms",
    "target_hp",
    "player_hp",
)
ALL_FIELDS = STRUCTURAL_FIELDS + HISTORY_FIELDS + PROTECTED_MISSING_FIELDS

SOURCE_HISTORY_FIELDS = {
    "recent_action_history": "recent_uniquely_linked_server_actions",
    "player_aura_history": "known_player_aura_event_ledger",
    "outgoing_target_aura_history": "known_outgoing_target_aura_event_ledger",
}


class L2TemporalJoinError(ValueError):
    """Input artifacts do not satisfy the L2 temporal join contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise L2TemporalJoinError(f"{path} must be a mapping")
    return value


def _integer(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise L2TemporalJoinError(f"{path} must be an integer")
    return value


def _anchor_key(anchor: Mapping[str, Any], path: str) -> tuple[int, int]:
    return (
        _integer(anchor.get("offset_ms"), f"{path}.offset_ms"),
        _integer(anchor.get("event_index"), f"{path}.event_index"),
    )


def _at_or_before(
    anchor: Mapping[str, Any],
    cutoff: tuple[int, int],
    path: str,
) -> bool:
    return _anchor_key(anchor, path) <= cutoff


def _field(value: Any, mask: str, provenance: Mapping[str, Any]) -> dict[str, Any]:
    if mask not in MASK_VALUES:
        raise L2TemporalJoinError(f"unsupported field mask {mask!r}")
    if mask == "MISSING" and value is not None:
        raise L2TemporalJoinError("MISSING field must have value=None")
    if mask != "MISSING" and value is None:
        raise L2TemporalJoinError("non-MISSING field must have a value")
    return {
        "value": deepcopy(value),
        "mask": mask,
        "provenance": deepcopy(dict(provenance)),
    }


def _missing(note: str) -> dict[str, Any]:
    return _field(None, "MISSING", {"kind": "MISSING", "note": note})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.casefold() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise L2TemporalJoinError(f"cannot read JSON document {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise L2TemporalJoinError(f"JSON document {path} must contain an object")
    return value


def _iter_jsonl_gzip(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise L2TemporalJoinError(
                        f"invalid JSON at {path}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise L2TemporalJoinError(
                        f"decision row at {path}:{line_number} must be an object"
                    )
                yield value
    except OSError as exc:
        raise L2TemporalJoinError(f"cannot stream decision partition {path}: {exc}") from exc


def _feature_index(report: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    if report.get("kind") != "chronicle_encounter_reconstruction_v1":
        raise L2TemporalJoinError(
            "feature report kind must be chronicle_encounter_reconstruction_v1"
        )
    encounters = report.get("encounters")
    if not isinstance(encounters, list):
        raise L2TemporalJoinError("feature report encounters must be a list")
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, raw in enumerate(encounters):
        encounter = _mapping(raw, f"encounters[{index}]")
        instance = str(encounter.get("instance") or "").strip()
        encounter_id = str(encounter.get("encounter") or "").strip()
        if not instance or not encounter_id:
            raise L2TemporalJoinError(
                f"encounters[{index}] lacks instance or encounter"
            )
        key = (instance, encounter_id)
        if key in result:
            raise L2TemporalJoinError(f"duplicate feature encounter key {key}")
        result[key] = encounter
    return result


def _decision_identity(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[int, int]]:
    identity = _mapping(record.get("identity"), "identity")
    source = _mapping(record.get("source"), "source")
    start_anchor = _mapping(source.get("start_anchor"), "source.start_anchor")
    action = _mapping(record.get("action"), "action")
    instance = str(identity.get("source_instance_ref") or "").strip()
    encounter = str(identity.get("encounter_id") or "").strip()
    decision_id = str(action.get("decision_id") or "").strip()
    if not instance or not encounter or not decision_id:
        raise L2TemporalJoinError(
            "decision row requires source_instance_ref, encounter_id, and decision_id"
        )
    cutoff = _anchor_key(start_anchor, "source.start_anchor")
    return (
        {
            "decision_id": decision_id,
            "source_instance_ref": instance,
            "encounter_id": encounter,
            "player_guid": identity.get("player_guid"),
            "action_spell_id": action.get("spell_id"),
            "action_spell_name": action.get("spell_name"),
            "start_anchor": deepcopy(dict(start_anchor)),
        },
        cutoff,
    )


def _sorted_waves(encounter: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_waves = encounter.get("waves")
    if not isinstance(raw_waves, list):
        raise L2TemporalJoinError("feature encounter waves must be a list")
    waves = [_mapping(value, f"waves[{index}]") for index, value in enumerate(raw_waves)]
    return sorted(
        waves,
        key=lambda wave: _anchor_key(
            _mapping(wave.get("first_anchor"), "wave.first_anchor"),
            "wave.first_anchor",
        ),
    )


def _current_wave(
    encounter: Mapping[str, Any], cutoff: tuple[int, int]
) -> tuple[list[Mapping[str, Any]], int | None]:
    waves = _sorted_waves(encounter)
    current_index: int | None = None
    for index, wave in enumerate(waves):
        anchor = _mapping(wave.get("first_anchor"), f"waves[{index}].first_anchor")
        if _at_or_before(anchor, cutoff, f"waves[{index}].first_anchor"):
            current_index = index
        else:
            break
    return waves, current_index


def _target_prefix(
    wave: Mapping[str, Any], cutoff: tuple[int, int]
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    targets = wave.get("targets")
    if not isinstance(targets, list):
        raise L2TemporalJoinError("wave.targets must be a list")
    seen: list[Mapping[str, Any]] = []
    alive: list[Mapping[str, Any]] = []
    observed_dead: list[Mapping[str, Any]] = []
    for index, raw_target in enumerate(targets):
        target = _mapping(raw_target, f"wave.targets[{index}]")
        interval = _mapping(
            target.get("activity_interval"),
            f"wave.targets[{index}].activity_interval",
        )
        first_anchor = _mapping(
            interval.get("first_anchor"),
            f"wave.targets[{index}].activity_interval.first_anchor",
        )
        if not _at_or_before(
            first_anchor,
            cutoff,
            f"wave.targets[{index}].activity_interval.first_anchor",
        ):
            continue
        seen.append(target)
        kill_proxy = target.get("kill_budget_proxy")
        death_anchor: Mapping[str, Any] | None = None
        if isinstance(kill_proxy, Mapping) and isinstance(
            kill_proxy.get("death_anchor"), Mapping
        ):
            death_anchor = _mapping(
                kill_proxy.get("death_anchor"),
                f"wave.targets[{index}].kill_budget_proxy.death_anchor",
            )
        if death_anchor is not None and _at_or_before(
            death_anchor,
            cutoff,
            f"wave.targets[{index}].kill_budget_proxy.death_anchor",
        ):
            observed_dead.append(target)
        else:
            alive.append(target)
    return seen, alive, observed_dead


def _target_guid(target: Mapping[str, Any], path: str) -> str:
    guid = str(target.get("target_guid") or "").strip()
    if not guid:
        raise L2TemporalJoinError(f"{path}.target_guid is required")
    return guid


def _latest_anchor(
    anchors: Sequence[Mapping[str, Any]], path: str
) -> dict[str, Any] | None:
    if not anchors:
        return None
    return deepcopy(
        dict(max(anchors, key=lambda value: _anchor_key(value, path)))
    )


def _components(nodes: set[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        if left in nodes and right in nodes:
            adjacency[left].add(right)
            adjacency[right].add(left)
    result: list[list[str]] = []
    unseen = set(adjacency)
    while unseen:
        root = min(unseen)
        stack = [root]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current] - component)
        unseen -= component
        if len(component) >= 2:
            result.append(sorted(component))
    return sorted(result)


def _pile_field(
    wave: Mapping[str, Any],
    alive_guids: set[str],
    cutoff: tuple[int, int],
    feature_name: str,
) -> dict[str, Any]:
    groups = wave.get("same_batch_damage_groups")
    if not isinstance(groups, list):
        raise L2TemporalJoinError("wave.same_batch_damage_groups must be a list")
    edges: set[tuple[str, str]] = set()
    evidence_count: Counter[tuple[str, str]] = Counter()
    evidence_anchors: list[Mapping[str, Any]] = []
    decision_offset_ms = cutoff[0]
    for index, raw_group in enumerate(groups):
        group = _mapping(raw_group, f"same_batch_damage_groups[{index}]")
        if group.get("relation") != "MELEE_REACH_COHIT":
            continue
        group_offset = _integer(
            group.get("offset_ms"),
            f"same_batch_damage_groups[{index}].offset_ms",
        )
        # A group is assembled from several rows at the same offset, while the
        # compact report retains only its first anchor.  Requiring a strictly
        # earlier offset prevents later rows at the decision millisecond from
        # leaking into the state.
        if group_offset >= decision_offset_ms:
            continue
        raw_guids = group.get("target_guids")
        if not isinstance(raw_guids, list):
            raise L2TemporalJoinError(
                f"same_batch_damage_groups[{index}].target_guids must be a list"
            )
        guids = sorted({str(value) for value in raw_guids if str(value)} & alive_guids)
        if len(guids) < 2:
            continue
        anchor = _mapping(
            group.get("first_anchor"),
            f"same_batch_damage_groups[{index}].first_anchor",
        )
        if not _at_or_before(
            anchor,
            cutoff,
            f"same_batch_damage_groups[{index}].first_anchor",
        ):
            continue
        evidence_anchors.append(anchor)
        for left_index, left in enumerate(guids):
            for right in guids[left_index + 1 :]:
                edge = (left, right)
                edges.add(edge)
                evidence_count[edge] += 1
    components = _components(alive_guids, edges)
    if not components:
        return _missing(
            "no strictly prior positive melee-reach co-hit evidence among currently alive proxies; absence does not prove separation"
        )
    return _field(
        {
            "components": components,
            "pair_evidence_counts": [
                {"target_guids": list(edge), "count": evidence_count[edge]}
                for edge in sorted(evidence_count)
            ],
            "semantics": "positive stacked-membership evidence only; not a complete pile partition",
        },
        "RECONSTRUCTED",
        {
            "kind": "RECONSTRUCTED",
            "feature_report": feature_name,
            "latest_evidence_anchor": _latest_anchor(
                evidence_anchors, "pile.evidence_anchor"
            ),
            "evidence_offset_rule": "strictly_before_decision_offset",
            "note": "connected components from prior MELEE_REACH_COHIT groups; no coordinate or separation claim",
        },
    )


def _history_field(
    record: Mapping[str, Any],
    output_name: str,
    *,
    history_limit: int,
) -> dict[str, Any]:
    source_name = SOURCE_HISTORY_FIELDS[output_name]
    values = _mapping(record.get("state_before"), "state_before")
    masks = _mapping(record.get("state_mask"), "state_mask")
    provenance = _mapping(record.get("state_provenance"), "state_provenance")
    source_provenance = _mapping(
        provenance.get(source_name), f"state_provenance.{source_name}"
    )
    source_mask = masks.get(source_name)
    source_value = values.get(source_name)
    source_status = source_provenance.get("kind")
    if source_mask is not True or source_status == "MISSING" or source_value is None:
        return _missing(f"decision source field {source_name} is MISSING")
    if source_status not in ("OBSERVED", "RECONSTRUCTED"):
        return _missing(
            f"decision source field {source_name} has unsupported status {source_status!r}"
        )
    compact = deepcopy(source_value)
    if output_name == "recent_action_history":
        if not isinstance(compact, list):
            raise L2TemporalJoinError(f"state_before.{source_name} must be a list")
        compact = compact[-history_limit:]
    copied_provenance = deepcopy(dict(source_provenance))
    copied_provenance["source_state_field"] = source_name
    if output_name == "recent_action_history":
        copied_provenance["retained_tail_count"] = history_limit
    return _field(compact, str(source_status), copied_provenance)


def _structural_fields(
    encounter: Mapping[str, Any] | None,
    cutoff: tuple[int, int],
    *,
    feature_name: str,
) -> dict[str, dict[str, Any]]:
    if encounter is None:
        note = "no feature encounter matched instance+encounter"
        return {name: _missing(note) for name in STRUCTURAL_FIELDS}
    waves, current_index = _current_wave(encounter, cutoff)
    if current_index is None:
        note = "no wave start anchor exists at or before the decision START"
        return {name: _missing(note) for name in STRUCTURAL_FIELDS}

    wave = waves[current_index]
    first_anchor = _mapping(wave.get("first_anchor"), "wave.first_anchor")
    wave_id = str(wave.get("wave_id") or "").strip()
    if not wave_id:
        raise L2TemporalJoinError("wave.wave_id is required")
    start_offset_ms = _integer(wave.get("start_offset_ms"), "wave.start_offset_ms")
    elapsed_ms = cutoff[0] - start_offset_ms
    if elapsed_ms < 0:
        raise L2TemporalJoinError("selected wave begins after decision cutoff")

    fields: dict[str, dict[str, Any]] = {
        "wave": _field(
            {
                "wave_id": wave_id,
                "ordinal": wave.get("ordinal"),
                "start_offset_ms": start_offset_ms,
                "prefix_state": "OPEN_OR_LULL",
            },
            "RECONSTRUCTED",
            {
                "kind": "RECONSTRUCTED",
                "feature_report": feature_name,
                "wave_first_anchor": deepcopy(dict(first_anchor)),
                "note": "latest wave whose start anchor is at or before decision START; final end is not consulted",
            },
        ),
        "wave_elapsed_ms": _field(
            elapsed_ms,
            "RECONSTRUCTED",
            {
                "kind": "RECONSTRUCTED",
                "feature_report": feature_name,
                "wave_first_anchor": deepcopy(dict(first_anchor)),
                "cutoff_offset_ms": cutoff[0],
                "note": "decision offset minus observed wave-prefix start offset",
            },
        ),
        "current_wave_final_duration_ms": _missing(
            "final current-wave duration is future information at decision time"
        ),
    }
    if current_index > 0:
        previous = waves[current_index - 1]
        previous_last_anchor = _mapping(
            previous.get("last_anchor"), "previous_wave.last_anchor"
        )
        duration_ms = _integer(
            previous.get("duration_ms"), "previous_wave.duration_ms"
        )
        fields["previous_wave_duration_ms"] = _field(
            duration_ms,
            "RECONSTRUCTED",
            {
                "kind": "RECONSTRUCTED",
                "feature_report": feature_name,
                "previous_wave_id": previous.get("wave_id"),
                "previous_wave_last_anchor": deepcopy(dict(previous_last_anchor)),
                "closure_witness_anchor": deepcopy(dict(first_anchor)),
                "note": "available only after the next wave start proves the prior combat-gap boundary",
            },
        )
    else:
        fields["previous_wave_duration_ms"] = _missing(
            "no earlier wave has been closed by a later wave start"
        )

    seen, alive, observed_dead = _target_prefix(wave, cutoff)
    seen_guids = sorted(
        _target_guid(target, "seen_target") for target in seen
    )
    alive_guids = sorted(
        _target_guid(target, "alive_target") for target in alive
    )
    seen_anchors = [
        _mapping(
            _mapping(target.get("activity_interval"), "target.activity_interval").get(
                "first_anchor"
            ),
            "target.activity_interval.first_anchor",
        )
        for target in seen
    ]
    fields["target_count"] = _field(
        len(seen_guids),
        "RECONSTRUCTED",
        {
            "kind": "RECONSTRUCTED",
            "feature_report": feature_name,
            "wave_id": wave_id,
            "latest_target_first_anchor": _latest_anchor(
                seen_anchors, "target.first_anchor"
            ),
            "note": "count of feature-eligible hostile GUIDs first observed by this START; a lower-coverage proxy, not final wave target_count",
        },
    )
    death_anchors: list[Mapping[str, Any]] = []
    for target in observed_dead:
        proxy = _mapping(target.get("kill_budget_proxy"), "target.kill_budget_proxy")
        death_anchors.append(
            _mapping(proxy.get("death_anchor"), "target.kill_budget_proxy.death_anchor")
        )
    alive_latest = _latest_anchor(
        [*seen_anchors, *death_anchors], "alive_proxy.anchor"
    )
    fields["alive_target_proxy"] = _field(
        {
            "count": len(alive_guids),
            "target_guids": alive_guids,
            "observed_dead_count": len(observed_dead),
            "semantics": "first-observed targets minus DEAD anchors observed by START",
        },
        "RECONSTRUCTED",
        {
            "kind": "RECONSTRUCTED",
            "feature_report": feature_name,
            "wave_id": wave_id,
            "latest_evidence_anchor": alive_latest,
            "note": "proxy can overcount deaths lacking DEAD and undercount targets not yet observed; it is not HP",
        },
    )
    fields["pile_cohit_components"] = _pile_field(
        wave,
        set(alive_guids),
        cutoff,
        feature_name,
    )
    return fields


def join_decision_record(
    record: Mapping[str, Any],
    feature_index: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    feature_name: str,
    history_limit: int = 8,
) -> dict[str, Any]:
    """Join one decision with prefix-only Chronicle encounter context."""

    if history_limit <= 0:
        raise ValueError("history_limit must be positive")
    validate_state_evidence_cutoff(record)
    decision, cutoff = _decision_identity(record)
    key = (decision["source_instance_ref"], decision["encounter_id"])
    encounter = feature_index.get(key)
    fields = _structural_fields(encounter, cutoff, feature_name=feature_name)
    for output_name in HISTORY_FIELDS:
        fields[output_name] = _history_field(
            record,
            output_name,
            history_limit=history_limit,
        )
    for name in PROTECTED_MISSING_FIELDS:
        fields[name] = _missing(
            "L2 Chronicle join does not reconstruct absolute rage, queue intent, hidden timers, or HP"
        )
    row = {
        "schema_version": 1,
        "schema": ROW_SCHEMA,
        "decision": decision,
        "join": {
            "key": {
                "instance": key[0],
                "encounter": key[1],
                "offset_ms": cutoff[0],
                "event_index": cutoff[1],
            },
            "status": "MATCHED" if encounter is not None else "MISSING_ENCOUNTER",
            "feature_report": feature_name,
            "future_leakage_rule": "all evidence anchors at or before START; same-offset co-hit groups excluded",
        },
        "fields": {name: fields[name] for name in ALL_FIELDS},
    }
    validate_joined_row(row)
    return row


def _validate_nested_event_indices(
    value: Any,
    *,
    cutoff_event_index: int,
    path: str,
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(key, str) and (
                key == "event_index" or key.endswith("_event_index")
            ):
                if child is None:
                    continue
                event_index = _integer(child, child_path)
                if event_index > cutoff_event_index:
                    raise L2TemporalJoinError(
                        f"future evidence at {child_path}: {event_index} > {cutoff_event_index}"
                    )
            else:
                _validate_nested_event_indices(
                    child,
                    cutoff_event_index=cutoff_event_index,
                    path=child_path,
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_nested_event_indices(
                child,
                cutoff_event_index=cutoff_event_index,
                path=f"{path}[{index}]",
            )


def validate_joined_row(row: Mapping[str, Any]) -> None:
    if row.get("schema") != ROW_SCHEMA or row.get("schema_version") != 1:
        raise L2TemporalJoinError(f"joined row must use {ROW_SCHEMA}")
    decision = _mapping(row.get("decision"), "decision")
    start_anchor = _mapping(decision.get("start_anchor"), "decision.start_anchor")
    cutoff_event_index = _integer(
        start_anchor.get("event_index"), "decision.start_anchor.event_index"
    )
    fields = _mapping(row.get("fields"), "fields")
    if tuple(fields) != ALL_FIELDS:
        raise L2TemporalJoinError(
            f"joined fields must exactly match {list(ALL_FIELDS)}"
        )
    for name in ALL_FIELDS:
        field = _mapping(fields[name], f"fields.{name}")
        if set(field) != {"value", "mask", "provenance"}:
            raise L2TemporalJoinError(
                f"fields.{name} must contain value/mask/provenance"
            )
        mask = field.get("mask")
        if mask not in MASK_VALUES:
            raise L2TemporalJoinError(f"fields.{name}.mask is invalid")
        if mask == "MISSING" and field.get("value") is not None:
            raise L2TemporalJoinError(f"MISSING fields.{name} must be null")
        if mask != "MISSING" and field.get("value") is None:
            raise L2TemporalJoinError(f"non-MISSING fields.{name} must have a value")
        provenance = _mapping(field.get("provenance"), f"fields.{name}.provenance")
        if provenance.get("kind") != mask:
            raise L2TemporalJoinError(
                f"fields.{name}.provenance.kind must equal its mask"
            )
    _validate_nested_event_indices(
        fields,
        cutoff_event_index=cutoff_event_index,
        path="fields",
    )


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def write_join_partition(
    decision_partition: Path,
    feature_report: Path,
    output_path: Path,
    *,
    max_records: int | None = None,
    history_limit: int = 8,
) -> dict[str, Any]:
    """Stream one decision partition into one compact L2 sidecar."""

    if max_records is not None and max_records <= 0:
        raise ValueError("max_records must be positive")
    feature = _read_json(feature_report)
    index = _feature_index(feature)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mask_counts: dict[str, Counter[str]] = {
        name: Counter() for name in ALL_FIELDS
    }
    join_counts: Counter[str] = Counter()
    row_count = 0
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as raw_handle:
        temporary = Path(raw_handle.name)
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", compresslevel=6, mtime=0) as compressed:
            for record in _iter_jsonl_gzip(decision_partition):
                if max_records is not None and row_count >= max_records:
                    break
                joined = join_decision_record(
                    record,
                    index,
                    feature_name=feature_report.name,
                    history_limit=history_limit,
                )
                encoded = (
                    json.dumps(joined, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                compressed.write(encoded)
                row_count += 1
                join_counts[joined["join"]["status"]] += 1
                for name, field in joined["fields"].items():
                    mask_counts[name][field["mask"]] += 1
    temporary.replace(output_path)
    return {
        "decision_partition": str(decision_partition.resolve()),
        "feature_report": str(feature_report.resolve()),
        "output_partition": str(output_path.resolve()),
        "record_count": row_count,
        "compressed_size_bytes": output_path.stat().st_size,
        "join_status_counts": dict(sorted(join_counts.items())),
        "field_mask_counts": {
            name: dict(sorted(mask_counts[name].items())) for name in ALL_FIELDS
        },
    }


def _feature_reports_by_source(
    manifest_path: Path, manifest: Mapping[str, Any]
) -> dict[str, Path]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise L2TemporalJoinError("feature manifest entries must be a list")
    result: dict[str, Path] = {}
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, f"feature_manifest.entries[{index}]")
        if entry.get("processing_status") != "COMPLETED":
            continue
        source = _mapping(entry.get("source"), f"feature_manifest.entries[{index}].source")
        source_name = str(source.get("name") or "").strip()
        outputs = _mapping(
            entry.get("outputs"), f"feature_manifest.entries[{index}].outputs"
        )
        feature = _mapping(
            outputs.get("feature_report"),
            f"feature_manifest.entries[{index}].outputs.feature_report",
        )
        relative = str(feature.get("path") or "").strip()
        if not source_name or not relative:
            raise L2TemporalJoinError(
                f"completed feature entry {index} lacks source name or feature path"
            )
        if source_name in result:
            raise L2TemporalJoinError(f"duplicate completed feature source {source_name}")
        result[source_name] = (manifest_path.parent / relative).resolve()
    return result


def build_l2_temporal_join(
    decision_manifest_path: Path = DEFAULT_DECISION_MANIFEST,
    feature_manifest_path: Path = DEFAULT_FEATURE_MANIFEST,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    max_partitions: int | None = None,
    max_records_per_partition: int | None = None,
    history_limit: int = 8,
) -> dict[str, Any]:
    """Build compact sidecars from existing decision and feature artifacts."""

    if max_partitions is not None and max_partitions <= 0:
        raise ValueError("max_partitions must be positive")
    decision_manifest = _read_json(decision_manifest_path)
    feature_manifest = _read_json(feature_manifest_path)
    if decision_manifest.get("schema") != "chronicle_fury_decision_dataset/v1":
        raise L2TemporalJoinError(
            "decision manifest schema must be chronicle_fury_decision_dataset/v1"
        )
    feature_by_source = _feature_reports_by_source(
        feature_manifest_path, feature_manifest
    )
    raw_partitions = decision_manifest.get("partitions")
    if not isinstance(raw_partitions, list):
        raise L2TemporalJoinError("decision manifest partitions must be a list")
    selected = raw_partitions[:max_partitions] if max_partitions else raw_partitions
    partition_summaries: list[dict[str, Any]] = []
    for index, raw_partition in enumerate(selected):
        partition = _mapping(raw_partition, f"decision_manifest.partitions[{index}]")
        normalized_path = Path(str(partition.get("normalized_file") or ""))
        decision_path = Path(str(partition.get("partition") or ""))
        if not normalized_path.name or not decision_path.name:
            raise L2TemporalJoinError(
                f"decision partition {index} lacks normalized_file or partition"
            )
        feature_path = feature_by_source.get(normalized_path.name)
        if feature_path is None:
            raise L2TemporalJoinError(
                f"no completed feature report for {normalized_path.name}"
            )
        output_name = decision_path.name.removesuffix(".jsonl.gz") + ".l2.jsonl.gz"
        partition_summaries.append(
            write_join_partition(
                decision_path,
                feature_path,
                output_dir / output_name,
                max_records=max_records_per_partition,
                history_limit=history_limit,
            )
        )

    aggregate_join_counts: Counter[str] = Counter()
    aggregate_masks: dict[str, Counter[str]] = {
        name: Counter() for name in ALL_FIELDS
    }
    for summary in partition_summaries:
        aggregate_join_counts.update(summary["join_status_counts"])
        for name, counts in summary["field_mask_counts"].items():
            aggregate_masks[name].update(counts)
    result = {
        "schema_version": 1,
        "kind": MANIFEST_KIND,
        "generated_at": _utc_now(),
        "inputs": {
            "decision_manifest": str(decision_manifest_path.resolve()),
            "feature_manifest": str(feature_manifest_path.resolve()),
            "normalized_or_raw_rows_read": 0,
        },
        "contract": {
            "join_key": ["instance", "encounter", "decision START offset/event_index"],
            "row_schema": ROW_SCHEMA,
            "output_role": "compact L2 sidecar; original decision rows are not copied",
            "mask_values": list(MASK_VALUES),
            "future_leakage_controls": [
                "feature anchors must be at or before decision START",
                "same-offset co-hit groups are excluded because the report lacks their final row anchor",
                "final current-wave target_count, duration, kill budgets, and final target groups are not copied",
                "prior wave duration appears only after the next wave start closes the boundary",
            ],
            "protected_missing_fields": list(PROTECTED_MISSING_FIELDS),
        },
        "selection": {
            "max_partitions": max_partitions,
            "max_records_per_partition": max_records_per_partition,
            "history_limit": history_limit,
        },
        "summary": {
            "partition_count": len(partition_summaries),
            "record_count": sum(value["record_count"] for value in partition_summaries),
            "compressed_size_bytes": sum(
                value["compressed_size_bytes"] for value in partition_summaries
            ),
            "join_status_counts": dict(sorted(aggregate_join_counts.items())),
            "field_mask_counts": {
                name: dict(sorted(aggregate_masks[name].items())) for name in ALL_FIELDS
            },
        },
        "partitions": partition_summaries,
        "limitations": [
            "target_count is an observed-so-far hostile GUID proxy, not exact simultaneous target count",
            "alive_target_proxy subtracts only observed DEAD anchors and is neither HP nor a complete life-state oracle",
            "pile_cohit_components contains positive prior melee-reach co-hit evidence only; missing does not mean separated",
            "aura ledgers retain incomplete initial state and durations from the decision dataset",
            "action history contains prior uniquely linked server actions, not client keypress or queue intent",
        ],
    }
    _write_json_atomic(output_dir / "manifest.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-manifest", type=Path, default=DEFAULT_DECISION_MANIFEST)
    parser.add_argument("--feature-manifest", type=Path, default=DEFAULT_FEATURE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-partitions", type=int)
    parser.add_argument("--max-records-per-partition", type=int)
    parser.add_argument("--history-limit", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_l2_temporal_join(
        args.decision_manifest,
        args.feature_manifest,
        args.output_dir,
        max_partitions=args.max_partitions,
        max_records_per_partition=args.max_records_per_partition,
        history_limit=args.history_limit,
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
