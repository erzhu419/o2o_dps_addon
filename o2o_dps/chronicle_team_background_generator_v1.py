"""Compile and replay empirical Chronicle team-background block draws.

This layer consumes ``chronicle_team_wave_model/v1`` artifacts only.  It does
not open normalized Chronicle data, infer retargeting, or bind to the current
simulator protocol.  A bootstrap draw selects one whole historical raid-wave
block inside one leakage component, preserving the observed correlation among
all teammate tracks.  The focal player's DIRECT_PLAYER and OWNED_ENTITY track
is then removed as a unit; unattributed events remain an explicit uncertainty
branch.

Both exact-derived blocks and bootstrap draws are diagnostic/training inputs.
They are deliberately nonvoting and not comparison ready until learned team
response/retarget models and a dynamic simulator bridge pass separate gates.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .chronicle_external_api_ingest_v1 import range_bug_boundary_contract
from .chronicle_team_wave_model_v1 import (
    IMPLEMENTATION_REVISION as TEAM_MODEL_IMPLEMENTATION_REVISION,
)
from .chronicle_team_wave_timeline_v1 import (
    CONTAMINATION_RULE_VERSION,
    LEGACY_GUILD,
    LEGACY_GUILD_IDS,
)


JSONMap = dict[str, Any]
SCHEMA = "chronicle_team_background_generator/v1"
SCHEMA_VERSION = 1
IMPLEMENTATION_REVISION = "v1.1_component_boundary_nontraining_bootstrap"
TEAM_MODEL_SCHEMA = "chronicle_team_wave_model/v1"
TEAM_MODEL_MANIFEST_KIND = "chronicle_team_wave_model_manifest"
MANIFEST_KIND = "chronicle_team_background_generator_manifest"
BLOCK_KIND = "chronicle_team_background_whole_wave_block"
DRAW_KIND = "chronicle_team_background_bootstrap_draw"
REPLAY_KIND = "chronicle_team_background_runtime_replay"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEAM_MODEL_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_team_wave_model"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_team_background_generator"
    / "v1"
)

TRAINING_CONTAMINATION_STATUSES = frozenset(
    ("POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH")
)
NONTRAINING_CONTAMINATION_STATUSES = frozenset(
    (
        "SUSPECT_36YD_RANGE_BUG",
        "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
        "UNKNOWN_NONVOTING",
    )
)
ALL_CONTAMINATION_STATUSES = (
    TRAINING_CONTAMINATION_STATUSES | NONTRAINING_CONTAMINATION_STATUSES
)
ATTRIBUTION_KINDS = frozenset(("DIRECT_PLAYER", "OWNED_ENTITY", "UNATTRIBUTED"))
FOCAL_REMOVAL_KINDS = frozenset(("DIRECT_PLAYER", "OWNED_ENTITY"))
ACTION_EVENT_TYPES = frozenset(("START", "CAST", "FAIL"))
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")

RETARGET_UNIDENTIFIED = "RETARGET_UNIDENTIFIED"
NO_RETARGET_CONTROL = "NO_RETARGET_CONTROL"
EXPLICIT_TARGET_MAPPING = "EXPLICIT_TARGET_MAPPING"
RETARGET_MODES = frozenset(
    (RETARGET_UNIDENTIFIED, NO_RETARGET_CONTROL, EXPLICIT_TARGET_MAPPING)
)

CLAIM_BLOCKERS = (
    "LEARNED_TEAM_RESPONSE_MODEL_MISSING",
    "LEARNED_RETARGET_MODEL_MISSING",
    "DYNAMIC_SIMULATOR_BRIDGE_ADMISSION_MISSING",
)


def _expected_contamination_contract() -> JSONMap:
    """Render the shared started-at rule without introducing another cutoff."""

    boundary = range_bug_boundary_contract()
    suspect_before = boundary["pre_fix_suspect_before_local"]
    postfix_at_or_after = boundary["postfix_known_clean_at_or_after_local"]
    return {
        "rule_version": CONTAMINATION_RULE_VERSION,
        "guild": LEGACY_GUILD,
        "known_equivalent_guild_ids": sorted(LEGACY_GUILD_IDS),
        "guild_identity_match_evidence": ["EXACT_NAME", "KNOWN_GUILD_ID"],
        "fuzzy_name_matching": False,
        **boundary,
        "segments": [
            {
                "started_at_before_local": suspect_before,
                "status": "SUSPECT_36YD_RANGE_BUG",
                "training_eligible": False,
            },
            {
                "started_at_at_or_after_local": suspect_before,
                "started_at_before_local": postfix_at_or_after,
                "status": "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
                "training_eligible": False,
            },
            {
                "started_at_at_or_after_local": postfix_at_or_after,
                "status": "POSTFIX_KNOWN_CLEAN",
                "training_eligible": True,
            },
        ],
        "training_eligible_statuses": sorted(TRAINING_CONTAMINATION_STATUSES),
        "nontraining_statuses": sorted(NONTRAINING_CONTAMINATION_STATUSES),
        "player_name_special_cases": False,
    }


class ChronicleTeamBackgroundGeneratorError(RuntimeError):
    """An input or generated background artifact violates the V1 contract."""


@dataclass(frozen=True)
class _WaveRecords:
    header: JSONMap
    exact_events: tuple[JSONMap, ...]
    player_episodes: tuple[JSONMap, ...]
    unattributed_episode: JSONMap
    target_outcomes: tuple[JSONMap, ...]
    wave_outcome: JSONMap


@dataclass(frozen=True)
class _BlockBuild:
    temporary_path: Path
    final_path: Path
    manifest_entry: JSONMap


@dataclass(frozen=True)
class ChronicleTeamBackgroundGeneratorResult:
    manifest: Path
    content_addressed_manifest: Path
    blocks: tuple[Path, ...]
    component_count: int
    block_count: int
    training_eligible_block_count: int

    def as_dict(self) -> JSONMap:
        return {
            "status": "ok",
            "schema": SCHEMA,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "blocks": [str(path) for path in self.blocks],
            "component_count": self.component_count,
            "block_count": self.block_count,
            "training_eligible_block_count": self.training_eligible_block_count,
            "comparison_ready": False,
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
        raise ChronicleTeamBackgroundGeneratorError(
            f"value is not canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ChronicleTeamBackgroundGeneratorError(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleTeamBackgroundGeneratorError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleTeamBackgroundGeneratorError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} must be a non-empty string"
        )
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ChronicleTeamBackgroundGeneratorError(f"{label} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} must be an integer"
        ) from error


def _nonnegative_number(value: Any, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} must be nonnegative numeric"
        )
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError) as error:
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} must be nonnegative numeric"
        ) from error
    if not math.isfinite(parsed) or parsed < 0:
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} must be finite and nonnegative"
        )
    return parsed


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _guid_key(value: Any) -> str:
    return (_optional_text(value) or "").casefold()


def _safe_component(value: str) -> str:
    rendered = SAFE_COMPONENT_RE.sub("-", value).strip("-._")
    if not rendered:
        raise ChronicleTeamBackgroundGeneratorError(
            f"cannot derive a safe filename from {value!r}"
        )
    return rendered


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        opener = gzip.open if path.name.endswith(".gz") else path.open
        if path.name.endswith(".gz"):
            with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[arg-type]
                value = json.load(handle)
        else:
            with opener("r", encoding="utf-8") as handle:  # type: ignore[call-arg]
                value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleTeamBackgroundGeneratorError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleTeamBackgroundGeneratorError(f"{label} must be an object")
    return value


def _verify_content_address(document: Mapping[str, Any], label: str) -> str:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    declared = _text(address.get("sha256"), f"{label}.content_address.sha256")
    core = {key: value for key, value in document.items() if key != "content_address"}
    actual = _sha256_json(core)
    if declared != actual:
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} content address mismatch: declared {declared}, computed {actual}"
        )
    return actual


def _portable_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return f"$EXTERNAL/{resolved.name}"


def _resolve_relative(manifest_path: Path, value: Any, label: str) -> Path:
    rendered = _text(value, label)
    candidate = Path(rendered)
    paths = [candidate]
    if not candidate.is_absolute():
        paths.insert(0, manifest_path.parent / candidate)
    for path in paths:
        if path.is_file():
            return path.resolve()
    raise ChronicleTeamBackgroundGeneratorError(
        f"cannot resolve {label} {rendered!r} from {manifest_path}"
    )


def _wave_identity(value: Any, label: str) -> JSONMap:
    wave = _mapping(value, label)
    result = {
        "instance_id": _text(wave.get("instance_id"), f"{label}.instance_id"),
        "encounter_id": _text(wave.get("encounter_id"), f"{label}.encounter_id"),
        "wave_id": _text(wave.get("wave_id"), f"{label}.wave_id"),
        "wave_ordinal": _integer(
            wave.get("wave_ordinal"), f"{label}.wave_ordinal"
        ),
    }
    return result


def _wave_key(value: Any, label: str) -> tuple[str, str, str, int]:
    wave = _wave_identity(value, label)
    return (
        wave["instance_id"],
        wave["encounter_id"],
        wave["wave_id"],
        wave["wave_ordinal"],
    )


def _node_component_index(manifest: Mapping[str, Any]) -> dict[str, str]:
    graph = _mapping(manifest.get("split_graph"), "team_model.split_graph")
    if (
        graph.get("required_split_unit") != "connected component"
        or graph.get("row_random_split_allowed") is not False
        or graph.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            "team-model split graph does not enforce connected-component isolation"
        )
    result: dict[str, str] = {}
    for index, raw in enumerate(
        _array(graph.get("node_to_component"), "split_graph.node_to_component")
    ):
        row = _mapping(raw, f"split_graph.node_to_component[{index}]")
        node = _text(row.get("node_id"), "node_to_component.node_id")
        component = _text(
            row.get("component_id"), "node_to_component.component_id"
        )
        previous = result.setdefault(node, component)
        if previous != component:
            raise ChronicleTeamBackgroundGeneratorError(
                f"split node {node} maps to multiple components"
            )
    if not result:
        raise ChronicleTeamBackgroundGeneratorError("split graph has no node mapping")
    return result


def _validate_team_model_manifest(document: Mapping[str, Any]) -> str:
    if (
        document.get("schema") != TEAM_MODEL_SCHEMA
        or document.get("schema_version") != 1
        or document.get("kind") != TEAM_MODEL_MANIFEST_KIND
        or document.get("implementation_revision")
        != TEAM_MODEL_IMPLEMENTATION_REVISION
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            f"input manifest must be {TEAM_MODEL_SCHEMA} {TEAM_MODEL_MANIFEST_KIND}"
        )
    content_sha = _verify_content_address(document, "team model manifest")
    boundary = _mapping(document.get("claim_boundary"), "team_model.claim_boundary")
    if (
        boundary.get("exact_trace_status") != "DESCRIPTIVE_NONVOTING"
        or boundary.get("exact_trace_can_vote") is not False
        or boundary.get("comparison_ready") is not False
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            "input team model widened its exact-trace claim boundary"
        )
    _node_component_index(document)
    return content_sha


def _iter_partition_waves(path: Path) -> Iterator[_WaveRecords]:
    header: JSONMap | None = None
    current_key: tuple[str, str, str, int] | None = None
    exact_events: list[JSONMap] = []
    players: list[JSONMap] = []
    unattributed: JSONMap | None = None
    targets: list[JSONMap] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ChronicleTeamBackgroundGeneratorError(
                        f"invalid JSON at {path}:{line_number}: {error}"
                    ) from error
                row = dict(_mapping(raw, f"{path}:{line_number}"))
                if row.get("schema") != TEAM_MODEL_SCHEMA:
                    raise ChronicleTeamBackgroundGeneratorError(
                        f"wrong schema at {path}:{line_number}"
                    )
                record_type = _text(
                    row.get("record_type"), f"{path}:{line_number}.record_type"
                )
                row_key = _wave_key(row.get("wave"), f"{path}:{line_number}.wave")
                if record_type == "wave_model_header":
                    if header is not None:
                        raise ChronicleTeamBackgroundGeneratorError(
                            f"new wave began before prior outcome at {path}:{line_number}"
                        )
                    header = row
                    current_key = row_key
                    exact_events = []
                    players = []
                    unattributed = None
                    targets = []
                    continue
                if header is None or row_key != current_key:
                    raise ChronicleTeamBackgroundGeneratorError(
                        f"orphan or cross-wave record at {path}:{line_number}"
                    )
                if record_type == "exact_trace_event":
                    if players or unattributed is not None or targets:
                        raise ChronicleTeamBackgroundGeneratorError(
                            f"exact event appears after episode records at {path}:{line_number}"
                        )
                    exact_events.append(
                        dict(_mapping(row.get("event"), "exact_trace_event.event"))
                    )
                elif record_type == "player_wave_episode":
                    if targets:
                        raise ChronicleTeamBackgroundGeneratorError(
                            f"player episode appears after target outcomes at {path}:{line_number}"
                        )
                    players.append(row)
                elif record_type == "unattributed_wave_episode":
                    if unattributed is not None:
                        raise ChronicleTeamBackgroundGeneratorError(
                            f"duplicate unattributed episode at {path}:{line_number}"
                        )
                    unattributed = row
                elif record_type == "descriptive_target_outcome":
                    targets.append(
                        dict(
                            _mapping(
                                row.get("outcome"),
                                "descriptive_target_outcome.outcome",
                            )
                        )
                    )
                elif record_type == "descriptive_wave_outcome":
                    if unattributed is None:
                        raise ChronicleTeamBackgroundGeneratorError(
                            f"wave lacks unattributed episode at {path}:{line_number}"
                        )
                    yield _WaveRecords(
                        header=header,
                        exact_events=tuple(exact_events),
                        player_episodes=tuple(players),
                        unattributed_episode=unattributed,
                        target_outcomes=tuple(targets),
                        wave_outcome=dict(
                            _mapping(row.get("outcome"), "descriptive_wave_outcome.outcome")
                        ),
                    )
                    header = None
                    current_key = None
                else:
                    raise ChronicleTeamBackgroundGeneratorError(
                        f"unsupported record type {record_type!r} at {path}:{line_number}"
                    )
    except (OSError, UnicodeError) as error:
        raise ChronicleTeamBackgroundGeneratorError(
            f"cannot stream team-model partition {path}: {error}"
        ) from error
    if header is not None:
        raise ChronicleTeamBackgroundGeneratorError(
            f"partition ended before descriptive_wave_outcome: {path}"
        )


def _episode_component(
    episode: Mapping[str, Any], node_to_component: Mapping[str, str], label: str
) -> str:
    membership = _mapping(episode.get("component_membership"), f"{label}.component_membership")
    nodes = [
        _text(membership.get("instance_node_id"), f"{label}.instance_node_id")
    ]
    player_node = _optional_text(membership.get("player_node_id"))
    if player_node:
        nodes.append(player_node)
    for raw in _array(membership.get("guild_node_ids"), f"{label}.guild_node_ids"):
        nodes.append(_text(raw, f"{label}.guild_node_id"))
    components: set[str] = set()
    for node in nodes:
        component = node_to_component.get(node)
        if component is None:
            raise ChronicleTeamBackgroundGeneratorError(
                f"{label} references split node absent from manifest: {node}"
            )
        components.add(component)
    if len(components) != 1:
        raise ChronicleTeamBackgroundGeneratorError(
            f"{label} crosses leakage components: {sorted(components)}"
        )
    return next(iter(components))


def _target_guid_from_outcome(value: Mapping[str, Any]) -> str | None:
    direct = _optional_text(value.get("target_guid"))
    if direct:
        return direct
    nested = value.get("target")
    if isinstance(nested, Mapping):
        return _optional_text(nested.get("target_guid") or nested.get("guid"))
    return None


def _schedule_event(event: Mapping[str, Any]) -> JSONMap | None:
    event_type = _text(event.get("event_type"), "exact event.event_type")
    if event_type not in ACTION_EVENT_TYPES and event_type != "DMG":
        return None
    order_raw = _array(event.get("order_key"), "exact event.order_key")
    if len(order_raw) != 3:
        raise ChronicleTeamBackgroundGeneratorError(
            "exact event.order_key must contain three integers"
        )
    order = [
        _integer(value, f"exact event.order_key[{index}]")
        for index, value in enumerate(order_raw)
    ]
    attribution = _mapping(event.get("attribution"), "exact event.attribution")
    kind = _text(attribution.get("kind"), "exact event.attribution.kind")
    if kind not in ATTRIBUTION_KINDS:
        raise ChronicleTeamBackgroundGeneratorError(
            f"unsupported attribution kind {kind!r}"
        )
    target = _mapping(event.get("target"), "exact event.target")
    source = _mapping(event.get("source"), "exact event.source")
    spell = _mapping(event.get("spell"), "exact event.spell")
    amount = event.get("amount")
    if amount is not None:
        amount = _render_number(_nonnegative_number(amount, "exact event.amount"))
    relative_ms = _integer(
        event.get("wave_offset_ms"), "exact event.wave_offset_ms"
    )
    if relative_ms < 0:
        raise ChronicleTeamBackgroundGeneratorError(
            "exact event.wave_offset_ms must be nonnegative"
        )
    core = {
        "relative_ms": relative_ms,
        "historical_order_key": order,
        "kind": "DAMAGE" if event_type == "DMG" else "ACTION",
        "event_type": event_type,
        "source_event_type": _text(
            event.get("source_event_type"), "exact event.source_event_type"
        ),
        "source_guid": _optional_text(source.get("guid")),
        "target_guid": _optional_text(target.get("guid")),
        "spell": {
            "id": spell.get("id"),
            "name": _optional_text(spell.get("name")),
        },
        "amount": amount,
        "amount_status": _optional_text(event.get("amount_status")),
        "attribution_kind": kind,
        "owner_player_guid": _optional_text(attribution.get("player_guid")),
        "runtime_damage_eligible": event_type == "DMG" and amount is not None,
        "historical_outcome": event.get("outcome"),
    }
    return {**core, "event_sha256": _sha256_json(core)}


def _target_tracks(events: Sequence[Mapping[str, Any]]) -> list[JSONMap]:
    grouped: dict[str, list[str]] = defaultdict(list)
    display: dict[str, str | None] = {}
    for event in events:
        target = _optional_text(event.get("target_guid"))
        key = _guid_key(target) if target else "__no_target__"
        display.setdefault(key, target)
        grouped[key].append(_text(event.get("event_sha256"), "event.event_sha256"))
    return [
        {
            "target_guid": display[key],
            "event_count": len(grouped[key]),
            "event_sha256s": grouped[key],
        }
        for key in sorted(grouped)
    ]


def _build_block_document(
    wave: _WaveRecords,
    node_to_component: Mapping[str, str],
    *,
    source_partition_sha256: str,
) -> JSONMap:
    wave_identity = _wave_identity(wave.header.get("wave"), "wave_model_header.wave")
    contamination = deepcopy(
        dict(_mapping(wave.header.get("contamination"), "wave_model_header.contamination"))
    )
    contamination_status = _text(
        contamination.get("status"), "wave_model_header.contamination.status"
    )
    if contamination_status not in ALL_CONTAMINATION_STATUSES:
        raise ChronicleTeamBackgroundGeneratorError(
            f"unsupported contamination status {contamination_status!r}"
        )
    header_eligibility = _mapping(
        wave.header.get("training_eligibility"),
        "wave_model_header.training_eligibility",
    )
    clean = contamination_status in TRAINING_CONTAMINATION_STATUSES
    training_eligible = clean and header_eligibility.get("default_eligible") is True

    episode_by_guid: dict[str, JSONMap] = {}
    components: set[str] = set()
    for index, episode in enumerate(wave.player_episodes):
        player = _mapping(episode.get("player"), f"player_episode[{index}].player")
        guid = _text(player.get("guid"), f"player_episode[{index}].player.guid")
        key = _guid_key(guid)
        if key in episode_by_guid:
            raise ChronicleTeamBackgroundGeneratorError(
                f"duplicate player episode {guid} in {wave_identity['wave_id']}"
            )
        episode_by_guid[key] = episode
        components.add(
            _episode_component(
                episode, node_to_component, f"player_episode[{index}]"
            )
        )
    components.add(
        _episode_component(
            wave.unattributed_episode, node_to_component, "unattributed_episode"
        )
    )
    if len(components) != 1:
        raise ChronicleTeamBackgroundGeneratorError(
            f"wave {wave_identity['wave_id']} crosses components: {sorted(components)}"
        )
    component_id = next(iter(components))

    target_guids = sorted(
        {
            guid
            for outcome in wave.target_outcomes
            if (guid := _target_guid_from_outcome(outcome)) is not None
        },
        key=str.casefold,
    )
    player_events: dict[str, list[JSONMap]] = defaultdict(list)
    unattributed_events: list[JSONMap] = []
    skipped_types: Counter[str] = Counter()
    last_order: tuple[int, int, int] | None = None
    for index, raw_event in enumerate(wave.exact_events):
        order_raw = _array(raw_event.get("order_key"), f"exact_events[{index}].order_key")
        if len(order_raw) != 3:
            raise ChronicleTeamBackgroundGeneratorError(
                f"exact_events[{index}].order_key must have three values"
            )
        order = tuple(
            _integer(value, f"exact_events[{index}].order_key[{position}]")
            for position, value in enumerate(order_raw)
        )
        if last_order is not None and order <= last_order:
            raise ChronicleTeamBackgroundGeneratorError(
                f"non-strict historical order in {wave_identity['wave_id']}"
            )
        last_order = order
        compact = _schedule_event(raw_event)
        if compact is None:
            skipped_types[str(raw_event.get("event_type") or "UNKNOWN")] += 1
            continue
        kind = compact["attribution_kind"]
        owner = _optional_text(compact.get("owner_player_guid"))
        if kind in FOCAL_REMOVAL_KINDS:
            if owner is None or _guid_key(owner) not in episode_by_guid:
                raise ChronicleTeamBackgroundGeneratorError(
                    f"owned/direct event lacks a player episode in {wave_identity['wave_id']}"
                )
            player_events[_guid_key(owner)].append(compact)
        elif kind == "UNATTRIBUTED":
            if owner is not None:
                raise ChronicleTeamBackgroundGeneratorError(
                    "UNATTRIBUTED exact event guessed a player owner"
                )
            unattributed_events.append(compact)

    player_tracks: list[JSONMap] = []
    for key in sorted(episode_by_guid):
        episode = episode_by_guid[key]
        player = deepcopy(dict(_mapping(episode.get("player"), "episode.player")))
        _text(player.get("hero_class"), "episode.player.hero_class")
        eligibility = deepcopy(
            dict(_mapping(episode.get("eligibility"), "episode.eligibility"))
        )
        specialization = _mapping(
            player.get("specialization"), "episode.player.specialization"
        )
        partition = _text(
            specialization.get("partition_key"),
            "episode.player.specialization.partition_key",
        )
        if partition == "WARRIOR_FURY":
            historical_lane = "FURY_TRAINING_INPUT_NONVOTING"
        elif partition == "WARRIOR_ARMS":
            historical_lane = "ARMS_COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING"
        else:
            historical_lane = "TEAM_BACKGROUND_TRACK_NONVOTING"
        events = sorted(
            player_events.get(key, []),
            key=lambda row: tuple(row["historical_order_key"]),
        )
        player_tracks.append(
            {
                "episode_id": _text(episode.get("episode_id"), "episode.episode_id"),
                "player": player,
                "specialization_partition": partition,
                "historical_warrior_lane": historical_lane,
                "team_background_training_eligible": (
                    training_eligible
                    and eligibility.get("team_behavior_training_eligible") is True
                ),
                "fury_policy_training_eligible": (
                    training_eligible
                    and partition == "WARRIOR_FURY"
                    and eligibility.get("historical_fury_policy_training_eligible")
                    is True
                ),
                "events": events,
                "target_tracks": _target_tracks(events),
            }
        )

    unattributed_events.sort(key=lambda row: tuple(row["historical_order_key"]))
    block_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": BLOCK_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "component_id": component_id,
        "source_wave": wave_identity,
        "source_partition_logical_sha256": source_partition_sha256,
        "contamination": contamination,
        "eligibility": {
            "training_eligible": training_eligible,
            "voting_eligible": False,
            "clean_statuses": sorted(TRAINING_CONTAMINATION_STATUSES),
            "blockers": (
                []
                if training_eligible
                else [f"CONTAMINATION_{contamination_status}"]
            ),
        },
        "target_guids": target_guids,
        "historical_duration_ms": max(
            max(
                (
                    int(event["relative_ms"])
                    for values in player_events.values()
                    for event in values
                ),
                default=0,
            ),
            max(
                (int(event["relative_ms"]) for event in unattributed_events),
                default=0,
            ),
        ),
        "player_tracks": player_tracks,
        "unattributed_branch": {
            "status": "EXPLICIT_UNKNOWN_NONVOTING",
            "events": unattributed_events,
            "target_tracks": _target_tracks(unattributed_events),
        },
        "diagnostics": {
            "skipped_exact_event_type_counts": dict(sorted(skipped_types.items())),
            "numeric_dead_is_not_scheduled_as_damage": True,
            "healing_is_not_a_runtime_background_event_in_v1": True,
        },
        "bootstrap_contract": {
            "sampling_unit": "WHOLE_PLAYER_WAVE_RAID_COMPONENT_BLOCK",
            "players_sampled_independently": False,
            "events_sampled_independently": False,
            "within_wave_player_correlation_preserved": True,
            "with_replacement_across_draws": True,
        },
        "claim_boundary": {
            "role": "EXACT_DERIVED_BOOTSTRAP_TRAINING_DIAGNOSTIC_NONVOTING",
            "comparison_ready": False,
            "blockers": list(CLAIM_BLOCKERS),
        },
    }
    return {
        **block_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(block_core),
        },
    }


def _write_gzip_document(
    document: Mapping[str, Any], output_directory: Path, filename_prefix: str
) -> _BlockBuild:
    content_sha = _verify_content_address(document, "background block")
    payload = _canonical_bytes(document) + b"\n"
    logical_sha = hashlib.sha256(payload).hexdigest()
    with tempfile.NamedTemporaryFile(
        prefix=f".{_safe_component(filename_prefix)}.",
        suffix=".json.gz.tmp",
        dir=output_directory,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
            ) as compressed:
                compressed.write(payload)
        final = output_directory / (
            f"{_safe_component(filename_prefix)}.{content_sha}.json.gz"
        )
        wave = _mapping(document.get("source_wave"), "block.source_wave")
        tracks = _array(document.get("player_tracks"), "block.player_tracks")
        entry = {
            "block_id": content_sha,
            "component_id": _text(document.get("component_id"), "block.component_id"),
            "source_wave": deepcopy(dict(wave)),
            "block": final.name,
            "document_content_sha256": content_sha,
            "logical_content_sha256": logical_sha,
            "compressed_file_sha256": _sha256_file(temporary),
            "compressed_size_bytes": temporary.stat().st_size,
            "training_eligible": _mapping(
                document.get("eligibility"), "block.eligibility"
            ).get("training_eligible")
            is True,
            "voting_eligible": False,
            "player_guids": sorted(
                _text(
                    _mapping(track, "player_track").get("player", {}).get("guid"),
                    "player_track.player.guid",
                )
                for track in tracks
            ),
            "target_guids": deepcopy(
                _array(document.get("target_guids"), "block.target_guids")
            ),
            "player_track_count": len(tracks),
            "event_count": sum(
                len(_array(_mapping(track, "player_track").get("events"), "track.events"))
                for track in tracks
            )
            + len(
                _array(
                    _mapping(
                        document.get("unattributed_branch"),
                        "block.unattributed_branch",
                    ).get("events"),
                    "unattributed.events",
                )
            ),
        }
        return _BlockBuild(temporary, final, entry)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _commit_or_reuse(temporary: Path, final: Path) -> None:
    if final.exists():
        if not final.is_file() or _sha256_file(final) != _sha256_file(temporary):
            raise ChronicleTeamBackgroundGeneratorError(
                f"refusing to overwrite non-identical content-addressed file {final}"
            )
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(final)


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


def validate_background_generator_manifest(document: Mapping[str, Any]) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != MANIFEST_KIND
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background manifest must use {SCHEMA} {MANIFEST_KIND}"
        )
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ChronicleTeamBackgroundGeneratorError(
            "background manifest implementation revision is stale or unsupported"
        )
    _verify_content_address(document, "background generator manifest")
    boundary = _mapping(document.get("claim_boundary"), "manifest.claim_boundary")
    if (
        boundary.get("comparison_ready") is not False
        or boundary.get("voting_eligible") is not False
        or boundary.get("learned_team_response_present") is not False
        or boundary.get("learned_retarget_present") is not False
        or boundary.get("dynamic_bridge_admitted") is not False
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            "background manifest widened its claim boundary"
        )
    bootstrap = _mapping(document.get("bootstrap_contract"), "manifest.bootstrap_contract")
    if (
        bootstrap.get("sampling_unit")
        != "WHOLE_PLAYER_WAVE_RAID_COMPONENT_BLOCK"
        or bootstrap.get("players_sampled_independently") is not False
        or bootstrap.get("component_crossing_allowed") is not False
        or bootstrap.get("rng") != "SHA256_KEYED_COUNTER"
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            "background manifest weakened block-bootstrap isolation"
        )
    contamination_contract = _mapping(
        document.get("contamination_contract"), "manifest.contamination_contract"
    )
    if dict(contamination_contract) != _expected_contamination_contract():
        raise ChronicleTeamBackgroundGeneratorError(
            "background manifest contamination contract is stale or unsupported"
        )
    blocks = _array(document.get("blocks"), "manifest.blocks")
    if not blocks:
        raise ChronicleTeamBackgroundGeneratorError("background manifest has no blocks")
    if any(_mapping(row, "manifest.block").get("voting_eligible") is not False for row in blocks):
        raise ChronicleTeamBackgroundGeneratorError("a background block became voting")


def _validate_background_block(document: Mapping[str, Any]) -> str:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != BLOCK_KIND
    ):
        raise ChronicleTeamBackgroundGeneratorError("invalid background block schema")
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ChronicleTeamBackgroundGeneratorError(
            "background block implementation revision is stale or unsupported"
        )
    content_sha = _verify_content_address(document, "background block")
    contamination = _mapping(document.get("contamination"), "block.contamination")
    status = _text(contamination.get("status"), "block.contamination.status")
    if status not in ALL_CONTAMINATION_STATUSES:
        raise ChronicleTeamBackgroundGeneratorError(
            f"unsupported background block contamination status {status!r}"
        )
    eligibility = _mapping(document.get("eligibility"), "block.eligibility")
    if eligibility.get("voting_eligible") is not False:
        raise ChronicleTeamBackgroundGeneratorError("a background block became voting")
    if (
        eligibility.get("training_eligible") is True
        and status not in TRAINING_CONTAMINATION_STATUSES
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            "a contaminated background block became training eligible"
        )
    boundary = _mapping(document.get("claim_boundary"), "block.claim_boundary")
    if boundary.get("comparison_ready") is not False:
        raise ChronicleTeamBackgroundGeneratorError(
            "a background block became comparison ready"
        )
    return content_sha


def build_chronicle_team_background_generator(
    *,
    team_model_manifest_path: str | Path = DEFAULT_TEAM_MODEL_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
) -> ChronicleTeamBackgroundGeneratorResult:
    """Compile content-addressed whole-wave component bootstrap blocks."""

    manifest_path = Path(team_model_manifest_path).expanduser().resolve()
    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    team_model = _load_json(manifest_path, "team model manifest")
    team_model_content_sha = _validate_team_model_manifest(team_model)
    node_to_component = _node_component_index(team_model)

    builds: list[_BlockBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        entries = _array(team_model.get("partitions"), "team_model.partitions")
        for entry_index, raw_entry in enumerate(entries):
            entry = _mapping(raw_entry, f"team_model.partitions[{entry_index}]")
            source_path = _resolve_relative(
                manifest_path,
                entry.get("partition"),
                f"team_model.partitions[{entry_index}].partition",
            )
            expected_compressed = _text(
                entry.get("compressed_file_sha256"),
                f"team_model.partitions[{entry_index}].compressed_file_sha256",
            )
            if _sha256_file(source_path) != expected_compressed:
                raise ChronicleTeamBackgroundGeneratorError(
                    f"team-model compressed hash mismatch for {source_path}"
                )
            if source_path.stat().st_size != _integer(
                entry.get("compressed_size_bytes"),
                f"team_model.partitions[{entry_index}].compressed_size_bytes",
            ):
                raise ChronicleTeamBackgroundGeneratorError(
                    f"team-model compressed size mismatch for {source_path}"
                )
            expected_logical = _text(
                entry.get("logical_content_sha256"),
                f"team_model.partitions[{entry_index}].logical_content_sha256",
            )
            logical = hashlib.sha256()
            with gzip.open(source_path, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    logical.update(chunk)
            if logical.hexdigest() != expected_logical:
                raise ChronicleTeamBackgroundGeneratorError(
                    f"team-model logical hash mismatch for {source_path}"
                )
            wave_count = 0
            for wave in _iter_partition_waves(source_path):
                document = _build_block_document(
                    wave,
                    node_to_component,
                    source_partition_sha256=expected_logical,
                )
                identity = _wave_identity(document.get("source_wave"), "block.source_wave")
                builds.append(
                    _write_gzip_document(
                        document,
                        output_dir,
                        f"{identity['instance_id']}.{identity['wave_ordinal']}.team-background",
                    )
                )
                wave_count += 1
            if wave_count != _integer(
                entry.get("wave_count"),
                f"team_model.partitions[{entry_index}].wave_count",
            ):
                raise ChronicleTeamBackgroundGeneratorError(
                    f"team-model wave count mismatch for {source_path}"
                )

        if not builds:
            raise ChronicleTeamBackgroundGeneratorError(
                "team model produced no background blocks"
            )
        builds.sort(
            key=lambda build: (
                build.manifest_entry["component_id"],
                build.manifest_entry["source_wave"]["instance_id"],
                build.manifest_entry["source_wave"]["encounter_id"],
                build.manifest_entry["source_wave"]["wave_ordinal"],
                build.manifest_entry["block_id"],
            )
        )
        grouped: dict[str, list[JSONMap]] = defaultdict(list)
        for build in builds:
            grouped[str(build.manifest_entry["component_id"])].append(
                build.manifest_entry
            )
        component_rows = []
        for component_id in sorted(grouped):
            rows = grouped[component_id]
            component_rows.append(
                {
                    "component_id": component_id,
                    "block_ids": [row["block_id"] for row in rows],
                    "training_eligible_block_ids": [
                        row["block_id"]
                        for row in rows
                        if row["training_eligible"] is True
                    ],
                    "focal_player_guids": sorted(
                        {
                            guid
                            for row in rows
                            for guid in row["player_guids"]
                        },
                        key=str.casefold,
                    ),
                }
            )
        manifest_core = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": MANIFEST_KIND,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "input": {
                "team_model_manifest_path": _portable_path(manifest_path),
                "team_model_manifest_file_sha256": _sha256_file(manifest_path),
                "team_model_manifest_content_sha256": team_model_content_sha,
                "team_model_schema": TEAM_MODEL_SCHEMA,
                "team_model_partitions_scanned_once": True,
                "normalized_or_raw_chronicle_opened": False,
            },
            "bootstrap_contract": {
                "sampling_unit": "WHOLE_PLAYER_WAVE_RAID_COMPONENT_BLOCK",
                "players_sampled_independently": False,
                "events_sampled_independently": False,
                "component_crossing_allowed": False,
                "with_replacement_across_draws": True,
                "rng": "SHA256_KEYED_COUNTER",
                "draw_key_fields": [
                    "manifest_content_sha256",
                    "component_id",
                    "focal_player_guid",
                    "seed",
                    "draw_index",
                ],
            },
            "focal_loo_contract": {
                "remove": sorted(FOCAL_REMOVAL_KINDS),
                "unattributed_separate": True,
                "unknown_owner_is_never_guessed": True,
            },
            "runtime_contract": {
                "schedule_time": "strict historical wave-relative milliseconds",
                "damage_lane": "numeric DMG only",
                "dead_target_future_hits_cancelled": True,
                "retarget_default": "RETARGET_UNIDENTIFIED_NONVOTING",
                "no_retarget_control_available": True,
            },
            "contamination_contract": _expected_contamination_contract(),
            "specialization_contract": {
                "fury_and_arms_merged": False,
                "arms_as_fury_allowed": False,
                "arms_historical_lane": "COMMON_ACTION_DIAGNOSTIC_ONLY_NONVOTING",
                "team_background_may_retain_observed_teammates_of_all_specs": True,
            },
            "claim_boundary": {
                "role": "BOOTSTRAP_TRAINING_DIAGNOSTIC_NONVOTING",
                "voting_eligible": False,
                "comparison_ready": False,
                "learned_team_response_present": False,
                "learned_retarget_present": False,
                "dynamic_bridge_admitted": False,
                "blockers": list(CLAIM_BLOCKERS),
            },
            "components": component_rows,
            "blocks": [build.manifest_entry for build in builds],
            "summary": {
                "component_count": len(component_rows),
                "block_count": len(builds),
                "training_eligible_block_count": sum(
                    build.manifest_entry["training_eligible"] is True
                    for build in builds
                ),
                "voting_eligible_block_count": 0,
                "comparison_ready": False,
            },
        }
        manifest = {
            **manifest_core,
            "content_address": {
                "algorithm": "sha256",
                "scope": "canonical JSON document excluding content_address",
                "sha256": _sha256_json(manifest_core),
            },
        }
        validate_background_generator_manifest(manifest)
        addressed = output_dir / (
            f"chronicle_team_background_generator_v1.{manifest['content_address']['sha256']}.json"
        )
        stable = output_dir / "manifest.json"
        addressed_temporary = _atomic_json_temporary(addressed, manifest)
        stable_temporary = _atomic_json_temporary(stable, manifest)
        for build in builds:
            _commit_or_reuse(build.temporary_path, build.final_path)
        _commit_or_reuse(addressed_temporary, addressed)
        addressed_temporary = None
        stable_temporary.replace(stable)
        stable_temporary = None
        return ChronicleTeamBackgroundGeneratorResult(
            manifest=stable,
            content_addressed_manifest=addressed,
            blocks=tuple(build.final_path for build in builds),
            component_count=len(component_rows),
            block_count=len(builds),
            training_eligible_block_count=sum(
                build.manifest_entry["training_eligible"] is True for build in builds
            ),
        )
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)


def _load_generator_manifest(path: str | Path) -> tuple[Path, JSONMap, str]:
    resolved = Path(path).expanduser().resolve()
    document = _load_json(resolved, "background generator manifest")
    validate_background_generator_manifest(document)
    return resolved, document, _verify_content_address(
        document, "background generator manifest"
    )


def _load_block(
    manifest_path: Path, entry: Mapping[str, Any]
) -> tuple[JSONMap, str]:
    path = _resolve_relative(manifest_path, entry.get("block"), "block.path")
    if _sha256_file(path) != _text(
        entry.get("compressed_file_sha256"), "block.compressed_file_sha256"
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background block compressed hash mismatch: {path}"
        )
    if path.stat().st_size != _integer(
        entry.get("compressed_size_bytes"), "block.compressed_size_bytes"
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background block size mismatch: {path}"
        )
    document = _load_json(path, "background block")
    content_sha = _validate_background_block(document)
    if content_sha != _text(
        entry.get("document_content_sha256"), "block.document_content_sha256"
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background block content mismatch: {path}"
        )
    if content_sha != _text(entry.get("block_id"), "block.block_id"):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background block identity mismatch: {path}"
        )
    if document.get("component_id") != entry.get("component_id"):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background block component mismatch: {path}"
        )
    eligibility = _mapping(document.get("eligibility"), "block.eligibility")
    if (
        eligibility.get("training_eligible") is True
    ) != (entry.get("training_eligible") is True):
        raise ChronicleTeamBackgroundGeneratorError(
            f"background block eligibility mismatch: {path}"
        )
    return document, content_sha


def _keyed_index(key: Mapping[str, Any], population: int) -> tuple[int, str]:
    if population <= 0:
        raise ChronicleTeamBackgroundGeneratorError("empty bootstrap population")
    digest = _sha256_json(key)
    return int(digest, 16) % population, digest


def _normalized_target_mapping(
    source_targets: Sequence[Any],
    *,
    mode: str,
    explicit: Mapping[str, str] | None,
) -> tuple[dict[str, str | None], str]:
    if mode not in RETARGET_MODES:
        raise ChronicleTeamBackgroundGeneratorError(
            f"unsupported retarget mode {mode!r}"
        )
    targets = [_text(value, "source target GUID") for value in source_targets]
    target_keys: set[str] = set()
    for target in targets:
        key = _guid_key(target)
        if key in target_keys:
            raise ChronicleTeamBackgroundGeneratorError(
                f"source target GUID is duplicated case-insensitively: {target}"
            )
        target_keys.add(key)
    result: dict[str, str | None] = {}
    if mode == EXPLICIT_TARGET_MAPPING:
        if explicit is None:
            raise ChronicleTeamBackgroundGeneratorError(
                "EXPLICIT_TARGET_MAPPING requires target_mapping"
            )
        supplied: dict[str, str] = {}
        for raw_key, raw_value in explicit.items():
            source_key = _guid_key(_text(raw_key, "mapped source target GUID"))
            mapped_value = _text(raw_value, "mapped target GUID")
            if source_key in supplied:
                raise ChronicleTeamBackgroundGeneratorError(
                    "explicit target mapping duplicates a source GUID "
                    "case-insensitively"
                )
            supplied[source_key] = mapped_value
        for target in targets:
            mapped = supplied.get(_guid_key(target))
            if mapped is None:
                raise ChronicleTeamBackgroundGeneratorError(
                    f"explicit target mapping lacks {target}"
                )
            result[target] = mapped
        return result, "EXPLICIT_OBSERVED_CALLER_MAPPING"
    if explicit is not None:
        raise ChronicleTeamBackgroundGeneratorError(
            "target_mapping is only accepted with EXPLICIT_TARGET_MAPPING"
        )
    if mode == NO_RETARGET_CONTROL:
        return {target: target for target in targets}, "IDENTITY_NO_RETARGET_CONTROL"
    return {target: None for target in targets}, "RETARGET_UNIDENTIFIED_NONVOTING"


def _resolve_runtime_target(
    historical_target: str | None,
    *,
    event_kind: str,
    mode: str,
    mapping_by_key: Mapping[str, str | None],
) -> tuple[str | None, str]:
    if historical_target is None:
        return None, "HISTORICAL_TARGET_ABSENT"
    key = _guid_key(historical_target)
    if key in mapping_by_key:
        mapped = mapping_by_key[key]
        if mapped is None:
            return None, "RETARGET_UNIDENTIFIED"
        if mode == NO_RETARGET_CONTROL:
            return mapped, "IDENTITY_NO_RETARGET_CONTROL"
        return mapped, "EXPLICIT_TARGET_MAPPING"
    if mode == NO_RETARGET_CONTROL:
        return historical_target, "IDENTITY_NO_RETARGET_CONTROL_OUTSIDE_HOSTILE_SET"
    if mode == RETARGET_UNIDENTIFIED:
        return None, "RETARGET_UNIDENTIFIED"
    if event_kind == "DAMAGE":
        raise ChronicleTeamBackgroundGeneratorError(
            f"damage event target {historical_target} is absent from explicit mapping"
        )
    return None, "NONHOSTILE_ACTION_TARGET_NOT_MAPPED"


def draw_background_schedule(
    *,
    generator_manifest_path: str | Path,
    component_id: str,
    focal_player_guid: str,
    seed: int,
    draw_index: int,
    retarget_mode: str = RETARGET_UNIDENTIFIED,
    target_mapping: Mapping[str, str] | None = None,
) -> JSONMap:
    """Draw one whole correlated player-wave block with focal LOO applied."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ChronicleTeamBackgroundGeneratorError("seed must be a nonnegative integer")
    if isinstance(draw_index, bool) or not isinstance(draw_index, int) or draw_index < 0:
        raise ChronicleTeamBackgroundGeneratorError(
            "draw_index must be a nonnegative integer"
        )
    component = _text(component_id, "component_id")
    focal = _text(focal_player_guid, "focal_player_guid")
    manifest_path, manifest, manifest_sha = _load_generator_manifest(
        generator_manifest_path
    )
    candidates = [
        dict(_mapping(raw, "manifest.block"))
        for raw in _array(manifest.get("blocks"), "manifest.blocks")
        if _mapping(raw, "manifest.block").get("component_id") == component
        and _mapping(raw, "manifest.block").get("training_eligible") is True
        and _guid_key(focal)
        in {
            _guid_key(value)
            for value in _array(
                _mapping(raw, "manifest.block").get("player_guids"),
                "manifest.block.player_guids",
            )
        }
    ]
    candidates.sort(key=lambda row: str(row["block_id"]))
    if not candidates:
        raise ChronicleTeamBackgroundGeneratorError(
            f"no clean training block for focal {focal} in component {component}"
        )
    draw_key = {
        "schema": SCHEMA,
        "purpose": "WHOLE_EPISODE_COMPONENT_BLOCK_BOOTSTRAP",
        "manifest_content_sha256": manifest_sha,
        "component_id": component,
        "focal_player_guid": focal.casefold(),
        "seed": seed,
        "draw_index": draw_index,
    }
    selected_index, rng_key_sha = _keyed_index(draw_key, len(candidates))
    selected_entry = candidates[selected_index]
    block, block_sha = _load_block(manifest_path, selected_entry)
    if block.get("component_id") != component:
        raise ChronicleTeamBackgroundGeneratorError(
            "selected block crossed the requested component"
        )

    source_targets = _array(block.get("target_guids"), "block.target_guids")
    mapping, mapping_status = _normalized_target_mapping(
        source_targets, mode=retarget_mode, explicit=target_mapping
    )
    mapping_by_key = {_guid_key(key): value for key, value in mapping.items()}
    focal_track: Mapping[str, Any] | None = None
    schedule: list[JSONMap] = []
    excluded_event_count = 0
    excluded_damage = 0.0
    unresolved_damage_target_count = 0
    for raw_track in _array(block.get("player_tracks"), "block.player_tracks"):
        track = _mapping(raw_track, "player_track")
        player = _mapping(track.get("player"), "player_track.player")
        player_guid = _text(player.get("guid"), "player_track.player.guid")
        events = _array(track.get("events"), "player_track.events")
        if _guid_key(player_guid) == _guid_key(focal):
            focal_track = track
            excluded_event_count += len(events)
            excluded_damage += sum(
                float(event.get("amount") or 0)
                for raw_event in events
                if (event := _mapping(raw_event, "focal event")).get("kind")
                == "DAMAGE"
                and event.get("amount") is not None
            )
            continue
        for raw_event in events:
            event = deepcopy(dict(_mapping(raw_event, "player event")))
            historical_target = _optional_text(event.get("target_guid"))
            runtime_target, runtime_target_status = _resolve_runtime_target(
                historical_target,
                event_kind=_text(event.get("kind"), "player event.kind"),
                mode=retarget_mode,
                mapping_by_key=mapping_by_key,
            )
            if event.get("kind") == "DAMAGE" and runtime_target is None:
                unresolved_damage_target_count += 1
            schedule.append(
                {
                    **event,
                    "actor_lane": "ATTRIBUTED_TEAMMATE",
                    "source_player": {
                        "guid": player_guid,
                        "name": _optional_text(player.get("name")),
                        "hero_class": _optional_text(player.get("hero_class")),
                        "specialization_partition": track.get(
                            "specialization_partition"
                        ),
                    },
                    "runtime_target_guid": runtime_target,
                    "runtime_target_status": runtime_target_status,
                }
            )
    if focal_track is None:
        raise ChronicleTeamBackgroundGeneratorError(
            "selected block lost the focal player's whole episode"
        )
    unattributed = _mapping(
        block.get("unattributed_branch"), "block.unattributed_branch"
    )
    unattributed_count = 0
    for raw_event in _array(unattributed.get("events"), "unattributed.events"):
        event = deepcopy(dict(_mapping(raw_event, "unattributed event")))
        historical_target = _optional_text(event.get("target_guid"))
        runtime_target, runtime_target_status = _resolve_runtime_target(
            historical_target,
            event_kind=_text(event.get("kind"), "unattributed event.kind"),
            mode=retarget_mode,
            mapping_by_key=mapping_by_key,
        )
        if event.get("kind") == "DAMAGE" and runtime_target is None:
            unresolved_damage_target_count += 1
        schedule.append(
            {
                **event,
                "actor_lane": "UNATTRIBUTED_EXPLICIT_UNKNOWN",
                "source_player": None,
                "runtime_target_guid": runtime_target,
                "runtime_target_status": runtime_target_status,
            }
        )
        unattributed_count += 1
    schedule.sort(
        key=lambda row: (
            _integer(row.get("relative_ms"), "schedule.relative_ms"),
            tuple(
                _integer(value, "schedule.historical_order_key")
                for value in _array(
                    row.get("historical_order_key"),
                    "schedule.historical_order_key",
                )
            ),
            str(row.get("event_sha256")),
        )
    )
    for index, event in enumerate(schedule):
        event["schedule_index"] = index

    focal_player = _mapping(focal_track.get("player"), "focal_track.player")
    focal_partition = _text(
        focal_track.get("specialization_partition"),
        "focal_track.specialization_partition",
    )
    retarget_unidentified = (
        retarget_mode == RETARGET_UNIDENTIFIED
        or unresolved_damage_target_count > 0
    )
    draw_status = (
        "RETARGET_UNIDENTIFIED_NONVOTING"
        if retarget_unidentified
        else "BOOTSTRAP_DIAGNOSTIC_NONVOTING"
    )
    pairing_core = {
        "generator_manifest_content_sha256": manifest_sha,
        "component_id": component,
        "block_id": block_sha,
        "focal_player_guid": focal.casefold(),
        "seed": seed,
        "draw_index": draw_index,
        "rng_key_sha256": rng_key_sha,
        "retarget_mode": retarget_mode,
        "target_mapping": mapping,
    }
    draw_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": DRAW_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": draw_status,
        "component_id": component,
        "source_block": {
            "block_id": block_sha,
            "source_wave": deepcopy(block.get("source_wave")),
            "contamination": deepcopy(block.get("contamination")),
            "training_eligible": _mapping(
                block.get("eligibility"), "block.eligibility"
            ).get("training_eligible")
            is True,
            "whole_wave_selected": True,
            "players_sampled_independently": False,
        },
        "draw_identity": {
            **pairing_core,
            "pairing_sha256": _sha256_json(pairing_core),
            "population_size": len(candidates),
            "selected_index": selected_index,
            "rng": "SHA256_KEYED_COUNTER",
        },
        "focal_leave_one_out": {
            "player_guid": focal,
            "player_name": _optional_text(focal_player.get("name")),
            "specialization_partition": focal_partition,
            "historical_warrior_lane": focal_track.get("historical_warrior_lane"),
            "removed_attribution_kinds": sorted(FOCAL_REMOVAL_KINDS),
            "excluded_whole_episode": True,
            "excluded_event_count": excluded_event_count,
            "excluded_numeric_damage": _render_number(excluded_damage),
            "arms_treated_as_fury": False,
        },
        "target_mapping": {
            "mode": retarget_mode,
            "status": mapping_status,
            "mapping": mapping,
            "inference_used": False,
            "unresolved_damage_target_count": unresolved_damage_target_count,
        },
        "schedule": schedule,
        "unattributed_branch": {
            "status": "EXPLICIT_UNKNOWN_NONVOTING",
            "event_count": unattributed_count,
            "retained_in_schedule": True,
            "owner_inference_used": False,
        },
        "runtime_contract": {
            "historical_relative_order_strict": True,
            "candidate_background_tie_break": "background before candidate at equal relative_ms",
            "dead_target_future_damage_cancelled": True,
            "unparsed_damage_not_applied": True,
        },
        "claim_boundary": {
            "role": "SEEDED_BLOCK_BOOTSTRAP_DIAGNOSTIC_NONVOTING",
            "voting_eligible": False,
            "comparison_ready": False,
            "runtime_schedule_eligible": not retarget_unidentified,
            "blockers": list(CLAIM_BLOCKERS)
            + (
                ["RETARGET_UNIDENTIFIED"]
                if retarget_unidentified
                else []
            ),
        },
    }
    return {
        **draw_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(draw_core),
        },
    }


def validate_background_draw(document: Mapping[str, Any]) -> None:
    if (
        document.get("schema") != SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != DRAW_KIND
    ):
        raise ChronicleTeamBackgroundGeneratorError("invalid background draw schema")
    if document.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ChronicleTeamBackgroundGeneratorError(
            "background draw implementation revision is stale or unsupported"
        )
    _verify_content_address(document, "background draw")
    boundary = _mapping(document.get("claim_boundary"), "draw.claim_boundary")
    if (
        boundary.get("voting_eligible") is not False
        or boundary.get("comparison_ready") is not False
    ):
        raise ChronicleTeamBackgroundGeneratorError(
            "background draw widened its claim boundary"
        )
    loo = _mapping(document.get("focal_leave_one_out"), "draw.focal_leave_one_out")
    if (
        sorted(_array(loo.get("removed_attribution_kinds"), "loo.removed_attribution_kinds"))
        != sorted(FOCAL_REMOVAL_KINDS)
        or loo.get("excluded_whole_episode") is not True
        or loo.get("arms_treated_as_fury") is not False
    ):
        raise ChronicleTeamBackgroundGeneratorError("draw weakened focal LOO")


def write_background_draw(
    document: Mapping[str, Any], output_directory: str | Path
) -> Path:
    """Persist one draw under its immutable content address."""

    validate_background_draw(document)
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    digest = _text(
        _mapping(document.get("content_address"), "draw.content_address").get(
            "sha256"
        ),
        "draw.content_address.sha256",
    )
    final = output / f"chronicle_team_background_draw_v1.{digest}.json"
    temporary = _atomic_json_temporary(final, document)
    _commit_or_reuse(temporary, final)
    return final


def replay_background_schedule(
    *,
    draw: Mapping[str, Any],
    target_health: Mapping[str, int | float],
    candidate_damage_events: Sequence[Mapping[str, Any]] = (),
) -> JSONMap:
    """Apply a draw on a dynamic multi-target health clock.

    Background order is historical.  Candidate rows use their supplied order
    and are deterministically placed after background rows at the same
    ``relative_ms``.  The first row crossing target health marks death; every
    later hit on that target is retained as a cancelled diagnostic event.
    """

    validate_background_draw(draw)
    health_by_key: dict[str, float] = {}
    health_display: dict[str, str] = {}
    for raw_guid, raw_health in target_health.items():
        guid = _text(raw_guid, "target_health target GUID")
        key = _guid_key(guid)
        if key in health_by_key:
            raise ChronicleTeamBackgroundGeneratorError(
                f"duplicate target health GUID {guid}"
            )
        parsed = _nonnegative_number(raw_health, f"target_health[{guid}]")
        if parsed <= 0:
            raise ChronicleTeamBackgroundGeneratorError(
                f"target health must be positive for {guid}"
            )
        health_by_key[key] = parsed
        health_display[key] = guid

    merged: list[tuple[tuple[Any, ...], JSONMap]] = []
    for index, raw in enumerate(_array(draw.get("schedule"), "draw.schedule")):
        event = deepcopy(dict(_mapping(raw, f"draw.schedule[{index}]")))
        relative_ms = _integer(event.get("relative_ms"), "schedule.relative_ms")
        merged.append(((relative_ms, 0, index), {**event, "stream": "BACKGROUND"}))
    for index, raw in enumerate(candidate_damage_events):
        event = _mapping(raw, f"candidate_damage_events[{index}]")
        relative_ms = _integer(
            event.get("relative_ms"), f"candidate_damage_events[{index}].relative_ms"
        )
        if relative_ms < 0:
            raise ChronicleTeamBackgroundGeneratorError(
                f"candidate_damage_events[{index}].relative_ms must be nonnegative"
            )
        target_guid = _text(
            event.get("target_guid"),
            f"candidate_damage_events[{index}].target_guid",
        )
        amount = _render_number(
            _nonnegative_number(
                event.get("amount"), f"candidate_damage_events[{index}].amount"
            )
        )
        candidate_core = {
            "relative_ms": relative_ms,
            "kind": "DAMAGE",
            "event_type": "CANDIDATE_DAMAGE",
            "target_guid": target_guid,
            "runtime_target_guid": target_guid,
            "amount": amount,
            "actor_lane": "CANDIDATE_POLICY",
            "candidate_event_id": _optional_text(event.get("event_id"))
            or f"candidate-{index}",
            "stream": "CANDIDATE",
        }
        merged.append(((relative_ms, 1, index), candidate_core))
    merged.sort(key=lambda item: item[0])

    cumulative: Counter[str] = Counter()
    background_damage: Counter[str] = Counter()
    candidate_damage: Counter[str] = Counter()
    deaths: dict[str, JSONMap] = {}
    emitted: list[JSONMap] = []
    cancelled: list[JSONMap] = []
    processed: list[JSONMap] = []
    unresolved_retarget = False
    for runtime_index, (runtime_order, event) in enumerate(merged):
        core = {
            **event,
            "runtime_order_key": list(runtime_order),
            "runtime_index": runtime_index,
        }
        if event.get("kind") == "ACTION":
            rendered = {**core, "runtime_status": "EMITTED_ACTION"}
            emitted.append(rendered)
            processed.append(rendered)
            continue
        target = _optional_text(event.get("runtime_target_guid"))
        amount_raw = event.get("amount")
        reason: str | None = None
        if target is None:
            reason = "RETARGET_UNIDENTIFIED"
            unresolved_retarget = True
        elif amount_raw is None:
            reason = "UNPARSED_DAMAGE_AMOUNT"
        elif _guid_key(target) not in health_by_key:
            reason = "TARGET_HEALTH_UNKNOWN"
        elif _guid_key(target) in deaths:
            reason = "TARGET_ALREADY_DEAD"
        if reason is not None:
            rendered = {
                **core,
                "runtime_status": "CANCELLED",
                "cancellation_reason": reason,
            }
            cancelled.append(rendered)
            processed.append(rendered)
            continue
        key = _guid_key(target)
        amount = _nonnegative_number(amount_raw, "runtime damage amount")
        cumulative[key] += amount
        if event.get("stream") == "BACKGROUND":
            background_damage[key] += amount
        else:
            candidate_damage[key] += amount
        rendered = {
            **core,
            "runtime_status": "EMITTED_DAMAGE",
            "cumulative_damage_after": _render_number(float(cumulative[key])),
            "target_health": _render_number(health_by_key[key]),
        }
        emitted.append(rendered)
        processed.append(rendered)
        if cumulative[key] >= health_by_key[key]:
            deaths[key] = {
                "target_guid": health_display[key],
                "relative_ms": _integer(event.get("relative_ms"), "event.relative_ms"),
                "runtime_index": runtime_index,
                "killing_stream": event.get("stream"),
                "cumulative_damage": _render_number(float(cumulative[key])),
                "target_health": _render_number(health_by_key[key]),
            }

    source_draw_sha = _text(
        _mapping(draw.get("content_address"), "draw.content_address").get("sha256"),
        "draw.content_address.sha256",
    )
    replay_status = (
        "RETARGET_UNIDENTIFIED_NONVOTING"
        if unresolved_retarget
        or draw.get("status") == "RETARGET_UNIDENTIFIED_NONVOTING"
        else "DYNAMIC_SCHEDULE_DIAGNOSTIC_NONVOTING"
    )
    result_core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": REPLAY_KIND,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": replay_status,
        "source_draw_content_sha256": source_draw_sha,
        "target_health": {
            health_display[key]: _render_number(value)
            for key, value in sorted(health_by_key.items())
        },
        "processed_events": processed,
        "emitted_events": emitted,
        "cancelled_events": cancelled,
        "death_clocks": [deaths[key] for key in sorted(deaths)],
        "damage_totals": {
            health_display[key]: {
                "candidate": _render_number(float(candidate_damage[key])),
                "background": _render_number(float(background_damage[key])),
                "combined": _render_number(float(cumulative[key])),
            }
            for key in sorted(health_by_key)
        },
        "runtime_contract": {
            "dead_target_future_damage_cancelled": True,
            "target_deaths_depend_on_candidate_plus_background": True,
            "background_historical_relative_order_preserved": True,
            "unknown_retarget_guessed": False,
        },
        "claim_boundary": {
            "voting_eligible": False,
            "comparison_ready": False,
            "dynamic_bridge_admitted": False,
            "blockers": list(CLAIM_BLOCKERS),
        },
    }
    return {
        **result_core,
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha256_json(result_core),
        },
    }


def iter_runtime_schedule(replay: Mapping[str, Any]) -> Iterator[JSONMap]:
    """Yield emitted runtime action/damage rows in deterministic order."""

    if (
        replay.get("schema") != SCHEMA
        or replay.get("schema_version") != SCHEMA_VERSION
        or replay.get("kind") != REPLAY_KIND
    ):
        raise ChronicleTeamBackgroundGeneratorError("invalid runtime replay document")
    if replay.get("implementation_revision") != IMPLEMENTATION_REVISION:
        raise ChronicleTeamBackgroundGeneratorError(
            "runtime replay implementation revision is stale or unsupported"
        )
    _verify_content_address(replay, "runtime replay")
    for raw in _array(replay.get("emitted_events"), "replay.emitted_events"):
        yield deepcopy(dict(_mapping(raw, "replay emitted event")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile component-block Chronicle team background schedules."
    )
    parser.add_argument(
        "--team-model-manifest", type=Path, default=DEFAULT_TEAM_MODEL_MANIFEST
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_chronicle_team_background_generator(
            team_model_manifest_path=args.team_model_manifest,
            output_directory=args.output_dir,
        )
    except ChronicleTeamBackgroundGeneratorError as error:
        print(f"ERROR: {error}", file=__import__("sys").stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ChronicleTeamBackgroundGeneratorError",
    "ChronicleTeamBackgroundGeneratorResult",
    "EXPLICIT_TARGET_MAPPING",
    "NO_RETARGET_CONTROL",
    "RETARGET_UNIDENTIFIED",
    "SCHEMA",
    "build_chronicle_team_background_generator",
    "draw_background_schedule",
    "iter_runtime_schedule",
    "replay_background_schedule",
    "validate_background_draw",
    "validate_background_generator_manifest",
    "write_background_draw",
]
