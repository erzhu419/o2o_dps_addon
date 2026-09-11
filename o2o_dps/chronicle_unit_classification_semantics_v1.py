"""Pinned official semantics for Chronicle ``UnitClassification`` events.

Chronicle serializes ``unitType`` and ``affiliation`` as protobuf ``int32``
fields, but their meaning is defined by ordinal Go enums.  This module keeps
that source distinction explicit: it preserves every wire number, attaches a
label only for values defined at the pinned upstream commit, and never treats
the numbers as WoW/CLEU flag bitmasks.

The iterator consumes the hash-verified external reconstruction admission
stream.  It adds a new top-level evidence field to CLASS rows and leaves all
pre-existing row fields unchanged.  The read-only first-encounter smoke helper
can project the small subset understood by the legacy V1 reconstructor in
memory; it neither writes an artifact nor admits the API instance into a
legacy capsule or comparison.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping

from .chronicle_external_reconstruction_admission_v1 import (
    iter_versioned_reconstruction_rows,
)


SCHEMA = "chronicle_unit_classification_semantics/v1"
IMPLEMENTATION_REVISION = "v1.0_pinned_004acd9_official_ordinal_semantics"
OFFICIAL_COMMIT = "004acd948773b7c914bb358ce09e3bc759650883"
ROW_FIELD = "chronicle_unit_classification_semantics_v1"

UNIT_TYPE_LABELS: Mapping[int, str] = MappingProxyType({
    0: "UNKNOWN",
    1: "PLAYER",
    2: "CREATURE",
    3: "OBJECT",
    4: "VEHICLE",
})
AFFILIATION_LABELS: Mapping[int, str] = MappingProxyType({
    0: "UNKNOWN",
    1: "FRIENDLY",
    2: "HOSTILE",
    3: "NEUTRAL",
})
UNIT_TYPE_DISPLAY_LABELS: Mapping[int, str] = MappingProxyType({
    0: "Unknown",
    1: "Player",
    2: "Creature",
    3: "Object",
    4: "Vehicle",
})
AFFILIATION_DISPLAY_LABELS: Mapping[int, str] = MappingProxyType({
    0: "Unknown",
    1: "Friendly",
    2: "Hostile",
    3: "Neutral",
})

# This is intentionally only the subset consumed by the existing V1
# reconstruction parser.  It is an in-memory compatibility projection, not an
# alternative definition of the official enums.
_LEGACY_RECONSTRUCTION_LABELS: Mapping[tuple[int, int], str] = MappingProxyType({
    (1, 1): "Friendly Player",
    (2, 1): "Friendly Creature",
    (3, 1): "Friendly Object",
    (2, 2): "Hostile Creature",
})


class ChronicleClassificationSemanticsError(ValueError):
    """A CLASS row conflicts with the pinned official semantics contract."""


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
        raise ChronicleClassificationSemanticsError(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChronicleClassificationSemanticsError(
            f"{field_name} must be an integer, not a bit field or coerced value"
        )
    return value


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChronicleClassificationSemanticsError(
            f"{field_name} must be an object"
        )
    return value


def official_evidence_contract() -> dict[str, Any]:
    """Return the content-addressed, pinned source basis for the resolver."""

    contract: dict[str, Any] = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "official_repository": "https://github.com/Emyrk/chronicle",
        "official_commit": OFFICIAL_COMMIT,
        "wire_contract": {
            "unit_type": {
                "protobuf_field": "UnitClassification.unitType",
                "field_number": 3,
                "wire_representation": "int32",
                "semantic_representation": "Chronicle ordinal enum; not flags",
            },
            "affiliation": {
                "protobuf_field": "UnitClassification.affiliation",
                "field_number": 4,
                "wire_representation": "int32",
                "semantic_representation": "Chronicle ordinal enum; not flags",
            },
        },
        "unit_type_mapping": [
            {
                "number": number,
                "label": label,
                "display_label": UNIT_TYPE_DISPLAY_LABELS[number],
            }
            for number, label in UNIT_TYPE_LABELS.items()
        ],
        "affiliation_mapping": [
            {
                "number": number,
                "label": label,
                "display_label": AFFILIATION_DISPLAY_LABELS[number],
            }
            for number, label in AFFILIATION_LABELS.items()
        ],
        "semantic_boundaries": {
            "zero_is_unknown_nonvoting": True,
            "zero_explicit_vs_proto3_default_distinguishable": False,
            "unit_type_creature_includes_pet_guid": True,
            "creature_vs_pet_resolvable_from_unit_type": False,
            "affiliation_is_relative_to_raid": True,
            "affiliation_can_change_with_possession": True,
            "metadata_roster_must_not_override_event_affiliation": True,
            "neutral_defined_and_frontend_supported": True,
            "neutral_reachable_from_audited_pinned_production_emitters": False,
            "out_of_range_policy": "SCHEMA_CONVENTION_CONFLICT_NONVOTING",
        },
        "official_sources": [
            {
                "role": "authoritative Go ordinal enum definitions",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/types/"
                    "classification.go#L5-L40"
                ),
                "supports": (
                    "unit type and affiliation numeric tables; Creature includes pet"
                ),
            },
            {
                "role": "protobuf wire field declarations",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/api/chronicleproto/"
                    "chronicle.proto#L196-L204"
                ),
                "supports": "plain int32 fields 3 and 4; protobuf performs no enum validation",
            },
            {
                "role": "Go to protobuf serialization",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/api/chronicleproto/types2proto/"
                    "toproto.go#L298-L316"
                ),
                "supports": "direct int32 conversion with no mask or translation",
            },
            {
                "role": "production frontend decoder",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/frontend/chronicle/src/api/"
                    "protodecode/decode.ts#L3868-L3917"
                ),
                "supports": "fields 3 and 4 decoded directly as numeric varints",
            },
            {
                "role": "production frontend semantic type",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/frontend/chronicle/src/pages/Instance/"
                    "EventsPanels/processorTypes.ts#L294-L301"
                ),
                "supports": "frontend comments state the same two numeric tables",
            },
            {
                "role": "production frontend display lookup",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/frontend/chronicle/src/pages/Instance/"
                    "EventsPanels/processors/allActivityDebug.processor.ts#L618-L634"
                ),
                "supports": "numbers index ordinal label arrays without bit decomposition",
            },
            {
                "role": "production frontend unit lookup",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/frontend/chronicle/src/pages/Instance/"
                    "EventsPanels/UnitLookup/UnitLookup.tsx#L11-L24"
                ),
                "supports": "affiliation number is used as a direct record key",
            },
            {
                "role": "GUID-derived unit type",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/guid/guid.go#L72-L105"
                ),
                "supports": "unit type comes from GUID high type, separately from flags",
            },
            {
                "role": "runtime classification and possession semantics",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/common/unitdb/"
                    "unitdb.go#L35-L69"
                ),
                "supports": "affiliation is raid-relative and possession-dependent",
            },
            {
                "role": "classification event emission",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/common/instances/"
                    "classificationemitter.go#L18-L85"
                ),
                "supports": (
                    "classification state is emitted over time, including owner/controller"
                ),
            },
            {
                "role": "WotLK base flags are separate parser fields",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/wotlk/"
                    "matcher.go#L16-L35"
                ),
                "supports": (
                    "sourceFlags and destFlags are parsed separately; pinned-tree audit found "
                    "no production read that derives UnitClassification from them"
                ),
            },
            {
                "role": "WotLK summon classification constructor",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/wotlk/"
                    "matcher.go#L718-L755"
                ),
                "supports": (
                    "summon classification derives type from GUID and emits affiliation Unknown"
                ),
            },
            {
                "role": "AzerothCore flags exclusion",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/combatlog/parser/azerothcore/"
                    "parser.go#L270-L307"
                ),
                "supports": "unitFlags is ignored rather than decoded into these fields",
            },
            {
                "role": "production frontend temporal owner/controller state",
                "url": (
                    "https://github.com/Emyrk/chronicle/blob/"
                    f"{OFFICIAL_COMMIT}/frontend/chronicle/src/pages/Instance/"
                    "EventsPanels/processors/unitState.ts#L67-L184"
                ),
                "supports": "controller is temporal and takes priority over permanent owner",
            },
        ],
    }
    digest = _sha256(contract)
    contract["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address",
        "sha256": digest,
    }
    return contract


OFFICIAL_EVIDENCE_SHA256 = official_evidence_contract()["content_address"]["sha256"]


def _resolve_ordinal(
    value: Any,
    *,
    field_name: str,
    labels: Mapping[int, str],
    display_labels: Mapping[int, str],
) -> dict[str, Any]:
    numeric = _integer(value, field_name=field_name)
    if numeric not in labels:
        return {
            "numeric": numeric,
            "label": None,
            "display_label": None,
            "defined_at_official_commit": False,
            "status": "SCHEMA_CONVENTION_CONFLICT_NONVOTING",
            "diagnostic": (
                f"{field_name}={numeric} is not defined by Chronicle at {OFFICIAL_COMMIT}"
            ),
        }
    label = labels[numeric]
    return {
        "numeric": numeric,
        "label": label,
        "display_label": display_labels[numeric],
        "defined_at_official_commit": True,
        "status": (
            "UNKNOWN_NONVOTING" if numeric == 0 else "OFFICIAL_ENUM_VALUE"
        ),
        "diagnostic": (
            "wire value 0 may be explicit Unknown or the proto3 scalar default"
            if numeric == 0
            else None
        ),
    }


def resolve_unit_type(value: Any) -> dict[str, Any]:
    """Resolve one official UnitType ordinal while preserving its number."""

    return _resolve_ordinal(
        value,
        field_name="UnitClassification.unitType",
        labels=UNIT_TYPE_LABELS,
        display_labels=UNIT_TYPE_DISPLAY_LABELS,
    )


def resolve_affiliation(value: Any) -> dict[str, Any]:
    """Resolve one official Affiliation ordinal while preserving its number."""

    return _resolve_ordinal(
        value,
        field_name="UnitClassification.affiliation",
        labels=AFFILIATION_LABELS,
        display_labels=AFFILIATION_DISPLAY_LABELS,
    )


def resolve_unit_classification(unit_type: Any, affiliation: Any) -> dict[str, Any]:
    """Resolve one numeric pair without GUID, roster, name, or flag inference."""

    resolved_type = resolve_unit_type(unit_type)
    resolved_affiliation = resolve_affiliation(affiliation)
    both_defined = (
        resolved_type["defined_at_official_commit"]
        and resolved_affiliation["defined_at_official_commit"]
    )
    has_unknown = (
        resolved_type["numeric"] == 0 or resolved_affiliation["numeric"] == 0
    )
    pair_label = (
        f"{resolved_affiliation['label']}_{resolved_type['label']}"
        if both_defined
        else None
    )
    if not both_defined:
        status = "SCHEMA_CONVENTION_CONFLICT_NONVOTING"
    elif has_unknown:
        status = "UNKNOWN_NONVOTING"
    else:
        status = "OFFICIAL_ENUM_PAIR"
    return {
        "unit_type": resolved_type,
        "affiliation": resolved_affiliation,
        "pair_label": pair_label,
        "status": status,
        "official_hostile_creature": (
            resolved_type["numeric"] == 2
            and resolved_affiliation["numeric"] == 2
            and both_defined
        ),
        "official_friendly_player": (
            resolved_type["numeric"] == 1
            and resolved_affiliation["numeric"] == 1
            and both_defined
        ),
        "semantic_label_inferred": False,
        "numeric_values_treated_as_bitmask": False,
    }


def _validated_class_message(row: Mapping[str, Any]) -> Mapping[str, Any]:
    if str(row.get("type") or "").upper() != "CLASS":
        raise ChronicleClassificationSemanticsError(
            "unit classification resolver accepts CLASS rows only"
        )
    official = _mapping(row.get("official"), field_name="row.official")
    if official.get("stream_type") != "unit_classification":
        raise ChronicleClassificationSemanticsError(
            "CLASS row official stream_type is not unit_classification"
        )
    message = _mapping(
        official.get("message"), field_name="row.official.message"
    )
    meta = _mapping(message.get("meta"), field_name="UnitClassification.meta")
    for row_key, meta_key in (("event_index", "event_index"), ("offset_ms", "offset_ms")):
        row_value = _integer(row.get(row_key), field_name=f"row.{row_key}")
        meta_value = _integer(meta.get(meta_key), field_name=f"message.meta.{meta_key}")
        if row_value != meta_value:
            raise ChronicleClassificationSemanticsError(
                f"CLASS row {row_key} conflicts with official EventMeta"
            )
    if message.get("target") != row.get("target_guid"):
        raise ChronicleClassificationSemanticsError(
            "CLASS row target_guid conflicts with official target"
        )
    unit_type = _integer(
        message.get("unit_type"), field_name="UnitClassification.unitType"
    )
    affiliation = _integer(
        message.get("affiliation"), field_name="UnitClassification.affiliation"
    )
    admission = row.get("external_admission")
    if isinstance(admission, Mapping):
        admitted_classification = admission.get("classification")
        if isinstance(admitted_classification, Mapping):
            checks = (
                ("official_unit_type_numeric", unit_type),
                ("official_affiliation_numeric", affiliation),
            )
            for key, expected in checks:
                present = admitted_classification.get(key)
                if present is not None:
                    admitted_numeric = _integer(
                        present, field_name=f"external_admission.classification.{key}"
                    )
                    if admitted_numeric != expected:
                        raise ChronicleClassificationSemanticsError(
                            f"external admission {key} conflicts with official message"
                        )
    return message


def enrich_unit_classification_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy and enrich one CLASS row after exact official-field validation."""

    message = _validated_class_message(row)
    resolved = resolve_unit_classification(
        message.get("unit_type"), message.get("affiliation")
    )
    result = dict(row)
    result[ROW_FIELD] = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "official_commit": OFFICIAL_COMMIT,
        "official_evidence_sha256": OFFICIAL_EVIDENCE_SHA256,
        "wire_values": {
            "target": message.get("target"),
            "unit_type": message.get("unit_type"),
            "affiliation": message.get("affiliation"),
            "owner": message.get("owner"),
            "controller": message.get("controller"),
            "spell_id": message.get("spell_id"),
        },
        "resolution": resolved,
        "temporal_contract": {
            "affiliation_is_event_time_state": True,
            "owner_is_permanent_relation_when_present": True,
            "controller_is_possession_relation_when_present": True,
            "controller_takes_priority_for_current_control": True,
            "metadata_roster_overrode_affiliation": False,
        },
    }
    return result


def iter_resolved_reconstruction_rows(
    admission_manifest_path: str | Path,
    *,
    instance_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield hash-verified admission rows, enriching only CLASS records."""

    for row in iter_versioned_reconstruction_rows(
        admission_manifest_path, instance_id=instance_id
    ):
        if str(row.get("type") or "").upper() == "CLASS":
            yield enrich_unit_classification_row(row)
        else:
            yield row


def project_row_for_reconstruction_smoke(row: Mapping[str, Any]) -> dict[str, Any]:
    """Make an in-memory legacy parser projection for a read-only smoke test.

    Only the four pairs explicitly understood by the legacy classifier are
    projected.  Every original field remains available in the caller's row;
    the returned copy alone receives the display outcome.  Unknown, neutral,
    hostile-player, hostile-object, hostile-vehicle, and convention-conflict
    pairs remain non-voting in that legacy parser.
    """

    if str(row.get("type") or "").upper() != "CLASS":
        return dict(row)
    # Always rederive from the official message.  A caller-supplied enrichment
    # can never bypass validation or become authoritative.
    enriched = enrich_unit_classification_row(row)
    semantics = _mapping(enriched.get(ROW_FIELD), field_name=ROW_FIELD)
    resolution = _mapping(
        semantics.get("resolution"), field_name=f"{ROW_FIELD}.resolution"
    )
    unit_type = _mapping(
        resolution.get("unit_type"), field_name="resolution.unit_type"
    ).get("numeric")
    affiliation = _mapping(
        resolution.get("affiliation"), field_name="resolution.affiliation"
    ).get("numeric")
    label = _LEGACY_RECONSTRUCTION_LABELS.get((unit_type, affiliation))
    projected = dict(enriched)
    if label is not None:
        message = _validated_class_message(projected)
        parts = [label]
        owner = message.get("owner")
        if isinstance(owner, str) and owner:
            parts.append(f"owner={owner.removeprefix('0x').removeprefix('0X')}")
        projected["outcome"] = " ".join(parts)
    projected[ROW_FIELD] = deepcopy(dict(semantics))
    projected[ROW_FIELD]["smoke_projection"] = {
        "legacy_v1_label": label,
        "projected": label is not None,
        "in_memory_only": True,
        "original_outcome": row.get("outcome"),
        "legacy_source_modified": False,
    }
    return projected


def summarize_classification_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize CLASS observations and hash their source-pinned projection."""

    unit_type_counts: Counter[str] = Counter()
    affiliation_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    target_guids: set[str] = set()
    player_targets: set[str] = set()
    hostile_targets: set[str] = set()
    hostile_creature_targets: set[str] = set()
    owner_values: set[str] = set()
    controller_values: set[str] = set()
    prior_by_encounter_target: dict[tuple[str, str], tuple[int, int]] = {}
    semantic_digest = hashlib.sha256()
    event_count = 0
    owner_event_count = 0
    owner_exact_player_resolution_count = 0
    controller_event_count = 0
    exact_metadata_player_event_count = 0
    exact_metadata_player_hostile_event_count = 0
    convention_conflict_count = 0
    unknown_nonvoting_count = 0
    temporal_pair_transition_count = 0

    for raw in rows:
        if str(raw.get("type") or "").upper() != "CLASS":
            continue
        # Always rederive rather than trusting a pre-existing semantic field.
        row = enrich_unit_classification_row(raw)
        semantics = _mapping(row.get(ROW_FIELD), field_name=ROW_FIELD)
        resolution = _mapping(
            semantics.get("resolution"), field_name=f"{ROW_FIELD}.resolution"
        )
        unit = _mapping(resolution.get("unit_type"), field_name="resolution.unit_type")
        affiliation = _mapping(
            resolution.get("affiliation"), field_name="resolution.affiliation"
        )
        unit_key = unit.get("label") or f"UNRECOGNIZED:{unit.get('numeric')}"
        affiliation_key = (
            affiliation.get("label")
            or f"UNRECOGNIZED:{affiliation.get('numeric')}"
        )
        pair_key = resolution.get("pair_label") or (
            f"UNRECOGNIZED:{affiliation.get('numeric')}:{unit.get('numeric')}"
        )
        unit_type_counts[str(unit_key)] += 1
        affiliation_counts[str(affiliation_key)] += 1
        pair_counts[str(pair_key)] += 1
        event_count += 1
        if resolution.get("status") == "SCHEMA_CONVENTION_CONFLICT_NONVOTING":
            convention_conflict_count += 1
        elif resolution.get("status") == "UNKNOWN_NONVOTING":
            unknown_nonvoting_count += 1

        wire = _mapping(semantics.get("wire_values"), field_name="wire_values")
        target = wire.get("target")
        if isinstance(target, str) and target:
            target_guids.add(target)
            if unit.get("numeric") == 1:
                player_targets.add(target)
            if affiliation.get("numeric") == 2:
                hostile_targets.add(target)
            if resolution.get("official_hostile_creature"):
                hostile_creature_targets.add(target)
            encounter = str(row.get("encounter") or "")
            key = (encounter, target)
            pair = (int(unit.get("numeric")), int(affiliation.get("numeric")))
            prior = prior_by_encounter_target.get(key)
            if prior is not None and prior != pair:
                temporal_pair_transition_count += 1
            prior_by_encounter_target[key] = pair

        owner = wire.get("owner")
        if isinstance(owner, str) and owner:
            owner_event_count += 1
            owner_values.add(owner)
        controller = wire.get("controller")
        if isinstance(controller, str) and controller:
            controller_event_count += 1
            controller_values.add(controller)

        admission = row.get("external_admission")
        if isinstance(admission, Mapping):
            if isinstance(admission.get("target_player"), Mapping):
                exact_metadata_player_event_count += 1
                if affiliation.get("numeric") == 2:
                    exact_metadata_player_hostile_event_count += 1
            owner_evidence = admission.get("owner")
            if (
                isinstance(owner_evidence, Mapping)
                and isinstance(owner_evidence.get("resolved_player"), Mapping)
            ):
                owner_exact_player_resolution_count += 1

        official = _mapping(row.get("official"), field_name="row.official")
        projection = {
            "encounter": row.get("encounter"),
            "event_index": row.get("event_index"),
            "offset_ms": row.get("offset_ms"),
            "target": target,
            "unit_type": unit.get("numeric"),
            "affiliation": affiliation.get("numeric"),
            "owner": owner,
            "controller": controller,
            "message_sha256": official.get("message_sha256"),
            "resolution_status": resolution.get("status"),
        }
        semantic_digest.update(_canonical_bytes(projection))
        semantic_digest.update(b"\n")

    return {
        "schema": SCHEMA,
        "official_commit": OFFICIAL_COMMIT,
        "official_evidence_sha256": OFFICIAL_EVIDENCE_SHA256,
        "classification_event_count": event_count,
        "unit_type_event_counts": dict(sorted(unit_type_counts.items())),
        "affiliation_event_counts": dict(sorted(affiliation_counts.items())),
        "pair_event_counts": dict(sorted(pair_counts.items())),
        "unique_target_count": len(target_guids),
        "unique_player_target_count": len(player_targets),
        "unique_hostile_target_count": len(hostile_targets),
        "unique_hostile_creature_target_count": len(hostile_creature_targets),
        "owner_event_count": owner_event_count,
        "unique_owner_value_count": len(owner_values),
        "owner_exact_metadata_player_resolution_count": (
            owner_exact_player_resolution_count
        ),
        "controller_event_count": controller_event_count,
        "unique_controller_value_count": len(controller_values),
        "exact_metadata_player_event_count": exact_metadata_player_event_count,
        "exact_metadata_player_hostile_event_count": (
            exact_metadata_player_hostile_event_count
        ),
        "temporal_pair_transition_count": temporal_pair_transition_count,
        "unknown_nonvoting_event_count": unknown_nonvoting_count,
        "schema_convention_conflict_event_count": convention_conflict_count,
        "semantic_projection_sha256": semantic_digest.hexdigest(),
    }


def first_encounter_wave_reconstruction_smoke(
    admission_manifest_path: str | Path,
    *,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Run one read-only legacy reconstruction smoke over resolved API rows."""

    # Imported lazily so the resolver remains independent of the legacy model.
    from .chronicle_encounter_reconstruction_v1 import iter_reconstructed_encounters

    rows = iter_resolved_reconstruction_rows(
        admission_manifest_path, instance_id=instance_id
    )
    try:
        first = next(rows)
    except StopIteration as error:
        raise ChronicleClassificationSemanticsError(
            "admitted stream contains no rows"
        ) from error
    first_encounter = first.get("encounter")
    if not isinstance(first_encounter, str) or not first_encounter:
        raise ChronicleClassificationSemanticsError(
            "first admitted row has no encounter id"
        )

    input_counts: Counter[str] = Counter()
    projected_counts: Counter[str] = Counter()
    input_digest = hashlib.sha256()

    def _observe_and_project(row: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        event_type = str(row.get("type") or "").upper()
        input_counts["rows"] += 1
        input_counts[event_type] += 1
        if event_type == "DEAD" and row.get("value") is not None:
            input_counts["dead_rows_with_projected_value"] += 1
        input_digest.update(
            _canonical_bytes(
                {
                    "event_index": row.get("event_index"),
                    "offset_ms": row.get("offset_ms"),
                    "type": event_type,
                    "official_message_sha256": (
                        row.get("official", {}).get("message_sha256")
                        if isinstance(row.get("official"), Mapping)
                        else None
                    ),
                }
            )
        )
        input_digest.update(b"\n")
        projected = project_row_for_reconstruction_smoke(row)
        if event_type == "CLASS":
            projection = _mapping(
                _mapping(projected.get(ROW_FIELD), field_name=ROW_FIELD).get(
                    "smoke_projection"
                ),
                field_name="smoke_projection",
            )
            if projection.get("projected"):
                projected_counts[str(projection.get("legacy_v1_label"))] += 1
            else:
                projected_counts["NONVOTING"] += 1
        yield projected

    def selected_rows() -> Iterator[dict[str, Any]]:
        if first.get("encounter") == first_encounter:
            yield from _observe_and_project(first)
        for row in rows:
            if row.get("encounter") == first_encounter:
                yield from _observe_and_project(row)

    reconstructed = list(
        iter_reconstructed_encounters(
            selected_rows(), encounter_filter=first_encounter, max_encounters=1
        )
    )
    if len(reconstructed) != 1:
        raise ChronicleClassificationSemanticsError(
            "first-encounter smoke did not produce exactly one encounter"
        )
    encounter = reconstructed[0]
    wave_count = int(encounter.get("wave_count", 0))
    target_count = sum(
        int(wave.get("target_count", 0))
        for wave in encounter.get("waves", [])
        if isinstance(wave, Mapping)
    )
    return {
        "status": (
            "READ_ONLY_COMPATIBILITY_SMOKE_PASSED_NOT_ADMITTED"
            if wave_count > 0 and target_count > 0
            else "READ_ONLY_COMPATIBILITY_SMOKE_NO_WAVES_NOT_ADMITTED"
        ),
        "instance_id": encounter.get("instance"),
        "encounter_id": first_encounter,
        "input_event_counts": dict(sorted(input_counts.items())),
        "input_anchor_projection_sha256": input_digest.hexdigest(),
        "classification_projection_counts": dict(sorted(projected_counts.items())),
        "reconstruction": encounter,
        "reconstruction_sha256": _sha256(encounter),
        "summary": {
            "wave_count": wave_count,
            "target_count": target_count,
            "explicit_hostile_creature_count": encounter.get(
                "classification_summary", {}
            ).get("explicit_hostile_creature_count"),
            "inferred_hostile_creature_count": encounter.get(
                "classification_summary", {}
            ).get("inferred_hostile_creature_count"),
            "dead_attribution_projected_to_value_count": input_counts.get(
                "dead_rows_with_projected_value", 0
            ),
        },
        "scientific_boundaries": {
            "artifact_written": False,
            "legacy_reconstruction_module_modified": False,
            "legacy_capsule_membership_changed": False,
            "comparison_authorized": False,
            "scientific_validation_claimed": False,
            "legacy_reconstructor_is_temporal_affiliation_aware": False,
            "legacy_reconstructor_is_controller_priority_aware": False,
            "slain_attribution_was_not_projected_to_damage_value": True,
        },
    }


__all__ = [
    "AFFILIATION_LABELS",
    "ChronicleClassificationSemanticsError",
    "IMPLEMENTATION_REVISION",
    "OFFICIAL_COMMIT",
    "OFFICIAL_EVIDENCE_SHA256",
    "ROW_FIELD",
    "SCHEMA",
    "UNIT_TYPE_LABELS",
    "enrich_unit_classification_row",
    "first_encounter_wave_reconstruction_smoke",
    "iter_resolved_reconstruction_rows",
    "official_evidence_contract",
    "project_row_for_reconstruction_smoke",
    "resolve_affiliation",
    "resolve_unit_classification",
    "resolve_unit_type",
    "summarize_classification_rows",
]
