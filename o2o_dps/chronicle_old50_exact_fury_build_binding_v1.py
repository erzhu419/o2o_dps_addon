"""Bind each old-50 exact-Fury wave to that player's historical build.

The dynamic-v3 adapter used to compile all waves from one caller-owned base
request.  That request is a wave/environment template, not evidence of the
selected historical Fury player's equipment or talents.  This module closes
that identity gap from the causal CombatantInfo build catalogue.

The binding is intentionally fail-closed:

* identity is the exact ``(instance_id, selected_guid)`` pair;
* every retained focal EventMeta anchor is joined with the latest INFO at or
  before that anchor (future INFO is never used); a wave with no focal events
  may use its content-addressed overlay's exact teammate EventMeta span;
* one static request is emitted only when the same catalogue segment covers
  the whole wave and its equipment/talents are simulator-admitted;
* the destination wave's encounter, team context, resources, execution
  settings and simulator seed are preserved byte-for-byte as JSON values.

For the 30 materialized adapter rows with no retained action sample, callers
may supply the matching exact-Fury overlay row.  The overlay retains all exact
focal event anchors, including outcome-context events that the training-only
adapter deliberately removed.  An identity or instance-manifest row without
an EventMeta anchor remains blocked rather than receiving a raid-wide default
build.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import gzip
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import chronicle_old50_exact_fury_dynamic_v3_adapter_v1 as adapter_v1
from . import chronicle_old50_exact_fury_slot_overlay_v1 as overlay_v1
from .fury_paired_multiseed_runner_v2 import sha256_json
from .historical_build_catalog_v1 import (
    AMBIGUOUS,
    IMPLEMENTATION_REVISION as CATALOG_IMPLEMENTATION_REVISION,
    MISSING,
    OBSERVED_EMPTY,
    OBSERVED_EQUIPPED,
    RECORD_SCHEMA as CATALOG_RECORD_SCHEMA,
    SCHEMA as CATALOG_SCHEMA,
    SIMULATOR_RELEVANT_SLOTS,
    HistoricalBuildCatalogError,
    historical_segment_to_character_profile,
    select_prefix_segment,
)
from .wowsims_profile import WowsimsProfileError, build_wowsims_profile


JSONMap = dict[str, Any]
SCHEMA = "chronicle_old50_exact_fury_build_binding/v1"
QUERY_SCHEMA = "chronicle_old50_exact_fury_build_query/v1"
AUDIT_SCHEMA = "chronicle_old50_exact_fury_build_coverage/v1"
IMPLEMENTATION_REVISION = "v1.0_exact_guid_causal_prefix_static_build_binding"


class ChronicleOld50ExactFuryBuildBindingV1Error(ValueError):
    """A source artifact or catalogue violates the binding contract."""


@dataclass(frozen=True)
class Old50WaveBuildQueryV1:
    instance_id: str
    encounter_id: str
    encounter_ordinal: int
    wave_ordinal: int
    selected_guid: str
    focal_order_keys: tuple[tuple[int, int, int, int, int], ...]
    anchor_source: str
    source_ref: Mapping[str, Any]
    destination_request: Mapping[str, Any] | None = None

    def identity(self) -> JSONMap:
        return {
            "instance_id": self.instance_id,
            "encounter_id": self.encounter_id,
            "encounter_ordinal": self.encounter_ordinal,
            "wave_ordinal": self.wave_ordinal,
            "selected_guid": self.selected_guid,
        }

    def as_dict(self) -> JSONMap:
        return {
            "schema": QUERY_SCHEMA,
            "identity": self.identity(),
            "focal_order_keys": [list(value) for value in self.focal_order_keys],
            "anchor_source": self.anchor_source,
            "source_ref": deepcopy(dict(self.source_ref)),
            "destination_request": deepcopy(self.destination_request),
        }


@dataclass(frozen=True)
class HistoricalBuildCatalogIndexV1:
    manifest_path: Path
    catalog_path: Path
    catalog_line_count: int
    resolution_mode: str
    segments_by_membership: Mapping[
        tuple[str, str], tuple[Mapping[str, Any], ...]
    ]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleOld50ExactFuryBuildBindingV1Error(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChronicleOld50ExactFuryBuildBindingV1Error(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"{label} must be a non-empty trimmed string"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _strict_json(value: Any, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"{label} must be strict JSON: {error}"
        ) from error


def _order_keys(value: Any, label: str) -> tuple[tuple[int, int, int, int, int], ...]:
    raw = _array(value, label)
    result: list[tuple[int, int, int, int, int]] = []
    for index, raw_key in enumerate(raw):
        parts = _array(raw_key, f"{label}[{index}]")
        if len(parts) != 5:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                f"{label}[{index}] must contain five EventMeta order components"
            )
        key = tuple(
            _integer(part, f"{label}[{index}][{part_index}]")
            for part_index, part in enumerate(parts)
        )
        result.append(key)  # type: ignore[arg-type]
    if result != sorted(set(result)):
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"{label} must be strictly increasing and unique"
        )
    return tuple(result)


def _query_identity(value: Mapping[str, Any]) -> tuple[str, str, int, int, str]:
    return (
        _text(value.get("instance_id"), "identity.instance_id"),
        _text(value.get("encounter_id"), "identity.encounter_id"),
        _integer(value.get("encounter_ordinal"), "identity.encounter_ordinal"),
        _integer(value.get("wave_ordinal"), "identity.wave_ordinal"),
        _text(value.get("selected_guid"), "identity.selected_guid"),
    )


def extract_old50_wave_build_query_v1(
    source: Mapping[str, Any] | Old50WaveBuildQueryV1,
) -> Old50WaveBuildQueryV1:
    """Extract the exact wave/GUID and all available causal focal anchors."""

    if isinstance(source, Old50WaveBuildQueryV1):
        return source
    raw = _mapping(source, "old50 build source")
    schema = raw.get("schema")
    if schema == adapter_v1.SCHEMA:
        try:
            validated = adapter_v1.validate_exact_fury_overlay_dynamic_v3_structure_v1(
                raw
            )
        except Exception as error:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                f"invalid dynamic-v3 adapter row: {error}"
            ) from error
        bindings = _mapping(validated.get("source_bindings"), "adapter bindings")
        join = _mapping(bindings.get("wave_identity_join"), "adapter wave join")
        join_key = _array(join.get("join_key"), "adapter join_key")
        if len(join_key) != 3:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                "adapter join_key must contain instance, encounter and wave ordinal"
            )
        training = _mapping(
            validated.get("expert_training_projection"), "adapter training projection"
        )
        samples = _array(training.get("samples"), "adapter training samples")
        keys = _order_keys(
            [
                _array(_mapping(sample, "adapter sample").get("order_key"), "sample order")
                for sample in samples
            ],
            "adapter focal action order keys",
        )
        encounter_ordinal = keys[0][0] if keys else 0
        # An empty adapter training projection does not retain encounter ordinal.
        # ``wave_ordinal`` is not a safe substitute, so mark the missing value as
        # zero only for the query identity; no build can be selected without keys.
        if not keys:
            encounter_ordinal = 0
        scenario = _mapping(validated.get("scenario"), "adapter scenario")
        return Old50WaveBuildQueryV1(
            instance_id=_text(join_key[0], "adapter instance_id"),
            encounter_id=_text(join_key[1], "adapter encounter_id"),
            encounter_ordinal=encounter_ordinal,
            wave_ordinal=_integer(join_key[2], "adapter wave_ordinal"),
            selected_guid=_text(training.get("selected_guid"), "adapter selected_guid"),
            focal_order_keys=keys,
            anchor_source="ADAPTER_OBSERVED_ACTION_SAMPLES",
            source_ref={
                "schema": schema,
                "content_sha256": _mapping(
                    validated.get("content_address"), "adapter content address"
                ).get("sha256"),
                "overlay_wave_content_sha256": bindings.get(
                    "overlay_wave_content_sha256"
                ),
            },
            destination_request=deepcopy(
                _mapping(scenario.get("request"), "adapter scenario request")
            ),
        )
    if schema == overlay_v1.RECORD_SCHEMA:
        try:
            validated = overlay_v1.validate_wave_overlay_v1(raw)
        except Exception as error:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                f"invalid exact-Fury overlay row: {error}"
            ) from error
        identity = _mapping(validated.get("identity"), "overlay identity")
        episode = _mapping(
            validated.get("focal_player_episode"), "overlay focal episode"
        )
        refs = _array(episode.get("target_trace_refs"), "overlay target trace refs")
        if refs:
            keys = _order_keys(
                [
                    _array(
                        _mapping(ref, "overlay trace ref").get("order_key"),
                        "ref order",
                    )
                    for ref in refs
                ],
                "overlay focal event order keys",
            )
            anchor_source = "OVERLAY_ALL_EXACT_FOCAL_EVENTS"
        else:
            projection = _mapping(
                validated.get("exact_guid_loo_teammate_schedule"),
                "overlay teammate schedule",
            )
            schedule = _array(
                projection.get("projected_schedule"), "overlay projected schedule"
            )
            keys = _order_keys(
                [
                    _array(
                        _mapping(event, "overlay teammate event").get(
                            "source_eventmeta_order_key"
                        ),
                        "teammate source EventMeta order",
                    )
                    for event in schedule
                ],
                "overlay exact teammate event span order keys",
            )
            anchor_source = (
                "OVERLAY_EXACT_TEAM_EVENT_SPAN_FALLBACK"
                if keys
                else "OVERLAY_HAS_NO_EXACT_WAVE_EVENTMETA"
            )
        instance, encounter, declared_encounter_ordinal, wave, guid = _query_identity(
            identity
        )
        encounter_ordinal = (
            keys[0][0] if keys else declared_encounter_ordinal
        )
        return Old50WaveBuildQueryV1(
            instance_id=instance,
            encounter_id=encounter,
            encounter_ordinal=encounter_ordinal,
            wave_ordinal=wave,
            selected_guid=guid,
            focal_order_keys=keys,
            anchor_source=anchor_source,
            source_ref={
                "schema": schema,
                "content_sha256": _mapping(
                    validated.get("content_address"), "overlay content address"
                ).get("sha256"),
            },
            destination_request=None,
        )
    if schema == QUERY_SCHEMA:
        identity = _mapping(raw.get("identity"), "build query identity")
        instance, encounter, encounter_ordinal, wave, guid = _query_identity(identity)
        keys = _order_keys(raw.get("focal_order_keys", []), "query focal_order_keys")
        destination = raw.get("destination_request")
        return Old50WaveBuildQueryV1(
            instance_id=instance,
            encounter_id=encounter,
            encounter_ordinal=encounter_ordinal,
            wave_ordinal=wave,
            selected_guid=guid,
            focal_order_keys=keys,
            anchor_source=_text(raw.get("anchor_source"), "query anchor_source"),
            source_ref=deepcopy(dict(_mapping(raw.get("source_ref"), "query source_ref"))),
            destination_request=(
                deepcopy(dict(_mapping(destination, "query destination_request")))
                if destination is not None
                else None
            ),
        )
    # A wave identity still has no causal EventMeta anchor.  Retain all exact
    # identity fields in the blocked result rather than silently turning the
    # identity into an instance-wide lookup.
    if all(
        field in raw
        for field in (
            "instance_id",
            "encounter_id",
            "encounter_ordinal",
            "wave_ordinal",
            "selected_guid",
        )
    ):
        instance, encounter, encounter_ordinal, wave, guid = _query_identity(raw)
        return Old50WaveBuildQueryV1(
            instance_id=instance,
            encounter_id=encounter,
            encounter_ordinal=encounter_ordinal,
            wave_ordinal=wave,
            selected_guid=guid,
            focal_order_keys=(),
            anchor_source="WAVE_IDENTITY_HAS_NO_EVENTMETA",
            source_ref={"schema": schema or "UNVERSIONED_WAVE_IDENTITY"},
            destination_request=None,
        )
    # A materialized instance-manifest row has a GUID but no wave identity or
    # EventMeta anchor.  Preserve the reason as a normal blocked query rather
    # than guessing one build for the whole raid.
    if "instance_id" in raw and "selected_guid" in raw:
        return Old50WaveBuildQueryV1(
            instance_id=_text(raw.get("instance_id"), "manifest row instance_id"),
            encounter_id="UNAVAILABLE_FROM_INSTANCE_MANIFEST_ROW",
            encounter_ordinal=0,
            wave_ordinal=0,
            selected_guid=_text(raw.get("selected_guid"), "manifest row selected_guid"),
            focal_order_keys=(),
            anchor_source="INSTANCE_MANIFEST_ROW_HAS_NO_WAVE_EVENTMETA",
            source_ref={"schema": schema or "UNVERSIONED_INSTANCE_MANIFEST_ROW"},
            destination_request=None,
        )
    raise ChronicleOld50ExactFuryBuildBindingV1Error(
        f"unsupported old50 build source schema: {schema!r}"
    )


def _resolve_catalog_data(
    manifest: Mapping[str, Any], manifest_path: Path, override: str | Path | None
) -> tuple[Path, str]:
    if override is not None:
        path = Path(override).expanduser().resolve()
        mode = "CALLER_EXPLICIT_RELOCATION"
    else:
        declared_text = _text(manifest.get("catalog_path"), "catalog catalog_path")
        declared = Path(declared_text)
        if declared.is_file():
            path, mode = declared.resolve(), "MANIFEST_DECLARED_PATH"
        else:
            declared_name = (
                PureWindowsPath(declared_text).name
                if "\\" in declared_text
                else PurePosixPath(declared_text).name
            )
            relocated = (manifest_path.parent / declared_name).resolve()
            path, mode = relocated, "MANIFEST_SIBLING_BASENAME_RELOCATION"
    if not path.is_file():
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"historical build catalog data is missing: {path}"
        )
    return path, mode


def load_historical_build_catalog_index_v1(
    manifest_path: str | Path,
    *,
    memberships: Iterable[tuple[str, str]] | None = None,
    catalog_data_path: str | Path | None = None,
) -> HistoricalBuildCatalogIndexV1:
    """Load only requested exact instance/GUID memberships from the catalogue."""

    path = Path(manifest_path).expanduser().resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"cannot read historical build catalog manifest {path}: {error}"
        ) from error
    manifest = _mapping(manifest, "historical build catalog manifest")
    causal = _mapping(manifest.get("causal_contract"), "catalog causal contract")
    if (
        manifest.get("schema") != CATALOG_SCHEMA
        or manifest.get("implementation_revision") != CATALOG_IMPLEMENTATION_REVISION
        or manifest.get("kind") != "historical_build_catalog_manifest"
        or causal.get("policy_join")
        != "LATEST_INFO_ANCHOR_AT_OR_BEFORE_DECISION_ONLY"
        or causal.get("future_info_backfill_allowed") is not False
    ):
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            "historical build catalog implementation or causal contract differs"
        )
    catalog_path, resolution_mode = _resolve_catalog_data(
        manifest, path, catalog_data_path
    )
    wanted = set(memberships) if memberships is not None else None
    retained: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    seen_segment_identities: set[tuple[str, str, str, str, str]] = set()
    line_count = 0
    try:
        with gzip.open(catalog_path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise ChronicleOld50ExactFuryBuildBindingV1Error(
                        f"catalog line {line_number} is blank"
                    )
                line_count = line_number
                value = json.loads(line)
                segment = dict(_mapping(value, f"catalog line {line_number}"))
                if segment.get("schema") != CATALOG_RECORD_SCHEMA:
                    raise ChronicleOld50ExactFuryBuildBindingV1Error(
                        f"catalog line {line_number} has unsupported schema"
                    )
                identity = _mapping(segment.get("identity"), "catalog segment identity")
                instance_id = _text(identity.get("instance_id"), "catalog instance_id")
                guid = _text(identity.get("player_guid"), "catalog player_guid")
                full_identity = (
                    _text(identity.get("server"), "catalog server"),
                    _text(identity.get("realm"), "catalog realm"),
                    guid,
                    instance_id,
                    _text(identity.get("build_segment_id"), "catalog build_segment_id"),
                )
                if full_identity in seen_segment_identities:
                    raise ChronicleOld50ExactFuryBuildBindingV1Error(
                        "historical build catalog contains a duplicate segment identity"
                    )
                seen_segment_identities.add(full_identity)
                membership = (instance_id, guid)
                if wanted is None or membership in wanted:
                    segment["_catalog_line_number"] = line_number
                    retained[membership].append(segment)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"cannot scan historical build catalog {catalog_path}: {error}"
        ) from error
    summary = _mapping(manifest.get("summary"), "catalog summary")
    declared_count = _integer(
        summary.get("build_segment_count"), "catalog summary.build_segment_count"
    )
    if line_count != declared_count:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            "catalog row count differs from its manifest summary"
        )
    for segments in retained.values():
        segments.sort(
            key=lambda segment: (
                _mapping(
                    _mapping(segment.get("observation"), "catalog observation").get(
                        "valid_from"
                    ),
                    "catalog valid_from",
                ).get("timestamp_ms", -1)
                if _mapping(segment.get("observation"), "catalog observation").get(
                    "valid_from"
                )
                is not None
                else -1,
                _mapping(segment.get("identity"), "catalog identity").get(
                    "build_segment_id"
                ),
            )
        )
    return HistoricalBuildCatalogIndexV1(
        manifest_path=path,
        catalog_path=catalog_path,
        catalog_line_count=line_count,
        resolution_mode=resolution_mode,
        segments_by_membership={
            key: tuple(value) for key, value in retained.items()
        },
    )


def _static_character_blockers(segment: Mapping[str, Any]) -> list[JSONMap]:
    blockers: list[JSONMap] = []
    player = _mapping(segment.get("player"), "historical player")
    if str(player.get("hero_class") or "").upper() != "WARRIOR":
        blockers.append({"code": "SELECTED_GUID_IS_NOT_WARRIOR"})
    if not isinstance(player.get("name"), str) or not player.get("name"):
        blockers.append({"code": "HISTORICAL_CHARACTER_NAME_MISSING"})
    talents = _mapping(segment.get("talents"), "historical talents")
    if talents.get("translation_status") != "TRANSLATED_EXACT":
        blockers.append(
            {
                "code": "HISTORICAL_TALENTS_NOT_TRANSLATED_EXACT",
                "translation_status": talents.get("translation_status"),
                "translation_reason": talents.get("translation_reason"),
            }
        )
    equipment = _mapping(segment.get("equipment"), "historical equipment")
    slots = _array(equipment.get("slots"), "historical equipment slots")
    by_slot: dict[int, Mapping[str, Any]] = {}
    for raw_slot in slots:
        slot = _mapping(raw_slot, "historical equipment slot")
        number = _integer(slot.get("inventory_slot"), "historical inventory_slot", minimum=1)
        if number in by_slot:
            blockers.append({"code": "HISTORICAL_EQUIPMENT_SLOT_DUPLICATED", "slot": number})
        by_slot[number] = slot
    for number in sorted(SIMULATOR_RELEVANT_SLOTS):
        slot = by_slot.get(number)
        if slot is None:
            blockers.append({"code": "HISTORICAL_EQUIPMENT_SLOT_ABSENT", "slot": number})
            continue
        status = slot.get("status")
        if status in {MISSING, AMBIGUOUS}:
            blockers.append(
                {
                    "code": "HISTORICAL_EQUIPMENT_SLOT_NOT_EXACT",
                    "slot": number,
                    "status": status,
                }
            )
        elif status not in {OBSERVED_EQUIPPED, OBSERVED_EMPTY}:
            blockers.append(
                {
                    "code": "HISTORICAL_EQUIPMENT_SLOT_STATUS_UNSUPPORTED",
                    "slot": number,
                    "status": status,
                }
            )
    return blockers


def _portable_character(segment: Mapping[str, Any]) -> JSONMap:
    identity = _mapping(segment.get("identity"), "historical identity")
    observation = _mapping(segment.get("observation"), "historical observation")
    equipment = _mapping(segment.get("equipment"), "historical equipment")
    talents = _mapping(segment.get("talents"), "historical talents")
    return {
        "identity": deepcopy(dict(identity)),
        "player": deepcopy(dict(_mapping(segment.get("player"), "historical player"))),
        "valid_from": deepcopy(observation.get("valid_from")),
        "valid_until_or_unknown": deepcopy(observation.get("valid_until_or_unknown")),
        "equipment": {
            "slots": deepcopy(_array(equipment.get("slots"), "equipment slots")),
            "item_dataset": equipment.get("item_dataset"),
        },
        "talents": {
            "original_summary": deepcopy(talents.get("original_summary")),
            "original_tree_rank_strings": deepcopy(
                talents.get("original_tree_rank_strings")
            ),
            "semantic_ranks": deepcopy(talents.get("semantic_ranks")),
            "translation_version": talents.get("translation_version"),
            "translation_status": talents.get("translation_status"),
        },
    }


_CHARACTER_FIELDS = frozenset(("name", "race", "class", "equipment", "talentsString"))


def _non_character_projection(request: Mapping[str, Any]) -> JSONMap:
    copied = deepcopy(dict(request))
    raid = _mapping(copied.get("raid"), "request raid")
    parties = _array(raid.get("parties"), "request parties")
    player = _mapping(
        parties[0],
        "request first party",
    )
    players = _array(player.get("players"), "request first-party players")
    focal = dict(_mapping(players[0], "request focal player"))
    for field in _CHARACTER_FIELDS:
        focal.pop(field, None)
    warrior = focal.get("warrior")
    if isinstance(warrior, Mapping):
        warrior_copy = deepcopy(dict(warrior))
        options = warrior_copy.get("options")
        if isinstance(options, Mapping):
            options_copy = deepcopy(dict(options))
            options_copy.pop("ravagerRank", None)
            warrior_copy["options"] = options_copy
        focal["warrior"] = warrior_copy
    players[0] = focal
    player["players"] = players
    parties[0] = dict(player)
    copied["raid"]["parties"] = parties  # type: ignore[index]
    return copied


def _bind_request(
    *,
    query: Old50WaveBuildQueryV1,
    segment: Mapping[str, Any],
    catalog_index: HistoricalBuildCatalogIndexV1,
) -> tuple[JSONMap | None, JSONMap, list[JSONMap]]:
    if query.destination_request is None:
        return None, {
            "status": "NOT_REQUESTED_IDENTITY_ONLY",
            "encounter_preserved": None,
            "team_resources_execution_seed_preserved": None,
        }, []
    request = deepcopy(dict(_mapping(query.destination_request, "destination request")))
    raid = _mapping(request.get("raid"), "destination raid")
    parties = _array(raid.get("parties"), "destination parties")
    if not parties:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            "destination request has no primary party"
        )
    primary = _mapping(parties[0], "destination primary party")
    players = _array(primary.get("players"), "destination primary-party players")
    if not players:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            "destination request has no focal player slot"
        )
    base_player = _mapping(players[0], "destination focal player")
    try:
        profile = historical_segment_to_character_profile(
            segment,
            catalog_path=catalog_index.catalog_path,
            catalog_line_number=_integer(
                segment.get("_catalog_line_number"), "catalog line number", minimum=1
            ),
            consumes=deepcopy(base_player.get("consumes", {})),
            database=deepcopy(base_player.get("database", {})),
        )
        bound, _ = build_wowsims_profile(request, profile.selection)
    except (HistoricalBuildCatalogError, WowsimsProfileError, TypeError, ValueError) as error:
        return None, {
            "status": "SIMULATOR_CHARACTER_PROJECTION_REJECTED",
            "encounter_preserved": None,
            "team_resources_execution_seed_preserved": None,
        }, [{"code": "SIMULATOR_CHARACTER_PROJECTION_REJECTED", "detail": str(error)}]
    before = _strict_json(request, "destination request")
    after = _strict_json(bound, "bound request")
    encounter_equal = before.get("encounter") == after.get("encounter")
    noncharacter_equal = _non_character_projection(before) == _non_character_projection(after)
    sim_options_equal = before.get("simOptions") == after.get("simOptions")
    blockers: list[JSONMap] = []
    if not encounter_equal:
        blockers.append({"code": "DESTINATION_ENCOUNTER_CHANGED"})
    if not noncharacter_equal:
        blockers.append({"code": "DESTINATION_TEAM_RESOURCE_OR_EXECUTION_CHANGED"})
    if not sim_options_equal:
        blockers.append({"code": "DESTINATION_SIM_OPTIONS_OR_SEED_CHANGED"})
    receipt = {
        "status": "PASS" if not blockers else "FAILED",
        "encounter_preserved": encounter_equal,
        "team_resources_execution_seed_preserved": (
            noncharacter_equal and sim_options_equal
        ),
        "character_fields_replaced": sorted(_CHARACTER_FIELDS),
        "ravager_rank_source": "EXACT_HISTORICAL_TALENT_TRANSLATION",
        "caller_base_equipment_or_talents_retained": False,
        "consumes_source": "DESTINATION_WAVE_RESOURCE_CONTEXT_NOT_COMBATANT_INFO",
        "request_sha256": sha256_json(after) if not blockers else None,
    }
    return (after if not blockers else None), receipt, blockers


def _blocked_result(
    query: Old50WaveBuildQueryV1,
    blockers: Sequence[Mapping[str, Any]],
    *,
    selected_segment: Mapping[str, Any] | None = None,
    portable_character: Mapping[str, Any] | None = None,
    request_receipt: Mapping[str, Any] | None = None,
    exact_guid_same_instance: bool | None = None,
    causal_prefix_selected: bool | None = None,
    equipment_evidence_complete: bool = False,
    talent_semantics_exact: bool = False,
) -> JSONMap:
    if exact_guid_same_instance is None:
        exact_guid_same_instance = selected_segment is not None
    if causal_prefix_selected is None:
        causal_prefix_selected = selected_segment is not None
    return {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": "BLOCKED",
        "wave_identity": query.identity(),
        "anchor_source": query.anchor_source,
        "focal_anchor_count": len(query.focal_order_keys),
        "source_ref": deepcopy(dict(query.source_ref)),
        "selected_catalog_segment": deepcopy(selected_segment),
        "portable_character": deepcopy(portable_character),
        "coverage": {
            "exact_guid_same_instance": exact_guid_same_instance,
            "causal_prefix_selected": causal_prefix_selected,
            "equipment_evidence_complete": equipment_evidence_complete,
            "talent_semantics_exact": talent_semantics_exact,
            "simulator_runtime_executable": False,
            "request_emitted": False,
            "blockers": [deepcopy(dict(value)) for value in blockers],
        },
        "destination_preservation": deepcopy(request_receipt),
        "request": None,
    }


def _bind_query(
    query: Old50WaveBuildQueryV1,
    catalog_index: HistoricalBuildCatalogIndexV1,
) -> JSONMap:
    if not query.focal_order_keys:
        return _blocked_result(
            query,
            [{"code": "NO_CAUSAL_FOCAL_EVENT_ANCHOR", "source": query.anchor_source}],
        )
    membership = (query.instance_id, query.selected_guid)
    segments = tuple(catalog_index.segments_by_membership.get(membership, ()))
    if not segments:
        return _blocked_result(query, [{"code": "EXACT_GUID_INSTANCE_NOT_IN_CATALOG"}])
    realms = {
        (
            _mapping(segment.get("identity"), "catalog identity").get("server"),
            _mapping(segment.get("identity"), "catalog identity").get("realm"),
        )
        for segment in segments
    }
    if len(realms) != 1:
        return _blocked_result(
            query,
            [{"code": "EXACT_GUID_INSTANCE_HAS_AMBIGUOUS_SERVER_REALM", "values": sorted(realms)}],
        )
    server, realm = next(iter(realms))
    selected: list[Mapping[str, Any]] = []
    missing: list[JSONMap] = []
    for anchor_index, order in enumerate(query.focal_order_keys):
        segment = select_prefix_segment(
            segments,
            server=str(server),
            realm=str(realm),
            player_guid=query.selected_guid,
            instance_id=query.instance_id,
            decision_timestamp_ms=order[1],
            encounter_id=query.encounter_id,
            decision_event_index=order[2],
        )
        if segment is None:
            missing.append(
                {
                    "code": "NO_PRIOR_COMBATANT_INFO",
                    "anchor_index": anchor_index,
                    "order_key": list(order),
                }
            )
        else:
            selected.append(segment)
    if missing:
        return _blocked_result(
            query,
            missing,
            exact_guid_same_instance=True,
            causal_prefix_selected=False,
        )
    identities = {
        tuple(
            _mapping(segment.get("identity"), "selected segment identity").get(key)
            for key in ("server", "realm", "player_guid", "instance_id", "build_segment_id")
        )
        for segment in selected
    }
    if len(identities) != 1:
        return _blocked_result(
            query,
            [
                {
                    "code": "HISTORICAL_BUILD_CHANGED_WITHIN_WAVE",
                    "selected_segment_ids": sorted(str(value[-1]) for value in identities),
                }
            ],
            exact_guid_same_instance=True,
            causal_prefix_selected=True,
        )
    segment = selected[0]
    identity = _mapping(segment.get("identity"), "selected segment identity")
    observation = _mapping(segment.get("observation"), "selected segment observation")
    selection_receipt = {
        "catalog_line_number": _integer(
            segment.get("_catalog_line_number"), "selected catalog line", minimum=1
        ),
        "identity": deepcopy(dict(identity)),
        "valid_from": deepcopy(observation.get("valid_from")),
        "valid_until_or_unknown": deepcopy(observation.get("valid_until_or_unknown")),
        "selection_contract": "LATEST_INFO_ANCHOR_AT_OR_BEFORE_EVERY_FOCAL_EVENT",
        "all_focal_events_select_same_segment": True,
    }
    portable = _portable_character(segment)
    static_blockers = _static_character_blockers(segment)
    coverage = _mapping(segment.get("coverage"), "selected segment coverage")
    if static_blockers:
        blocker_codes = {str(value.get("code")) for value in static_blockers}
        return _blocked_result(
            query,
            static_blockers,
            selected_segment=selection_receipt,
            portable_character=portable,
            equipment_evidence_complete=not any(
                code.startswith("HISTORICAL_EQUIPMENT_") for code in blocker_codes
            ),
            talent_semantics_exact=(
                "HISTORICAL_TALENTS_NOT_TRANSLATED_EXACT" not in blocker_codes
            ),
        )
    if coverage.get("representative_build_eligible") is not True:
        representativeness = _mapping(
            coverage.get("historical_representativeness"),
            "historical representativeness",
        )
        return _blocked_result(
            query,
            [
                {
                    "code": "HISTORICAL_BUILD_NOT_REPRESENTATIVE_EXECUTABLE",
                    "reasons": deepcopy(representativeness.get("reasons", [])),
                }
            ],
            selected_segment=selection_receipt,
            portable_character=portable,
            equipment_evidence_complete=True,
            talent_semantics_exact=True,
        )
    if coverage.get("runtime_executable") is not True:
        return _blocked_result(
            query,
            [
                {
                    "code": "HISTORICAL_BUILD_SIMULATOR_COVERAGE_INCOMPLETE",
                    "reasons": deepcopy(coverage.get("uncertainty", [])),
                }
            ],
            selected_segment=selection_receipt,
            portable_character=portable,
            equipment_evidence_complete=True,
            talent_semantics_exact=True,
        )
    request, preservation, request_blockers = _bind_request(
        query=query, segment=segment, catalog_index=catalog_index
    )
    if request_blockers:
        return _blocked_result(
            query,
            request_blockers,
            selected_segment=selection_receipt,
            portable_character=portable,
            request_receipt=preservation,
            equipment_evidence_complete=True,
            talent_semantics_exact=True,
        )
    status = "BOUND_EXECUTABLE_REQUEST" if request is not None else "BOUND_STATIC_CHARACTER"
    return {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": status,
        "wave_identity": query.identity(),
        "anchor_source": query.anchor_source,
        "focal_anchor_count": len(query.focal_order_keys),
        "source_ref": deepcopy(dict(query.source_ref)),
        "selected_catalog_segment": selection_receipt,
        "portable_character": portable,
        "coverage": {
            "exact_guid_same_instance": True,
            "causal_prefix_selected": True,
            "equipment_evidence_complete": True,
            "talent_semantics_exact": True,
            "simulator_runtime_executable": True,
            "request_emitted": request is not None,
            "blockers": [],
        },
        "destination_preservation": preservation,
        "request": request,
    }


def bind_old50_wave_historical_build_v1(
    source: Mapping[str, Any] | Old50WaveBuildQueryV1,
    catalog_index: HistoricalBuildCatalogIndexV1,
    *,
    overlay_wave: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Bind one adapter/overlay/query row to its exact historical character."""

    query = extract_old50_wave_build_query_v1(source)
    if overlay_wave is not None:
        overlay_query = extract_old50_wave_build_query_v1(overlay_wave)
        if query.source_ref.get("schema") != adapter_v1.SCHEMA:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                "an overlay may augment only a validated dynamic-v3 adapter row"
            )
        if not _adapter_overlay_identity_compatible(query, overlay_query):
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                "adapter and overlay wave identities differ"
            )
        expected = query.source_ref.get("overlay_wave_content_sha256")
        actual = overlay_query.source_ref.get("content_sha256")
        if not isinstance(expected, str) or not expected or expected != actual:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                "adapter does not bind the supplied overlay row"
            )
        query = Old50WaveBuildQueryV1(
            instance_id=query.instance_id,
            encounter_id=query.encounter_id,
            encounter_ordinal=overlay_query.encounter_ordinal,
            wave_ordinal=query.wave_ordinal,
            selected_guid=query.selected_guid,
            focal_order_keys=overlay_query.focal_order_keys,
            anchor_source=overlay_query.anchor_source,
            source_ref={**dict(query.source_ref), "overlay_verified": True},
            destination_request=query.destination_request,
        )
    return _bind_query(query, catalog_index)


def _adapter_overlay_identity_compatible(
    adapter_query: Old50WaveBuildQueryV1,
    overlay_query: Old50WaveBuildQueryV1,
) -> bool:
    """Compare only identity fields actually retained by the adapter.

    A zero-action adapter row has no EventMeta from which to recover the
    encounter ordinal.  Its content-addressed overlay partner supplies that
    value.  All other wave/GUID fields remain mandatory, and a non-empty
    adapter must also agree on the ordinal inferred from its own action keys.
    """

    for field in ("instance_id", "encounter_id", "wave_ordinal", "selected_guid"):
        if getattr(adapter_query, field) != getattr(overlay_query, field):
            return False
    return (
        not adapter_query.focal_order_keys
        or adapter_query.encounter_ordinal == overlay_query.encounter_ordinal
    )


def _partition_rows(
    manifest_path: Path,
    *,
    manifest_schema: str,
    record_schema: str,
) -> Iterator[JSONMap]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"cannot read corpus manifest {manifest_path}: {error}"
        ) from error
    manifest = _mapping(manifest, "corpus manifest")
    if manifest.get("schema") != manifest_schema:
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            f"corpus manifest schema must be {manifest_schema}"
        )
    total = 0
    for raw_entry in _array(manifest.get("instances"), "corpus instances"):
        entry = _mapping(raw_entry, "corpus instance")
        partition = _mapping(entry.get("partition"), "corpus partition")
        if partition.get("record_schema") != record_schema:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                "corpus partition record schema differs"
            )
        declared = _integer(partition.get("record_count"), "partition record_count")
        relative = Path(_text(partition.get("path"), "partition path"))
        candidate = (
            relative.resolve()
            if relative.is_absolute()
            else (manifest_path.parent / relative).resolve()
        )
        count = 0
        try:
            with gzip.open(candidate, "rt", encoding="utf-8") as handle:
                for line in handle:
                    row = dict(_mapping(json.loads(line), "corpus row"))
                    if row.get("schema") != record_schema:
                        raise ChronicleOld50ExactFuryBuildBindingV1Error(
                            "corpus row schema differs"
                        )
                    count += 1
                    total += 1
                    yield row
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                f"cannot read corpus partition {candidate}: {error}"
            ) from error
        if count != declared:
            raise ChronicleOld50ExactFuryBuildBindingV1Error(
                "corpus partition row count differs from manifest"
            )
    summary = _mapping(manifest.get("summary"), "corpus summary")
    declared_total = summary.get("compiled_wave_count", summary.get("wave_overlay_count"))
    if declared_total is not None and total != _integer(
        declared_total, "corpus summary wave count"
    ):
        raise ChronicleOld50ExactFuryBuildBindingV1Error(
            "corpus total row count differs from manifest summary"
        )


def audit_old50_adapter_build_coverage_v1(
    *,
    adapter_manifest_path: str | Path,
    catalog_manifest_path: str | Path,
    overlay_manifest_path: str | Path | None = None,
    catalog_data_path: str | Path | None = None,
) -> JSONMap:
    """Audit all adapter waves without retaining their large request payloads."""

    adapter_path = Path(adapter_manifest_path).expanduser().resolve()
    adapter_rows = _partition_rows(
        adapter_path,
        manifest_schema=adapter_v1.MATERIALIZED_MANIFEST_SCHEMA,
        record_schema=adapter_v1.SCHEMA,
    )
    overlay_queries: dict[str, Old50WaveBuildQueryV1] = {}
    if overlay_manifest_path is not None:
        overlay_path = Path(overlay_manifest_path).expanduser().resolve()
        for row in _partition_rows(
            overlay_path,
            manifest_schema=overlay_v1.MANIFEST_SCHEMA,
            record_schema=overlay_v1.RECORD_SCHEMA,
        ):
            query = extract_old50_wave_build_query_v1(row)
            content_sha = _text(
                query.source_ref.get("content_sha256"), "overlay row content SHA"
            )
            if content_sha in overlay_queries:
                raise ChronicleOld50ExactFuryBuildBindingV1Error(
                    "overlay corpus duplicates a wave content identity"
                )
            overlay_queries[content_sha] = query

    # The adapter manifest has exactly one selected GUID per instance.  Read
    # these small rows first so the 26 MB build catalogue retains only needed
    # memberships while still verifying its declared total row count.
    manifest = _mapping(
        json.loads(adapter_path.read_text(encoding="utf-8")), "adapter manifest"
    )
    memberships = {
        (
            _text(entry.get("instance_id"), "adapter manifest instance_id"),
            _text(entry.get("selected_guid"), "adapter manifest selected_guid"),
        )
        for entry in (
            _mapping(value, "adapter manifest instance")
            for value in _array(manifest.get("instances"), "adapter manifest instances")
        )
    }
    catalog = load_historical_build_catalog_index_v1(
        catalog_manifest_path,
        memberships=memberships,
        catalog_data_path=catalog_data_path,
    )
    status_counts: Counter[str] = Counter()
    blocker_wave_counts: Counter[str] = Counter()
    blocker_occurrence_counts: Counter[str] = Counter()
    blocker_detail_wave_counts: Counter[str] = Counter()
    anchor_source_counts: Counter[str] = Counter()
    coverage_counts: Counter[str] = Counter()
    by_instance: dict[str, Counter[str]] = defaultdict(Counter)
    wave_results: list[JSONMap] = []
    for row in adapter_rows:
        adapter_query = extract_old50_wave_build_query_v1(row)
        overlay_query = None
        overlay_sha = adapter_query.source_ref.get("overlay_wave_content_sha256")
        if overlay_queries:
            overlay_query = overlay_queries.get(str(overlay_sha))
            if overlay_query is None:
                raise ChronicleOld50ExactFuryBuildBindingV1Error(
                    "adapter row has no exact content-addressed overlay partner"
                )
            if not _adapter_overlay_identity_compatible(adapter_query, overlay_query):
                raise ChronicleOld50ExactFuryBuildBindingV1Error(
                    "adapter/overlay identity mismatch during corpus audit"
                )
            query = Old50WaveBuildQueryV1(
                **{
                    **adapter_query.__dict__,
                    "encounter_ordinal": overlay_query.encounter_ordinal,
                    "focal_order_keys": overlay_query.focal_order_keys,
                    "anchor_source": overlay_query.anchor_source,
                }
            )
        else:
            query = adapter_query
        result = _bind_query(query, catalog)
        status = str(result["status"])
        status_counts[status] += 1
        anchor_source_counts[query.anchor_source] += 1
        instance_counter = by_instance[query.instance_id]
        instance_counter["wave_count"] += 1
        instance_counter[status] += 1
        blockers = [
            _mapping(value, "binding blocker")
            for value in result["coverage"]["blockers"]
        ]
        codes = [str(value.get("code")) for value in blockers]
        blocker_wave_counts.update(set(codes))
        blocker_occurrence_counts.update(codes)
        detail_labels: set[str] = set()
        for blocker in blockers:
            code = str(blocker.get("code"))
            reasons = blocker.get("reasons", [])
            if isinstance(reasons, list):
                for reason in reasons:
                    detail = (
                        reason
                        if isinstance(reason, str)
                        else json.dumps(
                            _strict_json(reason, "binding blocker reason"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    detail_labels.add(f"{code}:{detail}")
        blocker_detail_wave_counts.update(detail_labels)
        for field in (
            "exact_guid_same_instance",
            "causal_prefix_selected",
            "equipment_evidence_complete",
            "talent_semantics_exact",
            "simulator_runtime_executable",
            "request_emitted",
        ):
            if result["coverage"].get(field) is True:
                coverage_counts[field] += 1
        if all(
            result["coverage"].get(field) is True
            for field in (
                "causal_prefix_selected",
                "equipment_evidence_complete",
                "talent_semantics_exact",
            )
        ):
            coverage_counts["bound_static_character"] += 1
        wave_results.append(
            {
                "wave_identity": result["wave_identity"],
                "status": status,
                "anchor_source": query.anchor_source,
                "focal_anchor_count": len(query.focal_order_keys),
                "catalog_line_number": (
                    result["selected_catalog_segment"].get("catalog_line_number")
                    if isinstance(result.get("selected_catalog_segment"), Mapping)
                    else None
                ),
                "coverage": {
                    field: result["coverage"].get(field)
                    for field in (
                        "exact_guid_same_instance",
                        "causal_prefix_selected",
                        "equipment_evidence_complete",
                        "talent_semantics_exact",
                        "simulator_runtime_executable",
                        "request_emitted",
                    )
                },
                "blocker_codes": codes,
                "request_sha256": (
                    result.get("destination_preservation", {}) or {}
                ).get("request_sha256"),
            }
        )
    wave_results.sort(
        key=lambda value: (
            value["wave_identity"]["instance_id"],
            value["wave_identity"]["encounter_id"],
            value["wave_identity"]["wave_ordinal"],
        )
    )
    total = len(wave_results)
    executable = status_counts["BOUND_EXECUTABLE_REQUEST"]
    return {
        "schema": AUDIT_SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": "COMPLETE_COVERAGE_AUDIT",
        "inputs": {
            "adapter_manifest": str(adapter_path),
            "overlay_manifest": (
                str(Path(overlay_manifest_path).expanduser().resolve())
                if overlay_manifest_path is not None
                else None
            ),
            "catalog_manifest": str(catalog.manifest_path),
            "catalog_data": str(catalog.catalog_path),
            "catalog_resolution_mode": catalog.resolution_mode,
        },
        "summary": {
            "wave_count": total,
            "bound_static_character_count": coverage_counts[
                "bound_static_character"
            ],
            "executable_request_count": executable,
            "blocked_count": status_counts["BLOCKED"],
            "status_counts": dict(sorted(status_counts.items())),
            "blocker_counts": dict(sorted(blocker_wave_counts.items())),
            "blocker_occurrence_counts": dict(
                sorted(blocker_occurrence_counts.items())
            ),
            "blocker_detail_wave_counts": dict(
                sorted(blocker_detail_wave_counts.items())
            ),
            "anchor_source_counts": dict(sorted(anchor_source_counts.items())),
            "coverage_counts": dict(sorted(coverage_counts.items())),
            "caller_base_request_used_as_build_count": 0,
        },
        "by_instance": [
            {"instance_id": key, **dict(sorted(value.items()))}
            for key, value in sorted(by_instance.items())
        ],
        "waves": wave_results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit old50 exact-Fury wave to historical-build coverage"
    )
    parser.add_argument("--adapter-manifest", required=True)
    parser.add_argument("--catalog-manifest", required=True)
    parser.add_argument("--overlay-manifest")
    parser.add_argument("--catalog-data")
    parser.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = audit_old50_adapter_build_coverage_v1(
            adapter_manifest_path=arguments.adapter_manifest,
            catalog_manifest_path=arguments.catalog_manifest,
            overlay_manifest_path=arguments.overlay_manifest,
            catalog_data_path=arguments.catalog_data,
        )
    except (ChronicleOld50ExactFuryBuildBindingV1Error, OSError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "error": str(error)}, ensure_ascii=False))
        return 2
    if arguments.output:
        output = Path(arguments.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0


__all__ = (
    "AUDIT_SCHEMA",
    "ChronicleOld50ExactFuryBuildBindingV1Error",
    "HistoricalBuildCatalogIndexV1",
    "IMPLEMENTATION_REVISION",
    "Old50WaveBuildQueryV1",
    "QUERY_SCHEMA",
    "SCHEMA",
    "audit_old50_adapter_build_coverage_v1",
    "bind_old50_wave_historical_build_v1",
    "extract_old50_wave_build_query_v1",
    "load_historical_build_catalog_index_v1",
    "main",
)


if __name__ == "__main__":
    raise SystemExit(main())
