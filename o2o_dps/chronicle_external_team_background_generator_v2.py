"""Compile External Chronicle team-wave evidence into dynamic backgrounds.

The generator preserves one complete historical wave as the bootstrap unit.
It never treats an observed death, a final damage total, or an unattributed
event as an online policy feature.  Runtime schedules contain only positive
official ``DMG`` events with exact direct/owner/controller player attribution
and an explicitly voting hostile-creature target.  Everything else remains a
named diagnostic lane.

Artifacts and draws are descriptive/nonvoting.  ``load_dynamic_v1`` adapters
also require an independently supplied target-health hypothesis; observed
background damage is never promoted to initial target health.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Any, Mapping, Sequence

from . import chronicle_external_team_wave_model_v2 as model_v2
from .sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
)


JSONMap = dict[str, Any]
SCHEMA = "chronicle_external_team_background_generator/v2"
PARTITION_RECORD_SCHEMA = "chronicle_external_team_background_wave/v2"
KIND = "chronicle_external_team_background_generator_manifest"
DRAW_SCHEMA = "chronicle_external_team_background_draw/v2"
ADAPTER_SCHEMA = "chronicle_external_team_background_load_dynamic_adapter/v1"
DYNAMIC_SCHEMA = "o2o_dynamic_team_background/v1"
STATUS = "DESCRIPTIVE_NONVOTING_NOT_COMPARISON"
IMPLEMENTATION_REVISION = (
    "v2.2_external_component_receipt_cohort_nontraining_adapter"
)
MAX_WORKERS = 32

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEAM_MODEL_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_wave_model"
    / "v2"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "chronicle_external_team_background_generator"
    / "v2"
)

PLAYER_ATTRIBUTION_KINDS = frozenset(
    {
        "DIRECT_FRIENDLY_PLAYER",
        "EXACT_OFFICIAL_OWNER",
        "EXACT_OFFICIAL_CONTROLLER",
    }
)
TRAINING_CONTAMINATION_LABELS = frozenset(
    {"POSTFIX_KNOWN_CLEAN", "NO_KNOWN_RULE_MATCH"}
)
NONTRAINING_CONTAMINATION_LABELS = frozenset(
    {
        "SUSPECT_36YD_RANGE_BUG",
        "RANGE_BUG_BOUNDARY_UNCERTAIN_NONVOTING",
        "UNKNOWN_NONVOTING",
    }
)
ALL_CONTAMINATION_LABELS = (
    TRAINING_CONTAMINATION_LABELS | NONTRAINING_CONTAMINATION_LABELS
)
RETARGET_NEXT_ALIVE_CYCLIC = "NEXT_ALIVE_CYCLIC"
RETARGET_REQUIRE_EXPLICIT = "REQUIRE_EXPLICIT"
RETARGET_MODES = frozenset(
    {RETARGET_NEXT_ALIVE_CYCLIC, RETARGET_REQUIRE_EXPLICIT}
)
SAME_TIMESTAMP_ORDER = "BACKGROUND_BEFORE_CANDIDATE"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEALTH_STAT_INDEX = 34


class ChronicleExternalTeamBackgroundGeneratorV2Error(RuntimeError):
    """An input or output violates the External team-background V2 contract."""


@dataclass(frozen=True)
class TargetHealthHypothesisV1:
    """Independent, explicitly non-comparative target-health assumptions."""

    hypothesis_id: str
    provenance: Mapping[str, Any]
    target_health_by_index: Mapping[int, float]


@dataclass(frozen=True)
class ExternalDynamicScheduleAdapterV1:
    """Typed split between bytes sent to Go and scientific provenance."""

    wire_request: Mapping[str, Any]
    dynamic_config: DynamicTeamBackgroundConfigV1
    provenance: Mapping[str, Any]
    comparison_ready: bool = False
    voting_eligible: bool = False

    def as_dict(self) -> JSONMap:
        return {
            "schema": ADAPTER_SCHEMA,
            "status": STATUS,
            "wire_request": deepcopy(dict(self.wire_request)),
            "provenance": deepcopy(dict(self.provenance)),
            "comparison_ready": self.comparison_ready,
            "voting_eligible": self.voting_eligible,
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
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"value is not canonical JSON: {error}"
        ) from error
    return rendered.encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"cannot hash {path}: {error}"
        ) from error
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be a non-empty string"
        )
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be an integer"
        )
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    parsed = _integer(value, label=label)
    if parsed < 0:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be nonnegative"
        )
    return parsed


def _positive_number(value: Any, *, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be a positive finite number"
        )
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be a positive finite number"
        )
    return value


def _sha(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if not _SHA_RE.fullmatch(rendered):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be a lowercase SHA-256"
        )
    return rendered


def _safe_component(value: Any, *, label: str) -> str:
    rendered = _text(value, label=label)
    if not _SAFE_COMPONENT_RE.fullmatch(rendered):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} contains unsafe filename characters"
        )
    return rendered


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    materialized = deepcopy(dict(core))
    materialized["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": _sha256_bytes(_canonical_bytes(core)),
    }
    return materialized


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = _mapping(value.get("content_address"), label=f"{label}.content_address")
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope") != "canonical JSON excluding content_address"
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} has an unsupported content-address contract"
        )
    declared = _sha(address.get("sha256"), label=f"{label}.content_address.sha256")
    core = {key: child for key, child in value.items() if key != "content_address"}
    actual = _sha256_bytes(_canonical_bytes(core))
    if actual != declared:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} content address mismatch"
        )
    return actual


def _load_json(path: Path, *, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be an object"
        )
    return value


def _data_root(path: Path) -> Path:
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.name == "derived":
            return parent.parent
    raise ChronicleExternalTeamBackgroundGeneratorV2Error(
        "manifest must be below an offline_data/derived directory"
    )


def _under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must stay under the model offline_data root"
        )
    return resolved


def _relative(path: Path, root: Path, *, label: str) -> str:
    return _under(path, root, label=label).relative_to(root.resolve()).as_posix()


def _resolve_relative(
    base: Path, relative_value: Any, root: Path, *, label: str
) -> Path:
    relative = Path(_text(relative_value, label=label))
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} must be a safe relative path"
        )
    resolved = _under(base / relative, root, label=label)
    if not resolved.is_file() or resolved.is_symlink():
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} is not a regular file"
        )
    return resolved


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, Mapping):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


@dataclass(frozen=True)
class _InputInstance:
    index: int
    instance_id: str
    entry: Mapping[str, Any]
    partition_path: Path
    component_by_node: Mapping[str, str]
    contamination: Mapping[str, Any]


@dataclass(frozen=True)
class _InputClosure:
    manifest: Mapping[str, Any]
    manifest_path: Path
    addressed_path: Path
    data_root: Path
    content_sha256: str
    file_sha256: str
    component_by_node: Mapping[str, str]
    instances: tuple[_InputInstance, ...]
    cohort_receipt_binding: Mapping[str, Any]


def _component_index(manifest: Mapping[str, Any]) -> dict[str, str]:
    graph = _mapping(manifest.get("split_graph"), label="model split_graph")
    if (
        graph.get("required_split_unit") != "connected component"
        or graph.get("row_random_split_allowed") is not False
        or graph.get("same_player_or_guild_can_cross_folds") is not False
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model split graph does not enforce component isolation"
        )
    result: dict[str, str] = {}
    for raw in _array(graph.get("node_to_component"), label="node_to_component"):
        row = _mapping(raw, label="node_to_component row")
        node = _text(row.get("node_id"), label="node_id")
        component = _sha(row.get("component_id"), label="component_id")
        previous = result.setdefault(node, component)
        if previous != component:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "split node maps to multiple components"
            )
    if not result:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model split graph has no component mapping"
        )
    return result


def _load_input_closure(path_value: str | Path) -> _InputClosure:
    try:
        manifest, stable = model_v2.load_external_team_wave_model_manifest(path_value)
    except model_v2.ChronicleExternalTeamWaveModelV2Error as error:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(str(error)) from error
    if (
        manifest.get("schema") != model_v2.SCHEMA
        or manifest.get("implementation_revision") != model_v2.IMPLEMENTATION_REVISION
        or manifest.get("status") != model_v2.STATUS
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "input is not the supported External team-wave V2 revision"
        )
    content_sha = _verify_content_address(manifest, label="model manifest")
    payload = _canonical_bytes(manifest) + b"\n"
    if stable.read_bytes() != payload:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model stable manifest is not canonical"
        )
    addressed = stable.with_name(
        f"chronicle_external_team_wave_model_v2.{content_sha}.manifest.json"
    )
    if (
        not addressed.is_file()
        or addressed.is_symlink()
        or addressed.read_bytes() != payload
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model addressed manifest is missing or differs"
        )
    root = _data_root(stable)
    model_input_closure = _mapping(
        manifest.get("input_closure"), label="model input_closure"
    )
    cohort_receipt_binding = deepcopy(
        dict(
            _mapping(
                model_input_closure.get("cohort_receipt"),
                label="model cohort_receipt binding",
            )
        )
    )
    component_by_node = _component_index(manifest)
    entries = _array(manifest.get("instances"), label="model instances")
    order = _array(manifest.get("instance_order"), label="model instance_order")
    if len(entries) != len(order) or not entries:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model instances and deterministic order differ"
        )
    instances: list[_InputInstance] = []
    for index, raw in enumerate(entries):
        entry = _mapping(raw, label=f"model instances[{index}]")
        _verify_content_address(entry, label="model instance entry")
        instance_id = _safe_component(entry.get("instance_id"), label="instance_id")
        if order[index] != instance_id:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "model instances are not in declared order"
            )
        partition = _mapping(entry.get("partition"), label="model partition")
        contamination = _mapping(
            entry.get("contamination_lane"), label="model contamination lane"
        )
        partition_path = _resolve_relative(
            stable.parent,
            partition.get("path"),
            root,
            label="model partition path",
        )
        instances.append(
            _InputInstance(
                index=index,
                instance_id=instance_id,
                entry=deepcopy(dict(entry)),
                partition_path=partition_path,
                component_by_node=component_by_node,
                contamination=deepcopy(dict(contamination)),
            )
        )
    return _InputClosure(
        manifest=manifest,
        manifest_path=stable,
        addressed_path=addressed,
        data_root=root,
        content_sha256=content_sha,
        file_sha256=_sha256_bytes(payload),
        component_by_node=component_by_node,
        instances=tuple(instances),
        cohort_receipt_binding=cohort_receipt_binding,
    )


def _wave_identity(wave: Mapping[str, Any]) -> JSONMap:
    return {
        "instance_id": _safe_component(
            wave.get("instance_id"), label="wave.instance_id"
        ),
        "encounter_id": _text(wave.get("encounter_id"), label="wave.encounter_id"),
        "encounter_ordinal": _nonnegative_integer(
            wave.get("encounter_ordinal"), label="wave.encounter_ordinal"
        ),
        "wave_id": _text(wave.get("wave_id"), label="wave.wave_id"),
        "wave_ordinal": _nonnegative_integer(
            wave.get("wave_ordinal"), label="wave.wave_ordinal"
        ),
    }


def _wave_key(wave: Mapping[str, Any]) -> tuple[str, str, int, str, int]:
    identity = _wave_identity(wave)
    return (
        identity["instance_id"],
        identity["encounter_id"],
        identity["encounter_ordinal"],
        identity["wave_id"],
        identity["wave_ordinal"],
    )


def _membership_nodes(value: Any, *, label: str) -> list[str]:
    membership = _mapping(value, label=label)
    if (
        membership.get("row_random_split_allowed") is not False
        or membership.get("required_split_unit")
        != "connected component of instance, guild, and player nodes"
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"{label} weakens component isolation"
        )
    nodes = [_text(membership.get("instance_node_id"), label="instance_node_id")]
    player = _optional_text(membership.get("player_node_id"))
    if player:
        nodes.append(player)
    for raw in _array(membership.get("guild_node_ids"), label="guild_node_ids"):
        nodes.append(_text(raw, label="guild_node_id"))
    return nodes


def _wave_component(
    wave: Mapping[str, Any], component_by_node: Mapping[str, str]
) -> str:
    nodes: list[str] = []
    for raw_player in _array(wave.get("players"), label="wave.players"):
        player = _mapping(raw_player, label="wave player")
        nodes.extend(
            _membership_nodes(
                player.get("component_membership"), label="player membership"
            )
        )
    unattributed = _mapping(
        wave.get("unattributed_episode"), label="unattributed_episode"
    )
    nodes.extend(
        _membership_nodes(
            unattributed.get("component_membership"),
            label="unattributed membership",
        )
    )
    components: set[str] = set()
    for node in nodes:
        component = component_by_node.get(node)
        if component is None:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "wave membership references a node absent from the split graph"
            )
        components.add(component)
    if len(components) != 1:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "one whole wave crosses leakage components"
        )
    return next(iter(components))


def _contamination(wave: Mapping[str, Any]) -> JSONMap:
    provenance = _mapping(wave.get("raid_provenance"), label="raid_provenance")
    source = _mapping(provenance.get("contamination"), label="contamination")
    label = _text(source.get("label"), label="contamination.label")
    if label not in ALL_CONTAMINATION_LABELS:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "contamination label is outside the raid-level contract"
        )
    expected_candidate = source.get("candidate_filter_passed")
    if type(expected_candidate) is not bool:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model receipt cohort candidate must be boolean"
        )
    expected_assignment = (
        "TRAINING_CANDIDATE"
        if expected_candidate
        else "DESCRIPTIVE_NONTRAINING"
    )
    if (
        source.get("time_field") != "started_at"
        or source.get("uploaded_at_used") is not False
        or source.get("player_name_used") is not False
        or source.get("raw_label_candidate_filter_passed")
        is not (label in TRAINING_CONTAMINATION_LABELS)
        or source.get("candidate_authority")
        != "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP"
        or source.get("cohort_assignment") != expected_assignment
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model contamination lane was widened or changed"
        )
    receipt_sha = _sha(
        source.get("cohort_receipt_content_sha256"),
        label="cohort receipt content SHA",
    )
    cohort_reason = _text(source.get("cohort_reason"), label="cohort reason")
    return {
        "label": label,
        "training_candidate": expected_candidate,
        "raw_label_candidate_filter_passed": source[
            "raw_label_candidate_filter_passed"
        ],
        "candidate_authority": source["candidate_authority"],
        "cohort_assignment": expected_assignment,
        "cohort_reason": cohort_reason,
        "cohort_receipt_content_sha256": receipt_sha,
        "time_field": "started_at",
        "started_at": source.get("started_at"),
        "uploaded_at_used": False,
        "player_name_used": False,
        "named_player_blacklist_or_weighting_used": False,
        "scope": "raid_instance",
    }


def _trace_order(row: Mapping[str, Any]) -> tuple[int, ...]:
    order = _array(row.get("order_key"), label="trace order_key")
    if len(order) != 5:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "trace order_key must contain five integers"
        )
    return tuple(_nonnegative_integer(value, label="trace order value") for value in order)


def _event_target(event: Mapping[str, Any]) -> tuple[str | None, str, bool]:
    target = _mapping(event.get("target"), label="event target")
    guid = _optional_text(target.get("guid"))
    lane = _text(target.get("lane"), label="event target lane")
    voting = target.get("voting_enemy_target") is True
    if voting and lane != "HOSTILE_CREATURE":
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "voting target is not a hostile creature"
        )
    return guid, lane, voting


def _window_start(wave: Mapping[str, Any]) -> int:
    outcome = _mapping(wave.get("descriptive_outcome"), label="descriptive_outcome")
    if outcome.get("allowed_in_state_before") is not False:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "descriptive outcome was authorized as a prefix feature"
        )
    reconstruction = _mapping(
        outcome.get("reconstruction_binding"), label="reconstruction_binding"
    )
    window = _mapping(reconstruction.get("window"), label="reconstruction window")
    return _nonnegative_integer(
        window.get("start_offset_ms"), label="window.start_offset_ms"
    )


def _event_time_ms(trace: Mapping[str, Any], start_offset_ms: int) -> int:
    anchor = _mapping(trace.get("anchor"), label="trace anchor")
    offset = _nonnegative_integer(anchor.get("offset_ms"), label="anchor.offset_ms")
    if offset < start_offset_ms:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "trace event precedes its wave start"
        )
    return offset - start_offset_ms


def _event_id(
    *, wave: Mapping[str, Any], trace: Mapping[str, Any], actor: str, target: str
) -> str:
    identity = {
        "wave": _wave_identity(wave),
        "trace_index": _nonnegative_integer(
            trace.get("trace_index"), label="trace_index"
        ),
        "order_key": list(_trace_order(trace)),
        "actor_player_guid": actor,
        "target_guid": target,
    }
    return f"chronicle-dmg-{_sha256_bytes(_canonical_bytes(identity))}"


def _target_registry(wave: Mapping[str, Any]) -> tuple[list[JSONMap], dict[str, int]]:
    registry: list[JSONMap] = []
    by_guid: dict[str, int] = {}
    for raw in _array(wave.get("exact_trace"), label="exact_trace"):
        trace = _mapping(raw, label="trace row")
        if trace.get("trace_kind") == "CLASSIFICATION_CONTEXT":
            continue
        event = _mapping(trace.get("event"), label="trace event")
        guid, lane, voting = _event_target(event)
        if not voting or guid is None:
            continue
        if guid not in by_guid:
            index = len(registry)
            by_guid[guid] = index
            registry.append(
                {
                    "target_index": index,
                    "target_guid": guid,
                    "lane": lane,
                    "first_observed_eventmeta_order_key": list(_trace_order(trace)),
                    "initial_health": None,
                    "initial_health_status": "REQUIRES_EXTERNAL_HYPOTHESIS",
                    "observed_background_damage_is_initial_health": False,
                }
            )
    return registry, by_guid


def _runtime_damage_event(
    *,
    wave: Mapping[str, Any],
    trace: Mapping[str, Any],
    target_index: int,
    target_guid: str,
    start_offset_ms: int,
) -> JSONMap:
    event = _mapping(trace.get("event"), label="DMG event")
    attribution = _mapping(event.get("attribution"), label="DMG attribution")
    kind = _text(
        attribution.get("attribution_kind"), label="DMG attribution_kind"
    )
    actor = _text(trace.get("player_guid"), label="DMG trace player_guid")
    if kind not in PLAYER_ATTRIBUTION_KINDS or attribution.get("player_guid") != actor:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "runtime DMG lacks exact player attribution"
        )
    damage = _mapping(event.get("damage"), label="DMG damage")
    if damage.get("amount_source") != "DMG_ONLY":
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "runtime damage amount is not official DMG-only evidence"
        )
    amount = _positive_number(damage.get("amount"), label="DMG amount")
    source = _mapping(event.get("source"), label="DMG source")
    anchor = _mapping(trace.get("anchor"), label="DMG anchor")
    return {
        "time_ms": _event_time_ms(trace, start_offset_ms),
        "target_index": target_index,
        "target_guid": target_guid,
        "event_id": _event_id(
            wave=wave, trace=trace, actor=actor, target=target_guid
        ),
        "damage": amount,
        "actor_player_guid": actor,
        "attribution_kind": kind,
        "source_guid": attribution.get("source_guid"),
        "source_lane": source.get("lane"),
        "trace_index": trace.get("trace_index"),
        "source_eventmeta_order_key": list(_trace_order(trace)),
        "official_message_sha256": anchor.get("official_message_sha256"),
        "evidence_status": "POSITIVE_OFFICIAL_DMG_EXACT_PLAYER_AND_TARGET",
        "runtime_eligible": True,
    }


def _diagnostic_damage(
    *, trace: Mapping[str, Any], start_offset_ms: int, reason: str
) -> JSONMap:
    event = _mapping(trace.get("event"), label="diagnostic DMG event")
    damage = _mapping(event.get("damage"), label="diagnostic DMG damage")
    attribution = _mapping(event.get("attribution"), label="diagnostic attribution")
    target = _mapping(event.get("target"), label="diagnostic target")
    return {
        "time_ms": _event_time_ms(trace, start_offset_ms),
        "trace_index": trace.get("trace_index"),
        "source_eventmeta_order_key": list(_trace_order(trace)),
        "damage": damage.get("amount"),
        "actor_player_guid": trace.get("player_guid"),
        "attribution_kind": attribution.get("attribution_kind"),
        "source_guid": attribution.get("source_guid"),
        "target_guid": target.get("guid"),
        "target_lane": target.get("lane"),
        "reason": reason,
        "runtime_eligible": False,
        "voting_eligible": False,
    }


def _compile_block(
    wave: Mapping[str, Any],
    *,
    component_by_node: Mapping[str, str],
    expected_contamination: Mapping[str, Any],
) -> JSONMap:
    model_v2._validate_model_wave(
        wave,
        instance_id=_wave_identity(_mapping(wave.get("wave"), label="wave"))[
            "instance_id"
        ],
        expected_contamination=expected_contamination,
    )
    model_wave_sha = _verify_content_address(wave, label="model wave")
    identity = _wave_identity(_mapping(wave.get("wave"), label="wave"))
    component = _wave_component(wave, component_by_node)
    contamination = _contamination(wave)
    start_offset = _window_start(wave)
    registry, target_by_guid = _target_registry(wave)
    roster = sorted(
        {
            _text(
                _mapping(
                    _mapping(raw, label="wave player").get("player"),
                    label="player metadata",
                ).get("guid"),
                label="player guid",
            )
            for raw in _array(wave.get("players"), label="wave players")
        }
    )
    runtime: list[JSONMap] = []
    nonruntime: list[JSONMap] = []
    dead_markers: list[JSONMap] = []
    negative: list[JSONMap] = []
    prior_order: tuple[int, ...] | None = None
    for raw_trace in _array(wave.get("exact_trace"), label="exact_trace"):
        trace = _mapping(raw_trace, label="trace row")
        order = _trace_order(trace)
        if prior_order is not None and order <= prior_order:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "exact trace is not strict EventMeta order"
            )
        prior_order = order
        kind = _text(trace.get("trace_kind"), label="trace_kind")
        if kind == "CLASSIFICATION_CONTEXT":
            continue
        event = _mapping(trace.get("event"), label="trace event")
        event_type = _text(event.get("event_type"), label="event_type")
        if kind == "DEATH_MARKER":
            if event_type != "DEAD" or "damage" in event:
                raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                    "DEAD diagnostic attempted to carry damage"
                )
            target = _mapping(event.get("target"), label="DEAD target")
            dead_markers.append(
                {
                    "time_ms": _event_time_ms(trace, start_offset),
                    "trace_index": trace.get("trace_index"),
                    "source_eventmeta_order_key": list(order),
                    "target_guid": target.get("guid"),
                    "marker_only": True,
                    "damage_added": 0,
                    "runtime_pre_cancel_used": False,
                    "runtime_eligible": False,
                }
            )
            continue
        if kind == "NEGATIVE_DMG_DIAGNOSTIC_CONTEXT":
            signed = model_v2._negative_damage_signed_amount(event)
            target = _mapping(event.get("target"), label="negative DMG target")
            negative.append(
                {
                    "time_ms": _event_time_ms(trace, start_offset),
                    "trace_index": trace.get("trace_index"),
                    "source_eventmeta_order_key": list(order),
                    "target_guid": target.get("guid"),
                    "signed_amount": signed,
                    "absolute_magnitude": abs(signed),
                    "damage_added": 0,
                    "reward_added": 0,
                    "abs_or_clamp_used": False,
                    "runtime_eligible": False,
                    "voting_eligible": False,
                }
            )
            continue
        if event_type != "DMG":
            continue
        target_guid, _target_lane, voting_target = _event_target(event)
        player_guid = _optional_text(trace.get("player_guid"))
        attribution = _mapping(event.get("attribution"), label="DMG attribution")
        attribution_kind = _text(
            attribution.get("attribution_kind"), label="DMG attribution_kind"
        )
        damage = _mapping(event.get("damage"), label="DMG damage")
        amount = damage.get("amount")
        reason: str | None = None
        if kind == "UNATTRIBUTED_EVENT" or player_guid is None:
            reason = "UNATTRIBUTED_UNKNOWN_NONVOTING"
        elif attribution_kind not in PLAYER_ATTRIBUTION_KINDS:
            reason = "NONEXACT_PLAYER_ATTRIBUTION_NONVOTING"
        elif attribution.get("player_guid") != player_guid:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "DMG trace player differs from exact attribution player"
            )
        elif not voting_target or target_guid not in target_by_guid:
            reason = "NONVOTING_TARGET_LANE"
        elif isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "DMG amount must be numeric"
            )
        elif not math.isfinite(float(amount)) or float(amount) <= 0:
            reason = "NONPOSITIVE_DMG_NONVOTING"
        if reason is not None:
            nonruntime.append(
                _diagnostic_damage(
                    trace=trace, start_offset_ms=start_offset, reason=reason
                )
            )
            continue
        runtime.append(
            _runtime_damage_event(
                wave=_mapping(wave.get("wave"), label="wave identity"),
                trace=trace,
                target_index=target_by_guid[str(target_guid)],
                target_guid=str(target_guid),
                start_offset_ms=start_offset,
            )
        )
    if len({row["event_id"] for row in runtime}) != len(runtime):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "runtime schedule event IDs are not unique"
        )
    actor_events: dict[str, list[JSONMap]] = defaultdict(list)
    target_events: dict[int, list[JSONMap]] = defaultdict(list)
    actor_target_events: dict[tuple[str, int], list[JSONMap]] = defaultdict(list)
    for event in runtime:
        actor_events[event["actor_player_guid"]].append(event)
        target_events[event["target_index"]].append(event)
        actor_target_events[
            (event["actor_player_guid"], event["target_index"])
        ].append(event)
    actor_tracks = [
        {
            "actor_player_guid": guid,
            "events": deepcopy(actor_events.get(guid, [])),
            "event_count": len(actor_events.get(guid, [])),
            "damage": sum(row["damage"] for row in actor_events.get(guid, [])),
        }
        for guid in roster
    ]
    target_tracks = [
        {
            "target_index": row["target_index"],
            "target_guid": row["target_guid"],
            "events": deepcopy(target_events.get(row["target_index"], [])),
            "event_count": len(target_events.get(row["target_index"], [])),
            "observed_positive_background_damage_proxy": sum(
                event["damage"] for event in target_events.get(row["target_index"], [])
            ),
            "proxy_is_initial_health": False,
        }
        for row in registry
    ]
    actor_target_tracks = [
        {
            "actor_player_guid": actor,
            "target_index": target_index,
            "event_ids": [row["event_id"] for row in events],
            "event_count": len(events),
            "damage": sum(row["damage"] for row in events),
        }
        for (actor, target_index), events in sorted(actor_target_events.items())
    ]
    schedule_semantics = {
        "wave": identity,
        "component_id": component,
        "target_registry": registry,
        "runtime_candidate_schedule": runtime,
        "nonruntime_damage_diagnostics": nonruntime,
        "dead_marker_diagnostics": dead_markers,
        "negative_damage_diagnostics": negative,
    }
    core = {
        "schema": PARTITION_RECORD_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "record_type": "component_whole_wave_background_block",
        "status": STATUS,
        "wave": identity,
        "component_id": component,
        "source_model": {
            "wave_content_sha256": model_wave_sha,
            "exact_trace_content_sha256": _sha256_bytes(
                _canonical_bytes(wave.get("exact_trace"))
            ),
        },
        "contamination_lane": contamination,
        "roster_player_guids": roster,
        "target_registry": registry,
        "runtime_candidate_schedule": runtime,
        "actor_tracks": actor_tracks,
        "target_tracks": target_tracks,
        "actor_target_tracks": actor_target_tracks,
        "nonruntime_damage_diagnostics": nonruntime,
        "dead_marker_diagnostics": dead_markers,
        "negative_damage_diagnostics": negative,
        "schedule_semantic_sha256": _sha256_bytes(
            _canonical_bytes(schedule_semantics)
        ),
        "runtime_contract": {
            "positive_official_dmg_only": True,
            "exact_player_direct_owner_controller_only": True,
            "voting_hostile_creature_target_only": True,
            "eventmeta_order_preserved_in_block": True,
            "historical_dead_marker_pre_cancel_used": False,
            "runtime_dead_target_cancellation_owner": "simulator_receipt",
            "initial_target_health_source": "REQUIRED_EXTERNAL_HYPOTHESIS",
            "observed_damage_or_death_used_as_initial_health": False,
        },
        "scientific_boundaries": {
            "exact_replay_status": "DESCRIPTIVE_NONVOTING",
            "voting_eligible": False,
            "comparison_ready": False,
            "policy_feature_exported": False,
            "future_teammate_event_exported_as_policy_feature": False,
            "final_wave_total_exported_as_policy_feature": False,
            "observed_death_time_exported_as_policy_feature": False,
            "player_name_used": False,
        },
        "summary": {
            "roster_player_count": len(roster),
            "target_count": len(registry),
            "runtime_candidate_event_count": len(runtime),
            "runtime_candidate_damage": sum(row["damage"] for row in runtime),
            "nonruntime_damage_diagnostic_count": len(nonruntime),
            "dead_marker_diagnostic_count": len(dead_markers),
            "negative_damage_diagnostic_count": len(negative),
            "negative_damage_signed_amount_excluded": sum(
                row["signed_amount"] for row in negative
            ),
            "negative_damage_absolute_amount_excluded": sum(
                row["absolute_magnitude"] for row in negative
            ),
            "negative_damage_added": 0,
            "training_candidate": contamination["training_candidate"],
            "voting_eligible": False,
            "comparison_ready": False,
        },
    }
    block = _content_addressed(core)
    _validate_block(block, expected_instance_id=identity["instance_id"])
    return block


def _validate_runtime_event(
    event: Mapping[str, Any], *, targets: Mapping[int, str]
) -> None:
    allowed_keys = {
        "time_ms",
        "target_index",
        "target_guid",
        "event_id",
        "damage",
        "actor_player_guid",
        "attribution_kind",
        "source_guid",
        "source_lane",
        "trace_index",
        "source_eventmeta_order_key",
        "official_message_sha256",
        "evidence_status",
        "runtime_eligible",
    }
    if set(event) != allowed_keys:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "runtime schedule contains undeclared or missing fields"
        )
    _nonnegative_integer(event.get("time_ms"), label="schedule.time_ms")
    target_index = _nonnegative_integer(
        event.get("target_index"), label="schedule.target_index"
    )
    if target_index not in targets or event.get("target_guid") != targets[target_index]:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "schedule target index/GUID differs from target registry"
        )
    _text(event.get("event_id"), label="schedule.event_id")
    _positive_number(event.get("damage"), label="schedule.damage")
    _text(event.get("actor_player_guid"), label="schedule.actor_player_guid")
    if event.get("attribution_kind") not in PLAYER_ATTRIBUTION_KINDS:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "runtime schedule has nonexact attribution"
        )
    if (
        event.get("runtime_eligible") is not True
        or event.get("evidence_status")
        != "POSITIVE_OFFICIAL_DMG_EXACT_PLAYER_AND_TARGET"
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "runtime schedule eligibility was widened"
        )
    order = _array(
        event.get("source_eventmeta_order_key"), label="source EventMeta order"
    )
    if len(order) != 5 or any(
        isinstance(value, bool) or not isinstance(value, int) for value in order
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "schedule source EventMeta order is malformed"
        )


def _validate_block(
    block: Mapping[str, Any], *, expected_instance_id: str | None = None
) -> JSONMap:
    if (
        block.get("schema") != PARTITION_RECORD_SCHEMA
        or block.get("implementation_revision") != IMPLEMENTATION_REVISION
        or block.get("record_type") != "component_whole_wave_background_block"
        or block.get("status") != STATUS
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "unsupported team-background block"
        )
    block_sha = _verify_content_address(block, label="background block")
    wave = _wave_identity(_mapping(block.get("wave"), label="block wave"))
    if expected_instance_id is not None and wave["instance_id"] != expected_instance_id:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background block crossed an instance partition"
        )
    _sha(block.get("component_id"), label="block.component_id")
    contamination = _mapping(
        block.get("contamination_lane"), label="block contamination"
    )
    label = _text(contamination.get("label"), label="contamination.label")
    training_candidate = contamination.get("training_candidate")
    if type(training_candidate) is not bool:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "block receipt cohort candidate must be boolean"
        )
    expected_assignment = (
        "TRAINING_CANDIDATE"
        if training_candidate
        else "DESCRIPTIVE_NONTRAINING"
    )
    if (
        label not in ALL_CONTAMINATION_LABELS
        or contamination.get("raw_label_candidate_filter_passed")
        is not (label in TRAINING_CONTAMINATION_LABELS)
        or contamination.get("candidate_authority")
        != "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP"
        or contamination.get("cohort_assignment") != expected_assignment
        or not isinstance(contamination.get("cohort_reason"), str)
        or not contamination.get("cohort_reason")
        or contamination.get("player_name_used") is not False
        or contamination.get("named_player_blacklist_or_weighting_used") is not False
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "block contamination contract differs from raid-level rule"
        )
    _sha(
        contamination.get("cohort_receipt_content_sha256"),
        label="block cohort receipt content SHA",
    )
    roster = _array(block.get("roster_player_guids"), label="roster_player_guids")
    if roster != sorted(set(roster)) or any(
        not isinstance(value, str) or not value for value in roster
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "block roster GUIDs are not unique and sorted"
        )
    registry = _array(block.get("target_registry"), label="target_registry")
    targets: dict[int, str] = {}
    target_guids: set[str] = set()
    for expected_index, raw in enumerate(registry):
        target = _mapping(raw, label="target registry row")
        index = _nonnegative_integer(target.get("target_index"), label="target_index")
        guid = _text(target.get("target_guid"), label="target_guid")
        if (
            index != expected_index
            or guid in target_guids
            or target.get("lane") != "HOSTILE_CREATURE"
            or target.get("initial_health") is not None
            or target.get("initial_health_status") != "REQUIRES_EXTERNAL_HYPOTHESIS"
            or target.get("observed_background_damage_is_initial_health") is not False
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "target registry violates explicit-health/index contract"
            )
        targets[index] = guid
        target_guids.add(guid)
    runtime = [
        _mapping(raw, label="runtime schedule event")
        for raw in _array(
            block.get("runtime_candidate_schedule"),
            label="runtime_candidate_schedule",
        )
    ]
    prior: tuple[int, ...] | None = None
    event_ids: set[str] = set()
    for event in runtime:
        _validate_runtime_event(event, targets=targets)
        order = tuple(event["source_eventmeta_order_key"])
        if prior is not None and order <= prior:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "runtime schedule is not strict source EventMeta order"
            )
        prior = order
        event_id = str(event["event_id"])
        if event_id in event_ids:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "runtime schedule event IDs are not unique"
            )
        event_ids.add(event_id)
    actor_tracks = _array(block.get("actor_tracks"), label="actor_tracks")
    tracked_ids: list[str] = []
    if [row.get("actor_player_guid") for row in actor_tracks] != roster:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "actor tracks do not follow the exact GUID roster"
        )
    for raw_track in actor_tracks:
        track = _mapping(raw_track, label="actor track")
        events = _array(track.get("events"), label="actor track events")
        if (
            track.get("event_count") != len(events)
            or track.get("damage") != sum(row.get("damage", 0) for row in events)
            or any(
                row.get("actor_player_guid") != track.get("actor_player_guid")
                for row in events
            )
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "actor track accounting differs from schedule"
            )
        tracked_ids.extend(str(row.get("event_id")) for row in events)
    if sorted(tracked_ids) != sorted(event_ids):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "actor tracks do not partition runtime schedule"
        )
    target_tracks = _array(block.get("target_tracks"), label="target_tracks")
    if len(target_tracks) != len(registry):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "target tracks do not cover the target registry"
        )
    target_tracked_ids: list[str] = []
    for expected_index, raw_track in enumerate(target_tracks):
        track = _mapping(raw_track, label="target track")
        events = _array(track.get("events"), label="target track events")
        if (
            track.get("target_index") != expected_index
            or track.get("target_guid") != targets[expected_index]
            or track.get("event_count") != len(events)
            or track.get("observed_positive_background_damage_proxy")
            != sum(event.get("damage", 0) for event in events)
            or track.get("proxy_is_initial_health") is not False
            or any(event.get("target_index") != expected_index for event in events)
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "target track accounting differs from schedule"
            )
        target_tracked_ids.extend(str(event.get("event_id")) for event in events)
    if sorted(target_tracked_ids) != sorted(event_ids):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "target tracks do not partition runtime schedule"
        )
    actor_target_tracks = _array(
        block.get("actor_target_tracks"), label="actor_target_tracks"
    )
    expected_actor_target: list[JSONMap] = []
    grouped_actor_target: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for event in runtime:
        grouped_actor_target[
            (str(event["actor_player_guid"]), int(event["target_index"]))
        ].append(event)
    for (actor, target_index), events in sorted(grouped_actor_target.items()):
        expected_actor_target.append(
            {
                "actor_player_guid": actor,
                "target_index": target_index,
                "event_ids": [row["event_id"] for row in events],
                "event_count": len(events),
                "damage": sum(row["damage"] for row in events),
            }
        )
    if actor_target_tracks != expected_actor_target:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "actor-target tracks differ from runtime schedule"
        )
    nonruntime = _array(
        block.get("nonruntime_damage_diagnostics"),
        label="nonruntime_damage_diagnostics",
    )
    if any(
        _mapping(row, label="nonruntime diagnostic").get("runtime_eligible")
        is not False
        for row in nonruntime
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "nonruntime diagnostic was made runtime eligible"
        )
    dead = _array(block.get("dead_marker_diagnostics"), label="dead diagnostics")
    if any(
        _mapping(row, label="dead diagnostic").get("damage_added") != 0
        or _mapping(row, label="dead diagnostic").get("marker_only") is not True
        for row in dead
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "DEAD marker contributed damage"
        )
    negative = _array(
        block.get("negative_damage_diagnostics"), label="negative diagnostics"
    )
    for raw in negative:
        row = _mapping(raw, label="negative diagnostic")
        signed = _integer(row.get("signed_amount"), label="signed_amount")
        if (
            signed >= 0
            or row.get("absolute_magnitude") != abs(signed)
            or row.get("damage_added") != 0
            or row.get("reward_added") != 0
            or row.get("abs_or_clamp_used") is not False
            or row.get("runtime_eligible") is not False
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "negative DMG diagnostic entered runtime/damage/reward"
            )
    schedule_semantics = {
        "wave": wave,
        "component_id": block.get("component_id"),
        "target_registry": registry,
        "runtime_candidate_schedule": runtime,
        "nonruntime_damage_diagnostics": nonruntime,
        "dead_marker_diagnostics": dead,
        "negative_damage_diagnostics": negative,
    }
    if block.get("schedule_semantic_sha256") != _sha256_bytes(
        _canonical_bytes(schedule_semantics)
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "block schedule semantic hash mismatch"
        )
    boundaries = _mapping(
        block.get("scientific_boundaries"), label="scientific_boundaries"
    )
    if (
        boundaries.get("voting_eligible") is not False
        or boundaries.get("comparison_ready") is not False
        or boundaries.get("policy_feature_exported") is not False
        or boundaries.get("player_name_used") is not False
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "block scientific boundary was widened"
        )
    if _contains_key(block, "player_name"):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "player_name leaked into a background block"
        )
    summary = _mapping(block.get("summary"), label="block summary")
    expected = {
        "roster_player_count": len(roster),
        "target_count": len(registry),
        "runtime_candidate_event_count": len(runtime),
        "runtime_candidate_damage": sum(row["damage"] for row in runtime),
        "nonruntime_damage_diagnostic_count": len(nonruntime),
        "dead_marker_diagnostic_count": len(dead),
        "negative_damage_diagnostic_count": len(negative),
        "negative_damage_signed_amount_excluded": sum(
            row["signed_amount"] for row in negative
        ),
        "negative_damage_absolute_amount_excluded": sum(
            row["absolute_magnitude"] for row in negative
        ),
        "negative_damage_added": 0,
        "training_candidate": contamination.get("training_candidate"),
        "voting_eligible": False,
        "comparison_ready": False,
    }
    if any(summary.get(key) != value for key, value in expected.items()):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "block summary differs from validated schedule"
        )
    return {"block_content_sha256": block_sha, **expected}


@dataclass(frozen=True)
class _PartitionBuild:
    temporary_path: Path
    final_path: Path
    compressed_file_sha256: str
    manifest_entry: Mapping[str, Any]
    block_descriptors: tuple[Mapping[str, Any], ...]


def _build_partition(
    context: _InputInstance, *, output_directory: Path
) -> _PartitionBuild:
    source_partition = _mapping(
        context.entry.get("partition"), label="model partition"
    )
    expected_size = _nonnegative_integer(
        source_partition.get("compressed_size_bytes"),
        label="model compressed_size_bytes",
    )
    expected_compressed_sha = _sha(
        source_partition.get("compressed_file_sha256"),
        label="model compressed_file_sha256",
    )
    expected_logical_sha = _sha(
        source_partition.get("logical_content_sha256"),
        label="model logical_content_sha256",
    )
    expected_count = _nonnegative_integer(
        source_partition.get("record_count"), label="model record_count"
    )
    if (
        context.partition_path.stat().st_size != expected_size
        or _sha256_file(context.partition_path) != expected_compressed_sha
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "model partition size/hash changed before background build"
        )
    descriptor, name = tempfile.mkstemp(
        prefix=f".{context.instance_id}.external-team-background.",
        suffix=".jsonl.gz.tmp",
        dir=output_directory,
    )
    os.close(descriptor)
    temporary = Path(name)
    input_logical = hashlib.sha256()
    output_logical = hashlib.sha256()
    output_size = 0
    count = 0
    prior_wave: tuple[int, int, str] | None = None
    descriptors: list[Mapping[str, Any]] = []
    totals: Counter[str] = Counter()
    try:
        with gzip.open(context.partition_path, "rb") as input_handle:
            with temporary.open("wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_output,
                    compresslevel=9,
                    mtime=0,
                ) as output_handle:
                    for line_number, raw_line in enumerate(input_handle, 1):
                        input_logical.update(raw_line)
                        try:
                            value = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as error:
                            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                                f"invalid model partition row {line_number}: {error}"
                            ) from error
                        wave = _mapping(value, label="model wave row")
                        if raw_line != _canonical_bytes(wave) + b"\n":
                            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                                "model partition row is not canonical JSONL"
                            )
                        identity = _wave_identity(
                            _mapping(wave.get("wave"), label="wave identity")
                        )
                        wave_order = (
                            identity["encounter_ordinal"],
                            identity["wave_ordinal"],
                            identity["wave_id"],
                        )
                        if prior_wave is not None and wave_order <= prior_wave:
                            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                                "model waves are not in strict deterministic order"
                            )
                        prior_wave = wave_order
                        block = _compile_block(
                            wave,
                            component_by_node=context.component_by_node,
                            expected_contamination=context.contamination,
                        )
                        payload = _canonical_bytes(block) + b"\n"
                        output_handle.write(payload)
                        output_logical.update(payload)
                        output_size += len(payload)
                        block_summary = _mapping(
                            block.get("summary"), label="block summary"
                        )
                        descriptor_row = {
                            "instance_id": context.instance_id,
                            "component_id": block["component_id"],
                            "wave": deepcopy(block["wave"]),
                            "block_content_sha256": block["content_address"]["sha256"],
                            "schedule_semantic_sha256": block[
                                "schedule_semantic_sha256"
                            ],
                            "roster_player_guids": deepcopy(
                                block["roster_player_guids"]
                            ),
                            "contamination_label": block["contamination_lane"][
                                "label"
                            ],
                            "training_candidate": block_summary[
                                "training_candidate"
                            ],
                            "whole_wave_selected": True,
                            "voting_eligible": False,
                            "comparison_ready": False,
                        }
                        descriptors.append(descriptor_row)
                        totals["runtime_candidate_event_count"] += int(
                            block_summary["runtime_candidate_event_count"]
                        )
                        totals["runtime_candidate_damage"] += int(
                            block_summary["runtime_candidate_damage"]
                        )
                        totals["nonruntime_damage_diagnostic_count"] += int(
                            block_summary["nonruntime_damage_diagnostic_count"]
                        )
                        totals["dead_marker_diagnostic_count"] += int(
                            block_summary["dead_marker_diagnostic_count"]
                        )
                        totals["negative_damage_diagnostic_count"] += int(
                            block_summary["negative_damage_diagnostic_count"]
                        )
                        totals["negative_damage_absolute_amount_excluded"] += int(
                            block_summary[
                                "negative_damage_absolute_amount_excluded"
                            ]
                        )
                        totals["negative_damage_signed_amount_excluded"] += int(
                            block_summary["negative_damage_signed_amount_excluded"]
                        )
                        totals["training_candidate_block_count"] += int(
                            block_summary["training_candidate"] is True
                        )
                        count += 1
                raw_output.flush()
                os.fsync(raw_output.fileno())
        if (
            count != expected_count
            or input_logical.hexdigest() != expected_logical_sha
            or context.partition_path.stat().st_size != expected_size
            or _sha256_file(context.partition_path) != expected_compressed_sha
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "model partition count/hash changed during background build"
            )
        logical_sha = output_logical.hexdigest()
        final = output_directory / f"{context.instance_id}.{logical_sha}.jsonl.gz"
        compressed_sha = _sha256_file(temporary)
        entry_core = {
            "instance_id": context.instance_id,
            "status": STATUS,
            "model_contamination_lane": deepcopy(dict(context.contamination)),
            "model_input": {
                "instance_entry_content_sha256": _verify_content_address(
                    context.entry, label="model instance entry"
                ),
                "logical_content_sha256": expected_logical_sha,
                "compressed_file_sha256": expected_compressed_sha,
                "compressed_size_bytes": expected_size,
                "record_count": expected_count,
                "scan_count": 1,
            },
            "partition": {
                "path": final.name,
                "logical_content_sha256": logical_sha,
                "logical_size_bytes": output_size,
                "compressed_file_sha256": compressed_sha,
                "compressed_size_bytes": temporary.stat().st_size,
                "record_count": count,
                "record_schema": PARTITION_RECORD_SCHEMA,
                "gzip_mtime": 0,
            },
            "summary": {
                "block_count": count,
                "training_candidate_block_count": totals[
                    "training_candidate_block_count"
                ],
                "runtime_candidate_event_count": totals[
                    "runtime_candidate_event_count"
                ],
                "runtime_candidate_damage": totals["runtime_candidate_damage"],
                "nonruntime_damage_diagnostic_count": totals[
                    "nonruntime_damage_diagnostic_count"
                ],
                "dead_marker_diagnostic_count": totals[
                    "dead_marker_diagnostic_count"
                ],
                "negative_damage_diagnostic_count": totals[
                    "negative_damage_diagnostic_count"
                ],
                "negative_damage_signed_amount_excluded": totals[
                    "negative_damage_signed_amount_excluded"
                ],
                "negative_damage_absolute_amount_excluded": totals[
                    "negative_damage_absolute_amount_excluded"
                ],
                "negative_damage_added": 0,
                "raw_row_copy_count": 0,
                "normalized_row_copy_count": 0,
                "network_request_count": 0,
            },
        }
        return _PartitionBuild(
            temporary_path=temporary,
            final_path=final,
            compressed_file_sha256=compressed_sha,
            manifest_entry=_content_addressed(entry_core),
            block_descriptors=tuple(descriptors),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parallel_builds(
    contexts: Sequence[_InputInstance], *, output_directory: Path, workers: int
) -> list[_PartitionBuild]:
    results: list[_PartitionBuild | None] = [None] * len(contexts)
    failure: BaseException | None = None
    with ProcessPoolExecutor(max_workers=min(workers, len(contexts))) as executor:
        futures = {
            executor.submit(
                _build_partition, context, output_directory=output_directory
            ): index
            for index, context in enumerate(contexts)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except BaseException as error:
                if failure is None:
                    failure = error
                for pending in futures:
                    if pending is not future:
                        pending.cancel()
    if failure is not None:
        for result in results:
            if result is not None:
                result.temporary_path.unlink(missing_ok=True)
        raise failure
    if any(result is None for result in results):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "parallel background build ended without every instance result"
        )
    return [result for result in results if result is not None]


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


def _publish_immutable(temporary: Path, final: Path, expected_sha: str) -> None:
    if final.exists():
        if not final.is_file() or final.is_symlink() or _sha256_file(final) != expected_sha:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                f"immutable content-addressed output differs: {final}"
            )
        temporary.unlink()
    else:
        temporary.replace(final)


def _component_pools(builds: Sequence[_PartitionBuild]) -> list[JSONMap]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for build in builds:
        for descriptor in build.block_descriptors:
            grouped[str(descriptor["component_id"])].append(descriptor)
    pools: list[JSONMap] = []
    for component_id in sorted(grouped):
        blocks = sorted(
            (deepcopy(dict(row)) for row in grouped[component_id]),
            key=lambda row: (
                row["instance_id"],
                row["wave"]["encounter_ordinal"],
                row["wave"]["wave_ordinal"],
                row["wave"]["wave_id"],
            ),
        )
        pools.append(
            {
                "component_id": component_id,
                "bootstrap_unit": "one complete historical raid wave",
                "blocks": blocks,
                "block_count": len(blocks),
                "training_candidate_block_count": sum(
                    row["training_candidate"] is True for row in blocks
                ),
                "row_or_actor_resampling_allowed": False,
                "cross_component_draw_allowed": False,
            }
        )
    return pools


def _manifest_summary(entries: Sequence[Mapping[str, Any]]) -> JSONMap:
    keys = (
        "block_count",
        "training_candidate_block_count",
        "runtime_candidate_event_count",
        "runtime_candidate_damage",
        "nonruntime_damage_diagnostic_count",
        "dead_marker_diagnostic_count",
        "negative_damage_diagnostic_count",
        "negative_damage_absolute_amount_excluded",
        "raw_row_copy_count",
        "normalized_row_copy_count",
        "network_request_count",
    )
    result = {
        key: sum(
            _nonnegative_integer(
                _mapping(entry.get("summary"), label="instance summary").get(key),
                label=f"instance summary.{key}",
            )
            for entry in entries
        )
        for key in keys
    }
    result["negative_damage_signed_amount_excluded"] = sum(
        _integer(
            _mapping(entry.get("summary"), label="instance summary").get(
                "negative_damage_signed_amount_excluded"
            ),
            label="negative_damage_signed_amount_excluded",
        )
        for entry in entries
    )
    result.update(
        {
            "instance_count": len(entries),
            "training_candidate_instance_count": sum(
                _mapping(
                    entry.get("model_contamination_lane"),
                    label="model contamination lane",
                ).get("candidate_filter_passed")
                is True
                for entry in entries
            ),
            "negative_damage_added": 0,
            "voting_eligible": False,
            "comparison_ready": False,
        }
    )
    result["descriptive_nontraining_instance_count"] = (
        result["instance_count"] - result["training_candidate_instance_count"]
    )
    return result


def build_external_team_background_generator(
    *,
    team_model_manifest_path: str | Path = DEFAULT_TEAM_MODEL_MANIFEST,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    workers: int = 1,
) -> JSONMap:
    """Build deterministic per-instance whole-wave background blocks."""

    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"workers must be an integer from 1 through {MAX_WORKERS}"
        )
    closure = _load_input_closure(team_model_manifest_path)
    output = _under(
        Path(output_directory), closure.data_root, label="background output directory"
    )
    output.mkdir(parents=True, exist_ok=True)
    builds: list[_PartitionBuild] = []
    addressed_temporary: Path | None = None
    stable_temporary: Path | None = None
    try:
        if workers == 1 or len(closure.instances) == 1:
            builds = [
                _build_partition(context, output_directory=output)
                for context in closure.instances
            ]
        else:
            builds = _parallel_builds(
                closure.instances, output_directory=output, workers=workers
            )
        entries = [deepcopy(dict(build.manifest_entry)) for build in builds]
        pools = _component_pools(builds)
        manifest_core = {
            "schema": SCHEMA,
            "kind": KIND,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "status": STATUS,
            "input_closure": {
                "team_model_manifest": {
                    "stable_path": _relative(
                        closure.manifest_path,
                        closure.data_root,
                        label="model stable manifest",
                    ),
                    "content_addressed_path": _relative(
                        closure.addressed_path,
                        closure.data_root,
                        label="model addressed manifest",
                    ),
                    "content_sha256": closure.content_sha256,
                    "file_sha256": closure.file_sha256,
                    "size_bytes": closure.manifest_path.stat().st_size,
                    "schema": model_v2.SCHEMA,
                    "implementation_revision": model_v2.IMPLEMENTATION_REVISION,
                    "status": model_v2.STATUS,
                },
                "cohort_receipt": deepcopy(
                    dict(closure.cohort_receipt_binding)
                ),
            },
            "instance_order": [entry["instance_id"] for entry in entries],
            "split_graph": deepcopy(closure.manifest["split_graph"]),
            "component_pools": pools,
            "identity_and_contamination_contract": {
                "player_identity": "exact model player GUID only",
                "owner_controller_identity": "exact official relation only",
                "player_name_used": False,
                "guid_suffix_inference_used": False,
                "named_player_blacklist_or_weighting_used": False,
                "contamination_scope": "raid_instance",
                "training_candidate_authority": (
                    "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP"
                ),
                "raw_contamination_label_may_expand_training_cohort": False,
                "raw_label_candidate_labels": sorted(
                    TRAINING_CONTAMINATION_LABELS
                ),
                "nontraining_labels": sorted(NONTRAINING_CONTAMINATION_LABELS),
            },
            "bootstrap_and_schedule_contract": {
                "bootstrap_unit": "one complete historical raid wave",
                "component_whole_wave_draw": True,
                "row_or_actor_independent_bootstrap": False,
                "cross_component_draw": False,
                "runtime_damage_evidence": (
                    "positive official DMG with exact player and voting hostile-creature target"
                ),
                "unattributed_or_nonvoting_target_runtime_damage": False,
                "dead_is_marker_only": True,
                "negative_dmg_runtime_damage_or_reward": False,
                "event_time_and_source_order": "exact EventMeta causal order",
                "same_timestamp_candidate_order": SAME_TIMESTAMP_ORDER,
                "future_dead_target_event_cancellation": "simulator receipt",
            },
            "dynamic_adapter_contract": {
                "command": "load_dynamic_v1",
                "schema": DYNAMIC_SCHEMA,
                "target_health_required_from_external_hypothesis": True,
                "background_observed_damage_used_as_initial_health": False,
                "hypothesis_id_and_provenance_retained_upstream": True,
                "hypothesis_metadata_sent_to_physics_kernel": False,
                "wire_event_order": ["time_ms", "schedule_index"],
                "schedule_index_source": "exact External EventMeta causal order",
                "target_index_never_reorders_same_timestamp_events": True,
                "same_timestamp_order": SAME_TIMESTAMP_ORDER,
                "retarget_modes": sorted(RETARGET_MODES),
            },
            "streaming_and_parallel_contract": {
                "one_model_wave_materialized_at_a_time_per_worker": True,
                "workers_min": 1,
                "workers_max": MAX_WORKERS,
                "requested_worker_count_enters_content_identity": False,
                "worker_completion_order_enters_result_order": False,
                "gzip_mtime": 0,
            },
            "scientific_boundaries": {
                "status": STATUS,
                "exact_replay_status": "DESCRIPTIVE_NONVOTING",
                "voting_eligible": False,
                "comparison_ready": False,
                "dynamic_simulator_fidelity_gate_closed": False,
                "heldout_team_response_fidelity_gate_closed": False,
                "future_teammate_events_available_as_policy_features": False,
                "superiority_claim": False,
            },
            "publication_contract": {
                "partition_per_instance": True,
                "blocks_and_manifest_content_addressed": True,
                "content_addressed_manifest_published_before_stable_pointer": True,
                "stable_manifest_committed_last": True,
                "stable_and_addressed_manifest_bytes_equal": True,
                "raw_or_normalized_rows_copied": False,
            },
            "summary": {
                **_manifest_summary(entries),
                "component_count": len(pools),
            },
            "instances": entries,
        }
        manifest = _content_addressed(manifest_core)
        content_sha = _verify_content_address(manifest, label="background manifest")
        payload = _canonical_bytes(manifest) + b"\n"
        file_sha = _sha256_bytes(payload)
        addressed = output / (
            f"chronicle_external_team_background_generator_v2.{content_sha}.manifest.json"
        )
        stable = output / "manifest.json"
        addressed_temporary = _write_temporary(addressed, payload)
        stable_temporary = _write_temporary(stable, payload)
        for build in builds:
            _publish_immutable(
                build.temporary_path,
                build.final_path,
                build.compressed_file_sha256,
            )
        _publish_immutable(addressed_temporary, addressed, file_sha)
        addressed_temporary = None
        stable_temporary.replace(stable)
        stable_temporary = None
        return {
            "schema": SCHEMA,
            "status": STATUS,
            "manifest_path": str(stable),
            "content_addressed_manifest_path": str(addressed),
            "content_sha256": content_sha,
            "manifest_file_sha256": file_sha,
            "partitions": [str(build.final_path) for build in builds],
            "summary": manifest["summary"],
            "workers_requested": workers,
            "workers_used": min(workers, len(builds)),
            "voting_eligible": False,
            "comparison_ready": False,
            "network_request_count": 0,
        }
    finally:
        for build in builds:
            build.temporary_path.unlink(missing_ok=True)
        if addressed_temporary is not None:
            addressed_temporary.unlink(missing_ok=True)
        if stable_temporary is not None:
            stable_temporary.unlink(missing_ok=True)


def _derive_component_pools(
    blocks: Sequence[Mapping[str, Any]]
) -> list[JSONMap]:
    grouped: dict[str, list[JSONMap]] = defaultdict(list)
    for block in blocks:
        summary = _mapping(block.get("summary"), label="block summary")
        grouped[str(block["component_id"])].append(
            {
                "instance_id": block["wave"]["instance_id"],
                "component_id": block["component_id"],
                "wave": deepcopy(block["wave"]),
                "block_content_sha256": block["content_address"]["sha256"],
                "schedule_semantic_sha256": block["schedule_semantic_sha256"],
                "roster_player_guids": deepcopy(block["roster_player_guids"]),
                "contamination_label": block["contamination_lane"]["label"],
                "training_candidate": summary["training_candidate"],
                "whole_wave_selected": True,
                "voting_eligible": False,
                "comparison_ready": False,
            }
        )
    result: list[JSONMap] = []
    for component_id in sorted(grouped):
        descriptors = sorted(
            grouped[component_id],
            key=lambda row: (
                row["instance_id"],
                row["wave"]["encounter_ordinal"],
                row["wave"]["wave_ordinal"],
                row["wave"]["wave_id"],
            ),
        )
        result.append(
            {
                "component_id": component_id,
                "bootstrap_unit": "one complete historical raid wave",
                "blocks": descriptors,
                "block_count": len(descriptors),
                "training_candidate_block_count": sum(
                    row["training_candidate"] is True for row in descriptors
                ),
                "row_or_actor_resampling_allowed": False,
                "cross_component_draw_allowed": False,
            }
        )
    return result


def _model_wave_bindings(
    context: _InputInstance,
) -> dict[tuple[str, str, int, str, int], JSONMap]:
    """Re-scan the bound model partition for block-level provenance checks."""

    partition = _mapping(context.entry.get("partition"), label="model partition")
    expected_size = _nonnegative_integer(
        partition.get("compressed_size_bytes"), label="model compressed_size_bytes"
    )
    expected_file_sha = _sha(
        partition.get("compressed_file_sha256"),
        label="model compressed_file_sha256",
    )
    expected_logical_sha = _sha(
        partition.get("logical_content_sha256"),
        label="model logical_content_sha256",
    )
    expected_count = _nonnegative_integer(
        partition.get("record_count"), label="model record_count"
    )
    if (
        context.partition_path.stat().st_size != expected_size
        or _sha256_file(context.partition_path) != expected_file_sha
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "bound model partition changed before block validation"
        )
    logical = hashlib.sha256()
    bindings: dict[tuple[str, str, int, str, int], JSONMap] = {}
    with gzip.open(context.partition_path, "rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            logical.update(raw_line)
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                    f"invalid bound model row {line_number}: {error}"
                ) from error
            wave = _mapping(value, label="bound model wave")
            if raw_line != _canonical_bytes(wave) + b"\n":
                raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                    "bound model row is not canonical JSONL"
                )
            model_v2._validate_model_wave(
                wave,
                instance_id=context.instance_id,
                expected_contamination=context.contamination,
            )
            identity = _mapping(wave.get("wave"), label="bound model wave identity")
            key = _wave_key(identity)
            if key in bindings:
                raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                    "bound model contains a duplicate wave identity"
                )
            roster = sorted(
                _text(
                    _mapping(
                        _mapping(raw, label="model player").get("player"),
                        label="model player metadata",
                    ).get("guid"),
                    label="model player guid",
                )
                for raw in _array(wave.get("players"), label="model wave players")
            )
            bindings[key] = {
                "wave_content_sha256": _verify_content_address(
                    wave, label="bound model wave"
                ),
                "exact_trace_content_sha256": _sha256_bytes(
                    _canonical_bytes(wave.get("exact_trace"))
                ),
                "component_id": _wave_component(
                    wave, context.component_by_node
                ),
                "roster_player_guids": roster,
                "contamination_lane": _contamination(wave),
            }
    if (
        len(bindings) != expected_count
        or logical.hexdigest() != expected_logical_sha
        or context.partition_path.stat().st_size != expected_size
        or _sha256_file(context.partition_path) != expected_file_sha
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "bound model partition changed during block validation"
        )
    return bindings


def load_external_team_background_generator_manifest(
    path_value: str | Path,
) -> tuple[JSONMap, Path]:
    """Load and fully revalidate a committed External background artifact."""

    requested = Path(path_value).expanduser().resolve()
    manifest = _load_json(requested, label="background manifest")
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("kind") != KIND
        or manifest.get("implementation_revision") != IMPLEMENTATION_REVISION
        or manifest.get("status") != STATUS
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "unsupported background manifest"
        )
    content_sha = _verify_content_address(manifest, label="background manifest")
    payload = _canonical_bytes(manifest) + b"\n"
    if requested.read_bytes() != payload:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background manifest is not canonical JSON"
        )
    stable = requested.parent / "manifest.json"
    addressed = requested.parent / (
        f"chronicle_external_team_background_generator_v2.{content_sha}.manifest.json"
    )
    for candidate, label in ((stable, "stable"), (addressed, "addressed")):
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or candidate.read_bytes() != payload
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                f"background {label} manifest is missing or differs"
            )
    root = _data_root(stable)
    input_closure = _mapping(manifest.get("input_closure"), label="input_closure")
    binding = _mapping(
        input_closure.get(
            "team_model_manifest"
        ),
        label="team_model_manifest binding",
    )
    receipt_binding = _mapping(
        input_closure.get("cohort_receipt"), label="cohort_receipt binding"
    )
    model_path = _resolve_relative(
        root,
        binding.get("stable_path"),
        root,
        label="bound model stable manifest",
    )
    closure = _load_input_closure(model_path)
    if (
        closure.content_sha256 != binding.get("content_sha256")
        or closure.file_sha256 != binding.get("file_sha256")
        or closure.manifest_path.stat().st_size != binding.get("size_bytes")
        or _relative(
            closure.addressed_path, root, label="bound model addressed manifest"
        )
        != binding.get("content_addressed_path")
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "live team-model input differs from background input closure"
        )
    if dict(receipt_binding) != dict(closure.cohort_receipt_binding):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background cohort receipt binding differs from the strict model"
        )
    entries = [
        _mapping(raw, label="background instance entry")
        for raw in _array(manifest.get("instances"), label="background instances")
    ]
    order = _array(manifest.get("instance_order"), label="instance_order")
    if len(entries) != len(order) or not entries:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background instances and order differ"
        )
    model_by_id = {context.instance_id: context for context in closure.instances}
    if manifest.get("split_graph") != closure.manifest.get("split_graph"):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background split graph differs from the bound model"
        )
    all_blocks: list[Mapping[str, Any]] = []
    derived_entries: list[Mapping[str, Any]] = []
    for index, entry in enumerate(entries):
        _verify_content_address(entry, label="background instance entry")
        instance_id = _safe_component(entry.get("instance_id"), label="instance_id")
        if order[index] != instance_id or instance_id not in model_by_id:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background instance order/set differs from bound model"
            )
        source = model_by_id[instance_id]
        if dict(
            _mapping(
                entry.get("model_contamination_lane"),
                label="background model contamination lane",
            )
        ) != dict(source.contamination):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background instance cohort assignment differs from strict model"
            )
        source_waves = _model_wave_bindings(source)
        model_input = _mapping(entry.get("model_input"), label="model_input")
        if (
            model_input.get("instance_entry_content_sha256")
            != _verify_content_address(source.entry, label="model instance entry")
            or model_input.get("logical_content_sha256")
            != source.entry["partition"]["logical_content_sha256"]
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background instance binding differs from model"
            )
        partition = _mapping(entry.get("partition"), label="background partition")
        partition_path = _resolve_relative(
            stable.parent,
            partition.get("path"),
            root,
            label="background partition path",
        )
        expected_size = _nonnegative_integer(
            partition.get("compressed_size_bytes"),
            label="background compressed_size_bytes",
        )
        if (
            partition_path.stat().st_size != expected_size
            or _sha256_file(partition_path)
            != partition.get("compressed_file_sha256")
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background partition compressed size/hash mismatch"
            )
        logical = hashlib.sha256()
        count = 0
        prior_wave: tuple[int, int, str] | None = None
        totals: Counter[str] = Counter()
        with gzip.open(partition_path, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                logical.update(raw_line)
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                        f"invalid background row {line_number}: {error}"
                    ) from error
                block = _mapping(value, label="background block")
                if raw_line != _canonical_bytes(block) + b"\n":
                    raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                        "background partition row is not canonical JSONL"
                    )
                _validate_block(block, expected_instance_id=instance_id)
                wave = _wave_identity(_mapping(block.get("wave"), label="block wave"))
                source_wave = source_waves.get(_wave_key(wave))
                source_binding = _mapping(
                    block.get("source_model"), label="block source_model"
                )
                if (
                    source_wave is None
                    or source_binding.get("wave_content_sha256")
                    != source_wave["wave_content_sha256"]
                    or source_binding.get("exact_trace_content_sha256")
                    != source_wave["exact_trace_content_sha256"]
                    or block.get("component_id") != source_wave["component_id"]
                    or block.get("roster_player_guids")
                    != source_wave["roster_player_guids"]
                    or block.get("contamination_lane")
                    != source_wave["contamination_lane"]
                ):
                    raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                        "background block differs from its exact bound model wave"
                    )
                del source_waves[_wave_key(wave)]
                wave_order = (
                    wave["encounter_ordinal"],
                    wave["wave_ordinal"],
                    wave["wave_id"],
                )
                if prior_wave is not None and wave_order <= prior_wave:
                    raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                        "background blocks are not in strict wave order"
                    )
                prior_wave = wave_order
                summary = _mapping(block.get("summary"), label="block summary")
                for key in (
                    "runtime_candidate_event_count",
                    "runtime_candidate_damage",
                    "nonruntime_damage_diagnostic_count",
                    "dead_marker_diagnostic_count",
                    "negative_damage_diagnostic_count",
                    "negative_damage_absolute_amount_excluded",
                ):
                    totals[key] += int(summary[key])
                totals["negative_damage_signed_amount_excluded"] += int(
                    summary["negative_damage_signed_amount_excluded"]
                )
                totals["training_candidate_block_count"] += int(
                    summary["training_candidate"] is True
                )
                all_blocks.append(block)
                count += 1
        if (
            count != partition.get("record_count")
            or logical.hexdigest() != partition.get("logical_content_sha256")
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background partition logical hash/count mismatch"
            )
        if source_waves:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background partition omitted bound model waves"
            )
        declared = _mapping(entry.get("summary"), label="instance summary")
        derived = {
            "block_count": count,
            "training_candidate_block_count": totals[
                "training_candidate_block_count"
            ],
            "runtime_candidate_event_count": totals[
                "runtime_candidate_event_count"
            ],
            "runtime_candidate_damage": totals["runtime_candidate_damage"],
            "nonruntime_damage_diagnostic_count": totals[
                "nonruntime_damage_diagnostic_count"
            ],
            "dead_marker_diagnostic_count": totals[
                "dead_marker_diagnostic_count"
            ],
            "negative_damage_diagnostic_count": totals[
                "negative_damage_diagnostic_count"
            ],
            "negative_damage_signed_amount_excluded": totals[
                "negative_damage_signed_amount_excluded"
            ],
            "negative_damage_absolute_amount_excluded": totals[
                "negative_damage_absolute_amount_excluded"
            ],
            "negative_damage_added": 0,
            "raw_row_copy_count": 0,
            "normalized_row_copy_count": 0,
            "network_request_count": 0,
        }
        if any(declared.get(key) != value for key, value in derived.items()):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "background instance summary differs from validated blocks"
            )
        derived_entries.append(entry)
    if set(order) != set(model_by_id) or len(order) != len(model_by_id):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background omitted or added a model instance"
        )
    pools = _derive_component_pools(all_blocks)
    if manifest.get("component_pools") != pools:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "component pools differ from whole-wave blocks"
        )
    expected_summary = {
        **_manifest_summary(derived_entries),
        "component_count": len(pools),
    }
    if manifest.get("summary") != expected_summary:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background manifest summary differs from instance entries"
        )
    if (
        expected_summary.get("instance_count")
        != receipt_binding.get("descriptive_instance_count")
        or expected_summary.get("training_candidate_instance_count")
        != receipt_binding.get("training_instance_count")
        or expected_summary.get("descriptive_nontraining_instance_count")
        != receipt_binding.get("descriptive_nontraining_instance_count")
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "background summary differs from the exact receipt cohort"
        )
    identity = _mapping(
        manifest.get("identity_and_contamination_contract"),
        label="identity_and_contamination_contract",
    )
    scientific = _mapping(
        manifest.get("scientific_boundaries"), label="scientific_boundaries"
    )
    if (
        identity.get("player_name_used") is not False
        or identity.get("guid_suffix_inference_used") is not False
        or identity.get("named_player_blacklist_or_weighting_used") is not False
        or identity.get("training_candidate_authority")
        != "BOUND_COHORT_RECEIPT_EXACT_INSTANCE_MEMBERSHIP"
        or identity.get("raw_contamination_label_may_expand_training_cohort")
        is not False
        or scientific.get("voting_eligible") is not False
        or scientific.get("comparison_ready") is not False
        or scientific.get("superiority_claim") is not False
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "manifest identity/scientific boundary was widened"
        )
    return manifest, stable


def _load_selected_block(
    *, manifest: Mapping[str, Any], stable: Path, descriptor: Mapping[str, Any]
) -> JSONMap:
    entry = next(
        (
            _mapping(raw, label="instance entry")
            for raw in _array(manifest.get("instances"), label="instances")
            if _mapping(raw, label="instance entry").get("instance_id")
            == descriptor.get("instance_id")
        ),
        None,
    )
    if entry is None:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "selected block instance is absent from manifest"
        )
    root = _data_root(stable)
    partition = _mapping(entry.get("partition"), label="partition")
    path = _resolve_relative(
        stable.parent, partition.get("path"), root, label="background partition path"
    )
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            block = _mapping(value, label="background block")
            if (
                _mapping(block.get("content_address"), label="block address").get(
                    "sha256"
                )
                == descriptor.get("block_content_sha256")
            ):
                _validate_block(
                    block, expected_instance_id=str(descriptor["instance_id"])
                )
                return deepcopy(dict(block))
    raise ChronicleExternalTeamBackgroundGeneratorV2Error(
        "selected whole-wave block is absent from its partition"
    )


def _draw_key(
    *,
    generator_content_sha256: str,
    component_id: str,
    focal_player_guid: str,
    seed: int,
    draw_index: int,
) -> str:
    return _sha256_bytes(
        _canonical_bytes(
            {
                "generator_content_sha256": generator_content_sha256,
                "component_id": component_id,
                "focal_player_guid": focal_player_guid,
                "seed": seed,
                "draw_index": draw_index,
            }
        )
    )


def draw_external_team_background_schedule(
    *,
    generator_manifest_path: str | Path,
    component_id: str,
    focal_player_guid: str,
    seed: int,
    draw_index: int = 0,
) -> JSONMap:
    """Select one clean whole-wave block and remove the exact focal lane."""

    manifest, stable = load_external_team_background_generator_manifest(
        generator_manifest_path
    )
    component_id = _sha(component_id, label="component_id")
    focal = _text(focal_player_guid, label="focal_player_guid")
    seed = _nonnegative_integer(seed, label="seed")
    draw_index = _nonnegative_integer(draw_index, label="draw_index")
    pool = next(
        (
            _mapping(raw, label="component pool")
            for raw in _array(manifest.get("component_pools"), label="component_pools")
            if _mapping(raw, label="component pool").get("component_id")
            == component_id
        ),
        None,
    )
    if pool is None:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "requested component is absent from the generator"
        )
    population = [
        _mapping(raw, label="block descriptor")
        for raw in _array(pool.get("blocks"), label="component blocks")
        if _mapping(raw, label="block descriptor").get("training_candidate") is True
        and focal
        in _array(
            _mapping(raw, label="block descriptor").get("roster_player_guids"),
            label="descriptor roster_player_guids",
        )
    ]
    if not population:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "component has no clean whole-wave block containing the exact focal player"
        )
    generator_sha = _verify_content_address(manifest, label="background manifest")
    key = _draw_key(
        generator_content_sha256=generator_sha,
        component_id=component_id,
        focal_player_guid=focal,
        seed=seed,
        draw_index=draw_index,
    )
    selected = population[int(key, 16) % len(population)]
    block = _load_selected_block(
        manifest=manifest, stable=stable, descriptor=selected
    )
    complete = [
        deepcopy(dict(_mapping(raw, label="runtime event")))
        for raw in _array(
            block.get("runtime_candidate_schedule"),
            label="runtime_candidate_schedule",
        )
    ]
    excluded = [row for row in complete if row["actor_player_guid"] == focal]
    included = [row for row in complete if row["actor_player_guid"] != focal]
    included_actor_tracks: list[JSONMap] = []
    grouped: dict[str, list[JSONMap]] = defaultdict(list)
    for row in included:
        grouped[str(row["actor_player_guid"])].append(row)
    for actor in sorted(grouped):
        events = grouped[actor]
        included_actor_tracks.append(
            {
                "actor_player_guid": actor,
                "events": deepcopy(events),
                "event_count": len(events),
                "damage": sum(row["damage"] for row in events),
            }
        )
    excluded_by_kind = Counter(
        str(row["attribution_kind"]) for row in excluded
    )
    excluded_damage_by_kind = Counter()
    for row in excluded:
        excluded_damage_by_kind[str(row["attribution_kind"])] += row["damage"]
    draw_core = {
        "schema": DRAW_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "generator_content_sha256": generator_sha,
        "draw_identity": {
            "component_id": component_id,
            "focal_player_guid": focal,
            "seed": seed,
            "draw_index": draw_index,
            "keyed_draw_sha256": key,
            "eligible_population_size": len(population),
            "selection_index": int(key, 16) % len(population),
            "selection_unit": "one complete historical raid wave",
        },
        "source_block": {
            **deepcopy(dict(selected)),
            "whole_wave_selected": True,
        },
        "component_id": component_id,
        "target_registry": deepcopy(block["target_registry"]),
        "schedule": included,
        "included_actor_tracks": included_actor_tracks,
        "focal_leave_one_out": {
            "focal_player_guid": focal,
            "filter_contract": (
                "exact player GUID over direct/official-owner/official-controller DMG"
            ),
            "excluded_events": excluded,
            "excluded_event_count": len(excluded),
            "excluded_damage": sum(row["damage"] for row in excluded),
            "excluded_event_count_by_attribution": dict(
                sorted(excluded_by_kind.items())
            ),
            "excluded_damage_by_attribution": dict(
                sorted(excluded_damage_by_kind.items())
            ),
            "name_or_guid_suffix_inference_used": False,
        },
        "diagnostic_lanes": {
            "unattributed_or_nonvoting_target_damage": deepcopy(
                block["nonruntime_damage_diagnostics"]
            ),
            "historical_dead_markers": deepcopy(block["dead_marker_diagnostics"]),
            "signed_negative_damage": deepcopy(
                block["negative_damage_diagnostics"]
            ),
            "all_runtime_eligible": False,
            "all_voting_eligible": False,
        },
        "schedule_content_sha256": _sha256_bytes(_canonical_bytes(included)),
        "runtime_contract": {
            "initial_target_health_source": "REQUIRED_EXTERNAL_HYPOTHESIS",
            "historical_dead_marker_pre_cancel_used": False,
            "future_dead_target_event_cancellation": "simulator receipt",
            "same_timestamp_order": SAME_TIMESTAMP_ORDER,
        },
        "scientific_boundaries": {
            "status": STATUS,
            "voting_eligible": False,
            "comparison_ready": False,
            "exact_replay_can_vote": False,
            "future_schedule_available_as_policy_feature": False,
            "heldout_fidelity_gate_closed": False,
            "dynamic_simulator_gate_closed": False,
            "player_name_used": False,
        },
    }
    draw = _content_addressed(draw_core)
    validate_external_team_background_draw(draw)
    return draw


def validate_external_team_background_draw(draw: Mapping[str, Any]) -> None:
    if (
        draw.get("schema") != DRAW_SCHEMA
        or draw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or draw.get("status") != STATUS
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "unsupported background draw"
        )
    _verify_content_address(draw, label="background draw")
    component = _sha(draw.get("component_id"), label="draw.component_id")
    identity = _mapping(draw.get("draw_identity"), label="draw_identity")
    if (
        identity.get("component_id") != component
        or identity.get("selection_unit") != "one complete historical raid wave"
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "draw did not select a component whole-wave unit"
        )
    focal = _text(identity.get("focal_player_guid"), label="focal_player_guid")
    source = _mapping(draw.get("source_block"), label="source_block")
    if (
        source.get("component_id") != component
        or source.get("whole_wave_selected") is not True
        or source.get("training_candidate") is not True
        or focal not in _array(source.get("roster_player_guids"), label="source roster")
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "draw source block is outside its clean focal component pool"
        )
    targets = {
        _nonnegative_integer(
            _mapping(raw, label="target registry row").get("target_index"),
            label="target_index",
        ): _text(
            _mapping(raw, label="target registry row").get("target_guid"),
            label="target_guid",
        )
        for raw in _array(draw.get("target_registry"), label="target_registry")
    }
    schedule = [
        _mapping(raw, label="draw schedule event")
        for raw in _array(draw.get("schedule"), label="draw schedule")
    ]
    prior: tuple[int, ...] | None = None
    ids: set[str] = set()
    for event in schedule:
        _validate_runtime_event(event, targets=targets)
        if event.get("actor_player_guid") == focal:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "focal event survived leave-one-player-out"
            )
        order = tuple(event["source_eventmeta_order_key"])
        if prior is not None and order <= prior:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "draw schedule is not source EventMeta order"
            )
        prior = order
        event_id = str(event["event_id"])
        if event_id in ids:
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                "draw schedule event IDs are not unique"
            )
        ids.add(event_id)
    if draw.get("schedule_content_sha256") != _sha256_bytes(
        _canonical_bytes(schedule)
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "draw schedule hash mismatch"
        )
    loo = _mapping(draw.get("focal_leave_one_out"), label="focal_leave_one_out")
    excluded = [
        _mapping(raw, label="excluded focal event")
        for raw in _array(loo.get("excluded_events"), label="excluded_events")
    ]
    if (
        loo.get("focal_player_guid") != focal
        or loo.get("name_or_guid_suffix_inference_used") is not False
        or any(row.get("actor_player_guid") != focal for row in excluded)
        or any(row.get("attribution_kind") not in PLAYER_ATTRIBUTION_KINDS for row in excluded)
        or loo.get("excluded_event_count") != len(excluded)
        or loo.get("excluded_damage") != sum(row["damage"] for row in excluded)
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "draw focal leave-one-out accounting is invalid"
        )
    diagnostics = _mapping(draw.get("diagnostic_lanes"), label="diagnostic_lanes")
    if (
        diagnostics.get("all_runtime_eligible") is not False
        or diagnostics.get("all_voting_eligible") is not False
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "diagnostic lane was made runtime/voting eligible"
        )
    boundaries = _mapping(
        draw.get("scientific_boundaries"), label="scientific_boundaries"
    )
    if (
        boundaries.get("voting_eligible") is not False
        or boundaries.get("comparison_ready") is not False
        or boundaries.get("future_schedule_available_as_policy_feature") is not False
        or boundaries.get("player_name_used") is not False
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "draw scientific boundary was widened"
        )
    if _contains_key(draw, "player_name"):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "player_name leaked into a background draw"
        )


def adapt_draw_to_load_dynamic_v1(
    *,
    draw: Mapping[str, Any],
    request: Mapping[str, Any],
    seed: int,
    target_health_hypothesis: TargetHealthHypothesisV1,
    retarget_mode: str = RETARGET_NEXT_ALIVE_CYCLIC,
) -> ExternalDynamicScheduleAdapterV1:
    """Create the exact ``load_dynamic_v1`` wire request plus provenance."""

    validate_external_team_background_draw(draw)
    request_value = deepcopy(dict(_mapping(request, label="simulator request")))
    _canonical_bytes(request_value)
    seed = _nonnegative_integer(seed, label="simulator seed")
    if retarget_mode not in RETARGET_MODES:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"retarget_mode must be one of {sorted(RETARGET_MODES)}"
        )
    if not isinstance(target_health_hypothesis, TargetHealthHypothesisV1):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "target_health_hypothesis must be TargetHealthHypothesisV1"
        )
    hypothesis_id = _text(
        target_health_hypothesis.hypothesis_id, label="hypothesis_id"
    )
    provenance = deepcopy(
        dict(
            _mapping(
                target_health_hypothesis.provenance,
                label="target health hypothesis provenance",
            )
        )
    )
    if not provenance:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "target health hypothesis provenance must not be empty"
        )
    registry = [
        _mapping(raw, label="target registry row")
        for raw in _array(draw.get("target_registry"), label="target_registry")
    ]
    expected_indices = set(range(len(registry)))
    observed_indices = set(target_health_hypothesis.target_health_by_index)
    if any(type(index) is not int or index < 0 for index in observed_indices):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "target health hypothesis indices must be nonnegative integers"
        )
    if observed_indices != expected_indices:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "target health hypothesis must provide complete target index coverage"
        )
    typed_target_health = []
    for index in sorted(expected_indices):
        health = _positive_number(
            target_health_hypothesis.target_health_by_index[index],
            label=f"target_health[{index}]",
        )
        typed_target_health.append(
            DynamicTargetHealthV1(target_index=index, health=health)
        )
    typed_background_events = [
        BackgroundDamageEventV1(
            time_ms=_nonnegative_integer(row.get("time_ms"), label="time_ms"),
            schedule_index=schedule_index,
            target_index=_nonnegative_integer(
                row.get("target_index"), label="target_index"
            ),
            event_id=_text(row.get("event_id"), label="event_id"),
            damage=_positive_number(row.get("damage"), label="damage"),
        )
        for schedule_index, row in enumerate(
            _mapping(raw, label="draw schedule event")
            for raw in _array(draw.get("schedule"), label="draw schedule")
        )
    ]
    if typed_background_events != sorted(
        typed_background_events,
        key=lambda row: (row.time_ms, row.schedule_index),
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "dynamic background does not preserve EventMeta causal order"
        )
    if len({row.event_id for row in typed_background_events}) != len(
        typed_background_events
    ):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "dynamic background event IDs must be unique"
        )
    try:
        dynamic_config = DynamicTeamBackgroundConfigV1(
            target_health=tuple(typed_target_health),
            background_damage_events=tuple(typed_background_events),
            same_timestamp_order=SAME_TIMESTAMP_ORDER,
            retarget_mode=retarget_mode,
        )
    except (TypeError, ValueError) as error:
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            f"dynamic bridge configuration is invalid: {error}"
        ) from error
    dynamic = dynamic_config.to_wire()
    target_health = dynamic["target_health"]

    encounter = request_value.get("encounter")
    if not isinstance(encounter, Mapping):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "simulator request encounter must be an object"
        )
    encounter = dict(encounter)
    request_value["encounter"] = encounter
    # The Go dynamic loader enforces health-mode internally.  Mirror that
    # mutation in the content-addressed request so Python provenance describes
    # the exact simulator semantics instead of a duration-mode lookalike.
    encounter["useHealth"] = True
    request_targets = encounter.get("targets")
    if not isinstance(request_targets, list):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "simulator request encounter.targets must be an array"
        )
    if len(request_targets) != len(typed_target_health):
        raise ChronicleExternalTeamBackgroundGeneratorV2Error(
            "simulator request encounter.targets count differs from target registry"
        )
    for index, hypothesis_target in enumerate(typed_target_health):
        request_target = request_targets[index]
        if not isinstance(request_target, Mapping):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                f"simulator request encounter.targets[{index}] must be an object"
            )
        request_target = dict(request_target)
        request_targets[index] = request_target
        stats = request_target.get("stats")
        if not isinstance(stats, list):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                f"simulator request encounter.targets[{index}].stats must be an array"
            )
        stats = list(stats)
        if len(stats) <= _HEALTH_STAT_INDEX:
            stats.extend(0.0 for _ in range(_HEALTH_STAT_INDEX + 1 - len(stats)))
        stats[_HEALTH_STAT_INDEX] = hypothesis_target.health
        request_target["stats"] = stats
        if struct.pack(">d", float(stats[_HEALTH_STAT_INDEX])) != struct.pack(
            ">d", hypothesis_target.health
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                f"simulator target {index} health IEEE-754 bits differ from hypothesis"
            )
    request_round_trip = json.loads(_canonical_bytes(request_value))
    round_trip_targets = request_round_trip["encounter"]["targets"]
    for index, hypothesis_target in enumerate(typed_target_health):
        round_trip_health = round_trip_targets[index]["stats"][_HEALTH_STAT_INDEX]
        if struct.pack(">d", float(round_trip_health)) != struct.pack(
            ">d", hypothesis_target.health
        ):
            raise ChronicleExternalTeamBackgroundGeneratorV2Error(
                f"serialized simulator target {index} health IEEE-754 bits differ from hypothesis"
            )
    wire_request = {
        "command": "load_dynamic_v1",
        "request": request_value,
        "seed": seed,
        "dynamic": dynamic,
    }
    request_content_sha = _sha256_bytes(_canonical_bytes(request_value))
    hypothesis_binding = {
        "hypothesis_id": hypothesis_id,
        "provenance": provenance,
        "target_health": target_health,
    }
    hypothesis_content_sha = _sha256_bytes(_canonical_bytes(hypothesis_binding))
    configuration_binding_sha = _sha256_bytes(
        _canonical_bytes(
            {
                "draw_content_sha256": _verify_content_address(
                    draw, label="background draw"
                ),
                "draw_schedule_content_sha256": draw.get(
                    "schedule_content_sha256"
                ),
                "target_health_hypothesis_content_sha256": (
                    hypothesis_content_sha
                ),
                "wire_dynamic_content_sha256": dynamic["content_sha256"],
                "request_sha256": request_content_sha,
            }
        )
    )
    adapter_provenance = {
        "schema": ADAPTER_SCHEMA,
        "status": STATUS,
        "draw_content_sha256": _verify_content_address(
            draw, label="background draw"
        ),
        "draw_schedule_content_sha256": draw.get("schedule_content_sha256"),
        "target_health_hypothesis": {
            "hypothesis_id": hypothesis_id,
            "provenance": provenance,
            "status": "EXTERNAL_HYPOTHESIS_NONCOMPARISON",
            "content_sha256": hypothesis_content_sha,
            "observed_background_damage_used_as_initial_health": False,
        },
        "wire_dynamic_content_sha256": dynamic["content_sha256"],
        "request_sha256": request_content_sha,
        "configuration_binding_sha256": configuration_binding_sha,
        "same_timestamp_order": SAME_TIMESTAMP_ORDER,
        "future_dead_target_events_cancelled_by": "simulator receipt",
        "hypothesis_metadata_sent_to_physics_kernel": False,
        "request_target_health_written_from_hypothesis": True,
        "request_target_health_stat_index": _HEALTH_STAT_INDEX,
        "dynamic_digest_float_encoding": "IEEE754_BINARY64_HEX",
        "voting_eligible": False,
        "comparison_ready": False,
    }
    return ExternalDynamicScheduleAdapterV1(
        wire_request=wire_request,
        dynamic_config=dynamic_config,
        provenance=adapter_provenance,
        comparison_ready=False,
        voting_eligible=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build External Chronicle whole-wave team backgrounds V2"
    )
    parser.add_argument(
        "--team-model-manifest", type=Path, default=DEFAULT_TEAM_MODEL_MANIFEST
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_external_team_background_generator(
            team_model_manifest_path=args.team_model_manifest,
            output_directory=args.output_dir,
            workers=args.workers,
        )
    except ChronicleExternalTeamBackgroundGeneratorV2Error as error:
        print(f"Chronicle External team-background V2 failed: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ADAPTER_SCHEMA",
    "ChronicleExternalTeamBackgroundGeneratorV2Error",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_TEAM_MODEL_MANIFEST",
    "DRAW_SCHEMA",
    "DYNAMIC_SCHEMA",
    "ExternalDynamicScheduleAdapterV1",
    "IMPLEMENTATION_REVISION",
    "MAX_WORKERS",
    "PARTITION_RECORD_SCHEMA",
    "RETARGET_NEXT_ALIVE_CYCLIC",
    "RETARGET_REQUIRE_EXPLICIT",
    "SCHEMA",
    "STATUS",
    "TargetHealthHypothesisV1",
    "adapt_draw_to_load_dynamic_v1",
    "build_external_team_background_generator",
    "draw_external_team_background_schedule",
    "load_external_team_background_generator_manifest",
    "main",
    "validate_external_team_background_draw",
]
