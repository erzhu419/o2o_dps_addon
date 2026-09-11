"""Compile Chronicle team timelines into non-voting training/replay capsules.

This module is the narrow layer between ``chronicle_team_wave_timeline/v1``
and a future dynamic simulator binding.  It preserves an exact observed event
lane for descriptive replay, and independently emits player-wave event
transitions whose ``state_before`` contains only strict-prefix information.

Nothing produced here is a learned policy or a comparison-ready simulator
environment.  Historical traces are permanently marked
``DESCRIPTIVE_NONVOTING``.  Target death clocks and final wave totals are kept
only in separately labelled descriptive outcome records and are never copied
into a transition feature.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from .chronicle_team_wave_timeline_v1 import (
    IMPLEMENTATION_REVISION as TIMELINE_IMPLEMENTATION_REVISION,
)


JSONMap = dict[str, Any]

SCHEMA = "chronicle_team_wave_model/v1"
SCHEMA_VERSION = 1
IMPLEMENTATION_REVISION = "v1.3_prefix_causal_boundary_nontraining_dmg_lane"
TIMELINE_SCHEMA = "chronicle_team_wave_timeline/v1"
TIMELINE_MANIFEST_KIND = "chronicle_team_wave_timeline_manifest"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMELINE_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_team_wave_timeline"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_team_wave_model"
    / "v1"
)

ATTRIBUTION_KINDS = ("DIRECT_PLAYER", "OWNED_ENTITY", "UNATTRIBUTED")
FOCAL_REMOVAL_KINDS = frozenset(("DIRECT_PLAYER", "OWNED_ENTITY"))
DEFAULT_TRAINING_CONTAMINATION_STATUSES = frozenset(
    ("POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH")
)
NONVOTING_CONTAMINATION_STATUSES = frozenset(
    (
        "SUSPECT_36YD_RANGE_BUG",
        "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
        "UNKNOWN_NONVOTING",
    )
)
ACTION_EVENT_TYPES = frozenset(("START", "CAST", "FAIL"))
DAMAGE_EVENT_TYPES = frozenset(("DMG", "DEAD"))
KNOWN_EVENT_TYPES = frozenset(("START", "CAST", "FAIL", "DMG", "DEAD", "HEAL"))
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_BANNED_PREFIX_FEATURE_KEYS = frozenset(
    (
        "death_clock",
        "target_summaries",
        "wave_summary",
        "team_damage",
        "final_totals",
        "final_target_count",
        "future_events",
        "wave_end_ms",
        "wave_duration_ms",
        "remaining_wave_ms",
    )
)


class ChronicleTeamWaveModelError(RuntimeError):
    """A timeline or derived team-model capsule violates the v1 contract."""


@dataclass(frozen=True)
class WaveRecords:
    header: JSONMap
    events: tuple[JSONMap, ...]
    target_summaries: tuple[JSONMap, ...]
    wave_summary: JSONMap


@dataclass(frozen=True)
class PartitionBuild:
    temporary_path: Path
    final_path: Path
    manifest_entry: JSONMap
    component_nodes: tuple[tuple[str, str], ...]
    component_edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ChronicleTeamWaveModelResult:
    manifest: Path
    content_addressed_manifest: Path
    partitions: tuple[Path, ...]
    instance_count: int
    wave_count: int
    player_wave_episode_count: int

    def as_dict(self) -> JSONMap:
        return {
            "status": "ok",
            "schema": SCHEMA,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "partitions": [str(path) for path in self.partitions],
            "instance_count": self.instance_count,
            "wave_count": self.wave_count,
            "player_wave_episode_count": self.player_wave_episode_count,
            "voting_ready": False,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ChronicleTeamWaveModelError(
            f"value is not strict canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ChronicleTeamWaveModelError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleTeamWaveModelError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleTeamWaveModelError(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleTeamWaveModelError(f"{label} must be nonempty text")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = value.strip()
    return rendered or None


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleTeamWaveModelError(f"{label} must be an integer")
    return value


def _nonnegative_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChronicleTeamWaveModelError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ChronicleTeamWaveModelError(
            f"{label} must be finite and nonnegative"
        )
    return int(parsed) if parsed.is_integer() else parsed


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ChronicleTeamWaveModelError(f"{label} must be lowercase SHA-256")
    return value


def _guid_key(value: Any) -> str:
    rendered = _optional_text(value)
    return rendered.casefold() if rendered else ""


def _safe_component(value: str) -> str:
    rendered = _SAFE_COMPONENT.sub("_", value).strip("._")
    return rendered or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleTeamWaveModelError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ChronicleTeamWaveModelError(f"{label} must contain one JSON object")
    return value


def _verify_content_address(document: Mapping[str, Any], label: str) -> str:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    declared = _sha(address.get("sha256"), f"{label}.content_address.sha256")
    core = {key: value for key, value in document.items() if key != "content_address"}
    actual = _sha256_json(core)
    if declared != actual:
        raise ChronicleTeamWaveModelError(
            f"{label} content address mismatch: expected {declared}, got {actual}"
        )
    return actual


def _resolve_partition(manifest_path: Path, raw: Any, label: str) -> Path:
    relative = Path(_text(raw, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleTeamWaveModelError(f"{label} must be a safe relative path")
    base = manifest_path.parent.resolve()
    try:
        resolved = (base / relative).resolve(strict=True)
    except OSError as error:
        raise ChronicleTeamWaveModelError(f"cannot resolve {label}: {error}") from error
    if not resolved.is_relative_to(base) or not resolved.is_file() or resolved.is_symlink():
        raise ChronicleTeamWaveModelError(f"{label} escapes or is not a regular file")
    return resolved


def _portable_input_path(path: Path) -> str:
    try:
        return "$PROJECT_ROOT/" + path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return "$EXTERNAL/" + path.name


def _wave_key(value: Mapping[str, Any], label: str) -> tuple[str, str, str, int]:
    wave = _mapping(value.get("wave"), f"{label}.wave")
    return (
        _text(wave.get("instance_id"), f"{label}.wave.instance_id"),
        _text(wave.get("encounter_id"), f"{label}.wave.encounter_id"),
        _text(wave.get("wave_id"), f"{label}.wave.wave_id"),
        _integer(wave.get("wave_ordinal"), f"{label}.wave.wave_ordinal"),
    )


def _iter_partition_rows(
    path: Path,
) -> tuple[Iterator[tuple[int, JSONMap]], Any]:
    digest = hashlib.sha256()

    def rows() -> Iterator[tuple[int, JSONMap]]:
        try:
            with gzip.open(path, "rb") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    digest.update(raw_line)
                    if not raw_line.strip():
                        continue
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ChronicleTeamWaveModelError(
                            f"invalid timeline JSON {path}:{line_number}: {error}"
                        ) from error
                    if not isinstance(value, dict):
                        raise ChronicleTeamWaveModelError(
                            f"timeline row is not an object {path}:{line_number}"
                        )
                    if value.get("schema") != TIMELINE_SCHEMA:
                        raise ChronicleTeamWaveModelError(
                            f"timeline row schema mismatch {path}:{line_number}"
                        )
                    if raw_line != _canonical_bytes(value) + b"\n":
                        raise ChronicleTeamWaveModelError(
                            f"timeline row is not canonical JSONL {path}:{line_number}"
                        )
                    yield line_number, value
        except ChronicleTeamWaveModelError:
            raise
        except OSError as error:
            raise ChronicleTeamWaveModelError(
                f"cannot stream timeline partition {path}: {error}"
            ) from error

    return rows(), digest


def _iter_waves(path: Path) -> tuple[Iterator[WaveRecords], Any, Counter[str]]:
    rows, digest = _iter_partition_rows(path)
    counts: Counter[str] = Counter()

    def waves() -> Iterator[WaveRecords]:
        header: JSONMap | None = None
        events: list[JSONMap] = []
        targets: list[JSONMap] = []
        stage = "EXPECT_HEADER"
        current_key: tuple[str, str, str, int] | None = None
        last_order: tuple[int, int, int] | None = None
        for line_number, row in rows:
            record_type = _text(
                row.get("record_type"), f"timeline row {line_number}.record_type"
            )
            counts[record_type] += 1
            if record_type == "wave_header":
                if stage != "EXPECT_HEADER":
                    raise ChronicleTeamWaveModelError(
                        f"wave_header before prior wave was sealed at {path}:{line_number}"
                    )
                header = row
                current_key = _wave_key(row, f"timeline row {line_number}")
                events = []
                targets = []
                last_order = None
                stage = "EVENTS"
                continue
            if header is None or current_key is None:
                raise ChronicleTeamWaveModelError(
                    f"{record_type} appears before wave_header at {path}:{line_number}"
                )
            if _wave_key(row, f"timeline row {line_number}") != current_key:
                raise ChronicleTeamWaveModelError(
                    f"wave identity changed before summary at {path}:{line_number}"
                )
            if record_type == "event":
                if stage != "EVENTS":
                    raise ChronicleTeamWaveModelError(
                        f"event appears after target summaries at {path}:{line_number}"
                    )
                order_raw = _array(row.get("order_key"), "event.order_key")
                if len(order_raw) != 3:
                    raise ChronicleTeamWaveModelError("event.order_key must have three integers")
                order = tuple(
                    _integer(value, f"event.order_key[{index}]")
                    for index, value in enumerate(order_raw)
                )
                if last_order is not None and order <= last_order:
                    raise ChronicleTeamWaveModelError(
                        f"timeline event order is not strict at {path}:{line_number}"
                    )
                last_order = order
                event_type = _text(row.get("event_type"), "event.event_type")
                if event_type not in KNOWN_EVENT_TYPES:
                    raise ChronicleTeamWaveModelError(
                        f"unsupported timeline event type {event_type!r}"
                    )
                _validate_attribution(row, f"timeline event {line_number}")
                events.append(row)
                continue
            if record_type == "target_summary":
                if stage not in ("EVENTS", "TARGETS"):
                    raise ChronicleTeamWaveModelError(
                        f"target_summary has invalid position at {path}:{line_number}"
                    )
                stage = "TARGETS"
                targets.append(row)
                continue
            if record_type == "wave_summary":
                if stage not in ("EVENTS", "TARGETS"):
                    raise ChronicleTeamWaveModelError(
                        f"wave_summary has invalid position at {path}:{line_number}"
                    )
                yield WaveRecords(header, tuple(events), tuple(targets), row)
                header = None
                current_key = None
                events = []
                targets = []
                last_order = None
                stage = "EXPECT_HEADER"
                continue
            raise ChronicleTeamWaveModelError(
                f"unsupported timeline record_type {record_type!r} at {path}:{line_number}"
            )
        if header is not None:
            raise ChronicleTeamWaveModelError(
                f"timeline partition ended before wave_summary: {path}"
            )

    return waves(), digest, counts


def _validate_attribution(event: Mapping[str, Any], label: str) -> JSONMap:
    attribution = _mapping(event.get("attribution"), f"{label}.attribution")
    kind = _text(attribution.get("kind"), f"{label}.attribution.kind")
    if kind not in ATTRIBUTION_KINDS:
        raise ChronicleTeamWaveModelError(f"{label} has unsupported attribution {kind!r}")
    player_guid = _optional_text(attribution.get("player_guid"))
    if kind in FOCAL_REMOVAL_KINDS and player_guid is None:
        raise ChronicleTeamWaveModelError(
            f"{label} {kind} attribution lacks its explicit owner player GUID"
        )
    if kind == "UNATTRIBUTED" and player_guid is not None:
        raise ChronicleTeamWaveModelError(
            f"{label} UNATTRIBUTED event must not guess a player GUID"
        )
    return dict(attribution)


def _compact_event(event: Mapping[str, Any]) -> JSONMap:
    attribution = _validate_attribution(event, "event")
    order = _array(event.get("order_key"), "event.order_key")
    if len(order) != 3:
        raise ChronicleTeamWaveModelError("event.order_key must have three integers")
    amount = event.get("amount")
    if amount is not None:
        amount = _nonnegative_number(amount, "event.amount")
    damage_accounting = event.get("damage_accounting")
    if damage_accounting is not None and not isinstance(damage_accounting, Mapping):
        raise ChronicleTeamWaveModelError("event.damage_accounting must be an object")
    return {
        "order_key": [
            _integer(value, f"event.order_key[{index}]")
            for index, value in enumerate(order)
        ],
        "offset_ms": _integer(event.get("offset_ms"), "event.offset_ms"),
        "wave_offset_ms": _integer(
            event.get("wave_offset_ms"), "event.wave_offset_ms"
        ),
        "event_type": _text(event.get("event_type"), "event.event_type"),
        "source_event_type": _text(
            event.get("source_event_type"), "event.source_event_type"
        ),
        "source": deepcopy(dict(_mapping(event.get("source"), "event.source"))),
        "target": deepcopy(dict(_mapping(event.get("target"), "event.target"))),
        "spell": deepcopy(dict(_mapping(event.get("spell"), "event.spell"))),
        "amount": amount,
        "amount_status": _text(event.get("amount_status"), "event.amount_status"),
        "outcome": event.get("outcome"),
        "attribution": attribution,
        "damage_accounting": (
            deepcopy(dict(damage_accounting))
            if isinstance(damage_accounting, Mapping)
            else None
        ),
    }


def _is_damage_event(event: Mapping[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "")
    # Match the timeline producer: every DMG row is damage-bearing, including
    # an unparsed amount; DEAD is damage-bearing only with an explicit value.
    return event_type == "DMG" or (
        event_type == "DEAD" and event.get("amount") is not None
    )


def _is_canonical_prefix_damage_event(event: Mapping[str, Any]) -> bool:
    """Return the prefix-causal, non-double-count damage lane.

    Numeric DEAD rows are preserved as terminal observations, but their value
    can duplicate a preceding DMG row.  Learned prefix state and LOO background
    therefore consume numeric DMG rows only.  This choice depends only on the
    current event type, never a future death clock or frozen wave total.
    """

    return str(event.get("event_type") or "") == "DMG"


def _damage_document(
    values: Mapping[str, int | float],
    event_counts: Mapping[str, int],
    unparsed_counts: Mapping[str, int],
) -> JSONMap:
    by_kind = {
        kind: {
            "value": _render_number(float(values.get(kind, 0))),
            "event_count": int(event_counts.get(kind, 0)),
            "unparsed_event_count": int(unparsed_counts.get(kind, 0)),
        }
        for kind in ATTRIBUTION_KINDS
    }
    return {
        "value": _render_number(
            float(sum(float(values.get(kind, 0)) for kind in ATTRIBUTION_KINDS))
        ),
        "event_count": int(
            sum(int(event_counts.get(kind, 0)) for kind in ATTRIBUTION_KINDS)
        ),
        "unparsed_event_count": int(
            sum(int(unparsed_counts.get(kind, 0)) for kind in ATTRIBUTION_KINDS)
        ),
        "by_attribution": by_kind,
    }


def build_leave_one_player_out_background(
    events: Sequence[Mapping[str, Any]], focal_player_guid: str
) -> JSONMap:
    """Materialize one descriptive background view for a focal player.

    Every direct or explicitly owned event attributed to the focal player is
    removed.  Unattributed events are retained in their own uncertainty branch
    and are never guessed to belong to the focal player or a teammate.
    """

    focal = _text(focal_player_guid, "focal_player_guid")
    focal_key = _guid_key(focal)
    included: list[JSONMap] = []
    excluded: list[JSONMap] = []
    included_damage: Counter[str] = Counter()
    excluded_damage: Counter[str] = Counter()
    included_damage_events: Counter[str] = Counter()
    excluded_damage_events: Counter[str] = Counter()
    included_unparsed_events: Counter[str] = Counter()
    excluded_unparsed_events: Counter[str] = Counter()
    for index, raw_event in enumerate(events):
        event = _compact_event(_mapping(raw_event, f"events[{index}]"))
        attribution = event["attribution"]
        kind = str(attribution["kind"])
        owner_key = _guid_key(attribution.get("player_guid"))
        remove = kind in FOCAL_REMOVAL_KINDS and owner_key == focal_key
        destination = excluded if remove else included
        destination.append(event)
        if _is_canonical_prefix_damage_event(event):
            values = excluded_damage if remove else included_damage
            event_counts = excluded_damage_events if remove else included_damage_events
            unparsed_counts = (
                excluded_unparsed_events if remove else included_unparsed_events
            )
            event_counts[kind] += 1
            if event.get("amount") is None:
                unparsed_counts[kind] += 1
            else:
                values[kind] += float(event["amount"])

    return {
        "schema": SCHEMA,
        "kind": "leave_one_player_out_background_projection",
        "status": "DESCRIPTIVE_NONVOTING",
        "voting_eligible": False,
        "focal_player_guid": focal,
        "filter_contract": {
            "excluded_attribution_kinds": sorted(FOCAL_REMOVAL_KINDS),
            "owner_match": "case-insensitive exact player GUID",
            "unattributed_events_retained": True,
            "name_based_owner_inference": False,
            "damage_value_lane": "FULL_NORMALIZED_WAVE_NUMERIC_DMG_ONLY",
            "numeric_dead_value_excluded_to_prevent_double_count": True,
        },
        "included_events": included,
        "excluded_focal_events": excluded,
        "included_damage": _damage_document(
            included_damage, included_damage_events, included_unparsed_events
        ),
        "excluded_focal_damage": _damage_document(
            excluded_damage, excluded_damage_events, excluded_unparsed_events
        ),
        "unattributed_branch": {
            "status": "EXPLICIT_UNKNOWN_NONVOTING",
            "event_count": sum(
                1
                for event in included
                if event["attribution"]["kind"] == "UNATTRIBUTED"
            ),
            "damage": _damage_document(
                Counter(
                    {
                        "UNATTRIBUTED": included_damage["UNATTRIBUTED"],
                    }
                ),
                Counter(
                    {
                        "UNATTRIBUTED": included_damage_events["UNATTRIBUTED"],
                    }
                ),
                Counter(
                    {
                        "UNATTRIBUTED": included_unparsed_events["UNATTRIBUTED"],
                    }
                ),
            ),
        },
    }


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _specialization(roster_row: Mapping[str, Any], hero_class: str) -> JSONMap:
    raw_specs = roster_row.get("leaderboard_specs_recorded_separately")
    if not isinstance(raw_specs, list):
        raw_specs = []
    by_key: dict[str, str] = {}
    for raw in raw_specs:
        rendered = _optional_text(raw)
        if rendered is not None:
            by_key.setdefault(rendered.casefold(), rendered)
    values = [by_key[key] for key in sorted(by_key)]
    if len(values) == 1:
        status = "OBSERVED_SINGLE"
        normalized = values[0].casefold()
        if hero_class == "WARRIOR" and normalized == "fury":
            partition = "WARRIOR_FURY"
        elif hero_class == "WARRIOR" and normalized == "arms":
            partition = "WARRIOR_ARMS"
        else:
            partition = f"{hero_class}_{re.sub(r'[^A-Z0-9]+', '_', values[0].upper()).strip('_')}"
    elif values:
        status = "CONFLICTING_MULTIPLE_OBSERVED_SPECS"
        partition = f"{hero_class}_CONFLICTING_NONVOTING"
    else:
        status = "UNKNOWN_EXPLICIT"
        partition = f"{hero_class}_UNKNOWN"
    return {
        "status": status,
        "observed_values": values,
        "partition_key": partition,
        "fury_arms_merged": False,
        "inference_from_talents_used": False,
    }


def _node_id(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def _component_membership(
    *, instance_id: str, guild_names: Iterable[str], player_guid: str | None
) -> tuple[JSONMap, dict[str, str], set[tuple[str, str]]]:
    nodes: dict[str, str] = {}
    edges: set[tuple[str, str]] = set()
    instance_node = _node_id("instance", instance_id)
    nodes[instance_node] = "instance"
    observed_guilds = sorted(
        {
            rendered.casefold(): rendered
            for value in guild_names
            if (rendered := _optional_text(value)) is not None
        }.values(),
        key=str.casefold,
    )
    guild_nodes = [_node_id("guild", value) for value in observed_guilds]
    player_node = _node_id("player", player_guid) if player_guid else None
    for guild_node in guild_nodes:
        nodes[guild_node] = "guild"
        edges.add(tuple(sorted((instance_node, guild_node))))
    if player_node:
        nodes[player_node] = "player"
        edges.add(tuple(sorted((instance_node, player_node))))
    membership = {
        "instance_node_id": instance_node,
        "guild_node_ids": guild_nodes,
        "player_node_id": player_node,
        "component_edges": [list(edge) for edge in sorted(edges)],
        "required_split_unit": "connected component of instance, guild, and player nodes",
        "row_random_split_allowed": False,
    }
    return membership, nodes, edges


def _roster_index(header: Mapping[str, Any]) -> dict[str, JSONMap]:
    result: dict[str, JSONMap] = {}
    for index, raw in enumerate(_array(header.get("roster"), "wave_header.roster")):
        row = _mapping(raw, f"wave_header.roster[{index}]")
        guid = _optional_text(row.get("player_guid"))
        if guid is None:
            # Retain no anonymous pseudo-player: those events remain explicitly
            # unattributed and cannot silently acquire a player identity.
            continue
        key = _guid_key(guid)
        if key in result:
            raise ChronicleTeamWaveModelError(f"duplicate roster player GUID {guid}")
        result[key] = deepcopy(dict(row))
    return result


def _guild_identities(
    header: Mapping[str, Any], roster_row: Mapping[str, Any]
) -> tuple[tuple[str, ...], str]:
    contamination = _mapping(header.get("contamination"), "wave_header.contamination")
    values_by_key = {
        value.casefold(): value
        for value in (
            _optional_text(contamination.get("guild_name")),
            _optional_text(roster_row.get("guild_name")),
        )
        if value is not None
    }
    values = tuple(values_by_key[key] for key in sorted(values_by_key))
    if len(values) == 1:
        return values, "OBSERVED_UNIQUE"
    if not values:
        return (), "UNKNOWN_EXPLICIT"
    # The raid-level guild and a participant's own guild can legitimately
    # differ.  Preserve and connect both identities instead of discarding the
    # edge or pretending that one is authoritative.
    return values, "OBSERVED_MULTIPLE_EXPLICIT"


def _episode_eligibility(
    *,
    contamination_status: str,
    player_guid: str | None,
    hero_class: str,
    guild_status: str,
    specialization: Mapping[str, Any],
) -> JSONMap:
    reasons: list[str] = []
    contamination_allowed = (
        contamination_status in DEFAULT_TRAINING_CONTAMINATION_STATUSES
    )
    if not contamination_allowed:
        reasons.append(f"CONTAMINATION_{contamination_status}")
    if player_guid is None:
        reasons.append("PLAYER_OWNER_UNKNOWN")
    if hero_class == "UNKNOWN":
        reasons.append("HERO_CLASS_UNKNOWN")
    team_eligible = not reasons
    fury_eligible = (
        team_eligible
        and hero_class == "WARRIOR"
        and specialization.get("partition_key") == "WARRIOR_FURY"
        and specialization.get("status") == "OBSERVED_SINGLE"
    )
    fury_reasons = list(reasons)
    if hero_class != "WARRIOR":
        fury_reasons.append("NOT_WARRIOR")
    if specialization.get("partition_key") != "WARRIOR_FURY":
        fury_reasons.append("NOT_OBSERVED_FURY")
    return {
        "contamination_status": contamination_status,
        "default_allowed_contamination_statuses": sorted(
            DEFAULT_TRAINING_CONTAMINATION_STATUSES
        ),
        "team_behavior_training_eligible": team_eligible,
        "historical_fury_policy_training_eligible": fury_eligible,
        "historical_arms_policy_voting_eligible": False,
        "policy_lane_role": (
            "FUTURE_LEARNED_FURY_BEHAVIOR_POLICY_TRAINING_INPUT_NONVOTING"
            if specialization.get("partition_key") == "WARRIOR_FURY"
            else (
                "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING"
                if specialization.get("partition_key") == "WARRIOR_ARMS"
                else "TEAM_BACKGROUND_TRAINING_INPUT_NONVOTING"
            )
        ),
        "team_behavior_blockers": sorted(set(reasons)),
        "historical_fury_policy_blockers": sorted(set(fury_reasons)),
        "exact_trace_voting_eligible": False,
    }


class _PrefixState:
    def __init__(self, target_guids: Iterable[str]) -> None:
        self.target_guids = {_guid_key(value): value for value in target_guids}
        self.seen_targets: set[str] = set()
        self.dead_targets: set[str] = set()
        self.event_count = 0
        self.damage: Counter[str] = Counter()
        self.target_damage: Counter[str] = Counter()
        self.target_first_seen_ms: dict[str, int] = {}
        self.target_last_seen_ms: dict[str, int] = {}
        self.player_damage: dict[str, Counter[str]] = defaultdict(Counter)
        self.actor_recent: dict[str, list[JSONMap]] = defaultdict(list)

    def snapshot(
        self,
        *,
        order_key: Sequence[int],
        wave_offset_ms: int,
        actor_key: str,
    ) -> JSONMap:
        alive = self.seen_targets - self.dead_targets
        elapsed_seconds = max(wave_offset_ms, 0) / 1000.0
        total_damage = float(sum(self.damage.values()))
        focal_damage = float(sum(self.player_damage.get(actor_key, {}).values()))
        explicit_player_damage = float(
            sum(sum(values.values()) for values in self.player_damage.values())
        )
        other_player_damage = explicit_player_damage - focal_damage
        unattributed_damage = float(self.damage["UNATTRIBUTED"])
        background_damage = other_player_damage + unattributed_damage
        background = None
        if actor_key != "__unattributed__":
            background = {
                "filter_contract": (
                    "exclude prefix DIRECT_PLAYER and OWNED_ENTITY damage whose "
                    "explicit owner GUID equals the focal player"
                ),
                "focal_player_guid_key": actor_key,
                "excluded_focal_damage": _render_number(focal_damage),
                "included_explicit_other_player_damage": _render_number(
                    other_player_damage
                ),
                "included_unattributed_damage": _render_number(
                    unattributed_damage
                ),
                "included_background_damage": _render_number(background_damage),
                "prefix_elapsed_average_background_dps": (
                    background_damage / elapsed_seconds
                    if elapsed_seconds > 0
                    else None
                ),
                "unattributed_retained_as_explicit_unknown": True,
            }
        result = {
            "cutoff_semantics": "strictly before current event order_key",
            "cutoff_exclusive_order_key": list(order_key),
            "wave_elapsed_ms": wave_offset_ms,
            "prefix_event_count": self.event_count,
            "seen_target_guids": [self.target_guids[key] for key in sorted(self.seen_targets)],
            "observed_dead_target_guids": [
                self.target_guids[key] for key in sorted(self.dead_targets)
            ],
            "alive_seen_target_guids": [self.target_guids[key] for key in sorted(alive)],
            "prefix_observed_damage_by_attribution": {
                kind: _render_number(float(self.damage[kind]))
                for kind in ATTRIBUTION_KINDS
            },
            "prefix_observed_damage_total": _render_number(total_damage),
            "prefix_elapsed_average_observed_dps": (
                total_damage / elapsed_seconds if elapsed_seconds > 0 else None
            ),
            "leave_one_player_out_background_before": background,
            "observed_target_prefix": [
                {
                    "target_guid": self.target_guids[key],
                    "first_seen_wave_offset_ms": self.target_first_seen_ms[key],
                    "last_seen_wave_offset_ms": self.target_last_seen_ms[key],
                    "prefix_observed_damage": _render_number(
                        float(self.target_damage[key])
                    ),
                    "observed_dead": key in self.dead_targets,
                    "availability_semantics": (
                        "observed active/dead proxy; not inferred attackability"
                    ),
                }
                for key in sorted(self.seen_targets)
            ],
            "actor_last_observed_target_guid": (
                self.actor_recent[actor_key][-1]["target"].get("guid")
                if self.actor_recent.get(actor_key)
                else None
            ),
            "actor_recent_observed_events": deepcopy(self.actor_recent.get(actor_key, [])[-8:]),
        }
        _assert_prefix_feature_contract(result)
        return result

    def observe(self, event: Mapping[str, Any], actor_key: str) -> None:
        target = _mapping(event.get("target"), "event.target")
        target_key = _guid_key(target.get("guid"))
        if target_key in self.target_guids:
            self.seen_targets.add(target_key)
            wave_offset_ms = int(event["wave_offset_ms"])
            self.target_first_seen_ms.setdefault(target_key, wave_offset_ms)
            self.target_last_seen_ms[target_key] = wave_offset_ms
            if event.get("event_type") == "DEAD":
                self.dead_targets.add(target_key)
        attribution = _mapping(event.get("attribution"), "event.attribution")
        kind = str(attribution.get("kind"))
        if _is_canonical_prefix_damage_event(event) and event.get("amount") is not None:
            amount = float(event["amount"])
            self.damage[kind] += amount
            if target_key in self.target_guids:
                self.target_damage[target_key] += amount
            if actor_key != "__unattributed__":
                self.player_damage[actor_key][kind] += amount
        compact_history = {
            "order_key": deepcopy(event["order_key"]),
            "event_type": event["event_type"],
            "spell": deepcopy(event["spell"]),
            "target": deepcopy(event["target"]),
            "outcome": event.get("outcome"),
        }
        self.actor_recent[actor_key].append(compact_history)
        self.event_count += 1


def _assert_prefix_feature_contract(value: Any, path: str = "state_before") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered = str(key)
            if rendered in _BANNED_PREFIX_FEATURE_KEYS:
                raise ChronicleTeamWaveModelError(
                    f"forbidden future/outcome key in prefix feature {path}.{rendered}"
                )
            _assert_prefix_feature_contract(child, f"{path}.{rendered}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_prefix_feature_contract(child, f"{path}[{index}]")


def _learning_role(event_type: str) -> str:
    if event_type == "START":
        return "ACTION_START_DECISION_LABEL"
    if event_type == "CAST":
        return "ACTION_CAST_OBSERVATION_OR_INSTANT_ACTION_CANDIDATE"
    if event_type == "FAIL":
        return "ACTION_FAILURE_OUTCOME_OBSERVATION"
    if event_type in DAMAGE_EVENT_TYPES:
        return "DAMAGE_OR_DEATH_OUTCOME_OBSERVATION"
    if event_type == "HEAL":
        return "HEALING_OUTCOME_OBSERVATION"
    raise ChronicleTeamWaveModelError(f"unsupported transition event type {event_type!r}")


def _transition(
    event: Mapping[str, Any], state_before: Mapping[str, Any], actor_kind: str
) -> JSONMap:
    event_type = str(event["event_type"])
    action_observation = None
    if event_type in ACTION_EVENT_TYPES:
        action_observation = {
            "event_type": event_type,
            "spell": deepcopy(event["spell"]),
            "target": deepcopy(event["target"]),
            "instant_cast_note": (
                "CAST can be the only observed action marker for an instant action; "
                "a future learner must prefix-pair START/CAST without future lookahead"
            ),
        }
    core = {
        "state_before": deepcopy(dict(state_before)),
        "observed_event": deepcopy(dict(event)),
        "action_observation": action_observation,
        "learning_role": _learning_role(event_type),
        "actor_kind": actor_kind,
        "feature_cutoff_is_strict_prefix": True,
        "future_outcomes_in_state_before": False,
    }
    return {**core, "transition_sha256": _sha256_json(core)}


def _descriptive_projection_from_aggregates(
    *,
    focal_player_guid: str,
    total_event_count: int,
    owner_event_counts: Mapping[str, int],
    total_damage: Mapping[str, int | float],
    total_damage_event_counts: Mapping[str, int],
    total_unparsed_damage_counts: Mapping[str, int],
    owner_damage: Mapping[str, Mapping[str, int | float]],
    owner_damage_event_counts: Mapping[str, Mapping[str, int]],
    owner_unparsed_damage_counts: Mapping[str, Mapping[str, int]],
    unattributed_event_count: int,
) -> JSONMap:
    focal_key = _guid_key(focal_player_guid)
    excluded_values = owner_damage.get(focal_key, {})
    excluded_events = owner_damage_event_counts.get(focal_key, {})
    excluded_unparsed = owner_unparsed_damage_counts.get(focal_key, {})
    included_values = {
        kind: float(total_damage.get(kind, 0))
        - float(excluded_values.get(kind, 0))
        for kind in ATTRIBUTION_KINDS
    }
    included_events = {
        kind: int(total_damage_event_counts.get(kind, 0))
        - int(excluded_events.get(kind, 0))
        for kind in ATTRIBUTION_KINDS
    }
    included_unparsed = {
        kind: int(total_unparsed_damage_counts.get(kind, 0))
        - int(excluded_unparsed.get(kind, 0))
        for kind in ATTRIBUTION_KINDS
    }
    residuals = (
        *included_values.values(),
        *included_events.values(),
        *included_unparsed.values(),
    )
    if any(value < 0 for value in residuals):
        raise ChronicleTeamWaveModelError("leave-one-player-out aggregate underflow")
    return {
        "status": "DESCRIPTIVE_NONVOTING",
        "voting_eligible": False,
        "focal_player_guid": focal_player_guid,
        "filter_contract": {
            "excluded_attribution_kinds": sorted(FOCAL_REMOVAL_KINDS),
            "owner_match": "case-insensitive exact player GUID",
            "unattributed_events_retained": True,
            "name_based_owner_inference": False,
        },
        "included_event_count": total_event_count
        - int(owner_event_counts.get(focal_key, 0)),
        "excluded_focal_event_count": int(owner_event_counts.get(focal_key, 0)),
        "included_damage": _damage_document(
            included_values, included_events, included_unparsed
        ),
        "excluded_focal_damage": _damage_document(
            excluded_values, excluded_events, excluded_unparsed
        ),
        "unattributed_branch": {
            "status": "EXPLICIT_UNKNOWN_NONVOTING",
            "event_count": unattributed_event_count,
            "damage": _damage_document(
                {"UNATTRIBUTED": total_damage.get("UNATTRIBUTED", 0)},
                {
                    "UNATTRIBUTED": total_damage_event_counts.get(
                        "UNATTRIBUTED", 0
                    )
                },
                {
                    "UNATTRIBUTED": total_unparsed_damage_counts.get(
                        "UNATTRIBUTED", 0
                    )
                },
            ),
        },
        "event_materialization": (
            "filter the wave's exact_trace_event records with this predicate; "
            "events are not duplicated per focal player"
        ),
    }


def _build_wave_output(wave: WaveRecords) -> tuple[list[JSONMap], JSONMap]:
    header = wave.header
    wave_identity = deepcopy(dict(_mapping(header.get("wave"), "wave_header.wave")))
    instance_id = _text(wave_identity.get("instance_id"), "wave.instance_id")
    contamination = deepcopy(
        dict(_mapping(header.get("contamination"), "wave_header.contamination"))
    )
    contamination_status = _text(
        contamination.get("status"), "wave_header.contamination.status"
    )
    if contamination_status not in (
        DEFAULT_TRAINING_CONTAMINATION_STATUSES | NONVOTING_CONTAMINATION_STATUSES
    ):
        raise ChronicleTeamWaveModelError(
            f"unsupported contamination status {contamination_status!r}"
        )
    roster = _roster_index(header)
    target_rows = _array(header.get("targets"), "wave_header.targets")
    target_guids = [
        _text(
            _mapping(value, f"wave_header.targets[{index}]").get("target_guid"),
            f"wave_header.targets[{index}].target_guid",
        )
        for index, value in enumerate(target_rows)
    ]
    raw_events = tuple(deepcopy(value) for value in wave.events)
    events = tuple(_compact_event(value) for value in raw_events)

    header_core = {
        "schema": SCHEMA,
        "record_type": "wave_model_header",
        "wave": wave_identity,
        "scenario": deepcopy(
            dict(_mapping(header.get("scenario"), "wave_header.scenario"))
        ),
        "contamination": contamination,
        "training_eligibility": {
            "default_eligible": contamination_status
            in DEFAULT_TRAINING_CONTAMINATION_STATUSES,
            "eligible_statuses": sorted(DEFAULT_TRAINING_CONTAMINATION_STATUSES),
            "nonvoting_statuses": sorted(NONVOTING_CONTAMINATION_STATUSES),
        },
        "lane_contract": {
            "exact_trace_status": "DESCRIPTIVE_NONVOTING",
            "exact_trace_can_vote": False,
            "prefix_transition_status": "TRAINING_INPUT_NOT_A_LEARNED_POLICY",
            "learned_prefix_causal_generator_present": False,
            "comparison_ready": False,
        },
        "source_hashes": deepcopy(
            dict(_mapping(header.get("source_hashes"), "wave_header.source_hashes"))
        ),
    }
    output: list[JSONMap] = [header_core]
    for event in raw_events:
        output.append(
            {
                "schema": SCHEMA,
                "record_type": "exact_trace_event",
                "wave": wave_identity,
                "lane": {
                    "status": "DESCRIPTIVE_NONVOTING",
                    "voting_eligible": False,
                    "adaptive_to_counterfactual_policy": False,
                },
                # This descriptive lane retains the complete canonical timeline
                # event.  The compact copy below is used only by model inputs.
                "event": deepcopy(event),
            }
        )

    state = _PrefixState(target_guids)
    transitions: dict[str, list[JSONMap]] = defaultdict(list)
    unattributed_transitions: list[JSONMap] = []
    player_attribution_counts: dict[str, Counter[str]] = defaultdict(Counter)
    player_attribution_damage: dict[str, Counter[str]] = defaultdict(Counter)
    unattributed_counts: Counter[str] = Counter()
    unattributed_damage: Counter[str] = Counter()
    owner_event_counts: Counter[str] = Counter()
    total_damage: Counter[str] = Counter()
    total_damage_event_counts: Counter[str] = Counter()
    total_unparsed_damage_counts: Counter[str] = Counter()
    owner_damage: dict[str, Counter[str]] = defaultdict(Counter)
    owner_damage_event_counts: dict[str, Counter[str]] = defaultdict(Counter)
    owner_unparsed_damage_counts: dict[str, Counter[str]] = defaultdict(Counter)
    unattributed_event_count = 0
    for event in events:
        attribution = _mapping(event.get("attribution"), "event.attribution")
        kind = str(attribution["kind"])
        owner_key = _guid_key(attribution.get("player_guid"))
        actor_key = owner_key if owner_key else "__unattributed__"
        state_before = state.snapshot(
            order_key=event["order_key"],
            wave_offset_ms=int(event["wave_offset_ms"]),
            actor_key=actor_key,
        )
        built = _transition(
            event,
            state_before,
            "PLAYER_OR_EXPLICIT_OWNER" if owner_key else "UNATTRIBUTED_UNKNOWN",
        )
        if owner_key:
            if owner_key not in roster:
                raise ChronicleTeamWaveModelError(
                    "an explicitly attributed player event is absent from the wave roster"
                )
            transitions[owner_key].append(built)
            owner_event_counts[owner_key] += 1
            player_attribution_counts[owner_key][kind] += 1
        else:
            unattributed_transitions.append(built)
            unattributed_event_count += 1
            unattributed_counts[kind] += 1
        if _is_canonical_prefix_damage_event(event):
            total_damage_event_counts[kind] += 1
            if owner_key:
                owner_damage_event_counts[owner_key][kind] += 1
            if event.get("amount") is None:
                total_unparsed_damage_counts[kind] += 1
                if owner_key:
                    owner_unparsed_damage_counts[owner_key][kind] += 1
            else:
                amount = float(event["amount"])
                total_damage[kind] += amount
                if owner_key:
                    owner_damage[owner_key][kind] += amount
                    player_attribution_damage[owner_key][kind] += amount
                else:
                    unattributed_damage[kind] += amount
        state.observe(event, actor_key)

    component_nodes: dict[str, str] = {}
    component_edges: set[tuple[str, str]] = set()
    episode_count = 0
    fury_count = 0
    arms_count = 0
    for player_key in sorted(roster):
        roster_row = roster[player_key]
        player_guid = _text(roster_row.get("player_guid"), "roster.player_guid")
        hero_class = (_optional_text(roster_row.get("hero_class")) or "UNKNOWN").upper()
        specialization = _specialization(roster_row, hero_class)
        guild_names, guild_status = _guild_identities(header, roster_row)
        membership, nodes, edges = _component_membership(
            instance_id=instance_id,
            guild_names=guild_names,
            player_guid=player_guid,
        )
        component_nodes.update(nodes)
        component_edges.update(edges)
        eligibility = _episode_eligibility(
            contamination_status=contamination_status,
            player_guid=player_guid,
            hero_class=hero_class,
            guild_status=guild_status,
            specialization=specialization,
        )
        episode_core = {
            "schema": SCHEMA,
            "record_type": "player_wave_episode",
            "wave": wave_identity,
            "episode_id": _sha256_json(
                {
                    "wave": wave_identity,
                    "player_guid": player_guid.casefold(),
                }
            ),
            "player": {
                "guid": player_guid,
                "name": _optional_text(roster_row.get("player_name")),
                "hero_class": hero_class,
                "hero_class_status": (
                    "OBSERVED" if hero_class != "UNKNOWN" else "UNKNOWN_EXPLICIT"
                ),
                "guild_names_observed": list(guild_names),
                "guild_identity_status": guild_status,
                "specialization": specialization,
                "gear": deepcopy(roster_row.get("gear")),
                "talents": deepcopy(roster_row.get("talents")),
            },
            "component_membership": membership,
            "eligibility": eligibility,
            "prefix_transitions": transitions.get(player_key, []),
            "transition_contract": {
                "state_feature_cutoff": "strictly before observed event order_key",
                "current_event_contract": (
                    "current event is a label or outcome, never part of state_before"
                ),
                "future_death_clock_or_wave_totals_in_state": False,
                "start_events_are_decision_labels": True,
                "target_availability_semantics": (
                    "prefix observed-active/dead proxy only; attackability is not guessed"
                ),
                "focal_background_is_prefix_leave_one_player_out": True,
                "damage_value_lane": "FULL_NORMALIZED_WAVE_NUMERIC_DMG_ONLY",
                "numeric_dead_value_is_terminal_observation_not_damage_feature": True,
            },
            "observed_attribution": {
                kind: {
                    "event_count": player_attribution_counts[player_key][kind],
                    "damage": _render_number(
                        float(player_attribution_damage[player_key][kind])
                    ),
                }
                for kind in FOCAL_REMOVAL_KINDS
            },
            "leave_one_player_out_background": _descriptive_projection_from_aggregates(
                focal_player_guid=player_guid,
                total_event_count=len(events),
                owner_event_counts=owner_event_counts,
                total_damage=total_damage,
                total_damage_event_counts=total_damage_event_counts,
                total_unparsed_damage_counts=total_unparsed_damage_counts,
                owner_damage=owner_damage,
                owner_damage_event_counts=owner_damage_event_counts,
                owner_unparsed_damage_counts=owner_unparsed_damage_counts,
                unattributed_event_count=unattributed_event_count,
            ),
            "exact_trace_lane": {
                "status": "DESCRIPTIVE_NONVOTING",
                "voting_eligible": False,
            },
        }
        output.append(episode_core)
        episode_count += 1
        if specialization["partition_key"] == "WARRIOR_FURY":
            fury_count += 1
        elif specialization["partition_key"] == "WARRIOR_ARMS":
            arms_count += 1

    unattributed_membership, nodes, edges = _component_membership(
        instance_id=instance_id,
        guild_names=tuple(
            value
            for value in (_optional_text(contamination.get("guild_name")),)
            if value is not None
        ),
        player_guid=None,
    )
    component_nodes.update(nodes)
    component_edges.update(edges)
    output.append(
        {
            "schema": SCHEMA,
            "record_type": "unattributed_wave_episode",
            "wave": wave_identity,
            "actor": {
                "kind": "UNATTRIBUTED_UNKNOWN",
                "player_guid": None,
                "hero_class": "UNKNOWN",
                "specialization_partition": "UNKNOWN",
                "owner_inference_used": False,
            },
            "component_membership": unattributed_membership,
            "eligibility": {
                "team_behavior_training_eligible": False,
                "historical_fury_policy_training_eligible": False,
                "blockers": ["PLAYER_OWNER_UNKNOWN"],
                "retained_as_uncertainty_branch": True,
            },
            "prefix_transitions": unattributed_transitions,
            "observed_attribution": {
                "event_count": sum(unattributed_counts.values()),
                "damage": _render_number(float(sum(unattributed_damage.values()))),
            },
            "exact_trace_lane": {
                "status": "DESCRIPTIVE_NONVOTING",
                "voting_eligible": False,
            },
        }
    )

    for target_summary in wave.target_summaries:
        output.append(
            {
                "schema": SCHEMA,
                "record_type": "descriptive_target_outcome",
                "wave": wave_identity,
                "lane": {
                    "status": "DESCRIPTIVE_NONVOTING",
                    "allowed_as_decision_feature": False,
                },
                "outcome": deepcopy(target_summary),
            }
        )
    _verify_wave_damage_conservation(events, wave.wave_summary)
    output.append(
        {
            "schema": SCHEMA,
            "record_type": "descriptive_wave_outcome",
            "wave": wave_identity,
            "lane": {
                "status": "DESCRIPTIVE_NONVOTING",
                "allowed_as_decision_feature": False,
            },
            "outcome": deepcopy(wave.wave_summary),
        }
    )
    return output, {
        "episode_count": episode_count,
        "fury_episode_count": fury_count,
        "arms_episode_count": arms_count,
        "transition_count": sum(len(value) for value in transitions.values())
        + len(unattributed_transitions),
        "exact_trace_event_count": len(events),
        "unattributed_transition_count": len(unattributed_transitions),
        "component_nodes": component_nodes,
        "component_edges": component_edges,
    }


def _verify_wave_damage_conservation(
    events: Sequence[Mapping[str, Any]], wave_summary: Mapping[str, Any]
) -> None:
    observed: Counter[str] = Counter()
    canonical_dmg_values: Counter[str] = Counter()
    numeric_damage_rows: Counter[str] = Counter()
    numeric_dmg_rows: Counter[str] = Counter()
    dmg_rows: Counter[str] = Counter()
    unparsed_dmg_rows: Counter[str] = Counter()
    lethal_dead_rows: Counter[str] = Counter()
    lethal_dead_values: Counter[str] = Counter()
    for event in events:
        if not _is_damage_event(event):
            continue
        kind = str(_mapping(event.get("attribution"), "event.attribution").get("kind"))
        event_type = str(event.get("event_type"))
        amount = event.get("amount")
        if event_type == "DMG":
            dmg_rows[kind] += 1
            if amount is None:
                unparsed_dmg_rows[kind] += 1
                continue
            numeric_dmg_rows[kind] += 1
            canonical_dmg_values[kind] += float(amount)
        elif event_type == "DEAD" and amount is not None:
            lethal_dead_rows[kind] += 1
            lethal_dead_values[kind] += float(amount)
        else:
            continue
        numeric_damage_rows[kind] += 1
        observed[kind] += float(amount)
    conservation = _mapping(
        wave_summary.get("damage_attribution_conservation"),
        "wave_summary.damage_attribution_conservation",
    )
    if conservation.get("sum_equals_team_damage") is not True:
        raise ChronicleTeamWaveModelError("input wave damage conservation is not attested")
    for kind, source_name in (
        ("DIRECT_PLAYER", "direct_player"),
        ("OWNED_ENTITY", "owned_entity"),
        ("UNATTRIBUTED", "unattributed"),
    ):
        source = _mapping(conservation.get(source_name), f"conservation.{source_name}")
        declared_value = _nonnegative_number(source.get("value"), f"{source_name}.value")
        checks = (
            (
                "event_count",
                _integer(source.get("event_count"), f"{source_name}.event_count"),
                numeric_damage_rows[kind],
            ),
            (
                "numeric_damage_bearing_row_count",
                _integer(
                    source.get("numeric_damage_bearing_row_count"),
                    f"{source_name}.numeric_damage_bearing_row_count",
                ),
                numeric_damage_rows[kind],
            ),
            (
                "dmg_row_count",
                _integer(source.get("dmg_row_count"), f"{source_name}.dmg_row_count"),
                dmg_rows[kind],
            ),
            (
                "unparsed_dmg_row_count",
                _integer(
                    source.get("unparsed_dmg_row_count"),
                    f"{source_name}.unparsed_dmg_row_count",
                ),
                unparsed_dmg_rows[kind],
            ),
            (
                "lethal_dead_damage_event_count",
                _integer(
                    source.get("lethal_dead_damage_event_count"),
                    f"{source_name}.lethal_dead_damage_event_count",
                ),
                lethal_dead_rows[kind],
            ),
        )
        declared_lethal_value = _nonnegative_number(
            source.get("lethal_dead_damage_value"),
            f"{source_name}.lethal_dead_damage_value",
        )
        if (
            float(declared_value) != float(observed[kind])
            or float(declared_lethal_value) != float(lethal_dead_values[kind])
            or any(declared != actual for _, declared, actual in checks)
        ):
            mismatches = [
                f"{label} declared={declared} observed={actual}"
                for label, declared, actual in checks
                if declared != actual
            ]
            raise ChronicleTeamWaveModelError(
                f"timeline attribution conservation mismatch for {kind}: "
                + "; ".join(mismatches or ["value mismatch"])
            )
    team = _mapping(wave_summary.get("team_damage"), "wave_summary.team_damage")
    team_checks = (
        ("event_count", sum(numeric_dmg_rows.values())),
        ("dmg_row_count", sum(dmg_rows.values())),
        (
            "numeric_damage_bearing_row_count",
            sum(numeric_damage_rows.values()),
        ),
        ("unparsed_event_count", sum(unparsed_dmg_rows.values())),
        ("lethal_dead_damage_event_count", sum(lethal_dead_rows.values())),
    )
    if (
        float(_nonnegative_number(team.get("value"), "team_damage.value"))
        != float(sum(observed.values()))
        or float(
            _nonnegative_number(
                team.get("canonical_dmg_value"),
                "team_damage.canonical_dmg_value",
            )
        )
        != float(sum(canonical_dmg_values.values()))
        or float(
            _nonnegative_number(
                team.get("lethal_dead_damage_value"),
                "team_damage.lethal_dead_damage_value",
            )
        )
        != float(sum(lethal_dead_values.values()))
        or any(
            _integer(team.get(field), f"team_damage.{field}") != expected
            for field, expected in team_checks
        )
    ):
        raise ChronicleTeamWaveModelError("timeline team damage total mismatch")


class _CanonicalGzipWriter:
    def __init__(self, path: Path) -> None:
        self._digest = hashlib.sha256()
        self.record_count = 0
        self._raw = path.open("wb")
        self._gzip = gzip.GzipFile(
            filename="", fileobj=self._raw, mode="wb", compresslevel=9, mtime=0
        )
        self._text = io.TextIOWrapper(self._gzip, encoding="utf-8", newline="\n")
        self._closed = False

    @property
    def logical_sha256(self) -> str:
        return self._digest.hexdigest()

    def write(self, value: Mapping[str, Any]) -> None:
        payload = _canonical_bytes(value) + b"\n"
        self._digest.update(payload)
        self._text.write(payload.decode("utf-8"))
        self.record_count += 1

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._text.flush()
            self._gzip.close()
        finally:
            self._raw.close()
            self._closed = True


def _build_partition(
    *,
    manifest_path: Path,
    entry: Mapping[str, Any],
    output_directory: Path,
) -> PartitionBuild:
    instance_id = _text(entry.get("instance_id"), "timeline partition.instance_id")
    source_path = _resolve_partition(
        manifest_path, entry.get("partition"), "timeline partition.path"
    )
    expected_compressed = _sha(
        entry.get("compressed_file_sha256"),
        "timeline partition.compressed_file_sha256",
    )
    actual_compressed = _sha256_file(source_path)
    if actual_compressed != expected_compressed:
        raise ChronicleTeamWaveModelError(
            f"timeline compressed SHA-256 mismatch for {instance_id}"
        )
    expected_size = _integer(
        entry.get("compressed_size_bytes"), "timeline partition.compressed_size_bytes"
    )
    if expected_size != source_path.stat().st_size:
        raise ChronicleTeamWaveModelError(
            f"timeline compressed size mismatch for {instance_id}"
        )
    expected_logical = _sha(
        entry.get("logical_content_sha256"),
        "timeline partition.logical_content_sha256",
    )

    with tempfile.NamedTemporaryFile(
        prefix=f".{_safe_component(instance_id)}.team-wave-model.",
        suffix=".jsonl.gz.tmp",
        dir=output_directory,
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
    writer = _CanonicalGzipWriter(temporary_path)
    wave_count = 0
    episode_count = 0
    fury_count = 0
    arms_count = 0
    transition_count = 0
    exact_event_count = 0
    unattributed_count = 0
    component_nodes: dict[str, str] = {}
    component_edges: set[tuple[str, str]] = set()
    input_counts: Counter[str]
    succeeded = False
    try:
        waves, input_digest, input_counts = _iter_waves(source_path)
        for wave in waves:
            if _wave_key(wave.header, "wave header")[0] != instance_id:
                raise ChronicleTeamWaveModelError(
                    f"timeline wave instance differs from partition {instance_id}"
                )
            rows, summary = _build_wave_output(wave)
            for row in rows:
                writer.write(row)
            wave_count += 1
            episode_count += int(summary["episode_count"])
            fury_count += int(summary["fury_episode_count"])
            arms_count += int(summary["arms_episode_count"])
            transition_count += int(summary["transition_count"])
            exact_event_count += int(summary["exact_trace_event_count"])
            unattributed_count += int(summary["unattributed_transition_count"])
            component_nodes.update(summary["component_nodes"])
            component_edges.update(summary["component_edges"])
        if input_digest.hexdigest() != expected_logical:
            raise ChronicleTeamWaveModelError(
                f"timeline logical SHA-256 mismatch for {instance_id}"
            )
        if wave_count != _integer(entry.get("wave_count"), "timeline partition.wave_count"):
            raise ChronicleTeamWaveModelError(
                f"timeline wave count mismatch for {instance_id}"
            )
        if exact_event_count != _integer(
            entry.get("event_count"), "timeline partition.event_count"
        ):
            raise ChronicleTeamWaveModelError(
                f"timeline event count mismatch for {instance_id}"
            )
        writer.close()
        logical_sha = writer.logical_sha256
        final_path = output_directory / (
            f"{_safe_component(instance_id)}.team-wave-model-v1.{logical_sha}.jsonl.gz"
        )
        compressed_sha = _sha256_file(temporary_path)
        manifest_entry = {
            "instance_id": instance_id,
            "partition": final_path.name,
            "logical_content_sha256": logical_sha,
            "compressed_file_sha256": compressed_sha,
            "compressed_size_bytes": temporary_path.stat().st_size,
            "record_count": writer.record_count,
            "wave_count": wave_count,
            "player_wave_episode_count": episode_count,
            "fury_episode_count": fury_count,
            "arms_episode_count": arms_count,
            "prefix_transition_count": transition_count,
            "exact_trace_event_count": exact_event_count,
            "unattributed_transition_count": unattributed_count,
            "timeline_input": {
                "path": "$TIMELINE/" + source_path.name,
                "logical_content_sha256": expected_logical,
                "compressed_file_sha256": actual_compressed,
                "compressed_size_bytes": expected_size,
                "record_type_counts": dict(sorted(input_counts.items())),
                "scan_count": 1,
            },
            "exact_trace_status": "DESCRIPTIVE_NONVOTING",
            "voting_ready": False,
        }
        succeeded = True
        return PartitionBuild(
            temporary_path=temporary_path,
            final_path=final_path,
            manifest_entry=manifest_entry,
            component_nodes=tuple(sorted(component_nodes.items())),
            component_edges=tuple(sorted(component_edges)),
        )
    finally:
        try:
            writer.close()
        except (OSError, ValueError):
            pass
        if not succeeded:
            temporary_path.unlink(missing_ok=True)


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, value: str) -> str:
        self.parent.setdefault(value, value)
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _split_graph(
    nodes: Mapping[str, str], edges: Iterable[tuple[str, str]]
) -> JSONMap:
    disjoint = _DisjointSet()
    for node in nodes:
        disjoint.find(node)
    normalized_edges = sorted({tuple(sorted(edge)) for edge in edges})
    for left, right in normalized_edges:
        if left not in nodes or right not in nodes:
            raise ChronicleTeamWaveModelError("component edge references an unknown node")
        disjoint.union(left, right)
    grouped: dict[str, list[str]] = defaultdict(list)
    for node in sorted(nodes):
        grouped[disjoint.find(node)].append(node)
    components = []
    node_to_component: list[JSONMap] = []
    for members in sorted(grouped.values(), key=lambda values: tuple(values)):
        component_id = _sha256_json({"nodes": members})
        components.append({"component_id": component_id, "node_ids": members})
        node_to_component.extend(
            {"node_id": node, "component_id": component_id} for node in members
        )
    return {
        "node_identity": "kind plus SHA-256 of case-folded observed identity",
        "nodes": [
            {"node_id": node, "kind": nodes[node]} for node in sorted(nodes)
        ],
        "edges": [list(edge) for edge in normalized_edges],
        "connected_components": components,
        "node_to_component": sorted(node_to_component, key=lambda row: row["node_id"]),
        "required_split_unit": "connected component",
        "row_random_split_allowed": False,
        "same_player_or_guild_can_cross_folds": False,
    }


def _atomic_json_temporary(path: Path, value: Mapping[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(_canonical_bytes(value) + b"\n")
    return temporary


def _commit_or_reuse(temporary: Path, final: Path) -> None:
    if final.exists():
        if not final.is_file() or _sha256_file(final) != _sha256_file(temporary):
            raise ChronicleTeamWaveModelError(
                f"refusing to overwrite non-identical content-addressed file {final}"
            )
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(final)


def validate_team_wave_model_manifest(document: Mapping[str, Any]) -> None:
    if document.get("schema") != SCHEMA or document.get("schema_version") != SCHEMA_VERSION:
        raise ChronicleTeamWaveModelError(f"model manifest must use {SCHEMA}")
    if document.get("kind") != "chronicle_team_wave_model_manifest":
        raise ChronicleTeamWaveModelError("invalid model manifest kind")
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ChronicleTeamWaveModelError(
            "team-model implementation revision is stale or unsupported"
        )
    _verify_content_address(document, "team model manifest")
    boundary = _mapping(document.get("claim_boundary"), "manifest.claim_boundary")
    if (
        boundary.get("exact_trace_status") != "DESCRIPTIVE_NONVOTING"
        or boundary.get("exact_trace_can_vote") is not False
        or boundary.get("learned_generator_present") is not False
        or boundary.get("comparison_ready") is not False
        or boundary.get("fourth_voting_baseline_present") is not False
        or boundary.get("arms_common_action_diagnostic_only") is not True
        or boundary.get("multiseed_12_cells_ready") is not False
    ):
        raise ChronicleTeamWaveModelError("team-model claim boundary was widened")
    split = _mapping(document.get("split_graph"), "manifest.split_graph")
    if (
        split.get("required_split_unit") != "connected component"
        or split.get("row_random_split_allowed") is not False
        or split.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ChronicleTeamWaveModelError("team-model split contract was weakened")
    partitions = _array(document.get("partitions"), "manifest.partitions")
    if not partitions:
        raise ChronicleTeamWaveModelError("team-model manifest has no partitions")
    if any(
        _mapping(value, "manifest partition").get("voting_ready") is not False
        or _mapping(value, "manifest partition").get("exact_trace_status")
        != "DESCRIPTIVE_NONVOTING"
        for value in partitions
    ):
        raise ChronicleTeamWaveModelError("a partition widened nonvoting status")


def build_chronicle_team_wave_model(
    *,
    timeline_manifest_path: str | Path = DEFAULT_TIMELINE_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
) -> ChronicleTeamWaveModelResult:
    """Build deterministic per-instance team-model training/replay capsules."""

    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ChronicleTeamWaveModelError("workers must be a positive integer")
    manifest_path = Path(timeline_manifest_path).expanduser().resolve()
    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timeline = _load_json(manifest_path, "timeline manifest")
    if (
        timeline.get("schema") != TIMELINE_SCHEMA
        or timeline.get("schema_version") != 1
        or timeline.get("kind") != TIMELINE_MANIFEST_KIND
        or timeline.get("implementation_revision")
        != TIMELINE_IMPLEMENTATION_REVISION
    ):
        raise ChronicleTeamWaveModelError(
            f"input manifest must be current {TIMELINE_SCHEMA} "
            f"{TIMELINE_MANIFEST_KIND} revision {TIMELINE_IMPLEMENTATION_REVISION}"
        )
    timeline_content_sha = _verify_content_address(timeline, "timeline manifest")
    entries = [
        dict(_mapping(value, f"timeline.partitions[{index}]"))
        for index, value in enumerate(_array(timeline.get("partitions"), "timeline.partitions"))
    ]
    if not entries:
        raise ChronicleTeamWaveModelError("timeline manifest has no partitions")
    instance_ids = [
        _text(value.get("instance_id"), f"timeline.partitions[{index}].instance_id")
        for index, value in enumerate(entries)
    ]
    if len(set(instance_ids)) != len(instance_ids):
        raise ChronicleTeamWaveModelError("timeline partition instance IDs must be unique")

    builds: list[PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        if workers == 1 or len(entries) == 1:
            for entry in entries:
                builds.append(
                    _build_partition(
                        manifest_path=manifest_path,
                        entry=entry,
                        output_directory=output_dir,
                    )
                )
        else:
            completed: dict[int, PartitionBuild] = {}
            first_error: BaseException | None = None
            # Partition compilation is dominated by gzip/JSON decoding and
            # canonical JSON encoding.  Threads serialize most of that work
            # behind CPython's GIL, so ``--workers`` did not provide real
            # parallelism on the Windows data host.  Each partition is fully
            # independent and ``_build_partition`` is a top-level, pickle-safe
            # function; a process pool therefore makes the requested worker
            # count effective while preserving the deterministic index-order
            # commit below.
            with ProcessPoolExecutor(max_workers=min(workers, len(entries))) as executor:
                futures = {
                    executor.submit(
                        _build_partition,
                        manifest_path=manifest_path,
                        entry=entry,
                        output_directory=output_dir,
                    ): index
                    for index, entry in enumerate(entries)
                }
                for future in as_completed(futures):
                    try:
                        completed[futures[future]] = future.result()
                    except BaseException as error:
                        if first_error is None:
                            first_error = error
            if first_error is not None:
                for build in completed.values():
                    build.temporary_path.unlink(missing_ok=True)
                raise first_error
            builds = [completed[index] for index in range(len(entries))]

        nodes: dict[str, str] = {}
        edges: set[tuple[str, str]] = set()
        for build in builds:
            for node, kind in build.component_nodes:
                previous = nodes.setdefault(node, kind)
                if previous != kind:
                    raise ChronicleTeamWaveModelError(
                        f"component node kind conflict for {node}"
                    )
            edges.update(build.component_edges)
        split_graph = _split_graph(nodes, edges)
        manifest_core = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "chronicle_team_wave_model_manifest",
            "implementation_revision": IMPLEMENTATION_REVISION,
            "input": {
                "timeline_manifest_path": _portable_input_path(manifest_path),
                "timeline_manifest_file_sha256": _sha256_file(manifest_path),
                "timeline_manifest_content_sha256": timeline_content_sha,
                "timeline_schema": TIMELINE_SCHEMA,
            },
            "output_contract": {
                "format": "deterministic gzip-compressed canonical JSON Lines",
                "partition_grain": "instance",
                "manifest_committed_last": True,
                "timeline_partition_scan_count": 1,
                "records": [
                    "wave_model_header",
                    "exact_trace_event",
                    "player_wave_episode",
                    "unattributed_wave_episode",
                    "descriptive_target_outcome",
                    "descriptive_wave_outcome",
                ],
                "focal_background": (
                    "filter direct and owned events whose explicit player GUID equals "
                    "the focal GUID; always retain unattributed as a separate branch"
                ),
                "damage_value_lane": "FULL_NORMALIZED_WAVE_NUMERIC_DMG_ONLY",
                "numeric_dead_handling": (
                    "retained in exact trace and terminal state; excluded from learned prefix damage to prevent duplicate lethal accounting"
                ),
                "bootstrap_rng": "NOT_IMPLEMENTED",
                "raw_or_normalized_input_opened": False,
            },
            "training_contract": {
                "default_eligible_contamination_statuses": sorted(
                    DEFAULT_TRAINING_CONTAMINATION_STATUSES
                ),
                "default_nonvoting_contamination_statuses": sorted(
                    NONVOTING_CONTAMINATION_STATUSES
                ),
                "fury_and_arms_are_separate_partitions": True,
                "unknown_owner_class_or_spec_is_never_guessed": True,
                "historical_fury_policy_requires_observed_single_fury_spec": True,
                "decision_feature_cutoff": "strictly before current event order_key",
                "death_clock_or_final_wave_totals_in_decision_features": False,
                "current_event_is_label_or_outcome_not_state_feature": True,
                "action_target_is_observed_on_start_cast_or_fail": True,
                "instant_cast_dedup_must_be_prefix_causal": True,
                "target_switch_features_are_strict_prefix": True,
                "attackability_is_not_inferred_from_silence": True,
                "focal_background_excludes_direct_and_owned_prefix_damage": True,
                "unattributed_prefix_damage_is_retained_as_explicit_unknown": True,
                "capsule_reconstruction_lane_is_descriptive_only": True,
                "pre_hostility_admission_damage_is_retained_in_prefix_after_observation": True,
                "post_first_dead_damage_is_not_backfilled_into_kill_budget": True,
                "historical_exact_replay_role": "DESCRIPTIVE_NONVOTING",
                "future_fourth_baseline_candidate": (
                    "learned prefix-causal Fury behavior policy only, after validation"
                ),
                "arms_history_role": "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING",
                "named_player_override_allowed": False,
            },
            "split_graph": split_graph,
            "claim_boundary": {
                "exact_trace_status": "DESCRIPTIVE_NONVOTING",
                "exact_trace_can_vote": False,
                "learned_generator_present": False,
                "prefix_causal_generator_validated": False,
                "dynamic_simulator_binding_present": False,
                "fourth_voting_baseline_present": False,
                "arms_common_action_diagnostic_only": True,
                "multiseed_12_cells_ready": False,
                "comparison_ready": False,
                "superiority_claim": False,
            },
            "summary": {
                "instance_count": len(builds),
                "wave_count": sum(row.manifest_entry["wave_count"] for row in builds),
                "player_wave_episode_count": sum(
                    row.manifest_entry["player_wave_episode_count"] for row in builds
                ),
                "fury_episode_count": sum(
                    row.manifest_entry["fury_episode_count"] for row in builds
                ),
                "arms_episode_count": sum(
                    row.manifest_entry["arms_episode_count"] for row in builds
                ),
                "prefix_transition_count": sum(
                    row.manifest_entry["prefix_transition_count"] for row in builds
                ),
                "exact_trace_event_count": sum(
                    row.manifest_entry["exact_trace_event_count"] for row in builds
                ),
                "voting_ready": False,
            },
            "partitions": [row.manifest_entry for row in builds],
        }
        manifest = {
            **manifest_core,
            "content_address": {
                "algorithm": "sha256",
                "scope": "canonical JSON document excluding content_address",
                "sha256": _sha256_json(manifest_core),
            },
        }
        validate_team_wave_model_manifest(manifest)
        addressed_path = output_dir / (
            f"chronicle_team_wave_model_v1."
            f"{manifest['content_address']['sha256']}.manifest.json"
        )
        stable_path = output_dir / "manifest.json"
        addressed_temporary = _atomic_json_temporary(addressed_path, manifest)
        stable_temporary = _atomic_json_temporary(stable_path, manifest)

        for build in builds:
            _commit_or_reuse(build.temporary_path, build.final_path)
        _commit_or_reuse(addressed_temporary, addressed_path)
        addressed_temporary = None
        # The stable manifest is the only mutable commit marker and is last.
        stable_temporary.replace(stable_path)
        stable_temporary = None
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)

    return ChronicleTeamWaveModelResult(
        manifest=stable_path,
        content_addressed_manifest=addressed_path,
        partitions=tuple(build.final_path for build in builds),
        instance_count=len(builds),
        wave_count=sum(build.manifest_entry["wave_count"] for build in builds),
        player_wave_episode_count=sum(
            build.manifest_entry["player_wave_episode_count"] for build in builds
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeline-manifest", type=Path, default=DEFAULT_TIMELINE_MANIFEST
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_chronicle_team_wave_model(
            timeline_manifest_path=args.timeline_manifest,
            output_directory=args.output_dir,
            workers=args.workers,
        )
    except ChronicleTeamWaveModelError as error:
        print(f"Chronicle team-wave model build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "ChronicleTeamWaveModelError",
    "ChronicleTeamWaveModelResult",
    "DEFAULT_TRAINING_CONTAMINATION_STATUSES",
    "IMPLEMENTATION_REVISION",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_chronicle_team_wave_model",
    "build_leave_one_player_out_background",
    "validate_team_wave_model_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
