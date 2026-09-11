"""Versioned encounter reconstruction for verified Chronicle External API data.

This consumer is intentionally separate from the manual-CSV V1 pipeline.  Its
only production entrypoint accepts a verified external reconstruction admission
manifest, streams the admitted partition through the pinned CLASS resolver, and
publishes deterministic per-encounter artifacts before committing a manifest.

The reconstruction is evidence bounded:

* EventMeta order is rechecked and encounters are processed one at a time.
* Only official CLASS events define temporal entity lanes.
* Owner/controller attribution requires an official GUID and an exact metadata
  resolver match; controller takes priority over permanent owner.
* DMG is the sole source of damage amounts.  DEAD supplies a death anchor only.
* Hostile objects, hostile players, and unknown entities remain explicit
  non-voting lanes instead of being discarded or guessed into creatures.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from .chronicle_external_event_normalizer_v1 import STREAM_ORDER
from .chronicle_external_reconstruction_admission_v1 import (
    IMPLEMENTATION_REVISION as ADMISSION_IMPLEMENTATION_REVISION,
    SCHEMA as ADMISSION_SCHEMA,
    SOURCE_EVIDENCE_KIND,
    STATUS as ADMISSION_STATUS,
    canonical_guid,
    canonicalize_optional_0x_owner,
    load_admission_manifest,
)
from .chronicle_unit_classification_semantics_v1 import (
    IMPLEMENTATION_REVISION as CLASSIFICATION_IMPLEMENTATION_REVISION,
    OFFICIAL_COMMIT,
    OFFICIAL_EVIDENCE_SHA256,
    ROW_FIELD as CLASSIFICATION_ROW_FIELD,
    SCHEMA as CLASSIFICATION_SCHEMA,
    iter_resolved_reconstruction_rows,
    official_evidence_contract,
)


SCHEMA = "chronicle_external_encounter_reconstruction/v2"
ARTIFACT_SCHEMA = "chronicle_external_encounter_reconstruction_encounter/v2"
KIND = "chronicle_external_encounter_reconstruction_manifest"
ARTIFACT_KIND = "chronicle_external_encounter_reconstruction_encounter"
STATUS = "RECONSTRUCTED_EXTERNAL_V2_NOT_CAPSULE_NOT_COMPARISON"
IMPLEMENTATION_REVISION = "v2.0_official_class_temporal_damage_only_manifest_last"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "chronicle_external_encounter_reconstruction"
    / "v2"
)
DEFAULT_COMBAT_GAP_MS = 8_000

SUPPORTED_EVENT_TYPES = frozenset(
    {"CLASS", "DMG", "HEAL", "DEAD", "START", "GO", "FAIL"}
)
BOUNDARY_EVENT_TYPES = frozenset({"DMG", "DEAD", "START", "GO", "FAIL"})

LANE_FRIENDLY_PLAYER = "FRIENDLY_PLAYER"
LANE_FRIENDLY_NONPLAYER = "FRIENDLY_NONPLAYER"
LANE_HOSTILE_CREATURE = "HOSTILE_CREATURE"
LANE_HOSTILE_OBJECT = "HOSTILE_OBJECT_NONVOTING"
LANE_HOSTILE_PLAYER = "HOSTILE_PLAYER_NONVOTING"
LANE_HOSTILE_OTHER = "HOSTILE_OTHER_NONVOTING"
LANE_NEUTRAL = "NEUTRAL_NONVOTING"
LANE_UNKNOWN = "UNKNOWN_NONVOTING"

TARGET_LANES = frozenset(
    {
        LANE_HOSTILE_CREATURE,
        LANE_HOSTILE_OBJECT,
        LANE_HOSTILE_PLAYER,
        LANE_HOSTILE_OTHER,
        LANE_NEUTRAL,
        LANE_UNKNOWN,
    }
)
NONVOTING_TARGET_LANES = TARGET_LANES - {LANE_HOSTILE_CREATURE}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ChronicleExternalReconstructionV2Error(RuntimeError):
    """The admitted stream or a published V2 artifact violates its contract."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ChronicleExternalReconstructionV2Error(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ChronicleExternalReconstructionV2Error(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalReconstructionV2Error(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalReconstructionV2Error(f"{label} must be an array")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalReconstructionV2Error(f"{label} must be an integer")
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    result = _integer(value, label=label)
    if result < 0:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} must be nonnegative"
        )
    return result


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} must be nonempty text"
        )
    return value


def _optional_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label=label)


def _safe_component(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if _SAFE_COMPONENT_RE.fullmatch(rendered) is None:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} is unsafe for an artifact filename"
        )
    return rendered


def _content_addressed(value: Mapping[str, Any]) -> dict[str, Any]:
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    digest = _sha256_bytes(_canonical_bytes(core))
    return {
        **core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address",
            "sha256": digest,
        },
    }


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = _mapping(value.get("content_address"), label=f"{label}.content_address")
    declared = _text(address.get("sha256"), label=f"{label}.content_address.sha256")
    if _SHA256_RE.fullmatch(declared) is None:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} content address is not SHA-256"
        )
    if address.get("algorithm") != "sha256":
        raise ChronicleExternalReconstructionV2Error(
            f"{label} content-address algorithm is unsupported"
        )
    core = {key: value for key, value in value.items() if key != "content_address"}
    actual = _sha256_bytes(_canonical_bytes(core))
    if actual != declared:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} content-address mismatch"
        )
    return declared


def _data_root_for(path: Path) -> Path:
    for candidate in (path.parent, *path.parents):
        if candidate.name.casefold() == "offline_data":
            return candidate.resolve()
    raise ChronicleExternalReconstructionV2Error(
        "admission manifest must be stored beneath offline_data"
    )


def _relative_to(path: Path, root: Path, *, label: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} must stay beneath {root}"
        ) from error


def _optional_relation_guid(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    try:
        suffix = canonicalize_optional_0x_owner(value)
    except Exception as error:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} is not an official full hexadecimal GUID"
        ) from error
    if suffix is None:
        return None
    try:
        return canonical_guid("0x" + suffix, label=label)
    except Exception as error:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} is not an official full hexadecimal GUID"
        ) from error


def _guid(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    try:
        return canonical_guid(value, label=label)
    except Exception as error:
        raise ChronicleExternalReconstructionV2Error(
            f"{label} is not a hexadecimal GUID"
        ) from error


def _anchor(row: Mapping[str, Any]) -> dict[str, Any]:
    official = _mapping(row.get("official"), label="row.official")
    provenance = _mapping(row.get("provenance"), label="row.provenance")
    message_sha256 = _text(
        official.get("message_sha256"), label="row.official.message_sha256"
    )
    if _SHA256_RE.fullmatch(message_sha256) is None:
        raise ChronicleExternalReconstructionV2Error(
            "official message SHA is not SHA-256"
        )
    return {
        "encounter_id": row.get("encounter"),
        "event_index": _integer(row.get("event_index"), label="row.event_index"),
        "offset_ms": _nonnegative_integer(row.get("offset_ms"), label="row.offset_ms"),
        "timestamp_ms": _nonnegative_integer(
            row.get("timestamp_ms"), label="row.timestamp_ms"
        ),
        "event_type": row.get("type"),
        "stream_type": provenance.get("stream_type"),
        "frame_index": _nonnegative_integer(
            provenance.get("frame_index"), label="row.provenance.frame_index"
        ),
        "frame_message_index": _nonnegative_integer(
            provenance.get("frame_message_index"),
            label="row.provenance.frame_message_index",
        ),
        "derived_jsonl_line": _nonnegative_integer(
            provenance.get("csv_line"), label="row.provenance.csv_line"
        ),
        "source_guid": row.get("source_guid"),
        "target_guid": row.get("target_guid"),
        "official_message_sha256": message_sha256,
    }


def _classification_lane(resolution: Mapping[str, Any]) -> str:
    unit = _mapping(resolution.get("unit_type"), label="resolution.unit_type")
    affiliation = _mapping(
        resolution.get("affiliation"), label="resolution.affiliation"
    )
    if resolution.get("status") != "OFFICIAL_ENUM_PAIR":
        return LANE_UNKNOWN
    unit_label = unit.get("label")
    affiliation_label = affiliation.get("label")
    if affiliation_label == "FRIENDLY" and unit_label == "PLAYER":
        return LANE_FRIENDLY_PLAYER
    if affiliation_label == "FRIENDLY":
        return LANE_FRIENDLY_NONPLAYER
    if affiliation_label == "HOSTILE" and unit_label == "CREATURE":
        return LANE_HOSTILE_CREATURE
    if affiliation_label == "HOSTILE" and unit_label == "OBJECT":
        return LANE_HOSTILE_OBJECT
    if affiliation_label == "HOSTILE" and unit_label == "PLAYER":
        return LANE_HOSTILE_PLAYER
    if affiliation_label == "HOSTILE":
        return LANE_HOSTILE_OTHER
    if affiliation_label == "NEUTRAL":
        return LANE_NEUTRAL
    return LANE_UNKNOWN


def _compact_player(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(record.get("metadata"), label="metadata player metadata")
    return {
        "guid": record.get("guid"),
        "name": metadata.get("name"),
        "class": metadata.get("class"),
        "race": metadata.get("race"),
        "level": metadata.get("level"),
        "evidence": "EXACT_METADATA_PLAYER_GUID_MATCH",
    }


@dataclass(frozen=True)
class SourceContext:
    admission_manifest_path: Path
    data_root: Path
    instance_id: str
    instance: Mapping[str, Any]
    player_resolver: Mapping[str, Mapping[str, Any]]
    source_binding: Mapping[str, Any]
    instance_provenance: Mapping[str, Any]
    artifact_provenance: Mapping[str, Any]


@dataclass(frozen=True)
class EntityState:
    guid: str
    lane: str
    unit_type_numeric: int
    unit_type_label: str | None
    affiliation_numeric: int
    affiliation_label: str | None
    resolution_status: str
    owner_guid: str | None
    owner_player: Mapping[str, Any] | None
    controller_guid: str | None
    controller_player: Mapping[str, Any] | None
    spell_id: int
    anchor: Mapping[str, Any]

    def compact(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "lane": self.lane,
            "unit_type": {
                "numeric": self.unit_type_numeric,
                "label": self.unit_type_label,
            },
            "affiliation": {
                "numeric": self.affiliation_numeric,
                "label": self.affiliation_label,
            },
            "resolution_status": self.resolution_status,
            "owner": {
                "official_canonical_guid": self.owner_guid,
                "resolved_player_guid": (
                    self.owner_player.get("guid") if self.owner_player else None
                ),
                "status": (
                    "EXACT_METADATA_PLAYER_GUID_MATCH"
                    if self.owner_player
                    else "UNRESOLVED_OR_ABSENT"
                ),
            },
            "controller": {
                "official_canonical_guid": self.controller_guid,
                "resolved_player_guid": (
                    self.controller_player.get("guid")
                    if self.controller_player
                    else None
                ),
                "status": (
                    "EXACT_METADATA_PLAYER_GUID_MATCH"
                    if self.controller_player
                    else "UNRESOLVED_OR_ABSENT"
                ),
            },
            "spell_id": self.spell_id,
            "anchor": dict(self.anchor),
        }


def _unknown_state(guid: str) -> EntityState:
    return EntityState(
        guid=guid,
        lane=LANE_UNKNOWN,
        unit_type_numeric=0,
        unit_type_label="UNKNOWN",
        affiliation_numeric=0,
        affiliation_label="UNKNOWN",
        resolution_status="NO_PRIOR_OR_SAME_ORDER_CLASS_UNKNOWN_NONVOTING",
        owner_guid=None,
        owner_player=None,
        controller_guid=None,
        controller_player=None,
        spell_id=0,
        anchor={
            "encounter_id": None,
            "event_index": None,
            "offset_ms": None,
            "timestamp_ms": None,
            "event_type": None,
            "source_guid": None,
            "target_guid": guid,
            "official_message_sha256": None,
        },
    )


def _exact_player(
    resolver: Mapping[str, Mapping[str, Any]], guid: str | None
) -> Mapping[str, Any] | None:
    return resolver.get(guid or "")


def _artifact_provenance_from_full(
    full_instance_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    temporal = _mapping(
        full_instance_provenance.get("temporal_and_guild_provenance"),
        label="temporal_and_guild_provenance",
    )
    warrior_spec = _mapping(
        full_instance_provenance.get("warrior_spec_evidence"),
        label="warrior_spec_evidence",
    )
    return {
        "started_at": temporal.get("started_at"),
        "started_at_source": temporal.get("started_at_source"),
        "guild": temporal.get("guild"),
        "guild_evidence": temporal.get("guild_evidence"),
        "contamination": temporal.get("contamination"),
        "warrior_spec_summary": {
            "observation_count": warrior_spec.get("observation_count"),
            "declared_counts": warrior_spec.get("declared_counts"),
            "recomputed_counts": warrior_spec.get("recomputed_counts"),
            "field_conflict_observation_count": warrior_spec.get(
                "field_conflict_observation_count"
            ),
            "conflicts_preserved_not_resolved": warrior_spec.get(
                "conflicts_preserved_not_resolved"
            ),
        },
        "full_instance_provenance_sha256": _sha256_bytes(
            _canonical_bytes(full_instance_provenance)
        ),
    }


def _state_from_class_row(
    row: Mapping[str, Any], context: SourceContext
) -> EntityState:
    semantics = _mapping(
        row.get(CLASSIFICATION_ROW_FIELD), label=CLASSIFICATION_ROW_FIELD
    )
    resolution = _mapping(
        semantics.get("resolution"), label="classification.resolution"
    )
    unit = _mapping(resolution.get("unit_type"), label="classification.unit_type")
    affiliation = _mapping(
        resolution.get("affiliation"), label="classification.affiliation"
    )
    wire = _mapping(semantics.get("wire_values"), label="classification.wire_values")
    guid = _guid(wire.get("target"), label="UnitClassification.target")
    if guid is None:
        raise ChronicleExternalReconstructionV2Error(
            "UnitClassification.target must not be absent"
        )
    owner_guid = _optional_relation_guid(
        wire.get("owner"), label="UnitClassification.owner"
    )
    controller_guid = _optional_relation_guid(
        wire.get("controller"), label="UnitClassification.controller"
    )
    spell_id = _integer(wire.get("spell_id"), label="UnitClassification.spellId")
    return EntityState(
        guid=guid,
        lane=_classification_lane(resolution),
        unit_type_numeric=_integer(unit.get("numeric"), label="resolved unit type"),
        unit_type_label=(
            str(unit.get("label")) if unit.get("label") is not None else None
        ),
        affiliation_numeric=_integer(
            affiliation.get("numeric"), label="resolved affiliation"
        ),
        affiliation_label=(
            str(affiliation.get("label"))
            if affiliation.get("label") is not None
            else None
        ),
        resolution_status=str(resolution.get("status")),
        owner_guid=owner_guid,
        owner_player=_exact_player(context.player_resolver, owner_guid),
        controller_guid=controller_guid,
        controller_player=_exact_player(context.player_resolver, controller_guid),
        spell_id=spell_id,
        anchor=_anchor(row),
    )


def _source_actor(
    source_guid: str | None,
    states: Mapping[str, EntityState],
    context: SourceContext,
) -> dict[str, Any]:
    if source_guid is None:
        return {
            "status": "UNRESOLVED_NO_SOURCE_GUID",
            "player": None,
            "source_guid": None,
        }
    current = states.get(source_guid) or _unknown_state(source_guid)
    direct = _exact_player(context.player_resolver, source_guid)
    if direct is not None and current.lane == LANE_FRIENDLY_PLAYER:
        return {
            "status": "EXACT_FRIENDLY_PLAYER_SOURCE",
            "player": _compact_player(direct),
            "source_guid": source_guid,
            "official_relation_path": [source_guid],
        }
    visited = {source_guid}
    relation_path = [source_guid]
    current_guid = source_guid
    for depth in range(1, 6):
        current_state = states.get(current_guid)
        if current_state is None:
            break
        # Chronicle's production resolver gives temporary controller priority
        # over permanent owner.  Missing fields in a CLASS snapshot clear the
        # relation because EntityState is replaced, never merged.
        relation_kind: str | None = None
        relation_guid: str | None = None
        if current_state.controller_guid is not None:
            relation_kind = "CONTROLLER"
            relation_guid = current_state.controller_guid
        elif current_state.owner_guid is not None:
            relation_kind = "OWNER"
            relation_guid = current_state.owner_guid
        if relation_guid is None:
            break
        relation_path.append(relation_guid)
        if relation_guid in visited:
            return {
                "status": "UNRESOLVED_OFFICIAL_RELATION_CYCLE",
                "player": None,
                "source_guid": source_guid,
                "official_relation_path": relation_path,
            }
        visited.add(relation_guid)
        player = _exact_player(context.player_resolver, relation_guid)
        if player is not None:
            return {
                "status": f"EXACT_OFFICIAL_{relation_kind}_PLAYER_DEPTH_{depth}",
                "player": _compact_player(player),
                "source_guid": source_guid,
                "official_relation_path": relation_path,
            }
        current_guid = relation_guid
    return {
        "status": (
            "EXACT_METADATA_PLAYER_BUT_EVENT_AFFILIATION_NOT_FRIENDLY"
            if direct is not None
            else "UNRESOLVED_NO_EXACT_PLAYER_RELATION"
        ),
        "player": None,
        "source_guid": source_guid,
        "official_relation_path": relation_path,
    }


@dataclass
class TargetAccumulator:
    guid: str
    lane_memberships: Counter[str] = field(default_factory=Counter)
    event_type_counts: Counter[str] = field(default_factory=Counter)
    first_anchor: Mapping[str, Any] | None = None
    last_anchor: Mapping[str, Any] | None = None
    first_classification_anchor: Mapping[str, Any] | None = None
    last_classification_anchor: Mapping[str, Any] | None = None
    incoming_damage_amount: int = 0
    incoming_damage_event_count: int = 0
    incoming_damage_through_first_death_amount: int = 0
    incoming_damage_through_first_death_event_count: int = 0
    post_death_incoming_damage_amount: int = 0
    post_death_incoming_damage_event_count: int = 0
    outgoing_damage_amount: int = 0
    outgoing_damage_event_count: int = 0
    healing_received_amount: int = 0
    healing_received_event_count: int = 0
    death_anchor: Mapping[str, Any] | None = None
    death_marker_count: int = 0
    damage_by_player: dict[str, dict[str, Any]] = field(default_factory=dict)
    unattributed_incoming_damage_amount: int = 0
    unattributed_incoming_damage_event_count: int = 0

    def observe_classification(self, state: EntityState) -> None:
        self.lane_memberships[state.lane] += 1
        if self.first_classification_anchor is None:
            self.first_classification_anchor = state.anchor
        self.last_classification_anchor = state.anchor

    def touch(self, row: Mapping[str, Any], state: EntityState) -> None:
        anchor = _anchor(row)
        if self.first_anchor is None:
            self.first_anchor = anchor
        self.last_anchor = anchor
        self.lane_memberships[state.lane] += 1
        self.event_type_counts[str(row.get("type"))] += 1

    def incoming_damage(
        self,
        row: Mapping[str, Any],
        state: EntityState,
        amount: int,
        actor: Mapping[str, Any],
    ) -> None:
        self.touch(row, state)
        self.incoming_damage_amount += amount
        self.incoming_damage_event_count += 1
        if self.death_anchor is None:
            self.incoming_damage_through_first_death_amount += amount
            self.incoming_damage_through_first_death_event_count += 1
        else:
            self.post_death_incoming_damage_amount += amount
            self.post_death_incoming_damage_event_count += 1
        player = actor.get("player")
        if isinstance(player, Mapping) and isinstance(player.get("guid"), str):
            player_guid = str(player["guid"])
            bucket = self.damage_by_player.setdefault(
                player_guid,
                {
                    "player": deepcopy(dict(player)),
                    "damage_amount": 0,
                    "damage_event_count": 0,
                    "attribution_status_counts": Counter(),
                },
            )
            bucket["damage_amount"] += amount
            bucket["damage_event_count"] += 1
            bucket["attribution_status_counts"][str(actor.get("status"))] += 1
        else:
            self.unattributed_incoming_damage_amount += amount
            self.unattributed_incoming_damage_event_count += 1

    def outgoing_damage(
        self, row: Mapping[str, Any], state: EntityState, amount: int
    ) -> None:
        self.touch(row, state)
        self.outgoing_damage_amount += amount
        self.outgoing_damage_event_count += 1

    def received_heal(
        self, row: Mapping[str, Any], state: EntityState, amount: int
    ) -> None:
        self.touch(row, state)
        self.healing_received_amount += amount
        self.healing_received_event_count += 1

    def observe_death(self, row: Mapping[str, Any], state: EntityState) -> None:
        self.touch(row, state)
        self.death_marker_count += 1
        if self.death_anchor is None:
            self.death_anchor = _anchor(row)

    def finish(self) -> dict[str, Any]:
        player_rows = []
        for player_guid in sorted(self.damage_by_player):
            raw = self.damage_by_player[player_guid]
            player_rows.append(
                {
                    "player": raw["player"],
                    "damage_amount": raw["damage_amount"],
                    "damage_event_count": raw["damage_event_count"],
                    "attribution_status_counts": dict(
                        sorted(raw["attribution_status_counts"].items())
                    ),
                }
            )
        memberships = dict(sorted(self.lane_memberships.items()))
        voting = memberships.get(LANE_HOSTILE_CREATURE, 0) > 0
        core = {
            "target_guid": self.guid,
            "lane_membership_observation_counts": memberships,
            "voting_for_simulator_target_model": voting,
            "voting_status": (
                "VOTING_OFFICIAL_HOSTILE_CREATURE"
                if voting
                else "NONVOTING_PRESERVED"
            ),
            "event_type_counts": dict(sorted(self.event_type_counts.items())),
            "first_activity_anchor": (
                dict(self.first_anchor) if self.first_anchor else None
            ),
            "last_activity_anchor": (
                dict(self.last_anchor) if self.last_anchor else None
            ),
            "first_classification_anchor": (
                dict(self.first_classification_anchor)
                if self.first_classification_anchor
                else None
            ),
            "last_classification_anchor": (
                dict(self.last_classification_anchor)
                if self.last_classification_anchor
                else None
            ),
            "damage_received": {
                "amount": self.incoming_damage_amount,
                "event_count": self.incoming_damage_event_count,
                "amount_source": "DMG_ONLY",
                "by_exact_player": player_rows,
                "unattributed_amount": self.unattributed_incoming_damage_amount,
                "unattributed_event_count": (
                    self.unattributed_incoming_damage_event_count
                ),
                "through_first_death": {
                    "amount": self.incoming_damage_through_first_death_amount,
                    "event_count": (
                        self.incoming_damage_through_first_death_event_count
                    ),
                    "interpretation": (
                        "observed DMG sum, not exact maximum or initial health"
                    ),
                },
                "after_first_death": {
                    "amount": self.post_death_incoming_damage_amount,
                    "event_count": self.post_death_incoming_damage_event_count,
                    "excluded_from_kill_budget_proxy": True,
                },
            },
            "damage_done": {
                "amount": self.outgoing_damage_amount,
                "event_count": self.outgoing_damage_event_count,
                "amount_source": "DMG_ONLY",
            },
            "healing_received": {
                "amount": self.healing_received_amount,
                "event_count": self.healing_received_event_count,
                "amount_source": "HEAL_ONLY",
            },
            "death": {
                "observed": self.death_anchor is not None,
                "marker_count": self.death_marker_count,
                "anchor": dict(self.death_anchor) if self.death_anchor else None,
                "damage_amount_added_from_slain": 0,
            },
            "armor": {
                "status": "MISSING_NOT_IN_EXTERNAL_CORE_STREAM_SET",
                "effective_armor": None,
                "aura_transitions": None,
            },
        }
        return _content_addressed(core)


def _observe_target_event(
    accumulator: TargetAccumulator,
    row: Mapping[str, Any],
    state: EntityState,
    *,
    role: str,
    amount: int | None,
    actor: Mapping[str, Any],
) -> None:
    event_type = str(row.get("type"))
    if event_type == "DMG" and amount is not None:
        if role == "target":
            accumulator.incoming_damage(row, state, amount, actor)
        else:
            accumulator.outgoing_damage(row, state, amount)
    elif event_type == "HEAL" and amount is not None and role == "target":
        accumulator.received_heal(row, state, amount)
    elif event_type == "DEAD" and role == "target":
        accumulator.observe_death(row, state)
    else:
        accumulator.touch(row, state)


@dataclass
class WaveAccumulator:
    encounter_id: str
    ordinal: int
    start_offset_ms: int
    first_anchor: Mapping[str, Any]
    last_boundary_offset_ms: int
    last_boundary_anchor: Mapping[str, Any]
    last_context_offset_ms: int
    last_context_anchor: Mapping[str, Any]
    combat_gap_ms: int
    event_type_counts: Counter[str] = field(default_factory=Counter)
    boundary_event_count: int = 0
    temporal_context_event_count: int = 0
    targets: dict[str, TargetAccumulator] = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        encounter_id: str,
        ordinal: int,
        row: Mapping[str, Any],
        *,
        combat_gap_ms: int,
    ) -> "WaveAccumulator":
        anchor = _anchor(row)
        offset = int(anchor["offset_ms"])
        return cls(
            encounter_id=encounter_id,
            ordinal=ordinal,
            start_offset_ms=offset,
            first_anchor=anchor,
            last_boundary_offset_ms=offset,
            last_boundary_anchor=anchor,
            last_context_offset_ms=offset,
            last_context_anchor=anchor,
            combat_gap_ms=combat_gap_ms,
        )

    def observe(
        self,
        row: Mapping[str, Any],
        states: Mapping[str, EntityState],
        context: SourceContext,
        *,
        boundary: bool,
        amount: int | None,
    ) -> None:
        anchor = _anchor(row)
        offset = int(anchor["offset_ms"])
        if offset < self.last_context_offset_ms:
            raise ChronicleExternalReconstructionV2Error(
                "wave context order moved backwards"
            )
        self.last_context_offset_ms = offset
        self.last_context_anchor = anchor
        self.event_type_counts[str(row.get("type"))] += 1
        if boundary:
            self.boundary_event_count += 1
            self.last_boundary_offset_ms = offset
            self.last_boundary_anchor = anchor
        else:
            self.temporal_context_event_count += 1

        source_guid = _guid(row.get("source_guid"), label="row.source_guid")
        target_guid = _guid(row.get("target_guid"), label="row.target_guid")
        source_state = states.get(source_guid or "")
        target_state = states.get(target_guid or "")
        actor = _source_actor(source_guid, states, context)
        for role, guid, state in (
            ("source", source_guid, source_state),
            ("target", target_guid, target_state),
        ):
            if guid is None:
                continue
            current = state or _unknown_state(guid)
            if current.lane not in TARGET_LANES:
                continue
            target = self.targets.setdefault(guid, TargetAccumulator(guid))
            _observe_target_event(
                target,
                row,
                current,
                role=role,
                amount=amount,
                actor=actor,
            )

    def finish(self) -> dict[str, Any]:
        targets = [self.targets[key].finish() for key in sorted(self.targets)]
        membership_sets: dict[str, set[str]] = {}
        voting_targets = 0
        for target in targets:
            if target["voting_for_simulator_target_model"]:
                voting_targets += 1
            for lane in target["lane_membership_observation_counts"]:
                membership_sets.setdefault(lane, set()).add(target["target_guid"])
        core = {
            "wave_id": f"{self.encounter_id}:external-v2-wave:{self.ordinal}",
            "ordinal": self.ordinal,
            "status": STATUS,
            "window": {
                "start_offset_ms": self.start_offset_ms,
                "last_hostile_creature_boundary_offset_ms": (
                    self.last_boundary_offset_ms
                ),
                "last_observed_context_offset_ms": self.last_context_offset_ms,
                "boundary_duration_ms": (
                    self.last_boundary_offset_ms - self.start_offset_ms
                ),
                "context_duration_ms": self.last_context_offset_ms - self.start_offset_ms,
                "first_anchor": dict(self.first_anchor),
                "last_boundary_anchor": dict(self.last_boundary_anchor),
                "last_context_anchor": dict(self.last_context_anchor),
            },
            "boundary_contract": {
                "driver": "core event touching current official HOSTILE_CREATURE state",
                "combat_gap_ms": self.combat_gap_ms,
                "hostile_object_drives_boundary": False,
                "hostile_player_drives_boundary": False,
                "unknown_drives_boundary": False,
            },
            "event_counts": dict(sorted(self.event_type_counts.items())),
            "boundary_event_count": self.boundary_event_count,
            "temporal_context_event_count": self.temporal_context_event_count,
            "target_count": len(targets),
            "voting_hostile_creature_target_count": voting_targets,
            "nonvoting_target_count": len(targets) - voting_targets,
            "target_lane_membership_counts": {
                lane: len(guids) for lane, guids in sorted(membership_sets.items())
            },
            "targets": targets,
            "scientific_boundaries": {
                "position_or_coordinates_inferred": False,
                "exact_health_claimed": False,
                "hostile_objects_promoted_to_simulator_targets": False,
                "hostile_players_promoted_to_simulator_targets": False,
                "unknown_entities_promoted_to_simulator_targets": False,
            },
        }
        return _content_addressed(core)


@dataclass
class EncounterAccumulator:
    context: SourceContext
    encounter_id: str
    encounter_ordinal: int
    first_timestamp_ms: int
    combat_gap_ms: int
    source_row_count: int = 0
    event_type_counts: Counter[str] = field(default_factory=Counter)
    accounting_counts: Counter[str] = field(default_factory=Counter)
    classification_lane_event_counts: Counter[str] = field(default_factory=Counter)
    classification_lane_entities: dict[str, set[str]] = field(default_factory=dict)
    classification_history: list[dict[str, Any]] = field(default_factory=list)
    classification_pair_transition_count: int = 0
    classification_relation_transition_count: int = 0
    owner_event_count: int = 0
    owner_exact_player_resolution_count: int = 0
    controller_event_count: int = 0
    controller_exact_player_resolution_count: int = 0
    states: dict[str, EntityState] = field(default_factory=dict)
    entity_activity: dict[str, TargetAccumulator] = field(default_factory=dict)
    waves: list[dict[str, Any]] = field(default_factory=list)
    current_wave: WaveAccumulator | None = None
    unassigned_event_type_counts: Counter[str] = field(default_factory=Counter)
    unassigned_lane_event_counts: Counter[str] = field(default_factory=Counter)
    unassigned_entities: set[str] = field(default_factory=set)
    first_unassigned_anchor: Mapping[str, Any] | None = None
    last_unassigned_anchor: Mapping[str, Any] | None = None
    damage_event_count: int = 0
    damage_amount: int = 0
    negative_damage_event_count: int = 0
    negative_damage_signed_amount: int = 0
    first_negative_damage_anchor: Mapping[str, Any] | None = None
    heal_event_count: int = 0
    heal_amount: int = 0
    death_event_count: int = 0
    death_nested_attribution_count: int = 0
    first_anchor: Mapping[str, Any] | None = None
    last_anchor: Mapping[str, Any] | None = None

    def _current_state(self, guid: str | None) -> EntityState | None:
        return self.states.get(guid or "")

    def _observe_classification(self, row: Mapping[str, Any]) -> None:
        current = _state_from_class_row(row, self.context)
        prior = self.states.get(current.guid)
        if prior is not None:
            prior_pair = (prior.unit_type_numeric, prior.affiliation_numeric)
            next_pair = (current.unit_type_numeric, current.affiliation_numeric)
            if prior_pair != next_pair:
                self.classification_pair_transition_count += 1
            prior_relations = (prior.owner_guid, prior.controller_guid, prior.spell_id)
            next_relations = (
                current.owner_guid,
                current.controller_guid,
                current.spell_id,
            )
            if prior_relations != next_relations:
                self.classification_relation_transition_count += 1
        self.states[current.guid] = current
        self.classification_lane_event_counts[current.lane] += 1
        self.classification_lane_entities.setdefault(current.lane, set()).add(
            current.guid
        )
        if current.owner_guid is not None:
            self.owner_event_count += 1
            if current.owner_player is not None:
                self.owner_exact_player_resolution_count += 1
        if current.controller_guid is not None:
            self.controller_event_count += 1
            if current.controller_player is not None:
                self.controller_exact_player_resolution_count += 1
        compact = current.compact()
        compact["prior_state_observed"] = prior is not None
        self.classification_history.append(compact)
        if current.lane in TARGET_LANES:
            self.entity_activity.setdefault(
                current.guid, TargetAccumulator(current.guid)
            ).observe_classification(current)

    def _relevant_states(
        self, row: Mapping[str, Any]
    ) -> list[tuple[str, EntityState]]:
        result: list[tuple[str, EntityState]] = []
        for label, raw in (
            ("source", row.get("source_guid")),
            ("target", row.get("target_guid")),
        ):
            guid = _guid(raw, label=f"row.{label}_guid")
            if guid is None:
                continue
            result.append((label, self.states.get(guid) or _unknown_state(guid)))
        return result

    def _observe_entity_activity(
        self,
        row: Mapping[str, Any],
        relevant: Sequence[tuple[str, EntityState]],
        amount: int | None,
    ) -> None:
        source_guid = _guid(row.get("source_guid"), label="row.source_guid")
        actor = _source_actor(source_guid, self.states, self.context)
        for role, state in relevant:
            if state.lane not in TARGET_LANES:
                continue
            target = self.entity_activity.setdefault(
                state.guid, TargetAccumulator(state.guid)
            )
            _observe_target_event(
                target,
                row,
                state,
                role=role,
                amount=amount,
                actor=actor,
            )

    def _mark_unassigned(
        self,
        row: Mapping[str, Any],
        relevant: Sequence[tuple[str, EntityState]],
    ) -> None:
        self.accounting_counts["UNASSIGNED_NO_HOSTILE_CREATURE_WAVE"] += 1
        self.unassigned_event_type_counts[str(row.get("type"))] += 1
        anchor = _anchor(row)
        if self.first_unassigned_anchor is None:
            self.first_unassigned_anchor = anchor
        self.last_unassigned_anchor = anchor
        for _, state in relevant:
            if state.lane in TARGET_LANES:
                self.unassigned_lane_event_counts[state.lane] += 1
                self.unassigned_entities.add(state.guid)

    def observe(self, row: Mapping[str, Any]) -> None:
        anchor = _anchor(row)
        if self.first_anchor is None:
            self.first_anchor = anchor
        self.last_anchor = anchor
        self.source_row_count += 1
        event_type = _text(row.get("type"), label="row.type").upper()
        if event_type not in SUPPORTED_EVENT_TYPES:
            raise ChronicleExternalReconstructionV2Error(
                f"unsupported admitted event type {event_type}"
            )
        self.event_type_counts[event_type] += 1
        if event_type == "CLASS":
            self.accounting_counts["CLASSIFICATION_STATE"] += 1
            self._observe_classification(row)
            return

        amount: int | None = None
        if event_type == "DMG":
            signed_amount = _integer(row.get("value"), label="DMG.value")
            if signed_amount < 0:
                self.negative_damage_event_count += 1
                self.negative_damage_signed_amount += signed_amount
                if self.first_negative_damage_anchor is None:
                    self.first_negative_damage_anchor = anchor
            else:
                amount = signed_amount
                self.damage_event_count += 1
                self.damage_amount += amount
        elif event_type == "HEAL":
            amount = _nonnegative_integer(row.get("value"), label="HEAL.value")
            self.heal_event_count += 1
            self.heal_amount += amount
        elif event_type == "DEAD":
            if row.get("value") is not None:
                raise ChronicleExternalReconstructionV2Error(
                    "DEAD.value must be absent; slain attribution may not enter damage accounting"
                )
            self.death_event_count += 1
            official = _mapping(row.get("official"), label="DEAD.official")
            message = _mapping(official.get("message"), label="DEAD.official.message")
            if isinstance(message.get("attribution"), Mapping):
                self.death_nested_attribution_count += 1

        relevant = self._relevant_states(row)
        self._observe_entity_activity(row, relevant, amount)
        has_hostile_creature = any(
            state.lane == LANE_HOSTILE_CREATURE for _, state in relevant
        )
        offset_ms = int(anchor["offset_ms"])
        boundary = event_type in BOUNDARY_EVENT_TYPES and has_hostile_creature
        if boundary:
            if (
                self.current_wave is not None
                and offset_ms - self.current_wave.last_boundary_offset_ms
                > self.combat_gap_ms
            ):
                self.waves.append(self.current_wave.finish())
                self.current_wave = None
            if self.current_wave is None:
                self.current_wave = WaveAccumulator.start(
                    self.encounter_id,
                    len(self.waves) + 1,
                    row,
                    combat_gap_ms=self.combat_gap_ms,
                )
            self.current_wave.observe(
                row,
                self.states,
                self.context,
                boundary=True,
                amount=amount,
            )
            self.accounting_counts["WAVE_BOUNDARY_EVENT"] += 1
            return

        within_active_window = (
            self.current_wave is not None
            and offset_ms - self.current_wave.last_boundary_offset_ms
            <= self.combat_gap_ms
        )
        if within_active_window:
            assert self.current_wave is not None
            self.current_wave.observe(
                row,
                self.states,
                self.context,
                boundary=False,
                amount=amount,
            )
            self.accounting_counts["WAVE_TEMPORAL_CONTEXT_EVENT"] += 1
        else:
            self._mark_unassigned(row, relevant)

    def finish(self) -> dict[str, Any]:
        if self.current_wave is not None:
            self.waves.append(self.current_wave.finish())
            self.current_wave = None
        if sum(self.accounting_counts.values()) != self.source_row_count:
            raise ChronicleExternalReconstructionV2Error(
                "encounter row accounting is not conserved"
            )

        lane_unique_counts = {
            lane: len(guids)
            for lane, guids in sorted(self.classification_lane_entities.items())
        }
        final_lane_counts = Counter(state.lane for state in self.states.values())
        nonvoting_entities: dict[str, list[dict[str, Any]]] = {}
        for lane in sorted(NONVOTING_TARGET_LANES):
            values = []
            for guid in sorted(
                key
                for key, accumulator in self.entity_activity.items()
                if lane in accumulator.lane_memberships
            ):
                values.append(self.entity_activity[guid].finish())
            nonvoting_entities[lane] = values

        wave_target_count = sum(int(wave["target_count"]) for wave in self.waves)
        voting_wave_target_count = sum(
            int(wave["voting_hostile_creature_target_count"])
            for wave in self.waves
        )
        nonvoting_wave_target_count = sum(
            int(wave["nonvoting_target_count"]) for wave in self.waves
        )
        artifact_core = {
            "schema": ARTIFACT_SCHEMA,
            "kind": ARTIFACT_KIND,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "status": STATUS,
            "instance_id": self.context.instance_id,
            "encounter_id": self.encounter_id,
            "encounter_ordinal": self.encounter_ordinal,
            "first_timestamp_ms": self.first_timestamp_ms,
            "source_binding": deepcopy(dict(self.context.source_binding)),
            "instance_provenance": deepcopy(dict(self.context.artifact_provenance)),
            "ordering_contract": {
                "input": [
                    "encounter_ordinal",
                    "timestamp_ms",
                    "EventMeta.index",
                    "fixed_stream_tiebreaker",
                    "frame_message_index",
                ],
                "encounter_processed_contiguously": True,
                "future_classification_backfill_allowed": False,
            },
            "classification": {
                "observation_count": len(self.classification_history),
                "pair_transition_count": self.classification_pair_transition_count,
                "relation_transition_count": (
                    self.classification_relation_transition_count
                ),
                "lane_observation_counts": dict(
                    sorted(self.classification_lane_event_counts.items())
                ),
                "lane_unique_entity_counts": lane_unique_counts,
                "final_lane_entity_counts": dict(sorted(final_lane_counts.items())),
                "owner_observation_count": self.owner_event_count,
                "owner_exact_player_resolution_count": (
                    self.owner_exact_player_resolution_count
                ),
                "controller_observation_count": self.controller_event_count,
                "controller_exact_player_resolution_count": (
                    self.controller_exact_player_resolution_count
                ),
                "future_classification_backfill_count": 0,
                "owner_or_controller_suffix_match_count": 0,
                "history": self.classification_history,
            },
            "event_accounting": {
                "source_row_count": self.source_row_count,
                "event_type_counts": dict(sorted(self.event_type_counts.items())),
                "exclusive_row_lane_counts": dict(
                    sorted(self.accounting_counts.items())
                ),
                "damage": {
                    "event_count": self.damage_event_count,
                    "amount": self.damage_amount,
                    "amount_source": "DMG_ONLY",
                },
                "healing": {
                    "event_count": self.heal_event_count,
                    "amount": self.heal_amount,
                    "amount_source": "HEAL_ONLY",
                },
                "slain": {
                    "event_count": self.death_event_count,
                    "nested_attribution_diagnostic_count": (
                        self.death_nested_attribution_count
                    ),
                    "amount_added_to_damage": 0,
                    "use": "DEATH_ANCHOR_ONLY",
                },
            },
            "waves": self.waves,
            "nonvoting_target_lanes": nonvoting_entities,
            "unassigned_activity": {
                "event_count": self.accounting_counts.get(
                    "UNASSIGNED_NO_HOSTILE_CREATURE_WAVE", 0
                ),
                "event_type_counts": dict(
                    sorted(self.unassigned_event_type_counts.items())
                ),
                "lane_touch_counts": dict(
                    sorted(self.unassigned_lane_event_counts.items())
                ),
                "unique_entity_count": len(self.unassigned_entities),
                "first_anchor": (
                    dict(self.first_unassigned_anchor)
                    if self.first_unassigned_anchor
                    else None
                ),
                "last_anchor": (
                    dict(self.last_unassigned_anchor)
                    if self.last_unassigned_anchor
                    else None
                ),
            },
            "summary": {
                "source_row_count": self.source_row_count,
                "wave_count": len(self.waves),
                "wave_target_observation_count": wave_target_count,
                "voting_hostile_creature_target_observation_count": (
                    voting_wave_target_count
                ),
                "nonvoting_wave_target_observation_count": (
                    nonvoting_wave_target_count
                ),
                "classified_entity_count": len(self.states),
                "hostile_object_entity_count": lane_unique_counts.get(
                    LANE_HOSTILE_OBJECT, 0
                ),
                "hostile_player_entity_count": lane_unique_counts.get(
                    LANE_HOSTILE_PLAYER, 0
                ),
                "unknown_entity_count": lane_unique_counts.get(LANE_UNKNOWN, 0),
                "classification_observation_count": len(
                    self.classification_history
                ),
                "owner_observation_count": self.owner_event_count,
                "owner_exact_player_resolution_count": (
                    self.owner_exact_player_resolution_count
                ),
                "controller_observation_count": self.controller_event_count,
                "controller_exact_player_resolution_count": (
                    self.controller_exact_player_resolution_count
                ),
                "damage_event_count": self.damage_event_count,
                "damage_amount": self.damage_amount,
                "death_event_count": self.death_event_count,
                "death_nested_attribution_diagnostic_count": (
                    self.death_nested_attribution_count
                ),
            },
            "scientific_boundaries": {
                "legacy_csv_v1_input_or_provenance_used": False,
                "frozen_50_capsule_member": False,
                "comparison_authorized": False,
                "policy_training_authorized": False,
                "raw_or_normalized_event_rows_copied": False,
                "hostile_creature_inferred_without_class": False,
                "name_based_owner_or_controller_resolution": False,
                "slain_attribution_projected_to_damage": False,
            },
            "first_anchor": dict(self.first_anchor) if self.first_anchor else None,
            "last_anchor": dict(self.last_anchor) if self.last_anchor else None,
        }
        if self.negative_damage_event_count:
            artifact_core["event_accounting"]["damage"].update(
                {
                    "negative_event_count_excluded": (
                        self.negative_damage_event_count
                    ),
                    "negative_signed_amount_excluded": (
                        self.negative_damage_signed_amount
                    ),
                    "absolute_amount_excluded": abs(
                        self.negative_damage_signed_amount
                    ),
                    "first_excluded_anchor": dict(
                        self.first_negative_damage_anchor or {}
                    ),
                    "policy": (
                        "PRESERVED_DIAGNOSTIC_NONVOTING_NO_ABS_OR_CLAMP"
                    ),
                }
            )
            artifact_core["summary"]["negative_damage_event_count"] = (
                self.negative_damage_event_count
            )
            artifact_core["summary"]["negative_damage_absolute_amount_excluded"] = (
                abs(self.negative_damage_signed_amount)
            )
            artifact_core["scientific_boundaries"][
                "negative_damage_amount_promoted_to_damage"
            ] = False
        return _content_addressed(artifact_core)


def _source_context_from_document(
    admission: Mapping[str, Any],
    resolved_path: Path,
    instance: Mapping[str, Any],
) -> SourceContext:
    if instance.get("status") != ADMISSION_STATUS:
        raise ChronicleExternalReconstructionV2Error(
            "admission instance status is unsupported"
        )
    consumers = _mapping(instance.get("consumer_status"), label="consumer_status")
    if (
        consumers.get("versioned_reconstruction_input") is not True
        or consumers.get("legacy_raw_csv_provenance") is not False
        or consumers.get("comparison_authorized") is not False
        or consumers.get("frozen_50_capsule_member") is not False
    ):
        raise ChronicleExternalReconstructionV2Error(
            "admission consumer boundary is not safe for External V2"
        )

    data_root = _data_root_for(resolved_path)
    payload = resolved_path.read_bytes()
    admission_content_sha = _verify_content_address(
        admission, label="admission manifest"
    )
    inputs = _mapping(admission.get("inputs"), label="admission.inputs")
    raw_input = deepcopy(
        dict(_mapping(inputs.get("raw_api_manifest"), label="raw_api_manifest"))
    )
    normalized_input = deepcopy(
        dict(
            _mapping(
                inputs.get("normalization_manifest"),
                label="normalization_manifest",
            )
        )
    )
    source_evidence = _mapping(
        instance.get("source_evidence"), label="instance.source_evidence"
    )
    if source_evidence.get("kind") != SOURCE_EVIDENCE_KIND:
        raise ChronicleExternalReconstructionV2Error(
            "instance source evidence is not an external API stream set"
        )
    partition = deepcopy(
        dict(
            _mapping(
                source_evidence.get("normalized_partition"),
                label="normalized_partition",
            )
        )
    )
    resolver_wrapper = _mapping(
        instance.get("metadata_player_resolver"), label="metadata_player_resolver"
    )
    players = _array(resolver_wrapper.get("players"), label="resolver.players")
    player_resolver: dict[str, Mapping[str, Any]] = {}
    canonical_players: list[dict[str, Any]] = []
    for raw in players:
        player = deepcopy(dict(_mapping(raw, label="resolver player")))
        guid = _guid(player.get("guid"), label="resolver player GUID")
        if guid is None or guid in player_resolver:
            raise ChronicleExternalReconstructionV2Error(
                "metadata player resolver contains an absent or duplicate GUID"
            )
        player["guid"] = guid
        player_resolver[guid] = player
        canonical_players.append(player)
    canonical_players.sort(key=lambda item: str(item["guid"]))
    resolver_sha = _sha256_bytes(_canonical_bytes(canonical_players))
    if resolver_sha != resolver_wrapper.get("players_sha256"):
        raise ChronicleExternalReconstructionV2Error(
            "metadata player resolver content hash mismatch"
        )
    if len(canonical_players) != resolver_wrapper.get("player_count"):
        raise ChronicleExternalReconstructionV2Error(
            "metadata player resolver count mismatch"
        )

    temporal = deepcopy(
        dict(
            _mapping(
                instance.get("temporal_and_guild_provenance"),
                label="temporal_and_guild_provenance",
            )
        )
    )
    warrior_spec = deepcopy(
        dict(
            _mapping(
                instance.get("warrior_spec_evidence"),
                label="warrior_spec_evidence",
            )
        )
    )
    full_instance_provenance = {
        "temporal_and_guild_provenance": temporal,
        "warrior_spec_evidence": warrior_spec,
        "metadata_player_resolver": {
            "kind": resolver_wrapper.get("kind"),
            "player_count": len(canonical_players),
            "players_sha256": resolver_sha,
            "name_or_class_inference_used": resolver_wrapper.get(
                "name_or_class_inference_used"
            ),
        },
        "combatant_info_evidence": deepcopy(
            dict(
                _mapping(
                    instance.get("combatant_info_evidence"),
                    label="combatant_info_evidence",
                )
            )
        ),
    }
    artifact_provenance = _artifact_provenance_from_full(
        full_instance_provenance
    )
    source_binding = {
        "raw_api_manifest": raw_input,
        "normalized_manifest": normalized_input,
        "normalized_partition": {
            key: partition.get(key)
            for key in (
                "path",
                "compressed_file_sha256",
                "logical_content_sha256",
                "record_count",
                "encounter_count",
            )
        },
        "admission_manifest": {
            "path": _relative_to(resolved_path, data_root, label="admission manifest"),
            "schema": ADMISSION_SCHEMA,
            "implementation_revision": ADMISSION_IMPLEMENTATION_REVISION,
            "content_sha256": admission_content_sha,
            "file_sha256": _sha256_bytes(payload),
            "size_bytes": len(payload),
        },
        "classification_resolver": {
            "schema": CLASSIFICATION_SCHEMA,
            "implementation_revision": CLASSIFICATION_IMPLEMENTATION_REVISION,
            "official_commit": OFFICIAL_COMMIT,
            "official_evidence_sha256": OFFICIAL_EVIDENCE_SHA256,
        },
        "metadata_player_resolver": {
            "kind": resolver_wrapper.get("kind"),
            "player_count": len(canonical_players),
            "players_sha256": resolver_sha,
            "resolution": "EXACT_FULL_CANONICAL_GUID_ONLY",
            "name_inference_used": False,
        },
    }
    return SourceContext(
        admission_manifest_path=resolved_path,
        data_root=data_root,
        instance_id=_text(instance.get("instance_id"), label="instance_id"),
        instance=instance,
        player_resolver=player_resolver,
        source_binding=source_binding,
        instance_provenance=full_instance_provenance,
        artifact_provenance=artifact_provenance,
    )


def _source_contexts(admission_path: str | Path) -> list[SourceContext]:
    """Return one independently bound context per admitted instance.

    The admission loader enforces unique, sorted instance identities and
    distinct normalized partition paths.  Reconstruction retains that order
    and never opens one instance's partition with another instance's resolver
    or temporal/guild provenance.
    """

    admission, resolved_path = load_admission_manifest(admission_path)
    if (
        admission.get("schema") != ADMISSION_SCHEMA
        or admission.get("status") != ADMISSION_STATUS
        or admission.get("implementation_revision")
        != ADMISSION_IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalReconstructionV2Error(
            "input is not the supported verified external admission manifest"
        )
    raw_instances = _array(admission.get("instances"), label="admission.instances")
    if not raw_instances:
        raise ChronicleExternalReconstructionV2Error(
            "External V2 requires at least one admitted instance"
        )
    instances = [
        _mapping(raw, label=f"admission.instances[{index}]")
        for index, raw in enumerate(raw_instances)
    ]
    instance_ids = [
        _text(instance.get("instance_id"), label="instance_id")
        for instance in instances
    ]
    if instance_ids != sorted(instance_ids) or len(instance_ids) != len(
        set(instance_ids)
    ):
        raise ChronicleExternalReconstructionV2Error(
            "admitted instances must have unique, deterministic instance-id order"
        )
    if len({value.casefold() for value in instance_ids}) != len(instance_ids):
        raise ChronicleExternalReconstructionV2Error(
            "admitted instance ids collide on a case-insensitive filesystem"
        )
    return [
        _source_context_from_document(admission, resolved_path, instance)
        for instance in instances
    ]


def _source_context(
    admission_path: str | Path, *, instance_id: str | None = None
) -> SourceContext:
    """Return one context, retaining the historical single-instance helper.

    A multi-instance admission must be selected explicitly so legacy callers
    cannot silently bind themselves to the first partition.
    """

    contexts = _source_contexts(admission_path)
    if instance_id is None:
        if len(contexts) != 1:
            raise ChronicleExternalReconstructionV2Error(
                "multi-instance admission requires an explicit instance_id"
            )
        return contexts[0]
    selected = [context for context in contexts if context.instance_id == instance_id]
    if len(selected) != 1:
        raise ChronicleExternalReconstructionV2Error(
            "requested admitted instance is absent or duplicated"
        )
    return selected[0]


def _row_order(
    row: Mapping[str, Any],
    *,
    context: SourceContext,
) -> tuple[int, int, int, int, int]:
    if row.get("instance") != context.instance_id:
        raise ChronicleExternalReconstructionV2Error(
            "row instance does not match admitted instance"
        )
    encounter_ordinal = _nonnegative_integer(
        row.get("encounter_ordinal"), label="row.encounter_ordinal"
    )
    first_timestamp_ms = _nonnegative_integer(
        row.get("first_timestamp_ms"), label="row.first_timestamp_ms"
    )
    offset_ms = _nonnegative_integer(row.get("offset_ms"), label="row.offset_ms")
    timestamp_ms = _nonnegative_integer(
        row.get("timestamp_ms"), label="row.timestamp_ms"
    )
    if first_timestamp_ms + offset_ms != timestamp_ms:
        raise ChronicleExternalReconstructionV2Error(
            "row timestamp does not equal encounter origin plus EventMeta offset"
        )
    event_index = _integer(row.get("event_index"), label="row.event_index")
    provenance = _mapping(row.get("provenance"), label="row.provenance")
    stream_type = _text(
        provenance.get("stream_type"), label="row.provenance.stream_type"
    )
    try:
        stream_order = STREAM_ORDER[stream_type]
    except KeyError as error:
        raise ChronicleExternalReconstructionV2Error(
            f"row has unsupported stream type {stream_type}"
        ) from error
    frame_message_index = _nonnegative_integer(
        provenance.get("frame_message_index"),
        label="row.provenance.frame_message_index",
    )
    official = _mapping(row.get("official"), label="row.official")
    message = _mapping(official.get("message"), label="row.official.message")
    meta = _mapping(message.get("meta"), label="row.official.message.meta")
    if (
        meta.get("event_index") != event_index
        or meta.get("offset_ms") != offset_ms
        or official.get("stream_type") != stream_type
    ):
        raise ChronicleExternalReconstructionV2Error(
            "row projection conflicts with official EventMeta or stream type"
        )
    return (
        encounter_ordinal,
        timestamp_ms,
        event_index,
        stream_order,
        frame_message_index,
    )


def _write_gzip_artifact(
    artifact: Mapping[str, Any],
    *,
    encounter_directory: Path,
    output_directory: Path,
) -> tuple[Path, dict[str, Any]]:
    content_sha = _verify_content_address(artifact, label="encounter artifact")
    encounter_id = _safe_component(
        artifact.get("encounter_id"), label="encounter_id"
    )
    ordinal = _nonnegative_integer(
        artifact.get("encounter_ordinal"), label="encounter_ordinal"
    )
    final = encounter_directory / (
        f"{ordinal:03d}.{encounter_id}.{content_sha}.json.gz"
    )
    payload = _canonical_bytes(artifact) + b"\n"
    descriptor, name = tempfile.mkstemp(
        prefix=f".{ordinal:03d}.{encounter_id}.",
        suffix=".json.gz.tmp",
        dir=encounter_directory,
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
            ) as compressed:
                compressed.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        file_sha = _sha256_file(temporary)
        size = temporary.stat().st_size
        if final.exists():
            if final.stat().st_size != size or _sha256_file(final) != file_sha:
                raise ChronicleExternalReconstructionV2Error(
                    f"immutable encounter artifact differs: {final}"
                )
            temporary.unlink()
        else:
            temporary.replace(final)
        entry = {
            "encounter_id": encounter_id,
            "encounter_ordinal": ordinal,
            "artifact": {
                "path": _relative_to(
                    final, output_directory, label="encounter artifact"
                ),
                "content_sha256": content_sha,
                "compressed_file_sha256": file_sha,
                "compressed_size_bytes": size,
                "logical_size_bytes": len(payload),
                "schema": ARTIFACT_SCHEMA,
                "status": STATUS,
            },
            "summary": deepcopy(dict(artifact.get("summary", {}))),
        }
        return final, entry
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_temporary(path: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_immutable(temporary: Path, final: Path, expected_sha256: str) -> None:
    if final.exists():
        if _sha256_file(final) != expected_sha256:
            raise ChronicleExternalReconstructionV2Error(
                f"immutable content-addressed manifest differs: {final}"
            )
        temporary.unlink()
    else:
        temporary.replace(final)


def _manifest_summary(
    entries: Sequence[Mapping[str, Any]], *, instance_count: int = 1
) -> dict[str, Any]:
    keys = (
        "source_row_count",
        "wave_count",
        "wave_target_observation_count",
        "voting_hostile_creature_target_observation_count",
        "nonvoting_wave_target_observation_count",
        "classified_entity_count",
        "hostile_object_entity_count",
        "hostile_player_entity_count",
        "unknown_entity_count",
        "classification_observation_count",
        "owner_observation_count",
        "owner_exact_player_resolution_count",
        "controller_observation_count",
        "controller_exact_player_resolution_count",
        "damage_event_count",
        "damage_amount",
        "death_event_count",
        "death_nested_attribution_diagnostic_count",
    )
    totals = {
        key: sum(
            _nonnegative_integer(
                _mapping(entry.get("summary"), label="entry.summary").get(key),
                label=f"entry.summary.{key}",
            )
            for entry in entries
        )
        for key in keys
    }
    result = {
        "instance_count": instance_count,
        "encounter_count": len(entries),
        **totals,
        "compressed_artifact_bytes": sum(
            _nonnegative_integer(
                _mapping(entry.get("artifact"), label="entry.artifact").get(
                    "compressed_size_bytes"
                ),
                label="artifact.compressed_size_bytes",
            )
            for entry in entries
        ),
        "network_request_count": 0,
        "raw_row_copy_count": 0,
        "normalized_row_copy_count": 0,
    }
    negative_event_count = sum(
        _nonnegative_integer(
            _mapping(entry.get("summary"), label="entry.summary").get(
                "negative_damage_event_count", 0
            ),
            label="entry.summary.negative_damage_event_count",
        )
        for entry in entries
    )
    if negative_event_count:
        result["negative_damage_event_count"] = negative_event_count
        result["negative_damage_absolute_amount_excluded"] = sum(
            _nonnegative_integer(
                _mapping(entry.get("summary"), label="entry.summary").get(
                    "negative_damage_absolute_amount_excluded", 0
                ),
                label=(
                    "entry.summary.negative_damage_absolute_amount_excluded"
                ),
            )
            for entry in entries
        )
    return result


def _build_instance_encounters(
    *,
    context: SourceContext,
    encounter_directory: Path,
    output_directory: Path,
    combat_gap_ms: int,
) -> list[dict[str, Any]]:
    """Stream exactly one admitted partition into its own artifact set."""

    encounter_directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    current: EncounterAccumulator | None = None
    prior_order: tuple[int, int, int, int, int] | None = None
    completed_encounters: set[str] = set()
    last_encounter_ordinal: int | None = None

    rows = iter_resolved_reconstruction_rows(
        context.admission_manifest_path, instance_id=context.instance_id
    )
    for row in rows:
        order = _row_order(row, context=context)
        if prior_order is not None and order <= prior_order:
            raise ChronicleExternalReconstructionV2Error(
                "admitted EventMeta order is not strictly increasing"
            )
        prior_order = order
        encounter_id = _text(row.get("encounter"), label="row.encounter")
        encounter_ordinal = order[0]
        if current is None and not entries and encounter_ordinal != 0:
            raise ChronicleExternalReconstructionV2Error(
                "first encounter ordinal must be zero"
            )
        first_timestamp_ms = _nonnegative_integer(
            row.get("first_timestamp_ms"), label="row.first_timestamp_ms"
        )
        if current is None or encounter_id != current.encounter_id:
            if current is not None:
                artifact = current.finish()
                _, entry = _write_gzip_artifact(
                    artifact,
                    encounter_directory=encounter_directory,
                    output_directory=output_directory,
                )
                entries.append(entry)
                completed_encounters.add(current.encounter_id)
                last_encounter_ordinal = current.encounter_ordinal
            if encounter_id in completed_encounters:
                raise ChronicleExternalReconstructionV2Error(
                    "an encounter reappeared after its streaming group was closed"
                )
            if (
                last_encounter_ordinal is not None
                and encounter_ordinal != last_encounter_ordinal + 1
            ):
                raise ChronicleExternalReconstructionV2Error(
                    "encounter ordinals are not contiguous"
                )
            current = EncounterAccumulator(
                context=context,
                encounter_id=encounter_id,
                encounter_ordinal=encounter_ordinal,
                first_timestamp_ms=first_timestamp_ms,
                combat_gap_ms=combat_gap_ms,
            )
        elif (
            encounter_ordinal != current.encounter_ordinal
            or first_timestamp_ms != current.first_timestamp_ms
        ):
            raise ChronicleExternalReconstructionV2Error(
                "encounter ordinal or origin changed inside its streaming group"
            )
        current.observe(row)

    if current is not None:
        artifact = current.finish()
        _, entry = _write_gzip_artifact(
            artifact,
            encounter_directory=encounter_directory,
            output_directory=output_directory,
        )
        entries.append(entry)
    if not entries:
        raise ChronicleExternalReconstructionV2Error(
            f"admitted stream for {context.instance_id} produced no encounters"
        )

    declared_partition = _mapping(
        _mapping(
            context.instance.get("source_evidence"), label="source_evidence"
        ).get("normalized_partition"),
        label="normalized_partition",
    )
    if len(entries) != declared_partition.get("encounter_count"):
        raise ChronicleExternalReconstructionV2Error(
            "reconstructed encounter count differs from admitted partition"
        )
    total_rows = sum(int(entry["summary"]["source_row_count"]) for entry in entries)
    if total_rows != declared_partition.get("record_count"):
        raise ChronicleExternalReconstructionV2Error(
            "reconstructed row count differs from admitted partition"
        )
    return entries


def _build_instance_worker(
    arguments: tuple[SourceContext, Path, Path, int]
) -> tuple[str, list[dict[str, Any]]]:
    """Pickle-safe worker for one fully isolated admitted instance."""

    context, encounter_directory, output_directory, combat_gap_ms = arguments
    entries = _build_instance_encounters(
        context=context,
        encounter_directory=encounter_directory,
        output_directory=output_directory,
        combat_gap_ms=combat_gap_ms,
    )
    return context.instance_id, entries


def _reconstruction_contract(combat_gap_ms: int) -> dict[str, Any]:
    return {
        "input": "verified external admission iterator only",
        "legacy_csv_v1_input_accepted": False,
        "event_order": [
            "encounter_ordinal",
            "timestamp_ms",
            "EventMeta.index",
            "fixed_stream_tiebreaker",
            "frame_message_index",
        ],
        "streaming_memory_scope": "one encounter accumulator plus compact manifest entries",
        "physical_input_passes": (
            "one full compressed/logical integrity pass before first yield, then "
            "one streaming semantic pass"
        ),
        "combat_gap_ms": combat_gap_ms,
        "wave_boundary_driver": "official HOSTILE_CREATURE core activity only",
        "damage_amount_source": "DMG_ONLY",
        "slain_use": "DEATH_ANCHOR_ONLY",
        "owner_controller_resolution": (
            "official full GUID plus exact metadata resolver; controller priority"
        ),
        "name_based_identity_inference": False,
        "hostile_object_lane": "PRESERVED_NONVOTING",
        "hostile_player_lane": "PRESERVED_NONVOTING",
        "unknown_lane": "PRESERVED_NONVOTING",
        "future_classification_backfill_count": 0,
        "owner_or_controller_suffix_match_count": 0,
        "relation_chain_max_depth": 5,
        "relation_cycle_policy": "UNRESOLVED_NONVOTING_ATTRIBUTION",
        "armor": "MISSING_NOT_IN_EXTERNAL_CORE_STREAM_SET",
    }


def _publication_contract() -> dict[str, Any]:
    return {
        "per_encounter_artifacts_content_addressed": True,
        "wave_and_target_records_content_addressed": True,
        "content_addressed_manifest_published_before_stable_pointer": True,
        "stable_manifest_committed_last": True,
        "stable_and_addressed_manifest_bytes_equal": True,
        "gzip_mtime": 0,
        "raw_or_normalized_rows_copied": False,
    }


def _scientific_boundaries() -> dict[str, Any]:
    return {
        "legacy_manual_csv_contract_modified": False,
        "manual_export_queue_impersonated": False,
        "frozen_50_capsule_membership_claimed": False,
        "comparison_or_policy_promotion_authorized": False,
        "protocol_modified": False,
        "status_is_reconstruction_only": True,
    }


def build_external_encounter_reconstruction(
    *,
    admission_manifest_path: str | Path,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    combat_gap_ms: int = DEFAULT_COMBAT_GAP_MS,
    workers: int = 1,
) -> dict[str, Any]:
    """Build deterministic External V2 encounter artifacts and manifest-last."""

    if isinstance(combat_gap_ms, bool) or not isinstance(combat_gap_ms, int):
        raise ChronicleExternalReconstructionV2Error(
            "combat_gap_ms must be an integer"
        )
    if combat_gap_ms <= 0:
        raise ChronicleExternalReconstructionV2Error(
            "combat_gap_ms must be positive"
        )
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise ChronicleExternalReconstructionV2Error("workers must be an integer")
    if workers < 1 or workers > 64:
        raise ChronicleExternalReconstructionV2Error(
            "workers must be between 1 and 64"
        )
    contexts = _source_contexts(admission_manifest_path)
    output = Path(output_directory).expanduser().resolve()
    for context in contexts:
        if context.data_root != contexts[0].data_root:
            raise ChronicleExternalReconstructionV2Error(
                "admitted instances do not share one offline_data root"
            )
    _relative_to(output, contexts[0].data_root, label="output directory")

    instance_results_by_id: dict[str, list[dict[str, Any]]] = {}
    multi_instance = len(contexts) > 1
    worker_arguments: list[tuple[SourceContext, Path, Path, int]] = []
    for context in contexts:
        if multi_instance:
            component = _safe_component(context.instance_id, label="instance_id")
            encounter_directory = output / "instances" / component / "encounters"
        else:
            # Keep the historical one-instance artifact paths and manifest
            # bytes stable while extending the same contract to batches.
            encounter_directory = output / "encounters"
        worker_arguments.append(
            (context, encounter_directory, output, combat_gap_ms)
        )
    effective_workers = min(workers, len(worker_arguments))
    if effective_workers == 1:
        for arguments in worker_arguments:
            instance_id, entries = _build_instance_worker(arguments)
            instance_results_by_id[instance_id] = entries
    else:
        with ProcessPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(_build_instance_worker, arguments): arguments[0].instance_id
                for arguments in worker_arguments
            }
            try:
                for future in as_completed(futures):
                    instance_id, entries = future.result()
                    if instance_id != futures[future]:
                        raise ChronicleExternalReconstructionV2Error(
                            "worker returned a cross-instance result"
                        )
                    instance_results_by_id[instance_id] = entries
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    instance_results = [
        (context, instance_results_by_id[context.instance_id])
        for context in contexts
    ]

    common_core = {
        "schema": SCHEMA,
        "kind": KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "classification_resolver_evidence": official_evidence_contract(),
        "reconstruction_contract": _reconstruction_contract(combat_gap_ms),
        "publication_contract": _publication_contract(),
        "scientific_boundaries": _scientific_boundaries(),
    }
    if not multi_instance:
        context, entries = instance_results[0]
        manifest_core = {
            **common_core,
            "source_binding": deepcopy(dict(context.source_binding)),
            "instance_provenance": deepcopy(dict(context.instance_provenance)),
            "summary": _manifest_summary(entries),
            "encounters": entries,
        }
    else:
        first_binding = instance_results[0][0].source_binding
        shared_binding = {
            key: deepcopy(dict(_mapping(first_binding.get(key), label=key)))
            for key in (
                "raw_api_manifest",
                "normalized_manifest",
                "admission_manifest",
                "classification_resolver",
            )
        }
        for context, _ in instance_results[1:]:
            for key in shared_binding:
                if context.source_binding.get(key) != shared_binding[key]:
                    raise ChronicleExternalReconstructionV2Error(
                        f"instance source bindings disagree on shared {key}"
                    )
        flattened = [
            entry for _, entries in instance_results for entry in entries
        ]
        manifest_core = {
            **common_core,
            "source_binding": {
                **shared_binding,
                "normalized_partition_binding_location": (
                    "instances[].source_binding.normalized_partition"
                ),
                "metadata_player_resolver_binding_location": (
                    "instances[].source_binding.metadata_player_resolver"
                ),
            },
            "instance_provenance": {
                "scope": "per_instance",
                "location": "instances[].instance_provenance",
                "instance_count": len(instance_results),
            },
            "summary": _manifest_summary(
                flattened, instance_count=len(instance_results)
            ),
            "instances": [
                {
                    "status": STATUS,
                    "instance_id": context.instance_id,
                    "source_binding": {
                        "normalized_partition": deepcopy(
                            dict(
                                _mapping(
                                    context.source_binding.get(
                                        "normalized_partition"
                                    ),
                                    label="normalized_partition",
                                )
                            )
                        ),
                        "metadata_player_resolver": deepcopy(
                            dict(
                                _mapping(
                                    context.source_binding.get(
                                        "metadata_player_resolver"
                                    ),
                                    label="metadata_player_resolver",
                                )
                            )
                        ),
                    },
                    "instance_provenance": deepcopy(
                        dict(context.instance_provenance)
                    ),
                    "summary": _manifest_summary(entries),
                    "encounters": entries,
                }
                for context, entries in instance_results
            ],
        }
    if manifest_core["summary"].get("negative_damage_event_count", 0):
        manifest_core["reconstruction_contract"][
            "negative_damage_value_policy"
        ] = "PRESERVED_DIAGNOSTIC_NONVOTING_NO_ABS_OR_CLAMP"
    manifest = _content_addressed(manifest_core)
    content_sha = _verify_content_address(manifest, label="reconstruction manifest")
    payload = _canonical_bytes(manifest) + b"\n"
    file_sha = _sha256_bytes(payload)
    addressed = output / (
        f"chronicle_external_encounter_reconstruction_v2.{content_sha}.manifest.json"
    )
    stable = output / "manifest.json"
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        addressed_temporary = _write_temporary(addressed, payload)
        stable_temporary = _write_temporary(stable, payload)
        _publish_immutable(addressed_temporary, addressed, file_sha)
        addressed_temporary = None
        stable_temporary.replace(stable)
        stable_temporary = None
    finally:
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)

    return {
        "status": STATUS,
        "schema": SCHEMA,
        "manifest_path": str(stable),
        "content_addressed_manifest_path": str(addressed),
        "content_sha256": content_sha,
        "manifest_file_sha256": file_sha,
        "summary": manifest["summary"],
        "network_request_count": 0,
        "legacy_capsule_member": False,
        "comparison_authorized": False,
    }


def _validate_encounter_entries(
    entries: Sequence[Any],
    *,
    output: Path,
    expected_instance_id: str | None,
    expected_path_prefix: tuple[str, ...],
    expected_source_binding: Mapping[str, Any],
    expected_instance_provenance: Mapping[str, Any],
    verify_artifacts: bool,
    seen_artifact_paths: set[str],
) -> list[Mapping[str, Any]]:
    if not entries:
        raise ChronicleExternalReconstructionV2Error(
            "reconstruction instance contains no encounters"
        )
    validated: list[Mapping[str, Any]] = []
    encounter_ids: set[str] = set()
    observed_instance_id: str | None = None
    expected_artifact_provenance = _artifact_provenance_from_full(
        expected_instance_provenance
    )
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, label=f"encounters[{index}]")
        if _nonnegative_integer(
            entry.get("encounter_ordinal"), label="entry.encounter_ordinal"
        ) != index:
            raise ChronicleExternalReconstructionV2Error(
                "manifest encounter ordinals are not contiguous from zero"
            )
        encounter_id = _safe_component(
            entry.get("encounter_id"), label="entry.encounter_id"
        )
        if encounter_id in encounter_ids:
            raise ChronicleExternalReconstructionV2Error(
                "manifest contains duplicate encounter ids within an instance"
            )
        encounter_ids.add(encounter_id)
        reference = _mapping(entry.get("artifact"), label="encounter.artifact")
        if (
            reference.get("schema") != ARTIFACT_SCHEMA
            or reference.get("status") != STATUS
        ):
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact reference has an unsupported contract"
            )
        relative_text = _text(reference.get("path"), label="artifact.path")
        relative = Path(relative_text)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or tuple(relative.parts[: len(expected_path_prefix)])
            != expected_path_prefix
        ):
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact path escapes its instance artifact set"
            )
        canonical_relative = relative.as_posix()
        if canonical_relative != relative_text:
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact path is not canonical POSIX-relative form"
            )
        if canonical_relative in seen_artifact_paths:
            raise ChronicleExternalReconstructionV2Error(
                "reconstruction instances share an encounter artifact path"
            )
        seen_artifact_paths.add(canonical_relative)
        artifact_path = (output / relative).resolve()
        _relative_to(artifact_path, output, label="encounter artifact")
        # Structure and summary accounting are validated even when callers
        # explicitly skip the comparatively expensive artifact hash pass.
        _mapping(entry.get("summary"), label="encounter.summary")
        _nonnegative_integer(
            reference.get("compressed_size_bytes"),
            label="artifact.compressed_size_bytes",
        )
        _nonnegative_integer(
            reference.get("logical_size_bytes"),
            label="artifact.logical_size_bytes",
        )
        for field in ("compressed_file_sha256", "content_sha256"):
            digest = _text(reference.get(field), label=f"artifact.{field}")
            if _SHA256_RE.fullmatch(digest) is None:
                raise ChronicleExternalReconstructionV2Error(
                    f"artifact.{field} is not SHA-256"
                )
        validated.append(entry)
        if not verify_artifacts:
            continue

        expected_size = int(reference["compressed_size_bytes"])
        expected_file_sha = str(reference["compressed_file_sha256"])
        try:
            if artifact_path.stat().st_size != expected_size:
                raise ChronicleExternalReconstructionV2Error(
                    "encounter artifact size mismatch"
                )
            if _sha256_file(artifact_path) != expected_file_sha:
                raise ChronicleExternalReconstructionV2Error(
                    "encounter artifact compressed hash mismatch"
                )
            with gzip.open(artifact_path, "rb") as handle:
                logical = handle.read()
            if len(logical) != int(reference["logical_size_bytes"]):
                raise ChronicleExternalReconstructionV2Error(
                    "encounter artifact logical size mismatch"
                )
            artifact = json.loads(logical.decode("utf-8"))
        except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChronicleExternalReconstructionV2Error(
                f"cannot verify encounter artifact {artifact_path}: {error}"
            ) from error
        if not isinstance(artifact, dict):
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact is not an object"
            )
        if (
            artifact.get("schema") != ARTIFACT_SCHEMA
            or artifact.get("kind") != ARTIFACT_KIND
            or artifact.get("status") != STATUS
            or artifact.get("implementation_revision") != IMPLEMENTATION_REVISION
        ):
            raise ChronicleExternalReconstructionV2Error(
                "unsupported encounter artifact"
            )
        if (
            artifact.get("encounter_id") != encounter_id
            or artifact.get("encounter_ordinal") != index
            or artifact.get("summary") != entry.get("summary")
        ):
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact identity or summary differs from manifest"
            )
        artifact_instance_id = _safe_component(
            artifact.get("instance_id"), label="artifact.instance_id"
        )
        if observed_instance_id is None:
            observed_instance_id = artifact_instance_id
        elif artifact_instance_id != observed_instance_id:
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifacts cross instance boundaries"
            )
        if (
            expected_instance_id is not None
            and artifact_instance_id != expected_instance_id
        ):
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact crosses its declared instance boundary"
            )
        if artifact.get("source_binding") != expected_source_binding:
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact source binding differs from its instance"
            )
        if artifact.get("instance_provenance") != expected_artifact_provenance:
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact temporal/guild provenance differs from its instance"
            )
        artifact_sha = _verify_content_address(artifact, label="encounter artifact")
        if artifact_sha != reference.get("content_sha256"):
            raise ChronicleExternalReconstructionV2Error(
                "encounter artifact content hash differs from manifest"
            )
        for wave in _array(artifact.get("waves"), label="artifact.waves"):
            wave_map = _mapping(wave, label="wave")
            _verify_content_address(wave_map, label="wave")
            for target in _array(wave_map.get("targets"), label="wave.targets"):
                _verify_content_address(
                    _mapping(target, label="target"), label="target"
                )
        nonvoting = _mapping(
            artifact.get("nonvoting_target_lanes"),
            label="nonvoting_target_lanes",
        )
        for targets in nonvoting.values():
            for target in _array(targets, label="nonvoting target lane"):
                _verify_content_address(
                    _mapping(target, label="nonvoting target"),
                    label="nonvoting target",
                )
    return validated


def load_external_reconstruction_manifest(
    path: str | Path, *, verify_artifacts: bool = True
) -> tuple[dict[str, Any], Path]:
    """Load and optionally verify every content-addressed V2 artifact."""

    resolved = Path(path).expanduser().resolve()
    try:
        payload = resolved.read_bytes()
        manifest = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalReconstructionV2Error(
            f"cannot load reconstruction manifest {resolved}: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise ChronicleExternalReconstructionV2Error(
            "reconstruction manifest is not an object"
        )
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("kind") != KIND
        or manifest.get("status") != STATUS
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
    ):
        raise ChronicleExternalReconstructionV2Error(
            "unsupported reconstruction manifest"
        )
    content_sha = _verify_content_address(manifest, label="reconstruction manifest")
    addressed_name = (
        f"chronicle_external_encounter_reconstruction_v2.{content_sha}.manifest.json"
    )
    stable = resolved.with_name("manifest.json")
    addressed = resolved.with_name(addressed_name)
    if resolved.name not in {"manifest.json", addressed_name}:
        raise ChronicleExternalReconstructionV2Error(
            "reconstruction manifest filename/content address mismatch"
        )
    try:
        if stable.read_bytes() != payload or addressed.read_bytes() != payload:
            raise ChronicleExternalReconstructionV2Error(
                "stable and addressed reconstruction manifests differ"
            )
    except OSError as error:
        raise ChronicleExternalReconstructionV2Error(
            f"stable or addressed reconstruction manifest is absent: {error}"
        ) from error

    output = resolved.parent
    seen_artifact_paths: set[str] = set()
    if "instances" not in manifest:
        encounter_entries = _array(
            manifest.get("encounters"), label="manifest.encounters"
        )
        source_binding = _mapping(
            manifest.get("source_binding"), label="manifest.source_binding"
        )
        instance_provenance = _mapping(
            manifest.get("instance_provenance"),
            label="manifest.instance_provenance",
        )
        validated = _validate_encounter_entries(
            encounter_entries,
            output=output,
            expected_instance_id=None,
            expected_path_prefix=("encounters",),
            expected_source_binding=source_binding,
            expected_instance_provenance=instance_provenance,
            verify_artifacts=verify_artifacts,
            seen_artifact_paths=seen_artifact_paths,
        )
        if manifest.get("summary") != _manifest_summary(validated):
            raise ChronicleExternalReconstructionV2Error(
                "reconstruction manifest summary differs from encounter entries"
            )
    else:
        if "encounters" in manifest:
            raise ChronicleExternalReconstructionV2Error(
                "multi-instance manifest must not expose an ambiguous encounter list"
            )
        raw_instances = _array(
            manifest.get("instances"), label="manifest.instances"
        )
        if len(raw_instances) < 2:
            raise ChronicleExternalReconstructionV2Error(
                "instances layout is reserved for multi-instance reconstruction"
            )
        common_binding = _mapping(
            manifest.get("source_binding"), label="manifest.source_binding"
        )
        expected_common_binding_keys = {
            "raw_api_manifest",
            "normalized_manifest",
            "admission_manifest",
            "classification_resolver",
            "normalized_partition_binding_location",
            "metadata_player_resolver_binding_location",
        }
        if set(common_binding) != expected_common_binding_keys:
            raise ChronicleExternalReconstructionV2Error(
                "multi-instance common source binding has unsupported fields"
            )
        common_parts = {
            key: deepcopy(dict(_mapping(common_binding.get(key), label=key)))
            for key in (
                "raw_api_manifest",
                "normalized_manifest",
                "admission_manifest",
                "classification_resolver",
            )
        }
        if (
            common_binding.get("normalized_partition_binding_location")
            != "instances[].source_binding.normalized_partition"
            or common_binding.get("metadata_player_resolver_binding_location")
            != "instances[].source_binding.metadata_player_resolver"
        ):
            raise ChronicleExternalReconstructionV2Error(
                "multi-instance source-binding locations are unsupported"
            )
        top_provenance = _mapping(
            manifest.get("instance_provenance"),
            label="manifest.instance_provenance",
        )
        if (
            top_provenance.get("scope") != "per_instance"
            or top_provenance.get("location")
            != "instances[].instance_provenance"
            or top_provenance.get("instance_count") != len(raw_instances)
        ):
            raise ChronicleExternalReconstructionV2Error(
                "multi-instance provenance descriptor is inconsistent"
            )
        instance_ids: list[str] = []
        partition_paths: set[str] = set()
        all_entries: list[Mapping[str, Any]] = []
        for index, raw_instance in enumerate(raw_instances):
            instance = _mapping(raw_instance, label=f"instances[{index}]")
            if instance.get("status") != STATUS:
                raise ChronicleExternalReconstructionV2Error(
                    "reconstruction instance has an unsupported status"
                )
            instance_id = _safe_component(
                instance.get("instance_id"), label="instance.instance_id"
            )
            if instance_id in instance_ids:
                raise ChronicleExternalReconstructionV2Error(
                    "reconstruction manifest contains duplicate instance ids"
                )
            instance_ids.append(instance_id)
            local_binding = _mapping(
                instance.get("source_binding"), label="instance.source_binding"
            )
            if set(local_binding) != {
                "normalized_partition",
                "metadata_player_resolver",
            }:
                raise ChronicleExternalReconstructionV2Error(
                    "multi-instance local source binding has unsupported fields"
                )
            partition = deepcopy(
                dict(
                    _mapping(
                        local_binding.get("normalized_partition"),
                        label="normalized_partition",
                    )
                )
            )
            partition_path = _text(partition.get("path"), label="partition.path")
            partition_key = partition_path.casefold()
            if partition_key in partition_paths:
                raise ChronicleExternalReconstructionV2Error(
                    "reconstruction instances share a normalized partition"
                )
            partition_paths.add(partition_key)
            resolver = deepcopy(
                dict(
                    _mapping(
                        local_binding.get("metadata_player_resolver"),
                        label="metadata_player_resolver",
                    )
                )
            )
            full_binding = {
                **common_parts,
                "normalized_partition": partition,
                "metadata_player_resolver": resolver,
            }
            provenance = _mapping(
                instance.get("instance_provenance"),
                label="instance.instance_provenance",
            )
            raw_entries = _array(
                instance.get("encounters"), label="instance.encounters"
            )
            validated = _validate_encounter_entries(
                raw_entries,
                output=output,
                expected_instance_id=instance_id,
                expected_path_prefix=("instances", instance_id, "encounters"),
                expected_source_binding=full_binding,
                expected_instance_provenance=provenance,
                verify_artifacts=verify_artifacts,
                seen_artifact_paths=seen_artifact_paths,
            )
            if instance.get("summary") != _manifest_summary(validated):
                raise ChronicleExternalReconstructionV2Error(
                    "reconstruction instance summary differs from encounters"
                )
            all_entries.extend(validated)
        if instance_ids != sorted(instance_ids):
            raise ChronicleExternalReconstructionV2Error(
                "reconstruction instances are not in deterministic order"
            )
        if len({value.casefold() for value in instance_ids}) != len(instance_ids):
            raise ChronicleExternalReconstructionV2Error(
                "reconstruction instance ids collide case-insensitively"
            )
        if manifest.get("summary") != _manifest_summary(
            all_entries, instance_count=len(raw_instances)
        ):
            raise ChronicleExternalReconstructionV2Error(
                "reconstruction manifest summary differs from instances"
            )
    return manifest, resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build versioned Chronicle External API encounter reconstruction V2"
    )
    parser.add_argument("--admission-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--combat-gap-ms", type=int, default=DEFAULT_COMBAT_GAP_MS)
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_external_encounter_reconstruction(
        admission_manifest_path=args.admission_manifest,
        output_directory=args.output_dir,
        combat_gap_ms=args.combat_gap_ms,
        workers=args.workers,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ARTIFACT_SCHEMA",
    "ChronicleExternalReconstructionV2Error",
    "DEFAULT_COMBAT_GAP_MS",
    "DEFAULT_OUTPUT_DIRECTORY",
    "IMPLEMENTATION_REVISION",
    "LANE_FRIENDLY_PLAYER",
    "LANE_HOSTILE_CREATURE",
    "LANE_HOSTILE_OBJECT",
    "LANE_HOSTILE_PLAYER",
    "LANE_UNKNOWN",
    "SCHEMA",
    "STATUS",
    "build_external_encounter_reconstruction",
    "load_external_reconstruction_manifest",
    "main",
]
